"""Points Service 包（ENG-04）。

- `service`：钱包账务写入（credit / reserve / consume / release / adjust）与余额查询；
- `redeem_invite` 的跨卡适配在 `app.services.ports`（`PointsPort` 真实实现）。

本包不实现 Generation、Provider、Storage 或任务状态机（ENG-05/ENG-06）。
"""
