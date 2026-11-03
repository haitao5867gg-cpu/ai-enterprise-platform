import { requestJson } from "./runs";

// docs/api/models.md is the source of truth for these wire types.
export type Purpose = "chat" | "embedding" | "rerank";
export type ProviderId =
  | "anthropic"
  | "openai"
  | "azure"
  | "bedrock"
  | "dashscope"
  | "deepseek"
  | "openai_compatible"
  | "mock"
  | "other";
export type KeyInfo = {
  source: "page" | "env" | "none" | "unknown";
  env: string | null;
};
export type ModelUsageSummary = {
  calls: number;
  errors: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number | null;
  last_call_at: string | null;
  last_error: string | null;
};
export type Model = {
  alias: string;
  display_name: string;
  purpose: Purpose;
  provider: ProviderId;
  model: string;
  api_base: string | null;
  key: KeyInfo;
  description: string;
  source: "config" | "page";
  is_default: boolean;
  allowed: boolean;
  context_window: number | null;
  price: { input: number; output: number } | null;
  usage: ModelUsageSummary;
};
export type GatewayState = "ok" | "no_db" | "down";
export type Defaults = { chat: string; embedding: string };
export type ModelList = {
  gateway: GatewayState;
  models: Model[];
  defaults: Defaults;
};
export type Problem = { code: string; message: string };
export type TestResult = {
  ok: boolean;
  latency_ms: number | null;
  reply: string | null;
  problem: Problem | null;
};
export type ModelUsage = {
  apps: {
    app: string;
    title: string;
    version: number;
    nodes: string[];
    via: "literal" | "default";
  }[];
};
export type KeyInput =
  | { source: "page"; value: string }
  | { source: "env"; env: string };
export type ModelInput = {
  alias: string;
  display_name: string;
  purpose: "chat" | "embedding";
  provider: ProviderId;
  model: string;
  api_base?: string;
  api_version?: string;
  aws_region?: string;
  key?: KeyInput;
  description?: string;
};
export type ModelChanges = Partial<
  Omit<ModelInput, "alias" | "purpose" | "provider">
>;

// What each provider asks for when a model is added (the table in docs/api/models.md).
export type ProviderForm = {
  id: ProviderId;
  label: string;
  base: "required" | "optional" | "none";
  version: boolean;
  region: boolean;
  key: boolean;
  modelLabel: string;
  modelHint: string;
  baseHint?: string;
  envHint: string;
  note?: string;
  purposes: ("chat" | "embedding")[];
};
export const providerForms: ProviderForm[] = [
  {
    id: "anthropic",
    label: "Anthropic（Claude）",
    base: "none",
    version: false,
    region: false,
    key: true,
    modelLabel: "型号",
    modelHint: "claude-sonnet-5-5",
    envHint: "ANTHROPIC_API_KEY",
    note: "key 在 Anthropic Console（console.anthropic.com）的 API Keys 里创建，调用按 Console 账号的 API 额度计费。Claude.ai 的 Pro / Max 订阅额度不能在这里用。",
    purposes: ["chat"],
  },
  {
    id: "openai",
    label: "OpenAI",
    base: "optional",
    version: false,
    region: false,
    key: true,
    modelLabel: "型号",
    modelHint: "gpt-5.1",
    baseHint: "不填时用 OpenAI 官方地址。",
    envHint: "OPENAI_API_KEY",
    purposes: ["chat", "embedding"],
  },
  {
    id: "azure",
    label: "Azure OpenAI",
    base: "required",
    version: true,
    region: false,
    key: true,
    modelLabel: "部署名",
    modelHint: "gpt-4o-prod",
    baseHint: "Azure OpenAI 资源的地址，例如 https://xxx.openai.azure.com。",
    envHint: "AZURE_API_KEY",
    purposes: ["chat", "embedding"],
  },
  {
    id: "bedrock",
    label: "AWS Bedrock",
    base: "none",
    version: false,
    region: true,
    key: false,
    modelLabel: "型号",
    modelHint: "anthropic.claude-sonnet-4-5-v1:0",
    envHint: "",
    note: "不用填 key：网关用它所在服务器上的 AWS 凭据（AWS_ACCESS_KEY_ID 等）。",
    purposes: ["chat", "embedding"],
  },
  {
    id: "dashscope",
    label: "通义千问（DashScope）",
    base: "none",
    version: false,
    region: false,
    key: true,
    modelLabel: "型号",
    modelHint: "qwen-plus",
    envHint: "DASHSCOPE_API_KEY",
    purposes: ["chat", "embedding"],
  },
  {
    id: "deepseek",
    label: "DeepSeek",
    base: "none",
    version: false,
    region: false,
    key: true,
    modelLabel: "型号",
    modelHint: "deepseek-chat",
    envHint: "DEEPSEEK_API_KEY",
    purposes: ["chat"],
  },
  {
    id: "openai_compatible",
    label: "OpenAI 兼容网关",
    base: "required",
    version: false,
    region: false,
    key: true,
    modelLabel: "型号",
    modelHint: "deepseek-v4.1-flash-new",
    baseHint: "网关的地址，一般以 /v1 结尾。",
    envHint: "COMPANY_API_KEY",
    purposes: ["chat", "embedding"],
  },
];
const otherLabels: Record<string, string> = {
  mock: "测试用（mock）",
  other: "其他",
};
export const providerLabel = (id: string) =>
  providerForms.find((form) => form.id === id)?.label ?? otherLabels[id] ?? id;
export const purposeLabels: Record<Purpose, string> = {
  chat: "对话模型",
  embedding: "向量模型",
  rerank: "重排模型",
};
// Where a model's key comes from, in the reader's language (pass `t` from useT()).
export const keyLabel = (
  t: (text: string, vars?: Record<string, string>) => string,
  key: KeyInfo,
) =>
  key.source === "page"
    ? t("页面上填的")
    : key.source === "env"
      ? t("环境变量 {name}", { name: key.env ?? "" })
      : key.source === "none"
        ? t("不用 key")
        : t("配置文件里");

const base = (alias: string) => `/models/${encodeURIComponent(alias)}`;
const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export const listModels = (signal?: AbortSignal) =>
  requestJson("/models", { signal }) as Promise<ModelList>;
export const createModel = (input: ModelInput) =>
  requestJson("/models", json("POST", input)) as Promise<Model>;
export const updateModel = (alias: string, changes: ModelChanges) =>
  requestJson(base(alias), json("PATCH", changes)) as Promise<Model>;
export const deleteModel = (alias: string) =>
  requestJson(base(alias), json("DELETE"));
export const testModel = (alias: string) =>
  requestJson(`${base(alias)}/test`, json("POST")) as Promise<TestResult>;
export const getModelUsage = (alias: string, signal?: AbortSignal) =>
  requestJson(`${base(alias)}/usage`, { signal }) as Promise<ModelUsage>;
export const setDefaults = (defaults: Partial<Defaults>) =>
  requestJson("/models/defaults", json("PUT", defaults)) as Promise<Defaults>;

// "Claude Sonnet 5.5（claude-sonnet）" when the display name differs from the alias.
export const modelLabel = (alias: string, label?: string | null) =>
  label && label !== alias ? `${label}（${alias}）` : alias;
