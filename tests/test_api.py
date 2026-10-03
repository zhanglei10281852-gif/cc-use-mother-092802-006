"""HTTP/JSON 接口测试：真实起服务、走完整流程与错误分支。"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from curtailment_case.api import make_server
from curtailment_case.clock import FixedClock
from curtailment_case.service import CurtailmentService


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FixedClock("2026-03-15T08:00:00Z")
        self.svc = CurtailmentService(
            os.path.join(self.tmp.name, "api.db"), clock=self.clock
        )
        self.addCleanup(self.svc.close)
        self.httpd = make_server(self.svc, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def _req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _seed(self):
        self.assertEqual(
            self._req("POST", "/grid-points", {"grid_point_id": "GP-1", "name": "汇集站"})[0], 201
        )
        self.assertEqual(
            self._req(
                "POST", "/plants",
                {"plant_id": "ST-1", "grid_point_id": "GP-1", "name": "风电一场", "capacity_mw": 50},
            )[0],
            201,
        )
        records = [
            {
                "interval_start": f"2026-03-15T0{h}:00:00Z",
                "interval_end": f"2026-03-15T0{h + 1}:00:00Z",
                "avg_mw": 10,
            }
            for h in (6, 7)
        ]
        status, _ = self._req(
            "POST", "/available-power", {"plant_id": "ST-1", "records": records}
        )
        self.assertEqual(status, 201)
        records = [
            {
                "interval_start": f"2026-03-15T0{h}:00:00Z",
                "interval_end": f"2026-03-15T0{h + 1}:00:00Z",
                "energy_mwh": 4,
            }
            for h in (6, 7)
        ]
        status, _ = self._req(
            "POST", "/metered-energy", {"plant_id": "ST-1", "records": records}
        )
        self.assertEqual(status, 201)
        status, _ = self._req(
            "POST", "/instructions",
            {
                "instruction_id": "DI-1", "plant_id": "ST-1", "event_type": "issue",
                "starts_at": "2026-03-15T06:00:00Z", "ends_at": "2026-03-15T08:00:00Z",
                "cap_mw": 4, "issued_at": "2026-03-15T05:30:00Z",
            },
        )
        self.assertEqual(status, 201)

    def test_full_flow_over_http(self):
        self._seed()

        status, body = self._req(
            "GET",
            "/curtailment-events?plant_id=ST-1&start=2026-03-15T00:00:00Z&end=2026-03-16T00:00:00Z",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["events"]), 1)
        self.assertEqual(body["events"][0]["attribution"], "dispatch_instruction")
        self.assertAlmostEqual(body["events"][0]["energy_mwh"], 12.0)

        status, body = self._req("GET", "/dispatch-instructions?plant_id=ST-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["instructions"][0]["requested_mw"], 4.0)

        status, case = self._req(
            "POST", "/cases",
            {
                "plant_id": "ST-1",
                "starts_at": "2026-03-15T06:00:00Z",
                "ends_at": "2026-03-15T08:00:00Z",
                "attribution": "dispatch_instruction",
                "reason": "限发争议",
            },
        )
        self.assertEqual(status, 201)
        cid = case["case_id"]

        status, _ = self._req("POST", f"/cases/{cid}/appeal", {"appeal_reason": "少结"})
        self.assertEqual(status, 200)
        status, _ = self._req("POST", f"/cases/{cid}/review", {"outcome": "upheld"})
        self.assertEqual(status, 200)

        status, sv = self._req(
            "POST", "/settlements/prepare", {"plant_id": "ST-1", "period": "2026-03"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(sv["total_compensable_mwh"], 12.0)
        status, sv = self._req("POST", f"/settlements/{sv['settlement_id']}/confirm")
        self.assertEqual(status, 200)
        self.assertEqual(sv["status"], "confirmed")

        status, disputes = self._req("GET", "/disputes?plant_id=ST-1&period=2026-03")
        self.assertEqual(status, 200)
        self.assertEqual(len(disputes["disputes"]), 1)
        item = disputes["disputes"][0]
        self.assertEqual(item["state"], "settled")
        kinds = {s["type"] for s in item["sources"]}
        self.assertEqual(kinds, {"instruction_event", "available_power", "metered_energy"})

        status, body = self._req("GET", "/settlements?plant_id=ST-1&period=2026-03")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["settlements"]), 1)

    def test_error_responses(self):
        self._seed()
        status, body = self._req("GET", "/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

        status, body = self._req("GET", "/curtailment-events?start=2026-03-15T00:00:00Z&end=2026-03-16T00:00:00Z")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "MISSING_PARAM")

        payload = {
            "plant_id": "ST-1",
            "starts_at": "2026-03-15T06:00:00Z",
            "ends_at": "2026-03-15T08:00:00Z",
            "attribution": "dispatch_instruction",
        }
        status, case = self._req("POST", "/cases", payload)
        self.assertEqual(status, 201)
        status, body = self._req("POST", "/cases", payload)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "DUPLICATE_CASE")

        status, body = self._req(
            "POST", f"/cases/{case['case_id']}/review", {"outcome": "upheld"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "ILLEGAL_STATE_TRANSITION")

        status, body = self._req("GET", "/cases/CA-999999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "CASE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
