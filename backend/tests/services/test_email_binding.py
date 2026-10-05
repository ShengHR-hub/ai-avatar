"""邮箱绑定与验证 Service 测试（ENG-03 第 2 步）。"""

from __future__ import annotations

import pytest

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.services.auth.email_tokens import (
    EMAIL_TOKEN_KEY_NAMESPACE,
    EMAIL_TOKEN_TTL_SECONDS,
    EmailTokenStore,
)
from app.services.ports import InMemoryEmailSink
from tests.services.fakes import FakeRedis, InMemoryUnitOfWork, MutableClock, make_user_row


# --------------------------------------------------------------------------- #
# 令牌存储
# --------------------------------------------------------------------------- #
def test_email_token_ttl_default_and_key_namespace(
    fake_redis: FakeRedis, session_secret: str, clock: MutableClock
) -> None:
    store = EmailTokenStore(fake_redis, secret=session_secret)
    token, record = store.issue(user_id=make_user_row().id, email="a@example.com", now=clock())

    assert store.ttl_seconds == EMAIL_TOKEN_TTL_SECONDS
    assert len(fake_redis.keys()) == 1
    key = fake_redis.keys()[0]
    assert key.startswith(f"{EMAIL_TOKEN_KEY_NAMESPACE}:")
    assert token not in key
    assert token not in "".join(fake_redis.raw_values())
    assert record.token_digest in key


def test_email_token_expires(
    fake_redis: FakeRedis, session_secret: str, clock: MutableClock
) -> None:
    store = EmailTokenStore(fake_redis, secret=session_secret, ttl_seconds=60)
    token, _ = store.issue(user_id=make_user_row().id, email="a@example.com", now=clock())

    clock.advance(61)

    assert store.read(token) is None
    assert store.consume(token) is None


def test_email_token_is_single_use(
    fake_redis: FakeRedis, session_secret: str, clock: MutableClock
) -> None:
    store = EmailTokenStore(fake_redis, secret=session_secret)
    token, record = store.issue(user_id=make_user_row().id, email="a@example.com", now=clock())

    assert store.consume(token) is not None
    assert store.consume(token) is None
    assert fake_redis.get(f"{EMAIL_TOKEN_KEY_NAMESPACE}:{record.token_digest}") is None


# --------------------------------------------------------------------------- #
# 请求验证（防枚举）
# --------------------------------------------------------------------------- #
def test_request_verification_sends_email(
    email_binding, email_sink: InMemoryEmailSink, seeded_user
) -> None:
    email_binding.request_verification(user_id=seeded_user.id, email="New@Example.com")

    assert len(email_sink.sent) == 1
    sent = email_sink.sent[0]
    assert sent["to_email"] == "New@Example.com"
    assert sent["expires_in_seconds"] == email_binding.token_ttl_seconds
    assert sent["token"]


