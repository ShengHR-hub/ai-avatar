"""0021_share_assets_and_feedback_category：多图分享关联表 + 反馈分类扩宽。

**追加的迁移**（不重排、不改写 0001–0020）。依据：

1. `03-api-contract.md` 第 5 节冻结的 `Share` 含 `output_asset_ids: uuid[]`，
   而 0014 建的 `shares` 只有单个 `asset_id`，无法表达「一次分享两张结果图」；
2. `shared/api-contract/openapi.json` 的 `FeedbackCreateRequest.category` 上限是
   **64 字符**，而 0014 建的 `feedback.category` 是 `varchar(32)`。

技术负责人 2026-10-02 裁决（本迁移据此落地）：

- 保留 `shares.asset_id`，存选中输出中 `output_index` **最小**的主资产；
  完整集合落新增的 `share_assets`，历史单图分享由本迁移**回填**一行关联；
- 只扩宽 `feedback.category` 到 64，不改变请求字段与长度契约；
- **降级守卫**：无法由旧 `asset_id` 表达的关联集合（即 `share_assets` 里存在
  与主资产不同的资产行）或任何 >32 字符的分类值，一律**拒绝降级**并给出恢复条件；
  可表达的历史单图必须支持无损往返。禁止自动删关联、截断分类或改写历史来通过降级。

新增表 `share_assets`（`02-database-design.md` 第 3.9 节）：

- 复合主键 `(share_id, asset_id)`：同一次分享里同一张图只能关联一次；
- `sort_order`：呈现顺序，取自 `generation_outputs.output_index`；
- `(share_id, sort_order)` 唯一：顺序不重复；
- 双 FK 均 `ON DELETE RESTRICT`：分享与媒体都只软删除，历史不可级联丢失；
- `(asset_id, share_id)` 索引：生命周期要按资产反查「还有没有有效分享」。

命名纪律：约束与索引一律用显式 DDL 按数据库实际名称创建（与 0018–0020 同一口径）。

Revision ID: 0021_share_asset_links
Revises: 0020_analysis_dispatches
Create Date: 2026-10-02

命名纪律：`alembic_version.version_num` 是 `varchar(32)`，因此 revision 标识必须
≤32 字符（0017 的缺陷根因）。本迁移的标识取 `0021_share_asset_links`（21 字符），
文件名保留完整人类可读说明。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021_share_asset_links"
down_revision: str | None = "0020_analysis_dispatches"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "share_assets"
_SHARES = "shares"
_FEEDBACK = "feedback"

_PK = "pk_share_assets"
_UQ_SORT = "uq_share_assets_share_id_sort_order"
_FK_SHARE = "fk_share_assets_share_id_shares"
_FK_ASSET = "fk_share_assets_asset_id_media_assets"
_IX_ASSET = "ix_share_assets_asset_id_share_id"

#: 降级守卫的错误说明（必须说明恢复条件，不允许静默丢数据）
_DOWNGRADE_MULTI_IMAGE = (
    "share_assets 中存在 {rows} 行非主资产关联，降级会丢失「一条链接分享多张结果图」"
    " 的关联事实，且旧 asset_id 无法表达它。"
    "恢复条件（必须让 share_assets 表本身满足条件，守卫每次都会重新读表）："
    "把每一条分享收敛成「只关联它的主资产」，即删除 (share_id, asset_id) 且"
    " asset_id <> shares.asset_id 的那些行（单图分享本来就不含这种行）。"
    "**撤销分享、让它到期、或者把关联清单另存导出，都不会让守卫通过**："
    "分享状态变化只改 shares.status，不会删掉 share_assets 里的行，"
    "守卫读的是当前表内容而不是历史记录。"
    "不要为了通过降级而自动删除这些关联。"
)
_DOWNGRADE_LONG_CATEGORY = (
    "feedback.category 中有 {rows} 行超过 32 字符，降级会把 varchar(64) 收回 varchar(32)"
    " 并截断这些分类值。"
    "恢复条件（同样必须让表本身满足条件）：把每一行的 category 改写为 ≤32 字符"
    "（或置空）之后才能降级。把长分类导出到审计/工单只是留证，"
    "**导出本身不会让守卫通过**，因为守卫仍然会读到那些超长值。"
    "本迁移不会自动截断历史，也不接受「先截断再降级」这种做法。"
)


def _guard_downgrade() -> None:
    """降级前确认不丢事实，否则响亮失败。"""

    conn = op.get_bind()

    if conn.execute(sa.text(f"SELECT to_regclass('public.{_TABLE}')")).scalar_one():
        multi = conn.execute(
            sa.text(
                f"""
                SELECT count(*) FROM {_TABLE} sa
                  JOIN {_SHARES} s ON s.id = sa.share_id
                 WHERE sa.asset_id <> s.asset_id
                """
            )
        ).scalar_one()
        if multi:
            raise RuntimeError(_DOWNGRADE_MULTI_IMAGE.format(rows=multi))

    long_categories = conn.execute(
        sa.text(f"SELECT count(*) FROM {_FEEDBACK} WHERE length(category) > 32")
    ).scalar_one()
    if long_categories:
        raise RuntimeError(_DOWNGRADE_LONG_CATEGORY.format(rows=long_categories))


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("share_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("asset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["share_id"], [f"{_SHARES}.id"], name=_FK_SHARE, ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["media_assets.id"], name=_FK_ASSET, ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("share_id", "asset_id", name=_PK),
        sa.UniqueConstraint("share_id", "sort_order", name=_UQ_SORT),
        # 原始名交给命名约定渲染（`ck_share_assets_sort_order`），与模型口径一致
        sa.CheckConstraint("sort_order >= 0", name="sort_order"),
    )
    op.create_index(_IX_ASSET, _TABLE, ["asset_id", "share_id"])

    # 历史单图分享回填：0014 之后 `asset_id` 就是唯一被分享的资产，可无损表达。
    op.execute(
        f"""
        INSERT INTO {_TABLE} (share_id, asset_id, sort_order)
        SELECT id, asset_id, 0 FROM {_SHARES}
        ON CONFLICT (share_id, asset_id) DO NOTHING
        """
    )

    op.execute(f"ALTER TABLE {_FEEDBACK} ALTER COLUMN category TYPE VARCHAR(64)")


def downgrade() -> None:
    """回到 0020 的形态：先守卫，再删表、收回分类宽度。

    `shares.asset_id` 在整个过程中未被修改，因此单图分享的往返是无损的；
    多图关联与超长分类会丢事实，守卫会拒绝并要求显式处理（见模块说明）。
    """

    _guard_downgrade()

    op.drop_index(_IX_ASSET, table_name=_TABLE)
    op.drop_table(_TABLE)

    op.execute(f"ALTER TABLE {_FEEDBACK} ALTER COLUMN category TYPE VARCHAR(32)")
