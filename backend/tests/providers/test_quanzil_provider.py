"""Quanzil `gpt-image-2.5-flare` 适配器测试（ENG-07 第 1 步）。

只使用 `httpx.MockTransport`：**不发起任何真实网络请求，不读取任何真实凭据**。

冻结依据与已实测事实（`LEAD-05` 前置补测，2026-09-28）：

- `POST /v1/images/edits` 同步返回 2 张 `b64_json` PNG（HTTP 200、23.2s，
  `usage.input_tokens/output_tokens/image_tokens` 齐备），`n` 参数被真实遵守；
- 该链路**没有**可查询任务 ID、没有取消端点、没有 Webhook；`x-oneapi-request-id`
  只是中转层追踪头，不得写进 `provider_request_id`；
- 因此结果只有 `SUCCEEDED`/`FAILED` 两种终态，永不产出 `PENDING`；
  `check_status`/`cancel` 保留协议但必须归一为「不支持」→ attempt `UNKNOWN`；
- **明确拒绝**（400/401/402/403/404/429）= 可证明未产生外部执行 → `FAILED` +
  归一 `ProviderError`，由编排层放款一次；**不可确认**（读超时、5xx、结果无法解析）
  → 抛 `ProviderCallError`，`classify` 判 `UNKNOWN_OUTCOME`，保持 `RESERVED`；
- 用量与成本未知保持 `NULL`，绝不伪造 0（第 5 节）；中转站 `quota` 原始单位在
  未标定换算率前不得变成金额。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable

import httpx
import pytest

from app.providers.ports import (
    GenerateRequest,
    ImageInput,
    ProviderCallError,
    ProviderErrorCategory,
    ProviderResult,
    ProviderState,
    classify,
)
from app.providers.quanzil import (
    QUANZIL_MODEL,
    QUANZIL_PROVIDER_NAME,
    QuanzilImageProvider,
)
from app.services.generation.provider_factory import build_ai_provider
from tests.services.fakes import png_with_markup_in_metadata, synthetic_png_bytes

API_KEY = "synthetic-relay-key-never-log"
BASE_URL = "https://quanzil.test/v1"
PROMPT = "把这张照片转成 3D 黏土风格头像，保持五官特征"
PNG = synthetic_png_bytes()
B64 = base64.b64encode(PNG).decode()


def make_image_input(index: int = 0) -> ImageInput:
    return ImageInput(
        mime_type="image/png",
        content=synthetic_png_bytes(size=(8, 8)),
        sha256=f"sha{index}",
    )


def request_for(
    mode: str,
    *,
    sources: tuple[ImageInput, ...] = (),
    references: tuple[ImageInput, ...] = (),
    count: int = 2,
) -> GenerateRequest:
    return GenerateRequest(
        mode=mode,
        prompt=PROMPT,
        source_images=sources,
        reference_images=references,
        style=None,
        output_count=count,
        model=QUANZIL_MODEL,
        client_request_id="gid-1:1",
    )


def image_response(count: int = 2, **overrides: object) -> httpx.Response:
    payload: dict[str, object] = {
        "created": 1790591007,
        "data": [{"b64_json": B64} for _ in range(count)],
        "size": "1024x1024",
        "quality": "auto",
        "output_format": "png",
        "background": "opaque",
        "usage": {
            "input_tokens": 2138,
            "input_tokens_details": {"image_tokens": 2048, "text_tokens": 90},
            "output_tokens": 458,
            "output_tokens_details": {"image_tokens": 458, "text_tokens": 0},
            "total_tokens": 2596,
        },
        "quota": 2000,
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


class Recorder:
    """假传输：记录每一次请求，并按脚本返回响应或抛出异常。"""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.calls: list[httpx.Request] = []
        self._handler = handler

    @property
    def count(self) -> int:
        return len(self.calls)

    def transport(self) -> httpx.MockTransport:
        def _handle(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return self._handler(request)

        return httpx.MockTransport(_handle)


def build(
    handler: Callable[[httpx.Request], httpx.Response], **overrides: object
) -> tuple[QuanzilImageProvider, Recorder]:
    recorder = Recorder(handler)
    params: dict[str, object] = {
        "base_url": BASE_URL,
        "api_key": API_KEY,
        "model": QUANZIL_MODEL,
        "transport": recorder.transport(),
    }
    params.update(overrides)
    return QuanzilImageProvider(**params), recorder  # type: ignore[arg-type]


def provider(**overrides: object) -> QuanzilImageProvider:
    made, _recorder = build(lambda _request: image_response(), **overrides)
    return made


def error_response(
    status: int, message: str, **extra: object
) -> Callable[[httpx.Request], httpx.Response]:
    error_type = extra.pop("type", "invalid_request_error")
    headers = extra.pop("headers", {})
    body = {"error": {"message": message, "type": error_type, **extra}}
    return lambda _request: httpx.Response(status, json=body, headers=dict(headers))


def failure_error(result: ProviderResult):
    assert result.error is not None, result
    return result.error


# --------------------------------------------------------------------------- #
# 端点路由与请求形状
# --------------------------------------------------------------------------- #
def test_text_prompt_calls_generations_and_returns_two_decoded_images() -> None:
    adapter, recorder = build(lambda _request: image_response(count=2))

    result = adapter.generate(request_for("TEXT_PROMPT"))

    assert result.state is ProviderState.SUCCEEDED
    assert len(result.outputs) == 2
    assert [item.content for item in result.outputs] == [PNG, PNG]
    assert [item.mime_type for item in result.outputs] == ["image/png", "image/png"]
    assert result.client_request_id == "gid-1:1"

    call = recorder.calls[0]
    assert call.method == "POST"
    assert str(call.url) == f"{BASE_URL}/images/generations"
    assert json.loads(call.content)["n"] == 2


def test_photo_prompt_calls_edits_as_multipart_with_the_source_image() -> None:
    source = make_image_input()
    adapter, recorder = build(lambda _request: image_response(count=2))

    result = adapter.generate(request_for("PHOTO_PROMPT", sources=(source,)))

    assert result.state is ProviderState.SUCCEEDED
    call = recorder.calls[0]
    assert str(call.url) == f"{BASE_URL}/images/edits"
    assert "multipart/form-data" in call.headers["content-type"]
    assert source.content in call.content
    for field in (b'name="prompt"', b'name="image"', b'name="n"', b'name="model"'):
        assert field in call.content, field


def test_relay_trace_header_is_never_mistaken_for_a_provider_request_id() -> None:
    """`x-oneapi-request-id` 只是中转层追踪头，写进 `provider_request_id` 会造出假的「可查询」句柄。"""

    def _handler(_request: httpx.Request) -> httpx.Response:
        response = image_response()
        response.headers["x-oneapi-request-id"] = "202609281023076901208888268d9d6MmAlpjid"
        return response

    adapter, _recorder = build(_handler)
    result = adapter.generate(request_for("PHOTO_PROMPT", sources=(make_image_input(),)))

    assert result.provider_request_id is None


def test_adapter_never_reports_pending() -> None:
    adapter, _recorder = build(lambda _request: image_response())

    assert adapter.generate(request_for("TEXT_PROMPT")).state is not ProviderState.PENDING


# --------------------------------------------------------------------------- #
# 能力缺失的边界：绝不假装支持
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("mode", "sources", "references"),
    [
        ("REFERENCE_IMAGE", (), (make_image_input(),)),
        ("PHOTO_PROMPT", (make_image_input(), make_image_input(1)), ()),
        ("TEXT_PROMPT", (make_image_input(),), ()),
    ],
)
def test_unsupported_input_shapes_are_refused_before_any_http_call(
    mode: str, sources: tuple[ImageInput, ...], references: tuple[ImageInput, ...]
) -> None:
    """实测只支持单图源：多参考图与「文字模式带图」都没验证过，只能明确拒绝而不是静默降级。

    这是本地即可证明「未受理」的明确失败，因此是 `FAILED + VALIDATION`：由编排层放款一次。
    若伪装成「结果不可确认」，用户点数会被无端挂在 `RESERVED` 上。
    """

    adapter, recorder = build(lambda _request: image_response())

    result = adapter.generate(request_for(mode, sources=sources, references=references))

    error = failure_error(result)
    assert result.state is ProviderState.FAILED
    assert error.category is ProviderErrorCategory.VALIDATION
    assert error.retryable is False
    assert error.request_accepted is False
    assert classify(error).value == "FAILURE"
    assert recorder.count == 0


def test_check_status_and_cancel_are_declared_unsupported() -> None:
    """无任务查询/取消端点：协议方法必须归一为「不可确认」，既不编造状态也不当作明确失败。"""

    adapter, recorder = build(lambda _request: image_response())

    for call in (lambda: adapter.check_status("whatever"), lambda: adapter.cancel("whatever")):
        with pytest.raises(ProviderCallError) as raised:
            call()
        assert raised.value.category is ProviderErrorCategory.UNKNOWN
        assert raised.value.request_accepted is None
        assert classify(raised.value).value == "UNKNOWN_OUTCOME"

    assert recorder.count == 0


# --------------------------------------------------------------------------- #
# usage 与成本：未知保持 NULL
# --------------------------------------------------------------------------- #
def test_usage_is_mapped_from_the_reported_token_counters() -> None:
    adapter, _recorder = build(lambda _request: image_response())

    usage = adapter.generate(request_for("TEXT_PROMPT")).usage

    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.image_count) == (2138, 458, 2)


def test_relay_quota_units_are_not_converted_without_a_verified_rate() -> None:
    """补测未标定 `quota` 与美元的换算关系：成本保持 NULL，伪造金额会污染内部核算。"""

    adapter, _recorder = build(lambda _request: image_response())

    usage = adapter.generate(request_for("TEXT_PROMPT")).usage

    assert usage is not None
    assert usage.estimated_cost is None
    assert usage.currency is None


def test_missing_usage_block_stays_none_instead_of_zero() -> None:
    adapter, _recorder = build(lambda _request: image_response(usage=None))

    usage = adapter.generate(request_for("TEXT_PROMPT")).usage

    assert usage is None or (
        usage.input_tokens is None
        and usage.output_tokens is None
        and usage.estimated_cost is None
    )


# --------------------------------------------------------------------------- #
# 传输层与状态码归一
# --------------------------------------------------------------------------- #
def test_read_timeout_is_ambiguous_and_never_retryable() -> None:
    """读超时意味着请求已送达：接受状态不明，只能人工复核，不能重试。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic read timeout", request=request)

    adapter, _recorder = build(_handler)

    with pytest.raises(ProviderCallError) as raised:
        adapter.generate(request_for("PHOTO_PROMPT", sources=(make_image_input(),)))

    error = raised.value
    assert error.category is ProviderErrorCategory.TIMEOUT
    assert error.request_accepted is None
    assert classify(error).value == "UNKNOWN_OUTCOME"


