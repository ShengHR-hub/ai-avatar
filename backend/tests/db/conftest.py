"""ENG-01 数据库测试公共夹具。

边界：

- 只连接 `ai-avatar-v1-private` 网络中的本地 PostgreSQL（连接串来自服务端 `DATABASE_URL`）；
- 只使用合成数据，不读取或转换旧 SQLite/Drizzle 表；
- 结构类用例在事务中执行并回滚，不向开发库写入持久数据；
- 迁移类用例使用独立临时数据库，用例结束即删除，不触碰 `ai-avatar-v1-postgres` 数据卷；
- 不创建临时业务角色（非超级用户 ACL 验证延后到部署/生产角色配置任务）。
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, TextClause, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.config import get_database_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

#: schema 指纹：全部约束定义与索引定义的 md5，用于证明迁移可重复执行
SCHEMA_FINGERPRINT_SQL = text(
    """
    SELECT md5(string_agg(x, '|' ORDER BY x)) AS fingerprint
      FROM (
            SELECT t.relname || ':' || c.conname || ':' || pg_get_constraintdef(c.oid) AS x
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
              JOIN pg_namespace n ON n.oid = t.relnamespace
             WHERE n.nspname = 'public'
            UNION ALL
            SELECT tablename || ':' || indexname || ':' || indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
           ) AS parts
    """
)


class DatabaseAdmin:
    """在同一个 PostgreSQL 实例上创建/删除临时数据库（用于迁移测试）。"""

    def __init__(self, maintenance_engine: Engine, base_url: str) -> None:
        self._engine = maintenance_engine
        self._base_url = base_url

    def create(self, name: str) -> None:
        with self._engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))

    def drop(self, name: str) -> None:
        with self._engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

    def url_for(self, name: str) -> str:
        return (
            make_url(self._base_url)
            .set(database=name)
            .render_as_string(hide_password=False)
        )


def upgrade_to_head(database_url: str) -> None:
    """对给定连接串执行 `alembic upgrade head`。

    `backend/alembic/env.py` 从环境变量 `DATABASE_URL` 读取连接串，
    因此这里临时切换该变量，执行完毕后恢复原值。
    """

    _run_alembic(database_url, "upgrade", "head")


def upgrade_to_revision(database_url: str, revision: str) -> None:
    """对给定连接串执行 `alembic upgrade <revision>`（ENG-09 往返验证用）。"""

    _run_alembic(database_url, "upgrade", revision)


def downgrade_to_revision(database_url: str, revision: str) -> None:
    """对给定连接串执行 `alembic downgrade <revision>`。

    **不吞异常**：降级守卫（0021）拒绝时要把原始错误原样传给用例，
    用例才能断言「拒绝降级且说明恢复条件」。
    """

    _run_alembic(database_url, "downgrade", revision)


def _run_alembic(database_url: str, action: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        config = Config(str(ALEMBIC_INI))
        if action == "upgrade":
            command.upgrade(config, revision)
        else:
            command.downgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def alembic_ini() -> Path:
    """Alembic 配置文件路径，供迁移测试构造 `Config`。"""

    return ALEMBIC_INI


@pytest.fixture(scope="session")
def upgrade_head_at() -> Callable[[str, str], None]:
    """`upgrade` 到指定 revision 的可调用对象。"""

    return upgrade_to_revision


@pytest.fixture(scope="session")
def downgrade_to() -> Callable[[str, str], None]:
    """`downgrade` 到指定 revision 的可调用对象（异常原样上抛）。"""

    return downgrade_to_revision


@pytest.fixture(scope="session")
def upgrade_head() -> Callable[[str], None]:
    """执行 `alembic upgrade head` 的可调用对象。"""

    return upgrade_to_head


@pytest.fixture(scope="session")
def schema_fingerprint_sql() -> TextClause:
    """schema 指纹查询语句。"""

    return SCHEMA_FINGERPRINT_SQL


@pytest.fixture(scope="session")
def database_url() -> str:
    """开发库连接串（来自服务端环境变量）。"""

    return get_database_url()


@pytest.fixture(scope="session")
def maintenance_engine(database_url: str) -> Iterator[Engine]:
    """连接到 `postgres` 维护库，用于 CREATE/DROP DATABASE。"""

    url = make_url(database_url).set(database="postgres")
    engine = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def db_admin(maintenance_engine: Engine, database_url: str) -> DatabaseAdmin:
    return DatabaseAdmin(maintenance_engine, database_url)


@pytest.fixture(scope="session")
def test_database_url(db_admin: DatabaseAdmin) -> Iterator[str]:
    """会话级临时测试库：新建空库 → 迁移到 head → 用例结束后删除。"""

    name = f"eng01_test_{uuid.uuid4().hex[:12]}"
    db_admin.create(name)
    url = db_admin.url_for(name)
    try:
        upgrade_to_head(url)
        yield url
    finally:
        db_admin.drop(name)


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Iterator[Engine]:
    eng = create_engine(test_database_url, poolclass=NullPool)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Connection]:
    """用例级连接：整个用例处于一个事务中，结束即回滚，不残留合成数据。"""

    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def rejected() -> Callable[..., str]:
    """断言某条语句被数据库拒绝，并保持外层事务可用（用 SAVEPOINT 隔离）。"""

    def _rejected(
        connection: Connection,
        statement: str,
        params: dict[str, object] | None = None,
        *,
        contains: str | None = None,
    ) -> str:
        savepoint = connection.begin_nested()
        try:
            with pytest.raises(DBAPIError) as excinfo:
                connection.execute(text(statement), params or {})
        finally:
            if savepoint.is_active:
                savepoint.rollback()
        message = str(excinfo.value.orig)
        if contains is not None:
            assert contains in message, message
        return message

    return _rejected


# --------------------------------------------------------------------------- #
# 合成数据工厂（只写合成值，不使用任何真实用户数据）
# --------------------------------------------------------------------------- #

VALID_USER = {
    "status": "ACTIVE",
    "locale": "zh-CN",
    "timezone": "Asia/Shanghai",
}


@pytest.fixture
def make_user(db: Connection) -> Callable[..., uuid.UUID]:
    def _make_user(**overrides: object) -> uuid.UUID:
        values = {**VALID_USER, **overrides}
        return db.execute(
            text(
                """
                INSERT INTO users (status, locale, timezone, updated_at)
                VALUES (:status, :locale, :timezone, now())
                RETURNING id
                """
            ),
            values,
        ).scalar_one()

    return _make_user


@pytest.fixture
def make_wallet(db: Connection) -> Callable[..., uuid.UUID]:
    def _make_wallet(user_id: uuid.UUID, **overrides: object) -> uuid.UUID:
        values = {
            "user_id": user_id,
            "available_points": overrides.get("available_points", 0),
            "reserved_points": overrides.get("reserved_points", 0),
            "lifetime_credited": overrides.get("lifetime_credited", 0),
            "lifetime_consumed": overrides.get("lifetime_consumed", 0),
        }
        return db.execute(
            text(
                """
                INSERT INTO point_wallets (
                    user_id, available_points, reserved_points,
                    lifetime_credited, lifetime_consumed, updated_at
                )
                VALUES (
                    :user_id, :available_points, :reserved_points,
                    :lifetime_credited, :lifetime_consumed, now()
                )
                RETURNING id
                """
            ),
            values,
        ).scalar_one()

    return _make_wallet


@pytest.fixture
def make_cost_config(db: Connection) -> Callable[..., uuid.UUID]:
    def _make_cost_config(**overrides: object) -> uuid.UUID:
        values = {
            "mode": overrides.get("mode", "PHOTO_PROMPT"),
            "model_name": overrides.get("model_name", "synthetic-model"),
            "output_count": overrides.get("output_count", 2),
            "points_cost": overrides.get("points_cost", 10),
        }
        return db.execute(
            text(
                """
                INSERT INTO generation_cost_configs (
                    mode, model_name, output_count, points_cost,
                    effective_from, status
                )
                VALUES (
                    :mode, :model_name, :output_count, :points_cost,
                    now(), 'ACTIVE'
                )
                RETURNING id
                """
            ),
            values,
        ).scalar_one()

    return _make_cost_config


@pytest.fixture
def make_task(db: Connection) -> Callable[..., uuid.UUID]:
    def _make_task(
        user_id: uuid.UUID,
        cost_config_id: uuid.UUID,
        *,
        status: str = "QUEUED",
        billing_status: str = "RESERVED",
        idempotency_key: str | None = None,
        reserved_points: int = 10,
        parent_task_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        values = {
            "user_id": user_id,
            "cost_config_id": cost_config_id,
            "status": status,
            "billing_status": billing_status,
            "idempotency_key": idempotency_key or f"synthetic-{uuid.uuid4().hex}",
            "reserved_points": reserved_points,
            "parent_task_id": parent_task_id,
        }
        return db.execute(
            text(
                """
                INSERT INTO generation_tasks (
                    user_id, parent_task_id, mode, status, billing_status, prompt,
                    requested_count, cost_config_id, cost_snapshot, reserved_points,
                    idempotency_key, updated_at
                )
                VALUES (
                    :user_id, :parent_task_id, 'PHOTO_PROMPT', :status, :billing_status,
                    'synthetic prompt', 2, :cost_config_id, '{}'::jsonb,
                    :reserved_points, :idempotency_key, now()
                )
                RETURNING id
                """
            ),
            values,
        ).scalar_one()

    return _make_task


@pytest.fixture
def make_ledger_entry(db: Connection) -> Callable[..., uuid.UUID]:
    def _make_ledger_entry(
        wallet_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        entry_type: str,
        amount: int,
        available_delta: int | None = None,
        reserved_delta: int | None = None,
        generation_id: uuid.UUID | None = None,
        order_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
        reason: str = "synthetic",
        metadata: dict[str, object] | None = None,
    ) -> uuid.UUID:
        if available_delta is None or reserved_delta is None:
            standard = {
                "CREDIT": (amount, 0),
                "RESERVE": (-amount, amount),
                "CONSUME": (0, -amount),
                "RELEASE": (amount, -amount),
            }
            default_available, default_reserved = standard.get(
                entry_type, (available_delta or 0, reserved_delta or 0)
            )
            available_delta = default_available if available_delta is None else available_delta
            reserved_delta = default_reserved if reserved_delta is None else reserved_delta
        values = {
            "wallet_id": wallet_id,
            "user_id": user_id,
            "entry_type": entry_type,
            "available_delta": available_delta,
            "reserved_delta": reserved_delta,
            "amount": amount,
            "generation_id": generation_id,
            "order_id": order_id,
            "idempotency_key": idempotency_key or f"synthetic-{uuid.uuid4().hex}",
            "reason": reason,
            "metadata": json.dumps(metadata or {}),
        }
        return db.execute(
            text(
                """
                INSERT INTO point_ledger (
                    wallet_id, user_id, entry_type, available_delta, reserved_delta,
                    amount, generation_id, order_id, idempotency_key, reason, metadata
                )
                VALUES (
                    :wallet_id, :user_id, :entry_type, :available_delta, :reserved_delta,
                    :amount, :generation_id, :order_id, :idempotency_key, :reason,
                    CAST(:metadata AS jsonb)
                )
                RETURNING id
                """
            ),
            values,
        ).scalar_one()

    return _make_ledger_entry


@pytest.fixture
def schema_fingerprint(db: Connection) -> Callable[[], str]:
    def _fingerprint() -> str:
        return db.execute(SCHEMA_FINGERPRINT_SQL).scalar_one()

    return _fingerprint
