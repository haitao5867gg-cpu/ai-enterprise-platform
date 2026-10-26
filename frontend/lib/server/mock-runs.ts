import "server-only";
import { randomUUID } from "node:crypto";
import { isRecord, type Decision, type Run } from "../runs";

type StoredRun = {
  run: Run;
  readyAt: number;
  decision?: Decision;
  comment?: string;
};
// Next dev may reload this module. One process-wide store keeps route requests consistent.
const globalStore = globalThis as typeof globalThis & {
  aepMockRuns?: Map<string, StoredRun>;
};
const store = (globalStore.aepMockRuns ??= new Map<string, StoredRun>());

function refresh(entry: StoredRun): Run {
  const { run } = entry;
  if (run.status !== "running" || Date.now() < entry.readyAt) return run;
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
    if (
      !isRecord(body) ||
      body.app !== "hello" ||
      !isRecord(body.input) ||
      typeof body.input.text !== "string" ||
      !body.input.text.trim()
    ) {
      return json({ detail: "Provide app=hello and input.text" }, 422);
    }
    const run: Run = {
      run_id: randomUUID(),
      app: "hello",
      status: "running",
      messages: [{ role: "user", content: body.input.text.trim() }],
      pending: null,
      result: null,
      error: null,
    };
    store.set(run.run_id, { run, readyAt: Date.now() + 600 });
    return json({ run_id: run.run_id, status: run.status }, 201);
  }
  const entry = store.get(path[1]);
  if (!entry) return json({ detail: "Run not found" }, 404);
  const run = refresh(entry);
  if (request.method === "GET") return json(run);
  if (
    !isRecord(body) ||
    (body.decision !== "approve" && body.decision !== "reject") ||
    (body.comment !== undefined && typeof body.comment !== "string")
  ) {
    return json({ detail: "Invalid decision or comment" }, 422);
  }
  if (run.status !== "waiting")
    return json({ detail: "Run is not waiting for approval" }, 409);
  entry.decision = body.decision as Decision;
  entry.comment =
    typeof body.comment === "string" ? body.comment.trim() : undefined;
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
