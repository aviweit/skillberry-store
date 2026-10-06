from skillberry_plugin_simulate.openapi_synth import OpenApiSynthesizer, to_oas30


def _tool(name="get_weather"):
    return {
        "name": name,
        "description": "Get weather for a city",
        "params": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }


def test_operation_id_is_tool_name_not_execute_prefixed():
    spec = OpenApiSynthesizer().synthesize([_tool()], title="skill")
    op = spec["paths"]["/get_weather"]["post"]
    assert op["operationId"] == "get_weather"
    assert not op["operationId"].startswith("execute_")


def test_openapi_version_and_structure():
    spec = OpenApiSynthesizer().synthesize([_tool()], title="skill")
    assert spec["openapi"] == "3.0.3"
    assert spec["info"]["title"] == "skill"
    op = spec["paths"]["/get_weather"]["post"]
    body_schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["properties"]["city"]["type"] == "string"
    assert "200" in op["responses"]


def test_multiple_tools_each_get_a_path():
    spec = OpenApiSynthesizer().synthesize([_tool("a"), _tool("b")], title="s")
    assert set(spec["paths"]) == {"/a", "/b"}


def test_tool_without_params_still_valid():
    spec = OpenApiSynthesizer().synthesize([{"name": "ping"}], title="s")
    op = spec["paths"]["/ping"]["post"]
    assert op["operationId"] == "ping"
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert schema["type"] == "object"


def test_rich_params_null_types_become_nullable():
    tool = {
        "name": "book",
        "params": {
            "type": "object",
            "properties": {
                "cabin": {"type": "string", "enum": ["economy", "business"]},
                "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                "flights": {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "object", "properties": {"date": {"type": "string"}}},
                            {"type": "object", "additionalProperties": True},
                        ]
                    },
                },
            },
            "required": ["cabin", "flights"],
        },
    }
    spec = OpenApiSynthesizer().synthesize([tool], title="s")
    props = spec["paths"]["/book"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert props["note"] == {"type": "string", "nullable": True, "default": None}
    assert props["cabin"]["enum"] == ["economy", "business"]
    assert len(props["flights"]["items"]["anyOf"]) == 2


def test_to_oas30_handles_multi_option_and_list_types():
    assert to_oas30({"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}) == {
        "anyOf": [{"type": "string"}, {"type": "integer"}],
        "nullable": True,
    }
    assert to_oas30({"type": ["string", "null"]}) == {"type": "string", "nullable": True}
    flat = {"type": "object", "properties": {"city": {"type": "string"}}}
    assert to_oas30(flat) == flat
