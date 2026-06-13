# services/vectoplan-chunk/routes/chunks.py
"""
Chunk routes for the VECTOPLAN chunk service.

This module exposes project-scoped chunk load endpoints.

Primary routes:

    GET  /projects/<project_id>/worlds/<world_id>/chunks
    POST /projects/<project_id>/worlds/<world_id>/chunks/batch

Read path:

    1. Resolve Project / Universe / WorldInstance from PostgreSQL.
    2. Check ChunkSnapshot in PostgreSQL.
    3. If snapshot exists:
         -> return snapshot as current load-truth.
    4. If no snapshot exists:
         -> generate the chunk through the provider/template world layer.
    5. Optionally materialize generated chunks when explicitly requested.

Important:
- `flat` is the provider/template world.
- `world_spawn` is the productive concrete world instance.
- ChunkSnapshot is load-truth for materialized chunks.
- Generated chunks are not stored unless `materializeGenerated=true`.
- ChunkEvents are not replayed on normal load.
- This file does not execute SetBlock/RemoveBlock.
- This file does not know FlatWorld internals.

Robustness notes:
- All ORM lookups in the chunk read path explicitly disable relationship loading
  via noload("*"). Chunk routes must never load Project -> Universe -> World ->
  Snapshot/Event/Command relationship graphs.
- Error responses are intentionally shallow and JSON-safe.
- Runtime chunk responses are intentionally built from explicit fields only.
- Generated chunks and snapshots are normalized into a flat RuntimeChunkContent-like
  response shape.
"""

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request

try:
    from sqlalchemy.orm import noload
except Exception:  # pragma: no cover - defensive fallback for unusual test envs
    noload = None  # type: ignore[assignment]

from extensions import db, get_database_status
from models import (
    ChunkSnapshot,
    Project,
    Universe,
    WorldInstance,
    get_model_debug_summary,
)


chunks_bp = Blueprint("chunks", __name__)

ROUTE_MODULE_VERSION = "0.3.0"
ROUTE_SOURCE = "routes.chunks"

CHUNK_RESPONSE_VERSION = "world-state-chunk-response.v1"
CHUNK_BATCH_RESPONSE_VERSION = "world-state-chunk-batch-response.v1"
CHUNKS_STATUS_RESPONSE_VERSION = "chunks-route-status-response.v1"

RUNTIME_CHUNK_CONTENT_VERSION = "runtime-chunk-content.v1"
CELL_ENCODING_VERSION = "cell-encoding.palette-index-plus-one.v1"
CELL_INDEX_ORDER = "x-fastest-y-then-z"
AIR_CELL_VALUE = 0
BLOCK_CELL_VALUE_RULE = "paletteIndex + 1"

DEFAULT_MAX_BATCH_CHUNKS = 256
DEFAULT_JSON_SAFE_MAX_DEPTH = 80

ENV_ROUTE_INCLUDE_DEBUG_ERRORS = "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS"
ENV_ROUTE_DEBUG_CHECKPOINTS = "VECTOPLAN_CHUNK_ROUTE_DEBUG_CHECKPOINTS"
ENV_ROUTE_DEFAULT_API_PREFIX = "VECTOPLAN_CHUNK_API_PREFIX"
ENV_ROUTE_MAX_BATCH_CHUNKS = "VECTOPLAN_CHUNK_ROUTE_MAX_BATCH_CHUNKS"
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
    fallback: int | None = None,
    field_name: str = "value",
) -> int:
    """Coerce arbitrary value to int or raise ValueError."""
    if value is None or str(value).strip() == "":
        if fallback is not None:
            return int(fallback)

        raise ValueError(f"{field_name} is required.")

    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


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


def _make_json_safe(
    value: Any,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
    max_depth: int = DEFAULT_JSON_SAFE_MAX_DEPTH,
) -> Any:
    """
    Convert values into JSON-safe structures.

    This function is deliberately recursion-safe. Routes must never crash or
    hang because an ORM/provider object accidentally contains cycles.
    """
    if _depth > max_depth:
        return "<max-depth-exceeded>"

    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (datetime, date)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)

    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "size": len(value),
        }

    if _seen is None:
        _seen = set()

    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in _seen:
            return "<recursive-reference>"

        _seen.add(value_id)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                try:
                    safe_key = str(key)
                except Exception:
                    safe_key = "<unserializable-key>"

                result[safe_key] = _make_json_safe(
                    item,
                    _seen=_seen,
                    _depth=_depth + 1,
                    max_depth=max_depth,
                )
            return result
        finally:
            _seen.discard(value_id)

    if isinstance(value, (list, tuple, set, frozenset)):
        value_id = id(value)
        if value_id in _seen:
            return "<recursive-reference>"

        _seen.add(value_id)
        try:
            return [
                _make_json_safe(
                    item,
                    _seen=_seen,
                    _depth=_depth + 1,
                    max_depth=max_depth,
                )
                for item in value
            ]
        finally:
            _seen.discard(value_id)

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


def _get_env_int(name: str, fallback: int) -> int:
    """Read integer env var defensively."""
    try:
        value = os.environ.get(name)
    except Exception:
        return int(fallback)

    try:
        return int(value)
    except Exception:
        return int(fallback)


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


def _get_query_int(
    *names: str,
    fallback: int | None = None,
    field_name: str = "value",
) -> int:
    """Read query integer."""
    value = _get_query_value(*names, fallback=None)

    return _coerce_int(
        value,
        fallback=fallback,
        field_name=field_name,
    )


def _get_query_string(*names: str, fallback: str = "") -> str:
    """Read query string."""
    value = _get_query_value(*names, fallback=None)

    if value is None:
        return str(fallback)

    return _coerce_string(value, fallback=fallback)


def _get_json_body() -> dict[str, Any]:
    """Read a JSON request body safely."""
    try:
        data = request.get_json(silent=True)
    except Exception as exc:
        raise ValueError(f"Invalid JSON body: {_safe_exception_message(exc)}") from exc

    if data is None:
        return {}

    if not isinstance(data, Mapping):
        raise ValueError("JSON body must be an object.")

    return dict(data)


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


