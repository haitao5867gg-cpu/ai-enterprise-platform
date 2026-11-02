import { forwardExternal } from "../../../../lib/server/external-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

// The app API (docs/api/app-access.md): runs of one app, with its API key.
const ROUTES: [RegExp, string[]][] = [
  [/^apps\/[a-z][a-z0-9-]{1,63}\/runs$/, ["POST"]],
  [/^runs\/[a-zA-Z0-9_-]{1,128}$/, ["GET"]],
  [/^runs\/[a-zA-Z0-9_-]{1,128}\/answer$/, ["POST"]],
];

async function handle(request: Request, context: Context) {
  const endpoint = (await context.params).path.join("/");
  const methods = ROUTES.find(([pattern]) => pattern.test(endpoint))?.[1];
  if (!methods) return Response.json({ detail: "Not found" }, { status: 404 });
  if (!methods.includes(request.method))
    return new Response(null, {
      status: 405,
      headers: { Allow: methods.join(", ") },
    });
  return forwardExternal(request, `v1/${endpoint}`);
}

export const GET = handle;
export const POST = handle;
