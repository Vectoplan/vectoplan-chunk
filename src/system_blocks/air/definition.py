# services/vectoplan-chunk/src/system_blocks/air/definition.py
"""
Canonical built-in Air definition for ``vectoplan-chunk``.

Air is a reserved runtime cell state and is intentionally not a normal block.

Persistence and runtime rules:

- ``cellValue = 0`` always means Air.
- Air is not stored as a ``BlockType`` row.
- Air is not added to a positive chunk palette.
- Air has no ``runtimeBlockTypeId``.
- Air cannot be placed through ``SetBlock``.
- ``RemoveBlock`` changes a cell to Air.
- Air is invisible, non-solid and non-collidable.
- Air is not selectable and does not appear in the editor inventory.
- Air may be replaced by a placeable positive block.

This module contains no Flask, SQLAlchemy, database, route or bootstrap logic.
It exposes one cached immutable ``SystemBlockDefinition`` plus defensive helper
functions used by the catalog, status endpoints, bootstrap invariant checks and
tests.

Importing this module does not create database rows and does not execute any
runtime mutation.
"""

from __future__ import annotations

from functools import lru_cache
from types import MappingProxyType
from typing import Any, Final, Mapping, Optional


# -----------------------------------------------------------------------------
# Contract import
#
# Relative import is the canonical package path. The fallback supports selected
# development and test environments that import ``src`` as the top-level
# package.
# -----------------------------------------------------------------------------

try:
    from ..contracts import (
        AIR_CELL_VALUE,
        BLOCK_CELL_VALUE_RULE,
        CELL_ENCODING_VERSION,
        RENDER_MODE_INVISIBLE,
        SHAPE_TYPE_EMPTY,
        SYSTEM_BLOCK_CATEGORY,
        SYSTEM_BLOCK_SOURCE,
        SystemBlockContractError,
        SystemBlockDefinition,
        make_json_safe,
        require_system_block_definition,
    )
except ImportError:
    try:
        from src.system_blocks.contracts import (
            AIR_CELL_VALUE,
            BLOCK_CELL_VALUE_RULE,
            CELL_ENCODING_VERSION,
            RENDER_MODE_INVISIBLE,
            SHAPE_TYPE_EMPTY,
            SYSTEM_BLOCK_CATEGORY,
            SYSTEM_BLOCK_SOURCE,
            SystemBlockContractError,
            SystemBlockDefinition,
            make_json_safe,
            require_system_block_definition,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Could not import the system-block contract while loading the "
            "built-in Air definition. Ensure "
            "'services/vectoplan-chunk/src/system_blocks/contracts.py' exists "
            "and the vectoplan-chunk source root is available on PYTHONPATH."
        ) from exc


# -----------------------------------------------------------------------------
# Public Air constants
# -----------------------------------------------------------------------------

AIR_SYSTEM_BLOCK_ID: Final[str] = "system_air"
AIR_KIND: Final[str] = "air"
AIR_LABEL: Final[str] = "Air"
AIR_DEFINITION_VERSION: Final[str] = "1"

AIR_DESCRIPTION: Final[str] = (
    "Reserved empty VECTOPLAN world-cell state. Air is represented by "
    "cellValue 0, is never stored as a BlockType and is created through "
    "RemoveBlock rather than SetBlock."
)

AIR_RUNTIME_BLOCK_TYPE_ID: Final[None] = None
AIR_RESERVED_CELL_VALUE: Final[int] = AIR_CELL_VALUE

AIR_PERSIST_AS_BLOCK_TYPE: Final[bool] = False
AIR_INVENTORY_VISIBLE: Final[bool] = False
AIR_IMMUTABLE_DEFINITION: Final[bool] = True

AIR_SOLID: Final[bool] = False
AIR_OPAQUE: Final[bool] = False
AIR_PLACEABLE: Final[bool] = False
AIR_BREAKABLE: Final[bool] = False
AIR_SELECTABLE: Final[bool] = False
AIR_COLLIDABLE: Final[bool] = False

AIR_EMITS_LIGHT: Final[bool] = False
AIR_LIGHT_LEVEL: Final[int] = 0
AIR_HARDNESS: Final[float] = 0.0
AIR_STACK_SIZE: Final[int] = 1

