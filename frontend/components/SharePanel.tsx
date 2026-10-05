"use client";

import { useEffect, useState } from "react";

import type { OutputView } from "@/services/api";
import { ApiFailure, failureTitle } from "@/services/errors";
import type { ShareState } from "@/hooks/useStudio";
import { Notice } from "./Notice";

/**
 * 分享面板：只分享**被明确选中**的结果图。
 *
 * 契约与产品口径（`03-api-contract.md` 第 4.4 节、`04-ai-provider.md` 第 8 节）：
 *
 * - 一条链接可以分享一张或两张；未选中的结果不会被公开（仍 404）；
 * - 链接是随机 slug，界面只展示服务端回放的 slug 与到期时间，不拼接任何内部 ID；
 * - 同任务重复点击复用同一个幂等键 → 回放同一条链接；换了选中组合服务端会
 *   返回稳定冲突（409），界面据此提示「这是另一次分享」，不假装成功。
 */
export function SharePanel({
  outputs,
  share,
  busy,
  locked,
  onGate,
  onShare,
  onAnother,
}: {
  outputs: OutputView[];
  share: ShareState;
  busy: boolean;
  /** 未登录/未验证时只提示开通，不发注定失败的请求。 */
  locked: boolean;
  onGate: () => void;
  onShare: (assetIds: string[]) => void | Promise<void>;
  /**
   * 明确的「再生成一条分享链接」。
   *
   * 它递增分享意图序号 → 换一把新幂等键 → 服务端**真正新建**一条分享。
   * 没有这个入口，用户想换一组图就只能改勾选，而改勾选会被稳定冲突挡住
   * （同一次意图内改内容 = 冲突，不是新建）。
   */
  onAnother: () => void;
}) {
  /**
   * 勾选列表就是**最终要公开的集合**，默认全选。
   *
   * 不写成「默认空、取消勾选反而选中」：那样勾选框的视觉状态与真实提交内容会不一致，
   * 用户以为取消了一张，实际只提交了那一张。
   */
  const [selected, setSelected] = useState<string[]>(() =>
    outputs.map((output) => output.assetId),
  );
  const [origin, setOrigin] = useState("");

  const effective = selected.filter((assetId) =>
    outputs.some((output) => output.assetId === assetId),
  );
  const creating = share.stage === "creating";

  const toggle = (assetId: string) => {
    setSelected((previous) =>
      previous.includes(assetId)
        ? previous.filter((value) => value !== assetId)
        : [...previous, assetId],
    );
  };

  // 分享链接的绝对地址在客户端拼（服务端只回 slug，不回主机名）。
  // 放在 effect 里：渲染期读 `window` 既会破坏水合一致性，也会在 SSR 下抛错。
  useEffect(() => {
    setOrigin(window.location.origin);
  }, []);

  const link = share.view ? `${origin}/s/${share.view.slug}` : "";

  if (outputs.length === 0) return null;

  return (
    <section aria-labelledby="share-title" className="panel">
      <div className="section__head">
        <div>
          <p className="eyebrow">分享</p>
          <h2 className="section__title" id="share-title">
            把成果发给别人看
          </h2>
          <p className="section__sub">
            只公开你选中的图；没选中的那张仍然是私密的。链接有有效期，到期后自动失效。
          </p>
        </div>
      </div>

      <ul className="share-pick">
        {outputs.map((output) => {
          const checked = effective.includes(output.assetId);
          return (
            <li key={output.assetId}>
              <label className="share-pick__item">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(output.assetId)}
                  disabled={locked || creating}
                />
                <span>
                  第 {output.outputIndex + 1} 张 · {output.width ?? "?"} ×{" "}
                  {output.height ?? "?"}
                </span>
              </label>
            </li>
          );
        })}
      </ul>

      <div className="btnrow btnrow--top">
        <button
          type="button"
          className="btn btn--primary"
          disabled={busy || creating || effective.length === 0}
          onClick={() => (locked ? onGate() : void onShare(effective))}
        >
          {creating ? "正在生成链接…" : "生成分享链接"}
        </button>
        {share.stage === "failed" || share.stage === "created" ? (
          <button
            type="button"
            className="btn"
            disabled={busy || creating}
            onClick={onAnother}
          >
            再生成一条分享链接
          </button>
        ) : null}
        <span className="hint">
          将公开 {effective.length} 张
        </span>
      </div>

      {share.stage === "failed" && share.failure ? (
        <Notice tone="danger" title={shareTitle(share.failure)} failure={share.failure}>
          {shareMessage(share.failure)}
        </Notice>
      ) : null}

      {share.stage === "created" && share.view ? (
        <div className="share-result">
          <p className="share-result__label">链接已生成</p>
          <p className="share-result__link">
            <a href={link} target="_blank" rel="noopener noreferrer">
              {link}
            </a>
          </p>
          <p className="hint">
            包含 {share.view.outputs.length} 张结果
            {share.view.expiresAt
              ? ` · ${expiresText(share.view.expiresAt)}`
              : ""}
          </p>
          <p className="hint">
            在别的浏览器或未登录状态下打开这个链接都能看到结果；撤销或到期后即失效。
            想换一组图分享，用上面的「再生成一条分享链接」。
          </p>
        </div>
      ) : null}
    </section>
  );
}

function shareTitle(failure: ApiFailure): string {
  if (failure.code === "IDEMPOTENCY_CONFLICT") return "这次选中的图和上一次不同";
  return failureTitle(failure.code);
}

function shareMessage(failure: ApiFailure): string {
  if (failure.code === "IDEMPOTENCY_CONFLICT") {
    return "同一次分享意图只能产生一条链接，而这次勾选的图不一样。想换一组图，请点「再生成一条分享链接」——那才算一次新的分享意图。";
  }
  if (failure.code === "GENERATION_NOT_SHAREABLE") {
    return "只有生成成功、且结果已就绪的作品可以分享。";
  }
  return failure.message;
}

function expiresText(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "有效期以服务端为准";
  const days = Math.max(0, Math.ceil((at.getTime() - Date.now()) / 86_400_000));
  return `${days} 天后到期`;
}
