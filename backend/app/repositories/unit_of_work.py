"""工作单元（ENG-03 第 2 步）。

`UnitOfWork` 是 Service 唯一的事务入口（`06-development-standard.md` 第 2 节）：
Service 在业务操作完成后调用 `commit()`，异常时调用 `rollback()`；
Repository 自身永不提交。

设计要点：

- 端口化后，单元测试可注入纯内存实现，无需 PostgreSQL；
- `SqlAlchemyUnitOfWork` 只负责装配 Repository 与提交/回滚，不承载业务规则。
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session, sessionmaker

from app.repositories.audit import (
    AuditLogRepository,
    SqlAlchemyAuditLogRepository,
)
from app.repositories.emails import SqlAlchemyUserEmailRepository, UserEmailRepository
from app.repositories.engagement import (
    FeedbackRepository,
    ShareRepository,
    SqlAlchemyFeedbackRepository,
    SqlAlchemyShareRepository,
)
from app.repositories.generation import (
    GenerationCostConfigRepository,
    GenerationTaskRepository,
    SqlAlchemyGenerationCostConfigRepository,
    SqlAlchemyGenerationTaskRepository,
)
from app.repositories.invites import (
    InviteCodeRepository,
    InviteRedemptionRepository,
    SqlAlchemyInviteCodeRepository,
    SqlAlchemyInviteRedemptionRepository,
)
from app.repositories.media import MediaAssetRepository, SqlAlchemyMediaAssetRepository
from app.repositories.points import (
    PointLedgerRepository,
    PointWalletRepository,
    SqlAlchemyPointLedgerRepository,
    SqlAlchemyPointWalletRepository,
)
from app.repositories.provider_attempt import (
    ProviderAttemptRepository,
    SqlAlchemyProviderAttemptRepository,
)
from app.repositories.users import SqlAlchemyUserRepository, UserRepository
from app.services.engagement_support import (
    IdempotencyLedger,
    SqlAlchemyIdempotencyLedger,
)

__all__ = ["SqlAlchemyUnitOfWork", "UnitOfWork"]


@runtime_checkable
class UnitOfWork(Protocol):
    """事务边界与仓储聚合端口。

    点数结算的统一锁顺序（`LEAD-04` §6:127，技术负责人 2026-09-27 裁决 ②）：
    **先锁 `generation_tasks`，再锁 `point_wallets`**；所有结算入口必须采用同一顺序。
    """

    users: UserRepository
    emails: UserEmailRepository
    invite_codes: InviteCodeRepository
    invite_redemptions: InviteRedemptionRepository
    media: MediaAssetRepository
    generation_tasks: GenerationTaskRepository
    generation_costs: GenerationCostConfigRepository
    point_wallets: PointWalletRepository
    point_ledger: PointLedgerRepository
    audit_logs: AuditLogRepository
    provider_attempts: ProviderAttemptRepository
    shares: ShareRepository
    feedback: FeedbackRepository
    #: 分享/反馈的幂等登记（ENG-09）。用途与职责划分见
    #: `app/services/engagement_support.py`：业务表唯一约束仍是并发裁决者。
    ledger: IdempotencyLedger

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyUnitOfWork:
    """`UnitOfWork` 的 SQLAlchemy 实现。

    用法：

    ```python
    with SqlAlchemyUnitOfWork(session_factory) as uow:
        ...           # 业务操作
        uow.commit()  # 显式提交
    ```

    未调用 `commit()` 即退出时自动回滚。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self.users: UserRepository
        self.emails: UserEmailRepository
        self.invite_codes: InviteCodeRepository
        self.invite_redemptions: InviteRedemptionRepository
        self.media: MediaAssetRepository
        self.generation_tasks: GenerationTaskRepository
        self.generation_costs: GenerationCostConfigRepository
        self.point_wallets: PointWalletRepository
        self.point_ledger: PointLedgerRepository
        self.audit_logs: AuditLogRepository
        self.provider_attempts: ProviderAttemptRepository
        self.shares: ShareRepository
        self.feedback: FeedbackRepository
        self._ledger: IdempotencyLedger | None = None

    @property
    def ledger(self) -> IdempotencyLedger:
        """幂等登记（惰性构造，复用本事务的 Session 与审计仓储）。"""

        if self._ledger is None:
            session = self.session
            self._ledger = SqlAlchemyIdempotencyLedger(
                session, audit=self.audit_logs
            )
        return self._ledger

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        session = self._session_factory()
        self._session = session
        self.users = SqlAlchemyUserRepository(session)
        self.emails = SqlAlchemyUserEmailRepository(session)
        self.invite_codes = SqlAlchemyInviteCodeRepository(session)
        self.invite_redemptions = SqlAlchemyInviteRedemptionRepository(session)
        self.media = SqlAlchemyMediaAssetRepository(session)
        self.generation_tasks = SqlAlchemyGenerationTaskRepository(session)
        self.generation_costs = SqlAlchemyGenerationCostConfigRepository(session)
        self.point_wallets = SqlAlchemyPointWalletRepository(session)
        self.point_ledger = SqlAlchemyPointLedgerRepository(session)
        self.audit_logs = SqlAlchemyAuditLogRepository(session)
        self.provider_attempts = SqlAlchemyProviderAttemptRepository(session)
        self.shares = SqlAlchemyShareRepository(session)
        self.feedback = SqlAlchemyFeedbackRepository(session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            if exc_type is not None:
                session.rollback()
        finally:
            session.close()

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("UnitOfWork 尚未进入上下文。")
        return self._session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
