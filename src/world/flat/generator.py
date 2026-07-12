# src/world/flat/generator.py
"""
VECTOPLAN Flat World Generator.

Diese Datei enthält die konkrete, deterministische Chunk-Generierung der
Standard-Flat-World.

Die Flat-World besteht fachlich ausschließlich aus zwei unveränderlichen
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
        - Zellwert wird aus der konkreten Palette über paletteIndex + 1 bestimmt

Vertikale Generierungsregel:

    worldY > surfaceY
        -> system_air / cellValue 0

    worldY == surfaceY
        -> system_terrain

    minY <= worldY < surfaceY
        -> system_terrain

    worldY < minY
        -> system_air / cellValue 0

    worldY > maxY
        -> system_air / cellValue 0

Architekturregeln:

- Diese Datei liest keine Dateien.
- Diese Datei parst keine world.json.
- Diese Datei importiert keinen Provider.
- Diese Datei kennt keine Flask-Routen.
- Diese Datei greift nicht auf PostgreSQL zu.
- Diese Datei schreibt keine Snapshots, Events oder Commands.
- Die WorldDefinition wird vor der Generierung vollständig geprüft.
- Positive Zellwerte bleiben palettenlokal.
- system_terrain erhält keinen global fest verdrahteten Zellwert.
- Generierte Cells sind immutable Tupel.
- Wiederkehrende vertikale Chunkprofile werden pro Prozess sicher gecacht.
- Vollständige GeneratedChunk-Objekte werden nicht global gecacht.
- Content-Hashes bleiben chunkkoordinatenabhängig und deterministisch.

Die öffentliche API der bisherigen Generator-Version bleibt erhalten.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final

try:
    from src.world.errors import (
        WorldGenerationError,
        WorldValidationError,
        coerce_world_error,
        make_json_safe,
    )
    from src.world.models import (
        DEFAULT_AIR_CELL_VALUE,
        DEFAULT_CELL_INDEX_ORDER,
        ChunkRequest,
        GeneratedChunk,
        WorldDefinition,
        calculate_cell_count,
        flatten_cell_index,
    )
    from src.world.flat.validator import (
        DEFAULT_SUBSURFACE_BLOCK_TYPE_ID,
        DEFAULT_SURFACE_BLOCK_TYPE_ID,
        EXPECTED_CELL_ENCODING_VERSION,
        EXPECTED_GENERATOR_TYPE,
        EXPECTED_GENERATOR_VERSION,
        EXPECTED_TERRAIN_CELL_VALUE,
        EXPECTED_WORLD_TYPE,
        SYSTEM_AIR_BLOCK_ID,
        SYSTEM_TERRAIN_BLOCK_TYPE_ID,
        get_flat_layer_block_type_ids,
        validate_flat_world_config,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.flat.generator requires src.world.errors, src.world.models "
        "and src.world.flat.validator to be importable before the generator "
        "can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Generator constants
# ---------------------------------------------------------------------------

FLAT_GENERATOR_VERSION: Final[str] = "0.2.0"
FLAT_GENERATION_RULE_VERSION: Final[str] = "flat-generation-rules.v2"
FLAT_GENERATION_CONTRACT_VERSION: Final[str] = (
    "flat-system-air-terrain-generation.v1"
)

FLAT_CHUNK_SOURCE: Final[str] = "generated"

CELL_ROLE_AIR: Final[str] = "air"
CELL_ROLE_SURFACE: Final[str] = "surface"
CELL_ROLE_SUBSURFACE: Final[str] = "subsurface"
CELL_ROLE_TERRAIN: Final[str] = "terrain"

DEFAULT_CONTENT_HASH_ALGORITHM: Final[str] = "sha256"
CONTENT_HASH_SCHEMA_VERSION: Final[str] = "flat-content-hash.v2"

MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION: Final[int] = 128 * 128 * 128

# Ein Cache-Eintrag enthält ein immutable Cell-Tupel und eine kleine Statistik.
# 256 Einträge decken viele vertikale Profile ab, ohne beliebig zu wachsen.
DEFAULT_GENERATION_PROFILE_CACHE_SIZE: Final[int] = 256

CELL_INDEX_FORMULA: Final[str] = (
    "index = localX + chunkSize * (localY + chunkSize * localZ)"
)


# ---------------------------------------------------------------------------
# Defensive utility helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt einen beliebigen Wert defensiv in einen bereinigten String um.
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
            raise WorldValidationError(
                f"Required integer field '{field_name}' is missing.",
                details={"field": field_name},
            )

    try:
        return int(value)
    except Exception as exc:
        raise WorldValidationError(
            f"Field '{field_name}' must be an integer.",
            details={
                "field": field_name,
                "value": make_json_safe(value),
            },
            cause=exc,
        ) from exc


def _append_error(
    errors: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    **details: Any,
) -> None:
    """
    Ergänzt einen JSON-sicheren strukturierten Validierungsfehler.
    """
    item: dict[str, Any] = {
        "code": code,
        "message": message,
    }

    for key, value in details.items():
        item[key] = make_json_safe(value)

    errors.append(item)


def _get_raw_config(world: WorldDefinition) -> dict[str, Any]:
    """
    Holt die rohe World-Konfiguration defensiv aus der WorldDefinition.
    """
    raw_config = getattr(world, "raw_config", None)

    if isinstance(raw_config, dict):
        return dict(raw_config)

    return {}


def _get_raw_layers(world: WorldDefinition) -> dict[str, Any]:
    """
    Holt die Layer-Konfiguration defensiv aus world.raw_config.
    """
    raw_config = _get_raw_config(world)
    layers = raw_config.get("layers", {})

    if isinstance(layers, dict):
        return dict(layers)

    return {}


def _get_layer_block_type_ids(
    world: WorldDefinition,
) -> tuple[str, str]:
    """
    Ermittelt Surface- und Subsurface-BlockTypeId.

    Priorität:
    1. validierter Validator-Helfer
    2. rohe Layer-Konfiguration
    3. kanonische Terrain-Defaults
    """
    raw_config = _get_raw_config(world)

    if raw_config:
        try:
            return get_flat_layer_block_type_ids(raw_config)
        except Exception:
            # Die vollständige Generatorvalidierung meldet anschließend einen
            # strukturierten Fehler. Hier bleibt der Fallback bewusst lokal.
            pass

    layers = _get_raw_layers(world)

    surface_block_type_id = _safe_str(
        layers.get("surfaceBlockTypeId"),
        default=DEFAULT_SURFACE_BLOCK_TYPE_ID,
    )
    subsurface_block_type_id = _safe_str(
        layers.get("subsurfaceBlockTypeId"),
        default=DEFAULT_SUBSURFACE_BLOCK_TYPE_ID,
    )

    return surface_block_type_id, subsurface_block_type_id


def _world_y_for_local_y(
    *,
    chunk_y: int,
    local_y: int,
    chunk_size: int,
) -> int:
    """
    Berechnet die globale Y-Koordinate einer lokalen Chunkzelle.
    """
    return chunk_y * chunk_size + local_y


def _world_x_for_local_x(
    *,
    chunk_x: int,
    local_x: int,
    chunk_size: int,
) -> int:
    """
    Berechnet die globale X-Koordinate.

    X beeinflusst die aktuelle Flat-Höhenentscheidung nicht. Der Helfer bleibt
    für Diagnose, Tests und spätere Generatorerweiterungen explizit vorhanden.
    """
    return chunk_x * chunk_size + local_x


def _world_z_for_local_z(
    *,
    chunk_z: int,
    local_z: int,
    chunk_size: int,
) -> int:
    """
    Berechnet die globale Z-Koordinate.

    Z beeinflusst die aktuelle Flat-Höhenentscheidung nicht. Der Helfer bleibt
    für Diagnose, Tests und spätere Generatorerweiterungen explizit vorhanden.
    """
    return chunk_z * chunk_size + local_z


def _cell_role_for_world_y(
    world_y: int,
    *,
    surface_y: int,
    min_y: int,
    max_y: int,
) -> str:
    """
    Bestimmt die semantische Rolle einer globalen Y-Zelle.
    """
    if world_y > max_y:
        return CELL_ROLE_AIR

    if world_y < min_y:
        return CELL_ROLE_AIR

    if world_y > surface_y:
        return CELL_ROLE_AIR

    if world_y == surface_y:
        return CELL_ROLE_SURFACE

    if min_y <= world_y < surface_y:
        return CELL_ROLE_SUBSURFACE

    return CELL_ROLE_AIR


def _terrain_role_for_cell_role(role: str) -> str:
    """
    Fasst Surface und Subsurface fachlich als Terrain zusammen.
    """
    if role in {CELL_ROLE_SURFACE, CELL_ROLE_SUBSURFACE}:
        return CELL_ROLE_TERRAIN

    return CELL_ROLE_AIR


def _validate_world_for_flat_generation(
    world: WorldDefinition,
) -> None:
    """
    Prüft die WorldDefinition unmittelbar vor der Generierung.

    Der Validator prüft world.json bereits vorher. Diese zweite, kleine
    Generatorgrenze schützt direkte interne Aufrufer und verhindert, dass eine
    manuell erzeugte WorldDefinition die Air-/Terrain-Invarianten umgeht.
    """
    if not isinstance(world, WorldDefinition):
        raise WorldValidationError(
            "FlatWorldGenerator requires WorldDefinition.",
            details={
                "worldType": type(world).__name__,
            },
        )

    errors: list[dict[str, Any]] = []

    try:
        world.validate()
    except Exception as exc:
        _append_error(
            errors,
            code="world_definition_validation_failed",
            message="WorldDefinition failed its general validation.",
            errorType=type(exc).__name__,
            error=str(exc),
        )

    if world.world_type != EXPECTED_WORLD_TYPE:
        _append_error(
            errors,
            code="invalid_world_type",
            message=(
                f"Flat generator requires worldType "
                f"'{EXPECTED_WORLD_TYPE}'."
            ),
            actual=world.world_type,
            expected=EXPECTED_WORLD_TYPE,
        )

    if world.generator_type != EXPECTED_GENERATOR_TYPE:
        _append_error(
            errors,
            code="invalid_generator_type",
            message=(
                f"Flat generator requires generatorType "
                f"'{EXPECTED_GENERATOR_TYPE}'."
            ),
            actual=world.generator_type,
            expected=EXPECTED_GENERATOR_TYPE,
        )

    if world.generator_version != EXPECTED_GENERATOR_VERSION:
        _append_error(
            errors,
            code="invalid_generator_version",
            message=(
                f"Flat generator requires generatorVersion "
                f"'{EXPECTED_GENERATOR_VERSION}'."
            ),
            actual=world.generator_version,
            expected=EXPECTED_GENERATOR_VERSION,
        )

    if world.chunk_size <= 0:
        _append_error(
            errors,
            code="invalid_chunk_size",
            message="chunkSize must be > 0.",
            actual=world.chunk_size,
        )

    if world.cell_size <= 0:
        _append_error(
            errors,
            code="invalid_cell_size",
            message="cellSize must be > 0.",
            actual=world.cell_size,
        )

    if world.min_y > world.max_y:
        _append_error(
            errors,
            code="invalid_y_range",
            message="minY must be <= maxY.",
            minY=world.min_y,
            maxY=world.max_y,
        )

    if not (world.min_y <= world.surface_y <= world.max_y):
        _append_error(
            errors,
            code="surface_y_out_of_range",
            message="surfaceY must be between minY and maxY.",
            surfaceY=world.surface_y,
            minY=world.min_y,
            maxY=world.max_y,
        )

    try:
        expected_cell_count = calculate_cell_count(world.chunk_size)
    except Exception as exc:
        expected_cell_count = 0
        _append_error(
            errors,
            code="cell_count_calculation_failed",
            message="Could not calculate chunk cell count.",
            errorType=type(exc).__name__,
            error=str(exc),
        )

    if expected_cell_count > MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION:
        _append_error(
            errors,
            code="chunk_too_large_for_direct_generation",
            message=(
                "Chunk is too large for direct Python tuple generation."
            ),
            chunkSize=world.chunk_size,
            cellCount=expected_cell_count,
            maxCellCount=MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION,
        )

    surface_block_type_id, subsurface_block_type_id = (
        _get_layer_block_type_ids(world)
    )
    available_block_type_ids = tuple(world.palette_block_type_ids)

    if SYSTEM_AIR_BLOCK_ID in available_block_type_ids:
        _append_error(
            errors,
            code="system_air_in_positive_palette",
            message=(
                "system_air must not appear in the positive world palette."
            ),
            availableBlockTypeIds=available_block_type_ids,
        )

    expected_palette = (SYSTEM_TERRAIN_BLOCK_TYPE_ID,)

    if available_block_type_ids != expected_palette:
        _append_error(
            errors,
            code="invalid_flat_world_palette",
            message=(
                "The canonical flat world palette must contain only "
                "system_terrain."
            ),
            actual=available_block_type_ids,
            expected=expected_palette,
        )

    if surface_block_type_id != SYSTEM_TERRAIN_BLOCK_TYPE_ID:
        _append_error(
            errors,
            code="invalid_surface_block_type",
            message="Flat surface must use system_terrain.",
            actual=surface_block_type_id,
            expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
        )

    if subsurface_block_type_id != SYSTEM_TERRAIN_BLOCK_TYPE_ID:
        _append_error(
            errors,
            code="invalid_subsurface_block_type",
            message="Flat subsurface must use system_terrain.",
            actual=subsurface_block_type_id,
            expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
        )

    if surface_block_type_id != subsurface_block_type_id:
        _append_error(
            errors,
            code="surface_subsurface_block_mismatch",
            message=(
                "Flat surface and subsurface must resolve to the same "
                "system_terrain block."
            ),
            surfaceBlockTypeId=surface_block_type_id,
            subsurfaceBlockTypeId=subsurface_block_type_id,
        )

    for field_name, block_type_id in (
        ("surfaceBlockTypeId", surface_block_type_id),
        ("subsurfaceBlockTypeId", subsurface_block_type_id),
    ):
        if block_type_id not in available_block_type_ids:
            _append_error(
                errors,
                code="layer_block_not_in_palette",
                message=f"{field_name} must exist in world palette.",
                field=field_name,
                blockTypeId=block_type_id,
                availableBlockTypeIds=available_block_type_ids,
            )

    raw_config = _get_raw_config(world)

    if raw_config:
        try:
            validate_flat_world_config(raw_config)
        except Exception as exc:
            _append_error(
                errors,
                code="raw_flat_config_validation_failed",
                message=(
                    "WorldDefinition.raw_config does not satisfy the "
                    "canonical flat Air/Terrain contract."
                ),
                errorType=type(exc).__name__,
                error=str(exc),
            )

    if errors:
        raise WorldValidationError(
            "WorldDefinition is not valid for flat Air/Terrain generation.",
            details={
                "worldId": getattr(world, "world_id", None),
                "generatorVersion": getattr(
                    world,
                    "generator_version",
                    None,
                ),
                "errors": errors,
            },
        )


def _validate_request_for_world(
    request: ChunkRequest,
    world: WorldDefinition,
) -> ChunkRequest:
    """
    Prüft und normalisiert eine ChunkRequest gegen eine WorldDefinition.
    """
    if not isinstance(request, ChunkRequest):
        raise WorldValidationError(
            "Flat chunk generation requires ChunkRequest.",
            details={
                "requestType": type(request).__name__,
            },
        )

    try:
        request.validate()
    except Exception as exc:
        raise WorldValidationError(
            "Flat chunk request failed validation.",
            details={
                "requestType": type(request).__name__,
                "request": make_json_safe(request),
            },
            cause=exc,
        ) from exc

    if request.world_id != world.world_id:
        raise WorldValidationError(
            "ChunkRequest worldId does not match WorldDefinition worldId.",
            details={
                "requestWorldId": request.world_id,
                "worldId": world.world_id,
                "chunkKey": request.chunk_key,
            },
        )

    return request


# ---------------------------------------------------------------------------
# Immutable generation profile and statistics
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlatGenerationProfileKey:
    """
    Hashbarer Schlüssel für ein vertikales Flat-Chunkprofil.

    X und Z gehören absichtlich nicht zum Schlüssel, weil die kanonische
    Flat-World nur von worldY abhängt. Dadurch kann die aufwendige Profilerzeugung für Chunks derselben
    chunkY-Ebene wiederverwendet werden. GeneratedChunk darf das Tupel bei
    seiner eigenen Normalisierung defensiv kopieren.
    """

    chunk_size: int
    chunk_y: int
    surface_y: int
    min_y: int
    max_y: int
    surface_cell_value: int
    subsurface_cell_value: int
    air_cell_value: int = DEFAULT_AIR_CELL_VALUE
    rule_version: str = FLAT_GENERATION_RULE_VERSION

    @property
    def expected_cell_count(self) -> int:
        """
        Erwartete Zellanzahl des Profils.
        """
        return calculate_cell_count(self.chunk_size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunkSize": self.chunk_size,
            "chunkY": self.chunk_y,
            "surfaceY": self.surface_y,
            "minY": self.min_y,
            "maxY": self.max_y,
            "surfaceCellValue": self.surface_cell_value,
            "subsurfaceCellValue": self.subsurface_cell_value,
            "airCellValue": self.air_cell_value,
            "ruleVersion": self.rule_version,
        }


@dataclass(frozen=True, slots=True)
class FlatGenerationStats:
    """
    Immutable Statistik eines generierten vertikalen Flat-Chunkprofils.
    """

    air_cells: int = 0
    surface_cells: int = 0
    subsurface_cells: int = 0
    non_air_cells: int = 0
    min_world_y: int | None = None
    max_world_y: int | None = None

    @property
    def terrain_cells(self) -> int:
        """
        Gesamte Anzahl aus Surface- und Subsurface-Terrain.
        """
        return self.surface_cells + self.subsurface_cells

    @property
    def total_cells(self) -> int:
        """
        Gesamte gezählte Zellanzahl.
        """
        return self.air_cells + self.non_air_cells

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert die Statistik.
        """
        return {
            "airCells": self.air_cells,
            "surfaceCells": self.surface_cells,
            "subsurfaceCells": self.subsurface_cells,
            "terrainCells": self.terrain_cells,
            "nonAirCells": self.non_air_cells,
            "totalCells": self.total_cells,
            "minWorldY": self.min_world_y,
            "maxWorldY": self.max_world_y,
        }


