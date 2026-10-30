"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import {
  CREDENTIALS_NOTE,
  READ_ONLY_TAG,
  TOOL_SERVICE_NOTE,
  UNDECLARED_TAG,
  forgetTools,
  getDatasource,
  getHealth,
  getUsage,
  healthLabels,
  isToolService,
  kindLabel,
  listTools,
  parameterNote,
  parameterType,
  toolDescription,
  undeclaredNote,
  viaLabels,
  type ConnectorTool,
  type Datasource,
  type DatasourceUsage,
  type Health,
  type ToolParameter,
} from "../lib/datasources";
import { ApiError, errorMessage, isRecord } from "../lib/runs";
import { AssetHeading, AssetLoading, AssetTime } from "./asset-ui";
import { DatasourceDialog, DatasourceForm } from "./datasource-form";
import { ErrorNotice } from "./ui";

// The note's example, the etl code app, picks a data source at run time, never a tool service.
const USAGE_NOTE =
  "只看各应用当前版本的配置；运行时才选、开始节点也没有默认值的不算在内";

function ToolCard({ tool, kind }: { tool: ConnectorTool; kind: string }) {
  const schema = isRecord(tool.input_schema) ? tool.input_schema : {};
  const properties = Object.entries(
    isRecord(schema.properties) ? schema.properties : {},
  ).map(([key, value]): [string, ToolParameter] => [
    key,
    isRecord(value) ? value : {},
  ]);
  const required = Array.isArray(schema.required) ? schema.required : [];
  const description = toolDescription(tool, kind);
  return (
    <article className="datasource-tool">
      <div className="datasource-tool-heading">
        <h3>
          <code>{tool.name}</code>
        </h3>
        <span
          className={`datasource-tag ${tool.read_only ? "" : "undeclared"}`}
        >
          {tool.read_only ? READ_ONLY_TAG : UNDECLARED_TAG}
        </span>
      </div>
      {description && <p>{description}</p>}
      {properties.length ? (
        <div className="table-scroll">
          <table
            className="mapping-table datasource-params"
            aria-label={`${tool.name} 的参数`}
          >
            <thead>
              <tr>
                {["参数", "类型", "必填", "说明"].map((heading) => (
                  <th scope="col" key={heading}>
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {properties.map(([key, parameter]) => (
                <tr key={key}>
                  <td>
                    <code>{key}</code>
                  </td>
                  <td>{parameterType(parameter)}</td>
                  <td>{required.includes(key) ? "是" : "否"}</td>
                  <td>{parameterNote(parameter)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">不需要参数。</p>
      )}
    </article>
  );
}

export function DatasourceDetail({ id }: { id: string }) {
  const [source, setSource] = useState<Datasource | null>(null);
  const [error, setError] = useState<{ message: string; missing: boolean }>();
  const [revision, setRevision] = useState(0);
  // Each check (opening the page, 重新检查, a failed tool listing) gets a number; the tools
  // load after it. A `keep` check only re-reads the status and keeps the tools as they are.
  const [check, setCheck] = useState({ n: 0, fresh: false, keep: false });
  const [connection, setConnection] = useState<{
    n: number;
    health: Health | "failed" | null;
    error: string;
    // The check the tools are listed for.
    listed: number;
    // The name when the check finished, so a rename alone announces nothing.
    name: string;
  }>({ n: -1, health: null, error: "", listed: -1, name: "" });
  const [toolState, setToolState] = useState<{
    key: string;
    tools?: ConnectorTool[];
    error?: string;
  }>({ key: "" });
  const [usageRevision, setUsageRevision] = useState(0);
  const [usage, setUsage] = useState<{
    n: number;
    value?: DatasourceUsage;
    error?: string;
  }>({ n: -1 });
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);
  const editButton = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const toolsHeading = useRef<HTMLHeadingElement>(null);
  const focusTools = useRef(false);
  const name = useRef("");
  useEffect(() => {
    name.current = source?.name ?? "";
  }, [source]);

  useEffect(() => {
    const controller = new AbortController();
    getDatasource(id, controller.signal)
      .then((value) => {
        setSource(value);
        setError(undefined);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        const missing = err instanceof ApiError && err.status === 404;
        setError({
          message: missing ? "没有找到这个数据源或工具服务" : errorMessage(err),
          missing,
        });
      });
    return () => controller.abort();
  }, [id, revision]);
  useEffect(() => {
    const controller = new AbortController();
    const done = (health: Health | "failed", error = "") => {
      // The tools load (and take the focus) only when the connector answers.
      if (health === "failed" || !health.ok) focusTools.current = false;
      setConnection((old) => ({
        n: check.n,
        health,
        error,
        listed: check.keep ? old.listed : check.n,
        name: name.current,
      }));
    };
    getHealth(id, { fresh: check.fresh, signal: controller.signal })
      .then((health) => done(health))
      .catch((err) => {
        if (!controller.signal.aborted) done("failed", errorMessage(err));
      });
    return () => controller.abort();
  }, [id, check]);
  const checking = connection.n !== check.n;
  const health = connection.health;
  const healthy = health !== null && health !== "failed" && health.ok;
  // The tools are listed only once the connector answers, and again after each check.
  const toolsKey = healthy ? String(connection.listed) : "";
  useEffect(() => {
    if (!toolsKey) return;
    const controller = new AbortController();
    listTools(id, controller.signal)
      .then((tools) => setToolState({ key: toolsKey, tools }))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setToolState({ key: toolsKey, error: errorMessage(err) });
        // Listing the tools also updated the status (docs/api/datasources.md, 连通状态):
        // read it again, so the page does not say 正常 above a connection error.
        if (err instanceof ApiError && err.status === 502)
          setCheck((old) => ({ n: old.n + 1, fresh: false, keep: true }));
      });
    return () => controller.abort();
  }, [id, toolsKey]);
  const toolsLoading = Boolean(toolsKey) && toolState.key !== toolsKey;
  useEffect(() => {
    if (toolsLoading || !focusTools.current) return;
    focusTools.current = false;
    toolsHeading.current?.focus();
  }, [toolsLoading]);
  useEffect(() => {
    const controller = new AbortController();
    getUsage(id, controller.signal)
      .then((value) => setUsage({ n: usageRevision, value }))
      .catch((err) => {
        if (!controller.signal.aborted)
          setUsage({ n: usageRevision, error: errorMessage(err) });
      });
    return () => controller.abort();
  }, [id, usageRevision]);

  function recheck(fromTools = false) {
    if (checking) return;
    focusTools.current = fromTools;
    setCheck((old) => ({ n: old.n + 1, fresh: true, keep: false }));
  }

  if (!source)
    return (
      <div className="asset-page">
        <Link className="back-link" href="/datasources">
          ← 数据源与工具
        </Link>
        {error ? (
          <ErrorNotice
            message={error.message}
            onRetry={
              error.missing ? undefined : () => setRevision((n) => n + 1)
            }
          />
        ) : (
          <AssetLoading />
        )}
      </div>
    );
  const tools = toolsKey && !toolsLoading ? toolState.tools : undefined;
  const undeclared = tools?.filter((tool) => !tool.read_only).length ?? 0;
  const apps = usage.n === usageRevision ? usage.value?.apps : undefined;
  // Read out when a later check finishes, even with the same result as before.
  const announcement =
    connection.n > 0 && !checking && health !== null
      ? `${connection.name}：${health === "failed" ? "没查到状态" : health.ok ? "正常" : healthLabels[health.error ?? "other"]}`
      : "";
  return (
    <div className="asset-page">
      <Link className="back-link" href="/datasources">
        ← 数据源与工具
      </Link>
      <AssetHeading title={source.name} />
      <div className="asset-actions">
        <div className="asset-tags">
          <span>{kindLabel(source.kind)}</span>
        </div>
        <button
          ref={editButton}
          type="button"
          className="button secondary"
          onClick={() => {
            setSaved(false);
            setEditing(true);
          }}
        >
          修改
        </button>
        <span className="muted" role="status">
          {saved ? "已保存" : ""}
        </span>
      </div>
      <section
        className="panel asset-section"
        aria-labelledby="datasource-info-title"
      >
        <h2 id="datasource-info-title">基本信息</h2>
        <dl className="datasource-meta">
          <div>
            <dt>业务说明</dt>
            <dd>{source.description || "还没有说明"}</dd>
          </div>
          <div>
            <dt>连接器地址</dt>
            <dd>
              <code className="datasource-url">{source.connector_url}</code>
              <p className="field-hint">
                {CREDENTIALS_NOTE}
                {isToolService(source) && TOOL_SERVICE_NOTE}
              </p>
            </dd>
          </div>
          <div>
            <dt>登记时间</dt>
            <dd>
              <AssetTime value={source.created_at} />
            </dd>
          </div>
        </dl>
      </section>
      <section
        className="panel asset-section"
        aria-labelledby="datasource-health-title"
      >
        <h2 id="datasource-health-title">连通状态</h2>
        <div className="datasource-status">
          <div
            className={`datasource-health ${health === null ? "pending" : healthy ? "ok" : "error"}`}
            aria-busy={checking}
          >
            {health === null
              ? "正在检查…"
              : health === "failed"
                ? `没查到状态：${connection.error}`
                : health.ok
                  ? `正常 · ${health.latency_ms} 毫秒 · ${health.tool_count} 个工具`
                  : (health.message ?? healthLabels[health.error ?? "other"])}
          </div>
          {health && health !== "failed" && (
            <p className="field-hint">
              检查时间{" "}
              <time dateTime={health.checked_at}>
                {new Date(health.checked_at).toLocaleTimeString("zh-CN", {
                  hour12: false,
                })}
              </time>
            </p>
          )}
          <button
            type="button"
            className="button secondary"
            aria-disabled={checking || undefined}
            onClick={() => recheck()}
          >
            {checking ? "检查中…" : "重新检查"}
          </button>
        </div>
      </section>
      <section
        className="panel asset-section"
        aria-labelledby="datasource-tools-title"
        aria-busy={toolsLoading || (health === null && checking)}
      >
        <h2 id="datasource-tools-title" ref={toolsHeading} tabIndex={-1}>
          工具{tools ? `（${tools.length}）` : ""}
        </h2>
        {health === null ? (
          <p className="muted">正在检查连通状态…</p>
        ) : !healthy ? (
          <div className="datasource-offline">
            <p>
              {health === "failed"
                ? "没查到连通状态，暂时看不到工具。"
                : "连接器现在连不上，暂时看不到工具。"}
            </p>
            <button
              type="button"
              className="button secondary"
              aria-label={
                checking ? "检查中…，完成后列出工具" : "重新检查连接并列出工具"
              }
              aria-disabled={checking || undefined}
              onClick={() => recheck(true)}
            >
              {checking ? "检查中…" : "重新检查"}
            </button>
          </div>
        ) : toolsLoading ? (
          <p className="muted">正在读取工具…</p>
        ) : toolState.error ? (
          <ErrorNotice
            message={toolState.error}
            onRetry={() => recheck(true)}
          />
        ) : tools?.length ? (
          <div className="datasource-tools">
            {undeclared > 0 && (
              <p className="datasource-note warn">
                {undeclaredNote(undeclared)}
              </p>
            )}
            {tools.map((tool) => (
              <ToolCard key={tool.name} tool={tool} kind={source.kind} />
            ))}
            <p className="field-hint">
              「只读」是连接器自己报的，只用来展示，平台没有核实。
            </p>
          </div>
        ) : (
          <p className="muted">这个连接器没有提供工具。</p>
        )}
      </section>
      <section
        className="panel asset-section"
        aria-labelledby="datasource-usage-title"
      >
        <h2 id="datasource-usage-title">被哪些应用使用</h2>
        {usage.n !== usageRevision ? (
          <p className="muted">统计中…</p>
        ) : usage.error ? (
          <ErrorNotice
            message={usage.error}
            onRetry={() => setUsageRevision((n) => n + 1)}
          />
        ) : apps?.length ? (
          <ul className="datasource-usage-list">
            {apps.map((item) => (
              <li key={item.app}>
                <Link href={`/apps/${encodeURIComponent(item.app)}`}>
                  {item.title}
                </Link>
                {` · v${item.version} · ${viaLabels[item.via] ?? item.via}`}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">还没有应用用到它。</p>
        )}
        <p className="field-hint">
          {USAGE_NOTE}
          {isToolService(source) ? "。" : "，比如「自然语言 ETL 工作流」。"}
        </p>
      </section>
      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
      {editing && (
        <DatasourceDialog
          ref={dialog}
          title={isToolService(source) ? "修改工具服务" : "修改数据源"}
          onClose={() => {
            setEditing(false);
            editButton.current?.focus();
          }}
        >
          <DatasourceForm
            mode="edit"
            initial={source}
            onSaved={(next) => {
              // Focus goes back to 修改, not to the tools when they load again.
              focusTools.current = false;
              // A new address was tested on save; that result is the new status, and
              // the flow editor lists the new connector's tools.
              if (next.connector_url !== source.connector_url) {
                forgetTools(id);
                setCheck((old) => ({
                  n: old.n + 1,
                  fresh: false,
                  keep: false,
                }));
              }
              setSource(next);
              setSaved(true);
              dialog.current?.close();
            }}
          />
        </DatasourceDialog>
      )}
    </div>
  );
}
