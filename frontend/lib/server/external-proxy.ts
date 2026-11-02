import "server-only";
import { isLocalMockEnabled } from "./config";

// The app API and each app's MCP endpoint (docs/api/app-access.md), for other systems:
// they carry an API key instead of the sign-in cookie, so only these headers go on.
const PASSED = [
  "authorization",
  "content-type",
  "accept",
  "mcp-session-id",
  "mcp-protocol-version",
  "last-event-id",
];
const RETURNED = [
  "content-type",
  "mcp-session-id",
  "mcp-protocol-version",
  "www-authenticate",
];

const jsonError = (detail: string, status: number) =>
  Response.json(
    { detail },
    { status, headers: { "Cache-Control": "no-store" } },
  );

export async function forwardExternal(request: Request, endpoint: string) {
  if (isLocalMockEnabled())
    return jsonError("本地模拟模式没有应用 API 和 MCP，请连真实后端试。", 501);
  const configuredUrl = process.env.AEP_API_INTERNAL_URL;
  if (!configuredUrl)
    return jsonError("AEP_API_INTERNAL_URL is not configured", 503);
  let base: URL;
  try {
    base = new URL(configuredUrl);
    if (!["http:", "https:"].includes(base.protocol)) throw new Error();
  } catch {
    return jsonError("Internal API configuration is invalid", 503);
  }
  const headers: Record<string, string> = {};
  for (const name of PASSED) {
    const value = request.headers.get(name);
    if (value) headers[name] = value;
  }
  const client = request.headers.get("x-forwarded-for");
  if (client) headers["X-Forwarded-For"] = client;
  const hasBody = !["GET", "HEAD", "DELETE"].includes(request.method);
  try {
    const response = await fetch(
      `${base.href.replace(/\/$/, "")}/${endpoint}${new URL(request.url).search}`,
      {
        method: request.method,
        headers,
        body: hasBody ? await request.text() : undefined,
        cache: "no-store",
        redirect: "manual",
        // A run may be waited for up to 60 seconds (MCP: 50).
        signal: AbortSignal.timeout(90_000),
      },
    );
    const out = new Headers({ "Cache-Control": "no-store" });
    for (const name of RETURNED) {
      const value = response.headers.get(name);
      if (value) out.set(name, value);
    }
    return new Response(response.body, {
      status: response.status,
      headers: out,
    });
  } catch (error) {
    return jsonError(
      "后端服务暂不可用。",
      error instanceof Error && error.name === "TimeoutError" ? 504 : 502,
    );
  }
}
