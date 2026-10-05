"""API 层（ENG-02 第 1 步）。

- `/api/v1` 路由根：`app.api.router`；
- 中间件、统一错误 envelope、异常映射与健康检查属本卡第 2、3 步；
- 业务路由（用户、邀请码、点数、生成任务、Provider、Storage）属后续卡片，
  不得在本包内实现业务规则。
"""
