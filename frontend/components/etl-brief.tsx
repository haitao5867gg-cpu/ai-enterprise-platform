"use client";

import { useT, type Translate } from "../lib/i18n/client";
import { CitedText, SourceList } from "./citations";
import { NoteButton } from "./review-notes";
import { formatTime } from "../lib/runs";
import {
  checkKinds,
  checkSeverities,
  metricColumns,
  type AnswerDesign,
  type Brief,
  type PlanTarget,
  type PlanV2,
  type ReviewRecord,
} from "../lib/etl-v2";

const violationLabels: Record<string, string> = {
  warn: "只告警",
  drop: "丢弃这一行",
  fail: "整次更新失败",
};
const basisLabels: [string, string][] = [
  ["knowledge", "按口径资料"],
  ["request", "按需求"],
  ["data", "按数据里看到的"],
  // Only the platform writes confirmed: from the answers on the review page (I-17).
  ["confirmed", "已经确认过（确认页上回答的）"],
  ["assumption", "假设，未确认"],
];
const UNCITED = "没找到口径文档，按需求理解";
// A text as a name, without its [n].
export const uncite = (text: string) => text.replace(/\s*\[\d+\]/g, "");

function Uncited({ shown }: { shown: boolean }) {
  const t = useT();
  return shown ? <span className="uncited-tag">{t(UNCITED)}</span> : null;
}

