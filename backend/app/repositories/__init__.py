"""Repository 层（ENG-03 第 2 步）。

分层约定（`06-development-standard.md` 第 2 节）：

- Repository 只做数据访问，**不得自行 `commit`**；事务边界由 Service 控制；
- 每个 Repository 都以 `Protocol` 定义端口，便于用内存实现做单元测试
  （本卡不启动 PostgreSQL）；
- `UnitOfWork` 聚合各领域 Repository 并暴露 `commit` / `rollback`，
  是 Service 唯一的事务入口。
"""

from __future__ import annotations

from app.repositories.audit import (
    AuditEntryDraft,
    AuditLogRepository,
    SqlAlchemyAuditLogRepository,
)
from app.repositories.emails import SqlAlchemyUserEmailRepository, UserEmailRepository
from app.repositories.engagement import (
    DuplicateFeedback,
    DuplicateShare,
    FeedbackDraft,
    FeedbackRepository,
    ShareAssetDraft,
    ShareAssetLink,
    ShareDraft,
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
from app.repositories.media import (
    MediaAssetDraft,
    MediaAssetRepository,
    SqlAlchemyMediaAssetRepository,
)
from app.repositories.points import (
    LedgerEntryDraft,
    PointLedgerRepository,
    PointWalletRepository,
    SqlAlchemyPointLedgerRepository,
    SqlAlchemyPointWalletRepository,
    WalletDelta,
    apply_wallet_delta,
)
from app.repositories.provider_attempt import (
    ProviderAttemptDraft,
    ProviderAttemptRepository,
    SqlAlchemyProviderAttemptRepository,
)
from app.repositories.users import SqlAlchemyUserRepository, UserRepository
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork

__all__ = [
    "AuditEntryDraft",
    "AuditLogRepository",
    "DuplicateFeedback",
    "DuplicateShare",
    "FeedbackDraft",
    "FeedbackRepository",
    "GenerationCostConfigRepository",
    "GenerationTaskRepository",
    "InviteCodeRepository",
    "InviteRedemptionRepository",
    "LedgerEntryDraft",
    "MediaAssetDraft",
    "MediaAssetRepository",
    "PointLedgerRepository",
    "PointWalletRepository",
    "ProviderAttemptDraft",
    "ProviderAttemptRepository",
    "ShareAssetDraft",
    "ShareAssetLink",
    "ShareDraft",
    "ShareRepository",
    "SqlAlchemyAuditLogRepository",
    "SqlAlchemyFeedbackRepository",
    "SqlAlchemyGenerationCostConfigRepository",
    "SqlAlchemyGenerationTaskRepository",
    "SqlAlchemyInviteCodeRepository",
    "SqlAlchemyInviteRedemptionRepository",
    "SqlAlchemyMediaAssetRepository",
    "SqlAlchemyPointLedgerRepository",
    "SqlAlchemyPointWalletRepository",
    "SqlAlchemyProviderAttemptRepository",
    "SqlAlchemyShareRepository",
    "SqlAlchemyUnitOfWork",
    "SqlAlchemyUserEmailRepository",
    "SqlAlchemyUserRepository",
    "UnitOfWork",
    "UserEmailRepository",
    "UserRepository",
    "WalletDelta",
    "apply_wallet_delta",
]
