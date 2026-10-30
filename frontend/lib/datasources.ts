import type { DataSource } from "./etl";
import { isRecord, requestJson } from "./runs";

// docs/api/datasources.md is the source of truth for these wire types.
export type DatasourceKind =
  | "postgres"
  | "mysql"
  | "sqlserver"
  | "file"
  | "mcp";
export type Datasource = DataSource & {
  connector_url: string;
  created_at: string;
};
// One property of a tool's `input_schema` (JSON Schema, as the connector reports it).
// Connectors are third-party services, so no field is trusted to have the right type.
export type ToolParameter = Record<string, unknown>;
export type ConnectorTool = {
  name: string;
  description: string;
  input_schema: {
    type?: string;
    properties?: Record<string, ToolParameter>;
    required?: string[];
  };
  read_only: boolean;
};
export type ConnectionProblem = "timeout" | "refused" | "not_mcp" | "other";
export type Problem = { code: string; message: string };
export type TestResult = {
  ok: boolean;
  latency_ms: number | null;
  tools: Pick<ConnectorTool, "name" | "description" | "read_only">[];
  missing_tools: string[];
  problems: Problem[];
};
export type Health = {
  ok: boolean;
  latency_ms: number | null;
  tool_count: number | null;
  error: ConnectionProblem | null;
  message: string | null;
  checked_at: string;
};
export type UsageVia = "literal" | "default_input";
export type DatasourceUsage = {
  apps: { app: string; title: string; version: number; via: UsageVia }[];
};
export type DatasourceInput = {
  name: string;
  kind: DatasourceKind;
  connector_url: string;
  description: string;
};

const base = (id: string) => `/datasources/${encodeURIComponent(id)}`;
const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export const listDatasources = (signal?: AbortSignal) =>
  requestJson("/datasources", { signal }) as Promise<Datasource[]>;
export const getDatasource = (id: string, signal?: AbortSignal) =>
  requestJson(base(id), { signal }) as Promise<Datasource>;
export const testConnector = (
  input: Pick<DatasourceInput, "kind" | "connector_url">,
  signal?: AbortSignal,
) =>
  requestJson("/datasources/test", {
    ...json("POST", input),
    signal,
  }) as Promise<TestResult>;
export const createDatasource = (input: DatasourceInput) =>
  requestJson("/datasources", json("POST", input)) as Promise<Datasource>;
export const updateDatasource = (
  id: string,
  input: Partial<Omit<DatasourceInput, "kind">>,
) => requestJson(base(id), json("PATCH", input)) as Promise<Datasource>;
export const getHealth = (
  id: string,
  { fresh = false, signal }: { fresh?: boolean; signal?: AbortSignal } = {},
) =>
  requestJson(`${base(id)}/health${fresh ? "?fresh=true" : ""}`, {
    signal,
  }) as Promise<Health>;
export const getUsage = (id: string, signal?: AbortSignal) =>
  requestJson(`${base(id)}/usage`, { signal }) as Promise<DatasourceUsage>;
export const listTools = (id: string, signal?: AbortSignal) =>
  requestJson(`${base(id)}/tools`, { signal }) as Promise<ConnectorTool[]>;

// Tool lists the flow editor has asked for, by data source id, kept for the session.
const toolLists = new Map<string, Promise<ConnectorTool[]>>();
export function cachedTools(id: string): Promise<ConnectorTool[]> {
  let request = toolLists.get(id);
  if (!request) {
    request = listTools(id).then((value) =>
      Array.isArray(value) ? value : [],
    );
    toolLists.set(id, request);
    request.catch(() => toolLists.delete(id));
  }
  return request;
}
// After a source's address changes its tools are listed again.
export const forgetTools = (id: string) => void toolLists.delete(id);

export const kindLabels: Record<string, string> = {
  postgres: "PostgreSQL",
  mysql: "MySQL",
  sqlserver: "SQL Server",
  file: "上传文件",
  mcp: "工具服务",
};
export const kindLabel = (kind: string) => kindLabels[kind] ?? kind;
// Tool services (`mcp`) only offer tools: tool and agent nodes may use them, ETL may not.
export const isToolService = (source: { kind: string }) =>
  source.kind === "mcp";
