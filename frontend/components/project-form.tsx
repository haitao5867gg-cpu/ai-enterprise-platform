"use client";

import { useId, useState } from "react";
import {
  KEY,
  PREFIX,
  PROJECT_ID,
  TABLE,
  THREE_PART,
  createProject,
  filterProblem,
  paramProblem,
  paramTypeLabels,
  replaceProject,
  type Choice,
  type Environment,
  type Param,
  type ParamType,
  type ParamValue,
  type Project,
  type ProjectInput,
  type ProjectOptions,
  type Reference,
} from "../lib/projects";
import { ApiError, errorMessage, type SampleLevel } from "../lib/runs";
import { useUnsavedWarning } from "./asset-ui";
import { CHECKS_HELP, ChecksField } from "./checks-field";
import {
  CheckField,
  FieldIssues,
  ListEditor,
  SelectField,
  TextField,
} from "./flow-fields";
import { ErrorNotice } from "./ui";

type Issues = Record<string, string[]>;

const PATTERN_HELP =
  "正则表达式，整个值都要符合，比如 FY\\d{2}_\\d{2}_M。新建运行时填的值不符合就不能开始。";
const KEY_RULE = "小写字母开头，只用小写字母、数字和下划线，最多 32 个字符";

function add(issues: Issues, where: string, message: string) {
  (issues[where] ??= []).push(message);
}
function twice(values: string[]) {
  return [...new Set(values.filter((value, i) => value && values.indexOf(value) !== i))];
}
const lines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

// What the form would send (strings trimmed, empty optional fields as null).
function payload(input: ProjectInput, adopted: string): ProjectInput {
  const orNull = (value: string | null) => (value?.trim() ? value.trim() : null);
  return {
    project_id: input.project_id.trim(),
    name: input.name.trim(),
    description: input.description,
    table_prefix: input.table_prefix.trim(),
    environments: input.environments.map((env) => ({
      key: env.key.trim(),
      label: env.label.trim(),
      catalog: env.catalog.trim(),
      schema: env.schema.trim(),
      cluster_id: orNull(env.cluster_id),
    })),
    references: input.references.map((ref) => ({
      name: ref.name.trim(),
      label: orNull(ref.label),
      filter: orNull(ref.filter),
      description: ref.description,
    })),
    adopted: lines(adopted),
    params: input.params.map((param) => ({
      key: param.key.trim(),
      label: param.label.trim(),
      type: param.type,
      required: param.required,
      default: param.default === "" ? null : param.default,
      choices:
        param.type === "select"
          ? (param.choices ?? []).map((choice) => ({
              value: choice.value.trim(),
              label: orNull(choice.label),
            }))
          : null,
      pattern: param.type === "text" && param.pattern ? param.pattern : null,
      description: param.description,
    })),
    checks: input.checks,
    policy: {
      samples: input.policy.samples,
      models: input.policy.models?.length ? input.policy.models : null,
    },
  };
}

