"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  activateAppVersion,
  isFlowConfig,
  exportAppVersion,
  deleteAppDraft,
  evaluateAppDraft,
  getApp,
  getAppVersions,
  publishAppDraft,
  saveAppDraft,
  type AppConfig,
  type AppDetail,
  type AppEval,
  type AppVersion,
} from "../lib/apps";
import { actorPayload, savedActor } from "../lib/actor";
import { ApiError, errorMessage } from "../lib/runs";
import {
  ActorField,
  AssetHeading,
  AssetLoading,
  AssetTime,
  useUnsavedWarning,
  VersionPicker,
} from "./asset-ui";
import { AppSettings, settingError } from "./app-settings";
import { FlowDiagram } from "./flow-diagram";
import { FlowEditor } from "./flow-editor";
import { FlowEnv } from "./flow-env";
import {
  knowledgeIssues,
  noIssues,
  sortIssues,
  type FlowIssues,
} from "../lib/flow-edit";
import { FlowNodeDesign } from "./flow-node-design";
import { ErrorNotice } from "./ui";
import { AppMetadata } from "./app-metadata";

const evalLabels = {
  running: "评测运行中",
  passed: "评测通过",
  failed: "评测未通过",
  error: "评测出错",
};
const caseLabels: Record<string, string> = {
  running: "运行中",
  done: "已完成",
  error: "失败",
  pending: "等待中",
  passed: "通过",
  failed: "未通过",
};
const scoreText = (value: number | null | undefined) =>
  value == null ? "—" : value.toFixed(2);

