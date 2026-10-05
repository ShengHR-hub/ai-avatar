"""审计日志数据访问（ENG-04 第 2 步扩围，**只追加**）。

依据：

- `02-database-design.md` §3.4：`audit_logs` 只允许追加；`entity_id` 是多态字段，
  不建立伪外键；`actor_id` 可空以保留系统操作；`actor_type` 由数据库
  `CHECK (actor_type IN ('USER','ADMIN','SYSTEM','WORKER'))` 约束，与
  `point_ledger.entry_type` 一样以数据库为唯一权威，本层不重复校验；
- `LEAD-04` §5.7 与 §8：人工结算与点数调整必须追加**独立**审计记录，携带管理员、
  请求 ID、理由、证据引用、决策时间与任务/钱包快照摘要；
- 迁移 `0016_append_only_guards` 已在数据库侧拒绝 `UPDATE`/`DELETE`，因此本模块
  **不提供任何更新或删除方法**。

脱敏（`09-repository-boundary.md` §7）：`metadata` 只允许写入结构化摘要，禁止 API Key、
Cookie、邮箱验证令牌、签名 URL、原图或 Base64、完整 Prompt、Provider 原始响应以及
未脱敏个人信息。

本卡不启动 PostgreSQL，`SqlAlchemy*` 实现由基础设施集成阶段验证；
单元测试使用 `tests.services.fakes` 的内存替身。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.db.models.audit import AuditLog

__all__ = [
    "AuditEntryDraft",
    "AuditLogRepository",
    "SqlAlchemyAuditLogRepository",
]


@dataclass(frozen=True, slots=True)
class AuditEntryDraft:
    """一条待追加的审计记录（对应 `audit_logs` 一行）。"""

    actor_type: str
    action: str
    entity_type: str
    actor_id: uuid.UUID | None = None
    entity_id: uuid.UUID | None = None
    request_id: str | None = None
    ip_address: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AuditLogRepository(Protocol):
    """审计仓储端口（只允许追加）。"""

    def append(self, draft: AuditEntryDraft) -> AuditLog: ...


class SqlAlchemyAuditLogRepository:
    """`AuditLogRepository` 的 SQLAlchemy 实现（不提交事务）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, draft: AuditEntryDraft) -> AuditLog:
        entry = AuditLog(
            actor_type=draft.actor_type,
            actor_id=draft.actor_id,
            action=draft.action,
            entity_type=draft.entity_type,
            entity_id=draft.entity_id,
            request_id=draft.request_id,
            ip_address=draft.ip_address,
            metadata_=dict(draft.metadata),
        )
        self._session.add(entry)
        self._session.flush()
        return entry
