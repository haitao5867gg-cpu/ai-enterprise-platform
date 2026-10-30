import "server-only";
import type { FlowConfig, FlowNode } from "../apps";
import type {
  DatabricksDevRunOutput,
  EtlCodeCheckOutput,
  EtlPlanCheckOutput,
  TablePreview,
} from "../data-nodes";
import { allMockSources, mockToolService } from "./mock-datasources";

// A node that fails the way the backend reports it, with its error type (input_error
// and the like); anything else a simulated node throws counts as a tool error.
export class MockNodeFailure extends Error {
  readonly type: string;
  constructor(type: string, message: string) {
    super(message);
    this.type = type;
  }
}

export const dataDefaults: FlowNode[] = [
  {
    id: "profile",
    type: "etl_profile",
    title: "探查数据",
    datasource: "",
    tables: "",
    sample_rows: 5,
  },
  {
    id: "check_plan",
    type: "etl_plan_check",
    title: "校验方案",
    reply: "",
    profile: "",
    hits: [],
    language: "",
    max_attempts: 3,
  },
  {
    id: "ingest",
    type: "databricks_ingest",
    title: "抽取到湖仓",
    datasource: "",
    tables: "",
    max_rows: 200000,
  },
  {
    id: "check_code",
    type: "etl_code_check",
    title: "检查并打包",
    reply: "",
    plan: "",
    source_name: "",
    name: "",
    max_attempts: 3,
  },
  {
    id: "dev_run",
    type: "databricks_dev_run",
    title: "dev 运行",
    bundle: "",
    max_runs: 3,
    preview_rows: 20,
  },
];
export const dataDescriptions: Record<string, string> = {
  etl_profile: "读取源表结构、统计和少量样本，为 ETL 方案提供依据。",
  etl_plan_check:
    "校验模型生成的方案，通过后交给人工确认；有问题时让模型重写。",
  databricks_ingest: "从只读数据源抽取表数据，写入 Databricks 湖仓的源表。",
  etl_code_check: "检查模型生成的转换代码，并打包为 Databricks bundle。",
  databricks_dev_run: "在 Databricks dev 运行 bundle，检查质量并预览结果数据。",
};