// 我们理解你要的是: the business brief, in the words of the business. `notes` lets a
// person hang a remark on a metric, a dimension or a filter.
export function BriefView({
  brief,
  uncited,
  notes = false,
}: {
  brief: Brief;
  uncited: string[];
  notes?: boolean;
}) {
  const t = useT();
  const missing = new Set(uncited);
  return (
    <section className="etl-brief" aria-labelledby="brief-title">
      <h3 id="brief-title">{t("我们理解你要的是")}</h3>
      <p className="brief-goal">
        <CitedText>{brief.goal}</CitedText>
      </p>
      {brief.grain && (
        <p className="brief-grain">
          <span>{t("粒度")}</span>
          <CitedText>{brief.grain}</CitedText>
        </p>
      )}
      <h4>{t("指标")}</h4>
      {brief.metrics.length ? (
        <div className="table-scroll">
          <table className="mapping-table brief-table" aria-label={t("指标")}>
            <thead>
              <tr>
                <th scope="col">{t("名称")}</th>
                <th scope="col">{t("单位")}</th>
                <th scope="col">{t("怎么算")}</th>
              </tr>
            </thead>
            <tbody>
              {brief.metrics.map((metric) => (
                <tr
                  key={metric.id}
                  className={missing.has(metric.id) ? "uncited" : undefined}
                >
                  <td>
                    <strong>{metric.name}</strong>
                    <Uncited shown={missing.has(metric.id)} />
                    {metricColumns(metric) && (
                      <span className="muted metric-columns">
                        {t("结果列 {columns}", {
                          columns: metricColumns(metric),
                        })}
                      </span>
                    )}
                  </td>
                  <td>{metric.unit ?? "—"}</td>
                  <td>
                    <CitedText>{metric.definition}</CitedText>
                    {notes && (
                      <NoteButton
                        item={`metric:${metric.id}`}
                        label={metric.name}
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">{t("方案里没有指标。")}</p>
      )}
      {brief.dimensions.length > 0 && (
        <>
          <h4>{t("维度")}</h4>
          <ul className="brief-list">
            {brief.dimensions.map((dimension) => (
              <li
                key={dimension.id}
                className={missing.has(dimension.id) ? "uncited" : undefined}
              >
                <strong>{dimension.name}</strong>
                <Uncited shown={missing.has(dimension.id)} />
                <span>
                  <CitedText>{dimension.definition}</CitedText>
                </span>
                {dimension.examples.length > 0 && (
                  <span className="muted">
                    {t("例如：{examples}", {
                      examples: dimension.examples.join(t("、")),
                    })}
                  </span>
                )}
                {notes && (
                  <NoteButton
                    item={`dimension:${dimension.id}`}
                    label={dimension.name}
                  />
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      <h4>{t("筛选")}</h4>
      {brief.filters.length ? (
        <ul className="brief-list">
          {brief.filters.map((filter) => (
            <li
              key={filter.id}
              className={missing.has(filter.id) ? "uncited" : undefined}
            >
              <span>
                <CitedText>{filter.text}</CitedText>
              </span>
              <Uncited shown={missing.has(filter.id)} />
              {notes && (
                <NoteButton
                  item={`filter:${filter.id}`}
                  label={uncite(filter.text)}
                />
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">{t("不筛选，用全部数据。")}</p>
      )}
      <h4>{t("时间范围")}</h4>
      <p>
        {brief.time_range ? (
          <CitedText>{brief.time_range.text}</CitedText>
        ) : (
          <span className="muted">{t("需求没限定时间范围。")}</span>
        )}
      </p>
    </section>
  );
}

// 最后给你的答案: the sentence of the answer design, and the shape of the table the
// answer reads, a dimension with the brief's examples and a metric as worked out later.
export function AnswerShape({
  design,
  brief,
}: {
  design: AnswerDesign;
  brief: Brief;
}) {
  const t = useT();
  const examples = (column: string) =>
    brief.dimensions.find((dimension) => dimension.column === column)
      ?.examples ?? [];
  return (
    <section className="etl-answer-shape" aria-labelledby="answer-shape-title">
      <h3 id="answer-shape-title">{t("最后给你的答案")}</h3>
      <p>{design.text}</p>
      <h4>{t("结果表长这样")}</h4>
      <div className="table-scroll">
        <table className="mapping-table" aria-label={t("结果表长这样")}>
          <thead>
            <tr>
              {design.columns.map((column) => (
                <th key={column.name} scope="col">
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              {design.columns.map((column) => (
                <td key={column.name}>
                  {column.role === "dimension" ? (
                    examples(column.name).join("、") || "—"
                  ) : (
                    <span className="muted">{t("运行后得出")}</span>
                  )}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  );
}

// 技术方案: each target table's columns, keys, load and quality rules; the rules the
// plan follows (folded by default) and the material it relies on. `columnNotes` and
// `ruleNotes` say what a person may hang a remark on here.
// How a target table is written: versions 1 and 2 say 全量 or 增量, version 3 one of
// its four ways (docs/api/data-nodes.md, 写表方式).
function loadLabel(t: Translate, target: PlanTarget): string {
  const load = target.load;
  switch (load.mode) {
    case "incremental":
      return t("增量加载 · {key}", { key: load.key ?? t("未设增量键") });
    case "overwrite":
      return t("整表覆盖");
    case "replace_where":
      return t("按条件替换 · {where}", { where: load.where ?? "" });
    case "append":
      return t("追加");
    case "merge":
      return t("按键合并 · {keys}", {
        keys: (load.keys.length ? load.keys : target.primary_key).join(t("、")),
      });
    default:
      return t("全量加载");
  }
}

export function TechPlanView({
  plan,
  columnNotes = false,
  qualityNotes = false,
  ruleNotes = false,
  checkNotes = false,
  checkCap = null,
}: {
  plan: PlanV2;
  columnNotes?: boolean;
  qualityNotes?: boolean;
  ruleNotes?: boolean;
  checkNotes?: boolean;
  // The project's cap on the checks the model proposes, when below error.
  checkCap?: "warn" | "info" | null;
}) {
  const t = useT();
  // A model's error-level check blocks delivery when it fails (jadeflow retest I-10).
  const blocking = plan.checks.filter(
    (check) => check.severity === "error",
  ).length;
  return (
    <section className="etl-tech-plan" aria-labelledby="tech-plan-title">
      <h3 id="tech-plan-title">{t("技术方案")}</h3>
      {plan.summary && (
        <p className="target-description">
          <CitedText>{plan.summary}</CitedText>
        </p>
      )}
      <div className="plan-targets">
        {plan.targets.map((target, i) => (
          <article
            key={target.name}
            className="plan-target"
            aria-label={t("目标表 {name}", { name: target.name })}
          >
            <div className="target-heading">
              <h4>
                <code>{target.name}</code>
              </h4>
              <span className="load-badge">{loadLabel(t, target)}</span>
            </div>
            {target.description && (
              <p className="target-description">
                <CitedText>{target.description}</CitedText>
              </p>
            )}
            <dl className="plan-facts">
              {target.sources.length > 0 && (
                <div>
                  <dt>{t("来源")}</dt>
                  <dd className="source-tags">
                    {target.sources.map((source) => (
                      <code key={source}>{source}</code>
                    ))}
                  </dd>
                </div>
              )}
              {target.logic && (
                <div>
                  <dt>{t("做法")}</dt>
                  <dd>
                    <CitedText>{target.logic}</CitedText>
                  </dd>
                </div>
              )}
              <div>
                <dt>{t("主键")}</dt>
                <dd>
                  <code>{target.primary_key.join(", ") || t("未设置")}</code>
                </dd>
              </div>
              {target.load.add_columns.length > 0 && (
                <div>
                  <dt>{t("加列")}</dt>
                  <dd>
                    {t("表已经存在，确认方案后在表里加 {n} 列：", {
                      n: target.load.add_columns.length,
                    })}
                    {target.load.add_columns.map((column, i) => (
                      <span key={column.name}>
                        {i > 0 && t("；")}
                        <code>{column.name}</code>
                        {column.type && (
                          <>
                            {" "}
                            <code>{column.type.toUpperCase()}</code>
                          </>
                        )}
                        {t("，可为空")}
                        {column.after ? (
                          <>
                            {t("（放在")} <code>{column.after}</code>{" "}
                            {t("后面）")}
                          </>
                        ) : (
                          t("（放在最前面）")
                        )}
                      </span>
                    ))}
                  </dd>
                </div>
              )}
            </dl>
            {/* With several tables the steps on the way fold their columns and rules,
                so the tables can be scanned first. The last table (the one the run
                ends with) and a table that gains columns stay open. */}
            <details
              className="plan-target-more"
              open={
                i === plan.targets.length - 1 ||
                target.load.add_columns.length > 0
              }
            >
              <summary>
                {t("列和质量规则")}
                <span className="muted">
                  {" "}
                  ·{" "}
                  {t("{columns} 列，{rules} 条规则", {
                    columns: target.columns.length,
                    rules: target.quality_rules.length,
                  })}
                </span>
              </summary>
              <div className="table-scroll">
                <table
                  className="mapping-table"
                  aria-label={t("{name} 的列", { name: target.name })}
                >
                  <thead>
                    <tr>
                      <th scope="col">{t("中文名")}</th>
                      <th scope="col">{t("列名")}</th>
                      <th scope="col">{t("Spark 表达式")}</th>
                      <th scope="col">{t("说明")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {target.columns.map((column) => (
                      <tr key={column.name}>
                        <td>{column.label || "—"}</td>
                        <td className="column-name">
                          <code>{column.name}</code>
                          {column.type && (
                            <span className="column-type">
                              {column.type.toUpperCase()}
                            </span>
                          )}
                          {target.load.add_columns.some(
                            (added) => added.name === column.name,
                          ) && (
                            <span className="column-added">{t("新加")}</span>
                          )}
                        </td>
                        <td>
                          <code>{column.source}</code>
                        </td>
                        <td>
                          <CitedText>{column.description || "—"}</CitedText>
                          {columnNotes && (
                            <NoteButton
                              item={`column:${target.name}.${column.name}`}
                              label={column.label || column.name}
                            />
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <h5>{t("质量规则")}</h5>
              {target.quality_rules.length ? (
                <ul className="quality-rules">
                  {target.quality_rules.map((rule) => (
                    <li key={rule.name}>
                      <div>
                        <code>{rule.name}</code>
                        <span>
                          {t("不满足时：{action}", {
                            action: t(
                              violationLabels[rule.on_violation] ??
                                rule.on_violation,
                            ),
                          })}
                        </span>
                      </div>
                      <code className="rule-condition">{rule.condition}</code>
                      {qualityNotes && (
                        <NoteButton
                          item={`quality:${target.name}.${rule.name}`}
                          label={rule.name}
                        />
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">{t("没有质量规则。")}</p>
              )}
            </details>
          </article>
        ))}
      </div>
      {plan.checks.length > 0 && (
        <section className="plan-checks" aria-label={t("建议的数据检查")}>
          <h4>{t("数据检查")}</h4>
          <p className="muted">
            {t("开发环境试算之后检查；填了老系统结果表时另加一条逐行对比。")}
          </p>
          {checkCap && (
            <p className="check-cap-note" role="note">
              {checkCap === "warn"
                ? t(
                    "本项目规定方案建议的检查最高按警告跑，不通过时不挡住交付；要挡住交付的检查，请加到项目的验收检查里。",
                  )
                : t(
                    "本项目规定方案建议的检查最高按提示跑，不通过时不挡住交付；要挡住交付的检查，请加到项目的验收检查里。",
                  )}
            </p>
          )}
          {blocking > 0 && (
            <p className="check-blocking-note" role="note">
              {t(
                "其中 {n} 条是错误级，会挡住交付：不通过就不能选“通过”。这些检查是模型建议的，请核对条件和方向写得对；拿不准的写意见改成警告。",
                { n: blocking },
              )}
            </p>
          )}
          <ul className="quality-rules">
            {plan.checks.map((check) => (
              <li
                key={check.id}
                className={
                  check.severity === "error" ? "check-blocking" : undefined
                }
              >
                <div>
                  <strong>{check.label}</strong>
                  <span>
                    {t(checkKinds[check.kind] ?? check.kind)}
                    {check.severity === "error"
                      ? t(" · 错误：会挡住交付")
                      : ` · ${t(checkSeverities[check.severity] ?? check.severity)}`}
                  </span>
                </div>
                <p className="check-text">{check.text}</p>
                {checkNotes && (
                  <NoteButton item={`check:${check.id}`} label={check.label} />
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {plan.rules.length > 0 && (
        <details className="plan-rules">
          <summary>
            {t("已按口径处理（{n} 条）", { n: plan.rules.length })}
          </summary>
          {basisLabels.map(([basis, label]) => {
            const rules = plan.rules.filter(
              (rule) =>
                rule.basis === basis ||
                (basis === "assumption" &&
                  !basisLabels.some(([known]) => known === rule.basis)),
            );
            return rules.length ? (
              <div key={basis}>
                <h5>{t(label)}</h5>
                <ul className="brief-list">
                  {rules.map((rule) => (
                    <li key={rule.id}>
                      <span>
                        <CitedText>{rule.text}</CitedText>
                      </span>
                      {ruleNotes && (
                        <NoteButton
                          item={`rule:${rule.id}`}
                          label={rule.text}
                        />
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null;
          })}
        </details>
      )}
      <SourceList title={t("依据的资料")} />
    </section>
  );
}

// What a person decided at a human step: who and when, each answer and whether it was
// the suggestion, the remarks and the fingerprint of what they saw.
export function RecordView({
  record,
  title,
}: {
  record: ReviewRecord;
  title: string;
}) {
  const t = useT();
  return (
    <section className="etl-record" aria-label={title}>
      <h3>{title}</h3>
      <p>
        {record.actor ?? t("未署名")}
        {record.at ? ` · ${formatTime(record.at)}` : ""} ·{" "}
        {t("第 {round} 轮 · 选了「{decision}」", {
          round: record.round,
          decision: record.decision_label,
        })}
        {record.submitted && record.submitted !== record.decision
          ? t("（点的是另一个选项，因为改了答案或写了意见）")
          : ""}
      </p>
      {record.answers.length > 0 && (
        <ul className="record-answers">
          {record.answers.map((answer) => (
            <li key={answer.key}>
              <span>{answer.label}</span>
              <strong>{answer.value_label}</strong>
              {answer.picked_suggested === true && (
                <span className="suggested-tag">{t("选了建议")}</span>
              )}
              {answer.picked_suggested === false && (
                <span className="changed-tag">
                  {t("改选（建议是「{label}」）", {
                    label: answer.suggested_label ?? "—",
                  })}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {record.comment && (
        <p className="record-comment">
          {t("意见：{comment}", { comment: record.comment })}
        </p>
      )}
      {record.notes.length > 0 && (
        <ul className="record-notes">
          {record.notes.map((note) => (
            <li key={note.item}>
              <strong>{note.label}</strong>：{note.text}
            </li>
          ))}
        </ul>
      )}
      {record.seen && (
        <p className="muted record-seen">
          {t("确认时所见内容的指纹：")}
          <code>{record.seen}</code>
        </p>
      )}
    </section>
  );
}
