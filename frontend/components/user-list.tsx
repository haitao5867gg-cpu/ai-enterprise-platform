"use client";

import { useEffect, useId, useRef, useState } from "react";
import {
  createUser,
  deleteUser,
  listUsers,
  PASSWORD_HINT,
  resetPassword,
  ROLES,
  roleHints,
  roleLabels,
  updateUser,
  USERNAME,
  USERNAME_HINT,
  type Role,
  type User,
} from "../lib/auth";
import { ApiError, errorMessage } from "../lib/runs";
import { AssetHeading, AssetLoading } from "./asset-ui";
import { DatasourceDialog } from "./datasource-form";
import { useSignedIn } from "./session";
import { ErrorNotice } from "./ui";

const time = (value: string | null) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "还没登录过";

type Dialog =
  | { kind: "create" }
  | { kind: "edit"; user: User }
  | { kind: "password"; user: User };

function RolePicker({
  value,
  onChange,
}: {
  value: Role[];
  onChange: (roles: Role[]) => void;
}) {
  return (
    <fieldset className="asset-field user-roles">
      <legend>角色</legend>
      {ROLES.map((role) => (
        <label key={role}>
          <input
            type="checkbox"
            checked={value.includes(role)}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? ROLES.filter((r) => r === role || value.includes(r))
                  : value.filter((r) => r !== role),
              )
            }
          />
          <span>
            <strong>{roleLabels[role]}</strong>
            <span className="muted">{roleHints[role]}</span>
          </span>
        </label>
      ))}
      <p className="field-hint">
        每个账号都能用使用者的功能。搭建者不自动有审批权限，做的人和批的人可以分开。
      </p>
    </fieldset>
  );
}

function UserForm({
  user,
  onSaved,
}: {
  user: User | null;
  onSaved: (user: User) => void;
}) {
  const id = useId();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [roles, setRoles] = useState<Role[]>(user?.roles ?? ["user"]);
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState(user?.email ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const emailOk =
    !email.trim() || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim());
  const blocker =
    !user && !USERNAME.test(username.trim().toLowerCase())
      ? `登录名${USERNAME_HINT}`
      : !displayName.trim()
        ? "请填显示名。"
        : !roles.length
          ? "至少选一个角色。"
          : !user && password.length < 8
            ? "初始密码至少 8 个字符。"
            : !emailOk
              ? "邮箱的格式不对。"
              : "";
  return (
    <form
      className="asset-form"
      noValidate
      onSubmit={async (event) => {
        event.preventDefault();
        if (blocker || saving) return;
        setSaving(true);
        setError("");
        try {
          onSaved(
            user
              ? await updateUser(user.user_id, {
                  display_name: displayName.trim(),
                  roles,
                  email: email.trim(),
                })
              : await createUser({
                  username: username.trim().toLowerCase(),
                  display_name: displayName.trim(),
                  roles,
                  password,
                  ...(email.trim() ? { email: email.trim() } : {}),
                }),
          );
        } catch (err) {
          setError(errorMessage(err));
          setSaving(false);
        }
      }}
    >
      {user ? (
        <div className="asset-field">
          <span>登录名</span>
          <code>{user.username}</code>
        </div>
      ) : (
        <div className="asset-field">
          <label htmlFor={`${id}-username`}>登录名</label>
          <input
            id={`${id}-username`}
            data-autofocus
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={40}
            value={username}
            aria-describedby={`${id}-username-hint`}
            onChange={(event) => setUsername(event.target.value)}
          />
          <p className="field-hint" id={`${id}-username-hint`}>
            {USERNAME_HINT}建好后不能改。
          </p>
        </div>
      )}
      <div className="asset-field">
        <label htmlFor={`${id}-name`}>显示名</label>
        <input
          id={`${id}-name`}
          data-autofocus={user ? true : undefined}
          maxLength={50}
          value={displayName}
          placeholder="运行和审批记录里写的名字，例如：张三"
          onChange={(event) => setDisplayName(event.target.value)}
        />
      </div>
      <div className="asset-field">
        <label htmlFor={`${id}-email`}>邮箱（选填）</label>
        <input
          id={`${id}-email`}
          type="email"
          autoComplete="off"
          maxLength={200}
          value={email}
          aria-describedby={`${id}-email-hint`}
          onChange={(event) => setEmail(event.target.value)}
        />
        <p className="field-hint" id={`${id}-email-hint`}>
          填了才收邮件通知（服务器要配好邮件，见「通知渠道」）。
        </p>
      </div>
      <RolePicker value={roles} onChange={setRoles} />
      {!user && (
        <div className="asset-field">
          <label htmlFor={`${id}-password`}>初始密码</label>
          <input
            id={`${id}-password`}
            type="password"
            autoComplete="new-password"
            maxLength={128}
            value={password}
            aria-describedby={`${id}-password-hint`}
            onChange={(event) => setPassword(event.target.value)}
          />
          <p className="field-hint" id={`${id}-password-hint`}>
            {PASSWORD_HINT}
            把初始密码私下告诉同事，请他登录后在左下角「修改密码」。
          </p>
        </div>
      )}
      {error && <ErrorNotice message={error} />}
      <div className="datasource-save-row">
        <button
          className="button primary"
          disabled={Boolean(blocker) || saving}
        >
          {saving ? "正在保存…" : "保存"}
        </button>
        {blocker && <p className="field-hint">{blocker}</p>}
      </div>
    </form>
  );
}

