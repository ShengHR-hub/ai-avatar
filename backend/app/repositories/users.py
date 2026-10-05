"""用户数据访问（ENG-03 第 2 步）。

- 只做数据访问，不提交事务、不做业务校验；
- 允许的更新字段由 Service 白名单决定，Repository 只负责写入。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.identity import User

__all__ = ["SqlAlchemyUserRepository", "UserRepository"]

#: `users` 表允许通过 API 更新的字段（契约第 3.2 节）
UPDATABLE_PROFILE_FIELDS: frozenset[str] = frozenset(
    {"display_name", "avatar_asset_id", "locale", "timezone"}
)


@runtime_checkable
class UserRepository(Protocol):
    """用户仓储端口。"""

    def get(self, user_id: uuid.UUID) -> User | None: ...

    def create(
        self,
        *,
        locale: str,
        timezone: str,
        status: str = "ACTIVE",
        display_name: str | None = None,
    ) -> User: ...

    def update_profile(self, user: User, changes: Mapping[str, Any]) -> User: ...

    def set_status(self, user: User, status: str) -> User: ...


class SqlAlchemyUserRepository:
    """`UserRepository` 的 SQLAlchemy 实现。

    本卡不启动 PostgreSQL，因此该实现由基础设施集成阶段验证；
    单元测试使用 `tests.services.fakes.InMemoryUserRepository`。
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, user_id: uuid.UUID) -> User | None:
        return self._session.get(User, user_id)

    def create(
        self,
        *,
        locale: str,
        timezone: str,
        status: str = "ACTIVE",
        display_name: str | None = None,
    ) -> User:
        user = User(
            locale=locale,
            timezone=timezone,
            status=status,
            display_name=display_name,
        )
        self._session.add(user)
        self._session.flush()
        return user

    def update_profile(self, user: User, changes: Mapping[str, Any]) -> User:
        for field, value in changes.items():
            if field not in UPDATABLE_PROFILE_FIELDS:
                raise ValueError(f"字段 {field} 不允许通过资料接口更新。")
            setattr(user, field, value)
        self._session.flush()
        return user

    def set_status(self, user: User, status: str) -> User:
        user.status = status
        self._session.flush()
        return user

    def find_by_primary_email(self, email: str) -> User | None:
        """按主邮箱查用户（仅供后续卡片使用；本卡 Service 走邮箱仓储）。"""

        from app.db.models.identity import UserEmail

        statement = (
            select(User)
            .join(UserEmail, UserEmail.user_id == User.id)
            .where(UserEmail.email == email)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()
