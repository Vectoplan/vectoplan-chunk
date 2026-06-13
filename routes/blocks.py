# services/vectoplan-chunk/routes/blocks.py
"""
Block routes for the VECTOPLAN chunk service.

This module exposes PostgreSQL-backed project-scoped block/palette endpoints.

Primary route:

    GET /projects/<project_id>/worlds/<world_id>/blocks

Meaning:

    projectId
    -> concrete worldId
    -> blockRegistryId / blockRegistryVersion
    -> BlockRegistry / BlockType
    -> editor-compatible palette response

Important:
- `flat` is the provider/template world.
- `world_spawn` is the productive concrete world instance.
- Air is not a BlockType row.
- `cellValue = 0` always means Air.
- `cellValue = paletteIndex + 1` means a block from the returned palette.
- This file stays a thin HTTP adapter.
- It does not generate chunks.
- It does not execute commands.
- It does not write ChunkSnapshots.
- It does not write ChunkEvents.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import or_

from extensions import db, get_database_status
from models import (
    BlockRegistry,
    BlockType,
    Project,
    Universe,
    WorldInstance,
    get_model_debug_summary,
)


blocks_bp = Blueprint("blocks", __name__)

ROUTE_MODULE_VERSION = "0.2.0"
ROUTE_SOURCE = "routes.blocks"

BLOCKS_RESPONSE_VERSION = "world-blocks-response.v1"
BLOCKS_STATUS_RESPONSE_VERSION = "blocks-route-status-response.v1"

AIR_CELL_VALUE = 0
BLOCK_CELL_VALUE_RULE = "paletteIndex + 1"
CELL_ENCODING_VERSION = "cell-encoding.palette-index-plus-one.v1"

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


def _get_default_registry_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID", "debug-blocks")


def _get_default_registry_version() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION", "1")


def _get_default_api_prefix() -> str:
    return _get_env_string(ENV_ROUTE_DEFAULT_API_PREFIX, "")


# -----------------------------------------------------------------------------
# Project / universe / world resolution
# -----------------------------------------------------------------------------

def _normalize_project_route_id(project_id: str | None) -> tuple[str | None, bool]:
    """Normalize project route id."""
    text = _coerce_string(project_id)

    if text.lower() in _DEFAULT_PROJECT_ALIASES:
        return None, True

    return text, False


def _normalize_universe_route_id(universe_id: str | None) -> tuple[str | None, bool]:
    """Normalize universe route id."""
    text = _coerce_string(universe_id)

    if text.lower() in _DEFAULT_UNIVERSE_ALIASES:
        return None, True

    return text, False


def _normalize_world_route_id(world_id: str | None) -> tuple[str | None, bool]:
    """Normalize world route id."""
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


def _resolve_project_world_context(
    project_id: str,
    world_id: str,
    *,
    universe_id: str | None = None,
    include_deleted: bool = False,
) -> tuple[Project, Universe, WorldInstance]:
    """Resolve project, universe and world."""
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

    project = _get_project_or_404(
        effective_project_id,
        include_deleted=include_deleted,
    )
    universe = _get_universe_or_404(
        project,
        universe_id,
        include_deleted=include_deleted,
    )
    world = _get_world_or_404(
        universe,
        world_id,
        include_deleted=include_deleted,
    )

    return project, universe, world


# -----------------------------------------------------------------------------
# Registry / palette helpers
# -----------------------------------------------------------------------------

def _get_registry_for_world(world: WorldInstance) -> BlockRegistry:
    """Load BlockRegistry for a world."""
    registry_id = world.block_registry_id or _get_default_registry_id()
    registry_version = world.block_registry_version or _get_default_registry_version()

    registry = BlockRegistry.query.filter_by(
        registry_id=registry_id,
        registry_version=registry_version,
    ).one_or_none()

    if registry is None:
        raise LookupError(
            f"Block registry '{registry_id}@{registry_version}' was not found."
        )

    return registry


def _query_block_types_for_registry(
    registry: BlockRegistry,
    *,
    include_inactive: bool = False,
    include_deleted: bool = False,
    search: str = "",
):
    """Build BlockType query for registry."""
    query = BlockType.query.filter(BlockType.registry_db_id == registry.id)

    if not include_deleted:
        query = query.filter(BlockType.deleted_at.is_(None))

    if not include_inactive:
        query = query.filter(BlockType.status == "active")

    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                BlockType.block_type_id.ilike(like),
                BlockType.label.ilike(like),
                BlockType.category.ilike(like),
                BlockType.material_id.ilike(like),
                BlockType.texture_id.ilike(like),
            )
        )

    return query


def _sort_blocks_for_palette(blocks: list[BlockType]) -> list[BlockType]:
    """Sort blocks in deterministic palette order."""
    return sorted(
        list(blocks or []),
        key=lambda block: (
            block.default_palette_index is None,
            block.default_palette_index if block.default_palette_index is not None else 999999,
            block.block_type_id,
        ),
    )


def _serialize_air_entry() -> dict[str, Any]:
    """Serialize the invariant Air entry."""
    return {
        "cellValue": AIR_CELL_VALUE,
        "blockTypeId": None,
        "label": "Air",
        "solid": False,
        "opaque": False,
        "placeable": False,
        "breakable": False,
        "selectable": False,
        "collidable": False,
    }


def _serialize_block_palette(
    blocks: list[BlockType],
    *,
    include_metadata: bool = True,
    include_raw: bool = True,
) -> list[dict[str, Any]]:
    """
    Serialize BlockType rows as editor-compatible palette entries.

    The palette index used in the response is computed from the returned order.
    This keeps the hard invariant:

        cellValue = paletteIndex + 1
    """
    palette: list[dict[str, Any]] = []

    for palette_index, block in enumerate(_sort_blocks_for_palette(blocks)):
        entry = block.to_palette_entry(
            palette_index=palette_index,
            include_metadata=include_metadata,
        )

        entry["defaultPaletteIndex"] = block.default_palette_index
        entry["computedPaletteIndex"] = palette_index
        entry["cellValue"] = palette_index + 1

        if include_raw:
            entry["raw"] = block.to_dict(
                include_internal=False,
                include_metadata=include_metadata,
            )

        palette.append(entry)

    return palette


def _serialize_blocks_response(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    registry: BlockRegistry,
    blocks: list[BlockType],
    include_context: bool = False,
    include_metadata: bool = True,
    include_raw: bool = True,
    include_route_hints: bool = True,
    api_prefix: str = "",
) -> dict[str, Any]:
    """Serialize world block registry/palette response."""
    palette = _serialize_block_palette(
        blocks,
        include_metadata=include_metadata,
        include_raw=include_raw,
    )

    body: dict[str, Any] = {
        "projectId": project.project_id,
        "universeId": universe.universe_id,
        "worldId": world.world_id,
        "templateId": world.template_id,
        "providerId": world.provider_id,
        "providerWorldId": world.provider_world_id,
        "blockRegistryId": registry.registry_id,
        "blockRegistryVersion": registry.registry_version,
        "registry": registry.to_dict(
            include_internal=False,
            include_metadata=include_metadata,
            include_blocks=False,
        ),
        "blocks": {
            "air": _serialize_air_entry(),
            "encoding": {
                "version": CELL_ENCODING_VERSION,
                "airCellValue": AIR_CELL_VALUE,
                "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
            },
            "blocks": palette,
            "palette": palette,
            "counts": {
                "blocks": len(blocks),
                "paletteEntries": len(palette),
                "includingAir": len(palette) + 1,
            },
        },
    }

    if include_context:
        body["context"] = {
            "projectScoped": True,
            "projectId": project.project_id,
            "universeId": universe.universe_id,
            "worldId": world.world_id,
            "templateId": world.template_id,
            "providerId": world.provider_id,
            "providerWorldId": world.provider_world_id,
            "generatorType": world.generator_type,
            "generatorVersion": world.generator_version,
            "chunkSize": world.chunk_size,
            "cellSize": world.cell_size,
            "coordinateSystem": world.coordinate_system,
            "projectionType": world.projection_type,
            "topologyType": world.topology_type,
        }

    if include_route_hints:
        prefix = _coerce_string(api_prefix).rstrip("/")
        body["routeHints"] = {
            "projectBootstrap": f"{prefix}/projects/{project.project_id}/bootstrap",
            "worlds": f"{prefix}/projects/{project.project_id}/worlds",
            "world": f"{prefix}/projects/{project.project_id}/worlds/{world.world_id}",
            "blocks": f"{prefix}/projects/{project.project_id}/worlds/{world.world_id}/blocks",
            "chunk": f"{prefix}/projects/{project.project_id}/worlds/{world.world_id}/chunks",
            "chunksBatch": f"{prefix}/projects/{project.project_id}/worlds/{world.world_id}/chunks/batch",
            "commands": f"{prefix}/projects/{project.project_id}/worlds/{world.world_id}/commands",
        }

    body["route"] = {
        "source": ROUTE_SOURCE,
        "projectId": project.project_id,
        "universeId": universe.universe_id,
        "worldId": world.world_id,
        "templateId": world.template_id,
        "providerWorldId": world.provider_world_id,
        "projectScoped": True,
        "dbBacked": True,
    }

    return body


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@blocks_bp.get("/projects/<project_id>/worlds/<world_id>/blocks")
def get_project_world_blocks(project_id: str, world_id: str):
    """
    Return block/palette data for a concrete project world.

    Example:
        GET /projects/dev-project/worlds/world_spawn/blocks
    """
    try:
        universe_id = _get_query_string("universeId", "universe_id", fallback="") or None
        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        include_inactive = _get_query_bool("includeInactive", "include_inactive", fallback=False)
        include_context = _get_query_bool("includeContext", "include_context", fallback=False)
        include_metadata = _get_query_bool("includeMetadata", "include_metadata", fallback=True)
        include_raw = _get_query_bool("includeRaw", "include_raw", fallback=True)
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        search = _get_query_string("q", "search", fallback="")
        api_prefix = _get_query_string("apiPrefix", "api_prefix", fallback=_get_default_api_prefix())

        project, universe, world = _resolve_project_world_context(
            project_id,
            world_id,
            universe_id=universe_id,
            include_deleted=include_deleted,
        )

        registry = _get_registry_for_world(world)

        block_query = _query_block_types_for_registry(
            registry,
            include_inactive=include_inactive,
            include_deleted=include_deleted,
            search=search,
        )

        blocks = block_query.all()

        body = _ok_response(
            response_version=BLOCKS_RESPONSE_VERSION,
            payload=_serialize_blocks_response(
                project=project,
                universe=universe,
                world=world,
                registry=registry,
                blocks=blocks,
                include_context=include_context,
                include_metadata=include_metadata,
                include_raw=include_raw,
                include_route_hints=include_route_hints,
                api_prefix=api_prefix,
            ),
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": project.project_id,
                "worldRouteId": world_id,
                "resolvedWorldId": world.world_id,
                "universeId": universe.universe_id,
                "templateId": world.template_id,
                "providerWorldId": world.provider_world_id,
                "registryId": registry.registry_id,
                "registryVersion": registry.registry_version,
                "includeDeleted": include_deleted,
                "includeInactive": include_inactive,
                "includeContext": include_context,
                "includeMetadata": include_metadata,
                "includeRaw": include_raw,
                "includeRouteHints": include_route_hints,
                "search": search,
                "dbBacked": True,
                "projectScoped": True,
            },
        )

        return _json_response(body, 200)

    except LookupError as exc:
        return _error_response(exc, code="blocks_context_not_found", status_code=404)
    except Exception as exc:
        return _error_response(exc)


@blocks_bp.get("/projects/<project_id>/blocks")
def get_project_default_world_blocks(project_id: str):
    """
    Development convenience route.

    Returns blocks for the project's spawn/default world.

    Productive editor code should prefer:
        GET /projects/<project_id>/worlds/<world_id>/blocks
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

        project = _get_project_or_404(effective_project_id)
        universe = _get_default_universe_for_project(project)

        world_id = universe.spawn_world_id or universe.default_world_id or _get_default_world_id()

        return get_project_world_blocks(project.project_id, world_id)

    except Exception as exc:
        return _error_response(exc)


