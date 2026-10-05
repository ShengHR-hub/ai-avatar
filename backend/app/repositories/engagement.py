"""分享与反馈的数据访问（ENG-09）。

分层约定（`06-development-standard.md` 第 2 节）：

- 只做 SQLAlchemy 查询、行锁与字段赋值，**不自行 `commit`**；事务由 Service 持有；
- **不判定业务规则**：谁可以分享、什么状态可反馈、可见性怎么变，全部由
  `app/services/shares` 与 `app/services/feedback` 决定；
- 幂等的**最终裁决者是数据库唯一约束**（`uq_shares_user_idempotency`、
  `uq_feedback_user_generation`），本层只把冲突转成明确的异常类型，
  由 Service 决定「回放 / 稳定冲突 / 进行中」。

为什么 `Share` 与 `Feedback` 共用本模块：两者同属「用户与产物的互动」这一聚合，
都只有少量查询，拆成两个文件会制造没有边界的碎片（与 `repositories/generation.py`
把任务、输入输出、成本配置放在一起同一先例）。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.engagement import Feedback, Share, ShareAsset

__all__ = [
    "DuplicateFeedback",
    "IdempotencyScopeBusy",
    "DuplicateShare",
    "FeedbackDraft",
    "FeedbackRepository",
    "ShareAssetDraft",
    "ShareAssetLink",
    "ShareDraft",
    "ShareRepository",
    "SqlAlchemyFeedbackRepository",
    "SqlAlchemyShareRepository",
    "STATUS_SHARED",
]

#: 分享状态字面量（数据库 CHECK 为唯一权威，这里只给查询用常量）
STATUS_ACTIVE = "ACTIVE"
STATUS_REVOKED = "REVOKED"
STATUS_EXPIRED = "EXPIRED"

#: 生命周期**唯一拥有**的资产可见性：`PRIVATE` 是默认值、`PUBLIC` 是有意发布，
#: 两者都不由分享收敛写入（见 `app/services/shares/lifecycle.py` 模块头）。
STATUS_SHARED = "SHARED"


class DuplicateShare(RuntimeError):
    """`(user_id, idempotency_key)` 已存在（唯一约束裁决）。"""


class DuplicateFeedback(RuntimeError):
    """`(user_id, generation_id)` 已存在（唯一约束裁决）。"""


#: 幂等作用域的咨询锁命名空间：`pg_advisory_xact_lock(int4, int4)` 两个键。
#: 用固定命名空间 + 32 位摘要，避免与其它模块的咨询锁相撞。
_IDEMPOTENCY_LOCK_NAMESPACE: dict[str, int] = {"share": 0x53484152, "feedback": 0x4644424B}
_LOCK_KEY_MASK = 0x7FFFFFFF

#: 幂等作用域的最长等待（`SET LOCAL lock_timeout` 的取值，字符串形式）。
#: 取值是「同一个键的写路径正常耗时」的量级：真正的业务事务只有几条语句，
#: 5 秒还没拿到锁意味着另一个同键请求卡住了，应当让调用方稍后重试而不是继续等。
_IDEMPOTENCY_LOCK_TIMEOUT = "5s"


class IdempotencyScopeBusy(RuntimeError):
    """同一个 `(用户, 幂等键)` 正被另一个请求占用（咨询锁等待超时）。

    这是一个**领域信号**，不是「随便什么数据库错误」：只有
    `pg_advisory_xact_lock` 等待超过 `_IDEMPOTENCY_LOCK_TIMEOUT` 才产生它。
    上层据此返回 409 `IDEMPOTENCY_IN_PROGRESS` + `Retry-After`。
    """


def share_covers_asset(asset_id: uuid.UUID):
    """SQL 条件：这条 `shares` 行**此刻**是否把 `asset_id` 当作自己的选中资产。

    返修第五轮冻结的语义（历史单图分享的读取与收敛路径）：

    ```text
    share_assets 有行  → 只按**明确选中的集合**授权（关联表里必须有这一行）
    share_assets 无行  → 回落 shares.asset_id（0021 之前的历史单图分享）
    ```

    两者是**互斥**的，不是简单的 `OR`：`OR(关联表命中, 主资产命中)` 会让
    「有关联行、但主资产恰好等于查询资产」这种数据异常也被当成授权；这里要求
    有关联行时**只**认关联表。

    为什么必须补这条回落：`share_assets` 是 0021 才引入的。只 JOIN 关联表会让
    「只有 `shares.asset_id` 的历史分享」在匿名授权、有效分享查询、计数与可见性
    收敛里全部查不到 —— 表现是「历史分享仍 ACTIVE，资产却被收敛成 PRIVATE」，
    公开链接还在、图却读不到。
    """

    linked = (
        select(ShareAsset.share_id)
        .where(ShareAsset.share_id == Share.id, ShareAsset.asset_id == asset_id)
        .exists()
    )
    has_links = select(ShareAsset.share_id).where(ShareAsset.share_id == Share.id).exists()
    return or_(linked, and_(Share.asset_id == asset_id, ~has_links))


def _acquire_idempotency_lock(
    session: Session, *, scope: str, user_id: uuid.UUID, key: str
) -> None:
    """取得「本作用域 + 本用户 + 本键」的事务级咨询锁。

    为什么用咨询锁而不是新表/新列：`02-database-design.md` 禁止为本卡新增表或列，
    而**同一个幂等键跨任务复用**这种冲突没有可查的业务行（`feedback` 的唯一键是
    `(user_id, generation_id)`，`shares` 的是 `(user_id, idempotency_key)`），
    应用层的「先查后写」在两个并发请求下会双双通过。事务级咨询锁让同一
    `(user_id, key)` 的写请求在**数据库层**排队，锁随提交/回滚自动释放，
    因此不需要额外的清理，也不改变任何既有错误语义。

    锁键只含用户 ID 与键摘要（不可逆），不落库、不进日志。

    等待上限是**有界**的（`lock_timeout`）：真正卡住的事务不应该让请求无限挂着。
    超时抛 `psycopg.errors.LockNotAvailable`，由上层映射为
    `IDEMPOTENCY_IN_PROGRESS`（409 + `Retry-After`）——**只**在这个位置、**只**对
    这一个异常做这个映射：它是「幂等作用域正被另一个同键请求占用」的唯一证据。
    其它数据库异常（行锁超时、死锁、序列化失败、约束冲突）都保持既有语义，
    不被当成「幂等处理中」。
    """

    #: 作用域要含**用户维度**，否则两个不同用户的同一个键会互相排队（无谓串行化）。
    material = f"{scope}\x00{user_id}\x00{key}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    lock_key = int.from_bytes(digest[:4], "big") & _LOCK_KEY_MASK
    # 惰性导入：把「数据库驱动」与「SQLAlchemy 异常包装」这两个具体依赖
    # 限制在本函数内，模块级只暴露与驱动无关的 `IdempotencyScopeBusy`
    from sqlalchemy.exc import OperationalError
    from psycopg.errors import LockNotAvailable

    session.execute(
        # `SET LOCAL` 不接受绑定参数（PG 语法限制），因此用等价的 `set_config()`：
        # `is_local = true` 与 `SET LOCAL` 语义一致，事务结束即失效。
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": _IDEMPOTENCY_LOCK_TIMEOUT},
    )
    try:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
            {"namespace": _IDEMPOTENCY_LOCK_NAMESPACE[scope], "key": lock_key},
        )
    except OperationalError as exc:
        # SQLAlchemy 把驱动异常包在 `OperationalError` 里，必须看 `orig` 才能
        # 把它和**其它** OperationalError（连接中断、语句超时等）区分开。
        # 只有「就是这条咨询锁等待超时」才意味着「同键请求正在处理中」。
        if not isinstance(exc.orig, LockNotAvailable):
            raise
        raise IdempotencyScopeBusy() from exc

@dataclass(frozen=True, slots=True)
class ShareDraft:
    """一条待创建的分享行。

    `asset_id` 是**主资产**（选中输出中 `output_index` 最小者）：该列在数据库里是
    `NOT NULL`，主资产口径保证它始终成立，也让历史单图分享无需改写。
    """

    user_id: uuid.UUID
    generation_id: uuid.UUID
    asset_id: uuid.UUID
    slug: str
    idempotency_key: str
    title: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ShareAssetDraft:
    """一条待创建的分享-资产关联。"""

    asset_id: uuid.UUID
    sort_order: int


@dataclass(frozen=True, slots=True)
class ShareAssetLink:
    """关联行 + 该资产的呈现元数据（匿名读取响应要用）。"""

    asset_id: uuid.UUID
    sort_order: int
    mime_type: str
    width: int | None
    height: int | None
    size_bytes: int


@dataclass(frozen=True, slots=True)
class FeedbackDraft:
    user_id: uuid.UUID
    generation_id: uuid.UUID
    rating: int
    category: str
    content: str | None = None


@runtime_checkable
class ShareRepository(Protocol):
    """分享聚合端口。"""

    def get_by_slug(self, slug: str) -> Share | None: ...

    def get_by_id(self, share_id: uuid.UUID) -> Share | None: ...

    def lock_by_id(self, share_id: uuid.UUID) -> Share | None:
        """`SELECT ... FOR UPDATE`：撤销/过期与读取都走同一把行锁。"""
        ...

    def lock_asset(self, asset_id: uuid.UUID) -> Any | None:
        """`SELECT ... FOR UPDATE` 媒体行（分享写入与生命周期清理共用的锁）。"""
        ...

    def lock_assets_by_id(self, asset_ids: list[uuid.UUID]) -> tuple[Any, ...]:
        """按**传入顺序**一次性锁住多张资产行。

        调用方必须传入**已排序**的 id 列表：这是本聚合里资产行的唯一取锁顺序，
        多资产分享与生命周期收敛都靠它避免 `[A,B]` / `[B,A]` 之间的死锁。
        """
        ...

    def refresh_asset(self, asset: Any) -> None:
        """让 Session 重新从数据库读取该资产行（丢弃 identity map 里的旧快照）。

        可见性判定必须基于**刷新后的数据库事实**：同一事务里如果先前读过这一行，
        `Session` 会直接返回缓存实例，于是「关闭分享后判断是否还有其他有效分享」
        会拿过期前的可见性做决定。
        """
        ...

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> Share | None: ...

    def lock_idempotency_scope(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> None:
        """数据库级幂等作用域串行化（事务级咨询锁，**不新增表或列**）。

        同一 `(user_id, key)` 的并发写请求在这里排队，因此「同一个键跨任务复用」
        这类**没有业务行可查**的冲突也能被数据库层挡住，而不是只靠应用层查询。
        """
        ...

    def lock_share_ids(self, share_ids: list[uuid.UUID]) -> tuple[Share, ...]:
        """按**传入顺序**（必须升序）一次锁住多条分享行。

        分享行是全局取锁顺序的**第一级**：任何事务都不得在持有资产行锁之后再
        来取分享行锁，否则会与「先锁分享、再锁资产」的撤销路径直接死锁。
        """
        ...

    def lock_active_for_asset(self, asset_id: uuid.UUID) -> tuple[Share, ...]:
        """引用该资产、且仍是 `ACTIVE` 的分享，**按 id 升序加行锁**。

        供「资产失效 → 收敛它的分享」使用：先锁分享（第一级），再让
        `close_share` 去锁资产（第二级），顺序与撤销路径完全一致。
        """
        ...

    def count_active_for_asset(self, asset_id: uuid.UUID) -> int:
        """该资产当前还有多少条 `ACTIVE` 分享（**不加锁**，用于延迟判定）。"""
        ...

    def count_active_for_assets(
        self, asset_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """这些资产各自的 `ACTIVE` 分享数（不加锁）。"""
        ...

    def peek_expired_shares(self, *, now: datetime, limit: int) -> tuple[Share, ...]:
        """已过期的 `ACTIVE` 分享（**不加锁**），按 id 升序（清理的预读阶段）。"""
        ...

    def peek_active_for_assets(self, asset_ids: list[uuid.UUID]) -> tuple[Share, ...]:
        """引用这些资产的 `ACTIVE` 分享（**不加锁**），按 id 升序。

        让"引用失效资产"的分享进入与"自身到期"同一批收敛目标，
        从而不必在持有资产锁时回头去查或锁分享。
        """
        ...

    def peek_active_for_invalid_assets(
        self, *, now: datetime, limit: int
    ) -> tuple[Share, ...]:
        """引用失效资产的 `ACTIVE` 分享（**不加锁**），按 id 升序，最多 `limit` 条。

        分享收敛的**驱动集合**：以分享为行，关闭一条就少一条，因此不会因为
        "已收敛的失效资产仍占据候选头部"而饿死后排（见实现处的说明）。
        """
        ...

    def create(self, draft: ShareDraft, *, created_at: datetime) -> Share: ...

    def add_assets(
        self,
        share: Share,
        links: tuple[ShareAssetDraft, ...],
        *,
        created_at: datetime,
    ) -> None: ...

    def list_selected_assets(self, share_id: uuid.UUID) -> tuple[ShareAssetLink, ...]:
        """选中资产的呈现元数据；关联为空时返回空元组（由 Service 回落主资产）。"""
        ...

    def list_selected_asset_ids(self, share_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        """选中资产的 ID（失效收敛要按**全部**关联资产重算可见性）。"""
        ...

    def get_asset(self, asset_id: uuid.UUID) -> Any | None:
        """读取媒体行（Service 用它做回落与校验，不经过媒体仓储以免耦合）。"""
        ...

    def list_valid_for_asset(self, asset_id: uuid.UUID, *, now: datetime) -> tuple[Share, ...]:
        """引用该资产且**当下有效**（ACTIVE 且未过期）的分享。"""
        ...

    def earliest_expiry_for_assets(
        self, asset_ids: tuple[uuid.UUID, ...]
    ) -> datetime | None:
        """这一组资产里最早的 `expires_at`（分享期限不得晚于它）。"""
        ...

    def list_expired(self, *, now: datetime, limit: int) -> tuple[Share, ...]:
        """ACTIVE 但已过期的分享（生命周期收敛用，带行锁）。"""
        ...
    def list_active_for_asset(self, asset_id: uuid.UUID) -> tuple[Share, ...]: ...

    def set_status(self, share: Share, status: str, *, changed_at: datetime) -> Share: ...

    def set_asset_visibility(self, asset_id: uuid.UUID, visibility: str) -> None: ...


@runtime_checkable
class FeedbackRepository(Protocol):
    """反馈聚合端口。"""

    def find(
        self, *, user_id: uuid.UUID, generation_id: uuid.UUID
    ) -> Feedback | None: ...

    def lock_idempotency_scope(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> None:
        """与 `ShareRepository.lock_idempotency_scope` 同一机制、不同作用域名。"""
        ...

    def create(self, draft: FeedbackDraft, *, created_at: datetime) -> Feedback: ...


class SqlAlchemyShareRepository:
    """`ShareRepository` 的 SQLAlchemy 实现（不提交事务）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------- 只读
    def get_by_slug(self, slug: str) -> Share | None:
        statement = select(Share).where(Share.slug == slug).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def get_by_id(self, share_id: uuid.UUID) -> Share | None:
        statement = select(Share).where(Share.id == share_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def lock_by_id(self, share_id: uuid.UUID) -> Share | None:
        statement = (
            select(Share).where(Share.id == share_id).limit(1).with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def lock_asset(self, asset_id: uuid.UUID) -> Any | None:
        from app.db.models.media import MediaAsset

        statement = (
            select(MediaAsset).where(MediaAsset.id == asset_id).limit(1).with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def lock_assets_by_id(self, asset_ids: list[uuid.UUID]) -> tuple[Any, ...]:
        """一次 `SELECT ... FOR UPDATE` 锁住多张资产行（按传入顺序加锁）。

        PostgreSQL 的行锁按 WHERE 命中的顺序逐个获取，因此调用方给出的顺序就是
        实际的加锁顺序。本方法不做排序（排序是调用方契约），但会**拒绝未排序的
        输入** —— 这是防止「按请求数组顺序取锁」这个死锁源再次回归的最后一道闸：
        错误用法在测试里立刻炸，而不是在生产上偶发死锁。
        """

        from app.db.models.media import MediaAsset

        ordered = list(asset_ids)
        if not ordered:
            return ()
        if ordered != sorted(ordered, key=str):
            raise ValueError(
                "lock_assets_by_id 要求按 id 升序传入：资产行的取锁顺序必须是"
                "全聚合唯一的，否则并发多资产分享会死锁。"
            )
        statement = (
            select(MediaAsset)
            .where(MediaAsset.id.in_(ordered))
            .order_by(MediaAsset.id.asc())
            .with_for_update()
            # 与 `lock_share_ids` 同理：预读阶段可能已把这一行载进 identity map，
            # 锁后必须看到**提交后的事实**（期限被改回未来、状态被终结等）。
            .execution_options(populate_existing=True)
        )
        return tuple(self._session.execute(statement).scalars().all())

    def refresh_asset(self, asset: Any) -> None:
        """`Session.refresh`：丢弃缓存快照，从数据库重新读取该资产行。"""

        self._session.refresh(asset)

    def lock_idempotency_scope(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> None:
        _acquire_idempotency_lock(
            self._session, scope="share", user_id=user_id, key=idempotency_key
        )

    def lock_share_ids(self, share_ids: list[uuid.UUID]) -> tuple[Share, ...]:
        """一次 `SELECT … FOR UPDATE` 锁住多条分享行（按传入顺序，必须升序）。

        **`populate_existing=True` 是必须的**：清理会先"预读"目标集合（同一事务里
        把 `Share` 载进 identity map），之后才加锁。若期间另一事务通过正式
        `ShareService` 撤销了其中一条，SQLAlchemy 默认会把预读到的**旧快照**
        原样返回（同一主键已在 identity map 里），于是 `close_share` 会把
        `REVOKED` 覆盖成 `EXPIRED`。强制从数据库重新取值后，调用方在锁内看到的
        就是**提交后的事实**。
        """

        ordered = list(share_ids)
        if not ordered:
            return ()
        if ordered != sorted(ordered, key=str):
            raise ValueError(
                "lock_share_ids 要求按 id 升序传入：分享行是全局取锁顺序的第一级，"
                "顺序不唯一会让撤销与清理互相等待。"
            )
        statement = (
            select(Share)
            .where(Share.id.in_(ordered))
            .order_by(Share.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return tuple(self._session.execute(statement).scalars().all())

    def lock_active_for_asset(self, asset_id: uuid.UUID) -> tuple[Share, ...]:
        """引用该资产的 `ACTIVE` 分享，按 id 升序**加行锁**。

        与 `list_active_for_asset` 同一套「关联表有行按选中集合、无行回落主资产」
        语义（见 `share_covers_asset`）：历史单图分享也必须被算进来，否则
        「资产失效 → 收敛它的分享」会漏掉这类分享。
        """

        statement = (
            select(Share)
            .where(Share.status == STATUS_ACTIVE, share_covers_asset(asset_id))
            .order_by(Share.id.asc())
            .with_for_update(of=Share)
        )
        return tuple(self._session.execute(statement).scalars().all())

    def count_active_for_asset(self, asset_id: uuid.UUID) -> int:
        """该资产当前还有多少条 `ACTIVE` 分享（不加锁，用于延迟判定）。

        含历史单图回落：只数关联表会让「历史分享还在」的资产被当成"没人引用"，
        于是资产阶段把它判成可清理、或可见性被错误收敛。
        """

        statement = (
            select(func.count(Share.id))
            .select_from(Share)
            .where(Share.status == STATUS_ACTIVE, share_covers_asset(asset_id))
        )
        return int(self._session.execute(statement).scalar_one())

    def lock_expired_shares(self, *, now: datetime, limit: int) -> tuple[Share, ...]:
        """已过期的 `ACTIVE` 分享，按 **id 升序加行锁**（清理的分享优先阶段用）。"""

        statement = (
            select(Share)
            .where(
                Share.status == STATUS_ACTIVE,
                Share.expires_at.is_not(None),
                Share.expires_at <= now,
            )
            .order_by(Share.id.asc())
            .limit(limit)
            .with_for_update()
        )
        return tuple(self._session.execute(statement).scalars().all())

    # ------------------------------------------------------- 预读（不加锁）
    def peek_expired_shares(self, *, now: datetime, limit: int) -> tuple[Share, ...]:
        """已过期的 `ACTIVE` 分享（**不加锁**），按 id 升序。

        清理先用它决定"锁谁"，再按升序一次性取锁：预读阶段取锁正是
        「持资产等分享」死锁的来源。
        """

        statement = (
            select(Share)
            .where(
                Share.status == STATUS_ACTIVE,
                Share.expires_at.is_not(None),
                Share.expires_at <= now,
            )
            .order_by(Share.id.asc())
            .limit(limit)
        )
        return tuple(self._session.execute(statement).scalars().all())

    def peek_active_for_assets(self, asset_ids: list[uuid.UUID]) -> tuple[Share, ...]:
        """引用这些资产的 `ACTIVE` 分享（**不加锁**），按 id 升序。

        让"引用失效资产"的分享进入与"自身到期"同一批收敛目标，
        从而不必在持有资产锁时回头去查/锁分享。

        命中口径与授权口径**同一处**（`share_covers_asset`）：
        关联表有行只按明确选中集合，无行回落 `shares.asset_id`（历史单图分享）。
        """

        if not asset_ids:
            return ()
        statement = (
            select(Share)
            .where(
                Share.status == STATUS_ACTIVE,
                or_(*[share_covers_asset(asset_id) for asset_id in asset_ids]),
            )
            .order_by(Share.id.asc())
        )
        return tuple(self._session.execute(statement).scalars().all())

    def peek_active_for_invalid_assets(
        self, *, now: datetime, limit: int
    ) -> tuple[Share, ...]:
        """**引用了失效资产**的 `ACTIVE` 分享（不加锁），按 id 升序，最多 `limit` 条。

        这是分享收敛的**驱动集合**（与「自身到期」并列）。它直接以**分享**为行，
        因此天然不会饿死：一条分享被关闭后就从结果里消失，下一轮自然轮到后面的候选。

        为什么不能改成"先取一批失效资产、再反查它们的分享"：失效资产的排序是
        **资产 id**，而已收敛（分享已 `EXPIRED`）的资产仍然是"失效资产"，会一直
        占据批次头部，于是 `limit` 之内的候选永远被它们占满、后面的分享永远轮不到
        （第五轮在真实隔离库上复现：连续多轮 `shares_expired` 恒为 0）。

        失效口径与 `MediaAssetRepository.peek_invalid` 一致：
        逻辑删除 ∪ 状态 `DELETED`/`EXPIRED` ∪ 已过 `expires_at`。
        "这条分享是否引用了失效资产"沿用 `share_covers_asset` 的授权语义
        （关联表有行只按选中集合、无行回落主资产）。
        """

        from app.db.models.media import MediaAsset

        invalid = or_(
            MediaAsset.deleted_at.is_not(None),
            MediaAsset.status.in_(("DELETED", "EXPIRED")),
            and_(
                MediaAsset.expires_at.is_not(None),
                MediaAsset.expires_at <= now,
            ),
        )
        linked_invalid = (
            select(ShareAsset.share_id)
            .join(MediaAsset, MediaAsset.id == ShareAsset.asset_id)
            .where(ShareAsset.share_id == Share.id, invalid)
            .exists()
        )
        has_links = select(ShareAsset.share_id).where(ShareAsset.share_id == Share.id).exists()
        primary_invalid = (
            select(MediaAsset.id)
            .where(MediaAsset.id == Share.asset_id, invalid)
            .exists()
        )
        statement = (
            select(Share)
            .where(
                Share.status == STATUS_ACTIVE,
                or_(linked_invalid, and_(~has_links, primary_invalid)),
            )
            .order_by(Share.id.asc())
            .limit(limit)
        )
        return tuple(self._session.execute(statement).scalars().all())

    def count_active_for_assets(
        self, asset_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """一次查出这些资产各自还有多少条 `ACTIVE` 分享（不加锁）。

        每个资产用同一条 `share_covers_asset` 条件（关联表优先、无行回落主资产），
        因此历史单图分享同样被计入。逐资产执行是有意的：这条条件含相关子查询，
        拼成一条 `UNION ALL` 只会更难读，而调用方的批量上限是 50。
        """

        if not asset_ids:
            return {}
        counts: dict[uuid.UUID, int] = {}
        for asset_id in asset_ids:
            statement = (
                select(func.count(Share.id))
                .select_from(Share)
                .where(Share.status == STATUS_ACTIVE, share_covers_asset(asset_id))
            )
            counts[asset_id] = int(self._session.execute(statement).scalar_one())
        return counts

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> Share | None:
        statement = (
            select(Share)
            .where(Share.user_id == user_id, Share.idempotency_key == idempotency_key)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def get_asset(self, asset_id: uuid.UUID) -> Any | None:
        from app.db.models.media import MediaAsset

        statement = select(MediaAsset).where(MediaAsset.id == asset_id).limit(1)
        return self._session.execute(statement).scalar_one_or_none()

    def list_selected_assets(self, share_id: uuid.UUID) -> tuple[ShareAssetLink, ...]:
        from app.db.models.media import MediaAsset

        statement = (
            select(
                ShareAsset.asset_id,
                ShareAsset.sort_order,
                MediaAsset.mime_type,
                MediaAsset.width,
                MediaAsset.height,
                MediaAsset.size_bytes,
            )
            .join(MediaAsset, MediaAsset.id == ShareAsset.asset_id)
            .where(ShareAsset.share_id == share_id)
            .order_by(ShareAsset.sort_order.asc())
        )
        return tuple(
            ShareAssetLink(
                asset_id=row.asset_id,
                sort_order=row.sort_order,
                mime_type=row.mime_type,
                width=row.width,
                height=row.height,
                size_bytes=row.size_bytes,
            )
            for row in self._session.execute(statement).all()
        )

    def list_valid_for_asset(
        self, asset_id: uuid.UUID, *, now: datetime
    ) -> tuple[Share, ...]:
        """引用该资产且**当下有效**（ACTIVE 且未过期）的分享。

        这是匿名/第三方读取 `SHARED` 资产的**实时授权依据**（`ShareService.
        asset_has_valid_share`）。命中口径见 `share_covers_asset`：
        历史单图分享（没有 `share_assets` 行）同样算数 —— 只 JOIN 关联表会让
        「历史分享仍 ACTIVE、资产却被收敛成 PRIVATE」的资产匿名读不到图。
        """

        statement = (
            select(Share)
            .where(
                Share.status == STATUS_ACTIVE,
                (Share.expires_at.is_(None)) | (Share.expires_at > now),
                share_covers_asset(asset_id),
            )
            .order_by(Share.id.asc())
        )
        return tuple(self._session.execute(statement).scalars().all())

    def earliest_expiry_for_assets(
        self, asset_ids: tuple[uuid.UUID, ...]
    ) -> datetime | None:
        """这一组资产里最早的 `expires_at`；全部为 `NULL` 时返回 `None`。

        分享期限不得晚于被选中资产的最早到期时间，否则链接会在图已清理之后仍然"有效"。
        """

        if not asset_ids:
            return None
        from app.db.models.media import MediaAsset

        statement = select(func.min(MediaAsset.expires_at)).where(
            MediaAsset.id.in_(asset_ids)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_expired(self, *, now: datetime, limit: int) -> tuple[Share, ...]:
        """已到期的 ACTIVE 分享；**带行锁**，避免与并发的 ShareService 写入交错。"""

        statement = (
            select(Share)
            .where(
                Share.status == STATUS_ACTIVE,
                Share.expires_at.is_not(None),
                Share.expires_at <= now,
            )
            .order_by(Share.expires_at.asc(), Share.id.asc())
            .limit(limit)
            .with_for_update()
        )
        return tuple(self._session.execute(statement).scalars().all())

    def list_active_for_asset(self, asset_id: uuid.UUID) -> tuple[Share, ...]:
        """引用该资产的 `ACTIVE` 分享（不加锁）。

        可见性收敛用它判断"还有没有其他有效分享"（`ShareLifecycle.
        has_other_valid_share`）。命中口径与授权口径一致（`share_covers_asset`）：
        漏掉历史单图分享会让「撤销新分享 → 历史分享还在，资产却被改成 PRIVATE」。
        """

        statement = (
            select(Share)
            .where(Share.status == STATUS_ACTIVE, share_covers_asset(asset_id))
            .order_by(Share.id.asc())
        )
        return tuple(self._session.execute(statement).scalars().all())

    def list_selected_asset_ids(self, share_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        statement = (
            select(ShareAsset.asset_id)
            .where(ShareAsset.share_id == share_id)
            .order_by(ShareAsset.sort_order.asc())
        )
        return tuple(self._session.execute(statement).scalars().all())

    # ------------------------------------------------------------------- 写入
    def create(self, draft: ShareDraft, *, created_at: datetime) -> Share:
        share = Share(
            slug=draft.slug,
            user_id=draft.user_id,
            generation_id=draft.generation_id,
            asset_id=draft.asset_id,
            title=draft.title,
            status=STATUS_ACTIVE,
            expires_at=draft.expires_at,
            idempotency_key=draft.idempotency_key,
            created_at=created_at,
        )
        self._session.add(share)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DuplicateShare() from exc
        return share

    def add_assets(
        self,
        share: Share,
        links: tuple[ShareAssetDraft, ...],
        *,
        created_at: datetime,
    ) -> None:
        for link in links:
            self._session.add(
                ShareAsset(
                    share_id=share.id,
                    asset_id=link.asset_id,
                    sort_order=link.sort_order,
                    created_at=created_at,
                )
            )
        self._session.flush()

    def set_status(self, share: Share, status: str, *, changed_at: datetime) -> Share:
        share.status = status
        if status == STATUS_REVOKED:
            share.revoked_at = changed_at
        self._session.flush()
        return share

    def set_asset_visibility(self, asset_id: uuid.UUID, visibility: str) -> None:
        from app.db.models.media import MediaAsset

        asset = self._session.get(MediaAsset, asset_id)
        if asset is None:
            return
        asset.visibility = visibility
        self._session.flush()


class SqlAlchemyFeedbackRepository:
    """`FeedbackRepository` 的 SQLAlchemy 实现（不提交事务）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find(self, *, user_id: uuid.UUID, generation_id: uuid.UUID) -> Feedback | None:
        statement = (
            select(Feedback)
            .where(Feedback.user_id == user_id, Feedback.generation_id == generation_id)
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def lock_idempotency_scope(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> None:
        _acquire_idempotency_lock(
            self._session, scope="feedback", user_id=user_id, key=idempotency_key
        )

    def create(self, draft: FeedbackDraft, *, created_at: datetime) -> Feedback:
        row = Feedback(
            user_id=draft.user_id,
            generation_id=draft.generation_id,
            rating=draft.rating,
            category=draft.category,
            content=draft.content,
            status="OPEN",
            created_at=created_at,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise DuplicateFeedback() from exc
        return row
