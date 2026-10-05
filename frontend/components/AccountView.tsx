"use client";

import Link from "next/link";
import { useState } from "react";

import { useAccount } from "@/hooks/useAccount";
import { logoutSession } from "@/services/api";
import { failureTitle } from "@/services/errors";
import { BalanceSummary, HistoryList } from "./AccountPanels";
import { Notice } from "./Notice";

/** 余额与历史全部来自服务端；余额区刻意收小，让生成记录更早出现。 */
export function AccountView({ onSignedOut }: { onSignedOut: () => void | Promise<void> }) {
  const account = useAccount();
  const [signingOut, setSigningOut] = useState(false);

  async function signOut() {
    setSigningOut(true);
    try {
      await logoutSession();
    } finally {
      setSigningOut(false);
      await onSignedOut();
    }
  }

  return (
    <div className="studio calm">
      <div className="calm__depth" aria-hidden="true" />

      <header className="account__head">
        <div>
          <p className="eyebrow">我的</p>
          <h1 className="studio__title">点数与生成记录</h1>
        </div>
        <div className="studio__side">
          <Link className="btn" href="/studio">
            回到创作台
          </Link>
          <button type="button" className="btn btn--quiet" onClick={signOut} disabled={signingOut}>
            {signingOut ? "正在退出…" : "退出登录"}
          </button>
        </div>
      </header>

      {account.failure ? (
        <Notice tone="danger" title={failureTitle(account.failure.code)} failure={account.failure}>
          {account.failure.message}
        </Notice>
      ) : null}

      <section className="panel" aria-labelledby="balance-title">
        <h2 className="hint hint--flush" id="balance-title">
          点数
        </h2>
        <div className="btnrow--top">
          <BalanceSummary balance={account.balance} />
        </div>
      </section>

      <section className="section section--tight" aria-labelledby="history-title">
        <div className="section__head">
          <div>
            <p className="eyebrow">记录</p>
            <h2 className="section__title" id="history-title">
              我的生成
            </h2>
          </div>
          <p className="section__sub">从记录里点开任意一次，会回到创作台接着显示那一张任务的结果。</p>
        </div>

        {account.loading && account.items.length === 0 ? (
          <p className="hint">正在读取生成记录…</p>
        ) : (
          <HistoryList items={account.items} />
        )}

        {account.nextCursor ? (
          <div className="btnrow btnrow--top">
            <button type="button" className="btn" disabled={account.loadingMore} onClick={account.loadMore}>
              {account.loadingMore ? "正在加载…" : "继续加载更早的记录"}
            </button>
          </div>
        ) : null}
      </section>
    </div>
  );
}
