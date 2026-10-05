"use client";

import Link from "next/link";

import { useSession } from "@/hooks/useSession";
import { AccountView } from "@/components/AccountView";
import { EmptyMark } from "@/components/BrandMark";
import { Notice } from "@/components/Notice";

/** 「我的」页：余额与历史全部来自服务端；未登录时回到入口流程。 */
export default function MePage() {
  const session = useSession();

  if (session.phase === "loading") {
    return <p className="hint">正在确认登录状态…</p>;
  }

  if (session.phase === "error") {
    return (
      <Notice tone="danger" title="暂时连不上服务">
        连接恢复后再回来看点数与记录。
      </Notice>
    );
  }

  if (session.phase === "anonymous") {
    return (
      <div className="panel calm">
        <div className="empty">
          <EmptyMark />
          <p className="empty__t">还没有开通账号</p>
          <p className="empty__d">用邀请码开通并完成邮箱验证后，这里会显示你的点数余额和每一次生成的结果。</p>
          <Link className="btn btn--primary" href="/studio">
            去创作台开通
          </Link>
        </div>
      </div>
    );
  }

  return (
    <>
      {session.phase === "needs-verification" ? (
        <Notice tone="warning" title="邮箱还没验证">
          确认邮箱后上传与生成才会解锁；如果下面的点数没有显示出来，多半就是这个原因。
        </Notice>
      ) : null}
      <AccountView onSignedOut={session.refresh} />
    </>
  );
}
