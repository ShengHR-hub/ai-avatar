"""ORM → API Schema 映射（ENG-03 第 2 步）。

Service 对外只暴露 `app.schemas` 中的冻结模型，ORM 实体不外泄。
"""

from __future__ import annotations

from app.db.models.identity import User, UserEmail
from app.schemas.user import User as ApiUser
from app.schemas.user import UserStatus

__all__ = ["to_api_user", "to_user_status"]


def to_user_status(raw: str) -> UserStatus:
    return UserStatus(raw)


def to_api_user(user: User, email_row: UserEmail | None = None) -> ApiUser:
    """把 `users` 行（与可选主邮箱行）映射为契约 `User`。"""

    return ApiUser(
        id=user.id,
        display_name=user.display_name,
        avatar_asset_id=user.avatar_asset_id,
        status=to_user_status(user.status),
        locale=user.locale,
        timezone=user.timezone,
        primary_email=email_row.email if email_row is not None else None,
        email_verified=bool(email_row is not None and email_row.verified_at is not None),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )
