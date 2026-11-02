"""Sign-in, roles and the record of changes (docs/api/auth.md).

Checked once at the API's entrance (``authorize``, a dependency on every route, and
``audit_middleware``): apps, nodes and connectors know nothing of it. With
``AEP_AUTH_ENABLED`` off every request is the local user, who has every role.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import secrets
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from fastapi import HTTPException, Request, Response
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.config import Settings
from aep.runtime.store import DEFAULT_TENANT

log = logging.getLogger(__name__)

Role = Literal["user", "approver", "builder", "admin"]
ROLES: tuple[Role, ...] = ("user", "approver", "builder", "admin")
ROLE_LABELS: dict[str, str] = {
    "user": "使用者",
    "approver": "审批者",
    "builder": "搭建者",
    "admin": "管理员",
}
COOKIE = "aep_session"
USERNAME = re.compile(r"^[a-z][a-z0-9._-]{1,39}$")
PASSWORD_MIN, PASSWORD_MAX = 8, 128
AUDIT_DAYS = 365
# Failed sign-ins per username before it is locked for the window.
FAILURES, FAILURE_WINDOW = 5, timedelta(minutes=15)


@dataclass
class User:
    user_id: str
    username: str
    display_name: str
    roles: list[str]
    disabled: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_login_at: datetime | None = None
    # Where email notifications go (docs/api/notifications.md); None: no email.
    email: str | None = None

    def has(self, role: str) -> bool:
        """Whether this account may act as ``role``: everyone is a user, an admin is all."""
        return role == "user" or "admin" in self.roles or role in self.roles

    def out(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "roles": self.roles,
            "disabled": self.disabled,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
            "email": self.email,
        }


LOCAL_USER = User(
    "local", "local", "本地用户", ["admin"], created_at=datetime(2026, 1, 1, tzinfo=UTC)
)


def ordered(roles: list[str]) -> list[str]:
    return [role for role in ROLES if role in roles]


# Passwords: scrypt with a random salt, stored as scrypt$n$r$p$salt$hash.
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    encode = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${encode(salt).decode()}${encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        found = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
    except ValueError:
        return False
    return hmac.compare_digest(found, base64.b64decode(digest))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Labels for the record of changes, by method and route template.
LABELS: dict[str, str] = {
    "login": "登录",
    "login_failed": "登录失败",
    "logout": "退出",
    "setup": "建第一个管理员",
    "PUT /auth/me/password": "修改自己的密码",
    "POST /users": "新建账号",
    "PATCH /users/{user_id}": "修改账号",
    "PUT /users/{user_id}/password": "重设密码",
    "DELETE /users/{user_id}": "删除账号",
    "POST /runs": "发起运行",
    "POST /runs/{run_id}/resume": "处理待确认",
    "POST /runs/{run_id}/cancel": "取消运行",
    "POST /runs/{run_id}/retry": "重试运行",
    "POST /runs/{run_id}/rerun": "复制运行",
    "POST /datasources": "登记数据源",
    "POST /datasources/test": "测试连接器",
    "POST /datasources/files": "上传文件",
    "PATCH /datasources/{datasource_id}": "修改数据源",
    "POST /projects": "新建项目",
    "PUT /projects/{project_id}": "修改项目",
    "DELETE /projects/{project_id}": "删除项目",
    "POST /apps": "新建应用",
    "POST /apps/import": "导入应用",
    "PATCH /apps/{app}": "修改应用信息",
    "DELETE /apps/{app}": "删除应用",
    "PUT /apps/{app}/draft": "保存草稿",
    "POST /apps/{app}/draft/import": "导入草稿",
    "DELETE /apps/{app}/draft": "放弃草稿",
    "POST /apps/{app}/nodes/{node}/test": "测试节点",
    "PUT /apps/{app}/secrets/{name}": "设置密钥",
    "DELETE /apps/{app}/secrets/{name}": "清除密钥",
    "POST /apps/{app}/draft/eval": "评测草稿",
    "POST /apps/{app}/draft/publish": "发布应用",
    "POST /apps/{app}/versions/{version}/activate": "切换应用版本",
    "POST /skills": "新建 Skill",
    "PUT /skills/{name}/draft": "保存 Skill 草稿",
    "DELETE /skills/{name}/draft": "放弃 Skill 草稿",
    "POST /skills/{name}/publish": "发布 Skill",
    "POST /knowledge-bases": "新建知识库",
    "PATCH /knowledge-bases/{kb}": "修改知识库",
    "DELETE /knowledge-bases/{kb}": "删除知识库",
    "POST /knowledge-bases/{kb}/documents": "上传文档",
    "DELETE /knowledge-bases/{kb}/documents/{doc}": "删除文档",
    "POST /knowledge-bases/{kb}/documents/{doc}/reindex": "重建文档索引",
    "POST /knowledge-bases/{kb}/reindex": "重建知识库索引",
    "POST /knowledge-bases/{kb}/search": "检索测试",
    "POST /models": "添加模型",
    "PATCH /models/{alias}": "修改模型",
    "DELETE /models/{alias}": "删除模型",
    "POST /models/{alias}/test": "测试模型",
    "PUT /models/defaults": "设置默认模型",
    "POST /llm/chat": "试调模型",
    "POST /apps/{app}/keys": "新建 API 密钥",
    "PUT /apps/{app}/eval-set": "保存评测集",
    "POST /apps/{app}/eval-set/cases/from-run": "把运行存成评测用例",
    "PUT /runs/{run_id}/feedback": "评价运行",
    "DELETE /runs/{run_id}/feedback": "撤回评价",
    "POST /runs/{run_id}/rerun-from": "从某一步重跑",
    "DELETE /apps/{app}/keys/{key_id}": "删除 API 密钥",
    "POST /v1/apps/{app}/runs": "经 API 发起运行",
    "POST /v1/runs/{run_id}/answer": "经 API 填表单",
    "POST /webhooks": "新建 Webhook",
    "PATCH /webhooks/{webhook_id}": "修改 Webhook",
    "DELETE /webhooks/{webhook_id}": "删除 Webhook",
    "POST /webhooks/{webhook_id}/test": "测试 Webhook",
}


def label(action: str) -> str:
    return LABELS.get(action, action)


# Who may call what (docs/api/auth.md, 「权限怎么检查」): None is public.
WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC = frozenset(
    {
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/auth/status"),
        ("POST", "/auth/setup"),
        ("POST", "/auth/login"),
    }
)
BUILT = ("/apps", "/skills", "/knowledge-bases", "/datasources", "/projects")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def required(method: str, path: str) -> str | None:
    if (method, path) in PUBLIC:
        return None
    # The app API answers to API keys only (docs/api/app-access.md), checked there.
    if _under(path, "/v1"):
        return None
    if path.startswith("/apps/{app}/keys") or path == "/runs/{run_id}/rerun-from":
        return "builder"
    if any(_under(path, p) for p in ("/users", "/audit", "/webhooks", "/notification-channels")):
        return "admin"
    if _under(path, "/models"):
        return "admin" if method in WRITES else "builder"
    if path == "/llm/chat":
        return "builder"
    if (
        method in WRITES
        and path != "/datasources/files"
        and any(_under(path, prefix) for prefix in BUILT)
    ):
        return "builder"
    return "user"


@dataclass
class AuditRecord:
    action: str
    status: int
    user: User | None = None
    target: dict[str, Any] = field(default_factory=dict)
    ip: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


class DuplicateUser(Exception):
    pass


class AuthStore(Protocol):
    async def setup(self) -> None: ...
    async def count_users(self) -> int: ...
    async def list_users(self) -> list[User]: ...
    async def get_user(self, user_id: str) -> User | None: ...
    async def find_login(self, username: str) -> tuple[User, str] | None: ...
    async def create_user(self, user: User, password_hash: str) -> User: ...
    async def save_user(self, user: User) -> None: ...
    async def set_password(self, user_id: str, password_hash: str) -> None: ...
    async def delete_user(self, user_id: str) -> None: ...
    async def add_session(self, digest: str, user_id: str, expires_at: datetime) -> None: ...
    async def session_user(self, digest: str, now: datetime) -> User | None: ...
    async def drop_session(self, digest: str) -> None: ...
    async def drop_sessions(self, user_id: str, keep: str | None = None) -> None: ...
    async def prune(self, now: datetime, audit_before: datetime) -> None: ...
    async def record(self, event: AuditRecord) -> None: ...
    async def events(
        self, limit: int, before: int | None, username: str | None, q: str | None
    ) -> list[dict[str, Any]]: ...


def _event_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "at": row["at"],
        "user": (
            {
                "user_id": row["user_id"],
                "username": row["username"],
                "display_name": row["display_name"],
            }
            if row["user_id"]
            else None
        ),
        "action": row["action"],
        "label": row["label"],
        "target": row["target"],
        "status": row["status"],
        "ip": row["ip"],
    }


def _event_row(event: AuditRecord, event_id: int) -> dict[str, Any]:
    user = event.user
    return {
        "id": event_id,
        "at": event.at,
        "user_id": user.user_id if user and user is not LOCAL_USER else None,
        "username": user.username if user and user is not LOCAL_USER else None,
        "display_name": user.display_name if user and user is not LOCAL_USER else None,
        "action": event.action,
        "label": label(event.action),
        "target": event.target,
        "status": event.status,
        "ip": event.ip,
    }


class MemoryAuthStore:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.passwords: dict[str, str] = {}
        self.sessions: dict[str, tuple[str, datetime]] = {}
        self.audit: list[dict[str, Any]] = []

    async def setup(self) -> None:
        pass

    async def count_users(self) -> int:
        return len(self.users)

    async def list_users(self) -> list[User]:
        return sorted((replace(u) for u in self.users.values()), key=lambda u: u.username)

    async def get_user(self, user_id: str) -> User | None:
        user = self.users.get(user_id)
        return replace(user, roles=list(user.roles)) if user else None

    async def find_login(self, username: str) -> tuple[User, str] | None:
        user = next((u for u in self.users.values() if u.username == username), None)
        return (replace(user), self.passwords[user.user_id]) if user else None

    async def create_user(self, user: User, password_hash: str) -> User:
        if any(u.username == user.username for u in self.users.values()):
            raise DuplicateUser(user.username)
        self.users[user.user_id] = replace(user, roles=list(user.roles))
        self.passwords[user.user_id] = password_hash
        return user

    async def save_user(self, user: User) -> None:
        self.users[user.user_id] = replace(user, roles=list(user.roles))

    async def set_password(self, user_id: str, password_hash: str) -> None:
        self.passwords[user_id] = password_hash

    async def delete_user(self, user_id: str) -> None:
        self.users.pop(user_id, None)
        self.passwords.pop(user_id, None)
        await self.drop_sessions(user_id)

    async def add_session(self, digest: str, user_id: str, expires_at: datetime) -> None:
        self.sessions[digest] = (user_id, expires_at)

    async def session_user(self, digest: str, now: datetime) -> User | None:
        found = self.sessions.get(digest)
        if found is None or found[1] <= now:
            return None
        user = await self.get_user(found[0])
        return user if user and not user.disabled else None

    async def drop_session(self, digest: str) -> None:
        self.sessions.pop(digest, None)

    async def drop_sessions(self, user_id: str, keep: str | None = None) -> None:
        self.sessions = {d: s for d, s in self.sessions.items() if s[0] != user_id or d == keep}

    async def prune(self, now: datetime, audit_before: datetime) -> None:
        self.sessions = {d: s for d, s in self.sessions.items() if s[1] > now}
        self.audit = [e for e in self.audit if e["at"] >= audit_before]

    async def record(self, event: AuditRecord) -> None:
        self.audit.append(_event_row(event, len(self.audit) + 1))

    async def events(
        self, limit: int, before: int | None, username: str | None, q: str | None
    ) -> list[dict[str, Any]]:
        rows = [
            e
            for e in reversed(self.audit)
            if (before is None or e["id"] < before)
            and (username is None or e["username"] == username)
            and (not q or q.lower() in e["action"].lower() or q in e["label"])
        ]
        return [_event_out(e) for e in rows[:limit]]


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS users (
    user_id        text PRIMARY KEY,
    tenant_id      text NOT NULL DEFAULT 'default',
    username       text NOT NULL,
    display_name   text NOT NULL,
    roles          text[] NOT NULL,
    password_hash  text NOT NULL,
    disabled       boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_login_at  timestamptz
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_username ON users (tenant_id, username)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email text",
    """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  text PRIMARY KEY,
    tenant_id   text NOT NULL DEFAULT 'default',
    user_id     text NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id)",
    """
CREATE TABLE IF NOT EXISTS audit_events (
    id            bigserial PRIMARY KEY,
    tenant_id     text NOT NULL DEFAULT 'default',
    at            timestamptz NOT NULL DEFAULT now(),
    user_id       text,
    username      text,
    display_name  text,
    action        text NOT NULL,
    label         text NOT NULL,
    target        jsonb NOT NULL DEFAULT '{}',
    status        integer NOT NULL,
    ip            text
)""",
    "CREATE INDEX IF NOT EXISTS audit_events_at ON audit_events (tenant_id, id DESC)",
]
_USER_COLUMNS = "user_id, username, display_name, roles, disabled, created_at, last_login_at, email"


