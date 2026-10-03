#!/usr/bin/env python3
"""演示:跨日限发 -> 申诉 -> 结算确认 -> 迟到数据 -> 争议清单。

运行: python3 demo.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from curtailment_case.clock import FixedClock
from curtailment_case.service import CurtailmentService
from curtailment_case.timeutil import parse_ts


def show(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    clock = FixedClock(parse_ts("2026-09-30T20:00:00Z"))
    svc = CurtailmentService(clock=clock)
    svc.create_grid_point("并网点-1", "gp-1")
    svc.create_plant("风电场-1", "gp-1", 100.0, "plant-1")

    # 可用功率 80 MW;调度 23:00-次日01:00 限到 20 MW;表计跟随指令
    points, readings = [], []
    b, end = parse_ts("2026-09-30T20:00:00Z"), parse_ts("2026-10-01T04:00:00Z")
    cs, ce = parse_ts("2026-09-30T23:00:00Z"), parse_ts("2026-10-01T01:00:00Z")
    while b < end:
        mw = 20.0 if cs <= b < ce else 80.0
        points.append({"ts": b, "mw": 80.0})
        readings.append({"start": b, "end": b + 900, "kwh": mw * 250.0})
        b += 900
    svc.add_available_power("plant-1", points, source="scada")
    svc.add_meter_readings("plant-1", readings, source="meter")

    clock.set(parse_ts("2026-09-30T22:50:00Z"))
    svc.upsert_instruction("D-1", "plant-1", 20.0, cs, ce,
                           parse_ts("2026-09-30T22:45:00Z"), source="dispatch")

    window = svc.compute_events("plant-1", parse_ts("2026-09-30T22:00:00Z"),
                                parse_ts("2026-10-01T02:00:00Z"))
    show("跨日限发事件(23:00 -> 次日 01:00)", window["events"])

    clock.set(parse_ts("2026-10-02T09:00:00Z"))
    ap = svc.create_appeal("plant-1", cs, ce, "限发电量未纳入结算")
    svc.start_review(ap["id"])
    svc.conclude(ap["id"], "accepted")

    clock.set(parse_ts("2026-10-05T09:00:00Z"))
    show("9 月结算 v1(跨月事件只取 9 月部分)", svc.confirm_settlement("plant-1", "2026-09"))

    # 月末后迟到一条更正表计:23:00-23:15 实际为 0
    clock.set(parse_ts("2026-10-07T10:00:00Z"))
    svc.add_meter_readings("plant-1", [{
        "start": parse_ts("2026-09-30T23:00:00Z"),
        "end": parse_ts("2026-09-30T23:15:00Z"), "kwh": 0.0,
    }], source="meter-late")

    show("争议清单(迟到数据 + 版本差额,带来源引用)",
         svc.dispute_list("plant-1", "2026-09"))
    show("已确认的 v1 保持不变", svc.get_settlement("plant-1", "2026-09", 1))
    show("再次确认产生 v2,旧版本仍在", svc.confirm_settlement("plant-1", "2026-09"))
    show("9 月全部结算版本", svc.list_settlements("plant-1", "2026-09"))


if __name__ == "__main__":
    main()
