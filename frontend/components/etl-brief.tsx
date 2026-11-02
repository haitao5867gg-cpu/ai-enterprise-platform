"use client";

import { CitedText, SourceList } from "./citations";
import { NoteButton } from "./review-notes";
import { formatTime } from "../lib/runs";
import {
  checkKinds,
  checkSeverities,
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
  ["confirmed", "已经确认过"],
  ["assumption", "假设"],
];
const UNCITED = "没找到口径文档，按需求理解";
// A text as a name, without its [n].
export const uncite = (text: string) => text.replace(/\s*\[\d+\]/g, "");

function Uncited({ shown }: { shown: boolean }) {
  return shown ? <span className="uncited-tag">{UNCITED}</span> : null;
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
  const missing = new Set(uncited);
  return (
    <section className="etl-brief" aria-labelledby="brief-title">
      <h3 id="brief-title">我们理解你要的是</h3>
      <p className="brief-goal">
        <CitedText>{brief.goal}</CitedText>
      </p>
      {brief.grain && (
        <p className="brief-grain">
          <span>粒度</span>
          <CitedText>{brief.grain}</CitedText>
        </p>
      )}
      <h4>指标</h4>
      {brief.metrics.length ? (
        <div className="table-scroll">
          <table className="mapping-table brief-table" aria-label="指标">
            <thead>
              <tr>
                <th scope="col">名称</th>
                <th scope="col">单位</th>
                <th scope="col">怎么算</th>
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
        <p className="muted">方案里没有指标。</p>
      )}
      {brief.dimensions.length > 0 && (
        <>
          <h4>维度</h4>
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
                    例如：{dimension.examples.join("、")}
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
      <h4>筛选</h4>
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
        <p className="muted">不筛选，用全部数据。</p>
      )}
      <h4>时间范围</h4>
      <p>
        {brief.time_range ? (
          <CitedText>{brief.time_range.text}</CitedText>
        ) : (
          <span className="muted">需求没限定时间范围。</span>
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
  const examples = (column: string) =>
    brief.dimensions.find((dimension) => dimension.column === column)
      ?.examples ?? [];
  return (
    <section className="etl-answer-shape" aria-labelledby="answer-shape-title">
      <h3 id="answer-shape-title">最后给你的答案</h3>
      <p>{design.text}</p>
      <h4>结果表长这样</h4>
      <div className="table-scroll">
        <table className="mapping-table" aria-label="结果表长这样">
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
                    <span className="muted">运行后得出</span>
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
function loadLabel(target: PlanTarget): string {
  const load = target.load;
  switch (load.mode) {
    case "incremental":
      return `增量加载 · ${load.key ?? "未设增量键"}`;
    case "overwrite":
      return "整表覆盖";
    case "replace_where":
      return `按条件替换 · ${load.where ?? ""}`;
    case "append":
      return "追加";
    case "merge":
      return `按键合并 · ${(load.keys.length ? load.keys : target.primary_key).join("、")}`;
    default:
      return "全量加载";
  }
}

export function TechPlanView({
  plan,
  columnNotes = false,
  qualityNotes = false,
  ruleNotes = false,
  checkNotes = false,
}: {
  plan: PlanV2;
  columnNotes?: boolean;
  qualityNotes?: boolean;
  ruleNotes?: boolean;
  checkNotes?: boolean;
}) {
  return (
    <section className="etl-tech-plan" aria-labelledby="tech-plan-title">
      <h3 id="tech-plan-title">技术方案</h3>
      {plan.summary && (
        <p className="target-description">
          <CitedText>{plan.summary}</CitedText>
        </p>
      )}
      <div className="plan-targets">
        {plan.targets.map((target) => (
          <article
            key={target.name}
            className="plan-target"
            aria-label={`目标表 ${target.name}`}
          >
            <div className="target-heading">
              <h4>
                <code>{target.name}</code>
              </h4>
              <span className="load-badge">{loadLabel(target)}</span>
            </div>
            {target.description && (
              <p className="target-description">
                <CitedText>{target.description}</CitedText>
              </p>
            )}
            <dl className="plan-facts">
              {target.sources.length > 0 && (
                <div>
                  <dt>来源</dt>
                  <dd className="source-tags">
                    {target.sources.map((source) => (
                      <code key={source}>{source}</code>
                    ))}
                  </dd>
                </div>
              )}
              {target.logic && (
                <div>
                  <dt>做法</dt>
                  <dd>
                    <CitedText>{target.logic}</CitedText>
                  </dd>
                </div>
              )}
              <div>
                <dt>主键</dt>
                <dd>
                  <code>{target.primary_key.join(", ") || "未设置"}</code>
                </dd>
              </div>
              {target.load.add_columns.length > 0 && (
                <div>
                  <dt>加列</dt>
                  <dd>
                    表已经存在，确认方案后在表里加{" "}
                    {target.load.add_columns.length} 列：
                    {target.load.add_columns.map((column, i) => (
                      <span key={column.name}>
                        {i > 0 && "；"}
                        <code>{column.name}</code>
                        {column.after ? (
                          <>
                            （放在 <code>{column.after}</code> 后面）
                          </>
                        ) : (
                          "（放在最前面）"
                        )}
                      </span>
                    ))}
                  </dd>
                </div>
              )}
            </dl>
            <div className="table-scroll">
              <table
                className="mapping-table"
                aria-label={`${target.name} 的列`}
              >
                <thead>
                  <tr>
                    <th scope="col">中文名</th>
                    <th scope="col">列名</th>
                    <th scope="col">Spark 表达式</th>
                    <th scope="col">说明</th>
                  </tr>
                </thead>
                <tbody>
                  {target.columns.map((column) => (
                    <tr key={column.name}>
                      <td>{column.label || "—"}</td>
                      <td className="column-name">
                        <code>{column.name}</code>
                        {target.load.add_columns.some(
                          (added) => added.name === column.name,
                        ) && <span className="column-added">新加</span>}
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
            <h5>质量规则</h5>
            {target.quality_rules.length ? (
              <ul className="quality-rules">
                {target.quality_rules.map((rule) => (
                  <li key={rule.name}>
                    <div>
                      <code>{rule.name}</code>
                      <span>
                        不满足时：
                        {violationLabels[rule.on_violation] ??
                          rule.on_violation}
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
              <p className="muted">没有质量规则。</p>
            )}
          </article>
        ))}
      </div>
      {plan.checks.length > 0 && (
        <section className="plan-checks" aria-label="建议的数据检查">
          <h4>数据检查</h4>
          <p className="muted">
            开发环境试算之后检查；填了老系统结果表时另加一条逐行对比。
          </p>
          <ul className="quality-rules">
            {plan.checks.map((check) => (
              <li key={check.id}>
                <div>
                  <strong>{check.label}</strong>
                  <span>
                    {checkKinds[check.kind] ?? check.kind}
                    {check.severity === "error"
                      ? " · 错误：不通过时结果不能直接通过"
                      : ` · ${checkSeverities[check.severity] ?? check.severity}`}
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
          <summary>已按口径处理（{plan.rules.length} 条）</summary>
          {basisLabels.map(([basis, label]) => {
            const rules = plan.rules.filter(
              (rule) =>
                rule.basis === basis ||
                (basis === "assumption" &&
                  !basisLabels.some(([known]) => known === rule.basis)),
            );
            return rules.length ? (
              <div key={basis}>
                <h5>{label}</h5>
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
      <SourceList title="依据的资料" />
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
  return (
    <section className="etl-record" aria-label={title}>
      <h3>{title}</h3>
      <p>
        {record.actor ?? "未署名"}
        {record.at ? ` · ${formatTime(record.at)}` : ""} · 第 {record.round} 轮
        · 选了「{record.decision_label}」
        {record.submitted && record.submitted !== record.decision
          ? "（点的是另一个选项，因为改了答案或写了意见）"
          : ""}
      </p>
      {record.answers.length > 0 && (
        <ul className="record-answers">
          {record.answers.map((answer) => (
            <li key={answer.key}>
              <span>{answer.label}</span>
              <strong>{answer.value_label}</strong>
              {answer.picked_suggested === true && (
                <span className="suggested-tag">选了建议</span>
              )}
              {answer.picked_suggested === false && (
                <span className="changed-tag">
                  改选（建议是「{answer.suggested_label}」）
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {record.comment && (
        <p className="record-comment">意见：{record.comment}</p>
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
          确认时所见内容的指纹：<code>{record.seen}</code>
        </p>
      )}
    </section>
  );
}
