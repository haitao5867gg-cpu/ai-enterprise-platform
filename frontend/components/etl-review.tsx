"use client";

import { useT } from "../lib/i18n/client";
import { useEffect, useState } from "react";
import { CitationScope } from "./citations";
import { ApprovalQuestions } from "./approval-questions";
import { AnswerCard, CopyButton } from "./etl-answer";
import {
  AnswerShape,
  BriefView,
  RecordView,
  TechPlanView,
  uncite,
} from "./etl-brief";
import { FileBrowser } from "./generated-files";
import { Icon } from "./icons";
import { NoteButton } from "./review-notes";
import type { FieldValues } from "../lib/input-fields";
import { readFlow } from "../lib/flow";
import {
  isFileMap,
  readAnswer,
  readAnswerDesign,
  readBrief,
  readDevTables,
  readPlanV2,
  readPreviews,
  readQuality,
  readRecord,
  readDataChecks,
  returnedForPlan,
  checkKinds,
  checkColumns,
  checkRow,
  checkSeverities,
  checkStatuses,
  checksPassed,
  httpsUrl,
  type Answer,
  type AnswerDesign,
  type Brief,
  type DataChecks,
  type DevPreview,
  type DevTable,
  type PlanV2,
  type Quality,
  type ReviewRecord,
} from "../lib/etl-v2";
import {
  errorMessage,
  isRecord,
  requestJson,
  type Question,
} from "../lib/runs";

// A preview's value as the dev run read it: null as null, objects as JSON.
const cell = (value: unknown) =>
  value === null
    ? "null"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value ?? "");
const strings = (value: unknown) =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

// A problem a review node (llm_review) found and the model did not fix: the node's
// `findings` (docs/api/flow-apps.md, "llm_review 模型审查").
export type Finding = {
  title: string;
  where: string;
  problem: string;
  fix: string;
  quote: string;
};
const NO_CODE = "这次运行的步骤里没有找到代码";

export function readFindings(value: unknown): Finding[] {
  if (!Array.isArray(value)) return [];
  const text = (item: Record<string, unknown>, key: string) =>
    typeof item[key] === "string" ? (item[key] as string) : "";
  return value.filter(isRecord).flatMap((item) =>
    text(item, "problem")
      ? [
          {
            title: text(item, "title") || text(item, "rule"),
            where: text(item, "where"),
            problem: text(item, "problem"),
            fix: text(item, "fix"),
            quote: text(item, "quote"),
          },
        ]
      : [],
  );
}