def _debug_checkpoints_enabled() -> bool:
    """Return whether route checkpoints should be logged."""
    query_debug = _get_query_bool(
        "debugRoute",
        "debug_route",
        "chunkDebug",
        "chunk_debug",
        fallback=False,
    )
    env_debug = _get_env_bool(ENV_ROUTE_DEBUG_CHECKPOINTS, fallback=False)
    return bool(query_debug or env_debug)


def _log_checkpoint(label: str, **fields: Any) -> None:
    """Write an optional shallow debug checkpoint."""
    if not _debug_checkpoints_enabled():
        return

    try:
        current_app.logger.warning(
            "CHUNK_ROUTE_DEBUG %s %s",
            label,
            _make_json_safe(fields, max_depth=10),
        )
    except Exception:
        pass


def _safe_rollback() -> None:
    """Rollback DB session defensively."""
    try:
        db.session.rollback()
    except Exception:
        pass


def _json_response(body: Mapping[str, Any], status_code: int = 200):
    """Return JSON response."""
    safe_body = _make_json_safe(dict(body))
    return jsonify(safe_body), int(status_code)


def _route_metadata(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build route metadata."""
    metadata = {
        "routeSource": ROUTE_SOURCE,
        "routeModuleVersion": ROUTE_MODULE_VERSION,
    }

    if extra:
        metadata.update(_make_json_safe(dict(extra), max_depth=20))

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
        body.update(_make_json_safe(dict(payload), max_depth=60))

    body["metadata"] = _route_metadata(metadata)
    return body


def _error_body(
    error: BaseException | Any,
    *,
    code: str = "route_error",
    status_code: int = 500,
) -> tuple[dict[str, Any], int]:
    """Build standard shallow error body."""
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


def _safe_one_or_none(query: Any, *, entity_name: str, lookup: Mapping[str, Any]) -> Any | None:
    """
    Execute a query and return zero or one row.

    Uses LIMIT 2 instead of one_or_none() so duplicate data yields a controlled
    application error and avoids eager loading surprises in some SQLAlchemy setups.
    """
    try:
        rows = query.limit(2).all()
    except Exception as exc:
        raise RuntimeError(
            f"Database lookup failed for {entity_name}."
        ) from exc

    if len(rows) > 1:
        raise RuntimeError(
            f"Database lookup for {entity_name} returned multiple rows: "
            f"{_make_json_safe(dict(lookup), max_depth=5)}"
        )

    return rows[0] if rows else None


def _query_without_relationships(query: Any) -> Any:
    """
    Disable ORM relationship loading for chunk read queries.

    This is critical for the chunk route. A chunk read must not load:
    Project -> Universes -> Worlds -> Snapshots -> Events -> Commands.
    """
    if noload is None:
        return query

    try:
        return query.options(noload("*"))
    except Exception:
        return query


# -----------------------------------------------------------------------------
# Default config helpers
# -----------------------------------------------------------------------------

def _get_default_project_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID", "dev-project")


def _get_default_universe_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID", "dev-universe")


def _get_default_world_id() -> str:
    return _get_config_string("VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID", "world_spawn")


def _get_default_api_prefix() -> str:
    return _get_env_string(ENV_ROUTE_DEFAULT_API_PREFIX, "")


def _get_batch_max_count() -> int:
    configured = _get_config_value("VECTOPLAN_CHUNK_ROUTE_MAX_BATCH_CHUNKS", None)
    if configured is not None:
        return max(1, _coerce_int(configured, fallback=DEFAULT_MAX_BATCH_CHUNKS))

    return max(
        1,
        _get_env_int(
            ENV_ROUTE_MAX_BATCH_CHUNKS,
            DEFAULT_MAX_BATCH_CHUNKS,
        ),
    )


# -----------------------------------------------------------------------------
# Coordinate helpers
# -----------------------------------------------------------------------------

def _build_chunk_key(chunk_x: int, chunk_y: int, chunk_z: int) -> str:
    """Build canonical chunk key."""
    return f"{int(chunk_x)}:{int(chunk_y)}:{int(chunk_z)}"


def _normalize_chunk_item(item: Any) -> dict[str, Any]:
    """Normalize one chunk coordinate item."""
    if isinstance(item, Mapping):
        chunk_x = _coerce_int(
            item.get("chunkX", item.get("chunk_x", item.get("x"))),
            field_name="chunkX",
        )
        chunk_y = _coerce_int(
            item.get("chunkY", item.get("chunk_y", item.get("y"))),
            field_name="chunkY",
        )
        chunk_z = _coerce_int(
            item.get("chunkZ", item.get("chunk_z", item.get("z"))),
            field_name="chunkZ",
        )
    elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
        if len(item) != 3:
            raise ValueError("Chunk coordinate arrays must have exactly 3 items.")
        chunk_x = _coerce_int(item[0], field_name="chunkX")
        chunk_y = _coerce_int(item[1], field_name="chunkY")
        chunk_z = _coerce_int(item[2], field_name="chunkZ")
    else:
        raise ValueError("Chunk item must be an object or a 3-item array.")

    return {
        "chunkX": chunk_x,
        "chunkY": chunk_y,
        "chunkZ": chunk_z,
        "chunkKey": _build_chunk_key(chunk_x, chunk_y, chunk_z),
    }


def _normalize_chunk_items(items: Sequence[Any], *, max_count: int) -> list[dict[str, Any]]:
    """Normalize chunk coordinate list."""
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise ValueError("chunks must be a JSON array.")

    if len(items) > max_count:
        raise ValueError(f"Too many chunks requested. Maximum is {max_count}.")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in items:
        normalized_item = _normalize_chunk_item(item)
        key = str(normalized_item["chunkKey"])

        if key in seen:
            continue

        seen.add(key)
        normalized.append(normalized_item)

    return normalized


def _extract_batch_chunks_from_body_or_query() -> list[dict[str, Any]]:
    """
    Extract chunk coordinate list for batch loading.

    Supported JSON body forms:
        { "chunks": [ { "chunkX": 0, "chunkY": 0, "chunkZ": 0 } ] }
        { "items": [...] }
        { "requests": [...] }

    Supported query fallback:
        ?chunkX=0&chunkY=0&chunkZ=0
    """
    body = _get_json_body()

    chunks = None

    for key in ("chunks", "items", "requests"):
        if key in body:
            chunks = body[key]
            break

    if chunks is None and any(name in request.args for name in ("chunkX", "chunk_x", "x")):
        chunks = [
            {
                "chunkX": _get_query_value("chunkX", "chunk_x", "x"),
                "chunkY": _get_query_value("chunkY", "chunk_y", "y"),
                "chunkZ": _get_query_value("chunkZ", "chunk_z", "z"),
            }
        ]

    if chunks is None:
        raise ValueError("Batch request requires a chunks array.")

    return _normalize_chunk_items(
        chunks,
        max_count=_get_batch_max_count(),
    )


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
    """Load project by public project id without ORM relationship loading."""
    query = Project.query.filter(Project.project_id == project_id)
    query = _query_without_relationships(query)

    if not include_deleted:
        query = query.filter(Project.deleted_at.is_(None))

    project = _safe_one_or_none(
        query,
        entity_name="Project",
        lookup={"projectId": project_id, "includeDeleted": include_deleted},
    )

    if project is None:
        raise LookupError(f"Project '{project_id}' was not found.")

    return project


def _get_default_universe_for_project(project: Project, *, include_deleted: bool = False) -> Universe:
    """Load default universe for project without ORM relationship loading."""
    universe_id = project.default_universe_id or _get_default_universe_id()

    query = Universe.query.filter(
        Universe.project_db_id == project.id,
        Universe.universe_id == universe_id,
    )
    query = _query_without_relationships(query)

    if not include_deleted:
        query = query.filter(Universe.deleted_at.is_(None))

    universe = _safe_one_or_none(
        query,
        entity_name="Universe",
        lookup={
            "projectDbId": project.id,
            "universeId": universe_id,
            "includeDeleted": include_deleted,
        },
    )

    if universe is None:
        fallback_query = Universe.query.filter(Universe.project_db_id == project.id)
        fallback_query = _query_without_relationships(fallback_query)

        if not include_deleted:
            fallback_query = fallback_query.filter(Universe.deleted_at.is_(None))

        try:
            universe = fallback_query.order_by(Universe.created_at.asc()).first()
        except Exception as exc:
            raise RuntimeError("Database lookup failed for default Universe fallback.") from exc

    if universe is None:
        raise LookupError(f"Project '{project.project_id}' has no universe.")

    return universe


def _get_universe_or_404(
    project: Project,
    universe_id: str | None = None,
    *,
    include_deleted: bool = False,
) -> Universe:
    """Load universe by id or project default without ORM relationship loading."""
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
    query = _query_without_relationships(query)

    if not include_deleted:
        query = query.filter(Universe.deleted_at.is_(None))

    universe = _safe_one_or_none(
        query,
        entity_name="Universe",
        lookup={
            "projectDbId": project.id,
            "universeId": effective_universe_id,
            "includeDeleted": include_deleted,
        },
    )

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
    """Load world by universe and world id without ORM relationship loading."""
    effective_world_id = _resolve_effective_world_id(
        world_id,
        allow_default_world=True,
    )

    query = WorldInstance.query.filter(
        WorldInstance.universe_db_id == universe.id,
        WorldInstance.world_id == effective_world_id,
    )
    query = _query_without_relationships(query)

    if not include_deleted:
        query = query.filter(WorldInstance.deleted_at.is_(None))

    world = _safe_one_or_none(
        query,
        entity_name="WorldInstance",
        lookup={
            "universeDbId": universe.id,
            "worldId": effective_world_id,
            "includeDeleted": include_deleted,
        },
    )

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

    _log_checkpoint(
        "before_project_lookup",
        projectId=effective_project_id,
        includeDeleted=include_deleted,
    )
    project = _get_project_or_404(
        effective_project_id,
        include_deleted=include_deleted,
    )

    _log_checkpoint(
        "before_universe_lookup",
        projectDbId=project.id,
        projectId=project.project_id,
        universeId=universe_id,
    )
    universe = _get_universe_or_404(
        project,
        universe_id,
        include_deleted=include_deleted,
    )

    _log_checkpoint(
        "before_world_lookup",
        universeDbId=universe.id,
        universeId=universe.universe_id,
        worldId=world_id,
    )
    world = _get_world_or_404(
        universe,
        world_id,
        include_deleted=include_deleted,
    )

    _log_checkpoint(
        "after_context_resolve",
        projectDbId=project.id,
        projectId=project.project_id,
        universeDbId=universe.id,
        universeId=universe.universe_id,
        worldDbId=world.id,
        worldId=world.world_id,
        providerWorldId=world.provider_world_id,
    )

    return project, universe, world


def _get_spawn_world_for_project(project: Project, *, include_deleted: bool = False) -> tuple[Universe, WorldInstance]:
    """Resolve project spawn/default world."""
    universe = _get_default_universe_for_project(project, include_deleted=include_deleted)
    world_id = universe.spawn_world_id or universe.default_world_id or _get_default_world_id()
    world = _get_world_or_404(universe, world_id, include_deleted=include_deleted)
    return universe, world


# -----------------------------------------------------------------------------
# Provider generation adapter
# -----------------------------------------------------------------------------

def _object_to_dict(value: Any) -> dict[str, Any]:
    """Best-effort object-to-dict conversion."""
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(value)

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        for kwargs in (
            {"camel_case": True},
            {"camelCase": True},
            {},
        ):
            try:
                result = to_dict(**kwargs)
                if isinstance(result, Mapping):
                    return dict(result)
            except TypeError:
                continue
            except Exception:
                break

    try:
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    except Exception:
        return {
            "repr": repr(value),
        }


def _try_generate_with_world_service(
    *,
    provider_world_id: str,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
) -> Any:
    """
    Generate provider chunk through src.world.service.

    The known current signature is:
        WorldService.generate_chunk(world_id, chunk_x, chunk_y, chunk_z)

    Fallbacks are deliberately limited and logged. Route code must not keep
    trying arbitrary signatures that can accidentally enter expensive provider
    paths.
    """
    try:
        service_module = importlib.import_module("src.world.service")
        get_service = getattr(service_module, "get_default_world_service", None)
        if not callable(get_service):
            raise RuntimeError("src.world.service.get_default_world_service is unavailable.")
        world_service = get_service()
    except Exception as exc:
        raise RuntimeError(f"Could not initialize world provider service: {_safe_exception_message(exc)}") from exc

    generate_chunk = getattr(world_service, "generate_chunk", None)
    if not callable(generate_chunk):
        raise RuntimeError("WorldService.generate_chunk is unavailable.")

    _log_checkpoint(
        "before_provider_generate",
        providerWorldId=provider_world_id,
        chunkX=chunk_x,
        chunkY=chunk_y,
        chunkZ=chunk_z,
    )

    attempts: list[dict[str, Any]] = []

    call_specs = (
        (
            "generate_chunk(world_id, chunk_x, chunk_y, chunk_z)",
            lambda: generate_chunk(
                provider_world_id,
                int(chunk_x),
                int(chunk_y),
                int(chunk_z),
            ),
        ),
        (
            "generate_chunk(world_id=..., chunk_x=..., chunk_y=..., chunk_z=...)",
            lambda: generate_chunk(
                world_id=provider_world_id,
                chunk_x=int(chunk_x),
                chunk_y=int(chunk_y),
                chunk_z=int(chunk_z),
            ),
        ),
    )

    for name, fn in call_specs:
        try:
            _log_checkpoint("provider_attempt_start", attempt=name)
            result = fn()
            _log_checkpoint(
                "provider_attempt_done",
                attempt=name,
                resultType=type(result).__name__,
            )
            return result
        except TypeError as exc:
            attempts.append(
                {
                    "attempt": name,
                    "errorType": type(exc).__name__,
                    "message": _safe_exception_message(exc),
                }
            )
            continue
        except Exception as exc:
            raise RuntimeError(
                f"Provider chunk generation failed in attempt '{name}': {_safe_exception_message(exc)}"
            ) from exc

    raise RuntimeError(
        "Provider chunk generation could not be called with supported signatures. "
        f"Attempts: {_make_json_safe(attempts, max_depth=8)}"
    )


def _extract_runtime_candidate(generated: Any) -> dict[str, Any]:
    """Extract runtime chunk candidate from provider generation result."""
    data = _object_to_dict(generated)

    for key in ("chunk", "runtimeContent", "runtime_content", "content"):
        value = data.get(key)
        if isinstance(value, Mapping):
            return dict(value)

    return data


def _extract_value(source: Mapping[str, Any], *keys: str, fallback: Any = None) -> Any:
    """Read first existing key from mapping."""
    for key in keys:
        if key in source:
            return source.get(key)
    return fallback


def _normalize_cells(value: Any) -> list[Any]:
    """Normalize chunk cell values into a JSON-safe list."""
    if value is None:
        return []

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _make_json_safe(list(value), max_depth=20)

    return []


def _normalize_palette(value: Any) -> list[Any]:
    """Normalize palette values into a JSON-safe list."""
    if value is None:
        return []

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _make_json_safe(list(value), max_depth=30)

    return []


def _runtime_content_from_generated(
    *,
    generated: Any,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
) -> dict[str, Any]:
    """Build project-scoped runtime content from provider-generated chunk."""
    wrapper = _object_to_dict(generated)
    candidate = _extract_runtime_candidate(generated)

    chunk_key = _build_chunk_key(chunk_x, chunk_y, chunk_z)

    raw_cells = _extract_value(candidate, "cells", "cellValues", "cell_values", fallback=None)
    if raw_cells is None:
        raw_cells = _extract_value(wrapper, "cells", "cellValues", "cell_values", fallback=[])

    cells = _normalize_cells(raw_cells)

    raw_palette = _extract_value(candidate, "palette", fallback=None)
    if raw_palette is None:
        raw_palette = _extract_value(wrapper, "palette", fallback=[])

    palette = _normalize_palette(raw_palette)

    cell_count = _extract_value(candidate, "cellCount", "cell_count", fallback=None)
    if cell_count is None:
        try:
            cell_count = len(cells)
        except Exception:
            cell_count = int(world.chunk_size or 16) ** 3

    source_world_id = (
        _extract_value(candidate, "providerSourceWorldId", "sourceWorldId", fallback=None)
        or _extract_value(wrapper, "worldId", "world_id", fallback=None)
        or world.provider_world_id
    )

    content_hash = _extract_value(candidate, "contentHash", "content_hash", fallback=None)
    if content_hash is None:
        content_hash = _extract_value(wrapper, "contentHash", "content_hash", fallback=None)

    stats = _extract_value(candidate, "stats", "generationStats", fallback=None)
    if stats is None:
        stats = _extract_value(wrapper, "stats", "generationStats", fallback=None)

    runtime: dict[str, Any] = {
        "projectId": project.project_id,
        "universeId": universe.universe_id,
        "worldId": world.world_id,
        "templateId": world.template_id,
        "providerId": world.provider_id,
        "providerWorldId": world.provider_world_id,
        "providerSourceWorldId": source_world_id,
        "chunkX": int(chunk_x),
        "chunkY": int(chunk_y),
        "chunkZ": int(chunk_z),
        "chunkKey": chunk_key,
        "source": "generated",
        "runtimeContentVersion": (
            _extract_value(candidate, "runtimeContentVersion", "runtime_content_version", fallback=None)
            or RUNTIME_CHUNK_CONTENT_VERSION
        ),
        "cellIndexOrder": (
            _extract_value(candidate, "cellIndexOrder", "cell_index_order", fallback=None)
            or CELL_INDEX_ORDER
        ),
        "airCellValue": AIR_CELL_VALUE,
        "cellEncoding": {
            "version": CELL_ENCODING_VERSION,
            "airCellValue": AIR_CELL_VALUE,
            "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
        },
        "palette": palette,
        "cells": cells,
        "cellCount": int(cell_count or 0),
        "blockRegistryId": world.block_registry_id,
        "blockRegistryVersion": world.block_registry_version,
        "coordinateSystem": world.coordinate_system,
        "projectionType": world.projection_type,
        "topologyType": world.topology_type,
        "chunkSize": world.chunk_size,
        "cellSize": world.cell_size,
    }

    if isinstance(stats, Mapping):
        runtime["stats"] = _make_json_safe(dict(stats), max_depth=20)

    if content_hash is not None:
        runtime["contentHash"] = _coerce_string(content_hash)

    return runtime


def _generate_runtime_chunk(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
) -> dict[str, Any]:
    """Generate runtime chunk through provider/template world layer."""
    generated = _try_generate_with_world_service(
        provider_world_id=world.provider_world_id,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
    )

    return _runtime_content_from_generated(
        generated=generated,
        project=project,
        universe=universe,
        world=world,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
    )


# -----------------------------------------------------------------------------
# Snapshot helpers
# -----------------------------------------------------------------------------

def _find_chunk_snapshot(
    *,
    world: WorldInstance,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    include_deleted: bool = False,
) -> Optional[ChunkSnapshot]:
    """Find active ChunkSnapshot for world/chunk coords without relationship loading."""
    query = ChunkSnapshot.query.filter(
        ChunkSnapshot.world_db_id == world.id,
        ChunkSnapshot.chunk_x == int(chunk_x),
        ChunkSnapshot.chunk_y == int(chunk_y),
        ChunkSnapshot.chunk_z == int(chunk_z),
    )
    query = _query_without_relationships(query)

    if not include_deleted:
        query = query.filter(ChunkSnapshot.deleted_at.is_(None))
        query = query.filter(ChunkSnapshot.status == "active")

    return _safe_one_or_none(
        query,
        entity_name="ChunkSnapshot",
        lookup={
            "worldDbId": world.id,
            "chunkX": chunk_x,
            "chunkY": chunk_y,
            "chunkZ": chunk_z,
            "includeDeleted": include_deleted,
        },
    )


def _runtime_content_from_snapshot(
    *,
    snapshot: ChunkSnapshot,
    project: Project,
    universe: Universe,
    world: WorldInstance,
) -> dict[str, Any]:
    """Build runtime chunk content from ChunkSnapshot."""
    runtime: dict[str, Any] = {}

    try:
        built = snapshot.build_runtime_content()
        if isinstance(built, Mapping):
            runtime = dict(built)
    except Exception as exc:
        _log_checkpoint(
            "snapshot_build_runtime_content_failed",
            snapshotId=getattr(snapshot, "snapshot_id", None),
            error=_safe_exception_message(exc),
        )

    if not runtime and isinstance(snapshot.content_json, Mapping):
        runtime = dict(snapshot.content_json)

    chunk_x = int(snapshot.chunk_x)
    chunk_y = int(snapshot.chunk_y)
    chunk_z = int(snapshot.chunk_z)
    chunk_key = snapshot.chunk_key or _build_chunk_key(chunk_x, chunk_y, chunk_z)

    cells = _extract_value(runtime, "cells", "cellValues", "cell_values", fallback=None)
    if cells is not None:
        runtime["cells"] = _normalize_cells(cells)

    runtime.update(
        {
            "projectId": project.project_id,
            "universeId": universe.universe_id,
            "worldId": world.world_id,
            "templateId": world.template_id,
            "providerId": world.provider_id,
            "providerWorldId": world.provider_world_id,
            "providerSourceWorldId": snapshot.provider_world_id or world.provider_world_id,
            "chunkX": chunk_x,
            "chunkY": chunk_y,
            "chunkZ": chunk_z,
            "chunkKey": chunk_key,
            "source": "snapshot",
            "snapshotId": snapshot.snapshot_id,
            "chunkRevision": snapshot.chunk_revision,
            "chunkVersion": snapshot.chunk_version,
            "runtimeContentVersion": snapshot.runtime_content_version or RUNTIME_CHUNK_CONTENT_VERSION,
            "cellIndexOrder": snapshot.cell_index_order or CELL_INDEX_ORDER,
            "airCellValue": AIR_CELL_VALUE,
            "cellEncoding": {
                "version": snapshot.cell_encoding_version or CELL_ENCODING_VERSION,
                "airCellValue": AIR_CELL_VALUE,
                "blockCellValueRule": snapshot.block_cell_value_rule or BLOCK_CELL_VALUE_RULE,
            },
            "palette": _normalize_palette(snapshot.palette_json or []),
            "objectRefs": _make_json_safe(snapshot.object_refs_json or [], max_depth=30),
            "cellCount": int(snapshot.cell_count or 0),
            "contentHash": snapshot.content_hash,
            "blockRegistryId": snapshot.block_registry_id,
            "blockRegistryVersion": snapshot.block_registry_version,
            "coordinateSystem": snapshot.coordinate_system,
            "projectionType": snapshot.projection_type,
            "topologyType": snapshot.topology_type,
            "chunkSize": snapshot.chunk_size,
            "cellSize": snapshot.cell_size,
        }
    )

    return _make_json_safe(runtime, max_depth=60)


def _materialize_generated_chunk(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    runtime_content: Mapping[str, Any],
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> ChunkSnapshot:
    """Persist a generated chunk as ChunkSnapshot."""
    snapshot = ChunkSnapshot.create_for_world(
        world,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
        content_json=dict(runtime_content),
        materialized_reason="manual",
        snapshot_source="materialized_generated",
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
        last_session_id=session_id,
        metadata_json={
            "materializedByRoute": ROUTE_SOURCE,
            "projectId": project.project_id,
            "universeId": universe.universe_id,
            "worldId": world.world_id,
        },
    )

    db.session.add(snapshot)
    db.session.flush()
    return snapshot


def _load_or_generate_chunk(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    include_deleted_snapshots: bool = False,
    prefer_snapshot: bool = True,
    allow_generated: bool = True,
    materialize_generated: bool = False,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Load ChunkSnapshot or generate provider chunk."""
    chunk_key = _build_chunk_key(chunk_x, chunk_y, chunk_z)

    _log_checkpoint(
        "load_or_generate_start",
        chunkKey=chunk_key,
        preferSnapshot=prefer_snapshot,
        allowGenerated=allow_generated,
        materializeGenerated=materialize_generated,
    )

    snapshot = None
    if prefer_snapshot:
        _log_checkpoint("before_snapshot_lookup", chunkKey=chunk_key)
        snapshot = _find_chunk_snapshot(
            world=world,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            include_deleted=include_deleted_snapshots,
        )
        _log_checkpoint(
            "after_snapshot_lookup",
            chunkKey=chunk_key,
            found=bool(snapshot is not None),
            snapshotId=getattr(snapshot, "snapshot_id", None),
        )

    if snapshot is not None:
        runtime = _runtime_content_from_snapshot(
            snapshot=snapshot,
            project=project,
            universe=universe,
            world=world,
        )
        return {
            "ok": True,
            "source": "snapshot",
            "chunkKey": chunk_key,
            "chunk": runtime,
            "snapshot": snapshot,
            "snapshotId": snapshot.snapshot_id,
            "chunkVersion": snapshot.chunk_version,
            "chunkRevision": snapshot.chunk_revision,
            "materialized": True,
            "generated": False,
            "createdSnapshot": False,
        }

    if not allow_generated:
        raise LookupError(f"Chunk '{chunk_key}' is not materialized and generated fallback is disabled.")

    _log_checkpoint("before_generate_runtime_chunk", chunkKey=chunk_key)
    runtime = _generate_runtime_chunk(
        project=project,
        universe=universe,
        world=world,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
    )
    _log_checkpoint(
        "after_generate_runtime_chunk",
        chunkKey=chunk_key,
        cellCount=runtime.get("cellCount"),
    )

    if materialize_generated:
        created_snapshot = _materialize_generated_chunk(
            project=project,
            universe=universe,
            world=world,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            runtime_content=runtime,
            user_id=user_id,
            session_id=session_id,
        )
        runtime = _runtime_content_from_snapshot(
            snapshot=created_snapshot,
            project=project,
            universe=universe,
            world=world,
        )
        return {
            "ok": True,
            "source": "snapshot",
            "chunkKey": chunk_key,
            "chunk": runtime,
            "snapshot": created_snapshot,
            "snapshotId": created_snapshot.snapshot_id,
            "chunkVersion": created_snapshot.chunk_version,
            "chunkRevision": created_snapshot.chunk_revision,
            "materialized": True,
            "generated": True,
            "createdSnapshot": True,
        }

    return {
        "ok": True,
        "source": "generated",
        "chunkKey": chunk_key,
        "chunk": runtime,
        "snapshot": None,
        "snapshotId": None,
        "chunkVersion": runtime.get("chunkVersion") or "generated",
        "chunkRevision": None,
        "materialized": False,
        "generated": True,
        "createdSnapshot": False,
    }


# -----------------------------------------------------------------------------
# Response serialization
# -----------------------------------------------------------------------------

def _serialize_snapshot_metadata(
    *,
    snapshot: ChunkSnapshot | None,
    project: Project,
    universe: Universe,
    world: WorldInstance,
) -> dict[str, Any] | None:
    """Serialize snapshot metadata shallowly."""
    if snapshot is None:
        return None

    try:
        return snapshot.to_dict(
            include_internal=False,
            include_content=False,
            project_id=project.project_id,
            universe_id=universe.universe_id,
            world_id=world.world_id,
        )
    except Exception:
        return {
            "snapshotId": getattr(snapshot, "snapshot_id", None),
            "chunkKey": getattr(snapshot, "chunk_key", None),
            "chunkVersion": getattr(snapshot, "chunk_version", None),
            "chunkRevision": getattr(snapshot, "chunk_revision", None),
        }


def _serialize_chunk_load_result(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    result: Mapping[str, Any],
    include_context: bool = False,
    include_snapshot_metadata: bool = True,
    include_route_hints: bool = True,
    api_prefix: str = "",
) -> dict[str, Any]:
    """Serialize one chunk load result."""
    chunk = _make_json_safe(result.get("chunk") or {}, max_depth=60)
    snapshot = result.get("snapshot")

    body: dict[str, Any] = {
        "projectId": project.project_id,
        "universeId": universe.universe_id,
        "worldId": world.world_id,
        "templateId": world.template_id,
        "providerId": world.provider_id,
        "providerWorldId": world.provider_world_id,
        "chunkKey": result.get("chunkKey"),
        "source": result.get("source"),
        "chunk": chunk,
        "flags": {
            "snapshotBacked": result.get("source") == "snapshot",
            "providerGenerated": bool(result.get("generated")),
            "materialized": bool(result.get("materialized")),
            "createdSnapshot": bool(result.get("createdSnapshot")),
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
            "blockRegistryId": world.block_registry_id,
            "blockRegistryVersion": world.block_registry_version,
            "chunkSize": world.chunk_size,
            "cellSize": world.cell_size,
            "coordinateSystem": world.coordinate_system,
            "projectionType": world.projection_type,
            "topologyType": world.topology_type,
        }

    if include_snapshot_metadata and snapshot is not None:
        body["snapshot"] = _serialize_snapshot_metadata(
            snapshot=snapshot,
            project=project,
            universe=universe,
            world=world,
        )

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
        "chunkKey": result.get("chunkKey"),
        "projectScoped": True,
        "dbBacked": True,
    }

    return body


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@chunks_bp.get("/projects/<project_id>/worlds/<world_id>/chunks")
def get_project_world_chunk(project_id: str, world_id: str):
    """
    Load one chunk for a concrete project world.

    Example:
        GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0
    """
    started_at = time.perf_counter()

    try:
        _log_checkpoint(
            "request_start",
            path=request.path,
            args=dict(request.args),
            projectRouteId=project_id,
            worldRouteId=world_id,
        )

        universe_id = _get_query_string("universeId", "universe_id", fallback="") or None
        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)

        chunk_x = _get_query_int("chunkX", "chunk_x", "x", field_name="chunkX")
        chunk_y = _get_query_int("chunkY", "chunk_y", "y", field_name="chunkY")
        chunk_z = _get_query_int("chunkZ", "chunk_z", "z", field_name="chunkZ")

        prefer_snapshot = _get_query_bool("preferSnapshot", "prefer_snapshot", fallback=True)
        allow_generated = _get_query_bool("allowGenerated", "allow_generated", fallback=True)
        materialize_generated = _get_query_bool(
            "materializeGenerated",
            "materialize_generated",
            "materialize",
            fallback=False,
        )

        include_context = _get_query_bool("includeContext", "include_context", fallback=False)
        include_snapshot_metadata = _get_query_bool(
            "includeSnapshot",
            "include_snapshot",
            "includeSnapshotMetadata",
            "include_snapshot_metadata",
            fallback=True,
        )
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=True)
        api_prefix = _get_query_string("apiPrefix", "api_prefix", fallback=_get_default_api_prefix())

        user_id = _get_query_string("userId", "user_id", fallback="") or None
        session_id = _get_query_string("sessionId", "session_id", fallback="") or None

        chunk_key = _build_chunk_key(chunk_x, chunk_y, chunk_z)

        _log_checkpoint(
            "request_parsed",
            chunkKey=chunk_key,
            preferSnapshot=prefer_snapshot,
            allowGenerated=allow_generated,
            materializeGenerated=materialize_generated,
        )

        project, universe, world = _resolve_project_world_context(
            project_id,
            world_id,
            universe_id=universe_id,
            include_deleted=include_deleted,
        )

        result = _load_or_generate_chunk(
            project=project,
            universe=universe,
            world=world,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            include_deleted_snapshots=include_deleted,
            prefer_snapshot=prefer_snapshot,
            allow_generated=allow_generated,
            materialize_generated=materialize_generated,
            user_id=user_id,
            session_id=session_id,
        )

        if materialize_generated and result.get("createdSnapshot"):
            db.session.commit()

        payload = _serialize_chunk_load_result(
            project=project,
            universe=universe,
            world=world,
            result=result,
            include_context=include_context,
            include_snapshot_metadata=include_snapshot_metadata,
            include_route_hints=include_route_hints,
            api_prefix=api_prefix,
        )

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)

        body = _ok_response(
            response_version=CHUNK_RESPONSE_VERSION,
            payload=payload,
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": project.project_id,
                "worldRouteId": world_id,
                "resolvedWorldId": world.world_id,
                "universeId": universe.universe_id,
                "templateId": world.template_id,
                "providerWorldId": world.provider_world_id,
                "chunkKey": result.get("chunkKey"),
                "preferSnapshot": prefer_snapshot,
                "allowGenerated": allow_generated,
                "materializeGenerated": materialize_generated,
                "source": result.get("source"),
                "snapshotBacked": result.get("source") == "snapshot",
                "providerGenerated": result.get("generated"),
                "createdSnapshot": result.get("createdSnapshot"),
                "dbBacked": True,
                "projectScoped": True,
                "elapsedMs": elapsed_ms,
            },
        )

        _log_checkpoint(
            "request_done",
            chunkKey=result.get("chunkKey"),
            source=result.get("source"),
            elapsedMs=elapsed_ms,
        )

        return _json_response(body, 200)

    except LookupError as exc:
        _safe_rollback()
        return _error_response(exc, code="chunk_not_found", status_code=404)

    except ValueError as exc:
        _safe_rollback()
        return _error_response(exc, code="invalid_chunk_request", status_code=400)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


