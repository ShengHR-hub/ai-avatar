# AI Avatar Studio V1.0 会话交接文档

> 创建日期：2026-09-25
> 用途：原始对话不可继续时，新会话先读取本文件，再继续项目工作。
> 当前工作目录：`D:\\wordbuddy\\ai-avatar`
> 当前角色：技术负责人 / 架构设计 / 契约制定 / 代码审核 / 测试设计

## 1. 用户长期要求

用户正在开发 AI Avatar Studio，一个 AI 头像生成 SaaS 平台。开发流程固定为：

```text
设计 → 约束 → 实现 → 审核
```

技术负责人（当前 GPT）负责：

- 架构设计和技术决策；
- 数据库、API、错误码、状态机和 Provider 契约；
- 点数预留、消费、释放、退款和人工复核规则；
- 测试设计、代码审核和阶段放行。

国产模型工程 Agent 负责：

- 按 Task Card 编写具体代码；
- 实现普通 API、CRUD、页面、组件、Worker、适配器和测试；
- 修复常规 Bug；
- 每完成一个步骤暂停，等待技术负责人明确授权下一步。

国产模型不得自行改变架构、技术栈、数据库、API、错误码、点数规则、任务状态机或产品范围。

## 2. V1.0 冻结架构

- 前端：Next.js + React + TypeScript；
- 后端：FastAPI + Python；
- 数据库：PostgreSQL；
- 队列和短期状态：Redis；
- 异步任务：Celery Worker + Celery Beat；
- 本地对象存储：MinIO；
- 生产对象存储：COS / OSS / S3，通过 Storage Adapter；
- AI 调用：AIProvider + ProviderRouter，先 MockProvider，再 PrimaryProvider；
- 本地编排：Docker Compose；
- API 契约：FastAPI Pydantic → OpenAPI → 前端生成 TypeScript 客户端；
- 单仓库目录：`frontend/`、`backend/`、`shared/`、`docs/`、`docker/`、`scripts/`。

旧测试版代码存在于仓库根目录，但它不是 V1.0 的迁移、复用或兼容前提。V1.0 按正式系统从零建立，不能把旧 SQLite、旧 Next Route Handler、旧 Node Worker 或旧点数实现当作正式基线。

正式架构文件：

- `docs/architecture/01-overview.md`
- `docs/architecture/02-database-design.md`
- `docs/architecture/03-api-contract.md`
- `docs/architecture/04-ai-provider.md`
- `docs/architecture/05-system-architecture.md`
- `docs/architecture/06-development-standard.md`
- `docs/architecture/07-development-roadmap.md`
- `docs/architecture/09-repository-boundary.md`

早期 `docs/architecture/08-task-cards-and-ownership.md` 只保留为历史参考，正式执行以 `docs/task-cards/` 为准。

## 3. 核心业务不变量

### 3.1 生成任务

```text
CREATED → QUEUED → PROCESSING → SUCCEEDED
                         ├──────→ FAILED
                         └──────→ NEEDS_REVIEW
```

任务状态和账务状态分离。Provider 结果未知或 Provider 可能已扣费但结果无法确认时，任务进入 `NEEDS_REVIEW`，点数保持 `RESERVED`，不得自动退款或自动消费。

### 3.2 点数

```text
预留：available -= cost，reserved += cost，写 RESERVE
成功：reserved -= cost，写 CONSUME
明确失败：reserved -= cost，available += cost，写 RELEASE
UNKNOWN/NEEDS_REVIEW：保持 RESERVED，等待人工决定
```

所有点数变化必须有追加式流水。禁止任何 Router、Worker、Provider 或页面直接修改钱包余额。同一生成任务最多一条最终 `CONSUME` 或 `RELEASE`，所有重复请求、重试和回调必须幂等。

### 3.3 API

- canonical 前缀：`/api/v1`；
- 认证接口：`/api/v1/auth/*`；
- 邀请码兑换：`POST /api/v1/auth/invites/redeem`；
- 当前用户：`GET/PATCH /api/v1/me`；
- 异步生成创建返回 `202 Accepted`；
- 写接口使用 `Idempotency-Key`；
- 错误使用稳定错误码，不使用异常文本作为业务判断。

## 4. 任务卡和授权规则

技术负责人任务卡：

```text
LEAD-01 架构与仓库边界
LEAD-02 PostgreSQL 数据库设计与迁移契约
LEAD-03 REST API、OpenAPI 与错误码契约
LEAD-04 点数账务与生成任务状态机
LEAD-05 AI Provider 与 Storage Adapter 契约
LEAD-06 测试矩阵与验收门禁
LEAD-07 代码审核与阶段放行
```

工程任务卡：

