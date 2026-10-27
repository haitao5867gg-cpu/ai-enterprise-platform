import "server-only";
import type { DataSource, EtlLanguage, EtlPlan } from "../etl";

export const mockDataSources: DataSource[] = [
  {
    datasource_id: "sample-postgres",
    name: "示例库",
    kind: "postgres",
    description: "合成客户与订单数据，仅用于本地工作台演示。",
  },
];

export function mockEtlPlan(language: EtlLanguage, feedback?: string): EtlPlan {
  return {
    summary:
      "清洗客户与订单数据，为分析准备两张目标表。" +
      (feedback ? `已根据意见调整：${feedback}` : ""),
    language,
    assumptions: [
      "需求说的“有效订单”理解为订单号不为空，样本里有 2% 的订单号为空。",
      "金额为负的订单视为退款，保留但不计入销售额。",
    ],
    targets: [
      {
        name: "orders_clean",
        description: "有效订单明细，按更新时间增量加载。",
        sources: ["public.orders"],
        logic:
          "去除订单号为空的记录，金额转为小数，按订单号去重。" +
          (feedback ? `调整要求：${feedback}` : ""),
        columns: [
          { name: "order_id", source: "id", description: "源订单编号" },
          {
            name: "amount",
            source: "CAST(amount AS DECIMAL(18, 2))",
            description: "订单金额",
          },
          {
            name: "updated_at",
            source: "updated_at",
            description: "源记录更新时间",
          },
        ],
        primary_key: ["order_id"],
        load: { mode: "incremental", key: "updated_at" },
        quality_rules: [
          {
            name: "valid_order",
            condition: "order_id IS NOT NULL",
            on_violation: "drop",
          },
          {
            name: "positive_amount",
            condition: "amount >= 0",
            on_violation: "fail",
          },
        ],
      },
      {
        name: "customers_clean",
        description: "规范化客户邮箱，供下游关联。",
        sources: ["public.customers"],
        logic: "邮箱去除首尾空格并转为小写，按客户号去重。",
        columns: [
          { name: "customer_id", source: "id", description: "客户编号" },
          {
            name: "email",
            source: "lower(trim(email))",
            description: "标准化邮箱",
          },
        ],
        primary_key: ["customer_id"],
        load: { mode: "full", key: null },
        quality_rules: [
          {
            name: "valid_email",
            condition: "email IS NOT NULL",
            on_violation: "warn",
          },
        ],
      },
    ],
  };
}

export type MockApproval = {
  step: "plan" | "release";
  decision: string;
  comment: string;
  actor: string;
  at: string;
};

export const mockProdTarget = {
  mode: "job",
  catalog: "prod_catalog",
  schema: "etl",
  schedule: "0 0 2 * * ?",
  timezone: "Asia/Shanghai",
  cluster_id: "mock-cluster",
};

// The run result as the backend returns it: what a passing dev run leaves for the release
// approval. Version 2 and later are the code rewritten after a rejected release.
export function mockEtlResult(
  plan: EtlPlan,
  approvals: MockApproval[],
  version = 1,
): Record<string, unknown> {
  const python = plan.language === "pyspark";
  const rewrite = version > 1 ? `# 第 ${version} 版：按上线审批意见重写\n` : "";
  const files: Record<string, string> = {
    "databricks.yml":
      "bundle:\n  name: etl_demo\ninclude:\n  - resources/*.yml\ntargets:\n  dev:\n    default: true\n",
    "resources/pipeline.yml": `resources:\n  pipelines:\n    etl_demo:\n      name: etl_demo\n      libraries:\n        - ${python ? "file" : "notebook"}:\n            path: ../src/orders_clean.${python ? "py" : "sql"}\n`,
    "README.md":
      "# ETL 示例\n\n这是本地 mock 生成的演示文件，不会部署或访问真实数据。\n",
  };
  for (const target of plan.targets) {
    files[`src/${target.name}.${python ? "py" : "sql"}`] = python
      ? `${rewrite}# 本地模拟代码：${target.description}\nfrom pyspark import pipelines as dp\n\n\n@dp.table\ndef ${target.name}():\n    return spark.read.table(\"${target.sources[0]}\")\n`
      : `${rewrite.replace("#", "--")}-- 本地模拟代码：${target.description}\nCREATE OR REFRESH MATERIALIZED VIEW ${target.name}\nAS SELECT * FROM ${target.sources[0]};\n`;
  }
  return {
    bundle: "etl_demo",
    language: plan.language,
    plan,
    files,
    checks: { passed: true, problems: [] },
    verification: {
      status: "passed",
      reason: "",
      mode: "job",
      url: null,
      attempt: 1,
      errors: [],
      warnings: [],
      tables: plan.targets.map((target, index) => ({
        name: target.name,
        status: "COMPLETED",
        rows: 1000 - index * 200,
        dropped: index === 0 ? 12 : 0,
      })),
    },
    approvals,
    asset: {
      status: "skipped",
      reason: "本地 mock 不提交资产仓库",
    },
  };
}

export const mockDeployed = {
  status: "deployed",
  ...mockProdTarget,
  job_id: 101,
  url: null,
  steps: [],
  errors: [],
};
