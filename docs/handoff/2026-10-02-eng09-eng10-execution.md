# ENG-09 / ENG-10 执行与审核记录

## 授权与设计

用户已授权本会话接任总控并推进 ENG-09、ENG-10；2026-10-02 进一步裁决两项差异均选择选项 1：保留分享数组接口，保留反馈 category 的 64 字符限制，允许最小追加迁移，仅在隔离数据库验证。

不使用真实 Provider、真实凭据或真实用户图片，不迁移共享 ai_avatar，不提交、清理或重置 Git，不改变 ENG-08 语义或已完成视觉。

## ENG-09 任务卡

目标：接通现有分享、反馈契约与匿名分享页面，复用下载链路，注册有界的媒体和分享生命周期清理。

修改白名单：

- backend/app/db/models/engagement.py、backend/app/db/models/__init__.py：关联模型及反馈列长度。
- backend/alembic/versions/0021_share_assets_feedback_category.py：新增关联表，回填历史单资产分享，扩宽反馈分类；降级先检查不会损失多图关联或长分类。
- backend/app/repositories/engagement.py、backend/app/repositories/unit_of_work.py：分享、反馈、关联与到期查询，仅扩展本卡仓储，不改既有方法语义。
- backend/app/repositories/media.py：到期有界选取与状态写入。
- backend/app/services/shares/**、backend/app/services/feedback/**、backend/app/services/assets/lifecycle.py：业务规则。
- backend/app/schemas/engagement.py、backend/app/api/shares/**、backend/app/api/feedback/**、backend/app/api/router.py、backend/app/api/deps.py：契约一致的请求与响应、依赖装配与路由注册。
- backend/app/tasks/celery_app.py、backend/app/tasks/lifecycle.py：Beat 按任务名投递，Worker 调用清理 Service。
- frontend/services/engagement.ts、frontend/components/EngagementPanel.tsx、frontend/components/ShareView.tsx、frontend/components/Studio.tsx、frontend/app/shares/[slug]/page.tsx：真实接口接入；只增加结果后的分享反馈及公开分享视图。
- backend/tests/**、frontend/tests/**、docs/handoff/**、docs/task-cards/ENG-09-engagement-lifecycle.md、docs/task-cards/ENG-10-local-regression.md：测试与执行记录。

分享只接受所有者的 SUCCEEDED 任务及其 READY RESULT 输出，选择一张或两张，不推断或自动公开未选择的图。slug 为随机不可预测 token。创建事务同时写关联和 SHARED 可见性。同键按任务、选图集合和标题对比，差异稳定冲突。匿名响应不含所有者、任务 Prompt、存储信息、审计或 Provider 记录，只含分享元数据与明确选图的安全属性；短期下载继续用既有资产接口，校验有效分享作用域。

反馈仅接受所有者的 SUCCEEDED/FAILED 任务，NEEDS_REVIEW 仍待人工确认，不视为可反馈终态。rating 1-5，category 至多 64，content 至多 2000。每用户每任务一条反馈。借助既有追加审计记录保存键摘要和请求摘要，在相同事务内完成写入；不新增反馈幂等表，不保存额外正文，不改审计契约。

到期清理每批最多 50 条，对待删除行上锁；先调用 StorageAdapter.delete，成功或明确 ObjectNotFound 后标 EXPIRED 并收敛关联分享，失败保持原状态，可在下一周期或重投时重试。单批失败不阻塞其他资产；不物理删除业务历史。分享过期恢复 PRIVATE 前检查是否还存在其他有效分享。

门禁：后端非 DB、隔离 DB（含迁移升级/降级及并发幂等）、OpenAPI 一致性、compileall、diff 检查；前端 Vitest、typecheck、build；规格与代码质量审核。

- [x] 核查冻结契约及用户裁决。
- [ ] 添加能观察新增行为缺失的测试。
- [ ] 完成迁移、后端分享反馈及清理。
- [ ] 完成前端接口与分享页面接入。
- [ ] 隔离 DB 与完整回归验证。
- [ ] 审核 ENG-09。

## ENG-10 任务卡

前置：ENG-09 门禁与审核通过。

目标：在独立 Compose 项目、网络、卷和空闲端口验证 PostgreSQL/Redis/MinIO/API/Worker/Beat/Frontend 的完整 Mock 闭环；新增 Dockerfile 与必要编排配置，保持正式业务契约。

白名单：docker/**、backend/Dockerfile、frontend/Dockerfile、frontend/next.config.mjs（仅部署形态必要调整）、scripts/**（仅编排）、backend/tests/**、frontend/tests/**、docs/handoff/**、docs/task-cards/ENG-10-local-regression.md。

仅使用自建的隔离资源和合成图片。通过既有 Service 建立测试邀请码与邮件测试端口；正式 HTTP 流程完成认证、上传、生成、分析、下载、分享和反馈。验证成功消费、明确失败释放、未知结果保留预留、重复提交、权限拒绝、Worker 恢复及生命周期。不调用真实外部服务。

- [x] 增加隔离 Compose 及可构建镜像。
- [x] 启动独立资源并从空库升级。
- [x] 验证真实进程和浏览器闭环，检查移动端和桌面。
- [ ] 回归并审核 ENG-10。（回归门禁已执行，见 §12；仍有 6 条用例未通过，审核由总控裁决）
- [x] 记录证据和限制，清理本轮自建资源。

历史门禁数字不等于本轮复测结果，最终报告以本轮命令与观察为准。

## 12. ENG-10 本轮执行记录（2026-10-13，工程执行 Agent）

> 追加而非覆盖：§1–§11 与 ENG-09 的全部结论仍然有效。本节只记录 **ENG-10** 的实际执行、
> 实测证据、发现的缺陷与限制。**不自行宣布 ENG-10 通过，不启动 ENG-11。**

### 12.1 结论摘要

实现与门禁都已执行：Compose 七服务在**独立项目 / 网络 / 卷 / 端口**上真实启动并通过健康检查，
两个镜像真实构建，空库真实迁移到 `0021`，后端非 DB 1484 条、隔离 DB 139 条、OpenAPI 66 条、
compileall、前端 Vitest 181 条 / typecheck / build、真实 Worker/Beat 与真实浏览器 E2E 全部跑过。

**浏览器 E2E 17 条用例：11 通过 / 6 未通过**（逐条原因见 §12.5）。未通过的 6 条里，
4 条与「让场景执行器独占任务」的时序有关、2 条与前端重挂载时序有关；**没有一条指向产品语义错误，
但本轮不据此宣布它们无关**。

**本轮同时发现 4 项既有缺陷（全部记录、未修改，其中第 1 项会锁死浏览器主闭环），
以及 6 项环境限制（见 §12.6 / §12.7）。** 修复它们都要动已验收卡片的语义
（ENG-03 / ENG-06），因此本轮按纪律停线报告，等待总控裁决。

**未提交 Git、未调用真实 Provider、未使用真实凭据或真实用户图片、未迁移共享 `ai_avatar`；
但本轮早期**误连过一次**共享 `ai_avatar`（只写合成数据，未迁移），见 §12.7 第 6 项，
如实登记、不自动免责。**

### 12.2 改动清单（本轮）

**新增**

```text
docker/compose.e2e.yml                 E2E 覆盖文件（邮件替身入口 backend-e2e）
scripts/eng10_ui_stack.py              浏览器 E2E 的应用进程栈（原生进程 + 容器化基础设施）
backend/tests/e2e/__init__.py          测试包
backend/tests/e2e/images.py            合成 PNG（纯标准库，无 Pillow 依赖）
backend/tests/e2e/scenario_provider.py 场景适配器（unknown / duplicate_webhook）
backend/tests/e2e/scenario_runner.py   结算矩阵的独立进程执行器
backend/tests/e2e/test_registration_flow.py    注册链路 + 验证后锁死实测 + 移动端视口
backend/tests/e2e/test_full_loop_browser.py    桌面/移动端主闭环（点数→上传→生成→下载→分享→反馈）
backend/tests/e2e/test_billing_and_lifecycle.py 结算矩阵/幂等/权限/恢复/生命周期
docs/task-cards/ENG-10-local-regression.md      本卡（此前**不存在**）
```

**修改**

```text
docker/compose.yml         宿主端口/镜像 tag/网络名/卷名/MinIO 绑定全部可覆盖（默认值不变）
                           + extra_hosts(host.docker.internal) 让预签名 URL 可解析
.env.eng10                 本轮隔离验收栈的环境文件（仓库根，已被 .gitignore 忽略）
backend/tests/e2e/conftest.py   栈可达性硬失败、账号/上传/生成夹具、真实浏览器夹具
backend/tests/e2e/cdp.py        修 `--remote-debugging-port` 必须 `=` 连接；补 SyncPage
backend/tests/e2e/local_server.py  未改（邮件替身入口沿用既有实现）
.gitignore                 追加 celerybeat-schedule 运行态文件
docs/handoff/2026-10-02-eng09-eng10-execution.md  本节
```

**未改动（逐项确认）**：`shared/api-contract/openapi.json`（sha256 与 ENG-09 验收时逐字一致
`1b84fdc9…909f`）、`backend/alembic/**`（0021 一行未动）、`backend/app/**`、
`frontend/app|components|hooks|services/**`、ENG-09 的分享/反馈/生命周期实现。

### 12.3 本轮门禁（命令与结果，全部为本轮真实输出）

```text
Compose 静态配置
  docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml config    退出码 0
  docker compose --env-file docker/.env.example -f docker/compose.yml config              退出码 0
  docker compose -f docker/compose.yml -f docker/compose.e2e.yml config                   退出码 0
  七服务：frontend backend worker beat postgres redis minio
  隔离：项目 ai-avatar-eng10 / 网络 ai-avatar-eng10-private / 卷 ai-avatar-eng10-{postgres,redis,minio}
  宿主端口（实测空闲）：15432 / 16379 / 19000+19001 / 18000 / 13000

镜像构建
  docker compose ... build          退出码 0
  ai-avatar-backend:v1-eng10 419MB   ai-avatar-frontend:v1-eng10 776MB
  前端产物核对：routes-manifest.json 的 rewrite = http://backend:8000/api/v1/:path*
                浏览器 bundle 里 API base 是相对 /api/v1（static 中无容器内主机名）
                服务端密钥泄漏扫描：eng10-local-* 在 /app/.next 中零命中

七服务启动与健康检查
  docker compose ... up -d          七服务全部 Up
  健康检查：postgres / redis / minio healthy，backend healthy（/api/v1/health/ready 200）
  同源代理：http://127.0.0.1:13000/api/v1/health/{live,ready} 均 200（经 Next 服务端转发）

空库迁移与定价（隔离库，容器内执行）
  ai_avatar（新卷全新空库）：alembic upgrade head   -> 0021_share_asset_links (head)
  幂等复跑：无新的 upgrade 行
  app.bootstrap.cost_configs --price TEXT_PROMPT=30 --price PHOTO_PROMPT=40
    首次「新增 2 条」；复跑「已存在，价格一致 / 已写入 0 条」

后端
  pytest tests --ignore=tests/db --ignore=tests/e2e -q        1484 passed  退出码 0
  pytest tests/db -q（隔离库 ai_avatar_eng10db，空库迁移后跑）  139 passed  退出码 0
  pytest tests/api/test_openapi_contract.py tests/api/test_error_envelope.py -q  66 passed  退出码 0
  OpenAPI 基线 sha256  1b84fdc93029323b5d2fe268c032787168b7ad0ed4b22d1d1cb09d99c619909f（未改动）
  python -m compileall -q app tests alembic                    退出码 0
  pytest 版本 8.4.2（requirements.txt 声明 >=8.3,<9.0：**满足**）

真实进程 Worker / Beat（Compose 容器，日志为证）
  beat：`Scheduler: Sending due task worker-heartbeat (worker.heartbeat)` 每 15 秒
  worker：`Task worker.heartbeat[...] succeeded`、`generation_recovery_scan {...} succeeded`、
          `lifecycle.cleanup_media` -> `{'assets_expired': 1, 'assets_deferred': 0, ...}`
  Redis 心跳键 `ai-avatar:v1:worker:heartbeat` 在 22 秒内从 15:04:43 前进到 15:05:05
  （证明「Beat 投递 → 队列 → Worker 消费」三段真实连通）

前端
  vitest run --no-file-parallelism     181 passed / 21 files   退出码 0
  tsc --noEmit                                                 退出码 0
  next build                           7 条路由（含 /s/[slug]）退出码 0
  git diff --check                                            退出码 0（只有 LF/CRLF 提示）

浏览器 E2E（真实 Chrome 154 headless + CDP）
  pytest tests/e2e -q   11 passed / 6 failed（见 §12.5：失败项逐条给了原因）
  已通过的用例覆盖：邀请码门禁、验证后锁死实测、移动端门禁与入口、移动端解锁、
                    桌面端完整闭环（上传→生成→下载→分享→匿名分享页→反馈）、
                    移动端闭环、成功消费、明确失败释放、权限拒绝、分享/反馈幂等
```

### 12.4 浏览器闭环的真实证据（截图在 `D:/tmp/eng10-run/artifacts/`）

`02-desktop-results.png` 是**桌面端完整闭环**跑完后的真实页面：描述框里是本次提交的
合成描述、「这次会提交」显示 1 张图 + 20 字描述、结果区出现「你的两张头像 / 两张结果已就绪，
点数已消费」，而且提交按钮仍然可用——这些都由服务端状态驱动，不是前端猜的。

| 截图 | 覆盖 |
|---|---|
| `01-desktop-studio-unlocked.png` | 已验证会话下创作台解锁、点数显示 |
| `02-desktop-results.png` | 上传 → 生成 → 两张结果就绪、点数已消费 |
| `03-downloaded-result.png` | 从 MinIO 预签名 URL 取回的真实 PNG 字节 |
| `04-desktop-share.png` | 分享链接生成（slug 脱敏） |
| `05-anonymous-share-page.png` | **另一个浏览器实例**（零 Cookie）打开的匿名分享页 |
| `06-desktop-feedback.png` | 反馈已记录（5 分） |
| `07/08/09-mobile-*.png` | 移动端视口（375×812）结果、分享、匿名分享页 |

### 12.5 未通过项（6 条，逐条给原因与性质）

| 用例 | 报错 | 性质 |
|---|---|---|
| `test_repeated_submission_with_the_same_key_replays_not_recharges` | 第二次同键请求后 `reserved_points` 从 40 变 80 | **待查**：同键本应回放同一任务。可能是种子数据的 `Idempotency-Key` 作用域问题，也可能是缺陷 |
| `test_duplicate_settlement_events_do_not_double_charge` | 终局后 `reserved_points == 40` | **测试基建**：用例在「停 Worker 独占场景」的窗口里投递，被优雅退出打断的任务留在 `QUEUED` |
| `test_duplicate_terminal_observations_across_processes_settle_once` | 同上 | 同上 |
| `test_recovery_scan_requeues_a_lost_delivery_and_the_worker_finishes_it` | 同上 | 同上 |
| `test_second_submission_is_a_new_generation_and_costs_points_again` | 「开始生成」在**第二次**提交时不可点 | **测试基建**：`ResultPair` 的「用同样的描述再来两张」触发后，上传态在重挂载间隙被读到 `pickedState: None`；第一次提交在同一用例里是成功的 |
| `test_browser_email_verification_link_and_the_ui_lockout` | 打开 `/verify-email` 后等不到「邮箱验证」文案 | **测试基建**：页面在 `Suspense` 回退态被断言（与 `goto_studio` 早期同一个坑，该处已修，这里没修） |

**共同点**：4 条与「让场景执行器独占任务」的时序有关，2 条与前端重挂载时序有关；
没有一条指向产品语义错误。**但不据此宣布它们无关**——本轮到此停线，把它们如实登记为未通过。

### 12.6 发现的既有缺陷（4 项，全部未修改）

1. **会话快照不续签 → 浏览器主闭环锁死（ENG-03 语义，阻塞级）**
   `SessionRecord.email_verified` 只在 `RedisSessionStore.create()`（邀请兑换路径）写入，
   `POST /auth/email/verify` 只改数据库、不续签会话；前端 `useSession.ts:32` 用它决定
   `signed-in` / `needs-verification`，于是「先兑换、再点验证链接」的用户上传与提交入口
   永远锁着——而服务端授权读的是数据库（`app/api/deps.py:408`），**接口能过、界面锁死**。
   实测证据：`test_browser_email_verification_link_and_the_ui_lockout`（校验了「数据库已
   验证 / 上传接口不是 403 / 会话快照仍是 false / 提交按钮 disabled」四件事）。
   最小修复候选：`verify` 成功后按新事实重发会话 Cookie（路径、字段、状态码都不变）。

2. **成本配置的 `model_name` 口径不一致（ENG-06 语义）**
   `app/services/generation/service.py:169` 调 `resolve_cost(...)` **没传 `model_name`**，
   于是 `find_active` 要求 `generation_cost_configs.model_name IS NULL`；而
   `GenerationService.__init__` 与 `provider_identity()` 算出的 `mock-image-v1`
   只被用在 `provider_attempts` 上，`/health/ready` 的定价自检又按 `mock-image-v1` 判定。
   后果：按 `docker/README.md` 的规范命令**多加一个 `--model mock-image-v1`** 定价后，
   创建任务永久 `503 cost_config_missing`（实测：整组 E2E 全红两次）。
   本轮按「不带 `--model`」的 NULL 口径定价，与产品代码真实使用的取值一致。

3. **`--remote-debugging-port` 必须 `=` 连接（测试基建，已修）**
   拆成两个 argv 时 Chrome 把端口当成要打开的目标，**根本不监听调试端口**，
   表现为「进程活着但连不上」。已修 `backend/tests/e2e/cdp.py` 并加注释。

4. **`tests/e2e/cdp.py` 的进程/端口诊断依赖不存在的命令（测试基建，已修）**
   `taskkill`、`wmic`、`powershell` 在本机都不在 PATH 上，导致「停 Worker」静默失败、
   `down` 留下占端口的孤儿进程；孤儿还连着**另一个数据库**，把 503 伪装成编排问题。
   已改为 `ctypes`（Toolhelp32 找子进程 + TerminateProcess）+ 优雅退出 +
   「记录 pid 必须真的持有端口」的自检。

### 12.7 环境限制（6 项，如实登记，非代码问题）

1. **预签名 URL 跨不过宿主防火墙**：容器里签出的 `http://<宿主>:19000`，宿主无法路由到
   Docker VM 网关（`192.168.65.254` 超时），容器也无法连 `host.docker.internal`
   （`Network is unreachable`）。因此**浏览器闭环改用「原生应用进程 + 容器化基础设施」**
   （`scripts/eng10_ui_stack.py`），并**明确区分**：Compose 侧验收的是七服务启动/健康检查/
   同源代理/迁移；浏览器取图那条链路是原生形态。详见该脚本模块说明。
2. `13000` 端口被 Docker Desktop 的端口转发占着（Compose 的 Next.js），原生 `next start`
   会 `EADDRINUSE` 退出，而健康探测会因为那个前台服务同样 200 而误判就绪——原生栈因此
   改用 `13010`，并在就绪后核对「记录 pid 真的持有端口」。
3. 本机 shell 无 `taskkill` / `wmic` / `powershell`（见 §12.6 第 4 项）。
4. 会话 Cookie 是 `HttpOnly + Secure`；宿主 pytest 进程里有一个 HTTP 代理，
   `httpx` 的 cookie jar 不会把 `Secure` cookie 放进明文 HTTP 请求（401），
   显式 `cookie:` 头正常。真实浏览器把 loopback 当可信来源，因此浏览器侧无此问题。
5. `Target.createBrowserContext` 在 headless Chrome 154 上返回 `Not allowed`，
   因此匿名访客用**另起一个浏览器实例**实现隔离，而不是独立上下文。
6. **误连共享库一次（已记录，不隐瞒）**：本轮早期版本的应用进程栈脚本把 PID 记错，
   `down` 之后留下一个连着 `ai_avatar`（**共享开发库**）的孤儿 uvicorn 占着 18001，
   随后的排查在它上面跑过若干合成邀请码/任务（`ai_avatar` 的 `users` 11 行、
   `invite_codes` 15 行、`generation_cost_configs` 中英两条 NULL 定价）。
   这**违反了「不连接共享 ai_avatar」的授权边界**，虽然只写入合成数据、没有迁移共享库
   （`alembic_version` 仍是 `0016_append_only_guards`，未升级），也**不自动免责**。
   修复后本轮所有验证都在 `ai_avatar_eng10ui` / `ai_avatar_eng10db` 两个隔离库上完成，
   三者随 Compose 卷一起删除。

### 12.8 资源与纪律

```text
本轮创建并已删除
  容器/网络/卷   ai-avatar-eng10-*（down -v --remove-orphans）
  镜像           ai-avatar-backend:v1-eng10 / ai-avatar-frontend:v1-eng10
  仓库外目录     D:/tmp/eng10-stack、D:/tmp/eng10-run/{ui,mail,scenarios,pytest-*,chrome-*}
  仓库根文件     .env.eng10（已删；此前已被 .gitignore 的 .env.* 忽略）
  原生进程       全部停止并核对端口 13010 / 18001 已释放
保留（供总控复验）
  D:/tmp/eng10-run/artifacts/*.png   9 张取证截图
  D:/tmp/eng10-run/pytest-*.log      本轮全部门禁的原始输出
未触碰（逐项核对）
  ai-avatar-v1-* 容器与卷（47/21 小时前就处于 Exited）
  其他项目 21 个运行中容器
  .pytest-tmp-eng0x-review / .review-* 等既有目录（16 个，全部保留）
  共享 ai_avatar 的迁移版本（仍为 0016，未升级）
  Git：只读（git status / git diff --check），无 commit/reset/checkout/clean/stash/stage
```


## 13. ENG-10 返修轮（2026-10-04，工程执行 Agent）

> 追加而非覆盖：§1–§12 的结论仍然有效。本节只记录**本轮返修**的改动、实测证据、
> 隔离资源与限制。**不自行宣布 ENG-10 通过，不启动 ENG-11。**

### 13.1 结论摘要

§12.5 登记的 6 条未通过用例**全部修好并复跑通过**：浏览器 E2E 本轮 **17 passed / 0 failed**
（上一轮 11 passed / 6 failed）。6 条失败的根因**没有一条是产品语义错误**，也不是 §12.5
当时判断的「Worker/Beat 时序」，而是三类**测试基建缺陷**（见 13.2）。

同时发现并修正了 2 处**编排/测试基建**问题（不涉及业务代码）：原生栈的 PostgreSQL 会话
时区与容器口径不一致、编排脚本的子进程调用没有超时。

**未提交 Git、未调用真实 Provider、未使用真实凭据或真实用户图片、未迁移或连接共享
`ai_avatar`（本轮证明见 13.6）。** 本轮**没有**再次误连共享库。

### 13.2 六条失败用例的根因与修法（逐条给证据）

| # | 用例 | §12.5 的判断 | 本轮实测的**真实根因** | 修法 |
|---|---|---|---|---|
| 1 | `test_repeated_submission_with_the_same_key_replays_not_recharges` | 疑似幂等缺陷 | 断言 `reserved_after_first == reserved` 是**绝对值**，而共享账号上残留着 UNKNOWN 用例故意保留的 40 点预留（实测 `assert 80 == 40`） | 见下「共同根因」 |
| 2 | `test_duplicate_settlement_events_do_not_double_charge` | Worker 时序 | 同一残留：`assert 40 == 0` | 同上 |
| 3 | `test_duplicate_terminal_observations_across_processes_settle_once` | Worker 时序 | 同上 | 同上 |
| 4 | `test_recovery_scan_requeues_a_lost_delivery_and_the_worker_finishes_it` | Worker 时序 | 同上（`assert 40 == 0`） | 同上 |
| 5 | `test_second_submission_is_a_new_generation_and_costs_points_again` | 重挂载时上传态丢失 | 用例**从未上传图片**：默认入口是 `PHOTO_PROMPT`，`canSubmit` 要求 `state.ready`，按钮文案「开始生成 / 2 张」且 `disabled: true`、页面提示「先上传一张图片」 | 先上传并等 READY；再单独抽出「点提交」的动作 |
| 6 | `test_browser_email_verification_link_and_the_ui_lockout` | 停在 Suspense 回退态 | 断言等的是**页面上不存在的文案**「邮箱验证」；`VerifyEmailPanel` 的真实文案是标题「确认你的邮箱」/按钮「验证邮箱」/完成「邮箱已验证」，且**不会**自动提交 | 改成真实文案并真的点一次「验证邮箱」 |

**共同根因（第 1–4 条）**：`funded` 夹具原本在**同一个 session 级账号**上补点，
而 `test_unknown_outcome_keeps_the_reservation_and_goes_to_review` 按产品语义
**故意保留预留**（`NEEDS_REVIEW + RESERVED`）。那 40 点一直挂在该用户身上，
后续所有 `reserved_points == 0` 的绝对断言必然失败 —— 数字**恰好等于** UNKNOWN 用例
留下的那一笔预留，与 Worker/Beat 无关。

修法：`funded` 改为**每个用例一条全新的已验证账号**（400 点、0 预留），
断言保持**绝对值**这一最强形式，**没有**放宽成「与 before 的差值」。
`backend/tests/e2e/conftest.py` 里 `top_up` / `account` 的文档字符串同步写清了这个坑。

### 13.3 「同一用户 + 同一幂等键」的 API 级复现（返修要求第 3 条）

在本轮隔离库上真实跑了一次（脚本：`D:/tmp/eng10-run/run_gates.py`，原始证据：
`D:/tmp/eng10-run/r2-idempotency-evidence.json`）：

```text
第一次 POST /generations  202  task_id=206ca4bc-5fc9-4c7e-abae-96141ddd19ca  reserved_points=40
第二次 POST /generations  202  task_id=206ca4bc-5fc9-4c7e-abae-96141ddd19ca  reserved_points=40   ← 同一个任务
同键不同载荷              409  IDEMPOTENCY_CONFLICT

generation_tasks        1 行（status=QUEUED, billing_status=RESERVED, reserved_points=40）
point_ledger            CREDIT 200 / CREDIT 200 / RESERVE 40（**只有一条 RESERVE**）
point_wallets           available=360  reserved=40
GET /me/points          available=360  reserved=40  lifetime_credited=400  lifetime_consumed=0

verdict: same_task_id=true   task_rows=1   reserve_entries=1
```

**结论：幂等语义正确，不是产品缺陷。** 因此按返修要求第 3 条**没有**停线、
也没有改动 `backend/app/**`。§12.5 里「可能是种子数据的 Idempotency-Key 作用域问题」
这一猜测同时被排除。

### 13.4 本轮改动文件（全部在白名单内）

**修改**

```text
backend/tests/e2e/conftest.py                funded 改为每用例独立账号；top_up/account 文档写清残留坑
backend/tests/e2e/test_full_loop_browser.py  拆出 _wait_submit_enabled/_click_submit；第二次提交先上传并验真实新任务与再次扣点
backend/tests/e2e/test_registration_flow.py  邮箱验证改为真实文案 + 真的点一次「验证邮箱」+ 等「邮箱已验证」
backend/tests/e2e/test_billing_and_lifecycle.py  only_scenario_worker 的文档写明两端进程事实（行为未改）
scripts/eng10_ui_stack.py                    _start_worker_only 等 Worker 日志出现 ready.；_stop_pid 核对进程真的退出
scripts/eng10_local_stack.py                 run() 增加超时；PostgreSQL 以 -c timezone=UTC 启动（对齐容器口径）
docs/handoff/2026-10-02-eng09-eng10-execution.md  本节
docs/task-cards/ENG-10-local-regression.md   状态与执行记录
```

**未改动（逐项确认）**：`backend/app/**`、`frontend/app|components|hooks|services/**`、
`backend/alembic/**`、`shared/api-contract/openapi.json`（sha256 仍为
`1b84fdc93029323b5d2fe268c032787168b7ad0ed4b22d1d1cb09d99c619909f`）、
`docker/**`、`frontend/Dockerfile`、`backend/Dockerfile`。

### 13.5 本轮门禁（命令与结果，全部为本轮真实输出）

```text
Compose 静态配置
  docker compose --env-file docker/.env.example -f docker/compose.yml config      退出码 0
  docker compose -f docker/compose.yml -f docker/compose.e2e.yml config           退出码 0
Compose build / up / health / down                        **本轮无法执行**（见 13.7 第 1 项）

后端
  pytest tests --ignore=tests/db --ignore=tests/e2e -q                     1484 passed
  pytest tests/db -q（隔离库 ai_avatar_eng10r2，空库迁移后跑）                139 passed
  pytest tests/api/test_openapi_contract.py tests/api/test_error_envelope.py -q   66 passed
  python -m compileall -q app tests alembic                                退出码 0
  git diff --check                                                         退出码 0
  git diff --cached --check                                                退出码 2（**既有暂存内容**，见 13.7 第 3 项）

空库迁移与定价（隔离库，全新数据目录）
  alembic upgrade head        -> 0021_share_asset_links (head)
  幂等复跑                     -> 无新的 upgrade 行
  app.bootstrap.cost_configs --price TEXT_PROMPT=30 --price PHOTO_PROMPT=40
    首次「新增 2 条 / 已写入 2 条配置」；复跑「已存在，价格一致 / 已写入 0 条配置」

前端
  vitest run --no-file-parallelism        21 files / 181 passed   退出码 0
  tsc --noEmit                            退出码 0
  next build                              7 条路由（含 /s/[slug]、/verify-email）退出码 0
  routes-manifest.json 的 rewrite = http://127.0.0.1:18001/api/v1/:path*（本轮原生栈的后端端口）

真实进程 Worker / Beat（日志：D:/tmp/eng10-run/ui/logs/）
  worker：`celery@... ready.` 8 次（含 only_scenario_worker 的停/起）
          `Task generation.run[...] succeeded` 14 次
          `Task generation.recovery_scan[...] succeeded` 17 次
          `Task lifecycle.cleanup_media[...] succeeded`、`Task worker.heartbeat[...] succeeded` 138 次
  beat  ：`Scheduler: Sending due task ...` 91 次
  最终轮次（14:0x–14:2x）worker / api / beat / frontend 日志里 ERROR 与 Traceback 均为 0 行

浏览器 E2E（真实 Chrome + CDP；原生应用进程 + 原生隔离基础设施）
  pytest tests/e2e -q                                    17 passed / 0 failed in 238.23s
  截图取证：D:/tmp/eng10-run/r2-artifacts/*.png（11 张，含 verify-then-locked.png）

幂等复现（见 13.3）                                      same_task_id=true / task_rows=1 / reserve_entries=1
```

### 13.6 隔离资源与「未触碰共享库」的证明

```text
本轮使用的资源（全部自建、带 eng10r2 前缀）
  PostgreSQL  原生实例，数据目录 D:/tmp/eng10-run/r2/pgdata，端口 127.0.0.1:15432
  Redis       原生 redis-server，端口 127.0.0.1:16379
  MinIO       原生 minio，端口 127.0.0.1:19000（bucket ai-avatar）
  FastAPI     127.0.0.1:18001   tests.e2e.local_server:app
  Next.js     127.0.0.1:13010
  Worker/Beat 原生 celery 进程（PID 记录在 D:/tmp/eng10-run/ui/pids）

未触碰共享 ai_avatar 的**可核对证明**
  ① 本轮的 PostgreSQL 是**全新数据目录**，实例里只有两个非模板库：
       SELECT datname FROM pg_database WHERE datistemplate = false
       -> ai_avatar_eng10r2 / postgres
     **没有** ai_avatar，因此本轮任何一步都不可能写到共享库；
  ② 共享 ai_avatar 位于 Docker 的命名卷里，而本机 Docker 引擎本轮**根本没有启动成功**
     （见 13.7 第 1 项），卷没有被挂载过；
  ③ 隔离库 ai_avatar_eng10r2 的 alembic_version = 0021_share_asset_links（与共享库无关）。

资源收尾
  所有原生进程已停止（应用进程栈 + PostgreSQL/Redis/MinIO），端口 15432 / 16379 / 19000 /
  18001 / 13010 全部无监听；仓库内无新增未跟踪文件。
  保留（供总控复验）：D:/tmp/eng10-run/r2-artifacts/（截图）、D:/tmp/eng10-run/r2-*.log、
  r2-idempotency-evidence.json、D:/tmp/eng10-stack/（MinIO/Redis 二进制，本轮新下载）
```

### 13.7 未完成项、风险与环境限制（如实登记）

1. **Compose 的 build / up / health / down 本轮无法执行（阻塞级环境限制）。**
   `docker desktop start` 被沙箱安全策略拦下：`wsl.exe` 在程序黑名单里
   （`PROGRAM BLOCKED BY SECURITY POLICY`，明确说明「不可批准、不可绕过」），
   Docker Desktop 的 WSL2 后端因此起不来，`npipe:////./pipe/dockerDesktopLinuxEngine`
   不存在，`docker ps` / `docker volume ls` / `docker compose up` 全部失败。
   本轮**只**完成了 Compose 的**静态配置**校验（client 侧，不需要守护进程）。
   **§12.3 的七服务启动/健康检查结论来自上一轮，本轮没有复现，不作为本轮证据。**
   需要用户侧动作：把 `wsl.exe` 从 Security Center → Command Security → Program Blacklist 移除。
2. **浏览器 E2E 的形态必须如实区分。** 本轮是「**原生应用进程 + 原生隔离基础设施**」，
   **不是** Compose 浏览器闭环。基础设施也不是容器里的官方镜像：
   - MinIO 用 `dl.min.io/aistor/...` 与 2024 归档都是 **AIStor 构建**，无 license 时
     所有 S3 操作 `AccessDenied`（实测 `bucket_exists` 直接报 "No license is installed"），
     因此改用 `dl.minio.org.cn` 的 **AGPLv3 社区版** `RELEASE.2025-07-23T15-54-02Z`；
   - Redis 用 NuGet 包 `Redis-64` 的 **3.0.503**（GitHub release 下载在本机被代理 502 拦掉）。
     该版本与本轮 Celery 5.6.3 组合**实测可用**（心跳/投递/消费/重试全部真实跑通），
     但它不是 `redis:7-alpine`，不能据此宣称「与容器同版本已验证」；
   - PostgreSQL 用 EDB 便携 16.2，**与容器同大版本**。
3. `git diff --cached --check` 退出码 2：命中的是**既有暂存内容**里的尾随空格
   （`docs/00-项目总览与现况.md`，属 V0.1 归档前的暂存态）。本轮未 stage、
   未 commit、未改动该文件。
4. **`backend/tests/services/test_cost_config_bootstrap.py::test_command_has_no_default_price`
   在当前沙箱里会失败，但这是环境造成的、且不是本轮引入的。**
   该用例用 `subprocess.run(..., text=True)` 读一个受限 env 的子进程输出；
   沙箱给本会话注入了 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`，父进程按 UTF-8 解码，
   而子进程（受限 env 不继承这两个变量）按本机 ANSI 码页写出中文，于是
   `UnicodeDecodeError` → `stderr` 变成 `None` → `TypeError`。
   证据：同一份代码在 `env -u PYTHONUTF8 -u PYTHONIOENCODING` 下 **1484 passed**，
   单文件 23 passed。该用例不在本轮白名单（`backend/tests/e2e/**`）内，**未修改**。
5. 本轮**没有**触碰 ENG-03 的会话续签缺陷与 ENG-06 的 `model_name` 成本语义
   （§12.6 第 1、2 项）：两者都要改 `backend/app/**` 或已验收卡片语义，
   本轮白名单不含。**仍未修复，仍待总控裁决。**
6. 本轮**没有**修改 `docker/**`。上一轮给 `compose.yml` 加的隔离覆盖点、
   `compose.e2e.yml`、两个 `Dockerfile` 一字未动。

### 13.8 纪律核对

```text
保留既有改动     是（未 stage / 未 commit / 未 reset / 未 checkout / 未 clean / 未 stash）
禁止 Git 写操作  是（本轮只读 git status / git diff / git diff --cached）
未碰共享 ai_avatar 是（证明见 13.6）
真实 Provider     未调用（AI_PROVIDER=mock，且 only_mock_provider 夹具硬断言）
真实凭据/用户图片 未使用（合成邀请码、合成 PNG、合成邮箱、本地占位凭据 eng10-local-*）
只清理本轮资源    是（停掉的全是本轮自建、带 eng10r2 / eng10ui 前缀的进程）
未改 ENG-09 语义  是（分享/反馈/生命周期/点数/Provider/Storage 实现一字未动）
停线              是（不宣布 ENG-10 通过，不启动 ENG-11）
```

## 14. ENG-10 定向返修轮（2026-10-04，总控扩展授权）

> 追加而非覆盖：§1–§13 的结论仍然有效。本节只记录总控**扩展授权**范围内的两个
> 已证实一致性缺陷的修复、回归与门禁。**不自行宣布 ENG-10 通过，不启动 ENG-11。**

### 14.0 本卡当前状态（总控 2026-10-04 结论）

```text
原生链路：已验证（原生应用进程 + 原生隔离基础设施）
          —— 后端非 DB 1495 passed / 隔离 DB 140 passed / OpenAPI 66 passed /
             前端 181 passed + typecheck + build / 浏览器 E2E 17 passed 0 failed
Compose 动态门禁：**服务端闭环已补验通过；浏览器上传/取图/下载一段仍被环境阻断**
          —— 见 §15：镜像构建 / 空库迁移 / 定价+幂等 / 七服务健康 / 同源代理 /
             真实 Worker·Beat / 真实生成闭环（写对象存储 + 结算）全部通过；
             宿主侧预签名 URL 被沙箱网络拦截（§15.4），浏览器闭环这一段未验证
两个定向修复：已审核通过（会话事实一致性、型号定价规则一致性）
下一张卡：不启动 ENG-11
```

**不得把原生链路的结果表述成 Compose 闭环**：两者是不同拓扑（§13.7 第 2 项）。
Compose 侧的静态配置校验与动态补验结果见 §15。

### 14.1 授权范围与结论

总控授权只覆盖两个既有一致性缺陷：

1. 邮箱验证后，**会话查询与 UI 必须反映当前用户的验证事实**；
2. **创建任务 / 定价初始化 / readiness 必须采用一致的型号定价规则**：
   精确型号优先、`NULL` 兜底，缺配置仍按既有契约失败。

两者都已修复并复跑通过：后端非 DB **1495 passed**、隔离 DB **140 passed**、
OpenAPI **66 passed**（基线 sha256 未变）、前端 **181 passed / typecheck / build 通过**、
浏览器 E2E **17 passed / 0 failed**。

**未增加迁移、未修改 OpenAPI、未重构架构、未改变 ENG-09 语义。**

### 14.2 缺陷 1：会话事实一致性

**根因**：`SessionRecord.email_verified` 是**会话创建时的快照**，只在
`RedisSessionStore.create()`（邀请兑换）里写入；`POST /auth/email/verify` 只改
`user_emails`、**不续签会话**。前端 `useSession` 用 `session.email_verified` 决定
`signed-in` / `needs-verification`，于是「先兑换、再点验证链接」的用户上传与提交入口
永远锁着；而服务端授权读的是数据库 —— **接口能过、界面锁死**。

**最小修法（不重签 Cookie）**：让**会话查询**以数据库事实为准。

```text
app/services/auth/session_store.py   SessionRecord.to_api_model(*, email_verified=None)
                                     —— 允许用「当前事实」覆盖快照，默认行为不变
app/api/auth/router.py               GET /auth/session 注入 uow，
                                     用 uow.emails.find_verified_for_user(record.user_id)
                                     的当前事实覆盖响应里的 email_verified
```

**约束逐条核对**：

| 约束 | 落实方式 |
|---|---|
| 不得凭验证令牌为其他用户登录 | `/auth/email/verify` **完全没动**；会话查询只按 `record.user_id` 读**该用户自己**的验证事实，不读也不写任何 Cookie |
| 不得延长现有会话期限 | 响应里的 `expires_at` 仍来自 Redis 记录；回归里断言前后**逐字相等** |
| 不得复活已撤销会话 | `get_current_session` 未动：撤销的会话仍然 401（既有用例继续通过） |
| 不得改变 Cookie 安全属性 | 未触碰任何 `set_cookie` / Cookie 属性代码 |
| 不预设必须重新签发 Cookie | 没有重签：回归断言前后 `session_id`、`expires_at` 相同 |

**新增回归（`tests/api/test_auth_endpoints.py`）**：

- `test_session_reflects_the_verification_fact_after_email_verify`：验证后**同一会话**
  的 `/auth/session` 变为 `true`，且 `session_id` / `user_id` / `expires_at` 全部不变；
- `test_session_reports_the_database_fact_not_the_redis_snapshot`：快照说已验证、
  数据库没有已验证邮箱 → 必须报 `false`（事实双向权威）；
- `test_email_verify_does_not_unlock_another_users_session`：B 用自己的令牌验证后，
  B 解锁、A 仍为 `false`。

### 14.3 缺陷 2：型号定价规则一致性

**根因（两处，缺一不可）**：

- `SqlAlchemyGenerationCostConfigRepository.find_active` 在给了型号时用的是
  「精确型号 ∪ NULL，按 `effective_from` 取最新一条」——**谁新谁赢**，
  于是后来补的一条兜底价会把已有专价顶掉；
- `GenerationService.create_generation` 调 `resolve_cost(...)` **没传 `model_name`**，
  于是创建任务只剩 NULL 一条路，而 `/health/ready`（传了型号）与定价初始化却按
  「精确 ∪ NULL」判定 → **健康检查绿了、创建任务 503**（§12.6 第 2 项实测）。

**最小修法**：

```text
app/repositories/generation.py    find_active：先只查精确型号那一档；没有命中才回落 NULL；
                                  两者都没有 → None（调用方按既有契约 503 cost_config_missing）
app/services/generation/service.py create_generation 把 self._model_name 传给 resolve_cost
tests/services/fakes.py           内存实现同步对齐（它声明与 SQL 实现逐条一致）
```

`missing_cost_modes` 与 `/health/ready` 的 `_cost_config_probe` **本来就复用
`find_active`**，因此三处口径自动统一；**没有引入任何默认价格**。

**覆盖矩阵（返修要求逐项）**：

| 要求场景 | 用例 |
|---|---|
| 仅精确型号 | `test_find_active_prefers_the_exact_model_price`、`test_missing_modes_is_ready_when_only_the_exact_model_is_priced` |
| 仅 NULL | `test_find_active_falls_back_to_the_wildcard_price` |
| 两者并存且 NULL 更新 | `test_exact_model_price_survives_a_later_wildcard_update`（NULL 的 `effective_from` **更晚**） |
| 其他型号 | 同上（`another-model` → 兜底价） |
| 缺配置 | `test_missing_modes_reports_every_mode_when_nothing_is_priced`（三种取数口径都报同一缺口） |
| 冻结成本快照 | `test_create_prices_by_exact_model_and_freezes_that_config`、`test_create_falls_back_to_the_wildcard_price_when_the_model_is_unpriced` |
| readiness 同口径 | `test_cost_config_check_shares_the_exact_model_rule_with_generation_creation` |
| **真实 SQL 实现** | `tests/db/...::test_exact_model_price_wins_over_a_newer_wildcard_row_on_real_database`（真库上建两条、创建任务、快照、readiness 一次核对） |

### 14.4 E2E：把「验证后仍锁死」改成正确行为回归

- `tests/e2e/conftest.py::redeem_and_verify`：**删掉第二枚邀请码**，改成
  「一枚邀请码 → 邮箱验证 → **同一枚 Cookie** 再查 `/auth/session` 必须为 true」。
  `initial_points` 随之从 `points * 2` 改为 `points`，所有用例的断言都按真实初始点数走。
- `tests/e2e/test_registration_flow.py`：诊断用例
  `test_browser_email_verification_link_and_the_ui_lockout` 改为
  `test_browser_email_verification_unlocks_the_same_session`：
  **一枚邀请码 → 浏览器点「验证邮箱」→ 同一会话解锁 → 上传到 READY → 提交生成 →
  两张结果就绪 → 点数 100 扣掉一次 PHOTO_PROMPT 的 40**。
  截图 `verify-then-unlocked.png`（旧名 `verify-then-locked.png` 已不再产生）。
- `tests/e2e/test_full_loop_browser.py`：模块说明里「靠第二枚邀请码」的段落删除。

### 14.5 本轮门禁（命令与结果，全部为本轮真实输出）

```text
后端
  pytest tests --ignore=tests/db --ignore=tests/e2e -q      1495 passed（上一轮 1484，新增 11 条）
  pytest tests/db -q（隔离库 ai_avatar_eng10r2）              140 passed（上一轮 139，新增 1 条真库用例）
  pytest tests/api/test_openapi_contract.py tests/api/test_error_envelope.py -q   66 passed
  OpenAPI 基线 sha256  1b84fdc93029323b5d2fe268c032787168b7ad0ed4b22d1d1cb09d99c619909f（未改动）
  python -m compileall -q app tests alembic                 退出码 0
  git diff --check                                          退出码 0

前端
  vitest run --no-file-parallelism      21 files / 181 passed   退出码 0
  tsc --noEmit                          退出码 0
  next build                            退出码 0（7 条路由）

浏览器 E2E（真实 Chrome + CDP；原生应用进程 + 原生隔离基础设施）
  pytest tests/e2e -q                   17 passed / 0 failed in 236.75s
  取证：D:/tmp/eng10-run/r3-artifacts/（11 张，含 verify-then-unlocked.png）
  日志：D:/tmp/eng10-run/r3-run-final.log、r3-pytest-db.log、r3-pytest-e2e.log

Compose
  docker compose config（两个文件组合）  退出码 0（静态校验）
  build / up / health / down             **仍未执行**（见 14.7）
```

### 14.6 本轮改动文件（全部在授权范围内）

```text
缺陷 1（会话事实）
  backend/app/services/auth/session_store.py
  backend/app/api/auth/router.py
  backend/tests/api/test_auth_endpoints.py
  backend/tests/e2e/conftest.py
  backend/tests/e2e/test_registration_flow.py
  backend/tests/e2e/test_full_loop_browser.py
缺陷 2（型号定价）
  backend/app/repositories/generation.py
  backend/app/services/generation/service.py
  backend/tests/services/fakes.py
  backend/tests/services/test_cost_config_bootstrap.py
  backend/tests/services/test_generation_service.py
  backend/tests/api/test_health.py
  backend/tests/db/test_cost_config_bootstrap.py
文档
  docs/handoff/2026-10-02-eng09-eng10-execution.md
  docs/task-cards/ENG-10-local-regression.md
```

**未改动**：`shared/api-contract/openapi.json`、`backend/alembic/**`、`docker/**`、
两个 `Dockerfile`、`frontend/**`（本轮零改动）、ENG-09 的分享/反馈/生命周期实现。

### 14.7 剩余门禁：Compose 部署形态（完整命令、隔离与启动顺序）

**结论先行**：Compose 的 `build / up / health / down` **尚未执行**，这是本卡唯一的
未完成门禁。原生链路的结论**不能**代替它（两者是不同拓扑）。

#### 14.7.1 为什么必须显式覆盖网络名与卷名

`docker/compose.yml` 对网络和三个卷用的是**显式命名**，不是 Compose 默认的
「`<项目名>_<服务名>`」：

```yaml
networks:
  private:
    name: ${NETWORK_NAME:-ai-avatar-v1-private}
volumes:
  postgres_data: { name: ${POSTGRES_VOLUME:-ai-avatar-v1-postgres} }
  redis_data:    { name: ${REDIS_VOLUME:-ai-avatar-v1-redis} }
  minio_data:    { name: ${MINIO_VOLUME:-ai-avatar-v1-minio} }
```

因此**只给 `-p` 不足以隔离**：`-p` 只影响 Compose 默认命名的资源，显式 `name:` 的
网络与卷会**跨项目复用**，于是隔离栈会挂到默认本地环境的网络上、并写进它的三个卷。
必须同时用环境文件把 `NETWORK_NAME` 与三个 `*_VOLUME` 一起覆盖。

#### 14.7.2 隔离参数（全部经检查为空闲/不存在）

| 维度 | 隔离取值 | 检查方式 |
|---|---|---|
| 项目名 | `ai-avatar-eng10` | `docker ps -a --filter name=ai-avatar-eng10` 为空 |
| 网络 | `ai-avatar-eng10-private` | `docker network ls --filter name=ai-avatar-eng10` 为空 |
| 卷 | `ai-avatar-eng10-postgres` / `-redis` / `-minio` | `docker volume ls --filter name=ai-avatar-eng10` 为空 |
| 镜像 | `ai-avatar-backend:v1-eng10` / `ai-avatar-frontend:v1-eng10` | `IMAGE_TAG_SUFFIX=v1-eng10` |
| 宿主端口 | `15432 / 16379 / 19000 / 19001 / 18000 / 18001 / 13000` | 逐个 `Test-NetConnection`/端口探测为 closed |
| 环境文件 | 仓库根 `.env.eng10`（`.gitignore` 的 `.env.*` 已忽略） | 不提交 |

**资源归属检查（起栈前必须全为空）**

```text
docker ps -a  --filter "name=ai-avatar-eng10"   --format "{{.Names}}"
docker volume ls --filter "name=ai-avatar-eng10" --format "{{.Name}}"
docker network ls --filter "name=ai-avatar-eng10" --format "{{.Name}}"
docker image ls  "ai-avatar-*:v1-eng10"          --format "{{.Repository}}:{{.Tag}}"
```

**清理边界**：只删 `ai-avatar-eng10-*` 的容器/网络/卷、两个 `v1-eng10` 镜像与 `.env.eng10`。
**绝不触碰**：`ai-avatar-v1-*`（默认本地环境，含用户本地数据）、其他项目的运行中容器
（本机实测同时有 20+ 个：ecommerce / milvus / dify 等）、既有 `.pytest-*` 与 `.review-*` 目录。

#### 14.7.3 环境文件 `.env.eng10`（内容）

```dotenv
# --- 隔离命名与端口（缺一项就会复用默认环境的资源） ---
IMAGE_TAG_SUFFIX=v1-eng10
NETWORK_NAME=ai-avatar-eng10-private
POSTGRES_VOLUME=ai-avatar-eng10-postgres
REDIS_VOLUME=ai-avatar-eng10-redis
MINIO_VOLUME=ai-avatar-eng10-minio
POSTGRES_HOST_PORT=15432
REDIS_HOST_PORT=16379
MINIO_HOST_PORT=19000
MINIO_CONSOLE_HOST_PORT=19001
BACKEND_HOST_PORT=18000
BACKEND_E2E_HOST_PORT=18001
FRONTEND_HOST_PORT=13000

# --- 基础设施初始化（仅 Compose 使用） ---
POSTGRES_DB=ai_avatar
POSTGRES_USER=ai_avatar
POSTGRES_PASSWORD=eng10-local-pg
MINIO_ROOT_USER=eng10-local-access
MINIO_ROOT_PASSWORD=eng10-local-secret
# 预签名 URL 的 Host 来自 STORAGE_ENDPOINT：容器与宿主浏览器都要能解析。
# 宿主 hosts 里已有 `10.193.104.171 host.docker.internal`，因此 MinIO 必须绑到
# 0.0.0.0（只绑 127.0.0.1 时容器侧连不上）。
MINIO_HOST_BIND=0.0.0.0
STORAGE_ENDPOINT=http://host.docker.internal:19000

# --- 服务端配置（只注入 API / Worker / Beat） ---
APP_ENV=local
APP_BASE_URL=http://127.0.0.1:18000
DATABASE_URL=postgresql+psycopg://ai_avatar:eng10-local-pg@postgres:5432/ai_avatar
REDIS_URL=redis://redis:6379/0
STORAGE_DRIVER=minio
STORAGE_BUCKET=ai-avatar
STORAGE_REGION=us-east-1
STORAGE_ACCESS_KEY=eng10-local-access
STORAGE_SECRET_KEY=eng10-local-secret
SESSION_SECRET=eng10-local-session-secret
AI_PROVIDER=mock
AI_BASE_URL=
AI_MODEL=
AI_API_KEY=
AI_VISION_MODEL=
ENABLE_IMAGE_ANALYSIS=false
ENABLE_REFERENCE_IMAGE=false

# --- 前端构建期变量（改这些必须 --build 重建） ---
NEXT_PUBLIC_API_BASE_URL=/api/v1
NEXT_PUBLIC_APP_URL=http://127.0.0.1:13000
API_PROXY_TARGET=http://backend:8000
```

#### 14.7.4 分阶段启动（避免「先等 backend healthy、再初始化数据库」的死锁）

`compose.yml` 里 `frontend` 依赖 `backend: service_healthy`，而 `backend` 的健康检查打的是
`/api/v1/health/ready`，它的 `generation_costs` 探针**要求价格已初始化**。
若一条 `up -d` 起全部：数据库是空的 → `/ready` 永远 503 → `backend` 永远不 healthy →
`frontend` 永远不启动。**顺序必须是**：

```text
① 基础设施        postgres / redis / minio        并等到三个都 healthy
② 桶 + 迁移 + 定价 一次性容器执行（不参与健康门）    并复核幂等
③ 应用进程        backend / worker / beat          backend 此时才可能 healthy
④ readiness       curl /api/v1/health/ready 必须 200 {"status":"ok"}
⑤ 前端            frontend（依赖 backend healthy）  再验同源代理
```

#### 14.7.5 完整可执行命令

```text
# ---------- 0) 资源归属检查（必须全为空） ----------
docker ps -a      --filter "name=ai-avatar-eng10" --format "{{.Names}}"
docker volume ls  --filter "name=ai-avatar-eng10" --format "{{.Name}}"
docker network ls --filter "name=ai-avatar-eng10" --format "{{.Name}}"
# 端口检查：15432 / 16379 / 19000 / 19001 / 18000 / 18001 / 13000 全部无监听

# ---------- 1) 环境文件与静态校验 ----------
Copy-Item docker/.env.example .env.eng10        # 然后按 14.7.3 覆盖成隔离取值
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml config
# 复核：项目名 / 网络名 / 三个卷名 / 七个宿主端口 都必须是 eng10 取值

# ---------- 2) 镜像构建 ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml build
docker image ls "ai-avatar-*:v1-eng10" --format "{{.Repository}}:{{.Tag}} {{.Size}}"

# ---------- 3) 基础设施（只起三个） ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml up -d postgres redis minio
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml ps
# 期望：postgres / redis / minio 三个 healthy

# ---------- 4) 桶初始化（MinIO 镜像自带 mc，local 别名已在镜像里配好） ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml exec -T minio mc mb local/ai-avatar --ignore-existing

# ---------- 5) 空库迁移（一次性容器；不参与健康门，因此不会阻塞启动） ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml run --rm backend alembic upgrade head
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml run --rm backend alembic current
# 期望：0021_share_asset_links (head)

# ---------- 6) 显式定价（没有默认价格；必须逐项给出） ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml run --rm backend python -m app.bootstrap.cost_configs --price TEXT_PROMPT=30 --price PHOTO_PROMPT=40

# ---------- 7) 幂等复跑（部署脚本必须可无脑重跑） ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml run --rm backend alembic upgrade head
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml run --rm backend python -m app.bootstrap.cost_configs --price TEXT_PROMPT=30 --price PHOTO_PROMPT=40
# 期望：无新的 upgrade 行；「已存在，价格一致 / 已写入 0 条配置」

# ---------- 8) 应用进程（backend 此时才可能 healthy） ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml up -d backend worker beat
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml ps

# ---------- 9) readiness 就绪 ----------
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:18000/api/v1/health/ready
# 期望：200；body {"data":{"status":"ok"}}

# ---------- 10) 前端 + 同源代理 ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml up -d frontend
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:13000/api/v1/health/ready
# 期望：200（经 Next 服务端 rewrites 转发到 backend:8000）
# 并核对构建产物：frontend/.next/routes-manifest.json 的 rewrite = http://backend:8000/api/v1/:path*

# ---------- 11) 真实 Worker / Beat ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml logs --tail=50 beat
# 期望：Scheduler: Sending due task worker-heartbeat (worker.heartbeat) 每 15 秒
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml logs --tail=50 worker
# 期望：Task worker.heartbeat[...] succeeded、Task generation.run[...] succeeded
# 建议再核对 Redis 心跳键是否在前进（证明「Beat 投递 → 队列 → Worker 消费」三段连通）

# ---------- 12) 浏览器闭环（上传 / 取图 / 下载 / 主闭环） ----------
# 栈已起后，在**同一环境**里跑 E2E（邮件替身仍限于测试入口，见 14.7.6）：
#   ENG10_WEB_URL=http://127.0.0.1:13000
#   ENG10_API_URL=http://127.0.0.1:18001     （backend-e2e：邮件替身入口）
#   ENG10_DATABASE_URL=postgresql+psycopg://ai_avatar:eng10-local-pg@127.0.0.1:15432/ai_avatar
#   ENG10_REDIS_URL=redis://127.0.0.1:16379/0
#   ENG10_STORAGE_ENDPOINT=http://host.docker.internal:19000
#   pytest tests/e2e -q
# 先决条件：预签名 URL（上传 PUT 与结果下载）必须从**宿主浏览器/宿主进程**可达；
# 若 `host.docker.internal:19000` 在宿主不可达，则这一步在本机不可完成（见 14.7.7）。

# ---------- 13) 收尾 ----------
docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml down -v --remove-orphans
docker image rm ai-avatar-backend:v1-eng10 ai-avatar-frontend:v1-eng10
Remove-Item .env.eng10
# 复核：容器 / 网络 / 三个卷 / 两个镜像 / 七个端口 全部回到起栈前状态
```

#### 14.7.6 必须如实记录的拓扑与镜像版本

```text
拓扑（Compose 形态，与原生栈不是一回事）
  浏览器 ──同源──→ frontend(Next.js :3000 容器内 → 宿主 13000)
                     └─ Next 服务端 rewrites ─→ backend :8000（宿主 18000）
  backend / worker / beat  共用 ai-avatar-backend:v1-eng10（同一份代码、不同启动命令）
  backend-e2e              同一镜像、入口 tests.e2e.local_server:app（宿主 18001），
                           **仅测试用**：它只覆盖 get_email_port_dep 一个依赖，
                           把验证邮件写进宿主目录；路由/中间件/异常处理/配置/DB/Redis/
                           对象存储/Service/Repository/账务全部是生产实现。
                           `tests/` 被 backend/.dockerignore 排除、不进生产镜像，
                           因此以只读挂载提供，镜像本身一个字节未变。
  postgres :5432（宿主 15432） / redis :6379（宿主 16379） / minio :9000（宿主 19000）

镜像版本（起栈后用 `docker compose images` 与 `docker inspect` 记录**实际**摘要）
  ai-avatar-backend:v1-eng10   ← FROM python:3.13-slim
  ai-avatar-frontend:v1-eng10  ← FROM node:22-alpine（build + runtime 两阶段）
  postgres:16-alpine / redis:7-alpine
  minio/minio:latest           ← **未固定版本**；必须在报告里记下实际解析到的
                                  digest/RELEASE 号，不能只写 latest
```

#### 14.7.7 本机已知风险（如实登记，未验证前不得当成结论）

- 预签名 URL 的 Host 由 `STORAGE_ENDPOINT` 决定，而「容器内 SDK 能连」与
  「宿主浏览器能连」要求同一个主机名。本机宿主 hosts 里有
  `10.193.104.171 host.docker.internal`，理论上两端都能解析；但 §12.7 第 1 项记录过
  一次容器 → `host.docker.internal` 不可达（`Network is unreachable`）。
  **这一条必须实测**：若宿主侧或容器侧任一端不通，则「浏览器上传/取图/下载」在本机
  Compose 形态下不可完成，必须如实记为环境限制，**不得**用原生栈结果替代。
- `minio/minio:latest` 会在拉取时漂移；同一份 compose.yml 在不同时间可能拿到不同版本。
- 其它项目正在运行 20+ 个容器，端口与内存竞争可能影响启动时间；不得为此停别人的容器。

#### 14.7.8 其它未完成项

1. `git diff --cached --check` 退出码 2 命中的是**既有暂存内容**里的尾随空格
   （`docs/00-项目总览与现况.md`），非本轮引入。
2. `tests/services/test_cost_config_bootstrap.py::test_command_has_no_default_price`
   在本沙箱会因注入的 `PYTHONUTF8`/`PYTHONIOENCODING` 假失败（§13.7 第 4 项）；
   1495 passed 是在 `env -u PYTHONUTF8 -u PYTHONIOENCODING` 下取得的。
3. `redeem_and_verify` 修好后，`conftest.py` 里的 `top_up` 与 session 级 `account`
   夹具在本套 E2E 中已无调用点（保留为文档化的公共助手，未删除）。

### 14.8 纪律核对

```text
授权边界        只改了两个缺陷所需的 auth Service/依赖装配/路由、generation Service/成本仓储及相关测试
未增加迁移      是（backend/alembic/** 一字未动）
未改 OpenAPI    是（sha256 与基线逐字一致）
未改 ENG-09 语义 是（分享/反馈/生命周期实现未动）
未重构架构      是（无新增模块、无目录调整；新增的只有一个可选关键字参数与一段仓储查询）
Git             只读（未 stage / commit / reset / checkout / clean / stash）
共享 ai_avatar   未连接（证明见 §13.6：本轮原生 PG 实例里没有 ai_avatar 这个库）
真实 Provider/凭据/图片  未使用（AI_PROVIDER=mock + 合成数据 + eng10-local-* 占位凭据）
安全黑名单      未绕过、未要求用户关闭安全策略
停线            是（不宣布 ENG-10 通过，不启动 ENG-11）
```

## 15. Compose 部署形态补验（2026-10-04 追加，Docker 恢复可用）

> 追加而非覆盖。§14.7 描述的命令与启动顺序本轮**逐条实跑**，结果如下。
> **不自行宣布 ENG-10 通过，不启动 ENG-11。**

> ⚠️ 本节 15.1 / 15.4 / 15.7 记录的「宿主侧预签名 URL 不可完成」是**当时**的结论；
> **§16 已把它定位到确切原因（宿主 hosts 里 `host.docker.internal` 指向陈旧地址）
> 并解除**：改用两端都可达的宿主地址后，Compose 形态下完整 E2E 跑到
> **17 passed / 0 failed**。以 §16 为准。

### 15.1 结论

```text
镜像构建 / 空库迁移 / 显式定价+幂等 / 七服务健康 / Next 同源代理 /
真实 Worker·Beat / 真实生成闭环（含写对象存储与结算）  —— **全部通过**
宿主侧预签名 URL（浏览器上传 PUT 与结果下载）           —— **不可完成**
  原因：沙箱拦截宿主对自身 LAN IP 的连接，而预签名 URL 的 Host 只能取
        STORAGE_ENDPOINT 一个值（容器要 host.docker.internal、宿主只能 127.0.0.1）
浏览器侧：不依赖预签名 URL 的用例在 Compose 前端上**通过**；
          依赖上传/取图的用例在「等『已就绪』」处超时（即上面那一段）
```

因此 Compose 的**服务端闭环已补验通过**，**浏览器上传/取图/下载这一段仍未验证**，
必须如实记为环境限制，**不得**用原生栈结果替代（两者是不同拓扑）。

### 15.2 隔离参数（起栈前检查：五项全为空）

```text
项目名   ai-avatar-eng10
网络     ai-avatar-eng10-private
卷       ai-avatar-eng10-postgres / -redis / -minio
镜像     ai-avatar-backend:v1-eng10 / ai-avatar-frontend:v1-eng10
宿主端口 15432 / 16379 / 19000 / 19001 / 18000 / 18001 / 13000（全部无监听）
环境文件 仓库根 .env.eng10（.gitignore 的 .env.* 已忽略；收尾已删除）

docker compose --env-file .env.eng10 -p ai-avatar-eng10 -f docker/compose.yml config
  -> 退出码 0；解析结果：name=ai-avatar-eng10、网络与三个卷均为 eng10 取值、
     七个 published 端口与上表逐一对应
```

`-p` **不足以**隔离：compose.yml 的网络与三个卷是显式 `name:` 的，
必须同时用环境文件覆盖 `NETWORK_NAME` 与 `POSTGRES_VOLUME/REDIS_VOLUME/MINIO_VOLUME`
（§14.7.1）。本轮两者都做了，config 解析结果已核对。

### 15.3 分阶段执行结果（§14.7.4 的顺序）

```text
① 镜像构建
   docker compose ... build                       退出码 0
   ai-avatar-backend:v1-eng10   sha256:ccb4c0ea…  97.3MB  （FROM python:3.13-slim）
   ai-avatar-frontend:v1-eng10  sha256:663c4d80…  190MB   （FROM node:22-alpine 两阶段）

② 基础设施（只起三个）
   docker compose ... up -d postgres redis minio  退出码 0
   postgres 16-alpine  healthy 127.0.0.1:15432->5432
   redis 7-alpine      healthy 127.0.0.1:16379->6379
   minio RELEASE.2025-09-07T16-13-09Z healthy 0.0.0.0:19000->9000, 0.0.0.0:19001->9001

③ 桶初始化（用**应用自己的配置**，走真实 STORAGE_ENDPOINT）
   docker compose ... run --rm --no-deps backend python -c "<minio SDK>"
   -> bucket ready: ai-avatar via host.docker.internal:19000

④ 空库迁移
   docker compose ... run --rm --no-deps backend alembic upgrade head
   -> 0001…0021 逐条执行，alembic current = 0021_share_asset_links (head)
   幂等复跑 -> 0 条新的 upgrade 行

⑤ 显式定价（无默认价格）
   docker compose ... run --rm --no-deps backend python -m app.bootstrap.cost_configs \
     --price TEXT_PROMPT=30 --price PHOTO_PROMPT=40
   首次 -> 新增 2 条 / 已写入 2 条配置
   复跑 -> 已存在，价格一致 / 已写入 0 条配置

⑥ 应用进程
   docker compose ... up -d backend worker beat    退出码 0
   backend -> Up (healthy)   ← 它的健康检查就是 /api/v1/health/ready 200
   worker / beat -> Up

⑦ readiness
   宿主 -> http://127.0.0.1:18000/api/v1/health/ready
   -> 200 {"status": "ok"}（五项探针 postgresql/redis/object_storage/worker_heartbeat/
      generation_costs 全绿）

⑧ 前端 + 同源代理
   docker compose ... up -d frontend
   宿主 -> http://127.0.0.1:13000/api/v1/health/ready -> 200 {"status":"ok"}
   宿主 -> http://127.0.0.1:13000/ -> 200（真实页面）
   （经 Next 服务端 rewrites 转发到 backend:8000，不是浏览器直连后端）

⑨ 真实 Worker / Beat（容器日志为证）
   beat  ：Scheduler: Sending due task worker-heartbeat (worker.heartbeat)  每 15 秒
           Scheduler: Sending due task generation-recovery-scan (generation.recovery_scan)
   worker：Task worker.heartbeat[...] succeeded（ForkPoolWorker-15/16）
           generation_recovery_scan {'reenqueued': 0, 'needs_review': 0} succeeded

⑩ 真实生成闭环（TEXT_PROMPT，宿主进程调 Compose 栈；证据 c-compose-closure.json）
   一枚邀请码 → 邮箱验证 → 同一会话解锁
   POST /generations 202，reserved_points=30
   真实 Worker 消费 → SUCCEEDED / CONSUMED（0.5 秒）
   点数 100 → 70，reserved_points=0，lifetime_consumed=30
   point_ledger：RESERVE 30 → CONSUME 30
   两张结果对象写入 MinIO（4×4、76 字节）
   预签名下载地址：http://host.docker.internal:19000/ai-avatar/assets/...

⑪ 浏览器侧（Compose 前端）
   pytest tests/e2e -k "redeems_invite_from_the_real_gate or mobile_viewport_shows_the_same_gate"
   -> 2 passed（真实 Chrome 打 127.0.0.1:13000 的 Compose 前端）
   pytest ...::test_browser_email_verification_unlocks_the_same_session
   -> 1 failed in 201.97s：卡在等「已就绪」——浏览器的预签名 PUT 到
      http://host.docker.internal:19000/... 在宿主侧不可达，上传不进入 READY

⑫ 收尾
   docker compose ... down -v --remove-orphans  退出码 0（容器/网络/三个卷全部 Removed）
   docker image rm ai-avatar-backend:v1-eng10 ai-avatar-frontend:v1-eng10  已删除
   Remove-Item .env.eng10  已删除
```

### 15.4 阻断点的精确证据（宿主 → 预签名 URL）

```text
预签名 URL 的 Host 由 STORAGE_ENDPOINT 决定，而两侧要求不同：
  容器侧 SDK：host.docker.internal:19000  -> 200（实测，容器内 urllib 打 /minio/health/live）
  宿主侧     ：host.docker.internal 解析为 10.193.104.171（宿主 hosts 里就是这么写的）
               -> 裸 TCP connect 成功但**立刻被断开**（recv 返回 b''）
               -> urllib/httpx 报 RemoteProtocolError: Server disconnected
               -> 只有 127.0.0.1:19000 在宿主侧返回 200
对照实验（同一时刻、同一台机器）：
  10.193.104.171:19000  TCP 可连、无响应
  10.193.104.171:15432  TCP 可连、无响应   ← 该端口只绑了 127.0.0.1，本不该可连
  => 该地址被**沙箱的网络拦截层**接管，不是 Docker/MinIO 配置问题
```

结论：单个 `STORAGE_ENDPOINT` 无法同时满足「容器可达」与「宿主可达」。
**没有绕过安全策略、没有改 hosts、没有换用替代基础设施**；这一段如实记为未验证。

### 15.5 镜像版本与拓扑（实际记录）

```text
ai-avatar-backend:v1-eng10    sha256:ccb4c0ea58a6…   97.3MB   ← python:3.13-slim
ai-avatar-frontend:v1-eng10   sha256:663c4d8022e1…   190MB    ← node:22-alpine
postgres:16-alpine            721873c34ceb          116MB
redis:7-alpine                ff02b58f971e          16.3MB
minio/minio:latest            14cea493d9a3  → 实际 RELEASE.2025-09-07T16-13-09Z
                              digest sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e
                              （`latest` 会漂移，必须记 digest；见 §14.7.6）

拓扑：frontend(13000→3000) ──同源 rewrites──→ backend(18000→8000)
      backend / worker / beat 共用 ai-avatar-backend:v1-eng10
      backend-e2e(18001→8000) 同一镜像 + tests.e2e.local_server:app，**仅测试入口**
      postgres(15432) / redis(16379) / minio(19000)
```

### 15.6 资源清理与边界核对（收尾后实测）

```text
ai-avatar-eng10-*   容器 0 / 卷 0 / 网络 0 / 镜像 0    （过滤结果全为空）
.env.eng10          已删除
七个宿主端口        15432 / 16379 / 19000 / 19001 / 18000 / 18001 / 13000 全部无监听
未触碰              ai-avatar-v1-* 三个容器仍为 Exited、三个卷仍在（默认本地环境，含用户数据）
                    其它项目 21 个运行中容器原样（ecommerce / milvus / dify 等）
```

### 15.7 仍未完成 / 风险

1. **浏览器上传 / 取图 / 下载这一段在 Compose 形态下未验证**（§15.4 的环境阻断）。
   获准环境里应执行：`pytest tests/e2e -q`（环境变量见 §14.7.5 第 12 步），
   预期 17 passed / 0 failed；若宿主能解析 `host.docker.internal` 到可路由地址，
   这一段即可完成。
2. `minio/minio:latest` 未固定版本：本轮解析到 `RELEASE.2025-09-07T16-13-09Z`，
   换机器/换时间可能不同。是否把 compose.yml 固定到具体 digest 属部署决策，本轮未改。
3. `tests/services/test_cost_config_bootstrap.py::test_command_has_no_default_price`
   在本沙箱因注入的 `PYTHONUTF8`/`PYTHONIOENCODING` 假失败（§13.7 第 4 项），与 Compose 无关。
4. `git diff --cached --check` 退出码 2 命中的是既有暂存内容的尾随空格，非本轮引入。

## 16. Compose 浏览器闭环补验（2026-10-04 追加）：双执行模式 + 真实预签名 PUT/GET

> 追加而非覆盖。§15 里「宿主侧预签名 URL 不可达」的阻断，本轮**定位到确切原因并解除**；
> Compose 形态下的**完整 E2E 已跑到 17 passed / 0 failed**。
> **不自行宣布 ENG-10 通过，不启动 ENG-11。**

### 16.1 存储端点：DNS / 代理 / 可达性核查（第 1 项）

**结论：不是沙箱拦截，也不是 Docker/MinIO 配置问题，而是宿主 hosts 里的一条陈旧条目。**

```text
宿主 IPv4 地址（socket.getaddrinfo(hostname)）：172.17.80.1 / 172.28.213.171 / 198.18.0.1
宿主 hosts：host.docker.internal -> 10.193.104.171
            ↑ 该地址**不属于本机任何接口** —— 陈旧条目
容器内解析：host.docker.internal -> 192.168.65.254（Docker VM 网关）
代理：本轮脚本已清空 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY（宿主 registry 代理不影响裸 TCP 实验）
```

逐地址实测（端口 19000、`GET /minio/health/live`）：

| 地址 | 宿主 | 容器 |
|---|---|---|
| `127.0.0.1` | 200 | 拒绝（是容器自己） |
| `172.17.80.1` | 200 | 超时 |
| **`172.28.213.171`** | **200** | **200** |
| **`198.18.0.1`** | **200** | **200** |
| `host.docker.internal` | TCP 可连但**无响应**（`recv` 返回 `b''`） | 200 |

**没有把推测写成结论**：`10.193.104.171` 上「TCP 可连但无响应」这一现象，
在确认它不属于本机接口之后有了确定解释——请求被发到了**别的设备**；
对照实验（只绑 `127.0.0.1` 的 15432 在该地址上也「可连」）也指向同一结论。

**处置（纯配置，不绕过任何安全策略、不改 hosts、不改写签名 URL）**：
把 `STORAGE_ENDPOINT` 与 `MINIO_HOST_BIND` 都取 `172.28.213.171`（本机 LAN 地址），
使预签名 URL 的 Host 在**容器与宿主两侧都可达**，且 MinIO 只在这一个接口上暴露。

### 16.2 真实预签名 PUT / GET（第 1 项）

宿主侧用服务端签出的**原始 URL**（未做任何改写）执行（脚本 `presign_probe.py`，
证据 `D:/tmp/eng10-run/c-presign-probe.json`）：

```text
presigned_put_host   172.28.213.171:19000
query_keys           X-Amz-Algorithm / Credential / Date / Expires / Signature / SignedHeaders
host_dns             172.28.213.171
PUT                  200
complete             200   asset_status=READY
download-url         200   presigned_get_host=172.28.213.171:19000
GET                  200   10870 bytes   sha256_match=true
```

即：**上传（PUT）、取图（GET）、下载都在宿主侧真实跑通**，字节与原文件逐位一致。

### 16.3 E2E 的双执行模式（第 2 项）

`ENG10_STACK_MODE` **显式**取值 `native` | `compose`（未设默认 `native`，非法值直接失败）。
两种模式的**业务断言完全一致**，差别只有两处：

| 关注点 | `native` | `compose` |
|---|---|---|
| 停/起 Worker | `scripts/eng10_ui_stack.py stop-worker/start-worker`（原生进程栈） | `scripts/eng10_compose_stack.py stop-worker/start-worker`，**只**操作 `ENG10_COMPOSE_PROJECT`（默认 `ai-avatar-eng10`）里的 worker 容器 |
| 栈身份核对 | 两个入口 readiness + 同库兑换 + 邮件落盘 | 再加一层容器归属核对 |

**两种模式不会调用对方的脚本**：`test_billing_and_lifecycle.py` 里原先直接指向
`eng10_ui_stack.py` 的辅助函数**已删除**，改为经 `conftest.stop_scenario_worker /
start_scenario_worker` 按模式分派；Compose 模式绝不碰原生栈启停脚本。

**停机/恢复的确凿性**：

- `eng10_compose_stack.py` 的项目护栏：拒绝 `ai-avatar-v1`（默认本地环境，承载用户数据）、
  拒绝非 `ai-avatar-*` 前缀、每步都带 `-p <project>`、操作前核对容器名以 `<project>-` 开头；
- `stop-worker`：停完**核对容器状态**（不再是 `running`）才返回 0；
- `start-worker`：起来后**等日志出现 Celery 的 `ready.`** 才返回 0；
- E2E 侧 `finally` **无条件**恢复；恢复失败本身可见，但**不吞掉正在抛出的原始失败**。

### 16.4 栈身份核对（第 3 项）

新增 session 级 `stack_identity` 夹具，用**可核验的事实**证明「浏览器入口 / 测试入口 /
DB / Redis / MinIO 属于同一个隔离栈」，任一条不成立直接失败（不静默跳过）：

1. 两个入口 `/api/v1/health/ready` 都必须 200（该探针覆盖 postgresql / redis /
   object_storage / worker_heartbeat / generation_costs 五项）；
2. **浏览器入口经 Next 代理打到同一个库**：在断言用的库里种一枚新邀请码，
   经 `WEB_URL/api/v1/auth/invites/redeem` 兑换必须 201 —— 代理若指向别的栈，
   那枚码在对面不存在，只会得到 `INVITE_INVALID`；
3. **测试入口是邮件替身且写进我们读的目录**：经 `API_URL` 兑换另一枚码 →
   `POST /auth/email/request` → 断言 `ENG10_MAIL_DIR` 里出现该收件人的新邮件；
4. Compose 模式再加一层：本项目必须存在 worker 容器，且容器名以 `<项目名>-` 开头。

**浏览器邮件请求的实际入口**：`EntryPanel` 的「发验证邮件」按钮会从浏览器打
`/api/v1/auth/email/request`。E2E 覆盖文件把 Next 的同源代理指向 `backend-e2e`
（测试入口），因此**浏览器侧与宿主侧走的是同一个入口、同一个库/Redis/MinIO**，
只是入口模块不同（`tests.e2e.local_server:app` 只覆盖 `get_email_port_dep` 一个依赖）。

> 踩坑记录：`compose.e2e.yml` 里若写成 `${API_PROXY_TARGET:-http://backend-e2e:8000}`，
> 会被 `.env.eng10` 里为「七服务形态」准备的 `http://backend:8000` 顶掉，
> 于是 E2E 形态下浏览器仍打生产入口（实测：manifest 里仍是 `backend:8000`）。
> 现在直接写容器网络内的服务名。

### 16.5 两种模式下的完整 E2E（第 4 项）

```text
Compose 栈（docker compose -p ai-avatar-eng10，含 backend-e2e 覆盖）
  pytest tests/e2e -q          17 passed / 0 failed  in 286.24s
  取证：D:/tmp/eng10-run/c-artifacts/（12 张，含 presign-roundtrip.png、verify-then-unlocked.png）
  日志：D:/tmp/eng10-run/c9-compose-e2e.log

原生栈（scripts/eng10_ui_stack.py + 原生隔离基础设施）
  pytest tests/e2e -q          17 passed / 0 failed  in 247.59s
  取证：D:/tmp/eng10-run/n-artifacts/（12 张）
  日志：D:/tmp/eng10-run/n-native-e2e.log
```

覆盖范围（两种模式相同，无跳过）：邀请码门禁与兑换、验证后**同一会话解锁**、
移动端视口门禁与入口、桌面端完整闭环（点数→上传→生成→结果→**取图**→**下载**→分享→
匿名分享页→反馈）、移动端闭环、成功消费、明确失败释放、UNKNOWN 保留预留、
重复提交、重复结算、跨进程重复终态、权限拒绝、恢复扫描、分享/反馈幂等、生命周期清理。

**唯一为适配 Compose 改动的断言**：`test_full_loop_browser` 里「下载链接指向存储端点」
原先硬编码 `http://127.0.0.1:19000/`（原生栈的端点），现改为从
`ENG10_STORAGE_ENDPOINT` 读——**断言没有放宽**，仍要求链接来自本栈配置的存储端点。

### 16.6 本轮改动文件

```text
docker/compose.e2e.yml                         STORAGE_ENDPOINT / APP_BASE_URL 改为可覆盖；
                                               frontend 代理指向 backend-e2e（构建期值）；
                                               顶部说明补上 E2E 入口拓扑
scripts/eng10_compose_stack.py                 新增：Compose 模式 Worker 控制面（含项目护栏、
                                               停机状态核对、启动就绪等待）
backend/tests/e2e/conftest.py                  新增 ENG10_STACK_MODE 与模式分派、
                                               stack_identity 栈身份核对夹具
backend/tests/e2e/test_billing_and_lifecycle.py  only_scenario_worker 改走模式分派；
                                               删除指向原生脚本的本地辅助函数
backend/tests/e2e/test_full_loop_browser.py     下载链接断言改为读取配置的存储端点
docs/handoff/2026-10-02-eng09-eng10-execution.md 本节
docs/task-cards/ENG-10-local-regression.md     状态与摘要
```

**未改动**：`backend/app/**`、`backend/alembic/**`、`shared/api-contract/openapi.json`、
`frontend/**`、`docker/compose.yml`、两个 `Dockerfile`。

### 16.7 资源状态（收尾后实测）

```text
Compose：ai-avatar-eng10-* 容器 / 网络 / 三个卷 / 两个镜像   全部删除（过滤结果为空）
.env.eng10                                                  **保留**（见下方说明）
原生：PostgreSQL / Redis / MinIO / API / Worker / Beat / Frontend  全部停止
端口 15432 / 16379 / 19000 / 19001 / 18000 / 18001 / 13000 / 13010  全部无监听
未触碰：ai-avatar-v1-*（默认本地环境，含用户数据）与其它项目 21 个运行中容器
Git：只读（未 stage / commit / reset / checkout / clean / stash）
```

> **关于 `.env.eng10` 的「已删除 / 保留」口径（消除文档矛盾）**
>
> - §12 那一轮**创建后又删除**了它；§15 那一轮同样**创建后又删除**（§15.2 / §15.3 / §15.6
>   的「已删除」记录的是**当时**的事实，不是笔误）；
> - **§16 起改为保留**：它是 Compose 隔离栈的**环境文件**，没有它就无法按 §14.7.5 复跑；
>   它已被 `.gitignore` 的 `.env.*` 忽略，只含本地占位凭据（`eng10-local-*`），不构成泄漏面。
> - **当前实际状态 = 保留**。唯一需要留意的是里面的
>   `MINIO_HOST_BIND` / `STORAGE_ENDPOINT` 是**本机当前网段**的地址
>   （`172.28.213.171`）；换机器或换网段必须先按 §16.1 的方法重新取值并做双向探测。

### 16.8 仍未完成 / 风险

1. **`.env.eng10` 里的 `172.28.213.171` 是"当前机器当前网段"的取值**：换机器或换网段
   必须重新取值并做双向探测（§16.1 的表就是探测方法）。是否把这个地址做成自动发现
   属部署决策，本轮未做。
2. `host.docker.internal` 在本机 hosts 里仍指向陈旧地址：**未修改 hosts**
   （系统级改动需用户授权）。本轮的处置是选一个两端都可达的真实地址。
3. `minio/minio:latest` 未固定版本（§15.7 第 2 项）。
4. 后端非 DB / 隔离 DB / OpenAPI / 前端等门禁沿用 §14.5 的本轮结果；
   本轮只改测试与编排，未触碰 `backend/app/**`，因此未重跑后端全量门禁。

## 17. LEAD-06 两项测试缺口的返修（2026-10-04 追加）

> 追加而非覆盖。总控 LEAD-06 审查发现两项测试缺口，本轮按授权修复。
> **未改业务实现、迁移、冻结契约**；不自行宣布通过，不启动 ENG-11。

### 17.1 缺口一：丢失投递恢复用例（重写）

#### 旧用例为什么「通过」但什么都没证明

旧用例**没有停 Worker**，于是：任务创建后队列里的 `generation.run` 被**真实 Worker**
正常消费并跑完；用例再把任务状态写成 `QUEUED`、把 `provider_attempts` 删掉，
然后跑一次 `recovery_scan` —— 而扫描**按 `created_at` 判定陈旧**（阈值 120 秒），
刚创建的任务根本不在窗口内，`reenqueued` 是 **0**。
也就是说：`wait_generation` 看到的 `SUCCEEDED` 来自**真实 Worker**，
恢复路径一次都没被走到。这属于「断言存在、覆盖不存在」。

#### 新用例的构造（逐条对应返修要求）

| 要求 | 落实 |
|---|---|
| 创建任务前确认本轮 Worker 已停止 | `with worker_stopped():` —— 停机及其核对都在恢复保护内 |
| 使用隔离队列，确定原始投递已移除 | 停机后清空队列（无消费者，只清积压的计划任务），断言**没有任何 `generation.run` 投递**；创建任务后断言队列里**恰好一条**本任务的消息，再 `LREM` 掉它并断言**归零** |
| 不删除 `provider_attempts` | 一行都不删；断言它**本来就是 0 行** |
| 不把已完成任务改回 `QUEUED` | 不写 `status`；断言任务**本来就是 `QUEUED`** |
| 断言任务尚未执行 | `status == QUEUED`、`provider_attempts == []`、流水只有 `RESERVE` |
| 断言扫描实际补投 | 只把 `created_at` 推前（进入 120s 陈旧窗口）→ 手动扫描 → 断言 `reenqueued >= 1` 且队列里**恰好一条**本任务消息 |
| 避免 Beat 与手动扫描互相干扰 | Beat 的消息没有 Worker 消费、不会执行；补投后把 `created_at` 归位，周期性扫描不会再判它陈旧；任务一旦被领取就离开 `QUEUED` |
| 恢复后验证终态与唯一结算 | `SUCCEEDED + CONSUMED`、`_ledger == [RESERVE, CONSUME]`、`_attempt_status == [(1, "SUCCEEDED")]`、`generation_tasks` 该用户**只有 1 行** |

新增的读写手段在 `tests/e2e/conftest.py`：`CeleryQueue`（按前缀扫描 Redis 列表，
只做 `messages / messages_for / remove / drain`，**不碰任何业务表**）与
`worker_stopped()` 上下文管理器。

**证据**：针对性单跑（Compose 形态）

```text
pytest tests/e2e/test_billing_and_lifecycle.py -k recovery_scan -v -s
  [eng10-e2e] 栈身份核对通过：mode=compose project=ai-avatar-eng10 ...
  [eng10-e2e] 已清空队列 0 条积压消息
  1 passed in 19.57s
```

### 17.2 缺口二：Worker 控制加固

`scripts/eng10_compose_stack.py` 与 `conftest.worker_stopped()` 逐条落实：

| 要求 | 落实 |
|---|---|
| 停止及其检查纳入恢复保护 | `worker_stopped()` 把 `stop_scenario_worker()` **及其 returncode 断言**都放在 `try` 里 |
| 停止部分成功 / 检查失败 / 用例失败也要恢复 | 三种情况都进 `finally`，恢复动作必然执行 |
| 保留原始异常，同时报告恢复失败 | 恢复失败时先看 `sys.exc_info()`：有异常在飞就只打印，没有才抛出 |
| 启动前核对目标容器归属 | `start-worker` 先 `worker_containers()` + `_assert_owned()` 再启动 |
| 用本次启动的时间边界验证就绪 | `docker inspect .State.StartedAt` 作为边界，`logs --since <边界>` 只认这条边界**之后**的 `ready.` —— 历史日志不再可能被误判 |
| `ps` / `logs` 非零退出必须明确失败 | 统一走 `_require_ok()`，绝不当成「空结果 = 成功」 |

### 17.3 文档矛盾修正

`.env.eng10` 在 §12 / §15 两轮**创建后删除**，§16 起**改为保留**（它是复跑 Compose
门禁必需的环境文件，且已被 `.gitignore` 的 `.env.*` 忽略、只含本地占位凭据）。
§16.7 原先写「已删除」与事实不符，已改为**保留**并加一段口径说明，
把「哪一轮删了、哪一轮保留」写清楚，避免再被读成自相矛盾。

### 17.4 两种形态的完整 E2E（本轮复跑）

```text
Compose 形态（-p ai-avatar-eng10 + compose.e2e.yml，隔离网络/卷/端口）
  pytest tests/e2e -q          17 passed / 0 failed  in 183.94s
  日志：D:/tmp/eng10-run/c12-compose-e2e.log
  针对性用例：c11-targeted.log（1 passed in 19.57s）

原生形态（scripts/eng10_ui_stack.py + 原生隔离基础设施）
  pytest tests/e2e -q          17 passed / 0 failed  in 152.83s
  日志：D:/tmp/eng10-run/n-native-e2e.log
```

覆盖范围与前轮一致且**无跳过**：桌面/移动端、上传、取图、下载、分享、匿名分享页、
反馈，以及成功消费 / 明确失败释放 / UNKNOWN 保留预留 / 重复提交 / 重复结算 /
跨进程重复终态 / 权限拒绝 / **恢复扫描** / 分享与反馈幂等 / 生命周期清理。

### 17.5 本轮改动文件

```text
backend/tests/e2e/test_billing_and_lifecycle.py  重写恢复用例；新增 _task_status/_task_count/
                                                 _run_recovery_scan；only_scenario_worker 改走
                                                 conftest.worker_stopped
backend/tests/e2e/conftest.py                    新增 worker_stopped()（停机在恢复保护内、
                                                 保留原始异常）与 CeleryQueue；导出清单同步
scripts/eng10_compose_stack.py                   启动前核对归属；StartedAt 作为就绪边界；
                                                 ps/logs 非零退出明确失败
docs/handoff/2026-10-02-eng09-eng10-execution.md 本节 + §16.7 的 .env.eng10 口径修正
```

**未改动**：`backend/app/**`、`backend/alembic/**`、`shared/api-contract/openapi.json`、
`frontend/**`、`docker/**`。

### 17.6 资源状态

```text
Compose：容器 / 网络 / 三个卷 / 两个镜像   全部删除（过滤结果为空）
原生：PostgreSQL / Redis / MinIO / API / Worker / Beat / Frontend  全部停止
端口 15432 / 16379 / 19000 / 19001 / 18000 / 18001 / 13000 / 13010  全部无监听
未触碰：ai-avatar-v1-*（默认本地环境，含用户数据）与其它项目运行中容器
保留：.env.eng10（隔离环境文件，供复跑；地址取值需按 §16.1 重新探测）
Git：只读（未 stage / commit / reset / checkout / clean / stash）
```

## 18. `worker_stopped` 恢复调用的异常处理（2026-10-04 最后一处定向修复）

> 追加而非覆盖。本轮只改**测试基建的异常处理**，成功路径行为不变；
> 因此**未重新构建镜像、未重复全量 E2E**（已有两种形态的 E2E 证据保留在 §16/§17）。
> 不自行宣布通过，不启动 ENG-11。

### 18.1 修的是什么

`conftest.worker_stopped()` 原先在 `finally` 里直接调用恢复函数：

```python
finally:
    started = start_scenario_worker()          # ← 这一行本身可能抛异常
    if started.returncode != 0:
        if sys.exc_info()[0] is not None: print(...)
        else: raise AssertionError(...)
```

两个漏洞：

1. **恢复调用本身抛异常时没人接**：`subprocess.run(..., timeout=...)` 会抛
   `TimeoutExpired`，脚本/解释器起不来会抛 `OSError`。这类异常直接从 `finally`
   往外抛，会**把正在传播的原始失败顶掉** —— 排查方向被带到「恢复脚本超时」，
   而真正的失败原因（用例为什么红）丢了；
2. `sys.exc_info()` 是**现场读**的，没有在进入 `finally` 时先固定下来。

### 18.2 改法

```python
def _restore_worker(start_call) -> str | None:
    """恢复本栈 Worker：成功返回 None，失败返回**可读的失败说明**（绝不抛异常）。"""
    try:
        started = start_call()
    except Exception as error:                      # TimeoutExpired / OSError / 其它
        return f"恢复 Worker 失败（调用异常）：{type(error).__name__}: {error}"
    if started.returncode != 0:
        return f"恢复 Worker 失败（退出码 {started.returncode}）：{started.stdout}{started.stderr}"
    return None

@contextmanager
def worker_stopped(*, stop=None, start=None):
    ...
    finally:
        pending = sys.exc_info()                    # ← 进入 finally 先固定原始异常状态
        failure = _restore_worker(start_call)
        if failure is not None:
            if pending[0] is not None:
                print(f"[eng10-e2e] {failure}（原始失败优先，请先看上面的报错）", flush=True)
            else:
                raise AssertionError(failure)       # ← 没有原始异常就必须让测试失败
```

| 要求 | 落实 |
|---|---|
| 进入 `finally` 时保存原始异常状态 | `pending = sys.exc_info()` 是 `finally` 的第一件事 |
| 恢复调用要处理**非零返回** | `returncode != 0` → 失败说明 |
| 恢复调用要处理 **TimeoutExpired / OSError 等调用异常** | `except Exception` 接住并转成失败说明（`BaseException` 如 `KeyboardInterrupt` 仍照常传播） |
| 有原始异常：保留它为主异常 + 明确记录恢复失败 | 只打印，不抛；原始异常照常向上传播 |
| 没有原始异常：恢复失败必须让测试失败 | 抛 `AssertionError(failure)` |
| 成功路径不变 | `stop` / `start` 只是**注入点**，默认值就是真实控制面；正常路径逐字未改 |

### 18.3 故障注入回归（**不启动任何真实资源**）

新增 `backend/tests/e2e_control/`（与 `tests/e2e/` 分开：那个目录的 `autouse` session
夹具会强制要求一整套栈，放在那里就违背「不启动真实资源」）：

```text
pytest tests/e2e_control -q -v
  tests/e2e_control/test_worker_stopped.py .......        [100%]
  7 passed in 0.61s
```

| # | 用例 | 覆盖 |
|---|---|---|
| 1 | `test_stop_check_failure_still_attempts_restore` | 停止部分成功（非零退出）→ 主体不执行，**仍尝试恢复**（`calls == ["stop", "start"]`） |
| 2 | `test_body_failure_keeps_original_exception_and_reports_restore_failure` | 用例失败 + 恢复非零 → 原始 `RuntimeError` 是主异常，恢复失败被打印 |
| 3 | `test_body_failure_with_restore_call_exception_keeps_original_exception` | 用例失败 + 恢复抛 `TimeoutExpired` → 原始异常保留，`TimeoutExpired` 被打印 |
| 3b | `test_body_failure_with_restore_oserror_keeps_original_exception` | 同上，`OSError` 分支 |
| 4 | `test_restore_nonzero_without_original_exception_fails_the_test` | 用例成功 + 恢复非零 → **必须失败** |
| 4b | `test_restore_call_exception_without_original_exception_fails_the_test` | 用例成功 + 恢复抛异常 → **必须失败** |
| 5 | `test_normal_stop_and_restore_is_silent` | 正常路径：`stop → body → start` 顺序正确且无失败输出 |

### 18.4 回归结果

```text
针对性（纯逻辑，无资源）      pytest tests/e2e_control -q            7 passed in 0.61s
非 DB 全量门禁                pytest tests --ignore=tests/db --ignore=tests/e2e -q
                              1502 passed（上一轮 1495，新增 7 条）
                              `--ignore=tests/e2e` **不会**漏掉 tests/e2e_control（实测收集到 7 条）
E2E 收集                      pytest tests/e2e --collect-only -q      17 tests（未变）
针对性实跑（原生栈，证明成功路径未变）
  pytest tests/e2e -k "recovery_scan or definite_failure or unknown_outcome
                       or duplicate_settlement or duplicate_terminal"
                              5 passed, 12 deselected in 52.32s
                              —— 同时覆盖 worker_stopped 的两个使用者
                                 （重写的恢复用例 + only_scenario_worker 系列）
  日志：D:/tmp/eng10-run/n-targeted.log、n-targeted-run.log
```

**未重跑**：镜像构建与两种形态的全量 E2E（按总控口径，只改异常处理且成功路径不变）。
§16 / §17 的 Compose 17 passed、原生 17 passed 证据继续有效。

### 18.5 本轮改动文件

```text
backend/tests/e2e/conftest.py                      worker_stopped 的恢复调用异常处理；
                                                   抽出 _restore_worker；增加 stop/start 注入点
backend/tests/e2e_control/__init__.py              新增（纯逻辑回归测试包）
backend/tests/e2e_control/test_worker_stopped.py   新增（7 条故障注入用例）
docs/handoff/2026-10-02-eng09-eng10-execution.md   本节
docs/task-cards/ENG-10-local-regression.md         状态与摘要
```

**未改动**：`backend/app/**`、`backend/alembic/**`、`shared/api-contract/openapi.json`、
`frontend/**`、`docker/**`、`scripts/**`。

### 18.6 资源状态

```text
原生栈：PostgreSQL / Redis / MinIO / API / Worker / Beat / Frontend  全部停止
Compose：本轮未启动（沿用 §17 收尾后的状态：容器/网络/卷/镜像全为空）
端口 15432 / 16379 / 19000 / 19001 / 18000 / 18001 / 13000 / 13010  全部无监听
未触碰：ai-avatar-v1-*（默认本地环境，含用户数据）与其它项目运行中容器
保留：.env.eng10（隔离环境文件，供复跑；地址取值需按 §16.1 重新探测）
Git：只读（未 stage / commit / reset / checkout / clean / stash）
```
