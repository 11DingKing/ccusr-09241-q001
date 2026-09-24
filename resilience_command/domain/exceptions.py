"""领域异常：接口层据此映射 HTTP 状态码。"""

from __future__ import annotations


class DomainError(Exception):
    """业务错误基类，携带稳定错误码与默认 HTTP 状态。"""

    http_status = 400

    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        if http_status is not None:
            self.http_status = http_status


class ValidationError(DomainError):
    http_status = 400


class UnauthorizedError(DomainError):
    http_status = 401


class ForbiddenError(DomainError):
    http_status = 403


class NotFoundError(DomainError):
    http_status = 404


class ConflictError(DomainError):
    http_status = 409