// What the model reviewer still found after the model's rewrites, for the person to weigh.
export function ReviewFindings({
  findings,
  what,
}: {
  findings: Finding[];
  what: string;
}) {
  const t = useT();
  if (!findings.length) return null;
  return (
    <div
      className="review-returned"
      role="note"
      aria-label={t("模型审查{what}没改过来的问题", { what })}
    >
      <h3>
        <Icon name="alert" size={16} />
        {t("模型审查{what}时发现的问题，模型重写后还没改过来（{n} 处）", {
          what,
          n: findings.length,
        })}
      </h3>
      <p>{t("请逐条看一下；需要改的话写意见，模型会按意见重写。")}</p>
      <ul>
        {findings.map((item, i) => (
          <li key={i}>
            <strong>{item.title}</strong>
            {item.where && t("（{where}）", { where: item.where })}
            {t("：{problem}", { problem: item.problem })}
            {item.fix && t(" 建议：{fix}", { fix: item.fix })}
            {item.quote && (
              <span className="muted">
                {t("（依据：{quote}）", { quote: item.quote })}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export type PlanReview = {
  brief: Brief;
  plan: PlanV2;
  design: AnswerDesign | null;
  uncited: string[];
  cleaned: string[];
  // What the plan lost after the platform sent a version back (checks, columns).
  dropped: string[];
  // What the model reviewer still found (the template's review_plan); [] without one.
  review: Finding[];
  returned: ReviewRecord | null;
  previous: ReviewRecord | null;
};

// What 确认方案 (view etl_plan_review) was given; null when it is not what the view
// reads, and the step's `show` is shown instead.
export function readPlanReview(data: unknown): PlanReview | null {
  if (!isRecord(data)) return null;
  const brief = readBrief(data.brief);
  const plan = readPlanV2(data.plan);
  if (!brief || !plan) return null;
  return {
    brief,
    plan,
    design: readAnswerDesign(data.answer),
    uncited: strings(data.uncited),
    cleaned: strings(data.cleaned),
    dropped: strings(data.dropped),
    review: readFindings(data.review),
    returned: readRecord(data.returned),
    previous: readRecord(data.previous),
  };
}

export type DevReview = {
  brief: Brief | null;
  plan: PlanV2 | null;
  uncited: string[];
  confirmed: ReviewRecord | null;
  status: "passed" | "failed";
  tables: DevTable[];
  preview: DevPreview[];
  answer: Answer | null;
  quality: Quality | null;
  url: string | null;
  errors: string;
  files: string[];
  // The data checks after the dev run; null for a template without them.
  checks: DataChecks | null;
  // What the model reviewer still found in the code (review_code); [] without one.
  codeReview: Finding[];
};

// What 审核试算结果 (view etl_dev_review) was given; null when it is not what the view reads.
export function readDevReview(data: unknown): DevReview | null {
  if (!isRecord(data) || (data.status !== "passed" && data.status !== "failed"))
    return null;
  return {
    brief: readBrief(data.brief),
    plan: readPlanV2(data.plan),
    uncited: strings(data.uncited),
    confirmed: readRecord(data.confirmed),
    status: data.status,
    tables: readDevTables(data.tables),
    preview: readPreviews(data.preview),
    answer: readAnswer(data.answer),
    quality: readQuality(data.quality),
    url: httpsUrl(data.url),
    errors: typeof data.errors === "string" ? data.errors : "",
    files: strings(data.files),
    checks: readDataChecks(data.checks, data.checks_summary),
    codeReview: readFindings(data.code_review),
  };
}

// The names of what a question affects: metrics, dimensions, filters and rules by id.
function affected(review: PlanReview, ids: string[]): string[] {
  const names = new Map<string, string>([
    ...review.brief.metrics.map((item) => [item.id, item.name] as const),
    ...review.brief.dimensions.map((item) => [item.id, item.name] as const),
    ...review.brief.filters.map((item) => [item.id, item.text] as const),
    ...review.plan.rules.map((item) => [item.id, item.text] as const),
  ]);
  return ids.map((id) => uncite(names.get(id) ?? id));
}

export function EtlPlanReview({
  review,
  title,
  round,
  questions,
  values,
  onChange,
  errors,
  disabled,
}: {
  review: PlanReview;
  title: string;
  round: number | null;
  questions: Question[];
  values: FieldValues;
  onChange: (values: FieldValues) => void;
  errors: Record<string, string>;
  disabled: boolean;
}) {
  const t = useT();
  const { brief, plan, design, returned } = review;
  return (
    <CitationScope sources={plan.references}>
      <section className="panel etl-review" aria-labelledby="review-title">
        <div className="section-heading">
          <h2 id="review-title">
            <Icon name="grid" />
            {title}
          </h2>
          {round !== null && (
            <span className="app-tag">{t("第 {round} 轮", { round })}</span>
          )}
        </div>
        {returned && returnedForPlan(returned, review.previous) && (
          <div className="review-returned" role="note">
            <h3>
              <Icon name="alert" size={16} />

              {t("审核要求改方案，请再确认一次")}
            </h3>
            <p>
              {t(
                "{actor}在审核试算结果时选了「{decision}」，方案已经按意见改过：",
                {
                  actor: returned.actor ?? t("审核人"),
                  decision: returned.decision_label,
                },
              )}
            </p>
            <ul>
              {returned.comment && <li>{returned.comment}</li>}
              {returned.notes.map((note) => (
                <li key={note.item}>
                  {note.label}：{note.text}
                </li>
              ))}
            </ul>
          </div>
        )}
        {review.dropped.length > 0 && (
          <div
            className="review-returned"
            role="note"
            aria-label={t("校验退回后改动的内容")}
          >
            <h3>
              <Icon name="alert" size={16} />

              {t("方案被平台校验退回后，这一版和退回的那版不一样")}
            </h3>
            <p>
              {t(
                "请对照需求确认还要不要；要的话写意见，让模型换一种平台支持的做法。",
              )}
            </p>
            <ul>
              {review.dropped.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        )}
        <ReviewFindings findings={review.review} what={t("方案")} />
        <div className="review-body">
          <BriefView brief={brief} uncited={review.uncited} notes />
          {design && <AnswerShape design={design} brief={brief} />}
          {questions.length > 0 && (
            <section
              id="questions"
              className="review-questions"
              aria-labelledby="questions-title"
            >
              <h3 id="questions-title">{t("请回答这些问题")}</h3>
              <p className="muted">
                {t(
                  "每题都要选。标“建议”的是方案现在的做法；改选或写了意见，方案会按你的意见重写，再请你确认一次。",
                )}
              </p>
              <ApprovalQuestions
                questions={questions}
                values={values}
                onChange={onChange}
                errors={errors}
                disabled={disabled}
                aside={(question) => (
                  <>
                    {question.affects?.length ? (
                      <p className="question-why">
                        <span>{t("影响：")}</span>
                        {affected(review, question.affects).join("、")}
                      </p>
                    ) : null}
                    <NoteButton
                      item={`question:${question.key}`}
                      label={question.label}
                    />
                  </>
                )}
              />
            </section>
          )}
          <TechPlanView plan={plan} columnNotes ruleNotes checkNotes />
          {review.cleaned.length > 0 && (
            <details className="review-cleaned">
              <summary>
                {t("平台自动整理过的地方（{n} 处）", {
                  n: review.cleaned.length,
                })}
              </summary>
              <ul>
                {review.cleaned.map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
        <a className="jump-to-decision" href="#approval">
          {t("去确认")}
          <Icon name="arrow" size={14} />
        </a>
      </section>
    </CitationScope>
  );
}

// The code the dev run ran: the files of the last code check's bundle, read from the
// run's steps (the same rule files.zip follows).
function CodeTab({ runId, files }: { runId: string; files: string[] }) {
  const t = useT();
  const [bundle, setBundle] = useState<Record<string, string> | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    requestJson(`/runs/${encodeURIComponent(runId)}/flow`, {
      signal: controller.signal,
    })
      .then((value) => {
        const flow = readFlow(value);
        const checks = new Set(
          flow.nodes
            .filter((node) => node.type === "etl_code_check")
            .map((node) => node.id),
        );
        for (const step of [...flow.steps].reverse()) {
          if (!checks.has(step.node) || !isRecord(step.output)) continue;
          const written = step.output.vars;
          const mine = isRecord(written) ? written[step.node] : null;
          const found =
            isRecord(mine) && isRecord(mine.bundle) ? mine.bundle.files : null;
          if (isFileMap(found)) {
            setBundle(found);
            return;
          }
        }
        setError(NO_CODE);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [runId]);
  return (
    <div className="review-code">
      <div className="review-code-heading">
        <p>
          {t("{n} 个文件", { n: files.length })}
          {files.length > 0 && t("，意见可以挂在打开的文件上")}
        </p>
        <a
          className="button secondary"
          href={`/api/runs/${encodeURIComponent(runId)}/files.zip`}
          download
        >
          <Icon name="download" size={15} />

          {t("下载 ZIP")}
        </a>
      </div>
      {bundle ? (
        <FileBrowser
          files={bundle}
          toolbar={(path) => (
            <>
              <CopyButton text={bundle[path]} label={t("复制")} />
              <NoteButton item={`file:${path}`} label={path} />
            </>
          )}
        />
      ) : (
        <>
          {error ? (
            <p className="muted" role="status">
              {t("代码内容取不到：{error}。可以下载 ZIP 查看。", {
                error: error === NO_CODE ? t(NO_CODE) : error,
              })}
            </p>
          ) : (
            <p className="muted" role="status">
              {t("正在读取代码…")}
            </p>
          )}
          <ul className="brief-list">
            {files.map((path) => (
              <li key={path}>
                <code>{path}</code>
                <NoteButton item={`file:${path}`} label={path} />
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function PreviewTable({ preview }: { preview: DevPreview }) {
  const t = useT();
  return (
    <div className="table-preview">
      <h5>{preview.table}</h5>
      <p className="muted">
        {preview.target && `${preview.target} · `}
        {t("前 {n} 行", { n: preview.rows.length })}
        {preview.sorted_by === null
          ? ""
          : preview.sorted_by.length
            ? t("，按 {columns} 从小到大排", {
                columns: preview.sorted_by.join(t("、")),
              })
            : t("，没有排序")}
      </p>
      <div className="table-scroll">
        <table
          className="mapping-table"
          aria-label={t("{table} 预览", { table: preview.table })}
        >
          <thead>
            <tr>
              {preview.columns.map((column) => (
                <th key={column} scope="col">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.rows.map((row, i) => (
              <tr key={i}>
                {preview.columns.map((column) => (
                  <td key={column}>{cell(row[column])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// What the plan does with the rows a quality rule fails.
const handlings: Record<string, string> = {
  drop: "，已丢弃",
  warn: "，只告警",
  fail: "，整次更新失败",
};
function handling(review: DevReview, table: string, rule: string): string {
  return (
    review.plan?.targets
      .find((target) => target.name === table)
      ?.quality_rules.find((item) => item.name === rule)?.on_violation ?? ""
  );
}

// The data checks after the dev run: each with its result, what it found and a place for
// a note; failed ones show their example rows.
function ChecksSection({ checks }: { checks: DataChecks }) {
  const t = useT();
  return (
    <section aria-label={t("数据检查")}>
      <h4>{t("数据检查")}</h4>
      <p className="muted">{checks.summary}</p>
      {checks.results.length > 0 && (
        <ul className="quality-rules data-checks">
          {checks.results.map((check) => {
            const bad = check.status === "failed" || check.status === "error";
            const rows = bad ? check.rows.map(checkRow) : [];
            const columns = checkColumns(rows, check.focus);
            return (
              <li key={check.id}>
                <div>
                  <strong>{check.label}</strong>
                  <span className={bad ? "rule-failed" : "rule-passed"}>
                    {t(checkStatuses[check.status] ?? check.status)}
                    {bad &&
                      check.severity !== "error" &&
                      t("（{severity}）", {
                        severity: t(
                          checkSeverities[check.severity] ?? check.severity,
                        ),
                      })}
                  </span>
                </div>
                <p className="check-text">
                  {t("{kind}：{message}", {
                    kind: t(checkKinds[check.kind] ?? check.kind),
                    message: check.message,
                  })}
                </p>
                {rows.length > 0 && (
                  <div className="table-scroll">
                    <table
                      className="mapping-table"
                      aria-label={t("{label} 的例子", { label: check.label })}
                    >
                      <thead>
                        <tr>
                          {columns.map((column) => (
                            <th key={column} scope="col">
                              {column}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((row, i) => (
                          <tr key={i}>
                            {columns.map((column) => (
                              <td key={column}>{cell(row[column])}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <NoteButton item={`check:${check.id}`} label={check.label} />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function ResultTab({ review }: { review: DevReview }) {
  const t = useT();
  return (
    <div className="review-result">
      {review.status === "failed" ? (
        <div className="checks-failed" role="alert">
          <h3>
            <Icon name="alert" />

            {t("试算没通过")}
          </h3>
          <p>{t("没通过的试算不能交付。可以改代码、改方案，或者终止。")}</p>
        </div>
      ) : (
        <p className="checks-passed">
          <Icon name="check" size={16} />
          {t("开发环境试算通过")}
          {review.url && (
            <a href={review.url} target="_blank" rel="noopener noreferrer">
              {t("在 Databricks 中查看")}
            </a>
          )}
        </p>
      )}
      {review.status === "passed" &&
        review.checks &&
        !checksPassed(review.checks) && (
          <div className="checks-failed" role="alert">
            <h3>
              <Icon name="alert" />

              {t("数据检查没通过")}
            </h3>
            <p>
              {t(
                "有严重级别为“错误”的检查没通过或出错，不能交付。可以改代码、改方案，或者终止。",
              )}
            </p>
          </div>
        )}
      {review.tables.length > 0 && (
        <section aria-label={t("结果表")}>
          <h4>{t("结果表")}</h4>
          <ul className="review-tables">
            {review.tables.map((table) => (
              <li key={table.name}>
                <div>
                  <code>{table.name}</code>
                  <span>
                    {table.rows === null
                      ? t("行数未知")
                      : t("{n} 行", {
                          n: table.rows.toLocaleString("en-US"),
                        })}
                    {table.dropped > 0 &&
                      t("，质量规则丢弃 {n} 行", {
                        n: table.dropped.toLocaleString("en-US"),
                      })}
                  </span>
                </div>
                {table.expectations.length > 0 && (
                  <ul className="quality-rules">
                    {table.expectations.map((rule) => (
                      <li key={rule.name}>
                        <div>
                          <code>{rule.name}</code>
                          <span
                            className={
                              rule.failed ? "rule-failed" : "rule-passed"
                            }
                          >
                            {rule.failed
                              ? t("{n} 行不通过{handling}", {
                                  n: rule.failed.toLocaleString("en-US"),
                                  handling: t(
                                    handlings[
                                      handling(review, table.name, rule.name)
                                    ] ?? "",
                                  ),
                                })
                              : t("全部通过")}
                          </span>
                        </div>
                        <NoteButton
                          item={`quality:${table.name}.${rule.name}`}
                          label={rule.name}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
          {review.quality && <p className="muted">{review.quality.text}</p>}
        </section>
      )}
      {review.checks && <ChecksSection checks={review.checks} />}
      {review.answer && (
        <section aria-label={t("答案预览")}>
          <h4>{t("答案预览")}</h4>
          <AnswerCard
            answer={review.answer}
            file={
              review.answer.target.split(".").pop()?.replaceAll("`", "") ||
              "answer"
            }
          />
        </section>
      )}
      {review.preview.length > 0 && (
        <section aria-label={t("结果表预览")}>
          <h4>{t("结果表预览")}</h4>
          {review.preview.map((preview) => (
            <PreviewTable key={preview.table} preview={preview} />
          ))}
        </section>
      )}
      {review.errors && (
        <section aria-label={t("报错")}>
          <h4>{t("报错原文")}</h4>
          <pre className="review-errors">{review.errors}</pre>
        </section>
      )}
      {review.status === "failed" && review.url && (
        <a href={review.url} target="_blank" rel="noopener noreferrer">
          {t("在 Databricks 中查看这次试算")}
        </a>
      )}
    </div>
  );
}

const devTabs = [
  ["plan", "口径与方案"],
  ["code", "代码"],
  ["result", "试算结果"],
  ["confirmed", "确认记录"],
] as const;

export function EtlDevReview({
  review,
  title,
  round,
  runId,
}: {
  review: DevReview;
  title: string;
  round: number | null;
  runId: string;
}) {
  const t = useT();
  const [tab, setTab] = useState<(typeof devTabs)[number][0]>("result");
  return (
    <CitationScope sources={review.plan?.references ?? []}>
      <section className="panel etl-review" aria-labelledby="review-title">
        <div className="section-heading">
          <h2 id="review-title">
            <Icon name="grid" />
            {title}
          </h2>
          {round !== null && (
            <span className="app-tag">{t("第 {round} 轮", { round })}</span>
          )}
        </div>
        <ReviewFindings findings={review.codeReview} what={t("代码")} />
        <div
          className="filter-tabs review-tabs"
          role="tablist"
          aria-label={title}
        >
          {devTabs.map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              id={`review-tab-${key}`}
              aria-controls={`review-panel-${key}`}
              aria-selected={tab === key}
              className={tab === key ? "selected" : ""}
              onClick={() => setTab(key)}
            >
              {t(label)}
            </button>
          ))}
        </div>
        <div
          className="review-body"
          role="tabpanel"
          id={`review-panel-${tab}`}
          aria-labelledby={`review-tab-${tab}`}
        >
          {tab === "plan" && (
            <>
              {review.brief && (
                <BriefView brief={review.brief} uncited={review.uncited} />
              )}
              {review.plan && (
                <TechPlanView plan={review.plan} columnNotes qualityNotes />
              )}
            </>
          )}
          {tab === "code" && <CodeTab runId={runId} files={review.files} />}
          {tab === "result" && <ResultTab review={review} />}
          {tab === "confirmed" &&
            (review.confirmed ? (
              <RecordView
                record={review.confirmed}
                title={t("确认方案的记录")}
              />
            ) : (
              <p className="muted">{t("还没有确认方案的记录。")}</p>
            ))}
        </div>
        <a className="jump-to-decision" href="#approval">
          {t("去审核")}
          <Icon name="arrow" size={14} />
        </a>
      </section>
    </CitationScope>
  );
}
