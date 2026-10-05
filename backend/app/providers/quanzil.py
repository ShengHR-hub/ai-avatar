"""Quanzil 中转站图像适配器（ENG-07 第 1 步）。

`LEAD-05` 前置补测（2026-09-28，唯一一次真实调用）确立的事实，是本文件全部取舍的依据：

- `POST {AI_BASE_URL}/images/edits` 与 `/images/generations` **同步**返回
  `data[].b64_json` PNG；`n` 被真实遵守（实测 `n=2` 一次返回 2 张不同图片，23.2s）；
- 该链路**没有**可查询任务 ID、没有取消端点、没有 Webhook；
  `x-oneapi-request-id` 只是中转层（new-api `v1.0.0-rc.22`）的追踪头，
  **不得**写进 `provider_request_id`——那会造出一个查不动的假句柄，让轮询与回调以为可恢复；
- 因此本适配器只产出 `SUCCEEDED` 与 `FAILED`，永不产出 `PENDING`；
  `check_status`/`cancel` 保留协议但归一为「不可确认」（attempt `UNKNOWN`），
  既不能编造状态，也不能被当成「明确失败」去放款。

分层与边界：

- 只做 HTTP 与归一，不写任务状态、不碰账务（`LEAD-04` §11）；
- 不导入任何供应商 SDK：`httpx` 是仓库既有依赖，代理经 `trust_env` 由运维环境决定；
- 配置只来自 `AI_BASE_URL`/`AI_MODEL`/`AI_API_KEY`；密钥既不进入代码、也不进入日志、
  错误文本或 `repr`；供应商原文一律不回显（其中可能带 Prompt 或凭据）；
- 结果图只做「能不能解码」的最小判断，像素/尺寸/哈希的完整校验仍由 Service 侧
  `persist_provider_outputs` 走同一条 `validate_image` 路径（`04-ai-provider.md` 第 8 节）。
"""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.providers.ports import (
    RETRYABLE_CATEGORIES,
    GenerateRequest,
    ProviderCallError,
    ProviderError,
    ProviderErrorCategory,
    ProviderOutput,
    ProviderResult,
    ProviderState,
    ProviderUsage,
)

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "EDITS_PATH",
    "GENERATIONS_PATH",
    "QUANZIL_MODEL",
    "QUANZIL_PROVIDER_NAME",
    "READ_TIMEOUT_SECONDS",
    "QuanzilImageProvider",
]

#: `media_assets.storage_provider` / `provider_attempts.provider_name` 用大写标识
QUANZIL_PROVIDER_NAME = "QUANZIL"

#: 前置补测实测通过的型号。这里只是**记录已验证事实**，装配值仍必须来自 `AI_MODEL`：
#: 代码不得替运维选型号（第 1 节：V1.0 固定单一 Provider，不做路由）。
QUANZIL_MODEL = "gpt-image-2.5-flare"

GENERATIONS_PATH = "/images/generations"
EDITS_PATH = "/images/edits"

#: 第 3 节冻结的时限：connect 10s / read 180s；整体 240s 由编排层的处理上限管
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 180.0
WRITE_TIMEOUT_SECONDS = 60.0
POOL_TIMEOUT_SECONDS = 10.0

#: `Retry-After` 只接受有界秒数：拿不可解析的值当等待时间会放大不可控延迟
MAX_RETRY_AFTER_SECONDS = 300

#: mode → 端点。未列出的 mode（`REFERENCE_IMAGE`）与多输入图形态都未经能力验证
_ENDPOINT_BY_MODE = {"TEXT_PROMPT": GENERATIONS_PATH, "PHOTO_PROMPT": EDITS_PATH}

#: 明确「按策略拒绝」的稳定特征（只用于分类，原文绝不外泄）
_CONTENT_POLICY_MARKS = (
    "content_policy",
    "content policy",
    "moderation",
    "safety",
    "policy",
    "prohibited",
    "rejected_by",
)

#: 实测（2026-09-28）中转站的「这个 model 当前没有可用渠道」以 **404 + code=model_not_found**
#: 返回，而不是 5xx。字面意思既可能是渠道临时全断，也可能就是型号配错，而
#: `total_usage` 在真实出图前后**根本不变化**，所以「探针增量 0」不能当作没有外部执行的证据。
#: 在能区分这两种成因之前按**不可重试的明确失败**处理：配错重试三次也不会变好，
#: 而万一本底是「请求已排进队列」，那三次重试可能就是三次真实扣费。证据不足时不拿钱赌。
_STABLE_PROVIDER_CODES = {
    "model_not_found": "quanzil_model_not_found",
    "invalid_request": "quanzil_invalid_request",
    "local:convert_request_failed": "quanzil_convert_request_failed",
}

