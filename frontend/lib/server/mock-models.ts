import "server-only";
import type { ModelChoice } from "../apps";
import {
  providerForms,
  type Defaults,
  type Model,
  type ModelUsage,
  type ProviderId,
} from "../models";
import { isRecord } from "../runs";
import { mockError, mockJson, noContent, readBody } from "./mock-assets";
import { mockModels } from "./mock-flow-apps";

// Mock of docs/api/models.md. No gateway is called. A pasted key that starts with
// "sk-bad" fails the test with an auth problem, and a model named "missing-model" with
// not_found, so the page's failure paths can be shown. Gaps against the backend: the
// address check is only the format check, the gateway is always "ok", and usage is
// fixed (only `mock` has calls, and the 零售数据 ETL app uses it by default).
const EMPTY = {
  calls: 0,
  errors: 0,
  prompt_tokens: 0,
  completion_tokens: 0,
  cost_usd: null,
  last_call_at: null,
  last_error: null,
};
const names: Record<string, [string, ProviderId]> = {
  mock: ["Mock（测试）", "mock"],
  gpt: ["GPT（OpenAI）", "openai"],
  "azure-gpt": ["GPT（Azure）", "azure"],
  "claude-bedrock": ["Claude（Bedrock）", "bedrock"],
  qwen: ["通义千问", "dashscope"],
  deepseek: ["DeepSeek", "deepseek"],
};
const configModel = (alias: string, purpose: Model["purpose"]): Model => ({
  alias,
  display_name: names[alias]?.[0] ?? alias,
  purpose,
  provider: names[alias]?.[1] ?? "other",
  model: alias,
  api_base: null,
  key: { source: "unknown", env: null },
  description: "",
  source: "config",
  is_default: false,
  allowed: true,
  context_window: null,
  price: null,
  usage:
    alias === "mock"
      ? {
          ...EMPTY,
          calls: 42,
          prompt_tokens: 128_000,
          completion_tokens: 9_600,
          last_call_at: "2026-01-05T08:00:00.000Z",
        }
      : { ...EMPTY },
});
type Keyed = Model & { secret?: string };
const globalStore = globalThis as typeof globalThis & {
  aepMockModels?: Map<string, Keyed>;
  aepMockModelDefaults?: Defaults;
};
const store = (globalStore.aepMockModels ??= new Map(
  [
    ...mockModels.map((alias) => configModel(alias, "chat")),
    configModel("mock-embed", "embedding"),
    configModel("company-embed", "embedding"),
  ].map((model) => [model.alias, model]),
));
const defaults = (globalStore.aepMockModelDefaults ??= {
  chat: "mock",
  embedding: "mock-embed",
});

const ALIAS = /^[a-z][a-z0-9_-]{1,39}$/;
const ENV = /^[A-Z][A-Z0-9_]*_API_KEY$/;
const URL_FORMAT =
  "地址格式不对。请填完整的地址，例如 https://api.example.com/v1：以 http:// 或 https:// 开头，不含空格、中文和 # 号，端口在 1～65535 之间。";
const AUTH = "认证失败：key 不对或已失效。";
const NOT_FOUND =
  "模型不存在：供应商那边没有这个型号，或者这个 key 没有权限用它。";

function out(model: Keyed): Model {
  const { secret: _secret, ...rest } = model;
  void _secret;
  return {
    ...rest,
    is_default: defaults[model.purpose as "chat" | "embedding"] === model.alias,
  };
}
function listed(): Model[] {
  const order = ["chat", "embedding", "rerank"];
  return [...store.values()]
    .map(out)
    .sort(
      (a, b) =>
        order.indexOf(a.purpose) - order.indexOf(b.purpose) ||
        Number(b.is_default) - Number(a.is_default) ||
        a.alias.localeCompare(b.alias),
    );
}
export const mockModelChoices = (): ModelChoice[] =>
  listed().map((model) => ({
    alias: model.alias,
    label: model.display_name,
    purpose: model.purpose,
    allowed: model.allowed,
  }));