@chunks_bp.post("/projects/<project_id>/worlds/<world_id>/chunks/batch")
def post_project_world_chunks_batch(project_id: str, world_id: str):
    """
    Load multiple chunks for a concrete project world.

    Example body:
        {
          "chunks": [
            { "chunkX": 0, "chunkY": 0, "chunkZ": 0 },
            { "chunkX": 1, "chunkY": 0, "chunkZ": 0 }
          ]
        }
    """
    started_at = time.perf_counter()

    try:
        universe_id = _get_query_string("universeId", "universe_id", fallback="") or None
        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        continue_on_error = _get_query_bool("continueOnError", "continue_on_error", fallback=False)

        prefer_snapshot = _get_query_bool("preferSnapshot", "prefer_snapshot", fallback=True)
        allow_generated = _get_query_bool("allowGenerated", "allow_generated", fallback=True)
        materialize_generated = _get_query_bool(
            "materializeGenerated",
            "materialize_generated",
            "materialize",
            fallback=False,
        )

        include_context = _get_query_bool("includeContext", "include_context", fallback=False)
        include_snapshot_metadata = _get_query_bool(
            "includeSnapshot",
            "include_snapshot",
            "includeSnapshotMetadata",
            "include_snapshot_metadata",
            fallback=False,
        )
        include_route_hints = _get_query_bool("includeRouteHints", "include_route_hints", fallback=False)
        api_prefix = _get_query_string("apiPrefix", "api_prefix", fallback=_get_default_api_prefix())

        user_id = _get_query_string("userId", "user_id", fallback="") or None
        session_id = _get_query_string("sessionId", "session_id", fallback="") or None

        chunk_items = _extract_batch_chunks_from_body_or_query()

        project, universe, world = _resolve_project_world_context(
            project_id,
            world_id,
            universe_id=universe_id,
            include_deleted=include_deleted,
        )

        chunks: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        requested: list[dict[str, Any]] = []
        created_snapshot_count = 0
        snapshot_count = 0
        generated_count = 0

        for item in chunk_items:
            requested.append(dict(item))

            try:
                result = _load_or_generate_chunk(
                    project=project,
                    universe=universe,
                    world=world,
                    chunk_x=item["chunkX"],
                    chunk_y=item["chunkY"],
                    chunk_z=item["chunkZ"],
                    include_deleted_snapshots=include_deleted,
                    prefer_snapshot=prefer_snapshot,
                    allow_generated=allow_generated,
                    materialize_generated=materialize_generated,
                    user_id=user_id,
                    session_id=session_id,
                )

                if result.get("source") == "snapshot":
                    snapshot_count += 1

                if result.get("generated"):
                    generated_count += 1

                if result.get("createdSnapshot"):
                    created_snapshot_count += 1

                chunks.append(
                    _serialize_chunk_load_result(
                        project=project,
                        universe=universe,
                        world=world,
                        result=result,
                        include_context=include_context,
                        include_snapshot_metadata=include_snapshot_metadata,
                        include_route_hints=include_route_hints,
                        api_prefix=api_prefix,
                    )
                )

            except Exception as exc:
                error_item = {
                    "chunk": item,
                    "chunkKey": item.get("chunkKey"),
                    "error": {
                        "code": "chunk_load_failed",
                        "message": _safe_exception_message(exc),
                    },
                }

                if _include_debug_errors():
                    error_item["error"]["debug"] = {
                        "type": type(exc).__name__,
                        "repr": repr(exc),
                    }

                errors.append(error_item)

                if not continue_on_error:
                    break

        if errors:
            _safe_rollback()
        elif materialize_generated and created_snapshot_count > 0:
            db.session.commit()

        ok = len(errors) == 0
        status_code = 200 if ok else 207 if continue_on_error else 400
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)

        body = _ok_response(
            response_version=CHUNK_BATCH_RESPONSE_VERSION,
            payload={
                "projectId": project.project_id,
                "universeId": universe.universe_id,
                "worldId": world.world_id,
                "templateId": world.template_id,
                "providerId": world.provider_id,
                "providerWorldId": world.provider_world_id,
                "requested": requested,
                "chunks": chunks,
                "errors": errors,
                "counts": {
                    "requested": len(requested),
                    "chunks": len(chunks),
                    "errors": len(errors),
                    "snapshots": snapshot_count,
                    "generated": generated_count,
                    "createdSnapshots": created_snapshot_count,
                },
                "flags": {
                    "ok": ok,
                    "partial": bool(errors and chunks),
                    "continueOnError": continue_on_error,
                    "preferSnapshot": prefer_snapshot,
                    "allowGenerated": allow_generated,
                    "materializeGenerated": materialize_generated,
                },
                "route": {
                    "source": ROUTE_SOURCE,
                    "projectId": project.project_id,
                    "universeId": universe.universe_id,
                    "worldId": world.world_id,
                    "templateId": world.template_id,
                    "providerWorldId": world.provider_world_id,
                    "projectScoped": True,
                    "dbBacked": True,
                },
            },
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": project.project_id,
                "worldRouteId": world_id,
                "resolvedWorldId": world.world_id,
                "universeId": universe.universe_id,
                "templateId": world.template_id,
                "providerWorldId": world.provider_world_id,
                "continueOnError": continue_on_error,
                "preferSnapshot": prefer_snapshot,
                "allowGenerated": allow_generated,
                "materializeGenerated": materialize_generated,
                "dbBacked": True,
                "projectScoped": True,
                "elapsedMs": elapsed_ms,
            },
        )

        if not ok:
            body["ok"] = False

        return _json_response(body, status_code)

    except ValueError as exc:
        _safe_rollback()
        return _error_response(exc, code="invalid_chunk_batch_request", status_code=400)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