def test_request_verification_is_silent_when_email_belongs_to_another_user(
    email_binding, email_sink: InMemoryEmailSink, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    """防枚举：邮箱已被他人占用时静默成功且不发送邮件。"""

    other = uow.users.seed(make_user_row())
    uow.emails.add(user_id=other.id, email="taken@example.com")

    email_binding.request_verification(user_id=seeded_user.id, email="taken@example.com")

    assert email_sink.sent == ()


def test_request_verification_allows_resending_to_own_email(
    email_binding, email_sink: InMemoryEmailSink, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    uow.emails.add(user_id=seeded_user.id, email="mine@example.com")

    email_binding.request_verification(user_id=seeded_user.id, email="mine@example.com")

    assert len(email_sink.sent) == 1


# --------------------------------------------------------------------------- #
# 确认验证
# --------------------------------------------------------------------------- #
def test_verify_binds_email_as_primary(
    email_binding, email_sink: InMemoryEmailSink, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    email_binding.request_verification(user_id=seeded_user.id, email="user@example.com")
    token = email_sink.sent[0]["token"]

    user = email_binding.verify(token=token)

    assert user.primary_email == "user@example.com"
    assert user.email_verified is True
    assert uow.commits == 1


def test_verify_marks_existing_unverified_row(
    email_binding, email_sink: InMemoryEmailSink, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    row = uow.emails.add(user_id=seeded_user.id, email="user@example.com", is_primary=True)

    email_binding.request_verification(user_id=seeded_user.id, email="user@example.com")
    email_binding.verify(token=email_sink.sent[0]["token"])

    assert row.verified_at is not None


def test_verify_rejects_unknown_or_expired_token(
    email_binding, email_sink: InMemoryEmailSink, seeded_user
) -> None:
    with pytest.raises(ApiError) as excinfo:
        email_binding.verify(token="not-a-real-token")

    assert excinfo.value.code is ErrorCode.EMAIL_TOKEN_INVALID
    assert excinfo.value.status_code == 422


def test_verify_rejects_token_used_twice(
    email_binding, email_sink: InMemoryEmailSink, seeded_user
) -> None:
    email_binding.request_verification(user_id=seeded_user.id, email="user@example.com")
    token = email_sink.sent[0]["token"]
    email_binding.verify(token=token)

    with pytest.raises(ApiError) as excinfo:
        email_binding.verify(token=token)

    assert excinfo.value.code is ErrorCode.EMAIL_TOKEN_INVALID


def test_verify_rejects_email_in_use_by_another_user(
    email_binding, email_sink: InMemoryEmailSink, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    email_binding.request_verification(user_id=seeded_user.id, email="user@example.com")
    token = email_sink.sent[0]["token"]

    other = uow.users.seed(make_user_row())
    uow.emails.add(user_id=other.id, email="user@example.com")

    with pytest.raises(ApiError) as excinfo:
        email_binding.verify(token=token)

    assert excinfo.value.code is ErrorCode.EMAIL_IN_USE
    assert excinfo.value.status_code == 409


def test_verify_rejects_already_verified_email(
    email_binding, email_sink: InMemoryEmailSink, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    from datetime import datetime, timezone

    row = uow.emails.add(user_id=seeded_user.id, email="user@example.com", is_primary=True)
    uow.emails.mark_verified(row, datetime.now(timezone.utc))

    email_binding.request_verification(user_id=seeded_user.id, email="user@example.com")
    token = email_sink.sent[0]["token"]

    with pytest.raises(ApiError) as excinfo:
        email_binding.verify(token=token)

    assert excinfo.value.code is ErrorCode.EMAIL_ALREADY_VERIFIED
    assert excinfo.value.status_code == 409


def test_verify_rejects_missing_user(
    email_binding, email_sink: InMemoryEmailSink, uow: InMemoryUnitOfWork
) -> None:
    ghost = make_user_row()
    email_binding.request_verification(user_id=ghost.id, email="ghost@example.com")
    token = email_sink.sent[0]["token"]

    with pytest.raises(ApiError) as excinfo:
        email_binding.verify(token=token)

    assert excinfo.value.code is ErrorCode.SESSION_INVALID


def test_verify_does_not_commit_on_failure(
    email_binding, email_sink: InMemoryEmailSink, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    email_binding.request_verification(user_id=seeded_user.id, email="user@example.com")
    token = email_sink.sent[0]["token"]
    other = uow.users.seed(make_user_row())
    uow.emails.add(user_id=other.id, email="user@example.com")

    with pytest.raises(ApiError):
        email_binding.verify(token=token)

    assert uow.commits == 0


def test_email_token_never_logged_in_api_user(
    email_binding, email_sink: InMemoryEmailSink, seeded_user
) -> None:
    email_binding.request_verification(user_id=seeded_user.id, email="user@example.com")
    token = email_sink.sent[0]["token"]

    user = email_binding.verify(token=token)

    assert token not in user.model_dump_json()
