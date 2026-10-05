"""Feedback Service（ENG-09）：任务反馈的校验、唯一性与幂等。

规则（`03-api-contract.md` 第 4.4 节 + 技术负责人 2026-10-02 裁决）：

| 情形 | 结果 |
|---|---|
| 任务不存在、或不属于当前用户 | `404 GENERATION_NOT_FOUND`（不泄露存在性） |
| 自己的任务但不是可反馈终态（`CREATED/QUEUED/PROCESSING/NEEDS_REVIEW`） | `422 VALIDATION_ERROR`，`details.fields=["generation_id"]` |
| 同键同载荷重复提交 | 回放第一次反馈（不重复写库） |
| 同键不同载荷 / 同键跨任务 | `409 IDEMPOTENCY_CONFLICT` |
| 已有反馈但换了新的幂等键 | `409 IDEMPOTENCY_CONFLICT`（每用户每任务一条） |

`NEEDS_REVIEW` **不是**可反馈终态：它代表结果未定、正在等人工决定，
此时收反馈只会污染评测数据。

幂等依据与写入同事务：业务唯一键 `uq_feedback_user_generation` 是并发裁决者，
`audit_logs` 幂等登记是可审计旁证（见 `app/services/engagement_support.py`）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.db.models.engagement import Feedback
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories import UnitOfWork
from app.repositories.engagement import (
    DuplicateFeedback,
    FeedbackDraft,
    IdempotencyScopeBusy,
)
from app.schemas.engagement import (
    CATEGORY_MAX_LENGTH,
    CONTENT_MAX_LENGTH,
    FeedbackCreateRequest,
)
from app.services.engagement_support import (
    ACTION_CREATE_FEEDBACK,
    ENTITY_FEEDBACK,
    conflict,
    fingerprint_of,
    in_progress,
)

__all__ = ["FEEDBACK_TERMINAL_STATUSES", "FeedbackService"]

#: 可反馈终态。`NEEDS_REVIEW` 刻意不在其中：结果未定，反馈没有意义。
FEEDBACK_TERMINAL_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "FAILED"})


class FeedbackService:
    """反馈聚合的业务规则与事务边界。"""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow = uow
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )

    def submit(
        self,
        *,
        user_id: uuid.UUID,
        payload: FeedbackCreateRequest,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> Feedback:
        """提交反馈；返回落库的反馈行（响应体字段由路由组装）。"""

        self._require_lengths(payload)

        # 0) 数据库级串行化（事务级咨询锁，不新增表/列）：
        #    同一 (用户, 键) 的并发写在这里排队。这是「同一个键跨任务复用」
        #    这类冲突唯一能靠数据库挡住的地方 —— 它没有可查的业务行。
        #    等待超时只表示「同键请求正在处理中」：回滚后返回
        #    409 `IDEMPOTENCY_IN_PROGRESS` + `Retry-After`（不降级成 500）。
        try:
            self._uow.feedback.lock_idempotency_scope(
                user_id=user_id, idempotency_key=idempotency_key
            )
        except IdempotencyScopeBusy:
            self._uow.rollback()
            raise in_progress() from None

        task = self._uow.generation_tasks.get_by_id(payload.generation_id)
        if task is None or task.user_id != user_id:
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
        if task.status not in FEEDBACK_TERMINAL_STATUSES:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": ["generation_id"], "status": task.status},
            )

        fingerprint = fingerprint_of(
            {
                "generation_id": str(payload.generation_id),
                "rating": payload.rating,
                "category": payload.category,
                "content": payload.content,
            }
        )

        existing = self._uow.feedback.find(
            user_id=user_id, generation_id=payload.generation_id
        )
        if existing is not None:
            return self._replay_or_conflict(
                existing, user_id=user_id, idempotency_key=idempotency_key, fingerprint=fingerprint
            )

        previous = self._uow.ledger.find(
            user_id=user_id,
            action=ACTION_CREATE_FEEDBACK,
            idempotency_key=idempotency_key,
        )
        if previous is not None:
            # 同一个键已经用在另一个任务上（或另一次载荷上）
            if str(previous.metadata.get("generation_id")) != str(payload.generation_id):
                raise conflict(fields=["generation_id"])
            if previous.fingerprint != fingerprint:
                raise conflict(fields=["category", "rating", "content"])

        moment = self._clock().astimezone(timezone.utc)
        try:
            row = self._uow.feedback.create(
                FeedbackDraft(
                    user_id=user_id,
                    generation_id=payload.generation_id,
                    rating=payload.rating,
                    category=payload.category,
                    content=payload.content,
                ),
                created_at=moment,
            )
            self._uow.ledger.record(
                user_id=user_id,
                action=ACTION_CREATE_FEEDBACK,
                entity_type=ENTITY_FEEDBACK,
                entity_id=row.id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                request_id=request_id,
                extra={"generation_id": str(payload.generation_id)},
            )
            self._uow.commit()
        except (DuplicateFeedback, IntegrityError) as error:
            # 并发下唯一约束只放行一个：回滚后按既有行重判回放 / 冲突
            self._uow.rollback()
            winner = self._uow.feedback.find(
                user_id=user_id, generation_id=payload.generation_id
            )
            if winner is None:  # pragma: no cover - 回滚后仍读不到即环境异常
                raise conflict() from error
            return self._replay_or_conflict(
                winner,
                user_id=user_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
        return row

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _require_lengths(payload: FeedbackCreateRequest) -> None:
        """分类与正文长度上限（基线 `category.maxLength=64`、`content.maxLength=2000`）。

        校验放在这里而不是 `Field(max_length=...)`：Pydantic 会把 `Field` 的长度约束
        提升到可空联合之外，产出与基线 `anyOf` 不同的 Schema。契约语义不变，
        越界一律以 `VALIDATION_ERROR` 拒绝并指明字段。
        """

        fields: list[str] = []
        if len(payload.category) > CATEGORY_MAX_LENGTH:
            fields.append("category")
        if payload.content is not None and len(payload.content) > CONTENT_MAX_LENGTH:
            fields.append("content")
        if fields:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": fields})

    def _replay_or_conflict(
        self,
        existing: Feedback,
        *,
        user_id: uuid.UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> Feedback:
        """已有反馈时：只有「同键同载荷」才回放，其余一律稳定冲突。

        「同键」的判据取自持久幂等登记（`audit_logs`），不靠比较正文——
        正文相同不代表同一次请求，正文不同也不代表换了意图。
        """

        record = self._uow.ledger.find(
            user_id=user_id,
            action=ACTION_CREATE_FEEDBACK,
            idempotency_key=idempotency_key,
        )
        if record is None or record.entity_id != existing.id:
            raise conflict(fields=["generation_id"])
        if record.fingerprint != fingerprint:
            raise conflict(fields=["category", "rating", "content"])
        return existing
