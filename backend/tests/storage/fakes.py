"""ENG-05 测试替身（放在 `tests/storage` 内，避免改动已验收的 `tests/services/fakes.py`）。

- `InMemoryMediaAssetRepository`：`MediaAssetRepository` 端口的内存实现，
  记录 `lock_calls` 以便断言「完成/删除入口确实走行锁」；
- `AssetUnitOfWork`：复用 ENG-03/04 的内存工作单元，只把 `media` 换成上面的完整实现。
  `tests/services/fakes.py` 的 `InMemoryMediaAssetRepository` 只有 `find_owner`
  （ENG-03 头像归属用），本卡不在允许修改清单内，因此在这里补齐，不改动旧替身。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.db.models.media import MediaAsset
from app.repositories.media import MediaAssetDraft
from tests.services.fakes import InMemoryUnitOfWork

__all__ = ["AssetUnitOfWork", "InMemoryMediaAssetRepository", "make_media_asset"]


def make_media_asset(
    *,
    user_id: uuid.UUID,
    purpose: str = "SOURCE",
    asset_type: str = "ORIGINAL",
    status: str = "PENDING_UPLOAD",
    visibility: str = "PRIVATE",
    storage_provider: str = "MINIO",
    storage_key: str | None = None,
    mime_type: str = "image/png",
    size_bytes: int = 64,
    width: int | None = None,
    height: int | None = None,
    sha256: str = "a" * 64,
    idempotency_key: str | None = "synthetic-key",
    expires_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> MediaAsset:
    """构造一行媒体资源（不落库），默认值均为合成数据。"""

    moment = datetime.now(timezone.utc)
    asset = MediaAsset(
        user_id=user_id,
        asset_type=asset_type,
        purpose=purpose,
        status=status,
        visibility=visibility,
        storage_provider=storage_provider,
        storage_key=storage_key or f"assets/{user_id}/{uuid.uuid4().hex}",
        mime_type=mime_type,
        size_bytes=size_bytes,
        width=width,
        height=height,
        sha256=sha256,
        idempotency_key=idempotency_key,
        expires_at=expires_at or (moment + timedelta(days=7)),
        deleted_at=deleted_at,
        created_at=moment,
    )
    asset.id = uuid.uuid4()
    return asset


class InMemoryMediaAssetRepository:
    """`MediaAssetRepository` 的内存实现（只追加/只改状态，不 commit）。"""

    def __init__(self, clock=None) -> None:
        self._rows: dict[uuid.UUID, MediaAsset] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock_calls: list[uuid.UUID] = []

    # ------------------------------------------------------------------ 只读
    def register(self, asset: MediaAsset) -> MediaAsset:
        """把已有行登记进替身（测试预置用）。"""

        self._rows[asset.id] = asset
        return asset

    def find_owner(self, asset_id: uuid.UUID) -> uuid.UUID | None:
        asset = self._rows.get(asset_id)
        return asset.user_id if asset is not None else None

    def get(self, asset_id: uuid.UUID) -> MediaAsset | None:
        return self._rows.get(asset_id)

    def lock_by_id(self, asset_id: uuid.UUID) -> MediaAsset | None:
        self.lock_calls.append(asset_id)
        return self._rows.get(asset_id)

    def find_by_idempotency_key(
        self, *, user_id: uuid.UUID, idempotency_key: str
    ) -> MediaAsset | None:
        return next(
            (
                row
                for row in self._rows.values()
                if row.user_id == user_id and row.idempotency_key == idempotency_key
            ),
            None,
        )

    # ------------------------------------------------------------------ 写入
    def create(self, draft: MediaAssetDraft) -> MediaAsset:
        asset = make_media_asset(
            user_id=draft.user_id,
            purpose=draft.purpose,
            asset_type=draft.asset_type,
            status="PENDING_UPLOAD",
            storage_provider=draft.storage_provider,
            storage_key=draft.storage_key,
            mime_type=draft.mime_type,
            size_bytes=draft.size_bytes,
            sha256=draft.sha256,
            idempotency_key=draft.idempotency_key,
            expires_at=draft.expires_at,
        )
        self._rows[asset.id] = asset
        return asset

    def mark_ready(
        self,
        asset: MediaAsset,
        *,
        mime_type: str,
        size_bytes: int,
        width: int,
        height: int,
    ) -> MediaAsset:
        asset.status = "READY"
        asset.mime_type = mime_type
        asset.size_bytes = size_bytes
        asset.width = width
        asset.height = height
        return asset

    def mark_deleted(self, asset: MediaAsset, *, deleted_at: datetime) -> MediaAsset:
        asset.status = "DELETED"
        asset.deleted_at = deleted_at
        return asset

    def set_status(self, asset: MediaAsset, status: str) -> MediaAsset:
        asset.status = status
        return asset

    @property
    def rows(self) -> tuple[MediaAsset, ...]:
        return tuple(self._rows.values())


class AssetUnitOfWork(InMemoryUnitOfWork):
    """带完整媒体仓储的内存工作单元（供 Asset Service / 端点测试使用）。"""

    def __init__(self, clock=None) -> None:
        super().__init__(clock)
        self.media = InMemoryMediaAssetRepository(clock)
