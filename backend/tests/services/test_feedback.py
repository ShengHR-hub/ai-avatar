"""FeedbackService 单元测试（ENG-09）。

覆盖（契约 `03-api-contract.md` 第 4.4 节 + 技术负责人 2026-10-02 裁决）：

| 情形 | 期望 |
|---|---|
| 任务不存在 / 他人任务 | `404 GENERATION_NOT_FOUND` |
| 自己的非可反馈终态（含 `NEEDS_REVIEW`） | `422 VALIDATION_ERROR` + `fields=["generation_id"]` |
| 同键同载荷重复提交 | 回放第一次反馈，不重复写库 |
| 同键不同载荷 / 同键跨任务 | `409 IDEMPOTENCY_CONFLICT` |
| 已有反馈但换了新键 | `409 IDEMPOTENCY_CONFLICT`（每用户每任务一条） |
| 分类 / 正文超长 | `422 VALIDATION_ERROR` + 指明字段 |

幂等依据必须持久化并与写入同事务：这里同时断言业务行与审计登记一起落库，
且失败时一起回滚。全部使用内存替身，不启动基础设施。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.schemas.engagement import (
    CATEGORY_MAX_LENGTH,
    CONTENT_MAX_LENGTH,
    FeedbackCreateRequest,
)
from app.services.feedback.service import FEEDBACK_TERMINAL_STATUSES, FeedbackService
from tests.services.fakes import (
    EngagementUnitOfWork,
    MutableClock,
    make_generation_task_row,
    make_user_row,
)

CLOCK_START = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class Harness:
    def __init__(self, uow: EngagementUnitOfWork, clock: MutableClock) -> None:
        self.uow = uow
        self.clock = clock
        self.service = FeedbackService(uow=uow, clock=clock)

    def submit(self, **kwargs):
        """以 `with uow:` 调用，镜像真实请求的事务边界。"""

        with self.uow:
            return self.service.submit(**kwargs)

    def seed_task(self, *, user_id: uuid.UUID, status: str = "SUCCEEDED") -> uuid.UUID:
        task = make_generation_task_row(user_id=user_id, status=status, now=self.clock())
        self.uow.generation_tasks.seed(task)
        return task.id


@pytest.fixture
def harness() -> Harness:
    clock = MutableClock(CLOCK_START)
    uow = EngagementUnitOfWork(clock=clock)
    return Harness(uow, clock)


def _payload(generation_id: uuid.UUID, **extra) -> FeedbackCreateRequest:
    body = {
        "generation_id": generation_id,
        "rating": 5,
        "category": "画面质量",
        "content": "两张都很喜欢",
    }
    body.update(extra)
    return FeedbackCreateRequest(**body)


# --------------------------------------------------------------------------- #
# 所有权与终态
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["CREATED", "QUEUED", "PROCESSING", "NEEDS_REVIEW"])
def test_non_terminal_task_is_422_with_generation_id(
    harness: Harness, status: str
) -> None:
    """`NEEDS_REVIEW` 不是可反馈终态：结果还没定，反馈没有意义。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id, status=status)

    with pytest.raises(ApiError) as failure:
        harness.submit(
            user_id=user.id, payload=_payload(generation_id), idempotency_key="fb-1"
        )

    assert failure.value.code == ErrorCode.VALIDATION_ERROR
    assert failure.value.status_code == 422
    assert failure.value.details["fields"] == ["generation_id"]
    assert failure.value.details["status"] == status


@pytest.mark.parametrize("status", sorted(FEEDBACK_TERMINAL_STATUSES))
def test_succeeded_and_failed_are_feedbackable(harness: Harness, status: str) -> None:
    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id, status=status)

    row = harness.submit(
        user_id=user.id, payload=_payload(generation_id), idempotency_key=f"fb-{status}"
    )

    assert row.generation_id == generation_id
    assert row.rating == 5
    assert row.category == "画面质量"
    assert row.status == "OPEN"


def test_missing_and_foreign_task_are_404(harness: Harness) -> None:
    """他人任务与不存在的任务统一 404，且绝不伪装成 422。"""

    owner = harness.uow.users.seed(make_user_row())
    stranger = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=owner.id)

    with pytest.raises(ApiError) as foreign:
        harness.submit(
            user_id=stranger.id,
            payload=_payload(generation_id),
            idempotency_key="fb-foreign",
        )
    assert foreign.value.code == ErrorCode.GENERATION_NOT_FOUND
    assert foreign.value.status_code == 404

    with pytest.raises(ApiError) as missing:
        harness.submit(
            user_id=owner.id, payload=_payload(uuid.uuid4()), idempotency_key="fb-missing"
        )
    assert missing.value.code == ErrorCode.GENERATION_NOT_FOUND


# --------------------------------------------------------------------------- #
# 字段边界
# --------------------------------------------------------------------------- #
def test_rating_bounds_are_enforced(harness: Harness) -> None:
    generation_id = uuid.uuid4()

    for rating in (0, 6, -1):
        with pytest.raises(ValidationError):
            _payload(generation_id, rating=rating)

    for rating in (1, 5):
        assert _payload(generation_id, rating=rating).rating == rating


