"""`AIProvider` 端口与归一类型（ENG-06 第 1 步）。

唯一来源：`docs/architecture/04-ai-provider.md` 第 2 节（内部类型与不变量）、
第 3 节（ID/超时/重试）、第 5 节（错误分类与默认动作），以及
`docs/task-cards/LEAD-05-provider-and-storage-contract.md`「冻结内容 / Provider」。

边界纪律：

- Generation Service、API、Worker 只依赖本模块，**不得导入任何供应商 SDK/HTTP 栈**；
- 跨边界的结果已经归一：`FAILED` 只携带 `ProviderError`（安全消息，不含 Prompt、
  凭据、原始请求/响应或供应商对象存储 URL）；
- 本模块**只提供 ENG-06 第 1 步需要的类型与裁决规则**：超时/退避的具体秒数、
  恢复扫描周期与 attempt 行落库都由调用方所在步骤同批落地，此处不预留空壳。

冻结的三态与另外两套枚举不得混用（第 2 节、第 5 节）：

```text
ProviderState      = SUCCEEDED | PENDING | FAILED        （Provider 结果）
Generation.status  = CREATED | QUEUED | PROCESSING | SUCCEEDED | FAILED | NEEDS_REVIEW
attempt.status     = STARTED | SUBMITTED | SUCCEEDED | FAILED | TIMEOUT | UNKNOWN
```

`UNKNOWN` 只是 attempt outcome，**不是第四种 Provider 状态**；无法确认时由
Generation Service 转 `NEEDS_REVIEW` 并保持 `RESERVED`。

已知契约含糊点（已上报待裁决，不在本模块自行定型）：第 2 节正文写「时间均为 UTC
aware datetime」，而同一节的类型示意把 `ProviderResult.completed_at` 写成 `str | None`。
本模块按**字面示意**保留 `str | None` 且不解释其格式，`MockProvider` 一律置 `None`
（时间戳由 Service 按自身时钟写入 attempt 行）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Protocol, Sequence, runtime_checkable

__all__ = [
    "AIProvider",
    "DESCRIPTION_MAX_CHARS",
    "GenerateRequest",
    "ImageAnalyzer",
    "ImageDescriptionRequest",
    "ImageDescriptionResult",
    "ImageInput",
    "ProviderCallError",
    "ProviderDisposition",
    "ProviderError",
    "ProviderErrorCategory",
    "ProviderOutput",
    "ProviderResult",
    "ProviderState",
    "ProviderUsage",
    "RETRYABLE_CATEGORIES",
    "classify",
]


class ProviderState(StrEnum):
    """Provider 结果三态（第 2 节）。"""

    SUCCEEDED = "SUCCEEDED"
    PENDING = "PENDING"
    FAILED = "FAILED"


class ProviderErrorCategory(StrEnum):
    """归一化错误分类（第 5 节表）。"""

    VALIDATION = "VALIDATION"
    AUTHENTICATION = "AUTHENTICATION"
    CONTENT_POLICY = "CONTENT_POLICY"
    RATE_LIMIT = "RATE_LIMIT"
    TEMPORARY = "TEMPORARY"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class ProviderDisposition(StrEnum):
    """一次失败可观察到的三种裁决，互斥且全覆盖（第 3、5 节）。"""

    RETRY = "RETRY"
    FAILURE = "FAILURE"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


#: 第 3 节：只有这三类可能被重试；其余类别即使适配器标记 `retryable` 也不得重试
RETRYABLE_CATEGORIES = frozenset(
    {
        ProviderErrorCategory.RATE_LIMIT,
        ProviderErrorCategory.TEMPORARY,
        ProviderErrorCategory.TIMEOUT,
    }
)


def _require_text(value: object, name: str) -> str:
    """相关性/错误标识字段必须是可用文本，空串会让 attempt 行无法关联。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串。")
    return value


