"""迁移 0021 的真实 PostgreSQL 验证（ENG-09）。

在**新建隔离数据库**里执行，用完即删；不触碰共享 `ai_avatar`、不触碰
`ai-avatar-v1-postgres` 数据卷里的任何既有库。

覆盖：

1. 空库 `upgrade head` 落地 `share_assets`：复合主键、双 FK（RESTRICT）、
   `sort_order >= 0`、`(share_id, sort_order)` 唯一、`(asset_id, share_id)` 索引；
2. `feedback.category` 扩宽到 `varchar(64)`；
3. **历史单图分享回填**：`upgrade` 时为每条既有分享插入一行关联
   （`sort_order = 0`）；
4. **无损往返**：只含「单图、可由 `asset_id` 表达」的历史数据时，
   `downgrade` → `upgrade` 往返后数据与结构都回到同一状态；
5. **降级守卫**：`share_assets` 存在非主资产关联时拒绝降级；分类超过 32 字符时
   同样拒绝降级。守卫必须说明恢复条件，**不允许**自动删关联或截断分类。

全部使用合成数据（自造 user / cost config / task / asset），不使用真实用户图片或凭据。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

BACKEND_DIR = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

#: 0021 之前的 head
PREVIOUS_REVISION = "0020_analysis_dispatches"
CURRENT_REVISION = "0021_share_asset_links"


# --------------------------------------------------------------------------- #
# 迁移助手（不依赖夹具命名；异常原样上抛以便断言降级守卫）
# --------------------------------------------------------------------------- #
def _run_alembic(database_url: str, action: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        if action == "upgrade":
            command.upgrade(config, revision)
        else:
            command.downgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def upgrade_head_at(database_url: str, revision: str) -> None:
    """升级到指定 revision。"""

    _run_alembic(database_url, "upgrade", revision)


def downgrade_to(database_url: str, revision: str) -> None:
    """降级到指定 revision；守卫拒绝时异常原样上抛。"""

    _run_alembic(database_url, "downgrade", revision)


@pytest.fixture
def isolated_database(db_admin) -> Iterator[str]:
    """一次性隔离库；用例结束后强制删除。"""

    name = f"eng09_mig_{uuid.uuid4().hex[:10]}"
    db_admin.create(name)
    try:
        yield db_admin.url_for(name)
    finally:
        db_admin.drop(name)


def _engine(url: str) -> Engine:
    return create_engine(url, poolclass=NullPool)


# --------------------------------------------------------------------------- #
# 合成数据
# --------------------------------------------------------------------------- #
def _seed_identity(conn) -> tuple[str, str]:
    """插入一行合成 user 与一行成本配置，返回 `(user_id, cost_config_id)`。

    `generation_tasks.cost_config_id` 是 NOT NULL 外键，成本配置必须真实存在。
    """

    user_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO users (id, display_name, status, locale, timezone,
                               created_at, updated_at)
            VALUES (:id, 'synthetic', 'ACTIVE', 'zh-CN', 'UTC', :now, :now)
            """
        ),
        {"id": user_id, "now": NOW},
    )
    cost_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO generation_cost_configs
                (id, mode, model_name, output_count, points_cost, cost_multiplier,
                 effective_from, effective_to, status, created_at)
            VALUES (:id, 'TEXT_PROMPT', NULL, 2, 10, 1, :now, NULL, 'ACTIVE', :now)
            """
        ),
        {"id": cost_id, "now": NOW},
    )
    return user_id, cost_id


def _seed_task(conn, *, user_id: str, cost_id: str, key: str) -> str:
    """插入一行 `SUCCEEDED + CONSUMED` 的合成任务，返回 generation_id。"""

    generation_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO generation_tasks (id, user_id, mode, status, billing_status,
                prompt, requested_count, cost_config_id, cost_snapshot,
                reserved_points, idempotency_key, retry_count, created_at, updated_at)
            VALUES (:id, :user_id, 'TEXT_PROMPT', 'SUCCEEDED', 'CONSUMED',
                    'synthetic', 2, :cost_id, '{}'::jsonb, 10, :key, 0, :now, :now)
            """
        ),
        {
            "id": generation_id,
            "user_id": user_id,
            "cost_id": cost_id,
            "key": key,
            "now": NOW,
        },
    )
    return generation_id


