# backend/ — V1.0 后端边界（ENG-00 骨架）

> 状态：骨架占位，不含任何业务实现。
> 依据：`docs/architecture/06-development-standard.md`、`docs/architecture/09-repository-boundary.md`

## 职责

FastAPI 与全部 Python 服务端代码，是唯一的业务规则和服务端密钥边界。

## 目录

| 目录 | 职责 |
|---|---|
| `app/main.py` | FastAPI 进程入口 |
| `app/config.py` | 服务端配置读取 |
| `app/api/` | 路由和请求/响应 Schema |
| `app/services/` | 业务规则和事务编排 |
| `app/repositories/` | SQLAlchemy 数据访问 |
| `app/db/` | Session、模型和数据库工具 |
| `app/tasks/` | Celery app、任务入口和调度任务 |
| `app/providers/` | AIProvider 接口及适配器 |
| `app/storage/` | StorageAdapter 接口及适配器 |
| `app/schemas/` | 跨路由复用的 Pydantic Schema |
| `alembic/` | 数据库迁移 |
| `tests/` | Python 单元和集成测试 |

## 依赖方向

```text
API Router → Service → Repository → PostgreSQL
Service ├→ AIProvider Interface
        └→ StorageAdapter Interface
```

## 进程模型

后端代码只维护一份，以不同进程启动：

```text
FastAPI API：uvicorn app.main:app
Celery Worker：celery -A app.tasks.celery_app worker
Celery Beat：celery -A app.tasks.celery_app beat
```

API 进程不执行长时间 AI 生成；Worker 与 Beat 不对外提供业务 HTTP 端口。

## 当前状态

ENG-00 只建立目录与进程入口约定。数据库模型与迁移属于 `ENG-01`；FastAPI 基础服务、配置、健康检查与 OpenAPI 属于 `ENG-02`。
