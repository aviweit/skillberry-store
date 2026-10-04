"""Business logic for admin operations (purge, backup, restore, metrics, health)."""

from __future__ import annotations

import io
import json
import logging
import os
import time
import zipfile
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict

import httpx
from fastapi import HTTPException

from skillberry_store.tools.configure import _default_sbs_dir

if TYPE_CHECKING:
    from skillberry_store.modules.vmcp_server_manager import VirtualMcpServerManager
    from skillberry_store.modules.vnfs_server_manager import VirtualNfsServerManager

logger = logging.getLogger(__name__)

PROMETHEUS_METRICS_PORT = int(os.getenv("PROMETHEUS_METRICS_PORT", 8090))

# Startup stages reported by ``GET /health``. Deliberately two values, not a
# richer taxonomy: this vocabulary is consumed by an external platform health
# check and by the UI, so it has to stay small and stable.
#
#   initializing -- the process serves HTTP, but at least one startup gate is
#                   still open. Normal and transient; NOT a failure.
#   operational  -- every gate is closed; semantic search and content requests
#                   are served in full.
STAGE_INITIALIZING = "initializing"
STAGE_OPERATIONAL = "operational"

# Object types whose description store has to exist before the store can answer
# content requests.
_READINESS_OBJECT_TYPES = ("tool", "snippet", "skill", "vmcp", "vnfs")