def _seed_media_asset(conn, *, user_id: str, key: str, sha: str = "a" * 64) -> str:
    """插入一行 `READY` 的结果图，返回 asset_id。"""

    asset_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO media_assets (id, user_id, asset_type, purpose, status,
                visibility, storage_provider, storage_key, mime_type, size_bytes,
                sha256, created_at)
            VALUES (:id, :user_id, 'RESULT', 'RESULT', 'READY', 'PRIVATE', 'MINIO',
                    :key, 'image/png', 1024, :sha, :now)
            """
        ),
        {"id": asset_id, "user_id": user_id, "key": key, "sha": sha, "now": NOW},
    )
    return asset_id


def _seed_share(
    conn, *, user_id: str, asset_id: str, generation_id: str, slug: str
) -> str:
    """插入一行 `ACTIVE` 分享，返回 share_id。"""

    share_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO shares (id, slug, user_id, generation_id, asset_id, title,
                                status, expires_at, idempotency_key, created_at)
            VALUES (:id, :slug, :user_id, :generation_id, :asset_id, NULL,
                    'ACTIVE', :expires_at, :key, :now)
            """
        ),
        {
            "id": share_id,
            "slug": slug,
            "user_id": user_id,
            "generation_id": generation_id,
            "asset_id": asset_id,
            "expires_at": NOW,
            "key": f"key-{slug}",
            "now": NOW,
        },
    )
    return share_id


def _insert_share_asset(conn, *, share_id: str, asset_id: str, sort_order: int) -> None:
    conn.execute(
        text(
            "INSERT INTO share_assets (share_id, asset_id, sort_order, created_at) "
            "VALUES (:share_id, :asset_id, :sort_order, :now)"
        ),
        {
            "share_id": share_id,
            "asset_id": asset_id,
            "sort_order": sort_order,
            "now": NOW,
        },
    )


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #
def test_upgrade_head_lands_share_assets_structure(
    isolated_database: str, upgrade_head
) -> None:
    upgrade_head(isolated_database)
    engine = _engine(isolated_database)
    try:
        with engine.connect() as conn:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert version == CURRENT_REVISION

            columns = {
                row.column_name: row.is_nullable
                for row in conn.execute(
                    text(
                        "SELECT column_name, is_nullable FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='share_assets'"
                    )
                )
            }
            assert set(columns) == {"share_id", "asset_id", "sort_order", "created_at"}
            assert columns["sort_order"] == "NO"

            constraints = dict(
                conn.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
                        "WHERE conrelid='share_assets'::regclass"
                    )
                ).all()
            )
            assert "pk_share_assets" in constraints
            assert "PRIMARY KEY (share_id, asset_id)" in constraints["pk_share_assets"]
            assert "uq_share_assets_share_id_sort_order" in constraints
            assert "RESTRICT" in constraints["fk_share_assets_share_id_shares"]
            assert "RESTRICT" in constraints["fk_share_assets_asset_id_media_assets"]
            # 单前缀名，不得出现双前缀
            assert "ck_share_assets_sort_order" in constraints
            assert not [
                name for name in constraints if name.startswith("ck_share_assets_ck_")
            ]

            indexes = set(
                conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname='public' AND tablename='share_assets'"
                    )
                ).scalars()
            )
            assert "ix_share_assets_asset_id_share_id" in indexes

            category_length = conn.execute(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='feedback' "
                    "AND column_name='category'"
                )
            ).scalar_one()
            assert category_length == 64
    finally:
        engine.dispose()


def test_share_assets_rejects_duplicate_and_negative_sort_order(
    isolated_database: str, upgrade_head
) -> None:
    """复合主键与 CHECK 是并发与数据质量的最终裁决者。"""

    upgrade_head(isolated_database)
    engine = _engine(isolated_database)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            user_id, cost_id = _seed_identity(conn)
            asset_id = _seed_media_asset(conn, user_id=user_id, key=f"{user_id}/dup")
            generation_id = _seed_task(conn, user_id=user_id, cost_id=cost_id, key="dup")
            share_id = _seed_share(
                conn,
                user_id=user_id,
                asset_id=asset_id,
                generation_id=generation_id,
                slug="dup-slug",
            )
            _insert_share_asset(conn, share_id=share_id, asset_id=asset_id, sort_order=0)

            # 复合主键拒绝同一 (share_id, asset_id) 再来一次
            with pytest.raises(DBAPIError):
                _insert_share_asset(
                    conn, share_id=share_id, asset_id=asset_id, sort_order=1
                )
            # sort_order 负数被 CHECK 拒绝
            with pytest.raises(DBAPIError):
                _insert_share_asset(
                    conn,
                    share_id=share_id,
                    asset_id=str(uuid.uuid4()),
                    sort_order=-1,
                )
            transaction.rollback()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# 回填与往返
