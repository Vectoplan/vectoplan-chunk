# src/world/flat/validator.py
"""
VECTOPLAN Flat World Validator.

Diese Datei enthält die spezifische Validierung für:

    src/world/flat/world.json

Sie prüft nur Regeln der flachen Welt.

Die neutrale, weltübergreifende Validierung liegt in:

    src/world/models.py
    → WorldDefinition.validate()

Die konkrete Flat-World-Validierung prüft zusätzlich:
- worldType == "flat"
- worldId == "flat"
- generatorType == "flat-world"
- projectionType == "flat-local-v1"
- topologyType == "flat-unbounded-v1"
- chunkSize/cellSize sind sinnvoll
- surfaceY liegt zwischen minY und maxY
- Palette ist vorhanden und eindeutig
- surfaceBlockTypeId existiert in der Palette
- subsurfaceBlockTypeId existiert in der Palette
- Air bleibt cellValue 0
- cellEncoding-Regel bleibt paletteIndex + 1
- cellIndexing-Regel bleibt x-fastest-y-then-z
- runtime-Konfiguration widerspricht Phase 1 nicht

Diese Datei liest keine Dateien.
Diese Datei generiert keine Chunks.
Diese Datei kennt keine Flask-Routes.
Diese Datei nutzt keine Datenbank.

Sie nimmt rohe JSON-Daten entgegen und gibt eine validierte,
leicht normalisierte Config zurück.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
# Constants
# ---------------------------------------------------------------------------

FLAT_VALIDATOR_VERSION: Final[str] = "0.1.0"

EXPECTED_WORLD_ID: Final[str] = "flat"
EXPECTED_WORLD_TYPE: Final[str] = "flat"

EXPECTED_GENERATOR_TYPE: Final[str] = "flat-world"
EXPECTED_GENERATOR_VERSION: Final[str] = "1"

EXPECTED_PROJECTION_TYPE: Final[str] = "flat-local-v1"
EXPECTED_TOPOLOGY_TYPE: Final[str] = "flat-unbounded-v1"
EXPECTED_COORDINATE_SYSTEM: Final[str] = "vectoplan-world-y-up-v1"

EXPECTED_CELL_ENCODING_VERSION: Final[str] = "cell-encoding.palette-index-plus-one.v1"
EXPECTED_CELL_INDEXING_VERSION: Final[str] = "cell-indexing.x-fastest-y-then-z.v1"
EXPECTED_CELL_INDEXING_ORDER: Final[str] = DEFAULT_CELL_INDEX_ORDER

EXPECTED_LAYERS_VERSION: Final[str] = "flat-layers.v1"

DEFAULT_SURFACE_BLOCK_TYPE_ID: Final[str] = "debug_grass"
DEFAULT_SUBSURFACE_BLOCK_TYPE_ID: Final[str] = "debug_dirt"

REQUIRED_TOP_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "worldId",
    "worldType",
    "generatorType",
    "chunkSize",
    "cellSize",
    "surfaceY",
    "minY",
    "maxY",
    "palette",
    "layers",
)

REQUIRED_LAYER_FIELDS: Final[tuple[str, ...]] = (
    "surfaceBlockTypeId",
    "subsurfaceBlockTypeId",
)

OPTIONAL_BOOLEAN_RUNTIME_FIELDS: Final[dict[str, bool]] = {
    "materializeUnchangedChunks": False,
    "supportsSnapshots": False,
    "supportsEvents": False,
    "supportsCommands": False,
    "supportsBatchChunks": True,
    "supportsNegativeChunkCoordinates": True,
}


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


def _get_field(raw: Mapping[str, Any], camel_key: str, snake_key: str | None = None, default: Any = None) -> Any:
    """
    Liest ein Feld tolerant aus camelCase oder optional snake_case.
    """
    if camel_key in raw:
        return raw.get(camel_key)

    if snake_key and snake_key in raw:
        return raw.get(snake_key)

    return default


def _require_field(raw: Mapping[str, Any], key: str) -> Any:
    """
    Prüft, ob ein Pflichtfeld existiert und nicht leer ist.
    """
    value = raw.get(key)

    if value is None:
        raise InvalidWorldDefinitionError(
            f"Required field '{key}' is missing.",
            details={"field": key},
        )

    if isinstance(value, str) and not value.strip():
        raise InvalidWorldDefinitionError(
            f"Required field '{key}' must not be empty.",
            details={"field": key},
        )

    return value


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


def _dedupe_preserve_order(values: Sequence[str]) -> tuple[str, ...]:
    """
    Entfernt Duplikate und erhält die Reihenfolge.
    """
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        text = _safe_str(value)

        if not text:
            continue

        if text in seen:
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


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlatWorldValidationResult:
    """
    Ergebnis der Flat-World-Validierung.

    Diese Struktur wird primär intern und für Tests verwendet.
    validate_flat_world_config gibt standardmäßig nur normalized_config zurück,
    weil der Loader mit Mapping-Rückgaben arbeitet.
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
            "warnings": list(self.warnings),
            "metadata": self.metadata,
        }


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
    Prüft Identität und Typ der flachen Welt.
    """
    world_id = _safe_str(_get_field(raw_config, "worldId", "world_id"))
    world_type = _safe_str(_get_field(raw_config, "worldType", "world_type"))
    generator_type = _safe_str(_get_field(raw_config, "generatorType", "generator_type"))
    projection_type = _safe_str(_get_field(raw_config, "projectionType", "projection_type"), default=EXPECTED_PROJECTION_TYPE)
    topology_type = _safe_str(_get_field(raw_config, "topologyType", "topology_type"), default=EXPECTED_TOPOLOGY_TYPE)
    coordinate_system = _safe_str(_get_field(raw_config, "coordinateSystem", "coordinate_system"), default=EXPECTED_COORDINATE_SYSTEM)

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
            message=f"Flat world generatorType must be '{EXPECTED_GENERATOR_TYPE}'.",
            field_name="generatorType",
            actual=generator_type,
            expected=EXPECTED_GENERATOR_TYPE,
        )

    if projection_type != EXPECTED_PROJECTION_TYPE:
        _append_error(
            errors,
            code="invalid_projection_type",
            message=f"Flat world projectionType must be '{EXPECTED_PROJECTION_TYPE}'.",
            field_name="projectionType",
            actual=projection_type,
            expected=EXPECTED_PROJECTION_TYPE,
        )

    if topology_type != EXPECTED_TOPOLOGY_TYPE:
        _append_error(
            errors,
            code="invalid_topology_type",
            message=f"Flat world topologyType must be '{EXPECTED_TOPOLOGY_TYPE}'.",
            field_name="topologyType",
            actual=topology_type,
            expected=EXPECTED_TOPOLOGY_TYPE,
        )

    if coordinate_system != EXPECTED_COORDINATE_SYSTEM:
        _append_error(
            errors,
            code="invalid_coordinate_system",
            message=f"Flat world coordinateSystem must be '{EXPECTED_COORDINATE_SYSTEM}'.",
            field_name="coordinateSystem",
            actual=coordinate_system,
            expected=EXPECTED_COORDINATE_SYSTEM,
        )

    if errors:
        raise InvalidWorldDefinitionError(
            "Flat world identity validation failed.",
            details={
                "worldId": world_id,
                "worldType": world_type,
                "errors": errors,
            },
        )


def validate_flat_dimensions(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft chunkSize, cellSize, surfaceY, minY und maxY.
    """
    chunk_size = _to_int(_get_field(raw_config, "chunkSize", "chunk_size"), field_name="chunkSize")
    cell_size = _to_float(_get_field(raw_config, "cellSize", "cell_size"), field_name="cellSize")
    surface_y = _to_int(_get_field(raw_config, "surfaceY", "surface_y"), field_name="surfaceY")
    min_y = _to_int(_get_field(raw_config, "minY", "min_y"), field_name="minY")
    max_y = _to_int(_get_field(raw_config, "maxY", "max_y"), field_name="maxY")

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

    if errors:
        raise InvalidWorldDefinitionError(
            "Flat world dimension validation failed.",
            details={
                "errors": errors,
                "chunkSize": chunk_size,
                "cellSize": cell_size,
                "surfaceY": surface_y,
                "minY": min_y,
                "maxY": max_y,
            },
        )


