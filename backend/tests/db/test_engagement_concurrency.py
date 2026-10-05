"""ENG-09 真实 PostgreSQL 并发与生命周期测试（返修项）。

为什么必须有这一组：内存替身没有行锁、没有咨询锁、没有唯一约束的真实裁决，
也没有「事务提交后另一个连接能不能看见」的语义。返修要求的三件事只能在这里证明：

1. **反馈幂等的数据库级并发保障**：同一 `(用户, 键)` 跨任务并发提交，
   咨询锁让两个请求串行，最终**恰好一条反馈**、另一条稳定 409；
2. **分享写入的并发**：同一键并发只有一条分享落库；换选中集合的并发请求稳定冲突，
   不会出现两条 `shares` 或半套 `share_assets`；
3. **清理与分享写入的并发锁策略 + 单次删除**：并发跑两轮清理，每个到期对象的
   `delete` 只发生一次（`SELECT ... FOR UPDATE` 让第二个执行者看不到已处理的行）。

并发用例在**独立连接**上各自提交，结束后自行清理派生行；不触碰共享库数据，
也不依赖外层用例事务（那样别的连接看不见）。
"""

from __future__ import annotations

import itertools
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.errors.exceptions import ApiError
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.engagement import FeedbackCreateRequest, ShareCreateRequest
from app.services.feedback.service import FeedbackService
from app.services.shares.lifecycle import MediaLifecycleService
from app.services.shares.service import ShareService

BACKEND_DIR = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

#: `generation_cost_configs` 的唯一键是 `(mode, model_name, output_count, effective_from)`
#: 且 `NULLS NOT DISTINCT`，因此同一用例里造多条配置必须让 `effective_from` 各不相同。
_CONFIG_SEQ = itertools.count()


def _next_effective_from() -> datetime:
    return NOW + timedelta(seconds=next(_CONFIG_SEQ))


# --------------------------------------------------------------------------- #
# 夹具与合成数据（全部提交，别的连接才看得见）
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_engine(test_database_url: str) -> Iterator[Engine]:
    engine = create_engine(test_database_url, poolclass=None)
    try:
        yield engine
    finally:
        engine.dispose()


class ClosedSessionTracker:
    """记录会话工厂建出的 Session，并**直接观测 `close()` 是否被调用**。

    `Session.is_active` 只表示"当前没有进行中的事务"，把 Session 泄漏在
    `autobegin` 状态时它也返回 False，因此不能用来证明「Worker 关掉了 UoW」。
    这里包装 `close()` 计数，才是对返修项「正确进入并关闭」的真实断言。
    """

    def __init__(self, engine: Engine) -> None:
        self._maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
        self.sessions: list[Session] = []
        self.closed: list[Session] = []
        self.rollbacks: list[Session] = []

    def __call__(self) -> Session:
        session = self._maker()
        self._wrap(session, "close", self.closed)
        self._wrap(session, "rollback", self.rollbacks)
        self.sessions.append(session)
        return session

    @staticmethod
    def _wrap(session: Session, name: str, sink: list[Session]) -> None:
        original = getattr(session, name)

        def _recorded(*args: Any, **kwargs: Any):
            sink.append(session)
            return original(*args, **kwargs)

        setattr(session, name, _recorded)

    @property
    def all_closed(self) -> bool:
        return bool(self.sessions) and len(self.closed) == len(self.sessions)


def _factory(engine: Engine, tracker: ClosedSessionTracker | None = None):
    """会话工厂；传入 tracker 时记录每个建出来的 Session。"""

    if tracker is not None:
        return tracker
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
    return maker


def _uow(engine: Engine, tracker: ClosedSessionTracker | None = None):
    factory = _factory(engine, tracker)
    return lambda: SqlAlchemyUnitOfWork(factory)


def _seed_user(engine: Engine) -> uuid.UUID:
    with engine.connect() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (status, locale, timezone, updated_at)"
                " VALUES ('ACTIVE','zh-CN','UTC', now()) RETURNING id"
            )
        ).scalar_one()
        conn.commit()
    return user_id


