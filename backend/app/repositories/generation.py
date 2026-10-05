"""生成任务与其输入/输出、成本配置的数据访问（ENG-04 建立，ENG-06 第 3 步扩展）。

用途与边界：

- `LEAD-04` §6:127 冻结统一锁顺序：**先锁 `generation_tasks`，再锁 `point_wallets`**；
- `billing_status` 由 Points Service 通过 `set_billing_status` 更新（§10.5）；
- 任务生命周期 `status` 由 Generation Service 通过**唯一写入口** `transition` 更新，
  Repository 自身不做任何状态机判断（合法性由 `services.generation.state` 裁决），
  也不自行 `commit`（`06-development-standard.md` 第 2 节）；
- 取值集合以数据库 CHECK 为唯一权威（`status`、`billing_status`、`mode`、`role`），
  Python 层刻意不重复校验，避免两处真相漂移；
- `generation_inputs` / `generation_outputs` 与 `generation_cost_configs` 属同一聚合，
  因此一并放在本模块，不另建仓储文件。

`create` 在唯一键 `(user_id, idempotency_key)` 冲突时抛 `DuplicateIdempotencyKey`：
数据库部分唯一索引是并发下的最终裁决者，调用方必须回滚后重查再判定
「回放 / `IDEMPOTENCY_CONFLICT` / `IDEMPOTENCY_IN_PROGRESS`」。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.generation import (
    GenerationCostConfig,
    GenerationInput,
    GenerationOutput,
    GenerationTask,
)

__all__ = [
    "DuplicateIdempotencyKey",
    "GenerationCostConfigRepository",
    "GenerationInputDraft",
    "GenerationOutputDraft",
    "GenerationTaskDraft",
    "GenerationTaskRepository",
    "KeysetPage",
    "SqlAlchemyGenerationTaskRepository",
    "SqlAlchemyGenerationCostConfigRepository",
]


class DuplicateIdempotencyKey(RuntimeError):
    """`(user_id, idempotency_key)` 已存在（唯一索引裁决）。"""


@dataclass(frozen=True, slots=True)
class GenerationTaskDraft:
    """一条待创建的任务行（成本与预留由 Generation Service 推导）。"""

    user_id: uuid.UUID
    mode: str
    prompt: str
    requested_count: int
    cost_config_id: uuid.UUID
    cost_snapshot: dict[str, Any]
    reserved_points: int
    idempotency_key: str
    status: str
    billing_status: str
    style_id: uuid.UUID | None = None
    prompt_template_id: uuid.UUID | None = None
    parent_task_id: uuid.UUID | None = None
    provider_name: str | None = None
    model_name: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationInputDraft:
    generation_id: uuid.UUID
    asset_id: uuid.UUID
    role: str
    sort_order: int


@dataclass(frozen=True, slots=True)
class GenerationOutputDraft:
    """一条结果资源引用。

    `idempotency_key` 是结果图 `media_assets` 的操作级键：Worker 至少一次投递下，
    同一 `(user_id, key)` 只能落地一份资源。
    """

    generation_id: uuid.UUID
    asset_id: uuid.UUID
    output_index: int
    provider_output_id: str | None = None


@dataclass(frozen=True, slots=True)
class KeysetPage:
    """`(created_at, id)` 键集分页结果（契约第 2.2 节）。"""

    rows: tuple[GenerationTask, ...]
    has_more: bool


@runtime_checkable
class GenerationTaskRepository(Protocol):
    """生成任务聚合端口。"""

    def get_by_id(self, generation_id: uuid.UUID) -> GenerationTask | None: ...

    def lock_by_id(self, generation_id: uuid.UUID) -> GenerationTask | None:
        """`SELECT ... FOR UPDATE`（结算与状态转换事务的第一把锁）。"""
        ...

    def create(self, draft: GenerationTaskDraft, *, created_at: datetime) -> GenerationTask: ...

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> GenerationTask | None: ...

    def set_billing_status(self, task: GenerationTask, billing_status: str) -> GenerationTask:
        """账务状态写入口（Points Service 独占，不触碰生命周期 `status`）。"""
        ...

    def transition(
        self,
        task: GenerationTask,
        *,
        status: str,
        changed_at: datetime,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> GenerationTask:
        """生命周期状态唯一写入口（Generation Service 独占）。

        只写状态、对应的阶段时间戳与失败字段：**绝不触碰 `billing_status`**，
        也不写流水；调用方必须在同一事务内完成结算。
        """
        ...

    def record_retry(self, task: GenerationTask, *, changed_at: datetime) -> GenerationTask:
        """`retry_count += 1`（传输层重试计数，不改变任务状态）。"""
        ...

    def list_for_user(
        self, *, user_id: uuid.UUID, cursor: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> KeysetPage: ...

    def list_stale(
        self, *, statuses: tuple[str, ...], older_than: datetime, limit: int
    ) -> tuple[GenerationTask, ...]:
        """恢复扫描用：按 `created_at` 找出停留在给定状态过久的任务。"""
        ...

    def list_timed_out(
        self, *, status: str, processing_before: datetime, limit: int
    ) -> tuple[GenerationTask, ...]:
        """整体时限判定：只按进入 `PROCESSING` 的时间，不按创建时间。"""
        ...

    def create_input(self, draft: GenerationInputDraft) -> GenerationInput: ...

    def list_inputs(self, generation_id: uuid.UUID) -> tuple[GenerationInput, ...]: ...

    def create_output(self, draft: GenerationOutputDraft) -> GenerationOutput: ...

    def list_outputs(self, generation_id: uuid.UUID) -> tuple[GenerationOutput, ...]: ...


@runtime_checkable
class GenerationCostConfigRepository(Protocol):
    """用户点数成本配置端口。

    读取面是热路径（每次创建任务都要解析价格）；写入面**只**给部署期的初始化/调价
    入口（`app.bootstrap.cost_configs`）使用：账务 Service 与路由都不写这张表，
    价格永远由运维显式提交，不由代码默认值产生。
    """

    def find_active(
        self, *, mode: str, model_name: str | None, output_count: int, at: datetime
    ) -> GenerationCostConfig | None:
        """取生效窗口内的激活配置：**精确型号优先，`NULL` 兜底**，都没有返回 `None`。

        这条规则是「创建任务」「`/health/ready` 自检」「定价初始化」三处**共用**的取值口径，
        实现细节与理由见 `SqlAlchemyGenerationCostConfigRepository.find_active`。
        """
        ...

    def list_active(self, *, at: datetime) -> tuple[GenerationCostConfig, ...]: ...

    def add(
        self,
        *,
        mode: str,
        output_count: int,
        points_cost: int,
        model_name: str | None,
        effective_from: datetime,
    ) -> GenerationCostConfig: ...

    def close(self, config: GenerationCostConfig, *, closed_at: datetime) -> GenerationCostConfig: ...


def _task_timestamp_column(status: str) -> str | None:
    """状态 → 阶段时间戳列（`02-database-design.md` 第 3.2 节）。"""

    return {
        "QUEUED": "queued_at",
        "PROCESSING": "processing_at",
        "SUCCEEDED": "completed_at",
        "FAILED": "completed_at",
    }.get(status)


class SqlAlchemyGenerationTaskRepository:
    """SQLAlchemy 实现（只 flush，不提交事务）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, generation_id: uuid.UUID) -> GenerationTask | None:
        statement = select(GenerationTask).where(GenerationTask.id == generation_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def lock_by_id(self, generation_id: uuid.UUID) -> GenerationTask | None:
        statement = (
            select(GenerationTask).where(GenerationTask.id == generation_id).with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def create(self, draft: GenerationTaskDraft, *, created_at: datetime) -> GenerationTask:
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
            created_at=created_at,
            updated_at=created_at,
        )
        self._session.add(task)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # 并发下由 `(user_id, idempotency_key)` 唯一索引裁决；调用方回滚后重查
            raise DuplicateIdempotencyKey(draft.idempotency_key) from exc
        return task

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> GenerationTask | None:
        statement = (
            select(GenerationTask)
            .where(
                GenerationTask.user_id == user_id,
                GenerationTask.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def set_billing_status(
        self, task: GenerationTask, billing_status: str
    ) -> GenerationTask:
        task.billing_status = billing_status
        self._session.flush()
        return task

    def transition(
        self,
        task: GenerationTask,
        *,
        status: str,
        changed_at: datetime,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> GenerationTask:
        task.status = status
        timestamp_column = _task_timestamp_column(status)
        if timestamp_column is not None:
            setattr(task, timestamp_column, changed_at)
        if failure_code is not None:
            task.failure_code = failure_code
        if failure_message is not None:
            task.failure_message = failure_message
        task.updated_at = changed_at
        self._session.flush()
        return task

    def record_retry(self, task: GenerationTask, *, changed_at: datetime) -> GenerationTask:
        task.retry_count = task.retry_count + 1
        task.updated_at = changed_at
        self._session.flush()
        return task

    def list_for_user(
        self, *, user_id: uuid.UUID, cursor: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> KeysetPage:
        statement = (
            select(GenerationTask)
            .where(GenerationTask.user_id == user_id)
            .order_by(
                GenerationTask.created_at.desc(),
                GenerationTask.id.desc(),
            )
            .limit(limit + 1)
        )
        if cursor is not None:
            created_at, task_id = cursor
            statement = statement.where(
                (GenerationTask.created_at < created_at)
                | (
                    (GenerationTask.created_at == created_at)
                    & (GenerationTask.id < task_id)
                )
            )
        rows = tuple(self._session.execute(statement).scalars())
        return KeysetPage(rows=rows[:limit], has_more=len(rows) > limit)

    def list_stale(
        self, *, statuses: tuple[str, ...], older_than: datetime, limit: int
    ) -> tuple[GenerationTask, ...]:
        statement = (
            select(GenerationTask)
            .where(
                GenerationTask.status.in_(statuses),
                GenerationTask.created_at <= older_than,
            )
            .order_by(GenerationTask.created_at, GenerationTask.id)
            .limit(limit)
        )
        return tuple(self._session.execute(statement).scalars())

    def list_timed_out(
        self, *, status: str, processing_before: datetime, limit: int
    ) -> tuple[GenerationTask, ...]:
        statement = (
            select(GenerationTask)
            .where(
                GenerationTask.status == status,
                GenerationTask.processing_at.is_not(None),
                GenerationTask.processing_at <= processing_before,
            )
            .order_by(GenerationTask.processing_at, GenerationTask.id)
            .limit(limit)
        )
        return tuple(self._session.execute(statement).scalars())

    def create_input(self, draft: GenerationInputDraft) -> GenerationInput:
        row = GenerationInput(
            generation_id=draft.generation_id,
            asset_id=draft.asset_id,
            role=draft.role,
            sort_order=draft.sort_order,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_inputs(self, generation_id: uuid.UUID) -> tuple[GenerationInput, ...]:
        statement = (
            select(GenerationInput)
            .where(GenerationInput.generation_id == generation_id)
            .order_by(GenerationInput.role, GenerationInput.sort_order)
        )
        return tuple(self._session.execute(statement).scalars())

    def create_output(self, draft: GenerationOutputDraft) -> GenerationOutput:
        row = GenerationOutput(
            generation_id=draft.generation_id,
            asset_id=draft.asset_id,
            output_index=draft.output_index,
            provider_output_id=draft.provider_output_id,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DuplicateIdempotencyKey(
                f"output:{draft.generation_id}:{draft.output_index}"
            ) from exc
        return row

    def list_outputs(self, generation_id: uuid.UUID) -> tuple[GenerationOutput, ...]:
        statement = (
            select(GenerationOutput)
            .where(GenerationOutput.generation_id == generation_id)
            .order_by(GenerationOutput.output_index)
        )
        return tuple(self._session.execute(statement).scalars())


class SqlAlchemyGenerationCostConfigRepository:
    """按生效窗口读取激活成本配置。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_active(
        self, *, mode: str, model_name: str | None, output_count: int, at: datetime
    ) -> GenerationCostConfig | None:
        """按生效窗口取激活配置；**精确型号优先，`NULL` 兜底**。

        ## 为什么必须是「精确优先」而不是「谁新谁赢」

        同一 `(mode, output_count)` 下可以同时存在「某个型号的专价」与「全部型号的兜底价」
        （`02-database-design.md` §3.2：`model_name IS NULL` 表示对所有型号生效）。
        如果只按 `effective_from` 取最新一条，那么**后来补的一条兜底价会把已有专价顶掉**，
        而「创建任务」「`/health/ready` 自检」「定价初始化」三处必须给出同一个答案 ——
        否则会出现「健康检查绿了、创建任务却 503」这种最难排查的组合（实测踩到）。

        因此：给了 `model_name` 就先只找它自己的那条；没找到才回落到 `NULL`。
        两者都没有就是**缺配置**，由调用方按既有契约返回 `NOT_READY`（503），
        这里**绝不**返回任何默认价。
        """

        def _query(*, exact: str | None) -> GenerationCostConfig | None:
            statement = (
                select(GenerationCostConfig)
                .where(
                    GenerationCostConfig.status == "ACTIVE",
                    GenerationCostConfig.mode == mode,
                    GenerationCostConfig.output_count == output_count,
                    GenerationCostConfig.effective_from <= at,
                    (GenerationCostConfig.effective_to.is_(None))
                    | (GenerationCostConfig.effective_to > at),
                )
                .order_by(GenerationCostConfig.effective_from.desc())
                .limit(1)
            )
            if exact is None:
                statement = statement.where(GenerationCostConfig.model_name.is_(None))
            else:
                statement = statement.where(GenerationCostConfig.model_name == exact)
            return self._session.execute(statement).scalar_one_or_none()

        if model_name is not None:
            exact_row = _query(exact=model_name)
            if exact_row is not None:
                return exact_row
        return _query(exact=None)

    def list_active(self, *, at: datetime) -> tuple[GenerationCostConfig, ...]:
        """当前生效窗口内的全部激活配置（部署期一致性检查用，只读）。"""

        statement = (
            select(GenerationCostConfig)
            .where(
                GenerationCostConfig.status == "ACTIVE",
                GenerationCostConfig.effective_from <= at,
                (GenerationCostConfig.effective_to.is_(None))
                | (GenerationCostConfig.effective_to > at),
            )
            .order_by(GenerationCostConfig.mode, GenerationCostConfig.output_count)
        )
        return tuple(self._session.execute(statement).scalars())

    def add(
        self,
        *,
        mode: str,
        output_count: int,
        points_cost: int,
        model_name: str | None,
        effective_from: datetime,
    ) -> GenerationCostConfig:
        """追加一条激活配置（只 flush，事务由调用方持有）。

        历史行**永不修改**：调价走「关闭旧窗口 + 追加新行」，`cost_snapshot` 引用的
        旧行因此始终能被读回（`02-database-design.md` 第 3.2 节）。
        """

        config = GenerationCostConfig(
            mode=mode,
            model_name=model_name,
            output_count=output_count,
            points_cost=points_cost,
            effective_from=effective_from,
            effective_to=None,
            status="ACTIVE",
        )
        self._session.add(config)
        self._session.flush()
        return config

    def close(
        self, config: GenerationCostConfig, *, closed_at: datetime
    ) -> GenerationCostConfig:
        """结束一条配置的生效窗口并置为 `INACTIVE`（不删除、不改价格）。"""

        config.status = "INACTIVE"
        config.effective_to = closed_at
        self._session.flush()
        return config
