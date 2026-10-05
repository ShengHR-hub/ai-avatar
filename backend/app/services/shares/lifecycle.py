"""媒体与分享的生命周期收敛（ENG-09）。

## 全局取锁顺序（本卡唯一的并发约定）

本卡涉及两类行：`shares` 与 `media_assets`。四条路径（创建 / 撤销 / 分享到期 /
媒体清理）**必须**按同一顺序取锁，否则会出现「一方持资产等分享、另一方持分享等
资产」的真实死锁（总控已在隔离 PostgreSQL 上复现：清理持资产锁、撤销持分享锁，
随后互等，撤销抛 `DeadlockDetected`）。

```text
第 0 级  咨询锁 (user_id, idempotency_key)   —— 只在 create_share / submit 最前面
第 1 级  shares 行锁，**按 id 升序**         —— lock_share_ids / lock_active_for_asset
第 2 级  media_assets 行锁，**按 id 升序**    —— lock_assets_by_id
第 3 级  状态与可见性写入
```

**硬规则：任何事务都不得在持有第 2 级锁之后再取第 1 级锁。**
因此清理不能"先锁一批资产、再逐个去找它们的分享"——那是死锁配方。
`MediaLifecycleService.run_once` 因此被拆成「分享收敛」与「资产清理」两个阶段
（见该方法文档）。分享收敛阶段的目标集合包含

```text
到期分享 ∪ 引用了失效资产的 ACTIVE 分享
失效资产 = 逻辑删除 ∪ 状态 DELETED/EXPIRED ∪ 已过 expires_at
```

因为第三轮只收敛"自身到期"的分享，于是「资产已到期、但分享期限在未来或为 NULL、
或是没有 share_assets 行的历史单图分享」会**永远**落到资产阶段的 `deferred` 分支，
公开链接一直可读。第四轮把这三类纳入同一批目标（历史单图按 `shares.asset_id` 回落）。

## 第五轮：预读只用来"锁谁"，结论一律在锁内重下

第四轮的目标集合仍把预读结果当成结论：预读说这条分享该关，持锁后就**无条件**关。
两个真实缺陷由此而来（总控在隔离 PostgreSQL 上复现）：

1. 预读之后资产期限被改回未来 → 分享仍被关闭；
2. 另一事务通过正式 `ShareService` 撤销了这条分享 → 清理把 `REVOKED` 覆盖成 `EXPIRED`。

现在分享阶段在**持有分享行锁与资产行锁之后**重新判定：

```text
仍是 ACTIVE？
  否 → 不动（REVOKED/EXPIRED 是别人的裁决，绝不复写）
  是 → 自身到期（expires_at <= now）？ 或 引用了此刻失效的资产？
        是 → 关闭；否 → 保持 ACTIVE
```

资产事实由 `lock_assets_by_id` 的 `populate_existing` 刷新后再判定（`asset_is_invalid`），
因此"期限被改回未来"会被如实看到。取锁顺序不变（分享升序 → 资产升序），
重裁决不引入新的取锁方向。

资产阶段仍然把「锁内确认仍被 ACTIVE 分享引用」的资产记为 `deferred`，
但那只可能来自"预读之后新写入的分享"（并发创建），并且该分享会在后续轮次
进入分享收敛阶段 —— 目标是**按 id 升序且有界**的，所以**每个候选都会在有限轮次内
被轮到**，不存在长期饿死；这不是"一轮内一定收敛"的承诺。

## 可见性归属

生命周期只拥有 `SHARED`：`PRIVATE` 是默认值、`PUBLIC` 是有意发布，两者都不由
本模块写入。因此 `revalidate_visibility` 的唯一动作是
「`SHARED` 且已无有效分享 → `PRIVATE`」，**不会**把 `PRIVATE` 提升为 `SHARED`
（那会让一个明确不想公开的资产重新可读），也不会改写 `PUBLIC`。

## 其它纪律

顺序纪律（技术负责人 2026-10-02 裁决 + `04-ai-provider.md` 第 7 节）：

```text
1. 按 media_assets.expires_at 有界批量选出到期资产（每批**最多 50 条**）
2. 先经 StorageAdapter.delete() 删除对象
   - 成功           → 才把状态改为 EXPIRED
   - 对象不存在      → 等同于删除成功（delete 对不存在 key 幂等成功）
   - 存储不可用/其它 → **保持原状态**，留待下一次批次重试，并继续处理后面的资产
3. 资产失效后收敛其分享；仅当该资产没有其他有效分享且**当前是 SHARED** 时才恢复 PRIVATE
4. 收敛到期的 ACTIVE 分享（一条分享的**全部**关联资产都收敛，不只主资产）
```

不可违背的边界：

- **不做物理删除**：`media_assets` 只置 `EXPIRED`，任务、流水、Provider 记录、
  分享、反馈与审计全部保留（`02-database-design.md` 第 4 节）；
- 单项失败不阻塞整批：每个资产独立处理，失败只计数并继续；
- 先删对象再改状态：任何一步失败都不得让数据库显示「已清理而对象还在」；
- 只经 `StorageAdapter` 接触对象存储，本模块不导入任何 SDK。

**工作单元生命周期**：本模块只持有 `uow_factory`，每次 `run_once` 都用 `with`
进入**独立**工作单元：正常结束 `close()` 释放 Session，异常 `rollback()` 后再关闭。

**批量上限**：`MAX_CLEANUP_BATCH_SIZE = 50` 是硬约束，构造期与调用期都会被收紧。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from app.errors.exceptions import ApiError
from app.repositories import UnitOfWork
from app.repositories.engagement import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_SHARED,
)
from app.storage import ObjectNotFound, StorageAdapter, StorageError, StorageUnavailable

__all__ = [
    "DEFAULT_CLEANUP_BATCH_SIZE",
    "MAX_CLEANUP_BATCH_SIZE",
    "CleanupReport",
    "MediaLifecycleService",
    "ShareLifecycle",
]

_LOGGER = logging.getLogger("ai_avatar.lifecycle")

#: 每批最多处理的对象数（技术负责人 2026-10-02 裁决：**每批最多 50 条**，硬上限）
MAX_CLEANUP_BATCH_SIZE = 50
DEFAULT_CLEANUP_BATCH_SIZE = MAX_CLEANUP_BATCH_SIZE

#: 资产的**终结**状态（与 `media_assets` 的 CHECK 同源语义）。
#: 终结资产的对象早已删除：它仍然是"失效资产"（引用它的分享要收敛），
#: 但**绝不允许**再进一次「删对象 → 置 EXPIRED」的路径。
_TERMINAL_ASSET_STATUSES = ("DELETED", "EXPIRED")


def asset_is_invalid(asset: object | None, *, now: datetime) -> bool:
    """资产**此刻**是否已失效（不可公开，且引用它的分享必须收敛）。

    判定口径与 `ShareService._asset_is_servable` 的反面一致，但这里额外覆盖
    「对象已删、状态已终结」两种情况 —— 清理跑过之后资产就是这两种状态，
    只按"未终结 + 已到期"筛会让公开链接永远留着。

    - 行不存在 → 失效（分享引用的对象已经没了）；
    - `deleted_at` 非空，或状态是 `DELETED`/`EXPIRED` → 已终结；
    - `expires_at IS NOT NULL AND expires_at <= now` → 已到期（边界取 `<=`，
      与 `effective_expired` / `ShareLifecycle.is_expired` 同一口径）；
    - `expires_at IS NULL` 且未终结 → 仍然有效（持久资源不因时间失效）。

    这是**纯判定**：不写库、不锁行。调用方必须在**持锁并刷新事实之后**调用它，
    否则会用预读快照下结论（那正是「预读后期限恢复为未来、分享仍被关闭」的成因）。
    """

    if asset is None:
        return True
    if getattr(asset, "deleted_at", None) is not None:
        return True
    if getattr(asset, "status", None) in _TERMINAL_ASSET_STATUSES:
        return True
    deadline = getattr(asset, "expires_at", None)
    if deadline is None:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= now


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """一次生命周期运行的计数摘要（可安全记录日志：只含数量，不含 key）。"""

    assets_expired: int = 0
    assets_deferred: int = 0
    assets_absent: int = 0
    shares_expired: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "assets_expired": self.assets_expired,
            "assets_deferred": self.assets_deferred,
            "assets_absent": self.assets_absent,
            "shares_expired": self.shares_expired,
        }


class ShareLifecycle:
    """分享状态的收敛与可见性恢复（`MediaLifecycleService` 与 `ShareService` 共用）。"""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow = uow
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )

    # ------------------------------------------------------------------ 判定
    def is_expired(self, share) -> bool:
        if share.expires_at is None:
            return False
        return share.expires_at <= self._moment()

    def has_other_valid_share(
        self,
        asset_id: uuid.UUID,
        *,
        excluding: uuid.UUID | None = None,
        closing: frozenset[uuid.UUID] | None = None,
    ) -> bool:
        """该资产是否还有**其他**有效分享。

        `excluding` 排除单条、`closing` 排除**本批本次要关闭的全部**分享：
        批量收敛时必须把整批都排除掉，否则「同一批里还有一条尚未轮到关闭」会让
        判定以为"仍有有效分享"，把该恢复 PRIVATE 的资产留成 SHARED。

        可见性判定与"是否有效"共用 `is_expired`，口径只有一处。
        """

        for candidate in self._uow.shares.list_active_for_asset(asset_id):
            if excluding is not None and candidate.id == excluding:
                continue
            if closing is not None and candidate.id in closing:
                continue
            if not self.is_expired(candidate):
                return True
        return False

    def closed_share_asset_ids(self, share) -> tuple[uuid.UUID, ...]:
        """一条分享**关联过的全部资产**（关联为空时回落主资产）。

        失效收敛必须按这个集合重算可见性：只看 `share.asset_id` 会把
        「分享两张、只恢复一张」的第二张留在没有依据的公开读口上。
        """

        linked = self._uow.shares.list_selected_asset_ids(share.id)
        if linked:
            return tuple(linked)
        return (share.asset_id,)

    # ------------------------------------------------------------------ 写入
    def revalidate_visibility(
        self,
        asset_id: uuid.UUID,
        *,
        closing: frozenset[uuid.UUID] | None = None,
    ) -> None:
        """按「是否还有有效分享」**收敛**可见性；绝不提升，也不改写 PUBLIC。

        **本模块只拥有 `SHARED`**：`PRIVATE` 是默认值、`PUBLIC` 是有意发布
        （`04-ai-provider.md` 第 8 节），两者都不是生命周期该写的东西。
        因此这里的动作只有两种：

        - 资产是 `SHARED` 且已经没有任何有效分享 → 恢复 `PRIVATE`（唯一的写）；
        - 其他情况 → **保持原样**（`PRIVATE` 不会被提升成 `SHARED`，`PUBLIC` 不动）。

        「新建分享时把资产置 `SHARED`」是 `ShareService` 的职责，它有创建意图
        作为依据；生命周期只是收敛，不能凭"存在一条有效分享"就把别人设成
        `PRIVATE` 的资产重新公开 —— 那会让一个明确不想公开的资产重新可读。

        `has_other_valid_share` 的判定在**资产行锁 + 刷新后的事实**下完成，
        因此并发的新建分享要么被看到（维持 `SHARED`），要么发生在我们之后。
        """

        asset = self._lock_asset_fresh(asset_id)
        if asset is None:
            return
        if asset.visibility != STATUS_SHARED:
            # PRIVATE / PUBLIC：生命周期不拥有，保持原样
            return
        if not self.has_other_valid_share(asset_id, closing=closing):
            self._uow.shares.set_asset_visibility(asset_id, "PRIVATE")

    def _lock_asset_fresh(self, asset_id: uuid.UUID):
        """按唯一取锁顺序锁住一张资产行，并清掉 identity map 里的陈旧快照。"""

        locked = self._uow.shares.lock_assets_by_id([asset_id])
        if not locked:
            return None
        asset = locked[0]
        # 行锁已经拿到，但 Session 里可能还缓存着过期前读到的同一行：
        # 让下一次属性访问从数据库重新取值，避免用快照做可见性判断
        self._uow.shares.refresh_asset(asset)
        return asset

    def close_share(
        self,
        share,
        *,
        status: str = STATUS_EXPIRED,
        closing: frozenset[uuid.UUID] | None = None,
    ) -> int:
        """只落状态：关闭一条分享（`EXPIRED`/`REVOKED`）。不提交。

        调用方**必须已经持有该分享的行锁**（`lock_share_ids`）。
        可见性收敛由 `revalidate_assets` 在**整批资产锁**之后统一做，
        这样事务内的资产取锁顺序恒为升序 —— 逐条 `close_share` 各锁各的资产
        会形成「分享1→资产10、分享2→资产5」这种高→低顺序，与创建分享的
        升序顺序交叉即死锁。

        **只从 `ACTIVE` 出发**：`REVOKED` 是用户/`ShareService` 的明确裁决，
        `EXPIRED` 是已经收敛过的终态，两者都不允许被清理覆盖（总控复现过
        「另一事务撤销后，清理把 `REVOKED` 覆盖成 `EXPIRED`」）。状态不是
        `ACTIVE` 时本方法什么都不写，返回 0。
        """

        if share.status != STATUS_ACTIVE:
            return 0
        self._uow.shares.set_status(share, status, changed_at=self._moment())
        return len(self.closed_share_asset_ids(share))

    def revalidate_assets(
        self,
        asset_ids: Iterable[uuid.UUID],
        *,
        closing: frozenset[uuid.UUID] | None = None,
    ) -> None:
        """**一次性**锁住整批资产（按 id 升序）并收敛可见性。不提交。

        这是本模块唯一的资产取锁入口：整批一起排好序再锁，事务内不会出现
        第二个方向。
        """

        ordered = sorted(set(asset_ids), key=str)
        if not ordered:
            return
        for asset in self._uow.shares.lock_assets_by_id(ordered):
            self._uow.shares.refresh_asset(asset)
            if asset.visibility != STATUS_SHARED:
                # PRIVATE / PUBLIC：生命周期不拥有，保持原样
                continue
            if not self.has_other_valid_share(asset.id, closing=closing):
                self._uow.shares.set_asset_visibility(asset.id, "PRIVATE")

    def lock_and_close_shares(
        self,
        share_ids: Iterable[uuid.UUID],
        *,
        status: str = STATUS_EXPIRED,
    ) -> tuple[object, ...]:
        """按 id 升序锁定并关闭一批分享，返回被关闭的行。不提交。"""

        ordered = sorted(set(share_ids), key=str)
        if not ordered:
            return ()
        rows = self._uow.shares.lock_share_ids(ordered)
        for share in rows:
            self.close_share(share, status=status)
        return tuple(rows)

    def expire_due_shares(self, *, limit: int) -> int:
        """把已到期的 ACTIVE 分享收敛为 `EXPIRED`，并收敛关联资产可见性。

        锁顺序：分享行（**按 id 升序**）→ 整批资产行（**按 id 升序**）。
        """

        rows = self._uow.shares.lock_expired_shares(now=self._moment(), limit=limit)
        if not rows:
            return 0
        closing = frozenset(row.id for row in rows)
        affected: list[uuid.UUID] = []
        for row in rows:
            affected.extend(self.closed_share_asset_ids(row))
        # 先关闭分享状态，再统一锁资产并收敛：资产锁顺序只由这里决定
        for row in rows:
            self.close_share(row, status=STATUS_EXPIRED)
        self.revalidate_assets(affected, closing=closing)
        return len(rows)

    def _moment(self) -> datetime:
        return self._clock().astimezone(timezone.utc)


class MediaLifecycleService:
    """到期媒体的有界批量清理。

    只持有 `uow_factory`：每个阶段用独立工作单元，退出即释放 Session。
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        storage: StorageAdapter,
        clock: Callable[[], datetime] | None = None,
        batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size 必须是正整数。")
        self._uow_factory = uow_factory
        self._storage = storage
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # 硬上限：裁决值是「每批最多 50 条」，构造期传入更大值也会被收紧
        self._batch_size = self._bounded(batch_size)

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def run_once(self, *, batch_size: int | None = None) -> CleanupReport:
        """执行一批清理；返回计数摘要。

        ## 事务级锁顺序

        本卡唯一的顺序是 `咨询锁 → 分享行（id 升序）→ 资产行（id 升序）`，
        且**整个事务**都必须遵守 —— 不只是"单次查询排了序"。
        每一批都先把**目标集合的 id 全部排序、一次性锁住**，再动任何状态：

        ```text
        段 1（分享收敛）
          1. 预读：到期 ACTIVE 分享 ∪ 「引用失效资产的 ACTIVE 分享」（都不加锁，只取 id）
          2. 一次性按 id 升序锁住这些分享      → lock_share_ids（populate_existing 刷新事实）
          3. 一次性按 id 升序锁住资产候选      → lock_assets_by_id（同样刷新）
          4. **锁内重新裁决**：仍是 ACTIVE 且（自身到期 或 引用失效资产）才真的关闭
          5. 关闭分享状态 + 统一收敛这些资产的可见性

        段 2（资产清理）
          1. 预读到期资产候选（不加锁，≤ batch；只含未终结状态）
          2. 一次性按 id 升序锁住它们
          3. 逐个：仍被 ACTIVE 分享引用 → deferred；否则先删对象、成功/不存在才置 EXPIRED
        ```

        「先分享后资产」+「各自升序」两条同时成立，事务内就不存在第二个取锁方向，
        因此与 `create_share`（资产升序）和 `revoke_share`（分享→资产）都不会成环。
        逐条关闭分享时各自锁各自的资产是**不行的**：那会形成「分享1→资产10、
        分享2→资产5」这种高→低顺序。

        ## 预读不是结论：锁内重裁决（第五轮）

        预读只回答"锁谁"。真正关哪些分享，在**同时持有分享行锁与资产行锁**、
        并且两边的行都被 `populate_existing` 刷新之后才决定：

        - 分享已经不是 `ACTIVE`（被 `ShareService` 撤销、或被别的轮次关闭）→ 跳过，
          绝不把 `REVOKED` 覆盖成 `EXPIRED`；
        - 分享期限被改回未来 → `is_expired` 为假，不再关闭；
        - 引用的资产期限被改回未来 → `asset_is_invalid` 为假，不再关闭。

        因此「预读之后资产期限恢复为未来、分享仍被关闭」这条真实缺陷不会重现。

        ## 到期资产不再无限期延期

        段 1 的目标集合包含「**引用了失效资产的 ACTIVE 分享**」，与分享自身期限无关。
        因此分享期限是未来、为 `NULL`、或历史单图回落（没有 `share_assets` 关联、
        只有 `shares.asset_id`）都能被收敛。失效资产的口径包含**已终结状态**
        （`DELETED`/`EXPIRED`，对象早已删除），因此「资产清理过、分享还 ACTIVE」也会被收敛；
        终结资产的对象不会因此被再次删除 —— 段 2 的候选来自 `peek_expired`，
        它只取未终结状态。

        段 2 仍然把「锁内确认仍被 ACTIVE 分享引用」的资产记为 `deferred`，
        但那种情况只可能来自"预读之后新写入的分享"（例如并发创建）；
        它会在后续轮次被段 1 收敛 —— 段 1 的目标按 id 升序且有界，
        因此每个候选都能在有限轮次内轮到，不存在长期饿死。

        ## 其它边界

        - 单项存储失败**不中断整批**，且**不改该资产状态**（留待下轮重试）；
        - 先 `StorageAdapter.delete()` 成功（或对象不存在）才把状态改为 `EXPIRED`；
        - 批量上限硬约束 50；历史（任务、流水、分享、反馈、审计）一律保留。
        """

        limit = self._bounded(batch_size or self._batch_size)
        moment = self._moment()

        # --- 段 1：分享收敛（到期分享 ∪ 引用失效资产的 ACTIVE 分享）---------
        shares_expired = 0
        with self._unit_of_work() as uow:
            lifecycle = ShareLifecycle(uow=uow, clock=self._clock)

            # 1a) 预读：到期分享（按 id）。**只用来决定"锁谁"**，不作结论。
            due_ids = [
                row.id
                for row in uow.shares.peek_expired_shares(now=moment, limit=limit)
            ]

            # 1b) 预读：**引用了失效资产**的 ACTIVE 分享（按 id，最多 limit 条）。
            #     以分享为行是刻意的：失效资产里既有"还没收敛的"，也有"早已收敛、
            #     分享已经 EXPIRED"的；若先取一批资产再反查分享，后者会一直占满
            #     批次头部，让后面的分享永远轮不到（真实隔离库上复现过）。
            invalid_share_ids = [
                row.id
                for row in uow.shares.peek_active_for_invalid_assets(
                    now=moment, limit=limit
                )
            ]

            # 1c) 失效资产（含已终结状态）：只用于把可见性收敛范围补全，
            #     不承担"锁谁"的驱动职责（那是 1b 的事）。
            invalid_asset_ids = [
                row.id for row in uow.media.peek_invalid(now=moment, limit=limit)
            ]

            target_share_ids = sorted(
                set(due_ids) | set(invalid_share_ids), key=str
            )[:limit]
            if target_share_ids or invalid_asset_ids:
                # 2) 一次性按 id 升序锁住分享（`populate_existing` → 锁内读到提交后的事实）
                locked = (
                    uow.shares.lock_share_ids(list(target_share_ids))
                    if target_share_ids
                    else ()
                )

                # 3) 一次性按 id 升序锁住资产候选（这些分享关联过的资产 ∪ 预读到的失效资产）
                asset_candidates: set[uuid.UUID] = set(invalid_asset_ids)
                for row in locked:
                    asset_candidates.update(lifecycle.closed_share_asset_ids(row))
                locked_assets = {
                    asset.id: asset
                    for asset in uow.shares.lock_assets_by_id(
                        sorted(asset_candidates, key=str)
                    )
                }

                # 4) **锁内重新裁决**：预读只是候选，真正关谁要看锁后的数据库事实。
                #    - 已被别的路径改成 REVOKED/EXPIRED → 不动（不得覆盖别人的裁决）；
                #    - 分享期限被改回未来、或引用的资产期限被改回未来 → 不再关闭；
                #    - 仍是 ACTIVE 且（自身到期 或 引用了此刻失效的资产）→ 关闭。
                to_close = [
                    row
                    for row in locked
                    if row.status == STATUS_ACTIVE
                    and (
                        lifecycle.is_expired(row)
                        or any(
                            asset_is_invalid(locked_assets.get(asset_id), now=moment)
                            for asset_id in lifecycle.closed_share_asset_ids(row)
                        )
                    )
                ]
                closing = frozenset(row.id for row in to_close)

                # 5) 可见性收敛范围 = 被关闭分享的全部关联资产 ∪ 此刻确实失效的资产。
                #    后者即使**没有**任何 ACTIVE 分享引用，也必须是 `PRIVATE`：
                #    失效资产留在 `SHARED` 上等于留了一个没有依据的公开读口。
                affected: set[uuid.UUID] = set()
                for row in to_close:
                    affected.update(lifecycle.closed_share_asset_ids(row))
                affected.update(
                    asset_id
                    for asset_id, asset in locked_assets.items()
                    if asset_is_invalid(asset, now=moment)
                )
                lifecycle.revalidate_assets(affected, closing=closing)

                # 6) 关闭状态（只写 status，不再各自去锁资产）
                for row in to_close:
                    if lifecycle.close_share(row, status=STATUS_EXPIRED):
                        shares_expired += 1
            uow.commit()

        # --- 段 2：资产清理 --------------------------------------------------
        expired = deferred = absent = 0
        with self._unit_of_work() as uow:
            candidates = sorted(
                (row.id for row in uow.media.peek_expired(now=moment, limit=limit)),
                key=str,
            )
            for asset in uow.media.lock_assets_by_id(candidates):
                # 锁内重算：只有此刻确实到期、且仍是未终结状态才处理
                if asset.expires_at is None or asset.expires_at > moment:
                    continue
                if asset.deleted_at is not None or asset.status not in (
                    "PENDING_UPLOAD",
                    "READY",
                ):
                    continue
                if uow.shares.count_active_for_asset(asset.id) > 0:
                    # 仍被 ACTIVE 分享引用：留给后续轮次由段 1 收敛
                    # （这里**不**回头取分享行锁，否则重新引入死锁）
                    deferred += 1
                    continue
                outcome = self._delete_object(asset)
                if outcome == "deferred":
                    deferred += 1
                    continue
                if outcome == "absent":
                    absent += 1
                uow.media.set_status(asset, "EXPIRED")
                ShareLifecycle(uow=uow, clock=self._clock).revalidate_visibility(
                    asset.id
                )
                expired += 1
            uow.commit()

        report = CleanupReport(
            assets_expired=expired,
            assets_deferred=deferred,
            assets_absent=absent,
            shares_expired=shares_expired,
        )
        if any(report.as_dict().values()):
            _LOGGER.info("lifecycle_cleanup %s", report.as_dict())
        return report

    # ------------------------------------------------------------------ 内部
    @contextmanager
    def _unit_of_work(self) -> Iterator[UnitOfWork]:
        """独立工作单元：异常回滚，退出即关闭（释放 Session）。"""

        uow = self._uow_factory()
        with uow:
            try:
                yield uow
            except Exception:
                uow.rollback()
                raise

    @staticmethod
    def _bounded(requested: int | None) -> int:
        """把批量上限夹到 `MAX_CLEANUP_BATCH_SIZE`（传更大值也只会处理 50 条）。"""

        if requested is None:
            return MAX_CLEANUP_BATCH_SIZE
        try:
            value = int(requested)
        except (TypeError, ValueError):
            return MAX_CLEANUP_BATCH_SIZE
        if value <= 0:
            return MAX_CLEANUP_BATCH_SIZE
        return min(value, MAX_CLEANUP_BATCH_SIZE)

    def _delete_object(self, asset) -> str:
        """删除对象；返回 `deleted` / `absent` / `deferred`。

        - `ObjectNotFound`：对象已不存在 → 视为删除成功（继续改状态）；
        - `StorageUnavailable` 与其他 `StorageError`：**不改状态**，下批重试；
        - 任何意外异常同样只延迟这一条，不打断整批。
        """

        try:
            self._storage.delete(asset.storage_key)
        except ObjectNotFound:
            return "absent"
        except StorageUnavailable:
            _LOGGER.warning("lifecycle_delete_deferred reason=unavailable")
            return "deferred"
        except StorageError:
            _LOGGER.warning("lifecycle_delete_deferred reason=storage_error")
            return "deferred"
        except ApiError:  # pragma: no cover - 端口不抛 ApiError，防御性保留
            raise
        except Exception:  # noqa: BLE001 - 单项失败绝不阻塞整批
            _LOGGER.warning("lifecycle_delete_deferred reason=unexpected")
            return "deferred"
        return "deleted"

    def _moment(self) -> datetime:
        return self._clock().astimezone(timezone.utc)
