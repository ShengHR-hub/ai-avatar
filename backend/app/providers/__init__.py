"""AI Provider 适配层（ENG-06）。

- `ports`：`AIProvider` 协议、归一类型、错误分类与「重试/明确失败/attempt UNKNOWN」裁决；
- `mock`：`MockProvider`，第 6 节七个冻结场景的确定性假适配器，**禁止生产**；
- `quanzil`：`QuanzilImageProvider`，ENG-07 经 LEAD-05 复核后接入的唯一真实适配器
  （同步出图、无任务查询、无取消、无 Webhook）。

业务 Service、API 与 Worker 只依赖本包的公开类型，不得导入任何供应商 SDK；真实出网
只发生在适配器内部。适配器按 `AI_PROVIDER` 装配，V1.0 不做多模型路由。
"""

from __future__ import annotations

from app.providers.mock import (
    MOCK_IMAGE_MIME,
    MOCK_MODEL,
    MOCK_PROVIDER_NAME,
    MockProvider,
    MockScenario,
)
from app.providers.quanzil import (
    QUANZIL_MODEL,
    QUANZIL_PROVIDER_NAME,
    QuanzilImageProvider,
)
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

__all__ = [
    "AIProvider",
    "QUANZIL_MODEL",
    "QUANZIL_PROVIDER_NAME",
    "QuanzilImageProvider",
    "GenerateRequest",
    "ImageInput",
    "MOCK_IMAGE_MIME",
    "MOCK_MODEL",
    "MOCK_PROVIDER_NAME",
    "MockProvider",
    "MockScenario",
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
