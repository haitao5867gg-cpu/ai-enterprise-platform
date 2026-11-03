import {
  CitationScope,
  CitedMarkdown,
  CitedText,
  SourceList,
} from "./citations";
import { useT } from "../lib/i18n/client";
import type { ReactNode } from "react";
import { languageLabels, type EtlPlan } from "../lib/etl";
import { Icon } from "./icons";

const violationLabels = { warn: "告警", drop: "丢弃记录", fail: "停止管道" };

// A plan up for approval lists what it relies on and asks about its
// assumptions. As part of a run's result (`asResult`) its [n] are the run's
// sources, which the result lists.
function OwnSources({
  plan,
  own,
  children,
}: {
  plan: EtlPlan;
  own: boolean;
  children: ReactNode;
}) {
  return own ? (
    <CitationScope sources={plan.references ?? []}>{children}</CitationScope>
  ) : (
    children
  );
}

export function EtlPlanView({
  plan,
  asResult = false,
}: {
  plan: EtlPlan;
  asResult?: boolean;
}) {
  const t = useT();
  return (
    <OwnSources plan={plan} own={!asResult}>
      <section className="panel etl-plan" aria-labelledby="etl-plan-title">
        <div className="section-heading">
          <h2 id="etl-plan-title">
            <Icon name="grid" />

            {t("ETL 方案")}
          </h2>
          <span className="app-tag">
            {t("{language} · {n} 张目标表", {
              language: t(languageLabels[plan.language]),
              n: plan.targets.length,
            })}
          </span>
        </div>
        <div className="plan-summary">
          <CitedMarkdown text={plan.summary} />
        </div>
        {plan.assumptions.length > 0 && (
          <section
            className="plan-assumptions"
            aria-labelledby="assumptions-title"
          >
            <h3 id="assumptions-title">
              <Icon name="alert" size={16} />
              {asResult ? t("方案里的假设") : t("请确认这些假设")}
            </h3>
            <p>
              {t("需求和数据对不上或有歧义的地方，方案是这样理解的。")}
              {!asResult && t("不对的话请驳回并写明。")}
            </p>
            <ul>
              {plan.assumptions.map((assumption, i) => (
                <li key={i}>
                  <CitedText>{assumption}</CitedText>
                </li>
              ))}
            </ul>
          </section>
        )}
        <div className="plan-targets">
          {plan.targets.map((target, index) => (
            <article
              className="plan-target"
              key={`${target.name}-${index}`}
              aria-label={t("目标表 {name}", { name: target.name })}
            >
              <div className="target-heading">
                <div>
                  <span className="eyebrow">
                    {t("目标表 {n}", { n: String(index + 1).padStart(2, "0") })}
                  </span>
                  <h3>
                    <code>{target.name}</code>
                  </h3>
                </div>
                <span className="load-badge">
                  {target.load.mode === "incremental"
                    ? t("增量加载")
                    : t("全量加载")}
                </span>
              </div>
              {target.description && (
                <p className="target-description">
                  <CitedText>{target.description}</CitedText>
                </p>
              )}
              <dl className="plan-facts">
                <div>
                  <dt>{t("来源")}</dt>
                  <dd className="source-tags">
                    {target.sources.map((source, i) => (
                      <code key={i}>{source}</code>
                    ))}
                  </dd>
                </div>
                <div>
                  <dt>{t("做法与清洗规则")}</dt>
                  <dd>
                    <CitedText>{target.logic}</CitedText>
                  </dd>
                </div>
                <div>
                  <dt>{t("主键")}</dt>
                  <dd>
                    <code>{target.primary_key.join(", ") || t("未设置")}</code>
                  </dd>
                </div>
                <div>
                  <dt>{t("加载策略")}</dt>
                  <dd>
                    {target.load.mode === "incremental" ? (
                      <>
                        {t("增量加载 · 增量键：")}
                        <code>{target.load.key || t("未设置")}</code>
                      </>
                    ) : (
                      t("全量加载")
                    )}
                  </dd>
                </div>
              </dl>
              {/* In a result the mapping is reference: folded per table. */}
              <details className="plan-target-more" open={!asResult}>
                <summary>
                  {t("列映射和质量规则")}
                  <span className="muted">
                    {" "}
                    ·{" "}
                    {t("{columns} 列，{rules} 条规则", {
                      columns: target.columns.length,
                      rules: target.quality_rules.length,
                    })}
                  </span>
                </summary>
                <h4>{t("列映射")}</h4>
                <div className="table-scroll">
                  <table
                    className="mapping-table"
                    aria-label={t("{name} 列映射", { name: target.name })}
                  >
                    <thead>
                      <tr>
                        <th scope="col">{t("目标列")}</th>
                        <th scope="col">{t("来源 / 表达式")}</th>
                        <th scope="col">{t("说明")}</th>
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
                          <td>
                            <CitedText>{column.description || "—"}</CitedText>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <h4>{t("质量规则")}</h4>
                {target.quality_rules.length === 0 ? (
                  <p className="muted">{t("未设置质量规则。")}</p>
                ) : (
                  <ul className="quality-rules">
                    {target.quality_rules.map((rule, i) => (
                      <li key={i}>
                        <div>
                          <code>{rule.name}</code>
                          <span>
                            {t("不满足时：{action}", {
                              action: t(violationLabels[rule.on_violation]),
                            })}
                          </span>
                        </div>
                        <code className="rule-condition">{rule.condition}</code>
                      </li>
                    ))}
                  </ul>
                )}
              </details>
            </article>
          ))}
        </div>
        {!asResult && <SourceList title={t("依据的资料")} />}
      </section>
    </OwnSources>
  );
}
