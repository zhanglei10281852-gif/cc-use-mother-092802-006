"""HTTP/JSON 接口的端到端测试:真实起服务、走完整业务流程。"""

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from curtailment_case.api import create_server
from curtailment_case.clock import FixedClock
from curtailment_case.service import CurtailmentService
from curtailment_case.timeutil import parse_ts


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = FixedClock(parse_ts("2026-09-30T20:00:00Z"))
        cls.service = CurtailmentService(clock=cls.clock)
        cls.server = create_server(cls.service, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_full_business_flow(self):
        # 档案
        status, gp = self.req("POST", "/grid-points", {"id": "gp-1", "name": "并网点-1"})
        self.assertEqual(status, 201)
        status, plant = self.req("POST", "/plants", {
            "id": "plant-1", "name": "风电场-1", "grid_point_id": "gp-1", "capacity_mw": 100})
        self.assertEqual(status, 201)

        # 量测数据:可用功率 80 MW;23:00-次日01:00 限发到 20 MW
        points, readings = [], []
        b = parse_ts("2026-09-30T20:00:00Z")
        end = parse_ts("2026-10-01T04:00:00Z")
        cs, ce = parse_ts("2026-09-30T23:00:00Z"), parse_ts("2026-10-01T01:00:00Z")
        while b < end:
            mw = 20.0 if cs <= b < ce else 80.0
            points.append({"ts": _iso(b), "mw": 80.0})
            readings.append({"start": _iso(b), "end": _iso(b + 900), "kwh": mw * 250.0})
            b += 900
        status, _ = self.req("POST", "/available-power",
                             {"plant_id": "plant-1", "source": "scada", "points": points})
        self.assertEqual(status, 201)
        status, _ = self.req("POST", "/meter-readings",
                             {"plant_id": "plant-1", "source": "meter", "readings": readings})
        self.assertEqual(status, 201)

        # 调度指令(跨日)
        self.clock.set(parse_ts("2026-09-30T22:50:00Z"))
        status, instr = self.req("POST", "/instructions", {
            "instruction_id": "D-1", "plant_id": "plant-1", "target_mw": 20,
            "start": "2026-09-30T23:00:00Z", "end": "2026-10-01T01:00:00Z",
            "issued_at": "2026-09-30T22:45:00Z", "source": "dispatch"})
        self.assertEqual(status, 201)
        self.assertEqual(instr["version"], 1)

        # 事件:跨日区间被识别为一个指令限发事件
        status, window = self.req(
            "GET", "/events?plant_id=plant-1"
            "&start=2026-09-30T22:00:00Z&end=2026-10-01T02:00:00Z")
        self.assertEqual(status, 200)
        self.assertEqual(len(window["events"]), 1)
        self.assertEqual(window["events"][0]["lost_kwh"], 120000.0)
        self.assertEqual(window["events"][0]["attribution"], "dispatch_curtailment")

        # 申诉 -> 复核 -> 结算确认
        self.clock.set(parse_ts("2026-10-02T09:00:00Z"))
        status, appeal = self.req("POST", "/appeals", {
            "plant_id": "plant-1", "start": "2026-09-30T23:00:00Z",
            "end": "2026-10-01T01:00:00Z", "reason": "限发电量未纳入结算"})
        self.assertEqual(status, 201)
        aid = appeal["id"]

        status, dup = self.req("POST", "/appeals", {
            "plant_id": "plant-1", "start": "2026-10-01T00:00:00Z",
            "end": "2026-10-01T02:00:00Z", "reason": "重复"})
        self.assertEqual(status, 409)
        self.assertEqual(dup["error"]["code"], "duplicate_appeal")

        status, bad = self.req("POST", f"/appeals/{aid}/conclude", {"decision": "accepted"})
        self.assertEqual(status, 409)  # 未开始复核
        self.assertEqual(bad["error"]["code"], "invalid_transition")

        self.assertEqual(self.req("POST", f"/appeals/{aid}/start-review")[0], 200)
        status, reviewed = self.req("POST", f"/appeals/{aid}/conclude",
                                    {"decision": "accepted"})
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["status"], "accepted")

        self.clock.set(parse_ts("2026-10-05T09:00:00Z"))
        status, stl = self.req("POST", "/settlements/confirm",
                               {"plant_id": "plant-1", "period": "2026-09"})
        self.assertEqual(status, 201)
        self.assertEqual(stl["total_kwh"], 60000.0)
        self.assertEqual(stl["version_no"], 1)

        # 迟到数据:月末后更正 23:00-23:15 表计为 0
        self.clock.set(parse_ts("2026-10-07T10:00:00Z"))
        status, _ = self.req("POST", "/meter-readings", {
            "plant_id": "plant-1", "source": "meter-late",
            "readings": [{"start": "2026-09-30T23:00:00Z",
                          "end": "2026-09-30T23:15:00Z", "kwh": 0}]})
        self.assertEqual(status, 201)

        # 争议清单:迟到数据 + 版本差额,每条带来源引用
        status, disputes = self.req("GET", "/disputes?plant_id=plant-1&period=2026-09")
        self.assertEqual(status, 200)
        kinds = {d["kind"] for d in disputes["disputes"]}
        self.assertIn("late_data", kinds)
        self.assertIn("settlement_stale", kinds)
        for d in disputes["disputes"]:
            self.assertTrue(d["sources"], "每条争议必须带来源引用")
            for s in d["sources"]:
                self.assertIn("type", s)
        late = next(d for d in disputes["disputes"] if d["kind"] == "late_data")
        self.assertTrue(any(s["type"] == "meter_reading" and s.get("source") == "meter-late"
                            for s in late["sources"]))

        # 已确认版本不被改写;再次确认产生 v2
        status, v1 = self.req("GET",
                              "/settlements?plant_id=plant-1&period=2026-09&version=1")
        self.assertEqual(status, 200)
        self.assertEqual(v1["total_kwh"], 60000.0)
        status, v2 = self.req("POST", "/settlements/confirm",
                              {"plant_id": "plant-1", "period": "2026-09"})
        self.assertEqual(status, 201)
        self.assertEqual((v2["version_no"], v2["total_kwh"]), (2, 65000.0))
        status, v1_after = self.req("GET",
                                    "/settlements?plant_id=plant-1&period=2026-09&version=1")
        self.assertEqual(v1_after["total_kwh"], 60000.0)

    def test_bad_request_and_not_found(self):
        status, err = self.req("POST", "/instructions", {"instruction_id": "X"})
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "bad_request")
        status, err = self.req("GET", "/no-such-route")
        self.assertEqual(status, 404)
        status, err = self.req("GET", "/events?plant_id=ghost&start=2026-09-30T00:00:00Z"
                                      "&end=2026-09-30T01:00:00Z")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"]["code"], "not_found")


def _iso(ts):
    from curtailment_case.timeutil import iso
    return iso(ts)


if __name__ == "__main__":
    unittest.main()
