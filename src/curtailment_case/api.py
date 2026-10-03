"""HTTP/JSON 接口（仅依赖标准库）。

错误响应统一为 {"error": {"code": ..., "message": ...}}。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import CurtailmentService, NotFoundError, ServiceError, ValidationError


class Api:
    """路由表：把 HTTP 请求映射到 CurtailmentService。"""

    def __init__(self, service: CurtailmentService):
        self.svc = service
        self._routes = [
            ("GET", re.compile(r"^/health$"), self.health),
            ("POST", re.compile(r"^/grid-points$"), self.create_grid_point),
            ("POST", re.compile(r"^/plants$"), self.create_plant),
            ("POST", re.compile(r"^/instructions$"), self.record_instruction),
            ("POST", re.compile(r"^/available-power$"), self.ingest_available_power),
            ("POST", re.compile(r"^/metered-energy$"), self.ingest_metered_energy),
            ("POST", re.compile(r"^/faults$"), self.record_fault),
            ("POST", re.compile(r"^/network-constraints$"), self.record_network_constraint),
            ("GET", re.compile(r"^/curtailment-events$"), self.curtailment_events),
            ("GET", re.compile(r"^/dispatch-instructions$"), self.dispatch_instructions),
            ("POST", re.compile(r"^/cases$"), self.open_case),
            ("GET", re.compile(r"^/cases$"), self.list_cases),
            ("GET", re.compile(r"^/cases/(?P<case_id>[^/]+)$"), self.get_case),
            ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/appeal$"), self.file_appeal),
            ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/review$"), self.review_case),
            ("POST", re.compile(r"^/settlements/prepare$"), self.prepare_settlement),
            ("POST", re.compile(r"^/settlements/(?P<settlement_id>[^/]+)/confirm$"), self.confirm_settlement),
            ("GET", re.compile(r"^/settlements/(?P<settlement_id>[^/]+)$"), self.get_settlement),
            ("GET", re.compile(r"^/settlements$"), self.list_settlements),
            ("GET", re.compile(r"^/disputes$"), self.disputes),
        ]

    def handle(self, method: str, raw_path: str, body: dict):
        parsed = urlparse(raw_path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        for verb, pattern, handler in self._routes:
            if verb != method:
                continue
            match = pattern.match(parsed.path)
            if match:
                return handler(body, query, **match.groupdict())
        raise NotFoundError("NOT_FOUND", f"路由不存在: {method} {parsed.path}")

    # ------------------------------------------------------------ 各端点

    def health(self, body, query):
        return 200, {"status": "ok"}

    def create_grid_point(self, body, query):
        return 201, self.svc.create_grid_point(body.get("grid_point_id", ""), body.get("name", ""))

    def create_plant(self, body, query):
        return 201, self.svc.create_plant(
            body.get("plant_id", ""),
            body.get("grid_point_id", ""),
            body.get("name", ""),
            body.get("capacity_mw"),
        )

    def record_instruction(self, body, query):
        return 201, self.svc.record_instruction(
            instruction_id=body.get("instruction_id", ""),
            plant_id=body.get("plant_id", ""),
            event_type=body.get("event_type", ""),
            starts_at=body.get("starts_at", ""),
            ends_at=body.get("ends_at", ""),
            cap_mw=body.get("cap_mw"),
            issued_at=body.get("issued_at"),
            reason=body.get("reason", ""),
            actor=body.get("actor", ""),
        )

    def ingest_available_power(self, body, query):
        records = body.get("records", body)
        return 201, self.svc.ingest_available_power(body.get("plant_id", ""), records)

    def ingest_metered_energy(self, body, query):
        records = body.get("records", body)
        return 201, self.svc.ingest_metered_energy(body.get("plant_id", ""), records)

    def record_fault(self, body, query):
        return 201, self.svc.record_fault(
            body.get("plant_id", ""),
            body.get("starts_at", ""),
            body.get("ends_at", ""),
            body.get("derated_mw", -1),
            body.get("description", ""),
        )

    def record_network_constraint(self, body, query):
        return 201, self.svc.record_network_constraint(
            body.get("grid_point_id", ""),
            body.get("starts_at", ""),
            body.get("ends_at", ""),
            body.get("limit_mw", -1),
            body.get("description", ""),
        )

    def curtailment_events(self, body, query):
        return 200, self.svc.compute_events(
            _required(query, "plant_id"),
            _required(query, "start"),
            _required(query, "end"),
            query.get("as_of"),
        )

    def dispatch_instructions(self, body, query):
        return 200, {
            "instructions": self.svc.effective_instructions(
                _required(query, "plant_id"), query.get("as_of")
            )
        }

    def open_case(self, body, query):
        return 201, self.svc.open_case(
            body.get("plant_id", ""),
            body.get("starts_at", ""),
            body.get("ends_at", ""),
            body.get("attribution", ""),
            body.get("reason", ""),
        )

    def list_cases(self, body, query):
        return 200, {"cases": self.svc.list_cases(query.get("plant_id"), query.get("period"))}

    def get_case(self, body, query, case_id):
        return 200, self.svc.get_case(case_id)

    def file_appeal(self, body, query, case_id):
        return 200, self.svc.file_appeal(case_id, body.get("appeal_reason", ""))

    def review_case(self, body, query, case_id):
        return 200, self.svc.review_case(
            case_id,
            body.get("outcome", ""),
            body.get("adjusted_attribution"),
            body.get("adjusted_energy_mwh"),
            body.get("note", ""),
        )

    def prepare_settlement(self, body, query):
        return 201, self.svc.prepare_settlement(body.get("plant_id", ""), body.get("period", ""))

    def confirm_settlement(self, body, query, settlement_id):
        return 200, self.svc.confirm_settlement(settlement_id)

    def get_settlement(self, body, query, settlement_id):
        return 200, self.svc.get_settlement(settlement_id)

    def list_settlements(self, body, query):
        return 200, {
            "settlements": self.svc.list_settlements(
                _required(query, "plant_id"), query.get("period")
            )
        }

    def disputes(self, body, query):
        return 200, self.svc.dispute_list(
            _required(query, "plant_id"), _required(query, "period")
        )


def _required(query: dict, name: str) -> str:
    value = query.get(name)
    if not value:
        raise ValidationError("MISSING_PARAM", f"缺少查询参数: {name}")
    return value


def make_server(service: CurtailmentService, host: str = "127.0.0.1", port: int = 8080):
    api = Api(service)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静默访问日志
            pass

        def _handle(self, method: str):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise ValidationError("INVALID_JSON", "请求体不是合法 JSON")
                    if not isinstance(body, dict):
                        raise ValidationError("INVALID_JSON", "请求体必须是 JSON 对象")
                else:
                    body = {}
                status, payload = api.handle(method, self.path, body)
            except ServiceError as exc:
                status, payload = exc.status, {
                    "error": {"code": exc.code, "message": exc.message}
                }
            except ValueError as exc:
                status, payload = 400, {
                    "error": {"code": "INVALID_VALUE", "message": str(exc)}
                }
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                status, payload = 500, {
                    "error": {"code": "INTERNAL", "message": f"内部错误: {exc}"}
                }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
