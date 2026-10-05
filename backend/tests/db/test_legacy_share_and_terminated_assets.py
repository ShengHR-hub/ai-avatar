"""ENG-09 第五轮返修：历史单图分享、终结资产收敛与「锁内重裁决」（真实隔离 PostgreSQL）。

本文件只放**必须在真实数据库上**才能证明的事 —— 内存替身没有行锁、没有
identity map、也没有「另一个事务提交后能不能看见」的语义：

1. **历史单图分享的读取与收敛**（总控缺陷 3）：`share_assets` 有行时只按明确选中
   集合授权；无行时回落 `shares.asset_id`。撤销同资产上的新分享后，历史分享必须
   仍然有效、资产必须仍是 `SHARED`、匿名取图必须继续可用。
2. **终结资产关联分享的收敛**（总控缺陷 4）：`DELETED`/`EXPIRED` 也是"失效资产"，
   引用它们的 ACTIVE 分享必须被收敛；**终结资产的对象不得被再次删除**。
3. **锁内重新裁决**（总控缺陷 2）：预读只是"锁谁"的候选，真正关谁要在持锁、
   刷新事实之后决定。两种交错都确定性构造：
   - 预读之后资产期限被恢复为未来 → 分享**不**关闭；
   - 预读之后另一事务通过正式 `ShareService` 撤销 → 清理**不得**把 `REVOKED`
     覆盖成 `EXPIRED`。

## 交错是怎么做成"确定性"的

生命周期事务的顺序是「预读（不加锁）→ 锁分享 → 锁资产 → 重裁决」。预读与取锁
之间**没有可观测的锁**（预读刻意不加锁），因此无法用"外部持锁"来卡住它。
这里用一层**只控制时序、不改行为**的替身 `_PreReadHook`：真实预读返回之后，
先让外部事务提交，再把结果交回去。外部事务是真的另一条连接、真的提交。

对照实验（证明用例不是空跑）：`tests/db/test_lock_order.py` 里
`test_cleanup_converges_expired_asset_and_its_active_share_in_one_round`
在没有交错时**会**关闭分享；本文件的两个用例只加了那一笔外部提交，结论就反转。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import sessionmaker

from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.services.assets.service import AssetService
from app.services.shares.lifecycle import MediaLifecycleService
from app.services.shares.service import ShareService

from tests.db.test_engagement_concurrency import (  # noqa: E402
    EngineStorage,
    NOW,
    _cleanup,
    _factory,
    _seed_asset,
    _seed_generation,
    _seed_user,
    _uow,
    _visibility,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture
def isolated_engine(test_database_url: str) -> Iterator[Engine]:
    engine = create_engine(test_database_url, poolclass=None)
    try:
        yield engine
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# 辅助：真实分享创建 / 状态读取
# --------------------------------------------------------------------------- #
def _create_share(
    engine: Engine, *, user_id: uuid.UUID, asset_ids: tuple[uuid.UUID, ...], key: str
) -> tuple[uuid.UUID, str]:
    """用真实 `ShareService` 建一条分享，返回 (share_id, slug)。"""

    from app.schemas.engagement import ShareCreateRequest

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
    with engine.connect() as conn:
        share_id = conn.execute(
            text("SELECT id FROM shares WHERE slug = :s"), {"s": view.slug}
        ).scalar_one()
    return share_id, view.slug


def _share_row(engine: Engine, slug: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, status, revoked_at, expires_at FROM shares WHERE slug = :s"
            ),
            {"s": slug},
        ).mappings().one()
    return dict(row)


def _make_legacy(engine: Engine, share_id: uuid.UUID, *, expires_at) -> None:
    """把一条分享变成 0021 之前的**历史单图分享**：删掉关联行、只留 `shares.asset_id`。"""

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM share_assets WHERE share_id = :s"), {"s": share_id}
        )
        conn.execute(
            text("UPDATE shares SET expires_at = :e WHERE id = :s"),
            {"e": expires_at, "s": share_id},
        )


def _set_asset(
    engine: Engine,
    asset_id: uuid.UUID,
    *,
    status: str | None = None,
    visibility: str | None = None,
    expires_at=None,
    deleted_at=None,
) -> None:
    sets: list[str] = []
    params: dict[str, Any] = {"a": asset_id}
    if status is not None:
        sets.append("status = :status")
        params["status"] = status
    if visibility is not None:
        sets.append("visibility = :visibility")
        params["visibility"] = visibility
    if expires_at is not None:
        sets.append("expires_at = :expires_at")
        params["expires_at"] = expires_at
    if deleted_at is not None:
        sets.append("deleted_at = :deleted_at")
        params["deleted_at"] = deleted_at
    if not sets:
        return
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE media_assets SET {', '.join(sets)} WHERE id = :a"), params
        )


def _active_shares_covering(engine: Engine, asset_id: uuid.UUID) -> int:
    """ACTIVE 且未过期、并**授权**该资产的分享数（含历史单图回落）。

    与仓储同一套语义，因此它也是"独立于被测代码"的核对口径。
    """

    with engine.connect() as conn:
        return conn.execute(
            text(
                """
                SELECT count(*) FROM shares s
                 WHERE s.status = 'ACTIVE'
                   AND (s.expires_at IS NULL OR s.expires_at > now())
                   AND (
                        EXISTS (SELECT 1 FROM share_assets sa
                                 WHERE sa.share_id = s.id AND sa.asset_id = :a)
                     OR (s.asset_id = :a
                         AND NOT EXISTS (SELECT 1 FROM share_assets sa
                                          WHERE sa.share_id = s.id))
                   )
                """
            ),
            {"a": asset_id},
        ).scalar_one()


def _share_lookup(engine: Engine) -> Callable[[uuid.UUID], bool]:
    """与 `app.api.deps._asset_share_lookup` 同形的**实时**授权查询（独立工作单元）。

    时钟固定为用例的 `NOW`：本文件的所有期限都是相对 `NOW` 写的合成值，
    用真实系统时钟会让"已过期"的断言依赖于测试运行时刻（`NOW` 本身是未来时间）。
    """

    def lookup(asset_id: uuid.UUID) -> bool:
        with SqlAlchemyUnitOfWork(_factory(engine)) as uow:
            return ShareService(uow=uow, clock=lambda: NOW).asset_has_valid_share(
                asset_id
            )

    return lookup


def _asset_service(engine: Engine) -> AssetService:
    return AssetService(
        uow=_uow(engine)(),
        storage=EngineStorage(),
        storage_provider="MINIO",
        enable_reference_image=False,
        clock=lambda: NOW,
        share_lookup=_share_lookup(engine),
    )


# --------------------------------------------------------------------------- #
# 缺陷 3：历史单图分享的读取与收敛路径
# --------------------------------------------------------------------------- #
def test_legacy_share_stays_authoritative_after_new_share_is_revoked(
    isolated_engine: Engine,
) -> None:
    """同一资产上有「有效历史单图分享」+「新分享」：撤销新分享后历史分享仍然有效。

    总控复现的缺陷：`list_active_for_asset` / `list_valid_for_asset` 只 JOIN
    `share_assets`，于是撤销新分享时看不到历史分享 → 把仍然公开的资产错收敛成
    `PRIVATE`（链接还在，图读不到）。
    """

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/legacy-read",
        expires_at=NOW + timedelta(days=30),
    )

    legacy_id, legacy_slug = _create_share(
        isolated_engine, user_id=user_id, asset_ids=(asset_id,), key="legacy-key"
    )
    _make_legacy(isolated_engine, legacy_id, expires_at=NOW + timedelta(days=30))

    new_id, new_slug = _create_share(
        isolated_engine, user_id=user_id, asset_ids=(asset_id,), key="new-key"
    )
    assert _visibility(isolated_engine, asset_id) == "SHARED"

    # 撤销新分享（走正式 Service）
    service = ShareService(uow=_uow(isolated_engine)())
    with service._uow:
        service.revoke_share(user_id=user_id, share_id=new_id)

    try:
        assert _share_row(isolated_engine, new_slug)["status"] == "REVOKED"
        assert _share_row(isolated_engine, legacy_slug)["status"] == "ACTIVE"

        # 资产必须仍是 SHARED（历史分享还活着），并且匿名取图继续可用
        assert _visibility(isolated_engine, asset_id) == "SHARED"
        assert _active_shares_covering(isolated_engine, asset_id) == 1
        assert _share_lookup(isolated_engine)(asset_id) is True

        # 匿名读取历史分享本身也仍然可用
        reader = ShareService(uow=_uow(isolated_engine)())
        with reader._uow:
            view = reader.read_share(slug=legacy_slug)
        assert view.slug == legacy_slug
        assert [item.asset_id for item in view.outputs] == [asset_id]

        # 匿名资产读取（真实 AssetService + 实时分享依据）放行
        asset_service = _asset_service(isolated_engine)
        with asset_service._uow:
            asset = asset_service.get_asset(asset_id=asset_id, viewer_user_id=None)
        assert asset.id == asset_id
    finally:
        _cleanup(isolated_engine, user_id)


def test_legacy_share_is_counted_and_authorizes_anonymously(
    isolated_engine: Engine,
) -> None:
    """计数与匿名授权都必须把历史单图分享算进去（含 `count_active_for_asset`）。"""

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/legacy-count",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _create_share(
        isolated_engine, user_id=user_id, asset_ids=(asset_id,), key="legacy-count"
    )
    _make_legacy(isolated_engine, share_id, expires_at=NOW + timedelta(days=30))

    try:
        with SqlAlchemyUnitOfWork(_factory(isolated_engine)) as uow:
            assert uow.shares.count_active_for_asset(asset_id) == 1
            assert uow.shares.count_active_for_assets([asset_id])[asset_id] == 1
            assert uow.shares.list_active_for_asset(asset_id)
            assert uow.shares.list_valid_for_asset(asset_id, now=NOW)
            assert uow.shares.peek_active_for_assets([asset_id])

        # 历史分享过期之后，授权必须收回（实时判定，不靠清理任务）
        _make_legacy(isolated_engine, share_id, expires_at=NOW - timedelta(seconds=1))
        with SqlAlchemyUnitOfWork(_factory(isolated_engine)) as uow:
            assert uow.shares.list_valid_for_asset(asset_id, now=NOW) == ()
        assert _share_lookup(isolated_engine)(asset_id) is False
    finally:
        _cleanup(isolated_engine, user_id)


# --------------------------------------------------------------------------- #
# 缺陷 4：终结资产关联分享的收敛
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("asset_status", ["EXPIRED", "DELETED"])
@pytest.mark.parametrize("share_expiry", ["future", "null"])
def test_terminated_asset_converges_linked_share_across_rounds(
    isolated_engine: Engine, asset_status: str, share_expiry: str
) -> None:
    """资产已终结（`EXPIRED`/`DELETED`）、分享仍 ACTIVE：连续多轮必须收敛。

    分享自身期限取**未来**与 `NULL` 两态 —— 只看分享期限的实现永远收敛不到它们。
    **终结资产的对象不得被再次删除**（`storage.deleted` 必须为空）。
    """

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/terminated-{asset_status}-{share_expiry}",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _create_share(
        isolated_engine,
        user_id=user_id,
        asset_ids=(asset_id,),
        key=f"terminated-{asset_status}-{share_expiry}",
    )
    # 分享期限：未来 或 NULL（永不过期）
    expiry = NOW + timedelta(days=365) if share_expiry == "future" else None
    with isolated_engine.begin() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = :e WHERE id = :s"),
            {"e": expiry, "s": share_id},
        )
    # 资产终结：对象早已删除、状态已终结，但可见性仍是 SHARED
    _set_asset(
        isolated_engine,
        asset_id,
        status=asset_status,
        visibility="SHARED",
        deleted_at=NOW - timedelta(days=1) if asset_status == "DELETED" else None,
    )

    storage = EngineStorage()
    try:
        service = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        )
        first = service.run_once()
        second = service.run_once()
        third = service.run_once()

        assert first.shares_expired == 1, first
        assert second.shares_expired == 0 and third.shares_expired == 0
        assert _share_row(isolated_engine, slug)["status"] == "EXPIRED"
        assert _visibility(isolated_engine, asset_id) == "PRIVATE"
        assert _active_shares_covering(isolated_engine, asset_id) == 0
        # 终结资产的对象**没有**被再次删除
        assert storage.deleted == [], storage.deleted
    finally:
        _cleanup(isolated_engine, user_id)


@pytest.mark.parametrize("asset_status", ["EXPIRED", "DELETED"])
def test_terminated_asset_converges_legacy_share(
    isolated_engine: Engine, asset_status: str
) -> None:
    """历史单图回落 + 终结资产：没有 `share_assets` 行也必须被收敛。"""

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/terminated-legacy-{asset_status}",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _create_share(
        isolated_engine,
        user_id=user_id,
        asset_ids=(asset_id,),
        key=f"terminated-legacy-{asset_status}",
    )
    _make_legacy(isolated_engine, share_id, expires_at=NOW + timedelta(days=365))
    _set_asset(
        isolated_engine,
        asset_id,
        status=asset_status,
        visibility="SHARED",
        deleted_at=NOW - timedelta(days=1) if asset_status == "DELETED" else None,
    )

    storage = EngineStorage()
    try:
        report = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        ).run_once()

        assert report.shares_expired == 1, report
        assert _share_row(isolated_engine, slug)["status"] == "EXPIRED"
        assert _visibility(isolated_engine, asset_id) == "PRIVATE"
        assert storage.deleted == [], storage.deleted
    finally:
        _cleanup(isolated_engine, user_id)


def test_terminated_asset_does_not_reexpire_an_already_closed_share(
    isolated_engine: Engine,
) -> None:
    """分享已经是 `REVOKED` 时，终结资产的收敛不得把它改写（也不重复删对象）。"""

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/terminated-revoked",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _create_share(
        isolated_engine, user_id=user_id, asset_ids=(asset_id,), key="terminated-revoked"
    )
    service = ShareService(uow=_uow(isolated_engine)())
    with service._uow:
        service.revoke_share(user_id=user_id, share_id=share_id)
    revoked_at = _share_row(isolated_engine, slug)["revoked_at"]
    _set_asset(
        isolated_engine,
        asset_id,
        status="EXPIRED",
        visibility="SHARED",
    )

    storage = EngineStorage()
    try:
        report = MediaLifecycleService(
            uow_factory=_uow(isolated_engine), storage=storage, clock=lambda: NOW
        ).run_once()

        assert report.shares_expired == 0, report
        row = _share_row(isolated_engine, slug)
        assert row["status"] == "REVOKED"
        assert row["revoked_at"] == revoked_at
        assert _visibility(isolated_engine, asset_id) == "PRIVATE"
        assert storage.deleted == [], storage.deleted
    finally:
        _cleanup(isolated_engine, user_id)


# --------------------------------------------------------------------------- #
# 缺陷 2：锁内重新裁决（确定性交错）
# --------------------------------------------------------------------------- #
class _PreReadHook:
    """在指定的一次分享预读**返回之后**插入一次外部事务。

    只控制时序，不改行为：先调用真实仓储拿到结果，再执行外部动作（真的另一条
    连接、真的提交），最后把原样的结果交回去。

    `fired` 是**跨工作单元共享**的列表：`run_once` 会开两个工作单元（分享阶段与
    资产阶段），钩子实例因此有两个，但"外部事务是否插入过"是整轮的性质。
    """

    def __init__(
        self, inner: Any, trigger: str, action: Callable[[], None], fired: list[str]
    ) -> None:
        self._inner = inner
        self._trigger = trigger
        self._action = action
        self._fired = fired

    def __getattr__(self, name: str) -> Any:  # 其余方法原样透传
        return getattr(self._inner, name)

    def _maybe_fire(self, name: str) -> None:
        if name != self._trigger or self._fired:
            return
        self._fired.append(name)
        self._action()

    def peek_expired_shares(self, *, now, limit):
        rows = self._inner.peek_expired_shares(now=now, limit=limit)
        self._maybe_fire("peek_expired_shares")
        return rows

    def peek_active_for_invalid_assets(self, *, now, limit):
        rows = self._inner.peek_active_for_invalid_assets(now=now, limit=limit)
        self._maybe_fire("peek_active_for_invalid_assets")
        return rows


def _hooked_factory(
    engine: Engine, trigger: str, action: Callable[[], None]
) -> tuple[Callable[[], SqlAlchemyUnitOfWork], list[str]]:
    """构造一个工作单元工厂：进入上下文后把分享仓储包一层预读钩子。"""

    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
    fired: list[str] = []

    class _HookedUow(SqlAlchemyUnitOfWork):
        def __enter__(self):
            super().__enter__()
            self.shares = _PreReadHook(  # type: ignore[assignment]
                self.shares, trigger, action, fired
            )
            return self

    return (lambda: _HookedUow(maker)), fired


def test_cleanup_does_not_close_share_when_asset_expiry_is_restored(
    isolated_engine: Engine,
) -> None:
    """预读之后资产期限恢复为未来：分享**不**得被关闭，资产仍 `SHARED`。

    确定性交错：真实预读（看到资产已到期）返回后，另一条连接把资产的 `expires_at`
    改回未来并提交，然后才进入「锁分享 → 锁资产 → 重裁决」。
    没有锁内重裁决的实现会照着预读结论无条件关闭分享。
    """

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/restore-expiry",
        expires_at=NOW - timedelta(days=1),
    )
    share_id, slug = _create_share(
        isolated_engine, user_id=user_id, asset_ids=(asset_id,), key="restore-expiry"
    )
    # 分享自身期限在未来：只能靠"引用了失效资产"进入目标集合
    with isolated_engine.begin() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = :far WHERE id = :s"),
            {"far": NOW + timedelta(days=365), "s": share_id},
        )
    assert _visibility(isolated_engine, asset_id) == "SHARED"

    def _restore() -> None:
        with isolated_engine.begin() as conn:
            conn.execute(
                text("UPDATE media_assets SET expires_at = :far WHERE id = :a"),
                {"far": NOW + timedelta(days=30), "a": asset_id},
            )

    factory, fired = _hooked_factory(
        isolated_engine, "peek_active_for_invalid_assets", _restore
    )
    storage = EngineStorage()
    try:
        report = MediaLifecycleService(
            uow_factory=factory, storage=storage, clock=lambda: NOW
        ).run_once()

        assert fired, "预读钩子没有触发：交错没有真正构造出来"
        assert report.shares_expired == 0, report
        assert _share_row(isolated_engine, slug)["status"] == "ACTIVE"
        assert _visibility(isolated_engine, asset_id) == "SHARED"
        assert storage.deleted == [], storage.deleted
    finally:
        _cleanup(isolated_engine, user_id)


def test_cleanup_does_not_overwrite_a_concurrently_revoked_share(
    isolated_engine: Engine,
) -> None:
    """预读之后另一事务用正式 `ShareService` 撤销：清理不得把 `REVOKED` 覆盖成 `EXPIRED`。

    确定性交错：真实预读（看到分享已到期）返回后，另一条连接通过 `ShareService.
    revoke_share` 撤销并提交，然后才进入取锁与重裁决。
    """

    user_id = _seed_user(isolated_engine)
    asset_id = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/concurrent-revoke",
        expires_at=NOW + timedelta(days=30),
    )
    share_id, slug = _create_share(
        isolated_engine,
        user_id=user_id,
        asset_ids=(asset_id,),
        key="concurrent-revoke",
    )
    # 分享已到期 → 进入 `peek_expired_shares` 的候选
    with isolated_engine.begin() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = :past WHERE id = :s"),
            {"past": NOW - timedelta(seconds=1), "s": share_id},
        )

    def _revoke() -> None:
        service = ShareService(uow=_uow(isolated_engine)())
        with service._uow:
            service.revoke_share(user_id=user_id, share_id=share_id)

    factory, fired = _hooked_factory(isolated_engine, "peek_expired_shares", _revoke)
    storage = EngineStorage()
    try:
        report = MediaLifecycleService(
            uow_factory=factory, storage=storage, clock=lambda: NOW
        ).run_once()

        assert fired, "预读钩子没有触发：交错没有真正构造出来"
        assert report.shares_expired == 0, report
        row = _share_row(isolated_engine, slug)
        assert row["status"] == "REVOKED", row
        assert row["revoked_at"] is not None
        # 撤销已经按"没有其他有效分享"把可见性收敛回 PRIVATE；清理不得改写
        assert _visibility(isolated_engine, asset_id) == "PRIVATE"
        assert storage.deleted == [], storage.deleted
    finally:
        _cleanup(isolated_engine, user_id)


def test_cleanup_rejudgement_keeps_other_candidates_working(
    isolated_engine: Engine,
) -> None:
    """重裁决不影响**其它**真正该关闭的分享：同一轮里该关的照关、不该关的不关。

    两条到期分享指向两张资产，其中一张的期限在预读后被恢复为未来：
    仍然到期的那条必须被关闭，被恢复的那条必须保持 ACTIVE，且无死锁、计数正确。
    """

    user_id = _seed_user(isolated_engine)
    restored_asset = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/mixed-restored",
        expires_at=NOW - timedelta(days=1),
    )
    doomed_asset = _seed_asset(
        isolated_engine,
        user_id=user_id,
        key=f"{user_id}/mixed-doomed",
        expires_at=NOW + timedelta(days=30),
    )
    restored_share, restored_slug = _create_share(
        isolated_engine,
        user_id=user_id,
        asset_ids=(restored_asset,),
        key="mixed-restored",
    )
    doomed_share, doomed_slug = _create_share(
        isolated_engine,
        user_id=user_id,
        asset_ids=(doomed_asset,),
        key="mixed-doomed",
    )
    with isolated_engine.begin() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = :far WHERE id = :s"),
            {"far": NOW + timedelta(days=365), "s": restored_share},
        )
        conn.execute(
            text("UPDATE shares SET expires_at = :past WHERE id = :s"),
            {"past": NOW - timedelta(seconds=1), "s": doomed_share},
        )

    def _restore() -> None:
        with isolated_engine.begin() as conn:
            conn.execute(
                text("UPDATE media_assets SET expires_at = :far WHERE id = :a"),
                {"far": NOW + timedelta(days=30), "a": restored_asset},
            )

    factory, fired = _hooked_factory(
        isolated_engine, "peek_active_for_invalid_assets", _restore
    )
    storage = EngineStorage()
    try:
        report = MediaLifecycleService(
            uow_factory=factory, storage=storage, clock=lambda: NOW
        ).run_once()

        assert fired, "预读钩子没有触发：交错没有真正构造出来"
        assert report.shares_expired == 1, report
        assert _share_row(isolated_engine, doomed_slug)["status"] == "EXPIRED"
        assert _share_row(isolated_engine, restored_slug)["status"] == "ACTIVE"
        assert _visibility(isolated_engine, restored_asset) == "SHARED"
        assert _visibility(isolated_engine, doomed_asset) == "PRIVATE"
        assert storage.deleted == [], storage.deleted
    finally:
        _cleanup(isolated_engine, user_id)
