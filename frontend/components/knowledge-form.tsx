"use client";

import { useId, useState } from "react";
import type {
  KnowledgeBase,
  KnowledgeInput,
  KnowledgeOption,
  KnowledgeOptions,
} from "../lib/knowledge";
import { ApiError, errorMessage } from "../lib/runs";
import { ErrorNotice } from "./ui";

export function KnowledgePicker({
  options,
  value,
  onChange,
  max = Infinity,
}: {
  options: KnowledgeOption[];
  value: string[];
  onChange: (value: string[]) => void;
  max?: number;
}) {
  return (
    <div className="skill-options">
      {!options.length && (
        <p className="muted">还没有知识库，请先在知识库页面创建。</p>
      )}
      {options.map((item) => (
        <label className="check-field" key={item.kb_id}>
          <input
            type="checkbox"
            checked={value.includes(item.kb_id)}
            disabled={value.length >= max && !value.includes(item.kb_id)}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...value, item.kb_id]
                  : value.filter((id) => id !== item.kb_id),
              )
            }
          />
          {item.name}
          <small className="muted">{item.document_count} 个文档</small>
        </label>
      ))}
      {value
        .filter((id) => !options.some((item) => item.kb_id === id))
        .map((id) => (
          <label className="check-field" key={id}>
            <input
              type="checkbox"
              checked
              onChange={() => onChange(value.filter((item) => item !== id))}
            />
            {id}（知识库已移除，请重新选择）
          </label>
        ))}
    </div>
  );
}

export function KnowledgeForm({
  initial,
  options,
  save,
}: {
  initial?: KnowledgeBase;
  options: KnowledgeOptions;
  save: (input: KnowledgeInput) => Promise<unknown>;
}) {
  const prefix = useId();
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [model, setModel] = useState(
    initial?.embedding_model ?? options.default_embedding_model,
  );
  const [size, setSize] = useState(
    initial?.chunking.size ?? options.chunking.size,
  );
  const [overlap, setOverlap] = useState(
    initial?.chunking.overlap ?? options.chunking.overlap,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [nameError, setNameError] = useState("");
  const [saved, setSaved] = useState(false);
  const chunkError =
    !Number.isInteger(size) || size < 200 || size > 2000
      ? "切分大小须为 200–2000 的整数"
      : !Number.isInteger(overlap) || overlap < 0 || overlap > size / 2
        ? "重叠须为 0 到切分大小一半的整数"
        : "";
  return (
    <form
      className="asset-form"
      onChange={() => setSaved(false)}
      onSubmit={async (event) => {
        event.preventDefault();
        if (chunkError) {
          setError(chunkError);
          return;
        }
        setBusy(true);
        setError("");
        setNameError("");
        try {
          await save({
            name: name.trim(),
            description,
            embedding_model: model,
            chunking: { size, overlap },
          });
          setSaved(true);
        } catch (err) {
          if (
            err instanceof ApiError &&
            err.status === 409 &&
            /名/.test(err.message)
          )
            setNameError(err.message);
          else setError(errorMessage(err));
        } finally {
          setBusy(false);
        }
      }}
    >
      <fieldset className="knowledge-fields" disabled={busy}>
        <div className="asset-field">
          <label htmlFor={`${prefix}-name`}>名称</label>
          <input
            id={`${prefix}-name`}
            required
            minLength={1}
            maxLength={50}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setNameError("");
            }}
            aria-invalid={Boolean(nameError)}
            aria-describedby={nameError ? `${prefix}-name-error` : undefined}
          />
          {nameError && (
            <p className="field-error" role="alert" id={`${prefix}-name-error`}>
              {nameError}
            </p>
          )}
        </div>
        <label className="asset-field">
          说明
          <textarea
            maxLength={500}
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
        <div className="asset-field">
          <label htmlFor={`${prefix}-model`}>向量模型</label>
          <select
            id={`${prefix}-model`}
            value={model}
            disabled={Boolean(initial?.document_count)}
            onChange={(event) => setModel(event.target.value)}
          >
            {options.embedding_models.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
          {Boolean(initial?.document_count) && (
            <small className="muted">已有文档，不能更改向量模型。</small>
          )}
        </div>
        <details className="knowledge-advanced">
          <summary>高级 · 文档切分</summary>
          <div className="knowledge-grid">
            <div className="asset-field">
              <label htmlFor={`${prefix}-size`}>切分大小</label>
              <input
                id={`${prefix}-size`}
                aria-describedby={`${prefix}-size-help`}
                type="number"
                min={200}
                max={2000}
                step={1}
                required
                value={Number.isNaN(size) ? "" : size}
                onChange={(event) => setSize(event.target.valueAsNumber)}
              />
              <small className="muted" id={`${prefix}-size-help`}>
                200–2000 字符
              </small>
            </div>
            <div className="asset-field">
              <label htmlFor={`${prefix}-overlap`}>切分重叠</label>
              <input
                id={`${prefix}-overlap`}
                aria-describedby={`${prefix}-overlap-help`}
                type="number"
                min={0}
                max={Math.floor(size / 2)}
                step={1}
                required
                value={Number.isNaN(overlap) ? "" : overlap}
                onChange={(event) => setOverlap(event.target.valueAsNumber)}
              />
              <small className="muted" id={`${prefix}-overlap-help`}>
                0 到切分大小的一半
              </small>
            </div>
          </div>
          {chunkError && <p className="field-error">{chunkError}</p>}
          {initial && <p className="muted">只影响之后上传或重建的文档</p>}
        </details>
        {error && <ErrorNotice message={error} />}
        <button
          className="button primary"
          disabled={busy || !name.trim() || Boolean(chunkError)}
        >
          {busy ? "正在保存…" : initial ? "保存设置" : "创建知识库"}
        </button>
        {saved && initial && (
          <span className="muted" role="status">
            设置已保存
          </span>
        )}
      </fieldset>
    </form>
  );
}
