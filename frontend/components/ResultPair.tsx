"use client";

import { useEffect, useMemo, useState } from "react";

import type { DownloadUrlView, OutputView } from "@/services/api";
import { STATUS_COPY } from "@/services/generation";
import { ImageViewer, type ViewerItem } from "./ImageViewer";

type DownloadState = DownloadUrlView | "loading" | "error" | undefined;

/**
 * 短期下载地址的有效期：给一个本地时间 + 剩余分钟，比原始 ISO 串有用得多。
 * 结果区只在客户端出现（没有服务端快照就不渲染），因此这里读时钟不会造成水合不一致。
 */
function expiryText(iso: string, now: number): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return `地址有效期到 ${iso}。`;
  const clock = `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
  const left = Math.ceil((at.getTime() - now) / 60_000);
  if (left <= 0) return `地址在 ${clock} 已经过期，重新获取一次就能继续下载。`;
  if (left <= 3) return `地址有效期到 ${clock}，只剩 ${left} 分钟，过期后重新获取。`;
  return `地址有效期到 ${clock}（约 ${left} 分钟后失效）。`;
}

function isExpired(iso: string, now: number): boolean {
  const at = new Date(iso).getTime();
  return Number.isFinite(at) && at <= now;
}

/**
 * 两张成品就是结果页的主体：大图直接铺开，每张都有自己的下载动作。
 *
 * 结果数量由契约固定为 2，界面按服务端给的 `output_index` 顺序呈现，不自行补齐。
 * 图片地址只能来自 `POST /assets/{id}/download-url` 签发的短期 URL，前端不拼对象路径。
 * 比例用服务端给的真实宽高，容器负责比例、img 只负责填充，避免高度失控。
 */
export function ResultPair({
  outputs,
  downloads,
  busy,
  onDownload,
  onRegenerate,
}: {
  outputs: OutputView[];
  downloads: Record<string, DownloadState>;
  busy: boolean;
  onDownload: (assetId: string) => void | Promise<void>;
  onRegenerate: () => void;
}) {
  // 短期地址会过期：链接还在时每 30 秒刷新一次剩余时间，让"还剩多久"是真的
  const [now, setNow] = useState(() => Date.now());
  /** 手机上默认单列大图；想对照时切成并排两列。 */
  const [compare, setCompare] = useState(false);
  const [viewerAt, setViewerAt] = useState<number | null>(null);
  const hasLink = Object.values(downloads).some((state) => typeof state === "object" && state !== null);

  useEffect(() => {
    if (!hasLink) return;
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [hasLink]);

  const viewerItems = useMemo<ViewerItem[]>(
    () =>
      outputs.flatMap((output, index) => {
        const state = downloads[output.assetId];
        if (typeof state !== "object" || state === null) return [];
        return [
          {
            src: state.url,
            alt: `第 ${output.outputIndex + 1} 张生成结果`,
            caption: `第 ${output.outputIndex + 1} 张 · ${output.width ?? "?"} × ${output.height ?? "?"}`,
            downloadHref: state.url,
            downloadName: `avatar-${output.outputIndex + 1}.png`,
          },
        ];
      }),
    [downloads, outputs],
  );

  return (
    <section aria-labelledby="results-title">
      <div className="section__head">
        <div>
          <p className="eyebrow">生成完成</p>
          <h2 className="section__title" id="results-title">
            你的两张头像
          </h2>
          <p className="section__sub">{STATUS_COPY.success.hint}</p>
        </div>
        <div className="result__head-acts">
          <button type="button" className="btn" aria-pressed={compare} onClick={() => setCompare((value) => !value)}>
            {compare ? "改回单张大图" : "并排对比"}
          </button>
          <button type="button" className="btn" onClick={onRegenerate} disabled={busy}>
            用同样的描述再来两张
          </button>
        </div>
      </div>

      {outputs.length === 0 ? (
        <p className="hint">这次没有返回可用结果，可以看下面的状态说明。</p>
      ) : (
        <ul className={compare ? "results results--pair results--compare" : "results results--pair"}>
          {outputs.map((output) => {
            const state = downloads[output.assetId];
            const link = typeof state === "object" ? state : null;
            const ratio = output.width && output.height ? `${output.width} / ${output.height}` : "1 / 1";
            const stale = link ? isExpired(link.expiresAt, now) : false;

            return (
              <li className="result" key={output.assetId}>
                <div className="media result__media" style={{ aspectRatio: ratio }}>
                  {link ? (
                    <>
                      <img src={link.url} alt={`第 ${output.outputIndex + 1} 张生成结果`} decoding="async" />
                      <button
                        type="button"
                        className="result__zoom"
                        onClick={() => setViewerAt(output.outputIndex)}
                        aria-label={`放大查看第 ${output.outputIndex + 1} 张`}
                      >
                        <span className="result__zoom-badge" aria-hidden="true">
                          放大查看
                        </span>
                      </button>
                      {stale ? <span className="result__stale">下载地址已过期</span> : null}
                    </>
                  ) : (
                    <div className="result__hold">
                      {state === "error" ? (
                        <>
                          <span>图片地址没取到</span>
                          <button
                            type="button"
                            className="btn btn--quiet"
                            onClick={() => void onDownload(output.assetId)}
                          >
                            再试一次
                          </button>
                        </>
                      ) : (
                        <>
                          <span className="spin" aria-hidden="true" />
                          <span>正在取图…</span>
                        </>
                      )}
                    </div>
                  )}
                </div>

                <div className="result__bar">
                  <p className="result__meta">
                    第 {output.outputIndex + 1} 张 · {output.width ?? "?"} × {output.height ?? "?"} ·{" "}
                    {output.mimeType.replace("image/", "").toUpperCase()} ·{" "}
                    {Math.round(output.sizeBytes / 1024)} KB
                  </p>
                  <div className="result__acts">
                    {link ? (
                      stale ? (
                        <button
                          type="button"
                          className="btn btn--primary"
                          onClick={() => void onDownload(output.assetId)}
                          disabled={busy}
                        >
                          重新获取下载地址
                        </button>
                      ) : (
                        <>
                          <a
                            className="btn btn--primary"
                            href={link.url}
                            download={`avatar-${output.outputIndex + 1}.png`}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            下载这一张
                          </a>
                          <button
                            type="button"
                            className="btn btn--quiet"
                            onClick={() => void onDownload(output.assetId)}
                          >
                            刷新地址
                          </button>
                        </>
                      )
                    ) : null}
                  </div>
                </div>
                {link ? <p className="result__exp">{expiryText(link.expiresAt, now)}</p> : null}
              </li>
            );
          })}
        </ul>
      )}

      {viewerAt !== null && viewerItems.length > 0 ? (
        <ImageViewer items={viewerItems} startIndex={viewerAt} onClose={() => setViewerAt(null)} />
      ) : null}
    </section>
  );
}
