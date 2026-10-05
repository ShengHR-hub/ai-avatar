"""ENG-03 测试替身（非 conftest，便于测试模块直接导入类型）。

- `FakeRedis`：`RedisPort` 的内存实现，支持可控时钟与 TTL 过期；
- `MutableClock`：可推进时钟，用于会话/令牌过期测试；
- 内存 Repository 与 `InMemoryUnitOfWork`：让 Service 单测不依赖 PostgreSQL；
- `FakePointsPort`：按冻结端口契约返回合成兑换结果。

本卡不启动 Redis / PostgreSQL，因此全部 Service 测试都通过这些替身完成。

ENG-06 第 3 步追加（只新增，不改动上面既有替身的行为）：
`GenerationUnitOfWork`（带内存事务快照/回滚与事务探针）、完整任务生命周期读写、
`InMemoryGenerationMediaRepository`、`InMemoryGenerationCostConfigRepository`、
`RecordingGenerationQueuePort`、`InMemoryReplayGuard`、`ScriptedProvider` /
`ProbingProvider` 与合成图片字节工厂。

ENG-09 第 2 步追加（同样只新增）：`InMemoryShareRepository`、
`InMemoryFeedbackRepository`、`InMemoryLifecycleMediaRepository` 与
`EngagementUnitOfWork`（在 `GenerationUnitOfWork` 之上补齐分享/反馈仓储、
幂等登记与生命周期查询）。
"""

from __future__ import annotations

import io
import uuid
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from PIL import Image

from app.db.models.audit import AuditLog
from app.db.models.engagement import Feedback, Share, ShareAsset
from app.db.models.generation import (
    GenerationCostConfig,
    GenerationInput,
    GenerationOutput,
    GenerationTask,
)
from app.db.models.identity import User, UserEmail
from app.db.models.invites import InviteCode, InviteRedemption
from app.db.models.media import MediaAsset
from app.db.models.points import PointLedger, PointWallet
from app.db.models.provider import ProviderAttempt
from app.providers.mock import MOCK_MODEL, MOCK_PROVIDER_NAME
from app.providers.ports import AIProvider, GenerateRequest, ProviderResult
from app.repositories.audit import AuditEntryDraft
from app.repositories.engagement import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    DuplicateFeedback,
    DuplicateShare,
    FeedbackDraft,
    ShareAssetDraft,
    ShareAssetLink,
    ShareDraft,
)
from app.repositories.generation import (
    DuplicateIdempotencyKey,
    GenerationInputDraft,
    GenerationOutputDraft,
    GenerationTaskDraft,
    KeysetPage,
    _task_timestamp_column,
)
from app.repositories.media import MediaAssetDraft
from app.repositories.points import LedgerKeysetPage
from app.repositories.provider_attempt import ProviderAttemptDraft
from app.schemas.user import PointBalance, Session
from app.services.auth.mappers import to_api_user
from app.services.engagement_support import InMemoryIdempotencyLedger
from app.services.ports import RedeemOutcome

__all__ = [
    "EngagementUnitOfWork",
    "FakePointsPort",
    "FakeRedis",
    "GenerationUnitOfWork",
    "InMemoryAuditLogRepository",
    "InMemoryFeedbackRepository",
    "InMemoryGenerationCostConfigRepository",
    "InMemoryGenerationMediaRepository",
    "InMemoryGenerationTaskRepository",
    "InMemoryIdempotencyLedger",
    "InMemoryInviteCodeRepository",
    "InMemoryInviteRedemptionRepository",
    "InMemoryLifecycleMediaRepository",
    "InMemoryMediaAssetRepository",
    "InMemoryPointLedgerRepository",
    "InMemoryPointWalletRepository",
    "InMemoryProviderAttemptRepository",
    "InMemoryReplayGuard",
    "InMemoryShareRepository",
    "InMemoryUnitOfWork",
    "InMemoryUserEmailRepository",
    "InMemoryUserRepository",
    "MutableClock",
    "PointsUnitOfWork",
    "ProbingProvider",
    "RecordingGenerationQueuePort",
    "RecordingSession",
    "ScriptedProvider",
    "TransactionProbe",
    "make_generation_cost_config",
    "make_generation_task_row",
    "make_media_asset_row",
    "make_user_row",
    "synthetic_png_bytes",
]


class FakeRedis:
    """`RedisPort` 的内存实现（仅覆盖本卡使用的子集）。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._values: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------- RedisPort
    def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> bool:
        """`SET name value [EX seconds] [NX]` 的内存语义。

        `nx=True` 必须真的实现「仅在键不存在时写入」并返回是否写入成功：参考图分析的
        单飞锁（`RedisAnalysisGuard.begin`）就靠这个返回值判断自己有没有抢到锁。
        早期替身不认 `nx` 关键字，于是真实装配下 `begin()` 会抛 `TypeError`、
        被归一成 `NOT_READY`，让「并发只放行一次」的用例测到的是替身缺陷而不是锁语义。
        """

        self._purge_if_expired(name)
        if nx and name in self._values:
            return False
        self._values[name] = value
        if ex is None:
            self._expires_at.pop(name, None)
        else:
            self._expires_at[name] = self._clock().timestamp() + ex
        return True

    def get(self, name: str) -> str | None:
        self._purge_if_expired(name)
        return self._values.get(name)

    def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if name in self._values:
                del self._values[name]
                removed += 1
            self._expires_at.pop(name, None)
        return removed

    def expire(self, name: str, time: int) -> bool:
        if name not in self._values:
            return False
        self._expires_at[name] = self._clock().timestamp() + time
        return True

    # ------------------------------------------------------------- 测试辅助
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))

    def raw_values(self) -> tuple[str, ...]:
        return tuple(self._values.values())

    def ttl(self, name: str) -> int | None:
        if name not in self._values:
            return None
        deadline = self._expires_at.get(name)
        if deadline is None:
            return None
        return int(deadline - self._clock().timestamp())

    def _purge_if_expired(self, name: str) -> None:
        deadline = self._expires_at.get(name)
        if deadline is not None and deadline <= self._clock().timestamp():
            self._values.pop(name, None)
            self._expires_at.pop(name, None)


class MutableClock:
    """可推进的时钟，用于会话/令牌过期测试。

    默认起点是**真实当前时间**：`EmailTokenStore` / `RedisSessionStore` 在读取时
    会用真实时钟做二次过期判断，若假时钟固定在过去，令牌会被真实时钟判定为已过期，
    使测试结果依赖运行时刻（曾导致 `test_email_token_is_single_use` 在 UTC 00:30 之后失败）。
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = datetime.fromtimestamp(self._now.timestamp() + seconds, tz=timezone.utc)


