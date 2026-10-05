"""任务状态机与合法转换表（ENG-06 第 3 步）。

唯一来源：`LEAD-04` §2（转换表）、§3（`status`/`billing_status` 合法配对）、
`docs/handoff/2026-09-27-current-handoff.md` §4.1。

- 六态、无 `CANCELLED`；终态不可逆，重复事件只能回放；
- `CREATED/QUEUED/PROCESSING` 不得跳过；
- 人工复核决策只改 `billing_status`、不改任务状态，因此 `NEEDS_REVIEW` 也属终态。

非法转换是服务端不变量破坏（不是用户可触发条件），统一抛 `IllegalTransition`，
由调用方映射到既有 `INTERNAL_ERROR`（500）：不伪造状态、不写流水。
"""

from __future__ import annotations

from types import MappingProxyType

__all__ = [
    "ALL_STATUSES",
    "CREATED",
    "FAILED",
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "NEEDS_REVIEW",
    "PROCESSING",
    "QUEUED",
    "STALE_RESUMABLE_STATUSES",
    "SUCCEEDED",
    "TERMINAL_STATUSES",
    "UNRESOLVED_STATUSES",
    "assert_transition",
    "is_terminal",
]

CREATED = "CREATED"
QUEUED = "QUEUED"
PROCESSING = "PROCESSING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
NEEDS_REVIEW = "NEEDS_REVIEW"

#: 冻结六态（与数据库 `CHECK` 完全一致）
ALL_STATUSES = (CREATED, QUEUED, PROCESSING, SUCCEEDED, FAILED, NEEDS_REVIEW)

#: 终态：提交后不可再转换（`LEAD-04` §2:41）
TERMINAL_STATUSES = frozenset({SUCCEEDED, FAILED, NEEDS_REVIEW})

#: 尚无终局流水、需要恢复扫描关注的状态
UNRESOLVED_STATUSES = (CREATED, QUEUED, PROCESSING)

#: 恢复扫描可安全重投的状态（事务已提交但投递丢失）
STALE_RESUMABLE_STATUSES = (CREATED, QUEUED)

#: 合法转换表（不得跳过中间态；`CREATED/QUEUED` 的取消归为 `FAILED`，`LEAD-04` §4）
LEGAL_TRANSITIONS = MappingProxyType(
    {
        CREATED: frozenset({QUEUED, FAILED}),
        QUEUED: frozenset({PROCESSING, FAILED}),
        PROCESSING: frozenset({SUCCEEDED, FAILED, NEEDS_REVIEW}),
        SUCCEEDED: frozenset(),
        FAILED: frozenset(),
        NEEDS_REVIEW: frozenset(),
    }
)


class IllegalTransition(RuntimeError):
    """状态转换非法：调用方必须放弃本次写入并按内部错误处理。"""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法任务状态转换：{current} -> {target}")
        self.current = current
        self.target = target


def is_terminal(status: str) -> bool:
    """是否终态。"""

    return status in TERMINAL_STATUSES


def assert_transition(current: str, target: str) -> None:
    """校验 `current -> target`；非法抛 `IllegalTransition`。"""

    allowed = LEGAL_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise IllegalTransition(current, target)
