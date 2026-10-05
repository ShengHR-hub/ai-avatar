import { describe, expect, it } from "vitest";

import { clipPrompt, PUBLIC_PROMPT_MAX, scanPublicPrompt } from "@/services/promptDisclosure";

describe("公开版提示词检查", () => {
  it("普通的描述性文字可以展示", () => {
    const result = scanPublicPrompt("把这张照片转成 3D 黏土风格头像，纯净浅灰背景，正方形构图");
    expect(result.ok).toBe(true);
    expect(result.reasons).toEqual([]);
  });

  it("命中私人信息或内部参数时判为不可展示，且只回类别不回原文", () => {
    const cases: [string, string][] = [
      ["联系方式 me@example.com", "邮箱地址"],
      ["加我 13800001111", "手机号"],
      ["参考 https://example.com/a.png", "网址"],
      ["读取 D:\\photos\\me.jpg 这张", "本地路径或文件名"],
      ["style_id=clay 加一点质感", "内部参数"],
      ["批次 550e8400-e29b-41d4-a716-446655440000", "疑似编号或密钥"],
    ];
    for (const [text, label] of cases) {
      const result = scanPublicPrompt(text);
      expect(result.ok, text).toBe(false);
      expect(result.reasons).toContain(label);
      // 只给类别，不把命中的内容回显出去
      expect(result.reasons.join("")).not.toContain("@");
    }
  });

  it("空内容不算可展示", () => {
    expect(scanPublicPrompt("   ").ok).toBe(false);
  });

  it("过长的描述会被截断并标记", () => {
    const long = "描".repeat(PUBLIC_PROMPT_MAX + 40);
    const clipped = clipPrompt(long);
    expect(clipped.clipped).toBe(true);
    expect(clipped.text.length).toBe(PUBLIC_PROMPT_MAX + 1);
    expect(clipPrompt("短句")).toEqual({ text: "短句", clipped: false });
  });
});