export function lakehouseConfig(ready = false): FlowConfig {
  return {
    schema_version: 1,
    env: { vars: [], secrets: [] },
    graph: {
      nodes: [
        {
          id: "start",
          type: "start",
          title: "开始",
          note: "选择数据源，填写需求、表范围和代码语言。",
          inputs: [
            {
              key: "datasource",
              label: "数据源",
              type: "datasource",
              required: true,
              default: "sample-postgres",
            },
            {
              key: "request",
              label: "需求",
              type: "textarea",
              required: true,
              default: "按城市和月份统计有效订单销售额",
            },
            {
              key: "tables",
              label: "表范围",
              type: "text",
              default: "retail.*",
            },
            {
              key: "language",
              label: "代码语言",
              type: "select",
              default: "pyspark",
              choices: [
                { value: "pyspark", label: "PySpark" },
                { value: "sql", label: "SQL" },
              ],
            },
            { key: "name", label: "bundle 名", type: "text" },
          ],
        },
        {
          id: "profile",
          type: "etl_profile",
          title: "探查数据",
          note: "读取表结构和样本，先了解真实数据中的空值和异常取值。",
          datasource: "{{start.datasource}}",
          tables: "{{start.tables}}",
          sample_rows: 5,
        },
        {
          id: "glossary",
          type: "knowledge",
          title: "查业务口径",
          note: "检索指标定义和有效订单口径，为方案标注资料依据。",
          knowledge_bases: ready ? ["retail-metrics"] : [],
          query: "{{start.request}}",
          top_k: 6,
          min_score: 0.3,
          retrieval: { mode: "hybrid", vector_weight: 0.7, rerank: null },
        },
        {
          id: "plan",
          type: "llm",
          title: "出方案",
          note: "结合需求、源表和公司数据口径生成可确认的 ETL 方案。",
          model: "mock",
          skills: [{ name: "etl-plan", version: 1 }],
          max_visits: 10,
          output: { kind: "text" },
          quiet: true,
          system:
            "输出 JSON，包含 summary、language、targets、assumptions；目标表包含列映射、加载方式、主键和质量规则。",
          prompt:
            "需求：{{start.request}}\n期望的代码语言：{{start.language}}\n源表（只能使用这些）：\n{{profile.description}}\n公司数据口径（参考资料）：\n{{glossary.context}}\n用 [n] 标注资料依据；冲突时以需求为准并写进 assumptions。\n上一版方案：{{check_plan.plan}}\n平台校验发现的问题：{{check_plan.problems}}\n用户的修改意见：{{review.comment}}\n输出完整方案。",
        },
        {
          id: "check_plan",
          type: "etl_plan_check",
          title: "校验方案",
          note: "检查源表、取值和方案格式；不通过时将问题交给模型重写。",
          reply: "{{plan.text}}",
          profile: "{{profile.tables}}",
          hits: ["{{glossary.hits}}"],
          language: "{{start.language}}",
          max_attempts: 3,
          max_visits: 10,
        },
        {
          id: "review",
          type: "human",
          title: "确认方案",
          note: "确认目标表、指标口径和假设后才开始抽取；驳回时填写修改意见。",
          mode: "approval",
          prompt: "确认这份 ETL 方案吗？",
          show: "{{check_plan.message}}",
          options: [
            { key: "approve", label: "确认" },
            { key: "reject", label: "驳回" },
          ],
          comment_required: ["reject"],
          max_visits: 5,
        },
        {
          id: "ingest",
          type: "databricks_ingest",
          title: "抽取到湖仓",
          note: "按确认的源表列表抽取数据，每张表最多 200000 行。",
          datasource: "{{start.datasource}}",
          tables: "{{check_plan.sources}}",
          max_rows: 200000,
        },
        {
          id: "standards",
          type: "knowledge",
          title: "查开发规范",
          note: "查询分层命名、主键、字段类型、质量规则和 PySpark 开发规范。",
          knowledge_bases: ready ? ["dw-standards"] : [],
          query: "表命名、主键、字段类型、数据质量规则、加载策略、PySpark 写法",
          top_k: 8,
          min_score: 0.3,
          retrieval: { mode: "hybrid", vector_weight: 0.7, rerank: null },
        },
        {
          id: "codegen",
          type: "llm",
          title: "写代码",
          note: "按已确认方案和开发规范生成转换代码，根据校验问题和 dev 报错修改。",
          model: "mock",
          skills: [{ name: "etl-codegen", version: 1 }],
          output: { kind: "text" },
          quiet: true,
          max_visits: 10,
          system: '输出 JSON：{"targets": {"目标表": "完整代码"}}。',
          prompt:
            "已确认的方案：{{check_plan.plan}}\n源表：{{profile.description}}\n开发规范（参考资料）：{{standards.context}}\n上一版代码：{{check_code.code}}\n静态检查发现的问题：{{check_code.problems}}\nDatabricks dev 运行的报错：{{dev_run.errors}}\n按问题和报错修改，输出全部目标表的完整代码。",
        },
        {
          id: "check_code",
          type: "etl_code_check",
          title: "检查并打包",
          note: "静态检查通过后生成 bundle，未通过时返回重写。",
          reply: "{{codegen.text}}",
          plan: "{{check_plan.plan}}",
          source_name: "{{profile.source_name}}",
          name: "{{start.name}}",
          max_attempts: 3,
          max_visits: 10,
        },
        {
          id: "dev_run",
          type: "databricks_dev_run",
          title: "dev 运行",
          note: "在 dev 验证结果行数和质量规则，并展示每张结果表的前几行。",
          bundle: "{{check_code.bundle}}",
          max_runs: 3,
          preview_rows: 20,
          max_visits: 3,
        },
        {
          id: "end",
          type: "end",
          title: "结束",
          note: "返回运行报告、确认的方案和抽取摘要。",
          outputs: [
            { key: "report", from: ["{{dev_run.report}}"] },
            { key: "plan", from: ["{{check_plan.plan}}"] },
            { key: "ingested", from: ["{{ingest.summary}}"] },
          ],
        },
      ],
      edges: [
        { from: "start", to: "profile" },
        { from: "profile", to: "glossary" },
        { from: "glossary", to: "plan" },
        { from: "plan", to: "check_plan" },
        { from: "check_plan", to: "review", branch: "ok" },
        { from: "check_plan", to: "plan", branch: "retry" },
        { from: "review", to: "ingest", branch: "approve" },
        { from: "review", to: "plan", branch: "reject" },
        { from: "ingest", to: "standards" },
        { from: "standards", to: "codegen" },
        { from: "codegen", to: "check_code" },
        { from: "check_code", to: "dev_run", branch: "ok" },
        { from: "check_code", to: "codegen", branch: "retry" },
        { from: "dev_run", to: "end", branch: "passed" },
        { from: "dev_run", to: "codegen", branch: "retry" },
        { from: "dev_run", to: "end", branch: "failed" },
      ],
    },
  };
}

