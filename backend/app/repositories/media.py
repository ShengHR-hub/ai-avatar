"""媒体资源元数据数据访问（ENG-03 只读子集 + ENG-05 上传生命周期）。

分层约定（`06-development-standard.md` 第 2 节）：

- 只做 SQLAlchemy 查询、行锁与字段赋值，**不自行 `commit`**；事务由 Service/`UnitOfWork` 持有；
- 不判定业务规则（谁可以读、校验是否通过、TTL 多长都由 `app.services.assets` 决定）；
- `lock_by_id` 用于「完成上传」与「删除」这类会改写同一行的入口，避免并发重复校验或重复删除；
- 只写 `status` 与元数据，**绝不写 `asset_type` 之外的历史字段语义**，也不物理删除任何行
  （`02-database-design.md` 第 4 节：媒体元数据不物理删除，只清理对象二进制）。

本卡不启动 PostgreSQL，`SqlAlchemy*` 实现由基础设施集成阶段验证；
单元测试使用 `tests/storage` 与 `tests/services/test_assets.py` 的内存替身。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models.media import ASSET_STATUSES, MediaAsset

__all__ = [
    "MediaAssetDraft",
    "MediaAssetRepository",
    "SqlAlchemyMediaAssetRepository",
]

#: 生命周期状态字面量取自模型层的 CHECK 同源元组，避免两处各写一遍
(
    _PENDING_UPLOAD,
    _READY,
    _DELETED,
    _EXPIRED,
) = ASSET_STATUSES

#: 未终结（还可能被清理）的状态：只有它们会进「删除对象 → 置 EXPIRED」这条路径。
_ACTIVE_STATUSES = (_PENDING_UPLOAD, _READY)

#: 已经终结的状态：对象早已删过，**绝不允许**再次删除，但仍是"失效资产"，
#: 引用它们的分享必须被收敛（见 `peek_invalid`）。
_TERMINAL_STATUSES = (_DELETED, _EXPIRED)


@dataclass(frozen=True, slots=True)
class MediaAssetDraft:
    """一条待创建的媒体资源行（上传意图阶段）。"""

    user_id: uuid.UUID
    asset_type: str
    purpose: str
    storage_provider: str
    storage_key: str
    mime_type: str
    size_bytes: int
    sha256: str
    idempotency_key: str
    expires_at: datetime


@runtime_checkable
class MediaAssetRepository(Protocol):
    """媒体资源仓储端口。"""

    def find_owner(self, asset_id: uuid.UUID) -> uuid.UUID | None: ...

    def get(self, asset_id: uuid.UUID) -> MediaAsset | None: ...

    def lock_by_id(self, asset_id: uuid.UUID) -> MediaAsset | None:
        """以 `SELECT ... FOR UPDATE` 锁定资源行（完成上传与删除入口使用）。"""
        ...

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> MediaAsset | None: ...

    def create(self, draft: MediaAssetDraft) -> MediaAsset: ...

    def mark_ready(
        self,
        asset: MediaAsset,
        *,
        mime_type: str,
        size_bytes: int,
        width: int,
        height: int,
    ) -> MediaAsset: ...

    def mark_deleted(self, asset: MediaAsset, *, deleted_at: datetime) -> MediaAsset: ...

    def set_status(self, asset: MediaAsset, status: str) -> MediaAsset: ...

    def list_expired(
        self, *, now: datetime, limit: int
    ) -> tuple[MediaAsset, ...]:
        """到期但尚未清理的资产（生命周期批量的选取口径），**按 id 升序加行锁**。

        「到期」= `expires_at IS NOT NULL AND expires_at <= now`；已逻辑删除的行不再
        参与（`mark_deleted` 已经把对象删掉了）。只包含 `PENDING_UPLOAD`/`READY`
        两种未终结状态，避免对已 `EXPIRED` 的行重复删除对象。

        `ORDER BY id`（而不是到期时间）是**加锁顺序**要求：全聚合只允许一种资产
        行的取锁顺序，见 `app/services/shares/lifecycle.py` 模块头。
        """
        ...

    def peek_invalid(self, *, now: datetime, limit: int) -> tuple[MediaAsset, ...]:
        """已失效资产（**不加锁**，按 id 升序）：逻辑删除、已终结（DELETED/EXPIRED），
        或已过到期时间。

        只用于**分享收敛**的目标选取；资产清理另有 `peek_expired`（限未终结状态），
        因此终结资产不会被重复删除对象。
        """
        ...

    def peek_expired(self, *, now: datetime, limit: int) -> tuple[MediaAsset, ...]:
        """到期候选（**不加锁**，按最早到期优先）。

        清理先用它知道"处理谁"，再按统一顺序加锁；候选阶段取锁会制造
        「持资产锁等分享锁」的死锁。候选过期是良性的：加锁阶段会重新校验。
        """
        ...

    def lock_assets_by_id(self, asset_ids: list[uuid.UUID]) -> tuple[MediaAsset, ...]:
        """按传入顺序一次锁住多张资产行（调用方必须传升序列表）。"""
        ...

class SqlAlchemyMediaAssetRepository:
    """`MediaAssetRepository` 的 SQLAlchemy 实现（不提交事务）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------- 只读
    def find_owner(self, asset_id: uuid.UUID) -> uuid.UUID | None:
        statement = select(MediaAsset.user_id).where(MediaAsset.id == asset_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def get(self, asset_id: uuid.UUID) -> MediaAsset | None:
        statement = select(MediaAsset).where(MediaAsset.id == asset_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def lock_by_id(self, asset_id: uuid.UUID) -> MediaAsset | None:
        statement = (
            select(MediaAsset).where(MediaAsset.id == asset_id).with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> MediaAsset | None:
        statement = (
            select(MediaAsset)
            .where(
                MediaAsset.user_id == user_id,
                MediaAsset.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    # ------------------------------------------------------------------- 写入
    def create(self, draft: MediaAssetDraft) -> MediaAsset:
        asset = MediaAsset(
            user_id=draft.user_id,
            asset_type=draft.asset_type,
            purpose=draft.purpose,
            status=_PENDING_UPLOAD,
            visibility="PRIVATE",
            storage_provider=draft.storage_provider,
            storage_key=draft.storage_key,
            mime_type=draft.mime_type,
            size_bytes=draft.size_bytes,
            sha256=draft.sha256,
            idempotency_key=draft.idempotency_key,
            expires_at=draft.expires_at,
        )
        self._session.add(asset)
        self._session.flush()
        return asset

    def mark_ready(
        self,
        asset: MediaAsset,
        *,
        mime_type: str,
        size_bytes: int,
        width: int,
        height: int,
    ) -> MediaAsset:
        """写入校验后的最终元数据并置 `READY`（观测值覆盖声明值）。"""

        asset.status = _READY
        asset.mime_type = mime_type
        asset.size_bytes = size_bytes
        asset.width = width
        asset.height = height
        self._session.flush()
        return asset

    def mark_deleted(self, asset: MediaAsset, *, deleted_at: datetime) -> MediaAsset:
        """逻辑删除：只写 `deleted_at` 与 `DELETED`，元数据与历史保留。"""

        asset.status = _DELETED
        asset.deleted_at = deleted_at
        self._session.flush()
        return asset

    def set_status(self, asset: MediaAsset, status: str) -> MediaAsset:
        asset.status = status
        self._session.flush()
        return asset

    def list_expired(self, *, now: datetime, limit: int) -> tuple[MediaAsset, ...]:
        """到期未清理的资产，**按 id 升序加行锁**。

        `ORDER BY` 刻意不是到期时间：这是**加锁顺序**，必须与全聚合唯一的顺序
        （资产 id 升序，见 `app/services/shares/lifecycle.py` 模块头）一致，
        否则同样的 `FOR UPDATE` 在不同路径上会以不同顺序取锁，重新引入死锁。
        「最早到期的先处理」由 `peek_expired`（无锁候选）负责。
        """

        statement = (
            select(MediaAsset)
            .where(
                MediaAsset.expires_at.is_not(None),
                MediaAsset.expires_at <= now,
                MediaAsset.deleted_at.is_(None),
                MediaAsset.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(MediaAsset.id.asc())
            .limit(limit)
            # 行锁：并发清理与「分享写入时锁资产行」必须落在同一批行上。
            # 没有它时两轮清理会同时读到同一批到期行，于是同一个对象被删两次
            # （真实 PostgreSQL 用例 `test_concurrent_cleanup_*` 就是这条的回归）。
            .with_for_update()
        )
        return tuple(self._session.execute(statement).scalars().all())

    def peek_expired(self, *, now: datetime, limit: int) -> tuple[MediaAsset, ...]:
        """到期未清理的资产候选（**不加锁**，按最早到期优先）。

        用途：清理需要先知道"处理谁"，再按统一的 id 升序去加锁。
        不加锁是刻意的 —— 候选过期是良性的（候选行在加锁阶段会被重新校验），
        而在候选阶段取锁恰恰是「持资产锁等分享锁」这个死锁的来源。
        """

        statement = (
            select(MediaAsset)
            .where(
                MediaAsset.expires_at.is_not(None),
                MediaAsset.expires_at <= now,
                MediaAsset.deleted_at.is_(None),
                MediaAsset.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(MediaAsset.expires_at.asc(), MediaAsset.id.asc())
            .limit(limit)
        )
        return tuple(self._session.execute(statement).scalars().all())

    def peek_invalid(self, *, now: datetime, limit: int) -> tuple[MediaAsset, ...]:
        """**已失效**的资产（不加锁），按 id 升序。

        「失效」= 已逻辑删除（`deleted_at` 非空 **或** 状态 `DELETED`）、
        状态 `EXPIRED`（对象已删、只留历史），或已过 `expires_at` 且仍是未终结状态。

        分享收敛要用它找"引用了失效资产的 ACTIVE 分享"。**终结状态必须算失效**：
        只按"未终结 + 已到期"筛会让「资产已经被清理成 `EXPIRED`/`DELETED`、分享
        仍然 `ACTIVE`」的链接永远留在公开读口上（总控连续三轮复现过）。

        注意本方法**只**用于决定"锁谁 / 收敛哪些分享"：资产清理阶段用的是
        `peek_expired`（限未终结状态），因此终结资产的对象**不会**被再次删除。
        """

        statement = (
            select(MediaAsset)
            .where(
                or_(
                    MediaAsset.deleted_at.is_not(None),
                    MediaAsset.status.in_(_TERMINAL_STATUSES),
                    and_(
                        MediaAsset.expires_at.is_not(None),
                        MediaAsset.expires_at <= now,
                    ),
                )
            )
            .order_by(MediaAsset.id.asc())
            .limit(limit)
        )
        return tuple(self._session.execute(statement).scalars().all())

    def lock_assets_by_id(self, asset_ids: list[uuid.UUID]) -> tuple[MediaAsset, ...]:
        """按传入顺序一次锁住多张资产行（调用方必须传升序列表）。

        `populate_existing=True`：清理的资产阶段会先"预读"候选（同一事务里把行载进
        identity map），加锁后必须看到**提交后的事实**（期限被改回未来、状态被终结、
        `deleted_at` 被写入），否则会基于旧快照做出错误的删除/跳过判断。
        """

        ordered = list(asset_ids)
        if not ordered:
            return ()
        if ordered != sorted(ordered, key=str):
            raise ValueError("lock_assets_by_id 要求按 id 升序传入")
        statement = (
            select(MediaAsset)
            .where(MediaAsset.id.in_(ordered))
            .order_by(MediaAsset.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return tuple(self._session.execute(statement).scalars().all())
