# services/vectoplan-chunk/src/world_state/serializer.py
"""
Serializers for the VECTOPLAN world-state layer.

This module serializes project-scoped runtime state into API/editor-compatible
JSON dictionaries.

It supports both:
- older in-memory world_state model objects
- newer PostgreSQL-backed model/service objects

Important invariants:
- Provider/template world id may be `flat`.
- Productive route world id is the concrete world instance, for example
  `world_spawn`.
- Chunk responses must expose:

    worldId         = world_spawn
    templateId      = flat
    providerWorldId = flat

- Cell encoding remains:

    cellValue = 0
    -> Air

    cellValue = paletteIndex + 1
    -> Block
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from .errors import (
    WorldStateSerializationError,
    error_to_api_response_body,
    error_to_response_tuple,
    get_error_status_code,
)


try:
    from .service import (
        WorldStateBlocksResult,
        WorldStateChunkBatchResult,
        WorldStateChunkResult,
        WorldStateWorldMetadataResult,
    )
except Exception:  # pragma: no cover - serializer must remain importable during partial refactors
    WorldStateBlocksResult = None  # type: ignore[assignment]
    WorldStateChunkBatchResult = None  # type: ignore[assignment]
    WorldStateChunkResult = None  # type: ignore[assignment]
    WorldStateWorldMetadataResult = None  # type: ignore[assignment]


SERIALIZER_MODULE_VERSION = "0.2.0"
SERIALIZER_SOURCE = "world_state.serializer"

PROJECT_BOOTSTRAP_RESPONSE_VERSION = "project-bootstrap-response.v1"
WORLD_INSTANCE_RESPONSE_VERSION = "world-instance-response.v1"
WORLD_INSTANCE_LIST_RESPONSE_VERSION = "world-instance-list-response.v1"
WORLD_STATE_BLOCKS_RESPONSE_VERSION = "world-state-blocks-response.v1"
WORLD_STATE_CHUNK_RESPONSE_VERSION = "world-state-chunk-response.v1"
WORLD_STATE_CHUNK_BATCH_RESPONSE_VERSION = "world-state-chunk-batch-response.v1"
WORLD_STATE_WORLD_METADATA_RESPONSE_VERSION = "world-state-world-metadata-response.v1"
WORLD_STATE_ERROR_RESPONSE_VERSION = "world-state-error-response.v1"

CELL_ENCODING_VERSION = "cell-encoding.palette-index-plus-one.v1"
RUNTIME_CHUNK_CONTENT_VERSION = "runtime-chunk-content.v1"

DEFAULT_AIR_CELL_VALUE = 0
DEFAULT_CELL_INDEX_ORDER = "x-fastest-y-then-z"

_serializer_cache_lock = threading.RLock()
_serializer_status_cache: dict[str, Any] | None = None


# -----------------------------------------------------------------------------
# Generic safe helpers
# -----------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Return current UTC timestamp as ISO string."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def make_json_safe(value: Any) -> Any:
    """
    Convert arbitrary values into JSON-safe values.

    Kept local so serialization remains safe even for SQLAlchemy rows, provider
    objects, dataclasses and legacy context objects.
    """
    if value is None:
        return None

    if isinstance(value, (str, int, bool)):
        return value

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    if isinstance(value, Decimal):
        try:
            return float(value)
        except Exception:
            return str(value)

    if isinstance(value, (datetime, date)):
        try:
            if isinstance(value, datetime) and value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        except Exception:
            return str(value)

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Path):
        return str(value)

    if dataclasses.is_dataclass(value):
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                return make_json_safe(to_dict())
            except Exception:
                pass

        try:
            return make_json_safe(dataclasses.asdict(value))
        except Exception:
            return str(value)

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            try:
                safe_key = str(key)
            except Exception:
                safe_key = repr(key)

            result[safe_key] = make_json_safe(item)

        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        return [make_json_safe(item) for item in value]

    # SQLAlchemy and normal Python objects
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        for kwargs in (
            {},
            {"include_internal": False},
            {"include_metadata": True},
            {"include_internal": False, "include_metadata": True},
        ):
            try:
                return make_json_safe(to_dict(**kwargs))
            except TypeError:
                continue
            except Exception:
                break

    try:
        json.dumps(value)
        return value
    except Exception:
        pass

    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


def deep_copy_json(value: Any) -> Any:
    """Return JSON-safe deep copy."""
    try:
        return copy.deepcopy(make_json_safe(value))
    except Exception:
        return make_json_safe(value)


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_json_dumps(value: Any) -> str:
    """Dump stable JSON defensively."""
    try:
        return json.dumps(
            make_json_safe(value),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        return repr(value)


def _as_dict(value: Any, *, include_internal: bool = False, include_metadata: bool = True) -> dict[str, Any]:
    """
    Convert object to dictionary defensively.

    This supports:
    - dict-like objects
    - dataclasses
    - SQLAlchemy model objects with to_dict()
    - arbitrary provider objects
    """
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(make_json_safe(value))

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        attempts = (
            {
                "include_internal": include_internal,
                "include_metadata": include_metadata,
            },
            {
                "include_internal": include_internal,
            },
            {
                "includeInternal": include_internal,
            },
            {},
        )

        for kwargs in attempts:
            try:
                result = to_dict(**kwargs)
                if isinstance(result, Mapping):
                    return dict(make_json_safe(result))
                return {"value": make_json_safe(result)}
            except TypeError:
                continue
            except Exception as exc:
                raise WorldStateSerializationError(
                    "Object to_dict() failed.",
                    details={
                        "type": value.__class__.__name__,
                        "error": _safe_exception_message(exc),
                    },
                    cause=exc,
                ) from exc

    if dataclasses.is_dataclass(value):
        try:
            return dict(make_json_safe(dataclasses.asdict(value)))
        except Exception as exc:
            raise WorldStateSerializationError(
                "Dataclass serialization failed.",
                details={
                    "type": value.__class__.__name__,
                    "error": _safe_exception_message(exc),
                },
                cause=exc,
            ) from exc

    try:
        return {
            key: make_json_safe(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    except Exception:
        return {
            "value": make_json_safe(value),
            "type": value.__class__.__name__,
        }


def _call_to_dict(
    value: Any,
    *,
    include_internal: bool = False,
    include_metadata: bool = True,
    project_id: str | None = None,
    universe_id: str | None = None,
    world_id: str | None = None,
    include_content: bool | None = None,
) -> dict[str, Any]:
    """
    Call to_dict with the richest compatible signature.

    This is needed because different model/context objects expose slightly
    different to_dict signatures.
    """
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(make_json_safe(value))

    to_dict = getattr(value, "to_dict", None)

    if not callable(to_dict):
        return _as_dict(
            value,
            include_internal=include_internal,
            include_metadata=include_metadata,
        )

    attempts: list[dict[str, Any]] = [
        {
            "include_internal": include_internal,
            "include_metadata": include_metadata,
            "project_id": project_id,
            "universe_id": universe_id,
            "world_id": world_id,
        },
        {
            "include_internal": include_internal,
            "include_metadata": include_metadata,
            "project_id": project_id,
            "universe_id": universe_id,
        },
        {
            "include_internal": include_internal,
            "include_metadata": include_metadata,
            "project_id": project_id,
        },
        {
            "include_internal": include_internal,
            "include_metadata": include_metadata,
        },
        {
            "include_internal": include_internal,
        },
        {},
    ]

    if include_content is not None:
        attempts.insert(
            0,
            {
                "include_internal": include_internal,
                "include_metadata": include_metadata,
                "include_content": include_content,
                "project_id": project_id,
                "universe_id": universe_id,
                "world_id": world_id,
            },
        )

    for kwargs in attempts:
        clean_kwargs = {
            key: item
            for key, item in kwargs.items()
            if item is not None
        }

        try:
            result = to_dict(**clean_kwargs)
            if isinstance(result, Mapping):
                return dict(make_json_safe(result))
            return {"value": make_json_safe(result)}
        except TypeError:
            continue
        except Exception as exc:
            return {
                "type": value.__class__.__name__,
                "serializationError": _safe_exception_message(exc),
            }

    return _as_dict(
        value,
        include_internal=include_internal,
        include_metadata=include_metadata,
    )


def _get_first(
    data: Mapping[str, Any],
    *keys: str,
    fallback: Any = None,
) -> Any:
    """Read the first existing key from a mapping."""
    for key in keys:
        try:
            if key in data:
                return data[key]
        except Exception:
            continue

    return fallback


def _get_attr_or_key(value: Any, *names: str, fallback: Any = None) -> Any:
    """Read attribute or mapping key."""
    if value is None:
        return fallback

    if isinstance(value, Mapping):
        return _get_first(value, *names, fallback=fallback)

    for name in names:
        try:
            if hasattr(value, name):
                result = getattr(value, name)
                if result is not None:
                    return result
        except Exception:
            continue

    # Try camel/snake conversions against serialized dict.
    try:
        data = _as_dict(value)
        return _get_first(data, *names, fallback=fallback)
    except Exception:
        return fallback


def _coerce_string(value: Any, *, fallback: str = "") -> str:
    """Coerce value to stripped string."""
    if value is None:
        return fallback

    try:
        text = str(value).strip()
    except Exception:
        return fallback

    return text or fallback


def _coerce_int_or_none(value: Any) -> int | None:
    """Coerce value to int or None."""
    if value is None:
        return None

    try:
        return int(value)
    except Exception:
        return None


def _route_hints(project_id: str | None, world_id: str | None, *, api_prefix: str = "") -> dict[str, str]:
    """Build route hints without importing models helpers."""
    if not project_id or not world_id:
        return {}

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


def _build_chunk_context_key(
    *,
    project_id: str | None,
    universe_id: str | None,
    world_id: str | None,
    chunk_key: str | None,
) -> str | None:
    """Build chunk context key if all parts are present."""
    if not project_id or not universe_id or not world_id or not chunk_key:
        return None

    return f"{project_id}:{universe_id}:{world_id}:{chunk_key}"


# -----------------------------------------------------------------------------
# Cell/palette serializers
# -----------------------------------------------------------------------------

def serialize_cell_encoding() -> dict[str, Any]:
    """Return the required cell encoding contract."""
    return {
        "version": CELL_ENCODING_VERSION,
        "airCellValue": DEFAULT_AIR_CELL_VALUE,
        "blockCellValueRule": "paletteIndex + 1",
    }


def serialize_air_block() -> dict[str, Any]:
    """Serialize invariant Air pseudo-block."""
    return {
        "cellValue": DEFAULT_AIR_CELL_VALUE,
        "blockTypeId": None,
        "label": "Air",
        "solid": False,
        "opaque": False,
        "placeable": False,
        "breakable": False,
        "selectable": False,
        "collidable": False,
    }


def _normalize_palette_entry(entry: Any, index: int | None = None) -> dict[str, Any]:
    """Normalize one palette/block entry."""
    data = _as_dict(entry)

    palette_index = _get_first(
        data,
        "paletteIndex",
        "palette_index",
        "computedPaletteIndex",
        "computed_palette_index",
        fallback=index,
    )

    try:
        palette_index_int = int(palette_index) if palette_index is not None else index
    except Exception:
        palette_index_int = index

    cell_value = _get_first(
        data,
        "cellValue",
        "cell_value",
        fallback=(palette_index_int + 1 if palette_index_int is not None else None),
    )

    cell_value_int = _coerce_int_or_none(cell_value)

    return make_json_safe(
        {
            "paletteIndex": palette_index_int,
            "cellValue": cell_value_int,
            "blockTypeId": _get_first(data, "blockTypeId", "block_type_id", "typeId", "id"),
            "label": _get_first(data, "label", "name", fallback=None),
            "category": _get_first(data, "category", fallback=None),
            "solid": _get_first(data, "solid", fallback=None),
            "opaque": _get_first(data, "opaque", fallback=None),
            "placeable": _get_first(data, "placeable", fallback=None),
            "breakable": _get_first(data, "breakable", fallback=None),
            "selectable": _get_first(data, "selectable", fallback=None),
            "collidable": _get_first(data, "collidable", fallback=None),
            "renderMode": _get_first(data, "renderMode", "render_mode", fallback=None),
            "shapeType": _get_first(data, "shapeType", "shape_type", fallback=None),
            "materialId": _get_first(data, "materialId", "material_id", fallback=None),
            "textureId": _get_first(data, "textureId", "texture_id", fallback=None),
            "iconId": _get_first(data, "iconId", "icon_id", fallback=None),
            "registryId": _get_first(data, "registryId", "registry_id", fallback=None),
            "registryVersion": _get_first(data, "registryVersion", "registry_version", fallback=None),
            "metadata": deep_copy_json(_get_first(data, "metadata", "metadataJson", "metadata_json", fallback={})),
        }
    )


def serialize_palette(palette: Any) -> list[dict[str, Any]]:
    """
    Normalize a palette list.

    Accepts:
    - list/tuple of entries
    - dict with `blocks` or `palette`
    - dict with nested `blocks.blocks`
    """
    if palette is None:
        return []

    if isinstance(palette, Mapping):
        items = _get_first(palette, "blocks", "palette", fallback=[])

        if isinstance(items, Mapping):
            items = _get_first(items, "blocks", "palette", fallback=[])
    else:
        items = palette

    if not isinstance(items, (list, tuple)):
        return []

    return [
        _normalize_palette_entry(entry, index)
        for index, entry in enumerate(items)
    ]


# -----------------------------------------------------------------------------
# Project / universe / world serializers
# -----------------------------------------------------------------------------

def serialize_project(project: Any) -> dict[str, Any]:
    """Serialize project context or Project model."""
    data = _as_dict(project)

    return make_json_safe(
        {
            "projectId": _get_first(data, "projectId", "project_id"),
            "slug": _get_first(data, "slug"),
            "name": _get_first(data, "name"),
            "description": _get_first(data, "description"),
            "defaultUniverseId": _get_first(data, "defaultUniverseId", "default_universe_id"),
            "status": _get_first(data, "status"),
            "revision": _get_first(data, "revision"),
            "ownerType": _get_first(data, "ownerType", "owner_type", fallback=None),
            "ownerId": _get_first(data, "ownerId", "owner_id", fallback=None),
            "createdByUserId": _get_first(data, "createdByUserId", "created_by_user_id", fallback=None),
            "updatedByUserId": _get_first(data, "updatedByUserId", "updated_by_user_id", fallback=None),
            "createdAt": _get_first(data, "createdAt", "created_at", fallback=None),
            "updatedAt": _get_first(data, "updatedAt", "updated_at", fallback=None),
            "archivedAt": _get_first(data, "archivedAt", "archived_at", fallback=None),
            "deletedAt": _get_first(data, "deletedAt", "deleted_at", fallback=None),
            "metadata": deep_copy_json(_get_first(data, "metadata", "metadataJson", "metadata_json", fallback={})),
            "schemaVersion": _get_first(data, "schemaVersion", "schema_version", fallback=None),
            "flags": deep_copy_json(_get_first(data, "flags", fallback={})),
        }
    )


def serialize_universe(universe: Any) -> dict[str, Any]:
    """Serialize universe context or Universe model."""
    data = _as_dict(universe)

    return make_json_safe(
        {
            "universeId": _get_first(data, "universeId", "universe_id"),
            "projectId": _get_first(data, "projectId", "project_id"),
            "slug": _get_first(data, "slug"),
            "name": _get_first(data, "name"),
            "description": _get_first(data, "description"),
            "defaultWorldId": _get_first(data, "defaultWorldId", "default_world_id"),
            "spawnWorldId": _get_first(data, "spawnWorldId", "spawn_world_id"),
            "status": _get_first(data, "status"),
            "revision": _get_first(data, "revision"),
            "createdAt": _get_first(data, "createdAt", "created_at", fallback=None),
            "updatedAt": _get_first(data, "updatedAt", "updated_at", fallback=None),
            "archivedAt": _get_first(data, "archivedAt", "archived_at", fallback=None),
            "deletedAt": _get_first(data, "deletedAt", "deleted_at", fallback=None),
            "metadata": deep_copy_json(_get_first(data, "metadata", "metadataJson", "metadata_json", fallback={})),
            "schemaVersion": _get_first(data, "schemaVersion", "schema_version", fallback=None),
            "flags": deep_copy_json(_get_first(data, "flags", fallback={})),
        }
    )


def serialize_world_instance(
    world: Any,
    *,
    include_runtime: bool = True,
    include_editor: bool = True,
    include_metadata: bool = True,
    include_route_hints: bool = False,
    api_prefix: str = "",
) -> dict[str, Any]:
    """Serialize one concrete project world instance."""
    data = _as_dict(world)

    project_id = _get_first(data, "projectId", "project_id")
    world_id = _get_first(data, "worldId", "world_id")

    payload = {
        "worldId": world_id,
        "projectId": project_id,
        "universeId": _get_first(data, "universeId", "universe_id"),
        "slug": _get_first(data, "slug"),
        "name": _get_first(data, "name"),
        "description": _get_first(data, "description"),
        "worldType": _get_first(data, "worldType", "world_type"),
        "worldRole": _get_first(data, "worldRole", "world_role"),
        "worldScope": _get_first(data, "worldScope", "world_scope"),
        "ownerType": _get_first(data, "ownerType", "owner_type"),
        "ownerId": _get_first(data, "ownerId", "owner_id"),
        "templateId": _get_first(data, "templateId", "template_id"),
        "providerId": _get_first(data, "providerId", "provider_id"),
        "providerWorldId": _get_first(data, "providerWorldId", "provider_world_id"),
        "generatorType": _get_first(data, "generatorType", "generator_type"),
        "generatorVersion": _get_first(data, "generatorVersion", "generator_version"),
        "projectionType": _get_first(data, "projectionType", "projection_type"),
        "topologyType": _get_first(data, "topologyType", "topology_type"),
        "coordinateSystem": _get_first(data, "coordinateSystem", "coordinate_system"),
        "chunkSize": _get_first(data, "chunkSize", "chunk_size"),
        "cellSize": _get_first(data, "cellSize", "cell_size"),
        "surfaceY": _get_first(data, "surfaceY", "surface_y"),
        "minY": _get_first(data, "minY", "min_y"),
        "maxY": _get_first(data, "maxY", "max_y"),
        "seed": _get_first(data, "seed"),
        "blockRegistryId": _get_first(data, "blockRegistryId", "block_registry_id"),
        "blockRegistryVersion": _get_first(data, "blockRegistryVersion", "block_registry_version"),
        "status": _get_first(data, "status"),
        "revision": _get_first(data, "revision"),
        "spawn": deep_copy_json(_get_first(data, "spawn", fallback={})),
        "createdAt": _get_first(data, "createdAt", "created_at", fallback=None),
        "updatedAt": _get_first(data, "updatedAt", "updated_at", fallback=None),
        "archivedAt": _get_first(data, "archivedAt", "archived_at", fallback=None),
        "deletedAt": _get_first(data, "deletedAt", "deleted_at", fallback=None),
        "schemaVersion": _get_first(data, "schemaVersion", "schema_version", fallback=None),
        "worldContextKey": _get_first(data, "worldContextKey", "world_context_key", fallback=None),
        "flags": deep_copy_json(_get_first(data, "flags", fallback={})),
    }

    if include_runtime:
        payload["runtime"] = deep_copy_json(_get_first(data, "runtime", fallback={}))

    if include_editor:
        payload["editor"] = deep_copy_json(_get_first(data, "editor", fallback={}))

    if include_metadata:
        payload["metadata"] = deep_copy_json(_get_first(data, "metadata", "metadataJson", "metadata_json", fallback={}))

    if include_route_hints and project_id and world_id:
        payload["routeHints"] = _route_hints(
            project_id=str(project_id),
            world_id=str(world_id),
            api_prefix=api_prefix,
        )

    return make_json_safe(payload)


def serialize_world_runtime_context(
    context: Any,
    *,
    include_route_hints: bool = False,
    api_prefix: str = "",
) -> dict[str, Any]:
    """Serialize a resolved world runtime context."""
    data = _as_dict(context)

    project = _get_attr_or_key(context, "project", fallback=_get_first(data, "project", fallback={}))
    universe = _get_attr_or_key(context, "universe", fallback=_get_first(data, "universe", fallback={}))
    world = _get_attr_or_key(context, "world", fallback=_get_first(data, "world", fallback={}))

    project_payload = serialize_project(project)
    universe_payload = serialize_universe(universe)
    world_payload = serialize_world_instance(
        world,
        include_route_hints=include_route_hints,
        api_prefix=api_prefix,
    )

    project_id = (
        _get_attr_or_key(context, "project_id", "projectId")
        or project_payload.get("projectId")
    )
    universe_id = (
        _get_attr_or_key(context, "universe_id", "universeId")
        or universe_payload.get("universeId")
    )
    world_id = (
        _get_attr_or_key(context, "world_id", "worldId")
        or world_payload.get("worldId")
    )
    template_id = (
        _get_attr_or_key(context, "template_id", "templateId")
        or world_payload.get("templateId")
    )
    provider_world_id = (
        _get_attr_or_key(context, "provider_world_id", "providerWorldId")
        or world_payload.get("providerWorldId")
    )

    return make_json_safe(
        {
            "project": project_payload,
            "universe": universe_payload,
            "world": world_payload,
            "context": {
                "projectId": project_id,
                "universeId": universe_id,
                "worldId": world_id,
                "templateId": template_id,
                "providerWorldId": provider_world_id,
                "worldContextKey": _get_attr_or_key(context, "world_context_key", "worldContextKey", fallback=None),
            },
        }
    )


def serialize_world_instance_list(
    worlds: Sequence[Any],
    *,
    project: Any | None = None,
    universe: Any | None = None,
    include_route_hints: bool = False,
    api_prefix: str = "",
) -> dict[str, Any]:
    """Serialize a project-scoped world list response."""
    serialized_worlds = [
        serialize_world_instance(
            world,
            include_route_hints=include_route_hints,
            api_prefix=api_prefix,
        )
        for world in tuple(worlds or ())
    ]

    project_payload = serialize_project(project) if project is not None else None
    universe_payload = serialize_universe(universe) if universe is not None else None

    project_id = (
        project_payload.get("projectId")
        if project_payload
        else (serialized_worlds[0].get("projectId") if serialized_worlds else None)
    )
    universe_id = (
        universe_payload.get("universeId")
        if universe_payload
        else (serialized_worlds[0].get("universeId") if serialized_worlds else None)
    )

    return make_json_safe(
        {
            "ok": True,
            "responseVersion": WORLD_INSTANCE_LIST_RESPONSE_VERSION,
            "source": SERIALIZER_SOURCE,
            "projectId": project_id,
            "universeId": universe_id,
            "project": project_payload,
            "universe": universe_payload,
            "worlds": serialized_worlds,
            "counts": {
                "worlds": len(serialized_worlds),
            },
            "metadata": {
                "createdAt": utc_now_iso(),
                "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
                "projectScoped": True,
                "dbBacked": True,
            },
        }
    )


# -----------------------------------------------------------------------------
# Chunk serializers
# -----------------------------------------------------------------------------

def _extract_provider_chunk_dict(chunk_result: Any) -> dict[str, Any]:
    """Extract low-level provider/runtime chunk dictionary."""
    if WorldStateChunkResult is not None and isinstance(chunk_result, WorldStateChunkResult):
        return chunk_result.provider_chunk_to_dict()

    if isinstance(chunk_result, Mapping):
        if "providerChunk" in chunk_result and isinstance(chunk_result["providerChunk"], Mapping):
            return dict(make_json_safe(chunk_result["providerChunk"]))
        if "chunk" in chunk_result and isinstance(chunk_result["chunk"], Mapping):
            return dict(make_json_safe(chunk_result["chunk"]))
        return dict(make_json_safe(chunk_result))

    provider_chunk_to_dict = getattr(chunk_result, "provider_chunk_to_dict", None)
    if callable(provider_chunk_to_dict):
        try:
            return dict(make_json_safe(provider_chunk_to_dict()))
        except Exception:
            pass

    provider_chunk = _get_attr_or_key(chunk_result, "provider_chunk", "providerChunk", fallback=None)
    if provider_chunk is not None:
        return _as_dict(provider_chunk)

    return _as_dict(chunk_result)


def _get_chunk_result_context(chunk_result: Any) -> Any:
    """Extract context from a chunk result."""
    return _get_attr_or_key(chunk_result, "context", fallback={})


def _get_chunk_result_coordinates(chunk_result: Any) -> Any:
    """Extract coordinates from chunk result."""
    return _get_attr_or_key(chunk_result, "coordinates", fallback={})


def _chunk_result_attr(chunk_result: Any, *names: str, fallback: Any = None) -> Any:
    """Read attr/key from chunk result."""
    return _get_attr_or_key(chunk_result, *names, fallback=fallback)


def _normalize_provider_chunk_payload(
    chunk_result: Any,
    *,
    include_provider_raw: bool = False,
) -> dict[str, Any]:
    """
    Normalize provider/snapshot chunk payload and overwrite context fields with
    concrete project-world state.
    """
    provider_chunk = _extract_provider_chunk_dict(chunk_result)
    context = _get_chunk_result_context(chunk_result)
    coordinates = _get_chunk_result_coordinates(chunk_result)

    project_id = (
        _chunk_result_attr(chunk_result, "project_id", "projectId")
        or _get_attr_or_key(context, "project_id", "projectId")
        or _get_first(provider_chunk, "projectId", "project_id")
    )
    universe_id = (
        _chunk_result_attr(chunk_result, "universe_id", "universeId")
        or _get_attr_or_key(context, "universe_id", "universeId")
        or _get_first(provider_chunk, "universeId", "universe_id")
    )
    world_id = (
        _chunk_result_attr(chunk_result, "world_id", "worldId")
        or _get_attr_or_key(context, "world_id", "worldId")
        or _get_first(provider_chunk, "worldId", "world_id")
    )
    template_id = (
        _chunk_result_attr(chunk_result, "template_id", "templateId")
        or _get_attr_or_key(context, "template_id", "templateId")
        or _get_first(provider_chunk, "templateId", "template_id")
    )
    provider_world_id = (
        _chunk_result_attr(chunk_result, "provider_world_id", "providerWorldId")
        or _get_attr_or_key(context, "provider_world_id", "providerWorldId")
        or _get_first(provider_chunk, "providerWorldId", "provider_world_id")
    )

    provider_source_world_id = _get_first(
        provider_chunk,
        "providerSourceWorldId",
        "provider_source_world_id",
        "sourceWorldId",
        "worldId",
        "world_id",
        fallback=provider_world_id,
    )

    chunk_x = (
        _get_attr_or_key(coordinates, "chunk_x", "chunkX")
        or _get_first(provider_chunk, "chunkX", "chunk_x")
    )
    chunk_y = (
        _get_attr_or_key(coordinates, "chunk_y", "chunkY")
        or _get_first(provider_chunk, "chunkY", "chunk_y")
    )
    chunk_z = (
        _get_attr_or_key(coordinates, "chunk_z", "chunkZ")
        or _get_first(provider_chunk, "chunkZ", "chunk_z")
    )
    chunk_key = (
        _chunk_result_attr(chunk_result, "chunk_key", "chunkKey")
        or _get_attr_or_key(coordinates, "chunk_key", "chunkKey")
        or _get_first(provider_chunk, "chunkKey", "chunk_key")
    )

    if chunk_key is None and chunk_x is not None and chunk_y is not None and chunk_z is not None:
        chunk_key = f"{int(chunk_x)}:{int(chunk_y)}:{int(chunk_z)}"

    chunk_payload = copy.deepcopy(provider_chunk)

    # Productive context overrides.
    chunk_payload["projectId"] = project_id
    chunk_payload["universeId"] = universe_id
    chunk_payload["worldId"] = world_id
    chunk_payload["templateId"] = template_id
    chunk_payload["providerWorldId"] = provider_world_id
    chunk_payload["providerSourceWorldId"] = provider_source_world_id

    chunk_payload["chunkX"] = _coerce_int_or_none(chunk_x)
    chunk_payload["chunkY"] = _coerce_int_or_none(chunk_y)
    chunk_payload["chunkZ"] = _coerce_int_or_none(chunk_z)
    chunk_payload["chunkKey"] = chunk_key
    chunk_payload["chunkContextKey"] = (
        _chunk_result_attr(chunk_result, "chunk_context_key", "chunkContextKey")
        or _build_chunk_context_key(
            project_id=project_id,
            universe_id=universe_id,
            world_id=world_id,
            chunk_key=chunk_key,
        )
    )

    chunk_payload.setdefault("source", _chunk_result_attr(chunk_result, "source", fallback=chunk_payload.get("source")))
    chunk_payload.setdefault("runtimeContentVersion", RUNTIME_CHUNK_CONTENT_VERSION)
    chunk_payload.setdefault("cellIndexOrder", DEFAULT_CELL_INDEX_ORDER)
    chunk_payload.setdefault("airCellValue", DEFAULT_AIR_CELL_VALUE)
    chunk_payload.setdefault("cellEncoding", serialize_cell_encoding())

    chunk_payload["worldContext"] = {
        "projectId": project_id,
        "universeId": universe_id,
        "worldId": world_id,
        "templateId": template_id,
        "providerWorldId": provider_world_id,
        "providerSourceWorldId": provider_source_world_id,
    }

    existing_metadata = _get_first(chunk_payload, "metadata", fallback={})
    chunk_payload["metadata"] = deep_copy_json(existing_metadata if isinstance(existing_metadata, Mapping) else {})
    chunk_payload["metadata"].update(
        {
            "worldStateSource": SERIALIZER_SOURCE,
            "serializedAt": utc_now_iso(),
            "productiveWorldId": world_id,
            "providerWorldId": provider_world_id,
            "providerSourceWorldId": provider_source_world_id,
            "snapshotBacked": _chunk_result_attr(chunk_result, "source", fallback="") == "snapshot",
        }
    )

    if include_provider_raw:
        chunk_payload["providerRaw"] = provider_chunk

    return make_json_safe(chunk_payload)


def serialize_world_state_chunk(
    chunk_result: Any,
    *,
    include_context: bool = False,
    include_provider_resolution: bool = False,
    include_provider_raw: bool = False,
) -> dict[str, Any]:
    """Serialize only the `chunk` object for a project-scoped chunk response."""
    try:
        chunk_payload = _normalize_provider_chunk_payload(
            chunk_result,
            include_provider_raw=include_provider_raw,
        )

        if include_context:
            chunk_payload["resolvedContext"] = serialize_world_runtime_context(
                _get_chunk_result_context(chunk_result),
            )

        if include_provider_resolution:
            provider_resolution = _chunk_result_attr(
                chunk_result,
                "provider_resolution",
                "providerResolution",
                fallback=None,
            )
            chunk_payload["providerResolution"] = (
                provider_resolution.to_dict(include_definition=False)
                if provider_resolution is not None and hasattr(provider_resolution, "to_dict")
                else make_json_safe(provider_resolution)
            )

        return chunk_payload

    except Exception as exc:
        raise WorldStateSerializationError(
            "Could not serialize world-state chunk.",
            details={
                "projectId": _chunk_result_attr(chunk_result, "project_id", "projectId", fallback=None),
                "universeId": _chunk_result_attr(chunk_result, "universe_id", "universeId", fallback=None),
                "worldId": _chunk_result_attr(chunk_result, "world_id", "worldId", fallback=None),
                "chunkKey": _chunk_result_attr(chunk_result, "chunk_key", "chunkKey", fallback=None),
                "error": _safe_exception_message(exc),
            },
            cause=exc if isinstance(exc, BaseException) else None,
        ) from exc


def serialize_world_state_chunk_response(
    chunk_result: Any,
    *,
    include_context: bool = False,
    include_provider_resolution: bool = False,
    include_provider_raw: bool = False,
) -> dict[str, Any]:
    """
    Serialize a project-scoped single chunk API response.

    Intended for:
        GET /projects/<projectId>/worlds/<worldId>/chunks
    """
    try:
        chunk = serialize_world_state_chunk(
            chunk_result,
            include_context=include_context,
            include_provider_resolution=include_provider_resolution,
            include_provider_raw=include_provider_raw,
        )

        source = _chunk_result_attr(chunk_result, "source", fallback=chunk.get("source"))

        return make_json_safe(
            {
                "ok": True,
                "responseVersion": WORLD_STATE_CHUNK_RESPONSE_VERSION,
                "source": SERIALIZER_SOURCE,
                "projectId": chunk.get("projectId"),
                "universeId": chunk.get("universeId"),
                "worldId": chunk.get("worldId"),
                "templateId": chunk.get("templateId"),
                "providerWorldId": chunk.get("providerWorldId"),
                "chunkKey": chunk.get("chunkKey"),
                "chunkContextKey": chunk.get("chunkContextKey"),
                "chunk": chunk,
                "metadata": {
                    "createdAt": utc_now_iso(),
                    "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
                    "projectScoped": True,
                    "providerBacked": source != "snapshot",
                    "snapshotBacked": source == "snapshot",
                    "dbBacked": True,
                },
            }
        )

    except Exception as exc:
        if isinstance(exc, WorldStateSerializationError):
            raise
        raise WorldStateSerializationError(
            "Could not serialize world-state chunk response.",
            details={
                "error": _safe_exception_message(exc),
            },
            cause=exc if isinstance(exc, BaseException) else None,
        ) from exc


def serialize_world_state_chunk_batch_response(
    batch_result: Any,
    *,
    include_context: bool = False,
    include_provider_resolution: bool = False,
    include_provider_raw: bool = False,
) -> dict[str, Any]:
    """
    Serialize a project-scoped chunk batch API response.

    Intended for:
        POST /projects/<projectId>/worlds/<worldId>/chunks/batch
    """
    try:
        chunks_in = tuple(_get_attr_or_key(batch_result, "chunks", fallback=()) or ())
        requested_in = tuple(_get_attr_or_key(batch_result, "requested", fallback=()) or ())
        errors_in = tuple(_get_attr_or_key(batch_result, "errors", fallback=()) or ())
        context = _get_attr_or_key(batch_result, "context", fallback={})

        chunks = [
            serialize_world_state_chunk(
                chunk_result,
                include_context=include_context,
                include_provider_resolution=include_provider_resolution,
                include_provider_raw=include_provider_raw,
            )
            for chunk_result in chunks_in
        ]

        requested = []
        for coords in requested_in:
            if hasattr(coords, "to_dict") and callable(coords.to_dict):
                requested.append(make_json_safe(coords.to_dict()))
            else:
                requested.append(make_json_safe(coords))

        ok = bool(_get_attr_or_key(batch_result, "ok", fallback=(len(errors_in) == 0)))

        project_id = _get_attr_or_key(batch_result, "project_id", "projectId") or _get_attr_or_key(context, "project_id", "projectId")
        universe_id = _get_attr_or_key(batch_result, "universe_id", "universeId") or _get_attr_or_key(context, "universe_id", "universeId")
        world_id = _get_attr_or_key(batch_result, "world_id", "worldId") or _get_attr_or_key(context, "world_id", "worldId")
        template_id = _get_attr_or_key(context, "template_id", "templateId")
        provider_world_id = _get_attr_or_key(context, "provider_world_id", "providerWorldId")

        metadata = deep_copy_json(_get_attr_or_key(batch_result, "metadata", fallback={}) or {})

        snapshot_count = sum(1 for item in chunks if item.get("source") == "snapshot")
        generated_count = sum(1 for item in chunks if item.get("source") != "snapshot")

        return make_json_safe(
            {
                "ok": ok,
                "responseVersion": WORLD_STATE_CHUNK_BATCH_RESPONSE_VERSION,
                "source": SERIALIZER_SOURCE,
                "projectId": project_id,
                "universeId": universe_id,
                "worldId": world_id,
                "templateId": template_id,
                "providerWorldId": provider_world_id,
                "requested": requested,
                "chunks": chunks,
                "errors": [deep_copy_json(error) for error in errors_in],
                "counts": {
                    "requested": len(requested),
                    "chunks": len(chunks),
                    "errors": len(errors_in),
                    "snapshots": snapshot_count,
                    "generated": generated_count,
                },
                "metadata": {
                    "createdAt": utc_now_iso(),
                    "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
                    "projectScoped": True,
                    "providerBacked": generated_count > 0,
                    "snapshotBacked": snapshot_count > 0,
                    "dbBacked": True,
                    **metadata,
                },
            }
        )

    except Exception as exc:
        raise WorldStateSerializationError(
            "Could not serialize world-state chunk batch response.",
            details={
                "projectId": _get_attr_or_key(batch_result, "project_id", "projectId", fallback=None),
                "universeId": _get_attr_or_key(batch_result, "universe_id", "universeId", fallback=None),
                "worldId": _get_attr_or_key(batch_result, "world_id", "worldId", fallback=None),
                "error": _safe_exception_message(exc),
            },
            cause=exc if isinstance(exc, BaseException) else None,
        ) from exc


# -----------------------------------------------------------------------------
# Blocks/world metadata/bootstrap serializers
# -----------------------------------------------------------------------------

def serialize_world_state_blocks_response(
    result: Any,
    *,
    include_context: bool = False,
    include_provider_resolution: bool = False,
) -> dict[str, Any]:
    """Serialize project-scoped block/palette response."""
    try:
        context = _get_attr_or_key(result, "context", fallback={})
        raw_blocks = deep_copy_json(_get_attr_or_key(result, "blocks_payload", "blocksPayload", fallback={}))

        # New DB service already returns normalized payload; keep it intact.
        palette = serialize_palette(raw_blocks)

        registry_id = (
            _get_first(raw_blocks, "registryId", "registry_id", "blockRegistryId")
            or _get_attr_or_key(context, "world.block_registry_id", fallback=None)
            or _get_attr_or_key(_get_attr_or_key(context, "world", fallback={}), "block_registry_id", "blockRegistryId")
        )
        registry_version = (
            _get_first(raw_blocks, "registryVersion", "registry_version", "blockRegistryVersion")
            or _get_attr_or_key(_get_attr_or_key(context, "world", fallback={}), "block_registry_version", "blockRegistryVersion")
        )

        blocks_payload = {
            "air": _get_first(raw_blocks, "air", fallback=serialize_air_block()),
            "encoding": _get_first(
                raw_blocks,
                "encoding",
                fallback={
                    "airCellValue": DEFAULT_AIR_CELL_VALUE,
                    "blockCellValueRule": "paletteIndex + 1",
                    "cellEncodingVersion": CELL_ENCODING_VERSION,
                },
            ),
            "registry": _get_first(raw_blocks, "registry", fallback={}),
            "registryId": registry_id,
            "registryVersion": registry_version,
            "blocks": palette,
            "palette": palette,
            "raw": raw_blocks,
        }

        project_id = _get_attr_or_key(context, "project_id", "projectId")
        universe_id = _get_attr_or_key(context, "universe_id", "universeId")
        world_id = _get_attr_or_key(context, "world_id", "worldId")
        template_id = _get_attr_or_key(context, "template_id", "templateId")
        provider_world_id = _get_attr_or_key(context, "provider_world_id", "providerWorldId")

        payload = {
            "ok": True,
            "responseVersion": WORLD_STATE_BLOCKS_RESPONSE_VERSION,
            "source": SERIALIZER_SOURCE,
            "projectId": project_id,
            "universeId": universe_id,
            "worldId": world_id,
            "templateId": template_id,
            "providerWorldId": provider_world_id,
            "blockRegistryId": registry_id,
            "blockRegistryVersion": registry_version,
            "blocks": blocks_payload,
            "metadata": {
                "createdAt": utc_now_iso(),
                "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
                "projectScoped": True,
                "dbBacked": True,
                **deep_copy_json(_get_attr_or_key(result, "metadata", fallback={}) or {}),
            },
        }

        if include_context:
            payload["context"] = serialize_world_runtime_context(context)

        if include_provider_resolution:
            provider_resolution = _get_attr_or_key(result, "provider_resolution", "providerResolution", fallback=None)
            payload["providerResolution"] = (
                provider_resolution.to_dict(include_definition=False)
                if provider_resolution is not None and hasattr(provider_resolution, "to_dict")
                else make_json_safe(provider_resolution)
            )

        return make_json_safe(payload)

    except Exception as exc:
        raise WorldStateSerializationError(
            "Could not serialize world-state blocks response.",
            details={
                "error": _safe_exception_message(exc),
            },
            cause=exc if isinstance(exc, BaseException) else None,
        ) from exc


def serialize_world_state_world_metadata_response(
    result: Any,
    *,
    include_context: bool = True,
    include_provider_resolution: bool = False,
) -> dict[str, Any]:
    """Serialize one project-scoped world metadata response."""
    try:
        context = _get_attr_or_key(result, "context", fallback={})
        provider_world_metadata = deep_copy_json(
            _get_attr_or_key(result, "provider_world_metadata", "providerWorldMetadata", fallback={})
        )

        project = _get_attr_or_key(context, "project", fallback={})
        universe = _get_attr_or_key(context, "universe", fallback={})
        world = _get_attr_or_key(context, "world", fallback={})

        project_id = _get_attr_or_key(context, "project_id", "projectId")
        universe_id = _get_attr_or_key(context, "universe_id", "universeId")
        world_id = _get_attr_or_key(context, "world_id", "worldId")
        template_id = _get_attr_or_key(context, "template_id", "templateId")
        provider_world_id = _get_attr_or_key(context, "provider_world_id", "providerWorldId")

        payload = {
            "ok": True,
            "responseVersion": WORLD_STATE_WORLD_METADATA_RESPONSE_VERSION,
            "source": SERIALIZER_SOURCE,
            "projectId": project_id,
            "universeId": universe_id,
            "worldId": world_id,
            "templateId": template_id,
            "providerWorldId": provider_world_id,
            "world": serialize_world_instance(world),
            "providerWorldMetadata": provider_world_metadata,
            "metadata": {
                "createdAt": utc_now_iso(),
                "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
                "projectScoped": True,
                "dbBacked": True,
                **deep_copy_json(_get_attr_or_key(result, "metadata", fallback={}) or {}),
            },
        }

        if include_context:
            payload["project"] = serialize_project(project)
            payload["universe"] = serialize_universe(universe)
            payload["context"] = serialize_world_runtime_context(context)

        if include_provider_resolution:
            provider_resolution = _get_attr_or_key(result, "provider_resolution", "providerResolution", fallback=None)
            payload["providerResolution"] = (
                provider_resolution.to_dict(include_definition=False)
                if provider_resolution is not None and hasattr(provider_resolution, "to_dict")
                else make_json_safe(provider_resolution)
            )

        return make_json_safe(payload)

    except Exception as exc:
        raise WorldStateSerializationError(
            "Could not serialize world metadata response.",
            details={
                "error": _safe_exception_message(exc),
            },
            cause=exc if isinstance(exc, BaseException) else None,
        ) from exc


def serialize_project_bootstrap(
    bootstrap: Any,
    *,
    include_route_hints: bool = True,
    include_worlds: bool = True,
    include_metadata: bool = True,
    api_prefix: str = "",
) -> dict[str, Any]:
    """
    Serialize project bootstrap context.

    Intended response for:
        GET /projects/<projectId>/bootstrap
    """
    try:
        if isinstance(bootstrap, Mapping):
            data = dict(bootstrap)
            project = _get_first(data, "project", fallback={})
            universe = _get_first(data, "universe", fallback={})
            default_world = _get_first(data, "defaultWorld", "default_world", fallback={})
            spawn_world = _get_first(data, "spawnWorld", "spawn_world", fallback=default_world)
            worlds = tuple(_get_first(data, "worlds", fallback=[]))
            route_hints = deep_copy_json(_get_first(data, "routeHints", "route_hints", fallback={}))
            metadata = deep_copy_json(_get_first(data, "metadata", fallback={}))
        else:
            project = _get_attr_or_key(bootstrap, "project", fallback={})
            universe = _get_attr_or_key(bootstrap, "universe", fallback={})
            default_world = _get_attr_or_key(bootstrap, "default_world", "defaultWorld", fallback={})
            spawn_world = _get_attr_or_key(bootstrap, "spawn_world", "spawnWorld", fallback=default_world)
            worlds = tuple(_get_attr_or_key(bootstrap, "worlds", fallback=()) or ())
            route_hints = deep_copy_json(_get_attr_or_key(bootstrap, "route_hints", "routeHints", fallback={}))
            metadata = deep_copy_json(_get_attr_or_key(bootstrap, "metadata", fallback={}) or {})

        project_payload = serialize_project(project)
        universe_payload = serialize_universe(universe)
        default_world_payload = serialize_world_instance(default_world)
        spawn_world_payload = serialize_world_instance(spawn_world)

        project_id = project_payload.get("projectId")
        universe_id = universe_payload.get("universeId")
        default_world_id = default_world_payload.get("worldId")
        spawn_world_id = spawn_world_payload.get("worldId")

        if include_route_hints and not route_hints and project_id and spawn_world_id:
            route_hints = _route_hints(
                project_id=str(project_id),
                world_id=str(spawn_world_id),
                api_prefix=api_prefix,
            )

        if include_route_hints:
            spawn_world_payload["routeHints"] = route_hints

        serialized_worlds = (
            [
                serialize_world_instance(
                    world,
                    include_route_hints=include_route_hints,
                    api_prefix=api_prefix,
                )
                for world in worlds
            ]
            if include_worlds
            else []
        )

        payload = {
            "ok": True,
            "responseVersion": PROJECT_BOOTSTRAP_RESPONSE_VERSION,
            "source": SERIALIZER_SOURCE,
            "projectId": project_id,
            "universeId": universe_id,
            "defaultWorldId": default_world_id,
            "spawnWorldId": spawn_world_id,
            "project": project_payload,
            "universe": universe_payload,
            "defaultWorld": default_world_payload,
            "spawnWorld": spawn_world_payload,
            "world": spawn_world_payload,
            "worlds": serialized_worlds,
            "routeHints": route_hints if include_route_hints else {},
            "counts": {
                "worlds": len(serialized_worlds),
            },
            "metadata": {
                "createdAt": utc_now_iso(),
                "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
                "projectScoped": True,
                "dbBacked": True,
                "bootstrapRequired": True,
                "routingInvariant": (
                    "Editor opens project bootstrap, then loads concrete worldId. "
                    "Provider/template id remains separate."
                ),
            },
        }

        if include_metadata:
            payload["metadata"].update(metadata)

        return make_json_safe(payload)

    except Exception as exc:
        raise WorldStateSerializationError(
            "Could not serialize project bootstrap.",
            details={
                "error": _safe_exception_message(exc),
            },
            cause=exc if isinstance(exc, BaseException) else None,
        ) from exc


def serialize_project_bootstrap_response(
    bootstrap: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Alias for route readability."""
    return serialize_project_bootstrap(bootstrap, **kwargs)


