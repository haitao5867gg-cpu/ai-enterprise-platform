"use client";

import Link from "next/link";
import { useState } from "react";
import {
  caseFromRun,
  rateRun,
  unrateRun,
  type RunFeedback,
} from "../lib/evals";
import { errorMessage, type Run } from "../lib/runs";
import { useMay } from "./session";

// "Was this right?" under a finished run, and for builders of a flow app, saving the
// run as an eval case (docs/api/evals.md, "运行反馈").
export function RunFeedbackPanel({
  run,
  onChange,
}: {
  run: Run;
  onChange: (feedback: RunFeedback) => void;
}) {
  const mayBuild = useMay("builder");
  const feedback = run.feedback;
  const [note, setNote] = useState(feedback?.mine?.note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  if (!feedback || (run.status !== "done" && run.status !== "error"))
    return null;
  const mine = feedback.mine;
  // Flow apps keep their eval set on the platform; a code app's is in the repository.
  const flowRun = Array.isArray(run.inputs);

  async function act(work: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  const rate = (rating: "good" | "bad") =>
    act(async () => {
      onChange(await rateRun(run.run_id, rating, note.trim() || null));
    });

  return (
    <section className="panel run-feedback" aria-labelledby="feedback-title">
      <div className="run-feedback-row">
        <h2 id="feedback-title">这次的结果对吗？</h2>
        <div className="run-feedback-buttons" role="group" aria-label="评价">
          <button
            type="button"
            className={`button ${mine?.rating === "good" ? "primary" : "secondary"}`}
            aria-pressed={mine?.rating === "good"}
            disabled={busy}
            onClick={() => void rate("good")}
          >
            👍 对
          </button>
          <button
            type="button"
            className={`button ${mine?.rating === "bad" ? "reject" : "secondary"}`}
            aria-pressed={mine?.rating === "bad"}
            disabled={busy}
            onClick={() => void rate("bad")}
          >
            👎 不对
          </button>
        </div>
        {feedback.good + feedback.bad > 0 && (
          <span className="muted">
            {feedback.good} 人说对 · {feedback.bad} 人说不对
          </span>
        )}
      </div>
      {mine && (
        <form
          className="run-feedback-note"
          onSubmit={(event) => {
            event.preventDefault();
            void rate(mine.rating);
          }}
        >
          <label className="asset-field">
            说一句为什么（可不填）
            <input
              value={note}
              maxLength={500}
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          <button
            className="button secondary"
            disabled={busy || note.trim() === (mine.note ?? "")}
          >
            保存说明
          </button>
          <button
            type="button"
            className="text-button"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                onChange(await unrateRun(run.run_id));
                setNote("");
              })
            }
          >
            撤回评价
          </button>
        </form>
      )}
      {flowRun && mayBuild && (
        <div className="run-feedback-case">
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                const rating =
                  mine?.rating ?? (run.status === "done" ? "good" : "bad");
                const made = await caseFromRun(run.app, run.run_id, rating);
                onChange({
                  ...feedback,
                  cases: [
                    ...feedback.cases,
                    { app: run.app, case_id: made.id },
                  ],
                });
                setSaved(
                  made.expect.length
                    ? `已存成评测用例 ${made.id}，按这次的结果生成了 ${made.expect.length} 项检查。`
                    : `已存成评测用例 ${made.id}。这次的结果不对，请到评测集里写上应该是什么。`,
                );
              })
            }
          >
            存成评测用例
          </button>
          <span className="muted">
            {mine?.rating === "bad"
              ? "按“不对”存：不带检查，由你补上应该是什么。"
              : "以后改应用时，用它检查结果没变坏。"}
          </span>
        </div>
      )}
      {saved && (
        <p className="asset-notice" role="status">
          {saved}{" "}
          <Link href={`/apps/${encodeURIComponent(run.app)}#release`}>
            去评测集看看
          </Link>
        </p>
      )}
      {feedback.cases.length > 0 && !saved && (
        <p className="muted">
          已存成评测用例：
          {feedback.cases.map((item, i) => (
            <span key={item.case_id}>
              {i > 0 && "、"}
              <Link
                href={`/apps/${encodeURIComponent(item.app)}#release`}
                className="monospace"
              >
                {item.case_id}
              </Link>
            </span>
          ))}
        </p>
      )}
      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
