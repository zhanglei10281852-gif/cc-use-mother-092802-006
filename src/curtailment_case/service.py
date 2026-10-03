"""限发事件与电量归因的领域服务。

设计要点
--------
* 统一时间轴:调度指令、可用功率、表计电量、停机申报全部对齐到 15 分钟
  结算时段(epoch // 900),跨日、跨月事件按时段切分归属。
* 双时间:每条事实带 recorded_at(注入时钟)与业务时间;补发/更正/撤销只
  追加新版本,从不改写历史。compute_events(..., as_of=T) 还原"T 时刻可见
  的信息"下的事件判定。
* 归因优先级(逐时段):设备故障(停机申报) > 指令限发(有效且目标值低于
  可用功率的调度指令) > 网络约束(剩余)。物理不可发优先于调度受限。
* 申诉状态机:submitted -> under_review -> accepted/rejected -> settled。
  同一场站、区间重叠且仍在进行中的申诉会被拒绝(避免同一限发区间重复申诉)。
* 复核结论可变更(追加 review 版本),但已确认的结算版本不可变——差异只
  能进入下一版本,并由争议清单显式呈现,绝不悄悄改写。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timezone

from .clock import SystemClock
from .store import Database
from .timeutil import (
    BUCKET_SECONDS,
    KWH_PER_MW_BUCKET,
    bucket_start,
    iso,
    period_bounds,
)

ATTR_DISPATCH = "dispatch_curtailment"   # 指令限发
ATTR_NETWORK = "network_constraint"      # 网络约束
ATTR_EQUIPMENT = "equipment_fault"       # 设备故障
ATTRIBUTIONS = (ATTR_DISPATCH, ATTR_NETWORK, ATTR_EQUIPMENT)
ATTR_LABELS = {
    ATTR_DISPATCH: "指令限发",
    ATTR_NETWORK: "网络约束",
    ATTR_EQUIPMENT: "设备故障",
}

APPEAL_OPEN_STATES = ("submitted", "under_review", "accepted", "settled")

# 争议类型
K_UNAPPEALED = "unappealed_curtailment"          # 限发事件尚无进行中的申诉
K_LATE_DATA = "late_data"                        # 迟到/补发数据(记录时间晚于事件结束)
K_BOUNDARY_SHIFT = "boundary_shift"              # 指令边界与实测限发起点相差若干时段
K_ATTR_CONFLICT = "attribution_conflict"         # 停机申报与受限指令区间重叠,归因冲突
K_ENERGY_MISMATCH = "energy_mismatch"            # 指令隐含限发量与表计损失对不上
K_REVIEW_CHANGED = "review_changed_after_confirm"  # 复核结论在结算确认后变更
K_SETTLEMENT_STALE = "settlement_stale"          # 已确认版本与当前计算结果不一致


class DomainError(Exception):
    """业务规则冲突,携带稳定的错误码与 HTTP 状态。"""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class CurtailmentService:
    def __init__(
        self,
        db: Database | None = None,
        clock=None,
        tz=timezone.utc,
        min_loss_mw: float = 0.05,
        mismatch_tolerance_kwh: float = 1000.0,
        max_shift_buckets: int = 8,
        late_instruction_seconds: int = 3600,
    ):
        self.db = db or Database()
        self.clock = clock or SystemClock()
        self.tz = tz
        self.min_loss_mw = float(min_loss_mw)
        self.mismatch_tolerance_kwh = float(mismatch_tolerance_kwh)
        self.max_shift_buckets = int(max_shift_buckets)
        self.late_instruction_seconds = int(late_instruction_seconds)

    # ------------------------------------------------------------------
    # 基础档案
    # ------------------------------------------------------------------

    def create_grid_point(self, name: str, gid: str | None = None) -> dict:
        gid = gid or _new_id("gp")
        self.db.execute(
            "INSERT INTO grid_points (id, name, recorded_at) VALUES (?,?,?)",
            (gid, name, self.clock.now()),
        )
        return {"id": gid, "name": name}

    def create_plant(self, name: str, grid_point_id: str, capacity_mw: float,
                     pid: str | None = None) -> dict:
        if not self.db.one("SELECT id FROM grid_points WHERE id=?", (grid_point_id,)):
            raise DomainError("not_found", f"并网点不存在: {grid_point_id}", 404)
        pid = pid or _new_id("plant")
        self.db.execute(
            "INSERT INTO plants (id, name, grid_point_id, capacity_mw, recorded_at)"
            " VALUES (?,?,?,?,?)",
            (pid, name, grid_point_id, float(capacity_mw), self.clock.now()),
        )
        return self.get_plant(pid)

    def get_plant(self, plant_id: str) -> dict:
        row = self.db.one("SELECT * FROM plants WHERE id=?", (plant_id,))
        if not row:
            raise DomainError("not_found", f"场站不存在: {plant_id}", 404)
        return {
            "id": row["id"],
            "name": row["name"],
            "grid_point_id": row["grid_point_id"],
            "capacity_mw": row["capacity_mw"],
        }

    # ------------------------------------------------------------------
    # 调度指令(补发/更正/撤销,保留历史版本)
    # ------------------------------------------------------------------

    def upsert_instruction(self, instruction_id: str, plant_id: str, target_mw: float,
                           start_ts: int, end_ts: int, issued_at: int,
                           source: str | None = None) -> dict:
        """新发或更正指令:同一 instruction_id 追加一个 active 版本。"""
        self.get_plant(plant_id)
        start_ts, end_ts, issued_at = int(start_ts), int(end_ts), int(issued_at)
        if end_ts <= start_ts:
            raise ValueError("指令结束时间必须晚于开始时间")
        if target_mw is None or float(target_mw) < 0:
            raise ValueError("target_mw 必须为非负数")
        row = self.db.one(
            "SELECT MAX(version) AS v FROM instructions WHERE instruction_id=?",
            (instruction_id,),
        )
        version = (row["v"] or 0) + 1
        self.db.execute(
            "INSERT INTO instructions (instruction_id, version, plant_id, target_mw,"
            " start_ts, end_ts, status, issued_at, recorded_at, source)"
            " VALUES (?,?,?,?,?,?, 'active', ?,?,?)",
            (instruction_id, version, plant_id, float(target_mw), start_ts, end_ts,
             issued_at, self.clock.now(), source),
        )
        return self._instruction_view(self.db.one(
            "SELECT * FROM instructions WHERE instruction_id=? AND version=?",
            (instruction_id, version)))

    def revoke_instruction(self, instruction_id: str) -> dict:
        """撤销指令:追加一个 revoked 版本,历史版本保留。"""
        latest = self.db.one(
            "SELECT * FROM instructions WHERE instruction_id=? ORDER BY version DESC LIMIT 1",
            (instruction_id,))
        if not latest:
            raise DomainError("not_found", f"指令不存在: {instruction_id}", 404)
        if latest["status"] != "active":
            raise DomainError("invalid_transition", f"指令 {instruction_id} 已处于撤销状态")
        now = self.clock.now()
        version = latest["version"] + 1
        self.db.execute(
            "INSERT INTO instructions (instruction_id, version, plant_id, target_mw,"
            " start_ts, end_ts, status, issued_at, recorded_at, source)"
            " VALUES (?,?,?,?,?,?, 'revoked', ?,?,?)",
            (instruction_id, version, latest["plant_id"], latest["target_mw"],
             latest["start_ts"], latest["end_ts"], now, now, latest["source"]),
        )
        return self._instruction_view(self.db.one(
            "SELECT * FROM instructions WHERE instruction_id=? AND version=?",
            (instruction_id, version)))

    def list_instructions(self, plant_id: str, as_of: int | None = None) -> list:
        """as_of 时刻可见的各指令最新版本(含已撤销)。"""
        self.get_plant(plant_id)
        as_of = self.clock.now() if as_of is None else int(as_of)
        return [self._instruction_view(r) for r in self._effective_instructions(plant_id, as_of)]

    def instruction_history(self, instruction_id: str) -> list:
        rows = self.db.all(
            "SELECT * FROM instructions WHERE instruction_id=? ORDER BY version",
            (instruction_id,))
        if not rows:
            raise DomainError("not_found", f"指令不存在: {instruction_id}", 404)
        return [self._instruction_view(r) for r in rows]

    def _effective_instructions(self, plant_id: str, as_of: int) -> list:
        rows = self.db.all(
            "SELECT * FROM instructions WHERE plant_id=? AND recorded_at<=?"
            " ORDER BY instruction_id, version",
            (plant_id, as_of))
        latest: dict[str, object] = {}
        for r in rows:
            latest[r["instruction_id"]] = r
        return list(latest.values())

    # ------------------------------------------------------------------
    # 量测数据(可用功率 / 表计电量 / 停机申报)
    # ------------------------------------------------------------------

    def add_available_power(self, plant_id: str, points: list, source: str | None = None) -> dict:
        """points: [{"ts": epoch, "mw": float}],ts 向下对齐到 15 分钟时段。"""
        self.get_plant(plant_id)
        now = self.clock.now()
        accepted = 0
        for p in points:
            ts = bucket_start(int(p["ts"]))
            mw = float(p["mw"])
            if mw < 0:
                raise ValueError("可用功率必须为非负")
            self.db.execute(
                "INSERT INTO available_power (plant_id, ts, mw, source, recorded_at)"
                " VALUES (?,?,?,?,?)",
                (plant_id, ts, mw, source, now))
            accepted += 1
        return {"plant_id": plant_id, "accepted": accepted, "recorded_at": iso(now, self.tz)}

    def add_meter_readings(self, plant_id: str, readings: list, source: str | None = None) -> dict:
        """readings: [{"start": epoch, "end": epoch, "kwh": float}]。"""
        self.get_plant(plant_id)
        now = self.clock.now()
        accepted = 0
        for r in readings:
            s, e, kwh = int(r["start"]), int(r["end"]), float(r["kwh"])
            if e <= s:
                raise ValueError("表计区间 end 必须大于 start")
            if kwh < 0:
                raise ValueError("kwh 必须为非负")
            self.db.execute(
                "INSERT INTO meter_readings (plant_id, start_ts, end_ts, kwh, source, recorded_at)"
                " VALUES (?,?,?,?,?,?)",
                (plant_id, s, e, kwh, source, now))
            accepted += 1
        return {"plant_id": plant_id, "accepted": accepted, "recorded_at": iso(now, self.tz)}

    def add_outage(self, plant_id: str, start_ts: int, end_ts: int,
                   reason: str | None = None, source: str | None = None) -> dict:
        self.get_plant(plant_id)
        start_ts, end_ts = int(start_ts), int(end_ts)
        if end_ts <= start_ts:
            raise ValueError("停机区间 end 必须大于 start")
        now = self.clock.now()
        cur = self.db.execute(
            "INSERT INTO outages (plant_id, start_ts, end_ts, reason, source, recorded_at)"
            " VALUES (?,?,?,?,?,?)",
            (plant_id, start_ts, end_ts, reason, source, now))
        return {
            "seq": cur.lastrowid, "plant_id": plant_id,
            "start": iso(start_ts, self.tz), "end": iso(end_ts, self.tz),
            "reason": reason, "recorded_at": iso(now, self.tz),
        }

    # ------------------------------------------------------------------
    # 限发事件计算(统一时间轴 + 归因)
    # ------------------------------------------------------------------

    def compute_events(self, plant_id: str, start_ts: int, end_ts: int,
                       as_of: int | None = None, with_buckets: bool = False) -> dict:
        """按 15 分钟时段扫描损失并归因,连续同归因时段合并为事件。

        as_of 为空时取当前时钟;传入历史时刻可还原"当时可见的信息"。
        """
        self.get_plant(plant_id)
        as_of = self.clock.now() if as_of is None else int(as_of)
        start_ts, end_ts = int(start_ts), int(end_ts)
        if end_ts <= start_ts:
            raise ValueError("end 必须大于 start")
        instructions = self._effective_instructions(plant_id, as_of)
        outages = self._outages(plant_id, start_ts, end_ts, as_of)
        avail = self._effective_avail(plant_id, bucket_start(start_ts), end_ts, as_of)
        actual = self._effective_actual_mw(plant_id, start_ts, end_ts, as_of)

        events, gaps = [], []
        current = None
        b = bucket_start(start_ts)
        while b < end_ts:
            a = avail.get(b)
            mw = actual.get(b)
            if a is None or mw is None:
                gaps.append(b)
                current = self._close_event(current, events, with_buckets)
            else:
                loss_mw = a["mw"] - mw
                if loss_mw > self.min_loss_mw:
                    attr, instr = self._attribute(b, a["mw"], instructions, outages)
                    bucket_info = {
                        "ts": b, "available_mw": a["mw"], "actual_mw": mw,
                        "loss_mw": loss_mw, "attribution": attr,
                        "instruction_id": instr["instruction_id"] if instr else None,
                    }
                    if current and current["attribution"] == attr and current["end_ts"] == b:
                        current["end_ts"] = b + BUCKET_SECONDS
                        current["lost_kwh"] += loss_mw * KWH_PER_MW_BUCKET
                        current["available_kwh"] += a["mw"] * KWH_PER_MW_BUCKET
                        current["actual_kwh"] += mw * KWH_PER_MW_BUCKET
                        current["buckets"].append(bucket_info)
                    else:
                        current = self._close_event(current, events, with_buckets)
                        current = {
                            "plant_id": plant_id, "attribution": attr,
                            "start_ts": b, "end_ts": b + BUCKET_SECONDS,
                            "lost_kwh": loss_mw * KWH_PER_MW_BUCKET,
                            "available_kwh": a["mw"] * KWH_PER_MW_BUCKET,
                            "actual_kwh": mw * KWH_PER_MW_BUCKET,
                            "buckets": [bucket_info],
                        }
                else:
                    current = self._close_event(current, events, with_buckets)
            b += BUCKET_SECONDS
        self._close_event(current, events, with_buckets)
        return {
            "plant_id": plant_id,
            "start_ts": start_ts, "end_ts": end_ts,
            "start": iso(start_ts, self.tz), "end": iso(end_ts, self.tz),
            "as_of": as_of, "as_of_iso": iso(as_of, self.tz),
            "events": events,
            "gaps": [iso(g, self.tz) for g in gaps],
            "gap_count": len(gaps),
        }

    def _attribute(self, bucket: int, avail_mw: float, instructions: list, outages: list):
        """逐时段归因:设备故障 > 指令限发 > 网络约束。"""
        for o in outages:
            if o["start_ts"] <= bucket < o["end_ts"]:
                return ATTR_EQUIPMENT, None
        for i in instructions:
            if (i["status"] == "active" and i["target_mw"] is not None
                    and i["start_ts"] <= bucket < i["end_ts"]
                    and i["target_mw"] < avail_mw):
                return ATTR_DISPATCH, i
        return ATTR_NETWORK, None

    def _close_event(self, current, events, with_buckets):
        if current is None:
            return None
        for key in ("lost_kwh", "available_kwh", "actual_kwh"):
            current[key] = round(current[key], 3)
        current["bucket_count"] = len(current["buckets"])
        current["event_id"] = "evt_" + hashlib.sha1(
            f"{current['plant_id']}|{current['start_ts']}|{current['end_ts']}"
            f"|{current['attribution']}".encode()
        ).hexdigest()[:10]
        current["attribution_label"] = ATTR_LABELS[current["attribution"]]
        current["start"] = iso(current["start_ts"], self.tz)
        current["end"] = iso(current["end_ts"], self.tz)
        if not with_buckets:
            current.pop("buckets", None)
        events.append(current)
        return None

    # ------------------------------------------------------------------
    # 申诉与复核(状态机)
    # ------------------------------------------------------------------

    def create_appeal(self, plant_id: str, start_ts: int, end_ts: int, reason: str,
                      attribution: str | None = None) -> dict:
        self.get_plant(plant_id)
        start_ts, end_ts = int(start_ts), int(end_ts)
        if end_ts <= start_ts:
            raise ValueError("申诉区间 end 必须大于 start")
        if not reason:
            raise ValueError("申诉原因不能为空")
        if attribution is not None and attribution not in ATTRIBUTIONS:
            raise ValueError(f"未知归因: {attribution}")
        placeholders = ",".join("?" * len(APPEAL_OPEN_STATES))
        clash = self.db.one(
            f"SELECT id, status FROM appeals WHERE plant_id=?"
            f" AND status IN ({placeholders}) AND start_ts<? AND end_ts>? LIMIT 1",
            (plant_id, *APPEAL_OPEN_STATES, end_ts, start_ts))
        if clash:
            raise DomainError(
                "duplicate_appeal",
                f"区间 [{iso(start_ts, self.tz)}, {iso(end_ts, self.tz)}) 与进行中的申诉 "
                f"{clash['id']}({clash['status']}) 重叠,同一限发区间不允许重复申诉")
        aid = _new_id("apl")
        self.db.execute(
            "INSERT INTO appeals (id, plant_id, start_ts, end_ts, attribution, reason,"
            " status, created_at) VALUES (?,?,?,?,?,?, 'submitted', ?)",
            (aid, plant_id, start_ts, end_ts, attribution, reason, self.clock.now()))
        return self.get_appeal(aid)

    def get_appeal(self, appeal_id: str) -> dict:
        row = self.db.one("SELECT * FROM appeals WHERE id=?", (appeal_id,))
        if not row:
            raise DomainError("not_found", f"申诉不存在: {appeal_id}", 404)
        return self._appeal_view(row)

    def list_appeals(self, plant_id: str | None = None) -> list:
        if plant_id:
            rows = self.db.all(
                "SELECT * FROM appeals WHERE plant_id=? ORDER BY created_at, id", (plant_id,))
        else:
            rows = self.db.all("SELECT * FROM appeals ORDER BY created_at, id")
        return [self._appeal_view(r) for r in rows]

    def start_review(self, appeal_id: str) -> dict:
        ap = self._require_appeal(appeal_id)
        if ap["status"] != "submitted":
            raise DomainError(
                "invalid_transition",
                f"申诉 {appeal_id} 状态为 {ap['status']},只有 submitted 可以开始复核")
        self.db.execute("UPDATE appeals SET status='under_review' WHERE id=?", (appeal_id,))
        return self.get_appeal(appeal_id)

    def conclude(self, appeal_id: str, decision: str, adjusted_kwh: float | None = None,
                 attribution: str | None = None, note: str | None = None) -> dict:
        """填写/变更复核结论。结论变更追加新版本,不回写历史 review。

        已 settled 的申诉允许变更结论——但已确认的结算版本不受影响,
        差异只会进入下一次确认的版本。
        """
        ap = self._require_appeal(appeal_id)
        if ap["status"] not in ("under_review", "accepted", "rejected", "settled"):
            raise DomainError(
                "invalid_transition",
                f"申诉 {appeal_id} 状态为 {ap['status']},不能填写复核结论(需先开始复核)")
        if decision not in ("accepted", "rejected"):
            raise ValueError("decision 必须是 accepted 或 rejected")
        if adjusted_kwh is not None and float(adjusted_kwh) < 0:
            raise ValueError("adjusted_kwh 必须为非负")
        if attribution is not None and attribution not in ATTRIBUTIONS:
            raise ValueError(f"未知归因: {attribution}")
        row = self.db.one("SELECT MAX(version) AS v FROM reviews WHERE appeal_id=?", (appeal_id,))
        version = (row["v"] or 0) + 1
        self.db.execute(
            "INSERT INTO reviews (appeal_id, version, decision, adjusted_kwh, attribution,"
            " note, decided_at) VALUES (?,?,?,?,?,?,?)",
            (appeal_id, version, decision,
             None if adjusted_kwh is None else float(adjusted_kwh),
             attribution, note, self.clock.now()))
        if ap["status"] != "settled":
            # settled 保持不动:它已进入某个已确认版本,结论变更留给下一版本体现
            self.db.execute("UPDATE appeals SET status=? WHERE id=?", (decision, appeal_id))
        return self.get_appeal(appeal_id)

    def withdraw(self, appeal_id: str) -> dict:
        ap = self._require_appeal(appeal_id)
        if ap["status"] not in ("submitted", "under_review"):
            raise DomainError(
                "invalid_transition",
                f"申诉 {appeal_id} 状态为 {ap['status']},不能撤回")
        self.db.execute("UPDATE appeals SET status='withdrawn' WHERE id=?", (appeal_id,))
        return self.get_appeal(appeal_id)

    def _require_appeal(self, appeal_id: str):
        row = self.db.one("SELECT * FROM appeals WHERE id=?", (appeal_id,))
        if not row:
            raise DomainError("not_found", f"申诉不存在: {appeal_id}", 404)
        return row

    # ------------------------------------------------------------------
    # 结算确认(版本不可变)
    # ------------------------------------------------------------------

    def confirm_settlement(self, plant_id: str, period: str) -> dict:
        """对当前已接受的申诉生成一个不可变的结算版本。

        内容未变化时不产生新版本(返回 unchanged=True);有变化时版本号 +1,
        旧版本原样保留。
        """
        self.get_plant(plant_id)
        p_start, p_end = period_bounds(period, self.tz)
        rows = self.db.all(
            "SELECT * FROM appeals WHERE plant_id=? AND status IN ('accepted','settled')"
            " AND start_ts<? AND end_ts>? ORDER BY start_ts, id",
            (plant_id, p_end, p_start))
        items = []
        settled_ids = []
        for ap in rows:
            review = self._latest_review(ap["id"])
            if not review or review["decision"] != "accepted":
                continue  # 最新复核结论不是 accepted 的申诉不进入结算
            s, e = max(ap["start_ts"], p_start), min(ap["end_ts"], p_end)
            window = self.compute_events(plant_id, s, e)
            computed = round(sum(ev["lost_kwh"] for ev in window["events"]), 3)
            attr = (review["attribution"] or ap["attribution"]
                    or self._dominant_attr(window["events"]))
            kwh = float(review["adjusted_kwh"]) if review["adjusted_kwh"] is not None else computed
            items.append({
                "appeal_id": ap["id"],
                "review_version": review["version"],
                "attribution": attr,
                "kwh": round(kwh, 3),
                "start_ts": s, "end_ts": e,
                "start": iso(s, self.tz), "end": iso(e, self.tz),
            })
            settled_ids.append(ap["id"])
        digest = hashlib.sha256(
            json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        latest = self.db.one(
            "SELECT * FROM settlements WHERE plant_id=? AND period=?"
            " ORDER BY version_no DESC LIMIT 1",
            (plant_id, period))
        if latest and latest["digest"] == digest:
            view = self._settlement_view(latest)
            view["unchanged"] = True
            return view
        version_no = (latest["version_no"] if latest else 0) + 1
        sid = _new_id("stl")
        total = round(sum(i["kwh"] for i in items), 3)
        self.db.execute(
            "INSERT INTO settlements (id, plant_id, period, version_no, items_json,"
            " total_kwh, digest, confirmed_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, plant_id, period, version_no,
             json.dumps(items, ensure_ascii=False, sort_keys=True),
             total, digest, self.clock.now()))
        for aid in settled_ids:
            self.db.execute(
                "UPDATE appeals SET status='settled' WHERE id=? AND status='accepted'", (aid,))
        return self.get_settlement(plant_id, period, version_no)

    def get_settlement(self, plant_id: str, period: str, version_no: int | None = None) -> dict:
        if version_no is None:
            row = self.db.one(
                "SELECT * FROM settlements WHERE plant_id=? AND period=?"
                " ORDER BY version_no DESC LIMIT 1",
                (plant_id, period))
        else:
            row = self.db.one(
                "SELECT * FROM settlements WHERE plant_id=? AND period=? AND version_no=?",
                (plant_id, period, version_no))
        if not row:
            raise DomainError(
                "not_found", f"结算版本不存在: {plant_id}/{period}/v{version_no}", 404)
        return self._settlement_view(row)

    def list_settlements(self, plant_id: str, period: str) -> list:
        rows = self.db.all(
            "SELECT * FROM settlements WHERE plant_id=? AND period=? ORDER BY version_no",
            (plant_id, period))
        return [self._settlement_view(r) for r in rows]

    # ------------------------------------------------------------------
    # 争议清单(带来源引用)
    # ------------------------------------------------------------------

    def dispute_list(self, plant_id: str, period: str) -> dict:
        """汇总一个结算周期内的争议点,每条争议附来源引用(sources)。

        来源引用包含类型、标识与记录时间,可据此回溯调度记录、场站申报、
        表计电量、申诉/复核与结算版本各自"当时说了什么"。
        """
        self.get_plant(plant_id)
        p_start, p_end = period_bounds(period, self.tz)
        now = self.clock.now()
        window = self.compute_events(plant_id, p_start, p_end, with_buckets=True)
        settlements = self.list_settlements(plant_id, period)
        latest_stl = settlements[-1] if settlements else None
        latest_items = {it["appeal_id"]: it for it in latest_stl["items"]} if latest_stl else {}
        appeals = self.db.all(
            "SELECT * FROM appeals WHERE plant_id=? AND start_ts<? AND end_ts>?"
            " ORDER BY start_ts, id",
            (plant_id, p_end, p_start))
        instructions = self._effective_instructions(plant_id, now)
        instr_by_id = {i["instruction_id"]: i for i in instructions}
        disputes: list[dict] = []

        def add(kind, summary, sources, event=None, appeal=None):
            if appeal is not None:
                related = [appeal]
            elif event is not None:
                related = [a for a in appeals
                           if a["start_ts"] < event["end_ts"] and a["end_ts"] > event["start_ts"]]
            else:
                related = []
            if any(a["status"] == "settled" for a in related):
                status = "settled"
            elif any(a["status"] in ("submitted", "under_review", "accepted") for a in related):
                status = "appealed"
            else:
                status = "open"
            anchor = (event["start_ts"] if event is not None
                      else appeal["start_ts"] if appeal is not None else p_start)
            did = "dsp_" + hashlib.sha1(
                f"{plant_id}|{period}|{kind}|{anchor}".encode()).hexdigest()[:10]
            item = {
                "dispute_id": did, "kind": kind, "plant_id": plant_id, "period": period,
                "summary": summary, "status": status, "sources": sources,
                "detected_at": iso(now, self.tz),
            }
            if event is not None:
                item["event"] = {k: event[k] for k in (
                    "event_id", "attribution", "attribution_label", "start", "end", "lost_kwh")}
            if appeal is not None:
                item["appeal"] = {"appeal_id": appeal["id"], "status": appeal["status"]}
            disputes.append(item)

        for ev in window["events"]:
            sources: list[dict] = []
            late_notes: list[str] = []
            shift_notes: list[str] = []
            ev_instr = [i for i in instructions
                        if i["start_ts"] < ev["end_ts"] and i["end_ts"] > ev["start_ts"]]
            for i in ev_instr:
                sources.append(self._instruction_source(i))
                if i["recorded_at"] > ev["end_ts"]:
                    late_notes.append(
                        f"指令 {i['instruction_id']} v{i['version']} 记录于 "
                        f"{iso(i['recorded_at'], self.tz)},晚于事件结束")
                elif i["recorded_at"] - i["issued_at"] > self.late_instruction_seconds:
                    late_notes.append(
                        f"指令 {i['instruction_id']} v{i['version']} 为补发"
                        f"(签发 {iso(i['issued_at'], self.tz)},"
                        f"记录 {iso(i['recorded_at'], self.tz)})")
                shift = (ev["start_ts"] - i["start_ts"]) // BUCKET_SECONDS
                if shift != 0 and abs(shift) <= self.max_shift_buckets:
                    shift_notes.append(
                        f"指令 {i['instruction_id']} 起于 {iso(i['start_ts'], self.tz)},"
                        f"实测限发起于 {iso(ev['start_ts'], self.tz)},相差 {shift} 个时段")
            ev_outages = self._outages(plant_id, ev["start_ts"], ev["end_ts"], now)
            for o in ev_outages:
                sources.append({
                    "type": "outage", "seq": o["seq"],
                    "start": iso(o["start_ts"], self.tz), "end": iso(o["end_ts"], self.tz),
                    "reason": o["reason"], "recorded_at": iso(o["recorded_at"], self.tz),
                })
            conflict = any(
                i["status"] == "active" and i["target_mw"] is not None
                and i["target_mw"] < max(bk["available_mw"] for bk in ev["buckets"])
                and o["start_ts"] < i["end_ts"] and i["start_ts"] < o["end_ts"]
                for o in ev_outages for i in ev_instr)
            for r in self._effective_reading_rows(plant_id, ev["start_ts"], ev["end_ts"], now):
                sources.append({
                    "type": "meter_reading", "seq": r["seq"],
                    "start": iso(r["start_ts"], self.tz), "end": iso(r["end_ts"], self.tz),
                    "kwh": r["kwh"], "source": r["source"],
                    "recorded_at": iso(r["recorded_at"], self.tz),
                })
                if r["recorded_at"] > ev["end_ts"]:
                    note = f"表计读数 #{r['seq']} 记录于 {iso(r['recorded_at'], self.tz)},晚于事件结束"
                    if r["recorded_at"] > p_end:
                        note += "(月末结账后到达)"
                    late_notes.append(note)
            agg = self.db.one(
                "SELECT COUNT(*) AS n, MAX(recorded_at) AS m FROM available_power"
                " WHERE plant_id=? AND ts>=? AND ts<? AND recorded_at<=?",
                (plant_id, ev["start_ts"], ev["end_ts"], now))
            sources.append({
                "type": "available_power", "points": agg["n"],
                "last_recorded_at": iso(agg["m"], self.tz) if agg["m"] else None,
            })
            if agg["m"] and agg["m"] > ev["end_ts"]:
                late_notes.append(
                    f"可用功率估计最新记录于 {iso(agg['m'], self.tz)},晚于事件结束")
            ev_appeals = [a for a in appeals
                          if a["start_ts"] < ev["end_ts"] and a["end_ts"] > ev["start_ts"]]
            for a in ev_appeals:
                sources.append(self._appeal_source(a))
                review = self._latest_review(a["id"])
                if review:
                    sources.append(self._review_source(review))
                if a["id"] in latest_items and latest_stl:
                    sources.append(self._settlement_source(latest_stl))
            if not any(a["status"] in APPEAL_OPEN_STATES for a in ev_appeals):
                add(K_UNAPPEALED,
                    f"限发事件 {ev['event_id']}({ev['attribution_label']},损失 "
                    f"{ev['lost_kwh']:.0f} kWh)尚无进行中的申诉",
                    sources, event=ev)
            if late_notes:
                add(K_LATE_DATA,
                    f"事件 {ev['event_id']} 存在迟到/补发数据:" + ";".join(late_notes),
                    sources, event=ev)
            if shift_notes:
                add(K_BOUNDARY_SHIFT, ";".join(shift_notes), sources, event=ev)
            if conflict:
                add(K_ATTR_CONFLICT,
                    f"事件 {ev['event_id']} 区间上停机申报与受限调度指令重叠,"
                    f"当前按设备故障归因,需人工确认",
                    sources, event=ev)
            if ev["attribution"] == ATTR_DISPATCH:
                implied = 0.0
                for bk in ev["buckets"]:
                    instr = instr_by_id.get(bk["instruction_id"])
                    if instr and instr["target_mw"] is not None:
                        implied += max(0.0, bk["available_mw"] - instr["target_mw"]) \
                            * KWH_PER_MW_BUCKET
                if abs(implied - ev["lost_kwh"]) > self.mismatch_tolerance_kwh:
                    add(K_ENERGY_MISMATCH,
                        f"事件 {ev['event_id']} 指令隐含限发量 {implied:.0f} kWh 与表计损失 "
                        f"{ev['lost_kwh']:.0f} kWh 偏差超过 {self.mismatch_tolerance_kwh:.0f} kWh,"
                        f"三方数字对不上",
                        sources, event=ev)

        # 申诉级争议:复核变更 / 已确认版本过期
        if latest_stl:
            for ap in appeals:
                if ap["status"] not in ("accepted", "settled"):
                    continue
                item = latest_items.get(ap["id"])
                if not item:
                    continue
                review = self._latest_review(ap["id"])
                sources = [self._appeal_source(ap)]
                if review:
                    sources.append(self._review_source(review))
                sources.append(self._settlement_source(latest_stl))
                if review and review["version"] > (item["review_version"] or 0):
                    add(K_REVIEW_CHANGED,
                        f"申诉 {ap['id']} 的复核结论已更新到 v{review['version']},但已确认结算"
                        f"版本 v{latest_stl['version_no']} 仍基于 v{item['review_version']};"
                        f"已确认版本保持不变,差异将进入下一版本",
                        sources, appeal=ap)
                s = max(ap["start_ts"], p_start)
                e = min(ap["end_ts"], p_end)
                current = self.compute_events(plant_id, s, e)
                computed = sum(ev["lost_kwh"] for ev in current["events"])
                current_kwh = (float(review["adjusted_kwh"])
                               if review and review["adjusted_kwh"] is not None else computed)
                if abs(current_kwh - item["kwh"]) > self.mismatch_tolerance_kwh:
                    add(K_SETTLEMENT_STALE,
                        f"申诉 {ap['id']} 当前应结算 {current_kwh:.1f} kWh,已确认版本 "
                        f"v{latest_stl['version_no']} 为 {item['kwh']:.1f} kWh,差额 "
                        f"{current_kwh - item['kwh']:+.1f} kWh 待下一版本处理",
                        sources, appeal=ap)

        return {
            "plant_id": plant_id,
            "period": period,
            "generated_at": iso(now, self.tz),
            "event_count": len(window["events"]),
            "gap_count": window["gap_count"],
            "settlement_versions": [s["version_no"] for s in settlements],
            "disputes": disputes,
        }

    # ------------------------------------------------------------------
    # 内部查询与视图
    # ------------------------------------------------------------------

    def _outages(self, plant_id, start_ts, end_ts, as_of):
        return self.db.all(
            "SELECT * FROM outages WHERE plant_id=? AND recorded_at<=?"
            " AND end_ts>? AND start_ts<? ORDER BY start_ts",
            (plant_id, as_of, start_ts, end_ts))

    def _effective_avail(self, plant_id, start_ts, end_ts, as_of):
        rows = self.db.all(
            "SELECT * FROM available_power WHERE plant_id=? AND ts>=? AND ts<?"
            " AND recorded_at<=? ORDER BY recorded_at, seq",
            (plant_id, start_ts, end_ts, as_of))
        by_ts = {}
        for r in rows:
            by_ts[r["ts"]] = r  # 同一时段多次上报,记录时间最新者生效
        return by_ts

    def _effective_actual_mw(self, plant_id, start_ts, end_ts, as_of):
        mw: dict[int, float] = {}
        for r in self._reading_rows(plant_id, start_ts, end_ts, as_of):
            hours = (r["end_ts"] - r["start_ts"]) / 3600.0
            power = r["kwh"] / 1000.0 / hours
            b = bucket_start(r["start_ts"])
            while b < r["end_ts"]:
                mw[b] = power  # 后记录的行覆盖先记录的(迟到更正生效)
                b += BUCKET_SECONDS
        return mw

    def _effective_reading_rows(self, plant_id, start_ts, end_ts, as_of):
        latest: dict[tuple, object] = {}
        for r in self._reading_rows(plant_id, start_ts, end_ts, as_of):
            latest[(r["start_ts"], r["end_ts"])] = r
        return list(latest.values())

    def _reading_rows(self, plant_id, start_ts, end_ts, as_of):
        return self.db.all(
            "SELECT * FROM meter_readings WHERE plant_id=? AND end_ts>? AND start_ts<?"
            " AND recorded_at<=? ORDER BY recorded_at, seq",
            (plant_id, start_ts, end_ts, as_of))

    def _latest_review(self, appeal_id):
        return self.db.one(
            "SELECT * FROM reviews WHERE appeal_id=? ORDER BY version DESC LIMIT 1",
            (appeal_id,))

    @staticmethod
    def _dominant_attr(events):
        best, best_kwh = None, -1.0
        for ev in events:
            if ev["lost_kwh"] > best_kwh:
                best, best_kwh = ev["attribution"], ev["lost_kwh"]
        return best

    def _instruction_view(self, r):
        return {
            "instruction_id": r["instruction_id"], "version": r["version"],
            "plant_id": r["plant_id"], "target_mw": r["target_mw"], "status": r["status"],
            "start_ts": r["start_ts"], "end_ts": r["end_ts"],
            "start": iso(r["start_ts"], self.tz), "end": iso(r["end_ts"], self.tz),
            "issued_at": iso(r["issued_at"], self.tz),
            "recorded_at": iso(r["recorded_at"], self.tz),
            "source": r["source"],
        }

    def _appeal_view(self, r):
        review = self._latest_review(r["id"])
        return {
            "id": r["id"], "plant_id": r["plant_id"], "status": r["status"],
            "attribution": r["attribution"], "reason": r["reason"],
            "start_ts": r["start_ts"], "end_ts": r["end_ts"],
            "start": iso(r["start_ts"], self.tz), "end": iso(r["end_ts"], self.tz),
            "created_at": iso(r["created_at"], self.tz),
            "latest_review": self._review_view(review) if review else None,
        }

    def _review_view(self, r):
        return {
            "appeal_id": r["appeal_id"], "version": r["version"],
            "decision": r["decision"], "adjusted_kwh": r["adjusted_kwh"],
            "attribution": r["attribution"], "note": r["note"],
            "decided_at": iso(r["decided_at"], self.tz),
        }

    def _settlement_view(self, r):
        return {
            "id": r["id"], "plant_id": r["plant_id"], "period": r["period"],
            "version_no": r["version_no"], "items": json.loads(r["items_json"]),
            "total_kwh": r["total_kwh"], "digest": r["digest"],
            "confirmed_at": iso(r["confirmed_at"], self.tz),
        }

    def _instruction_source(self, i):
        return {
            "type": "instruction", "instruction_id": i["instruction_id"],
            "version": i["version"], "status": i["status"], "target_mw": i["target_mw"],
            "start": iso(i["start_ts"], self.tz), "end": iso(i["end_ts"], self.tz),
            "issued_at": iso(i["issued_at"], self.tz),
            "recorded_at": iso(i["recorded_at"], self.tz),
        }

    def _appeal_source(self, a):
        return {
            "type": "appeal", "appeal_id": a["id"], "status": a["status"],
            "start": iso(a["start_ts"], self.tz), "end": iso(a["end_ts"], self.tz),
            "created_at": iso(a["created_at"], self.tz),
        }

    def _review_source(self, r):
        return {
            "type": "review", "appeal_id": r["appeal_id"], "version": r["version"],
            "decision": r["decision"], "adjusted_kwh": r["adjusted_kwh"],
            "decided_at": iso(r["decided_at"], self.tz),
        }

    def _settlement_source(self, s):
        return {
            "type": "settlement", "settlement_id": s["id"], "version_no": s["version_no"],
            "total_kwh": s["total_kwh"], "digest": s["digest"][:12],
            "confirmed_at": s["confirmed_at"],
        }
