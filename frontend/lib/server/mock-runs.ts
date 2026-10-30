import "server-only";
import type { Reference, RunSource } from "../knowledge";
import { searchMockKnowledge } from "./mock-knowledge";
import { randomUUID } from "node:crypto";
import {
  isFinished,
  isRecord,
  jsonObject,
  type Decision,
  type Message,
  type Run,
  type RunStatus,
  type RunSummary,
} from "../runs";
import { getEtlResult, type EtlLanguage, type EtlPlan } from "../etl";
import {
  mockDeployed,
  mockEtlPlan,
  mockEtlResult,
  mockProdTarget,
  type MockApproval,
} from "./mock-etl";
import { mockZip } from "./mock-zip";
import { addStep, decideStep, mockFlow, type MockSteps } from "./mock-flow";
import { mockUpload, mockUploads, mockUploadedPlan } from "./mock-uploads";
import { mockDatasources, mockToolService } from "./mock-datasources";
import type { UploadedDataset } from "../uploads";
import {
  isFlowConfig,
  type AppConfig,
  type FlowConfig,
  type FlowNode,
  type FormField,
} from "../apps";
import { defaultValues, inputPayload, validateFields } from "../input-fields";
import { mockAppSnapshot, mockAppTitle } from "./mock-apps";
import {
  allSources,
  flowView,
  humanPending,
  simulateNode,
  type VariablePool,
} from "./mock-flow-apps";
import { invalid } from "./mock-assets";
import type { AppManual } from "../app-manual";
import { buildMockManual } from "./mock-manual";
import {
  MockNodeFailure,
  dataNodeSaid,
  lakehouseConfig,
} from "./mock-data-nodes";

type StoredRun = {
  run: Run;
  manual?: AppManual;
  // The configuration the run uses, kept for a copy of a draft's run.
  config?: AppConfig;
  readyAt: number;
  decision?: Decision;
  comment?: string;
  evaluation?: { score: number };
  etl?: {
    language: EtlLanguage;
    knowledgeBases?: string[];
    knowledgeTopK?: number;
    plan?: EtlPlan;
    // Which approval the run is at: the plan, then the release to prod.
    stage: "plan" | "release";
    version: number;
    approvals: MockApproval[];
    source: string;
    upload?: UploadedDataset;
  };
  flow?: {
    config: FlowConfig;
    pool: VariablePool;
    next: string;
    visits: Record<string, number>;
    count: number;
    input: Record<string, unknown>;
    title: string;
    summary: string;
  };
  steps: MockSteps;
  // Where the run failed and why, for the detail's `failed`.
  failure?: { node: string; title: string; error_type: string | null };
};
// Like the backend: these failures are likely to pass on a second try.
const temporary = new Set([
  "timeout",
  "model_error",
  "tool_error",
  "knowledge_error",
  "databricks_error",
  "sandbox_unavailable",
]);
function retryable(entry: StoredRun) {
  if (entry.run.status !== "error") return false;
  const type = entry.failure?.error_type ?? null;
  return type === null || temporary.has(type);
}
function fail(
  entry: StoredRun,
  node: { id: string; title: string },
  error: string,
  type: string | null,
) {
  setStatus(entry.run, "error");
  entry.run.error = error;
  entry.failure = { node: node.id, title: node.title, error_type: type };
}
// What the detail adds to the run: its input, where it failed, what can be done next.
function detail(entry: StoredRun): Run {
  const { run } = entry;
  const failure = run.status === "error" ? entry.failure : undefined;
  const cited = entry.flow ? citedIndexes(entry) : null;
  return {
    ...withAppTitle(run),
    // Like the backend: what the run's own configuration asks for.
    inputs: entry.flow
      ? (entry.flow.config.graph.nodes.find((node) => node.type === "start")
          ?.inputs ?? [])
      : null,
    failed: failure ? { ...failure, retryable: retryable(entry) } : null,
    actions: {
      cancel: run.status === "running" || run.status === "waiting",
      retry: retryable(entry) && Boolean(entry.flow),
      rerun: Boolean(run.input) && Boolean(mockAppSnapshot(run.app)),
    },
    // Like the backend, worked out when the run is read.
    sources: cited
      ? run.sources?.map((source) => ({
          ...source,
          cited: cited.has(source.index),
        }))
      : (run.sources ?? []),
  };
}

// Like the backend (docs/api/knowledge.md, "运行记录里的出处"): the [n] in
// Markdown prose, leaving out code, links and images, and link definitions.
const notProse =
  /^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ \t]{0,3}\1[ \t]*$|(?![\s\S]))|(`+)[^`]*?\2|!?\[(?:[^[\]\n]|\[[^[\]\n]*\])*\]\([^)\n]*\)|^[ \t]{0,3}\[[^\]\n]+\]:[^\n]*$/gm;
