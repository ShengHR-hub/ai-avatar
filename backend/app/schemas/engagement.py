"""分享与反馈请求/响应 Schema（ENG-09）。

唯一来源：`docs/architecture/03-api-contract.md` 第 4.4、5 节与
`shared/api-contract/openapi.json` 的 `ShareCreateRequest` / `FeedbackCreateRequest` /
`ShareEnvelope`。

**基线未约束的形状必须保持未约束**（这是本卡最容易踩错的一点，已由
`tests/api/test_openapi_contract.py` 的逐字段比较实测暴露）：

- `ShareEnvelope.data` 在基线里是裸 `{"type": "object"}`。因此响应模型**不做收窄**，
  载荷由 Service 组装成 dict —— 与 `listGenerationOutputs` 使用通用 `PageEnvelope`
  同一先例。收窄成具体模型会让解引用后的响应内容与基线不一致；
- 基线把可空字段的长度约束写在**联合之外**
  （`title`：`{"type": ["string","null"], "maxLength": 160}`，
  `content`：`{"type": ["string","null"], "maxLength": 2000}`）。而
  `Field(max_length=...)` 会让 Pydantic 把约束塞进 `anyOf` 的字符串分支里，
  解引用后与基线不同。因此这两处用 `WithJsonSchema` 显式给出 `anyOf + maxLength`。

**`WithJsonSchema` 只改 JSON Schema，不产生运行时校验**。所以这两处的长度上限同时由
`field_validator` 强制（超长抛 `ValueError`，上层归一为 422 `VALIDATION_ERROR`），
否则「Schema 说 160、实际能存 10000」就是两处真相。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
)

from app.schemas.common import Envelope

__all__ = [
    "CATEGORY_MAX_LENGTH",
    "CONTENT_MAX_LENGTH",
    "FeedbackCreateRequest",
    "SLUG_MAX_LENGTH",
    "ShareAssetView",
    "ShareCreateRequest",
    "ShareEnvelope",
    "ShareView",
    "TITLE_MAX_LENGTH",
    "as_share_data",
]

#: 基线 `ShareCreateRequest.title.maxLength`
TITLE_MAX_LENGTH = 160
#: 基线 `FeedbackCreateRequest.category.maxLength`（迁移 0021 已把列扩宽到 64）
CATEGORY_MAX_LENGTH = 64
#: 基线 `FeedbackCreateRequest.content.maxLength`
CONTENT_MAX_LENGTH = 2000
#: 基线 `Slug` 参数的 maxLength（同时也是 `shares.slug` 的列宽）
SLUG_MAX_LENGTH = 128


class ShareCreateRequest(BaseModel):
    """创建分享的请求体（基线 `ShareCreateRequest`，`additionalProperties: false`）。

    `output_asset_ids` 的**数量与去重规则**由 Service 冻结为「1 或 2 张、不重复」：
    契约本轮把每次生成固定为两张，数量不在 Schema 表达（基线未声明
    minItems/maxItems），越界由 Service 以 `VALIDATION_ERROR` 拒绝并指明字段。

    `title` 与 `FeedbackCreateRequest.content` 同理：基线把 `maxLength` 写在可空**联合
    之外**，而 `Field(max_length=...)` 会让 Pydantic 把它塞进 `anyOf` 的字符串分支里，
    解引用后形状不同。因此两处都用 `WithJsonSchema` 显式给出 `anyOf + maxLength`。
    """

    model_config = ConfigDict(extra="forbid")

    generation_id: uuid.UUID
    output_asset_ids: list[uuid.UUID]
    title: Annotated[
        str | None,
        WithJsonSchema(
            {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "maxLength": TITLE_MAX_LENGTH,
            }
        ),
    ] = None

    @field_validator("title")
    @classmethod
    def _check_title_length(cls, value: Any) -> Any:
        """`WithJsonSchema` 不产生校验，长度上限必须显式强制。"""

        if isinstance(value, str) and len(value) > TITLE_MAX_LENGTH:
            raise ValueError(f"title 长度不得超过 {TITLE_MAX_LENGTH} 字符。")
        return value


class ShareView(BaseModel):
    """分享响应载荷（由 `ShareService` 组装；`ShareEnvelope.data` 保持未约束 object）。

    这个模型用于**服务端内部**的类型检查与文档说明，不直接作为响应模型，
    以免把基线里未约束的 `data` 收窄。
    """

    slug: str = Field(max_length=SLUG_MAX_LENGTH)
    title: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    outputs: list[ShareAssetView]


class ShareAssetView(BaseModel):
    """分享页里一张被选中结果的呈现元数据。"""

    asset_id: uuid.UUID
    output_index: int
    mime_type: str
    width: int | None = None
    height: int | None = None
    size_bytes: int


class ShareEnvelope(Envelope):
    """分享响应 envelope。

    基线 `ShareEnvelope` = `allOf[Envelope, {properties: {data: {type: object}}}]`，
    因此这里**继承通用 `Envelope` 而不收窄 `data`**。
    """


def as_share_data(view: ShareView) -> dict[str, object]:
    """把 `ShareView` 展平成响应 `data`（时间统一 ISO-8601 UTC 字符串）。"""

    return {
        "slug": view.slug,
        "title": view.title,
        "expires_at": view.expires_at.isoformat() if view.expires_at else None,
        "created_at": view.created_at.isoformat(),
        "outputs": [
            {
                "asset_id": str(output.asset_id),
                "output_index": output.output_index,
                "mime_type": output.mime_type,
                "width": output.width,
                "height": output.height,
                "size_bytes": output.size_bytes,
            }
            for output in view.outputs
        ],
    }


class FeedbackCreateRequest(BaseModel):
    """提交反馈的请求体（基线 `FeedbackCreateRequest`，`additionalProperties: false`）。

    `category` 必填 ≤64，`content` 可空 ≤2000 —— 与基线逐字一致。

    为什么 `content` 用 `WithJsonSchema`：基线把 `content` 写成
    `{"type": ["string","null"], "maxLength": 2000}`，即**长度约束作用在联合之外**；
    而 `Field(max_length=...)` 会让 Pydantic 输出
    `anyOf: [{type: string, maxLength: 2000}, {type: null}]`（约束落进分支里），
    解引用后与基线不同。显式声明 `anyOf` 才能得到基线形状；
    配套的 `field_validator` 才是真正执行校验的那一层。
    `category` 非空，直接 `max_length` 即可。
    """

    model_config = ConfigDict(extra="forbid")

    generation_id: uuid.UUID
    rating: int = Field(ge=1, le=5)
    category: str = Field(max_length=CATEGORY_MAX_LENGTH)
    content: Annotated[
        str | None,
        WithJsonSchema(
            {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "maxLength": CONTENT_MAX_LENGTH,
            }
        ),
    ] = None

    @field_validator("content")
    @classmethod
    def _check_content_length(cls, value: Any) -> Any:
        """同上：Schema 里的 `maxLength` 需要显式校验来配套。"""

        if isinstance(value, str) and len(value) > CONTENT_MAX_LENGTH:
            raise ValueError(f"content 长度不得超过 {CONTENT_MAX_LENGTH} 字符。")
        return value
