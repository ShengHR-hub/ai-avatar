"""Service 依赖的出站端口（ENG-06 第 3 步）。

只有协议，不含实现：

- `GenerationQueuePort`：把「投递」这件事从 Service 里隔开，测试可用内存替身断言
  「事务提交之后才投递」（`LEAD-04` §6:119），生产实现是 `app.tasks.queue`；
- `UowFactory`：Worker 与 Webhook 需要**每个步骤一个事务**，因此按工厂注入而不是
  直接传入一个 `UnitOfWork`；
- `ReplayGuard`：Webhook 的短期事件去重（权威判定仍是数据库的 attempt 行锁与终态检查，
  `04-ai-provider.md` 第 4 节第 4 条）；
- `WebhookAuthenticator`：把「验签 + 解析 + 时间窗」留在适配器侧，Service 只接收已
  归一的 `WebhookEvent`。

本模块只提供本卡调用方需要的子集，不给后续卡片预留空壳。
"""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.providers.ports import AIProvider, ProviderResult
from app.repositories.unit_of_work import UnitOfWork

__all__ = [
    "CLAIM_ACQUIRED",
    "CLAIM_IN_FLIGHT",
    "CLAIM_RECONCILED",
    "GenerationQueuePort",
    "ProviderFactory",
    "ReplayGuard",
    "UowFactory",
    "WebhookAuthenticator",
    "WebhookEvent",
]


@runtime_checkable
class GenerationQueuePort(Protocol):
    """生成任务与状态轮询的投递端口。"""

    def enqueue_generation(self, generation_id: uuid.UUID, *, countdown_seconds: int = 0) -> bool:
        """投递一次生成执行；返回 `False` 表示投递丢失，由恢复扫描补投。"""
        ...

    def enqueue_status_poll(
        self, generation_id: uuid.UUID, *, attempt_no: int, countdown_seconds: int
    ) -> bool:
        """投递一次状态轮询（与 Webhook 归一到同一条对账路径）。"""
        ...


@runtime_checkable
class UowFactory(Protocol):
    """工作单元工厂：`with factory() as uow` 每次给出一个独立事务。"""

    def __call__(self) -> AbstractContextManager[UnitOfWork]: ...


@runtime_checkable
class ProviderFactory(Protocol):
    """Provider 适配器工厂（V1.0 固定单一 Provider，不做路由）。"""

    def __call__(self) -> AIProvider: ...


#: `ReplayGuard.claim()` 的三种结论（字符串常量，便于跨进程日志与测试断言对齐）
CLAIM_ACQUIRED = "ACQUIRED"  #: 本次占用成功：可以继续对账
CLAIM_IN_FLIGHT = "IN_FLIGHT"  #: 另一次同事件 ID 仍在处理中：既非重复也非可重投
CLAIM_RECONCILED = "RECONCILED"  #: 之前已**持久对账成功**：重复事件才可按 no-op 成功

@runtime_checkable
class ReplayGuard(Protocol):
    """短期事件去重：以**一次原子占用**区分「首次 / 处理中 / 已对账」。

    只有三态而不是布尔，是因为「重复」有两种后果完全不同的成因：先前那次已经把所有
    写入提交进数据库（可以回 202，供应商不必再投），还是先前那次仍在跑或崩在半路
    （此时回 202 会让供应商不再重投，一次竞态就变成永久丢失的结果）。

    权威判定仍是数据库的 attempt 行锁与终态检查（`04-ai-provider.md` 第 4 节第 4 条），
    本端口只是快速路径；`release` 让「对账失败」的事件重新可投。
    """

    def claim(self, key: str, *, ttl_seconds: int) -> str:
        """占用事件 ID，返回 `CLAIM_*` 之一。"""
        ...

    def mark_reconciled(self, key: str, *, ttl_seconds: int) -> None:
        """把**自己占到的**那次占用标记为已持久对账；不是自己的占用则无操作。"""
        ...

    def release(self, key: str) -> None:
        """释放**自己占到的**那次占用（对账失败时允许供应商安全重投）。"""
        ...


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """已验签并归一的回调事件。

    只承载关联所需的三项事实：供应商名、外部请求 ID 与归一结果。
    回调载荷里携带的任何用户/任务标识都不作为授权依据
    （`04-ai-provider.md` 第 4 节第 3 条），因此这里不承载它们；
    事件 ID 只服务于防重放，由 `WebhookIngress` 持有，不进入对账。
    """

    provider_name: str
    provider_request_id: str
    result: ProviderResult


@runtime_checkable
class WebhookAuthenticator(Protocol):
    """回调验签与解析端口：任何一步失败都必须抛 `WEBHOOK_INVALID`（401）。

    `event_id_header` / `timestamp_header` 是路由从请求上取到的**未信任**值：
    实现方必须先对原始字节验签（恒定时间比较），之后才可以解析载荷、
    才可以使用这两个值判定时间窗与事件身份
    （`04-ai-provider.md` 第 4 节第 1、2 条）。
    """

    def authenticate(
        self,
        *,
        provider_name: str,
        raw_body: bytes,
        signature: str,
        event_id_header: str,
        timestamp_header: str,
    ) -> WebhookEvent:
        """验签（对原始字节）后才解析，并校验时间窗与事件 ID。"""
        ...
