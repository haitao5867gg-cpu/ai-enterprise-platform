// Local contract fixture for proxy tests. No database, network dependency or model.
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { Readable } from "node:stream";

const runs = new Map();
function createRun(text) {
  const run = {
    run_id: randomUUID(),
    app: "hello",
    status: "waiting",
    messages: [
      { role: "user", content: text },
      { role: "assistant", content: "来自契约测试后端的消息。" },
    ],
    pending: {
      kind: "approval",
      prompt: "请确认本次运行。",
      options: ["approve", "reject"],
    },
    result: null,
    error: null,
  };
  runs.set(run.run_id, run);
  return run;
}

createServer(async (request, response) => {
  function json(value, status = 200) {
    response.writeHead(status, { "Content-Type": "application/json" });
    response.end(JSON.stringify(value));
  }
  const path = request.url?.replace(/^\/internal\/v1/, "");
  if (!request.url?.startsWith("/internal/v1/"))
    return json({ detail: "Wrong API base path" }, 404);
  if (path === "/readyz")
    return json({ status: "ok", source: "contract-fixture" });
  if (path?.startsWith("/knowledge-bases/")) {
    if (path.endsWith("/documents") && request.method === "POST") {
      const form = await new Request("http://fixture/knowledge-upload", {
        method: "POST",
        headers: request.headers,
        body: Readable.toWeb(request),
        duplex: "half",
      }).formData();
      return json(
        await Promise.all(
          form.getAll("files").map(async (file) => ({
            name: file.name,
            bytes: [...new Uint8Array(await file.arrayBuffer())],
          })),
        ),
        202,
      );
    }
    if (path.includes("/chunks?")) {
      const url = new URL(path, "http://fixture");
      return json({
        offset: url.searchParams.get("offset"),
        limit: url.searchParams.get("limit"),
      });
    }
    if (path.endsWith("/file")) {
      response.writeHead(200, {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": "attachment; filename=policy.pdf",
      });
      return response.end(Buffer.from([0x25, 0x50, 0x44, 0x46, 0xff, 0, 0xfe]));
    }
  }
  if (path === "/datasources/files" && request.method === "POST") {
    const form = await new Request("http://fixture/upload", {
      method: "POST",
      headers: request.headers,
      body: Readable.toWeb(request),
      duplex: "half",
    }).formData();
    if (form.get("name") === "invalid")
      return json({ detail: "文件「bad.csv」：表头重复" }, 422);
    const files = await Promise.all(
      form.getAll("files").map(async (file) => ({
        name: file.name,
        bytes: [...new Uint8Array(await file.arrayBuffer())],
      })),
    );
    return json({ name: form.get("name"), files }, 201);
  }
  if (path?.startsWith("/datasources")) {
    const url = new URL(path, "http://fixture");
    // The DataSource of docs/api/datasources.md, the same in the list and on its own.
    const record = {
      datasource_id: "fixture-postgres",
      name: "契约示例库",
      kind: "postgres",
      connector_url: "http://connector-postgres:8001/mcp",
      description: "合成测试数据源",
      created_at: "2026-10-28T02:00:00Z",
    };
    if (url.pathname === "/datasources" && request.method === "GET")
      return json([record]);
    let payload = null;
    if (["POST", "PATCH"].includes(request.method)) {
      let raw = "";
      for await (const chunk of request) raw += chunk;
      payload = JSON.parse(raw);
    }
    const tools = [
      "list_schemas",
      "list_tables",
      "describe_table",
      "table_stats",
      "sample_rows",
      "read_rows",
    ].map((name) => ({
      name,
      description: `${name} 契约工具`,
      input_schema: { type: "object", properties: {} },
      read_only: true,
    }));
    const refused = {
      code: "refused",
      message:
        "连接被拒绝：这个地址和端口上没有服务。请检查端口，或确认连接器已经启动。",
    };
    const rejected = payload?.connector_url?.includes("refused");
    if (url.pathname === "/datasources/test")
      return json({
        ok: !rejected,
        latency_ms: rejected ? null : 12,
        tools: rejected
          ? []
          : tools.map(({ name, description, read_only }) => ({
              name,
              description,
              read_only,
            })),
        missing_tools: [],
        problems: rejected ? [refused] : [],
        received: payload,
      });
    if (url.pathname === "/datasources")
      return rejected
        ? json({ detail: `测试连接没有通过。${refused.message}` }, 422)
        : json(
            { ...record, ...payload, datasource_id: "fixture-created" },
            201,
          );
    const [, , id, part, extra] = url.pathname.split("/");
    if (id !== "fixture-postgres" || extra !== undefined)
      return json({ detail: "没有找到这个数据源或工具服务" }, 404);
    if (part === undefined)
      return json(
        request.method === "PATCH" ? { ...record, ...payload } : record,
      );
    if (part === "health")
      return json({
        ok: true,
        latency_ms: 12,
        tool_count: 6,
        error: null,
        message: null,
        checked_at: "2026-10-28T02:00:00Z",
        fresh: url.searchParams.get("fresh") === "true",
      });
    if (part === "usage") return json({ apps: [] });
    if (part === "tools") return json(tools);
  }
  if (path === "/runs/zip-fixture/files.zip") {
    response.writeHead(200, {
      "Content-Type": "application/zip",
      "Content-Disposition":
        "attachment; filename=\"fixture.zip\"; filename*=UTF-8''%E6%B5%8B%E8%AF%95.zip",
    });
    // Deliberately includes invalid UTF-8 and NUL bytes to detect text conversion.
    return response.end(
      Buffer.from([0x50, 0x4b, 3, 4, 0, 0xff, 0xfe, 0x80, 0xc3, 0x28, 0]),
    );
  }
  if (path === "/runs/unavailable-run") return request.socket.destroy();
  if (path === "/runs/timeout-run") return;
  if (path === "/runs/fixture") return json({ source: "contract-fixture" });
  if (path === "/runs/fixture/flow")
    return json({ source: "contract-fixture-flow" });
  const appSummary = {
    app: "hello",
    title: "契约问答示例",
    summary: "契约测试应用",
    kind: "flow",
    created_at: "2026-10-28T02:00:00Z",
    current_version: 1,
    has_draft: false,
    has_eval_set: false,
  };
  if (path === "/apps" && request.method === "GET") return json([appSummary]);
  if (path === "/apps/hello" && request.method === "GET")
    return json({
      ...appSummary,
      pattern: "问答和人工确认",
      knowledge: "",
      skills_text: "",
      nodes: [],
      edges: [],
      inputs: [
        {
          key: "text",
          label: "你想完成什么？",
          type: "textarea",
          required: true,
          max_length: 2000,
        },
      ],
      secrets: [],
      options: {
        models: ["mock"],
        default_model: "mock",
        skills: [],
        datasources: [],
      },
      draft: null,
      current: {
        version: 1,
        status: "current",
        config: {
          schema_version: 1,
          env: { vars: [], secrets: [] },
          graph: { nodes: [], edges: [] },
        },
        note: "契约测试",
        created_by: null,
        created_at: "2026-10-28T02:00:00Z",
        eval: null,
      },
    });
  if (path === "/apps/hello/versions/1/export") {
    response.writeHead(200, {
      "Content-Type": "text/yaml; charset=utf-8",
      "Content-Disposition":
        "attachment; filename=\"hello-v1.yaml\"; filename*=UTF-8''%E9%97%AE%E7%AD%94.yaml",
    });
    return response.end("format: aep-app/1\ntitle: 问答\n");
  }
  if (/^\/(apps|skills|node-types|app-templates)(\/|$)/.test(path)) {
    if (request.method === "DELETE") {
      response.writeHead(204);
      return response.end();
    }
    let payload = null;
    if (["POST", "PUT", "PATCH"].includes(request.method)) {
      let raw = "";
      for await (const chunk of request) raw += chunk;
      payload = JSON.parse(raw);
    }
    if (path === "/apps/conflict/draft/publish")
      return json({ detail: "请先通过评测" }, 409);
    if (path === "/skills/invalid/draft")
      return json(
        { detail: [{ loc: ["body", "description"], msg: "说明不能为空" }] },
        422,
      );
    return json(
      {
        source: "assets-contract-fixture",
        method: request.method,
        path,
        payload,
      },
      path === "/skills" && request.method === "POST" ? 201 : 200,
    );
  }
  // Other runs have no flow here: the run page leaves the panel out.
  if (path?.endsWith("/flow")) return json({ detail: "Not found" }, 404);
  let body;
  if (request.method === "POST") {
    try {
      let raw = "";
      for await (const chunk of request) raw += chunk;
      body = JSON.parse(raw);
    } catch {
      return json({ detail: "Invalid JSON" }, 422);
    }
  }
  if (path === "/runs" && request.method === "POST") {
    if (
      body.app !== "hello" ||
      typeof body.input?.text !== "string" ||
      !body.input.text.trim()
    )
      return json({ detail: "Invalid input" }, 422);
    const run = createRun(body.input.text);
    return json({ run_id: run.run_id, status: "running" }, 201);
  }
  if (path === "/runs") return json([...runs.values()].reverse());
  const run = runs.get(path?.split("/")[2]);
  if (!run) return json({ detail: "Run not found" }, 404);
  if (request.method === "GET") return json(run);
  if (path?.endsWith("/cancel")) {
    if (!["running", "waiting"].includes(run.status))
      return json({ detail: "只有运行中或等待确认的运行可以取消" }, 409);
    run.status = "cancelled";
    run.pending = null;
    run.cancelled_by = body.actor ?? null;
    return json({ run_id: run.run_id, status: run.status }, 202);
  }
  // Runs here never fail, so none can be retried.
  if (path?.endsWith("/retry"))
    return json(
      { detail: "这次运行已经不在失败状态，可能有人已经重试过" },
      409,
    );
  if (path?.endsWith("/rerun")) {
    const copy = createRun(body.input?.text ?? run.messages[0].content);
    copy.rerun_of = run.run_id;
    return json({ run_id: copy.run_id, status: "running" }, 201);
  }
  if (path?.endsWith("/resume")) {
    if (run.status !== "waiting")
      return json({ detail: "Already resumed" }, 409);
    if (
      !["approve", "reject"].includes(body.decision) ||
      typeof body.comment !== "string"
    )
      return json({ detail: "Invalid decision" }, 422);
    run.messages.push({
      role: "user",
      content: `${body.decision}: ${body.comment}`,
    });
    if (body.decision === "approve") {
      run.status = "done";
      run.pending = null;
      run.result = { answer: "已确认并完成。", source: "contract-fixture" };
    } else {
      run.messages.push({
        role: "assistant",
        content: `已根据意见重新生成：${body.comment}`,
      });
    }
    return json({ run_id: run.run_id, status: "running" }, 202);
  }
  return json({ detail: "Not found" }, 404);
}).listen(3102, "127.0.0.1");
