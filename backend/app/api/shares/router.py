"""分享路由（ENG-09）。

契约：`03-api-contract.md` 第 4.4 节；形状、状态码、参数与 `security`
逐字对齐 `shared/api-contract/openapi.json` 的 `createShare` / `getShare`。

| 方法 | 路径 | operationId | 权限 | 成功 | 基线声明的错误码 | 幂等 |
|---|---|---|---|---|---|---|
| `POST` | `/api/v1/shares` | `createShare` | 已登录且邮箱已验证 | `201` | 401/403/409 | 必须 |
| `GET` | `/api/v1/shares/{slug}` | `getShare` | 匿名 | `200` | 404 | 否 |

OpenAPI 对齐口径（沿用 ENG-02/03/05/06 已实测确立的先例，并新增一条）：

- 端点不写 docstring：FastAPI 会把它输出成 operation description，基线没有该字段；
- 两条 operation 都用 `openapi_extra` 接管参数、`security` 与**响应集合**
  （`response_model=None`），原因有三，均已实测：
  1. 基线 `ShareEnvelope.data` 是**裸 object**，而 `response_model` 无论用
     `Any`/`object`（输出空 schema）还是 `dict`（多出 `additionalProperties: true`）
     都推不出基线的 `{"type": "object"}`；
  2. 基线参数只有 `Idempotency-Key`，而 FastAPI 只要存在受校验参数就会自动补
     `422`，且不允许用 `responses={422: None}` 抑制；
  3. 反而**只要声明了 JSON 请求体，FastAPI 就会补 `422`**（实测：即使不声明任何
     参数也会补）。`POST /shares` 的基线没有 `422`，因此请求体改为手工解析，
     由 `ShareCreateRequest` 负责 Schema 校验（见下）。
- 请求体手工解析：越界/类型错误按 `VALIDATION_ERROR`（422）返回，
  与契约第 7 节一致；`Idempotency-Key` 由 `require_idempotency_key` 校验，
  缺失 → `IDEMPOTENCY_REQUIRED`、格式非法 → `IDEMPOTENCY_INVALID`（均 400）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app.api.deps import (
    IDEMPOTENCY_KEY_PARAMETER,
    get_share_service,
    get_verified_current_user,
    request_id_of_request,
    require_idempotency_key,
)
from app.db.models.identity import User
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.schemas.common import (
    baseline_envelope_response,
    baseline_error_response,
    baseline_request_body,
)
from app.schemas.engagement import ShareCreateRequest, as_share_data

__all__ = ["shares_router"]

SHARE_RESPONSE_DESCRIPTION = "Share"

#: 基线 `components.parameters.Slug`
SLUG_PARAMETER: dict[str, object] = {
    "name": "slug",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}

#: 冻结基线里这两条 operation 的响应集合与请求体（逐字对应基线形状）
_POST_SHARE_RESPONSES: dict[str, dict] = {
    "201": baseline_envelope_response(SHARE_RESPONSE_DESCRIPTION),
    "401": baseline_error_response(),
    "403": baseline_error_response(),
    "409": baseline_error_response(),
}
_GET_SHARE_RESPONSES: dict[str, dict] = {
    "200": baseline_envelope_response(SHARE_RESPONSE_DESCRIPTION),
    "404": baseline_error_response(),
}
#: 请求体必须出现在 OpenAPI 里，但**不能**声明为受校验的 FastAPI 参数：
#: 只要声明了 JSON 请求体，FastAPI 就会自动补一个基线没有的 `422`。
#: 因此在 `openapi_extra` 里手工给出 `requestBody`，Schema 仍是同一个
#: `ShareCreateRequest`（单一真相），校验由 `_share_payload` 执行。
_POST_SHARE_REQUEST_BODY: dict[str, object] = baseline_request_body(ShareCreateRequest)

shares_router = APIRouter(prefix="/shares", tags=["shares"])


def _slug_of(request: Request) -> str:
    """解析未经 FastAPI 校验的路径参数。

    「不存在 / 已撤销 / 已过期」都按同一 404 返回（契约第 7 节）；格式异常没有单独
    错误码可用，因此并入同一条路径，不泄露 slug 是否存在。
    """

    raw = str(request.path_params.get("slug", ""))
    if not 1 <= len(raw) <= 128:
        raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
    return raw


async def _share_payload(request: Request) -> ShareCreateRequest:
    """手工解析并校验请求体（避免 FastAPI 自动补出基线没有的 `422`）。"""

    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": ["body"]}
        ) from error
    if not isinstance(raw, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["body"]})
    try:
        return ShareCreateRequest.model_validate(raw)
    except ValidationError as error:
        fields = sorted(
            {
                str(item["loc"][0])
                for item in error.errors()
                if item.get("loc")
            }
        )
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": fields or ["body"]}
        ) from error


@shares_router.post(
    "",
    response_model=None,
    status_code=201,
    operation_id="createShare",
    summary="",
    openapi_extra={
        "parameters": [IDEMPOTENCY_KEY_PARAMETER],
        "requestBody": _POST_SHARE_REQUEST_BODY,
        "responses": _POST_SHARE_RESPONSES,
        "x-idempotent": True,
    },
)
async def create_share(
    request: Request,
    user: User = Depends(get_verified_current_user),
    service=Depends(get_share_service),
):
    # 创建分享：所有权 + SUCCEEDED + 选中输出校验 → 同一事务写分享/关联/可见性
    payload = await _share_payload(request)
    view = service.create_share(
        user_id=user.id,
        payload=payload,
        idempotency_key=require_idempotency_key(request),
        request_id=request_id_of_request(request),
    )
    return {
        "data": as_share_data(view),
        "request_id": request_id_of_request(request),
    }


@shares_router.get(
    "/{slug}",
    response_model=None,
    operation_id="getShare",
    summary="",
    openapi_extra={
        "parameters": [SLUG_PARAMETER],
        "responses": _GET_SHARE_RESPONSES,
        "security": [],
    },
)
def get_share(
    request: Request,
    service=Depends(get_share_service),
):
    # 匿名读取：实时检查状态与期限，只返回被明确选中的结果
    view = service.read_share(slug=_slug_of(request))
    return {
        "data": as_share_data(view),
        "request_id": request_id_of_request(request),
    }
