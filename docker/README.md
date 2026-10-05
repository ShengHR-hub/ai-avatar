# docker/ — 本地编排与部署顺序

> 依据：`docs/architecture/09-repository-boundary.md` 第 5 节
> 状态：本地编排、迁移与初始化的**实际顺序**已落到本文；不再只是骨架占位。

## 职责

只描述构建、网络、卷、健康检查、启动顺序与**部署期必须执行的命令**；
不在 Compose 文件中编写业务逻辑。

## 规范文件

- `compose.yml`：本地开发环境唯一编排文件（七个服务：`frontend`、`backend`、`worker`、`beat`、`postgres`、`redis`、`minio`）；
- `../backend/alembic.ini`：迁移入口（`alembic upgrade head`）；
- `../backend/app/bootstrap/cost_configs.py`：**点数价格初始化**的可重跑命令。

## 连接方向

```text
浏览器 ──同源──→ frontend :3000 ──Next 服务端代理──→ backend :8000
backend  → postgres / redis / minio
worker   → postgres / redis / minio / 外部 Provider
beat     → redis
```

浏览器**不直连** `backend:8000`：契约的 `paths` 自带 `/api/v1`，而 `localhost:3000`
与 `localhost:8000` 在浏览器里是跨源（同站、不同源）。后端没有也不应新增 CORS 中间件，
因此前端固定使用**相对同源**地址 `/api/v1`，由 Next 的 `rewrites` 在服务端转发。

## 环境变量文件

| 文件 | 定位 |
|---|---|
| `docker/.env.example` | **唯一 V1.0 环境变量模板**，只含占位值，可提交到仓库 |
| 根目录 `.env.v1` | **根目录本地实际配置文件**，由模板复制而来，已被 `.gitignore` 忽略，不得提交 |
| 根目录 `.env.example`、根目录 `README.md` | 旧测试版（V0.1）文件，**不属于 V1.0 配置入口**，不得覆盖或复用 |

首次使用前先由模板生成本地配置文件：

```powershell
Copy-Item docker/.env.example .env.v1
```

### 前端两个变量不能混用

| 变量 | 生效位置 | 取值 | 改它要不要重新构建 |
|---|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | 浏览器 bundle（构建期内联） | 相对路径 `/api/v1` | 要（值已写进产物） |
| `API_PROXY_TARGET` | Next 服务端进程 + `routes-manifest.json` | Compose：`http://backend:8000`；宿主 `next start`：`http://127.0.0.1:8100` | 要（rewrites 在构建期固化） |

两个值都必须是「构建期就确定」的，这是 Next 的既有约束，不是本项目的选择：
`NEXT_PUBLIC_*` 会被静态替换进客户端代码，`rewrites()` 的结果会写进构建产物。
把 `NEXT_PUBLIC_API_BASE_URL` 写成绝对地址（例如 `http://localhost:8000/api/v1`）
等于让浏览器直连后端：跨源、无 CORS、Cookie 与凭据请求都会失败。

## 规范启动顺序

**顺序不能换**：基础设施 → 迁移 → 显式定价 → 应用进程。跳过第 3 步或第 4 步，
服务能起来但用户第一次生成会失败（见下文「为什么定价必须显式」）。

```powershell
# 1. 配置
Copy-Item docker/.env.example .env.v1
docker compose --env-file .env.v1 -f docker/compose.yml config

# 2. 基础设施 + 应用
docker compose --env-file .env.v1 -f docker/compose.yml up --build -d

# 3. 迁移：空库 -> head（幂等，可重复执行）
docker compose --env-file .env.v1 -f docker/compose.yml exec backend alembic upgrade head

# 4. 点数价格初始化：**必须显式给出每一档价格**
docker compose --env-file .env.v1 -f docker/compose.yml exec backend `
  python -m app.bootstrap.cost_configs --price TEXT_PROMPT=<点数> --price PHOTO_PROMPT=<点数>

# 5. 核对
docker compose --env-file .env.v1 -f docker/compose.yml ps
curl.exe -s http://localhost:8000/api/v1/health/ready
```

宿主机直接跑后端与前端（不经 Compose 时的常用形状，示例端口 8100 避开其他项目占用的 8000）：

```powershell
# 后端（示例端口 8100，避开其他项目占用的 8000）
$env:DATABASE_URL="postgresql+psycopg://ai_avatar:<密码>@127.0.0.1:5432/inspo_local"
python -m alembic upgrade head
python -m app.bootstrap.cost_configs --price TEXT_PROMPT=<点数> --price PHOTO_PROMPT=<点数>
python -m uvicorn app.main:app --host 127.0.0.1 --port 8100

