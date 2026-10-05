"""参考图分析业务域（「参考图片找灵感」）。

- `service`：`AnalysisService`——归属与状态校验、送模型前的重编码降维、私人信息拦截；
- `guard`：`AnalysisGuard`——缓存回放、单飞锁、每日配额（三道付费闸门，fail-closed）；
- `analyzer_factory`：按服务端配置装配 `ImageAnalyzer`，绝不静默退回 Mock。

本包**不是**生成链路的一部分：不建任务、不占点数、不进状态机、不产出图片，
也不导入任何供应商 SDK（`04-ai-provider.md` 第 1 节、`09-repository-boundary.md` 第 4 节）。
"""

from __future__ import annotations

from app.services.analysis.analyzer_factory import build_image_analyzer
from app.services.analysis.guard import (
    AnalysisGuard,
    AnalysisLease,
    CachedDescription,
    RedisAnalysisGuard,
)
from app.services.analysis.service import MAX_ANALYSIS_SIDE, AnalysisOutcome, AnalysisService

__all__ = [
    "MAX_ANALYSIS_SIDE",
    "AnalysisGuard",
    "AnalysisLease",
    "AnalysisOutcome",
    "AnalysisService",
    "CachedDescription",
    "RedisAnalysisGuard",
    "build_image_analyzer",
]
