# services/vectoplan-chunk/src/world_state/service.py
"""
World-state service facade for the VECTOPLAN chunk service.

This module is the compatibility and service facade between:

    routes/*
    -> src.world_state.service
    -> PostgreSQL models
    -> src.world provider/template generation

Current persistent hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Provider/template hierarchy:

    WorldInstance(world_spawn)
      -> providerWorldId = flat
      -> src.world generates untouched chunks

Load rule:

    if ChunkSnapshot exists:
        -> snapshot is load-truth
    else:
        -> provider/template generator creates untouched chunk

Important:
- This file is not an HTTP adapter.
- This file does not execute SetBlock/RemoveBlock commands.
- This file does not write ChunkEvents.
- Commands are handled by routes/commands.py for the current slice.
- This file preserves the older world_state service interface while preferring
  PostgreSQL-backed Project / Universe / WorldInstance / ChunkSnapshot state.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence


try:
    from extensions import db
except Exception:  # pragma: no cover
    db = None  # type: ignore[assignment]


try:
    from models import (
        BlockRegistry,
        BlockType,
        ChunkSnapshot,
        Project,
        Universe,
        WorldInstance,
        get_model_debug_summary,
    )
except Exception:  # pragma: no cover
    BlockRegistry = None  # type: ignore[assignment]
    BlockType = None  # type: ignore[assignment]
    ChunkSnapshot = None  # type: ignore[assignment]
    Project = None  # type: ignore[assignment]
    Universe = None  # type: ignore[assignment]
    WorldInstance = None  # type: ignore[assignment]

    def get_model_debug_summary() -> dict[str, Any]:  # type: ignore[no-redef]
        return {
            "ready": False,
            "error": "models package could not be imported",
        }


from .errors import (
    InvalidWorldStatePayloadError,
    ProviderWorldResolutionError,
    WorldStateProviderError,
    WorldStateError,
    coerce_world_state_error,
    make_json_safe,
    raise_for_missing_project_id,
    raise_for_missing_world_id,
)

try:
    from .resolver import (
        ProviderWorldResolution,
        WorldStateResolver,
        get_default_world_state_resolver,
        reset_default_world_state_resolver_cache,
    )
except Exception:  # pragma: no cover
    ProviderWorldResolution = None  # type: ignore[assignment]
    WorldStateResolver = None  # type: ignore[assignment]

    def get_default_world_state_resolver(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        return None

    def reset_default_world_state_resolver_cache(*args: Any, **kwargs: Any) -> None:  # type: ignore[no-redef]
        return None


SERVICE_MODULE_VERSION = "0.2.0"
SERVICE_SOURCE = "world_state.service"

CHUNK_LOAD_SOURCE_GENERATED = "generated"
CHUNK_LOAD_SOURCE_PROVIDER = "provider"
CHUNK_LOAD_SOURCE_SNAPSHOT = "snapshot"
CHUNK_LOAD_SOURCE_UNKNOWN = "unknown"

RUNTIME_CHUNK_CONTENT_VERSION = "runtime-chunk-content.v1"
CELL_ENCODING_VERSION = "cell-encoding.palette-index-plus-one.v1"
CELL_INDEX_ORDER = "x-fastest-y-then-z"
AIR_CELL_VALUE = 0
BLOCK_CELL_VALUE_RULE = "paletteIndex + 1"

DEFAULT_MAX_BATCH_CHUNKS = 256

_default_service_lock = threading.RLock()
_default_world_state_service_cache: "WorldStateService | None" = None


# -----------------------------------------------------------------------------
# Safe helpers
# -----------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Return UTC timestamp as ISO string."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def deep_copy_json(value: Any) -> Any:
    """JSON-safe deep copy."""
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


def _safe_object_to_dict(value: Any) -> dict[str, Any]:
    """
    Convert an arbitrary object to a JSON-safe dictionary.
    """
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(make_json_safe(value))

    if dataclasses.is_dataclass(value):
        try:
            return dict(make_json_safe(dataclasses.asdict(value)))
        except Exception:
            return {
                "type": value.__class__.__name__,
                "value": make_json_safe(value),
            }

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        for kwargs in (
            {},
            {"include_internal": False},
            {"includeInternal": False},
        ):
            try:
                result = to_dict(**kwargs)
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

    result: dict[str, Any] = {
        "type": value.__class__.__name__,
    }

    try:
        for key, item in vars(value).items():
            if not key.startswith("_"):
                result[key] = make_json_safe(item)
    except Exception:
        result["repr"] = repr(value)

    return result


def _safe_import_module(module_path: str) -> tuple[Any | None, str | None]:
    """Import a module defensively."""
    try:
        return importlib.import_module(module_path), None
    except Exception as exc:
        return None, _safe_exception_message(exc)


def _safe_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any | None, str | None]:
    """Call a function defensively."""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:
        return None, _safe_exception_message(exc)


def _get_first(data: Mapping[str, Any], *keys: str, fallback: Any = None) -> Any:
    """Read first available key from mapping."""
    for key in keys:
        try:
            if key in data:
                return data[key]
        except Exception:
            continue

    return fallback


def _coerce_int(value: Any, *, field_name: str) -> int:
    """Coerce value to int or raise InvalidWorldStatePayloadError."""
    if value is None or str(value).strip() == "":
        raise InvalidWorldStatePayloadError(
            f"{field_name} is required.",
            code=f"missing_{field_name}",
            details={"field": field_name, "value": value},
        )

    try:
        return int(value)
    except Exception as exc:
        raise InvalidWorldStatePayloadError(
            f"{field_name} must be an integer.",
            code=f"invalid_{field_name}",
            details={"field": field_name, "value": make_json_safe(value)},
            cause=exc,
        ) from exc


def _coerce_optional_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce bool-like values."""
    if value is None:
        return bool(default)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = str(value).strip().lower()

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False

    return bool(default)


def _build_chunk_key(chunk_x: int, chunk_y: int, chunk_z: int) -> str:
    """Build canonical chunk key."""
    return f"{int(chunk_x)}:{int(chunk_y)}:{int(chunk_z)}"


def _build_chunk_context_key(
    *,
    project_id: str,
    universe_id: str,
    world_id: str,
    chunk_key: str,
) -> str:
    """Build stable project-scoped chunk context key."""
    return f"{project_id}:{universe_id}:{world_id}:{chunk_key}"


def _require_db_models_ready() -> None:
    """Ensure DB extension and required model classes are available."""
    if db is None:
        raise RuntimeError("SQLAlchemy db extension is unavailable.")

    missing: list[str] = []

    for name, value in (
        ("Project", Project),
        ("Universe", Universe),
        ("WorldInstance", WorldInstance),
        ("BlockRegistry", BlockRegistry),
        ("BlockType", BlockType),
        ("ChunkSnapshot", ChunkSnapshot),
    ):
        if value is None:
            missing.append(name)

    if missing:
        raise RuntimeError(f"Required model classes are unavailable: {', '.join(missing)}.")


def _call_to_dict(
    obj: Any,
    *,
    include_internal: bool = False,
    include_metadata: bool = True,
    project_id: Optional[str] = None,
    universe_id: Optional[str] = None,
    world_id: Optional[str] = None,
) -> dict[str, Any]:
    """Call a model's to_dict() with several compatible signatures."""
    if obj is None:
        return {}

    to_dict = getattr(obj, "to_dict", None)

    if not callable(to_dict):
        return _safe_object_to_dict(obj)

    attempts: tuple[dict[str, Any], ...] = (
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
    )

    for kwargs in attempts:
        clean_kwargs = {
            key: value
            for key, value in kwargs.items()
            if value is not None
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
                "type": obj.__class__.__name__,
                "serializationError": _safe_exception_message(exc),
            }

    return _safe_object_to_dict(obj)


