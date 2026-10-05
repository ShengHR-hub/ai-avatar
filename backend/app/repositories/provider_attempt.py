"""Repository access for the appendable Provider attempt record.

The repository deliberately owns persistence mechanics only. Provider state
transitions and retry policy remain in Generation Service; callers own the
transaction and this module never commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.provider import ProviderAttempt

__all__ = [
    "ProviderAttemptDraft",
    "ProviderAttemptRepository",
    "SqlAlchemyProviderAttemptRepository",
]


@dataclass(frozen=True, slots=True)
class ProviderAttemptDraft:
    """Values captured when one logical Provider attempt starts."""

    generation_id: uuid.UUID
    attempt_no: int
    provider_name: str
    model_name: str
    status: str
    started_at: datetime
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    image_count: int | None = None
    estimated_cost: Decimal | None = None
    currency: str | None = None
    request_metadata: dict[str, Any] = field(default_factory=dict)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    finished_at: datetime | None = None


@runtime_checkable
class ProviderAttemptRepository(Protocol):
    def get_by_id(self, attempt_id: uuid.UUID) -> ProviderAttempt | None: ...

    def get_by_generation_and_attempt(
        self, generation_id: uuid.UUID, attempt_no: int
    ) -> ProviderAttempt | None: ...

    def list_by_generation_id(
        self, generation_id: uuid.UUID
    ) -> tuple[ProviderAttempt, ...]: ...

    def lock_by_id(self, attempt_id: uuid.UUID) -> ProviderAttempt | None: ...

    def find_by_request_id(
        self, *, provider_name: str, request_id: str
    ) -> ProviderAttempt | None:
        """按 `(provider_name, request_id)` 关联 attempt（回调只允许这条关联路径）。"""
        ...

    def create(self, draft: ProviderAttemptDraft) -> ProviderAttempt: ...

    def update(self, attempt: ProviderAttempt, **changes: Any) -> ProviderAttempt: ...


_UPDATABLE_FIELDS = frozenset(
    {
        "request_id",
        "status",
        "input_tokens",
        "output_tokens",
        "image_count",
        "estimated_cost",
        "currency",
        "request_metadata",
        "response_metadata",
        "error_code",
        "error_message",
        "finished_at",
    }
)


class SqlAlchemyProviderAttemptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, attempt_id: uuid.UUID) -> ProviderAttempt | None:
        statement = select(ProviderAttempt).where(ProviderAttempt.id == attempt_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def get_by_generation_and_attempt(
        self, generation_id: uuid.UUID, attempt_no: int
    ) -> ProviderAttempt | None:
        statement = (
            select(ProviderAttempt)
            .where(
                ProviderAttempt.generation_id == generation_id,
                ProviderAttempt.attempt_no == attempt_no,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_by_generation_id(
        self, generation_id: uuid.UUID
    ) -> tuple[ProviderAttempt, ...]:
        statement = (
            select(ProviderAttempt)
            .where(ProviderAttempt.generation_id == generation_id)
            .order_by(ProviderAttempt.attempt_no)
        )
        return tuple(self._session.execute(statement).scalars())

    def lock_by_id(self, attempt_id: uuid.UUID) -> ProviderAttempt | None:
        statement = (
            select(ProviderAttempt)
            .where(ProviderAttempt.id == attempt_id)
            .with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def find_by_request_id(
        self, *, provider_name: str, request_id: str
    ) -> ProviderAttempt | None:
        statement = (
            select(ProviderAttempt)
            .where(
                ProviderAttempt.provider_name == provider_name,
                ProviderAttempt.request_id == request_id,
            )
            .order_by(ProviderAttempt.attempt_no.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def create(self, draft: ProviderAttemptDraft) -> ProviderAttempt:
        attempt = ProviderAttempt(
            generation_id=draft.generation_id,
            attempt_no=draft.attempt_no,
            provider_name=draft.provider_name,
            model_name=draft.model_name,
            request_id=draft.request_id,
            status=draft.status,
            input_tokens=draft.input_tokens,
            output_tokens=draft.output_tokens,
            image_count=draft.image_count,
            estimated_cost=draft.estimated_cost,
            currency=draft.currency,
            request_metadata=dict(draft.request_metadata),
            response_metadata=dict(draft.response_metadata),
            error_code=draft.error_code,
            error_message=draft.error_message,
            started_at=draft.started_at,
            finished_at=draft.finished_at,
        )
        self._session.add(attempt)
        self._session.flush()
        return attempt

    def update(self, attempt: ProviderAttempt, **changes: Any) -> ProviderAttempt:
        unknown = set(changes) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported provider attempt fields: {sorted(unknown)}")
        for field_name, value in changes.items():
            if field_name.endswith("metadata") and value is not None:
                value = dict(value)
            setattr(attempt, field_name, value)
        self._session.flush()
        return attempt