class EngineStorage:
    """最小 `StorageAdapter`：只实现生命周期用到的 `delete`，并统计调用。"""

    def __init__(self, *, failing: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self._failing = set(failing or ())
        self._lock = threading.Lock()

    def delete(self, key: str) -> None:
        from app.storage import StorageUnavailable

        with self._lock:
            self.deleted.append(key)
        if key in self._failing:
            raise StorageUnavailable("synthetic unavailable")


def _seed_asset(
    engine: Engine,
    *,
    user_id: uuid.UUID,
    key: str,
    expires_at: datetime,
    status: str = "READY",
) -> uuid.UUID:
    with engine.connect() as conn:
        asset_id = conn.execute(
            text(
                """
                INSERT INTO media_assets (id, user_id, asset_type, purpose, status,
                    visibility, storage_provider, storage_key, mime_type, size_bytes,
                    sha256, expires_at, created_at)
                VALUES (gen_random_uuid(), :user_id, 'RESULT', 'RESULT', :status,
                        'PRIVATE', 'MINIO', :key, 'image/png', 1024, :sha, :expires_at,
                        now())
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "status": status,
                "key": key,
                "sha": "a" * 64,
                "expires_at": expires_at,
            },
        ).scalar_one()
        conn.commit()
    return asset_id


def _seed_generation(
    engine: Engine,
    *,
    user_id: uuid.UUID,
    status: str,
    assets: tuple[uuid.UUID, ...] = (),
) -> uuid.UUID:
    with engine.connect() as conn:
        cost_id = conn.execute(
            text(
                "INSERT INTO generation_cost_configs (id, mode, model_name, output_count,"
                " points_cost, cost_multiplier, effective_from, status, created_at)"
                " VALUES (gen_random_uuid(), 'TEXT_PROMPT', NULL, 2, 10, 1, :now,"
                " 'ACTIVE', :now) RETURNING id"
            ),
            {"now": _next_effective_from()},
        ).scalar_one()
        generation_id = conn.execute(
            text(
                """
                INSERT INTO generation_tasks (id, user_id, mode, status, billing_status,
                    prompt, requested_count, cost_config_id, cost_snapshot,
                    reserved_points, idempotency_key, retry_count, created_at, updated_at)
                VALUES (gen_random_uuid(), :user_id, 'TEXT_PROMPT', :status, 'CONSUMED',
                        'synthetic', 2, :cost_id, '{}'::jsonb, 10, :idempotency_key, 0, now(), now())
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "status": status,
                "cost_id": cost_id,
                "idempotency_key": f"gen-{uuid.uuid4().hex[:12]}",
            },
        ).scalar_one()
        for index, asset_id in enumerate(assets):
            conn.execute(
                text(
                    "INSERT INTO generation_outputs (generation_id, asset_id, output_index,"
                    " created_at) VALUES (:generation_id, :asset_id, :index, now())"
                ),
                {"generation_id": generation_id, "asset_id": asset_id, "index": index},
            )
        conn.commit()
    return generation_id


def _cleanup(engine: Engine, user_id: uuid.UUID) -> None:
    """删除本用例派生的行（按外键顺序）。"""

    with engine.connect() as conn:
        conn.execute(
            text(
                "DELETE FROM share_assets WHERE share_id IN "
                "(SELECT id FROM shares WHERE user_id = :u)"
            ),
            {"u": user_id},
        )
        conn.execute(text("DELETE FROM shares WHERE user_id = :u"), {"u": user_id})
        conn.execute(text("DELETE FROM feedback WHERE user_id = :u"), {"u": user_id})
        conn.execute(
            text(
                "DELETE FROM generation_outputs WHERE generation_id IN "
                "(SELECT id FROM generation_tasks WHERE user_id = :u)"
            ),
            {"u": user_id},
        )
        conn.execute(
            text("DELETE FROM generation_tasks WHERE user_id = :u"), {"u": user_id}
        )
        conn.execute(
            text(
                "DELETE FROM generation_cost_configs WHERE id NOT IN "
                "(SELECT cost_config_id FROM generation_tasks)"
            )
        )
        conn.execute(text("DELETE FROM media_assets WHERE user_id = :u"), {"u": user_id})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        conn.commit()


# --------------------------------------------------------------------------- #
# 1. 反馈：同一键跨任务并发，恰好一条
# --------------------------------------------------------------------------- #
def _concurrent_feedback(
    engine: Engine,
    *,
    user_id: uuid.UUID,
    generation_id: uuid.UUID,
    key: str,
    barrier: threading.Barrier,
) -> str:
    service = FeedbackService(uow=_uow(engine)())
    with service._uow:
        barrier.wait()
        try:
            row = service.submit(
                user_id=user_id,
                payload=FeedbackCreateRequest(
                    generation_id=generation_id, rating=5, category="并发", content=None
                ),
                idempotency_key=key,
            )
            return f"created:{row.id}"
        except ApiError as error:
            return f"error:{error.code.value}"


def test_concurrent_feedback_with_same_key_across_tasks_yields_exactly_one(
    isolated_engine: Engine,
) -> None:
    """同一键并发提交到**两个不同任务**：咨询锁串行化，恰好一条反馈。"""

    user_id = _seed_user(isolated_engine)
    first = _seed_generation(isolated_engine, user_id=user_id, status="SUCCEEDED", assets=())
    second = _seed_generation(isolated_engine, user_id=user_id, status="FAILED", assets=())
    barrier = threading.Barrier(2)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda generation_id: _concurrent_feedback(
                        isolated_engine,
                        user_id=user_id,
                        generation_id=generation_id,
                        key="shared-across-tasks",
                        barrier=barrier,
                    ),
                    (first, second),
                )
            )

        created = [item for item in results if item.startswith("created:")]
        conflicted = [item for item in results if item == "error:IDEMPOTENCY_CONFLICT"]
        assert len(created) == 1, results
        assert len(conflicted) == 1, results

        with isolated_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT count(*) FROM feedback WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()
        assert rows == 1
    finally:
        _cleanup(isolated_engine, user_id)

