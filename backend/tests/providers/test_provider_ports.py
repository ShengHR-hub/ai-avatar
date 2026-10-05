"""Provider 内部类型契约测试（ENG-06 第 1 步）。

唯一来源：`docs/architecture/04-ai-provider.md` 第 2 节（类型与不变量）、
第 3 节（超时/重试）、第 5 节（错误分类与默认动作），以及
`docs/task-cards/LEAD-05-provider-and-storage-contract.md`「冻结内容 / Provider」。

本文件只测**契约层**：三态、七类错误分类、`ProviderResult` 不变量、
`ProviderCallError` 与「能否重试 / 是否明确失败 / 必须转 attempt UNKNOWN」的裁决函数。
任务状态机与结算编排属 Generation Service（ENG-06 第 3 步），不在此处断言。
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from types import MappingProxyType

import pytest

from app.providers.ports import (
    AIProvider,
    GenerateRequest,
    ImageInput,
    ProviderCallError,
    ProviderDisposition,
    ProviderError,
    ProviderErrorCategory,
    ProviderOutput,
    ProviderResult,
    ProviderState,
    ProviderUsage,
    RETRYABLE_CATEGORIES,
    classify,
)

CATEGORIES = (
    ProviderErrorCategory.VALIDATION,
    ProviderErrorCategory.AUTHENTICATION,
    ProviderErrorCategory.CONTENT_POLICY,
    ProviderErrorCategory.RATE_LIMIT,
    ProviderErrorCategory.TEMPORARY,
    ProviderErrorCategory.TIMEOUT,
    ProviderErrorCategory.UNKNOWN,
)

#: 第 5 节明确「不重试」的三类
NEVER_RETRIED = (
    ProviderErrorCategory.VALIDATION,
    ProviderErrorCategory.AUTHENTICATION,
    ProviderErrorCategory.CONTENT_POLICY,
)


def make_request(client_request_id: str = "clr-1", output_count: int = 2) -> GenerateRequest:
    """构造一个最小请求（Provider 层不校验产品规则，只校验相关性字段）。"""

    return GenerateRequest(
        mode="TEXT_PROMPT",
        prompt="a deterministic prompt",
        source_images=(),
        reference_images=(),
        style=MappingProxyType({"preset": "studio"}),
        output_count=output_count,
        model="mock-image-v1",
        client_request_id=client_request_id,
    )


def make_result(
    state: ProviderState = ProviderState.SUCCEEDED,
    *,
    provider_request_id: str | None = "mock-req-clr-1",
    error: ProviderError | None = None,
    outputs: tuple[ProviderOutput, ...] = (),
) -> ProviderResult:
    return ProviderResult(
        state=state,
        client_request_id="clr-1",
        provider_request_id=provider_request_id,
        outputs=outputs,
        usage=None,
        error=error,
        retry_after_seconds=None,
        completed_at=None,
    )


def make_error(
    category: ProviderErrorCategory,
    *,
    retryable: bool = True,
    request_accepted: bool | None = False,
) -> ProviderError:
    return ProviderError(
        category=category,
        code=f"mock_{category.value.lower()}",
        safe_message="归一化后的安全描述",
        retryable=retryable,
        request_accepted=request_accepted,
    )


# ------------------------------------------------------------------ ProviderState
def test_provider_state_is_exactly_the_three_frozen_values() -> None:
    """第 2 节：结果只能是 `SUCCEEDED`/`PENDING`/`FAILED`。"""

    assert {state.value for state in ProviderState} == {"SUCCEEDED", "PENDING", "FAILED"}


def test_state_enum_excludes_task_and_attempt_tokens() -> None:
    """`PROCESSING`、`CANCELLED`、`UNKNOWN` 不是 Provider 结果状态。

    它们分别属于任务状态机与 attempt outcome，三套枚举不得混用（第 2 节、第 5 节）。
    """

    names = {state.name for state in ProviderState}
    assert names.isdisjoint({"PROCESSING", "CANCELLED", "UNKNOWN", "NEEDS_REVIEW"})


def test_result_rejects_a_state_outside_the_three() -> None:
    with pytest.raises(ValueError):
        make_result(state=ProviderState("UNKNOWN"))  # type: ignore[arg-type]


# ------------------------------------------------------------------ 错误分类
def test_error_categories_are_the_seven_frozen_values() -> None:
    assert [category.value for category in ProviderErrorCategory] == [
        "VALIDATION",
        "AUTHENTICATION",
        "CONTENT_POLICY",
        "RATE_LIMIT",
        "TEMPORARY",
        "TIMEOUT",
        "UNKNOWN",
    ]


def test_retryable_category_set_is_limited_to_three_families() -> None:
    """第 3 节：只有 `RATE_LIMIT`、`TEMPORARY`、`TIMEOUT` 可能被重试。"""

    assert RETRYABLE_CATEGORIES == frozenset(
        {
            ProviderErrorCategory.RATE_LIMIT,
            ProviderErrorCategory.TEMPORARY,
            ProviderErrorCategory.TIMEOUT,
        }
    )


# ------------------------------------------------------------------ ProviderResult 不变量
def test_pending_must_carry_a_stable_provider_request_id() -> None:
    """第 2 节：`PENDING` 必须带 `provider_request_id`，否则无法轮询/关联回调。"""

    with pytest.raises(ValueError):
        make_result(ProviderState.PENDING, provider_request_id=None)
    with pytest.raises(ValueError):
        make_result(ProviderState.PENDING, provider_request_id="   ")


def test_succeeded_may_have_zero_outputs_at_the_adapter_boundary() -> None:
    """第 2 节：适配器可以返回零输出；转 `NEEDS_REVIEW` 是 Service 的判断，不在此处。"""

    result = make_result(ProviderState.SUCCEEDED, outputs=())

    assert result.state is ProviderState.SUCCEEDED
    assert result.outputs == ()


def test_failed_must_carry_a_normalized_error() -> None:
    with pytest.raises(ValueError):
        make_result(ProviderState.FAILED, error=None)


@pytest.mark.parametrize("state", [ProviderState.SUCCEEDED, ProviderState.PENDING])
def test_only_failed_carries_an_error(state: ProviderState) -> None:
    with pytest.raises(ValueError):
        make_result(state, error=make_error(ProviderErrorCategory.TEMPORARY))


def test_client_request_id_is_required_on_request_and_result() -> None:
    """`client_request_id` 是一次逻辑 attempt 的相关性键（第 3 节）。"""

    with pytest.raises(ValueError):
        make_request(client_request_id="")
    with pytest.raises(ValueError):
        ProviderResult(
            state=ProviderState.SUCCEEDED,
            client_request_id="  ",
            provider_request_id="mock-req",
            outputs=(),
            usage=None,
            error=None,
            retry_after_seconds=None,
            completed_at=None,
        )


@pytest.mark.parametrize("output_count", [0, -1])
def test_output_count_must_be_positive(output_count: int) -> None:
    """只校验正数；V1.0 固定 2 张是 API/产品规则，Provider 层不得硬编码。"""

    with pytest.raises(ValueError):
        make_request(output_count=output_count)


def test_model_is_required_because_attempt_rows_are_not_null() -> None:
    """`provider_attempts.model_name` NOT NULL（`02-database-design.md` 第 3.4 节）。"""

    with pytest.raises(ValueError):
        dataclasses.replace(make_request(), model="")


def test_result_is_frozen_and_fully_specified() -> None:
    result = make_result()

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = ProviderState.FAILED  # type: ignore[misc]
    assert result.client_request_id == "clr-1"


def test_provider_output_is_frozen() -> None:
    output = ProviderOutput(content=b"bytes", mime_type="image/png", provider_output_id="o-1")

    with pytest.raises(dataclasses.FrozenInstanceError):
        output.content = b"other"  # type: ignore[misc]


def test_unknown_usage_stays_none_and_is_never_fabricated_zero() -> None:
    """第 5 节：用量/成本未知写 `NULL`，不得伪造成 0。"""

    usage = ProviderUsage()

    assert (
        usage.input_tokens,
        usage.output_tokens,
        usage.image_count,
        usage.estimated_cost,
        usage.currency,
    ) == (None, None, None, None, None)


def test_usage_keeps_reported_values_without_conversion() -> None:
    usage = ProviderUsage(
        input_tokens=11,
        output_tokens=None,
        image_count=2,
        estimated_cost=Decimal("0.0137"),
        currency="USD",
    )

    assert usage.output_tokens is None
    assert usage.estimated_cost == Decimal("0.0137")


# ------------------------------------------------------------------ ProviderCallError
def test_call_error_carries_only_the_four_frozen_fields() -> None:
    """第 2 节示意：`category`/`code`/`safe_message`/`request_accepted`，没有 `retryable`。"""

    error = ProviderCallError(
        category=ProviderErrorCategory.UNKNOWN,
        code="mock_unresolvable",
        safe_message="无法确认外部状态",
        request_accepted=True,
    )

    assert {field.name for field in dataclasses.fields(error)} == {
        "category",
        "code",
        "safe_message",
        "request_accepted",
    }
    assert isinstance(error, Exception)
    assert str(error) == "无法确认外部状态"


def test_call_error_is_frozen() -> None:
    error = ProviderCallError(
        category=ProviderErrorCategory.TIMEOUT,
        code="mock_timeout",
        safe_message="超时",
        request_accepted=None,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        error.code = "changed"  # type: ignore[misc]


# ------------------------------------------------------------------ classify()
@pytest.mark.parametrize("category", CATEGORIES)
@pytest.mark.parametrize("retryable", [True, False])
@pytest.mark.parametrize("request_accepted", [True, False, None])
def test_classify_is_total_and_mutually_exclusive(
    category: ProviderErrorCategory, retryable: bool, request_accepted: bool | None
) -> None:
    """三种裁决必须互斥且全覆盖：重试 / 明确失败 / attempt UNKNOWN。"""

    dispositions = {
        ProviderDisposition.RETRY,
        ProviderDisposition.FAILURE,
        ProviderDisposition.UNKNOWN_OUTCOME,
    }
    error = make_error(category, retryable=retryable, request_accepted=request_accepted)

    outcome = classify(error)

    assert outcome in dispositions
    assert classify(error) is outcome


@pytest.mark.parametrize("category", NEVER_RETRIED)
@pytest.mark.parametrize("retryable", [True, False])
def test_never_retried_categories_are_never_retried(
    category: ProviderErrorCategory, retryable: bool
) -> None:
    """第 5 节：`VALIDATION`/`AUTHENTICATION`/`CONTENT_POLICY` 不重试，适配器标记也无效。"""

    assert (
        classify(make_error(category, retryable=retryable, request_accepted=False))
        is not ProviderDisposition.RETRY
    )


@pytest.mark.parametrize(
    "category",
    [ProviderErrorCategory.RATE_LIMIT, ProviderErrorCategory.TEMPORARY],
)
def test_throttled_and_transient_are_retried_only_with_provable_non_acceptance(
    category: ProviderErrorCategory,
) -> None:
    """第 5 节：「只在可证明安全时重试」= `request_accepted is False` 且适配器允许重试。"""

    assert (
        classify(make_error(category, retryable=True, request_accepted=False))
        is ProviderDisposition.RETRY
    )
    assert (
        classify(make_error(category, retryable=True, request_accepted=True))
        is ProviderDisposition.UNKNOWN_OUTCOME
    )
    assert (
        classify(make_error(category, retryable=True, request_accepted=None))
        is ProviderDisposition.UNKNOWN_OUTCOME
    )
    assert (
        classify(make_error(category, retryable=False, request_accepted=False))
        is ProviderDisposition.FAILURE
    )


def test_timeout_before_acceptance_is_retriable() -> None:
    """第 3 节：提交前超时，且有「未接受、无外部执行」的可靠证据时才可重试。"""

    assert (
        classify(
            make_error(ProviderErrorCategory.TIMEOUT, retryable=True, request_accepted=False)
        )
        is ProviderDisposition.RETRY
    )


@pytest.mark.parametrize("request_accepted", [True, None])
def test_timeout_after_acceptance_never_retries_and_forces_unknown(
    request_accepted: bool | None,
) -> None:
    """接受后超时/接受状态不明 → attempt `UNKNOWN`，永不自动重试（第 3 节）。"""

    error = make_error(
        ProviderErrorCategory.TIMEOUT, retryable=True, request_accepted=request_accepted
    )

    assert classify(error) is ProviderDisposition.UNKNOWN_OUTCOME


@pytest.mark.parametrize("retryable", [True, False])
@pytest.mark.parametrize("request_accepted", [True, False, None])
def test_unknown_category_never_blind_retries(
    retryable: bool, request_accepted: bool | None
) -> None:
    """第 5 节 `UNKNOWN`：不得盲目重试，必须转 attempt UNKNOWN 交人工复核。"""

    error = make_error(
        ProviderErrorCategory.UNKNOWN, retryable=retryable, request_accepted=request_accepted
    )

    assert classify(error) is ProviderDisposition.UNKNOWN_OUTCOME


@pytest.mark.parametrize("category", NEVER_RETRIED)
def test_definite_failure_needs_explicit_non_acceptance_evidence(
    category: ProviderErrorCategory,
) -> None:
    """只有「明确未接受」才是可放款的明确失败；接受状态不明必须保守转 UNKNOWN。"""

    assert (
        classify(make_error(category, retryable=False, request_accepted=False))
        is ProviderDisposition.FAILURE
    )
    assert (
        classify(make_error(category, retryable=False, request_accepted=None))
        is ProviderDisposition.UNKNOWN_OUTCOME
    )


@pytest.mark.parametrize("category", CATEGORIES)
@pytest.mark.parametrize("request_accepted", [True, False, None])
def test_call_error_retries_only_pre_acceptance_timeout(
    category: ProviderErrorCategory, request_accepted: bool | None
) -> None:
    """`ProviderCallError` 无法产出可信结果：仅「提交前超时」可重试，其余一律 UNKNOWN。"""

    error = ProviderCallError(
        category=category,
        code=f"mock_{category.value.lower()}",
        safe_message="无法产出可信归一结果",
        request_accepted=request_accepted,
    )
    expected = (
        ProviderDisposition.RETRY
        if category is ProviderErrorCategory.TIMEOUT and request_accepted is False
        else ProviderDisposition.UNKNOWN_OUTCOME
    )

    assert classify(error) is expected


# ------------------------------------------------------------------ AIProvider 协议
class _CompleteProvider:
    def generate(self, request: GenerateRequest) -> ProviderResult:
        return make_result()

    def check_status(self, provider_request_id: str) -> ProviderResult:
        return make_result()

    def cancel(self, provider_request_id: str) -> ProviderResult:
        return make_result()


class _IncompleteProvider:
    def generate(self, request: GenerateRequest) -> ProviderResult:
        return make_result()


def test_ai_provider_protocol_requires_all_three_operations() -> None:
    """第 2 节：`generate` / `check_status` / `cancel` 三方法齐备才算适配器。"""

    assert isinstance(_CompleteProvider(), AIProvider)
    assert not isinstance(_IncompleteProvider(), AIProvider)


def test_image_input_shape_matches_frozen_contract() -> None:
    image = ImageInput(mime_type="image/png", content=b"bytes", sha256="0" * 64)

    assert {field.name for field in dataclasses.fields(image)} == {
        "mime_type",
        "content",
        "sha256",
    }


def test_generate_request_field_names_are_frozen() -> None:
    """第 2 节示意的字段集合，改名即契约变化。"""

    assert {field.name for field in dataclasses.fields(GenerateRequest)} == {
        "mode",
        "prompt",
        "source_images",
        "reference_images",
        "style",
        "output_count",
        "model",
        "client_request_id",
    }
