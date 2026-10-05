"""Redis 会话存储（ENG-03 第 1 步）。

依据：`03-api-contract.md` 第 3 节（Cookie 值为 opaque token，服务端只在 Redis 保存会话状态）
与技术负责人 2026-09-26 裁决 ③：

- Cookie 值为**随机 opaque token**；
- **Redis 不保存 Cookie 原文**，key 使用 `HMAC-SHA256(SESSION_SECRET, token)` 的摘要；
- key 使用版本化命名空间 `ai_avatar:v1:session:{digest}`；
- 日志不得记录 token、Redis key 或完整 Cookie；
- **会话 TTL、滑动续期、最大并发会话仍属未冻结的产品参数**：本卡不新增产品规则，
  只以模块常量 + 构造参数隔离（见 `SESSION_TTL_SECONDS`），并在报告中列为待冻结项。

对外暴露的 `Session.session_id` 是**令牌摘要**（不可反推令牌），可用于展示与排障。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.schemas.user import Session
from app.services.ports import RedisPort

__all__ = [
    "SESSION_KEY_NAMESPACE",
    "SESSION_TOKEN_BYTES",
    "SESSION_TTL_SECONDS",
    "RedisSessionStore",
    "SessionRecord",
    "generate_session_token",
    "session_digest",
    "session_key",
]

#: 版本化 key 命名空间（裁决 ③）
SESSION_KEY_NAMESPACE = "ai_avatar:v1:session"

#: 会话令牌随机字节数（`secrets.token_urlsafe(32)` → 43 字符）
SESSION_TOKEN_BYTES = 32

#: 会话 TTL：**待冻结产品参数**，本卡取 14 天作为默认值，不构成产品规则
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14


def generate_session_token() -> str:
    """生成随机 opaque 会话令牌（URL 安全、无填充）。"""

    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def session_digest(secret: str, token: str) -> str:
    """计算 `HMAC-SHA256(SESSION_SECRET, token)` 的十六进制摘要。

    使用 HMAC 而非裸 SHA-256，避免在密钥未知时被离线字典/彩虹表反推。
    """

    if not secret:
        raise ValueError("SESSION_SECRET 未配置，无法派生会话摘要。")
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def session_key(digest: str) -> str:
    """返回会话在 Redis 中的完整 key。"""

    return f"{SESSION_KEY_NAMESPACE}:{digest}"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Redis 中保存的会话状态（不含 Cookie 原文）。"""

    session_id: str
    user_id: uuid.UUID
    email_verified: bool
    expires_at: datetime

    def to_payload(self) -> str:
        return json.dumps(
            {
                "user_id": str(self.user_id),
                "email_verified": self.email_verified,
                "expires_at": self.expires_at.isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_payload(cls, session_id: str, payload: str) -> SessionRecord:
        data = json.loads(payload)
        return cls(
            session_id=session_id,
            user_id=uuid.UUID(str(data["user_id"])),
            email_verified=bool(data["email_verified"]),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
        )

    def to_api_model(self, *, email_verified: bool | None = None) -> Session:
        """转换为对外 `Session` Schema。

        `email_verified` 允许被**当前事实**覆盖。`SessionRecord` 里的那一份是
        **会话创建时的快照**（`create()` 写入），而「这个用户现在是否已验证邮箱」的
        唯一事实在 `user_emails.verified_at`（`app/api/deps.py::get_verified_current_user`
        的授权判定也读它）。传 `None` 时保持原样，因此既有调用点行为不变。
        """

        return Session(
            session_id=self.session_id,
            user_id=self.user_id,
            email_verified=self.email_verified if email_verified is None else email_verified,
            expires_at=self.expires_at,
        )

    def is_expired(self, *, now: datetime | None = None) -> bool:
        moment = now or datetime.now(timezone.utc)
        if self.expires_at.tzinfo is None:
            moment = moment.replace(tzinfo=None)
        return self.expires_at <= moment


class RedisSessionStore:
    """基于 `RedisPort` 的会话存储。

    只依赖端口协议，因此可用内存 fake 完整测试（本卡不启动 Redis）。
    """

    def __init__(
        self,
        redis: RedisPort,
        *,
        secret: str,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def create(
        self,
        *,
        user_id: uuid.UUID,
        email_verified: bool,
        now: datetime | None = None,
    ) -> tuple[str, SessionRecord]:
        """创建会话，返回 `(token, record)`；只把摘要写入 Redis。"""

        moment = now or datetime.now(timezone.utc)
        token = generate_session_token()
        digest = session_digest(self._secret, token)
        record = SessionRecord(
            session_id=digest,
            user_id=user_id,
            email_verified=email_verified,
            expires_at=moment + timedelta(seconds=self._ttl_seconds),
        )
        self._redis.set(session_key(digest), record.to_payload(), ex=self._ttl_seconds)
        return token, record

    def read(self, token: str) -> SessionRecord | None:
        """按令牌读取会话；不存在、已过期或令牌为空返回 `None`。"""

        if not token:
            return None
        digest = session_digest(self._secret, token)
        payload = self._redis.get(session_key(digest))
        if payload is None:
            return None
        record = SessionRecord.from_payload(digest, payload)
        if record.is_expired():
            self.revoke(token)
            return None
        return record

    def revoke(self, token: str) -> None:
        """撤销会话（幂等）。"""

        if not token:
            return
        digest = session_digest(self._secret, token)
        self._redis.delete(session_key(digest))
