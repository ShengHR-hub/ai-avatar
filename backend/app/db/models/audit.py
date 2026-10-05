"""审计日志模型。

表：`audit_logs`
唯一来源：`docs/architecture/02-database-design.md` 第 3.4 节。

只允许追加；`entity_id` 是多态字段，不建立外键。
禁止记录 API Key、Cookie、令牌、原图、完整 Prompt、签名 URL 或 Provider 原始响应。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, Index, String, Uuid, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["AuditLog"]


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """`audit_logs`：不可变审计记录。"""

    __tablename__ = "audit_logs"

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(96), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('USER', 'ADMIN', 'SYSTEM', 'WORKER')", name="actor_type"
        ),
        Index("ix_audit_logs_entity_type_entity_id_created_at", "entity_type", "entity_id", "created_at"),
        Index(
            "ix_audit_logs_actor_id_created_at",
            "actor_id",
            "created_at",
            postgresql_where=text("actor_id IS NOT NULL"),
        ),
        Index(
            "ix_audit_logs_request_id",
            "request_id",
            postgresql_where=text("request_id IS NOT NULL"),
        ),
    )
