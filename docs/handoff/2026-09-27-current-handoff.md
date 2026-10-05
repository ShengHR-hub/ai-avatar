# AI Avatar Studio V1.0 总控交接文档

> 文档版本：2026-10-02
> 项目目录：D:\wordbuddy\ai-avatar
> 用途：总控会话切换、上下文压缩、Agent 更换后的唯一进度依据。
> 当前结论：ENG-00 至 ENG-08 已完成并放行；下一张工作卡是 ENG-09。
> 当前状态：ENG-09 尚未开始。本文件记录事实和边界，不代替总控最终审核。

## 1. 新会话先读什么

依次阅读：本文件、docs/task-cards/00-index.md、docs/task-cards/ENGINEERING-STRICT-PROTOCOL.md、docs/architecture/01-overview.md 至 09-repository-boundary.md、docs/product/00-product-direction.md、shared/api-contract/openapi.json，以及 ENG-09 相关模型、StorageAdapter、Celery、前端生成 SDK 和测试。代码、旧报告和本文件不一致时先停线记录差异。

## 2. 角色和权限边界

用户当前主会话是总控；新 Agent 是“工程执行 Agent / 仅实现明确授权的任务”。

不得自行宣布卡片放行或授权下一卡；不得改变已放行卡片、状态机、点数、Provider、Storage、认证、OpenAPI 或数据库契约；不得迁移共享数据库；不得 git reset、checkout、clean、stash、全仓 stage/commit/push；不得使用真实 Provider、真实 API Key、真实用户图片或新增付费调用；不得操作其他项目容器、卷、进程和端口。完成当前卡片后必须报告并停线。

## 3. 产品主线（不可回退）

V1.0 是任意类型头像创作和个人作品展示，不再以“真人照片转绘成像本人”为中心。人物、虚构角色、宠物、玩偶、风景、背影、抽象、手绘都允许。

三条能力：

| 能力 | 状态 | 语义 |
|---|---|---|
| 文字创作 | 可用 | TEXT_PROMPT，不上传图片，每次两张 |
| 改造我的图片 | 可用 | PHOTO_PROMPT + 恰好一张 SOURCE，题材不限于人像 |
| 参考图片找灵感 | 已接通 | 视觉模型生成可编辑描述，再由用户自行生成；不是恢复原 Prompt，也不是 REFERENCE_IMAGE 条件生成 |

用户已接受浅银灰、高透明 Apple 毛玻璃、真实作品主视觉、简洁高级但功能完整的前端。不得重做首页背景、毛玻璃、作品展示、灵感区或三入口。首页作品只能使用本站真实产物并附实际 Prompt、来源和授权；ex/ 小红书图、第三方摄影、私人测试图不能冒充作品。

## 4. 已完成历史

- LEAD-01 至 LEAD-07：架构、数据库、REST/OpenAPI、账务/状态机、Provider/Storage、测试策略和阶段审核完成。
- ENG-00 至 ENG-04：工程骨架、迁移、FastAPI、认证、点数账务完成并验证。
- ENG-05：StorageAdapter、Memory/MinIO、上传/完成校验、所有权/可见性、资产 API、0019 生命周期迁移；仅 JPEG/PNG/WebP 静态图，拒绝 SVG/GIF/动画/损坏/polyglot；SOURCE/REFERENCE 7 天、RESULT 30 天；签名上传最多 10 分钟、下载最多 5 分钟；桶私有。
- ENG-06：Generation Service、Celery、Redis 事件去重、Webhook 归一、恢复和 MockProvider 完成。状态为 CREATED -> QUEUED -> PROCESSING -> SUCCEEDED/FAILED/NEEDS_REVIEW；成功消费、明确失败释放、未知结果保留预留；未知结果不盲目重试。
- ENG-07：Quanzil gpt-image-2.5-flare 同步适配器和脱敏完成；真实两图闭环只有一次：n=2、HTTP 200、34.2 秒、任务 SUCCEEDED + CONSUMED、两张 1248x1248 PNG READY、流水 RESERVE -> CONSUME、钱包 100 -> 60、usage input=2124/output=458/image_count=2。成本未确认，total_usage 未变化，estimated_cost/currency 必须 NULL；真实 Provider 已禁止继续调用。
- ENG-08：Next.js 前端、浅银灰视觉、首页真实作品和 Prompt、文字/图片改造/参考图找灵感、上传、幂等、轮询恢复、短期下载、点数显示、分析 Guard/缓存/每日配额、/me/points、/me/points/ledger、0020_analysis_dispatches、前端防双击均已完成并由总控放行。参考图流程在隔离环境走通，未新增真实 Provider 调用。

最后一次门禁证据：

    backend 非 DB 1355 passed
    backend tests/db 93 passed
    frontend Vitest 126 passed / 16 files
    frontend tsc --noEmit 通过
    frontend next build 通过，7 条路由
    OpenAPI 运行时一致性 45 passed
    compileall 通过，git diff --check 通过

## 5. 当前仓库和环境

- HEAD：d4b5bfe docs: clean architecture markdown formatting。
- 工作树有既有 staged/unstaged/untracked 内容，包括 backend、frontend、shared、docker、docs 和 pytest 临时目录；不得清理。
- 最近检查本项目端口无监听；Docker Desktop 进程可能存在，不代表本项目容器运行。启动前先查端口/容器，只启动本项目授权服务。
- 上轮报告：本项目 PostgreSQL/Redis/MinIO/Worker/Frontend 已停止，其他项目容器未碰。
- 共享 ai_avatar 上次核验仍为 0016_append_only_guards，未迁移到 0020；共享库迁移必须单独授权。
- 隔离库曾迁移到 0020 并测试后清理；不能把隔离库当成共享库。
- ENG-07 真实图片只读在仓库外 D:/tmp/eng07x_artifacts/，不得复制进仓库。
- requirements.txt 声明 pytest>=8.3,<9.0；历史环境曾用 pytest 9.0.3，报告需注明版本差异。

## 6. 冻结业务不变量

- 每次生成 output_count 固定为 2。
- TEXT_PROMPT 不带输入图；PHOTO_PROMPT 必须恰好一张 SOURCE；REFERENCE_IMAGE 受开关控制且当前适配器不支持。
- 点数先预留，成功消费，明确失败释放，未知结果保留预留。
- 所有幂等写接口必须带 Idempotency-Key；重复请求只能回放或稳定冲突。
- 业务层只依赖 StorageAdapter；浏览器不能选择 storage key、bucket、visibility 或 URL 有效期。
- PRIVATE 不能匿名读取；SHARED 只能在有效分享作用域内读取；不得泄漏 API Key、Cookie、Prompt 全文、图片 Base64、签名 URL、Provider 原文或个人信息。

## 7. 当前卡片：ENG-09

ENG-09 范围是分享、下载、反馈和生命周期清理。下载基础能力已由 ENG-05/08 实现，不要重写下载链路。

### 7.1 冻结接口

shared/api-contract/openapi.json 已声明，不能修改路径、字段、错误码和状态码：

| 方法 | 路径 | operationId | 权限 |
|---|---|---|---|
| POST | /api/v1/shares | createShare | 登录、Idempotency-Key、201 |
| GET | /api/v1/shares/{slug} | getShare | 匿名、200/404 |
| POST | /api/v1/feedback | createFeedback | 登录、Idempotency-Key、201 |

