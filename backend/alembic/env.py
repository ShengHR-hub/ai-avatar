"""Alembic 运行环境（ENG-01 第 2 步）。

职责：

- 从服务端环境变量读取连接串（不写入 `alembic.ini`，避免凭据进入仓库）；
- 把 `Base.metadata` 暴露为 `target_metadata`，供后续 autogenerate 比较；
- 同时支持在线模式与离线模式（`--sql` 只生成 SQL，不连接数据库）。

边界：本文件不实现业务逻辑，不修改 Schema 语义。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.db.models  # noqa: F401  导入以注册全部 20 张表
from app.config import get_database_url
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不建立数据库连接。"""

    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库并执行迁移。"""

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
