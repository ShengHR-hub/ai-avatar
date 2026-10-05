# AI Avatar Studio V1.0 REST API 契约

> 版本：V1.0
> 状态：LEAD-03 冻结，等待技术负责人审核
> Canonical 前缀：`/api/v1`
> 认证：HttpOnly Session Cookie
> OpenAPI 基线：`shared/api-contract/openapi.json`

## 1. 契约边界

本文件冻结 REST 路由、请求/响应形状、HTTP 状态码、稳定错误码、认证方式、权限和幂等语义。FastAPI Pydantic Schema 是运行时来源，`shared/api-contract/openapi.json` 是提交到仓库的基线产物；ENG-02 必须从 FastAPI 导出同等内容，不能手写另一套契约。

本轮 LEAD-04 只修订本文件的人读契约；按任务限制不修改 `shared/api-contract/openapi.json`。该生成产物若仍包含旧任务状态或字段命名，必须在后续获授权的 OpenAPI 重新生成步骤中同步，不能作为 LEAD-04 新规则的依据。

所有对外和内部 HTTP 路由都使用 `/api/v1` canonical 前缀。本文没有列出的路由不得在 V1.0 中增加。本文不改变数据库字段、点数规则或生成任务状态机。

## 2. 统一请求与响应

### 2.1 请求追踪

- 客户端可发送 `X-Request-Id`（ASCII，1-128 字符）；服务端校验通过后沿用，否则生成 `req_<uuid>`。
- 所有响应都返回顶层 `request_id`，并通过同名响应头返回。
- 日志只记录 `request_id`、用户 ID、资源 ID 和稳定错误码，不记录 Cookie、令牌、签名 URL、原图或完整 Prompt。

### 2.2 成功响应

单对象或命令响应：

```json
{ "data": {}, "request_id": "req_01J..." }
```

分页响应：

```json
{
  "data": [],
  "page": { "next_cursor": "opaque-cursor", "has_more": true },
  "request_id": "req_01J..."
}
```

`next_cursor` 是不透明字符串；客户端只能原样回传 `cursor`。默认 `limit=20`，允许范围 `1..100`。删除成功返回 `204 No Content`，不得返回 JSON body。

### 2.3 错误响应

```json
{
  "error": { "code": "INSUFFICIENT_POINTS", "message": "可用点数不足", "details": {} },
  "request_id": "req_01J..."
}
```

`message` 仅用于展示，前端只能依赖 `error.code`。`details` 不得包含堆栈、SQL、Provider 原始响应或密钥。

## 3. 认证与会话

- 浏览器认证使用服务端签发的 `session` HttpOnly、Secure、SameSite=Lax Cookie；Cookie 值是 opaque token，服务端只在 Redis 保存会话状态。
- 浏览器不保存或发送 Provider/API 密钥；V1.0 不支持浏览器 Bearer Token。
- 写请求执行 Origin 校验；跨站调用策略由部署配置限定为产品前端域名。
- Provider webhook 使用服务端签名密钥和事件 ID，不属于用户会话。

### 3.1 认证接口

| 方法 | Canonical 路径 | 权限 | 成功 | 幂等 |
|---|---|---|---:|---|
| `POST` | `/api/v1/auth/invites/redeem` | 匿名 | `201` | 必须 |
| `POST` | `/api/v1/auth/email/request` | 已登录 | `202` | 必须 |
| `POST` | `/api/v1/auth/email/verify` | 邮箱令牌或已登录 | `200` | 必须 |
| `GET` | `/api/v1/auth/session` | 已登录 | `200` | 否 |
| `POST` | `/api/v1/auth/logout` | 已登录 | `204` | 必须 |

邀请码兑换请求：`{ "invite_code": "AVTR-ABCD-EFGH", "email": "user@example.com" }`。响应 `data` 为 `{ "user": User, "session": Session, "points": PointBalance }`，同时写入 Session Cookie。邀请码明文不进入日志或数据库。

邮箱请求为 `{ "email": "user@example.com" }`，邮箱确认请求为 `{ "token": "opaque-email-token" }`。邮箱令牌只存 Redis 并带 TTL；邮箱请求即使邮箱不存在也返回统一 `202`，避免枚举。

### 3.2 当前用户接口

| 方法 | Canonical 路径 | 请求体 | 成功 |
|---|---|---|---:|
| `GET` | `/api/v1/me` | 无 | `200` |
| `PATCH` | `/api/v1/me` | `UpdateMeRequest` | `200` |

`UpdateMeRequest` 只允许 `display_name`、`avatar_asset_id`、`locale`、`timezone`。未知字段拒绝；不能通过此接口修改邮箱、状态、点数或权限。

