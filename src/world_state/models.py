# services/vectoplan-chunk/src/world_state/models.py
"""
Framework-neutral models for the VECTOPLAN world-state layer.

This module intentionally contains no Flask, SQLAlchemy or PostgreSQL code.

Layer meaning:

- `src.world.models`
  Describes generator/provider concepts such as WorldDefinition,
  ChunkRequest and GeneratedChunk.

- `src.world_state.models`
  Describes concrete runtime state:
  Project -> Universe -> WorldInstance -> Provider/Template World.

Important invariant:

    projectId / universeId / worldId identify a concrete runtime world.
    templateId / providerWorldId identify the generator/template behind it.

Example for phase 1:

    projectId       = dev-project
    universeId      = dev-universe
    worldId         = world_spawn
    templateId      = flat
    providerWorldId = flat

The editor should use project-scoped world routes. It should not treat the
provider/template id `flat` as the concrete project world id.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


WORLD_STATE_SCHEMA_VERSION = "world-state.schema.v1"
PROJECT_CONTEXT_SCHEMA_VERSION = "project-runtime-context.v1"
UNIVERSE_CONTEXT_SCHEMA_VERSION = "universe-runtime-context.v1"
WORLD_INSTANCE_SCHEMA_VERSION = "world-instance-definition.v1"
WORLD_RUNTIME_CONTEXT_SCHEMA_VERSION = "world-runtime-context.v1"
PROJECT_BOOTSTRAP_SCHEMA_VERSION = "project-bootstrap-context.v1"
WORLD_STATE_CATALOG_SCHEMA_VERSION = "world-state-catalog.v1"

DEFAULT_STATUS_ACTIVE = "active"
DEFAULT_STATUS_DRAFT = "draft"
DEFAULT_STATUS_ARCHIVED = "archived"

KNOWN_STATUSES: tuple[str, ...] = (
    DEFAULT_STATUS_ACTIVE,
    DEFAULT_STATUS_DRAFT,
    DEFAULT_STATUS_ARCHIVED,
    "disabled",
    "deleted",
)

WORLD_SCOPE_PROJECT = "project"
WORLD_SCOPE_ADMIN = "admin"
WORLD_SCOPE_SYSTEM = "system"
WORLD_SCOPE_SHARED = "shared"

KNOWN_WORLD_SCOPES: tuple[str, ...] = (
    WORLD_SCOPE_PROJECT,
    WORLD_SCOPE_ADMIN,
    WORLD_SCOPE_SYSTEM,
    WORLD_SCOPE_SHARED,
)

WORLD_ROLE_DEFAULT_SPAWN = "default_spawn"
WORLD_ROLE_SPAWN = "spawn"
WORLD_ROLE_MAIN = "main"
WORLD_ROLE_REAL_SITE = "real_site"
WORLD_ROLE_SANDBOX = "sandbox"
WORLD_ROLE_INTERIOR = "interior"
WORLD_ROLE_ADMIN = "admin"
WORLD_ROLE_IMPORTED = "imported"
WORLD_ROLE_SIMULATION = "simulation"

KNOWN_WORLD_ROLES: tuple[str, ...] = (
    WORLD_ROLE_DEFAULT_SPAWN,
    WORLD_ROLE_SPAWN,
    WORLD_ROLE_MAIN,
    WORLD_ROLE_REAL_SITE,
    WORLD_ROLE_SANDBOX,
    WORLD_ROLE_INTERIOR,
    WORLD_ROLE_ADMIN,
    WORLD_ROLE_IMPORTED,
    WORLD_ROLE_SIMULATION,
)

OWNER_TYPE_PROJECT = "project"
OWNER_TYPE_UNIVERSE = "universe"
OWNER_TYPE_ADMIN = "admin"
OWNER_TYPE_SYSTEM = "system"

KNOWN_OWNER_TYPES: tuple[str, ...] = (
    OWNER_TYPE_PROJECT,
    OWNER_TYPE_UNIVERSE,
    OWNER_TYPE_ADMIN,
    OWNER_TYPE_SYSTEM,
)

DEFAULT_WORLD_TYPE = "runtime-world"
DEFAULT_TEMPLATE_ID = "flat"
DEFAULT_PROVIDER_WORLD_ID = "flat"
DEFAULT_PROVIDER_ID = "flat"
DEFAULT_GENERATOR_TYPE = "flat-world"
DEFAULT_GENERATOR_VERSION = "1"
DEFAULT_PROJECTION_TYPE = "flat-local-v1"
DEFAULT_TOPOLOGY_TYPE = "flat-unbounded-v1"
DEFAULT_COORDINATE_SYSTEM = "vectoplan-world-y-up-v1"
DEFAULT_BLOCK_REGISTRY_ID = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION = "1"
DEFAULT_CHUNK_SIZE = 16
DEFAULT_CELL_SIZE = 1.0
DEFAULT_SURFACE_Y = 0
DEFAULT_MIN_Y = -8
DEFAULT_MAX_Y = 64
DEFAULT_SEED = "dev-seed"

DEFAULT_INSTANCE_WORLD_ID = "world_spawn"
DEFAULT_PROJECT_ID = "dev-project"
DEFAULT_UNIVERSE_ID = "dev-universe"

ID_MIN_LENGTH = 1
ID_MAX_LENGTH = 128
SLUG_MAX_LENGTH = 128
NAME_MAX_LENGTH = 256

_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SAFE_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ModelValidationError(ValueError):
    """
    Raised when a world-state model cannot be normalized or validated.

    The dedicated `world_state.errors` module can later wrap this into
    structured API errors. This class avoids importing the errors module here
    and therefore prevents circular dependencies.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_world_state_model",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "invalid_world_state_model")
        self.details = make_json_safe(dict(details or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


def make_json_safe(value: Any) -> Any:
    """
    Convert arbitrary values into JSON-safe values.

    This function is intentionally defensive. It is used in model `to_dict`
    methods and diagnostics.
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
        return float(value)

    if isinstance(value, (datetime, date)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)

    if isinstance(value, uuid.UUID):
        return str(value)

    if isinstance(value, Path):
        return str(value)

    if dataclasses.is_dataclass(value):
        if hasattr(value, "to_dict") and callable(value.to_dict):
            return value.to_dict()
        return dataclasses.asdict(value)

    if isinstance(value, Mapping):
        safe_dict: dict[str, Any] = {}
        for key, item in value.items():
            try:
                safe_key = str(key)
            except Exception:
                safe_key = repr(key)
            safe_dict[safe_key] = make_json_safe(item)
        return safe_dict

    if isinstance(value, (list, tuple, set, frozenset)):
        return [make_json_safe(item) for item in value]

    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def utc_now_iso() -> str:
    """
    Return the current UTC timestamp as ISO-8601 string.
    """
    return datetime.now(timezone.utc).isoformat()


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _coerce_string(value: Any, *, fallback: str | None = None) -> str:
    if value is None:
        if fallback is None:
            return ""
        return str(fallback)

    try:
        text = str(value)
    except Exception:
        text = repr(value)

    text = text.strip()

    if not text and fallback is not None:
        return str(fallback)

    return text


def _coerce_optional_string(value: Any) -> str | None:
    text = _coerce_string(value)
    return text or None


def _coerce_int(value: Any, *, fallback: int, field_name: str) -> int:
    if value is None or value == "":
        return int(fallback)

    try:
        return int(value)
    except Exception as exc:
        raise ModelValidationError(
            f"Invalid integer value for {field_name}.",
            code="invalid_integer",
            details={
                "field": field_name,
                "value": make_json_safe(value),
            },
        ) from exc


def _coerce_float(value: Any, *, fallback: float, field_name: str) -> float:
    if value is None or value == "":
        return float(fallback)

    try:
        result = float(value)
    except Exception as exc:
        raise ModelValidationError(
            f"Invalid numeric value for {field_name}.",
            code="invalid_number",
            details={
                "field": field_name,
                "value": make_json_safe(value),
            },
        ) from exc

    if math.isnan(result) or math.isinf(result):
        raise ModelValidationError(
            f"Invalid finite numeric value for {field_name}.",
            code="invalid_number",
            details={
                "field": field_name,
                "value": make_json_safe(value),
            },
        )

    return result


def _get_first(
    data: Mapping[str, Any],
    *keys: str,
    fallback: Any = None,
) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return fallback


def _normalize_id(
    value: Any,
    *,
    field_name: str,
    fallback: str | None = None,
    allow_empty: bool = False,
) -> str:
    text = _coerce_string(value, fallback=fallback)

    if not text:
        if allow_empty:
            return ""
        raise ModelValidationError(
            f"{field_name} is required.",
            code="missing_id",
            details={"field": field_name},
        )

    if len(text) < ID_MIN_LENGTH or len(text) > ID_MAX_LENGTH:
        raise ModelValidationError(
            f"{field_name} length is invalid.",
            code="invalid_id_length",
            details={
                "field": field_name,
                "value": text,
                "minLength": ID_MIN_LENGTH,
                "maxLength": ID_MAX_LENGTH,
            },
        )

    if not _SAFE_ID_PATTERN.match(text):
        raise ModelValidationError(
            f"{field_name} contains invalid characters.",
            code="invalid_id",
            details={
                "field": field_name,
                "value": text,
                "allowedPattern": _SAFE_ID_PATTERN.pattern,
            },
        )

    return text


def _normalize_slug(
    value: Any,
    *,
    field_name: str,
    fallback: str | None = None,
    allow_empty: bool = False,
) -> str:
    text = _coerce_string(value, fallback=fallback).lower()

    if not text:
        if allow_empty:
            return ""
        raise ModelValidationError(
            f"{field_name} is required.",
            code="missing_slug",
            details={"field": field_name},
        )

    if len(text) > SLUG_MAX_LENGTH:
        raise ModelValidationError(
            f"{field_name} length is invalid.",
            code="invalid_slug_length",
            details={
                "field": field_name,
                "value": text,
                "maxLength": SLUG_MAX_LENGTH,
            },
        )

    if not _SAFE_SLUG_PATTERN.match(text):
        raise ModelValidationError(
            f"{field_name} contains invalid characters.",
            code="invalid_slug",
            details={
                "field": field_name,
                "value": text,
                "allowedPattern": _SAFE_SLUG_PATTERN.pattern,
            },
        )

    return text


def _normalize_name(
    value: Any,
    *,
    fallback: str,
    field_name: str = "name",
) -> str:
    text = _coerce_string(value, fallback=fallback)

    if len(text) > NAME_MAX_LENGTH:
        raise ModelValidationError(
            f"{field_name} length is invalid.",
            code="invalid_name_length",
            details={
                "field": field_name,
                "value": text,
                "maxLength": NAME_MAX_LENGTH,
            },
        )

    return text


def _normalize_status(value: Any, *, fallback: str = DEFAULT_STATUS_ACTIVE) -> str:
    status = _coerce_string(value, fallback=fallback).lower()

    if status not in KNOWN_STATUSES:
        raise ModelValidationError(
            "Unknown status.",
            code="unknown_status",
            details={
                "status": status,
                "knownStatuses": list(KNOWN_STATUSES),
            },
        )

    return status


def _normalize_world_scope(value: Any) -> str:
    scope = _coerce_string(value, fallback=WORLD_SCOPE_PROJECT).lower()

    if scope not in KNOWN_WORLD_SCOPES:
        raise ModelValidationError(
            "Unknown world scope.",
            code="unknown_world_scope",
            details={
                "worldScope": scope,
                "knownWorldScopes": list(KNOWN_WORLD_SCOPES),
            },
        )

    return scope


def _normalize_world_role(value: Any) -> str:
    role = _coerce_string(value, fallback=WORLD_ROLE_DEFAULT_SPAWN).lower()

    if role not in KNOWN_WORLD_ROLES:
        raise ModelValidationError(
            "Unknown world role.",
            code="unknown_world_role",
            details={
                "worldRole": role,
                "knownWorldRoles": list(KNOWN_WORLD_ROLES),
            },
        )

    return role


def _normalize_owner_type(value: Any) -> str:
    owner_type = _coerce_string(value, fallback=OWNER_TYPE_PROJECT).lower()

    if owner_type not in KNOWN_OWNER_TYPES:
        raise ModelValidationError(
            "Unknown owner type.",
            code="unknown_owner_type",
            details={
                "ownerType": owner_type,
                "knownOwnerTypes": list(KNOWN_OWNER_TYPES),
            },
        )

    return owner_type


def normalize_project_id(value: Any, *, fallback: str | None = None) -> str:
    return _normalize_id(
        value,
        field_name="projectId",
        fallback=fallback,
    )


def normalize_universe_id(value: Any, *, fallback: str | None = None) -> str:
    return _normalize_id(
        value,
        field_name="universeId",
        fallback=fallback,
    )


def normalize_world_instance_id(value: Any, *, fallback: str | None = None) -> str:
    return _normalize_id(
        value,
        field_name="worldId",
        fallback=fallback,
    )


def normalize_template_id(value: Any, *, fallback: str | None = None) -> str:
    return _normalize_id(
        value,
        field_name="templateId",
        fallback=fallback,
    )


def normalize_provider_world_id(value: Any, *, fallback: str | None = None) -> str:
    return _normalize_id(
        value,
        field_name="providerWorldId",
        fallback=fallback,
    )


def normalize_chunk_size(value: Any, *, fallback: int = DEFAULT_CHUNK_SIZE) -> int:
    chunk_size = _coerce_int(
        value,
        fallback=fallback,
        field_name="chunkSize",
    )

    if chunk_size <= 0:
        raise ModelValidationError(
            "chunkSize must be greater than zero.",
            code="invalid_chunk_size",
            details={"chunkSize": chunk_size},
        )

    return chunk_size


def normalize_cell_size(value: Any, *, fallback: float = DEFAULT_CELL_SIZE) -> float:
    cell_size = _coerce_float(
        value,
        fallback=fallback,
        field_name="cellSize",
    )

    if cell_size <= 0:
        raise ModelValidationError(
            "cellSize must be greater than zero.",
            code="invalid_cell_size",
            details={"cellSize": cell_size},
        )

    return cell_size


def normalize_vertical_bounds(
    *,
    surface_y: Any = DEFAULT_SURFACE_Y,
    min_y: Any = DEFAULT_MIN_Y,
    max_y: Any = DEFAULT_MAX_Y,
) -> tuple[int, int, int]:
    surface_y_int = _coerce_int(
        surface_y,
        fallback=DEFAULT_SURFACE_Y,
        field_name="surfaceY",
    )
    min_y_int = _coerce_int(
        min_y,
        fallback=DEFAULT_MIN_Y,
        field_name="minY",
    )
    max_y_int = _coerce_int(
        max_y,
        fallback=DEFAULT_MAX_Y,
        field_name="maxY",
    )

    if min_y_int > max_y_int:
        raise ModelValidationError(
            "minY must not be greater than maxY.",
            code="invalid_vertical_bounds",
            details={
                "minY": min_y_int,
                "maxY": max_y_int,
            },
        )

    if surface_y_int < min_y_int or surface_y_int > max_y_int:
        raise ModelValidationError(
            "surfaceY must be between minY and maxY.",
            code="invalid_surface_y",
            details={
                "surfaceY": surface_y_int,
                "minY": min_y_int,
                "maxY": max_y_int,
            },
        )

    return surface_y_int, min_y_int, max_y_int


def stable_hash(value: Any) -> str:
    """
    Create a deterministic SHA-256 hash for JSON-compatible content.
    """
    safe_value = make_json_safe(value)

    try:
        payload = json.dumps(
            safe_value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except Exception:
        payload = repr(safe_value)

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deep_copy_json(value: Any) -> Any:
    """
    Return a defensive deep copy of JSON-like content.
    """
    return copy.deepcopy(make_json_safe(value))


@dataclass(frozen=True, slots=True)
class ProjectRuntimeContext:
    """
    Runtime context for a user-facing project.

    In the VECTOPLAN chunk-service concept, a project is treated as the
    container that owns one universe. The universe can then contain one or
    multiple concrete worlds.

    Phase 1:
        One project -> one universe -> one flat-spawn world.
    """

    project_id: str
    slug: str
    name: str
    default_universe_id: str
    status: str = DEFAULT_STATUS_ACTIVE
    owner_user_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PROJECT_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            normalize_project_id(self.project_id),
        )
        object.__setattr__(
            self,
            "slug",
            _normalize_slug(
                self.slug,
                field_name="slug",
                fallback=self.project_id,
            ),
        )
        object.__setattr__(
            self,
            "name",
            _normalize_name(
                self.name,
                fallback=self.slug or self.project_id,
            ),
        )
        object.__setattr__(
            self,
            "default_universe_id",
            normalize_universe_id(self.default_universe_id),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_status(self.status),
        )
        object.__setattr__(
            self,
            "owner_user_id",
            _coerce_optional_string(self.owner_user_id),
        )
        object.__setattr__(
            self,
            "metadata",
            deep_copy_json(self.metadata or {}),
        )

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def defaultUniverseId(self) -> str:
        return self.default_universe_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "projectId": self.project_id,
            "slug": self.slug,
            "name": self.name,
            "defaultUniverseId": self.default_universe_id,
            "status": self.status,
            "ownerUserId": self.owner_user_id,
            "metadata": deep_copy_json(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectRuntimeContext":
        return cls(
            project_id=_get_first(data, "projectId", "project_id"),
            slug=_get_first(data, "slug", fallback=""),
            name=_get_first(data, "name", "label", fallback=""),
            default_universe_id=_get_first(
                data,
                "defaultUniverseId",
                "default_universe_id",
                "universeId",
                "universe_id",
            ),
            status=_get_first(data, "status", fallback=DEFAULT_STATUS_ACTIVE),
            owner_user_id=_get_first(
                data,
                "ownerUserId",
                "owner_user_id",
                fallback=None,
            ),
            metadata=_get_first(data, "metadata", fallback={}),
            schema_version=_get_first(
                data,
                "schemaVersion",
                "schema_version",
                fallback=PROJECT_CONTEXT_SCHEMA_VERSION,
            ),
        )

    def copy_with(self, **changes: Any) -> "ProjectRuntimeContext":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class UniverseRuntimeContext:
    """
    Runtime context for a project universe.

    A universe is the technical container that can hold one or more concrete
    world instances. In phase 1 it contains only `world_spawn`.
    """

    universe_id: str
    project_id: str
    slug: str
    name: str
    default_world_id: str
    spawn_world_id: str
    status: str = DEFAULT_STATUS_ACTIVE
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = UNIVERSE_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "universe_id",
            normalize_universe_id(self.universe_id),
        )
        object.__setattr__(
            self,
            "project_id",
            normalize_project_id(self.project_id),
        )
        object.__setattr__(
            self,
            "slug",
            _normalize_slug(
                self.slug,
                field_name="slug",
                fallback=self.universe_id,
            ),
        )
        object.__setattr__(
            self,
            "name",
            _normalize_name(
                self.name,
                fallback=self.slug or self.universe_id,
            ),
        )
        object.__setattr__(
            self,
            "default_world_id",
            normalize_world_instance_id(self.default_world_id),
        )
        object.__setattr__(
            self,
            "spawn_world_id",
            normalize_world_instance_id(
                self.spawn_world_id,
                fallback=self.default_world_id,
            ),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_status(self.status),
        )
        object.__setattr__(
            self,
            "metadata",
            deep_copy_json(self.metadata or {}),
        )

    @property
    def universeId(self) -> str:
        return self.universe_id

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def defaultWorldId(self) -> str:
        return self.default_world_id

    @property
    def spawnWorldId(self) -> str:
        return self.spawn_world_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "universeId": self.universe_id,
            "projectId": self.project_id,
            "slug": self.slug,
            "name": self.name,
            "defaultWorldId": self.default_world_id,
            "spawnWorldId": self.spawn_world_id,
            "status": self.status,
            "metadata": deep_copy_json(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UniverseRuntimeContext":
        return cls(
            universe_id=_get_first(data, "universeId", "universe_id"),
            project_id=_get_first(data, "projectId", "project_id"),
            slug=_get_first(data, "slug", fallback=""),
            name=_get_first(data, "name", "label", fallback=""),
            default_world_id=_get_first(
                data,
                "defaultWorldId",
                "default_world_id",
                "worldId",
                "world_id",
            ),
            spawn_world_id=_get_first(
                data,
                "spawnWorldId",
                "spawn_world_id",
                "defaultWorldId",
                "default_world_id",
                "worldId",
                "world_id",
            ),
            status=_get_first(data, "status", fallback=DEFAULT_STATUS_ACTIVE),
            metadata=_get_first(data, "metadata", fallback={}),
            schema_version=_get_first(
                data,
                "schemaVersion",
                "schema_version",
                fallback=UNIVERSE_CONTEXT_SCHEMA_VERSION,
            ),
        )

    def copy_with(self, **changes: Any) -> "UniverseRuntimeContext":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class WorldInstanceDefinition:
    """
    Concrete world instance inside a universe.

    This is the key distinction:

        world_id
            Concrete runtime world id used by routes, editor, snapshots
            and events.

        template_id / provider_world_id
            Generator/template ids used to produce unmaterialized chunks.

    For phase 1:
        world_id         = world_spawn
        template_id      = flat
        provider_world_id = flat
    """

    world_id: str
    universe_id: str
    project_id: str
    slug: str
    name: str

    template_id: str = DEFAULT_TEMPLATE_ID
    provider_world_id: str = DEFAULT_PROVIDER_WORLD_ID
    provider_id: str = DEFAULT_PROVIDER_ID

    world_type: str = DEFAULT_WORLD_TYPE
    world_role: str = WORLD_ROLE_DEFAULT_SPAWN
    world_scope: str = WORLD_SCOPE_PROJECT

    owner_type: str = OWNER_TYPE_PROJECT
    owner_id: str | None = None

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

    status: str = DEFAULT_STATUS_ACTIVE
    spawn: Mapping[str, Any] = field(default_factory=dict)
    runtime: Mapping[str, Any] = field(default_factory=dict)
    editor: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = WORLD_INSTANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "world_id",
            normalize_world_instance_id(self.world_id),
        )
        object.__setattr__(
            self,
            "universe_id",
            normalize_universe_id(self.universe_id),
        )
        object.__setattr__(
            self,
            "project_id",
            normalize_project_id(self.project_id),
        )
        object.__setattr__(
            self,
            "slug",
            _normalize_slug(
                self.slug,
                field_name="slug",
                fallback=self.world_id,
            ),
        )
        object.__setattr__(
            self,
            "name",
            _normalize_name(
                self.name,
                fallback=self.slug or self.world_id,
            ),
        )
        object.__setattr__(
            self,
            "template_id",
            normalize_template_id(
                self.template_id,
                fallback=DEFAULT_TEMPLATE_ID,
            ),
        )
        object.__setattr__(
            self,
            "provider_world_id",
            normalize_provider_world_id(
                self.provider_world_id,
                fallback=self.template_id or DEFAULT_PROVIDER_WORLD_ID,
            ),
        )
        object.__setattr__(
            self,
            "provider_id",
            _normalize_id(
                self.provider_id,
                field_name="providerId",
                fallback=self.provider_world_id or DEFAULT_PROVIDER_ID,
            ),
        )
        object.__setattr__(
            self,
            "world_type",
            _coerce_string(self.world_type, fallback=DEFAULT_WORLD_TYPE),
        )
        object.__setattr__(
            self,
            "world_role",
            _normalize_world_role(self.world_role),
        )
        object.__setattr__(
            self,
            "world_scope",
            _normalize_world_scope(self.world_scope),
        )
        object.__setattr__(
            self,
            "owner_type",
            _normalize_owner_type(self.owner_type),
        )

        owner_id = _coerce_optional_string(self.owner_id)
        if owner_id is None and self.owner_type == OWNER_TYPE_PROJECT:
            owner_id = self.project_id
        elif owner_id is None and self.owner_type == OWNER_TYPE_UNIVERSE:
            owner_id = self.universe_id

        object.__setattr__(
            self,
            "owner_id",
            owner_id,
        )

        object.__setattr__(
            self,
            "generator_type",
            _normalize_id(
                self.generator_type,
                field_name="generatorType",
                fallback=DEFAULT_GENERATOR_TYPE,
            ),
        )
        object.__setattr__(
            self,
            "generator_version",
            _coerce_string(
                self.generator_version,
                fallback=DEFAULT_GENERATOR_VERSION,
            ),
        )
        object.__setattr__(
            self,
            "projection_type",
            _normalize_id(
                self.projection_type,
                field_name="projectionType",
                fallback=DEFAULT_PROJECTION_TYPE,
            ),
        )
        object.__setattr__(
            self,
            "topology_type",
            _normalize_id(
                self.topology_type,
                field_name="topologyType",
                fallback=DEFAULT_TOPOLOGY_TYPE,
            ),
        )
        object.__setattr__(
            self,
            "coordinate_system",
            _normalize_id(
                self.coordinate_system,
                field_name="coordinateSystem",
                fallback=DEFAULT_COORDINATE_SYSTEM,
            ),
        )
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

        object.__setattr__(
            self,
            "seed",
            _coerce_string(self.seed, fallback=DEFAULT_SEED),
        )
        object.__setattr__(
            self,
            "block_registry_id",
            _normalize_id(
                self.block_registry_id,
                field_name="blockRegistryId",
                fallback=DEFAULT_BLOCK_REGISTRY_ID,
            ),
        )
        object.__setattr__(
            self,
            "block_registry_version",
            _coerce_string(
                self.block_registry_version,
                fallback=DEFAULT_BLOCK_REGISTRY_VERSION,
            ),
        )
        object.__setattr__(
            self,
            "status",
            _normalize_status(self.status),
        )
        object.__setattr__(
            self,
            "spawn",
            deep_copy_json(self.spawn or {}),
        )
        object.__setattr__(
            self,
            "runtime",
            deep_copy_json(self.runtime or {}),
        )
        object.__setattr__(
            self,
            "editor",
            deep_copy_json(self.editor or {}),
        )
        object.__setattr__(
            self,
            "metadata",
            deep_copy_json(self.metadata or {}),
        )

        if self.world_id == self.provider_world_id:
            # This is allowed for debug routes, but world_state should make the
            # distinction visible. We do not reject it because future admin
            # worlds or imported worlds may intentionally use same IDs.
            pass

    @property
    def worldId(self) -> str:
        return self.world_id

    @property
    def universeId(self) -> str:
        return self.universe_id

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def templateId(self) -> str:
        return self.template_id

    @property
    def providerWorldId(self) -> str:
        return self.provider_world_id

    @property
    def providerId(self) -> str:
        return self.provider_id

    @property
    def generatorType(self) -> str:
        return self.generator_type

    @property
    def generatorVersion(self) -> str:
        return self.generator_version

    @property
    def projectionType(self) -> str:
        return self.projection_type

    @property
    def topologyType(self) -> str:
        return self.topology_type

    @property
    def coordinateSystem(self) -> str:
        return self.coordinate_system

    @property
    def chunkSize(self) -> int:
        return self.chunk_size

    @property
    def cellSize(self) -> float:
        return self.cell_size

    @property
    def surfaceY(self) -> int:
        return self.surface_y

    @property
    def minY(self) -> int:
        return self.min_y

    @property
    def maxY(self) -> int:
        return self.max_y

    @property
    def blockRegistryId(self) -> str:
        return self.block_registry_id

    @property
    def blockRegistryVersion(self) -> str:
        return self.block_registry_version

    @property
    def is_project_scoped(self) -> bool:
        return self.world_scope == WORLD_SCOPE_PROJECT

    @property
    def is_default_spawn(self) -> bool:
        return self.world_role == WORLD_ROLE_DEFAULT_SPAWN

    def build_context_key(self) -> str:
        return build_world_context_key(
            project_id=self.project_id,
            universe_id=self.universe_id,
            world_id=self.world_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "projectId": self.project_id,
            "universeId": self.universe_id,
            "worldId": self.world_id,
            "slug": self.slug,
            "name": self.name,
            "worldType": self.world_type,
            "worldRole": self.world_role,
            "worldScope": self.world_scope,
            "ownerType": self.owner_type,
            "ownerId": self.owner_id,
            "templateId": self.template_id,
            "providerId": self.provider_id,
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
            "status": self.status,
            "spawn": deep_copy_json(self.spawn),
            "runtime": deep_copy_json(self.runtime),
            "editor": deep_copy_json(self.editor),
            "metadata": deep_copy_json(self.metadata),
            "worldContextKey": self.build_context_key(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorldInstanceDefinition":
        surface_y = _get_first(
            data,
            "surfaceY",
            "surface_y",
            fallback=DEFAULT_SURFACE_Y,
        )
        min_y = _get_first(
            data,
            "minY",
            "min_y",
            fallback=DEFAULT_MIN_Y,
        )
        max_y = _get_first(
            data,
            "maxY",
            "max_y",
            fallback=DEFAULT_MAX_Y,
        )

        return cls(
            world_id=_get_first(data, "worldId", "world_id"),
            universe_id=_get_first(data, "universeId", "universe_id"),
            project_id=_get_first(data, "projectId", "project_id"),
            slug=_get_first(data, "slug", fallback=""),
            name=_get_first(data, "name", "label", fallback=""),
            template_id=_get_first(
                data,
                "templateId",
                "template_id",
                fallback=DEFAULT_TEMPLATE_ID,
            ),
            provider_world_id=_get_first(
                data,
                "providerWorldId",
                "provider_world_id",
                "providerWorld",
                fallback=DEFAULT_PROVIDER_WORLD_ID,
            ),
            provider_id=_get_first(
                data,
                "providerId",
                "provider_id",
                fallback=DEFAULT_PROVIDER_ID,
            ),
            world_type=_get_first(
                data,
                "worldType",
                "world_type",
                fallback=DEFAULT_WORLD_TYPE,
            ),
            world_role=_get_first(
                data,
                "worldRole",
                "world_role",
                fallback=WORLD_ROLE_DEFAULT_SPAWN,
            ),
            world_scope=_get_first(
                data,
                "worldScope",
                "world_scope",
                fallback=WORLD_SCOPE_PROJECT,
            ),
            owner_type=_get_first(
                data,
                "ownerType",
                "owner_type",
                fallback=OWNER_TYPE_PROJECT,
            ),
            owner_id=_get_first(
                data,
                "ownerId",
                "owner_id",
                fallback=None,
            ),
            generator_type=_get_first(
                data,
                "generatorType",
                "generator_type",
                fallback=DEFAULT_GENERATOR_TYPE,
            ),
            generator_version=_get_first(
                data,
                "generatorVersion",
                "generator_version",
                fallback=DEFAULT_GENERATOR_VERSION,
            ),
            projection_type=_get_first(
                data,
                "projectionType",
                "projection_type",
                fallback=DEFAULT_PROJECTION_TYPE,
            ),
            topology_type=_get_first(
                data,
                "topologyType",
                "topology_type",
                fallback=DEFAULT_TOPOLOGY_TYPE,
            ),
            coordinate_system=_get_first(
                data,
                "coordinateSystem",
                "coordinate_system",
                fallback=DEFAULT_COORDINATE_SYSTEM,
            ),
            chunk_size=_get_first(
                data,
                "chunkSize",
                "chunk_size",
                fallback=DEFAULT_CHUNK_SIZE,
            ),
            cell_size=_get_first(
                data,
                "cellSize",
                "cell_size",
                fallback=DEFAULT_CELL_SIZE,
            ),
            surface_y=surface_y,
            min_y=min_y,
            max_y=max_y,
            seed=_get_first(data, "seed", fallback=DEFAULT_SEED),
            block_registry_id=_get_first(
                data,
                "blockRegistryId",
                "block_registry_id",
                fallback=DEFAULT_BLOCK_REGISTRY_ID,
            ),
            block_registry_version=_get_first(
                data,
                "blockRegistryVersion",
                "block_registry_version",
                fallback=DEFAULT_BLOCK_REGISTRY_VERSION,
            ),
            status=_get_first(data, "status", fallback=DEFAULT_STATUS_ACTIVE),
            spawn=_get_first(data, "spawn", fallback={}),
            runtime=_get_first(data, "runtime", fallback={}),
            editor=_get_first(data, "editor", fallback={}),
            metadata=_get_first(data, "metadata", fallback={}),
            schema_version=_get_first(
                data,
                "schemaVersion",
                "schema_version",
                fallback=WORLD_INSTANCE_SCHEMA_VERSION,
            ),
        )

    def copy_with(self, **changes: Any) -> "WorldInstanceDefinition":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class WorldRuntimeContext:
    """
    Fully resolved context for one concrete world instance.

    This combines project, universe and world instance data so service and route
    code can work with one object.
    """

    project: ProjectRuntimeContext
    universe: UniverseRuntimeContext
    world: WorldInstanceDefinition
    schema_version: str = WORLD_RUNTIME_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.project.project_id != self.universe.project_id:
            raise ModelValidationError(
                "Universe does not belong to project.",
                code="invalid_project_universe_binding",
                details={
                    "projectId": self.project.project_id,
                    "universeProjectId": self.universe.project_id,
                    "universeId": self.universe.universe_id,
                },
            )

        if self.project.project_id != self.world.project_id:
            raise ModelValidationError(
                "World does not belong to project.",
                code="invalid_project_world_binding",
                details={
                    "projectId": self.project.project_id,
                    "worldProjectId": self.world.project_id,
                    "worldId": self.world.world_id,
                },
            )

        if self.universe.universe_id != self.world.universe_id:
            raise ModelValidationError(
                "World does not belong to universe.",
                code="invalid_universe_world_binding",
                details={
                    "universeId": self.universe.universe_id,
                    "worldUniverseId": self.world.universe_id,
                    "worldId": self.world.world_id,
                },
            )

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
    def providerWorldId(self) -> str:
        return self.provider_world_id

    def build_context_key(self) -> str:
        return build_world_context_key(
            project_id=self.project_id,
            universe_id=self.universe_id,
            world_id=self.world_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "project": self.project.to_dict(),
            "universe": self.universe.to_dict(),
            "world": self.world.to_dict(),
            "context": {
                "projectId": self.project_id,
                "universeId": self.universe_id,
                "worldId": self.world_id,
                "templateId": self.template_id,
                "providerWorldId": self.provider_world_id,
                "worldContextKey": self.build_context_key(),
            },
        }

    def copy_with(
        self,
        *,
        project: ProjectRuntimeContext | None = None,
        universe: UniverseRuntimeContext | None = None,
        world: WorldInstanceDefinition | None = None,
    ) -> "WorldRuntimeContext":
        return WorldRuntimeContext(
            project=project or self.project,
            universe=universe or self.universe,
            world=world or self.world,
            schema_version=self.schema_version,
        )


@dataclass(frozen=True, slots=True)
class ProjectBootstrapContext:
    """
    Bootstrap context for opening a project in the editor.

    The editor should call the bootstrap route first. The response tells it
    which concrete world to load and which routes to use.
    """

    project: ProjectRuntimeContext
    universe: UniverseRuntimeContext
    default_world: WorldInstanceDefinition
    spawn_world: WorldInstanceDefinition
    worlds: tuple[WorldInstanceDefinition, ...] = field(default_factory=tuple)
    route_hints: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PROJECT_BOOTSTRAP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.project.project_id != self.universe.project_id:
            raise ModelValidationError(
                "Bootstrap universe does not belong to project.",
                code="invalid_bootstrap_project_universe_binding",
                details={
                    "projectId": self.project.project_id,
                    "universeProjectId": self.universe.project_id,
                    "universeId": self.universe.universe_id,
                },
            )

        if self.universe.default_world_id != self.default_world.world_id:
            raise ModelValidationError(
                "Bootstrap default world mismatch.",
                code="invalid_bootstrap_default_world",
                details={
                    "universeDefaultWorldId": self.universe.default_world_id,
                    "defaultWorldId": self.default_world.world_id,
                },
            )

        if self.universe.spawn_world_id != self.spawn_world.world_id:
            raise ModelValidationError(
                "Bootstrap spawn world mismatch.",
                code="invalid_bootstrap_spawn_world",
                details={
                    "universeSpawnWorldId": self.universe.spawn_world_id,
                    "spawnWorldId": self.spawn_world.world_id,
                },
            )

        normalized_worlds = tuple(self.worlds or ())
        if not normalized_worlds:
            normalized_worlds = tuple(
                dedupe_world_instances(
                    [
                        self.default_world,
                        self.spawn_world,
                    ]
                )
            )

        for world in normalized_worlds:
            if world.project_id != self.project.project_id:
                raise ModelValidationError(
                    "Bootstrap world does not belong to project.",
                    code="invalid_bootstrap_world_project_binding",
                    details={
                        "projectId": self.project.project_id,
                        "worldProjectId": world.project_id,
                        "worldId": world.world_id,
                    },
                )
            if world.universe_id != self.universe.universe_id:
                raise ModelValidationError(
                    "Bootstrap world does not belong to universe.",
                    code="invalid_bootstrap_world_universe_binding",
                    details={
                        "universeId": self.universe.universe_id,
                        "worldUniverseId": world.universe_id,
                        "worldId": world.world_id,
                    },
                )

        object.__setattr__(self, "worlds", normalized_worlds)
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
            "schemaVersion": self.schema_version,
            "project": self.project.to_dict(),
            "universe": self.universe.to_dict(),
            "defaultWorld": self.default_world.to_dict(),
            "spawnWorld": self.spawn_world.to_dict(),
            "worlds": [world.to_dict() for world in self.worlds],
            "routeHints": deep_copy_json(self.route_hints),
            "metadata": deep_copy_json(self.metadata),
        }

    def copy_with(self, **changes: Any) -> "ProjectBootstrapContext":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class WorldStateCatalog:
    """
    In-memory catalog of projects, universes and concrete world instances.

    In phase 1 this catalog is produced by `defaults.py`.

    Later this object can be constructed from PostgreSQL repositories while the
    resolver/service APIs remain stable.
    """

    projects: tuple[ProjectRuntimeContext, ...] = field(default_factory=tuple)
    universes: tuple[UniverseRuntimeContext, ...] = field(default_factory=tuple)
    worlds: tuple[WorldInstanceDefinition, ...] = field(default_factory=tuple)
    default_project_id: str = DEFAULT_PROJECT_ID
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = WORLD_STATE_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        projects = tuple(self.projects or ())
        universes = tuple(self.universes or ())
        worlds = tuple(self.worlds or ())

        project_ids: set[str] = set()
        for project in projects:
            if project.project_id in project_ids:
                raise ModelValidationError(
                    "Duplicate project id in world-state catalog.",
                    code="duplicate_project_id",
                    details={"projectId": project.project_id},
                )
            project_ids.add(project.project_id)

        universe_ids: set[str] = set()
        for universe in universes:
            if universe.universe_id in universe_ids:
                raise ModelValidationError(
                    "Duplicate universe id in world-state catalog.",
                    code="duplicate_universe_id",
                    details={"universeId": universe.universe_id},
                )
            universe_ids.add(universe.universe_id)

            if universe.project_id not in project_ids:
                raise ModelValidationError(
                    "Universe references unknown project.",
                    code="universe_references_unknown_project",
                    details={
                        "universeId": universe.universe_id,
                        "projectId": universe.project_id,
                    },
                )

        world_keys: set[tuple[str, str, str]] = set()
        for world in worlds:
            key = (world.project_id, world.universe_id, world.world_id)
            if key in world_keys:
                raise ModelValidationError(
                    "Duplicate world instance in world-state catalog.",
                    code="duplicate_world_instance",
                    details={
                        "projectId": world.project_id,
                        "universeId": world.universe_id,
                        "worldId": world.world_id,
                    },
                )
            world_keys.add(key)

            if world.project_id not in project_ids:
                raise ModelValidationError(
                    "World references unknown project.",
                    code="world_references_unknown_project",
                    details={
                        "worldId": world.world_id,
                        "projectId": world.project_id,
                    },
                )

            if world.universe_id not in universe_ids:
                raise ModelValidationError(
                    "World references unknown universe.",
                    code="world_references_unknown_universe",
                    details={
                        "worldId": world.world_id,
                        "universeId": world.universe_id,
                    },
                )

        for project in projects:
            if project.default_universe_id not in universe_ids:
                raise ModelValidationError(
                    "Project references unknown default universe.",
                    code="project_references_unknown_default_universe",
                    details={
                        "projectId": project.project_id,
                        "defaultUniverseId": project.default_universe_id,
                    },
                )

        for universe in universes:
            default_key_exists = any(
                world.project_id == universe.project_id
                and world.universe_id == universe.universe_id
                and world.world_id == universe.default_world_id
                for world in worlds
            )
            spawn_key_exists = any(
                world.project_id == universe.project_id
                and world.universe_id == universe.universe_id
                and world.world_id == universe.spawn_world_id
                for world in worlds
            )

            if not default_key_exists:
                raise ModelValidationError(
                    "Universe references unknown default world.",
                    code="universe_references_unknown_default_world",
                    details={
                        "projectId": universe.project_id,
                        "universeId": universe.universe_id,
                        "defaultWorldId": universe.default_world_id,
                    },
                )

            if not spawn_key_exists:
                raise ModelValidationError(
                    "Universe references unknown spawn world.",
                    code="universe_references_unknown_spawn_world",
                    details={
                        "projectId": universe.project_id,
                        "universeId": universe.universe_id,
                        "spawnWorldId": universe.spawn_world_id,
                    },
                )

        object.__setattr__(self, "projects", projects)
        object.__setattr__(self, "universes", universes)
        object.__setattr__(self, "worlds", worlds)
        object.__setattr__(
            self,
            "default_project_id",
            normalize_project_id(
                self.default_project_id,
                fallback=projects[0].project_id if projects else DEFAULT_PROJECT_ID,
            ),
        )
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))

        if projects and self.default_project_id not in project_ids:
            raise ModelValidationError(
                "Default project id is not part of catalog.",
                code="unknown_default_project",
                details={
                    "defaultProjectId": self.default_project_id,
                    "projectIds": sorted(project_ids),
                },
            )

    @property
    def defaultProjectId(self) -> str:
        return self.default_project_id

    def get_project(self, project_id: str) -> ProjectRuntimeContext | None:
        normalized_project_id = normalize_project_id(project_id)

        for project in self.projects:
            if project.project_id == normalized_project_id:
                return project

        return None

    def get_universe(self, universe_id: str) -> UniverseRuntimeContext | None:
        normalized_universe_id = normalize_universe_id(universe_id)

        for universe in self.universes:
            if universe.universe_id == normalized_universe_id:
                return universe

        return None

    def get_project_universes(
        self,
        project_id: str,
    ) -> tuple[UniverseRuntimeContext, ...]:
        normalized_project_id = normalize_project_id(project_id)

        return tuple(
            universe
            for universe in self.universes
            if universe.project_id == normalized_project_id
        )

    def get_project_worlds(
        self,
        project_id: str,
        *,
        universe_id: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[WorldInstanceDefinition, ...]:
        normalized_project_id = normalize_project_id(project_id)

        normalized_universe_id = (
            normalize_universe_id(universe_id)
            if universe_id is not None
            else None
        )

        result: list[WorldInstanceDefinition] = []

        for world in self.worlds:
            if world.project_id != normalized_project_id:
                continue
            if normalized_universe_id is not None and world.universe_id != normalized_universe_id:
                continue
            if not include_inactive and world.status != DEFAULT_STATUS_ACTIVE:
                continue
            result.append(world)

        return tuple(result)

    def get_world(
        self,
        *,
        project_id: str,
        world_id: str,
        universe_id: str | None = None,
    ) -> WorldInstanceDefinition | None:
        normalized_project_id = normalize_project_id(project_id)
        normalized_world_id = normalize_world_instance_id(world_id)

        normalized_universe_id = (
            normalize_universe_id(universe_id)
            if universe_id is not None
            else None
        )

        for world in self.worlds:
            if world.project_id != normalized_project_id:
                continue
            if world.world_id != normalized_world_id:
                continue
            if normalized_universe_id is not None and world.universe_id != normalized_universe_id:
                continue
            return world

        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "defaultProjectId": self.default_project_id,
            "projects": [project.to_dict() for project in self.projects],
            "universes": [universe.to_dict() for universe in self.universes],
            "worlds": [world.to_dict() for world in self.worlds],
            "counts": {
                "projects": len(self.projects),
                "universes": len(self.universes),
                "worlds": len(self.worlds),
            },
            "metadata": deep_copy_json(self.metadata),
            "catalogHash": self.catalog_hash(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorldStateCatalog":
        return cls(
            projects=tuple(
                ProjectRuntimeContext.from_dict(item)
                for item in _get_first(data, "projects", fallback=[])
            ),
            universes=tuple(
                UniverseRuntimeContext.from_dict(item)
                for item in _get_first(data, "universes", fallback=[])
            ),
            worlds=tuple(
                WorldInstanceDefinition.from_dict(item)
                for item in _get_first(data, "worlds", fallback=[])
            ),
            default_project_id=_get_first(
                data,
                "defaultProjectId",
                "default_project_id",
                fallback=DEFAULT_PROJECT_ID,
            ),
            metadata=_get_first(data, "metadata", fallback={}),
            schema_version=_get_first(
                data,
                "schemaVersion",
                "schema_version",
                fallback=WORLD_STATE_CATALOG_SCHEMA_VERSION,
            ),
        )

    def catalog_hash(self) -> str:
        return stable_hash(
            {
                "projects": [project.to_dict() for project in self.projects],
                "universes": [universe.to_dict() for universe in self.universes],
                "worlds": [world.to_dict() for world in self.worlds],
                "defaultProjectId": self.default_project_id,
            }
        )

    def copy_with(self, **changes: Any) -> "WorldStateCatalog":
        return replace(self, **changes)


def build_world_context_key(
    *,
    project_id: str,
    universe_id: str,
    world_id: str,
) -> str:
    """
    Build a stable context key for project-scoped world state.
    """
    return (
        f"{normalize_project_id(project_id)}:"
        f"{normalize_universe_id(universe_id)}:"
        f"{normalize_world_instance_id(world_id)}"
    )


def build_chunk_context_key(
    *,
    project_id: str,
    universe_id: str,
    world_id: str,
    chunk_key: str,
) -> str:
    """
    Build a stable context key for a chunk inside a concrete world instance.
    """
    normalized_chunk_key = _coerce_string(chunk_key)

    if not normalized_chunk_key:
        raise ModelValidationError(
            "chunkKey is required.",
            code="missing_chunk_key",
            details={"field": "chunkKey"},
        )

    return (
        f"{build_world_context_key(project_id=project_id, universe_id=universe_id, world_id=world_id)}:"
        f"{normalized_chunk_key}"
    )


def dedupe_world_instances(
    worlds: Iterable[WorldInstanceDefinition],
) -> tuple[WorldInstanceDefinition, ...]:
    """
    Deduplicate world instances by project/universe/world id while preserving
    order.
    """
    seen: set[tuple[str, str, str]] = set()
    result: list[WorldInstanceDefinition] = []

    for world in worlds:
        key = (world.project_id, world.universe_id, world.world_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(world)

    return tuple(result)


def create_world_runtime_context(
    *,
    project: ProjectRuntimeContext,
    universe: UniverseRuntimeContext,
    world: WorldInstanceDefinition,
) -> WorldRuntimeContext:
    return WorldRuntimeContext(
        project=project,
        universe=universe,
        world=world,
    )


def create_project_bootstrap_context(
    *,
    project: ProjectRuntimeContext,
    universe: UniverseRuntimeContext,
    default_world: WorldInstanceDefinition,
    spawn_world: WorldInstanceDefinition,
    worlds: Sequence[WorldInstanceDefinition] | None = None,
    route_hints: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProjectBootstrapContext:
    return ProjectBootstrapContext(
        project=project,
        universe=universe,
        default_world=default_world,
        spawn_world=spawn_world,
        worlds=tuple(worlds or ()),
        route_hints=route_hints or {},
        metadata=metadata or {},
    )


def create_route_hints(
    *,
    project_id: str,
    world_id: str,
    api_prefix: str = "",
) -> dict[str, Any]:
    """
    Create route hints for the editor.

    The returned values are intentionally relative by default. A gateway or
    frontend can prefix them with a host/base URL.
    """
    normalized_project_id = normalize_project_id(project_id)
    normalized_world_id = normalize_world_instance_id(world_id)

    prefix = _coerce_string(api_prefix).rstrip("/")

    project_base = f"{prefix}/projects/{normalized_project_id}"
    world_base = f"{project_base}/worlds/{normalized_world_id}"

    return {
        "projectBootstrap": f"{project_base}/bootstrap",
        "worlds": f"{project_base}/worlds",
        "world": world_base,
        "blocks": f"{world_base}/blocks",
        "chunk": f"{world_base}/chunks",
        "chunksBatch": f"{world_base}/chunks/batch",
        "commands": f"{world_base}/commands",
    }


def merge_metadata(
    *parts: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """
    Merge metadata dictionaries defensively.

    Later parts override earlier parts.
    """
    result: dict[str, Any] = {}

    for part in parts:
        if not part:
            continue

        safe_part = make_json_safe(part)

        if isinstance(safe_part, Mapping):
            result.update(dict(safe_part))

    return result


def assert_same_project(
    *,
    project: ProjectRuntimeContext,
    universe: UniverseRuntimeContext | None = None,
    world: WorldInstanceDefinition | None = None,
) -> None:
    """
    Validate that optional universe/world objects belong to the given project.
    """
    if universe is not None and universe.project_id != project.project_id:
        raise ModelValidationError(
            "Universe does not belong to project.",
            code="invalid_project_universe_binding",
            details={
                "projectId": project.project_id,
                "universeId": universe.universe_id,
                "universeProjectId": universe.project_id,
            },
        )

    if world is not None and world.project_id != project.project_id:
        raise ModelValidationError(
            "World does not belong to project.",
            code="invalid_project_world_binding",
            details={
                "projectId": project.project_id,
                "worldId": world.world_id,
                "worldProjectId": world.project_id,
            },
        )


def assert_same_universe(
    *,
    universe: UniverseRuntimeContext,
    world: WorldInstanceDefinition,
) -> None:
    """
    Validate that a world belongs to a universe.
    """
    if world.universe_id != universe.universe_id:
        raise ModelValidationError(
            "World does not belong to universe.",
            code="invalid_universe_world_binding",
            details={
                "universeId": universe.universe_id,
                "worldId": world.world_id,
                "worldUniverseId": world.universe_id,
            },
        )


def model_to_dict(value: Any) -> dict[str, Any]:
    """
    Convert a model object to dict.

    Raises a validation error for unsupported values instead of failing with
    unclear AttributeError messages in route code.
    """
    if hasattr(value, "to_dict") and callable(value.to_dict):
        result = value.to_dict()
        if isinstance(result, Mapping):
            return dict(result)

    if dataclasses.is_dataclass(value):
        return make_json_safe(dataclasses.asdict(value))

    if isinstance(value, Mapping):
        return make_json_safe(value)

    raise ModelValidationError(
        "Value cannot be converted to model dictionary.",
        code="not_a_model",
        details={
            "type": type(value).__name__,
        },
    )


__all__ = (
    "WORLD_STATE_SCHEMA_VERSION",
    "PROJECT_CONTEXT_SCHEMA_VERSION",
    "UNIVERSE_CONTEXT_SCHEMA_VERSION",
    "WORLD_INSTANCE_SCHEMA_VERSION",
    "WORLD_RUNTIME_CONTEXT_SCHEMA_VERSION",
    "PROJECT_BOOTSTRAP_SCHEMA_VERSION",
    "WORLD_STATE_CATALOG_SCHEMA_VERSION",
    "DEFAULT_STATUS_ACTIVE",
    "DEFAULT_STATUS_DRAFT",
    "DEFAULT_STATUS_ARCHIVED",
    "KNOWN_STATUSES",
    "WORLD_SCOPE_PROJECT",
    "WORLD_SCOPE_ADMIN",
    "WORLD_SCOPE_SYSTEM",
    "WORLD_SCOPE_SHARED",
    "KNOWN_WORLD_SCOPES",
    "WORLD_ROLE_DEFAULT_SPAWN",
    "WORLD_ROLE_SPAWN",
    "WORLD_ROLE_MAIN",
    "WORLD_ROLE_REAL_SITE",
    "WORLD_ROLE_SANDBOX",
    "WORLD_ROLE_INTERIOR",
    "WORLD_ROLE_ADMIN",
    "WORLD_ROLE_IMPORTED",
    "WORLD_ROLE_SIMULATION",
    "KNOWN_WORLD_ROLES",
    "OWNER_TYPE_PROJECT",
    "OWNER_TYPE_UNIVERSE",
    "OWNER_TYPE_ADMIN",
    "OWNER_TYPE_SYSTEM",
    "KNOWN_OWNER_TYPES",
    "DEFAULT_WORLD_TYPE",
    "DEFAULT_TEMPLATE_ID",
    "DEFAULT_PROVIDER_WORLD_ID",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_GENERATOR_TYPE",
    "DEFAULT_GENERATOR_VERSION",
    "DEFAULT_PROJECTION_TYPE",
    "DEFAULT_TOPOLOGY_TYPE",
    "DEFAULT_COORDINATE_SYSTEM",
    "DEFAULT_BLOCK_REGISTRY_ID",
    "DEFAULT_BLOCK_REGISTRY_VERSION",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_CELL_SIZE",
    "DEFAULT_SURFACE_Y",
    "DEFAULT_MIN_Y",
    "DEFAULT_MAX_Y",
    "DEFAULT_SEED",
    "DEFAULT_INSTANCE_WORLD_ID",
    "DEFAULT_PROJECT_ID",
    "DEFAULT_UNIVERSE_ID",
    "ModelValidationError",
    "ProjectRuntimeContext",
    "UniverseRuntimeContext",
    "WorldInstanceDefinition",
    "WorldRuntimeContext",
    "ProjectBootstrapContext",
    "WorldStateCatalog",
    "make_json_safe",
    "utc_now_iso",
    "normalize_project_id",
    "normalize_universe_id",
    "normalize_world_instance_id",
    "normalize_template_id",
    "normalize_provider_world_id",
    "normalize_chunk_size",
    "normalize_cell_size",
    "normalize_vertical_bounds",
    "stable_hash",
    "deep_copy_json",
    "build_world_context_key",
    "build_chunk_context_key",
    "dedupe_world_instances",
    "create_world_runtime_context",
    "create_project_bootstrap_context",
    "create_route_hints",
    "merge_metadata",
    "assert_same_project",
    "assert_same_universe",
    "model_to_dict",
)