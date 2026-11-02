import "server-only";
import type {
  AppMetrics,
  Budget,
  Day,
  Metrics,
  Period,
  Reuse,
  Summary,
} from "../ops";
import { MAX_BUDGET, MAX_PRICE, PERIODS } from "../ops";
import { mockCatalogApps } from "./mock-apps";
import {
  invalid,
  mockError,
  mockJson,
  noContent,
  readBody,
  type ValidationIssue,
} from "./mock-assets";
import { mockWho } from "./mock-auth";
import { mockCatalogItems } from "./mock-catalog";

// The operations dashboard (docs/api/ops.md) over fixed pretend usage: a few runs a day
// for the first apps, model costs per app and Databricks time. Budgets and the hourly
// price are kept in memory.
type Stored = Omit<Budget, "month_cost_usd" | "state">;
const holder = globalThis as typeof globalThis & {
  aepMockOps?: { price: number | null; budgets: Map<string, Stored> };
};
const state = (holder.aepMockOps ??= { price: null, budgets: new Map() });

// Per app, per day: [runs, done, error, model cost, compute seconds]. Index 0 is today.
type Usage = {
  runs: number;
  done: number;
  error: number;
  cost: number;
  compute: number;
};
const PATTERNS: ((day: number) => Usage)[] = [
  (day) => ({
    runs: day % 3 === 0 ? 3 : 1,
    done: day % 3 === 0 ? 2 : 1,
    error: day % 3 === 0 ? 1 : 0,
    cost: day % 3 === 0 ? 0.42 : 0.18,
    compute: day % 3 === 0 ? 1800 : 600,
  }),
  (day) => ({
    runs: day % 2 === 0 ? 2 : 0,
    done: day % 2 === 0 ? 2 : 0,
    error: 0,
    cost: day % 2 === 0 ? 0.05 : 0,
    compute: 0,
  }),
];

function apps() {
  return mockCatalogApps().slice(0, PATTERNS.length);
}

function usageOf(app: string, day: number): Usage | null {
  const index = apps().findIndex((item) => item.app === app);
  return index < 0 ? null : PATTERNS[index](day);
}

// This month so far (the mock month starts 10 days back).
function monthCost(app: string) {
  let cost = 0;
  for (let day = 0; day < 10; day++) cost += usageOf(app, day)?.cost ?? 0;
  return Math.round(cost * 1e6) / 1e6;
}

function budgetOut(app: string): Budget | null {
  const stored = state.budgets.get(app);
  if (!stored) return null;
  const spent = monthCost(app);
  return {
    ...stored,
    month_cost_usd: spent,
    state:
      spent >= stored.monthly_usd
        ? "over"
        : spent >= stored.monthly_usd * stored.warn_at
          ? "warn"
          : "ok",
  };
}

function metrics(usages: Usage[]): Metrics {
  const sum = (key: keyof Usage) => usages.reduce((n, u) => n + u[key], 0);
  const runs = sum("runs");
  const done = sum("done");
  const error = sum("error");
  const cost = Math.round(sum("cost") * 1e6) / 1e6;
  const compute = sum("compute");
  const firsts = done + error;
  return {
    runs,
    done,
    error,
    cancelled: 0,
    active: 0,
    delivery_seconds_median: done ? 840 : null,
    wait_seconds_median: done ? 300 : null,
    first_pass_rate: firsts ? Math.round((done / firsts) * 1e4) / 1e4 : null,
    model_cost_usd: runs ? cost : null,
    model_calls: runs * 4,
    unpriced_calls: 0,
    prompt_tokens: runs * 12_000,
    completion_tokens: runs * 900,
    compute_seconds: compute,
    compute_cost_usd:
      state.price === null
        ? null
        : Math.round((compute / 3600) * state.price * 1e4) / 1e4,
  };
}

function reuse(): Reuse {
  const items = mockCatalogItems();
  const users = (item: (typeof items)[number]) =>
    new Set(item.used_by.map((use) => use.app)).size;
  const assets = items.filter((item) => item.kind !== "template");
  return {
    assets: assets.length,
    reused: assets.filter((item) => users(item) >= 2).length,
    apps_from_templates: items
      .filter((item) => item.kind === "template")
      .reduce((n, item) => n + users(item), 0),
    top: assets
      .filter((item) => users(item) > 0)
      .sort((a, b) => users(b) - users(a) || a.title.localeCompare(b.title))
      .slice(0, 5)
      .map((item) => ({
        kind: item.kind,
        id: item.id,
        title: item.title,
        link: item.link,
        used_by: users(item),
      })),
  };
}

