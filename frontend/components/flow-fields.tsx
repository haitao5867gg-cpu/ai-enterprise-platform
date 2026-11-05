"use client";

import { useT } from "../lib/i18n/client";
import { useId, useRef, useState, type ReactNode } from "react";
import type { Variable } from "../lib/flow-edit";

export function FieldIssues({
  id,
  messages,
}: {
  id: string;
  messages: string[];
}) {
  if (!messages.length) return null;
  return (
    <p className="field-error" id={id}>
      {messages.join("；")}
    </p>
  );
}

type Common = {
  label: string;
  issues?: string[];
  help?: ReactNode;
  disabled?: boolean;
};

export function TextField({
  label,
  value,
  onChange,
  issues = [],
  help,
  disabled,
  maxLength,
  onBlur,
}: Common & {
  value: string;
  onChange: (value: string) => void;
  maxLength?: number;
  onBlur?: () => void;
}) {
  const id = useId();
  return (
    <div className="asset-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={value}
        maxLength={maxLength}
        disabled={disabled}
        aria-invalid={issues.length > 0}
        aria-describedby={issues.length ? `${id}-issues` : undefined}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
        onKeyDown={(event) => {
          if (event.key === "Enter" && onBlur) {
            event.preventDefault();
            onBlur();
          }
        }}
      />
      {help && <small className="muted">{help}</small>}
      <FieldIssues id={`${id}-issues`} messages={issues} />
    </div>
  );
}

export function NoteField({
  value,
  onChange,
  issues = [],
}: {
  value: string;
  onChange: (value: string) => void;
  issues?: string[];
}) {
  const t = useT();
  const id = useId();
  return (
    <div className="asset-field">
      <label htmlFor={id}>{t("讲解说明")}</label>
      <textarea
        id={id}
        rows={3}
        value={value}
        aria-invalid={issues.length > 0}
        aria-describedby={`${id}-count${issues.length ? ` ${id}-issues` : ""}`}
        onChange={(event) =>
          onChange(Array.from(event.target.value).slice(0, 500).join(""))
        }
      />
      <small id={`${id}-count`} className="muted note-count">
        {t("{n} / 500 字", { n: Array.from(value).length })}
      </small>
      <FieldIssues id={`${id}-issues`} messages={issues} />
    </div>
  );
}

export function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  issues = [],
  help,
  disabled,
}: Common & {
  value: number | undefined;
  onChange: (value: number) => void;
  min: number;
  max: number;
}) {
  const id = useId();
  return (
    <div className="asset-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        step={1}
        value={value ?? ""}
        disabled={disabled}
        aria-invalid={issues.length > 0}
        aria-describedby={issues.length ? `${id}-issues` : undefined}
        onChange={(event) => {
          const next = Number(event.target.value);
          if (event.target.value !== "" && Number.isFinite(next))
            onChange(next);
        }}
      />
      {help && <small className="muted">{help}</small>}
      <FieldIssues id={`${id}-issues`} messages={issues} />
    </div>
  );
}

export function SelectField({
  label,
  value,
  onChange,
  choices,
  issues = [],
  help,
  disabled,
}: Common & {
  value: string;
  onChange: (value: string) => void;
  choices: { value: string; label: string }[];
}) {
  const id = useId();
  return (
    <div className="asset-field">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        aria-invalid={issues.length > 0}
        aria-describedby={issues.length ? `${id}-issues` : undefined}
        onChange={(event) => onChange(event.target.value)}
      >
        {choices.map((choice) => (
          <option key={choice.value} value={choice.value}>
            {choice.label}
          </option>
        ))}
      </select>
      {help && <small className="muted">{help}</small>}
      <FieldIssues id={`${id}-issues`} messages={issues} />
    </div>
  );
}

export function CheckField({
  label,
  checked,
  onChange,
  disabled,
  help,
  issues = [],
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  help?: ReactNode;
  issues?: string[];
}) {
  const id = useId();
  const box = (
    <label className="check-field">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-describedby={help || issues.length ? `${id}-help` : undefined}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  );
  if (!help && !issues.length) return box;
  return (
    <div className="check-field-block" id={`${id}-help`}>
      {box}
      {help && <small className="muted">{help}</small>}
      <FieldIssues id={`${id}-issues`} messages={issues} />
    </div>
  );
}

