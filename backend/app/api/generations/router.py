"""生成任务路由（ENG-06 第 5 步）。

契约：`03-api-contract.md` 第 4.3 节；形状、状态码、参数顺序与 `security`
逐字对齐 `shared/api-contract/openapi.json` 的
`createGeneration` / `getGeneration` / `listGenerationOutputs` /
`listMyGenerations` / `retryGeneration`。

| 方法 | 路径 | operationId | 成功 | 基线声明的错误码 | 幂等 |
|---|---|---|---|---|---|
| `POST` | `/api/v1/generations` | `createGeneration` | `202` | 401/403/409/422 | 必须 |
| `GET` | `/api/v1/generations/{generation_id}` | `getGeneration` | `200` | 401/404 | 否 |
| `GET` | `/api/v1/generations/{generation_id}/outputs` | `listGenerationOutputs` | `200` | 401/404 | 否 |
| `GET` | `/api/v1/me/generations` | `listMyGenerations` | `200` | 401 | 否 |
| `POST` | `/api/v1/generations/{generation_id}/retry` | `retryGeneration` | `202` | 401/404/409 | 必须 |

OpenAPI 对齐口径（沿用 ENG-02/03/05 已实测确立的先例）：

- 端点不写 docstring：FastAPI 会把它输出成 operation description，基线没有该字段；
- 基线里 `getGeneration`、`listGenerationOutputs`、`listMyGenerations`、
  `retryGeneration` **都没有声明 `422`**，而 FastAPI 只要存在受校验参数就会自动补
  `422`；且 `retry` 的基线参数顺序是 `[Idempotency-Key, generation_id]`，
  FastAPI 恒把 path 参数排在 header 之前、顺序不可控。这四条因此用
  `openapi_extra.parameters` 显式给出参数，并在处理函数里手动校验与解析；
- `POST /generations` 的基线声明了 422，因此走正常的 `IdempotencyKey` 声明式参数；
- 越权与不存在统一 `GENERATION_NOT_FOUND`（404），不泄露任务是否存在；
- 分页 `data` 条目在基线 `PageEnvelope` 里是未约束的 `{}`，因此两个列表响应用
  通用 `PageEnvelope` + 服务端组装的 dict，不引入基线未约束的模型。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.api.deps import (
    IDEMPOTENCY_KEY_PARAMETER,
    IdempotencyKey,
    get_current_user,
    get_generation_service,
    request_id_of_request,
    require_idempotency_key,
)
from app.db.models.identity import User
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.schemas.common import ErrorResponse, PageEnvelope, PageMeta
from app.schemas.generation import Generation, GenerationCreateRequest, GenerationEnvelope
from app.services.generation.commands import NewGenerationCommand
from app.services.generation.mappers import to_api_generation
from app.services.generation.service import GenerationService

__all__ = ["generations_router", "me_generations_router"]

ERROR_RESPONSE_DESCRIPTION = "Stable API error"
GENERATION_ACCEPTED_DESCRIPTION = "Generation queued"
GENERATION_DESCRIPTION = "Generation"
GENERATION_PAGE_DESCRIPTION = "Generation page"
ASSET_PAGE_DESCRIPTION = "Asset page"

#: 基线 `components.parameters.GenerationId`
GENERATION_ID_PARAMETER: dict[str, object] = {
    "name": "generation_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "format": "uuid"},
}

#: 基线 `components.parameters.Cursor`
CURSOR_PARAMETER: dict[str, object] = {
    "name": "cursor",
    "in": "query",
    "required": False,
    "schema": {"type": "string"},
}

#: 基线 `components.parameters.Limit`
LIMIT_PARAMETER: dict[str, object] = {
    "name": "limit",
    "in": "query",
    "required": False,
    "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
}

#: 基线顺序：先 `Idempotency-Key`，后路径参数
_HEADER_THEN_PATH_PARAMETERS: list[dict[str, object]] = [
    IDEMPOTENCY_KEY_PARAMETER,
    GENERATION_ID_PARAMETER,
]

#: 分页默认与上限（`03-api-contract.md` 第 2.2 节）
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

generations_router = APIRouter(prefix="/generations", tags=["generations"])
me_generations_router = APIRouter(prefix="/me", tags=["generations"])


def _generation_id_of(request: Request) -> uuid.UUID:
    """解析未经 FastAPI 校验的路径参数；非法值按 `VALIDATION_ERROR` 拒绝。"""

    raw = str(request.path_params.get("generation_id", ""))
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, details={"fields": ["generation_id"]}
        ) from exc


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


@generations_router.post(
    "",
    response_model=GenerationEnvelope,
    status_code=202,
    operation_id="createGeneration",
    summary="",
    responses={
        202: {"description": GENERATION_ACCEPTED_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        403: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        409: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        422: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"x-idempotent": True},
)
def create_generation(
    request: Request,
    payload: GenerationCreateRequest,
    idempotency_key: IdempotencyKey,
    user: User = Depends(get_current_user),
    service: GenerationService = Depends(get_generation_service),
) -> GenerationEnvelope:
    # 创建任务：成本快照 -> 预留 -> QUEUED -> 提交后才投递队列
    outcome = service.create_generation(
        user_id=user.id,
        command=NewGenerationCommand(
            mode=payload.mode.value,
            prompt=payload.prompt,
            output_count=payload.output_count,
            style_id=payload.style_id,
            prompt_template_id=payload.prompt_template_id,
            input_asset_ids=tuple(payload.input_asset_ids),
        ),
        idempotency_key=idempotency_key,
    )
    return GenerationEnvelope(
        data=_generation_of(outcome.task), request_id=request_id_of_request(request)
    )


@generations_router.get(
    "/{generation_id}",
    response_model=GenerationEnvelope,
    operation_id="getGeneration",
    summary="",
    responses={
        200: {"description": GENERATION_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        404: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"parameters": [GENERATION_ID_PARAMETER]},
)
def get_generation(
    request: Request,
    user: User = Depends(get_current_user),
    service: GenerationService = Depends(get_generation_service),
) -> GenerationEnvelope:
    # 读取单个任务；非所有者按 404 处理
    task = service.get_generation(
        generation_id=_generation_id_of(request), requester_user_id=user.id
    )
    return GenerationEnvelope(
        data=_generation_of(task), request_id=request_id_of_request(request)
    )


@generations_router.get(
    "/{generation_id}/outputs",
    response_model=PageEnvelope,
    operation_id="listGenerationOutputs",
    summary="",
    responses={
        200: {"description": ASSET_PAGE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        404: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"parameters": [GENERATION_ID_PARAMETER]},
)
def list_generation_outputs(
    request: Request,
    user: User = Depends(get_current_user),
    service: GenerationService = Depends(get_generation_service),
) -> PageEnvelope:
    # 结果资源页：基线该 operation 没有分页参数，因此整页返回且 next_cursor 为 null
    rows = service.list_generation_outputs(
        generation_id=_generation_id_of(request), requester_user_id=user.id
    )
    return PageEnvelope(
        data=[
            {
                "asset_id": str(row.asset_id),
                "output_index": row.output_index,
                "mime_type": row.mime_type,
                "width": row.width,
                "height": row.height,
                "size_bytes": row.size_bytes,
            }
            for row in rows
        ],
        page=PageMeta(next_cursor=None, has_more=False),
        request_id=request_id_of_request(request),
    )


@me_generations_router.get(
    "/generations",
    response_model=PageEnvelope,
    operation_id="listMyGenerations",
    summary="",
    responses={
        200: {"description": GENERATION_PAGE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"parameters": [CURSOR_PARAMETER, LIMIT_PARAMETER]},
)
def list_my_generations(
    request: Request,
    user: User = Depends(get_current_user),
    service: GenerationService = Depends(get_generation_service),
) -> PageEnvelope:
    # 我的任务列表：键集分页游标由服务端编码，客户端只能原样回传
    page = service.list_generations(
        user_id=user.id, cursor=_query_cursor(request), limit=_query_limit(request)
    )
    return PageEnvelope(
        data=[_generation_of(task).model_dump(mode="json") for task in page.rows],
        page=PageMeta(next_cursor=page.next_cursor, has_more=page.has_more),
        request_id=request_id_of_request(request),
    )


@generations_router.post(
    "/{generation_id}/retry",
    response_model=GenerationEnvelope,
    status_code=202,
    operation_id="retryGeneration",
    summary="",
    responses={
        202: {"description": GENERATION_ACCEPTED_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        404: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        409: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={
        "parameters": _HEADER_THEN_PATH_PARAMETERS,
        "x-idempotent": True,
    },
)
def retry_generation(
    request: Request,
    user: User = Depends(get_current_user),
    service: GenerationService = Depends(get_generation_service),
) -> GenerationEnvelope:
    # 用户重试：只接受 FAILED，创建新任务并挂 parent_task_id，旧任务保持终态
    outcome = service.retry_generation(
        user_id=user.id,
        generation_id=_generation_id_of(request),
        idempotency_key=require_idempotency_key(request),
    )
    return GenerationEnvelope(
        data=_generation_of(outcome.task), request_id=request_id_of_request(request)
    )


def _generation_of(task) -> Generation:
    """DB 行 -> 契约 DTO（`parent_task_id` 对外映射为 `parent_generation_id`）。"""

    return to_api_generation(task)
