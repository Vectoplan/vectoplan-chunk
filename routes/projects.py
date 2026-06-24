# services/vectoplan-chunk/routes/projects.py
"""
Project routes for the VECTOPLAN chunk service.

This module is the project-level HTTP adapter for PostgreSQL-backed chunk
project state.

Main responsibilities:
- list chunk projects,
- create chunk projects directly,
- read chunk projects,
- patch chunk projects,
- soft-delete chunk projects,
- return project bootstrap data for the editor,
- create the default Universe + world_spawn for new chunk projects,
- idempotently provision a chunk project for a vectoplan-app project,
- expose route/database/schema/seed readiness diagnostics.

Persistent hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Service boundary:

    vectoplan-app owns App projects.
    vectoplan-chunk owns Chunk projects.

    vectoplan-app calls vectoplan-chunk through INTERNAL_URL.
    vectoplan-chunk creates or returns its own Project/Universe/WorldInstance.
    vectoplan-app stores only returned references:
        chunk_project_id
        chunk_universe_id
        chunk_world_id
        ProjectServiceLink(service="chunk", ...)

Important:
- This file is a route adapter.
- It does not generate chunks.
- It does not execute chunk commands.
- It does not write ChunkSnapshots.
- It does not write ChunkEvents.
- It only persists project/universe/world bootstrap state required by editor use.

World-id rule:
- world_spawn = concrete editable WorldInstance.
- flat        = template/provider id, not concrete world_id.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request

try:
    from sqlalchemy import or_
except Exception:  # pragma: no cover
    or_ = None  # type: ignore[assignment]

from extensions import db, get_database_status
from models import (
    Project,
    Universe,
    WorldInstance,
    get_model_debug_summary,
)

try:
    from src.bootstrap.db_bootstrap import (
        build_db_bootstrap_status,
        build_default_world_invariant_status,
    )
except Exception:  # pragma: no cover - route status must remain importable
    build_db_bootstrap_status = None  # type: ignore[assignment]
    build_default_world_invariant_status = None  # type: ignore[assignment]

try:
    from src.bootstrap.settings import (
        build_env_debug_snapshot,
        build_settings_summary,
    )
except Exception:  # pragma: no cover - route status must remain importable
    build_env_debug_snapshot = None  # type: ignore[assignment]
    build_settings_summary = None  # type: ignore[assignment]

try:
    from src.world_state.provisioning import (
        ChunkProjectProvisioningResult,
        ensure_chunk_project_for_app_project,
        ensure_chunk_project_from_payload,
        preview_chunk_project_ids,
        provisioning_result_to_response_tuple,
    )
except Exception:  # pragma: no cover - route status should still import.
    ChunkProjectProvisioningResult = None  # type: ignore[assignment]
    ensure_chunk_project_for_app_project = None  # type: ignore[assignment]
    ensure_chunk_project_from_payload = None  # type: ignore[assignment]
    preview_chunk_project_ids = None  # type: ignore[assignment]
    provisioning_result_to_response_tuple = None  # type: ignore[assignment]


projects_bp = Blueprint("projects", __name__)

ROUTE_MODULE_VERSION = "0.4.0"
ROUTE_SOURCE = "routes.projects"

PROJECT_RESPONSE_VERSION = "project-response.v1"
PROJECT_LIST_RESPONSE_VERSION = "project-list-response.v1"
PROJECT_CREATE_RESPONSE_VERSION = "project-create-response.v1"
PROJECT_PATCH_RESPONSE_VERSION = "project-patch-response.v1"
PROJECT_DELETE_RESPONSE_VERSION = "project-delete-response.v1"
PROJECT_BOOTSTRAP_RESPONSE_VERSION = "project-bootstrap-response.v1"
PROJECT_STATUS_RESPONSE_VERSION = "projects-route-status-response.v2"
PROJECT_CACHE_RESET_RESPONSE_VERSION = "projects-route-cache-reset-response.v1"
PROJECT_PROVISION_RESPONSE_VERSION = "project-provision-response.v1"
PROJECT_PROVISION_PREVIEW_RESPONSE_VERSION = "project-provision-preview-response.v1"
PROJECT_BY_APP_RESPONSE_VERSION = "project-by-app-response.v1"

ENV_ROUTE_INCLUDE_DEBUG_ERRORS = "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS"
ENV_ROUTE_ALLOW_DEFAULT_PROJECT = "VECTOPLAN_CHUNK_ROUTE_ALLOW_DEFAULT_PROJECT"
ENV_ROUTE_DEFAULT_API_PREFIX = "VECTOPLAN_CHUNK_API_PREFIX"

DEFAULT_PROJECT_ID = "dev-project"
DEFAULT_UNIVERSE_ID = "dev-universe"
DEFAULT_WORLD_ID = "world_spawn"
DEFAULT_TEMPLATE_ID = "flat"
DEFAULT_PROVIDER_ID = "flat"
DEFAULT_PROVIDER_WORLD_ID = "flat"

_DEFAULT_PROJECT_ALIASES = {
    "",
    "default",
    "_default",
    "current",
    "_current",
    "dev",
    "_dev",
}

_EXTERNAL_APP_PROJECT_ID_FIELDS = (
    "external_app_project_id",
    "app_project_public_id",
    "app_project_id",
    "source_project_id",
    "origin_project_id",
    "external_project_id",
)

_PROJECT_ID_FIELDS = (
    "project_id",
    "public_id",
    "slug",
    "key",
)

_PROJECT_PUBLIC_FIELDS = (
    "id",
    "project_id",
    "public_id",
    "slug",
    "name",
    "display_name",
    "title",
    "description",
    "status",
    "external_app_project_id",
    "app_project_public_id",
    "app_project_id",
    "default_universe_id",
    "default_world_id",
    "spawn_world_id",
    "created_at",
    "updated_at",
    "deleted_at",
    "metadata_json",
)


# -----------------------------------------------------------------------------
# Generic safe helpers
# -----------------------------------------------------------------------------

def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return a robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _coerce_string(value: Any, *, fallback: str = "") -> str:
    """Coerce arbitrary value to stripped string."""
    if value is None:
        return str(fallback)

    try:
        text = str(value)
    except Exception:
        text = repr(value)

    text = text.strip()
    return text if text else str(fallback)


def _coerce_int(
    value: Any,
    *,
    fallback: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Coerce arbitrary value to int with optional bounds."""
    try:
        result = int(value)
    except Exception:
        result = fallback

    if minimum is not None:
        result = max(minimum, result)

    if maximum is not None:
        result = min(maximum, result)

    return result


