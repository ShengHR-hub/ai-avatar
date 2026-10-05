"use client";

import { useState } from "react";

import type { FeedbackState } from "@/hooks/useStudio";
import { ApiFailure, failureTitle } from "@/services/errors";
import { Notice } from "./Notice";

/**
 * 反馈面板：对**已完成**的任务提交一次评分/分类/文字反馈。
 *
 * 契约与产品口径（`03-api-contract.md` 第 4.4 节）：
 *
 * - 只有 `SUCCEEDED`/`FAILED` 可反馈；`NEEDS_REVIEW` 结果未定，界面直接不给入口；
 * - 分类必填且 ≤64 字符，正文可选且 ≤2000 字符（与冻结基线一致）；
 * - 每用户每任务只有一条：提交成功后界面转为只读展示；
 * - 重复提交（同键同内容）回放第一次结果，换了内容服务端返回稳定冲突，
 *   界面如实说明「每个作品只有一次评价」，不假装又记下一条。
 */

/** 分类选项：与「作品/过程」两类问题覆盖面一致，且都远短于 64 字符上限。 */
export const FEEDBACK_CATEGORIES = [
  "画面质量",
  "描述还原度",
  "风格偏好",
  "等待时长",
  "其他问题",
] as const;

const CONTENT_MAX = 2000;
export const CATEGORY_MAX = 64;

export function FeedbackPanel({
  feedback,
  busy,
  locked,
  available,
  onGate,
  onSubmit,
}: {
  feedback: FeedbackState;
  busy: boolean;
  locked: boolean;
  /** 任务是否处于可反馈终态；非终态直接不渲染入口。 */
  available: boolean;
  onGate: () => void;
  onSubmit: (input: {
    rating: number;
    category: string;
    content: string;
  }) => void | Promise<void>;
}) {
  const [rating, setRating] = useState(5);
  const [category, setCategory] = useState<string>(FEEDBACK_CATEGORIES[0]);
  const [content, setContent] = useState("");

  if (!available) return null;

  const submitting = feedback.stage === "submitting";
  const tooLong = content.length > CONTENT_MAX;
  const canSubmit = !busy && !submitting && !tooLong && category.length > 0;

  return (
    <section aria-labelledby="feedback-title" className="panel">
      <div className="section__head">
        <div>
          <p className="eyebrow">反馈</p>
          <h2 className="section__title" id="feedback-title">
            这次生成怎么样？
          </h2>
          <p className="section__sub">
            每个作品只能评价一次，用来改进后续结果；反馈不影响点数结算。
          </p>
        </div>
      </div>

      {feedback.stage === "submitted" && feedback.view ? (
        <div className="feedback-done">
          <p className="feedback-done__line">
            已记录：{feedback.view.rating} 分 · {feedback.view.category}
          </p>
          {feedback.view.content ? (
            <p className="hint">{feedback.view.content}</p>
          ) : null}
          <p className="hint">想改内容可以联系客服，界面不再重复提交。</p>
        </div>
      ) : (
        <>
          <fieldset className="feedback-field">
            <legend>评分</legend>
            <div className="feedback-rating" role="group" aria-label="评分">
              {[1, 2, 3, 4, 5].map((value) => (
                <button
                  key={value}
                  type="button"
                  className={value === rating ? "rating rating--on" : "rating"}
                  aria-pressed={value === rating}
                  disabled={locked || submitting}
                  onClick={() => setRating(value)}
                >
                  {value}
                </button>
              ))}
            </div>
          </fieldset>

          <label className="feedback-field">
            <span>最贴近的问题</span>
            <select
              value={category}
              disabled={locked || submitting}
              onChange={(event) => setCategory(event.target.value)}
            >
              {FEEDBACK_CATEGORIES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>

          <label className="feedback-field">
            <span>补充说明（可选）</span>
            <textarea
              value={content}
              rows={4}
              maxLength={CONTENT_MAX}
              disabled={locked || submitting}
              placeholder="哪里好、哪里不对，写清楚更有用。"
              onChange={(event) => setContent(event.target.value)}
            />
            <span className="hint">
              {content.length} / {CONTENT_MAX}
            </span>
          </label>

          <div className="btnrow btnrow--top">
            <button
              type="button"
              className="btn btn--primary"
              disabled={!canSubmit}
              onClick={() =>
                locked
                  ? onGate()
                  : void onSubmit({ rating, category, content })
              }
            >
              {submitting ? "正在提交…" : "提交反馈"}
            </button>
          </div>

          {feedback.stage === "failed" && feedback.failure ? (
            <Notice
              tone="danger"
              title={feedbackTitle(feedback.failure)}
              failure={feedback.failure}
            >
              {feedbackMessage(feedback.failure)}
            </Notice>
          ) : null}
        </>
      )}
    </section>
  );
}

function feedbackTitle(failure: ApiFailure): string {
  if (failure.code === "IDEMPOTENCY_CONFLICT") return "这个作品已经评价过了";
  return failureTitle(failure.code);
}

function feedbackMessage(failure: ApiFailure): string {
  if (failure.code === "IDEMPOTENCY_CONFLICT") {
    return "每个作品只能有一条反馈。已经提交过的那条会保留，这次不会覆盖它。";
  }
  if (failure.code === "VALIDATION_ERROR") {
    return "这次提交没通过校验，请检查评分、分类或补充说明的长度。";
  }
  if (failure.code === "GENERATION_NOT_FOUND") {
    return "找不到这个作品，或者它不属于当前账号。";
  }
  return failure.message;
}
