"""规则层测试：乱序折叠、优先级、底线约束抢占、冻结保护、队伍改派。"""

import unittest

from resilience_command.models import Event, EventType
from resilience_command.rules import Committed, fold, solve

NOW = "2026-09-24T10:00:00Z"


def ev(eid: str, etype: str, occurred: str, payload: dict, seq: int) -> Event:
    return Event(event_id=eid, type=EventType(etype), occurred_at=occurred,
                 received_at=NOW, source="test", payload=payload, sequence=seq)


def base_scenario() -> list[Event]:
    return [
        ev("w1", "satellite_window", "2026-09-24T09:00:00Z",
           {"window_id": "w1", "start": "2026-09-24T09:00:00Z",
            "end": "2026-09-24T18:00:00Z", "capacity_mbps": 50}, 1),
        ev("s1", "portable_station_stock", "2026-09-24T08:00:00Z",
           {"quantity": 3, "source": "库1"}, 2),
        ev("t1", "repair_team_report", "2026-09-24T09:10:00Z",
           {"team_id": "T1", "status": "available", "lat": 22.5, "lng": 113.5}, 3),
        ev("h-d", "disaster_report", "2026-09-24T09:30:00Z",
           {"area": "医院", "area_type": "hospital", "severity": "critical",
            "population": 500, "lat": 22.55, "lng": 113.6}, 4),
        ev("h-b1", "base_station_down", "2026-09-24T09:31:00Z",
           {"area": "医院", "station_id": "H1"}, 5),
        ev("n-d", "disaster_report", "2026-09-24T09:40:00Z",
           {"area": "普通区", "area_type": "normal", "severity": "medium",
            "population": 800}, 6),
        ev("n-b1", "base_station_down", "2026-09-24T09:41:00Z",
           {"area": "普通区", "station_id": "N1"}, 7),
    ]


class FoldTests(unittest.TestCase):
    def test_late_and_duplicate_reports_collapse_by_occurred_at(self) -> None:
        events = base_scenario()
        # 迟到的灾情更新：发生时间更早的旧报不得覆盖新报
        events.append(ev("h-old", "disaster_report", "2026-09-24T09:20:00Z",
                         {"area": "医院", "area_type": "hospital", "severity": "low",
                          "population": 10}, 8))
        # 重复退服上报
        events.append(ev("h-b1-dup", "base_station_down", "2026-09-24T09:31:30Z",
                         {"area": "医院", "station_id": "H1"}, 9))
        situations, _ = fold(events, NOW)
        hosp = situations["医院"]
        self.assertEqual(hosp.severity.value, "critical")  # 新报未被旧报覆盖
        self.assertEqual(hosp.population, 500)
        self.assertEqual(hosp.offline_stations, {"H1"})   # 重复上报不重复计数

    def test_recovery_removes_resources_and_late_recovery_ignored(self) -> None:
        events = base_scenario()
        # 09:55 恢复，但一条 09:58 声称仍退服的迟到重复上报不应让它重新变红
        events.append(ev("rec1", "recovery_report", "2026-09-24T09:55:00Z",
                         {"area": "普通区", "restored_station_ids": ["N1"]}, 10))
        situations, _ = fold(events, NOW)
        self.assertEqual(situations["普通区"].offline_stations, set())

        # 一条比退服更早的“恢复”迟到，不得提前抹掉故障
        events2 = base_scenario()
        events2.append(ev("rec-old", "recovery_report", "2026-09-24T09:40:00Z",
                          {"area": "普通区", "restored_station_ids": ["N1"]}, 11))
        situations2, _ = fold(events2, NOW)
        self.assertEqual(situations2["普通区"].offline_stations, {"N1"})

    def test_latest_stock_and_factor_win_on_out_of_order_delivery(self) -> None:
        events = base_scenario()
        # 后到的旧库存报不得减少当前库存
        events.append(ev("s0", "portable_station_stock", "2026-09-24T07:00:00Z",
                         {"quantity": 1, "source": "库1"}, 12))
        events.append(ev("s2", "portable_station_stock", "2026-09-24T09:30:00Z",
                         {"quantity": 2, "source": "库2"}, 13))
        _, inventory = fold(events, NOW)
        self.assertEqual(inventory.total_stock, 5)  # 库1=3(最新) + 库2=2
        events.append(ev("d1", "link_degradation", "2026-09-24T08:00:00Z",
                         {"factor": 0.2}, 14))  # 更旧的退化报
        events.append(ev("d2", "link_degradation", "2026-09-24T09:50:00Z",
                         {"factor": 0.5}, 15))
        _, inventory2 = fold(events, NOW)
        self.assertEqual(inventory2.global_factor, 0.5)


