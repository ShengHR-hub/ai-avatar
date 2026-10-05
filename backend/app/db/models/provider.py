"""Provider 调用与成本记录模型。

表：`provider_attempts`
唯一来源：`docs/architecture/02-database-design.md` 第 3.3 节。

- Token 字段对图片模型可为 `NULL`；
- `estimated_cost` 只用于内部成本统计，不替代用户点数规则；
- 唯一部分索引 `(provider_name, request_id) WHERE request_id IS NOT NULL`
  防止同一外部请求被重复记账。
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

from app.db.base import Base, UUIDPrimaryKeyMixin

__all__ = ["ProviderAttempt"]


class ProviderAttempt(UUIDPrimaryKeyMixin, Base):
    """`provider_attempts`：每次 Provider 调用的状态、用量与估算成本。"""

    __tablename__ = "provider_attempts"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    image_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    request_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    response_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('STARTED', 'SUBMITTED', 'SUCCEEDED', 'FAILED', "
            "'TIMEOUT', 'UNKNOWN')",
            name="status",
        ),
        CheckConstraint("attempt_no > 0", name="attempt_no_positive"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_non_negative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_non_negative"),
        CheckConstraint("image_count >= 0", name="image_count_non_negative"),
        CheckConstraint("estimated_cost >= 0", name="estimated_cost_non_negative"),
        CheckConstraint(
            "estimated_cost IS NULL OR currency IS NOT NULL",
            name="currency_required_with_cost",
        ),
        UniqueConstraint(
            "generation_id", "attempt_no", name="uq_provider_attempts_generation_attempt"
        ),
        Index("ix_provider_attempts_generation_id_attempt_no", "generation_id", "attempt_no"),
        Index(
            "uq_provider_attempts_provider_request",
            "provider_name",
            "request_id",
            unique=True,
            postgresql_where=text("request_id IS NOT NULL"),
        ),
    )
