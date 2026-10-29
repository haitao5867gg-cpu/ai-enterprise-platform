import "server-only";
import {
  knowledgeExists,
  mockKnowledgeOptions,
  searchMockKnowledge,
} from "./mock-knowledge";
import type {
  AppTemplate,
  FlowConfig,
  FlowNode,
  HumanNode,
  Input,
  LlmNode,
  NodeType,
  Condition,
  ImportProblem,
  AgentOutput,
} from "../apps";
import { NODE_ID_PATTERN } from "../apps";
import {
  outputsOf,
  knowledgeIssues,
  REF_PATTERN,
  stringsOf,
  typeLabels,
  upstreamOf,
  walk,
  ownOutputs,
  canFail,
  branchesOf,
  branchLabel as displayBranch,
  nodeKind as kindOf,
} from "../flow-edit";
import { dataNodeIssues } from "../data-nodes";
import {
  dataDefaults,
  dataDescriptions,
  lakehouseConfig,
  simulateDataNode,
} from "./mock-data-nodes";
import type { Flow, NodeKind } from "../flow";
import { isRecord } from "../runs";
import { mockSkillVersionExists } from "./mock-skills";
import { mockDataSources } from "./mock-etl";
import { mockUploads } from "./mock-uploads";
import type { ValidationIssue } from "./mock-assets";