def test_concurrent_feedback_same_task_creates_one_row_and_replays(
    isolated_engine: Engine,
) -> None:
    """同一键并发提交到**同一个任务**：一条落库，另一个请求回放同一行。"""

    user_id = _seed_user(isolated_engine)
    generation_id = _seed_generation(
        isolated_engine, user_id=user_id, status="SUCCEEDED", assets=()
    )
    barrier = threading.Barrier(2)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: _concurrent_feedback(
                        isolated_engine,
                        user_id=user_id,
                        generation_id=generation_id,
                        key="same-task",
                        barrier=barrier,
                    ),
                    range(2),
                )
            )

        ids = {item.split(":", 1)[1] for item in results if item.startswith("created:")}
        assert all(item.startswith("created:") for item in results), results
        assert len(ids) == 1, results

        with isolated_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT count(*) FROM feedback WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()
        assert rows == 1
    finally:
        _cleanup(isolated_engine, user_id)
# --------------------------------------------------------------------------- #
# 2. 分享：并发写入
# --------------------------------------------------------------------------- #
def _concurrent_share(
    engine: Engine,
    *,
    user_id: uuid.UUID,
    generation_id: uuid.UUID,
    asset_ids: list[uuid.UUID],
    key: str,
    barrier: threading.Barrier,
) -> str:
    service = ShareService(uow=_uow(engine)())
    with service._uow:
        barrier.wait()
        try:
            view = service.create_share(
                user_id=user_id,
                payload=ShareCreateRequest(
                    generation_id=generation_id, output_asset_ids=asset_ids
                ),
                idempotency_key=key,
            )
            return f"ok:{view.slug}"
        except ApiError as error:
            return f"error:{error.code.value}"


