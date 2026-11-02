"use client";

import { useEffect, useRef, useState } from "react";
import {
  deleteModel,
  getModelUsage,
  keyLabel,
  listModels,
  providerLabel,
  purposeLabels,
  setDefaults,
  testModel,
  type Model,
  type ModelList as ModelListData,
  type ModelUsage,
  type Purpose,
  type TestResult,
} from "../lib/models";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { DatasourceDialog } from "./datasource-form";
import { ModelForm } from "./model-form";
import { ErrorNotice } from "./ui";

const NO_DB =
  "模型网关没有接数据库，这里只能查看配置文件里的模型，不能新增。请按部署说明给网关配置 DATABASE_URL 和 LITELLM_SALT_KEY 后重启。";
const DOWN = "连不上模型网关，看不到模型清单。请检查网关服务是否在运行。";
const purposes: Purpose[] = ["chat", "embedding", "rerank"];

const number = new Intl.NumberFormat("zh-CN");
const tokens = (value: number) =>
  value >= 10_000
    ? `${(value / 10_000).toFixed(value >= 100_000 ? 0 : 1)} 万`
    : number.format(value);
const time = (value: string) =>
  new Date(value).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

type Tested = TestResult | "failed" | "running";
type Dialog = { kind: "create" } | { kind: "edit"; model: Model };

function UsageCell({ model }: { model: Model }) {
  const { usage } = model;
  if (!usage.calls) return <span className="muted">近 30 天没有调用</span>;
  return (
    <span className="model-usage">
      <span>
        {number.format(usage.calls)} 次
        {usage.errors > 0 && (
          <span className="model-errors">，失败 {usage.errors}</span>
        )}
      </span>
      <span className="muted">
        {tokens(usage.prompt_tokens)} / {tokens(usage.completion_tokens)} token
        {usage.cost_usd !== null && ` · $${usage.cost_usd.toFixed(2)}`}
      </span>
      {usage.last_call_at && (
        <span className="muted">最近 {time(usage.last_call_at)}</span>
      )}
    </span>
  );
}

function TestCell({ model, tested }: { model: Model; tested?: Tested }) {
  if (tested === "running")
    return <span className="datasource-health pending">测试中…</span>;
  if (tested === "failed")
    return <span className="datasource-health error">没测成，请重试</span>;
  if (tested)
    return tested.ok ? (
      <span className="datasource-health ok">
        正常 · {tested.latency_ms} 毫秒
      </span>
    ) : (
      <span className="datasource-health error model-problem">
        {tested.problem?.message ?? "测试没有通过"}
      </span>
    );
  if (model.usage.last_error)
    return (
      <span className="datasource-health error model-problem">
        最近一次调用失败：{model.usage.last_error}
      </span>
    );
  return model.usage.calls ? (
    <span className="datasource-health ok">最近调用正常</span>
  ) : (
    <span className="muted">—</span>
  );
}

