"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  durationText,
  errorMessage,
  formatTime,
  readRunList,
  requestJson,
  runTitle,
  statusLabels,
  stepText,
  timeAgo,
  versionLabel,
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
  const newButton = (
    <Link className="button primary" href="/runs/new">
      <Icon name="plus" />
      新建运行
    </Link>
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>运行记录</h1>
          <p>从需求到结果，每一步都可追踪。</p>
        </div>
        {newButton}
      </div>
      <section className="stats-grid" aria-label="运行概览">
        {[
          { label: "全部运行", count: runs.length, icon: "runs" as const },
          {
            label: "等待确认",
            count: runs.filter((r) => r.status === "waiting").length,
            icon: "alert" as const,
          },
          {
            label: "已完成",
            count: runs.filter((r) => r.status === "done").length,
            icon: "check" as const,
          },
        ].map((stat) => (
          <div className="stat-card" key={stat.label}>
            <div>
              <span>{stat.label}</span>
              <strong>
                {loading || error
                  ? "—"
                  : stat.count.toString().padStart(2, "0")}
              </strong>
            </div>
            <span className="stat-icon">
              <Icon name={stat.icon} size={21} />
            </span>
          </div>
        ))}
      </section>
      <section className="panel runs-panel" aria-label="运行列表">
        <div className="panel-toolbar">
          <div className="filter-tabs" role="group" aria-label="筛选运行状态">
            {[{ key: "all" as const, label: "全部" }, ...filters].map(
              (item) => (
                <button
                  key={item.key}
                  aria-pressed={filter === item.key}
                  className={filter === item.key ? "selected" : ""}
                  onClick={() => setFilter(item.key)}
                >
                  {item.label}
                </button>
              ),
            )}
          </div>
          <button
            className="icon-button"
            aria-label="刷新运行列表"
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
            正在加载运行记录…
          </div>
        ) : visible.length === 0 ? (
          <EmptyState
            title={
              runs.length === 0 ? "从一个需求开始" : "这个状态下还没有运行"
            }
            action={runs.length === 0 ? newButton : undefined}
          >
            {runs.length === 0
              ? "选择一个应用，描述你的需求，开启第一次运行。"
              : "切换其他状态，或发起一条新的运行。"}
          </EmptyState>
        ) : (
          <div className="table-scroll">
            <table className="runs-table">
              <thead>
                <tr>
                  <th scope="col">需求</th>
                  <th scope="col">提出人</th>
                  <th scope="col">应用</th>
                  <th scope="col">状态</th>
                  <th scope="col">提交时间</th>
                  <th scope="col">用时</th>
                  <th scope="col">
                    <span className="sr-only">操作</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((run) => {
                  const title = runTitle(run);
                  const where = stepText(run);
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
                              （{run.created_by ? `${run.created_by}，` : ""}
                              {formatTime(run.created_at)} 提交）
                            </span>
                          )}
                        </Link>
                      </td>
                      <td className="run-cell">{run.created_by || "—"}</td>
                      <td>
                        <span className="app-tag">
                          <Icon name="spark" size={14} />
                          {run.app_title || run.app}
                        </span>
                        {versionLabel(run) && (
                          <span
                            className={run.draft ? "draft-tag" : "run-version"}
                          >
                            {versionLabel(run)}
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
                            title={formatTime(run.created_at)}
                          >
                            {timeAgo(run.created_at, now)}
                          </time>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="run-cell run-duration">
                        {(durationText(run, now) || "—")
                          .split(" · ")
                          .map((part) => (
                            <span key={part}>{part}</span>
                          ))}
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
        <div className="table-footer">
          <span>
            {error
              ? "加载失败"
              : loading
                ? "正在同步"
                : `共 ${visible.length} 条运行`}
          </span>
          <span>运行过程与结果集中管理</span>
        </div>
      </section>
      <div className="list-note">
        <Icon name="spark" size={16} />
        需要你确认的运行会暂停等待，确认后继续执行。
      </div>
    </>
  );
}
