"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  getHealth,
  getUsage,
  healthLabels,
  isCodeRepo,
  isToolService,
  kindLabel,
  listDatasources,
  type Datasource,
  type DatasourceKind,
  type DatasourceUsage,
  type Health,
} from "../lib/datasources";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { DatasourceDialog, DatasourceForm } from "./datasource-form";
import { ErrorNotice } from "./ui";
import { useT, type Translate } from "../lib/i18n/client";

// Per row: missing while the request runs, "failed" when it did not answer.
type Rows<T> = Record<string, T | "failed">;

const dialogTitles: Record<string, string> = {
  file: "上传文件#按钮",
  mcp: "登记工具服务",
};
const healthText = (t: Translate, value: Health | "failed" | undefined) =>
  value === undefined
    ? t("检查中…")
    : value === "failed"
      ? t("没查到状态")
      : value.ok
        ? t("正常")
        : t(healthLabels[value.error ?? "other"]);

function SourceTable({
  label,
  sources,
  health,
  usage,
  checking,
  rechecked,
  recheck,
}: {
  label: string;
  sources: Datasource[];
  health: Rows<Health>;
  usage: Rows<DatasourceUsage>;
  checking: Record<string, boolean>;
  rechecked: string[];
  recheck: (source: Datasource) => void;
}) {
  const t = useT();
  return (
    <div className="table-scroll">
      <table className="mapping-table datasource-table" aria-label={label}>
        <thead>
          <tr>
            <th scope="col">{t("名称")}</th>
            <th scope="col">{t("类型")}</th>
            <th scope="col">{t("连通状态")}</th>
            <th scope="col">{t("用量")}</th>
          </tr>
        </thead>
        <tbody>
          {sources.map((source) => {
            const id = source.datasource_id;
            const state = health[id];
            const used = usage[id];
            const busy = state === undefined || Boolean(checking[id]);
            const failing = state === "failed" || (state && !state.ok);
            const tools = state && state !== "failed" ? state.tool_count : null;
            return (
              <tr key={id}>
                <td className="datasource-name">
                  <Link href={`/datasources/${encodeURIComponent(id)}`}>
                    {source.name}
                  </Link>
                  {source.description && (
                    <small className="muted" title={source.description}>
                      {source.description}
                    </small>
                  )}
                </td>
                <td className="datasource-kind">
                  <span className="datasource-tag">
                    {t(kindLabel(source.kind))}
                  </span>
                </td>
                <td className="datasource-health-cell" aria-busy={busy}>
                  <span
                    className={`datasource-health ${state === undefined ? "pending" : failing ? "error" : "ok"}`}
                  >
                    {healthText(t, state)}
                  </span>
                  {state !== undefined &&
                    (failing || rechecked.includes(id)) && (
                      <button
                        type="button"
                        className="text-button"
                        aria-label={t(
                          checking[id]
                            ? t("检查中…「{name}」")
                            : t("重新检查「{name}」"),
                          { name: source.name },
                        )}
                        aria-disabled={checking[id] || undefined}
                        onClick={() => recheck(source)}
                      >
                        {checking[id] ? t("检查中…") : t("重新检查")}
                      </button>
                    )}
                </td>
                <td className="datasource-count datasource-usage">
                  <span className={tools === null ? "muted" : ""}>
                    {tools === null ? "—" : t("{n} 个工具", { n: tools })}
                  </span>
                  <span className="muted">
                    {used === undefined
                      ? t("统计中…")
                      : used === "failed"
                        ? t("没统计到")
                        : t("{n} 个应用在用", { n: used.apps.length })}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function DatasourceList() {
  const t = useT();
  const router = useRouter();
  const [items, setItems] = useState<Datasource[] | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [health, setHealth] = useState<Rows<Health>>({});
  const [usage, setUsage] = useState<Rows<DatasourceUsage>>({});
  const [checking, setChecking] = useState<Record<string, boolean>>({});
  // Rows the user rechecked keep their button, so focus stays on it when the row recovers.
  const [rechecked, setRechecked] = useState<string[]>([]);
  const [announcement, setAnnouncement] = useState("");
  const [dialogKind, setDialogKind] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    listDatasources(signal)
      .then((sources) => {
        if (signal.aborted) return;
        setItems(sources);
        setError("");
        setHealth({});
        setUsage({});
        // Status and usage come per row, so one slow connector holds up only its own row.
        for (const { datasource_id: id } of sources) {
          getHealth(id, { signal })
            .then((value) => setHealth((old) => ({ ...old, [id]: value })))
            .catch(() => {
              if (!signal.aborted)
                setHealth((old) => ({ ...old, [id]: "failed" }));
            });
          getUsage(id, signal)
            .then((value) => setUsage((old) => ({ ...old, [id]: value })))
            .catch(() => {
              if (!signal.aborted)
                setUsage((old) => ({ ...old, [id]: "failed" }));
            });
        }
      })
      .catch((err) => {
        if (!signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [revision]);

  async function recheck(source: Datasource) {
    const id = source.datasource_id;
    if (checking[id]) return;
    setChecking((old) => ({ ...old, [id]: true }));
    setRechecked((old) => (old.includes(id) ? old : [...old, id]));
    setAnnouncement("");
    let value: Health | "failed";
    try {
      value = await getHealth(id, { fresh: true });
    } catch {
      value = "failed";
    }
    setHealth((old) => ({ ...old, [id]: value }));
    setChecking((old) => ({ ...old, [id]: false }));
    setAnnouncement(
      t("{name}：{state}", { name: source.name, state: healthText(t, value) }),
    );
  }

  const databases =
    items?.filter((source) => !isToolService(source) && !isCodeRepo(source)) ??
    [];
  const services = items?.filter(isToolService) ?? [];
  const repos = items?.filter(isCodeRepo) ?? [];
  const table = (label: string, sources: Datasource[]) => (
    <SourceTable
      label={label}
      sources={sources}
      health={health}
      usage={usage}
      checking={checking}
      rechecked={rechecked}
      recheck={(source) => void recheck(source)}
    />
  );
  const open = (kind: DatasourceKind | "") => setDialogKind(kind);
  return (
    <div className="asset-page">
      <AssetHeading
        title={t("数据源与工具")}
        action={
          <div className="asset-actions heading-actions">
            <button
              type="button"
              className="button secondary"
              onClick={() => open("file")}
            >
              {t("上传文件#按钮")}
            </button>
            <button
              type="button"
              className="button primary"
              onClick={() => open("")}
            >
              {t("登记数据源")}
            </button>
          </div>
        }
      >
        {t(
          "数据和工具都经连接器（MCP 服务）接入，平台只保存地址，账号密码留在连接器上。",
        )}
      </AssetHeading>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!items && !error && <AssetLoading />}
      {items && (
        <>
          <section
            className="panel asset-section datasource-section"
            aria-labelledby="datasource-group-sources"
          >
            <h2 id="datasource-group-sources">{t("数据源#分组")}</h2>
            {databases.length ? (
              table(t("数据源#分组"), databases)
            ) : (
              <p className="muted">{t("还没有登记数据源。")}</p>
            )}
          </section>
          <section
            className="panel asset-section datasource-section"
            aria-labelledby="datasource-group-services"
          >
            <div className="asset-section-heading">
              <h2 id="datasource-group-services">{t("工具服务#分组")}</h2>
              <button
                type="button"
                className="button secondary"
                onClick={() => open("mcp")}
              >
                {t("登记工具服务")}
              </button>
            </div>
            {services.length ? (
              table(t("工具服务#分组"), services)
            ) : (
              <p className="muted">
                {t(
                  "还没有登记工具服务。工具服务只提供工具，比如查工单、查 CRM。",
                )}
              </p>
            )}
          </section>
          <section
            className="panel asset-section datasource-section"
            aria-labelledby="datasource-group-repos"
          >
            <div className="asset-section-heading">
              <h2 id="datasource-group-repos">{t("代码仓库#分组")}</h2>
              <button
                type="button"
                className="button secondary"
                onClick={() => open("git")}
              >
                {t("登记代码仓库")}
              </button>
            </div>
            {repos.length ? (
              table(t("代码仓库#分组"), repos)
            ) : (
              <p className="muted">
                {t(
                  "还没有登记代码仓库。代码仓库经 Git 连接器接入 GitHub 或 Bitbucket，「提交 PR」节点用它把代码以 PR 提交上去。",
                )}
              </p>
            )}
          </section>
          <p className="field-hint">
            {t(
              "连通状态在打开页面时检查，结果保留 60 秒；应用数按各应用当前版本的配置统计。",
            )}
          </p>
        </>
      )}
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      {dialogKind !== null && (
        <DatasourceDialog
          ref={dialog}
          title={t(dialogTitles[dialogKind] ?? t("登记数据源"))}
          onClose={() => setDialogKind(null)}
        >
          <DatasourceForm
            mode="create"
            initialKind={dialogKind as DatasourceKind | ""}
            onKindChange={setDialogKind}
            onCreated={(id) =>
              router.push(`/datasources/${encodeURIComponent(id)}`)
            }
          />
        </DatasourceDialog>
      )}
    </div>
  );
}
