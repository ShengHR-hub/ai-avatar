"""ShareService 单元测试（ENG-09）。

覆盖（契约 `03-api-contract.md` 第 4.4 节 + 技术负责人 2026-10-02 裁决）：

- 只有所有者的 `SUCCEEDED` 任务、且目标是该任务 `READY RESULT` 输出时可分享；
- **只公开被选中的资产**：同任务未选中的结果保持 `PRIVATE`（匿名读取 404 的依据）；
- 创建分享 / 关联 / `SHARED` 可见性同一事务；失败整体回滚，不留半个状态；
- slug 不可预测、不含内部 ID 或路径；
- 同键同载荷回放、同键异载荷稳定冲突、同键跨任务冲突；
- 匿名读取实时检查状态与期限；撤销/过期/不存在一律 404；
- 响应载荷不含 `generation_id`、所有者、Prompt 或存储信息；
- 撤销后没有其他有效分享时恢复 `PRIVATE`；还有别的有效分享时维持 `SHARED`。

全部使用内存替身，不启动 PostgreSQL / Redis / 对象存储，不调用任何 Provider。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from pydantic import ValidationError

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.repositories.generation import GenerationOutputDraft
from app.schemas.engagement import ShareCreateRequest, as_share_data
from app.services.shares.lifecycle import ShareLifecycle
from app.services.shares.service import DEFAULT_SHARE_TTL_DAYS, ShareService
from tests.services.fakes import (
    EngagementUnitOfWork,
    MutableClock,
    make_generation_task_row,
    make_media_asset_row,
    make_user_row,
)

CLOCK_START = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class Harness:
    def __init__(self, uow: EngagementUnitOfWork, clock: MutableClock) -> None:
        self.uow = uow
        self.clock = clock
        self.service = ShareService(uow=uow, clock=clock)

    def create_share(self, **kwargs):
        """以 `with uow:` 调用 Service，镜像真实请求的事务边界。

        `get_uow` 依赖在异常退出时会回滚未提交的事务；直接调 Service 会丢掉这层，
        回滚相关的用例必须走这条路径才有意义。
        """

        with self.uow:
            return self.service.create_share(**kwargs)

    def revoke_share(self, **kwargs):
        with self.uow:
            return self.service.revoke_share(**kwargs)

    def seed_task(
        self,
        *,
        user_id: uuid.UUID,
        status: str = "SUCCEEDED",
        asset_status: str = "READY",
        outputs: int = 2,
        purpose: str = "RESULT",
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """构造一个带 N 张结果图的任务；返回 (generation_id, [asset_id...])。"""

        task = make_generation_task_row(
            user_id=user_id, status=status, now=self.clock()
        )
        self.uow.generation_tasks.seed(task)
        asset_ids: list[uuid.UUID] = []
        for index in range(outputs):
            asset = make_media_asset_row(
                user_id=user_id,
                now=self.clock(),
                purpose=purpose,
                asset_type=purpose,
                status=asset_status,
                width=1024,
                height=1024,
                size_bytes=1024 * (index + 1),
            )
            self.uow.media.register(asset)
            self.uow.generation_tasks.create_output(
                GenerationOutputDraft(
                    generation_id=task.id, asset_id=asset.id, output_index=index
                )
            )
            asset_ids.append(asset.id)
        return task.id, asset_ids


@pytest.fixture
def harness() -> Harness:
    clock = MutableClock(CLOCK_START)
    uow = EngagementUnitOfWork(clock=clock)
    return Harness(uow, clock)


def _payload(generation_id: uuid.UUID, asset_ids: list[uuid.UUID], **extra):
    return ShareCreateRequest(
        generation_id=generation_id, output_asset_ids=asset_ids, **extra
    )


# --------------------------------------------------------------------------- #
# 创建：所有权、终态与选中集合
# --------------------------------------------------------------------------- #
def test_two_selected_results_are_both_shared(harness: Harness) -> None:
    """选中两张：两张都置 SHARED，响应按 output_index 返回两条。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)

    view = harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, asset_ids, title="我的两张头像"),
        idempotency_key="share-key-1",
    )

    assert [output.asset_id for output in view.outputs] == asset_ids
    assert [output.output_index for output in view.outputs] == [0, 1]
    assert view.title == "我的两张头像"
    assert harness.uow.media.get(asset_ids[0]).visibility == "SHARED"
    assert harness.uow.media.get(asset_ids[1]).visibility == "SHARED"
    # 主资产是 output_index 最小者（该列 NOT NULL，历史口径不变）
    stored = harness.uow.shares.get_by_slug(view.slug)
    assert stored is not None and stored.asset_id == asset_ids[0]
    assert len(harness.uow.shares.asset_links()) == 2


