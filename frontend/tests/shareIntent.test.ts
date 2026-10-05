/**
 * 分享意图生命周期（ENG-09 第五轮）：**唯一可信事实**是 `current.key`。
 *
 * 第五轮把键从「任务 + 序号推导」改成「每次意图生成一次的随机串并随记录持久化」。
 * 理由（总控用真实 SDK 请求复现过）：推导键在「清空任务 → 重访同一任务」后序号
 * 从 0 重新开始，于是**重放**第一条分享的历史请求头。
 *
 * 本文件锁死四件事：
 *
 * - 普通重试与刷新重试复用**同一把键**（当前意图不变，且键在第一次用到前已落盘）；
 * - 只有明确动作 `advanceShareIntent` 才启用下一意图；
 * - 记录被清空 / 版本不符 / 结构损坏 / 序号越界 → 整条作废并生成**新的随机键**，
 *   绝不会与任何历史键相同；
 * - 上限不回绕：序号用尽时抛错且不改写记录，已成功的意图仍可读。
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  advanceShareIntent,
  clearShareIntent,
  MAX_ATTEMPT,
  newShareKey,
  readShareIntent,
  rememberShareSlug,
  restoreShareIntent,
  ShareIntentExhausted,
  writeShareIntent,
} from "@/services/shareIntent";

const GENERATION = "g-intent-1";
const STORAGE_KEY = `ai-avatar.share-intent.${GENERATION}`;
const KEY_PATTERN = /^[0-9a-f]{32}$/;

function seed(attempt: number, key: string, nextAttempt: number, slug: string | null = null) {
  writeShareIntent({
    generationId: GENERATION,
    current: { attempt, key, slug },
    nextAttempt,
  });
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  window.sessionStorage.clear();
});

describe("分享意图记录", () => {
  it("没有任何记录时从 0 号意图开始，并**当场把键落盘**（刷新重试才有依据）", () => {
    const intent = restoreShareIntent(GENERATION, 0);

    expect(intent.attempt).toBe(0);
    expect(intent.slug).toBeNull();
    expect(intent.key).toMatch(KEY_PATTERN);

    const record = readShareIntent(GENERATION);
    expect(record?.current.key).toBe(intent.key);
    expect(record?.current.attempt).toBe(0);
    expect(record?.nextAttempt).toBe(1);
  });

  it("普通重试与刷新重试复用同一把键", () => {
    const first = restoreShareIntent(GENERATION, 0);
    // 两次"刷新后恢复"读到的键必须一致
    const second = restoreShareIntent(GENERATION, 0);
    const third = restoreShareIntent(GENERATION, 0);
    expect(second.key).toBe(first.key);
    expect(third.key).toBe(first.key);
  });

  it("当前意图与下一意图各自是明确字段（不是靠推算）", () => {
    const key = newShareKey();
    seed(3, key, 4, "slug-a");

    const record = readShareIntent(GENERATION);
    expect(record?.current.attempt).toBe(3);
    expect(record?.current.key).toBe(key);
    expect(record?.current.slug).toBe("slug-a");
    expect(record?.nextAttempt).toBe(4);
  });

  it("只有明确动作才启用下一意图，且新键从未被使用过", () => {
    const firstKey = newShareKey();
    seed(0, firstKey, 1, "slug-c");

    const used = new Set([firstKey]);
    const next = advanceShareIntent(GENERATION, 0);
    expect(next.attempt).toBe(1);
    expect(next.key).toMatch(KEY_PATTERN);
    expect(used.has(next.key)).toBe(false);

    // 再次明确动作 → 又是新键，且不回绕
    const third = advanceShareIntent(GENERATION, next.attempt);
    expect(third.attempt).toBe(2);
    expect([...used, next.key]).not.toContain(third.key);
  });

  it("**清空后重访同一任务**：绝不静默复用历史键", () => {
    const historical = restoreShareIntent(GENERATION, 0).key;
    const second = advanceShareIntent(GENERATION, 0).key;
    expect(second).not.toBe(historical);

    // 用户清空任务 → 记录被移除；重访同一任务时必须是一把新键
    clearShareIntent(GENERATION);
    expect(readShareIntent(GENERATION)).toBeNull();

    const revisited = restoreShareIntent(GENERATION, 0);
    expect(revisited.key).not.toBe(historical);
    expect(revisited.key).not.toBe(second);
  });

  it("旧版（v2 推导键）记录整条作废，不会留下半条旧键", () => {
    // 第四轮的形状：键是 `share:<任务>:<序号>`
    window.sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        version: 2,
        generationId: GENERATION,
        current: { attempt: 2, key: `share:${GENERATION}:2`, slug: "stale-slug" },
        nextAttempt: 3,
      }),
    );
    expect(readShareIntent(GENERATION)).toBeNull();

    const restored = restoreShareIntent(GENERATION, 0);
    expect(restored.key).toMatch(KEY_PATTERN);
    expect(restored.key).not.toContain("stale-slug");
    expect(restored.attempt).toBe(0);
  });

  it("记录损坏或字段不成立时整条作废", () => {
    const cases: string[] = [
      JSON.stringify({ version: 1, generationId: GENERATION, nextAttempt: 7 }),
      JSON.stringify({
        version: 3,
        generationId: "另一个任务",
        current: { attempt: 0, key: newShareKey(), slug: null },
        nextAttempt: 1,
      }),
      JSON.stringify({
        version: 3,
        generationId: GENERATION,
        current: { attempt: 0, key: "not-a-nonce", slug: null },
        nextAttempt: 1,
      }),
      JSON.stringify({
        version: 3,
        generationId: GENERATION,
        current: { attempt: 0, key: newShareKey(), slug: null },
        // 下一意图不晚于当前意图 → 会让"再生成一条"复用旧序号
        nextAttempt: 0,
      }),
      "不是 JSON",
    ];

    for (const raw of cases) {
      window.sessionStorage.setItem(STORAGE_KEY, raw);
      expect(readShareIntent(GENERATION)).toBeNull();
    }

    // 记录被作废后，新意图从 0 开始且拿到新键
    const restored = restoreShareIntent(GENERATION, 0);
    expect(restored.attempt).toBe(0);
    expect(restored.key).toMatch(KEY_PATTERN);
  });

  it("序号越界同样作废，不会因为越界而回退到 0 继续创建", () => {
    seed(MAX_ATTEMPT, newShareKey(), MAX_ATTEMPT);
    expect(readShareIntent(GENERATION)).toBeNull();
  });

  it("上限意图成功后刷新：记录仍可读，且不会回绕到 0", () => {
    // 当前意图是最后可用序号（MAX_ATTEMPT - 1），已成功建出链接
    const attempt = MAX_ATTEMPT - 1;
    const key = newShareKey();
    rememberShareSlug(GENERATION, { attempt, key }, "slug-last");

    const record = readShareIntent(GENERATION);
    expect(record?.current.attempt).toBe(attempt);
    expect(record?.current.key).toBe(key);
    expect(record?.current.slug).toBe("slug-last");
    expect(record?.nextAttempt).toBe(MAX_ATTEMPT);

    // 刷新后仍能读到"已经建出的链接"，键不变
    expect(restoreShareIntent(GENERATION, 0).key).toBe(key);

    // 再要一条：明确失败，且记录**没有被改写**（不回绕、不降级）
    expect(() => advanceShareIntent(GENERATION, attempt)).toThrow(ShareIntentExhausted);
    const after = readShareIntent(GENERATION);
    expect(after?.current.attempt).toBe(attempt);
    expect(after?.current.key).toBe(key);
    expect(after?.current.slug).toBe("slug-last");
    expect(after?.nextAttempt).toBe(MAX_ATTEMPT);
  });

  it("推进时把 slug 清空（新意图还没有链接），且键与记录一致", () => {
    seed(0, newShareKey(), 1, "old");
    const next = advanceShareIntent(GENERATION, 0);
    const record = readShareIntent(GENERATION);
    expect(record?.current.slug).toBeNull();
    expect(record?.current.key).toBe(next.key);
  });

  it("每次生成的键都不同（随机、不可预测）", () => {
    const keys = new Set<string>();
    for (let index = 0; index < 50; index += 1) {
      keys.add(newShareKey());
    }
    expect(keys.size).toBe(50);
    for (const key of keys) {
      expect(key).toMatch(KEY_PATTERN);
    }
  });
});
