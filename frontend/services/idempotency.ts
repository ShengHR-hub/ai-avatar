/**
 * 幂等键：键标识「一次用户意图」，不是「一次网络请求」。
 *
 * 契约第 9 节要求同键同参数回放第一次结果，所以同一意图的重试（含刷新后的重试、
 * 同一张图的重传）必须复用同一个键；意图进入终局后释放，下一次动作才是新意图。
 * 存 `sessionStorage`：同标签页刷新可恢复，关标签页自然清空。
 */

const PREFIX = "ai-avatar:idem:";

function store(): Storage | null {
  return typeof window === "undefined" ? null : window.sessionStorage;
}

function newKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return crypto.randomUUID();
  // 兜底路径只要求唯一且可复用，不承担安全强度
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

export function idempotencyKey(scope: string): string {
  const bucket = store();
  if (!bucket) return newKey();
  const existing = bucket.getItem(PREFIX + scope);
  if (existing) return existing;
  const created = newKey();
  bucket.setItem(PREFIX + scope, created);
  return created;
}

export function releaseIdempotencyKey(scope: string): void {
  store()?.removeItem(PREFIX + scope);
}

/**
 * 同一文件（同摘要同大小）的重传共用一键；换文件就是新意图。
 * `purpose` 必须在键里：同一张图既当"要改造的原图"又当"找灵感的参考图"上传时，
 * 少了这一维就会把两件事当成一件，回放到一个注定失败的意图上。
 */
export const uploadIntentScope = (
  digest: string,
  size: number,
  purpose: "SOURCE" | "REFERENCE" = "SOURCE",
): string => `upload-intent:${purpose}:${digest}:${size}`;

export const completeScope = (assetId: string): string => `complete:${assetId}`;

/**
 * 一次「读这张参考图」的意图。服务端另有更细的去重（同一张图 6 小时内回放、
 * 并发只放行一次），这里的键只保证同一意图的网络重试不会变成两次请求。
 */
export const analysisScope = (assetId: string): string => `analysis:${assetId}`;

/** 一次表单草稿 = 一次生成意图：草稿号不变，重试就复用同一个提交键。 */
export const submitScope = (draftId: string): string => `submit:${draftId}`;

export const retryScope = (generationId: string): string => `retry:${generationId}`;

/**
 * 一次「分享这个任务」的**意图**。
 *
 * 作用域按「任务 + 意图序号」派生：
 *
 * - 同一次意图内的网络重试、双击都复用同一把键（服务端回放同一条链接）；
 * - 用户**明确**点「再生成一条分享链接」时序号 +1，拿到的是新键，
 *   于是新链接被真正创建，而不是回放到上一条（`03-api-contract.md` 第 9 节：
 *   幂等键保证「一次意图只产生一个副作用」，不代表一个任务只能有一条分享）。
 *
 * 仍然不按「选中的图」派生：同一次意图里改选哪几张属于改内容，服务端以稳定冲突拒绝；
 * 想换一组图就递增序号，走一次新意图。
 */
export const shareScope = (generationId: string, attempt = 0): string =>
  `share:${generationId}:${attempt}`;

/** 一次「提交这个任务的反馈」的意图；每用户每任务只有一条反馈。 */
export const feedbackScope = (generationId: string): string =>
  `feedback:${generationId}`;

export const redeemScope = (code: string, email: string): string =>
  `redeem:${code.trim().toUpperCase()}:${email.trim().toLowerCase()}`;

export const emailRequestScope = (email: string): string =>
  `email-request:${email.trim().toLowerCase()}`;

export const verifyScope = (token: string): string => `verify:${token}`;

/** 一次点击取图是一个意图；成功后释放，下次点击才是新意图（短期 URL 会过期）。 */
export const downloadScope = (assetId: string): string => `download:${assetId}`;
