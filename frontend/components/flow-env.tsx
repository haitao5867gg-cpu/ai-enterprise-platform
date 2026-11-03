"use client";

import { useT } from "../lib/i18n/client";
import { useState } from "react";
import {
  deleteAppSecret,
  NODE_ID_PATTERN,
  setAppSecret,
  type AppDetail,
  type EnvVar,
  type FlowConfig,
} from "../lib/apps";
import { actorPayload, savedActor } from "../lib/actor";
import { issuesUnder } from "../lib/flow-edit";
import { errorMessage } from "../lib/runs";
import {
  CheckField,
  FieldIssues,
  ListEditor,
  SelectField,
  TextField,
} from "./flow-fields";

type Secret = AppDetail["secrets"][number];

function SecretRow({
  app,
  secret,
  declared,
  onSaved,
  onDeclare,
}: {
  app: string;
  secret: Secret;
  declared: boolean;
  onSaved: (secret: Secret) => void;
  onDeclare: (declared: boolean) => void;
}) {
  const t = useT();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const id = `secret-${secret.name}`;
  async function run(work: () => Promise<Secret>) {
    setBusy(true);
    setError("");
    try {
      onSaved(await work());
      setValue("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <li
      className="secret-row"
      aria-label={t("密钥 {name}", { name: secret.name })}
    >
      <div>
        <code>{secret.name}</code>
        <span className={`secret-state ${secret.set ? "set" : ""}`}>
          {secret.set ? t("已设置") : t("未设置")}
        </span>
        {!declared && <span className="muted">{t("未在配置中声明")}</span>}
      </div>
      <form
        className="secret-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!value) return;
          void run(async () => {
            await setAppSecret(
              app,
              secret.name,
              value,
              actorPayload(savedActor()).actor,
            );
            return {
              ...secret,
              set: true,
              updated_at: new Date().toISOString(),
            };
          });
        }}
      >
        <label className="sr-only" htmlFor={id}>
          {t("{name} 的值", { name: secret.name })}
        </label>
        <input
          id={id}
          type="password"
          autoComplete="new-password"
          placeholder={secret.set ? t("输入新值以替换") : t("输入密钥的值")}
          value={value}
          maxLength={4096}
          disabled={busy}
          onChange={(event) => setValue(event.target.value)}
        />
        <button className="button secondary" disabled={busy || !value}>
          {t("设置")}
        </button>
        {secret.set && (
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={() => {
              if (
                !window.confirm(
                  t("确定清除密钥 {name} 的值吗？", { name: secret.name }),
                )
              )
                return;
              void run(async () => {
                await deleteAppSecret(app, secret.name);
                return { ...secret, set: false, updated_at: null };
              });
            }}
          >
            {t("清除")}
          </button>
        )}
        <button
          type="button"
          className="text-button"
          onClick={() => onDeclare(!declared)}
        >
          {declared ? t("从配置移除") : t("在配置中声明")}
        </button>
      </form>
      {error && <p className="field-error">{error}</p>}
    </li>
  );
}