def test_category_length_boundary(harness: Harness) -> None:
    """分类上限 64（迁移 0021 已把列扩宽到 64），65 字符必须被拒。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id)

    # Schema 声明上限（基线形状：maxLength 直接在 string 上）
    schema = FeedbackCreateRequest.model_json_schema()["properties"]["category"]
    assert schema["maxLength"] == CATEGORY_MAX_LENGTH == 64

    exact = harness.submit(
        user_id=user.id,
        payload=_payload(generation_id, category="c" * 64),
        idempotency_key="fb-exact",
    )
    assert len(exact.category) == 64

    with pytest.raises(ValidationError):
        _payload(generation_id, category="c" * 65)


def test_content_length_boundary() -> None:
    """正文上限 2000，且 Schema 里的 `maxLength` 落在联合之外（与基线一致）。"""

    schema = FeedbackCreateRequest.model_json_schema()["properties"]["content"]
    assert schema["maxLength"] == CONTENT_MAX_LENGTH == 2000
    assert schema["anyOf"] == [{"type": "string"}, {"type": "null"}]

    generation_id = uuid.uuid4()
    assert _payload(generation_id, content="c" * 2000).content is not None
    with pytest.raises(ValidationError):
        _payload(generation_id, content="c" * 2001)


def test_unknown_fields_are_rejected() -> None:
    """基线的请求体是 `additionalProperties: false`，未知字段必须拒绝。"""

    with pytest.raises(ValidationError):
        FeedbackCreateRequest(
            generation_id=uuid.uuid4(), rating=5, category="x", internal_note="no"
        )


# --------------------------------------------------------------------------- #
# 每用户每任务一条 + 幂等
# --------------------------------------------------------------------------- #
def test_replay_with_same_key_and_payload(harness: Harness) -> None:
    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id)
    payload = _payload(generation_id)

    first = harness.submit(
        user_id=user.id, payload=payload, idempotency_key="fb-key"
    )
    commits = harness.uow.commits
    second = harness.submit(
        user_id=user.id, payload=payload, idempotency_key="fb-key"
    )

    assert second.id == first.id
    assert len(harness.uow.feedback.rows) == 1
    assert harness.uow.commits == commits  # 回放不产生新事务


def test_second_key_for_same_task_conflicts(harness: Harness) -> None:
    """每用户每任务一条：换了新键也不允许第二条。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id)

    harness.submit(user_id=user.id, payload=_payload(generation_id), idempotency_key="k1")

    with pytest.raises(ApiError) as failure:
        harness.submit(
            user_id=user.id,
            payload=_payload(generation_id, rating=1),
            idempotency_key="k2",
        )

    assert failure.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert len(harness.uow.feedback.rows) == 1


def test_same_key_different_payload_conflicts(harness: Harness) -> None:
    """同键不同载荷：稳定冲突，不覆盖第一次反馈。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id)

    harness.submit(user_id=user.id, payload=_payload(generation_id), idempotency_key="k1")

    with pytest.raises(ApiError) as failure:
        harness.submit(
            user_id=user.id,
            payload=_payload(generation_id, category="另一个分类"),
            idempotency_key="k1",
        )

    assert failure.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert failure.value.status_code == 409


def test_same_key_across_tasks_conflicts(harness: Harness) -> None:
    """同键跨任务：靠持久幂等登记判冲突，不依赖正文比较。"""

    user = harness.uow.users.seed(make_user_row())
    first_generation = harness.seed_task(user_id=user.id)
    second_generation = harness.seed_task(user_id=user.id)

    harness.submit(
        user_id=user.id, payload=_payload(first_generation), idempotency_key="shared-key"
    )

    with pytest.raises(ApiError) as failure:
        harness.submit(
            user_id=user.id,
            payload=_payload(second_generation),
            idempotency_key="shared-key",
        )

    assert failure.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert failure.value.details["fields"] == ["generation_id"]


def test_replay_after_commit_but_lost_response(harness: Harness) -> None:
    """提交成功但响应丢失：客户端用同键重试必须回放同一条，而不是报冲突。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id)
    payload = _payload(generation_id)

    # 第一次请求已经提交（业务行 + 幂等登记都落库），只是响应没回到客户端
    committed = harness.submit(
        user_id=user.id, payload=payload, idempotency_key="lost-response"
    )

    # 客户端重试（同键同载荷）
    replayed = harness.submit(
        user_id=user.id, payload=payload, idempotency_key="lost-response"
    )

    assert replayed.id == committed.id
    assert len(harness.uow.feedback.rows) == 1


def test_idempotency_record_persists_with_the_write(harness: Harness) -> None:
    """幂等依据与写入同事务：审计登记与反馈行一起保留，且不存原始正文。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id)

    harness.submit(
        user_id=user.id,
        payload=_payload(generation_id, content="私人正文不该进审计"),
        idempotency_key="fb-key",
    )

    entries = harness.uow.ledger.entries
    assert [entry.action for entry in entries] == ["FEEDBACK_CREATE"]
    metadata = dict(entries[0].metadata_)
    assert metadata["generation_id"] == str(generation_id)
    assert metadata["entity_id"] == str(harness.uow.feedback.rows[0].id)
    serialized = str(metadata)
    assert "fb-key" not in serialized
    assert "私人正文不该进审计" not in serialized


def test_failed_write_rolls_back_feedback_and_idempotency_record(
    harness: Harness,
) -> None:
    """写入失败必须整体回滚：不留业务行，也不留幂等登记（否则后续请求被误判回放）。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id = harness.seed_task(user_id=user.id)

    original = harness.uow.ledger.record

    def exploding_record(**kwargs):
        original(**kwargs)
        raise RuntimeError("synthetic failure inside the transaction")

    harness.uow.ledger.record = exploding_record  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        with harness.uow:
            harness.service.submit(
                user_id=user.id,
                payload=_payload(generation_id),
                idempotency_key="fb-key",
            )

    assert harness.uow.feedback.rows == ()
    assert harness.uow.ledger.entries == ()

    # 回滚后同一个键可以重新提交（没有被残留登记挡住）
    harness.uow.ledger.record = original  # type: ignore[method-assign]
    row = harness.submit(
        user_id=user.id, payload=_payload(generation_id), idempotency_key="fb-key"
    )
    assert row.generation_id == generation_id
