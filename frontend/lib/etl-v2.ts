// What the ETL template's second version shows a person (docs/api/data-nodes.md): the
// business brief and plan of the plan check, the answer worked out on the cluster, the
// records of the two human steps, and the result of a run that got to one of its ends.
import type { Reference } from "./knowledge";
import { isRecord } from "./runs";

export type BriefMetric = {
  id: string;
  name: string;
  unit: string | null;
  definition: string;
  column: string | null;
};
export type BriefDimension = {
  id: string;
  name: string;
  definition: string;
  column: string | null;
  examples: string[];
};
export type Brief = {
  goal: string;
  grain: string;
  metrics: BriefMetric[];
  dimensions: BriefDimension[];
  filters: { id: string; text: string }[];
  time_range: { text: string; column: string | null } | null;
};
export type PlanRule = { id: string; text: string; basis: string };
export type PlanQuestion = {
  id: string;
  text: string;
  why: string;
  options: { value: string; label: string }[];
  suggested: string | null;
};
export type PlanColumn = {
  name: string;
  label: string;
  source: string;
  description: string;
};
export type QualityRule = {
  name: string;
  condition: string;
  on_violation: string;
};
export type PlanTarget = {
  name: string;
  description: string;
  sources: string[];
  logic: string;
  columns: PlanColumn[];
  primary_key: string[];
  load: { mode: string; key: string | null };
  quality_rules: QualityRule[];
};
export type PlanV2 = {
  summary: string;
  language: string;
  targets: PlanTarget[];
  rules: PlanRule[];
  questions: PlanQuestion[];
  references: Reference[];
};
export type AnswerColumn = {
  name: string;
  label: string;
  role: "dimension" | "metric";
  agg: string | null;
  unit: string | null;
  format: "integer" | "decimal" | null;
};
export type Chart = { type: string; label: string[]; value: string | null };
export type AnswerDesign = {
  target: string;
  columns: AnswerColumn[];
  chart: Chart;
  text: string;
};
export type Quality = {
  rules: number;
  failed_rules: number;
  dropped: number;
  text: string;
};
export type Answer = {
  status: "ok" | "error";
  target: string;
  period: string | null;
  columns: AnswerColumn[];
  rows: Record<string, unknown>[];
  totals: Record<string, unknown>;
  groups: number | null;
  truncated: boolean;
  conclusion: string;
  chart: Chart;
  quality: Quality | null;
  sql: string;
  url: string | null;
  error: string;
};
export type ReviewAnswer = {
  key: string;
  label: string;
  value_label: string;
  suggested_label: string | null;
  picked_suggested: boolean | null;
};
export type ReviewRecord = {
  node: string;
  title: string;
  round: number;
  actor: string | null;
  at: string | null;
  decision: string;
  decision_label: string;
  submitted: string | null;
  seen: string | null;
  comment: string;
  changed: boolean;
  notes: { item: string; label: string; text: string }[];
  answers: ReviewAnswer[];
};
export type DevTable = {
  name: string;
  target: string;
  rows: number | null;
  dropped: number;
  expectations: { name: string; passed: number; failed: number }[];
};
export type DevPreview = {
  table: string;
  target: string;
  columns: string[];
  rows: Record<string, unknown>[];
  sorted_by: string[] | null;
};

const text = (value: unknown) => (typeof value === "string" ? value : "");
const textOrNull = (value: unknown) =>
  typeof value === "string" && value ? value : null;
const texts = (value: unknown) =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
const records = (value: unknown) =>
  Array.isArray(value) ? value.filter(isRecord) : [];
const count = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

// The business brief; null when there is none (a first version plan has none).
export function readBrief(value: unknown): Brief | null {
  if (!isRecord(value) || typeof value.goal !== "string") return null;
  const range = isRecord(value.time_range) ? value.time_range : null;
  return {
    goal: value.goal,
    grain: text(value.grain),
    metrics: records(value.metrics).map((metric) => ({
      id: text(metric.id),
      name: text(metric.name),
      unit: textOrNull(metric.unit),
      definition: text(metric.definition),
      column: textOrNull(metric.column),
    })),
    dimensions: records(value.dimensions).map((dimension) => ({
      id: text(dimension.id),
      name: text(dimension.name),
      definition: text(dimension.definition),
      column: textOrNull(dimension.column),
      examples: texts(dimension.examples),
    })),
    filters: records(value.filters).map((filter) => ({
      id: text(filter.id),
      text: text(filter.text),
    })),
    time_range:
      range && typeof range.text === "string"
        ? { text: range.text, column: textOrNull(range.column) }
        : null,
  };
}

