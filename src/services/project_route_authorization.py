"""HTTP enforcement for project-scoped Chunk routes.

Only an authenticated Editor service request reaches this guard.  The Editor
passes identity and public-read verification derived from the App-signed
ticket; browser-supplied identity headers never reach Chunk directly.
"""

from __future__ import annotations

import hmac
from typing import Any

from flask import Flask, Response, current_app, jsonify, request


def _header_bool(name: str) -> bool:
    return str(request.headers.get(name) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _operation() -> str:
    blueprint = str(request.blueprint or "").lower()
    method = request.method.upper()
    if blueprint == "projects":
        return "project.read" if method in {"GET", "HEAD"} else "project.manage"
    if blueprint == "project_access":
        return "access.manage"
    if blueprint == "worlds":
        return "world.read" if method in {"GET", "HEAD"} else "world.mutate"
    if blueprint == "blocks":
        return "blocks.read"
    if blueprint == "chunks":
        allow_generated = _header_bool("X-Vectoplan-Allow-Materialize")
        if not allow_generated:
            allow_generated = _header_bool("X-Vectoplan-Can-Materialize") and (
                str(request.args.get("allowGenerated") or "").lower() in {"1", "true", "yes"}
                or bool((request.get_json(silent=True) or {}).get("allowGenerated"))
            )
        if allow_generated:
            return "chunks.materialize"
        return "chunks.read" if method in {"GET", "HEAD"} else "chunks.batch.read"
    if blueprint == "commands":
        return "commands.execute"
    return ""


def _error(
    code: str,
    message: str,
    status_code: int,
    *,
    details: dict[str, Any] | None = None,
) -> Response:
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                },
            }
        ),
        status_code,
    )


def install_project_route_authorization(app: Flask) -> bool:
    if getattr(app, "_vectoplan_project_route_authorization_installed", False):
        return True

    @app.before_request
    def _authorize_project_route() -> Response | None:
        project_id = str((request.view_args or {}).get("project_id") or "").strip()
        operation = _operation()
        if not project_id or not operation:
            return None

        from extensions import db
        from models.project import Project
        from models.project_access_assignment import ProjectAccessAssignment
        from src.services.project_access_service import authorize_project_operation
        from src.services.service_auth_service import get_current_service_principal

        principal = get_current_service_principal(None)
        service_id = str(getattr(principal, "service_id", "") or "")

        # App owns provisioning and access projection. Runtime data access is
        # always performed by Editor and is checked below.
        if service_id == "vectoplan-app":
            return None
        if request.blueprint == "project_access":
            return _error(
                "project_access_owned_by_app",
                "Only vectoplan-app may read or mutate the project access projection.",
                403,
            )
        if (
            request.blueprint == "projects"
            and request.method.upper() not in {"GET", "HEAD"}
        ):
            return _error(
                "project_lifecycle_owned_by_app",
                "Only vectoplan-app may mutate the Chunk project lifecycle.",
                403,
            )

        if service_id != "vectoplan-editor":
            return _error(
                "service_not_allowed_for_project_runtime",
                "This service is not allowed to access project runtime routes.",
                403,
            )

        claimed_chunk_id = str(
            request.headers.get("X-Vectoplan-Chunk-Project-Id") or ""
        ).strip()
        if not claimed_chunk_id or not hmac.compare_digest(claimed_chunk_id, project_id):
            return _error(
                "chunk_project_binding_mismatch",
                "The requested Chunk project does not match the verified Editor context.",
                403,
            )

        claimed_app_id = str(
            request.headers.get("X-Vectoplan-App-Project-Id") or ""
        ).strip()
        # Only the App/Chunk binding is needed here. Loading a full Project ORM
        # entity causes its many select-in backrefs (worlds, snapshots, events,
        # object refs, ...) to be traversed before every runtime request. Once a
        # chunk is materialized this can make commands appear to hang. Selecting
        # the scalar column keeps authorization constant-time and relationship-free.
        stored_app_id = str(
            db.session.query(Project.external_app_project_id)
            .filter(Project.project_id == project_id)
            .limit(1)
            .scalar()
            or ""
        ).strip()
        if (
            not claimed_app_id
            or not stored_app_id
            or not hmac.compare_digest(claimed_app_id, stored_app_id)
        ):
            return _error(
                "app_chunk_project_binding_mismatch",
                "The App and Chunk project ids are not linked to the same project.",
                403,
            )

        decision = authorize_project_operation(
            project_id,
            operation,
            auth_user_id=request.headers.get("X-Vectoplan-Auth-User-Id", ""),
            session=db.session,
            assignment_model=ProjectAccessAssignment,
            project_model=Project,
            principal=principal,
            public=_header_bool("X-Vectoplan-Public-Access"),
            public_read_only_verified=_header_bool(
                "X-Vectoplan-Public-Read-Only-Verified"
            ),
            request_id=request.headers.get("X-Vectoplan-Chunk-Request-Id", ""),
            config=current_app.config,
        )
        if decision.allowed:
            return None
        return _error(
            decision.code,
            decision.reason or "Project operation denied.",
            decision.status_code,
            details=decision.to_dict(include_private=False),
        )

    setattr(app, "_vectoplan_project_route_authorization_installed", True)
    return True


__all__ = ["install_project_route_authorization"]
