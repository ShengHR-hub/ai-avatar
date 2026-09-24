# AI Avatar Studio V1.0 数据库设计

> 数据库：PostgreSQL
> 主键：UUID
> 时间：UTC `timestamptz`
> 状态：已冻结

## 1. 数据库原则

1. PostgreSQL 保存业务事实，不保存图片二进制。
2. 图片只保存对象存储引用和生命周期信息。
3. 点数余额是可查询快照，点数流水才是账务事实。
4. 点数流水和审计日志只允许追加，不覆盖历史。
5. 生成任务状态与计费状态分开。
6. 所有任务、结算、退款和重试操作必须幂等。
7. 数据库迁移统一使用 Alembic。

## 2. 用户与身份

### `users`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | uuid | PK | 用户 ID |
| `display_name` | varchar(80) | nullable | 显示名称 |
| `avatar_asset_id` | uuid | nullable | 用户头像资源 |
| `status` | varchar | not null | `ACTIVE`、`SUSPENDED`、`DELETED` |
| `locale` | varchar(16) | not null | 语言 |
| `timezone` | varchar(64) | not null | 时区 |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

### `user_emails`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | uuid | PK | 记录 ID |
| `user_id` | uuid | FK | 用户 |
| `email` | citext | UNIQUE | 邮箱 |
| `verified_at` | timestamptz | nullable | 验证时间 |
| `is_primary` | boolean | not null | 主邮箱 |
| `created_at` | timestamptz | not null | 创建时间 |

邮箱验证令牌和会话放在 Redis，并设置 TTL；数据库只保存已验证的身份事实。

## 3. 邀请码

### `invite_codes`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | uuid | PK | 邀请码 ID |
| `code_hash` | varchar(128) | UNIQUE | 邀请码哈希，不保存明文 |
| `status` | varchar | not null | `ACTIVE`、`USED`、`EXPIRED`、`DISABLED` |
| `initial_points` | bigint | not null | 兑换点数 |
| `max_redemptions` | integer | not null | 最大兑换次数 |
| `redeemed_count` | integer | not null | 已兑换次数 |
| `expires_at` | timestamptz | nullable | 过期时间 |
| `created_by` | uuid | nullable | 管理员 |
| `created_at` | timestamptz | not null | 创建时间 |

### `invite_redemptions`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | uuid | PK | 兑换记录 |
| `invite_code_id` | uuid | FK | 邀请码 |
| `user_id` | uuid | FK | 用户 |
| `points_credited` | bigint | not null | 实际发放点数 |
| `created_at` | timestamptz | not null | 兑换时间 |

唯一约束：`invite_code_id + user_id`。

## 4. 点数账户与流水

### `point_wallets`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | uuid | PK | 钱包 ID |
| `user_id` | uuid | UNIQUE | 用户 |
| `available_points` | bigint | not null, >= 0 | 可用点数 |
| `reserved_points` | bigint | not null, >= 0 | 预留点数 |
| `lifetime_credited` | bigint | not null | 累计获得 |
| `lifetime_consumed` | bigint | not null | 累计消费 |
| `version` | bigint | not null | 乐观锁版本 |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

### `point_ledger`

这是唯一的点数事实来源，只允许插入。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | uuid | PK | 流水 ID |
| `wallet_id` | uuid | FK | 钱包 |
| `user_id` | uuid | FK | 用户 |
| `entry_type` | varchar | not null | `CREDIT`、`RESERVE`、`CONSUME`、`RELEASE`、`ADJUSTMENT` |
| `available_delta` | bigint | not null | 可用点数变化 |
| `reserved_delta` | bigint | not null | 预留点数变化 |
| `amount` | bigint | not null | 涉及点数 |
| `generation_id` | uuid | nullable | 生成任务 |
| `order_id` | uuid | nullable | 订单 |
| `idempotency_key` | varchar(128) | not null | 幂等键 |
| `reason` | varchar(255) | not null | 业务原因 |
| `metadata` | jsonb | not null | 扩展信息 |
| `created_at` | timestamptz | not null | 创建时间 |

典型变化：

```text
CREDIT   available +100, reserved 0
RESERVE  available -10,  reserved +10
CONSUME  available 0,    reserved -10
RELEASE  available +10,  reserved -10
```