def test_only_selected_result_is_shared_and_other_stays_private(
    harness: Harness,
) -> None:
    """隐私边界：只分享一张时，另一张必须保持 PRIVATE，匿名读取 404。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    selected, private = asset_ids[0], asset_ids[1]

    view = harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, [selected]),
        idempotency_key="share-key-1",
    )

    assert [output.asset_id for output in view.outputs] == [selected]
    assert harness.uow.media.get(selected).visibility == "SHARED"
    assert harness.uow.media.get(private).visibility == "PRIVATE"


def test_purpose_must_be_result_and_status_ready(harness: Harness) -> None:
    """非 `RESULT` 或非 `READY` 的资产不可分享。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, _ = harness.seed_task(user_id=user.id, purpose="RESULT")
    other_generation, other_assets = harness.seed_task(
        user_id=user.id, purpose="SOURCE", asset_status="READY"
    )

    with pytest.raises(ApiError) as failure:
        harness.create_share(
            user_id=user.id,
            payload=_payload(other_generation, other_assets),
            idempotency_key="share-key-owned",
        )
    assert failure.value.code == ErrorCode.GENERATION_NOT_SHAREABLE

    pending_generation, pending_assets = harness.seed_task(
        user_id=user.id, asset_status="PENDING_UPLOAD"
    )
    with pytest.raises(ApiError) as pending_failure:
        harness.create_share(
            user_id=user.id,
            payload=_payload(pending_generation, pending_assets),
            idempotency_key="share-key-pending",
        )
    assert pending_failure.value.code == ErrorCode.GENERATION_NOT_SHAREABLE


def test_asset_from_another_generation_is_rejected(harness: Harness) -> None:
    """明确校验输出集合：不属于该任务的资产即使属于本人也不可分享。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    _, other_assets = harness.seed_task(user_id=user.id)

    with pytest.raises(ApiError) as failure:
        harness.create_share(
            user_id=user.id,
            payload=_payload(generation_id, [other_assets[0]]),
            idempotency_key="share-key-cross",
        )

    assert failure.value.code == ErrorCode.GENERATION_NOT_SHAREABLE
    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"


def test_foreign_or_missing_generation_is_404(harness: Harness) -> None:
    """他人任务与不存在的任务统一 404，不泄露存在性。"""

    owner = harness.uow.users.seed(make_user_row())
    stranger = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=owner.id)

    with pytest.raises(ApiError) as foreign:
        harness.create_share(
            user_id=stranger.id,
            payload=_payload(generation_id, asset_ids),
            idempotency_key="share-foreign",
        )
    assert foreign.value.code == ErrorCode.GENERATION_NOT_FOUND
    assert foreign.value.status_code == 404

    with pytest.raises(ApiError) as missing:
        harness.create_share(
            user_id=owner.id,
            payload=_payload(uuid.uuid4(), asset_ids),
            idempotency_key="share-missing",
        )
    assert missing.value.code == ErrorCode.GENERATION_NOT_FOUND


@pytest.mark.parametrize("status", ["CREATED", "QUEUED", "PROCESSING", "FAILED", "NEEDS_REVIEW"])
def test_only_succeeded_generation_can_be_shared(harness: Harness, status: str) -> None:
    """`FAILED`/`NEEDS_REVIEW`/未完成的任务都不是可分享终态。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id, status=status)

    with pytest.raises(ApiError) as failure:
        harness.create_share(
            user_id=user.id,
            payload=_payload(generation_id, asset_ids),
            idempotency_key=f"share-{status}",
        )

    assert failure.value.code == ErrorCode.GENERATION_NOT_SHAREABLE
    assert failure.value.status_code == 409


