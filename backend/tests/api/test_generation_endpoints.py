"""生成任务端点测试（ENG-06 第 5 步）。

全部外部依赖以内存替身注入：不启动 PostgreSQL、Redis 与对象存储，也不调用真实供应商。

覆盖的是**端点层**该管的事：认证与邮箱门槛、幂等 Header 的 400 映射、请求 Schema 的
`additionalProperties: false` 与 `const: 2`、`202` 响应体逐字段对齐冻结基线
`Generation`、越权与不存在统一 404、键集分页、重试只接受 `FAILED`，
以及「投递必须发生在提交之后」这条顺序在 HTTP 路径上同样成立。

状态机与结算矩阵本身在 `tests/services/test_generation_*` 覆盖，此处不重复。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_generation_service,
    get_redis_port,
    get_session_store,
    get_uow,
)
from app.config import reset_settings_cache
from app.main import create_app
from app.services.generation.service import GenerationService
from app.services.points.service import PointsService
from tests.services.fakes import (
    FakeRedis,
    GenerationUnitOfWork,
    MutableClock,
    RecordingGenerationQueuePort,
    make_generation_cost_config,
    make_media_asset_row,
    make_user_row,
)

BASE_URL = "https://testserver"
SESSION_SECRET = "synthetic-session-secret-for-tests"
GENERATIONS_PATH = "/api/v1/generations"
CREATE_KEY = "synthetic-create-key-001"
POINTS_COST = 10
USER_ID = uuid.UUID("11223344-5566-7788-99aa-bbccddeeff00")
OTHER_ID = uuid.UUID("99887766-5544-3322-1100-ffeeddccbbaa")


class Harness:
    def __init__(
        self,
        app: FastAPI,
        client: TestClient,
        uow: GenerationUnitOfWork,
        queue: RecordingGenerationQueuePort,
        redis: FakeRedis,
        clock: MutableClock,
    ) -> None:
        self.app = app
        self.client = client
        self.uow = uow
        self.queue = queue
        self.redis = redis
        self.clock = clock


class _CommitAwareQueue(RecordingGenerationQueuePort):
    """记录「投递发生时已经提交了几次」，用于证明投递只在提交之后。"""

    def __init__(self, uow: GenerationUnitOfWork) -> None:
        super().__init__()
        self._uow = uow
        self.commits_at_enqueue: list[int] = []

    def enqueue_generation(self, generation_id, *, countdown_seconds: int = 0) -> bool:
        self.commits_at_enqueue.append(self._uow.commits)
        return super().enqueue_generation(generation_id, countdown_seconds=countdown_seconds)


@pytest.fixture
def harness() -> Iterator[Harness]:
    clock = MutableClock()
    app = create_app()
    uow = GenerationUnitOfWork(clock=clock)
    redis = FakeRedis(clock=clock)
    queue = _CommitAwareQueue(uow)

    uow.generation_costs.seed(
        make_generation_cost_config(mode="PHOTO_PROMPT", output_count=2, points_cost=POINTS_COST)
    )
    uow.generation_costs.seed(
        make_generation_cost_config(mode="TEXT_PROMPT", output_count=2, points_cost=POINTS_COST)
    )

    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_redis_port] = lambda: redis
    app.dependency_overrides[get_session_store] = lambda: _store(redis)
    app.dependency_overrides[get_generation_service] = lambda: GenerationService(
        uow=uow,
        points=PointsService(uow=uow, clock=clock),
        clock=clock,
        queue=queue,
        enable_reference_image=False,
    )

    with TestClient(app, base_url=BASE_URL) as client:
        yield Harness(app, client, uow, queue, redis, clock)

    app.dependency_overrides.clear()
    reset_settings_cache()


def _store(redis: FakeRedis):
    from app.services.auth.session_store import RedisSessionStore

    return RedisSessionStore(redis, secret=SESSION_SECRET)


def _seed_user(harness: Harness, user_id: uuid.UUID = USER_ID, *, verified: bool = True) -> uuid.UUID:
    user = make_user_row(now=harness.clock())
    user.id = user_id
    harness.uow.users.seed(user)
    email = harness.uow.emails.add(user_id=user_id, email=f"{user_id.hex[:6]}@example.com", is_primary=True)
    if verified:
        harness.uow.emails.mark_verified(email, harness.clock())
    harness.uow.point_wallets.seed(user_id, available_points=100, lifetime_credited=100)
    return user_id


def _login(harness: Harness, user_id: uuid.UUID = USER_ID) -> None:
    token, _record = _store(harness.redis).create(user_id=user_id, email_verified=True)
    harness.client.headers["Cookie"] = f"session={token}"


def _ready_asset(harness: Harness, user_id: uuid.UUID = USER_ID) -> uuid.UUID:
    asset = make_media_asset_row(
        user_id=user_id,
        purpose="SOURCE",
        asset_type="ORIGINAL",
        status="READY",
        sha256=hashlib.sha256(b"synthetic-input").hexdigest(),
    )
    harness.uow.media.register(asset)
    return asset.id


def _body(**overrides: object) -> dict:
    payload: dict = {
        "mode": "PHOTO_PROMPT",
        "prompt": "保持人物特征，生成专业头像",
        "input_asset_ids": [],
        "output_count": 2,
    }
    payload.update(overrides)
    return payload


def _create(
    harness: Harness,
    *,
    key: str = CREATE_KEY,
    headers: dict[str, str] | None = None,
    **overrides: object,
):
    sent = {"Idempotency-Key": key} if headers is None else headers
    return harness.client.post(GENERATIONS_PATH, json=_body(**overrides), headers=sent)


# --------------------------------------------------------------------------- #
# 认证与门禁
# --------------------------------------------------------------------------- #
def test_create_requires_session(harness: Harness) -> None:
    response = _create(harness)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_create_requires_verified_email(harness: Harness) -> None:
    _seed_user(harness, verified=False)
    _login(harness)

    response = _create(harness)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


def test_create_requires_idempotency_key(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    response = harness.client.post(GENERATIONS_PATH, json=_body())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


def test_create_rejects_overlong_idempotency_key(harness: Harness) -> None:
    # Header 值只能承载 ASCII，非 ASCII 键在 HTTP 层根本发不出来；
    # 这里用契约上限（1-128）这一侧来验证同一个格式校验分支
    _seed_user(harness)
    _login(harness)

    response = _create(harness, key="k" * 129)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_INVALID"


def test_reference_image_is_rejected_before_any_write(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    asset = _ready_asset(harness)

    response = _create(harness, mode="REFERENCE_IMAGE", input_asset_ids=[str(asset)])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FEATURE_NOT_AVAILABLE"
    assert harness.uow.generation_tasks.rows == ()
    assert harness.uow.point_ledger.entries == ()
    assert harness.queue.generations == []


# --------------------------------------------------------------------------- #
# 请求 Schema：基线的 `const: 2` 与 `additionalProperties: false`
# --------------------------------------------------------------------------- #
def test_create_rejects_unknown_field(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    response = _create(harness, provider_hint="gpt")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("count", [1, 3, 0])
def test_create_only_accepts_two_outputs(harness: Harness, count: int) -> None:
    _seed_user(harness)
    _login(harness)

    response = _create(harness, output_count=count)

    assert response.status_code == 422
    assert harness.uow.generation_tasks.rows == ()


def test_create_rejects_empty_prompt_and_unknown_mode(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    assert _create(harness, prompt="").status_code == 422
    assert _create(harness, mode="NOT_A_MODE").status_code == 422
    assert harness.uow.generation_tasks.rows == ()


# --------------------------------------------------------------------------- #
# 成功路径与响应形状
# --------------------------------------------------------------------------- #
def test_create_returns_202_with_frozen_generation_body(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    asset = _ready_asset(harness)

    response = _create(harness, input_asset_ids=[str(asset)])

    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert set(data) == {
        "id",
        "parent_generation_id",
        "mode",
        "status",
        "billing_status",
        "prompt",
        "requested_count",
        "reserved_points",
        "failure_code",
        "created_at",
        "updated_at",
    }
    assert data["status"] == "QUEUED"
    assert data["billing_status"] == "RESERVED"
    assert data["requested_count"] == 2
    assert data["reserved_points"] == POINTS_COST
    assert data["parent_generation_id"] is None
    assert data["failure_code"] is None
    assert set(response.json()) == {"data", "request_id"}


def test_create_reserves_once_and_enqueues_only_after_commit(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    response = _create(harness, mode="TEXT_PROMPT")
    generation_id = uuid.UUID(response.json()["data"]["id"])

    assert [entry.entry_type for entry in harness.uow.point_ledger.entries] == ["RESERVE"]
    wallet = harness.uow.point_wallets.get_by_user_id(USER_ID)
    assert (wallet.available_points, wallet.reserved_points) == (
        100 - POINTS_COST,
        POINTS_COST,
    )
    assert [item.generation_id for item in harness.queue.generations] == [generation_id]
    # 投递必须在提交之后：`LEAD-04` §6:119 规定 Celery 只在事务提交后投递，
    # 且投递失败不回滚已提交账务
    assert harness.queue.commits_at_enqueue == [1]
    assert harness.uow.commits == 1
    assert harness.uow.rollbacks == 0


def test_insufficient_points_is_409_and_writes_nothing(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    wallet = harness.uow.point_wallets.get_by_user_id(USER_ID)
    wallet.available_points = 1

    response = _create(harness, mode="TEXT_PROMPT")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_POINTS"
    assert response.json()["error"]["details"] == {"available_points": 1}
    assert harness.uow.generation_tasks.rows == ()
    assert harness.queue.generations == []


# --------------------------------------------------------------------------- #
# 幂等：回放 / 冲突
# --------------------------------------------------------------------------- #
def test_same_key_and_body_replays_without_second_side_effect(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    first = _create(harness, mode="TEXT_PROMPT")
    second = _create(harness, mode="TEXT_PROMPT")

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["data"] == second.json()["data"]
    assert [entry.entry_type for entry in harness.uow.point_ledger.entries] == ["RESERVE"]
    assert len(harness.queue.generations) == 1
    assert len(harness.uow.generation_tasks.rows) == 1


def test_same_key_different_body_is_conflict(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    assert _create(harness, mode="TEXT_PROMPT").status_code == 202

    response = _create(harness, mode="TEXT_PROMPT", prompt="完全不同的提示词")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(harness.uow.generation_tasks.rows) == 1
    assert [entry.entry_type for entry in harness.uow.point_ledger.entries] == ["RESERVE"]


def test_idempotency_scope_is_per_user(harness: Harness) -> None:
    _seed_user(harness)
    _seed_user(harness, OTHER_ID)

    _login(harness, USER_ID)
    assert _create(harness, mode="TEXT_PROMPT").status_code == 202
    _login(harness, OTHER_ID)
    assert _create(harness, mode="TEXT_PROMPT").status_code == 202

    assert len(harness.uow.generation_tasks.rows) == 2


# --------------------------------------------------------------------------- #
# 读取、列表与重试
# --------------------------------------------------------------------------- #
def test_get_returns_owner_task_and_404_for_everyone_else(harness: Harness) -> None:
    _seed_user(harness)
    _seed_user(harness, OTHER_ID)
    _login(harness)
    generation_id = uuid.UUID(_create(harness, mode="TEXT_PROMPT").json()["data"]["id"])

    mine = harness.client.get(f"{GENERATIONS_PATH}/{generation_id}")
    assert mine.status_code == 200
    assert mine.json()["data"]["id"] == str(generation_id)

    _login(harness, OTHER_ID)
    foreign = harness.client.get(f"{GENERATIONS_PATH}/{generation_id}")
    missing = harness.client.get(f"{GENERATIONS_PATH}/{uuid.uuid4()}")

    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "GENERATION_NOT_FOUND"


def test_get_rejects_malformed_generation_id(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)

    response = harness.client.get(f"{GENERATIONS_PATH}/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"] == ["generation_id"]


def test_outputs_page_is_empty_until_worker_settles(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    generation_id = uuid.UUID(_create(harness, mode="TEXT_PROMPT").json()["data"]["id"])

    response = harness.client.get(f"{GENERATIONS_PATH}/{generation_id}/outputs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] == []
    assert payload["page"] == {"next_cursor": None, "has_more": False}


def test_list_my_generations_pages_over_cursor(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    created: list[str] = []
    for index in range(3):
        harness.clock.advance(1)
        created.append(_create(harness, mode="TEXT_PROMPT", key=f"key-{index}").json()["data"]["id"])

    first = harness.client.get("/api/v1/me/generations", params={"limit": 2})

    assert first.status_code == 200
    body = first.json()
    assert [item["id"] for item in body["data"]] == [created[2], created[1]]
    assert body["page"]["has_more"] is True
    assert body["page"]["next_cursor"]

    second = harness.client.get(
        "/api/v1/me/generations", params={"limit": 2, "cursor": body["page"]["next_cursor"]}
    )
    assert [item["id"] for item in second.json()["data"]] == [created[0]]
    assert second.json()["page"]["has_more"] is False


@pytest.mark.parametrize("limit", ["0", "101", "abc"])
def test_list_rejects_out_of_range_limit(harness: Harness, limit: str) -> None:
    _seed_user(harness)
    _login(harness)

    response = harness.client.get("/api/v1/me/generations", params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"] == ["limit"]


def test_list_is_scope_bound_to_the_session(harness: Harness) -> None:
    response = harness.client.get("/api/v1/me/generations")

    assert response.status_code == 401


def test_retry_creates_a_child_task_and_leaves_the_failed_parent(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    parent_id = uuid.UUID(_create(harness, mode="TEXT_PROMPT").json()["data"]["id"])
    _force_failed_release(harness, parent_id)

    response = harness.client.post(
        f"{GENERATIONS_PATH}/{parent_id}/retry", headers={"Idempotency-Key": "retry-key-1"}
    )

    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["parent_generation_id"] == str(parent_id)
    assert data["status"] == "QUEUED"
    assert data["billing_status"] == "RESERVED"
    parent = harness.uow.generation_tasks.get_by_id(parent_id)
    assert (parent.status, parent.billing_status) == ("FAILED", "RELEASED")
    entries = [entry.entry_type for entry in harness.uow.point_ledger.entries]
    assert entries == ["RESERVE", "RELEASE", "RESERVE"]


def test_retry_requires_idempotency_key_and_only_accepts_failed(harness: Harness) -> None:
    _seed_user(harness)
    _login(harness)
    queued_id = uuid.UUID(_create(harness, mode="TEXT_PROMPT").json()["data"]["id"])

    missing = harness.client.post(f"{GENERATIONS_PATH}/{queued_id}/retry")
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"

    conflict = harness.client.post(
        f"{GENERATIONS_PATH}/{queued_id}/retry", headers={"Idempotency-Key": "retry-key-2"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "GENERATION_NOT_RETRYABLE"

    missing_task = harness.client.post(
        f"{GENERATIONS_PATH}/{uuid.uuid4()}/retry", headers={"Idempotency-Key": "retry-key-3"}
    )
    assert missing_task.status_code == 404
    assert missing_task.json()["error"]["code"] == "GENERATION_NOT_FOUND"


def _force_failed_release(harness: Harness, generation_id: uuid.UUID) -> None:
    """把任务置成 `FAILED + RELEASED`，模拟 Worker 的明确失败终局。"""

    task = harness.uow.generation_tasks.get_by_id(generation_id)
    harness.uow.generation_tasks.transition(
        task, status="FAILED", changed_at=harness.clock(), failure_code="TEMPORARY"
    )
    PointsService(uow=harness.uow, clock=harness.clock).release(
        user_id=USER_ID,
        generation_id=generation_id,
        idempotency_key=f"generation-release:{generation_id}",
        reason="generation_failed",
    )
    harness.uow.commit()
