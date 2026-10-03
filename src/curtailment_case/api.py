"""HTTP/JSON 接口(仅标准库)。

启动:
    python -m curtailment_case.api --host 127.0.0.1 --port 8080 --db case.db

所有时间字段均为带时区的 ISO-8601 字符串;查询参数中的 ISO 时间建议
使用 'Z' 或对 '+' 做 URL 编码。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .clock import SystemClock
from .service import CurtailmentService, DomainError
from .store import Database
from .timeutil import parse_ts

ENDPOINTS = [
    "POST /grid-points {name, id?}",
    "POST /plants {name, grid_point_id, capacity_mw, id?}",
    "POST /instructions {instruction_id, plant_id, target_mw, start, end, issued_at, source?}",
    "POST /instructions/{instruction_id}/revoke",
    "GET  /instructions?plant_id[&as_of]",
    "GET  /instructions/{instruction_id}/history",
    "POST /available-power {plant_id, source?, points:[{ts, mw}]}",
    "POST /meter-readings {plant_id, source?, readings:[{start, end, kwh}]}",
    "POST /outages {plant_id, start, end, reason?, source?}",
    "GET  /events?plant_id&start&end[&as_of]",
    "POST /appeals {plant_id, start, end, reason, attribution?}",
    "GET  /appeals[?plant_id]",
    "POST /appeals/{id}/start-review",
    "POST /appeals/{id}/conclude {decision, adjusted_kwh?, attribution?, note?}",
    "POST /appeals/{id}/withdraw",
    "POST /settlements/confirm {plant_id, period}",
    "GET  /settlements?plant_id&period[&version]",
    "GET  /disputes?plant_id&period",
]


def _need(mapping, *names):
    missing = [n for n in names if mapping.get(n) is None]
    if missing:
        raise ValueError(f"缺少必填字段: {', '.join(missing)}")
    return [mapping[n] for n in names]


def make_handler(service: CurtailmentService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CurtailmentCase/1.0"

        def log_message(self, *args):  # 保持测试输出干净
            pass

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _send(self, code: int, obj) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                result = self._route(method, path, query)
                if result is None:
                    self._send(404, {"error": {"code": "not_found",
                                               "message": f"{method} {path} 不存在"}})
                else:
                    self._send(result[0], result[1])
            except DomainError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self._send(400, {"error": {"code": "bad_request", "message": str(exc)}})
            except sqlite3.IntegrityError as exc:
                self._send(400, {"error": {"code": "bad_request",
                                           "message": f"数据约束冲突: {exc}"}})

        def _route(self, method: str, path: str, query: dict):
            body = self._body() if method == "POST" else {}
            seg = [s for s in path.split("/") if s]
            svc = service

            if method == "GET" and not seg:
                return 200, {"service": "curtailment-case", "endpoints": ENDPOINTS}

            if method == "POST" and seg == ["grid-points"]:
                (name,) = _need(body, "name")
                return 201, svc.create_grid_point(name, body.get("id"))

            if method == "POST" and seg == ["plants"]:
                name, gp, cap = _need(body, "name", "grid_point_id", "capacity_mw")
                return 201, svc.create_plant(name, gp, cap, body.get("id"))

            if method == "POST" and seg == ["instructions"]:
                iid, plant, target, start, end, issued = _need(
                    body, "instruction_id", "plant_id", "target_mw", "start", "end", "issued_at")
                return 201, svc.upsert_instruction(
                    iid, plant, target, parse_ts(start), parse_ts(end), parse_ts(issued),
                    body.get("source"))

            if method == "GET" and seg == ["instructions"]:
                (plant,) = _need(query, "plant_id")
                as_of = parse_ts(query["as_of"]) if query.get("as_of") else None
                return 200, {"instructions": svc.list_instructions(plant, as_of)}

            if len(seg) == 3 and seg[0] == "instructions" and seg[2] == "revoke" \
                    and method == "POST":
                return 200, svc.revoke_instruction(seg[1])

            if len(seg) == 3 and seg[0] == "instructions" and seg[2] == "history" \
                    and method == "GET":
                return 200, {"history": svc.instruction_history(seg[1])}

            if method == "POST" and seg == ["available-power"]:
                (plant,) = _need(body, "plant_id")
                points = [{"ts": parse_ts(p["ts"]), "mw": p["mw"]}
                          for p in body.get("points") or []]
                return 201, svc.add_available_power(plant, points, body.get("source"))

            if method == "POST" and seg == ["meter-readings"]:
                (plant,) = _need(body, "plant_id")
                readings = [{"start": parse_ts(r["start"]), "end": parse_ts(r["end"]),
                             "kwh": r["kwh"]} for r in body.get("readings") or []]
                return 201, svc.add_meter_readings(plant, readings, body.get("source"))

            if method == "POST" and seg == ["outages"]:
                plant, start, end = _need(body, "plant_id", "start", "end")
                return 201, svc.add_outage(plant, parse_ts(start), parse_ts(end),
                                           body.get("reason"), body.get("source"))

            if method == "GET" and seg == ["events"]:
                plant, start, end = _need(query, "plant_id", "start", "end")
                as_of = parse_ts(query["as_of"]) if query.get("as_of") else None
                return 200, svc.compute_events(plant, parse_ts(start), parse_ts(end), as_of)

            if method == "POST" and seg == ["appeals"]:
                plant, start, end, reason = _need(body, "plant_id", "start", "end", "reason")
                return 201, svc.create_appeal(plant, parse_ts(start), parse_ts(end),
                                              reason, body.get("attribution"))

            if method == "GET" and seg == ["appeals"]:
                return 200, {"appeals": svc.list_appeals(query.get("plant_id"))}

            if len(seg) == 3 and seg[0] == "appeals" and method == "POST":
                aid, action = seg[1], seg[2]
                if action == "start-review":
                    return 200, svc.start_review(aid)
                if action == "withdraw":
                    return 200, svc.withdraw(aid)
                if action == "conclude":
                    (decision,) = _need(body, "decision")
                    return 200, svc.conclude(aid, decision, body.get("adjusted_kwh"),
                                             body.get("attribution"), body.get("note"))

            if method == "POST" and seg == ["settlements", "confirm"]:
                plant, period = _need(body, "plant_id", "period")
                return 201, svc.confirm_settlement(plant, period)

            if method == "GET" and seg == ["settlements"]:
                plant, period = _need(query, "plant_id", "period")
                if query.get("version"):
                    return 200, svc.get_settlement(plant, period, int(query["version"]))
                return 200, {"settlements": svc.list_settlements(plant, period)}

            if method == "GET" and seg == ["disputes"]:
                plant, period = _need(query, "plant_id", "period")
                return 200, svc.dispute_list(plant, period)

            return None

    return Handler


def create_server(service: CurtailmentService, host: str = "127.0.0.1", port: int = 0):
    return HTTPServer((host, port), make_handler(service))


def main(argv=None):
    parser = argparse.ArgumentParser(description="限发事件与电量归因服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=":memory:", help="SQLite 路径,默认内存库")
    args = parser.parse_args(argv)
    service = CurtailmentService(db=Database(args.db), clock=SystemClock())
    server = create_server(service, args.host, args.port)
    print(f"listening on http://{args.host}:{server.server_address[1]} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
