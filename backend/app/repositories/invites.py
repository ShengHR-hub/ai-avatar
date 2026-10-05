"""邀请码与兑换记录数据访问（ENG-03 第 2 步）。

并发约定（`02-database-design.md` 第 3.1 节）：

- 兑换时由 Service **锁定邀请码行**（`SELECT ... FOR UPDATE`）并在同一事务内递增
  `redeemed_count`，避免超出 `max_redemptions`；
- `invite_redemptions(invite_code_id, user_id)` 唯一约束保证同一用户对同一邀请码只兑换一次。

本卡不启动 PostgreSQL，`SqlAlchemy*` 实现由基础设施集成阶段验证；
单元测试使用内存实现。
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.invites import InviteCode, InviteRedemption

__all__ = [
    "InviteCodeRepository",
    "InviteRedemptionRepository",
    "SqlAlchemyInviteCodeRepository",
    "SqlAlchemyInviteRedemptionRepository",
]


@runtime_checkable
class InviteCodeRepository(Protocol):
    """邀请码仓储端口。"""

    def find_by_hash(self, code_hash: str) -> InviteCode | None: ...

    def lock_by_hash(self, code_hash: str) -> InviteCode | None:
        """以 `SELECT ... FOR UPDATE` 锁定邀请码行（兑换事务内使用）。"""
        ...

    def increment_redeemed_count(self, code: InviteCode) -> InviteCode: ...

    def mark_used(self, code: InviteCode) -> InviteCode: ...


@runtime_checkable
class InviteRedemptionRepository(Protocol):
    """邀请码兑换记录仓储端口。"""

    def find(self, *, invite_code_id: uuid.UUID, user_id: uuid.UUID) -> InviteRedemption | None: ...

    def add(
        self, *, invite_code_id: uuid.UUID, user_id: uuid.UUID, points_credited: int
    ) -> InviteRedemption: ...


class SqlAlchemyInviteCodeRepository:
    """`InviteCodeRepository` 的 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_by_hash(self, code_hash: str) -> InviteCode | None:
        statement = select(InviteCode).where(InviteCode.code_hash == code_hash).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def lock_by_hash(self, code_hash: str) -> InviteCode | None:
        statement = (
            select(InviteCode).where(InviteCode.code_hash == code_hash).with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def increment_redeemed_count(self, code: InviteCode) -> InviteCode:
        code.redeemed_count = code.redeemed_count + 1
        self._session.flush()
        return code

    def mark_used(self, code: InviteCode) -> InviteCode:
        code.status = "USED"
        self._session.flush()
        return code


class SqlAlchemyInviteRedemptionRepository:
    """`InviteRedemptionRepository` 的 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find(
        self, *, invite_code_id: uuid.UUID, user_id: uuid.UUID
    ) -> InviteRedemption | None:
        statement = (
            select(InviteRedemption)
            .where(
                InviteRedemption.invite_code_id == invite_code_id,
                InviteRedemption.user_id == user_id,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def add(
        self, *, invite_code_id: uuid.UUID, user_id: uuid.UUID, points_credited: int
    ) -> InviteRedemption:
        redemption = InviteRedemption(
            invite_code_id=invite_code_id,
            user_id=user_id,
            points_credited=points_credited,
        )
        self._session.add(redemption)
        self._session.flush()
        return redemption