def test_concurrent_share_same_key_across_generations_yields_exactly_one(
    isolated_engine: Engine,
) -> None:
    """同一键并发分享两个不同任务：恰好一条分享，另一条 409。"""

    user_id = _seed_user(isolated_engine)
    first_asset = _seed_asset(
        isolated_engine, user_id=user_id, key=f"{user_id}/a1", expires_at=NOW + timedelta(days=30)
    )
    second_asset = _seed_asset(
        isolated_engine, user_id=user_id, key=f"{user_id}/a2", expires_at=NOW + timedelta(days=30)
    )
    first = _seed_generation(
        isolated_engine, user_id=user_id, status="SUCCEEDED", assets=(first_asset,)
    )
    second = _seed_generation(
        isolated_engine, user_id=user_id, status="SUCCEEDED", assets=(second_asset,)
    )
    barrier = threading.Barrier(2)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: _concurrent_share(
                        isolated_engine,
                        user_id=user_id,
                        generation_id=pair[0],
                        asset_ids=[pair[1]],
                        key="shared-share-key",
                        barrier=barrier,
                    ),
                    ((first, first_asset), (second, second_asset)),
                )
            )

        created = [item for item in results if item.startswith("ok:")]
        conflicted = [item for item in results if item == "error:IDEMPOTENCY_CONFLICT"]
        assert len(created) == 1, results
        assert len(conflicted) == 1, results

        with isolated_engine.connect() as conn:
            shares = conn.execute(
                text("SELECT count(*) FROM shares WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()
            links = conn.execute(
                text(
                    "SELECT count(*) FROM share_assets WHERE share_id IN "
                    "(SELECT id FROM shares WHERE user_id = :u)"
                ),
                {"u": user_id},
            ).scalar_one()
        assert (shares, links) == (1, 1)
    finally:
        _cleanup(isolated_engine, user_id)


def test_concurrent_share_same_key_same_payload_replays_the_winner(
    isolated_engine: Engine,
) -> None:
    """同一键、同一载荷并发：**两个响应都成功**，且拿到同一条分享（输家回放赢家）。

    断言必须覆盖「两个都成功」：只检查成功 slug 集合大小会漏掉
    「一个成功、一个 409」这种被错判成冲突的实现。
    """

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine, user_id=user_id, key=f"{user_id}/only", expires_at=NOW + timedelta(days=30)
    )
    generation_id = _seed_generation(
        isolated_engine, user_id=user_id, status="SUCCEEDED", assets=(asset_id,)
    )
    barrier = threading.Barrier(2)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: _concurrent_share(
                        isolated_engine,
                        user_id=user_id,
                        generation_id=generation_id,
                        asset_ids=[asset_id],
                        key="same-payload-key",
                        barrier=barrier,
                    ),
                    range(2),
                )
            )

        # 两个请求都必须成功（谁都不是输家冲突），且 slug 完全一致
        assert all(item.startswith("ok:") for item in results), results
        slugs = {item.split(":", 1)[1] for item in results}
        assert len(slugs) == 1, results

        with isolated_engine.connect() as conn:
            shares = conn.execute(
                text("SELECT count(*) FROM shares WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()
            links = conn.execute(
                text(
                    "SELECT count(*) FROM share_assets WHERE share_id IN "
                    "(SELECT id FROM shares WHERE user_id = :u)"
                ),
                {"u": user_id},
            ).scalar_one()
        assert (shares, links) == (1, 1)
    finally:
        _cleanup(isolated_engine, user_id)


# --------------------------------------------------------------------------- #
# 3. 清理：并发只删一次 + 工作单元被关闭
# --------------------------------------------------------------------------- #
def test_concurrent_cleanup_deletes_each_expired_object_once(
    isolated_engine: Engine,
) -> None:
    """并发两轮清理同一批到期资产：每个对象只被删除一次（行锁串行化）。"""

    user_id = _seed_user(isolated_engine)
    keys = [f"{user_id}/exp-{index}" for index in range(3)]
    for key in keys:
        _seed_asset(
            isolated_engine,
            user_id=user_id,
            key=key,
            expires_at=NOW - timedelta(days=1),
        )

    storage = EngineStorage()
    tracker = ClosedSessionTracker(isolated_engine)
    barrier = threading.Barrier(2)

    def _run() -> dict[str, int]:
        service = MediaLifecycleService(
            uow_factory=_uow(isolated_engine, tracker), storage=storage, clock=lambda: NOW
        )
        barrier.wait()
        return service.run_once().as_dict()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda _: _run(), range(2)))

        assert sorted(storage.deleted) == sorted(keys), storage.deleted
        assert sum(report["assets_expired"] for report in reports) == len(keys)
        # 每个 Session 都被关闭（返修项：Worker 必须正确进入并关闭 UoW）
        assert tracker.all_closed, len(tracker.closed)

        with isolated_engine.connect() as conn:
            statuses = conn.execute(
                text("SELECT status FROM media_assets WHERE user_id = :u"), {"u": user_id}
            ).scalars()
        assert sorted(statuses) == ["EXPIRED"] * len(keys)
    finally:
        _cleanup(isolated_engine, user_id)


