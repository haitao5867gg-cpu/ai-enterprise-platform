"use client";

import { useT } from "../lib/i18n/client";
import { AppAccess } from "./app-access";
import { AppBudget } from "./app-budget";
import { AppSchedules } from "./app-schedules";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
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
  type FlowConfig,
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
import { EvalSetPanel } from "./eval-set";
import type { EvalSet } from "../lib/evals";
import { AppMetadata } from "./app-metadata";
import { AppManualPanel, useManual } from "./app-manual";
import { dataNodeIssues } from "../lib/data-nodes";

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

function Evaluation({
  evaluation,
  stale,
}: {
  evaluation: AppEval | null;
  // The eval set changed after this eval started.
  stale: boolean;
}) {
  const t = useT();
  if (!evaluation)
    return (
      <p className="muted">
        {t("尚未运行评测。每次保存草稿后，需要重新评测。")}
      </p>
    );
  return (
    <div className="evaluation-result" aria-live="polite">
      {stale && evaluation.status !== "running" && (
        <p className="eval-stale">{t("评测集改过了，建议重新评测。")}</p>
      )}
      <div className={`eval-summary ${evaluation.status}`}>
        <strong>{t(evalLabels[evaluation.status])}</strong>
        <span>
          {t("总分 {score} · 阈值 {threshold}", {
            score: scoreText(evaluation.score),
            threshold: scoreText(evaluation.threshold),
          })}
        </span>
      </div>
      {evaluation.error && <p className="field-error">{evaluation.error}</p>}
      <ul className="eval-cases" aria-label={t("评测用例")}>
        {evaluation.cases.map((item) => (
          <li key={item.id}>
            {item.run_id ? (
              <Link href={`/runs/${encodeURIComponent(item.run_id)}`}>
                {item.title || item.id} ↗
              </Link>
            ) : (
              <span>{item.title || item.id}</span>
            )}
            <span>
              {t("{status} · 得分 {score}", {
                status: t(caseLabels[item.status] ?? item.status),
                score: scoreText(item.score),
              })}
            </span>
            {item.error && <p className="field-error">{item.error}</p>}
            {item.checks && item.checks.length > 0 && (
              <ul
                className="eval-check-results"
                aria-label={t("{name} 的检查", { name: item.title || item.id })}
              >
                {item.checks.map((check, i) => (
                  <li
                    key={i}
                    className={
                      check.passed === null
                        ? "skipped"
                        : check.passed
                          ? "passed"
                          : "failed"
                    }
                  >
                    <span aria-hidden="true">
                      {check.passed === null ? "–" : check.passed ? "✓" : "✗"}
                    </span>
                    <span>
                      {check.name}
                      <span className="sr-only">
                        {check.passed === null
                          ? t("（跳过）")
                          : check.passed
                            ? t("（通过）")
                            : t("（没通过）")}
                      </span>
                      {check.detail && (
                        <small className="muted"> · {check.detail}</small>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
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
  const t = useT();
  if (isFlowConfig(left.config) || isFlowConfig(right.config))
    return (
      <section className="asset-diff" aria-label={t("配置差异")}>
        <h3>
          {t("配置差异：v{left} → v{right}", {
            left: left.version ?? "—",
            right: right.version ?? "—",
          })}
        </h3>
        {JSON.stringify(left.config) === JSON.stringify(right.config) ? (
          <p className="muted">{t("两个版本配置相同。")}</p>
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
      ? t("未设置")
      : value === null
        ? t("平台默认")
        : JSON.stringify(value);
  return (
    <section className="asset-diff" aria-label={t("配置差异")}>
      <h3>
        {t("配置差异：v{left} → v{right}", {
          left: left.version ?? "—",
          right: right.version ?? "—",
        })}
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
        <p className="muted">{t("两个版本配置相同。")}</p>
      )}
    </section>
  );
}

type Tab = "overview" | "design" | "release" | "access";
const NOT_FOUND = "没有找到这个应用。";

const TABS: [Tab, string][] = [
  ["overview", "概览"],
  ["design", "编排"],
  ["release", "评测与发布"],
  ["access", "接入"],
];

function AppTabs({
  tab,
  onChange,
}: {
  tab: Tab;
  onChange: (tab: Tab) => void;
}) {
  const t = useT();
  return (
    <div
      className="filter-tabs app-tabs"
      role="tablist"
      aria-label={t("应用页")}
    >
      {TABS.map(([key, label], i) => (
        <button
          key={key}
          type="button"
          role="tab"
          id={`app-tab-${key}`}
          aria-selected={tab === key}
          aria-controls={`app-panel-${key}`}
          className={tab === key ? "selected" : ""}
          tabIndex={tab === key ? 0 : -1}
          onClick={() => onChange(key)}
          onKeyDown={(event) => {
            const step =
              event.key === "ArrowRight"
                ? 1
                : event.key === "ArrowLeft"
                  ? -1
                  : 0;
            if (!step) return;
            event.preventDefault();
            const [next] = TABS[(i + step + TABS.length) % TABS.length];
            onChange(next);
            document.getElementById(`app-tab-${next}`)?.focus();
          }}
        >
          {t(label)}
        </button>
      ))}
    </div>
  );
}

// Every panel stays mounted, so switching tabs keeps unsaved edits and loaded data.
function TabPanel({
  tab,
  current,
  children,
}: {
  tab: Tab;
  current: Tab;
  children: ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={`app-panel-${tab}`}
      aria-labelledby={`app-tab-${tab}`}
      className="app-tab-panel"
      hidden={tab !== current}
    >
      {children}
    </div>
  );
}

export function AppDetailView({ app }: { app: string }) {
  const t = useT();
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
  const [manualDraft, setManualDraft] = useState(false);
  const [tab, setTab] = useState<Tab>("design");
  const [evalSet, setEvalSet] = useState<EvalSet | null>(null);
  useEffect(() => {
    // The tab is in the address (#release) so a link can open it; only known here.
    const follow = () => {
      const wanted = window.location.hash.slice(1);
      if (TABS.some(([key]) => key === wanted)) setTab(wanted as Tab);
    };
    const frame = requestAnimationFrame(follow);
    window.addEventListener("hashchange", follow);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("hashchange", follow);
    };
  }, []);
  function selectTab(next: Tab) {
    setTab(next);
    window.history.replaceState(null, "", `#${next}`);
  }
  const manualRevision = data
    ? JSON.stringify([data.current, data.draft, data.summary])
    : "";
  const manual = useManual(
    `/apps/${encodeURIComponent(app)}/manual${manualDraft && data?.draft ? "?draft=true" : ""}`,
    manualRevision,
  );
  const canvasManual = useManual(
    `/apps/${encodeURIComponent(app)}/manual${view === "draft" && data?.draft ? "?draft=true" : ""}`,
    manualRevision,
  );

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
              ? NOT_FOUND
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
      (dirty && !window.confirm(t("切换版本会丢弃未保存的修改，继续吗？")))
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
          const localIssues = [
            ...dataNodeIssues(config),
            ...knowledgeIssues(config, data.options.rerank_models ?? []),
          ];
          if (localIssues.length)
            throw new ApiError(t("请检查检索设置"), 422, localIssues);
          accept(
            await saveAppDraft(app, config),
            t("草稿已保存，上次评测结果已清空。"),
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
              t("草稿没有保存。"),
              count
                ? t("有 {count} 处问题，已标在对应的节点和设置下。", { count })
                : "",
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
            : t("缺少节点 {id} 的配置", { id: node.id }),
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
        t("草稿已保存，上次评测结果已清空。"),
      ),
    );
  }

  if (!data || !config)
    return (
      <div className="asset-page">
        <Link href="/apps" className="back-link">
          {t("← 应用")}
        </Link>
        {error ? (
          <ErrorNotice
            message={error === NOT_FOUND ? t(NOT_FOUND) : error}
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
    ? t("请先保存草稿。")
    : view !== "draft"
      ? t("请切换到草稿后发布。")
      : dirty
        ? t("有未保存的修改，请先保存草稿并重新评测。")
        : running
          ? t("评测运行中，请等待结果。")
          : data.has_eval_set && data.draft.eval?.status !== "passed"
            ? t("草稿须先通过评测才能发布；总分需达到阈值。")
            : "";
  const left = versions.find((item) => item.version === compare[0]);
  const right = versions.find((item) => item.version === compare[1]);

  return (
    <div className="asset-page">
      <Link href="/apps" className="back-link">
        {t("← 应用")}
      </Link>
      <AssetHeading
        eyebrow={data.kind === "flow" ? app : t("{app} · 代码应用", { app })}
        title={data.title}
        action={
          <div className="asset-actions heading-actions">
            <Link
              className="button secondary"
              href={`/use/${encodeURIComponent(app)}`}
            >
              {t("使用页")}
            </Link>
            <Link
              className="button primary"
              href={`/runs/new?app=${encodeURIComponent(app)}`}
            >
              {t("新建运行")}
            </Link>
          </div>
        }
      >
        {data.summary}
      </AssetHeading>
      {error && <ErrorNotice message={error} />}
      {notice && (
        <p className="asset-notice" role="status">
          {notice}
        </p>
      )}
      {/* The tabs and the version being edited share one row. */}
      <div className="app-tabbar">
        <AppTabs tab={tab} onChange={selectTab} />
        {(tab === "design" || tab === "release") && (
          <section
            className={`panel config-banner ${view}`}
            aria-label={t("正在查看的配置")}
          >
            <div>
              <strong>
                {view === "draft"
                  ? t("正在查看：草稿")
                  : t("正在查看：当前版本 v{current_version}", {
                      current_version: data.current_version,
                    })}
              </strong>
              <p>
                {dirty
                  ? t("有未保存的修改")
                  : view === "draft"
                    ? t("草稿已保存 · 当前发布版本 v{current_version}", {
                        current_version: data.current_version,
                      })
                    : data.kind === "flow"
                      ? t("新运行使用当前发布版本。")
                      : t("编辑节点设置后，可保存为草稿。")}
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
                    {t("查看当前版本")}
                  </button>
                  <button
                    type="button"
                    className="button secondary"
                    disabled={busy || view === "draft"}
                    onClick={() => switchView("draft")}
                  >
                    {t("查看草稿")}
                  </button>
                </>
              )}
              <button
                type="button"
                className="button primary"
                disabled={busy || running || !dirty}
                onClick={save}
              >
                {t("保存草稿")}
              </button>
              <button
                type="button"
                className="button secondary"
                disabled={busy || (!data.draft && !dirty)}
                onClick={() => {
                  if (!window.confirm(t("确定放弃草稿和未保存的修改吗？")))
                    return;
                  void action(async () => {
                    // With only unsaved edits there is no draft on the server to delete.
                    if (data.draft) await deleteAppDraft(app);
                    accept(await getApp(app), t("已放弃草稿。"));
                  });
                }}
              >
                {t("放弃草稿")}
              </button>
            </div>
          </section>
        )}
      </div>
      <TabPanel tab="overview" current={tab}>
        {data.kind === "flow" && (
          <AppMetadata
            data={data}
            changed={(value) => accept(value, t("应用信息已保存。"))}
          />
        )}
        <AppManualPanel
          resource={manual}
          controls={
            data.draft && (
              <div
                className="asset-actions manual-version"
                role="group"
                aria-label={t("说明书版本")}
              >
                <button
                  type="button"
                  className="button secondary"
                  aria-pressed={manualDraft}
                  onClick={() => setManualDraft(true)}
                >
                  {t("看草稿")}
                </button>
                <button
                  type="button"
                  className="button secondary"
                  aria-pressed={!manualDraft}
                  onClick={() => setManualDraft(false)}
                >
                  {t("看生效版本")}
                </button>
              </div>
            )
          }
        />
      </TabPanel>
      <TabPanel tab="design" current={tab}>
        {isFlowConfig(config) ? (
          <>
            <FlowEditor
              app={app}
              data={data}
              config={config}
              manual={canvasManual.manual}
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
          <section
            className="panel run-flow app-design"
            aria-label={t("应用设计")}
          >
            <div className="section-heading">
              <h2>{t("应用设计与节点配置")}</h2>
              <span className="muted">{t("点选节点查看设计与设置")}</span>
            </div>
            <div className="flow-about">
              <dl>
                <div>
                  <dt>{t("运行方式")}</dt>
                  <dd>{data.pattern}</dd>
                </div>
                <div>
                  <dt>{t("知识库")}</dt>
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
                  aria-label={t("节点 {title}", { title: node.title })}
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
                        setConfig({
                          nodes: { ...codeNodes, [node.id]: value },
                        });
                        setNotice("");
                      }}
                    />
                  ) : (
                    <p className="muted">{t("此节点没有可调设置。")}</p>
                  )}
                </div>
              )}
            </div>
          </section>
        )}
      </TabPanel>
      <TabPanel tab="release" current={tab}>
        <div className="asset-two-columns">
          <section className="panel asset-section" aria-label={t("发布前评测")}>
            <div className="asset-section-heading">
              <h2>{t("发布前评测")}</h2>
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
                        t("评测已开始，每 5 秒更新结果。"),
                      ),
                    )
                  }
                >
                  {running ? t("评测运行中…") : t("运行评测")}
                </button>
              )}
            </div>
            {data.has_eval_set ? (
              <>
                <p className="muted">
                  {view === "draft"
                    ? t("当前展示草稿最近一次评测。")
                    : t("当前展示版本 v{version} 发布时的评测。", {
                        version: data.current_version,
                      })}
                </p>
                <Evaluation
                  evaluation={displayedEval}
                  stale={
                    view === "draft" &&
                    Boolean(
                      evalSet?.updated_at &&
                      displayedEval &&
                      Date.parse(evalSet.updated_at) >
                        Date.parse(displayedEval.started_at),
                    )
                  }
                />
              </>
            ) : (
              <p className="muted">
                {t("此应用没有评测集，保存草稿后可直接发布。")}
              </p>
            )}
          </section>
          <section className="panel asset-section" aria-label={t("发布应用")}>
            <h2>{t("发布应用")}</h2>
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
                    t("应用已发布为新版本。"),
                  );
                  setNote("");
                  await refreshHistory();
                });
              }}
            >
              <label className="asset-field">
                {t("发布说明（必填）")}
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
                {t("发布新版本")}
              </button>
            </form>
          </section>
        </div>
        <EvalSetPanel
          app={app}
          config={
            data.kind === "flow"
              ? ((data.draft?.config ?? data.current.config) as FlowConfig)
              : null
          }
          options={data.options}
          onSaved={(value) => {
            setEvalSet(value);
            const gated = value.source === "file" || value.cases.length > 0;
            setData((now) =>
              now && now.has_eval_set !== gated
                ? { ...now, has_eval_set: gated }
                : now,
            );
          }}
        />
        <AppBudget app={app} title={data.title} />
        <section className="panel asset-section" aria-label={t("版本历史")}>
          <h2>{t("版本历史")}</h2>
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
                    {version.status === "current"
                      ? t("当前版本")
                      : t("历史版本")}
                  </strong>
                  <p>{version.note}</p>
                  <small>
                    {version.created_by ?? t("未署名")} ·{" "}
                    <AssetTime value={version.created_at} /> ·{" "}
                    {t("评测分 {score}", {
                      score: scoreText(version.eval?.score),
                    })}
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
                    {t("导出")}
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
                          t(
                            "确定将 v{version} 设为当前版本吗？新运行将使用该版本。",
                            { version: version.version ?? "—" },
                          ),
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
                          t("已将 v{version} 设为当前版本。", {
                            version: version.version ?? "—",
                          }),
                        );
                        await refreshHistory();
                      });
                    }}
                  >
                    {t("设为当前版本")}
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
          {left && right && (
            <ConfigDiff left={left} right={right} data={data} />
          )}
        </section>
      </TabPanel>
      <TabPanel tab="access" current={tab}>
        <AppAccess app={app} inputs={data.inputs} />
        <AppSchedules
          app={app}
          inputs={data.inputs}
          uses={data.project ?? null}
          datasources={data.options.datasources}
        />
      </TabPanel>
    </div>
  );
}
