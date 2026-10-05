"""邀请码规范化、哈希与可兑换性判定（ENG-03 第 2 步）。

依据：

- `02-database-design.md` 第 3.1 节：`invite_codes.code_hash varchar(128) NOT NULL UNIQUE`，
  **禁止保存明文邀请码**；
- `03-api-contract.md` 第 7 节：`INVITE_INVALID`（422，格式错误/过期/禁用/不存在）、
  `INVITE_ALREADY_USED`（409，已兑换或次数耗尽）；
- `03-api-contract.md` 第 9 节：幂等作用域使用 `invite_code_hash`。

哈希算法在文档中**未规定**，本卡采用 `SHA-256(规范化后的明文)` 十六进制（64 字符，符合
`varchar(128)`）。邀请码为高熵随机串（形如 `AVTR-ABCD-EFGH`），无密钥摘要足够；
如需改为带密钥的 HMAC，需先冻结密钥来源（当前 `09-repository-boundary.md` §6.1 未定义
邀请码专用密钥，故不使用 `SESSION_SECRET` 充当）。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone

from app.db.models.invites import InviteCode
from app.errors.codes import ErrorCode

__all__ = [
    "INVITE_CODE_HASH_ALGORITHM",
    "INVITE_CODE_PATTERN",
    "InviteCodeState",
    "evaluate_invite_code",
    "hash_invite_code",
    "invite_code_matches",
    "normalize_invite_code",
]

#: 哈希算法标识（写入代码注释与报告，便于后续冻结）
INVITE_CODE_HASH_ALGORITHM = "sha256"

#: 邀请码字符集与形态（`AVTR-ABCD-EFGH`）；契约只要求 1-128 字符，
#: 这里仅用于**规范化**（去空白、转大写），不做格式拒绝，避免新增产品规则。
INVITE_CODE_PATTERN = re.compile(r"^[A-Z0-9-]{1,128}$")

#: 邀请码状态枚举（`02-database-design.md` 第 3.1 节）
_REDEEMABLE_STATUSES = frozenset({"ACTIVE"})


def normalize_invite_code(raw: str) -> str:
    """规范化邀请码：去除首尾空白并转大写（用于哈希与比较）。"""

    return raw.strip().upper()


def hash_invite_code(raw: str) -> str:
    """返回邀请码的 SHA-256 十六进制摘要；**明文不得落库或进日志**。"""

    return hashlib.sha256(normalize_invite_code(raw).encode("utf-8")).hexdigest()


def invite_code_matches(raw: str, code_hash: str) -> bool:
    """以常量时间比较邀请码与库中哈希。"""

    return hmac.compare_digest(hash_invite_code(raw), code_hash)


class InviteCodeState:
    """邀请码可兑换性判定结果。"""

    __slots__ = ("code", "error")

    def __init__(self, code: InviteCode | None, error: ErrorCode | None) -> None:
        self.code = code
        self.error = error

    @property
    def redeemable(self) -> bool:
        return self.error is None and self.code is not None


def evaluate_invite_code(
    code: InviteCode | None, *, now: datetime | None = None
) -> InviteCodeState:
    """判定邀请码是否可兑换；不可兑换时给出契约错误码。

    - 不存在 / 状态非 `ACTIVE` / 已过期 → `INVITE_INVALID`（422）
    - 兑换次数已达上限 → `INVITE_ALREADY_USED`（409）
    """

    if code is None:
        return InviteCodeState(None, ErrorCode.INVITE_INVALID)

    if code.status not in _REDEEMABLE_STATUSES:
        return InviteCodeState(None, ErrorCode.INVITE_INVALID)

    moment = now or datetime.now(timezone.utc)
    expires_at = code.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            moment = moment.replace(tzinfo=None)
        if expires_at <= moment:
            return InviteCodeState(None, ErrorCode.INVITE_INVALID)

    if code.redeemed_count >= code.max_redemptions:
        return InviteCodeState(None, ErrorCode.INVITE_ALREADY_USED)

    return InviteCodeState(code, None)
