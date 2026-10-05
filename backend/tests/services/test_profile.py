"""用户资料 Service 测试（ENG-03 第 2 步）。"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.schemas.auth import UpdateMeRequest
from tests.services.fakes import InMemoryUnitOfWork, make_user_row


def test_get_profile_returns_contract_user(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    user = profile_service.get_profile(user_id=seeded_user.id)

    assert user.id == seeded_user.id
    assert user.status.value == "ACTIVE"
    assert user.locale == "zh-CN"
    assert user.timezone == "Asia/Shanghai"
    assert user.primary_email is None
    assert user.email_verified is False


def test_get_profile_includes_primary_email_and_verified_flag(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    from datetime import datetime, timezone

    row = uow.emails.add(user_id=seeded_user.id, email="User@Example.com", is_primary=True)
    uow.emails.mark_verified(row, datetime.now(timezone.utc))

    user = profile_service.get_profile(user_id=seeded_user.id)

    assert user.primary_email == "User@Example.com"
    assert user.email_verified is True


def test_update_allowed_text_fields(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    patch = UpdateMeRequest.model_validate(
        {"display_name": "十一", "locale": "en-US", "timezone": "UTC"}
    )

    user = profile_service.update_profile(user_id=seeded_user.id, patch=patch)

    assert user.display_name == "十一"
    assert user.locale == "en-US"
    assert user.timezone == "UTC"
    assert uow.commits == 1


def test_update_with_no_fields_does_not_commit(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    user = profile_service.update_profile(
        user_id=seeded_user.id, patch=UpdateMeRequest.model_validate({})
    )

    assert user.id == seeded_user.id
    assert uow.commits == 0


def test_update_avatar_when_owned(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    asset_id = uuid.uuid4()
    uow.media.register(asset_id, seeded_user.id)

    user = profile_service.update_profile(
        user_id=seeded_user.id,
        patch=UpdateMeRequest.model_validate({"avatar_asset_id": str(asset_id)}),
    )

    assert user.avatar_asset_id == asset_id


def test_update_avatar_can_be_cleared(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    user = profile_service.update_profile(
        user_id=seeded_user.id,
        patch=UpdateMeRequest.model_validate({"avatar_asset_id": None}),
    )

    assert user.avatar_asset_id is None


def test_update_avatar_not_found(
    profile_service, seeded_user
) -> None:
    with pytest.raises(ApiError) as excinfo:
        profile_service.update_profile(
            user_id=seeded_user.id,
            patch=UpdateMeRequest.model_validate({"avatar_asset_id": str(uuid.uuid4())}),
        )

    assert excinfo.value.code is ErrorCode.ASSET_NOT_FOUND
    assert excinfo.value.status_code == 404


def test_update_avatar_owned_by_another_user(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    asset_id = uuid.uuid4()
    uow.media.register(asset_id, uuid.uuid4())

    with pytest.raises(ApiError) as excinfo:
        profile_service.update_profile(
            user_id=seeded_user.id,
            patch=UpdateMeRequest.model_validate({"avatar_asset_id": str(asset_id)}),
        )

    assert excinfo.value.code is ErrorCode.ASSET_FORBIDDEN
    assert excinfo.value.status_code == 403


def test_explicit_null_on_non_nullable_field_is_rejected(
    profile_service, seeded_user
) -> None:
    with pytest.raises(ApiError) as excinfo:
        profile_service.update_profile(
            user_id=seeded_user.id,
            patch=UpdateMeRequest.model_validate({"locale": None}),
        )

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert excinfo.value.status_code == 422
    assert excinfo.value.details == {"fields": ["locale"]}


def test_unknown_field_is_rejected_by_schema(seeded_user) -> None:
    with pytest.raises(ValidationError):
        UpdateMeRequest.model_validate({"email": "new@example.com"})


def test_update_cannot_touch_email_status_or_points(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    before_status = seeded_user.status

    profile_service.update_profile(
        user_id=seeded_user.id,
        patch=UpdateMeRequest.model_validate({"display_name": "x"}),
    )

    assert seeded_user.status == before_status


def test_missing_user_raises_session_invalid(profile_service, uow: InMemoryUnitOfWork) -> None:
    with pytest.raises(ApiError) as excinfo:
        profile_service.get_profile(user_id=uuid.uuid4())

    assert excinfo.value.code is ErrorCode.SESSION_INVALID
    assert excinfo.value.status_code == 401


def test_deleted_user_raises_session_invalid(
    profile_service, uow: InMemoryUnitOfWork
) -> None:
    ghost = uow.users.seed(make_user_row(status="DELETED"))

    with pytest.raises(ApiError) as excinfo:
        profile_service.get_profile(user_id=ghost.id)

    assert excinfo.value.code is ErrorCode.SESSION_INVALID


def test_suspended_user_raises_account_suspended(
    profile_service, uow: InMemoryUnitOfWork
) -> None:
    suspended = uow.users.seed(make_user_row(status="SUSPENDED"))

    with pytest.raises(ApiError) as excinfo:
        profile_service.get_profile(user_id=suspended.id)

    assert excinfo.value.code is ErrorCode.ACCOUNT_SUSPENDED
    assert excinfo.value.status_code == 403


def test_update_does_not_commit_on_validation_failure(
    profile_service, uow: InMemoryUnitOfWork, seeded_user
) -> None:
    with pytest.raises(ApiError):
        profile_service.update_profile(
            user_id=seeded_user.id,
            patch=UpdateMeRequest.model_validate({"locale": None}),
        )

    assert uow.commits == 0