分享使用 ShareEnvelope；反馈使用普通 Envelope。分享字段为 generation_id、output_asset_ids、可选 title；反馈字段为 generation_id、rating、category、可选 content。以生成客户端类型为准。

### 7.2 已有模型，不得新建重复表

backend/app/db/models/engagement.py 已有：

- shares：slug 唯一；user_id、generation_id、asset_id、title、status ACTIVE/REVOKED/EXPIRED、expires_at、idempotency_key、revoked_at；(user_id, idempotency_key) 唯一。
- feedback：user_id、generation_id、rating 1-5、category、content、status OPEN/REVIEWED/RESOLVED；(user_id, generation_id) 唯一。

### 7.3 必须实现的行为

分享：

1. 只允许当前用户拥有的 SUCCEEDED 任务；目标资产必须是该任务的 RESULT、READY、且属于输出集合。
2. 创建时把目标媒体 visibility 改为 SHARED；slug 不可预测且不含内部 ID、路径或 storage key。
3. 相同幂等键回放同一分享；同键不同载荷返回稳定冲突。
4. 匿名读取只返回有效、未过期、ACTIVE 的分享和允许公开的结果；撤销/过期/不存在按契约 404，不泄漏所有者、Prompt 或存储细节。
5. 撤销/过期只改变分享和可见性/状态，不物理删除任务、流水、审计或 Provider 历史。

反馈：

1. 只允许任务所有者；任务必须是可反馈终态；他人/不存在任务拒绝。
2. 校验 rating、category、content 长度；唯一约束和幂等冲突按既有错误语义。
3. 不返回内部审计、Prompt、Provider 原文或点数细节。
4. Router 只处理 HTTP、Schema、认证和调用；业务规则在 Service，Repository 不自行 commit。

生命周期：

1. 按 media_assets.expires_at 批量选择到期对象；先 StorageAdapter.delete/cleanup，确认成功或对象不存在后再标记 EXPIRED/DELETED。
2. 删除失败不能提前标记，允许安全重试；delete 对不存在 key 幂等成功。
3. 保留数据库历史，不级联删除任务、账务、分享、反馈或审计。
4. 按现有 Celery Beat/Worker 模式注册；Beat 只投递任务名，业务逻辑放 Service。
5. 批量有界、可重试、幂等；报告要说明批量大小、重试和过期分享如何收敛。

### 7.4 默认修改边界

