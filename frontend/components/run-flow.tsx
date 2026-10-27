"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  kindLabels,
  readFlow,
  stepLabels,
  type Flow,
  type FlowEdge,
  type FlowNode,
  type FlowStep,
} from "../lib/flow";
import { ApiError, errorMessage, requestJson } from "../lib/runs";
import { Icon } from "./icons";

const NODE_W = 184;
const NODE_H = 46;
const TERMINAL_W = 76;
const ROW = 68;
const TOP = 14;
const LANE = 14;
const GAP = 22;
const RADIUS = 6;

type Side = "left" | "right";
type Route =
  | { kind: "down"; edge: FlowEdge; from: number; to: number }
  | { kind: "self"; edge: FlowEdge; from: number; to: number }
  | { kind: Side; edge: FlowEdge; from: number; to: number; lane: number };

// Nodes go in one column in the app's order. An edge to the next node is a straight
// arrow; one that goes back (a retry) runs in a lane on the left, one that skips ahead
// runs on the right. Lanes are shared by edges whose rows do not overlap.
function layout(flow: Flow) {
  const index = new Map(flow.nodes.map((node, i) => [node.id, i]));
  const routes: Route[] = [];
  const sided: { edge: FlowEdge; from: number; to: number; side: Side }[] = [];
  for (const edge of flow.edges) {
    const from = index.get(edge.source);
    const to = index.get(edge.target);
    if (from === undefined || to === undefined) continue;
    if (from === to) routes.push({ kind: "self", edge, from, to });
    else if (to === from + 1) routes.push({ kind: "down", edge, from, to });
    else sided.push({ edge, from, to, side: to < from ? "left" : "right" });
  }
  const taken: Record<Side, [number, number][][]> = {
    left: [],
    right: [
      routes
        .filter((r) => r.kind === "self")
        .map((r) => [r.from - 0.4, r.from + 0.4]),
    ],
  };
  sided.sort((a, b) => Math.abs(a.to - a.from) - Math.abs(b.to - b.from));
  for (const item of sided) {
    const lo = Math.min(item.from, item.to);
    const hi = Math.max(item.from, item.to);
    const lanes = taken[item.side];
    let lane = lanes.findIndex((spans) =>
      spans.every(([a, b]) => b <= lo || a >= hi),
    );
    if (lane === -1) lane = lanes.push([]) - 1;
    lanes[lane].push([lo, hi]);
    routes.push({ ...item, kind: item.side, lane });
  }
  const left = GAP + taken.left.length * LANE;
  const width = left + NODE_W + GAP + Math.max(taken.right.length, 1) * LANE;
  const height = TOP * 2 + (flow.nodes.length - 1) * ROW + NODE_H;
  return { routes, left, width, height };
}

function nodeBox(node: FlowNode, index: number, left: number) {
  const w = node.kind === "terminal" ? TERMINAL_W : NODE_W;
  return { x: left + (NODE_W - w) / 2, y: TOP + index * ROW, w };
}

