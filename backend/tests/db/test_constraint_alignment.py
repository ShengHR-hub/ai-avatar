"""ENG-04 第 1 步：纠正性约束对齐测试（**不依赖数据库**）。

本模块只读 SQLAlchemy 元数据与 Alembic 迁移脚本，因此可在无 PostgreSQL 的环境运行；
真实数据库的约束落地由 `test_migrations.py` / `test_constraints.py` 在集成阶段验证。

覆盖：

- 生成任务状态约束已移除 `CANCELLED`（模型层）；
- 订单状态约束**保留** `CANCELLED`（订单域不受影响）；
- `point_ledger` 的 `ADJUSTMENT` 规则已收紧（kind 必填、符号一致、不改预留）；
- 钱包五个非负约束完整；
- 0017 迁移与模型**无漂移**（任务状态表达式同一来源）；
- 0018 迁移与模型**无漂移**（delta 表达式同一来源），且已应用的 0017 未被改写；
- 0001–0016 历史迁移未被改写（仍含旧表达式）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint

from app.db.models.generation import GenerationTask
from app.db.models.orders import Order
from app.db.models.points import PointLedger, PointWallet

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"
MIGRATION_0017 = MIGRATIONS_DIR / "0017_corrective_constraint_alignment.py"
MIGRATION_0018 = MIGRATIONS_DIR / "0018_adjustment_kind_required.py"


def _normalize(sql: str) -> str:
    """去掉全部空白，便于比较同一约束的不同书写格式。"""

    return re.sub(r"\s+", "", sql)


def _check_sql(model_table, constraint_name: str) -> str:
    """按**命名约定展开后**的名称取 CHECK 约束 SQL。

    项目配置了 naming convention，`CheckConstraint(name="status")` 在元数据中即为
    `ck_<table>_status`。
    """

    expected = f"ck_{model_table.name}_{constraint_name}"
    for constraint in model_table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == expected:
            return _normalize(str(constraint.sqltext))
    raise AssertionError(f"{model_table.name} 缺少 CHECK 约束 {expected}")


# --------------------------------------------------------------------------- #
# 模型层约束
# --------------------------------------------------------------------------- #
def test_generation_task_status_excludes_cancelled() -> None:
    sql = _check_sql(GenerationTask.__table__, "status")

    assert "CANCELLED" not in sql
    for status in ("CREATED", "QUEUED", "PROCESSING", "SUCCEEDED", "FAILED", "NEEDS_REVIEW"):
        assert status in sql, status


def test_order_status_keeps_cancelled() -> None:
    """订单域的 `CANCELLED` 不受本次纠正影响。"""

    sql = _check_sql(Order.__table__, "status")

    assert "CANCELLED" in sql
    for status in ("PENDING", "PAID", "REFUNDED"):
        assert status in sql, status


def test_point_ledger_adjustment_rule_is_tightened() -> None:
    sql = _check_sql(PointLedger.__table__, "entry_type_delta")

    # 四种标准变化保持不变
    assert "entry_type='CREDIT'" in sql
    assert "entry_type='RESERVE'" in sql
    assert "entry_type='CONSUME'" in sql
    assert "entry_type='RELEASE'" in sql
    # ADJUSTMENT：kind 必填且仅两个取值、符号一致、不改预留余额
    assert "metadata->>'adjustment_kind'='POSITIVE_CREDIT'" in sql
    assert "metadata->>'adjustment_kind'='NEGATIVE_CONSUMPTION'" in sql
    assert "reserved_delta=0" in sql
    assert "available_delta>0" in sql
    assert "available_delta<0" in sql
    # 0018：必须显式 IS NOT NULL，否则缺失 kind 时比较返回 NULL，
    # 而 PostgreSQL 的 CHECK 把 NULL 视为通过（“必填”在库层失效）
    assert "metadata->>'adjustment_kind'ISNOTNULL" in sql
    # 旧的宽松分支必须消失
    assert "available_delta<>0ORreserved_delta<>0" not in sql


def test_point_ledger_standard_delta_relations_unchanged() -> None:
    sql = _check_sql(PointLedger.__table__, "entry_type_delta")

    assert "available_delta=amount" in sql
    assert "available_delta=-amount" in sql
    assert "reserved_delta=-amount" in sql


@pytest.mark.parametrize(
    "column", ["available_points", "reserved_points", "lifetime_credited", "lifetime_consumed"]
)
def test_point_wallet_non_negative_constraints(column: str) -> None:
    sql = _check_sql(PointWallet.__table__, f"{column}_non_negative")

    assert f"{column}>=0" in sql


def test_point_wallet_version_non_negative() -> None:
    assert "version>=0" in _check_sql(PointWallet.__table__, "version_non_negative")


def test_point_ledger_unique_constraints_declared() -> None:
    """`idempotency_key` 全局唯一，且两个部分唯一索引存在（与 02 §3.2 一致）。"""

    from sqlalchemy import UniqueConstraint

    unique_column_sets = [
        {column.name for column in constraint.columns}
        for constraint in PointLedger.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    index_names = {index.name for index in PointLedger.__table__.indexes}

    assert {"idempotency_key"} in unique_column_sets
    assert {
        "uq_point_ledger_generation_reserve",
        "uq_point_ledger_generation_final",
    } <= index_names


# --------------------------------------------------------------------------- #
# 迁移与模型一致性（防止漂移）
# --------------------------------------------------------------------------- #
def test_migration_0017_identity_is_frozen() -> None:
    """0017 已应用，标识与链位置必须保持冻结值（本用例原名误称 “is head”，0018 追加后已不成立）。"""

    assert MIGRATION_0017.exists()

    text = MIGRATION_0017.read_text(encoding="utf-8")
    assert 'revision: str = "0017_constraint_alignment"' in text
    assert 'down_revision: str | None = "0016_append_only_guards"' in text


def test_migration_0018_identity_and_head() -> None:
    """0018 追加在 0017 之后，成为新的 head；标识必须 ≤32 字符以适配记账表列宽。"""

    assert MIGRATION_0018.exists()

    text = MIGRATION_0018.read_text(encoding="utf-8")
    assert 'revision: str = "0018_adjustment_kind_required"' in text
    assert 'down_revision: str | None = "0017_constraint_alignment"' in text
    assert len("0018_adjustment_kind_required") <= 32


def test_applied_migration_0017_was_not_rewritten() -> None:
    """已成功应用的 0017 不得被 0018 改写：其 delta 表达式仍缺 `IS NOT NULL`。

    这条断言把「不修改已应用迁移」的治理约束变成可执行证据。
    """

    text = MIGRATION_0017.read_text(encoding="utf-8")

    assert "IS NOT NULL" not in _extract(text, "_NEW_DELTA_CHECK")
    assert '_TASK_STATUS_CONSTRAINT = "ck_generation_tasks_status"' in text
    assert 'revision: str = "0017_constraint_alignment"' in text


def test_migration_0017_matches_model_task_status() -> None:
    """0017 的任务状态约束表达式必须与模型逐字一致（去空白与 Python 引号后）。"""

    text = MIGRATION_0017.read_text(encoding="utf-8")

    assert _normalize(_extract(text, "_NEW_TASK_STATUS_CHECK").replace('"', "")) == _check_sql(
        GenerationTask.__table__, "status"
    )


def test_migration_0018_matches_model_delta_expression() -> None:
    """0018 的 delta 表达式必须是模型的唯一权威来源（防漂移）。"""

    text = MIGRATION_0018.read_text(encoding="utf-8")

    assert _normalize(_extract(text, "_NEW_DELTA_CHECK").replace('"', "")) == _check_sql(
        PointLedger.__table__, "entry_type_delta"
    )


def test_migration_0017_targets_only_two_named_constraints() -> None:
    """0017 只重建这两个 CHECK，且必须以数据库实际的单前缀名操作。

    本用例以前断言 `op.drop_constraint(` / `op.create_check_constraint(` 的**调用次数**，
    把那套会产生双前缀渲染的 API 形态当成了契约。2026-09-27 首次真实 PostgreSQL 补验证明
    该形态会把 `ck_generation_tasks_status` 再套一次命名约定、渲染成
    `ck_generation_tasks_ck_generation_tasks_status` 并在 DROP 时 UndefinedObject。
    因此这里改为按**契约面**断言：目标表与约束名、语义来源、不触碰无关对象、
    以及不再出现任何二次套约定的调用路径。断言强度不降反升（用 AST 解析真实调用）。
    """

    import ast

    text = MIGRATION_0017.read_text(encoding="utf-8")
    tree = ast.parse(text)

    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name:
                called.append(name)

    # 恰好 4 次约束重建：2 个 upgrade + 2 个 downgrade
    assert called.count("_replace_check_constraint") == 4
    # 不得再把完整 ck_ 名字交给命名约定二次渲染
    for forbidden_call in ("drop_constraint", "create_check_constraint", "f"):
        assert forbidden_call not in called, forbidden_call

    executable = text.split('"""', 2)[2]  # 去掉模块文档串
    assert '_TASK_STATUS_CONSTRAINT = "ck_generation_tasks_status"' in executable
    assert '_DELTA_CONSTRAINT = "ck_point_ledger_entry_type_delta"' in executable
    # 双前缀名字（曾经的缺陷产物）不得以任何形式出现
    assert "ck_generation_tasks_ck_" not in text
    assert "ck_point_ledger_ck_" not in text
    # 只针对这两张表
    assert "_TASK_STATUS_TABLE = \"generation_tasks\"" in executable
    assert "_DELTA_TABLE = \"point_ledger\"" in executable
    # 不触碰订单域
    assert "orders" not in executable
    # 不写入任何账务金额
    for forbidden in ("available_points =", "reserved_points =", "lifetime_"):
        assert forbidden not in executable, forbidden


