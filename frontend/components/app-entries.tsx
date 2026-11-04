"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ENTRIES,
  entryHelp,
  entryLabels,
  getEntries,
  setEntries,
  type AppEntries,
  type Entry,
} from "../lib/app-entries";
import { useT } from "../lib/i18n/client";
import { errorMessage } from "../lib/runs";
import { AssetTime } from "./asset-ui";
import { useMay } from "./session";
import { ErrorNotice } from "./ui";

// The app's four ways in, each with its switch and when it was last used, on the 接入 tab
// (docs/api/app-access.md, "入口"). Like keys, the switches are not part of a version.
export function AppEntriesPanel({ app }: { app: string }) {
  const t = useT();
  const mayBuild = useMay("builder");
  const [entries, setState] = useState<AppEntries | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<Entry | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    getEntries(app, controller.signal)
      .then((value) => {
        setState(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app, revision]);

  async function toggle(entry: Entry, enabled: boolean) {
    if (
      !enabled &&
      entry === "apps" &&
      entries?.apps.callers.length &&
      !window.confirm(
        t("有 {n} 个应用在调用它，关掉后它们的调用会失败或不再提供。继续吗？", {
          n: entries.apps.callers.length,
        }),
      )
    )
      return;
    setBusy(entry);
    setError("");
    try {
      setState(await setEntries(app, { [entry]: enabled }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      className="panel asset-section app-entries"
      aria-labelledby="app-entries-title"
    >
      <h2 id="app-entries-title">{t("入口")}</h2>
      <p className="muted">
        {t(
          "每个入口可以单独关掉，改了立刻生效，不随发布和回滚变化。关掉的入口来的请求会被拒绝。",
        )}
      </p>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!entries && !error && (
        <p className="muted" role="status">
          {t("正在读取…")}
        </p>
      )}
      {entries && (
        <div className="table-scroll">
          <table className="mapping-table entries-table">
            <caption className="sr-only">{t("入口")}</caption>
            <thead>
              <tr>
                <th scope="col">{t("入口")}</th>
                <th scope="col">{t("开放")}</th>
                <th scope="col">{t("最近使用")}</th>
                <th scope="col">{t("说明")}</th>
              </tr>
            </thead>
            <tbody>
              {ENTRIES.map((entry) => {
                const state = entries[entry];
                return (
                  <tr key={entry}>
                    <th scope="row">{t(entryLabels[entry])}</th>
                    <td>
                      <label className="entry-switch">
                        <input
                          type="checkbox"
                          role="switch"
                          checked={state.enabled}
                          disabled={!mayBuild || busy !== null}
                          aria-label={t("开放「{entry}」", {
                            entry: t(entryLabels[entry]),
                          })}
                          onChange={(event) =>
                            void toggle(entry, event.target.checked)
                          }
                        />
                        <span>{state.enabled ? t("开着") : t("关了")}</span>
                      </label>
                    </td>
                    <td>
                      {state.last_used_at ? (
                        <AssetTime value={state.last_used_at} />
                      ) : (
                        <span className="muted">{t("还没用过")}</span>
                      )}
                    </td>
                    <td>
                      <span className="muted">{t(entryHelp[entry])}</span>
                      {entry === "apps" && (
                        <Callers callers={entries.apps.callers} />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {!mayBuild && (
        <p className="muted">{t("入口由搭建者管理。")}</p>
      )}
    </section>
  );
}

function Callers({ callers }: { callers: AppEntries["apps"]["callers"] }) {
  const t = useT();
  if (!callers.length)
    return <p className="entry-callers muted">{t("现在没有应用调用它。")}</p>;
  return (
    <p className="entry-callers">
      {t("在调用它的应用：")}
      {callers.map((caller, i) => (
        <span key={`${caller.app}-${caller.via}`}>
          {i > 0 && t("、")}
          <Link href={`/apps/${encodeURIComponent(caller.app)}`}>
            {caller.title}
          </Link>
          {t("（v{version} · {via}）", {
            version: caller.version,
            via: caller.via === "node" ? t("调用应用节点") : t("智能体"),
          })}
        </span>
      ))}
    </p>
  );
}
