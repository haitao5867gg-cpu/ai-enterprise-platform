import "server-only";
import { randomUUID } from "node:crypto";
import type { AuditEvent, Role, User } from "../auth";
import { ROLES, USERNAME } from "../auth";
import { mockError, mockJson, noContent, readBody } from "./mock-assets";

// Mock of docs/api/auth.md. Sign-in is off (everyone is the local user) unless the
// browser sends the cookie aep_mock_auth=on; aep_mock_auth=setup acts as a platform
// with no accounts yet. Seeded accounts, all with the password "correct-horse":
// admin (王管理, admin), lilei (李雷, user), hanmeimei (韩梅梅, approver),
// builder (王搭建, builder). Gaps against the backend: other mock APIs do not check
// roles (pages do), there is no lockout, and sessions never expire.
const PASSWORD = "correct-horse";
const CREATED = "2026-01-05T08:00:00.000Z";
const seed = (
  user_id: string,
  username: string,
  display_name: string,
  roles: Role[],
): User => ({
  user_id,
  username,
  display_name,
  roles,
  disabled: false,
  created_at: CREATED,
  last_login_at: null,
});
const LOCAL = seed("local", "local", "本地用户", ["admin"]);
const globalStore = globalThis as typeof globalThis & {
  aepMockUsers?: Map<string, User & { password: string }>;
  aepMockSessions?: Map<string, string>;
  aepMockAudit?: AuditEvent[];
};
const users = (globalStore.aepMockUsers ??= new Map(
  [
    seed("u_admin", "admin", "王管理", ["admin"]),
    seed("u_lilei", "lilei", "李雷", ["user"]),
    seed("u_hanmeimei", "hanmeimei", "韩梅梅", ["approver"]),
    seed("u_builder", "builder", "王搭建", ["builder"]),
  ].map((user) => [user.user_id, { ...user, password: PASSWORD }]),
));
const sessions = (globalStore.aepMockSessions ??= new Map());
const audit = (globalStore.aepMockAudit ??= []);

