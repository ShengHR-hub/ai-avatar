import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    // 与 compose 注入的形状一致：含 `/api/v1` 后缀，用来验证剥离逻辑
    env: { NEXT_PUBLIC_API_BASE_URL: "http://backend.invalid/api/v1" },
  },
});