def _user(row: dict[str, Any]) -> User:
    return User(
        row["user_id"],
        row["username"],
        row["display_name"],
        ordered(list(row["roles"])),
        row["disabled"],
        row["created_at"],
        row["last_login_at"],
        row["email"],
    )


class PostgresAuthStore:
    """Accounts, sign-in sessions and the record of changes (dict rows, autocommit)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def count_users(self) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM users WHERE tenant_id = %s", (DEFAULT_TENANT,)
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def list_users(self) -> list[User]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE tenant_id = %s ORDER BY username",
                (DEFAULT_TENANT,),
            )
            return [_user(row) for row in await cur.fetchall()]

    async def get_user(self, user_id: str) -> User | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE tenant_id = %s AND user_id = %s",
                (DEFAULT_TENANT, user_id),
            )
            row = await cur.fetchone()
            return _user(row) if row else None

    async def find_login(self, username: str) -> tuple[User, str] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_USER_COLUMNS}, password_hash FROM users"
                " WHERE tenant_id = %s AND username = %s",
                (DEFAULT_TENANT, username),
            )
            row = await cur.fetchone()
            return (_user(row), row["password_hash"]) if row else None

    async def create_user(self, user: User, password_hash: str) -> User:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO users (user_id, tenant_id, username, display_name, roles,"
                " password_hash, disabled, created_at, email)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, username) DO NOTHING RETURNING user_id",
                (
                    user.user_id,
                    DEFAULT_TENANT,
                    user.username,
                    user.display_name,
                    user.roles,
                    password_hash,
                    user.disabled,
                    user.created_at,
                    user.email,
                ),
            )
            if await cur.fetchone() is None:
                raise DuplicateUser(user.username)
        return user

    async def save_user(self, user: User) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE users SET display_name = %s, roles = %s, disabled = %s,"
                " last_login_at = %s, email = %s WHERE tenant_id = %s AND user_id = %s",
                (
                    user.display_name,
                    user.roles,
                    user.disabled,
                    user.last_login_at,
                    user.email,
                    DEFAULT_TENANT,
                    user.user_id,
                ),
            )

    async def set_password(self, user_id: str, password_hash: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE users SET password_hash = %s WHERE tenant_id = %s AND user_id = %s",
                (password_hash, DEFAULT_TENANT, user_id),
            )

    async def delete_user(self, user_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM users WHERE tenant_id = %s AND user_id = %s",
                (DEFAULT_TENANT, user_id),
            )

    async def add_session(self, digest: str, user_id: str, expires_at: datetime) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO sessions (token_hash, tenant_id, user_id, expires_at)"
                " VALUES (%s, %s, %s, %s)",
                (digest, DEFAULT_TENANT, user_id, expires_at),
            )

    async def session_user(self, digest: str, now: datetime) -> User | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {', '.join('u.' + c for c in _USER_COLUMNS.split(', '))}"
                " FROM sessions s JOIN users u ON u.user_id = s.user_id"
                " WHERE s.tenant_id = %s AND s.token_hash = %s AND s.expires_at > %s"
                " AND NOT u.disabled",
                (DEFAULT_TENANT, digest, now),
            )
            row = await cur.fetchone()
            return _user(row) if row else None

    async def drop_session(self, digest: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE tenant_id = %s AND token_hash = %s",
                (DEFAULT_TENANT, digest),
            )

    async def drop_sessions(self, user_id: str, keep: str | None = None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE tenant_id = %s AND user_id = %s"
                " AND token_hash IS DISTINCT FROM %s",
                (DEFAULT_TENANT, user_id, keep),
            )

    async def prune(self, now: datetime, audit_before: datetime) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE tenant_id = %s AND expires_at <= %s",
                (DEFAULT_TENANT, now),
            )
            await conn.execute(
                "DELETE FROM audit_events WHERE tenant_id = %s AND at < %s",
                (DEFAULT_TENANT, audit_before),
            )

    async def record(self, event: AuditRecord) -> None:
        row = _event_row(event, 0)
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO audit_events (tenant_id, at, user_id, username, display_name,"
                " action, label, target, status, ip)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    DEFAULT_TENANT,
                    row["at"],
                    row["user_id"],
                    row["username"],
                    row["display_name"],
                    row["action"],
                    row["label"],
                    Jsonb(row["target"]),
                    row["status"],
                    row["ip"],
                ),
            )

    async def events(
        self, limit: int, before: int | None, username: str | None, q: str | None
    ) -> list[dict[str, Any]]:
        where = ["tenant_id = %s"]
        params: list[Any] = [DEFAULT_TENANT]
        if before is not None:
            where.append("id < %s")
            params.append(before)
        if username:
            where.append("username = %s")
            params.append(username)
        if q:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(action ILIKE %s OR label LIKE %s)")
            params += [pattern, pattern]
        params.append(limit)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, at, user_id, username, display_name, action, label, target,"
                f" status, ip FROM audit_events WHERE {' AND '.join(where)}"
                " ORDER BY id DESC LIMIT %s",
                params,
            )
            return [_event_out(row) for row in await cur.fetchall()]


class AuthProblem(Exception):
    """A request the API answers with ``status`` and the Chinese ``detail``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class AuthService:
    def __init__(
        self,
        store: AuthStore,
        *,
        enabled: bool,
        session_hours: int = 168,
        cookie_secure: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.session_hours = session_hours
        self.cookie_secure = cookie_secure
        self.now = now
        self._failures: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, store: AuthStore, settings: Settings) -> "AuthService":
        return cls(
            store,
            enabled=settings.auth_enabled,
            session_hours=settings.session_hours,
            cookie_secure=settings.cookie_secure,
        )

    async def setup(self) -> None:
        await self.store.setup()
        now = self.now()
        await self.store.prune(now, now - timedelta(days=AUDIT_DAYS))

    # Sessions

    async def user_for(self, token: str | None) -> User | None:
        if not token:
            return None
        return await self.store.session_user(token_hash(token), self.now())

    async def _open(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        expires = self.now() + timedelta(hours=self.session_hours)
        await self.store.add_session(token_hash(token), user.user_id, expires)
        user.last_login_at = self.now()
        await self.store.save_user(user)
        return token

    def set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            COOKIE,
            token,
            max_age=self.session_hours * 3600,
            httponly=True,
            samesite="lax",
            secure=self.cookie_secure,
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(
            COOKIE, httponly=True, samesite="lax", secure=self.cookie_secure, path="/"
        )

    async def login(self, username: str, password: str) -> tuple[User, str]:
        key = username.strip().lower()
        failures = self._failures[key]
        cutoff = self.now() - FAILURE_WINDOW
        while failures and failures[0] < cutoff:
            failures.popleft()
        if len(failures) >= FAILURES:
            raise AuthProblem(429, "登录失败次数太多，请 15 分钟后再试")
        found = await self.store.find_login(key)
        ok = found is not None and await asyncio.to_thread(verify_password, password, found[1])
        if not ok or found is None or found[0].disabled:
            failures.append(self.now())
            raise AuthProblem(401, "账号或密码不对")
        failures.clear()
        return found[0], await self._open(found[0])

    async def logout(self, token: str | None) -> None:
        if token:
            await self.store.drop_session(token_hash(token))

    async def first_admin(
        self, username: str, display_name: str, password: str
    ) -> tuple[User, str]:
        if not self.enabled:
            raise AuthProblem(409, "没有打开登录，不用建账号")
        async with self._lock:
            if await self.store.count_users():
                raise AuthProblem(409, "已经有账号了，请直接登录")
            user = await self.create(username, display_name, ["admin"], password)
        return user, await self._open(user)

    # Accounts

    async def create(
        self,
        username: str,
        display_name: str,
        roles: list[str],
        password: str,
        email: str | None = None,
    ) -> User:
        user = User(
            "u_" + secrets.token_hex(8), username, display_name, ordered(roles), email=email
        )
        digest = await asyncio.to_thread(hash_password, password)
        try:
            return await self.store.create_user(user, digest)
        except DuplicateUser as exc:
            raise AuthProblem(409, f"已经有登录名为「{username}」的账号了") from exc

    async def _get(self, user_id: str) -> User:
        user = await self.store.get_user(user_id)
        if user is None:
            raise AuthProblem(404, "没有找到这个账号")
        return user

    async def _keeps_admin(self, before: User, after: User | None) -> None:
        """409 when the change takes away the last enabled admin."""
        if "admin" not in before.roles or before.disabled:
            return
        if after is not None and "admin" in after.roles and not after.disabled:
            return
        others = [u for u in await self.store.list_users() if u.user_id != before.user_id]
        if not any("admin" in u.roles and not u.disabled for u in others):
            raise AuthProblem(409, "至少要留一个管理员")

    async def update(
        self,
        user_id: str,
        display_name: str | None = None,
        roles: list[str] | None = None,
        disabled: bool | None = None,
        email: str | None = None,
    ) -> User:
        """Change what is given; ``email`` "" clears it."""
        async with self._lock:
            user = await self._get(user_id)
            before = replace(user, roles=list(user.roles))
            if display_name is not None:
                user.display_name = display_name
            if roles is not None:
                user.roles = ordered(roles)
            if disabled is not None:
                user.disabled = disabled
            if email is not None:
                user.email = email or None
            await self._keeps_admin(before, user)
            await self.store.save_user(user)
        if user.disabled:
            await self.store.drop_sessions(user_id)
        return user

    async def reset_password(self, user_id: str, password: str) -> None:
        await self._get(user_id)
        await self.store.set_password(user_id, await asyncio.to_thread(hash_password, password))
        await self.store.drop_sessions(user_id)

    async def change_password(self, user: User, old: str, new: str, token: str | None) -> None:
        found = await self.store.find_login(user.username)
        if found is None or not await asyncio.to_thread(verify_password, old, found[1]):
            raise AuthProblem(422, "原密码不对")
        await self.store.set_password(user.user_id, await asyncio.to_thread(hash_password, new))
        await self.store.drop_sessions(user.user_id, keep=token_hash(token) if token else None)

    async def delete(self, user_id: str, by: User) -> None:
        if user_id == by.user_id:
            raise AuthProblem(409, "不能删除自己的账号")
        async with self._lock:
            await self._keeps_admin(await self._get(user_id), None)
            await self.store.delete_user(user_id)

    # The record of changes

    async def record(self, event: AuditRecord) -> None:
        try:
            await self.store.record(event)
        except Exception:  # noqa: BLE001 - the record never fails the request itself
            log.exception("could not record %s", event.action)


# FastAPI hooks


def _service(request: Request) -> AuthService | None:
    return getattr(request.app.state, "auth", None)


def enabled(request: Request) -> bool:
    auth = _service(request)
    return bool(auth and auth.enabled)


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is not None:
        return user
    if enabled(request):
        raise HTTPException(401, "请先登录")
    return LOCAL_USER


def who(request: Request, given: str | None) -> str | None:
    """The name to record for this request: the signed-in user's when sign-in is on,
    otherwise the name the client gave (as before sign-in existed)."""
    if enabled(request):
        return current_user(request).display_name
    return (given or "").strip() or None


def client_ip(request: Request) -> str | None:
    # The last address is the one the platform's own proxy saw; earlier ones are
    # whatever the client claimed.
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
    last = next((part for part in reversed(forwarded) if part), None)
    return last or (request.client.host if request.client else None)


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


async def authorize(request: Request) -> None:
    """Every route's first dependency: who is asking, and may they."""
    auth = _service(request)
    if auth is None or not auth.enabled:
        request.state.user = LOCAL_USER
        return
    need = required(request.method, _route_path(request))
    user = await auth.user_for(request.cookies.get(COOKIE))
    request.state.user = user
    if need is None:
        return
    if user is None:
        raise HTTPException(401, "请先登录")
    if not user.has(need):
        raise HTTPException(403, f"没有权限：这项操作需要{ROLE_LABELS[need]}")


def require(request: Request, role: str, message: str) -> None:
    """A check that depends on more than the route (e.g. what a run waits for)."""
    if enabled(request) and not current_user(request).has(role):
        raise HTTPException(403, message)


# Recorded on their own, with what was tried.
_SELF_RECORDED = frozenset({"/auth/login", "/auth/setup", "/auth/logout"})
# Not changes worth a record: marking one's own notifications read.
_UNRECORDED = frozenset({"/notifications/read"})


async def audit_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    auth = _service(request)
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    skipped = path is None or path in _SELF_RECORDED or path in _UNRECORDED
    if auth is None or request.method not in WRITES or skipped:
        return response
    user = getattr(request.state, "user", None)
    target = {k: str(v) for k, v in (request.scope.get("path_params") or {}).items()}
    key = getattr(request.state, "api_key", None)
    if key is not None:
        # Calls with an API key are nobody's; the key's name says whose system it was.
        user, target["key"] = None, key.name
    await auth.record(
        AuditRecord(
            f"{request.method} {path}", response.status_code, user, target, client_ip(request)
        )
    )
    return response
