"use client";

import { useEffect, type ReactNode } from "react";
import { saveActor } from "../lib/actor";

export function AssetHeading({
  eyebrow,
  title,
  children,
  action,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="page-heading asset-heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        {children && <p>{children}</p>}
      </div>
      {action}
    </div>
  );
}

export function AssetLoading() {
  return (
    <div className="loading-state" role="status">
      <span className="spinner" />
      正在加载…
    </div>
  );
}

export function ActorField({
  actor,
  onChange,
  disabled,
}: {
  actor: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <label className="asset-field">
      审批人<span className="muted">与运行工作台共用，浏览器会记住</span>
      <input
        aria-label="审批人"
        value={actor}
        disabled={disabled}
        onChange={(event) => {
          onChange(event.target.value);
          saveActor(event.target.value);
        }}
        autoComplete="name"
      />
    </label>
  );
}

export function useUnsavedWarning(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    function warn(event: BeforeUnloadEvent) {
      event.preventDefault();
    }
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
}

export function VersionPicker({
  versions,
  selected,
  onChange,
}: {
  versions: number[];
  selected: number[];
  onChange: (value: number[]) => void;
}) {
  return (
    <fieldset className="version-picker">
      <legend>选择两个版本对比</legend>
      {versions.map((version) => (
        <label key={version}>
          <input
            type="checkbox"
            checked={selected.includes(version)}
            disabled={selected.length === 2 && !selected.includes(version)}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...selected, version].sort((a, b) => a - b)
                  : selected.filter((item) => item !== version),
              )
            }
          />
          v{version}
        </label>
      ))}
    </fieldset>
  );
}

export function AssetTime({ value }: { value: string }) {
  return (
    <time dateTime={value}>{new Date(value).toLocaleString("zh-CN")}</time>
  );
}
