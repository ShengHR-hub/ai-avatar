import { defineConfig } from "@hey-api/openapi-ts";

// 生成物只服务于本前端：输入是冻结的 `shared/api-contract/openapi.json`，
// 输出按 operationId 命名，禁止再手写后端 DTO 或接口路径。
export default defineConfig({
  input: "../shared/api-contract/openapi.json",
  output: {
    path: "generated",
    clean: true,
  },
  plugins: [
    "@hey-api/typescript",
    "@hey-api/client-fetch",
    {
      name: "@hey-api/sdk",
      operations: { strategy: "flat" },
    },
  ],
});
