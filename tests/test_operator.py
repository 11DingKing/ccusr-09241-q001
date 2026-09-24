"""值班员操作：授权、冻结、撤回、接续。"""

import unittest

from resilience_command.domain.exceptions import (
    ConflictError,
    ForbiddenError,
    UnauthorizedError,
)
from tests.helpers import DUTY_TOKEN, T0, make_service, sat_window, station

END = "2026-09-24T12:00:00Z"


def service_with_plan():
    service, _, _ = make_service()
    service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
    service.ingest_report(station("E1", "BS-1", "A-H", "医院", T0))
    return service


class AuthorizationTests(unittest.TestCase):
    def test_bad_token_rejected(self) -> None:
        service = service_with_plan()
        with self.assertRaises(UnauthorizedError):
            service.freeze_plan("PLAN-0001", "wrong-token")

    def test_role_without_permission_rejected(self) -> None:
        service, _, _ = make_service(tokens={"tok": {"name": "观察员", "role": "viewer"}})
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        service.ingest_report(station("E1", "BS-1", "A-H", "医院", T0))
        with self.assertRaises(ForbiddenError):
            service.freeze_plan("PLAN-0001", "tok")

    def test_operator_name_is_recorded(self) -> None:
        service = service_with_plan()
        result = service.freeze_plan("PLAN-0001", DUTY_TOKEN)
        self.assertEqual(result["decision"]["operator"], "林值班")
        self.assertEqual(result["decision"]["kind"], "FREEZE")
        self.assertIn("R-FROZEN-PIN", result["decision"]["rules_hit"])


class FreezeTests(unittest.TestCase):
    def test_freeze_then_resume_plan(self) -> None:
        service = service_with_plan()
        service.freeze_plan("PLAN-0001", DUTY_TOKEN)
        self.assertEqual(service.get_plan("PLAN-0001")["status"], "FROZEN")
        with self.assertRaises(ConflictError):
            service.freeze_plan("PLAN-0001", DUTY_TOKEN)  # 重复冻结
        service.resume_plan("PLAN-0001", DUTY_TOKEN)
        self.assertEqual(service.get_plan("PLAN-0001")["status"], "ACTIVE")

    def test_freeze_unknown_plan(self) -> None:
        service = service_with_plan()
        with self.assertRaises(Exception) as ctx:
            service.freeze_plan("PLAN-9999", DUTY_TOKEN)
        self.assertEqual(getattr(ctx.exception, "http_status", None), 404)


class WithdrawResumeTests(unittest.TestCase):
    def test_withdraw_holds_area_and_resume_recovers(self) -> None:
        service = service_with_plan()
        result = service.withdraw_plan("PLAN-0001", DUTY_TOKEN, "改人工调度")
        self.assertEqual(result["decision"]["kind"], "WITHDRAW")
        self.assertEqual(service.get_plan("PLAN-0001")["status"], "WITHDRAWN")
        area = next(a for a in service.snapshot()["areas"] if a["area_id"] == "A-H")
        self.assertTrue(area["on_hold"])
        # 挂起期间即使资源到位也不自动编排
        service.ingest_report(sat_window("W2", "SAT-2", T0, END, 200, "2026-09-24T08:05:00Z"))
        plans = [p for p in service.list_plans(area_id="A-H") if p["status"] == "ACTIVE"]
        self.assertEqual(plans, [])
        # 接续后恢复自动编排
        resumed = service.resume_area("A-H", DUTY_TOKEN)
        followups = resumed["followups"]
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0]["reason"], "RESUMED")
        self.assertIn("R-AREA-RESUME", followups[0]["rules_hit"])
        self.assertFalse(
            next(a for a in service.snapshot()["areas"] if a["area_id"] == "A-H")["on_hold"]
        )

    def test_withdraw_terminal_plan_rejected(self) -> None:
        service = service_with_plan()
        service.withdraw_plan("PLAN-0001", DUTY_TOKEN)
        with self.assertRaises(ConflictError):
            service.withdraw_plan("PLAN-0001", DUTY_TOKEN)

    def test_resume_area_not_on_hold_rejected(self) -> None:
        service = service_with_plan()
        with self.assertRaises(ConflictError):
            service.resume_area("A-H", DUTY_TOKEN)


if __name__ == "__main__":
    unittest.main()