## 4. API 路由表

除特别标注外，所有写请求都必须发送 `Idempotency-Key`。该 Header 为 ASCII、1-128 字符，空白或超长值返回 `IDEMPOTENCY_INVALID`。

### 4.1 账号、用户和点数

| 方法 | 路径 | 说明 | 权限 | 成功 |
|---|---|---|---|---:|
| `POST` | `/api/v1/auth/invites/redeem` | 兑换邀请码、创建/恢复用户和会话 | 匿名 | `201` |
| `POST` | `/api/v1/auth/email/request` | 发送验证邮件 | 已登录 | `202` |
| `POST` | `/api/v1/auth/email/verify` | 消费验证令牌并绑定邮箱 | 令牌/已登录 | `200` |
| `GET` | `/api/v1/auth/session` | 读取当前会话 | 已登录 | `200` |
| `POST` | `/api/v1/auth/logout` | 撤销当前会话 | 已登录 | `204` |
| `GET` | `/api/v1/me` | 当前用户资料和邮箱摘要 | 已登录 | `200` |
| `PATCH` | `/api/v1/me` | 修改允许的资料字段 | 已登录 | `200` |
| `GET` | `/api/v1/me/points` | 点数余额快照 | 已登录 | `200` |
| `GET` | `/api/v1/me/points/ledger` | 点数追加流水，游标分页 | 已登录 | `200` |

余额响应固定包含 `available_points`、`reserved_points`、`lifetime_credited`、`lifetime_consumed`、`updated_at`。客户端不得把余额当作可写资源。

### 4.2 媒体资源

| 方法 | 路径 | 说明 | 权限 | 成功 |
|---|---|---|---|---:|
| `POST` | `/api/v1/assets/uploads` | 创建上传意图和短期上传 URL | 已登录且邮箱已验证 | `201` |
| `POST` | `/api/v1/assets/{asset_id}/complete` | 校验对象并标记上传完成 | 资源所有者 | `200` |
| `GET` | `/api/v1/assets/{asset_id}` | 读取资源元数据 | 所有者或分享可见 | `200` |
| `POST` | `/api/v1/assets/{asset_id}/download-url` | 创建短期下载 URL | 所有者或分享可见 | `200` |
| `DELETE` | `/api/v1/assets/{asset_id}` | 逻辑删除资源 | 资源所有者 | `204` |

上传请求：

```json
{
  "purpose": "SOURCE",
  "filename": "avatar.jpg",
  "mime_type": "image/jpeg",
  "size_bytes": 2457600,
  "sha256": "64-hex-characters"
}
```

完成接口必须验证对象存在、大小、MIME、SHA-256 和图片可解码性；未完成资源不得作为生成输入。

### 4.3 生成任务

| 方法 | 路径 | 说明 | 权限 | 成功 |
|---|---|---|---|---:|
| `POST` | `/api/v1/generations` | 创建异步生成任务并预留点数 | 已登录且邮箱已验证 | `202` |
| `GET` | `/api/v1/generations/{generation_id}` | 读取任务和计费状态 | 任务所有者 | `200` |
| `GET` | `/api/v1/generations/{generation_id}/outputs` | 读取结果列表 | 任务所有者 | `200` |
| `GET` | `/api/v1/me/generations` | 当前用户历史，游标分页 | 已登录 | `200` |
| `POST` | `/api/v1/generations/{generation_id}/retry` | FAILED 任务创建新任务 | 任务所有者 | `202` |

创建请求：

```json
{
  "mode": "PHOTO_PROMPT",
  "style_id": "uuid-or-null",
  "prompt_template_id": "uuid-or-null",
  "prompt": "保持人物特征，生成专业头像",
  "input_asset_ids": ["source-asset-uuid"],
  "output_count": 2
}
```

V1.0 约束：`PHOTO_PROMPT` 必须 1 张 `SOURCE`；`TEXT_PROMPT` 不允许图片；`REFERENCE_IMAGE` 必须 1 张 `SOURCE` 且最多 2 张 `REFERENCE`；`output_count` 固定为 `2`。客户端不能提交点数金额。

创建和重试均返回 `202 Accepted`：

```json
{
  "data": {
    "generation_id": "uuid",
    "status": "QUEUED",
    "billing_status": "RESERVED",
    "reserved_points": 10,
    "requested_count": 2,
    "parent_generation_id": null
  },
  "request_id": "req_01J..."
}
```

