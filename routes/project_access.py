# services/vectoplan-chunk/routes/project_access.py
"""
Prepared project-access HTTP routes for ``vectoplan-chunk``.

This module exposes project-scoped CRUD-style adapters for the persistent
access structures implemented by ``models/project_access.py`` and orchestrated
by ``src/project_access/service.py``.

The routes deliberately do **not** authenticate callers and do **not** enforce
permissions yet. They preserve the storage and transaction contracts required
for a later authorization layer:

* every row is scoped through the local ``Project.id``;
* external user ids remain plain strings without cross-service foreign keys;
* role/group references must belong to the same project;
* one route call owns one commit/rollback boundary;
* service helpers may flush, but never commit or rollback;
* system/default roles cannot be edited or deleted through generic CRUD routes;
* the Owner role may only be assigned to the current project owner;
* no chunks, snapshots, commands, events or object graphs are loaded here;
* no schema creation, migration or default bootstrap is executed here.

Prepared route surface
----------------------

Readiness and summary::

    GET  /project-access/_status
    GET  /projects/<project_id>/access
    PUT  /projects/<project_id>/access/initialize

Roles::

    GET    /projects/<project_id>/roles
    POST   /projects/<project_id>/roles
    GET    /projects/<project_id>/roles/<role_ref>
    PUT    /projects/<project_id>/roles/<role_ref>
    PATCH  /projects/<project_id>/roles/<role_ref>
    DELETE /projects/<project_id>/roles/<role_ref>

Groups and memberships::

    GET    /projects/<project_id>/groups
    POST   /projects/<project_id>/groups
    GET    /projects/<project_id>/groups/<group_ref>
    PUT    /projects/<project_id>/groups/<group_ref>
    PATCH  /projects/<project_id>/groups/<group_ref>
    DELETE /projects/<project_id>/groups/<group_ref>

    GET    /projects/<project_id>/groups/<group_ref>/members
    POST   /projects/<project_id>/groups/<group_ref>/members
    PUT    /projects/<project_id>/groups/<group_ref>/members/<user_id>
    DELETE /projects/<project_id>/groups/<group_ref>/members/<user_id>

Role assignments::

    GET    /projects/<project_id>/assignments
    POST   /projects/<project_id>/assignments
    GET    /projects/<project_id>/assignments/<assignment_id>
    PATCH  /projects/<project_id>/assignments/<assignment_id>
    DELETE /projects/<project_id>/assignments/<assignment_id>

All mutating endpoints are idempotent where the underlying identity is stable.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Final, Optional

try:
    from flask import Blueprint, current_app, jsonify, request
except Exception as exc:  # pragma: no cover - explicit runtime dependency
    raise RuntimeError(
        "Flask is required to import routes/project_access.py."
    ) from exc

try:
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
except Exception:  # pragma: no cover - SQLAlchemy is expected in runtime
    IntegrityError = Exception  # type: ignore[misc, assignment]
    SQLAlchemyError = Exception  # type: ignore[misc, assignment]

from extensions import db
from models import Project
from models.project_access import (
    ASSIGNMENT_STATUS_ACTIVE,
    ASSIGNMENT_STATUS_DELETED,
    ASSIGNMENT_STATUS_INACTIVE,
    ASSIGNMENT_STATUS_REVOKED,
    DEFAULT_PROJECT_ROLE_KEYS,
    DEFAULT_ROLE_OWNER,
    GROUP_STATUS_DELETED,
    MEMBERSHIP_STATUS_DELETED,
    ProjectGroup,
    ProjectGroupMember,
    ProjectRole,
    ProjectRoleAssignment,
    SUBJECT_TYPE_GROUP,
    SUBJECT_TYPE_USER,
    get_project_access_model_contract,
)

try:
    from src.project_access import (
        PROJECT_ACCESS_SERVICE_VERSION,
        EntityMutationResult,
        MutationStats,
        ProjectAccessConflictError,
        ProjectAccessCrossProjectError,
        ProjectAccessInvariantError,
        ProjectAccessNotFoundError,
        ProjectAccessPersistenceError,
        ProjectAccessServiceError,
        ProjectAccessValidationError,
        assign_role_to_group,
        assign_role_to_user,
        build_project_access_summary,
        clear_project_access_service_caches,
        ensure_project_access_initialized,
        ensure_project_group,
        ensure_role_assignment,
        ensure_user_in_group,
        find_project_group,
        find_project_role,
        get_project_access_service_contract,
        list_group_memberships,
        list_project_groups,
        list_project_role_assignments,
        list_project_roles,
        remove_user_from_group,
        resolve_project,
        revoke_role_assignment,
    )
except Exception:  # pragma: no cover - direct service fallback
    from src.project_access.service import (
        PROJECT_ACCESS_SERVICE_VERSION,
        EntityMutationResult,
        MutationStats,
        ProjectAccessConflictError,
        ProjectAccessCrossProjectError,
        ProjectAccessInvariantError,
        ProjectAccessNotFoundError,
        ProjectAccessPersistenceError,
        ProjectAccessServiceError,
        ProjectAccessValidationError,
        assign_role_to_group,
        assign_role_to_user,
        build_project_access_summary,
        clear_project_access_service_caches,
        ensure_project_access_initialized,
        ensure_project_group,
        ensure_role_assignment,
        ensure_user_in_group,
        find_project_group,
        find_project_role,
        get_project_access_service_contract,
        list_group_memberships,
        list_project_groups,
        list_project_role_assignments,
        list_project_roles,
        remove_user_from_group,
        resolve_project,
        revoke_role_assignment,
    )


project_access_bp = Blueprint("project_access", __name__)

ROUTE_MODULE_VERSION: Final[str] = "1.0.1"
ROUTE_SOURCE: Final[str] = "routes.project_access"
PROJECT_ACCESS_ROUTE_RESPONSE_VERSION: Final[str] = (
    "project-access-route-response.v1"
)
PROJECT_ACCESS_ROUTE_ERROR_VERSION: Final[str] = (
    "project-access-route-error.v1"
)
PROJECT_ACCESS_ROUTE_STATUS_VERSION: Final[str] = (
    "project-access-route-status.v1"
)

DEFAULT_PROJECT_ID: Final[str] = "dev-project"
DEFAULT_PROJECT_OWNER_USER_ID: Final[str] = "1"
DEFAULT_LIST_LIMIT: Final[int] = 100
MAX_LIST_LIMIT: Final[int] = 1000

DEFAULT_ROLE_KEY_SET: Final[frozenset[str]] = frozenset(
    DEFAULT_PROJECT_ROLE_KEYS
)

_PROJECT_ALIASES: Final[frozenset[str]] = frozenset(
    {
        "",
        "default",
        "_default",
        "current",
        "_current",
        "dev",
        "_dev",
    }
)

# Route-level CRUD must never make system/default roles drift from the
# canonical templates. The explicit access initializer owns synchronization.
_PROTECTED_ROLE_MUTATION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "title",
        "description",
        "permissions",
        "permissionsJson",
        "permissions_json",
        "isSystem",
        "is_system",
        "status",
        "metadata",
        "metadataJson",
        "metadata_json",
    }
)


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    try:
        return datetime.now(timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    text = _safe_str(value).lower()
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default


def _safe_int(
    value: Any,
    default: int = 0,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        result = int(value)
    except Exception:
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _safe_exception_message(exc: BaseException | Any) -> str:
    try:
        message = str(exc)
    except Exception:
        message = type(exc).__name__
    return message or type(exc).__name__


def _make_json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _make_json_safe(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_make_json_safe(item, depth=depth + 1) for item in value]
    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        try:
            return _make_json_safe(serializer(), depth=depth + 1)
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return repr(value)


def _config_value(name: str, default: Any = None) -> Any:
    try:
        return current_app.config.get(name, default)
    except Exception:
        return default


def _config_str(name: str, default: str = "") -> str:
    return _safe_str(_config_value(name, default), default)


def _config_bool(name: str, default: bool = False) -> bool:
    return _safe_bool(_config_value(name, default), default)


def _query_value(*names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if name in request.args:
                return request.args.get(name)
        except Exception:
            continue
    return default


def _query_bool(*names: str, default: bool = False) -> bool:
    value = _query_value(*names, default=None)
    return default if value is None else _safe_bool(value, default)


def _query_int(
    *names: str,
    default: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _query_value(*names, default=default)
    return _safe_int(
        value,
        default,
        minimum=minimum,
        maximum=maximum,
    )


def _query_str(*names: str, default: str = "") -> str:
    return _safe_str(_query_value(*names, default=default), default)


def _request_body_bytes() -> bytes | None:
    """
    Return the cached raw request body when Flask exposes it.

    ``None`` means that the current test/compatibility request object does not
    provide ``get_data``. An empty byte string means that Flask did provide the
    body and the request body is actually empty.
    """
    getter = getattr(request, "get_data", None)
    if not callable(getter):
        return None

    try:
        raw = getter(cache=True, as_text=False)
    except TypeError:
        try:
            raw = getter(cache=True)
        except TypeError:
            raw = getter()
    except Exception:
        return None

    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, memoryview):
        return raw.tobytes()
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace")

    try:
        return bytes(raw)
    except Exception:
        return _safe_str(raw).encode("utf-8", errors="replace")


def _request_body_diagnostics(raw_body: bytes | None) -> dict[str, Any]:
    """Build safe request-body diagnostics without echoing request content."""
    details: dict[str, Any] = {
        "expected": "one complete JSON object",
    }

    try:
        content_type = _safe_str(request.headers.get("Content-Type"))
    except Exception:
        content_type = ""
    if content_type:
        details["contentType"] = content_type

    try:
        mimetype = _safe_str(getattr(request, "mimetype", None))
    except Exception:
        mimetype = ""
    if mimetype:
        details["mimetype"] = mimetype

    try:
        content_length = getattr(request, "content_length", None)
    except Exception:
        content_length = None
    if content_length is not None:
        details["contentLength"] = _safe_int(
            content_length,
            0,
            minimum=0,
        )

    if raw_body is not None:
        details["bodyLength"] = len(raw_body)
        details["bodyPresent"] = bool(raw_body.strip())

    details["powershellHint"] = (
        "Use Invoke-RestMethod with ConvertTo-Json, or write JSON to a UTF-8 "
        "file and call curl.exe --data-binary @file. Windows PowerShell can "
        "split multiline variables passed directly to native executables."
    )
    return details


def _request_json() -> dict[str, Any]:
    """
    Read one JSON object without silently accepting malformed non-empty input.

    Empty bodies remain valid for idempotent DELETE/initialize operations. A
    non-empty body that Flask cannot parse is rejected before any database
    mutation. This prevents truncated native-shell arguments from being treated
    as ``{}`` and creating partially initialized roles, groups or assignments.
    """
    raw_body = _request_body_bytes()

    if raw_body is not None and not raw_body.strip():
        return {}

    try:
        value = request.get_json(silent=False)
    except Exception as exc:
        details = _request_body_diagnostics(raw_body)
        details["parserError"] = _safe_exception_message(exc)
        raise ProjectAccessValidationError(
            "request body contains invalid JSON",
            code="invalid_json_body",
            details=details,
            cause=exc,
        ) from exc

    if value is None:
        if raw_body is None:
            # Compatibility with minimal test request objects that expose only
            # ``get_json`` and represent an empty body as ``None``.
            return {}
        raise ProjectAccessValidationError(
            "request body contains invalid JSON",
            code="invalid_json_body",
            details=_request_body_diagnostics(raw_body),
        )

    if not isinstance(value, Mapping):
        details = _request_body_diagnostics(raw_body)
        details["actualType"] = type(value).__name__
        raise ProjectAccessValidationError(
            "request body must be a JSON object",
            code="invalid_json_body",
            details=details,
        )

    return dict(value)


def _request_header(*names: str) -> Optional[str]:
    for name in names:
        try:
            value = request.headers.get(name)
        except Exception:
            value = None
        text = _safe_str(value)
        if text:
            return text
    return None


def _include_debug_errors() -> bool:
    return bool(
        _query_bool("debug", "includeDebug", "include_debug", default=False)
        or _config_bool("VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS", False)
        or _safe_bool(os.environ.get("VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS"), False)
        or bool(getattr(current_app, "debug", False))
    )


def _actor_user_id(payload: Mapping[str, Any] | None = None) -> Optional[str]:
    """
    Return the external actor id carried by the request.

    This is audit metadata only. It is not authenticated in this module.
    Generic ``userId`` is intentionally not accepted because it commonly names
    the membership/assignment subject rather than the caller.
    """

    header_value = _request_header(
        "X-Vectoplan-User-Id",
        "X-Actor-User-Id",
        "X-User-Id",
    )
    if header_value:
        return header_value

    body = payload or {}
    for key in (
        "actorUserId",
        "actor_user_id",
        "updatedByUserId",
        "updated_by_user_id",
        "assignedByUserId",
        "assigned_by_user_id",
        "addedByUserId",
        "added_by_user_id",
    ):
        text = _safe_str(body.get(key))
        if text:
            return text

    configured = _config_str(
        "VECTOPLAN_CHUNK_PROJECT_ACCESS_DEFAULT_ACTOR_USER_ID",
        "",
    )
    return configured or None


def _default_owner_user_id() -> str:
    return _config_str(
        "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
        DEFAULT_PROJECT_OWNER_USER_ID,
    )


def _route_metadata(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "routeSource": ROUTE_SOURCE,
        "routeModuleVersion": ROUTE_MODULE_VERSION,
        "serviceVersion": PROJECT_ACCESS_SERVICE_VERSION,
        "authzEnforced": False,
        "externalUserForeignKeys": False,
        "transactionOwner": "route",
    }
    if extra:
        metadata.update(_make_json_safe(dict(extra)))
    return metadata


def _json_response(body: Mapping[str, Any], status_code: int = 200):
    return jsonify(_make_json_safe(dict(body))), int(status_code)


def _success_body(
    *,
    payload: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": True,
        "responseVersion": PROJECT_ACCESS_ROUTE_RESPONSE_VERSION,
    }
    if payload:
        body.update(_make_json_safe(dict(payload)))
    body["metadata"] = _route_metadata(metadata)
    return body


def _error_status_code(exc: BaseException | Any) -> int:
    if isinstance(exc, ProjectAccessValidationError):
        return 400
    if isinstance(exc, ProjectAccessNotFoundError):
        return 404
    if isinstance(
        exc,
        (
            ProjectAccessConflictError,
            ProjectAccessCrossProjectError,
            ProjectAccessInvariantError,
            IntegrityError,
        ),
    ):
        return 409
    if isinstance(exc, LookupError):
        return 404
    if isinstance(exc, ValueError):
        return 400
    return 500


def _error_code(exc: BaseException | Any) -> str:
    if isinstance(exc, ProjectAccessServiceError):
        return _safe_str(getattr(exc, "code", None), "project_access_error")
    if isinstance(exc, IntegrityError):
        return "project_access_integrity_error"
    if isinstance(exc, SQLAlchemyError):
        return "project_access_database_error"
    if isinstance(exc, LookupError):
        return "project_access_not_found"
    if isinstance(exc, ValueError):
        return "project_access_validation_failed"
    return "project_access_unexpected_error"


def _error_details(exc: BaseException | Any) -> dict[str, Any]:
    if isinstance(exc, ProjectAccessServiceError):
        return _make_json_safe(getattr(exc, "details", {}) or {})
    return {}


def _error_response(
    exc: BaseException | Any,
    *,
    status_code: int | None = None,
    code: str | None = None,
):
    body: dict[str, Any] = {
        "ok": False,
        "responseVersion": PROJECT_ACCESS_ROUTE_ERROR_VERSION,
        "error": {
            "code": code or _error_code(exc),
            "message": _safe_exception_message(exc),
            "details": _error_details(exc),
        },
        "metadata": _route_metadata(),
    }
    if _include_debug_errors():
        body["error"]["debug"] = {
            "type": type(exc).__name__,
            "repr": repr(exc),
        }
    return _json_response(
        body,
        status_code if status_code is not None else _error_status_code(exc),
    )


def _rollback_session() -> None:
    try:
        db.session.rollback()
    except Exception:
        pass


def _commit_session() -> None:
    try:
        db.session.commit()
    except IntegrityError as exc:
        _rollback_session()
        raise ProjectAccessConflictError(
            "project access change conflicts with an existing database row",
            code="project_access_integrity_conflict",
            details={"exceptionType": type(exc).__name__},
            cause=exc,
        ) from exc
    except SQLAlchemyError as exc:
        _rollback_session()
        raise ProjectAccessPersistenceError(
            "project access change could not be committed",
            code="project_access_commit_failed",
            details={"exceptionType": type(exc).__name__},
            cause=exc,
        ) from exc


def _entity_dict(
    entity: Any,
    *,
    include_internal: bool = False,
    include_metadata: bool = True,
) -> dict[str, Any]:
    if entity is None:
        return {}
    serializer = getattr(entity, "to_dict", None)
    if callable(serializer):
        try:
            return _make_json_safe(
                serializer(
                    include_internal=include_internal,
                    include_metadata=include_metadata,
                )
            )
        except TypeError:
            try:
                return _make_json_safe(serializer())
            except Exception:
                pass
        except Exception:
            pass
    return {
        key: _make_json_safe(getattr(entity, key, None))
        for key in (
            "id",
            "project_id",
            "role_id",
            "role_key",
            "group_id",
            "group_key",
            "membership_id",
            "assignment_id",
            "name",
            "status",
            "user_id",
            "subject_type",
            "subject_key",
        )
        if include_internal or key != "id"
    }


def _mutation_dict(
    result: EntityMutationResult,
    *,
    include_internal: bool,
    include_metadata: bool,
) -> dict[str, Any]:
    return result.to_dict(
        include_internal=include_internal,
        include_metadata=include_metadata,
    )


def _list_options() -> tuple[bool, bool, int, int]:
    include_deleted = _query_bool(
        "includeDeleted",
        "include_deleted",
        default=False,
    )
    include_internal = _query_bool(
        "includeInternal",
        "include_internal",
        default=False,
    )
    limit = _query_int(
        "limit",
        default=DEFAULT_LIST_LIMIT,
        minimum=1,
        maximum=MAX_LIST_LIMIT,
    )
    offset = _query_int("offset", default=0, minimum=0)
    return include_deleted, include_internal, limit, offset


def _slice_entities(
    entities: Sequence[Any],
    *,
    offset: int,
    limit: int,
) -> tuple[list[Any], dict[str, int]]:
    total = len(entities)
    page = list(entities[offset : offset + limit])
    return page, {
        "total": total,
        "count": len(page),
        "limit": limit,
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# Project/role/group resolution
# ---------------------------------------------------------------------------


def _normalize_route_project_id(project_id: Any) -> str:
    text = _safe_str(project_id)
    if text.lower() in _PROJECT_ALIASES:
        return _config_str(
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID",
            DEFAULT_PROJECT_ID,
        )
    if not text:
        raise ProjectAccessValidationError(
            "project_id is required",
            code="project_id_required",
        )
    return text


def _resolve_route_project(
    project_id: Any,
    *,
    include_deleted: bool = False,
    lock: bool = False,
) -> Project:
    resolved_id = _normalize_route_project_id(project_id)
    project = resolve_project(
        session=db.session,
        project_id=resolved_id,
        include_deleted=include_deleted,
        lock=lock,
    )
    return project


def _find_role_by_ref(
    project: Project,
    role_ref: Any,
    *,
    include_deleted: bool = False,
    required: bool = True,
    lock: bool = False,
) -> Optional[ProjectRole]:
    ref = _safe_str(role_ref)
    if not ref:
        if required:
            raise ProjectAccessValidationError("role reference is required")
        return None

    role: Optional[ProjectRole] = None
    try:
        role = find_project_role(
            project=project,
            role_key=ref,
            session=db.session,
            include_deleted=include_deleted,
            lock=lock,
            required=False,
        )
    except ProjectAccessValidationError:
        role = None

    if role is None:
        role = find_project_role(
            project=project,
            role_id=ref,
            session=db.session,
            include_deleted=include_deleted,
            lock=lock,
            required=False,
        )

    if role is None and required:
        raise ProjectAccessNotFoundError(
            "project role was not found",
            code="project_role_not_found",
            details={"roleRef": ref},
        )
    return role


def _find_group_by_ref(
    project: Project,
    group_ref: Any,
    *,
    include_deleted: bool = False,
    required: bool = True,
    lock: bool = False,
) -> Optional[ProjectGroup]:
    ref = _safe_str(group_ref)
    if not ref:
        if required:
            raise ProjectAccessValidationError("group reference is required")
        return None

    group: Optional[ProjectGroup] = None
    try:
        group = find_project_group(
            project=project,
            group_key=ref,
            session=db.session,
            include_deleted=include_deleted,
            lock=lock,
            required=False,
        )
    except ProjectAccessValidationError:
        group = None

    if group is None:
        group = find_project_group(
            project=project,
            group_id=ref,
            session=db.session,
            include_deleted=include_deleted,
            lock=lock,
            required=False,
        )

    if group is None and required:
        raise ProjectAccessNotFoundError(
            "project group was not found",
            code="project_group_not_found",
            details={"groupRef": ref},
        )
    return group


def _find_assignment(
    project: Project,
    assignment_id: Any,
    *,
    include_deleted: bool = False,
    lock: bool = False,
) -> ProjectRoleAssignment:
    assignment_id_text = _safe_str(assignment_id)
    if not assignment_id_text:
        raise ProjectAccessValidationError("assignment_id is required")

    query = db.session.query(ProjectRoleAssignment).filter(
        ProjectRoleAssignment.project_db_id == project.id,
        ProjectRoleAssignment.assignment_id == assignment_id_text,
    )
    if not include_deleted:
        query = query.filter(ProjectRoleAssignment.deleted_at.is_(None))
    if lock:
        try:
            query = query.with_for_update()
        except Exception:
            pass
    assignment = query.one_or_none()
    if assignment is None:
        raise ProjectAccessNotFoundError(
            "role assignment was not found",
            code="project_role_assignment_not_found",
            details={"assignmentId": assignment_id_text},
        )
    return assignment


def _project_identity(project: Project) -> dict[str, Any]:
    """
    Return stable route-level project identity.

    ``projectDbId`` is deliberately populated instead of emitting a misleading
    ``null`` field after the service serializer hides internal entity fields.
    The numeric id is already required for every access row and is useful for
    diagnostics and cross-checking transaction results.
    """
    return {
        "projectId": _safe_str(getattr(project, "project_id", None)) or None,
        "projectDbId": _make_json_safe(getattr(project, "id", None)),
    }


def _project_owner_user_id(project: Project) -> Optional[str]:
    owner_type = _safe_str(getattr(project, "owner_type", None)).lower()
    owner_id = _safe_str(getattr(project, "owner_id", None))
    if owner_type == SUBJECT_TYPE_USER and owner_id:
        return owner_id
    alias = _safe_str(getattr(project, "owner_user_id", None))
    return alias or None


def _ensure_owner_role_subject(
    *,
    project: Project,
    role: ProjectRole,
    subject_type: str,
    user_id: Any = None,
    group: Optional[ProjectGroup] = None,
) -> None:
    if role.role_key != DEFAULT_ROLE_OWNER:
        return

    owner_user_id = _project_owner_user_id(project)
    if not owner_user_id:
        raise ProjectAccessInvariantError(
            "project has no canonical user owner",
            code="project_owner_missing",
            details={"projectId": getattr(project, "project_id", None)},
        )
    if subject_type != SUBJECT_TYPE_USER or group is not None:
        raise ProjectAccessConflictError(
            "the Owner role can only be assigned directly to the project owner",
            code="owner_role_group_assignment_forbidden",
        )
    if _safe_str(user_id) != owner_user_id:
        raise ProjectAccessConflictError(
            "the Owner role subject must equal the current project owner",
            code="owner_role_subject_conflict",
            details={
                "ownerUserId": owner_user_id,
                "requestedUserId": _safe_str(user_id),
            },
        )


def _ensure_role_mutable(role: ProjectRole, payload: Mapping[str, Any]) -> None:
    protected = bool(role.is_system) or role.role_key in DEFAULT_ROLE_KEY_SET
    if not protected:
        return
    attempted = sorted(
        key for key in payload.keys() if key in _PROTECTED_ROLE_MUTATION_FIELDS
    )
    if attempted:
        raise ProjectAccessConflictError(
            "system/default roles are synchronized by the access initializer and "
            "cannot be changed through generic role CRUD",
            code="system_role_mutation_forbidden",
            details={
                "roleId": role.role_id,
                "roleKey": role.role_key,
                "attemptedFields": attempted,
            },
        )


def _identity_value(payload: Mapping[str, Any], *names: str) -> Optional[str]:
    for name in names:
        if name in payload:
            text = _safe_str(payload.get(name))
            return text or None
    return None


def _validate_role_identity_payload(
    role: ProjectRole,
    payload: Mapping[str, Any],
) -> None:
    requested_key = _identity_value(payload, "roleKey", "role_key", "key")
    requested_id = _identity_value(payload, "roleId", "role_id")
    if requested_key and requested_key.strip().lower().replace(" ", "_") != role.role_key:
        raise ProjectAccessConflictError(
            "role_key is immutable",
            code="project_role_key_immutable",
            details={
                "existingRoleKey": role.role_key,
                "requestedRoleKey": requested_key,
            },
        )
    if requested_id and requested_id != role.role_id:
        raise ProjectAccessConflictError(
            "role_id is immutable",
            code="project_role_id_immutable",
            details={
                "existingRoleId": role.role_id,
                "requestedRoleId": requested_id,
            },
        )


def _validate_group_identity_payload(
    group: ProjectGroup,
    payload: Mapping[str, Any],
) -> None:
    requested_key = _identity_value(payload, "groupKey", "group_key", "key")
    requested_id = _identity_value(payload, "groupId", "group_id")
    if requested_key and requested_key.strip().lower().replace(" ", "_") != group.group_key:
        raise ProjectAccessConflictError(
            "group_key is immutable",
            code="project_group_key_immutable",
            details={
                "existingGroupKey": group.group_key,
                "requestedGroupKey": requested_key,
            },
        )
    if requested_id and requested_id != group.group_id:
        raise ProjectAccessConflictError(
            "group_id is immutable",
            code="project_group_id_immutable",
            details={
                "existingGroupId": group.group_id,
                "requestedGroupId": requested_id,
            },
        )


# ---------------------------------------------------------------------------
# Role mutation helpers
# ---------------------------------------------------------------------------


def _create_role(
    project: Project,
    payload: Mapping[str, Any],
    *,
    actor_user_id: Any = None,
) -> EntityMutationResult:
    role_key = _identity_value(payload, "roleKey", "role_key", "key")
    if not role_key:
        raise ProjectAccessValidationError(
            "roleKey is required",
            code="project_role_key_required",
        )

    existing = find_project_role(
        project=project,
        role_key=role_key,
        session=db.session,
        include_deleted=True,
        lock=True,
        required=False,
    )
    if existing is not None:
        raise ProjectAccessConflictError(
            "a role with this roleKey already exists",
            code="project_role_already_exists",
            details={
                "roleKey": existing.role_key,
                "roleId": existing.role_id,
                "deleted": bool(existing.is_deleted),
            },
        )

    if _safe_bool(payload.get("isSystem", payload.get("is_system")), False):
        raise ProjectAccessConflictError(
            "custom routes cannot create system roles",
            code="system_role_creation_forbidden",
        )

    try:
        role = ProjectRole.from_create_payload(
            project_db_id=project.id,
            payload=payload,
            created_by_user_id=actor_user_id,
        )
    except ValueError as exc:
        raise ProjectAccessValidationError(
            "project role could not be created",
            details={"roleKey": role_key},
            cause=exc,
        ) from exc

    if role.role_key in DEFAULT_ROLE_KEY_SET:
        raise ProjectAccessConflictError(
            "default roles must be created through access initialization",
            code="default_role_creation_forbidden",
            details={"roleKey": role.role_key},
        )

    role.is_system = False
    db.session.add(role)
    db.session.flush()
    return EntityMutationResult(
        entity=role,
        action="created",
        stats=MutationStats(created=1),
    )


def _patch_role(
    role: ProjectRole,
    payload: Mapping[str, Any],
    *,
    actor_user_id: Any = None,
    restore_deleted: bool = False,
) -> EntityMutationResult:
    _validate_role_identity_payload(role, payload)
    _ensure_role_mutable(role, payload)

    stats = MutationStats()
    action = "reused"
    if role.is_deleted:
        if not restore_deleted:
            raise ProjectAccessConflictError(
                "role is deleted",
                code="project_role_deleted",
                details={"roleId": role.role_id, "roleKey": role.role_key},
            )
        role.restore(restored_by_user_id=actor_user_id)
        stats.reactivated += 1
        action = "reactivated"

    mutable_payload = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key
        not in {
            "roleKey",
            "role_key",
            "key",
            "roleId",
            "role_id",
            "actorUserId",
            "actor_user_id",
            "restoreDeleted",
            "restore_deleted",
        }
    }

    if mutable_payload:
        before = _entity_dict(
            role,
            include_internal=True,
            include_metadata=True,
        )
        try:
            role.apply_patch_payload(
                mutable_payload,
                updated_by_user_id=actor_user_id,
            )
        except ValueError as exc:
            raise ProjectAccessValidationError(
                "project role update is invalid",
                details={"roleId": role.role_id, "roleKey": role.role_key},
                cause=exc,
            ) from exc
        after = _entity_dict(
            role,
            include_internal=True,
            include_metadata=True,
        )
        if before != after and stats.reactivated == 0:
            stats.updated += 1
            action = "updated"

    if not stats.changed:
        stats.reused += 1
    db.session.add(role)
    if stats.changed:
        db.session.flush()
    return EntityMutationResult(entity=role, action=action, stats=stats)


def _delete_custom_role(
    project: Project,
    role: ProjectRole,
    *,
    actor_user_id: Any = None,
) -> dict[str, Any]:
    if bool(role.is_system) or role.role_key in DEFAULT_ROLE_KEY_SET:
        raise ProjectAccessConflictError(
            "system/default roles cannot be deleted",
            code="system_role_delete_forbidden",
            details={"roleId": role.role_id, "roleKey": role.role_key},
        )

    assignments = list_project_role_assignments(
        project=project,
        role=role,
        session=db.session,
        include_deleted=True,
        lock=True,
    )
    changed = False
    assignment_count = 0
    for assignment in assignments:
        if assignment.is_deleted:
            continue
        assignment.soft_delete(deleted_by_user_id=actor_user_id)
        db.session.add(assignment)
        assignment_count += 1
        changed = True

    if not role.is_deleted:
        role.soft_delete(deleted_by_user_id=actor_user_id)
        db.session.add(role)
        changed = True

    if changed:
        db.session.flush()
    return {
        "deleted": True,
        "changed": changed,
        "role": _entity_dict(role, include_internal=False, include_metadata=True),
        "counts": {"assignmentsSoftDeleted": assignment_count},
    }


# ---------------------------------------------------------------------------
# Group mutation helpers
# ---------------------------------------------------------------------------


def _patch_group(
    group: ProjectGroup,
    payload: Mapping[str, Any],
    *,
    actor_user_id: Any = None,
    restore_deleted: bool = False,
) -> EntityMutationResult:
    _validate_group_identity_payload(group, payload)
    stats = MutationStats()
    action = "reused"
    if group.is_deleted:
        if not restore_deleted:
            raise ProjectAccessConflictError(
                "group is deleted",
                code="project_group_deleted",
                details={"groupId": group.group_id, "groupKey": group.group_key},
            )
        group.restore(restored_by_user_id=actor_user_id)
        stats.reactivated += 1
        action = "reactivated"

    mutable_payload = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key
        not in {
            "groupKey",
            "group_key",
            "key",
            "groupId",
            "group_id",
            "actorUserId",
            "actor_user_id",
            "restoreDeleted",
            "restore_deleted",
        }
    }
    if mutable_payload:
        before = _entity_dict(
            group,
            include_internal=True,
            include_metadata=True,
        )
        try:
            group.apply_patch_payload(
                mutable_payload,
                updated_by_user_id=actor_user_id,
            )
        except ValueError as exc:
            raise ProjectAccessValidationError(
                "project group update is invalid",
                details={"groupId": group.group_id, "groupKey": group.group_key},
                cause=exc,
            ) from exc
        after = _entity_dict(
            group,
            include_internal=True,
            include_metadata=True,
        )
        if before != after and stats.reactivated == 0:
            stats.updated += 1
            action = "updated"

    if not stats.changed:
        stats.reused += 1
    db.session.add(group)
    if stats.changed:
        db.session.flush()
    return EntityMutationResult(entity=group, action=action, stats=stats)


def _delete_group_tree(
    project: Project,
    group: ProjectGroup,
    *,
    actor_user_id: Any = None,
) -> dict[str, Any]:
    memberships = list_group_memberships(
        project=project,
        group=group,
        session=db.session,
        include_deleted=True,
        lock=True,
    )
    assignments = list_project_role_assignments(
        project=project,
        group=group,
        session=db.session,
        include_deleted=True,
        lock=True,
    )

    membership_count = 0
    assignment_count = 0
    changed = False
    for membership in memberships:
        if membership.is_deleted:
            continue
        membership.soft_delete(deleted_by_user_id=actor_user_id)
        db.session.add(membership)
        membership_count += 1
        changed = True
    for assignment in assignments:
        if assignment.is_deleted:
            continue
        assignment.soft_delete(deleted_by_user_id=actor_user_id)
        db.session.add(assignment)
        assignment_count += 1
        changed = True
    if not group.is_deleted:
        group.soft_delete(deleted_by_user_id=actor_user_id)
        db.session.add(group)
        changed = True

    if changed:
        db.session.flush()
    return {
        "deleted": True,
        "changed": changed,
        "group": _entity_dict(group, include_internal=False, include_metadata=True),
        "counts": {
            "membershipsSoftDeleted": membership_count,
            "assignmentsSoftDeleted": assignment_count,
        },
    }


# ---------------------------------------------------------------------------
# Assignment payload helpers
# ---------------------------------------------------------------------------


def _payload_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectAccessValidationError(
            f"{field_name} must be a JSON object",
            details={"field": field_name},
        )
    return dict(value)


def _assignment_subject(payload: Mapping[str, Any]) -> tuple[str, Optional[str], Optional[str]]:
    subject = _payload_mapping(payload.get("subject"), field_name="subject")
    subject_type = _safe_str(
        payload.get(
            "subjectType",
            payload.get("subject_type", subject.get("type")),
        )
    ).lower()
    user_id = _safe_str(
        payload.get(
            "userId",
            payload.get("user_id", subject.get("userId", subject.get("user_id"))),
        )
    ) or None
    group_id = _safe_str(
        payload.get(
            "groupId",
            payload.get(
                "group_id",
                subject.get("groupId", subject.get("group_id")),
            ),
        )
    ) or None

    if not subject_type:
        if user_id and not group_id:
            subject_type = SUBJECT_TYPE_USER
        elif group_id and not user_id:
            subject_type = SUBJECT_TYPE_GROUP

    if subject_type not in {SUBJECT_TYPE_USER, SUBJECT_TYPE_GROUP}:
        raise ProjectAccessValidationError(
            "subjectType must be user or group",
            code="project_assignment_subject_type_invalid",
        )
    if subject_type == SUBJECT_TYPE_USER and not user_id:
        raise ProjectAccessValidationError("userId is required for user assignment")
    if subject_type == SUBJECT_TYPE_GROUP and not group_id:
        raise ProjectAccessValidationError("groupId is required for group assignment")
    return subject_type, user_id, group_id


def _assignment_role(project: Project, payload: Mapping[str, Any]) -> ProjectRole:
    role_payload = _payload_mapping(payload.get("role"), field_name="role")
    role_ref = _safe_str(
        payload.get(
            "roleKey",
            payload.get(
                "role_key",
                payload.get(
                    "roleId",
                    payload.get(
                        "role_id",
                        role_payload.get(
                            "roleKey",
                            role_payload.get("roleId", role_payload.get("id")),
                        ),
                    ),
                ),
            ),
        )
    )
    if not role_ref:
        raise ProjectAccessValidationError(
            "roleKey or roleId is required",
            code="project_assignment_role_required",
        )
    role = _find_role_by_ref(project, role_ref, required=True, lock=True)
    assert role is not None
    return role


def _ensure_assignment_from_payload(
    project: Project,
    payload: Mapping[str, Any],
    *,
    actor_user_id: Any = None,
) -> EntityMutationResult:
    role = _assignment_role(project, payload)
    subject_type, user_id, group_id = _assignment_subject(payload)
    group: Optional[ProjectGroup] = None
    if subject_type == SUBJECT_TYPE_GROUP:
        group = _find_group_by_ref(
            project,
            group_id,
            required=True,
            lock=True,
        )
        assert group is not None

    _ensure_owner_role_subject(
        project=project,
        role=role,
        subject_type=subject_type,
        user_id=user_id,
        group=group,
    )

    permission_overrides = payload.get(
        "permissionOverrides",
        payload.get(
            "permission_overrides",
            payload.get(
                "permissionOverridesJson",
                payload.get("permission_overrides_json"),
            ),
        ),
    )
    if permission_overrides is not None and not isinstance(permission_overrides, Mapping):
        raise ProjectAccessValidationError(
            "permissionOverrides must be a JSON object"
        )
    metadata = payload.get(
        "metadata",
        payload.get("metadataJson", payload.get("metadata_json")),
    )
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ProjectAccessValidationError("metadata must be a JSON object")

    return ensure_role_assignment(
        project=project,
        role=role,
        subject_type=subject_type,
        user_id=user_id,
        group=group,
        actor_user_id=actor_user_id,
        permission_overrides=dict(permission_overrides)
        if permission_overrides is not None
        else None,
        starts_at=payload.get("startsAt", payload.get("starts_at")),
        expires_at=payload.get("expiresAt", payload.get("expires_at")),
        metadata=dict(metadata) if metadata is not None else None,
        session=db.session,
        flush=True,
    )


def _patch_assignment(
    project: Project,
    assignment: ProjectRoleAssignment,
    payload: Mapping[str, Any],
    *,
    actor_user_id: Any = None,
) -> EntityMutationResult:
    immutable_fields = {
        "assignmentId",
        "assignment_id",
        "roleId",
        "role_id",
        "roleKey",
        "role_key",
        "subject",
        "subjectType",
        "subject_type",
        "subjectId",
        "subject_id",
        "userId",
        "user_id",
        "groupId",
        "group_id",
    }
    attempted_immutable = sorted(key for key in payload if key in immutable_fields)
    if attempted_immutable:
        raise ProjectAccessConflictError(
            "assignment identity is immutable; revoke it and create another assignment",
            code="project_assignment_identity_immutable",
            details={"attemptedFields": attempted_immutable},
        )

    role = _find_role_by_ref(
        project,
        assignment.role_id,
        include_deleted=True,
        required=True,
    )
    assert role is not None
    if role.role_key == DEFAULT_ROLE_OWNER:
        raise ProjectAccessConflictError(
            "Owner assignment changes must use project owner transfer/initialization",
            code="owner_assignment_mutation_forbidden",
        )

    requested_status = _safe_str(payload.get("status")).lower()
    if requested_status == ASSIGNMENT_STATUS_REVOKED:
        return revoke_role_assignment(
            project=project,
            assignment_id=assignment.assignment_id,
            actor_user_id=actor_user_id,
            reason=payload.get("revocationReason", payload.get("reason")),
            session=db.session,
            missing_ok=False,
            flush=True,
        )
    if requested_status == ASSIGNMENT_STATUS_DELETED:
        if assignment.is_deleted:
            return EntityMutationResult(
                entity=assignment,
                action="reused",
                stats=MutationStats(reused=1),
            )
        assignment.soft_delete(deleted_by_user_id=actor_user_id)
        db.session.add(assignment)
        db.session.flush()
        return EntityMutationResult(
            entity=assignment,
            action="deleted",
            stats=MutationStats(deleted=1),
        )
    if requested_status == ASSIGNMENT_STATUS_ACTIVE and (
        assignment.is_deleted
        or assignment.status in {ASSIGNMENT_STATUS_INACTIVE, ASSIGNMENT_STATUS_REVOKED}
    ):
        if assignment.is_deleted:
            assignment.restore(restored_by_user_id=actor_user_id)
        assignment.reactivate(
            reactivated_by_user_id=actor_user_id,
            starts_at=payload.get("startsAt", payload.get("starts_at")),
            expires_at=payload.get("expiresAt", payload.get("expires_at")),
        )
        db.session.add(assignment)
        db.session.flush()
        return EntityMutationResult(
            entity=assignment,
            action="reactivated",
            stats=MutationStats(reactivated=1),
        )

    mutable_payload = {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key
        not in {
            "actorUserId",
            "actor_user_id",
            "revocationReason",
            "reason",
        }
    }
    if not mutable_payload:
        return EntityMutationResult(
            entity=assignment,
            action="reused",
            stats=MutationStats(reused=1),
        )

    before = _entity_dict(
        assignment,
        include_internal=True,
        include_metadata=True,
    )
    try:
        assignment.apply_patch_payload(
            mutable_payload,
            updated_by_user_id=actor_user_id,
        )
    except ValueError as exc:
        raise ProjectAccessValidationError(
            "role assignment update is invalid",
            details={"assignmentId": assignment.assignment_id},
            cause=exc,
        ) from exc
    after = _entity_dict(
        assignment,
        include_internal=True,
        include_metadata=True,
    )
    changed = before != after
    db.session.add(assignment)
    if changed:
        db.session.flush()
    return EntityMutationResult(
        entity=assignment,
        action="updated" if changed else "reused",
        stats=MutationStats(updated=1) if changed else MutationStats(reused=1),
    )


# ---------------------------------------------------------------------------
# Status and access summary routes
# ---------------------------------------------------------------------------


@project_access_bp.get("/project-access/_status")
def get_project_access_route_status():
    """Return code/model/service readiness without evaluating permissions."""

    try:
        model_contract = get_project_access_model_contract()
        service_contract = get_project_access_service_contract()
        table_names = list(model_contract.get("tableNames") or [])
        body = {
            "ok": True,
            "status": "ready",
            "responseVersion": PROJECT_ACCESS_ROUTE_STATUS_VERSION,
            "authzEnforced": False,
            "storageContractReady": True,
            "serviceVersion": PROJECT_ACCESS_SERVICE_VERSION,
            "routeModuleVersion": ROUTE_MODULE_VERSION,
            "blueprint": project_access_bp.name,
            "models": model_contract,
            "service": service_contract,
            "tables": table_names,
            "routes": {
                "summary": "/projects/<project_id>/access",
                "initialize": "/projects/<project_id>/access/initialize",
                "roles": "/projects/<project_id>/roles",
                "groups": "/projects/<project_id>/groups",
                "assignments": "/projects/<project_id>/assignments",
            },
            "requestBodyContract": {
                "emptyBodyAllowed": True,
                "nonEmptyBodyMustBeValidJsonObject": True,
                "malformedJsonRejectedBeforeMutation": True,
                "requestContentEchoedInErrors": False,
            },
            "metadata": _route_metadata(
                {
                    "checkedAt": _utc_now().isoformat(),
                    "databaseQueried": False,
                }
            ),
        }
        return _json_response(body, 200)
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.get("/projects/<project_id>/access")
def get_project_access_summary(project_id: str):
    try:
        include_deleted = _query_bool(
            "includeDeleted",
            "include_deleted",
            default=False,
        )
        include_internal = _query_bool(
            "includeInternal",
            "include_internal",
            default=False,
        )
        include_metadata = _query_bool(
            "includeMetadata",
            "include_metadata",
            default=True,
        )
        project = _resolve_route_project(
            project_id,
            include_deleted=include_deleted,
        )
        summary = build_project_access_summary(
            project=project,
            session=db.session,
            include_deleted=include_deleted,
            include_internal=include_internal,
            include_metadata=include_metadata,
        )
        project_identity = _project_identity(project)
        summary.update(project_identity)
        summary["responseVersion"] = PROJECT_ACCESS_ROUTE_RESPONSE_VERSION
        summary["metadata"] = _route_metadata(
            {
                **project_identity,
                "includeDeleted": include_deleted,
                "includeInternal": include_internal,
                "includeMetadata": include_metadata,
            }
        )
        return _json_response(summary, 200)
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.put("/projects/<project_id>/access/initialize")
@project_access_bp.post("/projects/<project_id>/access/initialize")
def initialize_project_access(project_id: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        actor = _actor_user_id(payload)
        owner_user_id = _safe_str(
            payload.get(
                "ownerUserId",
                payload.get(
                    "owner_user_id",
                    _project_owner_user_id(project) or _default_owner_user_id(),
                ),
            )
        )
        allow_missing_owner = _safe_bool(
            payload.get("allowMissingOwner", payload.get("allow_missing_owner")),
            False,
        )
        replace_existing_owner = _safe_bool(
            payload.get(
                "replaceExistingOwner",
                payload.get("replace_existing_owner"),
            ),
            False,
        )
        project_identity = _project_identity(project)
        if project_identity.get("projectDbId") is None:
            raise ProjectAccessInvariantError(
                "resolved project has no persistent database id",
                code="project_database_identity_missing",
                details={"projectId": project_identity.get("projectId")},
            )

        result = ensure_project_access_initialized(
            project=project,
            owner_user_id=owner_user_id or None,
            actor_user_id=actor,
            session=db.session,
            synchronize_default_roles=_safe_bool(
                payload.get(
                    "synchronizeDefaultRoles",
                    payload.get("synchronize_default_roles"),
                ),
                True,
            ),
            restore_deleted_roles=_safe_bool(
                payload.get(
                    "restoreDeletedRoles",
                    payload.get("restore_deleted_roles"),
                ),
                True,
            ),
            replace_existing_owner=replace_existing_owner,
            allow_missing_owner=allow_missing_owner,
            lock_project=True,
            flush=True,
        )
        _commit_session()
        include_internal = _query_bool(
            "includeInternal",
            "include_internal",
            default=False,
        )
        include_metadata = _query_bool(
            "includeMetadata",
            "include_metadata",
            default=True,
        )
        data = result.to_dict(
            include_internal=include_internal,
            include_metadata=include_metadata,
        )
        data.update(project_identity)
        data["responseVersion"] = PROJECT_ACCESS_ROUTE_RESPONSE_VERSION
        data["metadata"] = _route_metadata(
            {
                **project_identity,
                "replaceExistingOwner": replace_existing_owner,
            }
        )
        return _json_response(data, 201 if result.changed else 200)
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


# ---------------------------------------------------------------------------
# Role routes
# ---------------------------------------------------------------------------


@project_access_bp.get("/projects/<project_id>/roles")
def get_project_roles(project_id: str):
    try:
        include_deleted, include_internal, limit, offset = _list_options()
        include_metadata = _query_bool(
            "includeMetadata",
            "include_metadata",
            default=True,
        )
        project = _resolve_route_project(project_id)
        roles = list_project_roles(
            project=project,
            session=db.session,
            include_deleted=include_deleted,
        )
        role_key_filter = _query_str("roleKey", "role_key", default="").lower()
        status_filter = _query_str("status", default="").lower()
        if role_key_filter:
            roles = [role for role in roles if role.role_key == role_key_filter]
        if status_filter:
            roles = [role for role in roles if role.status == status_filter]
        page, counts = _slice_entities(roles, offset=offset, limit=limit)
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "roles": [
                        _entity_dict(
                            role,
                            include_internal=include_internal,
                            include_metadata=include_metadata,
                        )
                        for role in page
                    ],
                    "counts": counts,
                },
                metadata={
                    "includeDeleted": include_deleted,
                    "includeInternal": include_internal,
                    "includeMetadata": include_metadata,
                },
            ),
            200,
        )
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.post("/projects/<project_id>/roles")
def create_project_role(project_id: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        result = _create_role(
            project,
            payload,
            actor_user_id=_actor_user_id(payload),
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                }
            ),
            201,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.get("/projects/<project_id>/roles/<role_ref>")
def get_project_role(project_id: str, role_ref: str):
    try:
        include_deleted = _query_bool(
            "includeDeleted",
            "include_deleted",
            default=False,
        )
        project = _resolve_route_project(project_id)
        role = _find_role_by_ref(
            project,
            role_ref,
            include_deleted=include_deleted,
            required=True,
        )
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "role": _entity_dict(
                        role,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                }
            ),
            200,
        )
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.put("/projects/<project_id>/roles/<role_ref>")
def put_project_role(project_id: str, role_ref: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        role = _find_role_by_ref(
            project,
            role_ref,
            include_deleted=True,
            required=False,
            lock=True,
        )
        actor = _actor_user_id(payload)
        if role is None:
            create_payload = dict(payload)
            create_payload.setdefault("roleKey", role_ref)
            result = _create_role(project, create_payload, actor_user_id=actor)
            status_code = 201
        else:
            result = _patch_role(
                role,
                payload,
                actor_user_id=actor,
                restore_deleted=_safe_bool(
                    payload.get(
                        "restoreDeleted",
                        payload.get("restore_deleted"),
                    ),
                    True,
                ),
            )
            status_code = 200
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                },
                metadata={"idempotent": True},
            ),
            status_code,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.patch("/projects/<project_id>/roles/<role_ref>")
def patch_project_role(project_id: str, role_ref: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        role = _find_role_by_ref(
            project,
            role_ref,
            include_deleted=True,
            required=True,
            lock=True,
        )
        assert role is not None
        result = _patch_role(
            role,
            payload,
            actor_user_id=_actor_user_id(payload),
            restore_deleted=_safe_bool(
                payload.get("restoreDeleted", payload.get("restore_deleted")),
                False,
            ),
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                }
            ),
            200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.delete("/projects/<project_id>/roles/<role_ref>")
def delete_project_role(project_id: str, role_ref: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        role = _find_role_by_ref(
            project,
            role_ref,
            include_deleted=True,
            required=True,
            lock=True,
        )
        assert role is not None
        result = _delete_custom_role(
            project,
            role,
            actor_user_id=_actor_user_id(payload),
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={"projectId": project.project_id, **result},
                metadata={"softDelete": True},
            ),
            200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


# ---------------------------------------------------------------------------
# Group routes
# ---------------------------------------------------------------------------


@project_access_bp.get("/projects/<project_id>/groups")
def get_project_groups(project_id: str):
    try:
        include_deleted, include_internal, limit, offset = _list_options()
        include_metadata = _query_bool(
            "includeMetadata",
            "include_metadata",
            default=True,
        )
        project = _resolve_route_project(project_id)
        groups = list_project_groups(
            project=project,
            session=db.session,
            include_deleted=include_deleted,
        )
        key_filter = _query_str("groupKey", "group_key", default="").lower()
        status_filter = _query_str("status", default="").lower()
        if key_filter:
            groups = [group for group in groups if group.group_key == key_filter]
        if status_filter:
            groups = [group for group in groups if group.status == status_filter]
        page, counts = _slice_entities(groups, offset=offset, limit=limit)
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "groups": [
                        _entity_dict(
                            group,
                            include_internal=include_internal,
                            include_metadata=include_metadata,
                        )
                        for group in page
                    ],
                    "counts": counts,
                },
                metadata={
                    "includeDeleted": include_deleted,
                    "includeInternal": include_internal,
                    "includeMetadata": include_metadata,
                },
            ),
            200,
        )
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.post("/projects/<project_id>/groups")
def create_project_group(project_id: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        result = ensure_project_group(
            project=project,
            payload=payload,
            actor_user_id=_actor_user_id(payload),
            session=db.session,
            synchronize_existing=_safe_bool(
                payload.get(
                    "synchronizeExisting",
                    payload.get("synchronize_existing"),
                ),
                False,
            ),
            restore_deleted=_safe_bool(
                payload.get("restoreDeleted", payload.get("restore_deleted")),
                False,
            ),
            flush=True,
        )
        if result.action == "reused" and not _safe_bool(
            payload.get("allowExisting", payload.get("allow_existing")),
            False,
        ):
            raise ProjectAccessConflictError(
                "a group with this groupKey already exists",
                code="project_group_already_exists",
                details={
                    "groupId": result.entity.group_id,
                    "groupKey": result.entity.group_key,
                },
            )
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                }
            ),
            201 if result.action == "created" else 200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.get("/projects/<project_id>/groups/<group_ref>")
def get_project_group(project_id: str, group_ref: str):
    try:
        include_deleted = _query_bool(
            "includeDeleted",
            "include_deleted",
            default=False,
        )
        project = _resolve_route_project(project_id)
        group = _find_group_by_ref(
            project,
            group_ref,
            include_deleted=include_deleted,
            required=True,
        )
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "group": _entity_dict(
                        group,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                }
            ),
            200,
        )
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.put("/projects/<project_id>/groups/<group_ref>")
def put_project_group(project_id: str, group_ref: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        existing = _find_group_by_ref(
            project,
            group_ref,
            include_deleted=True,
            required=False,
            lock=True,
        )
        actor = _actor_user_id(payload)
        if existing is None:
            create_payload = dict(payload)
            create_payload.setdefault("groupKey", group_ref)
            result = ensure_project_group(
                project=project,
                payload=create_payload,
                actor_user_id=actor,
                session=db.session,
                synchronize_existing=True,
                restore_deleted=True,
                flush=True,
            )
            status_code = 201
        else:
            result = _patch_group(
                existing,
                payload,
                actor_user_id=actor,
                restore_deleted=_safe_bool(
                    payload.get(
                        "restoreDeleted",
                        payload.get("restore_deleted"),
                    ),
                    True,
                ),
            )
            status_code = 200
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                },
                metadata={"idempotent": True},
            ),
            status_code,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.patch("/projects/<project_id>/groups/<group_ref>")
def patch_project_group(project_id: str, group_ref: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        group = _find_group_by_ref(
            project,
            group_ref,
            include_deleted=True,
            required=True,
            lock=True,
        )
        assert group is not None
        result = _patch_group(
            group,
            payload,
            actor_user_id=_actor_user_id(payload),
            restore_deleted=_safe_bool(
                payload.get("restoreDeleted", payload.get("restore_deleted")),
                False,
            ),
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                }
            ),
            200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.delete("/projects/<project_id>/groups/<group_ref>")
def delete_project_group(project_id: str, group_ref: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        group = _find_group_by_ref(
            project,
            group_ref,
            include_deleted=True,
            required=True,
            lock=True,
        )
        assert group is not None
        result = _delete_group_tree(
            project,
            group,
            actor_user_id=_actor_user_id(payload),
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={"projectId": project.project_id, **result},
                metadata={"softDelete": True},
            ),
            200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


# ---------------------------------------------------------------------------
# Group membership routes
# ---------------------------------------------------------------------------


@project_access_bp.get(
    "/projects/<project_id>/groups/<group_ref>/members"
)
def get_project_group_members(project_id: str, group_ref: str):
    try:
        include_deleted, include_internal, limit, offset = _list_options()
        include_metadata = _query_bool(
            "includeMetadata",
            "include_metadata",
            default=True,
        )
        project = _resolve_route_project(project_id)
        group = _find_group_by_ref(
            project,
            group_ref,
            include_deleted=include_deleted,
            required=True,
        )
        assert group is not None
        user_filter = _query_str("userId", "user_id", default="")
        memberships = list_group_memberships(
            project=project,
            group=group,
            user_id=user_filter or None,
            session=db.session,
            include_deleted=include_deleted,
        )
        status_filter = _query_str("status", default="").lower()
        effective_only = _query_bool(
            "effectiveOnly",
            "effective_only",
            default=False,
        )
        if status_filter:
            memberships = [
                membership
                for membership in memberships
                if membership.status == status_filter
            ]
        if effective_only:
            memberships = [
                membership
                for membership in memberships
                if membership.is_effective()
            ]
        page, counts = _slice_entities(
            memberships,
            offset=offset,
            limit=limit,
        )
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "group": _entity_dict(
                        group,
                        include_internal=include_internal,
                        include_metadata=include_metadata,
                    ),
                    "memberships": [
                        _entity_dict(
                            membership,
                            include_internal=include_internal,
                            include_metadata=include_metadata,
                        )
                        for membership in page
                    ],
                    "counts": counts,
                },
                metadata={
                    "includeDeleted": include_deleted,
                    "effectiveOnly": effective_only,
                },
            ),
            200,
        )
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.post(
    "/projects/<project_id>/groups/<group_ref>/members"
)
def create_project_group_member(project_id: str, group_ref: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        user_id = _safe_str(payload.get("userId", payload.get("user_id")))
        if not user_id:
            raise ProjectAccessValidationError("userId is required")
        return _put_project_group_member_impl(
            project_id,
            group_ref,
            user_id,
            payload,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


def _put_project_group_member_impl(
    project_id: str,
    group_ref: str,
    user_id: str,
    payload: Mapping[str, Any],
):
    project = _resolve_route_project(project_id, lock=True)
    group = _find_group_by_ref(
        project,
        group_ref,
        include_deleted=False,
        required=True,
        lock=True,
    )
    assert group is not None
    metadata = payload.get(
        "metadata",
        payload.get("metadataJson", payload.get("metadata_json")),
    )
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ProjectAccessValidationError("metadata must be a JSON object")
    result = ensure_user_in_group(
        project=project,
        group=group,
        user_id=user_id,
        actor_user_id=_actor_user_id(payload),
        starts_at=payload.get("startsAt", payload.get("starts_at")),
        expires_at=payload.get("expiresAt", payload.get("expires_at")),
        metadata=dict(metadata) if metadata is not None else None,
        session=db.session,
        flush=True,
    )
    _commit_session()
    return _json_response(
        _success_body(
            payload={
                "projectId": project.project_id,
                "groupId": group.group_id,
                "userId": user_id,
                "mutation": _mutation_dict(
                    result,
                    include_internal=_query_bool(
                        "includeInternal",
                        "include_internal",
                        default=False,
                    ),
                    include_metadata=_query_bool(
                        "includeMetadata",
                        "include_metadata",
                        default=True,
                    ),
                ),
            },
            metadata={"idempotent": True},
        ),
        201 if result.action == "created" else 200,
    )


@project_access_bp.put(
    "/projects/<project_id>/groups/<group_ref>/members/<user_id>"
)
def put_project_group_member(
    project_id: str,
    group_ref: str,
    user_id: str,
):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        return _put_project_group_member_impl(
            project_id,
            group_ref,
            user_id,
            payload,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.delete(
    "/projects/<project_id>/groups/<group_ref>/members/<user_id>"
)
def delete_project_group_member(
    project_id: str,
    group_ref: str,
    user_id: str,
):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        group = _find_group_by_ref(
            project,
            group_ref,
            include_deleted=False,
            required=True,
            lock=True,
        )
        assert group is not None
        result = remove_user_from_group(
            project=project,
            group=group,
            user_id=user_id,
            actor_user_id=_actor_user_id(payload),
            reason=payload.get(
                "removalReason",
                payload.get("reason", _query_str("reason", default="")),
            ),
            session=db.session,
            missing_ok=_query_bool(
                "missingOk",
                "missing_ok",
                default=True,
            ),
            flush=True,
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "groupId": group.group_id,
                    "userId": user_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                },
                metadata={"idempotent": True},
            ),
            200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


# ---------------------------------------------------------------------------
# Assignment routes
# ---------------------------------------------------------------------------


@project_access_bp.get("/projects/<project_id>/assignments")
def get_project_assignments(project_id: str):
    try:
        include_deleted, include_internal, limit, offset = _list_options()
        include_metadata = _query_bool(
            "includeMetadata",
            "include_metadata",
            default=True,
        )
        project = _resolve_route_project(project_id)

        role: Optional[ProjectRole] = None
        role_ref = _query_str(
            "roleKey",
            "role_key",
            "roleId",
            "role_id",
            default="",
        )
        if role_ref:
            role = _find_role_by_ref(
                project,
                role_ref,
                include_deleted=include_deleted,
                required=True,
            )

        group: Optional[ProjectGroup] = None
        group_ref = _query_str(
            "groupId",
            "group_id",
            "groupKey",
            "group_key",
            default="",
        )
        if group_ref:
            group = _find_group_by_ref(
                project,
                group_ref,
                include_deleted=include_deleted,
                required=True,
            )

        user_id = _query_str("userId", "user_id", default="")
        subject_type = _query_str(
            "subjectType",
            "subject_type",
            default="",
        )
        assignments = list_project_role_assignments(
            project=project,
            role=role,
            group=group,
            user_id=user_id or None,
            subject_type=subject_type or None,
            session=db.session,
            include_deleted=include_deleted,
        )
        status_filter = _query_str("status", default="").lower()
        effective_only = _query_bool(
            "effectiveOnly",
            "effective_only",
            default=False,
        )
        if status_filter:
            assignments = [
                assignment
                for assignment in assignments
                if assignment.status == status_filter
            ]
        if effective_only:
            assignments = [
                assignment
                for assignment in assignments
                if assignment.is_effective()
            ]
        page, counts = _slice_entities(
            assignments,
            offset=offset,
            limit=limit,
        )
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "assignments": [
                        _entity_dict(
                            assignment,
                            include_internal=include_internal,
                            include_metadata=include_metadata,
                        )
                        for assignment in page
                    ],
                    "counts": counts,
                },
                metadata={
                    "includeDeleted": include_deleted,
                    "effectiveOnly": effective_only,
                },
            ),
            200,
        )
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.post("/projects/<project_id>/assignments")
def create_project_assignment(project_id: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        result = _ensure_assignment_from_payload(
            project,
            payload,
            actor_user_id=_actor_user_id(payload),
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                },
                metadata={"idempotent": True},
            ),
            201 if result.action == "created" else 200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.get(
    "/projects/<project_id>/assignments/<assignment_id>"
)
def get_project_assignment(project_id: str, assignment_id: str):
    try:
        include_deleted = _query_bool(
            "includeDeleted",
            "include_deleted",
            default=False,
        )
        project = _resolve_route_project(project_id)
        assignment = _find_assignment(
            project,
            assignment_id,
            include_deleted=include_deleted,
        )
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "assignment": _entity_dict(
                        assignment,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                }
            ),
            200,
        )
    except Exception as exc:
        return _error_response(exc)


@project_access_bp.patch(
    "/projects/<project_id>/assignments/<assignment_id>"
)
def patch_project_assignment(project_id: str, assignment_id: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        assignment = _find_assignment(
            project,
            assignment_id,
            include_deleted=True,
            lock=True,
        )
        result = _patch_assignment(
            project,
            assignment,
            payload,
            actor_user_id=_actor_user_id(payload),
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                }
            ),
            200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


@project_access_bp.delete(
    "/projects/<project_id>/assignments/<assignment_id>"
)
def delete_project_assignment(project_id: str, assignment_id: str):
    payload: dict[str, Any] = {}
    try:
        payload = _request_json()
        project = _resolve_route_project(project_id, lock=True)
        assignment = _find_assignment(
            project,
            assignment_id,
            include_deleted=True,
            lock=True,
        )
        role = _find_role_by_ref(
            project,
            assignment.role_id,
            include_deleted=True,
            required=True,
        )
        assert role is not None
        if role.role_key == DEFAULT_ROLE_OWNER:
            owner_user_id = _project_owner_user_id(project)
            if assignment.user_id == owner_user_id:
                raise ProjectAccessConflictError(
                    "the canonical Owner assignment cannot be revoked; transfer "
                    "project ownership instead",
                    code="owner_assignment_revoke_forbidden",
                    details={
                        "assignmentId": assignment.assignment_id,
                        "ownerUserId": owner_user_id,
                    },
                )

        result = revoke_role_assignment(
            project=project,
            assignment_id=assignment.assignment_id,
            actor_user_id=_actor_user_id(payload),
            reason=payload.get(
                "revocationReason",
                payload.get("reason", _query_str("reason", default="")),
            ),
            session=db.session,
            missing_ok=_query_bool(
                "missingOk",
                "missing_ok",
                default=True,
            ),
            flush=True,
        )
        _commit_session()
        return _json_response(
            _success_body(
                payload={
                    "projectId": project.project_id,
                    "mutation": _mutation_dict(
                        result,
                        include_internal=_query_bool(
                            "includeInternal",
                            "include_internal",
                            default=False,
                        ),
                        include_metadata=_query_bool(
                            "includeMetadata",
                            "include_metadata",
                            default=True,
                        ),
                    ),
                },
                metadata={"idempotent": True, "revocation": True},
            ),
            200,
        )
    except Exception as exc:
        _rollback_session()
        return _error_response(exc)


# ---------------------------------------------------------------------------
# Cache reset route
# ---------------------------------------------------------------------------


@project_access_bp.post("/project-access/_cache/reset")
def reset_project_access_route_caches():
    """Clear pure normalization/import caches; never alter database state."""

    try:
        results: dict[str, Any] = {}
        try:
            results["service"] = clear_project_access_service_caches()
        except Exception as exc:
            results["service"] = {
                "cleared": False,
                "error": _safe_exception_message(exc),
            }

        try:
            from models.project_access import (
                clear_project_access_normalization_caches,
            )

            results["models"] = clear_project_access_normalization_caches()
        except Exception as exc:
            results["models"] = {
                "cleared": False,
                "error": _safe_exception_message(exc),
            }

        return _json_response(
            _success_body(
                payload={
                    "reset": results,
                    "databaseStateChanged": False,
                }
            ),
            200,
        )
    except Exception as exc:
        return _error_response(exc)


__all__ = (
    "PROJECT_ACCESS_ROUTE_ERROR_VERSION",
    "PROJECT_ACCESS_ROUTE_RESPONSE_VERSION",
    "PROJECT_ACCESS_ROUTE_STATUS_VERSION",
    "ROUTE_MODULE_VERSION",
    "ROUTE_SOURCE",
    "project_access_bp",
)
