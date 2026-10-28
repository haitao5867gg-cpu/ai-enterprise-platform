"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import {
  APP_ID_PATTERN,
  createApp,
  getAppTemplates,
  getAppVersions,
  importApp,
  type AppSource,
  type AppSummary,
  type AppTemplate,
  type AppVersion,
  type ImportResult,
} from "../lib/apps";
import { actorPayload } from "../lib/actor";
import { errorMessage } from "../lib/runs";
import { ErrorNotice } from "./ui";

export function AppCreate({
  apps,
  importing,
  close,
}: {
  apps: AppSummary[];
  importing: boolean;
  close: () => void;
}) {
  const router = useRouter();
  const [templates, setTemplates] = useState<AppTemplate[]>([]);
  const [source, setSource] = useState("blank");
  const [step, setStep] = useState(importing ? 2 : 1);
  const [copyApp, setCopyApp] = useState(
    apps.find((app) => app.kind === "flow")?.app ?? "",
  );
  const [versions, setVersions] = useState<AppVersion[]>([]);
  const [version, setVersion] = useState("");
  const [app, setApp] = useState("");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [yaml, setYaml] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  useEffect(() => {
    if (importing) return;
    const controller = new AbortController();
    getAppTemplates(controller.signal)
      .then(setTemplates)
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [importing]);
  useEffect(() => {
    if (source !== "copy" || !copyApp) return;
    const controller = new AbortController();
    getAppVersions(copyApp, controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) {
          setVersions(items);
          setVersion(
            String(
              items.find((item) => item.status === "current")?.version ??
                items[0]?.version ??
                "",
            ),
          );
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [source, copyApp]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!APP_ID_PATTERN.test(app)) {
      setError(
        "标识须以小写字母开头，使用小写字母、数字或连字符，共 2–64 字符。",
      );
      return;
    }
    if (!importing && (!title.trim() || title.length > 50)) {
      setError("名称须为 1–50 个字符。");
      return;
    }
    if (summary.length > 500) {
      setError("简介最多 500 个字符。");
      return;
    }
    if (importing && !yaml) {
      setError("请选择应用 YAML 文件。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (importing) {
        const imported = await importApp({
          app,
          ...(title.trim() ? { title: title.trim() } : {}),
          ...(summary ? { summary } : {}),
          yaml,
          ...actorPayload(),
        });
        if (imported.problems.length) setResult(imported);
        else router.push(`/apps/${encodeURIComponent(imported.app.app)}`);
      } else {
        const from: AppSource =
          source === "blank"
            ? { blank: true }
            : source === "copy"
              ? { app: copyApp, version: Number(version) }
              : { template: source };
        const created = await createApp({
          app,
          title: title.trim(),
          summary,
          from,
          ...actorPayload(),
        });
        router.push(`/apps/${encodeURIComponent(created.app)}`);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="panel asset-section"
      aria-label={importing ? "导入应用" : "新建应用"}
    >
      <div className="asset-section-heading">
        <h2>{importing ? "导入应用" : "新建应用"}</h2>
        <button
          type="button"
          className="text-button"
          onClick={close}
          disabled={busy}
        >
          关闭
        </button>
      </div>
      {result ? (
        <div role="status">
          <h3>应用已导入，以下引用需要补齐</h3>
          <ul className="import-problems">
            {result.problems.map((problem, i) => (
              <li key={i}>
                <strong>
                  {problem.kind === "skill"
                    ? `Skill：${problem.name} v${problem.version}`
                    : problem.kind === "knowledge_base"
                      ? `知识库：${problem.name ?? problem.kb_id}`
                      : problem.kind === "datasource"
                        ? `数据源：${problem.name ?? problem.datasource_id}`
                        : problem.kind === "model"
                          ? `模型：${problem.name}`
                          : `密钥：${problem.name}`}
                </strong>
                ：{problem.message}
              </li>
            ))}
          </ul>
          <Link
            className="button primary"
            href={`/apps/${encodeURIComponent(result.app.app)}`}
          >
            查看应用详情
          </Link>
        </div>
      ) : step === 1 ? (
        <>
          <p className="muted">选择来源，再填写应用信息。</p>
          <div
            className="template-grid"
            role="radiogroup"
            aria-label="应用来源"
          >
            {[
              {
                id: "blank",
                title: "空白应用",
                summary: "从开始、模型回答、结束三个节点开始。",
              },
              ...templates,
              {
                id: "copy",
                title: "复制已有编排应用",
                summary: "选择一个已发布版本，作为新应用的起点。",
              },
            ].map((item) => (
              <label
                className={`template-card ${source === item.id ? "selected" : ""}`}
                key={item.id}
              >
                <input
                  type="radio"
                  name="source"
                  value={item.id}
                  checked={source === item.id}
                  onChange={() => {
                    setSource(item.id);
                    setError("");
                  }}
                />
                <strong>{item.title}</strong>
                <span>{item.summary}</span>
                {"needs" in item && item.needs.includes("knowledge_base") && (
                  <small>创建后需选择知识库</small>
                )}
                {"needs" in item && item.needs.includes("datasource") && (
                  <small>创建后需选择数据源</small>
                )}
              </label>
            ))}
          </div>
          {source === "copy" && (
            <div className="asset-form">
              <label className="asset-field">
                来源应用
                <select
                  value={copyApp}
                  onChange={(e) => {
                    setCopyApp(e.target.value);
                    setVersion("");
                  }}
                >
                  <option value="">请选择编排应用</option>
                  {apps
                    .filter((item) => item.kind === "flow")
                    .map((item) => (
                      <option key={item.app} value={item.app}>
                        {item.title} · {item.app}
                      </option>
                    ))}
                </select>
              </label>
              <label className="asset-field">
                来源版本
                <select
                  value={version}
                  onChange={(e) => setVersion(e.target.value)}
                >
                  {versions.map((item) => (
                    <option key={item.version} value={String(item.version)}>
                      v{item.version}
                      {item.status === "current" ? "（当前版本）" : ""}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
          <div className="asset-actions">
            <button
              className="button primary"
              type="button"
              disabled={source === "copy" && (!copyApp || !version)}
              onClick={() => {
                setStep(2);
                if (!title)
                  setTitle(
                    templates.find((item) => item.id === source)?.title ?? "",
                  );
              }}
            >
              下一步
            </button>
          </div>
        </>
      ) : (
        <form className="asset-form" onSubmit={submit} noValidate>
          {importing && (
            <label className="asset-field">
              应用文件（.yaml / .yml）
              <input
                type="file"
                accept=".yaml,.yml"
                disabled={busy}
                onChange={async (event) => {
                  const file = event.target.files?.[0];
                  setYaml("");
                  setError("");
                  if (!file) return;
                  if (!/\.ya?ml$/i.test(file.name) || file.size > 1024 * 1024) {
                    setError("请选择不超过 1 MB 的 .yaml 或 .yml 文件。");
                    return;
                  }
                  try {
                    setYaml(await file.text());
                  } catch {
                    setError("无法读取文件，请重新选择。");
                  }
                }}
              />
            </label>
          )}
          <div className="asset-field">
            <label htmlFor="new-app-id">标识</label>
            <input
              id="new-app-id"
              value={app}
              onChange={(e) => setApp(e.target.value)}
              disabled={busy}
              maxLength={64}
              autoComplete="off"
            />
            <span className="field-hint">
              2–64 字符，小写字母开头，可用小写字母、数字和连字符。
            </span>
          </div>
          <label className="asset-field">
            名称{importing ? "（选填，默认使用文件中的名称）" : ""}
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={busy}
              maxLength={50}
            />
          </label>
          <label className="asset-field">
            简介
            <textarea
              rows={3}
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              disabled={busy}
              maxLength={500}
            />
          </label>
          <div className="asset-actions">
            {!importing && (
              <button
                className="button secondary"
                type="button"
                onClick={() => setStep(1)}
                disabled={busy}
              >
                上一步
              </button>
            )}
            <button className="button primary" disabled={busy}>
              {busy ? "正在提交…" : importing ? "导入应用" : "创建应用"}
            </button>
          </div>
        </form>
      )}
      {error && <ErrorNotice message={error} />}
    </section>
  );
}
