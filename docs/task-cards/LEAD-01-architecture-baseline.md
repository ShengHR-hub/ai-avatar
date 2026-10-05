# LEAD-01：V1.0 架构与仓库边界

## 会话目的

在独立对话中冻结正式 V1.0 的目录、进程、依赖关系、环境变量和本地启动方式。旧测试版只作为经验参考，不作为代码迁移目标。

## 技术负责人负责

- 确认 Next.js、FastAPI、PostgreSQL、Redis、Celery、MinIO、Docker Compose；
- 确认 `frontend/`、`backend/`、`shared/`、`docs/`、`docker/`、`scripts/` 边界；
- 确认 API、Worker、Beat、数据库、队列和对象存储的依赖方向；
- 定义密钥只存在服务端环境变量的规则；
- 编写目录、进程和启动验收标准。

## 输出

- 架构决策记录；
- 目标目录树；
- 进程拓扑；
- 环境变量清单；
- `ENG-00` 可直接执行的实现说明（见 `docs/task-cards/ENG-00-engineering-foundation.md`）。

## 冻结结果

正式基线见 `docs/architecture/09-repository-boundary.md`，并由 `01-overview.md`、`05-system-architecture.md` 和 `06-development-standard.md` 引用。早期的 `08-task-cards-and-ownership.md` 不再作为执行依据。冻结内容包括：

1. `frontend/`、`backend/`、`shared/`、`docs/`、`docker/`、`scripts/` 的职责和禁止越界项；
2. Next.js、FastAPI、PostgreSQL、Redis、Celery Worker、Celery Beat、MinIO 和 Docker Compose 的职责；
3. API → Service → Repository/Adapter → 基础设施，以及 Beat → Redis → Worker 的依赖方向；
4. 服务端变量、Compose 初始化变量、前端公开变量和结构化日志脱敏边界；
5. `docker/compose.yml` 的七服务本地启动拓扑和默认 MockProvider 配置。

本卡不改变数据库字段、API 路径、错误码、任务状态机、计费状态机或点数规则；不创建代码目录、不移动旧测试版文件、不运行迁移、不启动服务、不提交 Git。

## 不负责

不写业务 CRUD、页面和 Provider 代码，不为了适配旧代码而改变 V1.0 选型。

## 审核门禁

只有目录边界、依赖方向、启动命令、环境变量和安全边界均明确，且 `ENG-00` 卡片已可直接执行，才能授权 `ENG-00`。

## 下一步

通过后进入 `ENG-00`；同时可开启 `LEAD-02`、`LEAD-03`、`LEAD-04`、`LEAD-05` 的契约设计。`ENG-00` 完成后必须停下等待审核。
