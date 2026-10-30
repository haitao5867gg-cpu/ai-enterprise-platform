import "server-only";
import { randomUUID } from "node:crypto";
import type {
  ConnectionProblem,
  ConnectorTool,
  Datasource,
  Health,
  Problem,
  TestResult,
} from "../datasources";
import { isRecord } from "../runs";
import { mockError, mockJson } from "./mock-assets";
import { mockDataSources } from "./mock-etl";

// Mock of docs/api/datasources.md. Connectors are not called: what a connector offers
// follows from its host. Hosts under connector.test act out each case:
//   timeout / refused / not-mcp / nowhere   the four connection problems
//   partial   4 of the 6 ETL tools          tools   a ticket tool service
//   empty     no tools                      any other host   the 6 ETL tools
// Gaps against the backend: every *.connector.test host and port is allowed (the backend
// only allows what AEP_CONNECTOR_HOSTS names); the platform's own services are a fixed
// list, and loopback addresses are simply not allowed (the backend reads both from its
// settings), and their Kubernetes names (sandbox-relay.aep.svc…) are not recognised; a
// numeric host the URL parser rejects (08.1.2.3) is a format error rather than a host to
// allow; legacy addresses are not masked, since the mock never stores secrets.
const CREATED = "2026-01-05T08:00:00.000Z";
const SECONDS = 5;
const globalStore = globalThis as typeof globalThis & {
  aepMockDatasources?: Map<string, Datasource>;
  aepMockHealth?: Map<string, { url: string; at: number; health: Health }>;
};
const store = (globalStore.aepMockDatasources ??= new Map(
  [
    {
      ...mockDataSources[0],
      connector_url: "http://connector-postgres:8001/mcp",
      created_at: CREATED,
    },
    {
      datasource_id: "sample-mysql",
      name: "MySQL 示例库",
      kind: "mysql",
      connector_url: "http://connector-mysql:8002/mcp",
      description: "本地 MySQL 示例源库：sales 和 retail，都含脏数据",
      created_at: CREATED,
    },
    {
      datasource_id: "legacy-report",
      name: "旧报表库",
      kind: "mysql",
      connector_url: "http://refused.connector.test:8002/mcp",
      description: "已停用的报表库副本",
      created_at: CREATED,
    },
    {
      datasource_id: "ticket-tools",
      name: "工单系统",
      kind: "mcp",
      connector_url: "http://tools.connector.test:8010/mcp",
      description: "客服工单：查询和查看工单",
      created_at: CREATED,
    },
  ].map((source): [string, Datasource] => [source.datasource_id, source]),
));
// Per data source and address, 60 seconds, failures too.
const healthCache = (globalStore.aepMockHealth ??= new Map());

const byName = (a: Datasource, b: Datasource) =>
  a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
export const allMockSources = () => [...store.values()].sort(byName);
// The tool service with this id, if it is one: ETL and the data nodes refuse it.
export const mockToolService = (id: unknown) =>
  [...store.values()].find(
    (source) => source.datasource_id === id && source.kind === "mcp",
  );
// Uploads register here; the same object is kept, so a rename shows in the upload too.
export function registerMockSource(source: Datasource) {
  store.set(source.datasource_id, source);
  return source;
}

