"""邀请码兑换编排与端口提供者测试（ENG-03 第 2 步 / ENG-04 第 2 步接线）。"""

from __future__ import annotations

import inspect
from collections.abc import Iterator

import pytest
import redis

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.services.auth.redeem import RedeemService
from app.services.auth.session_store import SESSION_TTL_SECONDS
from app.services.ports import (
    PointsPort,
    RedisClientAdapter,
    TransactionalPointsPort,
    UnavailableEmailPort,
    UnavailablePointsPort,
    get_email_port,
    get_points_port,
    get_redis_port,
    reset_port_providers,
)
from tests.services.fakes import FakePointsPort

#: 占位连接串：单元测试不启动 PostgreSQL / Redis，只要求配置可读
PLACEHOLDER_DATABASE_URL = (
    "postgresql+psycopg://ai_avatar:placeholder@postgres:5432/ai_avatar"
)
CLOSED_REDIS_URL = "redis://127.0.0.1:6399/0"


@pytest.fixture(autouse=True)
def _isolate_port_cache() -> Iterator[None]:
    """端口提供者是进程级缓存；用例结束后清空，避免真实默认端口泄漏到其他模块。"""

    yield
    reset_port_providers()


# --------------------------------------------------------------------------- #
# 兑换编排
# --------------------------------------------------------------------------- #
def test_redeem_delegates_to_points_port(
    redeem_service: RedeemService, points_port: FakePointsPort
) -> None:
    outcome = redeem_service.redeem(invite_code="AVTR-ABCD-EFGH", email="user@example.com")

    assert points_port.calls == [
        {
            "invite_code": "AVTR-ABCD-EFGH",
            "email": "user@example.com",
            "session_ttl_seconds": SESSION_TTL_SECONDS,
        }
    ]
    assert outcome.session_token
    assert outcome.points.available_points > 0
    assert outcome.user.id == outcome.session.user_id


def test_redeem_passes_custom_session_ttl(points_port: FakePointsPort) -> None:
    service = RedeemService(points_port=points_port, session_ttl_seconds=600)

    service.redeem(invite_code="AVTR-ABCD-EFGH", email="user@example.com")

    assert points_port.calls[0]["session_ttl_seconds"] == 600
    assert service.session_ttl_seconds == 600


def test_redeem_maps_unavailable_port_to_not_ready() -> None:
    service = RedeemService(points_port=UnavailablePointsPort())

    with pytest.raises(ApiError) as excinfo:
        service.redeem(invite_code="AVTR-ABCD-EFGH", email="user@example.com")

    assert excinfo.value.code is ErrorCode.NOT_READY
    assert excinfo.value.status_code == 503


def test_redeem_service_has_no_database_or_points_logic() -> None:
    """裁决 ②：ENG-03 不得直接改钱包/流水，也不得复制 Points 逻辑。"""

    source = inspect.getsource(RedeemService)

    for token in ("point_ledger", "point_wallets", "commit(", "Session(", "insert", "update "):
        assert token not in source, token


def test_redeem_does_not_swallow_other_errors() -> None:
    boom = RuntimeError("unexpected")
    service = RedeemService(points_port=FakePointsPort(fail_with=boom))

    with pytest.raises(RuntimeError):
        service.redeem(invite_code="AVTR-ABCD-EFGH", email="user@example.com")


