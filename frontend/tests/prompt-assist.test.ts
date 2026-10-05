import { describe, expect, it } from "vitest";

import { IDEA_DIMENSIONS, USE_PRESETS } from "@/services/catalog";
import { OPEN_MODES } from "@/services/config";
import {
  ASSIST_TAIL,
  applyPreset,
  assistPreview,
  composePrompt,
  selectedFragments,
  selectionCount,
} from "@/services/promptAssist";

const OPEN = OPEN_MODES.map((mode) => mode.value);

describe("灵感标签：拼句规则", () => {
  it("纯文字创作由「主题」决定开头，其余维度按顺序拼在后面", () => {
    expect(composePrompt("TEXT_PROMPT", "", { subject: "pet", medium: "crayon", mood: "funny" })).toBe(
      `生成宠物头像，蜡笔涂鸦，搞怪夸张，${ASSIST_TAIL}`,
    );
  });

  it("没选主题时开头是通用的一句，不会拼出病句", () => {
    expect(composePrompt("TEXT_PROMPT", "", { medium: "crayon" })).toBe(
      `生成一张头像，蜡笔涂鸦，${ASSIST_TAIL}`,
    );
  });

  it("改造图片由「表现」决定开头，且不提供主题维度", () => {
    expect(composePrompt("PHOTO_PROMPT", "", { medium: "crayon", mood: "funny" })).toBe(
      `把这张图片改成蜡笔涂鸦头像，搞怪夸张，${ASSIST_TAIL}`,
    );
    expect(IDEA_DIMENSIONS.PHOTO_PROMPT.map((d) => d.id)).not.toContain("subject");
  });

  it("用户已经有内容时只追加，绝不删掉原有文字", () => {
    const base = "一只橘猫趴在窗台上";
    const preview = assistPreview("TEXT_PROMPT", base, { medium: "hand", mood: "healing" });
    expect(preview.action).toBe("append");
    expect(preview.text).toBe(`${base}，手绘，温暖治愈，${ASSIST_TAIL}`);
    expect(preview.added).toEqual(["手绘", "温暖治愈"]);
  });

  it("描述里已经有的词不重复追加", () => {
    const base = `生成宠物头像，手绘，${ASSIST_TAIL}`;
    const preview = assistPreview("TEXT_PROMPT", base, { medium: "hand" });
    expect(preview.added).toEqual([]);
    expect(preview.text).toBe(base);
  });

  it("同一维换一个标签是替换，不是叠加", () => {
    const first = composePrompt("TEXT_PROMPT", "", { mood: "healing" });
    const second = composePrompt("TEXT_PROMPT", "", { mood: "funny" });
    expect(first).toContain("温暖治愈");
    expect(second).toContain("搞怪夸张");
    expect(second).not.toContain("温暖治愈");
  });

  it("用途预设会带上附加句（例如两张风格统一）", () => {
    const couple = USE_PRESETS.find((preset) => preset.id === "couple");
    expect(couple).toBeTruthy();
    const { selection, extra } = applyPreset(couple!);
    const text = composePrompt("TEXT_PROMPT", "", selection, extra);
    expect(text).toContain("生成双人头像");
    expect(text).toContain("两张风格统一");
    // 附加句只出现一次，结尾仍然收在构图那一段
    expect(text.endsWith(ASSIST_TAIL)).toBe(true);
  });

  it("没有选择时原样返回用户内容，不会凭空生成", () => {
    expect(composePrompt("TEXT_PROMPT", "", {})).toBe("");
    expect(composePrompt("PHOTO_PROMPT", "自己写的一句", {})).toBe("自己写的一句");
  });

  it("计数与片段顺序按维度固定", () => {
    expect(selectionCount({ mood: "healing", medium: "hand" })).toBe(2);
    expect(selectedFragments("TEXT_PROMPT", { mood: "healing", medium: "hand" })).toEqual([
      "手绘",
      "温暖治愈",
    ]);
  });

  it("只产出描述文本：结果里不出现任何请求字段名", () => {
    for (const mode of OPEN) {
      const selection = Object.fromEntries(
        IDEA_DIMENSIONS[mode].map((dimension) => [dimension.id, dimension.options[0].id]),
      );
      const text = composePrompt(mode, "", selection);
      expect(text).not.toMatch(/style_id|prompt_template_id|output_count|mode=|asset/);
    }
  });
});
