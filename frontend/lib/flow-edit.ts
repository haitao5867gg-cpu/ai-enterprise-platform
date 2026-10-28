// Pure helpers for the flow editor: outputs, variables, branches and graph edits.
// The rules follow docs/api/flow-apps.md.
import type { Edge, FlowConfig, FlowNode, NodeType } from "./apps";
import type { FlowEdge, FlowNode as DiagramNode, NodeKind } from "./flow";
import type { ValidationIssue } from "./runs";

export const REF_PATTERN =
  /\{\{\s*([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\s*\}\}/g;
export const BRANCHED_ERROR_TYPES = [
  "llm",
  "classifier",
  "tool",
  "agent",
  "code",
];
export const UNTESTABLE_TYPES = ["start", "human", "end"];

export const typeLabels: Record<FlowNode["type"], string> = {
  start: "开始",
  llm: "调用模型",
  classifier: "意图分类",
  condition: "条件判断",
  tool: "调用工具",
  agent: "智能体",
  human: "人工",
  template: "文本拼接",
  code: "代码",
  end: "结束",
};

export function nodeKind(type: FlowNode["type"]): NodeKind {
  if (["llm", "classifier", "agent"].includes(type)) return "model";
  if (type === "tool") return "tool";
  if (type === "human") return "human";
  if (type === "start" || type === "end") return "terminal";
  return "code";
}

export function canFail(node: FlowNode): boolean {
  return BRANCHED_ERROR_TYPES.includes(node.type);
}
function failsOver(node: FlowNode): boolean {
  return (
    "on_error" in node &&
    (node.on_error?.then === "default" || node.on_error?.then === "branch")
  );
}

// The node's own outputs, without error_type / error_message.
export function ownOutputs(node: FlowNode): string[] {
  switch (node.type) {
    case "start":
      return node.inputs.map((item) => item.key);
    case "llm":
      return node.output.kind === "json"
        ? node.output.fields.map((item) => item.key)
        : ["text"];
    case "classifier":
      return ["class", "label"];
    case "condition":
      return ["rule"];
    case "tool":
      return ["result"];
    case "agent":
      return ["text", "steps"];
    case "human":
      return node.mode === "form"
        ? [...node.fields.map((item) => item.key), "actor"]
        : ["decision", "comment", "actor"];
    case "template":
      return ["text"];
    case "code":
      return node.outputs.map((item) => item.key);
    case "end":
      return [];
  }
}
export function outputsOf(node: FlowNode): string[] {
  return failsOver(node)
    ? [...ownOutputs(node), "error_type", "error_message"]
    : ownOutputs(node);
}

// Branch names a node's out-edges use; "" is the plain next step.
export function branchesOf(node: FlowNode): string[] {
  const list =
    node.type === "classifier"
      ? [...node.classes.map((item) => item.key), "other"]
      : node.type === "condition"
        ? [...node.rules.map((item) => item.key), "else"]
        : node.type === "human" && node.mode === "approval"
          ? node.options.map((item) => item.key)
          : node.type === "end"
            ? []
            : [""];
  return "on_error" in node && node.on_error?.then === "branch"
    ? [...list, "error"]
    : list;
}
export function branchLabel(
  node: FlowNode | undefined,
  branch: string,
): string {
  if (!branch) return "下一步";
  if (branch === "error") return "出错";
  if (node?.type === "classifier" && branch === "other") return "都不是";
  if (node?.type === "condition" && branch === "else") return "否则";
  const items =
    node?.type === "classifier"
      ? node.classes
      : node?.type === "condition"
        ? node.rules
        : node?.type === "human" && node.mode === "approval"
          ? node.options
          : [];
  return items.find((item) => item.key === branch)?.label || branch;
}
const branchOf = (edge: Edge) => edge.branch ?? "";

// Nodes reachable from `from` following edges (forward) or against them (back).
export function walk(
  config: FlowConfig,
  from: string[],
  back = false,
): Set<string> {
  const seen = new Set<string>();
  const queue = [...from];
  while (queue.length) {
    const id = queue.shift()!;
    for (const edge of config.graph.edges) {
      const [a, b] = back ? [edge.to, edge.from] : [edge.from, edge.to];
      if (a === id && !seen.has(b)) {
        seen.add(b);
        queue.push(b);
      }
    }
  }
  return seen;
}

// Nodes that can run before `id` on some path from the start node. Inside a loop this
// includes the later nodes of the loop, which the contract allows to be referenced.
export function upstreamOf(config: FlowConfig, id: string): FlowNode[] {
  const start = config.graph.nodes.find((node) => node.type === "start");
  if (!start) return [];
  const reached = walk(config, [start.id]);
  reached.add(start.id);
  const before = walk(config, [id], true);
  return config.graph.nodes.filter(
    (node) => node.id !== id && reached.has(node.id) && before.has(node.id),
  );
}

export type Variable = { ref: string; group: string; field: string };
export function variablesFor(
  config: FlowConfig,
  id: string,
  withSecrets = false,
): Variable[] {
  return [
    ...upstreamOf(config, id).flatMap((node) =>
      outputsOf(node).map((field) => ({
        ref: `{{${node.id}.${field}}}`,
        group: `${node.title}（${node.id}）`,
        field,
      })),
    ),
    ...config.env.vars.map((item) => ({
      ref: `{{env.${item.key}}}`,
      group: "常量",
      field: item.key,
    })),
    ...(withSecrets
      ? config.env.secrets.map((name) => ({
          ref: `{{secret.${name}}}`,
          group: "密钥",
          field: name,
        }))
      : []),
  ];
}

type Path = (string | number)[];
// Every string in a node's settings with its path; the code node's code is verbatim.
export function stringsOf(node: FlowNode): { path: Path; value: string }[] {
  const found: { path: Path; value: string }[] = [];
  function visit(value: unknown, path: Path) {
    if (typeof value === "string") found.push({ path, value });
    else if (Array.isArray(value))
      value.forEach((item, i) => visit(item, [...path, i]));
    else if (value && typeof value === "object")
      for (const [key, item] of Object.entries(value))
        visit(item, [...path, key]);
  }
  for (const [key, value] of Object.entries(node)) {
    if (["id", "type", "title", "ui"].includes(key)) continue;
    if (node.type === "code" && key === "code") continue;
    visit(value, [key]);
  }
  return found;
}
function mapStrings<T>(value: T, change: (text: string) => string): T {
  if (typeof value === "string") return change(value) as T;
  if (Array.isArray(value))
    return value.map((item) => mapStrings(item, change)) as T;
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        mapStrings(item, change),
      ]),
    ) as T;
  return value;
}
function mapNodeStrings(
  node: FlowNode,
  change: (text: string) => string,
): FlowNode {
  const next = { ...node } as Record<string, unknown>;
  for (const [key, value] of Object.entries(node)) {
    if (["id", "type", "title", "ui"].includes(key)) continue;
    if (node.type === "code" && key === "code") continue;
    next[key] = mapStrings(value, change);
  }
  return next as FlowNode;
}

