/**
 * 首页静态内容目录。
 *
 * 边界（`docs/product/00-product-direction.md`）：契约里没有风格与灵感模板的读取接口，
 * 所以这里只是前端静态文案。点选方向或标签的效果**只有**「把描述填进创作台」，
 * `services/api.ts` 永远不会发送 `style_id` 或 `prompt_template_id`。
 *
 * 素材诚实性（硬性口径，别为了好看绕过去）：
 * 1. 作品区只放**本站生成链路真实产出**的图，并记录来源、发布授权，
 *    以及实际提交过的提示词（或明确标注的公开改写版）；
 * 2. 纯文字生成的作品不配输入图；只有能证明属于同一次图片改造的素材才允许出现
 *    「输入 → 结果」的成组关系，证据写在 `WorkInput.sameRunEvidence` 里；
 * 3. 第三方摄影素材、`ex/` 里来自小红书的外部参考图、私人测试图，
 *    一律不得作为"作品"出现在这里；
 * 4. 拿不到合格素材就少放几条，不拿别的图凑数。
 */
import type { OpenMode } from "./config";

export const POINT_DISCLOSURE =
  "每次生成固定两张。提交时会先占用点数，成功后扣除，失败自动全额退回；需要人工确认时保持占用，界面不会替你判断结果。";

/* ------------------------------------------------------------------ 作品区 */
export type WorkImage = {
  src: string;
  srcSet?: string;
  width: number;
  height: number;
  alt: string;
};

/**
 * 一张作为"输入"展示的图。除了图本身，还必须写清**凭什么说它和下面两张结果
 * 来自同一次生成**（任务号、当时成组保存的素材等）。说不出证据就不该展示这层关系。
 */
export type WorkInput = WorkImage & { sameRunEvidence: string };

/**
 * 公开版提示词的两种口径，界面必须把区别说出来：
 * - `as-submitted`：这段文字就是当时提交给服务端的内容（只做过排版整理）；
 * - `display-only`：为了公开做过改写（去掉私人信息、内部参数或过于个人的描述），
 *   **不等于**原始提交内容，必须带 `reason` 说明。
 */
export type PromptDisclosure =
  | { kind: "as-submitted"; text: string }
  | { kind: "display-only"; text: string; reason: string };

/** 来源：这张图到底是怎么来的。V1.0 的作品区只接受本站生成链路产出的结果。 */
export type WorkOrigin = {
  kind: "site-generated";
  /** 实际出图的型号，与服务端任务登记的一致；不是营销措辞。 */
  model: string;
  /** 生成日期（ISO 日）。 */
  madeAt: string;
  /** 能取到时写任务号，方便日后核对；取不到就留空并在授权说明里写清楚。 */
  taskId?: string;
};

export type WorkEntry = {
  id: string;
  /** 同一次生成的两张结果，顺序即服务端 `output_index`。 */
  outputs: [WorkImage, WorkImage];
  /** 只有能证明是同一次生成的素材才填这里；纯文字作品留空。 */
  input?: WorkInput;
  /** 这次生成用的方式，用于说明作品怎么来的。 */
  mode: OpenMode;
  /** 一句话说明（方向、耗时、是否满意），可省略。 */
  note?: string;
  prompt: PromptDisclosure;
  origin: WorkOrigin;
  /** 发布授权：谁同意把这张图公开用在本站展示位上。 */
  authorization: string;
};

function workImage(file: string, alt: string, width = 1254, height = 1254): WorkImage {
  const base = `/art/works/${file}`;
  return {
    src: `${base}.png`,
    // 交付用缩小图：同一张原图按比例缩到 768，内容零改动（不裁切、不加滤镜）
    srcSet: `${base}-768.jpg 768w, ${base}.png 1254w`,
    width,
    height,
    alt,
  };
}

/**
 * 首页作品位的数据。每条都要能对外回答"这图哪来的、凭什么给你看、当时写了什么"。
 *
 * `WORKS[0]` 是本站真实生成链路（同一条 Provider 通路 + 固定两张的结算语义）产出的成品，
 * 提示词就是当时提交的内容——纯文字生成，所以没有输入图，这是产品事实而不是省略。
 */
