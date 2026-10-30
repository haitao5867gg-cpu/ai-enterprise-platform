"use client";

import { useState } from "react";
import { CitationScope, CitedMarkdown, SourceList } from "./citations";
import { AnswerCard } from "./etl-answer";
import { BriefView, RecordView, TechPlanView } from "./etl-brief";
import { FileBrowser } from "./generated-files";
import { Icon } from "./icons";
import { RunFlow } from "./run-flow";
import {
  isFileMap,
  readAnswer,
  readBrief,
  readPlanV2,
  readRecord,
} from "../lib/etl-v2";
import { isRecord, type Run } from "../lib/runs";

const tabLabels = {
  answer: "答案",
  plan: "方案",
  code: "代码",
  report: "运行报告",
  developer: "开发者视图",
} as const;
type Tab = keyof typeof tabLabels;

// What the ETL template's second version delivers (docs/api/data-nodes.md, "结果页"): the
// answer first, then the plan with how it was confirmed and reviewed, the code, the run
// report and the developer view. A stopped run has no answer and no code.
export function EtlResultView({
  run,
  result,
  traceUrl,
}: {
  run: Run;
  result: Record<string, unknown>;
  traceUrl: string | null;
}) {
  const stopped = run.outcome === "stopped";
  const tabs: Tab[] = stopped
    ? ["plan", "report", "developer"]
    : ["answer", "plan", "code", "report", "developer"];
  const [tab, setTab] = useState<Tab>(tabs[0]);
  const brief = readBrief(result.brief);
  const plan = readPlanV2(result.plan);
  const answer = isRecord(result.answer) ? readAnswer(result.answer) : null;
  const confirmed = readRecord(result.confirmed);
  const reviewed = readRecord(result.reviewed);
  const code =
    isRecord(result.code) && isFileMap(result.code.files) ? result.code : null;
  const files = code && isFileMap(code.files) ? code.files : {};
  const report = typeof result.report === "string" ? result.report : "";
  const reason = typeof result.reason === "string" ? result.reason : "";
  return (
    <section
      className="panel etl-review etl-result"
      aria-labelledby="etl-result-title"
    >
      <div className="section-heading">
        <h2 id="etl-result-title">
          <Icon name={stopped ? "alert" : "check"} />
          {stopped ? "已终止" : "运行结果"}
        </h2>
        {!stopped && <span className="app-tag">开发环境试算 · 尚未上线</span>}
      </div>
      {stopped && (
        <div className="review-returned" role="note">
          <h3>这次运行在审核时终止了</h3>
          <p>原因：{reason || "没有写明"}</p>
          <p className="muted">
            没通过审核的数字和代码不作为结果交付，代码仍可以下载 ZIP 查看。
          </p>
        </div>
      )}
      <div
        className="filter-tabs review-tabs"
        role="tablist"
        aria-label="运行结果"
      >
        {tabs.map((key) => (
          <button
            key={key}
            type="button"
            role="tab"
            id={`result-tab-${key}`}
            aria-controls={`result-panel-${key}`}
            aria-selected={tab === key}
            className={tab === key ? "selected" : ""}
            onClick={() => setTab(key)}
          >
            {tabLabels[key]}
          </button>
        ))}
      </div>
      <div
        className="review-body"
        role="tabpanel"
        id={`result-panel-${tab}`}
        aria-labelledby={`result-tab-${tab}`}
      >
        {tab === "answer" &&
          (answer ? (
            <AnswerCard
              answer={answer}
              heading={`开发环境试算 · ${
                reviewed?.actor ? `已由 ${reviewed.actor} 审核` : "已审核"
              } · 尚未上线`}
              file={
                answer.target.split(".").pop()?.replaceAll("`", "") || "answer"
              }
            />
          ) : (
            <p className="muted">这次方案没有设计答案。</p>
          ))}
        {tab === "plan" && (
          <CitationScope sources={plan?.references ?? []}>
            {brief && <BriefView brief={brief} uncited={[]} />}
            {confirmed && (
              <RecordView record={confirmed} title="确认方案的记录" />
            )}
            {reviewed && (
              <RecordView record={reviewed} title="审核试算结果的记录" />
            )}
            {plan && (
              <details className="result-tech-plan">
                <summary>技术方案</summary>
                <TechPlanView plan={plan} />
              </details>
            )}
          </CitationScope>
        )}
        {tab === "code" && (
          <div className="review-code">
            <div className="review-code-heading">
              <p>
                {typeof code?.name === "string" ? `${code.name} · ` : ""}
                {Object.keys(files).length} 个文件
              </p>
              {Object.keys(files).length > 0 && (
                <a
                  className="button secondary"
                  href={`/api/runs/${encodeURIComponent(run.run_id)}/files.zip`}
                  download
                >
                  <Icon name="download" size={15} />
                  下载 ZIP
                </a>
              )}
            </div>
            <FileBrowser files={files} />
          </div>
        )}
        {tab === "report" &&
          (report ? (
            <div className="result-text">
              <CitedMarkdown text={report} />
            </div>
          ) : (
            <p className="muted">这次运行没有运行报告。</p>
          ))}
        {tab === "developer" && (
          <div className="result-developer">
            <dl className="plan-facts">
              <div>
                <dt>运行编号</dt>
                <dd className="monospace">{run.run_id}</dd>
              </div>
              {traceUrl && (
                <div>
                  <dt>追踪</dt>
                  <dd>
                    <a
                      href={traceUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      在 Langfuse 中查看
                    </a>
                  </dd>
                </div>
              )}
            </dl>
            <RunFlow
              runId={run.run_id}
              version={`${run.status}:${run.messages.length}`}
              live={false}
            />
            <SourceList title="全部检索结果" />
            <details className="developer-view" open>
              <summary>原始结果</summary>
              <pre className="result-json">
                {JSON.stringify(result, null, 2)}
              </pre>
            </details>
          </div>
        )}
      </div>
    </section>
  );
}