# -----------------------------------------------------------------------------
# Generic response/error serializers
# -----------------------------------------------------------------------------

def serialize_error_response(
    error: BaseException | Any,
    *,
    include_debug: bool = False,
    include_private: bool = False,
) -> dict[str, Any]:
    """Serialize an error into the common API envelope."""
    body = error_to_api_response_body(
        error,
        include_debug=include_debug,
        include_private=include_private,
    )

    body["responseVersion"] = WORLD_STATE_ERROR_RESPONSE_VERSION
    body["source"] = SERIALIZER_SOURCE
    body["metadata"] = {
        "createdAt": utc_now_iso(),
        "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
    }

    return make_json_safe(body)


def serialize_error_response_tuple(
    error: BaseException | Any,
    *,
    include_debug: bool = False,
    include_private: bool = False,
) -> tuple[dict[str, Any], int]:
    """Return `(body, status_code)` for route code."""
    body, status_code = error_to_response_tuple(
        error,
        include_debug=include_debug,
        include_private=include_private,
    )

    body["responseVersion"] = WORLD_STATE_ERROR_RESPONSE_VERSION
    body["source"] = SERIALIZER_SOURCE
    body["metadata"] = {
        "createdAt": utc_now_iso(),
        "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
    }

    return make_json_safe(body), int(status_code)


