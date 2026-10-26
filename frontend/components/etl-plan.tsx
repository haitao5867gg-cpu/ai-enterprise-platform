import { languageLabels, type EtlPlan } from "../lib/etl";
import { Icon } from "./icons";

const violationLabels = { warn: "告警", drop: "丢弃记录", fail: "停止管道" };

export function EtlPlanView({ plan }: { plan: EtlPlan }) {
  return (
    <section className="panel etl-plan" aria-labelledby="etl-plan-title">
      <div className="section-heading">
        <h2 id="etl-plan-title">
          <Icon name="grid" />
          ETL 方案
        </h2>
        <span className="app-tag">
          {languageLabels[plan.language]} · {plan.targets.length} 张目标表
        </span>
      </div>
      <p className="plan-summary">{plan.summary}</p>
      <div className="plan-targets">
        {plan.targets.map((target, index) => (
          <article
            className="plan-target"
            key={`${target.name}-${index}`}
            aria-label={`目标表 ${target.name}`}
          >
            <div className="target-heading">
              <div>
                <span className="eyebrow">
                  目标表 {String(index + 1).padStart(2, "0")}
                </span>
                <h3>
                  <code>{target.name}</code>
                </h3>
              </div>
              <span className="load-badge">
                {target.load.mode === "incremental" ? "增量加载" : "全量加载"}
              </span>
            </div>
            {target.description && (
              <p className="target-description">{target.description}</p>
            )}
            <dl className="plan-facts">
              <div>
                <dt>来源</dt>
                <dd className="source-tags">
                  {target.sources.map((source, i) => (
                    <code key={i}>{source}</code>
                  ))}
                </dd>
              </div>
              <div>
                <dt>做法与清洗规则</dt>
                <dd>{target.logic}</dd>
              </div>
              <div>
                <dt>主键</dt>
                <dd>
                  <code>{target.primary_key.join(", ") || "未设置"}</code>
                </dd>
              </div>
              <div>
                <dt>加载策略</dt>
                <dd>
                  {target.load.mode === "incremental" ? (
                    <>
                      增量加载 · 增量键：
                      <code>{target.load.key || "未设置"}</code>
                    </>
                  ) : (
                    "全量加载"
                  )}
                </dd>
              </div>
            </dl>
            <h4>列映射</h4>
            <div className="table-scroll">
              <table
                className="mapping-table"
                aria-label={`${target.name} 列映射`}
              >
                <thead>
                  <tr>
                    <th scope="col">目标列</th>
                    <th scope="col">来源 / 表达式</th>
                    <th scope="col">说明</th>
                  </tr>
                </thead>
                <tbody>
                  {target.columns.map((column, i) => (
                    <tr key={i}>
                      <td>
                        <code>{column.name}</code>
                      </td>
                      <td>
                        <code>{column.source}</code>
                      </td>
                      <td>{column.description || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <h4>质量规则</h4>
            {target.quality_rules.length === 0 ? (
              <p className="muted">未设置质量规则。</p>
            ) : (
              <ul className="quality-rules">
                {target.quality_rules.map((rule, i) => (
                  <li key={i}>
                    <div>
                      <code>{rule.name}</code>
                      <span>
                        不满足时：{violationLabels[rule.on_violation]}
                      </span>
                    </div>
                    <code className="rule-condition">{rule.condition}</code>
                  </li>
                ))}
              </ul>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