AIR_RENDER_MODE: Final[str] = RENDER_MODE_INVISIBLE
AIR_SHAPE_TYPE: Final[str] = SHAPE_TYPE_EMPTY

AIR_TARGETABLE: Final[bool] = False
AIR_REPLACEABLE: Final[bool] = True

AIR_CREATION_COMMAND: Final[str] = "RemoveBlock"
AIR_FORBIDDEN_PLACEMENT_COMMAND: Final[str] = "SetBlock"
AIR_SET_BLOCK_ERROR_CODE: Final[str] = "air_requires_remove_block"

AIR_DEFINITION_MODULE_VERSION: Final[str] = "1.0.0"


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class AirDefinitionError(RuntimeError):
    """
    Raised when the canonical Air definition cannot be constructed or verified.
    """


class AirInvariantError(AirDefinitionError):
    """
    Raised when an Air definition violates the built-in Air invariants.
    """

    def __init__(self, errors: tuple[str, ...] | list[str]) -> None:
        normalized_errors = tuple(
            str(error).strip()
            for error in errors
            if str(error).strip()
        )

        self.errors = normalized_errors

        details = (
            "; ".join(normalized_errors)
            if normalized_errors
            else "unknown Air invariant violation"
        )

        super().__init__(f"Invalid built-in Air definition: {details}")


# -----------------------------------------------------------------------------
# Cached immutable metadata
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_air_metadata() -> Mapping[str, Any]:
    """
    Return immutable descriptive metadata for the Air definition.

    This metadata supplements the core system-block fields. It documents editor,
    command, storage, rendering and physics semantics that are not represented
    as direct ``BlockType`` columns.

    The mapping is cached because it is constant for the process lifetime.
    """
    metadata = {
        "semanticRole": "air",
        "runtimeRole": "empty_cell",
        "storageMode": "reserved_cell_value",
        "persistence": {
            "blockTypeRow": False,
            "paletteEntry": False,
            "cellValue": AIR_RESERVED_CELL_VALUE,
        },
        "cellEncoding": {
            "version": CELL_ENCODING_VERSION,
            "airCellValue": AIR_CELL_VALUE,
            "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
        },
        "commands": {
            "creationCommand": AIR_CREATION_COMMAND,
            "placementCommand": None,
            "forbiddenPlacementCommand": AIR_FORBIDDEN_PLACEMENT_COMMAND,
            "setBlockErrorCode": AIR_SET_BLOCK_ERROR_CODE,
        },
        "editor": {
            "inventoryVisible": AIR_INVENTORY_VISIBLE,
            "selectable": AIR_SELECTABLE,
            "targetable": AIR_TARGETABLE,
            "replaceable": AIR_REPLACEABLE,
        },
        "rendering": {
            "visible": False,
            "createsMesh": False,
            "renderMode": AIR_RENDER_MODE,
            "shapeType": AIR_SHAPE_TYPE,
        },
        "physics": {
            "solid": AIR_SOLID,
            "collidable": AIR_COLLIDABLE,
            "blocksMovement": False,
        },
        "definition": {
            "moduleVersion": AIR_DEFINITION_MODULE_VERSION,
            "immutable": AIR_IMMUTABLE_DEFINITION,
            "builtIn": True,
        },
    }

    return MappingProxyType(metadata)


# -----------------------------------------------------------------------------
# Definition construction
# -----------------------------------------------------------------------------

def _build_air_definition() -> SystemBlockDefinition:
    """
    Construct the canonical immutable Air definition.

    This function is deliberately separate from ``get_air_definition`` so tests
    can distinguish construction from cached retrieval.
    """
    return SystemBlockDefinition(
        system_block_id=AIR_SYSTEM_BLOCK_ID,
        runtime_block_type_id=AIR_RUNTIME_BLOCK_TYPE_ID,
        label=AIR_LABEL,
        description=AIR_DESCRIPTION,
        kind=AIR_KIND,
        definition_version=AIR_DEFINITION_VERSION,
        source=SYSTEM_BLOCK_SOURCE,
        category=SYSTEM_BLOCK_CATEGORY,
        reserved_cell_value=AIR_RESERVED_CELL_VALUE,
        persist_as_block_type=AIR_PERSIST_AS_BLOCK_TYPE,
        immutable_definition=AIR_IMMUTABLE_DEFINITION,
        inventory_visible=AIR_INVENTORY_VISIBLE,
        solid=AIR_SOLID,
        opaque=AIR_OPAQUE,
        placeable=AIR_PLACEABLE,
        breakable=AIR_BREAKABLE,
        selectable=AIR_SELECTABLE,
        collidable=AIR_COLLIDABLE,
        emits_light=AIR_EMITS_LIGHT,
        light_level=AIR_LIGHT_LEVEL,
        hardness=AIR_HARDNESS,
        stack_size=AIR_STACK_SIZE,
        render_mode=AIR_RENDER_MODE,
        shape_type=AIR_SHAPE_TYPE,
        default_palette_index=None,
        material_id=None,
        texture_id=None,
        icon_id=None,
        aliases=(),
        metadata=get_air_metadata(),
    )


