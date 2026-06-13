# src/world/models.py
"""
VECTOPLAN World Models.

Diese Datei enthält framework-neutrale Datenmodelle für die World-Schicht.

Wichtig:
- Keine Flask-Abhängigkeit.
- Keine SQLAlchemy-Abhängigkeit.
- Keine Datenbankmodelle.
- Keine konkrete Flat-World-Logik.
- Keine Three.js-Objekte.
- Nur stabile Python-Modelle für Weltdefinitionen, Chunk-Anfragen,
  Paletteneinträge und generierte Chunk-Daten.

Die Modelle dienen als interne Normalisierungsschicht zwischen:

    world.json
    → Provider / Validator / Generator
    → WorldService
    → Serializer
    → spätere API-Route
    → Editor

Grundregel:
- world.json ist Input.
- Diese Dataclasses sind interne Arbeitsmodelle.
- serializer.py erzeugt später die finale API-/Editor-JSON-Form.

Die konkreten Welten wie src/world/flat dürfen diese Modelle verwenden,
sollen ihre eigene Generatorlogik aber in ihrem Provider-/Generator-Ordner
behalten.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Final

try:
    from src.world.errors import (
        InvalidChunkRequestError,
        InvalidWorldDefinitionError,
        WorldValidationError,
        make_json_safe,
    )
except Exception:  # pragma: no cover - defensive fallback for early bootstrap
    InvalidChunkRequestError = ValueError  # type: ignore[assignment]
    InvalidWorldDefinitionError = ValueError  # type: ignore[assignment]
    WorldValidationError = ValueError  # type: ignore[assignment]

    def make_json_safe(value: Any, *, depth: int = 0) -> Any:  # type: ignore[no-redef]
        return value


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORLD_MODELS_VERSION: Final[str] = "0.1.1"

DEFAULT_WORLD_SCHEMA_VERSION: Final[str] = "world.schema.v1"
DEFAULT_CHUNK_SCHEMA_VERSION: Final[str] = "chunk.schema.v1"

DEFAULT_BLOCK_REGISTRY_ID: Final[str] = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION: Final[str] = "1"

DEFAULT_COORDINATE_SYSTEM: Final[str] = "vectoplan-world-y-up-v1"
DEFAULT_PROJECTION_TYPE: Final[str] = "flat-local-v1"
DEFAULT_TOPOLOGY_TYPE: Final[str] = "flat-unbounded-v1"

DEFAULT_CELL_INDEX_ORDER: Final[str] = "x-fastest-y-then-z"
DEFAULT_AIR_CELL_VALUE: Final[int] = 0

MIN_CHUNK_SIZE: Final[int] = 1
MAX_REASONABLE_CHUNK_SIZE: Final[int] = 128

MIN_CELL_SIZE: Final[float] = 0.000001
MAX_REASONABLE_CELL_SIZE: Final[float] = 1_000_000.0

MAX_PALETTE_SIZE: Final[int] = 65_535
MAX_METADATA_DEPTH: Final[int] = 8


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt einen beliebigen Wert robust in einen String um.
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
    Wandelt einen Wert in einen optionalen String um.
    Leere Strings werden zu None.
    """
    text = _safe_str(value)
    return text or None


def _required_str(
    value: Any,
    *,
    field_name: str,
    context: Mapping[str, Any] | None = None,
) -> str:
    """
    Liest ein Pflicht-Stringfeld.
    """
    text = _safe_str(value)

    if text:
        return text

    details = {"field": field_name}

    if context:
        details.update(dict(context))

    raise InvalidWorldDefinitionError(
        f"Required world field '{field_name}' is missing or empty.",
        details=details,
    )


def _to_int(
    value: Any,
    *,
    field_name: str,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
    error_cls: type[Exception] = InvalidWorldDefinitionError,
) -> int:
    """
    Wandelt einen Wert defensiv in int um und prüft optionale Grenzen.
    """
    if value is None:
        if default is not None:
            value = default
        else:
            raise error_cls(
                f"Required integer field '{field_name}' is missing.",
                details={"field": field_name},
            )

    try:
        converted = int(value)
    except Exception as exc:
        raise error_cls(
            f"Field '{field_name}' must be an integer.",
            details={"field": field_name, "value": make_json_safe(value)},
        ) from exc

    if minimum is not None and converted < minimum:
        raise error_cls(
            f"Field '{field_name}' must be >= {minimum}.",
            details={"field": field_name, "value": converted, "minimum": minimum},
        )

    if maximum is not None and converted > maximum:
        raise error_cls(
            f"Field '{field_name}' must be <= {maximum}.",
            details={"field": field_name, "value": converted, "maximum": maximum},
        )

    return converted


def _to_float(
    value: Any,
    *,
    field_name: str,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    error_cls: type[Exception] = InvalidWorldDefinitionError,
) -> float:
    """
    Wandelt einen Wert defensiv in float um und prüft optionale Grenzen.
    """
    if value is None:
        if default is not None:
            value = default
        else:
            raise error_cls(
                f"Required numeric field '{field_name}' is missing.",
                details={"field": field_name},
            )

    try:
        converted = float(value)
    except Exception as exc:
        raise error_cls(
            f"Field '{field_name}' must be numeric.",
            details={"field": field_name, "value": make_json_safe(value)},
        ) from exc

    if minimum is not None and converted < minimum:
        raise error_cls(
            f"Field '{field_name}' must be >= {minimum}.",
            details={"field": field_name, "value": converted, "minimum": minimum},
        )

    if maximum is not None and converted > maximum:
        raise error_cls(
            f"Field '{field_name}' must be <= {maximum}.",
            details={"field": field_name, "value": converted, "maximum": maximum},
        )

    return converted


