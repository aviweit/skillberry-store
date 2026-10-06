# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0

"""Tests for deriving tool params from Python type annotations (Pydantic, Literal)."""

import pytest

from skillberry_store.tools.anthropic.code_parser import (
    parse_code_file,
    parse_python_function,
)
from skillberry_store.utils.annotation_schema import (
    annotation_types,
    build_signature_module,
    derive_params_schema,
    inline_refs,
    needs_rich_schema,
)

AIRLINE_MODULE = '''
from typing import List, Literal, Optional
from pydantic import BaseModel, Field
from functions import env_update_reservation_flights  # not importable here

CabinClass = Literal["business", "economy", "basic_economy"]
DB = open("/nonexistent/side-effect")  # must never be evaluated

class FlightInfo(BaseModel):
    flight_number: str = Field(description="Flight number, such as 'HAT001'.")
    date: str = Field(description="The date for the flight, such as '2024-05-01'.")

class Passenger(BaseModel):
    first_name: str = Field(description="Passenger's first name")
    dob: str = Field(description="Date of birth in YYYY-MM-DD format")

def update_reservation_flights(
    reservation_id: str,
    cabin: CabinClass,
    flights: List[FlightInfo | dict],
    passengers: Optional[List[Passenger]] = None,
):
    """
    Update the flight information of a reservation.

    Args:
        reservation_id: The reservation ID, such as 'ZFA04Y'.
        cabin: The cabin class of the reservation
        flights: An array of objects containing details about each flight.
        passengers: The passengers, if they change.
    """
    raise SystemExit("the tool body must never run")
'''


def _flight_info_schema(items):
    return next(s for s in items["anyOf"] if s.get("title") == "FlightInfo")


class TestNeedsRichSchema:
    def test_plain_signature_is_not_rich(self):
        src = "from typing import List, Optional\ndef f(a: str, b: List[int], c: Optional[dict] = None):\n    pass\n"
        assert needs_rich_schema(src, "f") is False

    def test_literal_is_rich(self):
        src = "from typing import Literal\ndef f(a: Literal['x', 'y']):\n    pass\n"
        assert needs_rich_schema(src, "f") is True

    def test_module_defined_model_is_rich(self):
        assert needs_rich_schema(AIRLINE_MODULE, "update_reservation_flights") is True

    def test_string_forward_reference_is_rich(self):
        src = "from pydantic import BaseModel\nclass P(BaseModel):\n    x: int\ndef f(p: 'P'):\n    pass\n"
        assert needs_rich_schema(src, "f") is True

    def test_missing_function_or_bad_syntax(self):
        assert needs_rich_schema(AIRLINE_MODULE, "nope") is False
        assert needs_rich_schema("def f(:\n", "f") is False


class TestBuildSignatureModule:
    def test_keeps_only_what_the_signature_references(self):
        reduced = build_signature_module(AIRLINE_MODULE, "update_reservation_flights")
        assert "class FlightInfo" in reduced
        assert "class Passenger" in reduced
        assert "CabinClass = Literal" in reduced
        assert "from pydantic import" in reduced
        assert "open(" not in reduced
        assert "from functions import" not in reduced
        assert "SystemExit" not in reduced  # body replaced by `...`

    def test_follows_transitive_class_dependencies(self):
        src = (
            "from pydantic import BaseModel\n"
            "class Inner(BaseModel):\n    x: int\n"
            "class Outer(BaseModel):\n    inner: Inner\n"
            "def f(o: Outer):\n    pass\n"
        )
        reduced = build_signature_module(src, "f")
        assert "class Inner" in reduced and "class Outer" in reduced


class TestInlineRefs:
    def test_inlines_nested_refs_and_drops_defs(self):
        schema = {
            "$defs": {"A": {"type": "object", "properties": {"b": {"$ref": "#/$defs/B"}}},
                      "B": {"type": "string"}},
            "properties": {"a": {"$ref": "#/$defs/A", "description": "an A"}},
        }
        out = inline_refs(schema, schema["$defs"])
        assert "$defs" not in out
        assert out["properties"]["a"] == {
            "type": "object", "properties": {"b": {"type": "string"}}, "description": "an A",
        }

    def test_recursive_model_raises(self):
        defs = {"N": {"type": "object", "properties": {"next": {"$ref": "#/$defs/N"}}}}
        with pytest.raises(ValueError):
            inline_refs({"$ref": "#/$defs/N"}, defs)


