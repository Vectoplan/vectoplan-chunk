# services/vectoplan-chunk/src/system_blocks/railing/definition.py
"""
Canonical built-in Railing definition for ``vectoplan-chunk``.

The Railing system block is a normal positive runtime block. Unlike Air, it is
mirrored into the active world's persistent ``BlockRegistry`` as a
``BlockType`` and can therefore use the existing block lifecycle:

    SetBlock
    -> resolve BlockType in the world registry
    -> add or reuse a chunk palette entry
    -> write the positive cell value
    -> persist ChunkSnapshot
    -> append ChunkEvent
    -> update WorldCommandLog

Version 1 deliberately uses the simplest supported runtime representation:

- one world cell
- cube rendering
- cube shape
- full-cell collision
- solid
- opaque
- selectable
- placeable
- breakable
- inventory-visible
- no orientation
- no neighbour connection
- no multi-block object instance

The dedicated Railing package allows later versions to introduce:

- orientation
- narrow or partial collision shapes
- connected railing segments
- corners
- end pieces
- posts
- material variants
- custom meshes
- neighbour-aware rendering
- object-backed composite railings

without changing the stable runtime block identifier:

    system_railing

Important boundaries:

- no Flask imports
- no SQLAlchemy imports
- no database access
- no database commits
- no route registration
- no bootstrap side effects during import
- no fixed global cell value
- no assumption about the concrete palette index

The actual Railing ``cellValue`` always remains:

    paletteIndex + 1

for the concrete chunk palette in which the block occurs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence, Set
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Final, Optional


# -----------------------------------------------------------------------------
# Shared contract import
#
# Relative import is the canonical package path. The fallback supports selected
# development and test environments in which ``src`` is exposed as the
# top-level package.
# -----------------------------------------------------------------------------

try:
    from ..contracts import (
        BLOCK_CELL_VALUE_RULE,
        CELL_ENCODING_VERSION,
        RENDER_MODE_CUBE,
        SHAPE_TYPE_CUBE,
        SYSTEM_BLOCK_CATEGORY,
        SYSTEM_BLOCK_SOURCE,
        SystemBlockContractError,
        SystemBlockDefinition,
        SystemBlockPaletteError,
        SystemBlockPersistenceError,
        make_json_safe,
        require_system_block_definition,
    )
except ImportError:
    try:
        from src.system_blocks.contracts import (
            BLOCK_CELL_VALUE_RULE,
            CELL_ENCODING_VERSION,
            RENDER_MODE_CUBE,
            SHAPE_TYPE_CUBE,
            SYSTEM_BLOCK_CATEGORY,
            SYSTEM_BLOCK_SOURCE,
            SystemBlockContractError,
            SystemBlockDefinition,
            SystemBlockPaletteError,
            SystemBlockPersistenceError,
            make_json_safe,
            require_system_block_definition,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Could not import the shared system-block contract while loading "
            "the built-in Railing definition. Ensure "
            "'services/vectoplan-chunk/src/system_blocks/contracts.py' exists "
            "and the vectoplan-chunk source root is available on PYTHONPATH."
        ) from exc


# -----------------------------------------------------------------------------
# Public Railing constants
# -----------------------------------------------------------------------------

RAILING_SYSTEM_BLOCK_ID: Final[str] = "system_railing"
RAILING_RUNTIME_BLOCK_TYPE_ID: Final[str] = "system_railing"

RAILING_KIND: Final[str] = "railing"
RAILING_LABEL: Final[str] = "Railing"
RAILING_DEFINITION_VERSION: Final[str] = "1"

RAILING_DESCRIPTION: Final[str] = (
    "Built-in VECTOPLAN railing block. Version 1 uses a full one-cell cube "
    "representation and full-cell collision while preserving a stable system "
    "block identity for later custom railing geometry."
)

RAILING_DEFINITION_MODULE_VERSION: Final[str] = "1.0.0"

RAILING_PERSIST_AS_BLOCK_TYPE: Final[bool] = True
RAILING_IMMUTABLE_DEFINITION: Final[bool] = True
RAILING_INVENTORY_VISIBLE: Final[bool] = True

RAILING_SOLID: Final[bool] = True
RAILING_OPAQUE: Final[bool] = True
RAILING_PLACEABLE: Final[bool] = True
RAILING_BREAKABLE: Final[bool] = True
RAILING_SELECTABLE: Final[bool] = True
RAILING_COLLIDABLE: Final[bool] = True

RAILING_TARGETABLE: Final[bool] = True
RAILING_REPLACEABLE: Final[bool] = False

RAILING_EMITS_LIGHT: Final[bool] = False
RAILING_LIGHT_LEVEL: Final[int] = 0

RAILING_HARDNESS: Final[float] = 1.0
RAILING_STACK_SIZE: Final[int] = 64

RAILING_RENDER_MODE: Final[str] = RENDER_MODE_CUBE
RAILING_SHAPE_TYPE: Final[str] = SHAPE_TYPE_CUBE

# No registry-wide palette position is reserved. The concrete chunk cell value
# is generated dynamically from the chunk-local palette.
RAILING_DEFAULT_PALETTE_INDEX: Final[None] = None
RAILING_RESERVED_CELL_VALUE: Final[None] = None

# Version 1 intentionally relies on the runtime's generic cube fallback. No
# external Library asset or debug asset is required by the canonical contract.
RAILING_MATERIAL_ID: Final[None] = None
RAILING_TEXTURE_ID: Final[None] = None
RAILING_ICON_ID: Final[None] = None

RAILING_PLACEMENT_COMMAND: Final[str] = "SetBlock"
RAILING_REMOVAL_COMMAND: Final[str] = "RemoveBlock"

RAILING_CURRENT_GEOMETRY: Final[str] = "full_cube"
RAILING_CURRENT_COLLISION: Final[str] = "full_cube"

RAILING_FUTURE_GEOMETRY: Final[str] = "railing"
RAILING_ORIENTATION_SUPPORTED: Final[bool] = False
RAILING_NEIGHBOUR_CONNECTION_SUPPORTED: Final[bool] = False
RAILING_MULTI_BLOCK_OBJECT: Final[bool] = False

RAILING_SYSTEM_CATALOG_SCHEMA_VERSION: Final[str] = (
    "system-railing-catalog-entry.schema.v1"
)

RAILING_STATUS_SCHEMA_VERSION: Final[str] = (
    "system-railing-definition-status.schema.v1"
)


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class RailingDefinitionError(RuntimeError):
    """
    Base error for failures involving the built-in Railing definition.
    """


class RailingInvariantError(RailingDefinitionError):
    """
    Raised when a Railing definition violates canonical invariants.
    """

    def __init__(
        self,
        errors: Sequence[str],
    ) -> None:
        normalized_errors = _normalize_error_messages(errors)

        self.errors = normalized_errors

        details = (
            "; ".join(normalized_errors)
            if normalized_errors
            else "unknown Railing invariant violation"
        )

        super().__init__(
            f"Invalid built-in Railing definition: {details}"
        )


class RailingSerializationError(RailingDefinitionError):
    """
    Raised when the Railing definition cannot be serialized safely.
    """


# -----------------------------------------------------------------------------
# Safe primitive helpers
# -----------------------------------------------------------------------------

def _safe_exception_text(error: BaseException | Any) -> str:
    """
    Return a stable exception message without allowing error reporting to fail.
    """
    try:
        text = str(error).strip()
    except Exception:
        text = ""

    return text or type(error).__name__


def _normalize_error_messages(
    errors: Sequence[Any] | Set[Any] | None,
) -> tuple[str, ...]:
    """
    Normalize, deduplicate and preserve the order of diagnostic messages.
    """
    if errors is None:
        return tuple()

    normalized: list[str] = []
    seen: set[str] = set()

    try:
        values = tuple(errors)
    except Exception:
        values = (errors,)

    for error in values:
        try:
            text = str(error).strip()
        except Exception:
            text = type(error).__name__

        if not text or text in seen:
            continue

        seen.add(text)
        normalized.append(text)

    return tuple(normalized)


def _deep_freeze(
    value: Any,
    *,
    path: str = "value",
    seen: Optional[set[int]] = None,
) -> Any:
    """
    Convert supported metadata values into deeply immutable structures.

    Mappings become ``MappingProxyType`` values and sequences become tuples.
    Recursive references are rejected.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        if not math.isfinite(value):
            raise RailingDefinitionError(
                f"{path} contains a non-finite number."
            )
        return value

    if seen is None:
        seen = set()

    if isinstance(value, Mapping):
        value_id = id(value)

        if value_id in seen:
            raise RailingDefinitionError(
                f"{path} contains a recursive mapping reference."
            )

        seen.add(value_id)

        try:
            result: dict[str, Any] = {}

            for raw_key, raw_item in value.items():
                try:
                    key = str(raw_key).strip()
                except Exception as exc:
                    raise RailingDefinitionError(
                        f"{path} contains a non-stringable key."
                    ) from exc

                if not key:
                    raise RailingDefinitionError(
                        f"{path} contains an empty key."
                    )

                result[key] = _deep_freeze(
                    raw_item,
                    path=f"{path}.{key}",
                    seen=seen,
                )

            return MappingProxyType(result)
        finally:
            seen.discard(value_id)

    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        value_id = id(value)

        if value_id in seen:
            raise RailingDefinitionError(
                f"{path} contains a recursive sequence reference."
            )

        seen.add(value_id)

        try:
            return tuple(
                _deep_freeze(
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
            raise RailingDefinitionError(
                f"{path} contains a recursive set reference."
            )

        seen.add(value_id)

        try:
            frozen_items = [
                _deep_freeze(
                    item,
                    path=f"{path}[]",
                    seen=seen,
                )
                for item in value
            ]

            frozen_items.sort(
                key=lambda item: repr(make_json_safe(item))
            )

            return tuple(frozen_items)
        finally:
            seen.discard(value_id)

    to_dict = getattr(value, "to_dict", None)

    if callable(to_dict):
        try:
            converted = to_dict()
        except Exception as exc:
            raise RailingDefinitionError(
                f"{path} could not be converted through to_dict()."
            ) from exc

        return _deep_freeze(
            converted,
            path=path,
            seen=seen,
        )

    raise RailingDefinitionError(
        f"{path} contains unsupported value type "
        f"'{type(value).__name__}'."
    )


def _read_attribute(
    value: Any,
    attribute_name: str,
    fallback: Any = None,
) -> Any:
    """
    Read one attribute from an arbitrary BlockType-like object.
    """
    if value is None:
        return fallback

    try:
        return getattr(value, attribute_name)
    except Exception:
        return fallback


# -----------------------------------------------------------------------------
# Cached immutable metadata
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_railing_metadata() -> Mapping[str, Any]:
    """
    Return deeply immutable descriptive metadata for the Railing definition.

    This metadata documents semantics not represented by direct ``BlockType``
    columns. The bootstrap layer writes it into ``BlockType.metadata_json``
    through the shared contract's namespaced system metadata.
    """
    metadata = {
        "semanticRole": "railing",
        "runtimeRole": "single_cell_system_block",
        "storageMode": "persistent_block_type",
        "builtIn": True,
        "immutableDefinition": RAILING_IMMUTABLE_DEFINITION,
        "definitionModuleVersion": RAILING_DEFINITION_MODULE_VERSION,
        "persistence": {
            "blockTypeRow": True,
            "paletteEntry": True,
            "reservedCellValue": None,
            "fixedGlobalCellValue": False,
            "cellValueRule": BLOCK_CELL_VALUE_RULE,
        },
        "cellEncoding": {
            "version": CELL_ENCODING_VERSION,
            "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
        },
        "commands": {
            "placementCommand": RAILING_PLACEMENT_COMMAND,
            "removalCommand": RAILING_REMOVAL_COMMAND,
            "requiresNewCommandType": False,
        },
        "editor": {
            "inventoryVisible": RAILING_INVENTORY_VISIBLE,
            "selectable": RAILING_SELECTABLE,
            "targetable": RAILING_TARGETABLE,
            "replaceable": RAILING_REPLACEABLE,
            "runtimeBlockTypeId": RAILING_RUNTIME_BLOCK_TYPE_ID,
        },
        "rendering": {
            "visible": True,
            "createsMesh": True,
            "renderMode": RAILING_RENDER_MODE,
            "shapeType": RAILING_SHAPE_TYPE,
            "currentGeometry": RAILING_CURRENT_GEOMETRY,
            "futureGeometry": RAILING_FUTURE_GEOMETRY,
            "materialStrategy": "runtime_default_cube",
            "textureStrategy": "runtime_default_cube",
        },
        "physics": {
            "solid": RAILING_SOLID,
            "collidable": RAILING_COLLIDABLE,
            "currentCollision": RAILING_CURRENT_COLLISION,
            "blocksMovement": True,
        },
        "capabilities": {
            "orientationSupported": RAILING_ORIENTATION_SUPPORTED,
            "neighbourConnectionSupported": (
                RAILING_NEIGHBOUR_CONNECTION_SUPPORTED
            ),
            "multiBlockObject": RAILING_MULTI_BLOCK_OBJECT,
        },
        "futureCompatibility": {
            "stableRuntimeBlockTypeId": RAILING_RUNTIME_BLOCK_TYPE_ID,
            "geometryMayChange": True,
            "collisionMayChange": True,
            "orientationMayBeAdded": True,
            "neighbourConnectionsMayBeAdded": True,
        },
    }

    frozen = _deep_freeze(
        metadata,
        path="railingMetadata",
    )

    if not isinstance(frozen, Mapping):
        raise RailingDefinitionError(
            "Railing metadata did not resolve to an immutable mapping."
        )

    return frozen


# -----------------------------------------------------------------------------
# Definition construction
# -----------------------------------------------------------------------------

def _build_railing_definition() -> SystemBlockDefinition:
    """
    Construct the canonical immutable Railing definition.

    The resulting contract is SQLAlchemy-independent. The bootstrap layer later
    supplies registry-specific values when mirroring it into ``BlockType``.
    """
    return SystemBlockDefinition(
        system_block_id=RAILING_SYSTEM_BLOCK_ID,
        runtime_block_type_id=RAILING_RUNTIME_BLOCK_TYPE_ID,
        label=RAILING_LABEL,
        description=RAILING_DESCRIPTION,
        kind=RAILING_KIND,
        definition_version=RAILING_DEFINITION_VERSION,
        source=SYSTEM_BLOCK_SOURCE,
        category=SYSTEM_BLOCK_CATEGORY,
        reserved_cell_value=RAILING_RESERVED_CELL_VALUE,
        persist_as_block_type=RAILING_PERSIST_AS_BLOCK_TYPE,
        immutable_definition=RAILING_IMMUTABLE_DEFINITION,
        inventory_visible=RAILING_INVENTORY_VISIBLE,
        solid=RAILING_SOLID,
        opaque=RAILING_OPAQUE,
        placeable=RAILING_PLACEABLE,
        breakable=RAILING_BREAKABLE,
        selectable=RAILING_SELECTABLE,
        collidable=RAILING_COLLIDABLE,
        emits_light=RAILING_EMITS_LIGHT,
        light_level=RAILING_LIGHT_LEVEL,
        hardness=RAILING_HARDNESS,
        stack_size=RAILING_STACK_SIZE,
        render_mode=RAILING_RENDER_MODE,
        shape_type=RAILING_SHAPE_TYPE,
        default_palette_index=RAILING_DEFAULT_PALETTE_INDEX,
        material_id=RAILING_MATERIAL_ID,
        texture_id=RAILING_TEXTURE_ID,
        icon_id=RAILING_ICON_ID,
        aliases=(),
        metadata=get_railing_metadata(),
    )


@lru_cache(maxsize=1)
def get_railing_definition() -> SystemBlockDefinition:
    """
    Return the canonical cached Railing definition.

    The same immutable instance is reused by:

    - the system-block catalog,
    - bootstrap reconciliation,
    - route serialization,
    - readiness checks,
    - tests,
    - command-level reserved-ID checks.
    """
    try:
        definition = _build_railing_definition()
        require_railing_definition(definition)
        return definition
    except RailingDefinitionError:
        raise
    except SystemBlockContractError as exc:
        raise RailingDefinitionError(
            "The Railing definition violates the shared system-block contract."
        ) from exc
    except Exception as exc:
        raise RailingDefinitionError(
            "Could not construct the built-in Railing definition."
        ) from exc


# -----------------------------------------------------------------------------
# Railing-specific invariant validation
# -----------------------------------------------------------------------------

def collect_railing_invariant_errors(
    definition: Any,
) -> tuple[str, ...]:
    """
    Return all canonical Railing invariant violations.

    The shared contract validates general system-block semantics. This function
    additionally guarantees the exact Version-1 Railing definition.
    """
    errors: list[str] = []

    if not isinstance(definition, SystemBlockDefinition):
        return (
            "definition must be an instance of SystemBlockDefinition.",
        )

    try:
        errors.extend(
            definition.collect_validation_errors()
        )
    except Exception as exc:
        errors.append(
            "shared contract validation failed: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

    expected_values = {
        "system_block_id": RAILING_SYSTEM_BLOCK_ID,
        "runtime_block_type_id": RAILING_RUNTIME_BLOCK_TYPE_ID,
        "kind": RAILING_KIND,
        "label": RAILING_LABEL,
        "definition_version": RAILING_DEFINITION_VERSION,
        "source": SYSTEM_BLOCK_SOURCE,
        "category": SYSTEM_BLOCK_CATEGORY,
        "reserved_cell_value": RAILING_RESERVED_CELL_VALUE,
        "persist_as_block_type": RAILING_PERSIST_AS_BLOCK_TYPE,
        "immutable_definition": RAILING_IMMUTABLE_DEFINITION,
        "inventory_visible": RAILING_INVENTORY_VISIBLE,
        "solid": RAILING_SOLID,
        "opaque": RAILING_OPAQUE,
        "placeable": RAILING_PLACEABLE,
        "breakable": RAILING_BREAKABLE,
        "selectable": RAILING_SELECTABLE,
        "collidable": RAILING_COLLIDABLE,
        "emits_light": RAILING_EMITS_LIGHT,
        "light_level": RAILING_LIGHT_LEVEL,
        "hardness": RAILING_HARDNESS,
        "stack_size": RAILING_STACK_SIZE,
        "render_mode": RAILING_RENDER_MODE,
        "shape_type": RAILING_SHAPE_TYPE,
        "default_palette_index": RAILING_DEFAULT_PALETTE_INDEX,
        "material_id": RAILING_MATERIAL_ID,
        "texture_id": RAILING_TEXTURE_ID,
        "icon_id": RAILING_ICON_ID,
    }

    for attribute_name, expected_value in expected_values.items():
        actual_value = _read_attribute(
            definition,
            attribute_name,
            fallback="<missing>",
        )

        if isinstance(expected_value, float):
            try:
                matches = math.isclose(
                    float(actual_value),
                    expected_value,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            except Exception:
                matches = False
        else:
            matches = actual_value == expected_value

        if not matches:
            errors.append(
                f"{attribute_name} must be "
                f"{expected_value!r}, got {actual_value!r}."
            )

    try:
        if definition.is_reserved_cell_state:
            errors.append(
                "Railing must not be recognized as a reserved cell state."
            )
    except Exception as exc:
        errors.append(
            "could not evaluate reserved-cell-state flag: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

    try:
        if definition.is_air_state:
            errors.append(
                "Railing must not be recognized as Air."
            )
    except Exception as exc:
        errors.append(
            "could not evaluate Air-state flag: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

    try:
        if not definition.is_persisted_runtime_block:
            errors.append(
                "Railing must be recognized as a persisted runtime block."
            )
    except Exception as exc:
        errors.append(
            "could not evaluate persisted-runtime-block flag: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

    try:
        if not definition.can_appear_in_inventory:
            errors.append(
                "Railing must be eligible for editor inventory."
            )
    except Exception as exc:
        errors.append(
            "could not evaluate inventory eligibility: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

    try:
        definition.require_persistable()
    except Exception as exc:
        errors.append(
            "Railing must satisfy persistent BlockType requirements: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

    try:
        definition.require_palette_compatible()
    except Exception as exc:
        errors.append(
            "Railing must satisfy positive palette-entry requirements: "
            f"{type(exc).__name__}: {_safe_exception_text(exc)}"
        )

    return _normalize_error_messages(errors)


def validate_railing_definition(
    definition: Optional[SystemBlockDefinition] = None,
) -> bool:
    """
    Return whether a definition satisfies every canonical Railing invariant.

    When no definition is supplied, the cached built-in definition is checked.
    """
    resolved = definition

    if resolved is None:
        try:
            resolved = get_railing_definition()
        except Exception:
            return False

    return not collect_railing_invariant_errors(resolved)


def require_railing_definition(
    definition: Any,
) -> SystemBlockDefinition:
    """
    Return a valid canonical Railing definition or raise.
    """
    try:
        resolved = require_system_block_definition(definition)
    except Exception as exc:
        raise RailingInvariantError(
            (
                "definition does not satisfy the shared system-block contract: "
                f"{type(exc).__name__}: {_safe_exception_text(exc)}",
            )
        ) from exc

    errors = collect_railing_invariant_errors(resolved)

    if errors:
        raise RailingInvariantError(errors)

    return resolved


def require_railing_definition_ready() -> SystemBlockDefinition:
    """
    Return the cached Railing definition after all readiness checks.
    """
    definition = get_railing_definition()
    return require_railing_definition(definition)


# -----------------------------------------------------------------------------
# Identification helpers
# -----------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _is_railing_system_block_id_cached(
    value: str,
) -> bool:
    """
    Cached comparison against the canonical system-block ID.
    """
    return (
        value.strip().lower()
        == RAILING_SYSTEM_BLOCK_ID.lower()
    )


def is_railing_system_block_id(value: Any) -> bool:
    """
    Return whether a value identifies the canonical Railing system definition.
    """
    if value is None:
        return False

    try:
        text = str(value)
    except Exception:
        return False

    if not text.strip():
        return False

    return _is_railing_system_block_id_cached(text)


@lru_cache(maxsize=512)
def _is_railing_runtime_block_type_id_cached(
    value: str,
) -> bool:
    """
    Cached comparison against the runtime BlockType ID.
    """
    return (
        value.strip().lower()
        == RAILING_RUNTIME_BLOCK_TYPE_ID.lower()
    )


def is_railing_runtime_block_type_id(
    value: Any,
) -> bool:
    """
    Return whether a value identifies the persistent Railing BlockType.
    """
    if value is None:
        return False

    try:
        text = str(value)
    except Exception:
        return False

    if not text.strip():
        return False

    return _is_railing_runtime_block_type_id_cached(text)


def is_railing_identifier(value: Any) -> bool:
    """
    Return whether a value matches either supported Railing identity.
    """
    return bool(
        is_railing_system_block_id(value)
        or is_railing_runtime_block_type_id(value)
    )


# -----------------------------------------------------------------------------
# Persistence mapping
# -----------------------------------------------------------------------------

def build_railing_persistent_values(
    *,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """
    Return ``BlockType.create``-compatible Railing values.

    Registry-specific fields are deliberately excluded:

    - registry_db_id
    - registry_id
    - registry_version
    - created_by_user_id

    The bootstrap layer supplies those values for the registry assigned to the
    concrete ``WorldInstance``.
    """
    definition = require_railing_definition_ready()

    try:
        return definition.to_persistent_block_values(
            include_metadata=include_metadata,
        )
    except SystemBlockPersistenceError:
        raise
    except Exception as exc:
        raise RailingDefinitionError(
            "Could not build persistent BlockType values for Railing."
        ) from exc


def compare_railing_block_type(
    block_type: Any,
    *,
    include_metadata: bool = True,
    float_tolerance: float = 1e-9,
) -> dict[str, dict[str, Any]]:
    """
    Compare an existing BlockType-like object against the canonical definition.

    This function performs no mutation and no database access.
    """
    definition = require_railing_definition_ready()

    try:
        return definition.compare_block_type(
            block_type,
            include_metadata=include_metadata,
            float_tolerance=float_tolerance,
        )
    except Exception as exc:
        raise RailingDefinitionError(
            "Could not compare the existing BlockType with the canonical "
            "Railing definition."
        ) from exc


def is_railing_block_type_in_sync(
    block_type: Any,
    *,
    include_metadata: bool = True,
) -> bool:
    """
    Return whether a BlockType-like object matches the canonical definition.
    """
    try:
        return not compare_railing_block_type(
            block_type,
            include_metadata=include_metadata,
        )
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Palette serialization
# -----------------------------------------------------------------------------

def build_railing_palette_entry(
    *,
    palette_index: int,
    registry_id: Optional[str] = None,
    registry_version: Optional[str] = None,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """
    Build a positive chunk-palette entry for Railing.

    No cell value is stored globally. It is derived from the supplied concrete
    chunk-local ``palette_index``.
    """
    definition = require_railing_definition_ready()

    try:
        entry = definition.to_palette_entry(
            palette_index=palette_index,
            registry_id=registry_id,
            registry_version=registry_version,
            include_metadata=include_metadata,
        )
    except SystemBlockPaletteError:
        raise
    except Exception as exc:
        raise RailingSerializationError(
            "Could not serialize Railing as a positive palette entry."
        ) from exc

    entry["targetable"] = RAILING_TARGETABLE
    entry["replaceable"] = RAILING_REPLACEABLE
    entry["currentGeometry"] = RAILING_CURRENT_GEOMETRY
    entry["currentCollision"] = RAILING_CURRENT_COLLISION

    return make_json_safe(entry)


# -----------------------------------------------------------------------------
# System-catalog serialization
# -----------------------------------------------------------------------------

def serialize_railing_definition(
    *,
    include_metadata: bool = True,
    include_fingerprint: bool = True,
) -> dict[str, Any]:
    """
    Serialize the complete canonical Railing definition.
    """
    definition = require_railing_definition_ready()

    try:
        result = definition.to_api_dict(
            include_metadata=include_metadata,
            include_fingerprint=include_fingerprint,
        )
    except Exception as exc:
        raise RailingSerializationError(
            "Could not serialize the canonical Railing definition."
        ) from exc

    result["schemaVersion"] = (
        RAILING_SYSTEM_CATALOG_SCHEMA_VERSION
    )

    result["targetable"] = RAILING_TARGETABLE
    result["replaceable"] = RAILING_REPLACEABLE

    result["commandSemantics"] = {
        "placementCommand": RAILING_PLACEMENT_COMMAND,
        "removalCommand": RAILING_REMOVAL_COMMAND,
        "requiresNewCommandType": False,
    }

    result["persistenceSemantics"] = {
        "storedAsBlockType": True,
        "storedInPositivePalette": True,
        "reservedCellValue": None,
        "fixedGlobalCellValue": False,
        "cellValueRule": BLOCK_CELL_VALUE_RULE,
    }

    result["versionOneRuntime"] = {
        "singleCell": True,
        "renderMode": RAILING_RENDER_MODE,
        "shapeType": RAILING_SHAPE_TYPE,
        "geometry": RAILING_CURRENT_GEOMETRY,
        "collision": RAILING_CURRENT_COLLISION,
        "orientationSupported": (
            RAILING_ORIENTATION_SUPPORTED
        ),
        "neighbourConnectionSupported": (
            RAILING_NEIGHBOUR_CONNECTION_SUPPORTED
        ),
        "multiBlockObject": RAILING_MULTI_BLOCK_OBJECT,
    }

    return make_json_safe(result)


def serialize_railing_for_system_catalog() -> dict[str, Any]:
    """
    Serialize Railing for ``GET .../blocks/system``.

    This function intentionally does not assign a palette index or cell value.
    The system catalog describes definitions, whereas the concrete chunk palette
    determines runtime cell values.
    """
    result = serialize_railing_definition(
        include_metadata=True,
        include_fingerprint=True,
    )

    result["paletteSemantics"] = {
        "defaultPaletteIndex": None,
        "concretePaletteIndex": None,
        "concreteCellValue": None,
        "cellValueRule": BLOCK_CELL_VALUE_RULE,
        "assignedPerChunk": True,
    }

    result["inventory"] = {
        "visible": RAILING_INVENTORY_VISIBLE,
        "placeable": RAILING_PLACEABLE,
        "runtimeBlockTypeId": (
            RAILING_RUNTIME_BLOCK_TYPE_ID
        ),
        "source": SYSTEM_BLOCK_SOURCE,
        "kind": RAILING_KIND,
    }

    return make_json_safe(result)


# -----------------------------------------------------------------------------
# Readiness and diagnostics
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_railing_definition_status() -> Mapping[str, Any]:
    """
    Return cached, non-raising Railing-definition diagnostics.
    """
    try:
        definition = get_railing_definition()
        errors = collect_railing_invariant_errors(
            definition
        )

        persistent_values: Optional[dict[str, Any]]

        if errors:
            persistent_values = None
        else:
            try:
                persistent_values = (
                    definition.to_persistent_block_values(
                        include_metadata=False,
                    )
                )
            except Exception:
                persistent_values = None

        status = {
            "schemaVersion": RAILING_STATUS_SCHEMA_VERSION,
            "ready": not errors,
            "systemBlockId": RAILING_SYSTEM_BLOCK_ID,
            "runtimeBlockTypeId": (
                RAILING_RUNTIME_BLOCK_TYPE_ID
            ),
            "kind": RAILING_KIND,
            "definitionVersion": (
                RAILING_DEFINITION_VERSION
            ),
            "definitionFingerprint": (
                definition.definition_fingerprint
                if not errors
                else None
            ),
            "persistAsBlockType": (
                RAILING_PERSIST_AS_BLOCK_TYPE
            ),
            "inventoryVisible": (
                RAILING_INVENTORY_VISIBLE
            ),
            "placeable": RAILING_PLACEABLE,
            "breakable": RAILING_BREAKABLE,
            "solid": RAILING_SOLID,
            "collidable": RAILING_COLLIDABLE,
            "renderMode": RAILING_RENDER_MODE,
            "shapeType": RAILING_SHAPE_TYPE,
            "defaultPaletteIndex": None,
            "fixedGlobalCellValue": False,
            "blockCellValueRule": (
                BLOCK_CELL_VALUE_RULE
            ),
            "expectedPersistentValues": (
                persistent_values
            ),
            "errors": list(errors),
            "errorType": None,
            "error": None,
            "moduleVersion": (
                RAILING_DEFINITION_MODULE_VERSION
            ),
        }

    except Exception as exc:
        status = {
            "schemaVersion": RAILING_STATUS_SCHEMA_VERSION,
            "ready": False,
            "systemBlockId": RAILING_SYSTEM_BLOCK_ID,
            "runtimeBlockTypeId": (
                RAILING_RUNTIME_BLOCK_TYPE_ID
            ),
            "kind": RAILING_KIND,
            "definitionVersion": (
                RAILING_DEFINITION_VERSION
            ),
            "definitionFingerprint": None,
            "persistAsBlockType": (
                RAILING_PERSIST_AS_BLOCK_TYPE
            ),
            "inventoryVisible": (
                RAILING_INVENTORY_VISIBLE
            ),
            "placeable": RAILING_PLACEABLE,
            "breakable": RAILING_BREAKABLE,
            "solid": RAILING_SOLID,
            "collidable": RAILING_COLLIDABLE,
            "renderMode": RAILING_RENDER_MODE,
            "shapeType": RAILING_SHAPE_TYPE,
            "defaultPaletteIndex": None,
            "fixedGlobalCellValue": False,
            "blockCellValueRule": (
                BLOCK_CELL_VALUE_RULE
            ),
            "expectedPersistentValues": None,
            "errors": [
                "Could not construct or validate the Railing definition."
            ],
            "errorType": type(exc).__name__,
            "error": _safe_exception_text(exc),
            "moduleVersion": (
                RAILING_DEFINITION_MODULE_VERSION
            ),
        }

    frozen = _deep_freeze(
        status,
        path="railingDefinitionStatus",
    )

    if not isinstance(frozen, Mapping):
        return MappingProxyType(
            {
                "schemaVersion": RAILING_STATUS_SCHEMA_VERSION,
                "ready": False,
                "errors": [
                    "Railing status did not resolve to a mapping."
                ],
            }
        )

    return frozen


def get_railing_definition_debug_summary() -> dict[str, Any]:
    """
    Return a JSON-safe diagnostic summary.
    """
    try:
        return make_json_safe(
            get_railing_definition_status()
        )
    except Exception as exc:
        return {
            "schemaVersion": RAILING_STATUS_SCHEMA_VERSION,
            "ready": False,
            "systemBlockId": RAILING_SYSTEM_BLOCK_ID,
            "runtimeBlockTypeId": (
                RAILING_RUNTIME_BLOCK_TYPE_ID
            ),
            "errors": [
                "Could not build Railing definition diagnostics."
            ],
            "errorType": type(exc).__name__,
            "error": _safe_exception_text(exc),
        }


# -----------------------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------------------

def clear_railing_definition_caches() -> None:
    """
    Clear local Railing-definition caches.

    Intended for unit tests, development reload tooling and explicit diagnostic
    refreshes. Production request paths should not normally call it.
    """
    get_railing_metadata.cache_clear()
    get_railing_definition.cache_clear()
    get_railing_definition_status.cache_clear()

    _is_railing_system_block_id_cached.cache_clear()
    _is_railing_runtime_block_type_id_cached.cache_clear()


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "RAILING_BREAKABLE",
    "RAILING_COLLIDABLE",
    "RAILING_CURRENT_COLLISION",
    "RAILING_CURRENT_GEOMETRY",
    "RAILING_DEFAULT_PALETTE_INDEX",
    "RAILING_DEFINITION_MODULE_VERSION",
    "RAILING_DEFINITION_VERSION",
    "RAILING_DESCRIPTION",
    "RAILING_EMITS_LIGHT",
    "RAILING_FUTURE_GEOMETRY",
    "RAILING_HARDNESS",
    "RAILING_ICON_ID",
    "RAILING_IMMUTABLE_DEFINITION",
    "RAILING_INVENTORY_VISIBLE",
    "RAILING_KIND",
    "RAILING_LABEL",
    "RAILING_LIGHT_LEVEL",
    "RAILING_MATERIAL_ID",
    "RAILING_MULTI_BLOCK_OBJECT",
    "RAILING_NEIGHBOUR_CONNECTION_SUPPORTED",
    "RAILING_OPAQUE",
    "RAILING_ORIENTATION_SUPPORTED",
    "RAILING_PERSIST_AS_BLOCK_TYPE",
    "RAILING_PLACEABLE",
    "RAILING_PLACEMENT_COMMAND",
    "RAILING_REMOVAL_COMMAND",
    "RAILING_RENDER_MODE",
    "RAILING_REPLACEABLE",
    "RAILING_RESERVED_CELL_VALUE",
    "RAILING_RUNTIME_BLOCK_TYPE_ID",
    "RAILING_SELECTABLE",
    "RAILING_SHAPE_TYPE",
    "RAILING_SOLID",
    "RAILING_STACK_SIZE",
    "RAILING_STATUS_SCHEMA_VERSION",
    "RAILING_SYSTEM_BLOCK_ID",
    "RAILING_SYSTEM_CATALOG_SCHEMA_VERSION",
    "RAILING_TARGETABLE",
    "RAILING_TEXTURE_ID",
    "RailingDefinitionError",
    "RailingInvariantError",
    "RailingSerializationError",
    "build_railing_palette_entry",
    "build_railing_persistent_values",
    "clear_railing_definition_caches",
    "collect_railing_invariant_errors",
    "compare_railing_block_type",
    "get_railing_definition",
    "get_railing_definition_debug_summary",
    "get_railing_definition_status",
    "get_railing_metadata",
    "is_railing_block_type_in_sync",
    "is_railing_identifier",
    "is_railing_runtime_block_type_id",
    "is_railing_system_block_id",
    "require_railing_definition",
    "require_railing_definition_ready",
    "serialize_railing_definition",
    "serialize_railing_for_system_catalog",
    "validate_railing_definition",
]