function cookie(request: Request, name: string) {
  return request.headers
    .get("cookie")
    ?.split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`))
    ?.slice(name.length + 1);
}
const mode = (request: Request) => cookie(request, "aep_mock_auth");
const out = ({ password: _password, ...user }: User & { password: string }) => {
  void _password;
  return user;
};
function signedIn(request: Request) {
  const token = cookie(request, "aep_session");
  const user = token ? users.get(sessions.get(token) ?? "") : undefined;
  return user && !user.disabled ? user : undefined;
}
function withSession(user: User & { password: string }, status = 200) {
  const token = randomUUID();
  sessions.set(token, user.user_id);
  user.last_login_at = new Date().toISOString();
  const response = mockJson(out(user), status);
  response.headers.append(
    "Set-Cookie",
    `aep_session=${token}; Path=/; HttpOnly; SameSite=Lax`,
  );
  return response;
}
function record(
  action: string,
  label: string,
  user: User | undefined,
  status = 200,
) {
  audit.unshift({
    id: audit.length + 1,
    at: new Date().toISOString(),
    user: user
      ? {
          user_id: user.user_id,
          username: user.username,
          display_name: user.display_name,
        }
      : null,
    action,
    label,
    target: {},
    status,
    ip: "127.0.0.1",
  });
}
const orderedRoles = (roles: unknown): Role[] | null =>
  Array.isArray(roles) &&
  roles.length > 0 &&
  roles.every((role) => ROLES.includes(role as Role))
    ? ROLES.filter((role) => roles.includes(role))
    : null;
const enabledAdmins = (except?: string) =>
  [...users.values()].filter(
    (user) =>
      user.user_id !== except && user.roles.includes("admin") && !user.disabled,
  ).length;

export async function mockAuth(request: Request, path: string[]) {
  const on = mode(request);
  const user = signedIn(request);
  if (path[0] === "auth" && path[1] === "status")
    return mockJson(
      on
        ? {
            enabled: true,
            setup_needed: on === "setup",
            user: user ? out(user) : null,
          }
        : { enabled: false, setup_needed: false, user: LOCAL },
    );
  if (path[0] === "auth" && path[1] === "login") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const found = [...users.values()].find(
      (u) =>
        u.username ===
        String(body.username ?? "")
          .trim()
          .toLowerCase(),
    );
    if (!found || found.password !== body.password || found.disabled) {
      record("login_failed", "登录失败", undefined, 401);
      return mockError("账号或密码不对", 401);
    }
    record("login", "登录", found);
    return withSession(found);
  }
  if (path[0] === "auth" && path[1] === "setup") {
    if (on !== "setup") return mockError("已经有账号了，请直接登录", 409);
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const username = String(body.username ?? "");
    if (!USERNAME.test(username))
      return mockError("「登录名」的格式、长度或范围不正确", 422);
    const created = {
      ...seed(
        `u_${randomUUID().slice(0, 8)}`,
        username,
        String(body.display_name),
        ["admin"],
      ),
      password: String(body.password),
    };
    users.set(created.user_id, created);
    record("setup", "建第一个管理员", created, 201);
    return withSession(created, 201);
  }
  if (!on) {
    if (path[0] === "auth" && path[1] === "me") return mockJson(LOCAL);
    if (path[0] === "auth") return mockError("没有打开登录", 409);
  }
  if (on && !user) return mockError("请先登录", 401);
  if (path[0] === "auth" && path[1] === "logout") {
    const token = cookie(request, "aep_session");
    if (token) sessions.delete(token);
    record("logout", "退出", user, 204);
    const response = noContent();
    response.headers.append("Set-Cookie", "aep_session=; Path=/; Max-Age=0");
    return response;
  }
  if (path[0] === "auth" && path[1] === "me" && path.length === 2)
    return mockJson(user ? out(user) : LOCAL);
  if (path[0] === "auth" && path[2] === "password") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    if (user && body.old_password !== user.password)
      return mockError("原密码不对", 422);
    if (user) user.password = String(body.password);
    return noContent();
  }
  if (on && user && !user.roles.includes("admin"))
    return mockError("没有权限：这项操作需要管理员", 403);
  if (path[0] === "audit")
    return mockJson({ events: audit.slice(0, 50), next: null });
  // Accounts
  if (path.length === 1 && request.method === "GET")
    return mockJson(
      [...users.values()]
        .map(out)
        .sort((a, b) => a.username.localeCompare(b.username)),
    );
  if (path.length === 1) {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    const username = String(body.username ?? "");
    const roles = orderedRoles(body.roles);
    if (!USERNAME.test(username))
      return mockError("「登录名」的格式、长度或范围不正确", 422);
    if (!roles) return mockError("「角色」的格式、长度或范围不正确", 422);
    if ([...users.values()].some((u) => u.username === username))
      return mockError(`已经有登录名为「${username}」的账号了`, 409);
    const created = {
      ...seed(
        `u_${randomUUID().slice(0, 8)}`,
        username,
        String(body.display_name),
        roles,
      ),
      password: String(body.password),
    };
    users.set(created.user_id, created);
    record("POST /users", "新建账号", user, 201);
    return mockJson(out(created), 201);
  }
  const target = users.get(path[1]);
  if (!target) return mockError("没有找到这个账号", 404);
  if (path[2] === "password") {
    const body = await readBody(request);
    if (body instanceof Response) return body;
    target.password = String(body.password);
    for (const [token, id] of sessions)
      if (id === target.user_id) sessions.delete(token);
    record("PUT /users/{user_id}/password", "重设密码", user, 204);
    return noContent();
  }
  if (request.method === "DELETE") {
    if (target.user_id === user?.user_id)
      return mockError("不能删除自己的账号", 409);
    if (target.roles.includes("admin") && !enabledAdmins(target.user_id))
      return mockError("至少要留一个管理员", 409);
    users.delete(target.user_id);
    record("DELETE /users/{user_id}", "删除账号", user, 204);
    return noContent();
  }
  const body = await readBody(request);
  if (body instanceof Response) return body;
  const next = { ...target };
  if (typeof body.display_name === "string")
    next.display_name = body.display_name;
  if (body.roles !== undefined) {
    const roles = orderedRoles(body.roles);
    if (!roles) return mockError("「角色」的格式、长度或范围不正确", 422);
    next.roles = roles;
  }
  if (typeof body.disabled === "boolean") next.disabled = body.disabled;
  if (
    target.roles.includes("admin") &&
    !target.disabled &&
    (!next.roles.includes("admin") || next.disabled) &&
    !enabledAdmins(target.user_id)
  )
    return mockError("至少要留一个管理员", 409);
  users.set(target.user_id, next);
  record("PATCH /users/{user_id}", "修改账号", user);
  return mockJson(out(next));
}
