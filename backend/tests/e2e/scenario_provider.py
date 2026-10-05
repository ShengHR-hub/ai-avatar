"""E2E 场景适配器：让「明确失败」与「UNKNOWN」两条结算矩阵能在真实进程里跑通。

## 为什么需要它（以及它不是什么）

`MockProvider` 的七个冻结场景（`04-ai-provider.md` 第 6 节）在真实进程里**没有**
选择入口 —— `app/tasks/worker.py::build_orchestrator()` 固定构造
`build_ai_provider(ai_provider="mock")`，即 `MockScenario.SUCCESS`。这是刻意的：
场景绝不允许由公开用户输入决定，也不应该有部署方可达的开关。

ENG-10 要验证「明确失败释放」和「UNKNOWN 保留预留」这两条结算矩阵。它们由
`tests/e2e/scenario_runner.py` 在**独立进程**里驱动真实的
`app.tasks.worker.run_generation` 完成，本模块只提供：

- `UnknownScenarioProvider` 与 `patch_provider_factory(scenario)`：**只替换
  `app.services.generation.provider_factory.build_ai_provider` 一个函数**；
- `prefetch_unknown_outputs()`：给 UNKNOWN 场景准备真实的确定性图片字节。

其余全部真实：`GenerationOrchestrator`、`PointsService`、SQLAlchemy 工作单元、
PostgreSQL、MinIO 的签名 PUT 与读取。

## 与真实 Worker 进程的分工（报告里必须如实区分）

| 场景 | 投递方式 | 说明 |
|---|---|---|
| `success` | **真实 Celery Worker 容器消费真实 Redis 队列** | 浏览器 E2E 的主链路 |
| `failure` / `unknown` | **独立进程**调用同一个已注册任务函数 | 仍走真实编排、真实账务、真实存储；但**不是** broker 投递 |

这样区分是因为场景选择本身不该进入生产配置面。报告里不会把后者写成
「Worker 容器验收」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tests.e2e.images import synthetic_png

__all__ = [
    "SCENARIO_PREFIX",
    "UnknownScenarioProvider",
    "patch_provider_factory",
    "prefetch_unknown_outputs",
    "scenario_of_prompt",
]

#: 场景标记前缀（只出现在 E2E 合成数据里，任何产品代码都不读它）
SCENARIO_PREFIX = "[eng10-scenario:"

_SCENARIO_OUT = Path(os.environ.get("ENG10_SCENARIO_OUT", "D:/tmp/eng10-run/scenarios"))


def scenario_of_prompt(prompt: str) -> str:
    """从 prompt 前缀解析场景名；没有标记就是默认的 `success`。"""

    text = (prompt or "").lstrip()
    if not text.startswith(SCENARIO_PREFIX):
        return "success"
    rest = text[len(SCENARIO_PREFIX) :]
    name, _, _ = rest.partition("]")
    return name.strip() or "success"


def prefetch_unknown_outputs() -> int:
    """兼容保留：`unknown` 场景**不再需要**预生成字节（它不产图片）。

    留着这个函数只是为了让 `scenario_runner --prefetch` 与既有调用点继续可用。
    """

    return 0


class UnknownScenarioProvider:
    """`unknown` 场景：提交被接受（PENDING），之后状态**永远无法解析**。

    **不产出图片字节**：`PENDING` 的 outputs 从来没有被持久化的机会，给它们字节只会
    让这条路径看起来「有产物」。UNKNOWN 的成因是「**已接受但无法解析**」——
    由 `check_status` 抛 `request_accepted=True` 的 `ProviderCallError` 表达，
    结算上必须保持 `RESERVED` 并转 `NEEDS_REVIEW`。
    """

    provider_name = "MOCK"

    def __init__(self) -> None:
        self._attempts: dict[str, int] = {}

    def generate(self, request):  # noqa: ANN001, ANN201 - 签名由 AIProvider 端口决定
        from app.providers.ports import ProviderResult, ProviderState

        provider_request_id = f"mock-req-{request.client_request_id}"
        self._attempts[provider_request_id] = request.output_count
        return ProviderResult(
            state=ProviderState.PENDING,
            client_request_id=request.client_request_id,
            provider_request_id=provider_request_id,
            outputs=(),
            usage=None,
            error=None,
            retry_after_seconds=None,
            completed_at=None,
        )

    def check_status(self, provider_request_id: str):  # noqa: ANN201
        from app.providers.ports import ProviderCallError, ProviderErrorCategory

        if provider_request_id not in self._attempts:
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="mock_request_unlinkable",
                safe_message="外部请求 ID 无法关联到已知 attempt。",
                request_accepted=None,
            )
        raise ProviderCallError(
            category=ProviderErrorCategory.UNKNOWN,
            code="mock_status_unresolvable",
            safe_message="请求已被接受，但到期仍无法解析状态或结果。",
            request_accepted=True,
        )

    def cancel(self, provider_request_id: str):  # noqa: ANN201
        return self.check_status(provider_request_id)

    def next_webhook_event(self, provider_request_id: str):  # noqa: ANN201
        raise ValueError("unknown 场景不产回调脚本。")


class DuplicateWebhookScenarioProvider:
    """`duplicate_webhook` 场景：同一终态可被观察到多次，随后一个迟到 `PENDING`。

    冻结脚本（`04-ai-provider.md` 第 6 节）是「终态两次 + 一个迟到 PENDING」。
    这里每次 `check_status` 都产一个新事件，因此对账入口会被**真实地重复驱动**：
    结算只允许发生一次（终态单调 + attempt 行锁），迟到 PENDING 也不得重开终态。
    """

    provider_name = "MOCK"

    def __init__(self) -> None:
        self._attempts: dict[str, object] = {}
        self._events: dict[str, int] = {}

    def generate(self, request):  # noqa: ANN001, ANN201
        provider_request_id = f"mock-req-{request.client_request_id}"
        self._attempts[provider_request_id] = request
        self._events[provider_request_id] = 1
        return self._terminal(request, provider_request_id)

    def check_status(self, provider_request_id: str):  # noqa: ANN201
        return self._event(provider_request_id)

    def cancel(self, provider_request_id: str):  # noqa: ANN201
        return self._event(provider_request_id)

    def next_webhook_event(self, provider_request_id: str):  # noqa: ANN201
        return self._event(provider_request_id)

    def _event(self, provider_request_id: str):  # noqa: ANN201
        from app.providers.ports import (
            ProviderCallError,
            ProviderErrorCategory,
            ProviderResult,
            ProviderState,
        )

        request = self._attempts.get(provider_request_id)
        if request is None:
            raise ProviderCallError(
                category=ProviderErrorCategory.UNKNOWN,
                code="mock_request_unlinkable",
                safe_message="外部请求 ID 无法关联到已知 attempt。",
                request_accepted=None,
            )

        index = self._events.get(provider_request_id, 0) + 1
        self._events[provider_request_id] = index
        if index == 3:
            # 迟到 PENDING：终态之后的事件绝不能把已终结的 attempt 重开
            return ProviderResult(
                state=ProviderState.PENDING,
                client_request_id=request.client_request_id,  # type: ignore[attr-defined]
                provider_request_id=provider_request_id,
                outputs=(),
                usage=None,
                error=None,
                retry_after_seconds=None,
                completed_at=None,
            )
        return self._terminal(request, provider_request_id)  # type: ignore[arg-type]

    def _terminal(self, request, provider_request_id: str):  # noqa: ANN001, ANN201
        from app.providers.mock import _MOCK_INPUT_TOKENS, MOCK_IMAGE_MIME, _MOCK_PNG
        from app.providers.ports import (
            ProviderOutput,
            ProviderResult,
            ProviderState,
            ProviderUsage,
        )

        outputs = tuple(
            ProviderOutput(
                content=_MOCK_PNG,
                mime_type=MOCK_IMAGE_MIME,
                provider_output_id=f"mock-out-{request.client_request_id}-{index}",
            )
            for index in range(request.output_count)
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
                estimated_cost=None,
                currency=None,
            ),
            error=None,
            retry_after_seconds=None,
            completed_at=None,
        )


def patch_provider_factory(scenario: str) -> None:
    """把 `build_ai_provider` 换成固定场景的测试实现（只替换这一个函数）。"""

    from app.providers.mock import MockProvider, MockScenario
    from app.services.generation import provider_factory

    original = provider_factory.build_ai_provider

    def _scenario_provider(**kwargs: object) -> object:
        ai_provider = str(kwargs.get("ai_provider", "mock"))
        if ai_provider.strip().upper() != "MOCK" or scenario in ("", "success"):
            return original(**kwargs)  # type: ignore[arg-type]
        if scenario == "unknown":
            return MockProvider(
                MockScenario.TIMEOUT,
                timeout_accepted=True,  # 「接受之后超时」= request_accepted=True → UNKNOWN
                failure_code="mock_status_unresolvable",
                failure_safe_message="请求已被接受，但到期仍无法解析状态或结果。",
            )
        if scenario == "duplicate_webhook":
            return DuplicateWebhookScenarioProvider()
        return MockProvider(MockScenario(scenario))

    provider_factory.build_ai_provider = _scenario_provider  # type: ignore[assignment]


def main(argv: list[str] | None = None) -> int:
    """`prefetch-unknown` 子命令：给宿主/容器准备 UNKNOWN 场景的图片字节。"""

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "prefetch-unknown":
        written = prefetch_unknown_outputs()
        print(f"[scenario] 预生成 unknown 输出 {written} 个 → {_SCENARIO_OUT}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