export function referencesIn(
  node: FlowNode,
): { node: string; field: string }[] {
  const refs = new Map<string, { node: string; field: string }>();
  for (const { value } of stringsOf(node))
    for (const match of value.matchAll(REF_PATTERN))
      refs.set(`${match[1]}.${match[2]}`, { node: match[1], field: match[2] });
  return [...refs.values()];
}

// Where other nodes use `id`: its edges and the settings that reference it.
export function impactOf(config: FlowConfig, id: string) {
  const title = (key: string) =>
    config.graph.nodes.find((node) => node.id === key)?.title ?? key;
  return {
    edges: config.graph.edges
      .filter((edge) => edge.from === id || edge.to === id)
      .map((edge) => {
        const from = config.graph.nodes.find((node) => node.id === edge.from);
        const branch = branchOf(edge);
        return `${title(edge.from)} → ${title(edge.to)}${branch ? `（${branchLabel(from, branch)}）` : ""}`;
      }),
    references: config.graph.nodes
      .filter((node) => node.id !== id)
      .flatMap((node) =>
        stringsOf(node)
          .filter(({ value }) =>
            [...value.matchAll(REF_PATTERN)].some((match) => match[1] === id),
          )
          .map(({ path }) => `${node.title} · ${path.join(".")}`),
      ),
  };
}

export function renameNode(
  config: FlowConfig,
  from: string,
  to: string,
): FlowConfig {
  const pattern = new RegExp(
    `\\{\\{\\s*${from}\\.([a-z][a-z0-9_]*)\\s*\\}\\}`,
    "g",
  );
  const change = (text: string) => text.replace(pattern, `{{${to}.$1}}`);
  return {
    ...config,
    graph: {
      nodes: config.graph.nodes.map((node) =>
        mapNodeStrings(
          node.id === from ? ({ ...node, id: to } as FlowNode) : node,
          change,
        ),
      ),
      edges: config.graph.edges.map((edge) => ({
        ...edge,
        from: edge.from === from ? to : edge.from,
        to: edge.to === from ? to : edge.to,
      })),
    },
  };
}

export function renameBranch(
  config: FlowConfig,
  id: string,
  from: string,
  to: string,
): FlowConfig {
  if (from === to) return config;
  const taken = config.graph.edges.some(
    (edge) => edge.from === id && branchOf(edge) === to,
  );
  if (taken) return config;
  return {
    ...config,
    graph: {
      ...config.graph,
      edges: config.graph.edges.map((edge) =>
        edge.from === id && branchOf(edge) === from
          ? { ...edge, branch: to }
          : edge,
      ),
    },
  };
}

// Replace a node and drop out-edges for branches it no longer has.
export function replaceNode(config: FlowConfig, node: FlowNode): FlowConfig {
  const branches = branchesOf(node);
  return {
    ...config,
    graph: {
      nodes: config.graph.nodes.map((item) =>
        item.id === node.id ? node : item,
      ),
      edges: config.graph.edges.filter(
        (edge) => edge.from !== node.id || branches.includes(branchOf(edge)),
      ),
    },
  };
}

