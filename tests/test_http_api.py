"""本地 HTTP 接口：上报、查询、授权操作与错误映射。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from resilience_command.interfaces.http_api import create_server
from tests.helpers import DUTY_TOKEN, T0, make_service, sat_window, station

END = "2026-09-24T12:00:00Z"


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, _, _ = make_service()
        self.server = create_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _call(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_health(self) -> None:
        status, body = self._call("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_ingest_query_and_audit_flow(self) -> None:
        status, body = self._call(
            "POST", "/v1/reports", sat_window("W1", "SAT-1", T0, END, 100, T0)
        )
        self.assertEqual(status, 201)
        self.assertTrue(body["accepted"])
        status, body = self._call(
            "POST", "/v1/reports", station("E1", "BS-1", "A-H", "医院", T0)
        )
        self.assertEqual(status, 201)
        self.assertEqual(len(body["decisions"]), 1)
        plan_id = body["decisions"][0]["plan_id"]

        status, body = self._call("GET", f"/v1/plans/{plan_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["area_id"], "A-H")
        self.assertEqual(body["status"], "ACTIVE")

        status, body = self._call("GET", "/v1/plans?status=ACTIVE")
        self.assertEqual(status, 200)
        self.assertTrue(any(p["plan_id"] == plan_id for p in body["plans"]))

        status, body = self._call("GET", f"/v1/decisions?plan_id={plan_id}")
        self.assertEqual(status, 200)
        self.assertTrue(body["decisions"])
        self.assertEqual(body["decisions"][0]["operator"], "system")

        status, body = self._call("GET", "/v1/state")
        self.assertEqual(status, 200)
        self.assertTrue(body["unfinished_actions"])

    def test_duplicate_report_flagged(self) -> None:
        report = station("E2", "BS-2", "A-O", "普通区域", T0)
        self._call("POST", "/v1/reports", report)
        status, body = self._call("POST", "/v1/reports", report)
        self.assertEqual(status, 201)
        self.assertTrue(body["duplicate"])

    def test_batch_ingest(self) -> None:
        status, body = self._call(
            "POST",
            "/v1/reports",
            {
                "reports": [
                    sat_window("W3", "SAT-3", T0, END, 50, T0),
                    station("E3", "BS-3", "A-S", "避难点", T0),
                ]
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 2)
        self.assertTrue(all(r["accepted"] for r in body["results"]))

    def test_operator_action_authorization(self) -> None:
        self._call("POST", "/v1/reports", sat_window("W1", "SAT-1", T0, END, 100, T0))
        status, body = self._call(
            "POST", "/v1/reports", station("E4", "BS-4", "A-H2", "医院", T0)
        )
        plan_id = body["decisions"][0]["plan_id"]
        status, body = self._call("POST", f"/v1/plans/{plan_id}/freeze", {"token": "bad"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "BAD_TOKEN")
        status, body = self._call(
            "POST", f"/v1/plans/{plan_id}/freeze", {"token": DUTY_TOKEN}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["kind"], "FREEZE")
        status, body = self._call(
            "POST", f"/v1/plans/{plan_id}/resume", {"token": DUTY_TOKEN}
        )
        self.assertEqual(status, 200)
        status, body = self._call(
            "POST", f"/v1/plans/{plan_id}/withdraw", {"token": DUTY_TOKEN}
        )
        self.assertEqual(status, 200)
        status, body = self._call(
            "POST", "/v1/areas/A-H2/resume", {"token": DUTY_TOKEN}
        )
        self.assertEqual(status, 200)

    def test_error_mapping(self) -> None:
        status, body = self._call("GET", "/v1/plans/PLAN-9999")
        self.assertEqual(status, 404)
        status, body = self._call("GET", "/v1/nope")
        self.assertEqual(status, 404)
        status, body = self._call("POST", "/v1/reports", {"event_id": "X"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "MISSING_FIELD")


if __name__ == "__main__":
    unittest.main()
