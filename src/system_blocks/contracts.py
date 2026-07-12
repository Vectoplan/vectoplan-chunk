# services/vectoplan-chunk/src/system_blocks/contracts.py
"""
Contracts and shared validation rules for built-in VECTOPLAN system blocks.

This module defines the framework-independent contract used by all blocks that
are permanently integrated into ``vectoplan-chunk``.

System blocks are intentionally different from:

- Library/VPLIB blocks:
  These are externally managed definitions whose semantic ownership belongs to
  ``vectoplan-library-service``.

- Debug blocks:
  These are temporary development blocks such as ``debug_grass`` and
  ``debug_dirt``.

- Ordinary chunk palette entries:
  These are concrete runtime representations inside one materialized or
  generated chunk.

The system-block contract has two primary use cases:

1. Reserved cell states

   Example: Air

   Air is represented by ``cellValue = 0`` and must never be stored as a
   positive palette entry or as a normal ``BlockType`` row.

2. Persisted built-in block definitions

   Example: Railing

   A railing is defined canonically in Python code but mirrored into the
   current world's persistent ``BlockRegistry`` as a normal ``BlockType``.
   This allows the existing SetBlock, RemoveBlock, palette, snapshot, event and
   editor-runtime paths to use it without adding a second block system.

Important boundaries:

- no Flask imports
- no SQLAlchemy imports
- no database access
- no commits
- no route logic
- no filesystem access
- no bootstrap side effects during import

This module provides:

- immutable system-block definitions
- defensive normalization
- cross-field invariant validation
- JSON-safe serialization
- persistent BlockType value generation
- palette-entry generation
- stable definition fingerprints
- drift comparison against an existing BlockType-like object

The contract is deliberately broader than the first two system blocks so later
built-in blocks can be added without redesigning this file.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Final, Optional, TypeAlias


# -----------------------------------------------------------------------------
# Contract and encoding versions
# -----------------------------------------------------------------------------

SYSTEM_BLOCK_DEFINITION_SCHEMA_VERSION: Final[str] = (
    "system-block-definition.schema.v1"
)

SYSTEM_BLOCK_METADATA_SCHEMA_VERSION: Final[str] = (
    "system-block-metadata.schema.v1"
)

SYSTEM_BLOCK_SOURCE: Final[str] = "system"
SYSTEM_BLOCK_CATEGORY: Final[str] = "system"
SYSTEM_BLOCK_STATUS_ACTIVE: Final[str] = "active"

CELL_ENCODING_VERSION: Final[str] = (
    "cell-encoding.palette-index-plus-one.v1"
)

AIR_CELL_VALUE: Final[int] = 0
BLOCK_CELL_VALUE_RULE: Final[str] = "paletteIndex + 1"


# -----------------------------------------------------------------------------
# Supported BlockType-compatible values
# -----------------------------------------------------------------------------

RENDER_MODE_CUBE: Final[str] = "cube"
RENDER_MODE_INVISIBLE: Final[str] = "invisible"
RENDER_MODE_CUSTOM: Final[str] = "custom"
RENDER_MODE_MESH: Final[str] = "mesh"

VALID_RENDER_MODES: Final[frozenset[str]] = frozenset(
    {
        RENDER_MODE_CUBE,
        RENDER_MODE_INVISIBLE,
        RENDER_MODE_CUSTOM,
        RENDER_MODE_MESH,
    }
)

SHAPE_TYPE_CUBE: Final[str] = "cube"
SHAPE_TYPE_EMPTY: Final[str] = "empty"
SHAPE_TYPE_CUSTOM: Final[str] = "custom"

VALID_SHAPE_TYPES: Final[frozenset[str]] = frozenset(
    {
        SHAPE_TYPE_CUBE,
        SHAPE_TYPE_EMPTY,
        SHAPE_TYPE_CUSTOM,
    }
)


# -----------------------------------------------------------------------------
# Length limits
#
# These values intentionally follow the current models/block.py constraints.
# Keeping them aligned prevents a valid code definition from later failing when
# it is mirrored into BlockType.
# -----------------------------------------------------------------------------

SYSTEM_BLOCK_ID_MAX_LENGTH: Final[int] = 160
SYSTEM_BLOCK_KIND_MAX_LENGTH: Final[int] = 96
SYSTEM_BLOCK_LABEL_MAX_LENGTH: Final[int] = 255
SYSTEM_BLOCK_DESCRIPTION_MAX_LENGTH: Final[int] = 4096
SYSTEM_BLOCK_VERSION_MAX_LENGTH: Final[int] = 64

BLOCK_CATEGORY_MAX_LENGTH: Final[int] = 96
BLOCK_MATERIAL_ID_MAX_LENGTH: Final[int] = 160
BLOCK_TEXTURE_ID_MAX_LENGTH: Final[int] = 160
BLOCK_ICON_ID_MAX_LENGTH: Final[int] = 160

MAX_LIGHT_LEVEL: Final[int] = 15
MIN_STACK_SIZE: Final[int] = 1
MAX_STACK_SIZE: Final[int] = 2_147_483_647

DEFAULT_DEFINITION_VERSION: Final[str] = "1"
DEFAULT_HARDNESS: Final[float] = 1.0
DEFAULT_STACK_SIZE: Final[int] = 64


# -----------------------------------------------------------------------------
# Reserved metadata keys
# -----------------------------------------------------------------------------

SYSTEM_BLOCK_METADATA_NAMESPACE: Final[str] = "vectoplanSystemBlock"

_RESERVED_SYSTEM_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schemaVersion",
        "source",
        "systemBlockId",
        "runtimeBlockTypeId",
        "kind",
        "definitionVersion",
        "definitionFingerprint",
        "immutableDefinition",
        "persistAsBlockType",
        "inventoryVisible",
        "reservedCellValue",
        "cellEncodingVersion",
        "airCellValue",
        "blockCellValueRule",
    }
)


# -----------------------------------------------------------------------------
# JSON type aliases
# -----------------------------------------------------------------------------

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Mapping[str, Any] | Sequence[Any]


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class SystemBlockContractError(ValueError):
    """
    Base error for invalid system-block contract data.
    """


class SystemBlockDefinitionValidationError(SystemBlockContractError):
    """
    Raised when a SystemBlockDefinition violates one or more invariants.
    """

    def __init__(
        self,
        *,
        system_block_id: Optional[str],
        errors: Sequence[str],
    ) -> None:
        normalized_errors = tuple(
            str(error).strip()
            for error in errors
            if str(error).strip()
        )

        self.system_block_id = system_block_id
        self.errors = normalized_errors

        label = system_block_id or "<unknown>"
        details = "; ".join(normalized_errors) or "unknown validation error"

        super().__init__(
            f"Invalid system block definition '{label}': {details}"
        )


class SystemBlockPersistenceError(SystemBlockContractError):
    """
    Raised when a non-persistable definition is used as a BlockType.
    """


class SystemBlockPaletteError(SystemBlockContractError):
    """
    Raised when a definition cannot be represented as a positive palette entry.
    """


# -----------------------------------------------------------------------------
# Cached primitive helpers
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _type_id_pattern() -> re.Pattern[str]:
    """
    Return the cached identifier pattern.

    This matches the identifier rules currently used by models/block.py.
    """
    return re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


@lru_cache(maxsize=1)
def _version_pattern() -> re.Pattern[str]:
    """
    Return the cached version pattern.
    """
    return re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")


@lru_cache(maxsize=4096)
def _normalize_identifier_cached(
    value: str,
    field_name: str,
    max_length: int,
) -> str:
    """
    Normalize and validate an identifier through a bounded cache.
    """
    text = value.strip()

    if not text:
        raise SystemBlockContractError(f"{field_name} is required.")

    if len(text) > max_length:
        raise SystemBlockContractError(
            f"{field_name} must not exceed {max_length} characters."
        )

    if not _type_id_pattern().match(text):
        raise SystemBlockContractError(
            f"{field_name} may only contain letters, numbers, underscores, "
            "dashes, dots and colons, and must start with an alphanumeric "
            "character."
        )

    return text


@lru_cache(maxsize=1024)
def _normalize_version_cached(
    value: str,
    field_name: str,
    max_length: int,
) -> str:
    """
    Normalize and validate a version string through a bounded cache.
    """
    text = value.strip()

    if not text:
        raise SystemBlockContractError(f"{field_name} is required.")

    if len(text) > max_length:
        raise SystemBlockContractError(
            f"{field_name} must not exceed {max_length} characters."
        )

    if not _version_pattern().match(text):
        raise SystemBlockContractError(
            f"{field_name} contains unsupported characters."
        )

    return text


@lru_cache(maxsize=4096)
def _sha256_text_cached(value: str) -> str:
    """
    Return a stable SHA-256 hash for text.

    Definition fingerprints call this repeatedly during status checks, route
    serialization and bootstrap drift detection, so a bounded cache avoids
    recalculating identical definitions.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def get_system_block_contract_descriptor() -> Mapping[str, Any]:
    """
    Return immutable diagnostic metadata for this contract version.
    """
    return MappingProxyType(
        {
            "schemaVersion": SYSTEM_BLOCK_DEFINITION_SCHEMA_VERSION,
            "metadataSchemaVersion": SYSTEM_BLOCK_METADATA_SCHEMA_VERSION,
            "source": SYSTEM_BLOCK_SOURCE,
            "category": SYSTEM_BLOCK_CATEGORY,
            "cellEncodingVersion": CELL_ENCODING_VERSION,
            "airCellValue": AIR_CELL_VALUE,
            "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
            "renderModes": tuple(sorted(VALID_RENDER_MODES)),
            "shapeTypes": tuple(sorted(VALID_SHAPE_TYPES)),
        }
    )


