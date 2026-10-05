"""分享与反馈的幂等登记（ENG-09）。

幂等依据为什么落在 `audit_logs` 上（技术负责人 2026-10-02 裁决：不新增额外幂等表或列）：

- `audit_logs` 是**只追加**的持久表，写入与业务写入处于同一事务，天然满足
  「幂等依据必须持久化并与写入同事务」；
- 它已经承载 `action` / `entity_type` / `entity_id` / `metadata` 多态引用，
  正适合登记「作用域 + 键摘要 + 请求指纹 + 实体引用」；
- **不保存原始正文**：只写键的 SHA-256 摘要与规范化指纹，`metadata` 里没有
  Prompt、反馈正文或任何凭据（`09-repository-boundary.md` 第 7 节）。

职责划分（重要，避免把并发裁决交给审计行）：

```text
业务表唯一约束（uq_shares_user_idempotency / uq_feedback_user_generation）
    = 并发下的最终裁决者，也是「同键同载荷回放」的第一来源
audit_logs 幂等登记
    = 同一事务内的可审计旁证 + 「同一个键跨任务复用」这类没有业务行可查的冲突
```

因此 Service 的判定顺序固定为「先查业务行 → 再查幂等登记」。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.audit import AuditLog
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories.audit import AuditEntryDraft, AuditLogRepository

__all__ = [
    "ACTION_CREATE_FEEDBACK",
    "ACTION_CREATE_SHARE",
    "ENTITY_FEEDBACK",
    "ENTITY_SHARE",
    "IdempotencyLedger",
    "IdempotencyRecord",
    "IDEMPOTENCY_RETRY_AFTER_SECONDS",
    "InMemoryIdempotencyLedger",
    "SqlAlchemyIdempotencyLedger",
    "conflict",
    "fingerprint_of",
    "in_progress",
    "key_digest",
]

#: 审计动作名与实体类型（稳定字符串，供运维检索；不属于 API 契约）
ACTION_CREATE_SHARE = "SHARE_CREATE"
ACTION_CREATE_FEEDBACK = "FEEDBACK_CREATE"
ENTITY_SHARE = "share"
ENTITY_FEEDBACK = "feedback"


def key_digest(idempotency_key: str, *, scope: str) -> str:
    """键摘要：`sha256(scope + NUL + key)` 的十六进制。

    带作用域是为了让同一个键在分享与反馈两个域里互不干扰；原文不落库。
    """

    material = f"{scope}\x00{idempotency_key}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def fingerprint_of(payload: dict[str, Any]) -> str:
    """请求指纹：规范化 JSON 的 SHA-256（键排序、无空白，同义载荷同指纹）。"""

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def conflict(*, fields: Iterable[str] = ()) -> ApiError:
    """稳定的幂等冲突（`IDEMPOTENCY_CONFLICT`，409）。"""

    details = {"fields": list(fields)} if fields else {}
    return ApiError(ErrorCode.IDEMPOTENCY_CONFLICT, details=details)


#: 「同键请求正在处理中」时建议客户端等待的秒数（`Retry-After` 的取值）。
#: 与 `_IDEMPOTENCY_LOCK_TIMEOUT` 同量级：锁最多被持有这么久，
#: 因此这个等待时长足以让重试落在锁释放之后。
IDEMPOTENCY_RETRY_AFTER_SECONDS = 5


def in_progress() -> ApiError:
    """幂等作用域正被同键请求占用（409 `IDEMPOTENCY_IN_PROGRESS` + `Retry-After`）。

    与 `conflict()` 的区别是**语义**：`conflict` 表示「这个键已经用过了，且内容
    不一样」，重试永远不会成功；`in_progress` 表示「这个键此刻正在被处理」，
    稍后重试会得到确定结果（同载荷回放 / 异载荷冲突）。

    复用既有错误码 `IDEMPOTENCY_IN_PROGRESS`（`ERROR_HTTP_STATUS` 里已经是 409），
    不新增错误码，也不改动冻结的 OpenAPI 基线。
    """

    return ApiError(
        ErrorCode.IDEMPOTENCY_IN_PROGRESS,
        headers={"Retry-After": str(IDEMPOTENCY_RETRY_AFTER_SECONDS)},
    )


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """一条已存在的幂等登记。"""

    entity_id: uuid.UUID | None
    fingerprint: str
    metadata: dict[str, Any]


@runtime_checkable
class IdempotencyLedger(Protocol):
    """幂等登记的读写端口。"""

    def find(
        self, *, user_id: uuid.UUID, action: str, idempotency_key: str
    ) -> IdempotencyRecord | None: ...

    def record(
        self,
        *,
        user_id: uuid.UUID,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        idempotency_key: str,
        fingerprint: str,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None: ...


class SqlAlchemyIdempotencyLedger:
    """基于 `audit_logs` 的实现。

    `find` 用 `metadata` JSONB 上的等值条件定位「本作用域的这个键」：
    `(actor_type, actor_id, action)` 已把范围收窄到单个用户的一个动作，
    再按键摘要取最早的一行。
    """

    def __init__(self, session: Session, *, audit: AuditLogRepository) -> None:
        self._session = session
        self._audit = audit

    def find(
        self, *, user_id: uuid.UUID, action: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        digest = key_digest(idempotency_key, scope=action)
        statement = (
            select(AuditLog)
            .where(
                AuditLog.actor_type == "USER",
                AuditLog.actor_id == user_id,
                AuditLog.action == action,
                AuditLog.metadata_["idempotency_key_sha256"].astext == digest,
            )
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            .limit(1)
        )
        row = self._session.execute(statement).scalar_one_or_none()
        if row is None:
            return None
        return _record_of(dict(row.metadata_ or {}))

    def record(
        self,
        *,
        user_id: uuid.UUID,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        idempotency_key: str,
        fingerprint: str,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            # 作用域 + 键摘要 + 请求指纹 + 实体引用（无原始正文）
            "scope": action,
            "idempotency_key_sha256": key_digest(idempotency_key, scope=action),
            "request_fingerprint": fingerprint,
            "entity_id": str(entity_id),
        }
        if extra:
            metadata.update(extra)
        self._audit.append(
            AuditEntryDraft(
                actor_type="USER",
                actor_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                request_id=request_id,
                metadata=metadata,
            )
        )


class InMemoryIdempotencyLedger:
    """`IdempotencyLedger` 的内存实现（服务/API 单元测试用）。

    行为与 SQL 实现**逐点一致**，因此测试能在没有 PostgreSQL 时验证真实判定路径：

    - 唯一真相是注入的**审计仓储**：`find` 直接读它、`record` 直接写它，
      因此幂等登记与业务行受**同一事务边界**约束（回滚后登记一起消失，
      不会把后续请求误判成回放）；
    - 同作用域同键取**最早**一条，所以「同一个键跨任务复用」也走同一条冲突判定。
    """

    def __init__(self, audit: Any) -> None:
        self._audit = audit

    @property
    def entries(self) -> tuple[Any, ...]:
        """已登记的审计行（测试断言用）。"""

        return self._audit.entries

    def find(
        self, *, user_id: uuid.UUID, action: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        digest = key_digest(idempotency_key, scope=action)
        for entry in self._audit.entries:
            metadata = dict(getattr(entry, "metadata_", None) or {})
            if (
                getattr(entry, "actor_id", None) == user_id
                and getattr(entry, "action", None) == action
                and metadata.get("idempotency_key_sha256") == digest
            ):
                return _record_of(metadata)
        return None

    def record(
        self,
        *,
        user_id: uuid.UUID,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        idempotency_key: str,
        fingerprint: str,
        request_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            "scope": action,
            "idempotency_key_sha256": key_digest(idempotency_key, scope=action),
            "request_fingerprint": fingerprint,
            "entity_id": str(entity_id),
        }
        if extra:
            metadata.update(extra)
        self._audit.append(
            AuditEntryDraft(
                actor_type="USER",
                actor_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                request_id=request_id,
                metadata=metadata,
            )
        )


def _record_of(metadata: dict[str, Any]) -> IdempotencyRecord:
    raw_entity = metadata.get("entity_id")
    try:
        entity_id = uuid.UUID(str(raw_entity)) if raw_entity else None
    except ValueError:  # pragma: no cover - 只可能来自人工改库
        entity_id = None
    return IdempotencyRecord(
        entity_id=entity_id,
        fingerprint=str(metadata.get("request_fingerprint") or ""),
        metadata=metadata,
    )
