import "server-only";
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

type StoredRun = {
  run: Run;
  readyAt: number;
  decision?: Decision;
  comment?: string;
  etl?: {
    language: EtlLanguage;
    plan?: EtlPlan;
    // Which approval the run is at: the plan, then the release to prod.
    stage: "plan" | "release";
    version: number;
    approvals: MockApproval[];
  };
  steps: MockSteps;
};
// Next dev may reload this module. One process-wide store keeps route requests consistent.
const globalStore = globalThis as typeof globalThis & {
  aepMockRuns?: Map<string, StoredRun>;
};
const store = (globalStore.aepMockRuns ??= new Map<string, StoredRun>());

function refresh(entry: StoredRun): Run {
  const { run } = entry;
  if (run.status !== "running" || Date.now() < entry.readyAt) return run;
  if (entry.etl) {
    const etl = entry.etl;
    if (etl.stage === "plan" && entry.decision !== "approve") {
      const plan = mockEtlPlan(etl.language, entry.comment);
      const input = { input: { request: run.messages[0].content } };
      if (!entry.decision)
        addStep(entry.steps, "profile", input, {
          source_name: "示例库",
          profile: [{ name: "public.orders" }, { name: "public.customers" }],
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
  if (entry.decision !== "approve") {
    run.status = "waiting";
    run.messages.push({
      role: "assistant",
      content:
        entry.decision === "reject"
          ? `已根据你的意见重新整理：\n\n${entry.comment || "请换一种说法"}\n\n请再次确认本次回答。`
          : `已收到你的需求：\n\n${run.messages[0].content}\n\nhello 示例已准备就绪，请确认本次回答。`,
    });
    run.pending = {
      kind: "approval",
      prompt: "确认这个回答吗？驳回时可以写下修改意见。",
      options: ["approve", "reject"],
    };
    const input = { input: { text: run.messages[0].content } };
    addStep(entry.steps, "draft", input, {
      draft: run.messages.at(-1)?.content,
    });
    addStep(entry.steps, "review", input, null, run.pending);
  } else {
    run.status = "done";
    run.result = {
      answer:
        run.messages.findLast((message) => message.role === "assistant")
          ?.content ?? "hello",
    };
  }
  return run;
}

function json(value: unknown, status = 200) {
  return Response.json(value, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

export async function mockRuns(request: Request, path: string[]) {
  if (path[0] === "datasources") return json(mockDataSources);
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
    const input = body.input;
    const isEtl = body.app === "etl";
    const text = isEtl ? input.request : input.text;
    if (
      (body.app !== "hello" && !isEtl) ||
      typeof text !== "string" ||
      !text.trim() ||
      (isEtl &&
        (!mockDataSources.some(
          (source) => source.datasource_id === input.datasource_id,
        ) ||
          (input.language !== undefined &&
            input.language !== "pyspark" &&
            input.language !== "sql")))
    )
      return json({ detail: "Invalid app or input" }, 422);
    const run: Run = {
      run_id: randomUUID(),
      app: isEtl ? "etl" : "hello",
      status: "running",
      messages: [{ role: "user", content: text.trim() }],
      pending: null,
      result: null,
      error: null,
      trace_id: isEtl ? randomUUID().replaceAll("-", "") : null,
    };
    store.set(run.run_id, {
      run,
      readyAt: Date.now() + 600,
      steps: [],
      etl: isEtl
        ? {
            language: input.language === "sql" ? "sql" : "pyspark",
            stage: "plan",
            version: 1,
            approvals: [],
          }
        : undefined,
    });
    return json({ run_id: run.run_id, status: run.status }, 201);
  }
  const entry = store.get(path[1]);
  if (!entry) return json({ detail: "Run not found" }, 404);
  const run = refresh(entry);
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
  if (request.method === "GET" && path[2] === "flow")
    return json(mockFlow(run.app, entry.steps, run.status === "done"));
  if (request.method === "GET") return json(run);
  if (
    !isRecord(body) ||
    (body.decision !== "approve" && body.decision !== "reject") ||
    (body.comment !== undefined && typeof body.comment !== "string") ||
    (body.actor !== undefined && typeof body.actor !== "string")
  ) {
    return json({ detail: "Invalid decision, comment or actor" }, 422);
  }
  if (run.status !== "waiting")
    return json({ detail: "Run is not waiting for approval" }, 409);
  entry.decision = body.decision as Decision;
  entry.comment =
    typeof body.comment === "string" ? body.comment.trim() : undefined;
  const approval: MockApproval = {
    step: entry.etl?.stage ?? "plan",
    decision: entry.decision,
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