// The same rules the backend applies, so most mistakes show before saving.
function check(body: ProjectInput): Issues {
  const issues: Issues = {};
  if (!PROJECT_ID.test(body.project_id))
    add(issues, "project_id", "2–40 个字符：小写字母开头，只用小写字母、数字和 -");
  else if (body.project_id === "new") add(issues, "project_id", "项目标识不能是 new");
  if (!body.name) add(issues, "name", "请填写名称");
  if (!PREFIX.test(body.table_prefix))
    add(issues, "table_prefix", "2–30 个字符：小写字母开头，只用小写字母、数字和下划线");
  const envKeys = twice(body.environments.map((env) => env.key));
  body.environments.forEach((env, i) => {
    const at = `env.${i}`;
    if (!KEY.test(env.key)) add(issues, at, `标识要${KEY_RULE}`);
    else if (envKeys.includes(env.key)) add(issues, at, `环境 ${env.key} 重复`);
    if (!env.label) add(issues, at, "请填写显示名");
    if (!env.catalog) add(issues, at, "请填写 catalog");
    if (!env.schema) add(issues, at, "请填写 schema");
  });
  const refNames = twice(body.references.map((ref) => ref.name));
  body.references.forEach((ref, i) => {
    const at = `ref.${i}`;
    if (!THREE_PART.test(ref.name))
      add(issues, at, "表名要写成 catalog.schema.table 三段，每段不含空白和反引号");
    else if (refNames.includes(ref.name)) add(issues, at, `引用表 ${ref.name} 重复`);
    const problem = ref.filter && filterProblem(ref.filter);
    if (problem) add(issues, at, problem);
  });
  const adoptedTwice = twice(body.adopted);
  for (const table of body.adopted) {
    if (!TABLE.test(table))
      add(issues, "adopted", `表名 ${table} 只能用小写字母、数字和下划线，不含 catalog、schema`);
    else if (body.table_prefix && !table.startsWith(body.table_prefix))
      add(issues, "adopted", `${table} 要以表名前缀 ${body.table_prefix} 开头`);
  }
  if (adoptedTwice.length) add(issues, "adopted", `${adoptedTwice.join("、")} 重复`);
  if (body.checks.length > 50) add(issues, "checks", "最多 50 条验收检查");
  const checkIds = body.checks.map((item) => (typeof item.id === "string" ? item.id : ""));
  if (checkIds.includes("golden"))
    add(issues, "checks", "验收检查的 id 不能用 golden，它留给数据检查节点和老系统的对比");
  const checksTwice = twice(checkIds);
  if (checksTwice.length) add(issues, "checks", `验收检查 id ${checksTwice.join("、")} 重复`);
  const paramKeys = twice(body.params.map((param) => param.key));
  body.params.forEach((param, i) => {
    const at = `param.${i}`;
    if (!KEY.test(param.key)) add(issues, at, `标识要${KEY_RULE}`);
    else if (paramKeys.includes(param.key)) add(issues, at, `参数 ${param.key} 重复`);
    if (!param.label) add(issues, at, "请填写显示名");
    if (param.type === "select") {
      const values = (param.choices ?? []).map((choice) => choice.value);
      if (!values.length) add(issues, at, "选项类型要列出至少 1 个选项");
      if (values.some((value) => !value)) add(issues, at, "请填写每个选项的值");
      if (twice(values).length) add(issues, at, "选项的值重复");
    }
    if (param.label && param.default !== null) {
      const problem = paramProblem(param, param.default);
      if (problem) add(issues, at, `默认值不对：${problem}`);
    }
  });
  return issues;
}

// The default-value control for one parameter type; empty means no default.
function DefaultField({
  param,
  onChange,
}: {
  param: Param;
  onChange: (value: ParamValue) => void;
}) {
  const id = useId();
  const value = param.default;
  if (param.type === "boolean" || param.type === "select")
    return (
      <SelectField
        label="默认值"
        value={value === null ? "" : String(value)}
        choices={[
          { value: "", label: "不设默认值" },
          ...(param.type === "boolean"
            ? [
                { value: "true", label: "是" },
                { value: "false", label: "否" },
              ]
            : (param.choices ?? [])
                .filter((choice) => choice.value.trim())
                .map((choice) => ({
                  value: choice.value.trim(),
                  label: choice.label?.trim() || choice.value.trim(),
                }))),
        ]}
        onChange={(next) =>
          onChange(
            next === "" ? null : param.type === "boolean" ? next === "true" : next,
          )
        }
      />
    );
  const type =
    param.type === "number"
      ? "number"
      : param.type === "date"
        ? "date"
        : param.type === "month"
          ? "month"
          : "text";
  return (
    <div className="asset-field">
      <label htmlFor={id}>
        默认值<span className="muted">（选填）</span>
      </label>
      <input
        id={id}
        type={type}
        step={type === "number" ? "any" : undefined}
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
    </div>
  );
}

type Props = {
  initial: ProjectInput;
  options: ProjectOptions | null;
  onSaved: (project: Project) => void;
  onCancel?: () => void;
} & ({ mode: "create" } | { mode: "edit" });

