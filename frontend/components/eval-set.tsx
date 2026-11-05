"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";
import {
  chatModelChoices,
  type AppDetail,
  type FlowConfig,
  type FormField,
  type HumanNode,
  type Input,
} from "../lib/apps";
import {
  checkKinds,
  checkText,
  cleanCase,
  getEvalSet,
  importEvalSet,
  MAX_CASES,
  MAX_CHECKS,
  putEvalSet,
  SAME_MEANING,
  shown,
  typedValue,
  valueText,
  type EvalAnswer,
  type EvalCase,
  type EvalCheck,
  type EvalSet,
} from "../lib/evals";
import { errorMessage } from "../lib/runs";
import { useMay } from "./session";
import { InputFields } from "./input-fields";
import { ErrorNotice } from "./ui";

const CASE_ID = /^[a-z0-9_-]{1,60}$/;

function blankCheck(kind: EvalCheck["kind"], path: string | null): EvalCheck {
  if (kind === "equals") return { kind, path, value: "" };
  if (kind === "judge")
    return { kind, path, criteria: SAME_MEANING, reference: null };
  return { kind, path, values: [""] };
}

function nextId(cases: EvalCase[]): string {
  let n = cases.length + 1;
  while (cases.some((item) => item.id === `case-${n}`)) n += 1;
  return `case-${n}`;
}

