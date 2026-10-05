"""`MockImageAnalyzer` 的确定性（只用于测试与本地替身，禁止生产）。

它是「Mock 验证到哪一层」的边界证据：不出网、按图片摘要给固定描述、
每次调用都被记录下来，因此服务层可以断言「第二次请求没有再打供应商」。
"""

from __future__ import annotations

import pytest

from app.providers.mock_analysis import MOCK_VISION_MODEL, MockImageAnalyzer
from app.providers.ports import (
    DESCRIPTION_MAX_CHARS,
    ImageDescriptionRequest,
    ImageInput,
    ProviderCallError,
    ProviderErrorCategory,
)
from app.services.analysis.analyzer_factory import build_image_analyzer
from tests.services.fakes import synthetic_png_bytes


def request_for(content: bytes) -> ImageDescriptionRequest:
    return ImageDescriptionRequest(
        image=ImageInput(mime_type="image/png", content=content, sha256="a" * 64)
    )


def test_same_image_always_yields_the_same_description() -> None:
    content = synthetic_png_bytes(size=(8, 8))

    first = MockImageAnalyzer().describe_image(request_for(content))
    second = MockImageAnalyzer().describe_image(request_for(content))

    assert first.description == second.description
    assert first.model == MOCK_VISION_MODEL


def test_different_images_yield_different_descriptions() -> None:
    analyzer = MockImageAnalyzer()

    a = analyzer.describe_image(request_for(synthetic_png_bytes(size=(8, 8))))
    b = analyzer.describe_image(request_for(synthetic_png_bytes(size=(9, 9))))

    assert a.description != b.description


def test_every_sample_is_a_scene_description_within_the_port_cap() -> None:
    """四段样本都要能放进界面的一段话里，也不能出现「提示词」这种冒充原作的措辞。"""

    analyzer = MockImageAnalyzer()
    seen: set[str] = set()

    for size in [(4, 4), (5, 5), (6, 6), (7, 7), (8, 8), (9, 9), (10, 10), (11, 11)]:
        result = analyzer.describe_image(request_for(synthetic_png_bytes(size=size)))
        assert 0 < len(result.description) <= DESCRIPTION_MAX_CHARS
        assert "提示词" not in result.description
        seen.add(result.description)

    assert len(seen) > 1, "样本必须按图片轮换，否则『换图结果会变』这条断言是空的"


def test_calls_are_recorded_so_tests_can_count_paid_shape() -> None:
    analyzer = MockImageAnalyzer()

    analyzer.describe_image(request_for(synthetic_png_bytes()))

    assert len(analyzer.calls) == 1
    assert analyzer.calls[0].image.content == synthetic_png_bytes()


def test_scripted_failure_raises_the_normalized_error() -> None:
    error = ProviderCallError(
        category=ProviderErrorCategory.TEMPORARY,
        code="synthetic",
        safe_message="合成：供应商暂时不可用",
        request_accepted=None,
    )
    analyzer = MockImageAnalyzer(fail_with=error)

    with pytest.raises(ProviderCallError) as excinfo:
        analyzer.describe_image(request_for(synthetic_png_bytes()))

    assert excinfo.value is error


def test_mock_is_the_only_analyzer_available_without_credentials() -> None:
    """本地/测试默认零凭据可用；真实型号必须显式配置（见 analyzer_factory 的用例）。"""

    assert isinstance(build_image_analyzer(ai_provider="MOCK"), MockImageAnalyzer)
