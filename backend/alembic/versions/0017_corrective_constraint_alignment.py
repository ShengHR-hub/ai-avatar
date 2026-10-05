"""0017 corrective constraint alignment：修正生成任务状态与点数流水 delta 约束。

**新增的纠正性迁移**（不重排、不改写 0001–0016）：

1. `generation_tasks.ck_generation_tasks_status`
   移除生成任务状态 `CANCELLED`。依据：
   - `02-database-design.md` 第 3.5 节：`status` 为
     `CREATED/QUEUED/PROCESSING/SUCCEEDED/FAILED/NEEDS_REVIEW`，V1.0 不保留 `CANCELLED`；
   - `03-api-contract.md` 第 5 节与 `LEAD-04-points-and-state-machine.md` 第 4 节：
     V1.0 不提供公开取消 API，取消结果归类为 `FAILED + RELEASED` 或
     `NEEDS_REVIEW + RESERVED`。
   订单表 `orders.status` 的 `CANCELLED` **保持不变**（订单域独立）。

2. `point_ledger.ck_point_ledger_entry_type_delta`
   收紧 `ADJUSTMENT` 分支。依据 `02-database-design.md` 第 3.2 节：
   `metadata.adjustment_kind` 必填且只能是 `POSITIVE_CREDIT` 或 `NEGATIVE_CONSUMPTION`；
   前者要求 `available_delta > 0`、后者要求 `available_delta < 0`；两者均不得改变
   `reserved_points`（即 `reserved_delta = 0`）。原分支
   `(available_delta <> 0 OR reserved_delta <> 0)` 过宽，无法阻止：
   缺失/非法 `adjustment_kind`、符号与 kind 不一致、以及误改预留余额。

数据前置处理（本迁移**不改动任何账务金额**）：

- `generation_tasks` 中已存在的 `status = 'CANCELLED'` 行：**仅当** `billing_status = 'RESERVED'`
  （即尚未结算）时才按 LEAD-04 第 4 节归入「未完成人工决定」桶，把 `status` 改为
  `NEEDS_REVIEW`，因此满足 `NEEDS_REVIEW => RESERVED` 不变量（`02-database-design.md` 第 3.2 节）。
  **不生成任何 `point_ledger` 流水**，最终账务决定留给人工结算流程。
  若存在 `status = 'CANCELLED'` 且 `billing_status <> 'RESERVED'` 的行（已结算或未知状态），
  迁移**明确失败**，不静默改写、也不保留未知账务状态。
- `point_ledger` 中不满足新 `ADJUSTMENT` 规则的历史行：**不静默改写**，
  迁移会以明确错误终止，交由运营按审计记录处理。

Revision ID: 0017_constraint_alignment
Revises: 0016_append_only_guards
Create Date: 2026-09-26

（2026-09-27 变更：revision 标识由 `0017_corrective_constraint_alignment` 缩短为
`0017_constraint_alignment`。原因：Alembic 记账表 `alembic_version.version_num` 为
`varchar(32)`，而旧标识实测 36 字符，空库升级到 0017 时在写入版本戳阶段报
`StringDataRightTruncation`。文件名、人类可读说明、迁移顺序、约束语义与前置校验均未改变；
0017 此前未在任何数据库成功应用过，故无需历史数据回填。）
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_constraint_alignment"
down_revision: str | None = "0016_append_only_guards"
branch_labels: str | None = None
depends_on: str | None = None

#: 新状态约束（与 `app/db/models/generation.py` 的 `_TASK_STATUS_CHECK` 一致）
_NEW_TASK_STATUS_CHECK = (
    "status IN ('CREATED', 'QUEUED', 'PROCESSING', 'SUCCEEDED', "
    "'FAILED', 'NEEDS_REVIEW')"
)

#: 旧状态约束（用于 downgrade）
_OLD_TASK_STATUS_CHECK = (
    "status IN ('CREATED', 'QUEUED', 'PROCESSING', 'SUCCEEDED', "
    "'FAILED', 'CANCELLED', 'NEEDS_REVIEW')"
)

#: 新 delta 约束（与 `app/db/models/points.py` 的 `_ENTRY_TYPE_DELTA_CHECK` 一致）
_NEW_DELTA_CHECK = """
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

#: 旧 delta 约束（用于 downgrade）
_OLD_DELTA_CHECK = (
    "(entry_type = 'CREDIT' AND available_delta = amount AND reserved_delta = 0) "
    "OR (entry_type = 'RESERVE' AND available_delta = -amount AND reserved_delta = amount) "
    "OR (entry_type = 'CONSUME' AND available_delta = 0 AND reserved_delta = -amount) "
    "OR (entry_type = 'RELEASE' AND available_delta = amount AND reserved_delta = -amount) "
    "OR (entry_type = 'ADJUSTMENT' AND (available_delta <> 0 OR reserved_delta <> 0))"
)