export const WORKS: WorkEntry[] = [
  {
    id: "w1",
    mode: "TEXT_PROMPT",
    outputs: [
      workImage(
        "w1-output-1",
        "本站生成的头像作品一：短发女孩侧脸，手绘质感，暖色柔和光线，背景大面积留白",
      ),
      workImage(
        "w1-output-2",
        "本站生成的头像作品二：同一次生成的另一张结果，同一位侧脸人物与同一套配色",
      ),
    ],
    note: "不上传图片，一句话生成；两张来自同一次提交。",
    prompt: {
      kind: "as-submitted",
      text: "一个短发女孩的侧脸头像，手绘质感，暖色柔和光线，背景大面积留白，气质安静治愈",
    },
    origin: {
      kind: "site-generated",
      model: "gpt-image-2.5-flare",
      madeAt: "2026-09-30",
      taskId: "30aaa307-e6ae-445e-b7ff-ffc70b74ebef",
    },
    authorization:
      "这两张由本站自己的生成链路产出，不含第三方素材或他人作品，站点负责人已同意用于首页展示。",
  },
];

/** 作品区标题下的一句说明，解释这一区放的是什么。 */
export const WORKS_SECTION_SUB =
  "下面每一张都是本站生成链路真实产出的成品；点开可以看到当时提交的描述，改两个字就能带走再用一次。";

/** 一条都没有时的说法：宁可留一句实话，也不摆空框或拿别的图冒充。 */
export const WORKS_PENDING_NOTE = "还没有可公开的真实成品。这里不放示意图，也不拿别的来源的图冒充效果。";

/**
 * 把一段描述带回创作台的链接。作品区与灵感方向共用这一个构造方式，
 * 避免两处各自拼 query 拼出不同结果。
 *
 * `way: "inspire"` 只用于创作台的第三个入口；文字作品仍然按 `TEXT_PROMPT` 提交。
 */
export function studioLink(mode: OpenMode, prompt: string, way?: "inspire"): string {
  const query = new URLSearchParams({ mode, prompt });
  if (way) query.set("way", way);
  return `/studio?${query.toString()}`;
}

/** 交付真实作品时需要满足的素材规格（供素材提供方核对）。 */
export const WORK_MATERIAL_SPEC = [
  "结果图：保留服务端原始尺寸（2026-09-30 实测为 1254×1254 PNG），不加滤镜、不裁切；另存一档 768 宽只做交付用缩小图",
  "同一组：只有能证明是同一次图片改造的素材，才写 1 张输入图 + 对应 2 张结果，并在 sameRunEvidence 里留下证据",
  "来源与授权明确，已获准公开并同意用于本作品集展示",
  "提示词：给出可公开的版本，并说明它是否等于当时实际提交的内容",
  "第三方摄影素材、外部平台参考图、私人测试图一律不得作为作品展示",
  "可选：一句话说明（方向、耗时、是否满意）",
];

/**
 * 入库真实作品时对提示词的要求（`services/promptDisclosure.ts` 会做机器检查）：
 * 不得出现邮箱、手机号、链接、本地路径、文件名、内部参数（如 `style_id=`、
 * `prompt_template_id=`、`seed=`）以及真实姓名。命中任一项时界面宁可不展示，
 * 也不把可疑文本放出去。
 */

/* ------------------------------------------------- 灵感方向（首页只放这一小组） */
/**
 * 首页的灵感方向：**少而有代表性**，一个方向就是一张卡片，点一下带着写好的描述进创作台。
 * 完整的多维标签组合（主题 × 表现 × 情绪 × 场景 × 光线 × 构图）留在创作台的
 * 「灵感标签」里——首页的任务是让人明白"能做什么"，不是把词表摊完。
 *
 * 每条方向的 `selection` 只引用下面 `IDEA_DIMENSIONS` 里真实存在的选项，
 * 深链接的描述由 `composePrompt` 现算，和创作台里勾选出来的字完全一致。
 */
export type HomeDirection = {
  id: string;
  label: string;
  /** 这个方向看起来是什么感觉（卡片上的一行小字）。 */
  hint: string;
  selection: Partial<Record<IdeaDimensionId, string>>;
  extra?: string;
};

