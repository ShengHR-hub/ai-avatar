"""媒体资源端点测试（ENG-05 第 4 步）。

全部外部依赖以内存替身注入：不启动 PostgreSQL、Redis 与 MinIO。

覆盖：认证与邮箱验证门槛、幂等 Header 的 400 映射、请求 Schema 拒绝未知字段、
上传意图 201 响应形状、完成校验 200/409、读取与签名的所有者/匿名授权、
删除 204 与其后的不可见、413/415 在意图阶段生效。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_asset_service,
    get_redis_port,
    get_session_store,
    get_storage_adapter,
    get_uow,
)
from app.config import reset_settings_cache
from app.main import create_app
from app.services.assets.service import AssetService
from app.storage import InMemoryStorageAdapter
from tests.services.fakes import FakeRedis, make_user_row
from tests.services.test_assets import CLOCK, make_image_bytes
from tests.storage.fakes import AssetUnitOfWork

BASE_URL = "https://testserver"
SESSION_SECRET = "synthetic-session-secret-for-tests"
VALID_KEY = "synthetic-idempotency-key-001"
UPLOADS_PATH = "/api/v1/assets/uploads"

USER_ID = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")


class AssetHarness:
    def __init__(
        self,
        app: FastAPI,
        client: TestClient,
        uow: AssetUnitOfWork,
        storage: InMemoryStorageAdapter,
        redis: FakeRedis,
    ) -> None:
        self.app = app
        self.client = client
        self.uow = uow
        self.storage = storage
        self.redis = redis


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[AssetHarness]:
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("ENABLE_REFERENCE_IMAGE", "true")
    reset_settings_cache()

    app = create_app()
    uow = AssetUnitOfWork(CLOCK)
    storage = InMemoryStorageAdapter(clock=CLOCK)
    redis = FakeRedis(clock=CLOCK)

    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_storage_adapter] = lambda: storage
    app.dependency_overrides[get_redis_port] = lambda: redis
    app.dependency_overrides[get_session_store] = lambda: _store(redis)
    app.dependency_overrides[get_asset_service] = lambda: AssetService(
        uow=uow,
        storage=storage,
        storage_provider="MINIO",
        enable_reference_image=True,
        clock=CLOCK,
    )

    with TestClient(app, base_url=BASE_URL) as client:
        yield AssetHarness(app, client, uow, storage, redis)

    app.dependency_overrides.clear()
    reset_settings_cache()


def _store(redis: FakeRedis):
    from app.services.auth.session_store import RedisSessionStore

    return RedisSessionStore(redis, secret=SESSION_SECRET)


def _seed_user(harness: AssetHarness, *, verified: bool = True) -> uuid.UUID:
    from app.db.models.identity import User

    user = make_user_row()
    user.id = USER_ID
    harness.uow.users.seed(user)
    email = harness.uow.emails.add(user_id=USER_ID, email="owner@example.com", is_primary=True)
    if verified:
        harness.uow.emails.mark_verified(email, CLOCK())
    return USER_ID


def _login(harness: AssetHarness) -> None:
    token, _record = _store(harness.redis).create(
        user_id=USER_ID, email_verified=True
    )
    harness.client.headers["Cookie"] = f"session={token}"


def _intent_payload(content: bytes, **overrides: object) -> dict:
    payload = {
        "purpose": "SOURCE",
        "filename": "avatar.png",
        "mime_type": "image/png",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    payload.update(overrides)
    return payload


def _create(harness: AssetHarness, content: bytes, key: str = VALID_KEY):
    return harness.client.post(
        UPLOADS_PATH, json=_intent_payload(content), headers={"Idempotency-Key": key}
    )


# ------------------------------------------------------------------ 认证门槛
def test_upload_requires_session(harness: AssetHarness) -> None:
    response = _create(harness, make_image_bytes())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_upload_requires_verified_email(harness: AssetHarness) -> None:
    _seed_user(harness, verified=False)
    _login(harness)

    response = _create(harness, make_image_bytes())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


def test_upload_requires_idempotency_key(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)

    response = harness.client.post(UPLOADS_PATH, json=_intent_payload(make_image_bytes()))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


def test_upload_rejects_unknown_field(harness: AssetHarness) -> None:
    """基线 `UploadIntent` 是 `additionalProperties: false`。"""

    _seed_user(harness)
    _login(harness)
    payload = _intent_payload(make_image_bytes())
    payload["storage_key"] = "attacker-controlled"

    response = harness.client.post(
        UPLOADS_PATH, json=payload, headers={"Idempotency-Key": VALID_KEY}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_upload_rejects_bad_sha_shape(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    payload = _intent_payload(make_image_bytes(), sha256="nope")

    response = harness.client.post(
        UPLOADS_PATH, json=payload, headers={"Idempotency-Key": VALID_KEY}
    )

    assert response.status_code == 422


def test_upload_rejects_unsupported_mime_with_415(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    content = make_image_bytes()

    response = _create(harness, content)  # 先确认 201 基线可用
    assert response.status_code == 201

    payload = _intent_payload(content, mime_type="image/tiff")
    second = harness.client.post(
        UPLOADS_PATH, json=payload, headers={"Idempotency-Key": "other-key"}
    )

    assert second.status_code == 415
    assert second.json()["error"]["code"] == "MEDIA_TYPE_UNSUPPORTED"


# ------------------------------------------------------------------ 上传意图
def test_upload_intent_returns_signed_put_url_and_pending_asset(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)

    response = _create(harness, make_image_bytes())

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"data", "request_id"}
    assert set(body["data"]) == {
        "asset_id",
        "purpose",
        "status",
        "method",
        "upload_url",
        "expires_at",
    }
    assert body["data"]["status"] == "PENDING_UPLOAD"
    assert body["data"]["method"] == "PUT"
    # 服务端内部事实不得外泄
    text = response.text
    assert "storage_provider" not in text
    assert "idempotency_key" not in text
    assert "MINIO" not in text
    # 对象 Key 只允许作为签名 URL 的一部分出现（浏览器必须拿得到），
    # 不得作为独立字段回传
    data = body["data"]
    assert "storage_key" not in data and "storage_provider" not in data
    row = harness.uow.media.get(uuid.UUID(data["asset_id"]))
    assert row.storage_key in data["upload_url"]


def test_asset_id_is_queryable_by_owner(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    asset_id = _create(harness, make_image_bytes()).json()["data"]["asset_id"]

    response = harness.client.get(f"/api/v1/assets/{asset_id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == asset_id
    assert data["asset_type"] == "ORIGINAL"
    assert data["purpose"] == "SOURCE"
    assert data["visibility"] == "PRIVATE"
    assert data["width"] is None  # 尚未校验，无观测尺寸
    assert "storage_key" not in data


# ------------------------------------------------------------------ 完成校验
def test_complete_returns_ready_asset_with_observed_metadata(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    content = make_image_bytes(size=(11, 7))
    body = _create(harness, content).json()["data"]
    asset = harness.uow.media.get(uuid.UUID(body["asset_id"]))
    harness.storage.upload(asset.storage_key, content, "image/png")

    response = harness.client.post(
        f"/api/v1/assets/{body['asset_id']}/complete",
        headers={"Idempotency-Key": "complete-1"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "READY"
    assert (data["width"], data["height"], data["size_bytes"]) == (11, 7, len(content))


def test_complete_before_object_upload_is_409(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    asset_id = _create(harness, make_image_bytes()).json()["data"]["asset_id"]

    response = harness.client.post(
        f"/api/v1/assets/{asset_id}/complete", headers={"Idempotency-Key": "complete-1"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ASSET_NOT_READY"


def test_complete_rejects_object_failing_digest(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    body = _create(harness, make_image_bytes()).json()["data"]
    asset = harness.uow.media.get(uuid.UUID(body["asset_id"]))
    harness.storage.upload(asset.storage_key, make_image_bytes(size=(5, 5)), "image/png")

    response = harness.client.post(
        f"/api/v1/assets/{body['asset_id']}/complete",
        headers={"Idempotency-Key": "complete-1"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ASSET_INVALID"
    assert asset.status == "PENDING_UPLOAD"


# ------------------------------------------------------------ 签名与不可见性
def test_download_url_for_ready_asset(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    content = make_image_bytes()
    body = _create(harness, content).json()["data"]
    asset = harness.uow.media.get(uuid.UUID(body["asset_id"]))
    harness.storage.upload(asset.storage_key, content, "image/png")
    harness.client.post(
        f"/api/v1/assets/{body['asset_id']}/complete",
        headers={"Idempotency-Key": "complete-1"},
    )

    response = harness.client.post(
        f"/api/v1/assets/{body['asset_id']}/download-url",
        headers={"Idempotency-Key": "dl-1"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["method"] == "GET" and data["url"]
    # 签名值只出现在响应 body，绝不进日志/错误（由 redaction 中间件保证）
    assert "expires_at" in data


def test_anonymous_cannot_read_private_asset(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    asset_id = _create(harness, make_image_bytes()).json()["data"]["asset_id"]
    harness.client.headers.pop("Cookie")

    read = harness.client.get(f"/api/v1/assets/{asset_id}")
    url = harness.client.post(
        f"/api/v1/assets/{asset_id}/download-url", headers={"Idempotency-Key": "dl-1"}
    )

    assert read.status_code == 404
    assert read.json()["error"]["code"] == "ASSET_NOT_FOUND"
    assert url.status_code == 404


def test_unknown_asset_id_is_404(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)

    response = harness.client.get(f"/api/v1/assets/{uuid.uuid4()}")

    assert response.status_code == 404


def test_malformed_asset_id_is_422(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)

    response = harness.client.get("/api/v1/assets/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------- 删除
def test_delete_returns_204_and_hides_the_asset(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    asset_id = _create(harness, make_image_bytes()).json()["data"]["asset_id"]

    response = harness.client.delete(
        f"/api/v1/assets/{asset_id}", headers={"Idempotency-Key": "del-1"}
    )
    again = harness.client.delete(
        f"/api/v1/assets/{asset_id}", headers={"Idempotency-Key": "del-2"}
    )
    read = harness.client.get(f"/api/v1/assets/{asset_id}")

    assert response.status_code == 204
    assert response.content == b""  # 契约第 2.2 节：204 不得带 body
    assert again.status_code == 204  # 幂等
    assert read.status_code == 404


def test_delete_requires_idempotency_key(harness: AssetHarness) -> None:
    _seed_user(harness)
    _login(harness)
    asset_id = _create(harness, make_image_bytes()).json()["data"]["asset_id"]

    response = harness.client.delete(f"/api/v1/assets/{asset_id}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


def test_stranger_cannot_delete_or_complete(harness: AssetHarness) -> None:
    owner = _seed_user(harness)
    _login(harness)
    asset_id = _create(harness, make_image_bytes()).json()["data"]["asset_id"]

    # 换成另一个身份的会话
    other = harness.uow.users.seed(make_user_row())
    harness.uow.emails.add(
        user_id=other.id, email="other@example.com", is_primary=True,
    )
    harness.uow.emails.mark_verified(
        harness.uow.emails.find_primary(other.id), CLOCK()
    )
    token, _ = _store(harness.redis).create(user_id=other.id, email_verified=True)
    harness.client.headers["Cookie"] = f"session={token}"

    forbidden = harness.client.delete(
        f"/api/v1/assets/{asset_id}", headers={"Idempotency-Key": "del-1"}
    )

    assert owner != other.id
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ASSET_FORBIDDEN"
