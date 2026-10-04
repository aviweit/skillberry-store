"""Admin API endpoints for the Skillberry Store service."""

import logging
from typing import Optional

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import PlainTextResponse, StreamingResponse

from skillberry_store.access_control.decorator import requires
from skillberry_store.services.admin_service import AdminService

logger = logging.getLogger(__name__)


def _encoder_warmup_done(app: FastAPI) -> bool:
    """Whether the background semantic-encoder warmup has finished.

    The task handle is put on ``app.state`` by the lifespan hook. Its absence
    means no warmup was ever scheduled — a bare ``TestClient(app)`` used
    without its context manager, or an embedding of the app that does not run
    startup — which reads as "nothing to wait for", not "never ready".

    Args:
        app: The FastAPI application serving the request.

    Returns:
        bool: True when there is no warmup pending.
    """
    task = getattr(getattr(app, "state", None), "encoder_warmup_task", None)
    return task is None or task.done()


def register_admin_api(
    app: FastAPI,
    tags: str = "admin",
    service: Optional[AdminService] = None,
):
    """Register admin API endpoints with the FastAPI application.

    Args:
        app: The FastAPI application instance.
        tags: FastAPI tags for grouping the endpoints in documentation.
        service: Optional AdminService instance.  When ``None``, a new instance
            is created using the server managers stored in ``app.state``.
    """
    if service is None:
        vmcp_manager = getattr(getattr(app, "state", None), "vmcp_server_manager", None)
        vnfs_manager = getattr(getattr(app, "state", None), "vnfs_server_manager", None)
        service = AdminService(
            vmcp_server_manager=vmcp_manager,
            vnfs_server_manager=vnfs_manager,
        )

    @app.get(
        "/admin/metrics",
        tags=[tags],
        response_class=PlainTextResponse,
        openapi_extra={"x-cli-name": "metrics"},
    )
    async def get_metrics():
        """Proxy endpoint to fetch Prometheus metrics.

        This endpoint proxies requests to the Prometheus metrics server
        to avoid CORS issues when accessing metrics from the UI.

        Returns:
            PlainTextResponse: The raw Prometheus metrics in text format.

        Raises:
            HTTPException: If metrics server is not accessible (503).
        """
        text = await service.get_metrics()
        return PlainTextResponse(content=text, media_type="text/plain")

    @requires("admin", "admin")
    @app.delete(
        "/admin/purge-all", tags=[tags], openapi_extra={"x-cli-name": "purge-all"}
    )
    async def purge_all_data():
        """Delete all backend components including skills, tools, snippets, VMCP servers, and their descriptions.

        This endpoint performs a hard delete by:
        1. Stopping all running VMCP servers
        2. Clearing VMCP servers persistent storage
        3. Removing all data directories
        4. Recreating empty directories
        5. Resetting in-memory vector indexes

        Use with caution as this operation is irreversible.

        Returns:
            dict: Success message with details of deleted directories.

        Raises:
            HTTPException: If deletion fails (500 status code).
        """
        return service.purge_all()

    @requires("admin", "admin")
    @app.get(
        "/admin/backup",
        tags=[tags],
        openapi_extra={"x-cli-name": "backup"},
        response_class=StreamingResponse,
    )
    async def backup_all_data():
        """Create a backup of all data (skills, tools, snippets, VMCP servers, vNFS servers).

        Returns a compressed JSON file (.json.zip) containing all data.
        The UI should download this file directly.

        Returns:
            StreamingResponse: A ZIP file containing the backup JSON.

        Raises:
            HTTPException: If backup creation fails (500 status code).
        """
        from datetime import datetime

        zip_bytes = service.backup_all()
        return StreamingResponse(
            iter([zip_bytes]),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=skillberry-backup-"
                    f"{datetime.utcnow().strftime('%Y-%m-%d')}.json.zip"
                )
            },
        )

    @requires("admin", "admin")
    @app.post(
        "/admin/restore",
        tags=[tags],
        openapi_extra={"x-cli-name": "restore"},
    )
    async def restore_all_data(backup_file: UploadFile = File(...)):
        """Restore all data from a backup file.

        This endpoint:
        1. Purges all existing data (calls purge_all_data internally)
        2. Restores data from the uploaded backup file
        3. Imports in order: tools, snippets, skills, VMCP servers, vNFS servers
        4. Starts VMCP/vNFS servers that are in approved state
        5. Rebuilds caches and description indexes

        Args:
            backup_file: The backup ZIP file to restore from.

        Returns:
            dict: Success message with counts of restored items.

        Raises:
            HTTPException: If restore fails (400 or 500 status code).
        """
        content = await backup_file.read()
        return service.restore_all(content)

    @app.get(
        "/health",
        tags=[tags],
        openapi_extra={"x-cli-name": "health"},
    )
    def health_check(request: Request):
        """Liveness probe — 200 for as long as the process can serve HTTP.

        This is the endpoint an external platform health check should point at:
        Render's health check path, a Kubernetes ``livenessProbe``, an ALB
        target group. It answers 200 from the moment the server binds — *while*
        startup work is still running — and it never raises. A probe that
        returns 5xx during a normal boot is indistinguishable from a crash, so
        the platform restarts the instance, and the boot it interrupted never
        gets to finish. That restart loop is the failure this shape exists to
        prevent.

        Progress is reported in the body rather than through the status code:

        - ``stage``: ``initializing`` while any startup gate is still open,
          ``operational`` once they are all closed.
        - ``checks``: the individual gates, so a slow one can be named.
        - ``uptime_seconds``: how long this process has been up.

        A client that needs semantic search (the UI) waits for
        ``stage == "operational"``. A client that only needs to know the process
        is alive reads nothing but the status code. ``/health/ready`` keeps the
        stricter, traffic-gating contract.

        Returns:
            dict: ``status`` (always ``"healthy"``), ``stage``, ``checks``,
            ``uptime_seconds`` — always with HTTP 200.
        """
        return service.health_report(_encoder_warmup_done(request.app))

    @requires("admin", "list")
    @app.get("/changes", tags=[tags], openapi_extra={"x-cli-name": "changes-count"})
    def get_changes_count():
        """Get the global mutation counter for detecting data changes.

        Returns a counter that increments whenever data is modified (create, update, delete).
        The UI uses this to detect when to refresh data without polling individual endpoints.

        Args:
            None.

        Returns:
            dict: Contains 'count' key with the current mutation counter value.
        """
        from skillberry_store.fast_api.changes import get as get_count

        return {"count": get_count()}

    @app.get(
        "/health/ready",
        tags=[tags],
        openapi_extra={"x-cli-name": "health-ready"},
    )
    def readiness_check(request: Request):
        """Readiness probe — 200 only once the store can serve content requests.

        Gates on the description stores and on the semantic encoder warmup.
        Without the latter, the first request that triggers an embedding races
        the background warmup and both pay the model cold-load (download + onnx
        session build) at once.

        Use this to gate *traffic*: a Kubernetes ``readinessProbe``, a load
        balancer, a test harness waiting before it starts asserting. Do NOT use
        it as a platform health check — it is non-2xx by design during a normal
        boot, and a platform that restarts on that will never let the boot
        finish. Point those at ``/health``, which reports the same ``stage`` and
        ``checks`` with a 200.

        Returns:
            dict: ``status`` (``"ready"``), ``stage``, ``checks``,
            ``uptime_seconds``.

        Raises:
            HTTPException: 503 while still initializing, with ``Retry-After``
                and the same ``stage``/``checks`` payload under ``detail``.
        """
        return service.readiness_check(_encoder_warmup_done(request.app))
