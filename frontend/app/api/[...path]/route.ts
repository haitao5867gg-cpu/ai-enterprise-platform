import { isLocalMockEnabled } from "../../../lib/server/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

function jsonError(detail: string, status: number) {
  return Response.json(
    { detail },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

async function handle(request: Request, context: Context) {
  const { path } = await context.params;
  const endpoint = path.join("/");
  const allowed =
    endpoint === "readyz" ||
    endpoint === "datasources" ||
    endpoint === "runs" ||
    /^runs\/[a-zA-Z0-9_-]{1,128}(\/(resume|files\.zip|flow))?$/.test(endpoint);
  if (!allowed) return jsonError("Not found", 404);
  const isResume = path.length === 3 && path[2] === "resume";
  const canGet = !isResume;
  const canPost = endpoint === "runs" || isResume;
  if (
    (request.method === "GET" && !canGet) ||
    (request.method === "POST" && !canPost)
  ) {
    return new Response(null, {
      status: 405,
      headers: { Allow: canGet ? "GET" : "POST" },
    });
  }

  if (endpoint !== "readyz" && isLocalMockEnabled()) {
    const { mockRuns } = await import("../../../lib/server/mock-runs");
    return mockRuns(request, path);
  }

  const configuredUrl = process.env.AEP_API_INTERNAL_URL;
  if (!configuredUrl)
    return jsonError("AEP_API_INTERNAL_URL is not configured", 503);
  let base: URL;
  try {
    base = new URL(configuredUrl);
    if (
      !["http:", "https:"].includes(base.protocol) ||
      base.username ||
      base.password ||
      base.search ||
      base.hash
    ) {
      throw new Error("Invalid internal API URL");
    }
  } catch {
    return jsonError("Internal API configuration is invalid", 503);
  }

  try {
    const response = await fetch(
      `${base.href.replace(/\/$/, "")}/${endpoint}`,
      {
        method: request.method,
        headers:
          request.method === "POST"
            ? { "Content-Type": "application/json" }
            : undefined,
        body: request.method === "POST" ? await request.text() : undefined,
        cache: "no-store",
        redirect: "manual",
        signal: AbortSignal.timeout(10_000),
      },
    );
    const headers = new Headers({
      "Content-Type":
        response.headers.get("Content-Type") ?? "application/json",
      "Cache-Control": "no-store",
    });
    const disposition = response.headers.get("Content-Disposition");
    if (disposition) headers.set("Content-Disposition", disposition);
    // Stream upstream bytes unchanged, including ZIP archives and error responses.
    return new Response(response.body, { status: response.status, headers });
  } catch (error) {
    return jsonError(
      "Internal API is unavailable",
      error instanceof Error && error.name === "TimeoutError" ? 504 : 502,
    );
  }
}

export const GET = handle;
export const POST = handle;
