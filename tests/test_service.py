"""上报接收：幂等、迟到、乱序与决策审计字段。"""

import unittest

from resilience_command.domain.exceptions import ValidationError
from tests.helpers import T0, depot, make_service, sat_window, station

END = "2026-09-24T12:00:00Z"


class IngestTests(unittest.TestCase):
    def test_duplicate_event_id_is_idempotent(self) -> None:
        service, _, store = make_service()
        report = station("E1", "BS-1", "A-H", "医院", T0)
        first = service.ingest_report(report)
        self.assertTrue(first["accepted"])
        before = len(list(store.records()))
        second = service.ingest_report(report)
        self.assertFalse(second["accepted"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["decisions"], [])
        self.assertEqual(len(list(store.records())), before)  # 日志未增长

    def test_late_stale_report_does_not_change_facts(self) -> None:
        service, _, _ = make_service()
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        service.ingest_report(sat_window("W2", "SAT-1", T0, END, 40, "2026-09-24T08:05:00Z"))
        # 迟到的旧版本（occurred_at 更早）被受理但不改变事实、不产生决策
        late = service.ingest_report(sat_window("W3", "SAT-1", T0, END, 100, T0))
        self.assertTrue(late["accepted"])
        self.assertEqual(late["decisions"], [])
        windows = service.snapshot()["resources"]["satellite_windows"]
        self.assertEqual(windows[0]["capacity_mbps"], 40)

    def test_out_of_order_restore_then_outage(self) -> None:
        """恢复上报先到、退服上报迟到：最终事实仍是已恢复。"""
        service, _, _ = make_service()
        service.ingest_report(
            station("E2", "BS-1", "A-H", "医院", "2026-09-24T09:00:00Z", status="RESTORED")
        )
        result = service.ingest_report(station("E1", "BS-1", "A-H", "医院", T0))
        self.assertEqual(result["decisions"], [])
        self.assertEqual(service.snapshot()["counts"]["plans"], 0)

    def test_permutation_of_reports_converges(self) -> None:
        """同一组上报按不同顺序到达，最终事实与分配一致。"""
        reports = [
            depot("D1", "DEPOT-1", 2, 40, T0),
            sat_window("W1", "SAT-1", T0, END, 60, T0),
            station("E1", "BS-1", "A-H", "医院", T0),
            station("E2", "BS-2", "A-O", "普通区域", T0),
        ]
        snapshots = []
        for order in ([0, 1, 2, 3], [3, 2, 1, 0], [2, 0, 3, 1]):
            service, _, _ = make_service()
            for index in order:
                service.ingest_report(reports[index])
            snap = service.snapshot()
            normalized = {
                "areas": [
                    (a["area_id"], a["demand_mbps"], a["unmet_mbps"]) for a in snap["areas"]
                ],
                "actions": sorted(
                    (a["area_id"], a["kind"], a["resource_ref"], a["capacity_mbps"])
                    for a in snap["unfinished_actions"]
                ),
            }
            snapshots.append(normalized)
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[1], snapshots[2])


class AuditTests(unittest.TestCase):
    def test_decision_carries_traceability_fields(self) -> None:
        service, _, _ = make_service()
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        result = service.ingest_report(station("E1", "BS-1", "A-H", "医院", T0))
        self.assertEqual(len(result["decisions"]), 1)
        decision = result["decisions"][0]
        self.assertEqual(decision["operator"], "system")
        self.assertIn("R-HOSPITAL-GUARANTEE", decision["rules_hit"])
        self.assertIn("R-SATELLITE-WINDOW", decision["rules_hit"])
        # 输入版本记录了决策所依据的每份上报
        versions = {v["source_key"]: v["event_id"] for v in decision["input_versions"]}
        self.assertEqual(versions.get("satwin:SAT-1"), "W1")
        self.assertEqual(versions.get("station:BS-1"), "E1")
        self.assertEqual(decision["replaces"], [])

    def test_supersede_relation_is_recorded(self) -> None:
        service, clock, _ = make_service()
        service.ingest_report(sat_window("W1", "SAT-1", T0, END, 100, T0))
        service.ingest_report(station("E1", "BS-1", "A-H", "医院", T0))
        # 链路退化触发重排，新决策必须记录被替代的方案
        result = service.ingest_report(
            sat_window("W2", "SAT-1", T0, END, 40, "2026-09-24T08:05:00Z")
        )
        decision = result["decisions"][0]
        self.assertEqual(decision["reason"], "RESOURCE_DEGRADED")
        self.assertEqual(decision["replaces"], ["PLAN-0001"])
        old_plan = service.get_plan("PLAN-0001")
        self.assertEqual(old_plan["status"], "SUPERSEDED")
        self.assertEqual(old_plan["replaced_by"], decision["plan_id"])


class ValidationTests(unittest.TestCase):
    def test_invalid_reports_are_rejected(self) -> None:
        service, _, _ = make_service()
        bad_reports = [
            {"event_id": "X", "kind": "unknown", "occurred_at": T0},
            {"event_id": "X", "kind": "station_status", "occurred_at": "not-a-time",
             "station_id": "BS", "status": "OUTAGE",
             "area": {"area_id": "A", "category": "医院"}},
            {"event_id": "X", "kind": "station_status", "occurred_at": T0,
             "station_id": "BS", "status": "OUTAGE",
             "area": {"area_id": "A", "category": "商场"}},
            {"event_id": "X", "kind": "satellite_window", "occurred_at": T0,
             "sat_id": "S", "window_start": END, "window_end": T0, "capacity_mbps": 10},
            {"kind": "station_status", "occurred_at": T0, "station_id": "BS",
             "status": "OUTAGE", "area": {"area_id": "A", "category": "医院"}},
        ]
        for report in bad_reports:
            with self.assertRaises(ValidationError, msg=report):
                service.ingest_report(report)


if __name__ == "__main__":
    unittest.main()
