# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0

"""The vMCP server publishes an annotation-derived schema for Pydantic/Literal tools."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import types

from skillberry_store.modules import vmcp_server as vmcp_mod
from skillberry_store.modules.vmcp_server import VirtualMcpServer

RICH_MODULE = '''
from typing import List, Literal, Optional
from pydantic import BaseModel, Field

class FlightInfo(BaseModel):
    flight_number: str = Field(description="Flight number")
    date: str = Field(description="Flight date")

def rich(reservation_id: str, cabin: Literal["business", "economy"], flights: List[FlightInfo | dict], note: Optional[str] = None, payment_id: str = "gift_card_1"):
    """Update flights."""
    return reservation_id
'''

FLAT_MODULE = '''
def flat(a: str):
    """Echo."""
    return a
'''

# What the store saves for these tools (flat: one type per parameter).
RICH_STORED = {
    "type": "object",
    "properties": {
        "reservation_id": {"type": "string", "description": "The reservation ID"},
        "cabin": {"type": "string", "description": "Cabin"},
        "flights": {"type": "string", "description": "Flights"},
        "note": {"type": "string", "description": "Optional note"},
        "payment_id": {"type": "string", "description": "Payment id"},
    },
    "required": ["reservation_id", "cabin", "flights", "note", "payment_id"],
    "optional": [],
}
FLAT_STORED = {
    "type": "object",
    "properties": {"a": {"type": "string", "description": "first"}},
    "required": ["a"],
    "optional": [],
}

MANIFESTS = {
    "rich": {"uuid": "u-rich", "name": "rich", "module_name": "rich.py",
             "programming_language": "python", "packaging_format": "code", "params": RICH_STORED},
    "flat": {"uuid": "u-flat", "name": "flat", "module_name": "flat.py",
             "programming_language": "python", "packaging_format": "code", "params": FLAT_STORED},
}
MODULES = {"u-rich": RICH_MODULE, "u-flat": FLAT_MODULE}


@pytest.fixture(autouse=True)
def _stub_object_handlers(monkeypatch):
    handler = MagicMock()
    handler.read_file.side_effect = lambda uuid, _name, raw_content=False: MODULES[uuid]
    monkeypatch.setattr(vmcp_mod, "get_object_handler", lambda _name: handler)


def _tool(name):
    return types.Tool(name=name, description=f"{name} tool", inputSchema=MANIFESTS[name]["params"])


def _server(names):
    def fake_list_tools(self):
        self._tool_manifests.update({n: MANIFESTS[n] for n in names})
        return [_tool(n) for n in names]

    with patch.object(VirtualMcpServer, "list_tools", fake_list_tools), \
         patch.object(VirtualMcpServer, "_register_prompts"), \
         patch.object(VirtualMcpServer, "_start_server"):
        return VirtualMcpServer(name="t", description="d", port=None, tools=[], app=object())


def _listed(server):
    return {t.name: t.inputSchema for t in asyncio.run(server.mcp.list_tools())}


def test_rich_tool_publishes_annotation_schema():
    schema = _listed(_server(["rich"]))["rich"]
    props = schema["properties"]
    assert props["cabin"]["enum"] == ["business", "economy"]
    assert props["cabin"]["description"] == "Cabin"  # from the stored params
    flight = next(s for s in props["flights"]["items"]["anyOf"] if s.get("title") == "FlightInfo")
    assert flight["required"] == ["flight_number", "date"]
    assert props["note"] == {"type": "string", "default": None, "title": "Note", "description": "Optional note"}
    assert props["payment_id"]["default"] == "gift_card_1"
    assert "null" not in str(schema)
    assert schema["required"] == ["reservation_id", "cabin", "flights"]


def test_flat_tool_keeps_fastmcp_schema():
    schema = _listed(_server(["flat"]))["flat"]
    assert schema["title"] == "flatArguments"
    assert schema["properties"]["a"]["type"] == "string"


def test_stored_params_are_not_modified():
    _server(["rich"])
    assert MANIFESTS["rich"]["params"] == RICH_STORED
    assert RICH_STORED["properties"]["flights"] == {"type": "string", "description": "Flights"}


@pytest.mark.parametrize(
    "override",
    [{"packaging_format": "mcp"}, {"programming_language": "bash"}, {"uuid": "missing"}],
)
def test_no_rich_schema_for_mcp_bash_or_unreadable_tools(override):
    server = _server([])
    server._tool_manifests["rich"] = {**MANIFESTS["rich"], **override}
    assert server._rich_input_schema("rich") is None


def test_call_forwards_nested_and_untyped_arguments():
    server = _server(["rich"])
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
    assert parameters == {**args, "payment_id": "gift_card_1"}  # the list arrives intact


def test_omitted_optionals_forward_their_real_defaults_and_null_is_accepted():
    server = _server(["rich"])
    server.invoke_tool = AsyncMock(return_value={"return value": "ok"})

    asyncio.run(server.mcp.call_tool(
        "rich", {"reservation_id": "Z", "cabin": "economy", "flights": [], "note": None}
    ))

    parameters = server.invoke_tool.call_args.args[1]
    assert parameters["payment_id"] == "gift_card_1"  # not None
    assert parameters["note"] is None
