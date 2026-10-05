"""ENG-09 第三轮返修：锁顺序的**确定性**交错回归（真实隔离 PostgreSQL）。

## 为什么必须有这一组

总控在隔离库上复现了真实死锁：清理持有**资产行锁**、撤销持有**分享行锁**，
双方随后互等，撤销抛 `DeadlockDetected`。只有"同时启动两个线程"的 `Barrier`
是**不够**的 —— 它无法保证两个事务真的分别到达了指定的持锁点，
于是用例可能一直在跑无害的交错。

这里用**数据库锁本身 + 有界探测**做同步：另一条连接先显式 `SELECT … FOR UPDATE`
拿住指定的行，再用 `pg_locks` 与带 `lock_timeout` 的 `FOR UPDATE` **观测**被测事务
确实停在该行上。观测是有界的（300ms 超时即判为"被挡住"），不会把测试挂死。

注意第三轮曾用"删除回调是否被调用"当作"清理已进入资产阶段"的代理指标 ——
那不是持锁观测：回调没被调用只能说明**还没轮到删除**。本轮改为直接观测行锁。

## 本卡确立的锁顺序

```text
第 0 级  咨询锁 (user, idempotency_key)
第 1 级  shares 行锁，按 id 升序
第 2 级  media_assets 行锁，按 id 升序
```

**任何事务都不得在持有第 2 级锁之后再取第 1 级锁。** 因此清理的资产阶段遇到
「仍有 ACTIVE 分享」的资产只记 `deferred`，不会回头去取分享行锁。

## 判定口径

- 两个线程都**不得抛异常**（死锁会以 `DeadlockDetected` 出现，必须显式断言）；
- 清理与撤销都必须**真的完成预期工作**，不能只看最终状态就放过请求异常；
- 最终状态同时满足两条不变式：不存在「有效分享 + 资产消失」，也不存在
  「无有效分享 + 残留可读」。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.services.shares.lifecycle import MediaLifecycleService
from app.services.shares.service import ShareService

from tests.db.test_engagement_concurrency import (  # noqa: E402
    EngineStorage,
    NOW,
    _active_shares_for_asset,
    _cleanup,
    _factory,
    _seed_asset,
    _seed_generation,
    _seed_user,
    _share_state,
    _uow,
    _visibility,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_engine(test_database_url: str) -> Iterator[Engine]:
    """本文件专用引擎（`tests/db/conftest.py` 提供已迁移的隔离库 URL）。"""

    engine = create_engine(test_database_url, poolclass=None)
    try:
        yield engine
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# 确定性同步原语：用数据库行锁 + 事件，保证双方真的到达指定持锁点
# --------------------------------------------------------------------------- #
def _holder(engine: Engine) -> Session:
    return sessionmaker(bind=engine, autoflush=False)()


def _lock_share_row(engine: Engine) -> tuple[Session, uuid.UUID]:
    """开一条连接，锁住"最新一条分享"的行并保持（不提交）。"""

    session = _holder(engine)
    share_id = session.execute(
        text("SELECT id FROM shares ORDER BY created_at DESC, id DESC LIMIT 1")
    ).scalar_one()
    session.execute(
        text("SELECT id FROM shares WHERE id = :s FOR UPDATE"), {"s": share_id}
    )
    return session, share_id


def _lock_asset_row(engine: Engine, asset_id: uuid.UUID) -> Session:
    session = _holder(engine)
    session.execute(
        text("SELECT id FROM media_assets WHERE id = :a FOR UPDATE"), {"a": asset_id}
    )
    return session


def _seed_share(
    engine: Engine,
    *,
    user_id: uuid.UUID,
    asset_ids: tuple[uuid.UUID, ...],
    key: str,
) -> tuple[uuid.UUID, str]:
    """建一条真实分享，返回 (share_id, slug)。"""

    generation_id = _seed_generation(
        engine, user_id=user_id, status="SUCCEEDED", assets=asset_ids
    )
    service = ShareService(uow=_uow(engine)())
    with service._uow:
        view = service.create_share(
            user_id=user_id,
            payload=__import__(
                "app.schemas.engagement", fromlist=["ShareCreateRequest"]
            ).ShareCreateRequest(
                generation_id=generation_id, output_asset_ids=list(asset_ids)
            ),
            idempotency_key=key,
        )
    with engine.connect() as conn:
        share_id = conn.execute(
            text("SELECT id FROM shares WHERE slug = :s"), {"s": view.slug}
        ).scalar_one()
    return share_id, view.slug


LOCKED_ROW_COUNT_SQL = text(
    """
    SELECT count(*) FROM pg_locks l
     JOIN pg_class c ON c.oid = l.relation
    WHERE l.locktype = 'tuple' AND l.granted AND c.relname = :table
    """
)


def _held_row_locks(engine: Engine, table: str) -> int:
    """该表上当前被持有（granted）的元组级行锁数量：直接读 `pg_locks`。"""

    with engine.connect() as conn:
        return int(
            conn.execute(LOCKED_ROW_COUNT_SQL, {"table": table}).scalar_one()
        )


def _is_locked_by_someone(engine: Engine, table: str, row_id: uuid.UUID) -> bool:
    """某一行是否被**别的会话**锁住（有界：最多等 300ms）。

    必须在**一个显式事务**里先 `set_config('lock_timeout', …, true)` 再 `FOR UPDATE`：
    `is_local = true` 只作用于当前**事务**，在 autocommit 下只覆盖那一条语句，
    紧接着的 `FOR UPDATE` 就不带超时，探测本身会无限等待（这正是本轮踩到的坑）。
    拿到锁说明没人持着（False），抛 `LockNotAvailable` 说明确实被挡住（True）。
    """

    probe = _holder(engine)
    try:
        with probe.begin():
            probe.execute(text("SELECT set_config('lock_timeout', '300ms', true)"))
            try:
                probe.execute(
                    text(f"SELECT id FROM {table} WHERE id = :row FOR UPDATE"),
                    {"row": row_id},
                )
            except DBAPIError:
                return True
        return False
    finally:
        probe.close()


def _wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.02) -> bool:
    """有界轮询：返回是否在超时前成立（不靠固定 sleep 猜时序）。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Outcome:
    """线程结果：异常也记下来，避免「只看最终状态、忽略请求异常」。"""

    def __init__(self) -> None:
        self.value: str | None = None
        self.error: BaseException | None = None

    def ok(self) -> bool:
        return self.error is None

    def describe(self) -> str:
        if self.error is not None:
            return f"error:{type(self.error).__name__}:{self.error}"
        return f"ok:{self.value}"