# -----------------------------------------------------------------------------
# Primitive normalization
# -----------------------------------------------------------------------------

def _normalize_required_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """
    Normalize required human-readable text.
    """
    if value is None:
        raise SystemBlockContractError(f"{field_name} is required.")

    try:
        text = str(value).strip()
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be text-like."
        ) from exc

    if not text:
        raise SystemBlockContractError(f"{field_name} is required.")

    if len(text) > max_length:
        raise SystemBlockContractError(
            f"{field_name} must not exceed {max_length} characters."
        )

    return text


def _normalize_optional_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    """
    Normalize optional human-readable text.
    """
    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be text-like."
        ) from exc

    if not text:
        return None

    if len(text) > max_length:
        raise SystemBlockContractError(
            f"{field_name} must not exceed {max_length} characters."
        )

    return text


def _normalize_identifier(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """
    Normalize a required technical identifier.
    """
    if value is None:
        raise SystemBlockContractError(f"{field_name} is required.")

    try:
        text = str(value)
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be text-like."
        ) from exc

    return _normalize_identifier_cached(
        text,
        field_name,
        int(max_length),
    )


def _normalize_optional_identifier(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    """
    Normalize an optional technical identifier.
    """
    if value is None:
        return None

    try:
        text = str(value).strip()
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be text-like."
        ) from exc

    if not text:
        return None

    return _normalize_identifier_cached(
        text,
        field_name,
        int(max_length),
    )


