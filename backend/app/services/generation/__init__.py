"""Generation Service 领域包（ENG-06）。

- `state`：冻结的任务状态机与合法转换表（唯一裁决处）；
- `commands` / `ports`：Service 的入参形状与出站端口；
- `costs` / `idempotency`：成本快照与写请求幂等判定；
- `service`：用户侧编排（创建、重试、读取、内部取消）；
- `orchestrator`：Worker 与回调侧编排（领取、事务外调用、同事务落结果与结算）；
- `webhook` / `webhook_adapters`：回调验签、防重放与归一；
- `outputs`：结果图的持久化（与客户端上传同一套校验）；
- `mappers`：DB 行到契约 DTO。

依赖方向：本包可以依赖 Points Service、Repository、`StorageAdapter` 与
`app.providers` 的**内部契约**；不得导入任何供应商 SDK 或对象存储驱动
（`04-ai-provider.md` 第 1 节、`09-repository-boundary.md` 第 4 节）。
"""

from __future__ import annotations

from app.services.generation.commands import NewGenerationCommand
from app.services.generation.costs import ResolvedCost, resolve_cost
from app.services.generation.idempotency import fingerprint_of, replay_decision
from app.services.generation.mappers import GenerationOutputView, to_api_generation
from app.services.generation.orchestrator import GenerationOrchestrator
from app.services.generation.ports import (
    GenerationQueuePort,
    ProviderFactory,
    ReplayGuard,
    UowFactory,
    WebhookAuthenticator,
    WebhookEvent,
)
from app.services.generation.provider_factory import build_ai_provider
from app.services.generation.service import GenerationOutcome, GenerationService, Page
from app.services.generation.state import (
    ALL_STATUSES,
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    assert_transition,
)
from app.services.generation.webhook import WebhookIngress

__all__ = [
    "ALL_STATUSES",
    "GenerationOrchestrator",
    "GenerationOutcome",
    "GenerationOutputView",
    "GenerationQueuePort",
    "GenerationService",
    "LEGAL_TRANSITIONS",
    "NewGenerationCommand",
    "Page",
    "ProviderFactory",
    "ResolvedCost",
    "ReplayGuard",
    "TERMINAL_STATUSES",
    "UowFactory",
    "WebhookAuthenticator",
    "WebhookEvent",
    "WebhookIngress",
    "assert_transition",
    "build_ai_provider",
    "fingerprint_of",
    "replay_decision",
    "resolve_cost",
    "to_api_generation",
]
