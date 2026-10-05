"""Asset Service（ENG-05 第 2-4 步）。

职责：上传意图、完成校验、读取、短期签名与逻辑删除的全部业务规则与事务边界。

依据：

- `03-api-contract.md` 第 4.2 节：上传意图 → 浏览器直传短期 URL → 完成校验；
  未完成校验的资源不得作为生成输入；
- `04-ai-provider.md` 第 7、8 节：驱动边界、签名时长上限、图片校验与 visibility 授权；
- `02-database-design.md` 第 3.3、4 节：媒体元数据不物理删除，二进制清理不级联业务历史；
- `06-development-standard.md` 第 2 节：Repository 不 `commit`，事务边界由本 Service 持有。

纪律：

- 只通过 `StorageAdapter` 协议访问对象存储，不导入任何 SDK；
- 对象存储依赖故障统一映射既有 `NOT_READY`（503），不新增错误码，
  也不把「依赖不可用」误判成「对象不存在」或「校验失败」；
- 越权与不可见一律 `ASSET_NOT_FOUND`（404），不泄露他人资源是否存在；
- 服务端事实（`storage_key`、`storage_provider`、`idempotency_key`）不进入响应。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from app.db.models.media import MediaAsset
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories import MediaAssetDraft, UnitOfWork
from app.schemas.asset import UploadIntentRequest
from app.services.assets.policy import (
    DOWNLOAD_URL_TTL,
    ORIGINAL_TTL,
    PURPOSE_TO_ASSET_TYPE,
    RESULT_TTL,
    UPLOAD_URL_TTL,
)
from app.services.assets.validation import (
    ALLOWED_MIME_TYPES,
    MAX_IMAGE_BYTES,
    validate_image,
)
from app.storage import (
    ObjectNotFound,
    SignedUrl,
    StorageAdapter,
    StorageUnavailable,
)

__all__ = ["AssetService", "ShareLookup", "UploadIntentOutcome"]

_T = TypeVar("_T")

#: 非所有者读取时的**实时**分享依据查询：给定资产 ID，返回此刻是否存在
#: 一条有效（`ACTIVE` 且未过期）且选中该资产的分享。
#: 装配点见 `app/api/deps.py::_asset_share_lookup`（独立工作单元，不共享请求事务）。
ShareLookup = Callable[[uuid.UUID], bool]

#: 可完成上传的状态（`DELETED`/`EXPIRED` 已在前面单独处理）
_PENDING = "PENDING_UPLOAD"
_READY = "READY"
_DELETED = "DELETED"
_EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class UploadIntentOutcome:
    """上传意图结果：资源行 + 浏览器直传所需的短期 PUT URL。"""

    asset: MediaAsset
    upload_url: str
    method: str
    expires_at: datetime


def _new_storage_key(*, user_id: uuid.UUID) -> str:
    """服务端生成的不透明对象 Key（绝不采用用户提供的路径或文件名）。"""

    return f"assets/{user_id}/{uuid.uuid4().hex}"


class AssetService:
    """媒体资源生命周期编排。"""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        storage: StorageAdapter,
        storage_provider: str,
        enable_reference_image: bool,
        enable_image_analysis: bool = False,
        clock: Callable[[], datetime] | None = None,
        share_lookup: ShareLookup | None = None,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._storage_provider = storage_provider
        self._enable_reference_image = enable_reference_image
        self._enable_image_analysis = enable_image_analysis
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))
        #: 非所有者读取的**实时**分享依据查询。装配点见 `app/api/deps.py`：
        #: 它自己开一个独立工作单元，因此不会把分享表读进请求级事务缓存，
        #: 也不会让 AssetService 依赖分享聚合的实现细节。
        self._share_lookup = share_lookup

    # ---------------------------------------------------------------- 上传意图
    def create_upload_intent(
        self, *, user_id: uuid.UUID, intent: UploadIntentRequest, idempotency_key: str
    ) -> UploadIntentOutcome:
        """建立 `PENDING_UPLOAD` 资源并签发短期上传 URL。

        参考图在写入任何数据**之前**按 Feature Flag 拒绝（`LEAD-05`：不得静默忽略）。

        `purpose=REFERENCE` 由两个开关里**任一个**放行，因为这两个能力都只需要
        「图存在我自己的 REFERENCE 资产里」，而它们的开放状态是分别裁定的：

        - `enable_reference_image`：把参考图当**生成条件**的 `REFERENCE_IMAGE` 模式
          （上游能力未证实，默认继续关闭）；
        - `enable_image_analysis`：读一段**画面描述**找灵感（只读图，不送进生成）。

        合并这两个开关放行的只是**上传**；生成侧的 `mode=REFERENCE_IMAGE` 仍然只看
        `enable_reference_image`（`services/generation/service.py`），打开分析能力
        绝不会顺带放开那条未证实的生成路径。
        """

        purpose = intent.purpose.value
        if purpose not in PURPOSE_TO_ASSET_TYPE:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["purpose"]})
        if purpose == "REFERENCE" and not (
            self._enable_reference_image or self._enable_image_analysis
        ):
            raise ApiError(ErrorCode.FEATURE_NOT_AVAILABLE)

        # 基线 createAssetUpload 声明了 413/415：意图阶段就按声明值拒绝，
        # 避免给浏览器签发一个注定通不过完成校验的上传 URL。
        if intent.mime_type not in ALLOWED_MIME_TYPES:
            raise ApiError(ErrorCode.MEDIA_TYPE_UNSUPPORTED)
        if intent.size_bytes > MAX_IMAGE_BYTES:
            raise ApiError(ErrorCode.UPLOAD_TOO_LARGE)

        self._require_verified_email(user_id)

        existing = self._uow.media.find_by_idempotency_key(
            user_id=user_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return self._replay_intent(existing, intent=intent)

        asset = self._uow.media.create(
            MediaAssetDraft(
                user_id=user_id,
                asset_type=PURPOSE_TO_ASSET_TYPE[purpose],
                purpose=purpose,
                storage_provider=self._storage_provider,
                storage_key=_new_storage_key(user_id=user_id),
                mime_type=intent.mime_type,
                size_bytes=intent.size_bytes,
                sha256=intent.sha256.lower(),
                idempotency_key=idempotency_key,
                expires_at=self._clock() + self._lifetime_for_purpose(purpose),
            )
        )
        self._uow.commit()

        return self._intent_outcome(asset)

    def _replay_intent(
        self, asset: MediaAsset, *, intent: UploadIntentRequest
    ) -> UploadIntentOutcome:
        """同键同参数回放第一次创建的**资源行**；签名 URL 必须重新签发。

        `03-api-contract.md` 第 9 节要求原样回放第一次 body，但短期 URL 本身会过期，
        逐字回放只会交付一个失效链接。因此资源事实严格回放（不新建行、不改列、
        不重复发放），只有 `upload_url`/`expires_at` 按同一签发规则重新生成。
        该偏差已在停线报告中登记，等待总控确认。
        """

        same_request = (
            asset.purpose == intent.purpose.value
            and asset.mime_type == intent.mime_type
            and asset.size_bytes == intent.size_bytes
            and asset.sha256 == intent.sha256.lower()
        )
        if not same_request:
            raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)

        return self._intent_outcome(asset)

    def _intent_outcome(self, asset: MediaAsset) -> UploadIntentOutcome:
        signed: SignedUrl = self._guard(lambda: self._storage.create_signed_url(
            asset.storage_key, "PUT", UPLOAD_URL_TTL, asset.mime_type
        ))
        return UploadIntentOutcome(
            asset=asset,
            upload_url=signed.url,
            method="PUT",
            expires_at=signed.expires_at,
        )

    # ---------------------------------------------------------------- 完成上传
    def complete_upload(self, *, user_id: uuid.UUID, asset_id: uuid.UUID) -> MediaAsset:
        """校验对象并按**观测值**置 `READY`。

        幂等：已 `READY` 的资源不再读对象，直接返回既有事实。
        """

        asset = self._lock_owned_asset(user_id=user_id, asset_id=asset_id)

        if asset.status == _READY:
            return asset
        if asset.status in {_DELETED, _EXPIRED} or effective_expired(asset, now=self._clock()):
            # 生命周期已结束：不可能再被完成，按不可见处理，不泄露存在性
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        if asset.status != _PENDING:
            raise ApiError(ErrorCode.ASSET_INVALID)

        if not self._guard(lambda: self._storage.exists(asset.storage_key)):
            # 对象尚未上传：契约第 7 节的 ASSET_NOT_READY（409），可安全重试
            raise ApiError(ErrorCode.ASSET_NOT_READY)

        def load() -> bytes:
            try:
                content, _metadata = self._storage.read(asset.storage_key)
            except ObjectNotFound as exc:
                raise ApiError(ErrorCode.ASSET_NOT_READY) from exc
            return content

        content = self._guard(load)

        validated = validate_image(
            content,
            declared_mime_type=asset.mime_type,
            declared_size_bytes=asset.size_bytes,
            declared_sha256=asset.sha256,
        )

        self._uow.media.mark_ready(
            asset,
            mime_type=validated.mime_type,
            size_bytes=validated.size_bytes,
            width=validated.width,
            height=validated.height,
        )
        self._uow.commit()
        return asset

    # ------------------------------------------------------- 读取、签名与删除
    def get_asset(
        self, *, asset_id: uuid.UUID, viewer_user_id: uuid.UUID | None
    ) -> MediaAsset:
        """读取资源元数据；不可见与无权统一 404，不泄露存在性。"""

        asset = self._uow.media.get(asset_id)
        if asset is None or asset.status == _DELETED:
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        self._require_readable(asset, viewer_user_id=viewer_user_id)
        return asset

    def create_download_url(
        self, *, asset_id: uuid.UUID, viewer_user_id: uuid.UUID | None
    ) -> SignedUrl:
        """签发短期下载 URL；未就绪资源返回 409 而不是交付坏链接。"""

        asset = self.get_asset(asset_id=asset_id, viewer_user_id=viewer_user_id)
        if effective_expired(asset, now=self._clock()):
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        if asset.status != _READY:
            raise ApiError(ErrorCode.ASSET_NOT_READY)

        return self._guard(
            lambda: self._storage.create_signed_url(
                asset.storage_key, "GET", DOWNLOAD_URL_TTL
            )
        )

    def delete_asset(self, *, user_id: uuid.UUID, asset_id: uuid.UUID) -> bool:
        """删除对象二进制并逻辑删除元数据。

        幂等：已经 `DELETED` 的行返回 `False`（路由仍回 204），不重复删对象、
        不重复写时间戳。**从未存在的 id 仍返回 404**（基线声明了 404），
        他人的资源返回 403，不借幂等名义掩盖越权或把拼错的 id 假装删除成功。
        """

        asset = self._uow.media.lock_by_id(asset_id)
        if asset is None:
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        if asset.user_id != user_id:
            raise ApiError(ErrorCode.ASSET_FORBIDDEN)
        if asset.status == _DELETED:
            return False

        self._guard(lambda: self._storage.delete(asset.storage_key))
        self._uow.media.mark_deleted(asset, deleted_at=self._clock())
        self._uow.commit()
        return True

    # -------------------------------------------------------------------- 授权
    def _require_readable(
        self, asset: MediaAsset, *, viewer_user_id: uuid.UUID | None
    ) -> None:
        """非所有者读取的规则（`04-ai-provider.md` 第 8 节表格）。

        0. **所有者**：始终可读（既有权限不变）。
        1. **资产自身是否到期**：`expires_at <= now` 一律 404。这一条对**所有**
           非所有者读取生效 —— 到期是「当前事实」，与可见性快照无关，
           因此不依赖清理任务是否已经跑过（清理只负责把状态收敛）。
        2. **`PRIVATE`**：非所有者始终 404。
        3. **`SHARED`**：必须**当下**有一条 `ACTIVE` 且未过期、并把该资产登记为
           选中资产的分享（实时核对，不接受「可见性写着 SHARED」这种快照判断）。
           未接线 `share_lookup` 时一律拒绝（fail-closed：查不到依据就不公开）。
        4. **`PUBLIC`**：保留既有公开读取规则（只读已发布内容），**不**因为注入
           `share_lookup` 而被拒绝 —— 分享链路的接线不得改变 PUBLIC 的授权边界。

        未选中公开的结果保持 `PRIVATE`，匿名与第三方一律 404 ——
        分享一张不会顺带公开同任务的另一张。
        """

        if viewer_user_id is not None and asset.user_id == viewer_user_id:
            return
        if effective_expired(asset, now=self._clock()):
            # 已到期：即使清理任务还没跑、可见性还写着 SHARED，也不放行
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        if asset.visibility == "PUBLIC":
            # PUBLIC 的授权边界由既有规则决定，与分享链路无关
            return
        if asset.visibility != "SHARED":
            # PRIVATE（以及任何未知取值）：非所有者 404
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        if self._share_lookup is not None and self._share_lookup(asset.id):
            return
        raise ApiError(ErrorCode.ASSET_NOT_FOUND)

    def _lock_owned_asset(self, *, user_id: uuid.UUID, asset_id: uuid.UUID) -> MediaAsset:
        """锁定资源行并校验归属（完成上传入口）。"""

        asset = self._uow.media.lock_by_id(asset_id)
        if asset is None:
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        if asset.user_id != user_id:
            raise ApiError(ErrorCode.ASSET_FORBIDDEN)
        return asset

    def _require_verified_email(self, user_id: uuid.UUID) -> None:
        if self._uow.emails.find_verified_for_user(user_id) is None:
            raise ApiError(ErrorCode.EMAIL_NOT_VERIFIED)

    # ---------------------------------------------------------------- 存储边界
    @staticmethod
    def _lifetime_for_purpose(purpose: str) -> timedelta:
        return RESULT_TTL if purpose == "RESULT" else ORIGINAL_TTL

    def _guard(self, action: Callable[[], _T]) -> _T:
        """把对象存储依赖故障映射为既有 `NOT_READY`（503），不新增错误码。"""

        try:
            return action()
        except StorageUnavailable as exc:
            raise ApiError(ErrorCode.NOT_READY) from exc


def effective_expired(asset: MediaAsset, *, now: datetime) -> bool:
    """`READY`/`PENDING_UPLOAD` 行是否已过 `expires_at`（只读判定，不写库）。"""

    if asset.status not in {_PENDING, _READY} or asset.expires_at is None:
        return False
    deadline = asset.expires_at
    moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= moment