function citedNumbers(text: string): Set<number> {
  return new Set(
    [...text.replace(notProse, " ").matchAll(/\[([0-9]{1,6})\]/g)].map(
      (match) => Number(match[1]),
    ),
  );
}
// The source numbers a flow app's run cites in what it shows: its result's
// texts (not a knowledge node's context passed through), the latest answer of
// each llm node that is said and of each agent, what a waiting person is shown,
// and the references of a plan that passed its check.
function citedIndexes(entry: StoredRun): Set<number> {
  const { run, flow } = entry;
  const used = new Set<number>();
  if (!flow) return used;
  const contexts = new Set(
    flow.config.graph.nodes
      .filter((node) => node.type === "knowledge")
      .map((node) => flow.pool[node.id]?.context)
      .filter((context): context is string => typeof context === "string"),
  );
  const texts = isRecord(run.result)
    ? Object.values(run.result).filter(
        (value): value is string =>
          typeof value === "string" && !contexts.has(value),
      )
    : [];
  for (const node of flow.config.graph.nodes) {
    const output = flow.pool[node.id];
    if (!isRecord(output)) continue;
    if (
      (node.type === "agent" ||
        (node.type === "llm" && node.output.kind === "text" && !node.quiet)) &&
      typeof output.text === "string" &&
      !jsonObject(output.text)
    )
      texts.push(output.text);
    if (
      node.type === "etl_plan_check" &&
      output.ok === true &&
      isRecord(output.plan) &&
      Array.isArray(output.plan.references)
    )
      for (const reference of output.plan.references)
        if (isRecord(reference) && Number.isInteger(reference.index))
          used.add(reference.index as number);
  }
  const details = run.pending?.details;
  if (isRecord(details) && typeof details.show === "string")
    texts.push(details.show);
  for (const text of texts)
    for (const index of citedNumbers(text)) used.add(index);
  return used;
}

// Like the backend: a flow app's message names its step, who said it and when.
function stamp(
  node: { id: string; title: string },
  message: Message,
  actor: string | null = null,
  at = new Date().toISOString(),
): Message {
  return { ...message, node: node.id, node_title: node.title, actor, at };
}
// What a step of a flow app says in the run process, as the backend words it
// (docs/api/flow-apps.md, "运行"): an answer, unless it is JSON or the node is
// quiet (data for later nodes), or what a data node did.
function nodeSaid(
  node: FlowNode,
  outputs: Record<string, unknown>,
  branch: string | null,
): string | null {
  if (
    node.type === "agent" ||
    (node.type === "llm" && node.output.kind === "text" && !node.quiet)
  )
    return typeof outputs.text === "string" ? outputs.text : null;
  return dataNodeSaid(node, outputs, branch);
}
// What a person decided at a human step, in one sentence.
function approvalSaid(label: string, comment?: string) {
  return `选了「${label}」${comment ? `，意见：${comment}` : ""}`;
}
// A form's answers in one sentence, a part per field.
function formSaid(fields: FormField[], values: Record<string, unknown>) {
  return fields
    .map((field) => {
      const value = values[field.key];
      if (value == null || (typeof value === "string" && !value.trim()))
        return `${field.label}没填`;
      if (field.type === "select")
        return `${field.label}选了「${field.choices?.find((choice) => choice.value === value)?.label || value}」`;
      if (field.type === "boolean")
        return `${field.label}选了「${value ? "是" : "否"}」`;
      const text = typeof value === "object" ? JSON.stringify(value) : value;
      return `${field.label}填了「${String(text).trim()}」`;
    })
    .join("；");
}
// Like the backend's run store: a wait for a person is counted while it lasts.
function setStatus(run: Run, status: RunStatus) {
  const now = new Date();
  if (status === "waiting") {
    if (run.status !== "waiting" || !run.waiting_since)
      run.waiting_since = now.toISOString();
  } else {
    if (
      run.status === "waiting" &&
      run.waiting_since &&
      typeof run.wait_seconds === "number"
    )
      run.wait_seconds +=
        (now.getTime() - Date.parse(run.waiting_since)) / 1000;
    // A failure waits for someone to retry it.
    else if (
      run.status === "error" &&
      status !== "error" &&
      run.updated_at &&
      typeof run.wait_seconds === "number"
    )
      run.wait_seconds += (now.getTime() - Date.parse(run.updated_at)) / 1000;
    run.waiting_since = null;
  }
  run.status = status;
  run.updated_at = now.toISOString();
  run.finished_at = isFinished(status) ? run.updated_at : null;
}

