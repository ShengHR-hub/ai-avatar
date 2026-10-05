"""0019_asset_lifecycle：为 `media_assets` 补齐上传用途、生命周期状态与上传幂等键。

**追加的迁移**（不重排、不改写 0001–0018）。依据 `03-api-contract.md` 第 4.2/5 节的冻结
`Asset` 响应含 `purpose` 与 `status` 字段，而 0005 建的 `media_assets` 只有
`asset_type` + `deleted_at` + `expires_at`，无法表达「上传意图已建立但对象尚未上传」
与「已校验可用」这两种状态；`04-ai-provider.md` 第 7 节的上传幂等也需要落库唯一键。

新增列（技术负责人 2026-09-27 裁决）：

- `purpose varchar(16) NOT NULL`，枚举 `SOURCE/REFERENCE/RESULT`；
- `status varchar(16) NOT NULL`，枚举 `PENDING_UPLOAD/READY/DELETED/EXPIRED`；
- `idempotency_key varchar(128) NULL`，上传意图写入；结果图由 ENG-06 生成时可为空。

`asset_type` 及其含义保持不变，两列的映射固定为
`SOURCE -> ORIGINAL`、`REFERENCE -> REFERENCE`、`RESULT -> RESULT`；
本迁移按该映射的**逆映射**回填历史行的 `purpose`，因此不存在无法判定的 `purpose`。
`status` 回填规则：`deleted_at IS NOT NULL` → `DELETED`，否则 → `READY`
（0019 之前不存在上传意图概念，能落库的行都已完成写入）。

一致性 CHECK：

- `(deleted_at IS NOT NULL) = (status = 'DELETED')`（互为充要，禁止状态与时间戳脱钩）；
- `status <> 'EXPIRED' OR expires_at IS NOT NULL`（只约束必要前提；到期判定由生命周期任务
  与 `now()` 比较后写状态，不把墙上时钟写进 CHECK）。

安全处理：回填前若发现无法归类的历史行（`asset_type` 不在三个枚举内、或
`deleted_at IS NOT NULL AND status` 无法推导），迁移**明确失败**，不猜测填充。
已核对：0018 之后本地开发库 `media_assets` 无任何历史行。

命名纪律：约束与索引一律用显式 DDL 按数据库实际名称创建，不把已含前缀的完整名字
再交给命名约定渲染（0017 的缺陷根因）。

Revision ID: 0019_asset_lifecycle
Revises: 0018_adjustment_kind_required
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019_asset_lifecycle"
down_revision: str | None = "0018_adjustment_kind_required"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "media_assets"

_PURPOSE_CONSTRAINT = "ck_media_assets_purpose"
_STATUS_CONSTRAINT = "ck_media_assets_status"
_DELETED_MATCH_CONSTRAINT = "ck_media_assets_deleted_at_matches_status"
_EXPIRED_PRECONDITION_CONSTRAINT = "ck_media_assets_expired_requires_expires_at"
_IDEMPOTENCY_INDEX = "uq_media_assets_user_id_idempotency_key"

_PURPOSE_CHECK = "purpose IN ('SOURCE', 'REFERENCE', 'RESULT')"
_STATUS_CHECK = "status IN ('PENDING_UPLOAD', 'READY', 'DELETED', 'EXPIRED')"
_DELETED_MATCH_CHECK = "(deleted_at IS NOT NULL) = (status = 'DELETED')"
_EXPIRED_PRECONDITION_CHECK = "status <> 'EXPIRED' OR expires_at IS NOT NULL"

#: `asset_type` -> `purpose` 的逆映射（与 Service 的正向映射一一对应）
_PURPOSE_BACKFILL_CASE = """
    CASE asset_type
        WHEN 'ORIGINAL'  THEN 'SOURCE'
        WHEN 'REFERENCE' THEN 'REFERENCE'
        WHEN 'RESULT'    THEN 'RESULT'
    END
"""


def _require_backfillable() -> None:
    """回填前确认每一行都能被确定归类，否则响亮失败。"""

    conn = op.get_bind()

    unknown_asset_type = conn.execute(
        sa.text(
            f"SELECT count(*) FROM {_TABLE} "
            "WHERE asset_type IS NULL OR asset_type NOT IN "
            "('ORIGINAL', 'REFERENCE', 'RESULT')"
        )
    ).scalar_one()
    if unknown_asset_type:
        raise RuntimeError(
            f"{_TABLE} 存在 {unknown_asset_type} 行 asset_type 无法映射到 purpose；"
            "0019 不猜测填充，请先按审计记录人工核对。"
        )


def upgrade() -> None:
    _require_backfillable()

    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN purpose VARCHAR(16)")
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN status VARCHAR(16)")
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN idempotency_key VARCHAR(128)")

    op.execute(
        f"UPDATE {_TABLE} SET purpose = {_PURPOSE_BACKFILL_CASE}"
    )
    op.execute(
        f"UPDATE {_TABLE} SET status = CASE WHEN deleted_at IS NOT NULL "
        "THEN 'DELETED' ELSE 'READY' END"
    )

    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN purpose SET NOT NULL")
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN status SET NOT NULL")

    for name, condition in (
        (_PURPOSE_CONSTRAINT, _PURPOSE_CHECK),
        (_STATUS_CONSTRAINT, _STATUS_CHECK),
        (_DELETED_MATCH_CONSTRAINT, _DELETED_MATCH_CHECK),
        (_EXPIRED_PRECONDITION_CONSTRAINT, _EXPIRED_PRECONDITION_CHECK),
    ):
        op.execute(
            f'ALTER TABLE {_TABLE} ADD CONSTRAINT "{name}" CHECK ({condition})'
        )

    op.execute(
        f'CREATE UNIQUE INDEX "{_IDEMPOTENCY_INDEX}" ON {_TABLE} '
        "(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    """回到 0018 的形态：先删索引与 CHECK，再删列。

    `idempotency_key` 是 0019 新引入的列，历史数据为空，删除不丢业务事实；
    `purpose` 可由 `asset_type` 逆推，因此也不做数据保留。
    """

    op.execute(f'DROP INDEX IF EXISTS public."{_IDEMPOTENCY_INDEX}"')

    for name in (
        _EXPIRED_PRECONDITION_CONSTRAINT,
        _DELETED_MATCH_CONSTRAINT,
        _STATUS_CONSTRAINT,
        _PURPOSE_CONSTRAINT,
    ):
        op.execute(f'ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS "{name}"')

    for column in ("idempotency_key", "status", "purpose"):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
