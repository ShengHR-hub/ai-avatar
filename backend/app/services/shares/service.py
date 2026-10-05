"""Share Service（ENG-09）：分享创建、匿名读取与撤销。

职责与事务边界：

- `create_share` 在同一事务里完成「校验所有权与终态 → 写 `shares` → 写 `share_assets`
  → 把**被选中**的资产置 `SHARED` → 追加幂等登记」，任一步失败整笔回滚；
- `read_share` 是匿名读取：实时检查存在性、状态与期限，并校验资产仍然有效；
- `revoke_share` 只改状态与可见性，绝不物理删除任务、流水、审计或 Provider 历史；
- 到期的批量收敛由 `app.services.shares.lifecycle.ShareLifecycle` 负责，
  本模块只通过它做可见性判定，不重复实现一套过期逻辑。

幂等（`app/services/engagement_support.py` 有完整说明）：

```text
1. shares.(user_id, idempotency_key) 业务行  → 同键同载荷回放 / 异载荷稳定冲突
2. audit_logs 幂等登记                      → 同一个键跨任务复用的冲突
3. pg_advisory_xact_lock(作用域, 用户+键)    → 并发下只有一个赢家（数据库级，不新增表/列）
4. 业务表唯一约束                            → 最终兜底
```

**选择集合规范化**：选中集合按 `output_index` 升序排列后再比较与指纹化，
因此「请求里写 [A, B] 还是 [B, A]」是同一次意图的两个写法，不会互相判成冲突。

依据：`03-api-contract.md` 第 4.4 节、`04-ai-provider.md` 第 8 节（可见性不等于授权，
`SHARED` 只在「有一条有效分享」时成立）、`02-database-design.md` 第 3.4/3.9 节。

边界纪律：

- 只通过 `StorageAdapter` 间接接触对象存储，本 Service 不导入任何 SDK；
- 响应载荷**不含** `generation_id`、所有者、Prompt、`storage_key`、`storage_provider`
  或任何内部记录（见 `app.schemas.engagement.ShareView`）；
- 只有**被明确选中**的资产会被置为 `SHARED`；同任务的其他结果保持 `PRIVATE`，
  匿名读取时按 404 处理。
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app.db.models.engagement import Share
from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories import UnitOfWork
from app.repositories.engagement import (
    STATUS_ACTIVE,
    STATUS_REVOKED,
    DuplicateShare,
    IdempotencyScopeBusy,
    ShareAssetDraft,
    ShareAssetLink,
    ShareDraft,
)
from app.schemas.engagement import (
    TITLE_MAX_LENGTH,
    ShareAssetView,
    ShareCreateRequest,
    ShareView,
)
from app.services.engagement_support import (
    ACTION_CREATE_SHARE,
    ENTITY_SHARE,
    conflict,
    fingerprint_of,
    in_progress,
)
from app.services.shares.lifecycle import ShareLifecycle

__all__ = [
    "DEFAULT_SHARE_TTL_DAYS",
    "SHAREABLE_GENERATION_STATUS",
    "SHARE_ASSET_LIMIT",
    "ShareService",
]

#: 只有成功的任务可以分享（`03-api-contract.md` 第 4.4 节）
SHAREABLE_GENERATION_STATUS = "SUCCEEDED"

#: 每次生成固定两张，因此一条分享最多关联两张结果图（契约未声明 minItems/maxItems，
#: 数量规则由本 Service 冻结并在越界时以 `VALIDATION_ERROR` 拒绝）
SHARE_ASSET_LIMIT = 2

#: 分享默认有效期。契约第 12 节把「分享默认有效期」列为**未决项**，本卡取 7 天。
#: 实际 `expires_at` 还会被「选中资产最早的到期时间」收紧（见 `_expiry_for`）：
#: 分享期限**不得晚于**被选中资产的到期时间，否则链接会在图已清理后仍然"有效"。
DEFAULT_SHARE_TTL_DAYS = 7

_READY = "READY"
_RESULT_PURPOSE = "RESULT"


@dataclass(frozen=True, slots=True)
class SelectedAsset:
    """一张被选中分享的结果资产及其呈现顺序。"""

    asset_id: uuid.UUID
    output_index: int


def _new_slug() -> str:
    """不可预测的公开标识：32 字节随机 → 43 字符 URL-safe，不含任何内部 ID 或路径。"""

    return secrets.token_urlsafe(32)


class ShareService:
    """分享聚合的业务规则与事务边界。"""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        clock: Callable[[], datetime] | None = None,
        ttl_days: int = DEFAULT_SHARE_TTL_DAYS,
        lifecycle: ShareLifecycle | None = None,
    ) -> None:
        self._uow = uow
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )
        self._ttl = timedelta(days=ttl_days)
        self._lifecycle = lifecycle or ShareLifecycle(uow=uow, clock=self._clock)

    # ------------------------------------------------------------------ 创建
    def create_share(
        self,
        *,
        user_id: uuid.UUID,
        payload: ShareCreateRequest,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> ShareView:
        """创建分享并返回匿名可读的载荷。同键同载荷回放，异载荷稳定冲突。"""

        requested = self._validated_asset_id_list(payload.output_asset_ids)
        self._require_title_length(payload.title)

        # 0) 数据库级串行化：同一 (用户, 键) 的并发写在这里排队，
        #    因此下面「跨任务冲突」的判定不会与赢家的提交交错。
        #    等待超时（`IdempotencyScopeBusy`）**只**表示「同键请求正在处理中」：
        #    回滚本事务后返回 409 `IDEMPOTENCY_IN_PROGRESS` + `Retry-After`，
        #    而不是把领域信号降级成通用 500。此时尚未写入任何行，回滚即释放。
        try:
            self._uow.shares.lock_idempotency_scope(
                user_id=user_id, idempotency_key=idempotency_key
            )
        except IdempotencyScopeBusy:
            self._uow.rollback()
            raise in_progress() from None

        # 1) 幂等登记先判「同一个键跨任务复用」：这种冲突没有可回放的业务行，
        #    而且必须在误报成「载荷变了」之前被识别出来
        previous = self._uow.ledger.find(
            user_id=user_id, action=ACTION_CREATE_SHARE, idempotency_key=idempotency_key
        )

        # 2) 业务行：并发下的最终裁决者，也是同键回放的第一来源
        existing = self._uow.shares.find_by_idempotency_key(
            user_id=user_id, idempotency_key=idempotency_key
        )

        if previous is not None and existing is None:
            if str(previous.metadata.get("generation_id")) != str(payload.generation_id):
                raise conflict(fields=["generation_id"])
            raise conflict(fields=["output_asset_ids"])

        if existing is not None:
            if existing.generation_id != payload.generation_id:
                raise conflict(fields=["generation_id"])
            if existing.title != payload.title:
                raise conflict(fields=["output_asset_ids"])
            if not self._same_selection(existing, requested):
                raise conflict(fields=["output_asset_ids"])
            return self.to_view(existing)

        task = self._uow.generation_tasks.get_by_id(payload.generation_id)
        if task is None or task.user_id != user_id:
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
        if task.status != SHAREABLE_GENERATION_STATUS:
            raise ApiError(ErrorCode.GENERATION_NOT_SHAREABLE)

        selected = self._select_outputs(
            user_id=user_id, generation_id=payload.generation_id, asset_ids=requested
        )
        asset_ids = tuple(item.asset_id for item in selected)
        fingerprint = self._fingerprint(payload.generation_id, asset_ids, payload.title)

        moment = self._moment()
        try:
            share = self._uow.shares.create(
                ShareDraft(
                    user_id=user_id,
                    generation_id=payload.generation_id,
                    # 主资产 = output_index 最小者（该列 NOT NULL，历史口径不变）
                    asset_id=selected[0].asset_id,
                    slug=_new_slug(),
                    idempotency_key=idempotency_key,
                    title=payload.title,
                    expires_at=self._expiry_for(moment, asset_ids),
                ),
                created_at=moment,
            )
            self._uow.shares.add_assets(
                share,
                tuple(
                    ShareAssetDraft(asset_id=item.asset_id, sort_order=item.output_index)
                    for item in selected
                ),
                created_at=moment,
            )
            # 可见性只给被选中的资产；同任务其他结果保持 PRIVATE
            for item in selected:
                self._uow.shares.set_asset_visibility(item.asset_id, "SHARED")
            self._uow.ledger.record(
                user_id=user_id,
                action=ACTION_CREATE_SHARE,
                entity_type=ENTITY_SHARE,
                entity_id=share.id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                request_id=request_id,
                extra={
                    "generation_id": str(payload.generation_id),
                    "slug": share.slug,
                },
            )
            self._uow.commit()
        except (DuplicateShare, IntegrityError) as error:
            # 咨询锁之外还有唯一约束兜底：输家回滚后按既有行重判（回放或冲突）
            self._uow.rollback()
            winner = self._uow.shares.find_by_idempotency_key(
                user_id=user_id, idempotency_key=idempotency_key
            )
            if (
                winner is not None
                and winner.generation_id == payload.generation_id
                and winner.title == payload.title
                and self._same_selection(winner, requested)
            ):
                return self.to_view(winner)
            raise conflict(fields=["output_asset_ids"]) from error

        return self.to_view(share)

    # ------------------------------------------------------------------ 读取
    def read_share(self, *, slug: str) -> ShareView:
        """匿名读取：不存在、已撤销、已过期、资产失效一律 404。

        **不依赖清理任务**：期限是每次读取时实时比较的，清理只是把状态收敛，
        因此「到期但 Worker 还没跑」不会留下一个仍然可读的公开链接。
        """

        share = self._uow.shares.get_by_slug(slug)
        if share is None or share.status != STATUS_ACTIVE:
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
        if self.is_expired(share):
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)

        view = self.to_view(share, strict=True)
        if not view.outputs:
            # 被分享的资产已不可用（删除/到期/非 READY）：公开链接不再有效。
            # 这一判定是**实时**的：不依赖 `shares.expires_at` 已被收紧，
            # 也不依赖清理 Worker 是否已经跑过。
            raise ApiError(ErrorCode.GENERATION_NOT_FOUND)
        return view

    # ------------------------------------------------------------------ 撤销
    def revoke_share(self, *, user_id: uuid.UUID, share_id: uuid.UUID) -> Share | None:
        """撤销分享（内部行为；V1.0 不新增公开撤销接口）。

        只改状态与可见性：任务、流水、审计、分享与反馈历史全部保留。
        不存在的分享或他人的分享返回 `None`；已撤销的返回原行（幂等）。

        **锁顺序**：本方法只取两级锁，且顺序固定为
        `分享行（第一级） → 关联资产行（升序，第二级）`。
        这与媒体清理的「分享优先」阶段一致，因此不会出现
        「一方持资产等分享、另一方持分享等资产」的死锁。

        **被本次分享选中过的每一张资产都要收敛可见性**，不能只看主资产：
        一条链接分享两张时，只处理第一张会让第二张留在一个没有依据的公开读口上。
        """

        locked = self._uow.shares.lock_share_ids([share_id])
        share = locked[0] if locked else None
        if share is None or share.user_id != user_id:
            return None
        if share.status == STATUS_ACTIVE:
            # 整批一起收敛：`closing` 必须包含**本次要关闭的自己**，
            # 否则 `has_other_valid_share` 会把自己算成"仍然有效"，
            # 于是没有其他分享的资产永远恢复不到 PRIVATE。
            affected = self._lifecycle.closed_share_asset_ids(share)
            self._lifecycle.close_share(share, status=STATUS_REVOKED)
            self._lifecycle.revalidate_assets(
                affected, closing=frozenset({share.id})
            )
            self._uow.commit()
        return share

    # ------------------------------------------------------------------ 视图
    def to_view(self, share: Share, *, strict: bool = False) -> ShareView:
        """把分享行转成匿名可读载荷（只含此刻可用的结果图）。

        `strict=True` 供匿名读取使用：任一张选中资产失效即返回空载荷。
        """

        return ShareView(
            slug=share.slug,
            title=share.title,
            expires_at=share.expires_at,
            created_at=share.created_at,
            outputs=[
                ShareAssetView(
                    asset_id=link.asset_id,
                    output_index=link.sort_order,
                    mime_type=link.mime_type,
                    width=link.width,
                    height=link.height,
                    size_bytes=link.size_bytes,
                )
                for link in self.selected_assets(share, strict=strict)
            ],
        )

    def selected_assets(
        self, share: Share, *, strict: bool = False
    ) -> tuple[ShareAssetLink, ...]:
        """选中资产（含呈现元数据）；关联为空时回落主资产（历史单图分享）。

        `strict=False`（所有者视图）：过滤掉此刻不可用的资产，全部不可用返回空。
        `strict=True`（**匿名公开读取**）：只要有**任何**一张选中资产此刻不可用
        （删除/到期/非 READY RESULT），就整体视为链接失效返回空 —— 匿名链接与
        所有者的作品列表不同，不能悄悄少给一张还宣称链接有效。
        """

        now = self._moment()
        links = self._uow.shares.list_selected_assets(share.id)
        if links:
            servable = [
                link
                for link in links
                if self._asset_is_servable(
                    self._uow.shares.get_asset(link.asset_id), now=now
                )
            ]
            if strict and len(servable) != len(links):
                return ()
            return tuple(servable)

        asset = self._uow.shares.get_asset(share.asset_id)
        if not self._asset_is_servable(asset, now=now):
            return ()
        return (
            ShareAssetLink(
                asset_id=asset.id,
                sort_order=0,
                mime_type=asset.mime_type,
                width=asset.width,
                height=asset.height,
                size_bytes=asset.size_bytes,
            ),
        )

    def asset_has_valid_share(self, asset_id: uuid.UUID) -> bool:
        """该资产**此刻**是否有一条有效分享（用于匿名读取的实时授权）。

        只看 `ACTIVE` + 未过期，不依赖清理任务是否已经把过期行收敛；
        这比「读 `media_assets.visibility`」严格：可见性是快照，
        而公开读取的授权必须能用当前事实解释。
        """

        return bool(self._uow.shares.list_valid_for_asset(asset_id, now=self._moment()))

    # ------------------------------------------------------------------ 判定
    def is_expired(self, share: Share) -> bool:
        """已过 `expires_at`（状态与 `revoked_at` 由调用方判）。"""

        return self._lifecycle.is_expired(share)

    # ------------------------------------------------------------------ 内部
    def _moment(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def _expiry_for(
        self, moment: datetime, asset_ids: tuple[uuid.UUID, ...]
    ) -> datetime:
        """分享期限 = min(默认 TTL, 选中资产最早的到期时间)。

        没有资产过期时间（`expires_at IS NULL`，例如持久资源）时不收紧。
        """

        default = moment + self._ttl
        earliest = self._uow.shares.earliest_expiry_for_assets(asset_ids)
        if earliest is None:
            return default
        if earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=timezone.utc)
        return min(default, earliest)

    @staticmethod
    def _fingerprint(
        generation_id: uuid.UUID, asset_ids: tuple[uuid.UUID, ...], title: str | None
    ) -> str:
        """请求指纹：**规范化后**的选中集合（升序），因此顺序不同不算换载荷。"""

        return fingerprint_of(
            {
                "generation_id": str(generation_id),
                "output_asset_ids": [str(value) for value in sorted(asset_ids, key=str)],
                "title": title,
            }
        )

    def _same_selection(self, share: Share, requested: list[uuid.UUID]) -> bool:
        """分享行上的选中集合是否与本次请求一致（**规范化后**比较）。

        两侧都按 `output_index` 排序，因此「[A, B]」与「[B, A]」判定为同一意图。
        """

        current = list(self._uow.shares.list_selected_asset_ids(share.id))
        if not current:
            current = [share.asset_id]
        return sorted(current, key=str) == sorted(requested, key=str)

    @staticmethod
    def _validated_asset_id_list(raw: list[uuid.UUID]) -> list[uuid.UUID]:
        """数量与去重校验（契约未声明 minItems/maxItems，规则由本卡冻结）。"""

        if not raw or len(raw) > SHARE_ASSET_LIMIT:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, details={"fields": ["output_asset_ids"]}
            )
        if len(set(raw)) != len(raw):
            # 同一张图重复出现是请求错误，不是并发冲突
            raise ApiError(
                ErrorCode.VALIDATION_ERROR, details={"fields": ["output_asset_ids"]}
            )
        return list(raw)

    @staticmethod
    def _require_title_length(title: str | None) -> None:
        """标题长度上限（基线 `title.maxLength = 160`）。

        基线把 `title` 写成可空联合且 `maxLength` 在联合之外；为了让解引用后的
        Schema 与基线逐字一致，长度校验放在这里而不是 `Field(max_length=...)`。
        """

        if title is not None and len(title) > TITLE_MAX_LENGTH:
            raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": ["title"]})

    def _select_outputs(
        self,
        *,
        user_id: uuid.UUID,
        generation_id: uuid.UUID,
        asset_ids: list[uuid.UUID],
    ) -> tuple[SelectedAsset, ...]:
        """校验选中资产确实是该任务的 `READY RESULT` 输出，并**按 output_index 规范化**。"""

        outputs = {
            output.asset_id: output.output_index
            for output in self._uow.generation_tasks.list_outputs(generation_id)
        }
        for asset_id in asset_ids:
            if asset_id not in outputs:
                raise ApiError(ErrorCode.GENERATION_NOT_SHAREABLE)

        # 行锁：**按资产 id 升序**一次性取得，绝不按请求数组顺序取锁。
        # 多资产分享（最多两张）如果按请求顺序加锁，[A,B] 与 [B,A] 两个并发请求
        # 会各自持有对方要的行锁，形成真实的死锁（PG 只会杀掉其中一个并报
        # deadlock detected）。升序是这个聚合里资产行的**唯一**取锁顺序，
        # 生命周期清理与撤销路径也复用同一顺序（见 `lock_assets_by_id`）。
        locked = {
            asset.id: asset
            for asset in self._uow.shares.lock_assets_by_id(
                sorted(asset_ids, key=str)
            )
        }

        selected: list[SelectedAsset] = []
        for asset_id in asset_ids:
            asset = locked.get(asset_id)
            if (
                asset is None
                or asset.user_id != user_id
                or not self._asset_is_servable(asset, now=self._moment())
            ):
                raise ApiError(ErrorCode.GENERATION_NOT_SHAREABLE)
            selected.append(
                SelectedAsset(asset_id=asset_id, output_index=outputs[asset_id])
            )
        selected.sort(key=lambda item: item.output_index)
        return tuple(selected)

    @staticmethod
    def _asset_is_servable(asset: object | None, *, now: datetime) -> bool:
        """资产此刻是否可公开：`READY` + `RESULT` + **未过期**。

        期限必须在这里实时判定，而不是靠 `shares.expires_at` 被收紧或靠清理任务
        已经跑过：`02-database-design.md` 第 4 节的清理是**有界批量**的，可能滞后；
        `shares.expires_at` 只是「当时算出来」的快照。因此创建与读取两侧都按
        「现在」重新核对，边界取 `expires_at <= now` 即视为到期（与
        `effective_expired`、`ShareLifecycle.is_expired` 同一口径）。
        """

        if (
            getattr(asset, "status", None) != _READY
            or getattr(asset, "purpose", None) != _RESULT_PURPOSE
        ):
            return False
        deadline = getattr(asset, "expires_at", None)
        if deadline is None:
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline > now
