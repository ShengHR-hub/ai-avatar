"""参考图分析请求 Schema（「参考图片找灵感」）。

唯一来源：`docs/architecture/03-api-contract.md` 第 4.6 节与
`shared/api-contract/openapi.json` 的 `ReferenceDescriptionRequest`。

响应沿用通用 `Envelope`（与 `createAssetUpload` 同一口径）：本能力产出的是一段
**不入库的**描述文本，没有需要收窄成命名 Schema 的持久资源形状。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

__all__ = ["ReferenceDescriptionRequest"]


class ReferenceDescriptionRequest(BaseModel):
    """请求体：只接受一张已经上传完成的参考图（基线 `additionalProperties: false`）。"""

    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
