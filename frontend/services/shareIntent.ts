/**
 * 分享「意图」的持久化（ENG-09 第五轮返修）。
 *
 * ## 一条记录里同时存在两个不同的东西
 *
 * ```text
 * current  当前意图：正在被使用/已经建出链接的那一次意图，它的 key 就是本次幂等键
 * next     下一意图：只有用户**明确**点「再生成一条分享链接」时才会被启用
 * ```
 *
 * `current.key` 是**唯一的可信事实**：它会被原样放进 `POST /api/v1/shares` 的
 * `Idempotency-Key` 请求头（见 `hooks/useStudio.ts` 与 `services/api.ts`）。
 * 除此之外没有任何「另一份缓存」参与生成分享的幂等键。
 *
 * ## 为什么 key 是**随机且被持久化**的，而不是由任务+序号推导
 *
 * 第四轮把 key 定义成 `share:<任务>:<序号>`。它有两个致命后果：
 *
 * 1. 序号在「清空任务 → 重访同一任务」后会从 0 重新开始，于是**重放**第一条分享的
 *    历史请求头（总控用真实 SDK 请求复现过）；
 * 2. 同一把键既是「记录里的值」又是「可以随时推导出来的值」，两份事实必然漂移。
 *
 * 现在 key 是**每次意图生成一次的不可预测随机串**（32 位十六进制），并且随记录一起
 * 写入 `sessionStorage`：
 *
 * - 同一次意图（普通重试、刷新后重试）→ 从记录里读回**同一把键**；
 * - 明确的新意图（`advanceShareIntent`）→ 新随机键；
 * - 记录被清空 / 版本不符 / 结构损坏 / 越界 → 整条作废并生成**新的随机键**，
 *   绝不会与任何历史键相同（随机串无法被"推算回"历史值）。
 *
 * 键的格式（`KEY_PATTERN`）也被校验：`share:<任务>:<序号>` 这类旧版推导键不满足格式，
 * 于是旧记录会被判为损坏并作废 —— 这正是「旧版记录不得静默复用历史键」的落实。
 *
 * ## 上限
 *
 * 序号用尽时 `advanceShareIntent` 抛 `ShareIntentExhausted`，**不回绕、不改写记录**：
 * 回绕会让新键与历史键相同，那正是本文件要杜绝的事。已经成功的意图不受影响
 * （它的键仍然可读、可回放）；只有"再要一条"会明确失败。
 *
 * ## 为什么是 sessionStorage
 *
 * 一次"这个任务我要分享哪些图"的意图属于这一次浏览会话。关掉标签页就是新的一次
 * 意图（服务端的幂等登记也只要求同一意图内唯一）；`localStorage` 会让记录永久累积
 * 并跨标签页互相干扰。
 *
 * **不存**任何内部事实：没有用户标识、没有选中资产、没有令牌。`key` 是随机串，
 * 不是凭据（服务端仍以唯一约束为最终裁决者）。
 */

const VERSION = 3;

/** 序号上限：超过它说明存储被改坏或真的用尽了，按"不能安全新建"处理。 */
export const MAX_ATTEMPT = 10_000;

/** 服务端 `Idempotency-Key` 的硬上限（契约 `components.parameters.IdempotencyKey`）。 */
export const MAX_KEY_LENGTH = 128;

/**
 * 合法 key 的形状：32 位小写十六进制（16 字节随机）。
 *
 * 这是「旧版推导键会被判为损坏」的判据：`share:<generationId>:<n>` 里含 `:` 与字母，
 * 不匹配该模式，因此旧记录整条作废、重新生成随机键，不会静默复用历史键。
 */
const KEY_PATTERN = /^[0-9a-f]{32}$/;

export type ShareIntentRecord = {
  generationId: string;
  /** 当前意图（正在使用/已建出链接的那一次）。`key` 就是要发出去的幂等键。 */
  current: { attempt: number; key: string; slug: string | null };
  /** 下一意图的序号；只有明确动作才会把它提升为 current。 */
  nextAttempt: number;
};

/** 序号用尽：不能再安全地产生新键（绝不回绕到 0）。 */
export class ShareIntentExhausted extends Error {
  constructor() {
    super("这个作品的分享序号已经用尽，无法再安全地创建新的分享链接。");
    this.name = "ShareIntentExhausted";
  }
}

function keyFor(generationId: string): string {
  return `ai-avatar.share-intent.${generationId}`;
}

/** 生成一次意图的随机幂等键（16 字节随机 → 32 位十六进制）。 */
export function newShareKey(): string {
  const bytes = new Uint8Array(16);
  const cryptoRef = globalThis.crypto;
  if (typeof cryptoRef?.getRandomValues === "function") {
    cryptoRef.getRandomValues(bytes);
  } else {
    // 兜底路径只要求唯一且不可预测到"不会撞上历史值"，不承担密码学强度
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

/** 当前意图序号：`0..MAX_ATTEMPT-1`（最后一个可用序号是 `MAX_ATTEMPT-1`）。 */
function isCurrentAttempt(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 0 &&
    value < MAX_ATTEMPT
  );
}

/** 下一意图序号：`1..MAX_ATTEMPT`，且必须严格晚于当前意图。 */
function isNextAttempt(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= 1 &&
    value <= MAX_ATTEMPT
  );
}

