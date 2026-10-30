import "server-only";
import { createHash } from "node:crypto";
import type { FlowConfig, FlowNode, HumanNode } from "../apps";
import type { Reference } from "../knowledge";
import {
  answersChanged,
  isRecord,
  type Message,
  type Pending,
  type Question,
  type Run,
  type RunStatus,
} from "../runs";
import { lakehouseConfig } from "./mock-data-nodes";
import type { MockSteps } from "./mock-flow";

// A run of the ETL template's second version in the local mock (docs/api/data-nodes.md,
// "应用模板 etl-lakehouse 第 2 版"): it waits at 确认方案 and 审核试算结果 with what the two
// views read, takes answers and notes on items as the backend does, and ends delivered
// or stopped. What the model and Databricks would give is fixed; a request that says
// 试算失败 has its first dev run fail.
export const ETL_V2_APP = "retail-etl-v2";

type Record_ = Record<string, unknown>;
export type EtlV2State = {
  next: "review" | "data_review" | "delivered" | "stopped";
  rounds: { review: number; data_review: number; dev: number };
  failFirst: boolean;
  confirmed: Record_ | null;
  reviewed: Record_ | null;
  // What people answered so far, by question: answered questions become rules.
  answered: { label: string; value: string; actor: string | null }[];
};
type Entry = {
  run: Run;
  steps: MockSteps;
  readyAt: number;
  v2?: EtlV2State;
};
type Ops = {
  setStatus: (run: Run, status: RunStatus) => void;
  stamp: (
    node: { id: string; title: string },
    message: Message,
    actor?: string | null,
  ) => Message;
};

const reviewNode: HumanNode = {
  id: "review",
  type: "human",
  title: "确认方案",
  note: "确认业务说明、回答问题后才开始抽取；改了答案或写了意见就按意见重写方案。",
  mode: "approval",
  prompt:
    "请检查方案：业务说明（指标、维度、筛选、时间范围）、技术方案和已按口径处理的规则，并回答每道问题。确认后平台把用到的源表抽取到 Databricks；改了建议答案或写了意见，平台会按意见重写方案，再请你确认一次。",
  view: "etl_plan_review",
  show: "{{check_plan.message}}",
  data: {
    brief: "{{check_plan.brief}}",
    plan: "{{check_plan.plan}}",
    answer: "{{check_plan.answer}}",
    uncited: "{{check_plan.uncited}}",
    cleaned: "{{check_plan.cleaned}}",
    returned: "{{data_review.record}}",
    previous: "{{review.record}}",
  },
  fields: "{{check_plan.questions}}",
  changed_branch: "revise",
  options: [
    { key: "approve", label: "确认方案，开始做" },
    { key: "revise", label: "按我的意见修改方案" },
  ],
  comment_required: ["revise"],
  max_visits: 5,
};
const dataReviewNode: HumanNode = {
  id: "data_review",
  type: "human",
  title: "审核试算结果",
  note: "看试算结果、代码和方案；通过后交付，也可以改代码、改方案或终止。",
  mode: "approval",
  prompt:
    "请看开发环境试算的结果、代码和方案。通过后交付；改代码、改方案时写明意见，可以挂在某一列、某条质量规则或某个文件上；终止时写明原因。",
  view: "etl_dev_review",
  show: "{{dev_run.report}}",
  data: {
    brief: "{{check_plan.brief}}",
    plan: "{{check_plan.plan}}",
    uncited: "{{check_plan.uncited}}",
    confirmed: "{{review.record}}",
    status: "{{dev_run.status}}",
    tables: "{{dev_run.tables}}",
    preview: "{{dev_run.preview}}",
    answer: "{{dev_run.answer}}",
    quality: "{{dev_run.quality}}",
    url: "{{dev_run.url}}",
    errors: "{{dev_run.errors}}",
    files: "{{check_code.files}}",
  },
  options: [
    { key: "approve", label: "通过", requires: "{{dev_run.ok}}" },
    { key: "fix_code", label: "改代码" },
    { key: "fix_plan", label: "改方案" },
    { key: "stop", label: "终止" },
  ],
  comment_required: ["fix_code", "fix_plan", "stop"],
  max_visits: 4,
};
const sharedOutputs = [
  { key: "brief", from: ["{{check_plan.brief}}"] },
  { key: "plan", from: ["{{check_plan.plan}}"] },
  { key: "report", from: ["{{dev_run.report}}"] },
  { key: "confirmed", from: ["{{review.record}}"] },
  { key: "reviewed", from: ["{{data_review.record}}"] },
];

