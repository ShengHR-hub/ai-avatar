"""ENG-01 账务不变量与追加保护测试。

对应 `02-database-design.md` 第 3.2 节、第 6 节与第 8 节「必须运行的测试」：

- 点数流水 delta 的四种标准变化与 ADJUSTMENT 约束；
- 同一任务最多一次 RESERVE，CONSUME / RELEASE 互斥且总共最多一次；
- 钱包快照、流水 delta、任务计费状态三者一致性；
- `point_ledger` 与 `audit_logs` 的 UPDATE / DELETE 追加保护。

全部使用合成数据；用例结束回滚。触发器拒绝测试在 SAVEPOINT 内进行，
不会向数据库留下任何持久行。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.pool import NullPool

# 钱包快照 = 流水 delta 之和（02-database-design.md 第 3.2 节）
WALLET_SNAPSHOT_INVARIANT_SQL = text(
    """
    SELECT w.id AS wallet_id,
           w.available_points,
           w.reserved_points,
           COALESCE(SUM(l.available_delta), 0) AS ledger_available,
           COALESCE(SUM(l.reserved_delta), 0) AS ledger_reserved
      FROM point_wallets w
      LEFT JOIN point_ledger l ON l.wallet_id = w.id
     WHERE w.id = :wallet_id
     GROUP BY w.id, w.available_points, w.reserved_points
    """
)

# 任务状态与计费状态必须满足（02-database-design.md 第 3.2 节）：
#   SUCCEEDED => CONSUMED
#   FAILED => RELEASED
#   NEEDS_REVIEW => RESERVED（未完成人工决定），人工结算后为 NEEDS_REVIEW + CONSUMED/RELEASED
# 生成任务不使用 CANCELLED（0017 起状态约束仅六态）。
TASK_BILLING_CONSISTENCY_SQL = text(
    """
    SELECT t.id, t.status, t.billing_status, l.entry_type
      FROM generation_tasks t
      LEFT JOIN point_ledger l
        ON l.generation_id = t.id
       AND l.entry_type IN ('CONSUME', 'RELEASE')
     WHERE (t.status = 'SUCCEEDED'
            AND (t.billing_status <> 'CONSUMED' OR l.entry_type IS DISTINCT FROM 'CONSUME'))
        OR (t.status = 'FAILED'
            AND (t.billing_status <> 'RELEASED' OR l.entry_type IS DISTINCT FROM 'RELEASE'))
        OR (t.status = 'NEEDS_REVIEW'
            AND NOT (
                (t.billing_status = 'RESERVED' AND l.id IS NULL)
                OR (t.billing_status = 'CONSUMED' AND l.entry_type = 'CONSUME')
                OR (t.billing_status = 'RELEASED' AND l.entry_type = 'RELEASE')
            ))
    """
)


def _wallet_row(db: Connection, wallet_id: uuid.UUID):
    return db.execute(WALLET_SNAPSHOT_INVARIANT_SQL, {"wallet_id": wallet_id}).one()


# --------------------------------------------------------------------------- #
# 流水 delta 约束
# --------------------------------------------------------------------------- #
def test_standard_ledger_entries_are_accepted(
    db: Connection,
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)

    make_ledger_entry(wallet_id, user_id, entry_type="CREDIT", amount=100)
    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10)

    deltas = db.execute(
        text(
            """
            SELECT entry_type, available_delta, reserved_delta, amount
              FROM point_ledger
             WHERE wallet_id = :wallet_id
             ORDER BY created_at, entry_type
            """
        ),
        {"wallet_id": wallet_id},
    ).all()
    assert [(r.entry_type, r.available_delta, r.reserved_delta) for r in deltas] == [
        ("CREDIT", 100, 0),
        ("RESERVE", -10, 10),
    ]


def test_wrong_delta_for_entry_type_is_rejected(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)

    # CREDIT 的 reserved_delta 必须为 0
    rejected(
        db,
        """
        INSERT INTO point_ledger (
            wallet_id, user_id, entry_type, available_delta, reserved_delta,
            amount, idempotency_key, reason
        )
        VALUES (:wallet_id, :user_id, 'CREDIT', 100, 5, 100,
                :idempotency_key, 'synthetic')
        """,
        {"wallet_id": wallet_id, "user_id": user_id,
         "idempotency_key": f"synthetic-{uuid.uuid4().hex}"},
        contains="ck_point_ledger_entry_type_delta",
    )

    # RESERVE 的 available_delta 必须是 -amount
    rejected(
        db,
        """
        INSERT INTO point_ledger (
            wallet_id, user_id, entry_type, available_delta, reserved_delta,
            amount, idempotency_key, reason
        )
        VALUES (:wallet_id, :user_id, 'RESERVE', -5, 10, 10,
                :idempotency_key, 'synthetic')
        """,
        {"wallet_id": wallet_id, "user_id": user_id,
         "idempotency_key": f"synthetic-{uuid.uuid4().hex}"},
        contains="ck_point_ledger_entry_type_delta",
    )


def test_adjustment_requires_kind_and_sign_consistency(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    """0017 起：ADJUSTMENT 必须带 `metadata.adjustment_kind`、符号一致且不改预留。"""

    user_id = make_user()
    wallet_id = make_wallet(user_id)

    # 合法：正数调整
    make_ledger_entry(
        wallet_id,
        user_id,
        entry_type="ADJUSTMENT",
        amount=5,
        available_delta=5,
        reserved_delta=0,
        metadata={"adjustment_kind": "POSITIVE_CREDIT"},
    )
    # 合法：负数调整
    make_ledger_entry(
        wallet_id,
        user_id,
        entry_type="ADJUSTMENT",
        amount=3,
        available_delta=-3,
        reserved_delta=0,
        metadata={"adjustment_kind": "NEGATIVE_CONSUMPTION"},
    )

    invalid_cases = {
        "缺少 adjustment_kind": "{}",
        # 0018：JSON null 与缺失键同样必须被拒绝（`->>` 对二者都返回 SQL NULL，
        # 若约束里没有显式 IS NOT NULL，整条 CHECK 会返回 NULL 而被判为通过）
        "adjustment_kind 为 JSON null": '{"adjustment_kind": null}',
        "kind 非法": '{"adjustment_kind": "OTHER"}',
        "POSITIVE_CREDIT 符号不符": '{"adjustment_kind": "POSITIVE_CREDIT"}',
        "NEGATIVE_CONSUMPTION 符号不符": '{"adjustment_kind": "NEGATIVE_CONSUMPTION"}',
        "改动预留余额": '{"adjustment_kind": "POSITIVE_CREDIT"}',
        "delta 全零": '{"adjustment_kind": "POSITIVE_CREDIT"}',
    }
    deltas = {
        "缺少 adjustment_kind": (5, 0),
        "adjustment_kind 为 JSON null": (5, 0),
        "kind 非法": (5, 0),
        "POSITIVE_CREDIT 符号不符": (-5, 0),
        "NEGATIVE_CONSUMPTION 符号不符": (5, 0),
        "改动预留余额": (5, 1),
        "delta 全零": (0, 0),
    }

    for label, payload in invalid_cases.items():
        available_delta, reserved_delta = deltas[label]
        rejected(
            db,
            f"""
            INSERT INTO point_ledger (
                wallet_id, user_id, entry_type, available_delta, reserved_delta,
                amount, idempotency_key, reason, metadata
            )
            VALUES (:wallet_id, :user_id, 'ADJUSTMENT', {available_delta}, {reserved_delta},
                    5, :idempotency_key, 'synthetic', CAST('{payload}' AS jsonb))
            """,
            {
                "wallet_id": wallet_id,
                "user_id": user_id,
                "idempotency_key": f"synthetic-{uuid.uuid4().hex}",
            },
            contains="ck_point_ledger_entry_type_delta",
        )


def test_ledger_amount_must_be_positive(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)

    rejected(
        db,
        """
        INSERT INTO point_ledger (
            wallet_id, user_id, entry_type, available_delta, reserved_delta,
            amount, idempotency_key, reason
        )
        VALUES (:wallet_id, :user_id, 'CREDIT', 0, 0, 0,
                :idempotency_key, 'synthetic')
        """,
        {"wallet_id": wallet_id, "user_id": user_id,
         "idempotency_key": f"synthetic-{uuid.uuid4().hex}"},
        contains="ck_point_ledger_amount_positive",
    )


# --------------------------------------------------------------------------- #
# 预留与终结互斥
# --------------------------------------------------------------------------- #
def test_reserve_is_unique_per_generation(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)
    task_id = make_task(user_id, make_cost_config())

    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10, generation_id=task_id)

    rejected(
        db,
        """
        INSERT INTO point_ledger (
            wallet_id, user_id, entry_type, available_delta, reserved_delta,
            amount, generation_id, idempotency_key, reason
        )
        VALUES (:wallet_id, :user_id, 'RESERVE', -10, 10, 10, :generation_id,
                :idempotency_key, 'synthetic')
        """,
        {"wallet_id": wallet_id, "user_id": user_id, "generation_id": task_id,
         "idempotency_key": f"synthetic-{uuid.uuid4().hex}"},
        contains="uq_point_ledger_generation_reserve",
    )


def test_consume_and_release_are_mutually_exclusive(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)
    task_id = make_task(user_id, make_cost_config())

    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10, generation_id=task_id)
    make_ledger_entry(wallet_id, user_id, entry_type="CONSUME", amount=10, generation_id=task_id)

    # 已有 CONSUME 后不得再写 RELEASE
    rejected(
        db,
        """
        INSERT INTO point_ledger (
            wallet_id, user_id, entry_type, available_delta, reserved_delta,
            amount, generation_id, idempotency_key, reason
        )
        VALUES (:wallet_id, :user_id, 'RELEASE', 10, -10, 10, :generation_id,
                :idempotency_key, 'synthetic')
        """,
        {"wallet_id": wallet_id, "user_id": user_id, "generation_id": task_id,
         "idempotency_key": f"synthetic-{uuid.uuid4().hex}"},
        contains="uq_point_ledger_generation_final",
    )


def test_second_consume_is_rejected(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)
    task_id = make_task(user_id, make_cost_config())

    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10, generation_id=task_id)
    make_ledger_entry(wallet_id, user_id, entry_type="CONSUME", amount=10, generation_id=task_id)

    rejected(
        db,
        """
        INSERT INTO point_ledger (
            wallet_id, user_id, entry_type, available_delta, reserved_delta,
            amount, generation_id, idempotency_key, reason
        )
        VALUES (:wallet_id, :user_id, 'CONSUME', 0, -10, 10, :generation_id,
                :idempotency_key, 'synthetic')
        """,
        {"wallet_id": wallet_id, "user_id": user_id, "generation_id": task_id,
         "idempotency_key": f"synthetic-{uuid.uuid4().hex}"},
        contains="uq_point_ledger_generation_final",
    )


def test_ledger_idempotency_key_is_globally_unique(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    first_user = make_user()
    second_user = make_user()
    first_wallet = make_wallet(first_user)
    second_wallet = make_wallet(second_user)

    make_ledger_entry(
        first_wallet, first_user, entry_type="CREDIT", amount=10, idempotency_key="synthetic-global-1"
    )

    rejected(
        db,
        """
        INSERT INTO point_ledger (
            wallet_id, user_id, entry_type, available_delta, reserved_delta,
            amount, idempotency_key, reason
        )
        VALUES (:wallet_id, :user_id, 'CREDIT', 10, 0, 10,
                'synthetic-global-1', 'synthetic')
        """,
        {"wallet_id": second_wallet, "user_id": second_user},
        contains="uq_point_ledger_idempotency_key",
    )


# --------------------------------------------------------------------------- #
# 三方一致性：钱包快照 / 流水 delta / 任务计费状态
# --------------------------------------------------------------------------- #
def test_wallet_snapshot_matches_ledger_deltas(
    db: Connection,
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    """预留 → 成功消费后，钱包快照必须等于流水 delta 之和。"""

    user_id = make_user()
    wallet_id = make_wallet(user_id)
    task_id = make_task(user_id, make_cost_config(), reserved_points=10)

    make_ledger_entry(wallet_id, user_id, entry_type="CREDIT", amount=100)
    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10, generation_id=task_id)

    # 同一事务内更新快照：available 100-10=90，reserved 10
    db.execute(
        text(
            """
            UPDATE point_wallets
               SET available_points = 90, reserved_points = 10,
                   lifetime_credited = 100, version = version + 1, updated_at = now()
             WHERE id = :wallet_id
            """
        ),
        {"wallet_id": wallet_id},
    )
    row = _wallet_row(db, wallet_id)
    assert (row.available_points, row.reserved_points) == (row.ledger_available, row.ledger_reserved)

    # 成功消费：reserved 10 -> 0，lifetime_consumed 10
    make_ledger_entry(wallet_id, user_id, entry_type="CONSUME", amount=10, generation_id=task_id)
    db.execute(
        text(
            """
            UPDATE point_wallets
               SET reserved_points = 0, lifetime_consumed = 10,
                   version = version + 1, updated_at = now()
             WHERE id = :wallet_id
            """
        ),
        {"wallet_id": wallet_id},
    )
    row = _wallet_row(db, wallet_id)
    assert (row.available_points, row.reserved_points) == (90, 0)
    assert (row.available_points, row.reserved_points) == (row.ledger_available, row.ledger_reserved)


def test_negative_snapshot_is_rejected_by_check(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)

    rejected(
        db,
        "UPDATE point_wallets SET available_points = -1, updated_at = now() WHERE id = :wallet_id",
        {"wallet_id": wallet_id},
        contains="ck_point_wallets_available_points_non_negative",
    )


def test_task_billing_status_consistency_query(
    db: Connection,
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_cost_config: Callable[..., uuid.UUID],
    make_task: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    """一致性查询对合法场景返回空、对破坏场景能报出违规任务。"""

    user_id = make_user()
    wallet_id = make_wallet(user_id)
    cost_config_id = make_cost_config()

    # 合法：SUCCEEDED + CONSUMED + CONSUME 流水
    succeeded = make_task(user_id, cost_config_id, status="SUCCEEDED", billing_status="CONSUMED")
    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10, generation_id=succeeded)
    make_ledger_entry(wallet_id, user_id, entry_type="CONSUME", amount=10, generation_id=succeeded)

    # 合法：FAILED + RELEASED + RELEASE 流水
    failed = make_task(user_id, cost_config_id, status="FAILED", billing_status="RELEASED")
    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10, generation_id=failed)
    make_ledger_entry(wallet_id, user_id, entry_type="RELEASE", amount=10, generation_id=failed)

    # 合法：NEEDS_REVIEW + RESERVED 且无终结流水
    review = make_task(
        user_id, cost_config_id, status="NEEDS_REVIEW", billing_status="RESERVED"
    )
    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10, generation_id=review)

    assert db.execute(TASK_BILLING_CONSISTENCY_SQL).all() == []

    # 破坏场景：SUCCEEDED 但计费状态仍是 RESERVED
    broken = make_task(user_id, cost_config_id, status="SUCCEEDED", billing_status="RESERVED")
    make_ledger_entry(wallet_id, user_id, entry_type="RESERVE", amount=10, generation_id=broken)

    violations = db.execute(TASK_BILLING_CONSISTENCY_SQL).all()
    assert [row.id for row in violations] == [broken]


# --------------------------------------------------------------------------- #
# 追加保护
# --------------------------------------------------------------------------- #
def test_append_only_guards_are_enabled(db: Connection) -> None:
    rows = db.execute(
        text(
            """
            SELECT tgname, tgrelid::regclass::text AS tbl, tgenabled
              FROM pg_trigger
             WHERE NOT tgisinternal
             ORDER BY tgname
            """
        )
    ).all()
    assert [(r.tgname, r.tbl, r.tgenabled) for r in rows] == [
        ("trg_audit_logs_append_only", "audit_logs", "O"),
        ("trg_point_ledger_append_only", "point_ledger", "O"),
    ]


def test_append_only_acl_grants_are_revoked(db: Connection) -> None:
    """授权元数据中不得保留 UPDATE / DELETE（当前角色为超级用户，ACL 不参与运行时判定）。"""

    rows = db.execute(
        text(
            """
            SELECT c.relname, a.privilege_type
              FROM pg_class c, aclexplode(c.relacl) a
             WHERE c.relname IN ('point_ledger', 'audit_logs')
               AND a.privilege_type IN ('UPDATE', 'DELETE')
             ORDER BY c.relname, a.privilege_type
            """
        )
    ).all()
    assert rows == []

    # 追加写入所需的 INSERT 仍然保留
    insert_grants = db.execute(
        text(
            """
            SELECT c.relname, count(*)
              FROM pg_class c, aclexplode(c.relacl) a
             WHERE c.relname IN ('point_ledger', 'audit_logs')
               AND a.privilege_type = 'INSERT'
             GROUP BY c.relname
            """
        )
    ).all()
    assert sorted((r[0], r[1]) for r in insert_grants) == [
        ("audit_logs", 1),
        ("point_ledger", 1),
    ]


def test_append_only_trigger_rejects_audit_log_mutation(
    db: Connection, rejected: Callable[..., str]
) -> None:
    audit_id = db.execute(
        text(
            """
            INSERT INTO audit_logs (actor_type, action, entity_type)
            VALUES ('SYSTEM', 'synthetic_action', 'synthetic_entity')
            RETURNING id
            """
        )
    ).scalar_one()

    rejected(
        db,
        "UPDATE audit_logs SET action = 'mutated' WHERE id = :audit_id",
        {"audit_id": audit_id},
        contains="append_only_violation: UPDATE on audit_logs",
    )
    rejected(
        db,
        "DELETE FROM audit_logs WHERE id = :audit_id",
        {"audit_id": audit_id},
        contains="append_only_violation: DELETE on audit_logs",
    )

    # 追加写入仍然允许
    db.execute(
        text(
            """
            INSERT INTO audit_logs (actor_type, action, entity_type)
            VALUES ('WORKER', 'synthetic_action_2', 'synthetic_entity')
            """
        )
    )


def test_append_only_trigger_rejects_ledger_mutation(
    db: Connection,
    rejected: Callable[..., str],
    make_user: Callable[..., uuid.UUID],
    make_wallet: Callable[..., uuid.UUID],
    make_ledger_entry: Callable[..., uuid.UUID],
) -> None:
    user_id = make_user()
    wallet_id = make_wallet(user_id)
    entry_id = make_ledger_entry(wallet_id, user_id, entry_type="CREDIT", amount=10)

    rejected(
        db,
        "UPDATE point_ledger SET reason = 'mutated' WHERE id = :entry_id",
        {"entry_id": entry_id},
        contains="append_only_violation: UPDATE on point_ledger",
    )
    rejected(
        db,
        "DELETE FROM point_ledger WHERE id = :entry_id",
        {"entry_id": entry_id},
        contains="append_only_violation: DELETE on point_ledger",
    )

    remaining = db.execute(
        text("SELECT count(*) FROM point_ledger WHERE id = :entry_id"), {"entry_id": entry_id}
    ).scalar_one()
    assert remaining == 1


# --------------------------------------------------------------------------- #
# 并发预留：两个独立事务竞争同一钱包
# --------------------------------------------------------------------------- #
RESERVE_COST = 10


class InsufficientPointsError(RuntimeError):
    """合成信号：钱包可用点数不足以预留（等价 Points Service 的业务校验）。"""


@pytest.fixture(scope="module")
def isolated_engine(db_admin, upgrade_head) -> Iterator[Engine]:
    """模块级独立临时库。

    并发用例必须让两个连接看到**已提交**的数据，且成功预留写入的流水不可删除
    （`point_ledger` 有追加保护触发器），因此用可整体 DROP 的临时库隔离，
    而不是依赖事务回滚；用例结束即删除该库。
    """

    name = f"eng01_conc_{uuid.uuid4().hex[:12]}"
    db_admin.create(name)
    url = db_admin.url_for(name)
    engine = create_engine(url, poolclass=NullPool)
    try:
        upgrade_head(url)
        yield engine
    finally:
        engine.dispose()
        db_admin.drop(name)


def _seed_contended_wallet(
    engine: Engine, *, available_points: int, workers: int = 2
) -> dict[str, object]:
    """提交一个钱包（含等额 CREDIT 流水）与若干待预留任务。

    先写 CREDIT 流水再写快照，保证「快照 = 流水 delta 之和」从一开始就成立。
    """

    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                """
                INSERT INTO users (status, locale, timezone, updated_at)
                VALUES ('ACTIVE', 'zh-CN', 'Asia/Shanghai', now())
                RETURNING id
                """
            )
        ).scalar_one()

        wallet_id = conn.execute(
            text(
                """
                INSERT INTO point_wallets (
                    user_id, available_points, lifetime_credited, updated_at
                )
                VALUES (:user_id, :points, :points, now())
                RETURNING id
                """
            ),
            {"user_id": user_id, "points": available_points},
        ).scalar_one()

        conn.execute(
            text(
                """
                INSERT INTO point_ledger (
                    wallet_id, user_id, entry_type, available_delta, reserved_delta,
                    amount, idempotency_key, reason
                )
                VALUES (:wallet_id, :user_id, 'CREDIT', :points, 0, :points,
                        :idempotency_key, 'synthetic seed credit')
                """
            ),
            {
                "wallet_id": wallet_id,
                "user_id": user_id,
                "points": available_points,
                "idempotency_key": f"synthetic-seed-{uuid.uuid4().hex}",
            },
        )

        cost_config_id = conn.execute(
            text(
                """
                INSERT INTO generation_cost_configs (
                    mode, model_name, output_count, points_cost, effective_from, status
                )
                VALUES ('PHOTO_PROMPT', :model_name, 2, :points_cost, now(), 'ACTIVE')
                RETURNING id
                """
            ),
            {
                "model_name": f"synthetic-model-{uuid.uuid4().hex[:8]}",
                "points_cost": RESERVE_COST,
            },
        ).scalar_one()

        task_ids = [
            conn.execute(
                text(
                    """
                    INSERT INTO generation_tasks (
                        user_id, mode, status, billing_status, prompt, requested_count,
                        cost_config_id, cost_snapshot, reserved_points,
                        idempotency_key, updated_at
                    )
                    VALUES (:user_id, 'PHOTO_PROMPT', 'QUEUED', 'RESERVED', 'synthetic', 2,
                            :cost_config_id, '{}'::jsonb, :reserved_points,
                            :idempotency_key, now())
                    RETURNING id
                    """
                ),
                {
                    "user_id": user_id,
                    "cost_config_id": cost_config_id,
                    "reserved_points": RESERVE_COST,
                    "idempotency_key": f"synthetic-conc-{uuid.uuid4().hex}",
                },
            ).scalar_one()
            for _ in range(workers)
        ]

    return {
        "user_id": user_id,
        "wallet_id": wallet_id,
        "cost_config_id": cost_config_id,
        "task_ids": task_ids,
    }


def _reserve_worker(
    engine: Engine,
    *,
    wallet_id: uuid.UUID,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    cost: int,
    barrier: threading.Barrier,
) -> str:
    """等价的预留流程：锁钱包 → 校验余额 → 写 RESERVE → 更新钱包快照。

    两个线程使用各自独立的连接与事务，通过 `barrier` 同时进入临界区制造真实竞争。
    """

    try:
        with engine.connect() as conn:
            with conn.begin():
                barrier.wait(timeout=30)
                available = conn.execute(
                    text(
                        "SELECT available_points FROM point_wallets "
                        "WHERE id = :wallet_id FOR UPDATE"
                    ),
                    {"wallet_id": wallet_id},
                ).scalar_one()
                if available < cost:
                    raise InsufficientPointsError(f"available={available} cost={cost}")

                conn.execute(
                    text(
                        """
                        INSERT INTO point_ledger (
                            wallet_id, user_id, entry_type, available_delta, reserved_delta,
                            amount, generation_id, idempotency_key, reason
                        )
                        VALUES (:wallet_id, :user_id, 'RESERVE', -:cost, :cost,
                                :cost, :generation_id, :idempotency_key, 'synthetic reserve')
                        """
                    ),
                    {
                        "wallet_id": wallet_id,
                        "user_id": user_id,
                        "cost": cost,
                        "generation_id": task_id,
                        "idempotency_key": f"synthetic-reserve-{task_id}",
                    },
                )
                conn.execute(
                    text(
                        """
                        UPDATE point_wallets
                           SET available_points = available_points - :cost,
                               reserved_points = reserved_points + :cost,
                               version = version + 1,
                               updated_at = now()
                         WHERE id = :wallet_id
                        """
                    ),
                    {"cost": cost, "wallet_id": wallet_id},
                )
        return "reserved"
    except InsufficientPointsError as exc:
        return f"insufficient_points ({exc})"


def _run_concurrent_reservations(
    engine: Engine, scenario: dict[str, object], *, cost: int = RESERVE_COST
) -> list[str]:
    wallet_id = scenario["wallet_id"]
    user_id = scenario["user_id"]
    task_ids = scenario["task_ids"]
    assert isinstance(wallet_id, uuid.UUID)
    assert isinstance(user_id, uuid.UUID)
    assert isinstance(task_ids, list)

    barrier = threading.Barrier(len(task_ids))
    with ThreadPoolExecutor(max_workers=len(task_ids)) as pool:
        futures = [
            pool.submit(
                _reserve_worker,
                engine,
                wallet_id=wallet_id,
                user_id=user_id,
                task_id=task_id,
                cost=cost,
                barrier=barrier,
            )
            for task_id in task_ids
        ]
        return [future.result(timeout=60) for future in futures]


def _wallet_state(engine: Engine, wallet_id: uuid.UUID) -> dict[str, int]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT w.available_points,
                       w.reserved_points,
                       COALESCE(SUM(l.available_delta), 0) AS ledger_available,
                       COALESCE(SUM(l.reserved_delta), 0) AS ledger_reserved
                  FROM point_wallets w
                  LEFT JOIN point_ledger l ON l.wallet_id = w.id
                 WHERE w.id = :wallet_id
                 GROUP BY w.id, w.available_points, w.reserved_points
                """
            ),
            {"wallet_id": wallet_id},
        ).one()
        return {
            "available_points": row.available_points,
            "reserved_points": row.reserved_points,
            "ledger_available": row.ledger_available,
            "ledger_reserved": row.ledger_reserved,
        }


