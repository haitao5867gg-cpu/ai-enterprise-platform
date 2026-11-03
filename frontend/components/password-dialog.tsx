"use client";

import { useId, useState, type RefObject } from "react";
import { changePassword, PASSWORD_HINT } from "../lib/auth";
import { errorMessage } from "../lib/runs";
import { useT } from "../lib/i18n/client";
import { DatasourceDialog } from "./datasource-form";
import { ErrorNotice } from "./ui";

export function PasswordDialog({
  ref,
  onClose,
}: {
  ref: RefObject<HTMLDialogElement | null>;
  onClose: () => void;
}) {
  const t = useT();
  const id = useId();
  const [old, setOld] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const blocker = t(
    !old
      ? "请填原密码。"
      : next.length < 8
        ? "新密码至少 8 个字符。"
        : next !== again
          ? "两次填的新密码不一样。"
          : "",
  );
  return (
    <DatasourceDialog ref={ref} title={t("修改密码")} onClose={onClose}>
      {done ? (
        <p className="datasource-note" role="status">
          {t(
            "密码已经改好。你在其他浏览器上的登录已经失效，需要用新密码重新登录。",
          )}
        </p>
      ) : (
        <form
          className="asset-form"
          noValidate
          onSubmit={async (event) => {
            event.preventDefault();
            if (blocker || saving) return;
            setSaving(true);
            setError("");
            try {
              await changePassword(old, next);
              setDone(true);
            } catch (err) {
              setError(errorMessage(err));
            } finally {
              setSaving(false);
            }
          }}
        >
          <div className="asset-field">
            <label htmlFor={`${id}-old`}>{t("原密码")}</label>
            <input
              id={`${id}-old`}
              data-autofocus
              type="password"
              autoComplete="current-password"
              value={old}
              onChange={(event) => setOld(event.target.value)}
            />
          </div>
          <div className="asset-field">
            <label htmlFor={`${id}-new`}>{t("新密码")}</label>
            <input
              id={`${id}-new`}
              type="password"
              autoComplete="new-password"
              maxLength={128}
              value={next}
              aria-describedby={`${id}-hint`}
              onChange={(event) => setNext(event.target.value)}
            />
            <p className="field-hint" id={`${id}-hint`}>
              {t(PASSWORD_HINT)}
            </p>
          </div>
          <div className="asset-field">
            <label htmlFor={`${id}-again`}>{t("再填一次新密码")}</label>
            <input
              id={`${id}-again`}
              type="password"
              autoComplete="new-password"
              maxLength={128}
              value={again}
              onChange={(event) => setAgain(event.target.value)}
            />
          </div>
          {error && <ErrorNotice message={error} />}
          <div className="datasource-save-row">
            <button
              className="button primary"
              disabled={Boolean(blocker) || saving}
            >
              {t(saving ? "正在保存…" : "保存")}
            </button>
            {blocker && <p className="field-hint">{blocker}</p>}
          </div>
        </form>
      )}
    </DatasourceDialog>
  );
}