# -----------------------------------------------------------------------------
# Runtime context adapters
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ChunkCoordinates:
    """
    Normalized chunk coordinates for project-scoped chunk loading.
    """

    chunk_x: int
    chunk_y: int
    chunk_z: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunk_x", _coerce_int(self.chunk_x, field_name="chunkX"))
        object.__setattr__(self, "chunk_y", _coerce_int(self.chunk_y, field_name="chunkY"))
        object.__setattr__(self, "chunk_z", _coerce_int(self.chunk_z, field_name="chunkZ"))

    @property
    def chunkX(self) -> int:
        return self.chunk_x

    @property
    def chunkY(self) -> int:
        return self.chunk_y

    @property
    def chunkZ(self) -> int:
        return self.chunk_z

    @property
    def chunk_key(self) -> str:
        return _build_chunk_key(self.chunk_x, self.chunk_y, self.chunk_z)

    @property
    def chunkKey(self) -> str:
        return self.chunk_key

    def to_dict(self) -> dict[str, int | str]:
        return {
            "chunkX": self.chunk_x,
            "chunkY": self.chunk_y,
            "chunkZ": self.chunk_z,
            "chunkKey": self.chunk_key,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ChunkCoordinates":
        return cls(
            chunk_x=_get_first(data, "chunkX", "chunk_x", "x"),
            chunk_y=_get_first(data, "chunkY", "chunk_y", "y"),
            chunk_z=_get_first(data, "chunkZ", "chunk_z", "z"),
        )


@dataclass(frozen=True, slots=True)
class DbWorldRuntimeContext:
    """
    PostgreSQL-backed project/universe/world runtime context.
    """

    project: Any
    universe: Any
    world: Any
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))

    @property
    def project_id(self) -> str:
        return self.project.project_id

    @property
    def universe_id(self) -> str:
        return self.universe.universe_id

    @property
    def world_id(self) -> str:
        return self.world.world_id

    @property
    def template_id(self) -> str:
        return self.world.template_id

    @property
    def provider_id(self) -> str:
        return self.world.provider_id

    @property
    def provider_world_id(self) -> str:
        return self.world.provider_world_id

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def universeId(self) -> str:
        return self.universe_id

    @property
    def worldId(self) -> str:
        return self.world_id

    @property
    def templateId(self) -> str:
        return self.template_id

    @property
    def providerId(self) -> str:
        return self.provider_id

    @property
    def providerWorldId(self) -> str:
        return self.provider_world_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "universeId": self.universe_id,
            "worldId": self.world_id,
            "templateId": self.template_id,
            "providerId": self.provider_id,
            "providerWorldId": self.provider_world_id,
            "generatorType": self.world.generator_type,
            "generatorVersion": self.world.generator_version,
            "projectionType": self.world.projection_type,
            "topologyType": self.world.topology_type,
            "coordinateSystem": self.world.coordinate_system,
            "chunkSize": self.world.chunk_size,
            "cellSize": self.world.cell_size,
            "surfaceY": self.world.surface_y,
            "minY": self.world.min_y,
            "maxY": self.world.max_y,
            "blockRegistryId": self.world.block_registry_id,
            "blockRegistryVersion": self.world.block_registry_version,
            "project": _call_to_dict(self.project),
            "universe": _call_to_dict(self.universe, project_id=self.project_id),
            "world": _call_to_dict(
                self.world,
                project_id=self.project_id,
                universe_id=self.universe_id,
            ),
            "metadata": deep_copy_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DbProjectBootstrapContext:
    """
    PostgreSQL-backed project bootstrap context.
    """

    project: Any
    universe: Any
    default_world: Any
    spawn_world: Any
    worlds: tuple[Any, ...] = field(default_factory=tuple)
    route_hints: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "worlds", tuple(self.worlds or ()))
        object.__setattr__(self, "route_hints", deep_copy_json(self.route_hints or {}))
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))

    @property
    def project_id(self) -> str:
        return self.project.project_id

    @property
    def universe_id(self) -> str:
        return self.universe.universe_id

    @property
    def default_world_id(self) -> str:
        return self.default_world.world_id

    @property
    def spawn_world_id(self) -> str:
        return self.spawn_world.world_id

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def universeId(self) -> str:
        return self.universe_id

    @property
    def defaultWorldId(self) -> str:
        return self.default_world_id

    @property
    def spawnWorldId(self) -> str:
        return self.spawn_world_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "universeId": self.universe_id,
            "defaultWorldId": self.default_world_id,
            "spawnWorldId": self.spawn_world_id,
            "project": _call_to_dict(self.project),
            "universe": _call_to_dict(self.universe, project_id=self.project_id),
            "defaultWorld": _call_to_dict(
                self.default_world,
                project_id=self.project_id,
                universe_id=self.universe_id,
            ),
            "spawnWorld": _call_to_dict(
                self.spawn_world,
                project_id=self.project_id,
                universe_id=self.universe_id,
            ),
            "worlds": [
                _call_to_dict(
                    world,
                    project_id=self.project_id,
                    universe_id=self.universe_id,
                )
                for world in self.worlds
            ],
            "routeHints": deep_copy_json(self.route_hints),
            "metadata": deep_copy_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SimpleProviderWorldResolution:
    """
    Minimal provider-resolution object compatible with old serializers.
    """

    provider_world_id: str
    template_id: str | None = None
    project_id: str | None = None
    universe_id: str | None = None
    world_id: str | None = None
    available: bool = True
    provider_definition: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def providerWorldId(self) -> str:
        return self.provider_world_id

    @property
    def templateId(self) -> str | None:
        return self.template_id

    def to_dict(self, *, include_definition: bool = False) -> dict[str, Any]:
        payload = {
            "providerWorldId": self.provider_world_id,
            "templateId": self.template_id,
            "projectId": self.project_id,
            "universeId": self.universe_id,
            "worldId": self.world_id,
            "available": self.available,
            "metadata": deep_copy_json(self.metadata),
        }

        if include_definition:
            payload["providerDefinition"] = _safe_object_to_dict(self.provider_definition)

        return payload


# -----------------------------------------------------------------------------
# Result objects
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorldStateChunkResult:
    """
    Project-scoped chunk load result.
    """

    context: DbWorldRuntimeContext
    coordinates: ChunkCoordinates
    provider_resolution: Any | None
    provider_chunk: Any
    source: str = CHUNK_LOAD_SOURCE_UNKNOWN
    snapshot: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))
        object.__setattr__(self, "source", str(self.source or CHUNK_LOAD_SOURCE_UNKNOWN))

    @property
    def project_id(self) -> str:
        return self.context.project_id

    @property
    def universe_id(self) -> str:
        return self.context.universe_id

    @property
    def world_id(self) -> str:
        return self.context.world_id

    @property
    def template_id(self) -> str:
        return self.context.template_id

    @property
    def provider_world_id(self) -> str:
        return self.context.provider_world_id

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def universeId(self) -> str:
        return self.universe_id

    @property
    def worldId(self) -> str:
        return self.world_id

    @property
    def templateId(self) -> str:
        return self.template_id

    @property
    def providerWorldId(self) -> str:
        return self.provider_world_id

    @property
    def chunk_key(self) -> str:
        return self.coordinates.chunk_key

    @property
    def chunkKey(self) -> str:
        return self.chunk_key

    @property
    def chunk_context_key(self) -> str:
        return _build_chunk_context_key(
            project_id=self.project_id,
            universe_id=self.universe_id,
            world_id=self.world_id,
            chunk_key=self.chunk_key,
        )

    @property
    def chunkContextKey(self) -> str:
        return self.chunk_context_key

    def provider_chunk_to_dict(self) -> dict[str, Any]:
        return _safe_object_to_dict(self.provider_chunk)

    def to_dict(self, *, include_provider_chunk: bool = True) -> dict[str, Any]:
        payload = {
            "projectId": self.project_id,
            "universeId": self.universe_id,
            "worldId": self.world_id,
            "templateId": self.template_id,
            "providerWorldId": self.provider_world_id,
            "chunkX": self.coordinates.chunk_x,
            "chunkY": self.coordinates.chunk_y,
            "chunkZ": self.coordinates.chunk_z,
            "chunkKey": self.chunk_key,
            "chunkContextKey": self.chunk_context_key,
            "source": self.source,
            "context": self.context.to_dict(),
            "providerResolution": (
                self.provider_resolution.to_dict(include_definition=False)
                if self.provider_resolution is not None and hasattr(self.provider_resolution, "to_dict")
                else None
            ),
            "snapshot": (
                _call_to_dict(self.snapshot, include_internal=False, include_metadata=True)
                if self.snapshot is not None
                else None
            ),
            "metadata": deep_copy_json(self.metadata),
        }

        if include_provider_chunk:
            payload["providerChunk"] = self.provider_chunk_to_dict()

        return payload