先核实精确白名单，预期只涉及：

    backend/app/api/shares/**
    backend/app/api/feedback/**
    backend/app/schemas/engagement.py（若确有缺口）
    backend/app/services/shares/**
    backend/app/services/feedback/**
    backend/app/services/assets/**（仅复用生命周期服务）
    backend/app/repositories/**（仅相关查询）
    backend/app/tasks/**（仅生命周期注册）
    frontend/services/**、frontend/components/**、frontend/app/**（只接入实际分享/反馈）
    backend/tests/**、frontend/tests/**
    docs/handoff/**

shared/api-contract/openapi.json、Alembic 迁移、账务核心、Provider、已完成视觉和 Dockerfile 不在默认范围。发现契约缺口先停线，不自行补接口/迁移。

### 7.5 测试门禁

必须覆盖分享所有权、SUCCEEDED/READY、幂等、slug 脱敏、匿名读取、撤销/过期 404、SHARED visibility；反馈所有权、终态、字段边界、唯一键/幂等；生命周期成功删除、对象不存在、存储失败重试、批量边界、删除确认后再改状态、Beat 注册。

只用 Mock/Memory/隔离 PostgreSQL/隔离 MinIO，不得启用真实 Provider。报告至少给出 backend 非 DB、tests/db、compileall、git diff --check、OpenAPI 一致性和（若改前端）Vitest/typecheck/build。ENG-09 完成后必须停线，不自动进入 ENG-10。

## 8. 2026-10-02 批次：ENG-09 实现执行记录（新增事实，不覆盖上面任何旧结论）

> 本节由本轮工程执行 Agent 追加，供总控统一验收。§1–§7 是授权前的快照，仍然有效；
> 下面只**替代**这一条旧结论：`ENG-09：未开始，等待新 Agent 执行`。
> 编号说明：本节插在原先的 §8 之前，原 §8/§9/§10 顺延为 §9/§10/§11，内容未改。

### 8.1 结论摘要

`ENG-09`（分享、反馈与生命周期清理）**已按授权范围实现完成**，实现覆盖迁移、后端、
生命周期、前端与验证。**未提交 Git，未调用真实 Provider，未使用真实凭据，未迁移共享 `ai_avatar`。**
正式放行由总控裁决，本节不自行宣布通过，也未开始 `ENG-10`。

### 8.2 改动清单

**新增**

```text
backend/alembic/versions/0021_share_assets_and_feedback_category.py
backend/app/repositories/engagement.py          分享/反馈数据访问（双聚合同文件）
backend/app/schemas/engagement.py               请求/响应 Schema
backend/app/services/engagement_support.py      幂等登记（audit_logs）+ 键摘要/指纹
backend/app/services/shares/{__init__,service,lifecycle}.py
backend/app/services/feedback/{__init__,service}.py
backend/app/api/shares/{__init__,router}.py
backend/app/api/feedback/{__init__,router}.py
backend/tests/services/{test_shares,test_feedback,test_lifecycle_cleanup}.py
backend/tests/api/{test_share_endpoints,test_feedback_endpoints}.py
backend/tests/db/test_share_assets_migration.py
frontend/app/s/[slug]/page.tsx
frontend/components/{SharePanel,FeedbackPanel,SharePage}.tsx
frontend/tests/share.test.tsx
```

**修改**（只做本卡必要装配）

```text
backend/app/api/router.py                 注册 shares / feedback 两个 router
backend/app/api/deps.py                   get_share_service / get_feedback_service / get_media_lifecycle_service
backend/app/repositories/{__init__,unit_of_work,media}.py  挂载两个仓储 + ledger；媒体新增 list_expired
backend/app/services/assets/service.py    _require_readable 放行 SHARED（原为 403 占位）
backend/app/db/models/{engagement,__init__}.py  ShareAsset；category 64；表数 19→20
backend/app/tasks/{celery_app,worker}.py  lifecycle.cleanup_media + 300s Beat + Worker 入口
backend/app/schemas/common.py             baseline_envelope_response / baseline_error_response /
                                          baseline_request_body（冻结基线形状的显式 OpenAPI 片段）
backend/alembic/env.py                    docstring 表数
backend/tests/services/fakes.py           ENG-09 替身（InMemoryShare/Feedback/LifecycleMedia/EngagementUnitOfWork）
backend/tests/db/{conftest,test_migrations,test_constraints}.py  迁移助手与冻结清单/计数同步
backend/tests/api/test_openapi_contract.py  三条路径纳入已实现清单；修正 walker 对 properties.type 的误判
backend/tests/api/test_error_envelope.py    路径清单
backend/tests/services/test_points.py       基础内存 UoW 补齐新仓储
backend/tests/tasks/{test_celery_app_contract,test_task_boundaries}.py  第五个任务与导入白名单
frontend/services/{api,idempotency,errors}.ts  分享/反馈调用、幂等作用域、两个错误码标题
frontend/hooks/useStudio.ts               share / feedback 状态与动作
frontend/components/Studio.tsx            在终局后接两个面板
docs/architecture/{02-database-design,03-api-contract}.md  事实同步
```

### 8.3 迁移与回滚证据

- revision：`0021_share_asset_links`（**21 字符**；`alembic_version.version_num` 是
  `varchar(32)`，最初的 41 字符标识在真实空库 `upgrade` 时抛
  `StringDataRightTruncation`，已改短——这条正是 0017 的老陷阱，测试里也有长度断言）；
- 内容：新增 `share_assets`（复合主键 `(share_id, asset_id)`、`(share_id, sort_order)` 唯一、
  `ck_share_assets_sort_order`、双 FK `RESTRICT`、`ix_share_assets_asset_id_share_id`）+
  `feedback.category` `varchar(32) → varchar(64)` + **回填**历史单图分享（`sort_order = 0`）；
- 真实 PostgreSQL 覆盖（隔离库，用完即删）：空库 `upgrade head`、结构/约束名逐字比对、
  重复与负数插入被拒、**升级回填**、**单图历史降级/再升级无损往返**、
  **两条降级守卫**（多图关联存在 → 拒；分类 >32 字符 → 拒，且报错写明恢复条件）、
  守卫拒绝后结构与数据保持原样（未截断、未删关联）；
- 共享 `ai_avatar` **未迁移**（仍为 `0016_append_only_guards`）；本轮所有迁移验证都在
  新建的临时库里完成。

### 8.4 契约差异（已记录，未改基线）

1. `shared/api-contract/openapi.json` **一行未改**；三条 operation 已挂载并逐字段对齐
   （运行时 24 条路径，未挂载的只剩 5 条 Admin 路径）。为此在 `schemas/common.py` 新增了
   显式构造基线响应/请求体形状的助手：基线 `ShareEnvelope.data` 是裸 `{"type":"object"}`、
   `FeedbackCreateResponse` 引用通用 `Envelope`（`data: {}`），而 `response_model`
   无论用 `Any`/`object`/`dict` 都推不出这两个形状（实测差异见提交记录）。
   同时发现并修正了契约测试 walker 的一个误判：FastAPI 自动生成的
   `ValidationError.properties.type` 是**属性名**而非 JSON-Schema 关键字，
   walker 现在跳过 `properties` / `$defs` 这类「名字 → 子 Schema」的键。
2. **基线声明遗漏**：`POST /feedback` 的响应集合没有 `409`，而幂等冲突/处理中在全局
   语义下必须是 `409 IDEMPOTENCY_CONFLICT`（契约第 9 节）。实现按全局语义返回 `409`，
   并把这条遗漏写进 `03-api-contract.md` 第 4.4 节，**未修改冻结基线**，待裁决。
3. 本卡自取值（契约未冻结，需确认或改判）：
   - 分享默认有效期 **7 天**（契约第 12 节把「分享默认有效期」列为未决项）；
   - `output_asset_ids` 数量规则 **1–2 张且不重复**（基线未声明 minItems/maxItems）；
   - 分享页对每张选中资产调 `POST /assets/{id}/download-url`（复用既有下载链路）。
4. `POST /shares` 的请求体改为**手工解析**：FastAPI 只要声明了 JSON 请求体就会自动补
   `422`，而基线没有声明；参数与请求体因此用 `openapi_extra` 显式给出，
   Schema 仍是同一个 `ShareCreateRequest`（单一真相），校验结果仍是 422
   `VALIDATION_ERROR`。

### 8.5 本轮门禁证据

```text
backend（AI_PROVIDER=mock，隔离 PostgreSQL）
  pytest tests --ignore=tests/db -q       1455 passed   退出码 0
  pytest tests/db -q                        99 passed   退出码 0
  python -m compileall -q app tests alembic              退出码 0
  git diff --check                                       退出码 0
  pytest tests/api/test_openapi_contract.py -q  50 passed（含逐字段基线比较）
frontend
  npx vitest run --no-file-parallelism     147 passed / 17 files   退出码 0
  npx tsc --noEmit                                                  退出码 0
  npm run build    8 条路由（新增 /s/[slug] 动态路由）              退出码 0
```

`pytest` 实装 **9.0.3**，`requirements.txt` 声明 `>=8.3,<9.0`：**版本不符，未自动免责**，
沿用既有记录并请总控裁决是否调整依赖范围。

### 8.6 未完成项与风险

1. 浏览器端到端（分享页在真实浏览器 + 真实 MinIO 下的取图）**未做**：那属于 `ENG-10`
   的本地完整闭环；本轮前端只做了 Vitest/typecheck/build 与 fetch 级替身验证。
2. 分享/反馈的界面只使用既有设计令牌与语义类名，**未做视觉打磨**；深色模式、窄屏断点
   与真实配色只经过构建与单测，没有做过浏览器视觉验收。
3. 分享的**公开读取限流**未实现（契约第 12 节把它列为未决项）。
4. 生命周期清理的 Beat 投递只在配置面与单测层面验证；**没有**在真实 Worker/Beat 进程上
   跑过一轮（`ENG-10` 范围）。
5. `frontend/tests/share.test.tsx` 覆盖了分享与反馈面板；两部分最终合并为一个测试文件，
   与最初白名单里的「两个文件」不一致（内容覆盖未减少，此处如实登记）。
6. 过程脚本与契约形状探针放在**仓库外**（`.tmp-eng09/`，已被 `.gitignore` 忽略，
   非仓库文件），因此不出现在 git 状态里；它们是可复现的只读/一次性工具，
   删除前无需特别确认。

### 8.7 返修批次（2026-10-09）：总控 7 项缺陷的逐项修复

总控判定 ENG-09 **未通过**并列出 7 项缺陷，本轮按「先补能复现缺陷的回归测试，再修复」
完成，全部在真实隔离 PostgreSQL 与真实 `useStudio` 上验证。

| # | 缺陷 | 修复 | 回归证据 |
|---|---|---|---|
| 1 | 匿名资产读取只看可见性快照，且不检查资产期限 | `AssetService._require_readable` 改为**实时**核对：资产未到期 **且** 存在一条 `ACTIVE` 未过期分享（`share_lookup` 注入，独立工作单元）；未接线时 fail-closed | `tests/api/test_share_endpoints.py` 4 条：资产到期未清理、分享到期未清理、撤销后可见性未重算、未接线 fail-closed |
| 2 | 清理 Worker 未正确进入/关闭 UoW，异常不回滚 | `MediaLifecycleService` 只持有 `uow_factory`，每阶段 `with uow:`（异常先 `rollback()` 再 `close()`）；Worker 与 deps 装配点同步改为传工厂 | `tests/db/test_engagement_concurrency.py`：并发清理后 `close()` 全被调用、异常路径有 `rollback()` 且状态未改写；`tests/tasks/test_worker_task_delegation.py` 4 条装配断言 |
| 3 | 反馈幂等缺数据库级并发保障（同键跨任务） | `pg_advisory_xact_lock` 事务级咨询锁（**不新增表/列**），作用域含用户+键，配 `lock_timeout=5s`；分享写入同样接入 | `test_concurrent_feedback_with_same_key_across_tasks_yields_exactly_one`、`..._same_task_creates_one_row_and_replays` |
| 4 | 分享失效只收敛主资产 | 新增 `ShareLifecycle.close_share`：按 `list_selected_asset_ids` 对**每一张**关联资产重算可见性；撤销与过期共用同一入口 | `test_share_closes_every_linked_asset_visibility`、`test_share_close_keeps_asset_shared_while_another_valid_share_exists` |
| 5 | 选择集合未规范化；并发输家不回放赢家 | 选中集合按 `output_index` 排序后比较与指纹化；唯一约束冲突后重查赢家，同载荷回放、异载荷冲突 | `test_selection_order_is_normalised`、`test_concurrent_share_same_key_same_payload_replays_the_winner` |
| 6 | 分享/反馈状态跨任务互相污染 | `ShareState`/`FeedbackState` 带 `generationId`；`latestGeneration` ref 在响应回来时核对归属，迟到响应丢弃；`clearActive()` 现在同时清 `generationId` | `frontend/tests/engagement-isolation.test.tsx` 3 条 |
| 7 | 没有明确的新分享意图入口，且提示误导（叫人刷新页面） | `shareScope(generationId, attempt)`；`startAnotherShare()` 递增意图序号换新键；面板新增「再生成一条分享链接」按钮；删除「刷新页面」文案 | `share.test.tsx`（意图键/入口/文案）、`engagement-isolation.test.tsx`（新意图换键） |

**并发锁策略与批量上限（同批要求）**：

- `SqlAlchemyMediaAssetRepository.list_expired` 此前**漏了 `with_for_update()`**——
  这正是「并发两轮清理把同一个对象删两次」的真实根因（用独立调试脚本复现后修复），
  现在与 `ShareService` 的 `lock_asset` 落在同一批行上；
- 批量上限改为硬约束 `MAX_CLEANUP_BATCH_SIZE = 50`：构造期与 `run_once(batch_size=...)`
  都会被 `_bounded()` 夹到 50；
- 分享期限改为 `min(默认 7 天, 选中资产最早的 expires_at)`。

**测试基建修复（影响面超出本卡，如实登记）**：`frontend/tests/helpers/stub.ts` 此前
`const reply = matched(call)` **没有 await**，函数式回复返回 Promise 时会被当成 `Reply`
对象，`reply?.status ?? 200` 静默退化成「200 + 空响应体」。这让所有「受控放行 / 挂起响应」
类用例都在假问题上打转。已改为 `await matched(call)`（`await` 对非 Promise 是空操作，
既有同步用例行为不变）。

**返修批次门禁（2026-10-02 首轮返修）**：

```text
backend 非 DB  1468 passed
backend tests/db 106 passed
compileall / git diff --check  退出码 0
frontend vitest 154 passed / 19 files
frontend tsc --noEmit / next build  退出码 0（8 条路由）
```

观察到的 `pytest` 版本：**9.0.3**（全局环境），与本仓库 `requirements.txt` 声明的
`pytest>=8.3,<9.0` **不一致**。本轮没有改动全局环境；合规版本在下一节用隔离 venv 复核。

### 8.8 返修批次（第二轮，2026-10-02 深夜）：总控复验未通过的 7 项

总控判定第一轮返修**未通过**并列出 7 项，本轮连续修复。**先补能复现缺陷的回归测试，
再改实现**；关键结论都在真实隔离 PostgreSQL 或真实组件树上取得。

| # | 缺陷（总控） | 根因 | 修复 | 回归证据 |
|---|---|---|---|---|
| 1 | 资产期限校验不完整 | `_asset_is_servable` 只看 `READY`/`RESULT`，不看 `expires_at`；`to_view` 对匿名读取是"过滤"而非"整体失效" | 判定加入 `expires_at <= now`（与 `effective_expired`、`is_expired` 同口径）；创建阶段校验失败天然无副作用；`read_share` 走 `strict=True`（任一张失效即 404），`ShareService.selected_assets(strict=)` | `tests/api/test_share_endpoints.py`：已到期拒绝且零副作用、`== now` 边界、`now+1s` 通过、两张里一张到期即整体拒绝；**历史分享期限晚于资产期限**、**清理未运行**两个场景严格 404 |
| 2 | 可见性授权边界被破坏 | `_require_readable` 把 `PUBLIC` 也丢进 `share_lookup` 分支：接线后 PUBLIC 被拒；`revalidate_visibility` 会把 `PUBLIC` 改成 `PRIVATE`/`SHARED` | 改为按可见性分派：所有者放行 → 到期 404 → `PUBLIC` 保留既有规则（不受分享接线影响）→ `PRIVATE` 404 → `SHARED` 必须实时有有效分享；生命周期遇到 `PUBLIC` 直接返回，不拥有也不改写 | `test_public_asset_still_readable_with_share_lookup_wired`、`test_private_asset_is_404_for_anonymous_even_with_share_lookup`、`test_lifecycle_never_touches_public_visibility`、`test_lifecycle_never_promotes_private_to_shared`（都是**实际接线**，不是未接线替身） |
| 3 | 并发锁策略未完成 | ①`_select_outputs` 按**请求数组顺序**逐个加锁 → `[A,B]`/`[B,A]` 真实死锁；②`revalidate_visibility` 先读 `get_asset`（identity map 旧快照）再判"其他有效分享"，且判断期间没有资产行锁 | 新增 `lock_assets_by_id`（**按 id 升序**一次 `FOR UPDATE`，未排序直接 `ValueError`）；`close_share` 先落状态再**升序**取资产行锁并刷新事实；新增 `refresh_asset`；`revalidate_visibility` 先锁后判 | `tests/db/test_engagement_concurrency.py`：`[A,B]`/`[B,A]` 并发两个都成功；**创建持锁未提交时撤销最后一条旧分享**（无锁实现会把仍有有效分享的资产错改 PRIVATE）；清理与多图收敛并发后逐资产校验「SHARED ⇔ 有 ACTIVE 未过期分享」；`lock_assets_by_id` 拒绝未排序 |
| 4 | 分享意图未持久化 | `shareAttempt` 只在内存，`sessionStorage` 里留着旧键：刷新后序号归零 → 复用旧键（刷新重试回放、二次新建永远失败） | 新增 `frontend/services/shareIntent.ts`：按任务存 `{attempt, slug}`，序号**单调**推进（`max(内存, 持久化)+1`）；刷新后按 `slug` 重新读取而不是重新创建；冲突时推进序号 | `tests/engagement-isolation.test.tsx`：会话重挂后新意图的键**从未出现过**；回到同一任务时按 slug 恢复、不重复创建；`share.test.tsx` 的键复用/换键断言 |
| 5 | 幂等处理中语义未落实 | 咨询锁等待超时被 SQLAlchemy 包成 `OperationalError`，`except LockNotAvailable` 从不命中 → 落到通用 500 | 捕 `OperationalError` 并校验 `exc.orig` **就是** `LockNotAvailable`（其它 OperationalError 原样上抛）；转成领域信号 `IdempotencyScopeBusy`；两个 Service 回滚后抛既有码 `IDEMPOTENCY_IN_PROGRESS`（409）+ `Retry-After: 5`。不新增错误码、不改基线 | `tests/db/test_idempotency_scope_busy.py`（真实 PG）：持锁时得到 409+Retry-After 且零写入；锁释放后同载荷回放同 slug、异载荷 `IDEMPOTENCY_CONFLICT`；**行锁超时**抛 DBAPIError 而不是幂等码；`test_share_endpoints.py` 路由层 409/Retry-After 形状 |
| 6 | 前端必要状态缺失 | ①匿名分享页失败只有文案、没有入口；②`SharePanel`/`FeedbackPanel` 的局部状态（勾选、表单草稿）不随任务复位 | `SharePage` 用 `attempt` 状态把"加载/重新加载"合成同一条 effect，补真实重试按钮（在途禁用）与失效态刷新；`Studio` 给两个面板加 `key={generationId}`，任务切换即重新挂载 | `tests/share-page.test.tsx`（5 条：可重试并恢复、重试中不重复请求、404 不提供重试、图片地址过期可重取、重试仍失败如实显示）；`tests/share-panel-state.test.tsx`（换 key 复位、同 key 保留、只提交勾选项） |
| 7 | 门禁与报告不足 | 并发同载荷用例只查"成功 slug 集合大小"，漏掉"一成功一冲突"；批量上限只造 2 条证明不了 50；降级恢复条件写成"撤销分享或导出清单"（**都不成立**）；`pytest` 版本漂移；报告文件计数与日期不准 | 断言改为**两个响应都成功且 slug 相同**；批量用例造 51 条并断言单轮 50、后续 1、再后续 0；守卫文案与文档改为真实恢复条件并**加用例跑通恢复路径**；本轮在隔离 venv（pytest 8.4.2）复核 | 见下方"返修批次门禁" |

**并发锁顺序（统一后的全聚合顺序）**：

```text
1. 咨询锁 (user, idempotency_key)     —— 只在 create_share / submit 最前面
2. shares 行锁（如涉及既有分享）        —— lock_by_id
3. 资产行锁（**始终按 asset id 升序**） —— lock_assets_by_id
4. 状态与可见性写入
```

撤销、分享过期、媒体清理、创建分享四条路径都按这个顺序取锁；`lock_assets_by_id`
在输入未排序时直接报错，是防回归的最后一道闸。

> **第三轮更正**：上面这条顺序在第二轮**并没有真正成立**。清理当时是
> 「先锁一批资产行，再逐个回头取分享行锁」，因此与撤销（先分享、再资产）
> 直接死锁 —— 总控在隔离 PostgreSQL 上复现，本轮也在
> `.tmp-eng09/probe-old-order-deadlock.py` 里用对照实验复现出
> `DeadlockDetected`。正确顺序与拆阶段方案见第 8.9 节。

**返修批次门禁（第二轮，隔离 venv，pytest 8.4.2）**：

```text
python -m venv .venv-eng09-r2 && pip install -r backend/requirements.txt
  → pytest 8.4.2（满足 requirements 声明的 >=8.3,<9.0）

backend 非 DB  pytest tests --ignore=tests/db -q      1480 passed   退出码 0
backend DB     pytest tests/db -q                      116 passed   退出码 0
契约一致性      test_openapi_contract + test_error_envelope  66 passed  退出码 0
migrations     tests/db/test_share_assets_migration.py   7 passed   退出码 0
compileall     app tests alembic                       退出码 0
git diff --check                                       退出码 0
frontend       vitest run --no-file-parallelism        163 passed / 20 files  退出码 0
frontend       tsc --noEmit                            退出码 0
frontend       next build                              8 条路由，退出码 0
OpenAPI 基线   sha256 1b84fdc9…909f（未改动）
```

### 8.9 返修批次（第三轮，2026-10-03）：总控复验未通过的 5 项

| # | 缺陷（总控） | 根因 | 修复 | 回归证据 |
|---|---|---|---|---|
| 1 | **真实锁顺序冲突** | 清理是「先锁一批资产行 → 再逐个取分享行锁」，撤销是「先锁分享行 → 再锁资产行」，两者互等 → `DeadlockDetected`。第二轮只统一了**单次资产查询**的顺序，没有审查整个事务的隐式写锁 | 确立全卡唯一顺序 `分享行（升序）→ 资产行（升序）`，并**硬规则**：不得在持有资产行锁时再取分享行锁。媒体清理拆成「分享优先」与「资产」两个阶段；新增 `lock_share_ids` / `lock_active_for_asset` / `lock_expired_shares` / `peek_expired`；`list_expired` 的 `ORDER BY` 改为**按 id**（加锁顺序） | `tests/db/test_lock_order.py`（第三轮版本用"删除回调未被调用"当代理指标，**第四轮已改为直接观测行锁**）；对照实验证明旧顺序确实死锁（`DeadlockDetected`），新顺序两者都完成 |

> 第三轮遗留的两处不成立声明已在第四轮更正：①"确定性编排"当时靠的是删除回调
> 这个代理指标，不是持锁观测；②"留给下一轮（下一轮事务 A 会先收敛那些分享）"
> 对「资产到期但分享期限在未来/NULL/无关联」**不成立** —— 那正是第四轮修的持续延期。
| 2 | **生命周期提升 PRIVATE** | `revalidate_visibility` 写成 `SHARED if has_other_valid_share else PRIVATE`，于是"还有一条有效分享"就把明确设为 `PRIVATE` 的资产重新公开 | 改为**只收敛**：仅当资产当前是 `SHARED` 且已无有效分享时才写 `PRIVATE`；`PRIVATE`/`PUBLIC` 一律保持原样。`expire_shares_for_asset` 改用 `lock_active_for_asset`（先分享后资产） | 服务层两条（分享到期、媒体失效，都真正进入 `close_share/revalidate_visibility`）+ 端点层两条：匿名**读取与签名都 404**。原有"再次撤销已撤销的分享"用例已明确标注不能作为防提升证据 |
| 3 | **分享意图生命周期未完成** | ①存的是"最后用过的序号"，上限时回到 0；②`IDEMPOTENCY_CONFLICT` 自动换键（把冲突伪装成新建）；③序号与键分两次写，存在不一致窗口 | 改存**下一个序号**（`nextAttempt`），序号与键同属一份记录；`advanceShareIntent` 单调推进，超过 `MAX_ATTEMPT` **抛 `ShareIntentExhausted` 且不回绕**；冲突**不换键**，只有 `startAnotherShare`（明确动作）换键；序号用尽显示为明确失败 | `frontend/tests/engagement-isolation.test.tsx` 9 条，含**同一 generationId** 重挂后「再生成一条」拿到从未用过的键、冲突不换键（普通重试同键、明确意图换键）、序号用尽不回绕、**同一个 `useStudio` 实例**内切换任务后旧分享/反馈迟到响应被丢弃 |
| 4 | **验证与报告**（覆盖缺口说明是错的） | 第三轮报告称「`get_uow` 覆盖不到当前用户查询」——**不成立**：`get_current_user` 本身就 `Depends(get_uow)`，邮箱验证也走同一 `uow`。真实原因是覆盖写法不对：直接 `lambda: uow` 返回**未进入上下文**的实例，而 `users`/`emails` 是在 `__enter__` 里绑定的 | 不再"跳过正式鉴权"：`tests/db/test_share_endpoints_real_auth.py` 用**真实 Cookie** 走 `get_current_session → get_current_user → 邮箱验证`，`get_uow` 覆盖成与真实实现同形的生成器（进入上下文并关闭） | 4 条：完整鉴权栈上创建分享 201 且资产真的 `SHARED`、同载荷回放同 slug；邮箱验证门**先拒后放**（两步都测）；无 Cookie 401；匿名读取真实 slug 200 而未知 slug 404 |
| 5 | **执行纪律**（越界删除） | 见下方"资源与纪律" | 仅清理本轮有明确创建记录的资源（`.pytest-r3-*`） | 现存 `.pytest-tmp-eng0x-review` 系列**未被触碰** |

**返修批次门禁（第三轮，隔离 venv，pytest 8.4.2）**：

```text
backend 非 DB  pytest tests --ignore=tests/db -q      1484 passed   退出码 0
backend DB     pytest tests/db -q                      124 passed   退出码 0
  ├ 锁顺序      tests/db/test_lock_order.py                 4 passed
  ├ 真实鉴权    tests/db/test_share_endpoints_real_auth.py   4 passed
  ├ 并发/锁      tests/db/test_engagement_concurrency.py     11 passed
  ├ 幂等占用     tests/db/test_idempotency_scope_busy.py      5 passed
  └ 迁移三态     tests/db/test_share_assets_migration.py      7 passed
契约一致性      test_openapi_contract + test_error_envelope 66 passed  退出码 0
compileall     app tests alembic                       退出码 0
git diff --check                                       退出码 0
frontend       vitest run --no-file-parallelism        167 passed / 20 files  退出码 0
frontend       tsc --noEmit                            退出码 0
frontend       next build                              8 条路由，退出码 0
OpenAPI 基线   sha256 1b84fdc9…909f（未改动）
```

**资源与纪律（如实登记）**：第二轮收尾时我用 `.pytest-*` 通配删除了若干**此前卡片**
留下的临时目录（`.pytest-tmp-eng04-step2`、`eng05-all/c2/fix`、`eng06-all/api/c/fix/reg/s1/s3-api/tasks-all`、
`eng06fix-all/api/final`、`eng07-all/all2/fix-all`、`eng08-final-nondb/g1/nondb/oapi`、
`pytest-tmp-research`）。它们**未被 git 忽略**（`git check-ignore` 返回 1），因此是真实的
未跟踪目录，**无法确认内容、也无法恢复**。它们是 `--basetemp` 指定的测试临时目录，
重新跑对应测试即可再生成，但"可再生成"不等于"没删过" —— 这一点不自动免责。
本轮起只按明确前缀清理自己创建的资源（`.pytest-r3-*`），controller 的
`.pytest-tmp-eng0x-review` 系列保持原样。

### 8.10 返修批次（第四轮，2026-10-03）：总控复验未通过的 4 项

| # | 缺陷（总控） | 根因 | 修复 | 回归证据 |
|---|---|---|---|---|
| 1 | **整个事务的锁顺序** | `close_share` 逐条 `revalidate_visibility`，各锁各的资产：两条到期分享依次关联高/低资产时形成 **高→低** 的资产取锁顺序，与创建分享的**低→高**交叉即死锁。第二轮/第三轮只统一了"单次查询"的顺序 | 引入 `revalidate_assets`（**整批先排序、一次 `lock_assets_by_id`**）与 `lock_and_close_shares`；`close_share` 退化为**只写状态**，不再各自取资产锁。`run_once` 的分享阶段改成「预读目标集合 → 一次性按 id 升序锁分享 → 一次性按 id 升序锁资产 → 关闭 + 收敛」 | `tests/db/test_lock_order.py`：`test_cleanup_takes_asset_locks_in_ascending_order_across_shares`（两条到期分享共享同一张最早资产，外部持该资产锁，用 `pg_locks` + 带 `lock_timeout` 的 `FOR UPDATE` **观测**清理确实停在这张最小的资产上）；`test_cleanup_and_revoke_never_deadlock_on_a_shared_asset` 同样改为持锁观测 |
| 2 | **到期资产持续延期** | 只收敛"自身到期"的分享；资产阶段遇到仍有 `ACTIVE` 分享的资产就记 `deferred`，因此「资产已到期 + 分享期限未来/NULL/无 `share_assets` 关联」会永久延期，公开链接一直可读 | 分享收敛的目标集合改为 **到期分享 ∪ 引用了失效资产的 ACTIVE 分享**；`peek_active_for_assets` 同时按**关联表**与 `shares.asset_id`（历史单图回落）命中；`peek_invalid` 提供失效资产候选；资产是否"真的失效"在**锁内重新核对** | `tests/db/test_lock_order.py` 四条：分享期限在**未来**、为 **NULL**、**无 `share_assets` 行**（历史回落）三态都收敛且删除对象；第五轮起连续多轮**不手工改期限/状态**，并断言「有剩余工作时每轮都推进、总计等于候选数」 |
| 3 | **完整分享意图生命周期** | 记录只有一个 `nextAttempt`，要靠推算才知道"当前用哪把键"，普通重试与刷新重试的键没有明确出处 | 记录改为**两个明确字段**：`current{attempt,key,slug}`（正在使用/已建出）与 `nextAttempt`（下一意图）。`restoreShareIntent` 直接返回 `current`；只有 `advanceShareIntent`（明确动作）把 `next` 提升为 `current`；键与序号一致性在读取时校验（不一致/旧版本/越界 → 整条作废、从 0 开始，不会留下半条旧键）；上限不回绕且**不改写记录** | `frontend/tests/shareIntent.test.ts` 8 条（当前/下一意图分离、重试与刷新同键、只有明确动作换键、损坏与越界作废、上限成功后刷新记录仍可读且不回绕）；`tests/engagement-isolation.test.tsx` 补「记录损坏 → 从头开始且不复用历史值」 |
| 4 | **测试证据** | ①迟到响应用例先 `resolve` 再切换，实际没有覆盖"pending 期间切换"；②撤销/清理用例靠"删除回调未被调用"判断持锁；③文档写了"确定性""下一轮必收敛"两处不成立声明 | ①把范围写进测试文件注释与报告：`inFlight` 是既有防双击闸门，pending 期间**无法**切换任务，因此该交错在真实流程里不可达；用例验证的是"归属判定丢弃不属于当前任务的响应"，明确不声称覆盖前者；②统一改为**持锁观测**（`pg_locks` + 有界 `FOR UPDATE`）；③删除/更正两处声明（模块头与本节上方） | 见上表两行的证据列；`docs` 与模块 docstring 均已更正 |

> **第五轮更正（2026-10-03）**：上面第 4 行 ①「pending 期间**无法**切换任务」这条声明
> **不成立**。`inFlight` 只挡"再发起一次提交"，`clearActive()` 从不检查它，
> 因此"分享/反馈请求仍 pending 时清空任务"是真实可达的交错。第五轮已补两条真实
> 交错用例（分享与反馈各一条），见 8.11。第 3 行声称的「键与序号一致性在读取时校验」
> 当时只做到了"结构校验"，**没有**做到"键来自唯一可信事实并进入实际请求" —— 也在 8.11 更正。

**本轮门禁（隔离 venv，pytest 8.4.2）**：

```text
backend 非 DB  pytest tests --ignore=tests/db -q      1484 passed   退出码 0
backend DB     pytest tests/db -q                      127 passed   退出码 0
  └ 锁顺序/延期 tests/db/test_lock_order.py                7 passed
契约一致性      test_openapi_contract + test_error_envelope 66 passed  退出码 0
compileall     app tests alembic                       退出码 0
git diff --check                                       退出码 0
frontend       vitest run --no-file-parallelism        176 passed / 21 files  退出码 0
frontend       tsc --noEmit                            退出码 0
frontend       next build                              8 条路由，退出码 0
OpenAPI 基线   sha256 1b84fdc9…909f（未改动）
```

### 8.11 返修批次（第五轮，2026-10-03）：总控复验未通过的 4 项 + 测试/报告更正

本轮按「先补能复现缺陷的回归测试 → 再改实现 → 做对照实验证明测试有效」完成。
**未提交 Git、未调用真实 Provider、未使用真实凭据、未迁移共享 `ai_avatar`。**

#### 8.11.1 改动清单（第五轮）

**后端**

```text
app/repositories/engagement.py   新增 share_covers_asset（关联表有行按选中集合、无行回落主资产）
                                 新增 peek_active_for_invalid_assets（分享收敛的驱动集合）
                                 list_valid_for_asset / list_active_for_asset / lock_active_for_asset /
                                 count_active_for_asset / count_active_for_assets 改用同一命中口径
                                 lock_share_ids / lock_assets_by_id 加 populate_existing（锁内读提交后事实）
app/repositories/media.py        peek_invalid 纳入 DELETED/EXPIRED；状态常量拆成"未终结/终结"
                                 lock_assets_by_id 加 populate_existing
app/services/shares/lifecycle.py 新增 asset_is_invalid（纯判定）
                                 close_share 只从 ACTIVE 出发（不得覆盖 REVOKED/EXPIRED）
                                 run_once 段 1 改为「预读只决定锁谁 → 锁分享 → 锁资产 → 锁内重裁决 → 收敛/关闭」
                                 失效资产即使无 ACTIVE 分享引用也收敛可见性
tests/services/fakes.py          内存替身同步三处语义（历史回落、失效含终结状态、驱动集合）
tests/db/test_legacy_share_and_terminated_assets.py   新增 12 条真实隔离 DB 用例
```

**前端**

```text
services/shareIntent.ts   VERSION 3；key 改为「每次意图生成的 32 位十六进制随机串」并随记录持久化；
                          restoreShareIntent 在没有记录时**当场落盘**（刷新重试才有依据）；
                          旧版推导键 `share:<任务>:<序号>` 不匹配 KEY_PATTERN → 整条作废
services/api.ts           createShareLink 改为接收 idempotencyKey 并**原样**使用；
                          删除 `shareScope` 依赖（不再有第二份缓存）
hooks/useStudio.ts        createShareLink 装配 shareIntent.current.key；空键时先恢复再发送
tests/shareIntent.test.ts        重写为 11 条（随机键、清空重访不复用、旧版/损坏/越界作废、上限不回绕）
tests/share.test.tsx             服务层用例改为断言"传入的键原样进请求头"
tests/engagement-isolation.test.tsx  补 3 条：请求头==记录 current.key；建两条分享→清空→重访不复用历史键；
                                     分享/反馈各一条「pending 期间清空」；更正文件头的不成立声明
```

#### 8.11.2 四项修复与**前后对照证据**

| # | 缺陷 | 修复 | 对照实验（改坏 → 失败 → 还原） |
|---|---|---|---|
| 1 | 分享幂等键有两份事实：`shareIntent.current.key` 没进请求，`api.ts` 另走一份 `sessionStorage` 缓存；清空任务重访同任务会复用第一条分享的历史请求头 | 键由「任务+序号推导」改为**每次意图生成的随机串并持久化**；`useStudio` 把 `current.key` 交给 `api.createShareLink`，服务层原样放进 `Idempotency-Key`；清空/旧版/损坏/越界一律新键，上限不回绕 | 把 `newShareKey()` 改回常量（模拟推导键）→ `tests/engagement-isolation.test.tsx` **5 条失败**，其中含总控的原始复现「建两条分享后清空并重访同一任务」；还原后 14 条全过 |
| 2 | 生命周期按预读目标**无条件**关闭：预读后资产期限恢复为未来仍被关闭；另一事务撤销后清理把 `REVOKED` 覆盖成 `EXPIRED` | `lock_share_ids`/`lock_assets_by_id` 用 `populate_existing` 刷新；段 1 在**持锁后**重新判定「仍 ACTIVE 且（自身到期 或 引用失效资产）」才关闭；`close_share` 加「只从 ACTIVE 出发」的状态守卫 | 还原旧语义（`to_close = list(locked)` + 去掉状态守卫）→ 新增文件 **3 条失败**（`does_not_close_when_asset_expiry_is_restored`、`does_not_overwrite_a_concurrently_revoked_share`、`rejudgement_keeps_other_candidates_working`）；还原后全过 |
| 3 | 有效分享查询/计数/可见性收敛只 JOIN `share_assets`：撤销同资产上的新分享后，历史单图分享仍 ACTIVE，资产却被收敛成 `PRIVATE` | 新增 `share_covers_asset`：**关联表有行只按明确选中集合，无行回落 `shares.asset_id`**；授权、有效查询、计数、锁与可见性收敛共用同一口径 | 把 `share_covers_asset` 退化为「只看关联表」→ **2 条失败**（历史分享权威性、计数与匿名授权）；还原后全过 |
| 4 | `peek_invalid` 排除 `DELETED/EXPIRED`：终结资产关联的分享连续多轮保持 ACTIVE、可见性仍 `SHARED` | 失效口径纳入终结状态；新增 `peek_active_for_invalid_assets` 作为**以分享为行**的收敛驱动（避免已收敛资产占满候选头部导致饿死）；资产清理阶段仍只用 `peek_expired`（限未终结状态）→ **不重删终结对象** | 把失效口径退回旧条件 → **6 条失败**（EXPIRED/DELETED × 未来/NULL 期限 × 有/无关联行）；还原后全过 |
| 5 | 测试与报告：`pending` 期间清空被断言为"不可构造"；"单一幂等记录已接通""锁内重算已完成"等声明不成立 | 补分享/反馈各一条**真实 pending 清空**用例（先清空并确认复位，再释放旧响应）；更正三处声明 | 去掉 `useStudio` 的归属判定 → `pending` 分享用例失败；还原后全过 |

**关于"确定性交错"**：生命周期事务的预读刻意不加锁，因此无法用"外部持锁"卡住
预读与取锁之间。本轮用一层**只控制时序、不改行为**的替身
（`tests/db/test_legacy_share_and_terminated_assets.py::_PreReadHook`）：真实预读返回后，
先让**另一条连接**的真实事务提交（改期限 / 走正式 `ShareService.revoke_share`），
再把原样结果交回。用例同时断言钩子确实触发过，避免"交错没构造出来"的空跑。

#### 8.11.3 本轮门禁（隔离 venv，pytest 8.4.2）

```text
backend 非 DB  pytest tests --ignore=tests/db -q       1484 passed   退出码 0
backend DB     pytest tests/db -q                       139 passed   退出码 0
  ├ 迁移三态     tests/db/test_share_assets_migration.py           7 passed
  ├ 锁顺序/延期  tests/db/test_lock_order.py                       7 passed
  ├ 幂等占用     tests/db/test_idempotency_scope_busy.py           5 passed
  ├ 并发/锁      tests/db/test_engagement_concurrency.py          11 passed
  ├ 真实鉴权     tests/db/test_share_endpoints_real_auth.py        4 passed
  └ 本轮新增     tests/db/test_legacy_share_and_terminated_assets.py  12 passed
契约一致性      test_openapi_contract + test_error_envelope  66 passed  退出码 0
compileall     app tests alembic                        退出码 0
git diff --check                                        退出码 0
frontend       vitest run --no-file-parallelism         181 passed / 21 files  退出码 0
frontend       tsc --noEmit                             退出码 0
frontend       next build                               7 条路由，退出码 0
OpenAPI 基线   sha256 1b84fdc93029323b5d2fe268c032787168b7ad0ed4b22d1d1cb09d99c619909f（未改动）
```

**环境说明（如实登记，非代码缺陷）**：本机 shell 注入了 `PYTHONUTF8=1` 与
`PYTHONIOENCODING=utf-8`，而 `tests/services/test_cost_config_bootstrap.py::
test_command_has_no_default_price` 会以**受控 `env`**（不含这两个变量）起子进程，
子进程按系统 ANSI 代码页（GBK）写 stderr、父进程按 UTF-8 解码 → `UnicodeDecodeError`。
**取消这两个变量后 1484 条全过**；这不是本轮改动引入的，也不修改该用例（不在授权范围）。

#### 8.11.4 迁移与回滚状态

- **本轮未新增、未修改任何 Alembic 迁移**；`0021_share_asset_links` 一行未动。
- 隔离库从**空库** `upgrade head` 成功（`tests/db` 夹具每次新建临时库并升级）；
  迁移三态用例（升级回填 / 单图往返 / 两条降级守卫）7 条全过。
- 共享 `ai_avatar` **未连接、未迁移**（本轮所有迁移验证都在本轮新建的隔离实例里完成）。

#### 8.11.5 资源状态（本轮）

| 资源 | 身份 | 处置 |
|---|---|---|
| 隔离 PostgreSQL | 本轮自建：EDB PostgreSQL 16.2 便携实例，数据目录 `D:/tmp/eng09r5-pg/data-edb`，端口 `127.0.0.1:5432`，库 `ai_avatar_eng09r5`（另由夹具创建/删除 `eng01_test_*` 临时库） | **已停止**（不再占用端口）；目录保留供总控独立复验，可随时删除 |
| 仓库内临时目录 | `.pytest-r5-*`、`.tmp-eng09-r5-*`（本轮自建） | 本轮结束前清理 |
| 既有 `.pytest-tmp-eng0x-review` / `.review-*` 系列 | 非本轮创建 | **未触碰** |
| 共享 `ai-avatar-v1-postgres` 卷 / 其他项目容器 | 非本轮创建 | **未触碰**（本机 Docker 引擎未启动，见风险 3） |

#### 8.11.6 未完成项与风险

1. **Docker 引擎在本环境无法启动**：Docker Desktop 4.90 的 WSL2 后端在本机被安全策略
   拦截（`wsl.exe` 在程序黑名单里），后端进程停在 `InitState`。本轮改用**独立 PostgreSQL
   16.2 便携实例**（官方 EDB 二进制）提供真实隔离库，语义等价（真实行锁、真实事务、
   真实唯一约束）。`docker/compose.yml` 形态与容器级验证仍属 ENG-10。
2. 浏览器端到端（真实浏览器 + 真实 MinIO 的分享取图）**未做** —— 属 ENG-10 范围；
   本轮前端只做 Vitest / typecheck / build 与 fetch 级替身验证。
3. `frontend/services/idempotency.ts` 的 `shareScope` 导出**保留但已无人调用**：本轮授权
   范围只列了 `shareIntent.ts` / `api.ts` / `useStudio.ts`，因此不做删除，仅登记为死代码。
4. 分享的公开读取限流、界面视觉打磨、Beat/Worker 真进程验证仍与第四轮相同（未变）。
5. `tests/services/test_cost_config_bootstrap.py` 的编码敏感用例（见 8.11.3）在
   `PYTHONUTF8=1` 的 shell 下会失败；本轮未改该用例，也未改全局环境。

#### 8.11.7 禁令逐项确认

```text
真实 Provider 调用     未触碰（AI_PROVIDER=mock，未出网调用任何生成/视觉接口）
真实凭据               未读取、未使用、未回显（只用本地占位值与合成数据）
真实用户图片           未使用（全部为合成行/合成 key）
共享 ai_avatar          未连接、未迁移
Git 提交/reset/clean/stash  未执行（仅 git status / git diff --check 只读）
其他项目容器/卷/进程/端口   未触碰
既有历史目录与其他会话资源   未清理、未删除
本轮自建资源            只按明确前缀清理（.pytest-r5-*、.tmp-eng09-r5-*）
```

## 9. ENG-10 和更远未来
ENG-10 才负责 Dockerfile/Compose 形态、本地 PostgreSQL/Redis/MinIO/Worker/Beat/Frontend 全链路、Mock 生成/上传/分析/分享/反馈/生命周期和浏览器 E2E。

更远事项：共享 ai_avatar 迁移到 0020（另行授权）；部署镜像和 NEXT_PUBLIC_* build-time 注入；生产对象存储和生命周期策略；quota 到美元的真实对账；analysis_dispatches 归档；视觉模型 MAX_OUTPUT_TOKENS=900 复验；失败率/限流/内容策略样本。当前都不是 ENG-09 的默认工作。

## 10. 新 Agent 执行顺序

1. 只读核对本文件、ENG-09 架构、OpenAPI、模型、StorageAdapter、Celery 和前端 SDK；
2. 列出实际文件白名单和测试门禁，发现契约/迁移缺口立即停线；
3. 在不改变 ENG-00 至 ENG-08 语义的前提下实现 ENG-09；
4. 只用合成数据和隔离基础设施验证，清理自己创建的资源；
5. 报告改动、测试、数据库/容器状态、浏览器证据、未证实项和风险；
6. 明确说明是否调用真实 Provider、是否使用凭据、是否迁移共享库、是否提交 Git；
7. 停线等待总控审核，不自行宣布 ENG-09 通过或启动 ENG-10。

## 11. 最终交接结论

    已完成并放行：ENG-00 至 ENG-08
    ENG-09：分享、反馈、生命周期清理。首轮实现后由总控判定未通过，
            第一至第五轮返修均已完成（见第 8.7–8.11 节），
            并已由总控技术验收通过；语义**冻结，不得再改**
    当前卡片：ENG-10，本地完整闭环和回归测试（本轮工程执行记录见
            docs/handoff/2026-10-02-eng09-eng10-execution.md 第 12 节）
    ENG-10：实现与门禁已执行完毕；浏览器 E2E 17 条用例中 11 通过 / 6 未通过，
            另有 4 项既有缺陷与 6 项环境限制被如实登记。
            **本卡未自行宣布通过，停线等待总控统一审核，未启动 ENG-11**
    下一张：ENG-11（若存在），需总控另行授权
    真实 Provider：禁止继续调用
    真实凭据：禁止读取、使用或回显
    共享 ai_avatar：alembic_version 仍为 0016，未迁移；
            本轮早期**误连过一次**（只写合成数据、未迁移），已在执行记录 §12.7 第 6 项登记
    Git：不提交、不清理、不重置（本轮只做只读检查）

新会话以第 7 节为 ENG-09 的原始范围、以第 8 节为 ENG-09 的实现与未完成项、
以 `docs/handoff/2026-10-02-eng09-eng10-execution.md` 第 12 节为 ENG-10 的
实际执行与未通过项，以第 1、2、5、6、10 节作为持续有效的边界。
