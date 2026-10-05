"""中间件层（ENG-02 第 2 步）。

- `request_id`：请求 ID 的读取、生成、透传与响应头；
- `access_log`：已脱敏的结构化访问日志；
- `redaction`：日志脱敏工具；
- `idempotency`：`Idempotency-Key` 存在性与格式校验依赖。

装配顺序：`install_middleware()` 先加访问日志（内层）、后加请求 ID（最外层），
使请求 ID 覆盖包括错误响应在内的全部响应路径。
"""

from __future__ import annotations

from fastapi import FastAPI

from app.middleware.access_log import AccessLogMiddleware
from app.middleware.request_id import RequestIdMiddleware

__all__ = ["install_middleware"]


def install_middleware(app: FastAPI) -> None:
    """在应用上装配本卡第 2 步的中间件。"""

    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
