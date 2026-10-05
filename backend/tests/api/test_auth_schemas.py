"""ENG-03 请求与响应 Schema 形状测试（第 1 步）。

对照 `shared/api-contract/openapi.json`（LEAD-03 冻结基线）的关键形状逐项断言。
完整的「运行时 OpenAPI 与基线解引用比较」属第 4 步（需要路由装配后执行）。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.auth import (
    EmailAddress,
    EmailRequestRequest,
    EmailVerifyRequest,
    InviteRedeemRequest,
    UpdateMeRequest,
)
from app.schemas.user import (
    PointBalance,
    PointBalanceEnvelope,
    Session,
    SessionEnvelope,
    User,
    UserEnvelope,
    UserStatus,
)


def _properties(model) -> dict:
    return model.model_json_schema()["properties"]


def _without_title(schema: dict) -> dict:
    """去掉文档性注解键（`title`/`description`），与第 4 步规范化口径一致。"""

    return {
        key: value
        for key, value in schema.items()
        if key not in {"title", "description"}
    }


def _resolve_refs(root_schema: dict, node: dict) -> dict:
    """解析 `$ref`（Pydantic 会把 `StrEnum` 渲染为 `$ref` + `$defs`）。

    解引用后与基线一致，这是第 4 步规范化比较的同一口径。
    """

    if set(node) == {"$ref"}:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _resolve_refs(root_schema, root_schema["$defs"][name])
    return node


# --------------------------------------------------------------------------- #
# User
# --------------------------------------------------------------------------- #
def test_user_required_fields_match_baseline() -> None:
    assert set(User.model_json_schema()["required"]) == {
        "id",
        "status",
        "locale",
        "timezone",
        "email_verified",
    }


def test_user_nullable_fields_render_anyof_without_default() -> None:
    """可空字段用 `anyOf` 表达（联合 `type` 数组会被 FastAPI 0.115 拒绝）。"""

    properties = _properties(User)

    assert _without_title(properties["display_name"]) == {
        "anyOf": [{"type": "string", "maxLength": 80}, {"type": "null"}]
    }
    assert _without_title(properties["avatar_asset_id"]) == {
        "anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]
    }
    assert _without_title(properties["primary_email"]) == {
        "anyOf": [{"type": "string", "format": "email"}, {"type": "null"}]
    }
    for name in ("display_name", "avatar_asset_id", "primary_email"):
        assert "default" not in properties[name], name
        assert "type" not in properties[name], name


def test_user_status_is_string_enum() -> None:
    schema = User.model_json_schema()
    resolved = _resolve_refs(schema, schema["properties"]["status"])

    # 解引用并去掉文档性注解后，与基线的 {type: string, enum: [...]} 一致
    assert _without_title(resolved) == {
        "type": "string",
        "enum": ["ACTIVE", "SUSPENDED", "DELETED"],
    }
    assert UserStatus.ACTIVE.value == "ACTIVE"


def test_user_locale_and_timezone_length_limits() -> None:
    properties = _properties(User)

    assert _without_title(properties["locale"]) == {"type": "string", "maxLength": 16}
    assert _without_title(properties["timezone"]) == {"type": "string", "maxLength": 64}


def test_user_timestamps_are_optional_date_time() -> None:
    schema = User.model_json_schema()
    properties = schema["properties"]

    for name in ("created_at", "updated_at"):
        assert _without_title(properties[name]) == {"type": "string", "format": "date-time"}
        assert "default" not in properties[name]
        assert name not in schema["required"]


def test_user_has_no_additional_properties_constraint() -> None:
    assert User.model_json_schema().get("additionalProperties") is None


def test_user_accepts_synthetic_values() -> None:
    user = User(
        id=uuid.uuid4(),
        status=UserStatus.ACTIVE,
        locale="zh-CN",
        timezone="Asia/Shanghai",
        email_verified=False,
    )

    assert user.display_name is None
    assert user.primary_email is None
    assert user.status is UserStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Session / PointBalance
# --------------------------------------------------------------------------- #
def test_session_required_fields_match_baseline() -> None:
    schema = Session.model_json_schema()

    assert set(schema["required"]) == {
        "session_id",
        "user_id",
        "email_verified",
        "expires_at",
    }
    assert _without_title(schema["properties"]["user_id"]) == {
        "type": "string",
        "format": "uuid",
    }
    assert _without_title(schema["properties"]["expires_at"]) == {
        "type": "string",
        "format": "date-time",
    }


def test_point_balance_minimums_and_required() -> None:
    schema = PointBalance.model_json_schema()

    assert set(schema["required"]) == {
        "available_points",
        "reserved_points",
        "lifetime_credited",
        "lifetime_consumed",
        "updated_at",
    }
    for name in (
        "available_points",
        "reserved_points",
        "lifetime_credited",
        "lifetime_consumed",
    ):
        assert _without_title(schema["properties"][name]) == {
            "type": "integer",
            "minimum": 0,
        }, name


def test_point_balance_rejects_negative_values() -> None:
    from datetime import datetime, timezone

    with pytest.raises(ValidationError):
        PointBalance(
            available_points=-1,
            reserved_points=0,
            lifetime_credited=0,
            lifetime_consumed=0,
            updated_at=datetime.now(timezone.utc),
        )


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "envelope_model", [UserEnvelope, SessionEnvelope, PointBalanceEnvelope]
)
def test_envelopes_require_data_and_request_id(envelope_model) -> None:
    schema = envelope_model.model_json_schema()

    assert set(schema["required"]) == {"data", "request_id"}
    assert schema.get("additionalProperties") is None
    assert set(schema["properties"]) == {"data", "request_id"}


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
def test_invite_redeem_request_shape() -> None:
    schema = InviteRedeemRequest.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"invite_code", "email"}
    assert _without_title(schema["properties"]["invite_code"]) == {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
    }
    assert _without_title(schema["properties"]["email"]) == {
        "type": "string",
        "format": "email",
    }


def test_email_request_shape() -> None:
    schema = EmailRequestRequest.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"email"}
    assert _without_title(schema["properties"]["email"]) == {
        "type": "string",
        "format": "email",
    }


def test_email_verify_request_shape() -> None:
    schema = EmailVerifyRequest.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"token"}
    assert _without_title(schema["properties"]["token"]) == {
        "type": "string",
        "minLength": 1,
        "maxLength": 512,
    }


def test_update_me_request_shape_matches_baseline() -> None:
    schema = UpdateMeRequest.model_json_schema()

    assert schema["additionalProperties"] is False
    assert "required" not in schema  # 四个字段全部可选
    assert _without_title(schema["properties"]["display_name"]) == {
        "anyOf": [{"type": "string", "maxLength": 80}, {"type": "null"}]
    }
    assert _without_title(schema["properties"]["avatar_asset_id"]) == {
        "anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]
    }
    # locale / timezone 在基线中是非可空字符串
    assert _without_title(schema["properties"]["locale"]) == {
        "type": "string",
        "maxLength": 16,
    }
    assert _without_title(schema["properties"]["timezone"]) == {
        "type": "string",
        "maxLength": 64,
    }


# --------------------------------------------------------------------------- #
# 请求体运行时行为
# --------------------------------------------------------------------------- #
def test_request_models_reject_unknown_fields() -> None:
    for model, payload in (
        (InviteRedeemRequest, {"invite_code": "AVTR-ABCD", "email": "a@example.com", "x": 1}),
        (EmailRequestRequest, {"email": "a@example.com", "x": 1}),
        (EmailVerifyRequest, {"token": "t", "x": 1}),
        (UpdateMeRequest, {"locale": "zh-CN", "x": 1}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_update_me_unknown_field_rejected_and_allowed_fields_tracked() -> None:
    request = UpdateMeRequest.model_validate({"display_name": "十一"})

    assert request.provided_fields() == {"display_name"}
    assert request.non_nullable_fields_provided_as_null() == set()


def test_update_me_detects_explicit_null_on_non_nullable_fields() -> None:
    request = UpdateMeRequest.model_validate({"locale": None, "timezone": "Asia/Shanghai"})

    assert request.provided_fields() == {"locale", "timezone"}
    assert request.non_nullable_fields_provided_as_null() == {"locale"}


def test_update_me_allows_explicit_null_for_nullable_fields() -> None:
    request = UpdateMeRequest.model_validate({"display_name": None, "avatar_asset_id": None})

    assert request.non_nullable_fields_provided_as_null() == set()


def test_email_address_validation() -> None:
    from pydantic import TypeAdapter

    adapter = TypeAdapter(EmailAddress)

    assert adapter.validate_python("  user@example.com  ") == "user@example.com"
    for invalid in ("not-an-email", "a@b", "a b@example.com", ""):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_invite_code_length_bounds() -> None:
    with pytest.raises(ValidationError):
        InviteRedeemRequest(invite_code="", email="a@example.com")
    with pytest.raises(ValidationError):
        InviteRedeemRequest(invite_code="x" * 129, email="a@example.com")
