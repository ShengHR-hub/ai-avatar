"""健康检查路由（ENG-02 第 3-4 步）。

契约依据：`03-api-contract.md` 第 4.5 节与第 8 节权限矩阵；
响应形状对齐 `shared/api-contract/openapi.json`（LEAD-03 冻结基线）：

| 方法 | 路径 | 说明 | 权限 | 成功 |
|---|---|---|---|---|
| `GET` | `/api/v1/health/live` | 进程存活，不访问外部依赖 | 匿名/内部 | `200` |
| `GET` | `/api/v1/health/ready` | PostgreSQL、Redis、对象存储、Worker 心跳 | 内部 | `200`/`503` |

与基线对齐的声明（技术负责人 2026-09-26 裁决 ①③）：

- `operationId` 使用基线命名 `healthLive` / `healthReady`；
- 显式声明 `security: []`（健康检查不需要认证），经 `openapi_extra` 输出；
- 不添加 `summary` / `description`（基线未声明）。注意 FastAPI 0.141 的
  `generate_operation_summary()` 会**无条件**为每个 operation 生成 `summary`（由函数名派生），
  无法通过路由声明移除；该字段属文档性注解，由第 4 步的规范化比较忽略；
- 200 响应描述固定为 `Health response`，503 响应描述固定为 `Stable API error`；
- 响应体使用 `HealthEnvelope` / `ErrorResponse`，允许 FastAPI 内联，
  不要求复刻基线的 `components.responses` 命名与拓扑。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.health.checks import DependencyCheck, get_dependency_checks
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.middleware.request_id import request_id_of
from app.schemas.common import ErrorResponse
from app.schemas.health import DependencyReport, HealthData, HealthEnvelope

__all__ = ["health_router"]

#: 与冻结基线一致的响应描述
HEALTH_RESPONSE_DESCRIPTION = "Health response"
ERROR_RESPONSE_DESCRIPTION = "Stable API error"

health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get(
    "/live",
    response_model=HealthEnvelope,
    operation_id="healthLive",
    responses={
        200: {"description": HEALTH_RESPONSE_DESCRIPTION},
    },
    openapi_extra={"security": []},
)
def health_live(request: Request) -> HealthEnvelope:
    return HealthEnvelope(
        data=HealthData(status="ok"), request_id=request_id_of(request)
    )


@health_router.get(
    "/ready",
    response_model=HealthEnvelope,
    operation_id="healthReady",
    responses={
        200: {"description": HEALTH_RESPONSE_DESCRIPTION},
        503: {"description": ERROR_RESPONSE_DESCRIPTION, "model": ErrorResponse},
    },
    openapi_extra={"security": []},
)
def health_ready(
    request: Request,
    checks: tuple[DependencyCheck, ...] = Depends(get_dependency_checks),
) -> HealthEnvelope:
    reports = [
        DependencyReport(name=result.name, ready=result.ready, detail=result.detail)
        for result in (check() for check in checks)
    ]

    if any(not report.ready for report in reports):
        raise ApiError(
            ErrorCode.NOT_READY,
            details={
                "dependencies": [report.model_dump(mode="json") for report in reports]
            },
        )

    return HealthEnvelope(
        data=HealthData(status="ok"), request_id=request_id_of(request)
    )
