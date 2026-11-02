"""Sign-in, accounts and the record of changes. Contract: docs/api/auth.md."""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints, field_validator

from aep.api.validation import NoNulls, chinese_errors
from aep.auth import (
    COOKIE,
    LOCAL_USER,
    PASSWORD_MAX,
    PASSWORD_MIN,
    USERNAME,
    AuditRecord,
    AuthProblem,
    AuthService,
    client_ip,
    current_user,
)

router = APIRouter(
    tags=["auth"],
    route_class=chinese_errors(
        {
            "username": "登录名",
            "display_name": "显示名",
            "password": "密码",
            "old_password": "原密码",
            "roles": "角色",
            "disabled": "停用",
            "email": "邮箱",
        }
    ),
)

Username = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=40)]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50, pattern=r"^[^\x00]*$"),
]
Password = Annotated[str, StringConstraints(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)]
RoleName = Literal["user", "approver", "builder", "admin"]
# Loose on purpose: the mail server is the judge; "" (in a change) clears it.
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, max_length=200, pattern=r"^([^@\s]+@[^@\s]+\.[^@\s]+)?$"
    ),
]


class UserOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    roles: list[str]
    disabled: bool
    created_at: datetime
    last_login_at: datetime | None
    email: str | None = None


class Status(BaseModel):
    enabled: bool
    setup_needed: bool
    user: UserOut | None


def _valid_username(value: str) -> str:
    if not USERNAME.match(value):
        raise ValueError("登录名要以小写字母开头，只能有小写字母、数字、. _ 和 -，2–40 个字符")
    return value


class Setup(NoNulls):
    username: Username
    display_name: DisplayName
    password: Password

    _username = field_validator("username")(_valid_username)


class Login(NoNulls):
    username: Annotated[str, StringConstraints(max_length=100)]
    password: Annotated[str, StringConstraints(max_length=PASSWORD_MAX)]


class ChangePassword(NoNulls):
    old_password: Annotated[str, StringConstraints(max_length=PASSWORD_MAX)]
    password: Password


class CreateUser(NoNulls):
    username: Username
    display_name: DisplayName
    roles: list[RoleName] = Field(min_length=1, max_length=4)
    password: Password
    email: Email | None = None

    _username = field_validator("username")(_valid_username)


class UpdateUser(NoNulls):
    display_name: DisplayName | None = None
    roles: list[RoleName] | None = Field(None, min_length=1, max_length=4)
    disabled: bool | None = None
    email: Email | None = None


class ResetPassword(NoNulls):
    password: Password


def _auth(request: Request) -> AuthService:
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    if auth is None:
        raise HTTPException(503, "账号服务还没有准备好")
    return auth


def _out(user: Any) -> UserOut:
    return UserOut(**user.out())


def _problem(exc: AuthProblem) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


# Sign-in


@router.get("/auth/status")
async def auth_status(request: Request) -> Status:
    auth: AuthService | None = getattr(request.app.state, "auth", None)
    if auth is None or not auth.enabled:
        return Status(enabled=False, setup_needed=False, user=_out(LOCAL_USER))
    user = getattr(request.state, "user", None)
    return Status(
        enabled=True,
        setup_needed=await auth.store.count_users() == 0,
        user=_out(user) if user else None,
    )


@router.post("/auth/setup", status_code=status.HTTP_201_CREATED)
async def setup(body: Setup, request: Request, response: Response) -> UserOut:
    auth = _auth(request)
    try:
        user, token = await auth.first_admin(body.username, body.display_name, body.password)
    except AuthProblem as exc:
        raise _problem(exc) from exc
    auth.set_cookie(response, token)
    await auth.record(AuditRecord("setup", 201, user, {}, client_ip(request)))
    return _out(user)


@router.post("/auth/login")
async def login(body: Login, request: Request, response: Response) -> UserOut:
    auth = _auth(request)
    if not auth.enabled:
        raise HTTPException(409, "没有打开登录，不用登录")
    try:
        user, token = await auth.login(body.username, body.password)
    except AuthProblem as exc:
        await auth.record(
            AuditRecord(
                "login_failed",
                exc.status,
                None,
                {"username": body.username.strip()[:100]},
                client_ip(request),
            )
        )
        raise _problem(exc) from exc
    auth.set_cookie(response, token)
    await auth.record(AuditRecord("login", 200, user, {}, client_ip(request)))
    return _out(user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    auth = _auth(request)
    user = getattr(request.state, "user", None)
    await auth.logout(request.cookies.get(COOKIE))
    response = Response(status_code=204)
    auth.clear_cookie(response)
    if auth.enabled:
        await auth.record(AuditRecord("logout", 204, user, {}, client_ip(request)))
    return response


@router.get("/auth/me")
async def me(request: Request) -> UserOut:
    return _out(current_user(request))


@router.put("/auth/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: ChangePassword, request: Request) -> Response:
    auth = _auth(request)
    if not auth.enabled:
        raise HTTPException(409, "没有打开登录，没有密码可改")
    try:
        await auth.change_password(
            current_user(request), body.old_password, body.password, request.cookies.get(COOKIE)
        )
    except AuthProblem as exc:
        raise _problem(exc) from exc
    return Response(status_code=204)


# Accounts (admins)


@router.get("/users")
async def list_users(request: Request) -> list[UserOut]:
    return [_out(u) for u in await _auth(request).store.list_users()]


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUser, request: Request) -> UserOut:
    try:
        user = await _auth(request).create(
            body.username, body.display_name, list(body.roles), body.password, body.email or None
        )
    except AuthProblem as exc:
        raise _problem(exc) from exc
    return _out(user)


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UpdateUser, request: Request) -> UserOut:
    try:
        user = await _auth(request).update(
            user_id,
            display_name=body.display_name,
            roles=list(body.roles) if body.roles is not None else None,
            disabled=body.disabled,
            email=body.email,
        )
    except AuthProblem as exc:
        raise _problem(exc) from exc
    return _out(user)


@router.put("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(user_id: str, body: ResetPassword, request: Request) -> Response:
    try:
        await _auth(request).reset_password(user_id, body.password)
    except AuthProblem as exc:
        raise _problem(exc) from exc
    return Response(status_code=204)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, request: Request) -> Response:
    try:
        await _auth(request).delete(user_id, current_user(request))
    except AuthProblem as exc:
        raise _problem(exc) from exc
    return Response(status_code=204)


# The record of changes (admins)


@router.get("/audit")
async def audit(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[int | None, Query(ge=1)] = None,
    user: Annotated[str | None, Query(max_length=40)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    events = await _auth(request).store.events(limit + 1, before, user or None, q or None)
    more = len(events) > limit
    events = events[:limit]
    return {"events": events, "next": events[-1]["id"] if more and events else None}
