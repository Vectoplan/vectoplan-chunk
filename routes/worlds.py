# services/vectoplan-chunk/routes/worlds.py
"""
World routes for the VECTOPLAN chunk service.

This module exposes PostgreSQL-backed project-scoped world instance endpoints.

Primary routes:

    GET    /projects/<project_id>/worlds
    POST   /projects/<project_id>/worlds
    GET    /projects/<project_id>/worlds/<world_id>
    PATCH  /projects/<project_id>/worlds/<world_id>
    DELETE /projects/<project_id>/worlds/<world_id>

Meaning:

    projectId
    -> universeId
    -> concrete worldId
    -> templateId / providerWorldId

Phase 1 default mapping:

    /projects/dev-project/worlds
    -> returns world_spawn

    /projects/dev-project/worlds/world_spawn
    -> concrete world instance
    -> templateId      = flat
    -> providerWorldId = flat

Important:
- `flat` is the provider/template world.
- `world_spawn` is the productive project world instance.
- This file must stay a thin HTTP adapter.
- It must not generate chunks.
- It must not execute commands.
- It must not write ChunkSnapshots.
- It must not write ChunkEvents.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import or_

from extensions import db, get_database_status
from models import (
    Project,
    Universe,
    WorldInstance,
    get_model_debug_summary,
)


worlds_bp = Blueprint("worlds", __name__)

ROUTE_MODULE_VERSION = "0.2.0"
ROUTE_SOURCE = "routes.worlds"

WORLD_RESPONSE_VERSION = "world-response.v1"
WORLD_LIST_RESPONSE_VERSION = "world-list-response.v1"
WORLD_CREATE_RESPONSE_VERSION = "world-create-response.v1"
WORLD_PATCH_RESPONSE_VERSION = "world-patch-response.v1"
WORLD_DELETE_RESPONSE_VERSION = "world-delete-response.v1"
WORLD_STATUS_RESPONSE_VERSION = "worlds-route-status-response.v1"

ENV_ROUTE_INCLUDE_DEBUG_ERRORS = "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS"
ENV_ROUTE_DEFAULT_API_PREFIX = "VECTOPLAN_CHUNK_API_PREFIX"
ENV_ROUTE_ALLOW_DEFAULT_PROJECT = "VECTOPLAN_CHUNK_ROUTE_ALLOW_DEFAULT_PROJECT"

_DEFAULT_PROJECT_ALIASES = {
    "",
    "default",
    "_default",
    "current",
    "_current",
    "dev",
    "_dev",
}

_DEFAULT_UNIVERSE_ALIASES = {
    "",
    "default",
    "_default",
    "current",
    "_current",
    "dev",
    "_dev",
}

_DEFAULT_WORLD_ALIASES = {
    "",
    "default",
    "_default",
    "spawn",
    "_spawn",
    "current",
    "_current",
}


# -----------------------------------------------------------------------------
# Generic helpers
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


def _coerce_float(
    value: Any,
    *,
    fallback: float = 0.0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Coerce arbitrary value to float with optional bounds."""
    try:
        result = float(value)
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

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled"}:
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
        return str(value)
    except Exception:
        return "<unserializable-value>"


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


