"use client";

import { useT } from "../lib/i18n/client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { rerunFrom } from "../lib/evals";
import {
  msText,
  readFlow,
  stepLabels,
  tokensText,
  type Flow,
  type FlowNode,
  type FlowStep,
} from "../lib/flow";
import { ApiError, errorMessage, isRecord, requestJson } from "../lib/runs";
import { Icon } from "./icons";
import { FlowDiagram } from "./flow-diagram";
import { FlowNodeDesign, NodeNote } from "./flow-node-design";
import { TablePreviews } from "./table-previews";

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="flow-json">{JSON.stringify(value, null, 2)}</pre>;
}

// What an agent did, one line per step; also shown under a chat answer (chat.tsx).
export function AgentSteps({ output }: { output: unknown }) {
  const t = useT();
  if (!isRecord(output) || !Array.isArray(output.steps)) return null;
  if (!output.steps.length)
    return <p className="muted">{t("这一轮没有调用工具。")}</p>;
  return (
    <ol className="agent-steps" aria-label={t("智能体步骤")}>
      {output.steps.filter(isRecord).map((step, i) => (
        <li key={i}>
          {step.tool === "search_knowledge" ? (
            <>
              {t("检索知识库：{query}（命中 {n} 条）", {
                query: isRecord(step.args) ? String(step.args.query ?? "") : "",
                n: typeof step.hits === "number" ? step.hits : 0,
              })}
            </>
          ) : step.tool === "call_app" ? (
            <>
              {t("调用应用「{app}」", { app: String(step.app ?? "") })}
              {typeof step.run_id === "string" && (
                <>
                  {" "}
                  <Link href={`/runs/${encodeURIComponent(step.run_id)}`}>
                    {t("看那次运行")}
                  </Link>
                </>
              )}
            </>
          ) : (
            String(step.tool ?? t("工具调用"))
          )}
          {Boolean(step.error) && (
            <p className="flow-error">{String(step.error)}</p>
          )}
        </li>
      ))}
    </ol>
  );
}

function timeOf(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}

function askedPrompt(interrupt: unknown): string | null {
  return interrupt &&
    typeof interrupt === "object" &&
    "prompt" in interrupt &&
    typeof interrupt.prompt === "string"
    ? interrupt.prompt
    : null;
}

const DEV_KEY = "aep.developerView";

