"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { createRun, errorMessage, fieldErrors, requestJson } from "../lib/runs";
import { readDataSources, type DataSource, type EtlLanguage } from "../lib/etl";
import { Icon } from "./icons";
import { ErrorNotice } from "./ui";
import { FileUpload } from "./file-upload";
import { getApp, listApps, type AppDetail, type AppSummary } from "../lib/apps";
import {
  initialValues,
  inputPayload,
  validateFields,
  type FieldValues,
} from "../lib/input-fields";
import { InputFields } from "./input-fields";
import { savedActor, saveActor } from "../lib/actor";

export function NewRun() {
  const router = useRouter();
  const [text, setText] = useState("");
  const [app, setApp] = useState("");
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [detail, setDetail] = useState<AppDetail | null>(null);
  const [values, setValues] = useState<FieldValues>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [appsRevision, setAppsRevision] = useState(0);
  const [appsError, setAppsError] = useState("");
  const [sources, setSources] = useState<DataSource[]>([]);
  const [datasourceId, setDatasourceId] = useState("");
  const [language, setLanguage] = useState<EtlLanguage>("pyspark");
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [sourcesError, setSourcesError] = useState("");
  const [sourcesRevision, setSourcesRevision] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const requester = useRef<HTMLInputElement>(null);
  const busy = useRef(false);
  // Filled in after mounting: the server has no storage.
  useEffect(() => {
    if (requester.current && !requester.current.value)
      requester.current.value = savedActor();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    async function loadSources() {
      setSourcesLoading(true);
      setSourcesError("");
      try {
        const value = readDataSources(
          await requestJson("/datasources", {
            signal: controller.signal,
          }),
        );
        if (!controller.signal.aborted) {
          setSources(value);
          setDatasourceId((id) =>
            value.some((source) => source.datasource_id === id) ? id : "",
          );
        }
      } catch (err) {
        if (!controller.signal.aborted) setSourcesError(errorMessage(err));
      } finally {
        if (!controller.signal.aborted) setSourcesLoading(false);
      }
    }
    void loadSources();
    return () => controller.abort();
  }, [app, sourcesRevision]);
  useEffect(() => {
    const controller = new AbortController();
    listApps(controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return;
        setApps(items);
        setAppsError("");
        const requested =
          new URLSearchParams(window.location.search).get("app") ?? "hello";
        setApp((previous) =>
          items.some((item) => item.app === previous)
            ? previous
            : (items.find((item) => item.app === requested)?.app ??
              items[0]?.app ??
              ""),
        );
      })
      .catch((err) => {
        if (!controller.signal.aborted) setAppsError(errorMessage(err));
      });
    return () => controller.abort();
  }, [appsRevision]);
  useEffect(() => {
    if (!app) return;
    const controller = new AbortController();
    getApp(app, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setDetail(value);
        setValues(initialValues(value.inputs ?? []));
        setErrors({});
        setAppsError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setAppsError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app, appsRevision]);
  const fields = detail?.app === app ? (detail.inputs ?? []) : [];
  const needsSource =
    app === "etl" &&
    (sourcesLoading ||
      Boolean(sourcesError) ||
      !sources.some((source) => source.datasource_id === datasourceId));
  const selectedSource = sources.find(
    (source) => source.datasource_id === datasourceId,
  );
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      busy.current ||
      uploading ||
      needsSource ||
      !detail ||
      detail.app !== app ||
      (app === "etl" && !text.trim())
    )
      return;
    const issues = app === "etl" ? [] : validateFields(fields, values, "input");
    const nextErrors = Object.fromEntries(
      issues.map((issue) => [issue.loc[2], issue.msg]),
    );
    setErrors(nextErrors);
    if (issues.length) return;
    busy.current = true;
    setSubmitting(true);
    setError("");
    const name = requester.current?.value.trim() ?? "";
    if (name) saveActor(name);
    try {
      const receipt = await createRun({
        app,
        input:
          app === "etl"
            ? { datasource_id: datasourceId, request: text.trim(), language }
            : inputPayload(fields, values),
        ...(name ? { requester: name } : {}),
      });
      router.push(`/runs/${encodeURIComponent(receipt.run_id)}`);
    } catch (err) {
      setError(errorMessage(err));
      setErrors(fieldErrors(err, "input"));
      busy.current = false;
      setSubmitting(false);
    }
  }
  return (
    <>
      <Link className="back-link" href="/">
        <Icon name="back" size={16} />
        返回运行记录
      </Link>
      <div className="page-heading">
        <div>
          <h1>新建运行</h1>
          <p>告诉应用你想完成什么，接下来的过程在这里展开。</p>
        </div>
      </div>
      <div className="new-run-grid">
        <form
          className="panel new-run-form"
          onSubmit={submit}
          noValidate
          aria-label="新建运行"
        >
          <div className="form-section">
            <span className="step-label">
              01<span>选择应用</span>
            </span>
            <label htmlFor="app">应用</label>
            <select
              id="app"
              name="app"
              value={app}
              onChange={(event) => {
                setApp(event.target.value);
                setDetail(null);
                setErrors({});
                setError("");
              }}
              disabled={submitting || uploading}
            >
              {!apps.length && (
                <option value="">
                  {appsError ? "应用加载失败" : "正在加载应用…"}
                </option>
              )}
              {apps.map((item) => (
                <option key={item.app} value={item.app}>
                  {item.app} · {item.title}
                </option>
              ))}
            </select>
            <p className="field-hint">
              {app === "etl"
                ? "探查数据、确认方案，生成 Databricks Lakeflow 管道代码。"
                : "接收需求、请求人工确认，并返回运行结果。"}
            </p>
          </div>
          {appsError && (
            <ErrorNotice
              message={appsError}
              onRetry={() => setAppsRevision((value) => value + 1)}
            />
          )}
          {app === "etl" && (
            <div className="form-section">
              <span className="step-label">
                02<span>选择数据源</span>
              </span>
              <div className="source-heading">
                <label htmlFor="datasource">数据源</label>
                <button
                  type="button"
                  className="text-button"
                  disabled={submitting || uploading}
                  aria-expanded={uploadOpen}
                  aria-controls="file-upload-panel"
                  onClick={() => setUploadOpen((value) => !value)}
                >
                  {uploadOpen ? "收起上传" : "上传文件"}
                </button>
              </div>
              <select
                id="datasource"
                required
                value={datasourceId}
                onChange={(event) => setDatasourceId(event.target.value)}
                disabled={
                  submitting ||
                  uploading ||
                  sourcesLoading ||
                  Boolean(sourcesError)
                }
                aria-describedby="datasource-hint"
              >
                <option value="">
                  {sourcesLoading ? "正在加载数据源…" : "请选择数据源"}
                </option>
                {sources.map((source) => (
                  <option
                    key={source.datasource_id}
                    value={source.datasource_id}
                  >
                    {source.name} · {source.kind}
                  </option>
                ))}
              </select>
              {uploadOpen && (
                <div id="file-upload-panel">
                  <FileUpload
                    disabled={submitting}
                    onBusy={setUploading}
                    onUploaded={({ datasource }) => {
                      setSources((value) => [
                        ...value.filter(
                          (source) =>
                            source.datasource_id !== datasource.datasource_id,
                        ),
                        datasource,
                      ]);
                      setDatasourceId(datasource.datasource_id);
                      setSourcesError("");
                      setSourcesRevision((value) => value + 1);
                    }}
                  />
                </div>
              )}
              <p className="field-hint" id="datasource-hint">
                {selectedSource?.description ||
                  "从已登记的数据源中选择，连接器只读探查源数据。"}
              </p>
              {sourcesError && (
                <ErrorNotice
                  message={sourcesError}
                  onRetry={() => setSourcesRevision((value) => value + 1)}
                />
              )}
              {!sourcesLoading && !sourcesError && sources.length === 0 && (
                <p className="field-hint" role="status">
                  暂无可用数据源，请先登记数据源后刷新。
                </p>
              )}
              {!sourcesLoading && !sourcesError && (
                <button
                  type="button"
                  className="text-button source-refresh"
                  disabled={submitting || uploading}
                  onClick={() => setSourcesRevision((value) => value + 1)}
                >
                  <Icon name="refresh" size={14} />
                  刷新数据源
                </button>
              )}
            </div>
          )}
          {app !== "etl" && (
            <div className="form-section">
              <InputFields
                fields={fields}
                values={values}
                onChange={(next) => {
                  setValues(next);
                  setErrors({});
                }}
                errors={errors}
                disabled={submitting || uploading}
                datasources={sources}
              />
              {fields.some((field) => field.type === "datasource") &&
                sourcesError && (
                  <ErrorNotice
                    message={sourcesError}
                    onRetry={() => setSourcesRevision((value) => value + 1)}
                  />
                )}
            </div>
          )}
          {app === "etl" && (
            <div className="form-section">
              <span className="step-label">
                03
                <span>描述需求</span>
              </span>
              <label htmlFor="request">你想完成什么？</label>
              <textarea
                id="request"
                name="text"
                required
                rows={7}
                placeholder="例如：请整理一份客户数据清洗方案，并在执行前让我确认。"
                value={text}
                onChange={(e) => setText(e.target.value)}
                disabled={submitting || needsSource}
                aria-describedby="request-hint"
              />
              <p className="field-hint" id="request-hint">
                尽量说明目标与约束，帮助应用理解你的需求。
              </p>
            </div>
          )}
          {app === "etl" && (
            <div className="form-section">
              <label htmlFor="language">
                生成语言<span className="muted">（选填）</span>
              </label>
              <select
                id="language"
                value={language}
                disabled={submitting}
                onChange={(event) =>
                  setLanguage(event.target.value as EtlLanguage)
                }
              >
                <option value="pyspark">PySpark（默认）</option>
                <option value="sql">SQL</option>
              </select>
            </div>
          )}
          <div className="form-section">
            <label htmlFor="requester">
              你的名字<span className="muted">（选填）</span>
            </label>
            <input
              id="requester"
              className="requester-input"
              autoComplete="name"
              maxLength={100}
              ref={requester}
              disabled={submitting}
              aria-describedby="requester-hint"
            />
            <p className="field-hint" id="requester-hint">
              会显示在运行记录里，方便大家知道这个需求是谁提的。
            </p>
          </div>
          {error && <ErrorNotice message={error} />}
          <div className="form-actions">
            <span>运行过程与结果将自动记录</span>
            <button
              className="button primary"
              type="submit"
              disabled={
                submitting ||
                uploading ||
                !detail ||
                detail.app !== app ||
                Boolean(appsError) ||
                (app === "etl" && !text.trim()) ||
                (app === "hello" && !String(values.text ?? "").trim()) ||
                needsSource
              }
            >
              {submitting ? (
                <>
                  <span className="spinner" />
                  正在创建…
                </>
              ) : (
                <>
                  开始运行
                  <Icon name="arrow" size={17} />
                </>
              )}
            </button>
          </div>
        </form>
        <aside className="guide-card">
          <span className="guide-icon">
            <Icon name="spark" size={24} />
          </span>
          <h2>让需求有迹可循</h2>
          <p>每一次运行，都是一段完整的协作。</p>
          <ol className="guide-steps">
            <li>
              <strong>描述目标</strong>
              <span>用自然语言提出你的需求</span>
            </li>
            <li>
              <strong>查看过程</strong>
              <span>跟随应用消息，了解执行进展</span>
            </li>
            <li>
              <strong>确认方向</strong>
              <span>需要时确认继续，或驳回并给出意见</span>
            </li>
            <li>
              <strong>获得结果</strong>
              <span>随时回来查看本次运行</span>
            </li>
          </ol>
        </aside>
      </div>
    </>
  );
}
