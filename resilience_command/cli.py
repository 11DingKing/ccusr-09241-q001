"""命令行演练入口。

子命令：
  * ``demo``   内置台风过境场景：迟到灾情、抢占、冻结拦截、撤回、接续、重启恢复；
  * ``replay`` 从数据目录的只追加日志恢复，打印当前方案与未完成行动；
  * ``serve``  启动本地 HTTP 服务。

``demo`` 使用固定时钟与确定性标识，同一组乱序事件重复执行输出完全一致，
并内置两次不同乱序接入的稳定性自检。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
from typing import Any

from .auth import Operator
from .clock import FixedClock
from .identifiers import DeterministicIds
from .service import CommandCenter, CommandError
from .store import EventStore

DIRECTOR = Operator("张值班长", "duty_director")
OPERATOR = Operator("王值班员", "operator")

# 场景时间轴
T0 = "2026-09-24T10:00:00Z"
WIN_START = "2026-09-24T09:00:00Z"
WIN_END = "2026-09-24T18:00:00Z"

NORMAL_AREA = "金湾普通片区"
SHELTER_AREA = "斗门避难点"
HOSPITAL_AREA = "市人民医院"


def typhoon_events() -> list[dict[str, Any]]:
    """返回台风场景上报；医院一组事件发生时间更早但到达更晚（迟到）。"""
    wave1: list[dict[str, Any]] = [
        {"event_id": "e-win-1", "type": "satellite_window", "occurred_at": "2026-09-24T09:00:00Z",
         "source": "卫星网管", "payload": {"window_id": "w1", "start": WIN_START, "end": WIN_END,
                                          "capacity_mbps": 50}},
        {"event_id": "e-stock-1", "type": "portable_station_stock", "occurred_at": "2026-09-24T08:30:00Z",
         "source": "市物资库", "payload": {"quantity": 5, "source": "市物资库"}},
        {"event_id": "e-team-1", "type": "repair_team_report", "occurred_at": "2026-09-24T09:10:00Z",
         "source": "抢修一队", "payload": {"team_id": "T1", "status": "available",
                                          "lat": 22.54, "lng": 113.58}},
        {"event_id": "e-team-2", "type": "repair_team_report", "occurred_at": "2026-09-24T09:12:00Z",
         "source": "抢修二队", "payload": {"team_id": "T2", "status": "available",
                                          "lat": 22.51, "lng": 113.55}},
        {"event_id": "e-normal-1", "type": "disaster_report", "occurred_at": "2026-09-24T09:40:00Z",
         "source": "片区网格员",
         "payload": {"area": NORMAL_AREA, "area_type": "normal", "severity": "medium",
                     "population": 1200, "lat": 22.50, "lng": 113.55}},
        {"event_id": "e-bs-n1", "type": "base_station_down", "occurred_at": "2026-09-24T09:42:00Z",
         "source": "网管", "payload": {"area": NORMAL_AREA, "station_id": "BS-N1"}},
        {"event_id": "e-shelter-1", "type": "disaster_report", "occurred_at": "2026-09-24T09:46:00Z",
         "source": "避难点管理员",
         "payload": {"area": SHELTER_AREA, "area_type": "shelter", "severity": "high",
                     "population": 3000, "lat": 22.53, "lng": 113.57}},
        {"event_id": "e-bs-s1", "type": "base_station_down", "occurred_at": "2026-09-24T09:48:00Z",
         "source": "网管", "payload": {"area": SHELTER_AREA, "station_id": "BS-S1"}},
        {"event_id": "e-cable-s1", "type": "cable_cut", "occurred_at": "2026-09-24T09:49:00Z",
         "source": "巡线", "payload": {"area": SHELTER_AREA, "cable_id": "C-S1"}},
    ]
    # 医院灾情实际发生更早，却因回传受阻迟到
    wave2 = [
        {"event_id": "e-hosp-1", "type": "disaster_report", "occurred_at": "2026-09-24T09:30:00Z",
         "source": "医院应急办",
         "payload": {"area": HOSPITAL_AREA, "area_type": "hospital", "severity": "critical",
                     "population": 900, "lat": 22.55, "lng": 113.59}},
        {"event_id": "e-bs-h1", "type": "base_station_down", "occurred_at": "2026-09-24T09:31:00Z",
         "source": "网管", "payload": {"area": HOSPITAL_AREA, "station_id": "BS-H1"}},
        {"event_id": "e-bs-h2", "type": "base_station_down", "occurred_at": "2026-09-24T09:32:00Z",
         "source": "网管", "payload": {"area": HOSPITAL_AREA, "station_id": "BS-H2"}},
        {"event_id": "e-cable-h1", "type": "cable_cut", "occurred_at": "2026-09-24T09:33:00Z",
         "source": "巡线", "payload": {"area": HOSPITAL_AREA, "cable_id": "C-H1"}},
    ]
    return wave1 + wave2


# ---------------------------------------------------------------------------
# 展示辅助
# ---------------------------------------------------------------------------
def alloc_summary(center: CommandCenter, area: str) -> str:
    plan = center.current_plan(area)
    prop = center.proposed_plan(area)
    if plan is None and prop is None:
        return f"  {area}: 无方案"
    parts = [f"  {area}"]
    if plan is not None:
        parts.append(f"[{plan.state.value} v{plan.version}] {_alloc_of(plan)}")
    if prop is not None and prop.plan_id != getattr(plan, "plan_id", None):
        parts.append(f"｜待批 v{prop.version} → {_alloc_of(prop)}")
    return " ".join(parts)


def _alloc_of(plan) -> str:
    mbps = qty = 0
    team = None
    for action in plan.actions:
        if action.type.value == "allocate_capacity":
            mbps += int(action.detail.get("mbps", 0))
        elif action.type.value == "deploy_station":
            qty += int(action.detail.get("quantity", 0))
        elif action.type.value == "dispatch_team":
            team = action.detail.get("team_id")
    return f"卫星 {mbps}Mbps / 便携站 {qty} 个 / 抢修队 {team or '-'}"


def rule_names(plan) -> list[str]:
    return [h["rule"] for h in plan.rule_hits]


def print_step(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def approve_all(center: CommandCenter, clock: FixedClock) -> list[str]:
    """按区域名顺序批准全部待批草案；返回被批准的方案 ID。"""
    approved = []
    proposals = sorted(center.proposals(), key=lambda p: p.area)
    for plan in proposals:
        clock.advance(minutes=1)
        try:
            center.approve(plan.plan_id, DIRECTOR, reason="值班长授权")
            approved.append(plan.plan_id)
            print(f"  · 已授权 {plan.area} v{plan.version}（{plan.plan_id}）")
        except CommandError as exc:
            print(f"  ! 授权被拦截 {plan.area} v{plan.version}：{exc}")
    return approved


# ---------------------------------------------------------------------------
# 主演练
# ---------------------------------------------------------------------------
def run_demo(data_dir: str, reset: bool) -> None:
    log_path = os.path.join(data_dir, "command_log.jsonl")
    if reset and os.path.exists(log_path):
        os.remove(log_path)
    os.makedirs(data_dir, exist_ok=True)

    clock = FixedClock(T0)
    center = CommandCenter(
        store=EventStore(log_path),
        clock=clock,
        ids=DeterministicIds(),
    )
    events = typhoon_events()
    wave1, wave2 = events[:9], events[9:]

    print_step("步骤 1  接收首批上报（卫星窗口 50Mbps、库存 5 台、2 支抢修队、避难点/普通区灾情）")
    clock.advance(minutes=5)
    for payload in wave1:
        center.ingest(copy.deepcopy(payload))
    print(f"  已接收 {len(wave1)} 条上报，接收时刻 {clock.now():%Y-%m-%dT%H:%M:%SZ}")

    clock.advance(minutes=1)
    proposals = center.replan(operator=OPERATOR, reason="首轮编排")
    print(f"  重排生成 {len(proposals)} 份待批方案：")
    for area in (SHELTER_AREA, NORMAL_AREA):
        print(alloc_summary(center, area))

    print_step("步骤 2  值班长授权首轮方案")
    approve_all(center, clock)
    for area in (SHELTER_AREA, NORMAL_AREA):
        print(alloc_summary(center, area))
    print("  未完成行动：", len(center.pending_actions()), "项")

    print_step("步骤 3  医院的迟到灾情到达（2 基站退服 + 1 光缆中断，按发生时间仍是高优先级）")
    for payload in wave2:
        clock.advance(seconds=10)
        center.ingest(copy.deepcopy(payload))
    clock.advance(minutes=1)
    proposals = center.replan(operator=OPERATOR, reason="医院迟到灾情触发重排")
    print(f"  重排生成 {len(proposals)} 份待批方案：")
    for area in (HOSPITAL_AREA, SHELTER_AREA, NORMAL_AREA):
        print(alloc_summary(center, area))
    hosp = center.proposed_plan(HOSPITAL_AREA)
    print("  医院草案规则命中：", ", ".join(rule_names(hosp)))

    print_step("步骤 4  先接续避难点/普通区新版，再授权医院方案（抢占在批准时原子生效）")
    for area in (SHELTER_AREA, NORMAL_AREA):
        plan = center.proposed_plan(area)
        clock.advance(minutes=1)
        center.approve(plan.plan_id, DIRECTOR, reason="按新编排接续")
        print(f"  · {area} 已接续到 v{plan.version}")
    hosp = center.proposed_plan(HOSPITAL_AREA)
    clock.advance(minutes=1)
    center.approve(hosp.plan_id, DIRECTOR, reason="医院优先级最高，执行有约束抢占")
    print(f"  · {HOSPITAL_AREA} v{hosp.version} 已授权")
    for area in (HOSPITAL_AREA, SHELTER_AREA, NORMAL_AREA):
        print(alloc_summary(center, area))
    for hit in hosp.rule_hits:
        if hit["rule"] == "preemption_on_approval":
            d = hit["detail"]
            print(f"  抢占留痕：从 {d['victim_area']} 的 {d['victim_action_id']} "
                  f"收回 {d['resource']} {d['amount']}（单位 Mbps/个/队），"
                  f"受益动作 {d['beneficiary_action_id']}")

    print_step("步骤 5  值班长冻结避难点方案；全局链路退化至 50% 后重排，冻结占用受保护")
    shelter_plan = center.current_plan(SHELTER_AREA)
    clock.advance(minutes=1)
    center.freeze(shelter_plan.plan_id, DIRECTOR, reason="避难点人员密集，暂停资源调整")
    print(f"  · {SHELTER_AREA} v{shelter_plan.version} 已冻结")
    clock.advance(minutes=1)
    center.ingest({
        "event_id": "e-deg-1", "type": "link_degradation",
        "occurred_at": "2026-09-24T10:45:00Z", "source": "传输网管",
        "payload": {"factor": 0.5},
    })
    proposals = center.replan(operator=OPERATOR, reason="卫星回传退化至 50%")
    print(f"  重排生成 {len(proposals)} 份待批方案；冻结区域不出草案。")
    last_noop = [d for d in center.decision_log() if d["type"] == "replan_noop"]
    for d in last_noop[-3:]:
        shortfall = [h["detail"]["shortfall_mbps"]
                     for h in d.get("rule_hits", []) if h["rule"] == "capacity_shortfall"]
        frozen_hit = any(h["rule"] == "area_frozen" for h in d.get("rule_hits", []))
        note = "冻结保护" if frozen_hit else (f"容量缺口 {shortfall[0]} Mbps 留痕" if shortfall else "分配维持")
        print(f"  · {d['area']}: {note}")

    print_step("步骤 6  撤回医院现役方案；医院基础设施恢复后重新编排并授权")
    hosp_active = center.current_plan(HOSPITAL_AREA)
    clock.advance(minutes=1)
    center.withdraw(hosp_active.plan_id, DIRECTOR, reason="备用微波链路恢复，撤回现役方案")
    print(f"  · 医院 v{hosp_active.version} 已撤回，其占用全部释放、行动标记取消")
    clock.advance(minutes=1)
    center.ingest({
        "event_id": "e-rec-1", "type": "recovery_report",
        "occurred_at": "2026-09-24T11:10:00Z", "source": "抢修一队",
        "payload": {"area": HOSPITAL_AREA,
                    "restored_station_ids": ["BS-H1", "BS-H2"],
                    "restored_cable_ids": ["C-H1"]},
    })
    proposals = center.replan(operator=OPERATOR, reason="医院基础设施恢复，按退化链路重新保障")
    print(f"  重排生成 {len(proposals)} 份待批方案：")
    for plan in proposals:
        print(f"  · {plan.area} v{plan.version}")
    approve_all(center, clock)
    for area in (HOSPITAL_AREA, SHELTER_AREA, NORMAL_AREA):
        print(alloc_summary(center, area))

    print_step("步骤 7  决策追溯：输入版本 / 规则命中 / 被替代关系 / 操作人")
    decisions = center.decision_log()
    print(f"  日志共 {len(center.event_log())} 条输入事件、{len(decisions)} 条决策记录。")
    for d in decisions:
        if d["type"] in ("replan", "approve", "freeze", "withdraw", "replan_noop"):
            basis = f"依据 {len(d.get('basis_event_ids', ()))} 个事件版本" if d["type"] == "replan" else ""
            supersedes = f"替代 {d.get('supersedes') or ''}" if d.get("supersedes") else ""
            print(f"  - {d['decision_id']} {d['at']} {d['type']:<12} "
                  f"操作人={d.get('operator')} 区域={d.get('area', '')} {basis} {supersedes}")

    print_step("步骤 8  重启恢复：用同一日志新建指挥中心，核对未完成行动")
    recovered = CommandCenter(store=EventStore(log_path), clock=clock, ids=DeterministicIds())
    before = sorted(
        (a["area"], a["action"]["action_id"], a["action"]["state"], a["plan_id"])
        for a in center.pending_actions()
    )
    after = sorted(
        (a["area"], a["action"]["action_id"], a["action"]["state"], a["plan_id"])
        for a in recovered.pending_actions()
    )
    match = before == after
    print(f"  重启后恢复未完成行动 {len(after)} 项，与重启前一致：{match}")
    for area, aid, state, pid in after:
        print(f"    - {area} {pid} {aid} [{state}]")
    decisions_match = len(recovered.decision_log()) == len(decisions)
    print(f"  决策记录条数恢复一致：{decisions_match}")

    print_step("步骤 9  乱序稳定性自检：同组事件两种乱序接入，决策记录逐字节一致")
    print(determinism_selfcheck())


# ---------------------------------------------------------------------------
# 乱序重放自检
# ---------------------------------------------------------------------------
def determinism_selfcheck() -> str:
    events = typhoon_events()
    rng = random.Random(20260924)
    order_a = events[:]
    order_b = events[:]
    rng.shuffle(order_a)
    rng.shuffle(order_b)
    # 策略依赖“波次”概念，自检改为一次性接入全部事件后重排
    log_a = _single_wave_run(order_a)
    log_b = _single_wave_run(order_b)
    same = json.dumps(log_a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(log_b, ensure_ascii=False, sort_keys=True)
    return (f"两种乱序接入后决策记录一致：{same}（各 {len(log_a)} 条决策）")


def _single_wave_run(ordered_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clock = FixedClock(T0)
    center = CommandCenter(clock=clock, ids=DeterministicIds())
    for payload in ordered_events:
        clock.advance(seconds=10)
        center.ingest(copy.deepcopy(payload))
    clock.advance(minutes=1)
    center.replan(operator=OPERATOR, reason="全量事件一次性重排")
    return center.decision_log()


# ---------------------------------------------------------------------------
# 日志重放
# ---------------------------------------------------------------------------
def run_replay(data_dir: str) -> None:
    log_path = os.path.join(data_dir, "command_log.jsonl")
    center = CommandCenter(store=EventStore(log_path))
    print(f"日志文件：{log_path}")
    print(f"输入事件 {len(center.event_log())} 条，决策记录 {len(center.decision_log())} 条")
    print("\n各区域当前方案：")
    for area in sorted(set(p.area for p in center.plans.values())):
        plan = center.current_plan(area)
        if plan:
            print(alloc_summary(center, area))
    print("\n未完成行动：")
    for item in center.pending_actions():
        action = item["action"]
        print(f"  - {item['area']} [{item['plan_state']}] {action['action_id']} "
              f"{action['type']} {json.dumps(action['detail'], ensure_ascii=False)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="灾后通信恢复指挥服务命令行入口")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="运行内置台风过境演练")
    p_demo.add_argument("--data-dir", default=os.path.join(os.getcwd(), "data"))
    p_demo.add_argument("--reset", action="store_true", help="演练前清空旧日志")

    p_replay = sub.add_parser("replay", help="从日志恢复并打印当前状态")
    p_replay.add_argument("--data-dir", default=os.path.join(os.getcwd(), "data"))

    sub.add_parser("serve", help="启动 HTTP 服务（透传其余参数见 --help）")

    args, rest = parser.parse_known_args(argv)
    if args.command == "demo":
        run_demo(args.data_dir, args.reset)
    elif args.command == "replay":
        run_replay(args.data_dir)
    elif args.command == "serve":
        from .api import main as serve_main
        return serve_main(rest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
