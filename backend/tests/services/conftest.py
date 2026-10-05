"""ENG-03 Service 层测试夹具。

测试替身定义在 `tests.services.fakes`（便于测试模块直接导入类型），
本文件只负责把它们包装成 pytest 夹具。

ENG-06 第 3 步只**新增**末尾的 Generation 夹具，上面既有夹具保持不变。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import pytest

from app.db.models.identity import User
from app.services.auth.email_binding import EmailBindingService
from app.services.auth.email_tokens import EmailTokenStore
from app.services.auth.profile import ProfileService
from app.services.auth.redeem import RedeemService
from app.services.generation.orchestrator import GenerationOrchestrator
from app.services.generation.service import GenerationService
from app.services.points.service import PointsService
from app.services.ports import InMemoryEmailSink
from app.storage import InMemoryStorageAdapter
from tests.services.fakes import (
    FakePointsPort,
    FakeRedis,
    GenerationUnitOfWork,
    InMemoryUnitOfWork,
    MutableClock,
    PointsUnitOfWork,
    RecordingGenerationQueuePort,
    RecordingSession,
    make_user_row,
)


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def fake_redis(clock: MutableClock) -> FakeRedis:
    return FakeRedis(clock=clock)


@pytest.fixture
def email_sink() -> Iterator[InMemoryEmailSink]:
    sink = InMemoryEmailSink()
    yield sink
    sink.clear()


@pytest.fixture
def session_secret() -> str:
    return "synthetic-session-secret-for-tests"


@pytest.fixture
def uow(clock: MutableClock) -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork(clock=clock)


@pytest.fixture
def points_port() -> FakePointsPort:
    return FakePointsPort()


@pytest.fixture
def email_tokens(fake_redis: FakeRedis, session_secret: str) -> EmailTokenStore:
    return EmailTokenStore(fake_redis, secret=session_secret)


@pytest.fixture
def email_binding(
    uow: InMemoryUnitOfWork, email_tokens: EmailTokenStore, email_sink: InMemoryEmailSink
) -> EmailBindingService:
    return EmailBindingService(uow=uow, tokens=email_tokens, email_port=email_sink)


@pytest.fixture
def profile_service(uow: InMemoryUnitOfWork) -> ProfileService:
    return ProfileService(uow=uow)


@pytest.fixture
def redeem_service(points_port: FakePointsPort) -> RedeemService:
    return RedeemService(points_port=points_port)


@pytest.fixture
def seeded_user(uow: InMemoryUnitOfWork):
    """预置一个 ACTIVE 用户，返回 ORM 行。"""

    return uow.users.seed(make_user_row(now=datetime.now(timezone.utc)))


@pytest.fixture
def points_uow(clock: MutableClock) -> PointsUnitOfWork:
    return PointsUnitOfWork(clock=clock)


@pytest.fixture
def recording_session() -> RecordingSession:
    return RecordingSession()


# --------------------------------------------------------------------------- #
# ENG-06 第 3 步：Generation Service / Orchestrator 夹具（只新增）
# --------------------------------------------------------------------------- #
@pytest.fixture
def gen_uow(clock: MutableClock) -> GenerationUnitOfWork:
    """带事务语义（快照/回滚 + 事务探针）的内存工作单元。"""

    return GenerationUnitOfWork(clock=clock)


@pytest.fixture
def gen_queue(gen_uow: GenerationUnitOfWork) -> RecordingGenerationQueuePort:
    """投递记录器：与 `gen_uow.trace` 共享事件流，可断言「提交后才入队」。"""

    return RecordingGenerationQueuePort(trace=gen_uow.trace)


@pytest.fixture
def gen_storage(clock: MutableClock) -> InMemoryStorageAdapter:
    return InMemoryStorageAdapter(clock=clock)


@pytest.fixture
def gen_user(gen_uow: GenerationUnitOfWork, clock: MutableClock) -> User:
    """已验证邮箱且余额 100 点的用户（生成入口的默认前置条件）。"""

    user = gen_uow.users.seed(make_user_row(now=clock()))
    email_row = gen_uow.emails.add(user_id=user.id, email="owner@example.com", is_primary=True)
    gen_uow.emails.mark_verified(email_row, clock())
    gen_uow.point_wallets.seed(user.id, available_points=100, lifetime_credited=100)
    return user


@pytest.fixture
def make_generation_service(
    gen_uow: GenerationUnitOfWork, gen_queue: RecordingGenerationQueuePort, clock: MutableClock
):
    def _make(**overrides: object) -> GenerationService:
        params: dict[str, object] = {
            "uow": gen_uow,
            "points": PointsService(uow=gen_uow, clock=clock),
            "clock": clock,
            "queue": gen_queue,
            "enable_reference_image": False,
        }
        params.update(overrides)
        return GenerationService(**params)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def generation_service(make_generation_service) -> GenerationService:
    return make_generation_service()


@pytest.fixture
def make_orchestrator(
    gen_uow: GenerationUnitOfWork,
    gen_storage: InMemoryStorageAdapter,
    gen_queue: RecordingGenerationQueuePort,
    clock: MutableClock,
):
    def _make(provider, **overrides: object) -> GenerationOrchestrator:
        params: dict[str, object] = {
            "uow_factory": lambda: gen_uow,
            "points_factory": lambda uow: PointsService(uow=uow, clock=clock),
            "provider_factory": lambda: provider,
            "storage": gen_storage,
            "clock": clock,
            "queue": gen_queue,
        }
        params.update(overrides)
        return GenerationOrchestrator(**params)  # type: ignore[arg-type]

    return _make