export const REQUEST_EXAMPLES = [
  "按城市和会员等级统计上个月的销售",
  "统计每个品类每月的销售额和毛利率，只看线上渠道。",
  "统计每月新客户数和复购率，按城市拆开。",
];

export function etlV2Config(): FlowConfig {
  const base = lakehouseConfig(true);
  const nodes: FlowNode[] = base.graph.nodes.map((node) =>
    node.type === "start"
      ? {
          ...node,
          // As in template v2 after make demo: the optional inputs are folded away, and
          // the need has sentences to start from.
          inputs: node.inputs.map((input) =>
            input.key === "request"
              ? { ...input, examples: REQUEST_EXAMPLES }
              : input.key === "datasource"
                ? input
                : { ...input, advanced: true },
          ),
        }
      : node.id === "review"
        ? reviewNode
        : node.id === "dev_run"
          ? { ...node, title: "开发环境试算", max_visits: 9 }
          : node.id === "end"
            ? {
                id: "end",
                type: "end",
                title: "交付",
                outputs: [
                  ...sharedOutputs,
                  { key: "ingested", from: ["{{ingest.summary}}"] },
                  { key: "answer", from: ["{{dev_run.answer}}"] },
                  { key: "code", from: ["{{check_code.bundle}}"] },
                  { key: "quality", from: ["{{dev_run.quality}}"] },
                  { key: "dev_url", from: ["{{dev_run.url}}"] },
                ],
              }
            : node,
  );
  nodes.push(dataReviewNode, {
    id: "stopped",
    type: "end",
    title: "终止",
    outputs: [
      {
        key: "reason",
        from: ["{{data_review.comment}}", "{{data_review.feedback}}"],
      },
      ...sharedOutputs,
    ],
  });
  return {
    ...base,
    graph: {
      nodes,
      edges: [
        { from: "start", to: "profile" },
        { from: "profile", to: "glossary" },
        { from: "glossary", to: "standards" },
        { from: "standards", to: "plan" },
        { from: "plan", to: "check_plan" },
        { from: "check_plan", to: "review", branch: "ok" },
        { from: "check_plan", to: "plan", branch: "retry" },
        { from: "review", to: "ingest", branch: "approve" },
        { from: "review", to: "plan", branch: "revise" },
        { from: "ingest", to: "codegen" },
        { from: "codegen", to: "check_code" },
        { from: "check_code", to: "dev_run", branch: "ok" },
        { from: "check_code", to: "codegen", branch: "retry" },
        { from: "dev_run", to: "data_review", branch: "passed" },
        { from: "dev_run", to: "data_review", branch: "failed" },
        { from: "dev_run", to: "codegen", branch: "retry" },
        { from: "data_review", to: "end", branch: "approve" },
        { from: "data_review", to: "codegen", branch: "fix_code" },
        { from: "data_review", to: "plan", branch: "fix_plan" },
        { from: "data_review", to: "stopped", branch: "stop" },
      ],
    },
  };
}

function reference(
  index: number,
  document_name: string,
  heading: string,
  text: string,
): Reference {
  return {
    index,
    kb_id: index < 4 ? "retail-metrics" : "dw-standards",
    kb_name: index < 4 ? "零售指标口径" : "数仓开发规范",
    document_id: `doc-${index}`,
    document_name,
    chunk_id: `chunk-${index}`,
    position: index,
    heading,
    page: null,
    text,
    score: 0.82 - index * 0.04,
    scores: { vector: 0.8, keyword: 0.6, rerank: null },
  };
}
const references: Reference[] = [
  reference(
    1,
    "有效订单口径.md",
    "有效订单",
    "已支付、已发货或已完成的订单，排除测试账号。",
  ),
  reference(
    2,
    "销售指标定义.md",
    "销售额与客单价",
    "销售额 = 数量 × 成交单价 × (1 − 折扣)，不含运费；客单价 = 销售额 ÷ 订单数。",
  ),
  reference(
    3,
    "客户维度.md",
    "城市",
    "城市取客户档案里的城市，去掉末尾的“市”，缺失时记为“未知”。",
  ),
  reference(
    4,
    "数仓分层规范.md",
    "统计月",
    "按月统计的表用 stat_month，写成 yyyy-MM 的文字。",
  ),
];

