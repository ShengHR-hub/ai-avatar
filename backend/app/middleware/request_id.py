"""请求 ID 的读取、生成、透传与响应头（ENG-02 第 2 步）。

唯一来源：`docs/architecture/03-api-contract.md` 第 2.1 节：

- 客户端可发送 `X-Request-Id`（ASCII，1-128 字符）；服务端校验通过后沿用，
  否则生成 `req_<uuid>`；
- 所有响应都返回顶层 `request_id`，并通过同名响应头返回。
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

__all__ = [
    "REQUEST_ID_HEADER",
    "REQUEST_ID_MAX_LENGTH",
    "REQUEST_ID_PREFIX",
    "RequestIdMiddleware",
    "generate_request_id",
    "is_valid_request_id",
    "request_id_of",
    "resolve_request_id",
]

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_PREFIX = "req_"
REQUEST_ID_MAX_LENGTH = 128


def generate_request_id() -> str:
    """生成服务端请求 ID：`req_<uuid4 hex>`。"""

    return f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex}"


def is_valid_request_id(raw: str | None) -> bool:
    """客户端提供的请求 ID 是否合法。

    契约要求 ASCII、1-128 字符。额外拒绝控制字符（HTTP 头值本身不允许出现），
    这是防御性校验，不改变契约取值范围。
    """

    if raw is None:
        return False
    if not raw.isascii():
        return False
    if not 1 <= len(raw) <= REQUEST_ID_MAX_LENGTH:
        return False
    return all(ch >= " " for ch in raw)


def resolve_request_id(raw: str | None) -> str:
    """合法则沿用客户端值，否则生成新的服务端请求 ID。"""

    return raw if is_valid_request_id(raw) else generate_request_id()


def request_id_of(request: Request) -> str:
    """读取当前请求的 request_id；中间件缺失时兜底生成。

    异常处理器可能运行在最外层中间件之外（未处理异常路径），因此必须能独立取值。
    """

    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    return generate_request_id()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """把请求 ID 写入 `request.state`，并在响应头回写同名头。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
