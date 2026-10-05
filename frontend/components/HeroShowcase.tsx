import Link from "next/link";

import { WORKS, WORKS_PENDING_NOTE, type WorkImage } from "@/services/catalog";

/**
 * 首屏主视觉：本站真实产出的成品图。
 *
 * 刻意不做的事：
 * - 不摆"结果位"空框——空框是在告诉用户"这里将来有东西"，而产品页该给的是东西本身；
 * - 不拿第三方摄影素材或外部平台的参考图冒充本站生成结果；
 * - 不把提示词文本压在这两张图上。看图是主角，读字是作品区卡片里的事，
 *   所以这里只留一个跳到 `#works` 的入口。
 *
 * 一条都没有时宁可写一句实话，也不摆两个框凑版式。
 */
export function HeroShowcase() {
  const work = WORKS[0] ?? null;

  if (!work) {
    return (
      <p className="showcase__empty" data-testid="hero-empty">
        {WORKS_PENDING_NOTE}
      </p>
    );
  }

  const caption = work.mode === "TEXT_PROMPT" ? "一句话生成的两张" : "一张图改造出的两张";

  return (
    <figure className="showcase">
      <div className="showcase__pair">
        {work.outputs.map((image: WorkImage, index: number) => (
          <div className="media media--1x1 showcase__result" key={image.src}>
            <img
              src={image.src}
              srcSet={image.srcSet}
              sizes="(min-width: 1020px) 300px, (min-width: 760px) 260px, 44vw"
              width={image.width}
              height={image.height}
              alt={image.alt}
              fetchPriority={index === 0 ? "high" : "auto"}
              decoding={index === 0 ? "auto" : "async"}
            />
          </div>
        ))}
      </div>

      <figcaption className="showcase__caps">
        <span className="tag tag--accent">本站生成</span>
        <span className="showcase__cap">{caption}</span>
        <Link className="showcase__more" href="#works">
          看当时写了什么 →
        </Link>
      </figcaption>
    </figure>
  );
}