@blocks_bp.get("/blocks")
def get_default_project_world_blocks():
    """
    Development convenience route.

    Returns blocks for the configured default project's spawn world.

    Productive editor code should use:
        GET /projects/<project_id>/worlds/<world_id>/blocks
    """
    try:
        return get_project_default_world_blocks("default")
    except Exception as exc:
        return _error_response(exc)


@blocks_bp.get("/blocks/_status")
def get_blocks_route_status():
    """
    Return diagnostics for block routes, database and model registration.
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
                    "worlds": WorldInstance.query.count(),
                    "blockRegistries": BlockRegistry.query.count(),
                    "activeBlockRegistries": BlockRegistry.query.filter(BlockRegistry.deleted_at.is_(None)).count(),
                    "blockTypes": BlockType.query.count(),
                    "activeBlockTypes": BlockType.query.filter(BlockType.status == "active").count(),
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
                "defaultBlockRegistryId": _get_default_registry_id(),
                "defaultBlockRegistryVersion": _get_default_registry_version(),
                "cellEncoding": {
                    "version": CELL_ENCODING_VERSION,
                    "airCellValue": AIR_CELL_VALUE,
                    "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
                },
                "databaseUriConfigured": bool(_get_config_value("SQLALCHEMY_DATABASE_URI")),
            }

        body = _ok_response(
            response_version=BLOCKS_STATUS_RESPONSE_VERSION,
            payload={
                "route": {
                    "source": ROUTE_SOURCE,
                    "moduleVersion": ROUTE_MODULE_VERSION,
                    "blueprint": blocks_bp.name,
                    "dbBacked": True,
                    "productiveRoutes": [
                        "GET /projects/<project_id>/worlds/<world_id>/blocks",
                    ],
                    "devConvenienceRoutes": [
                        "GET /projects/<project_id>/blocks",
                        "GET /blocks",
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
    "blocks_bp",
    "ROUTE_MODULE_VERSION",
    "ROUTE_SOURCE",
)