export function FlowEnv({
  app,
  config,
  setConfig,
  secrets,
  setSecrets,
  issues,
  disabled,
}: {
  app: string;
  config: FlowConfig;
  setConfig: (config: FlowConfig) => void;
  secrets: Secret[];
  setSecrets: (secrets: Secret[]) => void;
  issues: Record<string, string>;
  disabled: boolean;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const names = [
    ...new Set([...secrets.map((item) => item.name), ...config.env.secrets]),
  ];
  // Folded while the app has none; a problem in them opens it.
  const [open, setOpen] = useState(
    config.env.vars.length > 0 || names.length > 0,
  );
  const hasIssues = Object.keys(issues).some(
    (key) => key.startsWith("vars") || key.startsWith("secrets"),
  );
  const setEnv = (env: Partial<FlowConfig["env"]>) =>
    setConfig({ ...config, env: { ...config.env, ...env } });
  const declare = (secret: string, on: boolean) =>
    setEnv({
      secrets: on
        ? [...new Set([...config.env.secrets, secret])]
        : config.env.secrets.filter((item) => item !== secret),
    });
  const nameIssue =
    name && !NODE_ID_PATTERN.test(name)
      ? t("以小写字母开头，只用小写字母、数字和下划线")
      : names.includes(name)
        ? t("已有这个密钥")
        : "";
  return (
    <section
      className="panel asset-section flow-env"
      aria-label={t("常量和密钥")}
    >
      <details
        open={open || hasIssues}
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>
          <h2>{t("常量和密钥")}</h2>
          <span className="muted">
            {t("{vars} 个常量，{secrets} 个密钥", {
              vars: config.env.vars.length,
              secrets: names.length,
            })}
          </span>
        </summary>
        <p className="muted">
          {t(
            "节点里用 {env} 引用常量，常量跟着草稿保存和发布。密钥用 {secret}，只能写在工具参数里；密钥的值单独保存，不进版本、导出和运行记录。",
            { env: t("{{env.名称}}"), secret: t("{{secret.名称}}") },
          )}
        </p>
        <ListEditor<EnvVar>
          label={t("常量")}
          items={config.env.vars}
          max={50}
          disabled={disabled}
          onChange={(vars) => setEnv({ vars })}
          make={() => ({
            key: `var_${config.env.vars.length + 1}`,
            type: "string",
            value: "",
            description: "",
          })}
          issues={(i) => issuesUnder(issues, `vars.${i}`)}
          render={(item, _i, update) => (
            <>
              <TextField
                label={t("名称")}
                value={item.key}
                onChange={(key) => update({ ...item, key })}
              />
              <SelectField
                label={t("类型")}
                value={item.type}
                choices={[
                  { value: "string", label: t("文字") },
                  { value: "number", label: t("数字") },
                  { value: "boolean", label: t("是/否") },
                ]}
                onChange={(type) =>
                  update({
                    ...item,
                    type: type as EnvVar["type"],
                    value:
                      type === "number" ? 0 : type === "boolean" ? false : "",
                  })
                }
              />
              {item.type === "boolean" ? (
                <CheckField
                  label={t("值")}
                  checked={item.value === true}
                  onChange={(value) => update({ ...item, value })}
                />
              ) : (
                <TextField
                  label={t("值")}
                  value={String(item.value)}
                  onChange={(value) =>
                    update({
                      ...item,
                      value:
                        item.type === "number" &&
                        value.trim() &&
                        Number.isFinite(Number(value))
                          ? Number(value)
                          : value,
                    })
                  }
                />
              )}
              <TextField
                label={t("说明")}
                value={item.description ?? ""}
                onChange={(description) => update({ ...item, description })}
              />
            </>
          )}
        />
        <h3>{t("密钥")}</h3>
        {!names.length && <p className="muted">{t("还没有密钥。")}</p>}
        <ul className="secret-list">
          {names.map((secret) => (
            <SecretRow
              key={secret}
              app={app}
              secret={
                secrets.find((item) => item.name === secret) ?? {
                  name: secret,
                  set: false,
                  updated_at: null,
                }
              }
              declared={config.env.secrets.includes(secret)}
              onDeclare={(on) => declare(secret, on)}
              onSaved={(saved) =>
                setSecrets([
                  ...secrets.filter((item) => item.name !== saved.name),
                  saved,
                ])
              }
            />
          ))}
        </ul>
        <form
          className="add-key"
          onSubmit={(event) => {
            event.preventDefault();
            if (!name || nameIssue) return;
            declare(name, true);
            setName("");
          }}
        >
          <div className="asset-field">
            <label htmlFor="new-secret">{t("新密钥名称")}</label>
            <input
              id="new-secret"
              value={name}
              disabled={disabled}
              aria-invalid={Boolean(nameIssue)}
              onChange={(event) => setName(event.target.value.trim())}
            />
            <FieldIssues
              id="new-secret-issues"
              messages={nameIssue ? [nameIssue] : []}
            />
          </div>
          <button
            className="button secondary"
            disabled={disabled || !name || Boolean(nameIssue)}
          >
            {t("添加密钥")}
          </button>
        </form>
        {issuesUnder(issues, "secrets").map((msg) => (
          <p className="field-error" key={msg}>
            {msg}
          </p>
        ))}
      </details>
    </section>
  );
}