function PasswordForm({ user, onSaved }: { user: User; onSaved: () => void }) {
  const id = useId();
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  return (
    <form
      className="asset-form"
      noValidate
      onSubmit={async (event) => {
        event.preventDefault();
        if (password.length < 8 || saving) return;
        setSaving(true);
        setError("");
        try {
          await resetPassword(user.user_id, password);
          onSaved();
        } catch (err) {
          setError(errorMessage(err));
          setSaving(false);
        }
      }}
    >
      <div className="asset-field">
        <label htmlFor={`${id}-password`}>新密码</label>
        <input
          id={`${id}-password`}
          data-autofocus
          type="password"
          autoComplete="new-password"
          maxLength={128}
          value={password}
          aria-describedby={`${id}-hint`}
          onChange={(event) => setPassword(event.target.value)}
        />
        <p className="field-hint" id={`${id}-hint`}>
          {PASSWORD_HINT}重设后{user.display_name}
          的登录全部失效，要用新密码重新登录。
        </p>
      </div>
      {error && <ErrorNotice message={error} />}
      <div className="datasource-save-row">
        <button
          className="button primary"
          disabled={password.length < 8 || saving}
        >
          {saving ? "正在保存…" : "重设密码"}
        </button>
      </div>
    </form>
  );
}

export function UserList() {
  const me = useSignedIn();
  const [users, setUsers] = useState<User[] | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    listUsers(controller.signal)
      .then((value) => {
        setUsers(value);
        setError("");
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      });
    return () => controller.abort();
  }, [revision]);

  async function act(run: () => Promise<unknown>, done: string) {
    setActionError("");
    setNotice("");
    try {
      await run();
      setNotice(done);
      setRevision((n) => n + 1);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : errorMessage(err));
    }
  }
  const close = (message: string) => {
    dialogRef.current?.close();
    setNotice(message);
    setRevision((n) => n + 1);
  };

  return (
    <div className="asset-page">
      <AssetHeading title="账号">
        给同事建账号、分角色。使用者发起运行和查看结果；审批者处理人工确认和上线审批；搭建者搭建应用、Skill、知识库和数据源；管理员管理模型、账号和操作记录。
      </AssetHeading>
      <div className="asset-actions">
        <button
          type="button"
          className="button primary"
          onClick={() => {
            setNotice("");
            setDialog({ kind: "create" });
          }}
        >
          新建账号
        </button>
      </div>
      {error && (
        <ErrorNotice
          message={error}
          onRetry={() => setRevision((n) => n + 1)}
        />
      )}
      {!users && !error && <AssetLoading />}
      {notice && (
        <p className="datasource-note" role="status">
          {notice}
        </p>
      )}
      {actionError && <ErrorNotice message={actionError} />}
      {users && (
        <section className="panel asset-section datasource-section">
          <div className="table-scroll">
            <table className="mapping-table user-table" aria-label="账号">
              <thead>
                <tr>
                  {["账号", "角色", "状态", "最近登录", "操作"].map((h) => (
                    <th scope="col" key={h}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {users.map((user) => {
                  const self = me?.user_id === user.user_id;
                  return (
                    <tr key={user.user_id}>
                      <td>
                        <div className="model-name-stack">
                          <strong>
                            {user.display_name}
                            {self && <span className="muted">（你）</span>}
                          </strong>
                          <code>{user.username}</code>
                        </div>
                      </td>
                      <td>
                        <span className="model-tags">
                          {user.roles.map((role) => (
                            <span key={role} className="datasource-tag">
                              {roleLabels[role]}
                            </span>
                          ))}
                        </span>
                      </td>
                      <td>
                        <span
                          className={`datasource-health ${user.disabled ? "error" : "ok"}`}
                        >
                          {user.disabled ? "已停用" : "正常"}
                        </span>
                      </td>
                      <td className="muted">{time(user.last_login_at)}</td>
                      <td className="model-actions">
                        <button
                          type="button"
                          className="text-button"
                          aria-label={`修改「${user.display_name}」`}
                          onClick={() => setDialog({ kind: "edit", user })}
                        >
                          修改
                        </button>
                        <button
                          type="button"
                          className="text-button"
                          aria-label={`重设「${user.display_name}」的密码`}
                          onClick={() => setDialog({ kind: "password", user })}
                        >
                          重设密码
                        </button>
                        {!self && (
                          <button
                            type="button"
                            className="text-button"
                            aria-label={`${user.disabled ? "启用" : "停用"}「${user.display_name}」`}
                            onClick={() =>
                              void act(
                                () =>
                                  updateUser(user.user_id, {
                                    disabled: !user.disabled,
                                  }),
                                `已${user.disabled ? "启用" : "停用"}「${user.display_name}」。`,
                              )
                            }
                          >
                            {user.disabled ? "启用" : "停用"}
                          </button>
                        )}
                        {!self && (
                          <button
                            type="button"
                            className="text-button danger-text"
                            aria-label={`删除「${user.display_name}」`}
                            onClick={() => {
                              if (
                                window.confirm(
                                  `确定删除「${user.display_name}」的账号吗？运行和审批记录里的名字会保留。`,
                                )
                              )
                                void act(
                                  () => deleteUser(user.user_id),
                                  `已删除「${user.display_name}」。`,
                                );
                            }}
                          >
                            删除
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {dialog && (
        <DatasourceDialog
          ref={dialogRef}
          title={
            dialog.kind === "create"
              ? "新建账号"
              : dialog.kind === "edit"
                ? `修改「${dialog.user.display_name}」`
                : `重设「${dialog.user.display_name}」的密码`
          }
          onClose={() => setDialog(null)}
        >
          {dialog.kind === "password" ? (
            <PasswordForm
              user={dialog.user}
              onSaved={() =>
                close(`已重设「${dialog.user.display_name}」的密码。`)
              }
            />
          ) : (
            <UserForm
              user={dialog.kind === "edit" ? dialog.user : null}
              onSaved={(user) =>
                close(
                  dialog.kind === "create"
                    ? `已建好「${user.display_name}」的账号。`
                    : `已保存「${user.display_name}」。`,
                )
              }
            />
          )}
        </DatasourceDialog>
      )}
    </div>
  );
}
