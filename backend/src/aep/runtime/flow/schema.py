"""A run's input as a JSON schema: how an app is offered as a tool, to MCP clients
(aep.app_mcp) and to agents that call apps (docs/api/agent-apps.md, "调用应用")."""

from typing import Any


def input_schema(inputs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The JSON schema of a run's input: a flow app's start inputs, else ``request``."""
    if inputs is None:
        return {
            "type": "object",
            "properties": {"request": {"type": "string", "description": "需求"}},
            "required": ["request"],
        }
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in inputs:
        kind = f.get("type")
        prop: dict[str, Any] = {"description": f.get("label") or f["key"]}
        if kind == "number":
            prop["type"] = "number"
        elif kind == "boolean":
            prop["type"] = "boolean"
        elif kind == "select":
            prop["enum"] = [c["value"] for c in f.get("choices") or []]
        else:
            prop["type"] = "string"
            if f.get("max_length"):
                prop["maxLength"] = f["max_length"]
            if kind == "datasource":
                prop["description"] += "（数据源 id）"
        if f.get("default") is not None:
            prop["default"] = f["default"]
        properties[f["key"]] = prop
        if f.get("required") and f.get("default") is None:
            required.append(f["key"])
    return {"type": "object", "properties": properties, "required": required}
