"""应用服务测试：版本接续、抢占生效、冻结拦截、撤回、授权、重启恢复、确定性。"""

import json
import os
import tempfile
import unittest

from resilience_command.auth import AuthorizationError, Operator
from resilience_command.cli import typhoon_events
from resilience_command.clock import FixedClock
from resilience_command.identifiers import DeterministicIds
from resilience_command.models import ActionState, PlanState
from resilience_command.service import CommandCenter, CommandError
from resilience_command.store import EventStore

T0 = "2026-09-24T10:00:00Z"
DIRECTOR = Operator("张值班长", "duty_director")
OPERATOR_USER = Operator("王值班员", "operator")


def fresh_center(store=None, clock=None) -> CommandCenter:
    return CommandCenter(store=store, clock=clock or FixedClock(T0), ids=DeterministicIds())


def first_wave(center: CommandCenter) -> None:
    for payload in typhoon_events()[:9]:
        center.ingest(dict(payload))


def second_wave(center: CommandCenter) -> None:
    for payload in typhoon_events()[9:]:
        center.ingest(dict(payload))


class LifecycleTests(unittest.TestCase):
    def test_replan_requires_authorization(self) -> None:
        center = fresh_center()
        first_wave(center)
        with self.assertRaises(AuthorizationError):
            center.replan(operator=None)
        # operator 角色可以重排
        plans = center.replan(operator=OPERATOR_USER)
        self.assertTrue(plans)

    def test_operator_cannot_approve_freeze_or_withdraw(self) -> None:
        center = fresh_center()
        first_wave(center)
        plan = center.replan(operator=OPERATOR_USER)[0]
        with self.assertRaises(AuthorizationError):
            center.approve(plan.plan_id, OPERATOR_USER)
        with self.assertRaises(AuthorizationError):
            center.withdraw(plan.plan_id, OPERATOR_USER)

    def test_full_plan_versioning_and_atomic_preemption(self) -> None:
        clock = FixedClock(T0)
        center = fresh_center(clock=clock)
        first_wave(center)
        center.replan(operator=OPERATOR_USER, reason="首轮")
        for plan in sorted(center.proposals(), key=lambda p: p.area):
            clock.advance(minutes=1)
            center.approve(plan.plan_id, DIRECTOR)
        shelter_v1 = center.current_plan("斗门避难点")
        normal_v1 = center.current_plan("金湾普通片区")
        self.assertEqual(shelter_v1.state, PlanState.ACTIVE)

        # 医院迟到 -> 重排产生三方草案
        second_wave(center)
        clock.advance(minutes=1)
        center.replan(operator=OPERATOR_USER, reason="医院到达")
        hospital_prop = center.proposed_plan("市人民医院")
        self.assertIsNotNone(hospital_prop)
        # 现役方案在医院草案批准前不受影响
        self.assertEqual(center.current_plan("斗门避难点").state, PlanState.ACTIVE)
        shelter_sat_action = next(
            a for a in center.current_plan("斗门避难点").actions if a.type.value == "allocate_capacity")
        self.assertEqual(shelter_sat_action.detail["mbps"], 20)

        # 受益方（医院）先批准：普通区原卫星动作被抢占到 0 -> PREEMPTED
        clock.advance(minutes=1)
        center.approve(hospital_prop.plan_id, DIRECTOR)
        # 受害方新版随后接续批准（其抢占表为空或已随受害方新版释放）
        for area in ("斗门避难点", "金湾普通片区"):
            clock.advance(minutes=1)
            center.approve(center.proposed_plan(area).plan_id, DIRECTOR)

        # 旧版方案被接续替代
        self.assertEqual(shelter_v1.state, PlanState.SUPERSEDED)
        self.assertEqual(normal_v1.state, PlanState.SUPERSEDED)
        # 新版现役；医院 40，避难点守底 10，普通区 0
        self.assertEqual(_sat(center, "市人民医院"), 40)
        self.assertEqual(_sat(center, "斗门避难点"), 10)
        self.assertEqual(_sat(center, "金湾普通片区"), 0)
        # 被抢占到 0 的普通区旧动作留痕：PREEMPTED 且指向医院动作
        normal_old_sat = next(
            a for a in normal_v1.actions if a.type.value == "allocate_capacity")
        self.assertEqual(normal_old_sat.state, ActionState.PREEMPTED)
        self.assertIsNotNone(normal_old_sat.preempted_by)
        # 避难点旧动作尚余 10Mbps，随后被自身新版接续 -> SUPERSEDED
        shelter_old_sat = next(
            a for a in shelter_v1.actions if a.type.value == "allocate_capacity")
        self.assertEqual(shelter_old_sat.state, ActionState.SUPERSEDED)
        self.assertIsNotNone(shelter_old_sat.superseded_by)

    def test_frozen_victim_blocks_beneficiary_approval(self) -> None:
        clock = FixedClock(T0)
        center = fresh_center(clock=clock)
        first_wave(center)
        center.replan(operator=OPERATOR_USER)
        for plan in sorted(center.proposals(), key=lambda p: p.area):
            clock.advance(minutes=1)
            center.approve(plan.plan_id, DIRECTOR)
        # 医院迟到并重排：医院草案此时已含针对避难点/普通区的待生效抢占
        second_wave(center)
        clock.advance(minutes=1)
        center.replan(operator=OPERATOR_USER)
        hospital_prop = center.proposed_plan("市人民医院")
        self.assertTrue(any(h["rule"] == "preemption_on_approval"
                            for h in hospital_prop.rule_hits))
        # 草案形成后，值班长冻结避难点现役方案
        shelter_active = center.current_plan("斗门避难点")
        clock.advance(minutes=1)
        center.freeze(shelter_active.plan_id, DIRECTOR, reason="人员密集管控")
        # 再批准医院：编排时可抢占的目标现已冻结，必须被拦截
        with self.assertRaises(CommandError) as ctx:
            clock.advance(minutes=1)
            center.approve(hospital_prop.plan_id, DIRECTOR)
        self.assertIn("冻结", str(ctx.exception))
        # 冻结方案占用维持不变，医院仍停留在待批
        self.assertEqual(_sat(center, "斗门避难点"), 20)
        self.assertEqual(center.plans[hospital_prop.plan_id].state, PlanState.PROPOSED)
        # 冻结态不能重复冻结
        with self.assertRaises(CommandError):
            center.freeze(shelter_active.plan_id, DIRECTOR)

    def test_freeze_only_allows_active_and_resume_restores(self) -> None:
        clock = FixedClock(T0)
        center = fresh_center(clock=clock)
        first_wave(center)
        center.replan(operator=OPERATOR_USER)
        prop = center.proposals()[0]
        # PROPOSED 不能冻结
        with self.assertRaises(CommandError):
            center.freeze(prop.plan_id, DIRECTOR)
        clock.advance(minutes=1)
        center.approve(prop.plan_id, DIRECTOR)
        clock.advance(minutes=1)
        center.freeze(prop.plan_id, DIRECTOR, reason="管控")
        self.assertEqual(center.current_plan(prop.area).state, PlanState.FROZEN)
        # 冻结期间重排不为该区域出草案
        clock.advance(minutes=1)
        created = center.replan(operator=OPERATOR_USER)
        self.assertFalse(any(p.area == prop.area for p in created))
        clock.advance(minutes=1)
        center.resume(prop.plan_id, DIRECTOR)
        self.assertEqual(center.current_plan(prop.area).state, PlanState.ACTIVE)

    def test_withdraw_proposal_vs_active(self) -> None:
        clock = FixedClock(T0)
        center = fresh_center(clock=clock)
        first_wave(center)
        center.replan(operator=OPERATOR_USER)
        prop = center.proposals()[0]
        clock.advance(minutes=1)
        center.withdraw(prop.plan_id, DIRECTOR, reason="误报")
        self.assertEqual(center.plans[prop.plan_id].state, PlanState.WITHDRAWN)
        # 撤回草案后该区域无现役方案，可重新编排出 v1
        clock.advance(minutes=1)
        again = center.replan(operator=OPERATOR_USER)
        self.assertTrue(any(p.area == prop.area and p.version == 1 for p in again))

    def test_withdraw_active_releases_resources(self) -> None:
        clock = FixedClock(T0)
        center = fresh_center(clock=clock)
        first_wave(center)
        center.replan(operator=OPERATOR_USER)
        for plan in sorted(center.proposals(), key=lambda p: p.area):
            clock.advance(minutes=1)
            center.approve(plan.plan_id, DIRECTOR)
        active = center.current_plan("金湾普通片区")
        clock.advance(minutes=1)
        center.withdraw(active.plan_id, DIRECTOR)
        self.assertTrue(all(a.state == ActionState.CANCELLED for a in active.actions))
        # 重排时被撤回区域不再占用窗口容量
        second_wave(center)
        clock.advance(minutes=1)
        new_plans = center.replan(operator=OPERATOR_USER)
        hospital = next(p for p in new_plans if p.area == "市人民医院")
        # 普通区释放 5，但避难点守底 10，医院卫星上限仍为 40（缺口由便携站补）
        self.assertEqual(_sat_of_plan(hospital), 40)

    def test_report_action_completed_closes_plan_when_all_done(self) -> None:
        clock = FixedClock(T0)
        center = fresh_center(clock=clock)
        first_wave(center)
        center.replan(operator=OPERATOR_USER)
        plan = sorted(center.proposals(), key=lambda p: p.area)[0]
        clock.advance(minutes=1)
        center.approve(plan.plan_id, DIRECTOR)
        for action in list(center.current_plan(plan.area).actions):
            clock.advance(minutes=1)
            center.report_action_completed(action.action_id, DIRECTOR, result="已恢复")
        self.assertEqual(center.current_plan(plan.area).state, PlanState.COMPLETED)

    def test_replan_replaces_unapproved_proposal_and_keeps_single_pending(self) -> None:
        clock = FixedClock(T0)
        center = fresh_center(clock=clock)
        first_wave(center)
        first = center.replan(operator=OPERATOR_USER, reason="v1 草案")
        first_ids = {p.plan_id for p in first}
        clock.advance(minutes=1)
        # 无新事件再次重排：分配不变 -> 只产生 noop，不新建草案
        again = center.replan(operator=OPERATOR_USER, reason="无变化")
        self.assertEqual(again, [])
        # 新事件到达后重排：旧待批草案被 DISCARDED，同区域只保留一个待批
        second_wave(center)
        clock.advance(minutes=1)
        newer = center.replan(operator=OPERATOR_USER, reason="v2 草案")
        for plan in first:
            # 仅当该区域确实产生了新版时旧草案才被丢弃
            new_for_area = [p for p in newer if p.area == plan.area]
            if new_for_area:
                self.assertEqual(center.plans[plan.plan_id].state, PlanState.DISCARDED)
                self.assertEqual(new_for_area[0].version, plan.version + 1)
                with self.assertRaises(CommandError):
                    center.approve(plan.plan_id, DIRECTOR)
        # 每区域至多一个待批草案
        pending_areas = [p.area for p in center.proposals()]
        self.assertEqual(len(pending_areas), len(set(pending_areas)))
        self.assertTrue(first_ids.isdisjoint({p.plan_id for p in center.proposals()}))

    def test_ingest_is_idempotent_on_supplied_event_id(self) -> None:
        center = fresh_center()
        payload = dict(typhoon_events()[0])
        e1 = center.ingest(payload)
        e2 = center.ingest(dict(payload))
        self.assertEqual(e1.event_id, e2.event_id)
        self.assertEqual(len(center.events), 1)


