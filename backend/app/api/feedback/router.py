"""反馈路由（ENG-09）。

契约：`03-api-contract.md` 第 4.4 节；形状、状态码、参数与 `security`
逐字对齐 `shared/api-contract/openapi.json` 的 `createFeedback`。

| 方法 | 路径 | operationId | 权限 | 成功 | 基线声明的错误码 | 幂等 |
|---|---|---|---|---|---|---|
| `POST` | `/api/v1/feedback` | `createFeedback` | 已登录且邮箱已验证 | `201` | 401/403/404/422 | 必须 |

对齐口径：

- 基线**声明了 `422`**（请求体校验），因此这里走正常的声明式 `IdempotencyKey` +
  `FeedbackCreateRequest`，与 `createGeneration`、`createAssetUpload` 同一先例；
- 响应集合仍用 `openapi_extra["responses"]` 显式给出：基线的 `Envelope.data` 是
  **裸 object**（`{"type": "object"}`），而 `response_model=Envelope` 会输出空 schema
  `{}`，两者不等（原因与实测见 `app.schemas.common` 的助手说明与
  `app/api/shares/router.py` 的模块说明）。

已知声明遗漏（记录，不改基线）：`03-api-contract.md` 第 4.4 节的反馈 operation
响应集合里没有 `409`，而幂等冲突/处理中在全局语义下是 `409`
（`IDEMPOTENCY_CONFLICT` / `IDEMPOTENCY_IN_PROGRESS`，契约第 9 节）。
本卡按全局语义返回 `409`，**不修改冻结基线**；该遗漏登记在交接文档里待裁决。

响应 `data` 只含反馈本身（id、评分、分类、正文、处理状态、时间），
不含 Prompt、Provider 原文、点数或内部审计信息。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import (
    IdempotencyKey,
    get_feedback_service,
    get_verified_current_user,
    request_id_of_request,
)
from app.db.models.identity import User
from app.schemas.common import baseline_envelope_response, baseline_error_response
from app.schemas.engagement import FeedbackCreateRequest

__all__ = ["feedback_router"]

FEEDBACK_RESPONSE_DESCRIPTION = "Feedback"

#: 冻结基线该 operation 的响应集合（含 422：请求体校验，基线已声明）
#: 201 解引用后是 `Envelope` + `data: {}`（基线未约束 data），因此显式传 `{}`。
_FEEDBACK_RESPONSES: dict[str, dict] = {
    "201": baseline_envelope_response(FEEDBACK_RESPONSE_DESCRIPTION, data={}),
    "401": baseline_error_response(),
    "403": baseline_error_response(),
    "404": baseline_error_response(),
    "422": baseline_error_response(),
}

feedback_router = APIRouter(prefix="/feedback", tags=["feedback"])


def _feedback_data(row) -> dict[str, object]:
    """反馈响应载荷：只暴露用户自己提交的内容与处理状态。"""

    return {
        "id": str(row.id),
        "generation_id": str(row.generation_id),
        "rating": row.rating,
        "category": row.category,
        "content": row.content,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@feedback_router.post(
    "",
    response_model=None,
    status_code=201,
    operation_id="createFeedback",
    summary="",
    openapi_extra={
        "responses": _FEEDBACK_RESPONSES,
        "x-idempotent": True,
    },
)
def create_feedback(
    request: Request,
    payload: FeedbackCreateRequest,
    idempotency_key: IdempotencyKey,
    user: User = Depends(get_verified_current_user),
    service=Depends(get_feedback_service),
):
    # 提交反馈：所有权 + 可反馈终态 + 每用户每任务一条 + 持久幂等
    row = service.submit(
        user_id=user.id,
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=request_id_of_request(request),
    )
    return {
        "data": _feedback_data(row),
        "request_id": request_id_of_request(request),
    }
