"""本地 HTTP 接口层。

仅依赖 Python 标准库；单线程 HTTP 服务器串行处理请求，与指挥中心的内存
状态配合无需加锁。值班员通过 ``X-Operator-Token`` 头完成授权。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import AuthorizationError, RosterAuthorizer
from .clock import Clock
from .identifiers import IdGenerator
from .service import CommandCenter, CommandError
from .store import EventStore

DEFAULT_DATA_DIR = os.environ.get("RC_DATA_DIR", os.path.join(os.getcwd(), "data"))


def build_center(
    data_dir: str | None = None,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
) -> CommandCenter:
    """构造带文件事件日志的指挥中心；已存在日志会被自动重放。"""
    directory = data_dir or DEFAULT_DATA_DIR
    os.makedirs(directory, exist_ok=True)
    store = EventStore(os.path.join(directory, "command_log.jsonl"))
    return CommandCenter(store=store, clock=clock, ids=ids)


class CommandHandler(BaseHTTPRequestHandler):
    center: CommandCenter = None  # type: ignore[assignment]
    authorizer = None

    server_version = "ResilienceCommand/1.0"

    # ------------------------------------------------------------ 基础工具
    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("RC_HTTP_QUIET"):
            return
        super().log_message(fmt, *args)

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(status, {"error": code, "message": message})

    def _read_json(self) -> dict[str, Any] | list[Any] | None:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"请求体不是合法 JSON：{exc}") from exc

    def _operator(self, action: str):
        token = self.headers.get("X-Operator-Token")
        return self.authorizer.require(token, action)

    def _optional_operator(self):
        token = self.headers.get("X-Operator-Token")
        if not token:
            return None
        return self.authorizer.resolve(token)

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/health":
                self._send(200, {"status": "ok"})
            elif path == "/situation":
                self._send(200, self.center.situation_view())
            elif path == "/plans":
                area = query.get("area", [None])[0]
                plans = self._list_plans(area)
                self._send(200, {"plans": plans})
            elif path.startswith("/plans/"):
                plan_id = path.split("/")[2]
                plan = self.center.plans.get(plan_id)
                if plan is None:
                    self._error(404, "not_found", f"方案 {plan_id} 不存在")
                else:
                    self._send(200, plan.to_dict())
            elif path == "/actions/pending":
                self._send(200, {"actions": self.center.pending_actions()})
            elif path == "/events":
                self._send(200, {"events": [r["event"] for r in self.center.event_log()]})
            elif path == "/decisions":
                self._send(200, {"decisions": self.center.decision_log()})
            else:
                self._error(404, "not_found", f"未知路径：{path}")
        except (CommandError, AuthorizationError) as exc:
            self._error(400, "bad_request", str(exc))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self._read_json()
            if path == "/events":
                if not isinstance(body, dict):
                    raise CommandError("/events 需要单个事件对象")
                event = self.center.ingest(body)
                self._send(201, {"event": event.to_dict()})
            elif path == "/events/batch":
                if not isinstance(body, dict) or not isinstance(body.get("events"), list):
                    raise CommandError("/events/batch 需要 {'events': [...]}")
                events = self.center.ingest_many(body["events"])
                self._send(201, {"events": [e.to_dict() for e in events]})
            elif path == "/replan":
                body = body if isinstance(body, dict) else {}
                operator = self._operator("replan")
                reason = str(body.get("reason", ""))
                plans = self.center.replan(operator=operator, reason=reason)
                self._send(201, {"proposals": [p.to_dict() for p in plans],
                                 "count": len(plans)})
            elif path.startswith("/plans/"):
                parts = path.split("/")
                # /plans/{id}/{action}
                if len(parts) == 4:
                    plan_id, action = parts[2], parts[3]
                    self._plan_action(plan_id, action, body if isinstance(body, dict) else {})
                else:
                    self._error(404, "not_found", f"未知路径：{path}")
            elif path.startswith("/actions/"):
                parts = path.split("/")
                if len(parts) == 4 and parts[3] == "complete":
                    operator = self._operator("report")
                    action = self.center.report_action_completed(
                        parts[2], operator, str((body or {}).get("result", "")))
                    self._send(200, {"action": action.to_dict()})
                else:
                    self._error(404, "not_found", f"未知路径：{path}")
            else:
                self._error(404, "not_found", f"未知路径：{path}")
        except AuthorizationError as exc:
            self._error(403, "forbidden", str(exc))
        except CommandError as exc:
            self._error(400, "bad_request", str(exc))

    def _plan_action(self, plan_id: str, action: str, body: dict[str, Any]) -> None:
        reason = str(body.get("reason", ""))
        if action == "approve":
            plan = self.center.approve(plan_id, self._operator("approve"), reason)
        elif action == "freeze":
            plan = self.center.freeze(plan_id, self._operator("freeze"), reason)
        elif action == "resume":
            plan = self.center.resume(plan_id, self._operator("resume"), reason)
        elif action == "withdraw":
            plan = self.center.withdraw(plan_id, self._operator("withdraw"), reason)
        elif action == "complete":
            plan = self.center.complete(plan_id, self._operator("complete"), reason)
        else:
            self._error(404, "not_found", f"未知方案操作：{action}")
            return
        self._send(200, {"plan": plan.to_dict()})

    def _list_plans(self, area: str | None) -> list[dict[str, Any]]:
        plans = list(self.center.plans.values())
        if area:
            plans = [p for p in plans if p.area == area]
        plans.sort(key=lambda p: (p.area, p.version, p.plan_id))
        return [p.to_dict() for p in plans]


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    data_dir: str | None = None,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
    authorizer=None,
) -> HTTPServer:
    center = build_center(data_dir=data_dir, clock=clock, ids=ids)

    handler = CommandHandler
    handler.center = center
    handler.authorizer = authorizer or RosterAuthorizer.default_local()
    server = HTTPServer((host, port), handler)
    return server


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="灾后通信恢复指挥服务 HTTP 接口")
    parser.add_argument("--host", default=os.environ.get("RC_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RC_PORT", "8080")))
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)

    server = create_server(host=args.host, port=args.port, data_dir=args.data_dir)
    print(f"指挥服务监听 http://{args.host}:{args.port}，数据目录 {args.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
