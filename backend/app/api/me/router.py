"""当前用户路由（ENG-03 第 3 步；ENG-04 点数读取补口）。

契约：`03-api-contract.md` 第 3.2、4.1 节。

| 方法 | 路径 | operationId | 成功 |
|---|---|---|---|
| `GET` | `/api/v1/me` | `getMe` | `200` |
| `PATCH` | `/api/v1/me` | `updateMe` | `200` |
| `GET` | `/api/v1/me/points` | `getPointBalance` | `200` |
| `GET` | `/api/v1/me/points/ledger` | `listPointLedger` | `200` |

`PATCH` 只接受 `display_name`、`avatar_asset_id`、`locale`、`timezone`；
未知字段由 Schema 拒绝，`avatar_asset_id` 归属由 Service 校验。
点数两个读取口只读 `point_wallets` 快照与 `point_ledger` 流水，**不接受任何写入**：
契约第 4.1 节明确「客户端不得把余额当作可写资源」，因此余额与增量一律由服务端给出，
前端不做算术。`metadata`（管理员工单、调整留痕等审计上下文）不进入用户侧响应。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import (
    CurrentSession,
    IdempotencyKey,
    get_current_session,
    get_current_user,
    get_points_service,
    get_profile_service,
    request_id_of_request,
)
from app.db.models.points import PointLedger
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.schemas.auth import UpdateMeRequest
from app.schemas.common import ErrorResponse, PageEnvelope, PageMeta
from app.schemas.user import PointBalanceEnvelope, User, UserEnvelope
from app.services.auth.profile import ProfileService
from app.services.points.service import PointsService

__all__ = ["me_router"]

ERROR_RESPONSE_DESCRIPTION = "Stable API error"
USER_RESPONSE_DESCRIPTION = "User"
POINT_BALANCE_RESPONSE_DESCRIPTION = "Point balance"
POINT_LEDGER_PAGE_RESPONSE_DESCRIPTION = "Point ledger page"

#: 基线 `components.parameters.Cursor` / `Limit`（与 `PageEnvelope` 一样，
#: 分页参数用 `openapi_extra` 声明并在处理函数里手动校验：只要存在受 FastAPI
#: 校验的查询参数，运行时就会自动补一个基线里没有的 `422`。）
CURSOR_PARAMETER: dict[str, object] = {
    "name": "cursor",
    "in": "query",
    "required": False,
    "schema": {"type": "string"},
}

LIMIT_PARAMETER: dict[str, object] = {
    "name": "limit",
    "in": "query",
    "required": False,
    "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
}

#: 分页默认与上限（`03-api-contract.md` 第 2.2 节）
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

me_router = APIRouter(prefix="/me", tags=["me"])


def _query_limit(request: Request) -> int:
    raw = request.query_params.get("limit")
    if raw is None or not raw.strip():
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["limit"]}) from exc
    if not 1 <= value <= MAX_LIMIT:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["limit"]})
    return value


def _query_cursor(request: Request) -> str | None:
    raw = request.query_params.get("cursor")
    if raw is None:
        return None
    if not raw.strip():
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["cursor"]})
    return raw


def _ledger_row_of(entry: PointLedger) -> dict[str, Any]:
    """用户侧流水条目：只暴露账务字段与归属引用，不带审计元数据。"""

    return {
        "id": str(entry.id),
        "entry_type": entry.entry_type,
        "amount": entry.amount,
        "available_delta": entry.available_delta,
        "reserved_delta": entry.reserved_delta,
        "reason": entry.reason,
        "generation_id": str(entry.generation_id) if entry.generation_id else None,
        "order_id": str(entry.order_id) if entry.order_id else None,
        "created_at": entry.created_at.isoformat(),
    }


@me_router.get(
    "",
    response_model=UserEnvelope,
    operation_id="getMe",
    summary="",
    responses={
        200: {"description": USER_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
)
def get_me(
    request: Request,
    current: CurrentSession = Depends(get_current_session),
    service: ProfileService = Depends(get_profile_service),
) -> UserEnvelope:
    # 读取当前用户资料（含主邮箱摘要）。端点不写 docstring：FastAPI 会把 docstring 作为
    # operation description 输出，而冻结基线的 operation 不含该字段。
    user = service.get_profile(user_id=current.record.user_id)
    return UserEnvelope(data=user, request_id=request_id_of_request(request))


@me_router.patch(
    "",
    response_model=UserEnvelope,
    operation_id="updateMe",
    summary="",
    responses={
        200: {"description": USER_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        422: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"x-idempotent": True},
)
def update_me(
    request: Request,
    payload: UpdateMeRequest,
    idempotency_key: IdempotencyKey,
    current: CurrentSession = Depends(get_current_session),
    service: ProfileService = Depends(get_profile_service),
) -> UserEnvelope:
    # 按白名单更新当前用户资料。端点不写 docstring，原因见 get_me。
    del idempotency_key
    user = service.update_profile(user_id=current.record.user_id, patch=payload)
    return UserEnvelope(data=user, request_id=request_id_of_request(request))


@me_router.get(
    "/points",
    response_model=PointBalanceEnvelope,
    operation_id="getPointBalance",
    summary="",
    responses={
        200: {"description": POINT_BALANCE_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
)
def get_point_balance(
    request: Request,
    user: User = Depends(get_current_user),
    service: PointsService = Depends(get_points_service),
) -> PointBalanceEnvelope:
    # 余额快照直接取钱包行；钱包行缺失时 Service 返回全零，不因此创建钱包。
    return PointBalanceEnvelope(
        data=service.balance(user_id=user.id),
        request_id=request_id_of_request(request),
    )


@me_router.get(
    "/points/ledger",
    response_model=PageEnvelope,
    operation_id="listPointLedger",
    summary="",
    responses={
        200: {"description": POINT_LEDGER_PAGE_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"parameters": [CURSOR_PARAMETER, LIMIT_PARAMETER]},
)
def list_point_ledger(
    request: Request,
    user: User = Depends(get_current_user),
    service: PointsService = Depends(get_points_service),
) -> PageEnvelope:
    # 游标由服务端生成，客户端只能原样回传；基线 PageEnvelope 的 data 条目未约束，
    # 因此这里组装 dict 而不是引入基线不存在的模型。
    page = service.list_ledger(
        user_id=user.id, cursor=_query_cursor(request), limit=_query_limit(request)
    )
    return PageEnvelope(
        data=[_ledger_row_of(entry) for entry in page.rows],
        page=PageMeta(next_cursor=page.next_cursor, has_more=page.has_more),
        request_id=request_id_of_request(request),
    )
