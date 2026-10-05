"""FastAPI 进程入口（ENG-02 第 1-3 步）。

职责：

- 创建 FastAPI 应用实例（`uvicorn app.main:app` 启动）；
- 装配中间件（请求 ID、脱敏访问日志）与统一异常处理器；
- 挂载 `/api/v1` 路由根与健康检查路由；
- 按 `APP_ENV` 控制交互式文档端点：`production` 关闭 `/docs`、`/redoc`、`/openapi.json`。

边界：

- 不实现业务路由：用户、邀请码、点数、生成任务、Provider、Storage 属后续卡片；
- OpenAPI 导出与基线比较属本卡第 4 步；
- 本模块不承载业务规则，不直接访问数据库、Provider 或对象存储。
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.router import API_PREFIX, api_router
from app.config import get_settings
from app.errors.handlers import install_exception_handlers
from app.middleware import install_middleware

__all__ = ["APP_TITLE", "APP_VERSION", "API_PREFIX", "app", "create_app"]

APP_TITLE = "AI Avatar Studio API"
APP_VERSION = "1.0.0"


def create_app() -> FastAPI:
    """创建并返回 FastAPI 应用。"""

    # 文档端点开关只依据既有 APP_ENV，不引入独立开关变量（2026-09-26 裁决）
    docs_enabled = not get_settings().is_production

    application = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    install_middleware(application)
    install_exception_handlers(application)
    # 单一聚合点：health / auth / me 全部由 `api_router` 挂载
    application.include_router(api_router, prefix=API_PREFIX)
    return application


app = create_app()
