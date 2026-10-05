"""Provider 结果的持久化（ENG-06 第 3 步）。

`LEAD-04` §6:123 要求「结果写入 + 任务终态 + 结算 + 钱包快照」在**同一个事务**里，
因此本模块只负责把已经归一的 `ProviderOutput` 变成
`media_assets` + `generation_outputs` 两份记录，不做任何状态判定。

口径：

- 图片走与客户端上传**完全相同**的校验路径（`app.services.assets.validation`）：
  签名嗅探、完整解码、MIME/大小/哈希/尺寸校验，Provider 不得享有第二条绿色通道；
- 对象 Key 由服务端生成（`assets/{user_id}/{hex}`），绝不使用 Provider 返回的路径或 URL；
  图片 URL 不跨越本边界（`04-ai-provider.md` 第 2 节）；
- 至少一次投递安全：每个结果图带确定性操作键
  `generation-output:{generation_id}:{index}`，重投时先查后写，不重复上传也不重复建行；
- 校验失败按既有 `ApiError` 抛出，由调用方转 `NEEDS_REVIEW`；
  存储不可用**不在这里兜**，直接向上抛，让事务整体回滚、Worker 安全重投
  （`LEAD-04` §6:126）——绝不把基础设施故障伪装成人工复核。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

from app.db.models.generation import GenerationTask
from app.repositories.generation import GenerationOutputDraft
from app.repositories.media import MediaAssetDraft
from app.repositories.unit_of_work import UnitOfWork
from app.services.assets.policy import PURPOSE_TO_ASSET_TYPE, RESULT_TTL
from app.services.assets.validation import ValidatedImage, validate_image
from app.providers.ports import ProviderOutput
from app.storage.ports import StorageAdapter

__all__ = ["output_idempotency_key", "persist_provider_outputs"]

_RESULT_PURPOSE = "RESULT"


def output_idempotency_key(generation_id: uuid.UUID, output_index: int) -> str:
    """结果图的确定性操作键（重投安全，且长度受 `varchar(128)` 约束）。"""

    return f"generation-output:{generation_id}:{output_index}"


def _new_storage_key(*, user_id: uuid.UUID) -> str:
    return f"assets/{user_id}/{uuid.uuid4().hex}"


def persist_provider_outputs(
    uow: UnitOfWork,
    *,
    storage: StorageAdapter,
    storage_provider: str,
    task: GenerationTask,
    outputs: Sequence[ProviderOutput],
    now: Callable[[], datetime] | datetime,
) -> int:
    """落库 Provider 结果图，返回本次新写入的条数。

    `outputs` 按位置对应 `generation_outputs.output_index`：重投时同一位置只会落地
    一条结果，因此顺序本身就是幂等的一部分。

    两遍走完：**先把所有待落地图全部校验通过，才开始上传**。校验/解码是纯内存操作，
    如果边校验边上传，第二张不合格就会把第一张的孤儿对象留在桶里——数据库事务回滚后
    没有任何一行记录指向那个 key，基于 `media_assets.expires_at` 的生命周期清理永远
    找不到它。上传一旦开始就带补偿：本调用内已上传的对象在失败时逐个删除后再向上抛，
    让调用方按既有语义回滚事务、转人工复核或安全重投。
    """

    moment = now() if callable(now) else now
    existing_indexes = {row.output_index for row in uow.generation_tasks.list_outputs(task.id)}

    verified: list[tuple[int, ProviderOutput, bytes, str, ValidatedImage]] = []
    for index, output in enumerate(outputs):
        if index in existing_indexes:
            continue

        content = bytes(output.content)
        digest = hashlib.sha256(content).hexdigest()
        # 不编造文件名扩展名：结果图没有客户端文件名，提示项为空才不会被凭空
        # 写死的 `.png` 反过来自相矛盾（JPEG/WebP 输出会因此被 `extension_disagreement` 拒掉）。
        validated = validate_image(
            content,
            declared_mime_type=output.mime_type,
            declared_size_bytes=len(content),
            declared_sha256=digest,
        )
        verified.append((index, output, content, digest, validated))

    written = 0
    uploaded: list[str] = []
    try:
        for index, output, content, digest, validated in verified:
            key = _new_storage_key(user_id=task.user_id)
            storage.upload(key, content, validated.mime_type)
            uploaded.append(key)

            idempotency_key = output_idempotency_key(task.id, index)
            asset = uow.media.find_by_idempotency_key(
                user_id=task.user_id, idempotency_key=idempotency_key
            )
            if asset is None:
                asset = uow.media.create(
                    MediaAssetDraft(
                        user_id=task.user_id,
                        asset_type=PURPOSE_TO_ASSET_TYPE[_RESULT_PURPOSE],
                        purpose=_RESULT_PURPOSE,
                        storage_provider=storage_provider,
                        storage_key=key,
                        mime_type=validated.mime_type,
                        size_bytes=len(content),
                        sha256=digest,
                        idempotency_key=idempotency_key,
                        expires_at=moment + RESULT_TTL,
                    )
                )
                uow.media.mark_ready(
                    asset,
                    mime_type=validated.mime_type,
                    size_bytes=len(content),
                    width=validated.width,
                    height=validated.height,
                )

            uow.generation_tasks.create_output(
                GenerationOutputDraft(
                    generation_id=task.id,
                    asset_id=asset.id,
                    output_index=index,
                    provider_output_id=output.provider_output_id,
                )
            )
            written += 1
    except BaseException:
        # 补偿删除是尽力而为：删不掉也不能用它盖掉真正的失败原因
        for orphan in uploaded:
            try:
                storage.delete(orphan)
            except Exception:  # noqa: BLE001 - 原始异常才是本次结论
                pass
        raise

    return written
