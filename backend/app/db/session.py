"""SQLAlchemy 2.x 引擎与会话管理（ENG-01 第 1 步）。

边界：

- 只负责引擎、sessionmaker 和会话生命周期，不实现业务逻辑、不写业务数据；
- 事务边界由 Service 控制，Repository 不得自行 `commit`
  （见 `docs/architecture/06-development-standard.md` 第 2 节）；
- 引擎惰性创建：导入本模块不要求 `DATABASE_URL` 已配置。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_database_url

__all__ = [
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_session_factory",
]

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """返回进程级复用的 SQLAlchemy 引擎。"""

    global _engine
    if _engine is None:
        _engine = create_engine(get_database_url(), pool_pre_ping=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """返回 sessionmaker；Service 用它自行控制事务边界。"""

    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _session_factory


def get_session() -> Iterator[Session]:
    """请求级会话生成器，供上层依赖注入使用；退出时关闭会话。

    本函数不提交事务，事务边界由调用方（Service）决定。
    """

    with get_session_factory()() as session:
        yield session


def dispose_engine() -> None:
    """释放连接池并清空缓存，供测试与进程退出使用。"""

    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
