import type { FlowConfig } from "./apps";
import type { EtlLanguage, EtlPlan } from "./etl";
import type { ValidationIssue } from "./runs";

export type EtlBundle = {
  name: string;
  language: EtlLanguage;
  files: Record<string, string>;
};
export type EtlProfileOutput = {
  source_id: string;
  source_name: string;
  tables: Record<string, unknown>[];
  names: string[];
  description: string;
  summary: string;
};
export type EtlPlanCheckOutput = {
  ok: boolean;
  plan: EtlPlan | Record<string, unknown> | null;
  problems: string;
  message: string;
  sources: string[];
  targets: string[];
};
export type DatabricksIngestOutput = {
  catalog: string;
  tables: { source: string; target: string; rows: number; columns: number }[];
  summary: string;
};
export type EtlCodeCheckOutput = {
  ok: boolean;
  code: Record<string, string>;
  problems: string;
  bundle: EtlBundle | null;
  summary: string;
};
export type TablePreview = {
  table: string;
  target: string;
  columns: string[];
  rows: Record<string, unknown>[];
};
export type DatabricksDevRunOutput = {
  status: "passed" | "failed";
  run: {
    status: "passed" | "failed";
    stage: "validate" | "deploy" | "run";
    steps: { name: string; ok: boolean; output: string }[];
    errors: string[];
    warnings: string[];
    fixable: boolean;
    mode: "pipeline" | "job" | "cluster";
    url: string | null;
    tables: {
      name: string;
      status: string;
      rows: number | null;
      dropped: number;
      expectations: Record<string, unknown>[];
    }[];
    attempt: number;
    cluster_id?: string | null;
    pipeline_id?: string | null;
    update_id?: string | null;
    job_id?: number | null;
    run_id?: number | null;
  };
  url: string | null;
  tables: {
    name: string;
    target: string;
    rows: number | null;
    dropped: number;
    expectations: Record<string, unknown>[];
  }[];
  preview: TablePreview[];
  errors: string;
  report: string;
};

export function dataNodeIssues(config: FlowConfig): ValidationIssue[] {
  return config.graph.nodes.flatMap((node) => {
    const issues: ValidationIssue[] = [];
    const add = (key: string, msg: string) =>
      issues.push({
        loc: ["body", "config", "graph", "nodes", node.id, key],
        msg,
      });
    if (
      node.note !== undefined &&
      (typeof node.note !== "string" || Array.from(node.note).length > 500)
    )
      add("note", "讲解说明最多 500 字");
    const integer = (key: string, value: number, min: number, max: number) => {
      if (!Number.isInteger(value) || value < min || value > max)
        add(key, `须为 ${min}–${max} 的整数`);
    };
    if (node.type === "etl_profile")
      integer("sample_rows", node.sample_rows, 1, 20);
    if (node.type === "databricks_ingest")
      integer("max_rows", node.max_rows, 1, 1_000_000);
    if (node.type === "etl_plan_check" || node.type === "etl_code_check")
      integer("max_attempts", node.max_attempts, 1, 5);
    if (
      node.type === "etl_plan_check" &&
      (!Array.isArray(node.hits) || node.hits.length > 5)
    )
      add("hits", "最多 5 个引用");
    if (node.type === "databricks_dev_run") {
      integer("max_runs", node.max_runs, 1, 3);
      integer("preview_rows", node.preview_rows, 0, 100);
    }
    return issues;
  });
}
