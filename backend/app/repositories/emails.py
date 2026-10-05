"""用户邮箱数据访问（ENG-03 第 2 步）。

`user_emails.email` 是 CITEXT，因此等值比较天然大小写不敏感（`02-database-design.md` 第 3.1 节）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.identity import UserEmail

__all__ = ["SqlAlchemyUserEmailRepository", "UserEmailRepository"]


@runtime_checkable
class UserEmailRepository(Protocol):
    """用户邮箱仓储端口。"""

    def find_by_email(self, email: str) -> UserEmail | None: ...

    def find_primary(self, user_id: uuid.UUID) -> UserEmail | None: ...

    def find_verified_for_user(self, user_id: uuid.UUID) -> UserEmail | None: ...

    def add(self, *, user_id: uuid.UUID, email: str, is_primary: bool = False) -> UserEmail: ...

    def mark_verified(self, row: UserEmail, verified_at: datetime) -> UserEmail: ...

    def set_primary(self, row: UserEmail, is_primary: bool) -> UserEmail: ...


class SqlAlchemyUserEmailRepository:
    """`UserEmailRepository` 的 SQLAlchemy 实现（单元测试用内存实现替代）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_by_email(self, email: str) -> UserEmail | None:
        statement = select(UserEmail).where(UserEmail.email == email).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def find_primary(self, user_id: uuid.UUID) -> UserEmail | None:
        statement = (
            select(UserEmail)
            .where(UserEmail.user_id == user_id, UserEmail.is_primary.is_(True))
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def find_verified_for_user(self, user_id: uuid.UUID) -> UserEmail | None:
        statement = (
            select(UserEmail)
            .where(UserEmail.user_id == user_id, UserEmail.verified_at.is_not(None))
            .order_by(UserEmail.is_primary.desc(), UserEmail.created_at.asc())
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def add(self, *, user_id: uuid.UUID, email: str, is_primary: bool = False) -> UserEmail:
        row = UserEmail(user_id=user_id, email=email, is_primary=is_primary)
        self._session.add(row)
        self._session.flush()
        return row

    def mark_verified(self, row: UserEmail, verified_at: datetime) -> UserEmail:
        row.verified_at = verified_at
        self._session.flush()
        return row

    def set_primary(self, row: UserEmail, is_primary: bool) -> UserEmail:
        row.is_primary = is_primary
        self._session.flush()
        return row
