"""A stand-in GitHub repository: the REST endpoints ``aep.assets.AssetRepo`` calls.

Trees are kept whole (path → content) under their sha; commits, branches, pull
requests and their comments live in plain dicts. ``merge`` fast-forwards the base
branch to a PR's head, as a person merging it would. ``fail`` makes the next
request to a path suffix answer with an error. Every request must carry the token.
"""

import hashlib
import json
import re
from typing import Any

import httpx

from aep.assets import AssetRepo
from aep.config import Settings

API = "https://api.github.test"
REPO = "acme/aep-assets"
TOKEN = "ghp-test"


def _sha(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()


class FakeGitHub:
    def __init__(self, empty: bool = False) -> None:
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.refs: dict[str, str] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[str]] = {}
        self.failures: dict[str, tuple[int, str]] = {}
        self.requests: list[tuple[str, str]] = []
        if not empty:
            self.refs["main"] = self._commit({"README.md": "# assets\n"}, "init", [])
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def settings(self, **changes: Any) -> Settings:
        values = {"assets_repo": REPO, "assets_github_token": TOKEN, "assets_api_url": API}
        return Settings(**values | changes)

    def repo(self, **changes: Any) -> AssetRepo:
        return AssetRepo(self.settings(**changes), http=self.http)

    def files(self, branch: str) -> dict[str, str]:
        return self.trees[self.commits[self.refs[branch]]["tree"]]

    def merge(self, number: int) -> None:
        pr = self.pulls[number]
        self.refs[pr["base"]] = self.refs[pr["head"]]
        pr["state"] = "closed"

    def fail(
        self, suffix: str, status: int = 403, message: str = "Resource not accessible"
    ) -> None:
        self.failures[suffix] = (status, message)

    def _commit(self, files: dict[str, str], message: str, parents: list[str]) -> str:
        tree = _sha(files)
        self.trees[tree] = files
        sha = _sha([tree, message, parents])
        self.commits[sha] = {"tree": tree, "message": message, "parents": parents}
        return sha

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        prefix = f"/repos/{REPO}"
        assert request.url.path.startswith(prefix), request.url.path
        path = request.url.path[len(prefix) :]
        method = request.method
        self.requests.append((method, path))
        for suffix, (status, message) in list(self.failures.items()):
            if path.endswith(suffix):
                del self.failures[suffix]
                return httpx.Response(status, json={"message": message})
        body = json.loads(request.content) if request.content else {}

        if method == "GET" and (m := re.fullmatch(r"/git/ref/heads/(.+)", path)):
            if m[1] not in self.refs:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.refs[m[1]]}})
        if method == "GET" and (m := re.fullmatch(r"/git/commits/(\w+)", path)):
            return httpx.Response(200, json={"tree": {"sha": self.commits[m[1]]["tree"]}})
        if method == "GET" and (m := re.fullmatch(r"/git/trees/(\w+)", path)):
            assert request.url.params.get("recursive") == "1"
            items = [{"path": p, "type": "blob"} for p in sorted(self.trees[m[1]])]
            return httpx.Response(200, json={"tree": items, "truncated": False})
        if method == "POST" and path == "/git/trees":
            files = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if entry.get("sha", "") is None:
                    files.pop(entry["path"], None)
                else:
                    files[entry["path"]] = entry["content"]
            sha = _sha(files)
            self.trees[sha] = files
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path == "/git/commits":
            files = self.trees[body["tree"]]
            sha = self._commit(files, body["message"], body["parents"])
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and path == "/git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[branch] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if method == "PATCH" and (m := re.fullmatch(r"/git/refs/heads/(.+)", path)):
            self.refs[m[1]] = body["sha"]
            return httpx.Response(200, json={})
        if method == "GET" and path == "/pulls":
            head = request.url.params["head"].split(":", 1)[1]
            state = request.url.params.get("state", "open")
            found = [p for p in self.pulls.values() if p["head"] == head and p["state"] == state]
            return httpx.Response(200, json=found)
        if method == "POST" and path == "/pulls":
            number = len(self.pulls) + 1
            self.pulls[number] = {
                "number": number,
                "html_url": f"https://github.test/{REPO}/pull/{number}",
                "state": "open",
                **{k: body[k] for k in ("title", "head", "base", "body")},
            }
            return httpx.Response(201, json=self.pulls[number])
        if method == "PATCH" and (m := re.fullmatch(r"/pulls/(\d+)", path)):
            pr = self.pulls[int(m[1])]
            pr.update(body)
            return httpx.Response(200, json=pr)
        if method == "POST" and (m := re.fullmatch(r"/issues/(\d+)/comments", path)):
            self.comments.setdefault(int(m[1]), []).append(body["body"])
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={"message": f"no fake for {method} {path}"})