# --------------------------------------------------------------------------- #
def test_upgrade_backfills_single_image_shares(
    isolated_database: str, upgrade_head
) -> None:
    """0021 之前创建的分享在升级时被回填一行关联（`sort_order = 0`）。"""

    upgrade_head_at(isolated_database, PREVIOUS_REVISION)
    engine = _engine(isolated_database)
    try:
        with engine.begin() as conn:
            user_id, cost_id = _seed_identity(conn)
            asset_id = _seed_media_asset(conn, user_id=user_id, key=f"{user_id}/legacy")
            generation_id = _seed_task(
                conn, user_id=user_id, cost_id=cost_id, key="legacy-task"
            )
            share_id = _seed_share(
                conn,
                user_id=user_id,
                asset_id=asset_id,
                generation_id=generation_id,
                slug="legacy-slug",
            )
    finally:
        engine.dispose()

    upgrade_head(isolated_database)
    engine = _engine(isolated_database)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_id, sort_order FROM share_assets WHERE share_id = :id"
                ),
                {"id": share_id},
            ).all()
            assert len(rows) == 1
            assert str(rows[0].asset_id) == asset_id
            assert rows[0].sort_order == 0
    finally:
        engine.dispose()


def test_downgrade_roundtrip_is_lossless_for_single_image_history(
    isolated_database: str, upgrade_head
) -> None:
    """单图历史可由 `asset_id` 表达，因此降级/再升级必须无损往返。"""

    upgrade_head(isolated_database)
    engine = _engine(isolated_database)
    try:
        with engine.begin() as conn:
            user_id, cost_id = _seed_identity(conn)
            asset_id = _seed_media_asset(
                conn, user_id=user_id, key=f"{user_id}/roundtrip", sha="c" * 64
            )
            generation_id = _seed_task(
                conn, user_id=user_id, cost_id=cost_id, key="roundtrip-task"
            )
            share_id = _seed_share(
                conn,
                user_id=user_id,
                asset_id=asset_id,
                generation_id=generation_id,
                slug="roundtrip-slug",
            )
            # 单图分享：只有主资产一行，可由 asset_id 无损表达
            _insert_share_asset(
                conn, share_id=share_id, asset_id=asset_id, sort_order=0
            )
            conn.execute(
                text(
                    "INSERT INTO feedback (id, user_id, generation_id, rating, category, "
                    "content, status, created_at, updated_at) "
                    "VALUES (:id, :user_id, :generation_id, 4, :category, NULL, 'OPEN', "
                    ":now, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "generation_id": generation_id,
                    "category": "x" * 32,  # 恰好可由 32 表达
                    "now": NOW,
                },
            )
    finally:
        engine.dispose()

    downgrade_to(isolated_database, PREVIOUS_REVISION)
    engine = _engine(isolated_database)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == PREVIOUS_REVISION
            )
            assert (
                conn.execute(text("SELECT to_regclass('public.share_assets')")).scalar_one()
                is None
            )
            assert (
                conn.execute(
                    text(
                        "SELECT character_maximum_length FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='feedback' "
                        "AND column_name='category'"
                    )
                ).scalar_one()
                == 32
            )
            # 分享行本身没有被降级动过
            assert conn.execute(text("SELECT count(*) FROM shares")).scalar_one() == 1
    finally:
        engine.dispose()

    upgrade_head(isolated_database)
    engine = _engine(isolated_database)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == CURRENT_REVISION
            )
            rows = conn.execute(
                text("SELECT asset_id, sort_order FROM share_assets WHERE share_id = :id"),
                {"id": share_id},
            ).all()
            assert len(rows) == 1
            assert str(rows[0].asset_id) == asset_id
            assert rows[0].sort_order == 0
            assert conn.execute(text("SELECT count(*) FROM shares")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM feedback")).scalar_one() == 1
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# 降级守卫
# --------------------------------------------------------------------------- #
def test_downgrade_refuses_when_multiple_images_are_linked(
    isolated_database: str, upgrade_head
) -> None:
    """存在非主资产关联时拒绝降级：不允许悄悄丢掉多图事实。"""

    upgrade_head(isolated_database)
    engine = _engine(isolated_database)
    try:
        with engine.begin() as conn:
            user_id, cost_id = _seed_identity(conn)
            primary = _seed_media_asset(conn, user_id=user_id, key=f"{user_id}/primary")
            second = _seed_media_asset(conn, user_id=user_id, key=f"{user_id}/second")
            generation_id = _seed_task(
                conn, user_id=user_id, cost_id=cost_id, key="guard-task"
            )
            share_id = _seed_share(
                conn,
                user_id=user_id,
                asset_id=primary,
                generation_id=generation_id,
                slug="guard-slug",
            )
            _insert_share_asset(conn, share_id=share_id, asset_id=primary, sort_order=0)
            _insert_share_asset(conn, share_id=share_id, asset_id=second, sort_order=1)
    finally:
        engine.dispose()

    with pytest.raises(Exception) as failure:
        downgrade_to(isolated_database, PREVIOUS_REVISION)

    message = str(failure.value)
    assert "share_assets" in message
    assert "恢复条件" in message
    # 恢复条件必须说清**实际**要做什么：守卫读的是当前表内容，
    # 因此"撤销分享"或"导出清单"都不能让它通过
    assert "share_assets 表本身" in message
    assert "撤销分享" in message
    assert "导出" in message
    assert "不会让守卫通过" in message

    # 守卫拒绝后结构必须保持原样（没有被半途改掉）
    engine = _engine(isolated_database)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == CURRENT_REVISION
            )
            assert (
                conn.execute(text("SELECT to_regclass('public.share_assets')")).scalar_one()
                is not None
            )
            assert conn.execute(text("SELECT count(*) FROM share_assets")).scalar_one() == 2
    finally:
        engine.dispose()


