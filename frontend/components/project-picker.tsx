"use client";

import Link from "next/link";
import { useEffect, useId, useState } from "react";
import type { ProjectUse } from "../lib/apps";
import {
  getProject,
  listProjects,
  paramProblem,
  type Param,
  type ParamValue,
  type Project,
  type ProjectChoice,
  type ProjectSummary,
} from "../lib/projects";
import { errorMessage } from "../lib/runs";
import { Icon } from "./icons";
import { ErrorNotice } from "./ui";

export type PickedProject = {
  project: Project | null;
  environment: string;
  values: Record<string, ParamValue>;
};

// What POST /runs gets, or the problems to show by parameter key ("" for the project).
export function projectChoice(
  uses: ProjectUse | null | undefined,
  picked: PickedProject,
): { choice?: ProjectChoice; problems: Record<string, string> } {
  const { project } = picked;
  if (!project)
    return uses === "required"
      ? { problems: { "": "这个应用需要选择一个项目" } }
      : { problems: {} };
  const problems: Record<string, string> = {};
  const params: Record<string, ParamValue> = {};
  for (const param of project.params) {
    const value = picked.values[param.key] ?? null;
    if (value === null || value === "") {
      if (param.required && param.default === null)
        problems[param.key] = `请填写参数「${param.label}」`;
      continue;
    }
    const problem = paramProblem(param, value);
    if (problem) problems[param.key] = problem;
    else params[param.key] = value;
  }
  return {
    choice: {
      project_id: project.project_id,
      environment: picked.environment || project.environments[0]?.key,
      params,
    },
    problems,
  };
}

function ParamInput({
  param,
  value,
  error,
  disabled,
  onChange,
}: {
  param: Param;
  value: ParamValue;
  error?: string;
  disabled?: boolean;
  onChange: (value: ParamValue) => void;
}) {
  const id = useId();
  const label = (
    <label htmlFor={id}>
      {param.label}
      {!param.required && <span className="muted">（选填）</span>}
    </label>
  );
  const pattern = param.type === "text" ? param.pattern : null;
  const hint = [param.description, pattern && `格式：${pattern}`].filter(Boolean).join("；");
  const described = [hint && `${id}-hint`, error && `${id}-error`]
    .filter(Boolean)
    .join(" ");
  const common = {
    id,
    disabled,
    "aria-invalid": Boolean(error),
    "aria-describedby": described || undefined,
  };
  let control;
  if (param.type === "boolean" || param.type === "select") {
    const choices =
      param.type === "boolean"
        ? [
            { value: "true", label: "是" },
            { value: "false", label: "否" },
          ]
        : (param.choices ?? []).map((choice) => ({
            value: choice.value,
            label: choice.label ?? choice.value,
          }));
    control = (
      <select
        {...common}
        value={value === null ? "" : String(value)}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === "" ? null : param.type === "boolean" ? next === "true" : next);
        }}
      >
        <option value="">{param.required ? "请选择" : "不填"}</option>
        {choices.map((choice) => (
          <option key={choice.value} value={choice.value}>
            {choice.label}
          </option>
        ))}
      </select>
    );
  } else
    control = (
      <input
        {...common}
        type={
          param.type === "number"
            ? "number"
            : param.type === "date"
              ? "date"
              : param.type === "month"
                ? "month"
                : "text"
        }
        step={param.type === "number" ? "any" : undefined}
        maxLength={param.type === "text" ? 500 : undefined}
        placeholder={param.type === "month" ? "YYYY-MM" : undefined}
        value={value === null ? "" : String(value)}
        onChange={(event) => {
          const raw = event.target.value;
          if (!raw) return onChange(null);
          if (param.type === "number") {
            const number = event.target.valueAsNumber;
            return onChange(Number.isFinite(number) ? number : null);
          }
          onChange(raw);
        }}
      />
    );
  return (
    <div className="asset-field">
      {label}
      {control}
      {hint && (
        <small className="muted" id={`${id}-hint`}>
          {hint}
        </small>
      )}
      {error && (
        <p className="field-error" id={`${id}-error`}>
          {error}
        </p>
      )}
    </div>
  );
}

