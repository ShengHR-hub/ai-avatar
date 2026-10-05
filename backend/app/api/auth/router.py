"""认证路由（ENG-03 第 3 步）。

契约：`03-api-contract.md` 第 3.1 节；形状对齐 `shared/api-contract/openapi.json`。

| 方法 | 路径 | operationId | 权限 | 成功 | 幂等 |
|---|---|---|---|---|---|
| `POST` | `/api/v1/auth/invites/redeem` | `redeemInvite` | 匿名 | `201` | 必须 |
| `POST` | `/api/v1/auth/email/request` | `requestEmailVerification` | 已登录 | `202` | 必须 |
| `POST` | `/api/v1/auth/email/verify` | `verifyEmail` | 令牌 | `200` | 必须 |
| `GET` | `/api/v1/auth/session` | `getSession` | 已登录 | `200` | 否 |
| `POST` | `/api/v1/auth/logout` | `logout` | 已登录 | `204` | 必须 |

- 不创建 `/api/v1/auth/me` 别名（契约只冻结 `/auth/session`）；
- 写接口的 `Idempotency-Key` 只做存在性与格式校验（不实现 Redis 幂等记录与回放）；
- 会话 Cookie 为 HttpOnly + Secure + SameSite=Lax，仅在邀请兑换时写入。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import (
    IDEMPOTENCY_KEY_PARAMETER,
    CurrentSession,
    IdempotencyKey,
    clear_session_cookie,
    get_current_session,
    get_current_user,
    get_email_binding_service,
    get_redeem_service,
    get_session_store,
    get_uow,
    request_id_of_request,
    require_idempotency_key,
    set_session_cookie,
)
from app.schemas.auth import (
    EmailRequestRequest,
    EmailVerifyRequest,
    InviteRedeemRequest,
    InviteRedeemedData,
)
from app.schemas.common import Envelope, ErrorResponse
from app.schemas.user import SessionEnvelope, UserEnvelope
from app.services.auth.email_binding import EmailBindingService
from app.services.auth.redeem import RedeemService
from app.services.auth.session_store import RedisSessionStore
from app.repositories.unit_of_work import UnitOfWork

__all__ = ["auth_router"]

#: 与冻结基线一致的响应描述
ERROR_RESPONSE_DESCRIPTION = "Stable API error"
ACCEPTED_RESPONSE_DESCRIPTION = "Accepted"
USER_RESPONSE_DESCRIPTION = "User"
SESSION_RESPONSE_DESCRIPTION = "Session"
INVITE_REDEEMED_RESPONSE_DESCRIPTION = "User, session and credited points"
LOGOUT_RESPONSE_DESCRIPTION = "Session revoked"

_SET_COOKIE_HEADER = {
    "Set-Cookie": {"schema": {"type": "string"}},
    "description": "Session Cookie（HttpOnly/Secure/SameSite=Lax）",
}

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post(
    "/invites/redeem",
    response_model=Envelope,
    status_code=201,
    operation_id="redeemInvite",
    summary="",
    responses={
        201: {
            "description": INVITE_REDEEMED_RESPONSE_DESCRIPTION,
            "headers": _SET_COOKIE_HEADER,
        },
        400: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        409: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        422: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"security": [], "x-idempotent": True},
)
def redeem_invite(
    request: Request,
    response: Response,
    payload: InviteRedeemRequest,
    idempotency_key: IdempotencyKey,
    service: RedeemService = Depends(get_redeem_service),
) -> Envelope:
    # 兑换邀请码：创建/恢复用户、写入兑换记录与 `CREDIT` 流水、签发会话。
    # 原子操作整体委托给 Points 端口（ENG-04）；未接入时返回 `NOT_READY`（503）。
    # 说明：端点不写 docstring——FastAPI 会把 docstring 作为 operation description 输出，
    # 而冻结基线的 operation 只有 operationId/tags/security/x-idempotent/parameters/
    # requestBody/responses，不含 description。
    del idempotency_key  # 本卡只校验存在性与格式，不做回放
    outcome = service.redeem(invite_code=payload.invite_code, email=str(payload.email))
    set_session_cookie(
        response, outcome.session_token, max_age=service.session_ttl_seconds
    )
    return Envelope(
        data=InviteRedeemedData(
            user=outcome.user, session=outcome.session, points=outcome.points
        ),
        request_id=request_id_of_request(request),
    )


@auth_router.post(
    "/email/request",
    response_model=Envelope,
    status_code=202,
    operation_id="requestEmailVerification",
    summary="",
    responses={
        202: {"description": ACCEPTED_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        403: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        422: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"x-idempotent": True},
)
def request_email_verification(
    request: Request,
    payload: EmailRequestRequest,
    idempotency_key: IdempotencyKey,
    user=Depends(get_current_user),
    service: EmailBindingService = Depends(get_email_binding_service),
) -> Envelope:
    # 签发邮箱验证令牌并投递邮件；**统一 202 防枚举**（端点不写 docstring，原因见 redeem_invite）。
    del idempotency_key
    service.request_verification(user_id=user.id, email=str(payload.email))
    return Envelope(data={}, request_id=request_id_of_request(request))


@auth_router.post(
    "/email/verify",
    response_model=UserEnvelope,
    operation_id="verifyEmail",
    summary="",
    responses={
        200: {"description": USER_RESPONSE_DESCRIPTION},
        409: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
        422: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"security": [], "x-idempotent": True},
)
def verify_email(
    request: Request,
    payload: EmailVerifyRequest,
    idempotency_key: IdempotencyKey,
    service: EmailBindingService = Depends(get_email_binding_service),
) -> UserEnvelope:
    # 消费邮箱验证令牌并绑定邮箱（端点不写 docstring，原因见 redeem_invite）。
    del idempotency_key
    user = service.verify(token=payload.token)
    return UserEnvelope(data=user, request_id=request_id_of_request(request))


@auth_router.get(
    "/session",
    response_model=SessionEnvelope,
    operation_id="getSession",
    summary="",
    responses={
        200: {"description": SESSION_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
)
def get_session(
    request: Request,
    current: CurrentSession = Depends(get_current_session),
    uow: UnitOfWork = Depends(get_uow),
) -> SessionEnvelope:
    # 读取当前会话（不续期；滑动续期属未冻结项）。端点不写 docstring，原因见 redeem_invite。
    #
    # `email_verified` 以**数据库事实**为准，不读会话快照：
    # `SessionRecord.email_verified` 只在 `RedisSessionStore.create()`（邀请兑换）时写入，
    # 而 `POST /auth/email/verify` 只改 `user_emails`、**不续签会话**。若这里回放快照，
    # 「先兑换、再点验证链接」的用户会拿到 `email_verified=false`，前端 `useSession`
    # 据此锁死上传与提交入口，而服务端授权读的是数据库 —— 表现为「接口能过、界面锁死」。
    #
    # 会话身份**仍然完全来自令牌**：这里只用 `current.record.user_id` 去读该用户自己的
    # 验证事实，既不凭令牌为别的用户取事实，也不重签 Cookie、不延长 TTL、
    # 不复活已撤销会话、不改动任何 Cookie 安全属性。
    verified = uow.emails.find_verified_for_user(current.record.user_id) is not None
    return SessionEnvelope(
        data=current.record.to_api_model(email_verified=verified),
        request_id=request_id_of_request(request),
    )


@auth_router.post(
    "/logout",
    status_code=204,
    operation_id="logout",
    summary="",
    responses={
        204: {"description": LOGOUT_RESPONSE_DESCRIPTION},
        401: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    # 幂等参数用 `openapi_extra` 声明 + 手动校验：基线只有 204/401，而声明为真实参数会让
    # FastAPI 自动补 422（详见 `app.api.deps.IDEMPOTENCY_KEY_PARAMETER` 的说明）。
    openapi_extra={
        "parameters": [IDEMPOTENCY_KEY_PARAMETER],
        "x-idempotent": True,
    },
)
def logout(
    request: Request,
    current: CurrentSession = Depends(get_current_session),
    store: RedisSessionStore = Depends(get_session_store),
) -> Response:
    # 撤销当前会话并清除 Cookie（幂等）。端点不写 docstring，原因见 redeem_invite。
    require_idempotency_key(request)
    store.revoke(current.token)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response
