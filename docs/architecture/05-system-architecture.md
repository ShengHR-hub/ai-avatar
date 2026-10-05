# AI Avatar Studio V1.0 系统架构

> 状态：已冻结
> 日期：2026-09-24

## 1. 总体拓扑

```text
浏览器 / 移动端
       ↓ 页面和公开资源
Next.js frontend
       ↓ REST /api/v1（仅传输，不承载业务规则）
FastAPI API
  ├── PostgreSQL
  ├── Redis
  └── 对象存储适配层
       └── Redis Broker
               ↓
        Celery Worker
               ↓
        Generation Service
               ↓
        ProviderRouter
               ↓
        MockProvider / PrimaryProvider

Celery Beat ─────────────→ Redis Broker（只投递计划任务）
```

Next.js 负责用户体验和公开内容；FastAPI 负责所有业务规则和事务编排；Celery Worker 负责耗时任务；Celery Beat 只负责按计划投递任务；PostgreSQL 保存业务事实；Redis 保存队列、会话和短期状态；MinIO 通过 Storage Adapter 保存图片二进制。

完整目录、职责矩阵和依赖约束见 [仓库边界与本地进程基线](./09-repository-boundary.md)。

## 2. 单仓库结构

```text
ai-avatar/
├── frontend/
│   ├── app/
│   ├── components/
│   ├── hooks/
│   ├── services/
│   ├── types/
│   ├── generated/
│   └── public/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── api/
│   │   ├── services/
│   │   ├── repositories/
│   │   ├── providers/
│   │   ├── storage/
│   │   ├── tasks/
│   │   ├── db/
│   │   └── schemas/
│   ├── alembic/
│   └── tests/
├── shared/
│   ├── api-contract/
│   ├── enums/
│   └── state-machines/
├── docs/
├── docker/
├── scripts/
└── README.md
```

`shared/` 只保存协议和文档。FastAPI 生成 OpenAPI，前端据此生成 TypeScript 类型和客户端，不在两种语言之间共享源代码。

业务域目录（例如 `auth/`、`users/`、`generations/`、`points/`、`styles/`、`prompts/`、`shares/`、`feedback/`、`orders/` 和 `admin/`）由对应工程任务卡在需要时创建；`ENG-00` 不创建这些目录。`09-repository-boundary.md` 是当前最小目录骨架的规范来源。

## 3. 进程模型

后端代码只维护一份，但以不同进程运行：

```text
FastAPI API：uvicorn app.main:app
Celery Worker：celery -A app.tasks.celery_app worker
Celery Beat：celery -A app.tasks.celery_app beat
```

API 进程不执行长时间 AI 生成。Worker 不对外提供业务 API。

依赖方向固定为：

```text
API → Service → Repository → PostgreSQL
              ├→ StorageAdapter → MinIO
              └→ Celery producer → Redis
Beat → Redis → Worker → Service
Worker → AIProvider/Router → 外部 Provider
```

Beat 不直接写 PostgreSQL、MinIO 或点数账务；Worker 不绕过 Service 直接修改账务表。

## 4. 本地开发环境

Docker Compose 启动：

```text
frontend     Next.js
backend      FastAPI
worker       Celery Worker
beat         Celery Beat
postgres     PostgreSQL
redis        Redis
minio        S3 兼容对象存储
```

规范文件为 `docker/compose.yml`，单一私有网络连接 `frontend`、`backend`、`worker`、`beat`、`postgres`、`redis`、`minio`。启动命令、卷策略和健康检查见 [仓库边界与本地进程基线](./09-repository-boundary.md)。

本地默认使用 `MockProvider`，并允许通过环境变量切换到 `PrimaryProvider` 做受控集成测试。

本地完整闭环必须覆盖：

```text
邀请码 → 邮箱 → 点数 → 上传 → 创建任务 → Mock 生成
→ 对象存储 → 结算 → 结果展示 → 下载 / 分享 / 反馈
```

## 5. 测试服务器

