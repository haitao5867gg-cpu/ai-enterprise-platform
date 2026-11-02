import { forwardExternal } from "../../../../../lib/server/external-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Context = { params: Promise<{ app: string }> };

// Each app as an MCP tool (docs/api/app-access.md): stateless Streamable HTTP.
async function handle(request: Request, context: Context) {
  const { app } = await context.params;
  if (!/^[a-z][a-z0-9-]{1,63}$/.test(app))
    return Response.json({ detail: "Not found" }, { status: 404 });
  return forwardExternal(request, `mcp/apps/${app}`);
}

export const GET = handle;
export const POST = handle;
export const DELETE = handle;
