# AI Avatar Studio V1.0 数据库设计

> 数据库：PostgreSQL 15+
> 状态：V1.0 Schema 已冻结，旧测试版不属于迁移输入
> 适用：`LEAD-02` 设计基线；`ENG-01` 的 SQLAlchemy/Alembic 必须逐项实现本文件

## 1. 冻结原则

1. 所有主键和业务外键使用 UUID；数据库默认使用 `gen_random_uuid()`，应用也必须能够显式传入 UUID。
2. 所有时间字段使用带时区的 `timestamptz`，应用只写 UTC；不使用 `timestamp without time zone`。
3. 点数是非负整数，统一使用 `bigint`；金额使用最小货币单位 `bigint`；Provider 估算成本使用定点 `numeric`，禁止浮点。
4. PostgreSQL 只保存业务事实和媒体元数据，不保存图片二进制、Session、验证码或队列消息。
   「已发生的外部调用」属于业务事实，即使它不产出业务结果也要落库：参考图分析的付费登记
   （`analysis_dispatches`，见第 3.8 节）就属于这一类，队列消息本身仍然不落库。
5. 状态字段使用 `varchar` 加 `CHECK` 约束，不使用 PostgreSQL enum。状态值属于 API/Service 契约，新增状态必须先更新架构文档和迁移。
6. `point_ledger`、`audit_logs` 只允许追加；钱包是与流水同事务更新的余额快照，不是独立事实来源。
7. 用户、任务、订单、媒体元数据和审计历史在 V1.0 不物理删除。用户和资源通过状态/时间戳逻辑删除，二进制由生命周期任务清理。

## 2. 关系总览

```text
users ──< user_emails
  │ ├──< invite_redemptions >── invite_codes
  │ ├──1 point_wallets ──< point_ledger
  │ ├──< generation_tasks ──< generation_inputs >── media_assets
  │ │                         └─< generation_outputs >── media_assets
  │ ├──< orders ──< point_ledger
  │ ├──< shares >── generation_tasks/media_assets
  │ │        └──< share_assets（选中集合，一条链接可分享一张或两张结果图）
  │ ├──< analysis_dispatches（只追加，不关联任务、不关联媒体行）
  │ └──< feedback >── generation_tasks

styles ──< prompt_templates
styles/prompt_templates/generation_cost_configs ──< generation_tasks
generation_tasks ──< provider_attempts
所有业务实体 ──< audit_logs（多态 entity_id，不建立多态外键）
```

`users.avatar_asset_id` 和 `styles.cover_asset_id` 指向 `media_assets.id`；这两个外键在媒体表创建后再加入，以避免建表循环。资源归属用户的一致性由 Service 校验，不能仅依赖 nullable 外键。

## 3. 表、字段和约束

以下表定义是 V1.0 的完整最小集合。除特别说明外，`id` 为 `uuid PRIMARY KEY`，`created_at` 为 `timestamptz NOT NULL DEFAULT now()`。

> 小节号是**稳定的引用键**，不是分组标签：`backend/app/db/models/*.py` 与
> `backend/app/services/**` 的 docstring 都按「`02-database-design.md` 第 3.x 节」互相引用，
> 而早期编号已把多组表并进同一小节（3.2 同时涵盖点数与成本配置，3.4 同时涵盖目录、
> 订单、分享、反馈与审计）。因此本文件**不重排既有小节号**，新增表统一排到 §3 末尾。
> 第 3.8 节 `analysis_dispatches` 与第 3.9 节 `share_assets` 就是后续追加的两张表。

### 3.1 用户与邀请码

#### `users`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 用户 ID |
| `display_name` | varchar(80) | nullable | 用户显示名称 |
| `avatar_asset_id` | uuid | nullable，FK `media_assets.id`，`ON DELETE SET NULL` | 用户头像资源；媒体表创建后加 FK |
| `status` | varchar(16) | NOT NULL，`ACTIVE/SUSPENDED/DELETED` | 用户生命周期 |
| `locale` | varchar(16) | NOT NULL | 语言 |
| `timezone` | varchar(64) | NOT NULL | IANA 时区 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |
| `updated_at` | timestamptz | NOT NULL | 更新时间 |

约束：`status` 必须来自枚举值；`display_name` 由 Service 做长度和内容校验。

#### `user_emails`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 邮箱记录 |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 所属用户 |
| `email` | citext | NOT NULL UNIQUE | 规范化邮箱 |
| `verified_at` | timestamptz | nullable | 验证时间 |
| `is_primary` | boolean | NOT NULL DEFAULT false | 是否主邮箱 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |

另建唯一部分索引 `user_emails(user_id) WHERE is_primary`，每个用户最多一个主邮箱。令牌和 Session 只放 Redis。

#### `invite_codes`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 邀请码 ID |
| `code_hash` | varchar(128) | NOT NULL UNIQUE | 邀请码哈希，禁止明文 |
| `status` | varchar(16) | NOT NULL，`ACTIVE/USED/EXPIRED/DISABLED` | 邀请码状态 |
| `initial_points` | bigint | NOT NULL CHECK `> 0` | 每次兑换发放点数 |
| `max_redemptions` | integer | NOT NULL CHECK `> 0` | 最大兑换次数 |
| `redeemed_count` | integer | NOT NULL DEFAULT 0 CHECK `>= 0` | 已兑换次数 |
| `expires_at` | timestamptz | nullable | 过期时间 |
| `created_by` | uuid | nullable FK `users.id` `ON DELETE SET NULL` | 创建管理员 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |

CHECK `redeemed_count <= max_redemptions`；并发兑换时锁定邀请码行并在同一事务递增计数。

#### `invite_redemptions`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 兑换记录 |
| `invite_code_id` | uuid | NOT NULL FK `invite_codes.id` `ON DELETE RESTRICT` | 邀请码 |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 用户 |
| `points_credited` | bigint | NOT NULL CHECK `> 0` | 实际发放点数 |
| `created_at` | timestamptz | NOT NULL | 兑换时间 |

唯一约束：`(invite_code_id, user_id)`。该表与 `point_ledger` 的 CREDIT 记录在同一事务创建。

### 3.2 点数账户与流水

#### `point_wallets`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 钱包 ID |
| `user_id` | uuid | NOT NULL UNIQUE FK `users.id` `ON DELETE RESTRICT` | 一个用户一个钱包 |
| `available_points` | bigint | NOT NULL DEFAULT 0 CHECK `>= 0` | 可用点数快照 |
| `reserved_points` | bigint | NOT NULL DEFAULT 0 CHECK `>= 0` | 预留点数快照 |
| `lifetime_credited` | bigint | NOT NULL DEFAULT 0 CHECK `>= 0` | 累计获得 |
| `lifetime_consumed` | bigint | NOT NULL DEFAULT 0 CHECK `>= 0` | 累计消费 |
| `version` | bigint | NOT NULL DEFAULT 0 CHECK `>= 0` | 乐观锁版本 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |
| `updated_at` | timestamptz | NOT NULL | 更新时间 |

