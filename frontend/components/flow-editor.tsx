"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  getNodeTypes,
  NODE_ID_PATTERN,
  testAppNode,
  type AppDetail,
  type FlowConfig,
  type FlowNode,
  type NodeTest,
  type NodeType,
  type OnError,
} from "../lib/apps";
import {
  branchesOf,
  branchLabel,
  canFail,
  deleteNode,
  diagramOf,
  freeId,
  impactOf,
  insertNode,
  issuesUnder,
  newNode,
  ownOutputs,
  referencesIn,
  renameBranch,
  renameNode,
  replaceNode,
  setNext,
  targetOf,
  typeLabels,
  UNTESTABLE_TYPES,
  type FlowIssues,
} from "../lib/flow-edit";
import {
  initialValues,
  inputPayload,
  validateFields,
  type FieldValues,
} from "../lib/input-fields";
import { createRun, errorMessage, fieldErrors } from "../lib/runs";
import { AddNodePanel, type Pending } from "./flow-add-node";
import {
  FlowCanvas,
  type CanvasActions,
  type AddPosition,
} from "./flow-canvas";
import { FlowDiagram } from "./flow-diagram";
import { NumberField, SelectField, TextField } from "./flow-fields";
import { NodeSettings, type Change } from "./flow-node-form";
import { InputFields } from "./input-fields";

// Settings every node has, rendered by the editor itself rather than the type's form.
const commonKeys = ["id", "title", "max_visits", "on_error", "ui"];
const formKeys: Record<FlowNode["type"], string[]> = {
  start: ["inputs"],
  llm: ["model", "skills", "system", "prompt", "output"],
  classifier: ["model", "skills", "text", "instruction", "classes"],
  condition: ["rules"],
  knowledge: ["knowledge_bases", "query", "top_k", "min_score"],
  tool: ["datasource", "tool", "args"],
  agent: ["model", "skills", "task", "tools", "max_steps"],
  human: ["mode", "prompt", "show", "options", "comment_required", "fields"],
  template: ["template"],
  code: ["code", "inputs", "outputs", "timeout_seconds"],
  end: ["outputs"],
};

function ErrorHandling({ node, change }: { node: FlowNode; change: Change }) {
  if (!canFail(node)) return null;
  const current: OnError = ("on_error" in node && node.on_error) || {
    then: "stop",
  };
  const outputs = ownOutputs(node);
  const typed = Object.fromEntries(
    (node.type === "code"
      ? node.outputs
      : node.type === "llm" && node.output.kind === "json"
        ? node.output.fields
        : []
    ).map((field) => [field.key, field.type]),
  );
  function set(next: OnError) {
    const keep = next.then !== "stop" || (next.retry && next.retry.max > 0);
    const updated = { ...node } as FlowNode & { on_error?: OnError };
    if (keep) updated.on_error = next;
    else delete updated.on_error;
    change(updated);
  }
  const retry = current.retry ?? { max: 0, interval_seconds: 1 };
  const withRetry = (value: typeof retry) => {
    const rest = { ...current };
    delete rest.retry;
    return value.max > 0 ? { ...rest, retry: value } : rest;
  };
  const defaults = current.then === "default" ? current.default : {};
  return (
    <fieldset className="list-editor" aria-label="出错时">
      <legend>出错时</legend>
      <NumberField
        label="重试次数"
        min={0}
        max={3}
        value={retry.max}
        onChange={(max) => set(withRetry({ ...retry, max }) as OnError)}
      />
      {retry.max > 0 && (
        <NumberField
          label="重试间隔秒数"
          min={0}
          max={60}
          value={retry.interval_seconds}
          onChange={(interval_seconds) =>
            set(withRetry({ ...retry, interval_seconds }) as OnError)
          }
        />
      )}
      <SelectField
        label="重试后仍失败"
        value={current.then}
        choices={[
          { value: "stop", label: "停止运行" },
          { value: "default", label: "用默认值继续" },
          { value: "branch", label: "走“出错”分支" },
        ]}
        onChange={(then) => {
          const base = current.retry ? { retry: current.retry } : {};
          set(
            then === "default"
              ? {
                  ...base,
                  then: "default",
                  default: Object.fromEntries(
                    outputs.map((key) => [key, defaults[key] ?? ""]),
                  ),
                }
              : { ...base, then: then as "stop" | "branch" },
          );
        }}
      />
      {current.then === "default" &&
        outputs.map((key) => (
          <TextField
            key={key}
            label={`默认值：${key}`}
            value={
              typeof defaults[key] === "string"
                ? (defaults[key] as string)
                : JSON.stringify(defaults[key] ?? "")
            }
            onChange={(value) => {
              let parsed: unknown = value;
              if (
                typed[key] === "number" &&
                value.trim() &&
                Number.isFinite(Number(value))
              )
                parsed = Number(value);
              else if (
                typed[key] === "boolean" &&
                (value === "true" || value === "false")
              )
                parsed = value === "true";
              set({
                ...current,
                then: "default",
                default: { ...defaults, [key]: parsed },
              });
            }}
          />
        ))}
      {current.then !== "stop" && (
        <p className="muted">
          出错后，后面的节点可以引用这个节点的 error_type 和 error_message。
        </p>
      )}
    </fieldset>
  );
}