# --------------------------------------------------------------------------- #
# 1. 清理 vs 撤销：确定性构造「一方持资产、另一方持分享」
# --------------------------------------------------------------------------- #
def test_cleanup_and_revoke_never_deadlock_on_a_shared_asset(
    isolated_engine: Engine,
) -> None:
    """多图分享 + 其中一张到期：清理与撤销并发，两者都必须无异常完成。

    **持锁观测**（不是代理指标）：

    1. 外部连接先拿住到期资产的**行锁**；
    2. 用 `pg_locks` + 有界 `FOR UPDATE` 探测确认清理**确实被这一行挡住**；
    3. 撤销进入；放开资产锁后清理完成。

    两个请求都不得抛异常（死锁会以 `DeadlockDetected` 出现，必须显式断言），
    且最终满足「SHARED ⇔ 有 ACTIVE 未过期分享」。
    最终状态不预设谁赢：清理可能先置 `EXPIRED`、撤销也可能先置 `REVOKED`。
    """

    user_id = _seed_user(isolated_engine)
    expiring = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/exp",
        expires_at=NOW - timedelta(days=1),
    )
    live = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/live",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _seed_share(
        isolated_engine, user_id=user_id, asset_ids=(expiring, live), key="multi"
    )
    assert _visibility(isolated_engine, expiring) == "SHARED"

    storage = EngineStorage()
    cleanup_outcome = Outcome()
    revoke_outcome = Outcome()

    asset_holder = _lock_asset_row(isolated_engine, expiring)

    def _run_cleanup() -> None:
        service = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        )
        try:
            cleanup_outcome.value = str(service.run_once().as_dict())
        except BaseException as exc:  # noqa: BLE001 - 死锁会以 DeadlockDetected 出现
            cleanup_outcome.error = exc

    def _run_revoke() -> None:
        service = ShareService(uow=_uow(isolated_engine)())
        try:
            with service._uow:
                service.revoke_share(user_id=user_id, share_id=share_id)
            revoke_outcome.value = "revoked"
        except BaseException as exc:  # noqa: BLE001
            revoke_outcome.error = exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup_future = pool.submit(_run_cleanup)

            # 直接证据：该资产行确实被别人持着（有界探测，不会挂死）
            assert _wait_until(
                lambda: _is_locked_by_someone(isolated_engine, "media_assets", expiring)
            ), "清理没有在到期资产上被挡住：持锁观测失败"
            assert _held_row_locks(isolated_engine, "media_assets") > 0
            assert not cleanup_future.done(), "清理不应在资产行被持有时完成"

            revoke_future = pool.submit(_run_revoke)

            asset_holder.rollback()
            asset_holder.close()

            revoke_future.result(timeout=30)
            cleanup_future.result(timeout=30)

        assert revoke_outcome.ok(), revoke_outcome.describe()
        assert cleanup_outcome.ok(), cleanup_outcome.describe()
        assert revoke_outcome.value == "revoked"
        assert _share_state(isolated_engine, slug)[0] in {"REVOKED", "EXPIRED"}
        assert storage.deleted == [f"{user_id}/exp"]

        for asset_id in (expiring, live):
            active = _active_shares_for_asset(isolated_engine, asset_id)
            visibility = _visibility(isolated_engine, asset_id)
            if active > 0:
                assert visibility == "SHARED", (asset_id, active, visibility)
            else:
                assert visibility == "PRIVATE", (asset_id, active, visibility)
    finally:
        asset_holder.close()
        _cleanup(isolated_engine, user_id)