任务状态固定为 `CREATED`、`QUEUED`、`PROCESSING`、`SUCCEEDED`、`FAILED`、`NEEDS_REVIEW`；V1.0 不保留生成任务状态 `CANCELLED`。计费状态固定为 `RESERVED`、`CONSUMED`、`RELEASED`。重试只接受 `FAILED`，创建新任务并设置对外字段 `parent_generation_id`，不修改旧任务。该字段映射数据库内部的 `generation_tasks.parent_task_id`，两者表示同一父任务关系。V1.0 不开放用户取消 API；内部 Service/Worker/运营命令不新增公开路径，取消结果按 LEAD-04 归类为 `FAILED + RELEASED` 或 `NEEDS_REVIEW + RESERVED`。

计费真值：未完成人工决定的 `NEEDS_REVIEW` 必须为 `RESERVED`；人工 `CONSUME` 后保持任务状态 `NEEDS_REVIEW` 并变为 `CONSUMED`；人工 `RELEASE` 后保持任务状态 `NEEDS_REVIEW` 并变为 `RELEASED`。人工决策必须记录管理员、理由、证据和请求幂等键；重复相同决策回放原结果，相反决策返回冲突。

### 4.4 分享、下载和反馈

| 方法 | 路径 | 说明 | 权限 | 成功 |
|---|---|---|---|---:|
| `POST` | `/api/v1/shares` | 创建分享链接并设置资源为 `SHARED` | 任务所有者 | `201` |
| `GET` | `/api/v1/shares/{slug}` | 公开读取分享元数据和结果 | 匿名 | `200` |
| `POST` | `/api/v1/feedback` | 提交任务评分/分类/文字反馈 | 任务所有者 | `201` |

分享只能使用当前用户拥有且已 `SUCCEEDED` 的结果；分享读取只返回明确标记为 `SHARED` 且未过期的资源。反馈请求包含 `generation_id`、`rating`（1-5）、`category` 和可选 `content`。

ENG-09 实现口径（不改变路径、请求字段、错误码与状态码，只把上面这句散文落到可实现的行为）：

**分享**

- `POST /shares` 的 `output_asset_ids` 是**被明确选中**的结果图（1 或 2 张，不重复）；
  目标是该任务 `READY` 的 `RESULT` 输出。同任务**未选中**的结果保持 `PRIVATE`，
  匿名读取时是 404 —— 分享一张不会顺带公开另一张；
- 创建分享、写选中集合（`share_assets`）与把选中资产置 `SHARED` 在**同一事务**完成；
  `shares.asset_id` 保留为主资产（`output_index` 最小者）；
- `slug` 由服务端 32 字节随机生成（43 字符 URL-safe），不含内部 ID、路径或存储键；
- 同键同载荷回放同一条分享；同键换了任务或换了选中集合返回 `409 IDEMPOTENCY_CONFLICT`；
  并发由 `uq_shares_user_idempotency` 唯一约束裁决，输家同样得到稳定冲突；
- 匿名读取（`security: []`）**实时**检查状态与期限，并逐个校验资产仍然可用；
  不存在、已撤销、已过期、资产已失效一律 `404 GENERATION_NOT_FOUND`，不区分原因；
- 响应 `data` 形状（`ShareEnvelope.data` 在基线里是未约束 object，因此这里冻结为）：

  ```json
  {"slug": "...", "title": null, "expires_at": null, "created_at": "...",
   "outputs": [{"asset_id": "...", "output_index": 0, "mime_type": "image/png",
                "width": 1024, "height": 1024, "size_bytes": 2048}]}
  ```

  **不含** `generation_id`、所有者、Prompt、`storage_key`、`storage_provider` 或任何内部记录；
- 下载复用既有链路：分享页对每张选中资产调 `POST /assets/{asset_id}/download-url`
  拿短期 URL。`04-ai-provider.md` 第 8 节的可见性规则「`SHARED` 只对有效分享作用域成立」
  由两件事共同保证：`SHARED` 只在创建分享时设置，并在**撤销、分享过期、资产失效**
  三条路径上按「是否还有有效分享」重算，没有有效分享就恢复 `PRIVATE`；
- 撤销是**内部行为**：V1.0 不新增公开撤销接口，由 Service 与测试验证；
- 分享默认有效期 7 天（契约第 12 节把该值列为未决项，这是本卡的取值）。

**反馈**

- 只接受**任务所有者**对 `SUCCEEDED`/`FAILED` 任务的反馈；`NEEDS_REVIEW` 结果未定，
  不是可反馈终态；
- 他人任务与不存在的任务返回 `404 GENERATION_NOT_FOUND`（不泄露存在性）；
  自己的任务但不是可反馈终态返回 `422 VALIDATION_ERROR`，`details.fields = ["generation_id"]`；
