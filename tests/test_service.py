"""限发事件与电量归因服务的领域测试。

使用固定时钟 + 内存 SQLite,验证:
* 跨日(跨月)限发区间按 15 分钟时段切分归属;
* 迟到/更正数据不改变已确认的结算版本,只产生新版本;
* 复核结论变更不悄悄改写已确认版本;
* 同一限发区间不允许重复申诉;
* 指令补发/更正/撤销后,as_of 查询仍能还原当时可见的信息。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from curtailment_case.clock import FixedClock
from curtailment_case.service import (
    ATTR_DISPATCH,
    ATTR_EQUIPMENT,
    ATTR_NETWORK,
    CurtailmentService,
    DomainError,
    K_ATTR_CONFLICT,
    K_BOUNDARY_SHIFT,
    K_ENERGY_MISMATCH,
    K_LATE_DATA,
    K_REVIEW_CHANGED,
    K_SETTLEMENT_STALE,
    K_UNAPPEALED,
)
from curtailment_case.timeutil import parse_ts

T0 = parse_ts("2026-09-30T20:00:00Z")
CURT_START = parse_ts("2026-09-30T23:00:00Z")
CURT_END = parse_ts("2026-10-01T01:00:00Z")  # 跨日也跨月


def make_service():
    clock = FixedClock(T0)
    svc = CurtailmentService(clock=clock)
    svc.create_grid_point("并网点-1", "gp-1")
    svc.create_plant("风电场-1", "gp-1", 100.0, "plant-1")
    return svc, clock


def seed_profile(svc, curt_start=CURT_START, curt_end=CURT_END,
                 avail_mw=80.0, curtailed_mw=20.0,
                 span_start="2026-09-30T20:00:00Z", span_end="2026-10-01T04:00:00Z"):
    """播种可用功率(恒定)与 15 分钟表计电量(限发窗口内降到 curtailed_mw)。"""
    points, readings = [], []
    b, end = parse_ts(span_start), parse_ts(span_end)
    while b < end:
        mw = curtailed_mw if curt_start <= b < curt_end else avail_mw
        points.append({"ts": b, "mw": avail_mw})
        readings.append({"start": b, "end": b + 900, "kwh": mw * 250.0})
        b += 900
    svc.add_available_power("plant-1", points, source="scada")
    svc.add_meter_readings("plant-1", readings, source="meter")


def seed_instruction(svc, clock, target_mw=20.0, start="2026-09-30T23:00:00Z",
                     end="2026-10-01T01:00:00Z", issued="2026-09-30T22:45:00Z"):
    clock.set(parse_ts("2026-09-30T22:50:00Z"))
    return svc.upsert_instruction("D-1", "plant-1", target_mw,
                                  parse_ts(start), parse_ts(end), parse_ts(issued),
                                  source="dispatch")


def make_appeal(svc, clock, start=CURT_START, end=CURT_END):
    clock.set(parse_ts("2026-10-02T09:00:00Z"))
    ap = svc.create_appeal("plant-1", start, end, "限发电量未纳入结算")
    svc.start_review(ap["id"])
    svc.conclude(ap["id"], "accepted")
    return ap


class CrossDayTests(unittest.TestCase):
    def test_cross_day_event_splits_across_months(self):
        svc, clock = make_service()
        seed_profile(svc)
        seed_instruction(svc, clock)

        window = svc.compute_events("plant-1", parse_ts("2026-09-30T22:00:00Z"),
                                    parse_ts("2026-10-01T02:00:00Z"))
        self.assertEqual(window["gap_count"], 0)
        self.assertEqual(len(window["events"]), 1)
        ev = window["events"][0]
        self.assertEqual(ev["attribution"], ATTR_DISPATCH)
        self.assertEqual(ev["start"], "2026-09-30T23:00:00+00:00")
        self.assertEqual(ev["end"], "2026-10-01T01:00:00+00:00")
        self.assertEqual(ev["bucket_count"], 8)
        # 60 MW * 2 h = 120 MWh
        self.assertEqual(ev["lost_kwh"], 120000.0)

        ap = make_appeal(svc, clock)
        clock.set(parse_ts("2026-10-05T09:00:00Z"))
        sept = svc.confirm_settlement("plant-1", "2026-09")
        octo = svc.confirm_settlement("plant-1", "2026-10")
        # 跨月事件按时段切分:9 月 4 个时段、10 月 4 个时段
        self.assertEqual(sept["total_kwh"], 60000.0)
        self.assertEqual(octo["total_kwh"], 60000.0)
        self.assertEqual(sept["items"][0]["end"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(octo["items"][0]["start"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(svc.get_appeal(ap["id"])["status"], "settled")


class LateDataTests(unittest.TestCase):
    def test_late_meter_data_creates_new_version_without_rewriting(self):
        svc, clock = make_service()
        seed_profile(svc)
        seed_instruction(svc, clock)
        ap = make_appeal(svc, clock)

        clock.set(parse_ts("2026-10-05T09:00:00Z"))
        v1 = svc.confirm_settlement("plant-1", "2026-09")
        self.assertEqual(v1["total_kwh"], 60000.0)

        # 月末后迟到一条更正表计:23:00-23:15 实际为 0(此前按 20 MW 记录)
        clock.set(parse_ts("2026-10-07T10:00:00Z"))
        svc.add_meter_readings("plant-1", [{
            "start": parse_ts("2026-09-30T23:00:00Z"),
            "end": parse_ts("2026-09-30T23:15:00Z"),
            "kwh": 0.0,
        }], source="meter-late")

        # 已确认版本原样保留
        v1_again = svc.get_settlement("plant-1", "2026-09", 1)
        self.assertEqual(v1_again["total_kwh"], 60000.0)
        self.assertEqual(v1_again["digest"], v1["digest"])

        # 争议清单同时暴露迟到数据与版本差额,且带来源引用
        disputes = svc.dispute_list("plant-1", "2026-09")
        kinds = {d["kind"] for d in disputes["disputes"]}
        self.assertIn(K_LATE_DATA, kinds)
        self.assertIn(K_SETTLEMENT_STALE, kinds)
        late = next(d for d in disputes["disputes"] if d["kind"] == K_LATE_DATA)
        self.assertIn("月末结账后到达", late["summary"])
        meter_sources = [s for s in late["sources"] if s["type"] == "meter_reading"]
        self.assertTrue(any(s["source"] == "meter-late" for s in meter_sources))
        # 每条来源引用都带类型与至少一个时间戳,可回溯"当时说了什么"
        time_keys = ("recorded_at", "created_at", "decided_at", "confirmed_at",
                     "last_recorded_at", "issued_at")
        for s in late["sources"]:
            self.assertIn("type", s)
            self.assertTrue(any(k in s and s[k] for k in time_keys),
                            f"来源缺少时间戳: {s}")

        # 再次确认产生 v2(23:00 时段损失变为 80 MW -> 65000 kWh),v1 不动
        v2 = svc.confirm_settlement("plant-1", "2026-09")
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["total_kwh"], 65000.0)
        self.assertEqual(svc.get_settlement("plant-1", "2026-09", 1)["total_kwh"], 60000.0)
        self.assertEqual(len(svc.list_settlements("plant-1", "2026-09")), 2)


class ReviewChangeTests(unittest.TestCase):
    def test_review_change_after_confirm_does_not_rewrite(self):
        svc, clock = make_service()
        seed_profile(svc)
        seed_instruction(svc, clock)
        ap = make_appeal(svc, clock)

        clock.set(parse_ts("2026-10-05T09:00:00Z"))
        v1 = svc.confirm_settlement("plant-1", "2026-09")
        self.assertEqual(v1["total_kwh"], 60000.0)

        # 结算确认后复核结论变更:认定量调整为 50000 kWh
        clock.set(parse_ts("2026-10-06T11:00:00Z"))
        svc.conclude(ap["id"], "accepted", adjusted_kwh=50000.0, note="按调度曲线核减")
        self.assertEqual(svc.get_appeal(ap["id"])["status"], "settled")  # 已结算身份不变

        # v1 不被悄悄改写
        self.assertEqual(svc.get_settlement("plant-1", "2026-09", 1)["total_kwh"], 60000.0)
        self.assertEqual(svc.get_settlement("plant-1", "2026-09", 1)["digest"], v1["digest"])

        # 争议清单显式提示复核变更
        disputes = svc.dispute_list("plant-1", "2026-09")
        kinds = {d["kind"] for d in disputes["disputes"]}
        self.assertIn(K_REVIEW_CHANGED, kinds)
        changed = next(d for d in disputes["disputes"] if d["kind"] == K_REVIEW_CHANGED)
        src_types = {s["type"] for s in changed["sources"]}
        self.assertEqual({"appeal", "review", "settlement"}, src_types)

        # 差异进入下一版本;无变化时不再产生新版本
        v2 = svc.confirm_settlement("plant-1", "2026-09")
        self.assertEqual((v2["version_no"], v2["total_kwh"]), (2, 50000.0))
        v3 = svc.confirm_settlement("plant-1", "2026-09")
        self.assertTrue(v3["unchanged"])
        self.assertEqual(v3["version_no"], 2)
        self.assertEqual(svc.get_settlement("plant-1", "2026-09", 1)["total_kwh"], 60000.0)


class AppealDedupTests(unittest.TestCase):
    def test_overlapping_appeal_is_rejected(self):
        svc, clock = make_service()
        seed_profile(svc)
        seed_instruction(svc, clock)
        make_appeal(svc, clock)

        with self.assertRaises(DomainError) as ctx:
            svc.create_appeal("plant-1", parse_ts("2026-10-01T00:00:00Z"),
                              parse_ts("2026-10-01T02:00:00Z"), "重复申诉")
        self.assertEqual(ctx.exception.code, "duplicate_appeal")

        # 不重叠的区间可以另行申诉
        other = svc.create_appeal("plant-1", parse_ts("2026-10-01T02:00:00Z"),
                                  parse_ts("2026-10-01T03:00:00Z"), "另一时段")
        # 撤回后原区间可再次申诉
        svc.withdraw(other["id"])
        again = svc.create_appeal("plant-1", parse_ts("2026-10-01T02:00:00Z"),
                                  parse_ts("2026-10-01T03:00:00Z"), "重新申诉")
        self.assertEqual(again["status"], "submitted")


class AsOfTests(unittest.TestCase):
    def test_correction_and_revoke_preserve_as_of_view(self):
        svc, clock = make_service()
        seed_profile(svc)
        seed_instruction(svc, clock)  # v1: 目标 20 MW,记录于 22:50
        t1 = clock.now()

        events_then = svc.compute_events("plant-1", CURT_START, CURT_END, as_of=t1)["events"]
        self.assertEqual(events_then[0]["attribution"], ATTR_DISPATCH)

        # 更正:目标改为 90 MW(高于可用功率,不再构成受限)-> 归因翻转
        clock.advance(3600)
        svc.upsert_instruction("D-1", "plant-1", 90.0, CURT_START, CURT_END, t1)
        events_now = svc.compute_events("plant-1", CURT_START, CURT_END)["events"]
        self.assertEqual(events_now[0]["attribution"], ATTR_NETWORK)
        # 但 t1 时刻可见的仍是 v1,事件判定不变
        events_asof = svc.compute_events("plant-1", CURT_START, CURT_END, as_of=t1)["events"]
        self.assertEqual(events_asof[0]["attribution"], ATTR_DISPATCH)

        # 撤销:追加 revoked 版本,历史仍可见
        clock.advance(3600)
        svc.revoke_instruction("D-1")
        self.assertEqual(svc.compute_events("plant-1", CURT_START, CURT_END)["events"][0]
                         ["attribution"], ATTR_NETWORK)
        self.assertEqual(svc.compute_events("plant-1", CURT_START, CURT_END, as_of=t1)
                         ["events"][0]["attribution"], ATTR_DISPATCH)

        visible_then = svc.list_instructions("plant-1", as_of=t1)
        self.assertEqual([(i["version"], i["status"], i["target_mw"]) for i in visible_then],
                         [(1, "active", 20.0)])
        visible_now = {i["instruction_id"]: i for i in svc.list_instructions("plant-1")}
        self.assertEqual(visible_now["D-1"]["status"], "revoked")
        history = svc.instruction_history("D-1")
        self.assertEqual([h["version"] for h in history], [1, 2, 3])


class AppealStateMachineTests(unittest.TestCase):
    def test_illegal_transitions_are_rejected(self):
        svc, clock = make_service()
        seed_profile(svc)
        ap = svc.create_appeal("plant-1", CURT_START, CURT_END, "测试")

        with self.assertRaises(DomainError) as ctx:  # 未开始复核不能给结论
            svc.conclude(ap["id"], "accepted")
        self.assertEqual(ctx.exception.code, "invalid_transition")

        svc.start_review(ap["id"])
        with self.assertRaises(DomainError):  # 不能重复开始复核
            svc.start_review(ap["id"])

        svc.conclude(ap["id"], "accepted")
        with self.assertRaises(DomainError):  # 已接受不能撤回
            svc.withdraw(ap["id"])
        self.assertEqual(svc.get_appeal(ap["id"])["status"], "accepted")


class AttributionTests(unittest.TestCase):
    def test_precedence_and_conflict_flag(self):
        svc, clock = make_service()
        seed_profile(svc)

        # 无指令:剩余归因为网络约束
        ev = svc.compute_events("plant-1", CURT_START, CURT_END)["events"][0]
        self.assertEqual(ev["attribution"], ATTR_NETWORK)

        # 停机申报覆盖后:设备故障
        svc.add_outage("plant-1", CURT_START, CURT_END, reason="变流器故障")
        ev = svc.compute_events("plant-1", CURT_START, CURT_END)["events"][0]
        self.assertEqual(ev["attribution"], ATTR_EQUIPMENT)

        # 再叠加受限指令:物理不可发优先,且争议清单提示归因冲突
        seed_instruction(svc, clock)
        ev = svc.compute_events("plant-1", CURT_START, CURT_END)["events"][0]
        self.assertEqual(ev["attribution"], ATTR_EQUIPMENT)
        kinds = {d["kind"] for d in svc.dispute_list("plant-1", "2026-09")["disputes"]}
        self.assertIn(K_ATTR_CONFLICT, kinds)

    def test_missing_data_is_reported_as_gaps(self):
        svc, clock = make_service()
        svc.add_available_power("plant-1",
                                [{"ts": CURT_START, "mw": 80.0}], source="scada")
        window = svc.compute_events("plant-1", CURT_START, CURT_END)
        self.assertEqual(window["events"], [])
        self.assertGreater(window["gap_count"], 0)


class DisputeDetectionTests(unittest.TestCase):
    def test_boundary_shift_one_bucket(self):
        """调度记录 23:00 起限,表计 23:15 才体现:相差一个时段。"""
        svc, clock = make_service()
        seed_profile(svc, curt_start=parse_ts("2026-09-30T23:15:00Z"))
        seed_instruction(svc, clock)

        ev = svc.compute_events("plant-1", CURT_START, CURT_END)["events"][0]
        self.assertEqual(ev["start"], "2026-09-30T23:15:00+00:00")
        disputes = svc.dispute_list("plant-1", "2026-09")["disputes"]
        shift = next(d for d in disputes if d["kind"] == K_BOUNDARY_SHIFT)
        self.assertIn("相差 1 个时段", shift["summary"])
        self.assertTrue(any(s["type"] == "instruction" for s in shift["sources"]))

    def test_energy_mismatch_between_instruction_and_meter(self):
        """9 月窗口内:指令隐含限发 60 MWh,表计损失 70 MWh,三方对不上。"""
        svc, clock = make_service()
        seed_profile(svc, curtailed_mw=10.0)
        seed_instruction(svc, clock)

        disputes = svc.dispute_list("plant-1", "2026-09")["disputes"]
        kinds = {d["kind"] for d in disputes}
        self.assertIn(K_ENERGY_MISMATCH, kinds)
        self.assertIn(K_UNAPPEALED, kinds)  # 尚未申诉
        mismatch = next(d for d in disputes if d["kind"] == K_ENERGY_MISMATCH)
        self.assertEqual(mismatch["status"], "open")
        self.assertIn("60000", mismatch["summary"])
        self.assertIn("70000", mismatch["summary"])


if __name__ == "__main__":
    unittest.main()