def test_cleanup_rolls_back_and_closes_when_share_close_fails(
    isolated_engine: Engine,
) -> None:
    """批量中途出现**真实**异常：工作单元回滚并关闭，状态改写被撤销。

    说明：`_delete_object` 对单项存储异常是**故意吞掉**的（返修要求「单项失败不阻塞
    整批」），因此这里注入的是状态写入口的异常（不属于被吞掉的那一类），
    才能覆盖「异常 → rollback → close」这条路径。
    """

    from app.repositories.media import SqlAlchemyMediaAssetRepository

    user_id = _seed_user(isolated_engine)
    key = f"{user_id}/boom"
    _seed_asset(
        isolated_engine, user_id=user_id, key=key, expires_at=NOW - timedelta(days=1)
    )

    tracker = ClosedSessionTracker(isolated_engine)
    storage = EngineStorage()
    service = MediaLifecycleService(
        uow_factory=_uow(isolated_engine, tracker),
        storage=storage,
        clock=lambda: NOW,
    )

    original = SqlAlchemyMediaAssetRepository.set_status

    def exploding_set_status(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic failure while writing the new status")

    SqlAlchemyMediaAssetRepository.set_status = exploding_set_status  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            service.run_once()

        # 对象已经被删（这一步在异常之前完成），但状态必须仍是 READY：
        # 事务回滚把中间态留在安全的一侧（下批重试），不会出现「状态已改而对象还在」
        assert storage.deleted == [key]
        assert tracker.all_closed, len(tracker.closed)
        assert tracker.rollbacks, "异常路径必须显式 rollback"
        with isolated_engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM media_assets WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()
        assert status == "READY"
    finally:
        SqlAlchemyMediaAssetRepository.set_status = original  # type: ignore[method-assign]
        _cleanup(isolated_engine, user_id)


def test_cleanup_batch_size_is_capped_at_fifty(isolated_engine: Engine) -> None:
    """批量上限是硬约束：单轮最多 50 条，剩余留给后续轮次。

    少于 51 条到期资产是证明不了上限的（那只能证明「都处理了」）：
    这里造 51 条，断言第一轮恰好 50、第二轮处理剩下 1 条。
    """

    user_id = _seed_user(isolated_engine)
    total = 51
    for index in range(total):
        _seed_asset(
            isolated_engine,
            user_id=user_id,
            key=f"{user_id}/cap-{index}",
            expires_at=NOW - timedelta(days=1),
        )

    storage = EngineStorage()
    service = MediaLifecycleService(
        uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
    )

    assert service.batch_size == 50
    try:
        first = service.run_once(batch_size=500)  # 传更大的值也只会处理 50 条
        second = service.run_once()
        third = service.run_once()

        assert first.assets_expired == 50, first
        assert second.assets_expired == 1, second
        assert third.assets_expired == 0, third
        assert len(storage.deleted) == total
        assert len(set(storage.deleted)) == total, "同一个对象不得被删除两次"
        assert service._bounded(500) == 50
        assert service._bounded(None) == 50
    finally:
        _cleanup(isolated_engine, user_id)


# --------------------------------------------------------------------------- #
# 4. 返修：锁顺序、关闭可见性、幂等作用域占用
# --------------------------------------------------------------------------- #
def _seed_share_for_asset(
    engine: Engine, *, user_id: uuid.UUID, asset_ids: tuple[uuid.UUID, ...], key: str
) -> str:
    """在独立连接上为这些资产建一条真实分享，返回 slug。"""

    generation_id = _seed_generation(
        engine, user_id=user_id, status="SUCCEEDED", assets=asset_ids
    )
    service = ShareService(uow=_uow(engine)())
    with service._uow:
        view = service.create_share(
            user_id=user_id,
            payload=ShareCreateRequest(
                generation_id=generation_id, output_asset_ids=list(asset_ids)
            ),
            idempotency_key=key,
        )
    return view.slug


def _share_state(engine: Engine, slug: str) -> tuple[str, int]:
    """(分享状态, 该分享关联的资产数)。"""

    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM shares WHERE slug = :s"), {"s": slug}
        ).scalar_one()
        links = conn.execute(
            text(
                "SELECT count(*) FROM share_assets WHERE share_id = "
                "(SELECT id FROM shares WHERE slug = :s)"
            ),
            {"s": slug},
        ).scalar_one()
    return status, links


def _visibility(engine: Engine, asset_id: uuid.UUID) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT visibility FROM media_assets WHERE id = :a"), {"a": asset_id}
        ).scalar_one()


def _active_shares_for_asset(engine: Engine, asset_id: uuid.UUID) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM shares s JOIN share_assets sa ON sa.share_id = s.id "
                "WHERE sa.asset_id = :a AND s.status = 'ACTIVE' "
                "AND (s.expires_at IS NULL OR s.expires_at > now())"
            ),
            {"a": asset_id},
        ).scalar_one()