@lru_cache(maxsize=1)
def get_air_definition() -> SystemBlockDefinition:
    """
    Return the canonical cached Air definition.

    The same immutable object is returned throughout the process lifetime. This
    prevents accidental divergence between routes, catalog lookups, bootstrap
    checks and command validation.
    """
    try:
        definition = _build_air_definition()
        require_air_definition(definition)
        return definition
    except AirDefinitionError:
        raise
    except SystemBlockContractError as exc:
        raise AirDefinitionError(
            "The Air definition violates the shared system-block contract."
        ) from exc
    except Exception as exc:
        raise AirDefinitionError(
            "Could not construct the built-in Air definition."
        ) from exc


# -----------------------------------------------------------------------------
# Air-specific validation
# -----------------------------------------------------------------------------

def collect_air_invariant_errors(
    definition: Any,
) -> tuple[str, ...]:
    """
    Return all Air-specific invariant violations.

    The shared contract validates general system-block rules. This function adds
    exact semantic checks that uniquely identify the canonical Air definition.
    """
    errors: list[str] = []

    if not isinstance(definition, SystemBlockDefinition):
        return (
            "definition must be an instance of SystemBlockDefinition.",
        )

    try:
        contract_errors = definition.collect_validation_errors()
    except Exception as exc:
        errors.append(
            "shared contract validation failed: "
            f"{type(exc).__name__}: {exc}"
        )
    else:
        errors.extend(contract_errors)

    if definition.system_block_id != AIR_SYSTEM_BLOCK_ID:
        errors.append(
            f"system_block_id must be '{AIR_SYSTEM_BLOCK_ID}'."
        )

    if definition.runtime_block_type_id is not AIR_RUNTIME_BLOCK_TYPE_ID:
        errors.append(
            "runtime_block_type_id must be null for Air."
        )

    if definition.kind != AIR_KIND:
        errors.append(
            f"kind must be '{AIR_KIND}'."
        )

    if definition.label != AIR_LABEL:
        errors.append(
            f"label must be '{AIR_LABEL}'."
        )

    if definition.definition_version != AIR_DEFINITION_VERSION:
        errors.append(
            "definition_version must be "
            f"'{AIR_DEFINITION_VERSION}'."
        )

    if definition.source != SYSTEM_BLOCK_SOURCE:
        errors.append(
            f"source must be '{SYSTEM_BLOCK_SOURCE}'."
        )

    if definition.category != SYSTEM_BLOCK_CATEGORY:
        errors.append(
            f"category must be '{SYSTEM_BLOCK_CATEGORY}'."
        )

    if definition.reserved_cell_value != AIR_CELL_VALUE:
        errors.append(
            f"reserved_cell_value must be {AIR_CELL_VALUE}."
        )

    if definition.persist_as_block_type:
        errors.append(
            "Air must not be persisted as BlockType."
        )

    if not definition.immutable_definition:
        errors.append(
            "Air must have immutable_definition=true."
        )

    if definition.inventory_visible:
        errors.append(
            "Air must not be visible in inventory."
        )

    if definition.solid:
        errors.append(
            "Air must not be solid."
        )

    if definition.opaque:
        errors.append(
            "Air must not be opaque."
        )

    if definition.placeable:
        errors.append(
            "Air must not be placeable."
        )

    if definition.breakable:
        errors.append(
            "Air must not be breakable."
        )

    if definition.selectable:
        errors.append(
            "Air must not be selectable."
        )

    if definition.collidable:
        errors.append(
            "Air must not be collidable."
        )

    if definition.emits_light:
        errors.append(
            "Air must not emit light."
        )

    if definition.light_level != AIR_LIGHT_LEVEL:
        errors.append(
            f"Air light_level must be {AIR_LIGHT_LEVEL}."
        )

    if definition.hardness != AIR_HARDNESS:
        errors.append(
            f"Air hardness must be {AIR_HARDNESS}."
        )

    if definition.render_mode != RENDER_MODE_INVISIBLE:
        errors.append(
            f"Air render_mode must be '{RENDER_MODE_INVISIBLE}'."
        )

    if definition.shape_type != SHAPE_TYPE_EMPTY:
        errors.append(
            f"Air shape_type must be '{SHAPE_TYPE_EMPTY}'."
        )

    if definition.default_palette_index is not None:
        errors.append(
            "Air must not define default_palette_index."
        )

    if definition.material_id is not None:
        errors.append(
            "Air must not define material_id."
        )

    if definition.texture_id is not None:
        errors.append(
            "Air must not define texture_id."
        )

    if definition.icon_id is not None:
        errors.append(
            "Air must not define icon_id."
        )

    try:
        if not definition.is_reserved_cell_state:
            errors.append(
                "Air must be recognized as a reserved cell state."
            )
    except Exception as exc:
        errors.append(
            "could not evaluate reserved-cell-state flag: "
            f"{type(exc).__name__}: {exc}"
        )

    try:
        if not definition.is_air_state:
            errors.append(
                "Air must be recognized as the canonical Air state."
            )
    except Exception as exc:
        errors.append(
            "could not evaluate Air-state flag: "
            f"{type(exc).__name__}: {exc}"
        )

    try:
        if definition.is_persisted_runtime_block:
            errors.append(
                "Air must not be recognized as a persisted runtime block."
            )
    except Exception as exc:
        errors.append(
            "could not evaluate persisted-runtime-block flag: "
            f"{type(exc).__name__}: {exc}"
        )

    try:
        if definition.can_appear_in_inventory:
            errors.append(
                "Air must not be eligible for editor inventory."
            )
    except Exception as exc:
        errors.append(
            "could not evaluate inventory eligibility: "
            f"{type(exc).__name__}: {exc}"
        )

    normalized_errors: list[str] = []
    seen: set[str] = set()

    for error in errors:
        text = str(error).strip()

        if not text or text in seen:
            continue

        seen.add(text)
        normalized_errors.append(text)

    return tuple(normalized_errors)


