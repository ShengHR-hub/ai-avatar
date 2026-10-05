# ENG-10：本地完整闭环与回归测试

> 前置：`ENG-03` 至 `ENG-09` 通过（ENG-09 由总控技术验收通过，语义不得再改）
> 状态：**原生与 Compose 两种形态下，完整 E2E 都已跑到 17 passed / 0 failed**。
>       - 原生形态（原生应用进程 + 原生隔离基础设施）：17 passed / 0 failed（247.59s）
>       - Compose 形态（独立项目/网络/卷/镜像/端口，含 `backend-e2e` 测试入口）：
>         17 passed / 0 failed（286.24s）—— 覆盖桌面/移动端、上传、取图、下载、分享、
>         反馈与异常矩阵，无跳过。
>       - 总控扩展授权内的两个一致性缺陷（会话事实、型号定价）已修复并回归。
>       - §15 记录的「宿主侧预签名 URL 不可达」已定位为宿主 hosts 陈旧条目，
>         改用两端都可达的宿主地址后解除（交接文档 §16）。
>       执行记录见 `docs/handoff/2026-10-02-eng09-eng10-execution.md` §12–§16。
> 放行权：总控（按 LEAD-06 最终验收，再按 LEAD-07 放行）。本卡不自行宣布通过。

## 会话目的

在**独立**的 Compose 项目、网络、卷与空闲端口上，验证
`PostgreSQL / Redis / MinIO / API / Worker / Beat / Frontend` 的完整 Mock 闭环；
补齐 `docker/**` 与两个 `Dockerfile`，并用真实浏览器覆盖桌面与移动端的关键流程。

## 白名单

```text
docker/**               编排、环境模板与 E2E 覆盖文件
backend/Dockerfile      后端镜像（api/worker/beat 共用）
frontend/Dockerfile     前端镜像（两阶段构建）
frontend/next.config.mjs  仅在部署形态必要时调整（本轮未改）
scripts/**              仅编排辅助（不承载业务规则）
backend/tests/**        测试（含 E2E）
frontend/tests/**       测试
docs/handoff/**         执行记录
docs/task-cards/ENG-10-local-regression.md  本文件
```

## 数据与凭据边界

- 只用**自建隔离资源**与**合成数据**：合成邀请码、合成 PNG、合成邮箱；
- `AI_PROVIDER=mock`，**禁止**真实 Provider、真实 API Key、真实用户图片；
- 共享开发库 `ai_avatar` **不连接、不迁移**；
- 其他项目的容器、卷、进程、端口**不触碰**。

## 必须覆盖

```text
Compose config（七服务、网络、卷、健康检查、构建期前端变量）
镜像 build（backend / frontend，含 .dockerignore 边界）
七服务启动 + 健康检查 + 停止
空库迁移（upgrade head，并复核幂等）
后端非 DB 测试 / 隔离 DB 测试
真实进程 Worker / Beat 验证
真实浏览器 E2E（桌面 + 移动端关键流程）
OpenAPI 一致性 / compileall / git diff --check
前端 Vitest / typecheck / build
```

业务闭环：

```text
邀请码 → 邮箱验证 → 点数 → 上传 → 创建生成任务 → Worker → Mock Provider
→ MinIO → 结算 → 结果展示 → 下载 → 分享 → 匿名分享页 → 反馈
```

异常矩阵：

```text
成功消费 / 明确失败释放 / UNKNOWN 保留预留 / 重复提交 / 重复回调
权限拒绝 / Worker 重启恢复 / 生命周期清理 / 分享与反馈幂等
```

## 硬性纪律

1. 浏览器 E2E **栈未启动时必须失败并给出原因**，不得静默跳过；
2. 必须区分「原生进程验证」与「Compose 容器验收」，不得把前者写成后者；
3. 只清理本轮自己创建、且带明确前缀的资源；既有 `.pytest-*`、`.review-*`
   与其他会话目录一律不碰；
4. 不得为通过测试删除或放宽测试；
5. 历史门禁数字不是本轮结果，最终以本轮命令与观察为准。

## 审核门禁

- 全部命令有本轮输出；
- 失败项、未完成项与风险如实登记（含环境限制）；
- 资源清理状态可核对；
- 完成后停线，等待总控统一审核。

## 不负责

- 不修改已冻结契约（`shared/api-contract/openapi.json`）与已应用迁移；
- 不改变 ENG-09 已验收的 API、数据库、状态机、点数、Provider、Storage 语义；
- 不启动 ENG-11 或任何后续卡片。

## 返修轮（2026-10-04）摘要

首轮 6 条未通过用例全部修好，复跑 **17 passed / 0 failed**。逐条根因、改动与证据见
交接文档 §13；三条要点：

1. 第 1–4 条不是 Worker/Beat 时序问题，而是**共享账号残留**：`funded` 夹具原本复用
   session 级账号，而 UNKNOWN 用例按产品语义**故意保留预留**，污染了后续所有
   `reserved_points == 0` 的绝对断言。修法是每用例独立账号，**断言未放宽**。