def test_connect_failure_proves_no_external_execution() -> None:
    """连接都没建立：可证明未接受，按提交前超时可重试（预算归编排层管）。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connect refused", request=request)

    adapter, _recorder = build(_handler)

    with pytest.raises(ProviderCallError) as raised:
        adapter.generate(request_for("TEXT_PROMPT"))

    error = raised.value
    assert error.category is ProviderErrorCategory.TIMEOUT
    assert error.request_accepted is False
    assert classify(error).value == "RETRY"


@pytest.mark.parametrize(
    ("status", "message", "expected_category", "expected_retryable"),
    [
        (401, "Invalid token (unauthorized)", ProviderErrorCategory.AUTHENTICATION, False),
        (403, "token is blocked", ProviderErrorCategory.AUTHENTICATION, False),
        (402, "insufficient user quota", ProviderErrorCategory.AUTHENTICATION, False),
        (400, "content policy: request rejected", ProviderErrorCategory.CONTENT_POLICY, False),
        (400, "image is too large", ProviderErrorCategory.VALIDATION, False),
        (404, "Unsupported model gpt-image-2.5-flare", ProviderErrorCategory.VALIDATION, False),
        (429, "rate limit exceeded", ProviderErrorCategory.RATE_LIMIT, True),
    ],
)
def test_definite_rejections_become_a_normalized_failure(
    status: int,
    message: str,
    expected_category: ProviderErrorCategory,
    expected_retryable: bool,
) -> None:
    """供应商明确拒绝 = 可证明没有外部执行：`FAILED` + 归一错误，由编排层放款一次。"""

    adapter, recorder = build(error_response(status, message))

    result = adapter.generate(request_for("TEXT_PROMPT"))

    error = failure_error(result)
    assert result.state is ProviderState.FAILED
    assert error.category is expected_category
    assert error.retryable is expected_retryable
    assert error.request_accepted is False
    assert recorder.count == 1


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_side_failures_do_not_claim_non_acceptance(status: int) -> None:
    """5xx 说明请求已到达服务端：不能断言「未被接受」，否则重试会重复出图重复扣费。"""

    adapter, _recorder = build(error_response(status, "upstream internal error"))

    with pytest.raises(ProviderCallError) as raised:
        adapter.generate(request_for("TEXT_PROMPT"))

    assert raised.value.category is ProviderErrorCategory.UNKNOWN
    assert raised.value.request_accepted is None
    assert classify(raised.value).value == "UNKNOWN_OUTCOME"


def test_rate_limit_carries_a_bounded_retry_after() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}},
            headers={"Retry-After": "7"},
        )

    adapter, _recorder = build(_handler)

    result = adapter.generate(request_for("TEXT_PROMPT"))

    assert failure_error(result).category is ProviderErrorCategory.RATE_LIMIT
    assert result.retry_after_seconds == 7
    assert classify(failure_error(result)).value == "RETRY"


def test_a_garbage_retry_after_header_is_dropped_not_trusted() -> None:
    """非数字 `Retry-After` 只能忽略：拿它当等待时间会放大不可控延迟。"""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "rate limit exceeded"}},
            headers={"Retry-After": "next-moon"},
        )

    adapter, _recorder = build(_handler)

    result = adapter.generate(request_for("TEXT_PROMPT"))

    assert result.retry_after_seconds is None


def test_authentication_failures_are_final_not_retried() -> None:
    adapter, _recorder = build(error_response(401, "Invalid token", type="authentication_error"))

    result = adapter.generate(request_for("TEXT_PROMPT"))

    assert classify(failure_error(result)).value == "FAILURE"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": [{"url": "https://relay.invalid/a.png"}]},
        {"data": [{"b64_json": "not-base64-@@@"}]},
        {"data": [{"b64_json": base64.b64encode(b"definitely not an image").decode()}]},
        "not-a-dict",
    ],
)
def test_malformed_terminal_responses_become_unknown(payload: object) -> None:
    """已受理但结果不可信：一律 attempt `UNKNOWN` + 人工复核，绝不写成功也不补齐结果。"""

    handler = (
        (lambda _request: httpx.Response(200, json=payload))
        if not isinstance(payload, str)
        else (lambda _request: httpx.Response(200, text=payload))
    )
    adapter, _recorder = build(handler)

    with pytest.raises(ProviderCallError) as raised:
        adapter.generate(request_for("TEXT_PROMPT"))

    assert raised.value.category is ProviderErrorCategory.UNKNOWN
    assert raised.value.request_accepted is None


def test_short_count_is_returned_as_is_for_the_service_to_review() -> None:
    """只回 1 张时适配器如实返回：不足量由 Service 判 `NEEDS_REVIEW`，不在这里伪造补齐。"""

    adapter, _recorder = build(lambda _request: image_response(count=1))

    result = adapter.generate(request_for("TEXT_PROMPT"))

    assert result.state is ProviderState.SUCCEEDED
    assert len(result.outputs) == 1


# --------------------------------------------------------------------------- #
# 脱敏与配置装配
# --------------------------------------------------------------------------- #
def test_credentials_prompt_and_urls_never_escape_into_errors() -> None:
    adapter, _recorder = build(
        error_response(400, "prompt flagged: " + PROMPT + " with key " + API_KEY)
    )

    result = adapter.generate(request_for("TEXT_PROMPT"))

    error = failure_error(result)
    text = error.safe_message + error.code
    assert text.strip()
    assert API_KEY not in text
    assert PROMPT not in text


def test_the_authorization_header_is_the_only_credential_on_the_wire() -> None:
    adapter, recorder = build(error_response(401, "Invalid token", type="authentication_error"))

    result = adapter.generate(request_for("TEXT_PROMPT"))

    assert recorder.calls[0].headers["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in repr(sorted(vars(adapter).keys()))
    assert API_KEY not in failure_error(result).safe_message


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "ftp://quanzil.test/v1",
        "https://",
        "https://user:pass@quanzil.test/v1",
        "https://quanzil.test/v1/images?token=abc",
        "https://quanzil.test/v1#frag",
    ],
)
def test_unusable_base_urls_fail_at_assembly(base_url: str) -> None:
    with pytest.raises(ValueError):
        QuanzilImageProvider(base_url=base_url, api_key=API_KEY, model=QUANZIL_MODEL)


@pytest.mark.parametrize("api_key", ["", "   "])
def test_a_missing_key_fails_closed_at_construction(api_key: str) -> None:
    with pytest.raises(ValueError):
        QuanzilImageProvider(base_url=BASE_URL, api_key=api_key, model=QUANZIL_MODEL)


def test_construction_does_not_open_a_connection() -> None:
    """装配时机：`celery -A` 加载 Worker 时不该因为构造适配器就连上供应商。"""

    adapter, recorder = build(lambda _request: image_response())

    assert isinstance(adapter, QuanzilImageProvider)
    assert recorder.count == 0


def test_provider_name_and_model_are_the_verified_pair() -> None:
    assert QUANZIL_PROVIDER_NAME == "QUANZIL"
    assert QUANZIL_MODEL == "gpt-image-2.5-flare"
    assert provider().provider_name == QUANZIL_PROVIDER_NAME
    assert provider().model_name == QUANZIL_MODEL


# --------------------------------------------------------------------------- #
# 工厂装配（唯一 Provider，不做路由）
# --------------------------------------------------------------------------- #
def test_factory_builds_quanzil_only_from_server_side_configuration() -> None:
    made = build_ai_provider(
        ai_provider="quanzil", base_url=BASE_URL, model=QUANZIL_MODEL, api_key=API_KEY
    )

    assert isinstance(made, QuanzilImageProvider)
    assert made.provider_name == QUANZIL_PROVIDER_NAME


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "", "model": QUANZIL_MODEL, "api_key": API_KEY},
        {"base_url": BASE_URL, "model": "", "api_key": API_KEY},
        {"base_url": BASE_URL, "model": QUANZIL_MODEL, "api_key": ""},
    ],
)
def test_factory_refuses_to_run_quanzil_with_incomplete_configuration(
    kwargs: dict[str, str],
) -> None:
    """缺任一项都必须响亮失败：绝不静默退回 Mock，让「没调用供应商」看起来像成功。"""

    with pytest.raises(RuntimeError):
        build_ai_provider(ai_provider="quanzil", **kwargs)


@pytest.mark.parametrize("ai_provider", ["openai", "dashscope", "stability", "", " mock2 "])
def test_factory_still_rejects_every_unselected_provider(ai_provider: str) -> None:
    with pytest.raises(RuntimeError):
        build_ai_provider(ai_provider=ai_provider)


def test_scenario_selection_stays_mock_only() -> None:
    """Mock 场景参数不得作用于真实适配器（第 6 节：场景只属于假适配器）。"""

    with pytest.raises(RuntimeError):
        build_ai_provider(
            ai_provider="quanzil",
            base_url=BASE_URL,
            model=QUANZIL_MODEL,
            api_key=API_KEY,
            scenario="failure",
        )


def test_generated_result_carries_the_client_request_id_of_the_attempt() -> None:
    """相关性键必须原样回传：attempt 行靠它把一次逻辑重试与外部请求对齐。"""

    adapter, _recorder = build(lambda _request: image_response())

    assert adapter.generate(request_for("TEXT_PROMPT")).client_request_id == "gid-1:1"


# --------------------------------------------------------------------------- #
# 配置同源：任务登记的 provider/model 必须与实际出网的适配器一致
# --------------------------------------------------------------------------- #
def test_provider_identity_uses_the_configured_pair() -> None:
    from app.services.generation.provider_factory import provider_identity

    assert provider_identity(ai_provider="quanzil", ai_model=QUANZIL_MODEL) == (
        "QUANZIL",
        QUANZIL_MODEL,
    )
    # MOCK 允许不配型号：假适配器本来就不认型号
    assert provider_identity(ai_provider="mock", ai_model="") == ("MOCK", "mock-image-v1")


def test_provider_identity_refuses_a_real_provider_without_a_model() -> None:
    """型号留空时不得猜一个默认值：猜错会把真实调用打到未验证的型号上。"""

    from app.services.generation.provider_factory import provider_identity

    with pytest.raises(RuntimeError):
        provider_identity(ai_provider="quanzil", ai_model="  ")


def test_provider_identity_rejects_unselected_providers() -> None:
    from app.services.generation.provider_factory import provider_identity

    with pytest.raises(RuntimeError):
        provider_identity(ai_provider="seedream", ai_model="x")


# --------------------------------------------------------------------------- #
# 2026-09-28 免费探针补到的真实错误形态
# --------------------------------------------------------------------------- #
def _error(status: int, code: str, message: str = "hidden from us", error_type: str = "new_api_error"):
    return lambda _request: httpx.Response(
        status, json={"error": {"message": message, "type": error_type, "code": code}}
    )


def test_model_not_found_is_not_retried_until_the_two_causes_are_distinguishable() -> None:
    """实测：没有可用渠道时中转站回 `404 code=model_not_found`，而不是 5xx。

    但同一个码既可能是渠道临时全断、也可能是型号配错，而 `total_usage` 在真实出图前后
    不变化，「探针增量 0」并不能证明没有外部执行。证据不足时**不自动重试**：
    配错重试不会变好，而本底若已排进队列，重试可能就是重复扣费。
    """

    adapter, _recorder = build(_error(404, "model_not_found"))

    error = failure_error(adapter.generate(request_for("TEXT_PROMPT")))

    assert error.category is ProviderErrorCategory.VALIDATION
    assert error.retryable is False
    assert error.request_accepted is False
    assert error.code == "quanzil_model_not_found"
    assert classify(error).value == "FAILURE"


@pytest.mark.parametrize("hostile_code", [API_KEY, PROMPT, API_KEY[:12] + " " + PROMPT])
def test_an_unknown_provider_code_never_reaches_the_database(hostile_code: str) -> None:
    """回归：`code` 是供应商可控字段，它完全可能正好等于密钥或 Prompt。

    早先的实现是「过滤字符后拼上 quanzil_ 前缀」——那把密钥原样搬进了
    `provider_attempts.error_code`。现在只有白名单码可落地，未知码一律回退成
    固定 HTTP 状态 token，长度也保证不超过 64 列宽。
    """

    adapter, _recorder = build(_error(400, hostile_code, "upstream said something"))

    error = failure_error(adapter.generate(request_for("TEXT_PROMPT")))

    assert error.code == "quanzil_rejected_request_400"
    assert API_KEY not in error.code and PROMPT not in error.code
    assert API_KEY not in error.safe_message and PROMPT not in error.safe_message
    assert len(error.code) <= 64


def test_only_whitelisted_machine_codes_are_mapped() -> None:
    adapter, _recorder = build(_error(400, "local:convert_request_failed"))

    error = failure_error(adapter.generate(request_for("TEXT_PROMPT")))

    assert error.code == "quanzil_convert_request_failed"


def test_known_machine_code_is_mapped_and_the_message_is_dropped() -> None:
    """`invalid_request` 这类已知码留进行里供运维分辨；message 里的 request id 一概不带。"""

    adapter, _recorder = build(
        _error(400, "invalid_request", "prompt is required (request id: 20260928185306788)")
    )

    error = failure_error(adapter.generate(request_for("TEXT_PROMPT")))

    assert error.category is ProviderErrorCategory.VALIDATION
    assert error.code == "quanzil_invalid_request"
    assert "request id" not in error.code and "20260928185306788" not in error.code


def test_wrong_method_shows_up_as_404_not_405_on_this_relay() -> None:
    """实测方法用错也是 404（无 code），归到不可重试的明确失败即可。"""

    adapter, _recorder = build(_error(404, "", "Invalid URL (GET /v1/images/generations)"))

    error = failure_error(adapter.generate(request_for("TEXT_PROMPT")))

    assert error.category is ProviderErrorCategory.VALIDATION
    assert error.code == "quanzil_rejected_request_404"
    assert classify(error).value == "FAILURE"


@pytest.mark.parametrize("output_count", [0, -1])
def test_a_non_positive_count_cannot_be_built_at_all(output_count: int) -> None:
    """实测中转站对 `n=0` 不报错而是照常出图（真实扣费），所以这种数量根本不能出网。

    第一道闸是冻结的请求类型：`GenerateRequest` 自己就拒绝非正数量，
    适配器里那层判断是纵深防御——两者都要在，因为供应商不会替我们挡。
    """

    adapter, recorder = build(lambda _request: image_response(count=1))

    with pytest.raises(ValueError):
        request_for("TEXT_PROMPT", count=output_count)

    assert recorder.count == 0


def test_the_wire_always_carries_a_positive_count() -> None:
    adapter, recorder = build(lambda _request: image_response())

    adapter.generate(request_for("TEXT_PROMPT", count=2))

    assert json.loads(recorder.calls[0].content)["n"] == 2


def test_the_multipart_edit_request_is_the_shape_the_relay_accepted() -> None:
    """逐字段核对真实线上形状：`image` 部分带文件名与 MIME，`n` 与 `model` 是表单字段。

    只断言「子串出现过」是不够的——中转站实测会**静默忽略**它不认识的参数
    （`image_urls`、`optimize_prompt_mode` 都是被接受但不生效），所以表单结构本身
    必须逐项可验证，否则我们会带着一个被忽略的参数以为它在生效。
    """

    source = make_image_input()
    adapter, recorder = build(lambda _request: image_response(count=2))

    result = adapter.generate(request_for("PHOTO_PROMPT", sources=(source,)))

    assert len(result.outputs) == 2
    body = recorder.calls[0].content
    assert b'name="image"; filename="source.png"' in body
    assert b"Content-Type: image/png" in body
    assert source.content in body
    assert b'name="model"\r\n\r\n' + QUANZIL_MODEL.encode() in body
    assert b'name="n"\r\n\r\n2' in body
    assert b'name="prompt"\r\n\r\n' + PROMPT.encode("utf-8") in body
    # 未经核实的扩展参数绝不能出现在请求里（它们被静默忽略过一次）
    for absent in (b"image_urls", b"optimize_prompt_mode", b"size", b"quality"):
        assert absent not in body


def test_markup_bearing_provider_output_survives_the_shared_validator() -> None:
    """厂商出图常在内嵌清单里携带 `<svg` 文本：适配器与共用校验器**都得**认这张合法 PNG。

    终验正是断在这里：`attempt.error_code=forbidden_format` 来自 `validate_image`
    对原始字节的关键字扫描，而不是 ENG-07 的解码。校验器改成按容器结构 + 完整解码
    判定之后，这条链路必须一路走通到 `SUCCEEDED`。
    """

    from app.services.assets.validation import validate_image

    decorated = png_with_markup_in_metadata(size=(8, 8))

    adapter, _recorder = build(
        lambda _request: httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(decorated).decode()}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )
    )

    result = adapter.generate(request_for("PHOTO_PROMPT", sources=(make_image_input(),), count=1))

    assert result.state is ProviderState.SUCCEEDED
    assert result.outputs[0].mime_type == "image/png"
    assert result.outputs[0].content == decorated

    validated = validate_image(
        decorated,
        declared_mime_type=result.outputs[0].mime_type,
        declared_size_bytes=len(decorated),
        declared_sha256=hashlib.sha256(decorated).hexdigest(),
    )

    assert validated.mime_type == "image/png"
    assert validated.size_bytes == len(decorated)
