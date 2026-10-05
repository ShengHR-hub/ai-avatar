"""API 依赖（ENG-03 第 3 步）。

集中提供：请求级 `UnitOfWork`、Redis 端口、会话/邮箱令牌存储、当前会话与当前用户、
以及**幂等 Header 参数**。

幂等 Header 的设计（技术负责人 2026-09-26 裁决 ④）：

- 以 `Header(alias="Idempotency-Key")` + `StringConstraints(min_length=1, max_length=128)`
  声明，使 OpenAPI 与基线 `components.parameters.IdempotencyKey` 一致；
- 空白、非 ASCII 值由 `AfterValidator` 拒绝，**错误由
  `app.errors.handlers` 统一映射为 400**（`IDEMPOTENCY_REQUIRED` / `IDEMPOTENCY_INVALID`），
  而不是 422。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Request, Security
from fastapi.security import APIKeyCookie
from pydantic import AfterValidator, StringConstraints

from app.config import get_settings
from app.db.models.identity import User
from app.db.session import get_session_factory
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories import UnitOfWork
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.services.assets.service import AssetService
from app.services.auth.email_binding import EmailBindingService
from app.services.auth.email_tokens import EmailTokenStore
from app.services.auth.profile import ProfileService
from app.services.auth.redeem import RedeemService
from app.services.auth.session_store import RedisSessionStore, SessionRecord
from app.services.ports import (
    EmailPort,
    PointsPort,
    RedisPort,
    get_email_port,
    get_points_port,
    get_redis_port,
)
from app.storage import StorageAdapter, build_storage_adapter, storage_provider_label

__all__ = [
    "IDEMPOTENCY_KEY_HEADER_NAME",
    "IDEMPOTENCY_KEY_PARAMETER",
    "SESSION_COOKIE_NAME",
    "CurrentSession",
    "IdempotencyKey",
    "clear_session_cookie",
    "get_analysis_guard",
    "get_analysis_service",
    "get_asset_service",
    "get_current_session",
    "get_current_user",
    "get_email_binding_service",
    "get_email_token_store",
    "get_feedback_service",
    "get_idempotency_key",
    "get_image_analyzer",
    "get_media_lifecycle_service",
    "get_points_port",
    "get_profile_service",
    "get_redeem_service",
    "get_redis_port",
    "get_session_store",
    "get_share_service",
    "get_event_dedupe_client",
    "get_storage_adapter",
    "get_uow",
    "get_verified_current_user",
    "require_idempotency_key",
    "reset_image_analyzer",
    "reset_storage_adapter",
    "session_cookie_security",
    "set_session_cookie",
]

#: Cookie 名固定为 `session`（`03-api-contract.md` 第 3 节）
SESSION_COOKIE_NAME = "session"

#: 幂等 Header 名（契约第 4 节）
IDEMPOTENCY_KEY_HEADER_NAME = "Idempotency-Key"

#: 会话 Cookie 的 security scheme；`scheme_name` 必须与基线一致
session_cookie_security = APIKeyCookie(
    name=SESSION_COOKIE_NAME,
    scheme_name="sessionCookie",
    description="HttpOnly Session Cookie",
    auto_error=False,
)


def _validate_idempotency_value(value: str) -> str:
    """拒绝空白与非 ASCII 值（契约第 4 节：空白或超长值返回 `IDEMPOTENCY_INVALID`）。"""

    if not value.strip():
        raise ValueError("Idempotency-Key 不能为空白。")
    if not value.isascii():
        raise ValueError("Idempotency-Key 必须是 ASCII 字符。")
    if any(character < " " for character in value):
        raise ValueError("Idempotency-Key 不能包含控制字符。")
    return value


#: 写接口的幂等 Header 参数；长度约束与基线 `components.parameters.IdempotencyKey` 一致
IdempotencyKey = Annotated[
    str,
    Header(alias=IDEMPOTENCY_KEY_HEADER_NAME),
    StringConstraints(min_length=1, max_length=128),
    AfterValidator(_validate_idempotency_value),
]


def get_idempotency_key(value: IdempotencyKey) -> str:
    """返回校验通过的幂等键。

    本卡**只做存在性与格式校验**，不实现 Redis 幂等记录、TTL、fingerprint 或请求回放。
    """

    return value


#: 幂等 Header 的 OpenAPI 参数对象（与基线 `components.parameters.IdempotencyKey` 一致）。
#: 供**无法声明为真实参数**的路由使用：`POST /auth/logout` 的基线响应只有 `204`/`401`，
#: 而 FastAPI 只要存在受校验参数就会自动补一个 `422`，且不允许用 `responses={422: None}` 抑制
#: （实测 `AssertionError: An additional response must be a dict`）。因此该路由改用
#: `openapi_extra` 声明参数 + 手动校验，保持与基线一致的状态码集合。
IDEMPOTENCY_KEY_PARAMETER: dict[str, object] = {
    "name": IDEMPOTENCY_KEY_HEADER_NAME,
    "in": "header",
    "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}


def require_idempotency_key(request: Request) -> str:
    """手动校验幂等 Header（用于 `openapi_extra` 声明的路由）。

    缺少 → `IDEMPOTENCY_REQUIRED`（400）；空白/超长/非 ASCII → `IDEMPOTENCY_INVALID`（400）。
    """

    raw = request.headers.get(IDEMPOTENCY_KEY_HEADER_NAME)
    if raw is None:
        raise ApiError(ErrorCode.IDEMPOTENCY_REQUIRED)
    if not 1 <= len(raw) <= 128:
        raise ApiError(ErrorCode.IDEMPOTENCY_INVALID)
    try:
        return _validate_idempotency_value(raw)
    except ValueError as exc:
        raise ApiError(ErrorCode.IDEMPOTENCY_INVALID) from exc


@dataclass(frozen=True, slots=True)
class CurrentSession:
    """当前请求的会话：令牌原文（仅用于撤销）与服务端记录。"""

    token: str
    record: SessionRecord


def get_uow() -> Iterator[UnitOfWork]:
    """请求级工作单元；退出时未提交的事务自动回滚。"""

    with SqlAlchemyUnitOfWork(get_session_factory()) as uow:
        yield uow


def get_session_store(redis: RedisPort = Depends(get_redis_port)) -> RedisSessionStore:
    return RedisSessionStore(redis, secret=get_settings().session_secret)


def get_email_token_store(redis: RedisPort = Depends(get_redis_port)) -> EmailTokenStore:
    return EmailTokenStore(redis, secret=get_settings().session_secret)


def get_current_session(
    session_token: str | None = Security(session_cookie_security),
    store: RedisSessionStore = Depends(get_session_store),
) -> CurrentSession:
    """读取并校验当前会话。

    - 无 Cookie → `AUTH_REQUIRED`（401）
    - Cookie 存在但会话不存在/已过期/已撤销 → `SESSION_INVALID`（401）
    """

    if not session_token:
        raise ApiError(ErrorCode.AUTH_REQUIRED)
    record = store.read(session_token)
    if record is None:
        raise ApiError(ErrorCode.SESSION_INVALID)
    return CurrentSession(token=session_token, record=record)


def get_current_user(
    current: CurrentSession = Depends(get_current_session),
    uow: UnitOfWork = Depends(get_uow),
) -> User:
    """加载当前会话对应的用户并校验账号状态。"""

    user = uow.users.get(current.record.user_id)
    if user is None or user.status == "DELETED":
        raise ApiError(ErrorCode.SESSION_INVALID)
    if user.status == "SUSPENDED":
        raise ApiError(ErrorCode.ACCOUNT_SUSPENDED)
    return user


def get_points_port_dep() -> PointsPort:
    return get_points_port()


def get_email_port_dep() -> EmailPort:
    return get_email_port()


def get_redeem_service(points: PointsPort = Depends(get_points_port_dep)) -> RedeemService:
    return RedeemService(points_port=points)


def get_email_binding_service(
    uow: UnitOfWork = Depends(get_uow),
    tokens: EmailTokenStore = Depends(get_email_token_store),
    email_port: EmailPort = Depends(get_email_port_dep),
) -> EmailBindingService:
    return EmailBindingService(uow=uow, tokens=tokens, email_port=email_port)


def get_profile_service(uow: UnitOfWork = Depends(get_uow)) -> ProfileService:
    return ProfileService(uow=uow)


# --------------------------------------------------------------------------- #
# 媒体资源（ENG-05）
# --------------------------------------------------------------------------- #
_storage_adapter: StorageAdapter | None = None


def get_storage_adapter() -> StorageAdapter:
    """返回进程级 `StorageAdapter`；按服务端配置装配，构造不建立网络连接。

    `Minio(...)` 只创建客户端对象，首次命令才真正访问对象存储
    （与 `RedisClientAdapter` 同一口径）。测试通过依赖覆盖注入内存驱动。
    """

    global _storage_adapter
    if _storage_adapter is None:
        settings = get_settings()
        _storage_adapter = build_storage_adapter(
            driver=settings.storage_driver,
            endpoint=settings.storage_endpoint,
            bucket=settings.storage_bucket,
            region=settings.storage_region,
            access_key=settings.storage_access_key,
            secret_key=settings.storage_secret_key,
        )
    return _storage_adapter


def reset_storage_adapter() -> None:
    """清空驱动缓存，供测试替换实现。"""

    global _storage_adapter
    _storage_adapter = None


def get_asset_service(
    uow: UnitOfWork = Depends(get_uow),
    storage: StorageAdapter = Depends(get_storage_adapter),
) -> AssetService:
    settings = get_settings()
    return AssetService(
        uow=uow,
        storage=storage,
        storage_provider=storage_provider_label(settings.storage_driver),
        enable_reference_image=settings.enable_reference_image,
        # 分析能力只需要「图存在我自己的 REFERENCE 资产里」，因此它也放行 REFERENCE 上传；
        # 生成侧的 mode=REFERENCE_IMAGE 仍然只看 enable_reference_image。
        enable_image_analysis=settings.enable_image_analysis,
        # 非所有者读取要**实时**核对有效分享（不靠可见性快照），见 AssetService 说明
        share_lookup=_asset_share_lookup,
    )


def _asset_share_lookup(asset_id: uuid.UUID) -> bool:
    """匿名/第三方读取时的实时分享依据查询。

    每次调用自己开一个**独立**工作单元：它只读 `shares` / `share_assets`，
    不与请求级事务共享 Session，因此不会把分享表读进请求事务的快照，
    也不会因为 AssetService 与 ShareService 各自的事务边界而互相干扰。
    未接线时 `AssetService` 会拒绝 `SHARED`（fail-closed）。
    """

    from app.services.shares.service import ShareService

    with SqlAlchemyUnitOfWork(get_session_factory()) as uow:
        return ShareService(uow=uow).asset_has_valid_share(asset_id)


# --------------------------------------------------------------------------- #
# 参考图分析（「参考图片找灵感」）
# --------------------------------------------------------------------------- #
_image_analyzer: Any = None


def get_image_analyzer() -> Any:
    """进程级 `ImageAnalyzer`；按服务端配置装配，构造不建立网络连接。

    与出图 Provider 分开装配、分开配置型号（`AI_VISION_MODEL`）。真实供应商缺配置时
    工厂直接 `RuntimeError`，**绝不静默退回 Mock**——那会让一段没读图的假描述被当成
    用户参考图的分析结果送到界面上。
    """

    global _image_analyzer
    if _image_analyzer is None:
        from app.services.analysis.analyzer_factory import build_image_analyzer

        settings = get_settings()
        _image_analyzer = build_image_analyzer(
            ai_provider=settings.ai_provider,
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            vision_model=settings.ai_vision_model,
        )
    return _image_analyzer


def reset_image_analyzer() -> None:
    """清空分析适配器缓存，供测试替换实现。"""

    global _image_analyzer
    _image_analyzer = None


def get_analysis_guard() -> Any:
    """付费闸门：Redis 缓存回放 + 单飞锁，PostgreSQL 判每日配额。

    Redis 沿用 `get_event_dedupe_client()` 那条**独立**连接：需要的原子
    `SET ... NX EX` 不在冻结 `RedisPort` 的表面上，去重和抢锁都绝不能写成
    「先 GET 再 SET」。同一台 Redis 上再开一个连接池不会换来任何隔离性，
    所以沿用 `RedisReplayGuard` 已经确立的做法。

    但额度计数不再交给 Redis：缓存重启或被清空后计数归零，而钱已经花出去了。
    配额判定改为读 `analysis_dispatches`（`PostgresAnalysisQuota`），Redis 侧的两个
    限额因此传 `None`，只保留加速能力。持久层不可用时一律 fail-closed 到 `NOT_READY`。

    守卫实例**按请求构造**：它记住的是本次请求占到的那次锁与那一行登记，竞态失败的
    一方不得抹掉赢家的占用。
    """

    from app.services.analysis.guard import RedisAnalysisGuard
    from app.services.analysis.quota import DurableAnalysisGuard, PostgresAnalysisQuota
    from app.services.generation.provider_factory import provider_identity

    settings = get_settings()
    provider_name, model_name = provider_identity(
        ai_provider=settings.ai_provider, ai_model=settings.ai_vision_model
    )
    return DurableAnalysisGuard(
        redis_guard=RedisAnalysisGuard(
            get_event_dedupe_client(),
            per_user_daily_limit=None,
            global_daily_limit=None,
        ),
        quota=PostgresAnalysisQuota(
            get_session_factory(),
            per_user_daily_limit=settings.analysis_daily_limit_per_user,
            global_daily_limit=settings.analysis_global_daily_limit,
        ),
        provider_name=provider_name,
        model_name=model_name,
    )


def get_analysis_service(
    uow: UnitOfWork = Depends(get_uow),
    storage: StorageAdapter = Depends(get_storage_adapter),
    analyzer: Any = Depends(get_image_analyzer),
    guard: Any = Depends(get_analysis_guard),
) -> Any:
    """请求级 Analysis Service（不创建任务、不占点数、不进状态机）。"""

    from app.services.analysis.service import AnalysisService

    return AnalysisService(
        uow=uow,
        storage=storage,
        analyzer=analyzer,
        guard=guard,
        enabled=get_settings().enable_image_analysis,
    )


def get_verified_current_user(
    user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> User:
    """已登录且邮箱已验证（契约第 4.2 节与第 8 节矩阵）。

    `AssetService` 在上传意图入口也会独立校验：该 Service 同时是后续 Worker 的领域入口，
    不能依赖 HTTP 层替它把关。两处共用同一条 `user_emails` 已验证查询。
    """

    if uow.emails.find_verified_for_user(user.id) is None:
        raise ApiError(ErrorCode.EMAIL_NOT_VERIFIED)
    return user


# --------------------------------------------------------------------------- #
# 分享与反馈（ENG-09）
# --------------------------------------------------------------------------- #
def get_share_service(uow: UnitOfWork = Depends(get_uow)) -> Any:
    """请求级 Share Service（与请求同一个工作单元，事务由 Service 提交）。"""

    from app.services.shares.service import ShareService

    return ShareService(uow=uow)


def get_feedback_service(uow: UnitOfWork = Depends(get_uow)) -> Any:
    """请求级 Feedback Service。"""

    from app.services.feedback.service import FeedbackService

    return FeedbackService(uow=uow)


def get_media_lifecycle_service(
    storage: StorageAdapter = Depends(get_storage_adapter),
) -> Any:
    """生命周期服务装配点。

    传**工厂**而不是现成的工作单元：清理任务一次要处理多批，必须每批开一个
    独立事务并在结束时关闭 Session（见 `MediaLifecycleService` 的模块说明）。
    """

    from app.services.shares.lifecycle import MediaLifecycleService

    return MediaLifecycleService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(get_session_factory()),
        storage=storage,
    )


def set_session_cookie(
    response, token: str, *, max_age: int, secure: bool = True
) -> None:
    """写入会话 Cookie：HttpOnly + Secure + SameSite=Lax（契约第 3 节）。"""

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    """清除会话 Cookie（登出）。"""

    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )


def request_id_of_request(request: Request) -> str:
    """便捷包装，避免路由重复导入中间件模块。"""

    from app.middleware.request_id import request_id_of

    return request_id_of(request)


# --------------------------------------------------------------------------- #
# ENG-06：Generation Service / Orchestrator / Webhook 装配
# --------------------------------------------------------------------------- #
_generation_queue: Any = None
_event_dedupe_client: Any = None


def get_generation_queue() -> Any:
    """进程级生成队列端口（Celery producer）。

    只按任务名投递，因此 Web 进程不会加载 Worker 侧执行栈；构造本身不连接 Broker，
    首次 `send_task` 才建立连接。测试通过依赖覆盖注入内存替身。
    """

    global _generation_queue
    if _generation_queue is None:
        from app.tasks.queue import CeleryGenerationQueue

        _generation_queue = CeleryGenerationQueue()
    return _generation_queue


def get_points_service(uow: UnitOfWork = Depends(get_uow)) -> Any:
    """请求级 Points Service（与 Generation Service 共用同一个工作单元）。"""

    from app.services.points.service import PointsService

    return PointsService(uow=uow)


def get_generation_service(
    uow: UnitOfWork = Depends(get_uow),
    points: Any = Depends(get_points_service),
) -> Any:
    """请求级 Generation Service。"""

    from app.services.generation.provider_factory import provider_identity
    from app.services.generation.service import GenerationService

    settings = get_settings()
    # 任务行登记的 provider/model 必须与实际出网的适配器同源：两处不一致会让
    # `provider_attempts` 与事后关联全部指向错误的供应商。
    provider_name, model_name = provider_identity(
        ai_provider=settings.ai_provider, ai_model=settings.ai_model
    )
    return GenerationService(
        uow=uow,
        points=points,
        queue=get_generation_queue(),
        enable_reference_image=settings.enable_reference_image,
        provider_name=provider_name,
        model_name=model_name,
    )


def get_webhook_authenticator() -> Any:
    """Provider Webhook 验签器。

    V1.0 只允许 `MOCK` 通道；真实签名头/算法/规范化在选定供应商后（ENG-07）验证。
    密钥缺省即全部拒绝：绝不因为「像是本地测试」而放行未验签的请求。
    """

    from app.services.generation.webhook_adapters import MockHmacWebhookAuthenticator

    settings = get_settings()
    normalized = (settings.ai_provider or "").strip().upper()
    if normalized != "MOCK":
        # 已复核的真实供应商不提供 Webhook：能打到这里的事件只可能是伪造。
        # 也绝不拿 AI_API_KEY 去验一个不存在的签名通路——那是把凭据当两种用途用。
        raise ApiError(ErrorCode.WEBHOOK_INVALID)
    return MockHmacWebhookAuthenticator(
        secret=settings.ai_api_key, provider_name=normalized or "MOCK"
    )


def get_webhook_replay_guard() -> Any:
    """短期重放去重存储；权威判定仍是数据库的 attempt 行锁与终态检查。

    守卫实例**按请求构造**：它记住的是「本次请求占到的那次占用」，只有自己能确认或
    释放，竞态失败的一方不得抹掉赢家的占用。
    """

    from app.services.generation.webhook_adapters import RedisReplayGuard

    return RedisReplayGuard(get_event_dedupe_client())


def get_event_dedupe_client() -> Any:
    """事件 ID 去重用的**独立** Redis 连接（进程级，惰性连接）。

    原子 `SET key value NX EX` 不在冻结 `RedisPort` 的表面上，而去重又不能写成
    「先 `GET` 再 `SET`」——两个 Worker 同时读空会双双放行。因此这里用一条独立客户端，
    `RedisPort` 保持原样。与 `RedisClientAdapter` 同口径：`from_url` 只建连接池，
    首次命令才真正连接，因此装配本身不触网。
    """

    global _event_dedupe_client
    if _event_dedupe_client is None:
        import redis

        _event_dedupe_client = redis.Redis.from_url(
            get_settings().redis_url, decode_responses=True
        )
    return _event_dedupe_client


def get_webhook_ingress(
    authenticator: Any = Depends(get_webhook_authenticator),
    replay_guard: Any = Depends(get_webhook_replay_guard),
) -> Any:
    """Provider 回调入口：验签 -> 防重放 -> 归一到统一对账路径。"""

    from app.services.generation.webhook import WebhookIngress

    return WebhookIngress(
        authenticator=authenticator,
        replay_guard=replay_guard,
        orchestrator=build_generation_orchestrator(),
    )


def build_generation_orchestrator() -> Any:
    """Worker 与 Webhook 共用的编排器装配点（同一套事务、Provider 与存储）。"""

    from app.tasks.worker import build_orchestrator

    return build_orchestrator()


def reset_generation_singletons() -> None:
    """清空进程级缓存，供测试替换实现。"""

    global _event_dedupe_client, _generation_queue
    _generation_queue = None
    _event_dedupe_client = None