def test_guard_passes_only_after_the_linked_rows_are_gone(
    isolated_database: str, upgrade_head, downgrade_to
) -> None:
    """恢复条件是可执行的：删掉非主资产关联之后，降级才被允许。

    这条用例把守卫说明里的"恢复条件"真正跑一遍，避免文档写着一套、
    守卫实际认另一套。
    """

    upgrade_head(isolated_database)
    engine = _engine(isolated_database)
    try:
        with engine.begin() as conn:
            user_id, cost_id = _seed_identity(conn)
            primary = _seed_media_asset(conn, user_id=user_id, key=f"{user_id}/p2")
            second = _seed_media_asset(conn, user_id=user_id, key=f"{user_id}/s2")
            generation_id = _seed_task(
                conn, user_id=user_id, cost_id=cost_id, key="guard-task-2"
            )
            share_id = _seed_share(
                conn,
                user_id=user_id,
                asset_id=primary,
                generation_id=generation_id,
                slug="guard-slug-2",
            )
            _insert_share_asset(conn, share_id=share_id, asset_id=primary, sort_order=0)
            _insert_share_asset(conn, share_id=share_id, asset_id=second, sort_order=1)
    finally:
        engine.dispose()

    # 1) 守卫先拒绝
    with pytest.raises(Exception):
        downgrade_to(isolated_database, PREVIOUS_REVISION)

    # 2) 按恢复条件收敛关联（只保留与主资产一致的那一行）
    engine = _engine(isolated_database)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM share_assets sa USING shares s "
                    "WHERE s.id = sa.share_id AND sa.asset_id <> s.asset_id"
                )
            )
            assert conn.execute(text("SELECT count(*) FROM share_assets")).scalar_one() == 1
            version_after_delete = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert version_after_delete == CURRENT_REVISION
    finally:
        engine.dispose()

    # 3) 现在降级被允许（说明恢复条件是真的可执行，不是文案）
    downgrade_to(isolated_database, PREVIOUS_REVISION)

    engine = _engine(isolated_database)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == PREVIOUS_REVISION
            )
            assert (
                conn.execute(text("SELECT to_regclass('public.share_assets')")).scalar_one()
                is None
            )
    finally:
        engine.dispose()


def test_downgrade_refuses_when_category_exceeds_32_chars(
    isolated_database: str, upgrade_head
) -> None:
    """存在 >32 字符的分类时拒绝降级：不允许截断历史。"""

    upgrade_head(isolated_database)
    engine = _engine(isolated_database)
    try:
        with engine.begin() as conn:
            user_id, cost_id = _seed_identity(conn)
            generation_id = _seed_task(
                conn, user_id=user_id, cost_id=cost_id, key="cat-task"
            )
            conn.execute(
                text(
                    "INSERT INTO feedback (id, user_id, generation_id, rating, category, "
                    "content, status, created_at, updated_at) "
                    "VALUES (:id, :user_id, :generation_id, 5, :category, NULL, 'OPEN', "
                    ":now, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "generation_id": generation_id,
                    "category": "c" * 64,
                    "now": NOW,
                },
            )
    finally:
        engine.dispose()

    with pytest.raises(Exception) as failure:
        downgrade_to(isolated_database, PREVIOUS_REVISION)

    message = str(failure.value)
    assert "feedback.category" in message
    assert "恢复条件" in message
    # 恢复条件同样必须说清：改写分类本身，导出只是留证
    assert "改写为 ≤32 字符" in message
    assert "导出本身不会让守卫通过" in message

    engine = _engine(isolated_database)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == CURRENT_REVISION
            )
            assert (
                conn.execute(
                    text(
                        "SELECT character_maximum_length FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='feedback' "
                        "AND column_name='category'"
                    )
                ).scalar_one()
                == 64
            )
            # 长分类原样保留，没有被截断
            stored = conn.execute(text("SELECT category FROM feedback")).scalar_one()
            assert len(stored) == 64
    finally:
        engine.dispose()
