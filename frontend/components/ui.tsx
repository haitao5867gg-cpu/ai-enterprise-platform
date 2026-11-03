import type { ReactNode } from "react";
import { statusText, type RunSummary } from "../lib/runs";
import { useT } from "../lib/i18n/client";
import { Icon } from "./icons";

export function StatusBadge({
  run,
}: {
  run: Pick<RunSummary, "status" | "outcome" | "step">;
}) {
  const t = useT();
  const stopped = run.status === "done" && run.outcome === "stopped";
  return (
    <span className={`status-badge ${run.status}${stopped ? " stopped" : ""}`}>
      <span className="dot" />
      {t(statusText(run))}
    </span>
  );
}

export function ErrorNotice({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  const t = useT();
  return (
    <div className="error-notice" role="alert">
      <Icon name="alert" />
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="text-button" onClick={onRetry}>
          {t("重试")}
          <Icon name="refresh" size={14} />
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon name="runs" size={30} />
      </span>
      <h2>{title}</h2>
      <p>{children}</p>
      {action}
    </div>
  );
}