#### `point_ledger`

只允许 `INSERT`，是点数事实来源。`idempotency_key` 是服务端生成的操作级键，必须全局唯一；API 请求键不得直接复用为多个账务操作键。

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 流水 ID |
| `wallet_id` | uuid | NOT NULL FK `point_wallets.id` `ON DELETE RESTRICT` | 钱包 |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 用户冗余列，便于审计查询 |
| `entry_type` | varchar(16) | NOT NULL，`CREDIT/RESERVE/CONSUME/RELEASE/ADJUSTMENT` | 流水类型 |
| `available_delta` | bigint | NOT NULL | 可用余额变化 |
| `reserved_delta` | bigint | NOT NULL | 预留余额变化 |
| `amount` | bigint | NOT NULL CHECK `> 0` | 本次涉及点数绝对值 |
| `generation_id` | uuid | nullable FK `generation_tasks.id` `ON DELETE RESTRICT` | 关联任务 |
| `order_id` | uuid | nullable FK `orders.id` `ON DELETE RESTRICT` | 关联订单 |
| `idempotency_key` | varchar(128) | NOT NULL UNIQUE | 操作幂等键 |
| `reason` | varchar(255) | NOT NULL | 业务原因 |
| `metadata` | jsonb | NOT NULL DEFAULT `'{}'` | 脱敏扩展信息 |
| `created_at` | timestamptz | NOT NULL | 入账时间 |

数据库 CHECK 固定四种标准变化：

```text
CREDIT   amount>0, available_delta=amount,  reserved_delta=0
RESERVE  amount>0, available_delta=-amount, reserved_delta=amount
CONSUME  amount>0, available_delta=0,        reserved_delta=-amount
RELEASE  amount>0, available_delta=amount,  reserved_delta=-amount
```

`ADJUSTMENT` 由管理员 Service 产生并必须带审计记录，且必须同时满足：`reserved_delta = 0`、
`metadata ->> 'adjustment_kind' IS NOT NULL`、`adjustment_kind` 只能是 `POSITIVE_CREDIT` 或
`NEGATIVE_CONSUMPTION`、正向 kind 要求 `available_delta > 0` 并计入 `lifetime_credited`、
负向 kind 要求 `available_delta < 0` 并计入 `lifetime_consumed`。早期的宽松分支
`(available_delta <> 0 OR reserved_delta <> 0)` 已被迁移 0017 废除。

`IS NOT NULL` 这一项**不可省略，也不可只靠 kind 取值比较隐式表达**：PostgreSQL 的 CHECK
只在结果为 FALSE 时拒绝写入，若 `metadata` 缺少 `adjustment_kind` 或其值为 JSON `null`，
`metadata ->> 'adjustment_kind' = 'POSITIVE_CREDIT'` 返回 NULL，整条 CHECK 会被判为通过，
「必填」在数据库层失效。该缺陷由首次真实 PostgreSQL 集成验证暴露，迁移 0018 补齐，
当前完整表达式为：

```text
(entry_type = 'CREDIT'      AND available_delta =  amount AND reserved_delta =  0)
OR (entry_type = 'RESERVE'  AND available_delta = -amount AND reserved_delta =  amount)
OR (entry_type = 'CONSUME'  AND available_delta =  0      AND reserved_delta = -amount)
OR (entry_type = 'RELEASE'  AND available_delta =  amount AND reserved_delta = -amount)
OR (
    entry_type = 'ADJUSTMENT'
    AND reserved_delta = 0
    AND metadata ->> 'adjustment_kind' IS NOT NULL
    AND (
        (metadata ->> 'adjustment_kind' = 'POSITIVE_CREDIT'      AND available_delta > 0)
        OR (metadata ->> 'adjustment_kind' = 'NEGATIVE_CONSUMPTION' AND available_delta < 0)
    )
)
```

另建两个唯一部分索引：`(generation_id) WHERE entry_type='RESERVE'`，以及 `(generation_id) WHERE entry_type IN ('CONSUME','RELEASE')`。因此一个任务最多一次预留，且 CONSUME 与 RELEASE 互斥、总共最多一次。

账务一致性不是通过允许直接改余额实现的，而是以下不可变不变量：

```text
wallet.available_points = 所有 ledger.available_delta 之和
wallet.reserved_points = 所有 ledger.reserved_delta 之和
wallet.lifetime_credited = CREDIT 与 POSITIVE_CREDIT ADJUSTMENT 的累计业务值
wallet.lifetime_consumed = CONSUME 与 NEGATIVE_CONSUMPTION ADJUSTMENT 的累计业务值
```

Service 必须在同一数据库事务中 `SELECT ... FOR UPDATE` 钱包、插入流水、更新快照和递增 `version`；数据库角色禁止业务账号对流水执行 UPDATE/DELETE。定期核对任务只读比对快照与流水，不自动修账。

### 3.3 生成任务、媒体和 Provider

#### `generation_cost_configs`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 配置 ID |
| `mode` | varchar(32) | NOT NULL，生成模式 | 适用模式 |
| `model_name` | varchar(128) | nullable | 适用模型 |
| `output_count` | integer | NOT NULL CHECK `> 0` | 输出数量 |
| `points_cost` | bigint | NOT NULL CHECK `> 0` | 用户点数成本 |
| `cost_multiplier` | numeric(12,4) | NOT NULL DEFAULT 1 CHECK `> 0` | 成本倍率 |
| `effective_from` | timestamptz | NOT NULL | 生效时间 |
| `effective_to` | timestamptz | nullable CHECK `effective_to > effective_from` | 失效时间 |
| `status` | varchar(16) | NOT NULL，`ACTIVE/INACTIVE` | 配置状态 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |

唯一键：`(mode, model_name, output_count, effective_from)`，且必须声明为 `UNIQUE NULLS NOT DISTINCT`（PostgreSQL 15+）。`model_name` 可空，PostgreSQL 默认的 NULL 互不相等语义会允许同一 `mode + NULL model_name + output_count + effective_from` 重复写入，因此该唯一键必须显式使用 `NULLS NOT DISTINCT`，不得依赖默认语义。同一模式/模型的生效时间区间不得重叠，由 Service 校验并由 Repository 集成测试覆盖。创建任务时必须把整行关键值复制到 `generation_tasks.cost_snapshot`，历史任务不随配置修改而变化。