本地闭环通过并获得测试反馈后，使用单服务器 Docker Compose：

```text
Nginx
├── Next.js
├── FastAPI
├── Celery Worker
├── Celery Beat
└── Redis

托管 PostgreSQL
COS / OSS / S3
```

测试环境不引入 Kubernetes、服务网格或多机编排。

## 6. 生产演进

生产阶段按真实压力逐步增加：

```text
CDN
负载均衡
托管 PostgreSQL
托管 Redis
对象存储
独立 Worker 节点
```

应用层必须保持无状态。会话存入 Redis，图片存入对象存储，业务事实存入 PostgreSQL，因此 API 和 Worker 可以在需要时横向扩展。

## 7. 配置边界

环境差异只由配置表达，至少包括：

```text
APP_ENV
APP_BASE_URL
DATABASE_URL
REDIS_URL
STORAGE_DRIVER
STORAGE_ENDPOINT
STORAGE_BUCKET
STORAGE_ACCESS_KEY
STORAGE_SECRET_KEY
AI_PROVIDER
AI_MODEL
AI_API_KEY
ENABLE_REFERENCE_IMAGE
SESSION_SECRET
```

密钥不得进入 Git、日志、OpenAPI、浏览器 bundle 或 `NEXT_PUBLIC_*` 环境变量。

服务端变量只注入 FastAPI、Worker 和 Beat；前端只允许非敏感的 `NEXT_PUBLIC_API_BASE_URL`、`NEXT_PUBLIC_APP_URL` 等公开配置。`DATABASE_URL`、`AI_API_KEY`、`SESSION_SECRET`、对象存储密钥和 Compose 初始化密码均不得进入前端。

## 8. 可观测性

日志必须为结构化日志，并包含：

```text
request_id
user_id（可用时）
generation_id（可用时）
provider_attempt_id（可用时）
event
duration_ms
error_code
```

指标至少覆盖：

- API 请求量、延迟和错误率；
- 队列长度和任务等待时间；
- 生成成功率、失败率和人工复核数；
- Provider 延迟和错误分类；
- 每日 AI 估算成本；
- 点数预留、消费和释放；
- 过期资源清理数量。

## 9. 安全边界

- 认证使用 HttpOnly、Secure、SameSite Cookie；
- 对象上传和下载使用短期签名 URL；
- 分享页只允许读取明确标记为 `SHARED` 的资源；
- Provider Webhook 必须验签并防重放；
- 上传完成后必须校验文件类型、大小、哈希和可解码性；
- 管理接口单独鉴权并记录审计日志；
- 原图、完整 Prompt 和 Provider 凭据不得写入普通日志。
- 日志不得记录签名 URL、邮箱验证令牌、完整 Provider 原始响应或未脱敏个人信息；异常堆栈只进入受控服务端诊断日志。

## 10. 契约冻结声明

本次 `LEAD-01` 只冻结仓库、进程、依赖、环境变量和日志边界，不修改数据库字段、API 路径、错误码、任务状态机、计费状态机或点数预留/消费/释放规则。

## 11. 架构验收标准

1. Docker Compose 能启动本地全部依赖。
2. Next.js 能通过 `/api/v1` 调用 FastAPI。
3. FastAPI 能创建异步生成任务并返回 `202`。
4. Worker 能从 Redis 领取任务并通过 MockProvider 完成生成。
5. 成功任务正确消费预留点数。
6. 失败任务正确释放点数。
7. 重复请求、Worker 重试和重复回调不会重复扣点或退款。
8. Worker 重启后未完成任务可以恢复。
9. Provider 成功但结果缺失时进入 `NEEDS_REVIEW`。
10. Provider 用量与估算成本可以被记录和统计。
11. 对象存储驱动切换不需要修改业务代码。
12. OpenAPI 可以生成可编译的前端 TypeScript 客户端。
13. Alembic 能从空数据库完成全部迁移。
14. 前端构建产物中不存在服务端密钥。
15. 所有点数变化均可从流水和审计日志还原。
