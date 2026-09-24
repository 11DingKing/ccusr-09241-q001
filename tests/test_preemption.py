"""有约束的抢占与重排：高优先级到达、链路退化、冻结保护。"""

import unittest

from tests.helpers import DUTY_TOKEN, T0, depot, make_service, sat_window, station

END = "2026-09-24T12:00:00Z"


class PreemptionTests(unittest.TestCase):
    def test_hospital_preempts_ordinary_area(self) -> None:
        service, _, _ = make_service()
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        service.ingest_report(station("E1", "BS-1", "A-O", "普通区域", T0))
        result = service.ingest_report(station("E2", "BS-2", "A-H", "医院", T0))
        by_area = {d["area_id"]: d for d in result["decisions"]}
        self.assertEqual(by_area["A-H"]["reason"], "NEW_DEMAND")
        self.assertEqual(by_area["A-O"]["reason"], "PREEMPTED")
        self.assertIn("R-PREEMPT-CONSTRAINED", by_area["A-O"]["rules_hit"])
        ordinary = service.get_plan(by_area["A-O"]["plan_id"])
        self.assertEqual(ordinary["unmet_mbps"], 20)
        self.assertEqual(ordinary["actions"], [])

    def test_frozen_plan_is_never_preempted(self) -> None:
        service, _, _ = make_service()
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        service.ingest_report(station("E1", "BS-1", "A-O", "普通区域", T0))
        service.freeze_plan("PLAN-0001", DUTY_TOKEN)  # 冻结普通区域的 20Mbps
        result = service.ingest_report(station("E2", "BS-2", "A-H", "医院", T0))
        by_area = {d["area_id"]: d for d in result["decisions"]}
        # 医院只能拿到冻结之外的 80Mbps，缺口 20
        hospital = service.get_plan(by_area["A-H"]["plan_id"])
        self.assertEqual(hospital["unmet_mbps"], 20)
        # 冻结方案原样保留
        frozen = service.get_plan("PLAN-0001")
        self.assertEqual(frozen["status"], "FROZEN")
        self.assertEqual(frozen["actions"][0]["capacity_mbps"], 20)
        self.assertNotIn("A-O", by_area)

    def test_equal_priority_does_not_preempt(self) -> None:
        service, _, _ = make_service()
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        service.ingest_report(station("E1", "BS-1", "A-H1", "医院", T0))
        result = service.ingest_report(station("E2", "BS-2", "A-H2", "医院", T0))
        by_area = {d["area_id"]: d for d in result["decisions"]}
        # 第二个医院只能拿剩余 0，先到先得，不发生抢占
        self.assertNotIn("A-H1", by_area)
        second = service.get_plan(by_area["A-H2"]["plan_id"])
        self.assertEqual(second["unmet_mbps"], 100)

    def test_link_degradation_triggers_reschedule(self) -> None:
        service, _, _ = make_service()
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        service.ingest_report(depot("D1", "DEPOT-1", 1, 40, T0))
        service.ingest_report(station("E1", "BS-1", "A-H", "医院", T0))
        result = service.ingest_report(
            sat_window("W2", "SAT-1", T0, END, 40, "2026-09-24T08:05:00Z")
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["reason"], "RESOURCE_DEGRADED")
        plan = service.get_plan(decision["plan_id"])
        # 卫星 40 + 便携站 40，仍缺 20
        self.assertEqual(plan["unmet_mbps"], 20)
        kinds = sorted(a["kind"] for a in plan["actions"])
        self.assertEqual(kinds, ["PORTABLE", "SATELLITE"])

    def test_window_expiry_releases_capacity(self) -> None:
        service, clock, _ = make_service()
        service.ingest_report(
            sat_window("W1", "SAT-1", T0, "2026-09-24T09:00:00Z", 100, T0)
        )
        service.ingest_report(station("E1", "BS-1", "A-H", "医院", T0))
        clock.set("2026-09-24T09:30:00Z")  # 窗口已过期
        result = service.reevaluate()
        decision = result["decisions"][0]
        self.assertEqual(decision["reason"], "RESOURCE_DEGRADED")
        plan = service.get_plan(decision["plan_id"])
        self.assertEqual(plan["unmet_mbps"], 100)


if __name__ == "__main__":
    unittest.main()
