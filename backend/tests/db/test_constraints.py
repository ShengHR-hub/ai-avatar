"""ENG-01 约束测试：外键、唯一键、CHECK、部分索引、NULLS NOT DISTINCT。

对应 `02-database-design.md` 第 3 至 5 节与第 8 节「必须运行的测试」。
全部使用合成数据，并在用例结束时回滚。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import Connection, text

EXPECTED_TABLE_COUNT = 20
EXPECTED_CHECK_COUNT = 60
EXPECTED_FOREIGN_KEY_COUNT = 32
EXPECTED_UNIQUE_COUNT = 20
EXPECTED_IX_INDEX_COUNT = 25
EXPECTED_PARTIAL_INDEX_COUNT = 13


# --------------------------------------------------------------------------- #
# 合成数据辅助
# --------------------------------------------------------------------------- #
def make_media_asset(
    db: Connection,
    user_id: uuid.UUID,
    *,
    asset_type: str = "RESULT",
    purpose: str = "RESULT",
    status: str = "READY",
    storage_provider: str = "MINIO",
    storage_key: str | None = None,
    size_bytes: int = 1024,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    return db.execute(
        text(
            """
            INSERT INTO media_assets (
                user_id, asset_type, purpose, status, storage_provider, storage_key,
                mime_type, size_bytes, sha256, idempotency_key
            )
            VALUES (
                :user_id, :asset_type, :purpose, :status, :storage_provider, :storage_key,
                'image/png', :size_bytes, repeat('a', 64), :idempotency_key
            )
            RETURNING id
            """
        ),
        {
            "user_id": user_id,
            "asset_type": asset_type,
            "purpose": purpose,
            "status": status,
            "storage_provider": storage_provider,
            "idempotency_key": idempotency_key,
            "storage_key": storage_key or f"synthetic/{uuid.uuid4().hex}.png",
            "size_bytes": size_bytes,
        },
    ).scalar_one()


def make_user_email(
    db: Connection, user_id: uuid.UUID, email: str, *, is_primary: bool = False
) -> uuid.UUID:
    return db.execute(
        text(
            """
            INSERT INTO user_emails (user_id, email, is_primary)
            VALUES (:user_id, :email, :is_primary)
            RETURNING id
            """
        ),
        {"user_id": user_id, "email": email, "is_primary": is_primary},
    ).scalar_one()


def make_prompt_template(
    db: Connection, *, slug: str, mode: str, version: int, status: str
) -> uuid.UUID:
    return db.execute(
        text(
            """
            INSERT INTO prompt_templates (slug, mode, template_text, version, status, updated_at)
            VALUES (:slug, :mode, 'synthetic template', :version, :status, now())
            RETURNING id
            """
        ),
        {"slug": slug, "mode": mode, "version": version, "status": status},
    ).scalar_one()


def make_order(
    db: Connection, user_id: uuid.UUID, *, provider_order_id: str | None = None
) -> uuid.UUID:
    return db.execute(
        text(
            """
            INSERT INTO orders (
                user_id, status, currency, amount_minor, points_amount,
                provider_order_id, idempotency_key, updated_at
            )
            VALUES (
                :user_id, 'PENDING', 'CNY', 1000, 100,
                :provider_order_id, :idempotency_key, now()
            )
            RETURNING id
            """
        ),
        {
            "user_id": user_id,
            "provider_order_id": provider_order_id,
            "idempotency_key": f"synthetic-{uuid.uuid4().hex}",
        },
    ).scalar_one()


def make_provider_attempt(
    db: Connection,
    generation_id: uuid.UUID,
    *,
    attempt_no: int = 1,
    request_id: str | None = None,
    estimated_cost: float | None = None,
    currency: str | None = None,
) -> uuid.UUID:
    return db.execute(
        text(
            """
            INSERT INTO provider_attempts (
                generation_id, attempt_no, provider_name, model_name,
                request_id, status, estimated_cost, currency, started_at
            )
            VALUES (
                :generation_id, :attempt_no, 'synthetic-provider', 'synthetic-model',
                :request_id, 'SUCCEEDED', :estimated_cost, :currency, now()
            )
            RETURNING id
            """
        ),
        {
            "generation_id": generation_id,
            "attempt_no": attempt_no,
            "request_id": request_id,
            "estimated_cost": estimated_cost,
            "currency": currency,
        },
    ).scalar_one()


# --------------------------------------------------------------------------- #
# 结构清单
# --------------------------------------------------------------------------- #
def test_constraint_and_index_inventory(db: Connection) -> None:
    tables = db.execute(
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
        db.execute(
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

    ix_indexes = db.execute(
        text(
            """
            SELECT count(*) FROM pg_indexes
             WHERE schemaname = 'public' AND indexname LIKE 'ix\\_%'
            """
        )
    ).scalar_one()
    assert ix_indexes == EXPECTED_IX_INDEX_COUNT

    partial_indexes = db.execute(
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


def test_deferred_avatar_foreign_key_is_set_null(db: Connection) -> None:
    """0006 添加的头像外键必须是 ON DELETE SET NULL。"""

    row = db.execute(
        text(
            """
            SELECT confdeltype, pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conname = 'fk_users_avatar_asset_id_media_assets'
            """
        )
    ).one_or_none()
    assert row is not None, "缺少 users.avatar_asset_id 外键"
    assert row.confdeltype == "n"  # n = SET NULL
    assert "ON DELETE SET NULL" in row.definition


def test_provider_cost_currency_check_exists(db: Connection) -> None:
    definition = db.execute(
        text(
            """
            SELECT pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conname = 'ck_provider_attempts_currency_required_with_cost'
            """
        )
    ).scalar_one_or_none()
    assert definition is not None, "缺少 Provider 成本币种 CHECK"
    assert "estimated_cost IS NULL" in definition
    assert "currency IS NOT NULL" in definition


# --------------------------------------------------------------------------- #
# 外键删除策略
# --------------------------------------------------------------------------- #
def test_foreign_key_restrict_blocks_user_delete(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    make_wallet(user_id)

    rejected(
        db,
        "DELETE FROM users WHERE id = :user_id",
        {"user_id": user_id},
        contains="fk_point_wallets_user_id_users",
    )


def test_foreign_key_set_null_clears_avatar(
    db: Connection, make_user: Callable[..., uuid.UUID]
) -> None:
    user_id = make_user()
    asset_id = make_media_asset(db, user_id, asset_type="ORIGINAL")
    db.execute(
        text("UPDATE users SET avatar_asset_id = :asset_id WHERE id = :user_id"),
        {"asset_id": asset_id, "user_id": user_id},
    )

    db.execute(text("DELETE FROM media_assets WHERE id = :asset_id"), {"asset_id": asset_id})

    avatar_asset_id = db.execute(
        text("SELECT avatar_asset_id FROM users WHERE id = :user_id"), {"user_id": user_id}
    ).scalar_one()
    assert avatar_asset_id is None


# --------------------------------------------------------------------------- #
# 唯一键与部分唯一索引
# --------------------------------------------------------------------------- #
def test_email_unique_is_case_insensitive(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
) -> None:
    first_user = make_user()
    second_user = make_user()
    make_user_email(db, first_user, "Synthetic.User@Example.COM")

    rejected(
        db,
        "INSERT INTO user_emails (user_id, email, is_primary) "
        "VALUES (:user_id, 'synthetic.user@example.com', false)",
        {"user_id": second_user},
        contains="uq_user_emails_email",
    )


def test_only_one_primary_email_per_user(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    make_user_email(db, user_id, "primary@example.com", is_primary=True)

    rejected(
        db,
        "INSERT INTO user_emails (user_id, email, is_primary) "
        "VALUES (:user_id, 'second@example.com', true)",
        {"user_id": user_id},
        contains="uq_user_emails_user_id_primary",
    )


def test_media_storage_key_is_unique_per_provider(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    make_media_asset(db, user_id, storage_key="synthetic/duplicate.png")

    rejected(
        db,
        """
        INSERT INTO media_assets (
            user_id, asset_type, purpose, status, storage_provider, storage_key,
            mime_type, size_bytes, sha256
        )
        VALUES (:user_id, 'RESULT', 'RESULT', 'READY', 'MINIO',
                'synthetic/duplicate.png', 'image/png', 1024, repeat('b', 64))
        """,
        {"user_id": user_id},
        contains="uq_media_assets_storage_provider_storage_key",
    )


def test_task_idempotency_key_is_unique_per_user(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    cost_config_id = make_cost_config()
    make_task(user_id, cost_config_id, idempotency_key="synthetic-idem-1")

    rejected(
        db,
        """
        INSERT INTO generation_tasks (
            user_id, mode, status, billing_status, prompt, requested_count,
            cost_config_id, cost_snapshot, reserved_points, idempotency_key, updated_at
        )
        VALUES (:user_id, 'PHOTO_PROMPT', 'QUEUED', 'RESERVED', 'synthetic', 2,
                :cost_config_id, '{}'::jsonb, 10, 'synthetic-idem-1', now())
        """,
        {"user_id": user_id, "cost_config_id": cost_config_id},
        contains="uq_generation_tasks_user_idempotency",
    )


def test_only_one_published_template_per_slug_and_mode(
    db: Connection, rejected: Callable[..., str]
) -> None:
    slug = f"synthetic-{uuid.uuid4().hex[:8]}"
    make_prompt_template(db, slug=slug, mode="PHOTO_PROMPT", version=1, status="PUBLISHED")

    # 非发布版本可以共存
    make_prompt_template(db, slug=slug, mode="PHOTO_PROMPT", version=2, status="DRAFT")

    rejected(
        db,
        """
        INSERT INTO prompt_templates (slug, mode, template_text, version, status, updated_at)
        VALUES (:slug, 'PHOTO_PROMPT', 'synthetic', 3, 'PUBLISHED', now())
        """,
        {"slug": slug},
        contains="uq_prompt_templates_slug_mode_published",
    )


def test_provider_request_id_is_unique(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    cost_config_id = make_cost_config()
    first_task = make_task(user_id, cost_config_id)
    second_task = make_task(user_id, cost_config_id)

    make_provider_attempt(db, first_task, attempt_no=1, request_id="synthetic-req-1")

    rejected(
        db,
        """
        INSERT INTO provider_attempts (
            generation_id, attempt_no, provider_name, model_name,
            request_id, status, started_at
        )
        VALUES (:generation_id, 1, 'synthetic-provider', 'synthetic-model',
                'synthetic-req-1', 'SUCCEEDED', now())
        """,
        {"generation_id": second_task},
        contains="uq_provider_attempts_provider_request",
    )


def test_provider_order_id_is_unique(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
) -> None:
    first_user = make_user()
    second_user = make_user()
    make_order(db, first_user, provider_order_id="synthetic-order-1")

    rejected(
        db,
        """
        INSERT INTO orders (
            user_id, status, currency, amount_minor, points_amount,
            provider_order_id, idempotency_key, updated_at
        )
        VALUES (:user_id, 'PENDING', 'CNY', 1000, 100,
                'synthetic-order-1', 'synthetic-order-idem', now())
        """,
        {"user_id": second_user},
        contains="uq_orders_provider_order_id",
    )


def test_cost_config_window_uses_nulls_not_distinct(
    db: Connection, rejected: Callable[..., str]
) -> None:
    """model_name 为 NULL 时，同一窗口仍必须被唯一键拒绝。"""

    nulls_not_distinct = db.execute(
        text(
            """
            SELECT i.indnullsnotdistinct
              FROM pg_constraint c
              JOIN pg_index i ON i.indexrelid = c.conindid
             WHERE c.conname = 'uq_generation_cost_configs_window'
            """
        )
    ).scalar_one()
    assert nulls_not_distinct is True

    effective_from = "2026-01-01 00:00:00+00"
    db.execute(
        text(
            """
            INSERT INTO generation_cost_configs (
                mode, model_name, output_count, points_cost, effective_from, status
            )
            VALUES ('TEXT_PROMPT', NULL, 2, 10, :effective_from, 'ACTIVE')
            """
        ),
        {"effective_from": effective_from},
    )

    rejected(
        db,
        """
        INSERT INTO generation_cost_configs (
            mode, model_name, output_count, points_cost, effective_from, status
        )
        VALUES ('TEXT_PROMPT', NULL, 2, 10, :effective_from, 'ACTIVE')
        """,
        {"effective_from": effective_from},
        contains="uq_generation_cost_configs_window",
    )


# --------------------------------------------------------------------------- #
# CHECK 约束
# --------------------------------------------------------------------------- #
def test_check_rejects_unknown_user_status(
    db: Connection, rejected: Callable[..., str]
) -> None:
    rejected(
        db,
        """
        INSERT INTO users (status, locale, timezone, updated_at)
        VALUES ('UNKNOWN_STATUS', 'zh-CN', 'Asia/Shanghai', now())
        """,
        contains="ck_users_status",
    )


def test_check_rejects_negative_wallet_balance(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    rejected(
        db,
        """
        INSERT INTO point_wallets (user_id, available_points, updated_at)
        VALUES (:user_id, -1, now())
        """,
        {"user_id": user_id},
        contains="ck_point_wallets_available_points_non_negative",
    )


def test_check_rejects_rating_out_of_range(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    task_id = make_task(user_id, make_cost_config())

    rejected(
        db,
        """
        INSERT INTO feedback (user_id, generation_id, rating, updated_at)
        VALUES (:user_id, :generation_id, 6, now())
        """,
        {"user_id": user_id, "generation_id": task_id},
        contains="ck_feedback_rating_range",
    )


def test_check_rejects_inverted_effective_window(
    db: Connection, rejected: Callable[..., str]
) -> None:
    rejected(
        db,
        """
        INSERT INTO generation_cost_configs (
            mode, model_name, output_count, points_cost,
            effective_from, effective_to, status
        )
        VALUES ('PHOTO_PROMPT', 'synthetic-model', 2, 10,
                '2026-02-01 00:00:00+00', '2026-01-01 00:00:00+00', 'ACTIVE')
        """,
        contains="ck_generation_cost_configs_effective_window",
    )


def test_check_rejects_unknown_task_status_and_billing_status(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    cost_config_id = make_cost_config()

    rejected(
        db,
        """
        INSERT INTO generation_tasks (
            user_id, mode, status, billing_status, prompt, requested_count,
            cost_config_id, cost_snapshot, reserved_points, idempotency_key, updated_at
        )
        VALUES (:user_id, 'PHOTO_PROMPT', 'DONE', 'RESERVED', 'synthetic', 2,
                :cost_config_id, '{}'::jsonb, 10, :idempotency_key, now())
        """,
        {"user_id": user_id, "cost_config_id": cost_config_id,
         "idempotency_key": f"synthetic-{uuid.uuid4().hex}"},
        contains="ck_generation_tasks_status",
    )

    rejected(
        db,
        """
        INSERT INTO generation_tasks (
            user_id, mode, status, billing_status, prompt, requested_count,
            cost_config_id, cost_snapshot, reserved_points, idempotency_key, updated_at
        )
        VALUES (:user_id, 'PHOTO_PROMPT', 'QUEUED', 'REFUNDED', 'synthetic', 2,
                :cost_config_id, '{}'::jsonb, 10, :idempotency_key, now())
        """,
        {"user_id": user_id, "cost_config_id": cost_config_id,
         "idempotency_key": f"synthetic-{uuid.uuid4().hex}"},
        contains="ck_generation_tasks_billing_status",
    )


def test_provider_cost_requires_currency(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
) -> None:
    task_id = make_task(make_user(), make_cost_config())

    # 无成本时允许没有币种
    make_provider_attempt(db, task_id, attempt_no=1, estimated_cost=None, currency=None)

    # 有成本但缺少币种必须被拒绝
    rejected(
        db,
        """
        INSERT INTO provider_attempts (
            generation_id, attempt_no, provider_name, model_name,
            status, estimated_cost, currency, started_at
        )
        VALUES (:generation_id, 2, 'synthetic-provider', 'synthetic-model',
                'SUCCEEDED', 0.25, NULL, now())
        """,
        {"generation_id": task_id},
        contains="ck_provider_attempts_currency_required_with_cost",
    )


# --------------------------------------------------------------------------- #
# updated_at 必须显式写入
# --------------------------------------------------------------------------- #
def test_updated_at_has_no_database_default_and_must_be_provided(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
) -> None:
    """设计文档只要求 updated_at NOT NULL，未定义数据库默认值。"""

    column_default = db.execute(
        text(
            """
            SELECT column_default
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'users'
               AND column_name = 'updated_at'
            """
        )
    ).scalar_one()
    assert column_default is None

    user_id = make_user()
    rejected(
        db,
        """
        INSERT INTO point_wallets (user_id) VALUES (:user_id)
        """,
        {"user_id": user_id},
        contains="updated_at",
    )


# --------------------------------------------------------------------------- #
# 0019：媒体资源生命周期列与上传幂等键
# --------------------------------------------------------------------------- #
def test_media_assets_has_lifecycle_columns(db: Connection) -> None:
    """0019 之后 `purpose` / `status` 必须是 NOT NULL 列。"""

    nullable = dict(
        db.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'media_assets' AND column_name IN "
                "('purpose', 'status', 'idempotency_key')"
            )
        ).all()
    )

    assert nullable["purpose"] == "NO"
    assert nullable["status"] == "NO"
    assert nullable["idempotency_key"] == "YES"  # 结果图等可无上传键


def test_check_rejects_unknown_asset_purpose(
    db: Connection, rejected, make_user
) -> None:
    user_id = make_user()

    rejected(
        db,
        "INSERT INTO media_assets (user_id, asset_type, purpose, status, "
        "storage_provider, storage_key, mime_type, size_bytes, sha256) VALUES "
        "(:user_id, 'ORIGINAL', 'SIDE_SHOT', 'READY', 'MINIO', "
        "'synthetic/purpose.png', 'image/png', 10, repeat('a', 64))",
        {"user_id": user_id},
        contains="ck_media_assets_purpose",
    )


def test_check_rejects_unknown_asset_status(
    db: Connection, rejected, make_user
) -> None:
    user_id = make_user()

    rejected(
        db,
        "INSERT INTO media_assets (user_id, asset_type, purpose, status, "
        "storage_provider, storage_key, mime_type, size_bytes, sha256) VALUES "
        "(:user_id, 'ORIGINAL', 'SOURCE', 'UPLOADING', 'MINIO', "
        "'synthetic/status.png', 'image/png', 10, repeat('a', 64))",
        {"user_id": user_id},
        contains="ck_media_assets_status",
    )


def test_deleted_timestamp_and_status_must_agree(
    db: Connection, rejected, make_user
) -> None:
    """`deleted_at` 与 `status='DELETED'` 互为充要条件，不允许脱钩。"""

    user_id = make_user()

    rejected(
        db,
        "INSERT INTO media_assets (user_id, asset_type, purpose, status, "
        "storage_provider, storage_key, mime_type, size_bytes, sha256, deleted_at) "
        "VALUES (:user_id, 'RESULT', 'RESULT', 'READY', 'MINIO', "
        "'synthetic/desync.png', 'image/png', 10, repeat('a', 64), now())",
        {"user_id": user_id},
        contains="ck_media_assets_deleted_at_matches_status",
    )


def test_expired_status_requires_expires_at(
    db: Connection, rejected, make_user
) -> None:
    user_id = make_user()

    rejected(
        db,
        "INSERT INTO media_assets (user_id, asset_type, purpose, status, "
        "storage_provider, storage_key, mime_type, size_bytes, sha256) VALUES "
        "(:user_id, 'RESULT', 'RESULT', 'EXPIRED', 'MINIO', "
        "'synthetic/noexpiry.png', 'image/png', 10, repeat('a', 64))",
        {"user_id": user_id},
        contains="ck_media_assets_expired_requires_expires_at",
    )


def test_upload_idempotency_key_is_unique_per_user(
    db: Connection, rejected, make_user
) -> None:
    """同一用户的同一上传键只能有一条资源行（部分唯一索引）。"""

    user_id = make_user()
    make_media_asset(db, user_id, idempotency_key="synthetic-upload-key")

    rejected(
        db,
        "INSERT INTO media_assets (user_id, asset_type, purpose, status, "
        "storage_provider, storage_key, mime_type, size_bytes, sha256, "
        "idempotency_key) VALUES (:user_id, 'ORIGINAL', 'SOURCE', 'PENDING_UPLOAD', "
        "'MINIO', 'synthetic/dup-key.png', 'image/png', 10, repeat('a', 64), "
        "'synthetic-upload-key')",
        {"user_id": user_id},
        contains="uq_media_assets_user_id_idempotency_key",
    )


def test_upload_idempotency_keys_are_still_allowed_when_null(
    db: Connection, make_user
) -> None:
    """结果资源没有上传键，必须可以继续插入多行（部分索引不约束 NULL）。"""

    user_id = make_user()

    first = make_media_asset(db, user_id, storage_key="synthetic/r1.png")
    second = make_media_asset(db, user_id, storage_key="synthetic/r2.png")

    assert first != second