#: 数据库中**实际存在**的约束名（单前缀形式，由 0009 / 0013 建表时创建）。
#:
#: 修复说明（2026-09-27，PostgreSQL 首次真实补验发现）：
#: 本迁移原先把完整约束名交给 Alembic/SQLAlchemy 的操作接口
#: （`op.drop_constraint(_TASK_STATUS_CONSTRAINT, ...)` 与
#: `op.create_check_constraint(op.f(_TASK_STATUS_CONSTRAINT), ...)`），
#: 于是命名约定 `ck_%(table_name)s_%(constraint_name)s` 又套了一次前缀，
#: 渲染出「表名前缀重复两次」的畸形标识，
#: 在 `ALTER TABLE ... DROP CONSTRAINT` 时报
#: `psycopg.errors.UndefinedObject`，导致空库 `upgrade head` 停在 0017。
#: 0001–0016 用 `op.create_table(..., sa.CheckConstraint(..., name=op.f("ck_...")))`
#: 一次性建表时只渲染一次，落库结果是单前缀名，与本迁移的删除路径渲染不一致。
#: 因此这里**改为直接以数据库实际约束名执行 DDL**，不再让任何
#: `ck_<table>_<constraint>` 形式的完整名字二次进入命名约定。
#: 约束语义、字段、索引、触发器与迁移顺序均不变。
_TASK_STATUS_CONSTRAINT = "ck_generation_tasks_status"
_TASK_STATUS_TABLE = "generation_tasks"
_DELTA_CONSTRAINT = "ck_point_ledger_entry_type_delta"
_DELTA_TABLE = "point_ledger"


def _replace_check_constraint(table: str, constraint: str, condition: str) -> None:
    """按数据库实际约束名重建 CHECK 约束（不经命名约定渲染）。

    DROP 不使用 `IF EXISTS`：约束缺失属于真实异常，必须响亮失败而不是被静默跳过。
    ADD 会由 PostgreSQL 对既有数据重新校验，数据不满足时迁移同样明确失败。
    """

    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{constraint}"')
    op.execute(f'ALTER TABLE {table} ADD CONSTRAINT "{constraint}" CHECK ({condition})')


def upgrade() -> None:
    conn = op.get_bind()

    # --- 1. generation_tasks：移除 CANCELLED -------------------------------- #
    # 前置校验：只允许把「仍处于 RESERVED 未结算」的旧取消任务归入 NEEDS_REVIEW。
    # 若旧行已是 CONSUMED/RELEASED（已人工结算）或其它未知状态，说明账务事实已发生变化，
    # 迁移不得静默改写或保留未知账务状态，必须明确失败交由人工核对。
    settled_cancelled = conn.execute(
        sa.text(
            "SELECT count(*) FROM generation_tasks "
            "WHERE status = 'CANCELLED' AND billing_status <> 'RESERVED'"
        )
    ).scalar_one()
    if settled_cancelled:
        raise RuntimeError(
            f"generation_tasks 存在 {settled_cancelled} 条 status='CANCELLED' 但 "
            "billing_status <> 'RESERVED' 的任务；文档只允许旧取消任务归入 "
            "NEEDS_REVIEW + RESERVED，迁移不静默改写已结算账务，请先人工核对（见本迁移文档串）。"
        )

    legacy_cancelled = conn.execute(
        sa.text("SELECT count(*) FROM generation_tasks WHERE status = 'CANCELLED'")
    ).scalar_one()
    if legacy_cancelled:
        # 仅改 status：保持 billing_status='RESERVED'，不生成流水，不猜测账务结果
        conn.execute(
            sa.text(
                "UPDATE generation_tasks SET status = 'NEEDS_REVIEW' "
                "WHERE status = 'CANCELLED' AND billing_status = 'RESERVED'"
            )
        )

    _replace_check_constraint(
        _TASK_STATUS_TABLE, _TASK_STATUS_CONSTRAINT, _NEW_TASK_STATUS_CHECK
    )

    # --- 2. point_ledger：收紧 ADJUSTMENT 规则 ----------------------------- #
    non_conformant = conn.execute(
        sa.text(
            "SELECT count(*) FROM point_ledger "
            "WHERE entry_type = 'ADJUSTMENT' AND NOT ("
            "  reserved_delta = 0 AND ("
            "    (metadata ->> 'adjustment_kind' = 'POSITIVE_CREDIT' AND available_delta > 0)"
            "    OR (metadata ->> 'adjustment_kind' = 'NEGATIVE_CONSUMPTION' "
            "        AND available_delta < 0)"
            "  )"
            ")"
        )
    ).scalar_one()
    if non_conformant:
        raise RuntimeError(
            f"point_ledger 存在 {non_conformant} 条不符合新 ADJUSTMENT 规则的流水；"
            "迁移不静默改写账务数据，请先按审计记录人工处理（见本迁移文档串）。"
        )

    _replace_check_constraint(
        _DELTA_TABLE, _DELTA_CONSTRAINT, _NEW_DELTA_CHECK
    )


def downgrade() -> None:
    """回到 0016 的约束形态。

    - 状态约束恢复允许 `CANCELLED`（已迁移为 `NEEDS_REVIEW` 的行**不回滚**，
      因为原状态已不可恢复，且 `NEEDS_REVIEW` 在新旧约束下均合法）；
    - delta 约束恢复为宽松的 `ADJUSTMENT` 规则（现有数据仍满足）。
    """

    _replace_check_constraint(
        _DELTA_TABLE, _DELTA_CONSTRAINT, _OLD_DELTA_CHECK
    )

    _replace_check_constraint(
        _TASK_STATUS_TABLE, _TASK_STATUS_CONSTRAINT, _OLD_TASK_STATUS_CHECK
    )
