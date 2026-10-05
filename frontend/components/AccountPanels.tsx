"use client";

import Link from "next/link";

import type { Generation, PointBalance } from "@/generated/types.gen";
import { toneFor, type GenerationTone } from "@/services/generation";
import { EmptyMark } from "./BrandMark";

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** 余额收成一行：数字来自服务端，前端不做任何加减。 */
export function BalanceSummary({ balance }: { balance: PointBalance | null }) {
  if (!balance) {
    return <p className="hint hint--flush">正在读取点数余额…</p>;
  }
  return (
    <div className="balance">
      <span className="balance__v mono-num">{balance.available_points}</span>
      <span className="balance__k">可用点数</span>
      <span className="balance__stat">
        占用中 <b>{balance.reserved_points}</b>
      </span>
      <span className="balance__stat">
        累计获得 <b>{balance.lifetime_credited}</b>
      </span>
      <span className="balance__stat">
        累计消耗 <b>{balance.lifetime_consumed}</b>
      </span>
      <span className="balance__when">更新于 {formatTime(balance.updated_at)}</span>
    </div>
  );
}

/** 状态一律翻译成人话；精确的机器码放在可展开的技术信息里。 */
const TONE_LABEL: Record<GenerationTone, string> = {
  working: "生成中",
  success: "已完成",
  failed: "生成失败",
  review: "等待人工确认",
  stalled: "仍在处理",
};

const TONE_TAG: Record<GenerationTone, string> = {
  working: "tag",
  success: "tag tag--ok",
  failed: "tag tag--danger",
  review: "tag tag--warn",
  stalled: "tag tag--warn",
};

const BILLING_TEXT: Record<string, string> = {
  RESERVED: "点数仍占用，既未扣除也未退回",
  CONSUMED: "点数已扣除",
  RELEASED: "点数已全额退回",
};

export function HistoryList({ items }: { items: Generation[] }) {
  if (items.length === 0) {
    return (
      <div className="empty">
        <EmptyMark />
        <p className="empty__t">还没有生成记录</p>
        <p className="empty__d">去创作台提交一次生成，这里就会列出每一次的结果、状态和点数去向。</p>
        <Link className="btn btn--primary" href="/studio">
          去创作台
        </Link>
      </div>
    );
  }

  return (
    <ol className="history">
      {items.map((item) => {
        const tone = toneFor(item.status, item.billing_status);
        const billing = BILLING_TEXT[item.billing_status] ?? null;
        return (
          <li key={item.id} className="history__item">
            <div>
              <p className="history__prompt">{item.prompt || "（这次没有写描述）"}</p>
              <p className="history__meta">
                <span className={TONE_TAG[tone]}>{TONE_LABEL[tone]}</span>
                <span>{item.mode === "PHOTO_PROMPT" ? "改造图片" : "文字创作"}</span>
                <span>{formatTime(item.created_at)}</span>
                <span>
                  {item.reserved_points} 点 · {billing ?? item.billing_status}
                </span>
              </p>
              <details className="disclose">
                <summary>技术信息</summary>
                <p>
                  任务 <code>{item.id}</code> · 状态 <code>{item.status}</code> · 计费{" "}
                  <code>{item.billing_status}</code>
                  {item.failure_code ? (
                    <>
                      {" "}
                      · 原因 <code>{item.failure_code}</code>
                    </>
                  ) : null}
                </p>
              </details>
            </div>
            <div className="history__acts">
              <Link className="history__go" href={`/studio?generation=${item.id}`}>
                查看这一次 →
              </Link>
              {item.prompt.trim().length > 0 ? (
                <Link
                  className="history__again"
                  href={`/studio?mode=${item.mode}&prompt=${encodeURIComponent(item.prompt)}`}
                >
                  沿用这段描述再创作
                </Link>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