def _get_config_float(
    name: str,
    fallback: float = 0.0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Read float config defensively."""
    return _coerce_float(
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
        return _coerce_int(fallback, fallback=fallback, minimum=minimum, maximum=maximum)

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


# -----------------------------------------------------------------------------
# Default config helpers
# -----------------------------------------------------------------------------

def _get_default_project_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", "dev-project")


def _get_default_universe_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", "dev-universe")


def _get_default_world_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID", "world_spawn")


def _get_default_world_slug() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG", "spawn")


def _get_default_world_name() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME", "Flat Spawn World")


def _get_default_template_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID", "flat")


def _get_default_provider_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID", "flat")


def _get_default_provider_world_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID", "flat")


def _get_default_api_prefix() -> str:
    return _get_env_string(ENV_ROUTE_DEFAULT_API_PREFIX, "")


# -----------------------------------------------------------------------------
# Project / universe / world resolution
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


def _normalize_universe_route_id(universe_id: str | None) -> tuple[str | None, bool]:
    """
    Normalize route universe id.

    Returns:
        (universe_id, allow_default_universe)
    """
    text = _coerce_string(universe_id)

    if text.lower() in _DEFAULT_UNIVERSE_ALIASES:
        return None, True

    return text, False


def _normalize_world_route_id(world_id: str | None) -> tuple[str | None, bool]:
    """
    Normalize route world id.

    Returns:
        (world_id, allow_default_world)
    """
    text = _coerce_string(world_id)

    if text.lower() in _DEFAULT_WORLD_ALIASES:
        return None, True

    return text, False


def _resolve_effective_project_id(project_id: str | None, *, allow_default_project: bool = False) -> str:
    """Resolve project id with optional default alias support."""
    normalized_project_id, route_allows_default = _normalize_project_route_id(project_id)

    if normalized_project_id:
        return normalized_project_id

    if allow_default_project or route_allows_default:
        return _get_default_project_id()

    raise ValueError("projectId is required.")


def _resolve_effective_universe_id(universe_id: str | None, *, allow_default_universe: bool = True) -> str | None:
    """Resolve optional universe id."""
    normalized_universe_id, route_allows_default = _normalize_universe_route_id(universe_id)

    if normalized_universe_id:
        return normalized_universe_id

    if allow_default_universe or route_allows_default:
        return None

    raise ValueError("universeId is required.")


def _resolve_effective_world_id(world_id: str | None, *, allow_default_world: bool = True) -> str:
    """Resolve world id with optional default alias support."""
    normalized_world_id, route_allows_default = _normalize_world_route_id(world_id)

    if normalized_world_id:
        return normalized_world_id

    if allow_default_world or route_allows_default:
        return _get_default_world_id()

    raise ValueError("worldId is required.")


def _get_project_or_404(project_id: str, *, include_deleted: bool = False) -> Project:
    """Load project by public project id."""
    query = Project.query.filter(Project.project_id == project_id)

    if not include_deleted:
        query = query.filter(Project.deleted_at.is_(None))

    project = query.one_or_none()

    if project is None:
        raise LookupError(f"Project '{project_id}' was not found.")

    return project


def _get_default_universe_for_project(project: Project, *, include_deleted: bool = False) -> Universe:
    """Load default universe for project."""
    universe_id = project.default_universe_id or _get_default_universe_id()

    query = Universe.query.filter(
        Universe.project_db_id == project.id,
        Universe.universe_id == universe_id,
    )

    if not include_deleted:
        query = query.filter(Universe.deleted_at.is_(None))

    universe = query.one_or_none()

    if universe is None:
        fallback_query = Universe.query.filter(Universe.project_db_id == project.id)
        if not include_deleted:
            fallback_query = fallback_query.filter(Universe.deleted_at.is_(None))
        universe = fallback_query.order_by(Universe.created_at.asc()).first()

    if universe is None:
        raise LookupError(f"Project '{project.project_id}' has no universe.")

    return universe


def _get_universe_or_404(
    project: Project,
    universe_id: str | None = None,
    *,
    include_deleted: bool = False,
) -> Universe:
    """Load universe by id or project default."""
    effective_universe_id = _resolve_effective_universe_id(
        universe_id,
        allow_default_universe=True,
    )

    if effective_universe_id is None:
        return _get_default_universe_for_project(project, include_deleted=include_deleted)

    query = Universe.query.filter(
        Universe.project_db_id == project.id,
        Universe.universe_id == effective_universe_id,
    )

    if not include_deleted:
        query = query.filter(Universe.deleted_at.is_(None))

    universe = query.one_or_none()

    if universe is None:
        raise LookupError(
            f"Universe '{effective_universe_id}' was not found in project '{project.project_id}'."
        )

    return universe


def _get_world_or_404(
    universe: Universe,
    world_id: str,
    *,
    include_deleted: bool = False,
) -> WorldInstance:
    """Load world by universe and world id."""
    effective_world_id = _resolve_effective_world_id(
        world_id,
        allow_default_world=True,
    )

    query = WorldInstance.query.filter(
        WorldInstance.universe_db_id == universe.id,
        WorldInstance.world_id == effective_world_id,
    )

    if not include_deleted:
        query = query.filter(WorldInstance.deleted_at.is_(None))

    world = query.one_or_none()

    if world is None:
        raise LookupError(
            f"World '{effective_world_id}' was not found in universe '{universe.universe_id}'."
        )

    return world


def _query_worlds(
    *,
    project: Project,
    universe: Universe | None = None,
    include_deleted: bool = False,
    include_archived: bool = True,
    search: str = "",
):
    """Build world list query."""
    query = WorldInstance.query.filter(WorldInstance.project_db_id == project.id)

    if universe is not None:
        query = query.filter(WorldInstance.universe_db_id == universe.id)

    if not include_deleted:
        query = query.filter(WorldInstance.deleted_at.is_(None))

    if not include_archived:
        query = query.filter(WorldInstance.status != "archived")

    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                WorldInstance.world_id.ilike(like),
                WorldInstance.slug.ilike(like),
                WorldInstance.name.ilike(like),
                WorldInstance.provider_world_id.ilike(like),
                WorldInstance.template_id.ilike(like),
            )
        )

    return query


# -----------------------------------------------------------------------------
# Serialization helpers
# -----------------------------------------------------------------------------

def _build_route_hints(
    *,
    project_id: str,
    world_id: str,
    api_prefix: str = "",
) -> dict[str, str]:
    """Build editor route hints for a world."""
    prefix = _coerce_string(api_prefix).rstrip("/")

    return {
        "projectBootstrap": f"{prefix}/projects/{project_id}/bootstrap",
        "project": f"{prefix}/projects/{project_id}",
        "worlds": f"{prefix}/projects/{project_id}/worlds",
        "world": f"{prefix}/projects/{project_id}/worlds/{world_id}",
        "blocks": f"{prefix}/projects/{project_id}/worlds/{world_id}/blocks",
        "chunk": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks",
        "chunksBatch": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks/batch",
        "commands": f"{prefix}/projects/{project_id}/worlds/{world_id}/commands",
    }


def _serialize_world(
    world: WorldInstance,
    *,
    project: Project,
    universe: Universe,
    include_metadata: bool = True,
    include_internal: bool = False,
    include_route_hints: bool = True,
    api_prefix: str = "",
) -> dict[str, Any]:
    """Serialize one world instance."""
    item = world.to_dict(
        include_internal=include_internal,
        include_metadata=include_metadata,
        project_id=project.project_id,
        universe_id=universe.universe_id,
    )

    item["context"] = {
        "projectScoped": True,
        "projectId": project.project_id,
        "universeId": universe.universe_id,
        "worldId": world.world_id,
        "templateId": world.template_id,
        "providerId": world.provider_id,
        "providerWorldId": world.provider_world_id,
        "generatorType": world.generator_type,
        "generatorVersion": world.generator_version,
        "blockRegistryId": world.block_registry_id,
        "blockRegistryVersion": world.block_registry_version,
        "chunkSize": world.chunk_size,
        "cellSize": world.cell_size,
    }

    if include_route_hints:
        item["routeHints"] = _build_route_hints(
            project_id=project.project_id,
            world_id=world.world_id,
            api_prefix=api_prefix,
        )

    return item


def _serialize_world_list(
    worlds: list[WorldInstance],
    *,
    project: Project,
    universe: Universe | None,
    include_metadata: bool = True,
    include_internal: bool = False,
    include_route_hints: bool = True,
    api_prefix: str = "",
) -> list[dict[str, Any]]:
    """Serialize list of world instances."""
    result: list[dict[str, Any]] = []

    universe_cache: dict[int, Universe] = {}
    if universe is not None:
        universe_cache[universe.id] = universe

    for world in worlds:
        item_universe = universe_cache.get(world.universe_db_id)
        if item_universe is None:
            item_universe = Universe.query.filter_by(id=world.universe_db_id).one_or_none()
            if item_universe is not None:
                universe_cache[world.universe_db_id] = item_universe

        if item_universe is None:
            continue

        result.append(
            _serialize_world(
                world,
                project=project,
                universe=item_universe,
                include_metadata=include_metadata,
                include_internal=include_internal,
                include_route_hints=include_route_hints,
                api_prefix=api_prefix,
            )
        )

    return result


def _world_payload_value(payload: Mapping[str, Any], camel_key: str, snake_key: str, default: Any = None) -> Any:
    """Read payload key in camelCase or snake_case."""
    if camel_key in payload:
        return payload.get(camel_key)

    if snake_key in payload:
        return payload.get(snake_key)

    return default


def _create_world_for_universe(
    universe: Universe,
    payload: Mapping[str, Any],
    *,
    created_by_user_id: Optional[str] = None,
) -> WorldInstance:
    """Create a world instance from request payload and config defaults."""
    world_id = (
        _world_payload_value(payload, "worldId", "world_id")
        or _get_default_world_id()
    )

    world = WorldInstance.create(
        project_db_id=universe.project_db_id,
        universe_db_id=universe.id,
        world_id=world_id,
        slug=_world_payload_value(payload, "slug", "slug")
        or _world_payload_value(payload, "worldSlug", "world_slug")
        or _get_default_world_slug(),
        name=_world_payload_value(payload, "name", "name")
        or _world_payload_value(payload, "worldName", "world_name")
        or _get_default_world_name(),
        description=_world_payload_value(payload, "description", "description"),
        world_type=_world_payload_value(payload, "worldType", "world_type")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE", "runtime-world"),
        world_role=_world_payload_value(payload, "worldRole", "world_role")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE", "default_spawn"),
        world_scope=_world_payload_value(payload, "worldScope", "world_scope")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE", "project"),
        template_id=_world_payload_value(payload, "templateId", "template_id")
        or _get_default_template_id(),
        provider_id=_world_payload_value(payload, "providerId", "provider_id")
        or _get_default_provider_id(),
        provider_world_id=_world_payload_value(payload, "providerWorldId", "provider_world_id")
        or _get_default_provider_world_id(),
        generator_type=_world_payload_value(payload, "generatorType", "generator_type")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE", "flat-world"),
        generator_version=_world_payload_value(payload, "generatorVersion", "generator_version")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION", "1"),
        projection_type=_world_payload_value(payload, "projectionType", "projection_type")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE", "flat-local-v1"),
        topology_type=_world_payload_value(payload, "topologyType", "topology_type")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE", "flat-unbounded-v1"),
        coordinate_system=_world_payload_value(payload, "coordinateSystem", "coordinate_system")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM", "vectoplan-world-y-up-v1"),
        chunk_size=_world_payload_value(payload, "chunkSize", "chunk_size")
        or _get_config_int("VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE", 16, minimum=1),
        cell_size=_world_payload_value(payload, "cellSize", "cell_size")
        or _get_config_float("VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE", 1.0, minimum=0.0001),
        surface_y=_world_payload_value(
            payload,
            "surfaceY",
            "surface_y",
            _get_config_int("VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y", 0),
        ),
        min_y=_world_payload_value(
            payload,
            "minY",
            "min_y",
            _get_config_int("VECTOPLAN_CHUNK_DEFAULT_MIN_Y", -8),
        ),
        max_y=_world_payload_value(
            payload,
            "maxY",
            "max_y",
            _get_config_int("VECTOPLAN_CHUNK_DEFAULT_MAX_Y", 64),
        ),
        seed=_world_payload_value(payload, "seed", "seed")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_SEED", "dev-seed"),
        block_registry_id=_world_payload_value(payload, "blockRegistryId", "block_registry_id")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", "debug-blocks"),
        block_registry_version=_world_payload_value(payload, "blockRegistryVersion", "block_registry_version")
        or _get_config_string("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", "1"),
        spawn_x=_world_payload_value(payload, "spawnX", "spawn_x")
        or _get_config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_X", 0),
        spawn_y=_world_payload_value(payload, "spawnY", "spawn_y")
        or _get_config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y", 2),
        spawn_z=_world_payload_value(payload, "spawnZ", "spawn_z")
        or _get_config_int("VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z", 0),
        created_by_user_id=created_by_user_id,
        metadata_json=_world_payload_value(payload, "metadataJson", "metadata_json")
        or payload.get("metadata")
        or {
            "createdByRoute": ROUTE_SOURCE,
        },
    )

    db.session.add(world)
    db.session.flush()

    should_set_default = _coerce_bool(
        payload.get("setAsDefaultWorld")
        if "setAsDefaultWorld" in payload
        else payload.get("set_as_default_world"),
        fallback=False,
    )
    should_set_spawn = _coerce_bool(
        payload.get("setAsSpawnWorld")
        if "setAsSpawnWorld" in payload
        else payload.get("set_as_spawn_world"),
        fallback=False,
    )

    if not universe.default_world_id or should_set_default:
        universe.set_default_world_id(world.world_id, updated_by_user_id=created_by_user_id)
        db.session.add(universe)

    if not universe.spawn_world_id or should_set_spawn:
        universe.set_spawn_world_id(world.world_id, updated_by_user_id=created_by_user_id)
        db.session.add(universe)

    return world


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@worlds_bp.get("/projects/<project_id>/worlds")
def list_project_worlds(project_id: str):
    """
    List concrete world instances for a project.

    Example:
        GET /projects/dev-project/worlds

    Phase 1 usually returns:
        world_spawn -> template/provider flat
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

        universe_id = _get_query_string("universeId", "universe_id", fallback="") or None
        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        include_archived = _get_query_bool("includeArchived", "include_archived", fallback=True)
        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_internal = _get_query_bool("includeInternal", "include_internal", fallback=False)
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        search = _get_query_string("q", "search", fallback="")
        limit = _get_query_int("limit", fallback=100, minimum=1, maximum=1000)
        offset = _get_query_int("offset", fallback=0, minimum=0)
        api_prefix = _get_query_string(
            "apiPrefix",
            "api_prefix",
            fallback=_get_default_api_prefix(),
        )

        project = _get_project_or_404(effective_project_id, include_deleted=include_deleted)
        universe = (
            _get_universe_or_404(project, universe_id, include_deleted=include_deleted)
            if universe_id
            else None
        )

        query = _query_worlds(
            project=project,
            universe=universe,
            include_deleted=include_deleted,
            include_archived=include_archived,
            search=search,
        )

        total = query.count()
        worlds = (
            query
            .order_by(WorldInstance.created_at.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        serialized_worlds = _serialize_world_list(
            worlds,
            project=project,
            universe=universe,
            include_metadata=include_metadata,
            include_internal=include_internal,
            include_route_hints=include_route_hints,
            api_prefix=api_prefix,
        )

        body = _ok_response(
            response_version=WORLD_LIST_RESPONSE_VERSION,
            payload={
                "projectId": project.project_id,
                "universeId": universe.universe_id if universe else None,
                "worlds": serialized_worlds,
                "counts": {
                    "worlds": len(serialized_worlds),
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
            },
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": project.project_id,
                "universeId": universe_id,
                "allowDefaultProject": allow_default_project,
                "includeDeleted": include_deleted,
                "includeArchived": include_archived,
                "includeMetadata": include_metadata,
                "includeInternal": include_internal,
                "includeRouteHints": include_route_hints,
                "search": search,
                "dbBacked": True,
                "projectScoped": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        return _error_response(exc, code="world_context_not_found", status_code=404)
    except Exception as exc:
        return _error_response(exc)


@worlds_bp.post("/projects/<project_id>/worlds")
def create_project_world(project_id: str):
    """
    Create a concrete world instance in a project.

    Example body:
        {
          "worldId": "world_spawn",
          "name": "Flat Spawn World",
          "universeId": "dev-universe",
          "templateId": "flat",
          "providerWorldId": "flat",
          "setAsSpawnWorld": true
        }
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

        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_internal = _get_query_bool("includeInternal", "include_internal", fallback=False)
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        api_prefix = _get_query_string("apiPrefix", "api_prefix", fallback=_get_default_api_prefix())

        universe_id = (
            payload.get("universeId")
            or payload.get("universe_id")
            or _get_query_string("universeId", "universe_id", fallback="")
            or None
        )

        created_by_user_id = (
            payload.get("createdByUserId")
            or payload.get("created_by_user_id")
            or payload.get("userId")
            or payload.get("user_id")
        )

        project = _get_project_or_404(effective_project_id)
        universe = _get_universe_or_404(project, universe_id)

        world = _create_world_for_universe(
            universe,
            payload,
            created_by_user_id=created_by_user_id,
        )

        db.session.commit()

        body = _ok_response(
            response_version=WORLD_CREATE_RESPONSE_VERSION,
            payload={
                "created": True,
                "projectId": project.project_id,
                "universeId": universe.universe_id,
                "worldId": world.world_id,
                "world": _serialize_world(
                    world,
                    project=project,
                    universe=universe,
                    include_metadata=include_metadata,
                    include_internal=include_internal,
                    include_route_hints=include_route_hints,
                    api_prefix=api_prefix,
                ),
            },
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": project.project_id,
                "resolvedUniverseId": universe.universe_id,
                "dbBacked": True,
                "projectScoped": True,
            },
        )

        return _json_response(body, 201)

    except LookupError as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc, code="world_context_not_found", status_code=404)
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass

        message = _safe_exception_message(exc).lower()
        if "unique" in message or "duplicate" in message:
            return _error_response(exc, code="world_already_exists", status_code=409)

        return _error_response(exc)


@worlds_bp.get("/projects/<project_id>/worlds/<world_id>")
def get_project_world(project_id: str, world_id: str):
    """
    Return one concrete world instance for a project.

    Example:
        GET /projects/dev-project/worlds/world_spawn
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

        effective_world_id = _resolve_effective_world_id(world_id, allow_default_world=True)

        universe_id = _get_query_string("universeId", "universe_id", fallback="") or None
        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_internal = _get_query_bool("includeInternal", "include_internal", fallback=False)
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        api_prefix = _get_query_string("apiPrefix", "api_prefix", fallback=_get_default_api_prefix())

        project = _get_project_or_404(effective_project_id, include_deleted=include_deleted)
        universe = _get_universe_or_404(project, universe_id, include_deleted=include_deleted)
        world = _get_world_or_404(universe, effective_world_id, include_deleted=include_deleted)

        body = _ok_response(
            response_version=WORLD_RESPONSE_VERSION,
            payload={
                "projectId": project.project_id,
                "universeId": universe.universe_id,
                "worldId": world.world_id,
                "world": _serialize_world(
                    world,
                    project=project,
                    universe=universe,
                    include_metadata=include_metadata,
                    include_internal=include_internal,
                    include_route_hints=include_route_hints,
                    api_prefix=api_prefix,
                ),
            },
            metadata={
                "projectRouteId": project_id,
                "worldRouteId": world_id,
                "resolvedProjectId": project.project_id,
                "resolvedUniverseId": universe.universe_id,
                "resolvedWorldId": world.world_id,
                "includeDeleted": include_deleted,
                "includeMetadata": include_metadata,
                "includeInternal": include_internal,
                "includeRouteHints": include_route_hints,
                "dbBacked": True,
                "projectScoped": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        return _error_response(exc, code="world_not_found", status_code=404)
    except Exception as exc:
        return _error_response(exc)


@worlds_bp.patch("/projects/<project_id>/worlds/<world_id>")
def patch_project_world(project_id: str, world_id: str):
    """
    Patch one concrete world instance.

    This can update world metadata and provider/generator mapping. It does not
    migrate existing ChunkSnapshots.
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
        effective_world_id = _resolve_effective_world_id(world_id, allow_default_world=True)

        universe_id = (
            payload.get("universeId")
            or payload.get("universe_id")
            or _get_query_string("universeId", "universe_id", fallback="")
            or None
        )

        updated_by_user_id = (
            payload.get("updatedByUserId")
            or payload.get("updated_by_user_id")
            or payload.get("userId")
            or payload.get("user_id")
        )

        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_internal = _get_query_bool("includeInternal", "include_internal", fallback=False)
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        api_prefix = _get_query_string("apiPrefix", "api_prefix", fallback=_get_default_api_prefix())

        project = _get_project_or_404(effective_project_id)
        universe = _get_universe_or_404(project, universe_id)
        world = _get_world_or_404(universe, effective_world_id)

        world.apply_patch_payload(payload, updated_by_user_id=updated_by_user_id)

        set_as_default = _coerce_bool(
            payload.get("setAsDefaultWorld")
            if "setAsDefaultWorld" in payload
            else payload.get("set_as_default_world"),
            fallback=False,
        )
        set_as_spawn = _coerce_bool(
            payload.get("setAsSpawnWorld")
            if "setAsSpawnWorld" in payload
            else payload.get("set_as_spawn_world"),
            fallback=False,
        )

        if set_as_default:
            universe.set_default_world_id(world.world_id, updated_by_user_id=updated_by_user_id)
            db.session.add(universe)

        if set_as_spawn:
            universe.set_spawn_world_id(world.world_id, updated_by_user_id=updated_by_user_id)
            db.session.add(universe)

        db.session.add(world)
        db.session.commit()

        body = _ok_response(
            response_version=WORLD_PATCH_RESPONSE_VERSION,
            payload={
                "changed": True,
                "projectId": project.project_id,
                "universeId": universe.universe_id,
                "worldId": world.world_id,
                "world": _serialize_world(
                    world,
                    project=project,
                    universe=universe,
                    include_metadata=include_metadata,
                    include_internal=include_internal,
                    include_route_hints=include_route_hints,
                    api_prefix=api_prefix,
                ),
            },
            metadata={
                "projectRouteId": project_id,
                "worldRouteId": world_id,
                "resolvedProjectId": project.project_id,
                "resolvedUniverseId": universe.universe_id,
                "resolvedWorldId": world.world_id,
                "setAsDefaultWorld": set_as_default,
                "setAsSpawnWorld": set_as_spawn,
                "dbBacked": True,
                "projectScoped": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc, code="world_not_found", status_code=404)
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc)


@worlds_bp.delete("/projects/<project_id>/worlds/<world_id>")
def delete_project_world(project_id: str, world_id: str):
    """
    Soft-delete one concrete world instance.

    Existing ChunkSnapshots, command logs and events remain in PostgreSQL.
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
        effective_world_id = _resolve_effective_world_id(world_id, allow_default_world=True)

        universe_id = (
            payload.get("universeId")
            or payload.get("universe_id")
            or _get_query_string("universeId", "universe_id", fallback="")
            or None
        )

        updated_by_user_id = (
            payload.get("updatedByUserId")
            or payload.get("updated_by_user_id")
            or payload.get("userId")
            or payload.get("user_id")
        )

        project = _get_project_or_404(effective_project_id)
        universe = _get_universe_or_404(project, universe_id)
        world = _get_world_or_404(universe, effective_world_id)

        was_default = universe.default_world_id == world.world_id
        was_spawn = universe.spawn_world_id == world.world_id

        world.soft_delete(updated_by_user_id=updated_by_user_id)

        if was_default:
            universe.default_world_id = None
            db.session.add(universe)

        if was_spawn:
            universe.spawn_world_id = None
            db.session.add(universe)

        db.session.add(world)
        db.session.commit()

        body = _ok_response(
            response_version=WORLD_DELETE_RESPONSE_VERSION,
            payload={
                "deleted": True,
                "softDelete": True,
                "projectId": project.project_id,
                "universeId": universe.universe_id,
                "worldId": world.world_id,
                "wasDefaultWorld": was_default,
                "wasSpawnWorld": was_spawn,
                "deletedAt": world.to_public_dict(
                    project_id=project.project_id,
                    universe_id=universe.universe_id,
                ).get("deletedAt"),
            },
            metadata={
                "projectRouteId": project_id,
                "worldRouteId": world_id,
                "resolvedProjectId": project.project_id,
                "resolvedUniverseId": universe.universe_id,
                "resolvedWorldId": world.world_id,
                "dbBacked": True,
                "projectScoped": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc, code="world_not_found", status_code=404)
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return _error_response(exc)


# -----------------------------------------------------------------------------
# Dev convenience routes
# -----------------------------------------------------------------------------

@worlds_bp.get("/worlds")
def list_default_project_worlds():
    """
    Development convenience route.

    Lists worlds for the configured default project.

    Productive editor code should use:
        GET /projects/<project_id>/worlds
    """
    try:
        return list_project_worlds("default")
    except Exception as exc:
        return _error_response(exc)


@worlds_bp.post("/worlds")
def create_default_project_world():
    """
    Development convenience route.

    Creates a world for the configured default project.

    Productive editor code should use:
        POST /projects/<project_id>/worlds
    """
    try:
        return create_project_world("default")
    except Exception as exc:
        return _error_response(exc)


@worlds_bp.get("/worlds/<world_id>")
def get_default_project_world(world_id: str):
    """
    Development convenience route.

    Returns one world from the configured default project.

    Productive editor code should use:
        GET /projects/<project_id>/worlds/<world_id>
    """
    try:
        return get_project_world("default", world_id)
    except Exception as exc:
        return _error_response(exc)


@worlds_bp.patch("/worlds/<world_id>")
def patch_default_project_world(world_id: str):
    """
    Development convenience route.

    Patches one world from the configured default project.
    """
    try:
        return patch_project_world("default", world_id)
    except Exception as exc:
        return _error_response(exc)


@worlds_bp.delete("/worlds/<world_id>")
def delete_default_project_world(world_id: str):
    """
    Development convenience route.

    Soft-deletes one world from the configured default project.
    """
    try:
        return delete_project_world("default", world_id)
    except Exception as exc:
        return _error_response(exc)


# -----------------------------------------------------------------------------
# Status
# -----------------------------------------------------------------------------

@worlds_bp.get("/worlds/_status")
def get_worlds_route_status():
    """
    Return diagnostics for world routes, database and model registration.
    """
    try:
        check_database = _get_query_bool(
            "checkDatabase",
            "check_database",
            "db",
            fallback=False,
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

        database_status = get_database_status(
            current_app,
            check_connection=check_database,
        )

        model_status = get_model_debug_summary() if include_models else None

        counts = None
        if include_counts:
            try:
                counts = {
                    "projects": Project.query.count(),
                    "universes": Universe.query.count(),
                    "worlds": WorldInstance.query.count(),
                    "activeWorlds": WorldInstance.query.filter(WorldInstance.deleted_at.is_(None)).count(),
                }
            except Exception as exc:
                counts = {
                    "error": _safe_exception_message(exc),
                }

        config = None
        if include_config:
            config = {
                "defaultProjectId": _get_default_project_id(),
                "defaultUniverseId": _get_default_universe_id(),
                "defaultWorldId": _get_default_world_id(),
                "defaultTemplateId": _get_default_template_id(),
                "defaultProviderId": _get_default_provider_id(),
                "defaultProviderWorldId": _get_default_provider_world_id(),
                "databaseUriConfigured": bool(_get_config_value("SQLALCHEMY_DATABASE_URI")),
            }

        body = _ok_response(
            response_version=WORLD_STATUS_RESPONSE_VERSION,
            payload={
                "route": {
                    "source": ROUTE_SOURCE,
                    "moduleVersion": ROUTE_MODULE_VERSION,
                    "blueprint": worlds_bp.name,
                    "dbBacked": True,
                    "productiveRoutes": [
                        "GET /projects/<project_id>/worlds",
                        "POST /projects/<project_id>/worlds",
                        "GET /projects/<project_id>/worlds/<world_id>",
                        "PATCH /projects/<project_id>/worlds/<world_id>",
                        "DELETE /projects/<project_id>/worlds/<world_id>",
                    ],
                    "devConvenienceRoutes": [
                        "GET /worlds",
                        "POST /worlds",
                        "GET /worlds/<world_id>",
                        "PATCH /worlds/<world_id>",
                        "DELETE /worlds/<world_id>",
                    ],
                },
                "database": database_status,
                "models": model_status,
                "counts": counts,
                "config": config,
            },
            metadata={
                "checkDatabase": check_database,
                "includeModels": include_models,
                "includeCounts": include_counts,
                "includeConfig": include_config,
            },
        )

        return _json_response(body, 200)

    except Exception as exc:
        return _error_response(exc)


__all__ = (
    "worlds_bp",
    "ROUTE_MODULE_VERSION",
    "ROUTE_SOURCE",
)