"""生成任务请求/响应 Schema（ENG-06 第 5 步）。

唯一来源：`docs/architecture/03-api-contract.md` 第 4.3、5 节与
`shared/api-contract/openapi.json` 的 `Generation` / `GenerationEnvelope` /
`GenerationCreateRequest`（LEAD-03 冻结基线）。

沿用已确立的基线对齐口径（见 `app/schemas/user.py`、`app/schemas/asset.py`）：

- 响应模型不设 `extra="forbid"`（基线未声明 `additionalProperties: false`）；
  **请求模型 `GenerationCreateRequest` 例外**：基线明确写了 `additionalProperties: false`；
- 基线的可空字段写联合 `type` 数组，运行时必须写 `anyOf`（FastAPI 0.115 的 OpenAPI
  模型把 `Schema.type` 校验为字符串），由契约测试折叠后逐字段一致；
- 基线里「非必填但一定出现」的字段用 `default_factory` 表达；
- `requested_count` / `output_count` 在基线里是 `{"type":"integer","const":2}`，
  因此用 `Literal[2]` 而不是 `int + 校验`，让契约自己禁止其他取值。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from app.schemas.common import Envelope

__all__ = [
    "GENERATION_OUTPUT_COUNT",
    "Generation",
    "GenerationBillingStatus",
    "GenerationCreateRequest",
    "GenerationEnvelope",
    "GenerationMode",
    "GenerationStatus",
]

#: V1.0 固定每次生成 2 张（`03-api-contract.md` 第 4.2 节；基线写 `const: 2`）
GENERATION_OUTPUT_COUNT = Literal[2]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GenerationMode(StrEnum):
    """`generation_tasks.mode`（`02-database-design.md` 第 3.2 节）。"""

    PHOTO_PROMPT = "PHOTO_PROMPT"
    TEXT_PROMPT = "TEXT_PROMPT"
    REFERENCE_IMAGE = "REFERENCE_IMAGE"


class GenerationStatus(StrEnum):
    """任务生命周期六态；V1.0 不含 `CANCELLED`（`LEAD-04` §2）。"""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class GenerationBillingStatus(StrEnum):
    """账务配对状态（`LEAD-04` §3）。"""

    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class Generation(BaseModel):
    """生成任务响应（基线 `Generation`）。

    required = id / mode / status / billing_status / prompt / requested_count /
    reserved_points；`parent_generation_id` 与 `failure_code` 可空，
    `created_at`、`updated_at` 非必填但总是由服务端填充。
    """

    id: uuid.UUID
    parent_generation_id: Annotated[
        uuid.UUID | None,
        WithJsonSchema({"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    mode: GenerationMode
    status: GenerationStatus
    billing_status: GenerationBillingStatus
    prompt: str
    requested_count: GENERATION_OUTPUT_COUNT
    reserved_points: Annotated[
        int,
        WithJsonSchema({"type": "integer", "minimum": 0}),
    ]
    failure_code: Annotated[
        str | None,
        WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class GenerationEnvelope(Envelope):
    """任务响应 envelope（基线 `GenerationEnvelope`：`allOf` Envelope + 收窄 `data`）。"""

    data: Generation


class GenerationCreateRequest(BaseModel):
    """创建任务请求体（基线 `GenerationCreateRequest`，`additionalProperties: false`）。"""

    model_config = ConfigDict(extra="forbid")

    mode: GenerationMode
    style_id: Annotated[
        uuid.UUID | None,
        WithJsonSchema({"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    prompt_template_id: Annotated[
        uuid.UUID | None,
        WithJsonSchema({"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    prompt: str = Field(min_length=1)
    input_asset_ids: list[uuid.UUID]
    output_count: GENERATION_OUTPUT_COUNT
