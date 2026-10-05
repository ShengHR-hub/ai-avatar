"""`media_assets` 行 -> API `Asset` 的映射（ENG-05）。

`status` 的对外取值包含 `EXPIRED`：数据库里的 `status` 只由写路径推进
（`PENDING_UPLOAD -> READY`、`-> DELETED`），而到期是一个随时间成立的事实。
因此读取时把「已 READY 且 `expires_at` 已过」的行**报告**为 `EXPIRED`，
但不就地写库——把状态写回是生命周期清理任务（ENG-09）的职责，
读路径产生写副作用会让只读接口互相竞争。

`storage_key`、`storage_provider` 与 `idempotency_key` 是服务端内部事实，
不出现在基线 `Asset` 里，也不得加入响应。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.db.models.media import MediaAsset
from app.schemas.asset import (
    Asset,
    AssetPurpose,
    AssetStatus,
    AssetType,
    AssetVisibility,
)

__all__ = ["to_api_asset"]


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def effective_status(asset: MediaAsset, *, now: datetime | None = None) -> str:
    """返回对外报告的状态（只读推导，不写库）。"""

    if asset.status != "READY" or asset.expires_at is None:
        return asset.status
    moment = _aware(now or datetime.now(timezone.utc))
    return "EXPIRED" if _aware(asset.expires_at) <= moment else asset.status


def to_api_asset(asset: MediaAsset, *, now: datetime | None = None) -> Asset:
    """把资源行转成基线 `Asset`。"""

    return Asset(
        id=asset.id,
        asset_type=AssetType(asset.asset_type),
        purpose=AssetPurpose(asset.purpose),
        status=AssetStatus(effective_status(asset, now=now)),
        visibility=AssetVisibility(asset.visibility),
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        width=asset.width,
        height=asset.height,
        sha256=asset.sha256,
        expires_at=asset.expires_at,
        created_at=asset.created_at,
    )