#### `generation_tasks`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 任务 ID |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 创建者 |
| `parent_task_id` | uuid | nullable FK `generation_tasks.id` `ON DELETE RESTRICT` | 重试来源 |
| `mode` | varchar(32) | NOT NULL，`PHOTO_PROMPT/TEXT_PROMPT/REFERENCE_IMAGE` | 生成模式 |
| `status` | varchar(20) | NOT NULL，`CREATED/QUEUED/PROCESSING/SUCCEEDED/FAILED/NEEDS_REVIEW` | 任务状态；V1.0 不保留 `CANCELLED` |
| `billing_status` | varchar(16) | NOT NULL，`RESERVED/CONSUMED/RELEASED` | 计费状态 |
| `prompt` | text | NOT NULL | 最终 Prompt |
| `style_id` | uuid | nullable FK `styles.id` `ON DELETE RESTRICT` | 风格 |
| `prompt_template_id` | uuid | nullable FK `prompt_templates.id` `ON DELETE RESTRICT` | 模板 |
| `requested_count` | integer | NOT NULL CHECK `> 0` | V1.0 固定为 2，由 Service 校验 |
| `cost_config_id` | uuid | NOT NULL FK `generation_cost_configs.id` `ON DELETE RESTRICT` | 使用的配置 |
| `cost_snapshot` | jsonb | NOT NULL | 创建时成本快照 |
| `reserved_points` | bigint | NOT NULL CHECK `> 0` | 本任务预留点数 |
| `provider_name` | varchar(64) | nullable | 实际 Provider |
| `model_name` | varchar(128) | nullable | 实际模型 |
| `idempotency_key` | varchar(128) | NOT NULL | 用户请求幂等键 |
| `retry_count` | integer | NOT NULL DEFAULT 0 CHECK `>= 0` | 已重试次数 |
| `queued_at` | timestamptz | nullable | 入队时间 |
| `processing_at` | timestamptz | nullable | 开始处理时间 |
| `completed_at` | timestamptz | nullable | 终态时间 |
| `failure_code` | varchar(64) | nullable | 稳定失败码 |
| `failure_message` | text | nullable | 内部诊断信息 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |
| `updated_at` | timestamptz | NOT NULL | 更新时间 |

唯一约束：`(user_id, idempotency_key)`。重试创建新任务，旧任务只读保留；`parent_task_id` 只允许指向同一用户的历史任务，由 Service 校验。

#### `media_assets`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 资源 ID |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 所属用户 |
| `asset_type` | varchar(16) | NOT NULL，`ORIGINAL/REFERENCE/RESULT` | 资源用途 |
| `visibility` | varchar(16) | NOT NULL DEFAULT `PRIVATE`，`PRIVATE/SHARED/PUBLIC` | 资源可见性 |
| `storage_provider` | varchar(16) | NOT NULL，`LOCAL/MINIO/COS/OSS/S3` | 对象存储驱动 |
| `storage_key` | varchar(512) | NOT NULL | 对象 Key |
| `mime_type` | varchar(128) | NOT NULL | MIME |
| `size_bytes` | bigint | NOT NULL CHECK `> 0` | 文件大小 |
| `width` | integer | nullable CHECK `> 0` | 图片宽度 |
| `height` | integer | nullable CHECK `> 0` | 图片高度 |
| `sha256` | varchar(64) | NOT NULL | 文件哈希 |
| `expires_at` | timestamptz | nullable | 二进制清理时间；持久资源可为空 |
| `deleted_at` | timestamptz | nullable | 逻辑删除时间 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |

唯一键：`(storage_provider, storage_key)`。`visibility` 是已批准字段；分享/撤销只能由 Service 改变。原图/参考图默认 7 天，结果图默认 30 天，实际期限写入 `expires_at`。

#### `generation_inputs`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 输入记录 |
| `generation_id` | uuid | NOT NULL FK `generation_tasks.id` `ON DELETE RESTRICT` | 任务 |
| `asset_id` | uuid | NOT NULL FK `media_assets.id` `ON DELETE RESTRICT` | 输入资源 |
| `role` | varchar(16) | NOT NULL，`SOURCE/REFERENCE` | 输入角色 |
| `sort_order` | integer | NOT NULL CHECK `>= 0` | 顺序 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |

唯一键：`(generation_id, role, sort_order)` 和 `(generation_id, asset_id)`。

#### `generation_outputs`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 输出记录 |
| `generation_id` | uuid | NOT NULL FK `generation_tasks.id` `ON DELETE RESTRICT` | 任务 |
| `asset_id` | uuid | NOT NULL FK `media_assets.id` `ON DELETE RESTRICT` | 结果资源 |
| `output_index` | integer | NOT NULL CHECK `>= 0` | 输出序号 |
| `provider_output_id` | varchar(256) | nullable | 外部结果 ID |
| `created_at` | timestamptz | NOT NULL | 创建时间 |

唯一键：`(generation_id, output_index)`；若 Provider 返回结果 ID，则在同一任务内不得重复。

#### `provider_attempts`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 调用记录 |
| `generation_id` | uuid | NOT NULL FK `generation_tasks.id` `ON DELETE RESTRICT` | 任务 |
| `attempt_no` | integer | NOT NULL CHECK `> 0` | 调用序号 |
| `provider_name` | varchar(64) | NOT NULL | Provider |
| `model_name` | varchar(128) | NOT NULL | 模型 |
| `request_id` | varchar(256) | nullable | Provider 外部请求 ID |
| `status` | varchar(16) | NOT NULL，`STARTED/SUBMITTED/SUCCEEDED/FAILED/TIMEOUT/UNKNOWN` | 调用状态 |
| `input_tokens` | bigint | nullable CHECK `>= 0` | 输入 Token；图片模型可为空 |
| `output_tokens` | bigint | nullable CHECK `>= 0` | 输出 Token；图片模型可为空 |
| `image_count` | integer | nullable CHECK `>= 0` | 输出图片数 |
| `estimated_cost` | numeric(18,8) | nullable CHECK `>= 0` | 调用时价格快照估算成本 |
| `currency` | varchar(3) | nullable | 成本币种；有成本时必填 |
| `request_metadata` | jsonb | NOT NULL DEFAULT `'{}'` | 脱敏请求元数据 |
| `response_metadata` | jsonb | NOT NULL DEFAULT `'{}'` | 脱敏响应元数据 |
| `error_code` | varchar(64) | nullable | 归一化错误码 |
| `error_message` | text | nullable | 内部错误信息 |
| `started_at` | timestamptz | NOT NULL | 开始时间 |
| `finished_at` | timestamptz | nullable | 结束时间 |

唯一键：`(generation_id, attempt_no)`；另建唯一部分索引 `(provider_name, request_id) WHERE request_id IS NOT NULL`，防止同一外部请求被重复记账。`estimated_cost` 只用于内部成本统计，不替代用户点数规则。

`provider_attempts` 只登记**生成任务**的调用。参考图分析不属于生成任务，没有 `generation_id`，
它自己的付费登记是独立的只追加表 `analysis_dispatches`（第 3.8 节）；两者不得互相代替，
也不得为了复用 `provider_attempts` 而给分析凭空造一条假任务。