// A text box whose value may contain {{node.field}} references, with a menu that
// inserts one of the variables this node can see at the cursor.
export function RefField({
  label,
  value,
  onChange,
  variables,
  multiline = false,
  rows = 4,
  issues = [],
  help,
  disabled,
}: Common & {
  value: string;
  onChange: (value: string) => void;
  variables: Variable[];
  multiline?: boolean;
  rows?: number;
}) {
  const t = useT();
  const id = useId();
  const box = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const field = useRef<HTMLDivElement>(null);
  const caret = useRef<[number, number] | null>(null);
  const [open, setOpen] = useState(false);
  function remember() {
    const element = box.current;
    if (element)
      caret.current = [
        element.selectionStart ?? element.value.length,
        element.selectionEnd ?? element.value.length,
      ];
  }
  function insert(ref: string) {
    const [a, b] = caret.current ?? [value.length, value.length];
    onChange(value.slice(0, a) + ref + value.slice(b));
    setOpen(false);
    const at = a + ref.length;
    caret.current = [at, at];
    requestAnimationFrame(() => {
      // Put the caret back after the variable, unless focus has already moved on to
      // another control: taking it back would send what is typed there into this box.
      const active = document.activeElement;
      if (
        active &&
        active !== document.body &&
        !field.current?.contains(active)
      )
        return;
      box.current?.focus();
      box.current?.setSelectionRange(at, at);
    });
  }
  const groups = [...new Set(variables.map((item) => item.group))];
  const props = {
    id,
    ref: box,
    value,
    disabled,
    "aria-invalid": issues.length > 0,
    "aria-describedby": issues.length ? `${id}-issues` : undefined,
    onSelect: remember,
    onKeyUp: remember,
    onBlur: remember,
    onChange: (event: { target: { value: string } }) =>
      onChange(event.target.value),
  };
  return (
    <div className="asset-field ref-field" ref={field}>
      <div className="ref-field-heading">
        <label htmlFor={id}>{label}</label>
        <button
          type="button"
          className="text-button"
          aria-expanded={open}
          aria-label={t("插入变量：{label}", { label })}
          disabled={disabled}
          onClick={() => setOpen((value) => !value)}
        >
          {t("插入变量")}
        </button>
      </div>
      {multiline ? <textarea {...props} rows={rows} /> : <input {...props} />}
      {open && (
        <div
          className="ref-menu"
          role="group"
          aria-label={t("可插入「{label}」的变量", { label })}
          onKeyDown={(event) => {
            if (event.key === "Escape") setOpen(false);
          }}
        >
          {!variables.length && (
            <p className="muted">
              {t("这里还没有可引用的变量。先把前面的节点连到这个节点。")}
            </p>
          )}
          {groups.map((group) => (
            <div key={group}>
              <strong>{group}</strong>
              <div className="ref-menu-items">
                {variables
                  .filter((item) => item.group === group)
                  .map((item) => (
                    <button
                      key={item.ref}
                      type="button"
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => insert(item.ref)}
                    >
                      {item.ref}
                    </button>
                  ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {help && <small className="muted">{help}</small>}
      <FieldIssues id={`${id}-issues`} messages={issues} />
    </div>
  );
}

// An editable list: each row has move up / move down / remove.
export function ListEditor<T>({
  label,
  items,
  onChange,
  make,
  render,
  issues,
  min = 0,
  max = 20,
  disabled,
  onRemove,
}: {
  label: string;
  items: T[];
  onChange: (items: T[]) => void;
  make: () => T;
  render: (item: T, index: number, update: (item: T) => void) => ReactNode;
  issues?: (index: number) => string[];
  min?: number;
  max?: number;
  disabled?: boolean;
  onRemove?: (item: T) => void;
}) {
  const t = useT();
  const move = (from: number, to: number) => {
    const next = [...items];
    next.splice(to, 0, next.splice(from, 1)[0]);
    onChange(next);
  };
  return (
    <fieldset className="list-editor" disabled={disabled}>
      <legend>{label}</legend>
      {items.map((item, index) => {
        const messages = issues?.(index) ?? [];
        return (
          <div
            className={`list-row ${messages.length ? "invalid" : ""}`}
            role="group"
            aria-label={`${label} ${index + 1}`}
            key={index}
          >
            <div className="list-row-fields">
              {render(item, index, (value) =>
                onChange(items.map((old, i) => (i === index ? value : old))),
              )}
            </div>
            <div className="list-row-actions">
              <button
                type="button"
                className="text-button"
                aria-label={t("上移{label} {n}", { label, n: index + 1 })}
                disabled={index === 0}
                onClick={() => move(index, index - 1)}
              >
                ↑
              </button>
              <button
                type="button"
                className="text-button"
                aria-label={t("下移{label} {n}", { label, n: index + 1 })}
                disabled={index === items.length - 1}
                onClick={() => move(index, index + 1)}
              >
                ↓
              </button>
              <button
                type="button"
                className="text-button"
                aria-label={t("删除{label} {n}", { label, n: index + 1 })}
                disabled={items.length <= min}
                onClick={() => {
                  onRemove?.(item);
                  onChange(items.filter((_, i) => i !== index));
                }}
              >
                {t("删除")}
              </button>
            </div>
            {messages.length > 0 && (
              <p className="field-error">{messages.join(t("；"))}</p>
            )}
          </div>
        );
      })}
      <button
        type="button"
        className="button secondary"
        disabled={items.length >= max}
        onClick={() => onChange([...items, make()])}
      >
        {t("添加{label}", { label })}
      </button>
    </fieldset>
  );
}