function runTitle(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const line = value.replace(/\s+/g, " ").trim();
  if (!line) return null;
  return line.length <= 80 ? line : `${line.slice(0, 79)}…`;
}

// The list carries only what the backend's summaries do.
const summaryKeys: (keyof RunSummary)[] = [
  "run_id",
  "app",
  "app_title",
  "app_version",
  "draft",
  "status",
  "title",
  "created_by",
  "cancelled_by",
  "rerun_of",
  "created_at",
  "updated_at",
  "finished_at",
  "step",
  "waiting_since",
  "wait_seconds",
];
function summary(run: Run): RunSummary {
  return Object.fromEntries(
    summaryKeys.map((key) => [key, withAppTitle(run)[key]]),
  ) as RunSummary;
}

function withAppTitle(run: Run): Run {
  return { ...run, app_title: mockAppTitle(run.app) ?? run.app_title };
}

// Next dev may reload this module. One process-wide store keeps route requests consistent.
const globalStore = globalThis as typeof globalThis & {
  aepMockRuns?: Map<string, StoredRun>;
};
const store = (globalStore.aepMockRuns ??= new Map<string, StoredRun>());

function seedRetailRun() {
  if (store.has("retail-etl-demo")) return;
  const config = lakehouseConfig(true);
  const input = {
    datasource: "sample-postgres",
    request: "按城市和月份统计有效订单销售额",
    tables: "retail.*",
    language: "pyspark",
    name: "retail_sales",
  };
  const pool: VariablePool = { env: {}, secret: {} };
  const steps: MockSteps = [];
  const sources: RunSource[] = [];
  const messages: Message[] = [];
  // When each step spoke: the review waited seven minutes for its answer.
  const minutes: Record<string, number> = {
    start: 0,
    profile: 0.5,
    check_plan: 2,
    review: 9,
    ingest: 10,
    check_code: 11,
    dev_run: 12,
  };
  const at = (node: string) =>
    new Date(
      Date.parse("2026-10-29T08:00:00.000Z") + (minutes[node] ?? 0) * 60_000,
    ).toISOString();
  for (const node of config.graph.nodes) {
    // The requester asked and approved the plan; the other steps are simulated.
    const person = node.type === "start" || node.type === "human";
    const simulated = person ? null : simulateNode(node, pool);
    const output: Record<string, unknown> =
      simulated?.outputs ??
      (node.type === "start"
        ? input
        : { decision: "approve", comment: "按口径执行", actor: "演示用户" });
    const said = simulated
      ? nodeSaid(node, output, simulated.branch)
      : node.type === "start"
        ? input.request
        : approvalSaid("确认", "按口径执行");
    if (said)
      messages.push(
        stamp(
          node,
          { role: person ? "user" : "assistant", content: said },
          person ? "演示用户" : null,
          at(node.id),
        ),
      );
    if (node.type === "knowledge" && Array.isArray(output.hits)) {
      output.hits = (output.hits as Reference[]).map((hit) => {
        const source = {
          ...hit,
          index: sources.length + 1,
          node: node.id,
          node_title: node.title,
        };
        sources.push(source);
        return source;
      });
    }
    addStep(steps, node.id, structuredClone(pool), output);
    pool[node.id] = output;
  }
  const title = "零售数据 ETL",
    summary =
      "探查零售数据、确认业务口径，生成 Databricks 转换并预览 dev 结果。";
  store.set("retail-etl-demo", {
    run: {
      run_id: "retail-etl-demo",
      app: "retail-etl",
      app_title: title,
      app_version: 1,
      draft: false,
      status: "done",
      input,
      title: input.request,
      created_by: "演示用户",
      created_at: "2026-10-29T08:00:00.000Z",
      updated_at: "2026-10-29T08:12:00.000Z",
      finished_at: "2026-10-29T08:12:00.000Z",
      step: { node: "end", title: "结束" },
      waiting_since: null,
      wait_seconds: 420,
      pending: null,
      result: pool.end,
      error: null,
      messages,
      sources,
    },
    manual: buildMockManual("retail-etl", title, summary, 1, config),
    readyAt: 0,
    steps,
    flow: {
      config,
      pool,
      next: "",
      visits: {},
      count: steps.length,
      input,
      title,
      summary,
    },
  });
}

export function createMockEvalRun(caseId: string, score: number): string {
  const run_id = randomUUID();
  store.set(run_id, {
    run: {
      run_id,
      app: "etl",
      app_title: "自然语言 ETL 工作流",
      app_version: null,
      draft: true,
      status: "running",
      title: `草稿评测：${caseId}`,
      created_by: null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      finished_at: null,
      step: { node: "plan", title: "出方案" },
      waiting_since: null,
      wait_seconds: 0,
      pending: null,
      error: null,
      result: null,
      messages: [{ role: "user", content: `草稿评测：${caseId}` }],
    },
    readyAt: Date.now() + 6_000,
    steps: [],
    evaluation: { score },
  });
  return run_id;
}