# 前端（构建期就要给两个变量）
$env:NEXT_PUBLIC_API_BASE_URL="/api/v1"
$env:API_PROXY_TARGET="http://127.0.0.1:8100"
npm run build ; npm run start -- -p 3010
```

## 为什么点数价格必须显式传入

`generation_cost_configs` 是**唯一**的用户点数价格来源（`02-database-design.md` 第 3.2 节）。
迁移只建表、**不填价格**：产品定价一旦确定不下来，代码里的任何「默认 20 点」都会
变成事实上的定价，也就是凭空记账。因此：

- `python -m app.bootstrap.cost_configs` **没有默认价格**，至少要给一项 `--price MODE=<点数>`；
  帮助文本里也只有 `<N>` 占位，不提供可以照抄的数字；
- 未初始化时创建生成任务返回 `503 NOT_READY`，`details.reason = "cost_config_missing"`，
  并带上可执行的补救命令——这是「部署少跑了一步」，不是随机 5xx；
- `/api/v1/health/ready` 的 `generation_costs` 探针用**同一套取值规则**核对：
  当前配置下应当能定价的模式（`TEXT_PROMPT`、`PHOTO_PROMPT`，以及开启
  `ENABLE_REFERENCE_IMAGE` 时的 `REFERENCE_IMAGE`）缺任一价格，`/ready` 就是 `503`。
  运维因此能在用户第一次失败之前看到缺口；
- 重复执行同价只报告 `unchanged`，不写库（部署脚本可无脑重跑）；
- **调价是显式动作**：同一 `(mode, model_name, output_count)` 已有生效价格且不同价时，
  必须加 `--allow-reprice`；执行方式是「关闭旧窗口 + 追加新行」，历史任务的
  `cost_config` 引用与 `reserved_points` 不会被改写；
- 先看计划不写库：`--dry-run`。

```powershell
python -m app.bootstrap.cost_configs --dry-run --price TEXT_PROMPT=<点数>
python -m app.bootstrap.cost_configs --price TEXT_PROMPT=<点数> --price PHOTO_PROMPT=<点数>
python -m app.bootstrap.cost_configs --allow-reprice --price TEXT_PROMPT=<新点数>
```

## 迁移链与共享库

- 迁移是**追加式**的：`0001` 起到当前 head 逐条前进，已应用的迁移不得修改；
- `alembic upgrade head` 对已到 head 的库是空操作，可重复执行；
- `0020_analysis_dispatches` 是「参考图分析」每日付费上限的持久登记表。它从空表开始，
  **不回填**历史：0020 之前的出网次数只在 Redis 里计过，无法可信复原，因此上线当天的
  已用额度会**重置一次**。这是一次性代价，不伪造回填；
- 共享开发库 `ai_avatar` 的迁移属于写库动作，必须单独授权；
  测试与验证一律使用临时隔离数据库（`tests/db` 会自己建库、迁移、用完即删）。

## 停止

```powershell
docker compose --env-file .env.v1 -f docker/compose.yml down
```

除非明确要重置本地数据，**不得使用 `down -v`**（会删除命名卷，清空 PostgreSQL、Redis 与 MinIO 的本地数据）。

## 当前状态

- `compose.yml` 已建立（ENG-00 步骤 2），是本地开发环境唯一编排文件；
  迁移与定价初始化命令已按上文加入启动流程。
- `backend` 健康检查路径已改为契约路径 `/api/v1/health/ready`
  （此前写作 `/health/ready`，容器健康检查会永远 404）；
  `frontend` 已接入同源代理（`NEXT_PUBLIC_API_BASE_URL=/api/v1` + `API_PROXY_TARGET`）。
- **镜像构建已补齐（ENG-10）**：`backend/Dockerfile`、`frontend/Dockerfile` 与各自的
  `.dockerignore` 已定义，`compose.yml` 的 `backend` 与 `frontend` 增加了 `build` 段，
  因此 `docker compose up --build -d` 可以直接构建。
  `backend` / `worker` / `beat` 共用 `ai-avatar-backend:v1`（同一份服务端代码、
  不同启动命令），Compose 只构建一次。
- 前端两个变量是**构建期**值：`compose.yml` 通过 `build.args` 注入，
  改它们必须重新构建（`--build`），运行期 `-e` 无效。

### 用 Compose 前必须核对 `API_PROXY_TARGET`

`docker/.env.example` 的默认值是 `http://backend:8000`（容器网络内的服务名），
**Compose 场景必须用这个值**。宿主机直跑前端时才改成 `http://127.0.0.1:<后端端口>`。

本仓库根目录的 `.env.v1` 是**本地实际配置文件**（已被 `.gitignore` 忽略），
它可能为了宿主机直跑而被改成 `127.0.0.1:8100`；此时直接
`docker compose --env-file .env.v1 ... up --build` 会把**容器内**的 127.0.0.1
当成后端地址，前端页面的 `/api/v1/*` 会全部 502/连不上。核对方法：

```powershell
docker compose --env-file .env.v1 -f docker/compose.yml config |
  Select-String "API_PROXY_TARGET"
# 期望：http://backend:8000
```
