import "server-only";
import {
  KEY,
  PREFIX,
  PROJECT_FIELDS,
  PROJECT_ID,
  TABLE,
  THREE_PART,
  filterProblem,
  paramProblem,
  patternRegExp,
  conventionStageLabels,
  scheduledUses,
  type Convention,
  type Environment,
  type Param,
  type ParamValue,
  type Project,
  type ProjectInput,
  type ProjectOptions,
  type Scheduled,
} from "../projects";
import { isRecord, type RunPolicy, type RunProject, type SampleLevel } from "../runs";
import { mockError, mockJson, noContent, readBody } from "./mock-assets";
import { mockModels } from "./mock-flow-apps";

// Mock of docs/api/projects.md. Gaps against the backend: validation messages cover the
// common cases only (a field of the wrong JSON type gets one general sentence), and
// nothing is stored across dev server restarts.
const CREATED = "2026-01-05T08:00:00.000Z";
const LEVELS: SampleLevel[] = ["full", "masked", "stats", "schema"];
const LABELS: Record<SampleLevel, string> = {
  full: "完整样本",
  masked: "脱敏样本",
  stats: "只发统计",
  schema: "只发结构",
};
const DESCRIPTIONS: Record<SampleLevel, string> = {
  full: "样本行和取值分布原样发给模型。",
  masked:
    "样本行和取值分布脱敏后发给模型：文字换成记号，数字只留一位有效数字，日期只留到月。",
  stats: "不发样本行和取值分布，只发空值率、不同值个数和表结构。",
  schema: "只发表结构：列名、类型和主外键。",
};
const LOCAL_MODELS = ["mock-embed", "mock-rerank"];
export const PLATFORM_POLICY: RunPolicy = { samples: "full", models: null };
const globalStore = globalThis as typeof globalThis & {
  aepMockProjects?: Map<string, Project>;
};
const store = (globalStore.aepMockProjects ??= new Map(
  [
    {
      project_id: "sales-forecast",
      name: "销售预测",
      description: "每月按上一期预测和实际销量出本期预测表。",
      table_prefix: "sf_",
      environments: [
        {
          key: "dev",
          label: "开发",
          catalog: "main",
          schema: "sales_dev",
          cluster_id: null,
        },
        {
          key: "prod",
          label: "生产",
          catalog: "main",
          schema: "sales",
          cluster_id: null,
        },
      ],
      references: [
        {
          name: "prev.sales.forecast",
          label: "上一期预测",
          filter: "Type = 'Vol.'",
          description: "上一期的需求预测，只读数量行",
        },
      ],
      adopted: ["sf_forecast_history"],
      params: [
        {
          key: "update_month",
          label: "统计月",
          type: "month",
          required: true,
          default: null,
          choices: null,
          description: "按自然月",
        },
        {
          key: "version",
          label: "版本号",
          type: "number",
          required: false,
          default: 1,
          choices: null,
          description: "",
        },
      ],
      checks: [
        {
          id: "rows_this_month",
          label: "本月有结果",
          kind: "aggregate",
          table: "sf_forecast_history",
          expr: "count(*)",
          where: "update_month = ${param.update_month}",
          op: "gt",
          value: 0,
        },
      ],
      conventions: [
        {
          key: "by_version",
          title: "读预测表的地方都先按版本筛选",
          text: "上一期预测表里有多个版本的数据，不筛选会重复计算。",
          applies_to: "code",
        },
      ],
      policy: { samples: "stats", models: null },
      created_at: CREATED,
      updated_at: CREATED,
    } satisfies Project,
  ].map((project): [string, Project] => [project.project_id, project]),
));

const byName = (a: Project, b: Project) =>
  a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
const summary = (project: Project) => ({
  project_id: project.project_id,
  name: project.name,
  description: project.description,
  environments: project.environments.map(({ key, label }) => ({ key, label })),
  updated_at: project.updated_at,
});
export const mockProject = (id: string) => store.get(id);

function text(value: unknown, low: number, high: number) {
  return typeof value === "string" && value.trim().length >= low && value.trim().length <= high
    ? value.trim()
    : null;
}
function repeated(values: string[]) {
  return [...new Set(values.filter((value, i) => values.indexOf(value) !== i))];
}

