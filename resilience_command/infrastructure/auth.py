"""操作人注册：令牌鉴权与角色权限。

令牌配置来自外部文件或环境变量（JSON），不写入代码库。
角色权限：
- duty_officer（值班员）：freeze / withdraw / resume / reevaluate
- admin（管理员）：全部操作
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..domain.exceptions import ForbiddenError, UnauthorizedError

ROLE_PERMISSIONS = {
    "duty_officer": {"freeze", "withdraw", "resume", "reevaluate"},
    "admin": {"freeze", "withdraw", "resume", "reevaluate"},
}


class OperatorRegistry:
    def __init__(self, tokens: dict[str, dict[str, str]] | None = None) -> None:
        self._tokens = dict(tokens or {})

    @staticmethod
    def from_file(path: str) -> "OperatorRegistry":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return OperatorRegistry(data.get("tokens", data))

    @staticmethod
    def from_env(var: str = "RESILIENCE_OPERATOR_TOKENS") -> "OperatorRegistry":
        raw = os.environ.get(var)
        if not raw:
            return OperatorRegistry()
        return OperatorRegistry(json.loads(raw))

    def authenticate(self, token: str) -> dict[str, Any]:
        entry = self._tokens.get(token or "")
        if entry is None:
            raise UnauthorizedError("BAD_TOKEN", "令牌无效或已吊销")
        return entry

    def authorize(self, token: str, action: str) -> str:
        """校验令牌与操作权限，返回操作人姓名。"""
        entry = self.authenticate(token)
        role = entry.get("role", "duty_officer")
        if action not in ROLE_PERMISSIONS.get(role, set()):
            raise ForbiddenError(
                "NOT_PERMITTED", f"角色 {role} 无权执行 {action}"
            )
        return str(entry.get("name", "unknown"))