function problem(model: Keyed) {
  if (model.secret?.startsWith("sk-bad"))
    return { code: "auth", message: AUTH };
  if (model.model === "missing-model")
    return { code: "not_found", message: NOT_FOUND };
  return null;
}
const testResult = (model: Keyed) => {
  const found = problem(model);
  return found
    ? { ok: false, latency_ms: 120, reply: null, problem: found }
    : {
        ok: true,
        latency_ms: 820,
        reply: model.purpose === "chat" ? "OK" : null,
        problem: null,
      };
};

// The checks shared by create and update; returns the 422 detail or "".
function badFields(body: Record<string, unknown>, provider: ProviderId) {
  const form = providerForms.find((item) => item.id === provider);
  if (!form) return "「供应商」的格式、长度或范围不正确";
  for (const [field, label, max] of [
    ["display_name", "显示名", 60],
    ["model", "型号", 200],
    ["description", "说明", 500],
  ] as const) {
    const value = body[field];
    if (
      value !== undefined &&
      (typeof value !== "string" || value.length > max)
    )
      return `「${label}」的格式、长度或范围不正确`;
  }
  if (typeof body.api_base === "string") {
    if (form.base === "none") return `${form.label}不用填地址`;
    try {
      const url = new URL(body.api_base.trim());
      if (!["http:", "https:"].includes(url.protocol)) throw new Error();
    } catch {
      return URL_FORMAT;
    }
  }
  if (body.key !== undefined) {
    if (!form.key) return "AWS Bedrock不用填 key，网关用它所在环境里的凭据";
    const key = body.key;
    if (
      !isRecord(key) ||
      !(
        (key.source === "page" &&
          typeof key.value === "string" &&
          key.value.trim()) ||
        (key.source === "env" && typeof key.env === "string")
      )
    )
      return "「key」的格式、长度或范围不正确";
    if (
      key.source === "env" &&
      (!ENV.test(key.env as string) ||
        /^(LITELLM_|AEP_)/.test(key.env as string))
    )
      return "环境变量名要以 _API_KEY 结尾，只能有大写字母、数字和下划线，不能以 LITELLM_ 或 AEP_ 开头";
  }
  return "";
}

function applyKey(model: Keyed, key: unknown) {
  if (!isRecord(key)) return;
  if (key.source === "page") {
    model.key = { source: "page", env: null };
    model.secret = String(key.value).trim();
  } else {
    model.key = { source: "env", env: String(key.env) };
    model.secret = undefined;
  }
}

async function create(request: Request) {
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (typeof body.alias !== "string" || !ALIAS.test(body.alias))
    return mockError("「别名」的格式、长度或范围不正确", 422);
  if (body.purpose === "rerank")
    return mockError(
      "页面上只能新增对话模型和向量模型，重排模型请在配置文件里加",
      422,
    );
  if (body.purpose !== "chat" && body.purpose !== "embedding")
    return mockError("「用途」的格式、长度或范围不正确", 422);
  if (typeof body.display_name !== "string" || !body.display_name.trim())
    return mockError("「显示名」的格式、长度或范围不正确", 422);
  const provider = body.provider as ProviderId;
  const bad = badFields(body, provider);
  if (bad) return mockError(bad, 422);
  const form = providerForms.find((item) => item.id === provider)!;
  if (typeof body.model !== "string" || !body.model.trim())
    return mockError(`${form.label}要填型号`, 422);
  if (form.base === "required" && typeof body.api_base !== "string")
    return mockError(`${form.label}要填地址`, 422);
  if (form.key && body.key === undefined)
    return mockError(`${form.label}要填key`, 422);
  if (store.has(body.alias))
    return mockError(`已经有叫「${body.alias}」的模型了`, 409);
  const model: Keyed = {
    alias: body.alias,
    display_name: body.display_name.trim(),
    purpose: body.purpose,
    provider,
    model: body.model.trim(),
    api_base: typeof body.api_base === "string" ? body.api_base.trim() : null,
    key: { source: "none", env: null },
    description: typeof body.description === "string" ? body.description : "",
    source: "page",
    is_default: false,
    allowed: true,
    context_window: null,
    price: null,
    usage: { ...EMPTY },
  };
  applyKey(model, body.key);
  const found = problem(model);
  if (found) return mockError(`测试没有通过：${found.message}`, 422);
  store.set(model.alias, model);
  return mockJson(out(model), 201);
}