class AdminService:
    """Service layer for admin operations.

    Provides business logic for purge, backup, restore, metrics proxy, and
    health/readiness checks. All FastAPI-specific concerns (request parsing,
    response construction) remain in the API layer.

    Attributes:
        vmcp_server_manager: Runtime manager for virtual MCP servers.
        vnfs_server_manager: Runtime manager for virtual NFS servers.
    """

    def __init__(
        self,
        vmcp_server_manager: "VirtualMcpServerManager | None" = None,
        vnfs_server_manager: "VirtualNfsServerManager | None" = None,
        started_at: float | None = None,
    ) -> None:
        self.vmcp_server_manager = vmcp_server_manager
        self.vnfs_server_manager = vnfs_server_manager
        # Monotonic, so the reported uptime cannot go backwards when the host
        # clock is stepped. Constructed inside ``SBS.__init__``, which is the
        # closest thing to "process start" this layer can observe; injectable so
        # a test can assert the field without sleeping.
        self._started_at = time.monotonic() if started_at is None else started_at

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def get_metrics(self) -> str:
        """Proxy-fetch Prometheus metrics from the local metrics server.

        Returns:
            Raw metrics text.

        Raises:
            HTTPException: 503 if the metrics server is unreachable or returns
                a non-200 status.
        """
        metrics_url = f"http://localhost:{PROMETHEUS_METRICS_PORT}/metrics"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(metrics_url, timeout=5.0)
                if response.status_code == 200:
                    return response.text
                raise HTTPException(
                    status_code=503,
                    detail=f"Metrics server returned status {response.status_code}",
                )
        except httpx.ConnectError:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Cannot connect to metrics server on port {PROMETHEUS_METRICS_PORT}. "
                    f"Ensure PROMETHEUS_METRICS_PORT environment variable is set correctly."
                ),
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching metrics: {e}")
            raise HTTPException(
                status_code=503, detail=f"Error fetching metrics: {str(e)}"
            )

    # ------------------------------------------------------------------
    # Purge
    # ------------------------------------------------------------------

    def purge_all(self) -> Dict[str, Any]:
        """Delete all backend data and stop all running servers.

        Steps:
        1. Stop all running VMCP servers.
        2. Stop all running vNFS servers.
        3. Purge all ObjectHandler data (tools, snippets, skills, vmcp, vnfs).

        Returns:
            dict: Summary of deleted object types and server counts.

        Raises:
            HTTPException: 500 if any object type failed to purge.
        """
        logger.warning("Admin purge-all called - deleting all backend data")

        from skillberry_store.modules.object_handler import get_object_handler

        # Step 1: Stop VMCP servers
        vmcp_stopped = False
        vmcp_servers_count = 0
        if self.vmcp_server_manager is not None:
            try:
                vmcp_handler = get_object_handler("vmcp")
                for vmcp_obj in vmcp_handler.iter_dicts():
                    name = vmcp_obj.get("name", "unknown")
                    uuid = vmcp_obj.get("uuid", "unknown")
                    try:
                        if name != "unknown" and uuid != "unknown":
                            self.vmcp_server_manager.remove_server(name, uuid)
                            logger.info(f"Stopped and removed VMCP server: {name} ({uuid})")
                            vmcp_servers_count += 1
                        else:
                            logger.warning(f"VMCP object missing name or uuid: {vmcp_obj}")
                    except Exception as e:
                        logger.warning(f"Failed to stop VMCP server {name}: {str(e)}")
                vmcp_stopped = True
                logger.info(f"All {vmcp_servers_count} VMCP servers stopped and removed")
            except Exception as e:
                logger.warning(f"Failed to stop VMCP servers: {str(e)}")
        else:
            logger.warning("vmcp_server_manager not available")

        # Step 2: Stop vNFS servers
        vnfs_stopped = False
        vnfs_servers_count = 0
        if self.vnfs_server_manager is not None:
            try:
                vnfs_handler = get_object_handler("vnfs")
                for vnfs_obj in vnfs_handler.iter_dicts():
                    name = vnfs_obj.get("name", "unknown")
                    uuid = vnfs_obj.get("uuid", "unknown")
                    try:
                        if name != "unknown" and uuid != "unknown":
                            self.vnfs_server_manager.remove_server(name, uuid)
                            logger.info(f"Stopped and removed vNFS server: {name} ({uuid})")
                            vnfs_servers_count += 1
                        else:
                            logger.warning(f"vNFS object missing name or uuid: {vnfs_obj}")
                    except Exception as e:
                        logger.warning(f"Failed to stop vNFS server {name}: {str(e)}")
                vnfs_stopped = True
                logger.info(f"All {vnfs_servers_count} vNFS servers stopped and removed")
            except Exception as e:
                logger.warning(f"Failed to stop vNFS servers: {str(e)}")
        else:
            logger.warning("vnfs_server_manager not available")

        # Step 3: Purge all ObjectHandler data
        deleted_dirs = []
        failed_dirs = []
        caches_cleared = False
        try:
            for object_type in ["tool", "snippet", "skill", "vmcp", "vnfs"]:
                try:
                    get_object_handler(object_type).purge_all()
                    deleted_dirs.append(object_type)
                except Exception as e:
                    logger.warning(f"Failed to purge {object_type}: {str(e)}")
                    failed_dirs.append({"name": object_type, "error": str(e)})
            caches_cleared = True
            logger.info("All ObjectHandler data and caches purged")
        except Exception as e:
            logger.warning(f"Failed to purge ObjectHandler data: {str(e)}")

        vector_indexes_reset = caches_cleared

        if failed_dirs:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Some directories failed to delete",
                    "deleted": deleted_dirs,
                    "failed": failed_dirs,
                    "vmcp_servers_stopped": vmcp_stopped,
                    "vector_indexes_reset": vector_indexes_reset,
                },
            )

        logger.info(
            f"Successfully purged all data. Deleted: {deleted_dirs}, "
            f"stopped {vmcp_servers_count} VMCP servers, stopped {vnfs_servers_count} vNFS servers"
        )
        return {
            "message": "All backend data successfully purged",
            "deleted_directories": deleted_dirs,
            "total_deleted": len(deleted_dirs),
            "vmcp_servers_stopped": vmcp_stopped,
            "vmcp_servers_count": vmcp_servers_count,
            "vnfs_servers_stopped": vnfs_stopped,
            "vnfs_servers_count": vnfs_servers_count,
            "caches_cleared": caches_cleared,
            "vector_indexes_reset": vector_indexes_reset,
        }

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    def backup_all(self) -> bytes:
        """Collect all objects and return a compressed JSON backup as ZIP bytes.

        Returns:
            bytes: ZIP-compressed JSON backup.

        Raises:
            HTTPException: 500 if backup creation fails.
        """
        logger.info("Admin backup called - creating backup of all data")

        try:
            from skillberry_store.modules.object_handler import get_object_handler

            backup_data: Dict[str, Any] = {
                "skills": [],
                "tools": [],
                "snippets": [],
                "vmcp_servers": [],
                "vnfs_servers": [],
                "exported_at": datetime.utcnow().isoformat(),
            }

            for skill_dict in get_object_handler("skill").iter_dicts():
                backup_data["skills"].append(skill_dict)

            tool_handler = get_object_handler("tool")
            for tool_dict in tool_handler.iter_dicts():
                tool_uuid = tool_dict.get("uuid")
                module_name = tool_dict.get("module_name")
                if tool_uuid and module_name:
                    try:
                        tool_dict["module_content"] = tool_handler.read_file(
                            tool_uuid, module_name, raw_content=True
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to get module content for tool {tool_dict.get('name')}: {e}"
                        )
                backup_data["tools"].append(tool_dict)

            for snippet_dict in get_object_handler("snippet").iter_dicts():
                backup_data["snippets"].append(snippet_dict)

            for vmcp_dict in get_object_handler("vmcp").iter_dicts():
                backup_data["vmcp_servers"].append(
                    {k: v for k, v in vmcp_dict.items() if k not in ["running", "runtime"]}
                )

            for vnfs_dict in get_object_handler("vnfs").iter_dicts():
                backup_data["vnfs_servers"].append(
                    {k: v for k, v in vnfs_dict.items() if k not in ["running", "export_path"]}
                )

            json_string = json.dumps(backup_data, indent=2)
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                filename = f"skillberry-backup-{datetime.utcnow().strftime('%Y-%m-%d')}.json"
                zip_file.writestr(filename, json_string)
            zip_buffer.seek(0)

            logger.info(
                f"Backup created: {len(backup_data['skills'])} skills, "
                f"{len(backup_data['tools'])} tools, {len(backup_data['snippets'])} snippets, "
                f"{len(backup_data['vmcp_servers'])} VMCP servers, "
                f"{len(backup_data['vnfs_servers'])} vNFS servers"
            )
            return zip_buffer.getvalue()

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Backup failed: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def restore_all(self, backup_bytes: bytes) -> Dict[str, Any]:
        """Restore all data from a raw ZIP backup payload.

        Parses the ZIP, purges existing data, then reimports tools, snippets,
        skills, VMCP servers, and vNFS servers in order.  Reloads all object
        handlers after import.

        Args:
            backup_bytes: Raw bytes of the uploaded ZIP backup file.

        Returns:
            dict: Counts of imported objects.

        Raises:
            HTTPException: 400 for invalid file format, 500 for other errors.
        """
        logger.warning("Admin restore called - restoring all data from backup")

        try:
            with zipfile.ZipFile(io.BytesIO(backup_bytes), "r") as zip_file:
                json_files = [n for n in zip_file.namelist() if n.endswith(".json")]
                if not json_files:
                    raise HTTPException(
                        status_code=400, detail="No JSON file found in the ZIP archive"
                    )
                backup_data = json.loads(zip_file.read(json_files[0]))
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid ZIP file format")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format in backup file")

        if not isinstance(backup_data, dict):
            raise HTTPException(
                status_code=400, detail="Invalid backup file format: expected JSON object"
            )

        # Purge existing data first
        logger.info("Purging existing data before restore...")
        try:
            self.purge_all()
        except Exception as e:
            logger.error(f"Failed to purge data before restore: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to purge existing data before restore: {str(e)}",
            )

        logger.info("Purge completed, starting restore...")

        from skillberry_store.modules.object_handler import (
            get_object_handler,
            reload_object_handlers,
        )

        imported_counts = {
            "tools": 0,
            "snippets": 0,
            "skills": 0,
            "vmcp_servers": 0,
            "vnfs_servers": 0,
        }

        if "tools" in backup_data and isinstance(backup_data["tools"], list):
            tool_handler = get_object_handler("tool")
            for tool_data in backup_data["tools"]:
                try:
                    module_content = tool_data.pop("module_content", None)
                    tool_uuid = tool_data.get("uuid")
                    module_name = tool_data.get("module_name")
                    tool_handler.write_dict(tool_uuid, tool_data)
                    if module_content and tool_uuid and module_name:
                        tool_handler.write_file(
                            tool_uuid,
                            module_name,
                            (
                                module_content.encode("utf-8")
                                if isinstance(module_content, str)
                                else module_content
                            ),
                        )
                    imported_counts["tools"] += 1
                except Exception as e:
                    logger.error(f"Failed to import tool: {e}")

        if "snippets" in backup_data and isinstance(backup_data["snippets"], list):
            snippet_handler = get_object_handler("snippet")
            for snippet_data in backup_data["snippets"]:
                try:
                    snippet_handler.write_dict(snippet_data.get("uuid"), snippet_data)
                    imported_counts["snippets"] += 1
                except Exception as e:
                    logger.error(f"Failed to import snippet: {e}")

        if "skills" in backup_data and isinstance(backup_data["skills"], list):
            skill_handler = get_object_handler("skill")
            for skill_data in backup_data["skills"]:
                try:
                    skill_handler.write_dict(skill_data.get("uuid"), skill_data)
                    imported_counts["skills"] += 1
                except Exception as e:
                    logger.error(f"Failed to import skill: {e}")

        if "vmcp_servers" in backup_data and isinstance(backup_data["vmcp_servers"], list):
            vmcp_handler = get_object_handler("vmcp")
            for vmcp_data in backup_data["vmcp_servers"]:
                try:
                    vmcp_handler.write_dict(vmcp_data.get("uuid"), vmcp_data)
                    if self.vmcp_server_manager and vmcp_data.get("state") == "approved":
                        try:
                            tool_uuids: list = []
                            snippet_uuids: list = []
                            skill_uuid = vmcp_data.get("skill_uuid")
                            if skill_uuid:
                                skill_handler = get_object_handler("skill")
                                try:
                                    skill_dict = skill_handler.read_dict(skill_uuid)
                                    tool_uuids = skill_dict.get("tool_uuids", [])
                                    snippet_uuids = skill_dict.get("snippet_uuids", [])
                                except Exception as e:
                                    logger.warning(f"Error loading skill {skill_uuid} for VMCP server: {e}")
                            server = self.vmcp_server_manager.add_server(
                                name=vmcp_data.get("name", ""),
                                uuid=vmcp_data.get("uuid", ""),
                                description=vmcp_data.get("description", ""),
                                port=vmcp_data.get("port"),
                                tools=tool_uuids,
                                snippets=snippet_uuids,
                                env_id="",
                            )
                            logger.info(f"Started VMCP server '{vmcp_data.get('name')}' on port {server.port}")
                        except Exception as e:
                            logger.warning(f"Failed to start VMCP server '{vmcp_data.get('name')}': {e}")
                    imported_counts["vmcp_servers"] += 1
                except Exception as e:
                    logger.error(f"Failed to import VMCP server: {e}")

        if "vnfs_servers" in backup_data and isinstance(backup_data["vnfs_servers"], list):
            vnfs_handler = get_object_handler("vnfs")
            for vnfs_data in backup_data["vnfs_servers"]:
                try:
                    vnfs_handler.write_dict(vnfs_data.get("uuid"), vnfs_data)
                    if self.vnfs_server_manager and vnfs_data.get("state") == "approved":
                        try:
                            from skillberry_store.schemas.vnfs_schema import VnfsSchema
                            server = self.vnfs_server_manager.add_server(VnfsSchema(**vnfs_data))
                            logger.info(f"Started vNFS server '{vnfs_data.get('name')}' on port {server.port}")
                        except Exception as e:
                            logger.warning(f"Failed to start vNFS server '{vnfs_data.get('name')}': {e}")
                    imported_counts["vnfs_servers"] += 1
                except Exception as e:
                    logger.error(f"Failed to import vNFS server: {e}")

        logger.info("Reloading object handlers from restored data...")
        reload_object_handlers()
        logger.info("Object handlers reloaded")

        logger.info(
            f"Restore completed: {imported_counts['skills']} skills, "
            f"{imported_counts['tools']} tools, {imported_counts['snippets']} snippets, "
            f"{imported_counts['vmcp_servers']} VMCP servers, "
            f"{imported_counts['vnfs_servers']} vNFS servers"
        )
        return {
            "message": "All data successfully restored from backup",
            "imported_counts": imported_counts,
            "total_imported": sum(imported_counts.values()),
        }

    # ------------------------------------------------------------------
    # Health / readiness
    # ------------------------------------------------------------------

    def collect_checks(self, encoder_warmup_done: bool) -> Dict[str, bool]:
        """Every startup gate, by name, as a flat ``name -> closed?`` map.

        Never raises, by contract. ``get_object_handler`` throws ``RuntimeError``
        until ``initialize_object_handlers()`` has run, and a liveness probe that
        propagated that would answer 5xx during exactly the window it exists to
        describe — which is what makes a platform health check tear a booting
        instance down and start the boot over. A gate that cannot be inspected
        yet is reported as still open instead.

        Args:
            encoder_warmup_done: Whether the background semantic-encoder warmup
                has finished. Resolved by the API layer from ``app.state``,
                which is where the task handle lives.

        Returns:
            dict: Gate name to ``True`` when closed (that part of startup is
            done), ``False`` while still pending.
        """
        from skillberry_store.modules.object_handler import get_object_handler

        checks: Dict[str, bool] = {}
        for object_type in _READINESS_OBJECT_TYPES:
            try:
                desc = get_object_handler(object_type).descriptions
            except Exception:
                checks[object_type] = False
                continue
            # A handler with no description store does not hold the gate open —
            # there is nothing to wait for. Pre-existing semantics, kept.
            if desc is not None:
                checks[object_type] = desc.is_ready
        checks["encoder_warmup"] = encoder_warmup_done
        return checks

    def health_report(self, encoder_warmup_done: bool) -> Dict[str, Any]:
        """Liveness payload: alive, plus which startup stage we are in.

        ``status`` is always ``"healthy"``: it answers "should this process be
        restarted?", and the answer is no for as long as it can serve this
        request at all. Boot progress is reported out of band in ``stage`` so a
        caller can distinguish "still booting" from "ready" *without* a non-2xx
        status code standing in for it.

        Args:
            encoder_warmup_done: Whether the background encoder warmup finished.

        Returns:
            dict: ``status``, ``stage``, per-gate ``checks``, ``uptime_seconds``.
        """
        checks = self.collect_checks(encoder_warmup_done)
        stage = STAGE_OPERATIONAL if all(checks.values()) else STAGE_INITIALIZING
        return {
            "status": "healthy",
            "stage": stage,
            "checks": checks,
            "uptime_seconds": round(time.monotonic() - self._started_at, 3),
        }

    def readiness_check(self, encoder_warmup_done: bool) -> Dict[str, Any]:
        """Readiness payload: 200 only once every startup gate is closed.

        Args:
            encoder_warmup_done: Whether the background encoder warmup finished.

        Returns:
            dict: The ``health_report`` payload with ``status`` set to
            ``"ready"``.

        Raises:
            HTTPException: 503 while still initialising, carrying the same
                ``stage``/``checks`` payload in ``detail``. 503 rather than the
                500 this used to raise: a store that has not finished booting is
                temporarily unable to serve, which an orchestrator should act on
                by waiting, not a server fault it should act on by restarting.
                ``Retry-After`` names the poll interval so a client does not
                have to invent one.
        """
        report = self.health_report(encoder_warmup_done)
        if report["stage"] != STAGE_OPERATIONAL:
            raise HTTPException(
                status_code=503,
                detail={**report, "status": STAGE_INITIALIZING},
                headers={"Retry-After": "5"},
            )
        return {**report, "status": "ready"}
