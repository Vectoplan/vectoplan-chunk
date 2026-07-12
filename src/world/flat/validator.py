# src/world/flat/validator.py
"""
VECTOPLAN Flat World Validator.

Diese Datei validiert ausschließlich die konkrete Standard-Flat-World unter:

    src/world/flat/world.json

Die Standard-Flat-World basiert ab Version 2 auf genau zwei unveränderlichen
Systemzuständen:

    system_air
        - reservierter leerer Zellzustand
        - cellValue = 0
        - keine BlockType-Zeile
        - kein positiver Paletteintrag

    system_terrain
        - einziger positiver Blocktyp der Standard-Flat-World
        - Oberfläche und Untergrund
        - persistenter, unveränderlicher Systemblock
        - Zellwert wird palettenlokal über paletteIndex + 1 bestimmt

Verantwortung dieses Moduls:

- rohe Flat-World-Konfiguration defensiv normalisieren
- stabile Defaults ergänzen
- Flat-Identität und Generatorversion prüfen
- Air-/Terrain-Systemblockvertrag prüfen
- positive Palette auf system_terrain begrenzen
- Surface- und Subsurface-Layer auf system_terrain festlegen
- Air dauerhaft auf cellValue 0 festlegen
- Cell-Encoding und Cell-Indexing absichern
- Runtime-/Cache-Metadaten prüfen
- eine framework- und datenbankunabhängige WorldDefinition erzeugen
- wiederholte identische Validierungen sicher cachen

Nicht Teil dieses Moduls:

- keine Dateien lesen
- keine Chunks generieren
- keine Flask-Routen
- keine SQLAlchemy-Modelle
- keine Datenbankabfragen
- keine äußeren Seiteneffekte

Die öffentliche API der bisherigen Validator-Version bleibt erhalten.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final

try:
    from src.world.errors import (
        InvalidWorldDefinitionError,
        UnsupportedWorldTypeError,
        WorldValidationError,
        make_json_safe,
    )
    from src.world.models import (
        DEFAULT_AIR_CELL_VALUE,
        DEFAULT_BLOCK_REGISTRY_ID,
        DEFAULT_BLOCK_REGISTRY_VERSION,
        DEFAULT_CELL_INDEX_ORDER,
        DEFAULT_COORDINATE_SYSTEM,
        DEFAULT_PROJECTION_TYPE,
        DEFAULT_TOPOLOGY_TYPE,
        MAX_REASONABLE_CHUNK_SIZE,
        MIN_CELL_SIZE,
        MIN_CHUNK_SIZE,
        WorldDefinition,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.flat.validator requires src.world.errors and "
        "src.world.models to be importable before the validator can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Version and identity constants
# ---------------------------------------------------------------------------

FLAT_VALIDATOR_VERSION: Final[str] = "0.2.0"
FLAT_VALIDATION_CONTRACT_VERSION: Final[str] = "flat-validation-contract.v2"

EXPECTED_WORLD_SCHEMA_VERSION: Final[str] = "world.schema.v1"
EXPECTED_WORLD_ID: Final[str] = "flat"
EXPECTED_WORLD_TYPE: Final[str] = "flat"
EXPECTED_WORLD_LABEL: Final[str] = "Flat System Terrain World"

EXPECTED_GENERATOR_TYPE: Final[str] = "flat-world"
EXPECTED_GENERATOR_VERSION: Final[str] = "2"

EXPECTED_PROJECTION_TYPE: Final[str] = "flat-local-v1"
EXPECTED_TOPOLOGY_TYPE: Final[str] = "flat-unbounded-v1"
EXPECTED_COORDINATE_SYSTEM: Final[str] = "vectoplan-world-y-up-v1"

EXPECTED_CELL_ENCODING_VERSION: Final[str] = (
    "cell-encoding.palette-index-plus-one.v1"
)
EXPECTED_CELL_INDEXING_VERSION: Final[str] = (
    "cell-indexing.x-fastest-y-then-z.v1"
)
EXPECTED_CELL_INDEXING_ORDER: Final[str] = DEFAULT_CELL_INDEX_ORDER

EXPECTED_LAYERS_VERSION: Final[str] = "flat-layers.v2"
EXPECTED_REQUIRED_SYSTEM_BLOCKS_VERSION: Final[str] = (
    "flat-required-system-blocks.v1"
)

SYSTEM_AIR_BLOCK_ID: Final[str] = "system_air"
SYSTEM_TERRAIN_BLOCK_TYPE_ID: Final[str] = "system_terrain"

DEFAULT_SURFACE_BLOCK_TYPE_ID: Final[str] = SYSTEM_TERRAIN_BLOCK_TYPE_ID
DEFAULT_SUBSURFACE_BLOCK_TYPE_ID: Final[str] = SYSTEM_TERRAIN_BLOCK_TYPE_ID
DEFAULT_TERRAIN_BLOCK_TYPE_ID: Final[str] = SYSTEM_TERRAIN_BLOCK_TYPE_ID

EXPECTED_POSITIVE_PALETTE_SIZE: Final[int] = 1
EXPECTED_TERRAIN_PALETTE_INDEX: Final[int] = 0
EXPECTED_TERRAIN_CELL_VALUE: Final[int] = 1

VALIDATION_CACHE_SIZE: Final[int] = 64
CANONICAL_JSON_SEPARATORS: Final[tuple[str, str]] = (",", ":")


REQUIRED_TOP_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "worldId",
    "worldType",
    "generatorType",
    "generatorVersion",
    "chunkSize",
    "cellSize",
    "surfaceY",
    "minY",
    "maxY",
    "blockRegistryId",
    "blockRegistryVersion",
    "requiredSystemBlocks",
    "cellEncoding",
    "cellIndexing",
    "palette",
    "layers",
)

REQUIRED_LAYER_FIELDS: Final[tuple[str, ...]] = (
    "airBlockValue",
    "airSystemBlockId",
    "surfaceBlockTypeId",
    "subsurfaceBlockTypeId",
    "terrainBlockTypeId",
)

OPTIONAL_BOOLEAN_RUNTIME_FIELDS: Final[dict[str, bool]] = {
    "materializeUnchangedChunks": False,
    "supportsSnapshots": False,
    "supportsEvents": False,
    "supportsCommands": False,
    "supportsBatchChunks": True,
    "supportsNegativeChunkCoordinates": True,
    "providerScopeOnly": True,
}

EXPECTED_TERRAIN_PALETTE_BOOLEAN_FIELDS: Final[dict[str, bool]] = {
    "solid": True,
    "placeable": True,
    "breakable": True,
}

EXPECTED_TERRAIN_METADATA_FIELDS: Final[dict[str, Any]] = {
    "source": "system",
    "category": "system",
    "systemBlockId": SYSTEM_TERRAIN_BLOCK_TYPE_ID,
    "runtimeBlockTypeId": SYSTEM_TERRAIN_BLOCK_TYPE_ID,
    "immutableDefinition": True,
    "generationRole": "terrain",
    "usedForSurface": True,
    "usedForSubsurface": True,
}


# ---------------------------------------------------------------------------
# Immutable contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlatSystemBlockContract:
    """
    Kanonischer, unveränderlicher Vertrag der beiden Flat-World-Systemzustände.
    """

    air_system_block_id: str
    air_reserved_cell_value: int
    air_runtime_block_type_id: str | None
    air_persist_as_block_type: bool
    air_stored_in_positive_palette: bool

    terrain_system_block_id: str
    terrain_runtime_block_type_id: str
    terrain_persist_as_block_type: bool
    terrain_stored_in_positive_palette: bool

    positive_palette_size: int
    terrain_palette_index: int
    terrain_cell_value: int

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert den Vertrag in eine JSON-nahe Struktur.
        """
        return {
            "air": {
                "systemBlockId": self.air_system_block_id,
                "runtimeBlockTypeId": self.air_runtime_block_type_id,
                "reservedCellValue": self.air_reserved_cell_value,
                "persistAsBlockType": self.air_persist_as_block_type,
                "storedInPositivePalette": self.air_stored_in_positive_palette,
            },
            "terrain": {
                "systemBlockId": self.terrain_system_block_id,
                "runtimeBlockTypeId": self.terrain_runtime_block_type_id,
                "persistAsBlockType": self.terrain_persist_as_block_type,
                "storedInPositivePalette": self.terrain_stored_in_positive_palette,
                "paletteIndex": self.terrain_palette_index,
                "cellValue": self.terrain_cell_value,
            },
            "positivePaletteSize": self.positive_palette_size,
        }


@dataclass(frozen=True, slots=True)
class FlatLayerRuleContract:
    """
    Erwartete deklarative Layer-Regel aus world.json.

    Die eigentliche Höhenentscheidung bleibt im Generator. Diese Struktur prüft,
    dass die beschreibenden Regeln nicht von der Generatorsemantik abweichen.
    """

    condition: str
    role: str
    system_block_id: str
    block_type_id: str | None
    cell_value: int | None

    @property
    def normalized_condition(self) -> str:
        """
        Bedingung ohne Leerzeichen für robuste Vergleiche.
        """
        return _normalize_condition(self.condition)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "role": self.role,
            "systemBlockId": self.system_block_id,
            "blockTypeId": self.block_type_id,
            "cellValue": self.cell_value,
        }


@lru_cache(maxsize=1)
def get_flat_system_block_contract() -> FlatSystemBlockContract:
    """
    Gibt den pro Prozess gecachten unveränderlichen Systemblockvertrag zurück.
    """
    return FlatSystemBlockContract(
        air_system_block_id=SYSTEM_AIR_BLOCK_ID,
        air_reserved_cell_value=DEFAULT_AIR_CELL_VALUE,
        air_runtime_block_type_id=None,
        air_persist_as_block_type=False,
        air_stored_in_positive_palette=False,
        terrain_system_block_id=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
        terrain_runtime_block_type_id=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
        terrain_persist_as_block_type=True,
        terrain_stored_in_positive_palette=True,
        positive_palette_size=EXPECTED_POSITIVE_PALETTE_SIZE,
        terrain_palette_index=EXPECTED_TERRAIN_PALETTE_INDEX,
        terrain_cell_value=EXPECTED_TERRAIN_CELL_VALUE,
    )


@lru_cache(maxsize=1)
def get_expected_flat_palette_block_type_ids() -> tuple[str, ...]:
    """
    Gibt die kanonische positive Palette der Standard-Flat-World zurück.
    """
    return (SYSTEM_TERRAIN_BLOCK_TYPE_ID,)