class RecoveryTests(unittest.TestCase):
    def test_recover_from_log_matches_in_memory_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "command_log.jsonl")
            clock = FixedClock(T0)
            center = CommandCenter(EventStore(log_path), clock=clock, ids=DeterministicIds())
            first_wave(center)
            center.replan(operator=OPERATOR_USER)
            for plan in sorted(center.proposals(), key=lambda p: p.area):
                clock.advance(minutes=1)
                center.approve(plan.plan_id, DIRECTOR)
            second_wave(center)
            clock.advance(minutes=1)
            center.replan(operator=OPERATOR_USER)
            expected_pending = _pending_tuples(center)
            expected_decisions = center.decision_log()

            # 重启：新对象从同一日志恢复
            recovered = CommandCenter(EventStore(log_path), clock=clock, ids=DeterministicIds())
            self.assertEqual(_pending_tuples(recovered), expected_pending)
            self.assertEqual(
                json.dumps(recovered.decision_log(), ensure_ascii=False, sort_keys=True),
                json.dumps(expected_decisions, ensure_ascii=False, sort_keys=True),
            )
            # 恢复后继续操作，生成的标识不与历史冲突
            clock.advance(minutes=1)
            new_plans = recovered.approve  # 存在性检查
            self.assertTrue(callable(new_plans))
            hosp = recovered.proposed_plan("市人民医院")
            clock.advance(minutes=1)
            recovered.approve(hosp.plan_id, DIRECTOR)
            self.assertEqual(recovered.current_plan("市人民医院").state, PlanState.ACTIVE)

    def test_replay_then_withdraw_and_replan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "command_log.jsonl")
            clock = FixedClock(T0)
            center = CommandCenter(EventStore(log_path), clock=clock, ids=DeterministicIds())
            first_wave(center)
            center.replan(operator=OPERATOR_USER)
            for plan in sorted(center.proposals(), key=lambda p: p.area):
                clock.advance(minutes=1)
                center.approve(plan.plan_id, DIRECTOR)
            del center

            recovered = CommandCenter(EventStore(log_path), clock=FixedClock(T0),
                                      ids=DeterministicIds())
            pending = recovered.pending_actions()
            self.assertGreaterEqual(len(pending), 2)
            active = recovered.current_plan("金湾普通片区")
            recovered.withdraw(active.plan_id, DIRECTOR)
            self.assertEqual(recovered.current_plan("金湾普通片区").state, PlanState.WITHDRAWN)


