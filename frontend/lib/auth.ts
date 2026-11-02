import { requestJson } from "./runs";

// docs/api/auth.md is the source of truth for these wire types.
export type Role = "user" | "approver" | "builder" | "admin";
export type User = {
  user_id: string;
  username: string;
  display_name: string;
  roles: Role[];
  disabled: boolean;
  created_at: string;
  last_login_at: string | null;
  email?: string | null;
};
export type AuthStatus = {
  enabled: boolean;
  setup_needed: boolean;
  user: User | null;
};
export type AuditEvent = {
  id: number;
  at: string;
  user: Pick<User, "user_id" | "username" | "display_name"> | null;
  action: string;
  label: string;
  target: Record<string, string>;
  status: number;
  ip: string | null;
};
export type AuditPage = { events: AuditEvent[]; next: number | null };

export const ROLES: Role[] = ["user", "approver", "builder", "admin"];
export const roleLabels: Record<Role, string> = {
  user: "使用者",
  approver: "审批者",
  builder: "搭建者",
  admin: "管理员",
};
export const roleHints: Record<Role, string> = {
  user: "看运行记录、发起运行、填表单",
  approver: "处理人工确认和上线审批",
  builder: "搭建应用、Skill、知识库、数据源和项目",
  admin: "全部，包括模型、账号和操作记录",
};

// Everyone signed in is a user; an admin may do everything (docs/api/auth.md).
export function hasRole(user: User | null | undefined, role: Role): boolean {
  if (!user) return false;
  return (
    role === "user" || user.roles.includes("admin") || user.roles.includes(role)
  );
}

// Which role a page needs, by path; the navigation and the page guard both use it.
export function pageRole(pathname: string): Role {
  const under = (prefix: string) =>
    pathname === prefix || pathname.startsWith(`${prefix}/`);
  if (under("/users") || under("/audit") || under("/notification-channels"))
    return "admin";
  if (
    [
      "/catalog",
      "/apps",
      "/skills",
      "/knowledge",
      "/datasources",
      "/projects",
      "/models",
      "/ops",
    ]
      .map(under)
      .some(Boolean)
  )
    return "builder";
  return "user";
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
export const getAuthStatus = (signal?: AbortSignal) =>
  requestJson("/auth/status", { signal }) as Promise<AuthStatus>;
export const login = (username: string, password: string) =>
  requestJson(
    "/auth/login",
    json("POST", { username, password }),
  ) as Promise<User>;
export const setupAdmin = (input: {
  username: string;
  display_name: string;
  password: string;
}) => requestJson("/auth/setup", json("POST", input)) as Promise<User>;
export const logout = () => requestJson("/auth/logout", json("POST"));
export const changePassword = (old_password: string, password: string) =>
  requestJson("/auth/me/password", json("PUT", { old_password, password }));
export const listUsers = (signal?: AbortSignal) =>
  requestJson("/users", { signal }) as Promise<User[]>;
export const createUser = (input: {
  username: string;
  display_name: string;
  roles: Role[];
  password: string;
  email?: string;
}) => requestJson("/users", json("POST", input)) as Promise<User>;
export const updateUser = (
  id: string,
  changes: Partial<Pick<User, "display_name" | "roles" | "disabled">> & {
    email?: string;
  },
) =>
  requestJson(
    `/users/${encodeURIComponent(id)}`,
    json("PATCH", changes),
  ) as Promise<User>;
export const resetPassword = (id: string, password: string) =>
  requestJson(
    `/users/${encodeURIComponent(id)}/password`,
    json("PUT", { password }),
  );
export const deleteUser = (id: string) =>
  requestJson(`/users/${encodeURIComponent(id)}`, json("DELETE"));
export function listAudit(
  params: { before?: number; user?: string; q?: string },
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ limit: "50" });
  if (params.before) query.set("before", String(params.before));
  if (params.user) query.set("user", params.user);
  if (params.q) query.set("q", params.q);
  return requestJson(`/audit?${query}`, { signal }) as Promise<AuditPage>;
}

export const USERNAME = /^[a-z][a-z0-9._-]{1,39}$/;
export const USERNAME_HINT =
  "小写字母开头，只能有小写字母、数字、. _ 和 -，2–40 个字符。";
export const PASSWORD_HINT = "8–128 个字符。";
