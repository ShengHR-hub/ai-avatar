"""`MockImageAnalyzer`：确定性的假看图实现（只用于测试与本地替身，禁止生产）。

它**不做任何网络调用**，输出由图片字节的摘要决定，因此同一个输入永远得到同一段描述——
测试可以断言结果，也可以断言"换一张图结果会变"。
"""

from __future__ import annotations

import hashlib

from app.providers.ports import (
    ImageDescriptionRequest,
    ImageDescriptionResult,
    ProviderCallError,
    ProviderUsage,
)

__all__ = ["MockImageAnalyzer", "MOCK_VISION_MODEL"]

MOCK_VISION_MODEL = "mock-vision-1"

#: 几段固定描述，按图片摘要轮换；刻意都是"画面描述"而不是"原始提示词"的口吻
_SAMPLES = (
    "一个人背对着镜头站在草地上，远处是起伏的山脊，天空占了大半画面，"
    "整体偏青绿色调，光线柔和，画法接近写实摄影，情绪安静。",
    "一只猫蹲在海边的岩石上，背影对着海面，海是深蓝的，天光偏冷，"
    "画面里有一些细小的飞虫，质感接近手绘，情绪松弛。",
    "一个圆脸小动物正对着镜头吐舌头，五官夸张、眼睛很大，背景是纯亮的玫红色，"
    "笔触粗、颜色跳，像蜡笔涂出来的，整体搞怪。",
    "一片雪地里的黑色剪影，人物只占画面很小一块，周围是低对比的白与灰，"
    "光从画面右侧压过来，留白很多，情绪冷而安静。",
)


class MockImageAnalyzer:
    """`ImageAnalyzer` 的确定性替身。"""

    provider_name = "MOCK"

    def __init__(self, *, fail_with: ProviderCallError | None = None) -> None:
        self._fail_with = fail_with
        self.calls: list[ImageDescriptionRequest] = []

    @property
    def model_name(self) -> str:
        return MOCK_VISION_MODEL

    def describe_image(self, request: ImageDescriptionRequest) -> ImageDescriptionResult:
        self.calls.append(request)
        if self._fail_with is not None:
            raise self._fail_with

        digest = hashlib.sha256(request.image.content).digest()
        description = _SAMPLES[digest[0] % len(_SAMPLES)]
        return ImageDescriptionResult(
            description=description,
            model=MOCK_VISION_MODEL,
            usage=ProviderUsage(input_tokens=len(request.image.content) // 4, output_tokens=len(description)),
        )