function NodeTestPanel({
  app,
  node,
  config,
}: {
  app: string;
  node: FlowNode;
  config: FlowConfig;
}) {
  const refs = referencesIn(node).filter(
    (ref) => ref.node !== "env" && ref.node !== "secret",
  );
  const [values, setValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<NodeTest | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <section className="node-test" aria-label="试跑这个节点">
      <h4>试跑这个节点</h4>
      <p className="muted">
        用下面的测试值只跑这一个节点，不保存、不建运行记录。用的是页面上还没保存的设置。
      </p>
      {refs.map((ref) => (
        <TextField
          key={`${ref.node}.${ref.field}`}
          label={`{{${ref.node}.${ref.field}}}`}
          value={values[`${ref.node}.${ref.field}`] ?? ""}
          onChange={(value) =>
            setValues({ ...values, [`${ref.node}.${ref.field}`]: value })
          }
        />
      ))}
      {!refs.length && (
        <p className="muted">这个节点没有引用其他节点的变量。</p>
      )}
      <button
        type="button"
        className="button secondary"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError("");
          setResult(null);
          try {
            setResult(await testAppNode(app, node.id, values, config));
          } catch (err) {
            setError(errorMessage(err));
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "试跑中…" : "开始试跑"}
      </button>
      {error && <p className="field-error">{error}</p>}
      {result && (
        <div
          className={`node-test-result ${result.status}`}
          aria-label="试跑结果"
          role="region"
        >
          <strong>
            {result.status === "done" ? "试跑成功" : "试跑出错"} ·{" "}
            {result.duration_ms} 毫秒
            {result.branch
              ? ` · 会走：${branchLabel(node, result.branch)}`
              : ""}
          </strong>
          {result.error && (
            <p className="field-error">
              {result.error.type}：{result.error.message}
            </p>
          )}
          {result.outputs && (
            <>
              <h5>产出</h5>
              <pre className="flow-json">
                {JSON.stringify(result.outputs, null, 2)}
              </pre>
            </>
          )}
          {Object.keys(result.rendered).length > 0 && (
            <>
              <h5>替换变量后的设置</h5>
              <pre className="flow-json">
                {JSON.stringify(result.rendered, null, 2)}
              </pre>
            </>
          )}
        </div>
      )}
    </section>
  );
}