### 3.4 风格、模板、订单、分享、反馈与审计

#### `styles`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 风格 ID |
| `slug` | varchar(96) | NOT NULL UNIQUE | 公开 URL 标识 |
| `name` | varchar(120) | NOT NULL | 风格名称 |
| `description` | text | NOT NULL DEFAULT `''` | 描述 |
| `cover_asset_id` | uuid | nullable FK `media_assets.id` `ON DELETE SET NULL` | 封面资源 |
| `status` | varchar(16) | NOT NULL，`DRAFT/PUBLISHED/ARCHIVED` | 发布状态 |
| `sort_order` | integer | NOT NULL DEFAULT 0 | 展示排序 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |
| `updated_at` | timestamptz | NOT NULL | 更新时间 |

#### `prompt_templates`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 模板 ID |
| `style_id` | uuid | nullable FK `styles.id` `ON DELETE RESTRICT` | 关联风格 |
| `slug` | varchar(96) | NOT NULL | 模板公开标识 |
| `mode` | varchar(32) | NOT NULL，生成模式 | 适用模式 |
| `template_text` | text | NOT NULL | 模板文本 |
| `version` | integer | NOT NULL CHECK `> 0` | 模板版本 |
| `status` | varchar(16) | NOT NULL，`DRAFT/PUBLISHED/ARCHIVED` | 发布状态 |
| `published_at` | timestamptz | nullable | 发布时间 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |
| `updated_at` | timestamptz | NOT NULL | 更新时间 |

唯一键：`(slug, mode, version)`；另建 `(slug, mode) WHERE status='PUBLISHED'` 唯一部分索引，保证每个模式最多一个当前发布版本。

#### `orders`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 订单 ID |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 用户 |
| `status` | varchar(16) | NOT NULL，`PENDING/PAID/CANCELLED/REFUNDED` | 订单状态 |
| `provider` | varchar(64) | nullable | 支付渠道预留 |
| `currency` | varchar(3) | NOT NULL | 币种 |
| `amount_minor` | bigint | NOT NULL CHECK `>= 0` | 最小货币单位金额 |
| `points_amount` | bigint | NOT NULL CHECK `> 0` | 订单对应点数 |
| `provider_order_id` | varchar(256) | nullable | 外部订单号 |
| `idempotency_key` | varchar(128) | NOT NULL | 订单写入幂等键 |
| `paid_at` | timestamptz | nullable | 支付时间 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |
| `updated_at` | timestamptz | NOT NULL | 更新时间 |

唯一键：`(user_id, idempotency_key)`；另建 `provider_order_id` 非空唯一部分索引。V1.0 只保留结构，不实现支付网关或价格规则。

#### `shares`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 分享 ID |
| `slug` | varchar(128) | NOT NULL UNIQUE | 分享 URL 标识（服务端 32 字节随机生成，不含内部 ID 或存储路径） |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 创建者 |
| `generation_id` | uuid | NOT NULL FK `generation_tasks.id` `ON DELETE RESTRICT` | 任务 |
| `asset_id` | uuid | NOT NULL FK `media_assets.id` `ON DELETE RESTRICT` | **主资产**：选中输出中 `output_index` 最小者 |
| `title` | varchar(160) | nullable | 分享标题 |
| `status` | varchar(16) | NOT NULL，`ACTIVE/REVOKED/EXPIRED` | 分享状态 |
| `expires_at` | timestamptz | nullable | 分享过期时间 |
| `idempotency_key` | varchar(128) | NOT NULL | 创建分享幂等键 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |
| `revoked_at` | timestamptz | nullable | 撤销时间 |

唯一键：`(user_id, idempotency_key)`。创建分享必须同时把**被选中**的媒体 `visibility` 置为 `SHARED`；撤销/过期只改变状态和可见性，不删除任务历史。

**可见性归属（ENG-09 第三轮收敛）**：`SHARED` 是本卡**唯一拥有**的可见性取值。

| 路径 | 允许的动作 |
|---|---|
| 创建分享（`ShareService`） | 把被选中且 `PRIVATE` 的资产置 `SHARED`（这是唯一的"提升"动作，依据是明确的创建意图） |
| 撤销 / 分享到期 / 资产失效（生命周期） | 只做**收敛**：资产是 `SHARED` 且已无有效分享 → 恢复 `PRIVATE` |
| 生命周期遇到 `PRIVATE` | **保持原样**。存在有效分享**不构成**提升依据 —— 否则一个明确不公开的资产会被重新公开 |
| 生命周期遇到 `PUBLIC` | **保持原样**。`PUBLIC` 是有意发布（`04-ai-provider.md` 第 8 节），不属于本卡 |

匿名读取的授权是**实时**判定（`ShareService.asset_has_valid_share` + 资产未到期），
不是读 `visibility` 快照；`PRIVATE` 对非所有者一律 404，匿名资产读取与短期签名地址都如此。

#### 锁顺序（ENG-09 第三、四轮：并发策略）

`shares` 与 `media_assets` 是同一聚合里会被并发改写的两类行。它们的取锁顺序
**全卡唯一**，四条路径（创建 / 撤销 / 分享到期 / 媒体清理）都必须遵守：

```text
第 0 级  咨询锁 (user_id, idempotency_key)
第 1 级  shares 行锁，按 id 升序
第 2 级  media_assets 行锁，按 id 升序
第 3 级  状态与可见性写入
```

**硬规则 ①：不得在持有第 2 级锁之后再取第 1 级锁。** 违反它会形成
「清理持资产等分享、撤销持分享等资产」的真实死锁 —— 已在隔离 PostgreSQL 上用
对照实验复现（旧顺序下撤销抛 `DeadlockDetected`，新顺序下两者都正常完成，
见 `tests/db/test_lock_order.py` 与 `tests/db/test_engagement_concurrency.py`）。

**硬规则 ②：第 2 级的锁必须对「整个事务要动的资产集合」一次性排序后取得。**
逐条处理时各锁各的资产是不够的：两条到期分享依次关联高/低资产时，事务内的资产取锁
顺序会变成 **高→低**，与创建分享的 **低→高** 交叉即死锁（第三轮遗留缺陷，第四轮修复）。
因此 `ShareLifecycle` 只暴露 `revalidate_assets(...)`（整批排序 + 一次 `lock_assets_by_id`），
`close_share` 退化为**只写状态**，不再自行取资产锁。

因此媒体清理被拆成「分享收敛」与「资产清理」两个阶段（`run_once`）：

```text
分享收敛：预读「到期分享 ∪ 引用失效资产的 ACTIVE 分享」
          → 一次性按 id 升序锁分享 → 一次性按 id 升序锁资产 → 关闭 + 收敛可见性
资产清理：预读到期资产（不加锁）→ 一次性按 id 升序锁资产 → 删对象 → 置 EXPIRED
```

