"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useLocale, useT } from "../lib/i18n/client";
import {
  durationParts,
  formatTime,
  runTitle,
  stepText,
  conversationText,
  timeAgo,
  triggerText,
  versionLabel,
} from "../lib/i18n/format";
import {
  errorMessage,
  readRunList,
  requestJson,
  statusLabels,
  type RunStatus,
  type RunSummary,
} from "../lib/runs";
import { Icon } from "./icons";
import { EmptyState, ErrorNotice, StatusBadge } from "./ui";

// While a run is running the list refreshes itself this often.
const REFRESH_MS = 10_000;

const filters: { key: Filter; label: string; statuses: RunStatus[] }[] = [
  { key: "running", label: statusLabels.running, statuses: ["running"] },
  { key: "waiting", label: statusLabels.waiting, statuses: ["waiting"] },
  { key: "done", label: statusLabels.done, statuses: ["done"] },
  { key: "stopped", label: "失败和取消", statuses: ["error", "cancelled"] },
];
type Filter = "all" | "running" | "waiting" | "done" | "stopped";

export function RunList() {
  const t = useT();
  const { locale } = useLocale();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [now, setNow] = useState(0);
  const [filter, setFilter] = useState<Filter>("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let first = true;
    async function load() {
      if (first) {
        setLoading(true);
        setError("");
      }
      let again = false;
      try {
        const data = readRunList(
          await requestJson("/runs", { signal: controller.signal }),
        );
        if (controller.signal.aborted) return;
        setRuns(data);
        setNow(Date.now());
        setError("");
        again = data.some((run) => run.status === "running");
      } catch (err) {
        if (controller.signal.aborted) return;
        // A refresh that fails keeps the list shown and tries again later.
        if (first) setError(errorMessage(err));
        else again = true;
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          first = false;
          if (again) timer = setTimeout(() => void load(), REFRESH_MS);
        }
      }
    }
    void load();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [revision]);
  // Times since and waits keep counting between refreshes.
  useEffect(() => {
    const clock = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(clock);
  }, []);

  const visible =
    filter === "all"
      ? runs
      : runs.filter((run) =>
          filters
            .find((item) => item.key === filter)
            ?.statuses.includes(run.status),
        );
  const counts: Record<Filter, number> = {
    all: runs.length,
    running: 0,
    waiting: 0,
    done: 0,
    stopped: 0,
  };
  for (const run of runs) {
    const key = filters.find((item) => item.statuses.includes(run.status))?.key;
    if (key) counts[key] += 1;
  }
  const newButton = (
    <Link className="button primary" href="/runs/new">
      <Icon name="plus" />
      {t("新建运行")}
    </Link>
  );
  return (
    <>
      <div className="page-heading">
        <h1>{t("运行记录")}</h1>
      </div>
      <section className="panel runs-panel" aria-label={t("运行列表")}>
        <div className="panel-toolbar">
          <div
            className="filter-tabs"
            role="group"
            aria-label={t("筛选运行状态")}
          >
            {[{ key: "all" as const, label: "全部" }, ...filters].map(
              (item) => (
                <button
                  key={item.key}
                  aria-pressed={filter === item.key}
                  className={filter === item.key ? "selected" : ""}
                  onClick={() => setFilter(item.key)}
                >
                  {t(item.label)}
                  {!loading && !error && (
                    // The name stays the filter's; the count is for the eye.
                    <span
                      className={`filter-count${item.key === "waiting" && counts.waiting ? " attention" : ""}`}
                      aria-hidden
                    >
                      {counts[item.key]}
                    </span>
                  )}
                </button>
              ),
            )}
          </div>
          <button
            className="icon-button"
            aria-label={t("刷新运行列表")}
            disabled={loading}
            onClick={() => setRevision((r) => r + 1)}
          >
            <Icon name="refresh" />
          </button>
        </div>
        {error ? (
          <div className="panel-padding">
            <ErrorNotice
              message={error}
              onRetry={() => setRevision((r) => r + 1)}
            />
          </div>
        ) : loading ? (
          <div className="loading-state" role="status">
            <span className="spinner" />
            {t("正在加载运行记录…")}
          </div>
        ) : visible.length === 0 ? (
          <EmptyState
            title={t(
              runs.length === 0 ? "从一个需求开始" : "这个状态下还没有运行",
            )}
            action={runs.length === 0 ? newButton : undefined}
          >
            {t(
              runs.length === 0
                ? "选择一个应用，描述你的需求，开启第一次运行。"
                : "切换其他状态，或发起一条新的运行。",
            )}
          </EmptyState>
        ) : (
          <div className="table-scroll">
            <table className="runs-table">
              <thead>
                <tr>
                  <th scope="col">{t("需求")}</th>
                  <th scope="col">{t("提出人")}</th>
                  <th scope="col">{t("所属应用")}</th>
                  <th scope="col">{t("状态")}</th>
                  <th scope="col">{t("提交时间")}</th>
                  <th scope="col">{t("用时")}</th>
                  <th scope="col">
                    <span className="sr-only">{t("操作")}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((run) => {
                  const title = runTitle(t, run);
                  const where = stepText(t, run);
                  return (
                    <tr key={run.run_id}>
                      <td>
                        <Link
                          className={`run-title${run.title ? "" : " untitled"}`}
                          href={`/runs/${encodeURIComponent(run.run_id)}`}
                          title={title}
                        >
                          {title}
                          {run.created_at && (
                            // Tells runs with the same title apart for screen readers.
                            <span className="sr-only">
                              {t(
                                run.created_by
                                  ? "（{who}，{time} 提交）"
                                  : "（{time} 提交）",
                                {
                                  who: run.created_by ?? "",
                                  time: formatTime(locale, run.created_at),
                                },
                              )}
                            </span>
                          )}
                        </Link>
                        {run.conversation && (
                          <span className="run-step">
                            {conversationText(t, run.conversation)}
                          </span>
                        )}
                      </td>
                      <td className="run-cell">
                        {run.created_by || "—"}
                        {run.trigger && (
                          <span className="run-step">
                            {triggerText(t, run.trigger)}
                          </span>
                        )}
                      </td>
                      <td>
                        <span className="app-tag">
                          <Icon name="spark" size={14} />
                          {run.app_title || run.app}
                        </span>
                        {versionLabel(t, run) && (
                          <span
                            className={run.draft ? "draft-tag" : "run-version"}
                          >
                            {versionLabel(t, run)}
                          </span>
                        )}
                      </td>
                      <td>
                        <StatusBadge run={run} />
                        {where && <span className="run-step">{where}</span>}
                      </td>
                      <td className="run-cell">
                        {run.created_at ? (
                          <time
                            dateTime={run.created_at}
                            title={formatTime(locale, run.created_at)}
                          >
                            {timeAgo(locale, run.created_at, now)}
                          </time>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="run-cell run-duration">
                        {durationParts(t, run, now).length
                          ? durationParts(t, run, now).map((part) => (
                              <span key={part}>{part}</span>
                            ))
                          : "—"}
                      </td>
                      <td>
                        {/* The title links to the run too; this is for the mouse. */}
                        <Link
                          href={`/runs/${encodeURIComponent(run.run_id)}`}
                          className="table-action"
                          aria-hidden="true"
                          tabIndex={-1}
                        >
                          <Icon name="arrow" />
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {!loading && !error && visible.length > 0 && (
          <div className="table-footer">
            <span>{t("共 {n} 条运行", { n: visible.length })}</span>
          </div>
        )}
      </section>
    </>
  );
}
