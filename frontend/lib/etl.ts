import type { Reference } from "./knowledge";
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
  references?: Reference[];
  summary: string;
  language: EtlLanguage;
  targets: EtlTarget[];
  // What the model assumed where the request and the data did not line up; may be empty.
  assumptions: string[];
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
export const deployModeLabels: Record<
  NonNullable<EtlVerification["mode"]>,
  string
> = {
  pipeline: "Lakeflow 管道",
  job: "Job（已有集群）",
  cluster: "集群试跑，不写表",
};
export type EtlApproval = {
  step: string;
  decision: string;
  comment: string;
  actor: string;
  at: string;
};
export type EtlProdTarget = {
  mode: "job" | "pipeline" | null;
  catalog: string;
  schema: string;
  schedule: string;
  timezone: string;
};
// The prod deploy after the release approval.
export type EtlRelease = EtlProdTarget & {
  status: "deployed" | "failed" | "skipped";
  reason: string;
  url: string | null;
  errors: string[];
};
// The bundle's pull request in the assets repository.
export type EtlAsset = {
  status: string;
  reason: string;
  repo: string;
  version: number | null;
  prNumber: number | null;
  prUrl: string | null;
  errors: string[];
};
// What the release approval shows: the dev run that passed and where prod is.
export type EtlReleaseRequest = {
  bundle: string;
  verification: EtlVerification | null;
  target: EtlProdTarget;
};
export type EtlResult = {
  bundle: string;
  language: EtlLanguage;
  files: Record<string, string>;
  checks: { passed: boolean; problems: string[] };
  // The Databricks dev run of the bundle; null while it has not happened.
  verification: EtlVerification | null;
  approvals: EtlApproval[];
  release: EtlRelease | null;
  asset: EtlAsset | null;
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
    ? {
        ...(plan as Omit<EtlPlan, "assumptions">),
        // Runs from before assumptions existed have none.
        assumptions: isStrings(plan.assumptions) ? plan.assumptions : [],
        references: Array.isArray(plan.references)
          ? (plan.references as Reference[])
          : [],
      }
    : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}
function httpsUrl(value: unknown): string | null {
  return typeof value === "string" && /^https:\/\//.test(value) ? value : null;
}

function readProdTarget(value: Record<string, unknown>): EtlProdTarget {
  return {
    mode: value.mode === "job" || value.mode === "pipeline" ? value.mode : null,
    catalog: text(value.catalog),
    schema: text(value.schema),
    schedule: text(value.schedule),
    timezone: text(value.timezone),
  };
}

export function getEtlRelease(details: unknown): EtlReleaseRequest | null {
  if (
    !isRecord(details) ||
    details.type !== "etl_release" ||
    typeof details.bundle !== "string" ||
    !isRecord(details.target)
  )
    return null;
  return {
    bundle: details.bundle,
    verification: readVerification(details.verification),
    target: readProdTarget(details.target),
  };
}

function readApprovals(value: unknown): EtlApproval[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item: unknown) =>
    isRecord(item) && typeof item.step === "string"
      ? [
          {
            step: item.step,
            decision: text(item.decision),
            comment: text(item.comment),
            actor: text(item.actor),
            at: text(item.at),
          },
        ]
      : [],
  );
}

function readRelease(value: unknown): EtlRelease | null {
  if (
    !isRecord(value) ||
    (value.status !== "deployed" &&
      value.status !== "failed" &&
      value.status !== "skipped")
  )
    return null;
  return {
    ...readProdTarget(value),
    status: value.status,
    reason: text(value.reason),
    url: httpsUrl(value.url),
    errors: isStrings(value.errors) ? value.errors : [],
  };
}

function readAsset(value: unknown): EtlAsset | null {
  if (!isRecord(value) || typeof value.status !== "string") return null;
  return {
    status: value.status,
    reason: text(value.reason),
    repo: text(value.repo),
    version: typeof value.version === "number" ? value.version : null,
    prNumber: typeof value.pr_number === "number" ? value.pr_number : null,
    prUrl: httpsUrl(value.pr_url),
    errors: isStrings(value.errors) ? value.errors : [],
  };
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
    url: httpsUrl(value.url),
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
        bundle: value.bundle,
        language: value.language,
        files: value.files as Record<string, string>,
        checks: value.checks as EtlResult["checks"],
        verification: readVerification(value.verification),
        approvals: readApprovals(value.approvals),
        release: readRelease(value.release),
        asset: readAsset(value.asset),
      }
    : null;
}

// A finished ETL run went wrong when its code failed the checks, or its dev run or
// prod deploy failed.
export function etlFailed(result: EtlResult): boolean {
  return (
    !result.checks.passed ||
    result.verification?.status === "failed" ||
    result.release?.status === "failed"
  );
}