2. 第 5 条是用例**漏了上传**（默认入口是 `PHOTO_PROMPT`，没有 READY 资源时提交按钮禁用）；
   第 6 条是断言了**页面上不存在的文案**「邮箱验证」（真实文案是「确认你的邮箱」/
   「验证邮箱」/「邮箱已验证」，且页面需手动确认）。
3. 「同一用户 + 同一幂等键」已做 API 级复现：两次响应同一任务 ID、
   `generation_tasks` 1 行、`point_ledger` 只有一条 `RESERVE`、`reserved_points` 只占 40。
   **幂等语义正确，不是产品缺陷**，因此未改 `backend/app/**`。

## 定向返修轮（2026-10-04，总控扩展授权）摘要

授权只覆盖两个**已证实的一致性缺陷**，两者都已修复；详细证据见交接文档 §14。

1. **会话事实一致性**：`GET /auth/session` 的 `email_verified` 改为以
   `user_emails` 的**当前事实**为准（原来回放会话创建时的 Redis 快照）。
   **没有重签 Cookie、没有延长 TTL、没有复活已撤销会话、没有改 Cookie 属性**，
   也**没有**凭验证令牌为其他用户开锁（`/auth/email/verify` 一行未动）。
   → 「先兑换、再点验证链接」的用户现在**同一个会话**就解锁。
2. **型号定价规则一致性**：`find_active` 改为**精确型号优先、`NULL` 兜底**，
   `create_generation` 补传 `model_name`；创建任务、定价初始化、`/health/ready`
   三处从此同一套取值规则。**未引入任何默认价格**，缺配置仍是
   `503 NOT_READY / cost_config_missing`。

E2E 侧同步纠正：`redeem_and_verify` **删掉第二枚邀请码**，主闭环改走
「一枚邀请码 → 邮箱验证 → 同一会话解锁」；诊断用例
`test_browser_email_verification_link_and_the_ui_lockout` 改写为正确行为回归
`test_browser_email_verification_unlocks_the_same_session`（解锁 → 上传 → 生成 → 扣点）。

## Compose 部署形态补验（2026-10-04，Docker 恢复可用）摘要

隔离参数：项目 `ai-avatar-eng10` / 网络 `ai-avatar-eng10-private` /
卷 `ai-avatar-eng10-{postgres,redis,minio}` / 镜像 `*:v1-eng10` /
宿主端口 `15432·16379·19000·19001·18000·18001·13000`（起栈前五项过滤均为空、端口无监听）。
`-p` **不足以**隔离——compose.yml 的网络与三个卷是显式 `name:` 的，必须用环境文件同时覆盖。

分阶段启动（避免「先等 backend healthy、再初始化数据库」的死锁）：
基础设施 → 桶/迁移/定价 → backend·worker·beat → readiness → frontend。

**通过**：镜像构建、空库迁移到 `0021 (head)` 且幂等复跑无新增、显式定价 2 条且复跑 0 条、
七服务健康（backend healthy 即 `/api/v1/health/ready` 200）、Next 同源代理
（`127.0.0.1:13000/api/v1/health/ready` → 200）、真实 Beat 每 15 秒投递 + Worker 消费、
真实生成闭环（TEXT_PROMPT 202 → SUCCEEDED/CONSUMED、点数 100→70、
`point_ledger` RESERVE 30 → CONSUME 30、结果对象写入 MinIO）。

**未通过（环境阻断）**：宿主侧预签名 URL。宿主把 `host.docker.internal` 解析为
`10.193.104.171`，而该地址被沙箱网络拦截层接管（裸 TCP 可连但无响应；
对照实验里只绑 `127.0.0.1` 的 15432 同样「可连」），因此浏览器的预签名 PUT 到不了 MinIO，
上传不进入 READY。**未绕过安全策略、未改 hosts、未用替代基础设施。**
获准环境里只需跑 §14.7.5 第 12 步的 `pytest tests/e2e -q` 即可补上这一段。

**镜像版本**：`ai-avatar-backend:v1-eng10 sha256:ccb4c0ea…`（python:3.13-slim）、
`ai-avatar-frontend:v1-eng10 sha256:663c4d80…`（node:22-alpine）、`postgres:16-alpine`、
`redis:7-alpine`、`minio/minio:latest` 实际解析为 `RELEASE.2025-09-07T16-13-09Z`
（digest `sha256:14cea493…`；`latest` 会漂移，未固定版本属部署决策）。

**清理边界**：`ai-avatar-eng10-*` 容器/网络/三个卷与两个 `v1-eng10` 镜像全部删除、
`.env.eng10` 已删；`ai-avatar-v1-*`（默认本地环境，含用户数据）与其它项目 21 个运行中容器
**未触碰**；七个端口回到无监听。

## Compose 浏览器闭环补验（2026-10-04 续，见交接文档 §16）