function summary(days: Period): Summary {
  const now = new Date();
  const listed = apps();
  const rows: AppMetrics[] = listed.map((app) => ({
    app: app.app,
    title: app.title,
    ...metrics(
      Array.from({ length: days }, (_, day) => usageOf(app.app, day)!),
    ),
    budget: budgetOut(app.app),
  }));
  for (const [app] of state.budgets)
    if (!rows.some((row) => row.app === app)) {
      const title =
        mockCatalogApps().find((item) => item.app === app)?.title ?? app;
      rows.push({ app, title, ...metrics([]), budget: budgetOut(app) });
    }
  rows.sort((a, b) => b.runs - a.runs || a.title.localeCompare(b.title));
  const daily: Day[] = Array.from({ length: days }, (_, n) => {
    const day = days - 1 - n;
    const date = new Date(now.getTime() - day * 86_400_000);
    const usages = listed.map((app) => usageOf(app.app, day)!);
    const m = metrics(usages);
    return {
      date: date.toISOString().slice(0, 10),
      runs: m.runs,
      done: m.done,
      error: m.error,
      model_cost_usd: m.model_cost_usd,
    };
  });
  const all = listed.flatMap((app) =>
    Array.from({ length: days }, (_, day) => usageOf(app.app, day)!),
  );
  return {
    days,
    since: new Date(now.getTime() - days * 86_400_000).toISOString(),
    totals: metrics(all),
    apps: rows,
    daily,
    reuse: reuse(),
    compute_hourly_usd: state.price,
  };
}

const isNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

export async function mockOps(request: Request, path: string[]) {
  if (path[0] === "ops" && path[1] === "summary") {
    const days = Number(new URL(request.url).searchParams.get("days") ?? 30);
    if (!PERIODS.includes(days as Period))
      return mockError("days 只能是 7、30 或 90", 422);
    return mockJson(summary(days as Period));
  }
  if (path[0] === "ops" && path[1] === "settings") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const value = body.compute_hourly_usd ?? null;
    if (value !== null && (!isNumber(value) || value < 0 || value > MAX_PRICE))
      return invalid([
        {
          loc: ["body", "compute_hourly_usd"],
          msg: `单价要在 0 到 ${MAX_PRICE} 之间`,
        },
      ]);
    state.price = value;
    return mockJson({ compute_hourly_usd: value });
  }
  // /apps/{app}/budget
  const app = decodeURIComponent(path[1]);
  if (!mockCatalogApps().some((item) => item.app === app))
    return mockError(`没有应用「${app}」`, 404);
  if (request.method === "GET") return mockJson(budgetOut(app));
  if (request.method === "DELETE") {
    state.budgets.delete(app);
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  const issues: ValidationIssue[] = [];
  const { monthly_usd, warn_at = 0.8, block = true } = body;
  if (!isNumber(monthly_usd) || monthly_usd < 0.01 || monthly_usd > MAX_BUDGET)
    issues.push({
      loc: ["body", "monthly_usd"],
      msg: `预算要在 0.01 到 ${MAX_BUDGET} 之间`,
    });
  if (!isNumber(warn_at) || warn_at < 0.5 || warn_at > 1)
    issues.push({
      loc: ["body", "warn_at"],
      msg: "提醒线要在 50% 到 100% 之间",
    });
  if (typeof block !== "boolean")
    issues.push({ loc: ["body", "block"], msg: "block 要是 true 或 false" });
  if (issues.length) return invalid(issues);
  const me = mockWho(request).user?.display_name;
  state.budgets.set(app, {
    monthly_usd: monthly_usd as number,
    warn_at: warn_at as number,
    block: block as boolean,
    updated_by: me ?? (typeof body.actor === "string" ? body.actor : null),
    updated_at: new Date().toISOString(),
  });
  return mockJson(budgetOut(app));
}