#: `provider_attempts.error_code` 的列宽（映射后必须是短、稳、可枚举的 token）
ERROR_CODE_MAX_LENGTH = 64

#: 认证/账务侧拒绝：账号或凭据状态问题，重试不会变好，且可证明没有外部执行
_AUTHENTICATION_STATUSES = frozenset({401, 402, 403})
_VALIDATION_STATUSES = frozenset({400, 404, 405, 409, 422})


def _require_text(value: str, name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{name} 不能为空。")
    return text


def _validated_base_url(base_url: str) -> str:
    """校验 `AI_BASE_URL`：只允许 http(s) 的绝对地址，不得内嵌凭据、查询或片段。

    端点是唯一的出网目标，放宽这里等于把 SSRF 与凭据外送写进配置通路；带 `?token=`
    的地址还会让密钥经 URL 进入对方访问日志。宁可在装配期报错，也不带着可疑地址出网。
    """

    raw = _require_text(base_url, "AI_BASE_URL")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("AI_BASE_URL 必须是 http 或 https 绝对地址。")
    if not parts.hostname:
        raise ValueError("AI_BASE_URL 缺少主机名。")
    if "@" in parts.netloc:
        raise ValueError("AI_BASE_URL 不得内嵌凭据。")
    if parts.query or parts.fragment:
        raise ValueError("AI_BASE_URL 不得携带查询参数或片段。")
    return raw.rstrip("/")


def _decoded_image(content: bytes) -> str | None:
    """最小可解码性判断（返回真实 MIME）；完整像素/尺寸/哈希校验留在 Service 侧。"""

    from PIL import Image

    if not content:
        return None
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return None
    try:
        with Image.open(BytesIO(content)) as opened:
            opened.verify()
    except Exception:  # noqa: BLE001 - 任何解码失败都只意味着「结果不可信」
        return None
    return mime


class QuanzilImageProvider:
    """`AIProvider` 的 Quanzil 实现：同步出图，无任务查询、无取消、无 Webhook。"""

    provider_name = QUANZIL_PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
        connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = READ_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        self._api_key = _require_text(api_key, "AI_API_KEY")
        self._model = _require_text(model, "AI_MODEL")
        self._transport = transport
        self._connect_timeout = float(connect_timeout_seconds)
        self._read_timeout = float(read_timeout_seconds)

    @property
    def model_name(self) -> str:
        return self._model

    # ------------------------------------------------------------------ 协议
    def generate(self, request: GenerateRequest) -> ProviderResult:
        """提交一次同步生成，把响应归一成 `SUCCEEDED`/`FAILED`。

        任何「已送出但结果不可信」的情形都抛 `ProviderCallError`：读超时、5xx、
        响应缺字段、b64 不可解码。按第 3 节它们一律落成 attempt `UNKNOWN`，
        点数保持 `RESERVED` 等人工判定——绝不自动重试，因为重试可能出第二份图。
        """

        rejection = self._reject_unsupported(request)
        if rejection is not None:
            return rejection

        path = _ENDPOINT_BY_MODE[request.mode.strip().upper()]
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": request.prompt,
            "n": int(request.output_count),
        }
        files = None
        if path == EDITS_PATH:
            source = request.source_images[0]
            files = {"image": (f"source.{_extension(source.mime_type)}", source.content, source.mime_type)}
            response = self._post(path, data=payload, files=files)
        else:
            response = self._post(path, json=payload)

        return self._normalize(response, request)

    def check_status(self, provider_request_id: str) -> ProviderResult:
        """该链路没有任务状态查询：只报告「不可确认」，绝不编造进度。"""

        raise ProviderCallError(
            category=ProviderErrorCategory.UNKNOWN,
            code="quanzil_status_unsupported",
            safe_message="该供应商不提供任务状态查询。",
            request_accepted=None,
        )

    def cancel(self, provider_request_id: str) -> ProviderResult:
        """没有取消端点：接受取消都无从谈起，只能归为不可确认（第 2 节）。"""

        raise ProviderCallError(
            category=ProviderErrorCategory.UNKNOWN,
            code="quanzil_cancel_unsupported",
            safe_message="该供应商不提供请求取消。",
            request_accepted=None,
        )

    # ------------------------------------------------------------------ 内部
    def _reject_unsupported(self, request: GenerateRequest) -> ProviderResult | None:
        """把未经验证的输入形态挡在出网之前：明确失败，且可证明没有外部执行。"""

        mode = request.mode.strip().upper()
        sources = tuple(request.source_images)
        references = tuple(request.reference_images)

        if mode not in _ENDPOINT_BY_MODE or references:
            return self._failure(
                request,
                ProviderErrorCategory.VALIDATION,
                "quanzil_unsupported_input_shape",
                "该输入形态未经供应商能力验证，已拒绝提交。",
            )
        if mode == "PHOTO_PROMPT" and len(sources) != 1:
            return self._failure(
                request,
                ProviderErrorCategory.VALIDATION,
                "quanzil_unsupported_input_shape",
                "照片模式需要且只支持一张源图。",
            )
        if mode == "TEXT_PROMPT" and sources:
            return self._failure(
                request,
                ProviderErrorCategory.VALIDATION,
                "quanzil_unsupported_input_shape",
                "文字模式不接受输入图。",
            )
        if int(request.output_count) < 1:
            return self._failure(
                request,
                ProviderErrorCategory.VALIDATION,
                "quanzil_invalid_output_count",
                "输出数量必须为正整数。",
            )
        return None

    def _failure(
        self,
        request: GenerateRequest,
        category: ProviderErrorCategory,
        code: str,
        safe_message: str,
    ) -> ProviderResult:
        """`FAILED` 必须携带归一错误（第 2 节硬约束），且 `request_accepted=False`。"""

        return ProviderResult(
            state=ProviderState.FAILED,
            client_request_id=request.client_request_id,
            provider_request_id=None,
            error=ProviderError(
                category=category,
                code=code,
                safe_message=safe_message,
                retryable=category in RETRYABLE_CATEGORIES,
                request_accepted=False,
            ),
        )

    def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        """发一次请求；传输层异常在这里就归一，绝不让它裸漏给编排层。"""

        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=WRITE_TIMEOUT_SECONDS,
            pool=POOL_TIMEOUT_SECONDS,
        )
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as client:
                return client.post(f"{self._base_url}{path}", headers=headers, **kwargs)
        except (httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            # 请求从未离开本机：可证明没有外部执行，按提交前超时可重试
            raise ProviderCallError(
                category=ProviderErrorCategory.TIMEOUT,
                code="quanzil_timeout_before_acceptance",
                safe_message="连接供应商超时，可证明未接受请求。",
                request_accepted=False,
            ) from error
        except httpx.ConnectError as error:
            # TCP/TLS 都没建立起来：请求从未离开本机，可证明没有外部执行
            raise ProviderCallError(
                category=ProviderErrorCategory.TIMEOUT,
                code="quanzil_timeout_before_acceptance",
                safe_message="无法与供应商建立连接，可证明未接受请求。",
                request_accepted=False,
            ) from error
        except httpx.TimeoutException as error:
            # 已经送出（读/写超时）：接受状态不明，只能人工判定（第 3 节）
            raise ProviderCallError(
                category=ProviderErrorCategory.TIMEOUT,
                code="quanzil_timeout_after_acceptance",
                safe_message="供应商响应超时，无法确认请求是否已被接受。",
                request_accepted=None,
            ) from error
        except httpx.HTTPError as error:
            # 连接中断/DNS/TLS 等一律按「不可确认」处理，避免把可能已扣费的任务重投
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="quanzil_transport_failure",
                safe_message="与供应商的传输链路异常，无法确认请求状态。",
                request_accepted=None,
            ) from error

    def _normalize(self, response: httpx.Response, request: GenerateRequest) -> ProviderResult:
        """按 HTTP 状态与响应体归一；供应商原文绝不进入 `code`/`safe_message`。"""

        status = response.status_code

        if 500 <= status:
            # 服务端故障意味着请求已到达：不能断言未接受，否则重试可能重复出图重复扣费
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code=f"quanzil_server_error_{status}",
                safe_message="供应商内部错误，无法确认请求是否已产生外部执行。",
                request_accepted=None,
            )

        if status == 429:
            result = self._failure(
                request,
                ProviderErrorCategory.RATE_LIMIT,
                "quanzil_rate_limited",
                "供应商限流，请求未被受理。",
            )
            return ProviderResult(
                state=ProviderState.FAILED,
                client_request_id=request.client_request_id,
                provider_request_id=None,
                error=result.error,
                retry_after_seconds=_retry_after_seconds(response),
            )

        if status in _AUTHENTICATION_STATUSES:
            return self._failure(
                request,
                ProviderErrorCategory.AUTHENTICATION,
                _mapped_code(
                    _provider_code(response), f"quanzil_rejected_authentication_{status}"
                ),
                "供应商拒绝了本次凭据或账务状态。",
            )

        if status in _VALIDATION_STATUSES:
            provider_code = _provider_code(response)
            if _looks_like_content_policy(response):
                return self._failure(
                    request,
                    ProviderErrorCategory.CONTENT_POLICY,
                    "quanzil_content_policy",
                    "供应商按内容策略拒绝了本次请求。",
                )
            return self._failure(
                request,
                ProviderErrorCategory.VALIDATION,
                _mapped_code(provider_code, f"quanzil_rejected_request_{status}"),
                "供应商拒绝了本次请求参数。",
            )

        if status not in (200, 201):
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code=f"quanzil_unexpected_status_{status}",
                safe_message="供应商返回了无法归一的状态。",
                request_accepted=None,
            )

        return self._from_body(response, request)

    def _from_body(self, response: httpx.Response, request: GenerateRequest) -> ProviderResult:
        try:
            body = response.json()
        except Exception as error:  # noqa: BLE001 - 非 JSON 终态响应属于结果不可信
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="quanzil_malformed_body",
                safe_message="供应商成功响应无法解析。",
                request_accepted=None,
            ) from error

        if not isinstance(body, dict):
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="quanzil_malformed_body",
                safe_message="供应商成功响应结构不可信。",
                request_accepted=None,
            )

        items = body.get("data")
        if not isinstance(items, list):
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="quanzil_missing_outputs",
                safe_message="供应商成功响应缺少结果列表。",
                request_accepted=None,
            )

        outputs: list[ProviderOutput] = []
        for item in items:
            decoded = _output_from_item(item)
            if decoded is None:
                # 只回 URL、b64 不可解或不是可解码图片：结果不可信，不补齐也不猜
                raise ProviderCallError(
                    category=ProviderErrorCategory.UNKNOWN,
                    code="quanzil_unusable_output",
                    safe_message="供应商结果不可解码，无法作为可信输出。",
                    request_accepted=None,
                )
            outputs.append(decoded)

        return ProviderResult(
            state=ProviderState.SUCCEEDED,
            client_request_id=request.client_request_id,
            # 同步链路没有可查询的外部请求 ID：保持 NULL，绝不拿中转追踪头冒充
            provider_request_id=None,
            outputs=tuple(outputs),
            usage=_usage_from_body(body, image_count=len(outputs)),
        )