def _normalize_version(
    value: Any,
    *,
    field_name: str = "definition_version",
) -> str:
    """
    Normalize a definition version.
    """
    if value is None:
        value = DEFAULT_DEFINITION_VERSION

    try:
        text = str(value)
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be text-like."
        ) from exc

    return _normalize_version_cached(
        text,
        field_name,
        SYSTEM_BLOCK_VERSION_MAX_LENGTH,
    )


def _normalize_bool(
    value: Any,
    *,
    field_name: str,
    default: bool,
) -> bool:
    """
    Normalize bool-like values defensively.
    """
    if value is None:
        return bool(default)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    try:
        text = str(value).strip().lower()
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be boolean-like."
        ) from exc

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False

    raise SystemBlockContractError(
        f"{field_name} must be a boolean."
    )


def _normalize_optional_int(
    value: Any,
    *,
    field_name: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Optional[int]:
    """
    Normalize an optional integer with bounds.
    """
    if value is None:
        return None

    try:
        result = int(value)
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be an integer."
        ) from exc

    if minimum is not None and result < minimum:
        raise SystemBlockContractError(
            f"{field_name} must be greater than or equal to {minimum}."
        )

    if maximum is not None and result > maximum:
        raise SystemBlockContractError(
            f"{field_name} must be less than or equal to {maximum}."
        )

    return result


