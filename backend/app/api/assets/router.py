"""媒体资源路由（ENG-05 第 4 步）。

契约：`03-api-contract.md` 第 4.2 节；形状、状态码与参数逐字对齐
`shared/api-contract/openapi.json` 的 `createAssetUpload` / `completeAssetUpload` /
`getAsset` / `createAssetDownloadUrl` / `deleteAsset`。

| 方法 | 路径 | operationId | 权限 | 成功 | 幂等 |
|---|---|---|---|---:|---|
| `POST` | `/api/v1/assets/uploads` | `createAssetUpload` | 已登录且邮箱已验证 | `201` | 必须 |
| `POST` | `/api/v1/assets/{asset_id}/complete` | `completeAssetUpload` | 资源所有者 | `200` | 必须 |
| `GET` | `/api/v1/assets/{asset_id}` | `getAsset` | 所有者或分享可见 | `200` | 否 |
| `POST` | `/api/v1/assets/{asset_id}/download-url` | `createAssetDownloadUrl` | 所有者或分享可见 | `200` | 必须 |
| `DELETE` | `/api/v1/assets/{asset_id}` | `deleteAsset` | 资源所有者 | `204` | 必须 |

OpenAPI 对齐口径（沿用 ENG-02/03 已确立的裁决与 `logout` 的先例）：

- 端点**不写 docstring**：FastAPI 会把它作为 operation description 输出，
  而冻结基线的 operation 不含该字段；
- 基线里同时带 Header 与路径参数的 operation，`parameters` 顺序是
  `[Idempotency-Key, asset_id]`；FastAPI 生成时恒把 path 参数排在 header 之前
  （`dependant.path_params + ... + header_params`），顺序无法由声明控制，
  也抑制不了它自动补的 `422`（基线的 `getAsset`/`deleteAsset`/
  `createAssetDownloadUrl` 都没有 422）。因此这四条改用
  `openapi_extra.parameters` 显式给出参数顺序，并在处理函数里手动校验
  Header 与解析 `asset_id`；`createAssetUpload` 只有 Header 参数，正常声明即可；
- `getAsset` 与 `createAssetDownloadUrl` 的基线 `security` 是 `[]`（分享页匿名读取入口），
  所以这两条不引入 `Security(...)` 依赖，改为手动读 Cookie 做**软认证**，
  授权判定全部留在 `AssetService`；
- 响应级 `description` 逐字取自基线。
"""

from __future__ import annotations

import uuid
from datetime import timezone

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import (
    IDEMPOTENCY_KEY_PARAMETER,
    SESSION_COOKIE_NAME,
    IdempotencyKey,
    get_asset_service,
    get_session_store,
    get_verified_current_user,
    request_id_of_request,
    require_idempotency_key,
)
from app.db.models.identity import User
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.schemas.asset import Asset, AssetEnvelope, UploadIntentRequest
from app.schemas.common import Envelope, ErrorResponse
from app.services.assets.mappers import to_api_asset
from app.services.assets.service import AssetService, UploadIntentOutcome
from app.services.auth.session_store import RedisSessionStore

__all__ = ["assets_router"]

ERROR_RESPONSE_DESCRIPTION = "Stable API error"
UPLOAD_INTENT_RESPONSE_DESCRIPTION = "Upload intent"
ASSET_RESPONSE_DESCRIPTION = "Media asset"
DOWNLOAD_URL_RESPONSE_DESCRIPTION = "Short-lived download URL"
ASSET_DELETED_RESPONSE_DESCRIPTION = "Asset logically deleted"

#: 基线 `components.parameters.AssetId`（与 `Asset.id` 同为 `format: uuid`）
ASSET_ID_PARAMETER: dict[str, object] = {
    "name": "asset_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "format": "uuid"},
}

#: 基线顺序：先 `Idempotency-Key`，后路径参数
_HEADER_THEN_PATH_PARAMETERS: list[dict[str, object]] = [
    IDEMPOTENCY_KEY_PARAMETER,
    ASSET_ID_PARAMETER,
]

assets_router = APIRouter(prefix="/assets", tags=["assets"])


def _viewer_id(
    request: Request, store: RedisSessionStore = Depends(get_session_store)
) -> uuid.UUID | None:
    """软认证：带会话就按所有者处理，不带就按匿名处理（不产生 OpenAPI security 要求）。"""

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    record = store.read(token)
    return record.user_id if record is not None else None


def _asset_id_of(request: Request) -> uuid.UUID:
    """解析未由 FastAPI 校验的路径参数；非法值按 `VALIDATION_ERROR`（422）拒绝。"""

    raw = str(request.path_params.get("asset_id", ""))
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["asset_id"]}) from exc


