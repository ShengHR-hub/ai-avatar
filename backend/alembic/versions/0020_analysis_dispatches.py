"""0020_analysis_dispatches：参考图分析的持久付费登记。

**追加的迁移**（不重排、不改写 0001–0019）。依据：`04-ai-provider.md` 第 2.1 节的
ImageAnalyzer 端口「超时不可判定是否已受理」，以及本轮收口要求——
每日付费上限原先只存在 Redis 计数器里，而 Redis 是缓存：重启、驱逐、`FLUSHALL`
之后计数归零，用户当天额度也跟着归零，可钱已经花出去了。费用闸门必须落在
不会被清空的存储上，因此新增这张只追加的登记表。

新增表 `analysis_dispatches`（`02-database-design.md` 第 3.8 节）：

- 一行 = 一次**已经送出**的视觉调用；
- `usage_day date NOT NULL` 是 UTC 日历日，与限额口径一致；冗余存列而不是对
  `created_at` 套函数，唯一键和按日聚合的索引才用得上；
- `digest character varying(64) NOT NULL` 是原图的 sha256 十六进制摘要：只用来去重，
  不存图片字节、不存存储键、不存描述、不存供应商响应；
- `outcome` 三值 `SUCCEEDED/FAILED/UNKNOWN`，默认 `UNKNOWN`：登记发生在出网**之前**，
  所以「调用发出后进程崩溃/超时」这种最贵的情形天然落在 UNKNOWN，
  该行继续占额度，自动重试打不到第二次付费；
- `uq_analysis_dispatches_user_id_usage_day_digest` 是并发下的最终裁决：
  同一用户同一天同一张图只可能插入一次，即使 Redis 单飞锁已失效；
- `ck_..._usage_day_matches_created_at` 钉住冗余列与写入时间同日，避免两处真相漂移。

不回填历史：0020 之前的出网次数只在 Redis 里计过，已经无法可信复原；
表从空开始，当天已用额度因此**重置一次**——这是本次上线的一次性代价，
交接文档与部署说明都已写明，不做伪造回填。

命名纪律：约束与索引一律用显式 DDL 按数据库实际名称创建（与 0018/0019 同一口径）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020_analysis_dispatches"
down_revision: str | None = "0019_asset_lifecycle"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "analysis_dispatches"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.Uuid(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("usage_day", sa.Date(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("provider_name", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column(
            "outcome",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'UNKNOWN'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_analysis_dispatches_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_dispatches")),
        sa.UniqueConstraint(
            "user_id",
            "usage_day",
            "digest",
            name=op.f("uq_analysis_dispatches_user_id_usage_day_digest"),
        ),
        sa.CheckConstraint(
            "outcome IN ('SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name=op.f("ck_analysis_dispatches_outcome"),
        ),
        sa.CheckConstraint(
            "usage_day = (created_at AT TIME ZONE 'UTC')::date",
            name=op.f("ck_analysis_dispatches_usage_day_matches_created_at"),
        ),
        sa.CheckConstraint(
            "length(digest) = 64", name=op.f("ck_analysis_dispatches_digest_length")
        ),
    )
    op.create_index(
        op.f("ix_analysis_dispatches_usage_day"), _TABLE, ["usage_day"]
    )


def downgrade() -> None:
    """删表。

    这张表是 0020 新建的，回滚即丢弃全部付费登记；`02-database-design.md` 第 3.8 节
    把它定义为费用闸门的权威来源，因此回滚等价于把闸门退回「只有 Redis 计数」的形态，
    必须由部署方显式执行并承担后果。
    """

    op.drop_index(op.f("ix_analysis_dispatches_usage_day"), table_name=_TABLE)
    op.drop_table(_TABLE)
