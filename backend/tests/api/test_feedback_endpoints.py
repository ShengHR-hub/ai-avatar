"""反馈端点测试（ENG-09）。

全部外部依赖以内存替身注入：不启动 PostgreSQL、Redis 与对象存储。

覆盖：认证与邮箱门槛、`Idempotency-Key` 的 400 映射、请求体
`additionalProperties: false` 与 rating/category 边界、201 响应形状、
404 与 422 的分工、同键回放与冲突（含跨任务）、以及响应不泄漏内部信息。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_feedback_service,
    get_redis_port,
    get_session_store,
    get_uow,
)
from app.config import reset_settings_cache
from app.main import create_app
from app.services.feedback.service import FeedbackService
from tests.services.fakes import (
    EngagementUnitOfWork,
    FakeRedis,
    MutableClock,
    make_generation_task_row,
    make_user_row,
)

BASE_URL = "https://testserver"
SESSION_SECRET = "synthetic-session-secret-for-tests"
FEEDBACK_PATH = "/api/v1/feedback"
CLOCK_START = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class FeedbackHarness:
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
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[FeedbackHarness]:
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    reset_settings_cache()

    clock = MutableClock(CLOCK_START)
    app = create_app()
    uow = EngagementUnitOfWork(clock=clock)
    redis = FakeRedis(clock=clock)

    app.dependency_overrides[get_uow] = lambda: uow
    app.dependency_overrides[get_redis_port] = lambda: redis
    app.dependency_overrides[get_session_store] = lambda: _store(redis)
    app.dependency_overrides[get_feedback_service] = lambda: FeedbackService(
        uow=uow, clock=clock
    )

    with TestClient(app, base_url=BASE_URL) as client:
        yield FeedbackHarness(app, client, uow, redis, clock)

    app.dependency_overrides.clear()
    reset_settings_cache()


def _seed_user(harness: FeedbackHarness, *, verified: bool = True):
    user = harness.uow.users.seed(make_user_row())
    row = harness.uow.emails.add(
        user_id=user.id,
        email=f"user-{uuid.uuid4().hex[:8]}@example.com",
        is_primary=True,
    )
    if verified:
        harness.uow.emails.mark_verified(row, harness.clock())
    return user


def _login(harness: FeedbackHarness, user) -> None:
    token, _record = _store(harness.redis).create(user_id=user.id, email_verified=True)
    harness.client.headers["Cookie"] = f"session={token}"


def _seed_task(harness: FeedbackHarness, *, user, status: str = "SUCCEEDED"):
    task = make_generation_task_row(user_id=user.id, status=status, now=harness.clock())
    harness.uow.generation_tasks.seed(task)
    return task.id


def _submit(harness: FeedbackHarness, generation_id, key: str = "fb-key", **overrides):
    body: dict[str, object] = {
        "generation_id": str(generation_id),
        "rating": 5,
        "category": "画面质量",
        "content": "两张都满意",
    }
    body.update(overrides)
    return harness.client.post(
        FEEDBACK_PATH, json=body, headers={"Idempotency-Key": key}
    )


# --------------------------------------------------------------------------- #
# 认证与 Header
# --------------------------------------------------------------------------- #
def test_feedback_requires_session(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    generation_id = _seed_task(harness, user=user)

    response = _submit(harness, generation_id)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_feedback_requires_verified_email(harness: FeedbackHarness) -> None:
    user = _seed_user(harness, verified=False)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    response = _submit(harness, generation_id)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"


def test_missing_idempotency_header_is_400(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    response = harness.client.post(
        FEEDBACK_PATH,
        json={"generation_id": str(generation_id), "rating": 5, "category": "质量"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"


# --------------------------------------------------------------------------- #
# 请求体边界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rating", [0, 6])
def test_rating_out_of_range_is_422(harness: FeedbackHarness, rating: int) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    response = _submit(harness, generation_id, rating=rating)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_missing_required_fields_are_422(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    for body in (
        {"rating": 5, "category": "x"},
        {"generation_id": str(generation_id), "category": "x"},
        {"generation_id": str(generation_id), "rating": 5},
    ):
        response = harness.client.post(
            FEEDBACK_PATH, json=body, headers={"Idempotency-Key": "k"}
        )
        assert response.status_code == 422, body


def test_unknown_field_is_422(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    response = _submit(harness, generation_id, internal_note="nope")

    assert response.status_code == 422


def test_category_boundary_at_64(harness: FeedbackHarness) -> None:
    """64 字符可通过（迁移 0021 已把列扩宽到 64），65 字符被拒。"""

    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    exact = _submit(harness, generation_id, category="c" * 64)
    assert exact.status_code == 201
    assert len(exact.json()["data"]["category"]) == 64

    too_long = _submit(harness, generation_id, category="c" * 65, key="k2")
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "VALIDATION_ERROR"


def test_content_boundary_at_2000(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    exact = _submit(harness, generation_id, content="c" * 2000)
    assert exact.status_code == 201

    other_id = _seed_task(harness, user=user)
    too_long = _submit(harness, other_id, content="c" * 2001, key="k2")
    assert too_long.status_code == 422


def test_content_is_optional(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    response = harness.client.post(
        FEEDBACK_PATH,
        json={
            "generation_id": str(generation_id),
            "rating": 4,
            "category": "仅评分",
        },
        headers={"Idempotency-Key": "fb-key"},
    )

    assert response.status_code == 201
    assert response.json()["data"]["content"] is None


# --------------------------------------------------------------------------- #
# 终态与所有权
# --------------------------------------------------------------------------- #
def test_needs_review_is_422_with_generation_id(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user, status="NEEDS_REVIEW")

    response = _submit(harness, generation_id)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"] == ["generation_id"]


def test_failed_generation_is_feedbackable(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user, status="FAILED")

    response = _submit(harness, generation_id)

    assert response.status_code == 201


def test_foreign_task_is_404(harness: FeedbackHarness) -> None:
    owner = _seed_user(harness)
    generation_id = _seed_task(harness, user=owner)

    stranger = _seed_user(harness)
    _login(harness, stranger)

    response = _submit(harness, generation_id)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "GENERATION_NOT_FOUND"


def test_missing_task_is_404(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)

    response = _submit(harness, uuid.uuid4())

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 201 形状与幂等
# --------------------------------------------------------------------------- #
def test_created_feedback_shape(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    response = _submit(harness, generation_id)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"data", "request_id"}
    assert set(body["data"]) == {
        "id",
        "generation_id",
        "rating",
        "category",
        "content",
        "status",
        "created_at",
        "updated_at",
    }
    assert body["data"]["rating"] == 5
    assert body["data"]["status"] == "OPEN"
    # 不泄漏内部事实
    assert "prompt" not in response.text
    assert "storage" not in response.text
    assert "points" not in response.text
    assert "cost" not in response.text


def test_same_key_replays_the_same_feedback(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    first = _submit(harness, generation_id)
    second = _submit(harness, generation_id)

    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert len(harness.uow.feedback.rows) == 1


def test_second_key_for_same_task_is_409(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    generation_id = _seed_task(harness, user=user)

    _submit(harness, generation_id, key="k1")
    conflict = _submit(harness, generation_id, key="k2")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_same_key_across_tasks_is_409(harness: FeedbackHarness) -> None:
    user = _seed_user(harness)
    _login(harness, user)
    first = _seed_task(harness, user=user)
    second = _seed_task(harness, user=user)

    _submit(harness, first, key="shared")
    conflict = _submit(harness, second, key="shared")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert conflict.json()["error"]["details"]["fields"] == ["generation_id"]
