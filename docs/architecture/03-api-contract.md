# AI Avatar Studio V1.0 REST API 契约

> 前缀：`/api/v1`  
> 认证：HttpOnly Session Cookie  
> 异步创建：`202 Accepted`  
> 状态：已冻结

## 1. 契约来源

FastAPI Pydantic Schema 是服务端契约源，生成 `shared/api-contract/openapi.json`。前端从 OpenAPI 生成 TypeScript 类型和客户端，不手写重复的后端响应类型。

## 2. 统一响应

成功：

```json
{
  "data": {},
  "request_id": "req_xxx"
}
```

失败：

```json
{
  "error": {
    "code": "INSUFFICIENT_POINTS",
    "message": "点数不足",
    "details": {}
  },
  "request_id": "req_xxx"
}
```

## 3. 认证接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/auth/invites/redeem` | 兑换邀请码并创建/恢复用户 |
| `POST` | `/auth/email/request` | 请求邮箱验证链接 |
| `POST` | `/auth/email/verify` | 验证邮箱 |
| `POST` | `/auth/logout` | 退出登录 |
| `GET` | `/auth/me` | 获取当前身份 |

邀请码兑换后必须绑定邮箱，邮箱验证令牌仅存 Redis 并设置过期时间。

## 4. 用户与点数

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/me` | 用户资料 |
| `PATCH` | `/me` | 修改 `display_name`、`avatar_asset_id` |
| `GET` | `/me/points` | 当前余额和预留点数 |
| `GET` | `/me/points/ledger` | 点数流水，游标分页 |
| `GET` | `/me/generations` | 历史任务，游标分页 |

点数响应至少包括：

```json
{
  "available_points": 20,
  "reserved_points": 10,
  "lifetime_credited": 100,
  "lifetime_consumed": 70
}
```

## 5. 风格与 Prompt

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/styles` | 已发布风格 |
| `GET` | `/styles/{slug}` | 风格详情 |
| `GET` | `/prompt-templates` | 已发布模板 |
| `GET` | `/prompt-templates/{id}` | 模板详情 |

公开接口不得返回未发布内容、Provider 凭据或内部成本配置。

## 6. 图片上传

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/assets/uploads` | 创建预签名上传 |
| `POST` | `/assets/{asset_id}/complete` | 校验并完成上传 |
| `POST` | `/assets/{asset_id}/download-url` | 创建短期下载链接 |

上传请求包括：

```json
{
  "purpose": "SOURCE",
  "filename": "avatar.jpg",
  "mime_type": "image/jpeg",
  "size_bytes": 2457600,
  "sha256": "..."
}
```

完成上传时必须验证对象存在、大小、MIME、哈希和图片可解码性。

## 7. 生成任务

### 创建任务

```http
POST /api/v1/generations
Idempotency-Key: user-request-uuid
```

请求：

```json
{
  "mode": "PHOTO_PROMPT",
  "style_id": "uuid",
  "prompt_template_id": "uuid",
  "prompt": "保持人物特征，生成专业头像",
  "input_asset_ids": ["source-asset-uuid"],
  "output_count": 2
}
```

第一阶段约束：

```text
PHOTO_PROMPT       1 张 SOURCE
TEXT_PROMPT        无图片
REFERENCE_IMAGE    1 张 SOURCE + 最多 2 张 REFERENCE
output_count       固定 2
```

响应 `202`：

```json
{
  "data": {
    "generation_id": "uuid",
    "status": "QUEUED",
    "billing_status": "RESERVED",
    "reserved_points": 10,
    "requested_count": 2
  },
  "request_id": "req_xxx"
}
```

### 查询任务

```text
GET /generations/{generation_id}
GET /generations/{generation_id}/outputs
```

任务状态：

```text
CREATED
QUEUED
PROCESSING
SUCCEEDED
FAILED
CANCELLED
NEEDS_REVIEW
```

计费状态：

```text
RESERVED
CONSUMED
RELEASED
```

### 重试与取消

```text
POST /generations/{generation_id}/retry
POST /generations/{generation_id}/cancel
```

重试只能作用于 `FAILED` 任务，创建新任务并设置 `parent_task_id`，不修改旧任务。取消只保证 `CREATED` 和 `QUEUED` 阶段；Provider 是否支持处理中断由 Worker 处理。

## 8. 分享与反馈

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/shares` | 创建分享链接 |
| `GET` | `/shares/{slug}` | 公开读取分享内容 |
| `POST` | `/feedback` | 提交任务反馈 |

分享只能访问明确设置为 `SHARED` 的资源。用户只能为自己拥有的任务提交反馈。

## 9. Provider Webhook 与健康检查

```text
POST /internal/providers/{provider_name}/webhook
GET  /health/live
GET  /health/ready
```

Webhook 必须验签、防重放、支持重复回调，并通过统一状态归并服务处理，不直接修改点数。

`ready` 至少检查 PostgreSQL、Redis、对象存储和 Worker 心跳。

## 10. 管理接口

最小管理接口：

```text
GET  /admin/generations
GET  /admin/generations/{id}
POST /admin/generations/{id}/review-resolution
GET  /admin/point-ledger
GET  /admin/provider-attempts
```

人工复核处理：

```json
{
  "decision": "CONSUME",
  "note": "Provider 控制台确认已生成并已补回结果"
}
```

允许的决策为 `CONSUME` 或 `RELEASE`，必须记录管理员和审计日志。

## 11. 幂等规则

以下写接口必须支持 `Idempotency-Key`：

```text
POST /auth/invites/redeem
POST /assets/uploads
POST /assets/{id}/complete
POST /generations
POST /generations/{id}/retry
POST /shares
```

相同用户、相同 Key、相同请求体返回第一次结果；相同 Key 但请求体不同返回 `IDEMPOTENCY_CONFLICT`。

## 12. 错误码

```text
AUTH_REQUIRED
EMAIL_NOT_VERIFIED
INVITE_INVALID
INVITE_ALREADY_USED
INSUFFICIENT_POINTS
ASSET_NOT_FOUND
ASSET_INVALID
ASSET_FORBIDDEN
GENERATION_NOT_FOUND
GENERATION_NOT_RETRYABLE
GENERATION_NOT_CANCELLABLE
IDEMPOTENCY_CONFLICT
PROVIDER_UNAVAILABLE
GENERATION_FAILED
FEATURE_NOT_AVAILABLE
RATE_LIMITED
UPLOAD_TOO_LARGE
INTERNAL_ERROR
```

HTTP 映射：`401` 身份、`403` 权限、`404` 不存在、`409` 状态或幂等冲突、`413` 文件过大、`422` 输入错误、`429` 限流、`502/503` 外部服务错误、`500` 未分类内部错误。

## 13. API 验收标准

1. OpenAPI 与实际响应一致。
2. 所有异步创建接口返回任务 ID 和 `202`。
3. 前端不直接访问数据库、Provider 或对象存储私密接口。
4. 所有点数相关错误有稳定错误码。
5. 生成、重试、上传完成和分享创建具备幂等行为。
6. 用户无法读取其他用户的私有资源。
7. `NEEDS_REVIEW` 只能由管理接口明确结算。
