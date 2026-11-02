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

// What the API needs from the browser's request: the body's type, the sign-in cookie
// and the client's address for the record of changes. Nothing else is passed on.
function forwarded(request: Request, hasBody: boolean, isUpload: boolean) {
  const headers: Record<string, string> = {};
  if (hasBody)
    headers["Content-Type"] = isUpload
      ? request.headers.get("content-type")!
      : "application/json";
  const session = request.headers
    .get("cookie")
    ?.split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("aep_session="));
  if (session) headers.Cookie = session;
  const client = request.headers.get("x-forwarded-for");
  if (client) headers["X-Forwarded-For"] = client;
  return headers;
}

async function handle(request: Request, context: Context) {
  const { path } = await context.params;
  const endpoint = path.join("/");
  const routes: [RegExp, string[]][] = [
    [/^readyz$/, ["GET"]],
    // `files` and `test` come before the id rule: the first match wins.
    [/^datasources$/, ["GET", "POST"]],
    [/^datasources\/(files|test)$/, ["POST"]],
    [/^datasources\/[a-zA-Z0-9_-]{1,128}$/, ["GET", "PATCH"]],
    [/^datasources\/[a-zA-Z0-9_-]{1,128}\/(tools|health|usage)$/, ["GET"]],
    // Sign-in, accounts and the record of changes (docs/api/auth.md).
    [/^auth\/(status|me)$/, ["GET"]],
    [/^auth\/(setup|login|logout)$/, ["POST"]],
    [/^auth\/me\/password$/, ["PUT"]],
    [/^users$/, ["GET", "POST"]],
    [/^users\/[a-zA-Z0-9_-]{1,64}$/, ["PATCH", "DELETE"]],
    [/^users\/[a-zA-Z0-9_-]{1,64}\/password$/, ["PUT"]],
    [/^audit$/, ["GET"]],
    // Notifications and their channels (docs/api/notifications.md).
    [/^notifications$/, ["GET"]],
    [/^notifications\/read$/, ["POST"]],
    [/^notification-channels$/, ["GET"]],
    [/^webhooks$/, ["POST"]],
    [/^webhooks\/[a-zA-Z0-9_-]{1,64}$/, ["PATCH", "DELETE"]],
    [/^webhooks\/[a-zA-Z0-9_-]{1,64}\/test$/, ["POST"]],
    // `defaults` comes before the alias rule: the first match wins.
    [/^models$/, ["GET", "POST"]],
    [/^models\/defaults$/, ["PUT"]],
    [/^models\/[a-z][a-z0-9_-]{1,39}$/, ["GET", "PATCH", "DELETE"]],
    [/^models\/[a-z][a-z0-9_-]{1,39}\/test$/, ["POST"]],
    [/^models\/[a-z][a-z0-9_-]{1,39}\/usage$/, ["GET"]],
    // `options` comes before the id rule: the first match wins.
    [/^projects$/, ["GET", "POST"]],
    [/^projects\/options$/, ["GET"]],
    [/^projects\/[a-z][a-z0-9-]{1,39}$/, ["GET", "PUT", "DELETE"]],
    [/^runs$/, ["GET", "POST"]],
    [/^runs\/[a-zA-Z0-9_-]{1,128}(\/(files\.zip|flow|manual))?$/, ["GET"]],
    [/^runs\/[a-zA-Z0-9_-]{1,128}\/(resume|cancel|retry|rerun)$/, ["POST"]],
    [/^knowledge-bases$/, ["GET", "POST"]],
    [/^knowledge-bases\/options$/, ["GET"]],
    [/^knowledge-bases\/[a-zA-Z0-9_-]{1,128}$/, ["GET", "PATCH", "DELETE"]],
    [/^knowledge-bases\/[a-zA-Z0-9_-]{1,128}\/documents$/, ["GET", "POST"]],
    [
      /^knowledge-bases\/[a-zA-Z0-9_-]{1,128}\/documents\/[a-zA-Z0-9_-]{1,128}$/,
      ["GET", "DELETE"],
    ],
    [
      /^knowledge-bases\/[a-zA-Z0-9_-]{1,128}\/documents\/[a-zA-Z0-9_-]{1,128}\/(file|chunks)$/,
      ["GET"],
    ],
    [
      /^knowledge-bases\/[a-zA-Z0-9_-]{1,128}\/documents\/[a-zA-Z0-9_-]{1,128}\/reindex$/,
      ["POST"],
    ],
    [/^knowledge-bases\/[a-zA-Z0-9_-]{1,128}\/(reindex|search)$/, ["POST"]],
    [/^(node-types|app-templates)$/, ["GET"]],
    [/^apps$/, ["GET", "POST"]],
    [/^apps\/import$/, ["POST"]],
    [/^apps\/[a-z][a-z0-9-]{1,63}$/, ["GET", "PATCH", "DELETE"]],
    [/^apps\/[a-zA-Z0-9_-]{1,128}\/manual$/, ["GET"]],
    [/^apps\/[a-z][a-z0-9-]{1,63}\/draft\/import$/, ["POST"]],
    [
      /^apps\/[a-z][a-z0-9-]{1,63}\/nodes\/[a-z][a-z0-9_]{0,31}\/test$/,
      ["POST"],
    ],
    [/^apps\/[a-z][a-z0-9-]{1,63}\/versions\/[1-9][0-9]*\/export$/, ["GET"]],
    [
      /^apps\/[a-z][a-z0-9-]{1,63}\/secrets\/[a-z][a-z0-9_]{0,31}$/,
      ["PUT", "DELETE"],
    ],
    [/^apps\/[a-zA-Z0-9_-]{1,128}(\/versions)?$/, ["GET"]],
    [/^apps\/[a-zA-Z0-9_-]{1,128}\/draft$/, ["PUT", "DELETE"]],
    [/^apps\/[a-zA-Z0-9_-]{1,128}\/draft\/(eval|publish)$/, ["POST"]],
    [/^apps\/[a-zA-Z0-9_-]{1,128}\/versions\/[1-9][0-9]*\/activate$/, ["POST"]],
    [/^skills$/, ["GET", "POST"]],
    [/^skills\/[a-z][a-z0-9-]{1,63}(\/versions\/[1-9][0-9]*)?$/, ["GET"]],
    [/^skills\/[a-z][a-z0-9-]{1,63}\/draft$/, ["PUT", "DELETE"]],
    [/^skills\/[a-z][a-z0-9-]{1,63}\/publish$/, ["POST"]],
  ];
  const isUpload =
    request.method === "POST" &&
    (endpoint === "datasources/files" ||
      /^knowledge-bases\/[^/]+\/documents$/.test(endpoint));
  const methods = routes.find(([pattern]) => pattern.test(endpoint))?.[1];
  if (!methods) return jsonError("Not found", 404);
  if (!methods.includes(request.method)) {
    return new Response(null, {
      status: 405,
      headers: { Allow: methods.join(", ") },
    });
  }

  if (
    isUpload &&
    !request.headers
      .get("content-type")
      ?.toLowerCase()
      .startsWith("multipart/form-data;")
  )
    return jsonError("请使用 multipart 表单上传文件", 415);

  if (endpoint !== "readyz" && isLocalMockEnabled()) {
    if (["apps", "node-types", "app-templates"].includes(path[0])) {
      const { mockApps } = await import("../../../lib/server/mock-apps");
      return mockApps(request, path);
    }
    if (path[0] === "knowledge-bases") {
      const { mockKnowledge } =
        await import("../../../lib/server/mock-knowledge");
      return mockKnowledge(request, path);
    }
    if (
      ["notifications", "notification-channels", "webhooks"].includes(path[0])
    ) {
      const { mockNotifications } =
        await import("../../../lib/server/mock-notifications");
      return mockNotifications(request, path);
    }
    if (["auth", "users", "audit"].includes(path[0])) {
      const { mockAuth } = await import("../../../lib/server/mock-auth");
      return mockAuth(request, path);
    }
    if (path[0] === "models") {
      const { mockModelsApi } = await import("../../../lib/server/mock-models");
      return mockModelsApi(request, path);
    }
    if (path[0] === "projects") {
      const { mockProjects } =
        await import("../../../lib/server/mock-projects");
      return mockProjects(request, path);
    }
    if (path[0] === "skills") {
      const { mockSkills } = await import("../../../lib/server/mock-skills");
      return mockSkills(request, path);
    }
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
    const hasBody = ["POST", "PUT", "PATCH"].includes(request.method);
    const response = await fetch(
      `${base.href.replace(/\/$/, "")}/${endpoint}${new URL(request.url).search}`,
      {
        method: request.method,
        headers: forwarded(request, hasBody, isUpload),
        body: hasBody
          ? isUpload
            ? request.body
            : await request.text()
          : undefined,
        cache: "no-store",
        redirect: "manual",
        signal: AbortSignal.timeout(
          isUpload
            ? 300_000
            : /\/nodes\/[^/]+\/test$/.test(endpoint) ||
                // Testing a webhook waits up to 10 seconds for the other end.
                /^webhooks\/[^/]+\/test$/.test(endpoint) ||
                // Saving or testing a model calls it on the gateway first.
                (/^models(\/[^/]+(\/test)?)?$/.test(endpoint) &&
                  ["POST", "PATCH"].includes(request.method))
              ? 120_000
              : 10_000,
        ),
        ...(isUpload ? { duplex: "half" as const } : {}),
      } as RequestInit & { duplex?: "half" },
    );
    const headers = new Headers({
      "Content-Type":
        response.headers.get("Content-Type") ?? "application/json",
      "Cache-Control": "no-store",
    });
    const disposition = response.headers.get("Content-Disposition");
    if (disposition) headers.set("Content-Disposition", disposition);
    // The sign-in cookie is set and cleared by the API (docs/api/auth.md).
    for (const cookie of response.headers.getSetCookie())
      headers.append("Set-Cookie", cookie);
    // Stream upstream bytes unchanged, including ZIP archives and error responses.
    return new Response(response.body, { status: response.status, headers });
  } catch (error) {
    // Pages may show a 502 detail on its own (lib/runs.ts requestJson), so it is Chinese.
    return jsonError(
      "后端服务暂不可用。",
      error instanceof Error && error.name === "TimeoutError" ? 504 : 502,
    );
  }
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const DELETE = handle;

export const PATCH = handle;
