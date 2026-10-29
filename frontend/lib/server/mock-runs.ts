import "server-only";
import type { Reference, RunSource } from "../knowledge";
import { searchMockKnowledge } from "./mock-knowledge";
import { randomUUID } from "node:crypto";
import { isRecord, type Decision, type Run } from "../runs";
import { getEtlResult, type EtlLanguage, type EtlPlan } from "../etl";
import {
  mockDataSources,
  mockDeployed,
  mockEtlPlan,
  mockEtlResult,
  mockProdTarget,
  type MockApproval,
} from "./mock-etl";
import { mockZip } from "./mock-zip";
import { addStep, decideStep, mockFlow, type MockSteps } from "./mock-flow";
import { mockUpload, mockUploads, mockUploadedPlan } from "./mock-uploads";
import type { UploadedDataset } from "../uploads";
import { isFlowConfig, type FlowConfig } from "../apps";
import { defaultValues, inputPayload, validateFields } from "../input-fields";
import { mockAppSnapshot } from "./mock-apps";
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
import { lakehouseConfig } from "./mock-data-nodes";

type StoredRun = {
  run: Run;
  manual?: AppManual;
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
};
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
  for (const node of config.graph.nodes) {
    const output: Record<string, unknown> =
      node.type === "start"
        ? input
        : node.type === "human"
          ? { decision: "approve", comment: "按口径执行", actor: "演示用户" }
          : simulateNode(node, pool).outputs;
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
      app_version: 1,
      draft: false,
      status: "done",
      pending: null,
      result: pool.end,
      error: null,
      messages: [
        { role: "user", content: input.request },
        {
          role: "assistant",
          content: "数据转换已在 dev 运行通过，详见运行报告。",
        },
      ],
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
      app_version: null,
      draft: true,
      status: "running",
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
      run.status = "error";
      run.error = `节点 ${flow.next} 不存在`;
      break;
    }
    flow.visits[node.id] = (flow.visits[node.id] ?? 0) + 1;
    if (++flow.count > 200 || flow.visits[node.id] > (node.max_visits ?? 3)) {
      run.status = "error";
      run.error = `节点「${node.title}」(${node.id})：超过执行次数上限`;
      break;
    }
    if (node.type === "human") {
      run.status = "waiting";
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
      // Like the backend: JSON output is data for later nodes, not a reply.
      if (
        (node.type === "agent" ||
          (node.type === "llm" && node.output.kind === "text")) &&
        typeof result.outputs.text === "string"
      )
        run.messages.push({ role: "assistant", content: result.outputs.text });
      if (node.type === "end") {
        run.status = "done";
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
      run.status = "error";
      run.error = `节点「${node.title}」(${node.id})：${error instanceof Error ? error.message : "模拟执行失败"}`;
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
    run.status = "done";
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
        run.sources = references.map((hit) => ({
          ...hit,
          node: "plan",
          node_title: "出方案",
        }));
      }
      const input = { input: { request: run.messages[0].content } };
      if (!entry.decision)
        addStep(entry.steps, "profile", input, {
          source_name: etl.upload?.datasource.name ?? "示例库",
          profile: etl.upload
            ? etl.upload.tables.map((table) => ({
                name: `${etl.upload!.schema}.${table.table}`,
              }))
            : [{ name: "public.orders" }, { name: "public.customers" }],
        });
      addStep(entry.steps, "plan", input, { plan });
      etl.plan = plan;
      run.status = "waiting";
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
      run.status = "waiting";
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
      run.status = "done";
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

const table = {
  schema: { type: "string", description: "模式名，例如 public" },
  table: { type: "string", description: "表名" },
};
const mockTools = [
  {
    name: "list_schemas",
    description: "列出模式",
    input_schema: { type: "object", properties: {} },
  },
  {
    name: "list_tables",
    description: "列出某个模式下的表",
    input_schema: {
      type: "object",
      properties: { schema: table.schema },
      required: ["schema"],
    },
  },
  {
    name: "describe_table",
    description: "查看表结构",
    input_schema: {
      type: "object",
      properties: table,
      required: ["schema", "table"],
    },
  },
  {
    name: "sample_rows",
    description: "读取几行样本",
    input_schema: {
      type: "object",
      properties: {
        ...table,
        limit: { type: "integer", description: "最多几行" },
      },
      required: ["schema", "table"],
    },
  },
];

export async function mockRuns(request: Request, path: string[]) {
  seedRetailRun();
  if (path[0] === "datasources") {
    if (path[1] === "files") return mockUpload(request);
    if (path[2] === "tools") {
      const known = [
        ...mockDataSources,
        ...[...mockUploads.values()].map((item) => item.datasource),
      ].some((item) => item.datasource_id === path[1]);
      return known ? json(mockTools) : json({ detail: "数据源不存在" }, 404);
    }
    return json([
      ...mockDataSources,
      ...[...mockUploads.values()].map((item) => item.datasource),
    ]);
  }
  if (request.method === "GET" && path.length === 1) {
    return json([...store.values()].reverse().map(refresh));
  }
  let body: unknown;
  if (request.method === "POST") {
    try {
      body = await request.json();
    } catch {
      return json({ detail: "Invalid JSON" }, 422);
    }
  }
  if (request.method === "POST" && path.length === 1) {
    if (!isRecord(body) || !isRecord(body.input)) {
      return json({ detail: "Provide app and input" }, 422);
    }
    if (typeof body.app !== "string")
      return json({ detail: "应用不存在" }, 404);
    if (body.draft !== undefined && typeof body.draft !== "boolean")
      return invalid([{ loc: ["body", "draft"], msg: "draft 必须为布尔值" }]);
    if (body.draft === true && body.app_version !== undefined)
      return json({ detail: "草稿运行不能同时指定 app_version" }, 422);
    const available = mockAppSnapshot(body.app);
    if (!available) return json({ detail: "应用不存在" }, 404);
    const snapshot = mockAppSnapshot(
      body.app,
      body.app_version as number | undefined,
      body.draft === true,
    );
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
    const text = isEtl
      ? input.request
      : fields
          .filter((field) => ["text", "textarea"].includes(field.type))
          .map((field) => input[field.key])
          .filter((value) => typeof value === "string")
          .join("\n");
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
    const run: Run = {
      run_id: randomUUID(),
      app: body.app,
      app_version: snapshot.version,
      draft: body.draft === true,
      status: "running",
      messages:
        typeof text === "string" && text.trim()
          ? [{ role: "user", content: text.trim() }]
          : [],
      pending: null,
      result: null,
      error: null,
      trace_id: isEtl ? randomUUID().replaceAll("-", "") : null,
    };
    store.set(run.run_id, {
      run,
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
            next: flow.graph.nodes.find((node) => node.type === "start")!.id,
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
            upload:
              typeof input.datasource_id === "string"
                ? mockUploads.get(input.datasource_id)
                : undefined,
          }
        : undefined,
    });
    return json({ run_id: run.run_id, status: run.status }, 201);
  }
  const entry = store.get(path[1]);
  if (!entry) return json({ detail: "Run not found" }, 404);
  const run = refresh(entry);
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
    if (!entry.flow)
      return json(mockFlow(run.app, entry.steps, run.status === "done"));
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
      steps: entry.steps,
      current: run.status === "waiting" ? entry.flow.next : null,
    });
  }
  if (request.method === "GET") return json(run);
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
    run.messages.push({
      role: "user",
      content:
        pending.kind === "form"
          ? JSON.stringify(values)
          : `${pending.option_labels?.[body.decision] ?? body.decision}${body.comment ? `：${body.comment}` : ""}`,
    });
    run.pending = null;
    run.status = "running";
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
  run.status = "running";
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