function Evaluation({ evaluation }: { evaluation: AppEval | null }) {
  if (!evaluation)
    return (
      <p className="muted">尚未运行评测。每次保存草稿后，需要重新评测。</p>
    );
  return (
    <div className="evaluation-result" aria-live="polite">
      <div className={`eval-summary ${evaluation.status}`}>
        <strong>{evalLabels[evaluation.status]}</strong>
        <span>
          总分 {scoreText(evaluation.score)} · 阈值{" "}
          {scoreText(evaluation.threshold)}
        </span>
      </div>
      {evaluation.error && <p className="field-error">{evaluation.error}</p>}
      <ul className="eval-cases" aria-label="评测用例">
        {evaluation.cases.map((item) => (
          <li key={item.id}>
            {item.run_id ? (
              <Link href={`/runs/${encodeURIComponent(item.run_id)}`}>
                {item.id} ↗
              </Link>
            ) : (
              <span>{item.id}</span>
            )}
            <span>
              {caseLabels[item.status] ?? item.status} · 得分{" "}
              {scoreText(item.score)}
            </span>
            {item.error && <p className="field-error">{item.error}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ConfigDiff({
  left,
  right,
  data,
}: {
  left: AppVersion;
  right: AppVersion;
  data: AppDetail;
}) {
  if (isFlowConfig(left.config) || isFlowConfig(right.config))
    return (
      <section className="asset-diff" aria-label="配置差异">
        <h3>
          配置差异：v{left.version} → v{right.version}
        </h3>
        {JSON.stringify(left.config) === JSON.stringify(right.config) ? (
          <p className="muted">两个版本配置相同。</p>
        ) : (
          <div className="flow-config-comparison">
            <pre className="flow-json">
              {JSON.stringify(left.config, null, 2)}
            </pre>
            <pre className="flow-json">
              {JSON.stringify(right.config, null, 2)}
            </pre>
          </div>
        )}
      </section>
    );
  const changes: {
    node: string;
    key: string;
    before: unknown;
    after: unknown;
  }[] = [];
  const names = new Set([
    ...Object.keys(left.config.nodes),
    ...Object.keys(right.config.nodes),
  ]);
  for (const node of names) {
    const a = left.config.nodes[node];
    const b = right.config.nodes[node];
    const settings = new Set([
      "model",
      "skills",
      ...Object.keys(a?.params ?? {}),
      ...Object.keys(b?.params ?? {}),
    ]);
    for (const key of settings) {
      const before =
        key === "model"
          ? a?.model
          : key === "skills"
            ? a?.skills
            : a?.params[key];
      const after =
        key === "model"
          ? b?.model
          : key === "skills"
            ? b?.skills
            : b?.params[key];
      if (JSON.stringify(before) !== JSON.stringify(after))
        changes.push({ node, key, before, after });
    }
  }
  const show = (value: unknown) =>
    value === undefined
      ? "未设置"
      : value === null
        ? "平台默认"
        : JSON.stringify(value);
  return (
    <section className="asset-diff" aria-label="配置差异">
      <h3>
        配置差异：v{left.version} → v{right.version}
      </h3>
      {changes.length ? (
        <ul>
          {changes.map((change) => {
            const node = data.nodes.find((item) => item.id === change.node);
            const setting = node?.settings.find(
              (item) => item.key === change.key,
            );
            return (
              <li key={`${change.node}.${change.key}`}>
                <strong>
                  {node?.title ?? change.node} / {setting?.label ?? change.key}{" "}
                  <code>
                    ({change.node}.{change.key})
                  </code>
                </strong>
                <div>
                  <del>{show(change.before)}</del>
                  <ins>{show(change.after)}</ins>
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="muted">两个版本配置相同。</p>
      )}
    </section>
  );
}

export function AppDetailView({ app }: { app: string }) {
  const [data, setData] = useState<AppDetail | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [view, setView] = useState<"draft" | "current">("current");
  const [selected, setSelected] = useState<string>("");
  const [versions, setVersions] = useState<AppVersion[]>([]);
  const [compare, setCompare] = useState<number[]>([]);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [actor, setActor] = useState(savedActor);
  const [retry, setRetry] = useState(0);
  const [issues, setIssues] = useState<FlowIssues>(noIssues);

  useEffect(() => {
    const controller = new AbortController();
    getApp(app, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setData(value);
        setConfig(structuredClone((value.draft ?? value.current).config));
        setView(value.draft ? "draft" : "current");
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted)
          setError(
            err instanceof ApiError && err.status === 404
              ? "没有找到这个应用。"
              : errorMessage(err),
          );
      });
    getAppVersions(app, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setVersions(value);
          setHistoryError("");
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setHistoryError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app, retry]);

  const running = data?.draft?.eval?.status === "running";
  useEffect(() => {
    if (!running) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      let keepPolling = true;
      try {
        const value = await getApp(app, controller.signal);
        if (controller.signal.aborted) return;
        setData(value);
        setError("");
        keepPolling = value.draft?.eval?.status === "running";
      } catch (err) {
        if (!controller.signal.aborted) setError(errorMessage(err));
      }
      if (keepPolling && !controller.signal.aborted)
        timer = setTimeout(() => void poll(), 5_000);
    }
    timer = setTimeout(() => void poll(), 5_000);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [app, running]);

  const source = data
    ? view === "draft"
      ? (data.draft ?? data.current)
      : data.current
    : null;
  const dirty =
    config !== null &&
    JSON.stringify(config) !== JSON.stringify(source?.config);
  useUnsavedWarning(dirty);

  function accept(value: AppDetail, message: string) {
    setIssues(noIssues);
    setData(value);
    setConfig(structuredClone((value.draft ?? value.current).config));
    setView(value.draft ? "draft" : "current");
    setNotice(message);
  }
  async function action(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await work();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  async function refreshHistory() {
    try {
      setVersions(await getAppVersions(app));
      setHistoryError("");
    } catch (err) {
      setHistoryError(errorMessage(err));
    }
  }
  function switchView(next: "current" | "draft") {
    if (
      !data ||
      (dirty && !window.confirm("切换版本会丢弃未保存的修改，继续吗？"))
    )
      return;
    setView(next);
    setConfig(
      structuredClone(
        (next === "draft" ? (data.draft ?? data.current) : data.current).config,
      ),
    );
    setError("");
    setNotice("");
  }
  function save() {
    if (!config || !data) return;
    if (isFlowConfig(config)) {
      void action(async () => {
        try {
          const localIssues = knowledgeIssues(
            config,
            data.options.rerank_models ?? [],
          );
          if (localIssues.length)
            throw new ApiError("请检查检索设置", 422, localIssues);
          accept(
            await saveAppDraft(app, config),
            "草稿已保存，上次评测结果已清空。",
          );
        } catch (err) {
          if (
            !(err instanceof ApiError) ||
            err.status !== 422 ||
            !err.issues.length
          )
            throw err;
          const sorted = sortIssues(err.issues, config);
          setIssues(sorted);
          const count = Object.values(sorted.nodes).reduce(
            (sum, bucket) => sum + Object.keys(bucket).length,
            Object.keys(sorted.env).length,
          );
          throw new ApiError(
            [
              "草稿没有保存。",
              count ? `有 ${count} 处问题，已标在对应的节点和设置下。` : "",
              ...sorted.graph,
            ]
              .filter(Boolean)
              .join(""),
            422,
          );
        }
      });
      return;
    }
    const codeNodes = isFlowConfig(config) ? {} : config.nodes;
    const errors = data.nodes
      .flatMap((node) =>
        node.settings.map((setting) =>
          codeNodes[node.id]
            ? settingError(setting, codeNodes[node.id])
            : `缺少节点 ${node.id} 的配置`,
        ),
      )
      .filter(Boolean);
    if (errors.length) {
      setError(errors.join("；"));
      return;
    }
    void action(async () =>
      accept(
        await saveAppDraft(app, config),
        "草稿已保存，上次评测结果已清空。",
      ),
    );
  }

  if (!data || !config)
    return (
      <div className="asset-page">
        <Link href="/apps" className="back-link">
          ← 应用
        </Link>
        {error ? (
          <ErrorNotice
            message={error}
            onRetry={() => setRetry((value) => value + 1)}
          />
        ) : (
          <AssetLoading />
        )}
      </div>
    );
  const codeNodes = isFlowConfig(config) ? {} : config.nodes;
  const node =
    data.nodes.find((item) => item.id === selected) ??
    data.nodes.find((item) => item.settings.length) ??
    data.nodes[0];
  const graph = {
    nodes: data.nodes.map((item) => ({
      ...item,
      model:
        codeNodes[item.id]?.model ??
        (item.kind === "model" ? data.options.default_model : null),
    })),
    edges: data.edges,
  };
  const displayedEval =
    view === "draft" ? (data.draft?.eval ?? null) : data.current.eval;
  const publishReason = !data.draft
    ? "请先保存草稿。"
    : view !== "draft"
      ? "请切换到草稿后发布。"
      : dirty
        ? "有未保存的修改，请先保存草稿并重新评测。"
        : running
          ? "评测运行中，请等待结果。"
          : data.has_eval_set && data.draft.eval?.status !== "passed"
            ? "草稿须先通过评测才能发布；总分需达到阈值。"
            : "";
  const left = versions.find((item) => item.version === compare[0]);
  const right = versions.find((item) => item.version === compare[1]);

  return (
    <div className="asset-page">
      <Link href="/apps" className="back-link">
        ← 应用
      </Link>
      <AssetHeading eyebrow={app} title={data.title}>
        {data.summary}
      </AssetHeading>
      <div className="asset-actions">
        <span className="app-tag">
          {data.kind === "flow" ? "编排应用" : "代码应用"}
        </span>
        <Link
          className="button primary"
          href={`/runs/new?app=${encodeURIComponent(app)}`}
        >
          新建运行
        </Link>
      </div>
      {data.kind === "flow" && (
        <AppMetadata
          data={data}
          changed={(value) => accept(value, "应用信息已保存。")}
        />
      )}
      {error && <ErrorNotice message={error} />}
      {notice && (
        <p className="asset-notice" role="status">
          {notice}
        </p>
      )}
      <section
        className={`panel config-banner ${view}`}
        aria-label="正在查看的配置"
      >
        <div>
          <strong>
            {view === "draft"
              ? "正在查看：草稿"
              : `正在查看：当前版本 v${data.current_version}`}
          </strong>
          <p>
            {dirty
              ? "有未保存的修改"
              : view === "draft"
                ? `草稿已保存 · 当前发布版本 v${data.current_version}`
                : data.kind === "flow"
                  ? "新运行使用当前发布版本。"
                  : "编辑节点设置后，可保存为草稿。"}
          </p>
        </div>
        <div className="asset-actions">
          {data.draft && (
            <>
              <button
                type="button"
                className="button secondary"
                disabled={busy || view === "current"}
                onClick={() => switchView("current")}
              >
                查看当前版本
              </button>
              <button
                type="button"
                className="button secondary"
                disabled={busy || view === "draft"}
                onClick={() => switchView("draft")}
              >
                查看草稿
              </button>
            </>
          )}
          <button
            type="button"
            className="button primary"
            disabled={busy || running || !dirty}
            onClick={save}
          >
            保存草稿
          </button>
          <button
            type="button"
            className="button secondary"
            disabled={busy || (!data.draft && !dirty)}
            onClick={() => {
              if (!window.confirm("确定放弃草稿和未保存的修改吗？")) return;
              void action(async () => {
                // With only unsaved edits there is no draft on the server to delete.
                if (data.draft) await deleteAppDraft(app);
                accept(await getApp(app), "已放弃草稿。");
              });
            }}
          >
            放弃草稿
          </button>
        </div>
      </section>
      {isFlowConfig(config) ? (
        <>
          <FlowEditor
            app={app}
            data={data}
            config={config}
            setConfig={(value) => {
              setConfig(value);
              setNotice("");
            }}
            issues={issues}
            dirty={dirty}
            disabled={busy || running}
          />
          <FlowEnv
            app={app}
            config={config}
            setConfig={(value) => {
              setConfig(value);
              setNotice("");
            }}
            secrets={data.secrets}
            setSecrets={(secrets) => setData({ ...data, secrets })}
            issues={issues.env}
            disabled={busy || running}
          />
        </>
      ) : (
        <section className="panel run-flow app-design" aria-label="应用设计">
          <div className="section-heading">
            <h2>应用设计与节点配置</h2>
            <span className="muted">点选节点查看设计与设置</span>
          </div>
          <div className="flow-about">
            <dl>
              <div>
                <dt>运行方式</dt>
                <dd>{data.pattern}</dd>
              </div>
              <div>
                <dt>知识库</dt>
                <dd>{data.knowledge}</dd>
              </div>
              <div>
                <dt>Skill</dt>
                <dd>{data.skills_text}</dd>
              </div>
            </dl>
          </div>
          <div className="flow-body">
            <FlowDiagram
              flow={graph}
              selectedId={node?.id}
              select={setSelected}
              designMode
            />
            {node && (
              <div
                className="flow-detail"
                role="region"
                aria-label={`节点 ${node.title}`}
              >
                <FlowNodeDesign
                  node={graph.nodes.find((item) => item.id === node.id)!}
                />
                {node.settings.length > 0 && codeNodes[node.id] ? (
                  <AppSettings
                    key={node.id}
                    node={node}
                    config={codeNodes[node.id]}
                    options={data.options}
                    disabled={busy || running}
                    onChange={(value) => {
                      setConfig({ nodes: { ...codeNodes, [node.id]: value } });
                      setNotice("");
                    }}
                  />
                ) : (
                  <p className="muted">此节点没有可调设置。</p>
                )}
              </div>
            )}
          </div>
        </section>
      )}
      <div className="asset-two-columns">
        <section className="panel asset-section" aria-label="发布前评测">
          <div className="asset-section-heading">
            <h2>发布前评测</h2>
            {data.has_eval_set && (
              <button
                className="button secondary"
                type="button"
                disabled={
                  busy || running || dirty || !data.draft || view !== "draft"
                }
                onClick={() =>
                  void action(async () =>
                    accept(
                      await evaluateAppDraft(app),
                      "评测已开始，每 5 秒更新结果。",
                    ),
                  )
                }
              >
                {running ? "评测运行中…" : "运行评测"}
              </button>
            )}
          </div>
          {data.has_eval_set ? (
            <>
              <p className="muted">
                当前展示
                {view === "draft"
                  ? "草稿最近一次"
                  : `版本 v${data.current_version} 发布时的`}
                评测。
              </p>
              <Evaluation evaluation={displayedEval} />
            </>
          ) : (
            <p className="muted">此应用没有评测集，保存草稿后可直接发布。</p>
          )}
        </section>
        <section className="panel asset-section" aria-label="发布应用">
          <h2>发布应用</h2>
          <form
            className="asset-form"
            onSubmit={(event) => {
              event.preventDefault();
              void action(async () => {
                accept(
                  await publishAppDraft(
                    app,
                    note.trim(),
                    actorPayload(actor).actor,
                  ),
                  "应用已发布为新版本。",
                );
                setNote("");
                await refreshHistory();
              });
            }}
          >
            <label className="asset-field">
              发布说明（必填）
              <textarea
                required
                value={note}
                disabled={busy}
                onChange={(event) => setNote(event.target.value)}
                rows={3}
              />
            </label>
            <ActorField actor={actor} onChange={setActor} disabled={busy} />
            {publishReason && (
              <p className="muted" id="app-publish-reason">
                {publishReason}
              </p>
            )}
            <button
              className="button primary"
              disabled={busy || Boolean(publishReason) || !note.trim()}
              aria-describedby={
                publishReason ? "app-publish-reason" : undefined
              }
            >
              发布新版本
            </button>
          </form>
        </section>
      </div>
      <section className="panel asset-section" aria-label="版本历史">
        <h2>版本历史</h2>
        {historyError && (
          <ErrorNotice
            message={historyError}
            onRetry={() => void refreshHistory()}
          />
        )}
        <ul className="asset-history">
          {versions.map((version) => (
            <li key={version.version}>
              <div>
                <strong>
                  v{version.version} ·{" "}
                  {version.status === "current" ? "当前版本" : "历史版本"}
                </strong>
                <p>{version.note}</p>
                <small>
                  {version.created_by ?? "未署名"} ·{" "}
                  <AssetTime value={version.created_at} /> · 评测分{" "}
                  {scoreText(version.eval?.score)}
                </small>
              </div>
              {data.kind === "flow" && (
                <button
                  type="button"
                  className="button secondary"
                  disabled={busy}
                  onClick={() =>
                    void action(() => exportAppVersion(app, version.version!))
                  }
                >
                  导出
                </button>
              )}
              {version.status !== "current" && (
                <button
                  type="button"
                  className="button secondary"
                  disabled={busy || dirty}
                  onClick={() => {
                    if (
                      !window.confirm(
                        `确定将 v${version.version} 设为当前版本吗？新运行将使用该版本。`,
                      )
                    )
                      return;
                    void action(async () => {
                      accept(
                        await activateAppVersion(
                          app,
                          version.version!,
                          actorPayload(actor).actor,
                        ),
                        `已将 v${version.version} 设为当前版本。`,
                      );
                      await refreshHistory();
                    });
                  }}
                >
                  设为当前版本
                </button>
              )}
            </li>
          ))}
        </ul>
        {versions.length >= 2 && (
          <VersionPicker
            versions={versions.map((item) => item.version!)}
            selected={compare}
            onChange={setCompare}
          />
        )}
        {left && right && <ConfigDiff left={left} right={right} data={data} />}
      </section>
    </div>
  );
}
