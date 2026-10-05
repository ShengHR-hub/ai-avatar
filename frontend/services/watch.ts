import { fetchGeneration, fetchOutputs, type OutputView } from "./api";
import { POLL_GIVE_UP_MS } from "./config";
import { clearActiveGeneration, isTerminal, nextDelay, rememberActiveGeneration, sleep } from "./generation";

export type WatchSnapshot = {
  status: string;
  billingStatus: string;
  failureCode: string | null;
  outputs: OutputView[];
  /** 超过 `POLL_GIVE_UP_MS` 仍未终局：停止刷屏，交给用户手动刷新。 */
  stalled: boolean;
};

export type WatchInput = {
  generationId: string;
  onSnapshot: (snapshot: WatchSnapshot) => void;
  signal?: AbortSignal;
};

/**
 * 唯一的轮询实现：页面、刷新恢复、重试后都走这里，不在多处各写一套循环
 * （`06-development-standard.md` 第 3 节明令禁止复制轮询逻辑）。
 *
 * 结果只在终局后取 `outputs`：`SUCCEEDED` 之外的任何状态都不许出现「半成品」列表。
 */
export async function watchGeneration(input: WatchInput): Promise<WatchSnapshot> {
  const startedAt = Date.now();
  let attempt = 0;
  let last: WatchSnapshot = {
    status: "QUEUED",
    billingStatus: "RESERVED",
    failureCode: null,
    outputs: [],
    stalled: false,
  };

  while (!input.signal?.aborted) {
    const generation = await fetchGeneration(input.generationId);
    const terminal = isTerminal(generation.status);
    let outputs: OutputView[] = [];
    if (generation.status === "SUCCEEDED") {
      outputs = await fetchOutputs(input.generationId);
    }
    if (terminal) clearActiveGeneration();
    else rememberActiveGeneration(input.generationId);

    last = {
      status: generation.status,
      billingStatus: generation.billing_status,
      failureCode: generation.failure_code ?? null,
      outputs,
      stalled: false,
    };
    input.onSnapshot(last);
    if (terminal) return last;

    const delay = nextDelay(attempt, Date.now(), startedAt);
    if (delay === 0) {
      const stalledSnapshot: WatchSnapshot = { ...last, stalled: true };
      input.onSnapshot(stalledSnapshot);
      return stalledSnapshot;
    }
    attempt += 1;
    await sleepWithAbort(delay, input.signal);
  }

  return last;
}

async function sleepWithAbort(ms: number, signal?: AbortSignal): Promise<void> {
  if (!signal) {
    await sleep(ms);
    return;
  }
  await new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      resolve();
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export { POLL_GIVE_UP_MS };