**存储端点根因（非推测）**：宿主 hosts 里 `host.docker.internal -> 10.193.104.171`，
而该地址**不属于本机任何接口**（本机是 `172.17.80.1 / 172.28.213.171 / 198.18.0.1`）
——陈旧条目，宿主连它 TCP 可连但无响应。逐地址双向实测后取
**`172.28.213.171`**（容器与宿主都 200），并让 MinIO 只绑这一个接口。
**未改 hosts、未绕过安全策略、未改写签名 URL。**

**真实预签名 PUT/GET（宿主侧）**：PUT 200 → complete 200（READY）→ download-url 200 →
GET 200（10870 字节，**sha256 与原文件一致**）。

**双执行模式**：`ENG10_STACK_MODE=native|compose` 显式声明。
`native` 用 `scripts/eng10_ui_stack.py`；`compose` 用新增的
`scripts/eng10_compose_stack.py`（只操作 `ENG10_COMPOSE_PROJECT`，拒绝 `ai-avatar-v1`，
停机核对容器状态、启动等 `ready.`）。**两种模式互不调用对方的脚本**；
`finally` 无条件恢复 Worker，恢复失败可见但不吞掉原始失败。

**栈身份核对**：新增 `stack_identity` 夹具 —— 两个入口 readiness 均 200；
在断言用的库里种的邀请码**经 Next 同源代理**兑换必须 201（证明浏览器入口同一库）；
经测试入口兑换并发邮件后，`ENG10_MAIL_DIR` 必须出现新邮件（证明测试入口就是邮件替身）。
E2E 覆盖文件把 Next 代理指向 `backend-e2e`，因此**浏览器与宿主走同一入口、同一库/Redis/MinIO**。

**结果**：Compose 栈 `17 passed / 0 failed`（286.24s）；原生栈 `17 passed / 0 failed`（247.59s）。
唯一为适配 Compose 调整的断言是「下载链接指向存储端点」——从硬编码改为读
`ENG10_STORAGE_ENDPOINT`，**断言未放宽**。

## LEAD-06 两项测试缺口返修（2026-10-04，见交接文档 §17）

1. **丢失投递恢复用例重写**。旧用例**没有停 Worker**，任务其实是被真实 Worker 正常跑完的；
   而恢复扫描按 `created_at` 判定陈旧（阈值 120s），刚创建的任务不在窗口内 ——
   旧用例的 `SUCCEEDED` 与恢复路径无关，属于「断言存在、覆盖不存在」。
   新用例：先停本栈 Worker → 清空队列（隔离队列，断言无 `generation.run` 残留）→
   创建任务（断言恰好一条投递）→ 移除该投递（断言归零）→
   断言任务仍 `QUEUED` 且 **`provider_attempts` 0 行**（**不删 attempt、不改状态**）→
   只推 `created_at` 进陈旧窗口 → 手动扫描（断言 `reenqueued >= 1` 且恰好补投一条）→
   恢复 Worker 后断言 `SUCCEEDED + CONSUMED`、**恰好一次 RESERVE/CONSUME、
   一行 attempt、该用户只有一个任务行**。
2. **Worker 控制加固**：停机及其核对纳入恢复保护（三种失败情形都恢复）；
   恢复失败可见但不吞原始异常；启动前核对容器归属；就绪判据改用
   `docker inspect .State.StartedAt` 这条**本次启动的时间边界**，不接受历史 `ready.`；
   `ps` / `logs` 非零退出明确失败。

两种形态复跑：Compose `17 passed / 0 failed`（183.94s）、原生 `17 passed / 0 failed`（152.83s）；
针对性用例单跑 `1 passed`（19.57s）。文档中 `.env.eng10` 的「已删除 / 保留」矛盾已修正
（当前实际状态 = **保留**）。

## `worker_stopped` 恢复调用异常处理（2026-10-04，见交接文档 §18）

`worker_stopped` 的 `finally` 原先直接调用恢复函数：恢复调用本身抛 `TimeoutExpired`/`OSError`
时会**把正在传播的原始失败顶掉**，且 `sys.exc_info()` 是现场读的。修法：

- 抽出 `_restore_worker()`：**绝不抛异常**，把「非零返回」与「调用异常」都转成可读失败说明；
- `finally` 第一件事 `pending = sys.exc_info()` 固定原始异常状态；
- 有原始异常 → 保留它为主异常并**打印**恢复失败；没有原始异常 → **抛 AssertionError**（不吞）；
- `stop` / `start` 只是注入点，成功路径逐字未改。

新增 `backend/tests/e2e_control/`（纯逻辑、**不起真实资源**，与 `tests/e2e/` 分开以免被
`autouse` 的栈夹具强制要求一整套栈）：**7 条故障注入用例全过（0.61s）**，
覆盖停止检查失败仍恢复 / 用例失败+恢复非零 / 用例失败+恢复抛 `TimeoutExpired`/`OSError` /
用例成功+恢复失败（非零与抛异常两种）/ 正常路径。

回归：非 DB 全量 **1502 passed**（+7）；E2E 收集仍 **17**；原生栈针对性实跑
**5 passed（52.32s）**证明成功路径未变。**未重跑**镜像构建与两种形态全量 E2E
（只改异常处理、成功路径不变）。
