"""用户与会话响应 Schema（ENG-03 第 1 步）。

唯一来源：`docs/architecture/03-api-contract.md` 第 5 节与
`shared/api-contract/openapi.json`（LEAD-03 冻结基线）的
`User` / `Session` / `PointBalance` / `UserEnvelope` / `SessionEnvelope` / `PointBalanceEnvelope`。

与冻结基线对齐的写法（沿用 ENG-02 第 4 步裁决 ①③④ 的口径）：

- **响应模型不设置 `extra="forbid"`**（基线未声明 `additionalProperties: false`）；
- 基线的可空字段写作 `{"type": ["string", "null"]}`。**不能直接输出联合 `type` 数组**：
  FastAPI 0.115 的 OpenAPI 模型把 `Schema.type` 校验为字符串，遇到列表会抛
  `pydantic_core.ValidationError`（已在系统解释器上复现）。因此这里统一用
  `WithJsonSchema({"anyOf": [{...}, {"type": "null"}]})` 表达可空性——`anyOf` 是
  Pydantic 原生形式，新旧 FastAPI 都能生成，语义与基线的联合 `type` 等价，
  由第 4 步的规范化比较折叠后逐字段一致；
- 基线的非必填但非可空字段（如 `created_at`）用 `default_factory` 表达「非必填、无 default」；
- 枚举字段用 `StrEnum`，Pydantic 会生成 `$ref`，解引用后与基线的 `enum` 形状一致。

注意：`WithJsonSchema` 只影响 OpenAPI 输出，不影响运行时校验；两者必须保持一致，
因此可空性/非可空性同时由类型注解与 `WithJsonSchema` 表达。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, WithJsonSchema

from app.schemas.common import Envelope

__all__ = [
    "PointBalance",
    "PointBalanceEnvelope",
    "Session",
    "SessionEnvelope",
    "User",
    "UserEnvelope",
    "UserStatus",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserStatus(StrEnum):
    """`users.status` 的契约取值（`02-database-design.md` 第 3.1 节）。"""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELETED = "DELETED"


class User(BaseModel):
    """用户资料响应（基线 `User`，required = id/status/locale/timezone/email_verified）。"""

    id: uuid.UUID
    display_name: Annotated[
        str | None,
        WithJsonSchema({"anyOf": [{"type": "string", "maxLength": 80}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    avatar_asset_id: Annotated[
        uuid.UUID | None,
        WithJsonSchema({"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    status: UserStatus
    locale: str = Field(max_length=16)
    timezone: str = Field(max_length=64)
    primary_email: Annotated[
        str | None,
        WithJsonSchema({"anyOf": [{"type": "string", "format": "email"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    email_verified: bool
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Session(BaseModel):
    """会话响应（基线 `Session`，四个字段均必填）。"""

    session_id: str
    user_id: uuid.UUID
    email_verified: bool
    expires_at: datetime


class PointBalance(BaseModel):
    """点数余额快照（基线 `PointBalance`，四个计数均 `minimum: 0`）。"""

    available_points: Annotated[int, Field(ge=0)]
    reserved_points: Annotated[int, Field(ge=0)]
    lifetime_credited: Annotated[int, Field(ge=0)]
    lifetime_consumed: Annotated[int, Field(ge=0)]
    updated_at: datetime


class UserEnvelope(Envelope):
    """用户响应 envelope（收窄 `data`）。"""

    data: User


class SessionEnvelope(Envelope):
    """会话响应 envelope（收窄 `data`）。"""

    data: Session


class PointBalanceEnvelope(Envelope):
    """点数余额响应 envelope（收窄 `data`）。"""

    data: PointBalance
