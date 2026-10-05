import { Suspense } from "react";

import { VerifyEmailView } from "@/components/VerifyEmailView";

export default function VerifyEmailPage() {
  return (
    <Suspense fallback={<p className="hint">正在准备验证表单…</p>}>
      <VerifyEmailView />
    </Suspense>
  );
}
