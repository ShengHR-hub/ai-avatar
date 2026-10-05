"""邀请码规范化、哈希与可兑换性判定测试（ENG-03 第 2 步）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.invites import InviteCode
from app.errors.codes import ErrorCode
from app.services.auth.invite_code import (
    evaluate_invite_code,
    hash_invite_code,
    invite_code_matches,
    normalize_invite_code,
)

NOW = datetime(2026, 9, 26, 0, 0, 0, tzinfo=timezone.utc)


def make_invite_code(
    *,
    raw: str = "AVTR-ABCD-EFGH",
    status: str = "ACTIVE",
    initial_points: int = 100,
    max_redemptions: int = 1,
    redeemed_count: int = 0,
    expires_at: datetime | None = None,
) -> InviteCode:
    code = InviteCode(
        code_hash=hash_invite_code(raw),
        status=status,
        initial_points=initial_points,
        max_redemptions=max_redemptions,
        redeemed_count=redeemed_count,
        expires_at=expires_at,
        created_at=NOW,
    )
    code.id = uuid.uuid4()
    return code


# --------------------------------------------------------------------------- #
# 规范化与哈希
# --------------------------------------------------------------------------- #
def test_normalize_trims_and_uppercases() -> None:
    assert normalize_invite_code("  avtr-abcd-efgh  ") == "AVTR-ABCD-EFGH"


def test_hash_is_stable_sha256_hex_and_case_insensitive() -> None:
    digest = hash_invite_code("AVTR-ABCD-EFGH")

    assert len(digest) == 64
    assert digest == hash_invite_code("avtr-abcd-efgh")
    assert digest == hash_invite_code("  AVTR-ABCD-EFGH ")
    assert digest != hash_invite_code("AVTR-ABCD-EFGI")


def test_hash_never_contains_plaintext() -> None:
    raw = "AVTR-SECRET-CODE"
    digest = hash_invite_code(raw)

    assert raw not in digest
    assert raw.lower() not in digest


def test_matches_uses_constant_time_comparison() -> None:
    code = make_invite_code(raw="AVTR-ABCD-EFGH")

    assert invite_code_matches("avtr-abcd-efgh", code.code_hash) is True
    assert invite_code_matches("AVTR-XXXX-XXXX", code.code_hash) is False


# --------------------------------------------------------------------------- #
# 可兑换性判定
# --------------------------------------------------------------------------- #
def test_active_code_is_redeemable() -> None:
    state = evaluate_invite_code(make_invite_code(), now=NOW)

    assert state.redeemable is True
    assert state.error is None
    assert state.code is not None


def test_missing_code_is_invite_invalid() -> None:
    state = evaluate_invite_code(None, now=NOW)

    assert state.redeemable is False
    assert state.error is ErrorCode.INVITE_INVALID


@pytest.mark.parametrize("status", ["USED", "EXPIRED", "DISABLED"])
def test_non_active_status_is_invite_invalid(status: str) -> None:
    state = evaluate_invite_code(make_invite_code(status=status), now=NOW)

    assert state.error is ErrorCode.INVITE_INVALID


def test_expired_code_is_invite_invalid() -> None:
    code = make_invite_code(expires_at=NOW - timedelta(seconds=1))

    assert evaluate_invite_code(code, now=NOW).error is ErrorCode.INVITE_INVALID


def test_code_expiring_exactly_now_is_invalid() -> None:
    code = make_invite_code(expires_at=NOW)

    assert evaluate_invite_code(code, now=NOW).error is ErrorCode.INVITE_INVALID


def test_code_with_future_expiry_is_redeemable() -> None:
    code = make_invite_code(expires_at=NOW + timedelta(hours=1))

    assert evaluate_invite_code(code, now=NOW).redeemable is True


def test_exhausted_redemptions_is_invite_already_used() -> None:
    code = make_invite_code(max_redemptions=2, redeemed_count=2)

    assert evaluate_invite_code(code, now=NOW).error is ErrorCode.INVITE_ALREADY_USED


def test_partially_redeemed_code_is_redeemable() -> None:
    code = make_invite_code(max_redemptions=3, redeemed_count=1)

    assert evaluate_invite_code(code, now=NOW).redeemable is True


def test_naive_expiry_is_compared_against_naive_now() -> None:
    """`expires_at` 为无时区值时按同一基准比较，避免抛异常。"""

    code = make_invite_code(expires_at=(NOW - timedelta(hours=1)).replace(tzinfo=None))

    assert evaluate_invite_code(code, now=NOW).error is ErrorCode.INVITE_INVALID
