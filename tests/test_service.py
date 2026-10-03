"""服务层测试：案件状态机、防重复申诉、结算版本不可变性、争议清单来源引用。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from curtailment_case.clock import FixedClock
from curtailment_case.service import (
    ConflictError,
    CurtailmentService,
    StateError,
    ValidationError,
)
from test_engine import hourly


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FixedClock("2026-03-20T09:00:00Z")
        self.svc = CurtailmentService(
            os.path.join(self.tmp.name, "svc.db"), clock=self.clock
        )
        self.addCleanup(self.svc.close)
        self.svc.create_grid_point("GP-1", "汇集站A")
        self.svc.create_plant("ST-1", "GP-1", "风电一场", 50.0)

    def seed_instruction_curtailment(self, day="2026-03-08", hour=10, count=2, cap=4.0):
        """造一段指令限发：可用 10MW，实发 4MWh/h，指令上限 4MW。"""
        intervals = hourly(day, hour, count)
        self.svc.ingest_available_power(
            "ST-1",
            [{"interval_start": s, "interval_end": e, "avg_mw": 10.0} for s, e in intervals],
        )
        self.svc.ingest_metered_energy(
            "ST-1",
            [{"interval_start": s, "interval_end": e, "energy_mwh": 4.0} for s, e in intervals],
        )
        self.svc.record_instruction(
            "DI-T", "ST-1", "issue", intervals[0][0], intervals[-1][1], cap_mw=cap
        )
        return intervals


class CaseDedupeTest(ServiceTestBase):
    def test_duplicate_case_rejected_for_overlapping_interval(self):
        self.seed_instruction_curtailment()
        case = self.svc.open_case(
            "ST-1", "2026-03-08T10:00:00Z", "2026-03-08T12:00:00Z",
            "dispatch_instruction", "限发申诉",
        )
        self.assertEqual(case["state"], "open")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.open_case(
                "ST-1", "2026-03-08T11:00:00Z", "2026-03-08T13:00:00Z",
                "dispatch_instruction",
            )
        self.assertEqual(ctx.exception.code, "DUPLICATE_CASE")

    def test_open_case_requires_existing_curtailment_and_matching_attribution(self):
        intervals = hourly("2026-03-08", 14, 1)
        self.svc.ingest_available_power(
            "ST-1", [{"interval_start": s, "interval_end": e, "avg_mw": 10.0} for s, e in intervals]
        )
        self.svc.ingest_metered_energy(
            "ST-1",
            [{"interval_start": s, "interval_end": e, "energy_mwh": 10.0} for s, e in intervals],
        )
        with self.assertRaises(ValidationError) as ctx:
            self.svc.open_case(
                "ST-1", "2026-03-08T14:00:00Z", "2026-03-08T15:00:00Z", "dispatch_instruction"
            )
        self.assertEqual(ctx.exception.code, "NO_CURTAILMENT")

        self.seed_instruction_curtailment(day="2026-03-08", hour=16, count=1)
        with self.assertRaises(ValidationError) as ctx:
            self.svc.open_case(
                "ST-1", "2026-03-08T16:00:00Z", "2026-03-08T17:00:00Z", "equipment_fault"
            )
        self.assertEqual(ctx.exception.code, "ATTRIBUTION_NOT_COMPUTED")


class CaseStateMachineTest(ServiceTestBase):
    def test_transitions_enforced(self):
        self.seed_instruction_curtailment()
        case = self.svc.open_case(
            "ST-1", "2026-03-08T10:00:00Z", "2026-03-08T12:00:00Z", "dispatch_instruction"
        )
        cid = case["case_id"]

        with self.assertRaises(StateError):
            self.svc.review_case(cid, "upheld")  # 未申诉不可复核

        self.svc.file_appeal(cid, "电量少结")
        with self.assertRaises(StateError):
            self.svc.file_appeal(cid)  # 不可重复申诉动作

        self.svc.review_case(cid, "partial", adjusted_energy_mwh=8.0)
        case = self.svc.get_case(cid)
        self.assertEqual(case["state"], "reviewed")
        self.assertEqual(len(case["reviews"]), 1)

        # 部分支持必须给出调整后电量
        with self.assertRaises(ValidationError):
            self.svc.review_case(cid, "partial")

        # 纳入已确认结算版本 -> settled
        sv = self.svc.prepare_settlement("ST-1", "2026-03")
        self.svc.confirm_settlement(sv["settlement_id"])
        self.assertEqual(self.svc.get_case(cid)["state"], "settled")

        # 结算后复核结论可变（产生新复核版本），状态回到 reviewed
        self.svc.review_case(cid, "upheld", note="调度日志补齐后改判")
        case = self.svc.get_case(cid)
        self.assertEqual(case["state"], "reviewed")
        self.assertEqual(len(case["reviews"]), 2)
        self.assertEqual(case["reviews"][1]["outcome"], "upheld")


class SettlementImmutabilityTest(ServiceTestBase):
    def test_review_change_does_not_rewrite_confirmed_settlement(self):
        self.seed_instruction_curtailment(day="2026-03-09")
        case = self.svc.open_case(
            "ST-1", "2026-03-09T10:00:00Z", "2026-03-09T12:00:00Z", "dispatch_instruction"
        )
        cid = case["case_id"]
        self.svc.file_appeal(cid)
        self.svc.review_case(cid, "partial", adjusted_energy_mwh=8.0)

        v1 = self.svc.prepare_settlement("ST-1", "2026-03")
        self.assertEqual(v1["version_no"], 1)
        self.assertEqual(v1["total_curtailed_mwh"], 12.0)
        self.assertEqual(v1["total_compensable_mwh"], 8.0)
        self.clock.advance(hours=1)
        v1 = self.svc.confirm_settlement(v1["settlement_id"])
        v1_hash, v1_confirmed_at, v1_lines = (
            v1["content_hash"], v1["confirmed_at"], v1["lines"],
        )

        # 复核结论变更：partial(8) -> upheld(全额 12)
        self.clock.advance(hours=1)
        self.svc.review_case(cid, "upheld", note="调度日志补齐")
        v2 = self.svc.prepare_settlement("ST-1", "2026-03")
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["total_compensable_mwh"], 12.0)
        self.svc.confirm_settlement(v2["settlement_id"])

        # 已确认的 v1 原封不动
        again = self.svc.get_settlement(v1["settlement_id"])
        self.assertEqual(again["status"], "confirmed")
        self.assertEqual(again["content_hash"], v1_hash)
        self.assertEqual(again["confirmed_at"], v1_confirmed_at)
        self.assertEqual(again["total_compensable_mwh"], 8.0)
        self.assertEqual(again["lines"], v1_lines)

        # 两个已确认版本共存，历史可追溯
        confirmed = [
            s for s in self.svc.list_settlements("ST-1", "2026-03")
            if s["status"] == "confirmed"
        ]
        self.assertEqual(len(confirmed), 2)

        # 已确认版本不可重复确认
        with self.assertRaises(StateError):
            self.svc.confirm_settlement(v1["settlement_id"])

    def test_late_data_creates_new_version_instead_of_mutation(self):
        intervals = hourly("2026-03-05", 8, 2)
        self.clock.set("2026-03-05T12:00:00Z")
        self.svc.ingest_available_power(
            "ST-1", [{"interval_start": s, "interval_end": e, "avg_mw": 10.0} for s, e in intervals]
        )
        self.svc.ingest_metered_energy(
            "ST-1",
            [{"interval_start": s, "interval_end": e, "energy_mwh": 10.0} for s, e in intervals],
        )
        v1 = self.svc.prepare_settlement("ST-1", "2026-03")
        self.assertEqual(v1["total_curtailed_mwh"], 0.0)
        v1 = self.svc.confirm_settlement(v1["settlement_id"])

        # 迟到更正：可用功率上修 -> 只能产生新版本
        self.clock.set("2026-03-06T09:00:00Z")
        self.svc.ingest_available_power(
            "ST-1",
            [
                {"interval_start": s, "interval_end": e, "avg_mw": 14.0, "source": "late"}
                for s, e in intervals
            ],
        )
        v2 = self.svc.prepare_settlement("ST-1", "2026-03")
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["total_curtailed_mwh"], 8.0)

        again = self.svc.get_settlement(v1["settlement_id"])
        self.assertEqual(again["total_curtailed_mwh"], 0.0)
        self.assertEqual(again["content_hash"], v1["content_hash"])
        self.assertEqual(again["status"], "confirmed")


class DisputeListTest(ServiceTestBase):
    def test_dispute_list_carries_source_citations(self):
        self.seed_instruction_curtailment()
        case = self.svc.open_case(
            "ST-1", "2026-03-08T10:00:00Z", "2026-03-08T12:00:00Z",
            "dispatch_instruction", "限发争议",
        )
        self.svc.file_appeal(case["case_id"], "少结 12 MWh")

        result = self.svc.dispute_list("ST-1", "2026-03")
        self.assertEqual(result["plant_id"], "ST-1")
        self.assertEqual(len(result["disputes"]), 1)
        item = result["disputes"][0]
        self.assertEqual(item["case_id"], case["case_id"])
        self.assertEqual(item["state"], "appealed")
        self.assertEqual(item["snapshot_curtailed_mwh"], 12.0)
        self.assertEqual(item["snapshot_as_of"], case["snapshot"]["as_of"])

        kinds = {s["type"] for s in item["sources"]}
        self.assertEqual(kinds, {"instruction_event", "available_power", "metered_energy"})
        for src in item["sources"]:
            self.assertTrue(src["id"])
            self.assertTrue(src["recorded_at"])
            self.assertTrue(src["summary"])


if __name__ == "__main__":
    unittest.main()