@assets_router.post(
    "/uploads",
    response_model=Envelope,
    status_code=201,
    operation_id="createAssetUpload",
    summary="",
    responses={
        201: {"description": UPLOAD_INTENT_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        403: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        413: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        415: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        422: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"x-idempotent": True},
)
def create_asset_upload(
    request: Request,
    payload: UploadIntentRequest,
    idempotency_key: IdempotencyKey,
    user: User = Depends(get_verified_current_user),
    service: AssetService = Depends(get_asset_service),
) -> Envelope:
    # 创建上传意图：落一条 PENDING_UPLOAD 元数据行并签发短期 PUT URL。
    outcome = service.create_upload_intent(
        user_id=user.id, intent=payload, idempotency_key=idempotency_key
    )
    return Envelope(
        data=_upload_intent_data(outcome),
        request_id=request_id_of_request(request),
    )


@assets_router.post(
    "/{asset_id}/complete",
    response_model=AssetEnvelope,
    operation_id="completeAssetUpload",
    summary="",
    responses={
        200: {"description": ASSET_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        403: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        404: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        409: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        422: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={
        "parameters": _HEADER_THEN_PATH_PARAMETERS,
        "x-idempotent": True,
    },
)
def complete_asset_upload(
    request: Request,
    user: User = Depends(get_verified_current_user),
    service: AssetService = Depends(get_asset_service),
) -> AssetEnvelope:
    # 完成上传：读对象 -> 嗅探/解码/哈希与尺寸校验 -> 按观测值置 READY。
    require_idempotency_key(request)
    asset = service.complete_upload(
        user_id=user.id, asset_id=_asset_id_of(request)
    )
    return AssetEnvelope(
        data=to_api_asset(asset), request_id=request_id_of_request(request)
    )


@assets_router.get(
    "/{asset_id}",
    response_model=AssetEnvelope,
    operation_id="getAsset",
    summary="",
    responses={
        200: {"description": ASSET_RESPONSE_DESCRIPTION},
        403: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        404: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"parameters": [ASSET_ID_PARAMETER], "security": []},
)
def get_asset(
    request: Request,
    viewer: uuid.UUID | None = Depends(_viewer_id),
    service: AssetService = Depends(get_asset_service),
) -> AssetEnvelope:
    # 读取资源元数据；非所有者且未分享的资源按 404 处理，不泄露存在性。
    asset = service.get_asset(asset_id=_asset_id_of(request), viewer_user_id=viewer)
    return AssetEnvelope(
        data=to_api_asset(asset), request_id=request_id_of_request(request)
    )


@assets_router.post(
    "/{asset_id}/download-url",
    response_model=Envelope,
    operation_id="createAssetDownloadUrl",
    summary="",
    responses={
        200: {"description": DOWNLOAD_URL_RESPONSE_DESCRIPTION},
        403: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        404: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        409: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={
        "parameters": _HEADER_THEN_PATH_PARAMETERS,
        "security": [],
        "x-idempotent": True,
    },
)
def create_asset_download_url(
    request: Request,
    viewer: uuid.UUID | None = Depends(_viewer_id),
    service: AssetService = Depends(get_asset_service),
) -> Envelope:
    # 只交付短期 URL；桶保持私有，签名值不得进日志或错误响应。
    require_idempotency_key(request)
    signed = service.create_download_url(
        asset_id=_asset_id_of(request), viewer_user_id=viewer
    )
    return Envelope(
        data={
            "asset_id": str(_asset_id_of(request)),
            "method": "GET",
            "url": signed.url,
            "expires_at": signed.expires_at.astimezone(timezone.utc).isoformat(),
        },
        request_id=request_id_of_request(request),
    )


@assets_router.delete(
    "/{asset_id}",
    status_code=204,
    operation_id="deleteAsset",
    summary="",
    responses={
        204: {"description": ASSET_DELETED_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        403: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        404: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={
        "parameters": _HEADER_THEN_PATH_PARAMETERS,
        "x-idempotent": True,
    },
)
def delete_asset(
    request: Request,
    user: User = Depends(get_verified_current_user),
    service: AssetService = Depends(get_asset_service),
) -> Response:
    # 逻辑删除：删对象二进制 + 写 deleted_at/DELETED；不级联任务、流水与审计。
    require_idempotency_key(request)
    service.delete_asset(user_id=user.id, asset_id=_asset_id_of(request))
    return Response(status_code=204)


def _upload_intent_data(outcome: UploadIntentOutcome) -> dict[str, object]:
    """上传意图响应 body。

    基线该响应用的是通用 `Envelope`（`data` 不约束），因此这里的字段集合由本卡冻结：
    只暴露资源身份与直传所需的三要素，`storage_key`、`storage_provider` 与服务端
    幂等键都不外泄（`09-repository-boundary.md` 第 7 节）。
    """

    asset: Asset = to_api_asset(outcome.asset)
    return {
        "asset_id": str(asset.id),
        "purpose": asset.purpose.value,
        "status": asset.status.value,
        "method": outcome.method,
        "upload_url": outcome.upload_url,
        "expires_at": outcome.expires_at.astimezone(timezone.utc).isoformat(),
    }
