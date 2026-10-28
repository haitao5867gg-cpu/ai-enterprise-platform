// Local contract fixture for proxy tests. No database, network dependency or model.
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { Readable } from "node:stream";

const runs = new Map();

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
  if (path === "/datasources")
    return json([
      {
        datasource_id: "fixture-postgres",
        name: "契约示例库",
        kind: "postgres",
        description: "合成测试数据源",
      },
    ]);
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
    const run = {
      run_id: randomUUID(),
      app: body.app,
      status: "waiting",
      messages: [
        { role: "user", content: body.input.text },
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
    return json({ run_id: run.run_id, status: "running" }, 201);
  }
  if (path === "/runs") return json([...runs.values()].reverse());
  const run = runs.get(path?.split("/")[2]);
  if (!run) return json({ detail: "Run not found" }, 404);
  if (request.method === "GET") return json(run);
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