function CheckEditor({
  check,
  index,
  onChange,
  onRemove,
}: {
  check: EvalCheck;
  index: number;
  onChange: (check: EvalCheck) => void;
  onRemove: () => void;
}) {
  const id = useId();
  return (
    <fieldset className="eval-check" aria-label={`检查 ${index + 1}`}>
      <div className="eval-check-row">
        <label className="asset-field">
          检查方式
          <select
            value={check.kind}
            onChange={(e) =>
              onChange(
                blankCheck(e.target.value as EvalCheck["kind"], check.path),
              )
            }
          >
            {Object.entries(checkKinds).map(([kind, label]) => (
              <option key={kind} value={kind}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="asset-field">
          结果字段
          <input
            value={check.path ?? ""}
            placeholder="不填就是整个结果"
            onChange={(e) => onChange({ ...check, path: e.target.value })}
          />
        </label>
        <button type="button" className="text-button" onClick={onRemove}>
          删除这项
        </button>
      </div>
      {check.kind === "equals" && (
        <label className="asset-field">
          应该等于
          <input
            value={valueText(check.value)}
            onChange={(e) =>
              onChange({ ...check, value: typedValue(e.target.value) })
            }
          />
          <small className="muted">
            数字、true / false 和 JSON 按值比较，其余按文字比较。
          </small>
        </label>
      )}
      {(check.kind === "contains" || check.kind === "excludes") && (
        <label className="asset-field">
          {check.kind === "contains" ? "每个词都要出现" : "这些词都不能出现"}
          <textarea
            rows={2}
            value={check.values.join("\n")}
            onChange={(e) =>
              onChange({ ...check, values: e.target.value.split("\n") })
            }
            aria-describedby={`${id}-words`}
          />
          <small className="muted" id={`${id}-words`}>
            一行一个词，不分大小写；写成「七天|7 天」表示其一即可。
          </small>
        </label>
      )}
      {check.kind === "judge" && (
        <>
          <label className="asset-field">
            判断标准
            <textarea
              rows={2}
              value={check.criteria}
              onChange={(e) => onChange({ ...check, criteria: e.target.value })}
            />
          </label>
          <label className="asset-field">
            参考答案（可不填）
            <textarea
              rows={3}
              value={check.reference == null ? "" : valueText(check.reference)}
              onChange={(e) =>
                onChange({ ...check, reference: e.target.value })
              }
            />
          </label>
        </>
      )}
    </fieldset>
  );
}

function AnswerEditor({
  node,
  answer,
  onChange,
}: {
  node: HumanNode;
  answer: EvalAnswer | undefined;
  onChange: (answer: EvalAnswer | undefined) => void;
}) {
  const id = useId();
  if (node.mode === "form") {
    const fields = node.fields as FormField[];
    return (
      <fieldset className="eval-answer" aria-label={`回答「${node.title}」`}>
        <legend>{node.title}（表单）</legend>
        <InputFields
          fields={fields as Input[]}
          values={answer?.values ?? {}}
          prefix={`${id}-form`}
          onChange={(values) => onChange({ values })}
        />
      </fieldset>
    );
  }
  return (
    <fieldset className="eval-answer" aria-label={`回答「${node.title}」`}>
      <legend>{node.title}（确认）</legend>
      <label className="asset-field">
        选哪个
        <select
          value={answer?.decision ?? ""}
          onChange={(e) =>
            onChange(
              e.target.value || answer?.comment
                ? {
                    ...answer,
                    decision: e.target.value || undefined,
                  }
                : undefined,
            )
          }
        >
          <option value="">第一个选项（{node.options[0]?.label}）</option>
          {node.options.map((option) => (
            <option key={option.key} value={option.key}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      <label className="asset-field">
        意见
        <input
          value={answer?.comment ?? ""}
          onChange={(e) =>
            onChange({ ...answer, comment: e.target.value || null })
          }
        />
      </label>
    </fieldset>
  );
}

function CaseDialog({
  initial,
  isNew,
  taken,
  inputs,
  humans,
  onSave,
  onClose,
}: {
  initial: EvalCase;
  isNew: boolean;
  taken: string[];
  inputs: Input[];
  humans: HumanNode[];
  onSave: (item: EvalCase) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [item, setItem] = useState(initial);
  const [problem, setProblem] = useState("");
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  const set = (patch: Partial<EvalCase>) => setItem({ ...item, ...patch });
  function save() {
    if (!CASE_ID.test(item.id))
      return setProblem(
        "用例标识只能用小写字母、数字、- 和 _，最多 60 个字符。",
      );
    if (taken.includes(item.id))
      return setProblem(`已经有用例 ${item.id} 了。`);
    const empty = item.expect.findIndex(
      (check) =>
        (check.kind === "judge" && !check.criteria.trim()) ||
        ((check.kind === "contains" || check.kind === "excludes") &&
          !check.values.some((value) => value.trim())),
    );
    if (empty >= 0) return setProblem(`检查 ${empty + 1} 还没写完。`);
    onSave(cleanCase(item));
  }
  return (
    <dialog
      ref={ref}
      className="knowledge-drawer eval-case-dialog"
      aria-labelledby="eval-case-title"
      onCancel={onClose}
      onClose={onClose}
    >
      <form
        className="asset-form"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <div className="asset-section-heading">
          <h2 id="eval-case-title">
            {isNew ? "新增用例" : `编辑用例 ${initial.id}`}
          </h2>
          <button type="button" className="text-button" onClick={onClose}>
            关闭
          </button>
        </div>
        <div className="eval-check-row">
          <label className="asset-field">
            用例标识
            <input
              required
              value={item.id}
              onChange={(e) => set({ id: e.target.value.trim() })}
            />
          </label>
          <label className="asset-field">
            名称
            <input
              value={item.title ?? ""}
              maxLength={100}
              onChange={(e) => set({ title: e.target.value })}
            />
          </label>
        </div>
        <h3>输入</h3>
        {inputs.length ? (
          <InputFields
            fields={inputs}
            values={item.input}
            prefix="eval-case-input"
            onChange={(input) => set({ input })}
          />
        ) : (
          <p className="muted">这个应用的开始节点没有输入。</p>
        )}
        {humans.length > 0 && (
          <>
            <h3>停在人工节点时怎么回答</h3>
            <p className="muted">
              没写的确认节点选第一个选项；表单节点必须写，否则这条用例出错。
            </p>
            {humans.map((node) => (
              <AnswerEditor
                key={node.id}
                node={node}
                answer={item.answers[node.id]}
                onChange={(answer) => {
                  const answers = { ...item.answers };
                  if (answer) answers[node.id] = answer;
                  else delete answers[node.id];
                  set({ answers });
                }}
              />
            ))}
          </>
        )}
        <h3>检查结果</h3>
        <p className="muted">
          每项是一个得分点；另有一项「运行完成」总是打分。
        </p>
        {item.expect.map((check, index) => (
          <CheckEditor
            key={index}
            index={index}
            check={check}
            onChange={(next) =>
              set({
                expect: item.expect.map((c, i) => (i === index ? next : c)),
              })
            }
            onRemove={() =>
              set({ expect: item.expect.filter((_, i) => i !== index) })
            }
          />
        ))}
        <button
          type="button"
          className="button secondary"
          disabled={item.expect.length >= MAX_CHECKS}
          onClick={() =>
            set({ expect: [...item.expect, blankCheck("contains", null)] })
          }
        >
          加一项检查
        </button>
        {problem && (
          <p className="field-error" role="alert">
            {problem}
          </p>
        )}
        <div className="eval-dialog-actions">
          <button className="button primary">放进评测集</button>
          <button type="button" className="button secondary" onClick={onClose}>
            取消
          </button>
        </div>
      </form>
    </dialog>
  );
}

// Cases from a spreadsheet (docs/api/evals.md, "从表格导入"): one row per case, the
// columns named after where their values go.
function ImportForm({
  app,
  count,
  inputs,
  humans,
  onImported,
  onClose,
}: {
  app: string;
  count: number;
  inputs: Input[];
  humans: HumanNode[];
  onImported: (set: EvalSet, imported: number) => void;
  onClose: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<"append" | "replace">("append");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const example = [
    "id",
    "title",
    ...(inputs.length
      ? inputs.map((input) => `input.${input.key}`)
      : ["input.question"]),
    "expect.<结果字段>",
    ...humans
      .slice(0, 2)
      .map((node) =>
        node.mode === "form" && node.fields[0]
          ? `answer.${node.id}.${node.fields[0].key}`
          : `answer.${node.id}`,
      ),
  ].join(", ");

  async function submit() {
    if (!file) return;
    if (
      mode === "replace" &&
      count > 0 &&
      !window.confirm(`替换会删掉现在的 ${count} 条用例，继续吗？`)
    )
      return;
    setBusy(true);
    setError("");
    try {
      const saved = await importEvalSet(app, file, mode);
      onImported(saved, saved.imported);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="eval-import"
      aria-label="从表格导入"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <p className="muted">
        一行一条用例，第一行写列名：id、title、input.&lt;输入&gt;、expect.&lt;结果字段&gt;
        （等于）、contains. 或 excludes.&lt;结果字段&gt;（多个词用分号隔开）、
        judge.&lt;结果字段&gt;（判断标准）、answer.&lt;人工节点&gt;（选项）、
        answer.&lt;人工节点&gt;.&lt;字段&gt;。例如：
        <span className="monospace"> {example}</span>
      </p>
      <div className="eval-check-row">
        <label className="asset-field">
          表格文件（.xlsx 或 .csv，最多 5 MB）
          <input
            type="file"
            accept=".xlsx,.csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </label>
        <fieldset className="eval-import-mode">
          <legend>导入方式</legend>
          <label>
            <input
              type="radio"
              name="eval-import-mode"
              checked={mode === "append"}
              onChange={() => setMode("append")}
            />{" "}
            加在现有用例后面
          </label>
          <label>
            <input
              type="radio"
              name="eval-import-mode"
              checked={mode === "replace"}
              onChange={() => setMode("replace")}
            />{" "}
            替换全部用例
          </label>
        </fieldset>
      </div>
      <div className="eval-import-actions">
        <button className="button primary" disabled={busy || !file}>
          {busy ? "正在导入…" : "导入"}
        </button>
        <button type="button" className="text-button" onClick={onClose}>
          取消
        </button>
      </div>
      {error && <ErrorNotice message={error} />}
    </form>
  );
}

export function EvalSetPanel({
  app,
  config,
  options,
  onSaved,
  revision = 0,
}: {
  app: string;
  // The configuration whose start inputs and person's steps the cases answer.
  config: FlowConfig | null;
  options: AppDetail["options"];
  onSaved: (set: EvalSet) => void;
  // Changes when a case was added elsewhere on the page (a debug chat's answer).
  revision?: number;
}) {
  const mayBuild = useMay("builder");
  const [set, setSet] = useState<EvalSet | null>(null);
  const [cases, setCases] = useState<EvalCase[]>([]);
  const [threshold, setThreshold] = useState("0.8");
  const [judge, setJudge] = useState("");
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const onLoaded = useRef(onSaved);
  useEffect(() => {
    onLoaded.current = onSaved;
  }, [onSaved]);

  function take(value: EvalSet) {
    setSet(value);
    setCases(value.cases);
    setThreshold(String(value.threshold));
    setJudge(value.judge_model ?? "");
    onLoaded.current(value);
  }

  useEffect(() => {
    const controller = new AbortController();
    getEvalSet(app, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) take(value);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app, retry, revision]);

  if (!set)
    return (
      <section className="panel asset-section" aria-label="评测集">
        <h2>评测集</h2>
        {error ? (
          <ErrorNotice message={error} onRetry={() => setRetry((n) => n + 1)} />
        ) : (
          <p className="muted">正在加载评测集…</p>
        )}
      </section>
    );

  if (set.source === "file")
    return (
      <section className="panel asset-section" aria-label="评测集">
        <h2>评测集</h2>
        <p className="muted">
          这个应用的评测集在代码仓库里维护（evals/{app}/cases.yaml），共{" "}
          {set.cases.length} 条用例，页面上只读。
        </p>
        <ul className="eval-file-cases">
          {set.cases.map((item) => (
            <li key={item.id} className="monospace">
              {item.id}
            </li>
          ))}
        </ul>
      </section>
    );

  const editable = set.editable && mayBuild;
  const dirty =
    JSON.stringify(cases) !== JSON.stringify(set.cases) ||
    Number(threshold) !== set.threshold ||
    (judge || null) !== set.judge_model;
  const thresholdValue = Number(threshold);
  const thresholdBad =
    !threshold.trim() ||
    !Number.isFinite(thresholdValue) ||
    thresholdValue < 0 ||
    thresholdValue > 1;
  const nodes = config?.graph.nodes ?? [];
  const start = nodes.find((node) => node.type === "start");
  const inputs = start?.type === "start" ? start.inputs : [];
  const humans = nodes.filter(
    (node): node is HumanNode => node.type === "human",
  );
  const models = chatModelChoices(options, judge || null);

  async function save() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      take(
        await putEvalSet(app, {
          cases,
          threshold: thresholdValue,
          judge_model: judge || null,
        }),
      );
      setNotice(
        cases.length
          ? "评测集已保存。草稿要重新评测，通过后才能发布。"
          : "评测集已清空，这个应用发布时不再需要评测。",
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel asset-section" aria-label="评测集">
      <div className="asset-section-heading">
        <h2>评测集</h2>
        {editable && (
          <button
            type="button"
            className="button secondary"
            disabled={busy || cases.length >= MAX_CASES}
            onClick={() => setEditing("new")}
          >
            新增用例
          </button>
        )}
        {editable && !importing && (
          <button
            type="button"
            className="button secondary"
            disabled={busy || dirty}
            title={dirty ? "先保存或撤销没保存的修改，再导入" : undefined}
            onClick={() => {
              setImporting(true);
              setNotice("");
            }}
          >
            从表格导入
          </button>
        )}
      </div>
      {editable && importing && (
        <ImportForm
          app={app}
          count={set.cases.length}
          inputs={inputs}
          humans={humans}
          onClose={() => setImporting(false)}
          onImported={(saved, imported) => {
            take(saved);
            setImporting(false);
            setError("");
            setNotice(
              `导入了 ${imported} 条用例，现在共 ${saved.cases.length} 条。草稿要重新评测，通过后才能发布。`,
            );
          }}
        />
      )}
      <p className="muted">
        {cases.length
          ? `共 ${cases.length} 条用例。草稿评测的总分达到门槛才能发布。`
          : "还没有用例：这个应用发布时不用评测。在运行页点“存成评测用例”，或在这里新增。"}
        {set.updated_at &&
          ` 上次由 ${set.updated_by ?? "未署名"} 修改于 ${new Date(set.updated_at).toLocaleString("zh-CN")}。`}
      </p>
      {cases.length > 0 && (
        <table className="mapping-table eval-case-table" aria-label="评测用例">
          <thead>
            <tr>
              <th>用例</th>
              <th>输入</th>
              <th>检查</th>
              <th>来源</th>
              {editable && <th aria-label="操作" />}
            </tr>
          </thead>
          <tbody>
            {cases.map((item, index) => (
              <tr key={item.id}>
                <td>
                  <strong>{item.title || item.id}</strong>
                  {item.title && (
                    <small className="muted monospace"> {item.id}</small>
                  )}
                </td>
                <td>
                  {shown(item.input)}
                  {item.history?.length ? (
                    <small className="muted">
                      {" "}
                      带前 {item.history.length} 轮对话
                    </small>
                  ) : null}
                </td>
                <td>
                  {item.expect.length ? (
                    <ul className="eval-check-list">
                      {item.expect.map((check, i) => (
                        <li key={i}>{checkText(check)}</li>
                      ))}
                    </ul>
                  ) : (
                    <span className="field-error">
                      还没有检查，只看运行完成
                    </span>
                  )}
                </td>
                <td>
                  {item.from_run ? (
                    <Link
                      className="text-button"
                      href={`/runs/${encodeURIComponent(item.from_run)}`}
                    >
                      来自运行 ↗
                    </Link>
                  ) : (
                    <span className="muted">手写</span>
                  )}
                </td>
                {editable && (
                  <td className="eval-case-actions">
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => setEditing(index)}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className="text-button danger-text"
                      onClick={() =>
                        setCases(cases.filter((_, i) => i !== index))
                      }
                    >
                      删除
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editable ? (
        <form
          className="eval-set-settings"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <label className="asset-field">
            发布门槛（0–1）
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={threshold}
              aria-invalid={thresholdBad}
              onChange={(e) => setThreshold(e.target.value)}
            />
          </label>
          <label className="asset-field">
            「模型判断」用的模型
            <select value={judge} onChange={(e) => setJudge(e.target.value)}>
              <option value="">平台默认（{options.default_model}）</option>
              {models.map((model) => (
                <option key={model.value} value={model.value}>
                  {model.label}
                </option>
              ))}
            </select>
          </label>
          <button
            className="button primary"
            disabled={busy || !dirty || thresholdBad}
          >
            {busy ? "正在保存…" : "保存评测集"}
          </button>
          {dirty && <span className="muted">有未保存的修改</span>}
        </form>
      ) : (
        <p className="muted">
          发布门槛 {set.threshold.toFixed(2)}
          {set.judge_model ? ` · 模型判断用 ${set.judge_model}` : ""}
          {!mayBuild && set.editable && " · 搭建者才能修改评测集"}
        </p>
      )}
      {notice && (
        <p className="asset-notice" role="status">
          {notice}
        </p>
      )}
      {error && <ErrorNotice message={error} />}
      {editing !== null && (
        <CaseDialog
          isNew={editing === "new"}
          initial={
            editing === "new"
              ? {
                  id: nextId(cases),
                  title: null,
                  input: {},
                  answers: {},
                  expect: [],
                  from_run: null,
                }
              : cases[editing]
          }
          taken={cases.filter((_, i) => i !== editing).map((item) => item.id)}
          inputs={inputs}
          humans={humans}
          onClose={() => setEditing(null)}
          onSave={(item) => {
            setCases(
              editing === "new"
                ? [...cases, item]
                : cases.map((c, i) => (i === editing ? item : c)),
            );
            setEditing(null);
          }}
        />
      )}
    </section>
  );
}
