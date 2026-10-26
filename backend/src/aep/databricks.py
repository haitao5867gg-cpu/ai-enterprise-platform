"""Deploys a generated bundle to the Databricks dev target, runs it and reads the outcome.

The Databricks CLI does the bundle work (``bundle validate``, ``bundle deploy -t
dev``, ``bundle run -t dev``) in a scratch directory holding only the bundle's
files. The generated code itself runs on Databricks, never here. After the run,
the pipeline's event log (REST API) says what each table wrote, what its quality
rules caught and, when the update failed, why.

With ``AEP_DATABRICKS_CLUSTER_ID`` set, nothing is deployed: the bundle's
transformations run on that existing cluster instead (``aep.cluster_harness``),
for workspaces where pipelines cannot start their own clusters. That checks the
generated code and its quality rules, not the pipeline configuration.

Off unless ``AEP_DATABRICKS_HOST`` and ``AEP_DATABRICKS_TOKEN`` are set.
"""

import asyncio
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from aep import cluster_harness
from aep.config import Settings
from aep.etl.codegen import PIPELINE_PY, PIPELINE_SQL, Bundle, CodeError, parse_sql

TARGET = "dev"
OUTPUT_TAIL = 4000
EVENT_PAGE = 250
# Connections to the workspace can drop mid-request (seen through egress proxies
# to Azure China); requests are retried this many times before giving up.
REQUEST_ATTEMPTS = 4
POLL_SECONDS = 3.0
CLUSTER_POLL_SECONDS = 15.0


class DatabricksError(Exception):
    """A call to the workspace failed (auth, permissions, network)."""


@dataclass
class Step:
    name: str
    ok: bool
    output: str