分享收敛的目标集合**与分享自身期限无关**：资产已到期/已删除时，引用它的 `ACTIVE`
分享（含分享期限在未来、为 `NULL`、或没有 `share_assets` 行的历史单图分享）
都要被收敛，否则资产会被无限期 `deferred`、公开链接一直可读。
资产清理阶段遇到「锁内确认仍被 ACTIVE 分享引用」的资产仍记 `deferred`，
但那只会来自"预读之后新写入的分享"（并发创建），并且该分享会在后续轮次进入
分享收敛阶段 —— 目标按 id 升序且有界，因此**每个候选都会在有限轮次内被轮到**；
这不是"一轮内一定收敛"的承诺。

`lock_share_ids` / `lock_assets_by_id` 在输入未排序时直接抛错，是防回归的最后一道闸。


`asset_id` 的语义在 ENG-09（迁移 0021）被收紧为「主资产」，与新增的 `share_assets`
共同表达「一条链接分享一张或两张」：

- **保留该列**让历史单图分享无需改写即可继续被读取，也保证 `NOT NULL` 始终成立；
- 完整集合落在 `share_assets`（第 3.9 节）；匿名读取优先取关联表，
  关联为空时回落到该列（历史兼容）；
- 未被选中的结果**保持 `PRIVATE`**，不会因为同任务另一张被分享而一起公开。

#### `feedback`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 反馈 ID |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 用户 |
| `generation_id` | uuid | NOT NULL FK `generation_tasks.id` `ON DELETE RESTRICT` | 任务 |
| `rating` | smallint | NOT NULL CHECK `rating BETWEEN 1 AND 5` | 评分 |
| `category` | varchar(64) | NOT NULL（迁移 0021 由 32 扩宽至 64） | 分类 |
| `content` | text | nullable | 反馈内容 |
| `status` | varchar(16) | NOT NULL DEFAULT `OPEN`，`OPEN/REVIEWED/RESOLVED` | 处理状态 |
| `created_at` | timestamptz | NOT NULL | 创建时间 |
| `updated_at` | timestamptz | NOT NULL | 更新时间 |

唯一键：`(user_id, generation_id)`，保证一个用户对同一任务只有一条 V1.0 反馈；修改反馈使用 UPDATE，不影响审计日志。

`category` 的列宽与冻结契约 `FeedbackCreateRequest.category`（`maxLength: 64`）保持一致；
迁移 0021 的降级守卫会拒绝把列收回 32（详见第 7 节），因为那会截断已写入的长分类。

#### `audit_logs`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK | 审计记录 |
| `actor_type` | varchar(16) | NOT NULL，`USER/ADMIN/SYSTEM/WORKER` | 操作者类型 |
| `actor_id` | uuid | nullable | 操作者；系统操作可为空 |
| `action` | varchar(96) | NOT NULL | 动作 |
| `entity_type` | varchar(64) | NOT NULL | 实体类型 |
| `entity_id` | uuid | nullable | 实体 ID；多态，不建 FK |
| `request_id` | varchar(128) | nullable | 请求/任务上下文 |
| `ip_address` | inet | nullable | 脱敏后的来源 IP |
| `metadata` | jsonb | NOT NULL DEFAULT `'{}'` | 脱敏元数据 |
| `created_at` | timestamptz | NOT NULL | 记录时间 |

只允许追加；不得记录 API Key、Cookie、令牌、原图、完整 Prompt、签名 URL 或 Provider 原始响应。

### 3.8 参考图分析的付费登记

#### `analysis_dispatches`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `id` | uuid | PK，DEFAULT `gen_random_uuid()` | 登记行 ID |
| `user_id` | uuid | NOT NULL FK `users.id` `ON DELETE RESTRICT` | 发起分析的用户 |
| `usage_day` | date | NOT NULL | UTC 日历日，与每日限额口径同源 |
| `digest` | varchar(64) | NOT NULL，CHECK `length(digest) = 64` | 原图 sha256 十六进制摘要，只用于去重 |
| `provider_name` | varchar(32) | NOT NULL | 实际调用的视觉 Provider |
| `model_name` | varchar(128) | NOT NULL | 实际调用的视觉型号 |
| `outcome` | varchar(16) | NOT NULL DEFAULT `'UNKNOWN'`，`SUCCEEDED/FAILED/UNKNOWN` | 调用终态 |
| `created_at` | timestamptz | NOT NULL DEFAULT `now()` | 登记时间；与 `usage_day` 必须是同一时刻 |

约束与索引：

```text
ux  analysis_dispatches(user_id, usage_day, digest)      # 唯一：uq_analysis_dispatches_user_id_usage_day_digest
ck  outcome IN ('SUCCEEDED','FAILED','UNKNOWN')          # ck_analysis_dispatches_outcome
ck  length(digest) = 64                                  # ck_analysis_dispatches_digest_length
ck  usage_day = (created_at AT TIME ZONE 'UTC')::date    # ck_analysis_dispatches_usage_day_matches_created_at
ix  analysis_dispatches(usage_day)                       # 全局日用量聚合
```

语义：

1. **一行 = 一次已经确定要送出的视觉调用。** 登记发生在出网**之前**，不是之后；
2. **`outcome` 默认 `UNKNOWN`，并且 `UNKNOWN` 继续占用当日额度。** 「调用发出后进程崩溃或
   超时、无法证明供应商没有受理」是最贵的情形，默认值让这种情形天然落在最保守的一档：
   该行不删、不退还，自动重试也打不到第二次付费。只有拿到明确成功/明确失败才收敛为
   `SUCCEEDED`/`FAILED`，两者同样不退还额度；
3. **`digest` 只用来去重，不承载内容。** 不存图片字节、不存 `storage_key`、不存描述、
   不存供应商原始响应；sha256 摘要不可逆推原图，因此允许长期留存。表内没有任何媒体外键：
   图片本身继续走 `media_assets` 的 7 天生命周期，删图不影响这笔已发生的支出登记；
4. **唯一键是并发下的最终裁决。** 即使 Redis 单飞锁失效（重启、驱逐、`FLUSHALL`），
   同一用户同一天同一张图也只可能插入一次；
5. **`usage_day` 是冗余列，不是第二处真相。** 它由应用与 `created_at` 同源写入，
   并由 CHECK 钉住同一天，避免出现「按 `created_at` 算一天、按 `usage_day` 算另一天」的漂移；
   冗余的理由是唯一键与按日聚合的索引必须作用在真实列上，不能对 `created_at` 套函数。

`analysis_dispatches` 是「参考图片找灵感」每日付费配额的**持久事实来源**：

