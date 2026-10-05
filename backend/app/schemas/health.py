"""健康检查 Schema（ENG-02 第 3-4 步）。

契约 `03-api-contract.md` 第 4.5 节冻结了健康检查的路径、权限与状态码；
`shared/api-contract/openapi.json`（LEAD-03 冻结基线）进一步给出了响应形状：

```text
HealthEnvelope = allOf[
  Envelope { data: any, request_id: string },
  { data: { status: string } }
]
```

因此 `data.status` 是**可选、非空**的字符串。为在 OpenAPI 中输出
`{"type": "string"}` 且不带 `default`、也不进入 `required`，这里使用
`Field(default_factory=...)`（Pydantic 不会为工厂生成 `default` 关键字）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Envelope

__all__ = ["DependencyReport", "HealthData", "HealthEnvelope"]


class HealthData(BaseModel):
    """健康检查响应的 `data`：`status` 可选、非空、无默认值关键字。"""

    status: str = Field(default_factory=lambda: "ok")


class HealthEnvelope(Envelope):
    """健康检查成功响应 envelope（收窄 `data`）。"""

    data: HealthData


class DependencyReport(BaseModel):
    """单个依赖的检查结果（仅出现在 503 的 `error.details.dependencies` 中）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    ready: bool
    detail: str | None = None
