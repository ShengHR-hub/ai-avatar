"""`build_image_analyzer` 的装配裁决（看图能力）。

核心只有一句话：**缺配置就报错，绝不静默退回 Mock**。
退回会让一次没读过图的请求在界面上显示成「已经从你的参考图里读出描述」，
而用户会拿这段假描述去发起一次真实生成、真的扣点数。
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.mock_analysis import MockImageAnalyzer
from app.providers.quanzil_vision import QuanzilVisionAnalyzer
from app.services.analysis.analyzer_factory import build_image_analyzer

BASE = "https://relay.invalid/v1"
KEY = "synthetic-key-never-real"
VISION = "synthetic-vision-model"


def test_mock_provider_gets_the_deterministic_offline_analyzer() -> None:
    analyzer = build_image_analyzer(ai_provider="MOCK")

    assert isinstance(analyzer, MockImageAnalyzer)
    assert analyzer.provider_name == "MOCK"


def test_provider_name_is_matched_case_insensitively_and_blank_means_mock() -> None:
    assert isinstance(build_image_analyzer(ai_provider="mock"), MockImageAnalyzer)
    assert isinstance(build_image_analyzer(ai_provider="  "), MockImageAnalyzer)


def test_unknown_provider_is_rejected_rather_than_defaulted() -> None:
    """V1.0 不做多模型路由：`AI_PROVIDER=other` 必须是装配期错误，不是「那就用 mock」。"""

    with pytest.raises(RuntimeError, match="未获支持"):
        build_image_analyzer(ai_provider="OTHER")


def test_real_analyzer_requires_all_three_settings() -> None:
    missing: list[str] = []
    for name, kwargs in (
        ("AI_BASE_URL", {"base_url": "", "api_key": KEY, "vision_model": VISION}),
        ("AI_API_KEY", {"base_url": BASE, "api_key": "  ", "vision_model": VISION}),
        ("AI_VISION_MODEL", {"base_url": BASE, "api_key": KEY, "vision_model": ""}),
    ):
        with pytest.raises(RuntimeError) as excinfo:
            build_image_analyzer(ai_provider="QUANZIL", **kwargs)
        missing.append(name)
        assert name in str(excinfo.value)
        assert "绝不退回 Mock" in str(excinfo.value)

    assert missing == ["AI_BASE_URL", "AI_API_KEY", "AI_VISION_MODEL"]


def test_the_generation_model_is_never_substituted_for_the_vision_model() -> None:
    """`AI_MODEL` 是出图型号；把它顶给看图这条链路会让两边都说不清结果。"""

    with pytest.raises(RuntimeError, match="AI_VISION_MODEL"):
        build_image_analyzer(
            ai_provider="QUANZIL", base_url=BASE, api_key=KEY, vision_model=None
        )


def test_real_analyzer_is_built_without_touching_the_network() -> None:
    """构造只建客户端对象、不出网——与 `RedisClientAdapter`/`Minio(...)` 同一口径。

    这里用一个「一问就告密」的 transport 兜住：装配完成后 `model_name` 可取，
    而 transport 从未被调用。
    """

    calls: list[str] = []

    def _spy(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        calls.append(str(request.url))
        raise AssertionError("装配阶段不得出网")

    analyzer = build_image_analyzer(
        ai_provider="QUANZIL", base_url=BASE, api_key=KEY, vision_model=VISION
    )

    assert isinstance(analyzer, QuanzilVisionAnalyzer)
    assert analyzer.model_name == VISION
    assert analyzer.provider_name == "QUANZIL"

    watched = QuanzilVisionAnalyzer(
        base_url=BASE, api_key=KEY, model=VISION, transport=httpx.MockTransport(_spy)
    )
    assert watched.model_name == VISION
    assert calls == []
