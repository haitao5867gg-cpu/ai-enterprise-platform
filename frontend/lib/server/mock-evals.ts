import "server-only";
import type { EvalCase, EvalCheck, EvalSet, RunFeedback } from "../evals";
import { MAX_CASES, MAX_CHECKS, SAME_MEANING } from "../evals";
import { isRecord } from "../runs";
import {
  invalid,
  mockError,
  mockJson,
  readBody,
  type ValidationIssue,
} from "./mock-assets";

// Like the backend (docs/api/evals.md): flow apps' eval sets and feedback on runs.
type Feedback = { rating: "good" | "bad"; note: string | null; at: string };
const globalStore = globalThis as typeof globalThis & {
  aepMockEvalSets?: Map<string, EvalSet>;
  aepMockFeedback?: Map<string, Feedback>;
};
const sets = (globalStore.aepMockEvalSets ??= new Map<string, EvalSet>());
// One rating per run: the mock has one person.
const feedback = (globalStore.aepMockFeedback ??= new Map<string, Feedback>());

const READ_ONLY = "这个应用的评测集在仓库里维护，页面上不能改";
const CASE_ID = /^[a-z0-9_-]{1,60}$/;

export function mockEvalSetOf(app: string): EvalSet | undefined {
  return sets.get(app);
}
export function mockHasEvalSet(app: string): boolean {
  return app === "etl" || Boolean(sets.get(app)?.cases.length);
}
export function copyMockEvalSet(from: string, to: string) {
  const found = sets.get(from);
  if (found) sets.set(to, { ...structuredClone(found), app: to });
}

function view(app: string, kind: "code" | "flow" | "agent"): EvalSet {
  if (app === "etl")
    return {
      app,
      source: "file",
      editable: false,
      threshold: 0.8,
      judge_model: null,
      cases: ["orders-clean", "customers-clean"].map((id) => ({
        id,
        title: null,
        input: {},
        answers: {},
        expect: [],
        from_run: null,
      })),
      updated_by: null,
      updated_at: null,
    };
  return (
    sets.get(app) ?? {
      app,
      source: null,
      editable: kind !== "code",
      threshold: 0.8,
      judge_model: null,
      cases: [],
      updated_by: null,
      updated_at: null,
    }
  );
}

function checkIssues(check: unknown, at: (string | number)[]) {
  const issues: ValidationIssue[] = [];
  if (!isRecord(check)) return [{ loc: at, msg: "检查必须是对象" }];
  if (check.kind === "contains" || check.kind === "excludes") {
    if (
      !Array.isArray(check.values) ||
      !check.values.length ||
      !check.values.every((v) => typeof v === "string" && v.trim())
    )
      issues.push({ loc: [...at, "values"], msg: "至少写一个词" });
  } else if (check.kind === "judge") {
    if (typeof check.criteria !== "string" || !check.criteria.trim())
      issues.push({ loc: [...at, "criteria"], msg: "判断标准不能为空" });
  } else if (check.kind !== "equals")
    issues.push({ loc: [...at, "kind"], msg: "检查方式不对" });
  return issues;
}

function caseIssues(cases: unknown): ValidationIssue[] {
  if (!Array.isArray(cases))
    return [{ loc: ["body", "cases"], msg: "cases 必须是列表" }];
  const issues: ValidationIssue[] = [];
  if (cases.length > MAX_CASES)
    issues.push({ loc: ["body", "cases"], msg: `最多 ${MAX_CASES} 条用例` });
  const seen = new Set<string>();
  cases.forEach((item, i) => {
    const at = ["body", "cases", i];
    if (!isRecord(item)) return issues.push({ loc: at, msg: "用例必须是对象" });
    if (typeof item.id !== "string" || !CASE_ID.test(item.id))
      issues.push({ loc: [...at, "id"], msg: "用例标识不对" });
    else if (seen.has(item.id))
      issues.push({ loc: [...at, "id"], msg: `用例 id ${item.id} 重复了` });
    else seen.add(item.id as string);
    const expect = Array.isArray(item.expect) ? item.expect : [];
    if (expect.length > MAX_CHECKS)
      issues.push({ loc: [...at, "expect"], msg: `最多 ${MAX_CHECKS} 项检查` });
    expect.forEach((check, j) =>
      issues.push(...checkIssues(check, [...at, "expect", j])),
    );
  });
  return issues;
}

