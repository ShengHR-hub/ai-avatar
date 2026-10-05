"""Quanzil 中转站的**图片理解**适配器（与出图适配器分开）。

为什么单独一个类、单独一个型号：

- 出图（`/images/generations`、`/images/edits`）与看图（`/chat/completions` + 视觉模型）
  是两种能力、两套计价；混进 `QuanzilImageProvider` 会让"参考图分析"看起来像一条生成路径；
- 型号必须来自 `AI_VISION_MODEL`，不复用 `AI_MODEL`——用出图型号去读图会两边都坏。

口径（沿用 ENG-07 已确立的纪律）：

- 只做 HTTP 与归一，不写任务状态、不碰账务；
- 凭据不进代码、日志、错误文本或 `repr`；供应商原文一律不回显；
- 失败一律抛 `ProviderCallError`，绝不返回半截文本；
- 输出是**近似画面描述**，不是原作者的原始提示词，也不承诺复刻。
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app.providers.ports import (
    DESCRIPTION_MAX_CHARS,
    ImageDescriptionRequest,
    ImageDescriptionResult,
    ProviderCallError,
    ProviderErrorCategory,
    ProviderUsage,
)
from app.providers.quanzil import (
    CONNECT_TIMEOUT_SECONDS,
    POOL_TIMEOUT_SECONDS,
    QUANZIL_PROVIDER_NAME,
    WRITE_TIMEOUT_SECONDS,
    _decoded_image,
    _require_text,
    _validated_base_url,
)

__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "VISION_READ_TIMEOUT_SECONDS",
    "QuanzilVisionAnalyzer",
]

CHAT_COMPLETIONS_PATH = "/chat/completions"

#: 看图比出图快得多；45 秒还没回来就按不可确认处理，绝不重试（重试等于二次付费）
VISION_READ_TIMEOUT_SECONDS = 45.0

#: 给模型的固定指令。三条硬约束写在指令里，也在适配器里兜底：
#: 只描述看到的画面 / 不猜原始提示词 / 不照抄图里的文字。
DESCRIBE_INSTRUCTION = (
    "你在帮用户把一张参考图片转成一段可编辑的画面描述。\n"
    "只描述你在这张图里实际看到的内容：主体是什么、姿态与朝向、画法或材质、"
    "配色与光线、背景与场景、整体情绪。\n"
    "用一段中文写，60–120 字，逗号分隔的短句；不要分点、不要小标题、不要评价好坏。\n"
    "不要猜测这张图是用什么提示词生成的，也不要出现“提示词”这类字样。\n"
    "图里如果有文字或水印，不要照抄其中的文字内容。\n"
    "不要写人名、品牌名或任何可识别的身份信息。"
)

#: 明确「按策略拒绝」的稳定特征（只用于分类，原文绝不外泄）
_CONTENT_POLICY_MARKS = (
    "content_policy",
    "content policy",
    "moderation",
    "safety",
    "prohibited",
)

_AUTHENTICATION_STATUSES = frozenset({401, 402, 403})
_VALIDATION_STATUSES = frozenset({400, 404, 405, 409, 422})


def _truncate(text: str) -> str:
    """超长描述按上限截断：一段读不完的描述对用户没有价值，还会放大成本。"""

    value = text.strip()
    if len(value) <= DESCRIPTION_MAX_CHARS:
        return value
    return value[: DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"


class QuanzilVisionAnalyzer:
    """`ImageAnalyzer` 的 Quanzil 实现：一次同步调用，返回一段描述。"""

    provider_name = QUANZIL_PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = VISION_READ_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        self._api_key = _require_text(api_key, "AI_API_KEY")
        self._model = _require_text(model, "AI_VISION_MODEL")
        self._transport = transport
        self._connect_timeout = float(connect_timeout_seconds)
        self._read_timeout = float(read_timeout_seconds)

    @property
    def model_name(self) -> str:
        return self._model

    # ------------------------------------------------------------------ 协议
    def describe_image(self, request: ImageDescriptionRequest) -> ImageDescriptionResult:
        image = request.image
        mime = _decoded_image(image.content)
        if mime is None:
            raise ProviderCallError(
                category=ProviderErrorCategory.VALIDATION,
                code="quanzil_vision_undecodable_image",
                safe_message="这张图片无法解码，未提交给供应商。",
                request_accepted=False,
            )

        data_url = "data:%s;base64,%s" % (mime, base64.b64encode(image.content).decode("ascii"))
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": int(request.max_output_tokens),
            "temperature": 0.2,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DESCRIBE_INSTRUCTION},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }

        response = self._post(payload)
        return self._normalize(response)

    # ------------------------------------------------------------------ 内部
    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=WRITE_TIMEOUT_SECONDS,
            pool=POOL_TIMEOUT_SECONDS,
        )
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as client:
                return client.post(
                    f"{self._base_url}{CHAT_COMPLETIONS_PATH}", headers=headers, json=payload
                )
        except (httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError) as error:
            # 请求从未离开本机：可证明没有外部执行
            raise ProviderCallError(
                category=ProviderErrorCategory.TIMEOUT,
                code="quanzil_vision_not_connected",
                safe_message="无法与供应商建立连接，可证明未接受请求。",
                request_accepted=False,
            ) from error
        except httpx.TimeoutException as error:
            # 已经送出：接受状态不明，绝不重试
            raise ProviderCallError(
                category=ProviderErrorCategory.TIMEOUT,
                code="quanzil_vision_timeout_after_send",
                safe_message="看图请求超时，无法确认供应商是否已完成。",
                request_accepted=None,
            ) from error
        except httpx.HTTPError as error:
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="quanzil_vision_transport_error",
                safe_message="与供应商通信失败。",
                request_accepted=None,
            ) from error

    def _normalize(self, response: httpx.Response) -> ImageDescriptionResult:
        status = response.status_code
        if 500 <= status:
            raise ProviderCallError(
                category=ProviderErrorCategory.TEMPORARY,
                code="quanzil_vision_upstream_error",
                safe_message="供应商暂时不可用。",
                request_accepted=None,
            )
        if status in _AUTHENTICATION_STATUSES:
            raise ProviderCallError(
                category=ProviderErrorCategory.AUTHENTICATION,
                code="quanzil_vision_authentication_failed",
                safe_message="供应商拒绝了这次看图请求的凭据或额度。",
                request_accepted=False,
            )
        if status in _VALIDATION_STATUSES:
            raise ProviderCallError(
                category=ProviderErrorCategory.VALIDATION,
                code="quanzil_vision_request_rejected",
                safe_message="供应商拒绝了这次看图请求。",
                request_accepted=False,
            )
        if status == 429:
            raise ProviderCallError(
                category=ProviderErrorCategory.RATE_LIMIT,
                code="quanzil_vision_rate_limited",
                safe_message="看图请求被供应商限流。",
                request_accepted=False,
            )
        if status != 200:
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="quanzil_vision_unexpected_status",
                safe_message="供应商返回了未预期的状态。",
                request_accepted=None,
            )

        try:
            body = response.json()
        except ValueError as error:
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="quanzil_vision_invalid_json",
                safe_message="供应商返回了无法解析的响应。",
                request_accepted=None,
            ) from error

        if isinstance(body, dict) and body.get("error"):
            raise ProviderCallError(
                category=self._category_of_error(body),
                code="quanzil_vision_error_body",
                safe_message="供应商返回了错误结果。",
                request_accepted=False,
            )

        choices = body.get("choices") if isinstance(body, dict) else None
        content = None
        if isinstance(choices, list) and choices:
            first = choices[0]
            message = first.get("message") if isinstance(first, dict) else None
            if isinstance(message, dict):
                raw = message.get("content")
                # 有些中转会把内容拆成块数组
                if isinstance(raw, list):
                    raw = "".join(
                        part.get("text", "")
                        for part in raw
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                if isinstance(raw, str):
                    content = raw

        text = _truncate(content) if content else ""
        if not text:
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="quanzil_vision_empty_description",
                safe_message="供应商没有返回可用的描述。",
                request_accepted=None,
            )

        usage_raw = body.get("usage") if isinstance(body, dict) else None
        usage = None
        if isinstance(usage_raw, dict):
            usage = ProviderUsage(
                input_tokens=_as_int(usage_raw.get("prompt_tokens")),
                output_tokens=_as_int(usage_raw.get("completion_tokens")),
            )

        return ImageDescriptionResult(description=text, model=self._model, usage=usage)

    @staticmethod
    def _category_of_error(body: dict[str, Any]) -> ProviderErrorCategory:
        """按响应体里的稳定特征分类；原文绝不进入 `code`/`safe_message`。"""

        raw = str(body.get("error") or "")
        lowered = raw.lower()
        if any(mark in lowered for mark in _CONTENT_POLICY_MARKS):
            return ProviderErrorCategory.CONTENT_POLICY
        if "rate" in lowered and "limit" in lowered:
            return ProviderErrorCategory.RATE_LIMIT
        return ProviderErrorCategory.UNKNOWN


def _as_int(value: Any) -> int | None:
    """未上报必须保持 `None`（落库为 `NULL`），不得伪造成 0。"""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None
