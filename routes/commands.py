# services/vectoplan-chunk/routes/commands.py
"""
Command routes for the VECTOPLAN chunk service.

This module is the first productive write path for the editable chunk world.

Primary route:

    POST /projects/<project_id>/worlds/<world_id>/commands

Supported command types in this slice:
- SetBlock
- RemoveBlock
- ReplaceBlock
- PlaceObject
- RemoveObject

Important persistence rules:
- ChunkSnapshot is the current load-truth for materialized chunks.
- ChunkEvent is append-only historical truth.
- WorldCommandLog groups one user/system command.
- One command can create one or many ChunkEvents.
- Events are not replayed on normal chunk load.
- Generated chunks are materialized only when changed by a command.
- Multi-block objects are prepared through WorldObjectInstance and
  WorldObjectChunkRef.

Current write flow:

    Editor sends command
    -> resolve Project / Universe / WorldInstance
    -> create WorldCommandLog
    -> load ChunkSnapshot or generate provider chunk
    -> apply cell/object changes
    -> create/update ChunkSnapshot
    -> write ChunkEvent(s)
    -> commit transaction
    -> return dirtyChunks for editor reload

Robustness notes:
- The command route disables ORM relationship loading in read-path lookups via
  noload("*"). A command must not accidentally load Project -> Universe -> World
  -> Snapshots -> Events -> Objects relationship graphs.
- The route serializes responses from explicit fields only.
- JSON-safe conversion is recursion-safe.
- SetBlock / RemoveBlock / ReplaceBlock are the primary stable command slice.
- PlaceObject / RemoveObject are supported defensively, but still depend on the
  current object model contract.
"""

from __future__ import annotations

import importlib
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any, Optional

from flask import Blueprint, current_app, jsonify, request

try:
    from sqlalchemy.orm import noload
except Exception:  # pragma: no cover
    noload = None  # type: ignore[assignment]

from extensions import db, get_database_status
from models import (
    BlockRegistry,
    BlockType,
    ChunkEvent,
    ChunkSnapshot,
    Project,
    Universe,
    WorldCommandLog,
    WorldInstance,
    WorldObjectChunkRef,
    WorldObjectInstance,
    get_model_debug_summary,
)


commands_bp = Blueprint("commands", __name__)

ROUTE_MODULE_VERSION = "0.2.0"
ROUTE_SOURCE = "routes.commands"

COMMAND_RESPONSE_VERSION = "world-command-response.v1"
COMMAND_STATUS_RESPONSE_VERSION = "commands-route-status-response.v1"

RUNTIME_CHUNK_CONTENT_VERSION = "runtime-chunk-content.v1"
CELL_ENCODING_VERSION = "cell-encoding.palette-index-plus-one.v1"
CELL_INDEX_ORDER = "x-fastest-y-then-z"
AIR_CELL_VALUE = 0
BLOCK_CELL_VALUE_RULE = "paletteIndex + 1"

DEFAULT_MAX_COMMAND_AFFECTED_CELLS = 65536
DEFAULT_JSON_SAFE_MAX_DEPTH = 80

ENV_ROUTE_INCLUDE_DEBUG_ERRORS = "VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS"
ENV_ROUTE_DEBUG_CHECKPOINTS = "VECTOPLAN_CHUNK_ROUTE_DEBUG_CHECKPOINTS"
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