@dataclass(frozen=True, slots=True)
class WorldStateChunkBatchResult:
    """
    Project-scoped batch chunk result.
    """

    context: DbWorldRuntimeContext
    chunks: tuple[WorldStateChunkResult, ...] = field(default_factory=tuple)
    requested: tuple[ChunkCoordinates, ...] = field(default_factory=tuple)
    errors: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunks", tuple(self.chunks or ()))
        object.__setattr__(self, "requested", tuple(self.requested or ()))
        object.__setattr__(
            self,
            "errors",
            tuple(deep_copy_json(error) for error in (self.errors or ())),
        )
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    @property
    def project_id(self) -> str:
        return self.context.project_id

    @property
    def universe_id(self) -> str:
        return self.context.universe_id

    @property
    def world_id(self) -> str:
        return self.context.world_id

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def universeId(self) -> str:
        return self.universe_id

    @property
    def worldId(self) -> str:
        return self.world_id

    def to_dict(self, *, include_provider_chunks: bool = True) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "projectId": self.project_id,
            "universeId": self.universe_id,
            "worldId": self.world_id,
            "templateId": self.context.template_id,
            "providerWorldId": self.context.provider_world_id,
            "requested": [coords.to_dict() for coords in self.requested],
            "chunks": [
                chunk.to_dict(include_provider_chunk=include_provider_chunks)
                for chunk in self.chunks
            ],
            "errors": [deep_copy_json(error) for error in self.errors],
            "counts": {
                "requested": len(self.requested),
                "chunks": len(self.chunks),
                "errors": len(self.errors),
            },
            "context": self.context.to_dict(),
            "metadata": deep_copy_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorldStateBlocksResult:
    """
    Project-scoped block/palette result for a concrete world instance.
    """

    context: DbWorldRuntimeContext
    blocks_payload: Mapping[str, Any]
    provider_resolution: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks_payload", deep_copy_json(self.blocks_payload or {}))
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.context.project_id,
            "universeId": self.context.universe_id,
            "worldId": self.context.world_id,
            "templateId": self.context.template_id,
            "providerWorldId": self.context.provider_world_id,
            "blocks": deep_copy_json(self.blocks_payload),
            "providerResolution": (
                self.provider_resolution.to_dict(include_definition=False)
                if self.provider_resolution is not None and hasattr(self.provider_resolution, "to_dict")
                else None
            ),
            "context": self.context.to_dict(),
            "metadata": deep_copy_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorldStateWorldMetadataResult:
    """
    Project-scoped world metadata result.
    """

    context: DbWorldRuntimeContext
    provider_world_metadata: Mapping[str, Any] = field(default_factory=dict)
    provider_resolution: Any | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_world_metadata",
            deep_copy_json(self.provider_world_metadata or {}),
        )
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.context.project_id,
            "universeId": self.context.universe_id,
            "worldId": self.context.world_id,
            "templateId": self.context.template_id,
            "providerWorldId": self.context.provider_world_id,
            "world": _call_to_dict(
                self.context.world,
                project_id=self.context.project_id,
                universe_id=self.context.universe_id,
            ),
            "project": _call_to_dict(self.context.project),
            "universe": _call_to_dict(
                self.context.universe,
                project_id=self.context.project_id,
            ),
            "providerWorldMetadata": deep_copy_json(self.provider_world_metadata),
            "providerResolution": (
                self.provider_resolution.to_dict(include_definition=False)
                if self.provider_resolution is not None and hasattr(self.provider_resolution, "to_dict")
                else None
            ),
            "metadata": deep_copy_json(self.metadata),
        }


# -----------------------------------------------------------------------------
# Chunk coordinate normalization
# -----------------------------------------------------------------------------

def normalize_chunk_coordinates(
    chunk_x: Any,
    chunk_y: Any,
    chunk_z: Any,
) -> ChunkCoordinates:
    """Normalize chunk coordinates from route query values."""
    return ChunkCoordinates(
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
    )


def normalize_chunk_coordinate_items(
    chunks: Sequence[Mapping[str, Any]] | Sequence[Any],
    *,
    max_count: int = DEFAULT_MAX_BATCH_CHUNKS,
) -> tuple[ChunkCoordinates, ...]:
    """
    Normalize batch chunk coordinates.

    Accepts entries like:
        { "chunkX": 0, "chunkY": 0, "chunkZ": 0 }

    Also accepts tuple/list entries:
        [0, 0, 0]
    """
    if chunks is None:
        raise InvalidWorldStatePayloadError(
            "chunks is required.",
            code="missing_chunks",
            details={"field": "chunks"},
        )

    try:
        items = list(chunks)
    except Exception as exc:
        raise InvalidWorldStatePayloadError(
            "chunks must be a list.",
            code="invalid_chunks",
            details={"value": make_json_safe(chunks)},
            cause=exc,
        ) from exc

    if len(items) > max_count:
        raise InvalidWorldStatePayloadError(
            "Too many chunks requested.",
            code="too_many_chunks",
            details={
                "count": len(items),
                "maxCount": max_count,
            },
        )

    normalized: list[ChunkCoordinates] = []
    seen: set[str] = set()

    for index, item in enumerate(items):
        try:
            if isinstance(item, Mapping):
                coords = ChunkCoordinates.from_mapping(item)
            elif isinstance(item, (list, tuple)) and len(item) == 3:
                coords = ChunkCoordinates(
                    chunk_x=item[0],
                    chunk_y=item[1],
                    chunk_z=item[2],
                )
            else:
                raise InvalidWorldStatePayloadError(
                    "Invalid chunk coordinate item.",
                    code="invalid_chunk_coordinate_item",
                    details={
                        "index": index,
                        "item": make_json_safe(item),
                    },
                )

            if coords.chunk_key not in seen:
                normalized.append(coords)
                seen.add(coords.chunk_key)

        except Exception as exc:
            raise InvalidWorldStatePayloadError(
                "Invalid chunk coordinate item.",
                code="invalid_chunk_coordinate_item",
                details={
                    "index": index,
                    "item": make_json_safe(item),
                },
                cause=exc if isinstance(exc, BaseException) else None,
            ) from exc

    return tuple(normalized)


# -----------------------------------------------------------------------------
# Provider adapter
# -----------------------------------------------------------------------------

def _looks_like_chunk_object(value: Any) -> bool:
    """Check whether an object looks like a provider chunk."""
    if value is None:
        return False

    if isinstance(value, Mapping):
        return any(
            key in value
            for key in (
                "chunkKey",
                "chunk_key",
                "cells",
                "palette",
                "chunkX",
                "chunk_x",
            )
        )

    return any(
        hasattr(value, attr)
        for attr in (
            "chunk_key",
            "chunkKey",
            "cells",
            "palette",
            "chunk_x",
            "chunkX",
        )
    )


