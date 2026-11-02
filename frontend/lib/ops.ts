import type { CatalogKind } from "./catalog";
import { requestJson } from "./runs";

// docs/api/ops.md is the source of truth for these wire types.
export const PERIODS = [7, 30, 90] as const;
export type Period = (typeof PERIODS)[number];
export const MAX_PRICE = 1000;
export const MAX_BUDGET = 100_000;

export type Metrics = {
  runs: number;
  done: number;
  error: number;
  cancelled: number;
  active: number;
  delivery_seconds_median: number | null;
  wait_seconds_median: number | null;
  first_pass_rate: number | null;
  model_cost_usd: number | null;
  model_calls: number;
  unpriced_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  compute_seconds: number;
  compute_cost_usd: number | null;
};
export type BudgetState = "ok" | "warn" | "over";
export type Budget = {
  monthly_usd: number;
  warn_at: number;
  block: boolean;
  month_cost_usd: number;
  state: BudgetState;
  updated_by: string | null;
  updated_at: string | null;
};
export type AppMetrics = Metrics & {
  app: string;
  title: string;
  budget: Budget | null;
};
export type Day = {
  date: string;
  runs: number;
  done: number;
  error: number;
  model_cost_usd: number | null;
};
export type Reuse = {
  assets: number;
  reused: number;
  apps_from_templates: number;
  top: {
    kind: CatalogKind;
    id: string;
    title: string;
    link: string;
    used_by: number;
  }[];
};
export type Summary = {
  days: Period;
  since: string;
  totals: Metrics;
  apps: AppMetrics[];
  daily: Day[];
  reuse: Reuse | null;
  compute_hourly_usd: number | null;
};
export type BudgetInput = {
  monthly_usd: number;
  warn_at: number;
  block: boolean;
  actor?: string;
};

export const budgetStateLabels: Record<BudgetState, string> = {
  ok: "正常",
  warn: "快用完了",
  over: "已用完",
};

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
const appPath = (app: string) => `/apps/${encodeURIComponent(app)}/budget`;

export const getSummary = (days: Period, signal?: AbortSignal) =>
  requestJson(`/ops/summary?days=${days}`, { signal }) as Promise<Summary>;
export const setComputePrice = (value: number | null) =>
  requestJson(
    "/ops/settings",
    json("PUT", { compute_hourly_usd: value }),
  ) as Promise<{ compute_hourly_usd: number | null }>;
export const getBudget = (app: string, signal?: AbortSignal) =>
  requestJson(appPath(app), { signal }) as Promise<Budget | null>;
export const setBudget = (app: string, input: BudgetInput) =>
  requestJson(appPath(app), json("PUT", input)) as Promise<Budget>;
export const deleteBudget = (app: string) =>
  requestJson(appPath(app), json("DELETE")) as Promise<unknown>;

// Display helpers.
export const usd = (value: number | null, digits = 2) =>
  value === null ? "—" : `$${value.toFixed(digits)}`;
export const percent = (value: number | null) =>
  value === null ? "—" : `${Math.round(value * 100)}%`;
export function duration(seconds: number | null) {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)} 分钟`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(hours < 10 ? 1 : 0)} 小时`;
  return `${(hours / 24).toFixed(1)} 天`;
}
export const hours = (seconds: number) =>
  seconds === 0
    ? "0"
    : `${(seconds / 3600).toFixed(seconds < 360 ? 2 : 1)} 小时`;
export function successRate(m: Metrics) {
  const finished = m.done + m.error;
  return finished ? m.done / finished : null;
}
