"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { actorPayload } from "../lib/actor";
import { APP_ID_PATTERN, convertToFlow, type AppDetail } from "../lib/apps";
import { useT } from "../lib/i18n/client";
import { errorMessage } from "../lib/runs";
import { Dialog } from "./catalog-list";
import { useMay } from "./session";
import { ErrorNotice } from "./ui";

// "转成编排应用" (docs/api/agent-apps.md): copies the agent app into a new flow app
// that runs the same way, for people who need approvals or branches. The agent app stays.
export function ConvertToFlow({ app, data }: { app: string; data: AppDetail }) {
  const t = useT();
  const router = useRouter();
  const mayBuild = useMay("builder");
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(`${app}-flow`.slice(0, 64));
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!mayBuild) return null;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!APP_ID_PATTERN.test(name)) {
      setError(
        t("标识须以小写字母开头，使用小写字母、数字或连字符，共 2–64 字符。"),
      );
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await convertToFlow(app, {
        app: name,
        ...(title.trim() ? { title: title.trim() } : {}),
        ...actorPayload(),
      });
      router.push(`/apps/${encodeURIComponent(created.app)}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }
  return (
    <>
      <button
        type="button"
        className="button secondary"
        onClick={() => setOpen(true)}
      >
        {t("转成编排应用")}
      </button>
      {open && (
        <Dialog title={t("转成编排应用")} close={() => setOpen(false)}>
          <form className="asset-form" onSubmit={submit} noValidate>
            <p className="muted">
              {t(
                "复制出一个新的编排应用，流程是「收到消息 → 智能体 → 回答」，用当前版本 v{version} 的配置和评测集，可以继续加审批、分支。这个智能体应用不变；反过来转不回来。",
                { version: data.current_version },
              )}
            </p>
            <div className="asset-field">
              <label htmlFor="convert-app">{t("新应用标识")}</label>
              <input
                id="convert-app"
                value={name}
                maxLength={64}
                autoComplete="off"
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div className="asset-field">
              <label htmlFor="convert-title">{t("名称（选填）")}</label>
              <input
                id="convert-title"
                value={title}
                maxLength={50}
                placeholder={data.title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </div>
            {error && <ErrorNotice message={error} />}
            <div className="asset-actions">
              <button className="button primary" disabled={busy}>
                {busy ? t("正在复制…") : t("复制成编排应用")}
              </button>
            </div>
          </form>
        </Dialog>
      )}
    </>
  );
}
