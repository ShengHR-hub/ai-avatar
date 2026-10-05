"""`ImageAnalyzer` 的装配（与 `provider_factory.build_ai_provider` 同一口径）。

看图与出图是两种能力、两个型号、两套计价，所以这里只认 `AI_VISION_MODEL`，
**绝不拿 `AI_MODEL` 顶上**：用出图型号去读图，两边都会得到说不清的结果。

`MOCK` 只给本地与测试；真实供应商缺任一必要配置就响亮报错，绝不静默退回 Mock——
退回会让一次「看起来成功了」的分析其实没调用任何模型，界面却会把假描述当成
用户的参考图读出来的东西（`09-repository-boundary.md` 第 3 节的 fail-fast 约定）。
"""

from __future__ import annotations

from app.providers.ports import ImageAnalyzer

__all__ = ["build_image_analyzer"]

#: 与出图侧共用同一份白名单：能装配分析器的 provider 必须也是已支持的 provider
SUPPORTED_ANALYZER_PROVIDERS = ("MOCK", "QUANZIL")


def build_image_analyzer(
    *,
    ai_provider: str,
    base_url: str | None = None,
    api_key: str | None = None,
    vision_model: str | None = None,
) -> ImageAnalyzer:
    """按服务端配置返回图片理解适配器；构造不建立任何网络连接。"""

    normalized = (ai_provider or "").strip().upper() or "MOCK"
    if normalized not in SUPPORTED_ANALYZER_PROVIDERS:
        raise RuntimeError(
            f"AI_PROVIDER={ai_provider!r} 未获支持；只允许 "
            f"{'、'.join(SUPPORTED_ANALYZER_PROVIDERS)}，且不做多模型自动路由。"
        )

    if normalized == "MOCK":
        from app.providers.mock_analysis import MockImageAnalyzer

        return MockImageAnalyzer()

    missing = [
        name
        for name, value in (
            ("AI_BASE_URL", base_url),
            ("AI_API_KEY", api_key),
            ("AI_VISION_MODEL", vision_model),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise RuntimeError(
            "AI_PROVIDER=QUANZIL 缺少必要配置：" + "、".join(missing) + "。"
            "绝不退回 Mock——那会让没有读图的请求被当成分析成功。"
        )

    from app.providers.quanzil_vision import QuanzilVisionAnalyzer

    return QuanzilVisionAnalyzer(
        base_url=str(base_url), api_key=str(api_key), model=str(vision_model)
    )
