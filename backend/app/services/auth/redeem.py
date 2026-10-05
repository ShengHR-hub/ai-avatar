"""邀请码兑换 Service（ENG-03 第 2 步）。

架构约束（技术负责人 2026-09-26 裁决 ②）：

- 邀请兑换的**完整原子操作**由注入式 `PointsPort.redeem_invite()` 完成：
  用户创建/恢复、邀请码兑换记录、`CREDIT` 流水、Session 创建；
- ENG-03 **不得直接修改 wallet 或 `point_ledger`**，也不得复制 Points Service 逻辑；
- Points 端口未接入时抛出 `PointsPortUnavailable` → 本 Service 映射为
  `NOT_READY`（503），不新增错误码。

事务边界：由 Points 端口实现内部负责（ENG-04）；本 Service 不开启数据库事务。
"""

from __future__ import annotations

from app.errors.codes import ErrorCode
from app.errors.exceptions import ApiError
from app.services.auth.session_store import SESSION_TTL_SECONDS
from app.services.ports import PointsPort, PointsPortUnavailable, RedeemOutcome

__all__ = ["RedeemService"]


class RedeemService:
    """邀请码兑换编排（委托给 Points 端口）。"""

    def __init__(
        self,
        *,
        points_port: PointsPort,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self._points_port = points_port
        self._session_ttl_seconds = session_ttl_seconds

    @property
    def session_ttl_seconds(self) -> int:
        return self._session_ttl_seconds

    def redeem(self, *, invite_code: str, email: str) -> RedeemOutcome:
        """执行兑换；Points 端口未接入时抛 `NOT_READY`。

        邀请码明文只在此处作为参数传递，不落库、不进日志。
        """

        try:
            return self._points_port.redeem_invite(
                invite_code=invite_code,
                email=email,
                session_ttl_seconds=self._session_ttl_seconds,
            )
        except PointsPortUnavailable as exc:
            raise ApiError(ErrorCode.NOT_READY) from exc