同一个生成任务的 `CONSUME` 和 `RELEASE` 必须互斥且各自最多一条。

## 5. 生成任务

### `generation_cost_configs`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid PK | 配置 ID |
| `mode` | varchar | 生成模式 |
| `model_name` | varchar nullable | 模型名 |
| `output_count` | integer | 输出数量 |
| `points_cost` | integer | 点数成本 |
| `cost_multiplier` | numeric(12,4) | 成本倍率 |
| `effective_from` | timestamptz | 生效时间 |
| `effective_to` | timestamptz nullable | 失效时间 |
| `status` | varchar | `ACTIVE`、`INACTIVE` |
| `created_at` | timestamptz | 创建时间 |

### `generation_tasks`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | uuid | PK | 任务 ID |
| `user_id` | uuid | FK | 用户 |
| `parent_task_id` | uuid | nullable | 重试的原任务 |
| `mode` | varchar | not null | `PHOTO_PROMPT`、`TEXT_PROMPT`、`REFERENCE_IMAGE` |
| `status` | varchar | not null | 见状态机 |
| `billing_status` | varchar | not null | `RESERVED`、`CONSUMED`、`RELEASED` |
| `prompt` | text | not null | 最终 Prompt |
| `style_id` | uuid | nullable | 风格 |
| `prompt_template_id` | uuid | nullable | 模板 |
| `requested_count` | integer | not null | 第一阶段固定为 2 |
| `cost_config_id` | uuid | FK | 使用的计费配置 |
| `cost_snapshot` | jsonb | not null | 创建时的成本快照 |
| `reserved_points` | integer | not null | 本任务预留点数 |
| `provider_name` | varchar | nullable | 实际 Provider |
| `model_name` | varchar | nullable | 实际模型 |
| `idempotency_key` | varchar(128) | not null | 用户请求幂等键 |
| `retry_count` | integer | not null | 已重试次数 |
| `queued_at` | timestamptz | nullable | 入队时间 |
| `processing_at` | timestamptz | nullable | 开始处理时间 |
| `completed_at` | timestamptz | nullable | 完成时间 |
| `failure_code` | varchar | nullable | 失败码 |
| `failure_message` | text | nullable | 内部失败信息 |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

任务状态：

```text
CREATED → QUEUED → PROCESSING → SUCCEEDED
                         ├──────→ FAILED
                         ├──────→ CANCELLED
                         └──────→ NEEDS_REVIEW
```

`NEEDS_REVIEW` 表示 Provider 可能已经收费或成功，但系统无法确认结果。此状态保持点数 `RESERVED`，不自动消费，也不自动释放。

## 6. 图片资源与结果

### `media_assets`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid PK | 资源 ID |
| `user_id` | uuid FK | 所属用户 |
| `asset_type` | varchar | `ORIGINAL`、`REFERENCE`、`RESULT` |
| `visibility` | varchar | `PRIVATE`、`SHARED`、`PUBLIC` |
| `storage_provider` | varchar | `LOCAL`、`MINIO`、`COS`、`OSS`、`S3` |
| `storage_key` | varchar | 对象 Key |
| `mime_type` | varchar | 文件类型 |
| `size_bytes` | bigint | 文件大小 |
| `width` | integer | 宽度 |
| `height` | integer | 高度 |
| `sha256` | varchar(64) | 文件哈希 |
| `expires_at` | timestamptz | 二进制过期时间 |
| `deleted_at` | timestamptz nullable | 逻辑删除时间 |
| `created_at` | timestamptz | 创建时间 |

默认生命周期：原图和参考图 7 天，结果图 30 天；历史任务、元数据、点数和审计长期保留。

### `generation_inputs`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid PK | 输入记录 |
| `generation_id` | uuid FK | 任务 |
| `asset_id` | uuid FK | 图片资源 |
| `role` | varchar | `SOURCE`、`REFERENCE` |
| `sort_order` | integer | 顺序 |
| `created_at` | timestamptz | 创建时间 |

### `generation_outputs`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid PK | 输出记录 |
| `generation_id` | uuid FK | 任务 |
| `asset_id` | uuid FK | 结果资源 |
| `output_index` | integer | 输出序号 |
| `provider_output_id` | varchar nullable | 外部结果 ID |
| `created_at` | timestamptz | 创建时间 |

唯一约束：`generation_id + output_index`。

