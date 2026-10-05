import { POLL_GIVE_UP_MS, POLL_INITIAL_MS, POLL_MAX_MS } from "./config";

/**
 * 一次「生成意图」的可恢复状态。
 *
 * 草稿号决定提交幂等键的归属：同一个草稿内重复提交（含网络失败后再点一次）复用同一
 * 个键；任务到达终局后才换新草稿，下一次提交才是新意图。进行中的任务号也存下来，
 * 刷新页面才能接着轮询同一任务而不是重新扣点提交。
 */

const DRAFT_KEY = "ai-avatar:draft-id";
const ACTIVE_KEY = "ai-avatar:active-generation";

function store(): Storage | null {
  return typeof window === "undefined" ? null : window.sessionStorage;
}

function newId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return crypto.randomUUID();
  return `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function currentDraftId(): string {
  const bucket = store();
  if (!bucket) return newId();
  const existing = bucket.getItem(DRAFT_KEY);
  if (existing) return existing;
  const created = newId();
  bucket.setItem(DRAFT_KEY, created);
  return created;
}

export function rotateDraftId(): string {
  const bucket = store();
  const created = newId();
  if (!bucket) return created;
  bucket.setItem(DRAFT_KEY, created);
  return created;
}

export function rememberActiveGeneration(generationId: string): void {
  store()?.setItem(ACTIVE_KEY, generationId);
}

export function readActiveGeneration(): string | null {
  return store()?.getItem(ACTIVE_KEY) ?? null;
}

export function clearActiveGeneration(): void {
  store()?.removeItem(ACTIVE_KEY);
}

export const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "SUCCEEDED",
  "FAILED",
  "NEEDS_REVIEW",
]);

export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}

/** 3 秒起、指数增长、10 秒封顶；实测单次出图约 34 秒，这个节奏既不空转也不刷屏。 */
export function nextDelay(attempt: number, now: number = Date.now(), startedAt?: number): number {
  if (startedAt !== undefined && now - startedAt >= POLL_GIVE_UP_MS) return 0;
  const scaled = POLL_INITIAL_MS * 2 ** Math.max(0, attempt);
  return Math.min(scaled, POLL_MAX_MS);
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * 任务状态对应的界面语义。文案口径来自 `LEAD-04`：`NEEDS_REVIEW` 是「点数仍在预留、
 * 等待人工判定」，绝不能写成「失败退款」；`FAILED + RELEASED` 才是「点数已退回」。
 */
export type GenerationTone = "working" | "success" | "failed" | "review" | "stalled";

export function toneFor(status: string, billingStatus: string): GenerationTone {
  if (status === "SUCCEEDED") return "success";
  if (status === "NEEDS_REVIEW") return "review";
  if (status === "FAILED") return "failed";
  void billingStatus;
  return "working";
}

export const STATUS_COPY: Record<GenerationTone, { title: string; hint: string }> = {
  working: { title: "正在生成", hint: "通常需要 30–90 秒，可以离开本页，回来会继续显示结果。" },
  success: { title: "已完成", hint: "两张结果已就绪，点数已消费。" },
  failed: { title: "生成失败", hint: "点数已全额退回，可以直接重试。" },
  review: {
    title: "等待人工复核",
    hint: "外部结果无法确认，点数仍在预留中，既未消费也未退回，复核完成后会更新状态。",
  },
  stalled: { title: "仍在处理", hint: "等待时间超过预期，请稍后刷新或到「我的生成」查看。" },
};
