import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  HOME_DIRECTIONS,
  HOME_IDEAS_MORE,
  IDEA_DIMENSIONS,
  IDEA_NOTE,
  POINT_DISCLOSURE,
  STEPS,
  USE_PRESETS,
  WORKS,
  WORK_MATERIAL_SPEC,
  WORKS_PENDING_NOTE,
  studioLink,
} from "@/services/catalog";
import { scanPublicPrompt } from "@/services/promptDisclosure";
import { OPEN_MODES } from "@/services/config";

const OPEN = OPEN_MODES.map((mode) => mode.value);

function inside(relative: string): boolean {
  return existsSync(resolve(process.cwd(), "public", relative.replace(/^\//, "").split(" ")[0]));
}

/** 每个维度下的全部选项 id，用来验证首页方向引用的词真的存在。 */
const TEXT_OPTIONS = new Set(
  IDEA_DIMENSIONS.TEXT_PROMPT.flatMap((dimension) =>
    dimension.options.map((option) => `${dimension.id}:${option.id}`),
  ),
);

describe("首页目录", () => {
  it("灵感分类只落在已开放的模式上，改造图片不提供「主题」", () => {
    for (const mode of OPEN) {
      expect(IDEA_DIMENSIONS[mode].length).toBeGreaterThan(0);
    }
    expect(Object.keys(IDEA_DIMENSIONS)).toEqual([...OPEN]);
    expect(OPEN).not.toContain("REFERENCE_IMAGE");

    // 改造图片时主体来自上传的图，提供「主题」会与"改造"自相矛盾
    const photoIds = IDEA_DIMENSIONS.PHOTO_PROMPT.map((dimension) => dimension.id);
    expect(photoIds).not.toContain("subject");
    expect(IDEA_DIMENSIONS.TEXT_PROMPT.map((dimension) => dimension.id)).toContain("subject");
  });

  it("每个标签都产出可读片段，维度内不重复 id 或标签", () => {
    for (const mode of OPEN) {
      for (const dimension of IDEA_DIMENSIONS[mode]) {
        expect(dimension.hint.trim().length).toBeGreaterThan(2);
        expect(dimension.options.length).toBeGreaterThanOrEqual(3);
        const ids = dimension.options.map((option) => option.id);
        const labels = dimension.options.map((option) => option.label);
        expect(new Set(ids).size).toBe(ids.length);
        expect(new Set(labels).size).toBe(labels.length);
        for (const option of dimension.options) {
          expect(option.fragment.trim().length).toBeGreaterThan(1);
          expect(option.fragment.length).toBeLessThanOrEqual(12);
        }
      }
    }
  });

  it("标签里不出现受保护角色/IP 名称，也不自称热门", () => {
    const banned = ["海绵", "派大星", "皮卡丘", "迪士尼", "热门", "热榜", "实时"];
    const text = [
      ...OPEN.flatMap((mode) => IDEA_DIMENSIONS[mode].flatMap((d) => d.options.map((o) => o.label))),
      ...USE_PRESETS.map((preset) => preset.label),
      ...HOME_DIRECTIONS.map((direction) => `${direction.label}${direction.hint}`),
      IDEA_NOTE,
    ].join("｜");
    for (const word of banned) expect(text).not.toContain(word);
    expect(IDEA_NOTE).toContain("灵感分类");
  });

  it("用途预设只引用真实存在的维度选项，且能一键套用", () => {
    expect(USE_PRESETS.length).toBeGreaterThanOrEqual(3);
    for (const preset of USE_PRESETS) {
      expect(preset.desc.trim().length).toBeGreaterThan(4);
      const keys = Object.keys(preset.selection);
      expect(keys.length).toBeGreaterThan(0);
      for (const [dimensionId, optionId] of Object.entries(preset.selection)) {
        expect(TEXT_OPTIONS.has(`${dimensionId}:${optionId}`)).toBe(true);
      }
    }
  });
});

describe("首页灵感方向（只摆有代表性的几条）", () => {
  it("方向数量少于一整面标签墙，并且每条都指向真实存在的标签", () => {
    expect(HOME_DIRECTIONS.length).toBeGreaterThanOrEqual(3);
    expect(HOME_DIRECTIONS.length).toBeLessThanOrEqual(8);

    const totalOptions = IDEA_DIMENSIONS.TEXT_PROMPT.reduce(
      (sum, dimension) => sum + dimension.options.length,
      0,
    );
    // 首页是"挑方向"，不是把词表摊完：方向条数必须远少于标签总数
    expect(HOME_DIRECTIONS.length).toBeLessThan(totalOptions);

    const ids = HOME_DIRECTIONS.map((direction) => direction.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const direction of HOME_DIRECTIONS) {
      expect(direction.label.trim().length).toBeGreaterThan(1);
      expect(direction.hint.trim().length).toBeGreaterThan(4);
      const keys = Object.keys(direction.selection);
      expect(keys.length).toBeGreaterThan(0);
      for (const [dimensionId, optionId] of Object.entries(direction.selection)) {
        expect(TEXT_OPTIONS.has(`${dimensionId}:${optionId}`)).toBe(true);
      }
    }
  });

  it("明确把完整标签组合留在创作台，不让人以为首页就是全部", () => {
    expect(HOME_IDEAS_MORE).toContain("创作台");
    expect(HOME_IDEAS_MORE).toContain("标签");
  });

  it("深链接带着描述与入口走，mode 与 way 都写在 query 上", () => {
    expect(studioLink("TEXT_PROMPT", "一只橘猫")).toBe(
      "/studio?mode=TEXT_PROMPT&prompt=%E4%B8%80%E5%8F%AA%E6%A9%98%E7%8C%AB",
    );
    const inspire = studioLink("TEXT_PROMPT", "", "inspire");
    expect(inspire).toContain("way=inspire");
    expect(inspire).toContain("mode=TEXT_PROMPT");
    // 参考图找灵感最终仍按文字创作提交，不会偷偷变成第四种生成方式
    expect(inspire).not.toContain("REFERENCE_IMAGE");
  });
});

describe("首页作品区", () => {
  it("作品都在仓库内、成组规则诚实、提示词先过公开检查", () => {
    expect(Array.isArray(WORKS)).toBe(true);
    for (const work of WORKS) {
      expect(work.outputs).toHaveLength(2);
      expect(work.authorization.trim().length).toBeGreaterThan(6);
      expect(work.origin.kind).toBe("site-generated");
      expect(work.origin.model.trim().length).toBeGreaterThan(2);
      expect(work.origin.madeAt).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(work.prompt.text.trim().length).toBeGreaterThan(8);
      expect(scanPublicPrompt(work.prompt.text).ok).toBe(true);
      if (work.prompt.kind === "display-only") {
        expect(work.prompt.reason.trim().length).toBeGreaterThan(4);
      }
      // 纯文字生成不该硬配输入图；有输入图就必须拿出"同一次生成"的证据
      if (work.input) {
        expect(work.input.sameRunEvidence.trim().length).toBeGreaterThan(4);
      }
      const images = work.input ? [work.input, ...work.outputs] : [...work.outputs];
      for (const image of images) {
        expect(image.src.startsWith("/art/")).toBe(true);
        expect(inside(image.src)).toBe(true);
        expect(image.alt.trim().length).toBeGreaterThan(2);
      }
    }
  });

  it("一条都没有时也有明确说法，不靠空框或别的来源的图凑版式", () => {
    expect(WORKS_PENDING_NOTE).toContain("没有");
    expect(WORKS_PENDING_NOTE).toContain("冒充");
  });

  it("素材规格里写清了授权、提示词口径与不许展示的来源", () => {
    const spec = WORK_MATERIAL_SPEC.join("｜");
    expect(spec).toContain("授权");
    expect(spec).toContain("提示词");
    expect(spec).toContain("同一次");
    expect(spec).toContain("不得作为作品展示");
  });

  it("点数口径以服务端为准，前端不承诺结算结果", () => {
    expect(POINT_DISCLOSURE).toContain("占用");
    expect(POINT_DISCLOSURE).toContain("退回");
    expect(STEPS).toHaveLength(3);
  });
});
