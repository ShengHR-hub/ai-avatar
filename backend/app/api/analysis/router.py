"""参考图分析路由（「参考图片找灵感」）。

契约：`03-api-contract.md` 第 4.6 节；形状、状态码与参数逐字对齐
`shared/api-contract/openapi.json` 的 `createReferenceAnalysis`。

| 方法 | 路径 | operationId | 权限 | 成功 | 幂等 |
|---|---|---|---|---:|---|
| `POST` | `/api/v1/analyses` | `createReferenceAnalysis` | 已登录且邮箱已验证 | `200` | 必须 |

对齐口径（沿用 `assets` 已确立的两条裁决）：

- 端点**不写 docstring**：FastAPI 会把它作为 operation description 输出，
  而冻结基线的 operation 不含该字段；
- 本路由只有 Header 参数与请求体，没有路径参数，因此 `Idempotency-Key`
  按真实参数声明即可（`createAssetUpload` 同一形态），基线的 `parameters`
  顺序也一致。

响应 `data` 的字段集合在这里冻结：只给描述文本、实际使用的型号、
对应的资产与「这次有没有复用缓存」。**不给**原图 URL、对象存储 key、
Provider 原始响应，也不给 token 用量——那既泄露内部成本口径，也没有界面用途。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import (
    IdempotencyKey,
    get_analysis_service,
    get_verified_current_user,
    request_id_of_request,
)
from app.db.models.identity import User
from app.schemas.analysis import ReferenceDescriptionRequest
from app.schemas.common import Envelope, ErrorResponse
from app.services.analysis.service import AnalysisService

__all__ = ["analyses_router"]

ERROR_RESPONSE_DESCRIPTION = "Stable API error"
DESCRIPTION_RESPONSE_DESCRIPTION = "Image description"

analyses_router = APIRouter(tags=["analysis"])


@analyses_router.post(
    "/analyses",
    response_model=Envelope,
    status_code=200,
    operation_id="createReferenceAnalysis",
    summary="",
    responses={
        200: {"description": DESCRIPTION_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        403: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        404: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        409: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        422: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        429: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        502: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        503: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"x-idempotent": True},
)
def create_reference_analysis(
    request: Request,
    payload: ReferenceDescriptionRequest,
    idempotency_key: IdempotencyKey,
    user: User = Depends(get_verified_current_user),
    service: AnalysisService = Depends(get_analysis_service),
) -> Envelope:
    # 读一张用户自己的参考图，换回一段可编辑的画面描述；不建任务、不占点数。
    # 幂等 Header 只校验存在性与格式：这里的重复保护按「同一张图」判定
    # （`services/analysis/guard.py` 的摘要缓存与单飞锁），比按请求键更贴合实际付费单位。
    del idempotency_key
    outcome = service.describe_reference_image(
        user_id=user.id, asset_id=payload.asset_id
    )
    return Envelope(
        data={
            "asset_id": str(outcome.asset_id),
            "description": outcome.description,
            "model": outcome.model,
            "from_cache": outcome.from_cache,
        },
        request_id=request_id_of_request(request),
    )
