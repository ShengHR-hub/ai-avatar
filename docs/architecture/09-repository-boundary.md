# AI Avatar Studio V1.0 仓库边界与本地进程基线

> 状态：已冻结
> 日期：2026-09-25
> 适用范围：V1.0 从零实现

## 1. 边界声明

V1.0 以本文件、`01-overview.md` 至 `07-development-roadmap.md` 以及 `docs/task-cards/` 下当前任务卡为正式架构基线。`08-task-cards-and-ownership.md` 是早期任务拆分参考，已被 `docs/task-cards/00-index.md` 和独立任务卡取代。仓库当前存在的旧测试版文件只说明历史现状，不是 V1.0 的实现前提；本任务不复制、移动、改造或迁移这些文件。

V1.0 是单仓库、前后端分层的应用，不拆分微服务。前端和后端通过 REST/OpenAPI 通信；Python 与 TypeScript 不共享运行时代码。

## 2. 目标目录边界

```text
ai-avatar/
├── frontend/                 # Next.js、React、TypeScript 前端
│   ├── app/                  # 路由、布局和页面组合
│   ├── components/           # 可复用 UI/领域组件
│   ├── hooks/                # 客户端交互和状态封装
│   ├── services/             # OpenAPI 客户端调用和 DTO 转换
│   ├── types/                # 前端专用类型
│   ├── generated/            # 由 OpenAPI 生成的客户端/类型
│   └── public/               # 不敏感静态资源
├── backend/                  # FastAPI 和全部 Python 服务端代码
│   ├── app/
│   │   ├── main.py           # FastAPI 入口
│   │   ├── config.py         # 服务端配置读取
│   │   ├── api/              # 路由和请求/响应 Schema
│   │   ├── services/         # 业务规则和事务编排
│   │   ├── repositories/     # SQLAlchemy 数据访问
│   │   ├── db/               # Session、模型和数据库工具
│   │   ├── tasks/            # Celery app、任务入口和调度任务
│   │   ├── providers/        # AIProvider 接口及适配器
│   │   ├── storage/          # StorageAdapter 接口及适配器
│   │   └── schemas/          # 跨路由复用的 Pydantic Schema
│   ├── alembic/              # 数据库迁移
│   └── tests/                # Python 单元和集成测试
├── shared/                   # 语言无关的契约产物，不放业务运行时代码
│   ├── api-contract/         # FastAPI 导出的 openapi.json
│   ├── enums/                # 必要的协议枚举文档/产物
│   └── state-machines/       # 状态机契约文档/产物
├── docs/                     # 架构、契约、任务卡和审核记录
├── docker/                   # Dockerfile、Compose 文件和本地编排配置
└── scripts/                  # 工程辅助脚本；不得承载业务规则
```

目录边界规则：

- `frontend/` 不持有 PostgreSQL、Redis、MinIO 或 Provider 凭据，也不使用基础设施 SDK；浏览器对象传输只能使用 API 签发的短期 URL，页面不得复制后端业务规则。
- `backend/` 是唯一的业务规则和服务端密钥边界；`frontend/` 不得成为第二套 API 实现。
- `shared/` 只保存可生成、可审查的协议产物；不得通过复制 Python/TypeScript 源码制造“共享业务层”。
- `docker/` 只描述构建、网络、卷、健康检查和进程命令；不在 Compose 文件中编写业务逻辑。
- `scripts/` 只做启动、检查、代码生成和测试编排；业务写入必须调用正式服务接口。
- 旧测试版根目录文件不属于上述 V1.0 目录，也不在 `ENG-00` 的复制或迁移范围内。

## 3. 服务职责

