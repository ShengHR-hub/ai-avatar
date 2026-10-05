"""ENG-09 返修第 5 项：幂等作用域占用 → 409 `IDEMPOTENCY_IN_PROGRESS` + `Retry-After`。

分两半验证，各自用能给出**确定性**证据的环境：

1. **真实 PostgreSQL**（本文件）：显式持住 `(用户, 幂等键)` 的咨询锁，
   让另一条连接上的 `ShareService.create_share` / `FeedbackService.submit` 等待超时。
   断言产生的是领域信号 `IdempotencyScopeBusy`（而不是通用内部错误），
   并且**没有任何写入**。同时反向断言：**行锁**超时**不会**产生这个信号。

2. **路由与响应映射**（`tests/api/test_share_endpoints.py` /
   `test_feedback_endpoints.py`）：把仓储层的 `lock_idempotency_scope` 换成抛
   `IdempotencyScopeBusy` 的替身，走真实端点断言
   `409` + `code == IDEMPOTENCY_IN_PROGRESS` + `Retry-After: 5`，
   并断言锁释放后同载荷回放、异载荷 409 冲突。

之所以不把 HTTP 端点搬到真实数据库上跑：端点依赖会话 Cookie，而用户查询走的是
全局引擎（`get_uow` 被覆盖也覆盖不到它），在 `tests/db` 的隔离库上无法在不削弱
鉴权的前提下构造。上半段用服务层、下半段用真实路由，两侧合起来覆盖同一条语义。
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.errors.exceptions import ApiError
from app.repositories.engagement import (
    _IDEMPOTENCY_LOCK_NAMESPACE,
    IdempotencyScopeBusy,
)
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.engagement import FeedbackCreateRequest, ShareCreateRequest
from app.services.feedback.service import FeedbackService
from app.services.shares.service import ShareService

NOW = datetime.now(timezone.utc)
CONTENDED_KEY = "synthetic-contended-key-001"


def _lock_key_for(*, scope: str, user_id: uuid.UUID, key: str) -> tuple[int, int]:
    """按实现同一公式推导咨询锁键（复用公式，不复制业务判定）。"""

    material = f"{scope}\x00{user_id}\x00{key}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return (
        _IDEMPOTENCY_LOCK_NAMESPACE[scope],
        int.from_bytes(digest[:4], "big") & 0x7FFFFFFF,
    )


@pytest.fixture
def db_engine(test_database_url: str) -> Iterator[Engine]:
    """本文件专用引擎（`tests/db/conftest.py` 提供已迁移的隔离库 URL）。"""

    engine = create_engine(test_database_url, poolclass=None)
    try:
        yield engine
    finally:
        engine.dispose()


def _factory(engine: Engine):
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
    return lambda: maker()


def _seed(engine: Engine) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """合成用户 + 成功任务 + 两张未到期 READY RESULT 资产。"""

    with engine.connect() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (status, locale, timezone, updated_at)"
                " VALUES ('ACTIVE','zh-CN','UTC', now()) RETURNING id"
            )
        ).scalar_one()
        cost_id = conn.execute(
            text(
                "INSERT INTO generation_cost_configs (id, mode, model_name, output_count,"
                " points_cost, cost_multiplier, effective_from, status, created_at)"
                " VALUES (gen_random_uuid(), 'TEXT_PROMPT', NULL, 2, 10, 1, now(),"
                " 'ACTIVE', now()) RETURNING id"
            )
        ).scalar_one()
        generation_id = conn.execute(
            text(
                "INSERT INTO generation_tasks (id, user_id, mode, status, billing_status,"
                " prompt, requested_count, cost_config_id, cost_snapshot, reserved_points,"
                " idempotency_key, retry_count, created_at, updated_at)"
                " VALUES (gen_random_uuid(), :u, 'TEXT_PROMPT', 'SUCCEEDED', 'CONSUMED',"
                " 'synthetic', 2, :c, '{}'::jsonb, 10, :k, 0, now(), now()) RETURNING id"
            ),
            {"u": user_id, "c": cost_id, "k": f"gen-{uuid.uuid4().hex[:12]}"},
        ).scalar_one()
        asset_ids: list[uuid.UUID] = []
        for index in range(2):
            asset_id = conn.execute(
                text(
                    """
                    INSERT INTO media_assets (id, user_id, asset_type, purpose, status,
                        visibility, storage_provider, storage_key, mime_type, size_bytes,
                        sha256, expires_at, created_at)
                    VALUES (gen_random_uuid(), :u, 'RESULT', 'RESULT', 'READY', 'PRIVATE',
                            'MINIO', :key, 'image/png', 1024, :sha, :exp, now())
                    RETURNING id
                    """
                ),
                {
                    "u": user_id,
                    "key": f"{user_id}/r{index}",
                    "sha": "a" * 64,
                    "exp": NOW + timedelta(days=30),
                },
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO generation_outputs (generation_id, asset_id, output_index,"
                    " created_at) VALUES (:g, :a, :i, now())"
                ),
                {"g": generation_id, "a": asset_id, "i": index},
            )
            asset_ids.append(asset_id)
        conn.commit()
    return user_id, generation_id, asset_ids


def _hold_scope_lock(
    engine: Engine, *, scope: str, user_id: uuid.UUID, key: str
) -> tuple[Session, object]:
    """在独立连接上取得咨询锁并保持不提交。"""

    session = sessionmaker(bind=engine, autoflush=False)()
    namespace, lock_key = _lock_key_for(scope=scope, user_id=user_id, key=key)
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :k)"), {"ns": namespace, "k": lock_key}
    )
    return session, session.connection()


def _cleanup(engine: Engine) -> None:
    """清理本文件派生的行。

    **不删 `audit_logs`**：`0016_append_only_guards` 之后它是只追加表，DELETE 会被
    `v1_reject_append_only_mutation()` 直接拒绝。幂等登记的审计行按设计永久保留
    （`02-database-design.md` 第 4 节：不物理删除业务历史）。
    """

    with engine.connect() as conn:
        conn.execute(text("DELETE FROM share_assets"))
        conn.execute(text("DELETE FROM shares"))
        conn.execute(text("DELETE FROM feedback"))
        conn.execute(text("DELETE FROM generation_outputs"))
        conn.execute(text("DELETE FROM generation_tasks"))
        conn.execute(text("DELETE FROM generation_cost_configs"))
        conn.execute(text("DELETE FROM media_assets"))
        conn.execute(text("DELETE FROM users"))
        conn.commit()


def test_share_creation_signals_scope_busy_under_real_lock_contention(
    db_engine: Engine,
) -> None:
    """真实咨询锁占用 → `IdempotencyScopeBusy`，且**零写入**。"""

    try:
        user_id, generation_id, asset_ids = _seed(db_engine)
        holder, connection = _hold_scope_lock(
            db_engine, scope="share", user_id=user_id, key=CONTENDED_KEY
        )
        try:
            service = ShareService(uow=SqlAlchemyUnitOfWork(_factory(db_engine)))
            # 服务层把领域信号转成 409 IDEMPOTENCY_IN_PROGRESS + Retry-After；
            # 这才是调用方（路由/客户端）看到的语义
            with pytest.raises(ApiError) as excinfo:
                with service._uow:
                    service.create_share(
                        user_id=user_id,
                        payload=ShareCreateRequest(
                            generation_id=generation_id, output_asset_ids=asset_ids
                        ),
                        idempotency_key=CONTENDED_KEY,
                    )
        finally:
            connection.close()
            holder.close()

        assert excinfo.value.code.value == "IDEMPOTENCY_IN_PROGRESS"
        assert excinfo.value.headers.get("Retry-After") == "5"
        with db_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM shares")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM share_assets")).scalar_one() == 0
            assert (
                conn.execute(
                    text("SELECT count(*) FROM media_assets WHERE visibility = 'SHARED'")
                ).scalar_one()
                == 0
            )
    finally:
        _cleanup(db_engine)


def test_feedback_signals_scope_busy_under_real_lock_contention(db_engine: Engine) -> None:
    """反馈侧同一机制、不同作用域名：同样产生领域信号且零写入。"""

    try:
        user_id, generation_id, _asset_ids = _seed(db_engine)
        holder, connection = _hold_scope_lock(
            db_engine, scope="feedback", user_id=user_id, key=CONTENDED_KEY
        )
        try:
            service = FeedbackService(uow=SqlAlchemyUnitOfWork(_factory(db_engine)))
            with pytest.raises(ApiError) as excinfo:
                with service._uow:
                    service.submit(
                        user_id=user_id,
                        payload=FeedbackCreateRequest(
                            generation_id=generation_id,
                            rating=5,
                            category="画面质量",
                            content=None,
                        ),
                        idempotency_key=CONTENDED_KEY,
                    )
        finally:
            connection.close()
            holder.close()

        assert excinfo.value.code.value == "IDEMPOTENCY_IN_PROGRESS"
        assert excinfo.value.headers.get("Retry-After") == "5"
        with db_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM feedback")).scalar_one() == 0
    finally:
        _cleanup(db_engine)


def test_scope_lock_is_released_with_the_transaction(db_engine: Engine) -> None:
    """锁随事务结束释放：占用方回滚后，同一请求立刻可以走完整路径。"""

    try:
        user_id, generation_id, asset_ids = _seed(db_engine)
        holder, connection = _hold_scope_lock(
            db_engine, scope="share", user_id=user_id, key=CONTENDED_KEY
        )
        blocked_service = ShareService(uow=SqlAlchemyUnitOfWork(_factory(db_engine)))
        with pytest.raises(ApiError):
            with blocked_service._uow:
                blocked_service.create_share(
                    user_id=user_id,
                    payload=ShareCreateRequest(
                        generation_id=generation_id, output_asset_ids=asset_ids
                    ),
                    idempotency_key=CONTENDED_KEY,
                )
        connection.close()
        holder.close()

        # 锁释放后：同载荷成功，再提交一次**回放**同一条分享
        service = ShareService(uow=SqlAlchemyUnitOfWork(_factory(db_engine)))
        with service._uow:
            first = service.create_share(
                user_id=user_id,
                payload=ShareCreateRequest(
                    generation_id=generation_id, output_asset_ids=asset_ids
                ),
                idempotency_key=CONTENDED_KEY,
            )
        replay_service = ShareService(uow=SqlAlchemyUnitOfWork(_factory(db_engine)))
        with replay_service._uow:
            replay = replay_service.create_share(
                user_id=user_id,
                payload=ShareCreateRequest(
                    generation_id=generation_id, output_asset_ids=asset_ids
                ),
                idempotency_key=CONTENDED_KEY,
            )
        assert replay.slug == first.slug

        # 异载荷 → 稳定冲突（既有语义不变）
        other_generation = _seed(db_engine)[1]
        conflict_service = ShareService(uow=SqlAlchemyUnitOfWork(_factory(db_engine)))
        with pytest.raises(ApiError) as excinfo:
            with conflict_service._uow:
                conflict_service.create_share(
                    user_id=user_id,
                    payload=ShareCreateRequest(
                        generation_id=other_generation, output_asset_ids=asset_ids
                    ),
                    idempotency_key=CONTENDED_KEY,
                )
        assert excinfo.value.code.value == "IDEMPOTENCY_CONFLICT"
    finally:
        _cleanup(db_engine)


def test_row_lock_timeout_is_not_reported_as_scope_busy(db_engine: Engine) -> None:
    """**行锁**超时不是幂等作用域占用：必须抛出数据库错误，不能被吸收成领域信号。

    这是「不要把所有数据库异常、行锁超时或死锁一概映射成幂等处理中」的可执行证据。
    """

    try:
        user_id, generation_id, asset_ids = _seed(db_engine)
        holder = sessionmaker(bind=db_engine, autoflush=False)()
        holder.execute(
            text("SELECT id FROM media_assets WHERE id = :a FOR UPDATE"),
            {"a": asset_ids[0]},
        )
        try:
            service = ShareService(uow=SqlAlchemyUnitOfWork(_factory(db_engine)))
            with pytest.raises(DBAPIError):
                with service._uow:
                    # 把行锁等待压到 100ms，让用例快速收敛
                    service._uow.session.execute(
                        text("SELECT set_config('lock_timeout', '100ms', true)")
                    )
                    service.create_share(
                        user_id=user_id,
                        payload=ShareCreateRequest(
                            generation_id=generation_id, output_asset_ids=asset_ids
                        ),
                        idempotency_key="row-lock-key",
                    )
        finally:
            holder.rollback()
            holder.close()

        with db_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM shares")).scalar_one() == 0
    finally:
        _cleanup(db_engine)


def test_two_connections_contending_on_the_same_scope_serialise(db_engine: Engine) -> None:
    """两个连接抢同一个作用域：一个成功、另一个要么成功回放、要么明确失败。

    这里证明的是「锁真的跨连接生效」，而不是同一 Session 内的自我串行化。
    """

    try:
        user_id, generation_id, asset_ids = _seed(db_engine)
        barrier = threading.Barrier(2)
        results: list[str] = []
        lock = threading.Lock()

        def _worker() -> None:
            service = ShareService(uow=SqlAlchemyUnitOfWork(_factory(db_engine)))
            barrier.wait(timeout=10)
            try:
                with service._uow:
                    view = service.create_share(
                        user_id=user_id,
                        payload=ShareCreateRequest(
                            generation_id=generation_id, output_asset_ids=asset_ids
                        ),
                        idempotency_key=CONTENDED_KEY,
                    )
                with lock:
                    results.append(f"ok:{view.slug}")
            except IdempotencyScopeBusy:
                with lock:
                    results.append("busy")
            except ApiError as error:
                with lock:
                    results.append(f"error:{error.code.value}")

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: _worker(), range(2)))

        assert len(results) == 2, results
        successes = [item for item in results if item.startswith("ok:")]
        # 至少一个成功；两个都成功时必须回放同一条
        assert successes, results
        assert len({item.split(":", 1)[1] for item in successes}) == 1, results
        assert all(
            item.startswith("ok:") or item in {"busy"} or item.startswith("error:")
            for item in results
        ), results
    finally:
        _cleanup(db_engine)