- `category` 必填 ≤64（迁移 0021 已把列由 32 扩宽到 64），`content` 可选 ≤2000；
  长度上限同时写在 OpenAPI Schema 与运行时校验里；
- 每用户每任务一条（`uq_feedback_user_generation`）。同键同载荷回放第一次反馈；
  同键跨任务、同键换了内容、或换了新键再提交同一任务，一律 `409 IDEMPOTENCY_CONFLICT`；
- **已知声明遗漏（记录，不改基线）**：本 operation 在基线里的响应集合没有 `409`，
  而幂等冲突/处理中在全局语义下是 `409`（第 9 节）。实现按全局语义返回 `409`，
  待技术负责人裁决是否补进基线。

### 4.5 健康检查和内部边界

| 方法 | 路径 | 说明 | 权限 | 成功 |
|---|---|---|---|---:|
| `GET` | `/api/v1/health/live` | 进程存活，不访问外部依赖 | 匿名/内部 | `200` |
| `GET` | `/api/v1/health/ready` | PostgreSQL、Redis、对象存储、Worker 心跳、点数成本配置 | 内部 | `200`/`503` |
| `POST` | `/api/v1/internal/providers/{provider_name}/webhook` | Provider 回调入口 | Provider 签名 | `202` |
| `GET` | `/api/v1/admin/generations` | 管理员任务检索 | Admin | `200` |
| `GET` | `/api/v1/admin/generations/{generation_id}` | 管理员任务详情 | Admin | `200` |
| `POST` | `/api/v1/admin/generations/{generation_id}/review-resolution` | 结算 `NEEDS_REVIEW` | Admin | `200` |
| `GET` | `/api/v1/admin/point-ledger` | 管理员流水检索 | Admin | `200` |
| `GET` | `/api/v1/admin/provider-attempts` | Provider 调用检索 | Admin | `200` |

Webhook 必须验签、防重放、要求 `Idempotency-Key`（由 Provider event ID 派生）并只投递内部 Service；不得直接改点数或任务状态。Admin/内部路径不是前端业务 API。

`/api/v1/health/ready` 逐项检查以下五项，**任一项未就绪即返回 `503 NOT_READY`**，
响应 `error.details.dependencies` 为逐项 `{name, ready, detail}` 列表：

```text
postgresql          SELECT 1，能往返即就绪
redis               一次连通性往返；键不存在不算故障
object_storage      桶级可达性（桶不存在 / 无权限都算未就绪）
worker_heartbeat    最近一次心跳时间戳，阈值 60 秒
generation_costs    generation_cost_configs 是否已有可用激活价格
```

`generation_costs` 是**部署步骤自检**，不是依赖探针：迁移只建 `generation_cost_configs`
表、**不填任何价格**，新环境少跑一次定价初始化时，创建生成任务会拿到
`503 NOT_READY`（`details.reason = "cost_config_missing"`）。放进 `/ready` 是为了把
「部署少了一步」在用户第一次失败之前变成运维能直接看见的信号。判定复用创建任务时
**同一套取值规则**（精确型号优先、`NULL` 兜底），因此不会出现「健康检查绿了但生成仍报缺配置」。
应当能定价的模式集合为 `TEXT_PROMPT`、`PHOTO_PROMPT`，以及开启 `ENABLE_REFERENCE_IMAGE`
时的 `REFERENCE_IMAGE`；初始化命令是 `python -m app.bootstrap.cost_configs --price MODE=<点数>`
（**没有默认价格**，必须由部署方显式给出，见 `02-database-design.md` 第 3.2 节与 `docker/README.md`）。

探针抛出的异常一律视为未就绪，且异常文本不进入响应（可能含连接串等敏感信息）。

### 4.6 参考图片找灵感（图片理解）

| 方法 | 路径 | 说明 | 权限 | 成功 |
|---|---|---|---|---:|
| `POST` | `/api/v1/analyses` | 读一张自己的参考图，换回一段可编辑的画面描述 | 已登录且邮箱已验证 | `200` |

这条能力**不是生成**：不创建任务、不占点数、不进任务状态机、不产出图片，也不复用
`REFERENCE_IMAGE` 模式。它只把用户自己的参考图交给视觉模型（`AI_VISION_MODEL`），
取回一段**近似画面描述**；用户改完这段文字后，自己再发起一次普通的文字创作或图片改造。
因此它**不承诺**恢复原作者的提示词，也**不承诺**复刻参考图。

请求：

```json
{ "asset_id": "uuid" }
```

`asset_id` 必须是调用者自己的、`purpose=REFERENCE` 且 `status=READY` 的资产；
先判存在再判归属，用途或状态不符一律按第 7 节的既有错误码拒绝，不新增错误码。

