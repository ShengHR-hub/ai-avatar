"use client";

import { useSearchParams } from "next/navigation";

import { useSession } from "@/hooks/useSession";
import { VerifyEmailPanel } from "./VerifyEmailPanel";

/** 邮件链接形如 `/verify-email?token=...`：令牌只在这条路上出现一次，不落地存储。 */
export function VerifyEmailView() {
  const params = useSearchParams();
  const session = useSession();
  return <VerifyEmailPanel token={params.get("token") ?? ""} onVerified={session.refresh} />;
}