function advanceFlow(entry: StoredRun): Run {
  const { run, flow } = entry;
  if (!flow) return run;
  while (flow.next) {
    const node = flow.config.graph.nodes.find((item) => item.id === flow.next);
    if (!node) {
      fail(
        entry,
        { id: flow.next, title: flow.next },
        `节点 ${flow.next} 不存在`,
        "invalid_config",
      );
      break;
    }
    flow.visits[node.id] = (flow.visits[node.id] ?? 0) + 1;
    run.step = { node: node.id, title: node.title };
    if (++flow.count > 200 || flow.visits[node.id] > (node.max_visits ?? 3)) {
      fail(
        entry,
        node,
        `节点「${node.title}」(${node.id})：超过执行次数上限`,
        flow.count > 200 ? "max_steps" : "max_visits",
      );
      break;
    }
    if (node.type === "human") {
      setStatus(run, "waiting");
      run.pending = humanPending(node, flow.pool);
      addStep(
        entry.steps,
        node.id,
        structuredClone(flow.pool),
        null,
        run.pending,
      );
      return run;
    }
    try {
      const result =
        node.type === "start"
          ? { outputs: flow.input, branch: null }
          : simulateNode(node, flow.pool);
      if (node.type === "knowledge" || node.type === "agent") {
        const sources = (run.sources ??= []);
        const hits = (result.outputs.hits as Reference[]).map((hit) => {
          const existing = sources.find(
            (source) => source.chunk_id === hit.chunk_id,
          );
          const source: RunSource = {
            ...hit,
            index:
              existing?.index ??
              Math.max(0, ...sources.map((item) => item.index)) + 1,
            node: node.id,
            node_title: node.title,
          };
          if (existing) Object.assign(existing, source);
          else sources.push(source);
          return source;
        });
        result.outputs.hits =
          node.type === "agent"
            ? [...hits].sort((a, b) => a.index - b.index)
            : hits;
        if (node.type === "knowledge")
          result.outputs.context = hits
            .map(
              (hit) =>
                `[${hit.index}]《${hit.document_name}》${hit.heading}${hit.page === null ? "" : `（第 ${hit.page} 页）`}\n${hit.text}`,
            )
            .join("\n\n");
        else if (hits.length)
          result.outputs.text = `根据检索到的资料：${hits.map((hit) => `${hit.text} [${hit.index}]`).join("\n\n")}`;
      }
      flow.pool[node.id] = result.outputs;
      addStep(entry.steps, node.id, structuredClone(flow.pool), result.outputs);
      const said = nodeSaid(node, result.outputs, result.branch);
      if (said)
        run.messages.push(stamp(node, { role: "assistant", content: said }));
      if (node.type === "end") {
        setStatus(run, "done");
        run.result = result.outputs;
        flow.next = "";
        return run;
      }
      flow.next =
        flow.config.graph.edges.find(
          (edge) =>
            edge.from === node.id && (edge.branch ?? null) === result.branch,
        )?.to ?? "";
      if (!flow.next) throw new Error("分支没有连接");
    } catch (error) {
      // Like a tool or model failure: the node is tried again on a retry.
      fail(
        entry,
        node,
        `节点「${node.title}」(${node.id})：${error instanceof Error ? error.message : "模拟执行失败"}`,
        error instanceof MockNodeFailure ? error.type : "tool_error",
      );
      break;
    }
  }
  return run;
}

