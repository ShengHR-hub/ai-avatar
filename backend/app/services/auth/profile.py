"""用户资料读取与更新 Service（ENG-03 第 2 步）。

依据 `03-api-contract.md` 第 3.2 节：

- `PATCH /api/v1/me` 只允许 `display_name`、`avatar_asset_id`、`locale`、`timezone`；
- 未知字段拒绝（由 `UpdateMeRequest` 的 `extra="forbid"` 与仓储白名单双重保证）；
- 不能通过该接口修改邮箱、状态、点数或权限；
- `avatar_asset_id` 如提供，必须校验属于当前用户；
- 资源不存在 → `ASSET_NOT_FOUND`（404），归属他人 → `ASSET_FORBIDDEN`（403）。

事务边界：`update_profile()` 在 `UnitOfWork` 内写入并显式 `commit()`。
"""

from __future__ import annotations

import uuid

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories import UnitOfWork
from app.schemas.auth import UpdateMeRequest
from app.schemas.user import User as ApiUser
from app.services.auth.mappers import to_api_user

__all__ = ["ProfileService"]


class ProfileService:
    """当前用户资料读取与更新。"""

    def __init__(self, *, uow: UnitOfWork) -> None:
        self._uow = uow

    def get_profile(self, *, user_id: uuid.UUID) -> ApiUser:
        """读取当前用户资料（含主邮箱摘要）。"""

        user = self._require_active_user(user_id)
        return to_api_user(user, self._uow.emails.find_primary(user.id))

    def update_profile(self, *, user_id: uuid.UUID, patch: UpdateMeRequest) -> ApiUser:
        """按白名单更新资料字段。"""

        user = self._require_active_user(user_id)

        invalid = patch.non_nullable_fields_provided_as_null()
        if invalid:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": sorted(invalid)},
            )

        provided = patch.provided_fields()
        changes: dict[str, object] = {}
        for field in ("display_name", "locale", "timezone"):
            if field in provided:
                changes[field] = getattr(patch, field)
        if "avatar_asset_id" in provided:
            changes["avatar_asset_id"] = self._resolve_avatar(user.id, patch.avatar_asset_id)

        if changes:
            self._uow.users.update_profile(user, changes)
            self._uow.commit()

        return to_api_user(user, self._uow.emails.find_primary(user.id))

    def _resolve_avatar(self, user_id: uuid.UUID, asset_id: uuid.UUID | None) -> uuid.UUID | None:
        """校验头像资源归属；`None` 表示清除头像。"""

        if asset_id is None:
            return None
        owner = self._uow.media.find_owner(asset_id)
        if owner is None:
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        if owner != user_id:
            raise ApiError(ErrorCode.ASSET_FORBIDDEN)
        return asset_id

    def _require_active_user(self, user_id: uuid.UUID):
        user = self._uow.users.get(user_id)
        if user is None or user.status == "DELETED":
            raise ApiError(ErrorCode.SESSION_INVALID)
        if user.status == "SUSPENDED":
            raise ApiError(ErrorCode.ACCOUNT_SUSPENDED)
        return user
