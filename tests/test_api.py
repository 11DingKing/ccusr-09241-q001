"""HTTP 接口测试：真实 socket 往返、授权、错误码、跨实例重启恢复。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer

from resilience_command.api import CommandHandler
from resilience_command.auth import RosterAuthorizer
from resilience_command.cli import typhoon_events
from resilience_command.clock import FixedClock
from resilience_command.identifiers import DeterministicIds
from resilience_command.service import CommandCenter
from resilience_command.store import EventStore

DIRECTOR_TOKEN = "director-token"
OPERATOR_TOKEN = "operator-token"


class ServerHarness:
    def __init__(self, data_dir: str, clock=None):
        self.clock = clock or FixedClock("2026-09-24T10:00:00Z")
        store = EventStore(os.path.join(data_dir, "command_log.jsonl"))
        self.center = CommandCenter(store=store, clock=self.clock, ids=DeterministicIds())
        CommandHandler.center = self.center
        CommandHandler.authorizer = RosterAuthorizer.default_local()
        self.server = HTTPServer(("127.0.0.1", 0), CommandHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method: str, path: str, body=None, token: str | None = None):
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if token:
            headers["X-Operator-Token"] = token
        req = urllib.request.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.harness = ServerHarness(self._tmp.name)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.harness.stop()
        self._tmp.cleanup()

    def test_health(self) -> None:
        status, body = self.harness.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_ingest_replan_approve_flow(self) -> None:
        # 批量接入首批事件
        status, body = self.harness.request("POST", "/events/batch",
                                            {"events": typhoon_events()[:9]})
        self.assertEqual(status, 201)
        self.assertEqual(len(body["events"]), 9)

        # 无令牌不能重排
        status, _ = self.harness.request("POST", "/replan", {"reason": "首轮"})
        self.assertEqual(status, 403)
        # operator 可以重排
        status, body = self.harness.request("POST", "/replan", {"reason": "首轮"},
                                            token=OPERATOR_TOKEN)
        self.assertEqual(status, 201)
        proposals = body["proposals"]
        self.assertGreaterEqual(len(proposals), 2)

        # operator 不能授权
        plan_id = proposals[0]["plan_id"]
        status, body = self.harness.request("POST", f"/plans/{plan_id}/approve",
                                            {"reason": "试"}, token=OPERATOR_TOKEN)
        self.assertEqual(status, 403)
        # director 授权
        status, body = self.harness.request("POST", f"/plans/{plan_id}/approve",
                                            {"reason": "值班长授权"}, token=DIRECTOR_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body["plan"]["state"], "active")

        # 态势查询
        status, body = self.harness.request("GET", "/situation")
        self.assertEqual(status, 200)
        self.assertIn("areas", body)
        self.assertIn("resources", body)

        # 待办行动查询
        status, body = self.harness.request("GET", "/actions/pending")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["actions"]), 2)

    def test_bad_event_returns_400(self) -> None:
        status, body = self.harness.request("POST", "/events", {"type": "unknown",
                                                                "occurred_at": "2026-09-24T10:00:00Z",
                                                                "payload": {}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_request")

    def test_illegal_lifecycle_transition_returns_400(self) -> None:
        self.harness.request("POST", "/events/batch", {"events": typhoon_events()[:9]})
        _, body = self.harness.request("POST", "/replan", {"reason": "x"}, token=DIRECTOR_TOKEN)
        plan_id = body["proposals"][0]["plan_id"]
        # 未批准直接冻结
        status, body = self.harness.request("POST", f"/plans/{plan_id}/freeze",
                                            {"reason": "x"}, token=DIRECTOR_TOKEN)
        self.assertEqual(status, 400)

    def test_decisions_audit_trail(self) -> None:
        self.harness.request("POST", "/events/batch", {"events": typhoon_events()[:9]})
        self.harness.request("POST", "/replan", {"reason": "首轮"}, token=OPERATOR_TOKEN)
        status, body = self.harness.request("GET", "/decisions")
        self.assertEqual(status, 200)
        self.assertTrue(any(d["type"] == "replan" for d in body["decisions"]))
        replan = next(d for d in body["decisions"] if d["type"] == "replan")
        self.assertIn("basis_event_ids", replan)
        self.assertIn("rule_hits", replan)
        self.assertEqual(replan["operator"], "王值班员")

    def test_state_survives_server_restart_on_same_log(self) -> None:
        self.harness.request("POST", "/events/batch", {"events": typhoon_events()[:9]})
        self.harness.request("POST", "/replan", {"reason": "首轮"}, token=OPERATOR_TOKEN)
        _, body = self.harness.request("GET", "/plans")
        first_versions = [(p["area"], p["version"], p["state"]) for p in body["plans"]]

        # 用同一数据目录新建第二个服务器实例（模拟重启）
        self.harness.stop()
        harness2 = ServerHarness(self._tmp.name)
        self.addCleanup(harness2.stop)
        status, body = harness2.request("GET", "/plans")
        self.assertEqual(status, 200)
        second_versions = [(p["area"], p["version"], p["state"]) for p in body["plans"]]
        self.assertEqual(first_versions, second_versions)


if __name__ == "__main__":
    unittest.main()