def test_asset_count_and_duplicates_are_validated(harness: Harness) -> None:
    """数量规则（1-2 张）与重复项属于请求错误，不是冲突。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id, outputs=3)

    for payload in (
        _payload(generation_id, []),
        _payload(generation_id, asset_ids),
        _payload(generation_id, [asset_ids[0], asset_ids[0]]),
    ):
        with pytest.raises(ApiError) as failure:
            harness.create_share(
                user_id=user.id, payload=payload, idempotency_key="share-bad"
            )
        assert failure.value.code == ErrorCode.VALIDATION_ERROR
        assert failure.value.status_code == 422


def test_title_length_is_enforced_by_the_schema() -> None:
    """`title` 上限来自冻结基线（`maxLength: 160`），落在 Schema 上。

    请求体校验由 Pydantic 完成（422 `VALIDATION_ERROR`），Service 侧的长度检查是
    防御性兜底；这里同时钉住「上限确实是 160」与「160 字可通过」。
    """

    schema = ShareCreateRequest.model_json_schema()["properties"]["title"]

    assert schema["maxLength"] == 160
    assert schema["anyOf"] == [{"type": "string"}, {"type": "null"}]

    generation_id, asset = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValidationError):
        _payload(generation_id, [asset], title="x" * 161)

    assert _payload(generation_id, [asset], title="x" * 160).title == "x" * 160


def test_overlong_title_is_rejected_at_the_api_boundary() -> None:
    """路由层手工解析后同样按 422 拒绝（ApiError，而不是 Pydantic 异常）。"""

    from app.api.shares.router import _share_payload  # noqa: PLC0415

    import asyncio

    class _Request:
        async def json(self):
            return {
                "generation_id": str(uuid.uuid4()),
                "output_asset_ids": [str(uuid.uuid4())],
                "title": "x" * 161,
            }

    with pytest.raises(ApiError) as failure:
        asyncio.run(_share_payload(_Request()))

    assert failure.value.code == ErrorCode.VALIDATION_ERROR
    assert "title" in failure.value.details["fields"]


# --------------------------------------------------------------------------- #
# slug
# --------------------------------------------------------------------------- #
def test_slug_is_opaque_and_unique(harness: Harness) -> None:
    """slug 不含内部 ID、用户 ID、存储键或 generation_id，且两次分享不同。"""

    user = harness.uow.users.seed(make_user_row())
    first_generation, first_assets = harness.seed_task(user_id=user.id)
    second_generation, second_assets = harness.seed_task(user_id=user.id)

    first = harness.create_share(
        user_id=user.id,
        payload=_payload(first_generation, first_assets),
        idempotency_key="share-1",
    )
    second = harness.create_share(
        user_id=user.id,
        payload=_payload(second_generation, second_assets),
        idempotency_key="share-2",
    )

    assert first.slug != second.slug
    assert len(first.slug) >= 32
    forbidden = [
        str(user.id),
        str(first_generation),
        str(first_assets[0]),
        "assets/",
        "storage_key",
    ]
    for token in forbidden:
        assert token not in first.slug


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #
def test_same_key_same_payload_replays(harness: Harness) -> None:
    """同键同载荷回放第一次分享，不新建分享、不重复写关联。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    payload = _payload(generation_id, asset_ids)

    first = harness.create_share(
        user_id=user.id, payload=payload, idempotency_key="share-key"
    )
    commits_after_first = harness.uow.commits
    second = harness.create_share(
        user_id=user.id, payload=payload, idempotency_key="share-key"
    )

    assert second.slug == first.slug
    assert len(harness.uow.shares.rows) == 1
    assert len(harness.uow.shares.asset_links()) == 2
    assert harness.uow.commits == commits_after_first  # 回放不产生新事务