成功响应 `data`：

```json
{
  "asset_id": "uuid",
  "description": "一段中文画面描述",
  "model": "实际使用的视觉型号",
  "from_cache": false
}
```

`from_cache=true` 表示这次**没有出网、没有再次付费**，结果是同一张图在缓存有效期内
（6 小时）复用的上一次成功描述。

边界与成本口径：

- 图片字节只在服务端内存中过一遍：**不落库、不写日志、不进响应**；送模型前先重编码为
  长边 ≤ 1024 的 JPEG，重编码同时剥离 EXIF 一类的相机元数据；
- 描述只在 Redis 里存活 6 小时，不入库，因此**没有** `GET/DELETE /analyses/{id}`
  这类端点，也没有新增迁移；图片本身继续走 `REFERENCE` 资产的 7 天生命周期；
- 描述文本可能带出图里的私人信息：命中邮箱、手机号、网址、本地路径、内部参数或成串编号时
  **整段不下发**，按 `PROVIDER_BAD_GATEWAY` 返回；
- **每日配额的事实来源是 PostgreSQL，不是 Redis。** 每一次「已确定要送出」的视觉调用在
  出网**之前**就写一行 `analysis_dispatches`（`02-database-design.md` 第 3.8 节），
  `usage_day` 为 UTC 日历日；判额读的是这张表的行数。Redis 只负责两件事：描述缓存回放，
  以及同一张图的单飞锁。Redis 重启、驱逐或 `FLUSHALL` 既不会让当日额度归零，也不会导致
  同一张图重复付费——额度是花钱买来的，不能放在会被清空的存储上；
- 三道付费闸门：同一张图的缓存回放（`from_cache=true`）、同一张图并发只放行一次
  （`IDEMPOTENCY_IN_PROGRESS`）、每用户与全局每日配额（`RATE_LIMITED` + `Retry-After`）。
  配额数的是**出网尝试次数**，失败不退还——该供应商没有幂等键，超时无法证明未受理；
  登记行默认 `outcome=UNKNOWN` 并继续占用额度，只有拿到明确成功/明确失败才收敛，
  两种终态同样不退还；
- 闸门依赖不可用时按 `NOT_READY` 拒绝，绝不「看不见额度就继续出网」；配额真值所在的
  PostgreSQL 不可用同样落到 `NOT_READY`（fail-closed），而不是退回只看缓存；
- 能力关闭（`ENABLE_IMAGE_ANALYSIS=false`）时返回 `FEATURE_NOT_AVAILABLE`，
  连缓存读取也不放行。

「上一次线当天额度会重置一次」是这条能力的已知一次性代价：0020 之前的计数只在 Redis 里，
无法可信复原，因此新表从空开始、不做伪造回填（`02-database-design.md` 第 7 节）。

## 5. 核心 Schema

以下为 Pydantic/OpenAPI 字段冻结；所有 ID 为 UUID 字符串，时间为 UTC RFC 3339。

```text
User {
  id: uuid
  display_name: string|null, max 80
  avatar_asset_id: uuid|null
  status: ACTIVE|SUSPENDED|DELETED
  locale: string, max 16
  timezone: string, max 64
  primary_email: string|null
  email_verified: boolean
  created_at: datetime
  updated_at: datetime
}

Session { session_id: string; user_id: uuid; email_verified: boolean; expires_at: datetime }

PointBalance {
  available_points: integer >= 0
  reserved_points: integer >= 0
  lifetime_credited: integer >= 0
  lifetime_consumed: integer >= 0
  updated_at: datetime
}

PointLedgerEntry {
  id: uuid
  entry_type: CREDIT|RESERVE|CONSUME|RELEASE|ADJUSTMENT
  available_delta: integer
  reserved_delta: integer
  amount: integer
  generation_id: uuid|null
  reason: string
  created_at: datetime
}

ADJUSTMENT 只允许管理员 Service 产生，必须携带 `adjustment_kind`（`POSITIVE_CREDIT` 或 `NEGATIVE_CONSUMPTION`）、工单/理由、前后钱包快照摘要和审计上下文；正向补发计入 `lifetime_credited`，负向消费计入 `lifetime_consumed`。ADJUSTMENT 不替代任务 `CONSUME`/`RELEASE`，历史流水不可覆盖或删除，重复请求按幂等键回放。

MediaAsset {
  id: uuid
  asset_type: ORIGINAL|REFERENCE|RESULT
  purpose: SOURCE|REFERENCE|RESULT
  status: PENDING_UPLOAD|READY|DELETED|EXPIRED
  visibility: PRIVATE|SHARED|PUBLIC
  mime_type: string
  size_bytes: integer
  width: integer|null
  height: integer|null
  sha256: string
  expires_at: datetime|null
  created_at: datetime
}

Generation {
  id: uuid
  parent_generation_id: uuid|null
  mode: PHOTO_PROMPT|TEXT_PROMPT|REFERENCE_IMAGE
  status: CREATED|QUEUED|PROCESSING|SUCCEEDED|FAILED|NEEDS_REVIEW
  billing_status: RESERVED|CONSUMED|RELEASED
  prompt: string
  requested_count: 2
  reserved_points: integer
  failure_code: string|null
  created_at: datetime
  updated_at: datetime
}

`parent_generation_id` 是 API DTO 名称；Repository 必须将其映射为数据库 `generation_tasks.parent_task_id`。工程 Agent 不得新增第二个父任务字段，也不得把两个字段解释为不同关系。

Share { slug: string; title: string|null; generation_id: uuid; output_asset_ids: uuid[]; expires_at: datetime|null; created_at: datetime }
```