export function ProjectForm({ initial, options, onSaved, onCancel, mode }: Props) {
  const prefix = useId();
  const [input, setInput] = useState<ProjectInput>(initial);
  const [adopted, setAdopted] = useState(initial.adopted.join("\n"));
  const [shown, setShown] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const body = payload(input, adopted);
  const issues = check(body);
  const blocking = Object.keys(issues).length > 0;
  const dirty =
    JSON.stringify(body) !== JSON.stringify(payload(initial, initial.adopted.join("\n")));
  useUnsavedWarning(dirty && !saving);
  const at = (where: string) => (shown ? (issues[where] ?? []) : []);
  const set = <K extends keyof ProjectInput>(key: K, value: ProjectInput[K]) => {
    setInput((old) => ({ ...old, [key]: value }));
    setError("");
  };

  async function save() {
    if (saving) return;
    setShown(true);
    if (blocking) {
      setError("还有没填好的地方，请按红字修改后再保存。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      onSaved(mode === "create" ? await createProject(body) : await replaceProject(body));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : errorMessage(err));
      setSaving(false);
    }
  }

  const levels = options?.sample_levels ?? [];
  const defaults = options?.defaults;
  const models = input.policy.models ?? [];
  const known = options?.models ?? null;
  const ids = { adopted: `${prefix}-adopted`, models: `${prefix}-models` };
  return (
    <form
      className="asset-form project-form"
      noValidate
      aria-label={mode === "create" ? "新建项目" : "编辑项目"}
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <fieldset className="panel asset-section project-form-section" disabled={saving}>
        <legend>基本信息</legend>
        {mode === "create" ? (
          <TextField
            label="项目标识"
            value={input.project_id}
            maxLength={40}
            issues={at("project_id")}
            help="建好后不能改。小写字母开头，只用小写字母、数字和 -，例如 sales-forecast。"
            onChange={(value) => set("project_id", value)}
          />
        ) : (
          <div className="asset-field">
            <span>项目标识</span>
            <strong className="monospace">{input.project_id}</strong>
            <small className="muted">建好后不能改。</small>
          </div>
        )}
        <TextField
          label="名称"
          value={input.name}
          maxLength={100}
          issues={at("name")}
          onChange={(value) => set("name", value)}
        />
        <div className="asset-field">
          <label htmlFor={`${prefix}-description`}>
            说明<span className="muted">（选填）</span>
          </label>
          <textarea
            id={`${prefix}-description`}
            rows={3}
            maxLength={2000}
            value={input.description}
            placeholder="这个项目做什么、给谁用"
            onChange={(event) => set("description", event.target.value)}
          />
        </div>
        <TextField
          label="表名前缀"
          value={input.table_prefix}
          maxLength={30}
          issues={at("table_prefix")}
          help="项目写的每张表都以它开头，例如 sf_。"
          onChange={(value) => set("table_prefix", value)}
        />
      </fieldset>

      <div className="panel asset-section project-form-section">
        <ListEditor<Environment>
          label="环境"
          items={input.environments}
          min={1}
          max={10}
          disabled={saving}
          issues={(i) => at(`env.${i}`)}
          make={() => ({ key: "", label: "", catalog: "", schema: "", cluster_id: null })}
          onChange={(environments) => set("environments", environments)}
          render={(env, i, update) => (
            <>
              <TextField
                label="标识"
                value={env.key}
                maxLength={32}
                onChange={(key) => update({ ...env, key })}
              />
              <TextField
                label="显示名"
                value={env.label}
                maxLength={50}
                onChange={(label) => update({ ...env, label })}
              />
              <TextField
                label="catalog"
                value={env.catalog}
                maxLength={255}
                onChange={(catalog) => update({ ...env, catalog })}
              />
              <TextField
                label="schema"
                value={env.schema}
                maxLength={255}
                onChange={(schema) => update({ ...env, schema })}
              />
              <TextField
                label="集群（选填）"
                value={env.cluster_id ?? ""}
                maxLength={200}
                help={i === 0 ? "默认环境。不填集群时用平台配置的集群。" : "不填时用平台配置的集群。"}
                onChange={(cluster_id) => update({ ...env, cluster_id })}
              />
            </>
          )}
        />
        <p className="field-hint">
          运行写表的位置。第一个是默认环境，新建运行时可以换。
        </p>
      </div>

      <div className="panel asset-section project-form-section">
        <ListEditor<Reference>
          label="引用表"
          items={input.references}
          max={100}
          disabled={saving}
          issues={(i) => at(`ref.${i}`)}
          make={() => ({ name: "", label: null, filter: null, description: "" })}
          onChange={(references) => set("references", references)}
          render={(ref, _i, update) => (
            <>
              <TextField
                label="表名（catalog.schema.table）"
                value={ref.name}
                maxLength={800}
                onChange={(name) => update({ ...ref, name })}
              />
              <TextField
                label="名称（选填）"
                value={ref.label ?? ""}
                maxLength={50}
                onChange={(label) => update({ ...ref, label })}
              />
              <TextField
                label="读取筛选（选填）"
                value={ref.filter ?? ""}
                maxLength={2000}
                help="一个 Spark SQL 条件，比如 Type = 'Vol.'。平台读这张表时一律带上它。"
                onChange={(filter) => update({ ...ref, filter })}
              />
              <TextField
                label="说明（选填）"
                value={ref.description}
                maxLength={2000}
                onChange={(description) => update({ ...ref, description })}
              />
            </>
          )}
        />
        <p className="field-hint">项目只读的表，可以跨 catalog。</p>
        <div className="asset-field">
          <label htmlFor={ids.adopted}>
            归本项目的表<span className="muted">（选填，每行一个表名）</span>
          </label>
          <textarea
            id={ids.adopted}
            rows={3}
            spellCheck={false}
            value={adopted}
            disabled={saving}
            aria-invalid={at("adopted").length > 0}
            aria-describedby={`${ids.adopted}-hint ${ids.adopted}-issues`}
            onChange={(event) => {
              setAdopted(event.target.value);
              setError("");
            }}
          />
          <small className="muted" id={`${ids.adopted}-hint`}>
            在平台之外建好、归本项目所有的表，只写表名（不含 catalog、schema），要以表名前缀开头。平台自己建的表不用登记。
          </small>
          <FieldIssues id={`${ids.adopted}-issues`} messages={at("adopted")} />
        </div>
      </div>

      <div className="panel asset-section project-form-section">
        <ListEditor<Param>
          label="运行参数"
          items={input.params}
          max={20}
          disabled={saving}
          issues={(i) => at(`param.${i}`)}
          make={() => ({
            key: "",
            label: "",
            type: "text",
            required: false,
            default: null,
            choices: null,
            pattern: null,
            description: "",
          })}
          onChange={(params) => set("params", params)}
          render={(param, _i, update) => (
            <>
              <TextField
                label="标识"
                value={param.key}
                maxLength={32}
                onChange={(key) => update({ ...param, key })}
              />
              <TextField
                label="显示名"
                value={param.label}
                maxLength={50}
                onChange={(label) => update({ ...param, label })}
              />
              <SelectField
                label="类型"
                value={param.type}
                choices={Object.entries(paramTypeLabels).map(([value, label]) => ({
                  value,
                  label,
                }))}
                onChange={(type) =>
                  update({
                    ...param,
                    type: type as ParamType,
                    default: null,
                    choices: type === "select" ? (param.choices ?? [{ value: "", label: null }]) : null,
                    pattern: type === "text" ? (param.pattern ?? null) : null,
                  })
                }
              />
              {param.type === "text" && (
                <TextField
                  label="格式（选填）"
                  value={param.pattern ?? ""}
                  maxLength={200}
                  help={PATTERN_HELP}
                  onChange={(pattern) => update({ ...param, pattern: pattern || null })}
                />
              )}
              <DefaultField
                param={param}
                onChange={(value) => update({ ...param, default: value })}
              />
              <CheckField
                label="必填"
                checked={param.required}
                onChange={(required) => update({ ...param, required })}
              />
              {param.type === "select" && (
                <ListEditor<Choice>
                  label="选项"
                  items={param.choices ?? []}
                  min={1}
                  max={50}
                  make={() => ({ value: "", label: null })}
                  onChange={(choices) => update({ ...param, choices })}
                  render={(choice, _j, change) => (
                    <>
                      <TextField
                        label="值"
                        value={choice.value}
                        maxLength={200}
                        onChange={(value) => change({ ...choice, value })}
                      />
                      <TextField
                        label="显示名（选填）"
                        value={choice.label ?? ""}
                        maxLength={50}
                        onChange={(label) => change({ ...choice, label })}
                      />
                    </>
                  )}
                />
              )}
              <TextField
                label="说明（选填）"
                value={param.description}
                maxLength={500}
                onChange={(description) => update({ ...param, description })}
              />
            </>
          )}
        />
        <p className="field-hint">
          新建运行时填值，所有入口的默认值都取这里。节点里用 {"{{param.标识}}"}
          引用，生成的代码用 param(&quot;标识&quot;) 读。
        </p>
      </div>

      <fieldset className="panel asset-section project-form-section" disabled={saving}>
        <legend>验收检查</legend>
        <p className="field-hint">
          项目自己的验收标准，绑定这个项目的运行在数据检查时都会跑（模板里引用{" "}
          {"{{project.checks}}"}）。不带 catalog、schema 的表名是这次运行写的表，要以表名前缀开头。
        </p>
        <ChecksField
          label="检查（JSON）"
          value={input.checks}
          issues={at("checks")}
          help={`最多 50 条，id 不能用 golden。${CHECKS_HELP}`}
          onChange={(checks) => set("checks", checks)}
        />
      </fieldset>

      <fieldset className="panel asset-section project-form-section" disabled={saving}>
        <legend>数据外发策略</legend>
        <p className="field-hint">
          管从数据源读出来的值以什么程度发给模型，以及能用哪些模型。不设的项用平台默认。
        </p>
        <SelectField
          label="发给模型的数据"
          value={input.policy.samples ?? ""}
          choices={[
            {
              value: "",
              label: `平台默认${defaults ? `（${levels.find((level) => level.value === defaults.samples)?.label ?? defaults.samples}）` : ""}`,
            },
            ...levels.map((level) => ({ value: level.value, label: level.label })),
          ]}
          help={
            levels.find((level) => level.value === (input.policy.samples ?? defaults?.samples))
              ?.description
          }
          onChange={(value) =>
            set("policy", {
              ...input.policy,
              samples: value ? (value as SampleLevel) : null,
            })
          }
        />
        <fieldset className="list-editor" aria-describedby={`${ids.models}-hint`}>
          <legend>允许的模型</legend>
          {known === null ? (
            <TextField
              label="模型别名（用逗号分开）"
              value={models.join(", ")}
              onChange={(value) =>
                set("policy", {
                  ...input.policy,
                  models: value
                    .split(/[,，]/)
                    .map((item) => item.trim())
                    .filter(Boolean),
                })
              }
            />
          ) : (
            <div className="check-grid">
              {[...new Set([...known, ...models])].map((model) => (
                <CheckField
                  key={model}
                  label={model}
                  checked={models.includes(model)}
                  onChange={(checked) =>
                    set("policy", {
                      ...input.policy,
                      models: checked
                        ? [...models, model]
                        : models.filter((item) => item !== model),
                    })
                  }
                />
              ))}
            </div>
          )}
          <small className="muted" id={`${ids.models}-hint`}>
            都不选时用平台默认
            {defaults?.models?.length ? `（只用 ${defaults.models.join("、")}）` : "（不限定）"}
            。选了以后，运行里每个调模型的节点都只能用这些模型。
          </small>
        </fieldset>
      </fieldset>

      {error && <ErrorNotice message={error} />}
      <div className="datasource-save-row">
        <button className="button primary" disabled={saving || (mode === "edit" && !dirty)}>
          {saving ? "正在保存…" : mode === "create" ? "建立项目" : "保存"}
        </button>
        {onCancel && (
          <button type="button" className="button secondary" disabled={saving} onClick={onCancel}>
            取消
          </button>
        )}
        {mode === "edit" && !dirty && <p className="field-hint">还没有修改。</p>}
      </div>
    </form>
  );
}
