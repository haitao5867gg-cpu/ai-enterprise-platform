"""The REST calls each host gets, against fake hosts."""

import json
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from aep_git.hosts import Bitbucket, GitError, GitHub
from tests.conftest import settings


class Recorder:
    def __init__(self, answers: dict[tuple[str, str], tuple[int, Any]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, Any, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, dict(request.url.params)))
        assert request.headers["authorization"] == "Bearer token-for-tests"
        status, data = self.answers[(request.method, request.url.path)]
        return httpx.Response(status, json=data)


def gh_pr(number: int, state: str = "open", merged: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/etl/pull/{number}",
        "state": state,
        "merged_at": "2026-11-02T00:00:00Z" if merged else None,
        "title": "t",
        "head": {"ref": "aep/x"},
        "base": {"ref": "main"},
    }


async def test_github(tmp_path: Path):
    fake = Recorder(
        {
            ("GET", "/repos/acme/etl"): (
                200,
                {"default_branch": "trunk", "html_url": "https://github.com/acme/etl"},
            ),
            ("GET", "/repos/acme/etl/pulls"): (200, []),
            ("POST", "/repos/acme/etl/pulls"): (201, gh_pr(7)),
            ("PATCH", "/repos/acme/etl/pulls/7"): (200, gh_pr(7)),
            ("GET", "/repos/acme/etl/pulls/7"): (200, gh_pr(7, "closed", merged=True)),
            ("POST", "/repos/acme/etl/issues/7/comments"): (201, {}),
        }
    )
    host = GitHub(settings(tmp_path, tmp_path), httpx.MockTransport(fake))
    assert (await host.repo("acme/etl")).default_branch == "trunk"
    assert await host.find_open("acme/etl", "aep/x") is None
    assert fake.calls[-1][3] == {"head": "acme:aep/x", "state": "open"}
    made = await host.create("acme/etl", "aep/x", "trunk", "t", "b")
    assert made.number == 7 and made.state == "open"
    assert fake.calls[-1][2] == {"title": "t", "head": "aep/x", "base": "trunk", "body": "b"}
    await host.update("acme/etl", 7, "t2", "b2")
    assert fake.calls[-1][2] == {"title": "t2", "body": "b2"}
    assert (await host.get("acme/etl", 7)).state == "merged"
    await host.comment("acme/etl", 7, "已上线")
    assert fake.calls[-1][2] == {"body": "已上线"}
    await host.close()


def bb_pr(number: int, state: str = "OPEN") -> dict[str, Any]:
    return {
        "id": number,
        "version": 3,
        "state": state,
        "title": "t",
        "fromRef": {"displayId": "aep/x"},
        "toRef": {"displayId": "master"},
        "links": {
            "self": [{"href": f"https://bb.co/projects/DATA/repos/etl/pull-requests/{number}"}]
        },
    }


async def test_bitbucket_data_center(tmp_path: Path):
    repo = "/rest/api/1.0/projects/DATA/repos/etl"
    fake = Recorder(
        {
            ("GET", repo): (
                200,
                {"links": {"self": [{"href": "https://bb.co/projects/DATA/repos/etl/browse"}]}},
            ),
            ("GET", repo + "/default-branch"): (200, {"displayId": "master"}),
            ("GET", repo + "/pull-requests"): (200, {"values": [bb_pr(5)]}),
            ("POST", repo + "/pull-requests"): (201, bb_pr(6)),
            ("GET", repo + "/pull-requests/5"): (200, bb_pr(5, "DECLINED")),
            ("PUT", repo + "/pull-requests/5"): (200, bb_pr(5)),
            ("POST", repo + "/pull-requests/5/comments"): (201, {}),
        }
    )
    host = Bitbucket(
        settings(tmp_path, tmp_path, git_provider="bitbucket", git_api_url="https://bb.co"),
        httpx.MockTransport(fake),
    )
    info = await host.repo("DATA/etl")
    assert info.default_branch == "master"
    assert info.url == "https://bb.co/projects/DATA/repos/etl/browse"
    found = await host.find_open("DATA/etl", "aep/x")
    assert found is not None and found.number == 5 and found.branch == "aep/x"
    assert fake.calls[-1][3] == {
        "state": "OPEN",
        "direction": "OUTGOING",
        "at": "refs/heads/aep/x",
    }
    await host.create("DATA/etl", "aep/x", "master", "t", "b")
    assert fake.calls[-1][2] == {
        "title": "t",
        "description": "b",
        "fromRef": {"id": "refs/heads/aep/x"},
        "toRef": {"id": "refs/heads/master"},
    }
    await host.update("DATA/etl", 5, "t2", "b2")
    assert fake.calls[-1][2] == {"version": 3, "title": "t2", "description": "b2"}
    assert (await host.get("DATA/etl", 5)).state == "closed"
    await host.comment("DATA/etl", 5, "已上线")
    assert fake.calls[-1][2] == {"text": "已上线"}


async def test_host_errors_say_what_happened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = Recorder(
        {
            ("GET", "/repos/acme/etl"): (403, {"message": "Resource not accessible"}),
            ("GET", "/repos/acme/etl/pulls/1"): (
                422,
                {"message": "Validation Failed", "errors": [{"message": "bad"}]},
            ),
        }
    )
    host = GitHub(settings(tmp_path, tmp_path), httpx.MockTransport(fake))
    with pytest.raises(GitError, match="GitHub 拒绝了访问（403），请检查令牌的权限"):
        await host.repo("acme/etl")
    with pytest.raises(GitError, match="GitHub 返回 422.*Validation Failed"):
        await host.get("acme/etl", 1)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("aep_git.hosts.RETRY_SECONDS", 0)
    offline = GitHub(settings(tmp_path, tmp_path), httpx.MockTransport(down))
    with pytest.raises(GitError, match="连不上 GitHub"):
        await offline.repo("acme/etl")
