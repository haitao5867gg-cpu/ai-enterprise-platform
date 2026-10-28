"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listApps, type AppSummary } from "../lib/apps";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { EmptyState, ErrorNotice } from "./ui";
import { AppCreate } from "./app-create";

export function AppList() {
  const [apps, setApps] = useState<AppSummary[] | null>(null);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState<"new" | "import" | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    listApps(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setApps(value);
          setError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [retry]);
  return (
    <div className="asset-page">
      <AssetHeading eyebrow="DESIGN, TEST, DELIVER" title="应用">
        查看工作流设计，调整节点配置，评测后发布新版本。
      </AssetHeading>
      <div className="asset-actions">
        <button
          type="button"
          className="button primary"
          onClick={() => setCreating("new")}
        >
          新建应用
        </button>
        <button
          type="button"
          className="button secondary"
          onClick={() => setCreating("import")}
        >
          导入
        </button>
      </div>
      {creating && (
        <AppCreate
          key={creating}
          apps={apps ?? []}
          importing={creating === "import"}
          close={() => {
            setCreating(null);
            setRetry((value) => value + 1);
          }}
        />
      )}
      {error ? (
        <ErrorNotice
          message={error}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : !apps ? (
        <AssetLoading />
      ) : !apps.length ? (
        <section className="panel">
          <EmptyState title="还没有应用">应用登记后会显示在这里。</EmptyState>
        </section>
      ) : (
        <div className="asset-cards">
          {apps.map((app) => (
            <Link
              className="panel asset-card"
              key={app.app}
              href={`/apps/${encodeURIComponent(app.app)}`}
            >
              <span className="eyebrow">{app.app}</span>
              <h2>{app.title}</h2>
              <p>{app.summary}</p>
              <div className="asset-tags">
                <span>{app.kind === "flow" ? "编排应用" : "代码应用"}</span>
                <span>当前 v{app.current_version}</span>
                <span className={app.has_draft ? "draft-tag" : ""}>
                  {app.has_draft ? "有草稿" : "无草稿"}
                </span>
                <span>{app.has_eval_set ? "需通过评测" : "无评测集"}</span>
              </div>
              <span className="asset-card-link">查看设计与配置 →</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