export const demoPreview: TablePreview[] = [
  {
    table: "dws_retail_sales_city_month",
    target: "demo.analytics.dws_retail_sales_city_month",
    columns: ["city", "month", "sales_amount", "order_count"],
    rows: [
      {
        city: "上海",
        month: "2026-09",
        sales_amount: 128560.5,
        order_count: 682,
      },
      { city: "北京", month: "2026-09", sales_amount: 96500, order_count: 510 },
    ],
  },
  {
    table: "dim_retail_customers",
    target: "demo.analytics.dim_retail_customers",
    columns: ["customer_id", "city", "member_level"],
    rows: [
      { customer_id: 1, city: "上海", member_level: "gold" },
      { customer_id: 2, city: "北京", member_level: null },
    ],
  },
];
export const demoReport =
  "## Databricks dev 运行成功\n\n运行方式：Job（已有集群）。按有效订单口径统计 [1]。\n\n### 质量检查\n\n| 结果表 | 行数 | 丢弃行数 | 质量规则 |\n| --- | ---: | ---: | --- |\n| dws_retail_sales_city_month | 96 | 0 | 主键不为空：通过 |\n| dim_retail_customers | 1200 | 0 | 客户主键唯一：通过 |\n\n### 销售额预览\n\n| 城市 | 月份 | 销售额 | 订单数 |\n| --- | --- | ---: | ---: |\n| 上海 | 2026-09 | 128560.50 | 682 |\n| 北京 | 2026-09 | 96500.00 | 510 |\n\n### 客户预览\n\n| 客户编号 | 城市 | 会员等级 |\n| --- | --- | --- |\n| 1 | 上海 | gold |\n| 2 | 北京 | null |";
export function demoDevOutput(previewRows = 20): DatabricksDevRunOutput {
  const tables = demoPreview.map((preview, i) => ({
    name: preview.table,
    target: preview.target,
    rows: i ? 1200 : 96,
    dropped: 0,
    expectations: [
      {
        name: "primary_key_not_null",
        passed_records: i ? 1200 : 96,
        failed_records: 0,
      },
    ],
  }));
  return {
    status: "passed",
    run: {
      status: "passed",
      stage: "run",
      steps: [{ name: "run", ok: true, output: "模拟 dev 运行完成" }],
      mode: "job",
      attempt: 1,
      fixable: false,
      url: null,
      errors: [],
      warnings: [],
      tables: tables.map((table) => ({ ...table, status: "passed" })),
    },
    url: null,
    tables,
    preview: previewRows
      ? demoPreview.map((table) => ({
          ...table,
          rows: table.rows.slice(0, previewRows),
        }))
      : [],
    errors: "",
    report: previewRows
      ? demoReport
      : demoReport.split("\n\n### 销售额预览")[0],
  };
}
export function demoPlanOutput(): EtlPlanCheckOutput {
  return {
    ok: true,
    plan: {
      summary: "按城市和月份汇总有效订单销售额 [1]",
      language: "pyspark",
      assumptions: [],
      targets: demoPreview.map((preview) => ({
        name: preview.table,
        description: "零售分析结果",
        sources: ["retail.orders", "retail.customers"],
        logic: "过滤无效订单并统一城市名称 [1]",
        columns: preview.columns.map((name) => ({
          name,
          source: name,
          description: name,
        })),
        primary_key: [preview.columns[0]],
        load: { mode: "full", key: null },
        quality_rules: [],
      })),
    },
    problems: "",
    message:
      "## ETL 方案\n\n按零售数据口径 [1] 统计销售额。\n\n| 目标表 | 加载方式 |\n| --- | --- |\n| dws_retail_sales_city_month | 全量 |\n| dim_retail_customers | 全量 |",
    sources: ["retail.customers", "retail.orders"],
    targets: demoPreview.map((preview) => preview.table),
  };
}
export function simulateDataNode(
  node: FlowNode,
  rendered: Record<string, unknown>,
) {
  if (node.type === "etl_profile" || node.type === "databricks_ingest") {
    // Like the backend: a tool service picked at run time cannot be read from.
    const service = mockToolService(rendered.datasource);
    if (service)
      throw new MockNodeFailure(
        "input_error",
        `「${service.name}」是工具服务，不能用来探查或抽取数据，请选数据库或上传文件的数据源`,
      );
  }
  const sourceName =
    allMockSources().find(
      (source) => source.datasource_id === rendered.datasource,
    )?.name ?? "示例库";
  if (node.type === "etl_profile")
    return {
      outputs: {
        source_id: String(rendered.datasource),
        source_name: sourceName,
        tables: ["retail.customers", "retail.orders"].map((name) => ({
          name,
          columns: [{ name: "id", data_type: "bigint" }],
          sample: [{ id: 1 }],
        })),
        names: ["retail.customers", "retail.orders"],
        description: "retail.customers：客户；retail.orders：订单。",
        summary: `已探查 ${sourceName} 的 2 张表：retail.customers、retail.orders`,
      },
      branch: null,
    };
  if (node.type === "etl_plan_check")
    return { outputs: { ...demoPlanOutput() }, branch: "ok" };
  if (node.type === "databricks_ingest")
    return {
      outputs: {
        catalog: "demo_source",
        tables: [
          {
            source: "retail.orders",
            target: "demo_source.retail.orders",
            rows: 15000,
            columns: 7,
          },
        ],
        summary:
          "已把 1 张表共 15000 行抽取到 Databricks：`demo_source.retail.orders`（15000 行）",
      },
      branch: null,
    };
  if (node.type === "etl_code_check")
    return {
      outputs: {
        ok: true,
        code: { dws_retail_sales_city_month: "# mock PySpark transformation" },
        problems: "",
        bundle: {
          name: String(rendered.name || "retail_sales"),
          language: "pyspark",
          files: {
            "databricks.yml": "bundle:\n  name: retail_sales",
            "src/transform.py": "# mock PySpark transformation",
          },
        },
        summary: "已生成 Databricks bundle retail_sales",
      },
      branch: "ok",
    };
  if (node.type === "databricks_dev_run")
    return {
      outputs: { ...demoDevOutput(node.preview_rows) },
      branch: "passed",
    };
  return null;
}

