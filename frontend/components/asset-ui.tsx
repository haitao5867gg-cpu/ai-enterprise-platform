"use client";

import { useEffect, type ReactNode } from "react";
import { saveActor } from "../lib/actor";

export function AssetHeading({
  eyebrow,
  title,
  children,
  action,
}: {
  eyebrow?: string;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="page-heading asset-heading">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
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
      你的名字<span className="muted">和运行页共用，浏览器会记住</span>
      <input
        aria-label="你的名字"
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

export const UNSAVED_MESSAGE = "有未保存的修改，确定离开这个页面吗？";

export function useUnsavedWarning(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    function warn(event: BeforeUnloadEvent) {
      event.preventDefault();
    }
    // In-app links navigate on the client and never fire beforeunload: ask first, and
    // stop the click before Next.js sees it (capture phase, on the document).
    function guard(event: MouseEvent) {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = (event.target as Element | null)?.closest?.("a[href]");
      if (!(link instanceof HTMLAnchorElement) || link.target === "_blank") return;
      const url = new URL(link.href, window.location.href);
      if (url.origin !== window.location.origin) return;
      if (url.pathname === window.location.pathname && url.search === window.location.search) {
        return;
      }
      if (!window.confirm(UNSAVED_MESSAGE)) {
        event.preventDefault();
        event.stopPropagation();
      }
    }
    window.addEventListener("beforeunload", warn);
    document.addEventListener("click", guard, true);
    return () => {
      window.removeEventListener("beforeunload", warn);
      document.removeEventListener("click", guard, true);
    };
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
