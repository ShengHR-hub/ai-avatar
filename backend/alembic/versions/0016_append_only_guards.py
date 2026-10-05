"""0016_append_only_guards：point_ledger 与 audit_logs 的追加保护。

对应 02-database-design.md 第 3.2 节、第 3.4 节、第 7 节顺序 16 与第 8 节实现规则 5。

两层保护：

1. 权限层：撤销当前业务数据库角色对这两张表的 UPDATE / DELETE 权限；
2. 触发器层（第二道保护）：BEFORE UPDATE OR DELETE 直接拒绝。

`downgrade` 仅用于本地测试，会恢复权限并移除触发器。
"""

from __future__ import annotations

from alembic import op

revision: str = "0016_append_only_guards"
down_revision: str | None = "0015_audit_logs"
branch_labels: str | None = None
depends_on: str | None = None

_GUARDED_TABLES = ("point_ledger", "audit_logs")

# 触发器函数：不拼接 SQL，不使用格式化占位符，避免驱动层参数解析歧义。
_CREATE_FUNCTION = (
    "CREATE OR REPLACE FUNCTION v1_reject_append_only_mutation()\n"
    "RETURNS trigger\n"
    "LANGUAGE plpgsql\n"
    "AS $$\n"
    "BEGIN\n"
    "    RAISE EXCEPTION USING\n"
    "        MESSAGE = 'append_only_violation: ' || TG_OP || ' on ' || TG_TABLE_NAME,\n"
    "        ERRCODE = 'restrict_violation';\n"
    "END;\n"
    "$$"
)


def upgrade() -> None:
    op.execute(_CREATE_FUNCTION)

    for table in _GUARDED_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION v1_reject_append_only_mutation()"
        )
        # 权限层：业务角色不得具备这两张表的 UPDATE/DELETE
        op.execute(f"REVOKE UPDATE, DELETE ON TABLE {table} FROM CURRENT_USER")


def downgrade() -> None:
    for table in reversed(_GUARDED_TABLES):
        op.execute(f"GRANT UPDATE, DELETE ON TABLE {table} TO CURRENT_USER")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")

    op.execute("DROP FUNCTION IF EXISTS v1_reject_append_only_mutation()")