class WorldProviderAdapter:
    """
    Defensive adapter around the existing src.world service.

    It avoids hard-coding one exact provider method signature.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._world_service: Any | None = None
        self._world_service_error: str | None = None
        self._module_diagnostics: dict[str, Any] | None = None

    def reset_cache(self) -> None:
        with self._lock:
            self._world_service = None
            self._world_service_error = None
            self._module_diagnostics = None

    def get_world_service(self, *, refresh: bool = False) -> Any:
        """Return src.world default service."""
        with self._lock:
            if self._world_service is not None and not refresh:
                return self._world_service

            module, import_error = _safe_import_module("src.world.service")

            diagnostics = {
                "module": "src.world.service",
                "imported": module is not None,
                "importError": import_error,
                "serviceCreated": False,
                "serviceError": None,
            }

            if module is None:
                self._world_service_error = import_error
                self._module_diagnostics = diagnostics
                raise WorldStateProviderError(
                    "Could not import src.world.service.",
                    details=diagnostics,
                )

            get_service = getattr(module, "get_default_world_service", None)

            if not callable(get_service):
                diagnostics["serviceError"] = "get_default_world_service_not_available"
                self._world_service_error = diagnostics["serviceError"]
                self._module_diagnostics = diagnostics
                raise WorldStateProviderError(
                    "src.world.service does not expose get_default_world_service.",
                    details=diagnostics,
                )

            service, service_error = _safe_call(get_service)

            diagnostics["serviceCreated"] = service is not None
            diagnostics["serviceError"] = service_error

            if service is None:
                self._world_service_error = service_error
                self._module_diagnostics = diagnostics
                raise WorldStateProviderError(
                    "Could not create src.world service.",
                    details=diagnostics,
                )

            self._world_service = service
            self._world_service_error = None
            self._module_diagnostics = diagnostics
            return service

    def get_status(self) -> dict[str, Any]:
        """Return provider adapter diagnostics."""
        with self._lock:
            return {
                "ok": self._world_service_error is None,
                "cached": self._world_service is not None,
                "error": self._world_service_error,
                "diagnostics": deep_copy_json(self._module_diagnostics or {}),
            }

    def get_world_metadata(self, provider_world_id: str) -> Mapping[str, Any]:
        """Load provider world metadata."""
        service = self.get_world_service()
        diagnostics: list[dict[str, Any]] = []

        for method_name in ("get_world_metadata", "get_world_definition", "load_world"):
            method = getattr(service, method_name, None)

            if not callable(method):
                diagnostics.append(
                    {
                        "method": method_name,
                        "ok": False,
                        "error": "method_not_available",
                    }
                )
                continue

            result, error = _safe_call(method, provider_world_id)
            diagnostics.append(
                {
                    "method": method_name,
                    "ok": result is not None,
                    "error": error,
                }
            )

            if result is not None:
                payload = _safe_object_to_dict(result)
                payload.setdefault("source", f"src.world.service.{method_name}")
                payload.setdefault("diagnostics", diagnostics)
                return payload

        raise WorldStateProviderError(
            "Could not load provider world metadata.",
            details={
                "providerWorldId": provider_world_id,
                "attempts": diagnostics,
            },
        )

    def generate_chunk(
        self,
        *,
        provider_world_id: str,
        chunk: ChunkCoordinates,
    ) -> Any:
        """
        Generate one chunk through src.world.
        """
        service = self.get_world_service()
        attempts: list[dict[str, Any]] = []

        method = getattr(service, "generate_chunk", None)

        if not callable(method):
            raise WorldStateProviderError(
                "src.world service does not expose generate_chunk.",
                details={
                    "providerWorldId": provider_world_id,
                    "chunk": chunk.to_dict(),
                },
            )

        call_variants: tuple[tuple[tuple[Any, ...], dict[str, Any], str], ...] = (
            (
                (provider_world_id, chunk.chunk_x, chunk.chunk_y, chunk.chunk_z),
                {},
                "generate_chunk(worldId, chunkX, chunkY, chunkZ)",
            ),
            (
                (),
                {
                    "world_id": provider_world_id,
                    "chunk_x": chunk.chunk_x,
                    "chunk_y": chunk.chunk_y,
                    "chunk_z": chunk.chunk_z,
                },
                "generate_chunk(world_id=..., chunk_x=..., chunk_y=..., chunk_z=...)",
            ),
            (
                (),
                {
                    "worldId": provider_world_id,
                    "chunkX": chunk.chunk_x,
                    "chunkY": chunk.chunk_y,
                    "chunkZ": chunk.chunk_z,
                },
                "generate_chunk(worldId=..., chunkX=..., chunkY=..., chunkZ=...)",
            ),
        )

        chunk_request = self._create_provider_chunk_request(
            provider_world_id=provider_world_id,
            chunk=chunk,
        )

        if chunk_request is not None:
            call_variants = (
                (
                    (chunk_request,),
                    {},
                    "generate_chunk(ChunkRequest)",
                ),
                *call_variants,
            )

        for args, kwargs, label in call_variants:
            result, error = _safe_call(method, *args, **kwargs)
            ok = _looks_like_chunk_object(result)

            attempts.append(
                {
                    "signature": label,
                    "ok": ok,
                    "error": error,
                    "resultType": result.__class__.__name__ if result is not None else None,
                }
            )

            if ok:
                return result

        raise WorldStateProviderError(
            "Provider chunk generation failed.",
            details={
                "providerWorldId": provider_world_id,
                "chunk": chunk.to_dict(),
                "attempts": attempts,
            },
        )

    def generate_chunk_batch(
        self,
        *,
        provider_world_id: str,
        chunks: Sequence[ChunkCoordinates],
    ) -> tuple[Any, ...] | None:
        """
        Try provider batch generation.

        Returns None if no suitable provider batch API is available.
        """
        service = self.get_world_service()
        method = getattr(service, "generate_chunk_batch", None)

        if not callable(method):
            return None

        chunk_payloads = [chunk.to_dict() for chunk in chunks]
        chunk_requests = [
            self._create_provider_chunk_request(
                provider_world_id=provider_world_id,
                chunk=chunk,
            )
            for chunk in chunks
        ]
        chunk_requests = [item for item in chunk_requests if item is not None]

        variants: list[tuple[tuple[Any, ...], dict[str, Any], str]] = [
            (
                (provider_world_id, chunk_payloads),
                {},
                "generate_chunk_batch(worldId, chunks)",
            ),
            (
                (),
                {
                    "world_id": provider_world_id,
                    "chunks": chunk_payloads,
                },
                "generate_chunk_batch(world_id=..., chunks=...)",
            ),
            (
                (),
                {
                    "worldId": provider_world_id,
                    "chunks": chunk_payloads,
                },
                "generate_chunk_batch(worldId=..., chunks=...)",
            ),
        ]

        if len(chunk_requests) == len(chunks):
            variants.insert(
                0,
                (
                    (chunk_requests,),
                    {},
                    "generate_chunk_batch(tuple[ChunkRequest])",
                ),
            )
            variants.insert(
                1,
                (
                    (),
                    {"requests": chunk_requests},
                    "generate_chunk_batch(requests=tuple[ChunkRequest])",
                ),
            )

        for args, kwargs, _label in variants:
            result, _error = _safe_call(method, *args, **kwargs)
            normalized = self._normalize_provider_batch_result(result)

            if normalized is not None:
                return normalized

        return None

    def _normalize_provider_batch_result(self, result: Any) -> tuple[Any, ...] | None:
        """Normalize provider batch result."""
        if result is None:
            return None

        if isinstance(result, (list, tuple)):
            if all(_looks_like_chunk_object(item) for item in result):
                return tuple(result)

        if isinstance(result, Mapping):
            for key in ("chunks", "results", "items"):
                value = result.get(key)
                if isinstance(value, (list, tuple)) and all(_looks_like_chunk_object(item) for item in value):
                    return tuple(value)

        if hasattr(result, "chunks"):
            try:
                chunks = getattr(result, "chunks")
                if isinstance(chunks, (list, tuple)) and all(_looks_like_chunk_object(item) for item in chunks):
                    return tuple(chunks)
            except Exception:
                return None

        return None

    def _create_provider_chunk_request(
        self,
        *,
        provider_world_id: str,
        chunk: ChunkCoordinates,
    ) -> Any | None:
        """Create src.world.models.ChunkRequest if available."""
        module, _import_error = _safe_import_module("src.world.models")

        if module is None:
            return None

        chunk_request_cls = getattr(module, "ChunkRequest", None)

        if chunk_request_cls is None:
            return None

        variants = (
            {
                "world_id": provider_world_id,
                "chunk_x": chunk.chunk_x,
                "chunk_y": chunk.chunk_y,
                "chunk_z": chunk.chunk_z,
            },
            {
                "worldId": provider_world_id,
                "chunkX": chunk.chunk_x,
                "chunkY": chunk.chunk_y,
                "chunkZ": chunk.chunk_z,
            },
        )

        for kwargs in variants:
            try:
                return chunk_request_cls(**kwargs)
            except Exception:
                continue

        try:
            return chunk_request_cls(
                provider_world_id,
                chunk.chunk_x,
                chunk.chunk_y,
                chunk.chunk_z,
            )
        except Exception:
            return None


# -----------------------------------------------------------------------------
# Chunk runtime helpers
# -----------------------------------------------------------------------------

def _extract_runtime_candidate(value: Any) -> dict[str, Any]:
    """Extract runtime chunk candidate from provider result."""
    data = _safe_object_to_dict(value)

    for key in ("chunk", "runtimeContent", "runtime_content", "content"):
        nested = data.get(key)
        if isinstance(nested, Mapping):
            return dict(nested)

    return data


def _ensure_cells(content: dict[str, Any], *, chunk_size: int) -> list[int]:
    """Ensure runtime content has full cells list."""
    expected_count = int(chunk_size) ** 3
    cells = content.get("cells")

    if not isinstance(cells, list):
        cells = []

    normalized: list[int] = []
    for value in cells:
        try:
            normalized.append(int(value))
        except Exception:
            normalized.append(AIR_CELL_VALUE)

    if len(normalized) < expected_count:
        normalized.extend([AIR_CELL_VALUE] * (expected_count - len(normalized)))
    elif len(normalized) > expected_count:
        normalized = normalized[:expected_count]

    content["cells"] = normalized
    content["cellCount"] = expected_count
    return normalized


def _ensure_palette(content: dict[str, Any]) -> list[dict[str, Any]]:
    """Ensure runtime content has mutable palette list."""
    palette = content.get("palette")

    if not isinstance(palette, list):
        palette = []

    normalized: list[dict[str, Any]] = []

    for entry in palette:
        if isinstance(entry, Mapping):
            normalized.append(dict(entry))
        elif isinstance(entry, str):
            normalized.append({"blockTypeId": entry})
        else:
            normalized.append({"raw": make_json_safe(entry)})

    content["palette"] = normalized
    return normalized


def _update_content_stats(content: dict[str, Any], *, chunk_size: int) -> None:
    """Update basic runtime content stats."""
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


def _runtime_content_from_generated(
    *,
    generated: Any,
    context: DbWorldRuntimeContext,
    coordinates: ChunkCoordinates,
) -> dict[str, Any]:
    """Build project-scoped runtime content from provider-generated chunk."""
    wrapper = _safe_object_to_dict(generated)
    candidate = _extract_runtime_candidate(generated)

    cells = candidate.get("cells")
    if cells is None:
        cells = wrapper.get("cells", [])

    palette = candidate.get("palette")
    if palette is None:
        palette = wrapper.get("palette", [])

    runtime = make_json_safe(dict(candidate))

    runtime.update(
        {
            "projectId": context.project_id,
            "universeId": context.universe_id,
            "worldId": context.world_id,
            "templateId": context.template_id,
            "providerId": context.provider_id,
            "providerWorldId": context.provider_world_id,
            "providerSourceWorldId": wrapper.get("worldId") or wrapper.get("world_id") or context.provider_world_id,
            "chunkX": coordinates.chunk_x,
            "chunkY": coordinates.chunk_y,
            "chunkZ": coordinates.chunk_z,
            "chunkKey": coordinates.chunk_key,
            "source": CHUNK_LOAD_SOURCE_GENERATED,
            "runtimeContentVersion": runtime.get("runtimeContentVersion") or RUNTIME_CHUNK_CONTENT_VERSION,
            "cellIndexOrder": runtime.get("cellIndexOrder") or CELL_INDEX_ORDER,
            "airCellValue": AIR_CELL_VALUE,
            "cellEncoding": {
                "version": CELL_ENCODING_VERSION,
                "airCellValue": AIR_CELL_VALUE,
                "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
            },
            "palette": make_json_safe(palette),
            "cells": make_json_safe(cells),
            "blockRegistryId": context.world.block_registry_id,
            "blockRegistryVersion": context.world.block_registry_version,
            "coordinateSystem": context.world.coordinate_system,
            "projectionType": context.world.projection_type,
            "topologyType": context.world.topology_type,
            "chunkSize": context.world.chunk_size,
            "cellSize": context.world.cell_size,
        }
    )

    _ensure_cells(runtime, chunk_size=context.world.chunk_size)
    _ensure_palette(runtime)
    _update_content_stats(runtime, chunk_size=context.world.chunk_size)

    return runtime


def _runtime_content_from_snapshot(
    *,
    snapshot: Any,
    context: DbWorldRuntimeContext,
) -> dict[str, Any]:
    """Build project-scoped runtime content from ChunkSnapshot."""
    runtime: dict[str, Any] = {}

    build_runtime_content = getattr(snapshot, "build_runtime_content", None)
    if callable(build_runtime_content):
        try:
            built = build_runtime_content()
            if isinstance(built, Mapping):
                runtime = dict(built)
        except Exception:
            runtime = {}

    if not runtime and isinstance(getattr(snapshot, "content_json", None), Mapping):
        runtime = dict(snapshot.content_json)

    chunk_x = int(snapshot.chunk_x)
    chunk_y = int(snapshot.chunk_y)
    chunk_z = int(snapshot.chunk_z)
    chunk_key = snapshot.chunk_key or _build_chunk_key(chunk_x, chunk_y, chunk_z)

    runtime.update(
        {
            "projectId": context.project_id,
            "universeId": context.universe_id,
            "worldId": context.world_id,
            "templateId": context.template_id,
            "providerId": context.provider_id,
            "providerWorldId": context.provider_world_id,
            "providerSourceWorldId": snapshot.provider_world_id or context.provider_world_id,
            "chunkX": chunk_x,
            "chunkY": chunk_y,
            "chunkZ": chunk_z,
            "chunkKey": chunk_key,
            "source": CHUNK_LOAD_SOURCE_SNAPSHOT,
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
            "palette": make_json_safe(snapshot.palette_json or []),
            "objectRefs": make_json_safe(snapshot.object_refs_json or []),
            "cellCount": snapshot.cell_count,
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

    _ensure_cells(runtime, chunk_size=context.world.chunk_size)
    _ensure_palette(runtime)
    _update_content_stats(runtime, chunk_size=context.world.chunk_size)

    return make_json_safe(runtime)


# -----------------------------------------------------------------------------
# Block serialization helpers
# -----------------------------------------------------------------------------

def _serialize_air_entry() -> dict[str, Any]:
    """Serialize invariant Air entry."""
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


def _sort_blocks_for_palette(blocks: Sequence[Any]) -> list[Any]:
    """Sort blocks deterministically."""
    return sorted(
        list(blocks or []),
        key=lambda block: (
            getattr(block, "default_palette_index", None) is None,
            getattr(block, "default_palette_index", None)
            if getattr(block, "default_palette_index", None) is not None
            else 999999,
            getattr(block, "block_type_id", ""),
        ),
    )


def _serialize_block_palette_entry(
    block: Any,
    *,
    palette_index: int,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """Serialize a BlockType row as palette entry."""
    to_palette_entry = getattr(block, "to_palette_entry", None)
    if callable(to_palette_entry):
        try:
            entry = to_palette_entry(
                palette_index=palette_index,
                include_metadata=include_metadata,
            )
            if isinstance(entry, Mapping):
                result = dict(entry)
            else:
                result = {}
        except Exception:
            result = {}
    else:
        result = {}

    if not result:
        result = {
            "paletteIndex": palette_index,
            "cellValue": palette_index + 1,
            "blockTypeId": getattr(block, "block_type_id", None),
            "label": getattr(block, "label", None),
            "registryId": getattr(block, "registry_id", None),
            "registryVersion": getattr(block, "registry_version", None),
            "solid": getattr(block, "solid", True),
            "opaque": getattr(block, "opaque", True),
            "placeable": getattr(block, "placeable", True),
            "breakable": getattr(block, "breakable", True),
            "selectable": getattr(block, "selectable", True),
            "collidable": getattr(block, "collidable", True),
            "renderMode": getattr(block, "render_mode", None),
            "shapeType": getattr(block, "shape_type", None),
            "materialId": getattr(block, "material_id", None),
            "textureId": getattr(block, "texture_id", None),
            "iconId": getattr(block, "icon_id", None),
        }

    result["paletteIndex"] = palette_index
    result["computedPaletteIndex"] = palette_index
    result["cellValue"] = palette_index + 1
    result["defaultPaletteIndex"] = getattr(block, "default_palette_index", None)

    if include_metadata:
        result.setdefault("metadata", make_json_safe(getattr(block, "metadata_json", {}) or {}))

    return make_json_safe(result)


def _serialize_blocks_payload(
    *,
    registry: Any,
    blocks: Sequence[Any],
    include_metadata: bool = True,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Serialize DB block registry and block types."""
    sorted_blocks = _sort_blocks_for_palette(blocks)

    palette = [
        _serialize_block_palette_entry(
            block,
            palette_index=index,
            include_metadata=include_metadata,
        )
        for index, block in enumerate(sorted_blocks)
    ]

    if include_raw:
        for entry, block in zip(palette, sorted_blocks):
            entry["raw"] = _call_to_dict(
                block,
                include_internal=False,
                include_metadata=include_metadata,
            )

    registry_payload = _call_to_dict(
        registry,
        include_internal=False,
        include_metadata=include_metadata,
    )

    return {
        "registry": registry_payload,
        "registryId": getattr(registry, "registry_id", None),
        "registryVersion": getattr(registry, "registry_version", None),
        "air": _serialize_air_entry(),
        "encoding": {
            "version": CELL_ENCODING_VERSION,
            "airCellValue": AIR_CELL_VALUE,
            "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
        },
        "blocks": palette,
        "palette": palette,
        "counts": {
            "blocks": len(sorted_blocks),
            "paletteEntries": len(palette),
            "includingAir": len(palette) + 1,
        },
        "source": "postgres-block-registry",
    }


