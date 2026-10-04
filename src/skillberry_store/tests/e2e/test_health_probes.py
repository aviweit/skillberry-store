"""End-to-end health probes against a real uvicorn-served store.

The unit tests in ``tests/fast_api/test_health_probes.py`` drive the app object
through ``TestClient``, which bypasses the ASGI server. These go over the wire
to the server the ``run_sbs`` fixture boots, so they cover what the unit tests
cannot: that the routes are actually mounted and reachable from a socket, that
the payload survives real JSON serialisation, and that an unauthenticated
client — which is all Render, a load balancer or ``curl`` ever is — gets through.

``run_sbs`` waits on ``/health/ready``, so by the time these run the store is
operational. The initializing side of the contract is covered in the unit
tests, where the warmup state can be controlled deterministically.
"""

import httpx
import pytest

BASE_URL = "http://localhost:8000"


@pytest.mark.asyncio
async def test_health_answers_200_with_a_stage_over_the_wire(run_sbs):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["stage"] == "operational"
    assert body["checks"]["encoder_warmup"] is True
    assert body["uptime_seconds"] >= 0


@pytest.mark.asyncio
async def test_health_needs_no_credentials(run_sbs):
    """A platform health check has no token to offer, ever.

    ``/health`` is in the ACL's unauthenticated floor precisely so that Render,
    a load balancer and ``curl`` can reach it before anyone can log in. Asserted
    from the wire with no ``Authorization`` header at all.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/health", headers={})

    assert response.status_code == 200
    assert "authorization" not in {k.lower() for k in response.request.headers}


@pytest.mark.asyncio
async def test_ready_answers_200_once_the_store_is_operational(run_sbs):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["stage"] == "operational"


@pytest.mark.asyncio
async def test_both_probes_report_the_same_checks(run_sbs):
    """A caller may poll either endpoint and must reach the same conclusion."""
    async with httpx.AsyncClient() as client:
        health = (await client.get(f"{BASE_URL}/health")).json()
        ready = (await client.get(f"{BASE_URL}/health/ready")).json()

    assert health["checks"] == ready["checks"]
    assert health["stage"] == ready["stage"]


@pytest.mark.asyncio
async def test_health_is_cheap_enough_to_poll(run_sbs):
    """Render polls its health check path continuously, forever.

    The handler does filesystem ``exists`` checks and reads a task flag — no
    embedding, no index load, no subprocess. If that ever changes, a probe
    interval measured in seconds becomes a standing load on the store.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/health")

    assert response.status_code == 200
    assert response.elapsed.total_seconds() < 2.0
