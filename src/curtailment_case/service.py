"""业务服务层：数据接入、案件状态机、结算版本控制、争议清单。

关键不变量：
- 原始数据只追加，recorded_at 取自注入时钟 —— 迟到数据不会改写历史视图；
- 同一场站的限发区间只能有一个案件（任意状态），防止重复申诉；
- 案件状态机 OPEN -> APPEALED -> REVIEWED -> SETTLED，复核结论变更产生新复核版本；
- 结算版本 CONFIRMED 后不可变：复核变更或迟到数据只能生成新的结算版本。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from functools import wraps

from . import db, engine
from .clock import SystemClock
from .contracts import Attribution, CaseState, InstructionEventType, ReviewOutcome
from .timeutil import clip_interval, fmt_ts, hours_between, parse_ts, period_window


class ServiceError(Exception):
    """业务错误：code 供程序判断，message 供运营人员阅读。"""

    status = 400

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ValidationError(ServiceError):
    status = 400


class NotFoundError(ServiceError):
    status = 404


class ConflictError(ServiceError):
    status = 409


class StateError(ServiceError):
    status = 409


_CASE_TRANSITIONS = {
    CaseState.OPEN: {CaseState.APPEALED},
    CaseState.APPEALED: {CaseState.REVIEWED},
    CaseState.REVIEWED: {CaseState.REVIEWED, CaseState.SETTLED},
    CaseState.SETTLED: {CaseState.REVIEWED},
}


def _locked(fn):
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ValidationError(code, message)


class CurtailmentService:
    def __init__(self, db_path: str, clock=None):
        self._conn = db.connect(db_path)
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return fmt_ts(self._clock.now())

    def _plant_or_404(self, plant_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM plants WHERE plant_id = ?", (plant_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("PLANT_NOT_FOUND", f"未知场站: {plant_id}")
        return dict(row)

    # -------------------------------------------------------------- 主数据

    @_locked
    def create_grid_point(self, grid_point_id: str, name: str) -> dict:
        _require(bool(grid_point_id), "INVALID_GRID_POINT", "grid_point_id 不能为空")
        try:
            self._conn.execute(
                "INSERT INTO grid_points (grid_point_id, name) VALUES (?, ?)",
                (grid_point_id, name or grid_point_id),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            if "UNIQUE" in str(exc) or "PRIMARY" in str(exc):
                raise ConflictError("DUPLICATE_GRID_POINT", f"并网点已存在: {grid_point_id}")
            raise
        return {"grid_point_id": grid_point_id, "name": name or grid_point_id}

    @_locked
    def create_plant(self, plant_id: str, grid_point_id: str, name: str, capacity_mw: float) -> dict:
        _require(bool(plant_id), "INVALID_PLANT", "plant_id 不能为空")
        _require(capacity_mw and capacity_mw > 0, "INVALID_CAPACITY", "capacity_mw 必须为正")
        gp = self._conn.execute(
            "SELECT grid_point_id FROM grid_points WHERE grid_point_id = ?", (grid_point_id,)
        ).fetchone()
        if gp is None:
            raise NotFoundError("GRID_POINT_NOT_FOUND", f"未知并网点: {grid_point_id}")
        try:
            self._conn.execute(
                "INSERT INTO plants (plant_id, grid_point_id, name, capacity_mw) VALUES (?, ?, ?, ?)",
                (plant_id, grid_point_id, name or plant_id, float(capacity_mw)),
            )
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            if "UNIQUE" in str(exc) or "PRIMARY" in str(exc):
                raise ConflictError("DUPLICATE_PLANT", f"场站已存在: {plant_id}")
            raise
        return {
            "plant_id": plant_id,
            "grid_point_id": grid_point_id,
            "name": name or plant_id,
            "capacity_mw": float(capacity_mw),
        }

    # -------------------------------------------------------------- 数据接入

    @_locked
    def record_instruction(
        self,
        instruction_id: str,
        plant_id: str,
        event_type: str,
        starts_at: str,
        ends_at: str,
        cap_mw: float | None = None,
        issued_at: str | None = None,
        reason: str = "",
        actor: str = "",
    ) -> dict:
        """记录调度指令事件：issue（下发/补发）、correct（更正）、revoke（撤销）。

        补发 = 一条 recorded_at 晚于作用区间的 issue 事件，天然支持。
        """
        self._plant_or_404(plant_id)
        try:
            etype = InstructionEventType(event_type)
        except ValueError:
            raise ValidationError("INVALID_EVENT_TYPE", f"未知指令事件类型: {event_type}")
        start, end = parse_ts(starts_at), parse_ts(ends_at)
        _require(end > start, "INVALID_INTERVAL", "ends_at 必须晚于 starts_at")
        if etype is InstructionEventType.REVOKE:
            _require(cap_mw is None, "INVALID_CAP", "撤销事件不应携带 cap_mw")
        else:
            _require(cap_mw is not None and cap_mw >= 0, "INVALID_CAP", "cap_mw 必须为非负数")
        cur = self._conn.execute(
            "INSERT INTO instruction_events "
            "(instruction_id, plant_id, event_type, cap_mw, starts_at, ends_at, issued_at, "
            " recorded_at, reason, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                instruction_id,
                plant_id,
                etype.value,
                cap_mw,
                fmt_ts(start),
                fmt_ts(end),
                fmt_ts(parse_ts(issued_at)) if issued_at else self._now(),
                self._now(),
                reason or "",
                actor or "",
            ),
        )
        self._conn.commit()
        return {"event_id": cur.lastrowid, "instruction_id": instruction_id, "event_type": etype.value}

    def _ingest_intervals(self, table: str, value_col: str, plant_id: str, records: list[dict]) -> dict:
        self._plant_or_404(plant_id)
        now = self._now()
        inserted = 0
        for rec in records:
            start, end = parse_ts(rec["interval_start"]), parse_ts(rec["interval_end"])
            _require(end > start, "INVALID_INTERVAL", "interval_end 必须晚于 interval_start")
            value = float(rec[value_col])
            _require(value >= 0, "INVALID_VALUE", f"{value_col} 必须为非负数")
            self._conn.execute(
                f"INSERT INTO {table} (plant_id, interval_start, interval_end, {value_col}, "
                "source, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (plant_id, fmt_ts(start), fmt_ts(end), value, rec.get("source", ""), now),
            )
            inserted += 1
        self._conn.commit()
        return {"inserted": inserted, "recorded_at": now}

    @_locked
    def ingest_available_power(self, plant_id: str, records) -> dict:
        if isinstance(records, dict):
            records = [records]
        return self._ingest_intervals("available_power", "avg_mw", plant_id, records)

    @_locked
    def ingest_metered_energy(self, plant_id: str, records) -> dict:
        if isinstance(records, dict):
            records = [records]
        return self._ingest_intervals("metered_energy", "energy_mwh", plant_id, records)

    @_locked
    def record_fault(
        self, plant_id: str, starts_at: str, ends_at: str, derated_mw: float, description: str = ""
    ) -> dict:
        self._plant_or_404(plant_id)
        start, end = parse_ts(starts_at), parse_ts(ends_at)
        _require(end > start, "INVALID_INTERVAL", "ends_at 必须晚于 starts_at")
        _require(derated_mw >= 0, "INVALID_DERATE", "derated_mw 必须为非负数")
        cur = self._conn.execute(
            "INSERT INTO equipment_faults (plant_id, starts_at, ends_at, derated_mw, description, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (plant_id, fmt_ts(start), fmt_ts(end), float(derated_mw), description or "", self._now()),
        )
        self._conn.commit()
        return {"fault_id": cur.lastrowid}

    @_locked
    def record_network_constraint(
        self,
        grid_point_id: str,
        starts_at: str,
        ends_at: str,
        limit_mw: float,
        description: str = "",
    ) -> dict:
        gp = self._conn.execute(
            "SELECT grid_point_id FROM grid_points WHERE grid_point_id = ?", (grid_point_id,)
        ).fetchone()
        if gp is None:
            raise NotFoundError("GRID_POINT_NOT_FOUND", f"未知并网点: {grid_point_id}")
        start, end = parse_ts(starts_at), parse_ts(ends_at)
        _require(end > start, "INVALID_INTERVAL", "ends_at 必须晚于 starts_at")
        _require(limit_mw >= 0, "INVALID_LIMIT", "limit_mw 必须为非负数")
        cur = self._conn.execute(
            "INSERT INTO network_constraints (grid_point_id, starts_at, ends_at, limit_mw, "
            "description, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (grid_point_id, fmt_ts(start), fmt_ts(end), float(limit_mw), description or "", self._now()),
        )
        self._conn.commit()
        return {"constraint_id": cur.lastrowid}

    # -------------------------------------------------------------- 归因计算

    @_locked
    def compute_events(
        self, plant_id: str, start: str, end: str, as_of: str | None = None
    ) -> dict:
        self._plant_or_404(plant_id)
        ws, we = parse_ts(start), parse_ts(end)
        _require(we > ws, "INVALID_INTERVAL", "end 必须晚于 start")
        as_of_dt = parse_ts(as_of) if as_of else self._clock.now()
        return engine.compute_curtailment(self._conn, plant_id, ws, we, as_of_dt)

    @_locked
    def effective_instructions(self, plant_id: str, as_of: str | None = None) -> list[dict]:
        self._plant_or_404(plant_id)
        as_of_dt = parse_ts(as_of) if as_of else self._clock.now()
        return [
            {
                "instruction_id": iv.instruction_id,
                "plant_id": iv.plant_id,
                "starts_at": fmt_ts(iv.starts_at),
                "ends_at": fmt_ts(iv.ends_at),
                "requested_mw": float(iv.requested_mw),
            }
            for iv in engine.effective_dispatch_intervals(self._conn, plant_id, as_of_dt)
        ]

    # -------------------------------------------------------------- 案件状态机

    def _case_row(self, case_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("CASE_NOT_FOUND", f"未知案件: {case_id}")
        return dict(row)

    def _transition(self, case: dict, target: CaseState) -> None:
        current = CaseState(case["state"])
        if target not in _CASE_TRANSITIONS[current]:
            raise StateError(
                "ILLEGAL_STATE_TRANSITION",
                f"案件 {case['case_id']} 不允许从 {current.value} 转到 {target.value}",
            )

    @_locked
    def open_case(
        self, plant_id: str, starts_at: str, ends_at: str, attribution: str, reason: str = ""
    ) -> dict:
        """立案：对一段限发区间建立争议案件（此时即固定计算快照与来源引用）。"""
        self._plant_or_404(plant_id)
        start, end = parse_ts(starts_at), parse_ts(ends_at)
        _require(end > start, "INVALID_INTERVAL", "ends_at 必须晚于 starts_at")
        try:
            attr = Attribution(attribution)
        except ValueError:
            raise ValidationError("INVALID_ATTRIBUTION", f"未知归因类别: {attribution}")

        dup = self._conn.execute(
            "SELECT case_id FROM cases WHERE plant_id = ? AND starts_at < ? AND ends_at > ?",
            (plant_id, fmt_ts(end), fmt_ts(start)),
        ).fetchone()
        if dup is not None:
            raise ConflictError(
                "DUPLICATE_CASE",
                f"该限发区间与已存在案件 {dup['case_id']} 重叠，禁止重复申诉",
            )

        snapshot = self.compute_events(plant_id, fmt_ts(start), fmt_ts(end))
        if not snapshot["events"]:
            raise ValidationError("NO_CURTAILMENT", "该区间在当前可见数据下不存在限发")
        computed = {e["attribution"] for e in snapshot["events"]}
        if attr.value not in computed:
            raise ValidationError(
                "ATTRIBUTION_NOT_COMPUTED",
                f"当前计算结果不包含归因 {attr.value}（实际为: {sorted(computed)}）",
            )
        snapshot["total_curtailed_mwh"] = round(
            sum(e["energy_mwh"] for e in snapshot["events"]), 6
        )

        now = self._now()
        cur = self._conn.execute(
            "INSERT INTO cases (case_id, plant_id, starts_at, ends_at, attribution, state, "
            "reason, snapshot_json, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "pending",
                plant_id,
                fmt_ts(start),
                fmt_ts(end),
                attr.value,
                CaseState.OPEN.value,
                reason or "",
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        case_id = f"CA-{cur.lastrowid:06d}"
        self._conn.execute(
            "UPDATE cases SET case_id = ? WHERE case_pk = ?", (case_id, cur.lastrowid)
        )
        self._conn.commit()
        return self.get_case(case_id)

    @_locked
    def file_appeal(self, case_id: str, appeal_reason: str = "") -> dict:
        case = self._case_row(case_id)
        self._transition(case, CaseState.APPEALED)
        self._conn.execute(
            "UPDATE cases SET state = ?, appealed_at = ?, appeal_reason = ? WHERE case_id = ?",
            (CaseState.APPEALED.value, self._now(), appeal_reason or "", case_id),
        )
        self._conn.commit()
        return self.get_case(case_id)

    @_locked
    def review_case(
        self,
        case_id: str,
        outcome: str,
        adjusted_attribution: str | None = None,
        adjusted_energy_mwh: float | None = None,
        note: str = "",
    ) -> dict:
        """复核：APPEALED/REVIEWED/SETTLED -> REVIEWED，每次结论生成新的复核版本。

        复核结论变更不会触碰任何已确认的结算版本。
        """
        case = self._case_row(case_id)
        self._transition(case, CaseState.REVIEWED)
        try:
            oc = ReviewOutcome(outcome)
        except ValueError:
            raise ValidationError("INVALID_OUTCOME", f"未知复核结论: {outcome}")
        if adjusted_attribution is not None:
            try:
                adjusted_attribution = Attribution(adjusted_attribution).value
            except ValueError:
                raise ValidationError(
                    "INVALID_ATTRIBUTION", f"未知归因类别: {adjusted_attribution}"
                )
        if oc is ReviewOutcome.PARTIAL:
            _require(
                adjusted_energy_mwh is not None,
                "MISSING_ADJUSTED_ENERGY",
                "部分支持的结论必须给出 adjusted_energy_mwh",
            )
        if adjusted_energy_mwh is not None:
            _require(adjusted_energy_mwh >= 0, "INVALID_ENERGY", "调整后电量必须为非负数")
            adjusted_energy_mwh = float(adjusted_energy_mwh)

        version = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM review_versions WHERE case_id = ?",
            (case_id,),
        ).fetchone()["v"]
        now = self._now()
        self._conn.execute(
            "INSERT INTO review_versions (case_id, version, outcome, adjusted_attribution, "
            "adjusted_energy_mwh, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (case_id, version, oc.value, adjusted_attribution, adjusted_energy_mwh, note or "", now),
        )
        self._conn.execute(
            "UPDATE cases SET state = ?, reviewed_at = ? WHERE case_id = ?",
            (CaseState.REVIEWED.value, now, case_id),
        )
        self._conn.commit()
        return self.get_case(case_id)

    def _case_dict(self, row: dict) -> dict:
        reviews = self._conn.execute(
            "SELECT * FROM review_versions WHERE case_id = ? ORDER BY version", (row["case_id"],)
        ).fetchall()
        return {
            "case_id": row["case_id"],
            "plant_id": row["plant_id"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "attribution": row["attribution"],
            "state": row["state"],
            "reason": row["reason"],
            "appeal_reason": row["appeal_reason"],
            "opened_at": row["opened_at"],
            "appealed_at": row["appealed_at"],
            "reviewed_at": row["reviewed_at"],
            "settled_at": row["settled_at"],
            "snapshot": json.loads(row["snapshot_json"]),
            "reviews": [
                {
                    "version": r["version"],
                    "outcome": r["outcome"],
                    "adjusted_attribution": r["adjusted_attribution"],
                    "adjusted_energy_mwh": r["adjusted_energy_mwh"],
                    "note": r["note"],
                    "created_at": r["created_at"],
                }
                for r in reviews
            ],
        }

    @_locked
    def get_case(self, case_id: str) -> dict:
        return self._case_dict(self._case_row(case_id))

    @_locked
    def list_cases(self, plant_id: str | None = None, period: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM cases WHERE 1=1", []
        if plant_id:
            sql += " AND plant_id = ?"
            params.append(plant_id)
        if period:
            ws, we = period_window(period)
            sql += " AND starts_at < ? AND ends_at > ?"
            params += [fmt_ts(we), fmt_ts(ws)]
        sql += " ORDER BY starts_at, case_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._case_dict(dict(r)) for r in rows]

    # -------------------------------------------------------------- 结算版本

    def _latest_review(self, case_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM review_versions WHERE case_id = ? ORDER BY version DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _dedup_sources(event: dict) -> list[dict]:
        seen: dict[tuple, dict] = {}
        for seg in event["segments"]:
            for src in seg["sources"]:
                seen.setdefault((src["type"], src["id"]), src)
        return list(seen.values())

    @_locked
    def prepare_settlement(self, plant_id: str, period: str) -> dict:
        """准备新的结算版本（draft）。已确认版本不受任何影响。"""
        self._plant_or_404(plant_id)
        ws, we = period_window(period)
        result = engine.compute_curtailment(self._conn, plant_id, ws, we, self._clock.now())

        lines = []
        for event in result["events"]:
            lines.append(
                {
                    "start_ts": event["start_ts"],
                    "end_ts": event["end_ts"],
                    "attribution": event["attribution"],
                    "energy_mwh": event["energy_mwh"],
                    "sources": self._dedup_sources(event),
                    "case": None,
                    "compensable_mwh": round(event["energy_mwh"], 6)
                    if event["attribution"] == Attribution.DISPATCH_INSTRUCTION.value
                    else 0.0,
                }
            )

        reviewed_cases = []
        pending_cases = []
        for case in self.list_cases(plant_id, period):
            if case["state"] in (CaseState.REVIEWED.value, CaseState.SETTLED.value):
                review = self._latest_review(case["case_id"])
                if review:
                    reviewed_cases.append((case, review))
            else:
                pending_cases.append(case["case_id"])

        for case, review in reviewed_cases:
            # 将复核结论落到重叠最大的结算行上（按重叠时长比例折算电量）
            best_line, best_overlap = None, 0.0
            for line in lines:
                clip = clip_interval(
                    parse_ts(line["start_ts"]), parse_ts(line["end_ts"]),
                    parse_ts(case["starts_at"]), parse_ts(case["ends_at"]),
                )
                if clip is None:
                    continue
                overlap_h = hours_between(*clip)
                if overlap_h > best_overlap:
                    best_line, best_overlap = line, overlap_h
            if best_line is None:
                continue
            line_hours = hours_between(
                parse_ts(best_line["start_ts"]), parse_ts(best_line["end_ts"])
            )
            frac = best_overlap / line_hours if line_hours > 0 else 0.0
            outcome = review["outcome"]
            eff_attr = review["adjusted_attribution"] or best_line["attribution"]
            if outcome == ReviewOutcome.REJECTED.value:
                compensable = 0.0
            else:
                eff_energy = (
                    review["adjusted_energy_mwh"]
                    if review["adjusted_energy_mwh"] is not None
                    else best_line["energy_mwh"]
                )
                compensable = (
                    eff_energy * frac
                    if eff_attr == Attribution.DISPATCH_INSTRUCTION.value
                    else 0.0
                )
            best_line["case"] = {
                "case_id": case["case_id"],
                "outcome": outcome,
                "review_version": review["version"],
                "adjusted_attribution": review["adjusted_attribution"],
                "adjusted_energy_mwh": review["adjusted_energy_mwh"],
            }
            best_line["compensable_mwh"] = round(compensable, 6)

        payload = {"lines": lines, "pending_cases": pending_cases}
        total_curtailed = round(sum(l["energy_mwh"] for l in lines), 6)
        total_compensable = round(sum(l["compensable_mwh"] for l in lines), 6)
        content_hash = hashlib.sha256(
            json.dumps(
                {"plant_id": plant_id, "period": period, **payload},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        version_no = self._conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS v FROM settlement_versions "
            "WHERE plant_id = ? AND period = ?",
            (plant_id, period),
        ).fetchone()["v"]
        now = self._now()
        cur = self._conn.execute(
            "INSERT INTO settlement_versions (settlement_id, plant_id, period, version_no, "
            "status, payload_json, total_curtailed_mwh, total_compensable_mwh, content_hash, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "pending",
                plant_id,
                period,
                version_no,
                "draft",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                total_curtailed,
                total_compensable,
                content_hash,
                now,
            ),
        )
        settlement_id = f"SV-{cur.lastrowid:06d}"
        self._conn.execute(
            "UPDATE settlement_versions SET settlement_id = ? WHERE settlement_pk = ?",
            (settlement_id, cur.lastrowid),
        )
        self._conn.commit()
        return self.get_settlement(settlement_id)

    @_locked
    def confirm_settlement(self, settlement_id: str) -> dict:
        """确认结算版本：draft -> confirmed。confirmed 行从此不可变。"""
        row = self._conn.execute(
            "SELECT * FROM settlement_versions WHERE settlement_id = ?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("SETTLEMENT_NOT_FOUND", f"未知结算版本: {settlement_id}")
        if row["status"] != "draft":
            raise StateError(
                "SETTLEMENT_NOT_DRAFT", f"结算版本 {settlement_id} 已确认，不可重复确认或修改"
            )
        now = self._now()
        self._conn.execute(
            "UPDATE settlement_versions SET status = 'confirmed', confirmed_at = ? "
            "WHERE settlement_id = ?",
            (now, settlement_id),
        )
        # 纳入该版本的已复核案件进入 SETTLED
        ws, we = period_window(row["period"])
        self._conn.execute(
            "UPDATE cases SET state = ?, settled_at = ? "
            "WHERE plant_id = ? AND state = ? AND starts_at < ? AND ends_at > ?",
            (
                CaseState.SETTLED.value,
                now,
                row["plant_id"],
                CaseState.REVIEWED.value,
                fmt_ts(we),
                fmt_ts(ws),
            ),
        )
        self._conn.commit()
        return self.get_settlement(settlement_id)

    def _settlement_dict(self, row: dict) -> dict:
        payload = json.loads(row["payload_json"])
        return {
            "settlement_id": row["settlement_id"],
            "plant_id": row["plant_id"],
            "period": row["period"],
            "version_no": row["version_no"],
            "status": row["status"],
            "lines": payload["lines"],
            "pending_cases": payload["pending_cases"],
            "total_curtailed_mwh": row["total_curtailed_mwh"],
            "total_compensable_mwh": row["total_compensable_mwh"],
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
        }

    @_locked
    def get_settlement(self, settlement_id: str) -> dict:
        row = self._conn.execute(
            "SELECT * FROM settlement_versions WHERE settlement_id = ?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("SETTLEMENT_NOT_FOUND", f"未知结算版本: {settlement_id}")
        return self._settlement_dict(dict(row))

    @_locked
    def list_settlements(self, plant_id: str, period: str | None = None) -> list[dict]:
        sql = "SELECT * FROM settlement_versions WHERE plant_id = ?"
        params: list = [plant_id]
        if period:
            sql += " AND period = ?"
            params.append(period)
        sql += " ORDER BY version_no"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._settlement_dict(dict(r)) for r in rows]

    # -------------------------------------------------------------- 争议清单

    @_locked
    def dispute_list(self, plant_id: str, period: str) -> dict:
        """生成带来源引用的争议清单：每个案件附立案快照中的数据出处。"""
        self._plant_or_404(plant_id)
        period_window(period)  # 校验格式
        cases = self.list_cases(plant_id, period)
        items = []
        for case in cases:
            sources: dict[tuple, dict] = {}
            for event in case["snapshot"]["events"]:
                for seg in event["segments"]:
                    for src in seg["sources"]:
                        sources.setdefault((src["type"], src["id"]), src)
            latest_review = case["reviews"][-1] if case["reviews"] else None
            items.append(
                {
                    "case_id": case["case_id"],
                    "plant_id": case["plant_id"],
                    "starts_at": case["starts_at"],
                    "ends_at": case["ends_at"],
                    "state": case["state"],
                    "attribution": case["attribution"],
                    "reason": case["reason"],
                    "appeal_reason": case["appeal_reason"],
                    "opened_at": case["opened_at"],
                    "snapshot_as_of": case["snapshot"]["as_of"],
                    "snapshot_curtailed_mwh": case["snapshot"]["total_curtailed_mwh"],
                    "latest_review": latest_review,
                    "review_version_count": len(case["reviews"]),
                    "sources": list(sources.values()),
                }
            )
        return {
            "plant_id": plant_id,
            "period": period,
            "generated_at": self._now(),
            "disputes": items,
        }