# -----------------------------------------------------------------------------
# WorldStateService
# -----------------------------------------------------------------------------

class WorldStateService:
    """
    PostgreSQL-backed project-scoped service facade.
    """

    def __init__(
        self,
        resolver: Any | None = None,
        *,
        provider_adapter: WorldProviderAdapter | None = None,
        max_batch_chunks: int = DEFAULT_MAX_BATCH_CHUNKS,
        strict_provider_resolution: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._resolver = resolver
        self._provider_adapter = provider_adapter or WorldProviderAdapter()
        self._max_batch_chunks = int(max_batch_chunks or DEFAULT_MAX_BATCH_CHUNKS)
        self._strict_provider_resolution = bool(strict_provider_resolution)
        self._metadata = deep_copy_json(metadata or {})
        self._status_cache: dict[str, Any] | None = None

    @property
    def resolver(self) -> Any:
        return self._resolver

    @property
    def provider_adapter(self) -> WorldProviderAdapter:
        return self._provider_adapter

    @property
    def max_batch_chunks(self) -> int:
        return self._max_batch_chunks

    def reset_caches(self) -> None:
        """Reset local service caches."""
        with self._lock:
            self._status_cache = None
            try:
                reset = getattr(self._resolver, "reset_caches", None)
                if callable(reset):
                    reset()
            except Exception:
                pass
            try:
                self._provider_adapter.reset_cache()
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # DB resolution helpers
    # -------------------------------------------------------------------------

    def _get_project(
        self,
        project_id: str | None,
        *,
        allow_default_project: bool = False,
        include_deleted: bool = False,
    ) -> Any:
        _require_db_models_ready()

        if not project_id and allow_default_project:
            project_id = self._get_default_project_id()

        raise_for_missing_project_id(project_id)

        query = Project.query.filter(Project.project_id == project_id)

        if not include_deleted:
            query = query.filter(Project.deleted_at.is_(None))

        project = query.one_or_none()

        if project is None:
            raise LookupError(f"Project '{project_id}' was not found.")

        return project

    def _get_default_universe_id(self) -> str:
        return "dev-universe"

    def _get_default_project_id(self) -> str:
        return "dev-project"

    def _get_default_world_id(self) -> str:
        return "world_spawn"

    def _get_project_default_universe(
        self,
        project: Any,
        *,
        include_deleted: bool = False,
    ) -> Any:
        _require_db_models_ready()

        universe_id = getattr(project, "default_universe_id", None) or self._get_default_universe_id()

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

    def _get_universe(
        self,
        project: Any,
        universe_id: str | None = None,
        *,
        include_deleted: bool = False,
    ) -> Any:
        _require_db_models_ready()

        if not universe_id:
            return self._get_project_default_universe(project, include_deleted=include_deleted)

        query = Universe.query.filter(
            Universe.project_db_id == project.id,
            Universe.universe_id == universe_id,
        )

        if not include_deleted:
            query = query.filter(Universe.deleted_at.is_(None))

        universe = query.one_or_none()

        if universe is None:
            raise LookupError(
                f"Universe '{universe_id}' was not found in project '{project.project_id}'."
            )

        return universe

    def _get_world(
        self,
        universe: Any,
        world_id: str | None,
        *,
        include_deleted: bool = False,
    ) -> Any:
        _require_db_models_ready()

        if not world_id:
            world_id = getattr(universe, "spawn_world_id", None) or getattr(universe, "default_world_id", None) or self._get_default_world_id()

        raise_for_missing_world_id(world_id)

        query = WorldInstance.query.filter(
            WorldInstance.universe_db_id == universe.id,
            WorldInstance.world_id == world_id,
        )

        if not include_deleted:
            query = query.filter(WorldInstance.deleted_at.is_(None))

        world = query.one_or_none()

        if world is None:
            raise LookupError(
                f"World '{world_id}' was not found in universe '{universe.universe_id}'."
            )

        return world

    def _get_default_or_spawn_world(
        self,
        universe: Any,
        *,
        include_deleted: bool = False,
    ) -> Any:
        world_id = getattr(universe, "spawn_world_id", None) or getattr(universe, "default_world_id", None) or self._get_default_world_id()

        try:
            return self._get_world(
                universe,
                world_id,
                include_deleted=include_deleted,
            )
        except LookupError:
            query = WorldInstance.query.filter(WorldInstance.universe_db_id == universe.id)
            if not include_deleted:
                query = query.filter(WorldInstance.deleted_at.is_(None))
            world = query.order_by(WorldInstance.created_at.asc()).first()

            if world is None:
                raise

            return world

    def _make_context(self, project: Any, universe: Any, world: Any) -> DbWorldRuntimeContext:
        return DbWorldRuntimeContext(
            project=project,
            universe=universe,
            world=world,
            metadata={
                "source": SERVICE_SOURCE,
                "dbBacked": True,
                "createdAt": utc_now_iso(),
            },
        )

    def _resolve_provider_world(self, context: DbWorldRuntimeContext) -> Any:
        """
        Build provider resolution, using old resolver if available, otherwise a
        simple local resolution object.
        """
        if self._resolver is not None:
            try:
                resolve_provider_world = getattr(self._resolver, "resolve_provider_world", None)
                if callable(resolve_provider_world):
                    return resolve_provider_world(
                        context,
                        require_available=self._strict_provider_resolution,
                    )
            except Exception:
                if self._strict_provider_resolution:
                    raise

        return SimpleProviderWorldResolution(
            provider_world_id=context.provider_world_id,
            template_id=context.template_id,
            project_id=context.project_id,
            universe_id=context.universe_id,
            world_id=context.world_id,
            available=True,
            metadata={
                "source": SERVICE_SOURCE,
                "fallback": True,
            },
        )

    # -------------------------------------------------------------------------
    # Public project/world API
    # -------------------------------------------------------------------------

    def resolve_context(
        self,
        project_id: str,
        world_id: str,
        *,
        universe_id: str | None = None,
    ) -> DbWorldRuntimeContext:
        """Resolve project, universe and world from PostgreSQL."""
        project = self._get_project(project_id)
        universe = self._get_universe(project, universe_id)
        world = self._get_world(universe, world_id)
        return self._make_context(project, universe, world)

    def get_project(self, project_id: str) -> Any:
        """Return persisted Project."""
        return self._get_project(project_id)

    def list_projects(
        self,
        *,
        include_inactive: bool = False,
    ) -> tuple[Any, ...]:
        """List persisted projects."""
        _require_db_models_ready()

        query = Project.query

        if not include_inactive:
            query = query.filter(Project.deleted_at.is_(None))
            query = query.filter(Project.status == "active")

        return tuple(query.order_by(Project.created_at.desc()).all())

    def list_worlds(
        self,
        project_id: str,
        *,
        universe_id: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[Any, ...]:
        """List concrete world instances of a project."""
        project = self._get_project(project_id, include_deleted=include_inactive)

        universe = None
        if universe_id:
            universe = self._get_universe(project, universe_id, include_deleted=include_inactive)

        query = WorldInstance.query.filter(WorldInstance.project_db_id == project.id)

        if universe is not None:
            query = query.filter(WorldInstance.universe_db_id == universe.id)

        if not include_inactive:
            query = query.filter(WorldInstance.deleted_at.is_(None))
            query = query.filter(WorldInstance.status == "active")

        return tuple(query.order_by(WorldInstance.created_at.asc()).all())

    def get_world(
        self,
        project_id: str,
        world_id: str,
        *,
        universe_id: str | None = None,
    ) -> WorldStateWorldMetadataResult:
        """Return productive metadata for a concrete world instance."""
        context = self.resolve_context(
            project_id,
            world_id,
            universe_id=universe_id,
        )

        provider_resolution = self._resolve_provider_world(context)

        provider_metadata: Mapping[str, Any]
        try:
            provider_metadata = self._provider_adapter.get_world_metadata(
                context.provider_world_id,
            )
        except Exception as exc:
            if self._strict_provider_resolution:
                raise ProviderWorldResolutionError(
                    context.provider_world_id,
                    template_id=context.template_id,
                    project_id=context.project_id,
                    universe_id=context.universe_id,
                    world_id=context.world_id,
                    details={
                        "operation": "get_world_metadata",
                        "error": _safe_exception_message(exc),
                    },
                    cause=exc if isinstance(exc, BaseException) else None,
                ) from exc

            provider_metadata = {
                "source": "world_state.service.provider_metadata_fallback",
                "error": _safe_exception_message(exc),
            }

        return WorldStateWorldMetadataResult(
            context=context,
            provider_world_metadata=provider_metadata,
            provider_resolution=provider_resolution,
            metadata={
                "source": SERVICE_SOURCE,
                "dbBacked": True,
                "createdAt": utc_now_iso(),
            },
        )

    def get_blocks(
        self,
        project_id: str,
        world_id: str,
        *,
        universe_id: str | None = None,
    ) -> WorldStateBlocksResult:
        """Return block/palette data for a concrete world instance."""
        context = self.resolve_context(
            project_id,
            world_id,
            universe_id=universe_id,
        )

        provider_resolution = self._resolve_provider_world(context)

        registry = (
            BlockRegistry.query
            .filter_by(
                registry_id=context.world.block_registry_id,
                registry_version=context.world.block_registry_version,
            )
            .one_or_none()
        )

        if registry is None:
            raise LookupError(
                f"Block registry '{context.world.block_registry_id}@{context.world.block_registry_version}' was not found."
            )

        blocks = (
            BlockType.query
            .filter(
                BlockType.registry_db_id == registry.id,
                BlockType.deleted_at.is_(None),
                BlockType.status == "active",
            )
            .all()
        )

        blocks_payload = _serialize_blocks_payload(
            registry=registry,
            blocks=blocks,
            include_metadata=True,
            include_raw=True,
        )

        return WorldStateBlocksResult(
            context=context,
            blocks_payload=blocks_payload,
            provider_resolution=provider_resolution,
            metadata={
                "source": SERVICE_SOURCE,
                "dbBacked": True,
                "createdAt": utc_now_iso(),
            },
        )

    # -------------------------------------------------------------------------
    # Chunk loading
    # -------------------------------------------------------------------------

    def _find_chunk_snapshot(
        self,
        *,
        context: DbWorldRuntimeContext,
        coordinates: ChunkCoordinates,
        include_deleted: bool = False,
    ) -> Any | None:
        """Find active ChunkSnapshot for context/chunk."""
        query = ChunkSnapshot.query.filter(
            ChunkSnapshot.world_db_id == context.world.id,
            ChunkSnapshot.chunk_x == coordinates.chunk_x,
            ChunkSnapshot.chunk_y == coordinates.chunk_y,
            ChunkSnapshot.chunk_z == coordinates.chunk_z,
        )

        if not include_deleted:
            query = query.filter(ChunkSnapshot.deleted_at.is_(None))
            query = query.filter(ChunkSnapshot.status == "active")

        return query.one_or_none()

    def get_chunk(
        self,
        project_id: str,
        world_id: str,
        chunk_x: Any,
        chunk_y: Any,
        chunk_z: Any,
        *,
        universe_id: str | None = None,
        prefer_snapshot: bool = True,
        allow_generated: bool = True,
    ) -> WorldStateChunkResult:
        """
        Load one chunk for a concrete project world.

        Uses PostgreSQL ChunkSnapshot first, then provider-generated fallback.
        """
        context = self.resolve_context(
            project_id,
            world_id,
            universe_id=universe_id,
        )
        coordinates = normalize_chunk_coordinates(chunk_x, chunk_y, chunk_z)
        provider_resolution = self._resolve_provider_world(context)

        snapshot = None

        if prefer_snapshot:
            snapshot = self._find_chunk_snapshot(
                context=context,
                coordinates=coordinates,
            )

        if snapshot is not None:
            runtime_content = _runtime_content_from_snapshot(
                snapshot=snapshot,
                context=context,
            )

            return WorldStateChunkResult(
                context=context,
                coordinates=coordinates,
                provider_resolution=provider_resolution,
                provider_chunk=runtime_content,
                source=CHUNK_LOAD_SOURCE_SNAPSHOT,
                snapshot=snapshot,
                metadata={
                    "source": SERVICE_SOURCE,
                    "createdAt": utc_now_iso(),
                    "snapshotChecked": True,
                    "snapshotAvailable": True,
                    "generatorUsed": False,
                    "dbBacked": True,
                    "snapshotId": snapshot.snapshot_id,
                    "chunkVersion": snapshot.chunk_version,
                },
            )

        if not allow_generated:
            raise LookupError(
                f"Chunk '{coordinates.chunk_key}' has no snapshot and generated fallback is disabled."
            )

        try:
            generated_chunk = self._provider_adapter.generate_chunk(
                provider_world_id=context.provider_world_id,
                chunk=coordinates,
            )
            runtime_content = _runtime_content_from_generated(
                generated=generated_chunk,
                context=context,
                coordinates=coordinates,
            )
        except Exception as exc:
            raise WorldStateProviderError(
                "Could not generate provider chunk for world instance.",
                details={
                    "projectId": context.project_id,
                    "universeId": context.universe_id,
                    "worldId": context.world_id,
                    "templateId": context.template_id,
                    "providerWorldId": context.provider_world_id,
                    "chunk": coordinates.to_dict(),
                    "error": _safe_exception_message(exc),
                },
                cause=exc if isinstance(exc, BaseException) else None,
            ) from exc

        return WorldStateChunkResult(
            context=context,
            coordinates=coordinates,
            provider_resolution=provider_resolution,
            provider_chunk=runtime_content,
            source=CHUNK_LOAD_SOURCE_GENERATED,
            snapshot=None,
            metadata={
                "source": SERVICE_SOURCE,
                "createdAt": utc_now_iso(),
                "snapshotChecked": bool(prefer_snapshot),
                "snapshotAvailable": False,
                "generatorUsed": True,
                "dbBacked": True,
                "providerWorldId": context.provider_world_id,
            },
        )

    def get_chunk_batch(
        self,
        project_id: str,
        world_id: str,
        chunks: Sequence[Mapping[str, Any]] | Sequence[Any],
        *,
        universe_id: str | None = None,
        continue_on_error: bool = False,
        prefer_snapshot: bool = True,
        allow_generated: bool = True,
    ) -> WorldStateChunkBatchResult:
        """
        Load multiple chunks for a concrete project world.
        """
        context = self.resolve_context(
            project_id,
            world_id,
            universe_id=universe_id,
        )

        requested = normalize_chunk_coordinate_items(
            chunks,
            max_count=self._max_batch_chunks,
        )

        results: list[WorldStateChunkResult] = []
        errors: list[Mapping[str, Any]] = []

        for coords in requested:
            try:
                result = self.get_chunk(
                    context.project_id,
                    context.world_id,
                    coords.chunk_x,
                    coords.chunk_y,
                    coords.chunk_z,
                    universe_id=context.universe_id,
                    prefer_snapshot=prefer_snapshot,
                    allow_generated=allow_generated,
                )
                results.append(result)
            except Exception as exc:
                error_payload = {
                    "scope": "chunk",
                    "chunk": coords.to_dict(),
                    "error": coerce_world_state_error(exc).to_dict(include_debug=True),
                }
                errors.append(error_payload)

                if not continue_on_error:
                    break

        return WorldStateChunkBatchResult(
            context=context,
            chunks=tuple(results),
            requested=requested,
            errors=tuple(errors),
            metadata={
                "source": SERVICE_SOURCE,
                "createdAt": utc_now_iso(),
                "batchMode": "snapshot-or-generated",
                "continueOnError": bool(continue_on_error),
                "preferSnapshot": bool(prefer_snapshot),
                "allowGenerated": bool(allow_generated),
                "dbBacked": True,
            },
        )

    # -------------------------------------------------------------------------
    # Bootstrap
    # -------------------------------------------------------------------------

    def create_project_bootstrap(
        self,
        project_id: str | None = None,
        *,
        allow_default_project: bool = False,
        api_prefix: str = "",
    ) -> DbProjectBootstrapContext:
        """
        Create bootstrap context for opening a project in the editor.
        """
        project = self._get_project(
            project_id,
            allow_default_project=allow_default_project or not project_id,
        )
        universe = self._get_project_default_universe(project)
        spawn_world = self._get_default_or_spawn_world(universe)

        default_world = spawn_world
        if getattr(universe, "default_world_id", None):
            try:
                default_world = self._get_world(universe, universe.default_world_id)
            except Exception:
                default_world = spawn_world

        worlds = tuple(
            WorldInstance.query
            .filter(
                WorldInstance.universe_db_id == universe.id,
                WorldInstance.deleted_at.is_(None),
            )
            .order_by(WorldInstance.created_at.asc())
            .all()
        )

        prefix = str(api_prefix or "").rstrip("/")
        route_hints = {
            "projectBootstrap": f"{prefix}/projects/{project.project_id}/bootstrap",
            "project": f"{prefix}/projects/{project.project_id}",
            "worlds": f"{prefix}/projects/{project.project_id}/worlds",
            "world": f"{prefix}/projects/{project.project_id}/worlds/{spawn_world.world_id}",
            "blocks": f"{prefix}/projects/{project.project_id}/worlds/{spawn_world.world_id}/blocks",
            "chunk": f"{prefix}/projects/{project.project_id}/worlds/{spawn_world.world_id}/chunks",
            "chunksBatch": f"{prefix}/projects/{project.project_id}/worlds/{spawn_world.world_id}/chunks/batch",
            "commands": f"{prefix}/projects/{project.project_id}/worlds/{spawn_world.world_id}/commands",
        }

        return DbProjectBootstrapContext(
            project=project,
            universe=universe,
            default_world=default_world,
            spawn_world=spawn_world,
            worlds=worlds,
            route_hints=route_hints,
            metadata={
                "source": SERVICE_SOURCE,
                "createdAt": utc_now_iso(),
                "phase": "postgres-world-state",
                "worldStateServiceVersion": SERVICE_MODULE_VERSION,
                "dbBacked": True,
            },
        )

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------

    def get_status(
        self,
        *,
        refresh: bool = False,
        include_resolver: bool = False,
        include_provider: bool = False,
        include_provider_checks: bool = False,
    ) -> dict[str, Any]:
        """
        Return JSON-safe diagnostics.
        """
        with self._lock:
            if (
                self._status_cache is not None
                and not refresh
                and not include_resolver
                and not include_provider
                and not include_provider_checks
            ):
                return copy.deepcopy(self._status_cache)

        payload: dict[str, Any] = {
            "ok": False,
            "source": SERVICE_SOURCE,
            "moduleVersion": SERVICE_MODULE_VERSION,
            "dbBacked": True,
            "maxBatchChunks": self._max_batch_chunks,
            "strictProviderResolution": self._strict_provider_resolution,
            "metadata": deep_copy_json(self._metadata),
            "models": None,
            "counts": None,
            "resolver": None,
            "providerAdapter": None,
            "error": None,
        }

        try:
            _require_db_models_ready()
            payload["models"] = get_model_debug_summary()

            payload["counts"] = {
                "projects": Project.query.count(),
                "universes": Universe.query.count(),
                "worlds": WorldInstance.query.count(),
                "chunkSnapshots": ChunkSnapshot.query.count(),
                "blockRegistries": BlockRegistry.query.count(),
                "blockTypes": BlockType.query.count(),
            }

            payload["ok"] = True

            if include_resolver and self._resolver is not None:
                resolver_status = getattr(self._resolver, "get_status", None)
                if callable(resolver_status):
                    payload["resolver"] = resolver_status(
                        refresh=refresh,
                        include_catalog=False,
                        include_provider_checks=include_provider_checks,
                    )
                else:
                    payload["resolver"] = {
                        "available": True,
                        "type": self._resolver.__class__.__name__,
                    }

            if include_provider:
                payload["providerAdapter"] = self._provider_adapter.get_status()

        except Exception as exc:
            payload["ok"] = False
            payload["error"] = coerce_world_state_error(exc).to_dict(include_debug=True)

        safe_payload = make_json_safe(payload)

        with self._lock:
            if not include_resolver and not include_provider and not include_provider_checks:
                self._status_cache = copy.deepcopy(safe_payload)

        return copy.deepcopy(safe_payload)

    def to_dict(self) -> dict[str, Any]:
        """Serialize service descriptor."""
        return {
            "source": SERVICE_SOURCE,
            "moduleVersion": SERVICE_MODULE_VERSION,
            "dbBacked": True,
            "maxBatchChunks": self._max_batch_chunks,
            "strictProviderResolution": self._strict_provider_resolution,
            "metadata": deep_copy_json(self._metadata),
        }


# -----------------------------------------------------------------------------
# Factory/cache functions
# -----------------------------------------------------------------------------

def create_default_world_state_service(
    *,
    refresh_resolver: bool = False,
    provider_check_enabled: bool = True,
    strict_provider_resolution: bool = False,
    max_batch_chunks: int = DEFAULT_MAX_BATCH_CHUNKS,
    metadata: Mapping[str, Any] | None = None,
) -> WorldStateService:
    """
    Create the default project-scoped world-state service.
    """
    resolver = None

    try:
        resolver = get_default_world_state_resolver(
            refresh=refresh_resolver,
            provider_check_enabled=provider_check_enabled,
            strict_provider_resolution=strict_provider_resolution,
        )
    except Exception:
        resolver = None

    return WorldStateService(
        resolver=resolver,
        provider_adapter=WorldProviderAdapter(),
        max_batch_chunks=max_batch_chunks,
        strict_provider_resolution=strict_provider_resolution,
        metadata={
            "source": SERVICE_SOURCE,
            "createdFrom": "create_default_world_state_service",
            **deep_copy_json(metadata or {}),
        },
    )


def get_default_world_state_service(
    *,
    refresh: bool = False,
    refresh_resolver: bool = False,
    provider_check_enabled: bool = True,
    strict_provider_resolution: bool = False,
    max_batch_chunks: int = DEFAULT_MAX_BATCH_CHUNKS,
) -> WorldStateService:
    """
    Return the cached default world-state service.
    """
    global _default_world_state_service_cache

    with _default_service_lock:
        if _default_world_state_service_cache is not None and not refresh and not refresh_resolver:
            cached = _default_world_state_service_cache

            if cached.max_batch_chunks == int(max_batch_chunks or DEFAULT_MAX_BATCH_CHUNKS):
                return cached

        _default_world_state_service_cache = create_default_world_state_service(
            refresh_resolver=refresh or refresh_resolver,
            provider_check_enabled=provider_check_enabled,
            strict_provider_resolution=strict_provider_resolution,
            max_batch_chunks=max_batch_chunks,
        )

        return _default_world_state_service_cache


def reset_default_world_state_service_cache(
    *,
    reset_resolver_cache: bool = False,
    reset_catalog_cache: bool = False,
) -> None:
    """
    Reset cached default service.
    """
    global _default_world_state_service_cache

    with _default_service_lock:
        if _default_world_state_service_cache is not None:
            try:
                _default_world_state_service_cache.reset_caches()
            except Exception:
                pass

        _default_world_state_service_cache = None

        if reset_resolver_cache:
            try:
                reset_default_world_state_resolver_cache(
                    reset_catalog_cache=reset_catalog_cache,
                )
            except Exception:
                pass


def get_world_state_service_status(
    *,
    refresh: bool = False,
    include_resolver: bool = False,
    include_provider: bool = False,
    include_provider_checks: bool = False,
) -> dict[str, Any]:
    """
    Convenience status function using the default service.
    """
    service = get_default_world_state_service(refresh=refresh)

    return service.get_status(
        refresh=refresh,
        include_resolver=include_resolver,
        include_provider=include_provider,
        include_provider_checks=include_provider_checks,
    )


def assert_world_state_service_ready(
    *,
    refresh: bool = False,
    require_provider_chunk_generation: bool = False,
) -> WorldStateService:
    """
    Validate that the default service can resolve the default project/world.

    If `require_provider_chunk_generation=True`, also attempts to generate
    chunk 0:0:0 for the default spawn world.
    """
    service = get_default_world_state_service(refresh=refresh)

    try:
        bootstrap = service.create_project_bootstrap(
            allow_default_project=True,
        )

        if require_provider_chunk_generation:
            service.get_chunk(
                bootstrap.project_id,
                bootstrap.spawn_world_id,
                0,
                0,
                0,
                universe_id=bootstrap.universe_id,
            )

    except Exception as exc:
        raise RuntimeError(
            f"World-state service is not ready: {_safe_exception_message(exc)}"
        ) from exc

    return service


__all__ = (
    "SERVICE_MODULE_VERSION",
    "SERVICE_SOURCE",
    "CHUNK_LOAD_SOURCE_GENERATED",
    "CHUNK_LOAD_SOURCE_PROVIDER",
    "CHUNK_LOAD_SOURCE_SNAPSHOT",
    "CHUNK_LOAD_SOURCE_UNKNOWN",
    "DEFAULT_MAX_BATCH_CHUNKS",
    "RUNTIME_CHUNK_CONTENT_VERSION",
    "CELL_ENCODING_VERSION",
    "CELL_INDEX_ORDER",
    "AIR_CELL_VALUE",
    "BLOCK_CELL_VALUE_RULE",
    "ChunkCoordinates",
    "DbWorldRuntimeContext",
    "DbProjectBootstrapContext",
    "SimpleProviderWorldResolution",
    "WorldStateChunkResult",
    "WorldStateChunkBatchResult",
    "WorldStateBlocksResult",
    "WorldStateWorldMetadataResult",
    "WorldProviderAdapter",
    "WorldStateService",
    "normalize_chunk_coordinates",
    "normalize_chunk_coordinate_items",
    "create_default_world_state_service",
    "get_default_world_state_service",
    "reset_default_world_state_service_cache",
    "get_world_state_service_status",
    "assert_world_state_service_ready",
)