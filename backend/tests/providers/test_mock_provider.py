"""`MockProvider` 七类场景测试（ENG-06 第 1 步）。

唯一来源：`docs/architecture/04-ai-provider.md` 第 6 节场景表与
`docs/task-cards/LEAD-05-provider-and-storage-contract.md`「冻结内容 / Provider」第 19 条。

纪律：
- 场景只由服务端构造参数选择，**不得由公开用户输入选择**；
- 全部 fake ID 与图片内容确定，无真实网络、凭据或可计费调用；
- 只断言可观察结果（状态、相关性 ID、输出、用量、错误归一与裁决），不断言实现相关耗时；
- 输出必须能被 ENG-05 的通用图片校验器接受（同一张图片校验路径）。
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.providers import MockProvider, MockScenario
from app.providers.mock import MOCK_MODEL, MOCK_PROVIDER_NAME
from app.providers.ports import (
    AIProvider,
    GenerateRequest,
    ProviderCallError,
    ProviderDisposition,
    ProviderErrorCategory,
    ProviderResult,
    ProviderState,
    classify,
)
from app.services.assets.validation import validate_image

PROVIDER_PACKAGE_DIR = Path(__file__).resolve().parents[2] / "app" / "providers"

#: 第 6 节场景表的全部 7 个场景名（`LEAD-05` 审核门禁第 2 条同为 7 类）
FROZEN_SCENARIOS = {
    "success",
    "delayed",
    "failure",
    "timeout",
    "unknown",
    "success_without_image",
    "duplicate_webhook",
}


def make_provider(scenario: MockScenario = MockScenario.SUCCESS, **kwargs: object) -> MockProvider:
    return MockProvider(scenario, **kwargs)  # type: ignore[arg-type]


def generate(
    provider: MockProvider, output_count: int = 2, client_request_id: str = "clr-1"
) -> ProviderResult:
    return provider.generate(
        GenerateRequest(
            mode="TEXT_PROMPT",
            prompt="a deterministic prompt",
            source_images=(),
            reference_images=(),
            style=None,
            output_count=output_count,
            model=MOCK_MODEL,
            client_request_id=client_request_id,
        )
    )


# ------------------------------------------------------------------ 场景集合与默认值
def test_scenario_enum_is_exactly_the_seven_frozen_scenarios() -> None:
    assert {scenario.value for scenario in MockScenario} == FROZEN_SCENARIOS


def test_default_scenario_is_success() -> None:
    """第 6 节：默认模式是 `success`。"""

    provider = MockProvider()

    assert provider.scenario is MockScenario.SUCCESS
    assert generate(provider).state is ProviderState.SUCCEEDED


def test_mock_provider_satisfies_the_ai_provider_protocol() -> None:
    assert isinstance(make_provider(), AIProvider)


@pytest.mark.parametrize("scenario", list(MockScenario))
def test_scenario_is_not_selectable_per_call(scenario: MockScenario) -> None:
    """第 6 节：场景只由服务端配置/测试夹具选择，调用签名不得带场景开关。"""

    provider = make_provider(scenario)

    assert list(inspect.signature(provider.generate).parameters) == ["request"]
    assert list(inspect.signature(provider.check_status).parameters) == ["provider_request_id"]
    assert list(inspect.signature(provider.cancel).parameters) == ["provider_request_id"]


#: ENG-07 起，被允许出网的适配器模块只有两个，且只能用仓库已声明的 `httpx`
#: （不是供应商 SDK）：出图 `quanzil.py`、看图 `quanzil_vision.py`。
#: 它们是同一个中转站的两种能力、两个型号，但共用同一条传输边界——
#: 「出网只发生在适配器内部」这条不变量对两者一视同仁。
#: 端口（`ports.py`）与假适配器仍在禁令之内，供应商 SDK 更是绝对禁止。
TRANSPORT_CAPABLE_PROVIDER_MODULES = frozenset({"quanzil.py", "quanzil_vision.py"})


def test_no_network_or_sdk_imports_cross_the_adapter_boundary() -> None:
    """第 1 节：业务与适配器边界不得导入供应商 SDK、未声明的 HTTP 客户端或真实网络栈。"""

    forbidden = (
        "httpx",
        "requests",
        "aiohttp",
        "urllib.request",
        "socket",
        "subprocess",
        "openai",
        "minio",
        "boto3",
    )
    offenders: list[str] = []

    for module in sorted(PROVIDER_PACKAGE_DIR.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        banned = set(forbidden)
        if module.name in TRANSPORT_CAPABLE_PROVIDER_MODULES:
            banned.discard("httpx")
        offenders.extend(f"{module.name}: {token}" for token in sorted(banned) if token in source)

    assert offenders == [], offenders


def test_the_real_adapter_stays_on_the_declared_http_client() -> None:
    """正向确认：出网模块只依赖 httpx，不碰任何供应商 SDK 或原始套接字。"""

    for module in sorted(TRANSPORT_CAPABLE_PROVIDER_MODULES):
        source = (PROVIDER_PACKAGE_DIR / module).read_text(encoding="utf-8")

        assert "import httpx" in source, module
        for token in ("openai", "minio", "boto3", "import socket", "subprocess", "requests"):
            assert token not in source, f"{module}: {token}"


# ------------------------------------------------------------------ success
def test_success_returns_exactly_requested_count_of_valid_images() -> None:
    """第 6 节：`generate()` 返回恰好请求数量的确定性光栅图 + 用量/成本夹具。"""

    result = generate(make_provider(), output_count=2)

    assert result.state is ProviderState.SUCCEEDED
    assert result.error is None
    assert len(result.outputs) == 2
    assert [output.provider_output_id for output in result.outputs] == [
        "mock-out-clr-1-0",
        "mock-out-clr-1-1",
    ]


@pytest.mark.parametrize("index", [0, 1])
def test_success_outputs_pass_the_common_image_validator(index: int) -> None:
    """输出必须走 ENG-05 同一套图片校验，不允许只给 URL 或占位字节。"""

    result = generate(make_provider(), output_count=2)
    output = result.outputs[index]

    validated = validate_image(
        output.content,
        declared_mime_type=output.mime_type,
        declared_size_bytes=len(output.content),
        declared_sha256=hashlib.sha256(output.content).hexdigest(),
    )

    assert validated.mime_type == "image/png"
    assert validated.width > 0 and validated.height > 0


def test_success_images_are_deterministic_across_instances() -> None:
    first = generate(MockProvider(), output_count=2).outputs
    second = generate(MockProvider(), output_count=2).outputs

    assert [item.content for item in first] == [item.content for item in second]
    assert len({item.content for item in first}) == 1


def test_success_reports_usage_without_fabricating_values() -> None:
    usage = generate(make_provider(), output_count=2).usage

    assert usage is not None
    assert usage.image_count == 2
    assert isinstance(usage.estimated_cost, Decimal)
    assert usage.currency == "USD"
    assert usage.input_tokens is not None


def test_result_echoes_client_request_id_and_exposes_provider_request_id() -> None:
    """第 3 节：两个 ID 分工明确，`provider_request_id` 用于状态/回调关联。"""

    result = generate(make_provider(), client_request_id="clr-42")

    assert result.client_request_id == "clr-42"
    assert result.provider_request_id
    assert result.provider_request_id != "clr-42"
    assert generate(make_provider(), client_request_id="clr-42").provider_request_id == (
        result.provider_request_id
    )
    assert generate(make_provider(), client_request_id="clr-43").provider_request_id != (
        result.provider_request_id
    )


def test_status_check_of_a_succeeded_request_stays_succeeded() -> None:
    provider = make_provider()
    submitted = generate(provider)

    assert provider.check_status(submitted.provider_request_id).state is ProviderState.SUCCEEDED


# ------------------------------------------------------------------ delayed
def test_delayed_is_pending_then_succeeded_by_poll_count() -> None:
    """第 6 节：首次 `generate()` 返回带稳定假 ID 的 `PENDING`，前 N 次轮询仍 PENDING 后成功。"""

    provider = make_provider(MockScenario.DELAYED, pending_polls=2)
    submitted = generate(provider)

    assert submitted.state is ProviderState.PENDING
    assert submitted.outputs == ()
    provider_request_id = submitted.provider_request_id
    assert [
        provider.check_status(provider_request_id).state for _ in range(2)
    ] == [ProviderState.PENDING, ProviderState.PENDING]

    settled = provider.check_status(provider_request_id)
    assert settled.state is ProviderState.SUCCEEDED
    assert settled.client_request_id == "clr-1"
    assert len(settled.outputs) == 2


def test_delayed_polling_is_stable_after_success() -> None:
    provider = make_provider(MockScenario.DELAYED, pending_polls=1)
    provider_request_id = generate(provider).provider_request_id

    provider.check_status(provider_request_id)
    first = provider.check_status(provider_request_id)
    second = provider.check_status(provider_request_id)

    assert first.state is ProviderState.SUCCEEDED
    assert second == first


def test_sleeper_is_controllable_and_default_never_waits() -> None:
    """第 6 节：可控 sleeper；默认不真的等待（耗时不是被测契约）。"""

    waited: list[float] = []
    delayed = make_provider(
        MockScenario.DELAYED, pending_polls=2, sleeper=waited.append, poll_delay_seconds=0.5
    )
    provider_request_id = generate(delayed).provider_request_id
    delayed.check_status(provider_request_id)
    delayed.check_status(provider_request_id)
    assert waited == [0.5, 0.5]

    default: list[float] = []
    plain = make_provider(MockScenario.DELAYED, pending_polls=1, sleeper=default.append)
    plain.check_status(generate(plain).provider_request_id)
    assert default == [0.0]


# ------------------------------------------------------------------ failure
def test_failure_is_normalized_failed_with_default_temporary_known_not_accepted() -> None:
    """第 6 节：默认 `TEMPORARY` 且「已知未接受」，可作为明确失败放款一次。"""

    result = generate(make_provider(MockScenario.FAILURE))

    assert result.state is ProviderState.FAILED
    assert result.outputs == ()
    assert result.error is not None
    assert result.error.category is ProviderErrorCategory.TEMPORARY
    assert result.error.request_accepted is False
    assert classify(result.error) is ProviderDisposition.FAILURE


def test_failure_can_be_marked_safely_retryable() -> None:
    result = generate(
        make_provider(
            MockScenario.FAILURE,
            failure_category=ProviderErrorCategory.RATE_LIMIT,
            failure_retryable=True,
            retry_after_seconds=7,
        )
    )

    assert result.error is not None
    assert result.error.retryable is True
    assert result.retry_after_seconds == 7
    assert classify(result.error) is ProviderDisposition.RETRY


def test_failure_category_and_code_are_configurable() -> None:
    result = generate(
        make_provider(
            MockScenario.FAILURE,
            failure_category=ProviderErrorCategory.CONTENT_POLICY,
            failure_code="mock_policy_refusal",
        )
    )

    assert result.error is not None
    assert result.error.category is ProviderErrorCategory.CONTENT_POLICY
    assert result.error.code == "mock_policy_refusal"
    assert classify(result.error) is ProviderDisposition.FAILURE


def test_failure_error_message_never_leaks_prompt_or_secret() -> None:
    """第 2/5 节：`FAILED` 的归一消息不得带原始请求体、凭据或 Prompt。"""

    provider = make_provider(MockScenario.FAILURE)
    result = generate(provider, client_request_id="clr-secret-token")
    text = f"{result.error.safe_message}{result.error.code}"

    assert "a deterministic prompt" not in text
    assert "secret" not in text


# ------------------------------------------------------------------ timeout
def test_timeout_before_acceptance_raises_retriable_call_error() -> None:
    """第 3 节：提交前超时且能证明未执行时才可重试。"""

    provider = make_provider(MockScenario.TIMEOUT, timeout_accepted=False)

    with pytest.raises(ProviderCallError) as excinfo:
        generate(provider)

    error = excinfo.value
    assert error.category is ProviderErrorCategory.TIMEOUT
    assert error.request_accepted is False
    assert classify(error) is ProviderDisposition.RETRY


def test_timeout_after_acceptance_forces_unknown_review_without_retry() -> None:
    """第 3 节：接受后超时 → attempt `UNKNOWN`，任务 `NEEDS_REVIEW + RESERVED`，不自动重试。"""

    provider = make_provider(MockScenario.TIMEOUT, timeout_accepted=True)

    with pytest.raises(ProviderCallError) as excinfo:
        generate(provider)

    assert excinfo.value.category is ProviderErrorCategory.TIMEOUT
    assert excinfo.value.request_accepted is True
    assert classify(excinfo.value) is ProviderDisposition.UNKNOWN_OUTCOME


def test_timeout_uses_the_same_operation_surface_as_other_scenarios() -> None:
    """两个超时变体都由同一 `generate()` 入口产生，不新增旁路方法。"""

    for accepted in (True, False):
        provider = make_provider(MockScenario.TIMEOUT, timeout_accepted=accepted)

        with pytest.raises(ProviderCallError):
            generate(provider)


# ------------------------------------------------------------------ unknown
def test_unknown_is_accepted_then_unresolvable_without_false_terminal_assertion() -> None:
    """第 6 节：请求已被接受但到期无法解析状态/结果，且不得伪造终态。"""

    provider = make_provider(MockScenario.UNKNOWN)
    submitted = generate(provider)

    assert submitted.state is ProviderState.PENDING
    assert submitted.provider_request_id

    with pytest.raises(ProviderCallError) as excinfo:
        provider.check_status(submitted.provider_request_id)

    assert excinfo.value.category is ProviderErrorCategory.UNKNOWN
    assert excinfo.value.request_accepted is True
    assert classify(excinfo.value) is ProviderDisposition.UNKNOWN_OUTCOME


def test_unknown_scenario_cancel_does_not_invent_a_terminal_state() -> None:
    provider = make_provider(MockScenario.UNKNOWN)
    submitted = generate(provider)

    with pytest.raises(ProviderCallError):
        provider.cancel(submitted.provider_request_id)


# ------------------------------------------------------------------ success_without_image
def test_success_without_image_returns_succeeded_with_no_outputs() -> None:
    """第 2 节边界 + 第 6 节场景：适配器可以返回零输出，转 `NEEDS_REVIEW` 由 Service 决定。"""

    result = generate(make_provider(MockScenario.SUCCESS_WITHOUT_IMAGE))

    assert result.state is ProviderState.SUCCEEDED
    assert result.outputs == ()
    assert result.error is None


def test_success_without_image_still_reports_usage_as_provided() -> None:
    usage = generate(make_provider(MockScenario.SUCCESS_WITHOUT_IMAGE)).usage

    assert usage is not None
    assert usage.image_count == 0


# ------------------------------------------------------------------ duplicate_webhook
def test_duplicate_webhook_repeats_the_identical_terminal_event() -> None:
    """第 6 节：同一有效终态回调至少重复两次，两次内容必须完全一致。"""

    provider = make_provider(MockScenario.DUPLICATE_WEBHOOK)
    submitted = generate(provider)
    first = provider.next_webhook_event(submitted.provider_request_id)
    second = provider.next_webhook_event(submitted.provider_request_id)

    assert first.state is ProviderState.SUCCEEDED
    assert second == first
    assert [output.content for output in first.outputs] == [
        output.content for output in second.outputs
    ]


def test_late_pending_webhook_cannot_be_promoted_by_the_authoritative_source() -> None:
    """第 4 节：终态单调；迟到的 `PENDING` 事件存在，但轮询权威源仍是终态。"""

    provider = make_provider(MockScenario.DUPLICATE_WEBHOOK)
    submitted = generate(provider)
    provider.next_webhook_event(submitted.provider_request_id)
    provider.next_webhook_event(submitted.provider_request_id)

    late = provider.next_webhook_event(submitted.provider_request_id)
    assert late.state is ProviderState.PENDING
    assert late.provider_request_id == submitted.provider_request_id

    authoritative = provider.check_status(submitted.provider_request_id)
    assert authoritative.state is ProviderState.SUCCEEDED


def test_webhook_events_after_the_script_are_safe_duplicates() -> None:
    provider = make_provider(MockScenario.DUPLICATE_WEBHOOK)
    submitted = generate(provider)
    terminal = provider.next_webhook_event(submitted.provider_request_id)

    for _ in range(3):
        provider.next_webhook_event(submitted.provider_request_id)

    assert provider.next_webhook_event(submitted.provider_request_id) == terminal


def test_webhook_event_for_an_unrelated_id_is_rejected_not_invented() -> None:
    """第 4 节第 3 条：只按已存在的 `provider_request_id` 关联，未知 ID 必须拒绝。"""

    provider = make_provider(MockScenario.DUPLICATE_WEBHOOK)
    generate(provider, client_request_id="clr-known")

    with pytest.raises(ProviderCallError) as excinfo:
        provider.next_webhook_event("mock-req-clr-unknown")

    assert excinfo.value.request_accepted is None
    assert classify(excinfo.value) is ProviderDisposition.UNKNOWN_OUTCOME


# ------------------------------------------------------------------ cancel 与身份
def test_cancel_is_idempotent_and_only_reports_observed_outcome() -> None:
    """第 2 节：`cancel()` 尽力且幂等，接受不等于取消成功。"""

    provider = make_provider()
    submitted = generate(provider)

    first = provider.cancel(submitted.provider_request_id)
    second = provider.cancel(submitted.provider_request_id)

    assert first.state is ProviderState.SUCCEEDED
    assert second == first


def test_cancel_of_a_pending_request_still_reports_pending() -> None:
    provider = make_provider(MockScenario.DELAYED, pending_polls=5)
    submitted = generate(provider)

    assert provider.cancel(submitted.provider_request_id).state is ProviderState.PENDING


def test_mock_identity_is_fixed_single_provider_and_model() -> None:
    """第 1 节：V1.0 固定单一 Provider，不实现多模型路由；重试也不得换 provider/model。"""

    provider = make_provider()

    assert provider.provider_name == MOCK_PROVIDER_NAME
    assert generate(provider, client_request_id="clr-x").client_request_id == "clr-x"


def test_two_requests_never_share_a_provider_request_id() -> None:
    provider = make_provider()

    assert generate(provider, client_request_id="clr-a").provider_request_id != generate(
        provider, client_request_id="clr-b"
    ).provider_request_id


def test_check_status_of_an_unseen_id_raises_call_error() -> None:
    provider = make_provider()

    with pytest.raises(ProviderCallError):
        provider.check_status("mock-req-never-submitted")


def test_results_are_immutable_and_fully_typed() -> None:
    result: ProviderResult = generate(make_provider())

    with pytest.raises(FrozenInstanceError):
        result.state = ProviderState.FAILED  # type: ignore[misc]
