# src/world/flat/generator.py
"""
VECTOPLAN Flat World Generator.

Diese Datei enthält die konkrete Generierungslogik für die erste flache
VECTOPLAN-Welt.

Aufgabe:
- aus einer WorldDefinition und ChunkRequest einen GeneratedChunk erzeugen
- keine Dateien lesen
- keine world.json parsen
- keine Provider-Module importieren
- keine Flask-Abhängigkeit
- keine Datenbank
- keine Snapshots
- keine Events
- keine Commands
- keine Three.js-Objekte

Die flache Welt folgt dieser Regel:

    worldY > surfaceY
        → Air

    worldY == surfaceY
        → surfaceBlockTypeId, z. B. debug_grass

    minY <= worldY < surfaceY
        → subsurfaceBlockTypeId, z. B. debug_dirt

    worldY < minY
        → Air

    worldY > maxY
        → Air

Wichtig:
- cellValue = 0 bedeutet Air.
- cellValue = paletteIndex + 1 bedeutet Block.
- Zellindex-Reihenfolge:
    index = localX + chunkSize * (localY + chunkSize * localZ)

Diese Datei ist bewusst so gebaut, dass später andere Generatoren neben
flat existieren können, ohne diese Logik zu vermischen.
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
        EXPECTED_GENERATOR_TYPE,
        EXPECTED_WORLD_TYPE,
        get_flat_layer_block_type_ids,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.flat.generator requires src.world.errors, src.world.models "
        "and src.world.flat.validator to be importable before the generator "
        "can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLAT_GENERATOR_VERSION: Final[str] = "0.1.0"

FLAT_CHUNK_SOURCE: Final[str] = "generated"

FLAT_GENERATION_RULE_VERSION: Final[str] = "flat-generation-rules.v1"

CELL_ROLE_AIR: Final[str] = "air"
CELL_ROLE_SURFACE: Final[str] = "surface"
CELL_ROLE_SUBSURFACE: Final[str] = "subsurface"

DEFAULT_CONTENT_HASH_ALGORITHM: Final[str] = "sha256"

MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION: Final[int] = 128 * 128 * 128


# ---------------------------------------------------------------------------
# Utility helpers
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


def _to_int(value: Any, *, field_name: str, default: int | None = None) -> int:
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


def _get_raw_layers(world: WorldDefinition) -> dict[str, Any]:
    """
    Holt die layers-Konfiguration aus world.raw_config.

    Falls keine raw_config vorhanden ist, wird eine defensive Default-Struktur
    verwendet.
    """
    raw_config = world.raw_config or {}
    layers = raw_config.get("layers", {})

    if isinstance(layers, dict):
        return dict(layers)

    return {}


def _get_layer_block_type_ids(world: WorldDefinition) -> tuple[str, str]:
    """
    Ermittelt surfaceBlockTypeId und subsurfaceBlockTypeId.

    Priorität:
    1. world.raw_config["layers"]
    2. Validator-Helfer mit raw_config
    3. defensive Defaults
    """
    raw_config = world.raw_config or {}

    try:
        if raw_config:
            return get_flat_layer_block_type_ids(raw_config)
    except Exception:
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


def _validate_world_for_flat_generation(world: WorldDefinition) -> None:
    """
    Prüft, ob eine WorldDefinition für diesen Flat-Generator geeignet ist.
    """
    errors: list[dict[str, Any]] = []

    if not isinstance(world, WorldDefinition):
        raise WorldValidationError(
            "FlatWorldGenerator requires WorldDefinition.",
            details={
                "worldType": type(world).__name__,
            },
        )

    if world.world_type != EXPECTED_WORLD_TYPE:
        errors.append(
            {
                "code": "invalid_world_type",
                "message": f"Flat generator requires worldType '{EXPECTED_WORLD_TYPE}'.",
                "actual": world.world_type,
                "expected": EXPECTED_WORLD_TYPE,
            }
        )

    if world.generator_type != EXPECTED_GENERATOR_TYPE:
        errors.append(
            {
                "code": "invalid_generator_type",
                "message": f"Flat generator requires generatorType '{EXPECTED_GENERATOR_TYPE}'.",
                "actual": world.generator_type,
                "expected": EXPECTED_GENERATOR_TYPE,
            }
        )

    if world.chunk_size <= 0:
        errors.append(
            {
                "code": "invalid_chunk_size",
                "message": "chunkSize must be > 0.",
                "actual": world.chunk_size,
            }
        )

    if world.cell_size <= 0:
        errors.append(
            {
                "code": "invalid_cell_size",
                "message": "cellSize must be > 0.",
                "actual": world.cell_size,
            }
        )

    if world.min_y > world.max_y:
        errors.append(
            {
                "code": "invalid_y_range",
                "message": "minY must be <= maxY.",
                "minY": world.min_y,
                "maxY": world.max_y,
            }
        )

    if not (world.min_y <= world.surface_y <= world.max_y):
        errors.append(
            {
                "code": "surface_y_out_of_range",
                "message": "surfaceY must be between minY and maxY.",
                "surfaceY": world.surface_y,
                "minY": world.min_y,
                "maxY": world.max_y,
            }
        )

    expected_cell_count = calculate_cell_count(world.chunk_size)

    if expected_cell_count > MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION:
        errors.append(
            {
                "code": "chunk_too_large_for_direct_generation",
                "message": "Chunk is too large for direct Python list generation.",
                "chunkSize": world.chunk_size,
                "cellCount": expected_cell_count,
                "maxCellCount": MAX_CHUNK_CELLS_FOR_DIRECT_GENERATION,
            }
        )

    surface_block_type_id, subsurface_block_type_id = _get_layer_block_type_ids(world)
    available_block_type_ids = world.palette_block_type_ids

    if surface_block_type_id not in available_block_type_ids:
        errors.append(
            {
                "code": "surface_block_not_in_palette",
                "message": "surfaceBlockTypeId must exist in world palette.",
                "blockTypeId": surface_block_type_id,
                "availableBlockTypeIds": available_block_type_ids,
            }
        )

    if subsurface_block_type_id not in available_block_type_ids:
        errors.append(
            {
                "code": "subsurface_block_not_in_palette",
                "message": "subsurfaceBlockTypeId must exist in world palette.",
                "blockTypeId": subsurface_block_type_id,
                "availableBlockTypeIds": available_block_type_ids,
            }
        )

    if errors:
        raise WorldValidationError(
            "WorldDefinition is not valid for flat chunk generation.",
            details={
                "worldId": getattr(world, "world_id", None),
                "errors": errors,
            },
        )


def _validate_request_for_world(request: ChunkRequest, world: WorldDefinition) -> ChunkRequest:
    """
    Prüft und normalisiert eine ChunkRequest gegen eine WorldDefinition.

    Alias-Fälle werden hier nicht aufgelöst. Der WorldService normalisiert
    normalerweise bereits auf die geladene world_id.
    """
    if not isinstance(request, ChunkRequest):
        raise WorldValidationError(
            "Flat chunk generation requires ChunkRequest.",
            details={
                "requestType": type(request).__name__,
            },
        )

    request.validate()

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


def _world_y_for_local_y(*, chunk_y: int, local_y: int, chunk_size: int) -> int:
    """
    Berechnet die globale worldY-Koordinate für eine lokale Y-Zelle.
    """
    return chunk_y * chunk_size + local_y


def _world_x_for_local_x(*, chunk_x: int, local_x: int, chunk_size: int) -> int:
    """
    Berechnet die globale worldX-Koordinate für eine lokale X-Zelle.

    Für die flache Welt ist X aktuell nicht terrain-relevant, aber die Funktion
    hält die Achsenlogik explizit.
    """
    return chunk_x * chunk_size + local_x


def _world_z_for_local_z(*, chunk_z: int, local_z: int, chunk_size: int) -> int:
    """
    Berechnet die globale worldZ-Koordinate für eine lokale Z-Zelle.

    Für die flache Welt ist Z aktuell nicht terrain-relevant, aber die Funktion
    hält die Achsenlogik explizit.
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
    Bestimmt die semantische Zellrolle für eine globale worldY-Koordinate.
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