const brief = {
  goal: "按月、按城市、按会员等级统计有效订单的 4 个指标",
  grain: "每个统计月 × 城市 × 会员等级一行",
  metrics: [
    {
      id: "m1",
      name: "销售额",
      unit: "元",
      definition:
        "有效订单里每行商品的成交金额之和：数量 × 成交单价 × (1 − 折扣)，不含运费 [2]",
      column: "sales_amount",
    },
    {
      id: "m2",
      name: "订单数",
      unit: null,
      definition: "有效订单的单数 [1]",
      column: "order_count",
    },
    {
      id: "m3",
      name: "下单客户数",
      unit: null,
      definition: "这个月下过有效订单的不同客户数",
      column: "customer_count",
    },
    {
      id: "m4",
      name: "客单价",
      unit: "元",
      definition: "销售额 ÷ 订单数 [2]",
      column: "avg_order_value",
    },
  ],
  dimensions: [
    {
      id: "d1",
      name: "统计月",
      definition: "下单时间所在的月份，写成 2026-10 [4]",
      column: "stat_month",
      examples: ["2026-09", "2026-10"],
    },
    {
      id: "d2",
      name: "城市",
      definition: "下单客户档案里的城市 [3]",
      column: "city",
      examples: ["上海", "北京", "未知"],
    },
    {
      id: "d3",
      name: "会员等级",
      definition: "下单时客户的会员等级",
      column: "member_level",
      examples: ["gold", "silver", "normal"],
    },
  ],
  filters: [
    {
      id: "f1",
      text: "只算有效订单：已支付、已发货或已完成，并且不是测试账号 [1]",
    },
  ],
  time_range: {
    text: "2025-11 至 2026-10，按下单时间 [4]",
    column: "stat_month",
  },
};
const uncited = ["m3", "d3"];
const baseRules = [
  {
    id: "r1",
    text: "城市名去掉末尾的「市」，缺失时记为「未知」[3]",
    basis: "knowledge",
  },
  { id: "r2", text: "折扣为空时按 0 处理", basis: "data" },
  { id: "r3", text: "客单价保留原值，页面显示两位小数", basis: "assumption" },
];
const questions = [
  {
    id: "q1",
    text: "统计哪段时间？",
    why: "需求没说时间范围，源库里有 12 个月的订单。",
    options: [
      { value: "all", label: "源库现有的 12 个月" },
      { value: "y2026", label: "只看 2026 年" },
    ],
    suggested: "all",
    affects: ["m1", "m2", "m3", "m4"],
  },
  {
    id: "q2",
    text: "退款的订单算不算有效订单？",
    why: "口径资料没有说退款订单怎么算，会影响销售额和订单数。",
    options: [
      { value: "exclude", label: "不算，整单去掉" },
      { value: "include", label: "算，按下单时的金额" },
    ],
    suggested: "exclude",
    affects: ["f1", "m1", "m2"],
  },
];
const targets = [
  {
    name: "dwd_sales_order_line",
    description: "有效订单的商品明细，一行一个订单商品 [1]",
    sources: ["retail.orders", "retail.order_items", "retail.customers"],
    logic: "订单关联订单商品和客户，只留有效订单，算出每行的成交金额",
    columns: [
      {
        name: "order_id",
        label: "订单号",
        source: "o.order_id",
        description: "订单编号",
      },
      {
        name: "line_no",
        label: "行号",
        source: "i.line_no",
        description: "订单里的第几件商品",
      },
      {
        name: "stat_month",
        label: "统计月",
        source: "date_format(o.ordered_at, 'yyyy-MM')",
        description: "下单月份 [4]",
      },
      {
        name: "city",
        label: "城市",
        source: "coalesce(regexp_replace(trim(c.city), '市$', ''), '未知')",
        description: "客户城市 [3]",
      },
      {
        name: "member_level",
        label: "会员等级",
        source: "c.member_level",
        description: "会员等级",
      },
      {
        name: "customer_id",
        label: "客户号",
        source: "o.customer_id",
        description: "下单客户",
      },
      {
        name: "line_amount",
        label: "成交金额（元）",
        source: "i.quantity * i.unit_price * (1 - coalesce(i.discount, 0))",
        description: "不含运费 [2]",
      },
    ],
    primary_key: ["order_id", "line_no"],
    load: { mode: "full", key: null },
    quality_rules: [
      {
        name: "valid_amount",
        condition: "line_amount >= 0",
        on_violation: "drop",
      },
      {
        name: "order_id_not_null",
        condition: "order_id IS NOT NULL",
        on_violation: "fail",
      },
    ],
  },
  {
    name: "ads_sales_city_member_m",
    description: "每个统计月 × 城市 × 会员等级一行的销售汇总",
    sources: ["dwd_sales_order_line"],
    logic: "按统计月、城市、会员等级汇总明细",
    columns: [
      {
        name: "stat_month",
        label: "统计月",
        source: "stat_month",
        description: "下单月份",
      },
      { name: "city", label: "城市", source: "city", description: "客户城市" },
      {
        name: "member_level",
        label: "会员等级",
        source: "member_level",
        description: "会员等级",
      },
      {
        name: "sales_amount",
        label: "销售额（元）",
        source: "sum(line_amount)",
        description: "成交金额之和 [2]",
      },
      {
        name: "order_count",
        label: "订单数",
        source: "count(order_id)",
        description: "一单一行的明细上数单数",
      },
      {
        name: "customer_count",
        label: "下单客户数",
        source: "count(DISTINCT customer_id)",
        description: "不同客户数",
      },
      {
        name: "avg_order_value",
        label: "客单价（元）",
        source: "sales_amount / nullif(order_count, 0)",
        description: "销售额 ÷ 订单数 [2]",
      },
    ],
    primary_key: ["stat_month", "city", "member_level"],
    load: { mode: "full", key: null },
    quality_rules: [
      { name: "city_not_empty", condition: "city <> ''", on_violation: "warn" },
    ],
  },
];
const answerColumns = [
  { name: "city", label: "城市", role: "dimension" },
  { name: "member_level", label: "会员等级", role: "dimension" },
  {
    name: "sales_amount",
    label: "销售额（元）",
    role: "metric",
    agg: "sum",
    unit: "元",
  },
  {
    name: "order_count",
    label: "订单数",
    role: "metric",
    agg: "sum",
    unit: null,
  },
  {
    name: "customer_count",
    label: "下单客户数",
    role: "metric",
    agg: "none",
    unit: null,
  },
  {
    name: "avg_order_value",
    label: "客单价（元）",
    role: "metric",
    agg: "ratio",
    unit: "元",
    numerator: "sales_amount",
    denominator: "order_count",
  },
];
const chart = {
  type: "bar",
  label: ["city", "member_level"],
  value: "sales_amount",
};
const design = {
  target: "ads_sales_city_member_m",
  filter: [{ column: "stat_month", pick: "latest" }],
  group_by: ["city", "member_level"],
  measures: [
    "sales_amount",
    "order_count",
    "customer_count",
    "avg_order_value",
  ],
  order_by: [{ column: "sales_amount", desc: true }],
  top_n: 10,
  chart,
  columns: answerColumns,
  text: "最近一个统计月里，各「城市 × 会员等级」组合按销售额从高到低排的前 10 组，附合计和柱状图。",
};
const formats: Record<string, string | null> = {
  city: null,
  member_level: null,
  sales_amount: "decimal",
  order_count: "integer",
  customer_count: "integer",
  avg_order_value: "decimal",
};
const groups: [string, string, number, number, number][] = [
  ["上海", "gold", 1234567.0, 5035, 812],
  ["北京", "gold", 1102345.5, 4620, 760],
  ["深圳", "gold", 986120.0, 4011, 702],
  ["上海", "silver", 802311.2, 4876, 1010],
  ["杭州", "gold", 701220.0, 2950, 520],
  ["北京", "silver", 688410.8, 4433, 980],
  ["广州", "gold", 640882.0, 2702, 488],
  ["成都", "gold", 512300.4, 2280, 430],
  ["上海", "normal", 498221.0, 5120, 1832],
  ["南京", "gold", 455002.6, 1960, 372],
];
const TARGET = "`icsd-ace-dev`.trac_b_test.ads_sales_city_member_m";
const DEV_URL = "https://adb-mock.databricks.azure.cn/#job/101/run/2001";
const quality = {
  rules: 3,
  failed_rules: 0,
  dropped: 12,
  text: "3 条质量规则全部通过，丢弃 12 行",
};
const failedQuality = {
  rules: 0,
  failed_rules: 0,
  dropped: 0,
  text: "没有质量规则",
};
const answer = {
  status: "ok",
  target: TARGET,
  period: "2026-10",
  columns: answerColumns.map((column) => ({
    ...column,
    format: formats[column.name],
  })),
  rows: groups.map(([city, member_level, sales, orders, customers]) => ({
    city,
    member_level,
    sales_amount: sales,
    order_count: orders,
    customer_count: customers,
    avg_order_value: sales / orders,
  })),
  totals: {
    sales_amount: 6709854.12,
    order_count: 55188,
    customer_count: null,
    avg_order_value: 6709854.12 / 55188,
  },
  mins: { sales_amount: 18342.6, order_count: 61 },
  groups: 27,
  row_count: 324,
  truncated: true,
  conclusion:
    "2026-10，上海 · gold 的销售额最高：1,234,567.00 元，占合计 18.4%。",
  chart,
  quality,
  sql: "SELECT `city`, `member_level`, sum(`sales_amount`) AS `sales_amount`, sum(`order_count`) AS `order_count`, CASE WHEN count(*) = 1 THEN first(`customer_count`) END AS `customer_count`, sum(`sales_amount`) / nullif(sum(`order_count`), 0) AS `avg_order_value`\nFROM `icsd-ace-dev`.`trac_b_test`.`ads_sales_city_member_m`\nWHERE `stat_month` = (SELECT max(`stat_month`) FROM `icsd-ace-dev`.`trac_b_test`.`ads_sales_city_member_m`)\nGROUP BY `city`, `member_level`\nORDER BY `sales_amount` DESC NULLS LAST, `city` ASC NULLS FIRST, `member_level` ASC NULLS FIRST\nLIMIT 10",
  url: DEV_URL,
  error: null,
};
const bundle = {
  name: "retail_sales",
  language: "pyspark",
  files: {
    "databricks.yml":
      "bundle:\n  name: retail_sales\ninclude:\n  - resources/*.yml\ntargets:\n  dev:\n    mode: development\n",
    "resources/retail_sales.job.yml":
      "resources:\n  jobs:\n    retail_sales:\n      name: retail_sales\n",
    "src/dwd_sales_order_line.py":
      'from pyspark.sql import functions as F\n\n\ndef build(spark):\n    orders = spark.read.table("retail.orders")\n    return orders.where(F.col("status").isin("paid", "shipped", "done"))\n',
    "src/ads_sales_city_member_m.py":
      'from pyspark.sql import functions as F\n\n\ndef build(spark):\n    lines = spark.read.table("dwd_sales_order_line")\n    return lines.groupBy("stat_month", "city", "member_level").agg(\n        F.sum("line_amount").alias("sales_amount")\n    )\n',
    "src/aep_harness.py": "# 平台的试跑程序\n",
  },
};
const files = Object.keys(bundle.files).sort();
function devTables(failed: boolean) {
  return failed
    ? []
    : [
        {
          name: "dwd_sales_order_line",
          target: "`icsd-ace-dev`.trac_b_test.dwd_sales_order_line",
          rows: 55176,
          dropped: 12,
          expectations: [
            { name: "valid_amount", passed: 55176, failed: 12 },
            { name: "order_id_not_null", passed: 55188, failed: 0 },
          ],
        },
        {
          name: "ads_sales_city_member_m",
          target: TARGET,
          rows: 324,
          dropped: 0,
          expectations: [{ name: "city_not_empty", passed: 324, failed: 0 }],
        },
      ];
}
const preview = [
  {
    table: "ads_sales_city_member_m",
    target: TARGET,
    columns: [
      "stat_month",
      "city",
      "member_level",
      "sales_amount",
      "order_count",
    ],
    rows: [
      {
        stat_month: "2025-11",
        city: "上海",
        member_level: "gold",
        sales_amount: 1120034.5,
        order_count: 4810,
      },
      {
        stat_month: "2025-11",
        city: "上海",
        member_level: "normal",
        sales_amount: 452210.0,
        order_count: 4922,
      },
      {
        stat_month: "2025-11",
        city: "北京",
        member_level: "gold",
        sales_amount: 998001.2,
        order_count: 4302,
      },
    ],
    sorted_by: ["stat_month", "city", "member_level"],
  },
];
const DEV_ERROR =
  "[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column or function parameter with name `discount_rate` cannot be resolved. Did you mean one of the following? [`discount`, `unit_price`].";