export async function mockEvalSet(
  request: Request,
  app: string,
  kind: "code" | "flow" | "agent",
  fromRun: boolean,
): Promise<Response> {
  if (request.method === "GET") return mockJson(view(app, kind));
  if (app === "etl" || kind === "code") return mockError(READ_ONLY, 409);
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (fromRun) {
    const run =
      typeof body.run_id === "string" ? mockRunCase(body.run_id) : null;
    if (!run) return mockError("没有找到这次运行", 404);
    if (run.app !== app) return mockError(`这次运行不是应用 ${app} 的`, 409);
    if (run.status !== "done" && run.status !== "error")
      return mockError("运行结束后才能存成用例", 409);
    const set = view(app, kind);
    if (set.cases.length >= MAX_CASES)
      return mockError(`一个评测集最多 ${MAX_CASES} 条用例`, 409);
    const base = `run-${run.run_id
      .toLowerCase()
      .replace(/[^a-z0-9]/g, "")
      .slice(-8)}`;
    let id = base;
    for (let n = 2; set.cases.some((item) => item.id === id); n++)
      id = `${base}-${n}`;
    const good = body.rating !== "bad" && run.status === "done";
    const made: EvalCase = {
      id,
      title: run.title,
      input: run.input,
      answers: run.answers,
      expect: good ? suggested(run.result) : [],
      from_run: run.run_id,
    };
    sets.set(app, {
      ...set,
      source: "platform",
      editable: true,
      cases: [...set.cases, made],
      updated_by: null,
      updated_at: new Date().toISOString(),
    });
    return mockJson(made, 201);
  }
  const issues = caseIssues(body.cases);
  if (
    body.threshold !== undefined &&
    body.threshold !== null &&
    (typeof body.threshold !== "number" ||
      body.threshold < 0 ||
      body.threshold > 1)
  )
    issues.push({ loc: ["body", "threshold"], msg: "门槛在 0 到 1 之间" });
  if (issues.length) return invalid(issues);
  const before = sets.get(app);
  const saved: EvalSet = {
    app,
    source: "platform",
    editable: true,
    threshold:
      typeof body.threshold === "number"
        ? body.threshold
        : (before?.threshold ?? 0.8),
    judge_model:
      typeof body.judge_model === "string" && body.judge_model
        ? body.judge_model
        : null,
    cases: (body.cases as EvalCase[]).map((item) => ({
      id: item.id,
      title: item.title?.trim() || null,
      input: isRecord(item.input) ? item.input : {},
      answers: isRecord(item.answers) ? item.answers : {},
      expect: Array.isArray(item.expect) ? item.expect : [],
      from_run: item.from_run ?? null,
    })),
    updated_by: null,
    updated_at: new Date().toISOString(),
  };
  sets.set(app, saved);
  return mockJson(saved);
}

function suggested(result: unknown): EvalCheck[] {
  if (!isRecord(result))
    return result == null
      ? []
      : [{ kind: "equals", path: null, value: result }];
  return Object.entries(result)
    .slice(0, MAX_CHECKS)
    .map(([key, value]) =>
      typeof value === "string" && value.length > 50
        ? { kind: "judge", path: key, criteria: SAME_MEANING, reference: value }
        : { kind: "equals", path: key, value },
    );
}

// The run a case is saved from, as mock-runs knows it.
export type MockRunCase = {
  run_id: string;
  app: string;
  status: string;
  title: string | null;
  input: Record<string, unknown>;
  result: unknown;
  answers: EvalCase["answers"];
};
let runCase: (id: string) => MockRunCase | null = () => null;
export function setMockRunCase(lookup: (id: string) => MockRunCase | null) {
  runCase = lookup;
}
const mockRunCase = (id: string) => runCase(id);

export function mockFeedbackOf(runId: string, app: string): RunFeedback {
  const mine = feedback.get(runId) ?? null;
  const cases = (sets.get(app)?.cases ?? [])
    .filter((item) => item.from_run === runId)
    .map((item) => ({ app, case_id: item.id }));
  return {
    good: mine?.rating === "good" ? 1 : 0,
    bad: mine?.rating === "bad" ? 1 : 0,
    mine,
    cases,
  };
}

export async function mockFeedback(
  request: Request,
  run: { run_id: string; app: string; status: string },
): Promise<Response> {
  if (run.status !== "done" && run.status !== "error")
    return mockError("运行结束后才能评价", 409);
  if (request.method === "DELETE") {
    feedback.delete(run.run_id);
    return mockJson(mockFeedbackOf(run.run_id, run.app));
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  if (body.rating !== "good" && body.rating !== "bad")
    return invalid([{ loc: ["body", "rating"], msg: "只能是 good 或 bad" }]);
  const note =
    typeof body.note === "string" && body.note.trim()
      ? body.note.trim().slice(0, 500)
      : null;
  feedback.set(run.run_id, {
    rating: body.rating,
    note,
    at: new Date().toISOString(),
  });
  return mockJson(mockFeedbackOf(run.run_id, run.app));
}
