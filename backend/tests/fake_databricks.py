"""A stand-in Databricks workspace: a fake ``databricks`` CLI and the REST endpoints we read.

The CLI is a small script that records each call (arguments, the files in the
bundle directory, the environment it got) and answers like the real one. Each
``bundle run`` starts a new pipeline update that passes or fails in the order
given by ``runs``; a failed update's event log carries an AnalysisException.
"""

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
        if request.headers.get("authorization") != "Bearer dapi-test-token":
            return httpx.Response(401, json={"error_code": "UNAUTHENTICATED"})
        updates = self._state()["updates"]
        if request.url.path == f"/api/2.0/pipelines/{PIPELINE_ID}":
            latest = [{"update_id": f"update-{updates}"}] if updates else []
            return httpx.Response(200, json={"pipeline_id": PIPELINE_ID, "latest_updates": latest})
        if request.url.path == f"/api/2.0/pipelines/{PIPELINE_ID}/events":
            return httpx.Response(200, json={"events": self._events(updates)})
        return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