```text
ENG-00 工程环境和目录骨架
ENG-01 PostgreSQL、SQLAlchemy、Alembic 和基础迁移
ENG-02 FastAPI 基础服务、配置、健康检查和 OpenAPI
ENG-03 用户、邀请码、邮箱绑定和 Session
ENG-04 Points Service 和点数流水
ENG-05 Storage Adapter、上传和媒体资源
ENG-06 Generation Service、Celery、Redis 和 MockProvider
ENG-07 PrimaryProvider 和成本审计
ENG-08 Next.js 前端生成工作流和用户页面
ENG-09 分享、下载、反馈和生命周期清理
ENG-10 本地完整闭环和回归测试
```

执行顺序：

```text
LEAD-01 → ENG-00
LEAD-02 + LEAD-03 + LEAD-04 + LEAD-05
→ ENG-01 → ENG-02 → ENG-03 → ENG-04 → ENG-05 → ENG-06
→ ENG-07 → ENG-08 → ENG-09 → ENG-10
→ LEAD-06 → LEAD-07
```

每张卡、每个步骤都必须停线等待审核。没有明确的“通过并授权下一步”，不得继续。

强制协议：`docs/task-cards/ENGINEERING-STRICT-PROTOCOL.md`。

## 5. LEAD-01 审核结果

`LEAD-01` 已完成并审核通过，结果写入：

- `docs/architecture/09-repository-boundary.md`
- `docs/architecture/01-overview.md`
- `docs/architecture/05-system-architecture.md`
- `docs/architecture/06-development-standard.md`
- `docs/task-cards/00-index.md`
- `docs/task-cards/LEAD-01-architecture-baseline.md`
- `docs/task-cards/ENG-00-engineering-foundation.md`

审核时修正了两处文档问题：

1. 删除 `ENG-00` 阶段的 `alembic upgrade head` 启动命令；迁移由 `ENG-01` 负责。
2. 删除 `09-repository-boundary.md` 对早期 `08-task-cards-and-ownership.md` 的正式基线引用。

## 6. ENG-00 当前状态

### 已完成并审核的步骤

`ENG-00` 第 1 步“创建 V1.0 目录骨架”已完成并通过审核。

已创建的 V1.0 骨架包括：

- `frontend/`：README、package.json、next.config.mjs、目录占位；
- `backend/`：README、main.py、config.py、alembic.ini、目录占位；
- `shared/`：README 和契约目录占位；
- `docker/README.md`；
- `scripts/README.md`。

第 1 步没有实现业务代码、ASGI 应用、配置读取、数据库模型、迁移、Provider、Storage 或 Celery 业务任务。

### 已裁决的风险

1. 根目录已有旧测试版 `.env.example`，不得修改。V1.0 唯一环境模板固定为 `docker/.env.example`。
2. 根目录旧 `README.md` 不得改写为两套架构混合文档。V1.0 启动说明放在 `docker/README.md` 和 `docs/architecture/09-repository-boundary.md`。
3. `backend/app/` 的业务域目录由后续工程卡按需创建，`ENG-00` 不创建。最小目录以 `09-repository-boundary.md` 为准。
4. `backend/alembic.ini` 可以保留占位文件，但完整配置、模型和迁移属于 `ENG-01`。

### 当前下一步

授权执行 `ENG-00` 第 2 步：

```text
在 docker/compose.yml 声明 frontend、backend、worker、beat、postgres、redis、minio 七个服务；
配置单一私有网络、命名卷、健康检查和服务连接方向；
worker/beat 复用后端镜像，只替换启动命令；
不实现业务逻辑；
只运行 docker compose ... config 做配置解析检查，不执行 up；
完成后立即暂停并报告。
```

第 2 步还必须把 `docker/README.md` 中的旧启动命令改为：

```powershell
Copy-Item docker/.env.example .env.v1
docker compose --env-file .env.v1 -f docker/compose.yml config
```

`docker/.env.example` 的创建属于 ENG-00 第 3 步，不得在第 2 步提前实现业务配置读取。

## 7. 当前工作区状态

仓库位于 `D:\\wordbuddy\\ai-avatar`。工作区存在许多历史未提交文件和旧测试版文件；新会话不得清理、回滚或覆盖这些文件。当前交接涉及的 V1.0 新文件主要位于 `frontend/`、`backend/`、`shared/`、`docker/` 和 `docs/`。

不要执行：

- `git reset --hard`；
- `git checkout --`；
- 删除旧目录；
- 修改旧根目录 `.env.example` 或 `README.md`；
- 数据库迁移；
- Docker Compose `up`；
- 真实 Provider 调用；
- Git commit。

## 8. 新会话启动指令

新会话先读取本文件，再读取本文件中列出的架构和任务卡。新会话的第一条工作回复必须确认：

```text
已读取会话交接文档。
当前阶段：正式开发阶段，LEAD-01 已通过。
当前卡片：ENG-00。
当前停点：ENG-00 第 1 步已通过，准备执行第 2 步。
本次只修改：docker/compose.yml 和必要的 docker/README.md 启动说明。
本次不做：业务代码、数据库模型、迁移、真实 Provider、Docker up、Git commit。
```

然后等待或执行用户明确授权的 `ENG-00` 第 2 步，完成后再次暂停。
