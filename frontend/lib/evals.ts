import { requestJson } from "./runs";

// docs/api/evals.md is the source of truth for these wire types.
export type EvalCheck =
  | { kind: "equals"; path: string | null; value: unknown }
  | { kind: "contains" | "excludes"; path: string | null; values: string[] }
  | {
      kind: "judge";
      path: string | null;
      criteria: string;
      reference: unknown;
    };
export type EvalAnswer = {
  decision?: string;
  comment?: string | null;
  values?: Record<string, unknown>;
};
export type EvalCase = {
  id: string;
  title: string | null;
  input: Record<string, unknown>;
  answers: Record<string, EvalAnswer>;
  expect: EvalCheck[];
  from_run: string | null;
  // A turn of a conversation (docs/api/evals.md): the history the model saw, the turn's
  // number, and how the masked values in that history read. Left out: a one-shot run.
  history?: { message: string; reply: string | null }[];
  turn?: number | null;
  restored?: Record<string, string>;
};
export type EvalSet = {
  app: string;
  source: "platform" | "file" | null;
  editable: boolean;
  threshold: number;
  judge_model: string | null;
  // The etl file set's cases have their own shape; only flow cases are edited here.
  cases: EvalCase[];
  updated_by: string | null;
  updated_at: string | null;
};
export type CheckResult = {
  kind: string;
  name: string;
  passed: boolean | null;
  detail: string;
};
export type RunFeedback = {
  good: number;
  bad: number;
  mine: { rating: "good" | "bad"; note: string | null; at: string } | null;
  cases: { app: string; case_id: string }[];
};

export const MAX_CASES = 50;
export const MAX_CHECKS = 20;
export const SAME_MEANING = "意思和参考答案一致";

export const checkKinds: Record<EvalCheck["kind"], string> = {
  equals: "等于",
  contains: "包含",
  excludes: "不含",
  judge: "模型判断",
};

const base = (app: string) => `/apps/${encodeURIComponent(app)}/eval-set`;
export const getEvalSet = (app: string, signal?: AbortSignal) =>
  requestJson(base(app), { signal }) as Promise<EvalSet>;
export const putEvalSet = (
  app: string,
  body: {
    cases: EvalCase[];
    threshold: number;
    judge_model: string | null;
  },
) =>
  requestJson(base(app), {
    method: "PUT",
    body: JSON.stringify(body),
  }) as Promise<EvalSet>;
export const caseFromRun = (
  app: string,
  runId: string,
  rating: "good" | "bad",
) =>
  requestJson(`${base(app)}/cases/from-run`, {
    method: "POST",
    body: JSON.stringify({ run_id: runId, rating }),
  }) as Promise<EvalCase>;

const runBase = (id: string) => `/runs/${encodeURIComponent(id)}`;
export const rateRun = (
  id: string,
  rating: "good" | "bad",
  note: string | null,
) =>
  requestJson(`${runBase(id)}/feedback`, {
    method: "PUT",
    body: JSON.stringify(note ? { rating, note } : { rating }),
  }) as Promise<RunFeedback>;
export const unrateRun = (id: string) =>
  requestJson(`${runBase(id)}/feedback`, {
    method: "DELETE",
  }) as Promise<RunFeedback>;
export const rerunFrom = (id: string, node: string, config: "draft" | "same") =>
  requestJson(`${runBase(id)}/rerun-from`, {
    method: "POST",
    body: JSON.stringify({ node, config }),
  }) as Promise<{ run_id: string; status: string }>;

// The value in words, for a table cell.
export function shown(value: unknown, max = 60): string {
  const text =
    typeof value === "string" ? value : (JSON.stringify(value) ?? "null");
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export function checkText(check: EvalCheck): string {
  const where = check.path || "结果";
  if (check.kind === "equals") return `${where} 等于 ${shown(check.value, 40)}`;
  if (check.kind === "judge")
    return `模型判断 ${where}：${shown(check.criteria, 40)}`;
  return `${where} ${checkKinds[check.kind]} ${check.values.join("、")}`;
}

// A value typed in a field: JSON when it reads as JSON (numbers, true, objects…),
// otherwise the text itself.
export function typedValue(text: string): unknown {
  const trimmed = text.trim();
  if (!trimmed) return "";
  try {
    return JSON.parse(trimmed);
  } catch {
    return text;
  }
}
export function valueText(value: unknown): string {
  if (value === undefined) return "";
  if (typeof value === "string") {
    // A string that would read back as something else keeps its quotes.
    return typedValue(value) === value ? value : JSON.stringify(value);
  }
  return JSON.stringify(value);
}

// The case to save: the shape the API takes, unused fields left out.
export function cleanCase(item: EvalCase): EvalCase {
  return {
    ...item,
    title: item.title?.trim() || null,
    expect: item.expect.map((check) => {
      const path = check.path?.trim() || null;
      if (check.kind === "equals") return { ...check, path };
      if (check.kind === "judge")
        return {
          ...check,
          path,
          criteria: check.criteria.trim(),
          reference:
            check.reference === "" || check.reference === undefined
              ? null
              : check.reference,
        };
      return {
        ...check,
        path,
        values: check.values.map((v) => v.trim()).filter(Boolean),
      };
    }),
  };
}
