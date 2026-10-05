"""统一异常映射与错误响应（ENG-02 第 2 步）。

覆盖四类异常：

1. `ApiError` —— 业务错误的唯一推荐路径，携带契约第 7 节的稳定错误码；
2. `RequestValidationError` —— Pydantic 请求校验失败 → `VALIDATION_ERROR`（422）；
3. `StarletteHTTPException` —— 带错误码的 `detail` 时按错误码映射；否则按状态码取
   契约中语义通用的错误码；
4. 未处理 `Exception` —— `INTERNAL_ERROR`（500），堆栈只进服务端诊断日志。

**契约缺口（本步记录，待技术负责人裁决）**：契约第 7 节没有为 `400`、`404`、`405`、
`409` 提供**语义通用**的错误码（404 只有 `ASSET_NOT_FOUND` / `GENERATION_NOT_FOUND`，
400 只有幂等键相关码）。为避免把框架级错误伪装成业务错误（例如把未知路径报成
「资源不存在」），这些状态保留原始 HTTP 状态并返回 `{"detail": ..., "request_id": ...}`，
不进入业务错误 envelope；`X-Request-Id` 响应头始终存在。
若需要统一，请在契约第 7 节增加通用码（如 `NOT_FOUND` / `BAD_REQUEST` /
`METHOD_NOT_ALLOWED`），届时只需扩展 `GENERIC_CODE_BY_STATUS`。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.errors.codes import (
    GENERIC_CODE_BY_STATUS,
    ErrorCode,
    default_message,
    http_status_for,
)
from app.errors.exceptions import ApiError
from app.middleware.request_id import REQUEST_ID_HEADER, request_id_of
from app.schemas.common import ErrorDetail, ErrorResponse

__all__ = [
    "ERROR_LOGGER_NAME",
    "build_error_response",
    "install_exception_handlers",
    "sanitize_validation_errors",
]

#: 受控服务端诊断日志名（异常堆栈只写入这里）
ERROR_LOGGER_NAME = "app.errors"

#: 幂等 Header 在校验错误 `loc` 中的小写名（FastAPI 统一小写化）
_IDEMPOTENCY_HEADER_LOCATION = "idempotency-key"

logger = logging.getLogger(ERROR_LOGGER_NAME)


def build_error_response(
    request: Request,
    code: ErrorCode,
    *,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """按契约第 2.3 节构造错误响应，并回写 `X-Request-Id`。"""

    request_id = request_id_of(request)
    request.state.error_code = code.value
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message or default_message(code),
            details=dict(details or {}),
        ),
        request_id=request_id,
    )
    response_headers = dict(headers or {})
    response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=http_status_for(code),
        content=payload.model_dump(mode="json"),
        headers=response_headers,
    )


def sanitize_validation_errors(
    errors: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """只保留字段路径、错误类型与说明。

    Pydantic 的错误项还包含 `input`、`ctx`、`url` 等字段，其中 `input` 会回显原始输入值
    （例如邀请码明文、邮箱），因此必须丢弃，避免敏感数据进入响应与日志。
    """

    sanitized: list[dict[str, Any]] = []
    for error in errors:
        item: dict[str, Any] = {}
        location = error.get("loc")
        if isinstance(location, (list, tuple)):
            item["loc"] = [str(part) for part in location]
        item["type"] = str(error.get("type", ""))
        item["msg"] = str(error.get("msg", ""))
        sanitized.append(item)
    return sanitized


def _code_from_detail(detail: Any) -> tuple[ErrorCode, str | None, dict[str, Any]] | None:
    """从 `HTTPException.detail` 中解析契约错误码（可选携带 message / details）。"""

    if not isinstance(detail, Mapping):
        return None
    raw_code = detail.get("code")
    if not isinstance(raw_code, str):
        return None
    try:
        code = ErrorCode(raw_code)
    except ValueError:
        return None
    raw_message = detail.get("message")
    message = raw_message if isinstance(raw_message, str) else None
    raw_details = detail.get("details")
    details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
    return code, message, details


async def _handle_api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return build_error_response(
        request,
        exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


def _idempotency_error_code(errors: Sequence[Mapping[str, Any]]) -> ErrorCode | None:
    """识别幂等 Header 的校验错误，返回应映射的 400 错误码。

    契约第 4 节：缺少 Header → `IDEMPOTENCY_REQUIRED`；空白、超长或格式非法 →
    `IDEMPOTENCY_INVALID`。OpenAPI 中仍保留 Header 参数约束，不把这些错误变成 422。
    """

    for error in errors:
        location = error.get("loc")
        if not isinstance(location, (list, tuple)) or len(location) != 2:
            continue
        if str(location[0]) != "header":
            continue
        if str(location[1]).lower() != _IDEMPOTENCY_HEADER_LOCATION:
            continue
        if str(error.get("type", "")) == "missing":
            return ErrorCode.IDEMPOTENCY_REQUIRED
        return ErrorCode.IDEMPOTENCY_INVALID
    return None


async def _handle_request_validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)

    # 幂等 Header 的校验错误映射为 400（技术负责人 2026-09-26 裁决 ④）：
    # 缺少 Header → IDEMPOTENCY_REQUIRED；空白/超长/非 ASCII → IDEMPOTENCY_INVALID。
    idempotency_code = _idempotency_error_code(exc.errors())
    if idempotency_code is not None:
        return build_error_response(request, idempotency_code)

    return build_error_response(
        request,
        ErrorCode.VALIDATION_ERROR,
        details={"fields": sanitize_validation_errors(exc.errors())},
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)

    parsed = _code_from_detail(exc.detail)
    if parsed is not None:
        code, message, details = parsed
        return build_error_response(
            request, code, message=message, details=details, headers=exc.headers
        )

    generic_code = GENERIC_CODE_BY_STATUS.get(exc.status_code)
    if generic_code is not None:
        return build_error_response(request, generic_code, headers=exc.headers)

    return _framework_fallback_response(request, exc)


def _framework_fallback_response(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """契约未提供通用错误码的框架级状态：保留原始状态，不伪造业务错误码。"""

    request_id = request_id_of(request)
    logger.warning(
        "http_exception_without_contract_code",
        extra={
            "event": "http_exception_without_contract_code",
            "request_id": request_id,
            "status": exc.status_code,
            "path": request.url.path,
        },
    )
    response_headers = dict(exc.headers or {})
    response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": request_id},
        headers=response_headers,
    )


async def _handle_unexpected_exception(request: Request, exc: Exception) -> Response:
    request_id = request_id_of(request)
    # 堆栈只进入受控服务端诊断日志，用户响应只返回稳定错误码
    logger.exception(
        "unhandled_exception",
        extra={
            "event": "unhandled_exception",
            "request_id": request_id,
            "path": request.url.path,
            "exception_type": type(exc).__name__,
        },
    )
    return build_error_response(request, ErrorCode.INTERNAL_ERROR)


def install_exception_handlers(app: FastAPI) -> None:
    """在应用上注册统一异常处理器。"""

    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_exception)
