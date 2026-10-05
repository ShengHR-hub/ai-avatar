"""媒体资源业务域（ENG-05）。

- `policy`：`purpose -> asset_type` 固定映射与生命周期/签名时长；
- `validation`：服务端图片完整解码与嗅探校验；
- `mappers`：`media_assets` 行 -> 基线 `Asset`；
- `service`：`AssetService`，上传意图、完成校验、读取、签名与逻辑删除的唯一入口。

本包不实现 Provider、生成任务或分享（ENG-06/ENG-09），也不导入任何对象存储 SDK。
"""

from __future__ import annotations

from app.services.assets.mappers import effective_status, to_api_asset
from app.services.assets.policy import PURPOSE_TO_ASSET_TYPE
from app.services.assets.service import AssetService, UploadIntentOutcome

__all__ = [
    "PURPOSE_TO_ASSET_TYPE",
    "AssetService",
    "UploadIntentOutcome",
    "effective_status",
    "to_api_asset",
]