function refresh(entry: StoredRun): Run {
  const { run } = entry;
  if (run.status !== "running" || Date.now() < entry.readyAt) return run;
  if (entry.flow) return advanceFlow(entry);
  if (entry.evaluation) {
    setStatus(run, "done");
    run.result = { score: entry.evaluation.score, threshold: 0.8 };
    run.messages.push({
      role: "assistant",
      content: `评测完成，得分 ${entry.evaluation.score}`,
    });
    addStep(entry.steps, "plan", { case: run.messages[0].content }, run.result);
    return run;
  }
  if (entry.etl) {
    const etl = entry.etl;
    // Like the backend's 探查数据 step: ETL reads data sources, not tool services.
    const service = mockToolService(etl.source);
    if (service && etl.stage === "plan" && !entry.decision) {
      run.step = { node: "profile", title: "探查数据" };
      fail(
        entry,
        { id: "profile", title: "探查数据" },
        `「${service.name}」是工具服务，ETL 要选数据库或上传文件的数据源`,
        "input_error",
      );
      return run;
    }
    if (etl.stage === "plan" && entry.decision !== "approve") {
      const plan = etl.upload
        ? mockUploadedPlan(etl.upload, etl.language, entry.comment)
        : mockEtlPlan(etl.language, entry.comment);
      const references = searchMockKnowledge(
        etl.knowledgeBases ?? [],
        run.messages[0].content,
        etl.knowledgeTopK ?? 5,
      ).map((hit, index) => ({ ...hit, index: index + 1 }));
      // This mock plan cites [1]; the run still records every retrieved source.
      plan.references = references.slice(0, 1);
      if (references.length) {
        plan.summary += " 业务口径按资料执行 [1]。";
        plan.assumptions[0] += " [1]";
        plan.targets[0].description += " [1]";
        plan.targets[0].columns[0].description += " [1]";
      }
      const input = { input: { request: run.messages[0].content } };
      if (!entry.decision)
        addStep(entry.steps, "profile", input, {
          source_name:
            allSources().find((item) => item.datasource_id === etl.source)
              ?.name ?? etl.source,
          profile: etl.upload
            ? etl.upload.tables.map((table) => ({
                name: `${etl.upload!.schema}.${table.table}`,
              }))
            : [{ name: "public.orders" }, { name: "public.customers" }],
        });
      addStep(entry.steps, "plan", input, { plan });
      etl.plan = plan;
      setStatus(run, "waiting");
      run.step = { node: "review", title: "确认方案" };
      run.messages.push({ role: "assistant", content: plan.summary });
      run.pending = {
        kind: "approval",
        prompt: "请检查 ETL 方案，确认后生成管道代码。",
        options: ["approve", "reject"],
        details: { type: "etl_plan", plan },
      };
      addStep(entry.steps, "review", { ...input, plan }, null, run.pending);
    } else if (
      etl.plan &&
      (etl.stage === "plan" || entry.decision === "reject")
    ) {
      // The plan was approved, or the release was turned down and the code rewritten.
      if (etl.stage === "release") etl.version += 1;
      etl.stage = "release";
      const result = mockEtlResult(etl.plan, etl.approvals, etl.version);
      const input = { plan: etl.plan };
      addStep(entry.steps, "codegen", input, { code: {}, problems: [] });
      addStep(entry.steps, "package", input, {
        result: { bundle: "etl_demo" },
      });
      addStep(entry.steps, "verify", input, {
        result: { verification: result.verification },
      });
      addStep(entry.steps, "publish", input, {
        result: { asset: result.asset },
      });
      setStatus(run, "waiting");
      run.step = { node: "release", title: "上线审批" };
      run.result = result;
      run.messages.push({
        role: "assistant",
        content: "dev 试跑通过（本地模拟）。",
      });
      run.pending = {
        kind: "approval",
        prompt:
          "dev 试跑已通过。确认把 etl_demo 上线到 prod（prod_catalog.etl，按计划 0 0 2 * * ? 运行）吗？" +
          "驳回时写下修改意见，代码会按意见重写。",
        options: ["approve", "reject"],
        comment_required: ["reject"],
        details: {
          type: "etl_release",
          bundle: "etl_demo",
          verification: result.verification,
          target: mockProdTarget,
        },
      };
      addStep(entry.steps, "release", input, null, run.pending);
    } else if (etl.plan) {
      setStatus(run, "done");
      run.step = { node: "deploy_prod", title: "上线 prod" };
      run.result = {
        ...mockEtlResult(etl.plan, etl.approvals, etl.version),
        release: mockDeployed,
      };
      addStep(
        entry.steps,
        "deploy_prod",
        { plan: etl.plan },
        {
          result: { release: mockDeployed },
        },
      );
      run.messages.push({
        role: "assistant",
        content: "已上线到 prod：etl_demo 部署为 Job 101（本地模拟）。",
      });
    }
    return run;
  }

  return run;
}