function devRun(failed: boolean) {
  return {
    status: failed ? "failed" : "passed",
    ok: !failed,
    url: DEV_URL,
    tables: devTables(failed),
    preview: failed ? [] : preview,
    quality: failed ? failedQuality : quality,
    answer: failed ? null : answer,
    errors: failed ? DEV_ERROR : "",
    report: failed
      ? `## Databricks dev 运行失败\n\n运行方式：Job（已有集群）。\n\n${DEV_ERROR}`
      : "## Databricks dev 运行成功\n\n运行方式：Job（已有集群）。\n\n### 结果表\n\n| 结果表 | 行数 | 丢弃行数 |\n| --- | ---: | ---: |\n| dwd_sales_order_line | 55176 | 12 |\n| ads_sales_city_member_m | 324 | 0 |\n\n### 答案\n\n2026-10，上海 · gold 的销售额最高：1,234,567.00 元，占合计 18.4%。",
  };
}

// The plan the round shows: questions answered in an earlier round are rules now.
function planFor(state: EtlV2State) {
  const asked = questions.filter(
    (question) =>
      !state.answered.some((answered) => answered.label === question.text),
  );
  const confirmed = state.answered.map((answered, i) => ({
    id: `r${baseRules.length + i + 1}`,
    text: `${answered.actor ? `${answered.actor}已确认` : "已确认"}：${answered.label}——${answered.value}`,
    basis: "confirmed",
  }));
  return {
    summary: brief.goal,
    language: "pyspark",
    brief,
    targets,
    rules: [...baseRules, ...confirmed],
    questions: asked,
    answer: design,
    assumptions: [],
    references,
  };
}
function fieldsOf(plan: ReturnType<typeof planFor>): Question[] {
  return plan.questions.map((question) => ({
    key: question.id,
    label: question.text,
    type: "select",
    required: true,
    choices: question.options,
    suggested: question.suggested,
    why: question.why,
    affects: question.affects,
  }));
}
function fingerprint(pending: unknown): string {
  return `sha256:${createHash("sha256").update(JSON.stringify(pending)).digest("hex")}`;
}

