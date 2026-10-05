/**
 * 灵感分类的组合逻辑：不调用任何模型，只把用户点选的标签拼成一段**可继续编辑**的描述。
 *
 * 四条不可让的口径：
 * 1. 只写 `prompt` 这一个字段——不产生 `style_id`、`prompt_template_id` 之类的请求字段
 *    （契约里虽有这两个字段，但没有目录读取接口，前端拿不到合法 UUID）。
 * 2. **不覆盖用户自己写的内容**。用户已有文字时，点选只会把片段追加到末尾。
 * 3. 点选结果对用户可见可撤销：界面必须显示"将要追加的文字"，并提供撤销与恢复原文。
 * 4. 标签词表在 `catalog.ts` 的 `IDEA_DIMENSIONS`，这里只负责拼句与去重。
 */
import { IDEA_DIMENSIONS, type IdeaDimensionId, type UsePreset } from "./catalog";
import type { OpenMode } from "./config";

export type AssistSelection = Partial<Record<IdeaDimensionId, string>>;

/** 契约固定正方形输出，拼句时统一补在末尾。 */
export const ASSIST_TAIL = "正方形构图";

/**
 * 句子的开头由哪一维决定：
 * - 纯文字创作：由「主题」决定（生成宠物头像 / 生成一张头像）；
 * - 改造图片：由「表现」决定（把这张图片改成蜡笔涂鸦头像 / 把这张图片改成头像）。
 * 这一维的片段只出现在开头，不会再重复拼一遍。
 */
const HEAD_DIMENSION: Record<OpenMode, IdeaDimensionId> = {
  PHOTO_PROMPT: "medium",
  TEXT_PROMPT: "subject",
};

const HEAD: Record<OpenMode, { lead: (style: string) => string; plain: string }> = {
  PHOTO_PROMPT: {
    lead: (style) => `把这张图片改成${style}头像`,
    plain: "把这张图片改成头像",
  },
  TEXT_PROMPT: {
    lead: (style) => `生成${style}头像`,
    plain: "生成一张头像",
  },
};

export function selectionCount(selection: AssistSelection): number {
  return Object.values(selection).filter(Boolean).length;
}

/** 按维度顺序取出选中片段。 */
export function selectedFragments(mode: OpenMode, selection: AssistSelection): string[] {
  return IDEA_DIMENSIONS[mode]
    .map((dimension) => {
      const picked = selection[dimension.id];
      return picked ? dimension.options.find((option) => option.id === picked)?.fragment ?? null : null;
    })
    .filter((value): value is string => Boolean(value));
}

export type AssistPreview = {
  /** `compose` = 用户还没写东西，点选会生成整段描述；`append` = 只往已有描述后面追加。 */
  action: "compose" | "append";
  /** 点击后会写进描述框的完整文字；没有选择时就是用户自己写的内容。 */
  text: string;
  /** 本次会新增的片段，用于界面逐条显示"将发生什么"。 */
  added: string[];
};

/**
 * 预览点击后的结果。`base` 是用户自己写的部分（不含灵感片段）。
 *
 * 去重规则：片段或结尾如果已经原样出现在 `base` 里，就不再重复追加——
 * 用户从首页灵感入口带进来的描述已经含了这些词，重复拼一遍会变成啰嗦的病句。
 */
export function assistPreview(
  mode: OpenMode,
  base: string,
  selection: AssistSelection,
  extra?: string,
): AssistPreview {
  const trimmed = base.trim();
  const fragments = selectedFragments(mode, selection);
  const extras = extra ? [extra] : [];

  if (fragments.length === 0 && extras.length === 0) {
    return { action: trimmed ? "append" : "compose", text: trimmed, added: [] };
  }

  if (!trimmed) {
    const headFragment = selection[HEAD_DIMENSION[mode]]
      ? IDEA_DIMENSIONS[mode]
          .find((dimension) => dimension.id === HEAD_DIMENSION[mode])
          ?.options.find((option) => option.id === selection[HEAD_DIMENSION[mode]])?.fragment
      : undefined;
    const head = headFragment ? HEAD[mode].lead(headFragment) : HEAD[mode].plain;
    const parts = [...fragments.filter((fragment) => fragment !== headFragment), ...extras].filter(
      (fragment) => !head.includes(fragment),
    );
    return {
      action: "compose",
      text: [head, ...parts, ASSIST_TAIL].join("，"),
      added: fragments,
    };
  }

  const added = [...fragments, ...extras].filter((fragment) => !trimmed.includes(fragment));
  const tail = trimmed.includes(ASSIST_TAIL) ? [] : [ASSIST_TAIL];
  const text = added.length === 0 && tail.length === 0 ? trimmed : [trimmed, ...added, ...tail].join("，");
  return { action: "append", text, added };
}

/** 把选择结果合成最终描述（`base` 为用户自己的文字）。 */
export function composePrompt(
  mode: OpenMode,
  base: string,
  selection: AssistSelection,
  extra?: string,
): string {
  return assistPreview(mode, base, selection, extra).text;
}

/** 用途预设一键套用：返回新的选择与附加句。 */
export function applyPreset(preset: UsePreset): { selection: AssistSelection; extra?: string } {
  return { selection: { ...preset.selection }, extra: preset.extra };
}