_COMMAND_TYPE_ALIASES = {
    "setblock": "SetBlock",
    "set_block": "SetBlock",
    "set-block": "SetBlock",
    "SetBlock": "SetBlock",

    "removeblock": "RemoveBlock",
    "remove_block": "RemoveBlock",
    "remove-block": "RemoveBlock",
    "breakblock": "RemoveBlock",
    "break_block": "RemoveBlock",
    "break-block": "RemoveBlock",
    "RemoveBlock": "RemoveBlock",

    "replaceblock": "ReplaceBlock",
    "replace_block": "ReplaceBlock",
    "replace-block": "ReplaceBlock",
    "ReplaceBlock": "ReplaceBlock",

    "placeobject": "PlaceObject",
    "place_object": "PlaceObject",
    "place-object": "PlaceObject",
    "PlaceObject": "PlaceObject",

    "removeobject": "RemoveObject",
    "remove_object": "RemoveObject",
    "remove-object": "RemoveObject",
    "RemoveObject": "RemoveObject",
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


def _coerce_positive_int(
    value: Any,
    *,
    fallback: int | None = None,
    field_name: str = "value",
    maximum: int | None = None,
) -> int:
    """Coerce positive integer with optional maximum."""
    result = _coerce_int(value, fallback=fallback, field_name=field_name)

    if result <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    if maximum is not None and result > maximum:
        raise ValueError(f"{field_name} must be less than or equal to {maximum}.")

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


def _make_json_safe(
    value: Any,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
    max_depth: int = DEFAULT_JSON_SAFE_MAX_DEPTH,
) -> Any:
    """
    Convert values into JSON-safe structures.

    This helper is recursion-safe because route responses must never hang on
    ORM/provider objects that contain circular references.
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


def _get_config_int(name: str, fallback: int = 0) -> int:
    """Read int config defensively."""
    try:
        return int(_get_config_value(name, fallback))
    except Exception:
        return int(fallback)


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
    """Return whether route debug checkpoints should be logged."""
    query_debug = _get_query_bool(
        "debugRoute",
        "debug_route",
        "commandDebug",
        "command_debug",
        fallback=False,
    )
    env_debug = _get_env_bool(ENV_ROUTE_DEBUG_CHECKPOINTS, fallback=False)
    return bool(query_debug or env_debug)


def _log_checkpoint(label: str, **fields: Any) -> None:
    """Write optional shallow debug checkpoint."""
    if not _debug_checkpoints_enabled():
        return

    try:
        current_app.logger.warning(
            "COMMAND_ROUTE_DEBUG %s %s",
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
    return jsonify(_make_json_safe(dict(body), max_depth=80)), int(status_code)


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
        body.update(_make_json_safe(dict(payload), max_depth=70))

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


def _query_without_relationships(query: Any) -> Any:
    """
    Disable ORM relationship loading for command-route lookups.

    This prevents command execution from loading large object graphs such as:
    Project -> Universes -> Worlds -> Snapshots -> Events -> Objects.
    """
    if noload is None:
        return query

    try:
        return query.options(noload("*"))
    except Exception:
        return query


def _safe_one_or_none(query: Any, *, entity_name: str, lookup: Mapping[str, Any]) -> Any | None:
    """
    Execute a query safely and return zero or one row.

    Uses LIMIT 2 to catch duplicate rows without depending on one_or_none()
    behavior.
    """
    try:
        rows = query.limit(2).all()
    except Exception as exc:
        raise RuntimeError(f"Database lookup failed for {entity_name}.") from exc

    if len(rows) > 1:
        raise RuntimeError(
            f"Database lookup for {entity_name} returned multiple rows: "
            f"{_make_json_safe(dict(lookup), max_depth=6)}"
        )

    return rows[0] if rows else None


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


def _get_max_command_affected_cells() -> int:
    return max(
        1,
        _get_config_int(
            "VECTOPLAN_CHUNK_MAX_COMMAND_AFFECTED_CELLS",
            DEFAULT_MAX_COMMAND_AFFECTED_CELLS,
        ),
    )


def _get_max_object_size_x() -> int:
    return max(1, _get_config_int("VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_X", 256))


def _get_max_object_size_y() -> int:
    return max(1, _get_config_int("VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_Y", 256))


def _get_max_object_size_z() -> int:
    return max(1, _get_config_int("VECTOPLAN_CHUNK_MAX_OBJECT_SIZE_Z", 256))


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
    """Load project by public project id without relationship loading."""
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
    """Load default universe for project without relationship loading."""
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
    """Load universe by id or project default without relationship loading."""
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
    """Load world by universe and world id without relationship loading."""
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
# Command payload helpers
# -----------------------------------------------------------------------------

def _normalize_command_type(value: Any) -> str:
    """Normalize command type aliases into canonical command type."""
    raw = _coerce_string(value)

    if not raw:
        raise ValueError("Command type is required.")

    key = raw.replace(" ", "").strip()
    canonical = _COMMAND_TYPE_ALIASES.get(key) or _COMMAND_TYPE_ALIASES.get(key.lower())

    if canonical is None:
        supported = sorted(set(_COMMAND_TYPE_ALIASES.values()))
        raise ValueError(f"Unsupported command type '{raw}'. Supported: {', '.join(supported)}.")

    return canonical


def _get_payload_position(payload: Mapping[str, Any], *, required: bool = True) -> Optional[dict[str, int]]:
    """Extract world position from command payload."""
    position = payload.get("position")

    if position is None:
        position = payload.get("targetPosition") or payload.get("target_position")

    if position is None:
        if not required:
            return None
        raise ValueError("Command requires position: {x, y, z}.")

    if not isinstance(position, Mapping):
        raise ValueError("position must be a JSON object.")

    return {
        "x": _coerce_int(position.get("x"), field_name="position.x"),
        "y": _coerce_int(position.get("y"), field_name="position.y"),
        "z": _coerce_int(position.get("z"), field_name="position.z"),
    }


def _get_payload_user_id(payload: Mapping[str, Any]) -> Optional[str]:
    """Extract user id from payload."""
    value = payload.get("userId") or payload.get("user_id")
    text = _coerce_string(value)
    return text or None


def _get_payload_session_id(payload: Mapping[str, Any]) -> Optional[str]:
    """Extract session id from payload."""
    value = payload.get("sessionId") or payload.get("session_id")
    text = _coerce_string(value)
    return text or None


def _get_payload_block_type_id(payload: Mapping[str, Any], *, required: bool = True) -> Optional[str]:
    """Extract block type id from payload."""
    value = (
        payload.get("blockTypeId")
        or payload.get("block_type_id")
        or payload.get("afterBlockTypeId")
        or payload.get("after_block_type_id")
        or payload.get("fillBlockTypeId")
        or payload.get("fill_block_type_id")
    )
    text = _coerce_string(value)

    if not text and required:
        raise ValueError("blockTypeId is required.")

    return text or None


def _get_payload_target_face(payload: Mapping[str, Any]) -> Optional[str]:
    """Extract target face."""
    value = payload.get("targetFace") or payload.get("target_face")
    text = _coerce_string(value)
    return text or None


def _get_payload_tool(payload: Mapping[str, Any]) -> Optional[str]:
    """Extract tool."""
    value = payload.get("tool")
    text = _coerce_string(value)
    return text or None


# -----------------------------------------------------------------------------
# Coordinate and chunk cell helpers
# -----------------------------------------------------------------------------

def _floor_div(value: int, divisor: int) -> int:
    """Floor division for world-to-chunk coordinates."""
    return int(value) // int(divisor)


def _world_to_chunk_local(value: int, chunk_size: int) -> tuple[int, int]:
    """Convert one world coordinate to chunk coordinate and local coordinate."""
    chunk = _floor_div(value, chunk_size)
    local = int(value) - chunk * int(chunk_size)
    return chunk, local


def _build_chunk_key(chunk_x: int, chunk_y: int, chunk_z: int) -> str:
    """Build canonical chunk key."""
    return f"{int(chunk_x)}:{int(chunk_y)}:{int(chunk_z)}"


def _world_position_to_chunk_cell(position: Mapping[str, int], chunk_size: int) -> dict[str, int]:
    """Convert world position to chunk/local cell data."""
    chunk_x, local_x = _world_to_chunk_local(int(position["x"]), chunk_size)
    chunk_y, local_y = _world_to_chunk_local(int(position["y"]), chunk_size)
    chunk_z, local_z = _world_to_chunk_local(int(position["z"]), chunk_size)

    return {
        "worldX": int(position["x"]),
        "worldY": int(position["y"]),
        "worldZ": int(position["z"]),
        "chunkX": chunk_x,
        "chunkY": chunk_y,
        "chunkZ": chunk_z,
        "localX": local_x,
        "localY": local_y,
        "localZ": local_z,
        "chunkKey": _build_chunk_key(chunk_x, chunk_y, chunk_z),
    }


def _flatten_cell_index(local_x: int, local_y: int, local_z: int, chunk_size: int) -> int:
    """Flatten local cell coordinates using x-fastest-y-then-z order."""
    return int(local_x) + int(chunk_size) * (int(local_y) + int(chunk_size) * int(local_z))


def _normalize_cells(value: Any) -> list[int]:
    """Normalize cells into integer list."""
    if value is None:
        return []

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []

    normalized: list[int] = []
    for item in value:
        try:
            normalized.append(int(item))
        except Exception:
            normalized.append(AIR_CELL_VALUE)

    return normalized


def _normalize_palette(value: Any) -> list[dict[str, Any]]:
    """Normalize palette into list of dicts."""
    if value is None:
        return []

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []

    normalized: list[dict[str, Any]] = []
    for entry in value:
        if isinstance(entry, Mapping):
            normalized.append(dict(entry))
        elif isinstance(entry, str):
            normalized.append({"blockTypeId": entry})
        else:
            normalized.append({"raw": _make_json_safe(entry, max_depth=8)})

    return normalized


def _ensure_cells(content: dict[str, Any], *, chunk_size: int) -> list[int]:
    """Ensure runtime content has a full mutable cells list."""
    expected_count = int(chunk_size) ** 3
    cells = _normalize_cells(content.get("cells"))

    if len(cells) < expected_count:
        cells.extend([AIR_CELL_VALUE] * (expected_count - len(cells)))
    elif len(cells) > expected_count:
        cells = cells[:expected_count]

    content["cells"] = cells
    content["cellCount"] = expected_count
    return cells


def _ensure_palette(content: dict[str, Any]) -> list[dict[str, Any]]:
    """Ensure runtime content has a mutable palette list."""
    palette = _normalize_palette(content.get("palette"))
    content["palette"] = palette
    return palette


def _get_cell_value(content: dict[str, Any], *, local_x: int, local_y: int, local_z: int, chunk_size: int) -> int:
    """Read cell value from runtime content."""
    cells = _ensure_cells(content, chunk_size=chunk_size)
    index = _flatten_cell_index(local_x, local_y, local_z, chunk_size)
    return int(cells[index])


def _set_cell_value(
    content: dict[str, Any],
    *,
    local_x: int,
    local_y: int,
    local_z: int,
    chunk_size: int,
    cell_value: int,
) -> None:
    """Write cell value into runtime content."""
    cells = _ensure_cells(content, chunk_size=chunk_size)
    index = _flatten_cell_index(local_x, local_y, local_z, chunk_size)
    cells[index] = int(cell_value)


def _block_type_id_from_cell_value(content: dict[str, Any], cell_value: int) -> Optional[str]:
    """Resolve block type id from cell value and chunk palette."""
    value = int(cell_value)

    if value == AIR_CELL_VALUE:
        return None

    palette_index = value - 1
    palette = _ensure_palette(content)

    if palette_index < 0 or palette_index >= len(palette):
        return None

    entry = palette[palette_index]

    return (
        entry.get("blockTypeId")
        or entry.get("block_type_id")
        or entry.get("typeId")
        or entry.get("type_id")
    )


def _get_block_attr(block_type: BlockType, name: str, fallback: Any = None) -> Any:
    """Read optional BlockType attribute defensively."""
    try:
        return getattr(block_type, name)
    except Exception:
        return fallback


def _cell_value_for_block_type(content: dict[str, Any], block_type: BlockType) -> int:
    """
    Return or create cell value for a BlockType in the runtime content palette.
    """
    palette = _ensure_palette(content)
    block_type_id = _coerce_string(getattr(block_type, "block_type_id", ""))

    for index, entry in enumerate(palette):
        if not isinstance(entry, Mapping):
            continue

        if entry.get("blockTypeId") == block_type_id or entry.get("block_type_id") == block_type_id:
            return index + 1

    new_index = len(palette)
    palette.append(
        {
            "paletteIndex": new_index,
            "cellValue": new_index + 1,
            "blockTypeId": block_type_id,
            "label": _get_block_attr(block_type, "label", block_type_id),
            "registryId": _get_block_attr(block_type, "registry_id", None),
            "registryVersion": _get_block_attr(block_type, "registry_version", None),
            "solid": _get_block_attr(block_type, "solid", True),
            "opaque": _get_block_attr(block_type, "opaque", True),
            "placeable": _get_block_attr(block_type, "placeable", True),
            "breakable": _get_block_attr(block_type, "breakable", True),
            "selectable": _get_block_attr(block_type, "selectable", True),
            "collidable": _get_block_attr(block_type, "collidable", True),
            "renderMode": _get_block_attr(block_type, "render_mode", None),
            "shapeType": _get_block_attr(block_type, "shape_type", None),
            "materialId": _get_block_attr(block_type, "material_id", None),
            "textureId": _get_block_attr(block_type, "texture_id", None),
            "iconId": _get_block_attr(block_type, "icon_id", None),
        }
    )

    return new_index + 1


def _update_content_stats(content: dict[str, Any], *, chunk_size: int) -> None:
    """Update basic stats in runtime content."""
    cells = _ensure_cells(content, chunk_size=chunk_size)
    non_air = sum(1 for value in cells if int(value) != AIR_CELL_VALUE)

    stats = content.get("stats")
    if not isinstance(stats, dict):
        stats = {}

    stats.update(
        {
            "cellCount": len(cells),
            "airCellCount": len(cells) - non_air,
            "nonAirCellCount": non_air,
        }
    )

    content["stats"] = stats
    content["cellCount"] = len(cells)


def _dirty_chunk_keys_for_cell(cell: Mapping[str, int], chunk_size: int) -> list[str]:
    """
    Compute dirty chunks for a changed cell.

    Includes target chunk and all neighbor chunks touching the local boundary.
    """
    chunk_x = int(cell["chunkX"])
    chunk_y = int(cell["chunkY"])
    chunk_z = int(cell["chunkZ"])
    local_x = int(cell["localX"])
    local_y = int(cell["localY"])
    local_z = int(cell["localZ"])

    dx_values = [0]
    dy_values = [0]
    dz_values = [0]

    if local_x == 0:
        dx_values.append(-1)
    if local_x == chunk_size - 1:
        dx_values.append(1)

    if local_y == 0:
        dy_values.append(-1)
    if local_y == chunk_size - 1:
        dy_values.append(1)

    if local_z == 0:
        dz_values.append(-1)
    if local_z == chunk_size - 1:
        dz_values.append(1)

    keys: set[str] = set()

    for dx in dx_values:
        for dy in dy_values:
            for dz in dz_values:
                keys.add(_build_chunk_key(chunk_x + dx, chunk_y + dy, chunk_z + dz))

    return sorted(keys)


def _add_object_ref_to_content(content: dict[str, Any], object_ref: Mapping[str, Any]) -> None:
    """Add or replace object reference in runtime content."""
    refs = content.get("objectRefs")
    if not isinstance(refs, list):
        refs = []

    object_instance_id = object_ref.get("objectInstanceId")
    filtered = [
        ref
        for ref in refs
        if not isinstance(ref, Mapping) or ref.get("objectInstanceId") != object_instance_id
    ]
    filtered.append(_make_json_safe(dict(object_ref), max_depth=20))
    content["objectRefs"] = filtered


def _remove_object_ref_from_content(content: dict[str, Any], object_instance_id: str) -> None:
    """Remove object reference from runtime content."""
    refs = content.get("objectRefs")
    if not isinstance(refs, list):
        content["objectRefs"] = []
        return

    content["objectRefs"] = [
        ref
        for ref in refs
        if not isinstance(ref, Mapping) or ref.get("objectInstanceId") != object_instance_id
    ]


# -----------------------------------------------------------------------------
# Registry / block validation
# -----------------------------------------------------------------------------

def _get_registry_for_world(world: WorldInstance) -> BlockRegistry:
    """Load BlockRegistry for a world without relationship loading."""
    query = BlockRegistry.query.filter(
        BlockRegistry.registry_id == world.block_registry_id,
        BlockRegistry.registry_version == world.block_registry_version,
    )
    query = _query_without_relationships(query)

    registry = _safe_one_or_none(
        query,
        entity_name="BlockRegistry",
        lookup={
            "registryId": world.block_registry_id,
            "registryVersion": world.block_registry_version,
        },
    )

    if registry is None:
        raise LookupError(
            f"Block registry '{world.block_registry_id}@{world.block_registry_version}' was not found."
        )

    return registry


def _get_block_type(
    *,
    world: WorldInstance,
    block_type_id: str,
    require_placeable: bool = False,
    require_breakable: bool = False,
) -> BlockType:
    """Load and validate BlockType."""
    registry = _get_registry_for_world(world)

    query = BlockType.query.filter(
        BlockType.registry_db_id == registry.id,
        BlockType.block_type_id == block_type_id,
    )
    query = _query_without_relationships(query)

    block = _safe_one_or_none(
        query,
        entity_name="BlockType",
        lookup={
            "registryDbId": registry.id,
            "blockTypeId": block_type_id,
        },
    )

    if block is None:
        raise LookupError(f"Block type '{block_type_id}' is not registered.")

    if bool(getattr(block, "is_deleted", False)):
        raise LookupError(f"Block type '{block_type_id}' is deleted.")

    if not bool(getattr(block, "is_active", True)):
        raise ValueError(f"Block type '{block_type_id}' is not active.")

    if require_placeable and not bool(getattr(block, "placeable", False)):
        raise ValueError(f"Block type '{block_type_id}' is not placeable.")

    if require_breakable and not bool(getattr(block, "breakable", False)):
        raise ValueError(f"Block type '{block_type_id}' is not breakable.")

    return block


def _validate_breakable_before_cell(
    *,
    world: WorldInstance,
    content: dict[str, Any],
    before_cell_value: int,
) -> None:
    """Validate breakable flag for non-air before-cell."""
    before_block_type_id = _block_type_id_from_cell_value(content, before_cell_value)

    if before_block_type_id is None:
        return

    _get_block_type(
        world=world,
        block_type_id=before_block_type_id,
        require_breakable=True,
    )


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


def _extract_value(source: Mapping[str, Any], *keys: str, fallback: Any = None) -> Any:
    """Read first existing key from mapping."""
    for key in keys:
        if key in source:
            return source.get(key)
    return fallback


def _extract_runtime_candidate(generated: Any) -> dict[str, Any]:
    """Extract runtime chunk candidate from provider generation result."""
    data = _object_to_dict(generated)

    for key in ("chunk", "runtimeContent", "runtime_content", "content"):
        value = data.get(key)
        if isinstance(value, Mapping):
            return dict(value)

    return data


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

    Fallbacks are deliberately limited. The route must not keep trying arbitrary
    signatures that can accidentally enter expensive provider paths.
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

    raw_palette = _extract_value(candidate, "palette", fallback=None)
    if raw_palette is None:
        raw_palette = _extract_value(wrapper, "palette", fallback=[])

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
        "providerSourceWorldId": (
            _extract_value(candidate, "providerSourceWorldId", "sourceWorldId", fallback=None)
            or _extract_value(wrapper, "worldId", "world_id", fallback=None)
            or world.provider_world_id
        ),
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
        "palette": _normalize_palette(raw_palette),
        "cells": _normalize_cells(raw_cells),
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

    _ensure_cells(runtime, chunk_size=int(world.chunk_size or 16))
    _ensure_palette(runtime)
    _update_content_stats(runtime, chunk_size=int(world.chunk_size or 16))

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
) -> Optional[ChunkSnapshot]:
    """Find active ChunkSnapshot for world/chunk coords without relationship loading."""
    query = ChunkSnapshot.query.filter(
        ChunkSnapshot.world_db_id == world.id,
        ChunkSnapshot.chunk_x == int(chunk_x),
        ChunkSnapshot.chunk_y == int(chunk_y),
        ChunkSnapshot.chunk_z == int(chunk_z),
        ChunkSnapshot.deleted_at.is_(None),
        ChunkSnapshot.status == "active",
    )
    query = _query_without_relationships(query)

    return _safe_one_or_none(
        query,
        entity_name="ChunkSnapshot",
        lookup={
            "worldDbId": world.id,
            "chunkX": chunk_x,
            "chunkY": chunk_y,
            "chunkZ": chunk_z,
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

    if "cells" in runtime:
        runtime["cells"] = _normalize_cells(runtime.get("cells"))

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

    _ensure_cells(runtime, chunk_size=int(world.chunk_size or 16))
    _ensure_palette(runtime)
    _update_content_stats(runtime, chunk_size=int(world.chunk_size or 16))

    return _make_json_safe(runtime, max_depth=60)


def _load_chunk_for_mutation(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
) -> tuple[Optional[ChunkSnapshot], dict[str, Any]]:
    """Load active snapshot or generate base chunk for mutation."""
    snapshot = _find_chunk_snapshot(
        world=world,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
    )

    if snapshot is not None:
        return snapshot, _runtime_content_from_snapshot(
            snapshot=snapshot,
            project=project,
            universe=universe,
            world=world,
        )

    return None, _generate_runtime_chunk(
        project=project,
        universe=universe,
        world=world,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
    )


def _save_snapshot_after_mutation(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    existing_snapshot: Optional[ChunkSnapshot],
    content: dict[str, Any],
    materialized_reason: str,
    command_log: WorldCommandLog,
    user_id: Optional[str],
    session_id: Optional[str],
) -> ChunkSnapshot:
    """Create or update ChunkSnapshot after content mutation."""
    chunk_x = int(content["chunkX"])
    chunk_y = int(content["chunkY"])
    chunk_z = int(content["chunkZ"])

    _update_content_stats(content, chunk_size=int(world.chunk_size or 16))

    if existing_snapshot is None:
        snapshot = ChunkSnapshot.create_for_world(
            world,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            content_json=content,
            materialized_reason=materialized_reason,
            snapshot_source="command",
            last_command_id=command_log.command_id,
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

    existing_snapshot.replace_content(
        content_json=content,
        materialized_reason=materialized_reason,
        snapshot_source="command",
        last_command_id=command_log.command_id,
        updated_by_user_id=user_id,
        last_session_id=session_id,
        bump_revision=True,
    )
    db.session.add(existing_snapshot)
    db.session.flush()
    return existing_snapshot


def _attach_event_to_snapshot(snapshot: ChunkSnapshot, event: ChunkEvent, *, user_id: Optional[str], session_id: Optional[str]) -> None:
    """Attach event context to snapshot after event creation."""
    try:
        snapshot.update_command_context(
            last_event_id=event.event_id,
            updated_by_user_id=user_id,
            last_session_id=session_id,
        )
    except Exception:
        try:
            snapshot.last_event_id = event.event_id
            snapshot.updated_by_user_id = user_id
            snapshot.last_session_id = session_id
        except Exception:
            pass

    db.session.add(snapshot)
    db.session.flush()


# -----------------------------------------------------------------------------
# Command execution helpers
# -----------------------------------------------------------------------------

def _safe_model_to_dict(model: Any, *, fallback: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Call model.to_dict defensively."""
    if model is None:
        return dict(fallback or {})

    to_dict = getattr(model, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict(**kwargs)
            if isinstance(result, Mapping):
                return _make_json_safe(dict(result), max_depth=40)
        except TypeError:
            try:
                result = to_dict()
                if isinstance(result, Mapping):
                    return _make_json_safe(dict(result), max_depth=40)
            except Exception:
                pass
        except Exception:
            pass

    return dict(fallback or {})


def _mark_command_applied(command_log: WorldCommandLog, **kwargs: Any) -> None:
    """Mark command as applied with fallback fields."""
    mark_applied = getattr(command_log, "mark_applied", None)

    if callable(mark_applied):
        mark_applied(**kwargs)
        return

    try:
        command_log.command_status = "applied"
        command_log.changed = bool(kwargs.get("changed"))
        command_log.affected_chunk_count = len(kwargs.get("affected_chunks_json") or [])
        command_log.affected_cell_count = len(kwargs.get("affected_cells_json") or [])
        command_log.event_count = int(kwargs.get("event_count") or 0)
        command_log.affected_chunks_json = list(kwargs.get("affected_chunks_json") or [])
        command_log.affected_cells_json = list(kwargs.get("affected_cells_json") or [])
        command_log.result_payload_json = dict(kwargs.get("result_payload_json") or {})
    except Exception:
        pass


def _create_command_log(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    payload: Mapping[str, Any],
    command_type: str,
    user_id: Optional[str],
    session_id: Optional[str],
) -> WorldCommandLog:
    """Create and flush WorldCommandLog."""
    position = _get_payload_position(payload, required=False) or {}

    command_log = WorldCommandLog.create(
        project_db_id=project.id,
        universe_db_id=universe.id,
        world_db_id=world.id,
        command_type=command_type,
        command_id=payload.get("commandId") or payload.get("command_id"),
        command_status="received",
        command_source=payload.get("commandSource") or payload.get("command_source") or "editor",
        user_id=user_id,
        session_id=session_id,
        request_id=payload.get("requestId") or payload.get("request_id"),
        trace_id=payload.get("traceId") or payload.get("trace_id"),
        client_id=payload.get("clientId") or payload.get("client_id"),
        anchor_x=position.get("x"),
        anchor_y=position.get("y"),
        anchor_z=position.get("z"),
        request_payload_json=_make_json_safe(dict(payload), max_depth=30),
        metadata_json={
            "routeSource": ROUTE_SOURCE,
            "projectId": project.project_id,
            "universeId": universe.universe_id,
            "worldId": world.world_id,
        },
    )

    db.session.add(command_log)
    db.session.flush()
    return command_log


def _create_chunk_event(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    command_log: WorldCommandLog,
    snapshot: ChunkSnapshot,
    command_type: str,
    event_type: str,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    user_id: Optional[str],
    session_id: Optional[str],
    position: Optional[Mapping[str, int]] = None,
    local_position: Optional[Mapping[str, int]] = None,
    before_block_type_id: Optional[str] = None,
    after_block_type_id: Optional[str] = None,
    before_cell_value: Optional[int] = None,
    after_cell_value: Optional[int] = None,
    target_face: Optional[str] = None,
    tool: Optional[str] = None,
    chunk_revision_before: Optional[int] = None,
    chunk_version_before: Optional[str] = None,
    content_hash_before: Optional[str] = None,
    affected_cells: Optional[Sequence[Mapping[str, Any]]] = None,
    dirty_chunks: Optional[Sequence[str]] = None,
    object_instance_id: Optional[str] = None,
    object_type_id: Optional[str] = None,
    object_variant_id: Optional[str] = None,
    object_footprint_json: Optional[Mapping[str, Any]] = None,
    affected_bounds_json: Optional[Mapping[str, Any]] = None,
    payload_json: Optional[Mapping[str, Any]] = None,
) -> ChunkEvent:
    """Create and flush ChunkEvent."""
    event = ChunkEvent.create(
        project_db_id=project.id,
        universe_db_id=universe.id,
        world_db_id=world.id,
        command_log_db_id=command_log.id,
        command_id=command_log.command_id,
        command_type=command_type,
        chunk_snapshot_db_id=snapshot.id,
        event_type=event_type,
        user_id=user_id,
        session_id=session_id,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
        position_x=position.get("x") if position else None,
        position_y=position.get("y") if position else None,
        position_z=position.get("z") if position else None,
        local_x=local_position.get("x") if local_position else None,
        local_y=local_position.get("y") if local_position else None,
        local_z=local_position.get("z") if local_position else None,
        block_before_type_id=before_block_type_id,
        block_after_type_id=after_block_type_id,
        cell_before_value=before_cell_value,
        cell_after_value=after_cell_value,
        target_face=target_face,
        tool=tool,
        chunk_revision_before=chunk_revision_before,
        chunk_revision_after=snapshot.chunk_revision,
        chunk_version_before=chunk_version_before,
        chunk_version_after=snapshot.chunk_version,
        content_hash_before=content_hash_before,
        content_hash_after=snapshot.content_hash,
        object_instance_id=object_instance_id,
        object_type_id=object_type_id,
        object_variant_id=object_variant_id,
        object_footprint_json=_make_json_safe(object_footprint_json or {}, max_depth=30),
        affected_bounds_json=_make_json_safe(affected_bounds_json or {}, max_depth=30),
        affected_cells_json=_make_json_safe(list(affected_cells or []), max_depth=30),
        dirty_chunks_json=list(dirty_chunks or []),
        payload_json=_make_json_safe(dict(payload_json or {}), max_depth=30),
        metadata_json={
            "routeSource": ROUTE_SOURCE,
            "projectId": project.project_id,
            "universeId": universe.universe_id,
            "worldId": world.world_id,
        },
    )

    db.session.add(event)
    db.session.flush()
    return event


def _execute_set_or_remove_block(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    payload: Mapping[str, Any],
    command_log: WorldCommandLog,
    command_type: str,
    user_id: Optional[str],
    session_id: Optional[str],
) -> dict[str, Any]:
    """Execute SetBlock, ReplaceBlock or RemoveBlock."""
    position = _get_payload_position(payload, required=True)
    chunk_size = int(world.chunk_size or 16)
    cell = _world_position_to_chunk_cell(position, chunk_size)

    _log_checkpoint(
        "before_load_chunk_for_mutation",
        commandType=command_type,
        chunkKey=cell["chunkKey"],
        position=position,
    )

    existing_snapshot, content = _load_chunk_for_mutation(
        project=project,
        universe=universe,
        world=world,
        chunk_x=cell["chunkX"],
        chunk_y=cell["chunkY"],
        chunk_z=cell["chunkZ"],
    )

    before_revision = existing_snapshot.chunk_revision if existing_snapshot else None
    before_version = existing_snapshot.chunk_version if existing_snapshot else "generated"
    before_hash = existing_snapshot.content_hash if existing_snapshot else content.get("contentHash")

    before_cell_value = _get_cell_value(
        content,
        local_x=cell["localX"],
        local_y=cell["localY"],
        local_z=cell["localZ"],
        chunk_size=chunk_size,
    )
    before_block_type_id = _block_type_id_from_cell_value(content, before_cell_value)

    if command_type == "RemoveBlock":
        _validate_breakable_before_cell(
            world=world,
            content=content,
            before_cell_value=before_cell_value,
        )
        after_block_type_id = None
        after_cell_value = AIR_CELL_VALUE
        materialized_reason = "remove_block"
    else:
        after_block_type_id = _get_payload_block_type_id(payload, required=True)
        block = _get_block_type(
            world=world,
            block_type_id=after_block_type_id,
            require_placeable=True,
        )
        after_cell_value = _cell_value_for_block_type(content, block)
        materialized_reason = "replace_block" if command_type == "ReplaceBlock" else "set_block"

    changed = int(before_cell_value) != int(after_cell_value)

    affected_cell = {
        "x": position["x"],
        "y": position["y"],
        "z": position["z"],
        "chunkX": cell["chunkX"],
        "chunkY": cell["chunkY"],
        "chunkZ": cell["chunkZ"],
        "localX": cell["localX"],
        "localY": cell["localY"],
        "localZ": cell["localZ"],
        "beforeCellValue": before_cell_value,
        "afterCellValue": after_cell_value,
        "beforeBlockTypeId": before_block_type_id,
        "afterBlockTypeId": after_block_type_id,
    }

    dirty_chunks = _dirty_chunk_keys_for_cell(cell, chunk_size)

    if not changed:
        _mark_command_applied(
            command_log,
            changed=False,
            affected_chunks_json=[cell["chunkKey"]],
            affected_cells_json=[affected_cell],
            event_count=0,
            result_payload_json={
                "changed": False,
                "reason": "cell_already_has_requested_value",
                "dirtyChunks": [],
            },
        )

        return {
            "changed": False,
            "commandType": command_type,
            "eventIds": [],
            "changedChunks": [],
            "dirtyChunks": [],
            "affectedCells": [affected_cell],
            "snapshotIds": [],
            "chunkVersions": {},
            "message": "No change.",
        }

    _set_cell_value(
        content,
        local_x=cell["localX"],
        local_y=cell["localY"],
        local_z=cell["localZ"],
        chunk_size=chunk_size,
        cell_value=after_cell_value,
    )

    snapshot = _save_snapshot_after_mutation(
        project=project,
        universe=universe,
        world=world,
        existing_snapshot=existing_snapshot,
        content=content,
        materialized_reason=materialized_reason,
        command_log=command_log,
        user_id=user_id,
        session_id=session_id,
    )

    event = _create_chunk_event(
        project=project,
        universe=universe,
        world=world,
        command_log=command_log,
        snapshot=snapshot,
        command_type=command_type,
        event_type="block_change",
        chunk_x=cell["chunkX"],
        chunk_y=cell["chunkY"],
        chunk_z=cell["chunkZ"],
        user_id=user_id,
        session_id=session_id,
        position=position,
        local_position={
            "x": cell["localX"],
            "y": cell["localY"],
            "z": cell["localZ"],
        },
        before_block_type_id=before_block_type_id,
        after_block_type_id=after_block_type_id,
        before_cell_value=before_cell_value,
        after_cell_value=after_cell_value,
        target_face=_get_payload_target_face(payload),
        tool=_get_payload_tool(payload),
        chunk_revision_before=before_revision,
        chunk_version_before=before_version,
        content_hash_before=before_hash,
        affected_cells=[affected_cell],
        dirty_chunks=dirty_chunks,
        payload_json=dict(payload),
    )

    _attach_event_to_snapshot(snapshot, event, user_id=user_id, session_id=session_id)

    _mark_command_applied(
        command_log,
        changed=True,
        affected_chunks_json=[cell["chunkKey"]],
        affected_cells_json=[affected_cell],
        event_count=1,
        result_payload_json={
            "changed": True,
            "eventId": event.event_id,
            "snapshotId": snapshot.snapshot_id,
            "chunkVersion": snapshot.chunk_version,
            "changedChunks": [cell["chunkKey"]],
            "dirtyChunks": dirty_chunks,
        },
    )

    return {
        "changed": True,
        "commandType": command_type,
        "eventIds": [event.event_id],
        "changedChunks": [cell["chunkKey"]],
        "dirtyChunks": dirty_chunks,
        "affectedCells": [affected_cell],
        "snapshotIds": [snapshot.snapshot_id],
        "chunkVersions": {
            cell["chunkKey"]: snapshot.chunk_version,
        },
    }


# -----------------------------------------------------------------------------
# Object commands
# -----------------------------------------------------------------------------

def _extract_object_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract object payload block."""
    object_payload = payload.get("object")

    if isinstance(object_payload, Mapping):
        result = dict(object_payload)
    else:
        result = {}

    for key in (
        "objectInstanceId",
        "object_instance_id",
        "objectTypeId",
        "object_type_id",
        "objectVariantId",
        "object_variant_id",
        "variantId",
        "variant_id",
        "dimensions",
        "rotation",
        "transform",
    ):
        if key in payload and key not in result:
            result[key] = payload[key]

    return result


def _extract_object_dimensions(payload: Mapping[str, Any]) -> dict[str, int]:
    """Extract object dimensions."""
    object_payload = _extract_object_payload(payload)
    dimensions = object_payload.get("dimensions")

    if not isinstance(dimensions, Mapping):
        dimensions = {}

    size_x = (
        dimensions.get("x")
        or dimensions.get("width")
        or payload.get("sizeX")
        or payload.get("size_x")
        or payload.get("width")
        or 1
    )
    size_y = (
        dimensions.get("y")
        or dimensions.get("height")
        or payload.get("sizeY")
        or payload.get("size_y")
        or payload.get("height")
        or 1
    )
    size_z = (
        dimensions.get("z")
        or dimensions.get("depth")
        or payload.get("sizeZ")
        or payload.get("size_z")
        or payload.get("depth")
        or 1
    )

    return {
        "x": _coerce_positive_int(
            size_x,
            field_name="dimensions.x",
            maximum=_get_max_object_size_x(),
        ),
        "y": _coerce_positive_int(
            size_y,
            field_name="dimensions.y",
            maximum=_get_max_object_size_y(),
        ),
        "z": _coerce_positive_int(
            size_z,
            field_name="dimensions.z",
            maximum=_get_max_object_size_z(),
        ),
    }


def _iter_object_cells(anchor: Mapping[str, int], dimensions: Mapping[str, int]) -> list[dict[str, int]]:
    """Build occupied world cells for an axis-aligned rectangular object."""
    max_cells = _get_max_command_affected_cells()
    count = int(dimensions["x"]) * int(dimensions["y"]) * int(dimensions["z"])

    if count > max_cells:
        raise ValueError(f"Object affects {count} cells, but maximum is {max_cells}.")

    cells: list[dict[str, int]] = []

    for dz in range(int(dimensions["z"])):
        for dy in range(int(dimensions["y"])):
            for dx in range(int(dimensions["x"])):
                cells.append(
                    {
                        "x": int(anchor["x"]) + dx,
                        "y": int(anchor["y"]) + dy,
                        "z": int(anchor["z"]) + dz,
                    }
                )

    return cells


def _group_world_cells_by_chunk(
    cells: Sequence[Mapping[str, int]],
    *,
    chunk_size: int,
) -> dict[str, dict[str, Any]]:
    """Group world cells by chunk key."""
    groups: dict[str, dict[str, Any]] = {}

    for world_cell in cells:
        cell = _world_position_to_chunk_cell(world_cell, chunk_size)
        chunk_key = cell["chunkKey"]

        group = groups.setdefault(
            chunk_key,
            {
                "chunkX": cell["chunkX"],
                "chunkY": cell["chunkY"],
                "chunkZ": cell["chunkZ"],
                "chunkKey": chunk_key,
                "cells": [],
            },
        )
        group["cells"].append(cell)

    return groups


def _execute_place_object(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    payload: Mapping[str, Any],
    command_log: WorldCommandLog,
    user_id: Optional[str],
    session_id: Optional[str],
) -> dict[str, Any]:
    """Execute simple rectangular PlaceObject using one fill block type."""
    anchor = _get_payload_position(payload, required=True)
    dimensions = _extract_object_dimensions(payload)
    object_payload = _extract_object_payload(payload)

    object_type_id = (
        object_payload.get("objectTypeId")
        or object_payload.get("object_type_id")
        or payload.get("objectTypeId")
        or payload.get("object_type_id")
        or "block_composite"
    )
    object_variant_id = (
        object_payload.get("objectVariantId")
        or object_payload.get("object_variant_id")
        or object_payload.get("variantId")
        or object_payload.get("variant_id")
        or payload.get("objectVariantId")
        or payload.get("object_variant_id")
    )
    object_instance_id = (
        object_payload.get("objectInstanceId")
        or object_payload.get("object_instance_id")
        or payload.get("objectInstanceId")
        or payload.get("object_instance_id")
    )

    fill_block_type_id = _get_payload_block_type_id(payload, required=True)
    fill_block = _get_block_type(
        world=world,
        block_type_id=fill_block_type_id,
        require_placeable=True,
    )

    occupied_world_cells = _iter_object_cells(anchor, dimensions)
    grouped = _group_world_cells_by_chunk(occupied_world_cells, chunk_size=int(world.chunk_size or 16))
    touched_chunks = sorted(grouped.keys())

    object_instance = WorldObjectInstance.create_for_world(
        world,
        object_instance_id=object_instance_id,
        object_type_id=object_type_id,
        object_variant_id=object_variant_id,
        anchor_x=anchor["x"],
        anchor_y=anchor["y"],
        anchor_z=anchor["z"],
        size_x=dimensions["x"],
        size_y=dimensions["y"],
        size_z=dimensions["z"],
        object_source=payload.get("objectSource") or payload.get("object_source") or "editor",
        object_kind=payload.get("objectKind") or payload.get("object_kind") or "block_composite",
        rotation_json=object_payload.get("rotation") if isinstance(object_payload.get("rotation"), Mapping) else None,
        transform_json=object_payload.get("transform") if isinstance(object_payload.get("transform"), Mapping) else None,
        occupied_cells_json=occupied_world_cells,
        touched_chunks_json=touched_chunks,
        primary_chunk_x=grouped[touched_chunks[0]]["chunkX"] if touched_chunks else None,
        primary_chunk_y=grouped[touched_chunks[0]]["chunkY"] if touched_chunks else None,
        primary_chunk_z=grouped[touched_chunks[0]]["chunkZ"] if touched_chunks else None,
        primary_chunk_key=touched_chunks[0] if touched_chunks else None,
        created_by_command_id=command_log.command_id,
        created_by_user_id=user_id,
        updated_by_user_id=user_id,
        last_session_id=session_id,
        metadata_json={
            "routeSource": ROUTE_SOURCE,
            "fillBlockTypeId": fill_block_type_id,
            "phase": "rectangular-block-composite-v1",
        },
    )
    db.session.add(object_instance)
    db.session.flush()

    changed_chunks: list[str] = []
    dirty_chunks_set: set[str] = set()
    affected_cells_all: list[dict[str, Any]] = []
    event_ids: list[str] = []
    snapshot_ids: list[str] = []
    chunk_versions: dict[str, str] = {}

    object_ref = {
        "objectInstanceId": object_instance.object_instance_id,
        "objectTypeId": object_instance.object_type_id,
        "objectVariantId": object_instance.object_variant_id,
        "anchor": anchor,
        "dimensions": dimensions,
        "fillBlockTypeId": fill_block_type_id,
    }

    for chunk_key, group in grouped.items():
        existing_snapshot, content = _load_chunk_for_mutation(
            project=project,
            universe=universe,
            world=world,
            chunk_x=group["chunkX"],
            chunk_y=group["chunkY"],
            chunk_z=group["chunkZ"],
        )

        before_revision = existing_snapshot.chunk_revision if existing_snapshot else None
        before_version = existing_snapshot.chunk_version if existing_snapshot else "generated"
        before_hash = existing_snapshot.content_hash if existing_snapshot else content.get("contentHash")

        after_cell_value = _cell_value_for_block_type(content, fill_block)
        group_affected_cells: list[dict[str, Any]] = []
        chunk_changed = False

        for cell in group["cells"]:
            before_cell_value = _get_cell_value(
                content,
                local_x=cell["localX"],
                local_y=cell["localY"],
                local_z=cell["localZ"],
                chunk_size=int(world.chunk_size or 16),
            )
            before_block_type_id = _block_type_id_from_cell_value(content, before_cell_value)

            if int(before_cell_value) != int(after_cell_value):
                _set_cell_value(
                    content,
                    local_x=cell["localX"],
                    local_y=cell["localY"],
                    local_z=cell["localZ"],
                    chunk_size=int(world.chunk_size or 16),
                    cell_value=after_cell_value,
                )
                chunk_changed = True

            affected_cell = {
                "x": cell["worldX"],
                "y": cell["worldY"],
                "z": cell["worldZ"],
                "chunkX": cell["chunkX"],
                "chunkY": cell["chunkY"],
                "chunkZ": cell["chunkZ"],
                "localX": cell["localX"],
                "localY": cell["localY"],
                "localZ": cell["localZ"],
                "beforeCellValue": before_cell_value,
                "afterCellValue": after_cell_value,
                "beforeBlockTypeId": before_block_type_id,
                "afterBlockTypeId": fill_block_type_id,
                "objectInstanceId": object_instance.object_instance_id,
            }
            group_affected_cells.append(affected_cell)
            affected_cells_all.append(affected_cell)

            for dirty_key in _dirty_chunk_keys_for_cell(cell, int(world.chunk_size or 16)):
                dirty_chunks_set.add(dirty_key)

        _add_object_ref_to_content(content, object_ref)

        snapshot = _save_snapshot_after_mutation(
            project=project,
            universe=universe,
            world=world,
            existing_snapshot=existing_snapshot,
            content=content,
            materialized_reason="object_placement",
            command_log=command_log,
            user_id=user_id,
            session_id=session_id,
        )

        ref_obj = WorldObjectChunkRef.create_for_object(
            object_instance,
            chunk_x=group["chunkX"],
            chunk_y=group["chunkY"],
            chunk_z=group["chunkZ"],
            chunk_key=chunk_key,
            ref_role="primary" if chunk_key == object_instance.primary_chunk_key else "occupied",
            occupied_cells_json=group_affected_cells,
            object_content_hash=snapshot.content_hash,
            world_bounds_json=object_instance.bounds_json,
            metadata_json={
                "routeSource": ROUTE_SOURCE,
                "fillBlockTypeId": fill_block_type_id,
            },
        )
        db.session.add(ref_obj)
        db.session.flush()

        event = _create_chunk_event(
            project=project,
            universe=universe,
            world=world,
            command_log=command_log,
            snapshot=snapshot,
            command_type="PlaceObject",
            event_type="object_change",
            chunk_x=group["chunkX"],
            chunk_y=group["chunkY"],
            chunk_z=group["chunkZ"],
            user_id=user_id,
            session_id=session_id,
            position=anchor,
            local_position=None,
            before_block_type_id=None,
            after_block_type_id=fill_block_type_id,
            before_cell_value=None,
            after_cell_value=after_cell_value,
            target_face=_get_payload_target_face(payload),
            tool=_get_payload_tool(payload),
            chunk_revision_before=before_revision,
            chunk_version_before=before_version,
            content_hash_before=before_hash,
            affected_cells=group_affected_cells,
            dirty_chunks=sorted(dirty_chunks_set),
            object_instance_id=object_instance.object_instance_id,
            object_type_id=object_instance.object_type_id,
            object_variant_id=object_instance.object_variant_id,
            object_footprint_json=object_instance.footprint_json,
            affected_bounds_json=object_instance.bounds_json,
            payload_json=dict(payload),
        )

        _attach_event_to_snapshot(snapshot, event, user_id=user_id, session_id=session_id)

        try:
            object_instance.updated_event_id = event.event_id
            if object_instance.created_event_id is None:
                object_instance.created_event_id = event.event_id
        except Exception:
            pass

        event_ids.append(event.event_id)
        snapshot_ids.append(snapshot.snapshot_id)
        chunk_versions[chunk_key] = snapshot.chunk_version

        if chunk_changed or chunk_key not in changed_chunks:
            changed_chunks.append(chunk_key)

    dirty_chunks = sorted(dirty_chunks_set)

    try:
        command_log.object_instance_id = object_instance.object_instance_id
        command_log.object_type_id = object_instance.object_type_id
        command_log.object_variant_id = object_instance.object_variant_id
        command_log.object_size_x = dimensions["x"]
        command_log.object_size_y = dimensions["y"]
        command_log.object_size_z = dimensions["z"]
        command_log.affected_bounds_json = object_instance.bounds_json
    except Exception:
        pass

    _mark_command_applied(
        command_log,
        changed=True,
        affected_chunks_json=touched_chunks,
        affected_cells_json=affected_cells_all,
        event_count=len(event_ids),
        result_payload_json={
            "changed": True,
            "objectInstanceId": object_instance.object_instance_id,
            "eventIds": event_ids,
            "snapshotIds": snapshot_ids,
            "changedChunks": changed_chunks,
            "dirtyChunks": dirty_chunks,
        },
    )

    return {
        "changed": True,
        "commandType": "PlaceObject",
        "objectInstanceId": object_instance.object_instance_id,
        "eventIds": event_ids,
        "changedChunks": sorted(changed_chunks),
        "dirtyChunks": dirty_chunks,
        "affectedCells": affected_cells_all,
        "snapshotIds": snapshot_ids,
        "chunkVersions": chunk_versions,
        "object": _safe_model_to_dict(
            object_instance,
            fallback={"objectInstanceId": object_instance.object_instance_id},
            include_internal=False,
            include_cells=False,
        ),
    }


def _execute_remove_object(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    payload: Mapping[str, Any],
    command_log: WorldCommandLog,
    user_id: Optional[str],
    session_id: Optional[str],
) -> dict[str, Any]:
    """Execute basic RemoveObject using stored WorldObjectChunkRef occupied cells."""
    object_payload = _extract_object_payload(payload)
    object_instance_id = (
        payload.get("objectInstanceId")
        or payload.get("object_instance_id")
        or object_payload.get("objectInstanceId")
        or object_payload.get("object_instance_id")
    )
    object_instance_id = _coerce_string(object_instance_id)

    if not object_instance_id:
        raise ValueError("objectInstanceId is required for RemoveObject.")

    query = WorldObjectInstance.query.filter(
        WorldObjectInstance.world_db_id == world.id,
        WorldObjectInstance.object_instance_id == object_instance_id,
        WorldObjectInstance.deleted_at.is_(None),
    )
    query = _query_without_relationships(query)

    object_instance = _safe_one_or_none(
        query,
        entity_name="WorldObjectInstance",
        lookup={"worldDbId": world.id, "objectInstanceId": object_instance_id},
    )

    if object_instance is None:
        raise LookupError(f"Object instance '{object_instance_id}' was not found.")

    refs_query = WorldObjectChunkRef.query.filter(
        WorldObjectChunkRef.object_instance_db_id == object_instance.id,
        WorldObjectChunkRef.deleted_at.is_(None),
    )
    refs_query = _query_without_relationships(refs_query)

    refs = refs_query.all()

    changed_chunks: list[str] = []
    dirty_chunks_set: set[str] = set()
    affected_cells_all: list[dict[str, Any]] = []
    event_ids: list[str] = []
    snapshot_ids: list[str] = []
    chunk_versions: dict[str, str] = {}

    for ref in refs:
        existing_snapshot, content = _load_chunk_for_mutation(
            project=project,
            universe=universe,
            world=world,
            chunk_x=ref.chunk_x,
            chunk_y=ref.chunk_y,
            chunk_z=ref.chunk_z,
        )

        before_revision = existing_snapshot.chunk_revision if existing_snapshot else None
        before_version = existing_snapshot.chunk_version if existing_snapshot else "generated"
        before_hash = existing_snapshot.content_hash if existing_snapshot else content.get("contentHash")

        group_cells = ref.occupied_cells_json if isinstance(ref.occupied_cells_json, list) else []
        group_affected_cells: list[dict[str, Any]] = []
        chunk_changed = False

        for cell in group_cells:
            if not isinstance(cell, Mapping):
                continue

            local_x = _coerce_int(cell.get("localX", cell.get("local_x")), field_name="localX")
            local_y = _coerce_int(cell.get("localY", cell.get("local_y")), field_name="localY")
            local_z = _coerce_int(cell.get("localZ", cell.get("local_z")), field_name="localZ")

            before_cell_value = _get_cell_value(
                content,
                local_x=local_x,
                local_y=local_y,
                local_z=local_z,
                chunk_size=int(world.chunk_size or 16),
            )
            before_block_type_id = _block_type_id_from_cell_value(content, before_cell_value)

            if before_cell_value != AIR_CELL_VALUE:
                _set_cell_value(
                    content,
                    local_x=local_x,
                    local_y=local_y,
                    local_z=local_z,
                    chunk_size=int(world.chunk_size or 16),
                    cell_value=AIR_CELL_VALUE,
                )
                chunk_changed = True

            world_x = _coerce_int(
                cell.get("x"),
                fallback=int(ref.chunk_x) * int(world.chunk_size or 16) + local_x,
                field_name="x",
            )
            world_y = _coerce_int(
                cell.get("y"),
                fallback=int(ref.chunk_y) * int(world.chunk_size or 16) + local_y,
                field_name="y",
            )
            world_z = _coerce_int(
                cell.get("z"),
                fallback=int(ref.chunk_z) * int(world.chunk_size or 16) + local_z,
                field_name="z",
            )

            cell_context = {
                "worldX": world_x,
                "worldY": world_y,
                "worldZ": world_z,
                "chunkX": int(ref.chunk_x),
                "chunkY": int(ref.chunk_y),
                "chunkZ": int(ref.chunk_z),
                "localX": local_x,
                "localY": local_y,
                "localZ": local_z,
                "chunkKey": ref.chunk_key,
            }

            affected_cell = {
                "x": world_x,
                "y": world_y,
                "z": world_z,
                "chunkX": int(ref.chunk_x),
                "chunkY": int(ref.chunk_y),
                "chunkZ": int(ref.chunk_z),
                "localX": local_x,
                "localY": local_y,
                "localZ": local_z,
                "beforeCellValue": before_cell_value,
                "afterCellValue": AIR_CELL_VALUE,
                "beforeBlockTypeId": before_block_type_id,
                "afterBlockTypeId": None,
                "objectInstanceId": object_instance.object_instance_id,
            }
            group_affected_cells.append(affected_cell)
            affected_cells_all.append(affected_cell)

            for dirty_key in _dirty_chunk_keys_for_cell(cell_context, int(world.chunk_size or 16)):
                dirty_chunks_set.add(dirty_key)

        _remove_object_ref_from_content(content, object_instance.object_instance_id)

        snapshot = _save_snapshot_after_mutation(
            project=project,
            universe=universe,
            world=world,
            existing_snapshot=existing_snapshot,
            content=content,
            materialized_reason="object_removal",
            command_log=command_log,
            user_id=user_id,
            session_id=session_id,
        )

        event = _create_chunk_event(
            project=project,
            universe=universe,
            world=world,
            command_log=command_log,
            snapshot=snapshot,
            command_type="RemoveObject",
            event_type="object_change",
            chunk_x=int(ref.chunk_x),
            chunk_y=int(ref.chunk_y),
            chunk_z=int(ref.chunk_z),
            user_id=user_id,
            session_id=session_id,
            position={
                "x": object_instance.anchor_x,
                "y": object_instance.anchor_y,
                "z": object_instance.anchor_z,
            },
            before_block_type_id=None,
            after_block_type_id=None,
            before_cell_value=None,
            after_cell_value=AIR_CELL_VALUE,
            target_face=_get_payload_target_face(payload),
            tool=_get_payload_tool(payload),
            chunk_revision_before=before_revision,
            chunk_version_before=before_version,
            content_hash_before=before_hash,
            affected_cells=group_affected_cells,
            dirty_chunks=sorted(dirty_chunks_set),
            object_instance_id=object_instance.object_instance_id,
            object_type_id=object_instance.object_type_id,
            object_variant_id=object_instance.object_variant_id,
            object_footprint_json=object_instance.footprint_json,
            affected_bounds_json=object_instance.bounds_json,
            payload_json=dict(payload),
        )

        _attach_event_to_snapshot(snapshot, event, user_id=user_id, session_id=session_id)

        try:
            ref.soft_delete()
        except Exception:
            try:
                ref.deleted_at = datetime.utcnow()
            except Exception:
                pass

        db.session.add(ref)

        event_ids.append(event.event_id)
        snapshot_ids.append(snapshot.snapshot_id)
        chunk_versions[ref.chunk_key] = snapshot.chunk_version

        if chunk_changed or ref.chunk_key not in changed_chunks:
            changed_chunks.append(ref.chunk_key)

    try:
        object_instance.soft_delete(
            updated_by_user_id=user_id,
            command_id=command_log.command_id,
            event_id=event_ids[-1] if event_ids else None,
        )
    except TypeError:
        try:
            object_instance.soft_delete(updated_by_user_id=user_id)
        except Exception:
            object_instance.deleted_at = datetime.utcnow()
    except Exception:
        try:
            object_instance.deleted_at = datetime.utcnow()
        except Exception:
            pass

    db.session.add(object_instance)

    dirty_chunks = sorted(dirty_chunks_set)

    try:
        command_log.object_instance_id = object_instance.object_instance_id
        command_log.object_type_id = object_instance.object_type_id
        command_log.object_variant_id = object_instance.object_variant_id
        command_log.affected_bounds_json = object_instance.bounds_json
    except Exception:
        pass

    _mark_command_applied(
        command_log,
        changed=True,
        affected_chunks_json=sorted(changed_chunks),
        affected_cells_json=affected_cells_all,
        event_count=len(event_ids),
        result_payload_json={
            "changed": True,
            "objectInstanceId": object_instance.object_instance_id,
            "eventIds": event_ids,
            "snapshotIds": snapshot_ids,
            "changedChunks": sorted(changed_chunks),
            "dirtyChunks": dirty_chunks,
        },
    )

    return {
        "changed": True,
        "commandType": "RemoveObject",
        "objectInstanceId": object_instance.object_instance_id,
        "eventIds": event_ids,
        "changedChunks": sorted(changed_chunks),
        "dirtyChunks": dirty_chunks,
        "affectedCells": affected_cells_all,
        "snapshotIds": snapshot_ids,
        "chunkVersions": chunk_versions,
    }


def _execute_command(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    payload: Mapping[str, Any],
) -> tuple[WorldCommandLog, dict[str, Any]]:
    """Create command log and execute command."""
    command_type = _normalize_command_type(
        payload.get("type")
        or payload.get("commandType")
        or payload.get("command_type")
    )
    user_id = _get_payload_user_id(payload)
    session_id = _get_payload_session_id(payload)

    command_log = _create_command_log(
        project=project,
        universe=universe,
        world=world,
        payload=payload,
        command_type=command_type,
        user_id=user_id,
        session_id=session_id,
    )

    _log_checkpoint(
        "command_log_created",
        commandId=command_log.command_id,
        commandType=command_type,
    )

    if command_type in {"SetBlock", "RemoveBlock", "ReplaceBlock"}:
        result = _execute_set_or_remove_block(
            project=project,
            universe=universe,
            world=world,
            payload=payload,
            command_log=command_log,
            command_type=command_type,
            user_id=user_id,
            session_id=session_id,
        )
    elif command_type == "PlaceObject":
        result = _execute_place_object(
            project=project,
            universe=universe,
            world=world,
            payload=payload,
            command_log=command_log,
            user_id=user_id,
            session_id=session_id,
        )
    elif command_type == "RemoveObject":
        result = _execute_remove_object(
            project=project,
            universe=universe,
            world=world,
            payload=payload,
            command_log=command_log,
            user_id=user_id,
            session_id=session_id,
        )
    else:
        raise ValueError(f"Command type '{command_type}' is not implemented in this route.")

    db.session.add(command_log)
    db.session.flush()

    return command_log, result


def _serialize_command_log(command_log: WorldCommandLog, *, project: Project, universe: Universe, world: WorldInstance) -> dict[str, Any]:
    """Serialize command log shallowly."""
    fallback = {
        "commandId": getattr(command_log, "command_id", None),
        "commandType": getattr(command_log, "command_type", None),
        "commandStatus": getattr(command_log, "command_status", None),
        "changed": getattr(command_log, "changed", None),
    }

    return _safe_model_to_dict(
        command_log,
        fallback=fallback,
        include_internal=False,
        include_payloads=False,
        project_id=project.project_id,
        universe_id=universe.universe_id,
        world_id=world.world_id,
    )


def _serialize_command_result(
    *,
    project: Project,
    universe: Universe,
    world: WorldInstance,
    command_log: WorldCommandLog,
    result: Mapping[str, Any],
    include_command_log: bool = True,
) -> dict[str, Any]:
    """Serialize command execution result."""
    body: dict[str, Any] = {
        "projectId": project.project_id,
        "universeId": universe.universe_id,
        "worldId": world.world_id,
        "templateId": world.template_id,
        "providerId": world.provider_id,
        "providerWorldId": world.provider_world_id,
        "commandId": command_log.command_id,
        "commandType": result.get("commandType") or command_log.command_type,
        "commandStatus": getattr(command_log, "command_status", None),
        "changed": bool(result.get("changed")),
        "eventIds": list(result.get("eventIds") or []),
        "changedChunks": list(result.get("changedChunks") or []),
        "dirtyChunks": list(result.get("dirtyChunks") or []),
        "affectedCells": _make_json_safe(list(result.get("affectedCells") or []), max_depth=35),
        "snapshotIds": list(result.get("snapshotIds") or []),
        "chunkVersions": dict(result.get("chunkVersions") or {}),
        "flags": {
            "dbBacked": True,
            "projectScoped": True,
            "snapshotWritten": bool(result.get("snapshotIds")),
            "eventsWritten": bool(result.get("eventIds")),
            "objectCommand": command_log.command_type in {"PlaceObject", "RemoveObject", "ReplaceObject"},
        },
        "route": {
            "source": ROUTE_SOURCE,
            "projectId": project.project_id,
            "universeId": universe.universe_id,
            "worldId": world.world_id,
            "projectScoped": True,
        },
    }

    if result.get("objectInstanceId"):
        body["objectInstanceId"] = result.get("objectInstanceId")

    if result.get("object"):
        body["object"] = _make_json_safe(result.get("object"), max_depth=35)

    if result.get("message"):
        body["message"] = result.get("message")

    if include_command_log:
        body["commandLog"] = _serialize_command_log(
            command_log,
            project=project,
            universe=universe,
            world=world,
        )

    return body


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@commands_bp.post("/projects/<project_id>/worlds/<world_id>/commands")
def post_project_world_command(project_id: str, world_id: str):
    """
    Execute one world command.

    Examples:

        POST /projects/dev-project/worlds/world_spawn/commands

        {
          "type": "SetBlock",
          "position": { "x": 1, "y": 1, "z": 1 },
          "blockTypeId": "debug_grass",
          "userId": "user_123",
          "sessionId": "session_abc"
        }

        {
          "type": "RemoveBlock",
          "position": { "x": 1, "y": 1, "z": 1 }
        }
    """
    started_at = time.perf_counter()

    try:
        payload = _get_json_body()

        _log_checkpoint(
            "request_start",
            path=request.path,
            projectRouteId=project_id,
            worldRouteId=world_id,
            payload=_make_json_safe(payload, max_depth=8),
        )

        universe_id = (
            payload.get("universeId")
            or payload.get("universe_id")
            or _get_query_string("universeId", "universe_id", fallback="")
            or None
        )
        include_deleted = _get_query_bool("includeDeleted", "include_deleted", fallback=False)
        include_command_log = _get_query_bool("includeCommandLog", "include_command_log", fallback=True)

        project, universe, world = _resolve_project_world_context(
            project_id,
            world_id,
            universe_id=universe_id,
            include_deleted=include_deleted,
        )

        command_log, result = _execute_command(
            project=project,
            universe=universe,
            world=world,
            payload=payload,
        )

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)

        response_payload = _serialize_command_result(
            project=project,
            universe=universe,
            world=world,
            command_log=command_log,
            result=result,
            include_command_log=include_command_log,
        )

        body = _ok_response(
            response_version=COMMAND_RESPONSE_VERSION,
            payload=response_payload,
            metadata={
                "projectRouteId": project_id,
                "resolvedProjectId": project.project_id,
                "worldRouteId": world_id,
                "resolvedWorldId": world.world_id,
                "universeId": universe.universe_id,
                "commandType": command_log.command_type,
                "commandId": command_log.command_id,
                "dbBacked": True,
                "projectScoped": True,
                "elapsedMs": elapsed_ms,
            },
        )

        db.session.commit()

        _log_checkpoint(
            "request_done",
            commandId=command_log.command_id,
            commandType=command_log.command_type,
            changed=result.get("changed"),
            elapsedMs=elapsed_ms,
        )

        return _json_response(body, 200)

    except LookupError as exc:
        _safe_rollback()
        return _error_response(exc, code="command_context_not_found", status_code=404)

    except ValueError as exc:
        _safe_rollback()
        return _error_response(exc, code="invalid_command", status_code=400)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


@commands_bp.post("/projects/<project_id>/commands")
def post_project_spawn_world_command(project_id: str):
    """
    Development convenience route.

    Executes a command against the project's spawn world.
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

        return post_project_world_command(project.project_id, world.world_id)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


@commands_bp.post("/commands")
def post_default_project_world_command():
    """
    Development convenience route.

    Executes a command against query-selected or default project/world.
    """
    try:
        project_id = _get_query_string("projectId", "project_id", fallback="default")
        world_id = _get_query_string("worldId", "world_id", fallback="spawn")

        return post_project_world_command(project_id, world_id)

    except Exception as exc:
        _safe_rollback()
        return _error_response(exc)


@commands_bp.get("/commands/_status")
def get_commands_route_status():
    """
    Return diagnostics for command routes, database and model registration.
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
                    "worldCommandLogs": WorldCommandLog.query.count(),
                    "chunkEvents": ChunkEvent.query.count(),
                    "worldObjectInstances": WorldObjectInstance.query.count(),
                    "worldObjectChunkRefs": WorldObjectChunkRef.query.count(),
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
                "maxCommandAffectedCells": _get_max_command_affected_cells(),
                "maxObjectSize": {
                    "x": _get_max_object_size_x(),
                    "y": _get_max_object_size_y(),
                    "z": _get_max_object_size_z(),
                },
                "supportedCommands": [
                    "SetBlock",
                    "RemoveBlock",
                    "ReplaceBlock",
                    "PlaceObject",
                    "RemoveObject",
                ],
                "cellEncoding": {
                    "version": CELL_ENCODING_VERSION,
                    "airCellValue": AIR_CELL_VALUE,
                    "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
                },
                "databaseUriConfigured": bool(_get_config_value("SQLALCHEMY_DATABASE_URI")),
                "relationshipLoadingDisabledInCommandPath": bool(noload is not None),
            }

        body = _ok_response(
            response_version=COMMAND_STATUS_RESPONSE_VERSION,
            payload={
                "route": {
                    "source": ROUTE_SOURCE,
                    "moduleVersion": ROUTE_MODULE_VERSION,
                    "blueprint": commands_bp.name,
                    "dbBacked": True,
                    "snapshotWrites": True,
                    "eventWrites": True,
                    "objectStoragePrepared": True,
                    "relationshipLoadingDisabledInCommandPath": bool(noload is not None),
                    "productiveRoutes": [
                        "POST /projects/<project_id>/worlds/<world_id>/commands",
                    ],
                    "devConvenienceRoutes": [
                        "POST /projects/<project_id>/commands",
                        "POST /commands",
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
    "commands_bp",
    "ROUTE_MODULE_VERSION",
    "ROUTE_SOURCE",
)