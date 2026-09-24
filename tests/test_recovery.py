"""重启恢复：事件日志重放后状态与未完成行动一致，编号连续。"""

import os
import tempfile
import unittest

from tests.helpers import (
    DUTY_TOKEN,
    T0,
    make_service,
    reopen_service,
    sat_window,
    station,
    team,
)

END = "2026-09-24T12:00:00Z"


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "eventlog.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build_history(self):
        service, _, _ = make_service(db_path=self.db)
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        service.ingest_report(team("T1", "TEAM-1", 1, 1, T0))
        service.ingest_report(station("E1", "BS-1", "A-H", "医院", T0))
        service.ingest_report(station("E2", "BS-2", "A-O", "普通区域", T0))
        service.freeze_plan("PLAN-0001", DUTY_TOKEN)
        return service

    def test_state_is_restored_after_restart(self) -> None:
        before = self._build_history()
        expected = before.snapshot()
        recovered = reopen_service(self.db)
        actual = recovered.snapshot()
        self.assertEqual(actual, expected)  # 同一时钟下快照逐字段一致
        self.assertEqual(actual["counts"]["decisions"], expected["counts"]["decisions"])

    def test_unfinished_actions_survive_restart(self) -> None:
        self._build_history()
        recovered = reopen_service(self.db)
        unfinished = recovered.snapshot()["unfinished_actions"]
        kinds = sorted(a["kind"] for a in unfinished)
        self.assertIn("SATELLITE", kinds)
        self.assertIn("REPAIR_TEAM", kinds)
        # 冻结的医院方案与自动的普通区域方案都处于未完成状态
        plans = {p["plan_id"]: p["status"] for p in recovered.list_plans()}
        self.assertEqual(plans["PLAN-0001"], "FROZEN")
        self.assertEqual(plans["PLAN-0002"], "ACTIVE")

    def test_ids_continue_after_restart(self) -> None:
        self._build_history()
        recovered = reopen_service(self.db)
        result = recovered.ingest_report(station("E3", "BS-3", "A-S", "避难点", T0))
        new_ids = [d["plan_id"] for d in result["decisions"] if d["plan_id"]]
        self.assertTrue(new_ids)
        for plan_id in new_ids:
            self.assertNotIn(plan_id, {"PLAN-0001", "PLAN-0002"})
        # 决策编号同样连续
        decision_ids = [d["decision_id"] for d in recovered.list_decisions()]
        self.assertEqual(len(decision_ids), len(set(decision_ids)))

    def test_audit_trail_survives_restart(self) -> None:
        self._build_history()
        recovered = reopen_service(self.db)
        decisions = recovered.list_decisions(plan_id="PLAN-0001")
        kinds = [d["kind"] for d in decisions]
        self.assertIn("REPLAN", kinds)
        self.assertIn("FREEZE", kinds)
        freeze = next(d for d in decisions if d["kind"] == "FREEZE")
        self.assertEqual(freeze["operator"], "林值班")
        self.assertTrue(freeze["input_versions"])


if __name__ == "__main__":
    unittest.main()
