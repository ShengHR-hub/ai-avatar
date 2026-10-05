"""分享聚合（ENG-09）。

- `service.py`：创建、匿名读取与撤销的业务规则与事务边界；
- `lifecycle.py`：到期收敛（资产与分享），先删对象再改状态。
"""

from app.services.shares.lifecycle import (
    DEFAULT_CLEANUP_BATCH_SIZE,
    CleanupReport,
    MediaLifecycleService,
    ShareLifecycle,
)
from app.services.shares.service import (
    DEFAULT_SHARE_TTL_DAYS,
    SHAREABLE_GENERATION_STATUS,
    SHARE_ASSET_LIMIT,
    ShareService,
)

__all__ = [
    "DEFAULT_CLEANUP_BATCH_SIZE",
    "DEFAULT_SHARE_TTL_DAYS",
    "SHAREABLE_GENERATION_STATUS",
    "SHARE_ASSET_LIMIT",
    "CleanupReport",
    "MediaLifecycleService",
    "ShareLifecycle",
    "ShareService",
]
