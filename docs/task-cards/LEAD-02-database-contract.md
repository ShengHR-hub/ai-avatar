# LEAD-02：PostgreSQL 数据库设计与迁移契约

> 状态：已通过（2026-09-25，技术负责人审核结论见文末）
> 范围：AI Avatar Studio V1.0 正式系统，从空 PostgreSQL 设计；旧测试版不是迁移目标
> 关联文档：[数据库设计](../architecture/02-database-design.md)

## 会话目的

冻结 V1.0 PostgreSQL Schema、字段、索引、外键、唯一约束、删除策略、账务不变量和 Alembic 迁移顺序，使 `ENG-01` 可以直接实现 SQLAlchemy 模型和第一版迁移。

## 冻结的数据表清单

| 领域 | 表 |
|---|---|
| 身份 | `users`、`user_emails` |
| 邀请 | `invite_codes`、`invite_redemptions` |
| 点数 | `point_wallets`、`point_ledger` |
| 生成 | `generation_tasks`、`generation_inputs`、`generation_outputs`、`media_assets` |
| 配置 | `prompt_templates`、`styles`、`generation_cost_configs` |
| Provider | `provider_attempts` |
| 订单预留 | `orders` |
| 公开内容 | `shares`、`feedback` |
| 合规审计 | `audit_logs` |

表的用途、完整字段、类型、检查约束、外键和索引以 `02-database-design.md` 为唯一实现来源。

## 已批准字段与类型决策

- `users.display_name`：`varchar(80)`，可空。
- `users.avatar_asset_id`：可空 UUID，指向 `media_assets.id`，`ON DELETE SET NULL`。
- `generation_tasks.retry_count`：非负 `integer`，重试创建新任务，旧任务不覆盖。
- `media_assets.visibility`：`PRIVATE/SHARED/PUBLIC`，默认 `PRIVATE`。
- `provider_attempts.input_tokens`、`output_tokens`：可空 `bigint`，图片模型没有 Token 时为 NULL。
- `provider_attempts.image_count`：可空非负 `integer`。
- `provider_attempts.estimated_cost`：可空非负 `numeric(18,8)`。
- `provider_attempts.currency`：可空 `varchar(3)`；有成本时必须填写。
- 所有主键 UUID；所有时间 UTC `timestamptz`；所有点数 `bigint`；金额最小单位 `bigint`；成本定点 `numeric`。

## 关键外键和删除策略

1. 用户、任务、订单、钱包、点数流水、媒体元数据和审计记录不物理删除；相关历史 FK 默认 `ON DELETE RESTRICT`。
2. `users.avatar_asset_id`、`styles.cover_asset_id` 和邀请码创建者使用 `ON DELETE SET NULL`。
3. 结果/原图只通过 `media_assets.deleted_at` 逻辑删除；生命周期任务删除对象存储二进制但保留元数据，不能级联删除任务或流水。
4. 审计 `entity_id` 是多态字段，不建立伪外键；`actor_id` 可空以保留系统操作。

## 唯一约束和幂等规则

- 邮箱：`user_emails.email`；主邮箱：`user_emails(user_id) WHERE is_primary`。
- 邀请：`invite_codes.code_hash`、`invite_redemptions(invite_code_id, user_id)`。
- 任务：`generation_tasks(user_id, idempotency_key)`。
- 流水：`point_ledger.idempotency_key` 全局唯一；`generation_id` 上 RESERVE 唯一，CONSUME/RELEASE 互斥且最多一条。
- Provider：`provider_attempts(generation_id, attempt_no)`，非空 `(provider_name, request_id)` 唯一。
- 结果：`generation_outputs(generation_id, output_index)`。
- 订单：`orders(user_id, idempotency_key)`，非空 `provider_order_id` 唯一。
- 分享：`shares.slug`、`shares(user_id, idempotency_key)`。
- 反馈：`feedback(user_id, generation_id)`。