// Rerun the source run from this node, on the app's draft or the run's own configuration
// (docs/api/evals.md, "从某一步重跑").
function RerunFrom({ runId, node }: { runId: string; node: FlowNode }) {
  const t = useT();
  const router = useRouter();
  const [config, setConfig] = useState<"draft" | "same">("draft");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <div className="rerun-from" role="group" aria-label={t("从这一步重跑")}>
      <select
        aria-label={t("用哪份配置重跑")}
        value={config}
        disabled={busy}
        onChange={(e) => setConfig(e.target.value as "draft" | "same")}
      >
        <option value="draft">{t("用应用的草稿")}</option>
        <option value="same">{t("用这次运行的配置")}</option>
      </select>
      <button
        type="button"
        className="button secondary"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError("");
          try {
            const made = await rerunFrom(runId, node.id, config);
            router.push(`/runs/${encodeURIComponent(made.run_id)}`);
          } catch (err) {
            setError(errorMessage(err));
            setBusy(false);
          }
        }}
      >
        {busy ? t("正在创建…") : t("从这一步重跑")}
      </button>
      <small className="muted">
        {t("新运行带着这一步之前的全部状态，从「{title}」开始往下跑。", {
          title: node.title,
        })}
      </small>
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function NodeDetail({
  node,
  steps,
  stepId,
  onStep,
  developer,
  rerunOf,
}: {
  node: FlowNode;
  steps: FlowStep[];
  stepId: string | null;
  onStep: (id: string) => void;
  developer: boolean;
  // The run to rerun from this node, when the person may.
  rerunOf: string | null;
}) {
  const t = useT();
  const runs = steps.filter((step) => step.node === node.id);
  const step = runs.find((s) => s.id === stepId) ?? runs[runs.length - 1];
  return (
    <div
      className="flow-detail"
      aria-label={t("节点 {title}", { title: node.title })}
      role="region"
    >
      {node.kind === "terminal" ? (
        <>
          <h3>{node.title}</h3>
          <NodeNote note={node.design?.note} />
          <p className="flow-summary">
            {node.id === "__start__"
              ? t("运行从这里开始，带着创建运行时填的输入。")
              : t("运行到这里结束，最终结果见运行结果。")}
          </p>
        </>
      ) : (
        <>
          <FlowNodeDesign node={node} />
          <h4>{t("本次运行")}</h4>
          {runs.length === 0 ? (
            <p className="muted flow-note">{t("这次运行没有走到这个节点。")}</p>
          ) : (
            <>
              {runs.length > 1 && (
                <div
                  className="flow-attempts"
                  role="group"
                  aria-label={t("执行次数")}
                >
                  {runs.map((run, i) => (
                    <button
                      type="button"
                      key={run.id}
                      className={run.id === step?.id ? "selected" : ""}
                      aria-pressed={run.id === step?.id}
                      onClick={() => onStep(run.id)}
                    >
                      {t("第 {n} 次 · {status}", {
                        n: i + 1,
                        status: t(stepLabels[run.status]),
                      })}
                    </button>
                  ))}
                </div>
              )}
              {step && (
                <div className="flow-step">
                  <p className="flow-note">
                    <span className={`flow-status ${step.status}`}>
                      {t(stepLabels[step.status])}
                    </span>
                    {step.started_at && (
                      <span>
                        {t("开始于 {time}", { time: timeOf(step.started_at) })}
                      </span>
                    )}
                  </p>
                  {developer && (
                    <p className="flow-note step-usage">
                      {t("耗时 {time}", { time: msText(step.duration_ms, t) })}
                      {step.duration_ms != null &&
                        !step.finished_at &&
                        t("（还没结束）")}
                      {" · "}
                      {tokensText(step.usage, t)}
                    </p>
                  )}
                  {developer &&
                    rerunOf &&
                    node.type &&
                    node.type !== "start" && (
                      <RerunFrom runId={rerunOf} node={node} />
                    )}
                  {askedPrompt(step.interrupt) && (
                    <p className="flow-asked">
                      {t("等待处理：{prompt}", {
                        prompt: askedPrompt(step.interrupt) ?? "",
                      })}
                    </p>
                  )}
                  {step.error && <p className="flow-error">{step.error}</p>}
                  {node.type === "agent" && <AgentSteps output={step.output} />}
                  <details open>
                    <summary>{t("输出（这一步写回的内容）")}</summary>
                    {step.output == null ? (
                      <p className="muted flow-note">
                        {step.status === "waiting"
                          ? t("等人做出决定后才有输出。")
                          : t("还没有输出。")}
                      </p>
                    ) : (
                      <>
                        {(node.type === "databricks_dev_run" ||
                          node.id === "dev_run") && (
                          <TablePreviews
                            output={nodeOutput(step.output, node.id)}
                          />
                        )}
                        {node.type === "databricks_release" && (
                          <ReleaseOutput
                            output={nodeOutput(step.output, node.id)}
                          />
                        )}
                        {node.type === "app" && (
                          <AppCallOutput
                            output={nodeOutput(step.output, node.id)}
                          />
                        )}
                        {node.type === "git_pr" && (
                          <PullRequestOutput
                            output={nodeOutput(step.output, node.id)}
                          />
                        )}
                        <JsonBlock value={step.output} />
                      </>
                    )}
                  </details>
                  <details>
                    <summary>{t("输入（这一步开始时看到的状态）")}</summary>
                    <JsonBlock value={step.input} />
                  </details>
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

function totalUsage(steps: FlowStep[]) {
  const used = steps.filter((s) => s.usage).map((s) => s.usage!);
  if (!used.length) return null;
  const costs = used.filter((u) => u.cost_usd != null);
  return {
    calls: used.reduce((n, u) => n + u.calls, 0),
    prompt_tokens: used.reduce((n, u) => n + u.prompt_tokens, 0),
    completion_tokens: used.reduce((n, u) => n + u.completion_tokens, 0),
    cost_usd: costs.length
      ? costs.reduce((n, u) => n + (u.cost_usd ?? 0), 0)
      : null,
  };
}

// A flow app's step writes its outputs under vars.<node id> (with what else it changed
// in the run's state); the code app's steps and the mock write them as they are.
function nodeOutput(output: unknown, node: string): unknown {
  if (isRecord(output) && isRecord(output.vars) && isRecord(output.vars[node]))
    return output.vars[node];
  return output;
}

// The run an app node started (docs/api/flow-apps.md, "app 调用应用").
function AppCallOutput({ output }: { output: unknown }) {
  const t = useT();
  if (!isRecord(output) || typeof output.run_id !== "string") return null;
  return (
    <div
      className="release-output passed"
      role="group"
      aria-label={t("调用的运行")}
    >
      <p>{t("被调用的运行已完成。")}</p>
      <Link href={`/runs/${encodeURIComponent(output.run_id)}`}>
        {t("查看被调用的运行")}
      </Link>
    </div>
  );
}

// The pull request a 提交 PR step opened or updated (docs/api/git.md).
function PullRequestOutput({ output }: { output: unknown }) {
  const t = useT();
  if (!isRecord(output) || typeof output.url !== "string") return null;
  // The address comes from the connector: only a web link is made a link.
  const url = /^https?:\/\//i.test(output.url) ? output.url : null;
  const changed = Array.isArray(output.changed) ? output.changed.length : 0;
  return (
    <div
      className="release-output passed"
      role="group"
      aria-label={t("提交的 PR")}
    >
      <p>
        {typeof output.summary === "string" ? output.summary : t("已提交 PR")}
        {changed ? t("（改动 {changed} 个文件）", { changed }) : ""}
      </p>
      {url && (
        <a href={url} target="_blank" rel="noreferrer">
          {t("打开 PR #{n} ↗", { n: String(output.number) })}
        </a>
      )}
    </div>
  );
}

// What a release did (docs/api/data-nodes.md, "databricks_release"): the job and its
// schedule, or why nothing was released.
function ReleaseOutput({ output }: { output: unknown }) {
  const t = useT();
  if (!isRecord(output) || typeof output.summary !== "string") return null;
  const url = typeof output.url === "string" ? output.url : null;
  return (
    <div
      className={`release-output ${output.ok === true ? "passed" : "failed"}`}
      role="group"
      aria-label={t("上线结果")}
    >
      <p>{output.summary}</p>
      {url && (
        <a href={url} target="_blank" rel="noreferrer">
          {t("在 Databricks 上打开 Job ↗")}
        </a>
      )}
    </div>
  );
}

export function RunFlow({
  runId,
  version,
  live,
  rerun = false,
  traceUrl = null,
}: {
  runId: string;
  // Changes whenever the run moves on, so the flow is fetched again.
  version: string;
  live: boolean;
  // Whether the person may rerun a flow run from one of its steps.
  rerun?: boolean;
  traceUrl?: string | null;
}) {
  const t = useT();
  const [developer, setDeveloper] = useState(false);
  const [other, setOther] = useState("");
  useEffect(() => {
    // Per viewer: a builder who turned it on sees it on every run.
    const frame = requestAnimationFrame(() => {
      try {
        setDeveloper(window.localStorage.getItem(DEV_KEY) === "on");
      } catch {
        // No storage (a private window): the view starts off.
      }
    });
    return () => cancelAnimationFrame(frame);
  }, []);
  function toggle(on: boolean) {
    setDeveloper(on);
    try {
      window.localStorage.setItem(DEV_KEY, on ? "on" : "off");
    } catch {
      // Kept for this page only.
    }
  }
  const [flow, setFlow] = useState<Flow | null>(null);
  const [error, setError] = useState("");
  // The backend has no flow for this run (an older API): leave the panel out.
  const [missing, setMissing] = useState(false);
  const [retry, setRetry] = useState(0);
  const [picked, setPicked] = useState<{
    node: string;
    step: string | null;
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const value = readFlow(
          await requestJson(`/runs/${encodeURIComponent(runId)}/flow`, {
            signal: controller.signal,
          }),
        );
        if (controller.signal.aborted) return;
        setFlow(value);
        setError("");
        setMissing(false);
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(errorMessage(err));
        setMissing(err instanceof ApiError && err.status === 404);
      } finally {
        // Steps without a message (a retry, a check) still show up while the run is going.
        if (live && !controller.signal.aborted)
          timer = setTimeout(() => void load(), 3_000);
      }
    }
    void load();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [runId, version, live, retry]);

  if (missing && !flow) return null;
  if (!flow) {
    return (
      <section className="panel run-flow" aria-label={t("工作流程")}>
        {error ? (
          // The flow only explains the run; failing to load it must not look like the run failed.
          <p className="panel-padding muted flow-note">
            {t("暂时无法加载工作流程：{error}", { error })}
            <button
              type="button"
              className="text-button"
              onClick={() => setRetry((v) => v + 1)}
            >
              {t("重新加载流程")}
            </button>
          </p>
        ) : (
          <div className="loading-state" role="status">
            <span className="spinner" />

            {t("正在加载工作流程…")}
          </div>
        )}
      </section>
    );
  }

  const { steps, current } = flow;
  const visits = new Map<string, number>();
  for (const step of steps)
    visits.set(step.node, (visits.get(step.node) ?? 0) + 1);
  const walked = new Set<string>();
  if (steps.length > 0) {
    walked.add(`__start__>${steps[0].node}`);
    if (!current) walked.add(`${steps[steps.length - 1].node}>__end__`);
  }
  steps
    .slice(1)
    .forEach((step, i) => walked.add(`${steps[i].node}>${step.node}`));
  const last = steps[steps.length - 1];
  const currentStatus = current && last?.node === current ? last.status : null;

  const fallback =
    current ?? last?.node ?? flow.nodes.find((n) => n.kind !== "terminal")?.id;
  const selectedId =
    picked && flow.nodes.some((n) => n.id === picked.node)
      ? picked.node
      : fallback;
  const selected = flow.nodes.find((node) => node.id === selectedId);
  const select = (node: string, step: string | null = null) =>
    setPicked({ node, step });

  return (
    <section className="panel run-flow" aria-labelledby="run-flow-title">
      <div className="section-heading">
        <h2 id="run-flow-title">
          <Icon name="grid" />

          {t("工作流程")}
        </h2>
        <span className="muted">
          {t("{nodes} 个节点 · 本次走了 {steps} 步", {
            nodes: flow.nodes.length - 2,
            steps: steps.length,
          })}
        </span>
        <label className="developer-toggle">
          <input
            type="checkbox"
            checked={developer}
            onChange={(e) => toggle(e.target.checked)}
          />

          {t("开发者视图")}
        </label>
      </div>
      {developer && (
        <div className="developer-bar" aria-label={t("开发者视图")}>
          <span>
            {t("合计耗时 {time}", {
              time: msText(
                steps.some((s) => s.duration_ms != null)
                  ? steps.reduce((sum, s) => sum + (s.duration_ms ?? 0), 0)
                  : null,
                t,
              ),
            })}
            {" · "}
            {tokensText(totalUsage(steps), t)}
          </span>
          {traceUrl && (
            <a target="_blank" rel="noopener noreferrer" href={traceUrl}>
              {t("在 Langfuse 中看每次模型调用 ↗")}
            </a>
          )}
          <form
            className="compare-form"
            onSubmit={(event) => event.preventDefault()}
          >
            <input
              aria-label={t("要对比的运行编号")}
              placeholder={t("另一次运行的编号")}
              value={other}
              onChange={(e) => setOther(e.target.value.trim())}
            />
            {other ? (
              <Link
                className="button secondary"
                href={`/runs/compare?a=${encodeURIComponent(runId)}&b=${encodeURIComponent(other)}`}
              >
                {t("对比两次运行")}
              </Link>
            ) : (
              <button className="button secondary" type="button" disabled>
                {t("对比两次运行")}
              </button>
            )}
          </form>
        </div>
      )}
      {flow.design && (
        <div className="flow-about">
          <p>
            <strong>{flow.design.title}</strong>
            {flow.design.summary}
          </p>
          {(flow.design.pattern ||
            flow.design.knowledge ||
            flow.design.skills) && (
            <dl>
              {(
                [
                  [t("运行方式"), flow.design.pattern],
                  [t("知识库"), flow.design.knowledge],
                  ["Skill", flow.design.skills],
                ] as const
              )
                .filter(([, text]) => text)
                .map(([label, text]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{text}</dd>
                  </div>
                ))}
            </dl>
          )}
        </div>
      )}
      {steps.length > 0 && (
        <ol className="flow-path" aria-label={t("本次运行的步骤")}>
          {steps.map((step, i) => {
            const node = flow.nodes.find((n) => n.id === step.node);
            return (
              <li key={step.id}>
                <button
                  type="button"
                  className={`flow-status ${step.status} ${
                    selectedId === step.node &&
                    (picked?.step ?? null) === step.id
                      ? "selected"
                      : ""
                  }`}
                  onClick={() => select(step.node, step.id)}
                >
                  {i + 1}. {node?.title ?? step.node}
                  {developer && step.duration_ms != null && (
                    <small className="step-cost">
                      {" · "}
                      {msText(step.duration_ms)}
                      {step.usage &&
                        ` · ${(step.usage.prompt_tokens + step.usage.completion_tokens).toLocaleString("en-US")} tokens`}
                    </small>
                  )}
                </button>
              </li>
            );
          })}
        </ol>
      )}
      <div className="flow-body">
        <FlowDiagram
          flow={flow}
          selectedId={selectedId}
          select={select}
          visits={visits}
          walked={walked}
          current={current}
          currentStatus={currentStatus}
          started={steps.length > 0}
        />
        {selected && (
          <NodeDetail
            node={selected}
            steps={steps}
            stepId={picked?.node === selected.id ? picked.step : null}
            onStep={(id) => select(selected.id, id)}
            developer={developer}
            rerunOf={rerun ? runId : null}
          />
        )}
      </div>
    </section>
  );
}
