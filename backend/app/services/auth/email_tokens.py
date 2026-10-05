"""邮箱验证令牌存储（ENG-03 第 2 步）。

依据 `03-api-contract.md` 第 3.1 节：邮箱令牌**只存 Redis 并带 TTL**；
`EMAIL_TOKEN_INVALID`（422）用于令牌无效或过期。

与 `session_store` 同一套纪律（技术负责人裁决 ③）：

- 令牌为随机 opaque 值，Redis **不保存原文**，key 使用
  `HMAC-SHA256(SESSION_SECRET, token)` 摘要；
- key 命名空间版本化：`ai_avatar:v1:email-verify:{digest}`；
- 令牌一次性消费（`consume` 成功后立即删除）；
- **令牌 TTL 属未冻结产品参数**（`03-api-contract.md` §12 第 2 项），
  以常量 + 构造参数隔离，本卡不视为产品规则。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.services.auth.session_store import (
    generate_session_token,
    session_digest,
)
from app.services.ports import RedisPort

__all__ = [
    "EMAIL_TOKEN_KEY_NAMESPACE",
    "EMAIL_TOKEN_TTL_SECONDS",
    "EmailTokenRecord",
    "EmailTokenStore",
]

#: 版本化 key 命名空间
EMAIL_TOKEN_KEY_NAMESPACE = "ai_avatar:v1:email-verify"

#: 邮箱令牌 TTL：**待冻结产品参数**，本卡取 30 分钟作为默认值
EMAIL_TOKEN_TTL_SECONDS = 60 * 30


def email_token_key(digest: str) -> str:
    return f"{EMAIL_TOKEN_KEY_NAMESPACE}:{digest}"


@dataclass(frozen=True, slots=True)
class EmailTokenRecord:
    """邮箱令牌对应的服务端状态（不含令牌原文）。"""

    token_digest: str
    user_id: uuid.UUID
    email: str
    expires_at: datetime

    def to_payload(self) -> str:
        import json

        return json.dumps(
            {
                "user_id": str(self.user_id),
                "email": self.email,
                "expires_at": self.expires_at.isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_payload(cls, token_digest: str, payload: str) -> EmailTokenRecord:
        import json

        data = json.loads(payload)
        return cls(
            token_digest=token_digest,
            user_id=uuid.UUID(str(data["user_id"])),
            email=str(data["email"]),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
        )

    def is_expired(self, *, now: datetime | None = None) -> bool:
        moment = now or datetime.now(timezone.utc)
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            moment = moment.replace(tzinfo=None)
        return expires_at <= moment


class EmailTokenStore:
    """基于 `RedisPort` 的邮箱验证令牌存储（只依赖端口协议，可注入内存 fake）。"""

    def __init__(
        self,
        redis: RedisPort,
        *,
        secret: str,
        ttl_seconds: int = EMAIL_TOKEN_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def issue(
        self, *, user_id: uuid.UUID, email: str, now: datetime | None = None
    ) -> tuple[str, EmailTokenRecord]:
        """签发一次性邮箱令牌，返回 `(token, record)`；Redis 只存摘要。"""

        moment = now or datetime.now(timezone.utc)
        token = generate_session_token()
        digest = session_digest(self._secret, token)
        record = EmailTokenRecord(
            token_digest=digest,
            user_id=user_id,
            email=email,
            expires_at=moment + timedelta(seconds=self._ttl_seconds),
        )
        self._redis.set(email_token_key(digest), record.to_payload(), ex=self._ttl_seconds)
        return token, record

    def read(self, token: str) -> EmailTokenRecord | None:
        """读取令牌状态但不消费（供测试与排障）。"""

        if not token:
            return None
        digest = session_digest(self._secret, token)
        payload = self._redis.get(email_token_key(digest))
        if payload is None:
            return None
        record = EmailTokenRecord.from_payload(digest, payload)
        if record.is_expired():
            self._redis.delete(email_token_key(digest))
            return None
        return record

    def consume(self, token: str) -> EmailTokenRecord | None:
        """一次性消费令牌；无效或过期返回 `None`。"""

        record = self.read(token)
        if record is None:
            return None
        self._redis.delete(email_token_key(record.token_digest))
        return record