def test_same_key_different_selection_conflicts(harness: Harness) -> None:
    """同键换了选中的图：稳定冲突，不派生成第二次分享。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)

    harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, [asset_ids[0]]),
        idempotency_key="share-key",
    )

    with pytest.raises(ApiError) as failure:
        harness.create_share(
            user_id=user.id,
            payload=_payload(generation_id, asset_ids),
            idempotency_key="share-key",
        )

    assert failure.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert failure.value.status_code == 409
    assert len(harness.uow.shares.rows) == 1


def test_same_key_across_generations_conflicts(harness: Harness) -> None:
    """同一个键用在另一个任务上：没有业务行可查，靠持久幂等登记判冲突。"""

    user = harness.uow.users.seed(make_user_row())
    first_generation, first_assets = harness.seed_task(user_id=user.id)
    second_generation, second_assets = harness.seed_task(user_id=user.id)

    harness.create_share(
        user_id=user.id,
        payload=_payload(first_generation, first_assets),
        idempotency_key="share-key",
    )

    with pytest.raises(ApiError) as failure:
        harness.create_share(
            user_id=user.id,
            payload=_payload(second_generation, second_assets),
            idempotency_key="share-key",
        )

    assert failure.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert failure.value.details["fields"] == ["generation_id"]


def test_idempotency_record_is_persisted_in_the_same_transaction(
    harness: Harness,
) -> None:
    """幂等依据必须持久化并与写入同事务（审计行与分享行一起提交）。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)

    harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, asset_ids),
        idempotency_key="share-key",
    )

    # 幂等登记的唯一真相是审计行（与 SQL 实现一致），因此直接读它
    entries = harness.uow.ledger.entries
    assert [entry.action for entry in entries] == ["SHARE_CREATE"]
    metadata = dict(entries[0].metadata_)
    assert metadata["generation_id"] == str(generation_id)
    assert metadata["entity_id"] == str(harness.uow.shares.rows[0].id)
    # 只存键摘要，不存原始键
    assert "share-key" not in str(metadata)


# --------------------------------------------------------------------------- #
# 匿名读取
# --------------------------------------------------------------------------- #
def test_anonymous_read_returns_only_selected_results(harness: Harness) -> None:
    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    created = harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, [asset_ids[0]], title="一半"),
        idempotency_key="share-key",
    )

    view = harness.service.read_share(slug=created.slug)
    data = as_share_data(view)

    assert [output["asset_id"] for output in data["outputs"]] == [str(asset_ids[0])]
    serialized = str(data)
    # 不泄漏内部事实
    assert str(generation_id) not in serialized
    assert str(user.id) not in serialized
    assert "assets/" not in serialized
    assert "storage" not in serialized


def test_unknown_and_revoked_and_expired_slug_are_404(harness: Harness) -> None:
    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    created = harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, asset_ids),
        idempotency_key="share-key",
    )

    with pytest.raises(ApiError) as unknown:
        harness.service.read_share(slug="not-a-real-slug")
    assert unknown.value.code == ErrorCode.GENERATION_NOT_FOUND

    # 过期：推进时钟越过 TTL
    harness.clock.advance(DEFAULT_SHARE_TTL_DAYS * 24 * 3600 + 1)
    with pytest.raises(ApiError) as expired:
        harness.service.read_share(slug=created.slug)
    assert expired.value.code == ErrorCode.GENERATION_NOT_FOUND

    # 撤销：内部行为（无公开撤销接口）
    share = harness.uow.shares.get_by_slug(created.slug)
    assert harness.revoke_share(user_id=user.id, share_id=share.id) is not None
    with pytest.raises(ApiError) as revoked:
        harness.service.read_share(slug=created.slug)
    assert revoked.value.code == ErrorCode.GENERATION_NOT_FOUND


