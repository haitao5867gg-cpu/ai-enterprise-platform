"use client";

import { useEffect, useState } from "react";
import { getBudget, type Budget } from "../lib/ops";
import { errorMessage } from "../lib/runs";
import { BudgetCell, BudgetDialog } from "./ops-dashboard";
import { useMay } from "./session";
import { ErrorNotice } from "./ui";

// The app's monthly model budget on its 评测与发布 tab (docs/api/ops.md, 「预算」).
export function AppBudget({ app, title }: { app: string; title: string }) {
  const [budget, setBudget] = useState<Budget | null | undefined>(undefined);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [revision, setRevision] = useState(0);
  const admin = useMay("admin");
  useEffect(() => {
    const controller = new AbortController();
    getBudget(app, controller.signal)
      .then((value) => {
        setBudget(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [app, revision]);
  return (
    <section className="panel asset-section" aria-label="模型预算">
      <h2>模型预算</h2>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {budget === undefined && !error && <p className="muted">正在加载…</p>}
      {budget !== undefined && (
        <>
          <p className="muted">
            {budget
              ? `本月已用 / 每月预算，提醒线 ${Math.round(budget.warn_at * 100)}%。`
              : "没设预算，这个应用的模型调用不受限制。"}
            {admin ? "" : "只有管理员能设预算。"}
          </p>
          <div className="ops-price">
            <BudgetCell budget={budget} />
            {admin && (
              <button
                type="button"
                className="button secondary"
                onClick={() => setEditing(true)}
              >
                {budget ? "修改预算" : "设预算"}
              </button>
            )}
          </div>
        </>
      )}
      {editing && (
        <BudgetDialog
          app={app}
          title={title}
          budget={budget ?? null}
          close={() => setEditing(false)}
          saved={(value) => {
            setBudget(value);
            setEditing(false);
          }}
        />
      )}
    </section>
  );
}
