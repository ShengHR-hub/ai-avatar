# backend/app/services/

业务规则与事务编排层。

职责：业务规则、权限和状态检查、事务边界、点数预留/消费/释放、Repository 与 Provider/Storage 的编排。

约束：

- 所有点数结算必须由专门的 Points Service 完成，其他模块不得直接修改 `point_wallets`；
- 业务层禁止直接调用具体 AI 供应商 SDK；
- 业务层禁止直接依赖 MinIO、COS、OSS 或 S3 SDK；
- 事务由 Service 控制，Repository 不自行 commit。

ENG-00 不在此目录创建任何 Service。实现属于 `ENG-03` 至 `ENG-09`。
