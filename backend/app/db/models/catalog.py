"""风格与 Prompt 模板模型。

表：`styles`、`prompt_templates`
唯一来源：`docs/architecture/02-database-design.md` 第 3.4 节。

发布版本唯一性由部分唯一索引 `(slug, mode) WHERE status='PUBLISHED'` 保证：
每个模式最多一个当前发布版本。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["PromptTemplate", "Style"]

_PUBLICATION_STATUS_CHECK = "status IN ('DRAFT', 'PUBLISHED', 'ARCHIVED')"


class Style(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """`styles`：可发布风格。"""

    __tablename__ = "styles"

    slug: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''"), default=""
    )
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )

    __table_args__ = (
        CheckConstraint(_PUBLICATION_STATUS_CHECK, name="status"),
        Index("ix_styles_status_sort_order", "status", "sort_order"),
    )


class PromptTemplate(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """`prompt_templates`：带版本号的 Prompt 模板。"""

    __tablename__ = "prompt_templates"

    style_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("styles.id", ondelete="RESTRICT"),
        nullable=True,
    )
    slug: Mapped[str] = mapped_column(String(96), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_PUBLICATION_STATUS_CHECK, name="status"),
        CheckConstraint("version > 0", name="version_positive"),
        UniqueConstraint(
            "slug", "mode", "version", name="uq_prompt_templates_slug_mode_version"
        ),
        Index(
            "uq_prompt_templates_slug_mode_published",
            "slug",
            "mode",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        Index("ix_prompt_templates_style_id_status", "style_id", "status"),
    )