API 的 `Idempotency-Key` 只约束同一用户的同一写请求；Points Service 必须为 RESERVE、CONSUME、RELEASE 生成不同的操作级流水键，不能把同一个 API key 写入多条流水。

## 账务和任务不变量

```text
wallet.available_points = Σ point_ledger.available_delta
wallet.reserved_points = Σ point_ledger.reserved_delta
```

- 钱包行锁、流水插入、余额快照更新和 `version` 递增必须在同一事务。
- 余额必须保持非负；禁止任何绕过流水的余额 UPDATE。
- `SUCCEEDED => CONSUMED`，`FAILED => RELEASED`，`NEEDS_REVIEW => RESERVED`；人工结算后为 `NEEDS_REVIEW + CONSUMED` 或 `NEEDS_REVIEW + RELEASED`。生成任务状态只有 `CREATED/QUEUED/PROCESSING/SUCCEEDED/FAILED/NEEDS_REVIEW` 六态，**不使用 `CANCELLED`**（`LEAD-04` 第 4 节；取消归类为 `FAILED + RELEASED` 或 `NEEDS_REVIEW + RESERVED`）。
- 同一生成任务最终只能 CONSUME 或 RELEASE 其中一个，重复 Worker/Webhook/人工复核返回已有结果。
- Provider UNKNOWN 或结果缺失进入 `NEEDS_REVIEW`，不自动扣点、不自动退款。
- `generation_tasks.cost_snapshot` 固化创建时配置；后续配置修改不影响历史任务。

## 迁移顺序

```text
0001_v1_extensions
0002_users_identity
0003_invites
0004_wallets
0005_media_assets
0006_media_reference_fks
0007_styles_and_templates
0008_generation_cost_configs
0009_generation_tasks
0010_generation_io
0011_orders
0012_provider_attempts
0013_point_ledger
0014_shares_feedback
0015_audit_logs
0016_append_only_guards
```

迁移从空库开始；已应用迁移不可编辑，只能追加。`upgrade head` 必须可重复执行。完整 revision 内容见 `02-database-design.md` 第 7 节。

## ENG-01 下一步说明

ENG-01 通过后才可实现 SQLAlchemy/Alembic，且只允许修改：

```text
backend/app/db/base.py
backend/app/db/session.py
backend/app/db/models/*.py
backend/alembic/env.py
backend/alembic/versions/0001_v1_extensions.py ... 0016_append_only_guards.py
backend/tests/db/test_migrations.py
backend/tests/db/test_constraints.py
backend/tests/db/test_points_invariants.py
```

工程步骤：

1. 在 `0001` 启用 `pgcrypto`、`citext`，配置 SQLAlchemy 2.x PostgreSQL 类型。
2. 按上表顺序编写 16 个线性 revision；在 `0006` 添加头像 FK，在 `0013` 添加指向任务/订单的流水 FK。
3. 用 `CheckConstraint` 实现状态、非负余额、点数 delta 和成本字段约束；用 PostgreSQL 部分唯一索引实现发布版本、外部 request ID 和账务终结互斥。
4. 在 `0016` 建立 `point_ledger`、`audit_logs` 的 UPDATE/DELETE 拒绝保护，并确保业务数据库角色无这两类权限。
5. 编写空库升级、重复升级、唯一/FK/check、追加保护、并发预留和账务一致性测试。测试数据必须是合成数据，不读取旧 Drizzle/SQLite。
6. ENG-01 完成后停止，按 `00-index.md` 的回报格式提交改动文件、命令输出和风险；等待技术负责人审核后才能进入 ENG-02/ENG-03。

## 测试要求

至少验证：

- `alembic upgrade head` 在空 PostgreSQL 成功，第二次执行无变化；
- 所有主键、外键、唯一键、CHECK 和部分唯一索引拒绝非法写入；
- 相同用户/幂等键不会创建第二个任务、订单或分享；同一外部 request ID 不会创建第二次 Provider 调用；
- 点数预留、成功消费、失败释放、人工复核和重复回调的流水/钱包/任务三者一致；
- 并发预留不会产生负余额；流水和审计的 UPDATE/DELETE 被拒绝；
- 软删除/过期只影响可见性或对象二进制，不级联删除业务历史。