@dataclass
class TableResult:
    name: str
    status: str
    rows: int | None = None
    dropped: int = 0
    expectations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DevRun:
    """One validate → deploy → run of a bundle on the dev target.

    ``stage`` is the step that failed (or ``run`` when everything passed).
    ``fixable`` means the pipeline itself failed, so its ``errors`` are worth
    handing back to code generation; a failed validate or deploy is about the
    workspace or the configuration, not the generated code. In ``cluster`` mode
    there is only the ``run`` stage, on ``cluster_id``.
    """

    status: Literal["passed", "failed"]
    stage: Literal["validate", "deploy", "run"]
    steps: list[Step]
    errors: list[str] = field(default_factory=list)
    fixable: bool = False
    pipeline_id: str | None = None
    update_id: str | None = None
    url: str | None = None
    tables: list[TableResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mode: Literal["pipeline", "cluster"] = "pipeline"
    cluster_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_TAIL else "…" + text[-OUTPUT_TAIL:]


def _error_lines(event: dict[str, Any]) -> list[str]:
    error = event.get("error") or {}
    lines = [
        f"{e.get('class_name', '')}: {e.get('message', '')}".strip(": ")
        for e in error.get("exceptions") or []
        if e.get("message") or e.get("class_name")
    ]
    if not lines and event.get("message"):
        lines = [event["message"]]
    flow = event.get("origin", {}).get("flow_name")
    return [f"{flow}: {line}" if flow and not line.startswith(flow) else line for line in lines]


def summarize_events(
    events: list[dict[str, Any]], update_id: str
) -> tuple[list[TableResult], list[str]]:
    """Per-table outcome and error messages of one pipeline update, from its event log."""
    tables: dict[str, TableResult] = {}
    errors: list[str] = []
    # The API lists newest first; replay in order so the last status wins.
    for event in sorted(events, key=lambda e: e.get("timestamp", "")):
        origin = event.get("origin") or {}
        if origin.get("update_id") != update_id:
            continue
        if event.get("level") == "ERROR":
            errors += _error_lines(event)
        progress = (event.get("details") or {}).get("flow_progress")
        name = origin.get("flow_name") or origin.get("dataset_name")
        if progress is None or not name:
            continue
        table = tables.setdefault(name, TableResult(name, progress.get("status", "")))
        table.status = progress.get("status", table.status)
        rows = (progress.get("metrics") or {}).get("num_output_rows")
        if rows is not None:
            table.rows = int(rows)
        quality = progress.get("data_quality") or {}
        if quality:
            table.dropped = int(quality.get("dropped_records") or 0)
            table.expectations = [
                {
                    "name": e.get("name"),
                    "passed": int(e.get("passed_records") or 0),
                    "failed": int(e.get("failed_records") or 0),
                }
                for e in quality.get("expectations") or []
            ]
    return list(tables.values()), list(dict.fromkeys(errors))


class DevTarget:
    """The dev target of one Databricks workspace."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._host = settings.databricks_host.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=60)

    @classmethod
    def from_settings(cls, settings: Settings) -> "DevTarget | None":
        if settings.databricks_host and settings.databricks_token:
            return cls(settings)
        return None

    async def close(self) -> None:
        await self._http.aclose()

    def _variables(self) -> list[str]:
        values = {
            "catalog": self._settings.databricks_catalog,
            "schema": self._settings.databricks_schema,
            "source_catalog": self._settings.databricks_source_catalog,
        }
        return [f"--var={k}={v}" for k, v in values.items() if v]

    def _env(self, home: Path) -> dict[str, str]:
        # Nothing of the platform's own configuration reaches the CLI, only the
        # workspace it deploys to (and the bundled Terraform's location).
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("AEP_", "LANGFUSE_", "DATABRICKS_"))
            or k.startswith("DATABRICKS_TF_")
        }
        env.update(
            HOME=str(home),
            DATABRICKS_HOST=self._host,
            DATABRICKS_TOKEN=self._settings.databricks_token,
            DATABRICKS_AUTH_TYPE="pat",
            DATABRICKS_CONFIG_FILE=str(home / ".databrickscfg"),
        )
        return env

    async def _cli(self, cwd: Path, env: dict[str, str], *args: str) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._settings.databricks_cli,
                *args,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return False, f"无法启动 Databricks CLI（{self._settings.databricks_cli}）：{exc}"
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), self._settings.databricks_timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            limit = self._settings.databricks_timeout_seconds
            return False, f"databricks {' '.join(args[:2])} 超过 {limit:g} 秒没有结束"
        return proc.returncode == 0, out.decode(errors="replace")

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, **params: Any
    ) -> dict[str, Any]:
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            try:
                resp = await self._http.request(
                    method,
                    f"{self._host}{path}",
                    params=params or None,
                    json=body,
                    headers={"Authorization": f"Bearer {self._settings.databricks_token}"},
                )
                break
            except httpx.TransportError as exc:
                if attempt == REQUEST_ATTEMPTS:
                    raise DatabricksError(f"{path}: {exc!r}") from exc
                await asyncio.sleep(attempt)
            except httpx.HTTPError as exc:
                raise DatabricksError(f"{path}: {exc!r}") from exc
        if resp.status_code != 200:
            raise DatabricksError(f"{path}: HTTP {resp.status_code} {resp.text[:500]}")
        return resp.json()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._request("GET", path, **params)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", path, body)

    async def _update_outcome(self, pipeline_id: str) -> tuple[str | None, list[dict[str, Any]]]:
        """The latest update of the pipeline and the event log entries around it."""
        pipeline = await self._get(f"/api/2.0/pipelines/{pipeline_id}")
        updates = pipeline.get("latest_updates") or []
        if not updates:
            return None, []
        events = await self._get(f"/api/2.0/pipelines/{pipeline_id}/events", max_results=EVENT_PAGE)
        return updates[0].get("update_id"), events.get("events") or []

    async def run(self, bundle: Bundle) -> DevRun:
        if self._settings.databricks_cluster_id:
            return await self._run_on_cluster(bundle)
        with tempfile.TemporaryDirectory(prefix="aep-bundle-") as scratch:
            root = Path(scratch) / bundle.name
            home = Path(scratch) / "home"
            home.mkdir()
            for path, content in bundle.files.items():
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(content)
            return await self._run(root, self._env(home), bundle.name)

    async def _run(self, root: Path, env: dict[str, str], name: str) -> DevRun:
        target = [f"--target={TARGET}", *self._variables()]
        steps: list[Step] = []

        async def step(label: str, *args: str) -> Step:
            ok, output = await self._cli(root, env, *args)
            steps.append(Step(label, ok, _tail(output)))
            return steps[-1]

        stages: tuple[tuple[Literal["validate", "deploy"], list[str]], ...] = (
            ("validate", ["bundle", "validate", *target]),
            ("deploy", ["bundle", "deploy", *target, "--auto-approve"]),
        )
        for label, args in stages:
            done = await step(label, *args)
            if not done.ok:
                return DevRun("failed", label, steps, errors=[done.output.strip()])

        summary = await step("summary", "bundle", "summary", *target, "--output=json")
        try:
            resource = json.loads(summary.output)["resources"]["pipelines"][name]
            pipeline_id, url = resource["id"], resource.get("url")
        except (ValueError, KeyError, TypeError):
            return DevRun("failed", "deploy", steps, errors=["部署后找不到管道的 id"])

        ran = await step("run", "bundle", "run", *target, name)
        dev = DevRun(
            "passed" if ran.ok else "failed", "run", steps, pipeline_id=pipeline_id, url=url
        )
        try:
            dev.update_id, events = await self._update_outcome(pipeline_id)
        except DatabricksError as exc:
            dev.warnings.append(f"读取管道事件失败：{exc}")
            events = []
        if dev.update_id:
            dev.tables, dev.errors = summarize_events(events, dev.update_id)
        if not ran.ok:
            # Only a failed table is the generated code's fault. An update that
            # failed before any table ran (no permission to create its cluster,
            # quota, no serverless) is about the workspace; regenerating won't help.
            dev.fixable = not dev.errors or any(t.status == "FAILED" for t in dev.tables)
            if not dev.errors:
                dev.errors = [ran.output.strip() or "管道运行失败，没有拿到报错信息"]
        for table in dev.tables:
            if table.rows == 0:
                dev.warnings.append(f"{table.name} 写入了 0 行")
        return dev

    # --- cluster mode -------------------------------------------------------

    def _harness_spec(self, bundle: Bundle) -> dict[str, Any]:
        source_catalog = self._settings.databricks_source_catalog
        if PIPELINE_PY in bundle.files:
            return {
                "language": "pyspark",
                "source_catalog": source_catalog,
                "code": bundle.files[PIPELINE_PY],
            }
        return {
            "language": "sql",
            "source_catalog": source_catalog,
            "targets": parse_sql(bundle.files[PIPELINE_SQL]),
        }

    async def _cluster_running(self, cluster_id: str, dev: DevRun) -> None:
        """Waits for the cluster to run, starting it once if it is stopped."""
        deadline = time.monotonic() + self._settings.databricks_timeout_seconds
        started = False
        while True:
            cluster = await self._get("/api/2.0/clusters/get", cluster_id=cluster_id)
            state = cluster.get("state")
            if state == "RUNNING":
                return
            if state == "TERMINATED" and not started:
                await self._post("/api/2.0/clusters/start", {"cluster_id": cluster_id})
                dev.warnings.append(f"集群 {cluster_id} 原本是停止的，已启动它来试跑")
                started = True
            elif state == "TERMINATED" or state == "ERROR":
                raise DatabricksError(f"集群 {cluster_id} 没能启动：{cluster.get('state_message')}")
            if time.monotonic() > deadline:
                raise DatabricksError(f"集群 {cluster_id} 一直没有就绪（{state}）")
            await asyncio.sleep(CLUSTER_POLL_SECONDS)

    async def _execute(self, cluster_id: str, command: str) -> str:
        """Runs Python on the cluster (Command Execution API) and returns its output."""
        context = await self._post(
            "/api/1.2/contexts/create", {"clusterId": cluster_id, "language": "python"}
        )
        ids = {"clusterId": cluster_id, "contextId": context["id"]}
        deadline = time.monotonic() + self._settings.databricks_timeout_seconds
        try:
            while (await self._get("/api/1.2/contexts/status", **ids)).get("status") != "Running":
                if time.monotonic() > deadline:
                    raise DatabricksError("集群上的执行环境一直没有就绪")
                await asyncio.sleep(POLL_SECONDS)
            cmd = await self._post(
                "/api/1.2/commands/execute", ids | {"language": "python", "command": command}
            )
            while True:
                status = await self._get("/api/1.2/commands/status", **ids, commandId=cmd["id"])
                if status.get("status") in ("Finished", "Error", "Cancelled"):
                    break
                if time.monotonic() > deadline:
                    await self._post("/api/1.2/commands/cancel", ids | {"commandId": cmd["id"]})
                    limit = self._settings.databricks_timeout_seconds
                    raise DatabricksError(f"集群上的试跑超过 {limit:g} 秒没有结束")
                await asyncio.sleep(POLL_SECONDS)
        finally:
            try:
                await self._post("/api/1.2/contexts/destroy", ids)
            except DatabricksError:
                pass
        results = status.get("results") or {}
        if status.get("status") != "Finished" or results.get("resultType") == "error":
            cause = results.get("summary") or results.get("cause") or status.get("status")
            raise DatabricksError(f"集群上的试跑程序出错：{str(cause)[:2000]}")
        return str(results.get("data") or "")

    async def _run_on_cluster(self, bundle: Bundle) -> DevRun:
        cluster_id = self._settings.databricks_cluster_id
        dev = DevRun(
            "failed",
            "run",
            [],
            mode="cluster",
            cluster_id=cluster_id,
            url=f"{self._host}/compute/clusters/{cluster_id}",
        )
        if not self._settings.databricks_source_catalog:
            dev.errors = [
                "在集群上试跑需要设置 AEP_DATABRICKS_SOURCE_CATALOG（源表所在的 catalog）"
            ]
            return dev
        try:
            spec = self._harness_spec(bundle)
        except CodeError as exc:
            dev.errors = [str(exc)]
            return dev
        command = "\n".join(
            [
                Path(cluster_harness.__file__).read_text(),
                f"main(spark, json.loads({json.dumps(json.dumps(spec))}))",
            ]
        )
        try:
            await self._cluster_running(cluster_id, dev)
            output = await self._execute(cluster_id, command)
        except DatabricksError as exc:
            dev.steps.append(Step("cluster", False, _tail(str(exc))))
            dev.errors = [str(exc)]
            return dev
        dev.steps.append(Step("cluster", True, _tail(output)))
        line = next((x for x in output.splitlines() if x.startswith(cluster_harness.MARKER)), None)
        if line is None:
            dev.errors = ["集群上的试跑没有输出结果"]
            return dev
        outcome = json.loads(line.removeprefix(cluster_harness.MARKER))
        dev.tables = [TableResult(**t) for t in outcome["tables"]]
        dev.errors = outcome["errors"]
        if not dev.errors:
            dev.status = "passed"
        else:
            # Every error here comes from running the generated code.
            dev.fixable = True
        for table in dev.tables:
            if table.rows == 0:
                dev.warnings.append(f"{table.name} 结果是 0 行")
        return dev
