"""`POST /api/v1/analyses` 端点测试（「参考图片找灵感」）。

全部外部依赖以内存替身注入：不启动 PostgreSQL、Redis 与 MinIO，也不出网。

覆盖：认证与邮箱门槛、幂等 Header 的 400 映射、请求 Schema 拒绝未知字段与非法 UUID、
200 响应形状与 `request_id`、能力开关、归属/用途/状态三类拒绝、429 与 `Retry-After`、
闸门依赖不可用 503，以及 **REFERENCE 上传在只开启分析能力时放行**、
`REFERENCE_IMAGE` 生成模式不受影响这一条解耦裁决。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    SESSION_COOKIE_NAME,
    get_analysis_guard,
    get_analysis_service,
    get_asset_service,
    get_image_analyzer,
    get_redis_port,
    get_session_store,
    get_storage_adapter,
    get_uow,
)
from app.config import reset_settings_cache
from app.errors.codes import ErrorCode
from app.main import create_app
from app.providers.mock_analysis import MOCK_VISION_MODEL, MockImageAnalyzer
from app.services.analysis.guard import CACHE_TTL_SECONDS, RedisAnalysisGuard
from app.services.analysis.service import AnalysisService
from app.services.assets.service import AssetService
from app.storage import InMemoryStorageAdapter
from tests.services.fakes import FakeRedis, SetNxRedisClient, make_user_row
from tests.services.test_analysis import CLOCK, png_bytes, seed_asset
from tests.storage.fakes import AssetUnitOfWork

BASE_URL = "https://testserver"
SESSION_SECRET = "synthetic-session-secret-for-tests"
VALID_KEY = "synthetic-idempotency-key-001"
ANALYSES_PATH = "/api/v1/analyses"
UPLOADS_PATH = "/api/v1/assets/uploads"
USER_ID = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")
STRANGER = uuid.UUID("22223344-5566-7788-99aa-bbccddeeff00")


class AnalysisHarness:
    def __init__(
        self,
        app: FastAPI,
        client: TestClient,
        uow: AssetUnitOfWork,
        storage: InMemoryStorageAdapter,
        redis: FakeRedis,
        guard_client: SetNxRedisClient,
        analyzer: MockImageAnalyzer,
    ) -> None:
        self.app = app
        self.client = client
        self.uow = uow
        self.storage = storage
        self.redis = redis
        self.guard_client = guard_client
        self.analyzer = analyzer

    def reference_asset(self, content: bytes | None = None, **kwargs: object) -> uuid.UUID:
        """播种一张 READY 的参考图，返回资产 ID。"""

        return seed_asset(
            self.uow, self.storage, content if content is not None else png_bytes(), **kwargs
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[AnalysisHarness]:
    """默认开启分析能力（`ENABLE_IMAGE_ANALYSIS=true`），出图侧继续走 Mock。

    用例里要改能力开关或限额时，改环境变量 + `reset_settings_cache()` 即可：
    下面的服务覆盖是**惰性**读取 `get_settings()` 的，所以配置通路本身也被测到。
    """

    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("ENABLE_IMAGE_ANALYSIS", "true")
    monkeypatch.setenv("ENABLE_REFERENCE_IMAGE", "false")
    monkeypatch.setenv("AI_PROVIDER", "MOCK")
    reset_settings_cache()

    app = create_app()
    uow = AssetUnitOfWork(CLOCK)
    storage = InMemoryStorageAdapter(clock=CLOCK)
    redis = FakeRedis(clock=CLOCK)
    guard_client = SetNxRedisClient(clock=CLOCK)
    analyzer = MockImageAnalyzer()

    def _guard() -> RedisAnalysisGuard:
        from app.config import get_settings

        settings = get_settings()
        return RedisAnalysisGuard(
            guard_client,
            per_user_daily_limit=settings.analysis_daily_limit_per_user,
            global_daily_limit=settings.analysis_global_daily_limit,
            cache_ttl_seconds=CACHE_TTL_SECONDS,
            clock=CLOCK,
        )

    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_storage_adapter] = lambda: storage
    app.dependency_overrides[get_redis_port] = lambda: redis
    app.dependency_overrides[get_session_store] = lambda: _store(redis)
    app.dependency_overrides[get_image_analyzer] = lambda: analyzer
    app.dependency_overrides[get_analysis_guard] = _guard
    app.dependency_overrides[get_analysis_service] = lambda: AnalysisService(
        uow=uow,
        storage=storage,
        analyzer=analyzer,
        guard=_guard(),
        enabled=_settings().enable_image_analysis,
    )
    app.dependency_overrides[get_asset_service] = lambda: AssetService(
        uow=uow,
        storage=storage,
        storage_provider="MINIO",
        enable_reference_image=_settings().enable_reference_image,
        enable_image_analysis=_settings().enable_image_analysis,
        clock=CLOCK,
    )

    with TestClient(app, base_url=BASE_URL) as client:
        yield AnalysisHarness(app, client, uow, storage, redis, guard_client, analyzer)

    app.dependency_overrides.clear()
    reset_settings_cache()


def _settings():
    from app.config import get_settings

    return get_settings()


def _store(redis: FakeRedis):
    from app.services.auth.session_store import RedisSessionStore

    return RedisSessionStore(redis, secret=SESSION_SECRET)


def _seed_user(
    harness: AnalysisHarness, *, user_id: uuid.UUID = USER_ID, verified: bool = True
) -> uuid.UUID:
    user = make_user_row()
    user.id = user_id
    harness.uow.users.seed(user)
    email = harness.uow.emails.add(
        user_id=user_id, email=f"{user_id.hex[:6]}@example.com", is_primary=True
    )
    if verified:
        harness.uow.emails.mark_verified(email, CLOCK())
    return user_id


def _login(harness: AnalysisHarness, *, user_id: uuid.UUID = USER_ID) -> None:
    token, _record = _store(harness.redis).create(user_id=user_id, email_verified=True)
    harness.client.headers["Cookie"] = f"{SESSION_COOKIE_NAME}={token}"


def _signed_in(harness: AnalysisHarness, *, verified: bool = True) -> uuid.UUID:
    user_id = _seed_user(harness, verified=verified)
    _login(harness, user_id=user_id)
    return user_id


def _post(harness: AnalysisHarness, asset_id: object, *, key: str | None = VALID_KEY):
    headers = {} if key is None else {"Idempotency-Key": key}
    return harness.client.post(ANALYSES_PATH, json={"asset_id": str(asset_id)}, headers=headers)


# ------------------------------------------------------------------ 认证与请求形状
def test_anonymous_request_is_rejected(harness: AnalysisHarness) -> None:
    response = harness.client.post(
        ANALYSES_PATH, json={"asset_id": str(uuid.uuid4())}, headers={"Idempotency-Key": VALID_KEY}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTH_REQUIRED.value


def test_missing_idempotency_key_is_400_not_422(harness: AnalysisHarness) -> None:
    _signed_in(harness)

    response = harness.client.post(ANALYSES_PATH, json={"asset_id": str(uuid.uuid4())})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_REQUIRED.value


def test_whitespace_idempotency_key_is_400(harness: AnalysisHarness) -> None:
    _signed_in(harness)

    response = harness.client.post(
        ANALYSES_PATH,
        json={"asset_id": str(uuid.uuid4())},
        headers={"Idempotency-Key": "   "},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_INVALID.value


def test_unknown_body_field_is_rejected(harness: AnalysisHarness) -> None:
    """基线 `additionalProperties: false`：不接受 `model`、`purpose` 之类的客户端自报字段。"""

    user_id = _signed_in(harness)
    asset_id = harness.reference_asset(owner=user_id)

    response = harness.client.post(
        ANALYSES_PATH,
        json={"asset_id": str(asset_id), "model": "gpt-whatever"},
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 422
    assert harness.analyzer.calls == []


def test_malformed_asset_id_is_422(harness: AnalysisHarness) -> None:
    _signed_in(harness)

    response = _post(harness, "not-a-uuid")

    assert response.status_code == 422


# ------------------------------------------------------------------ 成功路径
def test_reference_image_returns_an_editable_description(harness: AnalysisHarness) -> None:
    user_id = _signed_in(harness)
    asset_id = harness.reference_asset(owner=user_id)

    response = _post(harness, asset_id)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["asset_id"] == str(asset_id)
    assert data["model"] == MOCK_VISION_MODEL
    assert data["from_cache"] is False
    assert data["description"].strip()
    assert len(data["description"]) <= 600
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_response_never_carries_image_or_internal_cost_fields(
    harness: AnalysisHarness,
) -> None:
    _signed_in(harness)
    asset_id = harness.reference_asset()

    body = _post(harness, asset_id).json()

    assert set(body) == {"data", "request_id"}
    assert set(body["data"]) == {"asset_id", "description", "model", "from_cache"}
    for banned in ("url", "storage_key", "usage", "input_tokens", "cost", "content"):
        assert banned not in body["data"]


def test_second_request_for_the_same_image_is_served_from_cache_without_a_second_call(
    harness: AnalysisHarness,
) -> None:
    """重复点同一个按钮不该再付一次钱——这条是本次能力最关键的验收点。"""

    user_id = _signed_in(harness)
    asset_id = harness.reference_asset(owner=user_id)

    first = _post(harness, asset_id, key="key-a").json()["data"]
    second = _post(harness, asset_id, key="key-b").json()["data"]

    assert first["from_cache"] is False
    assert second["from_cache"] is True
    assert second["description"] == first["description"]
    assert len(harness.analyzer.calls) == 1


def test_two_different_images_get_two_descriptions(harness: AnalysisHarness) -> None:
    user_id = _signed_in(harness)
    first_asset = harness.reference_asset(png_bytes(size=(8, 6)), owner=user_id)
    second_asset = harness.reference_asset(png_bytes(size=(9, 7)), owner=user_id)

    first = _post(harness, first_asset).json()["data"]
    second = _post(harness, second_asset).json()["data"]

    assert first["from_cache"] is False and second["from_cache"] is False
    assert len(harness.analyzer.calls) == 2


def test_identical_picture_under_two_asset_ids_is_not_charged_twice(
    harness: AnalysisHarness,
) -> None:
    user_id = _signed_in(harness)
    content = png_bytes(size=(12, 12))
    a = harness.reference_asset(content, owner=user_id)
    b = harness.reference_asset(content, owner=user_id)

    _post(harness, a)
    replay = _post(harness, b).json()["data"]

    assert replay["from_cache"] is True
    assert len(harness.analyzer.calls) == 1


# ------------------------------------------------------------------ 能力开关与门槛
def test_disabled_capability_returns_feature_not_available(
    harness: AnalysisHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_IMAGE_ANALYSIS", "false")
    reset_settings_cache()

    user_id = _signed_in(harness)
    asset_id = harness.reference_asset(owner=user_id)

    response = _post(harness, asset_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == ErrorCode.FEATURE_NOT_AVAILABLE.value
    assert harness.analyzer.calls == []


def test_unverified_email_cannot_trigger_a_paid_call(harness: AnalysisHarness) -> None:
    _signed_in(harness, verified=False)
    asset_id = harness.reference_asset()

    response = _post(harness, asset_id)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.EMAIL_NOT_VERIFIED.value


# ------------------------------------------------------------------ 归属 / 用途 / 状态
def test_unknown_asset_is_404(harness: AnalysisHarness) -> None:
    _signed_in(harness)

    response = _post(harness, uuid.uuid4())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.ASSET_NOT_FOUND.value


def test_another_users_asset_is_403_and_never_read(harness: AnalysisHarness) -> None:
    _seed_user(harness, user_id=USER_ID, verified=True)
    owner = _seed_user(harness, user_id=STRANGER, verified=True)
    asset_id = harness.reference_asset(owner=owner)
    _login(harness, user_id=USER_ID)

    response = _post(harness, asset_id)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.ASSET_FORBIDDEN.value
    assert harness.analyzer.calls == []


def test_source_asset_cannot_be_read_as_a_reference(harness: AnalysisHarness) -> None:
    """把「改造用原图」当参考图读是用途错位：必须 422，而不是悄悄看一眼。"""

    user_id = _signed_in(harness)
    asset_id = harness.reference_asset(owner=user_id, purpose="SOURCE")

    response = _post(harness, asset_id)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert harness.analyzer.calls == []


@pytest.mark.parametrize("status", ["PENDING_UPLOAD", "DELETED", "EXPIRED"])
def test_asset_must_be_ready(harness: AnalysisHarness, status: str) -> None:
    user_id = _signed_in(harness)
    asset_id = harness.reference_asset(owner=user_id, status=status)

    response = _post(harness, asset_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.ASSET_NOT_READY.value


# ------------------------------------------------------------------ 配额与依赖
def test_daily_limit_returns_429_with_retry_after(
    harness: AnalysisHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANALYSIS_DAILY_LIMIT_PER_USER", "2")
    reset_settings_cache()

    user_id = _signed_in(harness)
    assets = [
        harness.reference_asset(png_bytes(size=(size, size)), owner=user_id)
        for size in (8, 9, 10, 11)
    ]

    for asset_id in assets[:2]:
        assert _post(harness, asset_id).status_code == 200

    blocked = _post(harness, assets[2])

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == ErrorCode.RATE_LIMITED.value
    assert int(blocked.headers["retry-after"]) > 0
    # 被限流的这次绝不能已经出网
    assert len(harness.analyzer.calls) == 2


def test_reference_upload_is_still_refused_when_nothing_is_enabled(
    harness: AnalysisHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个开关都关着时，`purpose=REFERENCE` 依旧拒绝——解耦不等于放开。"""

    monkeypatch.setenv("ENABLE_IMAGE_ANALYSIS", "false")
    reset_settings_cache()

    _signed_in(harness)

    response = harness.client.post(
        UPLOADS_PATH,
        json={
            "purpose": "REFERENCE",
            "filename": "inspo.png",
            "mime_type": "image/png",
            "size_bytes": 64,
            "sha256": hashlib.sha256(b"x").hexdigest(),
        },
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == ErrorCode.FEATURE_NOT_AVAILABLE.value


def test_reference_upload_is_allowed_by_the_analysis_flag_alone(
    harness: AnalysisHarness,
) -> None:
    """本次裁决的核心：只开分析能力（不开参考图生成）也必须能上传灵感参考图。"""

    _signed_in(harness)
    content = png_bytes()

    response = harness.client.post(
        UPLOADS_PATH,
        json={
            "purpose": "REFERENCE",
            "filename": "inspo.png",
            "mime_type": "image/png",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["purpose"] == "REFERENCE"
    assert response.json()["data"]["status"] == "PENDING_UPLOAD"


def test_reference_upload_still_works_with_the_generation_flag(
    harness: AnalysisHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """另一侧也照常：开 `ENABLE_REFERENCE_IMAGE`、关分析，上传不受影响。"""

    monkeypatch.setenv("ENABLE_IMAGE_ANALYSIS", "false")
    monkeypatch.setenv("ENABLE_REFERENCE_IMAGE", "true")
    reset_settings_cache()

    _signed_in(harness)
    content = png_bytes()

    response = harness.client.post(
        UPLOADS_PATH,
        json={
            "purpose": "REFERENCE",
            "filename": "inspo.png",
            "mime_type": "image/png",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        headers={"Idempotency-Key": VALID_KEY},
    )

    assert response.status_code == 201, response.text


def test_guard_dependency_outage_fails_closed_with_503(harness: AnalysisHarness) -> None:
    """Redis 连不上时宁可拒绝，也不能「看不见额度」就继续出网。"""

    user_id = _signed_in(harness)
    asset_id = harness.reference_asset(owner=user_id)

    class Down:
        def get(self, name):
            raise ConnectionError("redis down")

        def set(self, name, value, *, ex=None, nx=False):
            raise ConnectionError("redis down")

        def incr(self, name):
            raise ConnectionError("redis down")

        def expire(self, name, seconds):
            raise ConnectionError("redis down")

        def delete(self, *names):
            raise ConnectionError("redis down")

    harness.app.dependency_overrides[get_analysis_service] = lambda: AnalysisService(
        uow=harness.uow,
        storage=harness.storage,
        analyzer=harness.analyzer,
        guard=RedisAnalysisGuard(
            Down(), per_user_daily_limit=10, global_daily_limit=100, clock=CLOCK
        ),
        enabled=True,
    )

    response = _post(harness, asset_id)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.NOT_READY.value
    assert harness.analyzer.calls == []