@dataclass(frozen=True, slots=True)
class ImageInput:
    """服务端已经校验过的输入图（适配器内部临时数据，不得进入日志或响应）。"""

    mime_type: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    """一次逻辑 Provider attempt 的归一请求（第 2 节）。"""

    mode: str
    prompt: str
    source_images: Sequence[ImageInput]
    reference_images: Sequence[ImageInput]
    style: Mapping[str, str] | None
    output_count: int
    model: str
    client_request_id: str

    def __post_init__(self) -> None:
        _require_text(self.client_request_id, "client_request_id")
        _require_text(self.model, "model")
        # 只校验正数：V1.0 固定 2 张是 API/产品规则（`03-api-contract.md` 第 4.2 节），
        # Provider 契约层不得把产品数量写死
        if self.output_count <= 0:
            raise ValueError("output_count 必须为正整数。")


@dataclass(frozen=True, slots=True)
class ProviderOutput:
    """一张输出图的字节与其外部标识（Provider URL 不得跨越边界，第 2 节）。"""

    content: bytes
    mime_type: str
    provider_output_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """供应商上报或内部定价的用量与成本。

    第 5 节：未上报或未知必须保持 `None`（落库为 `NULL`），**不得伪造成 0**。
    成本只用于内部核算，绝不参与用户点数计算。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    image_count: int | None = None
    estimated_cost: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderError:
    """`FAILED` 结果的归一错误（第 2、5 节）。

    `safe_message`/`code` 只允许稳定、可展示的安全文本；`request_accepted` 是
    「请求是否可能已被供应商接受」的适配器断言，`None` 表示无法确认。
    """

    category: ProviderErrorCategory
    code: str
    safe_message: str
    retryable: bool
    request_accepted: bool | None

    def __post_init__(self) -> None:
        _require_category(self.category)
        _require_text(self.code, "code")
        _require_text(self.safe_message, "safe_message")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable 必须是 bool。")


@dataclass(frozen=True)
class ProviderCallError(Exception):
    """适配器无法产出可信归一结果（第 2 节）。

    与 `ProviderError` 的分工：`FAILED` 是**明确的**供应商失败；无法确认外部状态、
    接受状态不明、响应不完整或无法解析时抛出本异常，由 `classify` 判定是
    「可证明未接受的提交前超时」还是「必须转 attempt `UNKNOWN`」。
    本类型没有 `retryable` 字段（示意契约如此），是否安全重试由分类规则决定。
    """

    category: ProviderErrorCategory
    code: str
    safe_message: str
    request_accepted: bool | None

    def __post_init__(self) -> None:
        _require_category(self.category)
        _require_text(self.code, "code")
        _require_text(self.safe_message, "safe_message")

    def __str__(self) -> str:
        return self.safe_message


def _require_category(category: object) -> None:
    if not isinstance(category, ProviderErrorCategory):
        raise ValueError(f"category 必须是 ProviderErrorCategory，收到 {category!r}。")


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """归一结果（第 2 节）。"""

    state: ProviderState
    client_request_id: str
    provider_request_id: str | None
    outputs: Sequence[ProviderOutput] = ()
    usage: ProviderUsage | None = None
    error: ProviderError | None = None
    retry_after_seconds: int | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.client_request_id, "client_request_id")

        if not isinstance(self.state, ProviderState):
            raise ValueError(f"state 必须是 ProviderState，收到 {self.state!r}。")
        if self.state is ProviderState.FAILED:
            # 「FAILED 携带归一 ProviderError」是硬约束，否则 attempt 行无法归一
            if not isinstance(self.error, ProviderError):
                raise ValueError("FAILED 结果必须携带归一化的 ProviderError。")
        elif self.error is not None:
            raise ValueError(f"{self.state.value} 结果不得携带 error。")

        # PENDING 必须能关联到外部请求，否则轮询与回调都无法落地（第 2 节）
        if self.state is ProviderState.PENDING:
            _require_text(self.provider_request_id, "provider_request_id")


def classify(error: ProviderError | ProviderCallError) -> ProviderDisposition:
    """把一次失败归到「可安全重试 / 明确失败 / 必须 attempt UNKNOWN」。

    规则完全来自第 3、5 节，Provider 层只判定语义、不执行重试：

    1. `ProviderCallError` 说明结果不可信，只有「提交前超时且可证明未接受」可重试，
       其余一律 `UNKNOWN_OUTCOME`（接受后超时、连接中断、响应不完整、状态不可解析）；
    2. 重试要求三者同时成立：类别属于 `RATE_LIMIT/TEMPORARY/TIMEOUT`、
       适配器 `retryable=True`、`request_accepted is False`；
    3. 明确失败要求类别不是 `UNKNOWN` 且 `request_accepted is False`
       （只有能证明没有外部执行，才允许按 LEAD-04 放款）；
    4. 两条都不满足就是 `UNKNOWN_OUTCOME`：保持 `RESERVED`，转人工复核。
    """

    category = error.category
    accepted = error.request_accepted

    if isinstance(error, ProviderCallError):
        if category is ProviderErrorCategory.TIMEOUT and accepted is False:
            return ProviderDisposition.RETRY
        return ProviderDisposition.UNKNOWN_OUTCOME

    if error.retryable and category in RETRYABLE_CATEGORIES and accepted is False:
        return ProviderDisposition.RETRY
    if category is not ProviderErrorCategory.UNKNOWN and accepted is False:
        return ProviderDisposition.FAILURE
    return ProviderDisposition.UNKNOWN_OUTCOME


@runtime_checkable
class AIProvider(Protocol):
    """供应商适配边界（第 2 节）。

    V1.0 固定单一 Provider，不实现多模型自动路由；重试也不得换 provider/model
    （第 3 节）。真实能力（同步/异步、状态查询、取消、Webhook 验签、计价）
    必须等 ENG-07 选定供应商后验证。
    """

    def generate(self, request: GenerateRequest) -> ProviderResult:
        """提交生成请求，返回即时状态或带 `provider_request_id` 的 `PENDING`。"""
        ...

    def check_status(self, provider_request_id: str) -> ProviderResult:
        """按外部请求 ID 轮询状态；与 Webhook 归一到同一条对账路径。"""
        ...

    def cancel(self, provider_request_id: str) -> ProviderResult:
        """尽力且幂等取消；接受取消不是取消成功的证明（第 2 节）。"""
        ...


# ---------------------------------------------------------------- 图片描述端口
#: 描述文本的长度上限：界面按一段话展示，过长既没人读也会放大成本
DESCRIPTION_MAX_CHARS = 600


@dataclass(frozen=True, slots=True)
class ImageDescriptionRequest:
    """一次「看图写描述」的归一请求。

    这是一条**独立的**能力：它不产生图片，也不进入生成任务状态机。
    输入只有一张已通过服务端校验的图片，输出只有一段文字。
    """

    image: ImageInput
    #: 期望的输出语言（BCP-47 的粗粒度取值，当前只用到 `zh`）。
    language: str = "zh"
    #: 输出 token 上限：成本控制的第一道闸门，适配器必须透传给供应商。
    max_output_tokens: int = 300

    def __post_init__(self) -> None:
        _require_text(self.language, "language")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens 必须为正整数。")


@dataclass(frozen=True, slots=True)
class ImageDescriptionResult:
    """描述结果。

    `description` 是**近似画面描述**，不是原作者的原始提示词；适配器不得声称后者。
    """

    description: str
    model: str
    usage: ProviderUsage | None = None

    def __post_init__(self) -> None:
        _require_text(self.description, "description")
        _require_text(self.model, "model")
        if len(self.description) > DESCRIPTION_MAX_CHARS:
            raise ValueError("description 超过长度上限。")


@runtime_checkable
class ImageAnalyzer(Protocol):
    """图片理解边界（与 `AIProvider` 分开）。

    分成两个端口的原因：出图与看图是两种能力、两个型号、两套计价，
    混进 `AIProvider` 会让"参考图分析"看起来像一条生成路径。
    失败一律抛 `ProviderCallError`，由调用方归一，绝不返回半成品文本。
    """

    def describe_image(self, request: ImageDescriptionRequest) -> ImageDescriptionResult:
        """读图并返回一段近似画面描述。"""
        ...
