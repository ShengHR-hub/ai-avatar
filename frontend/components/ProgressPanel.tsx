"use client";

import { useEffect, useState } from "react";

import { ApertureSpinner } from "./BrandMark";

/**
 * 等待中：说明任务仍在进行、通常需要多久、可以离开并回来，同时用两个骨架相框
 * 表达"正在做两张"。
 *
 * 契约里没有真实进度，所以这里不显示任何百分比或预计剩余时间——只有真实的
 * 状态描述。`slow` 只来自本页已等待的时长，用来把语气放软，不当成进度。
 */
export function ProgressPanel({ resumed }: { resumed: boolean }) {
  const [slow, setSlow] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setSlow(true), 90_000);
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <section className="panel" aria-live="polite" aria-label="生成状态">
      <div className="progress">
        <div className="progress__head">
          <ApertureSpinner />
          <div>
            <p className="progress__t">{slow ? "还在生成，比平时久了一点" : "正在生成，两张一起做"}</p>
            <p className="progress__d">
              {resumed
                ? "已经接上你之前提交的那张任务，继续等结果就行。"
                : "通常需要 30–90 秒。这里不用一直盯着。"}
            </p>
          </div>
        </div>

        <ul className="progress__facts">
          <li>任务仍在进行</li>
          <li>可以离开这一页，回来会接着显示同一张任务</li>
          <li>不会因为刷新而重复提交</li>
        </ul>

        <div className="frames" aria-hidden="true">
          <span className="frame" />
          <span className="frame" />
        </div>
      </div>
    </section>
  );
}