```text
PostgreSQL（本表）  每日配额的裁决者：按 usage_day 计数 → 判额 → 插行，全部在同一事务内
Redis              只负责描述缓存与同一张图的单飞锁；它不是额度真值，丢失不影响额度
```

判额口径与 Redis 的关系由 `03-api-contract.md` 第 4.6 节固定：配额数的是**出网尝试次数**，
失败不退还；闸门自身出错一律 fail-closed。超限复用既有 `RATE_LIMITED`（429，带 `Retry-After`），
闸门不可用复用既有 `NOT_READY`（503），**不新增错误码**。

规模与保留：单行约百字节级，每用户每日最多 `ANALYSIS_DAILY_LIMIT_PER_USER` 行、
全站每日最多 `ANALYSIS_GLOBAL_DAILY_LIMIT` 行，因此无需额外分区或归档策略。
本表按「只追加 + 终态收敛」使用：除 `outcome` 之外任何列都不得更新。

### 3.9 分享的选中资产集合

#### `share_assets`

| 字段 | 类型 | 约束/默认值 | 用途 |
|---|---|---|---|
| `share_id` | uuid | PK 之一，FK `shares.id` `ON DELETE RESTRICT` | 分享 |
| `asset_id` | uuid | PK 之一，FK `media_assets.id` `ON DELETE RESTRICT` | 被明确选中的结果资产 |
| `sort_order` | integer | NOT NULL CHECK `>= 0` | 呈现顺序，取自 `generation_outputs.output_index` |
| `created_at` | timestamptz | NOT NULL DEFAULT `now()` | 创建时间 |

约束与索引：

```text
pk  share_assets(share_id, asset_id)          # 同一次分享里同一张图只能关联一次
uq  share_assets(share_id, sort_order)        # 顺序不重复
ck  sort_order >= 0                           # ck_share_assets_sort_order
ix  share_assets(asset_id, share_id)          # 生命周期按资产反查「还有没有有效分享」
```

语义（迁移 0021，ENG-09）：

1. **只登记被明确选中的资产。** 同任务未选中的结果既不写这里，也不被置为 `SHARED`；
   因此「分享两张中的一张」不会把另一张也暴露出去，匿名读取对未选中的图仍是 404；
2. **`shares.asset_id` 与它的关系是「主资产 + 完整集合」**：主资产 = `output_index`
   最小者。历史单图分享（0021 之前创建）由迁移回填一行 `sort_order = 0`，
   因此关联为空只可能出现在人工改库之后，读取端仍保留回落逻辑；
3. **无物理删除**：撤销、过期、资产失效都只改 `shares.status` 与
   `media_assets.visibility`，关联行保留，分享历史因此可追溯（第 4 节）；
4. 规模上每条分享最多两行（每次生成固定两张），无需分区或归档。

**降级守卫（必须在部署前知道）**：`share_assets` 里只要存在「与主资产不同」的关联行，
0021 的 `downgrade` 就会**明确拒绝**，因为旧 `asset_id` 无法表达多图关联，
强行降级等于静默丢失事实。恢复条件与 `feedback.category` 的守卫见第 7 节。

## 4. 外键与删除策略

| 关系 | 删除策略 | 原因 |
|---|---|---|
| 用户到邮箱、钱包、邀请码兑换、任务、媒体、订单、分享、反馈 | `ON DELETE RESTRICT` | 用户只软删除，保留身份与账务历史 |
| 用户到分析付费登记（`analysis_dispatches`） | `ON DELETE RESTRICT` | 已发生的付费事实不可级联丢失 |
| 分享与媒体到分享关联（`share_assets`） | `ON DELETE RESTRICT` | 分享的选中集合是公开范围的事实，不可级联丢失 |
| 邀请码到兑换记录 | `RESTRICT` | 兑换事实不可级联丢失 |
| 钱包/任务/订单到点数流水 | `RESTRICT` | 流水不可丢失 |
| 任务到输入、输出、Provider 调用、分享、反馈 | `RESTRICT` | 历史和审计不可级联删除 |
| 输入/输出到媒体 | `RESTRICT` | 只清理对象二进制，不删除元数据 |
| `users.avatar_asset_id`、`styles.cover_asset_id` | `SET NULL` | 删除头像/封面不删除拥有者 |
| 邀请码 `created_by` | `SET NULL` | 管理员软删除后保留邀请码 |

V1.0 禁止对业务表执行物理删除。`media_assets.deleted_at` 标记逻辑删除；生命周期任务先删除对象存储二进制，再保留数据库元数据。只有经过技术负责人批准的归档/保留策略才能引入物理删除迁移。

## 5. 索引清单

除 PK、UNIQUE 自动索引外，必须建立：

```text
users(status, created_at)
user_emails(user_id) WHERE is_primary
invite_codes(status, expires_at)
invite_redemptions(user_id, created_at)
point_ledger(user_id, created_at, id)
point_ledger(wallet_id, created_at, id)
point_ledger(generation_id) WHERE generation_id IS NOT NULL
generation_cost_configs(status, effective_from)
generation_tasks(user_id, created_at, id)
generation_tasks(status, created_at, id)
generation_tasks(parent_task_id) WHERE parent_task_id IS NOT NULL
media_assets(user_id, created_at, id)
media_assets(expires_at) WHERE expires_at IS NOT NULL AND deleted_at IS NULL
generation_inputs(generation_id, role, sort_order)
generation_outputs(generation_id, output_index)
generation_outputs(generation_id, provider_output_id) WHERE provider_output_id IS NOT NULL
provider_attempts(generation_id, attempt_no)
provider_attempts(provider_name, request_id) WHERE request_id IS NOT NULL
analysis_dispatches(usage_day)
share_assets(asset_id, share_id)
styles(status, sort_order)
prompt_templates(style_id, status)
prompt_templates(slug, mode) WHERE status='PUBLISHED'
orders(user_id, created_at, id)
orders(provider_order_id) WHERE provider_order_id IS NOT NULL
shares(generation_id, status)
feedback(status, created_at)
audit_logs(entity_type, entity_id, created_at)
audit_logs(actor_id, created_at) WHERE actor_id IS NOT NULL
audit_logs(request_id) WHERE request_id IS NOT NULL
```

账务最终化的两个部分唯一索引：

```text
point_ledger(generation_id) WHERE entry_type = 'RESERVE'
point_ledger(generation_id) WHERE entry_type IN ('CONSUME', 'RELEASE')
```

## 6. 事务与状态不变量

### 6.1 创建任务/预留

```text
锁定 users/point_wallets 行（FOR UPDATE）
→ 校验用户、成本快照和 available_points
→ 插入 generation_tasks（billing_status=RESERVED）
→ 插入唯一 RESERVE 流水
→ 更新 available_points -= cost, reserved_points += cost, version += 1
→ 提交后再投递 Celery
```

