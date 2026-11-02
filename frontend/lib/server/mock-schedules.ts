import "server-only";
import { randomUUID } from "node:crypto";
import { isRecord } from "../runs";
import type { Schedule } from "../schedules";
import { mockError, mockJson, noContent, readBody } from "./mock-assets";
import { mockRunStatus, mockScheduledRun } from "./mock-runs";

// Schedules (docs/api/schedules.md), kept in memory. Nothing runs them on time in the
// mock; "立即跑一次" starts a mock run as POST /runs does.
type Stored = Omit<Schedule, "next_run_at" | "next_runs" | "last_run"> & {
  last_run: { run_id: string; at: string } | null;
};
const holder = globalThis as typeof globalThis & {
  aepMockSchedules?: Stored[];
};
const schedules = (holder.aepMockSchedules ??= []);

const FIELDS: [string, number, number][] = [
  ["分", 0, 59],
  ["时", 0, 23],
  ["日", 1, 31],
  ["月", 1, 12],
  ["周", 0, 7],
];

function field(text: string, n: number): Set<number> {
  const [name, low, high] = FIELDS[n];
  const where = `第 ${n + 1} 段（${name}）`;
  const found = new Set<number>();
  for (const part of text.split(",")) {
    const [range, step = "1"] = part.split("/");
    if (!/^(\*|\d+(-\d+)?)$/.test(range) || !/^\d+$/.test(step))
      throw new Error(
        `${where}的「${part}」看不懂，只能写数字、*、-、/ 和逗号`,
      );
    if (+step < 1) throw new Error(`${where}的步长「${step}」要是正整数`);
    const [from, to] =
      range === "*"
        ? [low, high]
        : range.includes("-")
          ? range.split("-").map(Number)
          : [+range, part.includes("/") ? high : +range];
    for (const value of [from, to])
      if (value < low || value > high)
        throw new Error(`${where}的 ${value} 超出 ${low}–${high}`);
    if (from > to) throw new Error(`${where}的范围 ${range} 从大到小`);
    for (let v = from; v <= to; v += +step) found.add(n === 4 ? v % 7 : v);
  }
  return found;
}

// The next three times, in the time zone's offset now (good enough for the mock).
function nextRuns(cron: string, timezone: string): string[] {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5)
    throw new Error("要写 5 段（分 时 日 月 周），用空格分开");
  const [minutes, hours, days, months, weekdays] = parts.map(field);
  const now = new Date();
  const local = new Date(now.toLocaleString("en-US", { timeZone: timezone }));
  const offset =
    local.getTime() -
    new Date(now.toLocaleString("en-US", { timeZone: "UTC" })).getTime();
  const anyDay = parts[2] === "*";
  const anyWeekday = parts[4] === "*";
  const found: string[] = [];
  let t = Math.floor((now.getTime() + offset) / 60_000 + 1) * 60_000;
  for (let i = 0; i < 366 * 24 * 60 * 2 && found.length < 3; i++, t += 60_000) {
    const d = new Date(t);
    if (!minutes.has(d.getUTCMinutes()) || !hours.has(d.getUTCHours()))
      continue;
    if (!months.has(d.getUTCMonth() + 1)) continue;
    const onDay = days.has(d.getUTCDate());
    const onWeekday = weekdays.has(d.getUTCDay());
    const day =
      anyDay && anyWeekday
        ? true
        : anyDay
          ? onWeekday
          : anyWeekday
            ? onDay
            : onDay || onWeekday;
    if (day) found.push(new Date(t - offset).toISOString());
  }
  return found;
}

function zoneProblem(timezone: string): string | null {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: timezone });
    return null;
  } catch {
    return `时区 ${timezone} 不认识`;
  }
}

function view(s: Stored): Schedule {
  const upcoming = s.enabled ? nextRuns(s.cron, s.timezone) : [];
  return {
    ...s,
    next_run_at: upcoming[0] ?? null,
    next_runs: upcoming,
    last_run: s.last_run
      ? { ...s.last_run, status: mockRunStatus(s.last_run.run_id) }
      : null,
  };
}