def _build_content_hash(
    *,
    world: WorldDefinition,
    request: ChunkRequest,
    cells: tuple[int, ...],
) -> str:
    """
    Baut einen stabilen Content-Hash für generierte Chunk-Daten.

    Der Hash ist für Debugging, spätere Snapshot-Vergleiche und Tests nützlich.
    """
    try:
        digest_input = {
            "worldId": world.world_id,
            "worldType": world.world_type,
            "generatorType": world.generator_type,
            "generatorVersion": world.generator_version,
            "chunkKey": request.chunk_key,
            "chunkSize": world.chunk_size,
            "cellSize": world.cell_size,
            "surfaceY": world.surface_y,
            "minY": world.min_y,
            "maxY": world.max_y,
            "palette": world.palette_block_type_ids,
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
            },
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# Generation metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlatGenerationStats:
    """
    Einfache Statistik über einen generierten Flat-Chunk.
    """

    air_cells: int = 0
    surface_cells: int = 0
    subsurface_cells: int = 0
    non_air_cells: int = 0
    min_world_y: int | None = None
    max_world_y: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert die Statistik.
        """
        return {
            "airCells": self.air_cells,
            "surfaceCells": self.surface_cells,
            "subsurfaceCells": self.subsurface_cells,
            "nonAirCells": self.non_air_cells,
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
    surface_block_type_id: str
    subsurface_block_type_id: str
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

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert den Kontext ohne große Zellarrays.
        """
        return {
            "worldId": self.world.world_id,
            "chunkKey": self.chunk_key,
            "surfaceBlockTypeId": self.surface_block_type_id,
            "subsurfaceBlockTypeId": self.subsurface_block_type_id,
            "surfaceCellValue": self.surface_cell_value,
            "subsurfaceCellValue": self.subsurface_cell_value,
            "chunkSize": self.chunk_size,
            "surfaceY": self.surface_y,
            "minY": self.min_y,
            "maxY": self.max_y,
            "metadata": make_json_safe(self.metadata),
        }


