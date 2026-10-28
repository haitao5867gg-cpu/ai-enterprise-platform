"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { archiveApp, updateApp, type AppDetail } from "../lib/apps";
import { errorMessage } from "../lib/runs";
import { ErrorNotice } from "./ui";

export function AppMetadata({
  data,
  changed,
}: {
  data: AppDetail;
  changed: (data: AppDetail) => void;
}) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(data.title);
  const [summary, setSummary] = useState(data.summary);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <section className="panel asset-section" aria-label="应用信息">
      <div className="asset-section-heading">
        <h2>应用信息</h2>
        <div className="asset-actions">
          <button
            className="button secondary"
            type="button"
            disabled={busy}
            onClick={() => {
              setEditing(!editing);
              setTitle(data.title);
              setSummary(data.summary);
            }}
          >
            修改名称和简介
          </button>
          <button
            type="button"
            className="button reject"
            disabled={busy}
            onClick={async () => {
              if (
                !window.confirm(
                  `确定归档「${data.title}」吗？归档后不再显示在应用列表，历史运行仍可查看。`,
                )
              )
                return;
              setBusy(true);
              setError("");
              try {
                await archiveApp(data.app);
                router.push("/apps");
              } catch (err) {
                setError(errorMessage(err));
                setBusy(false);
              }
            }}
          >
            归档应用
          </button>
        </div>
      </div>
      {editing && (
        <form
          className="asset-form"
          onSubmit={async (event) => {
            event.preventDefault();
            if (busy) return;
            if (!title.trim() || title.length > 50 || summary.length > 500) {
              setError("名称为 1–50 个字符，简介最多 500 个字符。");
              return;
            }
            setBusy(true);
            setError("");
            try {
              changed(
                await updateApp(data.app, { title: title.trim(), summary }),
              );
              setEditing(false);
            } catch (err) {
              setError(errorMessage(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="asset-field">
            名称
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={50}
              disabled={busy}
            />
          </label>
          <label className="asset-field">
            简介
            <textarea
              value={summary}
              rows={3}
              onChange={(e) => setSummary(e.target.value)}
              maxLength={500}
              disabled={busy}
            />
          </label>
          <button className="button primary" disabled={busy}>
            {busy ? "保存中…" : "保存应用信息"}
          </button>
        </form>
      )}
      {error && <ErrorNotice message={error} />}
    </section>
  );
}