class DeterminismTests(unittest.TestCase):
    def _run(self, ordered_payloads) -> str:
        clock = FixedClock(T0)  # 全部接收到同一固定时刻
        center = fresh_center(clock=clock)
        for payload in ordered_payloads:
            center.ingest(dict(payload))
        clock.advance(minutes=1)
        center.replan(operator=OPERATOR_USER)
        return json.dumps(
            [p.to_dict() for p in sorted(center.proposals(), key=lambda p: p.plan_id)],
            ensure_ascii=False, sort_keys=True)

    def test_shuffled_ingest_orders_produce_identical_proposals(self) -> None:
        import random
        events = typhoon_events()
        rng = random.Random(7)
        orders = []
        for _ in range(3):
            order = events[:]
            rng.shuffle(order)
            orders.append(order)
        signatures = {self._run(order) for order in orders}
        self.assertEqual(len(signatures), 1)


def _sat(center: CommandCenter, area: str) -> int:
    return _sat_of_plan(center.current_plan(area))


def _sat_of_plan(plan) -> int:
    return sum(int(a.detail.get("mbps", 0)) for a in plan.actions
               if a.type.value == "allocate_capacity")


def _pending_tuples(center: CommandCenter):
    return sorted(
        (item["area"], item["plan_id"], item["action"]["action_id"], item["action"]["state"])
        for item in center.pending_actions())


if __name__ == "__main__":
    unittest.main()
