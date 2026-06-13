# services/vectoplan-chunk/src/world_state/defaults.py
"""
Default development world-state catalog for the VECTOPLAN chunk service.

This module creates the first non-persistent project/universe/world-instance
state used by the productive project-scoped API.

Phase 1 default mapping:

    projectId       = dev-project
    universeId      = dev-universe
    worldId         = world_spawn
    templateId      = flat
    providerWorldId = flat

Important:

- `flat` remains the provider/template world.
- `world_spawn` is the concrete runtime world instance inside the project.
- No PostgreSQL access happens here.
- This module is the later replacement point for repository-backed defaults.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .models import (
    DEFAULT_BLOCK_REGISTRY_ID,
    DEFAULT_BLOCK_REGISTRY_VERSION,
    DEFAULT_CELL_SIZE,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_COORDINATE_SYSTEM,
    DEFAULT_GENERATOR_TYPE,
    DEFAULT_GENERATOR_VERSION,
    DEFAULT_INSTANCE_WORLD_ID,
    DEFAULT_MAX_Y,
    DEFAULT_MIN_Y,
    DEFAULT_PROJECT_ID,
    DEFAULT_PROJECTION_TYPE,
    DEFAULT_PROVIDER_ID,
    DEFAULT_PROVIDER_WORLD_ID,
    DEFAULT_SEED,
    DEFAULT_STATUS_ACTIVE,
    DEFAULT_SURFACE_Y,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TOPOLOGY_TYPE,
    DEFAULT_UNIVERSE_ID,
    DEFAULT_WORLD_TYPE,
    OWNER_TYPE_PROJECT,
    PROJECT_CONTEXT_SCHEMA_VERSION,
    UNIVERSE_CONTEXT_SCHEMA_VERSION,
    WORLD_INSTANCE_SCHEMA_VERSION,
    WORLD_ROLE_DEFAULT_SPAWN,
    WORLD_SCOPE_PROJECT,
    ProjectRuntimeContext,
    UniverseRuntimeContext,
    WorldInstanceDefinition,
    WorldStateCatalog,
    deep_copy_json,
    make_json_safe,
    merge_metadata,
    normalize_cell_size,
    normalize_chunk_size,
    normalize_project_id,
    normalize_provider_world_id,
    normalize_template_id,
    normalize_universe_id,
    normalize_vertical_bounds,
    normalize_world_instance_id,
    stable_hash,
    utc_now_iso,
)


DEFAULTS_MODULE_VERSION = "0.1.0"
DEFAULTS_SOURCE = "world_state.defaults"

ENV_DEFAULT_PROJECT_ID = "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID"
ENV_DEFAULT_PROJECT_SLUG = "VECTOPLAN_CHUNK_DEFAULT_PROJECT_SLUG"
ENV_DEFAULT_PROJECT_NAME = "VECTOPLAN_CHUNK_DEFAULT_PROJECT_NAME"

ENV_DEFAULT_UNIVERSE_ID = "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID"
ENV_DEFAULT_UNIVERSE_SLUG = "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_SLUG"
ENV_DEFAULT_UNIVERSE_NAME = "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_NAME"

ENV_DEFAULT_INSTANCE_WORLD_ID = "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID"
ENV_DEFAULT_INSTANCE_WORLD_SLUG = "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_SLUG"
ENV_DEFAULT_INSTANCE_WORLD_NAME = "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_NAME"

ENV_DEFAULT_WORLD_TEMPLATE_ID = "VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID"
ENV_DEFAULT_PROVIDER_WORLD_ID = "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID"
ENV_DEFAULT_PROVIDER_ID = "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID"

ENV_DEFAULT_WORLD_TYPE = "VECTOPLAN_CHUNK_DEFAULT_WORLD_TYPE"
ENV_DEFAULT_WORLD_ROLE = "VECTOPLAN_CHUNK_DEFAULT_WORLD_ROLE"
ENV_DEFAULT_WORLD_SCOPE = "VECTOPLAN_CHUNK_DEFAULT_WORLD_SCOPE"
ENV_DEFAULT_OWNER_TYPE = "VECTOPLAN_CHUNK_DEFAULT_WORLD_OWNER_TYPE"

ENV_DEFAULT_GENERATOR_TYPE = "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_TYPE"
ENV_DEFAULT_GENERATOR_VERSION = "VECTOPLAN_CHUNK_DEFAULT_GENERATOR_VERSION"
ENV_DEFAULT_PROJECTION_TYPE = "VECTOPLAN_CHUNK_DEFAULT_PROJECTION_TYPE"
ENV_DEFAULT_TOPOLOGY_TYPE = "VECTOPLAN_CHUNK_DEFAULT_TOPOLOGY_TYPE"
ENV_DEFAULT_COORDINATE_SYSTEM = "VECTOPLAN_CHUNK_DEFAULT_COORDINATE_SYSTEM"

ENV_DEFAULT_CHUNK_SIZE = "VECTOPLAN_CHUNK_DEFAULT_CHUNK_SIZE"
ENV_DEFAULT_CELL_SIZE = "VECTOPLAN_CHUNK_DEFAULT_CELL_SIZE"
ENV_DEFAULT_SURFACE_Y = "VECTOPLAN_CHUNK_DEFAULT_SURFACE_Y"
ENV_DEFAULT_MIN_Y = "VECTOPLAN_CHUNK_DEFAULT_MIN_Y"
ENV_DEFAULT_MAX_Y = "VECTOPLAN_CHUNK_DEFAULT_MAX_Y"
ENV_DEFAULT_SEED = "VECTOPLAN_CHUNK_DEFAULT_SEED"

ENV_DEFAULT_BLOCK_REGISTRY_ID = "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID"
ENV_DEFAULT_BLOCK_REGISTRY_VERSION = "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION"

ENV_DEFAULT_SPAWN_X = "VECTOPLAN_CHUNK_DEFAULT_SPAWN_X"
ENV_DEFAULT_SPAWN_Y = "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Y"
ENV_DEFAULT_SPAWN_Z = "VECTOPLAN_CHUNK_DEFAULT_SPAWN_Z"
ENV_DEFAULT_SPAWN_YAW = "VECTOPLAN_CHUNK_DEFAULT_SPAWN_YAW"
ENV_DEFAULT_SPAWN_PITCH = "VECTOPLAN_CHUNK_DEFAULT_SPAWN_PITCH"

ENV_DEFAULT_BOOTSTRAP_API_PREFIX = "VECTOPLAN_CHUNK_API_PREFIX"
ENV_DISABLE_PROVIDER_ENRICHMENT = "VECTOPLAN_CHUNK_DISABLE_PROVIDER_ENRICHMENT"

LEGACY_ENV_DEFAULT_WORLD_ID = "VECTOPLAN_CHUNK_DEFAULT_WORLD_ID"

_DEFAULT_PROJECT_SLUG = "dev-project"
_DEFAULT_PROJECT_NAME = "Dev Project"
_DEFAULT_UNIVERSE_SLUG = "dev-universe"
_DEFAULT_UNIVERSE_NAME = "Dev Universe"
_DEFAULT_INSTANCE_WORLD_SLUG = "spawn"
_DEFAULT_INSTANCE_WORLD_NAME = "Flat Spawn World"

_DEFAULT_SPAWN = {
    "position": {
        "x": 0,
        "y": 2,
        "z": 0,
    },
    "rotation": {
        "yaw": 0,
        "pitch": 0,
    },
}

_DEFAULT_RUNTIME = {
    "isDefaultWorld": True,
    "isSpawnWorld": True,
    "chunkSource": "provider-template",
    "templateBacked": True,
    "snapshotBacked": False,
    "eventBacked": False,
}

_DEFAULT_EDITOR = {
    "recommendedRouteMode": "project-scoped",
    "loadStrategy": "chunk-batch",
    "supportsBlockCommands": False,
    "supportsSnapshots": False,
    "supportsEvents": False,
}

_DEFAULT_METADATA = {
    "source": DEFAULTS_SOURCE,
    "phase": "phase-1-dev-world-state",
    "description": (
        "Default in-memory project/universe/world-instance mapping for the "
        "first project-scoped chunk API."
    ),
}

_catalog_cache_lock = threading.RLock()
_default_world_state_catalog_cache: WorldStateCatalog | None = None
_default_settings_cache: "DefaultWorldStateSettings | None" = None
_default_status_cache: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DefaultWorldStateSettings:
    """
    Resolved settings for the default development world-state catalog.

    This object is created from environment variables and optional provider
    metadata. It is intentionally serializable and framework-neutral.
    """

    project_id: str = DEFAULT_PROJECT_ID
    project_slug: str = _DEFAULT_PROJECT_SLUG
    project_name: str = _DEFAULT_PROJECT_NAME

    universe_id: str = DEFAULT_UNIVERSE_ID
    universe_slug: str = _DEFAULT_UNIVERSE_SLUG
    universe_name: str = _DEFAULT_UNIVERSE_NAME

    instance_world_id: str = DEFAULT_INSTANCE_WORLD_ID
    instance_world_slug: str = _DEFAULT_INSTANCE_WORLD_SLUG
    instance_world_name: str = _DEFAULT_INSTANCE_WORLD_NAME

    template_id: str = DEFAULT_TEMPLATE_ID
    provider_world_id: str = DEFAULT_PROVIDER_WORLD_ID
    provider_id: str = DEFAULT_PROVIDER_ID

    world_type: str = DEFAULT_WORLD_TYPE
    world_role: str = WORLD_ROLE_DEFAULT_SPAWN
    world_scope: str = WORLD_SCOPE_PROJECT
    owner_type: str = OWNER_TYPE_PROJECT

    generator_type: str = DEFAULT_GENERATOR_TYPE
    generator_version: str = DEFAULT_GENERATOR_VERSION
    projection_type: str = DEFAULT_PROJECTION_TYPE
    topology_type: str = DEFAULT_TOPOLOGY_TYPE
    coordinate_system: str = DEFAULT_COORDINATE_SYSTEM

    chunk_size: int = DEFAULT_CHUNK_SIZE
    cell_size: float = DEFAULT_CELL_SIZE
    surface_y: int = DEFAULT_SURFACE_Y
    min_y: int = DEFAULT_MIN_Y
    max_y: int = DEFAULT_MAX_Y
    seed: str = DEFAULT_SEED

    block_registry_id: str = DEFAULT_BLOCK_REGISTRY_ID
    block_registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION

    spawn: Mapping[str, Any] = field(default_factory=lambda: copy.deepcopy(_DEFAULT_SPAWN))
    runtime: Mapping[str, Any] = field(default_factory=lambda: copy.deepcopy(_DEFAULT_RUNTIME))
    editor: Mapping[str, Any] = field(default_factory=lambda: copy.deepcopy(_DEFAULT_EDITOR))
    metadata: Mapping[str, Any] = field(default_factory=lambda: copy.deepcopy(_DEFAULT_METADATA))

    api_prefix: str = ""
    provider_enrichment_enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "universe_id", normalize_universe_id(self.universe_id))
        object.__setattr__(
            self,
            "instance_world_id",
            normalize_world_instance_id(self.instance_world_id),
        )
        object.__setattr__(
            self,
            "template_id",
            normalize_template_id(self.template_id),
        )
        object.__setattr__(
            self,
            "provider_world_id",
            normalize_provider_world_id(
                self.provider_world_id,
                fallback=self.template_id,
            ),
        )

        provider_id = _coerce_string(self.provider_id, fallback=self.provider_world_id)
        object.__setattr__(self, "provider_id", provider_id)

        object.__setattr__(
            self,
            "chunk_size",
            normalize_chunk_size(self.chunk_size),
        )
        object.__setattr__(
            self,
            "cell_size",
            normalize_cell_size(self.cell_size),
        )

        surface_y, min_y, max_y = normalize_vertical_bounds(
            surface_y=self.surface_y,
            min_y=self.min_y,
            max_y=self.max_y,
        )
        object.__setattr__(self, "surface_y", surface_y)
        object.__setattr__(self, "min_y", min_y)
        object.__setattr__(self, "max_y", max_y)

        object.__setattr__(self, "project_slug", _coerce_string(self.project_slug, fallback=self.project_id))
        object.__setattr__(self, "project_name", _coerce_string(self.project_name, fallback=self.project_slug))
        object.__setattr__(self, "universe_slug", _coerce_string(self.universe_slug, fallback=self.universe_id))
        object.__setattr__(self, "universe_name", _coerce_string(self.universe_name, fallback=self.universe_slug))
        object.__setattr__(self, "instance_world_slug", _coerce_string(self.instance_world_slug, fallback=self.instance_world_id))
        object.__setattr__(self, "instance_world_name", _coerce_string(self.instance_world_name, fallback=self.instance_world_slug))

        object.__setattr__(self, "generator_type", _coerce_string(self.generator_type, fallback=DEFAULT_GENERATOR_TYPE))
        object.__setattr__(self, "generator_version", _coerce_string(self.generator_version, fallback=DEFAULT_GENERATOR_VERSION))
        object.__setattr__(self, "projection_type", _coerce_string(self.projection_type, fallback=DEFAULT_PROJECTION_TYPE))
        object.__setattr__(self, "topology_type", _coerce_string(self.topology_type, fallback=DEFAULT_TOPOLOGY_TYPE))
        object.__setattr__(self, "coordinate_system", _coerce_string(self.coordinate_system, fallback=DEFAULT_COORDINATE_SYSTEM))
        object.__setattr__(self, "seed", _coerce_string(self.seed, fallback=DEFAULT_SEED))
        object.__setattr__(self, "block_registry_id", _coerce_string(self.block_registry_id, fallback=DEFAULT_BLOCK_REGISTRY_ID))
        object.__setattr__(self, "block_registry_version", _coerce_string(self.block_registry_version, fallback=DEFAULT_BLOCK_REGISTRY_VERSION))

        object.__setattr__(self, "spawn", deep_copy_json(self.spawn or {}))
        object.__setattr__(self, "runtime", deep_copy_json(self.runtime or {}))
        object.__setattr__(self, "editor", deep_copy_json(self.editor or {}))
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))
        object.__setattr__(self, "api_prefix", _coerce_string(self.api_prefix, fallback="").rstrip("/"))
        object.__setattr__(self, "provider_enrichment_enabled", bool(self.provider_enrichment_enabled))

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def universeId(self) -> str:
        return self.universe_id

    @property
    def worldId(self) -> str:
        return self.instance_world_id

    @property
    def templateId(self) -> str:
        return self.template_id

    @property
    def providerWorldId(self) -> str:
        return self.provider_world_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "projectSlug": self.project_slug,
            "projectName": self.project_name,
            "universeId": self.universe_id,
            "universeSlug": self.universe_slug,
            "universeName": self.universe_name,
            "instanceWorldId": self.instance_world_id,
            "instanceWorldSlug": self.instance_world_slug,
            "instanceWorldName": self.instance_world_name,
            "templateId": self.template_id,
            "providerWorldId": self.provider_world_id,
            "providerId": self.provider_id,
            "worldType": self.world_type,
            "worldRole": self.world_role,
            "worldScope": self.world_scope,
            "ownerType": self.owner_type,
            "generatorType": self.generator_type,
            "generatorVersion": self.generator_version,
            "projectionType": self.projection_type,
            "topologyType": self.topology_type,
            "coordinateSystem": self.coordinate_system,
            "chunkSize": self.chunk_size,
            "cellSize": self.cell_size,
            "surfaceY": self.surface_y,
            "minY": self.min_y,
            "maxY": self.max_y,
            "seed": self.seed,
            "blockRegistryId": self.block_registry_id,
            "blockRegistryVersion": self.block_registry_version,
            "spawn": deep_copy_json(self.spawn),
            "runtime": deep_copy_json(self.runtime),
            "editor": deep_copy_json(self.editor),
            "metadata": deep_copy_json(self.metadata),
            "apiPrefix": self.api_prefix,
            "providerEnrichmentEnabled": self.provider_enrichment_enabled,
            "settingsHash": self.settings_hash(),
        }

    def settings_hash(self) -> str:
        return stable_hash(
            {
                "projectId": self.project_id,
                "universeId": self.universe_id,
                "instanceWorldId": self.instance_world_id,
                "templateId": self.template_id,
                "providerWorldId": self.provider_world_id,
                "generatorType": self.generator_type,
                "generatorVersion": self.generator_version,
                "projectionType": self.projection_type,
                "topologyType": self.topology_type,
                "coordinateSystem": self.coordinate_system,
                "chunkSize": self.chunk_size,
                "cellSize": self.cell_size,
                "surfaceY": self.surface_y,
                "minY": self.min_y,
                "maxY": self.max_y,
                "seed": self.seed,
                "blockRegistryId": self.block_registry_id,
                "blockRegistryVersion": self.block_registry_version,
            }
        )

    def copy_with(self, **changes: Any) -> "DefaultWorldStateSettings":
        return replace(self, **changes)


def _safe_exception_message(exc: BaseException) -> str:
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _coerce_string(value: Any, *, fallback: str = "") -> str:
    if value is None:
        return str(fallback)

    try:
        text = str(value)
    except Exception:
        text = repr(value)

    text = text.strip()
    return text if text else str(fallback)


def _coerce_bool(value: Any, *, fallback: bool = False) -> bool:
    if value is None:
        return bool(fallback)

    if isinstance(value, bool):
        return value

    text = _coerce_string(value).lower()

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False

    return bool(fallback)


def _coerce_int(value: Any, *, fallback: int) -> int:
    if value is None or value == "":
        return int(fallback)

    try:
        return int(value)
    except Exception:
        return int(fallback)


def _coerce_float(value: Any, *, fallback: float) -> float:
    if value is None or value == "":
        return float(fallback)

    try:
        return float(value)
    except Exception:
        return float(fallback)


def _get_env(name: str, fallback: Any = None) -> Any:
    try:
        value = os.environ.get(name)
    except Exception:
        return fallback

    if value is None:
        return fallback

    if isinstance(value, str) and value.strip() == "":
        return fallback

    return value


def _get_env_string(name: str, fallback: str) -> str:
    return _coerce_string(_get_env(name, fallback), fallback=fallback)


def _get_env_int(name: str, fallback: int) -> int:
    return _coerce_int(_get_env(name, fallback), fallback=fallback)


def _get_env_float(name: str, fallback: float) -> float:
    return _coerce_float(_get_env(name, fallback), fallback=fallback)


def _get_env_bool(name: str, fallback: bool) -> bool:
    return _coerce_bool(_get_env(name, fallback), fallback=fallback)


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            make_json_safe(value),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        return repr(value)


def _load_provider_world_definition(provider_world_id: str) -> tuple[Any | None, dict[str, Any]]:
    """
    Try to load provider/template world metadata from the existing `src.world`
    layer.

    Failure is non-fatal. Defaults must be usable even when the provider layer
    is incomplete during development.
    """
    diagnostics: dict[str, Any] = {
        "attempted": True,
        "providerWorldId": provider_world_id,
        "loaded": False,
        "source": None,
        "error": None,
    }

    try:
        from src.world.service import get_default_world_service  # type: ignore
    except Exception as exc:
        diagnostics["error"] = f"import_service_failed: {_safe_exception_message(exc)}"
        return None, diagnostics

    try:
        world_service = get_default_world_service()
    except Exception as exc:
        diagnostics["error"] = f"create_service_failed: {_safe_exception_message(exc)}"
        return None, diagnostics

    # Prefer the high-level metadata call if available.
    try:
        if hasattr(world_service, "get_world_metadata"):
            definition = world_service.get_world_metadata(provider_world_id)
            diagnostics["loaded"] = definition is not None
            diagnostics["source"] = "src.world.service.get_world_metadata"
            return definition, diagnostics
    except Exception as exc:
        diagnostics["error"] = f"get_world_metadata_failed: {_safe_exception_message(exc)}"

    # Fallback to loader-like method names if the service shape changes.
    try:
        if hasattr(world_service, "get_world_definition"):
            definition = world_service.get_world_definition(provider_world_id)
            diagnostics["loaded"] = definition is not None
            diagnostics["source"] = "src.world.service.get_world_definition"
            return definition, diagnostics
    except Exception as exc:
        diagnostics["error"] = f"get_world_definition_failed: {_safe_exception_message(exc)}"

    try:
        from src.world.loader import get_world_definition  # type: ignore

        definition = get_world_definition(provider_world_id)
        diagnostics["loaded"] = definition is not None
        diagnostics["source"] = "src.world.loader.get_world_definition"
        diagnostics["error"] = None
        return definition, diagnostics
    except Exception as exc:
        diagnostics["error"] = f"loader_get_world_definition_failed: {_safe_exception_message(exc)}"
        return None, diagnostics


def _extract_world_definition_value(
    definition: Any,
    *names: str,
    fallback: Any = None,
) -> Any:
    """
    Extract an attribute or dictionary value from a provider WorldDefinition.

    Supports both snake_case and camelCase names.
    """
    if definition is None:
        return fallback

    for name in names:
        try:
            if isinstance(definition, Mapping) and name in definition:
                return definition[name]
        except Exception:
            pass

        try:
            if hasattr(definition, name):
                return getattr(definition, name)
        except Exception:
            pass

    return fallback


def _world_definition_to_safe_dict(definition: Any) -> dict[str, Any]:
    if definition is None:
        return {}

    if isinstance(definition, Mapping):
        return dict(make_json_safe(definition))

    if dataclasses.is_dataclass(definition):
        return dict(make_json_safe(dataclasses.asdict(definition)))

    if hasattr(definition, "to_dict") and callable(definition.to_dict):
        try:
            result = definition.to_dict()
            if isinstance(result, Mapping):
                return dict(make_json_safe(result))
        except Exception:
            pass

    result: dict[str, Any] = {}

    for attr in (
        "world_id",
        "worldId",
        "world_type",
        "worldType",
        "label",
        "name",
        "generator_type",
        "generatorType",
        "generator_version",
        "generatorVersion",
        "projection_type",
        "projectionType",
        "topology_type",
        "topologyType",
        "coordinate_system",
        "coordinateSystem",
        "chunk_size",
        "chunkSize",
        "cell_size",
        "cellSize",
        "surface_y",
        "surfaceY",
        "min_y",
        "minY",
        "max_y",
        "maxY",
        "seed",
        "block_registry_id",
        "blockRegistryId",
        "block_registry_version",
        "blockRegistryVersion",
        "spawn",
        "runtime",
        "editor",
        "metadata",
    ):
        try:
            if hasattr(definition, attr):
                result[attr] = make_json_safe(getattr(definition, attr))
        except Exception:
            continue

    return result


def _apply_provider_definition_to_settings(
    settings: DefaultWorldStateSettings,
    definition: Any,
    diagnostics: Mapping[str, Any] | None = None,
) -> DefaultWorldStateSettings:
    """
    Enrich default world-instance settings from provider/template metadata.

    This keeps the concrete instance id (`world_spawn`) separate from the
    provider id (`flat`).
    """
    if definition is None:
        return settings

    provider_metadata = _world_definition_to_safe_dict(definition)

    generator_type = _extract_world_definition_value(
        definition,
        "generator_type",
        "generatorType",
        fallback=settings.generator_type,
    )
    generator_version = _extract_world_definition_value(
        definition,
        "generator_version",
        "generatorVersion",
        fallback=settings.generator_version,
    )
    projection_type = _extract_world_definition_value(
        definition,
        "projection_type",
        "projectionType",
        fallback=settings.projection_type,
    )
    topology_type = _extract_world_definition_value(
        definition,
        "topology_type",
        "topologyType",
        fallback=settings.topology_type,
    )
    coordinate_system = _extract_world_definition_value(
        definition,
        "coordinate_system",
        "coordinateSystem",
        fallback=settings.coordinate_system,
    )
    chunk_size = _extract_world_definition_value(
        definition,
        "chunk_size",
        "chunkSize",
        fallback=settings.chunk_size,
    )
    cell_size = _extract_world_definition_value(
        definition,
        "cell_size",
        "cellSize",
        fallback=settings.cell_size,
    )
    surface_y = _extract_world_definition_value(
        definition,
        "surface_y",
        "surfaceY",
        fallback=settings.surface_y,
    )
    min_y = _extract_world_definition_value(
        definition,
        "min_y",
        "minY",
        fallback=settings.min_y,
    )
    max_y = _extract_world_definition_value(
        definition,
        "max_y",
        "maxY",
        fallback=settings.max_y,
    )
    seed = _extract_world_definition_value(
        definition,
        "seed",
        fallback=settings.seed,
    )
    block_registry_id = _extract_world_definition_value(
        definition,
        "block_registry_id",
        "blockRegistryId",
        fallback=settings.block_registry_id,
    )
    block_registry_version = _extract_world_definition_value(
        definition,
        "block_registry_version",
        "blockRegistryVersion",
        fallback=settings.block_registry_version,
    )

    provider_runtime = _extract_world_definition_value(
        definition,
        "runtime",
        fallback={},
    )
    provider_editor = _extract_world_definition_value(
        definition,
        "editor",
        fallback={},
    )
    provider_spawn = _extract_world_definition_value(
        definition,
        "spawn",
        fallback={},
    )

    metadata = merge_metadata(
        settings.metadata,
        {
            "providerDefinitionLoaded": True,
            "providerDefinitionSource": (
                diagnostics or {}
            ).get("source") if isinstance(diagnostics, Mapping) else None,
            "providerDefinition": provider_metadata,
        },
    )

    return settings.copy_with(
        generator_type=_coerce_string(generator_type, fallback=settings.generator_type),
        generator_version=_coerce_string(generator_version, fallback=settings.generator_version),
        projection_type=_coerce_string(projection_type, fallback=settings.projection_type),
        topology_type=_coerce_string(topology_type, fallback=settings.topology_type),
        coordinate_system=_coerce_string(coordinate_system, fallback=settings.coordinate_system),
        chunk_size=_coerce_int(chunk_size, fallback=settings.chunk_size),
        cell_size=_coerce_float(cell_size, fallback=settings.cell_size),
        surface_y=_coerce_int(surface_y, fallback=settings.surface_y),
        min_y=_coerce_int(min_y, fallback=settings.min_y),
        max_y=_coerce_int(max_y, fallback=settings.max_y),
        seed=_coerce_string(seed, fallback=settings.seed),
        block_registry_id=_coerce_string(block_registry_id, fallback=settings.block_registry_id),
        block_registry_version=_coerce_string(block_registry_version, fallback=settings.block_registry_version),
        spawn=merge_metadata(provider_spawn if isinstance(provider_spawn, Mapping) else {}, settings.spawn),
        runtime=merge_metadata(provider_runtime if isinstance(provider_runtime, Mapping) else {}, settings.runtime),
        editor=merge_metadata(provider_editor if isinstance(provider_editor, Mapping) else {}, settings.editor),
        metadata=metadata,
    )


def _create_spawn_from_env() -> dict[str, Any]:
    spawn = copy.deepcopy(_DEFAULT_SPAWN)

    spawn["position"]["x"] = _get_env_float(
        ENV_DEFAULT_SPAWN_X,
        float(spawn["position"]["x"]),
    )
    spawn["position"]["y"] = _get_env_float(
        ENV_DEFAULT_SPAWN_Y,
        float(spawn["position"]["y"]),
    )
    spawn["position"]["z"] = _get_env_float(
        ENV_DEFAULT_SPAWN_Z,
        float(spawn["position"]["z"]),
    )
    spawn["rotation"]["yaw"] = _get_env_float(
        ENV_DEFAULT_SPAWN_YAW,
        float(spawn["rotation"]["yaw"]),
    )
    spawn["rotation"]["pitch"] = _get_env_float(
        ENV_DEFAULT_SPAWN_PITCH,
        float(spawn["rotation"]["pitch"]),
    )

    return spawn


def create_default_world_state_settings(
    *,
    refresh: bool = False,
    enrich_from_provider: bool | None = None,
) -> DefaultWorldStateSettings:
    """
    Create settings for the default development world-state catalog.

    Args:
        refresh:
            Re-read environment and provider metadata even if settings are
            cached.
        enrich_from_provider:
            If true, try to load metadata from `src.world` provider world.
            If false, use only env/static defaults.
            If None, env `VECTOPLAN_CHUNK_DISABLE_PROVIDER_ENRICHMENT`
            controls the behavior.
    """
    global _default_settings_cache

    with _catalog_cache_lock:
        if _default_settings_cache is not None and not refresh:
            return copy.deepcopy(_default_settings_cache)

        legacy_default_world_id = _get_env(
            LEGACY_ENV_DEFAULT_WORLD_ID,
            DEFAULT_PROVIDER_WORLD_ID,
        )

        provider_world_id = _get_env_string(
            ENV_DEFAULT_PROVIDER_WORLD_ID,
            _coerce_string(legacy_default_world_id, fallback=DEFAULT_PROVIDER_WORLD_ID),
        )

        template_id = _get_env_string(
            ENV_DEFAULT_WORLD_TEMPLATE_ID,
            provider_world_id or DEFAULT_TEMPLATE_ID,
        )

        settings = DefaultWorldStateSettings(
            project_id=_get_env_string(ENV_DEFAULT_PROJECT_ID, DEFAULT_PROJECT_ID),
            project_slug=_get_env_string(ENV_DEFAULT_PROJECT_SLUG, _DEFAULT_PROJECT_SLUG),
            project_name=_get_env_string(ENV_DEFAULT_PROJECT_NAME, _DEFAULT_PROJECT_NAME),
            universe_id=_get_env_string(ENV_DEFAULT_UNIVERSE_ID, DEFAULT_UNIVERSE_ID),
            universe_slug=_get_env_string(ENV_DEFAULT_UNIVERSE_SLUG, _DEFAULT_UNIVERSE_SLUG),
            universe_name=_get_env_string(ENV_DEFAULT_UNIVERSE_NAME, _DEFAULT_UNIVERSE_NAME),
            instance_world_id=_get_env_string(
                ENV_DEFAULT_INSTANCE_WORLD_ID,
                DEFAULT_INSTANCE_WORLD_ID,
            ),
            instance_world_slug=_get_env_string(
                ENV_DEFAULT_INSTANCE_WORLD_SLUG,
                _DEFAULT_INSTANCE_WORLD_SLUG,
            ),
            instance_world_name=_get_env_string(
                ENV_DEFAULT_INSTANCE_WORLD_NAME,
                _DEFAULT_INSTANCE_WORLD_NAME,
            ),
            template_id=template_id,
            provider_world_id=provider_world_id,
            provider_id=_get_env_string(
                ENV_DEFAULT_PROVIDER_ID,
                provider_world_id or DEFAULT_PROVIDER_ID,
            ),
            world_type=_get_env_string(ENV_DEFAULT_WORLD_TYPE, DEFAULT_WORLD_TYPE),
            world_role=_get_env_string(ENV_DEFAULT_WORLD_ROLE, WORLD_ROLE_DEFAULT_SPAWN),
            world_scope=_get_env_string(ENV_DEFAULT_WORLD_SCOPE, WORLD_SCOPE_PROJECT),
            owner_type=_get_env_string(ENV_DEFAULT_OWNER_TYPE, OWNER_TYPE_PROJECT),
            generator_type=_get_env_string(ENV_DEFAULT_GENERATOR_TYPE, DEFAULT_GENERATOR_TYPE),
            generator_version=_get_env_string(ENV_DEFAULT_GENERATOR_VERSION, DEFAULT_GENERATOR_VERSION),
            projection_type=_get_env_string(ENV_DEFAULT_PROJECTION_TYPE, DEFAULT_PROJECTION_TYPE),
            topology_type=_get_env_string(ENV_DEFAULT_TOPOLOGY_TYPE, DEFAULT_TOPOLOGY_TYPE),
            coordinate_system=_get_env_string(ENV_DEFAULT_COORDINATE_SYSTEM, DEFAULT_COORDINATE_SYSTEM),
            chunk_size=_get_env_int(ENV_DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_SIZE),
            cell_size=_get_env_float(ENV_DEFAULT_CELL_SIZE, DEFAULT_CELL_SIZE),
            surface_y=_get_env_int(ENV_DEFAULT_SURFACE_Y, DEFAULT_SURFACE_Y),
            min_y=_get_env_int(ENV_DEFAULT_MIN_Y, DEFAULT_MIN_Y),
            max_y=_get_env_int(ENV_DEFAULT_MAX_Y, DEFAULT_MAX_Y),
            seed=_get_env_string(ENV_DEFAULT_SEED, DEFAULT_SEED),
            block_registry_id=_get_env_string(
                ENV_DEFAULT_BLOCK_REGISTRY_ID,
                DEFAULT_BLOCK_REGISTRY_ID,
            ),
            block_registry_version=_get_env_string(
                ENV_DEFAULT_BLOCK_REGISTRY_VERSION,
                DEFAULT_BLOCK_REGISTRY_VERSION,
            ),
            spawn=_create_spawn_from_env(),
            runtime=copy.deepcopy(_DEFAULT_RUNTIME),
            editor=copy.deepcopy(_DEFAULT_EDITOR),
            metadata=merge_metadata(
                _DEFAULT_METADATA,
                {
                    "createdAt": utc_now_iso(),
                    "legacyDefaultWorldId": _coerce_string(legacy_default_world_id),
                },
            ),
            api_prefix=_get_env_string(ENV_DEFAULT_BOOTSTRAP_API_PREFIX, ""),
            provider_enrichment_enabled=not _get_env_bool(
                ENV_DISABLE_PROVIDER_ENRICHMENT,
                False,
            ),
        )

        if enrich_from_provider is not None:
            settings = settings.copy_with(
                provider_enrichment_enabled=bool(enrich_from_provider),
            )

        provider_diagnostics: dict[str, Any] = {
            "attempted": False,
            "loaded": False,
            "error": None,
        }

        if settings.provider_enrichment_enabled:
            definition, provider_diagnostics = _load_provider_world_definition(
                settings.provider_world_id,
            )
            if definition is not None:
                settings = _apply_provider_definition_to_settings(
                    settings,
                    definition,
                    provider_diagnostics,
                )
            else:
                settings = settings.copy_with(
                    metadata=merge_metadata(
                        settings.metadata,
                        {
                            "providerDefinitionLoaded": False,
                            "providerDiagnostics": provider_diagnostics,
                        },
                    )
                )
        else:
            settings = settings.copy_with(
                metadata=merge_metadata(
                    settings.metadata,
                    {
                        "providerDefinitionLoaded": False,
                        "providerEnrichmentDisabled": True,
                    },
                )
            )

        _default_settings_cache = copy.deepcopy(settings)
        return copy.deepcopy(settings)


def create_default_project_context(
    settings: DefaultWorldStateSettings | None = None,
) -> ProjectRuntimeContext:
    """
    Create the default development project context.
    """
    resolved = settings or create_default_world_state_settings()

    return ProjectRuntimeContext(
        project_id=resolved.project_id,
        slug=resolved.project_slug,
        name=resolved.project_name,
        default_universe_id=resolved.universe_id,
        status=DEFAULT_STATUS_ACTIVE,
        owner_user_id=None,
        metadata={
            "source": DEFAULTS_SOURCE,
            "schemaVersion": PROJECT_CONTEXT_SCHEMA_VERSION,
            "createdBy": "default-world-state",
        },
    )


def create_default_universe_context(
    settings: DefaultWorldStateSettings | None = None,
) -> UniverseRuntimeContext:
    """
    Create the default development universe context.
    """
    resolved = settings or create_default_world_state_settings()

    return UniverseRuntimeContext(
        universe_id=resolved.universe_id,
        project_id=resolved.project_id,
        slug=resolved.universe_slug,
        name=resolved.universe_name,
        default_world_id=resolved.instance_world_id,
        spawn_world_id=resolved.instance_world_id,
        status=DEFAULT_STATUS_ACTIVE,
        metadata={
            "source": DEFAULTS_SOURCE,
            "schemaVersion": UNIVERSE_CONTEXT_SCHEMA_VERSION,
            "createdBy": "default-world-state",
            "containsMultipleWorlds": False,
            "canContainMultipleWorldsLater": True,
        },
    )


def create_default_world_instance(
    settings: DefaultWorldStateSettings | None = None,
) -> WorldInstanceDefinition:
    """
    Create the default concrete world instance.

    This world is the concrete runtime world used by project-scoped routes.
    It is backed by the provider/template world `flat`.
    """
    resolved = settings or create_default_world_state_settings()

    metadata = merge_metadata(
        resolved.metadata,
        {
            "source": DEFAULTS_SOURCE,
            "schemaVersion": WORLD_INSTANCE_SCHEMA_VERSION,
            "isConcreteWorldInstance": True,
            "isProviderTemplate": False,
            "templateId": resolved.template_id,
            "providerWorldId": resolved.provider_world_id,
            "routingInvariant": (
                "productive routes use worldId; provider generation uses "
                "providerWorldId"
            ),
        },
    )

    runtime = merge_metadata(
        resolved.runtime,
        {
            "concreteWorldId": resolved.instance_world_id,
            "templateId": resolved.template_id,
            "providerWorldId": resolved.provider_world_id,
        },
    )

    editor = merge_metadata(
        resolved.editor,
        {
            "bootstrapRequired": True,
            "projectScopedRoutes": True,
        },
    )

    return WorldInstanceDefinition(
        world_id=resolved.instance_world_id,
        universe_id=resolved.universe_id,
        project_id=resolved.project_id,
        slug=resolved.instance_world_slug,
        name=resolved.instance_world_name,
        template_id=resolved.template_id,
        provider_world_id=resolved.provider_world_id,
        provider_id=resolved.provider_id,
        world_type=resolved.world_type,
        world_role=resolved.world_role,
        world_scope=resolved.world_scope,
        owner_type=resolved.owner_type,
        owner_id=resolved.project_id,
        generator_type=resolved.generator_type,
        generator_version=resolved.generator_version,
        projection_type=resolved.projection_type,
        topology_type=resolved.topology_type,
        coordinate_system=resolved.coordinate_system,
        chunk_size=resolved.chunk_size,
        cell_size=resolved.cell_size,
        surface_y=resolved.surface_y,
        min_y=resolved.min_y,
        max_y=resolved.max_y,
        seed=resolved.seed,
        block_registry_id=resolved.block_registry_id,
        block_registry_version=resolved.block_registry_version,
        status=DEFAULT_STATUS_ACTIVE,
        spawn=resolved.spawn,
        runtime=runtime,
        editor=editor,
        metadata=metadata,
    )


def create_default_world_state_catalog(
    *,
    refresh_settings: bool = False,
    enrich_from_provider: bool | None = None,
) -> WorldStateCatalog:
    """
    Create the default in-memory world-state catalog.

    This is the current phase-1 substitute for PostgreSQL-backed state.
    """
    settings = create_default_world_state_settings(
        refresh=refresh_settings,
        enrich_from_provider=enrich_from_provider,
    )

    project = create_default_project_context(settings)
    universe = create_default_universe_context(settings)
    world = create_default_world_instance(settings)

    return WorldStateCatalog(
        projects=(project,),
        universes=(universe,),
        worlds=(world,),
        default_project_id=settings.project_id,
        metadata={
            "source": DEFAULTS_SOURCE,
            "moduleVersion": DEFAULTS_MODULE_VERSION,
            "createdAt": utc_now_iso(),
            "phase": "phase-1-dev-world-state",
            "storage": "in-memory",
            "persistent": False,
            "projectEqualsUniverseContainer": True,
            "universeCanContainMultipleWorlds": True,
            "currentWorldCount": 1,
            "defaultMapping": {
                "projectId": settings.project_id,
                "universeId": settings.universe_id,
                "worldId": settings.instance_world_id,
                "templateId": settings.template_id,
                "providerWorldId": settings.provider_world_id,
            },
            "settingsHash": settings.settings_hash(),
        },
    )


def get_default_world_state_catalog(
    *,
    refresh: bool = False,
    enrich_from_provider: bool | None = None,
) -> WorldStateCatalog:
    """
    Return the cached default world-state catalog.

    Use `refresh=True` after changing environment variables or provider config.
    """
    global _default_world_state_catalog_cache

    with _catalog_cache_lock:
        if _default_world_state_catalog_cache is not None and not refresh:
            return copy.deepcopy(_default_world_state_catalog_cache)

        catalog = create_default_world_state_catalog(
            refresh_settings=refresh,
            enrich_from_provider=enrich_from_provider,
        )

        _default_world_state_catalog_cache = copy.deepcopy(catalog)
        return copy.deepcopy(catalog)


def reset_default_world_state_catalog_cache() -> None:
    """
    Reset cached settings, catalog and status diagnostics.
    """
    global _default_world_state_catalog_cache
    global _default_settings_cache
    global _default_status_cache

    with _catalog_cache_lock:
        _default_world_state_catalog_cache = None
        _default_settings_cache = None
        _default_status_cache = None


def get_default_world_state_settings(
    *,
    refresh: bool = False,
    enrich_from_provider: bool | None = None,
) -> DefaultWorldStateSettings:
    """
    Public accessor for resolved default settings.
    """
    return create_default_world_state_settings(
        refresh=refresh,
        enrich_from_provider=enrich_from_provider,
    )


def get_default_world_state_ids(
    *,
    refresh: bool = False,
) -> dict[str, str]:
    """
    Return the primary default IDs used by the world-state layer.
    """
    settings = get_default_world_state_settings(refresh=refresh)

    return {
        "projectId": settings.project_id,
        "universeId": settings.universe_id,
        "worldId": settings.instance_world_id,
        "templateId": settings.template_id,
        "providerWorldId": settings.provider_world_id,
        "providerId": settings.provider_id,
    }


def get_default_world_state_status(
    *,
    refresh: bool = False,
    include_catalog: bool = False,
) -> dict[str, Any]:
    """
    Return JSON-safe diagnostics for the default world-state catalog.
    """
    global _default_status_cache

    with _catalog_cache_lock:
        if _default_status_cache is not None and not refresh and not include_catalog:
            return copy.deepcopy(_default_status_cache)

        status: dict[str, Any] = {
            "ok": False,
            "module": "src.world_state.defaults",
            "moduleVersion": DEFAULTS_MODULE_VERSION,
            "source": DEFAULTS_SOURCE,
            "error": None,
            "settings": None,
            "catalogHash": None,
            "counts": {
                "projects": 0,
                "universes": 0,
                "worlds": 0,
            },
            "ids": None,
            "catalog": None,
        }

        try:
            settings = get_default_world_state_settings(refresh=refresh)
            catalog = get_default_world_state_catalog(refresh=refresh)

            status["ok"] = True
            status["settings"] = settings.to_dict()
            status["catalogHash"] = catalog.catalog_hash()
            status["counts"] = {
                "projects": len(catalog.projects),
                "universes": len(catalog.universes),
                "worlds": len(catalog.worlds),
            }
            status["ids"] = get_default_world_state_ids(refresh=False)

            if include_catalog:
                status["catalog"] = catalog.to_dict()

        except Exception as exc:
            status["ok"] = False
            status["error"] = {
                "type": exc.__class__.__name__,
                "message": _safe_exception_message(exc),
            }

        safe_status = make_json_safe(status)

        if not include_catalog:
            _default_status_cache = copy.deepcopy(safe_status)

        return copy.deepcopy(safe_status)


def create_default_world_state_summary(
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """
    Return a compact summary useful for logs, health checks and route metadata.
    """
    status = get_default_world_state_status(refresh=refresh, include_catalog=False)

    return {
        "ok": bool(status.get("ok")),
        "source": DEFAULTS_SOURCE,
        "ids": status.get("ids"),
        "counts": status.get("counts"),
        "catalogHash": status.get("catalogHash"),
        "error": status.get("error"),
    }


def export_default_world_state_catalog_json(
    *,
    refresh: bool = False,
    indent: int | None = 2,
) -> str:
    """
    Export the default catalog as JSON string.

    Useful for debugging and snapshotting expected dev-state behavior.
    """
    catalog = get_default_world_state_catalog(refresh=refresh)

    return json.dumps(
        catalog.to_dict(),
        sort_keys=True,
        ensure_ascii=False,
        indent=indent,
    )


def assert_default_world_state_ready(*, refresh: bool = False) -> WorldStateCatalog:
    """
    Validate and return the default catalog.

    Raises RuntimeError if the default world state cannot be created.
    """
    try:
        catalog = get_default_world_state_catalog(refresh=refresh)
    except Exception as exc:
        raise RuntimeError(
            f"Default world-state catalog is not ready: {_safe_exception_message(exc)}"
        ) from exc

    if not catalog.projects or not catalog.universes or not catalog.worlds:
        raise RuntimeError(
            "Default world-state catalog is not ready: missing project, universe or world."
        )

    return catalog


__all__ = (
    "DEFAULTS_MODULE_VERSION",
    "DEFAULTS_SOURCE",
    "ENV_DEFAULT_PROJECT_ID",
    "ENV_DEFAULT_PROJECT_SLUG",
    "ENV_DEFAULT_PROJECT_NAME",
    "ENV_DEFAULT_UNIVERSE_ID",
    "ENV_DEFAULT_UNIVERSE_SLUG",
    "ENV_DEFAULT_UNIVERSE_NAME",
    "ENV_DEFAULT_INSTANCE_WORLD_ID",
    "ENV_DEFAULT_INSTANCE_WORLD_SLUG",
    "ENV_DEFAULT_INSTANCE_WORLD_NAME",
    "ENV_DEFAULT_WORLD_TEMPLATE_ID",
    "ENV_DEFAULT_PROVIDER_WORLD_ID",
    "ENV_DEFAULT_PROVIDER_ID",
    "ENV_DEFAULT_WORLD_TYPE",
    "ENV_DEFAULT_WORLD_ROLE",
    "ENV_DEFAULT_WORLD_SCOPE",
    "ENV_DEFAULT_OWNER_TYPE",
    "ENV_DEFAULT_GENERATOR_TYPE",
    "ENV_DEFAULT_GENERATOR_VERSION",
    "ENV_DEFAULT_PROJECTION_TYPE",
    "ENV_DEFAULT_TOPOLOGY_TYPE",
    "ENV_DEFAULT_COORDINATE_SYSTEM",
    "ENV_DEFAULT_CHUNK_SIZE",
    "ENV_DEFAULT_CELL_SIZE",
    "ENV_DEFAULT_SURFACE_Y",
    "ENV_DEFAULT_MIN_Y",
    "ENV_DEFAULT_MAX_Y",
    "ENV_DEFAULT_SEED",
    "ENV_DEFAULT_BLOCK_REGISTRY_ID",
    "ENV_DEFAULT_BLOCK_REGISTRY_VERSION",
    "ENV_DEFAULT_SPAWN_X",
    "ENV_DEFAULT_SPAWN_Y",
    "ENV_DEFAULT_SPAWN_Z",
    "ENV_DEFAULT_SPAWN_YAW",
    "ENV_DEFAULT_SPAWN_PITCH",
    "ENV_DEFAULT_BOOTSTRAP_API_PREFIX",
    "ENV_DISABLE_PROVIDER_ENRICHMENT",
    "LEGACY_ENV_DEFAULT_WORLD_ID",
    "DefaultWorldStateSettings",
    "create_default_world_state_settings",
    "create_default_project_context",
    "create_default_universe_context",
    "create_default_world_instance",
    "create_default_world_state_catalog",
    "get_default_world_state_catalog",
    "reset_default_world_state_catalog_cache",
    "get_default_world_state_settings",
    "get_default_world_state_ids",
    "get_default_world_state_status",
    "create_default_world_state_summary",
    "export_default_world_state_catalog_json",
    "assert_default_world_state_ready",
)