def test_read_404_when_shared_asset_is_no_longer_usable(harness: Harness) -> None:
    """资产被逻辑删除或转为非 READY 后，公开链接立刻失效。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    created = harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, asset_ids),
        idempotency_key="share-key",
    )

    harness.uow.media.set_status(harness.uow.media.get(asset_ids[0]), "DELETED")
    harness.uow.media.set_status(harness.uow.media.get(asset_ids[1]), "DELETED")

    with pytest.raises(ApiError) as failure:
        harness.service.read_share(slug=created.slug)
    assert failure.value.code == ErrorCode.GENERATION_NOT_FOUND


# --------------------------------------------------------------------------- #
# 可见性恢复
# --------------------------------------------------------------------------- #
def test_revoke_restores_private_when_no_other_valid_share(harness: Harness) -> None:
    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    created = harness.create_share(
        user_id=user.id, payload=_payload(generation_id, asset_ids), idempotency_key="k1"
    )
    share = harness.uow.shares.get_by_slug(created.slug)

    harness.revoke_share(user_id=user.id, share_id=share.id)

    assert share.status == "REVOKED"
    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"
    assert harness.uow.media.get(asset_ids[1]).visibility == "PRIVATE"


def test_revoke_keeps_shared_while_another_valid_share_exists(harness: Harness) -> None:
    """还有别的有效分享时不得恢复 PRIVATE——否则会掐断那条链接的下载。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    first = harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, [asset_ids[0]]),
        idempotency_key="k1",
    )
    second = harness.create_share(
        user_id=user.id,
        payload=_payload(generation_id, [asset_ids[0]]),
        idempotency_key="k2",
    )

    harness.revoke_share(
        user_id=user.id, share_id=harness.uow.shares.get_by_slug(first.slug).id
    )

    assert harness.uow.media.get(asset_ids[0]).visibility == "SHARED"
    # 另一条仍然可读
    assert harness.service.read_share(slug=second.slug).outputs[0].asset_id == asset_ids[0]


def test_revoke_is_owner_scoped_and_idempotent(harness: Harness) -> None:
    owner = harness.uow.users.seed(make_user_row())
    stranger = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=owner.id)
    created = harness.create_share(
        user_id=owner.id, payload=_payload(generation_id, asset_ids), idempotency_key="k1"
    )
    share = harness.uow.shares.get_by_slug(created.slug)

    assert harness.revoke_share(user_id=stranger.id, share_id=share.id) is None
    assert share.status == "ACTIVE"

    harness.revoke_share(user_id=owner.id, share_id=share.id)
    assert harness.revoke_share(user_id=owner.id, share_id=share.id) is not None
    assert share.status == "REVOKED"
    # 撤销不删除历史
    assert len(harness.uow.shares.rows) == 1


def test_failed_creation_rolls_back_share_and_visibility(harness: Harness) -> None:
    """创建失败必须整体回滚：不留半个分享，也不留下被改过的可见性。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)

    original_create = harness.uow.ledger.record

    def exploding_record(**kwargs):
        original_create(**kwargs)
        raise RuntimeError("synthetic failure inside the transaction")

    harness.uow.ledger.record = exploding_record  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        with harness.uow:
            harness.service.create_share(
                user_id=user.id,
                payload=_payload(generation_id, asset_ids),
                idempotency_key="share-key",
            )

    assert harness.uow.shares.rows == ()
    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"
    assert harness.uow.media.get(asset_ids[1]).visibility == "PRIVATE"


def test_lifecycle_expires_due_share_and_restores_visibility(harness: Harness) -> None:
    """过期收敛：把 ACTIVE 且到期的分享置 EXPIRED，并恢复 PRIVATE。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)
    created = harness.create_share(
        user_id=user.id, payload=_payload(generation_id, asset_ids), idempotency_key="k1"
    )

    lifecycle = ShareLifecycle(uow=harness.uow, clock=harness.clock)
    harness.clock.advance(DEFAULT_SHARE_TTL_DAYS * 24 * 3600 + 1)

    assert lifecycle.expire_due_shares(limit=10) == 1
    harness.uow.commit()

    share = harness.uow.shares.get_by_slug(created.slug)
    assert share.status == "EXPIRED"
    assert harness.uow.media.get(asset_ids[0]).visibility == "PRIVATE"


def test_share_ttl_is_finite_and_recorded(harness: Harness) -> None:
    """分享必须有明确期限（未决项取 7 天），且写入 `expires_at`。"""

    user = harness.uow.users.seed(make_user_row())
    generation_id, asset_ids = harness.seed_task(user_id=user.id)

    view = harness.create_share(
        user_id=user.id, payload=_payload(generation_id, asset_ids), idempotency_key="k1"
    )

    assert view.expires_at is not None
    expected = harness.clock() + timedelta(days=DEFAULT_SHARE_TTL_DAYS)
    assert abs((view.expires_at - expected).total_seconds()) < 1