@dataclass(frozen=True, slots=True)
class FlatGenerationContext:
    """
    Normalisierter Kontext für eine einzelne Flat-Chunk-Generierung.
    """

    world: WorldDefinition
    request: ChunkRequest

    air_system_block_id: str
    terrain_system_block_id: str

    surface_block_type_id: str
    subsurface_block_type_id: str

    air_cell_value: int
    surface_cell_value: int
    subsurface_cell_value: int

    chunk_size: int
    surface_y: int
    min_y: int
    max_y: int

    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chunk_key(self) -> str:
        return self.request.chunk_key

    @property
    def terrain_cell_value(self) -> int:
        """
        Kanonischer Terrain-Zellwert dieses konkreten Palettenkontexts.
        """
        return self.surface_cell_value

    @property
    def uses_single_terrain_value(self) -> bool:
        """
        Surface und Subsurface müssen denselben Terrain-Zellwert verwenden.
        """
        return self.surface_cell_value == self.subsurface_cell_value

    @property
    def profile_key(self) -> FlatGenerationProfileKey:
        """
        Baut den immutable Cache-Schlüssel des vertikalen Chunkprofils.
        """
        return FlatGenerationProfileKey(
            chunk_size=self.chunk_size,
            chunk_y=self.request.chunk_y,
            surface_y=self.surface_y,
            min_y=self.min_y,
            max_y=self.max_y,
            surface_cell_value=self.surface_cell_value,
            subsurface_cell_value=self.subsurface_cell_value,
            air_cell_value=self.air_cell_value,
        )

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert den Kontext ohne großes Zellarray.
        """
        return {
            "worldId": self.world.world_id,
            "chunkKey": self.chunk_key,
            "airSystemBlockId": self.air_system_block_id,
            "terrainSystemBlockId": self.terrain_system_block_id,
            "surfaceBlockTypeId": self.surface_block_type_id,
            "subsurfaceBlockTypeId": self.subsurface_block_type_id,
            "airCellValue": self.air_cell_value,
            "surfaceCellValue": self.surface_cell_value,
            "subsurfaceCellValue": self.subsurface_cell_value,
            "terrainCellValue": self.terrain_cell_value,
            "usesSingleTerrainValue": self.uses_single_terrain_value,
            "chunkSize": self.chunk_size,
            "surfaceY": self.surface_y,
            "minY": self.min_y,
            "maxY": self.max_y,
            "profileKey": self.profile_key.to_dict(),
            "metadata": make_json_safe(self.metadata),
        }


# ---------------------------------------------------------------------------
# Cell generation and safe profile cache
# ---------------------------------------------------------------------------

def _validate_profile_key(
    profile: FlatGenerationProfileKey,
) -> None:
    """
    Prüft einen Cache-/Generierungsprofil-Schlüssel.
    """
    if not isinstance(profile, FlatGenerationProfileKey):
        raise WorldValidationError(
            "Flat profile generation requires FlatGenerationProfileKey.",
            details={
                "profileType": type(profile).__name__,
            },
        )

    errors: list[dict[str, Any]] = []

    if profile.chunk_size <= 0:
        _append_error(
            errors,
            code="invalid_profile_chunk_size",
            message="Profile chunkSize must be > 0.",
            actual=profile.chunk_size,
        )

    try:
        cell_count = profile.expected_cell_count
    except Exception as exc:
        cell_count = 0
        _append_error(
            errors,
            code="profile_cell_count_failed",
            message="Could not calculate profile cell count.",
            errorType=type(exc).__name__,
            error=str(exc),
        )

    if cell_count > MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION:
        _append_error(
            errors,
            code="profile_chunk_too_large",
            message="Profile chunk exceeds direct generation limit.",
            cellCount=cell_count,
            maxCellCount=MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION,
        )

    if profile.min_y > profile.max_y:
        _append_error(
            errors,
            code="invalid_profile_y_range",
            message="Profile minY must be <= maxY.",
            minY=profile.min_y,
            maxY=profile.max_y,
        )

    if not (
        profile.min_y
        <= profile.surface_y
        <= profile.max_y
    ):
        _append_error(
            errors,
            code="profile_surface_out_of_range",
            message="Profile surfaceY must be between minY and maxY.",
            surfaceY=profile.surface_y,
            minY=profile.min_y,
            maxY=profile.max_y,
        )

    if profile.air_cell_value != DEFAULT_AIR_CELL_VALUE:
        _append_error(
            errors,
            code="invalid_profile_air_cell_value",
            message="Profile Air must use cellValue 0.",
            actual=profile.air_cell_value,
            expected=DEFAULT_AIR_CELL_VALUE,
        )

    if profile.surface_cell_value <= DEFAULT_AIR_CELL_VALUE:
        _append_error(
            errors,
            code="invalid_surface_cell_value",
            message="Surface terrain cellValue must be positive.",
            actual=profile.surface_cell_value,
        )

    if profile.subsurface_cell_value <= DEFAULT_AIR_CELL_VALUE:
        _append_error(
            errors,
            code="invalid_subsurface_cell_value",
            message="Subsurface terrain cellValue must be positive.",
            actual=profile.subsurface_cell_value,
        )

    if profile.surface_cell_value != profile.subsurface_cell_value:
        _append_error(
            errors,
            code="terrain_cell_value_mismatch",
            message=(
                "Canonical Flat surface and subsurface must use the same "
                "terrain cellValue."
            ),
            surfaceCellValue=profile.surface_cell_value,
            subsurfaceCellValue=profile.subsurface_cell_value,
        )

    if errors:
        raise WorldValidationError(
            "Flat generation profile is invalid.",
            details={
                "profile": profile.to_dict(),
                "errors": errors,
            },
        )


def _generate_cells_for_profile_uncached(
    profile: FlatGenerationProfileKey,
) -> tuple[tuple[int, ...], FlatGenerationStats]:
    """
    Erzeugt Cells und Statistik eines vertikalen Profils ohne Cache.

    Die Zellindex-Reihenfolge bleibt:

        index = localX + chunkSize * (localY + chunkSize * localZ)

    Da die Flat-Welt innerhalb einer Y-Schicht für alle X-Werte denselben
    Zellwert verwendet, werden zusammenhängende X-Zeilen geschrieben.
    """
    _validate_profile_key(profile)

    size = profile.chunk_size
    cell_count = profile.expected_cell_count
    cells: list[int] = [profile.air_cell_value] * cell_count

    air_cells = 0
    surface_cells = 0
    subsurface_cells = 0
    non_air_cells = 0

    min_world_y: int | None = None
    max_world_y: int | None = None

    try:
        for local_z in range(size):
            # Die globale Z-Koordinate wird absichtlich nicht benötigt, weil
            # das Profil ausschließlich von Y abhängt.
            for local_y in range(size):
                world_y = _world_y_for_local_y(
                    chunk_y=profile.chunk_y,
                    local_y=local_y,
                    chunk_size=size,
                )

                if min_world_y is None or world_y < min_world_y:
                    min_world_y = world_y

                if max_world_y is None or world_y > max_world_y:
                    max_world_y = world_y

                role = _cell_role_for_world_y(
                    world_y,
                    surface_y=profile.surface_y,
                    min_y=profile.min_y,
                    max_y=profile.max_y,
                )

                if role == CELL_ROLE_SURFACE:
                    cell_value = profile.surface_cell_value
                    surface_cells += size
                    non_air_cells += size
                elif role == CELL_ROLE_SUBSURFACE:
                    cell_value = profile.subsurface_cell_value
                    subsurface_cells += size
                    non_air_cells += size
                else:
                    cell_value = profile.air_cell_value
                    air_cells += size

                row_start = flatten_cell_index(
                    0,
                    local_y,
                    local_z,
                    chunk_size=size,
                )
                row_end = row_start + size

                cells[row_start:row_end] = [cell_value] * size

        stats = FlatGenerationStats(
            air_cells=air_cells,
            surface_cells=surface_cells,
            subsurface_cells=subsurface_cells,
            non_air_cells=non_air_cells,
            min_world_y=min_world_y,
            max_world_y=max_world_y,
        )

        immutable_cells = tuple(cells)

        if len(immutable_cells) != cell_count:
            raise WorldGenerationError(
                "Generated flat cell tuple has invalid size.",
                details={
                    "expectedCellCount": cell_count,
                    "actualCellCount": len(immutable_cells),
                    "profile": profile.to_dict(),
                },
            )

        if stats.total_cells != cell_count:
            raise WorldGenerationError(
                "Flat generation statistics do not match cell count.",
                details={
                    "expectedCellCount": cell_count,
                    "stats": stats.to_dict(),
                    "profile": profile.to_dict(),
                },
            )

        if any(
            value
            not in {
                profile.air_cell_value,
                profile.surface_cell_value,
                profile.subsurface_cell_value,
            }
            for value in immutable_cells
        ):
            raise WorldGenerationError(
                "Generated flat cells contain an unexpected cellValue.",
                details={
                    "profile": profile.to_dict(),
                },
            )

        return immutable_cells, stats

    except (WorldGenerationError, WorldValidationError):
        raise
    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="Flat chunk profile generation failed.",
            fallback_code="flat_chunk_profile_generation_failed",
            fallback_status_code=500,
            details={
                "profile": profile.to_dict(),
                "chunkSize": size,
                "cellCount": cell_count,
            },
        )
        raise world_error from exc


@lru_cache(maxsize=DEFAULT_GENERATION_PROFILE_CACHE_SIZE)
def _generate_cells_for_profile_cached(
    profile: FlatGenerationProfileKey,
) -> tuple[tuple[int, ...], FlatGenerationStats]:
    """
    Prozesslokaler LRU-Cache für immutable vertikale Chunkprofile.

    Sicherheiten:
    - Schlüssel ist eine frozen Dataclass.
    - Cells sind ein Tupel.
    - Statistik ist eine frozen Dataclass.
    - Vollständige Chunkobjekte werden nicht gecacht.
    - Content-Hashes werden nicht gecacht, da sie den Chunk-Key enthalten.
    """
    return _generate_cells_for_profile_uncached(profile)


def get_flat_generation_profile_cache_info() -> dict[str, Any]:
    """
    Gibt den aktuellen Cache-Status JSON-nah zurück.
    """
    try:
        info = _generate_cells_for_profile_cached.cache_info()

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
            "maxSize": DEFAULT_GENERATION_PROFILE_CACHE_SIZE,
            "currentSize": 0,
        }


def clear_flat_generation_profile_cache() -> None:
    """
    Leert den prozesslokalen vertikalen Profilcache.
    """
    _generate_cells_for_profile_cached.cache_clear()


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------

def _build_content_hash(
    *,
    world: WorldDefinition,
    request: ChunkRequest,
    context: FlatGenerationContext,
    cells: tuple[int, ...],
) -> str:
    """
    Baut einen stabilen SHA-256-Hash für generierte Chunkdaten.

    Der Hash enthält:
    - Generator- und Regelversion
    - Welt- und Chunkidentität
    - Höhenparameter
    - konkrete Palette
    - Air-/Terrain-Identitäten
    - konkrete palettenlokale Zellwerte
    - das vollständige immutable Cell-Tupel
    """
    try:
        digest_input = {
            "schemaVersion": CONTENT_HASH_SCHEMA_VERSION,
            "generationContractVersion": (
                FLAT_GENERATION_CONTRACT_VERSION
            ),
            "generationRuleVersion": FLAT_GENERATION_RULE_VERSION,
            "worldId": world.world_id,
            "worldType": world.world_type,
            "generatorType": world.generator_type,
            "generatorVersion": world.generator_version,
            "chunkKey": request.chunk_key,
            "chunkX": request.chunk_x,
            "chunkY": request.chunk_y,
            "chunkZ": request.chunk_z,
            "chunkSize": world.chunk_size,
            "cellSize": world.cell_size,
            "surfaceY": world.surface_y,
            "minY": world.min_y,
            "maxY": world.max_y,
            "blockRegistryId": world.block_registry_id,
            "blockRegistryVersion": world.block_registry_version,
            "palette": world.palette_block_type_ids,
            "airSystemBlockId": context.air_system_block_id,
            "terrainSystemBlockId": context.terrain_system_block_id,
            "airCellValue": context.air_cell_value,
            "surfaceCellValue": context.surface_cell_value,
            "subsurfaceCellValue": context.subsurface_cell_value,
            "cellEncodingVersion": EXPECTED_CELL_ENCODING_VERSION,
            "cellIndexOrder": DEFAULT_CELL_INDEX_ORDER,
            "cells": cells,
        }

        encoded = json.dumps(
            digest_input,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        return hashlib.sha256(encoded).hexdigest()

    except Exception as exc:
        raise WorldGenerationError(
            "Could not build flat chunk content hash.",
            details={
                "worldId": world.world_id,
                "chunkKey": request.chunk_key,
                "algorithm": DEFAULT_CONTENT_HASH_ALGORITHM,
                "hashSchemaVersion": CONTENT_HASH_SCHEMA_VERSION,
            },
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# FlatWorldGenerator
# ---------------------------------------------------------------------------

class FlatWorldGenerator:
    """
    Zustandsarmer Generator für deterministische Air-/Terrain-Flat-Chunks.

    Die Instanz hält nur Konfigurationsflags. Wiederverwendete Zellen liegen im
    modulweiten immutable Profilcache.
    """

    generator_name: Final[str] = "FlatWorldGenerator"
    generator_version: Final[str] = FLAT_GENERATOR_VERSION
    generation_rule_version: Final[str] = FLAT_GENERATION_RULE_VERSION
    generation_contract_version: Final[str] = (
        FLAT_GENERATION_CONTRACT_VERSION
    )

    def __init__(
        self,
        *,
        include_content_hash: bool = True,
        include_generation_stats: bool = True,
        use_generation_profile_cache: bool = True,
    ) -> None:
        self.include_content_hash = bool(include_content_hash)
        self.include_generation_stats = bool(
            include_generation_stats
        )
        self.use_generation_profile_cache = bool(
            use_generation_profile_cache
        )

    def get_status(self) -> dict[str, Any]:
        """
        Gibt einen kleinen, JSON-nahen Generatorstatus zurück.
        """
        return {
            "generatorName": self.generator_name,
            "generatorVersion": self.generator_version,
            "generationRuleVersion": self.generation_rule_version,
            "generationContractVersion": (
                self.generation_contract_version
            ),
            "includeContentHash": self.include_content_hash,
            "includeGenerationStats": (
                self.include_generation_stats
            ),
            "useGenerationProfileCache": (
                self.use_generation_profile_cache
            ),
            "profileCache": get_flat_generation_profile_cache_info(),
            "systemBlocks": {
                "air": {
                    "systemBlockId": SYSTEM_AIR_BLOCK_ID,
                    "cellValue": DEFAULT_AIR_CELL_VALUE,
                    "storedInPositivePalette": False,
                },
                "terrain": {
                    "systemBlockId": SYSTEM_TERRAIN_BLOCK_TYPE_ID,
                    "cellValueRule": "paletteIndex + 1",
                    "storedInPositivePalette": True,
                },
            },
        }

    def create_context(
        self,
        world: WorldDefinition,
        request: ChunkRequest,
    ) -> FlatGenerationContext:
        """
        Baut und validiert den Generierungskontext.
        """
        _validate_world_for_flat_generation(world)
        normalized_request = _validate_request_for_world(
            request,
            world,
        )

        surface_block_type_id, subsurface_block_type_id = (
            _get_layer_block_type_ids(world)
        )

        try:
            surface_cell_value = (
                world.get_cell_value_for_block_type(
                    surface_block_type_id
                )
            )
            subsurface_cell_value = (
                world.get_cell_value_for_block_type(
                    subsurface_block_type_id
                )
            )
        except Exception as exc:
            raise WorldGenerationError(
                "Could not resolve flat terrain layer cell values.",
                details={
                    "worldId": world.world_id,
                    "surfaceBlockTypeId": surface_block_type_id,
                    "subsurfaceBlockTypeId": (
                        subsurface_block_type_id
                    ),
                    "availableBlockTypeIds": (
                        world.palette_block_type_ids
                    ),
                },
                cause=exc,
            ) from exc

        errors: list[dict[str, Any]] = []

        if surface_block_type_id != SYSTEM_TERRAIN_BLOCK_TYPE_ID:
            _append_error(
                errors,
                code="context_surface_not_system_terrain",
                message="Surface block must be system_terrain.",
                actual=surface_block_type_id,
                expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            )

        if (
            subsurface_block_type_id
            != SYSTEM_TERRAIN_BLOCK_TYPE_ID
        ):
            _append_error(
                errors,
                code="context_subsurface_not_system_terrain",
                message="Subsurface block must be system_terrain.",
                actual=subsurface_block_type_id,
                expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            )

        if surface_cell_value <= DEFAULT_AIR_CELL_VALUE:
            _append_error(
                errors,
                code="context_surface_cell_not_positive",
                message="Surface terrain cellValue must be positive.",
                actual=surface_cell_value,
            )

        if subsurface_cell_value <= DEFAULT_AIR_CELL_VALUE:
            _append_error(
                errors,
                code="context_subsurface_cell_not_positive",
                message="Subsurface terrain cellValue must be positive.",
                actual=subsurface_cell_value,
            )

        if surface_cell_value != subsurface_cell_value:
            _append_error(
                errors,
                code="context_terrain_cell_value_mismatch",
                message=(
                    "Surface and subsurface must resolve to the same "
                    "terrain cellValue."
                ),
                surfaceCellValue=surface_cell_value,
                subsurfaceCellValue=subsurface_cell_value,
            )

        # In der kanonischen Welt mit genau einem positiven PaletteEntry ist
        # system_terrain aktuell cellValue 1. Dieser Wert bleibt trotzdem aus
        # der Palette abgeleitet und wird nicht zur globalen Terrain-ID.
        if surface_cell_value != EXPECTED_TERRAIN_CELL_VALUE:
            _append_error(
                errors,
                code="unexpected_canonical_terrain_cell_value",
                message=(
                    "The canonical default flat palette must currently "
                    "resolve system_terrain to cellValue 1."
                ),
                actual=surface_cell_value,
                expected=EXPECTED_TERRAIN_CELL_VALUE,
            )

        if errors:
            raise WorldGenerationError(
                "Could not create canonical flat generation context.",
                details={
                    "worldId": world.world_id,
                    "chunkKey": normalized_request.chunk_key,
                    "errors": errors,
                },
            )

        context = FlatGenerationContext(
            world=world,
            request=normalized_request,
            air_system_block_id=SYSTEM_AIR_BLOCK_ID,
            terrain_system_block_id=(
                SYSTEM_TERRAIN_BLOCK_TYPE_ID
            ),
            surface_block_type_id=surface_block_type_id,
            subsurface_block_type_id=(
                subsurface_block_type_id
            ),
            air_cell_value=DEFAULT_AIR_CELL_VALUE,
            surface_cell_value=surface_cell_value,
            subsurface_cell_value=subsurface_cell_value,
            chunk_size=world.chunk_size,
            surface_y=world.surface_y,
            min_y=world.min_y,
            max_y=world.max_y,
            metadata={
                "generatorName": self.generator_name,
                "generatorVersion": self.generator_version,
                "generationRuleVersion": (
                    self.generation_rule_version
                ),
                "generationContractVersion": (
                    self.generation_contract_version
                ),
                "cellEncodingVersion": (
                    EXPECTED_CELL_ENCODING_VERSION
                ),
            },
        )

        _validate_profile_key(context.profile_key)
        return context

    def cell_value_for_world_y(
        self,
        world_y: int,
        context: FlatGenerationContext,
    ) -> int:
        """
        Gibt den konkreten cellValue einer globalen Y-Koordinate zurück.
        """
        if not isinstance(context, FlatGenerationContext):
            raise WorldValidationError(
                "cell_value_for_world_y requires FlatGenerationContext.",
                details={
                    "contextType": type(context).__name__,
                },
            )

        normalized_world_y = _to_int(
            world_y,
            field_name="worldY",
        )

        role = _cell_role_for_world_y(
            normalized_world_y,
            surface_y=context.surface_y,
            min_y=context.min_y,
            max_y=context.max_y,
        )

        if role == CELL_ROLE_SURFACE:
            return context.surface_cell_value

        if role == CELL_ROLE_SUBSURFACE:
            return context.subsurface_cell_value

        return context.air_cell_value

    def system_block_id_for_world_y(
        self,
        world_y: int,
        context: FlatGenerationContext,
    ) -> str:
        """
        Gibt die fachliche Systemblock-ID einer globalen Y-Koordinate zurück.
        """
        cell_value = self.cell_value_for_world_y(
            world_y,
            context,
        )

        if cell_value == context.air_cell_value:
            return context.air_system_block_id

        return context.terrain_system_block_id

    def generate_cells(
        self,
        context: FlatGenerationContext,
    ) -> tuple[tuple[int, ...], FlatGenerationStats]:
        """
        Erzeugt das vollständige immutable Zellarray eines Chunks.

        Bei aktiviertem Profilcache wird das immutable Generierungsergebnis für
        dieselbe chunkY-Ebene wiederverwendet. GeneratedChunk kann dieses Tupel
        bei seiner Modellnormalisierung defensiv kopieren; gemeinsam mutierbarer
        Zustand entsteht dadurch nicht.
        """
        if not isinstance(context, FlatGenerationContext):
            raise WorldValidationError(
                "generate_cells requires FlatGenerationContext.",
                details={
                    "contextType": type(context).__name__,
                },
            )

        try:
            if self.use_generation_profile_cache:
                return _generate_cells_for_profile_cached(
                    context.profile_key
                )

            return _generate_cells_for_profile_uncached(
                context.profile_key
            )

        except (WorldGenerationError, WorldValidationError):
            raise
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Flat chunk cell generation failed.",
                fallback_code="flat_chunk_cell_generation_failed",
                fallback_status_code=500,
                details={
                    "worldId": context.world.world_id,
                    "chunkKey": context.chunk_key,
                    "profile": context.profile_key.to_dict(),
                },
            )
            raise world_error from exc

    def generate_chunk(
        self,
        world: WorldDefinition,
        request: ChunkRequest,
    ) -> GeneratedChunk:
        """
        Generiert einen vollständigen Flat-Chunk.
        """
        try:
            cache_before = get_flat_generation_profile_cache_info()

            context = self.create_context(world, request)
            cells, stats = self.generate_cells(context)

            cache_after = get_flat_generation_profile_cache_info()

            content_hash = (
                _build_content_hash(
                    world=world,
                    request=context.request,
                    context=context,
                    cells=cells,
                )
                if self.include_content_hash
                else None
            )

            metadata: dict[str, Any] = {
                "generator": {
                    "name": self.generator_name,
                    "version": self.generator_version,
                    "ruleVersion": (
                        self.generation_rule_version
                    ),
                    "contractVersion": (
                        self.generation_contract_version
                    ),
                    "source": FLAT_CHUNK_SOURCE,
                },
                "systemBlocks": {
                    "air": {
                        "systemBlockId": (
                            context.air_system_block_id
                        ),
                        "runtimeBlockTypeId": None,
                        "cellValue": context.air_cell_value,
                        "storedInPositivePalette": False,
                    },
                    "terrain": {
                        "systemBlockId": (
                            context.terrain_system_block_id
                        ),
                        "runtimeBlockTypeId": (
                            context.terrain_system_block_id
                        ),
                        "surfaceBlockTypeId": (
                            context.surface_block_type_id
                        ),
                        "subsurfaceBlockTypeId": (
                            context.subsurface_block_type_id
                        ),
                        "cellValue": context.terrain_cell_value,
                        "cellValueRule": "paletteIndex + 1",
                        "storedInPositivePalette": True,
                    },
                },
                "flat": {
                    "surfaceY": context.surface_y,
                    "minY": context.min_y,
                    "maxY": context.max_y,
                    "surfaceBlockTypeId": (
                        context.surface_block_type_id
                    ),
                    "subsurfaceBlockTypeId": (
                        context.subsurface_block_type_id
                    ),
                    "surfaceCellValue": (
                        context.surface_cell_value
                    ),
                    "subsurfaceCellValue": (
                        context.subsurface_cell_value
                    ),
                    "terrainCellValue": (
                        context.terrain_cell_value
                    ),
                },
                "cellEncoding": {
                    "version": EXPECTED_CELL_ENCODING_VERSION,
                    "airCellValue": context.air_cell_value,
                    "blockCellValueRule": "paletteIndex + 1",
                    "positivePaletteValuesAreLocal": True,
                },
                "cellIndexing": {
                    "order": DEFAULT_CELL_INDEX_ORDER,
                    "formula": CELL_INDEX_FORMULA,
                },
                "profileCache": {
                    "enabled": self.use_generation_profile_cache,
                    "profileKey": context.profile_key.to_dict(),
                    "before": cache_before,
                    "after": cache_after,
                },
            }

            if self.include_generation_stats:
                metadata["stats"] = stats.to_dict()

            chunk = GeneratedChunk.create(
                world=world,
                chunk_x=context.request.chunk_x,
                chunk_y=context.request.chunk_y,
                chunk_z=context.request.chunk_z,
                cells=cells,
                source=FLAT_CHUNK_SOURCE,
                content_hash=content_hash,
                metadata=metadata,
            )

            chunk.validate()

            # Letzte Invariantenprüfung an der fertigen Chunkstruktur.
            if chunk.air_cell_value != DEFAULT_AIR_CELL_VALUE:
                raise WorldGenerationError(
                    "Generated chunk uses invalid Air cell value.",
                    details={
                        "worldId": chunk.world_id,
                        "chunkKey": chunk.chunk_key,
                        "actual": chunk.air_cell_value,
                        "expected": DEFAULT_AIR_CELL_VALUE,
                    },
                )

            if chunk.palette != world.palette:
                raise WorldGenerationError(
                    "Generated chunk palette differs from WorldDefinition.",
                    details={
                        "worldId": chunk.world_id,
                        "chunkKey": chunk.chunk_key,
                    },
                )

            return chunk

        except (WorldGenerationError, WorldValidationError):
            raise
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Flat chunk generation failed.",
                fallback_code="flat_chunk_generation_failed",
                fallback_status_code=500,
                details={
                    "worldId": getattr(world, "world_id", None),
                    "request": (
                        request.to_dict(camel_case=True)
                        if isinstance(request, ChunkRequest)
                        else make_json_safe(request)
                    ),
                },
            )
            raise world_error from exc

    def preview_vertical_profile(
        self,
        world: WorldDefinition,
        *,
        min_y: int | None = None,
        max_y: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Gibt eine kompakte Air-/Terrain-Vorschau des vertikalen Profils zurück.
        """
        _validate_world_for_flat_generation(world)

        surface_block_type_id, subsurface_block_type_id = (
            _get_layer_block_type_ids(world)
        )

        start_y = _to_int(
            min_y,
            field_name="minY",
            default=world.min_y,
        )
        end_y = _to_int(
            max_y,
            field_name="maxY",
            default=world.max_y,
        )

        if start_y > end_y:
            raise WorldValidationError(
                "Vertical preview min_y must be <= max_y.",
                details={
                    "minY": start_y,
                    "maxY": end_y,
                },
            )

        try:
            terrain_cell_value = (
                world.get_cell_value_for_block_type(
                    SYSTEM_TERRAIN_BLOCK_TYPE_ID
                )
            )
        except Exception as exc:
            raise WorldGenerationError(
                "Could not resolve system_terrain for vertical preview.",
                details={
                    "worldId": world.world_id,
                    "availableBlockTypeIds": (
                        world.palette_block_type_ids
                    ),
                },
                cause=exc,
            ) from exc

        result: list[dict[str, Any]] = []

        for world_y in range(start_y, end_y + 1):
            role = _cell_role_for_world_y(
                world_y,
                surface_y=world.surface_y,
                min_y=world.min_y,
                max_y=world.max_y,
            )
            terrain_role = _terrain_role_for_cell_role(role)

            if role == CELL_ROLE_SURFACE:
                block_type_id = surface_block_type_id
                system_block_id = SYSTEM_TERRAIN_BLOCK_TYPE_ID
                cell_value = terrain_cell_value
            elif role == CELL_ROLE_SUBSURFACE:
                block_type_id = subsurface_block_type_id
                system_block_id = SYSTEM_TERRAIN_BLOCK_TYPE_ID
                cell_value = terrain_cell_value
            else:
                block_type_id = None
                system_block_id = SYSTEM_AIR_BLOCK_ID
                cell_value = DEFAULT_AIR_CELL_VALUE

            result.append(
                {
                    "worldY": world_y,
                    "role": role,
                    "generationRole": terrain_role,
                    "systemBlockId": system_block_id,
                    "blockTypeId": block_type_id,
                    "cellValue": cell_value,
                }
            )

        return result


