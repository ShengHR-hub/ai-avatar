"""`MockProvider`：七个冻结场景的确定性假适配器（ENG-06 第 1 步）。

唯一来源：`docs/architecture/04-ai-provider.md` 第 6 节场景表与
`docs/task-cards/LEAD-05-provider-and-storage-contract.md`「冻结内容 / Provider」第 19 条。

纪律：

- 场景只由服务端构造参数或测试夹具选择，**调用签名不带场景开关**，公开用户输入无法选择；
- 全部 ID、图片字节与用量确定；无网络、无凭据、无可计费调用；
- 等待用可控 `sleeper` 注入，默认不阻塞——被测的是可观察结果，不是耗时；
- 输出是真能被 ENG-05 通用图片校验器接受的光栅图，保证与未来真实适配器走
  同一条「校验 → 存储 → 结算」路径；
- 时间戳（`completed_at`）一律留空：时刻由 Generation Service 按自身时钟写入
  attempt 行，Provider 层不伪造完成时间。

`timeout` 场景用 `timeout_accepted` 区分第 6 节要求的「提交前」与「接受后」两个变体，
`unknown` 场景模拟「已接受但到期无法解析」，两者都不产出假的终态。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum

from app.providers.ports import (
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
    "MOCK_IMAGE_MIME",
    "MOCK_MODEL",
    "MOCK_PROVIDER_NAME",
    "MockProvider",
    "MockScenario",
]

#: V1.0 固定单一 Provider；本常量不是「已选定供应商」的声明（ENG-07 才选定）
MOCK_PROVIDER_NAME = "MOCK"
MOCK_MODEL = "mock-image-v1"
MOCK_IMAGE_MIME = "image/png"

#: 确定性的 4x4 RGB PNG（76 字节）。同一 `client_request_id` 永远得到同一批字节
_MOCK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGMUUDBggAEmOAsvBwATpABojuFRbgAAAABJRU5ErkJggg=="
)

#: 假用量：`output_tokens` 故意留 `None`，示范「未上报即 NULL，绝不伪造成 0」（第 5 节）
_MOCK_INPUT_TOKENS = 16
_MOCK_ESTIMATED_COST = Decimal("0.0137")
_MOCK_CURRENCY = "USD"

#: `duplicate_webhook` 脚本里「迟到 PENDING」所在的位置（第 6 节：终态之后可选迟到事件）
_LATE_PENDING_EVENT_INDEX = 2


class MockScenario(StrEnum):
    """第 6 节冻结的七个场景。"""

    SUCCESS = "success"
    DELAYED = "delayed"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
    SUCCESS_WITHOUT_IMAGE = "success_without_image"
    DUPLICATE_WEBHOOK = "duplicate_webhook"


def noop_sleeper(seconds: float) -> None:
    """默认 sleeper：不等待，`seconds` 只为满足可注入签名。"""

    return None


class MockProvider:
    """实现 `AIProvider` 的确定性假适配器。"""

    provider_name = MOCK_PROVIDER_NAME

    def __init__(
        self,
        scenario: MockScenario | str = MockScenario.SUCCESS,
        *,
        sleeper: Callable[[float], None] = noop_sleeper,
        poll_delay_seconds: float = 0.0,
        pending_polls: int = 1,
        timeout_accepted: bool = False,
        failure_category: ProviderErrorCategory | str = ProviderErrorCategory.TEMPORARY,
        failure_code: str = "mock_rejected",
        failure_safe_message: str = "供应商明确拒绝该请求",
        failure_retryable: bool = False,
        failure_request_accepted: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        self._scenario = MockScenario(scenario)
        self._sleeper = sleeper
        self._poll_delay_seconds = poll_delay_seconds
        self._pending_polls = pending_polls
        self._timeout_accepted = timeout_accepted
        self._failure_category = ProviderErrorCategory(failure_category)
        self._failure_code = failure_code
        self._failure_safe_message = failure_safe_message
        self._failure_retryable = failure_retryable
        self._failure_request_accepted = failure_request_accepted
        self._retry_after_seconds = retry_after_seconds
        self._attempts: dict[str, GenerateRequest] = {}
        self._poll_counts: dict[str, int] = {}
        self._webhook_counts: dict[str, int] = {}

    @property
    def scenario(self) -> MockScenario:
        """只读：场景在构造期由服务端配置固定，之后不可变。"""

        return self._scenario

    # -------------------------------------------------------------- AIProvider
    def generate(self, request: GenerateRequest) -> ProviderResult:
        """按场景提交；`timeout` 抛 `ProviderCallError`，`unknown` 返回可关联的 `PENDING`。"""

        if self._scenario is MockScenario.TIMEOUT:
            # 超时不留外部 ID：`provider_attempts.request_id` 在提交确认前必须为 NULL（第 5 节）
            raise self._timeout_error()

        provider_request_id = self._register(request)

        if self._scenario is MockScenario.FAILURE:
            return self._failed_result(request.client_request_id, provider_request_id)
        if self._scenario in (MockScenario.DELAYED, MockScenario.UNKNOWN):
            return self._pending_result(provider_request_id)
        return self._terminal_result(provider_request_id)

    def check_status(self, provider_request_id: str) -> ProviderResult:
        """轮询权威源：`delayed` 按配置次数保持 `PENDING`，终态一旦达成不再回退。"""

        self._require_attempt(provider_request_id)
        self._raise_if_unresolvable()

        if self._is_still_pending(provider_request_id):
            self._poll_counts[provider_request_id] = (
                self._poll_counts.get(provider_request_id, 0) + 1
            )
            self._sleeper(self._poll_delay_seconds)
            return self._pending_result(provider_request_id)

        return self._terminal_result(provider_request_id)

    def cancel(self, provider_request_id: str) -> ProviderResult:
        """尽力且幂等：只回报观察到的结果，接受取消不等于取消成功（第 2 节）。

        与 `check_status` 的唯一区别是不推进延迟轮询计数——取消不是一次状态轮询。
        V1.0 没有公开取消 API，本方法只服务内部 Service/Worker 路径。
        """

        self._require_attempt(provider_request_id)
        self._raise_if_unresolvable()

        if self._is_still_pending(provider_request_id):
            return self._pending_result(provider_request_id)
        return self._terminal_result(provider_request_id)

    # -------------------------------------------------------------- Webhook
    def next_webhook_event(self, provider_request_id: str) -> ProviderResult:
        """按第 6 节脚本产回调事件：同一终态两次，随后一个迟到 `PENDING`，再往后恒为终态。

        事件只是**不可信传输**，权威状态永远来自 `check_status`；
        未登记的外部 ID 必须拒绝而不是凭空造一个任务（第 4 节第 3 条）。
        """

        if self._scenario is not MockScenario.DUPLICATE_WEBHOOK:
            raise ValueError("next_webhook_event 只服务于 duplicate_webhook 场景。")

        self._require_attempt(provider_request_id)

        index = self._webhook_counts.get(provider_request_id, 0)
        self._webhook_counts[provider_request_id] = index + 1

        if index == _LATE_PENDING_EVENT_INDEX:
            return self._pending_result(provider_request_id)
        return self._terminal_result(provider_request_id)

    # -------------------------------------------------------------- 内部构造
    def _register(self, request: GenerateRequest) -> str:
        """按 `client_request_id` 派生稳定的假外部 ID，并登记这次逻辑 attempt。"""

        provider_request_id = f"mock-req-{request.client_request_id}"
        self._attempts[provider_request_id] = request
        return provider_request_id

    def _require_attempt(self, provider_request_id: str) -> GenerateRequest:
        """只按已登记的外部 ID 关联，未知 ID 一律拒绝而不是凭空造任务（第 4 节第 3 条）。"""

        request = self._attempts.get(provider_request_id)
        if request is None:
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="mock_request_unlinkable",
                safe_message="外部请求 ID 无法关联到已知 attempt。",
                request_accepted=None,
            )
        return request

    def _raise_if_unresolvable(self) -> None:
        """`unknown` 场景：已接受但到期无法解析状态或结果，且不断言假终态。"""

        if self._scenario is MockScenario.UNKNOWN:
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="mock_status_unresolvable",
                safe_message="请求已被接受，但到期仍无法解析状态或结果。",
                request_accepted=True,
            )

    def _is_still_pending(self, provider_request_id: str) -> bool:
        return (
            self._scenario is MockScenario.DELAYED
            and self._poll_counts.get(provider_request_id, 0) < self._pending_polls
        )

    def _timeout_error(self) -> ProviderCallError:
        """两个超时变体（第 3 节）：提交前可证明未接受，接受后一律按不可确认处理。"""

        if self._timeout_accepted:
            return ProviderCallError(
                category=ProviderErrorCategory.TIMEOUT,
                code="mock_timeout_after_acceptance",
                safe_message="请求已接受后超时，外部执行状态无法确认。",
                request_accepted=True,
            )
        return ProviderCallError(
            category=ProviderErrorCategory.TIMEOUT,
            code="mock_timeout_before_acceptance",
            safe_message="提交前超时，可证明供应商未接受请求且没有外部执行。",
            request_accepted=False,
        )

    def _pending_result(self, provider_request_id: str) -> ProviderResult:
        return ProviderResult(
            state=ProviderState.PENDING,
            client_request_id=self._require_attempt(provider_request_id).client_request_id,
            provider_request_id=provider_request_id,
            outputs=(),
            usage=None,
            error=None,
            retry_after_seconds=self._retry_after_seconds,
            completed_at=None,
        )

    def _failed_result(self, client_request_id: str, provider_request_id: str) -> ProviderResult:
        return ProviderResult(
            state=ProviderState.FAILED,
            client_request_id=client_request_id,
            provider_request_id=provider_request_id,
            outputs=(),
            usage=None,
            error=ProviderError(
                category=self._failure_category,
                code=self._failure_code,
                # 安全消息只含归一文本：绝不带 Prompt、图片字节、凭据或原始响应
                safe_message=self._failure_safe_message,
                retryable=self._failure_retryable,
                request_accepted=self._failure_request_accepted,
            ),
            retry_after_seconds=self._retry_after_seconds,
            completed_at=None,
        )

    def _terminal_result(self, provider_request_id: str) -> ProviderResult:
        request = self._require_attempt(provider_request_id)

        if self._scenario is MockScenario.FAILURE:
            return self._failed_result(request.client_request_id, provider_request_id)

        outputs = (
            ()
            if self._scenario is MockScenario.SUCCESS_WITHOUT_IMAGE
            else tuple(
                ProviderOutput(
                    content=_MOCK_PNG,
                    mime_type=MOCK_IMAGE_MIME,
                    provider_output_id=f"mock-out-{request.client_request_id}-{index}",
                )
                for index in range(request.output_count)
            )
        )

        return ProviderResult(
            state=ProviderState.SUCCEEDED,
            client_request_id=request.client_request_id,
            provider_request_id=provider_request_id,
            outputs=outputs,
            usage=ProviderUsage(
                input_tokens=_MOCK_INPUT_TOKENS,
                output_tokens=None,
                image_count=len(outputs),
                estimated_cost=_MOCK_ESTIMATED_COST,
                currency=_MOCK_CURRENCY,
            ),
            error=None,
            retry_after_seconds=self._retry_after_seconds,
            completed_at=None,
        )
