"use client";

import { useEffect, useState } from "react";
import {
  readFlow,
  stepLabels,
  type Flow,
  type FlowNode,
  type FlowStep,
} from "../lib/flow";
import { ApiError, errorMessage, requestJson } from "../lib/runs";
import { Icon } from "./icons";
import { FlowDiagram } from "./flow-diagram";
import { FlowNodeDesign } from "./flow-node-design";

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="flow-json">{JSON.stringify(value, null, 2)}</pre>;
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

function NodeDetail({
  node,
  steps,
  stepId,
  onStep,
}: {
  node: FlowNode;
  steps: FlowStep[];
  stepId: string | null;
  onStep: (id: string) => void;
}) {
  const runs = steps.filter((step) => step.node === node.id);
  const step = runs.find((s) => s.id === stepId) ?? runs[runs.length - 1];
  return (
    <div
      className="flow-detail"
      aria-label={`节点 ${node.title}`}
      role="region"
    >
      {node.kind === "terminal" ? (
        <>
          <h3>{node.title}</h3>
          <p className="flow-summary">
            {node.id === "__start__"
              ? "运行从这里开始，带着创建运行时填的输入。"
              : "运行到这里结束，最终结果见运行结果。"}
          </p>
        </>
      ) : (
        <>
          <FlowNodeDesign node={node} />
          <h4>本次运行</h4>
          {runs.length === 0 ? (
            <p className="muted flow-note">这次运行没有走到这个节点。</p>
          ) : (
            <>
              {runs.length > 1 && (
                <div
                  className="flow-attempts"
                  role="group"
                  aria-label="执行次数"
                >
                  {runs.map((run, i) => (
                    <button
                      type="button"
                      key={run.id}
                      className={run.id === step?.id ? "selected" : ""}
                      aria-pressed={run.id === step?.id}
                      onClick={() => onStep(run.id)}
                    >
                      第 {i + 1} 次 · {stepLabels[run.status]}
                    </button>
                  ))}
                </div>
              )}
              {step && (
                <div className="flow-step">
                  <p className="flow-note">
                    <span className={`flow-status ${step.status}`}>
                      {stepLabels[step.status]}
                    </span>
                    {step.started_at && (
                      <span>开始于 {timeOf(step.started_at)}</span>
                    )}
                  </p>
                  {askedPrompt(step.interrupt) && (
                    <p className="flow-asked">
                      向人提问：{askedPrompt(step.interrupt)}
                    </p>
                  )}
                  {step.error && <p className="flow-error">{step.error}</p>}
                  <details open>
                    <summary>输出（这一步写回的内容）</summary>
                    {step.output == null ? (
                      <p className="muted flow-note">
                        {step.status === "waiting"
                          ? "等人做出决定后才有输出。"
                          : "还没有输出。"}
                      </p>
                    ) : (
                      <JsonBlock value={step.output} />
                    )}
                  </details>
                  <details>
                    <summary>输入（这一步开始时看到的状态）</summary>
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

export function RunFlow({
  runId,
  version,
  live,
}: {
  runId: string;
  // Changes whenever the run moves on, so the flow is fetched again.
  version: string;
  live: boolean;
}) {
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
      <section className="panel run-flow" aria-label="工作流程">
        {error ? (
          // The flow only explains the run; failing to load it must not look like the run failed.
          <p className="panel-padding muted flow-note">
            暂时无法加载工作流程：{error}
            <button
              type="button"
              className="text-button"
              onClick={() => setRetry((v) => v + 1)}
            >
              重新加载流程
            </button>
          </p>
        ) : (
          <div className="loading-state" role="status">
            <span className="spinner" />
            正在加载工作流程…
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
          工作流程
        </h2>
        <span className="muted">
          {flow.nodes.length - 2} 个节点 · 本次走了 {steps.length} 步
        </span>
      </div>
      {flow.design && (
        <div className="flow-about">
          <p>
            <strong>{flow.design.title}</strong>
            {flow.design.summary}
          </p>
          <dl>
            <div>
              <dt>运行方式</dt>
              <dd>{flow.design.pattern}</dd>
            </div>
            <div>
              <dt>知识库</dt>
              <dd>{flow.design.knowledge}</dd>
            </div>
            <div>
              <dt>Skill</dt>
              <dd>{flow.design.skills}</dd>
            </div>
          </dl>
        </div>
      )}
      {steps.length > 0 && (
        <ol className="flow-path" aria-label="本次运行的步骤">
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
          />
        )}
      </div>
    </section>
  );
}