# ---------------------------------------------------------------------------
# Cached default generator
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_default_flat_world_generator() -> FlatWorldGenerator:
    """
    Gibt eine pro Prozess gecachte Default-Generatorinstanz zurück.
    """
    return FlatWorldGenerator(
        include_content_hash=True,
        include_generation_stats=True,
        use_generation_profile_cache=True,
    )


def reset_default_flat_world_generator_cache() -> None:
    """
    Leert Default-Generator- und vertikalen Profilcache.
    """
    try:
        get_default_flat_world_generator.cache_clear()
    finally:
        clear_flat_generation_profile_cache()


def get_default_flat_world_generator_cache_info() -> dict[str, Any]:
    """
    Gibt Status des Singleton- und Profilcaches zurück.
    """
    try:
        singleton_info = (
            get_default_flat_world_generator.cache_info()
        )

        singleton = {
            "hits": singleton_info.hits,
            "misses": singleton_info.misses,
            "maxSize": singleton_info.maxsize,
            "currentSize": singleton_info.currsize,
        }
    except Exception:
        singleton = {
            "hits": 0,
            "misses": 0,
            "maxSize": 1,
            "currentSize": 0,
        }

    return {
        "defaultGenerator": singleton,
        "generationProfiles": (
            get_flat_generation_profile_cache_info()
        ),
    }


