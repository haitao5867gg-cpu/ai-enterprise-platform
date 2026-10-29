"use client";

import { useId } from "react";
import type { RerankModel, Retrieval } from "../lib/knowledge";
import { retrievalProblems, retrievalValue } from "../lib/retrieval";

const modes = [
  { value: "hybrid", label: "混合", help: "向量和关键词加权" },
  { value: "vector", label: "向量", help: "按意思找" },
  { value: "keyword", label: "关键词", help: "按词找，不调向量模型" },
] as const;

export function RetrievalFields({
  value,
  topK,
  models,
  onChange,
  issues = [],
}: {
  value: Retrieval | undefined;
  topK: number;
  models: RerankModel[];
  onChange: (value: Retrieval) => void;
  issues?: string[];
}) {
  const id = useId();
  const current = retrievalValue(value);
  const errors = retrievalProblems(value, topK, models);
  const messages = [...new Set([...Object.values(errors), ...issues])];
  const update = (next: Partial<Retrieval>) =>
    onChange({ ...current, ...next });
  return (
    <fieldset className="list-editor retrieval-fields" aria-label="检索设置">
      <legend>检索方式</legend>
      <div className="retrieval-modes" role="group" aria-label="检索方式">
        {modes.map((mode) => (
          <button
            key={mode.value}
            type="button"
            aria-pressed={current.mode === mode.value}
            onClick={() => update({ mode: mode.value })}
          >
            {mode.label}
          </button>
        ))}
      </div>
      <p className="muted retrieval-help">
        {modes.find((mode) => mode.value === current.mode)?.help}
      </p>
      {current.mode === "hybrid" && (
        <label className="asset-field">
          向量权重
          <span className="retrieval-weight">
            <input
              type="range"
              aria-label="向量权重"
              min={0}
              max={1}
              step={0.05}
              value={current.vector_weight}
              onChange={(event) =>
                update({ vector_weight: event.target.valueAsNumber })
              }
            />
            <output>
              向量 {current.vector_weight.toFixed(2)} · 关键词{" "}
              {(1 - current.vector_weight).toFixed(2)}
            </output>
          </span>
        </label>
      )}
      <label className="asset-field retrieval-switch">
        <input
          type="checkbox"
          role="switch"
          checked={current.rerank !== null}
          disabled={!models.length}
          aria-describedby={!models.length ? `${id}-unavailable` : undefined}
          onChange={(event) =>
            update({
              rerank: event.target.checked
                ? { model: models[0].model, candidates: Math.max(20, topK) }
                : null,
            })
          }
        />
        重排
      </label>
      {!models.length && (
        <p className="muted retrieval-help" id={`${id}-unavailable`}>
          没有可选的重排模型，暂时无法开启重排。
        </p>
      )}
      {current.rerank && (
        <>
          <div className="asset-field">
            <label htmlFor={`${id}-model`}>重排模型</label>
            <select
              id={`${id}-model`}
              value={current.rerank.model}
              aria-invalid={Boolean(errors["rerank.model"])}
              onChange={(event) =>
                update({
                  rerank: { ...current.rerank!, model: event.target.value },
                })
              }
            >
              {(
                [
                  ["rerank", "重排模型"],
                  ["llm", "用对话模型打分"],
                ] as const
              ).map(([kind, label]) => (
                <optgroup key={kind} label={label}>
                  {models
                    .filter((model) => model.kind === kind)
                    .map(({ model }) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ))}
                </optgroup>
              ))}
            </select>
          </div>
          <label className="asset-field">
            重排候选数
            <input
              type="number"
              required
              min={5}
              max={50}
              step={1}
              value={
                Number.isNaN(current.rerank.candidates)
                  ? ""
                  : current.rerank.candidates
              }
              aria-invalid={Boolean(errors["rerank.candidates"])}
              aria-describedby={messages.length ? `${id}-errors` : undefined}
              onChange={(event) =>
                update({
                  rerank: {
                    ...current.rerank!,
                    candidates: event.target.valueAsNumber,
                  },
                })
              }
            />
          </label>
        </>
      )}
      {messages.length > 0 && (
        <p className="field-error" role="alert" id={`${id}-errors`}>
          {messages.join("；")}
        </p>
      )}
    </fieldset>
  );
}
