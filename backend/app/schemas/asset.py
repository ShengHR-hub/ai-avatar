"""媒体资源请求/响应 Schema（ENG-05 第 4 步）。

唯一来源：`docs/architecture/03-api-contract.md` 第 4.2、5 节与
`shared/api-contract/openapi.json` 的 `Asset` / `AssetEnvelope` / `UploadIntent`。

沿用 ENG-02 第 4 步与 ENG-03 已确立的基线对齐口径（见 `app/schemas/user.py` 模块说明）：

- 响应模型不设 `extra="forbid"`（基线未声明 `additionalProperties: false`）；
  **请求模型 `UploadIntentRequest` 例外**：基线 `UploadIntent` 明确写了
  `additionalProperties: false`，未知字段必须拒绝；
- 可空字段用 `WithJsonSchema({"anyOf": [..., {"type": "null"}]})` 表达
  （FastAPI 0.115 不能输出联合 `type` 数组），比较时由契约测试折叠，语义与基线一致；
- 「非必填但一定出现」的字段（`created_at`）用 `default_factory` 表达；
- 枚举用 `StrEnum`，解引用后与基线的 `enum` 形状一致。

注意 `purpose` 在两侧取值不同：响应 `Asset.purpose` 含 `RESULT`（结果图），
而上传请求只能是 `SOURCE`/`REFERENCE`（`03-api-contract.md` 第 4.2 节），
因此是两个不同的枚举类型，不得合并成一个。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from app.schemas.common import Envelope

__all__ = [
    "Asset",
    "AssetEnvelope",
    "AssetPurpose",
    "AssetStatus",
    "AssetType",
    "AssetVisibility",
    "SHA256_PATTERN",
    "UploadIntentRequest",
    "UploadPurpose",
]

#: 基线 `Asset.sha256` / `UploadIntent.sha256` 的模式
SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AssetType(StrEnum):
    """`media_assets.asset_type`（`02-database-design.md` 第 3.3 节）。"""

    ORIGINAL = "ORIGINAL"
    REFERENCE = "REFERENCE"
    RESULT = "RESULT"


class AssetPurpose(StrEnum):
    """响应侧用途（基线 `Asset.purpose`，含 `RESULT`）。"""

    SOURCE = "SOURCE"
    REFERENCE = "REFERENCE"
    RESULT = "RESULT"


class UploadPurpose(StrEnum):
    """请求侧用途（基线 `UploadIntent.purpose`，**不含** `RESULT`）。"""

    SOURCE = "SOURCE"
    REFERENCE = "REFERENCE"


class AssetStatus(StrEnum):
    """生命周期状态（迁移 0019 新增列，基线 `Asset.status`）。"""

    PENDING_UPLOAD = "PENDING_UPLOAD"
    READY = "READY"
    DELETED = "DELETED"
    EXPIRED = "EXPIRED"


class AssetVisibility(StrEnum):
    """可见性（`02-database-design.md` 第 3.3 节）。"""

    PRIVATE = "PRIVATE"
    SHARED = "SHARED"
    PUBLIC = "PUBLIC"


class Asset(BaseModel):
    """媒体资源响应（基线 `Asset`）。

    required = id / asset_type / purpose / status / visibility / mime_type /
    size_bytes / sha256；`width`、`height`、`expires_at` 可空，
    `created_at` 非必填但总是被服务端填充。
    """

    id: uuid.UUID
    asset_type: AssetType
    purpose: AssetPurpose
    status: AssetStatus
    visibility: AssetVisibility
    mime_type: str
    size_bytes: Annotated[
        int,
        WithJsonSchema({"type": "integer", "minimum": 0}),
    ]
    width: Annotated[
        int | None,
        WithJsonSchema({"anyOf": [{"type": "integer"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    height: Annotated[
        int | None,
        WithJsonSchema({"anyOf": [{"type": "integer"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    sha256: str = Field(pattern=SHA256_PATTERN)
    expires_at: Annotated[
        datetime | None,
        WithJsonSchema({"anyOf": [{"type": "string", "format": "date-time"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    created_at: datetime = Field(default_factory=_now)


class AssetEnvelope(Envelope):
    """资产响应 envelope（基线 `AssetEnvelope`：`allOf` Envelope + 收窄 `data`）。"""

    data: Asset


class UploadIntentRequest(BaseModel):
    """创建上传意图的请求体（基线 `UploadIntent`，`additionalProperties: false`）。"""

    model_config = ConfigDict(extra="forbid")

    purpose: UploadPurpose
    filename: str = Field(max_length=255)
    mime_type: str
    size_bytes: Annotated[
        int,
        WithJsonSchema({"type": "integer", "minimum": 1}),
    ]
    sha256: str = Field(pattern=SHA256_PATTERN)
