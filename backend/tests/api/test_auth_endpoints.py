"""ENG-03 第 3 步：认证与用户端点的行为与契约测试。

覆盖：Cookie 属性、幂等 Header 的 400 映射、401 语义、成功 envelope、
以及「不得实现」的两条路径。

所有外部依赖均以内存替身注入（不启动 Redis / PostgreSQL / Compose）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_email_port_dep,
    get_points_port_dep,
    get_redis_port,
    get_uow,
)
from app.config import reset_settings_cache
from app.errors.codes import ErrorCode
from app.main import create_app
from app.middleware.request_id import REQUEST_ID_HEADER
from app.services.ports import (
    InMemoryEmailSink,
    TransactionalPointsPort,
    UnavailableEmailPort,
    UnavailablePointsPort,
)
from tests.services.fakes import (
    FakePointsPort,
    FakeRedis,
    InMemoryUnitOfWork,
    make_generation_task_row,
    make_user_row,
)

#: Secure Cookie 需要 https 才能被客户端保存与回发
BASE_URL = "https://testserver"
VALID_KEY = "synthetic-idempotency-key-001"
REDEEM_PATH = "/api/v1/auth/invites/redeem"
REDEEM_BODY = {"invite_code": "AVTR-ABCD-EFGH", "email": "user@example.com"}


class AuthHarness:
    """认证端点测试装置：持有 app 与注入的替身。"""

    def __init__(
        self,
        app: FastAPI,
        client: TestClient,
        uow: InMemoryUnitOfWork,
        redis: FakeRedis,
        points: object,
        email: InMemoryEmailSink | UnavailableEmailPort,
    ) -> None:
        self.app = app
        self.client = client
        self.uow = uow
        self.redis = redis
        self.points = points
        self.email = email


SESSION_SECRET = "synthetic-session-secret-for-tests"


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[AuthHarness]:
    # 会话/令牌摘要依赖 SESSION_SECRET，测试必须与应用的配置一致
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    reset_settings_cache()
    app = create_app()
    uow = InMemoryUnitOfWork()
    redis = FakeRedis()
    points: FakePointsPort | UnavailablePointsPort = FakePointsPort()
    email: InMemoryEmailSink | UnavailableEmailPort = InMemoryEmailSink()

    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_redis_port] = lambda: redis
    app.dependency_overrides[get_points_port_dep] = lambda: points
    app.dependency_overrides[get_email_port_dep] = lambda: email

    with TestClient(app, base_url=BASE_URL) as client:
        yield AuthHarness(app, client, uow, redis, points, email)

    app.dependency_overrides.clear()
    reset_settings_cache()


def _seed_user(harness: AuthHarness):
    return harness.uow.users.seed(make_user_row())


def _login(harness: AuthHarness, user_id: uuid.UUID) -> None:
    """写入一个会话并让后续请求带上 `session` Cookie。"""

    from app.services.auth.session_store import RedisSessionStore

    store = RedisSessionStore(harness.redis, secret=SESSION_SECRET)
    token, _ = store.create(user_id=user_id, email_verified=False)
    harness.client.headers["Cookie"] = f"session={token}"


# --------------------------------------------------------------------------- #
# 邀请码兑换
# --------------------------------------------------------------------------- #
def test_redeem_returns_201_with_session_cookie(harness: AuthHarness) -> None:
    response = harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )

    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {"data", "request_id"}
    assert set(payload["data"]) == {"user", "session", "points"}
    assert payload["data"]["points"]["available_points"] > 0
    assert response.headers[REQUEST_ID_HEADER] == payload["request_id"]

    set_cookie = response.headers["set-cookie"]
    assert "session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")


def test_redeem_passes_plaintext_code_to_points_port(harness: AuthHarness) -> None:
    harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )

    assert harness.points.calls[0]["invite_code"] == "AVTR-ABCD-EFGH"
    assert harness.points.calls[0]["email"] == "user@example.com"


def test_redeem_returns_503_when_points_port_unwired(harness: AuthHarness) -> None:
    harness.app.dependency_overrides[get_points_port_dep] = lambda: UnavailablePointsPort()

    response = harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NOT_READY"


def test_redeem_rejects_invalid_payload(harness: AuthHarness) -> None:
    response = harness.client.post(
        REDEEM_PATH, json={"invite_code": "", "email": "not-an-email"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# 幂等 Header 的 400 映射
# --------------------------------------------------------------------------- #
def test_missing_idempotency_key_is_400_required(harness: AuthHarness) -> None:
    response = harness.client.post(REDEEM_PATH, json=REDEEM_BODY)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


def test_blank_idempotency_key_is_400_invalid(harness: AuthHarness) -> None:
    response = harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": "   "}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_INVALID"


def test_too_long_idempotency_key_is_400_invalid(harness: AuthHarness) -> None:
    response = harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": "a" * 129}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_INVALID"


def test_max_length_idempotency_key_is_accepted(harness: AuthHarness) -> None:
    response = harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": "a" * 128}
    )

    assert response.status_code == 201


def test_logout_requires_idempotency_key(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)

    response = harness.client.post("/api/v1/auth/logout")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def test_session_without_cookie_is_401_auth_required(harness: AuthHarness) -> None:
    response = harness.client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_session_with_unknown_cookie_is_401_session_invalid(harness: AuthHarness) -> None:
    harness.client.cookies.set("session", "unknown-token")

    response = harness.client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_INVALID"


def test_session_returns_session_envelope(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)

    response = harness.client.get("/api/v1/auth/session")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "request_id"}
    assert set(payload["data"]) == {"session_id", "user_id", "email_verified", "expires_at"}
    assert payload["data"]["user_id"] == str(user.id)


# --------------------------------------------------------------------------- #
# 会话事实一致性（ENG-03 缺陷回归）
#
# `SessionRecord.email_verified` 只在 `RedisSessionStore.create()`（邀请兑换）时写入，
# 而 `POST /auth/email/verify` 只改 `user_emails`、**不续签会话**。因此
# 「先兑换、再点验证链接」的用户拿到的会话快照永远是 false，前端 `useSession`
# 据此锁死上传与提交入口，而服务端授权读的是数据库 —— 「接口能过、界面锁死」。
#
# 修法是让**会话查询**以数据库事实为准（不重签 Cookie、不延长 TTL、
# 不复活已撤销会话、不改 Cookie 属性、不凭令牌为别人开锁）。下面三条是它的回归。
# --------------------------------------------------------------------------- #
def test_session_reflects_the_verification_fact_after_email_verify(
    harness: AuthHarness,
) -> None:
    """验证成功后，**同一个会话**的查询结果必须立刻反映已验证。"""

    user = _seed_user(harness)
    _login(harness, user.id)  # 快照里 email_verified=False

    before = harness.client.get("/api/v1/auth/session").json()["data"]
    assert before["email_verified"] is False, before

    harness.client.post(
        "/api/v1/auth/email/request",
        json={"email": "user@example.com"},
        headers={"Idempotency-Key": VALID_KEY},
    )
    verify = harness.client.post(
        "/api/v1/auth/email/verify",
        json={"token": harness.email.sent[0]["token"]},
        headers={"Idempotency-Key": VALID_KEY},
    )
    assert verify.status_code == 200, verify.text

    after = harness.client.get("/api/v1/auth/session").json()["data"]
    assert after["email_verified"] is True, after

    # 事实变了，会话本身**没有**被重签或延长：同一个 session_id、同一个过期时间。
    assert after["session_id"] == before["session_id"]
    assert after["user_id"] == before["user_id"]
    assert after["expires_at"] == before["expires_at"]


def test_session_reports_the_database_fact_not_the_redis_snapshot(
    harness: AuthHarness,
) -> None:
    """反向：快照说已验证、数据库里没有已验证邮箱 → 会话查询必须是 false。"""

    from app.services.auth.session_store import RedisSessionStore

    user = _seed_user(harness)
    store = RedisSessionStore(harness.redis, secret=SESSION_SECRET)
    token, _ = store.create(user_id=user.id, email_verified=True)
    harness.client.headers["Cookie"] = f"session={token}"

    payload = harness.client.get("/api/v1/auth/session").json()["data"]

    assert payload["email_verified"] is False, payload


def test_email_verify_does_not_unlock_another_users_session(harness: AuthHarness) -> None:
    """验证令牌只能改变**它自己那个用户**的验证事实，不得为别人开锁。"""

    from app.services.auth.session_store import RedisSessionStore

    user_a = _seed_user(harness)
    user_b = _seed_user(harness)
    store = RedisSessionStore(harness.redis, secret=SESSION_SECRET)
    token_a, _ = store.create(user_id=user_a.id, email_verified=False)
    token_b, _ = store.create(user_id=user_b.id, email_verified=False)

    harness.client.headers["Cookie"] = f"session={token_b}"
    harness.client.post(
        "/api/v1/auth/email/request",
        json={"email": "b@example.com"},
        headers={"Idempotency-Key": VALID_KEY},
    )
    verify = harness.client.post(
        "/api/v1/auth/email/verify",
        json={"token": harness.email.sent[0]["token"]},
        headers={"Idempotency-Key": VALID_KEY},
    )
    assert verify.status_code == 200, verify.text

    unlocked = harness.client.get("/api/v1/auth/session").json()["data"]
    assert unlocked["user_id"] == str(user_b.id)
    assert unlocked["email_verified"] is True, unlocked

    # 另一个用户的会话不受任何影响
    harness.client.headers["Cookie"] = f"session={token_a}"
    other = harness.client.get("/api/v1/auth/session").json()["data"]
    assert other["user_id"] == str(user_a.id)
    assert other["email_verified"] is False, other


def test_logout_revokes_session_and_clears_cookie(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)

    response = harness.client.post(
        "/api/v1/auth/logout", headers={"Idempotency-Key": VALID_KEY}
    )

    assert response.status_code == 204
    assert response.content == b""
    assert harness.client.get("/api/v1/auth/session").status_code == 401


# --------------------------------------------------------------------------- #
# 邮箱验证
# --------------------------------------------------------------------------- #
def test_email_request_returns_202_and_sends_via_port(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)

    response = harness.client.post(
        "/api/v1/auth/email/request",
        json={"email": "new@example.com"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 202
    assert response.json()["data"] == {}
    assert len(harness.email.sent) == 1


def test_email_request_without_session_is_401(harness: AuthHarness) -> None:
    response = harness.client.post(
        "/api/v1/auth/email/request",
        json={"email": "new@example.com"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_email_verify_with_unknown_token_is_422(harness: AuthHarness) -> None:
    response = harness.client.post(
        "/api/v1/auth/email/verify",
        json={"token": "not-a-real-token"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EMAIL_TOKEN_INVALID"


def test_email_verify_binds_email(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)
    harness.client.post(
        "/api/v1/auth/email/request",
        json={"email": "user@example.com"},
        headers={"Idempotency-Key": VALID_KEY},
    )
    token = harness.email.sent[0]["token"]

    response = harness.client.post(
        "/api/v1/auth/email/verify",
        json={"token": token},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "request_id"}
    assert payload["data"]["primary_email"] == "user@example.com"
    assert payload["data"]["email_verified"] is True


# --------------------------------------------------------------------------- #
# 当前用户
# --------------------------------------------------------------------------- #
def test_get_me_returns_user_envelope(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)

    response = harness.client.get("/api/v1/me")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"data", "request_id"}
    assert payload["data"]["id"] == str(user.id)
    assert payload["data"]["status"] == "ACTIVE"


def test_get_me_without_session_is_401(harness: AuthHarness) -> None:
    assert harness.client.get("/api/v1/me").status_code == 401


def test_patch_me_updates_allowed_field(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)

    response = harness.client.patch(
        "/api/v1/me",
        json={"display_name": "十一", "locale": "en-US"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 200
    assert response.json()["data"]["display_name"] == "十一"
    assert response.json()["data"]["locale"] == "en-US"


def test_patch_me_rejects_unknown_field(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)

    response = harness.client.patch(
        "/api/v1/me",
        json={"email": "hacked@example.com"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_me_rejects_explicit_null_on_non_nullable_field(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)

    response = harness.client.patch(
        "/api/v1/me",
        json={"locale": None},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_me_rejects_avatar_owned_by_another_user(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)
    asset_id = uuid.uuid4()
    harness.uow.media.register(asset_id, uuid.uuid4())

    response = harness.client.patch(
        "/api/v1/me",
        json={"avatar_asset_id": str(asset_id)},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ASSET_FORBIDDEN"


def test_patch_me_accepts_owned_avatar(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user.id)
    asset_id = uuid.uuid4()
    harness.uow.media.register(asset_id, user.id)

    response = harness.client.patch(
        "/api/v1/me",
        json={"avatar_asset_id": str(asset_id)},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 200
    assert response.json()["data"]["avatar_asset_id"] == str(asset_id)


# --------------------------------------------------------------------------- #
# 账号状态与权限错误（HTTP 级）
# --------------------------------------------------------------------------- #
def test_get_me_with_suspended_user_is_403(harness: AuthHarness) -> None:
    suspended = harness.uow.users.seed(make_user_row(status="SUSPENDED"))
    _login(harness, suspended.id)

    response = harness.client.get("/api/v1/me")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_SUSPENDED"


def test_get_me_with_deleted_user_is_401(harness: AuthHarness) -> None:
    deleted = harness.uow.users.seed(make_user_row(status="DELETED"))
    _login(harness, deleted.id)

    response = harness.client.get("/api/v1/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_INVALID"


def test_session_for_deleted_user_still_reads(harness: AuthHarness) -> None:
    """`/auth/session` 只读会话状态，账号状态校验发生在资料接口。"""

    deleted = harness.uow.users.seed(make_user_row(status="DELETED"))
    _login(harness, deleted.id)

    assert harness.client.get("/api/v1/auth/session").status_code == 200


def test_patch_me_with_suspended_user_is_403(harness: AuthHarness) -> None:
    suspended = harness.uow.users.seed(make_user_row(status="SUSPENDED"))
    _login(harness, suspended.id)

    response = harness.client.patch(
        "/api/v1/me",
        json={"display_name": "x"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_SUSPENDED"


# --------------------------------------------------------------------------- #
# 邀请码错误（由 Points 端口抛出，端点按契约映射）
# --------------------------------------------------------------------------- #
def _redeem_with_points_error(harness: AuthHarness, code: ErrorCode) -> object:
    from app.errors.exceptions import ApiError
    from tests.services.fakes import FakePointsPort

    harness.app.dependency_overrides[get_points_port_dep] = lambda: FakePointsPort(
        fail_with=ApiError(code)
    )
    return harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )


def test_redeem_maps_invite_invalid_to_422(harness: AuthHarness) -> None:
    response = _redeem_with_points_error(harness, ErrorCode.INVITE_INVALID)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVITE_INVALID"


def test_redeem_maps_invite_already_used_to_409(harness: AuthHarness) -> None:
    """重复兑换（或次数耗尽）由 `INVITE_ALREADY_USED` 表达。"""

    response = _redeem_with_points_error(harness, ErrorCode.INVITE_ALREADY_USED)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVITE_ALREADY_USED"


def test_redeem_maps_not_ready_to_503(harness: AuthHarness) -> None:
    from app.errors.exceptions import ApiError

    response = _redeem_with_points_error(harness, ErrorCode.NOT_READY)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NOT_READY"
    assert isinstance(response.json()["error"]["code"], str)
    assert ApiError(ErrorCode.NOT_READY).status_code == 503


# --------------------------------------------------------------------------- #
# 邮箱占用与令牌一次性消费（HTTP 级）
# --------------------------------------------------------------------------- #
def _request_email(harness: AuthHarness, user_id: uuid.UUID, email: str) -> None:
    _login(harness, user_id)
    harness.client.post(
        "/api/v1/auth/email/request",
        json={"email": email},
        headers={"Idempotency-Key": VALID_KEY},
    )


def test_email_verify_rejects_email_in_use(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _request_email(harness, user.id, "taken@example.com")
    token = harness.email.sent[0]["token"]

    other = harness.uow.users.seed(make_user_row())
    harness.uow.emails.add(user_id=other.id, email="taken@example.com", is_primary=True)

    response = harness.client.post(
        "/api/v1/auth/email/verify",
        json={"token": token},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_IN_USE"


def test_email_verify_rejects_already_verified(harness: AuthHarness) -> None:
    from datetime import datetime, timezone

    user = _seed_user(harness)
    row = harness.uow.emails.add(
        user_id=user.id, email="mine@example.com", is_primary=True
    )
    harness.uow.emails.mark_verified(row, datetime.now(timezone.utc))

    _request_email(harness, user.id, "mine@example.com")
    token = harness.email.sent[0]["token"]

    response = harness.client.post(
        "/api/v1/auth/email/verify",
        json={"token": token},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_ALREADY_VERIFIED"


def test_email_token_is_single_use_over_http(harness: AuthHarness) -> None:
    user = _seed_user(harness)
    _request_email(harness, user.id, "user@example.com")
    token = harness.email.sent[0]["token"]

    first = harness.client.post(
        "/api/v1/auth/email/verify",
        json={"token": token},
        headers={"Idempotency-Key": VALID_KEY},
    )
    second = harness.client.post(
        "/api/v1/auth/email/verify",
        json={"token": token},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert first.status_code == 200
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "EMAIL_TOKEN_INVALID"


def test_email_request_does_not_enumerate_other_users_email(harness: AuthHarness) -> None:
    """防枚举：邮箱属于他人时仍返回 202 且不投递。"""

    user = _seed_user(harness)
    other = harness.uow.users.seed(make_user_row())
    harness.uow.emails.add(user_id=other.id, email="taken@example.com", is_primary=True)
    _login(harness, user.id)

    response = harness.client.post(
        "/api/v1/auth/email/request",
        json={"email": "taken@example.com"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 202
    assert harness.email.sent == ()


# --------------------------------------------------------------------------- #
# 默认 PointsPort 接线（ENG-04 第 2 步）：端点走真实事务端口
# --------------------------------------------------------------------------- #
def _seed_invite_code(
    uow: InMemoryUnitOfWork, *, raw: str = "AVTR-ABCD-EFGH", initial_points: int = 150
):
    from datetime import datetime, timezone

    from app.db.models.invites import InviteCode
    from app.services.auth.invite_code import hash_invite_code

    code = InviteCode(
        code_hash=hash_invite_code(raw),
        status="ACTIVE",
        initial_points=initial_points,
        max_redemptions=3,
        redeemed_count=0,
        expires_at=None,
        created_at=datetime.now(timezone.utc),
    )
    code.id = uuid.uuid4()
    return uow.invite_codes.seed(code)


@pytest.fixture
def real_port_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[AuthHarness]:
    """与生产一致的默认接线：`get_points_port_dep` **不被覆盖**。

    只把默认端口内部的 `uow_factory` 换成内存工作单元（等价于
    `SqlAlchemyUnitOfWork(get_session_factory())`），因此链路覆盖
    「路由 → `RedeemService` → `TransactionalPointsPort` → `InviteRedeemService`
    → `PointsService` → 钱包/流水」，且路由与 `RedeemService` 中没有任何账务逻辑。
    """

    from app.api import deps as api_deps
    from app.services.auth.session_store import RedisSessionStore

    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    reset_settings_cache()
    app = create_app()
    uow = InMemoryUnitOfWork()
    redis = FakeRedis()
    email: InMemoryEmailSink | UnavailableEmailPort = InMemoryEmailSink()
    store = RedisSessionStore(redis, secret=SESSION_SECRET)
    port = TransactionalPointsPort(uow_factory=lambda: uow, session_store=store)

    monkeypatch.setattr(api_deps, "get_points_port", lambda: port)
    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_redis_port] = lambda: redis
    app.dependency_overrides[get_email_port_dep] = lambda: email

    with TestClient(app, base_url=BASE_URL) as client:
        yield AuthHarness(app, client, uow, redis, port, email)

    app.dependency_overrides.clear()
    reset_settings_cache()


def test_redeem_default_dependency_is_not_overridden(
    real_port_harness: AuthHarness,
) -> None:
    """默认路径必须穿过真实的 `get_points_port_dep`，而不是测试替身。"""

    assert get_points_port_dep not in real_port_harness.app.dependency_overrides
    assert isinstance(real_port_harness.points, TransactionalPointsPort)


def test_redeem_through_real_port_writes_credit_and_wallet(
    real_port_harness: AuthHarness,
) -> None:
    _seed_invite_code(real_port_harness.uow, initial_points=150)

    response = real_port_harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )

    assert response.status_code == 201
    payload = response.json()
    points = payload["data"]["points"]
    assert points["available_points"] == 150
    assert points["reserved_points"] == 0
    assert points["lifetime_credited"] == 150
    assert points["lifetime_consumed"] == 0
    assert payload["data"]["user"]["primary_email"] == "user@example.com"

    # 真实 CREDIT 流水 + 钱包快照（而不是 FakePointsPort 的合成值）
    entries = real_port_harness.uow.point_ledger.entries
    assert [e.entry_type for e in entries] == ["CREDIT"]
    assert entries[0].available_delta == 150
    assert entries[0].reserved_delta == 0
    user_id = uuid.UUID(payload["data"]["user"]["id"])
    wallet = real_port_harness.uow.point_wallets.get_by_user_id(user_id)
    assert (wallet.available_points, wallet.lifetime_credited, wallet.version) == (
        150,
        150,
        1,
    )
    # 事务由端口提交恰好一次
    assert real_port_harness.uow.commits == 1
    assert "session=" in response.headers["set-cookie"]


def test_redeem_through_real_port_issues_readable_session_cookie(
    real_port_harness: AuthHarness,
) -> None:
    from app.services.auth.session_store import RedisSessionStore

    _seed_invite_code(real_port_harness.uow)

    response = real_port_harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )
    cookie = response.headers["set-cookie"]
    token = cookie.split("session=", 1)[1].split(";", 1)[0]

    record = RedisSessionStore(real_port_harness.redis, secret=SESSION_SECRET).read(token)

    assert record is not None
    assert str(record.user_id) == response.json()["data"]["user"]["id"]


def test_redeem_through_real_port_rejects_unknown_code_without_writes(
    real_port_harness: AuthHarness,
) -> None:
    """邀请码无效时不得留下任何流水、钱包、兑换记录或会话。"""

    response = real_port_harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVITE_INVALID"
    assert real_port_harness.uow.point_ledger.entries == ()
    assert real_port_harness.uow.commits == 0
    assert real_port_harness.redis.keys() == ()


def test_redeem_through_real_port_is_idempotent_per_user(
    real_port_harness: AuthHarness,
) -> None:
    """同一用户对同一邀请码重复兑换：409，且点数只发放一次。"""

    code = _seed_invite_code(real_port_harness.uow, initial_points=120)

    first = real_port_harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )
    second = real_port_harness.client.post(
        REDEEM_PATH, json=REDEEM_BODY, headers={"Idempotency-Key": VALID_KEY}
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "INVITE_ALREADY_USED"
    assert code.redeemed_count == 1
    assert [e.entry_type for e in real_port_harness.uow.point_ledger.entries] == [
        "CREDIT"
    ]
    user_id = uuid.UUID(first.json()["data"]["user"]["id"])
    assert (
        real_port_harness.uow.point_wallets.get_by_user_id(user_id).available_points
        == 120
    )


def test_no_accounting_logic_in_route_or_redeem_service() -> None:
    """ENG-04 接线后仍不得把账务逻辑复制进 ENG-03 的路由或 Service。

    只扫描**代码标识符与调用**（注释里说明「流水由端口写入」是允许的）。
    """

    import inspect

    from app.api.auth import router as auth_router
    from app.services.auth.redeem import RedeemService

    for source in (inspect.getsource(auth_router), inspect.getsource(RedeemService)):
        for token in (
            "point_ledger",
            "point_wallets",
            "PointsService",
            "LedgerEntryDraft",
            "WalletDelta",
            "available_delta",
            "reserved_delta",
            "apply_delta",
            ".credit(",
            ".reserve(",
            ".consume(",
            ".release(",
            ".adjust(",
            "INSERT ",
            "UPDATE ",
        ):
            assert token not in source, token


# --------------------------------------------------------------------------- #
# 不得实现的路径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/api/v1/auth/me"])
def test_out_of_scope_paths_are_not_routed(harness: AuthHarness, path: str) -> None:
    assert harness.client.get(path).status_code == 404


def test_points_read_paths_are_routed_and_gated_by_session(harness: AuthHarness) -> None:
    """ENG-04 点数读取补口落地后，这两条路径不再属于「不得实现」。

    原先的 404 断言是本卡的**范围**断言（当时点数读取口未实现），实现完成后它
    自然失效：现在两条路径都已注册，匿名访问应当是认证门槛 `auth_required`（401），
    而不是「没有这条路由」（404）。逐字段的余额/流水语义由
    `tests/api/test_points_endpoints.py` 与 OpenAPI 基线比较负责，这里只钉住
    「路由已存在且先过认证」这一层，避免把 404 当成实现缺失的永久豁免。
    """

    for path in ("/api/v1/me/points", "/api/v1/me/points/ledger"):
        response = harness.client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"


# --------------------------------------------------------------------------- #
# ENG-04 修正批次（总控 2026-09-27 裁决 A）：错误映射经既有处理器落到契约状态
# --------------------------------------------------------------------------- #
def _points_probe_client(uow: InMemoryUnitOfWork) -> TestClient:
    """为**真实** `PointsService` 调用建一个探针 app，只安装既有异常处理器。

    - 不修改 `app/api/*`，不依赖 `tests/api/conftest.py`（本卡无权改）；
    - 探针路径 `/points-probe/*` 只存在于测试内部，不是业务路由，也不进 OpenAPI；
    - `raise_server_exceptions=False` 才能观察到未处理异常处理器产出的 500 响应体。
    """

    from app.errors.handlers import install_exception_handlers
    from app.services.points.service import PointsService

    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/points-probe/consume")
    def _consume(user_id: str, generation_id: str) -> dict[str, bool]:
        PointsService(uow=uow).consume(
            user_id=uuid.UUID(user_id),
            generation_id=uuid.UUID(generation_id),
            idempotency_key="f1",
            reason="settlement",
        )
        return {"ok": True}

    @app.get("/points-probe/review")
    def _review(generation_id: str) -> dict[str, bool]:
        PointsService(uow=uow).resolve_needs_review(
            generation_id=uuid.UUID(generation_id),
            decision="CONSUME",
            idempotency_key="rev1",
            admin_id=uuid.uuid4(),
            request_id="req_probe_0001",
            note="evidence verified",
        )
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_http_missing_generation_task_returns_404_not_found() -> None:
    uow = InMemoryUnitOfWork()
    user = uow.users.seed(make_user_row())

    response = _points_probe_client(uow).get(
        "/points-probe/consume",
        params={"user_id": str(user.id), "generation_id": str(uuid.uuid4())},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.GENERATION_NOT_FOUND.value


def test_http_foreign_generation_task_is_indistinguishable_from_missing() -> None:
    """归属不符与不存在返回**逐字相同**的 404：不泄露他人任务是否存在。"""

    uow = InMemoryUnitOfWork()
    owner = uow.users.seed(make_user_row())
    stranger = uow.users.seed(make_user_row())
    task = uow.generation_tasks.seed(make_generation_task_row(user_id=owner.id))
    client = _points_probe_client(uow)

    foreign = client.get(
        "/points-probe/consume",
        params={"user_id": str(stranger.id), "generation_id": str(task.id)},
    )
    missing = client.get(
        "/points-probe/consume",
        params={"user_id": str(stranger.id), "generation_id": str(uuid.uuid4())},
    )

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == missing.json()["error"]
    assert foreign.json()["error"]["code"] == ErrorCode.GENERATION_NOT_FOUND.value
    assert str(owner.id) not in foreign.text
    assert task.billing_status == "RESERVED"


def test_http_review_on_non_review_task_returns_409_idempotency_conflict() -> None:
    uow = InMemoryUnitOfWork()
    user = uow.users.seed(make_user_row())
    task = uow.generation_tasks.seed(
        make_generation_task_row(user_id=user.id, status="PROCESSING")
    )

    response = _points_probe_client(uow).get(
        "/points-probe/review", params={"generation_id": str(task.id)}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value
    assert task.billing_status == "RESERVED"
    assert uow.point_ledger.entries == ()
    assert uow.audit_logs.entries == ()


def test_http_invariant_breakage_maps_to_500_internal_error() -> None:
    """账务不变量破坏仍是内部异常：由**已注册**的未处理异常处理器给出 500。

    证明 `app/errors/handlers.py` 的 `add_exception_handler(Exception, ...)` →
    `build_error_response(request, INTERNAL_ERROR)` 链路成立，因此 Service 侧
    不需要任何新增错误码或包装层。
    """

    uow = InMemoryUnitOfWork()
    user = uow.users.seed(make_user_row())
    task = uow.generation_tasks.seed(
        make_generation_task_row(user_id=user.id, reserved_points=0)
    )

    response = _points_probe_client(uow).get(
        "/points-probe/consume",
        params={"user_id": str(user.id), "generation_id": str(task.id)},
    )

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == ErrorCode.INTERNAL_ERROR.value
    assert payload["error"]["details"] == {}
    assert "request_id" in payload
    # 内部异常文本（字段名、任务 ID）不得进入响应
    assert "reserved_points" not in response.text
    assert "WalletLedgerError" not in response.text
    assert str(task.id) not in response.text
    assert task.billing_status == "RESERVED"
    assert uow.point_ledger.entries == ()