class TestDeriveParamsSchema:
    def test_pydantic_and_literal_parameters(self):
        schema = derive_params_schema(
            AIRLINE_MODULE,
            "update_reservation_flights",
            {"cabin": "The cabin class of the reservation"},
        )
        props = schema["properties"]
        assert schema["type"] == "object"
        assert schema["required"] == ["reservation_id", "cabin", "flights"]
        assert props["cabin"]["enum"] == ["business", "economy", "basic_economy"]
        assert props["cabin"]["description"] == "The cabin class of the reservation"
        assert props["flights"]["type"] == "array"
        flight = _flight_info_schema(props["flights"]["items"])
        assert flight["required"] == ["flight_number", "date"]
        assert flight["properties"]["flight_number"]["description"].startswith("Flight number")
        passengers = props["passengers"]
        assert "type" not in passengers and "anyOf" in passengers  # Optional[...]
        assert passengers["default"] is None
        assert "$ref" not in str(schema) and "$defs" not in str(schema)

    def test_accepts_bytes(self):
        schema = derive_params_schema(AIRLINE_MODULE.encode(), "update_reservation_flights")
        assert schema is not None

    def test_plain_signature_returns_none(self):
        assert derive_params_schema("def f(a: str, b: int = 1):\n    pass\n", "f") is None

    def test_unresolvable_type_returns_none(self):
        src = "from typing import Literal\nfrom nowhere import Missing\ndef f(a: Missing, b: Literal['x']):\n    pass\n"
        assert derive_params_schema(src, "f") is None

    def test_recursive_model_returns_none(self):
        src = (
            "from typing import Optional\nfrom pydantic import BaseModel\n"
            "class Node(BaseModel):\n    next: Optional['Node'] = None\n"
            "def f(n: Node):\n    pass\n"
        )
        assert derive_params_schema(src, "f") is None

    def test_skips_self_and_cls(self):
        src = "from typing import Literal\ndef f(self, a: Literal['x']):\n    pass\n"
        assert list(derive_params_schema(src, "f")["properties"]) == ["a"]


class TestCodeParserIntegration:
    def test_parse_python_function_without_module_is_unchanged(self):
        _, params, _ = parse_python_function(AIRLINE_MODULE, "update_reservation_flights")
        assert params["properties"]["flights"] == {
            "type": "array",
            "description": "An array of objects containing details about each flight.",
        }

    def test_parse_code_file_derives_from_module(self):
        tools = parse_code_file(AIRLINE_MODULE, "urf.py", "scripts/urf.py", "airline")
        assert [t.name for t in tools] == ["update_reservation_flights"]
        props = tools[0].params["properties"]
        assert props["cabin"]["enum"] == ["business", "economy", "basic_economy"]
        # docstring descriptions are kept, including the parser's fallback text
        assert props["reservation_id"]["description"] == "The reservation ID, such as 'ZFA04Y'."
        assert _flight_info_schema(props["flights"]["items"])["title"] == "FlightInfo"

    def test_parse_code_file_plain_module_is_unchanged(self):
        src = 'def add(a: int, b: int = 2):\n    """Add.\n\n    Args:\n        a: first\n        b: second\n    """\n    return a + b\n'
        params = parse_code_file(src, "add.py", "scripts/add.py", "math")[0].params
        assert params == {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "first"},
                "b": {"type": "integer", "description": "second"},
            },
            "required": ["a"],
        }

    def test_multi_function_module_uses_classes_for_each_tool(self):
        src = AIRLINE_MODULE + (
            "\ndef get_cabin(cabin: CabinClass):\n"
            '    """Echo a cabin.\n\n    Args:\n        cabin: the cabin\n    """\n'
            "    return cabin\n"
        )
        tools = {t.name: t for t in parse_code_file(src, "m.py", "scripts/m.py", "airline")}
        assert set(tools) == {"update_reservation_flights", "get_cabin"}
        assert tools["get_cabin"].params["properties"]["cabin"]["enum"][0] == "business"


class TestTypeFallbacks:
    def test_annotation_types(self):
        src = "def f(self, a: int, b, *, c: List[str]):\n    pass\n"
        assert annotation_types(src, "f") == {"a": "int", "c": "List[str]"}
        assert annotation_types(b"def f(a: int): pass\n", "f") == {"a": "int"}
        assert annotation_types("def f(:", "f") == {}

    def test_docstring_type_fills_missing_annotation(self):
        src = (
            "def f(n, m: str, k, o):\n"
            '    """Do it.\n\n    Args:\n'
            "        n (int): A number.\n"
            "        m (int): Annotation wins.\n"
            "        k (List[str], optional): Some names.\n"
            "        o: No type anywhere.\n"
            '    """\n'
        )
        _, params, _ = parse_python_function(src, "f")
        types = {k: v["type"] for k, v in params["properties"].items()}
        assert types == {"n": "integer", "m": "string", "k": "array", "o": "string"}
