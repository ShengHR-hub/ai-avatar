"""点数钱包与流水数据访问（ENG-04 第 2 步）。

依据：

- `02-database-design.md` 第 3.2 节四条不变量：
  `available_points = Σ available_delta`、`reserved_points = Σ reserved_delta`、
  `lifetime_credited = Σ(CREDIT + POSITIVE_CREDIT ADJUSTMENT)`、
  `lifetime_consumed = Σ(CONSUME + NEGATIVE_CONSUMPTION ADJUSTMENT)`；
- `02-database-design.md` 第 6.1/6.2 节事务顺序：
  **锁钱包 → 插入流水 → 更新快照 → `version += 1`**；
- `06-development-standard.md` 第 2 节：Repository **不得自行 `commit`**，事务由 Service 控制。

本模块只做数据访问与纯算术；账务规则（何时 CREDIT/RESERVE/CONSUME/RELEASE/ADJUSTMENT）
由 `app.services.points.service` 决定。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.points import PointLedger, PointWallet

__all__ = [
    "LedgerEntryDraft",
    "LedgerKeysetPage",
    "PointLedgerRepository",
    "PointWalletRepository",
    "SqlAlchemyPointLedgerRepository",
    "SqlAlchemyPointWalletRepository",
    "WalletDelta",
    "apply_wallet_delta",
]


@dataclass(frozen=True, slots=True)
class WalletDelta:
    """一次账务操作对钱包快照的影响（四项增量，缺省为 0）。"""

    available: int = 0
    reserved: int = 0
    lifetime_credited: int = 0
    lifetime_consumed: int = 0


def apply_wallet_delta(wallet: PointWallet, delta: WalletDelta) -> PointWallet:
    """把增量应用到钱包快照并递增 `version`（纯算术，便于无库单测）。

    只做加法与 `version += 1`；非负与不变量校验由 Service 在调用前完成。
    """

    wallet.available_points = wallet.available_points + delta.available
    wallet.reserved_points = wallet.reserved_points + delta.reserved
    wallet.lifetime_credited = wallet.lifetime_credited + delta.lifetime_credited
    wallet.lifetime_consumed = wallet.lifetime_consumed + delta.lifetime_consumed
    wallet.version = wallet.version + 1
    return wallet


@dataclass(frozen=True, slots=True)
class LedgerEntryDraft:
    """一条待追加的流水（对应 `point_ledger` 一行）。"""

    wallet_id: uuid.UUID
    user_id: uuid.UUID
    entry_type: str
    available_delta: int
    reserved_delta: int
    amount: int
    idempotency_key: str
    reason: str
    generation_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PointWalletRepository(Protocol):
    """钱包仓储端口。"""

    def get_by_user_id(self, user_id: uuid.UUID) -> PointWallet | None: ...

    def lock_by_user_id(self, user_id: uuid.UUID) -> PointWallet | None:
        """以 `SELECT ... FOR UPDATE` 锁定钱包行（账务事务第一步）。"""
        ...

    def create(self, *, user_id: uuid.UUID) -> PointWallet: ...

    def apply_delta(self, wallet: PointWallet, delta: WalletDelta) -> PointWallet: ...


@dataclass(frozen=True, slots=True)
class LedgerKeysetPage:
    """流水的 `(created_at, id)` 键集分页结果（契约第 2.2 节）。

    与 `repositories.generation.KeysetPage` 同构但行型不同：`point_ledger` 是只追加的
    事实来源，用户侧翻页必须以它自己的行序为准，不能借用任务分页的行型。
    """

    rows: tuple[PointLedger, ...]
    has_more: bool


@runtime_checkable
class PointLedgerRepository(Protocol):
    """流水仓储端口（只允许追加）。"""

    def append(self, draft: LedgerEntryDraft) -> PointLedger: ...

    def find_by_idempotency_key(self, idempotency_key: str) -> PointLedger | None: ...

    def find_reserve_for_generation(self, generation_id: uuid.UUID) -> PointLedger | None: ...

    def find_final_for_generation(self, generation_id: uuid.UUID) -> PointLedger | None: ...

    def list_for_user(
        self, *, user_id: uuid.UUID, cursor: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> LedgerKeysetPage: ...


class SqlAlchemyPointWalletRepository:
    """`PointWalletRepository` 的 SQLAlchemy 实现（不提交事务）。

    本卡不启动 PostgreSQL，该实现由基础设施集成阶段验证；
    单元测试使用 `tests.services.conftest` 的内存替身。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_user_id(self, user_id: uuid.UUID) -> PointWallet | None:
        statement = select(PointWallet).where(PointWallet.user_id == user_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def lock_by_user_id(self, user_id: uuid.UUID) -> PointWallet | None:
        """行锁：`SELECT ... FOR UPDATE`，必须在账务事务内调用。"""

        statement = (
            select(PointWallet).where(PointWallet.user_id == user_id).with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def create(self, *, user_id: uuid.UUID) -> PointWallet:
        wallet = PointWallet(user_id=user_id)
        self._session.add(wallet)
        self._session.flush()
        return wallet

    def apply_delta(self, wallet: PointWallet, delta: WalletDelta) -> PointWallet:
        apply_wallet_delta(wallet, delta)
        self._session.flush()
        return wallet


class SqlAlchemyPointLedgerRepository:
    """`PointLedgerRepository` 的 SQLAlchemy 实现（只追加，不提交事务）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, draft: LedgerEntryDraft) -> PointLedger:
        entry = PointLedger(
            wallet_id=draft.wallet_id,
            user_id=draft.user_id,
            entry_type=draft.entry_type,
            available_delta=draft.available_delta,
            reserved_delta=draft.reserved_delta,
            amount=draft.amount,
            generation_id=draft.generation_id,
            order_id=draft.order_id,
            idempotency_key=draft.idempotency_key,
            reason=draft.reason,
            metadata_=dict(draft.metadata),
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def find_by_idempotency_key(self, idempotency_key: str) -> PointLedger | None:
        statement = (
            select(PointLedger)
            .where(PointLedger.idempotency_key == idempotency_key)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def find_reserve_for_generation(self, generation_id: uuid.UUID) -> PointLedger | None:
        statement = (
            select(PointLedger)
            .where(
                PointLedger.generation_id == generation_id,
                PointLedger.entry_type == "RESERVE",
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def find_final_for_generation(self, generation_id: uuid.UUID) -> PointLedger | None:
        statement = (
            select(PointLedger)
            .where(
                PointLedger.generation_id == generation_id,
                PointLedger.entry_type.in_(("CONSUME", "RELEASE")),
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_for_user(
        self, *, user_id: uuid.UUID, cursor: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> LedgerKeysetPage:
        """按用户倒序翻页（`ix_point_ledger_user_id_created_at_id`）。

        只按 `user_id` 过滤：流水是用户自己的账务事实，越权行不会出现在结果里，
        因此调用方无需二次过滤。多取一条用于判定 `has_more`。
        """

        statement = (
            select(PointLedger)
            .where(PointLedger.user_id == user_id)
            .order_by(PointLedger.created_at.desc(), PointLedger.id.desc())
            .limit(limit + 1)
        )
        if cursor is not None:
            created_at, ledger_id = cursor
            statement = statement.where(
                (PointLedger.created_at < created_at)
                | (
                    (PointLedger.created_at == created_at)
                    & (PointLedger.id < ledger_id)
                )
            )
        rows = tuple(self._session.execute(statement).scalars())
        return LedgerKeysetPage(rows=rows[:limit], has_more=len(rows) > limit)
