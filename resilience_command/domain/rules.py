"""保障规则编号与参数。规则编号会写入每条决策，供事后追溯。"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CATEGORY_HOSPITAL, CATEGORY_ORDINARY, CATEGORY_SHELTER

RULE_HOSPITAL = "R-HOSPITAL-GUARANTEE"  # 医院必须优先足额保障
RULE_SHELTER = "R-SHELTER-GUARANTEE"  # 避难点次优先保障
RULE_ORDINARY = "R-ORDINARY-BEST-EFFORT"  # 普通区域尽力而为
RULE_SATELLITE = "R-SATELLITE-WINDOW"  # 卫星容量仅在窗口期内可用
RULE_PORTABLE = "R-PORTABLE-DEPLOY"  # 便携站按库存补充回传能力
RULE_REPAIR = "R-REPAIR-DISPATCH"  # 物理损毁派遣最近可用抢修队
RULE_PREEMPT = "R-PREEMPT-CONSTRAINED"  # 仅更高优先级可抢占，冻结方案除外
RULE_FROZEN = "R-FROZEN-PIN"  # 冻结方案的资源被锁定，不参与重排
RULE_RESTORE = "R-RESTORE-RELEASE"  # 灾情恢复后释放资源并闭环方案
RULE_HOLD = "R-OPERATOR-HOLD"  # 撤回后区域挂起，不再自动编排
RULE_RESUME = "R-AREA-RESUME"  # 接续后区域恢复自动编排


@dataclass(frozen=True)
class RuleConfig:
    """各类区域的保障带宽（Mbps），可按演练需要调整。"""

    hospital_mbps: int = 100
    shelter_mbps: int = 50
    ordinary_mbps: int = 20

    def required_mbps(self, category: str) -> int:
        if category == CATEGORY_HOSPITAL:
            return self.hospital_mbps
        if category == CATEGORY_SHELTER:
            return self.shelter_mbps
        return self.ordinary_mbps

    def guarantee_rule(self, category: str) -> str:
        if category == CATEGORY_HOSPITAL:
            return RULE_HOSPITAL
        if category == CATEGORY_SHELTER:
            return RULE_SHELTER
        return RULE_ORDINARY
