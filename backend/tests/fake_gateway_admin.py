"""An in-process stand-in for LiteLLM's model management API (docs/api/models.md): the
models in its config file, those added through ``/model/new``, its database state."""

import copy
import json
from typing import Any

import httpx

from aep.config import Settings
from aep.models import GatewayAdmin

CONFIG_MODELS: list[dict[str, Any]] = [
    {
        "model_name": "mock",
        "litellm_params": {"model": "openai/mock", "mock_response": "这是 mock 模型的回复。"},
        "model_info": {"id": "cfg-mock", "db_model": False, "aep_display_name": "Mock（测试）"},
    },
    {
        "model_name": "company",
        "litellm_params": {
            "model": "openai/deepseek-v4.1-flash-new",
            "api_base": "https://aigw.example.com/v1",
        },
        "model_info": {
            "id": "cfg-company",
            "db_model": False,
            "aep_display_name": "公司网关 DeepSeek",
            "aep_purpose": "chat",
            "aep_provider": "openai_compatible",
        },
    },
    {
        "model_name": "company-embed",
        "litellm_params": {"model": "openai/text-embedding-v4"},
        "model_info": {
            "id": "cfg-company-embed",
            "db_model": False,
            "aep_purpose": "embedding",
            "mode": "embedding",
        },
    },
]


class FakeGatewayAdmin:
    def __init__(self, db: str = "connected", up: bool = True) -> None:
        self.db = db
        self.up = up
        self.deployments: list[dict[str, Any]] = copy.deepcopy(CONFIG_MODELS)
        # The params each page model was given, keys included (the gateway's own copy).
        self.secrets: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []

    def admin(self, settings: Settings) -> GatewayAdmin:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        return GatewayAdmin.from_settings(settings, client)

    def by_alias(self, alias: str) -> dict[str, Any] | None:
        return next((d for d in self.deployments if d["model_name"] == alias), None)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("refused", request=request)
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        if path == "/health/readiness":
            return httpx.Response(200, json={"status": "healthy", "db": self.db})
        if path == "/model/info":
            public = []
            for dep in self.deployments:
                item = copy.deepcopy(dep)
                item["litellm_params"].pop("api_key", None)
                public.append(item)
            return httpx.Response(200, json={"data": public})
        if self.db != "connected":
            return httpx.Response(500, json={"error": {"message": "No DB Connected"}})
        if path == "/model/new":
            info = dict(body["model_info"]) | {"db_model": True}
            if any(d["model_info"]["id"] == info["id"] for d in self.deployments):
                return httpx.Response(500, json={"error": {"message": "Failed to add model"}})
            params = dict(body["litellm_params"])
            self.secrets[info["id"]] = dict(params)
            self.deployments.append(
                {"model_name": body["model_name"], "litellm_params": params, "model_info": info}
            )
            return httpx.Response(200, json={"model_id": info["id"]})
        if path == "/model/delete":
            before = len(self.deployments)
            self.deployments = [d for d in self.deployments if d["model_info"]["id"] != body["id"]]
            if len(self.deployments) == before:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json={"message": "deleted"})
        if path.startswith("/model/") and path.endswith("/update"):
            model_id = path.removeprefix("/model/").removesuffix("/update")
            dep = next((d for d in self.deployments if d["model_info"]["id"] == model_id), None)
            if dep is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            dep["litellm_params"].update(body.get("litellm_params") or {})
            dep["model_info"].update(body.get("model_info") or {})
            self.secrets[model_id].update(body.get("litellm_params") or {})
            return httpx.Response(200, json={"model_id": model_id})
        return httpx.Response(404, json={"error": {"message": f"no route {path}"}})
