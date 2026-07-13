# services/vectoplan-chunk/src/world/earth/generator.py
"""Deterministischer Air-only-Generator des Earth-v1-Providers.

Der Generator erzeugt den Basiszustand unmaterialisierter Earth-Chunks. Er
verwendet dieselbe Chunkgröße, Zellindexreihenfolge und CellValue-Codierung wie
der bestehende Snapshot-/Command-Pfad:

* ``cellValue == 0`` bedeutet Air;
* X ist die schnellste Zellachse, danach Y, danach Z;
* Air ist nicht Bestandteil der Palette;
* ein generierter Chunk ist nicht automatisch materialisiert;
* erst Snapshots werden persistierte Lade-Wahrheit.

Earth-spezifische Regeln
------------------------
* Jede angeforderte Chunkadresse wird vor der Generierung kanonisiert.
* Periodische X-Aliase erzeugen denselben physischen Chunk.
* Z-Grenzen werden durch die konkrete ``PeriodicXTopology`` geprüft.
* Generatorcache-Keys verwenden ausschließlich kanonische Chunkadressen.
* Die Weltnaht erzeugt keine zusätzlichen Block- oder Grenzflächen.
* Der Generator speichert und berechnet keine globalen Koordinaten.

Cachemodell
-----------
Air-Zellarrays sind unveränderlich und werden pro Chunkgröße geteilt.
Kanonischer Chunkinhalt, Konfigurationen und Generatorinstanzen liegen in
begrenzten LRU-Caches. Öffentliche ``EarthGeneratedChunk``-Wrapper werden je
Anfrage neu erzeugt, damit angeforderte und kanonische Adresse korrekt
diagnostiziert werden können.

Das Modul führt keinen Datenbankzugriff, kein Logging, kein Seeding, keine
CRS-Transformation und keine HTTP-Serialisierung außerhalb seiner expliziten
Payloadmethoden aus.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from threading import RLock
from typing import Any, Final, Iterator, Self

from ...coordinates.chunk_math import checked_cell_count
from ...coordinates.errors import CoordinateError
from ...coordinates.models import (
    ChunkAddress,
    ChunkPosition,
    JsonValue,
    LocalCellPosition,
    NormalizedChunkAddress,
)
from ...coordinates.topology import (
    NorthSouthPolicy,
    PeriodicXTopology,
    get_periodic_x_topology,
)
from ...georeferencing.errors import (
    GeoreferencingConfigurationError,
    GeoreferencingValidationError,
)
from .validator import (
    DEFAULT_BLOCK_TYPE_ID,
    GENERATION_MODE,
    GENERATOR_TYPE,
    PROVIDER_ID,
    TOPOLOGY_TYPE,
    EarthWorldDefinition,
    load_earth_world_definition,
)


AIR_CELL_VALUE: Final[int] = 0
CELL_VALUE_ENCODING: Final[str] = (
    "air-zero-palette-index-plus-one-v1"
)
CHUNK_CONTENT_SCHEMA_VERSION: Final[str] = (
    "earth-generated-chunk.v1"
)
GENERATOR_CONFIG_SCHEMA_VERSION: Final[str] = (
    "earth-generator-config.v1"
)
GENERATOR_SOURCE: Final[str] = "generator"
INDEX_ORDER: Final[str] = "x-fastest-y-then-z"

_MAX_BATCH_SIZE: Final[int] = 4_096
_AIR_CELL_CACHE_SIZE: Final[int] = 16
_CONFIG_CACHE_SIZE: Final[int] = 64
_GENERATOR_CACHE_SIZE: Final[int] = 64
_CHUNK_CONTENT_CACHE_SIZE: Final[int] = 8_192

_METRICS_LOCK = RLock()
_GENERATE_CALLS = 0
_GENERATE_BATCH_CALLS = 0
_GENERATED_WRAPPERS = 0
_GENERATION_FAILURES = 0


@dataclass(frozen=True, slots=True)
class EarthGeneratorConfig:
    """Vollständig validierte Konfiguration einer Generatorinstanz."""

    definition_semantic_fingerprint: str
    provider_id: str
    generator_type: str
    generator_version: str
    topology_type: str
    chunk_size: int
    cell_count: int
    world_width_cells: int
    world_height_cells: int
    default_block_type_id: str
    generation_mode: str
    air_cell_value: int
    cell_value_encoding: str
    linear_index_order: str
    topology: PeriodicXTopology

    schema_version: Final[str] = GENERATOR_CONFIG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "definition_semantic_fingerprint",
            _require_sha256(
                self.definition_semantic_fingerprint,
                field_name="definitionSemanticFingerprint",
            ),
        )
        object.__setattr__(
            self,
            "provider_id",
            _require_exact_text(
                self.provider_id,
                expected=PROVIDER_ID,
                field_name="providerId",
            ),
        )
        object.__setattr__(
            self,
            "generator_type",
            _require_exact_text(
                self.generator_type,
                expected=GENERATOR_TYPE,
                field_name="generatorType",
            ),
        )
        object.__setattr__(
            self,
            "generator_version",
            _require_non_empty_text(
                self.generator_version,
                field_name="generatorVersion",
                maximum_length=128,
            ),
        )
        object.__setattr__(
            self,
            "topology_type",
            _require_exact_text(
                self.topology_type,
                expected=TOPOLOGY_TYPE,
                field_name="topologyType",
            ),
        )

        chunk_size = _require_positive_int(
            self.chunk_size,
            field_name="chunkSize",
        )
        cell_count = _require_positive_int(
            self.cell_count,
            field_name="cellCount",
        )
        expected_cell_count = checked_cell_count(chunk_size)
        if cell_count != expected_cell_count:
            raise GeoreferencingConfigurationError(
                "cellCount entspricht nicht chunkSize³.",
                details={
                    "chunkSize": chunk_size,
                    "cellCount": cell_count,
                    "expectedCellCount": expected_cell_count,
                },
            )

        width = _require_positive_int(
            self.world_width_cells,
            field_name="worldWidthCells",
        )
        height = _require_positive_int(
            self.world_height_cells,
            field_name="worldHeightCells",
        )

        if not isinstance(self.topology, PeriodicXTopology):
            raise GeoreferencingConfigurationError(
                "topology muss eine PeriodicXTopology sein.",
                details={
                    "actualType": type(self.topology).__name__,
                },
            )
        if self.topology.chunk_size != chunk_size:
            raise GeoreferencingConfigurationError(
                "Generator- und Topologie-Chunkgröße widersprechen sich.",
                details={
                    "generatorChunkSize": chunk_size,
                    "topologyChunkSize": self.topology.chunk_size,
                },
            )
        if self.topology.world_width_blocks != width:
            raise GeoreferencingConfigurationError(
                "Generator- und Topologie-Weltbreite widersprechen sich.",
                details={
                    "generatorWorldWidthCells": width,
                    "topologyWorldWidthBlocks": (
                        self.topology.world_width_blocks
                    ),
                },
            )
        if (
            self.topology.north_south_policy
            is not NorthSouthPolicy.BOUNDED
        ):
            raise GeoreferencingConfigurationError(
                "Earth-v1-Generator benötigt eine begrenzte Z-Topologie."
            )

        assert self.topology.minimum_z is not None
        assert self.topology.maximum_z is not None
        topology_height = (
            self.topology.maximum_z
            - self.topology.minimum_z
            + 1
        )
        if topology_height != height:
            raise GeoreferencingConfigurationError(
                "Generatorhöhe entspricht nicht der lokalen Topologiehöhe.",
                details={
                    "worldHeightCells": height,
                    "topologyHeight": topology_height,
                    "minimumZ": self.topology.minimum_z,
                    "maximumZ": self.topology.maximum_z,
                },
            )

        object.__setattr__(self, "chunk_size", chunk_size)
        object.__setattr__(self, "cell_count", cell_count)
        object.__setattr__(self, "world_width_cells", width)
        object.__setattr__(self, "world_height_cells", height)
        object.__setattr__(
            self,
            "default_block_type_id",
            _require_exact_text(
                self.default_block_type_id,
                expected=DEFAULT_BLOCK_TYPE_ID,
                field_name="defaultBlockTypeId",
            ),
        )
        object.__setattr__(
            self,
            "generation_mode",
            _require_exact_text(
                self.generation_mode,
                expected=GENERATION_MODE,
                field_name="generationMode",
            ),
        )

        air_value = _require_non_negative_int(
            self.air_cell_value,
            field_name="airCellValue",
        )
        if air_value != AIR_CELL_VALUE:
            raise GeoreferencingConfigurationError(
                "Earth-v1-Air muss cellValue 0 verwenden.",
                details={
                    "airCellValue": air_value,
                    "expectedAirCellValue": AIR_CELL_VALUE,
                },
            )
        object.__setattr__(self, "air_cell_value", air_value)
        object.__setattr__(
            self,
            "cell_value_encoding",
            _require_exact_text(
                self.cell_value_encoding,
                expected=CELL_VALUE_ENCODING,
                field_name="cellValueEncoding",
            ),
        )
        object.__setattr__(
            self,
            "linear_index_order",
            _require_exact_text(
                self.linear_index_order,
                expected=INDEX_ORDER,
                field_name="linearIndexOrder",
            ),
        )

    @classmethod
    def from_definition(
        cls,
        definition: EarthWorldDefinition,
        *,
        topology: PeriodicXTopology,
    ) -> Self:
        """Erzeugt eine Konfiguration aus einem validierten Manifest."""

        if not isinstance(definition, EarthWorldDefinition):
            raise GeoreferencingValidationError(
                "definition muss EarthWorldDefinition sein.",
                details={
                    "actualType": type(definition).__name__,
                },
            )
        if not isinstance(topology, PeriodicXTopology):
            raise GeoreferencingValidationError(
                "topology muss PeriodicXTopology sein.",
                details={
                    "actualType": type(topology).__name__,
                },
            )

        _validate_definition_for_generation(definition)

        return cls(
            definition_semantic_fingerprint=(
                definition.semantic_fingerprint
            ),
            provider_id=definition.provider_id,
            generator_type=definition.generator.generator_type,
            generator_version=definition.generator.version,
            topology_type=definition.topology.topology_type,
            chunk_size=definition.chunk.size,
            cell_count=checked_cell_count(
                definition.chunk.size
            ),
            world_width_cells=(
                definition.grid.world_width_cells
            ),
            world_height_cells=(
                definition.grid.world_height_cells
            ),
            default_block_type_id=(
                definition.generator.default_block_type_id
            ),
            generation_mode=(
                definition.generator.generation_mode
            ),
            air_cell_value=AIR_CELL_VALUE,
            cell_value_encoding=CELL_VALUE_ENCODING,
            linear_index_order=(
                definition.chunk.linear_index_order
            ),
            topology=topology,
        )

    @property
    def world_width_chunks(self) -> int:
        return self.world_width_cells // self.chunk_size

    @property
    def world_height_chunks(self) -> int:
        return self.world_height_cells // self.chunk_size

    @property
    def config_fingerprint(self) -> str:
        canonical = json.dumps(
            self.fingerprint_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def generated_chunk_version(self) -> str:
        return (
            f"{self.generator_type}:"
            f"{self.generator_version}:"
            f"{self.config_fingerprint[:16]}"
        )

    def fingerprint_payload(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "definitionSemanticFingerprint": (
                self.definition_semantic_fingerprint
            ),
            "providerId": self.provider_id,
            "generatorType": self.generator_type,
            "generatorVersion": self.generator_version,
            "topologyType": self.topology_type,
            "chunkSize": self.chunk_size,
            "cellCount": self.cell_count,
            "worldWidthCells": self.world_width_cells,
            "worldHeightCells": self.world_height_cells,
            "defaultBlockTypeId": self.default_block_type_id,
            "generationMode": self.generation_mode,
            "airCellValue": self.air_cell_value,
            "cellValueEncoding": self.cell_value_encoding,
            "linearIndexOrder": self.linear_index_order,
            "topology": self.topology.to_dict(),
        }

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            **self.fingerprint_payload(),
            "configFingerprint": self.config_fingerprint,
            "worldWidthChunks": self.world_width_chunks,
            "worldHeightChunks": self.world_height_chunks,
            "generatedChunkVersion": (
                self.generated_chunk_version
            ),
        }


@dataclass(frozen=True, slots=True)
class _EarthChunkContent:
    """Intern gecachter, ausschließlich kanonischer Chunkinhalt."""

    address: ChunkAddress
    cells: tuple[int, ...]
    palette: tuple[str, ...]
    content_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.address, ChunkAddress):
            raise GeoreferencingConfigurationError(
                "address muss ChunkAddress sein."
            )
        object.__setattr__(self, "cells", tuple(self.cells))
        object.__setattr__(self, "palette", tuple(self.palette))
        object.__setattr__(
            self,
            "content_fingerprint",
            _require_sha256(
                self.content_fingerprint,
                field_name="contentFingerprint",
            ),
        )


@dataclass(frozen=True, slots=True)
class EarthGeneratedChunk:
    """Snapshot-kompatibler Basiszustand eines Earth-Chunks."""

    normalization: NormalizedChunkAddress
    config: EarthGeneratorConfig
    cells: tuple[int, ...]
    palette: tuple[str, ...]
    content_fingerprint: str

    schema_version: Final[str] = CHUNK_CONTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(
            self.normalization,
            NormalizedChunkAddress,
        ):
            raise GeoreferencingValidationError(
                "normalization muss NormalizedChunkAddress sein.",
                details={
                    "actualType": type(self.normalization).__name__,
                },
            )
        if not isinstance(self.config, EarthGeneratorConfig):
            raise GeoreferencingValidationError(
                "config muss EarthGeneratorConfig sein.",
                details={
                    "actualType": type(self.config).__name__,
                },
            )

        cells = tuple(self.cells)
        palette = tuple(self.palette)

        if len(cells) != self.config.cell_count:
            raise GeoreferencingConfigurationError(
                "Generierter Chunk besitzt eine ungültige Zellanzahl.",
                details={
                    "actualCellCount": len(cells),
                    "expectedCellCount": self.config.cell_count,
                },
            )
        if palette:
            raise GeoreferencingConfigurationError(
                "Air-only-Chunk darf keine Palette besitzen.",
                details={"paletteSize": len(palette)},
            )
        if any(
            value != self.config.air_cell_value
            for value in cells
        ):
            raise GeoreferencingConfigurationError(
                "Air-only-Chunk enthält einen Nicht-Air-Zellwert."
            )

        object.__setattr__(self, "cells", cells)
        object.__setattr__(self, "palette", palette)
        object.__setattr__(
            self,
            "content_fingerprint",
            _require_sha256(
                self.content_fingerprint,
                field_name="contentFingerprint",
            ),
        )

    @property
    def requested_address(self) -> ChunkAddress:
        return self.normalization.requested

    @property
    def address(self) -> ChunkAddress:
        return self.normalization.canonical

    @property
    def chunk_key(self) -> str:
        return self.address.key

    @property
    def chunk_size(self) -> int:
        return self.config.chunk_size

    @property
    def cell_count(self) -> int:
        return self.config.cell_count

    @property
    def canonicalized(self) -> bool:
        return self.normalization.changed

    @property
    def non_air_cell_count(self) -> int:
        return 0

    @property
    def materialized(self) -> bool:
        return False

    @property
    def source(self) -> str:
        return GENERATOR_SOURCE

    @property
    def generation_fingerprint(self) -> str:
        payload = {
            "schemaVersion": self.schema_version,
            "configFingerprint": self.config.config_fingerprint,
            "chunkKey": self.chunk_key,
            "contentFingerprint": self.content_fingerprint,
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def cell_value(
        self,
        cell: LocalCellPosition,
    ) -> int:
        if not isinstance(cell, LocalCellPosition):
            raise GeoreferencingValidationError(
                "cell muss LocalCellPosition sein.",
                details={"actualType": type(cell).__name__},
            )
        index = cell.to_linear_index(self.chunk_size)
        return self.cells[index]

    def cell_value_by_index(self, index: int) -> int:
        normalized = _require_non_negative_int(
            index,
            field_name="linearIndex",
        )
        if normalized >= self.cell_count:
            raise GeoreferencingValidationError(
                "linearIndex liegt außerhalb des Chunks.",
                details={
                    "linearIndex": normalized,
                    "cellCount": self.cell_count,
                },
            )
        return self.cells[normalized]

    def iter_cells(self) -> Iterator[int]:
        return iter(self.cells)

    def to_snapshot_payload(
        self,
        *,
        include_cells: bool = True,
    ) -> dict[str, JsonValue]:
        """Liefert den materialisierbaren Generatorbasiszustand.

        Das Payload enthält ausschließlich die kanonische Chunkadresse.
        Angeforderte Aliasadressen dürfen niemals als Snapshot-Key
        persistiert werden.
        """

        payload: dict[str, JsonValue] = {
            "schemaVersion": self.schema_version,
            "source": self.source,
            "materialized": self.materialized,
            "providerId": self.config.provider_id,
            "generatorType": self.config.generator_type,
            "generatorVersion": self.config.generator_version,
            "chunkVersion": (
                self.config.generated_chunk_version
            ),
            "chunkRevision": 0,
            "chunkKey": self.address.key,
            "chunkX": self.address.x,
            "chunkY": self.address.y,
            "chunkZ": self.address.z,
            "chunkSize": self.chunk_size,
            "cellCount": self.cell_count,
            "palette": list(self.palette),
            "cellValueEncoding": (
                self.config.cell_value_encoding
            ),
            "linearIndexOrder": (
                self.config.linear_index_order
            ),
            "defaultBlockTypeId": (
                self.config.default_block_type_id
            ),
            "generationMode": self.config.generation_mode,
            "nonAirCellCount": self.non_air_cell_count,
            "contentFingerprint": self.content_fingerprint,
            "generationFingerprint": (
                self.generation_fingerprint
            ),
            "configFingerprint": (
                self.config.config_fingerprint
            ),
        }
        if include_cells:
            payload["cells"] = list(self.cells)
        return payload

    def to_runtime_payload(
        self,
        *,
        include_cells: bool = True,
    ) -> dict[str, JsonValue]:
        payload = self.to_snapshot_payload(
            include_cells=include_cells
        )
        payload["requestedChunk"] = (
            self.requested_address.to_dict()
        )
        payload["canonicalChunk"] = self.address.to_dict()
        payload["canonicalized"] = self.canonicalized
        payload["normalization"] = (
            self.normalization.metadata.to_dict()
        )
        return payload

    def to_dict(
        self,
        *,
        include_cells: bool = True,
    ) -> dict[str, JsonValue]:
        return self.to_runtime_payload(
            include_cells=include_cells
        )


@dataclass(frozen=True, slots=True)
class EarthFlatPeriodicGenerator:
    """Deterministischer Generator für unmaterialisierte Earth-Chunks."""

    config: EarthGeneratorConfig

    def __post_init__(self) -> None:
        if not isinstance(self.config, EarthGeneratorConfig):
            raise GeoreferencingValidationError(
                "config muss EarthGeneratorConfig sein.",
                details={
                    "actualType": type(self.config).__name__,
                },
            )

    @classmethod
    def from_definition(
        cls,
        definition: EarthWorldDefinition,
        *,
        topology: PeriodicXTopology,
    ) -> Self:
        return get_earth_flat_periodic_generator(
            get_earth_generator_config(
                definition,
                topology=topology,
            )
        )

    def generate_chunk(
        self,
        address: (
            ChunkAddress
            | ChunkPosition
            | Mapping[str, Any]
            | Sequence[int]
        ),
    ) -> EarthGeneratedChunk:
        """Generiert einen Chunk nach vorheriger Kanonisierung."""

        _record_generate_call()
        try:
            requested = _coerce_chunk_address(address)
            normalization = (
                self.config.topology.normalize_chunk_address(
                    requested
                )
            )
            canonical = normalization.canonical

            content = _generate_chunk_content_cached(
                self.config,
                canonical.x,
                canonical.y,
                canonical.z,
            )

            if content.address != canonical:
                raise GeoreferencingConfigurationError(
                    "Generatorcache lieferte eine falsche Chunkadresse.",
                    details={
                        "expectedChunkKey": canonical.key,
                        "actualChunkKey": content.address.key,
                    },
                )

            result = EarthGeneratedChunk(
                normalization=normalization,
                config=self.config,
                cells=content.cells,
                palette=content.palette,
                content_fingerprint=(
                    content.content_fingerprint
                ),
            )
            _record_generated_wrapper()
            return result
        except CoordinateError:
            # Bekannte Koordinaten-, Topologie- und
            # Georeferenzierungsfehler behalten Code und Details.
            _record_generation_failure()
            raise
        except Exception as error:
            _record_generation_failure()
            raise GeoreferencingConfigurationError(
                "Earth-Chunkgenerierung ist unerwartet fehlgeschlagen.",
                details={
                    "causeType": type(error).__name__,
                    "generatorType": (
                        self.config.generator_type
                    ),
                },
                cause=error,
            ) from error

    def generate_at(
        self,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
    ) -> EarthGeneratedChunk:
        return self.generate_chunk(
            ChunkPosition(chunk_x, chunk_y, chunk_z)
        )

    def generate_batch(
        self,
        addresses: Iterable[
            ChunkAddress
            | ChunkPosition
            | Mapping[str, Any]
            | Sequence[int]
        ],
        *,
        deduplicate_canonical: bool = False,
        maximum_batch_size: int = _MAX_BATCH_SIZE,
    ) -> tuple[EarthGeneratedChunk, ...]:
        """Generiert eine begrenzte Batchfolge in Eingabereihenfolge.

        Bei ``deduplicate_canonical=True`` wird jeder physische Chunk genau
        einmal zurückgegeben. Der erste Alias bestimmt die sichtbaren
        Normalisierungsmetadaten.
        """

        _record_batch_call()

        limit = _require_positive_int(
            maximum_batch_size,
            field_name="maximumBatchSize",
        )
        if limit > _MAX_BATCH_SIZE:
            raise GeoreferencingValidationError(
                "maximumBatchSize überschreitet das Generatorlimit.",
                details={
                    "maximumBatchSize": limit,
                    "generatorLimit": _MAX_BATCH_SIZE,
                },
            )

        if isinstance(addresses, (str, bytes, bytearray)):
            raise GeoreferencingValidationError(
                "addresses muss ein Iterable aus Chunkadressen sein.",
                details={
                    "actualType": type(addresses).__name__,
                },
            )

        try:
            iterator = iter(addresses)
        except TypeError as error:
            raise GeoreferencingValidationError(
                "addresses ist nicht iterierbar.",
                details={
                    "actualType": type(addresses).__name__,
                },
                cause=error,
            ) from error

        generated: list[EarthGeneratedChunk] = []
        seen: set[ChunkAddress] = set()

        for index, address in enumerate(iterator):
            if index >= limit:
                raise GeoreferencingValidationError(
                    "Generatorbatch überschreitet die maximale Größe.",
                    details={
                        "maximumBatchSize": limit,
                    },
                )

            chunk = self.generate_chunk(address)
            if (
                deduplicate_canonical
                and chunk.address in seen
            ):
                continue

            seen.add(chunk.address)
            generated.append(chunk)

        return tuple(generated)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "providerId": self.config.provider_id,
            "generatorType": self.config.generator_type,
            "generatorVersion": (
                self.config.generator_version
            ),
            "deterministic": True,
            "topologyAware": True,
            "airOnly": True,
            "materializesOnGenerate": False,
            "config": self.config.to_dict(),
        }


def get_earth_generator_config(
    definition: EarthWorldDefinition | None = None,
    *,
    topology: PeriodicXTopology | None = None,
) -> EarthGeneratorConfig:
    """Liefert eine gecachte Generatorconfig.

    Ohne explizite Topologie wird eine globale Test-/Definitions-Topologie mit
    Speicherursprung 0 verwendet. Konkrete WorldInstances sollen ihre aus dem
    ``EarthGridFrame`` abgeleitete lokale Topologie übergeben.
    """

    active_definition = (
        definition
        if definition is not None
        else load_earth_world_definition()
    )
    if not isinstance(active_definition, EarthWorldDefinition):
        raise GeoreferencingValidationError(
            "definition muss EarthWorldDefinition sein.",
            details={
                "actualType": type(active_definition).__name__,
            },
        )

    active_topology = (
        topology
        if topology is not None
        else get_periodic_x_topology(
            world_width_blocks=(
                active_definition.grid.world_width_cells
            ),
            chunk_size=active_definition.chunk.size,
            north_south_policy=NorthSouthPolicy.BOUNDED,
            minimum_z=(
                active_definition.grid
                .global_block_z_minimum_inclusive
            ),
            maximum_z=(
                active_definition.grid
                .global_block_z_maximum_exclusive
                - 1
            ),
        )
    )

    if not isinstance(active_topology, PeriodicXTopology):
        raise GeoreferencingValidationError(
            "topology muss PeriodicXTopology sein.",
            details={
                "actualType": type(active_topology).__name__,
            },
        )

    return _get_earth_generator_config_cached(
        active_definition,
        active_topology,
    )


@lru_cache(maxsize=_CONFIG_CACHE_SIZE)
def _get_earth_generator_config_cached(
    definition: EarthWorldDefinition,
    topology: PeriodicXTopology,
) -> EarthGeneratorConfig:
    return EarthGeneratorConfig.from_definition(
        definition,
        topology=topology,
    )


def get_earth_flat_periodic_generator(
    config: EarthGeneratorConfig | None = None,
) -> EarthFlatPeriodicGenerator:
    """Liefert eine gecachte unveränderliche Generatorinstanz."""

    active_config = (
        config
        if config is not None
        else get_earth_generator_config()
    )
    if not isinstance(active_config, EarthGeneratorConfig):
        raise GeoreferencingValidationError(
            "config muss EarthGeneratorConfig sein.",
            details={
                "actualType": type(active_config).__name__,
            },
        )

    return _get_earth_flat_periodic_generator_cached(
        active_config
    )


@lru_cache(maxsize=_GENERATOR_CACHE_SIZE)
def _get_earth_flat_periodic_generator_cached(
    config: EarthGeneratorConfig,
) -> EarthFlatPeriodicGenerator:
    return EarthFlatPeriodicGenerator(config=config)


@lru_cache(maxsize=_CHUNK_CONTENT_CACHE_SIZE)
def _generate_chunk_content_cached(
    config: EarthGeneratorConfig,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
) -> _EarthChunkContent:
    address = ChunkAddress.from_coordinates(
        chunk_x,
        chunk_y,
        chunk_z,
    )
    normalized = config.topology.normalize_chunk_address(
        address
    )
    if normalized.changed:
        raise GeoreferencingConfigurationError(
            "Interner Generatorcache-Key ist nicht kanonisch.",
            details={
                "providedChunkKey": address.key,
                "canonicalChunkKey": (
                    normalized.canonical.key
                ),
            },
        )

    cells = _air_cells_cached(
        config.chunk_size,
        config.cell_count,
        config.air_cell_value,
    )
    palette: tuple[str, ...] = ()
    content_fingerprint = _air_content_fingerprint_cached(
        config.chunk_size,
        config.cell_count,
        config.air_cell_value,
        config.cell_value_encoding,
        config.linear_index_order,
    )

    return _EarthChunkContent(
        address=address,
        cells=cells,
        palette=palette,
        content_fingerprint=content_fingerprint,
    )


@lru_cache(maxsize=_AIR_CELL_CACHE_SIZE)
def _air_cells_cached(
    chunk_size: int,
    cell_count: int,
    air_cell_value: int,
) -> tuple[int, ...]:
    expected = checked_cell_count(chunk_size)
    if cell_count != expected:
        raise GeoreferencingConfigurationError(
            "Air-Zellcache erhielt eine inkonsistente Zellanzahl.",
            details={
                "chunkSize": chunk_size,
                "cellCount": cell_count,
                "expectedCellCount": expected,
            },
        )
    if air_cell_value != AIR_CELL_VALUE:
        raise GeoreferencingConfigurationError(
            "Air-Zellcache unterstützt ausschließlich cellValue 0."
        )

    return (air_cell_value,) * cell_count


@lru_cache(maxsize=_AIR_CELL_CACHE_SIZE)
def _air_content_fingerprint_cached(
    chunk_size: int,
    cell_count: int,
    air_cell_value: int,
    cell_value_encoding: str,
    linear_index_order: str,
) -> str:
    payload = {
        "chunkSize": chunk_size,
        "cellCount": cell_count,
        "airCellValue": air_cell_value,
        "palette": [],
        "cellValueEncoding": cell_value_encoding,
        "linearIndexOrder": linear_index_order,
        "uniform": True,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def earth_generator_runtime_status() -> dict[str, JsonValue]:
    """Read-only Smoke-Test des Earth-Generators ohne pyproj oder Datenbank."""

    payload: dict[str, JsonValue] = {
        "ok": False,
        "ready": False,
        "definitionReady": False,
        "configReady": False,
        "generatorReady": False,
        "zeroChunkReady": False,
        "negativeChunkReady": False,
        "periodicAliasReady": False,
        "seamReady": False,
        "chunk": None,
        "cache": earth_generator_cache_info(),
        "errors": [],
    }
    errors: list[JsonValue] = payload["errors"]  # type: ignore[assignment]

    try:
        definition = load_earth_world_definition()
        payload["definitionReady"] = True

        config = get_earth_generator_config(definition)
        payload["configReady"] = True

        generator = get_earth_flat_periodic_generator(
            config
        )
        payload["generatorReady"] = True

        zero = generator.generate_at(0, 0, 0)
        payload["zeroChunkReady"] = bool(
            zero.chunk_key == "0:0:0"
            and zero.cell_count == 4_096
            and zero.non_air_cell_count == 0
            and zero.cell_value_by_index(0) == 0
            and zero.cell_value_by_index(
                zero.cell_count - 1
            )
            == 0
        )
        payload["chunk"] = zero.to_dict(
            include_cells=False
        )

        negative = generator.generate_at(-1, 0, -1)
        payload["negativeChunkReady"] = bool(
            negative.address.x == -1
            and negative.address.z == -1
            and negative.cell_value(
                LocalCellPosition(15, 15, 15)
            )
            == 0
        )

        alias = generator.generate_at(
            config.world_width_chunks,
            0,
            0,
        )
        payload["periodicAliasReady"] = bool(
            alias.address == zero.address
            and alias.canonicalized
            and alias.content_fingerprint
            == zero.content_fingerprint
        )

        last_x = config.topology.half_world_chunks - 1
        first_x = -config.topology.half_world_chunks
        last = generator.generate_at(last_x, 0, 0)
        east = generator.generate_at(last_x + 1, 0, 0)
        payload["seamReady"] = bool(
            last.address.x == last_x
            and east.address.x == first_x
            and east.canonicalized
        )
    except Exception as error:
        errors.append(_safe_error(error))

    payload["cache"] = earth_generator_cache_info()
    payload["ready"] = bool(
        payload["definitionReady"]
        and payload["configReady"]
        and payload["generatorReady"]
        and payload["zeroChunkReady"]
        and payload["negativeChunkReady"]
        and payload["periodicAliasReady"]
        and payload["seamReady"]
    )
    payload["ok"] = bool(
        payload["ready"] and not errors
    )
    return payload


def earth_generator_cache_info() -> dict[str, JsonValue]:
    """Liefert Cache- und Aufrufdiagnostik."""

    with _METRICS_LOCK:
        calls = _GENERATE_CALLS
        batch_calls = _GENERATE_BATCH_CALLS
        wrappers = _GENERATED_WRAPPERS
        failures = _GENERATION_FAILURES

    return {
        "configs": _cache_info_to_dict(
            _get_earth_generator_config_cached.cache_info()
        ),
        "generators": _cache_info_to_dict(
            _get_earth_flat_periodic_generator_cached.cache_info()
        ),
        "chunkContent": _cache_info_to_dict(
            _generate_chunk_content_cached.cache_info()
        ),
        "airCells": _cache_info_to_dict(
            _air_cells_cached.cache_info()
        ),
        "airContentFingerprints": _cache_info_to_dict(
            _air_content_fingerprint_cached.cache_info()
        ),
        "metrics": {
            "generateCalls": calls,
            "generateBatchCalls": batch_calls,
            "generatedWrappers": wrappers,
            "generationFailures": failures,
        },
    }


def clear_earth_generator_caches() -> dict[str, JsonValue]:
    """Leert alle reproduzierbaren Generatorcaches und Metrikzähler."""

    global _GENERATE_CALLS
    global _GENERATE_BATCH_CALLS
    global _GENERATED_WRAPPERS
    global _GENERATION_FAILURES

    _generate_chunk_content_cached.cache_clear()
    _get_earth_flat_periodic_generator_cached.cache_clear()
    _get_earth_generator_config_cached.cache_clear()
    _air_cells_cached.cache_clear()
    _air_content_fingerprint_cached.cache_clear()

    with _METRICS_LOCK:
        _GENERATE_CALLS = 0
        _GENERATE_BATCH_CALLS = 0
        _GENERATED_WRAPPERS = 0
        _GENERATION_FAILURES = 0

    return {
        "ok": True,
        "cleared": [
            "chunk_content",
            "generators",
            "configs",
            "air_cells",
            "air_content_fingerprints",
            "metrics",
        ],
        "remaining": earth_generator_cache_info(),
    }


def _validate_definition_for_generation(
    definition: EarthWorldDefinition,
) -> None:
    failures: list[str] = []

    checks = (
        (
            definition.enabled,
            "provider_disabled",
        ),
        (
            definition.provider_id == PROVIDER_ID,
            "provider_id_mismatch",
        ),
        (
            definition.generator_type == GENERATOR_TYPE,
            "root_generator_type_mismatch",
        ),
        (
            definition.generator.generator_type
            == GENERATOR_TYPE,
            "generator_type_mismatch",
        ),
        (
            definition.generator.deterministic,
            "generator_not_deterministic",
        ),
        (
            definition.generator.topology_aware,
            "generator_not_topology_aware",
        ),
        (
            definition.generator.default_block_type_id
            == DEFAULT_BLOCK_TYPE_ID,
            "default_block_type_mismatch",
        ),
        (
            definition.generator.generation_mode
            == GENERATION_MODE,
            "generation_mode_mismatch",
        ),
        (
            not definition.generator.terrain_surface_generated,
            "terrain_surface_must_be_disabled",
        ),
        (
            not definition.generator.north_south_boundary_generated,
            "north_south_boundary_must_not_generate_cells",
        ),
        (
            not definition.generator.periodic_x_boundary_generated,
            "periodic_x_boundary_must_not_generate_cells",
        ),
        (
            definition.generator.canonicalize_chunk_before_generation,
            "canonicalization_required",
        ),
        (
            definition.generator.snapshot_compatible,
            "snapshot_compatibility_required",
        ),
        (
            definition.generator.event_replay_compatible,
            "event_replay_compatibility_required",
        ),
        (
            definition.chunk.size == definition.grid.chunk_size,
            "chunk_size_grid_mismatch",
        ),
        (
            definition.chunk.linear_index_order == INDEX_ORDER,
            "linear_index_order_mismatch",
        ),
        (
            definition.chunk.canonical_key_required,
            "canonical_chunk_key_required",
        ),
        (
            not definition.chunk.periodic_aliases_allowed,
            "periodic_alias_persistence_forbidden",
        ),
        (
            not definition.chunk.duplicate_periodic_snapshots_allowed,
            "duplicate_periodic_snapshots_forbidden",
        ),
        (
            definition.topology.topology_type == TOPOLOGY_TYPE,
            "topology_type_mismatch",
        ),
        (
            definition.topology.wrap_axes == ("x",),
            "x_must_be_only_wrap_axis",
        ),
    )

    for passed, code in checks:
        if not passed:
            failures.append(code)

    if failures:
        raise GeoreferencingConfigurationError(
            "Die Earth-Definition ist nicht generatorfähig.",
            details={
                "failureCount": len(failures),
                "failures": failures,
                "semanticFingerprint": (
                    definition.semantic_fingerprint
                ),
            },
        )


def _coerce_chunk_address(
    value: (
        ChunkAddress
        | ChunkPosition
        | Mapping[str, Any]
        | Sequence[int]
    ),
) -> ChunkAddress:
    if isinstance(value, ChunkAddress):
        return value
    if isinstance(value, ChunkPosition):
        return ChunkAddress.from_position(value)
    if isinstance(value, Mapping):
        return ChunkAddress.from_mapping(value)
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
    ):
        if len(value) != 3:
            raise GeoreferencingValidationError(
                "Chunksequenz muss genau drei Koordinaten besitzen.",
                details={
                    "actualDimensions": len(value),
                    "expectedDimensions": 3,
                },
            )
        return ChunkAddress.from_position(
            ChunkPosition.from_sequence(value)
        )

    raise GeoreferencingValidationError(
        "Nicht unterstützte Chunkadresse.",
        details={
            "actualType": type(value).__name__,
            "acceptedTypes": [
                "ChunkAddress",
                "ChunkPosition",
                "Mapping",
                "Sequence[3]",
            ],
        },
    )


def _record_generate_call() -> None:
    global _GENERATE_CALLS
    with _METRICS_LOCK:
        _GENERATE_CALLS += 1


def _record_batch_call() -> None:
    global _GENERATE_BATCH_CALLS
    with _METRICS_LOCK:
        _GENERATE_BATCH_CALLS += 1


def _record_generated_wrapper() -> None:
    global _GENERATED_WRAPPERS
    with _METRICS_LOCK:
        _GENERATED_WRAPPERS += 1


def _record_generation_failure() -> None:
    global _GENERATION_FAILURES
    with _METRICS_LOCK:
        _GENERATION_FAILURES += 1


def _require_sha256(
    value: Any,
    *,
    field_name: str,
) -> str:
    normalized = _require_non_empty_text(
        value,
        field_name=field_name,
        maximum_length=64,
    ).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef"
        for character in normalized
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss ein SHA-256-Hash sein.",
            details={"length": len(normalized)},
        )
    return normalized


def _require_exact_text(
    value: Any,
    *,
    expected: str,
    field_name: str,
) -> str:
    normalized = _require_non_empty_text(
        value,
        field_name=field_name,
        maximum_length=256,
    )
    if normalized != expected:
        raise GeoreferencingConfigurationError(
            f"'{field_name}' besitzt einen unerwarteten Wert.",
            details={
                "expected": expected,
                "actual": normalized,
            },
        )
    return normalized


def _require_non_empty_text(
    value: Any,
    *,
    field_name: str,
    maximum_length: int,
) -> str:
    if not isinstance(value, str):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Zeichenfolge sein.",
            details={
                "actualType": type(value).__name__,
            },
        )
    normalized = value.strip()
    if not normalized:
        raise GeoreferencingValidationError(
            f"'{field_name}' darf nicht leer sein."
        )
    if len(normalized) > maximum_length:
        raise GeoreferencingValidationError(
            f"'{field_name}' überschreitet die maximale Länge.",
            details={
                "length": len(normalized),
                "maximumLength": maximum_length,
            },
        )
    return normalized


def _require_positive_int(
    value: Any,
    *,
    field_name: str,
) -> int:
    normalized = _require_non_negative_int(
        value,
        field_name=field_name,
    )
    if normalized == 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss größer als 0 sein.",
            details={"value": normalized},
        )
    return normalized


def _require_non_negative_int(
    value: Any,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine ganze Zahl sein.",
            details={
                "actualType": type(value).__name__,
            },
        )
    if value < 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' darf nicht negativ sein.",
            details={"value": value},
        )
    return value


def _cache_info_to_dict(cache_info: Any) -> dict[str, JsonValue]:
    return {
        "hits": int(cache_info.hits),
        "misses": int(cache_info.misses),
        "maxSize": (
            int(cache_info.maxsize)
            if cache_info.maxsize is not None
            else None
        ),
        "currentSize": int(cache_info.currsize),
    }


def _safe_error(error: BaseException) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "type": type(error).__name__,
        "message": (
            str(error).strip()
            or "Earth-Chunkgenerierung fehlgeschlagen."
        ),
    }
    code = getattr(error, "code", None)
    if code is not None:
        payload["code"] = str(code)
    return payload


__all__ = [
    "AIR_CELL_VALUE",
    "CELL_VALUE_ENCODING",
    "CHUNK_CONTENT_SCHEMA_VERSION",
    "EarthFlatPeriodicGenerator",
    "EarthGeneratedChunk",
    "EarthGeneratorConfig",
    "clear_earth_generator_caches",
    "earth_generator_cache_info",
    "earth_generator_runtime_status",
    "get_earth_flat_periodic_generator",
    "get_earth_generator_config",
]