function waitAt(
  node: HumanNode,
  data: Record_,
  round: number,
  fields: Question[],
  options: string[],
): Pending {
  const approval = node.mode === "approval" ? node : null;
  const shown = (approval?.options ?? []).filter((option) =>
    options.includes(option.key),
  );
  const pending = {
    kind: "approval" as const,
    prompt: node.prompt,
    options: shown.map((option) => option.key),
    comment_required: (approval?.comment_required ?? []).filter((key) =>
      options.includes(key),
    ),
    option_labels: Object.fromEntries(
      shown.map((option) => [option.key, option.label]),
    ),
    changed_branch: approval?.changed_branch ?? null,
    ...(fields.length ? { fields } : {}),
    details: {
      type: "flow_human",
      node: node.id,
      show: node.id === "review" ? "方案见上方。" : "试算结果见上方。",
      view: node.view,
      data,
      dropped_fields: [],
    },
  };
  return {
    ...pending,
    details: { ...pending.details, round, seen: fingerprint(pending) },
  };
}

function step(
  steps: MockSteps,
  node: string,
  output: Record_ | null,
  interrupt: unknown = null,
) {
  steps.push({
    id: `${node}-${steps.length + 1}`,
    node,
    status: output === null ? "waiting" : "done",
    started_at: new Date().toISOString(),
    input: {},
    output: output === null ? null : { vars: { [node]: output } },
    error: null,
    interrupt,
  });
}

