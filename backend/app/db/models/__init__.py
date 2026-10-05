"""V1.0 SQLAlchemy 模型包（ENG-01 第 1 步）。

导入本包即把全部 20 张表注册到 `Base.metadata`，供 Alembic 与测试使用。

唯一来源：`docs/architecture/02-database-design.md` 第 3 节。
字段名、类型、CHECK、外键 `ondelete` 与索引必须与该文件逐项一致。
"""

from app.db.models.analysis import AnalysisDispatch
from app.db.models.audit import AuditLog
from app.db.models.catalog import PromptTemplate, Style
from app.db.models.engagement import Feedback, Share, ShareAsset
from app.db.models.generation import (
    GenerationCostConfig,
    GenerationInput,
    GenerationOutput,
    GenerationTask,
)
from app.db.models.identity import User, UserEmail
from app.db.models.invites import InviteCode, InviteRedemption
from app.db.models.media import MediaAsset
from app.db.models.orders import Order
from app.db.models.points import PointLedger, PointWallet
from app.db.models.provider import ProviderAttempt

__all__ = [
    "AnalysisDispatch",
    "AuditLog",
    "Feedback",
    "GenerationCostConfig",
    "GenerationInput",
    "GenerationOutput",
    "GenerationTask",
    "InviteCode",
    "InviteRedemption",
    "MediaAsset",
    "Order",
    "PointLedger",
    "PointWallet",
    "PromptTemplate",
    "ProviderAttempt",
    "Share",
    "ShareAsset",
    "Style",
    "User",
    "UserEmail",
]
