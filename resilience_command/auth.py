"""值班员授权。

演示与本地部署使用静态操作人名册（环境变量/文件可覆盖）。生产环境可替换
:class:`Authorizer` 端口对接统一身份系统。所有被授权操作都必须携带操作人，
该操作人会进入决策记录。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

# 角色能力矩阵
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "duty_director": {"approve", "freeze", "resume", "withdraw", "complete", "replan", "report"},
    "deputy_director": {"approve", "freeze", "resume", "withdraw", "replan", "report"},
    "operator": {"replan", "report"},   # 值班员可触发重排/回报，不可冻结/撤回/授权
    "viewer": set(),
}


class AuthorizationError(PermissionError):
    """操作人缺失或权限不足。"""


@dataclass(frozen=True)
class Operator:
    name: str
    role: str

    def can(self, action: str) -> bool:
        return action in ROLE_PERMISSIONS.get(self.role, set())


class Authorizer(Protocol):
    def resolve(self, token: str | None) -> Operator: ...
    def require(self, token: str | None, action: str) -> Operator: ...


class RosterAuthorizer:
    """基于 token -> 操作人名册的授权器。"""

    def __init__(self, roster: dict[str, Operator]) -> None:
        self._roster = dict(roster)

    @classmethod
    def from_file(cls, path: str) -> "RosterAuthorizer":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        roster = {token: Operator(name=item["name"], role=item["role"]) for token, item in data.items()}
        return cls(roster)

    @classmethod
    def default_local(cls) -> "RosterAuthorizer":
        """本地演练默认名册，可用环境变量覆盖路径。"""
        path = os.environ.get("RC_ROSTER_FILE")
        if path and os.path.exists(path):
            return cls.from_file(path)
        return cls({
            "director-token": Operator("张值班长", "duty_director"),
            "deputy-token": Operator("李副班", "deputy_director"),
            "operator-token": Operator("王值班员", "operator"),
        })

    def resolve(self, token: str | None) -> Operator:
        if not token or token not in self._roster:
            raise AuthorizationError("缺少或无效的授权令牌")
        return self._roster[token]

    def require(self, token: str | None, action: str) -> Operator:
        operator = self.resolve(token)
        if not operator.can(action):
            raise AuthorizationError(f"操作人 {operator.name}（{operator.role}）无权执行 {action}")
        return operator