// The body as stored, or why it is not accepted.
function readProject(body: Record<string, unknown>): ProjectInput | string {
  const allowed = [
    "project_id",
    "name",
    "description",
    "table_prefix",
    "environments",
    "references",
    "adopted",
    "params",
    "checks",
    "conventions",
    "policy",
  ];
  const extra = Object.keys(body).find((key) => !allowed.includes(key));
  if (extra) return `项目不接受「${extra}」这个设置`;
  if (typeof body.project_id !== "string" || !PROJECT_ID.test(body.project_id))
    return "「项目标识」的格式、长度或范围不正确";
  if (body.project_id === "new") return "项目标识不能是 new，页面地址 /projects/new 用了它";
  const name = text(body.name, 1, 100);
  if (!name) return "「名称」的格式、长度或范围不正确";
  const description = body.description ?? "";
  if (typeof description !== "string" || description.length > 2000)
    return "「说明」的格式、长度或范围不正确";
  if (typeof body.table_prefix !== "string" || !PREFIX.test(body.table_prefix))
    return "「表名前缀」的格式、长度或范围不正确";
  const prefix = body.table_prefix;
  if (
    !Array.isArray(body.environments) ||
    body.environments.length < 1 ||
    body.environments.length > 10
  )
    return "「环境」的格式、长度或范围不正确";
  const environments: Environment[] = [];
  for (const [i, env] of body.environments.entries()) {
    const at = `环境的第 ${i + 1} 项`;
    if (!isRecord(env)) return `「${at}」的格式、长度或范围不正确`;
    if (typeof env.key !== "string" || !KEY.test(env.key))
      return `「${at}的标识」的格式、长度或范围不正确`;
    const label = text(env.label, 1, 50);
    const catalog = text(env.catalog, 1, 255);
    const schema = text(env.schema, 1, 255);
    if (!label) return `「${at}的显示名」的格式、长度或范围不正确`;
    if (!catalog) return `「${at}的catalog」的格式、长度或范围不正确`;
    if (!schema) return `「${at}的schema」的格式、长度或范围不正确`;
    const cluster =
      env.cluster_id === null || env.cluster_id === undefined
        ? null
        : text(env.cluster_id, 1, 200);
    if (env.cluster_id && !cluster) return `「${at}的集群」的格式、长度或范围不正确`;
    environments.push({ key: env.key, label, catalog, schema, cluster_id: cluster });
  }
  const references: ProjectInput["references"] = [];
  for (const [i, ref] of (Array.isArray(body.references) ? body.references : []).entries()) {
    if (!isRecord(ref) || typeof ref.name !== "string")
      return `「引用表的第 ${i + 1} 项」的格式、长度或范围不正确`;
    const refName = ref.name.trim();
    if (!THREE_PART.test(refName))
      return `引用表 ${refName} 要写成 catalog.schema.table 三段，每段不含空白和反引号`;
    const filter = ref.filter == null ? null : text(ref.filter, 1, 2000);
    if (ref.filter != null && !filter)
      return `「引用表的第 ${i + 1} 项的读取筛选」的格式、长度或范围不正确`;
    const problem = filter && filterProblem(filter);
    if (problem) return `引用表 ${refName}：${problem}`;
    references.push({
      name: refName,
      label: (ref.label == null ? null : text(ref.label, 1, 50)) ?? refName.split(".")[2].slice(0, 50),
      filter,
      description: typeof ref.description === "string" ? ref.description : "",
    });
  }
  const adopted = (Array.isArray(body.adopted) ? body.adopted : []).map((item) =>
    typeof item === "string" ? item.trim() : "",
  );
  const params: Param[] = [];
  for (const [i, param] of (Array.isArray(body.params) ? body.params : []).entries()) {
    const at = `参数的第 ${i + 1} 项`;
    if (!isRecord(param)) return `「${at}」的格式、长度或范围不正确`;
    if (typeof param.key !== "string" || !KEY.test(param.key))
      return `「${at}的标识」的格式、长度或范围不正确`;
    const label = text(param.label, 1, 50);
    if (!label) return `「${at}的显示名」的格式、长度或范围不正确`;
    const type = param.type as Param["type"];
    if (!["text", "number", "boolean", "date", "month", "select"].includes(type))
      return `「${at}的类型」的取值不对`;
    const choices = Array.isArray(param.choices)
      ? param.choices.map((choice) =>
          isRecord(choice)
            ? { value: String(choice.value ?? "").trim(), label: text(choice.label, 1, 50) }
            : { value: "", label: null },
        )
      : null;
    if (type === "select") {
      if (!choices || choices.length < 1 || choices.length > 50)
        return `参数「${label}」是选项，要列出 1–50 个选项`;
      if (choices.some((choice) => !choice.value))
        return `「${at}的选项」的格式、长度或范围不正确`;
      if (repeated(choices.map((choice) => choice.value)).length)
        return `参数「${label}」的选项值重复`;
    } else if (param.choices != null) return `参数「${label}」不是选项，不能写 choices`;
    let pattern: string | null = null;
    if (param.pattern != null) {
      if (typeof param.pattern !== "string" || !param.pattern || param.pattern.length > 200)
        return `「${at}的格式」的格式、长度或范围不正确`;
      if (type !== "text") return `参数「${label}」不是文本，不能写格式`;
      // The backend reads it as RE2; JavaScript is close enough for the mock.
      if (!patternRegExp(param.pattern)) return `参数「${label}」的格式不是有效的正则表达式`;
      pattern = param.pattern;
    }
    const value = (param.default ?? null) as ParamValue;
    const problem = value !== null && paramProblem({ label, type, choices, pattern }, value);
    if (problem) return `默认值不对：${problem}`;
    // How scheduled runs fill it in: the shape is kept, the backend checks the rest.
    let scheduled: Scheduled | null = null;
    if (param.scheduled != null) {
      const rule = param.scheduled;
      if (!isRecord(rule) || !scheduledUses(type).includes(rule.use as Scheduled["use"]))
        return `参数「${label}」定时运行时的取法不对`;
      const use = rule.use as Scheduled["use"];
      if (use === "date" && type === "text" && typeof rule.format !== "string")
        return `参数「${label}」定时运行时的取法不对：文本参数要写日期格式 format，如 FY%y_%m_M`;
      scheduled = {
        use,
        format: typeof rule.format === "string" ? rule.format : null,
        months: typeof rule.months === "number" ? rule.months : 0,
        day: rule.day === "first" || rule.day === "last" ? rule.day : null,
      };
    }
    params.push({
      key: param.key,
      label,
      type,
      required: param.required === true,
      default: value,
      choices: type === "select" ? choices : null,
      pattern,
      description: typeof param.description === "string" ? param.description : "",
      scheduled,
    });
  }
  const policy = isRecord(body.policy) ? body.policy : {};
  const samples = (policy.samples ?? null) as SampleLevel | null;
  if (samples !== null && !LEVELS.includes(samples)) return "「数据外发策略的样本级别」的取值不对";
  const models = policy.models ?? null;
  if (
    models !== null &&
    (!Array.isArray(models) ||
      models.length < 1 ||
      models.length > 20 ||
      !models.every((model) => typeof model === "string" && model.trim()))
  )
    return "「数据外发策略的允许的模型」的格式、长度或范围不正确";
  for (const [what, keys] of [
    ["环境", environments.map((env) => env.key)],
    ["引用表", references.map((ref) => ref.name)],
    ["参数", params.map((param) => param.key)],
    ["归本项目的表", adopted],
  ] as const) {
    const twice = repeated([...keys]).sort();
    if (twice.length) return `${what} ${twice.join("、")} 重复`;
  }
  for (const table of adopted) {
    if (!TABLE.test(table))
      return `表名 ${table} 只能用小写字母、数字和下划线，不含 catalog、schema`;
    if (!table.startsWith(prefix))
      return `归本项目的表 ${table} 要以表名前缀 ${prefix} 开头`;
  }
  const checks = body.checks ?? [];
  if (
    !Array.isArray(checks) ||
    checks.length > 50 ||
    !checks.every((item) => typeof item === "object" && item !== null && !Array.isArray(item))
  )
    return "「验收检查」的格式、长度或范围不正确";
  const conventions = body.conventions ?? [];
  if (
    !Array.isArray(conventions) ||
    conventions.length > 30 ||
    !conventions.every(
      (item) =>
        isRecord(item) &&
        typeof item.key === "string" &&
        KEY.test(item.key) &&
        text(item.title, 1, 100) !== null &&
        (item.text === undefined || (typeof item.text === "string" && item.text.length <= 2000)) &&
        [undefined, "plan", "code", "both"].includes(item.applies_to as string | undefined),
    )
  )
    return "「约定」的格式、长度或范围不正确";
  const conventionsTwice = repeated(conventions.map((item) => item.key as string)).sort();
  if (conventionsTwice.length) return `约定 ${conventionsTwice.join("、")} 重复`;
  const missing = ((models as string[] | null) ?? []).filter(
    (model) => !mockModels.includes(model) && !LOCAL_MODELS.includes(model),
  );
  if (missing.length) return `模型网关上没有模型 ${missing.join("、")}`;
  return {
    project_id: body.project_id,
    name,
    description,
    table_prefix: prefix,
    environments,
    references,
    adopted,
    params,
    checks: checks as Record<string, unknown>[],
    conventions: conventions.map(
      (item): Convention => ({
        key: item.key as string,
        title: text(item.title, 1, 100)!,
        text: (item.text as string | undefined) ?? "",
        applies_to: (item.applies_to as Convention["applies_to"] | undefined) ?? "both",
      }),
    ),
    policy: { samples, models: models as string[] | null },
  };
}