function json(value: unknown, status = 200) {
  return Response.json(value, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

// Create a run, or a copy of one: `copy` names the run it copies and, for a
// draft's run, the configuration it ran with.
function create(
  body: unknown,
  copy?: { rerunOf: string; config?: AppConfig },
): Response {
  if (!isRecord(body) || !isRecord(body.input)) {
    return json({ detail: "Provide app and input" }, 422);
  }
  if (typeof body.app !== "string") return json({ detail: "应用不存在" }, 404);
  if (body.draft !== undefined && typeof body.draft !== "boolean")
    return invalid([{ loc: ["body", "draft"], msg: "draft 必须为布尔值" }]);
  if (body.draft === true && body.app_version !== undefined)
    return json({ detail: "草稿运行不能同时指定 app_version" }, 422);
  if (
    body.requester !== undefined &&
    body.requester !== null &&
    (typeof body.requester !== "string" || body.requester.length > 100)
  )
    return invalid([
      { loc: ["body", "requester"], msg: "名字最多 100 个字符" },
    ]);
  const available = mockAppSnapshot(body.app);
  if (!available) return json({ detail: "应用不存在" }, 404);
  const found = mockAppSnapshot(
    body.app,
    body.app_version as number | undefined,
    body.draft === true,
  );
  // A copy of a draft's run uses the configuration that run had, and no version.
  const snapshot =
    found && copy?.config
      ? { ...found, config: copy.config, version: null }
      : found;
  if (!snapshot)
    return json(
      { detail: body.draft ? "请先保存应用草稿" : "应用版本不存在" },
      body.draft ? 409 : 404,
    );
  const flow = isFlowConfig(snapshot.config) ? snapshot.config : null;
  const fields =
    flow?.graph.nodes.find((node) => node.type === "start")?.inputs ?? [];
  const input = flow
    ? inputPayload(fields, { ...defaultValues(fields), ...body.input })
    : body.input;
  if (flow) {
    const issues = validateFields(fields, input, "input");
    for (const field of fields.filter((item) => item.type === "datasource"))
      if (
        input[field.key] &&
        !allSources().some(
          (source) => source.datasource_id === input[field.key],
        )
      )
        issues.push({
          loc: ["body", "input", field.key],
          msg: "请选择已登记的数据源",
        });
    if (issues.length) return invalid(issues);
  }
  const isEtl = body.app === "etl";
  // Like the backend: a flow app's run starts with the first text filled in.
  const text = isEtl
    ? input.request
    : fields
        .filter((field) => ["text", "textarea"].includes(field.type))
        .map((field) => input[field.key])
        .find((value) => typeof value === "string" && value.trim());
  if (
    isEtl &&
    (typeof text !== "string" ||
      !text.trim() ||
      !allSources().some(
        (source) => source.datasource_id === input.datasource_id,
      ) ||
      (input.language !== undefined &&
        !["pyspark", "sql"].includes(input.language as string)))
  )
    return json({ detail: "Invalid app or input" }, 422);
  const created = new Date().toISOString();
  const requester =
    typeof body.requester === "string" && body.requester.trim()
      ? body.requester.trim()
      : null;
  const start = flow?.graph.nodes.find((node) => node.type === "start");
  const run: Run = {
    run_id: randomUUID(),
    app: body.app,
    app_title: snapshot.title,
    app_version: snapshot.version,
    draft: body.draft === true || Boolean(copy?.config),
    status: "running",
    input,
    rerun_of: copy?.rerunOf ?? null,
    cancelled_by: null,
    // Like the backend: the first text input that was filled in.
    title: isEtl
      ? runTitle(input.request)
      : (fields
          .filter((field) => ["text", "textarea"].includes(field.type))
          .map((field) => runTitle(input[field.key]))
          .find(Boolean) ?? null),
    created_by: requester,
    created_at: created,
    updated_at: created,
    finished_at: null,
    step: null,
    waiting_since: null,
    wait_seconds: 0,
    // The etl code app's messages say nothing of steps, as in the backend.
    messages:
      typeof text === "string" && text.trim()
        ? [
            start
              ? stamp(
                  start,
                  { role: "user", content: text },
                  requester,
                  created,
                )
              : { role: "user", content: text },
          ]
        : [],
    pending: null,
    result: null,
    error: null,
    trace_id: isEtl ? randomUUID().replaceAll("-", "") : null,
  };
  store.set(run.run_id, {
    run,
    config: snapshot.config,
    manual: buildMockManual(
      body.app,
      snapshot.title,
      snapshot.summary,
      snapshot.version,
      snapshot.config,
    ),
    readyAt: Date.now() + 600,
    steps: [],
    flow: flow
      ? {
          config: flow,
          pool: {
            env: Object.fromEntries(
              flow.env.vars.map((item) => [item.key, item.value]),
            ),
            secret: Object.fromEntries(
              snapshot.secrets.map((name) => [name, "******"]),
            ),
          },
          next: start!.id,
          visits: {},
          count: 0,
          input,
          title: snapshot.title,
          summary: snapshot.summary,
        }
      : undefined,
    etl: isEtl
      ? {
          language: input.language === "sql" ? "sql" : "pyspark",
          knowledgeBases:
            !isFlowConfig(snapshot.config) &&
            Array.isArray(snapshot.config.nodes.plan?.params.knowledge_bases)
              ? snapshot.config.nodes.plan.params.knowledge_bases
              : [],
          knowledgeTopK:
            !isFlowConfig(snapshot.config) &&
            typeof snapshot.config.nodes.plan?.params.knowledge_top_k ===
              "number"
              ? snapshot.config.nodes.plan.params.knowledge_top_k
              : 5,
          stage: "plan",
          version: 1,
          approvals: [],
          source: String(input.datasource_id),
          upload:
            typeof input.datasource_id === "string"
              ? mockUploads.get(input.datasource_id)
              : undefined,
        }
      : undefined,
  });
  return json({ run_id: run.run_id, status: run.status }, 201);
}

export async function mockRuns(request: Request, path: string[]) {
  seedRetailRun();
  if (path[0] === "datasources")
    return path[1] === "files"
      ? mockUpload(request)
      : mockDatasources(request, path);
  if (request.method === "GET" && path.length === 1) {
    return json([...store.values()].reverse().map(refresh).map(summary));
  }
  let body: unknown;
  if (request.method === "POST") {
    try {
      body = await request.json();
    } catch {
      return json({ detail: "Invalid JSON" }, 422);
    }
  }
  if (request.method === "POST" && path.length === 1) return create(body);
  const entry = store.get(path[1]);
  if (!entry) return json({ detail: "Run not found" }, 404);
  const run = refresh(entry);
  if (request.method === "POST" && path[2] === "cancel") {
    if (
      !isRecord(body) ||
      (body.actor != null &&
        (typeof body.actor !== "string" || body.actor.length > 100)) ||
      (body.reason != null &&
        (typeof body.reason !== "string" || body.reason.length > 500))
    )
      return json({ detail: "Invalid actor or reason" }, 422);
    if (run.status !== "running" && run.status !== "waiting")
      return json({ detail: "只有运行中或等待确认的运行可以取消" }, 409);
    setStatus(run, "cancelled");
    run.pending = null;
    run.cancelled_by =
      typeof body.actor === "string" && body.actor.trim()
        ? body.actor.trim()
        : null;
    run.error =
      typeof body.reason === "string" && body.reason.trim()
        ? body.reason.trim()
        : null;
    return json({ run_id: run.run_id, status: run.status }, 202);
  }
  if (request.method === "POST" && path[2] === "retry") {
    if (
      !isRecord(body) ||
      (body.actor != null && typeof body.actor !== "string")
    )
      return json({ detail: "Invalid actor" }, 422);
    if (run.status !== "error")
      return json(
        { detail: "这次运行已经不在失败状态，可能有人已经重试过" },
        409,
      );
    if (!retryable(entry) || !entry.flow)
      return json({ detail: "这次运行不能重试，请复制后重新发起" }, 409);
    // The failed node runs again, as if it had not been tried.
    const { flow } = entry;
    flow.visits[flow.next] = Math.max(0, (flow.visits[flow.next] ?? 1) - 1);
    flow.count = Math.max(0, flow.count - 1);
    entry.failure = undefined;
    run.error = null;
    setStatus(run, "running");
    entry.readyAt = Date.now() + 300;
    return json({ run_id: run.run_id, status: run.status }, 202);
  }
  if (request.method === "POST" && path[2] === "rerun") {
    if (
      !isRecord(body) ||
      (body.input != null && !isRecord(body.input)) ||
      (body.requester != null && typeof body.requester !== "string")
    )
      return json({ detail: "Invalid input or requester" }, 422);
    if (!mockAppSnapshot(run.app)) return json({ detail: "应用不存在" }, 404);
    return create(
      {
        app: run.app,
        input: isRecord(body.input) ? body.input : run.input,
        requester: body.requester ?? undefined,
        ...(run.draft || run.app_version == null
          ? {}
          : { app_version: run.app_version }),
      },
      {
        rerunOf: run.run_id,
        config: run.draft ? (entry.flow?.config ?? entry.config) : undefined,
      },
    );
  }
  if (request.method === "GET" && path[2] === "manual") {
    if (entry.manual) return json(entry.manual);
    const snapshot = mockAppSnapshot(
      run.app,
      run.app_version ?? undefined,
      run.draft,
    );
    return snapshot
      ? json(
          buildMockManual(
            run.app,
            snapshot.title,
            snapshot.summary,
            snapshot.version,
            snapshot.config,
          ),
        )
      : json({ detail: "应用说明书不存在" }, 404);
  }
  if (request.method === "GET" && path[2] === "files.zip") {
    const result = getEtlResult(run.result);
    if (!result || Object.keys(result.files).length === 0)
      return json({ detail: "This run has no files" }, 404);
    return new Response(mockZip(result.files), {
      headers: {
        "Content-Type": "application/zip",
        "Content-Disposition": `attachment; filename="${result.bundle}.zip"`,
        "Cache-Control": "no-store",
      },
    });
  }
  if (request.method === "GET" && path[2] === "flow") {
    // Like the backend: a cancelled run's unfinished step shows where it stopped.
    const steps =
      run.status === "cancelled"
        ? entry.steps.map((step) =>
            step.status === "done"
              ? step
              : { ...step, status: "stopped" as const },
          )
        : entry.steps;
    const stopped = steps.at(-1)?.status === "stopped" ? steps.at(-1) : null;
    if (!entry.flow)
      return json(mockFlow(run.app, steps, run.status === "done"));
    const view = flowView(
      entry.flow.config,
      entry.flow.title,
      entry.flow.summary,
    );
    return json({
      ...view,
      // Like the backend: a flow app's run shows only its own title and summary.
      design: view.design && {
        ...view.design,
        pattern: "",
        knowledge: "",
        skills: "",
      },
      steps,
      current:
        run.status === "waiting" ? entry.flow.next : (stopped?.node ?? null),
    });
  }
  if (request.method === "GET") return json(detail(entry));
  if (
    !isRecord(body) ||
    typeof body.decision !== "string" ||
    (body.comment !== undefined && typeof body.comment !== "string") ||
    (body.actor !== undefined && typeof body.actor !== "string")
  ) {
    return json({ detail: "Invalid decision, comment or actor" }, 422);
  }
  if (run.status !== "waiting")
    return json({ detail: "Run is not waiting for approval" }, 409);
  if (!run.pending?.options.includes(body.decision))
    return invalid([{ loc: ["body", "decision"], msg: "请选择有效操作" }]);
  if (
    run.pending.comment_required?.includes(body.decision) &&
    (typeof body.comment !== "string" || !body.comment.trim())
  )
    return invalid([{ loc: ["body", "comment"], msg: "请填写处理意见" }]);
  if (entry.flow) {
    const pending = run.pending;
    const values = isRecord(body.values)
      ? { ...defaultValues(pending.fields ?? []), ...body.values }
      : defaultValues(pending.fields ?? []);
    if (pending.kind === "form") {
      const issues = validateFields(pending.fields ?? [], values, "values");
      if (issues.length) return invalid(issues);
    }
    const node = entry.flow.next;
    const output =
      pending.kind === "form"
        ? {
            ...inputPayload(pending.fields ?? [], values),
            actor: typeof body.actor === "string" ? body.actor : "",
          }
        : {
            decision: body.decision,
            comment: body.comment ?? "",
            actor: typeof body.actor === "string" ? body.actor : "",
          };
    entry.flow.pool[node] = output;
    decideStep(entry.steps, output);
    const step = entry.steps.at(-1);
    if (step) step.output = output;
    entry.flow.next =
      entry.flow.config.graph.edges.find(
        (edge) =>
          edge.from === node &&
          (edge.branch ?? null) ===
            (pending.kind === "form" ? null : body.decision),
      )?.to ?? "";
    const human = entry.flow.config.graph.nodes.find(
      (item) => item.id === node,
    );
    run.messages.push(
      stamp(
        { id: node, title: human?.title ?? node },
        {
          role: "user",
          content:
            pending.kind === "form"
              ? formSaid(
                  pending.fields ?? [],
                  inputPayload(pending.fields ?? [], values),
                )
              : approvalSaid(
                  pending.option_labels?.[body.decision] ?? body.decision,
                  body.comment,
                ),
        },
        typeof body.actor === "string" ? body.actor : null,
      ),
    );
    run.pending = null;
    setStatus(run, "running");
    entry.readyAt = Date.now() + 100;
    return json({ run_id: run.run_id, status: run.status }, 202);
  }
  entry.decision = body.decision as Decision;
  entry.comment =
    typeof body.comment === "string" ? body.comment.trim() : undefined;
  const approval: MockApproval = {
    step: entry.etl?.stage ?? "plan",
    decision: entry.decision as "approve" | "reject",
    comment: entry.comment ?? "",
    actor: typeof body.actor === "string" ? body.actor.trim() : "",
    at: new Date().toISOString(),
  };
  entry.etl?.approvals.push(approval);
  decideStep(entry.steps, approval);
  run.pending = null;
  setStatus(run, "running");
  run.messages.push({
    role: "user",
    content:
      entry.decision === "approve"
        ? "确认"
        : `驳回${entry.comment ? `：${entry.comment}` : "。"}`,
  });
  entry.readyAt = Date.now() + 600;
  return json({ run_id: run.run_id, status: run.status }, 202);
}
