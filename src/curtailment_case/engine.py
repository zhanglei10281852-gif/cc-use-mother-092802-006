"""限发归因引擎。

在统一时间轴上关联：场站 -> 并网点 -> 调度指令 -> 可用功率 -> 实际电量，
对每个数据时段做归因瀑布分配，再把相邻同归因的片段合并为限发事件。

归因优先级（每个时段内按序分配，剩余进入下一类）：
    1. 设备故障 equipment_fault      —— 按故障降出力 MW 解释
    2. 指令限发 dispatch_instruction —— 按 (可用功率 - 指令上限) 解释
    3. 网络约束 network_constraint   —— 并网点限额按各场站可用功率比例分摊后解释
    4. 未归因 unattributed           —— 剩余部分

所有输入均按 recorded_at <= as_of 过滤，因此同一窗口在不同知识时点
可重算出不同结果 —— 这就是“当时可见的信息”。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from sqlite3 import Connection

from .contracts import Attribution, DispatchInterval, InstructionEventType
from .timeutil import clip_interval, fmt_ts, hours_between, parse_ts

_EPS = 1e-9


def _source(kind: str, row_id: int, recorded_at: str, summary: str) -> dict:
    return {"type": kind, "id": row_id, "recorded_at": recorded_at, "summary": summary}


def _latest_interval_rows(
    conn: Connection, table: str, plant_id: str, as_of: str, ws: str, we: str
) -> dict[tuple[str, str], dict]:
    """取 as_of 之前可见的时段数据；同一时段多次上报（迟到/更正）时取最新记录。"""
    rows = conn.execute(
        f"SELECT * FROM {table} "
        "WHERE plant_id = ? AND recorded_at <= ? AND interval_end > ? AND interval_start < ? "
        "ORDER BY interval_start, recorded_at, id",
        (plant_id, as_of, ws, we),
    ).fetchall()
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        latest[(row["interval_start"], row["interval_end"])] = dict(row)
    return latest


def _active_rows(
    conn: Connection, table: str, key_col: str, key_val: str, as_of: str, ws: str, we: str
) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM {table} "
        f"WHERE {key_col} = ? AND recorded_at <= ? AND ends_at > ? AND starts_at < ? ORDER BY id",
        (key_val, as_of, ws, we),
    ).fetchall()
    return [dict(r) for r in rows]


def _effective_instruction_rows(conn: Connection, plant_id: str, as_of: str) -> list[dict]:
    """按 recorded_at 折叠指令事件流：同一 instruction_id 取最新事件，撤销即失效。"""
    rows = conn.execute(
        "SELECT * FROM instruction_events WHERE plant_id = ? AND recorded_at <= ? "
        "ORDER BY recorded_at, event_id",
        (plant_id, as_of),
    ).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["instruction_id"]] = dict(row)
    return [
        r for r in latest.values() if r["event_type"] != InstructionEventType.REVOKE.value
    ]


def effective_dispatch_intervals(
    conn: Connection, plant_id: str, as_of: datetime
) -> list[DispatchInterval]:
    """as_of 时点可见的有效调度指令（契约视图）。"""
    return [
        DispatchInterval(
            instruction_id=r["instruction_id"],
            plant_id=r["plant_id"],
            starts_at=parse_ts(r["starts_at"]),
            ends_at=parse_ts(r["ends_at"]),
            requested_mw=Decimal(str(r["cap_mw"])),
        )
        for r in _effective_instruction_rows(conn, plant_id, fmt_ts(as_of))
    ]


def _grid_point_interval_avgs(
    conn: Connection, grid_point_id: str, as_of: str
) -> dict[tuple[str, str], float]:
    """并网点下所有场站在各时段的可用功率合计（用于网络约束按比例分摊）。"""
    rows = conn.execute(
        "SELECT ap.* FROM available_power ap "
        "JOIN plants p ON p.plant_id = ap.plant_id "
        "WHERE p.grid_point_id = ? AND ap.recorded_at <= ? "
        "ORDER BY ap.plant_id, ap.interval_start, ap.recorded_at, ap.id",
        (grid_point_id, as_of),
    ).fetchall()
    latest: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        latest[(row["plant_id"], row["interval_start"], row["interval_end"])] = dict(row)
    totals: dict[tuple[str, str], float] = {}
    for (plant_id, istart, iend), row in latest.items():
        totals[(istart, iend)] = totals.get((istart, iend), 0.0) + row["avg_mw"]
    return totals


def compute_curtailment(
    conn: Connection,
    plant_id: str,
    window_start: datetime,
    window_end: datetime,
    as_of: datetime,
) -> dict:
    """计算 [window_start, window_end) 内、as_of 时点可见的限发事件。"""
    plant = conn.execute("SELECT * FROM plants WHERE plant_id = ?", (plant_id,)).fetchone()
    if plant is None:
        raise KeyError(f"未知场站: {plant_id}")
    ws, we, as_of_s = fmt_ts(window_start), fmt_ts(window_end), fmt_ts(as_of)

    avail = _latest_interval_rows(conn, "available_power", plant_id, as_of_s, ws, we)
    metered = _latest_interval_rows(conn, "metered_energy", plant_id, as_of_s, ws, we)
    instructions = _effective_instruction_rows(conn, plant_id, as_of_s)
    faults = _active_rows(conn, "equipment_faults", "plant_id", plant_id, as_of_s, ws, we)
    constraints = _active_rows(
        conn, "network_constraints", "grid_point_id", plant["grid_point_id"], as_of_s, ws, we
    )
    gp_totals = _grid_point_interval_avgs(conn, plant["grid_point_id"], as_of_s)

    segments: list[dict] = []
    warnings: list[str] = []

    for key in sorted(avail):
        ap = avail[key]
        istart, iend = parse_ts(ap["interval_start"]), parse_ts(ap["interval_end"])
        clip = clip_interval(istart, iend, window_start, window_end)
        if clip is None:
            continue
        cs, ce = clip
        hours = hours_between(cs, ce)
        full_hours = hours_between(istart, iend)
        avg_mw = ap["avg_mw"]
        avail_mwh = avg_mw * hours
        sources = [
            _source("available_power", ap["id"], ap["recorded_at"], f"可用功率 {avg_mw:g} MW")
        ]

        meter = metered.get(key)
        if meter is None:
            warnings.append(f"{key[0]} ~ {key[1]} 缺少实际电量，该时段跳过")
            continue
        ratio = hours / full_hours if full_hours > 0 else 0.0
        meter_mwh = meter["energy_mwh"] * ratio
        sources.append(
            _source(
                "metered_energy",
                meter["id"],
                meter["recorded_at"],
                f"实际电量 {meter['energy_mwh']:g} MWh",
            )
        )

        curtailed = avail_mwh - meter_mwh
        if curtailed <= _EPS:
            continue
        remaining = curtailed

        def emit(attribution: Attribution, energy: float, extra_sources: list[dict]) -> None:
            nonlocal remaining
            if energy <= _EPS:
                return
            energy = min(energy, remaining)
            segments.append(
                {
                    "interval_start": fmt_ts(cs),
                    "interval_end": fmt_ts(ce),
                    "attribution": attribution.value,
                    "energy_mwh": energy,
                    "sources": sources + extra_sources,
                }
            )
            remaining -= energy

        # 1) 设备故障：按故障降出力解释
        fault_energy = 0.0
        fault_sources: list[dict] = []
        for fault in faults:
            ov = clip_interval(parse_ts(fault["starts_at"]), parse_ts(fault["ends_at"]), cs, ce)
            if ov is None:
                continue
            fault_energy += fault["derated_mw"] * hours_between(*ov)
            fault_sources.append(
                _source(
                    "equipment_fault",
                    fault["id"],
                    fault["recorded_at"],
                    f"故障降出力 {fault['derated_mw']:g} MW",
                )
            )
        emit(Attribution.EQUIPMENT_FAULT, fault_energy, fault_sources)

        # 2) 指令限发：取重叠指令中最严格（上限最低）的一条
        best = None
        for ins in instructions:
            ov = clip_interval(parse_ts(ins["starts_at"]), parse_ts(ins["ends_at"]), cs, ce)
            if ov is None:
                continue
            cand = (ins["cap_mw"], -ins["event_id"], hours_between(*ov), ins)
            if best is None or cand < best:
                best = cand
        if best is not None:
            cap_mw, _neg_id, ov_hours, ins = best
            explained = max(0.0, avg_mw - cap_mw) * ov_hours
            emit(
                Attribution.DISPATCH_INSTRUCTION,
                explained,
                [
                    _source(
                        "instruction_event",
                        ins["event_id"],
                        ins["recorded_at"],
                        f"指令 {ins['instruction_id']} 上限 {cap_mw:g} MW",
                    )
                ],
            )

        # 3) 网络约束：并网点限额按可用功率占比分摊
        best = None
        for con in constraints:
            ov = clip_interval(parse_ts(con["starts_at"]), parse_ts(con["ends_at"]), cs, ce)
            if ov is None:
                continue
            cand = (con["limit_mw"], -con["id"], hours_between(*ov), con)
            if best is None or cand < best:
                best = cand
        if best is not None:
            limit_mw, _neg_id, ov_hours, con = best
            total_mw = gp_totals.get(key) or avg_mw
            share_mw = limit_mw * avg_mw / total_mw if total_mw > 0 else avg_mw
            explained = max(0.0, avg_mw - share_mw) * ov_hours
            emit(
                Attribution.NETWORK_CONSTRAINT,
                explained,
                [
                    _source(
                        "network_constraint",
                        con["id"],
                        con["recorded_at"],
                        f"并网点限额 {limit_mw:g} MW，本分摊 {share_mw:g} MW",
                    )
                ],
            )

        # 4) 未归因剩余
        emit(Attribution.UNATTRIBUTED, remaining, [])

    # 同一时段可能产生多个归因片段（交错出现），先按归因分桶，
    # 再把时间上首尾相接的片段合并为一个限发事件。
    _priority = (
        Attribution.EQUIPMENT_FAULT.value,
        Attribution.DISPATCH_INSTRUCTION.value,
        Attribution.NETWORK_CONSTRAINT.value,
        Attribution.UNATTRIBUTED.value,
    )
    buckets: dict[str, list[dict]] = {}
    for seg in segments:
        buckets.setdefault(seg["attribution"], []).append(seg)

    events: list[dict] = []
    for attribution, segs in buckets.items():
        for seg in segs:
            if events and events[-1]["attribution"] == attribution and (
                events[-1]["end_ts"] == seg["interval_start"]
            ):
                events[-1]["end_ts"] = seg["interval_end"]
                events[-1]["energy_mwh"] += seg["energy_mwh"]
                events[-1]["segments"].append(seg)
            else:
                events.append(
                    {
                        "plant_id": plant_id,
                        "start_ts": seg["interval_start"],
                        "end_ts": seg["interval_end"],
                        "attribution": attribution,
                        "energy_mwh": seg["energy_mwh"],
                        "segments": [seg],
                    }
                )
    events.sort(key=lambda e: (e["start_ts"], _priority.index(e["attribution"])))
    for event in events:
        event["energy_mwh"] = round(event["energy_mwh"], 6)
        for seg in event["segments"]:
            seg["energy_mwh"] = round(seg["energy_mwh"], 6)

    return {
        "plant_id": plant_id,
        "window_start": ws,
        "window_end": we,
        "as_of": as_of_s,
        "events": events,
        "warnings": warnings,
    }