def validate_air_definition(
    definition: Optional[SystemBlockDefinition] = None,
) -> bool:
    """
    Return whether a definition satisfies every canonical Air invariant.

    When no definition is supplied, the cached built-in definition is checked.
    """
    resolved = definition

    if resolved is None:
        try:
            resolved = get_air_definition()
        except Exception:
            return False

    return not collect_air_invariant_errors(resolved)


def require_air_definition(
    definition: Any,
) -> SystemBlockDefinition:
    """
    Return a valid canonical Air definition or raise ``AirInvariantError``.
    """
    try:
        resolved = require_system_block_definition(definition)
    except Exception as exc:
        raise AirInvariantError(
            (
                "definition does not satisfy the shared system-block contract: "
                f"{type(exc).__name__}: {exc}",
            )
        ) from exc

    errors = collect_air_invariant_errors(resolved)

    if errors:
        raise AirInvariantError(errors)

    return resolved


# -----------------------------------------------------------------------------
# Identification helpers
# -----------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _is_air_system_block_id_cached(value: str) -> bool:
    """
    Cached implementation for Air system-block ID comparison.
    """
    return value.strip().lower() == AIR_SYSTEM_BLOCK_ID.lower()


def is_air_system_block_id(value: Any) -> bool:
    """
    Return whether a value identifies the built-in Air definition.

    This function is deliberately strict. Generic values such as ``"air"`` are
    not accepted as the system ID.
    """
    if value is None:
        return False

    try:
        text = str(value)
    except Exception:
        return False

    if not text.strip():
        return False

    return _is_air_system_block_id_cached(text)