def _to_bool(value: Any, *, default: bool = False) -> bool:
    """
    Wandelt typische JSON-/ENV-/String-Werte robust in bool um.
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


def _to_mapping(
    value: Any,
    *,
    field_name: str,
    default: Mapping[str, Any] | None = None,
    error_cls: type[Exception] = InvalidWorldDefinitionError,
) -> dict[str, Any]:
    """
    Wandelt einen Wert robust in ein Dictionary um.
    """
    if value is None:
        return dict(default or {})

    if isinstance(value, Mapping):
        return dict(value)

    raise error_cls(
        f"Field '{field_name}' must be an object.",
        details={"field": field_name, "value": make_json_safe(value)},
    )


def _to_sequence(
    value: Any,
    *,
    field_name: str,
    default: Sequence[Any] | None = None,
    error_cls: type[Exception] = InvalidWorldDefinitionError,
) -> list[Any]:
    """
    Wandelt einen Wert robust in eine Liste um.
    """
    if value is None:
        return list(default or [])

    if isinstance(value, list | tuple):
        return list(value)

    raise error_cls(
        f"Field '{field_name}' must be an array.",
        details={"field": field_name, "value": make_json_safe(value)},
    )


def _clean_metadata(value: Any) -> dict[str, Any]:
    """
    Normalisiert beliebige Metadaten auf ein JSON-sicheres Dictionary.
    """
    safe_value = make_json_safe(value)

    if isinstance(safe_value, Mapping):
        return dict(safe_value)

    if safe_value in (None, ""):
        return {}

    return {"value": safe_value}


def _camel_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Konvertiert bekannte snake_case-Schlüssel in camelCase.

    Diese Funktion ist bewusst explizit statt generisch-magisch, damit
    versehentliche Umbenennungen vermieden werden.

    Wichtig:
    PaletteEntry nutzt intern registry_id / registry_version.
    Nach außen müssen diese Felder als registryId / registryVersion erscheinen,
    damit die API-/Editor-Payloads konsistent sind.
    """
    mapping = {
        "world_id": "worldId",
        "world_type": "worldType",
        "schema_version": "schemaVersion",
        "generator_type": "generatorType",
        "generator_version": "generatorVersion",
        "chunk_size": "chunkSize",
        "cell_size": "cellSize",
        "coordinate_system": "coordinateSystem",
        "projection_type": "projectionType",
        "topology_type": "topologyType",
        "surface_y": "surfaceY",
        "min_y": "minY",
        "max_y": "maxY",
        "seed": "seed",
        "block_registry_id": "blockRegistryId",
        "block_registry_version": "blockRegistryVersion",
        "block_type_id": "blockTypeId",
        "registry_id": "registryId",
        "registry_version": "registryVersion",
        "metadata_json": "metadata",
        "provider_id": "providerId",
        "provider_module": "providerModule",
        "config_path": "configPath",
        "supports_chunk_generation": "supportsChunkGeneration",
        "supports_world_metadata": "supportsWorldMetadata",
        "chunk_x": "chunkX",
        "chunk_y": "chunkY",
        "chunk_z": "chunkZ",
        "chunk_key": "chunkKey",
        "cell_index_order": "cellIndexOrder",
        "air_cell_value": "airCellValue",
        "content_hash": "contentHash",
        "chunk_version": "chunkVersion",
        "request_id": "requestId",
        "include_metadata": "includeMetadata",
        "default_world_id": "defaultWorldId",
        "raw_config": "rawConfig",
    }

    result: dict[str, Any] = {}

    for key, value in data.items():
        result[mapping.get(key, key)] = value

    return result