function DraftRun({ app, data }: { app: string; data: AppDetail }) {
  const router = useRouter();
  const config = data.draft?.config;
  const start =
    config && "graph" in config
      ? config.graph.nodes.find((node) => node.type === "start")
      : undefined;
  const fields = start?.type === "start" ? start.inputs : [];
  const [values, setValues] = useState<FieldValues>(() =>
    initialValues(fields),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="draft-run asset-form"
      aria-label="用草稿试运行"
      onSubmit={async (event) => {
        event.preventDefault();
        const issues = validateFields(fields, values, "input");
        setErrors(
          Object.fromEntries(issues.map((issue) => [issue.loc[2], issue.msg])),
        );
        if (issues.length || busy) return;
        setBusy(true);
        setError("");
        try {
          const receipt = await createRun({
            app,
            input: inputPayload(fields, values),
            draft: true,
          });
          router.push(`/runs/${encodeURIComponent(receipt.run_id)}`);
        } catch (err) {
          setErrors(fieldErrors(err, "input"));
          setError(errorMessage(err));
          setBusy(false);
        }
      }}
    >
      <p className="muted">
        用已保存的草稿运行一次，运行记录会标注“草稿试运行”。
      </p>
      <InputFields
        fields={fields}
        values={values}
        onChange={setValues}
        errors={errors}
        prefix="draft-input"
        datasources={data.options.datasources}
        disabled={busy}
      />
      {error && <p className="field-error">{error}</p>}
      <button className="button primary" disabled={busy}>
        {busy ? "正在创建…" : "开始试运行"}
      </button>
    </form>
  );
}

const VIEW_KEY = "aep.flow-editor.view";
function subscribeDesktop(notify: () => void) {
  const media = window.matchMedia("(min-width: 900px)");
  media.addEventListener("change", notify);
  return () => media.removeEventListener("change", notify);
}
function subscribeView(notify: () => void) {
  window.addEventListener("storage", notify);
  return () => window.removeEventListener("storage", notify);
}
function savedView(): "canvas" | "list" {
  try {
    return localStorage.getItem(VIEW_KEY) === "list" ? "list" : "canvas";
  } catch {
    return "canvas";
  }
}

