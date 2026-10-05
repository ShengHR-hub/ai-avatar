# LEAD-04：点数账务与生成任务状态机

> 状态：待技术负责人审核
> 适用版本：V1.0
> 本卡只冻结契约和工程说明，不实现 Points Service，不修改点数价格，不提交 Git。

## 1. 冻结结论

点数账务事实只来自追加式 `point_ledger`。任何路由、Celery Task、Provider 或前端都不得直接修改钱包余额；所有余额变化必须由 Points Service 在同一数据库事务中写入流水并更新钱包快照。

固定账务流程：

```text
预留：available -= cost，reserved += cost，写 RESERVE
成功：reserved -= cost，写 CONSUME，lifetime_consumed += cost
明确失败：reserved -= cost，available += cost，写 RELEASE
UNKNOWN / NEEDS_REVIEW：保持 RESERVED，不自动消费，不自动释放
```

每个 `generation_id` 至多一条 `RESERVE`、至多一条最终 `CONSUME` 或 `RELEASE`。`CONSUME` 与 `RELEASE` 互斥。所有重复请求、Worker 重投、Provider 回调和人工复核都必须幂等。用户重试关系在数据库内部字段为 `parent_task_id`，API 对外 DTO 字段为 `parent_generation_id`；两者是同一父任务关系，ENG-06 必须在 DTO/Repository 边界完成映射。

任务状态与 `billing_status` 完全分离：任务可以是 `NEEDS_REVIEW`，同时计费仍为 `RESERVED`；人工结算只改变计费状态，不补造任务状态转换。

## 2. 任务状态和合法转换

V1.0 只允许以下六个任务状态，不增加 `CANCELLED`：

```text
CREATED → QUEUED → PROCESSING → SUCCEEDED
                               ├→ FAILED
                               └→ NEEDS_REVIEW
```

| 当前状态 | 允许的下一状态 | 触发条件 | 账务要求 |
|---|---|---|---|
| `CREATED` | `QUEUED` | 任务、预留流水和钱包事务提交成功并完成入队准备 | `RESERVED` |
| `QUEUED` | `PROCESSING` | Worker 成功领取任务并创建 Provider attempt | `RESERVED` |
| `PROCESSING` | `SUCCEEDED` | Provider 成功且所有结果已持久化、数量和资源校验通过 | 原子 `CONSUME` |
| `PROCESSING` | `FAILED` | Provider 明确拒绝/失败、未提交且可证明未产生费用、或取消已明确生效 | 原子 `RELEASE` |
| `PROCESSING` | `NEEDS_REVIEW` | UNKNOWN、无法确认扣费/结果，或 Provider 成功但结果缺失/无法校验 | 保持 `RESERVED` |
| 终态 | 无 | 终态不可逆；重复事件只做幂等重放 | 不再改变账务 |

`CREATED`、`QUEUED` 和 `PROCESSING` 不得被直接删除或跳过状态。数据库更新必须通过 Generation Service 的状态转换函数完成；非法转换返回稳定错误并不写账。

## 3. `billing_status` 规则

`billing_status` 只允许 `RESERVED`、`CONSUMED`、`RELEASED`：

| 任务状态 | 允许的 `billing_status` |
|---|---|
| `CREATED`、`QUEUED`、`PROCESSING` | `RESERVED` |
| `SUCCEEDED` | `CONSUMED` |
| `FAILED` | `RELEASED` |
| `NEEDS_REVIEW` | `RESERVED`，直到人工决定 |
| `NEEDS_REVIEW`（人工已结算） | `CONSUMED` 或 `RELEASED` |

禁止出现 `SUCCEEDED + RESERVED`、`FAILED + RESERVED`，以及未完成人工决定的 `NEEDS_REVIEW + CONSUMED/RELEASED`。人工决定完成后允许 `NEEDS_REVIEW + CONSUMED` 或 `NEEDS_REVIEW + RELEASED`，任务状态仍保持 `NEEDS_REVIEW`。历史异常由人工对账处理，不允许通过覆盖流水修复。

## 4. 状态、错误和取消处理

| 事件 | 归类 | 处理 |
|---|---|---|
| 输入校验、权限、Provider 明确拒绝 | 明确失败 | `FAILED` + `RELEASE` |
| 请求未提交，网络错误且 Provider 明确返回未接受 | 明确失败 | `FAILED` + `RELEASE` |
| Provider 已接受前，且适配器可靠证明未接受且未产生外部执行的 `TIMEOUT` | 可安全重试 | 保留 `RESERVED`；每次重试保留独立 attempt 记录 |
| Provider `TIMEOUT`（已接受、接受状态不明、连接中断、响应不完整） | UNKNOWN | `NEEDS_REVIEW`，保留 `RESERVED`，不得自动重试 |
| Provider 返回成功，但结果为空、数量不足、对象校验失败 | 结果缺失 | `NEEDS_REVIEW`，保留 `RESERVED` |
| `CREATED`/`QUEUED` 阶段取消，Provider 尚未开始 | 明确失败 | 任务转 `FAILED`，`failure_code=CANCELLED_BY_USER`，`RELEASE` |
| `PROCESSING` 阶段取消且 Provider 明确确认取消、未收费 | 明确失败 | 转 `FAILED` 并 `RELEASE` |
| `PROCESSING` 阶段取消但无确认，或取消请求超时 | UNKNOWN | 转 `NEEDS_REVIEW`，不得自动退款 |