# ---------------------------------------------------------------------------
# FlatWorldGenerator
# ---------------------------------------------------------------------------

class FlatWorldGenerator:
    """
    Generator für deterministische flache Chunks.

    Diese Klasse kann wiederverwendet werden, ohne Zustand zwischen Chunks
    zu behalten.
    """

    generator_name: Final[str] = "FlatWorldGenerator"
    generator_version: Final[str] = FLAT_GENERATOR_VERSION

    def __init__(
        self,
        *,
        include_content_hash: bool = True,
        include_generation_stats: bool = True,
    ) -> None:
        self.include_content_hash = bool(include_content_hash)
        self.include_generation_stats = bool(include_generation_stats)

    def create_context(
        self,
        world: WorldDefinition,
        request: ChunkRequest,
    ) -> FlatGenerationContext:
        """
        Baut und validiert den Generierungskontext.
        """
        _validate_world_for_flat_generation(world)
        request = _validate_request_for_world(request, world)

        surface_block_type_id, subsurface_block_type_id = _get_layer_block_type_ids(world)

        try:
            surface_cell_value = world.get_cell_value_for_block_type(surface_block_type_id)
            subsurface_cell_value = world.get_cell_value_for_block_type(subsurface_block_type_id)
        except Exception as exc:
            raise WorldGenerationError(
                "Could not resolve flat layer block cell values.",
                details={
                    "worldId": world.world_id,
                    "surfaceBlockTypeId": surface_block_type_id,
                    "subsurfaceBlockTypeId": subsurface_block_type_id,
                    "availableBlockTypeIds": world.palette_block_type_ids,
                },
                cause=exc,
            ) from exc

        return FlatGenerationContext(
            world=world,
            request=request,
            surface_block_type_id=surface_block_type_id,
            subsurface_block_type_id=subsurface_block_type_id,
            surface_cell_value=surface_cell_value,
            subsurface_cell_value=subsurface_cell_value,
            chunk_size=world.chunk_size,
            surface_y=world.surface_y,
            min_y=world.min_y,
            max_y=world.max_y,
            metadata={
                "generatorName": self.generator_name,
                "generatorVersion": self.generator_version,
                "generationRuleVersion": FLAT_GENERATION_RULE_VERSION,
            },
        )

    def cell_value_for_world_y(
        self,
        world_y: int,
        context: FlatGenerationContext,
    ) -> int:
        """
        Gibt den cellValue für eine globale worldY-Koordinate zurück.
        """
        role = _cell_role_for_world_y(
            world_y,
            surface_y=context.surface_y,
            min_y=context.min_y,
            max_y=context.max_y,
        )

        if role == CELL_ROLE_SURFACE:
            return context.surface_cell_value

        if role == CELL_ROLE_SUBSURFACE:
            return context.subsurface_cell_value

        return DEFAULT_AIR_CELL_VALUE

    def generate_cells(
        self,
        context: FlatGenerationContext,
    ) -> tuple[tuple[int, ...], FlatGenerationStats]:
        """
        Erzeugt das komplette Zellarray eines Chunks.

        Zellindex-Reihenfolge:
            index = localX + chunkSize * (localY + chunkSize * localZ)
        """
        size = context.chunk_size
        cell_count = calculate_cell_count(size)
        cells: list[int] = [DEFAULT_AIR_CELL_VALUE] * cell_count

        air_cells = 0
        surface_cells = 0
        subsurface_cells = 0
        non_air_cells = 0

        min_world_y: int | None = None
        max_world_y: int | None = None

        try:
            for local_z in range(size):
                # X und Z sind bei der flachen Welt aktuell nicht relevant
                # für die Höhenentscheidung. Die Variablen werden trotzdem
                # bewusst berechnet, damit die Achsenlogik explizit bleibt
                # und später leichter erweitert werden kann.
                _world_z = _world_z_for_local_z(
                    chunk_z=context.request.chunk_z,
                    local_z=local_z,
                    chunk_size=size,
                )

                for local_y in range(size):
                    world_y = _world_y_for_local_y(
                        chunk_y=context.request.chunk_y,
                        local_y=local_y,
                        chunk_size=size,
                    )

                    if min_world_y is None or world_y < min_world_y:
                        min_world_y = world_y

                    if max_world_y is None or world_y > max_world_y:
                        max_world_y = world_y

                    role = _cell_role_for_world_y(
                        world_y,
                        surface_y=context.surface_y,
                        min_y=context.min_y,
                        max_y=context.max_y,
                    )

                    if role == CELL_ROLE_SURFACE:
                        cell_value = context.surface_cell_value
                        surface_cells += size
                        non_air_cells += size
                    elif role == CELL_ROLE_SUBSURFACE:
                        cell_value = context.subsurface_cell_value
                        subsurface_cells += size
                        non_air_cells += size
                    else:
                        cell_value = DEFAULT_AIR_CELL_VALUE
                        air_cells += size

                    for local_x in range(size):
                        _world_x = _world_x_for_local_x(
                            chunk_x=context.request.chunk_x,
                            local_x=local_x,
                            chunk_size=size,
                        )

                        index = flatten_cell_index(
                            local_x,
                            local_y,
                            local_z,
                            chunk_size=size,
                        )
                        cells[index] = cell_value

            stats = FlatGenerationStats(
                air_cells=air_cells,
                surface_cells=surface_cells,
                subsurface_cells=subsurface_cells,
                non_air_cells=non_air_cells,
                min_world_y=min_world_y,
                max_world_y=max_world_y,
            )

            return tuple(cells), stats

        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Flat chunk cell generation failed.",
                fallback_code="flat_chunk_cell_generation_failed",
                fallback_status_code=500,
                details={
                    "worldId": context.world.world_id,
                    "chunkKey": context.chunk_key,
                    "chunkSize": size,
                    "cellCount": cell_count,
                },
            )
            raise world_error from exc

    def generate_chunk(
        self,
        world: WorldDefinition,
        request: ChunkRequest,
    ) -> GeneratedChunk:
        """
        Generiert einen Chunk für die flache Welt.
        """
        try:
            context = self.create_context(world, request)
            cells, stats = self.generate_cells(context)

            content_hash = (
                _build_content_hash(
                    world=world,
                    request=request,
                    cells=cells,
                )
                if self.include_content_hash
                else None
            )

            metadata: dict[str, Any] = {
                "generator": {
                    "name": self.generator_name,
                    "version": self.generator_version,
                    "ruleVersion": FLAT_GENERATION_RULE_VERSION,
                },
                "flat": {
                    "surfaceY": context.surface_y,
                    "minY": context.min_y,
                    "maxY": context.max_y,
                    "surfaceBlockTypeId": context.surface_block_type_id,
                    "subsurfaceBlockTypeId": context.subsurface_block_type_id,
                    "surfaceCellValue": context.surface_cell_value,
                    "subsurfaceCellValue": context.subsurface_cell_value,
                },
                "cellIndexing": {
                    "order": DEFAULT_CELL_INDEX_ORDER,
                    "formula": "index = localX + chunkSize * (localY + chunkSize * localZ)",
                },
            }

            if self.include_generation_stats:
                metadata["stats"] = stats.to_dict()

            chunk = GeneratedChunk.create(
                world=world,
                chunk_x=request.chunk_x,
                chunk_y=request.chunk_y,
                chunk_z=request.chunk_z,
                cells=cells,
                source=FLAT_CHUNK_SOURCE,
                content_hash=content_hash,
                metadata=metadata,
            )

            chunk.validate()
            return chunk

        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Flat chunk generation failed.",
                fallback_code="flat_chunk_generation_failed",
                fallback_status_code=500,
                details={
                    "worldId": getattr(world, "world_id", None),
                    "request": request.to_dict(camel_case=True)
                    if isinstance(request, ChunkRequest)
                    else make_json_safe(request),
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
        Gibt eine kompakte Vorschau der vertikalen Flat-Regeln zurück.

        Diese Methode ist für Tests, Debugging und spätere Diagnose hilfreich.
        Sie erzeugt keinen Chunk.
        """
        _validate_world_for_flat_generation(world)

        surface_block_type_id, subsurface_block_type_id = _get_layer_block_type_ids(world)

        start_y = _to_int(min_y, field_name="minY", default=world.min_y)
        end_y = _to_int(max_y, field_name="maxY", default=world.max_y)

        if start_y > end_y:
            raise WorldValidationError(
                "Vertical preview min_y must be <= max_y.",
                details={
                    "minY": start_y,
                    "maxY": end_y,
                },
            )

        result: list[dict[str, Any]] = []

        for world_y in range(start_y, end_y + 1):
            role = _cell_role_for_world_y(
                world_y,
                surface_y=world.surface_y,
                min_y=world.min_y,
                max_y=world.max_y,
            )

            if role == CELL_ROLE_SURFACE:
                block_type_id = surface_block_type_id
                cell_value = world.get_cell_value_for_block_type(surface_block_type_id)
            elif role == CELL_ROLE_SUBSURFACE:
                block_type_id = subsurface_block_type_id
                cell_value = world.get_cell_value_for_block_type(subsurface_block_type_id)
            else:
                block_type_id = None
                cell_value = DEFAULT_AIR_CELL_VALUE

            result.append(
                {
                    "worldY": world_y,
                    "role": role,
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
    Gibt eine pro Prozess gecachte FlatWorldGenerator-Instanz zurück.
    """
    return FlatWorldGenerator(
        include_content_hash=True,
        include_generation_stats=True,
    )


def reset_default_flat_world_generator_cache() -> None:
    """
    Leert den Cache der Default-Generator-Instanz.
    """
    get_default_flat_world_generator.cache_clear()


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
    Generiert einen flachen Chunk.

    Diese Funktion ist der bevorzugte direkte Einstieg für provider.py.
    """
    active_generator = generator or get_default_flat_world_generator()
    return active_generator.generate_chunk(world, request)


def generate_chunk(
    world: WorldDefinition,
    request: ChunkRequest,
) -> GeneratedChunk:
    """
    Provider-kompatibler Alias.

    Erwartete Signatur für WorldService:

        generate_chunk(world: WorldDefinition, request: ChunkRequest) -> GeneratedChunk
    """
    return generate_flat_chunk(world, request)


def get_flat_vertical_profile(
    world: WorldDefinition,
    *,
    min_y: int | None = None,
    max_y: int | None = None,
) -> list[dict[str, Any]]:
    """
    Komfortfunktion für eine vertikale Profilvorschau der flachen Welt.
    """
    return get_default_flat_world_generator().preview_vertical_profile(
        world,
        min_y=min_y,
        max_y=max_y,
    )


__all__ = (
    "FLAT_GENERATOR_VERSION",
    "FLAT_CHUNK_SOURCE",
    "FLAT_GENERATION_RULE_VERSION",
    "CELL_ROLE_AIR",
    "CELL_ROLE_SURFACE",
    "CELL_ROLE_SUBSURFACE",
    "FlatGenerationStats",
    "FlatGenerationContext",
    "FlatWorldGenerator",
    "get_default_flat_world_generator",
    "reset_default_flat_world_generator_cache",
    "generate_flat_chunk",
    "generate_chunk",
    "get_flat_vertical_profile",
)