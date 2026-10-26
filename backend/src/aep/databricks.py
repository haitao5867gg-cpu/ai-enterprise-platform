"""Deploys a generated bundle to the Databricks dev target, runs it and reads the outcome.

The Databricks CLI does the bundle work (``bundle validate``, ``bundle deploy -t
dev``, ``bundle run -t dev``) in a scratch directory holding only the bundle's
files. The generated code itself runs on Databricks, never here. After the run,
the pipeline's event log (REST API) says what each table wrote, what its quality
rules caught and, when the update failed, why.

For workspaces where pipelines cannot start their own clusters there are two
modes on an existing cluster (``AEP_DATABRICKS_CLUSTER_ID``), chosen by
``AEP_DATABRICKS_DEPLOY``. ``job`` (the default when a cluster is given) deploys
the bundle's job through the REST API, no CLI needed: its files go where
``bundle deploy`` would put them and the job is created or updated like a dev
target would, then run once; the job's notebook reports the outcome. ``cluster``
deploys nothing: the transformations run once on the cluster through the
Command Execution API (``aep.cluster_harness``) and nothing is written.

After a person approves, ``release`` deploys the same bundle to the prod target
(``AEP_DATABRICKS_PROD_CATALOG`` / ``_SCHEMA``) on a schedule, without running it
there: in ``job`` mode the job is created or updated through the REST API, in
``pipeline`` mode ``bundle deploy -t prod`` deploys the pipeline and the job that
refreshes it.

Off unless ``AEP_DATABRICKS_HOST`` and ``AEP_DATABRICKS_TOKEN`` are set.
"""

import asyncio
import base64
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from aep import cluster_harness
from aep.config import Settings
from aep.etl.codegen import PIPELINE_PY, PIPELINE_SQL, Bundle, CodeError, parse_sql