const table = {
  schema: { type: "string", description: "模式名，例如 public" },
  table: { type: "string", description: "表名" },
};
const tableArgs = {
  type: "object",
  properties: table,
  required: ["schema", "table"],
};
// The descriptions are connectors/postgres's own; the page shows Chinese ones.
const etlTools: ConnectorTool[] = [
  {
    name: "list_schemas",
    description:
      "List accessible, non-system schemas in the configured source database.",
    input_schema: { type: "object", properties: {} },
    read_only: true,
  },
  {
    name: "list_tables",
    description:
      "List readable base/partitioned tables in a schema; views are excluded.",
    input_schema: {
      type: "object",
      properties: { schema: table.schema },
      required: ["schema"],
    },
    read_only: true,
  },
  {
    name: "describe_table",
    description:
      "Return ordered columns, types, nullability, primary-key flags and foreign keys.",
    input_schema: tableArgs,
    read_only: true,
  },
  {
    name: "table_stats",
    description:
      "Return a planner row estimate and bounded-sample null fractions/distinct counts.",
    input_schema: tableArgs,
    read_only: true,
  },
  {
    name: "sample_rows",
    description:
      "Read at most 100 rows as JSON, without an ordering guarantee.",
    input_schema: {
      ...tableArgs,
      properties: {
        ...table,
        limit: { type: "integer", default: 10, minimum: 1, maximum: 100 },
      },
    },
    read_only: true,
  },
  {
    name: "read_rows",
    description:
      "Read a stable page ordered by primary key, or all columns when there is no key.",
    input_schema: {
      ...tableArgs,
      properties: {
        ...table,
        offset: { type: "integer", default: 0, minimum: 0 },
        limit: { type: "integer", default: 1000, minimum: 1, maximum: 5000 },
      },
    },
    read_only: true,
  },
];
const ETL_TOOLS = etlTools.map((tool) => tool.name);
// Read tools only; get_customer does not declare itself read-only.
const ticketTools: ConnectorTool[] = [
  {
    name: "search_tickets",
    description: "按关键字、客户或状态查询工单",
    input_schema: {
      type: "object",
      properties: {
        keyword: { type: "string", description: "关键字，匹配标题和内容" },
        customer_id: { type: "string", description: "客户编号" },
        status: { type: "string", enum: ["open", "pending", "closed"] },
        limit: { type: "integer", default: 20, minimum: 1, maximum: 100 },
      },
    },
    read_only: true,
  },
  {
    name: "get_ticket",
    description: "查看一张工单的详情和处理记录",
    input_schema: {
      type: "object",
      properties: { ticket_id: { type: "string", description: "工单号" } },
      required: ["ticket_id"],
    },
    read_only: true,
  },
  {
    name: "get_customer",
    description: "Look up a customer's profile and their open tickets.",
    input_schema: {
      type: "object",
      properties: { customer_id: { type: "string" } },
      required: ["customer_id"],
    },
    read_only: false,
  },
];

const BAD_URL =
  "地址格式不对。请填完整的连接器地址，例如 http://connector-postgres:8001/mcp：以 http:// 或 https:// 开头，不含空格、中文和 # 号，端口在 1～65535 之间。";
const DATABASE_URL =
  "这是数据库的地址。这里要填连接器的地址（一般以 /mcp 结尾），数据库地址和账号密码配在连接器上。";
const PROBLEMS: Record<string, string> = {
  credentials:
    "地址里不能带账号密码，也不能带 password、token、secret、key 这类参数。数据库账号密码配在连接器上，平台只保存地址。",
  metadata: "不能使用云服务器的元数据地址（如 169.254.169.254）。",
  internal_host:
    "这是平台内部服务的地址（平台本身、代码沙箱、模型网关、Langfuse、数据库或 Redis），不能登记。",
  timeout: `超时：连接器在 ${SECONDS} 秒内没有响应。请确认连接器已经启动，地址和端口没写错。`,
  refused:
    "连接被拒绝：这个地址和端口上没有服务。请检查端口，或确认连接器已经启动。",
  not_mcp:
    "不是 MCP 服务：能连上这个地址，但对方不是 MCP 连接器。请检查路径（一般以 /mcp 结尾）；如果确实是连接器，请让管理员在连接器上允许这个主机名。",
  other:
    "网络不通：找不到这个主机，或者网络不通，也可能是 http 和 https 写反了。请检查地址，必要时请管理员查看平台日志。",
  no_tools: "连接器没有提供任何工具，工具服务至少要有一个工具。",
};
const problem = (code: string, message = PROBLEMS[code]): Problem => ({
  code,
  message,
});
const KINDS = ["postgres", "mysql", "sqlserver", "file", "mcp"];
const LABELS: Record<string, string> = {
  name: "名称",
  kind: "类型",
  connector_url: "连接器地址",
  description: "说明",
};
const FILE_KIND = "上传文件的数据源请用「上传文件」登记，不用填地址";
const DATABASE_SCHEME =
  /^(postgres|postgresql|mysql|mariadb|sqlserver|mssql|oracle|mongodb)(\+[a-z0-9_]+)?:\/\/|^jdbc:/i;
