"""0018_adjustment_kind_required：强制 `point_ledger` 的 ADJUSTMENT 必须带非空 adjustment_kind。

**追加的纠正性迁移**（不重排、不改写 0001–0017，0017 已成功应用故原文件保持不动）。

根因（2026-09-27 首次真实 PostgreSQL 补验暴露）：
0017 的 delta 约束里 `metadata ->> 'adjustment_kind' = 'POSITIVE_CREDIT'` 这类比较，
在键缺失或值为 JSON `null` 时返回 **NULL** 而不是 FALSE；PostgreSQL 的 CHECK 只在结果
确定为 FALSE 时拒绝写入，NULL 一律视为通过。于是「`adjustment_kind` 必填」在数据库层
实际不成立：不带该键的 ADJUSTMENT 流水可以被直接写入。
由 `tests/db/test_points_invariants.py::test_adjustment_requires_kind_and_sign_consistency`
的「缺少 adjustment_kind」用例以 `DID NOT RAISE` 报出。

修复：在 ADJUSTMENT 分支显式加 `metadata ->> 'adjustment_kind' IS NOT NULL`，
使缺失键与 JSON null 都确定为 FALSE。因此本迁移后必须拒绝：
缺少 adjustment_kind、adjustment_kind 为 JSON null、kind 取非法值、
kind 与 available_delta 符号不一致、reserved_delta 非 0、以及 delta 全零。

其余四种标准变化（CREDIT/RESERVE/CONSUME/RELEASE）、其它表、索引、触发器、
迁移顺序与业务规则均不变；`app/db/models/points.py` 的 `_ENTRY_TYPE_DELTA_CHECK`
同步为与本迁移逐字一致的表达式。

约束重建沿用 0017 的做法：直接以数据库实际约束名执行 DDL，不把已经含前缀的
`ck_<table>_<constraint>` 完整名字再交给命名约定渲染（那正是 0017 的缺陷根因）。

Revision ID: 0018_adjustment_kind_required
Revises: 0017_constraint_alignment
Create Date: 2026-09-27
"""

from __future__ import annotations

from alembic import op

revision: str = "0018_adjustment_kind_required"
down_revision: str | None = "0017_constraint_alignment"
branch_labels: str | None = None
depends_on: str | None = None

#: 数据库中实际存在的约束名（单前缀形式，由 0013 建表时创建，0017 原地重建沿用）。
_DELTA_CONSTRAINT = "ck_point_ledger_entry_type_delta"
_DELTA_TABLE = "point_ledger"

#: 0018 起生效的 delta 约束（与 `app/db/models/points.py` 的 `_ENTRY_TYPE_DELTA_CHECK` 逐字一致）。
_NEW_DELTA_CHECK = """
(entry_type = 'CREDIT' AND available_delta = amount AND reserved_delta = 0)
OR (entry_type = 'RESERVE' AND available_delta = -amount AND reserved_delta = amount)
OR (entry_type = 'CONSUME' AND available_delta = 0 AND reserved_delta = -amount)
OR (entry_type = 'RELEASE' AND available_delta = amount AND reserved_delta = -amount)
OR (
    entry_type = 'ADJUSTMENT'
    AND reserved_delta = 0
    AND metadata ->> 'adjustment_kind' IS NOT NULL
    AND (
        (metadata ->> 'adjustment_kind' = 'POSITIVE_CREDIT' AND available_delta > 0)
        OR (metadata ->> 'adjustment_kind' = 'NEGATIVE_CONSUMPTION' AND available_delta < 0)
    )
)
"""

#: 0017 版本的 delta 约束（仅用于 downgrade；其 ADJUSTMENT 分支对缺失 kind 返回 NULL）。
_0017_DELTA_CHECK = """
(entry_type = 'CREDIT' AND available_delta = amount AND reserved_delta = 0)
OR (entry_type = 'RESERVE' AND available_delta = -amount AND reserved_delta = amount)
OR (entry_type = 'CONSUME' AND available_delta = 0 AND reserved_delta = -amount)
OR (entry_type = 'RELEASE' AND available_delta = amount AND reserved_delta = -amount)
OR (
    entry_type = 'ADJUSTMENT'
    AND reserved_delta = 0
    AND (
        (metadata ->> 'adjustment_kind' = 'POSITIVE_CREDIT' AND available_delta > 0)
        OR (metadata ->> 'adjustment_kind' = 'NEGATIVE_CONSUMPTION' AND available_delta < 0)
    )
)
"""


def _replace_check_constraint(table: str, constraint: str, condition: str) -> None:
    """按数据库实际约束名重建 CHECK 约束（不经命名约定渲染）。

    DROP 不使用 `IF EXISTS`：约束缺失属于真实异常，必须响亮失败而不是被静默跳过。
    ADD 会由 PostgreSQL 重新校验既有数据，因此若历史流水连新规则都不满足，
    迁移会明确失败而不是静默放行或改写账务数据。
    """

    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{constraint}"')
    op.execute(f'ALTER TABLE {table} ADD CONSTRAINT "{constraint}" CHECK ({condition})')


def upgrade() -> None:
    _replace_check_constraint(_DELTA_TABLE, _DELTA_CONSTRAINT, _NEW_DELTA_CHECK)


def downgrade() -> None:
    """回到 0017 的约束形态。

    不回填、不改写任何历史流水：0018 只是收紧校验，放宽回 0017 不会使既有数据失配。
    """

    _replace_check_constraint(_DELTA_TABLE, _DELTA_CONSTRAINT, _0017_DELTA_CHECK)
