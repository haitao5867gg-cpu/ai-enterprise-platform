"use client";

import { useT } from "../lib/i18n/client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { listApps, type AppSummary } from "../lib/apps";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { EmptyState, ErrorNotice } from "./ui";
import { AppCreate } from "./app-create";

export function AppList() {
  const t = useT();
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
      <AssetHeading
        title={t("应用")}
        action={
          <div className="asset-actions heading-actions">
            <button
              type="button"
              className="button secondary"
              onClick={() => setCreating("import")}
            >
              {t("导入")}
            </button>
            <button
              type="button"
              className="button primary"
              onClick={() => setCreating("new")}
            >
              {t("新建应用")}
            </button>
          </div>
        }
      >
        {t("用应用发起运行；搭建的人在这里改流程、评测后发布新版本。")}
      </AssetHeading>
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
          <EmptyState title={t("还没有应用")}>
            {t("应用登记后会显示在这里。")}
          </EmptyState>
        </section>
      ) : (
        <div className="asset-cards">
          {apps.map((app) => {
            const id = encodeURIComponent(app.app);
            return (
              <article
                className="panel asset-card app-card"
                key={app.app}
                aria-label={app.title}
              >
                <h2>
                  <Link href={`/apps/${id}`}>{app.title}</Link>
                </h2>
                <p>{app.summary}</p>
                <div className="asset-tags">
                  <span>
                    {t("v{version}", { version: app.current_version })}
                  </span>
                  {app.kind !== "flow" && <span>{t("代码应用")}</span>}
                  {app.has_draft && (
                    <span className="draft-tag">{t("有草稿")}</span>
                  )}
                  {app.has_eval_set && <span>{t("需通过评测")}</span>}
                </div>
                <div className="app-card-actions">
                  <Link
                    className="button primary"
                    href={`/use/${id}`}
                    aria-label={t("使用「{title}」", { title: app.title })}
                  >
                    {t("使用")}
                  </Link>
                  <Link
                    className="button secondary"
                    href={`/apps/${id}`}
                    aria-label={t("设计「{title}」", { title: app.title })}
                  >
                    {t("设计")}
                  </Link>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