export async function mockProjects(request: Request, path: string[]) {
  const { method } = request;
  if (path.length === 1) {
    if (method === "GET") return mockJson([...store.values()].sort(byName).map(summary));
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const project = readProject(body);
    if (typeof project === "string") return mockError(project, 422);
    if (store.has(project.project_id))
      return mockError(`项目 ${project.project_id} 已经存在`, 409);
    if ([...store.values()].some((item) => item.name === project.name))
      return mockError(`已经有叫「${project.name}」的项目`, 409);
    const now = new Date().toISOString();
    const made: Project = { ...project, created_at: now, updated_at: now };
    store.set(made.project_id, made);
    return mockJson(made, 201);
  }
  if (path[1] === "options") {
    const options: ProjectOptions = {
      sample_levels: LEVELS.map((value) => ({
        value,
        label: LABELS[value],
        description: DESCRIPTIONS[value],
      })),
      models: [...mockModels],
      defaults: { samples: "full", models: null },
    };
    return mockJson(options);
  }
  const found = store.get(path[1]);
  if (method === "GET")
    return found ? mockJson(found) : mockError(`没有项目 ${path[1]}`, 404);
  if (method === "DELETE") {
    if (!found) return mockError(`没有项目 ${path[1]}`, 404);
    store.delete(path[1]);
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  const project = readProject(body);
  if (typeof project === "string") return mockError(project, 422);
  if (project.project_id !== path[1])
    return mockError("项目标识建好后不能改，要和地址里的一致", 422);
  if (!found) return mockError(`没有项目 ${path[1]}`, 404);
  if ([...store.values()].some((item) => item.name === project.name && item.project_id !== path[1]))
    return mockError(`已经有叫「${project.name}」的项目`, 409);
  const saved: Project = {
    ...project,
    created_at: found.created_at,
    updated_at: new Date().toISOString(),
  };
  store.set(saved.project_id, saved);
  return mockJson(saved);
}

// What a run keeps of its project (the backend's aep.projects.snapshot).
export type ProjectSnapshot = {
  project: Project;
  environment: Environment;
  values: Record<string, ParamValue>;
};

// Bind a new run to the project it names (docs/api/projects.md, "新建运行"), or say why
// not. `params` are the `{{param.*}}` keys the app uses.
export function bindMockProject(
  title: string,
  uses: string | null | undefined,
  choice: unknown,
  params: string[],
): ProjectSnapshot | null | string {
  if (choice == null) return uses === "required" ? `应用「${title}」需要选择一个项目` : null;
  if (!uses) return `应用「${title}」不使用项目`;
  if (!isRecord(choice) || typeof choice.project_id !== "string") return "请选择项目";
  const project = store.get(choice.project_id);
  if (!project) return `没有项目 ${choice.project_id}`;
  const key =
    typeof choice.environment === "string" ? choice.environment : project.environments[0].key;
  const environment = project.environments.find((env) => env.key === key);
  if (!environment) return `项目「${project.name}」没有环境 ${key}`;
  const given = choice.params ?? {};
  if (!isRecord(given)) return "运行参数要是一个对象";
  for (const name of Object.keys(given))
    if (!project.params.some((param) => param.key === name))
      return `项目「${project.name}」没有参数 ${name}`;
  const values: Record<string, ParamValue> = {};
  for (const param of project.params) {
    const value = (given[param.key] ?? param.default ?? null) as ParamValue;
    if (value === null) {
      if (param.required) return `请填写参数「${param.label}」`;
      values[param.key] = null;
      continue;
    }
    const problem = paramProblem(param, value);
    if (problem) return problem;
    values[param.key] = value;
  }
  const unknown = params.find((name) => !(name in values));
  if (unknown) return `应用用到了参数 ${unknown}，项目「${project.name}」没有定义它`;
  return { project: structuredClone(project), environment: { ...environment }, values };
}

export function projectPolicy(bound: ProjectSnapshot | null): RunPolicy {
  const own = bound?.project.policy;
  return {
    samples: own?.samples ?? PLATFORM_POLICY.samples,
    models: own?.models ?? PLATFORM_POLICY.models,
  };
}

export function runProject(bound: ProjectSnapshot | null): RunProject | null {
  if (!bound) return null;
  return {
    project_id: bound.project.project_id,
    name: bound.project.name,
    environment: bound.environment.key,
    environment_label: bound.environment.label,
    params: bound.values,
    param_labels: Object.fromEntries(
      bound.project.params.map((param) => [param.key, param.label]),
    ),
    policy: bound.project.policy,
  };
}

// `{{project.<field>}}` in a run bound to `bound`; null unbound (`brief` is "").
export function projectValues(bound: ProjectSnapshot | null): Record<string, unknown> {
  if (!bound)
    return Object.fromEntries(
      PROJECT_FIELDS.map((field) => [
        field,
        field === "brief" || field === "release_brief"
          ? ""
          : ["conventions", "plan_rules", "code_rules"].includes(field)
            ? []
            : null,
      ]),
    );
  const { project, environment, values } = bound;
  // Releasing (docs/api/projects.md): the mock does not work out scheduled values.
  const canRelease =
    project.environments.some((env) => env.key === "prod") && environment.key !== "prod";
  const conventions = project.conventions ?? [];
  const params = project.params.map((param) => ({
    key: param.key,
    label: param.label,
    type: param.type,
    value: values[param.key],
    description: param.description,
  }));
  const brief = [
    `## 项目：${project.name}`,
    project.description,
    `写到 \`${environment.catalog}.${environment.schema}\`，表名前缀 \`${project.table_prefix}\`；只能写以前缀开头、属于本项目的表。`,
    "（本地模拟：引用表和运行参数的表格略）",
    '生成的代码用 `param("标识")` 读参数，方案和检查里的条件用 `${param.标识}` 写。',
    conventions.length
      ? [
          "### 约定（必须遵守，平台会按它们审查）",
          ...conventions.map(
            (item) =>
              `- **${item.title}**（${conventionStageLabels[item.applies_to]}）${item.text ? `：${item.text}` : ""}`,
          ),
        ].join("\n")
      : "",
  ]
    .filter(Boolean)
    .join("\n\n");
  // A review node's checklist: the conventions for that stage (docs/api/projects.md).
  const rules = (stage: "plan" | "code") =>
    conventions
      .filter((item) => item.applies_to === stage || item.applies_to === "both")
      .map((item) => ({ key: item.key, title: item.title, instruction: item.text }));
  return {
    project_id: project.project_id,
    name: project.name,
    description: project.description,
    table_prefix: project.table_prefix,
    environment: environment.key,
    catalog: environment.catalog,
    schema: environment.schema,
    references: project.references,
    params,
    brief,
    checks: project.checks ?? [],
    conventions,
    plan_rules: rules("plan"),
    code_rules: rules("code"),
    can_release: canRelease,
    release_ready:
      canRelease &&
      project.params.every((param) => param.scheduled || param.default !== null || !param.required),
    release_brief: canRelease
      ? `## 上线说明\n\n- 上线到项目「${project.name}」的 prod 环境。\n\n（本地模拟：定时运行时的参数表略）`
      : "",
  };
}
