"""0001_v1_extensions：启用 pgcrypto、citext。

对应 02-database-design.md 第 7 节顺序 1。
本 revision 只建立扩展基础，不创建业务表。

Revision ID: 0001_v1_extensions
Revises:
Create Date: 2026-09-25
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_v1_extensions"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # gen_random_uuid() 是主键默认值来源；citext 用于 user_emails.email 大小写不敏感唯一。
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS citext")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
