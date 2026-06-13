# services/vectoplan-chunk/routes/projects.py
"""
Project routes for the VECTOPLAN chunk service.

This module is the project-level HTTP adapter for PostgreSQL-backed project
state.

Main responsibilities:
- list projects
- create projects
- read projects
- patch projects
- soft-delete projects
- return project bootstrap data for the editor
- create the default Universe + world_spawn for new projects

Current persistent hierarchy:

    Project
      -> Universe
          -> WorldInstance(world_spawn)
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Default new-project mapping:

    projectId       = generated or provided
    universeId      = dev-universe or provided default
    worldId         = world_spawn
    templateId      = flat
    providerWorldId = flat

Important:
- This file is still a route adapter.
- It does not generate chunks.
- It does not execute chunk commands.
- It does not write ChunkSnapshots.
- It does not write ChunkEvents.
- It performs only project/universe/world bootstrap persistence needed for
  editor project creation.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request

from extensions import db, get_database_status
from models import (
    Project,
    Universe,
    WorldInstance,
    get_model_debug_summary,
)


projects_bp = Blueprint("projects", __name__)

ROUTE_MODULE_VERSION = "0.2.0"
ROUTE_SOURCE = "routes.projects"

PROJECT_RESPONSE_VERSION = "project-response.v1"
PROJECT_LIST_RESPONSE_VERSION = "project-list-response.v1"
PROJECT_CREATE_RESPONSE_VERSION = "project-create-response.v1"
PROJECT_PATCH_RESPONSE_VERSION = "project-patch-response.v1"
PROJECT_DELETE_RESPONSE_VERSION = "project-delete-response.v1"
PROJECT_BOOTSTRAP_RESPONSE_VERSION = "project-bootstrap-response.v1"
PROJECT_STATUS_RESPONSE_VERSION = "projects-route-status-response.v1"
PROJECT_CACHE_RESET_RESPONSE_VERSION = "projects-route-cache-reset-response.v1"

ENV_ROUTE_INCLUDE_DEBUG_ERRORS = "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS"
ENV_ROUTE_ALLOW_DEFAULT_PROJECT = "VECTOPLAN_CHUNK_ROUTE_ALLOW_DEFAULT_PROJECT"
ENV_ROUTE_DEFAULT_API_PREFIX = "VECTOPLAN_CHUNK_API_PREFIX"

_DEFAULT_PROJECT_ALIASES = {
    "",
    "default",
    "_default",
    "current",
    "_current",
    "dev",
    "_dev",
}


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


def _coerce_int(value: Any, *, fallback: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
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


def _get_config_int(name: str, fallback: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
    """Read int config defensively."""
    return _coerce_int(_get_config_value(name, fallback), fallback=fallback, minimum=minimum, maximum=maximum)


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
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", "dev-project")


def _get_default_project_slug() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG", _get_default_project_id())


def _get_default_project_name() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME", "Dev Project")


def _get_default_universe_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", "dev-universe")


def _get_default_universe_slug() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG", _get_default_universe_id())


def _get_default_universe_name() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME", "Dev Universe")


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


def _resolve_effective_project_id(project_id: str | None, *, allow_default_project: bool = False) -> str:
    """Resolve project id with optional default alias support."""
    normalized_project_id, route_allows_default = _normalize_project_route_id(project_id)

    if normalized_project_id:
        return normalized_project_id

    if allow_default_project or route_allows_default:
        return _get_default_project_id()

    raise ValueError("projectId is required.")


def _get_project_or_404(project_id: str, *, include_deleted: bool = False) -> Project:
    """Load project by public project id."""
    query = Project.query.filter(Project.project_id == project_id)

    if not include_deleted:
        query = query.filter(Project.deleted_at.is_(None))

    project = query.one_or_none()

    if project is None:
        raise LookupError(f"Project '{project_id}' was not found.")

    return project


def _get_project_default_universe(project: Project, *, include_deleted: bool = False) -> Universe:
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
        # Fallback: first active universe in this project.
        fallback_query = Universe.query.filter(Universe.project_db_id == project.id)
        if not include_deleted:
            fallback_query = fallback_query.filter(Universe.deleted_at.is_(None))
        universe = fallback_query.order_by(Universe.created_at.asc()).first()

    if universe is None:
        raise LookupError(f"Project '{project.project_id}' has no universe.")

    return universe


def _get_universe_spawn_world(universe: Universe, *, include_deleted: bool = False) -> WorldInstance:
    """Load spawn/default world for universe."""
    world_id = universe.spawn_world_id or universe.default_world_id or _get_default_world_id()

    query = WorldInstance.query.filter(
        WorldInstance.universe_db_id == universe.id,
        WorldInstance.world_id == world_id,
    )

    if not include_deleted:
        query = query.filter(WorldInstance.deleted_at.is_(None))

    world = query.one_or_none()

    if world is None and universe.default_world_id and universe.default_world_id != world_id:
        fallback_query = WorldInstance.query.filter(
            WorldInstance.universe_db_id == universe.id,
            WorldInstance.world_id == universe.default_world_id,
        )
        if not include_deleted:
            fallback_query = fallback_query.filter(WorldInstance.deleted_at.is_(None))
        world = fallback_query.one_or_none()

    if world is None:
        fallback_query = WorldInstance.query.filter(WorldInstance.universe_db_id == universe.id)
        if not include_deleted:
            fallback_query = fallback_query.filter(WorldInstance.deleted_at.is_(None))
        world = fallback_query.order_by(WorldInstance.created_at.asc()).first()

    if world is None:
        raise LookupError(f"Universe '{universe.universe_id}' has no world.")

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
    project_dict = project.to_dict(include_internal=False, include_metadata=include_metadata)
    universe_dict = universe.to_dict(
        include_internal=False,
        include_metadata=include_metadata,
        project_id=project.project_id,
    )
    world_dict = world.to_dict(
        include_internal=False,
        include_metadata=include_metadata,
        project_id=project.project_id,
        universe_id=universe.universe_id,
    )

    body: dict[str, Any] = {
        "projectId": project.project_id,
        "universeId": universe.universe_id,
        "defaultWorldId": universe.default_world_id or world.world_id,
        "spawnWorldId": universe.spawn_world_id or world.world_id,
        "project": project_dict,
        "universe": universe_dict,
        "spawnWorld": world_dict,
        "world": world_dict,
    }

    if include_worlds:
        worlds_query = WorldInstance.query.filter(WorldInstance.universe_db_id == universe.id)
        worlds_query = worlds_query.filter(WorldInstance.deleted_at.is_(None))
        worlds = worlds_query.order_by(WorldInstance.created_at.asc()).all()

        body["worlds"] = [
            item.to_dict(
                include_internal=False,
                include_metadata=include_metadata,
                project_id=project.project_id,
                universe_id=universe.universe_id,
            )
            for item in worlds
        ]
        body["counts"] = {
            "worlds": len(body["worlds"]),
        }

    if include_route_hints:
        body["routeHints"] = _build_route_hints(
            project_id=project.project_id,
            world_id=world.world_id,
            api_prefix=api_prefix,
        )

    body["context"] = {
        "projectScoped": True,
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

    return body


def _create_default_universe_for_project(
    project: Project,
    *,
    payload: Mapping[str, Any] | None = None,
    created_by_user_id: Optional[str] = None,
) -> Universe:
    """Create default Universe for a project."""
    payload = payload or {}

    universe_id = (
        payload.get("universeId")
        or payload.get("universe_id")
        or payload.get("defaultUniverseId")
        or payload.get("default_universe_id")
        or project.default_universe_id
        or _get_default_universe_id()
    )

    universe = Universe.create(
        project_db_id=project.id,
        universe_id=universe_id,
        slug=payload.get("universeSlug") or payload.get("universe_slug") or _get_default_universe_slug(),
        name=payload.get("universeName") or payload.get("universe_name") or _get_default_universe_name(),
        default_world_id=payload.get("worldId") or payload.get("world_id") or _get_default_world_id(),
        spawn_world_id=payload.get("spawnWorldId") or payload.get("spawn_world_id") or payload.get("worldId") or payload.get("world_id") or _get_default_world_id(),
        created_by_user_id=created_by_user_id,
        metadata_json={
            "createdByRoute": ROUTE_SOURCE,
            "createdAsDefault": True,
        },
    )

    db.session.add(universe)
    db.session.flush()

    if not project.default_universe_id:
        project.set_default_universe_id(universe.universe_id, updated_by_user_id=created_by_user_id)

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

    world = WorldInstance.create(
        project_db_id=universe.project_db_id,
        universe_db_id=universe.id,
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
        metadata_json={
            "createdByRoute": ROUTE_SOURCE,
            "createdAsSpawnWorld": True,
        },
    )

    db.session.add(world)
    db.session.flush()

    changed_universe = False
    if not universe.default_world_id:
        universe.set_default_world_id(world.world_id, updated_by_user_id=created_by_user_id)
        changed_universe = True

    if not universe.spawn_world_id:
        universe.set_spawn_world_id(world.world_id, updated_by_user_id=created_by_user_id)
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

    project = Project.from_create_payload(
        payload,
        created_by_user_id=created_by_user_id,
    )

    if project.slug is None:
        project.slug = project.project_id

    if project.default_universe_id is None:
        project.default_universe_id = (
            payload.get("universeId")
            or payload.get("universe_id")
            or payload.get("defaultUniverseId")
            or payload.get("default_universe_id")
            or _get_default_universe_id()
        )

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

    if not include_deleted:
        query = query.filter(Project.deleted_at.is_(None))

    if not include_archived:
        query = query.filter(Project.status != "archived")

    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(
                Project.project_id.ilike(like),
                Project.slug.ilike(like),
                Project.name.ilike(like),
            )
        )

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
    result = project.to_dict(
        include_internal=include_internal,
        include_metadata=include_metadata,
    )

    if not include_universes:
        return result

    universes = Universe.query.filter_by(project_db_id=project.id)
    universes = universes.filter(Universe.deleted_at.is_(None))
    universes = universes.order_by(Universe.created_at.asc()).all()

    result["universes"] = []
    result["universeCount"] = len(universes)

    for universe in universes:
        universe_item = universe.to_dict(
            include_internal=include_internal,
            include_metadata=include_metadata,
            project_id=project.project_id,
        )

        if include_worlds:
            worlds = WorldInstance.query.filter_by(universe_db_id=universe.id)
            worlds = worlds.filter(WorldInstance.deleted_at.is_(None))
            worlds = worlds.order_by(WorldInstance.created_at.asc()).all()

            universe_item["worlds"] = [
                world.to_dict(
                    include_internal=include_internal,
                    include_metadata=include_metadata,
                    project_id=project.project_id,
                    universe_id=universe.universe_id,
                )
                for world in worlds
            ]
            universe_item["worldCount"] = len(worlds)

        result["universes"].append(universe_item)

    return result


# -----------------------------------------------------------------------------
# Routes
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
                "resolvedProjectId": project.project_id,
                "resolvedUniverseId": universe.universe_id,
                "resolvedWorldId": world.world_id,
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
    List projects from PostgreSQL.
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
        projects = (
            query
            .order_by(Project.created_at.desc())
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
    Create a new project with default Universe and world_spawn.

    Request body example:

        {
          "projectId": "my-project",
          "name": "My Project",
          "universeId": "dev-universe",
          "worldId": "world_spawn"
        }
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
                "resolvedProjectId": project.project_id,
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
    Patch a project.

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
        project.apply_patch_payload(payload, updated_by_user_id=updated_by_user_id)
        db.session.add(project)
        db.session.commit()

        body = _ok_response(
            response_version=PROJECT_PATCH_RESPONSE_VERSION,
            payload={
                "changed": True,
                "project": project.to_public_dict(),
            },
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": project.project_id,
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
    Soft-delete a project.

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
        project.soft_delete(updated_by_user_id=updated_by_user_id)

        # Soft-delete direct child universes/worlds for API consistency.
        universes = Universe.query.filter_by(project_db_id=project.id).all()
        for universe in universes:
            if not universe.is_deleted:
                universe.soft_delete(updated_by_user_id=updated_by_user_id)

        worlds = WorldInstance.query.filter_by(project_db_id=project.id).all()
        for world in worlds:
            if not world.is_deleted:
                world.soft_delete(updated_by_user_id=updated_by_user_id)

        db.session.commit()

        body = _ok_response(
            response_version=PROJECT_DELETE_RESPONSE_VERSION,
            payload={
                "deleted": True,
                "softDelete": True,
                "projectId": project.project_id,
                "deletedAt": project.to_public_dict().get("deletedAt"),
                "counts": {
                    "universesSoftDeleted": len(universes),
                    "worldsSoftDeleted": len(worlds),
                },
            },
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": project.project_id,
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


@projects_bp.get("/projects/_status")
def get_projects_route_status():
    """
    Return diagnostics for project routes, database and model registration.
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
                    "activeProjects": Project.query.filter(Project.deleted_at.is_(None)).count(),
                    "universes": Universe.query.count(),
                    "worlds": WorldInstance.query.count(),
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
                "autoCreateAll": _get_config_bool("VECTOPLAN_CHUNK_AUTO_CREATE_ALL", False),
                "autoSeedDefaults": _get_config_bool("VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS", True),
            }

        body = _ok_response(
            response_version=PROJECT_STATUS_RESPONSE_VERSION,
            payload={
                "route": {
                    "source": ROUTE_SOURCE,
                    "moduleVersion": ROUTE_MODULE_VERSION,
                    "blueprint": projects_bp.name,
                    "dbBacked": True,
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


@projects_bp.post("/projects/_cache/reset")
def reset_projects_route_cache():
    """
    Reset optional legacy caches.

    This route does not alter PostgreSQL state.
    """
    try:
        reset_results: dict[str, Any] = {}

        # Compatibility with older world_state cache modules, if they still exist.
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


__all__ = (
    "projects_bp",
    "ROUTE_MODULE_VERSION",
    "ROUTE_SOURCE",
)