"""ENG-06 Celery 编排层测试夹具。

冻结依据（本目录全部断言的来源）：

- `docs/architecture/05-system-architecture.md` 第 3 节：进程模型
  （`celery -A app.tasks.celery_app worker|beat`、Beat 只向 Redis 投递、
  Beat 与 Worker 都不直接写 PostgreSQL/MinIO/点数账务）；
- `docs/task-cards/LEAD-04-points-and-state-machine.md` 第 6 节（投递失败不回滚
  已提交账务、事务失败可安全重投）与第 11 节（Task 只解析参数、建立上下文、调用 Service）；
- `docs/architecture/04-ai-provider.md` 第 3 节（connect 10s / read 180s /
  整体处理上限 240s，且每次重试都留下独立 `provider_attempts` 行）。

设计要点：

- `app/config.py` 是 fail-fast 的（缺 `DATABASE_URL` 即抛 `ConfigError`），
  而 `app.tasks.celery_app` **在导入时**就会读取配置，因此本文件在导入应用之前
  注入占位连接串（与 `tests/api/conftest.py` 同一先例）；
- 本目录不启动 Worker/Beat、不连 Redis/Broker、不连数据库：投递侧用
  `send_task` 记录器替身，Service 装配侧用 `build_orchestrator` 记录器替身，
  Redis 侧复用 `tests.services.fakes.FakeRedis`（`RedisPort` 的内存实现）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from types import ModuleType

# `app.tasks.celery_app` 导入时读取 `get_settings()`，而 `DATABASE_URL` 为必填项；
# 在导入应用之前注入占位连接串，测试不依赖真实数据库。
PLACEHOLDER_DATABASE_URL = (
    "postgresql+psycopg://ai_avatar:placeholder@postgres:5432/ai_avatar"
)
os.environ.setdefault("DATABASE_URL", PLACEHOLDER_DATABASE_URL)

import pytest  # noqa: E402

from app.config import reset_settings_cache  # noqa: E402
from app.tasks.celery_app import celery_app  # noqa: E402,F401  (导入即装配并注册任务)
from tests.services.fakes import FakeRedis, MutableClock  # noqa: E402

#: `app.tasks.celery_app` 的模块名（`celery -A` 用的就是这个点分路径）
CELERY_APP_MODULE = "app.tasks.celery_app"

#: 稳定时钟起点（**带 UTC 时区**）：心跳与 TTL 断言不得依赖用例的实际运行时刻，
#: 也不得让 `datetime.timestamp()` 按进程本地时区解释裸时间。
FIXED_NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> MutableClock:
    """固定起点的可推进时钟（`FakeRedis` 的 TTL 判定与心跳时间戳共用它）。"""

    return MutableClock(FIXED_NOW)


@pytest.fixture
def fake_redis(clock: MutableClock) -> FakeRedis:
    """`RedisPort` 的内存实现（只覆盖 set/get/delete/expire 子集，不触网）。"""

    return FakeRedis(clock)


@pytest.fixture
def celery_module() -> ModuleType:
    """`app.tasks.celery_app` **模块**。

    注意 `app/tasks/__init__.py` 把同名属性重新绑定成了 Celery 实例，
    `from app.tasks import celery_app` 得到的不是模块；这里显式走模块路径，
    测试因此既能读模块级常量（`BEAT_SCHEDULE`、各时限常量），也能读实例。
    """

    module = sys.modules[CELERY_APP_MODULE]
    assert isinstance(module, ModuleType), f"{CELERY_APP_MODULE} 未被导入"
    assert module.celery_app is celery_app, "模块属性与包属性必须是同一个 Celery 实例"
    return module


@pytest.fixture
def celery(celery_module: ModuleType) -> object:
    """被测 Celery 实例（`celery -A app.tasks.celery_app` 解析到的就是它）。"""

    return celery_module.celery_app


@pytest.fixture(autouse=True)
def _restore_process_state() -> Iterator[None]:
    """每个用例结束后清掉进程级配置缓存。

    部分用例为验证「broker URL 来自 `get_settings().redis_url`」会临时改写
    `REDIS_URL` 并重建 Celery 实例；`app.config._settings` 是进程级缓存，
    用例之间不复位就会污染后续测试（含其他目录的用例）。
    """

    yield
    reset_settings_cache()
