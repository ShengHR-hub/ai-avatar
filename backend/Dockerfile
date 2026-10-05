# AI Avatar Studio V1.0 — 后端镜像（ENG-10）
#
# 同一个镜像服务三个进程（`backend` / `worker` / `beat`）：它们是**同一份服务端代码**，
# 只是启动命令不同（`05-system-architecture.md` 第 3 节、`09-repository-boundary.md` 第 5 节）。
# 因此这里不写 `CMD` 之外的进程选择逻辑，命令由 `docker/compose.yml` 的 `command` 给出。
#
# 边界：
# - 只安装 `requirements.txt` 声明的依赖；不装编译器（`psycopg[binary]`、`Pillow`
#   都有 manylinux wheel），因此不需要 gcc 与 libpq-dev；
# - 不把 `.env`、密钥或测试数据复制进镜像（`.dockerignore` 已排除）；
# - `curl` 只用于 Compose 健康检查（`compose.yml` 的 backend healthcheck 用 curl 打
#   契约路径 `/api/v1/health/ready`），因此它是运行期依赖而不是调试工具；
# - 迁移与定价初始化**不在**镜像启动时执行：它们是显式的部署步骤
#   （`docker/README.md`「规范启动顺序」第 3、4 步），由运维在 `exec` 里跑。

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 健康检查依赖 curl；`--no-install-recommends` + 清 apt 列表控制体积
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只复制依赖清单，让依赖层在源码变更时仍然命中缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 服务端代码 + 迁移。`alembic.ini` 里的 `script_location` 指向 `alembic/`，
# 因此两者必须一起进镜像，否则 `alembic upgrade head` 在容器里找不到迁移。
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

EXPOSE 8000

# 默认命令 = API 进程；worker/beat 由 compose 覆盖 `command`
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