def test_lock_helper_refuses_unsorted_input(isolated_engine: Engine) -> None:
    """`lock_assets_by_id` 拒绝未排序输入：取锁顺序必须是全聚合唯一的。"""

    from app.repositories.unit_of_work import SqlAlchemyUnitOfWork

    with SqlAlchemyUnitOfWork(_factory(isolated_engine)) as uow:
        high = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
        low = uuid.UUID("00000000-0000-4000-8000-000000000000")
        with pytest.raises(ValueError):
            uow.shares.lock_assets_by_id([high, low])
        # 升序输入正常返回（行不存在也只是空结果，不报错）
        assert uow.shares.lock_assets_by_id([low, high]) == ()


def test_concurrent_creation_with_reversed_arrays_does_not_deadlock(
    isolated_engine: Engine,
) -> None:
    """`[A,B]` 与 `[B,A]` 并发创建：两个都成功，且不死锁。"""

    user_id = _seed_user(isolated_engine)
    first = _seed_asset(
        isolated_engine, user_id=user_id, key=f"{user_id}/pair-1", expires_at=NOW + timedelta(days=30)
    )
    second = _seed_asset(
        isolated_engine, user_id=user_id, key=f"{user_id}/pair-2", expires_at=NOW + timedelta(days=30)
    )
    generation_id = _seed_generation(
        isolated_engine, user_id=user_id, status="SUCCEEDED", assets=(first, second)
    )
    barrier = threading.Barrier(2)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: _concurrent_share(
                        isolated_engine,
                        user_id=user_id,
                        generation_id=generation_id,
                        asset_ids=list(pair[1]),
                        key=pair[0],
                        barrier=barrier,
                    ),
                    (("order-ab", (first, second)), ("order-ba", (second, first))),
                )
            )

        assert all(item.startswith("ok:") for item in results), results
        slugs = sorted(item.split(":", 1)[1] for item in results)
        assert len(set(slugs)) == 2, "两个意图必须各建一条分享"
        for slug in slugs:
            status, links = _share_state(isolated_engine, slug)
            assert (status, links) == ("ACTIVE", 2)

        # 两张都因为「有有效分享」而保持 SHARED
        assert _visibility(isolated_engine, first) == "SHARED"
        assert _visibility(isolated_engine, second) == "SHARED"
        with isolated_engine.connect() as conn:
            shares = conn.execute(
                text("SELECT count(*) FROM shares WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()
        assert shares == 2
    finally:
        _cleanup(isolated_engine, user_id)


def test_creation_held_open_then_last_share_revoked_keeps_asset_shared(
    isolated_engine: Engine,
) -> None:
    """并发「新建分享」与「撤销最后一条旧分享」：不得留下有效分享+PRIVATE。

    这是返修第 3 项要证明的核心交错：新建事务**已经拿到资产行锁但尚未提交**时，
    撤销事务去关闭旧分享。撤销必须等锁，然后在**刷新后的数据库事实**上判断
    「还有其他有效分享」，从而把资产维持在 `SHARED`。
    没有资产行锁（或该判断读的是陈旧快照）时，撤销会把一张仍有有效分享的资产
    错改成 `PRIVATE`（匿名读取随即 404，但链接仍然 ACTIVE）。
    """

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine, user_id=user_id, key=f"{user_id}/race", expires_at=NOW + timedelta(days=30)
    )
    generation_id = _seed_generation(
        isolated_engine, user_id=user_id, status="SUCCEEDED", assets=(asset_id,)
    )
    old_slug = _seed_share_for_asset(
        isolated_engine, user_id=user_id, asset_ids=(asset_id,), key="old-share"
    )
    assert _visibility(isolated_engine, asset_id) == "SHARED"

    create_locked = threading.Event()
    release_create = threading.Event()
    outcome: dict[str, str] = {}

    def _creator() -> None:
        service = ShareService(uow=_uow(isolated_engine)())
        with service._uow:
            # 手动走一遍并停在「持锁未提交」的位置：先锁资产行，再通知对端
            service._uow.shares.lock_assets_by_id([asset_id])
            create_locked.set()
            release_create.wait(timeout=10)
            try:
                view = service.create_share(
                    user_id=user_id,
                    payload=ShareCreateRequest(
                        generation_id=generation_id, output_asset_ids=[asset_id]
                    ),
                    idempotency_key="new-share",
                )
                outcome["create"] = f"ok:{view.slug}"
            except ApiError as error:
                outcome["create"] = f"error:{error.code.value}"

    def _revoker() -> None:
        create_locked.wait(timeout=10)
        service = ShareService(uow=_uow(isolated_engine)())
        with service._uow:
            share = service._uow.shares.get_by_slug(old_slug)
            try:
                service.revoke_share(user_id=user_id, share_id=share.id)
                outcome["revoke"] = "ok"
            except ApiError as error:
                outcome["revoke"] = f"error:{error.code.value}"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            revoke_future = pool.submit(_revoker)
            create_future = pool.submit(_creator)
            # 让撤销先阻塞在资产行锁上，再放行创建
            assert create_locked.wait(timeout=10)
            release_create.set()
            revoke_future.result(timeout=30)
            create_future.result(timeout=30)

        assert outcome == {"create": outcome["create"], "revoke": "ok"}, outcome
        assert outcome["create"].startswith("ok:"), outcome

        # 不变式：有效分享在，资产就必须是 SHARED；不能出现「有效分享 + PRIVATE」
        assert _active_shares_for_asset(isolated_engine, asset_id) == 1
        assert _visibility(isolated_engine, asset_id) == "SHARED", outcome
        assert _share_state(isolated_engine, old_slug)[0] == "REVOKED"
    finally:
        _cleanup(isolated_engine, user_id)


def test_cleanup_and_multi_image_convergence_keep_invariants(
    isolated_engine: Engine,
) -> None:
    """媒体清理与多图分享收敛并发：不留「有效分享+PRIVATE」或「无有效分享+SHARED」。

    资产 A 已到期待清理；资产 B 被 A、C 两条分享同时选中（多图分享）。
    并发跑一轮清理和一次撤销：最终必须满足
    「SHARED ⇔ 至少有一条 ACTIVE 未过期分享选中它」。
    """

    user_id = _seed_user(isolated_engine)
    asset_a = _seed_asset(
        isolated_engine, user_id=user_id, key=f"{user_id}/exp-a", expires_at=NOW - timedelta(days=1)
    )
    asset_b = _seed_asset(
        isolated_engine, user_id=user_id, key=f"{user_id}/live-b", expires_at=NOW + timedelta(days=30)
    )
    slug_multi = _seed_share_for_asset(
        isolated_engine, user_id=user_id, asset_ids=(asset_a, asset_b), key="multi-share"
    )
    slug_other = _seed_share_for_asset(
        isolated_engine, user_id=user_id, asset_ids=(asset_b,), key="other-share"
    )
    assert _visibility(isolated_engine, asset_a) == "SHARED"
    assert _visibility(isolated_engine, asset_b) == "SHARED"

    barrier = threading.Barrier(2)
    storage = EngineStorage()

    def _cleanup_run() -> dict[str, int]:
        service = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        )
        barrier.wait()
        return service.run_once().as_dict()

    def _revoke_run() -> str:
        service = ShareService(uow=_uow(isolated_engine)())
        barrier.wait()
        with service._uow:
            share = service._uow.shares.get_by_slug(slug_other)
            service.revoke_share(user_id=user_id, share_id=share.id)
        return "ok"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup_future = pool.submit(_cleanup_run)
            revoke_future = pool.submit(_revoke_run)
            report = cleanup_future.result(timeout=30)
            assert revoke_future.result(timeout=30) == "ok"

        assert report["assets_expired"] >= 1
        assert _share_state(isolated_engine, slug_multi)[0] == "EXPIRED"
        assert _share_state(isolated_engine, slug_other)[0] == "REVOKED"

        # 不变式（对两张资产都成立）
        for asset_id in (asset_a, asset_b):
            active = _active_shares_for_asset(isolated_engine, asset_id)
            visibility = _visibility(isolated_engine, asset_id)
            if active > 0:
                assert visibility == "SHARED", (asset_id, active, visibility)
            else:
                assert visibility == "PRIVATE", (asset_id, active, visibility)

        # 两条分享都失效之后，B 必须回到 PRIVATE
        assert _active_shares_for_asset(isolated_engine, asset_b) == 0
        assert _visibility(isolated_engine, asset_b) == "PRIVATE"
    finally:
        _cleanup(isolated_engine, user_id)