def test_migration_0018_rebuilds_only_the_delta_check_with_explicit_ddl() -> None:
    """0018 只重建 point_ledger 的 delta CHECK，且沿用不经命名约定的显式 DDL。"""

    import ast

    assert MIGRATION_0018.exists()

    text = MIGRATION_0018.read_text(encoding="utf-8")
    tree = ast.parse(text)

    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name:
                called.append(name)

    # upgrade + downgrade 各一次重建
    assert called.count("_replace_check_constraint") == 2
    for forbidden_call in ("drop_constraint", "create_check_constraint", "f"):
        assert forbidden_call not in called, forbidden_call

    executable = text.split('"""', 2)[2]
    assert '_DELTA_CONSTRAINT = "ck_point_ledger_entry_type_delta"' in executable
    assert '_DELTA_TABLE = "point_ledger"' in executable
    # 不得出现双前缀畸形名
    assert "ck_point_ledger_ck_" not in text
    # 只针对 point_ledger：不触碰 generation_tasks / orders / 其它表
    for untouched in ("generation_tasks", "orders", "audit_logs", "media_assets"):
        assert untouched not in executable, untouched
    # 不写入任何账务金额，也不改四种标准变化
    for forbidden in ("available_points =", "reserved_points =", "lifetime_", "INSERT INTO"):
        assert forbidden not in executable, forbidden
    for standard in ("entry_type = 'CREDIT'", "entry_type = 'RESERVE'",
                     "entry_type = 'CONSUME'", "entry_type = 'RELEASE'"):
        assert standard in _extract(text, "_NEW_DELTA_CHECK"), standard