V1.0 不提供用户公开取消 API，不新增公开取消路径。内部 Worker、Service 或运营工具如需发出取消命令，必须走内部 Service 边界；不产生 `CANCELLED` 状态。取消命令在任务已终态时返回幂等结果；对 `NEEDS_REVIEW` 不允许绕过人工复核取消或自动退款。

## 5. Points Service 接口契约

以下是 ENG-04 必须提供的领域接口；具体 ORM 和模块路径由工程实现决定。

```python
reserve_generation(
    *, user_id, generation_id, amount, idempotency_key,
    reason, metadata, cost_snapshot
) -> ReservationResult

consume_generation(
    *, generation_id, idempotency_key, actor, reason, metadata
) -> SettlementResult

release_generation(
    *, generation_id, idempotency_key, actor, reason, metadata
) -> SettlementResult

resolve_needs_review(
    *, generation_id, decision: Literal["CONSUME", "RELEASE"],
    idempotency_key, admin_id, note, evidence, metadata
) -> SettlementResult

adjust_wallet(
    *, user_id, signed_amount, idempotency_key, admin_id,
    reason, case_reference, metadata
) -> AdjustmentResult

get_wallet_snapshot(*, user_id) -> WalletSnapshot
```

契约要求：

1. `amount` 必须为正整数；`adjust_wallet.signed_amount` 可正可负，但调整后可用和预留均不得小于零。
2. `reserve_generation` 在钱包行上执行 `SELECT ... FOR UPDATE`，校验余额、插入 `RESERVE`、更新快照；余额不足返回 `INSUFFICIENT_POINTS`，不创建部分记录。
3. `consume_generation`/`release_generation` 必须锁定任务关联钱包和任务账务记录，校验仍为 `RESERVED` 后写唯一流水并更新快照。
4. 相同幂等键和相同参数返回第一次结果；相同键不同参数返回 `IDEMPOTENCY_CONFLICT`。
5. 已完成相同结算操作是幂等成功；对已 `CONSUMED` 的任务执行 `RELEASE`，或对已 `RELEASED` 的任务执行 `CONSUME`，返回 `IDEMPOTENCY_CONFLICT`，不写反向流水。（2026-09-27 修订：稳定错误码集合不含 `BILLING_ALREADY_FINALIZED`，本条按技术负责人裁决改用既有 `IDEMPOTENCY_CONFLICT`，不新增错误码。）
6. `resolve_needs_review` 仅接受 `NEEDS_REVIEW + RESERVED`，必须保留管理员、理由、证据引用和请求 ID；决策本身不改变任务状态。
7. `adjust_wallet` 只能由管理员使用，调用方必须提供 `adjustment_kind=POSITIVE_CREDIT` 或 `NEGATIVE_CONSUMPTION`、工单/理由、前后钱包快照和审计上下文；前者计入 `lifetime_credited`，后者计入 `lifetime_consumed`。它不能替代任务的 `CONSUME`/`RELEASE`，必须写 `ADJUSTMENT` 和独立审计日志；重复请求幂等，历史流水不可覆盖或删除。

## 6. 事务边界和并发策略

### 创建与预留

在一个 PostgreSQL 事务内完成：锁钱包 → 校验可用余额 → 创建任务 → 写 `RESERVE` → 更新钱包快照 → 提交。提交后再投递 Celery；投递失败由恢复扫描重新入队，不回滚已提交的账务。

### Worker 领取

短事务锁定任务行，要求 `QUEUED`，更新为 `PROCESSING` 并写入 `provider_attempts(STARTED)` 后提交。Provider 网络调用绝不放在数据库事务内。

### 成功、失败和复核

结果写入、任务终态更新、Points Service 结算和钱包快照必须在同一事务内完成。事务内按固定顺序锁定 `generation_tasks` 再锁 `point_wallets`，所有结算入口采用相同顺序，避免死锁。事务失败整体回滚，Worker 可安全重投。

钱包 `FOR UPDATE` 是 V1.0 的并发闸门；`version` 继续递增用于快照校验和监控，不以应用层乐观检查替代行锁。流水唯一约束和任务计费状态条件更新是第二道防线。