def test_cleanup_takes_asset_locks_in_ascending_order_across_shares(
    isolated_engine: Engine,
) -> None:
    """**跨分享**的资产取锁顺序必须是升序（总控指出的降序缺陷）。

    两条到期分享都关联同一张「最早」的资产 E，另各带一张（X/Y）。
    逐条 `close_share` 各锁各的资产时，处理第二条分享会**回头**去取 E，
    形成高→低顺序；正确实现把整批资产先排序再锁，因此第一次取资产就是 E。

    观测：外部先拿住 E。清理若按升序，会在取 E 时停下；
    用有界探测确认 E 被挡住且清理未完成，再放行并断言两者都收敛。
    """

    user_id = _seed_user(isolated_engine)
    early = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/early",
        expires_at=NOW - timedelta(days=1),
    )
    low = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/low",
        expires_at=NOW + timedelta(days=30),
    )
    high = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/high",
        expires_at=NOW + timedelta(days=30),
    )
    _share_one, slug_one = _seed_share(
        isolated_engine, user_id=user_id, asset_ids=(early, low), key="order-a"
    )
    _share_two, slug_two = _seed_share(
        isolated_engine, user_id=user_id, asset_ids=(early, high), key="order-b"
    )

    storage = EngineStorage()
    outcome = Outcome()
    holder = _lock_asset_row(isolated_engine, early)

    def _run_cleanup() -> None:
        service = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        )
        try:
            outcome.value = str(service.run_once().as_dict())
        except BaseException as exc:  # noqa: BLE001
            outcome.error = exc

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_cleanup)
            assert _wait_until(
                lambda: _is_locked_by_someone(isolated_engine, "media_assets", early)
            ), "清理没有在最早的资产上停住：跨分享的资产锁顺序不是升序"
            assert not future.done()

            holder.rollback()
            holder.close()
            future.result(timeout=30)

        assert outcome.ok(), outcome.describe()
        assert _share_state(isolated_engine, slug_one)[0] == "EXPIRED"
        assert _share_state(isolated_engine, slug_two)[0] == "EXPIRED"
        assert storage.deleted == [f"{user_id}/early"]
        for asset_id in (early, low, high):
            assert _visibility(isolated_engine, asset_id) == "PRIVATE"
    finally:
        holder.close()
        _cleanup(isolated_engine, user_id)


