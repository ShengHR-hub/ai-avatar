import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  // Vitest 默认不暴露全局 afterEach，RTL 的自动清理不会生效；
  // 不显式清理时上一个用例的 DOM 会留在 body 里，让 getBy* 命中多个元素。
  cleanup();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});