export function startEtlV2(input: Record_): EtlV2State {
  return {
    next: "review",
    rounds: { review: 0, data_review: 0, dev: 0 },
    failFirst:
      typeof input.request === "string" && input.request.includes("试算失败"),
    confirmed: null,
    reviewed: null,
    answered: [],
  };
}

const titles: Record<string, string> = {
  profile: "探查数据",
  glossary: "查业务口径",
  standards: "查开发规范",
  plan: "出方案",
  check_plan: "校验方案",
  review: "确认方案",
  ingest: "抽取到湖仓",
  codegen: "写代码",
  check_code: "检查并打包",
  dev_run: "开发环境试算",
  data_review: "审核试算结果",
};

// The run is ready to move on: where it goes next, as the template would take it.
export function advanceEtlV2(entry: Entry, ops: Ops): Run {
  const { run, steps } = entry;
  const state = entry.v2!;
  const say = (node: string, content: string) =>
    run.messages.push(
      ops.stamp(
        { id: node, title: titles[node] ?? node },
        { role: "assistant", content },
      ),
    );
  const plan = planFor(state);
  const dev = devRun(state.failFirst && state.rounds.dev === 0);
  if (state.next === "review") {
    state.rounds.review += 1;
    if (state.rounds.review === 1)
      for (const node of ["profile", "glossary", "standards"])
        step(steps, node, { ok: true });
    step(steps, "plan", { text: JSON.stringify(plan) });
    step(steps, "check_plan", { ok: true, plan, brief, answer: design });
    say(
      "check_plan",
      `方案通过校验：2 张目标表、4 个指标、${plan.questions.length} 个问题待回答`,
    );
    run.pending = waitAt(
      reviewNode,
      {
        brief,
        plan,
        answer: design,
        uncited,
        cleaned: ["去掉了 2 条规则开头的英文前缀“advance:”"],
        returned: state.reviewed,
        previous: state.confirmed,
      },
      state.rounds.review,
      fieldsOf(plan),
      ["approve", "revise"],
    );
    run.step = { node: "review", title: "确认方案" };
    step(steps, "review", null, run.pending);
    ops.setStatus(run, "waiting");
    return run;
  }
  if (state.next === "data_review") {
    state.rounds.data_review += 1;
    for (const node of ["ingest", "codegen"]) step(steps, node, { ok: true });
    step(steps, "check_code", { ok: true, bundle, files });
    step(steps, "dev_run", dev);
    state.rounds.dev += 1;
    say(
      "dev_run",
      dev.ok ? "dev 运行通过，算出了答案" : "dev 运行失败，请看报错",
    );
    run.pending = waitAt(
      dataReviewNode,
      {
        brief,
        plan,
        uncited,
        confirmed: state.confirmed,
        status: dev.status,
        tables: dev.tables,
        preview: dev.preview,
        answer: dev.answer,
        quality: dev.quality,
        url: dev.url,
        errors: dev.errors,
        files,
      },
      state.rounds.data_review,
      [],
      dev.ok
        ? ["approve", "fix_code", "fix_plan", "stop"]
        : ["fix_code", "fix_plan", "stop"],
    );
    run.step = { node: "data_review", title: "审核试算结果" };
    step(steps, "data_review", null, run.pending);
    ops.setStatus(run, "waiting");
    return run;
  }
  const shared = {
    brief,
    plan,
    report: devRun(false).report,
    confirmed: state.confirmed,
    reviewed: state.reviewed,
  };
  if (state.next === "delivered") {
    run.result = {
      ...shared,
      ingested: "抽取了 3 张表，共 61,240 行",
      answer,
      code: bundle,
      quality,
      dev_url: DEV_URL,
    };
    run.outcome = "delivered";
    run.step = { node: "end", title: "交付" };
  } else {
    const reviewed = state.reviewed ?? {};
    run.result = {
      ...shared,
      reason:
        (typeof reviewed.comment === "string" && reviewed.comment) ||
        (typeof reviewed.feedback === "string" ? reviewed.feedback : ""),
    };
    run.outcome = "stopped";
    run.step = { node: "stopped", title: "终止" };
  }
  ops.setStatus(run, "done");
  return run;
}