def _normalize_int(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """
    Normalize a required integer with a default and bounds.
    """
    if value is None:
        value = default

    result = _normalize_optional_int(
        value,
        field_name=field_name,
        minimum=minimum,
        maximum=maximum,
    )

    if result is None:
        raise SystemBlockContractError(f"{field_name} is required.")

    return result


def _normalize_float(
    value: Any,
    *,
    field_name: str,
    default: float,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """
    Normalize a finite float with optional bounds.
    """
    if value is None:
        value = default

    try:
        result = float(value)
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be a number."
        ) from exc

    if not math.isfinite(result):
        raise SystemBlockContractError(
            f"{field_name} must be finite."
        )

    if minimum is not None and result < minimum:
        raise SystemBlockContractError(
            f"{field_name} must be greater than or equal to {minimum}."
        )

    if maximum is not None and result > maximum:
        raise SystemBlockContractError(
            f"{field_name} must be less than or equal to {maximum}."
        )

    return result


def _normalize_choice(
    value: Any,
    *,
    field_name: str,
    allowed: frozenset[str],
    default: str,
) -> str:
    """
    Normalize a lower-case enum-like string.
    """
    if value is None:
        value = default

    try:
        text = str(value).strip().lower()
    except Exception as exc:
        raise SystemBlockContractError(
            f"{field_name} must be text-like."
        ) from exc

    if text not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise SystemBlockContractError(
            f"Invalid {field_name} '{value}'. Allowed: {allowed_text}."
        )

    return text


def _normalize_aliases(
    value: Any,
    *,
    primary_ids: Sequence[Optional[str]],
) -> tuple[str, ...]:
    """
    Normalize optional alternate IDs.

    Aliases are deterministic, unique and sorted. Primary IDs are excluded.
    """
    if value is None:
        return tuple()

    if isinstance(value, (str, bytes, bytearray)):
        raw_aliases: Sequence[Any] = (value,)
    elif isinstance(value, Sequence) or isinstance(value, Set):
        raw_aliases = tuple(value)
    else:
        raise SystemBlockContractError(
            "aliases must be a string or a sequence of strings."
        )

    excluded = {
        str(item).strip()
        for item in primary_ids
        if item is not None and str(item).strip()
    }

    aliases: set[str] = set()

    for item in raw_aliases:
        alias = _normalize_identifier(
            item,
            field_name="alias",
            max_length=SYSTEM_BLOCK_ID_MAX_LENGTH,
        )

        if alias in excluded:
            continue

        aliases.add(alias)

    return tuple(sorted(aliases))


# -----------------------------------------------------------------------------
# Deep JSON freezing and serialization
# -----------------------------------------------------------------------------

def _freeze_json_value(
    value: Any,
    *,
    path: str,
    seen: Optional[set[int]] = None,
) -> Any:
    """
    Convert JSON-compatible input into immutable structures.

    Mappings become MappingProxyType objects and sequences become tuples.
    Recursive references are rejected instead of being silently truncated.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise SystemBlockContractError(
                f"{path} contains a non-finite number."
            )
        return value

    if seen is None:
        seen = set()

    if isinstance(value, Mapping):
        value_id = id(value)

        if value_id in seen:
            raise SystemBlockContractError(
                f"{path} contains a recursive mapping reference."
            )

        seen.add(value_id)

        try:
            frozen: dict[str, Any] = {}

            for raw_key, raw_item in value.items():
                try:
                    key = str(raw_key).strip()
                except Exception as exc:
                    raise SystemBlockContractError(
                        f"{path} contains a non-stringable key."
                    ) from exc

                if not key:
                    raise SystemBlockContractError(
                        f"{path} contains an empty key."
                    )

                frozen[key] = _freeze_json_value(
                    raw_item,
                    path=f"{path}.{key}",
                    seen=seen,
                )

            return MappingProxyType(frozen)
        finally:
            seen.discard(value_id)

    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        value_id = id(value)

        if value_id in seen:
            raise SystemBlockContractError(
                f"{path} contains a recursive sequence reference."
            )

        seen.add(value_id)

        try:
            return tuple(
                _freeze_json_value(
                    item,
                    path=f"{path}[{index}]",
                    seen=seen,
                )
                for index, item in enumerate(value)
            )
        finally:
            seen.discard(value_id)

    if isinstance(value, Set):
        value_id = id(value)

        if value_id in seen:
            raise SystemBlockContractError(
                f"{path} contains a recursive set reference."
            )

        seen.add(value_id)

        try:
            frozen_items = [
                _freeze_json_value(
                    item,
                    path=f"{path}[]",
                    seen=seen,
                )
                for item in value
            ]

            frozen_items.sort(
                key=lambda item: _stable_json_dumps(
                    _thaw_json_value(item)
                )
            )

            return tuple(frozen_items)
        finally:
            seen.discard(value_id)

    to_dict = getattr(value, "to_dict", None)

    if callable(to_dict):
        try:
            converted = to_dict()
        except Exception as exc:
            raise SystemBlockContractError(
                f"{path} could not be converted through to_dict()."
            ) from exc

        return _freeze_json_value(
            converted,
            path=path,
            seen=seen,
        )

    raise SystemBlockContractError(
        f"{path} contains unsupported value type "
        f"'{type(value).__name__}'."
    )


def _thaw_json_value(value: Any) -> Any:
    """
    Convert frozen contract data back into ordinary JSON-safe values.
    """
    if isinstance(value, Mapping):
        return {
            str(key): _thaw_json_value(item)
            for key, item in value.items()
        }

    if isinstance(value, tuple):
        return [
            _thaw_json_value(item)
            for item in value
        ]

    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return value

    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


def make_json_safe(value: Any) -> Any:
    """
    Public JSON-safe serializer for contract output.
    """
    return _thaw_json_value(value)


def _stable_json_dumps(value: Any) -> str:
    """
    Serialize JSON deterministically for fingerprints and drift checks.
    """
    return json.dumps(
        make_json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _normalize_metadata(value: Any) -> Mapping[str, Any]:
    """
    Normalize metadata into an immutable JSON mapping.
    """
    if value is None:
        return MappingProxyType({})

    if not isinstance(value, Mapping):
        raise SystemBlockContractError(
            "metadata must be a JSON object/mapping."
        )

    frozen = _freeze_json_value(
        value,
        path="metadata",
    )

    if not isinstance(frozen, Mapping):
        raise SystemBlockContractError(
            "metadata must resolve to an object/mapping."
        )

    return frozen


# -----------------------------------------------------------------------------
# Main contract
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SystemBlockDefinition:
    """
    Immutable definition of one built-in VECTOPLAN system block.

    A definition may represent either:

    - a reserved cell state such as Air, or
    - a normal runtime block mirrored into BlockType.
    """

    system_block_id: str
    label: str
    kind: str

    definition_version: str = DEFAULT_DEFINITION_VERSION
    description: Optional[str] = None

    source: str = SYSTEM_BLOCK_SOURCE
    category: str = SYSTEM_BLOCK_CATEGORY
    status: str = SYSTEM_BLOCK_STATUS_ACTIVE

    runtime_block_type_id: Optional[str] = None
    aliases: tuple[str, ...] = field(default_factory=tuple)

    reserved_cell_value: Optional[int] = None
    persist_as_block_type: bool = False
    immutable_definition: bool = True
    inventory_visible: bool = False

    solid: bool = False
    opaque: bool = False
    placeable: bool = False
    breakable: bool = False
    selectable: bool = False
    collidable: bool = False

    emits_light: bool = False
    light_level: int = 0
    hardness: float = DEFAULT_HARDNESS
    stack_size: int = DEFAULT_STACK_SIZE

    render_mode: str = RENDER_MODE_INVISIBLE
    shape_type: str = SHAPE_TYPE_EMPTY

    default_palette_index: Optional[int] = None

    material_id: Optional[str] = None
    texture_id: Optional[str] = None
    icon_id: Optional[str] = None

    metadata: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        """
        Normalize all values and enforce cross-field invariants.
        """
        normalized_system_block_id = _normalize_identifier(
            self.system_block_id,
            field_name="system_block_id",
            max_length=SYSTEM_BLOCK_ID_MAX_LENGTH,
        )

        normalized_runtime_block_type_id = _normalize_optional_identifier(
            self.runtime_block_type_id,
            field_name="runtime_block_type_id",
            max_length=SYSTEM_BLOCK_ID_MAX_LENGTH,
        )

        normalized_source = _normalize_required_text(
            self.source,
            field_name="source",
            max_length=64,
        ).lower()

        normalized_category = _normalize_required_text(
            self.category,
            field_name="category",
            max_length=BLOCK_CATEGORY_MAX_LENGTH,
        ).lower()

        normalized_status = _normalize_required_text(
            self.status,
            field_name="status",
            max_length=32,
        ).lower()

        object.__setattr__(
            self,
            "system_block_id",
            normalized_system_block_id,
        )
        object.__setattr__(
            self,
            "runtime_block_type_id",
            normalized_runtime_block_type_id,
        )
        object.__setattr__(
            self,
            "label",
            _normalize_required_text(
                self.label,
                field_name="label",
                max_length=SYSTEM_BLOCK_LABEL_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "kind",
            _normalize_identifier(
                self.kind,
                field_name="kind",
                max_length=SYSTEM_BLOCK_KIND_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "definition_version",
            _normalize_version(self.definition_version),
        )
        object.__setattr__(
            self,
            "description",
            _normalize_optional_text(
                self.description,
                field_name="description",
                max_length=SYSTEM_BLOCK_DESCRIPTION_MAX_LENGTH,
            ),
        )
        object.__setattr__(self, "source", normalized_source)
        object.__setattr__(self, "category", normalized_category)
        object.__setattr__(self, "status", normalized_status)

        object.__setattr__(
            self,
            "aliases",
            _normalize_aliases(
                self.aliases,
                primary_ids=(
                    normalized_system_block_id,
                    normalized_runtime_block_type_id,
                ),
            ),
        )

        object.__setattr__(
            self,
            "reserved_cell_value",
            _normalize_optional_int(
                self.reserved_cell_value,
                field_name="reserved_cell_value",
                minimum=0,
            ),
        )

        for field_name, default in (
            ("persist_as_block_type", False),
            ("immutable_definition", True),
            ("inventory_visible", False),
            ("solid", False),
            ("opaque", False),
            ("placeable", False),
            ("breakable", False),
            ("selectable", False),
            ("collidable", False),
            ("emits_light", False),
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_bool(
                    getattr(self, field_name),
                    field_name=field_name,
                    default=default,
                ),
            )

        object.__setattr__(
            self,
            "light_level",
            _normalize_int(
                self.light_level,
                field_name="light_level",
                default=0,
                minimum=0,
                maximum=MAX_LIGHT_LEVEL,
            ),
        )

        object.__setattr__(
            self,
            "hardness",
            _normalize_float(
                self.hardness,
                field_name="hardness",
                default=DEFAULT_HARDNESS,
                minimum=0.0,
            ),
        )

        object.__setattr__(
            self,
            "stack_size",
            _normalize_int(
                self.stack_size,
                field_name="stack_size",
                default=DEFAULT_STACK_SIZE,
                minimum=MIN_STACK_SIZE,
                maximum=MAX_STACK_SIZE,
            ),
        )

        object.__setattr__(
            self,
            "render_mode",
            _normalize_choice(
                self.render_mode,
                field_name="render_mode",
                allowed=VALID_RENDER_MODES,
                default=RENDER_MODE_INVISIBLE,
            ),
        )

        object.__setattr__(
            self,
            "shape_type",
            _normalize_choice(
                self.shape_type,
                field_name="shape_type",
                allowed=VALID_SHAPE_TYPES,
                default=SHAPE_TYPE_EMPTY,
            ),
        )

        object.__setattr__(
            self,
            "default_palette_index",
            _normalize_optional_int(
                self.default_palette_index,
                field_name="default_palette_index",
                minimum=0,
            ),
        )

        object.__setattr__(
            self,
            "material_id",
            _normalize_optional_identifier(
                self.material_id,
                field_name="material_id",
                max_length=BLOCK_MATERIAL_ID_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "texture_id",
            _normalize_optional_identifier(
                self.texture_id,
                field_name="texture_id",
                max_length=BLOCK_TEXTURE_ID_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "icon_id",
            _normalize_optional_identifier(
                self.icon_id,
                field_name="icon_id",
                max_length=BLOCK_ICON_ID_MAX_LENGTH,
            ),
        )

        object.__setattr__(
            self,
            "metadata",
            _normalize_metadata(self.metadata),
        )

        errors = self.collect_validation_errors()

        if errors:
            raise SystemBlockDefinitionValidationError(
                system_block_id=self.system_block_id,
                errors=errors,
            )

    # ------------------------------------------------------------------
    # Derived state
    # ------------------------------------------------------------------

    @property
    def is_reserved_cell_state(self) -> bool:
        """
        Return whether this definition represents a reserved cell value.
        """
        return self.reserved_cell_value is not None

    @property
    def is_air_state(self) -> bool:
        """
        Return whether this definition represents the invariant Air state.
        """
        return (
            self.reserved_cell_value == AIR_CELL_VALUE
            and not self.persist_as_block_type
            and self.runtime_block_type_id is None
        )

    @property
    def is_persisted_runtime_block(self) -> bool:
        """
        Return whether the definition must be mirrored into BlockType.
        """
        return bool(
            self.persist_as_block_type
            and self.runtime_block_type_id
            and self.reserved_cell_value is None
        )

    @property
    def can_appear_in_inventory(self) -> bool:
        """
        Return whether the definition may become a selectable inventory item.
        """
        return bool(
            self.inventory_visible
            and self.placeable
            and self.is_persisted_runtime_block
        )

    @property
    def definition_key(self) -> str:
        """
        Return stable system definition key.
        """
        return (
            f"{self.system_block_id}@{self.definition_version}"
        )

    @property
    def runtime_identity(self) -> Optional[str]:
        """
        Return the technical runtime BlockType ID, if one exists.
        """
        return self.runtime_block_type_id

    @property
    def definition_fingerprint(self) -> str:
        """
        Return deterministic SHA-256 fingerprint of the canonical definition.
        """
        serialized = _stable_json_dumps(
            self.to_api_dict(
                include_metadata=True,
                include_fingerprint=False,
            )
        )
        return _sha256_text_cached(serialized)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def collect_validation_errors(self) -> tuple[str, ...]:
        """
        Return all cross-field validation errors.

        Primitive field errors are already raised during normalization. This
        method focuses on relationships between fields.
        """
        errors: list[str] = []

        if self.source != SYSTEM_BLOCK_SOURCE:
            errors.append(
                f"source must be '{SYSTEM_BLOCK_SOURCE}'."
            )

        if self.category != SYSTEM_BLOCK_CATEGORY:
            errors.append(
                f"category must be '{SYSTEM_BLOCK_CATEGORY}'."
            )

        if self.status != SYSTEM_BLOCK_STATUS_ACTIVE:
            errors.append(
                f"status must be '{SYSTEM_BLOCK_STATUS_ACTIVE}'."
            )

        if not self.immutable_definition:
            errors.append(
                "immutable_definition must be true for built-in system blocks."
            )

        if self.is_reserved_cell_state:
            if self.reserved_cell_value != AIR_CELL_VALUE:
                errors.append(
                    f"reserved_cell_value must be {AIR_CELL_VALUE}; "
                    "the current cell encoding reserves only Air."
                )

            if self.persist_as_block_type:
                errors.append(
                    "reserved cell states must not be persisted as BlockType."
                )

            if self.runtime_block_type_id is not None:
                errors.append(
                    "reserved cell states must not have runtime_block_type_id."
                )

            if self.default_palette_index is not None:
                errors.append(
                    "reserved cell states must not have default_palette_index."
                )

            if self.inventory_visible:
                errors.append(
                    "reserved cell states must not be visible in inventory."
                )

            if self.placeable:
                errors.append(
                    "reserved cell states must not be placeable."
                )

            if self.breakable:
                errors.append(
                    "reserved cell states must not be breakable."
                )

            if self.selectable:
                errors.append(
                    "reserved cell states must not be selectable."
                )

            if self.solid:
                errors.append(
                    "reserved Air state must not be solid."
                )

            if self.opaque:
                errors.append(
                    "reserved Air state must not be opaque."
                )

            if self.collidable:
                errors.append(
                    "reserved Air state must not be collidable."
                )

            if self.render_mode != RENDER_MODE_INVISIBLE:
                errors.append(
                    "reserved Air state must use render_mode='invisible'."
                )

            if self.shape_type != SHAPE_TYPE_EMPTY:
                errors.append(
                    "reserved Air state must use shape_type='empty'."
                )

        if self.persist_as_block_type:
            if self.runtime_block_type_id is None:
                errors.append(
                    "persisted system blocks require runtime_block_type_id."
                )

            if self.reserved_cell_value is not None:
                errors.append(
                    "persisted system blocks must not use a reserved cell value."
                )

        if not self.persist_as_block_type:
            if self.default_palette_index is not None:
                errors.append(
                    "non-persisted system definitions must not define "
                    "default_palette_index."
                )

        if self.inventory_visible and not self.placeable:
            errors.append(
                "inventory_visible requires placeable=true."
            )

        if self.inventory_visible and not self.persist_as_block_type:
            errors.append(
                "inventory_visible requires persist_as_block_type=true."
            )

        if self.placeable and self.runtime_block_type_id is None:
            errors.append(
                "placeable system blocks require runtime_block_type_id."
            )

        if self.placeable and not self.persist_as_block_type:
            errors.append(
                "placeable system blocks must be persisted as BlockType."
            )

        if self.emits_light and self.light_level <= 0:
            errors.append(
                "emits_light=true requires light_level greater than 0."
            )

        if not self.emits_light and self.light_level != 0:
            errors.append(
                "emits_light=false requires light_level=0."
            )

        if self.render_mode == RENDER_MODE_INVISIBLE and self.opaque:
            errors.append(
                "invisible blocks must not be opaque."
            )

        return tuple(errors)

    def validate(self) -> bool:
        """
        Return true when the definition is valid.
        """
        return not self.collect_validation_errors()

    def require_valid(self) -> None:
        """
        Raise when the definition is not valid.
        """
        errors = self.collect_validation_errors()

        if errors:
            raise SystemBlockDefinitionValidationError(
                system_block_id=self.system_block_id,
                errors=errors,
            )

    def require_persistable(self) -> None:
        """
        Raise unless this definition may be mirrored into BlockType.
        """
        if not self.is_persisted_runtime_block:
            raise SystemBlockPersistenceError(
                f"System block '{self.system_block_id}' is not a persistent "
                "runtime BlockType definition."
            )

    def require_palette_compatible(self) -> None:
        """
        Raise unless this definition may become a positive palette entry.
        """
        if not self.is_persisted_runtime_block:
            raise SystemBlockPaletteError(
                f"System block '{self.system_block_id}' cannot be represented "
                "as a positive palette entry."
            )

    # ------------------------------------------------------------------
    # Metadata construction
    # ------------------------------------------------------------------

    def build_system_metadata(self) -> dict[str, Any]:
        """
        Build authoritative metadata written into BlockType.metadata_json.

        User-defined metadata is preserved, but the namespaced system metadata
        is always generated by the contract and cannot be overridden.
        """
        metadata = make_json_safe(self.metadata)

        if not isinstance(metadata, dict):
            metadata = {}

        existing_namespace = metadata.get(SYSTEM_BLOCK_METADATA_NAMESPACE)

        if isinstance(existing_namespace, Mapping):
            conflicting_keys = (
                set(existing_namespace.keys())
                & set(_RESERVED_SYSTEM_METADATA_KEYS)
            )

            if conflicting_keys:
                metadata = dict(metadata)
                metadata.pop(SYSTEM_BLOCK_METADATA_NAMESPACE, None)

        metadata[SYSTEM_BLOCK_METADATA_NAMESPACE] = {
            "schemaVersion": SYSTEM_BLOCK_METADATA_SCHEMA_VERSION,
            "source": SYSTEM_BLOCK_SOURCE,
            "systemBlockId": self.system_block_id,
            "runtimeBlockTypeId": self.runtime_block_type_id,
            "kind": self.kind,
            "definitionVersion": self.definition_version,
            "definitionFingerprint": self.definition_fingerprint,
            "immutableDefinition": self.immutable_definition,
            "persistAsBlockType": self.persist_as_block_type,
            "inventoryVisible": self.inventory_visible,
            "reservedCellValue": self.reserved_cell_value,
            "cellEncodingVersion": CELL_ENCODING_VERSION,
            "airCellValue": AIR_CELL_VALUE,
            "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
        }

        return metadata

    # ------------------------------------------------------------------
    # Persistence mapping
    # ------------------------------------------------------------------

    def to_persistent_block_values(
        self,
        *,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        """
        Return values compatible with the current BlockType.create contract.

        Registry-specific fields are intentionally excluded:

        - registry_db_id
        - registry_id
        - registry_version
        - created_by_user_id

        The bootstrap layer supplies those values for the active world registry.
        """
        self.require_persistable()

        values: dict[str, Any] = {
            "block_type_id": self.runtime_block_type_id,
            "label": self.label,
            "description": self.description,
            "status": self.status,
            "category": self.category,
            "default_palette_index": self.default_palette_index,
            "solid": self.solid,
            "opaque": self.opaque,
            "placeable": self.placeable,
            "breakable": self.breakable,
            "selectable": self.selectable,
            "collidable": self.collidable,
            "emits_light": self.emits_light,
            "light_level": self.light_level,
            "hardness": self.hardness,
            "stack_size": self.stack_size,
            "render_mode": self.render_mode,
            "shape_type": self.shape_type,
            "material_id": self.material_id,
            "texture_id": self.texture_id,
            "icon_id": self.icon_id,
            "library_type_id": None,
            "library_variant_id": None,
        }

        if include_metadata:
            values["metadata_json"] = self.build_system_metadata()

        return values

    def expected_persistent_attributes(
        self,
        *,
        include_metadata: bool = False,
    ) -> Mapping[str, Any]:
        """
        Return immutable expected attributes for drift comparison.
        """
        return MappingProxyType(
            self.to_persistent_block_values(
                include_metadata=include_metadata,
            )
        )

    def compare_block_type(
        self,
        block_type: Any,
        *,
        include_metadata: bool = True,
        float_tolerance: float = 1e-9,
    ) -> dict[str, dict[str, Any]]:
        """
        Compare this definition against a BlockType-like object.

        The return value maps drifted attribute names to:

        {
            "expected": ...,
            "actual": ...
        }

        This method does not mutate the object and does not depend on SQLAlchemy.
        """
        self.require_persistable()

        expected = self.to_persistent_block_values(
            include_metadata=False,
        )

        drift: dict[str, dict[str, Any]] = {}

        for attribute_name, expected_value in expected.items():
            try:
                actual_value = getattr(block_type, attribute_name)
            except Exception:
                actual_value = None

            if isinstance(expected_value, float):
                try:
                    actual_float = float(actual_value)
                    values_match = math.isclose(
                        actual_float,
                        expected_value,
                        rel_tol=float_tolerance,
                        abs_tol=float_tolerance,
                    )
                except Exception:
                    values_match = False
            else:
                values_match = actual_value == expected_value

            if not values_match:
                drift[attribute_name] = {
                    "expected": make_json_safe(expected_value),
                    "actual": make_json_safe(actual_value),
                }

        if include_metadata:
            expected_namespace = self.build_system_metadata().get(
                SYSTEM_BLOCK_METADATA_NAMESPACE,
                {},
            )

            try:
                actual_metadata = getattr(block_type, "metadata_json", {})
            except Exception:
                actual_metadata = {}

            if not isinstance(actual_metadata, Mapping):
                actual_metadata = {}

            actual_namespace = actual_metadata.get(
                SYSTEM_BLOCK_METADATA_NAMESPACE,
                {},
            )

            if make_json_safe(actual_namespace) != make_json_safe(
                expected_namespace
            ):
                drift["metadata_json"] = {
                    "expected": {
                        SYSTEM_BLOCK_METADATA_NAMESPACE: make_json_safe(
                            expected_namespace
                        )
                    },
                    "actual": {
                        SYSTEM_BLOCK_METADATA_NAMESPACE: make_json_safe(
                            actual_namespace
                        )
                    },
                }

        return drift

    # ------------------------------------------------------------------
    # Runtime/palette serialization
    # ------------------------------------------------------------------

    def to_palette_entry(
        self,
        *,
        palette_index: int,
        registry_id: Optional[str] = None,
        registry_version: Optional[str] = None,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        """
        Serialize this definition as a positive chunk palette entry.
        """
        self.require_palette_compatible()

        normalized_palette_index = _normalize_int(
            palette_index,
            field_name="palette_index",
            default=0,
            minimum=0,
        )

        normalized_registry_id = _normalize_optional_identifier(
            registry_id,
            field_name="registry_id",
            max_length=128,
        )

        normalized_registry_version = (
            _normalize_version(
                registry_version,
                field_name="registry_version",
            )
            if registry_version is not None
            else None
        )

        result: dict[str, Any] = {
            "paletteIndex": normalized_palette_index,
            "cellValue": normalized_palette_index + 1,
            "blockTypeId": self.runtime_block_type_id,
            "systemBlockId": self.system_block_id,
            "source": self.source,
            "category": self.category,
            "kind": self.kind,
            "definitionVersion": self.definition_version,
            "label": self.label,
            "solid": self.solid,
            "opaque": self.opaque,
            "placeable": self.placeable,
            "breakable": self.breakable,
            "selectable": self.selectable,
            "collidable": self.collidable,
            "emitsLight": self.emits_light,
            "lightLevel": self.light_level,
            "renderMode": self.render_mode,
            "shapeType": self.shape_type,
            "materialId": self.material_id,
            "textureId": self.texture_id,
            "iconId": self.icon_id,
            "registryId": normalized_registry_id,
            "registryVersion": normalized_registry_version,
        }

        if include_metadata:
            result["metadata"] = self.build_system_metadata()

        return result

    # ------------------------------------------------------------------
    # API serialization
    # ------------------------------------------------------------------

    def to_api_dict(
        self,
        *,
        include_metadata: bool = True,
        include_fingerprint: bool = True,
    ) -> dict[str, Any]:
        """
        Serialize the complete system-block definition for API responses.
        """
        result: dict[str, Any] = {
            "schemaVersion": SYSTEM_BLOCK_DEFINITION_SCHEMA_VERSION,
            "systemBlockId": self.system_block_id,
            "runtimeBlockTypeId": self.runtime_block_type_id,
            "definitionKey": self.definition_key,
            "definitionVersion": self.definition_version,
            "source": self.source,
            "category": self.category,
            "status": self.status,
            "kind": self.kind,
            "label": self.label,
            "description": self.description,
            "aliases": list(self.aliases),
            "reservedCellValue": self.reserved_cell_value,
            "persistAsBlockType": self.persist_as_block_type,
            "immutableDefinition": self.immutable_definition,
            "inventoryVisible": self.inventory_visible,
            "render": {
                "visible": self.render_mode != RENDER_MODE_INVISIBLE,
                "renderMode": self.render_mode,
                "shapeType": self.shape_type,
                "materialId": self.material_id,
                "textureId": self.texture_id,
                "iconId": self.icon_id,
                "opaque": self.opaque,
            },
            "physics": {
                "solid": self.solid,
                "collidable": self.collidable,
                "hardness": self.hardness,
            },
            "capabilities": {
                "placeable": self.placeable,
                "breakable": self.breakable,
                "selectable": self.selectable,
                "inventoryVisible": self.inventory_visible,
            },
            "lighting": {
                "emitsLight": self.emits_light,
                "lightLevel": self.light_level,
            },
            "stackSize": self.stack_size,
            "defaultPaletteIndex": self.default_palette_index,
            "cellEncoding": {
                "version": CELL_ENCODING_VERSION,
                "airCellValue": AIR_CELL_VALUE,
                "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
                "reservedCellValue": self.reserved_cell_value,
                "usesPositivePaletteEntry": (
                    self.is_persisted_runtime_block
                ),
            },
            "flags": {
                "reservedCellState": self.is_reserved_cell_state,
                "airState": self.is_air_state,
                "persistedRuntimeBlock": self.is_persisted_runtime_block,
                "canAppearInInventory": self.can_appear_in_inventory,
                "valid": self.validate(),
            },
        }

        if include_metadata:
            result["metadata"] = make_json_safe(self.metadata)

        if include_fingerprint:
            result["definitionFingerprint"] = self.definition_fingerprint

        return result

    def to_dict(
        self,
        *,
        include_metadata: bool = True,
        include_fingerprint: bool = True,
    ) -> dict[str, Any]:
        """
        Alias for API-friendly serialization.
        """
        return self.to_api_dict(
            include_metadata=include_metadata,
            include_fingerprint=include_fingerprint,
        )


# -----------------------------------------------------------------------------
# Public helper functions
# -----------------------------------------------------------------------------

def validate_system_block_definition(
    definition: SystemBlockDefinition,
) -> tuple[str, ...]:
    """
    Validate one definition and return all errors.
    """
    if not isinstance(definition, SystemBlockDefinition):
        return (
            "definition must be an instance of SystemBlockDefinition.",
        )

    return definition.collect_validation_errors()


def require_system_block_definition(
    value: Any,
) -> SystemBlockDefinition:
    """
    Return a valid SystemBlockDefinition or raise a contract error.
    """
    if not isinstance(value, SystemBlockDefinition):
        raise SystemBlockContractError(
            "Expected SystemBlockDefinition instance."
        )

    value.require_valid()
    return value


def serialize_system_block_definition(
    definition: SystemBlockDefinition,
    *,
    include_metadata: bool = True,
    include_fingerprint: bool = True,
) -> dict[str, Any]:
    """
    Serialize one validated definition.
    """
    resolved = require_system_block_definition(definition)

    return resolved.to_api_dict(
        include_metadata=include_metadata,
        include_fingerprint=include_fingerprint,
    )


def clear_system_block_contract_caches() -> None:
    """
    Clear bounded module caches.

    Intended for tests and development reload tooling. Normal runtime code
    should not need to call this function.
    """
    _type_id_pattern.cache_clear()
    _version_pattern.cache_clear()
    _normalize_identifier_cached.cache_clear()
    _normalize_version_cached.cache_clear()
    _sha256_text_cached.cache_clear()
    get_system_block_contract_descriptor.cache_clear()


__all__ = [
    "AIR_CELL_VALUE",
    "BLOCK_CELL_VALUE_RULE",
    "CELL_ENCODING_VERSION",
    "DEFAULT_DEFINITION_VERSION",
    "RENDER_MODE_CUBE",
    "RENDER_MODE_CUSTOM",
    "RENDER_MODE_INVISIBLE",
    "RENDER_MODE_MESH",
    "SHAPE_TYPE_CUBE",
    "SHAPE_TYPE_CUSTOM",
    "SHAPE_TYPE_EMPTY",
    "SYSTEM_BLOCK_CATEGORY",
    "SYSTEM_BLOCK_DEFINITION_SCHEMA_VERSION",
    "SYSTEM_BLOCK_METADATA_NAMESPACE",
    "SYSTEM_BLOCK_METADATA_SCHEMA_VERSION",
    "SYSTEM_BLOCK_SOURCE",
    "SYSTEM_BLOCK_STATUS_ACTIVE",
    "SystemBlockContractError",
    "SystemBlockDefinition",
    "SystemBlockDefinitionValidationError",
    "SystemBlockPaletteError",
    "SystemBlockPersistenceError",
    "clear_system_block_contract_caches",
    "get_system_block_contract_descriptor",
    "make_json_safe",
    "require_system_block_definition",
    "serialize_system_block_definition",
    "validate_system_block_definition",
]