# --------------------------------------------------------------------------- #
# 端口提供者默认行为（ENG-04 第 2 步：Points 端口默认已接入真实实现）
# --------------------------------------------------------------------------- #
def test_default_points_port_is_transactional(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENG-04 第 2 步：`get_points_port()` 默认返回真实端口，不再抛未接入。"""

    monkeypatch.setenv("DATABASE_URL", PLACEHOLDER_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", CLOSED_REDIS_URL)
    reset_port_providers()

    port = get_points_port()

    assert isinstance(port, TransactionalPointsPort)
    assert not isinstance(port, UnavailablePointsPort)
    assert isinstance(port, PointsPort)
    assert port is get_points_port()  # 进程级缓存


def test_default_points_port_is_also_the_route_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/auth/invites/redeem` 的默认依赖必须解析到同一个真实端口。"""

    from app.api.deps import get_points_port_dep

    monkeypatch.setenv("DATABASE_URL", PLACEHOLDER_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", CLOSED_REDIS_URL)
    reset_port_providers()

    assert isinstance(get_points_port_dep(), TransactionalPointsPort)
    assert get_points_port_dep() is get_points_port()


def test_default_points_port_assembly_creates_no_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装配默认端口不得创建 SQLAlchemy 引擎（即不得触碰 PostgreSQL）。"""

    import app.db.session as db_session

    monkeypatch.setenv("DATABASE_URL", PLACEHOLDER_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", CLOSED_REDIS_URL)
    reset_port_providers()

    def forbid_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("装配默认 PointsPort 时不得创建数据库引擎。")

    monkeypatch.setattr(db_session, "get_engine", forbid_engine)

    assert isinstance(get_points_port(), TransactionalPointsPort)


def test_default_points_port_assembly_issues_no_redis_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装配默认端口不得发出任何 Redis 命令（会话存储只持有客户端引用）。"""

    issued: list[str] = []

    class _ExplodingClient:
        def __getattr__(self, name: str):
            def command(*args: object, **kwargs: object) -> None:
                issued.append(name)
                raise AssertionError(f"装配默认 PointsPort 时不得执行 Redis 命令 {name}。")

            return command

    monkeypatch.setenv("DATABASE_URL", PLACEHOLDER_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", CLOSED_REDIS_URL)
    reset_port_providers()
    monkeypatch.setattr(
        redis.Redis, "from_url", staticmethod(lambda url, **kwargs: _ExplodingClient())
    )

    port = get_points_port()

    assert isinstance(port, TransactionalPointsPort)
    assert issued == []
    assert isinstance(get_redis_port(), RedisClientAdapter)


def test_default_points_port_redeem_opens_real_unit_of_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认端口的 `redeem_invite()` 必须真正开启工作单元，而不是抛未接入。"""

    import app.db.session as db_session

    opened: list[str] = []

    monkeypatch.setenv("DATABASE_URL", PLACEHOLDER_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", CLOSED_REDIS_URL)
    reset_port_providers()

    def record_and_fail(*args: object, **kwargs: object) -> None:
        opened.append("engine")
        raise RuntimeError("单元测试不提供数据库")

    monkeypatch.setattr(db_session, "get_engine", record_and_fail)
    port = get_points_port()

    with pytest.raises(RuntimeError):
        port.redeem_invite(
            invite_code="AVTR-ABCD-EFGH", email="a@example.com", session_ttl_seconds=600
        )

    assert opened == ["engine"]
    assert isinstance(port, TransactionalPointsPort)


def test_unavailable_points_port_can_still_be_injected_explicitly() -> None:
    """保留显式注入能力：路由/Service 注入 `UnavailablePointsPort` 仍映射 503。"""

    service = RedeemService(points_port=UnavailablePointsPort())

    with pytest.raises(ApiError) as excinfo:
        service.redeem(invite_code="AVTR-ABCD-EFGH", email="user@example.com")

    assert excinfo.value.code is ErrorCode.NOT_READY
    assert excinfo.value.status_code == 503


def test_default_email_port_is_unavailable() -> None:
    """不得静默丢信：默认邮件端口必须抛依赖未就绪。"""

    reset_port_providers()

    assert isinstance(get_email_port(), UnavailableEmailPort)


def test_default_redis_port_is_lazy_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://ai_avatar:placeholder@postgres:5432/ai_avatar"
    )
    reset_port_providers()

    port = get_redis_port()

    assert isinstance(port, RedisClientAdapter)
    assert port is get_redis_port()  # 进程级缓存


def test_reset_port_providers_clears_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://ai_avatar:placeholder@postgres:5432/ai_avatar"
    )
    reset_port_providers()
    first = get_points_port()

    reset_port_providers()

    assert get_points_port() is not first
