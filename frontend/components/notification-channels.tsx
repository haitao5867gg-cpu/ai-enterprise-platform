"use client";

import { useEffect, useId, useRef, useState } from "react";
import { listApps, type AppSummary } from "../lib/apps";
import {
  createWebhook,
  deleteWebhook,
  EVENTS,
  eventLabels,
  getChannels,
  KINDS,
  kindHints,
  kindLabels,
  testWebhook,
  updateWebhook,
  type ChannelStatus,
  type NotificationEvent,
  type Webhook,
  type WebhookKind,
} from "../lib/notifications";
import { errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { DatasourceDialog } from "./datasource-form";
import { ErrorNotice } from "./ui";

const time = (value: string | null) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";

function WebhookForm({
  hook,
  apps,
  onSaved,
}: {
  hook: Webhook | null;
  apps: AppSummary[];
  onSaved: (hook: Webhook) => void;
}) {
  const id = useId();
  const [name, setName] = useState(hook?.name ?? "");
  const [kind, setKind] = useState<WebhookKind>(hook?.kind ?? "wecom");
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<NotificationEvent[]>(
    hook?.events ?? ["run.waiting", "run.error"],
  );
  const [scoped, setScoped] = useState(Boolean(hook?.apps));
  const [chosen, setChosen] = useState<string[]>(hook?.apps ?? []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const blocker = !name.trim()
    ? "请填名称。"
    : !hook && !url.trim()
      ? "请填 Webhook 地址。"
      : url.trim() && !/^https?:\/\/\S+$/.test(url.trim())
        ? "地址要以 http:// 或 https:// 开头。"
        : !events.length
          ? "至少选一种事件。"
          : scoped && !chosen.length
            ? "至少选一个应用，或者改成所有应用。"
            : "";
  return (
    <form
      className="asset-form"
      noValidate
      onSubmit={async (event) => {
        event.preventDefault();
        if (blocker || saving) return;
        setSaving(true);
        setError("");
        const input = {
          name: name.trim(),
          kind,
          events,
          apps: scoped ? chosen : null,
          ...(url.trim() ? { url: url.trim() } : {}),
        };
        try {
          onSaved(
            hook
              ? await updateWebhook(hook.webhook_id, input)
              : await createWebhook({ ...input, enabled: true }),
          );
        } catch (err) {
          setError(errorMessage(err));
          setSaving(false);
        }
      }}
    >
      <div className="asset-field">
        <label htmlFor={`${id}-name`}>名称</label>
        <input
          id={`${id}-name`}
          data-autofocus
          maxLength={50}
          value={name}
          placeholder="例如：数据组群"
          onChange={(event) => setName(event.target.value)}
        />
      </div>
      <div className="asset-field">
        <label htmlFor={`${id}-kind`}>发到哪里</label>
        <select
          id={`${id}-kind`}
          value={kind}
          aria-describedby={`${id}-kind-hint`}
          onChange={(event) => setKind(event.target.value as WebhookKind)}
        >
          {KINDS.map((value) => (
            <option key={value} value={value}>
              {kindLabels[value]}
            </option>
          ))}
        </select>
        <p className="field-hint" id={`${id}-kind-hint`}>
          {kindHints[kind]}
        </p>
      </div>
      <div className="asset-field">
        <label htmlFor={`${id}-url`}>Webhook 地址</label>
        <input
          id={`${id}-url`}
          type="url"
          autoComplete="off"
          spellCheck={false}
          maxLength={1000}
          value={url}
          placeholder={
            hook ? `现在是 ${hook.url_hint}，不改就留空` : "https://…"
          }
          aria-describedby={`${id}-url-hint`}
          onChange={(event) => setUrl(event.target.value)}
        />
        <p className="field-hint" id={`${id}-url-hint`}>
          地址里通常带着机器人的密钥，保存后只显示开头和最后 4 个字符。
        </p>
      </div>
      <fieldset className="asset-field user-roles">
        <legend>哪些事件</legend>
        {EVENTS.map((value) => (
          <label key={value}>
            <input
              type="checkbox"
              checked={events.includes(value)}
              onChange={(event) =>
                setEvents(
                  event.target.checked
                    ? EVENTS.filter((e) => e === value || events.includes(e))
                    : events.filter((e) => e !== value),
                )
              }
            />
            <span>
              <strong>{eventLabels[value]}</strong>
            </span>
          </label>
        ))}
      </fieldset>
      <fieldset className="asset-field user-roles">
        <legend>哪些应用</legend>
        <label>
          <input
            type="radio"
            name={`${id}-scope`}
            checked={!scoped}
            onChange={() => setScoped(false)}
          />
          <span>
            <strong>所有应用</strong>
          </span>
        </label>
        <label>
          <input
            type="radio"
            name={`${id}-scope`}
            checked={scoped}
            onChange={() => setScoped(true)}
          />
          <span>
            <strong>只选这些应用</strong>
          </span>
        </label>
        {scoped && (
          <div className="webhook-apps">
            {apps.map((app) => (
              <label key={app.app}>
                <input
                  type="checkbox"
                  checked={chosen.includes(app.app)}
                  onChange={(event) =>
                    setChosen(
                      event.target.checked
                        ? [...chosen, app.app]
                        : chosen.filter((a) => a !== app.app),
                    )
                  }
                />
                {app.title}
              </label>
            ))}
          </div>
        )}
      </fieldset>
      {error && <ErrorNotice message={error} />}
      <div className="datasource-save-row">
        <button
          className="button primary"
          disabled={Boolean(blocker) || saving}
        >
          {saving ? "正在保存…" : "保存"}
        </button>
        {blocker && <p className="field-hint">{blocker}</p>}
      </div>
    </form>
  );
}

export function NotificationChannels() {
  const [status, setStatus] = useState<ChannelStatus | null>(null);
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [dialog, setDialog] = useState<{ hook: Webhook | null } | null>(null);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState("");
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    getChannels(controller.signal)
      .then((value) => {
        setStatus(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    listApps(controller.signal)
      .then(setApps)
      .catch(() => undefined);
    return () => controller.abort();
  }, [revision]);
  const titles = new Map(apps.map((app) => [app.app, app.title]));

  async function act(id: string, run: () => Promise<unknown>, done: string) {
    setBusy(id);
    setActionError("");
    setNotice("");
    try {
      await run();
      setNotice(done);
      setRevision((n) => n + 1);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : errorMessage(err));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="asset-page">
      <AssetHeading title="通知渠道">
        运行等人确认、完成或失败时，平台在站内通知相关的人；这里配置还要发到哪里：邮件，以及企业微信、钉钉、飞书的群机器人或你们自己的系统。
      </AssetHeading>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!status && !error && <AssetLoading />}
      {status && (
        <>
          <section className="panel asset-section" aria-labelledby="mail-title">
            <h2 id="mail-title">邮件</h2>
            {status.email.configured ? (
              <p>
                已配置，发件人 <code>{status.email.from}</code>
                。账号填了邮箱的人会收到发给他的通知（在「账号」页填邮箱）。
              </p>
            ) : (
              <p>
                还没有配置。在服务器的 <code>.env</code> 里填{" "}
                <code>AEP_SMTP_HOST</code>、<code>AEP_SMTP_PORT</code>、
                <code>AEP_SMTP_USER</code>、<code>AEP_SMTP_PASSWORD</code> 和{" "}
                <code>AEP_SMTP_FROM</code>，重启后生效。邮件只在打开登录后发。
              </p>
            )}
            <p className="muted">
              通知里的链接以 <code>{status.web_url}</code>{" "}
              开头；不对的话改服务器上的 <code>AEP_WEB_URL</code>。
            </p>
          </section>
          <section
            className="panel asset-section datasource-section"
            aria-labelledby="webhook-title"
          >
            <div className="asset-section-heading">
              <h2 id="webhook-title">Webhook</h2>
              <button
                type="button"
                className="button primary"
                onClick={() => {
                  setNotice("");
                  setDialog({ hook: null });
                }}
              >
                新建 Webhook
              </button>
            </div>
            {notice && (
              <p className="datasource-note" role="status">
                {notice}
              </p>
            )}
            {actionError && <ErrorNotice message={actionError} />}
            {status.webhooks.length ? (
              <div className="table-scroll">
                <table
                  className="mapping-table user-table"
                  aria-label="Webhook"
                >
                  <thead>
                    <tr>
                      {["名称", "事件", "应用", "最近一次", "操作"].map((h) => (
                        <th scope="col" key={h}>
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {status.webhooks.map((hook) => (
                      <tr key={hook.webhook_id}>
                        <td>
                          <div className="model-name-stack">
                            <strong>
                              {hook.name}
                              {!hook.enabled && (
                                <span className="muted">（已停用）</span>
                              )}
                            </strong>
                            <span className="muted">
                              {kindLabels[hook.kind]}
                            </span>
                            <code>{hook.url_hint}</code>
                          </div>
                        </td>
                        <td>
                          {hook.events.map((e) => eventLabels[e]).join("、")}
                        </td>
                        <td>
                          {hook.apps
                            ? hook.apps
                                .map((a) => titles.get(a) ?? a)
                                .join("、")
                            : "所有应用"}
                        </td>
                        <td>
                          {hook.last_status ? (
                            <div className="model-name-stack">
                              <span
                                className={`datasource-health ${hook.last_status}`}
                              >
                                {hook.last_status === "ok" ? "成功" : "失败"}
                              </span>
                              <span className="muted">
                                {time(hook.last_sent_at)}
                              </span>
                              {hook.last_error && (
                                <span className="danger-text">
                                  {hook.last_error}
                                </span>
                              )}
                            </div>
                          ) : (
                            <span className="muted">还没发过</span>
                          )}
                        </td>
                        <td className="model-actions">
                          <button
                            type="button"
                            className="text-button"
                            disabled={busy === hook.webhook_id}
                            aria-label={`给「${hook.name}」发测试消息`}
                            onClick={() =>
                              void act(
                                hook.webhook_id,
                                async () => {
                                  const result = await testWebhook(
                                    hook.webhook_id,
                                  );
                                  if (result.last_status !== "ok")
                                    throw new Error(
                                      `测试消息没发成功：${result.last_error ?? "原因不明"}`,
                                    );
                                },
                                `测试消息已发到「${hook.name}」，请到群里看一下。`,
                              )
                            }
                          >
                            {busy === hook.webhook_id
                              ? "正在发送…"
                              : "发测试消息"}
                          </button>
                          <button
                            type="button"
                            className="text-button"
                            aria-label={`修改「${hook.name}」`}
                            onClick={() => setDialog({ hook })}
                          >
                            修改
                          </button>
                          <button
                            type="button"
                            className="text-button"
                            aria-label={`${hook.enabled ? "停用" : "启用"}「${hook.name}」`}
                            onClick={() =>
                              void act(
                                hook.webhook_id,
                                () =>
                                  updateWebhook(hook.webhook_id, {
                                    enabled: !hook.enabled,
                                  }),
                                `已${hook.enabled ? "停用" : "启用"}「${hook.name}」。`,
                              )
                            }
                          >
                            {hook.enabled ? "停用" : "启用"}
                          </button>
                          <button
                            type="button"
                            className="text-button danger-text"
                            aria-label={`删除「${hook.name}」`}
                            onClick={() => {
                              if (
                                window.confirm(`确定删除「${hook.name}」吗？`)
                              )
                                void act(
                                  hook.webhook_id,
                                  () => deleteWebhook(hook.webhook_id),
                                  `已删除「${hook.name}」。`,
                                );
                            }}
                          >
                            删除
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">
                还没有 Webhook。新建一个，把通知发到企业微信、钉钉或飞书群里。
              </p>
            )}
          </section>
        </>
      )}
      {dialog && (
        <DatasourceDialog
          ref={dialogRef}
          title={dialog.hook ? `修改「${dialog.hook.name}」` : "新建 Webhook"}
          onClose={() => setDialog(null)}
        >
          <WebhookForm
            hook={dialog.hook}
            apps={apps}
            onSaved={(hook) => {
              dialogRef.current?.close();
              setNotice(
                dialog.hook
                  ? `已保存「${hook.name}」。`
                  : `已建好「${hook.name}」，可以点「发测试消息」试一下。`,
              );
              setRevision((n) => n + 1);
            }}
          />
        </DatasourceDialog>
      )}
    </div>
  );
}
