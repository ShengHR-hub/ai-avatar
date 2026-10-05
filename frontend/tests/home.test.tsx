import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import HomePage from "@/app/page";
import { HOME_DIRECTIONS, WORKS } from "@/services/catalog";

describe("首页结构", () => {
  it("主视觉是本站成品图，不再摆示例照片和空结果位", () => {
    const { container } = render(<HomePage />);

    expect(container.textContent).not.toContain("示例素材");
    expect(container.textContent).not.toContain("示例照片");
    expect(container.querySelectorAll(".slot")).toHaveLength(0);
    expect(container.querySelector("#samples")).toBeNull();

    // 首屏的作品图就是真实成品，不是占位框
    const hero = container.querySelector(".hero__media");
    expect(hero?.querySelectorAll(".showcase__pair img")).toHaveLength(WORKS.length > 0 ? 2 : 0);
    for (const image of Array.from(hero?.querySelectorAll("img") ?? [])) {
      expect(image.getAttribute("src")).toContain("/art/works/");
    }
  });

  it("灵感区只摆有代表性的方向，并把完整标签留在创作台", () => {
    const { container } = render(<HomePage />);

    const cards = Array.from(container.querySelectorAll("a.direction"));
    expect(cards).toHaveLength(HOME_DIRECTIONS.length);
    expect(HOME_DIRECTIONS.length).toBeLessThanOrEqual(8);
    for (const card of cards) {
      expect(card.getAttribute("href")).toContain("/studio?mode=TEXT_PROMPT");
      expect(card.getAttribute("href")).toContain("prompt=");
    }

    expect(screen.getByRole("link", { name: "去创作台自己搭词" })).toBeInTheDocument();
    expect(screen.getByText(/创作台里有完整的灵感标签/)).toBeInTheDocument();

    // 首页不该出现维度名铺满的标签墙（那是创作台的 IDEA 面板）
    expect(container.querySelector(".ideamap")).toBeNull();
    expect(container.querySelector(".chiprow")).toBeNull();
  });

  it("三个入口在首页都能走，第三个明确指向找灵感", () => {
    const { container } = render(<HomePage />);
    const entries = Array.from(container.querySelectorAll("a.entry"));

    expect(entries).toHaveLength(3);
    expect(entries[0].getAttribute("href")).toContain("mode=TEXT_PROMPT");
    expect(entries[1].getAttribute("href")).toContain("mode=PHOTO_PROMPT");
    expect(entries[2].getAttribute("href")).toContain("way=inspire");
    expect(document.body.textContent).toContain("参考图片找灵感");
  });

  it("手机端底栏三个入口都留得住，不只缩桌面布局", () => {
    const { container } = render(<HomePage />);
    const buttons = container.querySelectorAll(".dock .btn");

    expect(buttons).toHaveLength(3);
    expect(buttons[2].getAttribute("href")).toContain("way=inspire");
  });

  it("每张作品都在作品区里出现，来源与授权跟着一起走", () => {
    render(<HomePage />);

    expect(screen.getByRole("heading", { name: "本站做出来的头像" })).toBeInTheDocument();
    for (const work of WORKS) {
      expect(screen.getByRole("button", { name: "展开详情与描述" })).toBeInTheDocument();
      expect(document.body.textContent).toContain(work.origin.model);
    }
  });
});