def is_air_runtime_block_type_id(value: Any) -> bool:
    """
    Return whether a runtime block-type value represents Air.

    Air intentionally has no runtime BlockType ID. Therefore only ``None`` or an
    empty textual value is considered the Air runtime identity. The string
    ``system_air`` is a system-definition ID, not a positive runtime block ID.
    """
    if value is None:
        return True

    try:
        return not str(value).strip()
    except Exception:
        return False


@lru_cache(maxsize=128)
def _is_air_cell_value_cached(value: int) -> bool:
    """
    Cached integer comparison for the invariant Air cell value.
    """
    return value == AIR_CELL_VALUE


def is_air_cell_value(value: Any) -> bool:
    """
    Return whether a value resolves exactly to the invariant Air cell value.
    """
    if value is None or isinstance(value, bool):
        return False

    try:
        normalized = int(value)
    except Exception:
        return False

    return _is_air_cell_value_cached(normalized)


def is_forbidden_air_set_block_id(value: Any) -> bool:
    """
    Return whether ``SetBlock`` should reject the supplied block ID as Air.
    """
    return is_air_system_block_id(value)


# -----------------------------------------------------------------------------
# Serialization
# -----------------------------------------------------------------------------

def serialize_air_definition(
    *,
    include_metadata: bool = True,
    include_fingerprint: bool = True,
) -> dict[str, Any]:
    """
    Serialize the canonical Air definition for a system-block API response.
    """
    definition = get_air_definition()

    result = definition.to_api_dict(
        include_metadata=include_metadata,
        include_fingerprint=include_fingerprint,
    )

    result["targetable"] = AIR_TARGETABLE
    result["replaceable"] = AIR_REPLACEABLE

    result["commandSemantics"] = {
        "creationCommand": AIR_CREATION_COMMAND,
        "placementCommand": None,
        "forbiddenPlacementCommand": AIR_FORBIDDEN_PLACEMENT_COMMAND,
        "setBlockErrorCode": AIR_SET_BLOCK_ERROR_CODE,
    }

    result["persistenceSemantics"] = {
        "storedAsBlockType": False,
        "storedInPositivePalette": False,
        "storedAsCellValue": AIR_CELL_VALUE,
    }

    return make_json_safe(result)


def serialize_air_for_world_blocks_route() -> dict[str, Any]:
    """
    Serialize Air for the existing project/world block-palette response.

    Compatibility rule:

    ``blockTypeId`` remains ``None``.

    The existing world block response treats Air as a separate zero-value entry.
    Returning ``system_air`` as ``blockTypeId`` here could make older consumers
    interpret Air as a positive placeable palette block.
    """
    definition = get_air_definition()

    return {
        "cellValue": AIR_CELL_VALUE,
        "blockTypeId": None,
        "systemBlockId": definition.system_block_id,
        "label": definition.label,
        "category": definition.category,
        "kind": definition.kind,
        "source": definition.source,
        "definitionVersion": definition.definition_version,
        "reserved": True,
        "reservedCellState": True,
        "solid": definition.solid,
        "opaque": definition.opaque,
        "placeable": definition.placeable,
        "breakable": definition.breakable,
        "selectable": definition.selectable,
        "targetable": AIR_TARGETABLE,
        "replaceable": AIR_REPLACEABLE,
        "collidable": definition.collidable,
        "inventoryVisible": definition.inventory_visible,
        "renderMode": definition.render_mode,
        "shapeType": definition.shape_type,
        "materialId": None,
        "textureId": None,
        "iconId": None,
        "persistAsBlockType": False,
        "cellEncoding": {
            "version": CELL_ENCODING_VERSION,
            "airCellValue": AIR_CELL_VALUE,
            "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
        },
        "commandSemantics": {
            "creationCommand": AIR_CREATION_COMMAND,
            "placementCommand": None,
            "setBlockErrorCode": AIR_SET_BLOCK_ERROR_CODE,
        },
    }