function UsageList({ alias }: { alias: string }) {
  const [usage, setUsage] = useState<ModelUsage | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    getModelUsage(alias, controller.signal)
      .then(setUsage)
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [alias]);
  if (error) return <p className="field-error">{error}</p>;
  if (!usage) return <p className="muted">正在统计…</p>;
  if (!usage.apps.length)
    return <p className="muted">没有应用的当前版本在用它。</p>;
  return (
    <ul className="model-app-list">
      {usage.apps.map((app) => (
        <li key={app.app}>
          <strong>{app.title}</strong>
          <span className="muted">
            {" "}
            v{app.version} · {app.nodes.join("、")}
            {app.via === "default" && "（没指定模型，用的是平台默认）"}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ModelTable({
  purpose,
  models,
  tested,
  writable,
  test,
  edit,
  remove,
}: {
  purpose: Purpose;
  models: Model[];
  tested: Record<string, Tested>;
  writable: boolean;
  test: (model: Model) => void;
  edit: (model: Model) => void;
  remove: (model: Model) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="table-scroll">
      <table
        className="mapping-table model-table"
        aria-label={purposeLabels[purpose]}
      >
        <thead>
          <tr>
            {["模型", "供应商和型号", "key", "近 30 天", "状态", "操作"].map(
              (heading) => (
                <th scope="col" key={heading}>
                  {heading}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {models.map((model) => {
            const state = tested[model.alias];
            const page = model.source === "page";
            return [
              <tr key={model.alias}>
                <td className="model-name">
                  <div className="model-name-stack">
                    <strong>{model.display_name}</strong>
                    <code>{model.alias}</code>
                    <span className="model-tags">
                      {model.is_default && (
                        <span className="datasource-tag model-default">
                          默认
                        </span>
                      )}
                      {!page && (
                        <span className="datasource-tag">配置文件</span>
                      )}
                      {!model.allowed && (
                        <span className="datasource-tag undeclared">
                          外发策略不允许
                        </span>
                      )}
                    </span>
                    {model.description && (
                      <span className="muted model-description">
                        {model.description}
                      </span>
                    )}
                  </div>
                </td>
                <td>
                  {providerLabel(model.provider)}
                  <span className="muted model-id">{model.model}</span>
                </td>
                <td className="model-key-cell">{keyLabel(model.key)}</td>
                <td>
                  <UsageCell model={model} />
                </td>
                <td aria-live="polite">
                  <TestCell model={model} tested={state} />
                </td>
                <td className="model-actions">
                  {purpose !== "rerank" && (
                    <button
                      type="button"
                      className="text-button"
                      aria-label={`测试「${model.display_name}」`}
                      aria-disabled={state === "running" || undefined}
                      onClick={() => test(model)}
                    >
                      测试
                    </button>
                  )}
                  <button
                    type="button"
                    className="text-button"
                    aria-expanded={open === model.alias}
                    aria-label={`哪些应用在用「${model.display_name}」`}
                    onClick={() =>
                      setOpen(open === model.alias ? null : model.alias)
                    }
                  >
                    在用的应用
                  </button>
                  {page && writable && (
                    <>
                      <button
                        type="button"
                        className="text-button"
                        aria-label={`修改「${model.display_name}」`}
                        onClick={() => edit(model)}
                      >
                        修改
                      </button>
                      <button
                        type="button"
                        className="text-button danger-text"
                        aria-label={`删除「${model.display_name}」`}
                        onClick={() => remove(model)}
                      >
                        删除
                      </button>
                    </>
                  )}
                </td>
              </tr>,
              open === model.alias && (
                <tr key={`${model.alias}-usage`} className="model-usage-row">
                  <td colSpan={6}>
                    <UsageList alias={model.alias} />
                  </td>
                </tr>
              ),
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

function DefaultPicker({
  purpose,
  data,
  onSaved,
}: {
  purpose: "chat" | "embedding";
  data: ModelListData;
  onSaved: (alias: string) => void;
}) {
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const id = `default-${purpose}`;
  const choices = data.models.filter((model) => model.purpose === purpose);
  const current = data.defaults[purpose];
  return (
    <div className="asset-field">
      <label htmlFor={id}>默认{purposeLabels[purpose]}</label>
      <select
        id={id}
        value={current}
        disabled={saving || data.gateway === "down"}
        aria-describedby={`${id}-hint`}
        onChange={async (event) => {
          const alias = event.target.value;
          setSaving(true);
          setNote("");
          setError("");
          try {
            await setDefaults({ [purpose]: alias });
            onSaved(alias);
            setNote("已保存。");
          } catch (err) {
            setError(errorMessage(err));
          } finally {
            setSaving(false);
          }
        }}
      >
        {!choices.some((model) => model.alias === current) && (
          <option value={current}>{current}（网关上没有）</option>
        )}
        {choices.map((model) => (
          <option key={model.alias} value={model.alias}>
            {model.display_name === model.alias
              ? model.alias
              : `${model.display_name}（${model.alias}）`}
            {!model.allowed ? "（外发策略不允许）" : ""}
          </option>
        ))}
      </select>
      <p className="field-hint" id={`${id}-hint`} role="status">
        {saving
          ? "正在保存…"
          : error ||
            note ||
            (purpose === "chat"
              ? "节点没指定模型时用它。"
              : "新建知识库时默认用它；已有的知识库不变。")}
      </p>
    </div>
  );
}

export function ModelList() {
  const [data, setData] = useState<ModelListData | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [tested, setTested] = useState<Record<string, Tested>>({});
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    listModels(controller.signal)
      .then((value) => {
        setData(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [revision]);

  async function test(model: Model) {
    if (tested[model.alias] === "running") return;
    setTested((old) => ({ ...old, [model.alias]: "running" }));
    let result: Tested;
    try {
      result = await testModel(model.alias);
    } catch {
      result = "failed";
    }
    setTested((old) => ({ ...old, [model.alias]: result }));
  }

  async function remove(model: Model) {
    if (
      !window.confirm(
        `确定删除「${model.display_name}」吗？网关上保存的 key 也会一起删掉。`,
      )
    )
      return;
    setActionError("");
    setNotice("");
    try {
      await deleteModel(model.alias);
      setNotice(`已删除「${model.display_name}」。`);
      setRevision((n) => n + 1);
    } catch (err) {
      setActionError(errorMessage(err));
    }
  }

  const writable = data?.gateway === "ok";
  return (
    <div className="asset-page">
      <AssetHeading title="模型">
        应用和知识库都按别名调用模型，调用一律经过平台的模型网关。在这里接入
        Claude、OpenAI、通义千问、DeepSeek
        或公司自己的模型网关，加上就能在节点里选； key
        由网关加密保存，平台不保存 key。写在网关配置文件里的模型只能查看。
      </AssetHeading>
      <div className="asset-actions">
        <button
          type="button"
          className="button primary"
          disabled={!writable}
          aria-describedby={data && !writable ? "model-gateway" : undefined}
          onClick={() => {
            setNotice("");
            setDialog({ kind: "create" });
          }}
        >
          添加模型
        </button>
      </div>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!data && !error && <AssetLoading />}
      {data && data.gateway !== "ok" && (
        <p
          id="model-gateway"
          className={`datasource-note ${data.gateway === "no_db" ? "warn" : ""}`}
          role="status"
        >
          {data.gateway === "no_db" ? NO_DB : DOWN}
        </p>
      )}
      {notice && (
        <p className="datasource-note" role="status">
          {notice}
        </p>
      )}
      {actionError && <ErrorNotice message={actionError} />}
      {data && data.gateway !== "down" && (
        <>
          <section
            className="panel asset-section model-defaults"
            aria-labelledby="model-defaults"
          >
            <h2 id="model-defaults">平台默认</h2>
            <div className="model-default-fields">
              {(["chat", "embedding"] as const).map((purpose) => (
                <DefaultPicker
                  key={purpose}
                  purpose={purpose}
                  data={data}
                  onSaved={(alias) =>
                    setData(
                      (old) =>
                        old && {
                          ...old,
                          defaults: { ...old.defaults, [purpose]: alias },
                          models: old.models.map((model) =>
                            model.purpose === purpose
                              ? { ...model, is_default: model.alias === alias }
                              : model,
                          ),
                        },
                    )
                  }
                />
              ))}
            </div>
          </section>
          {purposes.map((purpose) => {
            const models = data.models.filter(
              (model) => model.purpose === purpose,
            );
            if (!models.length) return null;
            return (
              <section
                key={purpose}
                className="panel asset-section datasource-section"
                aria-labelledby={`model-group-${purpose}`}
              >
                <div className="asset-section-heading">
                  <h2 id={`model-group-${purpose}`}>
                    {purposeLabels[purpose]}
                    <span className="muted"> {models.length} 个</span>
                  </h2>
                </div>
                <ModelTable
                  purpose={purpose}
                  models={models}
                  tested={tested}
                  writable={writable}
                  test={(model) => void test(model)}
                  edit={(model) => {
                    setNotice("");
                    setDialog({ kind: "edit", model });
                  }}
                  remove={(model) => void remove(model)}
                />
              </section>
            );
          })}
        </>
      )}
      {dialog && (
        <DatasourceDialog
          ref={dialogRef}
          title={
            dialog.kind === "create"
              ? "添加模型"
              : `修改「${dialog.model.display_name}」`
          }
          onClose={() => setDialog(null)}
        >
          {dialog.kind === "create" ? (
            <ModelForm
              mode="create"
              onSaved={(model) => {
                dialogRef.current?.close();
                setNotice(
                  `已添加「${model.display_name}」，测试通过，现在可以在节点里选它了。`,
                );
                setRevision((n) => n + 1);
              }}
            />
          ) : (
            <ModelForm
              mode="edit"
              initial={dialog.model}
              onSaved={(model) => {
                dialogRef.current?.close();
                setNotice(`已保存「${model.display_name}」。`);
                setTested((old) =>
                  Object.fromEntries(
                    Object.entries(old).filter(
                      ([alias]) => alias !== model.alias,
                    ),
                  ),
                );
                setRevision((n) => n + 1);
              }}
            />
          )}
        </DatasourceDialog>
      )}
    </div>
  );
}
