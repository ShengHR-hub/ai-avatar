import { existsSync, readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * 本轮的三个已证实缺陷，各留一道静态门禁，避免改回去：
 *
 * 1. `backdrop-filter` 被构建时的 CSS 前缀处理去重成 `-webkit-backdrop-filter`，
 *    而 Chrome/Edge/Firefox 都不支持该前缀 —— 玻璃会整体失效。原因是源码里同时写了
 *    标准属性与 `-webkit-` 兄弟声明，压缩器去重后只留了前缀形式。所以：模糊值经 CSS
 *    变量下发，且源码不得出现 `-webkit-` 兄弟声明（构建会自己补前缀）。
 * 2. HTML 的 width/height 属性会以 presentational hint 赢过 img 上的 aspect-ratio，
 *    把 238×1200 那种细长条撑出来。比例必须落在容器上。
 * 3. 字号不得随屏宽缩放。
 */
const raw = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");
/** 注释里会提到这些属性名，门禁必须只看声明本身。 */
const css = raw.replace(/\/\*[\s\S]*?\*\//g, "");

describe("样式门禁", () => {
  it("backdrop-filter 经变量下发，源码不写 -webkit- 兄弟声明", () => {
    expect(css).not.toContain("-webkit-backdrop-filter");
    expect(css).not.toContain("-webkit-mask-image");
    const values = [...css.matchAll(/(?<!-webkit-)backdrop-filter:\s*([^;}]*)/g)].map((m) => m[1].trim());
    expect(values.length).toBeGreaterThanOrEqual(4);
    for (const value of values) {
      expect(value.startsWith("var(--blur-")).toBe(true);
    }
  });

  it("构建产物里必须有无前缀的 backdrop-filter（存在构建时检查）", () => {
    const dir = resolve(process.cwd(), ".next/static");
    if (!existsSync(dir)) return;
    const queue = [dir];
    const cssFiles: string[] = [];
    while (queue.length > 0) {
      const current = queue.pop()!;
      for (const entry of readdirSync(current, { withFileTypes: true })) {
        const full = resolve(current, entry.name);
        if (entry.isDirectory()) queue.push(full);
        else if (entry.name.endsWith(".css")) cssFiles.push(full);
      }
    }
    if (cssFiles.length === 0) return;
    const built = cssFiles.map((file) => readFileSync(file, "utf8")).join("\n");
    const unprefixed = (built.match(/(?<!-webkit-)backdrop-filter\s*:/g) ?? []).length;
    expect(unprefixed).toBeGreaterThanOrEqual(4);
  });

  it("比例落在容器上：img 自身不声明 aspect-ratio", () => {
    const imgRules = [...css.matchAll(/([^{}]*\bimg\b[^{}]*)\{([^}]*)\}/g)];
    expect(imgRules.length).toBeGreaterThan(0);
    for (const [, selector, body] of imgRules) {
      expect(selector.trim().length).toBeGreaterThan(0);
      expect(body).not.toContain("aspect-ratio");
    }
    expect(css).toMatch(/\.media\s*>\s*img\s*\{[^}]*height:\s*100%/);
    expect(css).toMatch(/\.media--1x1\s*\{[^}]*aspect-ratio:\s*1\s*\/\s*1/);
  });

  it("字号不随屏宽缩放", () => {
    expect(css).not.toMatch(/font-size:\s*clamp\(/);
    const fontSizeWithVw = css.match(/font-size:\s*[^;]*vw/g) ?? [];
    expect(fontSizeWithVw).toHaveLength(0);
  });

  it("重复卡片圆角不超过 8px", () => {
    expect(css).toMatch(/--r-card:\s*8px/);
  });
});
