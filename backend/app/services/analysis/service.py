"""参考图分析服务（「参考图片找灵感」的服务端能力）。

**这不是生成**：它不创建任务、不占点数、不进状态机、不产出图片。它只做一件事——
把用户自己上传的一张参考图交给视觉模型，换回一段**近似画面描述**。

边界（逐条对应产品与协议要求）：

- 输入必须是调用者自己的 `purpose=REFERENCE` 且 `status=READY` 的资产；
  与生成链路一样，先判存在再判归属；
- 图片字节只在内存里过一遍，**不落库、不写日志、不进响应**；
- 送模型前先**重编码**为长边 ≤ `MAX_ANALYSIS_SIDE` 的 JPEG：一来 token（也就是钱）与
  像素量近似成正比，二来重编码会连带丢掉 EXIF/ICC 之外的元数据——相机 GPS、设备号、
  作者字段这些和用户身份绑在一起的信息没有任何理由替用户送到中转站去；
- 描述文本**只在 Redis 里活 6 小时**（`guard.py` 的缓存回放），不写数据库、不加迁移；
  图片本身继续走既有 `REFERENCE` 资产的 7 天生命周期；
- 描述里若出现邮箱/手机号/网址/路径/内部参数/成串编号，**整段不下发**：模型的产出
  不受我们控制，宁可让用户换一张图重试，也不把可能属于他人的私人信息透出去；
- 输出是"根据图片生成的描述"，**不是**原作者的原始提示词，也不承诺复刻。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.providers.ports import (
    ImageAnalyzer,
    ImageDescriptionRequest,
    ImageInput,
    ProviderCallError,
    ProviderErrorCategory,
)
from app.repositories import UnitOfWork
from app.services.analysis.guard import AnalysisGuard
from app.services.assets.validation import compute_sha256, validate_image
from app.storage import StorageAdapter, StorageError

__all__ = ["AnalysisOutcome", "AnalysisService", "MAX_ANALYSIS_SIDE"]

_READY_STATUS = "READY"
_REFERENCE_PURPOSE = "REFERENCE"

#: 送进视觉模型前先把长边压到这个值以内：看图不需要原始分辨率，
#: 而 token 数（也就是钱）与像素量近似成正比。
MAX_ANALYSIS_SIDE = 1024

#: 重编码画质：描述只需要看得清主体、画法、配色，不需要原图级细节。
JPEG_QUALITY = 85

#: 一次输出的 token 上限。2026-09-30 真实调用实测：带思考 token 的视觉型号
#: （`gemini-2.5-flash`）会把预算花在思考上，300 时可见正文被截成半句
#: （"……其上方叠放着"），所以这里按"思考 + 一段 60–120 字正文"重新给量。
#: 描述本身仍由 `DESCRIPTION_MAX_CHARS` 截到 600 字，放大这个值不会放大界面长度。
MAX_OUTPUT_TOKENS = 900

_LOGGER = logging.getLogger("ai_avatar.analysis")


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """一次描述结果。

    `from_cache` 必须如实带出去：它意味着这次**没有出网、没有再付费**，
    界面拿它决定要不要告诉用户「这是刚才那次读出来的」。
    """

    asset_id: uuid.UUID
    description: str
    model: str
    from_cache: bool


class AnalysisService:
    """一次同步的"看图写描述"。"""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        storage: StorageAdapter,
        analyzer: ImageAnalyzer,
        guard: AnalysisGuard,
        enabled: bool,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._analyzer = analyzer
        self._guard = guard
        self._enabled = enabled

    def describe_reference_image(
        self, *, user_id: uuid.UUID, asset_id: uuid.UUID
    ) -> AnalysisOutcome:
        # 能力开关最先判：关掉之后连缓存回放也不给读，否则「关闭付费能力」会留下
        # 一条仍能取回旧描述的旁路，界面与运维看到的就不是同一个开关。
        if not self._enabled:
            raise ApiError(ErrorCode.FEATURE_NOT_AVAILABLE)

        self._require_verified_email(user_id)
        asset = self._load_owned_reference(user_id=user_id, asset_id=asset_id)
        content = self._read_bytes(asset.storage_key)

        # 复用生成链路同一条校验：不因为"只是看图"就放松边界。
        # 校验按资产自身的声明值进行——声明与实际不符时在这里就会失败。
        validate_image(
            content,
            declared_mime_type=asset.mime_type,
            declared_size_bytes=asset.size_bytes,
            declared_sha256=asset.sha256,
        )

        # 去重的键是**原始图**的摘要，不是重编码后的字节：同一张照片哪怕被上传成两份资产，
        # 也不该付两次钱；而两张内容不同的图永远不会因为压缩而撞上同一个键。
        cached = self._guard.cached(user_id=user_id, sha256=asset.sha256)
        if cached is not None:
            _LOGGER.info(
                "reference analysis replayed from cache user=%s asset=%s",
                user_id,
                asset_id,
            )
            return AnalysisOutcome(
                asset_id=asset.id,
                description=cached.description,
                model=cached.model,
                from_cache=True,
            )

        payload = _prepared_image(content)
        request = ImageDescriptionRequest(
            image=ImageInput(
                mime_type="image/jpeg",
                content=payload,
                sha256=compute_sha256(payload),
            ),
            language="zh",
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )

        lease = self._guard.begin(user_id=user_id, sha256=asset.sha256)
        try:
            result = self._analyzer.describe_image(request)
        except ProviderCallError as error:
            self._guard.rollback(lease)
            _LOGGER.warning(
                "reference analysis failed user=%s asset=%s category=%s",
                user_id,
                asset_id,
                error.category.value,
            )
            raise _to_api_error(error) from error
        except Exception as error:  # noqa: BLE001 - 非预期异常同样要先放开锁再上抛
            self._guard.rollback(lease)
            raise ApiError(
                ErrorCode.PROVIDER_BAD_GATEWAY,
                details={"reason": type(error).__name__},
            ) from error

        description = _tidy(result.description)
        if not description or _exposes_private_info(description):
            # 供应商确实被打出去了，所以额度不回退；但这段文字到我们为止，不再往下走。
            self._guard.rollback(lease)
            _LOGGER.warning(
                "reference analysis description withheld user=%s asset=%s",
                user_id,
                asset_id,
            )
            raise ApiError(
                ErrorCode.PROVIDER_BAD_GATEWAY, details={"reason": "description_withheld"}
            )

        self._guard.commit(lease, description=description, model=result.model)
        _LOGGER.info(
            "reference analysis succeeded user=%s asset=%s model=%s tokens=%s/%s",
            user_id,
            asset_id,
            result.model,
            _token_count(result.usage.input_tokens if result.usage else None),
            _token_count(result.usage.output_tokens if result.usage else None),
        )
        return AnalysisOutcome(
            asset_id=asset.id,
            description=description,
            model=result.model,
            from_cache=False,
        )

    # ------------------------------------------------------------------ 内部
    def _require_verified_email(self, user_id: uuid.UUID) -> None:
        """看图会产生外部费用，沿用与上传、生成一致的邮箱已验证门禁。"""

        if self._uow.emails.find_verified_for_user(user_id) is None:
            raise ApiError(ErrorCode.EMAIL_NOT_VERIFIED)

    def _load_owned_reference(self, *, user_id: uuid.UUID, asset_id: uuid.UUID):
        asset = self._uow.media.get(asset_id)
        if asset is None:
            raise ApiError(ErrorCode.ASSET_NOT_FOUND)
        if self._uow.media.find_owner(asset_id) != user_id:
            raise ApiError(ErrorCode.ASSET_FORBIDDEN)
        if asset.purpose != _REFERENCE_PURPOSE:
            # 声明与实际用途不一致时必须拒绝：否则一次"看参考图"会悄悄变成看别人的原图
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["asset_id"]})
        if asset.status != _READY_STATUS:
            raise ApiError(ErrorCode.ASSET_NOT_READY)
        return asset

    def _read_bytes(self, storage_key: str) -> bytes:
        try:
            content, _metadata = self._storage.read(storage_key)
        except StorageError as error:
            raise ApiError(ErrorCode.NOT_READY) from error
        if not content:
            raise ApiError(ErrorCode.ASSET_INVALID)
        return content


def _prepared_image(content: bytes) -> bytes:
    """把原图重编码为长边 ≤1024 的 JPEG（顺带丢掉元数据）。

    一律重编码，不做「够小就原样送出」的短路：一张尺寸合规的 JPEG 照样可能带着 GPS、
    相机序列号和作者姓名，而这些正是我们不该替用户送出去的东西。
    """

    try:
        with Image.open(BytesIO(content)) as opened:
            opened.load()
            transposed = ImageOps.exif_transpose(opened)
            image = transposed if transposed is not None else opened
            image.thumbnail((MAX_ANALYSIS_SIDE, MAX_ANALYSIS_SIDE), Image.LANCZOS)
            flat = _flatten_to_rgb(image)
            buffer = BytesIO()
            flat.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            payload = buffer.getvalue()
    except ApiError:
        raise
    except Exception as error:  # noqa: BLE001 - 任何解码/编码失败都只是这张图不能用
        raise ApiError(
            ErrorCode.ASSET_INVALID, details={"reason": "analysis_preparation_failed"}
        ) from error
    if not payload:
        raise ApiError(ErrorCode.ASSET_INVALID, details={"reason": "empty_prepared_image"})
    return payload


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """把带透明通道的图压到白底 RGB——JPEG 不认 alpha，直接 convert 会把透明区变成黑块。"""

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _tidy(text: str) -> str:
    """把模型输出收成一段纯文本：合并换行、去掉包裹引号与多余空白。"""

    collapsed = re.sub(r"\s+", " ", text or "").strip()
    for pair in (('"', '"'), ("“", "”"), ("'", "'")):
        if len(collapsed) > 1 and collapsed.startswith(pair[0]) and collapsed.endswith(pair[1]):
            collapsed = collapsed[1:-1].strip()
    return collapsed


#: 描述里不能出现的私人信息形态。与前端 `services/promptDisclosure.ts` 同一条口径：
#: 命中即整段不下发，而不是打码后下发——半截邮箱同样可能是别人的。
_PRIVATE_INFO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"(?<!\d)(?:\+?86)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE),
    re.compile(r"(?:[A-Za-z]:\\|/(?:Users|home|var|tmp|proc)/)"),
    re.compile(r"(?:style_id|prompt_template_id|asset_id|task_id|seed)\s*[=:]", re.IGNORECASE),
    re.compile(r"\b\d{15,}\b"),
    re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE),
)


def _exposes_private_info(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PRIVATE_INFO_PATTERNS)


def _token_count(value: int | None) -> str:
    """未上报保持 `—`，不伪造成 0（`04-ai-provider.md` 第 5 节）。"""

    return "—" if value is None else str(value)


def _to_api_error(error: ProviderCallError) -> ApiError:
    """把归一后的 Provider 失败映射到**既有**错误码，不新增码。"""

    category = error.category
    if category in (
        ProviderErrorCategory.TIMEOUT,
        ProviderErrorCategory.TEMPORARY,
        ProviderErrorCategory.RATE_LIMIT,
        ProviderErrorCategory.AUTHENTICATION,
    ):
        return ApiError(ErrorCode.PROVIDER_UNAVAILABLE)
    if category == ProviderErrorCategory.VALIDATION:
        # 供应商拒绝我们的输入：图片本身有问题，归到资产语义更准
        return ApiError(ErrorCode.ASSET_INVALID)
    if category == ProviderErrorCategory.CONTENT_POLICY:
        return ApiError(ErrorCode.ASSET_INVALID)
    return ApiError(ErrorCode.PROVIDER_BAD_GATEWAY)
