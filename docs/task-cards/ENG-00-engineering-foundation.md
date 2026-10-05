# ENG-00：工程环境和目录骨架

> 前置：`LEAD-01` 通过
> 负责人：工程 Agent
> 状态：待授权
> 目标：建立 V1.0 可启动的单仓库骨架，不实现业务功能

## 1. 开始前必须阅读

1. `docs/architecture/01-overview.md` 至 `09-repository-boundary.md`；
2. `docs/task-cards/00-index.md`、`docs/task-cards/ENGINEERING-STRICT-PROTOCOL.md` 和本卡；
3. `docs/architecture/03-api-contract.md`、`04-ai-provider.md` 中与进程边界有关的部分。

开始执行前先回复：

```text
准备执行：ENG-00
允许修改文件：frontend/、backend/、shared/、docker/、scripts/、README.md（仅启动说明）
依赖和门禁：LEAD-01 已通过；不改数据库字段、API 路径、错误码、任务状态机和点数规则
本卡不做：业务 CRUD、页面功能、Provider、迁移、数据搬运、旧代码迁移
等待技术负责人授权：是
```

没有明确授权不得开始改文件。

## 2. 允许修改和禁止修改

允许新增 V1.0 目录骨架、容器构建文件、Compose 文件、服务启动入口占位、公开环境变量模板和工程检查脚本。允许在 `README.md` 增加 V1.0 启动命令，但不得把旧测试版说明改写成迁移方案。

禁止：

- 修改 `docs/architecture/02-database-design.md` 的字段或约束；
- 修改 `docs/architecture/03-api-contract.md` 的路径、错误码或响应；
- 修改 `04-ai-provider.md` 的接口、错误分类或 Provider 行为；
- 实现用户、点数、生成任务、上传、分享、反馈或任何业务 CRUD；
- 编写真实 Provider、Storage 业务适配、Celery 业务 Task 或数据库迁移；
- 复制、移动、删除或改造仓库当前旧测试版文件；
- 运行迁移、启动服务、提交 Git，除非技术负责人在当前卡片明确授权对应验证。

## 3. 具体执行步骤

### 步骤 1：创建 V1.0 目录骨架

按 `docs/architecture/09-repository-boundary.md` 创建 `frontend/`、`backend/`、`shared/`、`docker/`、`scripts/` 及其规定的子目录。空目录用简短的 `README.md` 或 `.gitkeep` 保留；不要把业务代码放入占位文件。

至少建立以下边界文件：

```text
frontend/package.json
frontend/next.config.*
frontend/app/README.md
frontend/services/README.md
backend/app/main.py
backend/app/config.py
backend/app/api/README.md
backend/app/services/README.md
backend/app/repositories/README.md
backend/app/tasks/README.md
backend/alembic.ini
shared/api-contract/README.md
docker/compose.yml
scripts/README.md
```

文件名后缀可按所选工具链调整，但目录职责不得改变。`main.py` 只需保留进程入口约定，不实现业务路由；数据库模型和迁移留给 `ENG-01`，健康检查和 OpenAPI 留给 `ENG-02`。

### 步骤 2：建立 Compose 服务和连接

在 `docker/compose.yml` 声明 `frontend`、`backend`、`worker`、`beat`、`postgres`、`redis`、`minio` 七个服务，配置单一私有网络、命名卷和健康检查。`worker`、`beat` 复用后端镜像，只替换进程命令；不得在 Compose 中加入第二套数据库、队列或对象存储。

连接必须符合以下方向：

```text
frontend → backend
backend  → postgres / redis / minio
worker   → postgres / redis / minio / 外部 Provider（本卡只留配置入口）
beat     → redis
```

本卡使用 `AI_PROVIDER=mock` 和 `ENABLE_REFERENCE_IMAGE=false` 的默认值。真实凭据只通过服务端变量注入，不得写入 Compose 文件或前端配置。

### 步骤 3：建立环境变量模板

提供 `.env.example` 的 V1.0 变量分组和注释，至少列出以下名称：

```text
服务端：APP_ENV、APP_BASE_URL、DATABASE_URL、REDIS_URL、STORAGE_DRIVER、STORAGE_ENDPOINT、STORAGE_BUCKET、STORAGE_REGION、STORAGE_ACCESS_KEY、STORAGE_SECRET_KEY、AI_PROVIDER、AI_BASE_URL、AI_MODEL、AI_API_KEY、SESSION_SECRET、ENABLE_REFERENCE_IMAGE
Compose：POSTGRES_DB、POSTGRES_USER、POSTGRES_PASSWORD、MINIO_ROOT_USER、MINIO_ROOT_PASSWORD
前端公开：NEXT_PUBLIC_API_BASE_URL、NEXT_PUBLIC_APP_URL
```

模板只能包含占位值；真实 `.env` 不得提交。不要把任何服务端变量改名为 `NEXT_PUBLIC_*`。

### 步骤 4：建立最小工程检查

在 `scripts/` 增加只读检查或 Compose 辅助命令，验证：目录存在、Compose 配置可解析、必需变量已提供、前端公开变量不含敏感名称。脚本不得连接真实 Provider、写业务数据或执行迁移。

### 步骤 5：记录启动说明

在 `docker/README.md` 或 `docs/architecture/09-repository-boundary.md` 中保留以下规范命令。不得修改根目录旧测试版 `README.md`，不得使用根目录旧 `.env.example`：

```powershell
Copy-Item docker/.env.example .env.v1
docker compose --env-file .env.v1 -f docker/compose.yml config
docker compose --env-file .env.v1 -f docker/compose.yml up --build -d
docker compose --env-file .env.v1 -f docker/compose.yml ps
```

数据库迁移命令由 `ENG-01` 补充，本卡不得执行或实现迁移。

## 4. 验收标准

1. 目标目录与 `09-repository-boundary.md` 一致，旧测试版文件未被移动、复制或修改。
2. `docker/.env.example` 是唯一 V1.0 环境模板；根目录旧 `.env.example` 和 `README.md` 未被修改。`docker compose --env-file .env.v1 ... config` 成功，七个服务名称和连接方向正确。
3. `frontend`、`backend`、`worker`、`beat` 的启动命令已明确，Worker/Beat 没有对外 HTTP 端口。
4. PostgreSQL、Redis、MinIO 使用命名卷和健康检查；没有第二套事实存储。
5. `.env.example` 只含占位值；服务端密钥不会出现在前端公开变量、Compose 明文业务配置或日志配置中。
6. 检查脚本只做结构和配置校验，不写数据库、不调用 Provider、不运行迁移。
7. 没有新增数据库字段、API 路径、错误码、任务状态机或点数规则。

## 5. 回报格式与停线

完成后只报告并暂停：

```text
Task Card：ENG-00
改动文件：
目录/进程变更：
Compose 配置检查命令与结果：
环境变量检查结果：
是否修改数据库/API/Provider/业务实现：否
未完成事项：
风险或待确认问题：
请求技术负责人审核：是
```

没有技术负责人明确的“通过并授权下一步”，不得开始 `ENG-01` 或任何业务实现。