# --------------------------------------------------------------------------- #
# 内存 Repository（让 Service 单测不依赖 PostgreSQL）
# --------------------------------------------------------------------------- #
def make_user_row(
    *,
    status: str = "ACTIVE",
    locale: str = "zh-CN",
    timezone_name: str = "Asia/Shanghai",
    display_name: str | None = None,
    avatar_asset_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> User:
    """构造一个已填充时间戳的 `users` 实体（不落库）。"""

    moment = now or datetime.now(timezone.utc)
    user = User(
        status=status,
        locale=locale,
        timezone=timezone_name,
        display_name=display_name,
        avatar_asset_id=avatar_asset_id,
        created_at=moment,
        updated_at=moment,
    )
    user.id = uuid.uuid4()
    return user


class InMemoryUserRepository:
    """`UserRepository` 的内存实现。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._users: dict[uuid.UUID, User] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def seed(self, user: User) -> User:
        self._users[user.id] = user
        return user

    def get(self, user_id: uuid.UUID) -> User | None:
        return self._users.get(user_id)

    def create(
        self,
        *,
        locale: str,
        timezone: str,
        status: str = "ACTIVE",
        display_name: str | None = None,
    ) -> User:
        return self.seed(
            make_user_row(
                status=status,
                locale=locale,
                timezone_name=timezone,
                display_name=display_name,
                now=self._clock(),
            )
        )

    def update_profile(self, user: User, changes: Mapping[str, Any]) -> User:
        from app.repositories.users import UPDATABLE_PROFILE_FIELDS

        for field, value in changes.items():
            if field not in UPDATABLE_PROFILE_FIELDS:
                raise ValueError(f"字段 {field} 不允许通过资料接口更新。")
            setattr(user, field, value)
        user.updated_at = self._clock()
        return user

    def set_status(self, user: User, status: str) -> User:
        user.status = status
        return user


class InMemoryUserEmailRepository:
    """`UserEmailRepository` 的内存实现（邮箱比较不区分大小写，对齐 CITEXT）。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._rows: list[UserEmail] = []
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def find_by_email(self, email: str) -> UserEmail | None:
        needle = email.lower()
        return next((row for row in self._rows if row.email.lower() == needle), None)

    def find_primary(self, user_id: uuid.UUID) -> UserEmail | None:
        return next(
            (row for row in self._rows if row.user_id == user_id and row.is_primary), None
        )

    def find_verified_for_user(self, user_id: uuid.UUID) -> UserEmail | None:
        return next(
            (
                row
                for row in self._rows
                if row.user_id == user_id and row.verified_at is not None
            ),
            None,
        )

    def add(self, *, user_id: uuid.UUID, email: str, is_primary: bool = False) -> UserEmail:
        row = UserEmail(
            user_id=user_id, email=email, is_primary=is_primary, created_at=self._clock()
        )
        row.id = uuid.uuid4()
        self._rows.append(row)
        return row

    def mark_verified(self, row: UserEmail, verified_at: datetime) -> UserEmail:
        row.verified_at = verified_at
        return row

    def set_primary(self, row: UserEmail, is_primary: bool) -> UserEmail:
        row.is_primary = is_primary
        return row


class InMemoryInviteCodeRepository:
    """`InviteCodeRepository` 的内存实现。

    内存实现没有真实行锁，`lock_by_hash` 记录调用以便断言「兑换路径确实走了加锁查询」。
    """

    def __init__(self) -> None:
        self._codes: list[InviteCode] = []
        self.lock_calls: list[str] = []

    def seed(self, code: InviteCode) -> InviteCode:
        self._codes.append(code)
        return code

    def find_by_hash(self, code_hash: str) -> InviteCode | None:
        return next((c for c in self._codes if c.code_hash == code_hash), None)

    def lock_by_hash(self, code_hash: str) -> InviteCode | None:
        self.lock_calls.append(code_hash)
        return self.find_by_hash(code_hash)

    def increment_redeemed_count(self, code: InviteCode) -> InviteCode:
        code.redeemed_count = code.redeemed_count + 1
        return code

    def mark_used(self, code: InviteCode) -> InviteCode:
        code.status = "USED"
        return code


class InMemoryInviteRedemptionRepository:
    """`InviteRedemptionRepository` 的内存实现。"""

    def __init__(self) -> None:
        self._rows: list[InviteRedemption] = []

    def find(
        self, *, invite_code_id: uuid.UUID, user_id: uuid.UUID
    ) -> InviteRedemption | None:
        return next(
            (
                row
                for row in self._rows
                if row.invite_code_id == invite_code_id and row.user_id == user_id
            ),
            None,
        )

    def add(
        self, *, invite_code_id: uuid.UUID, user_id: uuid.UUID, points_credited: int
    ) -> InviteRedemption:
        row = InviteRedemption(
            invite_code_id=invite_code_id,
            user_id=user_id,
            points_credited=points_credited,
            created_at=datetime.now(timezone.utc),
        )
        row.id = uuid.uuid4()
        self._rows.append(row)
        return row


class InMemoryMediaAssetRepository:
    """`MediaAssetRepository` 的内存实现（只读归属查询）。"""

    def __init__(self) -> None:
        self._owners: dict[uuid.UUID, uuid.UUID] = {}

    def register(self, asset_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self._owners[asset_id] = user_id

    def find_owner(self, asset_id: uuid.UUID) -> uuid.UUID | None:
        return self._owners.get(asset_id)


# --------------------------------------------------------------------------- #
# ENG-04 第 2 步扩围：生成任务与审计日志替身
# --------------------------------------------------------------------------- #
def make_generation_task_row(
    *,
    generation_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    status: str = "PROCESSING",
    billing_status: str = "RESERVED",
    reserved_points: int = 20,
    mode: str = "PHOTO_PROMPT",
    prompt: str = "synthetic prompt",
    requested_count: int = 2,
    cost_config_id: uuid.UUID | None = None,
    cost_snapshot: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> GenerationTask:
    """构造一个已填充时间戳的 `generation_tasks` 实体（不落库）。"""

    moment = now or datetime.now(timezone.utc)
    task = GenerationTask(
        user_id=user_id or uuid.uuid4(),
        mode=mode,
        status=status,
        billing_status=billing_status,
        prompt=prompt,
        requested_count=requested_count,
        cost_config_id=cost_config_id or uuid.uuid4(),
        cost_snapshot=dict(cost_snapshot or {"points_cost": reserved_points}),
        reserved_points=reserved_points,
        idempotency_key=idempotency_key or f"synthetic-generation-{uuid.uuid4().hex[:12]}",
        created_at=moment,
        updated_at=moment,
    )
    task.id = generation_id or uuid.uuid4()
    return task


class InMemoryGenerationTaskRepository:
    """`GenerationTaskRepository` 的内存实现（行锁 + 唯一的 `billing_status` 写路径）。

    内存实现没有真实行锁，`lock_by_id` 记录调用以便断言「结算事务确实先锁任务行」，
    并镜像统一锁顺序（先 `generation_tasks` 再 `point_wallets`）。
    写方法**只有** `set_billing_status`（账务状态，Points Service 独占）；
    任务生命周期状态 `status` 属于 Generation Service，本替身不提供任何写入口。

    ENG-06 第 3 步追加：生命周期唯一写入口 `transition`、`create`（含
    `(user_id, idempotency_key)` 唯一索引裁决）、`record_retry`、输入/输出行读写与
    键集/恢复扫描查询。追加的方法不改变上面既有读写语义。
    """

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self._tasks: dict[uuid.UUID, GenerationTask] = {}
        self._inputs: list[GenerationInput] = []
        self._outputs: list[GenerationOutput] = []
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trace = trace
        self.lock_calls: list[uuid.UUID] = []
        self.read_calls: list[uuid.UUID] = []
        self.billing_status_calls: list[tuple[uuid.UUID, str]] = []
        self.transition_calls: list[tuple[uuid.UUID, str]] = []

    def _record(self, event: str) -> None:
        if self._trace is not None:
            self._trace.append(event)

    def seed(self, task: GenerationTask) -> GenerationTask:
        self._tasks[task.id] = task
        return task

    def get_by_id(self, generation_id: uuid.UUID) -> GenerationTask | None:
        self.read_calls.append(generation_id)
        self._record(f"read:generation_task:{generation_id}")
        return self._tasks.get(generation_id)

    def lock_by_id(self, generation_id: uuid.UUID) -> GenerationTask | None:
        self.lock_calls.append(generation_id)
        self._record(f"lock:generation_task:{generation_id}")
        return self._tasks.get(generation_id)

    def set_billing_status(
        self, task: GenerationTask, billing_status: str
    ) -> GenerationTask:
        self.billing_status_calls.append((task.id, billing_status))
        self._record(f"billing_status:{billing_status}")
        task.billing_status = billing_status
        task.updated_at = self._clock()
        return task

    # ---------------------------------------------- ENG-06 第 3 步追加（生命周期与聚合）
    @property
    def rows(self) -> tuple[GenerationTask, ...]:
        """测试断言用：当前存储里的全部任务行（顺序不保证）。"""

        return tuple(self._tasks.values())

    def create(self, draft: GenerationTaskDraft, *, created_at: datetime) -> GenerationTask:
        """镜像 `(user_id, idempotency_key)` 唯一索引：冲突时抛 `DuplicateIdempotencyKey`。"""

        clash = self.find_by_idempotency_key(
            user_id=draft.user_id, idempotency_key=draft.idempotency_key
        )
        if clash is not None:
            raise DuplicateIdempotencyKey(draft.idempotency_key)
        task = GenerationTask(
            user_id=draft.user_id,
            parent_task_id=draft.parent_task_id,
            mode=draft.mode,
            status=draft.status,
            billing_status=draft.billing_status,
            prompt=draft.prompt,
            style_id=draft.style_id,
            prompt_template_id=draft.prompt_template_id,
            requested_count=draft.requested_count,
            cost_config_id=draft.cost_config_id,
            cost_snapshot=dict(draft.cost_snapshot),
            reserved_points=draft.reserved_points,
            provider_name=draft.provider_name,
            model_name=draft.model_name,
            idempotency_key=draft.idempotency_key,
            retry_count=0,
            created_at=created_at,
            updated_at=created_at,
        )
        task.id = uuid.uuid4()
        self._tasks[task.id] = task
        self._record(f"create:generation_task:{task.id}")
        return task

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> GenerationTask | None:
        return next(
            (
                task
                for task in self._tasks.values()
                if task.user_id == user_id and task.idempotency_key == idempotency_key
            ),
            None,
        )

    def transition(
        self,
        task: GenerationTask,
        *,
        status: str,
        changed_at: datetime,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> GenerationTask:
        """生命周期状态唯一写入口：只写状态/阶段时间戳/失败字段，绝不碰 `billing_status`。"""

        task.status = status
        # 时间戳列映射与 SQLAlchemy 实现同源，避免两处各写一遍（app/repositories/generation.py）
        timestamp_column = _task_timestamp_column(status)
        if timestamp_column is not None:
            setattr(task, timestamp_column, changed_at)
        if failure_code is not None:
            task.failure_code = failure_code
        if failure_message is not None:
            task.failure_message = failure_message
        task.updated_at = changed_at
        self.transition_calls.append((task.id, status))
        self._record(f"transition:{status}")
        return task

    def record_retry(self, task: GenerationTask, *, changed_at: datetime) -> GenerationTask:
        task.retry_count = task.retry_count + 1
        task.updated_at = changed_at
        self._record(f"record_retry:{task.retry_count}")
        return task

    def list_for_user(
        self, *, user_id: uuid.UUID, cursor: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> KeysetPage:
        """`(created_at, id)` 倒序键集分页，多取一条以判定 `has_more`（与 SQL 实现一致）。"""

        ordered = sorted(
            (task for task in self._tasks.values() if task.user_id == user_id),
            key=lambda task: (task.created_at, task.id),
            reverse=True,
        )
        if cursor is not None:
            created_at, task_id = cursor
            ordered = [
                task
                for task in ordered
                if (task.created_at, task.id) < (created_at, task_id)
            ]
        return KeysetPage(rows=tuple(ordered[:limit]), has_more=len(ordered) > limit)

    def list_stale(
        self, *, statuses: tuple[str, ...], older_than: datetime, limit: int
    ) -> tuple[GenerationTask, ...]:
        ordered = sorted(
            (
                task
                for task in self._tasks.values()
                if task.status in statuses and task.created_at <= older_than
            ),
            key=lambda task: (task.created_at, task.id),
        )
        return tuple(ordered[:limit])

    def list_timed_out(
        self, *, status: str, processing_before: datetime, limit: int
    ) -> tuple[GenerationTask, ...]:
        """与 SQL 实现同口径：超时只按进入 `PROCESSING` 的时间判定，不按创建时间。"""

        ordered = sorted(
            (
                task
                for task in self._tasks.values()
                if task.status == status
                and task.processing_at is not None
                and task.processing_at <= processing_before
            ),
            key=lambda task: (task.processing_at, task.id),
        )
        return tuple(ordered[:limit])

    def create_input(self, draft: GenerationInputDraft) -> GenerationInput:
        row = GenerationInput(
            generation_id=draft.generation_id,
            asset_id=draft.asset_id,
            role=draft.role,
            sort_order=draft.sort_order,
            created_at=self._clock(),
        )
        row.id = uuid.uuid4()
        self._inputs.append(row)
        self._record(f"create:generation_input:{draft.role}")
        return row

    def list_inputs(self, generation_id: uuid.UUID) -> tuple[GenerationInput, ...]:
        return tuple(
            sorted(
                (row for row in self._inputs if row.generation_id == generation_id),
                key=lambda row: (row.role, row.sort_order),
            )
        )

    def create_output(self, draft: GenerationOutputDraft) -> GenerationOutput:
        clash = next(
            (
                row
                for row in self._outputs
                if row.generation_id == draft.generation_id
                and row.output_index == draft.output_index
            ),
            None,
        )
        if clash is not None:
            raise DuplicateIdempotencyKey(
                f"output:{draft.generation_id}:{draft.output_index}"
            )
        row = GenerationOutput(
            generation_id=draft.generation_id,
            asset_id=draft.asset_id,
            output_index=draft.output_index,
            provider_output_id=draft.provider_output_id,
            created_at=self._clock(),
        )
        row.id = uuid.uuid4()
        self._outputs.append(row)
        self._record(f"create:generation_output:{draft.output_index}")
        return row

    def list_outputs(self, generation_id: uuid.UUID) -> tuple[GenerationOutput, ...]:
        return tuple(
            sorted(
                (row for row in self._outputs if row.generation_id == generation_id),
                key=lambda row: row.output_index,
            )
        )


class InMemoryAuditLogRepository:
    """`AuditLogRepository` 的内存实现（只追加，无 update/delete）。"""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self._entries: list[AuditLog] = []
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trace = trace

    def append(self, draft: AuditEntryDraft) -> AuditLog:
        entry = AuditLog(
            actor_type=draft.actor_type,
            actor_id=draft.actor_id,
            action=draft.action,
            entity_type=draft.entity_type,
            entity_id=draft.entity_id,
            request_id=draft.request_id,
            ip_address=draft.ip_address,
            metadata_=dict(draft.metadata),
            created_at=self._clock(),
        )
        entry.id = uuid.uuid4()
        self._entries.append(entry)
        if self._trace is not None:
            self._trace.append(f"append:audit:{draft.action}")
        return entry

    @property
    def entries(self) -> tuple[AuditLog, ...]:
        return tuple(self._entries)


class InMemoryProviderAttemptRepository:
    """In-memory ProviderAttempt repository for service tests."""

    def __init__(self) -> None:
        self._attempts: dict[uuid.UUID, ProviderAttempt] = {}
        self.lock_calls: list[uuid.UUID] = []

    def get_by_id(self, attempt_id: uuid.UUID) -> ProviderAttempt | None:
        return self._attempts.get(attempt_id)

    def get_by_generation_and_attempt(
        self, generation_id: uuid.UUID, attempt_no: int
    ) -> ProviderAttempt | None:
        return next(
            (
                attempt
                for attempt in self._attempts.values()
                if attempt.generation_id == generation_id
                and attempt.attempt_no == attempt_no
            ),
            None,
        )

    def list_by_generation_id(
        self, generation_id: uuid.UUID
    ) -> tuple[ProviderAttempt, ...]:
        return tuple(
            sorted(
                (
                    attempt
                    for attempt in self._attempts.values()
                    if attempt.generation_id == generation_id
                ),
                key=lambda attempt: attempt.attempt_no,
            )
        )

    def lock_by_id(self, attempt_id: uuid.UUID) -> ProviderAttempt | None:
        self.lock_calls.append(attempt_id)
        return self._attempts.get(attempt_id)

    def find_by_request_id(
        self, *, provider_name: str, request_id: str
    ) -> ProviderAttempt | None:
        """与 SQL 实现同口径：只按 `(provider_name, request_id)` 关联，取最新 attempt。"""

        candidates = [
            attempt
            for attempt in self._attempts.values()
            if attempt.provider_name == provider_name and attempt.request_id == request_id
        ]
        return max(candidates, key=lambda attempt: attempt.attempt_no) if candidates else None

    def create(self, draft: ProviderAttemptDraft) -> ProviderAttempt:
        attempt = ProviderAttempt(
            generation_id=draft.generation_id,
            attempt_no=draft.attempt_no,
            provider_name=draft.provider_name,
            model_name=draft.model_name,
            request_id=draft.request_id,
            status=draft.status,
            input_tokens=draft.input_tokens,
            output_tokens=draft.output_tokens,
            image_count=draft.image_count,
            estimated_cost=draft.estimated_cost,
            currency=draft.currency,
            request_metadata=dict(draft.request_metadata),
            response_metadata=dict(draft.response_metadata),
            error_code=draft.error_code,
            error_message=draft.error_message,
            started_at=draft.started_at,
            finished_at=draft.finished_at,
        )
        # SQLAlchemy 由 `flush()` 生成主键；内存实现必须自己赋值，否则所有行都落在
        # 同一个 `None` 键上，多 attempt 的用例会互相覆盖
        attempt.id = uuid.uuid4()
        self._attempts[attempt.id] = attempt
        return attempt

    def update(self, attempt: ProviderAttempt, **changes: Any) -> ProviderAttempt:
        allowed = {
            "request_id",
            "status",
            "input_tokens",
            "output_tokens",
            "image_count",
            "estimated_cost",
            "currency",
            "request_metadata",
            "response_metadata",
            "error_code",
            "error_message",
            "finished_at",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported provider attempt fields: {sorted(unknown)}")
        for field_name, value in changes.items():
            if field_name.endswith("metadata") and value is not None:
                value = dict(value)
            setattr(attempt, field_name, value)
        return attempt


class InMemoryUnitOfWork:
    """`UnitOfWork` 的内存实现，记录 commit/rollback 次数。

    `trace` 是所有仓储共享的**有序事件流**，用于断言固定锁顺序与
    「锁 → 写流水 → 更新快照 → 更新 billing_status → 追加审计」的事务内步骤顺序
    （内存实现没有真实行锁，顺序只能靠事件流证明）。
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        moment = clock or (lambda: datetime.now(timezone.utc))
        self.trace: list[str] = []
        self.users = InMemoryUserRepository(moment)
        self.emails = InMemoryUserEmailRepository(moment)
        self.invite_codes = InMemoryInviteCodeRepository()
        self.invite_redemptions = InMemoryInviteRedemptionRepository()
        self.media = InMemoryMediaAssetRepository()
        self.generation_tasks = InMemoryGenerationTaskRepository(moment, self.trace)
        # ENG-06 第 3 步：`UnitOfWork` 协议新增 `generation_costs`，替身同步暴露（只读配置）
        self.generation_costs = InMemoryGenerationCostConfigRepository()
        self.point_wallets = InMemoryPointWalletRepository(moment, self.trace)
        self.point_ledger = InMemoryPointLedgerRepository(moment, self.trace)
        self.audit_logs = InMemoryAuditLogRepository(moment, self.trace)
        self.provider_attempts = InMemoryProviderAttemptRepository()
        # ENG-09：`UnitOfWork` 协议新增 shares / feedback / ledger，基础替身同步补齐。
        # 这里**不**给分享替身绑定资产注册表：本替身的 `media` 只有归属查询，
        # 没有资产行，绑上去只会在属性缺失时炸掉（分享路径由 `EngagementUnitOfWork` 覆盖）。
        self.shares = InMemoryShareRepository(moment)
        self.feedback = InMemoryFeedbackRepository()
        self._ledger = InMemoryIdempotencyLedger(self.audit_logs)
        self.commits = 0
        self.rollbacks = 0

    @property
    def ledger(self) -> InMemoryIdempotencyLedger:
        return self._ledger

    def __enter__(self) -> InMemoryUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


# --------------------------------------------------------------------------- #
# ENG-04：点数钱包/流水替身
# --------------------------------------------------------------------------- #
class InMemoryPointWalletRepository:
    """`PointWalletRepository` 的内存实现。"""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self._wallets: dict[uuid.UUID, PointWallet] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trace = trace
        self.lock_calls: list[uuid.UUID] = []

    def _record(self, event: str) -> None:
        if self._trace is not None:
            self._trace.append(event)

    def seed(self, user_id: uuid.UUID, **balances: int) -> PointWallet:
        moment = self._clock()
        wallet = PointWallet(
            user_id=user_id,
            available_points=balances.get("available_points", 0),
            reserved_points=balances.get("reserved_points", 0),
            lifetime_credited=balances.get("lifetime_credited", 0),
            lifetime_consumed=balances.get("lifetime_consumed", 0),
            version=balances.get("version", 0),
            created_at=moment,
            updated_at=moment,
        )
        wallet.id = uuid.uuid4()
        self._wallets[user_id] = wallet
        return wallet

    def get_by_user_id(self, user_id: uuid.UUID) -> PointWallet | None:
        self._record(f"read:wallet:{user_id}")
        return self._wallets.get(user_id)

    def lock_by_user_id(self, user_id: uuid.UUID) -> PointWallet | None:
        self.lock_calls.append(user_id)
        self._record(f"lock:wallet:{user_id}")
        return self._wallets.get(user_id)

    def create(self, *, user_id: uuid.UUID) -> PointWallet:
        return self.seed(user_id)

    def apply_delta(self, wallet: PointWallet, delta) -> PointWallet:
        from app.repositories.points import apply_wallet_delta

        self._record("apply_delta:wallet")
        return apply_wallet_delta(wallet, delta)


class InMemoryPointLedgerRepository:
    """`PointLedgerRepository` 的内存实现（只追加）。"""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self._entries: list[PointLedger] = []
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trace = trace

    def append(self, draft) -> PointLedger:
        entry = PointLedger(
            wallet_id=draft.wallet_id,
            user_id=draft.user_id,
            entry_type=draft.entry_type,
            available_delta=draft.available_delta,
            reserved_delta=draft.reserved_delta,
            amount=draft.amount,
            generation_id=draft.generation_id,
            order_id=draft.order_id,
            idempotency_key=draft.idempotency_key,
            reason=draft.reason,
            metadata_=dict(draft.metadata),
            created_at=self._clock(),
        )
        entry.id = uuid.uuid4()
        self._entries.append(entry)
        if self._trace is not None:
            self._trace.append(f"append:ledger:{draft.entry_type}")
        return entry

    def find_by_idempotency_key(self, idempotency_key: str) -> PointLedger | None:
        return next(
            (e for e in self._entries if e.idempotency_key == idempotency_key), None
        )

    def find_reserve_for_generation(self, generation_id: uuid.UUID) -> PointLedger | None:
        return next(
            (
                e
                for e in self._entries
                if e.generation_id == generation_id and e.entry_type == "RESERVE"
            ),
            None,
        )

    def find_final_for_generation(self, generation_id: uuid.UUID) -> PointLedger | None:
        return next(
            (
                e
                for e in self._entries
                if e.generation_id == generation_id
                and e.entry_type in ("CONSUME", "RELEASE")
            ),
            None,
        )

    def list_for_user(
        self, *, user_id: uuid.UUID, cursor: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> LedgerKeysetPage:
        """`(created_at, id)` 倒序键集分页，多取一条判定 `has_more`（与 SQL 实现一致）。"""

        ordered = sorted(
            (entry for entry in self._entries if entry.user_id == user_id),
            key=lambda entry: (entry.created_at, entry.id),
            reverse=True,
        )
        if cursor is not None:
            created_at, ledger_id = cursor
            ordered = [
                entry
                for entry in ordered
                if (entry.created_at, entry.id) < (created_at, ledger_id)
            ]
        return LedgerKeysetPage(rows=tuple(ordered[:limit]), has_more=len(ordered) > limit)

    @property
    def entries(self) -> tuple[PointLedger, ...]:
        return tuple(self._entries)


class RecordingSession:
    """记录 SQL 与写操作的最小 session 替身（用于验证 Repository 的语句形状）。"""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.added: list[object] = []
        self.flushes = 0
        self.commits = 0

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))

        class _Result:
            def scalar_one_or_none(self_inner):
                return None

            def scalar_one(self_inner):
                return 0

            def scalars(self_inner):
                return ()

        return _Result()

    def add(self, instance: object) -> None:
        self.added.append(instance)

    def flush(self) -> None:
        self.flushes += 1

    def commit(self) -> None:
        self.commits += 1


class PointsUnitOfWork:
    """只包含点数/结算所需仓储的轻量工作单元替身（用于 Repository 层单测）。

    与 `InMemoryUnitOfWork` 一样共享 `trace` 事件流；不含身份/邀请码仓储，
    因此不能用于 `redeem_invite` 的端到端编排测试。
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        moment = clock or (lambda: datetime.now(timezone.utc))
        self.trace: list[str] = []
        self.generation_tasks = InMemoryGenerationTaskRepository(moment, self.trace)
        self.point_wallets = InMemoryPointWalletRepository(moment, self.trace)
        self.point_ledger = InMemoryPointLedgerRepository(moment, self.trace)
        self.audit_logs = InMemoryAuditLogRepository(moment, self.trace)
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> PointsUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakePointsPort:
    """`PointsPort` 替身：按冻结契约返回合成兑换结果，并记录调用参数。"""

    def __init__(
        self,
        *,
        available_points: int = 100,
        fail_with: BaseException | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._available_points = available_points
        self._fail_with = fail_with

    def redeem_invite(
        self, *, invite_code: str, email: str, session_ttl_seconds: int
    ) -> RedeemOutcome:
        self.calls.append(
            {
                "invite_code": invite_code,
                "email": email,
                "session_ttl_seconds": session_ttl_seconds,
            }
        )
        if self._fail_with is not None:
            raise self._fail_with

        moment = datetime.now(timezone.utc)
        user_row = make_user_row(now=moment)
        user_row.display_name = None
        return RedeemOutcome(
            user=to_api_user(user_row, None),
            points=PointBalance(
                available_points=self._available_points,
                reserved_points=0,
                lifetime_credited=self._available_points,
                lifetime_consumed=0,
                updated_at=moment,
            ),
            session=Session(
                session_id="f" * 64,
                user_id=user_row.id,
                email_verified=False,
                expires_at=moment + timedelta(seconds=session_ttl_seconds),
            ),
            session_token="synthetic-session-token",
        )


# --------------------------------------------------------------------------- #
# ENG-06 第 3 步：Generation Service / Orchestrator / Webhook 测试替身
# --------------------------------------------------------------------------- #
def make_generation_cost_config(
    *,
    mode: str = "PHOTO_PROMPT",
    model_name: str | None = None,
    output_count: int = 2,
    points_cost: int = 10,
    cost_multiplier: Decimal = Decimal("1"),
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    status: str = "ACTIVE",
) -> GenerationCostConfig:
    """构造一行成本配置（不落库）；生效窗口默认覆盖「现在」。"""

    moment = effective_from or datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = GenerationCostConfig(
        mode=mode,
        model_name=model_name,
        output_count=output_count,
        points_cost=points_cost,
        cost_multiplier=cost_multiplier,
        effective_from=moment,
        effective_to=effective_to,
        status=status,
        created_at=moment,
    )
    row.id = uuid.uuid4()
    return row


class InMemoryGenerationCostConfigRepository:
    """`GenerationCostConfigRepository` 的内存实现（按生效窗口取激活配置 + 部署期写入）。

    选择规则与 `SqlAlchemyGenerationCostConfigRepository.find_active` 逐条对齐：
    `ACTIVE` + `mode` + `output_count` + 窗口包含 `at`；`model_name` 给定时
    **精确型号优先**，只有精确型号没有命中时才回落到通配（`NULL`）行；
    同一档内多命中按 `effective_from` 倒序取第一条；两者都没有返回 `None`。

    `add` / `close` 只服务部署期初始化命令的单元测试：与 SQL 实现一样，
    只改内存行，**不提交事务**（提交由调用方持有）。
    """

    def __init__(self) -> None:
        self._rows: list[GenerationCostConfig] = []
        self.calls: list[dict[str, Any]] = []

    def seed(self, row: GenerationCostConfig) -> GenerationCostConfig:
        self._rows.append(row)
        return row

    def _pick(self, *, mode: str, model_name: str | None, output_count: int, at: datetime):
        candidates = [
            row
            for row in self._rows
            if row.status == "ACTIVE"
            and row.mode == mode
            and row.output_count == output_count
            and row.effective_from <= at
            and (row.effective_to is None or row.effective_to > at)
            and (row.model_name is None if model_name is None else row.model_name == model_name)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: row.effective_from)

    def find_active(
        self, *, mode: str, model_name: str | None, output_count: int, at: datetime
    ) -> GenerationCostConfig | None:
        self.calls.append(
            {"mode": mode, "model_name": model_name, "output_count": output_count, "at": at}
        )
        if model_name is not None:
            exact_row = self._pick(
                mode=mode, model_name=model_name, output_count=output_count, at=at
            )
            if exact_row is not None:
                return exact_row
        return self._pick(mode=mode, model_name=None, output_count=output_count, at=at)

    def list_active(self, *, at: datetime) -> tuple[GenerationCostConfig, ...]:
        return tuple(
            row
            for row in self._rows
            if row.status == "ACTIVE"
            and row.effective_from <= at
            and (row.effective_to is None or row.effective_to > at)
        )

    def add(
        self,
        *,
        mode: str,
        output_count: int,
        points_cost: int,
        model_name: str | None,
        effective_from: datetime,
    ) -> GenerationCostConfig:
        row = GenerationCostConfig(
            mode=mode,
            model_name=model_name,
            output_count=output_count,
            points_cost=points_cost,
            effective_from=effective_from,
            effective_to=None,
            status="ACTIVE",
        )
        row.id = uuid.uuid4()
        self._rows.append(row)
        return row

    def close(
        self, config: GenerationCostConfig, *, closed_at: datetime
    ) -> GenerationCostConfig:
        config.status = "INACTIVE"
        config.effective_to = closed_at
        return config


def make_media_asset_row(
    *,
    user_id: uuid.UUID,
    purpose: str = "SOURCE",
    asset_type: str = "ORIGINAL",
    status: str = "PENDING_UPLOAD",
    storage_provider: str = "MINIO",
    storage_key: str | None = None,
    mime_type: str = "image/png",
    size_bytes: int = 64,
    width: int | None = None,
    height: int | None = None,
    sha256: str = "a" * 64,
    idempotency_key: str | None = "synthetic-key",
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> MediaAsset:
    """构造一行媒体资源（不落库），默认值全部是合成数据。

    `now` 必须与调用方的时钟一致：`expires_at` 默认是「now + 7 天」，
    若用真实墙钟而调用方用可控时钟，资产到期时间会比时钟早/晚一整天，
    「分享期限不得晚于资产最早到期」这类断言就会在假问题上打转。
    """

    moment = now or datetime.now(timezone.utc)
    asset = MediaAsset(
        user_id=user_id,
        asset_type=asset_type,
        purpose=purpose,
        status=status,
        visibility="PRIVATE",
        storage_provider=storage_provider,
        storage_key=storage_key or f"assets/{user_id}/{uuid.uuid4().hex}",
        mime_type=mime_type,
        size_bytes=size_bytes,
        width=width,
        height=height,
        sha256=sha256,
        idempotency_key=idempotency_key,
        expires_at=expires_at or (moment + timedelta(days=7)),
        created_at=moment,
    )
    asset.id = uuid.uuid4()
    return asset


class InMemoryGenerationMediaRepository:
    """完整 `MediaAssetRepository` 内存实现（结果图落地与输入图读取都需要写路径）。

    `tests/services/fakes.py` 原有的 `InMemoryMediaAssetRepository` 只有归属查询，
    ENG-06 的第 3 步要创建/置 `READY` 结果行，因此单独补一个完整实现，
    不改动旧替身（与 `tests/storage/fakes.py` 同一先例）。
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._rows: dict[uuid.UUID, MediaAsset] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock_calls: list[uuid.UUID] = []
        self.create_calls: list[str] = []

    @property
    def rows(self) -> tuple[MediaAsset, ...]:
        return tuple(self._rows.values())

    def register(self, asset: MediaAsset) -> MediaAsset:
        self._rows[asset.id] = asset
        return asset

    def find_owner(self, asset_id: uuid.UUID) -> uuid.UUID | None:
        asset = self._rows.get(asset_id)
        return asset.user_id if asset is not None else None

    def get(self, asset_id: uuid.UUID) -> MediaAsset | None:
        return self._rows.get(asset_id)

    def lock_by_id(self, asset_id: uuid.UUID) -> MediaAsset | None:
        self.lock_calls.append(asset_id)
        return self._rows.get(asset_id)

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> MediaAsset | None:
        return next(
            (
                row
                for row in self._rows.values()
                if row.user_id == user_id and row.idempotency_key == idempotency_key
            ),
            None,
        )

    def create(self, draft: MediaAssetDraft) -> MediaAsset:
        asset = make_media_asset_row(
            user_id=draft.user_id,
            purpose=draft.purpose,
            asset_type=draft.asset_type,
            status="PENDING_UPLOAD",
            storage_provider=draft.storage_provider,
            storage_key=draft.storage_key,
            mime_type=draft.mime_type,
            size_bytes=draft.size_bytes,
            sha256=draft.sha256,
            idempotency_key=draft.idempotency_key,
            expires_at=draft.expires_at,
        )
        self._rows[asset.id] = asset
        self.create_calls.append(draft.idempotency_key)
        return asset

    def mark_ready(
        self,
        asset: MediaAsset,
        *,
        mime_type: str,
        size_bytes: int,
        width: int,
        height: int,
    ) -> MediaAsset:
        asset.status = "READY"
        asset.mime_type = mime_type
        asset.size_bytes = size_bytes
        asset.width = width
        asset.height = height
        return asset

    def mark_deleted(self, asset: MediaAsset, *, deleted_at: datetime) -> MediaAsset:
        asset.status = "DELETED"
        asset.deleted_at = deleted_at
        return asset

    def set_status(self, asset: MediaAsset, status: str) -> MediaAsset:
        asset.status = status
        return asset


class TransactionProbe:
    """事务开合探针：证明 Provider 网络调用绝不落在数据库事务内。

    `events` 可与 `GenerationUnitOfWork.trace` 共用同一个列表，这样
    「Provider 调用 vs 行锁 vs 提交」的顺序只需读一条事件流。
    """

    def __init__(self, events: list[str] | None = None, *, strict: bool = True) -> None:
        self.open = False
        self.strict = strict
        self.events: list[str] = events if events is not None else []

    def begin(self) -> None:
        self.open = True
        self.events.append("tx:begin")

    def commit(self) -> None:
        self.events.append("tx:commit")

    def rollback(self) -> None:
        self.events.append("tx:rollback")

    def end(self) -> None:
        self.open = False
        self.events.append("tx:end")

    def observe(self, label: str) -> None:
        """记录一次外部调用；`strict` 下事务仍开着就是契约违规，必须立刻失败。"""

        self.events.append(label)
        if self.strict and self.open:
            raise AssertionError(f"{label} 发生在数据库事务内（Provider 调用禁止入事务）。")


class GenerationUnitOfWork(InMemoryUnitOfWork):
    """带内存事务语义的工作单元：快照/回滚 + 事务探针 + 完整媒体仓储。

    `InMemoryUnitOfWork` 只累计 commit/rollback 次数；ENG-06 的第 3 步必须能证明
    「事务中途失败整体回滚、重投安全」（`LEAD-04` §6），因此这里补齐：

    - `__enter__` 抓取检查点（各容器的行与列值），未提交就退出时整体还原；
    - `commit()` 推进检查点，之后的失败只回滚到已提交状态；
    - `probe` 记录 begin/commit/end 与外部调用的先后顺序。
    """

    def __init__(
        self, clock: Callable[[], datetime] | None = None, *, probe: TransactionProbe | None = None
    ) -> None:
        moment = clock or (lambda: datetime.now(timezone.utc))
        super().__init__(moment)
        self._moment = moment
        self.media = InMemoryGenerationMediaRepository(moment)
        # 探针与仓储共用同一条事件流：Provider 调用 vs 行锁 vs 提交的顺序只需读一处
        self.probe = probe or TransactionProbe(events=self.trace)
        self.open = False
        self._committed_in_transaction = False
        self._checkpoint: tuple[Any, Any] | None = None

    # ------------------------------------------------------------------ 事务边界
    def _containers(self) -> tuple[Any, ...]:
        return (
            self.generation_tasks._tasks,
            self.generation_tasks._inputs,
            self.generation_tasks._outputs,
            self.media._rows,
            self.point_wallets._wallets,
            self.point_ledger._entries,
            self.audit_logs._entries,
            self.provider_attempts._attempts,
            self.generation_costs._rows,
        )

    def _capture(self) -> tuple[Any, Any]:
        containers: list[Any] = []
        for container in self._containers():
            if isinstance(container, dict):
                containers.append(list(container.items()))
            else:
                containers.append(list(container))
        values: list[tuple[Any, dict[str, Any]]] = []
        for container in self._containers():
            rows = container.values() if isinstance(container, dict) else container
            for row in rows:
                columns = type(row).__table__.columns
                values.append((row, {column.name: getattr(row, column.name) for column in columns}))
        return (containers, values)

    def _restore(self) -> None:
        checkpoint = self._checkpoint
        if checkpoint is None:
            return
        containers, values = checkpoint
        for container, snapshot in zip(self._containers(), containers, strict=True):
            if isinstance(container, dict):
                container.clear()
                container.update(dict(snapshot))
            else:
                container.clear()
                container.extend(snapshot)
        for row, row_values in values:
            for name, value in row_values.items():
                setattr(row, name, value)

    def _checkpoint_now(self) -> None:
        self._checkpoint = self._capture()

    def __enter__(self) -> GenerationUnitOfWork:
        self._checkpoint_now()
        self._committed_in_transaction = False
        self.open = True
        self.probe.begin()
        return self

    def __exit__(self, *args: object) -> None:
        if not self._committed_in_transaction:
            self._restore()
        self.open = False
        self._committed_in_transaction = False
        self.probe.end()

    def commit(self) -> None:
        self.commits += 1
        self._committed_in_transaction = True
        self._checkpoint_now()
        self.probe.commit()

    def rollback(self) -> None:
        self.rollbacks += 1
        self._restore()
        self._committed_in_transaction = False
        self.probe.rollback()


class QueuedGeneration:
    """一次入队调用的记录（任务 ID + 延迟秒数）。"""

    __slots__ = ("generation_id", "countdown_seconds")

    def __init__(self, generation_id: uuid.UUID, countdown_seconds: int) -> None:
        self.generation_id = generation_id
        self.countdown_seconds = countdown_seconds


class QueuedPoll:
    """一次轮询投递的记录（任务 ID + attempt 序号 + 延迟秒数）。"""

    __slots__ = ("attempt_no", "countdown_seconds", "generation_id")

    def __init__(
        self, generation_id: uuid.UUID, attempt_no: int, countdown_seconds: int
    ) -> None:
        self.generation_id = generation_id
        self.attempt_no = attempt_no
        self.countdown_seconds = countdown_seconds


class RecordingGenerationQueuePort:
    """`GenerationQueuePort` 的内存实现：记录投递并写入共享 `trace`。

    `deliver=False` 复刻 `CeleryGenerationQueue` 的投递失败语义：返回 `False`
    而不抛异常，已提交的账务不得因此回滚（`LEAD-04` §6:119）。
    """

    def __init__(self, trace: list[str] | None = None, *, deliver: bool = True) -> None:
        self.generations: list[QueuedGeneration] = []
        self.polls: list[QueuedPoll] = []
        self.trace = trace
        self.deliver = deliver

    def enqueue_generation(self, generation_id: uuid.UUID, *, countdown_seconds: int = 0) -> bool:
        if not self.deliver:
            return False
        self.generations.append(QueuedGeneration(generation_id, countdown_seconds))
        self._record(f"enqueue:generation:{generation_id}")
        return True

    def enqueue_status_poll(
        self, generation_id: uuid.UUID, *, attempt_no: int, countdown_seconds: int
    ) -> bool:
        if not self.deliver:
            return False
        self.polls.append(QueuedPoll(generation_id, attempt_no, countdown_seconds))
        self._record(f"enqueue:poll:{generation_id}:{attempt_no}")
        return True

    def _record(self, event: str) -> None:
        if self.trace is not None:
            self.trace.append(event)

    @property
    def countdowns(self) -> tuple[int, ...]:
        return tuple(item.countdown_seconds for item in self.generations)


class InMemoryReplayGuard:
    """`ReplayGuard` 的内存实现：三态 + 归属校验（可置为「依赖不可用」）。

    与真实实现同构：占用时写入带 nonce 的状态，只有**自己占到的那次**才能把它
    改成「已对账」或释放；别的请求的占用不受影响。
    """

    def __init__(self, *, unavailable: bool = False) -> None:
        self.claims: dict[str, int] = {}
        self.states: dict[str, str] = {}
        self.released: list[str] = []
        self.confirmed: list[str] = []
        self.unavailable = unavailable
        self._owned: dict[str, str] = {}

    def claim(self, key: str, *, ttl_seconds: int) -> str:
        from app.services.generation.ports import (
            CLAIM_ACQUIRED,
            CLAIM_IN_FLIGHT,
            CLAIM_RECONCILED,
        )

        if self.unavailable:
            raise RuntimeError("synthetic replay store unavailable")
        current = self.states.get(key)
        if current is not None:
            return CLAIM_RECONCILED if str(current).endswith("|reconciled") else CLAIM_IN_FLIGHT
        nonce = f"nonce-{len(self._owned) + 1}"
        self.states[key] = f"{nonce}|processing"
        self.claims[key] = ttl_seconds
        self._owned[key] = nonce
        return CLAIM_ACQUIRED

    def mark_reconciled(self, key: str, *, ttl_seconds: int) -> None:
        from app.services.generation.ports import CLAIM_RECONCILED

        nonce = self._owned.get(key)
        if nonce is None or not str(self.states.get(key, "")).startswith(nonce + "|"):
            return
        self.states[key] = f"{nonce}|reconciled"
        self.claims[key] = ttl_seconds
        self.confirmed.append(key)

    def release(self, key: str) -> None:
        nonce = self._owned.get(key)
        if nonce is None or not str(self.states.get(key, "")).startswith(nonce + "|"):
            return
        self.states.pop(key, None)
        self.claims.pop(key, None)
        self._owned.pop(key, None)
        self.released.append(key)

    def is_reconciled(self, key: str) -> bool:
        return str(self.states.get(key, "")).endswith("|reconciled")


def synthetic_png_bytes(*, size: tuple[int, int] = (4, 4)) -> bytes:
    """真能解码的合成 PNG（与 MockProvider 输出同一条校验路径）。"""

    return synthetic_image_bytes(fmt="PNG", size=size)


def synthetic_image_bytes(*, fmt: str = "PNG", size: tuple[int, int] = (4, 4)) -> bytes:
    """真能完整解码的合成静态图（`PNG`/`JPEG`/`WEBP`），不含任何真实照片。"""

    buffer = io.BytesIO()
    Image.new("RGB", size, (12, 34, 56)).save(buffer, format=fmt)
    return buffer.getvalue()


def png_with_markup_in_metadata(*, size: tuple[int, int] = (4, 4)) -> bytes:
    """一张**结构合法**、但元数据里带 `<svg` 文本的 PNG。

    真实厂商出图就会携带这类内嵌清单/缩略图文本，所以它是「原始字节关键字一票否决」
    与「按容器结构 + 完整解码判定」两种做法的分水岭样本。CRC 必须算对，否则这张图
    连合法 PNG 都不是，测的就变成解码而不是嗅探。
    """

    content = synthetic_png_bytes(size=size)
    payload = b"c2pa\x00<svg xmlns='http://www.w3.org/2000/svg'/>"
    chunk_body = b"tEXt" + payload
    chunk = (
        len(payload).to_bytes(4, "big")
        + chunk_body
        + (zlib.crc32(chunk_body) & 0xFFFFFFFF).to_bytes(4, "big")
    )
    insert_at = len(b"\x89PNG\r\n\x1a\n") + 25  # 签名 + IHDR 整块
    return content[:insert_at] + chunk + content[insert_at:]


class ScriptedProvider:
    """`AIProvider` 替身：按脚本依次产出结果或抛出异常，并记录每次调用。"""

    provider_name = MOCK_PROVIDER_NAME

    def __init__(
        self,
        *,
        generate: Sequence[object] = (),
        check_status: Sequence[object] = (),
        cancel: Sequence[object] = (),
        probe: TransactionProbe | None = None,
        model_name: str = MOCK_MODEL,
    ) -> None:
        self._generate_script = tuple(generate)
        self._check_status_script = tuple(check_status)
        self._cancel_script = tuple(cancel)
        self._probe = probe
        self.model_name = model_name
        self.generate_requests: list[GenerateRequest] = []
        self.status_requests: list[str] = []
        self.cancel_requests: list[str] = []

    @property
    def generate_calls(self) -> int:
        return len(self.generate_requests)

    @property
    def status_calls(self) -> int:
        return len(self.status_requests)

    @property
    def cancel_calls(self) -> int:
        return len(self.cancel_requests)

    def generate(self, request: GenerateRequest) -> ProviderResult:
        self._observe("provider:generate")
        self.generate_requests.append(request)
        return self._next(self._generate_script, len(self.generate_requests), "generate")

    def check_status(self, provider_request_id: str) -> ProviderResult:
        self._observe("provider:check_status")
        self.status_requests.append(provider_request_id)
        return self._next(
            self._check_status_script, len(self.status_requests), "check_status"
        )

    def cancel(self, provider_request_id: str) -> ProviderResult:
        self._observe("provider:cancel")
        self.cancel_requests.append(provider_request_id)
        return self._next(self._cancel_script, len(self.cancel_requests), "cancel")

    def _observe(self, label: str) -> None:
        if self._probe is not None:
            self._probe.observe(label)

    @staticmethod
    def _next(script: tuple[object, ...], index: int, name: str) -> ProviderResult:
        if index > len(script):
            raise AssertionError(f"ScriptedProvider.{name} 脚本已用尽（第 {index} 次调用）。")
        item = script[index - 1]
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, ProviderResult), f"{name} 脚本项必须是 ProviderResult"
        return item


class ProbingProvider:
    """把任意 `AIProvider` 包一层事务探针：每次调用前断言没有打开的事务。"""

    def __init__(self, inner: AIProvider, probe: TransactionProbe) -> None:
        self._inner = inner
        self._probe = probe
        self.generate_requests: list[GenerateRequest] = []

    @property
    def provider_name(self) -> str:
        return getattr(self._inner, "provider_name", MOCK_PROVIDER_NAME)

    def generate(self, request: GenerateRequest) -> ProviderResult:
        self._probe.observe("provider:generate")
        self.generate_requests.append(request)
        return self._inner.generate(request)

    def check_status(self, provider_request_id: str) -> ProviderResult:
        self._probe.observe("provider:check_status")
        return self._inner.check_status(provider_request_id)

    def cancel(self, provider_request_id: str) -> ProviderResult:
        self._probe.observe("provider:cancel")
        return self._inner.cancel(provider_request_id)


class SetNxRedisClient:
    """独立 Redis 连接的行为替身（原子 `SET ... NX EX` + `INCR` + `EXPIRE`）。

    与 `FakeRedis` 分开是有意的：`FakeRedis` 模拟冻结的 `RedisPort`
    （只有 `set/get/delete/expire`，没有原子占用与计数能力），而 Webhook 的事件 ID 去重
    与参考图分析的付费闸门都必须靠**原子命令**才成立——「先 GET 再 SET」的并发请求
    会双双读到「没有」，于是一起放行。因此这两处走这条独立连接。
    返回值语义与真实 Redis 一致：占用成功返回 `True`，键已存在且 `nx=True` 返回 `None`；
    `INCR` 新建的计数器不带 TTL。
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._values: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.calls: list[tuple[Any, ...]] = []

    def set(
        self, name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        self.calls.append(("set", name, value, ex, nx))
        self._purge_if_expired(name)
        moment = self._clock().timestamp()
        if nx and name in self._values:
            return None
        self._values[name] = value
        if ex is None:
            self._expires_at.pop(name, None)
        else:
            self._expires_at[name] = moment + ex
        return True

    def get(self, name: str) -> str | None:
        self._purge_if_expired(name)
        return self._values.get(name)

    def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if self._values.pop(name, None) is not None:
                removed += 1
            self._expires_at.pop(name, None)
        return removed

    def incr(self, name: str) -> int:
        """原子自增；与真实 Redis 一致，新建的计数器**不带** TTL。"""

        self._purge_if_expired(name)
        current = self._values.get(name)
        if current is None:
            number = 1
        else:
            try:
                number = int(current) + 1
            except ValueError as error:  # pragma: no cover - 真实 Redis 同样拒绝
                raise ValueError(f"{name} 不是计数器。") from error
        self._values[name] = str(number)
        return number

    def expire(self, name: str, seconds: int) -> bool:
        self._purge_if_expired(name)
        if name not in self._values:
            return False
        self._expires_at[name] = self._clock().timestamp() + seconds
        return True

    def ttl(self, name: str) -> int:
        self._purge_if_expired(name)
        deadline = self._expires_at.get(name)
        if name not in self._values:
            return -2
        if deadline is None:
            return -1
        return max(0, int(deadline - self._clock().timestamp()))

    def _purge_if_expired(self, name: str) -> None:
        deadline = self._expires_at.get(name)
        if deadline is not None and deadline <= self._clock().timestamp():
            self._values.pop(name, None)
            self._expires_at.pop(name, None)

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._values))


# --------------------------------------------------------------------------- #
# ENG-09：分享 / 反馈 / 生命周期替身
# --------------------------------------------------------------------------- #
class InMemoryShareRepository:
    """`ShareRepository` 的内存实现（含多图关联与并发唯一键裁决）。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._moment = clock or (lambda: datetime.now(timezone.utc))
        self._rows: dict[uuid.UUID, Share] = {}
        self._assets: list[ShareAsset] = []
        self._asset_registry: dict[uuid.UUID, MediaAsset] = {}
        self.lock_share_calls: list[uuid.UUID] = []
        self.lock_asset_calls: list[uuid.UUID] = []
        self.refresh_asset_calls: list[uuid.UUID] = []
        self.lock_scope_calls: list[tuple[uuid.UUID, str]] = []

    # ------------------------------------------------------------------ 只读
    @property
    def rows(self) -> tuple[Share, ...]:
        return tuple(self._rows.values())

    def asset_links(self) -> tuple[ShareAsset, ...]:
        return tuple(self._assets)

    def get_by_slug(self, slug: str) -> Share | None:
        return next((row for row in self._rows.values() if row.slug == slug), None)

    def get_by_id(self, share_id: uuid.UUID) -> Share | None:
        return self._rows.get(share_id)

    def lock_by_id(self, share_id: uuid.UUID) -> Share | None:
        self.lock_share_calls.append(share_id)
        return self._rows.get(share_id)

    def lock_asset(self, asset_id: uuid.UUID) -> MediaAsset | None:
        self.lock_asset_calls.append(asset_id)
        return self.get_asset(asset_id)

    def lock_share_ids(self, share_ids: list[uuid.UUID]) -> tuple[Share, ...]:
        """按传入顺序（必须升序）"加锁"读取分享行。

        内存替身没有行锁，但**保留同一条顺序契约**：顺序错了直接报错，
        这样「先锁资产再锁分享」的写法会在单测里立刻暴露。
        """

        if list(share_ids) != sorted(share_ids, key=str):
            raise ValueError("lock_share_ids 要求按 id 升序传入")
        rows: list[Share] = []
        for share_id in share_ids:
            self.lock_share_calls.append(share_id)
            row = self._rows.get(share_id)
            if row is not None:
                rows.append(row)
        return tuple(rows)

    def lock_active_for_asset(self, asset_id: uuid.UUID) -> tuple[Share, ...]:
        """引用该资产的 ACTIVE 分享，按 id 升序（记录取锁调用）。"""

        rows = sorted(
            (
                row
                for row in self._active_for_asset(asset_id)
            ),
            key=lambda row: str(row.id),
        )
        for row in rows:
            self.lock_share_calls.append(row.id)
        return tuple(rows)

    def count_active_for_asset(self, asset_id: uuid.UUID) -> int:
        return len(self._active_for_asset(asset_id))

    def count_active_for_assets(
        self, asset_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        return {
            asset_id: len(self._active_for_asset(asset_id)) for asset_id in asset_ids
        }

    def peek_expired_shares(self, *, now: datetime, limit: int) -> tuple[Share, ...]:
        return self.list_expired(now=now, limit=limit)

    def peek_active_for_assets(self, asset_ids: list[uuid.UUID]) -> tuple[Share, ...]:
        rows = [
            row
            for asset_id in asset_ids
            for row in self._active_for_asset(asset_id)
        ]
        unique = {row.id: row for row in rows}
        return tuple(unique[key] for key in sorted(unique, key=str))

    def peek_active_for_invalid_assets(
        self, *, now: datetime, limit: int
    ) -> tuple[Share, ...]:
        """引用失效资产的 ACTIVE 分享（分享收敛的驱动集合）。

        以分享为行，因此关闭一条就少一条，不会因为"已收敛的失效资产仍占据候选
        头部"而饿死后排 —— 与真实仓储同一口径。
        """

        rows = [
            row
            for share_id, row in self._rows.items()
            if row.status == STATUS_ACTIVE
            and any(
                self._asset_invalid(self.get_asset(asset_id), now=now)
                for asset_id in self._covered_asset_ids(share_id, row)
            )
        ]
        rows.sort(key=lambda row: str(row.id))
        return tuple(rows[:limit])

    def _covered_asset_ids(
        self, share_id: uuid.UUID, row: Share
    ) -> list[uuid.UUID]:
        """该分享**授权过的**资产：关联表有行按选中集合，无行回落主资产。"""

        linked = [item.asset_id for item in self._assets if item.share_id == share_id]
        return linked if linked else [row.asset_id]

    @staticmethod
    def _asset_invalid(asset: MediaAsset | None, *, now: datetime) -> bool:
        """与 `app.services.shares.lifecycle.asset_is_invalid` 同一口径。"""

        if asset is None:
            return True
        if asset.deleted_at is not None:
            return True
        if asset.status in ("DELETED", "EXPIRED"):
            return True
        if asset.expires_at is None:
            return False
        deadline = asset.expires_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline <= now

    def lock_expired_shares(self, *, now: datetime, limit: int) -> tuple[Share, ...]:
        rows = sorted(self.list_expired(now=now, limit=limit), key=lambda row: str(row.id))
        for row in rows:
            self.lock_share_calls.append(row.id)
        return tuple(rows)

    def lock_assets_by_id(self, asset_ids: list[uuid.UUID]) -> tuple[MediaAsset, ...]:
        """内存替身按同一条契约断言排序：顺序错了直接报错，不静默通过。

        与真实实现保持同一纪律 —— 「按请求数组顺序取锁」必须在这里就被拦下，
        而不是等到真实 PostgreSQL 的并发用例里偶发死锁。
        """

        if list(asset_ids) != sorted(asset_ids, key=str):
            raise ValueError("lock_assets_by_id 要求按 id 升序传入")
        rows: list[MediaAsset] = []
        for asset_id in asset_ids:
            self.lock_asset_calls.append(asset_id)
            asset = self.get_asset(asset_id)
            if asset is not None:
                rows.append(asset)
        return tuple(rows)

    def refresh_asset(self, asset: MediaAsset) -> None:
        """内存替身没有 identity map，无需刷新（记录调用以便断言）。"""

        self.refresh_asset_calls.append(asset.id)

    def lock_idempotency_scope(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> None:
        """内存替身不排队，而是**记录**调用：并发串行化由真实 PG 用例证明。"""

        self.lock_scope_calls.append((user_id, idempotency_key))

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> Share | None:
        return next(
            (
                row
                for row in self._rows.values()
                if row.user_id == user_id and row.idempotency_key == idempotency_key
            ),
            None,
        )

    def get_asset(self, asset_id: uuid.UUID) -> MediaAsset | None:
        return self._asset_registry.get(asset_id)

    def list_selected_assets(self, share_id: uuid.UUID) -> tuple[ShareAssetLink, ...]:
        links: list[ShareAssetLink] = []
        for row in sorted(
            (item for item in self._assets if item.share_id == share_id),
            key=lambda item: item.sort_order,
        ):
            asset = self.get_asset(row.asset_id)
            if asset is None:
                continue
            links.append(
                ShareAssetLink(
                    asset_id=row.asset_id,
                    sort_order=row.sort_order,
                    mime_type=asset.mime_type,
                    width=asset.width,
                    height=asset.height,
                    size_bytes=asset.size_bytes,
                )
            )
        return tuple(links)

    def list_valid_for_asset(
        self, asset_id: uuid.UUID, *, now: datetime
    ) -> tuple[Share, ...]:
        return tuple(
            row
            for row in self._active_for_asset(asset_id)
            if row.expires_at is None or row.expires_at > now
        )

    def list_selected_asset_ids(self, share_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        return tuple(
            row.asset_id
            for row in sorted(
                (item for item in self._assets if item.share_id == share_id),
                key=lambda item: item.sort_order,
            )
        )

    def earliest_expiry_for_assets(
        self, asset_ids: tuple[uuid.UUID, ...]
    ) -> datetime | None:
        deadlines = [
            asset.expires_at
            for asset_id in asset_ids
            if (asset := self.get_asset(asset_id)) is not None
            and asset.expires_at is not None
        ]
        return min(deadlines) if deadlines else None

    def list_expired(self, *, now: datetime, limit: int) -> tuple[Share, ...]:
        rows = [
            row
            for row in self._rows.values()
            if row.status == STATUS_ACTIVE
            and row.expires_at is not None
            and row.expires_at <= now
        ]
        rows.sort(key=lambda row: (row.expires_at, str(row.id)))
        return tuple(rows[:limit])

    def list_active_for_asset(self, asset_id: uuid.UUID) -> tuple[Share, ...]:
        return self._active_for_asset(asset_id)

    def _active_for_asset(self, asset_id: uuid.UUID) -> list[Share]:
        """引用该资产的 ACTIVE 分享，**与真实仓储同一套命中语义**。

        `share_assets` 有行 → 只按明确选中的集合；无行 → 回落 `shares.asset_id`
        （0021 之前的历史单图分享）。替身必须一致，否则服务层单测会掩盖
        「历史分享仍 ACTIVE、资产却被收敛成 PRIVATE」这类缺陷。
        """

        return [
            row
            for share_id, row in self._rows.items()
            if row.status == STATUS_ACTIVE and self._covers_asset(share_id, row, asset_id)
        ]

    def _covers_asset(
        self, share_id: uuid.UUID, row: Share, asset_id: uuid.UUID
    ) -> bool:
        linked = {item.asset_id for item in self._assets if item.share_id == share_id}
        if linked:
            return asset_id in linked
        return row.asset_id == asset_id

    # ------------------------------------------------------------------ 写入
    def create(self, draft: ShareDraft, *, created_at: datetime) -> Share:
        if self.find_by_idempotency_key(
            user_id=draft.user_id, idempotency_key=draft.idempotency_key
        ):
            raise DuplicateShare()
        share = Share(
            slug=draft.slug,
            user_id=draft.user_id,
            generation_id=draft.generation_id,
            asset_id=draft.asset_id,
            title=draft.title,
            status=STATUS_ACTIVE,
            expires_at=draft.expires_at,
            idempotency_key=draft.idempotency_key,
            created_at=created_at,
        )
        share.id = uuid.uuid4()
        self._rows[share.id] = share
        return share

    def add_assets(
        self,
        share: Share,
        links: tuple[ShareAssetDraft, ...],
        *,
        created_at: datetime,
    ) -> None:
        for link in links:
            self._assets.append(
                ShareAsset(
                    share_id=share.id,
                    asset_id=link.asset_id,
                    sort_order=link.sort_order,
                    created_at=created_at,
                )
            )

    def set_status(self, share: Share, status: str, *, changed_at: datetime) -> Share:
        share.status = status
        if status == STATUS_REVOKED:
            share.revoked_at = changed_at
        return share

    def set_asset_visibility(self, asset_id: uuid.UUID, visibility: str) -> None:
        asset = self.get_asset(asset_id)
        if asset is not None:
            asset.visibility = visibility

    # ------------------------------------------------------------------ 接线
    def bind_asset_registry(self, registry: dict[uuid.UUID, MediaAsset]) -> None:
        """与 UoW 的媒体仓储共用同一份资产字典（避免两处真相漂移）。"""

        self._asset_registry = registry


class InMemoryFeedbackRepository:
    """`FeedbackRepository` 的内存实现（含 `(user_id, generation_id)` 唯一裁决）。"""

    def __init__(self) -> None:
        self._rows: list[Feedback] = []
        self.lock_scope_calls: list[tuple[uuid.UUID, str]] = []

    @property
    def rows(self) -> tuple[Feedback, ...]:
        return tuple(self._rows)

    def lock_idempotency_scope(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> None:
        """内存替身只记录调用；并发串行化由真实 PG 用例证明。"""

        self.lock_scope_calls.append((user_id, idempotency_key))

    def find(self, *, user_id: uuid.UUID, generation_id: uuid.UUID) -> Feedback | None:
        return next(
            (
                row
                for row in self._rows
                if row.user_id == user_id and row.generation_id == generation_id
            ),
            None,
        )

    def create(self, draft: FeedbackDraft, *, created_at: datetime) -> Feedback:
        if self.find(user_id=draft.user_id, generation_id=draft.generation_id):
            raise DuplicateFeedback()
        row = Feedback(
            user_id=draft.user_id,
            generation_id=draft.generation_id,
            rating=draft.rating,
            category=draft.category,
            content=draft.content,
            status="OPEN",
            created_at=created_at,
            updated_at=created_at,
        )
        row.id = uuid.uuid4()
        self._rows.append(row)
        return row


class InMemoryLifecycleMediaRepository(InMemoryGenerationMediaRepository):
    """在 ENG-06 完整媒体替身上补生命周期需要的方法（只新增，不改既有行为）。"""

    def peek_expired(self, *, now: datetime, limit: int) -> tuple[MediaAsset, ...]:
        """到期候选（不加锁）。"""

        return self.list_expired(now=now, limit=limit)

    def peek_invalid(self, *, now: datetime, limit: int) -> tuple[MediaAsset, ...]:
        """已失效资产（逻辑删除 / 已终结 `DELETED`·`EXPIRED` / 已过到期时间），按 id 升序。

        与真实仓储同一口径：**终结状态也算失效**，否则「资产已被清理、分享仍 ACTIVE」
        的公开链接永远收敛不掉。它只用于分享收敛的目标选取；资产清理另走
        `peek_expired`（限未终结状态），因此终结资产不会被再次删除对象。
        """

        rows = [
            row
            for row in self._rows.values()
            if row.deleted_at is not None
            or row.status in ("DELETED", "EXPIRED")
            or (row.expires_at is not None and row.expires_at <= now)
        ]
        rows.sort(key=lambda row: str(row.id))
        return tuple(rows[:limit])

    def lock_assets_by_id(self, asset_ids: list[uuid.UUID]) -> tuple[MediaAsset, ...]:
        """按传入顺序（必须升序）"加锁"读取资产行。"""

        if list(asset_ids) != sorted(asset_ids, key=str):
            raise ValueError("lock_assets_by_id 要求按 id 升序传入")
        rows: list[MediaAsset] = []
        for asset_id in asset_ids:
            asset = self._rows.get(asset_id)
            if asset is not None:
                rows.append(asset)
        return tuple(rows)

    def list_expired(self, *, now: datetime, limit: int) -> tuple[MediaAsset, ...]:
        rows = [
            row
            for row in self._rows.values()
            if row.expires_at is not None
            and row.expires_at <= now
            and row.deleted_at is None
            and row.status in ("PENDING_UPLOAD", "READY")
        ]
        rows.sort(key=lambda row: (row.expires_at, str(row.id)))
        return tuple(rows[:limit])


def make_engagement_uow_factory(
    uow: "EngagementUnitOfWork",
) -> Callable[[], "EngagementUnitOfWork"]:
    """把单个内存工作单元包装成工厂（供 `MediaLifecycleService` 装配）。

    真实装配每次都新建工作单元；内存替身复用同一个实例，因此事务语义由
    `EngagementUnitOfWork` 自己的快照/回滚负责（异常仍会 rollback）。
    """

    return lambda: uow


class EngagementUnitOfWork(GenerationUnitOfWork):
    """ENG-09 的测试工作单元：在 ENG-06 替身之上补齐分享/反馈与幂等登记。

    只做加法：`generation_tasks`、`media`、`point_wallets` 等既有替身语义不变；
    分享与反馈替身参与同一套快照/回滚，因此「创建分享 + 置 SHARED 同一事务」
    这条要求在**失败回滚**方向上也能被真实断言。
    """

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        probe: TransactionProbe | None = None,
    ) -> None:
        raw = clock or (lambda: datetime.now(timezone.utc))
        super().__init__(raw, probe=probe)
        self.media = InMemoryLifecycleMediaRepository(raw)
        self.shares = InMemoryShareRepository(raw)
        self.shares.bind_asset_registry(self.media._rows)
        self.feedback = InMemoryFeedbackRepository()
        # 幂等登记写进同一套审计仓储：因此它和业务行一样参与快照/回滚
        self._ledger = InMemoryIdempotencyLedger(self.audit_logs)

    @property
    def ledger(self) -> InMemoryIdempotencyLedger:  # type: ignore[override]
        return self._ledger

    def _containers(self) -> tuple[Any, ...]:
        return super()._containers() + (
            self.shares._rows,
            self.feedback._rows,
        )
