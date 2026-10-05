import Link from "next/link";

import { HeroShowcase } from "@/components/HeroShowcase";
import { WorkCard } from "@/components/WorkCard";
import {
  HERO_FACTS,
  HOME_DIRECTIONS,
  HOME_IDEAS_MORE,
  IDEA_NOTE,
  POINT_DISCLOSURE,
  STEPS,
  WORKS,
  WORKS_PENDING_NOTE,
  WORKS_SECTION_SUB,
  studioLink,
} from "@/services/catalog";
import { composePrompt } from "@/services/promptAssist";

/**
 * 首页即产品页，按「先看效果 → 再挑方向 → 最后才是说明」排：
 *
 * 1. 首屏：产品名 + 本站真实产出的成品图 + 创作入口；
 * 2. 作品：每条都能看描述、复制、带进创作台改；
 * 3. 灵感方向：**只放几个有代表性的方向**，完整的多维标签组合在创作台里；
 * 4. 怎么用：压成一句一行，排在视觉内容之后。
 *
 * 图片是主角：描述文字一律收在卡片下方或可展开面板里，不叠在画面上。
 * 上一版这里放的是"适合上传的示例照片"和两个空结果位——那不是在展示产品能做什么，
 * 而是在描述一个还没上线的功能，所以整块拿掉了。
 */
export default function HomePage() {
  return (
    <>
      <section className="hero">
        {/* 首页更有表现力的一层柔光；创作台与账户页不出现 */}
        <div className="hero__light" aria-hidden="true" />

        <div className="hero__copy reveal">
          <p className="eyebrow">AI 头像创作</p>
          <h1 className="display hero__title">
            把你想要的样子，
            <br />
            做成<em>一张头像</em>
          </h1>
          <p className="lede">
            上传一张图片按描述改造，或者直接用一句话从零画一张。人物、宠物、虚构角色、风景、背影、抽象画面都能做。
          </p>
        </div>

        <div className="hero__media reveal reveal--2">
          <HeroShowcase />
        </div>

        <div className="hero__entries reveal reveal--3">
          <div className="entrypair">
            <Link className="entry glass" href="/studio?mode=TEXT_PROMPT">
              <div>
                <p className="entry__index">入口 01 · 不用图片</p>
                <h2 className="entry__title">文字创作</h2>
                <p className="entry__desc">一句话描述，从零画一张。</p>
              </div>
              <span className="entry__go" aria-hidden="true">
                →
              </span>
            </Link>

            <Link className="entry glass" href="/studio?mode=PHOTO_PROMPT">
              <div>
                <p className="entry__index">入口 02 · 有图片</p>
                <h2 className="entry__title">改造我的图片</h2>
                <p className="entry__desc">上传一张图片，按描述改。</p>
              </div>
              <span className="entry__go" aria-hidden="true">
                →
              </span>
            </Link>
          </div>

          {/* 第三个入口是「先找感觉」的前置动作：放一张喜欢的图，换回一段能改的描述 */}
          <Link className="entry entry--alt glass" href={studioLink("TEXT_PROMPT", "", "inspire")}>
            <div>
              <p className="entry__index">入口 03 · 还没想好怎么写</p>
              <h2 className="entry__title">参考图片找灵感</h2>
              <p className="entry__desc">放一张喜欢的图，让它写段描述给你改。</p>
            </div>
            <span className="entry__go" aria-hidden="true">
              →
            </span>
          </Link>

          <ul className="hero__facts">
            {HERO_FACTS.map((fact) => (
              <li key={fact}>{fact}</li>
            ))}
          </ul>
        </div>
      </section>

      <p className="cue">
        <span>继续往下看</span>
        <span className="cue__line" />
      </p>

      <section className="section section--lead" id="works" aria-labelledby="works-title">
        <div className="section__head">
          <div>
            <p className="eyebrow">作品</p>
            <h2 className="section__title" id="works-title">
              本站做出来的头像
            </h2>
          </div>
          <p className="section__sub">{WORKS_SECTION_SUB}</p>
        </div>

        {WORKS.length > 0 ? (
          <div className="works">
            {WORKS.map((work, index) => (
              <WorkCard key={work.id} work={work} label={`作品 ${index + 1}`} />
            ))}
          </div>
        ) : (
          <p className="note">{WORKS_PENDING_NOTE}</p>
        )}
      </section>

      <section className="section" id="ideas" aria-labelledby="ideas-title">
        <div className="section__head">
          <div>
            <p className="eyebrow">灵感方向</p>
            <h2 className="section__title" id="ideas-title">
              先挑一个方向，进去再慢慢改
            </h2>
          </div>
          <p className="section__sub">{IDEA_NOTE}</p>
        </div>

        {/* 首页只摆有代表性的几条：把六七个维度全铺完是创作台的事，不是首页的活儿 */}
        <div className="directions">
          {HOME_DIRECTIONS.map((direction) => (
            <Link
              className="direction glass"
              key={direction.id}
              href={studioLink(
                "TEXT_PROMPT",
                composePrompt("TEXT_PROMPT", "", direction.selection, direction.extra),
              )}
            >
              <h3 className="direction__t">{direction.label}</h3>
              <p className="direction__d">{direction.hint}</p>
              <p className="direction__p">
                {composePrompt("TEXT_PROMPT", "", direction.selection, direction.extra)}
              </p>
            </Link>
          ))}
        </div>

        <div className="btnrow btnrow--top ideas__more">
          <Link className="btn btn--primary" href="/studio?mode=TEXT_PROMPT">
            去创作台自己搭词
          </Link>
          <span className="hint ideas__more-hint">{HOME_IDEAS_MORE}</span>
        </div>
      </section>

      <section className="section section--how" id="how" aria-labelledby="how-title">
        <div className="section__head">
          <div>
            <p className="eyebrow">怎么用</p>
            <h2 className="section__title" id="how-title">
              三步拿到头像
            </h2>
          </div>
          <p className="section__sub">账户、点数和任务状态由系统承接，你只需要决定想要什么。</p>
        </div>

        <div className="steps">
          {STEPS.map((step, index) => (
            <div className="step" key={step.title}>
              <span className="step__n">{index + 1}</span>
              <div>
                <h3 className="step__t">{step.title}</h3>
                <p className="step__d">{step.body}</p>
              </div>
            </div>
          ))}
        </div>

        <p className="note">
          <b>点数怎么走：</b>
          {POINT_DISCLOSURE}
        </p>
      </section>

      {/* 手机端把入口钉在拇指可及的位置；第三个入口名字缩短，三颗按钮都还够点 */}
      <nav className="dock bar" aria-label="快捷入口">
        <Link className="btn btn--primary" href="/studio?mode=TEXT_PROMPT">
          文字创作
        </Link>
        <Link className="btn" href="/studio?mode=PHOTO_PROMPT">
          改造图片
        </Link>
        <Link className="btn" href={studioLink("TEXT_PROMPT", "", "inspire")}>
          找灵感
        </Link>
      </nav>
    </>
  );
}