## 7. 幂等、重复请求和重试

- 创建任务：唯一键为 `(user_id, idempotency_key)`；相同请求返回原任务，参数不同返回冲突。
- 账务流水：每次操作携带作用域明确的幂等键；数据库对 `generation_id + entry_type` 建唯一约束（适用于 `RESERVE/CONSUME/RELEASE`）。
- Provider 回调：以 provider、外部 request ID、attempt 和事件 ID 去重；重复回调只重新读取任务结果。
- Worker 重投：已终态任务直接 no-op；`PROCESSING` 任务只允许拥有当前 attempt 的 Worker 继续处理，不能重复结算。
- 自动重试：仅允许 Provider 未明确接受前、且适配器可靠证明未接受和未产生外部执行的 `RATE_LIMIT`、`TEMPORARY` 或 `TIMEOUT`；每次尝试保留独立 `provider_attempts` 记录。已接受、接受状态不明、连接中断、响应不完整、结果缺失和 UNKNOWN 不得自动重试，并进入 `NEEDS_REVIEW + RESERVED`。
- 用户重试：只允许对 `FAILED` 任务发起，创建全新任务、全新幂等键和全新预留，设置数据库 `parent_task_id=原任务.id`，对外映射为 `parent_generation_id`；旧任务保持 `FAILED + RELEASED`，不复制旧流水。`NEEDS_REVIEW` 必须先人工结算，不能通过重试绕过保留点数。

## 8. 人工复核和审计

`NEEDS_REVIEW` 队列必须展示任务、Provider attempt、外部 request ID、结果检索证据和当前钱包/流水快照。管理员只能选择：

- `CONSUME`：有可验证的 Provider 成功/收费证据，且结果已补回或业务明确接受结果缺失；写 `CONSUME`。
- `RELEASE`：有可验证的 Provider 未执行/未收费/明确失败证据；写 `RELEASE`。

两种决定都必须记录 `admin_id`、`request_id`、决策时间、理由、证据引用、任务和钱包快照摘要，并追加 `audit_logs`。人工操作重复提交返回原决定，且**只为首个成功决定保留一条审计**：幂等回放不追加新审计记录。相反决定被拒绝，返回既有 `IDEMPOTENCY_CONFLICT`；由于 Points Service 与结算流水同事务，拒绝路径上的审计行会随事务回滚，因此**相反决定的拒绝审计留给 Admin API 的独立事务记录**，不由 Points Service 在同一事务内写入。管理员不能直接编辑余额、删除流水或覆盖任务状态。

## 9. 账务异常测试矩阵

| 场景 | 预期任务/计费 | 预期流水与余额 | 必测断言 |
|---|---|---|---|
| 正常预留 | `CREATED/QUEUED + RESERVED` | 一条 `RESERVE`，可用减、预留加 | 钱包非负且快照可重算 |
| 余额不足 | 无任务 | 无流水、余额不变 | 返回 `INSUFFICIENT_POINTS` |
| 相同创建请求重复 | 返回同一任务 | 仅一条 `RESERVE` | 响应完全一致 |
| 相同键不同请求体 | 拒绝 | 无新流水 | `IDEMPOTENCY_CONFLICT` |
| 两个并发预留 | 一个成功或按余额分别成功 | 每次成功恰一条 `RESERVE` | 无负余额、无超卖 |
| 成功回调重复 | `SUCCEEDED + CONSUMED` | 仅一条 `CONSUME` | 第二次 no-op |
| 明确失败重复 | `FAILED + RELEASED` | 仅一条 `RELEASE` | 第二次 no-op |
| CONSUME/RELEASE 竞态 | 一个胜出 | 仅一条最终流水 | 另一方 `IDEMPOTENCY_CONFLICT`（2026-09-27 修订，不新增错误码） |
| 安全 pre-accept TIMEOUT | 当前任务保持预留，新增 attempt | 无最终流水 | 仅有可靠未接受/未执行证据时有限重试 |
| Provider post-accept/UNKNOWN TIMEOUT | `NEEDS_REVIEW + RESERVED` | 无 `CONSUME/RELEASE` | 不自动退款、不自动重试 |
| Provider 成功但结果缺失 | `NEEDS_REVIEW + RESERVED` | 无最终流水 | 可人工检索证据 |
| 创建/排队取消 | `FAILED + RELEASED` | 一条 `RELEASE` | 不出现 `CANCELLED` |
| 处理中取消无确认 | `NEEDS_REVIEW + RESERVED` | 无最终流水 | 不自动退款 |
| Worker 重启/重复投递 | 原状态继续 | 无重复结算 | attempt 和状态可恢复 |
| FAILED 用户重试 | 新任务 `RESERVED`，父任务不变 | 新 `RESERVE`，旧任务仍 `RELEASED` | parent 链可追溯 |
| NEEDS_REVIEW 用户重试 | 拒绝 | 无新流水 | 不绕过人工结算 |
| 人工 CONSUME/RELEASE 重复 | 任务仍 `NEEDS_REVIEW`，计费终态 | 恰一条最终流水 | 管理员、理由、证据齐全；回放只返回原决定，**不追加新审计** |
| 人工相反决定 | 拒绝，返回 `IDEMPOTENCY_CONFLICT` | 余额和流水不变 | Points Service 不在同事务写拒绝审计；拒绝留痕由 Admin API 的独立事务记录（2026-09-27 修订） |
| ADJUSTMENT 正向补发 | 钱包快照更新 | 一条 `ADJUSTMENT` | `POSITIVE_CREDIT`，计入 `lifetime_credited`，管理员、工单和前后快照齐全 |
| ADJUSTMENT 负向消费 | 钱包快照更新 | 一条 `ADJUSTMENT` | `NEGATIVE_CONSUMPTION`，计入 `lifetime_consumed`，管理员、工单和前后快照齐全 |
| 事务中途失败 | 原子回滚 | 不产生半条流水 | 可安全重试 |