## 6. HTTP 状态码规范

| 状态码 | 使用场景 |
|---:|---|
| `200` | 读取、资料更新、邮箱验证、上传完成、下载 URL、管理结算 |
| `201` | 邀请兑换创建身份、上传意图、分享、反馈 |
| `202` | 异步生成创建、生成重试、邮箱发送、Provider webhook 接收 |
| `204` | 登出、媒体逻辑删除 |
| `400` | 缺少/非法请求头、幂等键格式、请求语义无法解析 |
| `401` | 未登录、Session 无效或已过期 |
| `403` | 已登录但无权限、邮箱未验证、账号被暂停 |
| `404` | 资源不存在或对当前主体不可见 |
| `409` | 状态冲突、重复兑换、点数不足、幂等冲突/处理中 |
| `413` | 上传超过 V1.0 大小上限 |
| `415` | 不支持的媒体类型 |
| `422` | Pydantic 字段校验、邀请码格式、业务输入不合法 |
| `429` | 限流，响应带 `Retry-After` |
| `500` | 未分类内部错误 |
| `502` | Provider 返回不可用/错误网关结果 |
| `503` | 依赖不可用或服务未就绪 |

## 7. 稳定错误码及映射

| 错误码 | HTTP | 说明 |
|---|---:|---|
| `AUTH_REQUIRED` | 401 | 缺少有效 Session |
| `SESSION_INVALID` | 401 | Session 不存在、过期或撤销 |
| `EMAIL_NOT_VERIFIED` | 403 | 需要已验证邮箱 |
| `ACCOUNT_SUSPENDED` | 403 | 账号已暂停 |
| `FORBIDDEN` | 403 | 资源归属或角色不允许 |
| `INVITE_INVALID` | 422 | 邀请码格式错误、过期、禁用或不存在 |
| `INVITE_ALREADY_USED` | 409 | 当前用户已兑换或次数耗尽 |
| `EMAIL_TOKEN_INVALID` | 422 | 邮箱验证令牌无效或过期 |
| `EMAIL_ALREADY_VERIFIED` | 409 | 当前邮箱已验证 |
| `EMAIL_IN_USE` | 409 | 邮箱已绑定其他用户 |
| `VALIDATION_ERROR` | 422 | 请求字段不符合 Schema |
| `ASSET_NOT_FOUND` | 404 | 资源不存在或已逻辑删除 |
| `ASSET_FORBIDDEN` | 403 | 无权读取或操作资源 |
| `ASSET_INVALID` | 422 | 哈希、尺寸、解码或用途校验失败 |
| `ASSET_NOT_READY` | 409 | 上传尚未完成 |
| `UPLOAD_TOO_LARGE` | 413 | 超出文件大小上限 |
| `MEDIA_TYPE_UNSUPPORTED` | 415 | MIME 不在允许集合 |
| `GENERATION_NOT_FOUND` | 404 | 任务不存在或不属于当前用户 |
| `GENERATION_NOT_RETRYABLE` | 409 | 任务不是 `FAILED` |
| `GENERATION_NOT_SHAREABLE` | 409 | 任务未成功或结果不可分享 |
| `INSUFFICIENT_POINTS` | 409 | 可用点数不足，未创建任务 |
| `FEATURE_NOT_AVAILABLE` | 422 | 当前 V1.0 或 Feature Flag 未开放 |
| `IDEMPOTENCY_REQUIRED` | 400 | 写请求缺少 Header |
| `IDEMPOTENCY_INVALID` | 400 | 幂等键格式非法 |
| `IDEMPOTENCY_CONFLICT` | 409 | 同一作用域/键对应不同请求体 |
| `IDEMPOTENCY_IN_PROGRESS` | 409 | 相同请求仍在另一请求处理中 |
| `RATE_LIMITED` | 429 | 触发限流 |
| `PROVIDER_UNAVAILABLE` | 503 | Provider 暂不可用 |
| `PROVIDER_BAD_GATEWAY` | 502 | Provider 响应无法归类或网关错误 |
| `GENERATION_FAILED` | 502 | 任务明确失败 |
| `WEBHOOK_INVALID` | 401 | Provider 签名或事件校验失败 |
| `NOT_READY` | 503 | 依赖或 Worker 未就绪 |
| `INTERNAL_ERROR` | 500 | 未分类内部错误 |