| 组件 | V1.0 职责 | 明确不负责 |
|---|---|---|
| Next.js | 页面、布局、SEO、上传/生成交互、调用 FastAPI、展示服务端状态 | 数据库、点数结算、Provider 调用、私密对象访问、第二套业务 API |
| FastAPI | REST `/api/v1`、认证、输入校验、权限、Service 编排、事务边界、投递 Celery 任务、OpenAPI 和健康检查 | 长时间 AI 生成、直接写对象二进制、直接调用具体 Provider SDK |
| PostgreSQL | 用户、任务、点数、媒体元数据、Provider 调用、订单预留和审计等业务事实 | 图片二进制、会话 TTL、队列消息 |
| Redis | Celery Broker、Session/验证令牌 TTL、限流和短期锁 | 点数、任务最终状态或审计等持久业务事实 |
| Celery Worker | 消费队列、执行 Generation Service、调用 Provider/Storage Adapter、重试和恢复 | 对外提供业务 HTTP API、绕过 Service 修改账务 |
| Celery Beat | 按计划向 Redis 投递清理/看门狗等任务 | 执行业务逻辑、直接结算点数、替代 Worker |
| MinIO | 本地 S3 兼容对象存储，保存图片二进制并提供短期签名 URL | 保存业务元数据、直接接受未授权访问 |
| Docker Compose | 本地启动、网络、卷、健康检查和服务依赖编排 | 生产编排、业务逻辑和数据迁移决策 |

## 4. 依赖方向

```text
浏览器
  ↓ 页面/公开资源
Next.js frontend
  ↓ REST /api/v1（可使用仅传输性的同源代理）
FastAPI API
  ↓
Service
  ├── Repository ───────────────→ PostgreSQL
  ├── StorageAdapter ───────────→ MinIO（预签名上传/下载）
  └── Celery producer ──────────→ Redis Broker

Celery Beat ────────────────────→ Redis Broker（只投递计划任务）
Redis Broker ───────────────────→ Celery Worker
Celery Worker
  ↓ 调用同一套 Service
Service
  ├── Repository ───────────────→ PostgreSQL
  ├── StorageAdapter ───────────→ MinIO
  └── AIProvider/Router ────────→ 外部 AI Provider
```

约束：

1. API、Worker、Beat 都可以依赖 Redis，但只有 Worker 执行耗时任务；任务最终状态和账务事实以 PostgreSQL 为准。
2. API 和 Worker 通过接口依赖 StorageAdapter，不直接导入 MinIO/COS/OSS/S3 SDK；浏览器只使用服务端签发的短期 URL。
3. API 与 Worker 共享后端 Service/Repository 代码，但通过不同进程启动；不得为 Worker 复制一套业务逻辑。
4. Beat 不直接依赖 Provider、Storage 或账务 Service；它只发送已注册的任务名，具体执行由 Worker 完成。
5. 前端、Redis、MinIO 和外部 Provider 都不得反向依赖 PostgreSQL 的内部表结构。

## 5. 本地 Docker Compose 拓扑

规范文件：`docker/compose.yml`。默认服务和连接关系如下：

```text
frontend :3000  ──→ backend :8000
backend  :8000  ──→ postgres :5432
backend  :8000  ──→ redis :6379
backend  :8000  ──→ minio :9000
worker           ──→ redis :6379 / postgres :5432 / minio :9000 / Provider
beat             ──→ redis :6379
```

Compose 必须定义 `frontend`、`backend`、`worker`、`beat`、`postgres`、`redis`、`minio` 七个服务，使用单一私有网络；PostgreSQL、Redis 和 MinIO 使用命名卷。`worker` 与 `beat` 使用后端同一镜像和同一服务端配置，只替换启动命令。

本地默认 `AI_PROVIDER=mock`、`ENABLE_REFERENCE_IMAGE=false`、`ENABLE_IMAGE_ANALYSIS=false`。切换真实 Provider 只能通过服务端环境变量，不能修改前端代码或 Compose 中的业务逻辑。

两个开关管的是不同能力，不得互相顶替：`ENABLE_REFERENCE_IMAGE` 管把参考图当**生成条件**的 `REFERENCE_IMAGE` 模式（上游能力未证实）；`ENABLE_IMAGE_ANALYSIS` 管「读一段画面描述」这条**不出图**的路径，它同时放行 `purpose=REFERENCE` 的上传。打开分析不会放开参考图生成，反之亦然。

PostgreSQL、Redis 与 MinIO 只把端口绑定到宿主 loopback（`127.0.0.1:`），供宿主机上运行的测试与开发进程使用，不对外暴露。

