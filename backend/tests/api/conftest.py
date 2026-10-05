"""ENG-02 API 层测试夹具。

设计要点：

- 探针路由只存在于测试构造的独立应用实例（`create_app()` 每次返回新实例），
  生产应用仍然没有任何业务路由；
- 因此测试同时覆盖「真实装配」（中间件与异常处理器由 `create_app()` 注册）
  与「探针行为」（触发各类异常与校验路径）。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterable, Iterator, Sequence

# `create_app()` 读取服务端配置且 DATABASE_URL 为 fail-fast 必填项；
# 在导入应用之前注入占位连接串，测试不依赖真实数据库。
PLACEHOLDER_DATABASE_URL = (
    "postgresql+psycopg://ai_avatar:placeholder@postgres:5432/ai_avatar"
)
os.environ.setdefault("DATABASE_URL", PLACEHOLDER_DATABASE_URL)

import pytest  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.api.health.checks import DependencyCheck, get_dependency_checks  # noqa: E402
from app.config import reset_settings_cache  # noqa: E402
from app.errors.codes import ErrorCode  # noqa: E402
from app.errors.exceptions import ApiError  # noqa: E402
from app.main import create_app  # noqa: E402
from app.middleware.idempotency import require_idempotency_key  # noqa: E402
from app.middleware.request_id import request_id_of  # noqa: E402
from app.schemas.common import Envelope, PageEnvelope, PageMeta  # noqa: E402

#: 用于验证校验错误不回显原始输入值
SENSITIVE_INPUT_SENTINEL = "SENTINEL-INVITE-CODE-DO-NOT-ECHO-0123456789"


RawResponse = tuple[int, dict[str, str], bytes]


def call_app_raw(
    app: FastAPI,
    path: str,
    *,
    method: str = "GET",
    headers: Iterable[tuple[bytes, bytes]] = (),
    body: bytes = b"",
) -> RawResponse:
    """以 ASGI 协议直接调用应用。

    httpx 拒绝发送非 ASCII 请求头（`UnicodeEncodeError`），因此「非 ASCII 头」这一契约
    场景必须绕过 HTTP 客户端，在 ASGI 层构造原始字节头来验证。
    """

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    received: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        received.append(message)

    asyncio.run(app(scope, receive, send))

    status = 0
    response_headers: dict[str, str] = {}
    response_body = b""
    for message in received:
        if message["type"] == "http.response.start":
            status = message["status"]
            response_headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            response_body += message.get("body", b"")
    return status, response_headers, response_body


@pytest.fixture
def call_raw() -> Callable[..., RawResponse]:
    """返回 ASGI 层直调助手（用于非 ASCII 请求头等 httpx 无法构造的场景）。"""

    return call_app_raw


def parse_body(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


class ProbePayload(BaseModel):
    """探针请求体：`min_length` 故意大于哨兵长度，便于制造校验失败。"""

    invite_code: str = Field(min_length=200)


@pytest.fixture
def sensitive_input_sentinel() -> str:
    """用于断言「校验错误不回显原始输入值」的哨兵字符串。"""

    return SENSITIVE_INPUT_SENTINEL


@pytest.fixture
def probe_app() -> FastAPI:
    """构造带探针路由的独立应用实例。"""

    app = create_app()

    @app.get("/probe/echo-request-id")
    def echo_request_id(request: Request) -> dict[str, str]:
        return {"request_id": request.state.request_id}

    @app.get("/probe/envelope")
    def envelope(request: Request) -> Envelope:
        return Envelope(data={"probe": True}, request_id=request_id_of(request))

    @app.get("/probe/paginated")
    def paginated(request: Request) -> PageEnvelope:
        return PageEnvelope(
            data=[{"id": "synthetic-1"}],
            page=PageMeta(next_cursor="opaque-cursor", has_more=True),
            request_id=request_id_of(request),
        )

    @app.get("/probe/api-error")
    def api_error() -> None:
        raise ApiError(
            ErrorCode.INSUFFICIENT_POINTS, details={"available_points": 0}
        )

    @app.get("/probe/api-error-with-headers")
    def api_error_with_headers() -> None:
        raise ApiError(ErrorCode.RATE_LIMITED, headers={"Retry-After": "30"})

    @app.get("/probe/http-exception-with-code")
    def http_exception_with_code() -> None:
        raise HTTPException(status_code=500, detail={"code": "GENERATION_NOT_FOUND"})

    @app.get("/probe/http-exception-plain")
    def http_exception_plain() -> None:
        raise HTTPException(status_code=409, detail="plain framework detail")

    @app.get("/probe/http-exception-401")
    def http_exception_401() -> None:
        raise HTTPException(status_code=401, detail="not authenticated")

    @app.post("/probe/validated")
    def validated(payload: ProbePayload) -> dict[str, str]:
        return {"invite_code": payload.invite_code}

    @app.post("/probe/idempotent")
    def idempotent(key: str = Depends(require_idempotency_key)) -> dict[str, str]:
        return {"idempotency_key": key}

    @app.get("/probe/boom")
    def boom() -> None:
        raise RuntimeError(f"internal detail must not leak: {SENSITIVE_INPUT_SENTINEL}")

    return app


@pytest.fixture
def client(probe_app: FastAPI) -> Iterator[TestClient]:
    """探针客户端；`raise_server_exceptions=False` 以便断言 500 处理器输出。"""

    with TestClient(probe_app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def override_checks(probe_app: FastAPI) -> Iterator[Callable[[Sequence[DependencyCheck]], FastAPI]]:
    """替换 `/ready` 使用的依赖检查集合。"""

    def _override(checks: Sequence[DependencyCheck]) -> FastAPI:
        probe_app.dependency_overrides[get_dependency_checks] = lambda: tuple(checks)
        return probe_app

    yield _override
    probe_app.dependency_overrides.clear()


@pytest.fixture
def make_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., FastAPI]]:
    """按临时环境变量构造应用（用于 `APP_ENV` 相关行为）。

    `create_app()` 会缓存服务端配置，因此每次构造前重置缓存；夹具结束后再次重置，
    避免影响其他用例。
    """

    def _make_app(**env: str) -> FastAPI:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings_cache()
        return create_app()

    yield _make_app
    reset_settings_cache()
