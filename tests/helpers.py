"""测试公共构造：可注入时钟的服务实例与各类上报报文。"""

from __future__ import annotations

from resilience_command.application.service import CommandService
from resilience_command.domain.rules import RuleConfig
from resilience_command.infrastructure.auth import OperatorRegistry
from resilience_command.infrastructure.clock import ManualClock
from resilience_command.infrastructure.eventlog import JsonlEventLog
from resilience_command.infrastructure.ids import SequentialIds

T0 = "2026-09-24T08:00:00Z"
DUTY_TOKEN = "tok-duty"
TOKENS = {DUTY_TOKEN: {"name": "林值班", "role": "duty_officer"}}


def make_service(
    db_path: str | None = None,
    start: str = T0,
    tokens: dict | None = None,
) -> tuple[CommandService, ManualClock, JsonlEventLog]:
    clock = ManualClock(start)
    store = JsonlEventLog(db_path)
    auth = OperatorRegistry(TOKENS if tokens is None else tokens)
    service = CommandService(store, clock, SequentialIds(), auth, RuleConfig())
    return service, clock, store


def reopen_service(
    db_path: str, start: str = T0, tokens: dict | None = None
) -> CommandService:
    """模拟重启：从同一事件日志重建服务。"""
    service, _, _ = make_service(db_path=db_path, start=start, tokens=tokens)
    return service


def station(
    event_id: str,
    station_id: str,
    area_id: str,
    category: str,
    at: str,
    status: str = "OUTAGE",
    x: float = 0.0,
    y: float = 0.0,
) -> dict:
    return {
        "event_id": event_id,
        "kind": "station_status",
        "occurred_at": at,
        "station_id": station_id,
        "status": status,
        "area": {
            "area_id": area_id,
            "name": area_id,
            "category": category,
            "x": x,
            "y": y,
        },
    }


def fiber(
    event_id: str, cable_id: str, area_id: str, category: str, at: str,
    status: str = "CUT", x: float = 0.0, y: float = 0.0,
) -> dict:
    return {
        "event_id": event_id,
        "kind": "fiber_status",
        "occurred_at": at,
        "cable_id": cable_id,
        "status": status,
        "area": {
            "area_id": area_id,
            "name": area_id,
            "category": category,
            "x": x,
            "y": y,
        },
    }


def sat_window(
    event_id: str,
    sat_id: str,
    start: str,
    end: str,
    capacity: int,
    at: str,
) -> dict:
    return {
        "event_id": event_id,
        "kind": "satellite_window",
        "occurred_at": at,
        "sat_id": sat_id,
        "window_start": start,
        "window_end": end,
        "capacity_mbps": capacity,
    }


def depot(event_id: str, depot_id: str, available: int, station_mbps: int, at: str) -> dict:
    return {
        "event_id": event_id,
        "kind": "portable_inventory",
        "occurred_at": at,
        "depot_id": depot_id,
        "available": available,
        "station_mbps": station_mbps,
    }


def team(event_id: str, team_id: str, x: float, y: float, at: str, status: str = "AVAILABLE") -> dict:
    return {
        "event_id": event_id,
        "kind": "repair_team",
        "occurred_at": at,
        "team_id": team_id,
        "status": status,
        "x": x,
        "y": y,
    }
