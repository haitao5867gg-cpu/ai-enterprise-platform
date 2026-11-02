"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useId, useState } from "react";
import {
  login,
  PASSWORD_HINT,
  setupAdmin,
  USERNAME,
  USERNAME_HINT,
} from "../lib/auth";
import { errorMessage } from "../lib/runs";
import { Icon } from "./icons";
import { useSession } from "./session";
import { ErrorNotice } from "./ui";

// Only a path inside the platform: never an address on another site.
function safeNext(value: string | null) {
  return value && value.startsWith("/") && !value.startsWith("//")
    ? value
    : "/";
}

export function LoginPage() {
  const id = useId();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const { status, loaded } = useSession();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const setup = Boolean(status?.setup_needed);
  // Already signed in, or sign-in is off: nothing to do here.
  useEffect(() => {
    if (status && (!status.enabled || status.user))
      window.location.replace(next);
  }, [status, next]);

  const blocker = setup
    ? !USERNAME.test(username.trim().toLowerCase())
      ? `登录名${USERNAME_HINT}`
      : !displayName.trim()
        ? "请填显示名。"
        : password.length < 8
          ? "密码至少 8 个字符。"
          : password !== again
            ? "两次填的密码不一样。"
            : ""
    : !username.trim() || !password
      ? "请填登录名和密码。"
      : "";

  async function submit() {
    if (blocker || busy) return;
    setBusy(true);
    setError("");
    try {
      if (setup)
        await setupAdmin({
          username: username.trim().toLowerCase(),
          display_name: displayName.trim(),
          password,
        });
      else await login(username.trim(), password);
      // A full load, so every page starts with the new account.
      window.location.replace(next);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <main className="login-page" id="main-content">
      <div className="login-card">
        <div className="brand login-brand">
          <span className="brand-mark">
            <Icon name="grid" size={21} />
          </span>
          <span>
            AEP<span className="brand-subtitle">企业 AI 应用平台</span>
          </span>
        </div>
        {!loaded ? (
          <div className="loading-state" role="status">
            <span className="spinner" />
            正在加载…
          </div>
        ) : (
          <form
            className="asset-form"
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <h1>{setup ? "建第一个管理员" : "登录"}</h1>
            {setup && (
              <p className="field-hint">
                平台里还没有账号。先建一个管理员，之后在「账号」里给同事建账号、分角色。
              </p>
            )}
            <div className="asset-field">
              <label htmlFor={`${id}-username`}>登录名</label>
              <input
                id={`${id}-username`}
                autoFocus
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                maxLength={40}
                value={username}
                aria-describedby={setup ? `${id}-username-hint` : undefined}
                onChange={(event) => setUsername(event.target.value)}
              />
              {setup && (
                <p className="field-hint" id={`${id}-username-hint`}>
                  {USERNAME_HINT}
                </p>
              )}
            </div>
            {setup && (
              <div className="asset-field">
                <label htmlFor={`${id}-name`}>显示名</label>
                <input
                  id={`${id}-name`}
                  autoComplete="name"
                  maxLength={50}
                  value={displayName}
                  placeholder="运行和审批记录里写的名字，例如：张三"
                  onChange={(event) => setDisplayName(event.target.value)}
                />
              </div>
            )}
            <div className="asset-field">
              <label htmlFor={`${id}-password`}>密码</label>
              <input
                id={`${id}-password`}
                type="password"
                autoComplete={setup ? "new-password" : "current-password"}
                maxLength={128}
                value={password}
                aria-describedby={setup ? `${id}-password-hint` : undefined}
                onChange={(event) => setPassword(event.target.value)}
              />
              {setup && (
                <p className="field-hint" id={`${id}-password-hint`}>
                  {PASSWORD_HINT}
                </p>
              )}
            </div>
            {setup && (
              <div className="asset-field">
                <label htmlFor={`${id}-again`}>再填一次密码</label>
                <input
                  id={`${id}-again`}
                  type="password"
                  autoComplete="new-password"
                  maxLength={128}
                  value={again}
                  onChange={(event) => setAgain(event.target.value)}
                />
              </div>
            )}
            {error && <ErrorNotice message={error} />}
            <button
              className="button primary login-submit"
              disabled={Boolean(blocker) || busy}
              aria-describedby={blocker ? `${id}-blocker` : undefined}
            >
              {busy ? "请稍候…" : setup ? "建管理员并登录" : "登录"}
            </button>
            {blocker && setup && (
              <p className="field-hint" id={`${id}-blocker`}>
                {blocker}
              </p>
            )}
            {!setup && (
              <p className="field-hint">忘了密码请找管理员在「账号」里重设。</p>
            )}
          </form>
        )}
      </div>
    </main>
  );
}