def test_concurrent_reservation_allows_exactly_one_winner(
    isolated_engine: Engine,
) -> None:
    """余额只够一次预留时，两个并发事务只能有一个成功，且余额不得为负。"""

    scenario = _seed_contended_wallet(isolated_engine, available_points=RESERVE_COST)
    wallet_id = scenario["wallet_id"]
    task_ids = scenario["task_ids"]
    assert isinstance(wallet_id, uuid.UUID)
    assert isinstance(task_ids, list)

    results = _run_concurrent_reservations(isolated_engine, scenario)

    # 恰好一个事务预留成功，另一个必须因余额不足失败（顺序无关）
    assert results.count("reserved") == 1, results
    assert sum(1 for r in results if r.startswith("insufficient_points")) == 1, results

    state = _wallet_state(isolated_engine, wallet_id)
    assert state["available_points"] == 0
    assert state["reserved_points"] == RESERVE_COST
    # 余额非负，且钱包快照与流水 delta 之和一致
    assert state["available_points"] >= 0
    assert state["available_points"] == state["ledger_available"]
    assert state["reserved_points"] == state["ledger_reserved"]

    with isolated_engine.connect() as conn:
        reserves = conn.execute(
            text(
                """
                SELECT generation_id, available_delta, reserved_delta
                  FROM point_ledger
                 WHERE wallet_id = :wallet_id AND entry_type = 'RESERVE'
                """
            ),
            {"wallet_id": wallet_id},
        ).all()
        assert len(reserves) == 1
        assert (reserves[0].available_delta, reserves[0].reserved_delta) == (
            -RESERVE_COST,
            RESERVE_COST,
        )

        # 失败的一方不得留下任何流水
        losing_task_ids = [tid for tid in task_ids if tid != reserves[0].generation_id]
        assert len(losing_task_ids) == 1
        orphan = conn.execute(
            text(
                "SELECT count(*) FROM point_ledger WHERE generation_id = :task_id"
            ),
            {"task_id": losing_task_ids[0]},
        ).scalar_one()
        assert orphan == 0


def test_concurrent_reservation_cannot_overdraw_wallet(
    isolated_engine: Engine,
) -> None:
    """余额低于单次成本时，两个并发事务都必须失败且钱包保持不变。"""

    scenario = _seed_contended_wallet(isolated_engine, available_points=5)
    wallet_id = scenario["wallet_id"]
    assert isinstance(wallet_id, uuid.UUID)

    results = _run_concurrent_reservations(isolated_engine, scenario)

    assert all(result.startswith("insufficient_points") for result in results), results

    state = _wallet_state(isolated_engine, wallet_id)
    assert state["available_points"] == 5
    assert state["reserved_points"] == 0
    assert state["available_points"] == state["ledger_available"]
    assert state["reserved_points"] == state["ledger_reserved"]

    with isolated_engine.connect() as conn:
        reserves = conn.execute(
            text(
                """
                SELECT count(*) FROM point_ledger
                 WHERE wallet_id = :wallet_id AND entry_type = 'RESERVE'
                """
            ),
            {"wallet_id": wallet_id},
        ).scalar_one()
        assert reserves == 0