@lru_cache(maxsize=1)
def get_expected_flat_layer_rule_contracts() -> tuple[FlatLayerRuleContract, ...]:
    """
    Gibt die kanonischen deklarativen Layer-Regeln zurück.
    """
    return (
        FlatLayerRuleContract(
            condition="worldY > surfaceY",
            role="air",
            system_block_id=SYSTEM_AIR_BLOCK_ID,
            block_type_id=None,
            cell_value=DEFAULT_AIR_CELL_VALUE,
        ),
        FlatLayerRuleContract(
            condition="worldY == surfaceY",
            role="terrain",
            system_block_id=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            block_type_id=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            cell_value=None,
        ),
        FlatLayerRuleContract(
            condition="minY <= worldY < surfaceY",
            role="terrain",
            system_block_id=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            block_type_id=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            cell_value=None,
        ),
        FlatLayerRuleContract(
            condition="worldY < minY",
            role="air",
            system_block_id=SYSTEM_AIR_BLOCK_ID,
            block_type_id=None,
            cell_value=DEFAULT_AIR_CELL_VALUE,
        ),
        FlatLayerRuleContract(
            condition="worldY > maxY",
            role="air",
            system_block_id=SYSTEM_AIR_BLOCK_ID,
            block_type_id=None,
            cell_value=DEFAULT_AIR_CELL_VALUE,
        ),
    )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt einen Wert defensiv in einen bereinigten String um.
    """
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


def _optional_str(value: Any) -> str | None:
    """
    Wandelt einen Wert in einen optionalen bereinigten String um.
    """
    text = _safe_str(value)
    return text or None


def _to_int(
    value: Any,
    *,
    field_name: str,
    default: int | None = None,
) -> int:
    """
    Wandelt einen Wert robust in int um.
    """
    if value is None:
        if default is not None:
            value = default
        else:
            raise InvalidWorldDefinitionError(
                f"Required integer field '{field_name}' is missing.",
                details={"field": field_name},
            )

    try:
        return int(value)
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            f"Field '{field_name}' must be an integer.",
            details={
                "field": field_name,
                "value": make_json_safe(value),
            },
            cause=exc,
        ) from exc


def _to_float(
    value: Any,
    *,
    field_name: str,
    default: float | None = None,
) -> float:
    """
    Wandelt einen Wert robust in float um.
    """
    if value is None:
        if default is not None:
            value = default
        else:
            raise InvalidWorldDefinitionError(
                f"Required numeric field '{field_name}' is missing.",
                details={"field": field_name},
            )

    try:
        return float(value)
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            f"Field '{field_name}' must be numeric.",
            details={
                "field": field_name,
                "value": make_json_safe(value),
            },
            cause=exc,
        ) from exc


def _to_bool(value: Any, *, default: bool = False) -> bool:
    """
    Wandelt typische JSON-/String-/Integer-Werte robust in bool um.
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int | float):
        return bool(value)

    text = _safe_str(value).lower()

    if text in {"1", "true", "yes", "y", "on"}:
        return True

    if text in {"0", "false", "no", "n", "off"}:
        return False

    return default


def _is_bool_like(value: Any) -> bool:
    """
    Prüft, ob ein Wert eindeutig als Boolean interpretierbar ist.
    """
    if isinstance(value, bool):
        return True

    if isinstance(value, int | float):
        return value in {0, 1, 0.0, 1.0}

    return _safe_str(value).lower() in {
        "1",
        "0",
        "true",
        "false",
        "yes",
        "no",
        "y",
        "n",
        "on",
        "off",
    }


def _as_dict(value: Any, *, field_name: str) -> dict[str, Any]:
    """
    Erzwingt ein Dictionary.
    """
    if isinstance(value, Mapping):
        return dict(value)

    raise InvalidWorldDefinitionError(
        f"Field '{field_name}' must be an object.",
        details={
            "field": field_name,
            "value": make_json_safe(value),
        },
    )


def _as_list(value: Any, *, field_name: str) -> list[Any]:
    """
    Erzwingt eine Liste.
    """
    if isinstance(value, list):
        return list(value)

    if isinstance(value, tuple):
        return list(value)

    raise InvalidWorldDefinitionError(
        f"Field '{field_name}' must be an array.",
        details={
            "field": field_name,
            "value": make_json_safe(value),
        },
    )


def _get_field(
    raw: Mapping[str, Any],
    camel_key: str,
    snake_key: str | None = None,
    default: Any = None,
) -> Any:
    """
    Liest ein Feld tolerant aus camelCase oder optional snake_case.
    """
    if camel_key in raw:
        return raw.get(camel_key)

    if snake_key and snake_key in raw:
        return raw.get(snake_key)

    return default


def _append_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    field_name: str | None = None,
    **extra: Any,
) -> None:
    """
    Ergänzt einen strukturierten Validierungsfehler.
    """
    item: dict[str, Any] = {
        "code": code,
        "message": message,
    }

    if field_name:
        item["field"] = field_name

    for key, value in extra.items():
        item[key] = make_json_safe(value)

    errors.append(item)


def _append_warning(
    warnings: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    field_name: str | None = None,
    **extra: Any,
) -> None:
    """
    Ergänzt eine strukturierte Warnung.
    """
    item: dict[str, Any] = {
        "code": code,
        "message": message,
    }

    if field_name:
        item["field"] = field_name

    for key, value in extra.items():
        item[key] = make_json_safe(value)

    warnings.append(item)


def _dedupe_preserve_order(values: Sequence[str]) -> tuple[str, ...]:
    """
    Entfernt Duplikate und erhält die Reihenfolge.
    """
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        text = _safe_str(value)

        if not text or text in seen:
            continue

        seen.add(text)
        result.append(text)

    return tuple(result)


def _normalize_metadata(value: Any) -> dict[str, Any]:
    """
    Normalisiert Metadaten in ein JSON-sicheres Dictionary.
    """
    if value is None:
        return {}

    safe = make_json_safe(value)

    if isinstance(safe, Mapping):
        return dict(safe)

    return {"value": safe}


def _normalize_condition(value: Any) -> str:
    """
    Normalisiert eine deklarative Layer-Bedingung für stabile Vergleiche.
    """
    return "".join(_safe_str(value).split())


