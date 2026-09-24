# AI Avatar Studio V1.0 开发规范与代码约束

> 状态：已冻结
> 日期：2026-09-24

## 1. 通用规则

1. 开发前必须阅读对应架构文档、API 契约和 Task Card。
2. 不得自行改变框架、数据库、队列、状态机或点数规则。
3. 不得在当前任务中顺带增加未要求的产品功能。
4. 所有外部输入必须校验，所有外部调用必须有超时。
5. 所有涉及点数、订单和任务结算的修改必须有事务和幂等测试。
6. 代码变更必须同步更新受影响的 OpenAPI、迁移和架构文档。
7. 密钥、令牌、真实用户图片和敏感日志不得提交到仓库。

## 2. 后端分层

后端调用方向固定为：

```text
API Router
    ↓
Service
    ↓
Repository
    ↓
PostgreSQL
```

外部能力调用方向：

```text
Service
├── AIProvider Interface
└── StorageAdapter Interface
```

### API Router

负责：

- 路由和 HTTP 状态码；
- 请求 Schema 校验；
- 获取当前用户；
- 调用 Service；
- 返回响应 Schema。

禁止：

- 直接写 SQL；
- 直接修改钱包余额；
- 编排生成或退款事务；
- 直接调用 Provider SDK；
- 在 `router.py` 中实现业务规则。

### Service

负责：

- 业务规则；
- 权限和状态检查；
- 事务边界；
- 点数预留、消费和释放；
- Repository、Provider 和 Storage 的编排。

所有点数结算必须由专门的 Points Service 完成，其他模块不得直接修改 `point_wallets`。

### Repository

负责：

- SQLAlchemy 查询；
- 数据持久化；
- 行锁和数据库级查询优化。

禁止：

- HTTP 逻辑；
- Provider 调用；
- 跨领域业务决策；
- 隐式提交事务。

事务由 Service 控制，Repository 不自行 `commit`。

### Celery Task

Celery Task 只负责：

- 解析任务参数；
- 建立执行上下文；
- 调用 Service；
- 根据错误分类决定重试。

禁止在 Celery Task 函数中复制业务逻辑或直接修改账务表。

### Provider 与 Storage Adapter

- 每个具体实现必须满足统一接口；
- SDK 响应必须转换成内部类型；
- Provider 错误必须转换成统一错误分类；
- 供应商名称、模型和端点必须配置化；
- 业务模块不得导入具体供应商 SDK。

## 3. 前端分层

前端目录职责：

```text
app/          路由、布局、页面组合
components/   可复用 UI 和领域组件
hooks/        客户端状态与交互封装
services/     API 调用和数据转换
types/        前端内部类型
generated/    OpenAPI 自动生成代码
public/       静态资源
```

### 页面与组件

页面负责组合界面和加载所需数据，不直接拼接 API URL，不直接解释后端错误码。

禁止：

- 在页面组件中散落 `fetch`；
- 手写与 OpenAPI 重复的后端响应类型；
- 在浏览器保存 AI Provider 密钥；
- 在多个页面复制任务轮询逻辑；
- 使用前端状态代替后端任务和点数事实。

### Services

所有 API 请求通过 `services/` 中的封装调用生成客户端。Service 负责：

- 传递幂等键；
- 映射错误码；
- 统一认证失效处理；
- 将后端 DTO 转换为页面需要的模型。

### 状态来源

- 服务端数据以 FastAPI 响应为准；
- 任务状态由轮询获取，刷新页面后必须恢复；
- 点数余额不能只在前端乐观修改；
- 上传完成前不得把资源视为可用于生成。

## 4. API 与类型规范

- API 统一放在 `/api/v1`；
- 请求和响应使用 Pydantic Schema；
- FastAPI 自动生成 OpenAPI；
- `shared/api-contract/openapi.json` 为生成产物；
- 前端类型和客户端由 OpenAPI 生成；
- 错误码使用稳定字符串，不把异常文本作为业务判断条件；
- 所有异步创建接口返回 `202 Accepted`；
- 所有幂等写接口要求 `Idempotency-Key`。

## 5. 数据库规范

- 主键统一 UUID；
- 时间统一 `timestamptz` 和 UTC；
- 金额使用最小货币单位整数；
- 估算成本使用定点 `numeric`，禁止浮点数；
- 点数使用整数；
- Schema 变更必须通过 Alembic；
- 已应用的迁移不得修改，只能新增迁移；
- 点数流水和审计日志只追加，不覆盖历史；
- 删除用户可见数据优先使用逻辑删除，二进制清理由生命周期任务处理。

## 6. 错误与日志规范

业务错误必须转换为统一错误码。日志应记录上下文标识，不记录敏感内容。

禁止记录：

- API Key；
- Session Cookie；
- 邮箱验证令牌；
- 原图二进制或 Base64；
- 完整 Provider 原始响应；
- 未脱敏的个人信息。

异常必须保留内部诊断信息，但用户响应只返回稳定错误码和安全说明。

## 7. 测试要求

必须覆盖：

- Service 业务规则单元测试；
- Repository 与 PostgreSQL 集成测试；
- API 契约测试；
- MockProvider 成功、失败、超时和人工复核测试；
- 点数预留、消费、释放和重复调用测试；
- Worker 重试与恢复测试；
- 上传权限和分享权限测试；
- 本地完整闭环 E2E 测试。

涉及点数的测试必须验证流水、钱包快照和任务计费状态三者一致。

## 8. Code Review 红线

出现以下情况不得合并：

- 路由或 Celery Task 直接修改点数；
- 直接修改 `available_points` 而不写流水；
- 业务代码直接依赖具体 Provider 或对象存储 SDK；
- 写接口没有幂等保护；
- 状态转换绕过 Service；
- API Schema 与 OpenAPI 不一致；
- 数据库变更没有迁移；
- 服务端密钥进入前端；
- 测试使用真实用户图片或真实生产凭据。

## 9. 完成标准

每张 Task Card 至少满足：

1. 验收标准全部实现；
2. 必要测试通过；
3. 类型检查和构建通过；
4. 数据库迁移可前向执行；
5. OpenAPI 和前端生成类型已同步；
6. 没有新增敏感信息或未说明的架构变更；
7. 文档与最终行为一致。
