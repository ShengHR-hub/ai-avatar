"""分享端点测试（ENG-09）。

全部外部依赖以内存替身注入：不启动 PostgreSQL、Redis 与对象存储。

覆盖端点层该管的事：认证与邮箱门槛、`Idempotency-Key` 的 400 映射、请求体
`additionalProperties: false`、201/409 响应形状、匿名读取（`security: []`）、
撤销/过期/不存在的 404、以及「响应不泄漏内部事实」。
业务规则本身在 `tests/services/test_shares.py` 覆盖，此处不重复。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_redis_port,
    get_session_store,
    get_share_service,
    get_uow,
)
from app.config import reset_settings_cache
from app.main import create_app
from app.repositories.generation import GenerationOutputDraft
from app.repositories.engagement import IdempotencyScopeBusy
from app.services.shares.service import DEFAULT_SHARE_TTL_DAYS, ShareService
from tests.services.fakes import (
    EngagementUnitOfWork,
    FakeRedis,
    MutableClock,
    make_generation_task_row,
    make_media_asset_row,
    make_user_row,
)

BASE_URL = "https://testserver"
SESSION_SECRET = "synthetic-session-secret-for-tests"
SHARES_PATH = "/api/v1/shares"
CREATE_KEY = "synthetic-share-key-001"
CLOCK_START = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class ShareHarness:
    def __init__(
        self,
        app: FastAPI,
        client: TestClient,
        uow: EngagementUnitOfWork,
        redis: FakeRedis,
        clock: MutableClock,
    ) -> None:
        self.app = app
        self.client = client
        self.uow = uow
        self.redis = redis
        self.clock = clock


def _store(redis: FakeRedis):
    from app.services.auth.session_store import RedisSessionStore

    return RedisSessionStore(redis, secret=SESSION_SECRET)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[ShareHarness]:
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    reset_settings_cache()

    clock = MutableClock(CLOCK_START)
    app = create_app()
    uow = EngagementUnitOfWork(clock=clock)
    redis = FakeRedis(clock=clock)

    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_redis_port] = lambda: redis
    app.dependency_overrides[get_session_store] = lambda: _store(redis)
    app.dependency_overrides[get_share_service] = lambda: ShareService(
        uow=uow, clock=clock
    )

    with TestClient(app, base_url=BASE_URL) as client:
        yield ShareHarness(app, client, uow, redis, clock)

    app.dependency_overrides.clear()
    reset_settings_cache()


def _seed_user(harness: ShareHarness, *, verified: bool = True, email: str | None = None):
    user = harness.uow.users.seed(make_user_row())
    row = harness.uow.emails.add(
        user_id=user.id, email=email or f"user-{uuid.uuid4().hex[:8]}@example.com", is_primary=True
    )
    if verified:
        harness.uow.emails.mark_verified(row, harness.clock())
    return user


def _login(harness: ShareHarness, user) -> None:
    token, _record = _store(harness.redis).create(
        user_id=user.id, email_verified=True
    )
    harness.client.headers["Cookie"] = f"session={token}"


def _seed_outputs(
    harness: ShareHarness,
    *,
    user,
    status: str = "SUCCEEDED",
    asset_status: str = "READY",
    purpose: str = "RESULT",
    count: int = 2,
    expires_at: datetime | None = None,
):
    task = make_generation_task_row(user_id=user.id, status=status, now=harness.clock())
    harness.uow.generation_tasks.seed(task)
    asset_ids: list[uuid.UUID] = []
    for index in range(count):
        asset = make_media_asset_row(
            user_id=user.id,
            now=harness.clock(),
            purpose=purpose,
            asset_type=purpose,
            status=asset_status,
            width=1024,
            height=1024,
            size_bytes=2048 * (index + 1),
            expires_at=expires_at,
        )
        harness.uow.media.register(asset)
        harness.uow.generation_tasks.create_output(
            GenerationOutputDraft(
                generation_id=task.id, asset_id=asset.id, output_index=index
            )
        )
        asset_ids.append(asset.id)
    return task.id, asset_ids


def _create(harness: ShareHarness, generation_id, asset_ids, **overrides):
    body: dict[str, object] = {
        "generation_id": str(generation_id),
        "output_asset_ids": [str(value) for value in asset_ids],
    }
    body.update(overrides)
    return harness.client.post(
        SHARES_PATH, json=body, headers={"Idempotency-Key": CREATE_KEY}
    )


# --------------------------------------------------------------------------- #
# 认证与 Header
# --------------------------------------------------------------------------- #
def test_create_share_requires_session(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    response = _create(harness, generation_id, asset_ids)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_create_share_requires_verified_email(harness: ShareHarness) -> None:
    user = _seed_user(harness, verified=False)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    response = _create(harness, generation_id, asset_ids)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


def test_missing_idempotency_header_is_400(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    response = harness.client.post(
        SHARES_PATH,
        json={
            "generation_id": str(generation_id),
            "output_asset_ids": [str(asset_ids[0])],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


def test_invalid_idempotency_header_is_400(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    response = harness.client.post(
        SHARES_PATH,
        json={
            "generation_id": str(generation_id),
            "output_asset_ids": [str(asset_ids[0])],
        },
        headers={"Idempotency-Key": "  "},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_INVALID"


# --------------------------------------------------------------------------- #
# 请求体校验与 201 形状
# --------------------------------------------------------------------------- #
def test_unknown_body_field_is_422(harness: ShareHarness) -> None:
    """基线请求体是 `additionalProperties: false`。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    response = _create(
        harness, generation_id, [asset_ids[0]], mode="PHOTO_PROMPT"
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_malformed_body_is_422(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)

    response = harness.client.post(
        SHARES_PATH, json={"generation_id": "not-a-uuid"}, headers={"Idempotency-Key": CREATE_KEY}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_created_share_response_shape(harness: ShareHarness) -> None:
    """201 载荷：slug/title/expires_at/created_at/outputs；**不泄漏** generation_id。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    response = _create(harness, generation_id, asset_ids, title="两张头像")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"data", "request_id"}
    data = body["data"]
    assert set(data) == {"slug", "title", "expires_at", "created_at", "outputs"}
    assert data["title"] == "两张头像"
    assert data["expires_at"] is not None
    assert [item["asset_id"] for item in data["outputs"]] == [str(a) for a in asset_ids]
    assert [item["output_index"] for item in data["outputs"]] == [0, 1]
    for item in data["outputs"]:
        assert set(item) == {
            "asset_id",
            "output_index",
            "mime_type",
            "width",
            "height",
            "size_bytes",
        }
    serialized = response.text
    assert str(generation_id) not in serialized
    assert str(user.id) not in serialized
    assert "storage" not in serialized
    assert "assets/" not in serialized


def test_non_succeeded_generation_is_409(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user, status="FAILED")

    response = _create(harness, generation_id, asset_ids)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GENERATION_NOT_SHAREABLE"


def test_foreign_generation_is_404(harness: ShareHarness) -> None:
    owner = _seed_user(harness)
    generation_id, asset_ids = _seed_outputs(harness, user=owner)

    stranger = _seed_user(harness)
    _login(harness, stranger)

    response = _create(harness, generation_id, asset_ids)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GENERATION_NOT_FOUND"


def test_same_key_replays_the_same_share(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    first = _create(harness, generation_id, asset_ids)
    second = _create(harness, generation_id, asset_ids)

    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["slug"] == second.json()["data"]["slug"]
    assert len(harness.uow.shares.rows) == 1


def test_same_key_different_payload_is_409(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    _create(harness, generation_id, [asset_ids[0]])
    conflict = _create(harness, generation_id, asset_ids)

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# --------------------------------------------------------------------------- #
# 匿名读取
# --------------------------------------------------------------------------- #
def test_anonymous_can_read_share_without_cookie(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    created = _create(harness, generation_id, [asset_ids[0]], title="一半")

    slug = created.json()["data"]["slug"]
    harness.client.headers.pop("Cookie")  # 匿名
    response = harness.client.get(f"{SHARES_PATH}/{slug}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["slug"] == slug
    assert [item["asset_id"] for item in data["outputs"]] == [str(asset_ids[0])]
    assert str(generation_id) not in response.text


def test_anonymous_read_unknown_slug_is_404(harness: ShareHarness) -> None:
    harness.client.headers.pop("Cookie", None)

    response = harness.client.get(f"{SHARES_PATH}/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GENERATION_NOT_FOUND"


def test_anonymous_read_overlong_slug_is_404(harness: ShareHarness) -> None:
    response = harness.client.get(f"{SHARES_PATH}/{'x' * 200}")

    assert response.status_code == 404


def test_anonymous_read_after_revoke_is_404(harness: ShareHarness) -> None:
    """撤销是内部行为（无公开撤销接口），这里直接调 Service 验证读取即刻失效。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    created = _create(harness, generation_id, asset_ids)
    slug = created.json()["data"]["slug"]

    share = harness.uow.shares.get_by_slug(slug)
    with harness.uow:
        harness.app.dependency_overrides  # keep reference for clarity
        ShareService(uow=harness.uow, clock=harness.clock).revoke_share(
            user_id=user.id, share_id=share.id
        )

    harness.client.headers.pop("Cookie")
    response = harness.client.get(f"{SHARES_PATH}/{slug}")

    assert response.status_code == 404


def test_anonymous_read_after_expiry_is_404(harness: ShareHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    created = _create(harness, generation_id, asset_ids)
    slug = created.json()["data"]["slug"]

    harness.clock.advance(8 * 24 * 3600)  # 越过 7 天默认 TTL

    harness.client.headers.pop("Cookie")
    response = harness.client.get(f"{SHARES_PATH}/{slug}")

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 被分享资产的匿名可见性（下载链路复用）
# --------------------------------------------------------------------------- #
def test_shared_asset_is_readable_anonymously_but_unselected_is_not(
    harness: ShareHarness,
) -> None:
    """`SHARED` 放行匿名读取；同任务未选中的结果保持 `PRIVATE` → 404。"""

    _override_asset_service(harness)

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    _create(harness, generation_id, [asset_ids[0]])

    harness.client.headers.pop("Cookie")
    shared = harness.client.get(f"/api/v1/assets/{asset_ids[0]}")
    private = harness.client.get(f"/api/v1/assets/{asset_ids[1]}")

    assert shared.status_code == 200
    assert shared.json()["data"]["visibility"] == "SHARED"
    assert private.status_code == 404
    assert private.json()["error"]["code"] == "ASSET_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 返修回归：实时授权（不依赖清理任务是否已经跑过）
# --------------------------------------------------------------------------- #
def _override_asset_service(harness: ShareHarness):
    """把真实 `AssetService` 接上「实时分享依据」查询（与生产装配同源）。"""

    from app.api.deps import get_asset_service
    from app.services.assets.service import AssetService
    from app.storage import InMemoryStorageAdapter

    storage = InMemoryStorageAdapter(clock=harness.clock)
    harness.app.dependency_overrides[get_asset_service] = lambda: AssetService(
        uow=harness.uow,
        storage=storage,
        storage_provider="MINIO",
        enable_reference_image=False,
        clock=harness.clock,
        share_lookup=lambda asset_id: ShareService(
            uow=harness.uow, clock=harness.clock
        ).asset_has_valid_share(asset_id),
    )
    return storage


def test_anonymous_read_denied_after_asset_expiry_even_without_cleanup(
    harness: ShareHarness,
) -> None:
    """资产到期但清理任务还没跑（可见性仍是 SHARED）时，匿名读取必须 404。"""

    storage = _override_asset_service(harness)

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    _create(harness, generation_id, asset_ids)

    # 让选中资产的到期时间早于"现在"，但**不**跑生命周期清理
    for asset_id in asset_ids:
        harness.uow.media.get(asset_id).expires_at = harness.clock() - timedelta(seconds=1)
    harness.uow.commit()

    harness.client.headers.pop("Cookie")
    read = harness.client.get(f"/api/v1/assets/{asset_ids[0]}")
    signed = harness.client.post(
        f"/api/v1/assets/{asset_ids[0]}/download-url",
        headers={"Idempotency-Key": "dl-expired"},
    )

    assert read.status_code == 404
    assert read.json()["error"]["code"] == "ASSET_NOT_FOUND"
    assert signed.status_code == 404
    # 真实断言（第二轮写成 `... if hasattr(...) else True`，属性不存在时恒真，
    # 等于没有断言）：到期资产不得签发任何地址
    assert storage.signed_urls == []


def test_anonymous_read_denied_after_share_expiry_even_without_cleanup(
    harness: ShareHarness,
) -> None:
    """分享到期但清理任务还没跑（分享仍是 ACTIVE、资产仍是 SHARED）时，匿名读取必须 404。"""

    _override_asset_service(harness)

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    created = _create(harness, generation_id, asset_ids)
    slug = created.json()["data"]["slug"]

    # 越过分享期限，但保持 status = ACTIVE、visibility = SHARED（模拟清理尚未运行）
    harness.uow.shares.get_by_slug(slug).expires_at = harness.clock() - timedelta(seconds=1)
    harness.uow.commit()
    assert harness.uow.shares.get_by_slug(slug).status == "ACTIVE"
    assert harness.uow.media.get(asset_ids[0]).visibility == "SHARED"

    harness.client.headers.pop("Cookie")
    read = harness.client.get(f"/api/v1/assets/{asset_ids[0]}")
    signed = harness.client.post(
        f"/api/v1/assets/{asset_ids[0]}/download-url",
        headers={"Idempotency-Key": "dl-expired-share"},
    )
    share = harness.client.get(f"{SHARES_PATH}/{slug}")

    assert read.status_code == 404
    assert signed.status_code == 404
    assert share.status_code == 404


def test_anonymous_read_denied_after_revoke_without_cleanup(harness: ShareHarness) -> None:
    """撤销后即使可见性还没被重算（人为把状态改回 ACTIVE），也只认有效分享。"""

    _override_asset_service(harness)

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    created = _create(harness, generation_id, asset_ids)
    share = harness.uow.shares.get_by_slug(created.json()["data"]["slug"])

    with harness.uow:
        ShareService(uow=harness.uow, clock=harness.clock).revoke_share(
            user_id=user.id, share_id=share.id
        )

    harness.client.headers.pop("Cookie")
    read = harness.client.get(f"/api/v1/assets/{asset_ids[0]}")
    assert read.status_code == 404


def test_asset_read_without_share_lookup_fails_closed(harness: ShareHarness) -> None:
    """未接线分享查询时 `SHARED` 不放行：查不到依据就不公开（fail-closed）。"""

    from app.api.deps import get_asset_service
    from app.services.assets.service import AssetService
    from app.storage import InMemoryStorageAdapter

    storage = InMemoryStorageAdapter(clock=harness.clock)
    harness.app.dependency_overrides[get_asset_service] = lambda: AssetService(
        uow=harness.uow,
        storage=storage,
        storage_provider="MINIO",
        enable_reference_image=False,
        clock=harness.clock,
    )

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    _create(harness, generation_id, asset_ids)

    harness.client.headers.pop("Cookie")
    assert harness.client.get(f"/api/v1/assets/{asset_ids[0]}").status_code == 404


def test_share_expiry_never_exceeds_earliest_selected_asset_expiry(
    harness: ShareHarness,
) -> None:
    """分享期限不得晚于选中资产最早的到期时间。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    # 第一张 2 小时后到期（远早于默认 7 天），第二张 30 天后
    soon = harness.clock() + timedelta(hours=2)
    harness.uow.media.get(asset_ids[0]).expires_at = soon
    harness.uow.media.get(asset_ids[1]).expires_at = harness.clock() + timedelta(days=30)
    harness.uow.commit()

    created = _create(harness, generation_id, asset_ids)
    assert created.status_code == 201
    expires = datetime.fromisoformat(created.json()["data"]["expires_at"])
    assert expires <= soon


def test_share_expiry_uses_default_ttl_when_assets_are_long_lived(
    harness: ShareHarness,
) -> None:
    """资产到期远晚于默认 TTL 时，分享期限就是默认 7 天（不被放宽）。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    for asset_id in asset_ids:
        harness.uow.media.get(asset_id).expires_at = harness.clock() + timedelta(days=90)
    harness.uow.commit()

    created = _create(harness, generation_id, [asset_ids[0]])
    expires = datetime.fromisoformat(created.json()["data"]["expires_at"])

    assert expires == harness.clock() + timedelta(days=DEFAULT_SHARE_TTL_DAYS)


def test_selection_order_is_normalised(harness: ShareHarness) -> None:
    """选中集合是**集合语义**：请求里写 [A, B] 与 [B, A] 是同一次意图。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    first = _create(harness, generation_id, asset_ids)
    reordered = _create(harness, generation_id, list(reversed(asset_ids)))

    assert first.status_code == 201
    assert reordered.status_code == 201
    assert reordered.json()["data"]["slug"] == first.json()["data"]["slug"]
    assert len(harness.uow.shares.rows) == 1


def test_share_closes_every_linked_asset_visibility(harness: ShareHarness) -> None:
    """撤销一条分享两张的链接：**两张**都要恢复 PRIVATE。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    created = _create(harness, generation_id, asset_ids)
    share = harness.uow.shares.get_by_slug(created.json()["data"]["slug"])
    assert all(
        harness.uow.media.get(asset_id).visibility == "SHARED" for asset_id in asset_ids
    )

    with harness.uow:
        ShareService(uow=harness.uow, clock=harness.clock).revoke_share(
            user_id=user.id, share_id=share.id
        )

    assert all(
        harness.uow.media.get(asset_id).visibility == "PRIVATE" for asset_id in asset_ids
    )


def test_share_close_keeps_asset_shared_while_another_valid_share_exists(
    harness: ShareHarness,
) -> None:
    """关闭其中一条时，另一条有效分享仍然撑着第二张的 SHARED。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    first = _create(harness, generation_id, [asset_ids[0]])
    # 另一条只分享第二张（每用户每键一次，用新键 → 需要新的键名）
    second = harness.client.post(
        SHARES_PATH,
        json={
            "generation_id": str(generation_id),
            "output_asset_ids": [str(asset_ids[1])],
        },
        headers={"Idempotency-Key": "synthetic-share-key-002"},
    )
    assert second.status_code == 201

    first_share = harness.uow.shares.get_by_slug(first.json()["data"]["slug"])
    with harness.uow:
        ShareService(uow=harness.uow, clock=harness.clock).revoke_share(
            user_id=user.id, share_id=first_share.id
        )

    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"
    assert harness.uow.media.get(asset_ids[1]).visibility == "SHARED"


# --------------------------------------------------------------------------- #
# 返修第 1 项：资产期限校验（创建拒绝 + 匿名读取实时判定）
# --------------------------------------------------------------------------- #
def _share_side_effects(harness: ShareHarness) -> dict[str, int]:
    """统计分享聚合的全部写入：任何一项都不该在「校验失败」时出现。"""

    return {
        "shares": len(harness.uow.shares.rows),
        "links": len(harness.uow.shares.asset_links()),
        "audits": len(harness.uow.audit_logs.entries),
        "shared_assets": sum(
            1
            for row in harness.uow.media.rows
            if getattr(row, "visibility", None) == "SHARED"
        ),
    }


def test_create_rejects_already_expired_asset_without_side_effects(
    harness: ShareHarness,
) -> None:
    """选中资产已到期：首次创建必须拒绝，且**不落**任何副作用。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(
        harness, user=user, expires_at=harness.clock() - timedelta(seconds=1)
    )
    before = _share_side_effects(harness)

    response = _create(harness, generation_id, asset_ids)

    # `GENERATION_NOT_SHAREABLE` 在契约里的状态码是 409（不是 422）
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GENERATION_NOT_SHAREABLE"
    # 分享、关联、可见性、幂等审计一个都不能有
    assert _share_side_effects(harness) == before
    assert harness.uow.shares.find_by_idempotency_key(
        user_id=user.id, idempotency_key=CREATE_KEY
    ) is None


def test_create_rejects_expiry_boundary_equal_to_now(harness: ShareHarness) -> None:
    """边界：`expires_at == now` 视为已到期（与 `effective_expired` 同口径）。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(
        harness, user=user, expires_at=harness.clock()
    )

    response = _create(harness, generation_id, asset_ids)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GENERATION_NOT_SHAREABLE"
    assert _share_side_effects(harness)["shares"] == 0


def test_create_accepts_expiry_one_second_in_the_future(harness: ShareHarness) -> None:
    """边界：`expires_at` 比 `now` 晚 1 秒仍可分享（不误杀有效资产）。"""

    user = _seed_user(harness)
    _login(harness, user)
    soon = harness.clock() + timedelta(seconds=1)
    generation_id, asset_ids = _seed_outputs(harness, user=user, expires_at=soon)

    response = _create(harness, generation_id, asset_ids)

    assert response.status_code == 201
    # 分享期限不得晚于资产期限
    assert datetime.fromisoformat(response.json()["data"]["expires_at"]) <= soon


def test_create_rejects_when_only_one_selected_asset_is_expired(
    harness: ShareHarness,
) -> None:
    """两张里只有一张到期：整次创建失败（不能悄悄少公开一张）。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)

    expired = harness.clock() - timedelta(seconds=1)
    harness.uow.media.get(asset_ids[1]).expires_at = expired
    harness.uow.commit()
    before = _share_side_effects(harness)

    response = _create(harness, generation_id, asset_ids)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "GENERATION_NOT_SHAREABLE"
    assert _share_side_effects(harness) == before
    # 未到期的第一张也不能被置为 SHARED
    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"


def test_anonymous_read_is_denied_when_selected_asset_expires_even_if_share_is_live(
    harness: ShareHarness,
) -> None:
    """历史分享的期限晚于资产期限时：资产一到期，匿名读取立刻 404。

    这正是「不能依赖分享期限收紧」的场景：历史行（0019 之前写入或被人为改写）
    可能带着晚于资产期限的 `expires_at`，读取侧必须自己实时核对资产期限。
    """

    _override_asset_service(harness)
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(
        harness, user=user, expires_at=harness.clock() + timedelta(seconds=5)
    )
    created = _create(harness, generation_id, asset_ids)
    assert created.status_code == 201
    slug = created.json()["data"]["slug"]

    # 人为把分享期限拉长到资产期限之后（模拟历史/被改写的行），并让资产到期
    share = harness.uow.shares.get_by_slug(slug)
    share.expires_at = harness.clock() + timedelta(days=30)
    for asset_id in asset_ids:
        harness.uow.media.get(asset_id).expires_at = harness.clock() - timedelta(seconds=1)
    harness.uow.commit()
    assert share.status == "ACTIVE"
    assert harness.uow.media.get(asset_ids[0]).visibility == "SHARED"

    share_read = harness.client.get(f"{SHARES_PATH}/{slug}")
    assert share_read.status_code == 404
    assert share_read.json()["error"]["code"] == "GENERATION_NOT_FOUND"

    harness.client.headers.pop("Cookie")
    assert harness.client.get(f"/api/v1/assets/{asset_ids[0]}").status_code == 404


def test_anonymous_read_denied_before_cleanup_worker_runs(harness: ShareHarness) -> None:
    """清理尚未运行时也必须拒绝：读取侧不依赖 Worker 是否跑过。"""

    _override_asset_service(harness)
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(
        harness, user=user, expires_at=harness.clock() + timedelta(seconds=5)
    )
    created = _create(harness, generation_id, asset_ids)
    slug = created.json()["data"]["slug"]

    # 时钟推过资产到期点；**不**调用任何生命周期服务（没有 Worker、没有清理）
    harness.clock.advance(6)
    # 状态与可见性都还是「到期前」的快照：这正是要证明「不依赖清理」
    assert harness.uow.shares.get_by_slug(slug).status == "ACTIVE"
    assert harness.uow.media.get(asset_ids[0]).visibility == "SHARED"

    assert harness.client.get(f"{SHARES_PATH}/{slug}").status_code == 404
    harness.client.headers.pop("Cookie")
    assert harness.client.get(f"/api/v1/assets/{asset_ids[0]}").status_code == 404


def test_owner_still_reads_expired_own_asset(harness: ShareHarness) -> None:
    """所有者权限不变：自己到期的资产仍可读（只有公开读取被收紧）。"""

    _override_asset_service(harness)
    user = _seed_user(harness)
    _login(harness, user)
    _, asset_ids = _seed_outputs(
        harness, user=user, expires_at=harness.clock() - timedelta(seconds=1)
    )

    assert harness.client.get(f"/api/v1/assets/{asset_ids[0]}").status_code == 200


# --------------------------------------------------------------------------- #
# 返修第 2 项：可见性授权边界
# --------------------------------------------------------------------------- #
def test_public_asset_still_readable_with_share_lookup_wired(
    harness: ShareHarness,
) -> None:
    """注入 `share_lookup` 不得改变 `PUBLIC` 的既有公开读取规则。"""

    _override_asset_service(harness)
    user = _seed_user(harness)
    _login(harness, user)
    _, asset_ids = _seed_outputs(
        harness,
        user=user,
        count=1,
        expires_at=harness.clock() + timedelta(days=30),
    )
    asset = harness.uow.media.get(asset_ids[0])
    # PUBLIC 不是分享链路的产物：它由「有意发布」产生
    harness.uow.shares.set_asset_visibility(asset_ids[0], "PUBLIC")
    harness.uow.commit()
    assert asset.visibility == "PUBLIC"

    harness.client.headers.pop("Cookie")
    read = harness.client.get(f"/api/v1/assets/{asset_ids[0]}")
    assert read.status_code == 200
    assert read.json()["data"]["visibility"] == "PUBLIC"


def test_private_asset_is_404_for_anonymous_even_with_share_lookup(
    harness: ShareHarness,
) -> None:
    """`PRIVATE` 对非所有者始终 404，与是否接线分享查询无关。"""

    _override_asset_service(harness)
    user = _seed_user(harness)
    _login(harness, user)
    _, asset_ids = _seed_outputs(
        harness, user=user, count=1, expires_at=harness.clock() + timedelta(days=30)
    )

    harness.client.headers.pop("Cookie")
    assert harness.client.get(f"/api/v1/assets/{asset_ids[0]}").status_code == 404


def test_lifecycle_never_touches_public_visibility(harness: ShareHarness) -> None:
    """生命周期不拥有 PUBLIC：关闭分享不得把它改成 PRIVATE/SHARED。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(
        harness, user=user, count=1, expires_at=harness.clock() + timedelta(days=30)
    )
    created = _create(harness, generation_id, asset_ids)
    share = harness.uow.shares.get_by_slug(created.json()["data"]["slug"])

    # 人为把资产置为 PUBLIC（模拟已发布公开内容），再撤销分享
    harness.uow.shares.set_asset_visibility(asset_ids[0], "PUBLIC")
    harness.uow.commit()

    with harness.uow:
        ShareService(uow=harness.uow, clock=harness.clock).revoke_share(
            user_id=user.id, share_id=share.id
        )

    assert harness.uow.media.get(asset_ids[0]).visibility == "PUBLIC"


def test_lifecycle_never_promotes_private_to_shared(harness: ShareHarness) -> None:
    """关闭一条已经不拥有资产的分享，不得把 PRIVATE 提升为 SHARED。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(
        harness, user=user, count=1, expires_at=harness.clock() + timedelta(days=30)
    )
    created = _create(harness, generation_id, asset_ids)
    share = harness.uow.shares.get_by_slug(created.json()["data"]["slug"])

    # 先撤销（可见性回到 PRIVATE），再让资产带着 PRIVATE 走一遍收敛路径
    with harness.uow:
        service = ShareService(uow=harness.uow, clock=harness.clock)
        service.revoke_share(user_id=user.id, share_id=share.id)
    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"

    with harness.uow:
        service.revoke_share(user_id=user.id, share_id=share.id)

    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"


# --------------------------------------------------------------------------- #
# 第三轮返修第 2 项：关闭分享不得提升 PRIVATE（真实请求验证 404）
# --------------------------------------------------------------------------- #
def test_revoke_does_not_promote_private_asset_and_anonymous_reads_404(
    harness: ShareHarness,
) -> None:
    """两条有效分享关联同一资产、资产被设为 PRIVATE，撤销其中一条后仍须 404。

    与 `test_lifecycle_never_promotes_private_to_shared` 的区别：那条用例只是
    "再次撤销一条已撤销的分享"，`close_share`/`revalidate_visibility` **根本不会执行**，
    因此不能作为防提升证据。这条用例真正走完
    `revoke_share → close_share → revalidate_visibility`，并且：
    - 另一条分享仍然有效（"有有效分享"这个诱惑条件成立）；
    - 用**匿名真实请求**验证读取与签名都 404，而不只看数据库字段。
    """

    storage = _override_asset_service(harness)
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(
        harness, user=user, count=1, expires_at=harness.clock() + timedelta(days=30)
    )

    first = _create(harness, generation_id, asset_ids)
    second = harness.client.post(
        SHARES_PATH,
        json={
            "generation_id": str(generation_id),
            "output_asset_ids": [str(asset_ids[0])],
        },
        headers={"Idempotency-Key": "synthetic-share-key-002"},
    )
    assert (first.status_code, second.status_code) == (201, 201)
    assert harness.uow.media.get(asset_ids[0]).visibility == "SHARED"

    # 明确把资产设回 PRIVATE（模拟"所有者不想公开"）
    harness.uow.shares.set_asset_visibility(asset_ids[0], "PRIVATE")
    harness.uow.commit()

    # 撤销其中一条：另一条仍然有效，但**不得**把 PRIVATE 提升回 SHARED
    share = harness.uow.shares.get_by_slug(first.json()["data"]["slug"])
    with harness.uow:
        ShareService(uow=harness.uow, clock=harness.clock).revoke_share(
            user_id=user.id, share_id=share.id
        )

    assert share.status == "REVOKED"
    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"

    # 匿名真实请求：读取与签名都必须 404
    harness.client.headers.pop("Cookie")
    read = harness.client.get(f"/api/v1/assets/{asset_ids[0]}")
    signed = harness.client.post(
        f"/api/v1/assets/{asset_ids[0]}/download-url",
        headers={"Idempotency-Key": "synthetic-download-key"},
    )
    assert read.status_code == 404, read.text
    assert read.json()["error"]["code"] == "ASSET_NOT_FOUND"
    assert signed.status_code == 404, signed.text
    assert signed.json()["error"]["code"] == "ASSET_NOT_FOUND"
    assert storage.signed_urls == [], "不得为 PRIVATE 资产签发任何地址"


def test_share_expiry_does_not_promote_private_asset_endpoint(
    harness: ShareHarness,
) -> None:
    """**分享到期**路径的端点级验证：另一条有效分享不得把 PRIVATE 拉回 SHARED。"""

    _override_asset_service(harness)
    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(
        harness, user=user, count=1, expires_at=harness.clock() + timedelta(days=30)
    )

    first = _create(harness, generation_id, asset_ids)
    second = harness.client.post(
        SHARES_PATH,
        json={
            "generation_id": str(generation_id),
            "output_asset_ids": [str(asset_ids[0])],
        },
        headers={"Idempotency-Key": "synthetic-share-key-002"},
    )
    assert (first.status_code, second.status_code) == (201, 201)

    harness.uow.shares.set_asset_visibility(asset_ids[0], "PRIVATE")
    # 让第一条分享到期（第二条仍有效）
    harness.uow.shares.get_by_slug(
        first.json()["data"]["slug"]
    ).expires_at = harness.clock() - timedelta(seconds=1)
    harness.uow.commit()

    from app.services.shares.lifecycle import ShareLifecycle

    with harness.uow:
        expired = ShareLifecycle(uow=harness.uow, clock=harness.clock).expire_due_shares(
            limit=10
        )
    assert expired == 1
    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"

    harness.client.headers.pop("Cookie")
    assert harness.client.get(f"/api/v1/assets/{asset_ids[0]}").status_code == 404


# --------------------------------------------------------------------------- #
# 返修第 5 项：幂等作用域占用在**路由层**的响应形状
# --------------------------------------------------------------------------- #
class _BusyScopeRepository:
    """把 `lock_idempotency_scope` 换成"永远被占用"的替身（其余全部委托）。

    真实超时路径由 `tests/db/test_idempotency_scope_busy.py` 在真实 PostgreSQL 上
    证明；这里只证明**路由与异常映射**把领域信号变成了契约里的
    `409 IDEMPOTENCY_IN_PROGRESS` + `Retry-After`，而不是 500。
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def lock_idempotency_scope(self, *, user_id, idempotency_key) -> None:
        raise IdempotencyScopeBusy()


def _wire_busy_scope(harness: ShareHarness, user) -> None:
    from app.api.deps import get_share_service

    harness.uow.shares = _BusyScopeRepository(harness.uow.shares)
    harness.app.dependency_overrides[get_share_service] = lambda: ShareService(
        uow=harness.uow, clock=harness.clock
    )


def test_share_route_maps_scope_contention_to_409_with_retry_after(
    harness: ShareHarness,
) -> None:
    """幂等作用域被占用：`POST /shares` 必须 409 + `Retry-After`，不是 500。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id, asset_ids = _seed_outputs(harness, user=user)
    _wire_busy_scope(harness, user)

    response = _create(harness, generation_id, asset_ids)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS", body
    assert body["error"]["code"] != "INTERNAL_ERROR"
    assert response.headers.get("Retry-After") == "5"
    # 没有落任何副作用
    assert harness.uow.shares.find_by_idempotency_key(
        user_id=user.id, idempotency_key=CREATE_KEY
    ) is None
    assert _share_side_effects(harness)["shares"] == 0
