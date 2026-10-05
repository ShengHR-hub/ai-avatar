"""0006_media_reference_fks：添加 users.avatar_asset_id 外键。

对应 02-database-design.md 第 3.1 节、第 4 节与第 7 节顺序 6。
该外键指向 media_assets，与 media_assets.user_id 构成循环，因此在媒体表建立后单独添加。

Revision ID: 0006_media_reference_fks
Revises: 0005_media_assets
Create Date: 2026-09-25
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_media_reference_fks"
down_revision: str | None = "0005_media_assets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_users_avatar_asset_id_media_assets",
        "users",
        "media_assets",
        ["avatar_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_users_avatar_asset_id_media_assets"), "users", type_="foreignkey"
    )
