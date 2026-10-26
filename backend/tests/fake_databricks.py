"""A stand-in Databricks workspace: a fake ``databricks`` CLI and the REST endpoints we read.

The CLI is a small script that records each call (arguments, the files in the
bundle directory, the environment it got) and answers like the real one. Each
``bundle run`` starts a new pipeline update that passes or fails in the order
given by ``runs``; a failed update's event log carries an AnalysisException.
``deny_clusters`` makes updates fail before any table runs, as in a workspace
where the user may not create clusters; ``drop`` makes the next REST requests
lose their connection.

The workspace also has one all-purpose cluster (``CLUSTER_ID``) answering the
Command Execution API: each command gets the next of ``cluster_outputs`` (an
outcome as ``aep.cluster_harness`` prints it, or an error string), and is kept
in ``commands``.

For job deploys it keeps imported files in ``files`` (a notebook under its path
without ``.py``) and jobs in ``jobs`` (id → settings). Each job run ends with
the next of ``job_outputs``: an outcome as the job's notebook reports it (the
run fails when it has errors), or an error string from a run that failed before
the notebook could report.
"""

import base64
import json
import stat
import sys
from pathlib import Path
from typing import Any

import httpx

from aep.config import Settings
from aep.databricks import DevTarget

HOST = "https://adb-1.test.databricks.azure.cn"
PIPELINE_ID = "pipe-1"
CLUSTER_ID = "0101-000000-abcdefgh"
USER = "dev.user@example.com"
JOB_PATHS = ("/api/2.0/workspace/", "/api/2.1/jobs/", "/api/2.0/preview/scim/")
CLUSTER_DENIED = (
    "Failed to create a pipeline cluster: PERMISSION_DENIED: You are not authorized to create "
    "clusters. Please contact your administrator."
)
MISSING_COLUMN = (
    "[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column or function parameter with name `o`.`qty` "
    "cannot be resolved. Did you mean one of the following? [`o`.`quantity`]"
)

CLI = """#!{python}
import json, os, sys
from pathlib import Path

import yaml

state_file = Path({state!r})
state = json.loads(state_file.read_text())
args = sys.argv[1:]
step = args[1] if len(args) > 1 else ""
state["calls"].append({{
    "args": args,
    "files": sorted(str(p) for p in Path(".").rglob("*") if p.is_file()),
    "env": {{k: v for k, v in os.environ.items() if k.startswith(("AEP_", "DATABRICKS_"))}},
}})
code, out = 0, ""
if step in state["fail"]:
    code, out = 1, state["fail"][step]
elif step == "summary":
    name = yaml.safe_load(Path("databricks.yml").read_text())["bundle"]["name"]
    url = "{host}/pipelines/{pipeline}"
    out = json.dumps({{"resources": {{"pipelines": {{name: {{"id": "{pipeline}", "url": url}}}}}}}})
elif step == "run":
    passed = state["runs"][state["updates"]] if state["updates"] < len(state["runs"]) else True
    state["updates"] += 1
    code = 0 if passed else 1
    out = "Update completed" if passed else "Error: update failed. Check the event log."
state_file.write_text(json.dumps(state))
print(out)
sys.exit(code)
"""


