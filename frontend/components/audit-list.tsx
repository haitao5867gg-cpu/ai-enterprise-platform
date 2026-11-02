"use client";

import { useEffect, useState } from "react";
import { listAudit, type AuditEvent } from "../lib/auth";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { ErrorNotice } from "./ui";

const time = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
const target = (event: AuditEvent) =>
  Object.entries(event.target)
    .map(([key, value]) => `${key}: ${value}`)
    .join("，") || "—";
const outcome = (status: number) =>
  status < 400
    ? { text: "成功", tone: "ok" }
    : status === 401 || status === 403
      ? { text: "被拒绝", tone: "error" }
      : { text: `没成功（${status}）`, tone: "error" };

export function AuditList() {
  const [user, setUser] = useState("");
  const [q, setQ] = useState("");
  const [filters, setFilters] = useState({ user: "", q: "" });
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    listAudit(filters, controller.signal)
      .then((page) => {
        setEvents(page.events);
        setNext(page.next);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [filters, revision]);

  async function more() {
    if (next === null || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await listAudit({ ...filters, before: next });
      setEvents((old) => [...(old ?? []), ...page.events]);
      setNext(page.next);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <div className="asset-page">
      <AssetHeading title="操作记录">
        谁在什么时候改了什么：新建和修改应用、发布、审批、管理账号和模型，以及登录。只记接口和对象，不记提交的内容。保留一年。
      </AssetHeading>
      <form
        className="audit-filters"
        onSubmit={(event) => {
          event.preventDefault();
          setEvents(null);
          setFilters({ user: user.trim(), q: q.trim() });
        }}
      >
        <label className="asset-field">
          登录名
          <input
            value={user}
            placeholder="全部"
            autoCapitalize="none"
            spellCheck={false}
            onChange={(event) => setUser(event.target.value)}
          />
        </label>
        <label className="asset-field">
          操作
          <input
            value={q}
            placeholder="例如：发布、登录、/apps"
            onChange={(event) => setQ(event.target.value)}
          />
        </label>
        <button className="button secondary">筛选</button>
      </form>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!events && !error && <AssetLoading />}
      {events && (
        <section className="panel asset-section datasource-section">
          {events.length === 0 ? (
            <p className="muted">没有符合条件的记录。</p>
          ) : (
            <div className="table-scroll">
              <table
                className="mapping-table audit-table"
                aria-label="操作记录"
              >
                <thead>
                  <tr>
                    {["时间", "谁", "操作", "对象", "结果", "IP"].map((h) => (
                      <th scope="col" key={h}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {events.map((event) => {
                    const result = outcome(event.status);
                    return (
                      <tr key={event.id}>
                        <td className="muted">{time(event.at)}</td>
                        <td>
                          {event.user ? (
                            <span className="model-name-stack">
                              <strong>{event.user.display_name}</strong>
                              <code>{event.user.username}</code>
                            </span>
                          ) : (
                            <span className="muted">
                              {event.action === "login_failed"
                                ? "未登录"
                                : "本地用户"}
                            </span>
                          )}
                        </td>
                        <td>
                          <span className="model-name-stack">
                            <span>{event.label}</span>
                            {event.label !== event.action && (
                              <code className="muted">{event.action}</code>
                            )}
                          </span>
                        </td>
                        <td>{target(event)}</td>
                        <td>
                          <span className={`datasource-health ${result.tone}`}>
                            {result.text}
                          </span>
                        </td>
                        <td className="muted">{event.ip ?? "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {next !== null && (
            <button
              type="button"
              className="button secondary audit-more"
              aria-disabled={loadingMore || undefined}
              onClick={() => void more()}
            >
              {loadingMore ? "正在加载…" : "加载更早的记录"}
            </button>
          )}
        </section>
      )}
    </div>
  );
}
