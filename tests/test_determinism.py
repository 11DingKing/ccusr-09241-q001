"""确定性：同一乱序事件集稳定重放出一致结果。"""

import io
import json
import os
import tempfile
import unittest
from argparse import Namespace

from tests.helpers import T0, depot, make_service, sat_window, station, team

END = "2026-09-24T12:00:00Z"
SCENARIO = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scenarios",
    "typhoon_landing.json",
)


class DrillDeterminismTests(unittest.TestCase):
    def _run_drill(self, db: str | None = None) -> str:
        out = io.StringIO()
        args = Namespace(scenario=SCENARIO, db=db)
        from resilience_command.interfaces.cli import run_drill

        code = run_drill(args, out)
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_same_scenario_replays_identically(self) -> None:
        first = self._run_drill()
        second = self._run_drill()
        self.assertEqual(first, second)

    def test_drill_output_covers_key_moments(self) -> None:
        output = self._run_drill()
        for fragment in (
            "PREEMPTED",
            "RESOURCE_DEGRADED",
            "重复忽略",
            "FREEZE",
            "WITHDRAW",
            "RESUME_AREA",
            "DEMAND_RESOLVED",
            "未完成行动",
        ):
            self.assertIn(fragment, output)

    def test_drill_db_replay_recovers_same_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "drill.jsonl")
            drill_out = self._run_drill(db=db)
            drill_snapshot = drill_out.split("== 状态快照 ==")[-1]
            replay_out = io.StringIO()
            from resilience_command.interfaces.cli import run_replay

            code = run_replay(Namespace(db=db), replay_out)
            self.assertEqual(code, 0)
            replay_snapshot = replay_out.getvalue().split("== 状态快照 ==")[-1]
            # 快照主体一致（时间行除外：replay 使用纪元时钟）
            drill_lines = [l for l in drill_snapshot.splitlines() if not l.startswith("时间")]
            replay_lines = [l for l in replay_snapshot.splitlines() if not l.startswith("时间")]
            self.assertEqual(drill_lines, replay_lines)


class PermutationTests(unittest.TestCase):
    """乱序事件集：不同到达顺序收敛到一致的资源分配。"""

    REPORTS = [
        depot("D1", "DEPOT-1", 1, 40, T0),
        sat_window("W1", "SAT-1", T0, END, 100, T0),
        team("T1", "TEAM-1", 0, 0, T0),
        station("E1", "BS-1", "A-H", "医院", T0),
        station("E2", "BS-2", "A-S", "避难点", T0),
        station("E3", "BS-3", "A-O", "普通区域", T0),
        sat_window("W2", "SAT-1", T0, END, 60, "2026-09-24T08:05:00Z"),  # 链路退化
    ]

    PERMUTATIONS = (
        [0, 1, 2, 3, 4, 5, 6],
        [6, 5, 4, 3, 2, 1, 0],
        [3, 0, 6, 1, 4, 2, 5],
        [5, 2, 4, 6, 0, 3, 1],
    )

    def _normalized_state(self, order) -> dict:
        service, _, _ = make_service()
        for index in order:
            service.ingest_report(self.REPORTS[index])
        snap = service.snapshot()
        return {
            "areas": [
                (a["area_id"], a["demand_mbps"], a["unmet_mbps"], a["on_hold"])
                for a in snap["areas"]
            ],
            "actions": sorted(
                (a["area_id"], a["kind"], a["resource_ref"], a["capacity_mbps"], a["units"])
                for a in snap["unfinished_actions"]
            ),
            "resources": json.dumps(snap["resources"], sort_keys=True),
        }

    def test_all_permutations_converge(self) -> None:
        states = [self._normalized_state(order) for order in self.PERMUTATIONS]
        for state in states[1:]:
            self.assertEqual(state, states[0])


if __name__ == "__main__":
    unittest.main()
