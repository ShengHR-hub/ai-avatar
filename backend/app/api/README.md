# backend/app/api/

路由与请求/响应 Schema。统一前缀 `/api/v1`。

职责：路由和 HTTP 状态码、请求 Schema 校验、获取当前用户、调用 Service、返回响应 Schema。

禁止：直接写 SQL；直接修改钱包余额；编排生成或退款事务；直接调用 Provider SDK；在 `router.py` 中实现业务规则。

ENG-00 不在此目录创建任何路由。实现属于 `ENG-02` 及后续业务卡。
