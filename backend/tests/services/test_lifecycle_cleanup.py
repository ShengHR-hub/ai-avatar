"""生命周期清理测试（ENG-09）。

依据 `04-ai-provider.md` 第 7 节与 `03-api-contract.md` 第 4.4 节，以及技术负责人
2026-10-02 裁决（每 300 秒调度、每批最多 50 条）：

```text
1. 有界批量选出到期资产
2. 先经 StorageAdapter.delete() 删除对象
   - 成功 / 对象不存在 → 才把状态改为 EXPIRED
   - 存储不可用或其它失败 → 保留原状态，下批重试，并继续处理后面的资产
3. 资产失效后收敛其分享；仅当没有其他有效分享时才把 SHARED 恢复 PRIVATE
4. 收敛到期的 ACTIVE 分享
```

同时断言：单项失败不阻塞整批、批量上限生效、不物理删除任何业务历史、
任务/流水/Provider/分享/反馈/审计全部保留。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.generation import GenerationOutputDraft
from app.storage import ObjectNotFound, StorageUnavailable
from app.services.shares.lifecycle import (
    DEFAULT_CLEANUP_BATCH_SIZE,
    MediaLifecycleService,
    ShareLifecycle,
)
from app.services.shares.service import DEFAULT_SHARE_TTL_DAYS, ShareService
from tests.services.fakes import (
    EngagementUnitOfWork,
    MutableClock,
    make_generation_task_row,
    make_media_asset_row,
    make_user_row,
)

CLOCK_START = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class RecordingStorage:
    """记录对象存储调用，并可按 key 注入失败（模拟 `StorageUnavailable`）。"""

    def __init__(self, *, missing: set[str] | None = None, failing: set[str] | None = None):
        self.deleted: list[str] = []
        self._missing = set(missing or ())
        self._failing = set(failing or ())

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        if key in self._failing:
            raise StorageUnavailable("synthetic unavailable")
        if key in self._missing:
            raise ObjectNotFound("synthetic missing")

    # 其余端口方法本卡不使用；生命周期只调用 delete
    def exists(self, key: str) -> bool:  # pragma: no cover
        return key not in self._missing

    def read(self, key: str):  # pragma: no cover
        raise ObjectNotFound(key)

    def upload(self, key, content, content_type):  # pragma: no cover
        raise NotImplementedError

    def create_signed_url(self, key, method, expires_in, content_type=None):  # pragma: no cover
        raise NotImplementedError


class Harness:
    def __init__(
        self,
        uow: EngagementUnitOfWork,
        clock: MutableClock,
        storage: RecordingStorage,
    ) -> None:
        self.uow = uow
        self.clock = clock
        self.storage = storage
        self.shares = ShareService(uow=uow, clock=clock)
        self.lifecycle = MediaLifecycleService(uow_factory=lambda: uow, storage=storage, clock=clock)

    def run(self, *, batch_size: int | None = None):
        """跑一轮清理。

        不额外套一层 `with self.uow:`：`run_once` 自己按阶段开/提交/关闭工作单元
        （分享优先 → 资产）。外面再套一层会让内层事务在退出时被回滚，
        于是「分享已收敛」「资产已 EXPIRED」这类结果永远观察不到。
        """

        return self.lifecycle.run_once(batch_size=batch_size)

    def seed_expired_asset(
        self,
        *,
        user_id: uuid.UUID,
        key: str,
        expired_seconds_ago: int = 60,
        status: str = "READY",
    ):
        asset = make_media_asset_row(
            user_id=user_id,
            purpose="RESULT",
            asset_type="RESULT",
            status=status,
            storage_key=key,
            now=self.clock(),
            expires_at=self.clock() - timedelta(seconds=expired_seconds_ago),
        )
        return self.uow.media.register(asset)

    def share_asset(self, *, user_id: uuid.UUID, asset) -> str:
        task = make_generation_task_row(user_id=user_id, status="SUCCEEDED", now=self.clock())
        self.uow.generation_tasks.seed(task)
        self.uow.generation_tasks.create_output(
            GenerationOutputDraft(generation_id=task.id, asset_id=asset.id, output_index=0)
        )
        with self.uow:
            view = self.shares.create_share(
                user_id=user_id,
                payload=_payload(task.id, [asset.id]),
                idempotency_key=f"share-{uuid.uuid4().hex[:8]}",
            )
        return view.slug


def _payload(generation_id: uuid.UUID, asset_ids: list[uuid.UUID]):
    from app.schemas.engagement import ShareCreateRequest

    return ShareCreateRequest(generation_id=generation_id, output_asset_ids=asset_ids)


@pytest.fixture
def harness() -> Harness:
    clock = MutableClock(CLOCK_START)
    uow = EngagementUnitOfWork(clock=clock)
    return Harness(uow, clock, RecordingStorage())


# --------------------------------------------------------------------------- #
# 先删对象，再改状态
# --------------------------------------------------------------------------- #
def test_object_is_deleted_before_status_changes(harness: Harness) -> None:
    user = harness.uow.users.seed(make_user_row())
    asset = harness.seed_expired_asset(user_id=user.id, key="assets/a/expired")

    report = harness.run()

    assert report.assets_expired == 1
    assert report.assets_deferred == 0
    assert harness.storage.deleted == ["assets/a/expired"]
    assert asset.status == "EXPIRED"


def test_missing_object_is_treated_as_deleted(harness: Harness) -> None:
    """`delete` 对不存在的 key 幂等成功：对象已不在，状态同样要收敛。"""

    user = harness.uow.users.seed(make_user_row())
    asset = harness.seed_expired_asset(user_id=user.id, key="assets/b/gone")
    harness.storage._missing.add("assets/b/gone")

    report = harness.run()

    assert report.assets_expired == 1
    assert report.assets_absent == 1
    assert asset.status == "EXPIRED"


def test_storage_failure_keeps_status_and_retries_next_batch(harness: Harness) -> None:
    """存储不可用时保留 `READY`（下次重试），不提前标记。"""

    user = harness.uow.users.seed(make_user_row())
    asset = harness.seed_expired_asset(user_id=user.id, key="assets/c/flaky")
    harness.storage._failing.add("assets/c/flaky")

    first = harness.run()
    assert first.assets_expired == 0
    assert first.assets_deferred == 1
    assert asset.status == "READY"

    # 恢复后同一批可被重试并成功
    harness.storage._failing.clear()
    second = harness.run()
    assert second.assets_expired == 1
    assert asset.status == "EXPIRED"


def test_single_failure_does_not_block_the_rest_of_the_batch(harness: Harness) -> None:
    """一条失败不得阻塞整批：其余资产照常清理。"""

    user = harness.uow.users.seed(make_user_row())
    failing = harness.seed_expired_asset(
        user_id=user.id, key="assets/d/fail", expired_seconds_ago=30
    )
    healthy = harness.seed_expired_asset(
        user_id=user.id, key="assets/d/ok", expired_seconds_ago=10
    )
    harness.storage._failing.add("assets/d/fail")

    report = harness.run()

    assert report.assets_expired == 1
    assert report.assets_deferred == 1
    assert healthy.status == "EXPIRED"
    assert failing.status == "READY"


def test_batch_size_is_bounded(harness: Harness) -> None:
    """每批最多 50 条（裁决值）：超出的留到下一批。"""

    assert DEFAULT_CLEANUP_BATCH_SIZE == 50
    user = harness.uow.users.seed(make_user_row())
    for index in range(55):
        harness.seed_expired_asset(user_id=user.id, key=f"assets/e/{index}")

    first = harness.run()
    assert first.assets_expired == 50
    assert len(harness.storage.deleted) == 50

    second = harness.run()
    assert second.assets_expired == 5


def test_not_yet_expired_assets_are_untouched(harness: Harness) -> None:
    user = harness.uow.users.seed(make_user_row())
    asset = make_media_asset_row(
        user_id=user.id,
            now=harness.clock(),
        purpose="RESULT",
        asset_type="RESULT",
        status="READY",
        expires_at=harness.clock() + timedelta(days=1),
    )
    harness.uow.media.register(asset)

    report = harness.run()

    assert report.as_dict() == {
        "assets_expired": 0,
        "assets_deferred": 0,
        "assets_absent": 0,
        "shares_expired": 0,
    }
    assert harness.storage.deleted == []
    assert asset.status == "READY"


def test_already_terminal_assets_are_not_reprocessed(harness: Harness) -> None:
    """已 `EXPIRED`/`DELETED` 的行不参与（避免对同一个对象重复删除）。"""

    user = harness.uow.users.seed(make_user_row())
    harness.seed_expired_asset(user_id=user.id, key="assets/f/expired", status="EXPIRED")

    report = harness.run()

    assert report.assets_expired == 0
    assert harness.storage.deleted == []


# --------------------------------------------------------------------------- #
# 可见性收敛
# --------------------------------------------------------------------------- #
def test_expired_share_is_closed_and_visibility_restored(harness: Harness) -> None:
    """分享到期 → `EXPIRED`；没有其他有效分享时资产恢复 `PRIVATE`。"""

    user = harness.uow.users.seed(make_user_row())
    asset = harness.seed_expired_asset(
        user_id=user.id, key="assets/g/live", expired_seconds_ago=-10
    )
    asset.expires_at = harness.clock() + timedelta(days=30)  # 资产本身未到期
    slug = harness.share_asset(user_id=user.id, asset=asset)
    assert asset.visibility == "SHARED"

    harness.clock.advance(DEFAULT_SHARE_TTL_DAYS * 24 * 3600 + 1)
    report = harness.run()

    assert report.shares_expired == 1
    assert harness.uow.shares.get_by_slug(slug).status == "EXPIRED"
    assert asset.visibility == "PRIVATE"


def test_other_valid_share_keeps_visibility_shared(harness: Harness) -> None:
    """还有另一条有效分享时不得恢复 PRIVATE，否则会掐断那条链接。"""

    user = harness.uow.users.seed(make_user_row())
    asset = make_media_asset_row(
        user_id=user.id,
            now=harness.clock(),
        purpose="RESULT",
        asset_type="RESULT",
        status="READY",
        expires_at=harness.clock() + timedelta(days=30),
    )
    harness.uow.media.register(asset)
    task = make_generation_task_row(user_id=user.id, status="SUCCEEDED", now=harness.clock())
    harness.uow.generation_tasks.seed(task)
    harness.uow.generation_tasks.create_output(
        GenerationOutputDraft(generation_id=task.id, asset_id=asset.id, output_index=0)
    )

    with harness.uow:
        first = harness.shares.create_share(
            user_id=user.id, payload=_payload(task.id, [asset.id]), idempotency_key="k1"
        )
    # 第二条分享更晚到期
    harness.clock.advance(3600)
    with harness.uow:
        second = harness.shares.create_share(
            user_id=user.id, payload=_payload(task.id, [asset.id]), idempotency_key="k2"
        )

    # 让第一条过期（把它的 expires_at 提前）
    harness.uow.shares.get_by_slug(first.slug).expires_at = harness.clock() - timedelta(seconds=1)
    report = harness.run()

    assert report.shares_expired == 1
    assert asset.visibility == "SHARED"
    assert harness.uow.shares.get_by_slug(second.slug).status == "ACTIVE"


def test_asset_expiry_closes_its_shares_and_keeps_history(harness: Harness) -> None:
    """资产到期清理后：分享收敛为 EXPIRED，任务/流水/分享/审计历史全部保留。

    注意资产**先有效、后到期**：返修第 1 项之后，已到期的资产不允许再被分享
    （那正是「不能分享一张已经清理掉的图」这条不变式），所以这里用可控时钟
    把「创建分享」放在到期之前，再推进时间让资产到期。
    """

    user = harness.uow.users.seed(make_user_row())
    # 资产 60 秒后到期 —— 远早于默认分享 TTL，因此分享期限被收紧到同一时刻
    asset = make_media_asset_row(
        user_id=user.id,
        purpose="RESULT",
        asset_type="RESULT",
        status="READY",
        storage_key="assets/h/expired",
        now=harness.clock(),
        expires_at=harness.clock() + timedelta(seconds=60),
    )
    harness.uow.media.register(asset)
    slug = harness.share_asset(user_id=user.id, asset=asset)
    share = harness.uow.shares.get_by_slug(slug)
    task_count = len(harness.uow.generation_tasks.rows)
    audit_count = len(harness.uow.audit_logs.entries)
    assert asset.visibility == "SHARED"

    harness.clock.advance(61)  # 越过资产到期时刻（分享期限也被收紧到同一点）

    report = harness.run()

    assert report.assets_expired == 1
    assert share.status == "EXPIRED"
    assert harness.uow.media.get(asset.id).visibility == "PRIVATE"
    # 历史保留：没有任何物理删除
    assert len(harness.uow.shares.rows) == 1
    assert len(harness.uow.shares.asset_links()) == 1
    assert len(harness.uow.generation_tasks.rows) == task_count
    assert len(harness.uow.audit_logs.entries) >= audit_count


def test_share_expiry_does_not_promote_private_asset(harness: Harness) -> None:
    """**分享到期**路径：资产被明确设成 PRIVATE 后，关闭分享不得把它提升为 SHARED。

    两条有效分享关联同一资产：关闭其中一条时另一条仍然有效 ——
    旧实现会把资产写成 SHARED（因为"还有有效分享"），于是把一个明确不公开的
    资产重新变成匿名可读。生命周期只拥有 SHARED，不得凭"存在有效分享"去提升。
    """

    user = harness.uow.users.seed(make_user_row())
    asset = make_media_asset_row(
        user_id=user.id,
        purpose="RESULT",
        asset_type="RESULT",
        status="READY",
        now=harness.clock(),
        expires_at=harness.clock() + timedelta(days=30),
    )
    harness.uow.media.register(asset)

    task = make_generation_task_row(user_id=user.id, status="SUCCEEDED", now=harness.clock())
    harness.uow.generation_tasks.seed(task)
    harness.uow.generation_tasks.create_output(
        GenerationOutputDraft(generation_id=task.id, asset_id=asset.id, output_index=0)
    )
    with harness.uow:
        first = harness.shares.create_share(
            user_id=user.id, payload=_payload(task.id, [asset.id]), idempotency_key="p1"
        )
    with harness.uow:
        harness.shares.create_share(
            user_id=user.id, payload=_payload(task.id, [asset.id]), idempotency_key="p2"
        )
    assert asset.visibility == "SHARED"

    # 所有者（或任何有意的发布动作）明确把它设回 PRIVATE
    harness.uow.shares.set_asset_visibility(asset.id, "PRIVATE")
    harness.uow.commit()

    # 让第一条分享到期，跑一轮清理（close_share → revalidate_visibility）
    harness.uow.shares.get_by_slug(first.slug).expires_at = harness.clock() - timedelta(
        seconds=1
    )
    harness.uow.commit()
    report = harness.run()

    assert report.shares_expired == 1
    # 另一条分享仍然有效，但**不得**因此把 PRIVATE 提升回来
    assert harness.uow.media.get(asset.id).visibility == "PRIVATE"


def test_asset_invalidation_does_not_promote_private_asset(harness: Harness) -> None:
    """**媒体失效**路径：资产 PRIVATE 且仍有有效分享时，清理不得提升可见性。

    新锁顺序下清理**不会**在持有资产锁时回头取分享行锁，因此"仍有 ACTIVE 分享"
    的到期资产会被记为 `deferred`（下一轮分享先收敛后再处理）。
    这条用例锁定的是：无论如何收敛，`PRIVATE` 都不会被提升成 `SHARED`。
    """

    user = harness.uow.users.seed(make_user_row())
    asset = make_media_asset_row(
        user_id=user.id,
        purpose="RESULT",
        asset_type="RESULT",
        status="READY",
        storage_key="assets/priv/asset",
        now=harness.clock(),
        expires_at=harness.clock() + timedelta(days=30),
    )
    harness.uow.media.register(asset)

    task = make_generation_task_row(user_id=user.id, status="SUCCEEDED", now=harness.clock())
    harness.uow.generation_tasks.seed(task)
    harness.uow.generation_tasks.create_output(
        GenerationOutputDraft(generation_id=task.id, asset_id=asset.id, output_index=0)
    )
    for key in ("m1", "m2"):
        with harness.uow:
            harness.shares.create_share(
                user_id=user.id, payload=_payload(task.id, [asset.id]), idempotency_key=key
            )
    harness.uow.shares.set_asset_visibility(asset.id, "PRIVATE")
    harness.uow.commit()

    # 注意顺序：`create_share` 会把分享期限**收紧**到选中资产的最早到期时间，
    # 因此不能先把资产改成"已过期"再建分享。这里在创建之后分别改：
    # 分享 1 立刻到期（走分享收敛路径），分享 2 拉长到 60 天（保证"仍有有效分享"），
    # 资产本身过期（走资产失效路径）。
    shares = list(harness.uow.shares.rows)
    shares[0].expires_at = harness.clock() - timedelta(seconds=1)
    shares[1].expires_at = harness.clock() + timedelta(days=60)
    harness.uow.media.get(asset.id).expires_at = harness.clock() - timedelta(seconds=1)
    harness.uow.commit()
    assert (
        len(harness.uow.shares.list_expired(now=harness.clock(), limit=50)) == 1
    ), "构造失败：应当恰好有一条到期分享"

    report = harness.run()

    # 资产失效 ⇒ **两条**分享都被收敛（分享 1 自身到期，分享 2 引用失效资产），
    # 资产随后可清理。这正是第四轮修掉的"持续延期"：
    # 旧实现只看分享自身期限，分享 2 的期限在未来就会被永久延后。
    assert report.shares_expired == 2, report
    assert report.assets_expired == 1, report
    assert harness.uow.media.get(asset.id).visibility == "PRIVATE"


def test_share_lifecycle_is_idempotent(harness: Harness) -> None:
    """重复运行不再对已终态的分享/资产重复动作。"""

    user = harness.uow.users.seed(make_user_row())
    asset = harness.seed_expired_asset(user_id=user.id, key="assets/i/once")

    first = harness.run()
    second = harness.run()

    assert first.assets_expired == 1
    assert second.assets_expired == 0
    assert harness.storage.deleted == ["assets/i/once"]


def test_share_lifecycle_helper_reports_valid_share(harness: Harness) -> None:
    """`ShareLifecycle.has_other_valid_share` 的排除语义（当前这条不算）。"""

    user = harness.uow.users.seed(make_user_row())
    asset = make_media_asset_row(
        user_id=user.id,
            now=harness.clock(),
        purpose="RESULT",
        asset_type="RESULT",
        status="READY",
        expires_at=harness.clock() + timedelta(days=30),
    )
    harness.uow.media.register(asset)
    task = make_generation_task_row(user_id=user.id, status="SUCCEEDED", now=harness.clock())
    harness.uow.generation_tasks.seed(task)
    harness.uow.generation_tasks.create_output(
        GenerationOutputDraft(generation_id=task.id, asset_id=asset.id, output_index=0)
    )
    with harness.uow:
        view = harness.shares.create_share(
            user_id=user.id, payload=_payload(task.id, [asset.id]), idempotency_key="k1"
        )
    share = harness.uow.shares.get_by_slug(view.slug)
    lifecycle = ShareLifecycle(uow=harness.uow, clock=harness.clock)

    assert lifecycle.has_other_valid_share(asset.id) is True
    assert lifecycle.has_other_valid_share(asset.id, excluding=share.id) is False