投递失败不能回写为已入队；由可靠投递/补偿任务依据数据库状态重新投递。相同用户和幂等键只能返回第一次任务。

### 6.2 成功、失败和人工复核

```text
锁定 generation_tasks 和 point_wallets
→ 校验 billing_status=RESERVED
→ 成功：保存 outputs/assets，写 CONSUME，reserved -= cost，lifetime_consumed += cost
→ 明确失败（包括内部取消已明确未执行）：写 RELEASE，available += cost，reserved -= cost
→ UNKNOWN 或 NEEDS_REVIEW：不写 CONSUME/RELEASE，保持 RESERVED
→ 提交
```

任务状态与计费状态必须满足：`SUCCEEDED => CONSUMED`、`FAILED => RELEASED`；未完成人工决定的 `NEEDS_REVIEW => RESERVED`，人工 `CONSUME` 后为 `NEEDS_REVIEW + CONSUMED`，人工 `RELEASE` 后为 `NEEDS_REVIEW + RELEASED`。人工结算只改变 `billing_status`，不改变任务状态；必须有唯一最终流水、人工审计和幂等记录。重复 Worker、Webhook 或人工请求依赖唯一索引和幂等键返回已有结果。生成任务不使用 `CANCELLED`；订单表的 `CANCELLED` 仅属于订单域。

### 6.3 输入、输出和生命周期

- `PHOTO_PROMPT` 必须恰好一个 `SOURCE`；`TEXT_PROMPT` 不得有图片；`REFERENCE_IMAGE` 的开关和数量由 Service 校验。
- `requested_count` V1.0 固定为 2；数据库只保证正数，产品规则由 Service/配置保证。
- 结果缺失、Provider 成功但无法确认外部状态时进入 `NEEDS_REVIEW`，不得自动扣点或退款。
- 媒体对象过期、撤销分享或用户删除不会级联删除任务、流水、审计和 Provider 记录。

## 7. Alembic 迁移顺序

迁移必须从空 PostgreSQL 数据库执行，不能读取或转换旧 SQLite/Drizzle 表。建议使用以下不可修改的线性 revision：

| 顺序 | Revision | 内容 |
|---:|---|---|
| 1 | `0001_v1_extensions` | 启用 `pgcrypto`、`citext`；建立 UUID/时间基础约定 |
| 2 | `0002_users_identity` | `users`、`user_emails`、用户状态检查和邮箱唯一索引 |
| 3 | `0003_invites` | `invite_codes`、`invite_redemptions`、兑换唯一键和计数检查 |
| 4 | `0004_wallets` | `point_wallets`、余额非负检查 |
| 5 | `0005_media_assets` | `media_assets`、对象 Key 唯一键和生命周期索引 |
| 6 | `0006_media_reference_fks` | 添加 `users.avatar_asset_id` 外键 |
| 7 | `0007_styles_and_templates` | `styles`、`prompt_templates`、发布版本唯一部分索引 |
| 8 | `0008_generation_cost_configs` | 成本配置、点数成本检查和生效索引 |
| 9 | `0009_generation_tasks` | `generation_tasks`、自引用重试外键、任务幂等键 |
| 10 | `0010_generation_io` | `generation_inputs`、`generation_outputs` 及序号唯一键 |
| 11 | `0011_orders` | 订单预留表、订单幂等键和外部订单号索引 |
| 12 | `0012_provider_attempts` | Provider 调用、用量/成本字段、外部 request_id 唯一索引 |
| 13 | `0013_point_ledger` | 流水表、任务/订单外键、delta 检查、最终结算唯一索引 |
| 14 | `0014_shares_feedback` | 分享和反馈表、权限/幂等唯一键 |
| 15 | `0015_audit_logs` | 审计表、实体/请求查询索引 |
| 16 | `0016_append_only_guards` | 流水/审计拒绝 UPDATE/DELETE 的数据库触发器或等价权限保护 |
| 17 | `0017_constraint_alignment`（文件 `0017_corrective_constraint_alignment.py`；2026-09-27 起 revision 标识缩短至 ≤32 字符以适配 `alembic_version.version_num varchar(32)`，文件名与人类可读说明「0017 corrective constraint alignment」不变） | **纠正性追加**：移除 `generation_tasks.status` 的 `CANCELLED`（订单域不受影响）；收紧 `point_ledger` 的 `ADJUSTMENT` 规则（`metadata.adjustment_kind` 必填且仅 `POSITIVE_CREDIT`/`NEGATIVE_CONSUMPTION`、符号与 kind 一致、`reserved_delta = 0`）。不改写 0001–0016，不重排既有 revision |
| 18 | `0018_adjustment_kind_required` | **纠正性追加**：`point_ledger` 的 `ADJUSTMENT` 分支显式加 `metadata ->> 'adjustment_kind' IS NOT NULL`。0017 的表达式在 kind 缺失或为 JSON `null` 时整体返回 NULL，而 PostgreSQL 的 CHECK 把 NULL 视为通过，导致「必填」在数据库层失效；该缺陷由首次真实 PostgreSQL 集成验证暴露。其余四种 entry_type 规则、其它表、索引与触发器不变；0017 已应用，故不修改其文件 |
| 19 | `0019_asset_lifecycle` | **追加**：为 ENG-05 的媒体生命周期补齐 `media_assets` 的 `purpose`、`status`、可空 `idempotency_key` 与 `(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL` 部分唯一索引、状态与删除时间戳 CHECK、生命周期索引。不改写 0001–0018 |
| 20 | `0020_analysis_dispatches` | **追加**：新增只追加表 `analysis_dispatches`（第 3.8 节），作为「参考图片找灵感」每日付费配额的持久事实来源，含唯一键 `uq_analysis_dispatches_user_id_usage_day_digest`、三个 CHECK 与 `ix_analysis_dispatches_usage_day`。不改写 0001–0019，**不回填**历史（0020 之前的出网次数只在 Redis 里计过，无法可信复原） |
| 21 | `0021_share_asset_links`（文件 `0021_share_assets_and_feedback_category.py`；revision 标识取 21 字符以适配 `alembic_version.version_num varchar(32)`，文件名保留完整人类可读说明） | **追加（当前 head）**：新增 `share_assets`（第 3.9 节），承载「一条链接分享一张或两张」的**选中集合**，并把 `feedback.category` 由 `varchar(32)` 扩宽到 `varchar(64)` 以对齐冻结契约的 `maxLength: 64`。`shares.asset_id` 保留为主资产语义不变；**升级时回填**历史单图分享各一行 `sort_order = 0`。不改写 0001–0020 |

`0019` → `0020` → `0021` 都是**追加式**迁移：`0021_share_asset_links` 是当前唯一迁移 head，
`0001` 到 `0021` 构成单条线性链，没有分支、没有合并点。任何后续卡片只能在这条链尾部继续追加。