# -----------------------------------------------------------------------------
# Readiness and diagnostics
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_air_definition_status() -> Mapping[str, Any]:
    """
    Return immutable, non-raising Air-definition diagnostics.

    Status endpoints and bootstrap diagnostics can call this function without
    risking a route failure when the definition is invalid.
    """
    try:
        definition = get_air_definition()
        errors = collect_air_invariant_errors(definition)

        status = {
            "ready": not errors,
            "systemBlockId": AIR_SYSTEM_BLOCK_ID,
            "runtimeBlockTypeId": AIR_RUNTIME_BLOCK_TYPE_ID,
            "kind": AIR_KIND,
            "definitionVersion": AIR_DEFINITION_VERSION,
            "definitionFingerprint": (
                definition.definition_fingerprint
                if not errors
                else None
            ),
            "reservedCellValue": AIR_CELL_VALUE,
            "persistAsBlockType": AIR_PERSIST_AS_BLOCK_TYPE,
            "inventoryVisible": AIR_INVENTORY_VISIBLE,
            "renderMode": AIR_RENDER_MODE,
            "shapeType": AIR_SHAPE_TYPE,
            "errors": list(errors),
            "errorType": None,
            "error": None,
            "moduleVersion": AIR_DEFINITION_MODULE_VERSION,
        }

    except Exception as exc:
        status = {
            "ready": False,
            "systemBlockId": AIR_SYSTEM_BLOCK_ID,
            "runtimeBlockTypeId": AIR_RUNTIME_BLOCK_TYPE_ID,
            "kind": AIR_KIND,
            "definitionVersion": AIR_DEFINITION_VERSION,
            "definitionFingerprint": None,
            "reservedCellValue": AIR_CELL_VALUE,
            "persistAsBlockType": AIR_PERSIST_AS_BLOCK_TYPE,
            "inventoryVisible": AIR_INVENTORY_VISIBLE,
            "renderMode": AIR_RENDER_MODE,
            "shapeType": AIR_SHAPE_TYPE,
            "errors": [
                "Could not construct or validate the Air definition."
            ],
            "errorType": type(exc).__name__,
            "error": str(exc),
            "moduleVersion": AIR_DEFINITION_MODULE_VERSION,
        }

    return MappingProxyType(status)


def require_air_definition_ready() -> SystemBlockDefinition:
    """
    Return the canonical Air definition or raise when readiness checks fail.
    """
    definition = get_air_definition()
    errors = collect_air_invariant_errors(definition)

    if errors:
        raise AirInvariantError(errors)

    return definition


# -----------------------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------------------

def clear_air_definition_caches() -> None:
    """
    Clear local Air-definition caches.

    This is intended for tests and development reload tooling. Production
    request paths should not normally call it.
    """
    get_air_metadata.cache_clear()
    get_air_definition.cache_clear()
    get_air_definition_status.cache_clear()
    _is_air_system_block_id_cached.cache_clear()
    _is_air_cell_value_cached.cache_clear()


__all__ = [
    "AIR_BREAKABLE",
    "AIR_COLLIDABLE",
    "AIR_CREATION_COMMAND",
    "AIR_DEFINITION_MODULE_VERSION",
    "AIR_DEFINITION_VERSION",
    "AIR_DESCRIPTION",
    "AIR_EMITS_LIGHT",
    "AIR_FORBIDDEN_PLACEMENT_COMMAND",
    "AIR_HARDNESS",
    "AIR_IMMUTABLE_DEFINITION",
    "AIR_INVENTORY_VISIBLE",
    "AIR_KIND",
    "AIR_LABEL",
    "AIR_LIGHT_LEVEL",
    "AIR_OPAQUE",
    "AIR_PERSIST_AS_BLOCK_TYPE",
    "AIR_PLACEABLE",
    "AIR_RENDER_MODE",
    "AIR_REPLACEABLE",
    "AIR_RESERVED_CELL_VALUE",
    "AIR_RUNTIME_BLOCK_TYPE_ID",
    "AIR_SELECTABLE",
    "AIR_SET_BLOCK_ERROR_CODE",
    "AIR_SHAPE_TYPE",
    "AIR_SOLID",
    "AIR_STACK_SIZE",
    "AIR_SYSTEM_BLOCK_ID",
    "AIR_TARGETABLE",
    "AirDefinitionError",
    "AirInvariantError",
    "clear_air_definition_caches",
    "collect_air_invariant_errors",
    "get_air_definition",
    "get_air_definition_status",
    "get_air_metadata",
    "is_air_cell_value",
    "is_air_runtime_block_type_id",
    "is_air_system_block_id",
    "is_forbidden_air_set_block_id",
    "require_air_definition",
    "require_air_definition_ready",
    "serialize_air_definition",
    "serialize_air_for_world_blocks_route",
    "validate_air_definition",
]