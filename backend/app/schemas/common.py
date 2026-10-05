"""统一响应 envelope（ENG-02 第 2-4 步）。

唯一来源：`docs/architecture/03-api-contract.md` 第 2.2、2.3 节，
以及 `shared/api-contract/openapi.json`（LEAD-03 冻结基线）。

成功（单对象或命令响应）：

```json
{ "data": {}, "request_id": "req_01J..." }
```

成功（分页）：

```json
{ "data": [], "page": { "next_cursor": "opaque-cursor", "has_more": true }, "request_id": "req_01J..." }
```

错误：

```json
{ "error": { "code": "INSUFFICIENT_POINTS", "message": "可用点数不足", "details": {} }, "request_id": "req_01J..." }
```

与冻结基线对齐的口径（技术负责人 2026-09-26 裁决 ①③④）：

- 本模块的模型形状必须使**解引用后的响应内容**与基线一致，因此不设置
  `extra="forbid"`（基线未声明 `additionalProperties: false`）；
- `Error.code` 在 OpenAPI 中必须是 `type: string`、**不生成 enum**；Python 内部仍用
  `app.errors.codes.ErrorCode` 做错误映射与合法性校验，序列化时输出 `ErrorCode.value`
  （`StrEnum` 实例在 `code: str` 字段上会被规范化为纯字符串）；
- `Error.details` 是必填字段（基线 `required` 含 `details`）；
- `PageEnvelope.page.next_cursor` 是必填字段（基线 `required` 含 `next_cursor`，类型 `string|null`）；
- `message` 仅用于展示，前端只能依赖 `error.code`；`details` 不得包含堆栈、SQL、
  Provider 原始响应或密钥；
- 删除成功返回 `204 No Content`，不得返回 JSON body（不适用本模块的 envelope）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "Envelope",
    "ErrorDetail",
    "ErrorResponse",
    "PageEnvelope",
    "PageMeta",
    "PaginationQuery",
    "baseline_error_response",
    "baseline_envelope_response",
    "baseline_request_body",
]

#: 分页默认条数与上限（契约第 2.2 节）
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100


class Envelope(BaseModel):
    """成功响应 envelope 基类：`data` 为任意 JSON 值。

    具体路由通过子类收窄 `data`（例如 `app.schemas.health.HealthEnvelope`），
    与冻结基线的 `Envelope` + 命名子类结构一致。
    """

    data: Any
    request_id: str


class PageMeta(BaseModel):
    """游标分页元数据；`next_cursor` 为必填（可为 `null`）。"""

    next_cursor: str | None
    has_more: bool


class PageEnvelope(BaseModel):
    """统一分页响应 envelope。"""

    data: list[Any]
    page: PageMeta
    request_id: str


class ErrorDetail(BaseModel):
    """错误明细：稳定错误码字符串 + 展示文案 + 安全的结构化细节。

    `code` 故意声明为 `str`（而非 `ErrorCode` 枚举），使 OpenAPI 输出
    `type: string` 且不生成 enum——这是冻结基线的形状要求。
    """

    code: str
    message: str
    details: dict[str, Any] = Field(...)


class ErrorResponse(BaseModel):
    """统一错误响应 envelope。"""

    error: ErrorDetail
    request_id: str


class PaginationQuery(BaseModel):
    """游标分页查询参数（`cursor` 为不透明字符串，原样回传）。

    这是查询参数模型，不是响应 Schema；对未知查询参数保持严格拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    cursor: str | None = None
    limit: int = Field(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT)


# --------------------------------------------------------------------------- #
# 冻结基线形状的显式 OpenAPI 片段（ENG-09）
# --------------------------------------------------------------------------- #
#: 基线 `Envelope.data` 是**裸 object**：`{"type": "object"}`（没有 properties，
#: 也没有 `additionalProperties`）。Pydantic 的 `Any`/`object` 会被 FastAPI 输出成
#: 空 schema（`{}`），`dict` 会额外带上 `additionalProperties: true`，两者都不等于基线。
#: 因此对「基线把 `data` 保持未约束」的 operation，用下面两个助手显式声明响应，
#: 而不是靠 `response_model` 反推——形状由冻结基线逐字决定。
_JSON_MEDIA_TYPE = "application/json"

#: 响应描述必须逐字与基线一致
BASELINE_ERROR_DESCRIPTION = "Stable API error"


def baseline_envelope_response(description: str, *, data: Any = None) -> dict[str, Any]:
    """构造与基线一致的 `Envelope`（或 `ShareEnvelope`）响应片段。

    **内联**信封形状而不是 `$ref` 到运行时的 `Envelope`：契约测试在比较前会递归解引用，
    而 `$ref` 会展开成运行时自己的 `Envelope`，其 `data` 形状与具体基线未必一致
    （例如基线 `ShareEnvelope.data` 是 `{"type": "object"}`，而运行时通用 `Envelope`
    的 `data` 是 `{}`）。内联后两边解引用结果逐字相同。

    - `data is None` → `data` 为 `{"type": "object"}`（`ShareEnvelope` 这类
      「基线写成 object」的响应）；
    - `data` 传具体片段（`{}` 表示不约束）→ 原样使用。`{}` 用于基线里 `data: {}`
      的通用 `Envelope`（如 `createFeedback` 的 201）。
    """

    envelope_properties: dict[str, Any] = {
        "data": {"type": "object"} if data is None else data,
        "request_id": {"type": "string"},
    }
    return {
        "description": description,
        "content": {
            _JSON_MEDIA_TYPE: {
                "schema": {
                    "type": "object",
                    "properties": envelope_properties,
                    "required": ["data", "request_id"],
                }
            }
        },
    }


def baseline_error_response() -> dict[str, Any]:
    """构造与基线一致的错误响应片段（`ErrorResponse` + `Stable API error`）。"""

    return {
        "description": BASELINE_ERROR_DESCRIPTION,
        "content": {
            _JSON_MEDIA_TYPE: {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
            }
        },
    }


def baseline_request_body(model: type[BaseModel]) -> dict[str, Any]:
    """构造与基线一致的 JSON 请求体片段，**内联**该模型的 Schema。

    为什么不写 `$ref: #/components/schemas/<Model>`：在 `openapi_extra` 里手工声明
    的请求体不会让 FastAPI 把该模型注册进 `components.schemas`，运行时会留下一个
    悬空引用（契约测试解引用时直接 `KeyError`）。因此这里把模型自己的 JSON Schema
    内联进来 —— 单一真相仍是同一个 Pydantic 模型，不存在第二套手写 Schema。
    """

    schema = model.model_json_schema()
    definitions = schema.pop("$defs", None)
    if definitions:
        schema["$defs"] = definitions
    return {
        "required": True,
        "content": {_JSON_MEDIA_TYPE: {"schema": schema}},
    }
