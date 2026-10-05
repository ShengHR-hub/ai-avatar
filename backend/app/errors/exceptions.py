"""应用异常类型（ENG-02 第 2 步）。

业务代码只允许通过 `ApiError` 抛出对外错误，异常必须携带契约第 7 节的稳定错误码。
禁止把异常文本、SQL、Provider 原始响应或密钥放入 `details`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.errors.codes import ErrorCode, default_message, http_status_for

__all__ = ["ApiError", "IdempotencyKeyInvalid", "IdempotencyKeyRequired"]


class ApiError(Exception):
    """携带稳定错误码的对外错误。

    - `code` 决定 HTTP 状态码（契约第 7 节固定映射，不允许逐路由改写）；
    - `message` 仅用于展示，缺省时使用错误码的默认文案；
    - `details` 只允许放安全的结构化信息，禁止堆栈、SQL、原始响应或密钥；
    - `headers` 用于附加协议头（例如 `429` 的 `Retry-After`）。
    """

    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message or default_message(code)
        self.details: dict[str, Any] = dict(details or {})
        self.headers: dict[str, str] = dict(headers or {})
        self.status_code = http_status_for(code)
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class IdempotencyKeyRequired(ApiError):
    """写请求缺少 `Idempotency-Key`（契约第 7 节 `IDEMPOTENCY_REQUIRED`）。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.IDEMPOTENCY_REQUIRED)


class IdempotencyKeyInvalid(ApiError):
    """`Idempotency-Key` 格式非法（契约第 7 节 `IDEMPOTENCY_INVALID`）。"""

    def __init__(self) -> None:
        super().__init__(ErrorCode.IDEMPOTENCY_INVALID)