## 7. Provider 调用与成本

### `provider_attempts`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid PK | 调用记录 |
| `generation_id` | uuid FK | 任务 |
| `attempt_no` | integer | 调用序号 |
| `provider_name` | varchar | Provider |
| `model_name` | varchar | 模型 |
| `request_id` | varchar nullable | 外部请求 ID |
| `status` | varchar | `STARTED`、`SUBMITTED`、`SUCCEEDED`、`FAILED`、`TIMEOUT`、`UNKNOWN` |
| `input_tokens` | bigint nullable | 输入 Token |
| `output_tokens` | bigint nullable | 输出 Token |
| `image_count` | integer nullable | 图片数量 |
| `estimated_cost` | numeric(18,8) nullable | 估算成本 |
| `currency` | varchar(3) nullable | 成本币种 |
| `request_metadata` | jsonb | 脱敏请求信息 |
| `response_metadata` | jsonb | 脱敏响应信息 |
| `error_code` | varchar nullable | Provider 错误 |
| `error_message` | text nullable | 错误信息 |
| `started_at` | timestamptz | 开始时间 |
| `finished_at` | timestamptz nullable | 结束时间 |

图片模型没有 Token 用量时，Token 字段为 `NULL`。成本根据调用时的价格快照估算，只用于内部统计，不直接替代用户点数规则。

## 8. 风格、模板、分享和反馈

### `styles`

保存风格名称、描述、封面资源、发布状态和排序。

### `prompt_templates`

保存风格关联、模板文本、适用模式、发布状态和版本号。

### `shares`

保存分享 slug、任务、结果资源、标题、状态、过期时间和创建者。创建分享时把资源可见性改为 `SHARED`。

### `feedback`

保存用户、任务、评分、分类、内容和处理状态。

## 9. 订单预留

### `orders`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid PK | 订单 ID |
| `user_id` | uuid FK | 用户 |
| `status` | varchar | `PENDING`、`PAID`、`CANCELLED`、`REFUNDED` |
| `provider` | varchar nullable | 支付渠道 |
| `currency` | varchar(3) | 币种 |
| `amount_minor` | bigint | 最小货币单位金额 |
| `points_amount` | bigint | 购买点数 |
| `provider_order_id` | varchar nullable | 外部订单号 |
| `paid_at` | timestamptz nullable | 支付时间 |
| `created_at` | timestamptz | 创建时间 |

第一阶段不开发支付，但未来支付回调必须通过外部订单号和幂等键处理。

## 10. 审计日志

### `audit_logs`

记录 `USER`、`ADMIN`、`SYSTEM`、`WORKER` 的操作，包括邀请码兑换、点数变化、任务状态、退款、人工复核、订单和权限变化。

字段至少包括：

```text
id
actor_type
actor_id
action
entity_type
entity_id
request_id
ip_address
metadata
created_at
```

## 11. 幂等和索引

重要唯一约束：

```text
user_emails.email
invite_codes.code_hash
invite_redemptions.invite_code_id + user_id
generation_tasks.user_id + idempotency_key
generation_outputs.generation_id + output_index
point_ledger.generation_id + entry_type（适用时）
```

重要索引：

```text
point_ledger(user_id, created_at)
generation_tasks(user_id, created_at)
generation_tasks(status, created_at)
media_assets(user_id, expires_at)
provider_attempts(generation_id, attempt_no)
audit_logs(entity_type, entity_id, created_at)
```

## 12. 扣点、消费和释放事务

创建任务：

```text
锁定钱包
→ 校验 available_points
→ 创建 generation_tasks
→ 写入 RESERVE 流水
→ available_points - cost
→ reserved_points + cost
→ 提交
→ 投递 Celery
```

成功：

```text
锁定任务和钱包
→ 保存结果
→ 更新任务 SUCCEEDED
→ 写入 CONSUME 流水
→ reserved_points - cost
→ lifetime_consumed + cost
→ 提交
```

失败：

```text
锁定任务和钱包
→ 更新任务 FAILED
→ 写入 RELEASE 流水
→ available_points + cost
→ reserved_points - cost
→ 提交
```

人工复核：

```text
任务 NEEDS_REVIEW
→ 点数保持 RESERVED
→ 管理员确认成功：CONSUME
→ 管理员确认失败：RELEASE
```
