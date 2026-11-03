"use client";

import { useT } from "../lib/i18n/client";
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
import { useLocale } from "../lib/i18n/client";
import {
  formatCompact,
  formatNumber,
  formatShortTime,
} from "../lib/i18n/format";

const NO_DB =
  "模型网关没有接数据库，这里只能查看配置文件里的模型，不能新增。请按部署说明给网关配置 DATABASE_URL 和 LITELLM_SALT_KEY 后重启。";
const DOWN = "连不上模型网关，看不到模型清单。请检查网关服务是否在运行。";
const purposes: Purpose[] = ["chat", "embedding", "rerank"];

type Tested = TestResult | "failed" | "running";
type Dialog = { kind: "create" } | { kind: "edit"; model: Model };

function UsageCell({ model }: { model: Model }) {
  const t = useT();
  const { locale } = useLocale();
  const { usage } = model;
  if (!usage.calls)
    return <span className="muted">{t("近 30 天没有调用")}</span>;
  return (
    <span className="model-usage">
      <span>
        {t("{n} 次", { n: formatNumber(locale, usage.calls) })}
        {usage.errors > 0 && (
          <span className="model-errors">
            {t("，失败 {n}", { n: usage.errors })}
          </span>
        )}
      </span>
      <span className="muted">
        {t("{input} / {output} token", {
          input: formatCompact(locale, usage.prompt_tokens),
          output: formatCompact(locale, usage.completion_tokens),
        })}
        {usage.cost_usd !== null && ` · $${usage.cost_usd.toFixed(2)}`}
      </span>
      {usage.last_call_at && (
        <span className="muted">
          {t("最近 {time}", {
            time: formatShortTime(locale, usage.last_call_at),
          })}
        </span>
      )}
    </span>
  );
}

function TestCell({ model, tested }: { model: Model; tested?: Tested }) {
  const t = useT();
  if (tested === "running")
    return <span className="datasource-health pending">{t("测试中…")}</span>;
  if (tested === "failed")
    return (
      <span className="datasource-health error">{t("没测成，请重试")}</span>
    );
  if (tested)
    return tested.ok ? (
      <span className="datasource-health ok">
        {t("正常 · {ms} 毫秒", { ms: tested.latency_ms ?? "—" })}
      </span>
    ) : (
      <span className="datasource-health error model-problem">
        {tested.problem?.message ?? t("测试没有通过")}
      </span>
    );
  if (model.usage.last_error)
    return (
      <span className="datasource-health error model-problem">
        {t("最近一次调用失败：{error}", { error: model.usage.last_error })}
      </span>
    );
  return model.usage.calls ? (
    <span className="datasource-health ok">{t("最近调用正常")}</span>
  ) : (
    <span className="muted">—</span>
  );
}

// Where the key comes from, under the state: it rarely matters, so it is small.
function StateCell({ model, tested }: { model: Model; tested?: Tested }) {
  const t = useT();
  return (
    <span className="model-state">
      <TestCell model={model} tested={tested} />
      <span className="muted model-key">
        {t("key：{source}", { source: keyLabel(t, model.key) })}
      </span>
    </span>
  );
}

