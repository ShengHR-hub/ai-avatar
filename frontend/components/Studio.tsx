"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Composer } from "./Composer";
import { FeedbackPanel } from "./FeedbackPanel";
import { GenerationStatus } from "./GenerationStatus";
import { AuthGate } from "./AuthGate";
import { Notice } from "./Notice";
import { ReferencePanel } from "./ReferencePanel";
import { SharePanel } from "./SharePanel";
import { usePoints } from "@/hooks/usePoints";
import { useSession } from "@/hooks/useSession";
import { useStudio, type StudioWay } from "@/hooks/useStudio";
import { OPEN_MODES, type OpenMode } from "@/services/config";
import { ApiFailure, failureTitle } from "@/services/errors";
import { isTerminal } from "@/services/generation";

/** 只接受契约里已开放的模式；`REFERENCE_IMAGE` 与其它未知值一律回落默认。 */
function safeMode(raw: string | null): OpenMode {
  const match = OPEN_MODES.find((mode) => mode.value === raw);
  return match ? match.value : "PHOTO_PROMPT";
}

/**
 * 入口参数：`way=inspire` 深链接到第三个入口（参考图片找灵感）。
 * 它不是第四种生成方式——落到状态里时 `mode` 仍然是 `TEXT_PROMPT`，
 * 提交走的还是那条已经验收过的文字创作链路。
 */
function safeWay(raw: string | null, mode: OpenMode): StudioWay {
  if (raw === "inspire") return "inspire";
  return mode === "TEXT_PROMPT" ? "text" : "photo";
}

/** 每个入口自己的标题与一句说明——切入口时用户应该立刻知道现在在走哪条路。 */
const HEADINGS: Record<StudioWay, { title: string; lede: string }> = {
  text: {
    title: "用文字做头像",
    lede: "不上传图片，用一句话描述你想要的头像；人物、虚构角色、宠物、风景、背影、抽象画面都行。",
  },
  photo: {
    title: "改造我的图片",
    lede: "上传一张图片，按描述把它改成你想要的样子——人物、宠物、玩偶、风景、抽象画面都可以。",
  },
  inspire: {
    title: "从一张参考图找灵感",
    lede: "放一张你喜欢的图，让它看图说话，写出一段可以改的画面描述；你改完再按文字创作出图。",
  },
};

/**
 * 创作台。访客同样能选模式、填描述、看说明；只有真正要上传或提交时才按现有认证
 * 契约引导开通——服务端仍是唯一裁判，这里不复制它的规则，也不发注定 401 的请求。
 */