// What is wrong with the schedule as it would be saved, as the backend says it.
async function problem(s: Stored): Promise<Response | null> {
  try {
    nextRuns(s.cron, "UTC");
  } catch (err) {
    return mockError(`定时写法不对：${(err as Error).message}`, 422);
  }
  const zone = zoneProblem(s.timezone);
  if (zone) return mockError(zone, 422);
  const checked = mockScheduledRun(runBody(s), null);
  if (checked.ok) return null;
  const answer = await checked.json();
  // POST /runs's input problems name `input`, as they do here.
  return mockJson(answer, checked.status);
}

function runBody(s: Stored): Record<string, unknown> {
  return {
    app: s.app,
    input: s.input,
    ...(s.created_by ? { requester: s.created_by } : {}),
    ...(s.project ? { project: s.project } : {}),
  };
}

function text(value: unknown, max: number): string | null {
  return typeof value === "string" && value.trim() && value.length <= max
    ? value.trim()
    : null;
}

export async function mockAppSchedules(request: Request, app: string) {
  if (request.method === "GET")
    return mockJson(schedules.filter((s) => s.app === app).map(view));
  const body = await readBody(request);
  if (body instanceof Response) return body;
  const title = text(body.title, 100);
  const cron = text(body.cron, 200);
  if (!title || !cron) return mockError("「名称」和「什么时候跑」要填", 422);
  const now = new Date().toISOString();
  const made: Stored = {
    schedule_id: `sch_${randomUUID().slice(0, 8)}`,
    app,
    title,
    cron: cron.split(/\s+/).join(" "),
    timezone: text(body.timezone, 64) ?? "Asia/Shanghai",
    input: isRecord(body.input) ? body.input : {},
    project: isRecord(body.project)
      ? (body.project as Schedule["project"])
      : null,
    enabled: body.enabled !== false,
    created_by: text(body.requester, 100),
    created_at: now,
    updated_at: now,
    last_run: null,
    last_error: null,
  };
  const wrong = await problem(made);
  if (wrong) return wrong;
  if (schedules.filter((s) => s.app === app).length >= 20)
    return mockError("每个应用最多 20 个定时", 409);
  schedules.push(made);
  return mockJson(view(made), 201);
}

export async function mockSchedules(request: Request, path: string[]) {
  if (path[1] === "preview") {
    const url = new URL(request.url);
    const cron = (url.searchParams.get("cron") ?? "")
      .trim()
      .split(/\s+/)
      .join(" ");
    const timezone = url.searchParams.get("timezone") || "Asia/Shanghai";
    const zone = zoneProblem(timezone);
    if (zone) return mockError(zone, 422);
    try {
      return mockJson({ cron, timezone, next_runs: nextRuns(cron, timezone) });
    } catch (err) {
      return mockError(`定时写法不对：${(err as Error).message}`, 422);
    }
  }
  const index = schedules.findIndex((s) => s.schedule_id === path[1]);
  if (index < 0) return mockError(`没有定时 ${path[1]}`, 404);
  const found = schedules[index];
  if (path[2] === "run") {
    const started = mockScheduledRun(runBody(found), {
      kind: "schedule",
      schedule_id: found.schedule_id,
      title: found.title,
    });
    if (!started.ok) return started;
    const run = (await started.json()) as { run_id: string; status: string };
    found.last_run = { run_id: run.run_id, at: new Date().toISOString() };
    found.last_error = null;
    return mockJson({ run_id: run.run_id, status: run.status }, 201);
  }
  if (request.method === "GET") return mockJson(view(found));
  if (request.method === "DELETE") {
    schedules.splice(index, 1);
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  const changed: Stored = { ...found, updated_at: new Date().toISOString() };
  if (body.title !== undefined)
    changed.title = text(body.title, 100) ?? found.title;
  if (typeof body.cron === "string")
    changed.cron = body.cron.trim().split(/\s+/).join(" ");
  if (typeof body.timezone === "string") changed.timezone = body.timezone;
  if (isRecord(body.input)) changed.input = body.input;
  if ("project" in body)
    changed.project = isRecord(body.project)
      ? (body.project as Schedule["project"])
      : null;
  if (typeof body.enabled === "boolean") changed.enabled = body.enabled;
  const wrong = await problem(changed);
  if (wrong) return wrong;
  schedules[index] = changed;
  return mockJson(view(changed));
}

export function deleteMockSchedules(app: string) {
  for (let i = schedules.length - 1; i >= 0; i--)
    if (schedules[i].app === app) schedules.splice(i, 1);
}
