# LEAD-03：REST API、OpenAPI 与错误码契约

## 会话目的

冻结 `/api/v1` REST API 的资源、请求响应、HTTP 状态码、稳定错误码、认证和幂等要求，让前后端可以并行开发。

## 必须覆盖

- 用户和邀请码；
- 点数余额和流水只读接口；
- 媒体上传、完成和访问；
- 生成任务创建、查询、历史和重试；
- 分享、下载、反馈；
- 健康检查和管理接口边界。

## 固定规则

- 所有接口的 canonical 前缀为 `/api/v1`；认证接口使用 `/api/v1/auth/*`，用户接口使用 `/api/v1/me`；
- 邀请码兑换的 canonical 路径为 `POST /api/v1/auth/invites/redeem`；
- 异步生成创建返回 `202 Accepted`；
- 所有写接口要求 `Idempotency-Key`；
- 错误使用稳定字符串，不用异常文本驱动前端；
- API Router 只做校验、鉴权、Service 调用和响应转换；
- OpenAPI 是前端类型生成的唯一来源。

## 输出

- `docs/architecture/03-api-contract.md`：唯一人读契约，包含路由表、Schema、HTTP 状态码、错误码、权限和幂等规则；
- `shared/api-contract/openapi.json`：OpenAPI 3.1 基线产物，供 `ENG-02` 从 FastAPI/Pydantic 导出并校验；
- 稳定错误码表、认证/权限矩阵、重复请求和请求体冲突语义；
- `ENG-02`、`ENG-03`、`ENG-08` 可直接执行的工程说明；
- 尚未确定问题清单和明确不做范围。

## 不负责

不实现页面、不直接写数据库查询、不在契约中加入尚未批准的功能。

## 审核门禁

每个写接口都有输入约束、成功响应、错误响应、幂等行为和权限说明，才可授权工程实现。

## 审核门禁

技术负责人审核必须逐项确认：

1. 所有 canonical 路径均以 `/api/v1` 开头，邀请码兑换固定为 `POST /api/v1/auth/invites/redeem`，用户接口固定为 `GET/PATCH /api/v1/me`；
2. 所有写接口均声明 `Idempotency-Key`，生成创建和重试均为 `202 Accepted`；
3. 统一成功、分页和错误 envelope 与 OpenAPI JSON 一致；
4. 错误码到 HTTP 状态映射稳定，权限矩阵不允许越权读取；
5. OpenAPI 只作为基线，ENG-02 必须从 FastAPI/Pydantic 生成并通过规范化 diff；
6. 未增加数据库字段、点数规则、任务状态机或未批准的业务 API。

## 下一步与停点

本卡完成后只等待技术负责人审核，不授权继续实现。审核通过后按顺序授权：

1. `ENG-02`：FastAPI 基础服务、配置、健康检查、公共 Schema、异常映射、幂等 Header 校验和 OpenAPI 导出；
2. `ENG-03`：用户、邀请码、邮箱绑定和 Redis Session，必须严格使用本卡 canonical 路径和错误码；
3. `ENG-08`：从 `shared/api-contract/openapi.json` 生成前端客户端，接入统一认证、幂等、错误码映射和异步任务轮询。

未收到明确的“LEAD-03 通过并授权”消息前，不得执行上述工程任务、修改 Router、页面、数据库或提交 Git。