# -----------------------------------------------------------------------------
# Development convenience routes
# -----------------------------------------------------------------------------

@chunks_bp.get("/projects/<project_id>/chunks")
def get_project_spawn_world_chunk(project_id: str):
    """
    Development convenience route.

    Loads one chunk from the project's spawn world.

    Productive editor code should prefer:
        GET /projects/<project_id>/worlds/<world_id>/chunks
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
        _universe, world = _get_spawn_world_for_project(project)

        return get_project_world_chunk(project.project_id, world.world_id)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


@chunks_bp.post("/projects/<project_id>/chunks/batch")
def post_project_spawn_world_chunks_batch(project_id: str):
    """
    Development convenience route.

    Loads chunk batch from the project's spawn world.

    Productive editor code should prefer:
        POST /projects/<project_id>/worlds/<world_id>/chunks/batch
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
        _universe, world = _get_spawn_world_for_project(project)

        return post_project_world_chunks_batch(project.project_id, world.world_id)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


@chunks_bp.get("/chunks")
def get_default_project_world_chunk():
    """
    Development convenience route.

    Loads one chunk from the configured default project/world unless projectId
    and worldId are supplied as query parameters.

    Productive editor code should use:
        GET /projects/<project_id>/worlds/<world_id>/chunks
    """
    try:
        project_id = _get_query_string("projectId", "project_id", fallback="default")
        world_id = _get_query_string("worldId", "world_id", fallback="spawn")

        return get_project_world_chunk(project_id, world_id)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