def _output_from_item(item: Any) -> ProviderOutput | None:
    if not isinstance(item, dict):
        return None
    encoded = item.get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    mime_type = _decoded_image(content)
    if mime_type is None:
        return None
    external_id = item.get("id") or item.get("provider_output_id")
    return ProviderOutput(
        content=content,
        mime_type=mime_type,
        provider_output_id=str(external_id) if external_id is not None else None,
    )


def _usage_from_body(body: dict[str, Any], *, image_count: int) -> ProviderUsage | None:
    """映射用量；供应商未给的字段保持 `NULL`，绝不写成 0（第 5 节）。

    `quota` 是中转站的原始计价单位，补测未标定它与美元的换算率，因此 `estimated_cost`
    与 `currency` 一律保持 `NULL`——内部核算继续用 `generation_cost_configs` 的快照。
    """

    raw = body.get("usage")
    if not isinstance(raw, dict):
        return None

    def _count(key: str) -> int | None:
        value = raw.get(key)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    return ProviderUsage(
        input_tokens=_count("input_tokens"),
        output_tokens=_count("output_tokens"),
        image_count=image_count or None,
        estimated_cost=None,
        currency=None,
    )


def _error_block(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 错误体解析不了不影响归一为一个明确失败
        return {}
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    return error if isinstance(error, dict) else {}


def _provider_code(response: httpx.Response) -> str:
    """取供应商的稳定机读码（实测形如 `model_not_found`、`invalid_request`）。"""

    return str(_error_block(response).get("code") or "").strip()


def _mapped_code(provider_code: str, fallback: str) -> str:
    """只有**已知机读码**才映射成固定 token；未知码一律走 HTTP 状态回退码。

    不能「过滤字符后拼进去」：`code` 字段本身也是供应商可控文本——实测它曾出现过
    `local:convert_request_failed` 这类自由格式值，谁也无法保证哪天不会正好是密钥或
    Prompt 片段。白名单之外的内容一个字都不落库，运维定位靠固定状态码 + 安全日志。
    """

    mapped = _STABLE_PROVIDER_CODES.get((provider_code or "").strip().lower())
    if mapped is not None:
        return mapped
    if len(fallback) > ERROR_CODE_MAX_LENGTH:
        raise AssertionError("回退 error_code 超出列宽，映射表必须保持短码。")
    return fallback


def _looks_like_content_policy(response: httpx.Response) -> bool:
    """分类判断可以读供应商原文，但原文本身绝不外泄到日志、响应或 attempt 行。"""

    error = _error_block(response)
    haystack = " ".join(
        str(error.get(key, "")) for key in ("code", "type", "err_code", "param", "message")
    ).lower()
    return any(mark in haystack for mark in _CONTENT_POLICY_MARKS)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw.isdigit():
        return None
    return min(int(raw), MAX_RETRY_AFTER_SECONDS)


def _extension(mime_type: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(
        mime_type.strip().lower(), "bin"
    )