// Like the backend (docs/api/flow-apps.md, "带扩展设置的人工节点"): what a note's item
// points to in the step's data, by sorted keys, past the records of earlier decisions.
function sortedValues(value: Record_) {
  return Object.keys(value)
    .sort()
    .map((key) => value[key]);
}
const isDecision = (value: Record_) =>
  "node" in value && "round" in value && "decision" in value;
function* objects(value: unknown): Generator<Record_> {
  if (Array.isArray(value)) for (const item of value) yield* objects(item);
  else if (isRecord(value) && !isDecision(value)) {
    yield value;
    for (const item of sortedValues(value)) yield* objects(item);
  }
}
function* listed(value: unknown): Generator<unknown> {
  if (Array.isArray(value))
    for (const item of value)
      yield* isRecord(item) || Array.isArray(item) ? listed(item) : [item];
  else if (isRecord(value) && !isDecision(value))
    for (const item of sortedValues(value)) yield* listed(item);
}
const named = (
  value: Record_,
  id: string,
  keys = ["id", "key", "name", "path"],
) => keys.some((key) => value[key] === id);
// The label the platform gives a note: the object's own name, the listed text, or the id.
function lookUp(data: unknown, id: string): string | null {
  for (const value of objects(data))
    if (named(value, id)) {
      const label = ["label", "name", "text", "title"]
        .map((key) => value[key])
        .find((name) => typeof name === "string" && name);
      return typeof label === "string" ? label : id;
    }
  for (const value of listed(data)) if (value === id) return id;
  const dot = id.indexOf(".");
  if (dot > 0) {
    const [outer, inner] = [id.slice(0, dot), id.slice(dot + 1)];
    for (const value of objects(data))
      if (named(value, outer, ["id", "key", "name"]))
        for (const nested of objects(sortedValues(value)))
          if (named(nested, inner, ["id", "key", "name"])) return id;
  }
  return null;
}
const kinds: Record<string, string> = {
  metric: "指标",
  dimension: "维度",
  filter: "筛选",
  rule: "规则",
  question: "问题",
  column: "列",
  quality: "质量规则",
  file: "文件",
};

type Decided =
  | { status: number; body: unknown }
  | { decision: string; record: Record_; feedback: string };

