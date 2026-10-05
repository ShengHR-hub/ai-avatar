import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Composer } from "@/components/Composer";
import { HistoryList } from "@/components/AccountPanels";
import { Notice } from "@/components/Notice";
import { ProgressPanel } from "@/components/ProgressPanel";
import type { UploadState } from "@/hooks/useStudio";
import { ApiFailure } from "@/services/errors";
import type { Generation } from "@/generated/types.gen";
import { generation } from "./helpers/stub";

/**
 * 文案门禁：普通用户不应该在界面上看到 `READY`、`Provider`、`PROMPT`、接口缺失
 * 这类内部术语。这些断言锁住"自然语言界面"这个产品要求，避免后续改动退回去。
 */
const FORBIDDEN = ["READY", "Provider", "PROMPT", "Prompt", "prompt", "接口", "API", "状态机", "undefined", "NaN"];

function assertPlainLanguage(text: string) {
  for (const word of FORBIDDEN) {
    expect(text).not.toContain(word);
  }
}

const IDLE: UploadState = {
  stage: "idle",
  fileName: "",
  assetId: null,
  status: "",
  failure: null,
  previewUrl: null,
};

const READY: UploadState = {
  stage: "ready",
  fileName: "me.png",
  assetId: "a1",
  status: "READY",
  failure: null,
  previewUrl: null,
};

function composer(
  mode: "PHOTO_PROMPT" | "TEXT_PROMPT",
  upload: UploadState,
  prompt = "换成黏土质感",
  locked = false,
) {
  return render(
    <Composer
      mode={mode}
      upload={upload}
      busy={false}
      ready={upload.stage === "ready"}
      prompt={prompt}
      canSubmit
      working={false}
      locked={locked}
      onGate={vi.fn()}
      onPick={vi.fn()}
      onReset={vi.fn()}
      onPromptChange={vi.fn()}
      onSubmit={vi.fn()}
    />,
  );
}

describe("界面文案", () => {
  it("改造图片模式：上传状态说人话，不出现内部状态名", () => {
    const { container } = composer("PHOTO_PROMPT", READY);
    const text = container.textContent ?? "";
    expect(text).toContain("图片已就绪");
    expect(text).toContain("上传一张图片");
    assertPlainLanguage(text);
  });

  it("文字创作不出现任何上传步骤", () => {
    const { container } = composer("TEXT_PROMPT", IDLE);
    expect(container.querySelector('input[type="file"]')).toBeNull();
    expect(container.textContent ?? "").not.toContain("上传一张图片");
    assertPlainLanguage(container.textContent ?? "");
  });

  it("等待状态只用真实描述说明，不给伪造进度", () => {
    const { container } = render(<ProgressPanel resumed={false} />);
    const text = container.textContent ?? "";
    expect(text).toContain("正在生成");
    expect(text).toContain("不会因为刷新而重复提交");
    expect(text).not.toContain("%");
    assertPlainLanguage(text);
  });

  it("失败提示先给结论，错误码只作为次要信息", () => {
    const failure = new ApiFailure({ code: "RATE_LIMITED", message: "请求太频繁了。", status: 429 });
    const { container } = render(
      <Notice tone="warning" title="稍后再试" failure={failure}>
        请求太频繁了。
      </Notice>,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("稍后再试");
    expect(text).toContain("问题编号");
    expect(text).toContain("RATE_LIMITED");
  });

  it("人工复核与失败必须区分：预留与已退回不能写反", () => {
    const reviewItems = [
      generation({ id: "g-review", status: "NEEDS_REVIEW", billing_status: "RESERVED", prompt: "换成水彩" }),
    ] as unknown as Generation[];
    const review = render(<HistoryList items={reviewItems} />);
    const reviewText = review.container.textContent ?? "";
    expect(reviewText).toContain("等待人工确认");
    expect(reviewText).toContain("点数仍占用");
    expect(reviewText).not.toContain("已全额退回");

    const failedItems = [
      generation({
        id: "g-failed",
        status: "FAILED",
        billing_status: "RELEASED",
        failure_code: "VALIDATION",
        prompt: "换成油画",
      }),
    ] as unknown as Generation[];
    const failed = render(<HistoryList items={failedItems} />);
    const failedText = failed.container.textContent ?? "";
    expect(failedText).toContain("生成失败");
    expect(failedText).toContain("点数已全额退回");
  });

  it("访客拿不到文件选择框：先走认证引导，而不是发注定失败的请求", () => {
    const { container } = composer("PHOTO_PROMPT", IDLE, "", true);
    expect(container.querySelector('input[type="file"]')).toBeNull();
    expect(container.textContent ?? "").toContain("开通后就能上传图片");
  });

  it("没有记录时给出下一步，而不是空白页", () => {
    render(<HistoryList items={[]} />);
    expect(screen.getByText("还没有生成记录")).toBeInTheDocument();
  });
});
