"use client";

import { useId, useMemo } from "react";
import {
  kindLabels,
  type Flow,
  type FlowEdge,
  type FlowNode,
  type StepStatus,
} from "../lib/flow";

type FlowGraph = Pick<Flow, "nodes" | "edges">;

const NODE_W = 184;
const NODE_H = 46;
const TERMINAL_W = 76;
const ROW = 80;
const TOP = 14;
const LANE = 34;
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
function layout(flow: FlowGraph) {
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
  const hasLabels = flow.edges.some((edge) => edge.label);
  const left = GAP + taken.left.length * LANE + (hasLabels ? 36 : 0);
  const width =
    left +
    NODE_W +
    GAP +
    Math.max(taken.right.length, 1) * LANE +
    (hasLabels ? 52 : 0);
  const height = TOP * 2 + (flow.nodes.length - 1) * ROW + NODE_H;
  return { routes, left, width, height };
}

// A flow app has its own start and end nodes, so the graph's __start__ and __end__
// would only draw a second "开始" and "结束".
export function withoutGraphEnds(flow: FlowGraph): FlowGraph {
  if (!flow.nodes.some((node) => node.type === "start")) return flow;
  const ends = new Set(["__start__", "__end__"]);
  return {
    nodes: flow.nodes.filter((node) => !ends.has(node.id)),
    edges: flow.edges.filter(
      (edge) => !ends.has(edge.source) && !ends.has(edge.target),
    ),
  };
}

function nodeBox(node: FlowNode, index: number, left: number) {
  const w = node.kind === "terminal" ? TERMINAL_W : NODE_W;
  return { x: left + (NODE_W - w) / 2, y: TOP + index * ROW, w };
}

function routePath(route: Route, flow: FlowGraph, left: number): string {
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

export function FlowDiagram({
  flow,
  selectedId,
  select,
  visits = new Map(),
  walked = new Set(),
  current = null,
  currentStatus = null,
  started = false,
  designMode = false,
}: {
  flow: FlowGraph;
  selectedId?: string;
  select: (node: string) => void;
  visits?: Map<string, number>;
  walked?: Set<string>;
  current?: string | null;
  currentStatus?: StepStatus | null;
  started?: boolean;
  designMode?: boolean;
}) {
  const graph = useMemo(() => withoutGraphEnds(flow), [flow]);
  const shape = useMemo(() => layout(graph), [graph]);
  const arrowId = useId();
  return (
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
            id={arrowId}
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
            id={`${arrowId}-on`}
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
        {shape.routes.map((route, index) => {
          const on = walked.has(`${route.edge.source}>${route.edge.target}`);
          const a = nodeBox(graph.nodes[route.from], route.from, shape.left);
          const b = nodeBox(graph.nodes[route.to], route.to, shape.left);
          const labelX =
            route.kind === "down"
              ? shape.left + NODE_W / 2 + 7
              : route.kind === "left"
                ? shape.left - 18 - route.lane * LANE
                : route.kind === "right"
                  ? shape.left + NODE_W + 18 + route.lane * LANE
                  : a.x + a.w + 20;
          const labelY =
            route.kind === "down"
              ? (a.y + NODE_H + b.y) / 2 + 3
              : route.kind === "self"
                ? a.y + 10
                : a.y + NODE_H / 2 + 2;
          return (
            <g key={`${route.edge.source}>${route.edge.target}:${index}`}>
              <path
                d={routePath(route, graph, shape.left)}
                className={`flow-edge ${on ? "on" : ""} ${
                  route.edge.conditional ? "conditional" : ""
                }`}
                markerEnd={`url(#${on ? `${arrowId}-on` : arrowId})`}
              />
              {route.edge.label && (
                <text
                  className="flow-edge-label"
                  x={labelX}
                  y={labelY}
                  textAnchor={route.kind === "left" ? "end" : "start"}
                >
                  {route.edge.label}
                </text>
              )}
            </g>
          );
        })}
        {graph.nodes.map((node, i) => {
          const box = nodeBox(node, i, shape.left);
          const count = visits.get(node.id) ?? 0;
          const visited =
            designMode ||
            count > 0 ||
            (node.id === "__start__" && started) ||
            (node.id === "__end__" && !current && started);
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
  );
}
