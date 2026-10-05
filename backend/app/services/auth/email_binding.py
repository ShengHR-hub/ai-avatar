"""邮箱绑定与验证 Service（ENG-03 第 2 步）。

依据 `03-api-contract.md` 第 3.1 节与第 7 节：

- 邮箱请求**即使邮箱不存在也返回统一 `202`**，避免枚举；因此
  `request_verification()` 不抛业务错误（除依赖未就绪外）；
- 邮箱令牌只存 Redis 并带 TTL，一次性消费；
- 令牌无效或过期 → `EMAIL_TOKEN_INVALID`（422）；
- 邮箱已绑定其他用户 → `EMAIL_IN_USE`（409）；
- 当前邮箱已验证 → `EMAIL_ALREADY_VERIFIED`（409）；
- 邮件依赖未就绪 → `EmailPortUnavailable` 由调用方映射为 `NOT_READY`（503）。

事务边界：`verify()` 在 `UnitOfWork` 内完成绑定并显式 `commit()`；
`request_verification()` 只写 Redis，不开启数据库事务。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories import UnitOfWork
from app.schemas.user import User as ApiUser
from app.services.auth.email_tokens import EMAIL_TOKEN_TTL_SECONDS, EmailTokenStore
from app.services.auth.mappers import to_api_user
from app.services.ports import EmailPort

__all__ = ["EmailBindingService"]


class EmailBindingService:
    """邮箱验证请求与确认。"""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        tokens: EmailTokenStore,
        email_port: EmailPort,
        token_ttl_seconds: int = EMAIL_TOKEN_TTL_SECONDS,
    ) -> None:
        self._uow = uow
        self._tokens = tokens
        self._email_port = email_port
        self._token_ttl_seconds = token_ttl_seconds

    @property
    def token_ttl_seconds(self) -> int:
        return self._token_ttl_seconds

    def request_verification(self, *, user_id: uuid.UUID, email: str) -> None:
        """签发令牌并投递验证邮件。

        **防枚举**：邮箱已被其他用户占用时，静默成功且不发送邮件；
        调用方统一返回 `202`。
        """

        existing = self._uow.emails.find_by_email(email)
        if existing is not None and existing.user_id != user_id:
            return

        token, _ = self._tokens.issue(user_id=user_id, email=email)
        self._email_port.send_verification_email(
            to_email=email,
            token=token,
            expires_in_seconds=self._token_ttl_seconds,
        )

    def verify(self, *, token: str, now: datetime | None = None) -> ApiUser:
        """消费令牌并把邮箱绑定到令牌所属用户。"""

        moment = now or datetime.now(timezone.utc)
        record = self._tokens.consume(token)
        if record is None:
            raise ApiError(ErrorCode.EMAIL_TOKEN_INVALID)

        existing = self._uow.emails.find_by_email(record.email)
        if existing is not None and existing.user_id != record.user_id:
            raise ApiError(ErrorCode.EMAIL_IN_USE)

        if existing is not None and existing.verified_at is not None:
            raise ApiError(ErrorCode.EMAIL_ALREADY_VERIFIED)

        user = self._uow.users.get(record.user_id)
        if user is None:
            raise ApiError(ErrorCode.SESSION_INVALID)

        if existing is None:
            has_primary = self._uow.emails.find_primary(user.id) is not None
            row = self._uow.emails.add(
                user_id=user.id, email=record.email, is_primary=not has_primary
            )
        else:
            row = existing

        row = self._uow.emails.mark_verified(row, moment)
        self._uow.commit()

        primary = row if row.is_primary else self._uow.emails.find_primary(user.id)
        return to_api_user(user, primary)