const HOSTNAME = /^[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?$/;
const SENSITIVE = /pass|pwd|token|secret|key|auth|credential|signature/i;
const INTERNAL = [
  "sandbox-relay",
  "connector-sandbox",
  "litellm",
  "postgres",
  "redis",
  "source-postgres",
  "source-mysql",
  "api",
  "web",
];
const isMetadata = (host: string) =>
  /^169\.254\./.test(host) ||
  ["168.63.129.16", "100.100.100.200", "[fd00:ec2::254]"].includes(host) ||
  /^\[fe[89ab]/.test(host);
const ALLOWED = [
  "connector-postgres:8001",
  "connector-mysql:8002",
  "connector-files:8003",
];

function decoded(text: string) {
  try {
    return decodeURIComponent(text.replace(/\+/g, " "));
  } catch {
    return text;
  }
}
// "[::ffff:a9fe:a9fe]" is 169.254.169.254; the URL parser already turns numeric forms
// such as 2852039166, 0xa9fea9fe or 127.1 into dotted IPv4.
function unmapped(host: string) {
  const groups = host.match(/^\[::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})\]$/);
  if (!groups) return host;
  const [high, low] = [parseInt(groups[1], 16), parseInt(groups[2], 16)];
  return [high >> 8, high & 255, low >> 8, low & 255].join(".");
}
// Address rules 1-5 of the contract, in order; the normalised address when they pass.
function checkUrl(text: string): { url: string } | { problem: Problem } {
  const raw = text.trim();
  const bad = { problem: problem("bad_url", BAD_URL) };
  if (DATABASE_SCHEME.test(raw))
    return { problem: problem("bad_url", DATABASE_URL) };
  const authority = raw.match(/^https?:\/\/([^/?]*)/i)?.[1];
  let parsed: URL | null = null;
  try {
    parsed = new URL(raw);
  } catch {}
  if (
    authority === undefined ||
    !parsed ||
    raw.length > 2048 ||
    !/^[\x21-\x7e]+$/.test(raw) ||
    /[#\\]/.test(raw) ||
    !parsed.hostname ||
    parsed.port === "0"
  )
    return bad;
  // The host as written, before the URL parser rewrites it (127.1 to 127.0.0.1).
  const hostinfo = authority
    .slice(authority.lastIndexOf("@") + 1)
    .toLowerCase();
  const written = hostinfo.startsWith("[")
    ? hostinfo.slice(0, hostinfo.indexOf("]") + 1)
    : hostinfo.split(":")[0];
  const host = unmapped(parsed.hostname);
  const ipv4 = /^\d+\.\d+\.\d+\.\d+$/.test(host);
  // Another notation of an IPv4 address: metadata if it is one, otherwise a format error.
  if (ipv4 && !written.startsWith("[") && written !== host)
    return isMetadata(host) ? { problem: problem("metadata") } : bad;
  const first = ipv4 ? Number(host.split(".")[0]) : -1;
  if (
    (!ipv4 &&
      !written.startsWith("[") &&
      (!HOSTNAME.test(written) || written.includes(".."))) ||
    first === 0 ||
    (first >= 224 && first <= 239) ||
    host === "255.255.255.255" ||
    host === "[::]" ||
    /^\[ff[0-9a-f]{2}:/.test(host)
  )
    return bad;
  const keys = (raw.split("?")[1] ?? "")
    .split(/[&;]/)
    .map((part) => decoded(part.split("=")[0]).trim().toLowerCase());
  if (
    authority.includes("@") ||
    keys.some((key) => SENSITIVE.test(key) || key === "sig" || key === "code")
  )
    return { problem: problem("credentials") };
  if (isMetadata(host)) return { problem: problem("metadata") };
  if (INTERNAL.includes(host) || host.startsWith("langfuse-"))
    return { problem: problem("internal_host") };
  const port = parsed.port || (parsed.protocol === "https:" ? "443" : "80");
  if (!ALLOWED.includes(`${host}:${port}`) && !host.endsWith(".connector.test"))
    return {
      problem: problem(
        "host_not_allowed",
        `主机 ${host}:${port} 不在允许的范围内，请联系管理员把它加进 AEP_CONNECTOR_HOSTS。`,
      ),
    };
  return {
    url: `${parsed.protocol}//${parsed.host}${raw.replace(/^https?:\/\/[^/?]*/i, "")}`,
  };
}

function probe(
  url: string,
): { reason: ConnectionProblem } | { tools: ConnectorTool[] } {
  let host = "";
  try {
    host = new URL(url).hostname;
  } catch {
    return { reason: "other" };
  }
  const reasons: Record<string, ConnectionProblem> = {
    "timeout.connector.test": "timeout",
    "refused.connector.test": "refused",
    "not-mcp.connector.test": "not_mcp",
    "nowhere.connector.test": "other",
  };
  if (reasons[host]) return { reason: reasons[host] };
  if (host === "partial.connector.test")
    return {
      tools: etlTools.filter(
        (tool) => !["table_stats", "read_rows"].includes(tool.name),
      ),
    };
  if (host === "tools.connector.test") return { tools: ticketTools };
  if (host === "empty.connector.test") return { tools: [] };
  return { tools: etlTools };
}

export function mockConnectorTest(
  kind: string,
  raw: string,
): TestResult & { url: string | null } {
  const failed = (problems: Problem[], url: string | null = null) => ({
    ok: false,
    latency_ms: null,
    tools: [],
    missing_tools: [],
    problems,
    url,
  });
  const checked = checkUrl(raw);
  if ("problem" in checked) return failed([checked.problem]);
  const found = probe(checked.url);
  if ("reason" in found) return failed([problem(found.reason)], checked.url);
  const names = found.tools.map((tool) => tool.name);
  const missing =
    kind === "mcp" ? [] : ETL_TOOLS.filter((name) => !names.includes(name));
  const problems = missing.length
    ? [
        problem(
          "missing_tools",
          `缺少 ETL 需要的工具：${missing.join("、")}。数据源要提供 list_schemas、list_tables、describe_table、table_stats、sample_rows、read_rows 这 6 个工具；如果它只提供别的工具，请把类型选成「工具服务」。`,
        ),
      ]
    : kind === "mcp" && !names.length
      ? [problem("no_tools")]
      : [];
  return {
    ok: !problems.length,
    latency_ms: 35,
    tools: found.tools.map(({ name, description, read_only }) => ({
      name,
      description,
      read_only,
    })),
    missing_tools: missing,
    problems,
    url: checked.url,
  };
}

// What a check finds, cached per data source and address for 60 seconds, failures too.
function remember(source: Datasource, found: ReturnType<typeof probe>) {
  const checked_at = new Date().toISOString();
  const health: Health =
    "reason" in found
      ? {
          ok: false,
          latency_ms: null,
          tool_count: null,
          error: found.reason,
          message: PROBLEMS[found.reason],
          checked_at,
        }
      : {
          ok: true,
          latency_ms: 35,
          tool_count: found.tools.length,
          error: null,
          message: null,
          checked_at,
        };
  healthCache.set(source.datasource_id, {
    url: source.connector_url,
    at: Date.now(),
    health,
  });
  return health;
}
function mockHealth(source: Datasource, fresh: boolean): Health {
  const cached = healthCache.get(source.datasource_id);
  return !fresh &&
    cached &&
    cached.url === source.connector_url &&
    Date.now() - cached.at < 60_000
    ? cached.health
    : remember(source, probe(source.connector_url));
}

// The backend's 422 texts for request fields: null, then each field in turn.
function badFields(
  body: Record<string, unknown>,
  keys: string[],
  required: string[] = [],
): string | null {
  if (keys.some((key) => body[key] === null)) return "请求字段不能为 null";
  for (const key of keys) {
    const value = body[key];
    if (value === undefined && !required.includes(key)) continue;
    const ok =
      key === "kind"
        ? KINDS.includes(value as string)
        : typeof value === "string" &&
          (key === "name"
            ? value.trim().length >= 1 && value.trim().length <= 100
            : key !== "description" || value.length <= 2000);
    if (!ok) return `「${LABELS[key]}」的格式、长度或范围不正确`;
  }
  return null;
}
const nameTaken = (name: string, except?: string) =>
  [...store.values()].some(
    (item) => item.name === name && item.datasource_id !== except,
  );
export const duplicateName = (name: string) =>
  `已经有叫「${name}」的数据源或工具服务了`;
const failedTest = (result: TestResult) =>
  mockError(
    `测试连接没有通过。${result.problems.map((item) => item.message).join("")}`,
    422,
  );
// Same address once the scheme and host are lowercased and a trailing / is dropped.
function sameUrl(a: string, b: string) {
  const plain = (url: string) => {
    try {
      const parsed = new URL(url);
      return `${parsed.protocol}//${parsed.host}${url.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/?#]*/i, "")}`.replace(
        /\/$/,
        "",
      );
    } catch {
      return url;
    }
  };
  return plain(a) === plain(b);
}

export async function mockDatasources(request: Request, path: string[]) {
  let body: Record<string, unknown> = {};
  if (["POST", "PATCH"].includes(request.method)) {
    try {
      const value: unknown = await request.json();
      if (!isRecord(value)) throw new Error("not an object");
      body = value;
    } catch {
      return mockError("请求体必须是 JSON 对象", 422);
    }
  }
  if (path.length === 1 && request.method === "GET")
    return mockJson(allMockSources());
  if (path[1] === "test") {
    const issue = badFields(
      body,
      ["kind", "connector_url"],
      ["kind", "connector_url"],
    );
    if (issue) return mockError(issue, 422);
    if (body.kind === "file") return mockError(FILE_KIND, 422);
    const result = mockConnectorTest(
      body.kind as string,
      body.connector_url as string,
    );
    return mockJson({ ...result, url: undefined });
  }
  if (path.length === 1 && request.method === "POST") {
    const issue = badFields(body, Object.keys(LABELS), [
      "name",
      "kind",
      "connector_url",
    ]);
    if (issue) return mockError(issue, 422);
    if (body.kind === "file") return mockError(FILE_KIND, 422);
    const name = (body.name as string).trim();
    // A taken name is reported before the slow connection test.
    if (nameTaken(name)) return mockError(duplicateName(name), 409);
    const kind = body.kind as string;
    const result = mockConnectorTest(kind, body.connector_url as string);
    if (!result.ok || !result.url) return failedTest(result);
    const source = registerMockSource({
      datasource_id: randomUUID(),
      name,
      kind,
      connector_url: result.url,
      description: (body.description as string | undefined) ?? "",
      created_at: new Date().toISOString(),
    });
    // The test is the first status, as in the backend.
    remember(source, probe(result.url));
    return mockJson(source, 201);
  }
  const source = store.get(path[1]);
  if (!source) return mockError("没有找到这个数据源或工具服务", 404);
  if (path.length === 2 && request.method === "GET") return mockJson(source);
  if (path.length === 2 && request.method === "PATCH") {
    if ("kind" in body) return mockError("类型登记后不能修改", 422);
    const issue = badFields(body, ["name", "description", "connector_url"]);
    if (issue) return mockError(issue, 422);
    const changes: Partial<Datasource> = {};
    if (typeof body.name === "string") changes.name = body.name.trim();
    if (typeof body.description === "string")
      changes.description = body.description;
    const url =
      typeof body.connector_url === "string" &&
      !sameUrl(body.connector_url, source.connector_url)
        ? body.connector_url
        : null;
    if (changes.name && nameTaken(changes.name, source.datasource_id))
      return mockError(duplicateName(changes.name), 409);
    if (url && source.kind === "file")
      return mockError("上传文件的数据源不能修改地址", 422);
    if (url) {
      const result = mockConnectorTest(source.kind, url);
      if (!result.ok || !result.url) return failedTest(result);
      changes.connector_url = result.url;
    }
    // In place: an upload's record is this same object.
    Object.assign(source, changes);
    if (url) remember(source, probe(source.connector_url));
    return mockJson(source);
  }
  if (path[2] === "health")
    return mockJson(
      mockHealth(
        source,
        new URL(request.url).searchParams.get("fresh") === "true",
      ),
    );
  if (path[2] === "usage") {
    // Loaded here: mock-apps reaches this module through mock-flow-apps.
    const { mockDatasourceUsage } = await import("./mock-apps");
    return mockJson({ apps: mockDatasourceUsage(source.datasource_id) });
  }
  if (path[2] === "tools") {
    // Listing the tools is a check too: the page's status follows it.
    const found = probe(source.connector_url);
    const health = remember(source, found);
    return "reason" in found
      ? mockError(health.message ?? "", 502)
      : mockJson(found.tools);
  }
  return mockError("Not found", 404);
}
