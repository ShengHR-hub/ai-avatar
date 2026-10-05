"""Points Service（ENG-04 第 2–5 步）。

职责：点数钱包的全部账务写入，以及结算入口对 `generation_tasks.billing_status` 的更新。
依据：

- `02-database-design.md` 第 3.2 节四条不变量与同事务顺序
  （**锁钱包 → 插入流水 → 更新快照 → `version += 1`**）；
- 第 3.3 节与第 6.2 节：结算必须校验 `billing_status=RESERVED`，写唯一终局流水后
  把 `billing_status` 推进为 `CONSUMED` / `RELEASED`，任务生命周期 `status` 不变；
- 第 3.4 节与 `LEAD-04` 第 8 节：**首次成功**的人工结算与点数调整必须追加
  **独立** `audit_logs`；同向幂等回放与反向拒绝决定均不追加
  （技术负责人 2026-09-27 裁决 C，反向拒绝的留痕未来由 Admin API 的独立事务承担）；
- `LEAD-04` 第 5.3、第 6:127 节：结算金额取自 `generation_tasks.reserved_points`，
  固定锁顺序为 **先锁 `generation_tasks`，再锁 `point_wallets`**
  （技术负责人 2026-09-27 裁决 ②）；
- `03-api-contract.md` 第 9 节：账务幂等使用服务端生成的操作级键；
- `LEAD-04` 第 4 节：取消归类为 `FAILED + RELEASED` 或 `NEEDS_REVIEW + RESERVED`，
  生成任务不使用 `CANCELLED`。

设计约束：

- 所有写操作都在**同一事务内**完成，顺序固定；本 Service **不 `commit`**，
  事务边界由调用方（Generation/Admin Service 或路由端口适配）持有；
- 结算入口（`consume` / `release` / `resolve_needs_review`）**不接受调用方传入金额**，
  金额一律来自锁定后的任务行 `reserved_points`；
- 每次操作携带 `idempotency_key`；命中已有流水且账务参数一致时**回放既有结果**，
  不重复入账；同一键对应不同参数时返回 `IDEMPOTENCY_CONFLICT`（`LEAD-04` §5.4）；
- `RESERVE` 每任务最多一次；`CONSUME` 与 `RELEASE` 互斥且总共最多一次；
  同向重复幂等回放，**反向结算返回 `IDEMPOTENCY_CONFLICT`（409）**（`LEAD-04` §5.5，
  稳定错误码集合无 `BILLING_ALREADY_FINALIZED`，按技术负责人 2026-09-27 裁决 ③复用既有码）；
- 余额非负；`available_points` 不足时抛 `INSUFFICIENT_POINTS`；
- `ADJUSTMENT` 不改变 `reserved_points`，不覆盖或删除历史流水，且必须留痕管理员、
  请求 ID、工单引用与前后钱包快照；Admin/RBAC 归属调用方，本 Service 只做参数留痕；
- 不直接 UPDATE 余额而不写流水。

对外错误映射（技术负责人 2026-09-27 修正批次裁决 A：**不新增任何错误码**）：

- 任务不存在 / 任务归属与调用方声明不符 → `ApiError(GENERATION_NOT_FOUND)`（404）。
  两者同一错误码，避免通过差异化的响应探测他人任务是否存在；
- 已结算或账务状态冲突（同键不同参数、反向终局决定）与
  `NEEDS_REVIEW` 前置状态不符 → `ApiError(IDEMPOTENCY_CONFLICT)`（409）；
- 钱包无法锁定、`reserved_points` 非正、缺少 `RESERVE` 流水、快照与流水矛盾、
  预留余额不足、非法终局类型 → **保持内部异常 `WalletLedgerError`**：这些是账务
  不变量破坏，不是业务分支。对外由 `app/errors/handlers.py` 已注册的
  `_handle_unexpected_exception`（`app.add_exception_handler(Exception, ...)`）
  统一映射为 `INTERNAL_ERROR`（500），本 Service 不为此新增 `ApiError` 抛出点。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.db.models.generation import GenerationTask
from app.db.models.points import PointLedger, PointWallet
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories import AuditEntryDraft, UnitOfWork
from app.repositories.points import LedgerEntryDraft, WalletDelta
from app.schemas.user import PointBalance
from app.services.ports import RedeemOutcome

__all__ = [
    "ADJUSTMENT_KINDS",
    "DEFAULT_LOCALE",
    "DEFAULT_TIMEZONE",
    "REVIEW_DECISIONS",
    "InviteRedeemService",
    "LedgerPage",
    "PointsService",
    "WalletLedgerError",
]

#: ADJUSTMENT 允许的两种 kind（`02-database-design.md` 第 3.2 节）
ADJUSTMENT_KINDS = frozenset({"POSITIVE_CREDIT", "NEGATIVE_CONSUMPTION"})

#: `resolve_needs_review` 允许的两种人工决定（`LEAD-04` 第 8 节）
REVIEW_DECISIONS = frozenset({"CONSUME", "RELEASE"})

#: 新用户默认本地化设置：**待冻结产品参数**（`03-api-contract.md` §12 未规定），
#: 本卡以常量隔离，不视为产品规则。
DEFAULT_LOCALE = "zh-CN"
DEFAULT_TIMEZONE = "UTC"

#: 与数据库列宽一致的自由文本上限（超长会在写库时被截断/报错，提前以
#: `VALIDATION_ERROR` 拒绝，不新增错误码）
_NOTE_MAX_LENGTH = 255  # point_ledger.reason varchar(255)
_REQUEST_ID_MAX_LENGTH = 128  # audit_logs.request_id varchar(128)
_CASE_REFERENCE_MAX_LENGTH = 255


class WalletLedgerError(RuntimeError):
    """内部一致性错误（不应出现在正常业务流中）。"""


@dataclass(frozen=True, slots=True)
class _SettlementSnapshots:
    """首次成功结算产生的前后快照（人工复核审计的唯一消费者）。

    总控 2026-09-27 裁决 C：幂等回放**不写审计**，因此回放路径没有快照消费者，
    不再为无人使用的数据额外读一次钱包行。
    """

    task_before: dict[str, object]
    wallet_before: dict[str, object]
    wallet_after: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Settlement:
    """一次终局结算的内部结果（`consume` / `release` / `resolve_needs_review` 共用）。

    - `applied=True` 表示本次真正入账：写了一条终局流水并推进了 `billing_status`，
      此时 `snapshots` 必然存在（不变式，供审计留痕）；
    - `applied=False` 表示幂等回放：没有写新流水、没有推进 `billing_status`，
      按裁决 C 也不追加审计，`snapshots` 为 `None`；
    - `task` 是本次操作锁定并结算的任务行。
    """

    entry: PointLedger
    applied: bool
    task: GenerationTask
    snapshots: _SettlementSnapshots | None = None


@dataclass(frozen=True, slots=True)
class LedgerPage:
    """用户侧流水翻页结果：`next_cursor` 由服务端生成，客户端只能原样回传。"""

    rows: tuple[PointLedger, ...]
    next_cursor: str | None
    has_more: bool


def _encode_cursor(created_at: datetime, ledger_id: uuid.UUID) -> str:
    return f"{created_at.isoformat()}|{ledger_id}"


def _decode_cursor(raw: str | None) -> tuple[datetime, uuid.UUID] | None:
    """游标由服务端生成，客户端只能原样回传；被篡改时按非法输入拒绝。"""

    if raw is None:
        return None
    try:
        stamp, _, identifier = raw.partition("|")
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment, uuid.UUID(identifier)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["cursor"]}) from exc


class PointsService:
    """钱包账务写入与查询。

    调用方负责事务边界：本类的每个公开方法都在**同一事务内**完成
    「（结算时先）锁任务 → 锁钱包 → 插流水 → 更新快照 →（结算时）更新 `billing_status`」，
    但**不提交**。
    """

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow = uow
        #: 决策时间戳来源（人工复核审计用）；测试可注入可控时钟
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ 内部
    def _locked_wallet(self, user_id: uuid.UUID) -> PointWallet:
        """固定锁顺序第二步：锁定钱包；不存在时创建后重新锁定。"""

        wallet = self._uow.point_wallets.lock_by_user_id(user_id)
        if wallet is None:
            self._uow.point_wallets.create(user_id=user_id)
            wallet = self._uow.point_wallets.lock_by_user_id(user_id)
        if wallet is None:
            raise WalletLedgerError(f"用户 {user_id} 的钱包无法创建或锁定。")
        return wallet

    def _replay(
        self,
        idempotency_key: str,
        *,
        entry_type: str,
        user_id: uuid.UUID,
        amount: int,
        available_delta: int,
        reserved_delta: int,
        generation_id: uuid.UUID | None = None,
        order_id: uuid.UUID | None = None,
    ) -> PointLedger | None:
        """幂等回放（`LEAD-04` §5.4）。

        同一幂等键且**账务参数一致**时返回第一次的流水；参数不一致时返回
        `IDEMPOTENCY_CONFLICT`（409），既不写第二条流水也不改余额。
        `reason` 与 `metadata` 是展示/审计文本，不参与一致性比较。
        """

        existing = self._uow.point_ledger.find_by_idempotency_key(idempotency_key)
        if existing is None:
            return None
        if (
            existing.entry_type == entry_type
            and existing.user_id == user_id
            and existing.amount == amount
            and existing.available_delta == available_delta
            and existing.reserved_delta == reserved_delta
            and existing.generation_id == generation_id
            and existing.order_id == order_id
        ):
            return existing
        raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)

    def _write(
        self,
        wallet: PointWallet,
        *,
        entry_type: str,
        available_delta: int,
        reserved_delta: int,
        amount: int,
        idempotency_key: str,
        reason: str,
        wallet_delta: WalletDelta,
        generation_id: uuid.UUID | None = None,
        order_id: uuid.UUID | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PointLedger:
        """锁后写流水 + 更新快照 + 校验非负（顺序固定，不提交）。"""

        entry = self._uow.point_ledger.append(
            LedgerEntryDraft(
                wallet_id=wallet.id,
                user_id=wallet.user_id,
                entry_type=entry_type,
                available_delta=available_delta,
                reserved_delta=reserved_delta,
                amount=amount,
                idempotency_key=idempotency_key,
                reason=reason,
                generation_id=generation_id,
                order_id=order_id,
                metadata=dict(metadata or {}),
            )
        )
        self._uow.point_wallets.apply_delta(wallet, wallet_delta)
        self._assert_non_negative(wallet)
        return entry

    @staticmethod
    def _assert_non_negative(wallet: PointWallet) -> None:
        if wallet.available_points < 0:
            raise ApiError(ErrorCode.INSUFFICIENT_POINTS)
        if wallet.reserved_points < 0:
            raise WalletLedgerError("预留余额出现负数，账务不变量被破坏。")

    @staticmethod
    def _require_positive(amount: int) -> None:
        if amount <= 0:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["amount"]})

    @staticmethod
    def _require_text(
        value: object, field: str, *, max_length: int | None = None
    ) -> None:
        """自由文本参数必须是**非空且不超过列宽**的字符串。"""

        if not isinstance(value, str) or not value.strip():
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": [field]})
        if max_length is not None and len(value) > max_length:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": [field]})

    @staticmethod
    def _require_uuid(value: object, field: str) -> None:
        if not isinstance(value, uuid.UUID):
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": [field]})

    def _replay_or_reject_final(
        self, generation_id: uuid.UUID, *, entry_type: str
    ) -> PointLedger | None:
        """终局流水检查（`LEAD-04` §5.5）。

        - 已有**同向**终局流水：幂等回放第一次结果；
        - 已有**反向**终局流水（`CONSUME` ↔ `RELEASE`）：返回
          `IDEMPOTENCY_CONFLICT`（409），**不写反向流水、不改余额**；
        - 尚无终局流水：返回 `None`，由调用方继续结算。

        稳定错误码集合中没有 `BILLING_ALREADY_FINALIZED`；按技术负责人
        2026-09-27 裁决 ③复用既有的 `IDEMPOTENCY_CONFLICT`，不新增错误码。
        """

        final = self._uow.point_ledger.find_final_for_generation(generation_id)
        if final is None:
            return None
        if final.entry_type == entry_type:
            return final
        raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)

    def _require_reserve(self, generation_id: uuid.UUID) -> PointLedger:
        reserve = self._uow.point_ledger.find_reserve_for_generation(generation_id)
        if reserve is None:
            raise WalletLedgerError(f"任务 {generation_id} 缺少 RESERVE 流水，无法结算。")
        return reserve

    # ------------------------------------------------- 快照（审计与对账用，只读）
    @staticmethod
    def _wallet_snapshot(wallet: PointWallet | None) -> dict[str, object]:
        """`point_wallets` 的四条余额 + `version`；钱包不存在时全部记 0。"""

        if wallet is None:
            return {
                "wallet_id": None,
                "available_points": 0,
                "reserved_points": 0,
                "lifetime_credited": 0,
                "lifetime_consumed": 0,
                "version": 0,
            }
        return {
            "wallet_id": str(wallet.id),
            "available_points": wallet.available_points,
            "reserved_points": wallet.reserved_points,
            "lifetime_credited": wallet.lifetime_credited,
            "lifetime_consumed": wallet.lifetime_consumed,
            "version": wallet.version,
        }

    @staticmethod
    def _task_snapshot(task: GenerationTask) -> dict[str, object]:
        """`generation_tasks` 的账务相关摘要快照。

        刻意**不包含** `prompt`：`02-database-design.md` §3.4 禁止把完整 Prompt 写入审计。
        """

        return {
            "generation_id": str(task.id),
            "user_id": str(task.user_id),
            "status": task.status,
            "billing_status": task.billing_status,
            "reserved_points": task.reserved_points,
            "mode": task.mode,
            "requested_count": task.requested_count,
        }

    # ------------------------------------------------------- 入账与预留（第 2 步）
    def credit(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        reason: str,
        order_id: uuid.UUID | None = None,
    ) -> PointLedger:
        """入账：`available += amount`、`lifetime_credited += amount`。"""

        self._require_positive(amount)
        existing = self._replay(
            idempotency_key,
            entry_type="CREDIT",
            user_id=user_id,
            amount=amount,
            available_delta=amount,
            reserved_delta=0,
            order_id=order_id,
        )
        if existing is not None:
            return existing

        wallet = self._locked_wallet(user_id)
        return self._write(
            wallet,
            entry_type="CREDIT",
            available_delta=amount,
            reserved_delta=0,
            amount=amount,
            idempotency_key=idempotency_key,
            reason=reason,
            order_id=order_id,
            wallet_delta=WalletDelta(available=amount, lifetime_credited=amount),
        )

    def reserve(
        self,
        *,
        user_id: uuid.UUID,
        generation_id: uuid.UUID,
        amount: int,
        idempotency_key: str,
        reason: str,
    ) -> PointLedger:
        """预留：`available -= amount`、`reserved += amount`；每任务最多一次。

        预留金额**必须由调用方传入**：`02-database-design.md` §6.1 的顺序是
        「锁钱包 → 校验余额 → 创建任务 → 写 RESERVE」，预留发生时任务行可能还不存在，
        成本由 Generation Service 从 `cost_snapshot` 推导并写入 `reserved_points`。
        """

        self._require_positive(amount)
        existing = self._replay(
            idempotency_key,
            entry_type="RESERVE",
            user_id=user_id,
            amount=amount,
            available_delta=-amount,
            reserved_delta=amount,
            generation_id=generation_id,
        )
        if existing is not None:
            return existing
        already = self._uow.point_ledger.find_reserve_for_generation(generation_id)
        if already is not None:
            return already

        wallet = self._locked_wallet(user_id)
        if wallet.available_points < amount:
            raise ApiError(ErrorCode.INSUFFICIENT_POINTS)

        return self._write(
            wallet,
            entry_type="RESERVE",
            available_delta=-amount,
            reserved_delta=amount,
            amount=amount,
            idempotency_key=idempotency_key,
            reason=reason,
            generation_id=generation_id,
            wallet_delta=WalletDelta(available=-amount, reserved=amount),
        )

    # ------------------------------------------------------- 结算（第 3 步核心）
    def _lock_settleable_task(
        self, generation_id: uuid.UUID, *, user_id: uuid.UUID | None
    ) -> GenerationTask:
        """固定锁顺序第一步：`SELECT ... FOR UPDATE` 锁任务行（`LEAD-04` §6:127）。

        `user_id` 为调用方声明的归属用户，**只做一致性校验**：真正的钱包归属以
        锁定后的任务行为准（人工复核入口没有 `user_id` 可声明）。

        裁决 A：任务不存在与归属不符都返回 `GENERATION_NOT_FOUND`（404）——
        用同一个错误码才不会让调用方通过响应差异探测他人任务是否存在；
        不新增错误码，也不把这两种业务分支降级成内部不变量异常。
        """

        task = self._uow.generation_tasks.lock_by_id(generation_id)
        if task is None:
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
        if user_id is not None and task.user_id != user_id:
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
        return task

    def _settle_locked(
        self,
        task: GenerationTask,
        *,
        entry_type: str,
        idempotency_key: str,
        reason: str,
        metadata: dict[str, object] | None = None,
    ) -> _Settlement:
        """在**已锁定的任务行**上完成终局结算（`02-database-design.md` §6.2）。

        顺序（幂等回放判定必须早于 `billing_status` 前置校验，否则会破坏
        已验收的「同向回放 / 反向 409 且不写不加锁」语义）：

        1. 金额取锁定行的 `reserved_points`（不信任调用方）；
        2. 幂等键回放判定（同键同参数回放，同键不同参数 `IDEMPOTENCY_CONFLICT`）；
        3. 终局流水判定（同向回放，反向 `IDEMPOTENCY_CONFLICT`，**此时尚未锁钱包**）；
        4. `RESERVE` 流水必须存在；
        5. `billing_status` 必须仍为 `RESERVED`（第二道防线）；
        6. **锁钱包** → 校验预留余额 → 写唯一终局流水 → 更新快照；
        7. 更新 `billing_status` 为 `CONSUMED` / `RELEASED`（不触碰任务 `status`）。
        """

        amount = task.reserved_points
        if amount <= 0:
            raise WalletLedgerError(
                f"任务 {task.id} 的 reserved_points={amount}，无法推导结算金额。"
            )

        billing_status: str
        wallet_delta: WalletDelta
        if entry_type == "CONSUME":
            available_delta = 0
            reserved_delta = -amount
            wallet_delta = WalletDelta(reserved=-amount, lifetime_consumed=amount)
            billing_status = "CONSUMED"
        elif entry_type == "RELEASE":
            available_delta = amount
            reserved_delta = -amount
            wallet_delta = WalletDelta(available=amount, reserved=-amount)
            billing_status = "RELEASED"
        else:  # pragma: no cover - 由内部调用方保证，且终局类型由数据库 CHECK 权威
            raise WalletLedgerError(f"{entry_type} 不是合法的终局流水类型。")

        def _replayed(entry: PointLedger) -> _Settlement:
            """幂等回放：返回第一次的流水。

            不写流水、不推进 `billing_status`，按裁决 C 也不写审计，
            因此不构造快照（旧实现在此读钱包行只为给无人使用的 `applied=False`
            审计填前后快照，属于死取值，已随裁决 C 一并清理）。
            """

            return _Settlement(entry=entry, applied=False, task=task)

        existing = self._replay(
            idempotency_key,
            entry_type=entry_type,
            user_id=task.user_id,
            amount=amount,
            available_delta=available_delta,
            reserved_delta=reserved_delta,
            generation_id=task.id,
        )
        if existing is not None:
            return _replayed(existing)

        settled = self._replay_or_reject_final(task.id, entry_type=entry_type)
        if settled is not None:
            return _replayed(settled)

        self._require_reserve(task.id)
        if task.billing_status != "RESERVED":
            # 这不是「已结算冲突」：真正的已结算/反向场景已在 `_replay_or_reject_final`
            # 以 IDEMPOTENCY_CONFLICT 返回。走到这里说明标志已被推进却查不到终局流水，
            # 即快照与流水互相矛盾 = 账务不变量破坏，按裁决 A 保持内部异常。
            raise WalletLedgerError(
                f"任务 {task.id} 的 billing_status={task.billing_status}，"
                "但流水中没有对应终局记录，账务不变量被破坏。"
            )

        task_before = self._task_snapshot(task)
        wallet = self._locked_wallet(task.user_id)
        wallet_before = self._wallet_snapshot(wallet)
        if wallet.reserved_points < amount:
            raise WalletLedgerError(
                f"用户 {task.user_id} 的预留余额不足，无法 {entry_type}。"
            )

        entry = self._write(
            wallet,
            entry_type=entry_type,
            available_delta=available_delta,
            reserved_delta=reserved_delta,
            amount=amount,
            idempotency_key=idempotency_key,
            reason=reason,
            generation_id=task.id,
            metadata=metadata,
            wallet_delta=wallet_delta,
        )
        self._uow.generation_tasks.set_billing_status(task, billing_status)

        return _Settlement(
            entry=entry,
            applied=True,
            task=task,
            snapshots=_SettlementSnapshots(
                task_before=task_before,
                wallet_before=wallet_before,
                wallet_after=self._wallet_snapshot(wallet),
            ),
        )

    def consume(
        self,
        *,
        user_id: uuid.UUID,
        generation_id: uuid.UUID,
        idempotency_key: str,
        reason: str,
        metadata: dict[str, object] | None = None,
    ) -> PointLedger:
        """结算消费：`reserved -= amount`、`lifetime_consumed += amount`。

        ENG-04 第 3 步：`amount` **不再是入参**，而是锁定任务行后取自
        `generation_tasks.reserved_points`；钱包归属同样以任务行为准。

        与 `RELEASE` 互斥且总共最多一次；同向重复幂等回放，对已 `RELEASED`
        的任务再 `CONSUME` 返回 `IDEMPOTENCY_CONFLICT`（409），不写反向流水。
        任务不存在或不属于 `user_id` 均返回 `GENERATION_NOT_FOUND`（404）。
        """

        task = self._lock_settleable_task(generation_id, user_id=user_id)
        settlement = self._settle_locked(
            task,
            entry_type="CONSUME",
            idempotency_key=idempotency_key,
            reason=reason,
            metadata=metadata,
        )
        return settlement.entry

    def release(
        self,
        *,
        user_id: uuid.UUID,
        generation_id: uuid.UUID,
        idempotency_key: str,
        reason: str,
        metadata: dict[str, object] | None = None,
    ) -> PointLedger:
        """释放预留：`available += amount`、`reserved -= amount`。

        ENG-04 第 3 步：金额来自锁定任务行的 `reserved_points`，不接受调用方传入。

        与 `CONSUME` 互斥且总共最多一次；同向重复幂等回放，对已 `CONSUMED`
        的任务再 `RELEASE` 返回 `IDEMPOTENCY_CONFLICT`（409），不写反向流水。
        任务不存在或不属于 `user_id` 均返回 `GENERATION_NOT_FOUND`（404）。
        """

        task = self._lock_settleable_task(generation_id, user_id=user_id)
        settlement = self._settle_locked(
            task,
            entry_type="RELEASE",
            idempotency_key=idempotency_key,
            reason=reason,
            metadata=metadata,
        )
        return settlement.entry

    # ------------------------------------------------- 人工复核（`LEAD-04` §8）
    def resolve_needs_review(
        self,
        *,
        generation_id: uuid.UUID,
        decision: Literal["CONSUME", "RELEASE"],
        idempotency_key: str,
        admin_id: uuid.UUID,
        request_id: str,
        note: str,
        evidence: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PointLedger:
        """人工结算 `NEEDS_REVIEW` 任务（`LEAD-04` §5.6、§8）。

        契约要点：

        - **只接受 `status=NEEDS_REVIEW` 且 `billing_status=RESERVED`** 的任务；
          任务不存在返回 `GENERATION_NOT_FOUND`（404），状态非 `NEEDS_REVIEW`
          返回 `IDEMPOTENCY_CONFLICT`（409）（裁决 A，均为既有错误码）；
        - `decision` 只能是 `CONSUME` 或 `RELEASE`；
        - 金额取任务行的 `reserved_points`；**决策不改变任务状态**，
          只把 `billing_status` 推进为 `CONSUMED` / `RELEASED`；
        - 同向重复提交幂等回放原决定（返回同一条终局流水），
          相反决定返回既有的 `IDEMPOTENCY_CONFLICT`（409），不新增错误码；
        - **只有首次成功的决定**追加一条 `audit_logs`，留痕管理员、请求 ID、理由、
          证据引用、决策时间、任务快照与前后钱包快照；同向幂等回放与反向拒绝
          都不追加审计（裁决 C：反向拒绝的留痕未来由 Admin API 在独立事务中记录）；
        - 不写、不改任务 `status`，也不覆盖历史流水。
        """

        if decision not in REVIEW_DECISIONS:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, details={"fields": ["decision"]}
            )
        self._require_uuid(admin_id, "admin_id")
        self._require_text(request_id, "request_id", max_length=_REQUEST_ID_MAX_LENGTH)
        self._require_text(note, "note", max_length=_NOTE_MAX_LENGTH)

        task = self._lock_settleable_task(generation_id, user_id=None)
        if task.status != "NEEDS_REVIEW":
            # 裁决 A：前置状态不符是账务/状态机冲突，不是不变量破坏
            # （任务存在、流水自洽），复用既有 IDEMPOTENCY_CONFLICT（409）。
            raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT)

        settlement = self._settle_locked(
            task,
            entry_type=decision,
            idempotency_key=idempotency_key,
            reason=note,
            metadata=metadata,
        )

        # 裁决 C：只有**首次成功**的决定写审计。`snapshots` 只在 `applied=True` 时
        # 存在，因此它同时是「本次确实入账」的判据；同向幂等回放不追加审计，
        # 反向决定已在 `_replay_or_reject_final` 抛 `IDEMPOTENCY_CONFLICT` 冒泡，
        # 根本到不了这里（拒绝尝试的留痕未来由 Admin API 的独立事务记录）。
        snapshots = settlement.snapshots
        if snapshots is not None:
            self._uow.audit_logs.append(
                AuditEntryDraft(
                    actor_type="ADMIN",
                    actor_id=admin_id,
                    action="generation.review_resolution",
                    entity_type="generation_task",
                    entity_id=task.id,
                    request_id=request_id,
                    metadata={
                        "decision": decision,
                        "note": note,
                        "evidence": evidence,
                        "applied": settlement.applied,
                        "ledger_id": str(settlement.entry.id),
                        "entry_type": settlement.entry.entry_type,
                        "amount": settlement.entry.amount,
                        "idempotency_key": idempotency_key,
                        "decided_at": self._clock().isoformat(),
                        "task_before_snapshot": snapshots.task_before,
                        "task_after_snapshot": self._task_snapshot(task),
                        "wallet_before_snapshot": snapshots.wallet_before,
                        "wallet_after_snapshot": snapshots.wallet_after,
                    },
                )
            )
        return settlement.entry

    # -------------------------------------------------- 人工调整（第 5 步）
    def adjust(
        self,
        *,
        user_id: uuid.UUID,
        amount: int,
        adjustment_kind: str,
        idempotency_key: str,
        reason: str,
        admin_id: uuid.UUID,
        request_id: str,
        case_reference: str,
        metadata: dict[str, object] | None = None,
    ) -> PointLedger:
        """人工调整：**不改变预留余额**，符号由 `adjustment_kind` 决定。

        - `POSITIVE_CREDIT`：`available += amount`、`lifetime_credited += amount`
        - `NEGATIVE_CONSUMPTION`：`available -= amount`、`lifetime_consumed += amount`

        ENG-04 第 5 步（`LEAD-04` §5.7、§8）：必须提供 `admin_id`、`request_id`、
        `case_reference`、理由与调整类型；写 `ADJUSTMENT` 流水的同时追加**独立**审计
        日志，并保留调整前后的钱包快照。历史流水不可覆盖、不可删除。
        Admin/RBAC 由调用方负责，本方法只做参数留痕，不实现鉴权。

        总控 2026-09-27 裁决 C：同键同参的幂等回放只返回第一次的流水，
        **不**再追加一条 `applied=False` 的审计；审计只记录首次成功的调整。
        """

        self._require_positive(amount)
        if adjustment_kind not in ADJUSTMENT_KINDS:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, details={"fields": ["adjustment_kind"]}
            )
        self._require_uuid(admin_id, "admin_id")
        self._require_text(request_id, "request_id", max_length=_REQUEST_ID_MAX_LENGTH)
        self._require_text(reason, "reason", max_length=_NOTE_MAX_LENGTH)
        self._require_text(
            case_reference, "case_reference", max_length=_CASE_REFERENCE_MAX_LENGTH
        )

        positive = adjustment_kind == "POSITIVE_CREDIT"
        signed = amount if positive else -amount

        existing = self._replay(
            idempotency_key,
            entry_type="ADJUSTMENT",
            user_id=user_id,
            amount=amount,
            available_delta=signed,
            reserved_delta=0,
        )
        if existing is not None:
            # 裁决 C：同键同参回放直接返回第一次的流水，**不追加** `applied=False`
            # 审计（旧实现在这里读钱包快照只为写一条无人复核过的回放审计）。
            return existing

        wallet = self._locked_wallet(user_id)
        wallet_before = self._wallet_snapshot(wallet)

        # 调用方 metadata 不得伪造 adjustment_kind：kind 最后写入，权威覆盖。
        entry = self._write(
            wallet,
            entry_type="ADJUSTMENT",
            available_delta=signed,
            reserved_delta=0,
            amount=amount,
            idempotency_key=idempotency_key,
            reason=reason,
            metadata={
                **dict(metadata or {}),
                "adjustment_kind": adjustment_kind,
                "admin_id": str(admin_id),
                "case_reference": case_reference,
            },
            wallet_delta=WalletDelta(
                available=signed,
                lifetime_credited=amount if positive else 0,
                lifetime_consumed=0 if positive else amount,
            ),
        )
        wallet_after = self._wallet_snapshot(wallet)

        self._append_adjustment_audit(
            user_id=user_id,
            entry=entry,
            adjustment_kind=adjustment_kind,
            signed_amount=signed,
            admin_id=admin_id,
            request_id=request_id,
            case_reference=case_reference,
            reason=reason,
            wallet_before=wallet_before,
            wallet_after=wallet_after,
            applied=True,
        )
        return entry

    def adjust_wallet(
        self,
        *,
        user_id: uuid.UUID,
        signed_amount: int,
        idempotency_key: str,
        admin_id: uuid.UUID,
        reason: str,
        case_reference: str,
        request_id: str,
        metadata: dict[str, object] | None = None,
    ) -> PointLedger:
        """`LEAD-04` §5 冻结名：以**带符号金额**发起人工调整。

        符号决定 `adjustment_kind`（正 → `POSITIVE_CREDIT`，负 →
        `NEGATIVE_CONSUMPTION`；0 视为参数错误），其余约束与 `adjust` 完全一致。
        """

        if signed_amount == 0:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, details={"fields": ["signed_amount"]}
            )
        return self.adjust(
            user_id=user_id,
            amount=abs(signed_amount),
            adjustment_kind=(
                "POSITIVE_CREDIT" if signed_amount > 0 else "NEGATIVE_CONSUMPTION"
            ),
            idempotency_key=idempotency_key,
            reason=reason,
            admin_id=admin_id,
            request_id=request_id,
            case_reference=case_reference,
            metadata=metadata,
        )

    def _append_adjustment_audit(
        self,
        *,
        user_id: uuid.UUID,
        entry: PointLedger,
        adjustment_kind: str,
        signed_amount: int,
        admin_id: uuid.UUID,
        request_id: str,
        case_reference: str,
        reason: str,
        wallet_before: dict[str, object],
        wallet_after: dict[str, object],
        applied: bool,
    ) -> None:
        """ADJUSTMENT 的**独立**审计记录（`LEAD-04` §5.7）。

        与点数流水同事务追加，只留结构化摘要：不含 Cookie、令牌、签名 URL 等敏感值。

        `applied` 是审计载荷的固定字段（区分「本次入账」与「仅回放」）；裁决 C 之后
        只有首次成功入账会走到这里，故唯一调用点传 `True`，回放路径不再产生审计。
        """

        self._uow.audit_logs.append(
            AuditEntryDraft(
                actor_type="ADMIN",
                actor_id=admin_id,
                action="wallet.adjust",
                entity_type="point_wallet",
                entity_id=entry.wallet_id,
                request_id=request_id,
                metadata={
                    "user_id": str(user_id),
                    "adjustment_kind": adjustment_kind,
                    "signed_amount": signed_amount,
                    "amount": entry.amount,
                    "available_delta": entry.available_delta,
                    "reserved_delta": entry.reserved_delta,
                    "note": reason,
                    "case_reference": case_reference,
                    "idempotency_key": entry.idempotency_key,
                    "ledger_id": str(entry.id),
                    "applied": applied,
                    "adjusted_at": self._clock().isoformat(),
                    "wallet_before_snapshot": wallet_before,
                    "wallet_after_snapshot": wallet_after,
                },
            )
        )

    # ------------------------------------------------------------------ 查询
    def balance(self, *, user_id: uuid.UUID) -> PointBalance:
        """读取余额快照（只读，不加锁）。"""

        wallet = self._uow.point_wallets.get_by_user_id(user_id)
        if wallet is None:
            now = datetime.now(timezone.utc)
            return PointBalance(
                available_points=0,
                reserved_points=0,
                lifetime_credited=0,
                lifetime_consumed=0,
                updated_at=now,
            )
        return PointBalance(
            available_points=wallet.available_points,
            reserved_points=wallet.reserved_points,
            lifetime_credited=wallet.lifetime_credited,
            lifetime_consumed=wallet.lifetime_consumed,
            updated_at=wallet.updated_at,
        )

    def list_ledger(
        self, *, user_id: uuid.UUID, cursor: str | None, limit: int
    ) -> LedgerPage:
        """我的点数流水（`(created_at, id)` 倒序键集分页）。

        只读、不加锁：余额快照与流水的一致性由「同事务写入」保证，
        翻页只做展示，不参与任何账务判定。`limit` 的区间校验属于查询参数，
        由路由层以 `VALIDATION_ERROR` 拒绝（契约第 2.2 节）。
        """

        page = self._uow.point_ledger.list_for_user(
            user_id=user_id, cursor=_decode_cursor(cursor), limit=limit
        )
        last = page.rows[-1] if page.rows else None
        return LedgerPage(
            rows=page.rows,
            next_cursor=(
                _encode_cursor(last.created_at, last.id)
                if last is not None and page.has_more
                else None
            ),
            has_more=page.has_more,
        )


class InviteRedeemService:
    """`PointsPort.redeem_invite` 的实现（ENG-04 第 2 步）。

    在**同一个事务**内依次完成（技术负责人 2026-09-26 裁决 ②）：

    1. 锁定邀请码行并校验可兑换性（哈希比对、状态、有效期、剩余次数）；
    2. 创建或恢复用户，并建立主邮箱绑定；
    3. 写入邀请码兑换记录并递增 `redeemed_count`（用尽时置 `USED`）；
    4. 通过 `PointsService.credit()` 写入 `CREDIT` 流水并更新钱包快照；
    5. 创建 Session（Redis，不参与数据库事务）。

    事务边界由本类持有：成功时 `commit()`，异常时由调用方的 `with` 上下文回滚。
    **ENG-03 的路由与 `RedeemService` 不参与任何账务逻辑。**
    """

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        session_store: object,
        locale: str = DEFAULT_LOCALE,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> None:
        self._uow = uow
        self._session_store = session_store
        self._locale = locale
        self._timezone = timezone

    def redeem_invite(
        self, *, invite_code: str, email: str, session_ttl_seconds: int
    ) -> RedeemOutcome:
        from app.services.auth.invite_code import evaluate_invite_code, hash_invite_code
        from app.services.auth.mappers import to_api_user

        # 1. 锁定邀请码并校验
        code = self._uow.invite_codes.lock_by_hash(hash_invite_code(invite_code))
        state = evaluate_invite_code(code)
        if not state.redeemable or state.code is None:
            raise ApiError(state.error or ErrorCode.INVITE_INVALID)
        locked_code = state.code

        # 2. 创建或恢复用户 + 主邮箱
        email_row = self._uow.emails.find_by_email(email)
        if email_row is None:
            user = self._uow.users.create(locale=self._locale, timezone=self._timezone)
            email_row = self._uow.emails.add(user_id=user.id, email=email, is_primary=True)
        else:
            existing_user = self._uow.users.get(email_row.user_id)
            if existing_user is None or existing_user.status == "DELETED":
                raise ApiError(ErrorCode.SESSION_INVALID)
            if existing_user.status == "SUSPENDED":
                raise ApiError(ErrorCode.ACCOUNT_SUSPENDED)
            user = existing_user

        # 3. 兑换记录（唯一约束：同一用户对同一邀请码只能兑换一次）
        if self._uow.invite_redemptions.find(
            invite_code_id=locked_code.id, user_id=user.id
        ) is not None:
            raise ApiError(ErrorCode.INVITE_ALREADY_USED)

        self._uow.invite_redemptions.add(
            invite_code_id=locked_code.id,
            user_id=user.id,
            points_credited=locked_code.initial_points,
        )
        self._uow.invite_codes.increment_redeemed_count(locked_code)
        if locked_code.redeemed_count >= locked_code.max_redemptions:
            self._uow.invite_codes.mark_used(locked_code)

        # 4. CREDIT 流水 + 钱包快照
        points_service = PointsService(uow=self._uow)
        points_service.credit(
            user_id=user.id,
            amount=locked_code.initial_points,
            idempotency_key=f"invite:{locked_code.id}:{user.id}",
            reason="invite_redeem",
        )

        # 5. Session（Redis，不参与数据库事务）
        token, record = self._session_store.create(
            user_id=user.id,
            email_verified=email_row.verified_at is not None,
        )

        self._uow.commit()

        return RedeemOutcome(
            user=to_api_user(user, email_row),
            points=points_service.balance(user_id=user.id),
            session=record.to_api_model(),
            session_token=token,
        )
