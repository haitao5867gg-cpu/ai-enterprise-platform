"use client";

import { useId, useState } from "react";
import {
  createModel,
  providerForms,
  providerLabel,
  purposeLabels,
  updateModel,
  type KeyInput,
  type Model,
  type ModelChanges,
  type ProviderId,
} from "../lib/models";
import { ApiError, errorMessage } from "../lib/runs";
import { useUnsavedWarning } from "./asset-ui";
import { ErrorNotice } from "./ui";

type Props =
  | { mode: "create"; onSaved: (model: Model) => void }
  | { mode: "edit"; initial: Model; onSaved: (model: Model) => void };
type KeyMode = "page" | "env" | "keep";

const ALIAS = /^[a-z][a-z0-9_-]{1,39}$/;
const ENV = /^[A-Z][A-Z0-9_]*_API_KEY$/;
const TEST_FAILED = "测试没有通过";

// A starting alias from the provider's model name: "claude-sonnet-5-5" stays as it is.
const suggestAlias = (model: string) =>
  model
    .toLowerCase()
    .replace(/^.*\//, "")
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .replace(/-+$/, "")
    .slice(0, 40);

export function ModelForm(props: Props) {
  const prefix = useId();
  const initial = props.mode === "edit" ? props.initial : null;
  const [purpose, setPurpose] = useState<"chat" | "embedding">(
    initial?.purpose === "embedding" ? "embedding" : "chat",
  );
  const [provider, setProvider] = useState<ProviderId | "">(
    initial?.provider ?? "",
  );
  const [displayName, setDisplayName] = useState(initial?.display_name ?? "");
  const [alias, setAlias] = useState(initial?.alias ?? "");
  const [aliasTouched, setAliasTouched] = useState(false);
  const [model, setModel] = useState(initial?.model ?? "");
  const [apiBase, setApiBase] = useState(initial?.api_base ?? "");
  const [apiVersion, setApiVersion] = useState("");
  const [awsRegion, setAwsRegion] = useState("");
  const [keyMode, setKeyMode] = useState<KeyMode>(initial ? "keep" : "page");
  const [keyValue, setKeyValue] = useState("");
  const [keyEnv, setKeyEnv] = useState(
    initial?.key.source === "env" ? (initial.key.env ?? "") : "",
  );
  const [description, setDescription] = useState(initial?.description ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [aliasError, setAliasError] = useState("");
  const [testError, setTestError] = useState("");
  const form = providerForms.find((item) => item.id === provider);

  const key: KeyInput | null =
    !form?.key || keyMode === "keep"
      ? null
      : keyMode === "page"
        ? { source: "page", value: keyValue.trim() }
        : { source: "env", env: keyEnv.trim() };
  const changes: ModelChanges = initial
    ? {
        ...(displayName.trim() !== initial.display_name
          ? { display_name: displayName.trim() }
          : {}),
        ...(description !== initial.description ? { description } : {}),
        ...(model.trim() !== initial.model ? { model: model.trim() } : {}),
        ...(apiBase.trim() && apiBase.trim() !== (initial.api_base ?? "")
          ? { api_base: apiBase.trim() }
          : {}),
        ...(apiVersion.trim() ? { api_version: apiVersion.trim() } : {}),
        ...(awsRegion.trim() ? { aws_region: awsRegion.trim() } : {}),
        ...(key ? { key } : {}),
      }
    : {};
  const dirty = initial
    ? Object.keys(changes).length > 0
    : Boolean(
        provider || displayName.trim() || model.trim() || keyValue || keyEnv,
      );
  useUnsavedWarning(dirty);
  // Only the display name and description change without a test call.
  const testing = initial
    ? Object.keys(changes).some(
        (field) => field !== "display_name" && field !== "description",
      )
    : true;

  const blocker = !form
    ? "请先选择供应商。"
    : initial && !dirty
      ? "还没有修改。"
      : !displayName.trim()
        ? "请填写显示名。"
        : !initial && !ALIAS.test(alias)
          ? "别名要以小写字母开头，只能有小写字母、数字、- 和 _，2–40 个字符。"
          : !model.trim()
            ? `请填写${form.modelLabel}。`
            : form.base === "required" && !apiBase.trim()
              ? "请填写地址。"
              : !initial && form.version && !apiVersion.trim()
                ? "请填写 API 版本。"
                : !initial && form.region && !awsRegion.trim()
                  ? "请填写区域。"
                  : key?.source === "page" && !key.value
                    ? "请粘贴 key。"
                    : key?.source === "env" && !ENV.test(key.env)
                      ? "环境变量名要以 _API_KEY 结尾，只能有大写字母、数字和下划线。"
                      : "";

  async function save() {
    if (blocker || saving || !form) return;
    setSaving(true);
    setError("");
    setAliasError("");
    setTestError("");
    try {
      const saved = initial
        ? await updateModel(initial.alias, changes)
        : await createModel({
            alias,
            display_name: displayName.trim(),
            purpose,
            provider: form.id,
            model: model.trim(),
            ...(apiBase.trim() && form.base !== "none"
              ? { api_base: apiBase.trim() }
              : {}),
            ...(form.version ? { api_version: apiVersion.trim() } : {}),
            ...(form.region ? { aws_region: awsRegion.trim() } : {}),
            ...(key ? { key } : {}),
            ...(description ? { description } : {}),
          });
      // The key never comes back; drop it from memory as soon as it is saved.
      setKeyValue("");
      props.onSaved(saved);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && !initial)
        setAliasError(err.message);
      else if (
        err instanceof ApiError &&
        err.status === 422 &&
        err.message.startsWith(TEST_FAILED)
      )
        setTestError(err.message);
      else setError(errorMessage(err));
      setSaving(false);
    }
  }

  const ids = {
    purpose: `${prefix}-purpose`,
    provider: `${prefix}-provider`,
    providerNote: `${prefix}-provider-note`,
    name: `${prefix}-name`,
    alias: `${prefix}-alias`,
    aliasHint: `${prefix}-alias-hint`,
    aliasError: `${prefix}-alias-error`,
    model: `${prefix}-model`,
    base: `${prefix}-base`,
    baseHint: `${prefix}-base-hint`,
    version: `${prefix}-version`,
    region: `${prefix}-region`,
    keyValue: `${prefix}-key-value`,
    keyEnv: `${prefix}-key-env`,
    keyHint: `${prefix}-key-hint`,
    description: `${prefix}-description`,
    saveHint: `${prefix}-save-hint`,
  };
  const unchangedHint = initial ? "不改就留空。" : "";
  return (
    <form
      className="asset-form datasource-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <fieldset className="datasource-fields" disabled={saving}>
        {initial ? (
          <div className="asset-field">
            <span>用途和供应商</span>
            <strong className="datasource-kind">
              {purposeLabels[initial.purpose]} ·{" "}
              {providerLabel(initial.provider)}
            </strong>
            <p className="field-hint">
              别名 <code>{initial.alias}</code>
              、用途和供应商登记后不能修改；要换供应商请新增一个模型。
            </p>
          </div>
        ) : (
          <>
            <div className="asset-field">
              <label htmlFor={ids.purpose}>用途</label>
              <select
                id={ids.purpose}
                value={purpose}
                onChange={(event) => {
                  const next = event.target.value as "chat" | "embedding";
                  setPurpose(next);
                  if (form && !form.purposes.includes(next)) setProvider("");
                }}
              >
                <option value="chat">对话模型（节点里用）</option>
                <option value="embedding">向量模型（知识库用）</option>
              </select>
              {purpose === "embedding" && (
                <p className="field-hint">
                  知识库按 1024 维建索引，向量模型要能输出 1024 维（例如
                  text-embedding-v4、text-embedding-3-large）。
                </p>
              )}
            </div>
            <div className="asset-field">
              <label htmlFor={ids.provider}>供应商</label>
              <select
                id={ids.provider}
                data-autofocus
                value={provider}
                aria-describedby={form?.note ? ids.providerNote : undefined}
                onChange={(event) => {
                  setProvider(event.target.value as ProviderId);
                  setTestError("");
                }}
              >
                <option value="">请选择供应商</option>
                {providerForms
                  .filter((item) => item.purposes.includes(purpose))
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.label}
                    </option>
                  ))}
              </select>
            </div>
          </>
        )}
        {form?.note && (
          <p className="datasource-note" id={ids.providerNote}>
            {form.note}
          </p>
        )}
        {form && (
          <>
            <div className="asset-field">
              <label htmlFor={ids.model}>{form.modelLabel}</label>
              <input
                id={ids.model}
                autoComplete="off"
                spellCheck={false}
                maxLength={200}
                value={model}
                placeholder={form.modelHint}
                onChange={(event) => {
                  setModel(event.target.value);
                  setTestError("");
                  if (!initial && !aliasTouched)
                    setAlias(suggestAlias(event.target.value));
                }}
              />
            </div>
            <div className="asset-field">
              <label htmlFor={ids.name}>显示名</label>
              <input
                id={ids.name}
                data-autofocus={initial ? true : undefined}
                maxLength={60}
                value={displayName}
                placeholder="页面上显示的名字，例如：Claude Sonnet 5.5"
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </div>
            {!initial && (
              <div className="asset-field">
                <label htmlFor={ids.alias}>别名</label>
                <input
                  id={ids.alias}
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={40}
                  value={alias}
                  placeholder="claude-sonnet"
                  aria-invalid={Boolean(aliasError)}
                  aria-describedby={[
                    aliasError && ids.aliasError,
                    ids.aliasHint,
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  onChange={(event) => {
                    setAlias(event.target.value);
                    setAliasTouched(true);
                    setAliasError("");
                  }}
                />
                {aliasError && (
                  <p className="field-error" role="alert" id={ids.aliasError}>
                    {aliasError}
                  </p>
                )}
                <p className="field-hint" id={ids.aliasHint}>
                  应用和知识库按别名调用模型，登记后不能修改。小写字母开头，只能有小写字母、数字、-
                  和 _。
                </p>
              </div>
            )}
            {form.base !== "none" && (
              <div className="asset-field">
                <label htmlFor={ids.base}>
                  地址
                  {form.base === "optional" && (
                    <span className="muted">（选填）</span>
                  )}
                </label>
                <input
                  id={ids.base}
                  type="text"
                  inputMode="url"
                  autoComplete="off"
                  spellCheck={false}
                  value={apiBase}
                  placeholder="https://api.example.com/v1"
                  aria-describedby={ids.baseHint}
                  onChange={(event) => {
                    setApiBase(event.target.value);
                    setTestError("");
                  }}
                />
                <p className="field-hint" id={ids.baseHint}>
                  {form.baseHint}地址里不要带 key。
                </p>
              </div>
            )}
            {form.version && (
              <div className="asset-field">
                <label htmlFor={ids.version}>API 版本</label>
                <input
                  id={ids.version}
                  autoComplete="off"
                  spellCheck={false}
                  value={apiVersion}
                  placeholder="2024-10-21"
                  onChange={(event) => setApiVersion(event.target.value)}
                />
                {unchangedHint && <p className="field-hint">{unchangedHint}</p>}
              </div>
            )}
            {form.region && (
              <div className="asset-field">
                <label htmlFor={ids.region}>区域</label>
                <input
                  id={ids.region}
                  autoComplete="off"
                  spellCheck={false}
                  value={awsRegion}
                  placeholder="us-east-1"
                  onChange={(event) => setAwsRegion(event.target.value)}
                />
                {unchangedHint && <p className="field-hint">{unchangedHint}</p>}
              </div>
            )}
            {form.key && (
              <fieldset className="asset-field model-key">
                <legend>key</legend>
                <div className="model-key-modes">
                  {initial && (
                    <label>
                      <input
                        type="radio"
                        name={`${prefix}-key`}
                        checked={keyMode === "keep"}
                        onChange={() => setKeyMode("keep")}
                      />
                      不改（
                      {initial.key.source === "env"
                        ? `环境变量 ${initial.key.env ?? ""}`
                        : "用原来的 key"}
                      ）
                    </label>
                  )}
                  <label>
                    <input
                      type="radio"
                      name={`${prefix}-key`}
                      checked={keyMode === "page"}
                      onChange={() => setKeyMode("page")}
                    />
                    在这里粘贴
                  </label>
                  <label>
                    <input
                      type="radio"
                      name={`${prefix}-key`}
                      checked={keyMode === "env"}
                      onChange={() => setKeyMode("env")}
                    />
                    用服务器上的环境变量
                  </label>
                </div>
                {keyMode === "page" && (
                  <input
                    id={ids.keyValue}
                    type="password"
                    aria-label="key"
                    autoComplete="new-password"
                    spellCheck={false}
                    maxLength={500}
                    value={keyValue}
                    placeholder="sk-..."
                    aria-describedby={ids.keyHint}
                    onChange={(event) => {
                      setKeyValue(event.target.value);
                      setTestError("");
                    }}
                  />
                )}
                {keyMode === "env" && (
                  <input
                    id={ids.keyEnv}
                    aria-label="环境变量名"
                    autoComplete="off"
                    spellCheck={false}
                    value={keyEnv}
                    placeholder={form.envHint}
                    aria-describedby={ids.keyHint}
                    onChange={(event) => {
                      setKeyEnv(event.target.value);
                      setTestError("");
                    }}
                  />
                )}
                <p className="field-hint" id={ids.keyHint}>
                  {keyMode === "env"
                    ? "变量名要以 _API_KEY 结尾。变量要配进服务器的 .env，重启模型网关后才生效。"
                    : "key 直接交给模型网关加密保存，平台不保存，页面上也不会再显示。"}
                </p>
              </fieldset>
            )}
            <div className="asset-field">
              <label htmlFor={ids.description}>
                说明<span className="muted">（选填）</span>
              </label>
              <textarea
                id={ids.description}
                maxLength={500}
                rows={2}
                value={description}
                placeholder="适合做什么、谁的账号，例如：Console 赠送额度，用于方案和写代码"
                onChange={(event) => setDescription(event.target.value)}
              />
            </div>
          </>
        )}
        {testError && (
          <div className="datasource-test error" role="alert">
            <strong>{testError}</strong>
            <p>没有保存。改好后再保存一次。</p>
          </div>
        )}
        {error && <ErrorNotice message={error} />}
        <div className="datasource-save-row">
          <button
            className="button primary"
            disabled={Boolean(blocker) || saving}
            aria-describedby={blocker || testing ? ids.saveHint : undefined}
          >
            {saving ? (testing ? "正在测试并保存…" : "正在保存…") : "保存"}
          </button>
          <p className="field-hint" id={ids.saveHint} role="status">
            {blocker ||
              (testing
                ? "保存前先调一次模型试试，通过了才保存，最多等 30 秒。"
                : "")}
          </p>
        </div>
      </fieldset>
    </form>
  );
}
