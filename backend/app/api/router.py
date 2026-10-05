"""`/api/v1` 路由根（ENG-03 第 3 步收敛为单一聚合点）。

本模块是唯一的 API 根路由聚合点：`app.main` 只把 `api_router` 挂到 `API_PREFIX`，
不在入口散落 `include_router`。

当前包含：

- `health`（ENG-02）：`/health/live`、`/health/ready`
- `auth`（ENG-03）：`/auth/invites/redeem`、`/auth/email/request`、`/auth/email/verify`、
  `/auth/session`、`/auth/logout`
- `me`（ENG-03）：`GET/PATCH /me`
- `assets`（ENG-05）：`POST /assets/uploads`、`POST /assets/{asset_id}/complete`、
  `GET /assets/{asset_id}`、`POST /assets/{asset_id}/download-url`、`DELETE /assets/{asset_id}`
- `analysis`：`POST /analyses`——读一张用户自己的参考图，换回一段可编辑的画面描述
  （不建任务、不占点数，与生成链路完全分开）
- `generations`（ENG-06）：`POST /generations`、`GET /generations/{generation_id}`、
  `GET /generations/{generation_id}/outputs`、`POST /generations/{generation_id}/retry`、
  `GET /me/generations`
- `shares`（ENG-09）：`POST /shares`、`GET /shares/{slug}`（匿名读取）
- `feedback`（ENG-09）：`POST /feedback`
- `internal`（ENG-06）：`POST /internal/providers/{provider_name}/webhook`（`x-internal`）

边界：本模块不实现业务规则，不访问数据库、Provider 或对象存储。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.analysis.router import analyses_router
from app.api.assets.router import assets_router
from app.api.auth.router import auth_router
from app.api.feedback.router import feedback_router
from app.api.generations.router import generations_router, me_generations_router
from app.api.health.router import health_router
from app.api.internal.webhook_router import internal_router
from app.api.me.router import me_router
from app.api.shares.router import shares_router

__all__ = ["API_PREFIX", "api_router"]

#: 冻结的 canonical 前缀（`03-api-contract.md` 第 1 节）
API_PREFIX = "/api/v1"

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(me_router)
api_router.include_router(assets_router)
api_router.include_router(analyses_router)
api_router.include_router(generations_router)
api_router.include_router(me_generations_router)
api_router.include_router(shares_router)
api_router.include_router(feedback_router)
api_router.include_router(internal_router)