function UsageList({ alias }: { alias: string }) {
  const t = useT();
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
  if (!usage) return <p className="muted">{t("正在统计…")}</p>;
  if (!usage.apps.length)
    return <p className="muted">{t("没有应用的当前版本在用它。")}</p>;
  return (
    <ul className="model-app-list">
      {usage.apps.map((app) => (
        <li key={app.app}>
          <strong>{app.title}</strong>
          <span className="muted">
            {" "}
            v{app.version} · {app.nodes.join("、")}
            {app.via === "default" && t("（没指定模型，用的是平台默认）")}
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
  const t = useT();
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="table-scroll">
      <table
        className="mapping-table model-table"
        aria-label={t(purposeLabels[purpose])}
      >
        <thead>
          <tr>
            <th scope="col">{t("模型#字段")}</th>
            <th scope="col">{t("供应商和型号")}</th>
            <th scope="col">{t("近 30 天")}</th>
            <th scope="col">{t("状态")}</th>
            <th scope="col">{t("操作")}</th>
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
                          {t("默认")}
                        </span>
                      )}
                      {!model.allowed && (
                        <span className="datasource-tag undeclared">
                          {t("外发策略不允许")}
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
                  {t(providerLabel(model.provider))}
                  <span className="muted model-id">{model.model}</span>
                </td>
                <td>
                  <UsageCell model={model} />
                </td>
                <td aria-live="polite">
                  <StateCell model={model} tested={state} />
                </td>
                <td className="model-actions">
                  {purpose !== "rerank" && (
                    <button
                      type="button"
                      className="text-button"
                      aria-label={t("测试「{display_name}」", {
                        display_name: model.display_name,
                      })}
                      aria-disabled={state === "running" || undefined}
                      onClick={() => test(model)}
                    >
                      {t("测试")}
                    </button>
                  )}
                  <button
                    type="button"
                    className="text-button"
                    aria-expanded={open === model.alias}
                    aria-label={t("哪些应用在用「{display_name}」", {
                      display_name: model.display_name,
                    })}
                    onClick={() =>
                      setOpen(open === model.alias ? null : model.alias)
                    }
                  >
                    {t("在用的应用")}
                  </button>
                  {page && writable && (
                    <>
                      <button
                        type="button"
                        className="text-button"
                        aria-label={t("修改「{display_name}」", {
                          display_name: model.display_name,
                        })}
                        onClick={() => edit(model)}
                      >
                        {t("修改")}
                      </button>
                      <button
                        type="button"
                        className="text-button danger-text"
                        aria-label={t("删除「{display_name}」", {
                          display_name: model.display_name,
                        })}
                        onClick={() => remove(model)}
                      >
                        {t("删除")}
                      </button>
                    </>
                  )}
                </td>
              </tr>,
              open === model.alias && (
                <tr key={`${model.alias}-usage`} className="model-usage-row">
                  <td colSpan={5}>
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
  const t = useT();
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const id = `default-${purpose}`;
  const choices = data.models.filter((model) => model.purpose === purpose);
  const current = data.defaults[purpose];
  return (
    <div className="asset-field">
      <label htmlFor={id}>
        {t(purpose === "chat" ? "默认对话模型" : "默认向量模型")}
      </label>
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
            setNote(t("已保存。"));
          } catch (err) {
            setError(errorMessage(err));
          } finally {
            setSaving(false);
          }
        }}
      >
        {!choices.some((model) => model.alias === current) && (
          <option value={current}>
            {t("{alias}（网关上没有）", { alias: current })}
          </option>
        )}
        {choices.map((model) => (
          <option key={model.alias} value={model.alias}>
            {model.display_name === model.alias
              ? model.alias
              : t("{name}（{alias}）", {
                  name: model.display_name,
                  alias: model.alias,
                })}
            {!model.allowed ? t("（外发策略不允许）") : ""}
          </option>
        ))}
      </select>
      <p className="field-hint" id={`${id}-hint`} role="status">
        {saving
          ? t("正在保存…")
          : error ||
            note ||
            (purpose === "chat"
              ? t("节点没指定模型时用它。")
              : t("新建知识库时默认用它；已有的知识库不变。"))}
      </p>
    </div>
  );
}

export function ModelList() {
  const t = useT();
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
        t("确定删除「{display_name}」吗？网关上保存的 key 也会一起删掉。", {
          display_name: model.display_name,
        }),
      )
    )
      return;
    setActionError("");
    setNotice("");
    try {
      await deleteModel(model.alias);
      setNotice(
        t("已删除「{display_name}」。", { display_name: model.display_name }),
      );
      setRevision((n) => n + 1);
    } catch (err) {
      setActionError(errorMessage(err));
    }
  }

  const writable = data?.gateway === "ok";
  return (
    <div className="asset-page">
      <AssetHeading
        title={t("模型")}
        action={
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
            {t("添加模型")}
          </button>
        }
      >
        {t(
          "节点按别名调用这里的模型，调用都经过平台的模型网关，key 由网关加密保存。",
        )}
      </AssetHeading>
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
          {t(data.gateway === "no_db" ? NO_DB : DOWN)}
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
            <h2 id="model-defaults">{t("平台默认")}</h2>
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
                    {t(purposeLabels[purpose])}
                    <span className="muted">
                      {" "}
                      {t("{n} 个", { n: models.length })}
                    </span>
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
              ? t("添加模型")
              : t("修改「{display_name}」", {
                  display_name: dialog.model.display_name,
                })
          }
          onClose={() => setDialog(null)}
        >
          {dialog.kind === "create" ? (
            <ModelForm
              mode="create"
              onSaved={(model) => {
                dialogRef.current?.close();
                setNotice(
                  t(
                    "已添加「{display_name}」，测试通过，现在可以在节点里选它了。",
                    { display_name: model.display_name },
                  ),
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
                setNotice(
                  t("已保存「{display_name}」。", {
                    display_name: model.display_name,
                  }),
                );
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
