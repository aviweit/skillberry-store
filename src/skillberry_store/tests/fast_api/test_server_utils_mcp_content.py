# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0

"""mcp_content / mcp_json_converter tolerate MCP properties without a top-level "type"."""

import ast

from skillberry_store.fast_api.server_utils import mcp_content, mcp_json_converter

TOOL = {
    "name": "book",
    "description": "Book a flight.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "cabin": {"type": "string", "enum": ["economy", "business"]},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        },
        "required": ["cabin"],
    },
}


def test_mcp_content_falls_back_to_object_for_untyped_property():
    stub = mcp_content(TOOL)
    assert stub.startswith("def book(cabin: string, note: object):")
    assert "note (object): The note parameter." in stub
    ast.parse(stub)


def test_mcp_content_flat_tool_is_unchanged():
    flat = {
        "name": "echo",
        "description": "Echo.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
    assert mcp_content(flat) == (
        "def echo(text: string):\n"
        '    """\n'
        "    Echo.\n"
        "    Parameters:\n"
        "        text (string): The text parameter.\n"
        '    """'
    )


def test_mcp_json_converter_falls_back_to_object_for_untyped_property():
    params = mcp_json_converter(TOOL, {})["params"]
    assert params["properties"]["note"]["type"] == "object"
    assert params["properties"]["cabin"]["type"] == "string"
    assert params["required"] == ["cabin"]
