import { afterEach, describe, expect, it, vi } from "vitest";

import {
  OPEN_MODES,
  OUTPUT_COUNT,
  apiBase,
  apiBaseOrNull,
  requestBase,
  resolveOrigin,
} from "@/services/config";

/**
 * `NEXT_PUBLIC_API_BASE_URL` 的两种合法形状：
 *
 * - **相对同源**（部署默认）：`/api/v1` → base 为 `""`，请求走当前源 + Next 同源代理；
 * - **绝对 origin**（前后端分域）：`https://api.example.com/api/v1` → base 为 origin。
 *
 * 两者都必须容忍尾部的 `/api/v1`（compose 模板的历史形状），否则会拼出
 * `/api/v1/api/v1/...`。测试环境默认值仍由 `vitest.config.ts` 注入为绝对形状。
 */

function withBase(value: string | undefined): void {
  if (value === undefined) vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "");
  else vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", value);
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("API base 解析", () => {
  it("相对同源配置：base 为空串，请求落在当前页面的源上", () => {
    withBase("/api/v1");
    expect(apiBase()).toBe("");
    // 代理生效的关键形状：不能解析成某个绝对 origin，否则会绕开同源代理变成跨源
    expect(requestBase()).toBe(new URL(document.baseURI).origin);
  });

  it("相对配置也容忍尾部斜杠与重复前缀", () => {
    withBase("/api/v1/");
    expect(apiBase()).toBe("");
    withBase("/");
    expect(apiBase()).toBe("");
  });

  it("剥掉契约自带前缀，避免生成路径的 /api/v1 被拼两次", () => {
    withBase("http://localhost:8000/api/v1");
    expect(apiBase()).toBe("http://localhost:8000");
    withBase("http://localhost:8000/api/v1/");
    expect(apiBase()).toBe("http://localhost:8000");
  });

  it("绝对配置没有后缀时按 origin 使用", () => {
    withBase("https://api.example.com");
    expect(apiBase()).toBe("https://api.example.com");
  });

  it("拒绝非 http(s) 的绝对形状，而不是静默回退", () => {
    withBase("ftp://host/api/v1");
    expect(() => apiBase()).toThrow(/origin/);
    withBase("not-a-url");
    expect(() => apiBase()).toThrow();
  });

  it("缺少配置时按「未配置」返回 null，由调用点决定是否报错", () => {
    withBase(undefined);
    expect(apiBaseOrNull()).toBeNull();
    expect(requestBase()).toBeNull();
    expect(() => apiBase()).toThrow(/NEXT_PUBLIC_API_BASE_URL/);
  });

  it("测试环境注入的是绝对形状，且已剥离后缀", () => {
    // vitest.config.ts 的 env 与 compose 的形状一致（含 /api/v1）
    expect(apiBase()).toBe("http://backend.invalid");
    expect(requestBase()).toBe("http://backend.invalid");
  });

  it("resolveOrigin 仍然只接受绝对 origin（保留给显式分域调用点）", () => {
    expect(resolveOrigin("http://localhost:8000/api/v1")).toBe("http://localhost:8000");
    expect(resolveOrigin("https://api.example.com")).toBe("https://api.example.com");
    expect(() => resolveOrigin("")).toThrow(/NEXT_PUBLIC_API_BASE_URL/);
  });
});

describe("契约常量", () => {
  it("每次固定两张，界面不提供数量选择", () => {
    expect(OUTPUT_COUNT).toBe(2);
  });

  it("只开放两种模式：参考图入口不出现", () => {
    expect(OPEN_MODES.map((mode) => mode.value)).toEqual(["PHOTO_PROMPT", "TEXT_PROMPT"]);
  });
});