class SolveTests(unittest.TestCase):
    def test_priority_hospital_first_and_floor_constrained_preemption(self) -> None:
        # 普通区现役占用 5Mbps（已批准方案），医院迟到后应抢占该 5Mbps
        situations, inventory = fold(base_scenario(), NOW)
        committed = Committed.empty()
        committed.satellite["普通区"] = [{"action_id": "act-old", "mbps": 5.0}]
        drafts = solve(situations, inventory, committed)
        self.assertEqual(drafts["医院"].satellite_grant, 50)
        self.assertEqual(drafts["普通区"].satellite_grant, 0)
        # 医院的抢占记录指向普通区原动作
        hosp_reclaims = [r for r in drafts["医院"].reclaims if r.beneficiary_area == "医院"]
        self.assertTrue(hosp_reclaims)
        self.assertEqual(hosp_reclaims[0].victim_area, "普通区")
        self.assertEqual(hosp_reclaims[0].victim_action_id, "act-old")
        self.assertEqual(hosp_reclaims[0].amount, 5.0)
        # 普通区草案保留让出解释
        self.assertTrue(any(h["rule"] == "yielded_on_approval"
                            for h in drafts["普通区"].rule_hits))

    def test_floor_blocks_reclaim_against_shelter(self) -> None:
        # 避难点现役 16Mbps（底线 10），医院需要额外 10：最多让出 6
        events = [
            ev("w1", "satellite_window", "2026-09-24T09:00:00Z",
               {"window_id": "w1", "start": "2026-09-24T09:00:00Z",
                "end": "2026-09-24T18:00:00Z", "capacity_mbps": 56}, 1),
            ev("stk", "portable_station_stock", "2026-09-24T08:00:00Z",
               {"quantity": 5, "source": "库"}, 2),
            ev("hd", "disaster_report", "2026-09-24T09:30:00Z",
               {"area": "医院", "area_type": "hospital", "severity": "critical",
                "population": 900}, 3),
            ev("hb", "base_station_down", "2026-09-24T09:31:00Z",
               {"area": "医院", "station_id": "H1"}, 4),
            ev("sd", "disaster_report", "2026-09-24T09:20:00Z",
               {"area": "避难点", "area_type": "shelter", "severity": "high",
                "population": 2000}, 5),
            ev("sb", "base_station_down", "2026-09-24T09:21:00Z",
               {"area": "避难点", "station_id": "S1"}, 6),
        ]
        situations, inventory = fold(events, NOW)
        committed = Committed.empty()
        committed.satellite["避难点"] = [{"action_id": "act-s", "mbps": 16.0}]
        drafts = solve(situations, inventory, committed)
        # 空闲池 40 给医院后仍需 10；避难点只能让到 10 底线 -> 让出 6
        # 总窗口 56、避难点守底 10，故医院卫星上限为 46
        self.assertEqual(drafts["避难点"].satellite_grant, 10)
        self.assertEqual(drafts["医院"].satellite_grant, 46)
        reclaim = [r for r in drafts["医院"].reclaims if r.kind == "satellite"]
        self.assertEqual(reclaim[0].amount, 6.0)
        # 医院仍缺 4 有效 Mbps，由便携站补 1 台
        self.assertEqual(drafts["医院"].station_qty, 1)

    def test_hospital_floor_protected_when_hospital_is_victim(self) -> None:
        # 两台医院级区域不可能互相抢占；构造医院已有 30、避难点需求更大容量不足的场景
        events = [
            ev("w1", "satellite_window", "2026-09-24T09:00:00Z",
               {"window_id": "w1", "start": "2026-09-24T09:00:00Z",
                "end": "2026-09-24T18:00:00Z", "capacity_mbps": 40}, 1),
            ev("hd", "disaster_report", "2026-09-24T09:30:00Z",
               {"area": "医院", "area_type": "hospital", "severity": "critical",
                "population": 900}, 2),
            ev("sd", "disaster_report", "2026-09-24T09:31:00Z",
               {"area": "避难点", "area_type": "shelter", "severity": "high",
                "population": 9000}, 3),
            ev("sb", "base_station_down", "2026-09-24T09:32:00Z",
               {"area": "避难点", "station_id": "S1"}, 4),
        ]
        situations, inventory = fold(events, NOW)
        drafts = solve(situations, inventory, Committed.empty())
        # 医院 30、避难点目标 (10+10)*0.8=16，共 46 > 40
        self.assertEqual(drafts["医院"].satellite_grant, 30)  # 足额
        self.assertEqual(drafts["避难点"].satellite_grant, 10)  # 只剩 10
        self.assertTrue(any(h["rule"] == "capacity_shortfall"
                            for h in drafts["避难点"].rule_hits))

    def test_stations_fill_gap_and_can_be_reclaimed_with_floor(self) -> None:
        events = [
            ev("w1", "satellite_window", "2026-09-24T09:00:00Z",
               {"window_id": "w1", "start": "2026-09-24T09:00:00Z",
                "end": "2026-09-24T18:00:00Z", "capacity_mbps": 5}, 1),
            ev("stk", "portable_station_stock", "2026-09-24T08:00:00Z",
               {"quantity": 5, "source": "库"}, 2),
            ev("hd", "disaster_report", "2026-09-24T09:30:00Z",
               {"area": "医院", "area_type": "hospital", "severity": "critical",
                "population": 900}, 3),
            ev("hb", "base_station_down", "2026-09-24T09:31:00Z",
               {"area": "医院", "station_id": "H1"}, 4),
        ]
        situations, inventory = fold(events, NOW)
        drafts = solve(situations, inventory, Committed.empty())
        # 医院目标 50，窗口仅 5，缺口 45 -> 5 台便携站
        self.assertEqual(drafts["医院"].satellite_grant, 5)
        self.assertEqual(drafts["医院"].station_qty, 5)

    def test_global_degradation_shrinks_effective_capacity(self) -> None:
        events = base_scenario()
        events.append(ev("g", "link_degradation", "2026-09-24T09:50:00Z",
                         {"factor": 0.5}, 20))
        situations, inventory = fold(events, NOW)
        drafts = solve(situations, inventory, Committed.empty())
        # 名义窗口 50 在退化下有效交付仅 25；医院目标 50，
        # 拿满名义 50 后缺口 25 有效 Mbps，由 3 台便携站补足（30≥25）
        self.assertEqual(drafts["医院"].satellite_grant, 50)
        self.assertEqual(drafts["医院"].station_qty, 3)
        self.assertFalse(any(h["rule"] == "capacity_shortfall" for h in drafts["医院"].rule_hits))

    def test_team_reassigned_only_from_lower_priority_area(self) -> None:
        events = base_scenario()  # 仅 T1 一支队伍
        situations, inventory = fold(events, NOW)
        drafts = solve(situations, inventory, Committed.empty())
        self.assertEqual(drafts["医院"].team_id, "T1")
        # 普通区无队伍可派（唯一队伍被高优先级改派）
        self.assertIsNone(drafts["普通区"].team_id)


if __name__ == "__main__":
    unittest.main()
