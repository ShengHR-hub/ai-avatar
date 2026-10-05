/**
 * 公开版提示词的机器检查。
 *
 * 作品区会把提示词直接展示给访客，所以入库前必须先过一遍：只要命中私人信息或内部参数，
 * 界面就**不展示**这段文字，而不是把可疑内容放出去。检查只回答"能不能展示"，
 * 不会自动改写——改写必须由人来做，改完还要说明它是否仍等于当时提交的内容
 * （见 `catalog.ts` 的 `PromptDisclosure`）。
 *
 * 这里不判断"真实姓名"：机器猜不准，属于人工核对项，写在 `WORK_MATERIAL_SPEC` 里。
 */

export type PromptScan = {
  /** 可以公开展示。 */
  ok: boolean;
  /** 命中的类别（只给类别，不回显命中内容，避免二次泄露）。 */
  reasons: string[];
};

type Rule = { id: string; label: string; test: RegExp };

const RULES: Rule[] = [
  { id: "email", label: "邮箱地址", test: /[\w.+-]+@[\w-]+\.[\w.-]+/ },
  { id: "phone", label: "手机号", test: /(?:\+?86[-\s]?)?1[3-9]\d{9}/ },
  { id: "url", label: "网址", test: /https?:\/\/|www\.[\w-]+\.[a-z]{2,}/i },
  {
    id: "path",
    label: "本地路径或文件名",
    test: /[a-z]:\\|\/(?:users|home|tmp|var)\/|\.(?:png|jpe?g|webp|heic|bmp)\b/i,
  },
  {
    id: "internal-param",
    label: "内部参数",
    test: /\b(?:style_id|prompt_template_id|seed|provider|model_name|api[_-]?key|token)\s*[:=]/i,
  },
  { id: "long-token", label: "疑似编号或密钥", test: /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}|\b[A-Za-z0-9_-]{32,}\b/ },
];

/** 扫描一段公开版提示词；`ok` 为 false 时界面应改为"暂不展示"。 */
export function scanPublicPrompt(text: string): PromptScan {
  const value = text.trim();
  if (value.length === 0) return { ok: false, reasons: ["内容为空"] };
  const reasons = RULES.filter((rule) => rule.test.test(value)).map((rule) => rule.label);
  return { ok: reasons.length === 0, reasons };
}

/** 提示词长度上限：展示区不做滚动条，过长的描述直接截断并说明。 */
export const PUBLIC_PROMPT_MAX = 600;

export function clipPrompt(text: string): { text: string; clipped: boolean } {
  const value = text.trim();
  if (value.length <= PUBLIC_PROMPT_MAX) return { text: value, clipped: false };
  return { text: `${value.slice(0, PUBLIC_PROMPT_MAX)}…`, clipped: true };
}