export function Studio() {
  const params = useSearchParams();
  const session = useSession();
  const preset = useMemo(
    () => ({
      mode: safeMode(params.get("mode")),
      way: safeWay(params.get("way"), safeMode(params.get("mode"))),
      prompt: params.get("prompt") ?? "",
      generation: params.get("generation"),
    }),
    [params],
  );
  const [gateOpen, setGateOpen] = useState(false);

  const studio = useStudio({
    initialMode: preset.mode,
    initialWay: preset.way,
    initialPrompt: preset.prompt,
    initialGenerationId: preset.generation,
  });

  const signedIn = session.phase === "signed-in";
  const points = usePoints(signedIn);

  // 终局后重读点数：扣除或退回的结果以服务端为准，前端不做任何加减
  const status = studio.watch?.status;
  const reload = points.reload;
  useEffect(() => {
    if (status && isTerminal(status)) void reload();
  }, [status, reload]);

  const failure: ApiFailure | null = studio.failure ?? (session.phase === "error" ? session.failure : null);
  const photoMode = studio.mode === "PHOTO_PROMPT";

  // 终局判定只看服务端状态：分享要 SUCCEEDED，反馈要 SUCCEEDED/FAILED
  const terminalStatus = studio.watch?.status ?? "";
  const succeeded = terminalStatus === "SUCCEEDED";
  const feedbackable = succeeded || terminalStatus === "FAILED";

  /** 门禁：会话不是「已登录且已验证」时，只打开引导，不把注定失败的请求发出去。 */
  const guard = useCallback(
    <Args extends unknown[]>(action: (...args: Args) => void | Promise<void>) =>
      async (...args: Args) => {
        if (!signedIn) {
          setGateOpen(true);
          return;
        }
        await action(...args);
      },
    [signedIn],
  );

  return (
    <div className="studio calm">
      {/* 只给这一页的"背后明暗"：让下面的玻璃面板有东西可以透 */}
      <div className="calm__depth" aria-hidden="true" />

      <header className="studio__head">
        <div>
          <p className="eyebrow">创作台</p>
          <h1 className="studio__title">{HEADINGS[studio.way].title}</h1>
          <p className="studio__lede">
            {HEADINGS[studio.way].lede}
            每次固定产出两张。
          </p>
        </div>

        <div className="studio__side">
          {signedIn ? (
            <div className="points">
              <span className="points__v mono-num">
                {points.balance ? points.balance.available_points : "—"}
              </span>
              <span className="points__k">可用点数</span>
              {points.balance ? (
                <>
                  <span className="points__sep" aria-hidden="true" />
                  <span className="points__k">占用中 {points.balance.reserved_points}</span>
                </>
              ) : null}
              <span className="points__sep" aria-hidden="true" />
              <Link className="points__link" href="/me">
                明细
              </Link>
            </div>
          ) : null}
        </div>
      </header>

      {/* 三个入口都是真能走的：01/02 直接生成，03 先把参考图换成一段可编辑的描述 */}
      <div className="ways" role="group" aria-label="创作方式">
        <button
          type="button"
          className={studio.way === "text" ? "way way--on" : "way"}
          aria-pressed={studio.way === "text"}
          onClick={() => studio.setWay("text")}
        >
          <span className="way__n">入口 01</span>
          <b className="way__t">文字创作</b>
          <span className="way__d">从一句话开始，不上传图片。</span>
        </button>

        <button
          type="button"
          className={studio.way === "photo" ? "way way--on" : "way"}
          aria-pressed={studio.way === "photo"}
          onClick={() => studio.setWay("photo")}
        >
          <span className="way__n">入口 02</span>
          <b className="way__t">改造我的图片</b>
          <span className="way__d">上传一张图片，按描述改；人物、宠物、风景都行。</span>
        </button>

        <button
          type="button"
          className={studio.way === "inspire" ? "way way--on" : "way"}
          aria-pressed={studio.way === "inspire"}
          onClick={() => studio.setWay("inspire")}
        >
          <span className="way__n">入口 03</span>
          <b className="way__t">参考图片找灵感</b>
          <span className="way__d">放一张喜欢的图，先换回一段能改的描述。</span>
        </button>
      </div>

      {studio.way === "inspire" ? (
        <ReferencePanel
          reference={studio.reference}
          busy={studio.busy}
          locked={session.phase !== "loading" && !signedIn}
          onGate={() => setGateOpen(true)}
          onPick={guard(studio.pickReferenceFile)}
          onAnalyze={guard(studio.analyzeReference)}
          onApply={studio.applyReferenceDescription}
          onReset={studio.resetReference}
        />
      ) : null}

      {!signedIn ? (
        <Notice
          tone={session.phase === "error" ? "danger" : "info"}
          title={
            session.phase === "error"
              ? "暂时连不上服务"
              : session.phase === "needs-verification"
                ? "账号已开通，只差邮箱验证"
                : session.phase === "loading"
                  ? "正在确认登录状态"
                  : "先开通，再开始生成"
          }
        >
          {session.phase === "error" ? (
            "浏览不受影响；连接恢复后再回来上传和生成。"
          ) : session.phase === "loading" ? (
            "马上就好。"
          ) : (
            <>
              作品、灵感分类和生成方式都可以先随便看。上传图片与提交生成需要用邀请码开通并完成邮箱验证。
              {session.phase === "needs-verification" ? " 你已经有账号了，只差验证邮箱。" : ""}
              <span className="btnrow btnrow--top">
                <button type="button" className="btn btn--primary" onClick={() => setGateOpen(true)}>
                  {session.phase === "needs-verification" ? "处理邮箱验证" : "去开通"}
                </button>
              </span>
            </>
          )}
        </Notice>
      ) : null}

      <Composer
        mode={studio.mode}
        upload={studio.upload}
        busy={studio.busy}
        ready={studio.ready}
        prompt={studio.prompt}
        canSubmit={studio.canSubmit}
        working={studio.working}
        locked={session.phase !== "loading" && !signedIn}
        onGate={() => setGateOpen(true)}
        onPick={guard(studio.pickFile)}
        onReset={studio.resetUpload}
        onPromptChange={studio.setPrompt}
        onSubmit={guard(studio.submit)}
      />

      {failure && !failure.isAuth && !studio.watch ? (
        <Notice tone="danger" title={failureTitle(failure.code)} failure={failure}>
          {failure.message}
        </Notice>
      ) : null}

      <GenerationStatus
        watch={studio.watch}
        resumed={studio.resumed}
        failure={studio.failure}
        downloads={studio.downloads}
        busy={studio.busy}
        onDownload={studio.download}
        onRetry={studio.retry}
        onRegenerate={guard(studio.submit)}
      />

      {/*
        分享与反馈只对「已有终局任务」的会话出现。
        `SUCCEEDED` 才可能分享（服务端同样只放行成功任务）；
        反馈接受 `SUCCEEDED`/`FAILED`，`NEEDS_REVIEW` 结果未定，不给入口。

        `key` 绑定任务号：两个面板各自持有**局部**状态（勾选了哪几张、
        反馈表单草稿）。任务一换就重新挂载，局部状态随之清空 —— 否则
        「上一个任务里取消勾选的第二张」会带到新任务下，用户看到的选择
        与真正提交的内容不一致。
      */}
      {succeeded ? (
        <SharePanel
          key={`share-${studio.generationId ?? "none"}`}
          outputs={studio.watch?.outputs ?? []}
          share={studio.share}
          busy={studio.busy}
          locked={session.phase !== "loading" && !signedIn}
          onGate={() => setGateOpen(true)}
          onShare={guard(studio.createShareLink)}
          onAnother={studio.startAnotherShare}
        />
      ) : null}

      {feedbackable ? (
        <FeedbackPanel
          key={`feedback-${studio.generationId ?? "none"}`}
          feedback={studio.feedback}
          busy={studio.busy}
          locked={session.phase !== "loading" && !signedIn}
          available={feedbackable}
          onGate={() => setGateOpen(true)}
          onSubmit={guard(studio.submitFeedback)}
        />
      ) : null}

      <p className="hint">
        想要更多结果，用同一个描述再提交一次就行——每次提交都会按服务端结算扣点。
        <Link href="/me"> 查看我的点数与历史 →</Link>
      </p>

      {gateOpen ? (
        <AuthGate
          phase={session.phase}
          failure={session.failure}
          onSessionChanged={session.refresh}
          onRetry={session.refresh}
          onClose={() => setGateOpen(false)}
        />
      ) : null}
    </div>
  );
}
