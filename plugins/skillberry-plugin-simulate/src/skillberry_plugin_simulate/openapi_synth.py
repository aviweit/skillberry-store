"""Synthesize an OpenAPI 3.0.3 spec from skill tool manifests.

operationId is emitted as the exact MCP tool name so the simulation harness
exposes tool names identical to the real vMCP (load-bearing — §4.5).
"""
from typing import Any, Dict, List, Protocol


class Synthesizer(Protocol):
    def synthesize(self, tools: List[Dict[str, Any]], title: str) -> Dict[str, Any]:
        ...


def to_oas30(node: Any) -> Any:
    """Rewrite JSON Schema null types into the OpenAPI 3.0 ``nullable`` form.

    Tool params are JSON Schema (pydantic emits ``Optional[X]`` as
    ``anyOf: [X, {"type": "null"}]``), but OpenAPI 3.0 has no ``null`` type.
    """
    if isinstance(node, list):
        return [to_oas30(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: to_oas30(v) for k, v in node.items()}
    for key in ("anyOf", "oneOf"):
        options = out.get(key)
        if not isinstance(options, list):
            continue
        rest = [o for o in options if o != {"type": "null"}]
        if len(rest) == len(options):
            continue
        out["nullable"] = True
        if len(rest) == 1 and isinstance(rest[0], dict):
            del out[key]
            out = {**rest[0], **out}
        else:
            out[key] = rest
    if isinstance(out.get("type"), list):
        types = [t for t in out["type"] if t != "null"]
        if len(types) < len(out["type"]):
            out["nullable"] = True
        out["type"] = types[0] if types else "object"
    return out


class OpenApiSynthesizer:
    """Default synthesizer: input-schema-only fidelity (D7 enhancement deferred)."""

    OPENAPI_VERSION = "3.0.3"

    def synthesize(self, tools: List[Dict[str, Any]], title: str) -> Dict[str, Any]:
        paths: Dict[str, Any] = {}
        for tool in tools:
            name = tool["name"]
            params = tool.get("params") or {"type": "object", "properties": {}}
            request_schema = to_oas30(dict(params))
            request_schema.setdefault("type", "object")
            paths[f"/{name}"] = {
                "post": {
                    "operationId": name,  # NOT execute_<name>
                    "summary": tool.get("description") or name,
                    "requestBody": {
                        "required": bool(request_schema.get("required")),
                        "content": {
                            "application/json": {"schema": request_schema}
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Successful tool execution",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"}
                                }
                            },
                        },
                        "400": {"description": "Bad request"},
                        "500": {"description": "Internal server error"},
                    },
                }
            }
        return {
            "openapi": self.OPENAPI_VERSION,
            "info": {"title": title, "version": "1.0.0"},
            "paths": paths,
        }
