# shared/api-contract/

存放由 FastAPI 导出的 `openapi.json`。当前 `openapi.json` 是 LEAD-03 冻结基线；ENG-02 必须从 FastAPI/Pydantic 重新导出并通过规范化 JSON diff 校验。

- FastAPI Pydantic Schema 是服务端契约源；
- 前端从 OpenAPI 生成 TypeScript 类型与客户端，不手写重复的后端响应类型；
- 该文件不是业务实现；契约变更必须先更新 `docs/architecture/03-api-contract.md` 并经技术负责人审核，再由 ENG-02 更新生成产物。

ENG-00 不生成 `openapi.json`。导出流程与前端类型生成属于 `ENG-02` 及其后续任务卡。