规范启动顺序：

```powershell
Copy-Item docker/.env.example .env.v1
docker compose --env-file .env.v1 -f docker/compose.yml config
docker compose --env-file .env.v1 -f docker/compose.yml up --build -d
docker compose --env-file .env.v1 -f docker/compose.yml ps
```

根目录已有的旧测试版 `.env.example` 不属于 V1.0 配置模板，不得覆盖。V1.0 模板固定为 `docker/.env.example`，本地实际配置文件固定为根目录 `.env.v1`，并且必须加入 `.gitignore`。停止本地环境使用 `docker compose --env-file .env.v1 -f docker/compose.yml down`；除非明确要重置本地数据，不得使用 `down -v`。`ENG-00` 只需完成进程和基础设施启动验证；数据库字段、Alembic 配置和迁移命令由 `ENG-01` 负责。完成 `ENG-01` 后，迁移命令才加入开发启动流程。

## 6. 环境变量边界

### 6.1 仅注入 FastAPI、Worker、Beat 的服务端变量

```text
APP_ENV
APP_BASE_URL
DATABASE_URL
REDIS_URL
STORAGE_DRIVER
STORAGE_ENDPOINT
STORAGE_BUCKET
STORAGE_REGION
STORAGE_ACCESS_KEY
STORAGE_SECRET_KEY
AI_PROVIDER
AI_BASE_URL
AI_MODEL
AI_API_KEY
AI_VISION_MODEL
ENABLE_IMAGE_ANALYSIS
ANALYSIS_DAILY_LIMIT_PER_USER
ANALYSIS_GLOBAL_DAILY_LIMIT
SESSION_SECRET
ENABLE_REFERENCE_IMAGE
```

这些变量及其值不得进入浏览器 bundle、OpenAPI、错误响应、截图、提交记录或普通日志。`DATABASE_URL`、`STORAGE_SECRET_KEY`、`AI_API_KEY` 和 `SESSION_SECRET` 始终视为高敏感密钥。

### 6.2 Compose/基础设施变量

PostgreSQL 和 MinIO 的初始化账号变量只供 Compose 和对应服务使用，例如 `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`、`MINIO_ROOT_USER`、`MINIO_ROOT_PASSWORD`。它们不得映射到 `NEXT_PUBLIC_*`，也不得由前端容器读取。

### 6.3 前端公开变量

前端最多接收非敏感的公开配置，例如：

```text
NEXT_PUBLIC_API_BASE_URL   # 公开 API origin；优先使用同源相对路径
NEXT_PUBLIC_APP_URL        # 公开站点地址
```

`NEXT_PUBLIC_*` 只表示“可以公开”，不允许放入任何密码、Token、签名密钥、Provider 端点凭据或数据库连接串。能力开关以服务端响应为准，不在前端单独复制安全策略。

## 7. 日志安全边界

所有服务使用结构化日志，至少包含 `request_id`、可用时的 `user_id`/`generation_id`、`event`、`duration_ms` 和稳定 `error_code`。日志系统必须在写出前做字段级脱敏。

禁止记录：API Key、数据库/Redis/MinIO 密码、Session Cookie、邮箱验证令牌、签名 URL 查询参数、原图或 Base64、完整 Prompt、完整 Provider 原始响应、未脱敏个人信息以及整个环境变量对象。异常堆栈只进入受控服务端诊断日志，用户响应只返回稳定错误码和安全说明。

前端浏览器日志同样不得打印 Cookie、Authorization、上传内容、完整响应或内部 URL；生产环境默认关闭调试日志。任何新增日志字段都必须先确认不会泄露上述内容。

## 8. 本基线不改变的契约

本文件只冻结仓库、进程、依赖和环境边界，不修改：

- `02-database-design.md` 中的数据库字段、约束和迁移规则；
- `03-api-contract.md` 中的 API 路径、请求/响应、错误码和幂等规则；
- `04-ai-provider.md` 中的 Provider 接口、错误分类和超时策略；
- 任务状态机、计费状态机、点数预留/消费/释放规则。