@chunks_bp.post("/chunks/batch")
def post_default_project_world_chunks_batch():
    """
    Development convenience route.

    Loads chunks from the configured default project/world unless projectId and
    worldId are supplied as query parameters.

    Productive editor code should use:
        POST /projects/<project_id>/worlds/<world_id>/chunks/batch
    """
    try:
        project_id = _get_query_string("projectId", "project_id", fallback="default")
        world_id = _get_query_string("worldId", "world_id", fallback="spawn")

        return post_project_world_chunks_batch(project_id, world_id)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


# -----------------------------------------------------------------------------
# Status
# -----------------------------------------------------------------------------

@chunks_bp.get("/chunks/_status")
def get_chunks_route_status():
    """
    Return diagnostics for chunk routes, database and model registration.

    This route must remain shallow and cheap.
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
                    "chunkSnapshots": ChunkSnapshot.query.count(),
                    "activeChunkSnapshots": ChunkSnapshot.query.filter(ChunkSnapshot.deleted_at.is_(None)).count(),
                    "snapshotsWithObjectRefs": ChunkSnapshot.query.filter(ChunkSnapshot.has_object_refs.is_(True)).count(),
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
                "maxBatchChunks": _get_batch_max_count(),
                "cellEncoding": {
                    "version": CELL_ENCODING_VERSION,
                    "airCellValue": AIR_CELL_VALUE,
                    "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
                },
                "databaseUriConfigured": bool(_get_config_value("SQLALCHEMY_DATABASE_URI")),
                "relationshipLoadingDisabledInReadPath": bool(noload is not None),
            }

        body = _ok_response(
            response_version=CHUNKS_STATUS_RESPONSE_VERSION,
            payload={
                "route": {
                    "source": ROUTE_SOURCE,
                    "moduleVersion": ROUTE_MODULE_VERSION,
                    "blueprint": chunks_bp.name,
                    "dbBacked": True,
                    "snapshotBacked": True,
                    "generatedFallback": True,
                    "eventReplayLoadPath": False,
                    "relationshipLoadingDisabledInReadPath": bool(noload is not None),
                    "maxBatchChunks": _get_batch_max_count(),
                    "productiveRoutes": [
                        "GET /projects/<project_id>/worlds/<world_id>/chunks",
                        "POST /projects/<project_id>/worlds/<world_id>/chunks/batch",
                    ],
                    "devConvenienceRoutes": [
                        "GET /projects/<project_id>/chunks",
                        "POST /projects/<project_id>/chunks/batch",
                        "GET /chunks",
                        "POST /chunks/batch",
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
        _safe_rollback()
        return _error_response(exc)


__all__ = (
    "chunks_bp",
    "ROUTE_MODULE_VERSION",
    "ROUTE_SOURCE",
)