// The technical plan; null when it is not a plan that passed its check.
export function readPlanV2(value: unknown): PlanV2 | null {
  if (
    !isRecord(value) ||
    !Array.isArray(value.targets) ||
    !value.targets.length ||
    !value.targets.every(
      (target) => isRecord(target) && typeof target.name === "string",
    )
  )
    return null;
  return {
    summary: text(value.summary),
    language: text(value.language),
    targets: records(value.targets).map((target) => {
      const load = isRecord(target.load) ? target.load : {};
      return {
        name: text(target.name),
        description: text(target.description),
        sources: texts(target.sources),
        logic: text(target.logic),
        columns: records(target.columns).map((column) => ({
          name: text(column.name),
          label: text(column.label),
          source: text(column.source),
          description: text(column.description),
        })),
        primary_key: texts(target.primary_key),
        load: { mode: text(load.mode), key: textOrNull(load.key) },
        quality_rules: records(target.quality_rules).map((rule) => ({
          name: text(rule.name),
          condition: text(rule.condition),
          on_violation: text(rule.on_violation),
        })),
      };
    }),
    rules: records(value.rules).map((rule) => ({
      id: text(rule.id),
      text: text(rule.text),
      basis: text(rule.basis) || "assumption",
    })),
    questions: records(value.questions).map((question) => ({
      id: text(question.id),
      text: text(question.text),
      why: text(question.why),
      options: records(question.options).map((option) => ({
        value: String(option.value ?? ""),
        label: text(option.label),
      })),
      suggested: question.suggested == null ? null : String(question.suggested),
    })),
    references: Array.isArray(value.references)
      ? (value.references.filter(isRecord) as Reference[])
      : [],
  };
}

function readColumns(value: unknown): AnswerColumn[] {
  return records(value)
    .filter((column) => typeof column.name === "string")
    .map((column) => ({
      name: String(column.name),
      label: text(column.label) || String(column.name),
      role: column.role === "dimension" ? "dimension" : "metric",
      agg: textOrNull(column.agg),
      unit: textOrNull(column.unit),
      format:
        column.format === "integer" || column.format === "decimal"
          ? column.format
          : null,
    }));
}

function readChart(value: unknown): Chart {
  const chart = isRecord(value) ? value : {};
  return {
    type: text(chart.type) || "none",
    label: texts(chart.label),
    value: textOrNull(chart.value),
  };
}

export function readQuality(value: unknown): Quality | null {
  if (!isRecord(value) || typeof value.text !== "string") return null;
  return {
    rules: count(value.rules) ?? 0,
    failed_rules: count(value.failed_rules) ?? 0,
    dropped: count(value.dropped) ?? 0,
    text: value.text,
  };
}

// The answer design a plan check gives; null when the plan has none.
export function readAnswerDesign(value: unknown): AnswerDesign | null {
  if (!isRecord(value) || typeof value.target !== "string") return null;
  return {
    target: value.target,
    columns: readColumns(value.columns),
    chart: readChart(value.chart),
    text: text(value.text),
  };
}

// The answer worked out on the cluster; null when there is none.
export function readAnswer(value: unknown): Answer | null {
  if (!isRecord(value) || (value.status !== "ok" && value.status !== "error"))
    return null;
  return {
    status: value.status,
    target: text(value.target),
    period: value.period == null ? null : String(value.period),
    columns: readColumns(value.columns),
    rows: records(value.rows),
    totals: isRecord(value.totals) ? value.totals : {},
    groups: count(value.groups),
    truncated: value.truncated === true,
    conclusion: text(value.conclusion),
    chart: readChart(value.chart),
    quality: readQuality(value.quality),
    sql: text(value.sql),
    url: httpsUrl(value.url),
    error: text(value.error),
  };
}