def _deep_merge_defaults(
    target: Mapping[str, Any] | None,
    defaults: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Ergänzt verschachtelte Defaults, ohne vorhandene Benutzerwerte zu ersetzen.

    Alle Rückgaben sind neue Dictionaries. Der Eingabewert und die gecachten
    Defaultverträge werden nicht mutiert.
    """
    current = dict(target or {})
    result = deepcopy(current)

    for key, default_value in defaults.items():
        if key not in result:
            result[key] = deepcopy(default_value)
            continue

        if isinstance(default_value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge_defaults(
                result.get(key),
                default_value,
            )

    return result


def _canonical_json(value: Mapping[str, Any]) -> str:
    """
    Baut eine stabile JSON-Repräsentation für Cache-Key und Fingerprint.
    """
    if not isinstance(value, Mapping):
        raise InvalidWorldDefinitionError(
            "Flat world config must be an object.",
            details={
                "configType": type(value).__name__,
                "config": make_json_safe(value),
            },
        )

    safe = make_json_safe(dict(value))

    if not isinstance(safe, Mapping):
        raise InvalidWorldDefinitionError(
            "Flat world config could not be normalized to an object.",
            details={
                "configType": type(value).__name__,
                "config": safe,
            },
        )

    try:
        return json.dumps(
            dict(safe),
            sort_keys=True,
            separators=CANONICAL_JSON_SEPARATORS,
            ensure_ascii=False,
        )
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Flat world config could not be canonicalized.",
            details={
                "configType": type(value).__name__,
                "config": make_json_safe(value),
            },
            cause=exc,
        ) from exc


def _validation_cache_key(canonical_config: str) -> str:
    """
    Erzeugt einen kompakten Diagnose-Fingerprint der Eingabekonfiguration.
    """
    try:
        return hashlib.sha256(canonical_config.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _build_default_required_system_blocks() -> dict[str, Any]:
    """
    Baut eine neue mutable Defaultstruktur für requiredSystemBlocks.
    """
    contract = get_flat_system_block_contract()

    return {
        "version": EXPECTED_REQUIRED_SYSTEM_BLOCKS_VERSION,
        "immutable": True,
        "air": {
            "systemBlockId": contract.air_system_block_id,
            "runtimeBlockTypeId": contract.air_runtime_block_type_id,
            "reservedCellValue": contract.air_reserved_cell_value,
            "persistAsBlockType": contract.air_persist_as_block_type,
            "storedInPositivePalette": contract.air_stored_in_positive_palette,
            "inventoryVisible": False,
            "placeable": False,
            "generationRole": "air",
        },
        "terrain": {
            "systemBlockId": contract.terrain_system_block_id,
            "runtimeBlockTypeId": contract.terrain_runtime_block_type_id,
            "persistAsBlockType": contract.terrain_persist_as_block_type,
            "storedInPositivePalette": contract.terrain_stored_in_positive_palette,
            "inventoryVisible": True,
            "generationRole": "terrain",
        },
    }


def _build_default_terrain_palette_metadata() -> dict[str, Any]:
    """
    Baut die kanonischen Terrain-Metadaten als neue mutable Struktur.
    """
    return {
        **deepcopy(EXPECTED_TERRAIN_METADATA_FIELDS),
        "inventoryVisible": True,
        "cellValueRule": "paletteIndex + 1",
        "editorHint": (
            "Built-in immutable terrain definition used by the default "
            "flat-world generator."
        ),
    }


def _raise_collected_errors(
    *,
    message: str,
    errors: Sequence[Mapping[str, Any]],
    details: Mapping[str, Any] | None = None,
) -> None:
    """
    Wirft genau einen strukturierten Fehler für eine Sammlung von Problemen.
    """
    if not errors:
        return

    payload = dict(details or {})
    payload["errors"] = [make_json_safe(dict(item)) for item in errors]

    raise InvalidWorldDefinitionError(
        message,
        details=payload,
    )


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlatWorldValidationResult:
    """
    Ergebnis der Flat-World-Validierung.

    Das Objekt ist immutable. Verschachtelte Dictionaries werden bei
    Cache-Rückgaben über JSON rekonstruiert, damit kein Aufrufer den Cache
    versehentlich mutieren kann.
    """

    valid: bool
    normalized_config: dict[str, Any]
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert das Validierungsergebnis.
        """
        return {
            "valid": self.valid,
            "normalizedConfig": make_json_safe(self.normalized_config),
            "warnings": [make_json_safe(item) for item in self.warnings],
            "metadata": make_json_safe(self.metadata),
        }


def _result_from_dict(raw: Mapping[str, Any]) -> FlatWorldValidationResult:
    """
    Rekonstruiert ein ValidationResult aus einer gecachten JSON-Struktur.
    """
    normalized = raw.get("normalizedConfig", {})
    warnings = raw.get("warnings", ())
    metadata = raw.get("metadata", {})

    return FlatWorldValidationResult(
        valid=bool(raw.get("valid", False)),
        normalized_config=dict(normalized) if isinstance(normalized, Mapping) else {},
        warnings=tuple(
            dict(item)
            for item in warnings
            if isinstance(item, Mapping)
        ),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


# ---------------------------------------------------------------------------
# Specific validation helpers
# ---------------------------------------------------------------------------

def validate_required_top_level_fields(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft zentrale Pflichtfelder.
    """
    missing: list[str] = []
    empty: list[str] = []

    for field_name in REQUIRED_TOP_LEVEL_FIELDS:
        if field_name not in raw_config:
            missing.append(field_name)
            continue

        value = raw_config.get(field_name)

        if value is None:
            empty.append(field_name)
        elif isinstance(value, str) and not value.strip():
            empty.append(field_name)

    if missing or empty:
        raise InvalidWorldDefinitionError(
            "Flat world config is missing required top-level fields.",
            details={
                "missingFields": missing,
                "emptyFields": empty,
                "requiredFields": REQUIRED_TOP_LEVEL_FIELDS,
            },
        )


def validate_flat_identity(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft Identität, Providerform und Generatorversion der flachen Welt.
    """
    world_id = _safe_str(_get_field(raw_config, "worldId", "world_id"))
    world_type = _safe_str(_get_field(raw_config, "worldType", "world_type"))
    generator_type = _safe_str(
        _get_field(raw_config, "generatorType", "generator_type")
    )
    generator_version = _safe_str(
        _get_field(raw_config, "generatorVersion", "generator_version")
    )
    projection_type = _safe_str(
        _get_field(raw_config, "projectionType", "projection_type"),
        default=EXPECTED_PROJECTION_TYPE,
    )
    topology_type = _safe_str(
        _get_field(raw_config, "topologyType", "topology_type"),
        default=EXPECTED_TOPOLOGY_TYPE,
    )
    coordinate_system = _safe_str(
        _get_field(raw_config, "coordinateSystem", "coordinate_system"),
        default=EXPECTED_COORDINATE_SYSTEM,
    )

    errors: list[dict[str, Any]] = []

    if world_id != EXPECTED_WORLD_ID:
        _append_error(
            errors,
            code="invalid_world_id",
            message=f"Flat world worldId must be '{EXPECTED_WORLD_ID}'.",
            field_name="worldId",
            actual=world_id,
            expected=EXPECTED_WORLD_ID,
        )

    if world_type != EXPECTED_WORLD_TYPE:
        raise UnsupportedWorldTypeError(
            world_type,
            supported_world_types=(EXPECTED_WORLD_TYPE,),
            details={
                "field": "worldType",
                "expected": EXPECTED_WORLD_TYPE,
                "actual": world_type,
            },
        )

    if generator_type != EXPECTED_GENERATOR_TYPE:
        _append_error(
            errors,
            code="invalid_generator_type",
            message=(
                f"Flat world generatorType must be "
                f"'{EXPECTED_GENERATOR_TYPE}'."
            ),
            field_name="generatorType",
            actual=generator_type,
            expected=EXPECTED_GENERATOR_TYPE,
        )

    if generator_version != EXPECTED_GENERATOR_VERSION:
        _append_error(
            errors,
            code="invalid_generator_version",
            message=(
                f"Flat world generatorVersion must be "
                f"'{EXPECTED_GENERATOR_VERSION}'."
            ),
            field_name="generatorVersion",
            actual=generator_version,
            expected=EXPECTED_GENERATOR_VERSION,
        )

    if projection_type != EXPECTED_PROJECTION_TYPE:
        _append_error(
            errors,
            code="invalid_projection_type",
            message=(
                f"Flat world projectionType must be "
                f"'{EXPECTED_PROJECTION_TYPE}'."
            ),
            field_name="projectionType",
            actual=projection_type,
            expected=EXPECTED_PROJECTION_TYPE,
        )

    if topology_type != EXPECTED_TOPOLOGY_TYPE:
        _append_error(
            errors,
            code="invalid_topology_type",
            message=(
                f"Flat world topologyType must be "
                f"'{EXPECTED_TOPOLOGY_TYPE}'."
            ),
            field_name="topologyType",
            actual=topology_type,
            expected=EXPECTED_TOPOLOGY_TYPE,
        )

    if coordinate_system != EXPECTED_COORDINATE_SYSTEM:
        _append_error(
            errors,
            code="invalid_coordinate_system",
            message=(
                f"Flat world coordinateSystem must be "
                f"'{EXPECTED_COORDINATE_SYSTEM}'."
            ),
            field_name="coordinateSystem",
            actual=coordinate_system,
            expected=EXPECTED_COORDINATE_SYSTEM,
        )

    _raise_collected_errors(
        message="Flat world identity validation failed.",
        errors=errors,
        details={
            "worldId": world_id,
            "worldType": world_type,
            "generatorType": generator_type,
            "generatorVersion": generator_version,
        },
    )


def validate_flat_dimensions(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft chunkSize, cellSize, surfaceY, minY und maxY.
    """
    chunk_size = _to_int(
        _get_field(raw_config, "chunkSize", "chunk_size"),
        field_name="chunkSize",
    )
    cell_size = _to_float(
        _get_field(raw_config, "cellSize", "cell_size"),
        field_name="cellSize",
    )
    surface_y = _to_int(
        _get_field(raw_config, "surfaceY", "surface_y"),
        field_name="surfaceY",
    )
    min_y = _to_int(
        _get_field(raw_config, "minY", "min_y"),
        field_name="minY",
    )
    max_y = _to_int(
        _get_field(raw_config, "maxY", "max_y"),
        field_name="maxY",
    )

    errors: list[dict[str, Any]] = []

    if chunk_size < MIN_CHUNK_SIZE:
        _append_error(
            errors,
            code="chunk_size_too_small",
            message=f"chunkSize must be >= {MIN_CHUNK_SIZE}.",
            field_name="chunkSize",
            actual=chunk_size,
            minimum=MIN_CHUNK_SIZE,
        )

    if chunk_size > MAX_REASONABLE_CHUNK_SIZE:
        _append_error(
            errors,
            code="chunk_size_too_large",
            message=f"chunkSize must be <= {MAX_REASONABLE_CHUNK_SIZE}.",
            field_name="chunkSize",
            actual=chunk_size,
            maximum=MAX_REASONABLE_CHUNK_SIZE,
        )

    if cell_size < MIN_CELL_SIZE:
        _append_error(
            errors,
            code="cell_size_too_small",
            message=f"cellSize must be >= {MIN_CELL_SIZE}.",
            field_name="cellSize",
            actual=cell_size,
            minimum=MIN_CELL_SIZE,
        )

    if min_y > max_y:
        _append_error(
            errors,
            code="invalid_y_range",
            message="minY must be <= maxY.",
            field_name="minY/maxY",
            minY=min_y,
            maxY=max_y,
        )

    if not (min_y <= surface_y <= max_y):
        _append_error(
            errors,
            code="surface_y_out_of_range",
            message="surfaceY must be between minY and maxY.",
            field_name="surfaceY",
            surfaceY=surface_y,
            minY=min_y,
            maxY=max_y,
        )

    _raise_collected_errors(
        message="Flat world dimension validation failed.",
        errors=errors,
        details={
            "chunkSize": chunk_size,
            "cellSize": cell_size,
            "surfaceY": surface_y,
            "minY": min_y,
            "maxY": max_y,
        },
    )


def validate_required_system_blocks(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft den unveränderlichen Air-/Terrain-Systemblockvertrag.
    """
    required = _as_dict(
        raw_config.get("requiredSystemBlocks"),
        field_name="requiredSystemBlocks",
    )
    air = _as_dict(
        required.get("air"),
        field_name="requiredSystemBlocks.air",
    )
    terrain = _as_dict(
        required.get("terrain"),
        field_name="requiredSystemBlocks.terrain",
    )

    contract = get_flat_system_block_contract()
    errors: list[dict[str, Any]] = []

    version = _safe_str(required.get("version"))
    immutable = _to_bool(required.get("immutable"), default=False)

    if version != EXPECTED_REQUIRED_SYSTEM_BLOCKS_VERSION:
        _append_error(
            errors,
            code="invalid_required_system_blocks_version",
            message=(
                "requiredSystemBlocks.version must be "
                f"'{EXPECTED_REQUIRED_SYSTEM_BLOCKS_VERSION}'."
            ),
            field_name="requiredSystemBlocks.version",
            actual=version,
            expected=EXPECTED_REQUIRED_SYSTEM_BLOCKS_VERSION,
        )

    if not immutable:
        _append_error(
            errors,
            code="system_blocks_must_be_immutable",
            message="The required flat-world system blocks must be immutable.",
            field_name="requiredSystemBlocks.immutable",
            actual=required.get("immutable"),
            expected=True,
        )

    air_system_block_id = _safe_str(air.get("systemBlockId"))
    air_runtime_block_type_id = _optional_str(air.get("runtimeBlockTypeId"))
    air_reserved_cell_value = _to_int(
        air.get("reservedCellValue"),
        field_name="requiredSystemBlocks.air.reservedCellValue",
        default=DEFAULT_AIR_CELL_VALUE,
    )
    air_persist_as_block_type = _to_bool(
        air.get("persistAsBlockType"),
        default=False,
    )
    air_stored_in_positive_palette = _to_bool(
        air.get("storedInPositivePalette"),
        default=False,
    )

    if air_system_block_id != contract.air_system_block_id:
        _append_error(
            errors,
            code="invalid_air_system_block_id",
            message=(
                "requiredSystemBlocks.air.systemBlockId must identify "
                f"'{contract.air_system_block_id}'."
            ),
            field_name="requiredSystemBlocks.air.systemBlockId",
            actual=air_system_block_id,
            expected=contract.air_system_block_id,
        )

    if air_runtime_block_type_id is not None:
        _append_error(
            errors,
            code="air_runtime_block_type_forbidden",
            message="system_air must not have a runtimeBlockTypeId.",
            field_name="requiredSystemBlocks.air.runtimeBlockTypeId",
            actual=air_runtime_block_type_id,
            expected=None,
        )

    if air_reserved_cell_value != contract.air_reserved_cell_value:
        _append_error(
            errors,
            code="invalid_air_reserved_cell_value",
            message=(
                "system_air must reserve exactly "
                f"cellValue {contract.air_reserved_cell_value}."
            ),
            field_name="requiredSystemBlocks.air.reservedCellValue",
            actual=air_reserved_cell_value,
            expected=contract.air_reserved_cell_value,
        )

    if air_persist_as_block_type:
        _append_error(
            errors,
            code="air_persistence_forbidden",
            message="system_air must never be persisted as BlockType.",
            field_name="requiredSystemBlocks.air.persistAsBlockType",
            actual=air_persist_as_block_type,
            expected=False,
        )

    if air_stored_in_positive_palette:
        _append_error(
            errors,
            code="air_positive_palette_forbidden",
            message="system_air must never be stored in the positive palette.",
            field_name="requiredSystemBlocks.air.storedInPositivePalette",
            actual=air_stored_in_positive_palette,
            expected=False,
        )

    terrain_system_block_id = _safe_str(terrain.get("systemBlockId"))
    terrain_runtime_block_type_id = _safe_str(
        terrain.get("runtimeBlockTypeId")
    )
    terrain_persist_as_block_type = _to_bool(
        terrain.get("persistAsBlockType"),
        default=False,
    )
    terrain_stored_in_positive_palette = _to_bool(
        terrain.get("storedInPositivePalette"),
        default=False,
    )

    if terrain_system_block_id != contract.terrain_system_block_id:
        _append_error(
            errors,
            code="invalid_terrain_system_block_id",
            message=(
                "requiredSystemBlocks.terrain.systemBlockId must identify "
                f"'{contract.terrain_system_block_id}'."
            ),
            field_name="requiredSystemBlocks.terrain.systemBlockId",
            actual=terrain_system_block_id,
            expected=contract.terrain_system_block_id,
        )

    if terrain_runtime_block_type_id != contract.terrain_runtime_block_type_id:
        _append_error(
            errors,
            code="invalid_terrain_runtime_block_type_id",
            message=(
                "system_terrain must use the stable runtimeBlockTypeId "
                f"'{contract.terrain_runtime_block_type_id}'."
            ),
            field_name="requiredSystemBlocks.terrain.runtimeBlockTypeId",
            actual=terrain_runtime_block_type_id,
            expected=contract.terrain_runtime_block_type_id,
        )

    if not terrain_persist_as_block_type:
        _append_error(
            errors,
            code="terrain_persistence_required",
            message="system_terrain must be persisted as BlockType.",
            field_name="requiredSystemBlocks.terrain.persistAsBlockType",
            actual=terrain_persist_as_block_type,
            expected=True,
        )

    if not terrain_stored_in_positive_palette:
        _append_error(
            errors,
            code="terrain_positive_palette_required",
            message="system_terrain must be stored in the positive palette.",
            field_name="requiredSystemBlocks.terrain.storedInPositivePalette",
            actual=terrain_stored_in_positive_palette,
            expected=True,
        )

    _raise_collected_errors(
        message="Flat world required system block validation failed.",
        errors=errors,
        details={
            "contract": contract.to_dict(),
            "requiredSystemBlocks": make_json_safe(required),
        },
    )


def extract_palette_block_type_ids(
    raw_config: Mapping[str, Any],
) -> tuple[str, ...]:
    """
    Extrahiert alle blockTypeIds aus der positiven Palette.
    """
    palette = _as_list(raw_config.get("palette"), field_name="palette")
    block_type_ids: list[str] = []
    errors: list[dict[str, Any]] = []

    for index, entry in enumerate(palette):
        if not isinstance(entry, Mapping):
            _append_error(
                errors,
                code="invalid_palette_entry_type",
                message="Palette entry must be an object.",
                field_name="palette",
                index=index,
                entryType=type(entry).__name__,
            )
            continue

        block_type_id = _safe_str(
            entry.get("blockTypeId") or entry.get("block_type_id")
        )

        if not block_type_id:
            _append_error(
                errors,
                code="missing_block_type_id",
                message="Palette entry requires blockTypeId.",
                field_name="palette.blockTypeId",
                index=index,
            )
            continue

        block_type_ids.append(block_type_id)

    duplicate_ids = sorted(
        block_type_id
        for block_type_id in set(block_type_ids)
        if block_type_ids.count(block_type_id) > 1
    )

    if duplicate_ids:
        _append_error(
            errors,
            code="duplicate_block_type_ids",
            message="Palette contains duplicate blockTypeIds.",
            field_name="palette",
            duplicateBlockTypeIds=duplicate_ids,
        )

    if not block_type_ids:
        _append_error(
            errors,
            code="empty_palette",
            message="Flat world palette must contain system_terrain.",
            field_name="palette",
        )

    _raise_collected_errors(
        message="Flat world palette extraction failed.",
        errors=errors,
        details={"blockTypeIds": block_type_ids},
    )

    return tuple(block_type_ids)


def validate_flat_palette(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft die positive Palette und erzwingt genau system_terrain.

    Air ist kein PaletteEntry. Damit besteht die Welt fachlich aus zwei
    Systemzuständen, technisch aber aus genau einem positiven PaletteEntry plus
    dem reservierten Zellwert 0.
    """
    palette = _as_list(raw_config.get("palette"), field_name="palette")
    block_registry_id = _safe_str(
        _get_field(raw_config, "blockRegistryId", "block_registry_id"),
        default=DEFAULT_BLOCK_REGISTRY_ID,
    )
    block_registry_version = _safe_str(
        _get_field(
            raw_config,
            "blockRegistryVersion",
            "block_registry_version",
        ),
        default=DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    errors: list[dict[str, Any]] = []

    if not block_registry_id:
        _append_error(
            errors,
            code="missing_block_registry_id",
            message="blockRegistryId must not be empty.",
            field_name="blockRegistryId",
        )

    if not block_registry_version:
        _append_error(
            errors,
            code="missing_block_registry_version",
            message="blockRegistryVersion must not be empty.",
            field_name="blockRegistryVersion",
        )

    if len(palette) != EXPECTED_POSITIVE_PALETTE_SIZE:
        _append_error(
            errors,
            code="invalid_flat_positive_palette_size",
            message=(
                "The standard flat world must contain exactly one positive "
                "palette entry: system_terrain."
            ),
            field_name="palette",
            actual=len(palette),
            expected=EXPECTED_POSITIVE_PALETTE_SIZE,
        )

    seen: set[str] = set()

    for index, entry in enumerate(palette):
        if not isinstance(entry, Mapping):
            _append_error(
                errors,
                code="invalid_palette_entry_type",
                message="Palette entry must be an object.",
                field_name="palette",
                index=index,
                entryType=type(entry).__name__,
            )
            continue

        block_type_id = _safe_str(
            entry.get("blockTypeId") or entry.get("block_type_id")
        )
        label = _safe_str(entry.get("label"), default=block_type_id)

        if not block_type_id:
            _append_error(
                errors,
                code="missing_block_type_id",
                message="Palette entry requires blockTypeId.",
                field_name="palette.blockTypeId",
                index=index,
            )
            continue

        if block_type_id in seen:
            _append_error(
                errors,
                code="duplicate_block_type_id",
                message="Palette blockTypeId must be unique.",
                field_name="palette.blockTypeId",
                index=index,
                blockTypeId=block_type_id,
            )

        seen.add(block_type_id)

        if block_type_id == SYSTEM_AIR_BLOCK_ID:
            _append_error(
                errors,
                code="system_air_in_positive_palette",
                message="system_air must not appear in the positive palette.",
                field_name="palette.blockTypeId",
                index=index,
                blockTypeId=block_type_id,
            )

        if block_type_id != SYSTEM_TERRAIN_BLOCK_TYPE_ID:
            _append_error(
                errors,
                code="unexpected_flat_palette_block",
                message=(
                    "The standard flat world positive palette may contain "
                    "only system_terrain."
                ),
                field_name="palette.blockTypeId",
                index=index,
                blockTypeId=block_type_id,
                expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            )

        if index != EXPECTED_TERRAIN_PALETTE_INDEX:
            _append_error(
                errors,
                code="invalid_terrain_palette_index",
                message=(
                    "system_terrain must be paletteIndex 0 in the canonical "
                    "default flat world."
                ),
                field_name="palette",
                index=index,
                expected=EXPECTED_TERRAIN_PALETTE_INDEX,
            )

        if not label:
            _append_error(
                errors,
                code="missing_block_label",
                message="Palette entry label must not be empty.",
                field_name="palette.label",
                index=index,
                blockTypeId=block_type_id,
            )

        for boolean_field, expected_value in (
            EXPECTED_TERRAIN_PALETTE_BOOLEAN_FIELDS.items()
        ):
            raw_value = entry.get(boolean_field, expected_value)

            if not _is_bool_like(raw_value):
                _append_error(
                    errors,
                    code="invalid_palette_boolean",
                    message=(
                        f"Palette field '{boolean_field}' must be "
                        "boolean-like."
                    ),
                    field_name=f"palette.{boolean_field}",
                    index=index,
                    blockTypeId=block_type_id,
                    value=raw_value,
                )
                continue

            actual_value = _to_bool(raw_value, default=expected_value)

            if actual_value != expected_value:
                _append_error(
                    errors,
                    code="invalid_system_terrain_flag",
                    message=(
                        f"system_terrain requires {boolean_field}="
                        f"{expected_value} in the default flat palette."
                    ),
                    field_name=f"palette.{boolean_field}",
                    index=index,
                    actual=actual_value,
                    expected=expected_value,
                )

        entry_registry_id = _safe_str(
            entry.get("registryId") or entry.get("registry_id"),
            default=block_registry_id,
        )
        entry_registry_version = _safe_str(
            entry.get("registryVersion") or entry.get("registry_version"),
            default=block_registry_version,
        )

        if entry_registry_id != block_registry_id:
            _append_error(
                errors,
                code="palette_registry_id_mismatch",
                message=(
                    "Palette entry registryId must match world "
                    "blockRegistryId."
                ),
                field_name="palette.registryId",
                index=index,
                blockTypeId=block_type_id,
                entryRegistryId=entry_registry_id,
                worldRegistryId=block_registry_id,
            )

        if entry_registry_version != block_registry_version:
            _append_error(
                errors,
                code="palette_registry_version_mismatch",
                message=(
                    "Palette entry registryVersion must match world "
                    "blockRegistryVersion."
                ),
                field_name="palette.registryVersion",
                index=index,
                blockTypeId=block_type_id,
                entryRegistryVersion=entry_registry_version,
                worldRegistryVersion=block_registry_version,
            )

        metadata = _as_dict(
            entry.get("metadata", {}),
            field_name=f"palette[{index}].metadata",
        )

        for field_name, expected_value in (
            EXPECTED_TERRAIN_METADATA_FIELDS.items()
        ):
            actual_value = metadata.get(field_name)

            if isinstance(expected_value, bool):
                if not _is_bool_like(actual_value):
                    _append_error(
                        errors,
                        code="missing_or_invalid_terrain_metadata_boolean",
                        message=(
                            f"system_terrain metadata.{field_name} must be "
                            f"{expected_value}."
                        ),
                        field_name=f"palette.metadata.{field_name}",
                        index=index,
                        actual=actual_value,
                        expected=expected_value,
                    )
                    continue

                actual_value = _to_bool(
                    actual_value,
                    default=not expected_value,
                )
            else:
                actual_value = _safe_str(actual_value)

            if actual_value != expected_value:
                _append_error(
                    errors,
                    code="invalid_terrain_metadata",
                    message=(
                        f"system_terrain metadata.{field_name} must equal "
                        f"{expected_value!r}."
                    ),
                    field_name=f"palette.metadata.{field_name}",
                    index=index,
                    actual=actual_value,
                    expected=expected_value,
                )

    block_type_ids = tuple(
        entry_id
        for entry_id in (
            _safe_str(
                entry.get("blockTypeId") or entry.get("block_type_id")
            )
            for entry in palette
            if isinstance(entry, Mapping)
        )
        if entry_id
    )

    if block_type_ids != get_expected_flat_palette_block_type_ids():
        _append_error(
            errors,
            code="flat_palette_contract_mismatch",
            message=(
                "The standard flat world palette must equal "
                "('system_terrain',)."
            ),
            field_name="palette",
            actual=block_type_ids,
            expected=get_expected_flat_palette_block_type_ids(),
        )

    _raise_collected_errors(
        message="Flat world palette validation failed.",
        errors=errors,
        details={
            "paletteSize": len(palette),
            "blockRegistryId": block_registry_id,
            "blockRegistryVersion": block_registry_version,
            "expectedBlockTypeIds": get_expected_flat_palette_block_type_ids(),
        },
    )


def validate_flat_layers(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft die kanonischen Air-/Terrain-Layer der Standard-Flat-World.
    """
    layers = _as_dict(raw_config.get("layers"), field_name="layers")
    block_type_ids = extract_palette_block_type_ids(raw_config)
    errors: list[dict[str, Any]] = []

    for field_name in REQUIRED_LAYER_FIELDS:
        value = layers.get(field_name)

        if value is None:
            _append_error(
                errors,
                code="missing_layer_field",
                message=f"layers.{field_name} is required.",
                field_name=f"layers.{field_name}",
            )

    version = _safe_str(
        layers.get("version"),
        default=EXPECTED_LAYERS_VERSION,
    )
    air_block_value = _to_int(
        layers.get("airBlockValue", DEFAULT_AIR_CELL_VALUE),
        field_name="layers.airBlockValue",
        default=DEFAULT_AIR_CELL_VALUE,
    )
    air_system_block_id = _safe_str(
        layers.get("airSystemBlockId"),
        default=SYSTEM_AIR_BLOCK_ID,
    )
    surface_block_type_id = _safe_str(
        layers.get("surfaceBlockTypeId"),
        default=DEFAULT_SURFACE_BLOCK_TYPE_ID,
    )
    subsurface_block_type_id = _safe_str(
        layers.get("subsurfaceBlockTypeId"),
        default=DEFAULT_SUBSURFACE_BLOCK_TYPE_ID,
    )
    terrain_block_type_id = _safe_str(
        layers.get("terrainBlockTypeId"),
        default=DEFAULT_TERRAIN_BLOCK_TYPE_ID,
    )

    if version != EXPECTED_LAYERS_VERSION:
        _append_error(
            errors,
            code="invalid_layers_version",
            message=(
                f"layers.version must be '{EXPECTED_LAYERS_VERSION}'."
            ),
            field_name="layers.version",
            actual=version,
            expected=EXPECTED_LAYERS_VERSION,
        )

    if air_block_value != DEFAULT_AIR_CELL_VALUE:
        _append_error(
            errors,
            code="invalid_air_block_value",
            message=(
                f"layers.airBlockValue must be "
                f"{DEFAULT_AIR_CELL_VALUE}."
            ),
            field_name="layers.airBlockValue",
            actual=air_block_value,
            expected=DEFAULT_AIR_CELL_VALUE,
        )

    if air_system_block_id != SYSTEM_AIR_BLOCK_ID:
        _append_error(
            errors,
            code="invalid_layer_air_system_block_id",
            message=(
                f"layers.airSystemBlockId must be "
                f"'{SYSTEM_AIR_BLOCK_ID}'."
            ),
            field_name="layers.airSystemBlockId",
            actual=air_system_block_id,
            expected=SYSTEM_AIR_BLOCK_ID,
        )

    for field_name, block_type_id in (
        ("surfaceBlockTypeId", surface_block_type_id),
        ("subsurfaceBlockTypeId", subsurface_block_type_id),
        ("terrainBlockTypeId", terrain_block_type_id),
    ):
        if block_type_id != SYSTEM_TERRAIN_BLOCK_TYPE_ID:
            _append_error(
                errors,
                code="flat_layer_must_use_system_terrain",
                message=(
                    f"layers.{field_name} must be "
                    f"'{SYSTEM_TERRAIN_BLOCK_TYPE_ID}'."
                ),
                field_name=f"layers.{field_name}",
                actual=block_type_id,
                expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            )

        if block_type_id and block_type_id not in block_type_ids:
            _append_error(
                errors,
                code="layer_block_not_in_palette",
                message=f"layers.{field_name} must exist in palette.",
                field_name=f"layers.{field_name}",
                blockTypeId=block_type_id,
                availableBlockTypeIds=block_type_ids,
            )

    if surface_block_type_id != subsurface_block_type_id:
        _append_error(
            errors,
            code="surface_subsurface_mismatch",
            message=(
                "The default flat world surface and subsurface must use "
                "the same system_terrain block."
            ),
            field_name="layers",
            surfaceBlockTypeId=surface_block_type_id,
            subsurfaceBlockTypeId=subsurface_block_type_id,
        )

    rules = _as_list(
        layers.get("rules", []),
        field_name="layers.rules",
    )

    expected_rules = {
        contract.normalized_condition: contract
        for contract in get_expected_flat_layer_rule_contracts()
    }
    seen_conditions: set[str] = set()

    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            _append_error(
                errors,
                code="invalid_layer_rule_type",
                message="Layer rule must be an object.",
                field_name="layers.rules",
                index=index,
                ruleType=type(rule).__name__,
            )
            continue

        condition = _normalize_condition(rule.get("condition"))

        if not condition:
            _append_error(
                errors,
                code="missing_layer_rule_condition",
                message="Layer rule requires condition.",
                field_name="layers.rules.condition",
                index=index,
            )
            continue

        if condition in seen_conditions:
            _append_error(
                errors,
                code="duplicate_layer_rule_condition",
                message="Layer rule conditions must be unique.",
                field_name="layers.rules.condition",
                index=index,
                condition=rule.get("condition"),
            )

        seen_conditions.add(condition)
        expected = expected_rules.get(condition)

        if expected is None:
            _append_error(
                errors,
                code="unsupported_flat_layer_rule",
                message=(
                    "The canonical default flat world does not support "
                    "additional layer conditions."
                ),
                field_name="layers.rules.condition",
                index=index,
                condition=rule.get("condition"),
                supportedConditions=[
                    item.condition
                    for item in get_expected_flat_layer_rule_contracts()
                ],
            )
            continue

        actual_system_block_id = _safe_str(rule.get("systemBlockId"))
        actual_block_type_id = _optional_str(rule.get("blockTypeId"))

        if actual_system_block_id != expected.system_block_id:
            _append_error(
                errors,
                code="layer_rule_system_block_mismatch",
                message=(
                    "Layer rule systemBlockId does not match its "
                    "canonical role."
                ),
                field_name="layers.rules.systemBlockId",
                index=index,
                condition=expected.condition,
                actual=actual_system_block_id,
                expected=expected.system_block_id,
            )

        if actual_block_type_id != expected.block_type_id:
            _append_error(
                errors,
                code="layer_rule_block_type_mismatch",
                message=(
                    "Layer rule blockTypeId does not match its "
                    "canonical role."
                ),
                field_name="layers.rules.blockTypeId",
                index=index,
                condition=expected.condition,
                actual=actual_block_type_id,
                expected=expected.block_type_id,
            )

        if expected.cell_value is not None:
            actual_cell_value = _to_int(
                rule.get("cellValue"),
                field_name="layers.rules.cellValue",
                default=DEFAULT_AIR_CELL_VALUE,
            )

            if actual_cell_value != expected.cell_value:
                _append_error(
                    errors,
                    code="layer_rule_cell_value_mismatch",
                    message=(
                        "Air layer rules must explicitly use cellValue 0."
                    ),
                    field_name="layers.rules.cellValue",
                    index=index,
                    condition=expected.condition,
                    actual=actual_cell_value,
                    expected=expected.cell_value,
                )
        elif "cellValue" in rule:
            actual_cell_value = _to_int(
                rule.get("cellValue"),
                field_name="layers.rules.cellValue",
            )

            if actual_cell_value != EXPECTED_TERRAIN_CELL_VALUE:
                _append_error(
                    errors,
                    code="invalid_optional_terrain_rule_cell_value",
                    message=(
                        "If a canonical terrain rule declares cellValue, "
                        "it must currently equal 1."
                    ),
                    field_name="layers.rules.cellValue",
                    index=index,
                    condition=expected.condition,
                    actual=actual_cell_value,
                    expected=EXPECTED_TERRAIN_CELL_VALUE,
                )

    missing_conditions = sorted(
        set(expected_rules.keys()) - seen_conditions
    )

    if missing_conditions:
        _append_error(
            errors,
            code="missing_canonical_flat_layer_rules",
            message=(
                "The canonical default flat world is missing one or more "
                "required layer rules."
            ),
            field_name="layers.rules",
            missingConditions=[
                expected_rules[item].condition
                for item in missing_conditions
            ],
        )

    _raise_collected_errors(
        message="Flat world layer validation failed.",
        errors=errors,
        details={
            "layers": make_json_safe(layers),
            "expectedTerrainBlockTypeId": SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            "expectedAirSystemBlockId": SYSTEM_AIR_BLOCK_ID,
        },
    )


def validate_cell_encoding(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft die harte Cell-Encoding-Invariante.
    """
    cell_encoding = _as_dict(
        raw_config.get("cellEncoding"),
        field_name="cellEncoding",
    )

    version = _safe_str(
        cell_encoding.get("version"),
        default=EXPECTED_CELL_ENCODING_VERSION,
    )
    air_cell_value = _to_int(
        cell_encoding.get("airCellValue", DEFAULT_AIR_CELL_VALUE),
        field_name="cellEncoding.airCellValue",
        default=DEFAULT_AIR_CELL_VALUE,
    )
    air_system_block_id = _safe_str(
        cell_encoding.get("airSystemBlockId"),
        default=SYSTEM_AIR_BLOCK_ID,
    )
    block_cell_value_rule = _safe_str(
        cell_encoding.get("blockCellValueRule"),
        default="paletteIndex + 1",
    )
    positive_values_are_local = _to_bool(
        cell_encoding.get("positivePaletteValuesAreLocal"),
        default=True,
    )

    errors: list[dict[str, Any]] = []

    if version != EXPECTED_CELL_ENCODING_VERSION:
        _append_error(
            errors,
            code="invalid_cell_encoding_version",
            message=(
                "cellEncoding.version must be "
                f"'{EXPECTED_CELL_ENCODING_VERSION}'."
            ),
            field_name="cellEncoding.version",
            actual=version,
            expected=EXPECTED_CELL_ENCODING_VERSION,
        )

    if air_cell_value != DEFAULT_AIR_CELL_VALUE:
        _append_error(
            errors,
            code="invalid_air_cell_value",
            message=(
                f"cellEncoding.airCellValue must be "
                f"{DEFAULT_AIR_CELL_VALUE}."
            ),
            field_name="cellEncoding.airCellValue",
            actual=air_cell_value,
            expected=DEFAULT_AIR_CELL_VALUE,
        )

    if air_system_block_id != SYSTEM_AIR_BLOCK_ID:
        _append_error(
            errors,
            code="invalid_cell_encoding_air_system_block_id",
            message=(
                f"cellEncoding.airSystemBlockId must be "
                f"'{SYSTEM_AIR_BLOCK_ID}'."
            ),
            field_name="cellEncoding.airSystemBlockId",
            actual=air_system_block_id,
            expected=SYSTEM_AIR_BLOCK_ID,
        )

    normalized_rule = block_cell_value_rule.replace(" ", "").lower()

    if normalized_rule not in {"paletteindex+1", "palette_index+1"}:
        _append_error(
            errors,
            code="invalid_block_cell_value_rule",
            message=(
                "cellEncoding.blockCellValueRule must be "
                "'paletteIndex + 1'."
            ),
            field_name="cellEncoding.blockCellValueRule",
            actual=block_cell_value_rule,
            expected="paletteIndex + 1",
        )

    if not positive_values_are_local:
        _append_error(
            errors,
            code="positive_palette_values_must_be_local",
            message=(
                "Positive cell values must remain local to the concrete "
                "palette."
            ),
            field_name="cellEncoding.positivePaletteValuesAreLocal",
            actual=positive_values_are_local,
            expected=True,
        )

    examples = cell_encoding.get("examples", [])

    if examples is not None:
        examples_list = _as_list(
            examples,
            field_name="cellEncoding.examples",
        )

        for index, example in enumerate(examples_list):
            if not isinstance(example, Mapping):
                _append_error(
                    errors,
                    code="invalid_cell_encoding_example",
                    message="Cell encoding example must be an object.",
                    field_name="cellEncoding.examples",
                    index=index,
                    exampleType=type(example).__name__,
                )
                continue

            example_cell_value = _to_int(
                example.get("cellValue"),
                field_name="cellEncoding.examples.cellValue",
                default=DEFAULT_AIR_CELL_VALUE,
            )
            example_block_type_id = _optional_str(
                example.get("blockTypeId")
            )
            example_system_block_id = _safe_str(
                example.get("systemBlockId")
            )

            if example_cell_value == DEFAULT_AIR_CELL_VALUE:
                if example_block_type_id is not None:
                    _append_error(
                        errors,
                        code="air_example_has_block_type_id",
                        message=(
                            "The cellValue 0 example must not reference "
                            "a blockTypeId."
                        ),
                        field_name="cellEncoding.examples.blockTypeId",
                        index=index,
                        actual=example_block_type_id,
                    )

                if (
                    example_system_block_id
                    and example_system_block_id != SYSTEM_AIR_BLOCK_ID
                ):
                    _append_error(
                        errors,
                        code="air_example_system_block_mismatch",
                        message=(
                            "The cellValue 0 example must reference "
                            "system_air."
                        ),
                        field_name="cellEncoding.examples.systemBlockId",
                        index=index,
                        actual=example_system_block_id,
                        expected=SYSTEM_AIR_BLOCK_ID,
                    )
            else:
                if example_block_type_id == SYSTEM_AIR_BLOCK_ID:
                    _append_error(
                        errors,
                        code="positive_example_uses_system_air",
                        message=(
                            "A positive cell encoding example must not "
                            "use system_air."
                        ),
                        field_name="cellEncoding.examples.blockTypeId",
                        index=index,
                    )

                if (
                    example_block_type_id
                    and example_block_type_id
                    != SYSTEM_TERRAIN_BLOCK_TYPE_ID
                ):
                    _append_error(
                        errors,
                        code="unexpected_positive_example_block",
                        message=(
                            "The default flat-world positive example must "
                            "use system_terrain."
                        ),
                        field_name="cellEncoding.examples.blockTypeId",
                        index=index,
                        actual=example_block_type_id,
                        expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
                    )

    _raise_collected_errors(
        message="Flat world cell encoding validation failed.",
        errors=errors,
        details={"cellEncoding": make_json_safe(cell_encoding)},
    )


def validate_cell_indexing(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft die Cell-Indexing-Invariante.
    """
    cell_indexing = _as_dict(
        raw_config.get("cellIndexing"),
        field_name="cellIndexing",
    )

    version = _safe_str(
        cell_indexing.get("version"),
        default=EXPECTED_CELL_INDEXING_VERSION,
    )
    order = _safe_str(
        cell_indexing.get("order"),
        default=EXPECTED_CELL_INDEXING_ORDER,
    )

    errors: list[dict[str, Any]] = []

    if version != EXPECTED_CELL_INDEXING_VERSION:
        _append_error(
            errors,
            code="invalid_cell_indexing_version",
            message=(
                "cellIndexing.version must be "
                f"'{EXPECTED_CELL_INDEXING_VERSION}'."
            ),
            field_name="cellIndexing.version",
            actual=version,
            expected=EXPECTED_CELL_INDEXING_VERSION,
        )

    if order != EXPECTED_CELL_INDEXING_ORDER:
        _append_error(
            errors,
            code="invalid_cell_indexing_order",
            message=(
                f"cellIndexing.order must be "
                f"'{EXPECTED_CELL_INDEXING_ORDER}'."
            ),
            field_name="cellIndexing.order",
            actual=order,
            expected=EXPECTED_CELL_INDEXING_ORDER,
        )

    _raise_collected_errors(
        message="Flat world cell indexing validation failed.",
        errors=errors,
        details={"cellIndexing": make_json_safe(cell_indexing)},
    )


def validate_runtime_flags(
    raw_config: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """
    Prüft providerlokale Runtime- und Cache-Hinweise.

    Abweichungen der historischen Capability-Flags werden als Warnung gemeldet,
    weil Snapshot-/Event-/Command-Fähigkeiten außerhalb des reinen Providers in
    der World-State-Schicht liegen.
    """
    runtime = _as_dict(
        raw_config.get("runtime", {}),
        field_name="runtime",
    )
    warnings: list[dict[str, Any]] = []

    for field_name, expected_value in (
        OPTIONAL_BOOLEAN_RUNTIME_FIELDS.items()
    ):
        if field_name not in runtime:
            continue

        raw_value = runtime.get(field_name)

        if not _is_bool_like(raw_value):
            _append_warning(
                warnings,
                code="invalid_runtime_boolean",
                message=(
                    f"runtime.{field_name} is not clearly boolean-like; "
                    f"the fallback value {expected_value} will be used."
                ),
                field_name=f"runtime.{field_name}",
                value=raw_value,
                fallback=expected_value,
            )
            continue

        actual_value = _to_bool(
            raw_value,
            default=expected_value,
        )

        if actual_value != expected_value:
            _append_warning(
                warnings,
                code="unexpected_runtime_flag",
                message=(
                    f"runtime.{field_name} is {actual_value}, but the "
                    f"flat provider contract expects {expected_value}."
                ),
                field_name=f"runtime.{field_name}",
                expected=expected_value,
                actual=actual_value,
            )

    cache_policy_raw = runtime.get("cachePolicy")

    if cache_policy_raw is not None:
        cache_policy = _as_dict(
            cache_policy_raw,
            field_name="runtime.cachePolicy",
        )

        expected_cache_flags = {
            "worldDefinitionCacheAllowed": True,
            "invalidateOnProcessRestart": True,
            "generatorOutputCacheRequired": False,
        }

        for field_name, expected_value in expected_cache_flags.items():
            if field_name not in cache_policy:
                _append_warning(
                    warnings,
                    code="missing_cache_policy_field",
                    message=(
                        f"runtime.cachePolicy.{field_name} is not set."
                    ),
                    field_name=f"runtime.cachePolicy.{field_name}",
                    expected=expected_value,
                )
                continue

            raw_value = cache_policy.get(field_name)

            if not _is_bool_like(raw_value):
                _append_warning(
                    warnings,
                    code="invalid_cache_policy_boolean",
                    message=(
                        f"runtime.cachePolicy.{field_name} must be "
                        "boolean-like."
                    ),
                    field_name=f"runtime.cachePolicy.{field_name}",
                    value=raw_value,
                )

    return tuple(warnings)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_flat_world_config(
    raw_config: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Normalisiert eine Flat-World-Konfiguration.

    Die Funktion:
    - mutiert die Eingabe nicht
    - ergänzt nur sichere Defaults
    - erzeugt neue verschachtelte Strukturen
    - ersetzt keine explizit gesetzten fachlichen Werte
    - bereitet die Konfiguration für die strikte Validierung vor
    """
    if not isinstance(raw_config, Mapping):
        raise InvalidWorldDefinitionError(
            "Flat world config must be an object.",
            details={
                "configType": type(raw_config).__name__,
                "config": make_json_safe(raw_config),
            },
        )

    try:
        normalized = deepcopy(dict(raw_config))
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Flat world config could not be copied safely.",
            details={
                "configType": type(raw_config).__name__,
                "config": make_json_safe(raw_config),
            },
            cause=exc,
        ) from exc

    normalized.setdefault("schemaVersion", EXPECTED_WORLD_SCHEMA_VERSION)
    normalized.setdefault("worldId", EXPECTED_WORLD_ID)
    normalized.setdefault("worldType", EXPECTED_WORLD_TYPE)
    normalized.setdefault("label", EXPECTED_WORLD_LABEL)
    normalized.setdefault("status", "development")

    normalized.setdefault("generatorType", EXPECTED_GENERATOR_TYPE)
    normalized.setdefault("generatorVersion", EXPECTED_GENERATOR_VERSION)

    normalized.setdefault(
        "coordinateSystem",
        EXPECTED_COORDINATE_SYSTEM,
    )
    normalized.setdefault(
        "projectionType",
        EXPECTED_PROJECTION_TYPE,
    )
    normalized.setdefault(
        "topologyType",
        EXPECTED_TOPOLOGY_TYPE,
    )

    normalized.setdefault(
        "blockRegistryId",
        DEFAULT_BLOCK_REGISTRY_ID,
    )
    normalized.setdefault(
        "blockRegistryVersion",
        DEFAULT_BLOCK_REGISTRY_VERSION,
    )

    normalized["chunkSize"] = _to_int(
        normalized.get("chunkSize", 16),
        field_name="chunkSize",
        default=16,
    )
    normalized["cellSize"] = _to_float(
        normalized.get("cellSize", 1.0),
        field_name="cellSize",
        default=1.0,
    )
    normalized["surfaceY"] = _to_int(
        normalized.get("surfaceY", 0),
        field_name="surfaceY",
        default=0,
    )
    normalized["minY"] = _to_int(
        normalized.get("minY", -8),
        field_name="minY",
        default=-8,
    )
    normalized["maxY"] = _to_int(
        normalized.get("maxY", 64),
        field_name="maxY",
        default=64,
    )

    normalized["metadata"] = _normalize_metadata(
        normalized.get("metadata", {})
    )

    required_system_blocks = _deep_merge_defaults(
        normalized.get("requiredSystemBlocks")
        if isinstance(
            normalized.get("requiredSystemBlocks"),
            Mapping,
        )
        else {},
        _build_default_required_system_blocks(),
    )
    normalized["requiredSystemBlocks"] = required_system_blocks

    layers_defaults = {
        "version": EXPECTED_LAYERS_VERSION,
        "airBlockValue": DEFAULT_AIR_CELL_VALUE,
        "airSystemBlockId": SYSTEM_AIR_BLOCK_ID,
        "surfaceBlockTypeId": DEFAULT_SURFACE_BLOCK_TYPE_ID,
        "subsurfaceBlockTypeId": DEFAULT_SUBSURFACE_BLOCK_TYPE_ID,
        "terrainBlockTypeId": DEFAULT_TERRAIN_BLOCK_TYPE_ID,
    }

    layers = _deep_merge_defaults(
        normalized.get("layers")
        if isinstance(normalized.get("layers"), Mapping)
        else {},
        layers_defaults,
    )
    normalized["layers"] = layers

    cell_encoding_defaults = {
        "version": EXPECTED_CELL_ENCODING_VERSION,
        "airCellValue": DEFAULT_AIR_CELL_VALUE,
        "airSystemBlockId": SYSTEM_AIR_BLOCK_ID,
        "blockCellValueRule": "paletteIndex + 1",
        "positivePaletteValuesAreLocal": True,
    }

    cell_encoding = _deep_merge_defaults(
        normalized.get("cellEncoding")
        if isinstance(normalized.get("cellEncoding"), Mapping)
        else {},
        cell_encoding_defaults,
    )
    normalized["cellEncoding"] = cell_encoding

    cell_indexing_defaults = {
        "version": EXPECTED_CELL_INDEXING_VERSION,
        "order": EXPECTED_CELL_INDEXING_ORDER,
        "formula": (
            "index = localX + chunkSize * "
            "(localY + chunkSize * localZ)"
        ),
    }

    cell_indexing = _deep_merge_defaults(
        normalized.get("cellIndexing")
        if isinstance(normalized.get("cellIndexing"), Mapping)
        else {},
        cell_indexing_defaults,
    )
    normalized["cellIndexing"] = cell_indexing

    runtime_defaults: dict[str, Any] = {
        **OPTIONAL_BOOLEAN_RUNTIME_FIELDS,
        "source": "generated",
        "cachePolicy": {
            "worldDefinitionCacheAllowed": True,
            "invalidateOnProcessRestart": True,
            "generatorOutputCacheRequired": False,
        },
    }

    runtime = _deep_merge_defaults(
        normalized.get("runtime")
        if isinstance(normalized.get("runtime"), Mapping)
        else {},
        runtime_defaults,
    )
    normalized["runtime"] = runtime

    palette_raw = normalized.get("palette")

    if isinstance(palette_raw, list | tuple):
        normalized_palette: list[Any] = []

        for index, entry in enumerate(palette_raw):
            if not isinstance(entry, Mapping):
                normalized_palette.append(entry)
                continue

            normalized_entry = deepcopy(dict(entry))
            normalized_entry.setdefault(
                "registryId",
                normalized.get(
                    "blockRegistryId",
                    DEFAULT_BLOCK_REGISTRY_ID,
                ),
            )
            normalized_entry.setdefault(
                "registryVersion",
                normalized.get(
                    "blockRegistryVersion",
                    DEFAULT_BLOCK_REGISTRY_VERSION,
                ),
            )

            block_type_id = _safe_str(
                normalized_entry.get("blockTypeId")
                or normalized_entry.get("block_type_id")
            )

            if block_type_id == SYSTEM_TERRAIN_BLOCK_TYPE_ID:
                normalized_entry.setdefault("label", "Terrain")

                for field_name, expected_value in (
                    EXPECTED_TERRAIN_PALETTE_BOOLEAN_FIELDS.items()
                ):
                    normalized_entry.setdefault(
                        field_name,
                        expected_value,
                    )

                metadata = _deep_merge_defaults(
                    normalized_entry.get("metadata")
                    if isinstance(
                        normalized_entry.get("metadata"),
                        Mapping,
                    )
                    else {},
                    _build_default_terrain_palette_metadata(),
                )
                normalized_entry["metadata"] = metadata

            normalized_palette.append(normalized_entry)

        normalized["palette"] = normalized_palette

    return normalized


# ---------------------------------------------------------------------------
# Cached complete validation
# ---------------------------------------------------------------------------

def _validate_flat_world_config_uncached(
    raw_config: Mapping[str, Any],
) -> FlatWorldValidationResult:
    """
    Interne vollständige Validierung ohne Cache.
    """
    try:
        normalized = normalize_flat_world_config(raw_config)

        validate_required_top_level_fields(normalized)
        validate_flat_identity(normalized)
        validate_flat_dimensions(normalized)
        validate_required_system_blocks(normalized)
        validate_flat_palette(normalized)
        validate_flat_layers(normalized)
        validate_cell_encoding(normalized)
        validate_cell_indexing(normalized)

        warnings = validate_runtime_flags(normalized)

        definition = WorldDefinition.from_dict(normalized)
        definition.validate()

        surface_block_type_id, subsurface_block_type_id = (
            get_flat_layer_block_type_ids(normalized)
        )

        return FlatWorldValidationResult(
            valid=True,
            normalized_config=normalized,
            warnings=warnings,
            metadata={
                "validatorVersion": FLAT_VALIDATOR_VERSION,
                "validationContractVersion": (
                    FLAT_VALIDATION_CONTRACT_VERSION
                ),
                "worldId": definition.world_id,
                "worldType": definition.world_type,
                "generatorType": definition.generator_type,
                "generatorVersion": definition.generator_version,
                "paletteBlockTypeIds": (
                    definition.palette_block_type_ids
                ),
                "airSystemBlockId": SYSTEM_AIR_BLOCK_ID,
                "airCellValue": DEFAULT_AIR_CELL_VALUE,
                "terrainSystemBlockId": (
                    SYSTEM_TERRAIN_BLOCK_TYPE_ID
                ),
                "surfaceBlockTypeId": surface_block_type_id,
                "subsurfaceBlockTypeId": (
                    subsurface_block_type_id
                ),
                "systemBlockContract": (
                    get_flat_system_block_contract().to_dict()
                ),
            },
        )

    except (
        InvalidWorldDefinitionError,
        UnsupportedWorldTypeError,
        WorldValidationError,
    ):
        raise
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Flat world config validation failed unexpectedly.",
            details={
                "config": make_json_safe(raw_config),
                "errorType": type(exc).__name__,
                "error": str(exc),
            },
            cause=exc,
        ) from exc


@lru_cache(maxsize=VALIDATION_CACHE_SIZE)
def _validate_flat_world_config_cached_json(
    canonical_config: str,
) -> str:
    """
    Validiert kanonisches JSON und cached ausschließlich immutable JSON-Text.

    Dadurch erhält jeder Aufrufer später eine neu rekonstruierte Struktur und
    kann den Cache nicht durch Mutation beschädigen.
    """
    try:
        parsed = json.loads(canonical_config)
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Cached flat world config JSON could not be parsed.",
            details={
                "canonicalConfigHash": _validation_cache_key(
                    canonical_config
                ),
            },
            cause=exc,
        ) from exc

    if not isinstance(parsed, Mapping):
        raise InvalidWorldDefinitionError(
            "Cached flat world config root must be an object.",
            details={
                "canonicalConfigHash": _validation_cache_key(
                    canonical_config
                ),
                "rootType": type(parsed).__name__,
            },
        )

    result = _validate_flat_world_config_uncached(parsed)

    try:
        return json.dumps(
            result.to_dict(),
            sort_keys=True,
            separators=CANONICAL_JSON_SEPARATORS,
            ensure_ascii=False,
        )
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Validated flat world config could not be cached.",
            details={
                "canonicalConfigHash": _validation_cache_key(
                    canonical_config
                ),
            },
            cause=exc,
        ) from exc


def validate_flat_world_config_detailed(
    raw_config: Mapping[str, Any],
    *,
    use_cache: bool = True,
) -> FlatWorldValidationResult:
    """
    Validiert eine Flat-World-Konfiguration und gibt ein detailliertes Ergebnis
    zurück.

    Cache-Verhalten:
    - identische Eingabekonfigurationen werden pro Prozess wiederverwendet
    - gecacht wird serialisierter JSON-Text
    - Rückgaben sind immer neu rekonstruierte Objekte
    - Exceptions werden nicht gecacht
    """
    canonical = _canonical_json(raw_config)
    cache_key = _validation_cache_key(canonical)

    if not use_cache:
        result = _validate_flat_world_config_uncached(
            json.loads(canonical)
        )
        metadata = dict(result.metadata)
        metadata["cacheUsed"] = False
        metadata["validationCacheKey"] = cache_key

        return FlatWorldValidationResult(
            valid=result.valid,
            normalized_config=deepcopy(result.normalized_config),
            warnings=tuple(deepcopy(item) for item in result.warnings),
            metadata=metadata,
        )

    cached_json = _validate_flat_world_config_cached_json(canonical)

    try:
        payload = json.loads(cached_json)
    except Exception as exc:  # pragma: no cover - defensive cache guard
        _validate_flat_world_config_cached_json.cache_clear()
        raise InvalidWorldDefinitionError(
            "Cached flat world validation result could not be parsed.",
            details={
                "validationCacheKey": cache_key,
            },
            cause=exc,
        ) from exc

    if not isinstance(payload, Mapping):
        _validate_flat_world_config_cached_json.cache_clear()
        raise InvalidWorldDefinitionError(
            "Cached flat world validation result is invalid.",
            details={
                "validationCacheKey": cache_key,
                "resultType": type(payload).__name__,
            },
        )

    result = _result_from_dict(payload)
    metadata = dict(result.metadata)
    metadata["cacheUsed"] = True
    metadata["validationCacheKey"] = cache_key

    return FlatWorldValidationResult(
        valid=result.valid,
        normalized_config=deepcopy(result.normalized_config),
        warnings=tuple(deepcopy(item) for item in result.warnings),
        metadata=metadata,
    )


def validate_flat_world_config(
    raw_config: Mapping[str, Any],
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Validiert und normalisiert eine Flat-World-Konfiguration.

    Dies ist die wichtigste öffentliche Validator-Funktion für provider.py.
    """
    result = validate_flat_world_config_detailed(
        raw_config,
        use_cache=use_cache,
    )
    return deepcopy(result.normalized_config)


def create_validated_flat_world_definition(
    raw_config: Mapping[str, Any],
    *,
    use_cache: bool = True,
) -> WorldDefinition:
    """
    Validiert eine Flat-World-Konfiguration und erzeugt eine WorldDefinition.
    """
    normalized = validate_flat_world_config(
        raw_config,
        use_cache=use_cache,
    )

    try:
        definition = WorldDefinition.from_dict(normalized)
        definition.validate()
        return definition
    except (
        InvalidWorldDefinitionError,
        WorldValidationError,
    ):
        raise
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Validated flat world definition could not be created.",
            details={
                "worldId": normalized.get("worldId"),
                "generatorVersion": normalized.get(
                    "generatorVersion"
                ),
            },
            cause=exc,
        ) from exc


def get_flat_layer_block_type_ids(
    raw_config: Mapping[str, Any],
) -> tuple[str, str]:
    """
    Gibt Surface- und Subsurface-BlockTypeId zurück.

    Für die Standard-Flat-World müssen beide Werte system_terrain sein.
    """
    normalized = normalize_flat_world_config(raw_config)
    layers = _as_dict(
        normalized.get("layers"),
        field_name="layers",
    )

    surface_block_type_id = _safe_str(
        layers.get("surfaceBlockTypeId"),
        default=DEFAULT_SURFACE_BLOCK_TYPE_ID,
    )
    subsurface_block_type_id = _safe_str(
        layers.get("subsurfaceBlockTypeId"),
        default=DEFAULT_SUBSURFACE_BLOCK_TYPE_ID,
    )

    return surface_block_type_id, subsurface_block_type_id


def get_flat_validation_summary(
    raw_config: Mapping[str, Any],
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Gibt eine kompakte Validierungszusammenfassung für Diagnose und Tests zurück.
    """
    result = validate_flat_world_config_detailed(
        raw_config,
        use_cache=use_cache,
    )

    surface_block_type_id, subsurface_block_type_id = (
        get_flat_layer_block_type_ids(result.normalized_config)
    )

    return {
        "ok": result.valid,
        "validatorVersion": FLAT_VALIDATOR_VERSION,
        "validationContractVersion": FLAT_VALIDATION_CONTRACT_VERSION,
        "worldId": result.normalized_config.get("worldId"),
        "worldType": result.normalized_config.get("worldType"),
        "generatorType": result.normalized_config.get(
            "generatorType"
        ),
        "generatorVersion": result.normalized_config.get(
            "generatorVersion"
        ),
        "chunkSize": result.normalized_config.get("chunkSize"),
        "cellSize": result.normalized_config.get("cellSize"),
        "surfaceY": result.normalized_config.get("surfaceY"),
        "minY": result.normalized_config.get("minY"),
        "maxY": result.normalized_config.get("maxY"),
        "airSystemBlockId": SYSTEM_AIR_BLOCK_ID,
        "airCellValue": DEFAULT_AIR_CELL_VALUE,
        "terrainSystemBlockId": SYSTEM_TERRAIN_BLOCK_TYPE_ID,
        "surfaceBlockTypeId": surface_block_type_id,
        "subsurfaceBlockTypeId": subsurface_block_type_id,
        "paletteBlockTypeIds": list(
            extract_palette_block_type_ids(
                result.normalized_config
            )
        ),
        "positivePaletteSize": len(
            result.normalized_config.get("palette", [])
        ),
        "warnings": [deepcopy(item) for item in result.warnings],
        "cache": {
            "used": bool(result.metadata.get("cacheUsed")),
            "key": result.metadata.get("validationCacheKey"),
            "maxSize": VALIDATION_CACHE_SIZE,
            "info": get_flat_validator_cache_info(),
        },
    }


def get_flat_validator_cache_info() -> dict[str, Any]:
    """
    Gibt den aktuellen Validation-Cache-Status JSON-nah zurück.
    """
    try:
        info = _validate_flat_world_config_cached_json.cache_info()

        return {
            "hits": info.hits,
            "misses": info.misses,
            "maxSize": info.maxsize,
            "currentSize": info.currsize,
        }
    except Exception:
        return {
            "hits": 0,
            "misses": 0,
            "maxSize": VALIDATION_CACHE_SIZE,
            "currentSize": 0,
        }


def clear_flat_validator_caches() -> None:
    """
    Leert alle prozesslokalen Validator-Caches.

    Diese Funktion ist für Tests, Development-Reloads und explizite
    Diagnosepfade vorgesehen. Sie wird nicht automatisch beim Import ausgeführt.
    """
    _validate_flat_world_config_cached_json.cache_clear()
    get_flat_system_block_contract.cache_clear()
    get_expected_flat_palette_block_type_ids.cache_clear()
    get_expected_flat_layer_rule_contracts.cache_clear()


__all__ = (
    "FLAT_VALIDATOR_VERSION",
    "FLAT_VALIDATION_CONTRACT_VERSION",
    "EXPECTED_WORLD_SCHEMA_VERSION",
    "EXPECTED_WORLD_ID",
    "EXPECTED_WORLD_TYPE",
    "EXPECTED_WORLD_LABEL",
    "EXPECTED_GENERATOR_TYPE",
    "EXPECTED_GENERATOR_VERSION",
    "EXPECTED_PROJECTION_TYPE",
    "EXPECTED_TOPOLOGY_TYPE",
    "EXPECTED_COORDINATE_SYSTEM",
    "EXPECTED_CELL_ENCODING_VERSION",
    "EXPECTED_CELL_INDEXING_VERSION",
    "EXPECTED_CELL_INDEXING_ORDER",
    "EXPECTED_LAYERS_VERSION",
    "EXPECTED_REQUIRED_SYSTEM_BLOCKS_VERSION",
    "SYSTEM_AIR_BLOCK_ID",
    "SYSTEM_TERRAIN_BLOCK_TYPE_ID",
    "DEFAULT_SURFACE_BLOCK_TYPE_ID",
    "DEFAULT_SUBSURFACE_BLOCK_TYPE_ID",
    "DEFAULT_TERRAIN_BLOCK_TYPE_ID",
    "EXPECTED_POSITIVE_PALETTE_SIZE",
    "EXPECTED_TERRAIN_PALETTE_INDEX",
    "EXPECTED_TERRAIN_CELL_VALUE",
    "VALIDATION_CACHE_SIZE",
    "FlatSystemBlockContract",
    "FlatLayerRuleContract",
    "FlatWorldValidationResult",
    "get_flat_system_block_contract",
    "get_expected_flat_palette_block_type_ids",
    "get_expected_flat_layer_rule_contracts",
    "validate_required_top_level_fields",
    "validate_flat_identity",
    "validate_flat_dimensions",
    "validate_required_system_blocks",
    "extract_palette_block_type_ids",
    "validate_flat_palette",
    "validate_flat_layers",
    "validate_cell_encoding",
    "validate_cell_indexing",
    "validate_runtime_flags",
    "normalize_flat_world_config",
    "validate_flat_world_config_detailed",
    "validate_flat_world_config",
    "create_validated_flat_world_definition",
    "get_flat_layer_block_type_ids",
    "get_flat_validation_summary",
    "get_flat_validator_cache_info",
    "clear_flat_validator_caches",
)
