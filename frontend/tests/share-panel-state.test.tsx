/**
 * ENG-09 返修第 6 项：分享选图的**局部**状态随任务复位。
 *
 * 缺陷：`SharePanel` 把「勾选了哪几张」放在自己的 `useState` 里。任务切换时
 * 面板原本不会重新挂载，于是上一个任务里取消勾选的第二张会带进新任务 ——
 * 用户看到的选中集合与真正提交的内容不一致（这个不一致正是 ENG-09 第一轮
 * 已经踩过一次的坑：勾选框的视觉状态必须等于真实提交内容）。
 *
 * 修复方式是在 `Studio` 上用 `generationId` 作 `key`。因此这组用例锁定的是
 * **key 变化 ⇒ 局部状态复位** 这条契约（`rerender` 保留组件身份，正是为了
 * 复现"没有 key 时状态被带过去"的情形）。
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SharePanel } from "@/components/SharePanel";
import type { ShareState } from "@/hooks/useStudio";
import type { OutputView } from "@/services/api";
import { installFetchStub, type Stub } from "./helpers/stub";

const OUTPUTS: OutputView[] = [
  {
    assetId: "r1",
    outputIndex: 0,
    mimeType: "image/png",
    width: 1024,
    height: 1024,
    sizeBytes: 2048,
  },
  {
    assetId: "r2",
    outputIndex: 1,
    mimeType: "image/png",
    width: 1024,
    height: 1024,
    sizeBytes: 4096,
  },
];

const IDLE: ShareState = {
  stage: "idle",
  generationId: "g-1",
  view: null,
  failure: null,
};

const noop = () => {};

let stub: Stub = installFetchStub();
afterEach(() => stub.restore());

function renderPanel(share: ShareState, key: string) {
  return render(
    <SharePanel
      key={key}
      outputs={OUTPUTS}
      share={share}
      busy={false}
      locked={false}
      onGate={vi.fn()}
      onShare={vi.fn()}
      onAnother={vi.fn()}
    />,
  );
}

describe("分享选图的局部状态", () => {
  it("换任务（key 变化）后勾选集合回到默认全选", () => {
    const { rerender } = renderPanel(IDLE, "share-g-1");
    const boxes = () => screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(boxes().every((box) => box.checked)).toBe(true);

    // 用户在任务 A 里取消了第二张
    fireEvent.click(boxes()[1]);
    expect(boxes()[1].checked).toBe(false);

    rerender(
      <SharePanel
        key="share-g-2"
        outputs={OUTPUTS}
        share={{ ...IDLE, generationId: "g-2" }}
        busy={false}
        locked={false}
        onGate={noop}
        onShare={noop}
        onAnother={noop}
      />,
    );

    // 新任务下必须回到默认全选：上一轮的局部选择不得带过来
    expect(boxes().every((box) => box.checked)).toBe(true);
  });

  it("同一任务内重新渲染仍保留用户的选择（复位只在换任务时发生）", () => {
    const { rerender } = renderPanel(IDLE, "share-g-1");
    fireEvent.click(screen.getAllByRole("checkbox")[1]);

    rerender(
      <SharePanel
        key="share-g-1"
        outputs={OUTPUTS}
        share={{ ...IDLE, stage: "creating" }}
        busy={false}
        locked={false}
        onGate={noop}
        onShare={noop}
        onAnother={noop}
      />,
    );

    expect((screen.getAllByRole("checkbox")[1] as HTMLInputElement).checked).toBe(false);
  });

  it("只有被勾选的那张会进入提交载荷", () => {
    const onShare = vi.fn();
    render(
      <SharePanel
        key="share-g-1"
        outputs={OUTPUTS}
        share={IDLE}
        busy={false}
        locked={false}
        onGate={vi.fn()}
        onShare={onShare}
        onAnother={vi.fn()}
      />,
    );

    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "生成分享链接" }));

    expect(onShare).toHaveBeenCalledWith(["r1"]);
  });
});