export const mockModels = [
  "mock",
  "gpt",
  "azure-gpt",
  "claude-bedrock",
  "qwen",
  "deepseek",
];
export const questionInput: Input = {
  key: "question",
  label: "客户问题",
  type: "textarea",
  required: true,
  max_length: 2000,
};
export function blankConfig(): FlowConfig {
  return {
    schema_version: 1,
    env: { vars: [], secrets: [] },
    graph: {
      nodes: [
        { id: "start", type: "start", title: "开始", inputs: [questionInput] },
        {
          id: "answer",
          type: "llm",
          title: "回答",
          model: null,
          skills: [],
          prompt: "{{start.question}}",
          output: { kind: "text" },
        },
        {
          id: "end",
          type: "end",
          title: "结束",
          outputs: [{ key: "answer", from: ["{{answer.text}}"] }],
        },
      ],
      edges: [
        { from: "start", to: "answer" },
        { from: "answer", to: "end" },
      ],
    },
  };
}
export function helloConfig(): FlowConfig {
  const config = blankConfig();
  config.graph.nodes = [
    {
      id: "start",
      type: "start",
      title: "开始",
      inputs: [
        {
          key: "text",
          label: "你想完成什么？",
          type: "textarea",
          required: true,
          max_length: 2000,
        },
      ],
    },
    {
      id: "draft",
      type: "llm",
      title: "回答",
      model: null,
      skills: [],
      system: "你是一个乐于助人的助手，用中文简洁地回答。",
      prompt: "{{start.text}}\n修改意见：{{review.comment}}",
      output: { kind: "text" },
    },
    {
      id: "review",
      type: "human",
      title: "确认",
      mode: "approval",
      prompt: "确认这个回答吗？驳回时可以写下修改意见。",
      show: "{{draft.text}}",
      options: [
        { key: "approve", label: "确认" },
        { key: "reject", label: "驳回" },
      ],
      comment_required: [],
    },
    {
      id: "end",
      type: "end",
      title: "结束",
      outputs: [{ key: "answer", from: ["{{draft.text}}"] }],
    },
  ];
  config.graph.edges = [
    { from: "start", to: "draft" },
    { from: "draft", to: "review" },
    { from: "review", to: "end", branch: "approve" },
    { from: "review", to: "draft", branch: "reject" },
  ];
  return config;
}
export function intentConfig(): FlowConfig {
  return {
    schema_version: 1,
    env: { vars: [], secrets: [] },
    graph: {
      nodes: [
        { id: "start", type: "start", title: "开始", inputs: [questionInput] },
        {
          id: "route",
          type: "classifier",
          title: "意图分类",
          model: null,
          skills: [],
          text: "{{start.question}}",
          instruction: "根据客户问题选择最合适的类别。",
          classes: [
            {
              key: "after_sales",
              label: "售后",
              description: "退换货、维修、投诉",
            },
            {
              key: "product",
              label: "产品咨询",
              description: "产品功能、规格和使用方法",
            },
          ],
        },
        {
          id: "draft",
          type: "llm",
          title: "起草售后回复",
          model: null,
          skills: [],
          system: "你是售后客服，耐心回答客户的问题。",
          prompt: "客户问：{{start.question}}\n修改意见：{{review.comment}}",
          output: { kind: "text" },
        },
        {
          id: "review",
          type: "human",
          title: "确认售后回复",
          mode: "approval",
          prompt: "确认这条售后回复吗？",
          show: "{{draft.text}}",
          options: [
            { key: "approve", label: "确认回复" },
            { key: "reject", label: "驳回重写" },
          ],
          comment_required: ["reject"],
        },
        {
          id: "answer",
          type: "llm",
          title: "回答产品问题",
          model: null,
          skills: [],
          prompt: "回答产品问题：{{start.question}}",
          output: { kind: "text" },
        },
        {
          id: "handoff",
          type: "template",
          title: "转人工客服",
          template: "客户的问题「{{start.question}}」已转给人工客服。",
        },
        {
          id: "end",
          type: "end",
          title: "结束",
          outputs: [
            {
              key: "reply",
              from: ["{{draft.text}}", "{{answer.text}}", "{{handoff.text}}"],
            },
          ],
        },
      ],
      edges: [
        { from: "start", to: "route" },
        { from: "route", to: "draft", branch: "after_sales" },
        { from: "route", to: "answer", branch: "product" },
        { from: "route", to: "handoff", branch: "other" },
        { from: "draft", to: "review" },
        { from: "review", to: "draft", branch: "reject" },
        { from: "review", to: "end", branch: "approve" },
        { from: "answer", to: "end" },
        { from: "handoff", to: "end" },
      ],
    },
  };
}
export function formConfig(): FlowConfig {
  return {
    schema_version: 1,
    env: { vars: [], secrets: [] },
    graph: {
      nodes: [
        { id: "start", type: "start", title: "开始", inputs: [questionInput] },
        {
          id: "ask_order",
          type: "human",
          title: "填写订单号",
          mode: "form",
          prompt: "请提供订单号",
          show: "客户问题：{{start.question}}",
          fields: [
            {
              key: "order_id",
              label: "订单号",
              type: "text",
              required: true,
              max_length: 32,
            },
          ],
        },
        {
          id: "end",
          type: "end",
          title: "结束",
          outputs: [{ key: "order_id", from: ["{{ask_order.order_id}}"] }],
        },
      ],
      edges: [
        { from: "start", to: "ask_order" },
        { from: "ask_order", to: "end" },
      ],
    },
  };
}
export function knowledgeConfig(): FlowConfig {
  return {
    ...blankConfig(),
    graph: {
      nodes: [
        { id: "start", type: "start", title: "开始", inputs: [questionInput] },
        {
          id: "kb",
          type: "knowledge",
          title: "知识检索",
          knowledge_bases: [],
          query: "{{start.question}}",
          top_k: 5,
          min_score: 0.3,
          retrieval: { ...mockKnowledgeOptions.retrieval },
        },
        {
          id: "answer",
          type: "llm",
          title: "回答",
          model: null,
          skills: [],
          prompt:
            "根据以下资料回答问题，并按 [n] 标注出处。资料里没有答案就直说。\n{{kb.context}}\n问题：{{start.question}}",
          output: { kind: "text" },
        },
        {
          id: "end",
          type: "end",
          title: "结束",
          outputs: [{ key: "answer", from: ["{{answer.text}}"] }],
        },
      ],
      edges: [
        { from: "start", to: "kb" },
        { from: "kb", to: "answer" },
        { from: "answer", to: "end" },
      ],
    },
  };
}
export const mockTemplates: AppTemplate[] = [
  {
    id: "etl-lakehouse",
    title: "数据 ETL（湖仓）",
    summary:
      "探查数据源、按口径知识库出方案、经人确认后抽取到 Databricks，按开发规范写 PySpark 代码并在 dev 运行，预览结果数据",
    needs: ["datasource", "knowledge_base"],
    config: lakehouseConfig(),
  },
  {
    id: "knowledge-agent",
    title: "知识库智能体",
    summary: "智能体自主检索企业资料，按引用来源回答问题。",
    needs: ["knowledge_base"],
    config: {
      ...blankConfig(),
      graph: {
        nodes: [
          {
            id: "start",
            type: "start",
            title: "开始",
            inputs: [questionInput],
          },
          {
            id: "agent",
            type: "agent",
            title: "知识库智能体",
            model: null,
            skills: [],
            tools: [],
            max_steps: 6,
            task: "先检索知识库，再根据资料回答，按 [n] 标出处：{{start.question}}",
            knowledge: {
              knowledge_bases: [],
              top_k: 5,
              min_score: 0.3,
              retrieval: { ...mockKnowledgeOptions.retrieval },
            },
          },
          {
            id: "end",
            type: "end",
            title: "结束",
            outputs: [{ key: "answer", from: ["{{agent.text}}"] }],
          },
        ],
        edges: [
          { from: "start", to: "agent" },
          { from: "agent", to: "end" },
        ],
      },
    },
  },
  {
    id: "knowledge-qa",
    title: "知识问答",
    summary: "检索企业资料，按引用来源回答问题。",
    needs: ["knowledge_base"],
    config: knowledgeConfig(),
  },
  {
    id: "qa-approval",
    title: "问答加确认",
    summary: "模型回答，经人工确认后返回；驳回后重写。",
    needs: [],
    config: helloConfig(),
  },
  {
    id: "intent-routing",
    title: "意图分类路由",
    summary: "区分售后、产品咨询和其他问题，售后回复需人工确认。",
    needs: [],
    config: intentConfig(),
  },
  {
    id: "datasource-qa",
    title: "数据源问答",
    summary: "智能体调用只读数据源工具回答问题；创建后请先选择数据源。",
    needs: ["datasource"],
    config: {
      ...blankConfig(),
      graph: {
        nodes: [
          {
            id: "start",
            type: "start",
            title: "开始",
            inputs: [questionInput],
          },
          {
            id: "agent",
            type: "agent",
            title: "数据问答",
            model: null,
            skills: [],
            task: "根据数据回答：{{start.question}}",
            tools: [],
            knowledge: null,
            max_steps: 8,
          },
          {
            id: "end",
            type: "end",
            title: "结束",
            outputs: [{ key: "answer", from: ["{{agent.text}}"] }],
          },
        ],
        edges: [
          { from: "start", to: "agent" },
          { from: "agent", to: "end" },
        ],
      },
    },
  },
];
const defaults: FlowNode[] = [
  ...dataDefaults,
  {
    id: "kb",
    type: "knowledge",
    title: "知识检索",
    knowledge_bases: [],
    query: "",
    top_k: 5,
    min_score: 0.3,
    retrieval: { ...mockKnowledgeOptions.retrieval },
  },
  blankConfig().graph.nodes[0],
  { ...(blankConfig().graph.nodes[1] as LlmNode), quiet: false },
  intentConfig().graph.nodes[1],
  {
    id: "condition",
    type: "condition",
    title: "条件判断",
    rules: [
      {
        key: "matched",
        label: "符合条件",
        match: "all",
        conditions: [{ left: "{{start.question}}", op: "not_empty" }],
      },
    ],
  },
  {
    id: "tool",
    type: "tool",
    title: "调用工具",
    datasource: "sample-postgres",
    tool: "list_schemas",
    args: {},
  },
  {
    id: "agent",
    type: "agent",
    title: "智能体",
    model: null,
    skills: [],
    task: "回答：{{start.question}}",
    tools: [],
    knowledge: null,
    max_steps: 8,
  },
  {
    id: "human",
    type: "human",
    title: "人工",
    mode: "approval",
    prompt: "请确认",
    show: "",
    options: [
      { key: "approve", label: "确认" },
      { key: "reject", label: "驳回" },
    ],
    comment_required: [],
  },
  {
    id: "template",
    type: "template",
    title: "文本拼接",
    template: "{{start.question}}",
  },
  {
    id: "code",
    type: "code",
    title: "代码",
    code: "def main(inputs):\n    return {'text': inputs.get('text', '')}",
    inputs: { text: "{{start.question}}" },
    outputs: [{ key: "text", type: "string" }],
    timeout_seconds: 10,
  },
  blankConfig().graph.nodes[2],
];
export const mockNodeTypes: NodeType[] = defaults.map((node) => {
  const settings = Object.fromEntries(
    Object.entries(node).filter(
      ([key]) => !["id", "type", "title"].includes(key),
    ),
  );
  const kind = nodeKind(node);
  return {
    type: node.type,
    label: typeLabels[node.type],
    description:
      dataDescriptions[node.type] ?? `添加${typeLabels[node.type]}节点。`,
    category: dataDescriptions[node.type]
      ? "data"
      : kind === "code"
        ? "logic"
        : kind,
    error_handling: canFail(node),
    outputs: (node.type === "start" ||
    node.type === "code" ||
    node.type === "end"
      ? []
      : ownOutputs(node)
    ).map((key) => ({ key, description: key })),
    defaults: settings,
    presets:
      node.type === "llm"
        ? [
            {
              key: "extract",
              label: "抽取字段",
              description: "从文本里抽取结构化字段",
              settings: {
                prompt: "从下面的文本中抽取字段：\n{{start.question}}",
                output: {
                  kind: "json",
                  fields: [
                    { key: "amount", type: "number", description: "金额" },
                  ],
                },
              },
            },
          ]
        : [],
    max_count: node.type === "start" ? 1 : null,
  } as NodeType;
});
export function nodeKind(node: FlowNode): NodeKind {
  return kindOf(node.type);
}
export function branches(node: FlowNode): string[] {
  return branchesOf(node);
}
function branchLabel(
  node: FlowNode | undefined,
  branch?: string | null,
): string | null {
  return branch ? displayBranch(node, branch) : null;
}
export function flowView(
  config: FlowConfig,
  title: string,
  summary: string,
): Pick<Flow, "nodes" | "edges" | "design"> {
  const nodes = config.graph.nodes.map((node) => {
    const kind = nodeKind(node);
    return {
      id: node.id,
      title: node.title,
      type: node.type,
      kind,
      model: "model" in node ? (node.model ?? "mock") : null,
      skills: "skills" in node ? node.skills : [],
      design: {
        title: node.title,
        kind,
        summary: `执行${node.title}。`,
        note: node.note ?? null,
        inputs: "按节点中的变量引用读取输入。",
        outputs: "写入该节点的产出字段。",
        tools: node.type === "tool" ? [node.tool] : [],
        model_step: kind === "model" ? node.id : null,
        prompt:
          node.type === "llm"
            ? [node.system, node.prompt].filter(Boolean).join("\n")
            : node.type === "agent"
              ? node.task
              : node.type === "classifier"
                ? node.text
                : null,
        knowledge: null,
        checks: null,
        retry: null,
      },
    };
  });
  const start = config.graph.nodes.find((node) => node.type === "start");
  return {
    design: {
      title,
      summary,
      pattern: "按编排配置依次执行节点，遇到人工节点暂停。",
      knowledge: "由节点选择的数据源提供。",
      skills: "由各模型节点引用的 Skill 提供。",
    },
    nodes: [
      { id: "__start__", title: "开始", kind: "terminal", type: null },
      ...nodes,
      { id: "__end__", title: "结束", kind: "terminal", type: null },
    ],
    edges: [
      ...(start
        ? [
            {
              source: "__start__",
              target: start.id,
              conditional: false,
              label: null,
            },
          ]
        : []),
      ...config.graph.edges.map((edge) => ({
        source: edge.from,
        target: edge.to,
        conditional: Boolean(edge.branch),
        label: branchLabel(
          config.graph.nodes.find((node) => node.id === edge.from),
          edge.branch,
        ),
      })),
      ...config.graph.nodes
        .filter((node) => node.type === "end")
        .map((node) => ({
          source: node.id,
          target: "__end__",
          conditional: false,
          label: null,
        })),
    ],
  };
}
export function validateFlow(config: unknown): ValidationIssue[] {
  const root = ["body", "config", "graph"];
  if (
    !isRecord(config) ||
    config.schema_version !== 1 ||
    !isRecord(config.env) ||
    !Array.isArray(config.env.vars) ||
    !Array.isArray(config.env.secrets) ||
    !isRecord(config.graph) ||
    !Array.isArray(config.graph.nodes) ||
    !Array.isArray(config.graph.edges)
  )
    return [{ loc: root, msg: "需要 schema_version、env 和完整的 graph 配置" }];
  const { nodes, edges } = config.graph as FlowConfig["graph"];
  if (
    !nodes.every(
      (node) =>
        isRecord(node) &&
        NODE_ID_PATTERN.test(node.id) &&
        !["env", "secret"].includes(node.id) &&
        defaults.some((item) => item.type === node.type) &&
        typeof node.title === "string" &&
        node.title.trim(),
    )
  )
    return [{ loc: root, msg: "节点标识、标题或类型不合法" }];
  if (
    nodes.filter((node) => node.type === "start").length !== 1 ||
    !nodes.some((node) => node.type === "end") ||
    nodes.length > 50
  )
    return [
      { loc: root, msg: "需要一个开始节点和至少一个结束节点，最多 50 个节点" },
    ];
  const issues: ValidationIssue[] = [
    ...dataNodeIssues(config as FlowConfig),
    ...knowledgeIssues(
      config as FlowConfig,
      mockKnowledgeOptions.rerank_models,
    ),
  ];
  const ids = new Set<string>();
  for (const node of nodes) {
    const loc = [...root, "nodes", node.id];
    if (ids.has(node.id))
      issues.push({ loc: [...loc, "id"], msg: "节点 id 重复" });
    ids.add(node.id);
    if (node.type === "knowledge") {
      if (
        typeof node.query !== "string" ||
        !node.query.trim() ||
        node.query.length > 2000
      )
        issues.push({
          loc: [...loc, "query"],
          msg: "检索问题须为 1–2000 字符",
        });
    }
    if (
      node.type === "llm" &&
      node.quiet !== undefined &&
      typeof node.quiet !== "boolean"
    )
      issues.push({
        loc: [...loc, "quiet"],
        msg: "quiet 必须是 true 或 false",
      });
    if (
      node.type === "agent" &&
      (node.tools.length > 10 ||
        !Number.isInteger(node.max_steps) ||
        node.max_steps < 1 ||
        node.max_steps > 20)
    )
      issues.push({
        loc: [...loc, "tools"],
        msg: "数据源工具最多 10 个，最多步数须为 1–20 的整数",
      });
    try {
      for (const branch of branches(node))
        if (
          edges.filter(
            (edge) => edge.from === node.id && (edge.branch ?? "") === branch,
          ).length !== 1
        )
          issues.push({
            loc: [...loc, "branches", branch],
            msg: "每个分支须连接一条出边",
          });
    } catch {
      issues.push({ loc, msg: "节点设置不完整" });
    }
  }
  edges.forEach((edge, i) => {
    if (!ids.has(edge.to))
      issues.push({ loc: [...root, "edges", i, "to"], msg: "目标节点不存在" });
  });
  if (issues.length) return issues;
  const flow = config as FlowConfig;
  const start = nodes.find((node) => node.type === "start")!;
  const reached = walk(flow, [start.id]);
  const exits = walk(
    flow,
    nodes.filter((node) => node.type === "end").map((node) => node.id),
    true,
  );
  for (const node of nodes) {
    const loc = [...root, "nodes", node.id];
    if (node.type !== "start" && !reached.has(node.id))
      issues.push({ loc, msg: "从开始节点到不了这个节点" });
    else if (node.type !== "end" && !exits.has(node.id))
      issues.push({ loc, msg: "这个节点到不了结束节点，循环需要一个出口" });
    for (const { path, value } of stringsOf(node))
      for (const [, id, field] of value.matchAll(REF_PATTERN)) {
        const at = { loc: [...loc, ...path] };
        const target = nodes.find((item) => item.id === id);
        if (id === "env") {
          if (!flow.env.vars.some((item) => item.key === field))
            issues.push({ ...at, msg: `没有常量 ${field}` });
        } else if (id === "secret") {
          if (node.type !== "tool" || path[0] !== "args")
            issues.push({ ...at, msg: "密钥只能用在工具参数里" });
          else if (!flow.env.secrets.includes(field))
            issues.push({ ...at, msg: `密钥 ${field} 没有在配置中声明` });
        } else if (!target || !outputsOf(target).includes(field))
          issues.push({ ...at, msg: `引用了不存在的变量 {{${id}.${field}}}` });
        else if (!upstreamOf(flow, node.id).some((item) => item.id === id))
          issues.push({
            ...at,
            msg: `{{${id}.${field}}} 所在的节点在这里可能还没运行`,
          });
      }
  }
  return issues;
}
export const allSources = () => [
  ...mockDataSources,
  ...[...mockUploads.values()].map((item) => item.datasource),
];
export function missingReferences(
  config: FlowConfig,
  setSecrets: string[] = [],
): ImportProblem[] {
  const problems: ImportProblem[] = [];
  for (const node of config.graph.nodes) {
    if ("skills" in node)
      for (const ref of node.skills)
        if (!mockSkillVersionExists(ref.name, ref.version))
          problems.push({
            kind: "skill",
            ...ref,
            message: "没有这个 Skill 版本",
          });
    const knowledge =
      node.type === "knowledge"
        ? node
        : node.type === "agent"
          ? node.knowledge
          : null;
    if (knowledge)
      for (const kb_id of knowledge.knowledge_bases)
        if (!knowledgeExists(kb_id))
          problems.push({
            kind: "knowledge_base",
            kb_id,
            message: "没有这个知识库，请在节点里重新选择",
          });
    const sources =
      node.type === "tool" ||
      node.type === "etl_profile" ||
      node.type === "databricks_ingest"
        ? [node.datasource]
        : node.type === "agent"
          ? node.tools.map((item) => item.datasource)
          : [];
    for (const datasource_id of sources)
      if (
        !datasource_id.includes("{{") &&
        !allSources().some((item) => item.datasource_id === datasource_id)
      )
        problems.push({
          kind: "datasource",
          datasource_id,
          message: "没有这个数据源，请在节点里重新选择",
        });
    if ("model" in node && node.model && !mockModels.includes(node.model))
      problems.push({
        kind: "model",
        name: node.model,
        message: "模型网关上没有这个别名",
      });
  }
  for (const name of config.env.secrets)
    if (!setSecrets.includes(name))
      problems.push({
        kind: "secret",
        name,
        message: "密钥没有值，请在应用设置里填写",
      });
  return [
    ...new Map(problems.map((item) => [JSON.stringify(item), item])).values(),
  ];
}
export type VariablePool = Record<string, Record<string, unknown>>;
export function render(
  value: unknown,
  pool: VariablePool,
  raw = false,
): unknown {
  if (typeof value === "string") {
    const pattern = /\{\{\s*([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\s*\}\}/g;
    const exact = value.match(
      /^\{\{\s*([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\s*\}\}$/,
    );
    if (raw && exact) return pool[exact[1]]?.[exact[2]] ?? "";
    return value.replace(pattern, (_, node, key) => {
      const item = pool[node]?.[key];
      return item == null
        ? ""
        : typeof item === "object"
          ? JSON.stringify(item)
          : String(item);
    });
  }
  if (Array.isArray(value)) return value.map((item) => render(item, pool, raw));
  if (isRecord(value))
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        render(item, pool, raw),
      ]),
    );
  return value;
}
function matches(condition: Condition, pool: VariablePool) {
  const left = render(condition.left, pool, true),
    right = render(condition.right, pool, true);
  switch (condition.op) {
    case "eq":
      return left === right;
    case "ne":
      return left !== right;
    case "empty":
      return (
        left == null || left === "" || (Array.isArray(left) && !left.length)
      );
    case "not_empty":
      return (
        left != null &&
        left !== "" &&
        (!Array.isArray(left) || Boolean(left.length))
      );
    case "contains":
      return String(left).includes(String(right));
    case "not_contains":
      return !String(left).includes(String(right));
    default: {
      if (left === "" || right === "" || left == null || right == null)
        return false;
      const a = Number(left),
        b = Number(right);
      if (!Number.isFinite(a) || !Number.isFinite(b)) return false;
      return condition.op === "gt"
        ? a > b
        : condition.op === "gte"
          ? a >= b
          : condition.op === "lt"
            ? a < b
            : a <= b;
    }
  }
}
export function simulateNode(
  node: FlowNode,
  pool: VariablePool,
): {
  outputs: Record<string, unknown>;
  branch: string | null;
  rendered: Record<string, unknown>;
} {
  let outputs: Record<string, unknown> = {},
    branch: string | null = null;
  const rendered = render(
    Object.fromEntries(
      Object.entries(node).filter(
        ([key]) => !["id", "type", "title", "note"].includes(key),
      ),
    ),
    pool,
    true,
  ) as Record<string, unknown>;
  const dataResult = simulateDataNode(node, rendered);
  if (dataResult) return { ...dataResult, rendered };
  const fakeValue = (type: string): unknown =>
    type === "number"
      ? 100
      : type === "boolean"
        ? true
        : type === "array"
          ? []
          : type === "object"
            ? {}
            : "（模拟抽取）";
  if (node.type === "knowledge") {
    for (const id of node.knowledge_bases)
      if (!knowledgeExists(id)) throw new Error("知识库不存在");
    const hits = searchMockKnowledge(
      node.knowledge_bases,
      String(rendered.query),
      node.top_k,
      node.min_score,
      node.retrieval,
    ).map((hit, index) => ({ ...hit, index: index + 1 }));
    outputs = {
      hits,
      context: hits
        .map(
          (hit) =>
            `[${hit.index}]《${hit.document_name}》${hit.heading}${hit.page === null ? "" : `（第 ${hit.page} 页）`}\n${hit.text}`,
        )
        .join("\n\n"),
    };
  }
  if (node.type === "llm")
    outputs =
      node.output.kind === "text"
        ? { text: `（模拟回答）${render(node.prompt, pool)}` }
        : Object.fromEntries(
            node.output.fields.map((field) => [
              field.key,
              fakeValue(field.type),
            ]),
          );
  if (node.type === "agent") {
    if (!node.tools.length && !node.knowledge)
      throw new Error("至少选一个数据源工具或知识库");
    const output: AgentOutput = {
      text: `（模拟回答）${render(node.task, pool)}`,
      steps: [],
      hits: [],
    };
    if (node.knowledge) {
      const query = String(pool.start?.question ?? rendered.task);
      const step: AgentOutput["steps"][number] = {
        tool: "search_knowledge",
        datasource: null,
        args: { query, knowledge_bases: node.knowledge.knowledge_bases },
        error: null,
        hits: 0,
      };
      try {
        for (const id of node.knowledge.knowledge_bases)
          if (!knowledgeExists(id)) throw new Error("知识库不存在");
        output.hits = searchMockKnowledge(
          node.knowledge.knowledge_bases,
          query,
          node.knowledge.top_k,
          node.knowledge.min_score,
          node.knowledge.retrieval,
        ).map((hit, i) => ({ ...hit, index: i + 1 }));
        step.hits = output.hits.length;
        output.text = output.hits.length
          ? `根据检索到的资料：${output.hits.map((hit) => `${hit.text} [${hit.index}]`).join("\n\n")}`
          : "没有找到相关资料";
      } catch (error) {
        step.error = error instanceof Error ? error.message : "检索失败";
        output.text = `知识库检索未成功：${step.error}`;
      }
      output.steps.push(step);
    }
    outputs = output;
  }
  if (node.type === "classifier") {
    branch = node.classes[0]?.key ?? "other";
    outputs = { class: branch, label: node.classes[0]?.label ?? "都不是" };
  }
  if (node.type === "condition") {
    branch =
      node.rules.find((rule) =>
        rule.match === "all"
          ? rule.conditions.every((item) => matches(item, pool))
          : rule.conditions.some((item) => matches(item, pool)),
      )?.key ?? "else";
    outputs = { rule: branch };
  }
  if (node.type === "template") outputs = { text: render(node.template, pool) };
  if (node.type === "tool")
    outputs = { result: { mock: true, tool: node.tool, args: rendered.args } };
  if (node.type === "code")
    outputs = Object.fromEntries(
      node.outputs.map((field) => [
        field.key,
        (render(node.inputs, pool, true) as Record<string, unknown>)[
          field.key
        ] ?? fakeValue(field.type),
      ]),
    );
  if (node.type === "end")
    outputs = Object.fromEntries(
      node.outputs.map((field) => [
        field.key,
        field.from
          .map((ref) => render(ref, pool, true))
          .find(
            (value) => value !== "" && value !== null && value !== undefined,
          ) ?? null,
      ]),
    );
  return { outputs, branch, rendered };
}
export function humanPending(node: HumanNode, pool: VariablePool) {
  return {
    kind: node.mode,
    prompt: String(render(node.prompt, pool)),
    options:
      node.mode === "form"
        ? ["submit"]
        : node.options.map((option) => option.key),
    comment_required:
      node.mode === "approval" ? (node.comment_required ?? []) : [],
    option_labels:
      node.mode === "form"
        ? { submit: "提交" }
        : Object.fromEntries(
            node.options.map((option) => [option.key, option.label]),
          ),
    ...(node.mode === "form" ? { fields: node.fields } : {}),
    details: {
      type: "flow_human",
      node: node.id,
      show: render(node.show ?? "", pool),
    },
  };
}
