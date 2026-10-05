"""结构化访问日志（ENG-02 第 2 步，已脱敏）。

依据 `docs/architecture/05-system-architecture.md` 第 8 节，日志为结构化字段，
至少包含 `request_id`、可用时的 `user_id` / `generation_id`、`event`、`duration_ms`
和稳定 `error_code`。

本中间件**只记录安全字段**：方法、路径（不含查询串）、状态码、耗时、请求 ID、
稳定错误码。绝不记录请求头、Cookie、请求体、响应体或查询参数。
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.middleware.redaction import redact_url
from app.middleware.request_id import request_id_of

__all__ = ["ACCESS_LOGGER_NAME", "AccessLogMiddleware"]

ACCESS_LOGGER_NAME = "app.access"

logger = logging.getLogger(ACCESS_LOGGER_NAME)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """记录每个请求的结构化访问日志。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 未处理异常由 `app.errors.handlers` 兜底；此处只记录耗时与事件，
            # 异常堆栈由异常处理器写入受控诊断日志，避免重复与泄露。
            logger.warning(
                "http_request_failed",
                extra={
                    "event": "http_request_failed",
                    "request_id": request_id_of(request),
                    "method": request.method,
                    "path": redact_url(request.url.path),
                    "duration_ms": _elapsed_ms(started),
                },
            )
            raise

        logger.info(
            "http_request",
            extra={
                "event": "http_request",
                "request_id": request_id_of(request),
                "method": request.method,
                "path": redact_url(request.url.path),
                "status": response.status_code,
                "duration_ms": _elapsed_ms(started),
                "error_code": getattr(request.state, "error_code", None),
            },
        )
        return response


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