def _coerce_bool(value: Any, *, fallback: bool = False) -> bool:
    """Coerce arbitrary value to bool."""
    if value is None:
        return bool(fallback)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = _coerce_string(value).lower()

    if text in {"1", "true", "yes", "y", "on", "enabled", "enable"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled", "disable"}:
        return False

    return bool(fallback)


def _make_json_safe(value: Any) -> Any:
    """Convert values into JSON-safe structures."""
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            try:
                safe_key = str(key)
            except Exception:
                safe_key = "<unserializable-key>"
            result[safe_key] = _make_json_safe(item)
        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        return [_make_json_safe(item) for item in value]

    try:
        isoformat = getattr(value, "isoformat", None)
        if callable(isoformat):
            return isoformat()
    except Exception:
        pass

    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


def _get_nested_value(payload: Any, path: tuple[str, ...], fallback: Any = None) -> Any:
    """Read nested mapping value defensively."""
    current = payload

    for part in path:
        if not isinstance(current, Mapping):
            return fallback
        current = current.get(part)

    return current if current is not None else fallback


def _get_nested_bool(payload: Any, path: tuple[str, ...], fallback: bool | None = None) -> bool | None:
    """Read nested mapping value as bool."""
    value = _get_nested_value(payload, path, fallback)

    if value is None:
        return fallback

    return _coerce_bool(value, fallback=bool(fallback))


def _get_env_bool(name: str, fallback: bool = False) -> bool:
    """Read boolean env var defensively."""
    try:
        value = os.environ.get(name)
    except Exception:
        return bool(fallback)

    return _coerce_bool(value, fallback=fallback)


def _get_env_string(name: str, fallback: str = "") -> str:
    """Read string env var defensively."""
    try:
        value = os.environ.get(name)
    except Exception:
        return fallback

    return _coerce_string(value, fallback=fallback)


def _get_config_value(name: str, fallback: Any = None) -> Any:
    """Read current_app config defensively."""
    try:
        return current_app.config.get(name, fallback)
    except Exception:
        return fallback


def _get_config_bool(name: str, fallback: bool = False) -> bool:
    """Read bool config defensively."""
    return _coerce_bool(_get_config_value(name, fallback), fallback=fallback)


def _get_config_string(name: str, fallback: str = "") -> str:
    """Read string config defensively."""
    return _coerce_string(_get_config_value(name, fallback), fallback=fallback)


def _get_config_int(
    name: str,
    fallback: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read int config defensively."""
    return _coerce_int(
        _get_config_value(name, fallback),
        fallback=fallback,
        minimum=minimum,
        maximum=maximum,
    )


def _get_query_value(*names: str, fallback: Any = None) -> Any:
    """Read first available query param."""
    for name in names:
        try:
            if name in request.args:
                return request.args.get(name)
        except Exception:
            continue

    return fallback


def _get_query_bool(*names: str, fallback: bool = False) -> bool:
    """Read query bool."""
    value = _get_query_value(*names, fallback=None)

    if value is None:
        return bool(fallback)

    return _coerce_bool(value, fallback=fallback)


def _get_query_string(*names: str, fallback: str = "") -> str:
    """Read query string."""
    value = _get_query_value(*names, fallback=None)

    if value is None:
        return str(fallback)

    return _coerce_string(value, fallback=fallback)


def _get_query_int(
    *names: str,
    fallback: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read query integer."""
    value = _get_query_value(*names, fallback=None)

    if value is None:
        return _coerce_int(
            fallback,
            fallback=fallback,
            minimum=minimum,
            maximum=maximum,
        )

    return _coerce_int(value, fallback=fallback, minimum=minimum, maximum=maximum)


def _get_json_body() -> dict[str, Any]:
    """Read request JSON body defensively."""
    try:
        payload = request.get_json(silent=True)
    except Exception:
        payload = None

    if payload is None:
        return {}

    if not isinstance(payload, Mapping):
        raise ValueError("Request body must be a JSON object.")

    return dict(payload)


def _include_debug_errors() -> bool:
    """Return whether debug error details should be included."""
    query_debug = _get_query_bool(
        "debug",
        "includeDebug",
        "include_debug",
        fallback=False,
    )

    env_debug = _get_env_bool(
        ENV_ROUTE_INCLUDE_DEBUG_ERRORS,
        fallback=False,
    )

    try:
        app_debug = bool(current_app.debug)
    except Exception:
        app_debug = False

    return bool(query_debug or env_debug or app_debug)


def _json_response(body: Mapping[str, Any], status_code: int = 200):
    """Return JSON response."""
    return jsonify(_make_json_safe(dict(body))), int(status_code)


def _route_metadata(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build route metadata."""
    metadata = {
        "routeSource": ROUTE_SOURCE,
        "routeModuleVersion": ROUTE_MODULE_VERSION,
    }

    if extra:
        metadata.update(_make_json_safe(dict(extra)))

    return metadata


def _ok_response(
    *,
    response_version: str,
    payload: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build standard ok response."""
    body: dict[str, Any] = {
        "ok": True,
        "responseVersion": response_version,
    }

    if payload:
        body.update(_make_json_safe(dict(payload)))

    body["metadata"] = _route_metadata(metadata)
    return body


def _error_body(
    error: BaseException | Any,
    *,
    code: str = "route_error",
    status_code: int = 500,
) -> tuple[dict[str, Any], int]:
    """Build standard error body."""
    include_debug = _include_debug_errors()
    message = _safe_exception_message(error)

    body: dict[str, Any] = {
        "ok": False,
        "responseVersion": "error-response.v1",
        "error": {
            "code": code,
            "message": message,
        },
        "metadata": _route_metadata(),
    }

    if include_debug:
        body["error"]["debug"] = {
            "type": type(error).__name__,
            "repr": repr(error),
        }

    return body, int(status_code)


def _error_response(
    error: BaseException | Any,
    *,
    code: str = "route_error",
    status_code: int = 500,
):
    """Return JSON error response."""
    body, status = _error_body(error, code=code, status_code=status_code)
    return _json_response(body, status)


def _model_columns(model_or_obj: Any) -> set[str]:
    """Return SQLAlchemy column names for a model or model instance."""
    model = model_or_obj if isinstance(model_or_obj, type) else type(model_or_obj)

    try:
        table = getattr(model, "__table__", None)
        columns = getattr(table, "columns", None)
        if columns is not None:
            return {str(column.name) for column in columns}
    except Exception:
        return set()

    return set()


def _model_supports_attr(model_or_obj: Any, name: str) -> bool:
    """Return whether a SQLAlchemy model likely supports a field."""
    if name in _model_columns(model_or_obj):
        return True

    try:
        return hasattr(model_or_obj, name)
    except Exception:
        return False


def _model_value(obj: Any, name: str, fallback: Any = None) -> Any:
    """Read model field defensively."""
    try:
        return getattr(obj, name, fallback)
    except Exception:
        return fallback


def _serialize_model_fields(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """Serialize selected model fields if they exist."""
    if obj is None:
        return {}

    result: dict[str, Any] = {}

    for field_name in fields:
        if not _model_supports_attr(obj, field_name):
            continue

        value = _model_value(obj, field_name)
        result[field_name] = _make_json_safe(value)

    return result


def _set_model_value_if_supported(obj: Any, name: str, value: Any, *, overwrite: bool = True) -> bool:
    """Set model field when supported."""
    if obj is None or not _model_supports_attr(obj, name):
        return False

    try:
        current = getattr(obj, name, None)
    except Exception:
        current = None

    if not overwrite and current not in (None, "", {}, []):
        return False

    if current == value:
        return False

    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False


def _provider_like_world_id(value: Any) -> bool:
    """Return whether a value looks like template/provider instead of concrete world id."""
    text = _coerce_string(value).lower()
    if not text:
        return False

    template_id = _get_default_template_id().lower()
    provider_id = _get_default_provider_id().lower()
    provider_world_id = _get_default_provider_world_id().lower()

    return text in {
        DEFAULT_TEMPLATE_ID,
        DEFAULT_PROVIDER_ID,
        DEFAULT_PROVIDER_WORLD_ID,
        template_id,
        provider_id,
        provider_world_id,
    }


# -----------------------------------------------------------------------------
# Project/world helpers
# -----------------------------------------------------------------------------

def _normalize_project_route_id(project_id: str | None) -> tuple[str | None, bool]:
    """
    Normalize route project id.

    Returns:
        (project_id, allow_default_project)
    """
    text = _coerce_string(project_id)

    if text.lower() in _DEFAULT_PROJECT_ALIASES:
        return None, True

    return text, False


def _get_default_project_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", DEFAULT_PROJECT_ID)


def _get_default_project_slug() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG", _get_default_project_id())


def _get_default_project_name() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME", DEFAULT_PROJECT_NAME)


def _get_default_universe_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", DEFAULT_UNIVERSE_ID)


def _get_default_universe_slug() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG", _get_default_universe_id())


def _get_default_universe_name() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME", DEFAULT_UNIVERSE_NAME)


def _get_default_template_id() -> str:
    return _get_config_string(
        "VECTOPLAN_CHUNK_DEFAULT_TEMPLATE_ID",
        _get_config_string("VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID", DEFAULT_TEMPLATE_ID),
    )


def _get_default_provider_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", DEFAULT_PROVIDER_ID)


def _get_default_provider_world_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", DEFAULT_PROVIDER_WORLD_ID)


def _get_default_world_id() -> str:
    raw_world_id = _get_config_string(
        "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID",
        _get_config_string(
            "VECTOPLAN_CHUNK_DEFAULT_WORLD_ID",
            DEFAULT_WORLD_ID,
        ),
    )

    if _provider_like_world_id(raw_world_id):
        return DEFAULT_WORLD_ID

    return raw_world_id or DEFAULT_WORLD_ID


def _get_default_world_slug() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG", DEFAULT_WORLD_SLUG)


def _get_default_world_name() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME", DEFAULT_WORLD_NAME)


def _resolve_effective_project_id(
    project_id: str | None,
    *,
    allow_default_project: bool = False,
) -> str:
    """Resolve project id with optional default alias support."""
    normalized_project_id, route_allows_default = _normalize_project_route_id(project_id)

    if normalized_project_id:
        return normalized_project_id

    if allow_default_project or route_allows_default:
        return _get_default_project_id()

    raise ValueError("projectId is required.")


def _project_query_by_identifier(project_id: str, *, include_deleted: bool = False):
    """Build query by project identifier across supported identifier fields."""
    filters = []

    for field_name in _PROJECT_ID_FIELDS:
        if _model_supports_attr(Project, field_name):
            try:
                filters.append(getattr(Project, field_name) == project_id)
            except Exception:
                continue

    if not filters and _model_supports_attr(Project, "project_id"):
        filters.append(Project.project_id == project_id)

    if not filters:
        raise RuntimeError("Project model has no supported project identifier field.")

    query = Project.query

    if or_ is not None and len(filters) > 1:
        query = query.filter(or_(*filters))
    else:
        query = query.filter(filters[0])

    if not include_deleted and _model_supports_attr(Project, "deleted_at"):
        query = query.filter(Project.deleted_at.is_(None))

    return query


def _get_project_or_404(project_id: str, *, include_deleted: bool = False) -> Project:
    """Load project by public project id."""
    project = _project_query_by_identifier(
        project_id,
        include_deleted=include_deleted,
    ).one_or_none()

    if project is None:
        raise LookupError(f"Project '{project_id}' was not found.")

    return project


def _get_project_public_id(project: Project) -> str:
    """Return stable public chunk project id."""
    for field_name in _PROJECT_ID_FIELDS:
        value = _coerce_string(_model_value(project, field_name))
        if value:
            return value

    value = _coerce_string(_model_value(project, "id"))
    if value:
        return value

    raise LookupError("Project has no public identifier.")


def _get_universe_public_id(universe: Universe) -> str:
    """Return stable universe id."""
    for field_name in ("universe_id", "public_id", "slug", "key"):
        value = _coerce_string(_model_value(universe, field_name))
        if value:
            return value

    value = _coerce_string(_model_value(universe, "id"))
    if value:
        return value

    raise LookupError("Universe has no public identifier.")


def _get_world_public_id(world: WorldInstance) -> str:
    """Return stable world id."""
    for field_name in ("world_id", "public_id", "slug", "key"):
        value = _coerce_string(_model_value(world, field_name))
        if value:
            return value

    value = _coerce_string(_model_value(world, "id"))
    if value:
        return value

    raise LookupError("World has no public identifier.")


def _query_project_by_app_project_id(
    app_project_public_id: str,
    *,
    include_deleted: bool = False,
) -> Project | None:
    """Find a chunk project by external vectoplan-app project id."""
    app_project_public_id = _coerce_string(app_project_public_id)

    if not app_project_public_id:
        return None

    filters = []

    for field_name in _EXTERNAL_APP_PROJECT_ID_FIELDS:
        if _model_supports_attr(Project, field_name):
            try:
                filters.append(getattr(Project, field_name) == app_project_public_id)
            except Exception:
                continue

    query = Project.query

    if filters:
        if or_ is not None and len(filters) > 1:
            query = query.filter(or_(*filters))
        else:
            query = query.filter(filters[0])
    else:
        if not _model_supports_attr(Project, "metadata_json"):
            return None

        try:
            candidates = Project.query.all()
        except Exception:
            return None

        for project in candidates:
            if not include_deleted and _model_supports_attr(project, "deleted_at"):
                if _model_value(project, "deleted_at") is not None:
                    continue

            metadata = _model_value(project, "metadata_json", {}) or {}
            if isinstance(metadata, Mapping):
                if _coerce_string(metadata.get("externalAppProjectId")) == app_project_public_id:
                    return project
                if _coerce_string(metadata.get("external_app_project_id")) == app_project_public_id:
                    return project

        return None

    if not include_deleted and _model_supports_attr(Project, "deleted_at"):
        query = query.filter(Project.deleted_at.is_(None))

    return query.one_or_none()


def _get_project_default_universe(
    project: Project,
    *,
    include_deleted: bool = False,
) -> Universe:
    """Load default universe for project."""
    project_db_id = _model_value(project, "id")
    universe_id = _model_value(project, "default_universe_id") or _get_default_universe_id()

    query = Universe.query

    if _model_supports_attr(Universe, "project_db_id") and project_db_id is not None:
        query = query.filter(Universe.project_db_id == project_db_id)
    elif _model_supports_attr(Universe, "project_id"):
        query = query.filter(Universe.project_id == _get_project_public_id(project))

    if _model_supports_attr(Universe, "universe_id"):
        query = query.filter(Universe.universe_id == universe_id)

    if not include_deleted and _model_supports_attr(Universe, "deleted_at"):
        query = query.filter(Universe.deleted_at.is_(None))

    universe = query.one_or_none()

    if universe is None:
        fallback_query = Universe.query

        if _model_supports_attr(Universe, "project_db_id") and project_db_id is not None:
            fallback_query = fallback_query.filter(Universe.project_db_id == project_db_id)
        elif _model_supports_attr(Universe, "project_id"):
            fallback_query = fallback_query.filter(Universe.project_id == _get_project_public_id(project))

        if not include_deleted and _model_supports_attr(Universe, "deleted_at"):
            fallback_query = fallback_query.filter(Universe.deleted_at.is_(None))

        if _model_supports_attr(Universe, "created_at"):
            fallback_query = fallback_query.order_by(Universe.created_at.asc())

        universe = fallback_query.first()

    if universe is None:
        raise LookupError(f"Project '{_get_project_public_id(project)}' has no universe.")

    return universe


def _get_universe_spawn_world(
    universe: Universe,
    *,
    include_deleted: bool = False,
) -> WorldInstance:
    """Load spawn/default world for universe."""
    universe_db_id = _model_value(universe, "id")
    world_id = (
        _model_value(universe, "spawn_world_id")
        or _model_value(universe, "default_world_id")
        or _get_default_world_id()
    )

    if _provider_like_world_id(world_id):
        world_id = _get_default_world_id()

    query = WorldInstance.query

    if _model_supports_attr(WorldInstance, "universe_db_id") and universe_db_id is not None:
        query = query.filter(WorldInstance.universe_db_id == universe_db_id)
    elif _model_supports_attr(WorldInstance, "universe_id"):
        query = query.filter(WorldInstance.universe_id == _get_universe_public_id(universe))

    if _model_supports_attr(WorldInstance, "world_id"):
        query = query.filter(WorldInstance.world_id == world_id)

    if not include_deleted and _model_supports_attr(WorldInstance, "deleted_at"):
        query = query.filter(WorldInstance.deleted_at.is_(None))

    world = query.one_or_none()

    default_world_id = _model_value(universe, "default_world_id")

    if world is None and default_world_id and default_world_id != world_id and not _provider_like_world_id(default_world_id):
        fallback_query = WorldInstance.query

        if _model_supports_attr(WorldInstance, "universe_db_id") and universe_db_id is not None:
            fallback_query = fallback_query.filter(WorldInstance.universe_db_id == universe_db_id)
        elif _model_supports_attr(WorldInstance, "universe_id"):
            fallback_query = fallback_query.filter(WorldInstance.universe_id == _get_universe_public_id(universe))

        if _model_supports_attr(WorldInstance, "world_id"):
            fallback_query = fallback_query.filter(WorldInstance.world_id == default_world_id)

        if not include_deleted and _model_supports_attr(WorldInstance, "deleted_at"):
            fallback_query = fallback_query.filter(WorldInstance.deleted_at.is_(None))

        world = fallback_query.one_or_none()

    if world is None:
        fallback_query = WorldInstance.query

        if _model_supports_attr(WorldInstance, "universe_db_id") and universe_db_id is not None:
            fallback_query = fallback_query.filter(WorldInstance.universe_db_id == universe_db_id)
        elif _model_supports_attr(WorldInstance, "universe_id"):
            fallback_query = fallback_query.filter(WorldInstance.universe_id == _get_universe_public_id(universe))

        if not include_deleted and _model_supports_attr(WorldInstance, "deleted_at"):
            fallback_query = fallback_query.filter(WorldInstance.deleted_at.is_(None))

        if _model_supports_attr(WorldInstance, "created_at"):
            fallback_query = fallback_query.order_by(WorldInstance.created_at.asc())

        world = fallback_query.first()

    if world is None:
        raise LookupError(f"Universe '{_get_universe_public_id(universe)}' has no world.")

    return world


def _build_route_hints(
    *,
    project_id: str,
    world_id: str,
    api_prefix: str = "",
) -> dict[str, str]:
    """Build editor route hints."""
    prefix = _coerce_string(api_prefix).rstrip("/")

    return {
        "projectBootstrap": f"{prefix}/projects/{project_id}/bootstrap",
        "project": f"{prefix}/projects/{project_id}",
        "worlds": f"{prefix}/projects/{project_id}/worlds",
        "world": f"{prefix}/projects/{project_id}/worlds/{world_id}",
        "blocks": f"{prefix}/projects/{project_id}/worlds/{world_id}/blocks",
        "chunk": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks",
        "chunks": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks",
        "chunksBatch": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks/batch",
        "commands": f"{prefix}/projects/{project_id}/worlds/{world_id}/commands",
    }


def _serialize_project_bootstrap(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    include_route_hints: bool = True,
    include_worlds: bool = True,
    include_metadata: bool = True,
    api_prefix: str = "",
) -> dict[str, Any]:
    """Serialize project bootstrap response."""
    project_id = _get_project_public_id(project)
    universe_id = _get_universe_public_id(universe)
    world_id = _get_world_public_id(world)

    if hasattr(project, "to_dict"):
        project_dict = project.to_dict(include_internal=False, include_metadata=include_metadata)
    else:
        project_dict = _serialize_model_fields(project, _PROJECT_PUBLIC_FIELDS)

    if hasattr(universe, "to_dict"):
        universe_dict = universe.to_dict(
            include_internal=False,
            include_metadata=include_metadata,
            project_id=project_id,
        )
    else:
        universe_dict = _serialize_model_fields(
            universe,
            (
                "id",
                "universe_id",
                "public_id",
                "slug",
                "name",
                "status",
                "default_world_id",
                "spawn_world_id",
                "metadata_json",
            ),
        )

    if hasattr(world, "to_dict"):
        world_dict = world.to_dict(
            include_internal=False,
            include_metadata=include_metadata,
            project_id=project_id,
            universe_id=universe_id,
        )
    else:
        world_dict = _serialize_model_fields(
            world,
            (
                "id",
                "world_id",
                "public_id",
                "slug",
                "name",
                "status",
                "template_id",
                "provider_world_id",
                "generator_type",
                "generator_version",
                "projection_type",
                "topology_type",
                "coordinate_system",
                "chunk_size",
                "cell_size",
                "surface_y",
                "min_y",
                "max_y",
                "block_registry_id",
                "block_registry_version",
                "metadata_json",
            ),
        )

    body: dict[str, Any] = {
        "projectId": project_id,
        "chunkProjectId": project_id,
        "universeId": universe_id,
        "chunkUniverseId": universe_id,
        "defaultWorldId": _model_value(universe, "default_world_id") or world_id,
        "spawnWorldId": _model_value(universe, "spawn_world_id") or world_id,
        "chunkWorldId": world_id,
        "project": project_dict,
        "universe": universe_dict,
        "spawnWorld": world_dict,
        "world": world_dict,
    }

    if include_worlds:
        worlds_query = WorldInstance.query

        if _model_supports_attr(WorldInstance, "universe_db_id"):
            worlds_query = worlds_query.filter(WorldInstance.universe_db_id == _model_value(universe, "id"))
        elif _model_supports_attr(WorldInstance, "universe_id"):
            worlds_query = worlds_query.filter(WorldInstance.universe_id == universe_id)

        if _model_supports_attr(WorldInstance, "deleted_at"):
            worlds_query = worlds_query.filter(WorldInstance.deleted_at.is_(None))

        if _model_supports_attr(WorldInstance, "created_at"):
            worlds_query = worlds_query.order_by(WorldInstance.created_at.asc())

        worlds = worlds_query.all()

        body["worlds"] = [
            item.to_dict(
                include_internal=False,
                include_metadata=include_metadata,
                project_id=project_id,
                universe_id=universe_id,
            )
            if hasattr(item, "to_dict")
            else _serialize_model_fields(
                item,
                (
                    "id",
                    "world_id",
                    "public_id",
                    "slug",
                    "name",
                    "status",
                    "metadata_json",
                ),
            )
            for item in worlds
        ]
        body["counts"] = {
            "worlds": len(body["worlds"]),
        }

    if include_route_hints:
        body["routeHints"] = _build_route_hints(
            project_id=project_id,
            world_id=world_id,
            api_prefix=api_prefix,
        )

    body["context"] = {
        "projectScoped": True,
        "worldId": world_id,
        "chunkWorldId": world_id,
        "templateId": _model_value(world, "template_id") or _model_value(world, "world_template_id"),
        "providerId": _model_value(world, "provider_id"),
        "providerWorldId": _model_value(world, "provider_world_id"),
        "generatorType": _model_value(world, "generator_type"),
        "generatorVersion": _model_value(world, "generator_version"),
        "blockRegistryId": _model_value(world, "block_registry_id"),
        "blockRegistryVersion": _model_value(world, "block_registry_version"),
        "chunkSize": _model_value(world, "chunk_size"),
        "cellSize": _model_value(world, "cell_size"),
    }

    return body


def _create_default_universe_for_project(
    project: Project,
    *,
    payload: Mapping[str, Any] | None = None,
    created_by_user_id: Optional[str] = None,
) -> Universe:
    """Create default Universe for a project."""
    payload = payload or {}

    requested_world_id = (
        payload.get("worldId")
        or payload.get("world_id")
        or payload.get("spawnWorldId")
        or payload.get("spawn_world_id")
        or _get_default_world_id()
    )

    if _provider_like_world_id(requested_world_id):
        requested_world_id = _get_default_world_id()

    universe_id = (
        payload.get("universeId")
        or payload.get("universe_id")
        or payload.get("defaultUniverseId")
        or payload.get("default_universe_id")
        or _model_value(project, "default_universe_id")
        or _get_default_universe_id()
    )

    universe = Universe.create(
        project_db_id=_model_value(project, "id"),
        universe_id=universe_id,
        slug=payload.get("universeSlug") or payload.get("universe_slug") or _get_default_universe_slug(),
        name=payload.get("universeName") or payload.get("universe_name") or _get_default_universe_name(),
        default_world_id=requested_world_id,
        spawn_world_id=requested_world_id,
        created_by_user_id=created_by_user_id,
        metadata_json={
            "createdByRoute": ROUTE_SOURCE,
            "createdAsDefault": True,
            "defaultWorldId": requested_world_id,
            "spawnWorldId": requested_world_id,
        },
    )

    db.session.add(universe)
    db.session.flush()

    if not _model_value(project, "default_universe_id"):
        if hasattr(project, "set_default_universe_id"):
            project.set_default_universe_id(universe.universe_id, updated_by_user_id=created_by_user_id)
        elif _model_supports_attr(project, "default_universe_id"):
            project.default_universe_id = universe.universe_id

    if _model_supports_attr(project, "default_world_id"):
        project.default_world_id = requested_world_id
    if _model_supports_attr(project, "spawn_world_id"):
        project.spawn_world_id = requested_world_id

    return universe


def _create_default_world_for_universe(
    universe: Universe,
    *,
    payload: Mapping[str, Any] | None = None,
    created_by_user_id: Optional[str] = None,
) -> WorldInstance:
    """Create default world_spawn for a universe."""
    payload = payload or {}

    world_id = payload.get("worldId") or payload.get("world_id") or _get_default_world_id()

    if _provider_like_world_id(world_id):
        world_id = _get_default_world_id()

    create_flat_spawn = getattr(WorldInstance, "create_flat_spawn", None)
    if callable(create_flat_spawn):
        try:
            world = create_flat_spawn(
                project_db_id=_model_value(universe, "project_db_id"),
                universe_db_id=_model_value(universe, "id"),
                world_id=world_id,
                slug=payload.get("worldSlug") or payload.get("world_slug") or _get_default_world_slug(),
                name=payload.get("worldName") or payload.get("world_name") or _get_default_world_name(),
                created_by_user_id=created_by_user_id,
                source_service=ROUTE_SOURCE,
                external_ref=world_id,
                metadata_json={
                    "createdByRoute": ROUTE_SOURCE,
                    "createdAsSpawnWorld": True,
                    "templateId": payload.get("templateId") or payload.get("template_id") or _get_default_template_id(),
                    "providerWorldId": payload.get("providerWorldId") or payload.get("provider_world_id") or _get_default_provider_world_id(),
                },
            )
        except TypeError:
            world = None
    else:
        world = None

    if world is None:
        world = WorldInstance.create(
            project_db_id=_model_value(universe, "project_db_id"),
            universe_db_id=_model_value(universe, "id"),
            world_id=world_id,
            slug=payload.get("worldSlug") or payload.get("world_slug") or _get_default_world_slug(),
            name=payload.get("worldName") or payload.get("world_name") or _get_default_world_name(),
            world_type=payload.get("worldType") or payload.get("world_type") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE", "runtime-world"),
            world_role=payload.get("worldRole") or payload.get("world_role") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE", "default_spawn"),
            world_scope=payload.get("worldScope") or payload.get("world_scope") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE", "project"),
            template_id=payload.get("templateId") or payload.get("template_id") or _get_default_template_id(),
            provider_id=payload.get("providerId") or payload.get("provider_id") or _get_default_provider_id(),
            provider_world_id=payload.get("providerWorldId") or payload.get("provider_world_id") or _get_default_provider_world_id(),
            generator_type=payload.get("generatorType") or payload.get("generator_type") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE", "flat-world"),
            generator_version=payload.get("generatorVersion") or payload.get("generator_version") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION", "1"),
            projection_type=payload.get("projectionType") or payload.get("projection_type") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE", "flat-local-v1"),
            topology_type=payload.get("topologyType") or payload.get("topology_type") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE", "flat-unbounded-v1"),
            coordinate_system=payload.get("coordinateSystem") or payload.get("coordinate_system") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM", "vectoplan-world-y-up-v1"),
            chunk_size=payload.get("chunkSize") or payload.get("chunk_size") or _get_config_int("VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE", 16, minimum=1),
            cell_size=payload.get("cellSize") or payload.get("cell_size") or _get_config_value("VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE", 1.0),
            surface_y=payload.get("surfaceY") if "surfaceY" in payload else payload.get("surface_y", _get_config_int("VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y", 0)),
            min_y=payload.get("minY") if "minY" in payload else payload.get("min_y", _get_config_int("VECTOPLAN_CHUNK_DEFAULT_MIN_Y", -8)),
            max_y=payload.get("maxY") if "maxY" in payload else payload.get("max_y", _get_config_int("VECTOPLAN_CHUNK_DEFAULT_MAX_Y", 64)),
            seed=payload.get("seed") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_SEED", "dev-seed"),
            block_registry_id=payload.get("blockRegistryId") or payload.get("block_registry_id") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", "debug-blocks"),
            block_registry_version=payload.get("blockRegistryVersion") or payload.get("block_registry_version") or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", "1"),
            spawn_x=payload.get("spawnX") if "spawnX" in payload else payload.get("spawn_x", _get_config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_X", 0)),
            spawn_y=payload.get("spawnY") if "spawnY" in payload else payload.get("spawn_y", _get_config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y", 2)),
            spawn_z=payload.get("spawnZ") if "spawnZ" in payload else payload.get("spawn_z", _get_config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z", 0)),
            created_by_user_id=created_by_user_id,
            source_service=ROUTE_SOURCE,
            external_ref=world_id,
            metadata_json={
                "createdByRoute": ROUTE_SOURCE,
                "createdAsSpawnWorld": True,
            },
        )

    db.session.add(world)
    db.session.flush()

    if hasattr(world, "ensure_bootstrap_defaults"):
        try:
            world.ensure_bootstrap_defaults(updated_by_user_id=created_by_user_id)
        except Exception:
            pass

    changed_universe = False
    if not _model_value(universe, "default_world_id"):
        if hasattr(universe, "set_default_world_id"):
            universe.set_default_world_id(world.world_id, updated_by_user_id=created_by_user_id)
        elif _model_supports_attr(universe, "default_world_id"):
            universe.default_world_id = world.world_id
        changed_universe = True

    if not _model_value(universe, "spawn_world_id"):
        if hasattr(universe, "set_spawn_world_id"):
            universe.set_spawn_world_id(world.world_id, updated_by_user_id=created_by_user_id)
        elif _model_supports_attr(universe, "spawn_world_id"):
            universe.spawn_world_id = world.world_id
        changed_universe = True

    if changed_universe:
        db.session.add(universe)

    return world


def _create_project_graph_from_payload(payload: Mapping[str, Any]) -> tuple[Project, Universe, WorldInstance]:
    """
    Create Project + Universe + WorldInstance in one transaction context.

    Caller must commit or rollback.
    """
    created_by_user_id = (
        payload.get("createdByUserId")
        or payload.get("created_by_user_id")
        or payload.get("userId")
        or payload.get("user_id")
    )

    if hasattr(Project, "from_create_payload"):
        project = Project.from_create_payload(
            payload,
            created_by_user_id=created_by_user_id,
        )
    else:
        project = Project()
        if _model_supports_attr(project, "project_id"):
            project.project_id = payload.get("projectId") or payload.get("project_id") or _get_default_project_id()
        if _model_supports_attr(project, "slug"):
            project.slug = payload.get("slug") or payload.get("projectSlug") or _get_default_project_slug()
        if _model_supports_attr(project, "name"):
            project.name = payload.get("name") or payload.get("projectName") or _get_default_project_name()
        if _model_supports_attr(project, "status"):
            project.status = payload.get("status") or "active"

    if _model_supports_attr(project, "slug") and _model_value(project, "slug") is None:
        project.slug = _get_project_public_id(project)

    default_world_id = (
        payload.get("worldId")
        or payload.get("world_id")
        or payload.get("spawnWorldId")
        or payload.get("spawn_world_id")
        or _get_default_world_id()
    )
    if _provider_like_world_id(default_world_id):
        default_world_id = _get_default_world_id()

    if _model_supports_attr(project, "default_universe_id") and _model_value(project, "default_universe_id") is None:
        project.default_universe_id = (
            payload.get("universeId")
            or payload.get("universe_id")
            or payload.get("defaultUniverseId")
            or payload.get("default_universe_id")
            or _get_default_universe_id()
        )

    _set_model_value_if_supported(project, "default_world_id", default_world_id, overwrite=True)
    _set_model_value_if_supported(project, "spawn_world_id", default_world_id, overwrite=True)

    db.session.add(project)
    db.session.flush()

    universe = _create_default_universe_for_project(
        project,
        payload=payload,
        created_by_user_id=created_by_user_id,
    )

    world = _create_default_world_for_universe(
        universe,
        payload=payload,
        created_by_user_id=created_by_user_id,
    )

    return project, universe, world


def _query_projects(
    *,
    include_deleted: bool = False,
    include_archived: bool = True,
    search: str = "",
):
    """Build project list query."""
    query = Project.query

    if not include_deleted and _model_supports_attr(Project, "deleted_at"):
        query = query.filter(Project.deleted_at.is_(None))

    if not include_archived and _model_supports_attr(Project, "status"):
        query = query.filter(Project.status != "archived")

    if search:
        like = f"%{search}%"
        search_filters = []

        for field_name in ("project_id", "public_id", "slug", "name", "display_name", "title"):
            if _model_supports_attr(Project, field_name):
                try:
                    search_filters.append(getattr(Project, field_name).ilike(like))
                except Exception:
                    continue

        if search_filters:
            if or_ is not None and len(search_filters) > 1:
                query = query.filter(or_(*search_filters))
            else:
                query = query.filter(search_filters[0])

    return query


def _serialize_project_detail(
    project: Project,
    *,
    include_universes: bool = True,
    include_worlds: bool = True,
    include_metadata: bool = True,
    include_internal: bool = False,
) -> dict[str, Any]:
    """Serialize project with optional universes/worlds."""
    if hasattr(project, "to_dict"):
        result = project.to_dict(
            include_internal=include_internal,
            include_metadata=include_metadata,
        )
    else:
        result = _serialize_model_fields(project, _PROJECT_PUBLIC_FIELDS)

    if not include_universes:
        return result

    project_db_id = _model_value(project, "id")
    project_public_id = _get_project_public_id(project)

    universes_query = Universe.query

    if _model_supports_attr(Universe, "project_db_id") and project_db_id is not None:
        universes_query = universes_query.filter_by(project_db_id=project_db_id)
    elif _model_supports_attr(Universe, "project_id"):
        universes_query = universes_query.filter_by(project_id=project_public_id)

    if _model_supports_attr(Universe, "deleted_at"):
        universes_query = universes_query.filter(Universe.deleted_at.is_(None))

    if _model_supports_attr(Universe, "created_at"):
        universes_query = universes_query.order_by(Universe.created_at.asc())

    universes = universes_query.all()

    result["universes"] = []
    result["universeCount"] = len(universes)

    for universe in universes:
        universe_public_id = _get_universe_public_id(universe)

        if hasattr(universe, "to_dict"):
            universe_item = universe.to_dict(
                include_internal=include_internal,
                include_metadata=include_metadata,
                project_id=project_public_id,
            )
        else:
            universe_item = _serialize_model_fields(
                universe,
                (
                    "id",
                    "universe_id",
                    "public_id",
                    "slug",
                    "name",
                    "status",
                    "default_world_id",
                    "spawn_world_id",
                    "metadata_json",
                ),
            )

        if include_worlds:
            worlds_query = WorldInstance.query

            if _model_supports_attr(WorldInstance, "universe_db_id"):
                worlds_query = worlds_query.filter_by(universe_db_id=_model_value(universe, "id"))
            elif _model_supports_attr(WorldInstance, "universe_id"):
                worlds_query = worlds_query.filter_by(universe_id=universe_public_id)

            if _model_supports_attr(WorldInstance, "deleted_at"):
                worlds_query = worlds_query.filter(WorldInstance.deleted_at.is_(None))

            if _model_supports_attr(WorldInstance, "created_at"):
                worlds_query = worlds_query.order_by(WorldInstance.created_at.asc())

            worlds = worlds_query.all()

            universe_item["worlds"] = [
                world.to_dict(
                    include_internal=include_internal,
                    include_metadata=include_metadata,
                    project_id=project_public_id,
                    universe_id=universe_public_id,
                )
                if hasattr(world, "to_dict")
                else _serialize_model_fields(
                    world,
                    (
                        "id",
                        "world_id",
                        "public_id",
                        "slug",
                        "name",
                        "status",
                        "template_id",
                        "provider_world_id",
                        "metadata_json",
                    ),
                )
                for world in worlds
            ]
            universe_item["worldCount"] = len(worlds)

        result["universes"].append(universe_item)

    return result


def _provisioning_available() -> bool:
    """Return whether provisioning module is available."""
    return bool(
        ensure_chunk_project_for_app_project is not None
        and ensure_chunk_project_from_payload is not None
        and preview_chunk_project_ids is not None
    )


def _provisioning_unavailable_response():
    """Return standardized provisioning unavailable response."""
    return _json_response(
        _ok_response(
            response_version=PROJECT_PROVISION_RESPONSE_VERSION,
            payload={
                "provisioningAvailable": False,
                "ok": False,
                "error": {
                    "code": "project_provisioning_module_unavailable",
                    "message": "Project provisioning module is not available.",
                },
            },
            metadata={
                "provisioningAvailable": False,
            },
        ),
        500,
    )


def _wrap_provisioning_result(result: Any, *, metadata: Mapping[str, Any] | None = None):
    """Convert provisioning result to HTTP response."""
    if hasattr(result, "to_dict"):
        body = result.to_dict()
        status_code = int(getattr(result, "status_code", 200))
    elif provisioning_result_to_response_tuple is not None:
        body, status_code = provisioning_result_to_response_tuple(result)
    elif isinstance(result, Mapping):
        body = dict(result)
        status_code = 200 if body.get("ok", False) else 500
    else:
        body = {
            "ok": False,
            "code": "invalid_provisioning_result",
            "message": "Provisioning returned an invalid result.",
        }
        status_code = 500

    body.setdefault("responseVersion", PROJECT_PROVISION_RESPONSE_VERSION)
    body["metadata"] = _route_metadata(
        {
            "provisioningAvailable": _provisioning_available(),
            **dict(metadata or {}),
        }
    )

    return _json_response(body, status_code)


# -----------------------------------------------------------------------------
# Status helpers/routes
# -----------------------------------------------------------------------------

def _build_model_status_safe(*, include_models: bool) -> dict[str, Any] | None:
    """Build model debug status safely."""
    if not include_models:
        return None

    try:
        return get_model_debug_summary()
    except Exception as exc:
        return {
            "ok": False,
            "error": _safe_exception_message(exc),
            "exceptionType": exc.__class__.__name__,
        }


def _build_counts_safe(*, include_counts: bool) -> dict[str, Any] | None:
    """Build table counts safely."""
    if not include_counts:
        return None

    try:
        project_query = Project.query
        active_project_query = Project.query

        if _model_supports_attr(Project, "deleted_at"):
            active_project_query = active_project_query.filter(Project.deleted_at.is_(None))

        return {
            "projects": project_query.count(),
            "activeProjects": active_project_query.count(),
            "universes": Universe.query.count(),
            "worlds": WorldInstance.query.count(),
        }
    except Exception as exc:
        return {
            "error": _safe_exception_message(exc),
            "exceptionType": exc.__class__.__name__,
        }


def _build_settings_summary_safe(*, include_settings: bool) -> dict[str, Any] | None:
    """Build bootstrap settings summary safely."""
    if not include_settings:
        return None

    if build_settings_summary is None:
        return {
            "ok": False,
            "error": "build_settings_summary unavailable.",
        }

    try:
        return build_settings_summary(current_app)
    except Exception as exc:
        return {
            "ok": False,
            "error": _safe_exception_message(exc),
            "exceptionType": exc.__class__.__name__,
        }


def _build_env_snapshot_safe(*, include_env: bool) -> dict[str, Any] | None:
    """Build safe environment snapshot."""
    if not include_env:
        return None

    if build_env_debug_snapshot is None:
        return {
            "ok": False,
            "error": "build_env_debug_snapshot unavailable.",
        }

    try:
        return build_env_debug_snapshot()
    except Exception as exc:
        return {
            "ok": False,
            "error": _safe_exception_message(exc),
            "exceptionType": exc.__class__.__name__,
        }


def _build_direct_default_graph_status() -> dict[str, Any]:
    """Build minimal read-only default graph status directly from models."""
    project_id = _get_default_project_id()
    universe_id = _get_default_universe_id()
    world_id = _get_default_world_id()

    result = {
        "ok": False,
        "status": "partial",
        "defaults": {
            "projectId": project_id,
            "universeId": universe_id,
            "worldId": world_id,
            "templateId": _get_default_template_id(),
            "providerId": _get_default_provider_id(),
            "providerWorldId": _get_default_provider_world_id(),
        },
        "project": {
            "exists": False,
            "projectId": project_id,
            "dbId": None,
        },
        "universe": {
            "exists": False,
            "universeId": universe_id,
            "dbId": None,
        },
        "world": {
            "exists": False,
            "worldId": world_id,
            "dbId": None,
        },
        "ready": {
            "project": False,
            "universe": False,
            "world": False,
        },
    }

    try:
        project = _project_query_by_identifier(project_id, include_deleted=False).one_or_none()
        result["project"]["exists"] = project is not None
        result["project"]["dbId"] = _model_value(project, "id") if project is not None else None

        if project is None:
            return result

        universe = _get_project_default_universe(project, include_deleted=False)
        result["universe"]["exists"] = universe is not None
        result["universe"]["dbId"] = _model_value(universe, "id") if universe is not None else None

        world = _get_universe_spawn_world(universe, include_deleted=False)
        result["world"]["exists"] = world is not None
        result["world"]["dbId"] = _model_value(world, "id") if world is not None else None

        result["ready"] = {
            "project": project is not None,
            "universe": universe is not None,
            "world": world is not None,
        }
        result["ok"] = bool(project is not None and universe is not None and world is not None)
        result["status"] = "ready" if result["ok"] else "partial"
        return result

    except Exception as exc:
        result["error"] = _safe_exception_message(exc)
        result["exceptionType"] = exc.__class__.__name__
        return result


def _build_bootstrap_status_safe() -> dict[str, Any]:
    """Build bootstrap status safely, falling back to direct default graph status."""
    if build_db_bootstrap_status is not None:
        try:
            status = build_db_bootstrap_status(current_app, db_extension=db)
            if isinstance(status, Mapping):
                return dict(status)
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "error": _safe_exception_message(exc),
                "exceptionType": exc.__class__.__name__,
                "fallbackDefaultGraph": _build_direct_default_graph_status(),
            }

    direct = _build_direct_default_graph_status()
    return {
        "ok": bool(direct.get("ok")),
        "status": direct.get("status", "partial"),
        "schemaReady": None,
        "seedReady": bool(direct.get("ok")),
        "defaultProjectReady": _get_nested_bool(direct, ("ready", "project"), False),
        "defaultUniverseReady": _get_nested_bool(direct, ("ready", "universe"), False),
        "defaultWorldReady": _get_nested_bool(direct, ("ready", "world"), False),
        "seed": {
            "defaultWorldInvariant": direct,
        },
        "fallback": True,
    }


def _extract_readiness(bootstrap_status: Mapping[str, Any], database_status: Mapping[str, Any]) -> dict[str, Any]:
    """Extract normalized readiness flags from bootstrap/database status."""
    schema_ready = bootstrap_status.get("schemaReady")
    if schema_ready is None:
        schema_ready = _get_nested_bool(bootstrap_status, ("schema", "ok"), False)

    seed_ready = bootstrap_status.get("seedReady")
    if seed_ready is None:
        seed_ready = _get_nested_bool(bootstrap_status, ("seed", "ok"), False)

    default_project_ready = bootstrap_status.get("defaultProjectReady")
    if default_project_ready is None:
        default_project_ready = _get_nested_bool(
            bootstrap_status,
            ("seed", "defaultWorldInvariant", "ready", "project"),
            None,
        )

    default_universe_ready = bootstrap_status.get("defaultUniverseReady")
    if default_universe_ready is None:
        default_universe_ready = _get_nested_bool(
            bootstrap_status,
            ("seed", "defaultWorldInvariant", "ready", "universe"),
            None,
        )

    default_world_ready = bootstrap_status.get("defaultWorldReady")
    if default_world_ready is None:
        default_world_ready = _get_nested_bool(
            bootstrap_status,
            ("seed", "defaultWorldInvariant", "ready", "world"),
            None,
        )

    if default_project_ready is None:
        default_project_ready = _get_nested_bool(bootstrap_status, ("seed", "project", "exists"), False)
    if default_universe_ready is None:
        default_universe_ready = _get_nested_bool(bootstrap_status, ("seed", "universe", "exists"), False)
    if default_world_ready is None:
        default_world_ready = _get_nested_bool(bootstrap_status, ("seed", "world", "exists"), False)

    database_configured = _coerce_bool(database_status.get("configured"), fallback=False)
    database_connection_ok = database_status.get("connectionOk")
    if database_connection_ok is None:
        database_connection_ok = True if not database_status.get("connectionChecked") else False
    database_connection_ok = _coerce_bool(database_connection_ok, fallback=False)

    schema_ready_bool = _coerce_bool(schema_ready, fallback=False)
    seed_ready_bool = _coerce_bool(seed_ready, fallback=False)
    default_project_ready_bool = _coerce_bool(default_project_ready, fallback=False)
    default_universe_ready_bool = _coerce_bool(default_universe_ready, fallback=False)
    default_world_ready_bool = _coerce_bool(default_world_ready, fallback=False)

    schema_required = _get_config_bool("VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED", True)
    seed_required = _get_config_bool("VECTOPLAN_CHUNK_SEED_READY_REQUIRED", True)
    default_world_required = _get_config_bool("VECTOPLAN_CHUNK_DEFAULT_WORLD_READY_REQUIRED", True)

    service_ready = bool(
        database_configured
        and database_connection_ok
        and (schema_ready_bool if schema_required else True)
        and (seed_ready_bool if seed_required else True)
        and (default_world_ready_bool if default_world_required else True)
    )

    return {
        "serviceReady": service_ready,
        "databaseConfigured": database_configured,
        "databaseConnectionOk": database_connection_ok,
        "schemaReady": schema_ready_bool,
        "seedReady": seed_ready_bool,
        "defaultProjectReady": default_project_ready_bool,
        "defaultUniverseReady": default_universe_ready_bool,
        "defaultWorldReady": default_world_ready_bool,
        "requirements": {
            "schemaReadyRequired": schema_required,
            "seedReadyRequired": seed_required,
            "defaultWorldReadyRequired": default_world_required,
        },
    }


def _build_projects_config_status() -> dict[str, Any]:
    """Build route config status."""
    default_world_id = _get_default_world_id()
    provisioning_default_world_id = _get_config_string(
        "VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_WORLD_ID",
        default_world_id,
    )

    if _provider_like_world_id(provisioning_default_world_id):
        provisioning_default_world_id = default_world_id

    return {
        "defaultProjectId": _get_default_project_id(),
        "defaultUniverseId": _get_default_universe_id(),
        "defaultWorldId": default_world_id,
        "defaultInstanceWorldId": default_world_id,
        "defaultTemplateId": _get_default_template_id(),
        "defaultProviderId": _get_default_provider_id(),
        "defaultProviderWorldId": _get_default_provider_world_id(),
        "databaseUriConfigured": bool(_get_config_value("SQLALCHEMY_DATABASE_URI")),
        "runtimeIsReadOnly": _get_config_bool("VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY", True),
        "autoCreateAll": _get_config_bool("VECTOPLAN_CHUNK_AUTO_CREATE_ALL", False),
        "autoSeedDefaults": _get_config_bool("VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS", False),
        "seedDebugBlocks": _get_config_bool("VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS", False),
        "seedDevProject": _get_config_bool("VECTOPLAN_CHUNK_SEED_DEV_PROJECT", False),
        "allowRuntimeDbMutations": _get_config_bool("VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS", False),
        "bootstrap": {
            "enabled": _get_config_bool("VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED", False),
            "createAll": _get_config_bool("VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL", False),
            "seedDefaults": _get_config_bool("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS", False),
            "seedDebugBlocks": _get_config_bool("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS", False),
            "seedDevProject": _get_config_bool("VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT", False),
            "repairMissingColumns": _get_config_bool("VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS", False),
            "repairSeedInvariants": _get_config_bool("VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS", False),
        },
        "projectProvisioning": {
            "available": _provisioning_available(),
            "enabled": _get_config_bool("VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ENABLED", True),
            "routeByAppEnabled": _get_config_bool("VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ROUTE_BY_APP_ENABLED", True),
            "routeEnsureEnabled": _get_config_bool("VECTOPLAN_CHUNK_PROJECT_PROVISIONING_ROUTE_ENSURE_ENABLED", True),
            "defaultTemplateId": _get_config_string("VECTOPLAN_CHUNK_PROJECT_PROVISIONING_DEFAULT_TEMPLATE_ID", _get_default_template_id()),
            "defaultWorldId": provisioning_default_world_id,
        },
    }


@projects_bp.get("/projects/_status")
def get_projects_route_status():
    """
    Return diagnostics for project routes, database, model registration,
    schema/seed readiness and app-project provisioning readiness.
    """
    try:
        check_database = _get_query_bool(
            "checkDatabase",
            "check_database",
            "db",
            fallback=True,
        )
        include_models = _get_query_bool(
            "includeModels",
            "include_models",
            fallback=True,
        )
        include_counts = _get_query_bool(
            "includeCounts",
            "include_counts",
            fallback=True,
        )
        include_config = _get_query_bool(
            "includeConfig",
            "include_config",
            fallback=True,
        )
        include_settings = _get_query_bool(
            "includeSettings",
            "include_settings",
            fallback=True,
        )
        include_env = _get_query_bool(
            "includeEnv",
            "include_env",
            fallback=False,
        )

        database_status = get_database_status(
            current_app,
            check_connection=check_database,
        )

        bootstrap_status = _build_bootstrap_status_safe()
        readiness = _extract_readiness(bootstrap_status, database_status)

        model_status = _build_model_status_safe(include_models=include_models)
        counts = _build_counts_safe(include_counts=include_counts)
        config = _build_projects_config_status() if include_config else None
        settings = _build_settings_summary_safe(include_settings=include_settings)
        env_snapshot = _build_env_snapshot_safe(include_env=include_env)

        status_text = "ready" if readiness["serviceReady"] else "not_ready"

        body = {
            "ok": bool(readiness["serviceReady"]),
            "status": status_text,
            "responseVersion": PROJECT_STATUS_RESPONSE_VERSION,
            "serviceReady": bool(readiness["serviceReady"]),
            "schemaReady": bool(readiness["schemaReady"]),
            "seedReady": bool(readiness["seedReady"]),
            "defaultProjectReady": bool(readiness["defaultProjectReady"]),
            "defaultUniverseReady": bool(readiness["defaultUniverseReady"]),
            "defaultWorldReady": bool(readiness["defaultWorldReady"]),
            "defaultIds": {
                "projectId": _get_default_project_id(),
                "universeId": _get_default_universe_id(),
                "worldId": _get_default_world_id(),
                "templateId": _get_default_template_id(),
                "providerId": _get_default_provider_id(),
                "providerWorldId": _get_default_provider_world_id(),
            },
            "requirements": readiness["requirements"],
            "route": {
                "source": ROUTE_SOURCE,
                "moduleVersion": ROUTE_MODULE_VERSION,
                "blueprint": projects_bp.name,
                "dbBacked": True,
                "provisioningRoutes": {
                    "previewByApp": "/projects/preview/by-app/<app_project_public_id>",
                    "getByApp": "/projects/by-app/<app_project_public_id>",
                    "ensureByApp": "/projects/by-app/<app_project_public_id>",
                    "ensureFromPayload": "/projects/ensure",
                },
                "statusRoute": "/projects/_status",
            },
            "database": database_status,
            "bootstrapStatus": bootstrap_status,
            "models": model_status,
            "counts": counts,
            "config": config,
            "settings": settings,
            "env": env_snapshot,
            "metadata": _route_metadata(
                {
                    "checkDatabase": check_database,
                    "includeModels": include_models,
                    "includeCounts": include_counts,
                    "includeConfig": include_config,
                    "includeSettings": include_settings,
                    "includeEnv": include_env,
                }
            ),
        }

        return _json_response(body, 200)

    except Exception as exc:
        return _error_response(exc)


@projects_bp.post("/projects/_cache/reset")
def reset_projects_route_cache():
    """
    Reset optional legacy caches.

    This route does not alter PostgreSQL state.
    """
    try:
        reset_results: dict[str, Any] = {}

        optional_resets = (
            ("src.world_state.bootstrap", "reset_project_bootstrap_cache"),
            ("src.world_state.service", "reset_default_world_state_service_cache"),
            ("src.world_state.resolver", "reset_default_world_state_resolver_cache"),
            ("src.world_state.defaults", "reset_default_world_state_catalog_cache"),
        )

        for module_name, function_name in optional_resets:
            try:
                module = __import__(module_name, fromlist=[function_name])
                reset_fn = getattr(module, function_name, None)
                if callable(reset_fn):
                    reset_fn()
                    reset_results[f"{module_name}.{function_name}"] = "reset"
                else:
                    reset_results[f"{module_name}.{function_name}"] = "missing"
            except Exception as exc:
                reset_results[f"{module_name}.{function_name}"] = f"error: {_safe_exception_message(exc)}"

        body = _ok_response(
            response_version=PROJECT_CACHE_RESET_RESPONSE_VERSION,
            payload={
                "reset": reset_results,
                "postgresStateChanged": False,
            },
            metadata={
                "dbBacked": True,
            },
        )

        return _json_response(body, 200)

    except Exception as exc:
        return _error_response(exc)


# -----------------------------------------------------------------------------
# Provisioning routes
# -----------------------------------------------------------------------------

@projects_bp.get("/projects/preview/by-app/<app_project_public_id>")
def preview_project_by_app(app_project_public_id: str):
    """
    Preview deterministic chunk ids for an app project without writing to DB.

    Example:
        GET /projects/preview/by-app/prj_abc
    """
    try:
        if preview_chunk_project_ids is None:
            return _provisioning_unavailable_response()

        preview = preview_chunk_project_ids(
            app_project_public_id,
            {
                "chunk_project_id": _get_query_string("chunkProjectId", "chunk_project_id", fallback=""),
                "chunk_universe_id": _get_query_string("chunkUniverseId", "chunk_universe_id", fallback=""),
                "chunk_world_id": _get_query_string("chunkWorldId", "chunk_world_id", fallback=""),
            },
        )

        body = _ok_response(
            response_version=PROJECT_PROVISION_PREVIEW_RESPONSE_VERSION,
            payload={
                "preview": preview,
                "appProjectPublicId": app_project_public_id,
                "willCreateDatabaseRows": False,
            },
            metadata={
                "provisioningAvailable": _provisioning_available(),
            },
        )

        return _json_response(body, 200)

    except Exception as exc:
        return _error_response(exc)


@projects_bp.get("/projects/by-app/<app_project_public_id>")
def get_project_by_app(app_project_public_id: str):
    """
    Return existing chunk project linked to an app project id.

    This route does not create anything. Use PUT on the same path to ensure.
    """
    try:
        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        include_universes = _get_query_bool("includeUniverses", "include_universes", fallback=True)
        include_worlds = _get_query_bool("includeWorlds", "include_worlds", fallback=True)
        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_internal = _get_query_bool("includeInternal", "include_internal", fallback=False)
        include_bootstrap = _get_query_bool("includeBootstrap", "include_bootstrap", fallback=True)
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        api_prefix = _get_query_string(
            "apiPrefix",
            "api_prefix",
            fallback=_get_env_string(ENV_ROUTE_DEFAULT_API_PREFIX, ""),
        )

        project = _query_project_by_app_project_id(
            app_project_public_id,
            include_deleted=include_deleted,
        )

        if project is None:
            preview = None
            if preview_chunk_project_ids is not None:
                preview = preview_chunk_project_ids(app_project_public_id, {})

            return _json_response(
                _ok_response(
                    response_version=PROJECT_BY_APP_RESPONSE_VERSION,
                    payload={
                        "found": False,
                        "appProjectPublicId": app_project_public_id,
                        "preview": preview,
                    },
                    metadata={
                        "dbBacked": True,
                        "includeDeleted": include_deleted,
                    },
                ),
                404,
            )

        project_payload = _serialize_project_detail(
            project,
            include_universes=include_universes,
            include_worlds=include_worlds,
            include_metadata=include_metadata,
            include_internal=include_internal,
        )

        payload: dict[str, Any] = {
            "found": True,
            "appProjectPublicId": app_project_public_id,
            "chunkProjectId": _get_project_public_id(project),
            "project": project_payload,
        }

        if include_bootstrap:
            universe = _get_project_default_universe(project, include_deleted=include_deleted)
            world = _get_universe_spawn_world(universe, include_deleted=include_deleted)

            payload["bootstrap"] = _serialize_project_bootstrap(
                project=project,
                universe=universe,
                world=world,
                include_route_hints=include_route_hints,
                include_worlds=include_worlds,
                include_metadata=include_metadata,
                api_prefix=api_prefix,
            )

        body = _ok_response(
            response_version=PROJECT_BY_APP_RESPONSE_VERSION,
            payload=payload,
            metadata={
                "dbBacked": True,
                "includeDeleted": include_deleted,
                "includeBootstrap": include_bootstrap,
                "includeRouteHints": include_route_hints,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        return _error_response(exc, code="project_not_found", status_code=404)
    except Exception as exc:
        return _error_response(exc)


@projects_bp.put("/projects/by-app/<app_project_public_id>")
@projects_bp.post("/projects/by-app/<app_project_public_id>")
def ensure_project_by_app(app_project_public_id: str):
    """
    Idempotently ensure chunk project for a vectoplan-app project.

    This is the main endpoint vectoplan-app should call.
    """
    try:
        if ensure_chunk_project_for_app_project is None:
            return _provisioning_unavailable_response()

        payload = _get_json_body()
        result = ensure_chunk_project_for_app_project(
            app_project_public_id,
            payload,
            session=db.session,
            commit=True,
        )

        return _wrap_provisioning_result(
            result,
            metadata={
                "route": "by-app",
                "appProjectPublicId": app_project_public_id,
                "idempotent": True,
            },
        )

    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc)


@projects_bp.post("/projects/ensure")
def ensure_project_from_payload():
    """
    Idempotently ensure chunk project using request body only.
    """
    try:
        if ensure_chunk_project_from_payload is None:
            return _provisioning_unavailable_response()

        payload = _get_json_body()
        result = ensure_chunk_project_from_payload(
            payload,
            session=db.session,
            commit=True,
        )

        return _wrap_provisioning_result(
            result,
            metadata={
                "route": "ensure",
                "idempotent": True,
            },
        )

    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc)


# -----------------------------------------------------------------------------
# Bootstrap and project CRUD routes
# -----------------------------------------------------------------------------

@projects_bp.get("/projects/<project_id>/bootstrap")
def get_project_bootstrap(project_id: str):
    """
    Return editor bootstrap data for a concrete project.

    Example:
        GET /projects/dev-project/bootstrap
    """
    try:
        route_project_id, route_allows_default = _normalize_project_route_id(project_id)

        allow_default_project = bool(
            route_allows_default
            or _get_query_bool(
                "allowDefaultProject",
                "allow_default_project",
                fallback=_get_env_bool(ENV_ROUTE_ALLOW_DEFAULT_PROJECT, False),
            )
        )

        effective_project_id = _resolve_effective_project_id(
            route_project_id,
            allow_default_project=allow_default_project,
        )

        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        include_worlds = _get_query_bool("includeWorlds", "include_worlds", fallback=True)
        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        api_prefix = _get_query_string(
            "apiPrefix",
            "api_prefix",
            fallback=_get_env_string(ENV_ROUTE_DEFAULT_API_PREFIX, ""),
        )

        project = _get_project_or_404(effective_project_id, include_deleted=include_deleted)
        universe = _get_project_default_universe(project, include_deleted=include_deleted)
        world = _get_universe_spawn_world(universe, include_deleted=include_deleted)

        body = _ok_response(
            response_version=PROJECT_BOOTSTRAP_RESPONSE_VERSION,
            payload=_serialize_project_bootstrap(
                project=project,
                universe=universe,
                world=world,
                include_route_hints=include_route_hints,
                include_worlds=include_worlds,
                include_metadata=include_metadata,
                api_prefix=api_prefix,
            ),
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": _get_project_public_id(project),
                "resolvedUniverseId": _get_universe_public_id(universe),
                "resolvedWorldId": _get_world_public_id(world),
                "allowDefaultProject": allow_default_project,
                "includeRouteHints": include_route_hints,
                "includeWorlds": include_worlds,
                "includeMetadata": include_metadata,
                "dbBacked": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        return _error_response(exc, code="project_not_found", status_code=404)
    except Exception as exc:
        return _error_response(exc)


@projects_bp.get("/projects/bootstrap")
def get_default_project_bootstrap():
    """
    Return bootstrap data for the configured default project.

    Useful for development and smoke tests. Productive editor calls should
    normally use `/projects/<project_id>/bootstrap`.
    """
    try:
        return get_project_bootstrap("default")
    except Exception as exc:
        return _error_response(exc)


@projects_bp.get("/projects")
def list_projects():
    """
    List chunk projects from PostgreSQL.
    """
    try:
        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        include_archived = _get_query_bool("includeArchived", "include_archived", fallback=True)
        include_universes = _get_query_bool("includeUniverses", "include_universes", fallback=True)
        include_worlds = _get_query_bool("includeWorlds", "include_worlds", fallback=True)
        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_internal = _get_query_bool("includeInternal", "include_internal", fallback=False)
        search = _get_query_string("q", "search", fallback="")
        limit = _get_query_int("limit", fallback=100, minimum=1, maximum=1000)
        offset = _get_query_int("offset", fallback=0, minimum=0)

        query = _query_projects(
            include_deleted=include_deleted,
            include_archived=include_archived,
            search=search,
        )

        total = query.count()

        if _model_supports_attr(Project, "created_at"):
            query = query.order_by(Project.created_at.desc())

        projects = (
            query
            .offset(offset)
            .limit(limit)
            .all()
        )

        serialized_projects = [
            _serialize_project_detail(
                project,
                include_universes=include_universes,
                include_worlds=include_worlds,
                include_metadata=include_metadata,
                include_internal=include_internal,
            )
            for project in projects
        ]

        body = _ok_response(
            response_version=PROJECT_LIST_RESPONSE_VERSION,
            payload={
                "projects": serialized_projects,
                "counts": {
                    "projects": len(serialized_projects),
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
            },
            metadata={
                "includeDeleted": include_deleted,
                "includeArchived": include_archived,
                "includeUniverses": include_universes,
                "includeWorlds": include_worlds,
                "includeMetadata": include_metadata,
                "includeInternal": include_internal,
                "search": search,
                "dbBacked": True,
            },
        )

        return _json_response(body, 200)

    except Exception as exc:
        return _error_response(exc)


@projects_bp.post("/projects")
def create_project():
    """
    Create a new chunk project with default Universe and world_spawn.

    This is the direct chunk-project creation route.

    For vectoplan-app integration, prefer:
        PUT /projects/by-app/<app_project_public_id>
        POST /projects/ensure
    """
    try:
        payload = _get_json_body()

        project, universe, world = _create_project_graph_from_payload(payload)
        db.session.commit()

        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        include_worlds = _get_query_bool("includeWorlds", "include_worlds", fallback=True)
        api_prefix = _get_query_string(
            "apiPrefix",
            "api_prefix",
            fallback=_get_env_string(ENV_ROUTE_DEFAULT_API_PREFIX, ""),
        )

        body = _ok_response(
            response_version=PROJECT_CREATE_RESPONSE_VERSION,
            payload={
                "created": True,
                **_serialize_project_bootstrap(
                    project=project,
                    universe=universe,
                    world=world,
                    include_route_hints=include_route_hints,
                    include_worlds=include_worlds,
                    include_metadata=include_metadata,
                    api_prefix=api_prefix,
                ),
            },
            metadata={
                "dbBacked": True,
                "createdProject": True,
                "createdUniverse": True,
                "createdWorld": True,
                "preferProvisioningForAppProjects": True,
            },
        )

        return _json_response(body, 201)

    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass

        message = _safe_exception_message(exc).lower()
        if "unique" in message or "duplicate" in message:
            return _error_response(exc, code="project_already_exists", status_code=409)

        return _error_response(exc)


@projects_bp.get("/projects/<project_id>")
def get_project(project_id: str):
    """
    Return project details plus optional universes/worlds.
    """
    try:
        route_project_id, route_allows_default = _normalize_project_route_id(project_id)

        allow_default_project = bool(
            route_allows_default
            or _get_query_bool("allowDefaultProject", "allow_default_project", fallback=False)
        )

        effective_project_id = _resolve_effective_project_id(
            route_project_id,
            allow_default_project=allow_default_project,
        )

        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        include_universes = _get_query_bool("includeUniverses", "include_universes", fallback=True)
        include_worlds = _get_query_bool("includeWorlds", "include_worlds", fallback=True)
        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_internal = _get_query_bool("includeInternal", "include_internal", fallback=False)

        project = _get_project_or_404(effective_project_id, include_deleted=include_deleted)

        body = _ok_response(
            response_version=PROJECT_RESPONSE_VERSION,
            payload={
                "project": _serialize_project_detail(
                    project,
                    include_universes=include_universes,
                    include_worlds=include_worlds,
                    include_metadata=include_metadata,
                    include_internal=include_internal,
                )
            },
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": _get_project_public_id(project),
                "allowDefaultProject": allow_default_project,
                "includeDeleted": include_deleted,
                "includeUniverses": include_universes,
                "includeWorlds": include_worlds,
                "includeMetadata": include_metadata,
                "includeInternal": include_internal,
                "dbBacked": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        return _error_response(exc, code="project_not_found", status_code=404)
    except Exception as exc:
        return _error_response(exc)


@projects_bp.patch("/projects/<project_id>")
def patch_project(project_id: str):
    """
    Patch a chunk project.

    This route changes only Project fields. Universe/world changes belong to
    dedicated universe/world routes.
    """
    try:
        payload = _get_json_body()
        route_project_id, route_allows_default = _normalize_project_route_id(project_id)

        allow_default_project = bool(
            route_allows_default
            or _get_query_bool("allowDefaultProject", "allow_default_project", fallback=False)
        )

        effective_project_id = _resolve_effective_project_id(
            route_project_id,
            allow_default_project=allow_default_project,
        )

        updated_by_user_id = (
            payload.get("updatedByUserId")
            or payload.get("updated_by_user_id")
            or payload.get("userId")
            or payload.get("user_id")
        )

        project = _get_project_or_404(effective_project_id)

        if hasattr(project, "apply_patch_payload"):
            project.apply_patch_payload(payload, updated_by_user_id=updated_by_user_id)
        else:
            for field_name in ("name", "display_name", "title", "description", "status"):
                if field_name in payload and _model_supports_attr(project, field_name):
                    setattr(project, field_name, payload[field_name])

        db.session.add(project)
        db.session.commit()

        project_payload = (
            project.to_public_dict()
            if hasattr(project, "to_public_dict")
            else _serialize_model_fields(project, _PROJECT_PUBLIC_FIELDS)
        )

        body = _ok_response(
            response_version=PROJECT_PATCH_RESPONSE_VERSION,
            payload={
                "changed": True,
                "project": project_payload,
            },
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": _get_project_public_id(project),
                "dbBacked": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        return _error_response(exc, code="project_not_found", status_code=404)
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc)


@projects_bp.delete("/projects/<project_id>")
def delete_project(project_id: str):
    """
    Soft-delete a chunk project.

    Chunks, command logs and events remain in PostgreSQL for history/audit/AI.
    """
    try:
        payload = _get_json_body()
        route_project_id, route_allows_default = _normalize_project_route_id(project_id)

        allow_default_project = bool(
            route_allows_default
            or _get_query_bool("allowDefaultProject", "allow_default_project", fallback=False)
        )

        effective_project_id = _resolve_effective_project_id(
            route_project_id,
            allow_default_project=allow_default_project,
        )

        updated_by_user_id = (
            payload.get("updatedByUserId")
            or payload.get("updated_by_user_id")
            or payload.get("userId")
            or payload.get("user_id")
        )

        project = _get_project_or_404(effective_project_id)

        if hasattr(project, "soft_delete"):
            project.soft_delete(updated_by_user_id=updated_by_user_id)
        elif _model_supports_attr(project, "status"):
            project.status = "deleted"

        project_db_id = _model_value(project, "id")

        universes_query = Universe.query
        if _model_supports_attr(Universe, "project_db_id") and project_db_id is not None:
            universes_query = universes_query.filter_by(project_db_id=project_db_id)

        universes = universes_query.all()

        for universe in universes:
            is_deleted = bool(_model_value(universe, "is_deleted", False))
            if not is_deleted:
                if hasattr(universe, "soft_delete"):
                    universe.soft_delete(updated_by_user_id=updated_by_user_id)
                elif _model_supports_attr(universe, "status"):
                    universe.status = "deleted"

        worlds_query = WorldInstance.query
        if _model_supports_attr(WorldInstance, "project_db_id") and project_db_id is not None:
            worlds_query = worlds_query.filter_by(project_db_id=project_db_id)

        worlds = worlds_query.all()

        for world in worlds:
            is_deleted = bool(_model_value(world, "is_deleted", False))
            if not is_deleted:
                if hasattr(world, "soft_delete"):
                    world.soft_delete(updated_by_user_id=updated_by_user_id)
                elif _model_supports_attr(world, "status"):
                    world.status = "deleted"

        db.session.commit()

        project_payload = (
            project.to_public_dict()
            if hasattr(project, "to_public_dict")
            else _serialize_model_fields(project, _PROJECT_PUBLIC_FIELDS)
        )

        body = _ok_response(
            response_version=PROJECT_DELETE_RESPONSE_VERSION,
            payload={
                "deleted": True,
                "softDelete": True,
                "projectId": _get_project_public_id(project),
                "deletedAt": project_payload.get("deletedAt") or project_payload.get("deleted_at"),
                "counts": {
                    "universesSoftDeleted": len(universes),
                    "worldsSoftDeleted": len(worlds),
                },
            },
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": _get_project_public_id(project),
                "dbBacked": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        return _error_response(exc, code="project_not_found", status_code=404)
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc)


__all__ = (
    "projects_bp",
    "ROUTE_MODULE_VERSION",
    "ROUTE_SOURCE",
)