// What a data node says in the run process, worded as the backend does
// (docs/api/flow-apps.md, "运行"). The mock's checks always pass.
export function dataNodeSaid(
  node: FlowNode,
  outputs: Record<string, unknown>,
  branch: string | null,
): string | null {
  if (node.type === "etl_profile" || node.type === "databricks_ingest")
    return typeof outputs.summary === "string" ? outputs.summary : null;
  if (node.type === "etl_plan_check" && branch === "ok") {
    const { plan } = outputs as EtlPlanCheckOutput;
    if (!plan || !Array.isArray(plan.targets)) return null;
    const assumptions = Array.isArray(plan.assumptions)
      ? plan.assumptions.length
      : 0;
    return `方案整理好了：${plan.targets.length} 张目标表${assumptions ? `、${assumptions} 条假设待确认` : ""}`;
  }
  if (node.type === "etl_code_check" && branch === "ok") {
    const { bundle } = outputs as EtlCodeCheckOutput;
    return bundle
      ? `代码检查通过，打包成 bundle ${bundle.name}（${Object.keys(bundle.files).length} 个文件）`
      : null;
  }
  if (node.type === "databricks_dev_run") {
    const dev = outputs as DatabricksDevRunOutput;
    if (dev.status === "passed") {
      if (!dev.tables.length) return "dev 运行通过";
      const verb = dev.run.mode === "cluster" ? "得到" : "写出";
      const rows = dev.tables.every((table) => table.rows !== null)
        ? `共 ${dev.tables.reduce((sum, table) => sum + (table.rows ?? 0), 0)} 行`
        : "";
      const dropped = dev.tables.reduce(
        (sum, table) => sum + (table.dropped || 0),
        0,
      );
      return `dev 运行通过：${verb} ${dev.tables.length} 张表${rows}${dropped ? `，质量规则丢弃 ${dropped} 行` : ""}`;
    }
    const errors = dev.errors.split("\n").filter(Boolean).length;
    const head = errors ? `dev 运行失败（${errors} 条报错）` : "dev 运行失败";
    return branch === "retry"
      ? `${head}，让模型改代码（第 ${dev.run.attempt + 1}/${node.max_runs ?? 3} 次运行）`
      : `${head}，不再重试`;
  }
  return null;
}