export function FlowEditor({
  app,
  data,
  config,
  setConfig,
  issues,
  dirty,
  disabled,
}: {
  app: string;
  data: AppDetail;
  config: FlowConfig;
  setConfig: (config: FlowConfig) => void;
  issues: FlowIssues;
  dirty: boolean;
  disabled: boolean;
}) {
  const desktop = useSyncExternalStore(
    subscribeDesktop,
    () => window.matchMedia("(min-width: 900px)").matches,
    () => false,
  );
  const storedView = useSyncExternalStore(
    subscribeView,
    savedView,
    () => "canvas" as const,
  );
  const [chosenView, setChosenView] = useState<"canvas" | "list" | null>(null);
  const canvasMode = desktop && (chosenView ?? storedView) === "canvas";
  const canvasRef = useRef<CanvasActions>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [addPosition, setAddPosition] = useState<AddPosition | null>(null);
  const nodes = config.graph.nodes;
  const [selected, setSelected] = useState(nodes[0]?.id ?? "");
  const [types, setTypes] = useState<NodeType[]>([]);
  const [typesError, setTypesError] = useState("");
  const [adding, setAdding] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [removing, setRemoving] = useState("");
  const [idDraft, setIdDraft] = useState<{
    node: string;
    value: string;
  } | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    getNodeTypes(controller.signal)
      .then((value) => !controller.signal.aborted && setTypes(value))
      .catch(
        (err) => !controller.signal.aborted && setTypesError(errorMessage(err)),
      );
    return () => controller.abort();
  }, []);
  const node = nodes.find((item) => item.id === selected) ?? nodes[0];
  const invalid = new Set(Object.keys(issues.nodes));
  const nodeIssues = node ? issues.nodes[node.id] : undefined;
  const runReason = dirty
    ? "有未保存的修改，请先保存草稿。"
    : !data.draft
      ? "保存草稿后可以用草稿试运行。"
      : "";

  const change: Change = (next, rename) => {
    let updated = config;
    if (rename && rename.from !== rename.to)
      updated = renameBranch(updated, next.id, rename.from, rename.to);
    setConfig(replaceNode(updated, next));
  };
  function add(type: NodeType, preset?: NodeType["presets"][number]) {
    const created = newNode(config, type, preset);
    if (canvasMode)
      created.ui = canvasRef.current?.positionFor(created, addPosition?.point);
    let updated = insertNode(config, created, pending?.from ?? node?.id);
    if (pending)
      updated = setNext(updated, pending.from, pending.branch, created.id);
    setConfig(updated);
    setSelected(created.id);
    setPanelOpen(true);
    setAddPosition(null);
    setAdding(false);
    setPending(null);
  }
  function copyNode(item: FlowNode) {
    const point = canvasRef.current?.positionOf(item.id) ?? item.ui;
    const copy = {
      ...structuredClone(item),
      id: freeId(config, item.id),
      title: `${item.title}（副本）`.slice(0, 50),
      ...(point ? { ui: { x: point.x + 40, y: point.y + 40 } } : {}),
    };
    setConfig(insertNode(config, copy, item.id));
    setSelected(copy.id);
    setPanelOpen(true);
  }
  function commitId() {
    if (!idDraft || !node || idDraft.node !== node.id) return;
    const value = idDraft.value.trim();
    if (value === node.id) return setIdDraft(null);
    if (!NODE_ID_PATTERN.test(value) || ["env", "secret"].includes(value))
      return;
    if (nodes.some((item) => item.id === value)) return;
    setConfig(renameNode(config, node.id, value));
    setSelected(value);
    setIdDraft(null);
  }
  const idValue =
    idDraft && node && idDraft.node === node.id
      ? idDraft.value
      : (node?.id ?? "");
  const idIssue =
    idValue === node?.id
      ? ""
      : !NODE_ID_PATTERN.test(idValue) || ["env", "secret"].includes(idValue)
        ? "标识以小写字母开头，只用小写字母、数字和下划线，最多 32 个字符；env、secret 不能用。"
        : nodes.some((item) => item.id === idValue)
          ? "已有节点用了这个标识。"
          : "";
  const removal = removing ? impactOf(config, removing) : null;
  const removingNode = nodes.find((item) => item.id === removing);
  const unplaced = node
    ? Object.entries(nodeIssues ?? {})
        .filter(([key]) => {
          const head = key.split(".")[0];
          return !(
            key.startsWith("next:") ||
            commonKeys.includes(head) ||
            formKeys[node.type].includes(head)
          );
        })
        .map(([key, msg]) => (key ? `${key}：${msg}` : msg))
    : [];

  const removalDialog =
    removal && removingNode ? (
      <div
        className="remove-node"
        role="dialog"
        aria-label={`删除节点「${removingNode.title}」`}
      >
        <strong>删除「{removingNode.title}」？</strong>
        {removal.edges.length > 0 && (
          <>
            <p>会一起删除这些连线：</p>
            <ul>
              {removal.edges.map((edge) => (
                <li key={edge}>{edge}</li>
              ))}
            </ul>
          </>
        )}
        {removal.references.length > 0 && (
          <>
            <p>这些设置引用了它的变量，删除后需要修改：</p>
            <ul>
              {removal.references.map((ref) => (
                <li key={ref}>{ref}</li>
              ))}
            </ul>
          </>
        )}
        {!removal.edges.length && !removal.references.length && (
          <p>没有连线和变量引用受影响。</p>
        )}
        <div className="asset-actions">
          <button
            type="button"
            className="button reject"
            onClick={() => {
              setConfig(deleteNode(config, removing));
              if (selected === removing)
                setSelected(nodes.find((n) => n.id !== removing)?.id ?? "");
              setRemoving("");
            }}
          >
            确认删除
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={() => setRemoving("")}
          >
            取消
          </button>
        </div>
      </div>
    ) : null;

  return (
    <section
      className="panel flow-editor"
      aria-label="编排"
      onKeyDown={(event) => {
        if (
          !canvasMode ||
          adding ||
          removing ||
          !["Delete", "Backspace"].includes(event.key)
        )
          return;
        if (
          event.target instanceof Element &&
          event.target.closest(
            "input, textarea, select, [contenteditable], [role=dialog]",
          )
        )
          return;
        if (canvasRef.current?.deleteSelection()) event.preventDefault();
      }}
    >
      <div className="section-heading">
        <h2>编排</h2>
        <span className="muted">
          {dirty
            ? "有未保存的修改，保存草稿后生效"
            : canvasMode
              ? "拖动出口连线，点击节点修改设置"
              : "增删节点、设置每个节点，用“下一步”连线"}
        </span>
      </div>
      <div className="flow-editor-toolbar">
        {desktop && (
          <div className="flow-view-switch" role="group" aria-label="编排视图">
            {(
              [
                ["canvas", "画布"],
                ["list", "列表"],
              ] as const
            ).map(([view, label]) => (
              <button
                key={view}
                type="button"
                className="text-button"
                aria-pressed={(chosenView ?? storedView) === view}
                onClick={() => {
                  setChosenView(view);
                  setAdding(false);
                  try {
                    localStorage.setItem(VIEW_KEY, view);
                  } catch {
                    /* Storage may be unavailable. */
                  }
                }}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        <button
          type="button"
          className="button secondary"
          disabled={Boolean(runReason) || disabled}
          aria-describedby={runReason ? "draft-run-reason" : undefined}
          aria-expanded={runOpen}
          onClick={() => setRunOpen((value) => !value)}
        >
          用草稿试运行
        </button>
        {runReason && (
          <span className="muted" id="draft-run-reason">
            {runReason}
          </span>
        )}
      </div>
      {runOpen && !runReason && (
        <DraftRun key={data.draft?.created_at} app={app} data={data} />
      )}
      {canvasMode && removalDialog}
      {canvasMode && typesError && (
        <p className="field-error">读取节点类型失败：{typesError}</p>
      )}
      {!canvasMode && (
        <div className="flow-editor-preview">
          <FlowDiagram
            flow={diagramOf(config)}
            selectedId={node?.id}
            select={setSelected}
            invalid={invalid}
            designMode
          />
        </div>
      )}
      <div
        className={
          canvasMode
            ? `flow-editor-body canvas-editor-body${panelOpen && !adding ? " with-node-panel" : ""}`
            : "flow-editor-body"
        }
      >
        {canvasMode && (
          <FlowCanvas
            ref={canvasRef}
            config={config}
            setConfig={setConfig}
            invalid={invalid}
            disabled={disabled}
            selectedId={panelOpen ? (node?.id ?? "") : ""}
            select={(id) => {
              if (id) setSelected(id);
              setPanelOpen(Boolean(id));
            }}
            removeNode={setRemoving}
            canAdd={Boolean(types.length)}
            openAdd={(position) => {
              setAddPosition(position);
              setPending(position.pending);
              setAdding(true);
            }}
            menu={
              adding ? (
                <div
                  className="canvas-add-menu"
                  style={{
                    left: addPosition?.menu.x ?? 16,
                    top: addPosition?.menu.y ?? 16,
                    maxHeight: `min(460px, calc(100% - ${(addPosition?.menu.y ?? 16) + 8}px))`,
                  }}
                >
                  <AddNodePanel
                    types={types}
                    config={config}
                    pending={pending}
                    add={add}
                    close={() => {
                      setAdding(false);
                      setPending(null);
                      setAddPosition(null);
                    }}
                  />
                </div>
              ) : null
            }
          />
        )}
        {!canvasMode && (
          <aside className="node-list" aria-label="节点列表">
            <ul>
              {nodes.map((item) => (
                <li
                  key={item.id}
                  className={`${item.id === node?.id ? "selected" : ""} ${invalid.has(item.id) ? "invalid" : ""}`}
                >
                  <button
                    type="button"
                    className="node-list-select"
                    aria-pressed={item.id === node?.id}
                    aria-label={`${item.title}（${item.id}）${invalid.has(item.id) ? "，有问题" : ""}`}
                    onClick={() => setSelected(item.id)}
                  >
                    <span className={`node-type-badge ${item.type}`}>
                      {typeLabels[item.type]}
                    </span>
                    <strong>{item.title}</strong>
                    <code>{item.id}</code>
                  </button>
                  <span className="node-list-actions">
                    <button
                      type="button"
                      className="text-button"
                      disabled={disabled || item.type === "start"}
                      aria-label={`复制节点 ${item.title}`}
                      onClick={() => copyNode(item)}
                    >
                      复制
                    </button>
                    <button
                      type="button"
                      className="text-button"
                      disabled={disabled}
                      aria-label={`删除节点 ${item.title}`}
                      onClick={() => setRemoving(item.id)}
                    >
                      删除
                    </button>
                  </span>
                </li>
              ))}
            </ul>
            {!canvasMode && removalDialog}
            <button
              type="button"
              className="button secondary add-node-button"
              disabled={disabled || !types.length}
              onClick={() => {
                setPending(null);
                setAdding(true);
              }}
            >
              添加节点
            </button>
            {typesError && (
              <p className="field-error">读取节点类型失败：{typesError}</p>
            )}
          </aside>
        )}
        {adding && !canvasMode ? (
          <AddNodePanel
            types={types}
            config={config}
            pending={pending}
            add={add}
            close={() => {
              setAdding(false);
              setPending(null);
            }}
          />
        ) : node && (!canvasMode || (panelOpen && !adding)) ? (
          <div
            className="node-panel"
            role="region"
            aria-label={`节点设置：${node.title}`}
          >
            <div className="flow-detail-heading">
              <span className={`node-type-badge ${node.type}`}>
                {typeLabels[node.type]}
              </span>
              <h3>{node.title}</h3>
              {canvasMode && (
                <button
                  type="button"
                  className="text-button"
                  aria-label="关闭节点设置"
                  onClick={() => setPanelOpen(false)}
                >
                  关闭
                </button>
              )}
            </div>
            {canvasMode && (
              <div className="asset-actions canvas-node-actions">
                <button
                  type="button"
                  className="text-button"
                  disabled={disabled || node.type === "start"}
                  onClick={() => copyNode(node)}
                >
                  复制
                </button>
                <button
                  type="button"
                  className="text-button"
                  disabled={disabled}
                  onClick={() => setRemoving(node.id)}
                >
                  删除节点
                </button>
              </div>
            )}
            {unplaced.map((msg) => (
              <p className="field-error node-issue" key={msg}>
                {msg}
              </p>
            ))}
            <fieldset className="node-form" disabled={disabled}>
              <TextField
                label="标题"
                value={node.title}
                maxLength={50}
                issues={issuesUnder(nodeIssues, "title")}
                onChange={(title) => change({ ...node, title })}
              />
              <TextField
                label="标识"
                value={idValue}
                issues={[idIssue, ...issuesUnder(nodeIssues, "id")].filter(
                  Boolean,
                )}
                help="改标识时，连线和引用它的变量会一起更新。"
                onChange={(value) => setIdDraft({ node: node.id, value })}
                onBlur={commitId}
              />
              <NodeSettings
                key={node.id}
                node={node}
                config={config}
                options={data.options}
                issues={nodeIssues}
                change={change}
              />
              <ErrorHandling node={node} change={change} />
              {node.type !== "end" && (
                <fieldset className="list-editor next-steps">
                  <legend>下一步</legend>
                  {branchesOf(node).map((branch) => (
                    <SelectField
                      key={branch}
                      label={branchLabel(node, branch)}
                      value={targetOf(config, node.id, branch)}
                      issues={
                        nodeIssues?.[`next:${branch}`]
                          ? [nodeIssues[`next:${branch}`]]
                          : []
                      }
                      onChange={(to) => {
                        if (to === "__new__") {
                          setAddPosition(null);
                          setPending({ from: node.id, branch });
                          setAdding(true);
                        } else setConfig(setNext(config, node.id, branch, to));
                      }}
                      choices={[
                        { value: "", label: "（未连接）" },
                        ...nodes
                          .filter((item) => item.type !== "start")
                          .map((item) => ({
                            value: item.id,
                            label: `${item.title}（${item.id}）`,
                          })),
                        ...(types.length
                          ? [{ value: "__new__", label: "新建节点…" }]
                          : []),
                      ]}
                    />
                  ))}
                </fieldset>
              )}
              <details className="node-advanced">
                <summary>高级</summary>
                <NumberField
                  label="一次运行最多执行次数"
                  min={1}
                  max={10}
                  value={node.max_visits ?? 3}
                  issues={issuesUnder(nodeIssues, "max_visits")}
                  help="循环里的节点超过这个次数时运行失败。"
                  onChange={(max_visits) => change({ ...node, max_visits })}
                />
              </details>
            </fieldset>
            {!UNTESTABLE_TYPES.includes(node.type) && (
              <NodeTestPanel
                key={node.id}
                app={app}
                node={node}
                config={config}
              />
            )}
          </div>
        ) : !canvasMode ? (
          <p className="muted">还没有节点。</p>
        ) : null}
      </div>
    </section>
  );
}