## 明确不做

本卡不实现 SQLAlchemy、Alembic、Repository、Service、支付网关、价格和产品规则，不修改 API 路径、响应格式、Provider 接口、旧测试版数据库，也不增加支付、社区或推荐功能。

## 尚未确定的问题

1. 生产托管 PostgreSQL 是否允许 `pgcrypto`、`citext` 和触发器；若不允许，ENG-01 开始前必须批准等价替代方案。
2. 用户头像是否永久保留；当前 Schema 允许 `media_assets.expires_at = NULL`，生命周期需产品/隐私负责人确认。
3. `ADJUSTMENT` 的审批角色和 `lifetime_consumed` 统计口径由 `LEAD-04` 交叉审查，本卡不自行决定。
4. 成本配置时间区间暂由 Service + 集成测试防重叠，是否增加 PostgreSQL `EXCLUDE` 约束需单独批准。
5. 是否允许同一任务多个分享链接；当前 Schema 允许，使用 slug、状态和幂等键控制重复写入。

## 审核门禁

只有在技术负责人确认：空库迁移可执行、重复执行无变化、账务流水追加保护有效、任务/流水/外部请求唯一约束可测试、以及上述未决问题有明确处理结论后，才授权 `ENG-01` 开始。完成本卡后必须停止，不得继续执行后续任务。

## 审核结论（2026-09-25）

技术负责人审核结果：**通过**，并据此授权 `ENG-01` 开始。

### 裁决 1：PostgreSQL 扩展与触发器（对应「尚未确定的问题」第 1 项）

- `ENG-01` 只针对本地 `postgres:16-alpine`；
- 允许使用 `pgcrypto`、`citext` 和 PostgreSQL 触发器；
- 生产托管 PostgreSQL 的等价能力与兼容性延后，由部署相关审核与 `LEAD-05` 处理；
- 该未决项**不阻塞** `ENG-01`。

### 裁决 2：`ENG-01` 允许修改的文件

以技术负责人 2026-09-25 授权说明为准：

```text
backend/app/__init__.py
backend/app/config.py
backend/app/db/__init__.py
backend/app/db/base.py
backend/app/db/session.py
backend/app/db/models/*.py
backend/alembic.ini
backend/alembic/env.py
backend/alembic/versions/*.py
backend/tests/db/__init__.py
backend/tests/db/conftest.py
backend/tests/db/test_migrations.py
backend/tests/db/test_constraints.py
backend/tests/db/test_points_invariants.py
backend/requirements.txt
```

约束：

- `backend/app/config.py` 只允许补数据库配置读取，不实现业务逻辑；
- `backend/requirements.txt` 只加入 SQLAlchemy 2.x、Alembic、psycopg、pytest 及数据库测试所需依赖；
- 不得修改 API、Provider、Storage、Points Service 或旧 Drizzle/SQLite 文件；
- 本次**不修改** `docker/compose.yml`。

### 裁决 3：权威来源

不补发独立 `ENG-01` 任务卡。`ENG-01` 的权威来源为：

```text
docs/task-cards/LEAD-02-database-contract.md
docs/architecture/02-database-design.md
技术负责人 2026-09-25 的 ENG-01 授权说明
docs/task-cards/ENGINEERING-STRICT-PROTOCOL.md
```

### 裁决 4：数据库验证路径

只允许 PostgreSQL 单服务：

```text
允许：docker compose --env-file .env.v1 -f docker/compose.yml up -d postgres
允许：docker compose --env-file .env.v1 -f docker/compose.yml stop postgres
禁止：启动 frontend、backend、worker、beat、redis、minio
禁止：down -v、删除命名卷、启动完整 Compose
```
