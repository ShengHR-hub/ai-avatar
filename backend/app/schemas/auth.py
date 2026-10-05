"""认证与资料更新请求 Schema（ENG-03 第 1 步）。

唯一来源：`docs/architecture/03-api-contract.md` 第 3.1、3.2 节与
`shared/api-contract/openapi.json` 的
`components.requestBodies.{InviteRedeem, EmailRequest, EmailVerify, UpdateMe}` 与
`components.schemas.UpdateMeRequest`。

对齐要点：

- **请求体模型一律 `extra="forbid"`**：基线的请求体 Schema 都声明了
  `additionalProperties: false`，未知字段必须被拒绝（契约第 3.2 节明确要求）；
- `email` 字段：基线为 `{"type": "string", "format": "email"}`。本卡不引入
  `email-validator` 依赖（`requirements.txt` 仅允许增加 `redis`），因此用
  `WithJsonSchema` 输出与基线一致的 Schema，并在运行时用显式校验器保证一致性；
- 可空字段（`display_name`、`avatar_asset_id`）用
  `WithJsonSchema({"anyOf": [{...}, {"type": "null"}]})` 表达：**不能输出联合 `type` 数组**，
  FastAPI 0.115 的 OpenAPI 模型会把 `Schema.type` 校验为字符串并抛
  `pydantic_core.ValidationError`（已复现）。`anyOf` 是 Pydantic 原生形式，语义等价；
- `UpdateMeRequest` 的 `locale` / `timezone` 在基线中是**非可空、非必填、无 default**：
  类型注解保留 `| None` 以便用 `model_fields_set` 判断「未提供」，
  但 Schema 与运行时都按非可空处理（显式传 `null` 会被拒绝）；
- 201 响应使用**通用 `Envelope`**（基线 `responses.InviteRedeemed` 的 `data` 为 `{}`），
  不额外收窄，避免与基线形状不一致。
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
)

from app.schemas.user import PointBalance, Session
from app.schemas.user import User as ApiUser

__all__ = [
    "EmailAddress",
    "EmailRequestRequest",
    "EmailVerifyRequest",
    "InviteRedeemRequest",
    "InviteRedeemedData",
    "UpdateMeRequest",
]

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_format(value: str) -> str:
    """最小可用的邮箱格式校验（契约只声明 `format: email`，未规定具体规则）。"""

    if not _EMAIL_PATTERN.match(value):
        raise ValueError("email 格式不正确")
    return value


#: 与基线 `{"type": "string", "format": "email"}` 一致的邮箱字段类型。
#: 长度上限取 RFC 5321 的 254（契约未规定，属防御性实现细节）。
EmailAddress = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=3, max_length=254),
    AfterValidator(_validate_email_format),
    WithJsonSchema({"type": "string", "format": "email"}),
]


class InviteRedeemRequest(BaseModel):
    """`POST /api/v1/auth/invites/redeem` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    invite_code: str = Field(min_length=1, max_length=128)
    email: EmailAddress


class EmailRequestRequest(BaseModel):
    """`POST /api/v1/auth/email/request` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    email: EmailAddress


class EmailVerifyRequest(BaseModel):
    """`POST /api/v1/auth/email/verify` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=512)


class UpdateMeRequest(BaseModel):
    """`PATCH /api/v1/me` 请求体；只允许四个资料字段（契约第 3.2 节）。"""

    model_config = ConfigDict(extra="forbid")

    display_name: Annotated[
        str | None,
        WithJsonSchema({"anyOf": [{"type": "string", "maxLength": 80}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    avatar_asset_id: Annotated[
        uuid.UUID | None,
        WithJsonSchema({"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}),
    ] = Field(default_factory=lambda: None)
    locale: Annotated[
        str | None,
        WithJsonSchema({"type": "string", "maxLength": 16}),
    ] = Field(default_factory=lambda: None)
    timezone: Annotated[
        str | None,
        WithJsonSchema({"type": "string", "maxLength": 64}),
    ] = Field(default_factory=lambda: None)

    def provided_fields(self) -> set[str]:
        """返回本次请求**显式提供**的字段名（PATCH 语义依赖此集合）。"""

        return set(self.model_fields_set)

    def non_nullable_fields_provided_as_null(self) -> set[str]:
        """返回「非可空字段被显式传 null」的字段名，供调用方拒绝该请求。"""

        non_nullable = ("locale", "timezone")
        return {
            name
            for name in non_nullable
            if name in self.model_fields_set and getattr(self, name) is None
        }


class InviteRedeemedData(BaseModel):
    """邀请兑换成功时的 `data`（契约第 3.1 节：`{user, session, points}`）。

    注意：基线 `responses.InviteRedeemed` 的响应 Schema 是**通用 `Envelope`**（`data` 为任意值），
    因此该模型只用于构造响应体，**不作为路由的 `response_model`**，避免收窄 `data` 造成形状不一致。
    """

    user: ApiUser
    session: Session
    points: PointBalance
