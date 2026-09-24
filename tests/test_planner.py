"""规划器规则：优先级、冻结锁定、窗口期、抢修队调度。"""

import unittest

from resilience_command.domain.models import (
    ALLOC_PORTABLE,
    ALLOC_REPAIR,
    ALLOC_SATELLITE,
    Allocation,
    build_facts,
    build_registry,
    parse_time,
    report_from_dict,
)
from resilience_command.domain.planner import derive_demands, plan_all
from resilience_command.domain.rules import RuleConfig
from tests.helpers import T0, depot, fiber, sat_window, station, team

NOW = parse_time(T0)
END = "2026-09-24T12:00:00Z"


def facts_and_registry(*report_dicts):
    reports = [report_from_dict(d) for d in report_dicts]
    return build_facts(reports), build_registry(reports)


class DemandTests(unittest.TestCase):
    def test_requirement_by_category(self) -> None:
        facts, registry = facts_and_registry(
            station("E1", "BS-1", "A-H", "医院", T0),
            station("E2", "BS-2", "A-S", "避难点", T0),
            station("E3", "BS-3", "A-O", "普通区域", T0),
        )
        demands = derive_demands(facts, registry, RuleConfig())
        self.assertEqual(demands["A-H"].required_mbps, 100)
        self.assertEqual(demands["A-S"].required_mbps, 50)
        self.assertEqual(demands["A-O"].required_mbps, 20)

    def test_restored_station_yields_no_demand(self) -> None:
        facts, registry = facts_and_registry(
            station("E1", "BS-1", "A-H", "医院", T0),
            station("E2", "BS-1", "A-H", "医院", "2026-09-24T09:00:00Z", status="RESTORED"),
        )
        self.assertEqual(derive_demands(facts, registry, RuleConfig()), {})


class AllocationTests(unittest.TestCase):
    def test_hospital_served_before_ordinary_under_scarcity(self) -> None:
        facts, registry = facts_and_registry(
            sat_window("W1", "SAT-1", T0, END, 100, T0),
            station("E1", "BS-1", "A-O", "普通区域", T0),
            station("E2", "BS-2", "A-H", "医院", T0),
        )
        plans = plan_all(facts, registry, (), frozenset(), NOW, RuleConfig())
        hosp = [a for a in plans["A-H"].allocations if a.kind == ALLOC_SATELLITE]
        self.assertEqual(sum(a.capacity_mbps for a in hosp), 100)
        self.assertEqual(plans["A-O"].allocations, ())
        self.assertEqual(plans["A-O"].unmet_mbps, 20)

    def test_expired_window_is_not_allocated(self) -> None:
        facts, registry = facts_and_registry(
            sat_window("W1", "SAT-1", "2026-09-24T01:00:00Z", "2026-09-24T07:00:00Z", 100, T0),
            station("E1", "BS-1", "A-H", "医院", T0),
        )
        plans = plan_all(facts, registry, (), frozenset(), NOW, RuleConfig())
        self.assertEqual(plans["A-H"].allocations, ())
        self.assertEqual(plans["A-H"].unmet_mbps, 100)

    def test_pinned_allocation_reserves_capacity(self) -> None:
        facts, registry = facts_and_registry(
            sat_window("W1", "SAT-1", T0, END, 100, T0),
            station("E1", "BS-1", "A-H", "医院", T0),
            station("E2", "BS-2", "A-O", "普通区域", T0),
        )
        pinned = (
            Allocation("A-O", ALLOC_SATELLITE, "SAT-1", 20, window_start=NOW, window_end=parse_time(END)),
        )
        plans = plan_all(facts, registry, pinned, frozenset(), NOW, RuleConfig())
        hosp_sat = [a for a in plans["A-H"].allocations if a.kind == ALLOC_SATELLITE]
        self.assertEqual(sum(a.capacity_mbps for a in hosp_sat), 80)
        self.assertEqual(plans["A-H"].unmet_mbps, 20)

    def test_portable_stations_fill_residual(self) -> None:
        facts, registry = facts_and_registry(
            depot("D1", "DEPOT-1", 2, 40, T0),
            station("E1", "BS-1", "A-H", "医院", T0),
        )
        plans = plan_all(facts, registry, (), frozenset(), NOW, RuleConfig())
        portable = [a for a in plans["A-H"].allocations if a.kind == ALLOC_PORTABLE]
        self.assertEqual(len(portable), 2)  # 40 + 40，覆盖 100 中的 80
        self.assertEqual(plans["A-H"].unmet_mbps, 20)

    def test_nearest_team_is_dispatched(self) -> None:
        facts, registry = facts_and_registry(
            team("T1", "TEAM-FAR", 100, 100, T0),
            team("T2", "TEAM-NEAR", 1, 1, T0),
            fiber("F1", "FO-1", "A-H", "医院", T0, x=0, y=0),
        )
        plans = plan_all(facts, registry, (), frozenset(), NOW, RuleConfig())
        repair = [a for a in plans["A-H"].allocations if a.kind == ALLOC_REPAIR]
        self.assertEqual(len(repair), 1)
        self.assertEqual(repair[0].resource_ref, "TEAM-NEAR")

    def test_held_area_is_excluded(self) -> None:
        facts, registry = facts_and_registry(
            sat_window("W1", "SAT-1", T0, END, 100, T0),
            station("E1", "BS-1", "A-H", "医院", T0),
        )
        plans = plan_all(facts, registry, (), frozenset({"A-H"}), NOW, RuleConfig())
        self.assertEqual(plans, {})


if __name__ == "__main__":
    unittest.main()