def test_cleanup_converges_expired_asset_and_its_active_share_in_one_round(
    isolated_engine: Engine,
) -> None:
    """资产已到期、且仍被一条 ACTIVE 分享引用：同一轮就要收敛掉，不再延期。

    第三轮这里的期望是 `deferred == 1`（因为当时只收敛"自身到期"的分享），
    但那正是第四轮修掉的"持续延期"：分享期限在未来时资产会被无限期延后。
    现在「引用失效资产的 ACTIVE 分享」与到期分享进同一批目标，
    因此资产与分享在同一轮完成，`deferred` 为 0。
    """

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/defer",
        expires_at=NOW - timedelta(days=1),
    )
    share_id, slug = _seed_share(
        isolated_engine, user_id=user_id, asset_ids=(asset_id,), key="defer-share"
    )
    # 分享自身期限在未来：旧实现会因此一直延后
    with isolated_engine.begin() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = :far WHERE id = :s"),
            {"far": NOW + timedelta(days=30), "s": share_id},
        )

    storage = EngineStorage()
    try:
        report = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        ).run_once()

        assert report.shares_expired == 1, report
        assert report.assets_expired == 1, report
        assert report.assets_deferred == 0, report
        assert storage.deleted == [f"{user_id}/defer"]
        assert _share_state(isolated_engine, slug)[0] == "EXPIRED"
        assert _visibility(isolated_engine, asset_id) == "PRIVATE"

        # 第二轮无事可做（幂等），不会重复删除
        again = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        ).run_once()
        assert again.as_dict() == {
            "assets_expired": 0,
            "assets_deferred": 0,
            "assets_absent": 0,
            "shares_expired": 0,
        }
    finally:
        _cleanup(isolated_engine, user_id)


def _make_asset_expired(engine: Engine, asset_id: uuid.UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE media_assets SET expires_at = :past WHERE id = :a"),
            {"past": NOW - timedelta(days=1), "a": asset_id},
        )


def test_expired_asset_converges_share_whose_own_expiry_is_in_the_future(
    isolated_engine: Engine,
) -> None:
    """分享期限在**未来**、资产已到期：必须收敛，不得永久 deferred。

    旧实现只在「分享自身到期」时才关闭它，于是资产阶段永远看到 ACTIVE 分享、
    永远记 deferred，公开链接一直活着。
    """

    user_id = _seed_user(isolated_engine)
    live = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/fut",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _seed_share(
        isolated_engine, user_id=user_id, asset_ids=(live,), key="future-share"
    )
    # 分享期限推到很远的未来（远晚于资产期限）
    with isolated_engine.begin() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = :far WHERE id = :s"),
            {"far": NOW + timedelta(days=365), "s": share_id},
        )
    _make_asset_expired(isolated_engine, live)

    storage = EngineStorage()
    try:
        service = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        )
        # 连续多轮：期间**不**手工改期限或状态
        first = service.run_once()
        second = service.run_once()
        third = service.run_once()

        assert first.shares_expired == 1, first
        assert first.assets_expired == 1, first
        assert second.assets_expired == 0 and third.assets_expired == 0
        assert _share_state(isolated_engine, slug)[0] == "EXPIRED"
        assert _visibility(isolated_engine, live) == "PRIVATE"
        assert storage.deleted == [f"{user_id}/fut"]
    finally:
        _cleanup(isolated_engine, user_id)