function isKey(value: unknown): value is string {
  return typeof value === "string" && KEY_PATTERN.test(value);
}

/** 读取并校验记录；任何不成立的字段都让整条记录作废（当成"没有意图"）。 */
export function readShareIntent(generationId: string): ShareIntentRecord | null {
  if (typeof window === "undefined") return null;
  let raw: string | null = null;
  try {
    raw = window.sessionStorage.getItem(keyFor(generationId));
  } catch {
    // 隐私模式等场景下 sessionStorage 不可用：退化成"每次都新意图"，
    // 不抛错、不阻塞分享功能
    return null;
  }
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const record = parsed as Record<string, unknown>;
    if (record.version !== VERSION) return null;
    if (record.generationId !== generationId) return null;

    const current = record.current;
    if (typeof current !== "object" || current === null) return null;
    const currentRecord = current as Record<string, unknown>;
    const attempt = currentRecord.attempt;
    const key = currentRecord.key;
    const slug = currentRecord.slug;
    if (!isCurrentAttempt(attempt)) return null;
    if (!isKey(key) || key.length > MAX_KEY_LENGTH) return null;
    if (slug !== null && typeof slug !== "string") return null;

    const nextAttempt = record.nextAttempt;
    if (!isNextAttempt(nextAttempt)) return null;
    // 下一意图必须严格晚于当前意图，否则「再生成一条」会拿到已经用过的序号
    if (nextAttempt <= attempt) return null;

    return {
      generationId,
      current: { attempt, key, slug: slug ?? null },
      nextAttempt,
    };
  } catch {
    return null;
  }
}

/** 覆盖写入记录；存储不可用时静默降级（分享本身不能因此失败）。 */
export function writeShareIntent(record: ShareIntentRecord): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(
      keyFor(record.generationId),
      JSON.stringify({ version: VERSION, ...record }),
    );
  } catch {
    // 同上：降级而不是抛出
  }
}

/** 清掉某个任务的意图（任务被清空时用，避免下次回到它时复用旧意图）。 */
export function clearShareIntent(generationId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(keyFor(generationId));
  } catch {
    // 忽略
  }
}

/**
 * 恢复意图：返回当前意图（键/序号/已建出的 slug）。
 *
 * 没有可用记录时**当场生成一把新的随机键并写入**（而不是只返回一个临时值）：
 * 「刷新后重试」必须复用原键，因此键必须在第一次用到之前就落进持久记录。
 */
export function restoreShareIntent(
  generationId: string,
  inMemoryAttempt: number,
): { attempt: number; key: string; slug: string | null } {
  const stored = readShareIntent(generationId);
  if (stored) {
    return {
      attempt: stored.current.attempt,
      key: stored.current.key,
      slug: stored.current.slug,
    };
  }
  const attempt = isCurrentAttempt(inMemoryAttempt) ? inMemoryAttempt : 0;
  const key = newShareKey();
  writeShareIntent({
    generationId,
    current: { attempt, key, slug: null },
    nextAttempt: attempt + 1,
  });
  return { attempt, key, slug: null };
}

/**
 * **明确**开启下一次意图：把 `next` 提升为 `current`，并给出新的下一序号。
 *
 * 只有用户的"再生成一条分享链接"应该调用它。返回新的当前意图。
 * 序号用尽时抛 `ShareIntentExhausted`，记录保持不变。
 */
export function advanceShareIntent(
  generationId: string,
  inMemoryAttempt: number,
): { attempt: number; key: string } {
  const stored = readShareIntent(generationId);
  const baseNext = stored
    ? stored.nextAttempt
    : (isCurrentAttempt(inMemoryAttempt) ? inMemoryAttempt : 0) + 1;

  const attempt = baseNext;
  const following = attempt + 1;
  if (following > MAX_ATTEMPT) {
    // 不回绕、不改写：把"不能再安全新建"如实报出去
    throw new ShareIntentExhausted();
  }
  const key = newShareKey();
  writeShareIntent({
    generationId,
    current: { attempt, key, slug: null },
    nextAttempt: following,
  });
  return { attempt, key };
}

/** 记下当前意图已经建出的链接（刷新后据此重新读取，而不是重新创建）。 */
export function rememberShareSlug(
  generationId: string,
  intent: { attempt: number; key: string },
  slug: string,
): void {
  writeShareIntent({
    generationId,
    current: { attempt: intent.attempt, key: intent.key, slug },
    nextAttempt: intent.attempt + 1,
  });
}
