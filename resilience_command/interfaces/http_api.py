"""本地 HTTP 接口（仅依赖标准库）。

路由一览：
    GET  /v1/health                     健康检查
    POST /v1/reports                    接收上报（单条或 {"reports": [...]} 批量）
    GET  /v1/state                      值班大屏快照
    GET  /v1/plans[?status=&area_id=]   方案列表
    GET  /v1/plans/{id}                 方案详情
    POST /v1/plans/{id}/freeze          冻结方案   {"token": ...}
    POST /v1/plans/{id}/withdraw        撤回方案   {"token": ..., "reason": ...}
    POST /v1/plans/{id}/resume          接续方案   {"token": ...}
    POST /v1/areas/{id}/resume           接续区域   {"token": ...}
    POST /v1/reevaluate                 按时钟重估 {"token": ...}
    GET  /v1/decisions[?plan_id=&area_id=]  决策审计
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..application.service import CommandService
from ..domain.exceptions import DomainError, ValidationError

Handler = Callable[[dict[str, str]], tuple[int, Any]]


def _json_body(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("INVALID_JSON", f"请求体不是合法 JSON: {exc}") from exc


def make_handler(service: CommandService) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "ResilienceCommand/1.0"
        protocol_version = "HTTP/1.1"

        # -- 基础工具 -------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
            return

        def _send(self, status: int, obj: Any) -> None:
            body = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _query(self) -> dict[str, str]:
            parsed = urlparse(self.path)
            return {k: v[0] for k, v in parse_qs(parsed.query).items()}

        def _path(self) -> str:
            return urlparse(self.path).path.rstrip("/") or "/"

        # -- 分发 -----------------------------------------------------

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                status, obj = self._route(method)
            except DomainError as err:
                status, obj = err.http_status, {
                    "error": {"code": err.code, "message": err.message}
                }
            except Exception as err:  # pragma: no cover - 兜底
                status, obj = 500, {
                    "error": {"code": "INTERNAL", "message": f"内部错误: {err}"}
                }
            self._send(status, obj)

        def _route(self, method: str) -> tuple[int, Any]:
            path = self._path()
            query = self._query()

            if method == "GET" and path == "/v1/health":
                return 200, {"status": "ok", "time": service.snapshot()["time"]}
            if method == "GET" and path == "/v1/state":
                return 200, service.snapshot()
            if method == "GET" and path == "/v1/plans":
                return 200, {
                    "plans": service.list_plans(
                        status=query.get("status"), area_id=query.get("area_id")
                    )
                }
            if method == "GET" and path == "/v1/decisions":
                return 200, {
                    "decisions": service.list_decisions(
                        plan_id=query.get("plan_id"), area_id=query.get("area_id")
                    )
                }

            match = re.fullmatch(r"/v1/plans/([A-Za-z0-9-]+)", path)
            if method == "GET" and match:
                return 200, service.get_plan(match.group(1))

            if method == "POST" and path == "/v1/reports":
                body = _json_body(self)
                if isinstance(body, dict) and "reports" in body:
                    reports = body["reports"]
                    if not isinstance(reports, list):
                        raise ValidationError("INVALID_BATCH", "reports 应为数组")
                    return 200, {
                        "results": [service.ingest_report(item) for item in reports]
                    }
                return 201, service.ingest_report(body)

            if method == "POST" and path == "/v1/reevaluate":
                body = _json_body(self)
                return 200, service.reevaluate(token=body.get("token"))

            match = re.fullmatch(r"/v1/plans/([A-Za-z0-9-]+)/(freeze|withdraw|resume)", path)
            if method == "POST" and match:
                plan_id, action = match.group(1), match.group(2)
                body = _json_body(self)
                token = str(body.get("token", ""))
                if action == "freeze":
                    return 200, service.freeze_plan(plan_id, token)
                if action == "withdraw":
                    return 200, service.withdraw_plan(plan_id, token, body.get("reason"))
                return 200, service.resume_plan(plan_id, token)

            match = re.fullmatch(r"/v1/areas/([A-Za-z0-9-]+)/resume", path)
            if method == "POST" and match:
                body = _json_body(self)
                return 200, service.resume_area(match.group(1), str(body.get("token", "")))

            raise ValidationError("NOT_FOUND", f"未知路由: {method} {path}", http_status=404)

    return ApiHandler


def create_server(
    service: CommandService, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    """创建本地 HTTP 服务实例（port=0 时由系统分配端口）。"""
    return ThreadingHTTPServer((host, port), make_handler(service))
