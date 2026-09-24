"""命令行入口：演练、重放与参数解析。"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from resilience_command.interfaces.cli import main

SCENARIO = (
    Path(__file__).resolve().parent.parent / "scenarios" / "typhoon_landing.json"
)


class CliTests(unittest.TestCase):
    def test_drill_via_main_entry(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["drill", str(SCENARIO)])
        self.assertEqual(code, 0)
        self.assertIn("台风过境通信恢复演练", out.getvalue())

    def test_drill_then_replay_via_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "eventlog.jsonl")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["drill", str(SCENARIO), "--db", db]), 0)
            self.assertTrue(os.path.exists(db))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["replay", "--db", db]), 0)
            text = out.getvalue()
            self.assertIn("重放", text)
            self.assertIn("未完成行动", text)
            self.assertIn("PLAN-", text)

    def test_drill_rejects_unknown_step_gracefully(self) -> None:
        scenario = {
            "name": "空场景",
            "start": "2026-09-24T08:00:00Z",
            "steps": [{"note": "只有旁白"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scenario.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(scenario, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["drill", path]), 0)
            self.assertIn("空场景", out.getvalue())

    def test_operator_rejection_is_reported_not_fatal(self) -> None:
        scenario = {
            "name": "越权场景",
            "start": "2026-09-24T08:00:00Z",
            "tokens": {},
            "steps": [
                {"at": "2026-09-24T08:01:00Z",
                 "freeze": {"plan_id": "PLAN-0001", "token": "nobody"}}
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scenario.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(scenario, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["drill", path]), 0)
            self.assertIn("拒绝", out.getvalue())
            self.assertIn("BAD_TOKEN", out.getvalue())


if __name__ == "__main__":
    unittest.main()
