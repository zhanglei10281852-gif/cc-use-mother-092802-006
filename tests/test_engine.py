"""归因引擎测试：固定时钟 + 临时 SQLite。

覆盖：跨日区间、指令更正/撤销的历史可见性、迟到数据、补发指令、归因瀑布、网络约束分摊。
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from curtailment_case.clock import FixedClock
from curtailment_case.service import CurtailmentService


def hourly(day: str, start_hour: int, count: int):
    """生成 count 个 1 小时时段 [(start_iso, end_iso), ...]，可跨日。"""
    base = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(hours=start_hour)
    return [
        ((base + timedelta(hours=i)).isoformat(), (base + timedelta(hours=i + 1)).isoformat())
        for i in range(count)
    ]


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FixedClock("2026-03-02T12:00:00Z")
        self.svc = CurtailmentService(
            os.path.join(self.tmp.name, "test.db"), clock=self.clock
        )
        self.addCleanup(self.svc.close)
        self.svc.create_grid_point("GP-1", "汇集站A")
        self.svc.create_plant("ST-1", "GP-1", "风电一场", 50.0)

    def ingest(self, plant, intervals, avail=None, meter=None, source="t"):
        if avail is not None:
            values = avail if isinstance(avail, list) else [avail] * len(intervals)
            self.svc.ingest_available_power(
                plant,
                [
                    {"interval_start": s, "interval_end": e, "avg_mw": v, "source": source}
                    for (s, e), v in zip(intervals, values)
                ],
            )
        if meter is not None:
            values = meter if isinstance(meter, list) else [meter] * len(intervals)
            self.svc.ingest_metered_energy(
                plant,
                [
                    {"interval_start": s, "interval_end": e, "energy_mwh": v, "source": source}
                    for (s, e), v in zip(intervals, values)
                ],
            )

    def day_events(self, plant, day, as_of=None):
        return self.svc.compute_events(
            plant, f"{day}T00:00:00Z", self._next_day(day) + "T00:00:00Z", as_of=as_of
        )["events"]

    @staticmethod
    def _next_day(day):
        dt = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(days=1)
        return dt.date().isoformat()


class CrossDayInstructionTest(EngineTestBase):
    def test_cross_day_instruction_splits_energy_by_day(self):
        # 22:00(3-1) -> 02:00(3-2) 指令限到 4MW，可用 10MW
        intervals = hourly("2026-03-01", 20, 8)
        self.ingest(
            "ST-1",
            intervals,
            avail=10,
            meter=[10, 10, 4, 4, 4, 4, 10, 10],
        )
        self.svc.record_instruction(
            "DI-1", "ST-1", "issue",
            "2026-03-01T22:00:00Z", "2026-03-02T02:00:00Z",
            cap_mw=4.0, issued_at="2026-03-01T21:30:00Z", actor="dispatch",
        )

        day1 = self.day_events("ST-1", "2026-03-01")
        self.assertEqual(len(day1), 1)
        self.assertEqual(day1[0]["attribution"], "dispatch_instruction")
        self.assertEqual(day1[0]["start_ts"], "2026-03-01T22:00:00+00:00")
        self.assertEqual(day1[0]["end_ts"], "2026-03-02T00:00:00+00:00")
        self.assertAlmostEqual(day1[0]["energy_mwh"], 12.0)  # 2h * (10-4)

        day2 = self.day_events("ST-1", "2026-03-02")
        self.assertEqual(len(day2), 1)
        self.assertEqual(day2[0]["start_ts"], "2026-03-02T00:00:00+00:00")
        self.assertEqual(day2[0]["end_ts"], "2026-03-02T02:00:00+00:00")
        self.assertAlmostEqual(day2[0]["energy_mwh"], 12.0)

        # 整月窗口下跨日合并为一个事件，边界精确落在 02:00
        month = self.svc.compute_events(
            "ST-1", "2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"
        )["events"]
        self.assertEqual(len(month), 1)
        self.assertEqual(month[0]["start_ts"], "2026-03-01T22:00:00+00:00")
        self.assertEqual(month[0]["end_ts"], "2026-03-02T02:00:00+00:00")
        self.assertAlmostEqual(month[0]["energy_mwh"], 24.0)


class InstructionHistoryTest(EngineTestBase):
    def test_correction_and_revoke_preserve_visible_history(self):
        # 08:00 功率/电量数据已入库（recorded_at=08:00，之后各时点均可见）
        self.clock.set("2026-03-10T08:00:00Z")
        intervals = hourly("2026-03-10", 10, 2)
        self.ingest("ST-1", intervals, avail=10, meter=5)  # 限发 10 MWh

        self.clock.set("2026-03-10T09:00:00Z")
        self.svc.record_instruction(
            "DI-9", "ST-1", "issue", "2026-03-10T10:00:00Z", "2026-03-10T12:00:00Z", cap_mw=4.0
        )
        self.clock.set("2026-03-10T09:30:00Z")
        self.svc.record_instruction(
            "DI-9", "ST-1", "correct", "2026-03-10T10:00:00Z", "2026-03-10T12:00:00Z", cap_mw=6.0
        )
        self.clock.set("2026-03-10T09:45:00Z")
        self.svc.record_instruction(
            "DI-9", "ST-1", "revoke", "2026-03-10T10:00:00Z", "2026-03-10T12:00:00Z"
        )

        # 09:15 可见 cap=4：指令可解释 (10-4)*2=12 >= 10，全部归指令限发
        events = self.day_events("ST-1", "2026-03-10", as_of="2026-03-10T09:15:00Z")
        self.assertEqual(
            {e["attribution"]: e["energy_mwh"] for e in events},
            {"dispatch_instruction": 10.0},
        )
        # 09:35 可见 cap=6：指令解释 8，剩余 2 未归因
        events = self.day_events("ST-1", "2026-03-10", as_of="2026-03-10T09:35:00Z")
        self.assertEqual(
            {e["attribution"]: e["energy_mwh"] for e in events},
            {"dispatch_instruction": 8.0, "unattributed": 2.0},
        )
        # 撤销后（当前）：全部未归因
        events = self.day_events("ST-1", "2026-03-10")
        self.assertEqual(
            {e["attribution"]: e["energy_mwh"] for e in events}, {"unattributed": 10.0}
        )

        # 有效指令视图同样随 as_of 变化
        iv = self.svc.effective_instructions("ST-1", as_of="2026-03-10T09:15:00Z")
        self.assertEqual([x["requested_mw"] for x in iv], [4.0])
        iv = self.svc.effective_instructions("ST-1", as_of="2026-03-10T09:35:00Z")
        self.assertEqual([x["requested_mw"] for x in iv], [6.0])
        self.assertEqual(self.svc.effective_instructions("ST-1"), [])


class LateDataTest(EngineTestBase):
    def test_late_available_power_only_visible_after_arrival(self):
        intervals = hourly("2026-03-05", 8, 2)
        self.clock.set("2026-03-05T12:00:00Z")
        self.ingest("ST-1", intervals, avail=10, meter=10)
        t1 = "2026-03-05T12:00:00Z"
        self.assertEqual(self.day_events("ST-1", "2026-03-05", as_of=t1), [])

        # 次日迟到更正：可用功率上修为 14MW
        self.clock.set("2026-03-06T09:00:00Z")
        self.ingest("ST-1", intervals, avail=14, source="late-correction")

        # 历史视图（as_of=t1）不被迟到数据改写
        self.assertEqual(self.day_events("ST-1", "2026-03-05", as_of=t1), [])
        # 当前视图出现限发 8 MWh
        events = self.day_events("ST-1", "2026-03-05")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["attribution"], "unattributed")
        self.assertAlmostEqual(events[0]["energy_mwh"], 8.0)

    def test_back_issued_instruction_not_visible_before_recording(self):
        """补发：指令声称昨日签发，但今天才录入 —— 录入前的视图看不到它。"""
        intervals = hourly("2026-03-03", 14, 2)
        self.clock.set("2026-03-04T09:00:00Z")
        self.ingest("ST-1", intervals, avail=10, meter=4)
        events = self.day_events("ST-1", "2026-03-03")
        self.assertEqual({e["attribution"] for e in events}, {"unattributed"})

        self.clock.set("2026-03-04T10:00:00Z")
        self.svc.record_instruction(
            "DI-late", "ST-1", "issue",
            "2026-03-03T14:00:00Z", "2026-03-03T16:00:00Z",
            cap_mw=4.0, issued_at="2026-03-03T13:30:00Z",
        )
        # 录入前时点的视图：仍然未归因
        events = self.day_events("ST-1", "2026-03-03", as_of="2026-03-04T09:30:00Z")
        self.assertEqual({e["attribution"] for e in events}, {"unattributed"})
        # 当前视图：归指令限发 12 MWh
        events = self.day_events("ST-1", "2026-03-03")
        self.assertEqual(
            {e["attribution"]: e["energy_mwh"] for e in events},
            {"dispatch_instruction": 12.0},
        )


class AttributionTest(EngineTestBase):
    def test_waterfall_fault_then_instruction_then_unattributed(self):
        intervals = hourly("2026-03-06", 10, 1)
        self.ingest("ST-1", intervals, avail=10, meter=1)  # 限发 9 MWh
        self.svc.record_fault(
            "ST-1", "2026-03-06T10:00:00Z", "2026-03-06T11:00:00Z", 3.0, "箱变故障"
        )
        self.svc.record_instruction(
            "DI-2", "ST-1", "issue", "2026-03-06T10:00:00Z", "2026-03-06T11:00:00Z", cap_mw=6.0
        )
        events = self.day_events("ST-1", "2026-03-06")
        self.assertEqual(
            [(e["attribution"], e["energy_mwh"]) for e in events],
            [
                ("equipment_fault", 3.0),
                ("dispatch_instruction", 4.0),
                ("unattributed", 2.0),
            ],
        )

    def test_network_constraint_prorated_across_grid_point(self):
        self.svc.create_plant("ST-2", "GP-1", "光伏二场", 30.0)
        intervals = hourly("2026-03-07", 12, 1)
        self.ingest("ST-1", intervals, avail=10, meter=5)
        self.ingest("ST-2", intervals, avail=10, meter=5)
        self.svc.record_network_constraint(
            "GP-1", "2026-03-07T12:00:00Z", "2026-03-07T13:00:00Z", 15.0, "断面限额"
        )
        # 每场站分摊 15 * 10/20 = 7.5MW → 网络约束解释 2.5MWh，其余 2.5 未归因
        for plant in ("ST-1", "ST-2"):
            events = self.day_events(plant, "2026-03-07")
            self.assertEqual(
                {e["attribution"]: e["energy_mwh"] for e in events},
                {"network_constraint": 2.5, "unattributed": 2.5},
            )


if __name__ == "__main__":
    unittest.main()