def serialize_ok_response(
    *,
    response_version: str,
    payload: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a small generic ok response."""
    result = {
        "ok": True,
        "responseVersion": response_version,
        "source": SERIALIZER_SOURCE,
        "metadata": {
            "createdAt": utc_now_iso(),
            "worldStateSerializerVersion": SERIALIZER_MODULE_VERSION,
            **deep_copy_json(metadata or {}),
        },
    }

    if payload:
        result.update(deep_copy_json(payload))

    return make_json_safe(result)


def serialize_health_response(
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Lightweight serializer health response."""
    return serialize_ok_response(
        response_version="world-state-serializer-health.v1",
        payload={
            "service": "vectoplan-chunk",
            "component": "world_state.serializer",
            "moduleVersion": SERIALIZER_MODULE_VERSION,
            "cellEncoding": serialize_cell_encoding(),
            "dbBacked": True,
        },
        metadata=extra or {},
    )


def get_serializer_status(*, refresh: bool = False) -> dict[str, Any]:
    """Return serializer diagnostics."""
    global _serializer_status_cache

    with _serializer_cache_lock:
        if _serializer_status_cache is not None and not refresh:
            return copy.deepcopy(_serializer_status_cache)

        payload = {
            "ok": True,
            "source": SERIALIZER_SOURCE,
            "moduleVersion": SERIALIZER_MODULE_VERSION,
            "dbBacked": True,
            "responseVersions": {
                "projectBootstrap": PROJECT_BOOTSTRAP_RESPONSE_VERSION,
                "worldInstance": WORLD_INSTANCE_RESPONSE_VERSION,
                "worldInstanceList": WORLD_INSTANCE_LIST_RESPONSE_VERSION,
                "blocks": WORLD_STATE_BLOCKS_RESPONSE_VERSION,
                "chunk": WORLD_STATE_CHUNK_RESPONSE_VERSION,
                "chunkBatch": WORLD_STATE_CHUNK_BATCH_RESPONSE_VERSION,
                "worldMetadata": WORLD_STATE_WORLD_METADATA_RESPONSE_VERSION,
                "error": WORLD_STATE_ERROR_RESPONSE_VERSION,
            },
            "cellEncoding": serialize_cell_encoding(),
            "runtimeChunkContentVersion": RUNTIME_CHUNK_CONTENT_VERSION,
            "metadata": {
                "createdAt": utc_now_iso(),
            },
        }

        _serializer_status_cache = make_json_safe(payload)
        return copy.deepcopy(_serializer_status_cache)


def reset_serializer_status_cache() -> None:
    """Reset serializer diagnostics cache."""
    global _serializer_status_cache

    with _serializer_cache_lock:
        _serializer_status_cache = None


def export_serialized_json(
    value: Any,
    *,
    indent: int | None = 2,
) -> str:
    """Export a value as stable JSON."""
    return json.dumps(
        make_json_safe(value),
        sort_keys=True,
        ensure_ascii=False,
        indent=indent,
    )


__all__ = (
    "SERIALIZER_MODULE_VERSION",
    "SERIALIZER_SOURCE",
    "PROJECT_BOOTSTRAP_RESPONSE_VERSION",
    "WORLD_INSTANCE_RESPONSE_VERSION",
    "WORLD_INSTANCE_LIST_RESPONSE_VERSION",
    "WORLD_STATE_BLOCKS_RESPONSE_VERSION",
    "WORLD_STATE_CHUNK_RESPONSE_VERSION",
    "WORLD_STATE_CHUNK_BATCH_RESPONSE_VERSION",
    "WORLD_STATE_WORLD_METADATA_RESPONSE_VERSION",
    "WORLD_STATE_ERROR_RESPONSE_VERSION",
    "CELL_ENCODING_VERSION",
    "RUNTIME_CHUNK_CONTENT_VERSION",
    "DEFAULT_AIR_CELL_VALUE",
    "DEFAULT_CELL_INDEX_ORDER",
    "make_json_safe",
    "deep_copy_json",
    "utc_now_iso",
    "serialize_cell_encoding",
    "serialize_air_block",
    "serialize_palette",
    "serialize_project",
    "serialize_universe",
    "serialize_world_instance",
    "serialize_world_runtime_context",
    "serialize_world_instance_list",
    "serialize_world_state_chunk",
    "serialize_world_state_chunk_response",
    "serialize_world_state_chunk_batch_response",
    "serialize_world_state_blocks_response",
    "serialize_world_state_world_metadata_response",
    "serialize_project_bootstrap",
    "serialize_project_bootstrap_response",
    "serialize_error_response",
    "serialize_error_response_tuple",
    "serialize_ok_response",
    "serialize_health_response",
    "get_serializer_status",
    "reset_serializer_status_cache",
    "export_serialized_json",
    "get_error_status_code",
)