"""纯函数规划器：由事实集合计算每个区域应有的资源分配。

规划器不读写任何外部状态，输出只取决于输入，因此：
- 乱序/迟到上报折叠成同一事实集合后，规划结果必然一致；
- 注入不同时钟即可稳定复现窗口过期等时间敏感行为。

抢占通过"按优先级贪心分配 + 冻结资源预先锁定"实现：
高优先级需求先取资源，低优先级自然让位（在决策差异中体现为 PREEMPTED），
而被冻结方案锁定的资源在分配前就被保留，任何自动重排都无法触碰。
"""

from __future__ import annotations

from datetime import datetime

from .models import (
    ALLOC_PORTABLE,
    ALLOC_REPAIR,
    ALLOC_SATELLITE,
    CATEGORY_RANK,
    STATUS_CUT,
    STATUS_OUTAGE,
    TEAM_AVAILABLE,
    Allocation,
    AreaInfo,
    AreaPlan,
    Demand,
    Facts,
)
from .rules import (
    RULE_FROZEN,
    RULE_PORTABLE,
    RULE_REPAIR,
    RULE_SATELLITE,
    RuleConfig,
)


def derive_demands(
    facts: Facts, registry: dict[str, AreaInfo], config: RuleConfig
) -> dict[str, Demand]:
    """由未恢复的基站/光缆损毁推导各区域保障需求。"""
    damaged: dict[str, list] = {}
    for report in facts.stations.values():
        if report.payload["status"] == STATUS_OUTAGE:
            damaged.setdefault(report.payload["area"]["area_id"], []).append(report)
    for report in facts.fibers.values():
        if report.payload["status"] == STATUS_CUT:
            damaged.setdefault(report.payload["area"]["area_id"], []).append(report)

    demands: dict[str, Demand] = {}
    for area_id, reports in damaged.items():
        info = registry.get(area_id)
        category = info.category if info else "ORDINARY"
        first = min(r.occurred_at for r in reports)
        demands[area_id] = Demand(
            area_id=area_id,
            category=category,
            required_mbps=config.required_mbps(category),
            needs_repair=True,
            order_key=(first, area_id),
            sources=tuple(sorted(r.source_key for r in reports)),
        )
    return demands


def plan_all(
    facts: Facts,
    registry: dict[str, AreaInfo],
    pinned: tuple[Allocation, ...],
    held_areas: frozenset[str],
    now: datetime,
    config: RuleConfig,
) -> dict[str, AreaPlan]:
    """计算所有未挂起需求区域的目标分配。

    ``pinned`` 是冻结方案锁定的分配，先从资源池中扣除，再按
    (类别优先级, 灾情发生时间, 区域号) 的顺序贪心分配剩余资源。
    """
    demands = derive_demands(facts, registry, config)
    ordered = sorted(
        (d for d in demands.values() if d.area_id not in held_areas),
        key=lambda d: (CATEGORY_RANK[d.category], d.order_key),
    )

    pinned_by_area: dict[str, list[Allocation]] = {}
    for alloc in pinned:
        pinned_by_area.setdefault(alloc.area_id, []).append(alloc)

    # 卫星窗口：未过期且有剩余容量者可用，按 (窗口结束, 卫星号) 消耗。
    window_remaining: dict[str, int] = {}
    window_bounds: dict[str, tuple[datetime, datetime]] = {}
    for source_key, report in facts.windows.items():
        payload = report.payload
        if payload["window_end"] > now and payload["capacity_mbps"] > 0:
            window_remaining[source_key] = payload["capacity_mbps"]
            window_bounds[source_key] = (payload["window_start"], payload["window_end"])
    # 便携站库存：按仓库扣减。
    depot_remaining: dict[str, int] = {
        key: report.payload["available"] for key, report in facts.depots.items()
    }
    depot_capacity = {
        key: report.payload["station_mbps"] for key, report in facts.depots.items()
    }
    # 抢修队：仅空闲可派。
    team_available: dict[str, tuple[float, float]] = {
        key: (report.payload["x"], report.payload["y"])
        for key, report in facts.teams.items()
        if report.payload["status"] == TEAM_AVAILABLE
    }

    for alloc in pinned:
        if alloc.kind == ALLOC_SATELLITE:
            key = f"satwin:{alloc.resource_ref}"
            if key in window_remaining:
                window_remaining[key] = max(0, window_remaining[key] - alloc.capacity_mbps)
        elif alloc.kind == ALLOC_PORTABLE:
            key = f"depot:{alloc.resource_ref}"
            if key in depot_remaining:
                depot_remaining[key] = max(0, depot_remaining[key] - alloc.units)
        elif alloc.kind == ALLOC_REPAIR:
            team_available.pop(f"team:{alloc.resource_ref}", None)

    result: dict[str, AreaPlan] = {}
    for demand in ordered:
        area_id = demand.area_id
        rules: list[str] = [config.guarantee_rule(demand.category)]
        allocations: list[Allocation] = []
        area_pinned = pinned_by_area.get(area_id, [])
        if area_pinned:
            rules.append(RULE_FROZEN)
        residual = demand.required_mbps - sum(
            a.capacity_mbps for a in area_pinned if a.kind != ALLOC_REPAIR
        )

        # 抢修队：有物理损毁且未被冻结方案覆盖时，派最近的空闲队伍。
        if demand.needs_repair and not any(a.kind == ALLOC_REPAIR for a in area_pinned):
            info = registry.get(area_id)
            if team_available and info is not None:
                team_key = min(
                    team_available,
                    key=lambda k: (
                        (team_available[k][0] - info.x) ** 2
                        + (team_available[k][1] - info.y) ** 2,
                        k,
                    ),
                )
                team_id = team_key.split(":", 1)[1]
                allocations.append(
                    Allocation(area_id, ALLOC_REPAIR, team_id, capacity_mbps=0)
                )
                team_available.pop(team_key)
                rules.append(RULE_REPAIR)

        # 卫星容量：允许跨窗口拆分，窗口早结束者先用。
        if residual > 0:
            for key in sorted(
                window_remaining,
                key=lambda k: (window_bounds[k][1], k),
            ):
                if residual <= 0:
                    break
                remaining = window_remaining[key]
                if remaining <= 0:
                    continue
                take = min(remaining, residual)
                start, end = window_bounds[key]
                allocations.append(
                    Allocation(
                        area_id,
                        ALLOC_SATELLITE,
                        key.split(":", 1)[1],
                        capacity_mbps=take,
                        window_start=start,
                        window_end=end,
                    )
                )
                window_remaining[key] = remaining - take
                residual -= take
            if any(a.kind == ALLOC_SATELLITE for a in allocations):
                rules.append(RULE_SATELLITE)

        # 便携站：按仓库顺序补足剩余缺口。
        if residual > 0:
            used_portable = False
            for key in sorted(depot_remaining):
                while residual > 0 and depot_remaining[key] > 0:
                    capacity = depot_capacity[key]
                    allocations.append(
                        Allocation(
                            area_id,
                            ALLOC_PORTABLE,
                            key.split(":", 1)[1],
                            capacity_mbps=capacity,
                            units=1,
                        )
                    )
                    depot_remaining[key] -= 1
                    residual -= capacity
                    used_portable = True
            if used_portable:
                rules.append(RULE_PORTABLE)

        result[area_id] = AreaPlan(
            area_id=area_id,
            demand=demand,
            allocations=tuple(allocations),
            unmet_mbps=max(0, residual),
            rules=tuple(rules),
        )
    return result
