"""参考图分析付费登记的持久读写（`analysis_dispatches`）。

职责边界：这张表**只追加、不改写业务事实**，唯一的 UPDATE 是把 `outcome` 从
`UNKNOWN` 收敛到 `SUCCEEDED`/`FAILED`（行不删、占用不退）。Repository 只做数据访问
与并发裁决，「超限时返回哪个错误码」由 `services.analysis.quota` 决定；与其余仓储一样
**不自行提交事务**。

并发裁决为什么放在这一层：每日付费上限要在两个进程同时放行时仍然成立。
「按日计数 → 判额 → 插行」不是原子的，所以这里先取两把**事务级咨询锁**
（`pg_advisory_xact_lock`）把它们串起来：

- 顺序固定为「先全局日锁、再用户日锁」，反序会与新来的请求互锁；
- 锁随事务提交/回滚自动释放，不需要维护「谁持有」的状态；
- 同一 `(用户, 日, 图片摘要)` 的重复出网最后仍由唯一索引兜底——即使有人绕过本模块。

摘要只是指纹：本模块不写入也不读取图片字节、存储键或描述文本。
"""

from __future__ import annotations

import uuid
import zlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.analysis import AnalysisDispatch

__all__ = [
    "AnalysisDispatchRepository",
    "DispatchDraft",
    "DuplicateAnalysisDispatch",
    "SqlAlchemyAnalysisDispatchRepository",
]

#: `pg_advisory_xact_lock(int, int)` 的第一个参数：本项目专用命名空间，
#: 让别的代码用同一函数也不会与我们撞键。
_LOCK_CLASS = 0x41494156  # "AIAV"

#: PostgreSQL SQLSTATE：唯一约束冲突。只有这一种 `IntegrityError` 才是「今天这张图已经
#: 登记过」；其余（CHECK、外键、非空）都是真错误，必须原样上抛。
_UNIQUE_VIOLATION = "23505"


class DuplicateAnalysisDispatch(RuntimeError):
    """同一 `(用户, 日, 图片摘要)` 今天已经登记过一次（唯一索引裁决）。"""


@dataclass(frozen=True, slots=True)
class DispatchDraft:
    """一条待登记的出网记录。

    `created_at` 由调用方显式给出（必填）：数据库 CHECK 要求
    `usage_day = (created_at AT TIME ZONE 'UTC')::date`，因此两列必须来自同一个时间点。
    如果这里回退到 `utcnow()`，注入时钟的调用方就会写出「日期来自注入时钟、时间来自
    系统时钟」的半套记录，在 UTC 日界附近被 CHECK 拒绝。必填让这件事无法被忘记。
    """

    user_id: uuid.UUID
    usage_day: date
    digest: str
    provider_name: str
    model_name: str
    created_at: datetime


@runtime_checkable
class AnalysisDispatchRepository(Protocol):
    """付费登记端口。"""

    def lock_day(self, *, usage_day: date) -> None:
        """取得全局日锁（必须先于 `lock_user_day`）。"""
        ...

    def lock_user_day(self, *, user_id: uuid.UUID, usage_day: date) -> None: ...

    def record(self, draft: DispatchDraft) -> AnalysisDispatch:
        """插入一条 `UNKNOWN` 行；重复即抛 `DuplicateAnalysisDispatch`。"""
        ...

    def count_for_user(self, *, user_id: uuid.UUID, usage_day: date) -> int: ...

    def count_for_day(self, *, usage_day: date) -> int: ...

    def set_outcome(self, dispatch_id: uuid.UUID, *, outcome: str) -> None: ...


def _lock_object(scope: str) -> int:
    """把锁标识稳定映射到 32 位有符号整数。

    必须用 `crc32` 而不是内置 `hash()`：后者按进程随机化（`PYTHONHASHSEED`），
    两个 worker 会对同一天算出不同的键，锁就形同不存在。
    """

    return zlib.crc32(scope.encode("utf-8")) & 0x7FFFFFFF


class SqlAlchemyAnalysisDispatchRepository:
    """`AnalysisDispatchRepository` 的 SQLAlchemy 实现（只 flush，不提交）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock_day(self, *, usage_day: date) -> None:
        self._advisory_xact_lock(f"day:{usage_day.isoformat()}")

    def lock_user_day(self, *, user_id: uuid.UUID, usage_day: date) -> None:
        self._advisory_xact_lock(f"user:{user_id}:{usage_day.isoformat()}")

    def record(self, draft: DispatchDraft) -> AnalysisDispatch:
        row = AnalysisDispatch(
            id=uuid.uuid4(),
            user_id=draft.user_id,
            usage_day=draft.usage_day,
            digest=draft.digest,
            provider_name=draft.provider_name,
            model_name=draft.model_name,
            outcome="UNKNOWN",
            # 与 `usage_day` 同源：两列一起来自调用方的时间点，CHECK 才能稳定成立
            created_at=draft.created_at,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as error:
            raise _classify_integrity_error(error) from error
        return row

    def count_for_user(self, *, user_id: uuid.UUID, usage_day: date) -> int:
        return self._count(
            select(func.count())
            .select_from(AnalysisDispatch)
            .where(
                AnalysisDispatch.user_id == user_id,
                AnalysisDispatch.usage_day == usage_day,
            )
        )

    def count_for_day(self, *, usage_day: date) -> int:
        return self._count(
            select(func.count())
            .select_from(AnalysisDispatch)
            .where(AnalysisDispatch.usage_day == usage_day)
        )

    def set_outcome(self, dispatch_id: uuid.UUID, *, outcome: str) -> None:
        """只收敛 `outcome`：行不删、额度不退、摘要不改。"""

        self._session.execute(
            update(AnalysisDispatch)
            .where(AnalysisDispatch.id == dispatch_id)
            .values(outcome=outcome)
        )
        self._session.flush()

    # ------------------------------------------------------------------ 内部
    def _count(self, statement: object) -> int:
        return int(self._session.execute(statement).scalar_one())  # type: ignore[arg-type]

    def _advisory_xact_lock(self, scope: str) -> None:
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_class, :lock_object)"),
            {"lock_class": _LOCK_CLASS, "lock_object": _lock_object(scope)},
        )


def _classify_integrity_error(error: IntegrityError) -> Exception:
    """把 `IntegrityError` 分成「重复登记」与「真错误」。

    只按唯一约束冲突（SQLSTATE 23505）判定重复。早期实现把所有 `IntegrityError` 都当成
    「今天已经登记过」，于是 `ck_analysis_dispatches_usage_day_matches_created_at` 这类
    CHECK 失败会被伪装成用户额度问题（还给 `scope=image` 的 429）——真正的写入故障被
    掩盖成一个看起来很正常的业务拒绝。因此这里分辨 SQLSTATE：非唯一冲突原样上抛，
    由 `PostgresAnalysisQuota` 归为失败关闭（`NOT_READY`），而不是「额度用完」。
    """

    sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
    if sqlstate == _UNIQUE_VIOLATION:
        return DuplicateAnalysisDispatch()
    return error
