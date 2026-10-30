"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  getHealth,
  getUsage,
  healthLabels,
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

// Per row: missing while the request runs, "failed" when it did not answer.
type Rows<T> = Record<string, T | "failed">;

const dialogTitles: Record<string, string> = {
  file: "上传文件",
  mcp: "登记工具服务",
};
const healthText = (value: Health | "failed" | undefined) =>
  value === undefined
    ? "检查中…"
    : value === "failed"
      ? "没查到状态"
      : value.ok
        ? "正常"
        : healthLabels[value.error ?? "other"];

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
  return (
    <div className="table-scroll">
      <table className="mapping-table datasource-table" aria-label={label}>
        <thead>
          <tr>
            {[
              "名称",
              "业务说明",
              "类型",
              "连通状态",
              "工具数",
              "被几个应用使用",
            ].map((heading) => (
              <th scope="col" key={heading}>
                {heading}
              </th>
            ))}
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
                </td>
                <td className="datasource-description">
                  {source.description || "—"}
                </td>
                <td className="datasource-kind">
                  <span className="datasource-tag">
                    {kindLabel(source.kind)}
                  </span>
                </td>
                <td className="datasource-health-cell" aria-busy={busy}>
                  <span
                    className={`datasource-health ${state === undefined ? "pending" : failing ? "error" : "ok"}`}
                  >
                    {healthText(state)}
                  </span>
                  {state !== undefined &&
                    (failing || rechecked.includes(id)) && (
                      <button
                        type="button"
                        className="text-button"
                        aria-label={`${checking[id] ? "检查中…" : "重新检查"}「${source.name}」`}
                        aria-disabled={checking[id] || undefined}
                        onClick={() => recheck(source)}
                      >
                        {checking[id] ? "检查中…" : "重新检查"}
                      </button>
                    )}
                </td>
                <td
                  className={`datasource-count ${tools === null ? "unknown" : ""}`}
                >
                  {tools === null ? "—" : `${tools} 个工具`}
                </td>
                <td className="datasource-count datasource-usage">
                  {used === undefined
                    ? "统计中…"
                    : used === "failed"
                      ? "没统计到"
                      : `${used.apps.length} 个应用`}
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
    setAnnouncement(`${source.name}：${healthText(value)}`);
  }

  const databases = items?.filter((source) => !isToolService(source)) ?? [];
  const services = items?.filter(isToolService) ?? [];
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
      <AssetHeading title="数据源与工具">
        数据和工具都通过连接器接入。连接器是数据团队部署的小服务（MCP
        协议），平台只保存它的地址，数据库账号密码配在连接器上。数据源给 ETL
        和「数据工程」类节点读数据；工具服务只提供工具，比如查工单、查
        CRM，在编排应用的「调用工具」和「智能体」节点里使用。
      </AssetHeading>
      <div className="asset-actions">
        <button
          type="button"
          className="button primary"
          onClick={() => open("")}
        >
          登记数据源
        </button>
        <button
          type="button"
          className="button secondary"
          onClick={() => open("file")}
        >
          上传文件
        </button>
      </div>
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
            <h2 id="datasource-group-sources">数据源</h2>
            {databases.length ? (
              table("数据源", databases)
            ) : (
              <p className="muted">还没有登记数据源。</p>
            )}
          </section>
          <section
            className="panel asset-section datasource-section"
            aria-labelledby="datasource-group-services"
          >
            <div className="asset-section-heading">
              <h2 id="datasource-group-services">工具服务</h2>
              <button
                type="button"
                className="button secondary"
                onClick={() => open("mcp")}
              >
                登记工具服务
              </button>
            </div>
            {services.length ? (
              table("工具服务", services)
            ) : (
              <p className="muted">
                还没有登记工具服务。工具服务只提供工具，比如查工单、查 CRM。
              </p>
            )}
          </section>
          <p className="field-hint">
            连通状态在打开页面时检查，结果保留 60
            秒；应用数按各应用当前版本的配置统计。
          </p>
        </>
      )}
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      {dialogKind !== null && (
        <DatasourceDialog
          ref={dialog}
          title={dialogTitles[dialogKind] ?? "登记数据源"}
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
