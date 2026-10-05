"""Quanzil **图片理解**适配器测试（「参考图片找灵感」）。

只使用 `httpx.MockTransport`：**不发起任何真实网络请求，不读取任何真实凭据**。

要证明的四件事：

1. 出网的是 `POST {base}/chat/completions`，带视觉型号、`max_tokens`、
   data URL 图片与固定指令；
2. 明确拒绝（400/401/402/403/404/422/429）= 可证明未产生外部执行；
   不可确认（读超时、5xx、结果无法解析或为空）= `request_accepted=None`，
   由 `classify` 判成 `UNKNOWN_OUTCOME`——**绝不重试**，重试等于二次付费；
3. 供应商原文、原始 code/message 与凭据一律不进 `code`/`safe_message`/异常文本；
4. 图根本解码不了时**在出网之前**就拒绝。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import httpx
import pytest

from app.providers.ports import (
    DESCRIPTION_MAX_CHARS,
    ImageDescriptionRequest,
    ImageInput,
    ProviderCallError,
    ProviderDisposition,
    ProviderErrorCategory,
    classify,
)
from app.providers.quanzil_vision import (
    CHAT_COMPLETIONS_PATH,
    DESCRIBE_INSTRUCTION,
    VISION_READ_TIMEOUT_SECONDS,
    QuanzilVisionAnalyzer,
)
from tests.services.fakes import synthetic_image_bytes, synthetic_png_bytes

API_KEY = "synthetic-relay-key-never-log"
BASE_URL = "https://quanzil.test/v1"
VISION_MODEL = "synthetic-vision-model"
DESCRIPTION = "一个人背对镜头站在草地上，远处是起伏的山脊，天空占了大半画面，整体偏青绿色调。"


def make_request(*, content: bytes | None = None, mime: str = "image/png") -> ImageDescriptionRequest:
    payload = content if content is not None else synthetic_png_bytes()
    return ImageDescriptionRequest(
        image=ImageInput(mime_type=mime, content=payload, sha256="a" * 64),
        language="zh",
        max_output_tokens=300,
    )


def make_analyzer(handler: Callable[[httpx.Request], httpx.Response]) -> QuanzilVisionAnalyzer:
    return QuanzilVisionAnalyzer(
        base_url=BASE_URL,
        api_key=API_KEY,
        model=VISION_MODEL,
        transport=httpx.MockTransport(handler),
    )


def completion(text: str = DESCRIPTION, **extra: object) -> httpx.Response:
    body = {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 60, "total_tokens": 1260},
    }
    body.update(extra)
    return httpx.Response(200, json=body)


# ------------------------------------------------------------------ 请求形状
def test_posts_to_chat_completions_with_the_vision_model() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return completion()

    make_analyzer(handler).describe_image(make_request())

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path.endswith(CHAT_COMPLETIONS_PATH)
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"

    payload = json.loads(request.content)
    assert payload["model"] == VISION_MODEL
    assert payload["max_tokens"] == 300
    parts = payload["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": DESCRIBE_INSTRUCTION}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_instruction_forbids_reconstructing_the_original_prompt() -> None:
    """产品口径写进了指令本身：只描述看到的画面，不猜提示词、不抄图里的字。"""

    assert "不要猜测这张图是用什么提示词生成的" in DESCRIBE_INSTRUCTION
    assert "不要照抄其中的文字内容" in DESCRIBE_INSTRUCTION
    assert "不要写人名、品牌名或任何可识别的身份信息" in DESCRIBE_INSTRUCTION


@pytest.mark.parametrize(
    ("fmt", "mime"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_data_url_uses_the_sniffed_mime_not_the_declared_one(fmt: str, mime: str) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content)["messages"][0]["content"][1]["image_url"]["url"])
        return completion()

    request = make_request(content=synthetic_image_bytes(fmt=fmt, size=(6, 6)), mime="image/png")
    make_analyzer(handler).describe_image(request)

    assert captured[0].startswith(f"data:{mime};base64,")


def test_undecodable_image_is_refused_before_any_network_call() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(str(request.url))
        return completion()

    analyzer = make_analyzer(handler)

    with pytest.raises(ProviderCallError) as excinfo:
        analyzer.describe_image(make_request(content=b"\x89PNG\r\n\x1a\n nope"))

    assert excinfo.value.category is ProviderErrorCategory.VALIDATION
    assert excinfo.value.request_accepted is False
    assert calls == []


def test_classify_is_not_the_arbiter_for_the_analysis_port() -> None:
    """`classify` 服务的是**生成** attempt 的放行/复核裁决；分析端口只有一个失败通道
    （抛 `ProviderCallError`），且适配器从不自行重试。这里把两者分开写清楚，
    免得后来人以为「明确拒绝」也能被 classify 成 FAILURE 去放款。"""

    rejection = ProviderCallError(
        category=ProviderErrorCategory.VALIDATION,
        code="synthetic",
        safe_message="合成",
        request_accepted=False,
    )
    pre_send_timeout = ProviderCallError(
        category=ProviderErrorCategory.TIMEOUT,
        code="synthetic",
        safe_message="合成",
        request_accepted=False,
    )

    assert classify(rejection) is ProviderDisposition.UNKNOWN_OUTCOME
    assert classify(pre_send_timeout) is ProviderDisposition.RETRY


# ------------------------------------------------------------------ 成功归一
def test_success_returns_description_model_and_reported_usage() -> None:
    result = make_analyzer(lambda request: completion()).describe_image(make_request())

    assert result.description == DESCRIPTION
    assert result.model == VISION_MODEL
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1200, 60)
    # 未知成本保持 None，绝不伪造成 0
    assert result.usage.estimated_cost is None
    assert result.usage.currency is None


def test_usage_absent_from_the_body_stays_none() -> None:
    body = {"choices": [{"message": {"content": DESCRIPTION}}]}
    result = make_analyzer(lambda request: httpx.Response(200, json=body)).describe_image(
        make_request()
    )

    assert result.usage is None


def test_content_split_into_text_blocks_is_joined() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "一只猫蹲在海边岩石上，"},
                        {"type": "text", "text": "海是深蓝的。"},
                    ]
                }
            }
        ]
    }

    result = make_analyzer(lambda request: httpx.Response(200, json=body)).describe_image(
        make_request()
    )

    assert result.description == "一只猫蹲在海边岩石上，海是深蓝的。"


def test_overlong_description_is_truncated_to_the_port_cap() -> None:
    result = make_analyzer(
        lambda request: completion("猫" * (DESCRIPTION_MAX_CHARS + 50))
    ).describe_image(make_request())

    assert len(result.description) == DESCRIPTION_MAX_CHARS
    assert result.description.endswith("…")


# ------------------------------------------------------------------ 失败分类
@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, ProviderErrorCategory.VALIDATION),
        (404, ProviderErrorCategory.VALIDATION),
        (422, ProviderErrorCategory.VALIDATION),
        (401, ProviderErrorCategory.AUTHENTICATION),
        (402, ProviderErrorCategory.AUTHENTICATION),
        (403, ProviderErrorCategory.AUTHENTICATION),
        (429, ProviderErrorCategory.RATE_LIMIT),
        (500, ProviderErrorCategory.TEMPORARY),
        (503, ProviderErrorCategory.TEMPORARY),
        (302, ProviderErrorCategory.UNKNOWN),
    ],
)
def test_http_statuses_map_onto_the_frozen_categories(status: int, category) -> None:
    analyzer = make_analyzer(lambda request: httpx.Response(status, json={"error": {"message": "x"}}))

    with pytest.raises(ProviderCallError) as excinfo:
        analyzer.describe_image(make_request())

    assert excinfo.value.category is category


def test_provable_rejections_assert_that_nothing_was_accepted() -> None:
    """400/401/403/429 = 供应商在受理前就拒了：适配器把 `request_accepted` 断言为 `False`。

    分析链路不据此放款（它本来就不占点数），但这个断言必须存在：
    一旦哪天把分析接进账务，「可证明未执行」与「不可确认」的区分就是唯一的依据。
    """

    for status in (400, 401, 403, 429):
        analyzer = make_analyzer(lambda request: httpx.Response(status, json={}))
        with pytest.raises(ProviderCallError) as excinfo:
            analyzer.describe_image(make_request())
        assert excinfo.value.request_accepted is False, status


def test_server_side_and_unparseable_results_are_never_claimed_unaccepted() -> None:
    """5xx / 非法 JSON / 空描述：钱可能已经花出去了，只能保持不可确认。"""

    cases = [
        httpx.Response(502, json={}),
        httpx.Response(200, text="not json at all"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]}),
    ]
    for index, canned in enumerate(cases):
        analyzer = make_analyzer(lambda request, _c=canned: _c)
        with pytest.raises(ProviderCallError) as excinfo:
            analyzer.describe_image(make_request())
        assert excinfo.value.request_accepted is None, index
        assert classify(excinfo.value) is ProviderDisposition.UNKNOWN_OUTCOME, index


def test_read_timeout_after_send_is_unconfirmable_and_never_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(ProviderCallError) as excinfo:
        make_analyzer(handler).describe_image(make_request())

    assert excinfo.value.category is ProviderErrorCategory.TIMEOUT
    assert excinfo.value.request_accepted is None
    assert classify(excinfo.value) is ProviderDisposition.UNKNOWN_OUTCOME


def test_connect_failure_is_provably_not_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    with pytest.raises(ProviderCallError) as excinfo:
        make_analyzer(handler).describe_image(make_request())

    assert excinfo.value.category is ProviderErrorCategory.TIMEOUT
    assert excinfo.value.request_accepted is False


def test_vision_read_timeout_is_shorter_than_the_generation_one() -> None:
    """看图比出图快得多；45s 不回就按不可确认处理，别把用户吊在一条可能已付费的请求上。"""

    assert VISION_READ_TIMEOUT_SECONDS == 45.0


# ------------------------------------------------------------------ 脱敏
def test_provider_error_body_never_leaks_its_own_text() -> None:
    """中转站的原文里可能带 key 片段、内部 URL 或别人的内容：只分类，不透传。"""

    secret = "sk-SUPER-SECRET-MESSAGE-with-a-url-https://leak.invalid/x"
    body = {"error": {"message": secret, "code": "internal_trace_993"}}

    with pytest.raises(ProviderCallError) as excinfo:
        make_analyzer(lambda request: httpx.Response(200, json=body)).describe_image(
            make_request()
        )

    error = excinfo.value
    blob = " ".join([error.code, error.safe_message, str(error)])
    assert secret not in blob
    assert "internal_trace_993" not in blob
    assert API_KEY not in blob


@pytest.mark.parametrize(
    "marker",
    ["content policy", "moderation flagged", "safety_violation", "prohibited content"],
)
def test_content_policy_rejections_are_classified_by_stable_marks(marker: str) -> None:
    body = {"error": {"message": f"Your input tripped {marker}."}}

    with pytest.raises(ProviderCallError) as excinfo:
        make_analyzer(lambda request: httpx.Response(200, json=body)).describe_image(
            make_request()
        )

    assert excinfo.value.category is ProviderErrorCategory.CONTENT_POLICY
    assert marker.lower() not in excinfo.value.safe_message


def test_api_key_never_appears_in_repr_or_failure_text() -> None:
    """`repr` 与所有失败文本都不得带出凭据（异常会被日志与错误处理器搬运）。"""

    analyzer = QuanzilVisionAnalyzer(base_url=BASE_URL, api_key=API_KEY, model=VISION_MODEL)

    assert API_KEY not in repr(analyzer)
    assert API_KEY not in str(analyzer)

    failing = make_analyzer(lambda request: httpx.Response(500, json={}))
    with pytest.raises(ProviderCallError) as excinfo:
        failing.describe_image(make_request())
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in excinfo.value.safe_message


def test_error_codes_are_stable_short_tokens_not_provider_text() -> None:
    """错误码将来要落库/落日志：固定 token、按类别复用，绝不是拼出来的原文。"""

    by_status: dict[int, str] = {}
    for status in (400, 404, 401, 429, 500, 302):
        analyzer = make_analyzer(lambda request: httpx.Response(status, json={}))
        with pytest.raises(ProviderCallError) as excinfo:
            analyzer.describe_image(make_request())
        by_status[status] = excinfo.value.code

    for status, code in by_status.items():
        assert re.fullmatch(r"quanzil_vision_[a-z_]{1,48}", code), (status, code)

    # 同一状态永远得到同一个码（可枚举、可告警），不同类别之间才不同
    again = make_analyzer(lambda request: httpx.Response(400, json={}))
    with pytest.raises(ProviderCallError) as excinfo:
        again.describe_image(make_request())
    assert excinfo.value.code == by_status[400] == by_status[404]