// Picks the project a new run binds, its environment and the parameter values; the
// parent sends them with projectChoice().
export function ProjectPicker({
  uses,
  picked,
  onChange,
  errors,
  disabled,
}: {
  uses: ProjectUse;
  picked: PickedProject;
  onChange: (picked: PickedProject) => void;
  errors: Record<string, string>;
  disabled?: boolean;
}) {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    listProjects(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setProjects(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [revision]);

  async function pick(id: string) {
    if (!id) return onChange({ project: null, environment: "", values: {} });
    setLoading(true);
    setError("");
    try {
      const project = await getProject(id);
      onChange({
        project,
        environment: project.environments[0]?.key ?? "",
        values: Object.fromEntries(project.params.map((param) => [param.key, param.default])),
      });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }

  const { project } = picked;
  const env = project?.environments.find((item) => item.key === picked.environment);
  return (
    <div className="form-section project-picker">
      <label htmlFor="project">
        项目{uses === "optional" && <span className="muted">（选填）</span>}
      </label>
      <select
        id="project"
        value={project?.project_id ?? ""}
        disabled={disabled || loading || projects === null}
        aria-invalid={Boolean(errors[""])}
        aria-describedby="project-hint"
        onChange={(event) => void pick(event.target.value)}
      >
        <option value="">
          {projects === null
            ? "正在加载项目…"
            : uses === "required"
              ? "请选择项目"
              : "不绑定项目"}
        </option>
        {(projects ?? []).map((item) => (
          <option key={item.project_id} value={item.project_id}>
            {item.name}
          </option>
        ))}
      </select>
      <p className="field-hint" id="project-hint">
        {project
          ? project.description || "运行按项目的设置读写数据。"
          : uses === "required"
            ? "这个应用的运行要绑定一个项目：写到项目的环境，按项目的参数和数据外发策略。"
            : "可以不绑定项目；绑定后按项目的环境、参数和数据外发策略运行。"}
      </p>
      {errors[""] && <p className="field-error">{errors[""]}</p>}
      {error && <ErrorNotice message={error} onRetry={() => setRevision((n) => n + 1)} />}
      {projects !== null && !projects.length && (
        <p className="field-hint" role="status">
          还没有项目，请先在<Link href="/projects/new">项目</Link>里新建，再回来刷新。
        </p>
      )}
      {projects !== null && (
        <button
          type="button"
          className="text-button source-refresh"
          disabled={disabled}
          onClick={() => setRevision((n) => n + 1)}
        >
          <Icon name="refresh" size={14} />
          刷新项目
        </button>
      )}
      {project && (
        <>
          <label htmlFor="project-environment">环境</label>
          <select
            id="project-environment"
            value={picked.environment}
            disabled={disabled}
            aria-describedby="project-environment-hint"
            onChange={(event) => onChange({ ...picked, environment: event.target.value })}
          >
            {project.environments.map((item) => (
              <option key={item.key} value={item.key}>
                {item.label}（{item.key}）
              </option>
            ))}
          </select>
          <p className="field-hint" id="project-environment-hint">
            {env && `写到 ${env.catalog}.${env.schema}，表名以 ${project.table_prefix} 开头。`}
          </p>
          {project.params.length > 0 && (
            <fieldset className="list-editor project-params">
              <legend>运行参数</legend>
              {project.params.map((param) => (
                <ParamInput
                  key={param.key}
                  param={param}
                  value={picked.values[param.key] ?? null}
                  error={errors[param.key]}
                  disabled={disabled}
                  onChange={(value) =>
                    onChange({ ...picked, values: { ...picked.values, [param.key]: value } })
                  }
                />
              ))}
            </fieldset>
          )}
        </>
      )}
    </div>
  );
}