export const HOME_DIRECTIONS: HomeDirection[] = [
  {
    id: "social",
    label: "手绘 · 治愈",
    hint: "干净好认，缩到聊天列表也清楚",
    selection: { subject: "person", medium: "hand", mood: "healing", frame: "front" },
  },
  {
    id: "character",
    label: "漫画 · 冷感",
    hint: "角色感强一点，暗底高对比",
    selection: { subject: "character", medium: "comic", mood: "cool", light: "backlight" },
  },
  {
    id: "pet",
    label: "蜡笔 · 宠物",
    hint: "把毛孩子画得跳脱一点",
    selection: { subject: "pet", medium: "crayon", mood: "funny", frame: "half" },
  },
  {
    id: "landscape",
    label: "胶片 · 风景",
    hint: "不出现脸也能当头像",
    selection: { subject: "landscape", medium: "film", scene: "rainynight", mood: "melancholy" },
  },
  {
    id: "back",
    label: "写实 · 背影",
    hint: "留白多一点，不想露脸就用这个",
    selection: { subject: "back", medium: "real", frame: "space", light: "backlight" },
  },
  {
    id: "doll",
    label: "黏土 · 呆萌",
    hint: "玩偶、静物、圆脸小东西都合适",
    selection: { subject: "doll", medium: "clay", mood: "dopey", frame: "front" },
  },
];

/** 首页那句"这里不是全部"的入口文案。 */
export const HOME_IDEAS_MORE = "想自己搭词？创作台里有完整的灵感标签，勾几个就拼好一段描述。";

/* ------------------------------------------------ 创作台灵感分类（可组合目录） */
/**
 * 灵感分类：取代上一版「几个固定模板代表所有玩法」的做法。
 *
 * 设计口径：
 * 1. 每个标签只产出一段**可读、可拼**的描述片段，点选后写进描述框，用户仍然可以改字；
 * 2. 维度之间自由组合（主题 × 表现 × 情绪 × 场景 × 光线 × 构图），不是从固定清单里挑一个；
 * 3. 只作用于 `prompt` 字段。契约里虽然有 `style_id`/`prompt_template_id`，但没有目录读取
 *    接口，前端拿不到合法 UUID，所以**不发送**这两个字段；
 * 4. 不使用受保护角色或 IP 的名称，也不提供"复刻某个角色"的标签。
 *
 * 词表来源：用户给定的分类方向与公开的头像、绘画风格关键词资料。
 * `ex/` 里的图只作内部观察，未核实授权，不参与这里的词表与任何展示素材。
 * 栏目一律叫「灵感分类」——**没有可核验的热度数据，所以不写"热门/热榜"**，
 * 也不给标签排序或加"实时"字样。
 */
export type IdeaDimensionId = "subject" | "medium" | "mood" | "scene" | "light" | "frame";

export type IdeaOption = {
  id: string;
  label: string;
  /** 拼进描述里的短语，本身可读，组合顺序变化也不会变成病句。 */
  fragment: string;
};

export type IdeaDimension = {
  id: IdeaDimensionId;
  label: string;
  /** 这一维在管什么。 */
  hint: string;
  options: IdeaOption[];
};

/** 主体：只有纯文字创作才需要——改造图片时主体来自用户上传的那张图。 */
const SUBJECT: IdeaDimension = {
  id: "subject",
  label: "主题",
  hint: "画的是什么",
  options: [
    { id: "person", label: "人物", fragment: "人物" },
    { id: "character", label: "虚构角色", fragment: "虚构角色" },
    { id: "pet", label: "宠物", fragment: "宠物" },
    { id: "landscape", label: "风景", fragment: "风景" },
    { id: "back", label: "背影", fragment: "背影" },
    { id: "doll", label: "玩偶", fragment: "玩偶" },
    { id: "abstract", label: "抽象", fragment: "抽象" },
    { id: "pair", label: "两个人", fragment: "双人" },
  ],
};

const MEDIUM: IdeaDimension = {
  id: "medium",
  label: "表现",
  hint: "用什么画法",
  options: [
    { id: "hand", label: "手绘", fragment: "手绘" },
    { id: "crayon", label: "蜡笔", fragment: "蜡笔涂鸦" },
    { id: "film", label: "胶片", fragment: "胶片质感" },
    { id: "comic", label: "漫画", fragment: "漫画感" },
    { id: "pixel", label: "像素", fragment: "像素风" },
    { id: "clay", label: "黏土", fragment: "黏土质感" },
    { id: "collage", label: "拼贴", fragment: "拼贴" },
    { id: "real", label: "写实", fragment: "写实" },
  ],
};

const MOOD: IdeaDimension = {
  id: "mood",
  label: "情绪",
  hint: "看起来是什么感觉",
  options: [
    { id: "melancholy", label: "忧郁", fragment: "忧郁安静" },
    { id: "healing", label: "治愈", fragment: "温暖治愈" },
    { id: "funny", label: "搞怪", fragment: "搞怪夸张" },
    { id: "dopey", label: "呆萌", fragment: "呆萌" },
    { id: "cool", label: "冷感", fragment: "冷感克制" },
    { id: "mystery", label: "神秘", fragment: "神秘" },
    { id: "easy", label: "松弛", fragment: "松弛随性" },
    { id: "warm", label: "热烈", fragment: "热烈" },
  ],
};

