"use client";

import { IDEA_DIMENSIONS, IDEA_NOTE, USE_PRESETS, type IdeaDimensionId } from "@/services/catalog";
import type { OpenMode } from "@/services/config";
import { selectionCount, type AssistPreview, type AssistSelection } from "@/services/promptAssist";

/**
 * 灵感分类面板：可组合的标签目录 + 一句"将发生什么" + 撤销/恢复。
 *
 * 这里不调用任何模型，也不会自己改写用户已经写好的句子——点选只是把可读的短语
 * 追加到描述末尾。真正的拼句规则在 `services/promptAssist.ts`，组件只负责说清楚。
 */
export function IdeaPicker({
  mode,
  selection,
  preview,
  presetId,
  canUndo,
  canRestore,
  onToggle,
  onPreset,
  onUndo,
  onRestore,
}: {
  mode: OpenMode;
  selection: AssistSelection;
  preview: AssistPreview;
  /** 当前套用的用途预设（一键套用后会高亮）。 */
  presetId: string | null;
  canUndo: boolean;
  canRestore: boolean;
  onToggle: (dimensionId: IdeaDimensionId, optionId: string) => void;
  onPreset: (presetId: string) => void;
  onUndo: () => void;
  onRestore: () => void;
}) {
  const dimensions = IDEA_DIMENSIONS[mode];
  const picked = selectionCount(selection);

  return (
    <div className="ideas">
      <p className="ideas__head">
        <b>灵感分类</b>
        <span>{IDEA_NOTE}</span>
      </p>

      <div className="ideas__group ideas__group--use">
        <p className="ideas__label">
          用途
          <span>一键套用一组标签，套完还能逐项改</span>
        </p>
        <div className="chiprow">
          {USE_PRESETS.map((preset) => {
            const on = presetId === preset.id;
            return (
              <button
                key={preset.id}
                type="button"
                className={on ? "chip chip--sm chip--on" : "chip chip--sm"}
                aria-pressed={on}
                onClick={() => onPreset(preset.id)}
              >
                {preset.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="ideas__grid">
        {dimensions.map((dimension) => (
          <div className="ideas__group" key={dimension.id}>
            <p className="ideas__label">
              {dimension.label}
              <span>{dimension.hint}</span>
            </p>
            <div className="chiprow">
              {dimension.options.map((option) => {
                const on = selection[dimension.id] === option.id;
                return (
                  <button
                    key={option.id}
                    type="button"
                    className={on ? "chip chip--sm chip--on" : "chip chip--sm"}
                    aria-pressed={on}
                    onClick={() => onToggle(dimension.id, option.id)}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <p className="ideas__preview" role="status" aria-live="polite">
        {picked === 0 ? (
          "还没选。选中的词会出现在下面这段描述里，之后随时可以改。"
        ) : preview.action === "compose" ? (
          <>
            会生成这段描述：<b>{preview.text}</b>
          </>
        ) : (
          <>
            会追加到你已有描述的末尾：
            <b>{preview.added.length > 0 ? preview.added.join("，") : "（都已经在里面了，不会重复写一遍）"}</b>
          </>
        )}
      </p>

      <div className="btnrow ideas__acts">
        <button type="button" className="btn btn--quiet" disabled={!canUndo} onClick={onUndo}>
          撤销上一步
        </button>
        <button type="button" className="btn btn--quiet" disabled={!canRestore} onClick={onRestore}>
          恢复刚进来时的描述
        </button>
      </div>
    </div>
  );
}
