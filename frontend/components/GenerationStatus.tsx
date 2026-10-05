"use client";

import type { DownloadUrlView } from "@/services/api";
import { failureTitle, type ApiFailure } from "@/services/errors";
import { STATUS_COPY, toneFor } from "@/services/generation";
import type { WatchSnapshot } from "@/services/watch";
import { Notice } from "./Notice";
import { ProgressPanel } from "./ProgressPanel";
import { ResultPair } from "./ResultPair";

type DownloadState = DownloadUrlView | "loading" | "error" | undefined;

/**
 * 等待与终局呈现。按钮与文案严格跟随服务端状态：`NEEDS_REVIEW` 是「点数仍在预留、
 * 等人工判定」，绝不能写成「失败退款」；只有失败才是「点数已退回」。
 */
export function GenerationStatus({
  watch,
  resumed,
  failure,
  downloads,
  busy,
  onDownload,
  onRetry,
  onRegenerate,
}: {
  watch: WatchSnapshot | null;
  resumed: boolean;
  failure: ApiFailure | null;
  downloads: Record<string, DownloadState>;
  busy: boolean;
  onDownload: (assetId: string) => void | Promise<void>;
  onRetry: () => void;
  onRegenerate: () => void;
}) {
  if (failure && !watch) {
    return (
      <Notice tone="danger" title={failureTitle(failure.code)} failure={failure}>
        {failure.message}
      </Notice>
    );
  }
  if (!watch) return null;

  if (watch.stalled) {
    return (
      <Notice tone="warning" title={STATUS_COPY.stalled.title}>
        {STATUS_COPY.stalled.hint}
      </Notice>
    );
  }

  const tone = toneFor(watch.status, watch.billingStatus);

  if (tone === "working") {
    return <ProgressPanel resumed={resumed} />;
  }

  if (tone === "success") {
    return (
      <ResultPair
        outputs={watch.outputs}
        downloads={downloads}
        busy={busy}
        onDownload={onDownload}
        onRegenerate={onRegenerate}
      />
    );
  }

  const copy = tone === "review" ? STATUS_COPY.review : STATUS_COPY.failed;

  return (
    <section aria-live="polite" aria-label="生成状态">
      <Notice tone={tone === "review" ? "warning" : "danger"} title={copy.title}>
        {copy.hint}
        {watch.failureCode ? (
          <>
            {" "}
            详细原因对照问题编号 <code>{watch.failureCode}</code>。
          </>
        ) : null}
      </Notice>

      {tone === "failed" ? (
        <div className="btnrow btnrow--top">
          <button type="button" className="btn btn--primary" onClick={onRetry} disabled={busy}>
            {busy ? "正在重试…" : "重试这一次生成"}
          </button>
        </div>
      ) : null}
    </section>
  );
}
