"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
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

export function RunList() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [filter, setFilter] = useState<RunStatus | "all">("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      setLoading(true);
      setError("");
      try {
        const data = readRunList(
          await requestJson("/runs", { signal: controller.signal }),
        );
        if (!controller.signal.aborted) setRuns(data);
      } catch (err) {
        if (!controller.signal.aborted) setError(errorMessage(err));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [revision]);

  const visible =
    filter === "all" ? runs : runs.filter((run) => run.status === filter);
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
          <div className="eyebrow">YOUR WORK, IN MOTION</div>
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
            {(["all", "running", "waiting", "done", "error"] as const).map(
              (status) => (
                <button
                  key={status}
                  aria-pressed={filter === status}
                  className={filter === status ? "selected" : ""}
                  onClick={() => setFilter(status)}
                >
                  {status === "all" ? "全部" : statusLabels[status]}
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
                  <th scope="col">运行</th>
                  <th scope="col">应用</th>
                  <th scope="col">状态</th>
                  <th scope="col">
                    <span className="sr-only">操作</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((run) => (
                  <tr key={run.run_id}>
                    <td>
                      <Link
                        className="run-title"
                        href={`/runs/${encodeURIComponent(run.run_id)}`}
                      >
                        {run.messages?.find((m) => m.role === "user")
                          ?.content || `运行 ${run.run_id.slice(0, 8)}`}
                      </Link>
                      <span className="run-id" title={run.run_id}>
                        {run.run_id}
                      </span>
                    </td>
                    <td>
                      <span className="app-tag">
                        <Icon name="spark" size={14} />
                        {run.app}
                      </span>
                    </td>
                    <td>
                      <StatusBadge status={run.status} />
                    </td>
                    <td>
                      <Link
                        href={`/runs/${encodeURIComponent(run.run_id)}`}
                        className="table-action"
                        aria-label={`查看运行 ${run.run_id}`}
                      >
                        <Icon name="arrow" />
                      </Link>
                    </td>
                  </tr>
                ))}
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