// The record a human step with extended settings leaves; null before it decided.
export function readRecord(value: unknown): ReviewRecord | null {
  if (
    !isRecord(value) ||
    typeof value.node !== "string" ||
    typeof value.decision !== "string"
  )
    return null;
  return {
    node: value.node,
    title: text(value.title),
    round: count(value.round) ?? 1,
    actor: textOrNull(value.actor),
    at: textOrNull(value.at),
    decision: value.decision,
    decision_label: text(value.decision_label) || value.decision,
    submitted: textOrNull(value.submitted),
    seen: textOrNull(value.seen),
    comment: text(value.comment),
    changed: value.changed === true,
    notes: records(value.notes).map((note) => ({
      item: text(note.item),
      label: text(note.label) || text(note.item),
      text: text(note.text),
    })),
    answers: records(value.answers).map((answer) => ({
      key: text(answer.key),
      label: text(answer.label),
      value_label: text(answer.value_label) || String(answer.value ?? ""),
      suggested_label: textOrNull(answer.suggested_label),
      picked_suggested:
        typeof answer.picked_suggested === "boolean"
          ? answer.picked_suggested
          : null,
    })),
  };
}

export function readDevTables(value: unknown): DevTable[] {
  return records(value).map((table) => ({
    name: text(table.name),
    target: text(table.target),
    rows: count(table.rows),
    dropped: count(table.dropped) ?? 0,
    expectations: records(table.expectations).map((rule) => ({
      name: text(rule.name),
      passed: count(rule.passed) ?? 0,
      failed: count(rule.failed) ?? 0,
    })),
  }));
}

export function readPreviews(value: unknown): DevPreview[] {
  return records(value)
    .filter((preview) => typeof preview.table === "string")
    .map((preview) => ({
      table: String(preview.table),
      target: text(preview.target),
      columns: texts(preview.columns),
      rows: records(preview.rows),
      sorted_by: Array.isArray(preview.sorted_by)
        ? texts(preview.sorted_by)
        : null,
    }));
}

export function httpsUrl(value: unknown): string | null {
  return typeof value === "string" && /^https:\/\//.test(value) ? value : null;
}

// Whether the record ``returned`` (the data review's) sends the plan back after the
// plan review last decided (``previous``): then the plan review says so at its top.
export function returnedForPlan(
  returned: ReviewRecord | null,
  previous: ReviewRecord | null,
): boolean {
  if (!returned || returned.decision !== "fix_plan") return false;
  if (!previous) return true;
  return Boolean(
    returned.at &&
    previous.at &&
    Date.parse(returned.at) > Date.parse(previous.at),
  );
}

const integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

// A value as the answer card writes it: a number by its column's format, never by
// whether it has a decimal point (JSON in the browser cannot tell 1234567.0 from 1234567).
export function shown(
  value: unknown,
  format: AnswerColumn["format"] = null,
): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number" && format === "integer")
    return integer.format(value);
  if (typeof value === "number" && format === "decimal")
    return decimal.format(value);
  return typeof value === "string" ? value : JSON.stringify(value);
}

// The rows as a CSV file, with the values as they came (not formatted).
export function answerCsv(answer: Answer): string {
  const cell = (value: unknown) => {
    const raw =
      value === null || value === undefined
        ? ""
        : typeof value === "string"
          ? value
          : JSON.stringify(value);
    return /[",\n\r]/.test(raw) ? `"${raw.replaceAll('"', '""')}"` : raw;
  };
  return [
    answer.columns.map((column) => cell(column.label)).join(","),
    ...answer.rows.map((row) =>
      answer.columns.map((column) => cell(row[column.name])).join(","),
    ),
  ].join("\r\n");
}

// The result of a run of the second version: a brief with a goal, or the code as the
// code check's bundle (path → text), as files.zip reads it.
export function isV2Result(result: unknown): result is Record<string, unknown> {
  if (!isRecord(result)) return false;
  if (isRecord(result.brief) && typeof result.brief.goal === "string")
    return true;
  return isRecord(result.code) && isFileMap(result.code.files);
}

export function isFileMap(value: unknown): value is Record<string, string> {
  return (
    isRecord(value) &&
    Object.values(value).every((content) => typeof content === "string")
  );
}
