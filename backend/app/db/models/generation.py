"""生成任务、计费配置与输入输出模型。

表：`generation_cost_configs`、`generation_tasks`、`generation_inputs`、`generation_outputs`
唯一来源：`docs/architecture/02-database-design.md` 第 3.3 节。

- 任务状态与计费状态分离：`status` 管任务生命周期，`billing_status` 管账务终态；
- `cost_snapshot` 固化创建时的计费配置，历史任务不随配置修改而变化；
- 重试创建新任务并写 `parent_task_id`，旧任务只读保留。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

__all__ = [
    "GenerationCostConfig",
    "GenerationInput",
    "GenerationOutput",
    "GenerationTask",
]

_TASK_STATUS_CHECK = (
    "status IN ('CREATED', 'QUEUED', 'PROCESSING', 'SUCCEEDED', "
    "'FAILED', 'NEEDS_REVIEW')"
)
_TASK_MODE_CHECK = "mode IN ('PHOTO_PROMPT', 'TEXT_PROMPT', 'REFERENCE_IMAGE')"


class GenerationCostConfig(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`generation_cost_configs`：用户点数成本配置。"""

    __tablename__ = "generation_cost_configs"

    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_count: Mapped[int] = mapped_column(Integer, nullable=False)
    points_cost: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("1"), default=Decimal("1")
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="status"),
        CheckConstraint("output_count > 0", name="output_count_positive"),
        CheckConstraint("points_cost > 0", name="points_cost_positive"),
        CheckConstraint("cost_multiplier > 0", name="cost_multiplier_positive"),
        CheckConstraint("effective_to > effective_from", name="effective_window"),
        # PostgreSQL 15+：`model_name` 可空，默认 NULL 互不相等会让同一
        # mode + NULL model_name + output_count + effective_from 重复写入，
        # 因此唯一键必须使用 NULLS NOT DISTINCT。
        UniqueConstraint(
            "mode",
            "model_name",
            "output_count",
            "effective_from",
            name="uq_generation_cost_configs_window",
            postgresql_nulls_not_distinct=True,
        ),
        # 设计文档 §7（revision 0008「生效索引」）要求按状态与生效时间查询；
        # 该索引已同步登记到 02-database-design.md 第 5 节索引清单。
        Index(
            "ix_generation_cost_configs_status_effective_from",
            "status",
            "effective_from",
        ),
    )


class GenerationTask(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """`generation_tasks`：异步生成任务与计费状态。"""

    __tablename__ = "generation_tasks"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="RESTRICT"),
        nullable=True,
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    billing_status: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("styles.id", ondelete="RESTRICT"),
        nullable=True,
    )
    prompt_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prompt_templates.id", ondelete="RESTRICT"),
        nullable=True,
    )
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_config_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_cost_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cost_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reserved_points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(_TASK_MODE_CHECK, name="mode"),
        CheckConstraint(_TASK_STATUS_CHECK, name="status"),
        CheckConstraint(
            "billing_status IN ('RESERVED', 'CONSUMED', 'RELEASED')",
            name="billing_status",
        ),
        CheckConstraint("requested_count > 0", name="requested_count_positive"),
        CheckConstraint("reserved_points > 0", name="reserved_points_positive"),
        CheckConstraint("retry_count >= 0", name="retry_count_non_negative"),
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_generation_tasks_user_idempotency"
        ),
        Index(
            "ix_generation_tasks_user_id_created_at_id", "user_id", "created_at", "id"
        ),
        Index("ix_generation_tasks_status_created_at_id", "status", "created_at", "id"),
        Index(
            "ix_generation_tasks_parent_task_id",
            "parent_task_id",
            postgresql_where=text("parent_task_id IS NOT NULL"),
        ),
    )


class GenerationInput(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`generation_inputs`：任务输入资源及其角色。"""

    __tablename__ = "generation_inputs"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('SOURCE', 'REFERENCE')", name="role"),
        CheckConstraint("sort_order >= 0", name="sort_order_non_negative"),
        UniqueConstraint(
            "generation_id",
            "role",
            "sort_order",
            name="uq_generation_inputs_generation_role_sort",
        ),
        UniqueConstraint(
            "generation_id", "asset_id", name="uq_generation_inputs_generation_asset"
        ),
        Index("ix_generation_inputs_generation_id_role_sort_order", "generation_id", "role", "sort_order"),
    )


class GenerationOutput(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`generation_outputs`：任务结果资源。"""

    __tablename__ = "generation_outputs"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    output_index: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_output_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    __table_args__ = (
        CheckConstraint("output_index >= 0", name="output_index_non_negative"),
        UniqueConstraint(
            "generation_id",
            "output_index",
            name="uq_generation_outputs_generation_output_index",
        ),
        Index(
            "uq_generation_outputs_generation_provider_output",
            "generation_id",
            "provider_output_id",
            unique=True,
            postgresql_where=text("provider_output_id IS NOT NULL"),
        ),
        # 该索引已同步登记到 02-database-design.md 第 5 节索引清单。
        Index("ix_generation_outputs_generation_id_output_index", "generation_id", "output_index"),
    )
