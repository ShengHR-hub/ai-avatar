"""参考图分析的付费登记模型。

表：`analysis_dispatches`
唯一来源：`docs/architecture/02-database-design.md` 第 3.11 节。

一行 = 一次**已经送出**的视觉模型调用。这张表存在的唯一理由：每日付费上限不能只靠
Redis 计数——Redis 是缓存，重启、被驱逐、被 `FLUSHALL` 之后计数归零，用户当天的
额度也就归零，而钱已经花出去了。把「出过网」写进 PostgreSQL 之后，缓存只负责加速，
费用闸门本身不可被清空。

刻意不存的东西：

- **图片字节与原图路径**：`digest` 是内容摘要，够用来去重，不指向任何可还原的文件；
- **描述文本**：可能含用户私人信息，只允许在 Redis 里按 TTL 存活（`guard.py`）；
- **供应商响应**：`outcome` 只记「成功 / 失败 / 状态不明」，超时且不明时保持
  `UNKNOWN`——这行仍占额度，正是为了让自动重试打不到第二次付费。

`usage_day` 冗余存一份 UTC 日历日（而不是每次对 `created_at` 做函数转换），
好让 `(user_id, usage_day, digest)` 的唯一键与 `(usage_day)` 索引都能被用到；
CHECK 保证它与 `created_at` 同一天，两处真相不会漂移。
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin

__all__ = ["OUTCOMES", "AnalysisDispatch"]

#: `outcome` 的三种取值：送出去了但不知道结果，是一种**独立且合法**的终态
OUTCOMES: tuple[str, ...] = ("SUCCEEDED", "FAILED", "UNKNOWN")


class AnalysisDispatch(CreatedAtMixin, Base):
    """一次已出网的视觉调用（同日同图唯一）。"""

    __tablename__ = "analysis_dispatches"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        nullable=False,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    usage_day: Mapped[date] = mapped_column(Date, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'UNKNOWN'"), default="UNKNOWN"
    )

    __table_args__ = (
        # 同一用户、同一天、同一张图：第二次付费出网在这张表上根本插不进去，
        # 因此并发（哪怕 Redis 单飞锁已失效）也只能有一次真的打出去。
        UniqueConstraint(
            "user_id",
            "usage_day",
            "digest",
            name="uq_analysis_dispatches_user_id_usage_day_digest",
        ),
        CheckConstraint(
            "outcome IN ('SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name="outcome",
        ),
        # 计数按 usage_day 聚合，所以它必须真的等于写入当天（UTC）
        CheckConstraint(
            "usage_day = (created_at AT TIME ZONE 'UTC')::date",
            name="usage_day_matches_created_at",
        ),
        CheckConstraint("length(digest) = 64", name="digest_length"),
        Index("ix_analysis_dispatches_usage_day", "usage_day"),
    )