def extract_palette_block_type_ids(raw_config: Mapping[str, Any]) -> tuple[str, ...]:
    """
    Extrahiert alle blockTypeIds aus der Palette.
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
            message="Flat world palette must contain at least one block type.",
            field_name="palette",
        )

    if errors:
        raise InvalidWorldDefinitionError(
            "Flat world palette validation failed.",
            details={
                "errors": errors,
                "blockTypeIds": block_type_ids,
            },
        )

    return tuple(block_type_ids)


def validate_flat_palette(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft die Palette und Registry-Angaben.
    """
    palette = _as_list(raw_config.get("palette"), field_name="palette")
    block_registry_id = _safe_str(
        _get_field(raw_config, "blockRegistryId", "block_registry_id"),
        default=DEFAULT_BLOCK_REGISTRY_ID,
    )
    block_registry_version = _safe_str(
        _get_field(raw_config, "blockRegistryVersion", "block_registry_version"),
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

        block_type_id = _safe_str(entry.get("blockTypeId") or entry.get("block_type_id"))
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

        if not label:
            _append_error(
                errors,
                code="missing_block_label",
                message="Palette entry label must not be empty.",
                field_name="palette.label",
                index=index,
                blockTypeId=block_type_id,
            )

        for boolean_field in ("solid", "placeable", "breakable"):
            if boolean_field in entry and not isinstance(entry.get(boolean_field), bool):
                value = entry.get(boolean_field)

                if _safe_str(value).lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                    _append_error(
                        errors,
                        code="invalid_palette_boolean",
                        message=f"Palette field '{boolean_field}' must be boolean-like.",
                        field_name=f"palette.{boolean_field}",
                        index=index,
                        blockTypeId=block_type_id,
                        value=value,
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
                message="Palette entry registryId should match world blockRegistryId in phase 1.",
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
                message="Palette entry registryVersion should match world blockRegistryVersion in phase 1.",
                field_name="palette.registryVersion",
                index=index,
                blockTypeId=block_type_id,
                entryRegistryVersion=entry_registry_version,
                worldRegistryVersion=block_registry_version,
            )

    if errors:
        raise InvalidWorldDefinitionError(
            "Flat world palette validation failed.",
            details={
                "errors": errors,
                "paletteSize": len(palette),
                "blockRegistryId": block_registry_id,
                "blockRegistryVersion": block_registry_version,
            },
        )


def validate_flat_layers(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft die Layer-Regeln der flachen Welt.
    """
    layers = _as_dict(raw_config.get("layers"), field_name="layers")
    block_type_ids = extract_palette_block_type_ids(raw_config)

    errors: list[dict[str, Any]] = []

    for field_name in REQUIRED_LAYER_FIELDS:
        value = _safe_str(layers.get(field_name))

        if not value:
            _append_error(
                errors,
                code="missing_layer_field",
                message=f"layers.{field_name} is required.",
                field_name=f"layers.{field_name}",
            )

    surface_block_type_id = _safe_str(
        layers.get("surfaceBlockTypeId"),
        default=DEFAULT_SURFACE_BLOCK_TYPE_ID,
    )
    subsurface_block_type_id = _safe_str(
        layers.get("subsurfaceBlockTypeId"),
        default=DEFAULT_SUBSURFACE_BLOCK_TYPE_ID,
    )

    if surface_block_type_id and surface_block_type_id not in block_type_ids:
        _append_error(
            errors,
            code="surface_block_not_in_palette",
            message="layers.surfaceBlockTypeId must exist in palette.",
            field_name="layers.surfaceBlockTypeId",
            blockTypeId=surface_block_type_id,
            availableBlockTypeIds=block_type_ids,
        )

    if subsurface_block_type_id and subsurface_block_type_id not in block_type_ids:
        _append_error(
            errors,
            code="subsurface_block_not_in_palette",
            message="layers.subsurfaceBlockTypeId must exist in palette.",
            field_name="layers.subsurfaceBlockTypeId",
            blockTypeId=subsurface_block_type_id,
            availableBlockTypeIds=block_type_ids,
        )

    air_block_value = _to_int(
        layers.get("airBlockValue", DEFAULT_AIR_CELL_VALUE),
        field_name="layers.airBlockValue",
        default=DEFAULT_AIR_CELL_VALUE,
    )

    if air_block_value != DEFAULT_AIR_CELL_VALUE:
        _append_error(
            errors,
            code="invalid_air_block_value",
            message=f"layers.airBlockValue must be {DEFAULT_AIR_CELL_VALUE}.",
            field_name="layers.airBlockValue",
            actual=air_block_value,
            expected=DEFAULT_AIR_CELL_VALUE,
        )

    version = _safe_str(layers.get("version"), default=EXPECTED_LAYERS_VERSION)

    if version != EXPECTED_LAYERS_VERSION:
        _append_error(
            errors,
            code="invalid_layers_version",
            message=f"layers.version must be '{EXPECTED_LAYERS_VERSION}'.",
            field_name="layers.version",
            actual=version,
            expected=EXPECTED_LAYERS_VERSION,
        )

    if "rules" in layers:
        rules = _as_list(layers.get("rules"), field_name="layers.rules")

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

            block_type_id = rule.get("blockTypeId")

            if block_type_id is not None:
                block_type_id_text = _safe_str(block_type_id)

                if block_type_id_text and block_type_id_text not in block_type_ids:
                    _append_error(
                        errors,
                        code="layer_rule_block_not_in_palette",
                        message="Layer rule blockTypeId must exist in palette.",
                        field_name="layers.rules.blockTypeId",
                        index=index,
                        blockTypeId=block_type_id_text,
                        availableBlockTypeIds=block_type_ids,
                    )

            if "cellValue" in rule:
                cell_value = _to_int(
                    rule.get("cellValue"),
                    field_name="layers.rules.cellValue",
                    default=DEFAULT_AIR_CELL_VALUE,
                )

                if cell_value < 0:
                    _append_error(
                        errors,
                        code="negative_layer_rule_cell_value",
                        message="Layer rule cellValue must be >= 0.",
                        field_name="layers.rules.cellValue",
                        index=index,
                        cellValue=cell_value,
                    )

    if errors:
        raise InvalidWorldDefinitionError(
            "Flat world layer validation failed.",
            details={
                "errors": errors,
                "layers": make_json_safe(layers),
            },
        )


def validate_cell_encoding(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft die cellEncoding-Sektion, falls vorhanden.
    """
    if "cellEncoding" not in raw_config:
        return

    cell_encoding = _as_dict(raw_config.get("cellEncoding"), field_name="cellEncoding")

    version = _safe_str(
        cell_encoding.get("version"),
        default=EXPECTED_CELL_ENCODING_VERSION,
    )
    air_cell_value = _to_int(
        cell_encoding.get("airCellValue", DEFAULT_AIR_CELL_VALUE),
        field_name="cellEncoding.airCellValue",
        default=DEFAULT_AIR_CELL_VALUE,
    )
    block_cell_value_rule = _safe_str(
        cell_encoding.get("blockCellValueRule"),
        default="paletteIndex + 1",
    )

    errors: list[dict[str, Any]] = []

    if version != EXPECTED_CELL_ENCODING_VERSION:
        _append_error(
            errors,
            code="invalid_cell_encoding_version",
            message=f"cellEncoding.version must be '{EXPECTED_CELL_ENCODING_VERSION}'.",
            field_name="cellEncoding.version",
            actual=version,
            expected=EXPECTED_CELL_ENCODING_VERSION,
        )

    if air_cell_value != DEFAULT_AIR_CELL_VALUE:
        _append_error(
            errors,
            code="invalid_air_cell_value",
            message=f"cellEncoding.airCellValue must be {DEFAULT_AIR_CELL_VALUE}.",
            field_name="cellEncoding.airCellValue",
            actual=air_cell_value,
            expected=DEFAULT_AIR_CELL_VALUE,
        )

    normalized_rule = block_cell_value_rule.replace(" ", "").lower()

    if normalized_rule not in {"paletteindex+1", "palette_index+1"}:
        _append_error(
            errors,
            code="invalid_block_cell_value_rule",
            message="cellEncoding.blockCellValueRule must be 'paletteIndex + 1'.",
            field_name="cellEncoding.blockCellValueRule",
            actual=block_cell_value_rule,
            expected="paletteIndex + 1",
        )

    if errors:
        raise InvalidWorldDefinitionError(
            "Flat world cell encoding validation failed.",
            details={
                "errors": errors,
                "cellEncoding": make_json_safe(cell_encoding),
            },
        )


def validate_cell_indexing(raw_config: Mapping[str, Any]) -> None:
    """
    Prüft die cellIndexing-Sektion, falls vorhanden.
    """
    if "cellIndexing" not in raw_config:
        return

    cell_indexing = _as_dict(raw_config.get("cellIndexing"), field_name="cellIndexing")

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
            message=f"cellIndexing.version must be '{EXPECTED_CELL_INDEXING_VERSION}'.",
            field_name="cellIndexing.version",
            actual=version,
            expected=EXPECTED_CELL_INDEXING_VERSION,
        )

    if order != EXPECTED_CELL_INDEXING_ORDER:
        _append_error(
            errors,
            code="invalid_cell_indexing_order",
            message=f"cellIndexing.order must be '{EXPECTED_CELL_INDEXING_ORDER}'.",
            field_name="cellIndexing.order",
            actual=order,
            expected=EXPECTED_CELL_INDEXING_ORDER,
        )

    if errors:
        raise InvalidWorldDefinitionError(
            "Flat world cell indexing validation failed.",
            details={
                "errors": errors,
                "cellIndexing": make_json_safe(cell_indexing),
            },
        )


def validate_runtime_flags(raw_config: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """
    Prüft runtime-Flags.

    Für Phase 1 sind Snapshots, Events und Commands in dieser reinen
    World-Generierung noch nicht aktiv. Diese Funktion gibt Warnungen zurück,
    wenn runtime-Flags davon abweichen.
    """
    if "runtime" not in raw_config:
        return tuple()

    runtime = _as_dict(raw_config.get("runtime"), field_name="runtime")
    warnings: list[dict[str, Any]] = []

    for field_name, expected_value in OPTIONAL_BOOLEAN_RUNTIME_FIELDS.items():
        if field_name not in runtime:
            continue

        actual_value = _to_bool(runtime.get(field_name), default=expected_value)

        if actual_value != expected_value:
            warnings.append(
                {
                    "code": "unexpected_runtime_flag",
                    "field": f"runtime.{field_name}",
                    "expected": expected_value,
                    "actual": actual_value,
                    "message": (
                        f"runtime.{field_name} is {actual_value}, "
                        f"but Phase 1 expects {expected_value}."
                    ),
                }
            )

    return tuple(warnings)


def normalize_flat_world_config(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    """
    Normalisiert eine Flat-World-Konfiguration.

    Diese Funktion ergänzt defensive Defaults, ohne die fachlichen Regeln
    der Welt zu ändern.
    """
    if not isinstance(raw_config, Mapping):
        raise InvalidWorldDefinitionError(
            "Flat world config must be an object.",
            details={
                "configType": type(raw_config).__name__,
                "config": make_json_safe(raw_config),
            },
        )

    normalized = dict(raw_config)

    normalized.setdefault("schemaVersion", "world.schema.v1")
    normalized.setdefault("worldId", EXPECTED_WORLD_ID)
    normalized.setdefault("worldType", EXPECTED_WORLD_TYPE)
    normalized.setdefault("label", "Flat Debug World")
    normalized.setdefault("status", "development")

    normalized.setdefault("generatorType", EXPECTED_GENERATOR_TYPE)
    normalized.setdefault("generatorVersion", EXPECTED_GENERATOR_VERSION)

    normalized.setdefault("coordinateSystem", EXPECTED_COORDINATE_SYSTEM)
    normalized.setdefault("projectionType", EXPECTED_PROJECTION_TYPE)
    normalized.setdefault("topologyType", EXPECTED_TOPOLOGY_TYPE)

    normalized.setdefault("blockRegistryId", DEFAULT_BLOCK_REGISTRY_ID)
    normalized.setdefault("blockRegistryVersion", DEFAULT_BLOCK_REGISTRY_VERSION)

    if "chunkSize" in normalized:
        normalized["chunkSize"] = _to_int(normalized.get("chunkSize"), field_name="chunkSize")
    else:
        normalized["chunkSize"] = 16

    if "cellSize" in normalized:
        normalized["cellSize"] = _to_float(normalized.get("cellSize"), field_name="cellSize")
    else:
        normalized["cellSize"] = 1.0

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

    if "metadata" in normalized:
        normalized["metadata"] = _normalize_metadata(normalized.get("metadata"))
    else:
        normalized["metadata"] = {}

    layers = _as_dict(normalized.get("layers", {}), field_name="layers")
    layers.setdefault("version", EXPECTED_LAYERS_VERSION)
    layers.setdefault("airBlockValue", DEFAULT_AIR_CELL_VALUE)
    layers.setdefault("surfaceBlockTypeId", DEFAULT_SURFACE_BLOCK_TYPE_ID)
    layers.setdefault("subsurfaceBlockTypeId", DEFAULT_SUBSURFACE_BLOCK_TYPE_ID)
    normalized["layers"] = layers

    if "cellEncoding" in normalized:
        cell_encoding = _as_dict(normalized.get("cellEncoding"), field_name="cellEncoding")
    else:
        cell_encoding = {}

    cell_encoding.setdefault("version", EXPECTED_CELL_ENCODING_VERSION)
    cell_encoding.setdefault("airCellValue", DEFAULT_AIR_CELL_VALUE)
    cell_encoding.setdefault("blockCellValueRule", "paletteIndex + 1")
    normalized["cellEncoding"] = cell_encoding

    if "cellIndexing" in normalized:
        cell_indexing = _as_dict(normalized.get("cellIndexing"), field_name="cellIndexing")
    else:
        cell_indexing = {}

    cell_indexing.setdefault("version", EXPECTED_CELL_INDEXING_VERSION)
    cell_indexing.setdefault("order", EXPECTED_CELL_INDEXING_ORDER)
    cell_indexing.setdefault(
        "formula",
        "index = localX + chunkSize * (localY + chunkSize * localZ)",
    )
    normalized["cellIndexing"] = cell_indexing

    runtime = _as_dict(normalized.get("runtime", {}), field_name="runtime")

    for field_name, expected_value in OPTIONAL_BOOLEAN_RUNTIME_FIELDS.items():
        runtime.setdefault(field_name, expected_value)

    runtime.setdefault("source", "generated")
    normalized["runtime"] = runtime

    return normalized


def validate_flat_world_config_detailed(
    raw_config: Mapping[str, Any],
) -> FlatWorldValidationResult:
    """
    Validiert eine Flat-World-Konfiguration und gibt ein detailliertes Ergebnis
    zurück.

    Diese Funktion ist für Tests und Diagnose sinnvoll.
    Der Provider kann stattdessen validate_flat_world_config verwenden.
    """
    try:
        normalized = normalize_flat_world_config(raw_config)

        validate_required_top_level_fields(normalized)
        validate_flat_identity(normalized)
        validate_flat_dimensions(normalized)
        validate_flat_palette(normalized)
        validate_flat_layers(normalized)
        validate_cell_encoding(normalized)
        validate_cell_indexing(normalized)

        warnings = validate_runtime_flags(normalized)

        # Weltübergreifende Strukturvalidierung zusätzlich ausführen.
        definition = WorldDefinition.from_dict(normalized)
        definition.validate()

        return FlatWorldValidationResult(
            valid=True,
            normalized_config=normalized,
            warnings=warnings,
            metadata={
                "validatorVersion": FLAT_VALIDATOR_VERSION,
                "worldId": definition.world_id,
                "worldType": definition.world_type,
                "generatorType": definition.generator_type,
                "paletteBlockTypeIds": definition.palette_block_type_ids,
            },
        )

    except (InvalidWorldDefinitionError, UnsupportedWorldTypeError, WorldValidationError):
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


def validate_flat_world_config(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    """
    Validiert eine Flat-World-Konfiguration und gibt eine normalisierte
    Config zurück.

    Diese Funktion ist die wichtigste öffentliche Validator-Funktion für
    src/world/flat/provider.py.

    Rückgabe:
        dict[str, Any]

    Warum kein bool?
        Der WorldLoader kann Mapping-Rückgaben aus Provider-Validatoren direkt
        weiterverwenden. So kommen normalisierte Defaults kontrolliert in die
        weitere Pipeline.
    """
    result = validate_flat_world_config_detailed(raw_config)
    return result.normalized_config


def create_validated_flat_world_definition(raw_config: Mapping[str, Any]) -> WorldDefinition:
    """
    Validiert eine Flat-World-Konfiguration und erzeugt daraus eine
    WorldDefinition.

    Diese Funktion ist für provider.py vorbereitet.
    """
    normalized = validate_flat_world_config(raw_config)
    definition = WorldDefinition.from_dict(normalized)
    definition.validate()
    return definition


def get_flat_layer_block_type_ids(raw_config: Mapping[str, Any]) -> tuple[str, str]:
    """
    Gibt surfaceBlockTypeId und subsurfaceBlockTypeId aus einer validierten
    oder rohen Flat-Config zurück.

    Rückgabe:
        (surface_block_type_id, subsurface_block_type_id)
    """
    normalized = normalize_flat_world_config(raw_config)
    layers = _as_dict(normalized.get("layers"), field_name="layers")

    surface_block_type_id = _safe_str(
        layers.get("surfaceBlockTypeId"),
        default=DEFAULT_SURFACE_BLOCK_TYPE_ID,
    )
    subsurface_block_type_id = _safe_str(
        layers.get("subsurfaceBlockTypeId"),
        default=DEFAULT_SUBSURFACE_BLOCK_TYPE_ID,
    )

    return surface_block_type_id, subsurface_block_type_id


def get_flat_validation_summary(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    """
    Gibt eine kompakte Validierungszusammenfassung zurück.

    Diese Funktion ist für Tests, Debugging oder spätere Diagnose-Routen
    gedacht.
    """
    result = validate_flat_world_config_detailed(raw_config)

    return {
        "ok": result.valid,
        "validatorVersion": FLAT_VALIDATOR_VERSION,
        "worldId": result.normalized_config.get("worldId"),
        "worldType": result.normalized_config.get("worldType"),
        "generatorType": result.normalized_config.get("generatorType"),
        "chunkSize": result.normalized_config.get("chunkSize"),
        "cellSize": result.normalized_config.get("cellSize"),
        "surfaceY": result.normalized_config.get("surfaceY"),
        "minY": result.normalized_config.get("minY"),
        "maxY": result.normalized_config.get("maxY"),
        "paletteBlockTypeIds": list(extract_palette_block_type_ids(result.normalized_config)),
        "warnings": list(result.warnings),
    }


__all__ = (
    "FLAT_VALIDATOR_VERSION",
    "EXPECTED_WORLD_ID",
    "EXPECTED_WORLD_TYPE",
    "EXPECTED_GENERATOR_TYPE",
    "EXPECTED_GENERATOR_VERSION",
    "EXPECTED_PROJECTION_TYPE",
    "EXPECTED_TOPOLOGY_TYPE",
    "EXPECTED_COORDINATE_SYSTEM",
    "EXPECTED_CELL_ENCODING_VERSION",
    "EXPECTED_CELL_INDEXING_VERSION",
    "EXPECTED_CELL_INDEXING_ORDER",
    "EXPECTED_LAYERS_VERSION",
    "DEFAULT_SURFACE_BLOCK_TYPE_ID",
    "DEFAULT_SUBSURFACE_BLOCK_TYPE_ID",
    "FlatWorldValidationResult",
    "validate_required_top_level_fields",
    "validate_flat_identity",
    "validate_flat_dimensions",
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
)