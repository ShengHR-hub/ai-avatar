"""ENG-01 迁移测试：空库迁移、重复迁移、revision 链完整性。

对应 `02-database-design.md` 第 7 节与第 8 节「必须运行的测试」。
迁移在独立临时数据库中执行，不触碰开发库与 `ai-avatar-v1-postgres` 数据卷。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, TextClause, create_engine, text
from sqlalchemy.pool import NullPool

#: 冻结的迁移顺序（02-database-design.md 第 7 节）
FROZEN_REVISION_ORDER = [
    "0001_v1_extensions",
    "0002_users_identity",
    "0003_invites",
    "0004_wallets",
    "0005_media_assets",
    "0006_media_reference_fks",
    "0007_styles_and_templates",
    "0008_generation_cost_configs",
    "0009_generation_tasks",
    "0010_generation_io",
    "0011_orders",
    "0012_provider_attempts",
    "0013_point_ledger",
    "0014_shares_feedback",
    "0015_audit_logs",
    "0016_append_only_guards",
    # ENG-04 第 1 步：纠正性约束对齐（追加，不重排既有 revision）
    # revision 标识于 2026-09-27 缩短为 `0017_constraint_alignment`：
    # alembic_version.version_num 是 varchar(32)，旧标识 36 字符会在写版本戳时溢出。
    "0017_constraint_alignment",
    "0018_adjustment_kind_required",
    # ENG-05：媒体资源生命周期列与上传幂等键（追加，不改写 0001-0018）
    "0019_asset_lifecycle",
    # 参考图分析的持久付费登记（追加，不改写 0001-0019）
    "0020_analysis_dispatches",
    # ENG-09：多图分享关联表 + 反馈分类扩宽（追加，不改写 0001-0020）
    "0021_share_asset_links",
]
HEAD_REVISION = FROZEN_REVISION_ORDER[-1]

#: Alembic 记账表列宽为 varchar(32)；任何更长的 revision 标识都会让
#: `upgrade head` 在 `UPDATE alembic_version` 阶段报 StringDataRightTruncation。
MAX_REVISION_LENGTH = 32

EXPECTED_TABLE_COUNT = 20
EXPECTED_CHECK_COUNT = 60
EXPECTED_FOREIGN_KEY_COUNT = 32
EXPECTED_UNIQUE_COUNT = 20
EXPECTED_PARTIAL_INDEX_COUNT = 13

#: 冻结索引（02-database-design.md 第 5 节 + 第 3 节显式要求的唯一部分索引 + 0020 付费登记）
EXPECTED_INDEX_NAMES = frozenset(
    {
        "ix_users_status_created_at",
        "uq_user_emails_user_id_primary",
        "ix_invite_codes_status_expires_at",
        "ix_invite_redemptions_user_id_created_at",
        "ix_point_ledger_user_id_created_at_id",
        "ix_point_ledger_wallet_id_created_at_id",
        "ix_point_ledger_generation_id",
        "uq_point_ledger_generation_reserve",
        "uq_point_ledger_generation_final",
        "ix_generation_cost_configs_status_effective_from",
        "ix_generation_tasks_user_id_created_at_id",
        "ix_generation_tasks_status_created_at_id",
        "ix_generation_tasks_parent_task_id",
        "ix_media_assets_user_id_created_at_id",
        "ix_media_assets_expires_at",
        "ix_generation_inputs_generation_id_role_sort_order",
        "ix_generation_outputs_generation_id_output_index",
        "uq_generation_outputs_generation_provider_output",
        "ix_provider_attempts_generation_id_attempt_no",
        "uq_provider_attempts_provider_request",
        "ix_styles_status_sort_order",
        "ix_prompt_templates_style_id_status",
        "uq_prompt_templates_slug_mode_published",
        "ix_orders_user_id_created_at_id",
        "uq_orders_provider_order_id",
        "ix_shares_generation_id_status",
        "ix_feedback_status_created_at",
        "ix_audit_logs_entity_type_entity_id_created_at",
        "ix_audit_logs_actor_id_created_at",
        "ix_audit_logs_request_id",
        "ix_analysis_dispatches_usage_day",
        "uq_analysis_dispatches_user_id_usage_day_digest",
        # ENG-09：多图分享关联表（0021）
        "ix_share_assets_asset_id_share_id",
        "uq_share_assets_share_id_sort_order",
    }
)


@pytest.fixture
def empty_database(db_admin) -> Iterator[str]:
    """一个全新的空数据库；用例结束后删除。"""

    name = f"eng01_mig_{uuid.uuid4().hex[:12]}"
    db_admin.create(name)
    url = db_admin.url_for(name)
    try:
        yield url
    finally:
        db_admin.drop(name)


def _connect(url: str) -> Engine:
    return create_engine(url, poolclass=NullPool)


def test_alembic_chain_is_linear_and_matches_frozen_order(alembic_ini: Path) -> None:
    """revision 链必须单一 head、无分叉，且顺序与冻结清单一致。"""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(alembic_ini)))

    assert script.get_heads() == [HEAD_REVISION]

    # walk_revisions 从 head 向 base 走；反转后即为冻结的执行顺序
    walked = [rev.revision for rev in script.walk_revisions()]
    assert list(reversed(walked)) == FROZEN_REVISION_ORDER

    # 除首个 revision 外，每个 revision 恰好一个 down_revision（线性、无分支）
    for revision in script.walk_revisions():
        down = revision.down_revision
        if revision.revision == FROZEN_REVISION_ORDER[0]:
            assert down is None
        else:
            assert isinstance(down, str), f"{revision.revision} 的 down_revision 不是单一字符串"


def test_every_revision_id_fits_the_version_table_column() -> None:
    """revision 标识必须容得下 `alembic_version.version_num varchar(32)`。

    过长的标识会让 `upgrade head` 在迁移体成功之后的版本戳写入阶段失败，
    并使整笔迁移回滚（2026-09-27 首次真实 PostgreSQL 补验即因此暴露）。
    """

    overlong = {r: len(r) for r in FROZEN_REVISION_ORDER if len(r) > MAX_REVISION_LENGTH}

    assert not overlong, f"revision 标识超过 {MAX_REVISION_LENGTH} 字符：{overlong}"


def test_0020_is_appended_directly_after_0019(alembic_ini: Path) -> None:
    """0020 必须紧接 0019，且不改写任何既有 revision。

    `analysis_dispatches` 是每日付费上限的权威存储；如果它被插到链中间或被别的
    revision 抢先接上，`upgrade head` 的执行顺序、以及「0020 之前没有这张表」这一
    前提（迁移里写明不回填历史）都会同时失效。这里直接读 Alembic 脚本链，不靠文本匹配。
    """

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(alembic_ini)))
    revision_0020 = script.get_revision("0020_analysis_dispatches")

    assert revision_0020.down_revision == "0019_asset_lifecycle"
    # Alembic 把「未声明」归一成空集合而不是 None
    assert not revision_0020.branch_labels
    # 0019 的 down_revision 未变：0020 是追加，不是重排
    assert script.get_revision("0019_asset_lifecycle").down_revision == (
        "0018_adjustment_kind_required"
    )
    # 0020 之后只允许继续追加（ENG-09 的 0021 直接接在它后面）
    assert script.get_revision("0021_share_asset_links").down_revision == (
        "0020_analysis_dispatches"
    )
    assert script.get_heads() == ["0021_share_asset_links"]


def test_upgrade_from_empty_database_creates_frozen_schema(
    empty_database: str, upgrade_head: Callable[[str], None]
) -> None:
    """空库 `upgrade head` 成功，并落地冻结的表、约束与索引集合。"""

    upgrade_head(empty_database)
    engine = _connect(empty_database)
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == HEAD_REVISION

            tables = conn.execute(
                text(
                    """
                    SELECT count(*) FROM information_schema.tables
                     WHERE table_schema = 'public'
                       AND table_type = 'BASE TABLE'
                       AND table_name <> 'alembic_version'
                    """
                )
            ).scalar_one()
            assert tables == EXPECTED_TABLE_COUNT

            by_type = dict(
                conn.execute(
                    text(
                        """
                        SELECT c.contype, count(*)
                          FROM pg_constraint c
                          JOIN pg_class t ON t.oid = c.conrelid
                          JOIN pg_namespace n ON n.oid = t.relnamespace
                         WHERE n.nspname = 'public'
                         GROUP BY c.contype
                        """
                    )
                ).all()
            )
            assert by_type.get("c") == EXPECTED_CHECK_COUNT
            assert by_type.get("f") == EXPECTED_FOREIGN_KEY_COUNT
            assert by_type.get("u") == EXPECTED_UNIQUE_COUNT
            # 18 张业务表 + alembic_version
            assert by_type.get("p") == EXPECTED_TABLE_COUNT + 1

            existing_indexes = set(
                conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
                ).scalars()
            )
            assert sorted(EXPECTED_INDEX_NAMES - existing_indexes) == []

            partial_indexes = conn.execute(
                text(
                    """
                    SELECT count(*)
                      FROM pg_index i
                      JOIN pg_class c ON c.oid = i.indexrelid
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public' AND i.indpred IS NOT NULL
                    """
                )
            ).scalar_one()
            assert partial_indexes == EXPECTED_PARTIAL_INDEX_COUNT

            extensions = set(
                conn.execute(
                    text("SELECT extname FROM pg_extension WHERE extname IN ('pgcrypto','citext')")
                ).scalars()
            )
            assert extensions == {"pgcrypto", "citext"}
    finally:
        engine.dispose()


def test_repeat_upgrade_head_does_not_change_schema(
    empty_database: str,
    upgrade_head: Callable[[str], None],
    schema_fingerprint_sql: TextClause,
) -> None:
    """重复执行 `upgrade head` 不执行任何迁移，schema 指纹保持一致。"""

    upgrade_head(empty_database)
    engine = _connect(empty_database)
    try:
        with engine.connect() as conn:
            before = conn.execute(schema_fingerprint_sql).scalar_one()

        # 第二次与第三次执行都必须是空操作
        upgrade_head(empty_database)
        upgrade_head(empty_database)

        with engine.connect() as conn:
            after = conn.execute(schema_fingerprint_sql).scalar_one()
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

        assert after == before
        assert version == HEAD_REVISION
    finally:
        engine.dispose()


def test_upgraded_schema_lands_0017_constraints_under_real_names(
    empty_database: str, upgrade_head: Callable[[str], None]
) -> None:
    """0017 重建的两个 CHECK 必须以数据库实际单前缀名落库，且语义确实收紧。

    2026-09-27 首次真实补验发现：删除/重建若走命名约定二次渲染，会操作
    「表名前缀重复两次」的畸形标识并在 DROP 时 UndefinedObject。本用例把
    「约束真实存在于哪个名字上」钉成数据库侧断言，而不是迁移源码文本断言。
    """

    upgrade_head(empty_database)
    engine = _connect(empty_database)
    try:
        with engine.connect() as conn:
            definitions = dict(
                conn.execute(
                    text(
                        """
                        SELECT c.conname, pg_get_constraintdef(c.oid)
                          FROM pg_constraint c
                          JOIN pg_class t ON t.oid = c.conrelid
                          JOIN pg_namespace n ON n.oid = t.relnamespace
                         WHERE n.nspname = 'public'
                           AND t.relname IN ('generation_tasks', 'point_ledger')
                           AND c.contype = 'c'
                        """
                    )
                ).all()
            )
    finally:
        engine.dispose()

    assert "ck_generation_tasks_status" in definitions
    assert "ck_point_ledger_entry_type_delta" in definitions
    # 任何双前缀畸形名都不得出现在落库结果里
    assert not [name for name in definitions if name.startswith(("ck_generation_tasks_ck_", "ck_point_ledger_ck_"))]
    # 语义：生成任务状态集合不含 CANCELLED，含 NEEDS_REVIEW
    status_def = definitions["ck_generation_tasks_status"]
    assert "CANCELLED" not in status_def
    assert "NEEDS_REVIEW" in status_def
    # 语义：ADJUSTMENT 必须带非空 adjustment_kind 且不改预留
    delta_def = definitions["ck_point_ledger_entry_type_delta"]
    assert "adjustment_kind" in delta_def
    assert "IS NOT NULL" in delta_def
    assert "reserved_delta = 0" in delta_def