class FakeWorkspace:
    def __init__(self, tmp_path: Path, runs: list[bool] | None = None) -> None:
        self.state_file = tmp_path / "workspace.json"
        self.state_file.write_text(
            json.dumps({"calls": [], "runs": runs or [], "fail": {}, "updates": 0})
        )
        self.clusters_denied = False
        self.drops = 0
        self.cluster_state = "RUNNING"
        self.cluster_outputs: list[dict[str, Any] | str] = []
        self.commands: list[dict[str, Any]] = []
        self.contexts: set[str] = set()
        self.files: dict[str, str] = {}
        self.folders: set[str] = set()
        self.jobs: dict[int, dict[str, Any]] = {}
        self.job_runs: list[int] = []
        self.job_outputs: list[dict[str, Any] | str] = []
        self.cli = tmp_path / "databricks"
        self.cli.write_text(
            CLI.format(
                python=sys.executable, state=str(self.state_file), host=HOST, pipeline=PIPELINE_ID
            )
        )
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IEXEC)
        self.requests: list[httpx.Request] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text())

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self._state()["calls"]

    def fail(self, step: str, output: str) -> None:
        state = self._state()
        state["fail"][step] = output
        self.state_file.write_text(json.dumps(state))

    def deny_clusters(self) -> None:
        self.clusters_denied = True
        state = self._state()
        state["runs"] = [False] * 10
        self.state_file.write_text(json.dumps(state))

    def drop(self, requests: int) -> None:
        self.drops = requests

    def settings(self, **changes: Any) -> Settings:
        values: dict[str, Any] = {
            "llm_gateway_url": "http://gateway.test",
            "llm_gateway_key": "sk-test",
            "databricks_host": HOST,
            "databricks_token": "dapi-test-token",
            "databricks_cli": str(self.cli),
            "databricks_catalog": "icsd-ace-dev",
            "databricks_schema": "trac_b_test",
            "databricks_source_catalog": "icsd-ace-dev",
            "databricks_timeout_seconds": 30,
        }
        return Settings(**(values | changes))

    def target(self, **changes: Any) -> DevTarget:
        return DevTarget(self.settings(**changes), http=self.http)

    def _events(self, update: int) -> list[dict[str, Any]]:
        update_id = f"update-{update}"
        origin = {"pipeline_id": PIPELINE_ID, "update_id": update_id, "flow_name": "order_facts"}
        passed = self._state()["runs"][update - 1] if update <= len(self._state()["runs"]) else True
        if self.clusters_denied:
            return [
                {
                    "timestamp": "2026-10-26T08:00:02Z",
                    "level": "ERROR",
                    "event_type": "update_progress",
                    "message": f"Update {update_id} is FAILED.",
                    "origin": {"pipeline_id": PIPELINE_ID, "update_id": update_id},
                    "error": {
                        "exceptions": [
                            {
                                "class_name": "com.databricks.pipelines.common.errors."
                                "deployment.DeploymentException",
                                "message": CLUSTER_DENIED,
                            }
                        ]
                    },
                    "details": {"update_progress": {"state": "FAILED"}},
                }
            ]
        if not passed:
            return [
                {
                    "timestamp": "2026-10-26T08:00:02Z",
                    "level": "ERROR",
                    "event_type": "flow_progress",
                    "message": "Failed to resolve flow: 'order_facts'.",
                    "origin": origin,
                    "error": {
                        "exceptions": [
                            {
                                "class_name": "pyspark.errors.AnalysisException",
                                "message": MISSING_COLUMN,
                            }
                        ]
                    },
                    "details": {"flow_progress": {"status": "FAILED"}},
                },
                {
                    "timestamp": "2026-10-26T08:00:01Z",
                    "level": "INFO",
                    "origin": origin,
                    "details": {"flow_progress": {"status": "STARTING"}},
                },
            ]
        return [
            {
                "timestamp": "2026-10-26T08:05:00Z",
                "level": "INFO",
                "origin": origin,
                "details": {
                    "flow_progress": {
                        "status": "COMPLETED",
                        "metrics": {"num_output_rows": 4990},
                        "data_quality": {
                            "dropped_records": 10,
                            "expectations": [
                                {
                                    "name": "positive_quantity",
                                    "dataset": "order_facts",
                                    "passed_records": 4990,
                                    "failed_records": 10,
                                }
                            ],
                        },
                    }
                },
            },
            {
                "timestamp": "2026-10-26T08:04:00Z",
                "level": "INFO",
                "origin": origin,
                "details": {"flow_progress": {"status": "RUNNING"}},
            },
            # An earlier update's event; not part of this one.
            {
                "timestamp": "2026-10-26T07:00:00Z",
                "level": "ERROR",
                "message": "old failure",
                "origin": {**origin, "update_id": "update-0"},
            },
        ]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.drops:
            self.drops -= 1
            raise httpx.ConnectError("Connection reset by peer", request=request)
        if request.headers.get("authorization") != "Bearer dapi-test-token":
            return httpx.Response(401, json={"error_code": "UNAUTHENTICATED"})
        if "/clusters/" in request.url.path or request.url.path.startswith("/api/1.2/"):
            return self._cluster(request)
        if request.url.path.startswith(JOB_PATHS):
            return self._jobs(request)
        updates = self._state()["updates"]
        if request.url.path == f"/api/2.0/pipelines/{PIPELINE_ID}":
            latest = [{"update_id": f"update-{updates}"}] if updates else []
            return httpx.Response(200, json={"pipeline_id": PIPELINE_ID, "latest_updates": latest})
        if request.url.path == f"/api/2.0/pipelines/{PIPELINE_ID}/events":
            return httpx.Response(200, json={"events": self._events(updates)})
        return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})

    def _cluster(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        body = json.loads(request.content) if request.content else {}
        cluster = request.url.params.get("cluster_id") or body.get("cluster_id")
        cluster = cluster or request.url.params.get("clusterId") or body.get("clusterId")
        if cluster != CLUSTER_ID:
            return httpx.Response(400, json={"error_code": "INVALID_PARAMETER_VALUE"})
        if path == "2.0/clusters/get":
            state = self.cluster_state
            if state == "STARTING":  # comes up on the next look
                self.cluster_state = "RUNNING"
                state = "PENDING"
            return httpx.Response(200, json={"cluster_id": CLUSTER_ID, "state": state})
        if path == "2.0/clusters/start":
            self.cluster_state = "STARTING"
            return httpx.Response(200, json={})
        if path == "1.2/contexts/create":
            context = f"ctx-{len(self.contexts) + 1}"
            self.contexts.add(context)
            return httpx.Response(200, json={"id": context})
        if path == "1.2/contexts/status":
            return httpx.Response(200, json={"status": "Running"})
        if path == "1.2/contexts/destroy":
            self.contexts.discard(body["contextId"])
            return httpx.Response(200, json={})
        if path == "1.2/commands/execute":
            self.commands.append(body)
            return httpx.Response(200, json={"id": f"cmd-{len(self.commands)}"})
        if path == "1.2/commands/status":
            index = int(request.url.params["commandId"].removeprefix("cmd-")) - 1
            output = self.cluster_outputs[index] if index < len(self.cluster_outputs) else {}
            if isinstance(output, str):
                results = {"resultType": "error", "summary": output}
            else:
                outcome = {"tables": [], "errors": []} | output
                results = {"resultType": "text", "data": "AEP_RESULT " + json.dumps(outcome)}
            return httpx.Response(200, json={"status": "Finished", "results": results})
        return httpx.Response(404, json={"error_code": "ENDPOINT_NOT_FOUND"})

    def _jobs(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        body = json.loads(request.content) if request.content else {}
        params = request.url.params
        if path == "2.0/preview/scim/v2/Me":
            return httpx.Response(200, json={"userName": USER})
        if path == "2.0/workspace/mkdirs":
            self.folders.add(body["path"])
            return httpx.Response(200, json={})
        if path == "2.0/workspace/import":
            folder = body["path"].rsplit("/", 1)[0]
            if folder not in self.folders:
                return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
            content = base64.b64decode(body["content"]).decode()
            target = body["path"]
            if content.startswith("# Databricks notebook source"):
                target = target.removesuffix(".py")
            self.files[target] = content
            return httpx.Response(200, json={})
        if path == "2.1/jobs/list":
            jobs = [
                {"job_id": job_id, "settings": job}
                for job_id, job in self.jobs.items()
                if job["name"] == params.get("name")
            ]
            return httpx.Response(200, json={"jobs": jobs} if jobs else {})
        if path == "2.1/jobs/create":
            job_id = 100 + len(self.jobs)
            self.jobs[job_id] = body
            return httpx.Response(200, json={"job_id": job_id})
        if path == "2.1/jobs/reset":
            self.jobs[body["job_id"]] = body["new_settings"]
            return httpx.Response(200, json={})
        if path == "2.1/jobs/run-now":
            if body["job_id"] not in self.jobs:
                return httpx.Response(400, json={"error_code": "INVALID_PARAMETER_VALUE"})
            self.job_runs.append(body["job_id"])
            return httpx.Response(200, json={"run_id": 500 + len(self.job_runs)})
        if path == "2.1/jobs/runs/get":
            run_id = int(params["run_id"])
            output = self._job_output(run_id - 500)
            passed = isinstance(output, dict) and not output.get("errors")
            state = {
                "life_cycle_state": "TERMINATED",
                "result_state": "SUCCESS" if passed else "FAILED",
                "state_message": "" if passed else "Workload failed, see run output for details",
            }
            return httpx.Response(
                200,
                json={
                    "run_id": run_id,
                    "state": state,
                    "run_page_url": f"{HOST}/#job/{self.job_runs[run_id - 501]}/run/{run_id}",
                    "tasks": [{"run_id": run_id + 1000, "task_key": "run", "attempt_number": 0}],
                },
            )
        if path == "2.1/jobs/runs/get-output":
            output = self._job_output(int(params["run_id"]) - 1500)
            if isinstance(output, str):
                return httpx.Response(200, json={"error": output})
            outcome = {"tables": [], "errors": [], "write_errors": []} | output
            text = "AEP_RESULT " + json.dumps(outcome)
            if outcome["errors"] or outcome["write_errors"]:
                return httpx.Response(200, json={"error": f"RuntimeError: {text}"})
            return httpx.Response(200, json={"notebook_output": {"result": text}})
        return httpx.Response(404, json={"error_code": "ENDPOINT_NOT_FOUND"})

    def _job_output(self, number: int) -> dict[str, Any] | str:
        index = number - 1
        return self.job_outputs[index] if index < len(self.job_outputs) else {}
