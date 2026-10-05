"""端口契约测试（ENG-03 第 1 步）。

验证裁决 ②③ 的端口行为：默认实现明确抛出依赖未就绪、不复制业务逻辑、
端口可被内存替身满足、Redis 适配器构造时不建立连接。
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from app.schemas.user import PointBalance, Session, User, UserStatus
from app.services.ports import (
    EmailPort,
    EmailPortUnavailable,
    InMemoryEmailSink,
    PointsPort,
    PointsPortUnavailable,
    RedeemOutcome,
    RedisClientAdapter,
    RedisPort,
    UnavailableEmailPort,
    UnavailablePointsPort,
)
from tests.services.fakes import FakeRedis

SESSION_TTL = 3600


# --------------------------------------------------------------------------- #
# Points 端口
# --------------------------------------------------------------------------- #
def test_unavailable_points_port_raises_dependency_error() -> None:
    port = UnavailablePointsPort()

    with pytest.raises(PointsPortUnavailable) as excinfo:
        port.redeem_invite(invite_code="AVTR-ABCD-EFGH", email="a@example.com",
                           session_ttl_seconds=SESSION_TTL)

    # 契约是异常类型（映射 `NOT_READY` / 503）；展示文本不参与判定
    assert "Points" in str(excinfo.value)


def test_unavailable_points_port_satisfies_protocol() -> None:
    assert isinstance(UnavailablePointsPort(), PointsPort)


def test_redeem_invite_is_keyword_only() -> None:
    """跨卡契约：`redeem_invite` 只接受关键字参数。"""

    signature = inspect.signature(PointsPort.redeem_invite)

    for name, parameter in signature.parameters.items():
        if name == "self":
            continue
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
    assert set(signature.parameters) == {
        "self",
        "invite_code",
        "email",
        "session_ttl_seconds",
    }


def test_unavailable_points_port_does_not_touch_database() -> None:
    """默认实现不得包含任何数据库或点数写入逻辑（只允许出现参数名）。"""

    body = inspect.getsource(UnavailablePointsPort.redeem_invite)

    for token in ("point_ledger", "point_wallets", "insert into", "update ", "session.add"):
        assert token not in body.lower()


# --------------------------------------------------------------------------- #
# 邮件端口
# --------------------------------------------------------------------------- #
def test_unavailable_email_port_raises_dependency_error() -> None:
    port = UnavailableEmailPort()

    with pytest.raises(EmailPortUnavailable):
        port.send_verification_email(
            to_email="a@example.com", token="synthetic-token", expires_in_seconds=600
        )


def test_email_ports_satisfy_protocol(email_sink: InMemoryEmailSink) -> None:
    assert isinstance(UnavailableEmailPort(), EmailPort)
    assert isinstance(email_sink, EmailPort)


def test_in_memory_email_sink_records_without_sending(email_sink: InMemoryEmailSink) -> None:
    email_sink.send_verification_email(
        to_email="a@example.com", token="synthetic-token", expires_in_seconds=600
    )
    email_sink.send_verification_email(
        to_email="b@example.com", token="another-token", expires_in_seconds=900
    )

    assert len(email_sink.sent) == 2
    assert email_sink.sent[0] == {
        "to_email": "a@example.com",
        "token": "synthetic-token",
        "expires_in_seconds": 600,
    }

    email_sink.clear()
    assert email_sink.sent == ()


def test_email_sink_is_not_used_as_default() -> None:
    """默认邮件端口必须是「未接入」实现，避免生产静默丢信。"""

    assert not isinstance(UnavailableEmailPort(), InMemoryEmailSink)


# --------------------------------------------------------------------------- #
# Redis 端口
# --------------------------------------------------------------------------- #
def test_fake_redis_satisfies_redis_port(fake_redis: FakeRedis) -> None:
    assert isinstance(fake_redis, RedisPort)


def test_redis_client_adapter_constructs_without_connecting() -> None:
    """构造适配器不得建立网络连接（本卡不启动 Redis）。"""

    adapter = RedisClientAdapter("redis://127.0.0.1:6399/0")

    assert isinstance(adapter, RedisPort)
    for method in ("set", "get", "delete", "expire", "ping"):
        assert callable(getattr(adapter, method))


def test_redis_port_exposes_only_documented_subset() -> None:
    for name in ("set", "get", "delete", "expire"):
        assert hasattr(RedisPort, name), name


# --------------------------------------------------------------------------- #
# RedeemOutcome
# --------------------------------------------------------------------------- #
def test_redeem_outcome_is_immutable_and_complete() -> None:
    outcome = RedeemOutcome(
        user=User(
            id=uuid.uuid4(),
            status=UserStatus.ACTIVE,
            locale="zh-CN",
            timezone="Asia/Shanghai",
            email_verified=True,
        ),
        points=PointBalance(
            available_points=100,
            reserved_points=0,
            lifetime_credited=100,
            lifetime_consumed=0,
            updated_at=datetime.now(timezone.utc),
        ),
        session=Session(
            session_id="a" * 64,
            user_id=uuid.uuid4(),
            email_verified=True,
            expires_at=datetime.now(timezone.utc),
        ),
        session_token="synthetic-token",
    )

    assert outcome.session_token == "synthetic-token"
    with pytest.raises(FrozenInstanceError):
        outcome.session_token = "mutated"  # type: ignore[misc]