同一错误码在所有路由保持相同 HTTP 映射；异常文本、数据库错误和 Provider 原始码不得直接暴露。

## 8. 认证与权限矩阵

| 能力 | 匿名 | 已登录未验证 | 已登录已验证 | Admin | Provider/Internal |
|---|---|---|---|---|---|
| 兑换邀请码 | 允许 | 允许 | 允许（按规则） | 允许 | 禁止 |
| 请求/确认邮箱验证 | 否/令牌例外 | 允许 | 已验证时返回冲突 | 可审计读取 | 禁止 |
| 读取/更新 `/me` | 否 | 允许 | 允许 | 管理边界另行授权 | 禁止 |
| 读取点数/流水 | 否 | 允许 | 允许 | 管理接口另行授权 | 禁止 |
| 上传/完成/删除媒体 | 否 | 禁止 | 允许 | 不代替用户操作 | 禁止 |
| 参考图找灵感（图片理解） | 否 | 禁止 | 允许 | 不代替用户操作 | 禁止 |
| 创建生成/重试 | 否 | 禁止 | 允许 | 不代替用户操作 | 禁止 |
| 读取自己的任务 | 否 | 允许已拥有记录 | 允许 | 管理接口允许 | 禁止 |
| 读取公开分享 | 允许 | 允许 | 允许 | 允许 | 禁止 |
| 创建分享/反馈 | 否 | 禁止 | 允许 | 不代替用户 | 禁止 |
| 健康检查 | live | live | live | ready | ready |
| Provider webhook | 禁止 | 禁止 | 禁止 | 禁止 | 签名允许 |
| 管理任务/人工结算 | 禁止 | 禁止 | 禁止 | 允许 | 禁止 |

资源不可见和无权访问默认都可返回 `404`，避免泄露其他用户资源是否存在。

## 9. 幂等键行为

### 9.1 必须携带的写接口

```text
POST /api/v1/auth/invites/redeem
POST /api/v1/auth/email/request
POST /api/v1/auth/email/verify
POST /api/v1/auth/logout
PATCH /api/v1/me
POST /api/v1/assets/uploads
POST /api/v1/assets/{asset_id}/complete
POST /api/v1/assets/{asset_id}/download-url
DELETE /api/v1/assets/{asset_id}
POST /api/v1/analyses
POST /api/v1/generations
POST /api/v1/generations/{generation_id}/retry
POST /api/v1/shares
POST /api/v1/feedback
POST /api/v1/internal/providers/{provider_name}/webhook
POST /api/v1/admin/generations/{generation_id}/review-resolution
```

服务端计算 `request_fingerprint = SHA-256(method + canonical_path + normalized_json_body)`，不记录原始 body。作用域为 `user_id + method + path + key`；邀请兑换使用 `invite_code_hash`；Webhook 使用 `provider_name + provider_event_id`；管理请求使用 `admin_user_id + method + path + key`。

重复规则：

1. 同一作用域、同一 Key、同一 fingerprint 且首次已完成：原样回放第一次状态/body，不重复写库、发点、投递任务或发送副作用。
2. 同一 Key 但 fingerprint 不同：`409 IDEMPOTENCY_CONFLICT`，不得执行第二次请求。
3. 首次请求仍在事务或异步入队中：`409 IDEMPOTENCY_IN_PROGRESS`，带 `Retry-After`；客户端重试后得到第一次结果。
4. 事务未提交即返回服务器错误时允许安全重试；事务已提交但响应丢失时必须通过幂等记录回放。
5. 幂等记录 TTL 由 ENG-02/业务 Service 配置，但不得短于业务副作用可重放窗口。

## 10. OpenAPI 生成规则

