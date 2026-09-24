"""命令行入口：本地服务、演练重放、日志恢复查看。

用法：
    python3 -m resilience_command serve  [--host H] [--port P] --db var/eventlog.jsonl [--tokens ops.json]
    python3 -m resilience_command drill  scenarios/typhoon_landing.json [--db var/drill.jsonl]
    python3 -m resilience_command replay --db var/drill.jsonl

``drill`` 使用手动时钟按场景文件逐步推进，同一场景文件无论何时运行都
产生逐字节一致的输出；``replay`` 从事件日志重建状态，展示重启恢复效果。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, TextIO

from ..application.service import CommandService
from ..domain.exceptions import DomainError
from ..domain.models import CATEGORY_LABELS, format_time
from ..domain.rules import RuleConfig
from ..infrastructure.auth import OperatorRegistry
from ..infrastructure.clock import ManualClock, SystemClock
from ..infrastructure.eventlog import JsonlEventLog
from ..infrastructure.ids import SequentialIds


def _build_service(
    store: JsonlEventLog, clock: Any, auth: OperatorRegistry
) -> CommandService:
    return CommandService(store, clock, SequentialIds(), auth, RuleConfig())


def _print_decision(decision: dict[str, Any], out: TextIO, indent: str = "  ") -> None:
    rules = ",".join(decision["rules_hit"])
    plan = decision.get("plan_id") or "-"
    replaces = ",".join(decision["replaces"]) or "-"
    print(
        f"{indent}{decision['decision_id']} {decision['kind']} {decision['reason']} "
        f"area={decision.get('area_id') or '-'} plan={plan} replaces={replaces} "
        f"by={decision['operator']} rules=[{rules}]",
        file=out,
    )


def _print_snapshot(snapshot: dict[str, Any], out: TextIO) -> None:
    print("== 状态快照 ==", file=out)
    print(f"时间: {snapshot['time']}", file=out)
    for area in snapshot["areas"]:
        label = CATEGORY_LABELS.get(area["category"], area["category"])
        plans = ",".join(area["plans"]) or "-"
        hold = "是" if area["on_hold"] else "否"
        print(
            f"区域 {area['area_id']} {area['name']}({label}) "
            f"需求={area['demand_mbps']}Mbps 缺口={area['unmet_mbps']}Mbps "
            f"挂起={hold} 方案={plans}",
            file=out,
        )
    resources = snapshot["resources"]
    for window in resources["satellite_windows"]:
        expired = " 已过期" if window["expired"] else ""
        print(
            f"卫星窗口 {window['sat_id']} 容量={window['capacity_mbps']}Mbps "
            f"剩余={window['remaining_mbps']}Mbps "
            f"[{window['window_start']}..{window['window_end']}]{expired}",
            file=out,
        )
    for depot in resources["portable_depots"]:
        print(
            f"便携站 {depot['depot_id']} 库存={depot['available']} "
            f"已调拨={depot['reserved']} 单站={depot['station_mbps']}Mbps",
            file=out,
        )
    for team in resources["repair_teams"]:
        assigned = team["assigned_area"] or "-"
        print(
            f"抢修队 {team['team_id']} {team['status']} "
            f"位置=({team['x']},{team['y']}) 派驻={assigned}",
            file=out,
        )
    unfinished = snapshot["unfinished_actions"]
    print(f"未完成行动: {len(unfinished)} 项", file=out)
    for action in unfinished:
        window = ""
        if action.get("window_start"):
            window = f" [{action['window_start']}..{action['window_end']}]"
        print(
            f"  {action['action_id']} {action['kind']} {action['resource_ref']} "
            f"{action['capacity_mbps']}Mbps x{action['units']} "
            f"plan={action['plan_id']} area={action['area_id']}{window}",
            file=out,
        )
    counts = snapshot["counts"]
    print(
        f"计数: 上报={counts['reports']} 方案={counts['plans']} "
        f"决策={counts['decisions']} 挂起={counts['holds']}",
        file=out,
    )


def run_drill(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    with open(args.scenario, "r", encoding="utf-8") as fh:
        scenario = json.load(fh)
    clock = ManualClock(scenario["start"])
    store = JsonlEventLog(args.db)
    auth = OperatorRegistry(scenario.get("tokens", {}))
    service = _build_service(store, clock, auth)

    print(f"# 演练: {scenario.get('name', args.scenario)}", file=out)
    print(f"# 起始时间: {format_time(clock.now())}", file=out)
    for index, step in enumerate(scenario.get("steps", []), start=1):
        at = step.get("at")
        if at:
            clock.set(at)
        stamp = format_time(clock.now())
        note = step.get("note")
        if note:
            print(f"[{stamp}] # {note}", file=out)
        try:
            if "report" in step:
                result = service.ingest_report(step["report"])
                report = step["report"]
                flag = "重复忽略" if result["duplicate"] else "已受理"
                print(
                    f"[{stamp}] 步{index} 上报 {report.get('event_id')} "
                    f"{report.get('kind')} -> {flag}",
                    file=out,
                )
                for decision in result["decisions"]:
                    _print_decision(decision, out)
            elif "reports" in step:
                for report in step["reports"]:
                    result = service.ingest_report(report)
                    flag = "重复忽略" if result["duplicate"] else "已受理"
                    print(
                        f"[{stamp}] 步{index} 上报 {report.get('event_id')} "
                        f"{report.get('kind')} -> {flag}",
                        file=out,
                    )
                    for decision in result["decisions"]:
                        _print_decision(decision, out)
            elif "freeze" in step:
                cmd = step["freeze"]
                result = service.freeze_plan(cmd["plan_id"], cmd.get("token", ""))
                print(f"[{stamp}] 步{index} 冻结 {cmd['plan_id']}", file=out)
                _print_decision(result["decision"], out)
                for decision in result["followups"]:
                    _print_decision(decision, out)
            elif "withdraw" in step:
                cmd = step["withdraw"]
                result = service.withdraw_plan(
                    cmd["plan_id"], cmd.get("token", ""), cmd.get("reason")
                )
                print(f"[{stamp}] 步{index} 撤回 {cmd['plan_id']}", file=out)
                _print_decision(result["decision"], out)
                for decision in result["followups"]:
                    _print_decision(decision, out)
            elif "resume_plan" in step:
                cmd = step["resume_plan"]
                result = service.resume_plan(cmd["plan_id"], cmd.get("token", ""))
                print(f"[{stamp}] 步{index} 接续方案 {cmd['plan_id']}", file=out)
                _print_decision(result["decision"], out)
                for decision in result["followups"]:
                    _print_decision(decision, out)
            elif "resume_area" in step:
                cmd = step["resume_area"]
                result = service.resume_area(cmd["area_id"], cmd.get("token", ""))
                print(f"[{stamp}] 步{index} 接续区域 {cmd['area_id']}", file=out)
                _print_decision(result["decision"], out)
                for decision in result["followups"]:
                    _print_decision(decision, out)
            elif "reevaluate" in step:
                result = service.reevaluate()
                print(f"[{stamp}] 步{index} 时钟重估", file=out)
                for decision in result["decisions"]:
                    _print_decision(decision, out)
            elif not note:
                print(f"[{stamp}] 步{index} （空步骤，忽略）", file=out)
        except DomainError as err:
            print(
                f"[{stamp}] 步{index} 拒绝: {err.code} {err.message}",
                file=out,
            )
    _print_snapshot(service.snapshot(), out)
    return 0


def run_replay(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    """从事件日志重建服务状态，验证重启后未完成行动可恢复。"""
    store = JsonlEventLog(args.db)
    service = _build_service(store, ManualClock("1970-01-01T00:00:00Z"), OperatorRegistry())
    print(f"# 从 {args.db} 重放 {len(list(store.records()))} 条记录", file=out)
    _print_snapshot(service.snapshot(), out)
    return 0


def run_serve(args: argparse.Namespace, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    from .http_api import create_server

    store = JsonlEventLog(args.db)
    if args.tokens:
        auth = OperatorRegistry.from_file(args.tokens)
    else:
        auth = OperatorRegistry.from_env()
    service = _build_service(store, SystemClock(), auth)
    server = create_server(service, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"指挥服务已启动: http://{host}:{port} （事件日志: {args.db or '内存'}）", file=out)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("收到中断信号，正在关闭……", file=out)
    finally:
        server.shutdown()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resilience_command", description="灾后多制式通信恢复指挥服务"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动本地 HTTP 指挥服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--db", default=None, help="事件日志路径（缺省为纯内存）")
    serve.add_argument("--tokens", default=None, help="操作人令牌配置 JSON")
    serve.set_defaults(func=run_serve)

    drill = sub.add_parser("drill", help="按场景文件演练（手动时钟，稳定复现）")
    drill.add_argument("scenario", help="场景 JSON 文件")
    drill.add_argument("--db", default=None, help="同时把事件写入该日志文件")
    drill.set_defaults(func=run_drill)

    replay = sub.add_parser("replay", help="从事件日志重放恢复状态")
    replay.add_argument("--db", required=True, help="事件日志路径")
    replay.set_defaults(func=run_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