export const healthLabels: Record<ConnectionProblem, string> = {
  timeout: "超时",
  refused: "连接被拒绝",
  not_mcp: "不是 MCP 服务",
  other: "网络不通",
};
export const viaLabels: Record<UsageVia, string> = {
  literal: "节点设置里指定",
  default_input: "开始节点的默认值",
};
export const CREDENTIALS_NOTE = "数据库账号密码配在连接器上，平台只保存地址。";
export const TOOL_SERVICE_NOTE = "其他账号和密钥也一样配在连接器上。";
export const READ_ONLY_TAG = "只读（连接器自报）";
export const UNDECLARED_TAG = "未声明只读";
export const undeclaredNote = (count: number) =>
  `有 ${count} 个工具没有声明只读。第一阶段平台只支持只读工具，请确认它们不会修改数据。`;

// What the 6 ETL tools of connectors/postgres (and the other database connectors) do.
// Other tools keep the connector's own description.
const etlToolDescriptions: Record<string, string> = {
  list_schemas: "列出可以读取的 schema，不含系统 schema",
  list_tables: "列出某个 schema 下可以读取的表，不含视图",
  describe_table: "查看表结构：列、类型、能否为空、主键和外键",
  table_stats: "估算行数，并抽样统计各列的空值比例、不同值个数和常见取值",
  sample_rows: "读取最多 100 行样本，不保证顺序",
  read_rows: "按主键顺序分页读取；没有主键时按全部列排序",
};
// connectors/files: an upload has one schema, no views, and its rows are counted exactly.
const fileToolDescriptions: Record<string, string> = {
  list_schemas: "列出上传数据集的 schema",
  list_tables: "列出数据集里的表",
  table_stats: "统计行数，并抽样统计各列的空值比例、不同值个数和常见取值",
};
export const toolDescription = (
  tool: Pick<ConnectorTool, "name" | "description">,
  kind: string,
) =>
  isToolService({ kind })
    ? tool.description
    : (kind === "file" && fileToolDescriptions[tool.name]) ||
      etlToolDescriptions[tool.name] ||
      tool.description;

const typeWords: Record<string, string> = {
  string: "文字",
  integer: "整数",
  number: "数字",
  boolean: "是/否",
  array: "列表",
  object: "对象",
};
// A schema value as a list: `type` may be one name or several.
const listed = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [value];
export function parameterType(parameter: ToolParameter): string {
  const types = [
    ...listed(parameter.type),
    ...(Array.isArray(parameter.anyOf) ? parameter.anyOf : []).flatMap(
      (item) => (isRecord(item) ? listed(item.type) : []),
    ),
  ].filter(
    (type): type is string =>
      typeof type === "string" && Boolean(type) && type !== "null",
  );
  if (!types.length) return "—";
  return Object.hasOwn(typeWords, types[0]) ? typeWords[types[0]] : types[0];
}
const shown = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value);
const numeric = (value: unknown) =>
  typeof value === "number" ? value : undefined;
// The schema's description, else its default and range, e.g. 「默认 10，1～100」.
export function parameterNote(parameter: ToolParameter): string {
  const description =
    typeof parameter.description === "string"
      ? parameter.description.trim()
      : "";
  if (description) return description;
  const [min, max] = [numeric(parameter.minimum), numeric(parameter.maximum)];
  const notes = [
    parameter.default !== undefined && parameter.default !== null
      ? `默认 ${shown(parameter.default)}`
      : "",
    min !== undefined && max !== undefined
      ? `${min}～${max}`
      : min !== undefined
        ? `最小 ${min}`
        : max !== undefined
          ? `最大 ${max}`
          : "",
    Array.isArray(parameter.enum) && parameter.enum.length
      ? `可选：${parameter.enum.map(shown).join("、")}`
      : "",
  ].filter(Boolean);
  return notes.length ? notes.join("，") : "—";
}