每个 revision 必须可单独 `upgrade`，`upgrade head` 在空库成功；已应用 revision 不得编辑，只能追加新 revision。`downgrade` 仅用于本地测试，不得作为生产删除策略。

`0021` 的降级守卫（**必须显式执行并承担后果**，见 `0021` 迁移文件与真实 PostgreSQL 用例）：

```text
拒降级条件 ①  share_assets 里存在与 shares.asset_id 不同的关联行
             （旧 asset_id 无法表达多图关联，降级等于静默丢事实）
拒降级条件 ②  feedback.category 里存在 > 32 字符的值
             （收回 varchar(32) 会截断历史分类）
允许降级     只有「单图、可由 asset_id 表达」的历史 + 分类都 ≤32 字符
             → 无损往返：downgrade 后 shares/feedback 行本身未被改动，
               重新 upgrade 会再次回填同一批关联
```

迁移**不会**自动删除关联、截断分类或改写历史。守卫报错时会写明**实际可执行的恢复条件**
（见下），由部署方处理后重跑：

```text
恢复条件 ①  让 share_assets 表本身只含「与主资产一致」的关联：
            删除 (share_id, asset_id) 且 asset_id <> shares.asset_id 的行。
            撤销分享、让分享到期、或把关联清单另存导出都**不会**让守卫通过 ——
            分享状态变化只改 shares.status，不删 share_assets 的行；
            守卫每次读的都是当前表内容，不是历史记录。
恢复条件 ②  把每一行 feedback.category 改写为 ≤32 字符（或置空）。
            把长分类导出到审计/工单只是留证，导出本身不会让守卫通过。
            不接受「先截断再降级」。
```

真实 PostgreSQL 用例把这两条恢复条件都跑了一遍：先断言守卫拒绝，再按上面的方式
收敛数据，断言降级随后被允许（`tests/db/test_share_assets_migration.py`）。

`0020` 的一次性代价（部署方必须知情）：新表从空开始，**上线当天的分析已用额度会重置一次**。
这是「把闸门从易失存储搬到持久存储」的必然结果，不接受伪造回填——0020 之前的计数只在 Redis 里，
重启或驱逐后连用户自己都无法复原，写回任何推算值都等于凭空记账。

`0017` 的数据前置处理：已存在的 `generation_tasks.status = 'CANCELLED'` 行按 `LEAD-04` 第 4 节归入
`NEEDS_REVIEW`（保持 `billing_status = 'RESERVED'`，满足 `NEEDS_REVIEW => RESERVED` 不变量），
**不生成任何 `point_ledger` 流水**；不符合新 `ADJUSTMENT` 规则的 `point_ledger` 历史行不被静默改写，
迁移会以明确错误终止，交由运营按审计记录处理。

## 8. `ENG-01` 可直接执行的工程说明

> 本节是 `ENG-01` 当时（迁移链停在 `0016`）的授权记录，作为历史证据保留。
> 迁移已推进到 `0020`（第 7 节），白名单里的版本区间不随之改写；后续卡片各自的允许范围
> 以对应任务卡与交接文档为准。

### 允许修改的文件

仅允许新增/修改：

```text
backend/app/db/base.py
backend/app/db/session.py
backend/app/db/models/*.py
backend/alembic/env.py
backend/alembic/versions/0001_v1_extensions.py ... 0016_append_only_guards.py
backend/tests/db/test_migrations.py
backend/tests/db/test_constraints.py
backend/tests/db/test_points_invariants.py
docker/compose.yml（仅补 PostgreSQL/迁移启动依赖）
```

不得修改旧 Drizzle/SQLite 文件、API 路径、Pydantic 响应、Provider 接口、点数价格或产品规则。

### 实现规则

1. SQLAlchemy 2.x 使用 `Uuid(as_uuid=True)`、`DateTime(timezone=True)`、`BigInteger`、`Numeric(18, 8)`、`JSONB`、`INET`、`CITEXT`；模型字段名必须与本文件完全一致。
2. 状态用 `String/VARCHAR + CheckConstraint`；不得生成 Python/PG enum 作为数据库真值。
3. 所有 FK 的 `ondelete` 按第 4 节实现；涉及循环的头像外键在 `0006` 添加。
4. 对部分唯一索引使用 Alembic `op.create_index(..., unique=True, postgresql_where=...)`；不能用应用层查询替代数据库唯一约束。
5. 在 `0001` 启用扩展，在 `0016` 建立流水和审计追加保护。业务角色必须没有这两张表的 UPDATE/DELETE 权限；触发器作为第二道保护。
6. Points Service 的钱包行锁、流水插入、快照更新和 `version` 递增必须处于同一事务；Repository 不得隐式 commit。
7. 迁移脚本必须支持重复执行检查；不得在迁移中读取旧数据库或写入业务种子数据。风格、模板和成本配置由后续管理/配置任务创建。

### 必须运行的测试

```text
alembic upgrade head                         # 空库成功
alembic upgrade head                         # 重复执行无变化
pytest backend/tests/db/test_migrations.py
pytest backend/tests/db/test_constraints.py
pytest backend/tests/db/test_points_invariants.py
```

测试至少证明：主键/类型正确；所有唯一键和外键拒绝非法数据；任务相同用户/幂等键不能重复；Provider `request_id` 不能重复；CONSUME/RELEASE 互斥；流水和审计 UPDATE/DELETE 被拒绝；并发预留在 `SELECT FOR UPDATE` 下不会造成负余额；钱包快照、流水 delta、任务 billing_status 三者一致。

## 9. 明确不做与未决问题

本卡不实现 SQLAlchemy、Alembic、Repository、Service、支付网关、价格/产品规则、社区/推荐、分库分表或旧测试版数据迁移。

提交技术负责人审核时需要明确记录的未决项：

1. 生产 PostgreSQL 托管服务是否允许 `pgcrypto`、`citext` 和触发器；若不允许，必须在 ENG-01 开始前批准替代方案。
2. 用户头像是否需要永久保留；当前允许 `expires_at=NULL`，具体生命周期由产品/隐私负责人确认。
3. `ADJUSTMENT` 的统计口径已由 LEAD-04 冻结：`POSITIVE_CREDIT` 计入 `lifetime_credited`，`NEGATIVE_CONSUMPTION` 计入 `lifetime_consumed`；审批角色仍限定为具备管理员权限的人工复核主体，具体 RBAC 角色名由管理域实现确定，不得改变账务语义。
4. 成本配置的时间区间不重叠目前由 Service + 集成测试保证，是否增加 PostgreSQL `EXCLUDE` 约束需单独评审，不能由 ENG-01 自行添加。
5. 是否允许同一任务创建多个分享链接；当前 Schema 允许，分享状态和幂等键控制重复写入。
