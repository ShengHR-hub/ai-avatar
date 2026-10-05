"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  createDownloadUrl,
  fetchShare,
  type DownloadUrlView,
  type ShareView,
} from "@/services/api";
import { ApiFailure, failureTitle } from "@/services/errors";

type LoadState =
  | { stage: "loading" }
  | { stage: "ready"; share: ShareView }
  | { stage: "missing" }
  | { stage: "failed"; failure: ApiFailure | null };

type UrlState = DownloadUrlView | "loading" | "expired";

/**
 * 匿名分享页（`/s/[slug]`）。
 *
 * 边界（`03-api-contract.md` 第 4.4 节、`04-ai-provider.md` 第 8 节）：
 *
 * - 未登录也能看：`GET /shares/{slug}` 是 `security: []` 的匿名读取；
 * - 图片仍然只通过**短期签名下载地址**渲染，前端不拼任何对象路径；
 * - 不存在 / 已撤销 / 已过期一律按 404 处理，界面只说「链接失效」，
 *   不区分也不泄露是哪种原因（服务端同样只回 404）；
 * - 页面不展示所有者、Prompt、存储信息或任何内部 ID —— 分享载荷里本来就没有。
 */
export function SharePage({ slug }: { slug: string }) {
  const [state, setState] = useState<LoadState>({ stage: "loading" });
  const [urls, setUrls] = useState<Record<string, UrlState>>({});
  /**
   * 重试序号：把它并进 effect 依赖，就能用同一个 effect 完成"加载 / 重新加载"，
   * 不需要第二套加载逻辑（两套逻辑迟早会走偏）。
   */
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    setState({ stage: "loading" });
    fetchShare(slug)
      .then((share) => {
        if (alive) setState({ stage: "ready", share });
      })
      .catch((error: unknown) => {
        if (!alive) return;
        if (error instanceof ApiFailure && error.code === "GENERATION_NOT_FOUND") {
          setState({ stage: "missing" });
          return;
        }
        setState({
          stage: "failed",
          failure: error instanceof ApiFailure ? error : null,
        });
      });
    return () => {
      alive = false;
    };
  }, [slug, attempt]);

  /** 首次加载失败、链接失效或图片地址过期时的统一重试入口。 */
  const retry = useCallback(() => {
    // 重新加载时把已缓存的图片地址一并丢弃：它们可能正是"服务端刚恢复，
    // 但本地还握着旧结果"的来源。
    setUrls({});
    setAttempt((previous) => previous + 1);
  }, []);

  const loading = state.stage === "loading";

  const loadUrl = useCallback(async (assetId: string) => {
    setUrls((previous) => ({ ...previous, [assetId]: "loading" }));
    try {
      const view = await createDownloadUrl(assetId);
      setUrls((previous) => ({ ...previous, [assetId]: view }));
    } catch {
      setUrls((previous) => ({ ...previous, [assetId]: "expired" }));
    }
  }, []);

  // 结果一拿到就自己换短期地址：分享页的主体就是图，不该先让人点一下才看见
  useEffect(() => {
    if (state.stage !== "ready") return;
    for (const output of state.share.outputs) {
      if (urls[output.assetId] === undefined) void loadUrl(output.assetId);
    }
  }, [loadUrl, state, urls]);

  return (
    <main className="share-page">
      <div className="calm__depth" aria-hidden="true" />
      <header className="share-page__head">
        <Link className="brand" href="/">
          <span className="brand__mark" aria-hidden="true" />
          <span className="brand__name">AI Avatar Studio</span>
        </Link>
      </header>

      {state.stage === "loading" ? (
        <p className="hint">正在打开分享…</p>
      ) : null}

      {state.stage === "missing" ? (
        <section className="panel share-page__empty">
          <h1 className="section__title">这个分享链接已经失效</h1>
          <p className="section__sub">
            它可能已经被撤销、超过了有效期，或者链接不完整。
          </p>
          <p className="btnrow btnrow--top">
            <button
              type="button"
              className="btn"
              onClick={retry}
              disabled={loading}
            >
              {loading ? "正在重新加载…" : "重新加载"}
            </button>
            <Link className="btn btn--primary" href="/studio">
              自己做一个
            </Link>
          </p>
        </section>
      ) : null}

      {state.stage === "failed" ? (
        <section className="panel share-page__empty">
          <h1 className="section__title">
            {state.failure ? failureTitle(state.failure.code) : "暂时打不开"}
          </h1>
          <p className="section__sub">
            {state.failure?.message ?? "连接服务失败，稍后再试一次。"}
          </p>
          {state.failure?.status === 404 ? (
            // 服务端事实是"这个链接不成立"：重试没意义，只给出口
            <p className="btnrow btnrow--top">
              <Link className="btn btn--primary" href="/studio">
                自己做一个
              </Link>
            </p>
          ) : (
            <p className="btnrow btnrow--top">
              <button
                type="button"
                className="btn btn--primary"
                onClick={retry}
                disabled={loading}
              >
                {loading ? "正在重试…" : "重试一次"}
              </button>
            </p>
          )}
        </section>
      ) : null}

      {state.stage === "ready" ? (
        <section aria-labelledby="share-page-title" className="panel">
          <p className="eyebrow">分享的作品</p>
          <h1 className="section__title" id="share-page-title">
            {state.share.title?.trim() ? state.share.title : "两张头像"}
          </h1>
          <p className="section__sub">
            共 {state.share.outputs.length} 张结果
            {expiresLine(state.share.expiresAt)}
          </p>

          {state.share.outputs.length === 0 ? (
            <p className="hint">这个分享里没有可展示的结果。</p>
          ) : (
            <ul className="results results--pair">
              {state.share.outputs.map((output) => {
                const url = urls[output.assetId];
                const link = typeof url === "object" ? url : null;
                const ratio =
                  output.width && output.height
                    ? `${output.width} / ${output.height}`
                    : "1 / 1";
                const stale = link ? isExpired(link.expiresAt) : false;
                return (
                  <li className="result" key={output.assetId}>
                    <div className="media result__media" style={{ aspectRatio: ratio }}>
                      {link && !stale ? (
                        <img
                          src={link.url}
                          alt={`第 ${output.outputIndex + 1} 张生成结果`}
                          decoding="async"
                        />
                      ) : (
                        <div className="result__hold">
                          {url === "expired" || stale ? (
                            <>
                              <span>图片地址已过期</span>
                              <button
                                type="button"
                                className="btn btn--quiet"
                                onClick={() => void loadUrl(output.assetId)}
                              >
                                重新获取
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
                        第 {output.outputIndex + 1} 张 · {output.width ?? "?"} ×{" "}
                        {output.height ?? "?"} ·{" "}
                        {output.mimeType.replace("image/", "").toUpperCase()}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}

          <p className="btnrow btnrow--top">
            <Link className="btn btn--primary" href="/studio">
              也做一个
            </Link>
          </p>
        </section>
      ) : null}
    </main>
  );
}

function isExpired(iso: string): boolean {
  const at = new Date(iso).getTime();
  return Number.isFinite(at) && at <= Date.now();
}

function expiresLine(iso: string | null): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const days = Math.ceil((at.getTime() - Date.now()) / 86_400_000);
  if (days <= 0) return " · 链接已到期";
  return ` · ${days} 天后链接到期`;
}