def test_migration_0017_checks_billing_status_before_migrating() -> None:
    """旧 `CANCELLED` 任务只有在 `billing_status = 'RESERVED'` 时才允许归入 `NEEDS_REVIEW`。

    已结算（`CONSUMED`/`RELEASED`）或未知账务状态的旧行必须让迁移**明确失败**，
    不得静默改写或保留未知账务状态。
    """

    text = MIGRATION_0017.read_text(encoding="utf-8")

    assert "status = 'CANCELLED' AND billing_status <> 'RESERVED'" in text
    assert "raise RuntimeError(" in text
    # 只有 RESERVED 的行才被改写
    assert "WHERE status = 'CANCELLED' AND billing_status = 'RESERVED'" in text
    # 前置校验必须在 UPDATE 之前
    assert text.index("billing_status <> 'RESERVED'") < text.index(
        "UPDATE generation_tasks SET status = 'NEEDS_REVIEW'"
    )


def test_historical_migrations_are_not_rewritten() -> None:
    """0001–0016 必须仍保留旧表达式，证明历史未被改写。"""

    task_status = (MIGRATIONS_DIR / "0009_generation_tasks.py").read_text(encoding="utf-8")
    ledger = (MIGRATIONS_DIR / "0013_point_ledger.py").read_text(encoding="utf-8")

    assert "'FAILED', 'CANCELLED', 'NEEDS_REVIEW'" in task_status
    assert "available_delta <> 0 OR reserved_delta <> 0" in ledger


def _extract(text: str, variable: str) -> str:
    """取出迁移脚本中 `variable = ...` 的常量内容（三引号字符串或括号表达式）。"""

    marker = f"{variable} = "
    start = text.index(marker) + len(marker)
    rest = text[start:].lstrip()

    if rest.startswith('"""'):
        return rest.split('"""')[1]

    opening = rest.index("(")
    depth = 0
    for index, character in enumerate(rest[opening:], start=opening):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return rest[opening + 1 : index]
    raise AssertionError(f"无法解析 {variable}")
