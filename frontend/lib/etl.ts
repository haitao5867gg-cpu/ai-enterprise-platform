import { ApiError, isRecord } from "./runs";

export type EtlLanguage = "pyspark" | "sql";
export const languageLabels: Record<EtlLanguage, string> = {
  pyspark: "PySpark",
  sql: "SQL",
};
export type DataSource = {
  datasource_id: string;
  name: string;
  kind: string;
  description: string;
};
export type EtlTarget = {
  name: string;
  description: string;
  sources: string[];
  logic: string;
  columns: { name: string; source: string; description: string }[];
  primary_key: string[];
  load: { mode: "full" | "incremental"; key: string | null };
  quality_rules: {
    name: string;
    condition: string;
    on_violation: "warn" | "drop" | "fail";
  }[];
};
export type EtlPlan = {
  summary: string;
  language: EtlLanguage;
  targets: EtlTarget[];
};
export type EtlVerification = {
  status: "passed" | "failed" | "skipped";
  reason: string;
  mode: "pipeline" | "job" | "cluster" | null;
  url: string | null;
  errors: string[];
  warnings: string[];
  tables: { name: string; rows: number | null; dropped: number }[];
};
export type EtlResult = {
  bundle: string;
  language: EtlLanguage;
  files: Record<string, string>;
  checks: { passed: boolean; problems: string[] };
  // The Databricks dev run of the bundle; null while it has not happened.
  verification: EtlVerification | null;
};

function isLanguage(value: unknown): value is EtlLanguage {
  return value === "pyspark" || value === "sql";
}
function isStrings(value: unknown): value is string[] {
  return (
    Array.isArray(value) &&
    value.every((item: unknown) => typeof item === "string")
  );
}

export function readDataSources(value: unknown): DataSource[] {
  if (
    !Array.isArray(value) ||
    !value.every(
      (item: unknown) =>
        isRecord(item) &&
        typeof item.datasource_id === "string" &&
        Boolean(item.datasource_id) &&
        typeof item.name === "string" &&
        typeof item.kind === "string" &&
        typeof item.description === "string",
    )
  )
    throw new ApiError("数据源列表格式不正确，请重试。");
  return value;
}

function isTarget(value: unknown): value is EtlTarget {
  return (
    isRecord(value) &&
    typeof value.name === "string" &&
    typeof value.description === "string" &&
    isStrings(value.sources) &&
    typeof value.logic === "string" &&
    Array.isArray(value.columns) &&
    value.columns.every(
      (column: unknown) =>
        isRecord(column) &&
        typeof column.name === "string" &&
        typeof column.source === "string" &&
        typeof column.description === "string",
    ) &&
    isStrings(value.primary_key) &&
    isRecord(value.load) &&
    (value.load.mode === "full" || value.load.mode === "incremental") &&
    (value.load.key === null || typeof value.load.key === "string") &&
    Array.isArray(value.quality_rules) &&
    value.quality_rules.every(
      (rule: unknown) =>
        isRecord(rule) &&
        typeof rule.name === "string" &&
        typeof rule.condition === "string" &&
        (rule.on_violation === "warn" ||
          rule.on_violation === "drop" ||
          rule.on_violation === "fail"),
    )
  );
}

// Malformed or future details fall back to the prompt and approval controls.
export function getEtlPlan(details: unknown): EtlPlan | null {
  if (!isRecord(details) || details.type !== "etl_plan") return null;
  const plan = details.plan;
  return isRecord(plan) &&
    typeof plan.summary === "string" &&
    isLanguage(plan.language) &&
    Array.isArray(plan.targets) &&
    plan.targets.length > 0 &&
    plan.targets.every(isTarget)
    ? (plan as EtlPlan)
    : null;
}

// Unknown or partial fields are dropped rather than failing the whole result.
function readVerification(value: unknown): EtlVerification | null {
  if (
    !isRecord(value) ||
    (value.status !== "passed" &&
      value.status !== "failed" &&
      value.status !== "skipped")
  )
    return null;
  const mode =
    value.mode === "pipeline" ||
    value.mode === "job" ||
    value.mode === "cluster"
      ? value.mode
      : null;
  const tables = Array.isArray(value.tables)
    ? value.tables.flatMap((table: unknown) =>
        isRecord(table) && typeof table.name === "string"
          ? [
              {
                name: table.name,
                rows: typeof table.rows === "number" ? table.rows : null,
                dropped: typeof table.dropped === "number" ? table.dropped : 0,
              },
            ]
          : [],
      )
    : [];
  return {
    status: value.status,
    reason: typeof value.reason === "string" ? value.reason : "",
    mode,
    url:
      typeof value.url === "string" && /^https:\/\//.test(value.url)
        ? value.url
        : null,
    errors: isStrings(value.errors) ? value.errors : [],
    warnings: isStrings(value.warnings) ? value.warnings : [],
    tables,
  };
}

export function getEtlResult(value: unknown): EtlResult | null {
  return isRecord(value) &&
    typeof value.bundle === "string" &&
    isLanguage(value.language) &&
    isRecord(value.files) &&
    Object.values(value.files).every(
      (content) => typeof content === "string",
    ) &&
    isRecord(value.checks) &&
    typeof value.checks.passed === "boolean" &&
    isStrings(value.checks.problems)
    ? {
        ...(value as Omit<EtlResult, "verification">),
        verification: readVerification(value.verification),
      }
    : null;
}

// A finished ETL run went wrong when its code failed the checks or its dev run failed.
export function etlFailed(result: EtlResult): boolean {
  return !result.checks.passed || result.verification?.status === "failed";
}
