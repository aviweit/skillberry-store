# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0

"""The vMCP server publishes a tool's stored JSON Schema when it is richer than flat."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import types

from skillberry_store.modules import vmcp_server as vmcp_mod
from skillberry_store.modules.vmcp_server import (
    VirtualMcpServer,
    param_type_to_python_type,
    publishable_input_schema,
)

FLIGHT = {
    "type": "object",
    "title": "FlightInfo",
    "properties": {
        "flight_number": {"type": "string", "description": "Flight number"},
        "date": {"type": "string", "description": "Flight date"},
    },
    "required": ["flight_number", "date"],
}

RICH_PARAMS = {
    "type": "object",
    "properties": {
        "reservation_id": {"type": "string", "description": "The reservation ID"},
        "cabin": {"type": "string", "enum": ["business", "economy"], "description": "Cabin"},
        "flights": {"type": "array", "items": FLIGHT, "description": "Flights"},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None,
                 "description": "Optional note"},
    },
    "required": ["reservation_id", "cabin", "flights"],
    "optional": ["note"],
}

FLAT_PARAMS = {
    "type": "object",
    "properties": {"a": {"type": "string", "description": "first"}},
    "required": ["a"],
    "optional": [],
}


@pytest.fixture(autouse=True)
def _stub_object_handlers(monkeypatch):
    monkeypatch.setattr(vmcp_mod, "get_object_handler", lambda _name: MagicMock())


def _server(tools):
    with patch.object(VirtualMcpServer, "list_tools", return_value=tools), \
         patch.object(VirtualMcpServer, "_register_prompts"), \
         patch.object(VirtualMcpServer, "_start_server"):
        return VirtualMcpServer(name="t", description="d", port=None, tools=[])


def _tool(name, params):
    return types.Tool(name=name, description=f"{name} tool", inputSchema=params)


class TestPublishableInputSchema:
    def test_rich_schema_is_published_without_optional(self):
        schema = publishable_input_schema(RICH_PARAMS)
        assert schema is not None and "optional" not in schema
        assert schema["properties"]["flights"]["items"] == FLIGHT

    def test_flat_schema_is_not_published(self):
        assert publishable_input_schema(FLAT_PARAMS) is None

    def test_invalid_schema_is_not_published(self):
        bad = {"type": "object", "properties": {"x": {"type": "array", "items": 5}}}
        assert publishable_input_schema(bad) is None

    @pytest.mark.parametrize("params", [None, [], {"type": "array"}, {"type": "object"}])
    def test_non_object_schemas_are_not_published(self, params):
        assert publishable_input_schema(params) is None


def test_param_type_list_maps_to_object():
    assert param_type_to_python_type(["string", "null"]) is object
    assert param_type_to_python_type("array") is list


def test_list_tools_publishes_rich_schema_and_keeps_flat_as_is():
    server = _server([_tool("rich", RICH_PARAMS), _tool("flat", FLAT_PARAMS)])
    listed = {t.name: t.inputSchema for t in asyncio.run(server.mcp.list_tools())}

    assert listed["rich"] == {k: v for k, v in RICH_PARAMS.items() if k != "optional"}
    # a flat tool keeps the schema FastMCP derives from the handler signature
    assert listed["flat"]["properties"]["a"]["type"] == "string"
    assert listed["flat"]["title"] == "flatArguments"


def test_call_forwards_nested_and_untyped_arguments():
    server = _server([_tool("rich", RICH_PARAMS)])
    server.invoke_tool = AsyncMock(return_value={"return value": "ok"})
    args = {
        "reservation_id": "ZFA04Y",
        "cabin": "economy",
        "flights": [{"flight_number": "HAT001", "date": "2024-05-01"}],
        "note": "window",
    }

    asyncio.run(server.mcp.call_tool("rich", args))

    tool_name, parameters, _env = server.invoke_tool.call_args.args
    assert tool_name == "rich"
    assert parameters == args  # "note" (anyOf, no top-level type) is not dropped