function routePath(route: Route, flow: Flow, left: number): string {
  const a = nodeBox(flow.nodes[route.from], route.from, left);
  const b = nodeBox(flow.nodes[route.to], route.to, left);
  const cx = left + NODE_W / 2;
  if (route.kind === "down") return `M ${cx} ${a.y + NODE_H} V ${b.y - 2}`;
  if (route.kind === "self") {
    const x = a.x + a.w;
    const y = a.y + NODE_H / 2;
    return `M ${x} ${y - 9} C ${x + 30} ${y - 12} ${x + 30} ${y + 12} ${x + 2} ${y + 9}`;
  }
  // Leave below the middle, arrive above it, so edges at one node do not meet.
  const ys = a.y + NODE_H / 2 + 7;
  const yt = b.y + NODE_H / 2 - 7;
  const dir = route.kind === "left" ? -1 : 1;
  const x0 = route.kind === "left" ? a.x : a.x + a.w;
  const x1 = route.kind === "left" ? b.x : b.x + b.w;
  const lane =
    route.kind === "left"
      ? left - 12 - route.lane * LANE
      : left + NODE_W + 12 + route.lane * LANE;
  const dy = yt > ys ? 1 : -1;
  return [
    `M ${x0} ${ys}`,
    `H ${lane - dir * RADIUS}`,
    `Q ${lane} ${ys} ${lane} ${ys + dy * RADIUS}`,
    `V ${yt - dy * RADIUS}`,
    `Q ${lane} ${yt} ${lane - dir * RADIUS} ${yt}`,
    `H ${x1 + dir * 2}`,
  ].join(" ");
}

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
  const design = node.design;
  const runs = steps.filter((step) => step.node === node.id);
  const step = runs.find((s) => s.id === stepId) ?? runs[runs.length - 1];
  const facts: [string, ReactNode][] = design
    ? [
        ["读取", design.inputs],
        ["产出", design.outputs],
        [
          "工具",
          design.tools.length > 0 ? (
            <ul>
              {design.tools.map((tool) => (
                <li key={tool}>{tool}</li>
              ))}
            </ul>
          ) : null,
        ],
        ["模型", node.model ? <code>{node.model}</code> : null],
        ["知识来源", design.knowledge],
        ["校验", design.checks],
        ["重试", design.retry],
      ]
    : [];
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
          <div className="flow-detail-heading">
            <span className={`flow-kind ${node.kind}`}>
              {kindLabels[node.kind]}
            </span>
            <code>{node.id}</code>
          </div>
          <h3>{node.title}</h3>
          {design ? (
            <p className="flow-summary">{design.summary}</p>
          ) : (
            <p className="flow-summary muted">这个节点没有填写设计说明。</p>
          )}
          <dl className="flow-facts">
            {facts
              .filter(([, value]) => value)
              .map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
          </dl>
          {design?.prompt && (
            <details className="flow-prompt">
              <summary>查看提示词全文</summary>
              <pre className="flow-json">{design.prompt}</pre>
            </details>
          )}
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

  const shape = useMemo(() => (flow ? layout(flow) : null), [flow]);
  if (missing && !flow) return null;
  if (!flow || !shape) {
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
        <div className="flow-canvas">
          <svg
            width={shape.width}
            height={shape.height}
            viewBox={`0 0 ${shape.width} ${shape.height}`}
            role="group"
            aria-label="流程图"
          >
            <defs>
              <marker
                id="flow-arrow"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" className="flow-arrow" />
              </marker>
              <marker
                id="flow-arrow-on"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" className="flow-arrow on" />
              </marker>
            </defs>
            {shape.routes.map((route) => {
              const on = walked.has(
                `${route.edge.source}>${route.edge.target}`,
              );
              return (
                <path
                  key={`${route.edge.source}>${route.edge.target}`}
                  d={routePath(route, flow, shape.left)}
                  className={`flow-edge ${on ? "on" : ""} ${
                    route.edge.conditional ? "conditional" : ""
                  }`}
                  markerEnd={`url(#${on ? "flow-arrow-on" : "flow-arrow"})`}
                />
              );
            })}
            {flow.nodes.map((node, i) => {
              const box = nodeBox(node, i, shape.left);
              const count = visits.get(node.id) ?? 0;
              const visited =
                count > 0 ||
                (node.id === "__start__" && steps.length > 0) ||
                (node.id === "__end__" && !current && steps.length > 0);
              const state =
                node.id === current ? `current ${currentStatus ?? ""}` : "";
              return (
                <g
                  key={node.id}
                  className={`flow-node ${node.kind} ${visited ? "visited" : ""} ${state} ${
                    node.id === selectedId ? "selected" : ""
                  }`}
                  transform={`translate(${box.x} ${box.y})`}
                  role="button"
                  tabIndex={0}
                  aria-label={`${node.title}${node.kind !== "terminal" ? `，${kindLabels[node.kind]}` : ""}${
                    count > 0 ? `，执行 ${count} 次` : ""
                  }`}
                  aria-pressed={node.id === selectedId}
                  onClick={() => select(node.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      select(node.id);
                    }
                  }}
                >
                  <rect
                    width={box.w}
                    height={NODE_H}
                    rx={node.kind === "terminal" ? 23 : 8}
                  />
                  {node.kind === "terminal" ? (
                    <text x={box.w / 2} y={NODE_H / 2 + 4} textAnchor="middle">
                      {node.title}
                    </text>
                  ) : (
                    <>
                      <rect
                        className="flow-accent"
                        width={4}
                        height={NODE_H}
                        rx={2}
                      />
                      <text className="flow-title" x={16} y={20}>
                        {node.title}
                      </text>
                      <text className="flow-sub" x={16} y={36}>
                        {kindLabels[node.kind]}
                        {node.model ? ` · ${node.model}` : ""}
                      </text>
                      {count > 1 && (
                        <text
                          className="flow-count"
                          x={box.w - 12}
                          y={20}
                          textAnchor="end"
                        >
                          ×{count}
                        </text>
                      )}
                    </>
                  )}
                </g>
              );
            })}
          </svg>
          <ul className="flow-legend" aria-label="图例">
            {(["tool", "model", "human", "code"] as const).map((kind) => (
              <li key={kind}>
                <span className={`flow-swatch ${kind}`} />
                {kindLabels[kind]}
              </li>
            ))}
            <li>
              <span className="flow-swatch dashed" />
              按条件走
            </li>
          </ul>
        </div>
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
