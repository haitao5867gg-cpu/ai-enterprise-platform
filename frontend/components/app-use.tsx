"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { converses, getApp, isAgentConfig, type AppDetail } from "../lib/apps";
import {
  readRunList,
  requestJson,
  runTitle,
  type RunSummary,
} from "../lib/runs";
import { CopyButton } from "./app-access";
import { AppChatUse } from "./app-chat-use";
import { useManual } from "./app-manual";
import { AssetTime } from "./asset-ui";
import { NewRun } from "./new-run";
import { useMay } from "./session";
import { StatusBadge } from "./ui";

// An app's own page for the people who use it: what it does, where it will ask them
// something, the form to start it and their recent runs. Shareable as /use/{app}.
export function AppUse({
  app,
  conversation = null,
}: {
  app: string;
  conversation?: string | null;
}) {
  const mayBuild = useMay("builder");
  const [detail, setDetail] = useState<AppDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [link, setLink] = useState("");
  const { manual } = useManual(`/apps/${encodeURIComponent(app)}/manual`);
  useEffect(() => {
    const frame = requestAnimationFrame(() =>
      setLink(`${window.location.origin}/use/${encodeURIComponent(app)}`),
    );
    return () => cancelAnimationFrame(frame);
  }, [app]);
  useEffect(() => {
    const controller = new AbortController();
    getApp(app, controller.signal)
      .then((value) => !controller.signal.aborted && setDetail(value))
      .catch(() => {
        // The form below says what went wrong.
        if (!controller.signal.aborted) setFailed(true);
      });
    requestJson("/runs", { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted)
          setRuns(readRunList(value).filter((run) => run.app === app));
      })
      .catch(() => {
        // Recent runs are a convenience: without them the page still works.
        if (!controller.signal.aborted) setRuns([]);
      });
    return () => controller.abort();
  }, [app]);
  return (
    <div className="app-use">
      <div className="page-heading">
        <div>
          <h1>{detail?.title ?? manual?.title ?? app}</h1>
          {detail || failed ? (
            (detail?.summary || manual?.summary) && (
              <p>{detail?.summary || manual?.summary}</p>
            )
          ) : (
            <p>{manual?.summary || "正在读取应用…"}</p>
          )}
        </div>
        <div className="asset-actions">
          {link && <CopyButton text={link} label="复制使用页链接" />}
          {mayBuild && (
            <Link
              className="button secondary"
              href={`/apps/${encodeURIComponent(app)}`}
            >
              应用设计
            </Link>
          )}
        </div>
      </div>
      {detail && converses(detail.current.config) ? (
        <AppChatUse
          app={app}
          detail={detail}
          initial={conversation}
          chat={
            detail.chat ??
            (isAgentConfig(detail.current.config)
              ? detail.current.config.chat
              : null)
          }
        />
      ) : !detail && !failed ? (
        <p className="muted" role="status">
          正在读取应用…
        </p>
      ) : (
        <div className="app-use-grid">
          <NewRun locked={app} />
          <aside className="app-use-side">
            <section className="panel" aria-labelledby="use-steps">
              <h2 id="use-steps">会在哪里问你</h2>
              {!manual ? (
                <p className="muted" role="status">
                  正在读取…
                </p>
              ) : manual.human.length ? (
                <ol className="app-use-human">
                  {manual.human.map((step) => (
                    <li key={step.id}>
                      <strong>{step.title}</strong>
                      {step.prompt && <p className="muted">{step.prompt}</p>}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="muted">这个应用会一口气跑完，中途不用人确认。</p>
              )}
            </section>
            <section className="panel" aria-labelledby="use-runs">
              <h2 id="use-runs">最近的运行</h2>
              {runs === null ? (
                <p className="muted" role="status">
                  正在读取…
                </p>
              ) : runs.length ? (
                <ul className="app-use-runs" aria-label="这个应用最近的运行">
                  {runs.slice(0, 8).map((run) => (
                    <li key={run.run_id}>
                      <Link href={`/runs/${encodeURIComponent(run.run_id)}`}>
                        {runTitle(run)}
                      </Link>
                      <span className="muted">
                        {run.created_by ?? "未署名"}
                        {run.created_at && (
                          <>
                            {" · "}
                            <AssetTime value={run.created_at} />
                          </>
                        )}
                      </span>
                      <StatusBadge run={run} />
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">还没有人用过，填好左边就能开始。</p>
              )}
            </section>
          </aside>
        </div>
      )}
    </div>
  );
}
