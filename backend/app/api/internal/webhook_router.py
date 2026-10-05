"""Provider Webhook 入口（ENG-06 第 5 步）。

契约：`03-api-contract.md` 第 4.4 节；形状逐字对齐基线 `providerWebhook`：

| 方法 | 路径 | operationId | 成功 | 基线声明的错误码 | security | 扩展 |
|---|---|---|---|---|---|---|
| `POST` | `/api/v1/internal/providers/{provider_name}/webhook` | `providerWebhook` | `202` | 401/409 | `providerSignature` | `x-internal`、`x-idempotent` |

对齐与实现口径：

- 基线该 operation **没有** `422`，且 `parameters` 只有 `ProviderName` 与
  `Idempotency-Key`（真实供应商的时间戳/事件头在 ENG-07 选定后才验证），
  因此参数、请求体与扩展字段全部由 `openapi_extra` 显式给出；
- `requestBody` 按基线保持未约束的 JSON 对象：`{"type":"object","additionalProperties":true}`；
- 处理函数必须是 `async`：验签要在**解析前的原始字节**上做（第 4 节第 1 条），
  只有 async 入口能拿到原始 body；数据库与对象存储工作随即交回线程池执行，
  不阻塞事件循环；
- 本路由不做任何授权或结算判断：验签、防重放、关联与终态判定都在 Service 层。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Security
from fastapi.concurrency import run_in_threadpool
from fastapi.security import APIKeyHeader

from app.api.deps import (
    IDEMPOTENCY_KEY_PARAMETER,
    get_webhook_ingress,
    request_id_of_request,
    require_idempotency_key,
)
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.schemas.common import Envelope, ErrorResponse
from app.services.generation.webhook import WebhookIngress

__all__ = ["internal_router"]

ERROR_RESPONSE_DESCRIPTION = "Stable API error"
ACCEPTED_DESCRIPTION = "Accepted"

#: 基线 `components.securitySchemes.providerSignature` 的头部名
PROVIDER_SIGNATURE_HEADER_NAME = "X-Provider-Signature"

#: MOCK 通道的事件身份与时间戳头名。冻结基线的 webhook operation 只声明
#: `ProviderName` 与 `Idempotency-Key` 两个参数，因此这两个头**不进 OpenAPI**；
#: 真实供应商的头名/算法/规范化形式必须等选定供应商后（ENG-07）验证。
EVENT_ID_HEADER_NAME = "X-Provider-Event-Id"
TIMESTAMP_HEADER_NAME = "X-Provider-Timestamp"

#: 基线 `components.parameters.ProviderName`
PROVIDER_NAME_PARAMETER: dict[str, object] = {
    "name": "provider_name",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 64},
}

#: 基线顺序：先路径参数 `ProviderName`，后 `Idempotency-Key`
_WEBHOOK_PARAMETERS: list[dict[str, object]] = [
    PROVIDER_NAME_PARAMETER,
    IDEMPOTENCY_KEY_PARAMETER,
]

#: 基线 `components.requestBodies.JsonObject`（载荷刻意不约束：真实载荷形状属 ENG-07）
_JSON_REQUEST_BODY: dict[str, object] = {
    "required": True,
    "content": {
        "application/json": {"schema": {"type": "object", "additionalProperties": True}}
    },
}

provider_signature_security = APIKeyHeader(
    name=PROVIDER_SIGNATURE_HEADER_NAME,
    scheme_name="providerSignature",
    description="Provider request signature",
    auto_error=False,
)

internal_router = APIRouter(prefix="/internal", tags=["internal"])


@internal_router.post(
    "/providers/{provider_name}/webhook",
    response_model=Envelope,
    status_code=202,
    operation_id="providerWebhook",
    summary="",
    responses={
        202: {"description": ACCEPTED_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        409: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={
        "parameters": _WEBHOOK_PARAMETERS,
        "requestBody": _JSON_REQUEST_BODY,
        "x-idempotent": True,
        "x-internal": True,
    },
)
async def provider_webhook(
    request: Request,
    idempotency_key: str = Depends(require_idempotency_key),
    signature: str | None = Security(provider_signature_security),
    ingress: WebhookIngress = Depends(get_webhook_ingress),
) -> Envelope:
    # 先取原始字节（验签必须在解析前），再把阻塞的对账工作交给线程池
    raw_body = await request.body()
    provider_name = str(request.path_params.get("provider_name", ""))
    if not provider_name.strip():
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["provider_name"]})

    # 事件身份与时间戳只能来自传输层：路由**先不解析载荷**就取不到它们。
    # 冻结基线的 webhook operation 没有声明任何额外参数，所以这两个头按未声明的
    # 传输细节读取，且认证器会要求它们与**已签名载荷**里的同名字段完全一致，
    # 未信任值因此无法伪造重放键（第 4 节第 2 条）。真实头名在 ENG-07 选定供应商后确定。
    outcome = await run_in_threadpool(
        ingress.ingest,
        provider_name=provider_name,
        raw_body=raw_body,
        signature=signature or "",
        event_id=request.headers.get(EVENT_ID_HEADER_NAME, ""),
        timestamp=request.headers.get(TIMESTAMP_HEADER_NAME, ""),
        idempotency_key=idempotency_key,
    )
    return Envelope(
        data={"accepted": True, "outcome": outcome},
        request_id=request_id_of_request(request),
    )