1. ENG-02 在 `backend/app/main.py` 创建 FastAPI 应用、统一 `/api/v1` router、请求 ID 中间件和统一异常处理器，并通过 `app.openapi()` 导出 `shared/api-contract/openapi.json`。
2. Pydantic model 是请求/响应唯一来源；路径参数、查询参数、Header、Cookie、枚举和错误响应必须显式声明。
3. 每个 operation 必须有 `operationId`、tags、summary、请求体、成功响应、稳定错误响应；写接口声明 `Idempotency-Key` Header 和 Cookie security scheme。
4. 导出稳定排序，并在 CI 以 JSON 规范化比较运行时导出与基线；禁止手工修改生成产物绕过 Schema。
5. 前端只从该 OpenAPI 生成 TypeScript 类型/客户端；内部 admin/webhook 路径保留并标记 `x-internal: true`，默认不暴露给前端业务服务。

## 11. ENG-02、ENG-03、ENG-08 下一步说明

### ENG-02：FastAPI 基础服务、配置、健康检查和 OpenAPI

1. 创建 FastAPI 应用、`/api/v1` 根路由、请求 ID 中间件、统一异常处理器和 Session Cookie security scheme。
2. 创建公共 Pydantic Schema/响应包装器，覆盖本文及 `shared/api-contract/openapi.json` 的路径、参数和错误响应。
3. 实现 `/api/v1/health/live` 与 `/api/v1/health/ready`；ready 检查 PostgreSQL、Redis、对象存储、Worker 心跳和点数成本配置初始化（`generation_cost_configs` 缺当前应定价的模式时返回 `503 NOT_READY`），失败返回 `503 NOT_READY`。
4. 对所有写路由注册 `Idempotency-Key` 校验和统一异常映射；业务 Service 接入时保留相同语义。
5. 导出规范化 OpenAPI，并增加契约测试：canonical 前缀、写接口 Header、生成 `202`、错误响应形状。

### ENG-03：用户、邀请码、邮箱绑定和 Session

1. 实现 auth 与 me 路由和 Service/Repository 分层；不得保留 `/api/v1/invites/redeem` 别名。
2. 实现 Redis Session 和邮箱令牌 TTL；Cookie 使用 HttpOnly/Secure/SameSite=Lax，日志不记录令牌；邮箱请求统一 `202` 防枚举。
3. 兑换事务原子完成邀请码校验、用户创建/恢复、兑换记录、`CREDIT` 流水和 Session；重复请求通过幂等记录及唯一约束回放。
4. `/api/v1/me` 仅允许本文资料字段；邮箱占用返回 `EMAIL_IN_USE`，未验证操作返回 `EMAIL_NOT_VERIFIED`。
5. 覆盖有效/无效/过期/重复邀请码、邮箱令牌过期、邮箱占用、Session 过期、重复兑换和请求体冲突测试。

### ENG-08：Next.js 前端生成工作流和用户页面

1. 使用 `shared/api-contract/openapi.json` 生成 `frontend/generated/` 客户端和类型，禁止手写后端 DTO 或 URL。
2. 在 `frontend/services/` 统一发送 `credentials: include`、`X-Request-Id` 和写操作 `Idempotency-Key`，映射稳定 `error.code`。
3. 生成工作流消费 `POST /api/v1/generations` 的 `202`，轮询任务和 outputs；刷新页面后从服务端恢复并识别 `NEEDS_REVIEW`、`FAILED`、`RELEASED`。
4. 上传必须完成并确认 `READY` 后才能生成；下载只使用后端短期 URL；余额和任务状态以服务端为准。
5. 增加重复提交复用同一幂等键、401、429 和固定 `output_count=2` 的客户端契约检查。

## 12. 尚未确定的问题

1. Session TTL、滑动续期、最大并发 Session 和 Redis key 前缀。
2. 邮件供应商、令牌有效期、重发频率和失败重试策略。
3. 图片 MIME 集合、单文件大小/像素上限和病毒扫描方式。
4. 分享默认有效期、公开读取限流、slug 规则和后续撤销接口。
5. 幂等记录精确 TTL、存储表/Redis 选型和跨部署版本兼容。
6. CORS 允许来源、生产域名和是否增加 CSRF token；当前冻结为 SameSite=Lax + Origin 校验。
7. Admin 角色来源、内部网络边界和 webhook 签名算法/密钥轮换。
8. ready 的 Worker 心跳阈值以及对象存储探针策略。
9. Provider 错误到稳定错误码的最终映射；不得改变本文件错误码集合。

## 13. 明确不做

- 不实现 FastAPI Router、Service、Repository、前端页面或 OpenAPI 生成代码；
- 不修改数据库字段、迁移、点数规则或任务状态机；
- 不增加支付、取消生成、社交评论、批量生成、Bearer Token、密码登录或未批准的管理 API；
- 不提交 Git，不授权 ENG-02/ENG-03/ENG-08 在技术负责人审核前继续执行。
