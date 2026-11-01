"use client";

import { useId, useState } from "react";
import { FieldIssues } from "./flow-fields";

// Checks as docs/api/data-nodes.md ("data_check") describes them; the data check node
// and a project's acceptance checks are both edited here and checked by the backend.
export const CHECKS_HELP =
  "每条检查写 id、label、kind（aggregate 聚合值、reconcile 两表对账、unique 键不重复、golden 和老系统逐行对比、rows 不该有的行）和各自的设置；表名写结果表的名字或 catalog.schema.table，条件里可以用 ${param.参数标识}。";
const isCheck = (item: unknown): item is Record<string, unknown> =>
  typeof item === "object" && item !== null && !Array.isArray(item);

// The checks as JSON: the text is kept while it does not parse, and follows the value
// when it changes from outside (undo, an import).
export function ChecksField({
  value,
  onChange,
  issues,
  label = "检查（JSON）",
  help = CHECKS_HELP,
}: {
  value: Record<string, unknown>[];
  onChange: (value: Record<string, unknown>[]) => void;
  issues: string[];
  label?: string;
  help?: string;
}) {
  const id = useId();
  const [draft, setDraft] = useState(() => JSON.stringify(value, null, 2));
  const [problem, setProblem] = useState("");
  const [shown, setShown] = useState(value);
  if (shown !== value) {
    // The value changed: keep the text when it already says the same checks.
    setShown(value);
    let same = false;
    try {
      same =
        JSON.stringify(JSON.parse(draft || "[]")) === JSON.stringify(value);
    } catch {
      same = false;
    }
    if (!same) {
      setDraft(JSON.stringify(value, null, 2));
      setProblem("");
    }
  }
  const messages = problem ? [problem, ...issues] : issues;
  return (
    <div className="asset-field">
      <label htmlFor={id}>{label}</label>
      <textarea
        id={id}
        className="code-input"
        rows={12}
        spellCheck={false}
        value={draft}
        aria-invalid={messages.length > 0}
        aria-describedby={messages.length ? `${id}-issues` : undefined}
        onChange={(event) => {
          const text = event.target.value;
          setDraft(text);
          let parsed: unknown;
          try {
            parsed = JSON.parse(text.trim() || "[]");
          } catch {
            setProblem("不是合法的 JSON，改好之前不会保存这里的修改");
            return;
          }
          if (!Array.isArray(parsed) || !parsed.every(isCheck)) {
            setProblem("要写成检查的列表：[{...}, {...}]");
            return;
          }
          setProblem("");
          onChange(parsed);
        }}
      />
      <small className="muted">{help}</small>
      {value.length > 0 && (
        <ul className="muted" aria-label="已写的检查">
          {value.map((item, i) => (
            <li key={i}>
              {String(item.label ?? item.id ?? `第 ${i + 1} 条`)}
              {item.kind ? `（${String(item.kind)}）` : ""}
            </li>
          ))}
        </ul>
      )}
      <FieldIssues id={`${id}-issues`} messages={messages} />
    </div>
  );
}