// A decision at one of the two steps: the notes are checked and named, a changed answer
// or a remark turns the decision into changed_branch, and the record is written.
export function decideEtlV2(entry: Entry, body: Record_): Decided {
  const pending = entry.run.pending!;
  const details = isRecord(pending.details) ? pending.details : {};
  const raw = body.notes ?? [];
  if (!Array.isArray(raw))
    return {
      status: 422,
      body: { detail: [{ loc: ["body", "notes"], msg: "意见要是列表" }] },
    };
  if (raw.length > 50)
    return {
      status: 422,
      body: { detail: [{ loc: ["body", "notes"], msg: "最多挂 50 条意见" }] },
    };
  const notes: { item: string; label: string; text: string }[] = [];
  for (const [i, note] of raw.entries()) {
    const item =
      isRecord(note) && typeof note.item === "string" ? note.item : "";
    const text =
      isRecord(note) && typeof note.text === "string" ? note.text.trim() : "";
    const match = item.match(/^([a-z][a-z0-9_]*):(.{1,200})$/s);
    const label = match ? lookUp(details.data, match[2]) : null;
    if (!match || label === null)
      return {
        status: 422,
        body: {
          detail: [
            {
              loc: ["body", "notes", i, "item"],
              msg: `找不到「${item}」这一项`,
            },
          ],
        },
      };
    if (!text || text.length > 500)
      return {
        status: 422,
        body: {
          detail: [
            { loc: ["body", "notes", i, "text"], msg: "意见要写 1–500 个字" },
          ],
        },
      };
    notes.push({
      item,
      label: label.length > 60 ? `${label.slice(0, 59)}…` : label,
      text,
    });
  }
  const asked = pending.fields ?? [];
  const values = isRecord(body.values) ? body.values : {};
  const comment = typeof body.comment === "string" ? body.comment.trim() : "";
  const changed = answersChanged(asked, values);
  const remarked = Boolean(comment) || notes.length > 0;
  const required = pending.comment_required ?? [];
  const submitted = String(body.decision);
  const turn = pending.changed_branch;
  const decision =
    turn &&
    pending.options.includes(turn) &&
    submitted !== turn &&
    !required.includes(submitted) &&
    (changed || remarked)
      ? turn
      : submitted;
  if (required.includes(decision) && !remarked && !changed)
    return { status: 422, body: { detail: "这个决定需要写明意见" } };
  const label = (key: string) => pending.option_labels?.[key] ?? key;
  const round = typeof details.round === "number" ? details.round : 1;
  const node = String(details.node);
  const actor =
    typeof body.actor === "string" && body.actor.trim()
      ? body.actor.trim()
      : null;
  const answers = asked.map((field) => {
    const choice = (value: unknown) =>
      field.choices?.find((option) => option.value === value)?.label ??
      String(value);
    return {
      key: field.key,
      label: field.label,
      value: values[field.key],
      value_label: choice(values[field.key]),
      suggested: field.suggested ?? null,
      suggested_label: field.suggested == null ? null : choice(field.suggested),
      picked_suggested:
        field.suggested == null ? null : values[field.key] === field.suggested,
    };
  });
  const record = {
    node,
    title: entry.run.step?.title ?? node,
    round,
    actor,
    at: new Date().toISOString(),
    decision,
    decision_label: label(decision),
    submitted: decision !== submitted ? submitted : null,
    seen: typeof details.seen === "string" ? details.seen : null,
    comment,
    changed,
    notes,
    answers,
    dropped_fields: [],
  };
  const feedback =
    decision === turn || required.includes(decision)
      ? [
          `【第 ${round} 轮 · ${record.title} · ${label(decision)}】`,
          ...answers
            .filter((answer) => answer.picked_suggested === false)
            .map((answer) => `${answer.label}改选「${answer.value_label}」`),
          ...(comment ? [`意见：${comment}`] : []),
          ...notes.map((note) => {
            const kind = kinds[note.item.split(":")[0]] ?? "条目";
            return `${kind}「${note.label}」：${note.text}`;
          }),
        ].join("\n")
      : "";
  return { decision, record, feedback };
}

// The decision is taken: the run goes on to where the template sends it.
export function moveEtlV2(
  entry: Entry,
  decided: { decision: string; record: Record_; feedback: string },
) {
  const state = entry.v2!;
  const record = { ...decided.record, feedback: decided.feedback };
  if (decided.record.node === "review") {
    state.confirmed = decided.record;
    for (const answer of decided.record.answers as Record_[])
      if (!state.answered.some((item) => item.label === answer.label))
        state.answered.push({
          label: String(answer.label),
          value: String(answer.value_label),
          actor: (decided.record.actor as string | null) ?? null,
        });
    state.next = decided.decision === "approve" ? "data_review" : "review";
  } else {
    state.reviewed = record;
    state.next =
      decided.decision === "approve"
        ? "delivered"
        : decided.decision === "stop"
          ? "stopped"
          : decided.decision === "fix_plan"
            ? "review"
            : "data_review";
  }
  const waiting = entry.steps.findLast((item) => item.status === "waiting");
  if (waiting) {
    waiting.status = "done";
    waiting.output = { vars: { [String(decided.record.node)]: record } };
  }
}
