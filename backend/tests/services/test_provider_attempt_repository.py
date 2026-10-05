from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.db.models.provider import ProviderAttempt
from app.repositories.provider_attempt import (
    ProviderAttemptDraft,
    ProviderAttemptRepository,
    SqlAlchemyProviderAttemptRepository,
)
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork
from tests.services.fakes import (
    InMemoryProviderAttemptRepository,
    RecordingSession,
)


GENERATION_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
STARTED_AT = datetime(2026, 9, 28, tzinfo=timezone.utc)


def _draft(*, generation_id: uuid.UUID = GENERATION_ID, attempt_no: int = 1) -> ProviderAttemptDraft:
    return ProviderAttemptDraft(
        generation_id=generation_id,
        attempt_no=attempt_no,
        provider_name="mock",
        model_name="mock-v1",
        status="STARTED",
        started_at=STARTED_AT,
        request_metadata={"client_request_id": "client-1"},
    )


def test_protocol_and_uow_expose_provider_attempt_repository() -> None:
    assert isinstance(InMemoryProviderAttemptRepository(), ProviderAttemptRepository)
    assert "provider_attempts" in getattr(UnitOfWork, "__annotations__", {})


def test_in_memory_repository_reads_locks_lists_creates_and_updates() -> None:
    repository = InMemoryProviderAttemptRepository()

    attempt = repository.create(_draft())

    assert repository.get_by_id(attempt.id) is attempt
    assert repository.get_by_generation_and_attempt(GENERATION_ID, 1) is attempt
    assert repository.list_by_generation_id(GENERATION_ID) == (attempt,)
    assert repository.lock_by_id(attempt.id) is attempt

    updated = repository.update(
        attempt,
        status="SUCCEEDED",
        request_id="provider-request-1",
        image_count=2,
        estimated_cost=Decimal("0.12000000"),
        currency="USD",
        response_metadata={"output_count": 2},
        finished_at=STARTED_AT,
    )

    assert updated is attempt
    assert attempt.status == "SUCCEEDED"
    assert attempt.request_id == "provider-request-1"
    assert attempt.image_count == 2
    assert attempt.estimated_cost == Decimal("0.12000000")
    assert attempt.currency == "USD"
    assert attempt.response_metadata == {"output_count": 2}
    assert attempt.finished_at == STARTED_AT


def test_sqlalchemy_repository_uses_select_for_update_and_flush_without_commit() -> None:
    session = RecordingSession()
    repository = SqlAlchemyProviderAttemptRepository(session)

    repository.get_by_id(uuid.uuid4())
    repository.get_by_generation_and_attempt(GENERATION_ID, 2)
    repository.list_by_generation_id(GENERATION_ID)
    repository.lock_by_id(uuid.uuid4())
    created = repository.create(_draft(attempt_no=2))
    repository.update(created, status="SUBMITTED", request_id="provider-request-2")

    assert any("provider_attempts" in statement for statement in session.statements)
    assert any("FOR UPDATE" in statement.upper() for statement in session.statements)
    assert session.flushes >= 2
    assert session.commits == 0
