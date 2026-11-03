"use client";

import { useT } from "../lib/i18n/client";
import { useState } from "react";
import { CitationScope, CitedMarkdown, SourceList } from "./citations";
import { AnswerCard } from "./etl-answer";
import { BriefView, RecordView, TechPlanView } from "./etl-brief";
import { FileBrowser } from "./generated-files";
import { Icon } from "./icons";
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
  const t = useT();
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
  // The run report, then the data checks' report when the template checks the data.
  const report = [result.report, result.checks]
    .filter((text): text is string => typeof text === "string" && text !== "")
    .join("\n\n");
  const reason = typeof result.reason === "string" ? result.reason : "";
  return (
    <section
      className="panel etl-review etl-result"
      aria-labelledby="etl-result-title"
    >
      <div className="section-heading">
        <h2 id="etl-result-title">
          <Icon name={stopped ? "alert" : "check"} />
          {stopped ? t("已终止") : t("运行结果")}
        </h2>
        {!stopped && (
          <span className="app-tag">{t("开发环境试算 · 尚未上线")}</span>
        )}
      </div>
      {stopped && (
        <div className="review-returned" role="note">
          <h3>{t("这次运行在审核时终止了")}</h3>
          <p>{t("原因：{reason}", { reason: reason || t("没有写明") })}</p>
          <p className="muted">
            {t(
              "没通过审核的数字和代码不作为结果交付，代码仍可以下载 ZIP 查看。",
            )}
          </p>
        </div>
      )}
      <div
        className="filter-tabs review-tabs"
        role="tablist"
        aria-label={t("运行结果")}
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
            {t(tabLabels[key])}
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
              heading={
                reviewed?.actor
                  ? t("开发环境试算 · 已由 {actor} 审核 · 尚未上线", {
                      actor: reviewed.actor,
                    })
                  : t("开发环境试算 · 已审核 · 尚未上线")
              }
              file={
                answer.target.split(".").pop()?.replaceAll("`", "") || "answer"
              }
            />
          ) : (
            <p className="muted">{t("这次方案没有设计答案。")}</p>
          ))}
        {tab === "plan" && (
          <CitationScope sources={plan?.references ?? []}>
            {brief && <BriefView brief={brief} uncited={[]} />}
            {confirmed && (
              <RecordView record={confirmed} title={t("确认方案的记录")} />
            )}
            {reviewed && (
              <RecordView record={reviewed} title={t("审核试算结果的记录")} />
            )}
            {plan && (
              <details className="result-tech-plan">
                <summary>{t("技术方案")}</summary>
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
                {t("{n} 个文件", { n: Object.keys(files).length })}
              </p>
              {Object.keys(files).length > 0 && (
                <a
                  className="button secondary"
                  href={`/api/runs/${encodeURIComponent(run.run_id)}/files.zip`}
                  download
                >
                  <Icon name="download" size={15} />

                  {t("下载 ZIP")}
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
            <p className="muted">{t("这次运行没有运行报告。")}</p>
          ))}
        {tab === "developer" && (
          <div className="result-developer">
            <dl className="plan-facts">
              <div>
                <dt>{t("运行编号")}</dt>
                <dd className="monospace">{run.run_id}</dd>
              </div>
              {traceUrl && (
                <div>
                  <dt>{t("追踪")}</dt>
                  <dd>
                    <a
                      href={traceUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {t("在 Langfuse 中查看")}
                    </a>
                  </dd>
                </div>
              )}
            </dl>
            <details className="developer-view" open>
              <summary>{t("原始结果")}</summary>
              <pre className="result-json">
                {JSON.stringify(result, null, 2)}
              </pre>
            </details>
          </div>
        )}
      </div>
      {/* Mounted whatever the tab: the report and the run's conversation cite them. */}
      <SourceList title={t("全部检索结果")} />
    </section>
  );
}