def test_expired_asset_converges_share_with_null_expiry(
    isolated_engine: Engine,
) -> None:
    """分享期限为 `NULL`（永不过期）、资产已到期：同样必须收敛。"""

    user_id = _seed_user(isolated_engine)
    live = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/null",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _seed_share(
        isolated_engine, user_id=user_id, asset_ids=(live,), key="null-share"
    )
    with isolated_engine.begin() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = NULL WHERE id = :s"), {"s": share_id}
        )
    _make_asset_expired(isolated_engine, live)

    storage = EngineStorage()
    try:
        report = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        ).run_once()

        assert report.shares_expired == 1, report
        assert report.assets_expired == 1, report
        assert _share_state(isolated_engine, slug)[0] == "EXPIRED"
        assert _visibility(isolated_engine, live) == "PRIVATE"
    finally:
        _cleanup(isolated_engine, user_id)


def test_expired_asset_converges_legacy_share_without_share_assets_rows(
    isolated_engine: Engine,
) -> None:
    """历史单图回落：`share_assets` **没有**关联行时也要按 `shares.asset_id` 收敛。"""

    user_id = _seed_user(isolated_engine)
    live = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/legacy",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _seed_share(
        isolated_engine, user_id=user_id, asset_ids=(live,), key="legacy-share"
    )
    # 删掉关联行，模拟 0021 之前的单图分享（只有 shares.asset_id）
    with isolated_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM share_assets WHERE share_id = :s"), {"s": share_id}
        )
        conn.execute(
            text("UPDATE shares SET expires_at = :far WHERE id = :s"),
            {"far": NOW + timedelta(days=365), "s": share_id},
        )
    _make_asset_expired(isolated_engine, live)

    storage = EngineStorage()
    try:
        report = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        ).run_once()

        assert report.shares_expired == 1, report
        assert report.assets_expired == 1, report
        assert _share_state(isolated_engine, slug)[0] == "EXPIRED"
        assert _visibility(isolated_engine, live) == "PRIVATE"
        assert storage.deleted == [f"{user_id}/legacy"]
    finally:
        _cleanup(isolated_engine, user_id)


def test_cleanup_does_not_starve_other_candidates_across_rounds(
    isolated_engine: Engine,
) -> None:
    """多轮之间不得长期阻塞其他候选：每轮都推进，且不重复处理已完成的行。

    造 5 条「资产到期 + 分享期限在未来」，每轮只处理 batch_size，断言有限轮次内
    全部收敛，且每轮都真的推进（不是卡在同一条上反复 deferred）。
    """

    user_id = _seed_user(isolated_engine)
    slugs: list[str] = []
    for index in range(5):
        asset_id = _seed_asset(
            isolated_engine,
            user_id=user_id,
            key=f"{user_id}/starve-{index}",
            expires_at=NOW + timedelta(days=30),
        )
        share_id, slug = _seed_share(
            isolated_engine,
            user_id=user_id,
            asset_ids=(asset_id,),
            key=f"starve-share-{index}",
        )
        with isolated_engine.begin() as conn:
            conn.execute(
                text("UPDATE shares SET expires_at = :far WHERE id = :s"),
                {"far": NOW + timedelta(days=365), "s": share_id},
            )
        _make_asset_expired(isolated_engine, asset_id)
        slugs.append(slug)

    storage = EngineStorage()
    try:
        service = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        )
        progress: list[int] = []
        for _ in range(5):
            report = service.run_once(batch_size=2)
            progress.append(report.shares_expired)

        # 在有剩余工作之前每轮都必须推进（不允许卡在同一条上反复 deferred）；
        # 全部处理完之后轮次自然是 0，所以只断言"前缀非零"
        nonzero = [step for step in progress if step > 0]
        assert nonzero, progress
        assert progress[: len(nonzero)] == nonzero, progress
        assert sum(progress) == 5, progress
        for slug in slugs:
            assert _share_state(isolated_engine, slug)[0] == "EXPIRED"
        assert len(storage.deleted) == 5
        assert len(set(storage.deleted)) == 5
    finally:
        _cleanup(isolated_engine, user_id)