export function targetOf(
  config: FlowConfig,
  id: string,
  branch: string,
): string {
  return (
    config.graph.edges.find(
      (edge) => edge.from === id && branchOf(edge) === branch,
    )?.to ?? ""
  );
}
export function setNext(
  config: FlowConfig,
  id: string,
  branch: string,
  to: string,
): FlowConfig {
  const same = (edge: Edge) => edge.from === id && branchOf(edge) === branch;
  const index = config.graph.edges.findIndex(same);
  const edges = config.graph.edges.filter((edge) => !same(edge));
  // Keep the edge where it was so the diagram does not jump around.
  if (to)
    edges.splice(
      index === -1 ? edges.length : index,
      0,
      branch ? { from: id, to, branch } : { from: id, to },
    );
  return { ...config, graph: { ...config.graph, edges } };
}

export function deleteNode(config: FlowConfig, id: string): FlowConfig {
  return {
    ...config,
    graph: {
      nodes: config.graph.nodes.filter((node) => node.id !== id),
      edges: config.graph.edges.filter(
        (edge) => edge.from !== id && edge.to !== id,
      ),
    },
  };
}

export function freeId(config: FlowConfig, base: string): string {
  const taken = new Set(config.graph.nodes.map((node) => node.id));
  if (!taken.has(base)) return base;
  const stem = base.replace(/_\d+$/, "").slice(0, 28);
  for (let i = 2; ; i++) if (!taken.has(`${stem}_${i}`)) return `${stem}_${i}`;
}

export function newNode(
  config: FlowConfig,
  type: NodeType,
  preset?: NodeType["presets"][number],
): FlowNode {
  return {
    ...structuredClone(type.defaults),
    ...structuredClone(preset?.settings ?? {}),
    id: freeId(config, type.type),
    type: type.type,
    title: preset?.label ?? type.label,
  } as FlowNode;
}

// Insert after `after`, otherwise before the first end node.
export function insertNode(
  config: FlowConfig,
  node: FlowNode,
  after?: string,
): FlowConfig {
  const nodes = [...config.graph.nodes];
  const anchor = after ? nodes.findIndex((item) => item.id === after) + 1 : 0;
  const end = nodes.findIndex((item) => item.type === "end");
  const index =
    anchor > 0
      ? anchor
      : end === -1 || node.type === "end"
        ? nodes.length
        : end;
  nodes.splice(index, 0, node);
  return { ...config, graph: { ...config.graph, nodes } };
}

export function diagramOf(config: FlowConfig): {
  nodes: DiagramNode[];
  edges: FlowEdge[];
} {
  const byId = new Map(config.graph.nodes.map((node) => [node.id, node]));
  return {
    nodes: config.graph.nodes.map((node) => ({
      id: node.id,
      title: node.title,
      type: node.type,
      kind: nodeKind(node.type),
      model: "model" in node ? (node.model ?? "平台默认") : null,
    })),
    edges: config.graph.edges.map((edge) => ({
      source: edge.from,
      target: edge.to,
      conditional: Boolean(edge.branch),
      label: edge.branch ? branchLabel(byId.get(edge.from), edge.branch) : null,
    })),
  };
}

// A 422 from saving the draft, sorted onto the page: per node and setting path,
// per constant, and whatever belongs to the whole graph.
export type FlowIssues = {
  graph: string[];
  env: Record<string, string>;
  nodes: Record<string, Record<string, string>>;
};
export const noIssues: FlowIssues = { graph: [], env: {}, nodes: {} };
export function sortIssues(
  issues: ValidationIssue[],
  config: FlowConfig,
): FlowIssues {
  const sorted: FlowIssues = { graph: [], env: {}, nodes: {} };
  const add = (node: string, key: string, msg: string) => {
    const bucket = (sorted.nodes[node] ??= {});
    bucket[key] = bucket[key] ? `${bucket[key]}；${msg}` : msg;
  };
  for (const { loc, msg } of issues) {
    const [body, root, section, kind, id, ...rest] = loc;
    if (body !== "body" || root !== "config") {
      sorted.graph.push(msg);
    } else if (section === "env") {
      sorted.env[loc.slice(3).join(".")] = msg;
    } else if (section !== "graph" || loc.length === 3) {
      sorted.graph.push(msg);
    } else if (kind === "nodes" && typeof id === "string") {
      if (!config.graph.nodes.some((node) => node.id === id))
        sorted.graph.push(msg);
      else if (rest[0] === "branches") add(id, `next:${rest[1] ?? ""}`, msg);
      else add(id, rest.join("."), msg);
    } else if (
      kind === "edges" &&
      typeof id === "number" &&
      config.graph.edges[id]
    ) {
      const edge = config.graph.edges[id];
      add(edge.from, `next:${branchOf(edge)}`, msg);
    } else sorted.graph.push(msg);
  }
  return sorted;
}
// Messages for a setting and everything under it, e.g. "inputs.0" covers "inputs.0.key".
export function issuesUnder(
  bucket: Record<string, string> | undefined,
  path: string,
): string[] {
  if (!bucket) return [];
  return Object.entries(bucket)
    .filter(([key]) => key === path || key.startsWith(`${path}.`))
    .map(([, msg]) => msg);
}
