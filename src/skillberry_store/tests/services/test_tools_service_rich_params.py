# Copyright 2025 IBM Corp.
# Licensed under the Apache License, Version 2.0

"""ToolsService.add_from_python derives params from Pydantic/Literal annotations."""

from unittest.mock import MagicMock, patch

from skillberry_store.services.tools_service import ToolsService

RICH = b'''
from typing import List, Literal
from pydantic import BaseModel, Field

class FlightInfo(BaseModel):
    flight_number: str = Field(description="Flight number, such as 'HAT001'.")
    date: str = Field(description="The date for the flight.")

def update_reservation_flights(
    reservation_id: str,
    cabin: Literal["business", "economy", "basic_economy"],
    flights: List[FlightInfo | dict],
    payment_id: str = "",
):
    """
    Update the flight information of a reservation.

    Args:
        reservation_id: The reservation ID, such as 'ZFA04Y'.
        cabin: The cabin class of the reservation
        flights: An array of objects containing details about each flight.
        payment_id: The payment id stored in user profile.
    """
    return reservation_id
'''

PLAIN = b'''
def add(a: int, b: int):
    """
    Add two numbers.

    Args:
        a (int): first
        b (int): second
    """
    return a + b
'''


def _service():
    handler = MagicMock()
    handler.lookup_by_name.return_value = None
    svc = ToolsService(handler)
    svc.create = MagicMock(
        side_effect=lambda data, **kw: {"name": data["name"], "uuid": "u1", "module_name": kw["module_filename"]}
    )
    return svc


def test_add_from_python_stores_rich_params():
    svc = _service()
    with patch("skillberry_store.services.tools_service.add_tool_from_python_counter"):
        svc.add_from_python(RICH, "urf.py", "update_reservation_flights")
    params = svc.create.call_args.args[0]["params"]
    props = params["properties"]
    assert props["cabin"]["enum"] == ["business", "economy", "basic_economy"]
    assert props["cabin"]["description"] == "The cabin class of the reservation"
    flight = next(s for s in props["flights"]["items"]["anyOf"] if s.get("title") == "FlightInfo")
    assert flight["properties"]["date"]["description"] == "The date for the flight."
    assert params["required"] == ["reservation_id", "cabin", "flights"]
    assert params["optional"] == ["payment_id"]


def test_add_from_python_plain_params_unchanged():
    svc = _service()
    with patch("skillberry_store.services.tools_service.add_tool_from_python_counter"):
        svc.add_from_python(PLAIN, "add.py", "add")
    assert svc.create.call_args.args[0]["params"] == {
        "type": "object",
        "properties": {
            "a": {"type": "int", "description": "first"},
            "b": {"type": "int", "description": "second"},
        },
        "required": ["a", "b"],
        "optional": [],
    }


HINT_ONLY = b'''
def scale(n: int, factor):
    """
    Scale a number.

    Args:
        n: the number
        factor (float): the factor
    """
    return n * factor
'''


def test_add_from_python_hint_fills_missing_docstring_type():
    svc = _service()
    with patch("skillberry_store.services.tools_service.add_tool_from_python_counter"):
        svc.add_from_python(HINT_ONLY, "scale.py", "scale")
    props = svc.create.call_args.args[0]["params"]["properties"]
    assert props["n"]["type"] == "int"  # from the hint
    assert props["factor"]["type"] == "float"  # from the docstring
