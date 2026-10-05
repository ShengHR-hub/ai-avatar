import { Suspense } from "react";

import { Studio } from "@/components/Studio";

/** `/studio` 读 query 预填模式与提示词，因此必须有 Suspense 边界。 */
export default function StudioPage() {
  return (
    <Suspense fallback={<p className="hint">正在准备创作台…</p>}>
      <Studio />
    </Suspense>
  );
}
