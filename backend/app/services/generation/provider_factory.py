"""单一 Provider 的装配（ENG-06 第 4 步；ENG-07 接入 Quanzil）。

`04-ai-provider.md` 第 1 节：V1.0 固定配置单一 Provider，**不实现多模型自动路由**；
第 6 节：Mock 场景只由服务端配置或测试夹具选择，绝不由公开用户输入决定。

因此本工厂只认识 `AI_PROVIDER` 的白名单取值，其余一律响亮失败：

- `MOCK`：确定性假适配器，禁止生产；
- `QUANZIL`：真实适配器，但必须有完整的 `AI_BASE_URL`/`AI_MODEL`/`AI_API_KEY`——
  缺任一项就报错，**绝不静默退回 Mock**。退回会让一次「看起来成功」的生成实际上
  没有调用任何供应商，还会把假图片写进用户的对象存储（`09-repository-boundary.md`
  第 3 节的 fail-fast 约定）。
"""

from __future__ import annotations

from app.providers.mock import MockProvider
from app.providers.ports import AIProvider

__all__ = [
    "MOCK_DEFAULT_MODEL",
    "SUPPORTED_AI_PROVIDERS",
    "build_ai_provider",
    "provider_identity",
]

#: 支持的 Provider 标识（与 `provider_attempts.provider_name` 同样是大写枚举值）
SUPPORTED_AI_PROVIDERS = ("MOCK", "QUANZIL")

#: `MOCK` 的固定型号：`AI_MODEL` 允许留空，因为假适配器本来就不认型号
MOCK_DEFAULT_MODEL = "mock-image-v1"


def provider_identity(*, ai_provider: str, ai_model: str | None = None) -> tuple[str, str]:
    """给出任务与 attempt 行要登记的 `(provider_name, model_name)`。

    这两个值必须由**同一份配置**推导：任务行登记 `MOCK` 而实际出网打向 Quanzil，会让
    `provider_attempts` 与 Webhook 关联（`provider_name + request_id`）整体失真，
    事后事故复盘会对着错误的供应商排查。缺型号时真实供应商直接报错，不做默认猜测。
    """

    from app.providers.quanzil import QUANZIL_MODEL

    normalized = (ai_provider or "").strip().upper() or "MOCK"
    if normalized not in SUPPORTED_AI_PROVIDERS:
        raise RuntimeError(f"AI_PROVIDER={ai_provider!r} 未获支持。")
    model = (ai_model or "").strip()
    if normalized == "MOCK":
        return normalized, model or MOCK_DEFAULT_MODEL
    if not model:
        raise RuntimeError("AI_PROVIDER=QUANZIL 必须配置 AI_MODEL（已验证型号：" + QUANZIL_MODEL + "）。")
    return normalized, model


def build_ai_provider(
    *,
    ai_provider: str,
    scenario: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> AIProvider:
    """按服务端配置返回适配器实例；构造不建立任何网络连接。

    `scenario` 只对 `MOCK` 有意义。真实适配器需要三项配置齐备：它们只来自服务端环境，
    适配器因此永远拿不到「用户可控的出网目标」，也永远不会有凭据落到代码或日志里。
    """

    normalized = ai_provider.strip().upper()
    if normalized not in SUPPORTED_AI_PROVIDERS:
        raise RuntimeError(
            f"AI_PROVIDER={ai_provider!r} 未获支持；V1.0 只允许 "
            f"{'、'.join(SUPPORTED_AI_PROVIDERS)}，且不做多模型自动路由。"
        )

    if normalized == "MOCK":
        if scenario is None:
            return MockProvider()

        from app.providers.mock import MockScenario

        return MockProvider(MockScenario(scenario))

    if scenario is not None:
        raise RuntimeError(
            "scenario 只适用于 MOCK；真实供应商不得由场景参数决定行为（第 6 节）。"
        )

    missing = [
        name
        for name, value in (
            ("AI_BASE_URL", base_url),
            ("AI_MODEL", model),
            ("AI_API_KEY", api_key),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise RuntimeError(
            "AI_PROVIDER=QUANZIL 缺少必要配置：" + "、".join(missing) + "。"
            "绝不退回 Mock——那会让没有调用供应商的任务被记成成功。"
        )

    from app.providers.quanzil import QuanzilImageProvider

    return QuanzilImageProvider(
        base_url=str(base_url), model=str(model), api_key=str(api_key)
    )