const SCENE: IdeaDimension = {
  id: "scene",
  label: "场景",
  hint: "在哪里",
  options: [
    { id: "sea", label: "海边", fragment: "海边" },
    { id: "snow", label: "雪地", fragment: "雪地" },
    { id: "rainynight", label: "雨夜", fragment: "雨夜" },
    { id: "forest", label: "森林", fragment: "森林" },
    { id: "window", label: "窗边", fragment: "窗边" },
    { id: "city", label: "城市夜景", fragment: "城市夜景" },
  ],
};

const LIGHT: IdeaDimension = {
  id: "light",
  label: "光线",
  hint: "光从哪来",
  options: [
    { id: "soft", label: "柔和", fragment: "柔和光线" },
    { id: "backlight", label: "逆光", fragment: "逆光轮廓" },
    { id: "warm", label: "暖色", fragment: "暖色调光" },
    { id: "cool", label: "冷色", fragment: "冷色调光" },
  ],
};

const FRAME: IdeaDimension = {
  id: "frame",
  label: "构图",
  hint: "画面怎么取",
  options: [
    { id: "front", label: "正面", fragment: "正面构图" },
    { id: "side", label: "侧脸", fragment: "侧脸构图" },
    { id: "half", label: "半身", fragment: "半身构图" },
    { id: "space", label: "留白", fragment: "大面积留白" },
  ],
};

/** 改造图片时不提供「主题」：主体来自用户上传的那张图，换主题会与"改造"自相矛盾。 */
export const IDEA_DIMENSIONS: Record<OpenMode, IdeaDimension[]> = {
  PHOTO_PROMPT: [MEDIUM, MOOD, SCENE, LIGHT, FRAME],
  TEXT_PROMPT: [SUBJECT, MEDIUM, MOOD, SCENE, LIGHT, FRAME],
};

/** 用途：一键套用一组维度，不是"另一种玩法"。 */
export type UsePreset = {
  id: string;
  label: string;
  desc: string;
  selection: Partial<Record<IdeaDimensionId, string>>;
  /** 额外追加在描述末尾的一句（例如"两张风格统一"）。 */
  extra?: string;
};

export const USE_PRESETS: UsePreset[] = [
  {
    id: "social",
    label: "社交头像",
    desc: "干净、好认，缩到聊天列表也清楚",
    selection: { medium: "hand", mood: "healing", frame: "front" },
  },
  {
    id: "game",
    label: "游戏头像",
    desc: "角色感强一点，暗底高对比",
    selection: { subject: "character", medium: "comic", mood: "cool", light: "backlight" },
  },
  {
    id: "couple",
    label: "情侣头像",
    desc: "两张风格统一、能对上",
    selection: { subject: "pair", medium: "hand", mood: "warm" },
    extra: "两张风格统一，可以拼在一起看",
  },
  {
    id: "friends",
    label: "闺蜜头像",
    desc: "两张风格统一，气质轻快",
    selection: { subject: "pair", medium: "crayon", mood: "easy" },
    extra: "两张风格统一，可以拼在一起看",
  },
  {
    id: "pet",
    label: "宠物头像",
    desc: "把毛孩子放在画面正中",
    selection: { subject: "pet", medium: "hand", mood: "dopey", frame: "half" },
  },
];

/** 首页与创作台共用的一句口径：这是灵感，不是全部玩法。 */
export const IDEA_NOTE = "这些只是灵感分类，不是全部玩法；描述框里随便改，也可以完全自己写。";

/** 首屏下方的一行事实，只写界面能负责的部分。 */
export const HERO_FACTS = ["每次固定两张", "通常 30–90 秒", "可离开，回来接着看"];

/**
 * 创作步骤：只描述用户能感知的动作。
 * 三步说明不是首屏内容，正文压到一句一行，整块排在作品与灵感之后。
 */
export const STEPS = [
  {
    title: "选一种方式",
    body: "写一句话，或者上传一张图片让它在描述下被改造，也可以传一张喜欢的图先读出段描述。",
  },
  {
    title: "挑几个灵感，或者自己写",
    body: "灵感标签点一下就填进描述，之后还能改字。",
  },
  {
    title: "等一会儿，拿两张",
    body: "可以离开页面，回来接着看同一张任务。",
  },
];
