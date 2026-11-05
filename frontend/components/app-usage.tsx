"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  getUsage,
  RUN_ENTRIES,
  runEntryLabels,
  type AppUsage,
} from "../lib/app-entries";
import { useT } from "../lib/i18n/client";
import { PERIODS, percent, successRate, usd, type Period } from "../lib/ops";
import { errorMessage } from "../lib/runs";
import { Bars, Card } from "./ops-dashboard";
import { useMay } from "./session";
import { ErrorNotice } from "./ui";

const TOOL_NAMES: Record<string, string> = {
  search_knowledge: "检索知识库",
  call_app: "调用应用",
};

// How the app is used, on its 概览 tab (docs/api/ops.md, "应用的使用情况"): the
// dashboard's figures for this one app, plus its ways in, conversations, answers,
// feedback, and the tools and apps it called. Builders only.
export function AppUsagePanel({ app }: { app: string }) {
  const t = useT();
  const mayBuild = useMay("builder");
  const [days, setDays] = useState<Period>(30);
  const [usage, setUsage] = useState<AppUsage | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!mayBuild) return;
    const controller = new AbortController();
    getUsage(app, days, controller.signal)
      .then((value) => {
        setUsage(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app, days, mayBuild, revision]);
  if (!mayBuild) return null;
  const totals = usage?.totals;
  const answers = usage?.answers;
  const asked = answers ? answers.answered + answers.ungrounded : 0;
  return (
    <section
      className="panel asset-section app-usage"
      aria-labelledby="app-usage-title"
    >
      <div className="asset-section-heading">
        <h2 id="app-usage-title">{t("使用情况")}</h2>
        <div className="filter-tabs" role="group" aria-label={t("统计期")}>
          {PERIODS.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={days === value}
              className={days === value ? "selected" : ""}
              onClick={() => setDays(value)}
            >
              {t("近 {n} 天", { n: value })}
            </button>
          ))}
        </div>
      </div>
      <p className="muted">
        {t("统计期内新建的运行，草稿的试跑和评测不算。")}
        {usage?.truncated && t("运行太多，只看了最近的 2000 次。")}
      </p>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!usage && !error && (
        <p className="muted" role="status">
          {t("正在读取…")}
        </p>
      )}
      {usage && totals && (
        <>
          <div className="ops-cards" role="group" aria-label={t("总览")}>
            <Card
              label={t("运行数")}
              value={String(totals.runs)}
              note={t("成功 {done}，失败 {error}", {
                done: totals.done,
                error: totals.error,
              })}
            />
            <Card
              label={t("成功率")}
              value={percent(successRate(totals))}
            />
            <Card
              label={t("模型费用")}
              value={usd(totals.model_cost_usd)}
              note={t("{n} 次模型调用", { n: totals.model_calls })}
            />
            {usage.conversations && (
              <Card
                label={t("会话数")}
                value={String(usage.conversations.count)}
                note={
                  usage.conversations.turns_avg === null
                    ? undefined
                    : t("平均每个会话 {n} 轮", {
                        n: usage.conversations.turns_avg,
                      })
                }
              />
            )}
            {answers && (
              <Card
                label={t("没找到依据")}
                value={String(answers.ungrounded)}
                note={
                  asked
                    ? t("占回答的 {rate}", {
                        rate: percent(answers.ungrounded / asked),
                      })
                    : t("还没有回答")
                }
              />
            )}
            <Card
              label={t("点赞 / 点踩")}
              value={`${usage.feedback.good} / ${usage.feedback.bad}`}
            />
          </div>
          {totals.runs > 0 && (
            <Bars
              title={t("每天的运行数")}
              days={usage.daily}
              value={(day) => day.runs}
              format={(day) =>
                t("{runs} 次运行，成功 {done}，失败 {error}", {
                  runs: day.runs,
                  done: day.done,
                  error: day.error,
                })
              }
            />
          )}
          <div className="usage-tables">
            <table className="mapping-table">
              <caption>{t("从哪个入口来")}</caption>
              <tbody>
                {RUN_ENTRIES.map((entry) => (
                  <tr key={entry}>
                    <th scope="row">{t(runEntryLabels[entry])}</th>
                    <td>{usage.entries[entry]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <table className="mapping-table">
              <caption>{t("用得最多的工具")}</caption>
              {usage.tools.length ? (
                <>
                  <thead>
                    <tr>
                      <th scope="col">{t("工具")}</th>
                      <th scope="col">{t("次数")}</th>
                      <th scope="col">{t("成功率")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.tools.map((tool) => (
                      <tr key={`${tool.tool}/${tool.datasource}/${tool.app}`}>
                        <td>
                          {tool.app
                            ? t("调用应用 {app}", { app: tool.app })
                            : TOOL_NAMES[tool.tool]
                              ? t(TOOL_NAMES[tool.tool])
                              : tool.tool}
                        </td>
                        <td>{tool.calls}</td>
                        <td>
                          {percent((tool.calls - tool.errors) / tool.calls)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </>
              ) : (
                <tbody>
                  <tr>
                    <td className="muted">{t("这段时间没有调用工具。")}</td>
                  </tr>
                </tbody>
              )}
            </table>
            <table className="mapping-table">
              <caption>{t("调用过的应用")}</caption>
              {usage.apps.length ? (
                <>
                  <thead>
                    <tr>
                      <th scope="col">{t("应用")}</th>
                      <th scope="col">{t("次数")}</th>
                      <th scope="col">{t("成功率")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.apps.map((called) => (
                      <tr key={called.app}>
                        <td>
                          <Link href={`/apps/${encodeURIComponent(called.app)}`}>
                            {called.title}
                          </Link>
                        </td>
                        <td>{called.calls}</td>
                        <td>
                          {percent(
                            (called.calls - called.errors) / called.calls,
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </>
              ) : (
                <tbody>
                  <tr>
                    <td className="muted">{t("这段时间没有调用别的应用。")}</td>
                  </tr>
                </tbody>
              )}
            </table>
          </div>
        </>
      )}
    </section>
  );
}