def _without_none(data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Entfernt None-Werte aus einem Dictionary.
    """
    return {key: value for key, value in data.items() if value is not None}


def build_chunk_key(chunk_x: int, chunk_y: int, chunk_z: int) -> str:
    """
    Baut einen stabilen Chunk-Key.

    Diese einfache Form ist bewusst kompatibel mit dem bisherigen Entwurf:

        "0:0:0"
        "-1:0:0"

    Später kann diese Funktion in src/coordinates/chunk_keys.py gespiegelt
    oder dorthin verschoben werden. Für den ersten World-Slice bleibt sie hier,
    damit GeneratedChunk ohne zusätzliche Abhängigkeit konsistent bleibt.
    """
    return f"{chunk_x}:{chunk_y}:{chunk_z}"


def calculate_cell_count(chunk_size: int) -> int:
    """
    Berechnet die Anzahl der Zellen eines kubischen Chunks.
    """
    size = _to_int(
        chunk_size,
        field_name="chunkSize",
        minimum=MIN_CHUNK_SIZE,
        maximum=MAX_REASONABLE_CHUNK_SIZE,
    )

    return size * size * size


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PaletteEntry:
    """
    Ein Eintrag in der Chunk-Palette.

    Encoding-Invariante:
        cellValue = 0
            → Air

        cellValue = paletteIndex + 1
            → Block an diesem Palette-Index

    Beispiel:
        palette[0].block_type_id = "debug_grass"
        cellValue = 1
            → debug_grass
    """

    block_type_id: str
    label: str
    solid: bool = True
    placeable: bool = True
    breakable: bool = True
    registry_id: str = DEFAULT_BLOCK_REGISTRY_ID
    registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PaletteEntry":
        """
        Erstellt einen PaletteEntry aus einem JSON-/Dictionary-Objekt.
        """
        if not isinstance(raw, Mapping):
            raise InvalidWorldDefinitionError(
                "Palette entry must be an object.",
                details={"value": make_json_safe(raw)},
            )

        block_type_id = _required_str(
            raw.get("blockTypeId") or raw.get("block_type_id"),
            field_name="blockTypeId",
        )
        label = _safe_str(raw.get("label"), default=block_type_id)

        return cls(
            block_type_id=block_type_id,
            label=label,
            solid=_to_bool(raw.get("solid"), default=True),
            placeable=_to_bool(raw.get("placeable"), default=True),
            breakable=_to_bool(raw.get("breakable"), default=True),
            registry_id=_safe_str(
                raw.get("registryId") or raw.get("registry_id"),
                default=DEFAULT_BLOCK_REGISTRY_ID,
            ),
            registry_version=_safe_str(
                raw.get("registryVersion") or raw.get("registry_version"),
                default=DEFAULT_BLOCK_REGISTRY_VERSION,
            ),
            metadata=_clean_metadata(
                raw.get("metadata")
                if "metadata" in raw
                else raw.get("metadataJson", raw.get("metadata_json", {}))
            ),
        )

    def to_dict(self, *, camel_case: bool = True, include_metadata: bool = True) -> dict[str, Any]:
        """
        Serialisiert den Paletteneintrag als Dictionary.
        """
        data = {
            "block_type_id": self.block_type_id,
            "label": self.label,
            "solid": self.solid,
            "placeable": self.placeable,
            "breakable": self.breakable,
            "registry_id": self.registry_id,
            "registry_version": self.registry_version,
        }

        if include_metadata:
            data["metadata"] = self.metadata

        return _camel_dict(data) if camel_case else data

    def with_metadata(self, metadata: Mapping[str, Any]) -> "PaletteEntry":
        """
        Gibt eine Kopie mit ersetzten Metadaten zurück.
        """
        return replace(self, metadata=_clean_metadata(metadata))


@dataclass(frozen=True, slots=True)
class WorldProviderInfo:
    """
    Beschreibung eines verfügbaren World-Providers.

    Ein Provider ist eine konkrete Weltimplementierung unterhalb von src/world,
    z. B.:

        src/world/flat
        src/world/realWorld

    Diese Klasse beschreibt den Provider, nicht die geladene Weltkonfiguration.
    """

    provider_id: str
    world_type: str
    label: str
    provider_module: str
    config_path: str | None = None
    supports_chunk_generation: bool = True
    supports_world_metadata: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorldProviderInfo":
        """
        Erstellt Provider-Info aus einem Dictionary.
        """
        if not isinstance(raw, Mapping):
            raise InvalidWorldDefinitionError(
                "World provider info must be an object.",
                details={"value": make_json_safe(raw)},
            )

        provider_id = _required_str(
            raw.get("providerId") or raw.get("provider_id"),
            field_name="providerId",
        )
        world_type = _safe_str(
            raw.get("worldType") or raw.get("world_type"),
            default=provider_id,
        )
        label = _safe_str(raw.get("label"), default=provider_id)

        return cls(
            provider_id=provider_id,
            world_type=world_type,
            label=label,
            provider_module=_required_str(
                raw.get("providerModule") or raw.get("provider_module"),
                field_name="providerModule",
            ),
            config_path=_optional_str(raw.get("configPath") or raw.get("config_path")),
            supports_chunk_generation=_to_bool(
                raw.get("supportsChunkGeneration") or raw.get("supports_chunk_generation"),
                default=True,
            ),
            supports_world_metadata=_to_bool(
                raw.get("supportsWorldMetadata") or raw.get("supports_world_metadata"),
                default=True,
            ),
            metadata=_clean_metadata(raw.get("metadata", {})),
        )

    def to_dict(self, *, camel_case: bool = True) -> dict[str, Any]:
        """
        Serialisiert die Provider-Info als Dictionary.
        """
        data = _without_none(
            {
                "provider_id": self.provider_id,
                "world_type": self.world_type,
                "label": self.label,
                "provider_module": self.provider_module,
                "config_path": self.config_path,
                "supports_chunk_generation": self.supports_chunk_generation,
                "supports_world_metadata": self.supports_world_metadata,
                "metadata": self.metadata,
            }
        )

        return _camel_dict(data) if camel_case else data


@dataclass(frozen=True, slots=True)
class WorldDefinition:
    """
    Normalisierte Definition einer geladenen Welt.

    Diese Klasse ist kein SQLAlchemy-Model.

    Sie beschreibt den aus world.json geladenen und validierten Zustand,
    den Generatoren und Serializer verwenden können.
    """

    world_id: str
    world_type: str
    label: str
    schema_version: str = DEFAULT_WORLD_SCHEMA_VERSION

    generator_type: str = "flat-world"
    generator_version: str = "1"

    chunk_size: int = 16
    cell_size: float = 1.0

    coordinate_system: str = DEFAULT_COORDINATE_SYSTEM
    projection_type: str = DEFAULT_PROJECTION_TYPE
    topology_type: str = DEFAULT_TOPOLOGY_TYPE

    surface_y: int = 0
    min_y: int = -8
    max_y: int = 64
    seed: str | int | None = None

    palette: tuple[PaletteEntry, ...] = field(default_factory=tuple)

    block_registry_id: str = DEFAULT_BLOCK_REGISTRY_ID
    block_registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION

    metadata: dict[str, Any] = field(default_factory=dict)
    raw_config: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorldDefinition":
        """
        Erstellt eine normalisierte WorldDefinition aus world.json-Daten.
        """
        if not isinstance(raw, Mapping):
            raise InvalidWorldDefinitionError(
                "World definition must be an object.",
                details={"value": make_json_safe(raw)},
            )

        palette_raw = _to_sequence(raw.get("palette"), field_name="palette")
        palette = tuple(PaletteEntry.from_dict(item) for item in palette_raw)

        block_registry_id = _safe_str(
            raw.get("blockRegistryId") or raw.get("block_registry_id"),
            default=DEFAULT_BLOCK_REGISTRY_ID,
        )
        block_registry_version = _safe_str(
            raw.get("blockRegistryVersion") or raw.get("block_registry_version"),
            default=DEFAULT_BLOCK_REGISTRY_VERSION,
        )

        definition = cls(
            world_id=_required_str(
                raw.get("worldId") or raw.get("world_id"),
                field_name="worldId",
            ),
            world_type=_required_str(
                raw.get("worldType") or raw.get("world_type"),
                field_name="worldType",
            ),
            label=_safe_str(raw.get("label") or raw.get("name"), default="Unnamed World"),
            schema_version=_safe_str(
                raw.get("schemaVersion") or raw.get("schema_version"),
                default=DEFAULT_WORLD_SCHEMA_VERSION,
            ),
            generator_type=_required_str(
                raw.get("generatorType") or raw.get("generator_type"),
                field_name="generatorType",
            ),
            generator_version=_safe_str(
                raw.get("generatorVersion") or raw.get("generator_version"),
                default="1",
            ),
            chunk_size=_to_int(
                raw.get("chunkSize") or raw.get("chunk_size"),
                field_name="chunkSize",
                default=16,
                minimum=MIN_CHUNK_SIZE,
                maximum=MAX_REASONABLE_CHUNK_SIZE,
            ),
            cell_size=_to_float(
                raw.get("cellSize") or raw.get("cell_size"),
                field_name="cellSize",
                default=1.0,
                minimum=MIN_CELL_SIZE,
                maximum=MAX_REASONABLE_CELL_SIZE,
            ),
            coordinate_system=_safe_str(
                raw.get("coordinateSystem") or raw.get("coordinate_system"),
                default=DEFAULT_COORDINATE_SYSTEM,
            ),
            projection_type=_safe_str(
                raw.get("projectionType") or raw.get("projection_type"),
                default=DEFAULT_PROJECTION_TYPE,
            ),
            topology_type=_safe_str(
                raw.get("topologyType") or raw.get("topology_type"),
                default=DEFAULT_TOPOLOGY_TYPE,
            ),
            surface_y=_to_int(
                raw.get("surfaceY") if "surfaceY" in raw else raw.get("surface_y"),
                field_name="surfaceY",
                default=0,
            ),
            min_y=_to_int(
                raw.get("minY") if "minY" in raw else raw.get("min_y"),
                field_name="minY",
                default=-8,
            ),
            max_y=_to_int(
                raw.get("maxY") if "maxY" in raw else raw.get("max_y"),
                field_name="maxY",
                default=64,
            ),
            seed=raw.get("seed"),
            palette=palette,
            block_registry_id=block_registry_id,
            block_registry_version=block_registry_version,
            metadata=_clean_metadata(raw.get("metadata", {})),
            raw_config=dict(raw),
        )

        definition.validate()
        return definition

    def validate(self) -> None:
        """
        Prüft allgemeine, weltübergreifende Invarianten.

        Spezifische Weltregeln, z. B. für flat, gehören zusätzlich in
        src/world/flat/validator.py.
        """
        errors: list[dict[str, Any]] = []

        if not self.world_id:
            errors.append({"field": "worldId", "message": "worldId is required."})

        if not self.world_type:
            errors.append({"field": "worldType", "message": "worldType is required."})

        if self.chunk_size < MIN_CHUNK_SIZE:
            errors.append(
                {
                    "field": "chunkSize",
                    "message": f"chunkSize must be >= {MIN_CHUNK_SIZE}.",
                    "value": self.chunk_size,
                }
            )

        if self.chunk_size > MAX_REASONABLE_CHUNK_SIZE:
            errors.append(
                {
                    "field": "chunkSize",
                    "message": f"chunkSize must be <= {MAX_REASONABLE_CHUNK_SIZE}.",
                    "value": self.chunk_size,
                }
            )

        if self.cell_size < MIN_CELL_SIZE:
            errors.append(
                {
                    "field": "cellSize",
                    "message": f"cellSize must be >= {MIN_CELL_SIZE}.",
                    "value": self.cell_size,
                }
            )

        if self.min_y > self.max_y:
            errors.append(
                {
                    "field": "minY/maxY",
                    "message": "minY must be <= maxY.",
                    "minY": self.min_y,
                    "maxY": self.max_y,
                }
            )

        if not self.palette:
            errors.append(
                {
                    "field": "palette",
                    "message": "palette must contain at least one block type.",
                }
            )

        if len(self.palette) > MAX_PALETTE_SIZE:
            errors.append(
                {
                    "field": "palette",
                    "message": f"palette must contain <= {MAX_PALETTE_SIZE} entries.",
                    "size": len(self.palette),
                }
            )

        seen_block_type_ids: set[str] = set()

        for index, entry in enumerate(self.palette):
            if entry.block_type_id in seen_block_type_ids:
                errors.append(
                    {
                        "field": "palette",
                        "message": "palette contains duplicate blockTypeId.",
                        "blockTypeId": entry.block_type_id,
                        "index": index,
                    }
                )

            seen_block_type_ids.add(entry.block_type_id)

        if errors:
            raise InvalidWorldDefinitionError(
                "World definition failed validation.",
                details={
                    "worldId": self.world_id,
                    "worldType": self.world_type,
                    "errors": errors,
                },
            )

    @property
    def chunk_cell_count(self) -> int:
        """
        Anzahl der Zellen pro kubischem Chunk.
        """
        return calculate_cell_count(self.chunk_size)

    @property
    def palette_block_type_ids(self) -> tuple[str, ...]:
        """
        Gibt alle BlockTypeIds der Palette zurück.
        """
        return tuple(entry.block_type_id for entry in self.palette)

    def get_palette_index(self, block_type_id: str) -> int | None:
        """
        Gibt den Palette-Index für einen Blocktyp zurück.

        Rückgabe:
            0-basierter Palette-Index oder None.
        """
        for index, entry in enumerate(self.palette):
            if entry.block_type_id == block_type_id:
                return index

        return None

    def get_cell_value_for_block_type(self, block_type_id: str) -> int:
        """
        Wandelt eine blockTypeId in einen cellValue um.

        Invariante:
            cellValue = paletteIndex + 1
        """
        palette_index = self.get_palette_index(block_type_id)

        if palette_index is None:
            raise WorldValidationError(
                f"Block type '{block_type_id}' is not part of world palette.",
                details={
                    "worldId": self.world_id,
                    "blockTypeId": block_type_id,
                    "availableBlockTypeIds": self.palette_block_type_ids,
                },
            )

        return palette_index + 1

    def get_block_type_id_for_cell_value(self, cell_value: int) -> str | None:
        """
        Wandelt einen cellValue zurück in eine blockTypeId.

        Rückgabe:
            None für Air.
        """
        value = _to_int(cell_value, field_name="cellValue", minimum=0)

        if value == DEFAULT_AIR_CELL_VALUE:
            return None

        palette_index = value - 1

        if palette_index < 0 or palette_index >= len(self.palette):
            raise WorldValidationError(
                "cellValue references a palette entry that does not exist.",
                details={
                    "worldId": self.world_id,
                    "cellValue": value,
                    "paletteSize": len(self.palette),
                },
            )

        return self.palette[palette_index].block_type_id

    def to_metadata_dict(self, *, camel_case: bool = True) -> dict[str, Any]:
        """
        Serialisiert nur die Welt-Metadaten, nicht die vollständige raw_config.
        """
        data = {
            "world_id": self.world_id,
            "world_type": self.world_type,
            "label": self.label,
            "schema_version": self.schema_version,
            "generator_type": self.generator_type,
            "generator_version": self.generator_version,
            "chunk_size": self.chunk_size,
            "cell_size": self.cell_size,
            "coordinate_system": self.coordinate_system,
            "projection_type": self.projection_type,
            "topology_type": self.topology_type,
            "surface_y": self.surface_y,
            "min_y": self.min_y,
            "max_y": self.max_y,
            "seed": self.seed,
            "block_registry_id": self.block_registry_id,
            "block_registry_version": self.block_registry_version,
            "metadata": self.metadata,
        }

        return _camel_dict(_without_none(data)) if camel_case else _without_none(data)

    def to_dict(
        self,
        *,
        camel_case: bool = True,
        include_palette: bool = True,
        include_raw_config: bool = False,
    ) -> dict[str, Any]:
        """
        Serialisiert die Weltdefinition.
        """
        data = self.to_metadata_dict(camel_case=False)

        if include_palette:
            data["palette"] = [
                entry.to_dict(camel_case=camel_case)
                for entry in self.palette
            ]

        if include_raw_config:
            data["raw_config"] = self.raw_config

        return _camel_dict(data) if camel_case else data


@dataclass(frozen=True, slots=True)
class ChunkRequest:
    """
    Interne Anfrage für einen Chunk.

    Diese Klasse normalisiert Werte, die später aus Query-Parametern oder
    JSON-Requests kommen können.
    """

    world_id: str
    chunk_x: int
    chunk_y: int
    chunk_z: int

    request_id: str | None = None
    include_metadata: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChunkRequest":
        """
        Erstellt eine ChunkRequest aus Query-/JSON-nahen Daten.
        """
        if not isinstance(raw, Mapping):
            raise InvalidChunkRequestError(
                "Chunk request must be an object.",
                details={"value": make_json_safe(raw)},
            )

        request = cls(
            world_id=_required_str(
                raw.get("worldId") or raw.get("world_id"),
                field_name="worldId",
            ),
            chunk_x=_to_int(
                raw.get("chunkX") if "chunkX" in raw else raw.get("chunk_x"),
                field_name="chunkX",
                error_cls=InvalidChunkRequestError,
            ),
            chunk_y=_to_int(
                raw.get("chunkY") if "chunkY" in raw else raw.get("chunk_y"),
                field_name="chunkY",
                error_cls=InvalidChunkRequestError,
            ),
            chunk_z=_to_int(
                raw.get("chunkZ") if "chunkZ" in raw else raw.get("chunk_z"),
                field_name="chunkZ",
                error_cls=InvalidChunkRequestError,
            ),
            request_id=_optional_str(raw.get("requestId") or raw.get("request_id")),
            include_metadata=_to_bool(
                raw.get("includeMetadata") or raw.get("include_metadata"),
                default=True,
            ),
            metadata=_clean_metadata(raw.get("metadata", {})),
        )

        request.validate()
        return request

    @classmethod
    def create(
        cls,
        world_id: str,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        *,
        request_id: str | None = None,
        include_metadata: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ChunkRequest":
        """
        Komfortkonstruktor für interne Aufrufer.
        """
        request = cls(
            world_id=_required_str(world_id, field_name="worldId"),
            chunk_x=_to_int(chunk_x, field_name="chunkX", error_cls=InvalidChunkRequestError),
            chunk_y=_to_int(chunk_y, field_name="chunkY", error_cls=InvalidChunkRequestError),
            chunk_z=_to_int(chunk_z, field_name="chunkZ", error_cls=InvalidChunkRequestError),
            request_id=request_id,
            include_metadata=include_metadata,
            metadata=_clean_metadata(metadata or {}),
        )

        request.validate()
        return request

    @property
    def chunk_key(self) -> str:
        """
        Stabiler Chunk-Key.
        """
        return build_chunk_key(self.chunk_x, self.chunk_y, self.chunk_z)

    def validate(self) -> None:
        """
        Prüft die Chunk-Anfrage.
        """
        if not self.world_id:
            raise InvalidChunkRequestError(
                "Chunk request requires worldId.",
                details={"field": "worldId"},
            )

    def to_dict(self, *, camel_case: bool = True) -> dict[str, Any]:
        """
        Serialisiert die Chunk-Anfrage.
        """
        data = _without_none(
            {
                "world_id": self.world_id,
                "chunk_x": self.chunk_x,
                "chunk_y": self.chunk_y,
                "chunk_z": self.chunk_z,
                "chunk_key": self.chunk_key,
                "request_id": self.request_id,
                "include_metadata": self.include_metadata,
                "metadata": self.metadata,
            }
        )

        return _camel_dict(data) if camel_case else data


@dataclass(frozen=True, slots=True)
class GeneratedChunk:
    """
    Ergebnis einer Chunk-Generierung.

    Diese Klasse ist die interne Repräsentation eines generierten Chunks.

    Sie ist noch keine HTTP-Antwort. Die spätere API-Form wird in
    src/world/serializer.py gebaut.
    """

    world_id: str
    chunk_x: int
    chunk_y: int
    chunk_z: int

    chunk_size: int
    cell_size: float

    palette: tuple[PaletteEntry, ...]
    cells: tuple[int, ...]

    source: str = "generated"
    schema_version: str = DEFAULT_CHUNK_SCHEMA_VERSION

    coordinate_system: str = DEFAULT_COORDINATE_SYSTEM
    projection_type: str = DEFAULT_PROJECTION_TYPE
    topology_type: str = DEFAULT_TOPOLOGY_TYPE

    generator_type: str = "flat-world"
    generator_version: str = "1"

    block_registry_id: str = DEFAULT_BLOCK_REGISTRY_ID
    block_registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION

    cell_index_order: str = DEFAULT_CELL_INDEX_ORDER
    air_cell_value: int = DEFAULT_AIR_CELL_VALUE

    chunk_version: str | None = None
    content_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        world: WorldDefinition,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        cells: Sequence[int],
        source: str = "generated",
        chunk_version: str | None = None,
        content_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "GeneratedChunk":
        """
        Erstellt einen GeneratedChunk aus einer WorldDefinition.
        """
        chunk = cls(
            world_id=world.world_id,
            chunk_x=_to_int(chunk_x, field_name="chunkX"),
            chunk_y=_to_int(chunk_y, field_name="chunkY"),
            chunk_z=_to_int(chunk_z, field_name="chunkZ"),
            chunk_size=world.chunk_size,
            cell_size=world.cell_size,
            palette=world.palette,
            cells=tuple(_to_int(value, field_name="cellValue", minimum=0) for value in cells),
            source=_safe_str(source, default="generated"),
            schema_version=DEFAULT_CHUNK_SCHEMA_VERSION,
            coordinate_system=world.coordinate_system,
            projection_type=world.projection_type,
            topology_type=world.topology_type,
            generator_type=world.generator_type,
            generator_version=world.generator_version,
            block_registry_id=world.block_registry_id,
            block_registry_version=world.block_registry_version,
            chunk_version=chunk_version,
            content_hash=content_hash,
            metadata=_clean_metadata(metadata or {}),
        )

        chunk.validate()
        return chunk

    @property
    def chunk_key(self) -> str:
        """
        Stabiler Chunk-Key.
        """
        return build_chunk_key(self.chunk_x, self.chunk_y, self.chunk_z)

    @property
    def expected_cell_count(self) -> int:
        """
        Erwartete Anzahl an Zellen.
        """
        return calculate_cell_count(self.chunk_size)

    @property
    def is_empty_air_chunk(self) -> bool:
        """
        Gibt zurück, ob der Chunk nur Air enthält.
        """
        return all(value == self.air_cell_value for value in self.cells)

    def validate(self) -> None:
        """
        Prüft die Chunk-Struktur.
        """
        errors: list[dict[str, Any]] = []

        if not self.world_id:
            errors.append({"field": "worldId", "message": "worldId is required."})

        if self.chunk_size < MIN_CHUNK_SIZE:
            errors.append(
                {
                    "field": "chunkSize",
                    "message": f"chunkSize must be >= {MIN_CHUNK_SIZE}.",
                    "value": self.chunk_size,
                }
            )

        expected = self.expected_cell_count
        actual = len(self.cells)

        if actual != expected:
            errors.append(
                {
                    "field": "cells",
                    "message": "cells length does not match chunkSize³.",
                    "expected": expected,
                    "actual": actual,
                    "chunkSize": self.chunk_size,
                }
            )

        palette_size = len(self.palette)

        for index, value in enumerate(self.cells):
            if value < 0:
                errors.append(
                    {
                        "field": "cells",
                        "message": "cellValue must be >= 0.",
                        "index": index,
                        "value": value,
                    }
                )
                break

            if value > palette_size:
                errors.append(
                    {
                        "field": "cells",
                        "message": "cellValue references missing palette entry.",
                        "index": index,
                        "value": value,
                        "paletteSize": palette_size,
                    }
                )
                break

        if errors:
            raise WorldValidationError(
                "Generated chunk failed validation.",
                details={
                    "worldId": self.world_id,
                    "chunkKey": self.chunk_key,
                    "errors": errors,
                },
            )

    def to_dict(
        self,
        *,
        camel_case: bool = True,
        include_palette: bool = True,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        """
        Serialisiert den generierten Chunk als Dictionary.

        Hinweis:
        Diese Ausgabe ist bereits JSON-nah, aber die endgültige API-Form
        soll später über serializer.py laufen.
        """
        data = _without_none(
            {
                "world_id": self.world_id,
                "chunk_x": self.chunk_x,
                "chunk_y": self.chunk_y,
                "chunk_z": self.chunk_z,
                "chunk_key": self.chunk_key,
                "chunk_size": self.chunk_size,
                "cell_size": self.cell_size,
                "source": self.source,
                "schema_version": self.schema_version,
                "coordinate_system": self.coordinate_system,
                "projection_type": self.projection_type,
                "topology_type": self.topology_type,
                "generator_type": self.generator_type,
                "generator_version": self.generator_version,
                "block_registry_id": self.block_registry_id,
                "block_registry_version": self.block_registry_version,
                "cell_index_order": self.cell_index_order,
                "air_cell_value": self.air_cell_value,
                "chunk_version": self.chunk_version,
                "content_hash": self.content_hash,
                "cells": list(self.cells),
            }
        )

        if include_palette:
            data["palette"] = [
                entry.to_dict(camel_case=camel_case)
                for entry in self.palette
            ]

        if include_metadata:
            data["metadata"] = self.metadata

        return _camel_dict(data) if camel_case else data


# ---------------------------------------------------------------------------
# Batch helper models
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ChunkBatchRequest:
    """
    Interne Batch-Anfrage für mehrere Chunks.

    Diese Klasse wird nicht zwingend sofort von __init__.py exportiert,
    ist aber für die spätere /chunks/batch-Route vorbereitet.
    """

    world_id: str
    chunks: tuple[ChunkRequest, ...]
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChunkBatchRequest":
        """
        Erstellt eine Batch-Anfrage aus einem JSON-nahen Objekt.
        """
        if not isinstance(raw, Mapping):
            raise InvalidChunkRequestError(
                "Chunk batch request must be an object.",
                details={"value": make_json_safe(raw)},
            )

        world_id = _required_str(
            raw.get("worldId") or raw.get("world_id"),
            field_name="worldId",
        )

        chunks_raw = _to_sequence(
            raw.get("chunks"),
            field_name="chunks",
            error_cls=InvalidChunkRequestError,
        )

        chunks: list[ChunkRequest] = []

        for index, item in enumerate(chunks_raw):
            if not isinstance(item, Mapping):
                raise InvalidChunkRequestError(
                    "Each batch chunk entry must be an object.",
                    details={"index": index, "value": make_json_safe(item)},
                )

            merged = dict(item)
            merged.setdefault("worldId", world_id)

            chunks.append(ChunkRequest.from_dict(merged))

        batch = cls(
            world_id=world_id,
            chunks=tuple(chunks),
            request_id=_optional_str(raw.get("requestId") or raw.get("request_id")),
            metadata=_clean_metadata(raw.get("metadata", {})),
        )

        batch.validate()
        return batch

    def validate(self) -> None:
        """
        Prüft die Batch-Anfrage.
        """
        if not self.world_id:
            raise InvalidChunkRequestError(
                "Chunk batch request requires worldId.",
                details={"field": "worldId"},
            )

        if not self.chunks:
            raise InvalidChunkRequestError(
                "Chunk batch request requires at least one chunk.",
                details={"field": "chunks"},
            )

        for chunk in self.chunks:
            if chunk.world_id != self.world_id:
                raise InvalidChunkRequestError(
                    "All chunks in a batch must use the same worldId.",
                    details={
                        "batchWorldId": self.world_id,
                        "chunkWorldId": chunk.world_id,
                        "chunkKey": chunk.chunk_key,
                    },
                )

    def to_dict(self, *, camel_case: bool = True) -> dict[str, Any]:
        """
        Serialisiert die Batch-Anfrage.
        """
        data = _without_none(
            {
                "world_id": self.world_id,
                "request_id": self.request_id,
                "chunks": [chunk.to_dict(camel_case=camel_case) for chunk in self.chunks],
                "metadata": self.metadata,
            }
        )

        return _camel_dict(data) if camel_case else data


@dataclass(frozen=True, slots=True)
class WorldListResult:
    """
    Ergebnis einer späteren list_worlds()-Operation.
    """

    worlds: tuple[WorldProviderInfo, ...]
    default_world_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, camel_case: bool = True) -> dict[str, Any]:
        """
        Serialisiert die Weltliste.
        """
        data = _without_none(
            {
                "worlds": [world.to_dict(camel_case=camel_case) for world in self.worlds],
                "default_world_id": self.default_world_id,
                "metadata": self.metadata,
            }
        )

        return _camel_dict(data) if camel_case else data


# ---------------------------------------------------------------------------
# Public helper functions
# ---------------------------------------------------------------------------

def normalize_world_definition(raw: Mapping[str, Any]) -> WorldDefinition:
    """
    Normalisiert rohe world.json-Daten in eine WorldDefinition.
    """
    return WorldDefinition.from_dict(raw)


def normalize_chunk_request(
    world_id: str,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    *,
    request_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ChunkRequest:
    """
    Erstellt eine validierte ChunkRequest.
    """
    return ChunkRequest.create(
        world_id=world_id,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
        request_id=request_id,
        metadata=metadata,
    )


def flatten_cell_index(
    local_x: int,
    local_y: int,
    local_z: int,
    *,
    chunk_size: int,
) -> int:
    """
    Berechnet den linearen Zellindex innerhalb eines Chunks.

    Vorläufige Invariante:
        x ist die schnellste Achse,
        danach y,
        danach z.

        index = x + chunkSize * (y + chunkSize * z)

    Wichtig:
    Diese Funktion ist bewusst hier nur als lokale Modellhilfe enthalten.
    Die endgültige, zwischen Python und TypeScript synchronisierte
    Koordinatenlogik soll später in src/coordinates liegen.
    """
    size = _to_int(
        chunk_size,
        field_name="chunkSize",
        minimum=MIN_CHUNK_SIZE,
        maximum=MAX_REASONABLE_CHUNK_SIZE,
    )

    x = _to_int(local_x, field_name="localX", minimum=0, maximum=size - 1)
    y = _to_int(local_y, field_name="localY", minimum=0, maximum=size - 1)
    z = _to_int(local_z, field_name="localZ", minimum=0, maximum=size - 1)

    return x + size * (y + size * z)


def empty_air_cells(chunk_size: int) -> tuple[int, ...]:
    """
    Erzeugt ein vollständig leeres Air-Zellarray.
    """
    return tuple(DEFAULT_AIR_CELL_VALUE for _ in range(calculate_cell_count(chunk_size)))


__all__ = (
    "WORLD_MODELS_VERSION",
    "DEFAULT_WORLD_SCHEMA_VERSION",
    "DEFAULT_CHUNK_SCHEMA_VERSION",
    "DEFAULT_BLOCK_REGISTRY_ID",
    "DEFAULT_BLOCK_REGISTRY_VERSION",
    "DEFAULT_COORDINATE_SYSTEM",
    "DEFAULT_PROJECTION_TYPE",
    "DEFAULT_TOPOLOGY_TYPE",
    "DEFAULT_CELL_INDEX_ORDER",
    "DEFAULT_AIR_CELL_VALUE",
    "MIN_CHUNK_SIZE",
    "MAX_REASONABLE_CHUNK_SIZE",
    "PaletteEntry",
    "WorldProviderInfo",
    "WorldDefinition",
    "ChunkRequest",
    "GeneratedChunk",
    "ChunkBatchRequest",
    "WorldListResult",
    "build_chunk_key",
    "calculate_cell_count",
    "normalize_world_definition",
    "normalize_chunk_request",
    "flatten_cell_index",
    "empty_air_cells",
)