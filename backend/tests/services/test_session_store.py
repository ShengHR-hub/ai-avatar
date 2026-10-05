"""Redis 会话存储测试（ENG-03 第 1 步）。

覆盖技术负责人裁决 ③ 的全部要求：随机 opaque token、Redis 不保存原文、
HMAC-SHA256 摘要 key、版本化命名空间、TTL、撤销与过期。
"""

from __future__ import annotations

import re
import uuid

import pytest

from app.services.auth.session_store import (
    SESSION_KEY_NAMESPACE,
    SESSION_TTL_SECONDS,
    RedisSessionStore,
    SessionRecord,
    generate_session_token,
    session_digest,
    session_key,
)
from tests.services.fakes import FakeRedis, MutableClock

USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture
def store(fake_redis: FakeRedis, session_secret: str) -> RedisSessionStore:
    return RedisSessionStore(fake_redis, secret=session_secret)


# --------------------------------------------------------------------------- #
# 令牌与 key 派生
# --------------------------------------------------------------------------- #
def test_generate_session_token_is_random_and_urlsafe() -> None:
    first = generate_session_token()
    second = generate_session_token()

    assert first != second
    assert len(first) == 43  # token_urlsafe(32)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)


def test_session_digest_is_hmac_sha256_hex(session_secret: str) -> None:
    digest = session_digest(session_secret, "synthetic-token")

    assert DIGEST_PATTERN.match(digest)
    # 同一输入稳定、不同密钥不同、不同输入不同
    assert digest == session_digest(session_secret, "synthetic-token")
    assert digest != session_digest("other-secret", "synthetic-token")
    assert digest != session_digest(session_secret, "other-token")


def test_session_digest_requires_secret() -> None:
    with pytest.raises(ValueError):
        session_digest("", "synthetic-token")


def test_session_key_uses_versioned_namespace(session_secret: str) -> None:
    digest = session_digest(session_secret, "synthetic-token")

    assert session_key(digest) == f"{SESSION_KEY_NAMESPACE}:{digest}"
    assert SESSION_KEY_NAMESPACE == "ai_avatar:v1:session"


# --------------------------------------------------------------------------- #
# 创建
# --------------------------------------------------------------------------- #
def test_create_writes_only_digest_to_redis(
    store: RedisSessionStore, fake_redis: FakeRedis, session_secret: str, clock: MutableClock
) -> None:
    token, record = store.create(user_id=USER_ID, email_verified=False, now=clock())

    expected_digest = session_digest(session_secret, token)
    assert record.session_id == expected_digest
    assert record.user_id == USER_ID
    assert record.email_verified is False
    assert fake_redis.keys() == (session_key(expected_digest),)
    # 裁决 ③：Redis 不保存 Cookie 原文
    assert token not in "".join(fake_redis.keys())
    assert token not in "".join(fake_redis.raw_values())
    assert expected_digest in fake_redis.raw_values() or expected_digest in "".join(
        fake_redis.keys()
    )


def test_create_applies_ttl(
    store: RedisSessionStore, fake_redis: FakeRedis, clock: MutableClock
) -> None:
    _, record = store.create(user_id=USER_ID, email_verified=True, now=clock())

    assert fake_redis.ttl(session_key(record.session_id)) == SESSION_TTL_SECONDS
    assert (record.expires_at - clock()).total_seconds() == SESSION_TTL_SECONDS


def test_store_accepts_custom_ttl(
    fake_redis: FakeRedis, session_secret: str, clock: MutableClock
) -> None:
    """TTL 属待冻结产品参数，必须以构造参数隔离而非硬编码。"""

    custom = RedisSessionStore(fake_redis, secret=session_secret, ttl_seconds=60)
    _, record = custom.create(user_id=USER_ID, email_verified=False, now=clock())

    assert custom.ttl_seconds == 60
    assert fake_redis.ttl(session_key(record.session_id)) == 60


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def test_read_returns_record_for_valid_token(store: RedisSessionStore, clock: MutableClock) -> None:
    token, created = store.create(user_id=USER_ID, email_verified=True, now=clock())

    found = store.read(token)

    assert found is not None
    assert found.session_id == created.session_id
    assert found.user_id == USER_ID
    assert found.email_verified is True
    assert found.expires_at == created.expires_at


def test_read_returns_none_for_unknown_or_empty_token(store: RedisSessionStore) -> None:
    assert store.read("not-a-real-token") is None
    assert store.read("") is None


def test_read_returns_none_after_expiry(
    store: RedisSessionStore, fake_redis: FakeRedis, clock: MutableClock
) -> None:
    token, record = store.create(user_id=USER_ID, email_verified=False, now=clock())

    clock.advance(SESSION_TTL_SECONDS + 1)

    assert store.read(token) is None
    # 过期会话应被清除
    assert fake_redis.get(session_key(record.session_id)) is None


def test_read_at_exact_expiry_boundary_is_expired(
    store: RedisSessionStore, clock: MutableClock
) -> None:
    token, _ = store.create(user_id=USER_ID, email_verified=False, now=clock())

    clock.advance(SESSION_TTL_SECONDS)

    assert store.read(token) is None


# --------------------------------------------------------------------------- #
# 撤销
# --------------------------------------------------------------------------- #
def test_revoke_removes_session_and_is_idempotent(
    store: RedisSessionStore, fake_redis: FakeRedis, clock: MutableClock
) -> None:
    token, _ = store.create(user_id=USER_ID, email_verified=False, now=clock())

    store.revoke(token)
    assert store.read(token) is None
    assert fake_redis.keys() == ()

    store.revoke(token)  # 幂等
    store.revoke("")  # 空令牌不报错


# --------------------------------------------------------------------------- #
# 记录与对外模型
# --------------------------------------------------------------------------- #
def test_record_payload_roundtrip(store: RedisSessionStore, clock: MutableClock) -> None:
    _, record = store.create(user_id=USER_ID, email_verified=True, now=clock())

    restored = SessionRecord.from_payload(record.session_id, record.to_payload())

    assert restored == record


def test_record_payload_has_no_token_field(store: RedisSessionStore, clock: MutableClock) -> None:
    token, record = store.create(user_id=USER_ID, email_verified=False, now=clock())

    payload = record.to_payload()

    assert token not in payload
    assert "token" not in payload


def test_record_to_api_model_matches_contract_shape(
    store: RedisSessionStore, clock: MutableClock
) -> None:
    token, record = store.create(user_id=USER_ID, email_verified=True, now=clock())

    model = record.to_api_model()

    assert set(model.model_dump()) == {"session_id", "user_id", "email_verified", "expires_at"}
    assert model.session_id == record.session_id
    assert token not in model.model_dump_json()


def test_session_id_is_digest_not_token(store: RedisSessionStore, clock: MutableClock) -> None:
    token, record = store.create(user_id=USER_ID, email_verified=False, now=clock())

    assert DIGEST_PATTERN.match(record.session_id)
    assert record.session_id != token
