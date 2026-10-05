"""外部依赖端口（ENG-03 第 1 步）。

本模块定义 ENG-03 与外部世界之间的三个**可注入端口**，以及它们的默认实现。
端口是 ENG-03 与后续卡片的跨卡契约，签名一旦冻结，ENG-04/ENG-05/ENG-06 应保持不变。

| 端口 | 责任 | 默认实现 | 归属 |
|---|---|---|---|
| `RedisPort` | 会话与邮箱令牌的键值存储 | `RedisClientAdapter`（`redis>=5.2`，惰性连接） | ENG-03 |
| `PointsPort` | 邀请兑换的**完整原子操作** | `TransactionalPointsPort`（一个工作单元一次提交） | ENG-04 实现 |
| `EmailPort` | 发送邮箱验证邮件 | `UnavailableEmailPort`（抛 `EmailPortUnavailable`） | 后续邮件卡实现 |

设计约束（技术负责人 2026-09-26 裁决 ②③，ENG-04 第 2 步接线）：

- **ENG-03 不直接修改 wallet 或 `point_ledger`**：邀请兑换的用户创建/恢复、兑换记录、
  `CREDIT` 流水与 Session 创建全部由 `PointsPort.redeem_invite()` 协调完成；
  路由与 `RedeemService` 中不得出现任何账务逻辑；
- Points 端口默认已接入真实实现；`UnavailablePointsPort` 保留给测试显式注入，
  用于验证「依赖未就绪 → `NOT_READY` / HTTP 503」这条映射仍然成立；
- **装配默认端口时不建立 PostgreSQL 引擎、不连接 Redis**：数据库工作单元在
  `redeem_invite()` 内部才创建，Redis 客户端仅在首次命令时连接；
- **本卡不启动 Redis**：`RedisPort` 在测试中用内存 fake 注入；生产实现为 `redis>=5.2`；
- 邮件端口不接入真实供应商，测试使用 `InMemoryEmailSink`；
- 日志不得记录会话令牌、Redis key 或完整 Cookie。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.schemas.user import PointBalance, Session, User

__all__ = [
    "EmailPort",
    "EmailPortUnavailable",
    "InMemoryEmailSink",
    "PointsPort",
    "PointsPortUnavailable",
    "RedeemOutcome",
    "RedisClientAdapter",
    "RedisPort",
    "TransactionalPointsPort",
    "UnavailableEmailPort",
    "UnavailablePointsPort",
    "get_email_port",
    "get_points_port",
    "get_redis_port",
    "reset_port_providers",
]


class PointsPortUnavailable(RuntimeError):
    """Points 端口尚未接入可调用实现（ENG-04）。调用方应映射为 `NOT_READY` / 503。"""


class EmailPortUnavailable(RuntimeError):
    """邮件端口尚未接入实现。调用方应映射为 `NOT_READY` / 503。"""


@dataclass(frozen=True, slots=True)
class RedeemOutcome:
    """邀请兑换的原子结果。

    `PointsPort.redeem_invite()` 在一个数据库事务内完成用户创建/恢复、邀请码兑换记录
    与 `CREDIT` 流水，并协调会话创建；本对象是其对外结果。
    """

    user: User
    points: PointBalance
    session: Session
    session_token: str


@runtime_checkable
class PointsPort(Protocol):
    """冻结的 Points Service 边界（ENG-04 实现，ENG-03 只调用）。"""

    def redeem_invite(
        self,
        *,
        invite_code: str,
        email: str,
        session_ttl_seconds: int,
    ) -> RedeemOutcome:
        """原子完成邀请码兑换。

        实现方必须保证：用户创建/恢复、邀请码兑换记录、`CREDIT` 流水在同一事务内完成，
        并创建会话；失败时整体回滚。
        """
        ...


class UnavailablePointsPort:
    """显式注入的「未接入」实现：明确抛出依赖未就绪，绝不复制 Points Service 逻辑。

    ENG-04 第 2 步后不再是进程默认值，只用于测试与运维演练
    （验证 `PointsPortUnavailable` → `NOT_READY` / 503 的映射）。
    """

    def redeem_invite(
        self,
        *,
        invite_code: str,
        email: str,
        session_ttl_seconds: int,
    ) -> RedeemOutcome:
        raise PointsPortUnavailable(
            "Points Service 端口未接入；邀请码兑换暂不可用。"
        )


@runtime_checkable
class EmailPort(Protocol):
    """冻结的邮件发送边界。"""

    def send_verification_email(
        self,
        *,
        to_email: str,
        token: str,
        expires_in_seconds: int,
    ) -> None:
        """发送邮箱验证邮件；实现方不得把令牌写入日志。"""
        ...


class UnavailableEmailPort:
    """默认实现：邮件供应商尚未接入，明确抛出依赖未就绪。"""

    def send_verification_email(
        self,
        *,
        to_email: str,
        token: str,
        expires_in_seconds: int,
    ) -> None:
        raise EmailPortUnavailable("邮件供应商尚未接入；邮箱验证邮件暂不可用。")


class InMemoryEmailSink:
    """测试/本地用邮件端口：只记录发送请求，不真正发信。

    **不得用于生产**：它不会投递任何邮件。
    """

    def __init__(self) -> None:
        self._sent: list[dict[str, Any]] = []

    def send_verification_email(
        self,
        *,
        to_email: str,
        token: str,
        expires_in_seconds: int,
    ) -> None:
        self._sent.append(
            {
                "to_email": to_email,
                "token": token,
                "expires_in_seconds": expires_in_seconds,
            }
        )

    @property
    def sent(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._sent)

    def clear(self) -> None:
        self._sent.clear()


@runtime_checkable
class RedisPort(Protocol):
    """冻结的 Redis 客户端端口（仅暴露本卡需要的子集）。"""

    def set(self, name: str, value: str, *, ex: int | None = None) -> Any:
        """写入键值；`ex` 为秒级 TTL。"""
        ...

    def get(self, name: str) -> str | None:
        """读取键值；不存在返回 `None`。"""
        ...

    def delete(self, *names: str) -> int:
        """删除键；返回被删除的数量。"""
        ...

    def expire(self, name: str, time: int) -> bool:
        """重设 TTL（用于会话滑动续期；本卡不启用滑动续期）。"""
        ...


class RedisClientAdapter:
    """生产实现：`redis>=5.2` 的同步客户端。

    - 构造时**不建立连接**（`from_url` 只创建连接池），首次命令才连接；
    - 本卡不启动 Redis，因此该实现不在本卡的运行验证范围内，
      由基础设施集成阶段验证；
    - 端点使用同步 `def`（FastAPI 在线程池中执行），与同步 SQLAlchemy 栈保持一致。
    """

    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url, decode_responses=True)

    def set(self, name: str, value: str, *, ex: int | None = None) -> Any:
        return self._client.set(name, value, ex=ex)

    def get(self, name: str) -> str | None:
        return self._client.get(name)

    def delete(self, *names: str) -> int:
        return int(self._client.delete(*names))

    def expire(self, name: str, time: int) -> bool:
        return bool(self._client.expire(name, time))

    def ping(self) -> bool:
        """连通性探测（供后续 readiness 探针使用）。"""

        return bool(self._client.ping())


# --------------------------------------------------------------------------- #
# 端口提供者（进程级缓存；测试通过 app.dependency_overrides 或直接替换）
# --------------------------------------------------------------------------- #
_redis_port: RedisPort | None = None
_points_port: PointsPort | None = None
_email_port: EmailPort | None = None


def get_redis_port() -> RedisPort:
    """返回进程级 Redis 端口；默认实现为 `RedisClientAdapter`（惰性连接）。"""

    global _redis_port
    if _redis_port is None:
        from app.config import get_settings

        _redis_port = RedisClientAdapter(get_settings().redis_url)
    return _redis_port


def _sqlalchemy_unit_of_work() -> Any:
    """默认工作单元工厂：**调用时**才解析引擎并开启一个 SQLAlchemy 工作单元。

    `get_points_port()` 只保存本函数引用，因此装配默认端口不会创建引擎、
    不会连接 PostgreSQL；`SqlAlchemyUnitOfWork.__exit__` 在异常时回滚。
    """

    from app.db.session import get_session_factory
    from app.repositories.unit_of_work import SqlAlchemyUnitOfWork

    return SqlAlchemyUnitOfWork(get_session_factory())


def _default_session_store() -> Any:
    """默认会话存储：与 `app.api.deps.get_session_store` 同一装配方式。

    `RedisClientAdapter` 只创建连接池、不建立连接（见
    `test_redis_client_adapter_constructs_without_connecting`），因此这里不触碰 Redis。
    """

    from app.config import get_settings
    from app.services.auth.session_store import RedisSessionStore

    return RedisSessionStore(get_redis_port(), secret=get_settings().session_secret)


def get_points_port() -> PointsPort:
    """返回 Points 端口；默认实现为 `TransactionalPointsPort`（ENG-04 第 2 步）。

    装配过程不连接 PostgreSQL、不连接 Redis；测试通过
    `app.api.deps.get_points_port_dep` 的依赖覆盖，或直接注入
    `UnavailablePointsPort` 来替换默认实现。
    """

    global _points_port
    if _points_port is None:
        _points_port = TransactionalPointsPort(
            uow_factory=_sqlalchemy_unit_of_work,
            session_store=_default_session_store(),
        )
    return _points_port


def get_email_port() -> EmailPort:
    """返回邮件端口；供应商未接入前为 `UnavailableEmailPort`（不静默丢信）。"""

    global _email_port
    if _email_port is None:
        _email_port = UnavailableEmailPort()
    return _email_port


def reset_port_providers() -> None:
    """清空端口缓存，供测试替换实现后重新获取。"""

    global _redis_port, _points_port, _email_port
    _redis_port = None
    _points_port = None
    _email_port = None


class TransactionalPointsPort:
    """`PointsPort` 的真实实现（ENG-04 第 2 步）。

    - 每个 `redeem_invite()` 调用打开**一个**工作单元，成功即提交、异常即回滚；
    - 账务规则全部在 `app.services.points.service`，本类只负责事务边界与装配；
    - 依赖 `uow_factory`（返回 `UnitOfWork` 上下文管理器）而非具体 Session，
      因此可在无 PostgreSQL 环境下注入内存替身测试；
    - `PointsPort` 的协议签名保持不变。
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], Any],
        session_store: Any,
    ) -> None:
        self._uow_factory = uow_factory
        self._session_store = session_store

    def redeem_invite(
        self,
        *,
        invite_code: str,
        email: str,
        session_ttl_seconds: int,
    ) -> RedeemOutcome:
        # 延迟导入，避免 `service` ↔ `ports` 的循环依赖
        from app.services.points.service import InviteRedeemService

        with self._uow_factory() as uow:
            service = InviteRedeemService(
                uow=uow,
                session_store=self._session_store,
            )
            return service.redeem_invite(
                invite_code=invite_code,
                email=email,
                session_ttl_seconds=session_ttl_seconds,
            )
