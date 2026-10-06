"""
E2E: a tool whose parameters are Pydantic models / Literals keeps that structure
from insert (``/tools/add`` and ``/skills/import-anthropic``) through the vMCP
server's ``list_tools``, and its nested arguments reach the tool on a call.
"""

import asyncio
import io
import json
import zipfile

import httpx
import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client

BASE_URL = "http://localhost:8000"

TOOL_NAME = "e2e_rich_book"
SKILL_NAME = "e2e_rich_schema_skill"
VMCP_NAME = "e2e_rich_schema_vmcp"

TOOL_CODE = f'''
import json
from typing import List, Literal
from pydantic import BaseModel, Field

CabinClass = Literal["business", "economy", "basic_economy"]

class Passenger(BaseModel):
    first_name: str = Field(description="Passenger's first name")
    last_name: str = Field(description="Passenger's last name")

def {TOOL_NAME}(cabin: CabinClass, passengers: List[Passenger | dict]):
    """
    Book seats for passengers.

    Args:
        cabin: The cabin class.
        passengers: The passengers to book.
    """
    names = [p["first_name"] + " " + p["last_name"] for p in passengers]
    return cabin + ":" + ",".join(names)
'''.encode()


def _passenger_schema(prop):
    return next(s for s in prop["items"]["anyOf"] if s.get("title") == "Passenger")


def _assert_rich(params):
    props = params["properties"]
    assert props["cabin"]["enum"] == ["business", "economy", "basic_economy"]
    assert props["cabin"]["description"] == "The cabin class."
    passenger = _passenger_schema(props["passengers"])
    assert passenger["required"] == ["first_name", "last_name"]
    assert "$ref" not in json.dumps(params)


@pytest.mark.asyncio
async def test_rich_schema_from_tools_add_through_vmcp(run_sbs):
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{BASE_URL}/tools/add",
                params={"update": "true"},
                files={"tool": (f"{TOOL_NAME}.py", TOOL_CODE, "text/x-python")},
            )
            assert resp.status_code == 200, resp.text
            tool_uuid = resp.json()["uuid"]

            stored = (await client.get(f"{BASE_URL}/tools/{TOOL_NAME}", params={"fields": "full"})).json()
            _assert_rich(stored["params"])

            resp = await client.post(
                f"{BASE_URL}/skills/",
                params={"name": SKILL_NAME, "description": "rich schema", "tool_uuids": [tool_uuid]},
            )
            assert resp.status_code == 200, resp.text
            resp = await client.post(
                f"{BASE_URL}/vmcp_servers/",
                params={"name": VMCP_NAME, "description": "rich schema", "skill_uuid": resp.json()["uuid"]},
            )
            assert resp.status_code == 200, resp.text
            port = resp.json()["port"]
            await asyncio.sleep(3)

            async with asyncio.timeout(30):
                async with sse_client(f"http://127.0.0.1:{port}/sse") as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = {t.name: t for t in (await session.list_tools()).tools}
                        _assert_rich(tools[TOOL_NAME].inputSchema)
                        assert "optional" not in tools[TOOL_NAME].inputSchema

                        result = await session.call_tool(
                            TOOL_NAME,
                            {
                                "cabin": "economy",
                                "passengers": [
                                    {"first_name": "Ada", "last_name": "Lovelace"},
                                    {"first_name": "Alan", "last_name": "Turing"},
                                ],
                            },
                        )
                        text = result.content[0].text
                        assert "economy:Ada Lovelace,Alan Turing" in text, text
        finally:
            await client.delete(f"{BASE_URL}/vmcp_servers/{VMCP_NAME}")
            await client.delete(f"{BASE_URL}/skills/{SKILL_NAME}")
            await client.delete(f"{BASE_URL}/tools/{TOOL_NAME}")


@pytest.mark.asyncio
async def test_rich_schema_from_anthropic_import(run_sbs):
    skill_dir = "e2e_rich_import_skill"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{skill_dir}/SKILL.md", f"---\nname: {skill_dir}\ndescription: rich schema import\n---\n# Rich\n")
        zf.writestr(f"{skill_dir}/scripts/{TOOL_NAME}.py", TOOL_CODE)

    async with httpx.AsyncClient(timeout=60.0) as client:
        skill_name = None
        try:
            resp = await client.post(
                f"{BASE_URL}/skills/import-anthropic",
                files={"zip_file": ("s.zip", buf.getvalue(), "application/zip")},
                data={"source_type": "zip", "snippet_mode": "file"},
            )
            assert resp.status_code == 200, resp.text
            skill_name = resp.json()["skill_name"]

            stored = (await client.get(f"{BASE_URL}/tools/{TOOL_NAME}", params={"fields": "full"})).json()
            _assert_rich(stored["params"])
        finally:
            if skill_name:
                await client.delete(f"{BASE_URL}/skills/{skill_name}")
            await client.delete(f"{BASE_URL}/tools/{TOOL_NAME}")