async function update(request: Request, model: Keyed) {
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (model.source === "config")
    return mockError(
      `「${model.alias}」写在网关的配置文件里，只能在配置文件里修改`,
      409,
    );
  for (const [field, label] of [
    ["alias", "别名"],
    ["purpose", "用途"],
    ["provider", "供应商"],
  ])
    if (field in body) return mockError(`${label}登记后不能修改`, 422);
  const bad = badFields(body, model.provider);
  if (bad) return mockError(bad, 422);
  const next: Keyed = { ...model, key: { ...model.key } };
  if (typeof body.display_name === "string")
    next.display_name = body.display_name.trim();
  if (typeof body.description === "string") next.description = body.description;
  if (typeof body.model === "string") next.model = body.model.trim();
  if (typeof body.api_base === "string") next.api_base = body.api_base.trim();
  applyKey(next, body.key);
  const tested = ["model", "api_base", "api_version", "aws_region", "key"].some(
    (field) => field in body,
  );
  const found = tested ? problem(next) : null;
  if (found) return mockError(`测试没有通过：${found.message}`, 422);
  store.set(model.alias, next);
  return mockJson(out(next));
}

function usage(alias: string): ModelUsage {
  return alias === defaults.chat
    ? {
        apps: [
          {
            app: "etl",
            title: "零售数据 ETL",
            version: 1,
            nodes: ["生成方案", "写代码"],
            via: "default",
          },
        ],
      }
    : { apps: [] };
}

export async function mockModelsApi(request: Request, path: string[]) {
  if (path.length === 1) {
    if (request.method === "POST") return create(request);
    return mockJson({ gateway: "ok", models: listed(), defaults });
  }
  if (path[1] === "defaults") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    for (const purpose of ["chat", "embedding"] as const) {
      const alias = body[purpose];
      if (alias === undefined) continue;
      const model = typeof alias === "string" ? store.get(alias) : undefined;
      if (!model) return mockError("没有找到这个模型", 422);
      if (model.purpose !== purpose)
        return mockError(
          `「${model.alias}」不是${purpose === "chat" ? "对话" : "向量"}模型`,
          422,
        );
    }
    Object.assign(defaults, body);
    return mockJson(defaults);
  }
  const model = store.get(path[1]);
  if (!model) return mockError("没有找到这个模型", 404);
  if (path[2] === "test")
    return mockJson(
      model.purpose === "rerank"
        ? {
            ok: false,
            latency_ms: null,
            reply: null,
            problem: {
              code: "unsupported",
              message: "重排模型暂不支持在页面上测试。",
            },
          }
        : testResult(model),
    );
  if (path[2] === "usage") return mockJson(usage(model.alias));
  if (request.method === "PATCH") return update(request, model);
  if (request.method === "DELETE") {
    if (model.source === "config")
      return mockError(
        `「${model.alias}」写在网关的配置文件里，只能在配置文件里修改`,
        409,
      );
    const purpose = model.purpose as "chat" | "embedding";
    if (defaults[purpose] === model.alias)
      return mockError(
        `「${model.alias}」是平台默认的${purpose === "chat" ? "对话" : "向量"}模型，请先换一个默认模型`,
        409,
      );
    store.delete(model.alias);
    return noContent();
  }
  return mockJson(out(model));
}