# ---------------------------------------------------------------------------
# Public generation functions
# ---------------------------------------------------------------------------

def generate_flat_chunk(
    world: WorldDefinition,
    request: ChunkRequest,
    *,
    generator: FlatWorldGenerator | None = None,
) -> GeneratedChunk:
    """
    Generiert einen kanonischen flachen Air-/Terrain-Chunk.
    """
    active_generator = (
        generator or get_default_flat_world_generator()
    )

    if not isinstance(active_generator, FlatWorldGenerator):
        raise WorldValidationError(
            "generator must be FlatWorldGenerator.",
            details={
                "generatorType": type(active_generator).__name__,
            },
        )

    return active_generator.generate_chunk(world, request)


def generate_chunk(
    world: WorldDefinition,
    request: ChunkRequest,
) -> GeneratedChunk:
    """
    Provider-kompatibler Alias.

    Erwartete Signatur:

        generate_chunk(
            world: WorldDefinition,
            request: ChunkRequest,
        ) -> GeneratedChunk
    """
    return generate_flat_chunk(world, request)


def get_flat_vertical_profile(
    world: WorldDefinition,
    *,
    min_y: int | None = None,
    max_y: int | None = None,
) -> list[dict[str, Any]]:
    """
    Komfortfunktion für eine Air-/Terrain-Profilvorschau.
    """
    return (
        get_default_flat_world_generator()
        .preview_vertical_profile(
            world,
            min_y=min_y,
            max_y=max_y,
        )
    )


__all__ = (
    "FLAT_GENERATOR_VERSION",
    "FLAT_GENERATION_RULE_VERSION",
    "FLAT_GENERATION_CONTRACT_VERSION",
    "FLAT_CHUNK_SOURCE",
    "CELL_ROLE_AIR",
    "CELL_ROLE_SURFACE",
    "CELL_ROLE_SUBSURFACE",
    "CELL_ROLE_TERRAIN",
    "DEFAULT_CONTENT_HASH_ALGORITHM",
    "CONTENT_HASH_SCHEMA_VERSION",
    "MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION",
    "DEFAULT_GENERATION_PROFILE_CACHE_SIZE",
    "CELL_INDEX_FORMULA",
    "FlatGenerationProfileKey",
    "FlatGenerationStats",
    "FlatGenerationContext",
    "FlatWorldGenerator",
    "get_flat_generation_profile_cache_info",
    "clear_flat_generation_profile_cache",
    "get_default_flat_world_generator",
    "reset_default_flat_world_generator_cache",
    "get_default_flat_world_generator_cache_info",
    "generate_flat_chunk",
    "generate_chunk",
    "get_flat_vertical_profile",
)
