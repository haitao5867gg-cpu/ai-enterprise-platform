"use client";

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
  returnedForPlan,
  httpsUrl,
  type Answer,
  type AnswerDesign,
  type Brief,
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

export type PlanReview = {
  brief: Brief;
  plan: PlanV2;
  design: AnswerDesign | null;
  uncited: string[];
  cleaned: string[];
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
  const { brief, plan, design, returned } = review;
  return (
    <CitationScope sources={plan.references}>
      <section className="panel etl-review" aria-labelledby="review-title">
        <div className="section-heading">
          <h2 id="review-title">
            <Icon name="grid" />
            {title}
          </h2>
          {round !== null && <span className="app-tag">第 {round} 轮</span>}
        </div>
        {returned && returnedForPlan(returned, review.previous) && (
          <div className="review-returned" role="note">
            <h3>
              <Icon name="alert" size={16} />
              审核要求改方案，请再确认一次
            </h3>
            <p>
              {returned.actor ?? "审核人"}在审核试算结果时选了「
              {returned.decision_label}」，方案已经按意见改过：
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
        <div className="review-body">
          <BriefView brief={brief} uncited={review.uncited} notes />
          {design && <AnswerShape design={design} brief={brief} />}
          {questions.length > 0 && (
            <section
              id="questions"
              className="review-questions"
              aria-labelledby="questions-title"
            >
              <h3 id="questions-title">请回答这些问题</h3>
              <p className="muted">
                每题都要选。标“建议”的是方案现在的做法；改选或写了意见，方案会按你的意见重写，再请你确认一次。
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
                        <span>影响：</span>
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
          <TechPlanView plan={plan} columnNotes ruleNotes />
          {review.cleaned.length > 0 && (
            <details className="review-cleaned">
              <summary>
                平台自动整理过的地方（{review.cleaned.length} 处）
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
          去确认
          <Icon name="arrow" size={14} />
        </a>
      </section>
    </CitationScope>
  );
}

// The code the dev run ran: the files of the last code check's bundle, read from the
// run's steps (the same rule files.zip follows).
function CodeTab({ runId, files }: { runId: string; files: string[] }) {
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
        setError("这次运行的步骤里没有找到代码");
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
          {files.length} 个文件
          {files.length > 0 && "，意见可以挂在打开的文件上"}
        </p>
        <a
          className="button secondary"
          href={`/api/runs/${encodeURIComponent(runId)}/files.zip`}
          download
        >
          <Icon name="download" size={15} />
          下载 ZIP
        </a>
      </div>
      {bundle ? (
        <FileBrowser
          files={bundle}
          toolbar={(path) => (
            <>
              <CopyButton text={bundle[path]} label="复制" />
              <NoteButton item={`file:${path}`} label={path} />
            </>
          )}
        />
      ) : (
        <>
          {error ? (
            <p className="muted" role="status">
              代码内容取不到：{error}。可以下载 ZIP 查看。
            </p>
          ) : (
            <p className="muted" role="status">
              正在读取代码…
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
  return (
    <div className="table-preview">
      <h5>{preview.table}</h5>
      <p className="muted">
        {preview.target && `${preview.target} · `}前 {preview.rows.length} 行
        {preview.sorted_by === null
          ? ""
          : preview.sorted_by.length
            ? `，按 ${preview.sorted_by.join("、")} 从小到大排`
            : "，没有排序"}
      </p>
      <div className="table-scroll">
        <table className="mapping-table" aria-label={`${preview.table} 预览`}>
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

function ResultTab({ review }: { review: DevReview }) {
  return (
    <div className="review-result">
      {review.status === "failed" ? (
        <div className="checks-failed" role="alert">
          <h3>
            <Icon name="alert" />
            试算没通过
          </h3>
          <p>没通过的试算不能交付。可以改代码、改方案，或者终止。</p>
        </div>
      ) : (
        <p className="checks-passed">
          <Icon name="check" size={16} />
          开发环境试算通过
          {review.url && (
            <a href={review.url} target="_blank" rel="noopener noreferrer">
              在 Databricks 中查看
            </a>
          )}
        </p>
      )}
      {review.tables.length > 0 && (
        <section aria-label="结果表">
          <h4>结果表</h4>
          <ul className="review-tables">
            {review.tables.map((table) => (
              <li key={table.name}>
                <div>
                  <code>{table.name}</code>
                  <span>
                    {table.rows === null
                      ? "行数未知"
                      : `${table.rows.toLocaleString("en-US")} 行`}
                    {table.dropped > 0 &&
                      `，质量规则丢弃 ${table.dropped.toLocaleString("en-US")} 行`}
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
                              ? `${rule.failed.toLocaleString("en-US")} 行不通过${
                                  handlings[
                                    handling(review, table.name, rule.name)
                                  ] ?? ""
                                }`
                              : "全部通过"}
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
      {review.answer && (
        <section aria-label="答案预览">
          <h4>答案预览</h4>
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
        <section aria-label="结果表预览">
          <h4>结果表预览</h4>
          {review.preview.map((preview) => (
            <PreviewTable key={preview.table} preview={preview} />
          ))}
        </section>
      )}
      {review.errors && (
        <section aria-label="报错">
          <h4>报错原文</h4>
          <pre className="review-errors">{review.errors}</pre>
        </section>
      )}
      {review.status === "failed" && review.url && (
        <a href={review.url} target="_blank" rel="noopener noreferrer">
          在 Databricks 中查看这次试算
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
  const [tab, setTab] = useState<(typeof devTabs)[number][0]>("result");
  return (
    <CitationScope sources={review.plan?.references ?? []}>
      <section className="panel etl-review" aria-labelledby="review-title">
        <div className="section-heading">
          <h2 id="review-title">
            <Icon name="grid" />
            {title}
          </h2>
          {round !== null && <span className="app-tag">第 {round} 轮</span>}
        </div>
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
              {label}
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
              <RecordView record={review.confirmed} title="确认方案的记录" />
            ) : (
              <p className="muted">还没有确认方案的记录。</p>
            ))}
        </div>
        <a className="jump-to-decision" href="#approval">
          去审核
          <Icon name="arrow" size={14} />
        </a>
      </section>
    </CitationScope>
  );
}