所有测试必须同时核对任务状态、`billing_status`、钱包快照、流水和审计日志；不能只断言 API 响应。

## 10. ENG-04 可直接执行的工程说明

1. 在 `backend/app/services/` 建立 Points Service，在 `backend/app/repositories/` 提供钱包/流水的锁定查询和追加写入；Repository 不提交事务。
2. 实现本卡第 5 节的六个方法，所有入口使用统一事务上下文、固定锁顺序和稳定错误码。
3. 为 `point_ledger` 增加适用的唯一约束/索引和非负余额约束；通过 Alembic 新增迁移，不修改已应用迁移。
4. 为预留、消费、释放、复核、调整和并发竞态编写 Service 单元测试与 PostgreSQL 集成测试，覆盖第 9 节矩阵。
5. 对外只暴露由 Generation/Admin Service 调用的领域接口；路由、Worker、Provider 不得写钱包或流水。
6. 完成后运行后端测试、类型检查和迁移前向验证；任何账务测试失败立即停止并回报，不进入 ENG-06。

## 11. ENG-06 可直接执行的工程说明

1. 在 `backend/app/services/` 实现 Generation Service 的状态转换和任务编排，在 `backend/app/tasks/` 实现 Celery 入口；Task 只解析参数、建立上下文、调用 Service 和分类重试。
2. 创建任务必须先调用 Points Service 预留，再提交任务和入队；Worker 领取时只做 `QUEUED → PROCESSING`，不直接碰账务表。
3. Provider 调用前后写 `provider_attempts`；成功结果持久化与 `CONSUME` 同事务，明确失败与 `RELEASE` 同事务，UNKNOWN/结果缺失只转 `NEEDS_REVIEW`。
4. 实现创建、查询、取消、重试、重复回调、Worker 重投和恢复扫描；取消按第 4 节归类，不引入 `CANCELLED`。
5. 自动重试仅覆盖“未提交且可证明安全”的错误；用户重试只接受 `FAILED` 并创建新任务及 `parent_task_id`。
6. 使用 MockProvider 覆盖成功、明确失败、未提交失败、TIMEOUT、UNKNOWN、成功结果缺失、取消已确认和取消未确认；每个场景验证任务状态、计费状态、流水、钱包和审计。
7. ENG-06 依赖 ENG-04 通过；若发现状态机、计费状态或 Provider 错误分类与本卡不一致，停止实现并提交技术负责人审核。

## 12. 尚未确定的问题

1. `docs/architecture/04-ai-provider.md` 已提供内部 Provider 契约；具体供应商的取消确认和外部计费证据字段仍需在 LEAD-05/ENG-07 按真实能力验证，但不得改变本卡“UNKNOWN 保留 RESERVED”、仅安全 pre-accept TIMEOUT 可重试的原则。
2. 结果缺失时，人工 `CONSUME` 是否要求结果必须最终补回，还是允许仅凭 Provider 收费证据结算，由产品/财务负责人确认；两种选择都不改变自动处理规则。
3. 任务入队失败的恢复扫描周期、最大告警延迟和 Celery 投递确认参数尚未冻结；不影响已提交预留的账务正确性。
4. 管理员证据附件的存储位置、保留期限和脱敏字段尚未冻结；审计字段、ADJUSTMENT 类型和不可覆盖要求已冻结。

## 13. 审核门禁与停止点

技术负责人必须能从钱包快照、点数流水、任务计费状态、Provider attempt 和审计日志还原每一次点数变化，才可授权 `ENG-04`。`ENG-04` 通过后才能开始 `ENG-06`；本卡完成后停止，不执行后续工程任务，等待审核。