TARGET = "dev"
PROD = "prod"
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
    workspace or the configuration, not the generated code. In ``job`` mode the
    stages are ``deploy`` (files and job) and ``run`` (``job_id``, ``run_id``) on
    ``cluster_id``; in ``cluster`` mode there is only the ``run`` stage.
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
    mode: Literal["pipeline", "job", "cluster"] = "pipeline"
    cluster_id: str | None = None
    job_id: int | None = None
    run_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Release:
    """The deploy of an approved bundle to the prod target.

    Nothing runs at deploy time: the job (``job_id``; in ``pipeline`` mode the
    job that refreshes ``pipeline_id``) runs on ``schedule`` (Quartz cron) in
    ``timezone`` and writes to ``catalog``.``schema``.
    """

    status: Literal["deployed", "failed"]
    mode: Literal["pipeline", "job"]
    catalog: str
    schema: str
    schedule: str
    timezone: str
    steps: list[Step] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    job_id: int | None = None
    pipeline_id: str | None = None
    cluster_id: str | None = None
    url: str | None = None

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

    def _values(self, target: str, schedule: str | None = None) -> dict[str, str]:
        """Bundle variables the platform sets for ``target``; empty ones keep the bundle's."""
        s = self._settings
        if target == PROD:
            return {
                "catalog": s.databricks_prod_catalog,
                "schema": s.databricks_prod_schema,
                "source_catalog": s.databricks_source_catalog,
                "cluster_id": s.databricks_prod_cluster_id or s.databricks_cluster_id,
                "schedule": schedule or s.databricks_prod_schedule,
                "timezone": s.databricks_prod_timezone,
            }
        return {
            "catalog": s.databricks_catalog,
            "schema": s.databricks_schema,
            "source_catalog": s.databricks_source_catalog,
            "cluster_id": s.databricks_cluster_id,
        }

    def _variables(self, target: str = TARGET, schedule: str | None = None) -> list[str]:
        values = self._values(target, schedule)
        values.pop("cluster_id")  # Only job bundles have it, and those deploy over REST.
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
        mode = self._settings.databricks_deploy_mode
        if mode == "cluster":
            return await self._run_on_cluster(bundle)
        if mode == "job":
            return await self._run_job(bundle)
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
        outcome = _outcome(output)
        if outcome is None:
            dev.errors = ["集群上的试跑没有输出结果"]
            return dev
        _apply(dev, outcome)
        return dev

    # --- job mode -----------------------------------------------------------

    async def _upload(self, root: str, files: dict[str, str]) -> None:
        """Puts the bundle's files under ``root``; a notebook source becomes a notebook."""
        for folder in sorted({str(Path(root, path).parent) for path in files}):
            await self._post("/api/2.0/workspace/mkdirs", {"path": folder})
        for path, content in files.items():
            body = {
                "path": f"{root}/{path}",
                "format": "AUTO",
                "overwrite": True,
                "content": base64.b64encode(content.encode()).decode(),
            }
            await self._post("/api/2.0/workspace/import", body)

    def _job_settings(
        self,
        config: dict[str, Any],
        name: str,
        short_name: str,
        root: str,
        target: str = TARGET,
        schedule: str | None = None,
    ) -> dict[str, Any]:
        """The bundle's job as ``target`` deploys it, its variables filled in."""
        values = {k: str(v.get("default", "")) for k, v in config["variables"].items()}
        overrides = config["targets"].get(target) or {}
        values |= {
            k: str(v).replace("${workspace.current_user.short_name}", short_name)
            for k, v in (overrides.get("variables") or {}).items()
        }
        values |= {k: v for k, v in self._values(target, schedule).items() if v}

        def fill(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: fill(v) for k, v in node.items()}
            if isinstance(node, list):
                return [fill(v) for v in node]
            if isinstance(node, str):
                return re.sub(r"\$\{var\.(\w+)\}", lambda m: values.get(m[1], m[0]), node)
            return node

        job = fill(config["resources"]["jobs"][name])
        job |= fill(((overrides.get("resources") or {}).get("jobs") or {}).get(name) or {})
        if target == PROD:
            job["tags"] = {**job.get("tags", {}), "aep_bundle": name, "aep_target": PROD}
        else:
            # Development mode, as bundle deploy does it: the job is named and tagged
            # for its user. A failure goes back to code generation at once, so the
            # dev job does not retry.
            job["name"] = f"[dev {short_name}] {job.get('name', name)}"
            job["tags"] = {**job.get("tags", {}), "dev": short_name, "aep_bundle": name}
        for task in job.get("tasks", []):
            if target != PROD:
                task.pop("max_retries", None)
                task.pop("min_retry_interval_millis", None)
            notebook = task.get("notebook_task")
            if notebook and not notebook["notebook_path"].startswith("/"):
                # Imported notebooks lose their extension.
                path = notebook["notebook_path"].removesuffix(".py")
                notebook["notebook_path"] = f"{root}/{path}"
        return job

    async def _put_job(self, settings: dict[str, Any]) -> int:
        """Updates the job of that name, or creates it."""
        found = await self._get("/api/2.1/jobs/list", name=settings["name"])
        jobs = found.get("jobs") or []
        if jobs:
            job_id = int(jobs[0]["job_id"])
            await self._post("/api/2.1/jobs/reset", {"job_id": job_id, "new_settings": settings})
            return job_id
        return int((await self._post("/api/2.1/jobs/create", settings))["job_id"])

    async def _wait_run(self, run_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self._settings.databricks_timeout_seconds
        while True:
            run = await self._get("/api/2.1/jobs/runs/get", run_id=run_id)
            state = (run.get("state") or {}).get("life_cycle_state")
            if state in ("TERMINATED", "SKIPPED", "INTERNAL_ERROR"):
                return run
            if time.monotonic() > deadline:
                await self._post("/api/2.1/jobs/runs/cancel", {"run_id": run_id})
                limit = self._settings.databricks_timeout_seconds
                raise DatabricksError(f"Job 运行超过 {limit:g} 秒没有结束，已取消")
            await asyncio.sleep(CLUSTER_POLL_SECONDS)

    async def _run_job(self, bundle: Bundle) -> DevRun:
        cluster_id = self._settings.databricks_cluster_id
        dev = DevRun("failed", "deploy", [], mode="job", cluster_id=cluster_id or None)
        if not cluster_id or not self._settings.databricks_source_catalog:
            dev.errors = [
                "部署成 Job 需要设置 AEP_DATABRICKS_CLUSTER_ID（Job 跑在哪台集群上）和"
                " AEP_DATABRICKS_SOURCE_CATALOG（源表所在的 catalog）"
            ]
            return dev
        try:
            config = yaml.safe_load(bundle.files["databricks.yml"])
            config["resources"]["jobs"][bundle.name]
        except (yaml.YAMLError, KeyError, TypeError):
            dev.errors = ["bundle 里没有这个 Job 的定义（databricks.yml 的 resources.jobs）"]
            return dev
        try:
            user = (await self._get("/api/2.0/preview/scim/v2/Me"))["userName"]
            short_name = re.sub(r"[^a-z0-9]+", "_", user.split("@")[0].lower()).strip("_")
            root = f"/Users/{user}/.bundle/{bundle.name}/{TARGET}/files"
            await self._upload(root, bundle.files)
            dev.job_id = await self._put_job(
                self._job_settings(config, bundle.name, short_name, root)
            )
        except DatabricksError as exc:
            dev.steps.append(Step("deploy", False, _tail(str(exc))))
            dev.errors = [str(exc)]
            return dev
        dev.steps.append(Step("deploy", True, f"job {dev.job_id}，文件在 {root}"))
        dev.stage = "run"
        try:
            dev.run_id = int(
                (await self._post("/api/2.1/jobs/run-now", {"job_id": dev.job_id}))["run_id"]
            )
            run = await self._wait_run(dev.run_id)
            dev.url = run.get("run_page_url")
            tasks = sorted(run.get("tasks") or [], key=lambda t: t.get("attempt_number", 0))
            output = await self._get("/api/2.1/jobs/runs/get-output", run_id=tasks[-1]["run_id"])
        except (DatabricksError, IndexError, KeyError) as exc:
            dev.steps.append(Step("run", False, _tail(str(exc))))
            dev.errors = [str(exc) or "Job 运行失败，没有拿到报错信息"]
            return dev
        state = run.get("state") or {}
        text = (output.get("notebook_output") or {}).get("result") or output.get("error") or ""
        dev.steps.append(Step("run", state.get("result_state") == "SUCCESS", _tail(text)))
        outcome = _outcome(text)
        if outcome is None:
            # The job failed before the harness could report: the cluster, the
            # notebook or the workspace, not the generated code.
            message = output.get("error") or state.get("state_message") or "Job 没有输出结果"
            dev.errors = [str(message)]
            return dev
        _apply(dev, outcome)
        return dev

    # --- release to prod ----------------------------------------------------

    async def release(self, bundle: Bundle, schedule: str | None = None) -> Release:
        """Deploys an approved bundle to the prod target, to run on ``schedule``."""
        values = self._values(PROD, schedule)
        mode = self._settings.databricks_deploy_mode
        release = Release(
            "failed",
            "job" if mode == "job" else "pipeline",
            values["catalog"],
            values["schema"],
            values["schedule"],
            values["timezone"],
        )
        blocker = self._settings.databricks_release_blocker
        if blocker:
            release.errors = [blocker]
            return release
        if mode == "job":
            await self._release_job(bundle, release, values)
            return release
        with tempfile.TemporaryDirectory(prefix="aep-bundle-") as scratch:
            root = Path(scratch) / bundle.name
            home = Path(scratch) / "home"
            home.mkdir()
            for path, content in bundle.files.items():
                file = root / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(content)
            await self._release_pipeline(root, self._env(home), bundle.name, release)
        return release

    async def _release_job(self, bundle: Bundle, release: Release, values: dict[str, str]) -> None:
        release.cluster_id = values["cluster_id"] or None
        if not release.cluster_id:
            release.errors = ["Job 上线需要集群（AEP_DATABRICKS_PROD_CLUSTER_ID 或 _CLUSTER_ID）"]
            return
        try:
            config = yaml.safe_load(bundle.files["databricks.yml"])
            config["resources"]["jobs"][bundle.name]
        except (yaml.YAMLError, KeyError, TypeError):
            release.errors = ["bundle 里没有这个 Job 的定义（databricks.yml 的 resources.jobs）"]
            return
        try:
            user = (await self._get("/api/2.0/preview/scim/v2/Me"))["userName"]
            short_name = re.sub(r"[^a-z0-9]+", "_", user.split("@")[0].lower()).strip("_")
            root = f"/Users/{user}/.bundle/{bundle.name}/{PROD}/files"
            await self._upload(root, bundle.files)
            settings = self._job_settings(
                config, bundle.name, short_name, root, PROD, release.schedule
            )
            release.job_id = await self._put_job(settings)
        except DatabricksError as exc:
            release.steps.append(Step("deploy", False, _tail(str(exc))))
            release.errors = [str(exc)]
            return
        release.status = "deployed"
        release.url = f"{self._host}/#job/{release.job_id}"
        release.steps.append(Step("deploy", True, f"job {release.job_id}，文件在 {root}"))

    async def _release_pipeline(
        self, root: Path, env: dict[str, str], name: str, release: Release
    ) -> None:
        target = [f"--target={PROD}", *self._variables(PROD, release.schedule)]
        stages = (
            ("validate", ["bundle", "validate", *target]),
            ("deploy", ["bundle", "deploy", *target, "--auto-approve"]),
            ("summary", ["bundle", "summary", *target, "--output=json"]),
        )
        output = ""
        for label, args in stages:
            ok, output = await self._cli(root, env, *args)
            release.steps.append(Step(label, ok, _tail(output)))
            if not ok:
                release.errors = [output.strip()]
                return
        try:
            resources = json.loads(output)["resources"]
            pipeline = resources["pipelines"][name]
            release.pipeline_id, release.url = pipeline["id"], pipeline.get("url")
            refresh = (resources.get("jobs") or {}).get(f"{name}_refresh") or {}
            release.job_id = int(refresh["id"]) if refresh.get("id") else None
        except (ValueError, KeyError, TypeError):
            release.errors = ["部署后找不到管道的 id"]
            return
        release.status = "deployed"


def _outcome(text: str) -> dict[str, Any] | None:
    """The harness's ``AEP_RESULT`` JSON in an output or error message."""
    at = text.find(cluster_harness.MARKER)
    if at < 0:
        return None
    try:
        outcome, _ = json.JSONDecoder().raw_decode(text, at + len(cluster_harness.MARKER))
    except ValueError:
        return None
    return outcome if isinstance(outcome, dict) else None


def _apply(dev: DevRun, outcome: dict[str, Any]) -> None:
    dev.tables = [TableResult(**t) for t in outcome.get("tables", [])]
    code_errors = outcome.get("errors", [])
    dev.errors = code_errors + outcome.get("write_errors", [])
    if not dev.errors:
        dev.status = "passed"
    # Errors from running the generated code are worth regenerating it for;
    # failing to store a table is about the workspace.
    dev.fixable = bool(code_errors)
    for table in dev.tables:
        if table.rows == 0:
            dev.warnings.append(f"{table.name} 结果是 0 行")
