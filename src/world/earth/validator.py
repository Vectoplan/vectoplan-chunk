# services/vectoplan-chunk/src/world/earth/validator.py
"""Parser und Cross-Field-Validator der statischen Earth-v1-Definition.

Das Modul lädt ``world.json`` in unveränderliche, typisierte Verträge und
prüft nicht nur einzelne Felder, sondern auch alle wesentlichen Beziehungen
zwischen Provideridentität, Raster, Topologie, Chunkformat, Referenzpunkt,
Generator, Spawn, Persistenz und Runtime.

Architekturregeln
-----------------
* Ein ungültiges Manifest erzeugt nie eine teilweise gültige Definition.
* ``validate_earth_world_definition`` sammelt mehrere Fehler in einem Lauf.
* ``load_earth_world_definition`` schlägt bei Fehlern mit einem Domänenfehler
  fehl und liefert ausschließlich eine vollständig validierte Definition.
* Unbekannte Felder sind im v1-Schema standardmäßig Fehler. Für kontrollierte
  Vorwärtskompatibilitätsprüfungen können sie als Warnungen behandelt werden.
* Dateicaches verwenden Pfad, Änderungszeit, Größe und Validierungsmodus.
* Manifest- und Semantik-Fingerprints sind SHA-256-basiert.
* Die Validierung startet weder pyproj noch Datenbank-, Generator- oder
  Provider-Runtime.
* Vollständige Dateipfade werden in Diagnosen nur gehasht ausgegeben.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, ClassVar, Final, TypeAlias

from ...coordinates.models import JsonValue
from ...georeferencing.errors import (
    GeoreferencingConfigurationError,
    GeoreferencingValidationError,
)


MANIFEST_SCHEMA_VERSION: Final[str] = "earth-world-definition.v1"
PROVIDER_CONTRACT_VERSION: Final[str] = "earth-provider.v1"
PROVIDER_ID: Final[str] = "earth"
TEMPLATE_ID: Final[str] = "earth"
PROVIDER_WORLD_ID: Final[str] = "earth"
WORLD_TYPE: Final[str] = "earth"
GENERATOR_TYPE: Final[str] = "earth-flat-periodic"
TOPOLOGY_TYPE: Final[str] = "periodic-x-v1"
COORDINATE_SYSTEM_ID: Final[str] = "vectoplan-earth-grid-v1"
AXIS_CONVENTION: Final[str] = "x-east-y-up-z-north"
GRID_ID: Final[str] = "vectoplan-earth-grid"
GRID_VERSION: Final[str] = "1"
PROJECTION_ID: Final[str] = "vectoplan-periodic-equirectangular"
PROJECTION_VERSION: Final[str] = "1"
STORAGE_ORIGIN_POLICY: Final[str] = "global-chunk-origin-floor-v1"
CANONICAL_GEOGRAPHIC_CRS_ID: Final[str] = "EPSG:4979"
CANONICAL_GEOCENTRIC_CRS_ID: Final[str] = "EPSG:4978"
DEFAULT_BLOCK_TYPE_ID: Final[str] = "system_air"
GENERATION_MODE: Final[str] = "air-only-v1"
MINIMUM_PYPROJ_VERSION: Final[str] = "3.7.0"

_MAX_MANIFEST_SIZE_BYTES: Final[int] = 1_048_576
_DEFINITION_CACHE_SIZE: Final[int] = 32
_MAX_ISSUES_IN_EXCEPTION: Final[int] = 50
_MAX_STRING_LENGTH: Final[int] = 4_096
_MAX_SEQUENCE_ITEMS: Final[int] = 1_024
_MAX_JSON_DEPTH: Final[int] = 32
_VERSION_COMPONENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d+")

PathComponent: TypeAlias = str | int
JsonPath: TypeAlias = tuple[PathComponent, ...]


class ValidationSeverity(StrEnum):
    """Schweregrad eines Manifestbefunds."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class EarthWorldValidationIssue:
    """Ein einzelner, serialisierbarer Validierungsbefund."""

    severity: ValidationSeverity
    code: str
    path: JsonPath
    message: str
    expected: JsonValue = None
    actual: JsonValue = None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, ValidationSeverity):
            object.__setattr__(
                self,
                "severity",
                ValidationSeverity(str(self.severity)),
            )
        object.__setattr__(self, "code", _require_identifier(self.code, "code"))
        object.__setattr__(self, "path", tuple(self.path))
        object.__setattr__(
            self,
            "message",
            _require_text(self.message, "message", maximum_length=2_048),
        )

    @property
    def json_pointer(self) -> str:
        if not self.path:
            return ""
        return "/" + "/".join(
            str(component).replace("~", "~0").replace("/", "~1")
            for component in self.path
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "path": list(self.path),
            "jsonPointer": self.json_pointer,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True, slots=True)
class CoordinateSpacesContract:
    persisted: str
    chunk: str
    cell: str
    sub_cell: str
    global_input: str
    derived_earth_grid: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "persisted": self.persisted,
            "chunk": self.chunk,
            "cell": self.cell,
            "subCell": self.sub_cell,
            "globalInput": self.global_input,
            "derivedEarthGrid": self.derived_earth_grid,
        }


@dataclass(frozen=True, slots=True)
class EarthGridContract:
    grid_id: str
    grid_version: str
    projection_id: str
    projection_version: str
    topology_type: str
    axis_convention: str
    world_width_cells: int
    world_height_cells: int
    half_world_width_cells: int
    half_world_height_cells: int
    chunk_size: int
    world_width_chunks: int
    world_height_chunks: int
    central_meridian_deg: Decimal
    pole_exclusion_epsilon_deg: Decimal
    vertical_meters_per_cell: Decimal
    horizontal_scale_policy: str
    horizontal_distance_semantics: str
    storage_origin_policy: str
    canonical_block_x_minimum_inclusive: int
    canonical_block_x_maximum_exclusive: int
    global_block_z_minimum_inclusive: int
    global_block_z_maximum_exclusive: int
    poles_addressable: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "gridId": self.grid_id,
            "gridVersion": self.grid_version,
            "projectionId": self.projection_id,
            "projectionVersion": self.projection_version,
            "topologyType": self.topology_type,
            "axisConvention": self.axis_convention,
            "worldWidthCells": self.world_width_cells,
            "worldHeightCells": self.world_height_cells,
            "halfWorldWidthCells": self.half_world_width_cells,
            "halfWorldHeightCells": self.half_world_height_cells,
            "chunkSize": self.chunk_size,
            "worldWidthChunks": self.world_width_chunks,
            "worldHeightChunks": self.world_height_chunks,
            "centralMeridianDeg": _decimal_text(self.central_meridian_deg),
            "poleExclusionEpsilonDeg": _decimal_text(
                self.pole_exclusion_epsilon_deg
            ),
            "verticalMetersPerCell": _decimal_text(
                self.vertical_meters_per_cell
            ),
            "horizontalScalePolicy": self.horizontal_scale_policy,
            "horizontalDistanceSemantics": self.horizontal_distance_semantics,
            "storageOriginPolicy": self.storage_origin_policy,
            "canonicalBlockXRange": {
                "minimumInclusive": self.canonical_block_x_minimum_inclusive,
                "maximumExclusive": self.canonical_block_x_maximum_exclusive,
            },
            "globalBlockZRange": {
                "minimumInclusive": self.global_block_z_minimum_inclusive,
                "maximumExclusive": self.global_block_z_maximum_exclusive,
            },
            "polesAddressable": self.poles_addressable,
        }


@dataclass(frozen=True, slots=True)
class EarthTopologyContract:
    topology_type: str
    wrap_axes: tuple[str, ...]
    non_wrap_axes: tuple[str, ...]
    x_range: str
    x_minimum_inclusive: int
    x_maximum_exclusive: int
    antipodal_canonical_value: int
    normalize_before_chunk_key: bool
    normalize_before_persistence: bool
    normalize_before_database_lookup: bool
    north_south_policy: str
    north_south_minimum_inclusive: int
    north_south_maximum_inclusive: int
    north_south_wrap: bool
    poles_addressable: bool
    vertical_policy: str
    vertical_wrap: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "type": self.topology_type,
            "wrapAxes": list(self.wrap_axes),
            "nonWrapAxes": list(self.non_wrap_axes),
            "xCanonicalization": {
                "range": self.x_range,
                "minimumInclusive": self.x_minimum_inclusive,
                "maximumExclusive": self.x_maximum_exclusive,
                "antipodalCanonicalValue": self.antipodal_canonical_value,
                "normalizeBeforeChunkKey": self.normalize_before_chunk_key,
                "normalizeBeforePersistence": self.normalize_before_persistence,
                "normalizeBeforeDatabaseLookup": (
                    self.normalize_before_database_lookup
                ),
            },
            "northSouth": {
                "policy": self.north_south_policy,
                "minimumGlobalBlockZInclusive": (
                    self.north_south_minimum_inclusive
                ),
                "maximumGlobalBlockZInclusive": (
                    self.north_south_maximum_inclusive
                ),
                "wrap": self.north_south_wrap,
                "polesAddressable": self.poles_addressable,
            },
            "vertical": {
                "policy": self.vertical_policy,
                "wrap": self.vertical_wrap,
            },
        }


@dataclass(frozen=True, slots=True)
class EarthChunkContract:
    size: int
    shape: str
    coordinate_type: str
    cell_coordinate_type: str
    key_format: str
    linear_index_order: str
    canonical_key_required: bool
    periodic_aliases_allowed: bool
    duplicate_periodic_snapshots_allowed: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "size": self.size,
            "shape": self.shape,
            "coordinateType": self.coordinate_type,
            "cellCoordinateType": self.cell_coordinate_type,
            "keyFormat": self.key_format,
            "linearIndexOrder": self.linear_index_order,
            "canonicalKeyRequired": self.canonical_key_required,
            "periodicAliasesAllowed": self.periodic_aliases_allowed,
            "duplicatePeriodicSnapshotsAllowed": (
                self.duplicate_periodic_snapshots_allowed
            ),
        }


@dataclass(frozen=True, slots=True)
class EarthGlobalReferenceContract:
    required: bool
    cardinality: str
    persisted: bool
    coordinate_precision: str
    crs_required: bool
    crs_guessing_allowed: bool
    trusted_metadata_crs_read_allowed: bool
    supported_coordinate_dimensions: tuple[int, ...]
    canonical_geographic_crs_id: str
    canonical_geocentric_crs_id: str
    allow_ballpark_transformations: bool
    require_best_available_transformation: bool
    always_xy: bool
    default_maximum_roundtrip_error_m: Decimal
    mutable_before_materialization: bool
    mutable_after_materialization: bool
    normal_reanchor_allowed: bool
    reanchor_requires_dedicated_migration: bool
    materialization_lock_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "required": self.required,
            "cardinality": self.cardinality,
            "persisted": self.persisted,
            "coordinatePrecision": self.coordinate_precision,
            "crsRequired": self.crs_required,
            "crsGuessingFromNumericValuesAllowed": self.crs_guessing_allowed,
            "trustedMetadataCrsReadAllowed": (
                self.trusted_metadata_crs_read_allowed
            ),
            "supportedCoordinateDimensions": list(
                self.supported_coordinate_dimensions
            ),
            "canonicalGeographicCrsId": self.canonical_geographic_crs_id,
            "canonicalGeocentricCrsId": self.canonical_geocentric_crs_id,
            "allowBallparkTransformations": (
                self.allow_ballpark_transformations
            ),
            "requireBestAvailableTransformation": (
                self.require_best_available_transformation
            ),
            "alwaysXy": self.always_xy,
            "defaultMaximumRoundtripErrorM": _decimal_text(
                self.default_maximum_roundtrip_error_m
            ),
            "mutableBeforeMaterialization": (
                self.mutable_before_materialization
            ),
            "mutableAfterMaterialization": self.mutable_after_materialization,
            "normalReanchorAllowed": self.normal_reanchor_allowed,
            "reanchorRequiresDedicatedMigration": (
                self.reanchor_requires_dedicated_migration
            ),
            "materializationLockReasons": list(
                self.materialization_lock_reasons
            ),
        }


@dataclass(frozen=True, slots=True)
class EarthStorageFrameContract:
    persisted: bool
    cacheable: bool
    reproducible_from_global_reference: bool
    origin_policy: str
    chunk_aligned_axes: tuple[str, ...]
    reference_point_may_be_sub_cell: bool
    reference_local_position_derived: bool
    rotation_allowed: bool
    regional_runtime_crs_allowed: bool
    per_project_grid_phase_allowed: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "persisted": self.persisted,
            "cacheable": self.cacheable,
            "reproducibleFromGlobalReference": (
                self.reproducible_from_global_reference
            ),
            "originPolicy": self.origin_policy,
            "chunkAlignedAxes": list(self.chunk_aligned_axes),
            "referencePointMayBeSubCell": self.reference_point_may_be_sub_cell,
            "referenceLocalPositionDerived": (
                self.reference_local_position_derived
            ),
            "rotationAllowed": self.rotation_allowed,
            "regionalRuntimeCrsAllowed": self.regional_runtime_crs_allowed,
            "perProjectGridPhaseAllowed": self.per_project_grid_phase_allowed,
        }


@dataclass(frozen=True, slots=True)
class EarthGeneratorContract:
    generator_type: str
    version: str
    deterministic: bool
    topology_aware: bool
    default_block_type_id: str
    generation_mode: str
    terrain_surface_generated: bool
    north_south_boundary_generated: bool
    periodic_x_boundary_generated: bool
    canonicalize_chunk_before_generation: bool
    snapshot_compatible: bool
    event_replay_compatible: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "type": self.generator_type,
            "version": self.version,
            "deterministic": self.deterministic,
            "topologyAware": self.topology_aware,
            "defaultBlockTypeId": self.default_block_type_id,
            "generationMode": self.generation_mode,
            "terrainSurfaceGenerated": self.terrain_surface_generated,
            "northSouthBoundaryGenerated": self.north_south_boundary_generated,
            "periodicXBoundaryGenerated": self.periodic_x_boundary_generated,
            "canonicalizeChunkBeforeGeneration": (
                self.canonicalize_chunk_before_generation
            ),
            "snapshotCompatible": self.snapshot_compatible,
            "eventReplayCompatible": self.event_replay_compatible,
        }


@dataclass(frozen=True, slots=True)
class EarthSpawnContract:
    persisted_coordinate_space: str
    default_policy: str
    global_coordinate_input_supported: bool
    explicit_crs_required_for_global_input: bool
    move_changes_global_reference: bool
    move_reanchors_world: bool
    x_canonicalized_before_persistence: bool
    north_south_bounds_validated: bool
    vertical_requires_resolved_reference_height: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "persistedCoordinateSpace": self.persisted_coordinate_space,
            "defaultPolicy": self.default_policy,
            "globalCoordinateInputSupported": (
                self.global_coordinate_input_supported
            ),
            "explicitCrsRequiredForGlobalInput": (
                self.explicit_crs_required_for_global_input
            ),
            "moveChangesGlobalReference": self.move_changes_global_reference,
            "moveReanchorsWorld": self.move_reanchors_world,
            "xCanonicalizedBeforePersistence": (
                self.x_canonicalized_before_persistence
            ),
            "northSouthBoundsValidated": self.north_south_bounds_validated,
            "verticalRequiresResolvedReferenceHeight": (
                self.vertical_requires_resolved_reference_height
            ),
        }


@dataclass(frozen=True, slots=True)
class EarthPersistenceContract:
    blocks_stored_globally: bool
    chunks_stored_globally: bool
    events_stored_globally: bool
    commands_stored_globally: bool
    objects_stored_globally: bool
    players_stored_globally: bool
    spawn_stored_globally: bool
    derived_global_coordinates_persisted_per_entity: bool
    global_reference_record_count: int
    canonicalize_before_write: bool
    canonicalize_before_read: bool
    canonicalize_before_chunk_key: bool
    canonicalize_before_snapshot_lookup: bool
    canonicalize_dirty_chunks: bool
    deduplicate_canonical_dirty_chunks: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "blocksStoredGlobally": self.blocks_stored_globally,
            "chunksStoredGlobally": self.chunks_stored_globally,
            "eventsStoredGlobally": self.events_stored_globally,
            "commandsStoredGlobally": self.commands_stored_globally,
            "objectsStoredGlobally": self.objects_stored_globally,
            "playersStoredGlobally": self.players_stored_globally,
            "spawnStoredGlobally": self.spawn_stored_globally,
            "derivedGlobalCoordinatesPersistedPerEntity": (
                self.derived_global_coordinates_persisted_per_entity
            ),
            "globalReferenceRecordCount": self.global_reference_record_count,
            "canonicalizeBeforeWrite": self.canonicalize_before_write,
            "canonicalizeBeforeRead": self.canonicalize_before_read,
            "canonicalizeBeforeChunkKey": self.canonicalize_before_chunk_key,
            "canonicalizeBeforeSnapshotLookup": (
                self.canonicalize_before_snapshot_lookup
            ),
            "canonicalizeDirtyChunks": self.canonicalize_dirty_chunks,
            "deduplicateCanonicalDirtyChunks": (
                self.deduplicate_canonical_dirty_chunks
            ),
        }


@dataclass(frozen=True, slots=True)
class EarthCapabilityContract:
    chunk_generation: bool
    chunk_snapshots: bool
    chunk_events: bool
    block_commands: bool
    batch_commands: bool
    global_reference: bool
    global_to_local_conversion: bool
    local_to_global_conversion: bool
    global_spawn_input: bool
    periodic_x: bool
    periodic_z: bool
    normal_reanchor: bool
    terrain_import: bool
    regional_crs: bool
    project_grid_rotation: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "chunkGeneration": self.chunk_generation,
            "chunkSnapshots": self.chunk_snapshots,
            "chunkEvents": self.chunk_events,
            "blockCommands": self.block_commands,
            "batchCommands": self.batch_commands,
            "globalReference": self.global_reference,
            "globalToLocalConversion": self.global_to_local_conversion,
            "localToGlobalConversion": self.local_to_global_conversion,
            "globalSpawnInput": self.global_spawn_input,
            "periodicX": self.periodic_x,
            "periodicZ": self.periodic_z,
            "normalReanchor": self.normal_reanchor,
            "terrainImport": self.terrain_import,
            "regionalCrs": self.regional_crs,
            "projectGridRotation": self.project_grid_rotation,
        }


@dataclass(frozen=True, slots=True)
class EarthCompatibilityContract:
    shared_chunk_math: bool
    shared_snapshot_format: bool
    shared_event_format: bool
    shared_command_path: bool
    flat_provider_unchanged: bool
    flat_provider_default_behavior_unchanged: bool
    provider_selection_required: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "sharedChunkMath": self.shared_chunk_math,
            "sharedSnapshotFormat": self.shared_snapshot_format,
            "sharedEventFormat": self.shared_event_format,
            "sharedCommandPath": self.shared_command_path,
            "flatProviderUnchanged": self.flat_provider_unchanged,
            "flatProviderDefaultBehaviorUnchanged": (
                self.flat_provider_default_behavior_unchanged
            ),
            "providerSelectionRequired": self.provider_selection_required,
        }


@dataclass(frozen=True, slots=True)
class EarthRuntimeContract:
    requires_pyproj: bool
    minimum_pyproj_version: str
    requires_proj_database: bool
    proj_network_enabled_by_default: bool
    automatic_grid_download_allowed: bool
    readiness_requires_canonical_crs: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "requiresPyproj": self.requires_pyproj,
            "minimumPyprojVersion": self.minimum_pyproj_version,
            "requiresProjDatabase": self.requires_proj_database,
            "projNetworkEnabledByDefault": self.proj_network_enabled_by_default,
            "automaticGridDownloadAllowed": (
                self.automatic_grid_download_allowed
            ),
            "readinessRequiresCanonicalCrs": list(
                self.readiness_requires_canonical_crs
            ),
        }


@dataclass(frozen=True, slots=True)
class EarthObservabilityContract:
    emit_canonicalization_metrics: bool
    emit_wrap_count_metrics: bool
    emit_transformer_cache_metrics: bool
    emit_earth_frame_cache_metrics: bool
    include_full_crs_definitions_in_logs: bool
    include_full_transform_pipelines_in_logs: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "emitCanonicalizationMetrics": self.emit_canonicalization_metrics,
            "emitWrapCountMetrics": self.emit_wrap_count_metrics,
            "emitTransformerCacheMetrics": self.emit_transformer_cache_metrics,
            "emitEarthFrameCacheMetrics": self.emit_earth_frame_cache_metrics,
            "includeFullCrsDefinitionsInLogs": (
                self.include_full_crs_definitions_in_logs
            ),
            "includeFullTransformPipelinesInLogs": (
                self.include_full_transform_pipelines_in_logs
            ),
        }


@dataclass(frozen=True, slots=True)
class EarthWorldDefinition:
    """Vollständig validierter, unveränderlicher Earth-v1-Vertrag."""

    schema_version: str
    definition_version: int
    provider_contract_version: str
    provider_id: str
    template_id: str
    provider_world_id: str
    world_type: str
    display_name: str
    description: str
    enabled: bool
    generator_type: str
    generator_version: str
    topology_type: str
    coordinate_system_id: str
    axis_convention: str
    coordinate_spaces: CoordinateSpacesContract
    grid_id: str
    grid_version: str
    grid: EarthGridContract
    topology: EarthTopologyContract
    chunk: EarthChunkContract
    global_reference: EarthGlobalReferenceContract
    storage_frame: EarthStorageFrameContract
    generator: EarthGeneratorContract
    spawn: EarthSpawnContract
    persistence: EarthPersistenceContract
    capabilities: EarthCapabilityContract
    compatibility: EarthCompatibilityContract
    runtime: EarthRuntimeContract
    observability: EarthObservabilityContract
    manifest_fingerprint: str | None = None
    source_path_fingerprint: str | None = None

    contract_version: ClassVar[str] = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        expected_identity = (
            self.schema_version == MANIFEST_SCHEMA_VERSION
            and self.provider_contract_version == PROVIDER_CONTRACT_VERSION
            and self.provider_id == PROVIDER_ID
            and self.template_id == TEMPLATE_ID
            and self.provider_world_id == PROVIDER_WORLD_ID
            and self.world_type == WORLD_TYPE
        )
        if not expected_identity:
            raise GeoreferencingValidationError(
                "EarthWorldDefinition besitzt eine ungültige Provideridentität."
            )

        for field_name, value, expected_type in (
            ("coordinate_spaces", self.coordinate_spaces, CoordinateSpacesContract),
            ("grid", self.grid, EarthGridContract),
            ("topology", self.topology, EarthTopologyContract),
            ("chunk", self.chunk, EarthChunkContract),
            ("global_reference", self.global_reference, EarthGlobalReferenceContract),
            ("storage_frame", self.storage_frame, EarthStorageFrameContract),
            ("generator", self.generator, EarthGeneratorContract),
            ("spawn", self.spawn, EarthSpawnContract),
            ("persistence", self.persistence, EarthPersistenceContract),
            ("capabilities", self.capabilities, EarthCapabilityContract),
            ("compatibility", self.compatibility, EarthCompatibilityContract),
            ("runtime", self.runtime, EarthRuntimeContract),
            ("observability", self.observability, EarthObservabilityContract),
        ):
            if not isinstance(value, expected_type):
                raise GeoreferencingValidationError(
                    f"'{field_name}' besitzt einen ungültigen Vertragstyp."
                )

    @property
    def semantic_fingerprint(self) -> str:
        canonical = json.dumps(
            self.semantic_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def semantic_payload(self) -> dict[str, JsonValue]:
        return {
            "$comment": "services/vectoplan-chunk/src/world/earth/world.json",
            "schemaVersion": self.schema_version,
            "definitionVersion": self.definition_version,
            "providerContractVersion": self.provider_contract_version,
            "providerId": self.provider_id,
            "templateId": self.template_id,
            "providerWorldId": self.provider_world_id,
            "worldType": self.world_type,
            "displayName": self.display_name,
            "description": self.description,
            "enabled": self.enabled,
            "generatorType": self.generator_type,
            "generatorVersion": self.generator_version,
            "topologyType": self.topology_type,
            "coordinateSystemId": self.coordinate_system_id,
            "axisConvention": self.axis_convention,
            "coordinateSpaces": self.coordinate_spaces.to_dict(),
            "gridId": self.grid_id,
            "gridVersion": self.grid_version,
            "grid": self.grid.to_dict(),
            "topology": self.topology.to_dict(),
            "chunk": self.chunk.to_dict(),
            "globalReference": self.global_reference.to_dict(),
            "storageFrame": self.storage_frame.to_dict(),
            "generator": self.generator.to_dict(),
            "spawn": self.spawn.to_dict(),
            "persistence": self.persistence.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "compatibility": self.compatibility.to_dict(),
            "runtime": self.runtime.to_dict(),
            "observability": self.observability.to_dict(),
        }

    def to_dict(self, *, include_source: bool = True) -> dict[str, JsonValue]:
        payload = self.semantic_payload()
        payload["semanticFingerprint"] = self.semantic_fingerprint
        if include_source:
            payload["manifestFingerprint"] = self.manifest_fingerprint
            payload["sourcePathFingerprint"] = self.source_path_fingerprint
        return payload

    def to_earth_grid_definition(self) -> Any:
        """Erzeugt die Georeferenzierungs-Griddefinition ohne pyproj-Start."""

        try:
            from ...georeferencing.earth_grid import get_earth_grid_definition
        except Exception as error:
            raise GeoreferencingConfigurationError(
                "EarthGridDefinition konnte nicht importiert werden.",
                details={"causeType": type(error).__name__},
                cause=error,
            ) from error

        definition = get_earth_grid_definition(
            grid_id=self.grid.grid_id,
            grid_version=self.grid.grid_version,
            world_width_cells=self.grid.world_width_cells,
            world_height_cells=self.grid.world_height_cells,
            chunk_size=self.grid.chunk_size,
            meters_per_cell=self.grid.vertical_meters_per_cell,
            central_meridian_deg=self.grid.central_meridian_deg,
            pole_exclusion_epsilon_deg=(
                self.grid.pole_exclusion_epsilon_deg
            ),
        )

        if definition.grid.projection_id != self.grid.projection_id:
            raise GeoreferencingConfigurationError(
                "Validiertes Manifest und EarthGridDefinition widersprechen sich."
            )
        return definition


@dataclass(frozen=True, slots=True)
class EarthWorldValidationResult:
    """Gesammeltes Ergebnis einer Manifestvalidierung."""

    definition: EarthWorldDefinition | None
    issues: tuple[EarthWorldValidationIssue, ...]
    manifest_fingerprint: str | None = None
    source_path_fingerprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        if self.definition is not None and not isinstance(
            self.definition,
            EarthWorldDefinition,
        ):
            raise GeoreferencingValidationError(
                "definition muss EarthWorldDefinition oder None sein."
            )

        if self.errors and self.definition is not None:
            raise GeoreferencingValidationError(
                "Ein fehlerhaftes Ergebnis darf keine Definition enthalten."
            )

    @property
    def errors(self) -> tuple[EarthWorldValidationIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is ValidationSeverity.ERROR
        )

    @property
    def warnings(self) -> tuple[EarthWorldValidationIssue, ...]:
        return tuple(
            issue
            for issue in self.issues
            if issue.severity is ValidationSeverity.WARNING
        )

    @property
    def valid(self) -> bool:
        return not self.errors and self.definition is not None

    def raise_for_errors(self) -> None:
        if self.valid:
            return

        issue_payload = [
            issue.to_dict()
            for issue in self.errors[:_MAX_ISSUES_IN_EXCEPTION]
        ]
        raise GeoreferencingConfigurationError(
            "Die statische Earth-Providerdefinition ist ungültig.",
            details={
                "errorCount": len(self.errors),
                "warningCount": len(self.warnings),
                "issues": issue_payload,
                "issuesTruncated": (
                    len(self.errors) > _MAX_ISSUES_IN_EXCEPTION
                ),
                "manifestFingerprint": self.manifest_fingerprint,
                "sourcePathFingerprint": self.source_path_fingerprint,
            },
        )

    def to_dict(
        self,
        *,
        include_definition: bool = False,
    ) -> dict[str, JsonValue]:
        return {
            "ok": self.valid,
            "valid": self.valid,
            "errorCount": len(self.errors),
            "warningCount": len(self.warnings),
            "manifestFingerprint": self.manifest_fingerprint,
            "sourcePathFingerprint": self.source_path_fingerprint,
            "semanticFingerprint": (
                self.definition.semantic_fingerprint
                if self.definition is not None
                else None
            ),
            "issues": [issue.to_dict() for issue in self.issues],
            "definition": (
                self.definition.to_dict()
                if include_definition and self.definition is not None
                else None
            ),
        }


class _ValidationContext:
    def __init__(self, *, allow_unknown_fields: bool) -> None:
        self.allow_unknown_fields = bool(allow_unknown_fields)
        self.issues: list[EarthWorldValidationIssue] = []

    def error(
        self,
        code: str,
        path: JsonPath,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        self.issues.append(
            EarthWorldValidationIssue(
                severity=ValidationSeverity.ERROR,
                code=code,
                path=path,
                message=message,
                expected=_safe_json(expected),
                actual=_safe_json(actual),
            )
        )

    def warning(
        self,
        code: str,
        path: JsonPath,
        message: str,
        *,
        expected: Any = None,
        actual: Any = None,
    ) -> None:
        self.issues.append(
            EarthWorldValidationIssue(
                severity=ValidationSeverity.WARNING,
                code=code,
                path=path,
                message=message,
                expected=_safe_json(expected),
                actual=_safe_json(actual),
            )
        )

    @property
    def has_errors(self) -> bool:
        return any(
            issue.severity is ValidationSeverity.ERROR
            for issue in self.issues
        )

    def check_unknown_fields(
        self,
        mapping: Mapping[str, Any],
        *,
        allowed: set[str],
        path: JsonPath,
    ) -> None:
        for key in mapping:
            if key in allowed:
                continue
            method = self.warning if self.allow_unknown_fields else self.error
            method(
                "unknown_field",
                (*path, key),
                "Das Feld ist im Earth-v1-Schema nicht definiert.",
                expected=sorted(allowed),
                actual=key,
            )

    def mapping(
        self,
        value: Any,
        *,
        path: JsonPath,
    ) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            self.error(
                "type_mismatch",
                path,
                "Es wurde ein JSON-Objekt erwartet.",
                expected="object",
                actual=type(value).__name__,
            )
            return None
        return value

    def required_mapping(
        self,
        mapping: Mapping[str, Any],
        key: str,
        *,
        path: JsonPath,
    ) -> Mapping[str, Any] | None:
        if key not in mapping:
            self.error(
                "required_field_missing",
                (*path, key),
                "Ein Pflichtobjekt fehlt.",
                expected="object",
            )
            return None
        return self.mapping(mapping[key], path=(*path, key))

    def string(
        self,
        mapping: Mapping[str, Any],
        key: str,
        *,
        path: JsonPath,
        maximum_length: int = _MAX_STRING_LENGTH,
    ) -> str | None:
        if key not in mapping:
            self.error(
                "required_field_missing",
                (*path, key),
                "Ein Pflichtfeld fehlt.",
                expected="string",
            )
            return None
        value = mapping[key]
        if not isinstance(value, str):
            self.error(
                "type_mismatch",
                (*path, key),
                "Es wurde eine Zeichenfolge erwartet.",
                expected="string",
                actual=type(value).__name__,
            )
            return None
        normalized = value.strip()
        if not normalized:
            self.error(
                "empty_string",
                (*path, key),
                "Die Zeichenfolge darf nicht leer sein.",
            )
            return None
        if len(normalized) > maximum_length:
            self.error(
                "string_too_long",
                (*path, key),
                "Die Zeichenfolge überschreitet die maximale Länge.",
                expected=maximum_length,
                actual=len(normalized),
            )
            return None
        return normalized

    def boolean(
        self,
        mapping: Mapping[str, Any],
        key: str,
        *,
        path: JsonPath,
    ) -> bool | None:
        if key not in mapping:
            self.error(
                "required_field_missing",
                (*path, key),
                "Ein Pflichtfeld fehlt.",
                expected="boolean",
            )
            return None
        value = mapping[key]
        if not isinstance(value, bool):
            self.error(
                "type_mismatch",
                (*path, key),
                "Es wurde ein Boolean erwartet.",
                expected="boolean",
                actual=type(value).__name__,
            )
            return None
        return value

    def integer(
        self,
        mapping: Mapping[str, Any],
        key: str,
        *,
        path: JsonPath,
    ) -> int | None:
        if key not in mapping:
            self.error(
                "required_field_missing",
                (*path, key),
                "Ein Pflichtfeld fehlt.",
                expected="integer",
            )
            return None
        value = mapping[key]
        if isinstance(value, bool) or not isinstance(value, int):
            self.error(
                "type_mismatch",
                (*path, key),
                "Es wurde eine ganze Zahl erwartet.",
                expected="integer",
                actual=type(value).__name__,
            )
            return None
        return value

    def decimal(
        self,
        mapping: Mapping[str, Any],
        key: str,
        *,
        path: JsonPath,
    ) -> Decimal | None:
        if key not in mapping:
            self.error(
                "required_field_missing",
                (*path, key),
                "Ein Pflichtfeld fehlt.",
                expected="decimal string",
            )
            return None
        value = mapping[key]
        if isinstance(value, bool) or not isinstance(
            value,
            (str, int, float, Decimal),
        ):
            self.error(
                "type_mismatch",
                (*path, key),
                "Es wurde eine Dezimalzahl erwartet.",
                expected="decimal string",
                actual=type(value).__name__,
            )
            return None
        try:
            normalized = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as error:
            self.error(
                "invalid_decimal",
                (*path, key),
                "Die Dezimalzahl ist ungültig.",
                actual=type(error).__name__,
            )
            return None
        if not normalized.is_finite():
            self.error(
                "non_finite_decimal",
                (*path, key),
                "Die Dezimalzahl muss endlich sein.",
                actual=str(normalized),
            )
            return None
        return normalized

    def string_tuple(
        self,
        mapping: Mapping[str, Any],
        key: str,
        *,
        path: JsonPath,
    ) -> tuple[str, ...] | None:
        sequence = self._sequence(mapping, key, path=path)
        if sequence is None:
            return None
        result: list[str] = []
        for index, value in enumerate(sequence):
            if not isinstance(value, str) or not value.strip():
                self.error(
                    "type_mismatch",
                    (*path, key, index),
                    "Es wurde eine nicht-leere Zeichenfolge erwartet.",
                    expected="string",
                    actual=type(value).__name__,
                )
                continue
            result.append(value.strip())
        return tuple(result)

    def int_tuple(
        self,
        mapping: Mapping[str, Any],
        key: str,
        *,
        path: JsonPath,
    ) -> tuple[int, ...] | None:
        sequence = self._sequence(mapping, key, path=path)
        if sequence is None:
            return None
        result: list[int] = []
        for index, value in enumerate(sequence):
            if isinstance(value, bool) or not isinstance(value, int):
                self.error(
                    "type_mismatch",
                    (*path, key, index),
                    "Es wurde eine ganze Zahl erwartet.",
                    expected="integer",
                    actual=type(value).__name__,
                )
                continue
            result.append(value)
        return tuple(result)

    def _sequence(
        self,
        mapping: Mapping[str, Any],
        key: str,
        *,
        path: JsonPath,
    ) -> Sequence[Any] | None:
        if key not in mapping:
            self.error(
                "required_field_missing",
                (*path, key),
                "Ein Pflichtfeld fehlt.",
                expected="array",
            )
            return None
        value = mapping[key]
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(
            value,
            Sequence,
        ):
            self.error(
                "type_mismatch",
                (*path, key),
                "Es wurde ein Array erwartet.",
                expected="array",
                actual=type(value).__name__,
            )
            return None
        if len(value) > _MAX_SEQUENCE_ITEMS:
            self.error(
                "array_too_large",
                (*path, key),
                "Das Array enthält zu viele Elemente.",
                expected=_MAX_SEQUENCE_ITEMS,
                actual=len(value),
            )
            return None
        return value


def validate_earth_world_definition(
    payload: Mapping[str, Any] | str | bytes | bytearray,
    *,
    allow_unknown_fields: bool = False,
    manifest_fingerprint: str | None = None,
    source_path: str | Path | None = None,
) -> EarthWorldValidationResult:
    """Validiert ein bereits geladenes Manifest ohne Dateisystemzugriff."""

    source_path_fingerprint = (
        _path_fingerprint(Path(source_path))
        if source_path is not None
        else None
    )

    try:
        normalized_payload, calculated_fingerprint = _normalize_payload(payload)
    except Exception as error:
        issue = EarthWorldValidationIssue(
            severity=ValidationSeverity.ERROR,
            code="manifest_parse_failed",
            path=(),
            message="Das Earth-Manifest konnte nicht als JSON-Objekt gelesen werden.",
            actual=type(error).__name__,
        )
        return EarthWorldValidationResult(
            definition=None,
            issues=(issue,),
            manifest_fingerprint=manifest_fingerprint,
            source_path_fingerprint=source_path_fingerprint,
        )

    effective_fingerprint = manifest_fingerprint or calculated_fingerprint
    context = _ValidationContext(
        allow_unknown_fields=allow_unknown_fields
    )
    definition = _parse_definition(
        normalized_payload,
        context=context,
        manifest_fingerprint=effective_fingerprint,
        source_path_fingerprint=source_path_fingerprint,
    )

    return EarthWorldValidationResult(
        definition=(None if context.has_errors else definition),
        issues=tuple(context.issues),
        manifest_fingerprint=effective_fingerprint,
        source_path_fingerprint=source_path_fingerprint,
    )


def load_earth_world_definition(
    path: str | Path | None = None,
    *,
    allow_unknown_fields: bool = False,
    use_cache: bool = True,
) -> EarthWorldDefinition:
    """Lädt und validiert ``world.json`` atomar.

    Bei ungültigem Inhalt wird keine teilweise Definition zurückgegeben.
    """

    target = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().with_name("world.json")
    )

    try:
        resolved = target.expanduser().resolve(strict=True)
        stat_result = resolved.stat()
    except FileNotFoundError as error:
        raise GeoreferencingConfigurationError(
            "Die statische Earth-Providerdefinition fehlt.",
            details={
                "sourcePathFingerprint": _path_fingerprint(target),
                "filename": target.name,
            },
            cause=error,
        ) from error
    except OSError as error:
        raise GeoreferencingConfigurationError(
            "Die statische Earth-Providerdefinition konnte nicht geprüft werden.",
            details={
                "sourcePathFingerprint": _path_fingerprint(target),
                "filename": target.name,
                "causeType": type(error).__name__,
            },
            cause=error,
        ) from error

    if not resolved.is_file():
        raise GeoreferencingConfigurationError(
            "Der Earth-Manifestpfad ist keine reguläre Datei.",
            details={
                "sourcePathFingerprint": _path_fingerprint(resolved),
                "filename": resolved.name,
            },
        )

    size = int(stat_result.st_size)
    if size <= 0 or size > _MAX_MANIFEST_SIZE_BYTES:
        raise GeoreferencingConfigurationError(
            "Die Earth-Providerdefinition ist leer oder zu groß.",
            details={
                "sizeBytes": size,
                "maximumSizeBytes": _MAX_MANIFEST_SIZE_BYTES,
                "sourcePathFingerprint": _path_fingerprint(resolved),
            },
        )

    if use_cache:
        result = _load_earth_world_definition_cached(
            str(resolved),
            int(stat_result.st_mtime_ns),
            size,
            bool(allow_unknown_fields),
        )
    else:
        result = _load_earth_world_definition_uncached(
            resolved,
            expected_mtime_ns=int(stat_result.st_mtime_ns),
            expected_size=size,
            allow_unknown_fields=bool(allow_unknown_fields),
        )

    result.raise_for_errors()
    assert result.definition is not None
    return result.definition


@lru_cache(maxsize=_DEFINITION_CACHE_SIZE)
def _load_earth_world_definition_cached(
    path_text: str,
    mtime_ns: int,
    size_bytes: int,
    allow_unknown_fields: bool,
) -> EarthWorldValidationResult:
    return _load_earth_world_definition_uncached(
        Path(path_text),
        expected_mtime_ns=mtime_ns,
        expected_size=size_bytes,
        allow_unknown_fields=allow_unknown_fields,
    )


def _load_earth_world_definition_uncached(
    path: Path,
    *,
    expected_mtime_ns: int,
    expected_size: int,
    allow_unknown_fields: bool,
) -> EarthWorldValidationResult:
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as error:
        raise GeoreferencingConfigurationError(
            "Die Earth-Providerdefinition konnte nicht gelesen werden.",
            details={
                "sourcePathFingerprint": _path_fingerprint(path),
                "causeType": type(error).__name__,
            },
            cause=error,
        ) from error

    stable = (
        int(before.st_mtime_ns) == expected_mtime_ns
        and int(after.st_mtime_ns) == expected_mtime_ns
        and int(before.st_size) == expected_size
        and int(after.st_size) == expected_size
        and len(raw) == expected_size
    )
    if not stable:
        raise GeoreferencingConfigurationError(
            "Die Earth-Providerdefinition wurde während des Lesens verändert.",
            details={
                "sourcePathFingerprint": _path_fingerprint(path),
                "expectedMtimeNs": expected_mtime_ns,
                "expectedSizeBytes": expected_size,
            },
        )

    manifest_fingerprint = sha256(raw).hexdigest()
    return validate_earth_world_definition(
        raw,
        allow_unknown_fields=allow_unknown_fields,
        manifest_fingerprint=manifest_fingerprint,
        source_path=path,
    )


def earth_world_definition_status(
    path: str | Path | None = None,
    *,
    allow_unknown_fields: bool = False,
    include_definition: bool = False,
) -> dict[str, JsonValue]:
    """Liefert eine read-only Readiness-Diagnose der Providerdefinition."""

    target = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().with_name("world.json")
    )

    try:
        definition = load_earth_world_definition(
            target,
            allow_unknown_fields=allow_unknown_fields,
            use_cache=True,
        )
        grid_definition = definition.to_earth_grid_definition()
        payload: dict[str, JsonValue] = {
            "ok": True,
            "ready": True,
            "schemaVersion": definition.schema_version,
            "definitionVersion": definition.definition_version,
            "providerId": definition.provider_id,
            "generatorType": definition.generator_type,
            "topologyType": definition.topology_type,
            "gridId": definition.grid_id,
            "gridVersion": definition.grid_version,
            "manifestFingerprint": definition.manifest_fingerprint,
            "semanticFingerprint": definition.semantic_fingerprint,
            "sourcePathFingerprint": definition.source_path_fingerprint,
            "earthGridDefinitionReady": True,
            "earthGridDefinitionFingerprint": grid_definition.fingerprint,
            "warningCount": 0,
            "warnings": [],
            "cache": earth_world_definition_cache_info(),
            "errors": [],
        }
        if include_definition:
            payload["definition"] = definition.to_dict()
        return payload
    except Exception as error:
        details = getattr(error, "details", None)
        return {
            "ok": False,
            "ready": False,
            "schemaVersion": None,
            "definitionVersion": None,
            "providerId": None,
            "generatorType": None,
            "topologyType": None,
            "gridId": None,
            "gridVersion": None,
            "manifestFingerprint": None,
            "semanticFingerprint": None,
            "sourcePathFingerprint": _path_fingerprint(target),
            "earthGridDefinitionReady": False,
            "earthGridDefinitionFingerprint": None,
            "warningCount": 0,
            "warnings": [],
            "cache": earth_world_definition_cache_info(),
            "errors": [
                {
                    "type": type(error).__name__,
                    "code": str(getattr(error, "code", "earth_definition_invalid")),
                    "message": str(error).strip()
                    or "Earth-Definition ist nicht bereit.",
                    "details": _safe_json(details),
                }
            ],
        }


def clear_earth_world_definition_cache() -> dict[str, JsonValue]:
    """Leert ausschließlich den reproduzierbaren Definitionscache."""

    _load_earth_world_definition_cached.cache_clear()
    return {
        "ok": True,
        "cleared": ["earth_world_definition"],
        "remaining": earth_world_definition_cache_info(),
    }


def earth_world_definition_cache_info() -> dict[str, JsonValue]:
    info = _load_earth_world_definition_cached.cache_info()
    return {
        "hits": int(info.hits),
        "misses": int(info.misses),
        "maxSize": int(info.maxsize) if info.maxsize is not None else None,
        "currentSize": int(info.currsize),
    }


def _parse_definition(
    payload: Mapping[str, Any],
    *,
    context: _ValidationContext,
    manifest_fingerprint: str | None,
    source_path_fingerprint: str | None,
) -> EarthWorldDefinition | None:
    root_allowed = {
        "$comment",
        "schemaVersion",
        "definitionVersion",
        "providerContractVersion",
        "providerId",
        "templateId",
        "providerWorldId",
        "worldType",
        "displayName",
        "description",
        "enabled",
        "generatorType",
        "generatorVersion",
        "topologyType",
        "coordinateSystemId",
        "axisConvention",
        "coordinateSpaces",
        "gridId",
        "gridVersion",
        "grid",
        "topology",
        "chunk",
        "globalReference",
        "storageFrame",
        "generator",
        "spawn",
        "persistence",
        "capabilities",
        "compatibility",
        "runtime",
        "observability",
    }
    context.check_unknown_fields(payload, allowed=root_allowed, path=())

    comment = context.string(payload, "$comment", path=())
    schema_version = context.string(payload, "schemaVersion", path=())
    definition_version = context.integer(payload, "definitionVersion", path=())
    provider_contract_version = context.string(
        payload,
        "providerContractVersion",
        path=(),
    )
    provider_id = context.string(payload, "providerId", path=())
    template_id = context.string(payload, "templateId", path=())
    provider_world_id = context.string(payload, "providerWorldId", path=())
    world_type = context.string(payload, "worldType", path=())
    display_name = context.string(payload, "displayName", path=())
    description = context.string(payload, "description", path=())
    enabled = context.boolean(payload, "enabled", path=())
    generator_type = context.string(payload, "generatorType", path=())
    generator_version = context.string(payload, "generatorVersion", path=())
    topology_type = context.string(payload, "topologyType", path=())
    coordinate_system_id = context.string(
        payload,
        "coordinateSystemId",
        path=(),
    )
    axis_convention = context.string(payload, "axisConvention", path=())
    grid_id = context.string(payload, "gridId", path=())
    grid_version = context.string(payload, "gridVersion", path=())

    coordinate_spaces = _parse_coordinate_spaces(
        context.required_mapping(payload, "coordinateSpaces", path=()),
        context=context,
        path=("coordinateSpaces",),
    )
    grid = _parse_grid(
        context.required_mapping(payload, "grid", path=()),
        context=context,
        path=("grid",),
    )
    topology = _parse_topology(
        context.required_mapping(payload, "topology", path=()),
        context=context,
        path=("topology",),
    )
    chunk = _parse_chunk(
        context.required_mapping(payload, "chunk", path=()),
        context=context,
        path=("chunk",),
    )
    global_reference = _parse_global_reference(
        context.required_mapping(payload, "globalReference", path=()),
        context=context,
        path=("globalReference",),
    )
    storage_frame = _parse_storage_frame(
        context.required_mapping(payload, "storageFrame", path=()),
        context=context,
        path=("storageFrame",),
    )
    generator = _parse_generator(
        context.required_mapping(payload, "generator", path=()),
        context=context,
        path=("generator",),
    )
    spawn = _parse_spawn(
        context.required_mapping(payload, "spawn", path=()),
        context=context,
        path=("spawn",),
    )
    persistence = _parse_persistence(
        context.required_mapping(payload, "persistence", path=()),
        context=context,
        path=("persistence",),
    )
    capabilities = _parse_capabilities(
        context.required_mapping(payload, "capabilities", path=()),
        context=context,
        path=("capabilities",),
    )
    compatibility = _parse_compatibility(
        context.required_mapping(payload, "compatibility", path=()),
        context=context,
        path=("compatibility",),
    )
    runtime = _parse_runtime(
        context.required_mapping(payload, "runtime", path=()),
        context=context,
        path=("runtime",),
    )
    observability = _parse_observability(
        context.required_mapping(payload, "observability", path=()),
        context=context,
        path=("observability",),
    )

    _validate_cross_field_invariants(
        context=context,
        comment=comment,
        schema_version=schema_version,
        definition_version=definition_version,
        provider_contract_version=provider_contract_version,
        provider_id=provider_id,
        template_id=template_id,
        provider_world_id=provider_world_id,
        world_type=world_type,
        enabled=enabled,
        generator_type=generator_type,
        generator_version=generator_version,
        topology_type=topology_type,
        coordinate_system_id=coordinate_system_id,
        axis_convention=axis_convention,
        grid_id=grid_id,
        grid_version=grid_version,
        coordinate_spaces=coordinate_spaces,
        grid=grid,
        topology=topology,
        chunk=chunk,
        global_reference=global_reference,
        storage_frame=storage_frame,
        generator=generator,
        spawn=spawn,
        persistence=persistence,
        capabilities=capabilities,
        compatibility=compatibility,
        runtime=runtime,
        observability=observability,
    )

    required_values = (
        schema_version,
        definition_version,
        provider_contract_version,
        provider_id,
        template_id,
        provider_world_id,
        world_type,
        display_name,
        description,
        enabled,
        generator_type,
        generator_version,
        topology_type,
        coordinate_system_id,
        axis_convention,
        coordinate_spaces,
        grid_id,
        grid_version,
        grid,
        topology,
        chunk,
        global_reference,
        storage_frame,
        generator,
        spawn,
        persistence,
        capabilities,
        compatibility,
        runtime,
        observability,
    )
    if context.has_errors or any(value is None for value in required_values):
        return None

    return EarthWorldDefinition(
        schema_version=schema_version,
        definition_version=definition_version,
        provider_contract_version=provider_contract_version,
        provider_id=provider_id,
        template_id=template_id,
        provider_world_id=provider_world_id,
        world_type=world_type,
        display_name=display_name,
        description=description,
        enabled=enabled,
        generator_type=generator_type,
        generator_version=generator_version,
        topology_type=topology_type,
        coordinate_system_id=coordinate_system_id,
        axis_convention=axis_convention,
        coordinate_spaces=coordinate_spaces,
        grid_id=grid_id,
        grid_version=grid_version,
        grid=grid,
        topology=topology,
        chunk=chunk,
        global_reference=global_reference,
        storage_frame=storage_frame,
        generator=generator,
        spawn=spawn,
        persistence=persistence,
        capabilities=capabilities,
        compatibility=compatibility,
        runtime=runtime,
        observability=observability,
        manifest_fingerprint=manifest_fingerprint,
        source_path_fingerprint=source_path_fingerprint,
    )


def _parse_coordinate_spaces(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> CoordinateSpacesContract | None:
    if mapping is None:
        return None
    allowed = {
        "persisted",
        "chunk",
        "cell",
        "subCell",
        "globalInput",
        "derivedEarthGrid",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    values = (
        context.string(mapping, "persisted", path=path),
        context.string(mapping, "chunk", path=path),
        context.string(mapping, "cell", path=path),
        context.string(mapping, "subCell", path=path),
        context.string(mapping, "globalInput", path=path),
        context.string(mapping, "derivedEarthGrid", path=path),
    )
    if any(value is None for value in values):
        return None
    return CoordinateSpacesContract(*values)


def _parse_grid(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthGridContract | None:
    if mapping is None:
        return None
    allowed = {
        "gridId",
        "gridVersion",
        "projectionId",
        "projectionVersion",
        "topologyType",
        "axisConvention",
        "worldWidthCells",
        "worldHeightCells",
        "halfWorldWidthCells",
        "halfWorldHeightCells",
        "chunkSize",
        "worldWidthChunks",
        "worldHeightChunks",
        "centralMeridianDeg",
        "poleExclusionEpsilonDeg",
        "verticalMetersPerCell",
        "horizontalScalePolicy",
        "horizontalDistanceSemantics",
        "storageOriginPolicy",
        "canonicalBlockXRange",
        "globalBlockZRange",
        "polesAddressable",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    x_range = context.required_mapping(mapping, "canonicalBlockXRange", path=path)
    z_range = context.required_mapping(mapping, "globalBlockZRange", path=path)
    if x_range is not None:
        context.check_unknown_fields(
            x_range,
            allowed={"minimumInclusive", "maximumExclusive"},
            path=(*path, "canonicalBlockXRange"),
        )
    if z_range is not None:
        context.check_unknown_fields(
            z_range,
            allowed={"minimumInclusive", "maximumExclusive"},
            path=(*path, "globalBlockZRange"),
        )

    values = (
        context.string(mapping, "gridId", path=path),
        context.string(mapping, "gridVersion", path=path),
        context.string(mapping, "projectionId", path=path),
        context.string(mapping, "projectionVersion", path=path),
        context.string(mapping, "topologyType", path=path),
        context.string(mapping, "axisConvention", path=path),
        context.integer(mapping, "worldWidthCells", path=path),
        context.integer(mapping, "worldHeightCells", path=path),
        context.integer(mapping, "halfWorldWidthCells", path=path),
        context.integer(mapping, "halfWorldHeightCells", path=path),
        context.integer(mapping, "chunkSize", path=path),
        context.integer(mapping, "worldWidthChunks", path=path),
        context.integer(mapping, "worldHeightChunks", path=path),
        context.decimal(mapping, "centralMeridianDeg", path=path),
        context.decimal(mapping, "poleExclusionEpsilonDeg", path=path),
        context.decimal(mapping, "verticalMetersPerCell", path=path),
        context.string(mapping, "horizontalScalePolicy", path=path),
        context.string(mapping, "horizontalDistanceSemantics", path=path),
        context.string(mapping, "storageOriginPolicy", path=path),
        (
            context.integer(
                x_range,
                "minimumInclusive",
                path=(*path, "canonicalBlockXRange"),
            )
            if x_range is not None
            else None
        ),
        (
            context.integer(
                x_range,
                "maximumExclusive",
                path=(*path, "canonicalBlockXRange"),
            )
            if x_range is not None
            else None
        ),
        (
            context.integer(
                z_range,
                "minimumInclusive",
                path=(*path, "globalBlockZRange"),
            )
            if z_range is not None
            else None
        ),
        (
            context.integer(
                z_range,
                "maximumExclusive",
                path=(*path, "globalBlockZRange"),
            )
            if z_range is not None
            else None
        ),
        context.boolean(mapping, "polesAddressable", path=path),
    )
    if any(value is None for value in values):
        return None
    return EarthGridContract(*values)


def _parse_topology(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthTopologyContract | None:
    if mapping is None:
        return None
    allowed = {"type", "wrapAxes", "nonWrapAxes", "xCanonicalization", "northSouth", "vertical"}
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    x = context.required_mapping(mapping, "xCanonicalization", path=path)
    ns = context.required_mapping(mapping, "northSouth", path=path)
    vertical = context.required_mapping(mapping, "vertical", path=path)
    if x is not None:
        context.check_unknown_fields(
            x,
            allowed={
                "range",
                "minimumInclusive",
                "maximumExclusive",
                "antipodalCanonicalValue",
                "normalizeBeforeChunkKey",
                "normalizeBeforePersistence",
                "normalizeBeforeDatabaseLookup",
            },
            path=(*path, "xCanonicalization"),
        )
    if ns is not None:
        context.check_unknown_fields(
            ns,
            allowed={
                "policy",
                "minimumGlobalBlockZInclusive",
                "maximumGlobalBlockZInclusive",
                "wrap",
                "polesAddressable",
            },
            path=(*path, "northSouth"),
        )
    if vertical is not None:
        context.check_unknown_fields(
            vertical,
            allowed={"policy", "wrap"},
            path=(*path, "vertical"),
        )

    values = (
        context.string(mapping, "type", path=path),
        context.string_tuple(mapping, "wrapAxes", path=path),
        context.string_tuple(mapping, "nonWrapAxes", path=path),
        context.string(x, "range", path=(*path, "xCanonicalization")) if x else None,
        context.integer(x, "minimumInclusive", path=(*path, "xCanonicalization")) if x else None,
        context.integer(x, "maximumExclusive", path=(*path, "xCanonicalization")) if x else None,
        context.integer(x, "antipodalCanonicalValue", path=(*path, "xCanonicalization")) if x else None,
        context.boolean(x, "normalizeBeforeChunkKey", path=(*path, "xCanonicalization")) if x else None,
        context.boolean(x, "normalizeBeforePersistence", path=(*path, "xCanonicalization")) if x else None,
        context.boolean(x, "normalizeBeforeDatabaseLookup", path=(*path, "xCanonicalization")) if x else None,
        context.string(ns, "policy", path=(*path, "northSouth")) if ns else None,
        context.integer(ns, "minimumGlobalBlockZInclusive", path=(*path, "northSouth")) if ns else None,
        context.integer(ns, "maximumGlobalBlockZInclusive", path=(*path, "northSouth")) if ns else None,
        context.boolean(ns, "wrap", path=(*path, "northSouth")) if ns else None,
        context.boolean(ns, "polesAddressable", path=(*path, "northSouth")) if ns else None,
        context.string(vertical, "policy", path=(*path, "vertical")) if vertical else None,
        context.boolean(vertical, "wrap", path=(*path, "vertical")) if vertical else None,
    )
    if any(value is None for value in values):
        return None
    return EarthTopologyContract(*values)


def _parse_chunk(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthChunkContract | None:
    if mapping is None:
        return None
    allowed = {
        "size", "shape", "coordinateType", "cellCoordinateType", "keyFormat",
        "linearIndexOrder", "canonicalKeyRequired", "periodicAliasesAllowed",
        "duplicatePeriodicSnapshotsAllowed",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    values = (
        context.integer(mapping, "size", path=path),
        context.string(mapping, "shape", path=path),
        context.string(mapping, "coordinateType", path=path),
        context.string(mapping, "cellCoordinateType", path=path),
        context.string(mapping, "keyFormat", path=path),
        context.string(mapping, "linearIndexOrder", path=path),
        context.boolean(mapping, "canonicalKeyRequired", path=path),
        context.boolean(mapping, "periodicAliasesAllowed", path=path),
        context.boolean(mapping, "duplicatePeriodicSnapshotsAllowed", path=path),
    )
    if any(value is None for value in values):
        return None
    return EarthChunkContract(*values)


def _parse_global_reference(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthGlobalReferenceContract | None:
    if mapping is None:
        return None
    allowed = {
        "required", "cardinality", "persisted", "coordinatePrecision", "crsRequired",
        "crsGuessingFromNumericValuesAllowed", "trustedMetadataCrsReadAllowed",
        "supportedCoordinateDimensions", "canonicalGeographicCrsId",
        "canonicalGeocentricCrsId", "allowBallparkTransformations",
        "requireBestAvailableTransformation", "alwaysXy",
        "defaultMaximumRoundtripErrorM", "mutableBeforeMaterialization",
        "mutableAfterMaterialization", "normalReanchorAllowed",
        "reanchorRequiresDedicatedMigration", "materializationLockReasons",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    values = (
        context.boolean(mapping, "required", path=path),
        context.string(mapping, "cardinality", path=path),
        context.boolean(mapping, "persisted", path=path),
        context.string(mapping, "coordinatePrecision", path=path),
        context.boolean(mapping, "crsRequired", path=path),
        context.boolean(mapping, "crsGuessingFromNumericValuesAllowed", path=path),
        context.boolean(mapping, "trustedMetadataCrsReadAllowed", path=path),
        context.int_tuple(mapping, "supportedCoordinateDimensions", path=path),
        context.string(mapping, "canonicalGeographicCrsId", path=path),
        context.string(mapping, "canonicalGeocentricCrsId", path=path),
        context.boolean(mapping, "allowBallparkTransformations", path=path),
        context.boolean(mapping, "requireBestAvailableTransformation", path=path),
        context.boolean(mapping, "alwaysXy", path=path),
        context.decimal(mapping, "defaultMaximumRoundtripErrorM", path=path),
        context.boolean(mapping, "mutableBeforeMaterialization", path=path),
        context.boolean(mapping, "mutableAfterMaterialization", path=path),
        context.boolean(mapping, "normalReanchorAllowed", path=path),
        context.boolean(mapping, "reanchorRequiresDedicatedMigration", path=path),
        context.string_tuple(mapping, "materializationLockReasons", path=path),
    )
    if any(value is None for value in values):
        return None
    return EarthGlobalReferenceContract(*values)


def _parse_storage_frame(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthStorageFrameContract | None:
    if mapping is None:
        return None
    allowed = {
        "persisted", "cacheable", "reproducibleFromGlobalReference", "originPolicy",
        "chunkAlignedAxes", "referencePointMayBeSubCell",
        "referenceLocalPositionDerived", "rotationAllowed",
        "regionalRuntimeCrsAllowed", "perProjectGridPhaseAllowed",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    values = (
        context.boolean(mapping, "persisted", path=path),
        context.boolean(mapping, "cacheable", path=path),
        context.boolean(mapping, "reproducibleFromGlobalReference", path=path),
        context.string(mapping, "originPolicy", path=path),
        context.string_tuple(mapping, "chunkAlignedAxes", path=path),
        context.boolean(mapping, "referencePointMayBeSubCell", path=path),
        context.boolean(mapping, "referenceLocalPositionDerived", path=path),
        context.boolean(mapping, "rotationAllowed", path=path),
        context.boolean(mapping, "regionalRuntimeCrsAllowed", path=path),
        context.boolean(mapping, "perProjectGridPhaseAllowed", path=path),
    )
    if any(value is None for value in values):
        return None
    return EarthStorageFrameContract(*values)


def _parse_generator(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthGeneratorContract | None:
    if mapping is None:
        return None
    allowed = {
        "type", "version", "deterministic", "topologyAware", "defaultBlockTypeId",
        "generationMode", "terrainSurfaceGenerated", "northSouthBoundaryGenerated",
        "periodicXBoundaryGenerated", "canonicalizeChunkBeforeGeneration",
        "snapshotCompatible", "eventReplayCompatible",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    values = (
        context.string(mapping, "type", path=path),
        context.string(mapping, "version", path=path),
        context.boolean(mapping, "deterministic", path=path),
        context.boolean(mapping, "topologyAware", path=path),
        context.string(mapping, "defaultBlockTypeId", path=path),
        context.string(mapping, "generationMode", path=path),
        context.boolean(mapping, "terrainSurfaceGenerated", path=path),
        context.boolean(mapping, "northSouthBoundaryGenerated", path=path),
        context.boolean(mapping, "periodicXBoundaryGenerated", path=path),
        context.boolean(mapping, "canonicalizeChunkBeforeGeneration", path=path),
        context.boolean(mapping, "snapshotCompatible", path=path),
        context.boolean(mapping, "eventReplayCompatible", path=path),
    )
    if any(value is None for value in values):
        return None
    return EarthGeneratorContract(*values)


def _parse_spawn(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthSpawnContract | None:
    if mapping is None:
        return None
    allowed = {
        "persistedCoordinateSpace", "defaultPolicy", "globalCoordinateInputSupported",
        "explicitCrsRequiredForGlobalInput", "moveChangesGlobalReference",
        "moveReanchorsWorld", "xCanonicalizedBeforePersistence",
        "northSouthBoundsValidated", "verticalRequiresResolvedReferenceHeight",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    values = (
        context.string(mapping, "persistedCoordinateSpace", path=path),
        context.string(mapping, "defaultPolicy", path=path),
        context.boolean(mapping, "globalCoordinateInputSupported", path=path),
        context.boolean(mapping, "explicitCrsRequiredForGlobalInput", path=path),
        context.boolean(mapping, "moveChangesGlobalReference", path=path),
        context.boolean(mapping, "moveReanchorsWorld", path=path),
        context.boolean(mapping, "xCanonicalizedBeforePersistence", path=path),
        context.boolean(mapping, "northSouthBoundsValidated", path=path),
        context.boolean(mapping, "verticalRequiresResolvedReferenceHeight", path=path),
    )
    if any(value is None for value in values):
        return None
    return EarthSpawnContract(*values)


def _parse_persistence(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthPersistenceContract | None:
    if mapping is None:
        return None
    allowed = {
        "blocksStoredGlobally", "chunksStoredGlobally", "eventsStoredGlobally",
        "commandsStoredGlobally", "objectsStoredGlobally", "playersStoredGlobally",
        "spawnStoredGlobally", "derivedGlobalCoordinatesPersistedPerEntity",
        "globalReferenceRecordCount", "canonicalizeBeforeWrite",
        "canonicalizeBeforeRead", "canonicalizeBeforeChunkKey",
        "canonicalizeBeforeSnapshotLookup", "canonicalizeDirtyChunks",
        "deduplicateCanonicalDirtyChunks",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    values = (
        context.boolean(mapping, "blocksStoredGlobally", path=path),
        context.boolean(mapping, "chunksStoredGlobally", path=path),
        context.boolean(mapping, "eventsStoredGlobally", path=path),
        context.boolean(mapping, "commandsStoredGlobally", path=path),
        context.boolean(mapping, "objectsStoredGlobally", path=path),
        context.boolean(mapping, "playersStoredGlobally", path=path),
        context.boolean(mapping, "spawnStoredGlobally", path=path),
        context.boolean(mapping, "derivedGlobalCoordinatesPersistedPerEntity", path=path),
        context.integer(mapping, "globalReferenceRecordCount", path=path),
        context.boolean(mapping, "canonicalizeBeforeWrite", path=path),
        context.boolean(mapping, "canonicalizeBeforeRead", path=path),
        context.boolean(mapping, "canonicalizeBeforeChunkKey", path=path),
        context.boolean(mapping, "canonicalizeBeforeSnapshotLookup", path=path),
        context.boolean(mapping, "canonicalizeDirtyChunks", path=path),
        context.boolean(mapping, "deduplicateCanonicalDirtyChunks", path=path),
    )
    if any(value is None for value in values):
        return None
    return EarthPersistenceContract(*values)


def _parse_capabilities(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthCapabilityContract | None:
    if mapping is None:
        return None
    keys = (
        "chunkGeneration", "chunkSnapshots", "chunkEvents", "blockCommands",
        "batchCommands", "globalReference", "globalToLocalConversion",
        "localToGlobalConversion", "globalSpawnInput", "periodicX", "periodicZ",
        "normalReanchor", "terrainImport", "regionalCrs", "projectGridRotation",
    )
    context.check_unknown_fields(mapping, allowed=set(keys), path=path)
    values = tuple(context.boolean(mapping, key, path=path) for key in keys)
    if any(value is None for value in values):
        return None
    return EarthCapabilityContract(*values)


def _parse_compatibility(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthCompatibilityContract | None:
    if mapping is None:
        return None
    keys = (
        "sharedChunkMath", "sharedSnapshotFormat", "sharedEventFormat",
        "sharedCommandPath", "flatProviderUnchanged",
        "flatProviderDefaultBehaviorUnchanged", "providerSelectionRequired",
    )
    context.check_unknown_fields(mapping, allowed=set(keys), path=path)
    values = tuple(context.boolean(mapping, key, path=path) for key in keys)
    if any(value is None for value in values):
        return None
    return EarthCompatibilityContract(*values)


def _parse_runtime(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthRuntimeContract | None:
    if mapping is None:
        return None
    allowed = {
        "requiresPyproj", "minimumPyprojVersion", "requiresProjDatabase",
        "projNetworkEnabledByDefault", "automaticGridDownloadAllowed",
        "readinessRequiresCanonicalCrs",
    }
    context.check_unknown_fields(mapping, allowed=allowed, path=path)
    values = (
        context.boolean(mapping, "requiresPyproj", path=path),
        context.string(mapping, "minimumPyprojVersion", path=path),
        context.boolean(mapping, "requiresProjDatabase", path=path),
        context.boolean(mapping, "projNetworkEnabledByDefault", path=path),
        context.boolean(mapping, "automaticGridDownloadAllowed", path=path),
        context.string_tuple(mapping, "readinessRequiresCanonicalCrs", path=path),
    )
    if any(value is None for value in values):
        return None
    return EarthRuntimeContract(*values)


def _parse_observability(
    mapping: Mapping[str, Any] | None,
    *,
    context: _ValidationContext,
    path: JsonPath,
) -> EarthObservabilityContract | None:
    if mapping is None:
        return None
    keys = (
        "emitCanonicalizationMetrics", "emitWrapCountMetrics",
        "emitTransformerCacheMetrics", "emitEarthFrameCacheMetrics",
        "includeFullCrsDefinitionsInLogs", "includeFullTransformPipelinesInLogs",
    )
    context.check_unknown_fields(mapping, allowed=set(keys), path=path)
    values = tuple(context.boolean(mapping, key, path=path) for key in keys)
    if any(value is None for value in values):
        return None
    return EarthObservabilityContract(*values)


def _validate_cross_field_invariants(
    *,
    context: _ValidationContext,
    comment: str | None,
    schema_version: str | None,
    definition_version: int | None,
    provider_contract_version: str | None,
    provider_id: str | None,
    template_id: str | None,
    provider_world_id: str | None,
    world_type: str | None,
    enabled: bool | None,
    generator_type: str | None,
    generator_version: str | None,
    topology_type: str | None,
    coordinate_system_id: str | None,
    axis_convention: str | None,
    grid_id: str | None,
    grid_version: str | None,
    coordinate_spaces: CoordinateSpacesContract | None,
    grid: EarthGridContract | None,
    topology: EarthTopologyContract | None,
    chunk: EarthChunkContract | None,
    global_reference: EarthGlobalReferenceContract | None,
    storage_frame: EarthStorageFrameContract | None,
    generator: EarthGeneratorContract | None,
    spawn: EarthSpawnContract | None,
    persistence: EarthPersistenceContract | None,
    capabilities: EarthCapabilityContract | None,
    compatibility: EarthCompatibilityContract | None,
    runtime: EarthRuntimeContract | None,
    observability: EarthObservabilityContract | None,
) -> None:
    _expect(context, ("$comment",), comment, "services/vectoplan-chunk/src/world/earth/world.json")
    _expect(context, ("schemaVersion",), schema_version, MANIFEST_SCHEMA_VERSION)
    _expect(context, ("definitionVersion",), definition_version, 1)
    _expect(context, ("providerContractVersion",), provider_contract_version, PROVIDER_CONTRACT_VERSION)
    _expect(context, ("providerId",), provider_id, PROVIDER_ID)
    _expect(context, ("templateId",), template_id, TEMPLATE_ID)
    _expect(context, ("providerWorldId",), provider_world_id, PROVIDER_WORLD_ID)
    _expect(context, ("worldType",), world_type, WORLD_TYPE)
    _expect(context, ("enabled",), enabled, True)
    _expect(context, ("generatorType",), generator_type, GENERATOR_TYPE)
    _expect(context, ("generatorVersion",), generator_version, "1")
    _expect(context, ("topologyType",), topology_type, TOPOLOGY_TYPE)
    _expect(context, ("coordinateSystemId",), coordinate_system_id, COORDINATE_SYSTEM_ID)
    _expect(context, ("axisConvention",), axis_convention, AXIS_CONVENTION)
    _expect(context, ("gridId",), grid_id, GRID_ID)
    _expect(context, ("gridVersion",), grid_version, GRID_VERSION)

    if coordinate_spaces is not None:
        for field, actual, expected in (
            ("persisted", coordinate_spaces.persisted, "local_world"),
            ("chunk", coordinate_spaces.chunk, "chunk"),
            ("cell", coordinate_spaces.cell, "local_cell"),
            ("subCell", coordinate_spaces.sub_cell, "local_metric"),
            ("globalInput", coordinate_spaces.global_input, "explicit_crs"),
            ("derivedEarthGrid", coordinate_spaces.derived_earth_grid, "earth_grid"),
        ):
            _expect(context, ("coordinateSpaces", field), actual, expected)

    if grid is not None:
        for path, actual, expected in (
            (("grid", "gridId"), grid.grid_id, grid_id),
            (("grid", "gridVersion"), grid.grid_version, grid_version),
            (("grid", "projectionId"), grid.projection_id, PROJECTION_ID),
            (("grid", "projectionVersion"), grid.projection_version, PROJECTION_VERSION),
            (("grid", "topologyType"), grid.topology_type, topology_type),
            (("grid", "axisConvention"), grid.axis_convention, axis_convention),
            (("grid", "storageOriginPolicy"), grid.storage_origin_policy, STORAGE_ORIGIN_POLICY),
            (("grid", "horizontalScalePolicy"), grid.horizontal_scale_policy, "normalized-angular-v1"),
            (("grid", "horizontalDistanceSemantics"), grid.horizontal_distance_semantics, "not-globally-metric"),
            (("grid", "polesAddressable"), grid.poles_addressable, False),
        ):
            _expect(context, path, actual, expected)

        _expect_positive(context, ("grid", "worldWidthCells"), grid.world_width_cells)
        _expect_positive(context, ("grid", "worldHeightCells"), grid.world_height_cells)
        _expect_positive(context, ("grid", "chunkSize"), grid.chunk_size)
        _expect_positive_decimal(context, ("grid", "verticalMetersPerCell"), grid.vertical_meters_per_cell)
        _expect_positive_decimal(context, ("grid", "poleExclusionEpsilonDeg"), grid.pole_exclusion_epsilon_deg)
        if grid.pole_exclusion_epsilon_deg >= Decimal("90"):
            context.error(
                "range_violation",
                ("grid", "poleExclusionEpsilonDeg"),
                "Die Polausschluss-Epsilon muss kleiner als 90° sein.",
                expected="0 < epsilon < 90",
                actual=_decimal_text(grid.pole_exclusion_epsilon_deg),
            )
        if grid.central_meridian_deg < Decimal("-180") or grid.central_meridian_deg >= Decimal("180"):
            context.error(
                "range_violation",
                ("grid", "centralMeridianDeg"),
                "Der Zentralmeridian muss im Bereich [-180, 180) liegen.",
                expected="[-180, 180)",
                actual=_decimal_text(grid.central_meridian_deg),
            )

        if grid.world_width_cells > 0:
            _expect(context, ("grid", "halfWorldWidthCells"), grid.half_world_width_cells, grid.world_width_cells // 2)
        if grid.world_height_cells > 0:
            _expect(context, ("grid", "halfWorldHeightCells"), grid.half_world_height_cells, grid.world_height_cells // 2)
        if grid.chunk_size > 0:
            if grid.world_width_cells % grid.chunk_size != 0:
                context.error("chunk_alignment_failed", ("grid", "worldWidthCells"), "Die Weltbreite ist nicht durch die Chunkgröße teilbar.", expected=0, actual=grid.world_width_cells % grid.chunk_size)
            if grid.world_height_cells % grid.chunk_size != 0:
                context.error("chunk_alignment_failed", ("grid", "worldHeightCells"), "Die Welthöhe ist nicht durch die Chunkgröße teilbar.", expected=0, actual=grid.world_height_cells % grid.chunk_size)
            if grid.half_world_width_cells % grid.chunk_size != 0:
                context.error("half_world_alignment_failed", ("grid", "halfWorldWidthCells"), "Die halbe Weltbreite ist nicht chunk-ausgerichtet.", expected=0, actual=grid.half_world_width_cells % grid.chunk_size)
            if grid.half_world_height_cells % grid.chunk_size != 0:
                context.error("half_world_alignment_failed", ("grid", "halfWorldHeightCells"), "Die halbe Welthöhe ist nicht chunk-ausgerichtet.", expected=0, actual=grid.half_world_height_cells % grid.chunk_size)
            _expect(context, ("grid", "worldWidthChunks"), grid.world_width_chunks, grid.world_width_cells // grid.chunk_size)
            _expect(context, ("grid", "worldHeightChunks"), grid.world_height_chunks, grid.world_height_cells // grid.chunk_size)

        _expect(context, ("grid", "canonicalBlockXRange", "minimumInclusive"), grid.canonical_block_x_minimum_inclusive, -grid.half_world_width_cells)
        _expect(context, ("grid", "canonicalBlockXRange", "maximumExclusive"), grid.canonical_block_x_maximum_exclusive, grid.half_world_width_cells)
        _expect(context, ("grid", "globalBlockZRange", "minimumInclusive"), grid.global_block_z_minimum_inclusive, -grid.half_world_height_cells)
        _expect(context, ("grid", "globalBlockZRange", "maximumExclusive"), grid.global_block_z_maximum_exclusive, grid.half_world_height_cells)

    if chunk is not None:
        _expect(context, ("chunk", "size"), chunk.size, grid.chunk_size if grid else 16)
        _expect(context, ("chunk", "shape"), chunk.shape, "cube")
        _expect(context, ("chunk", "coordinateType"), chunk.coordinate_type, "signed-int64")
        _expect(context, ("chunk", "cellCoordinateType"), chunk.cell_coordinate_type, "signed-int32")
        _expect(context, ("chunk", "keyFormat"), chunk.key_format, "x:y:z")
        _expect(context, ("chunk", "linearIndexOrder"), chunk.linear_index_order, "x-fastest-y-then-z")
        _expect(context, ("chunk", "canonicalKeyRequired"), chunk.canonical_key_required, True)
        _expect(context, ("chunk", "periodicAliasesAllowed"), chunk.periodic_aliases_allowed, False)
        _expect(context, ("chunk", "duplicatePeriodicSnapshotsAllowed"), chunk.duplicate_periodic_snapshots_allowed, False)

    if topology is not None:
        _expect(context, ("topology", "type"), topology.topology_type, topology_type)
        _expect(context, ("topology", "wrapAxes"), topology.wrap_axes, ("x",))
        _expect(context, ("topology", "nonWrapAxes"), topology.non_wrap_axes, ("y", "z"))
        _expect(context, ("topology", "xCanonicalization", "range"), topology.x_range, "half-open-centered")
        if grid is not None:
            _expect(context, ("topology", "xCanonicalization", "minimumInclusive"), topology.x_minimum_inclusive, grid.canonical_block_x_minimum_inclusive)
            _expect(context, ("topology", "xCanonicalization", "maximumExclusive"), topology.x_maximum_exclusive, grid.canonical_block_x_maximum_exclusive)
            _expect(context, ("topology", "xCanonicalization", "antipodalCanonicalValue"), topology.antipodal_canonical_value, grid.canonical_block_x_minimum_inclusive)
            _expect(context, ("topology", "northSouth", "minimumGlobalBlockZInclusive"), topology.north_south_minimum_inclusive, grid.global_block_z_minimum_inclusive)
            _expect(context, ("topology", "northSouth", "maximumGlobalBlockZInclusive"), topology.north_south_maximum_inclusive, grid.global_block_z_maximum_exclusive - 1)
        for path, actual, expected in (
            (("topology", "xCanonicalization", "normalizeBeforeChunkKey"), topology.normalize_before_chunk_key, True),
            (("topology", "xCanonicalization", "normalizeBeforePersistence"), topology.normalize_before_persistence, True),
            (("topology", "xCanonicalization", "normalizeBeforeDatabaseLookup"), topology.normalize_before_database_lookup, True),
            (("topology", "northSouth", "policy"), topology.north_south_policy, "bounded"),
            (("topology", "northSouth", "wrap"), topology.north_south_wrap, False),
            (("topology", "northSouth", "polesAddressable"), topology.poles_addressable, False),
            (("topology", "vertical", "policy"), topology.vertical_policy, "unbounded-local"),
            (("topology", "vertical", "wrap"), topology.vertical_wrap, False),
        ):
            _expect(context, path, actual, expected)

    if global_reference is not None:
        expected_lock_reasons = {
            "chunk_snapshots", "chunk_events", "block_commands", "imported_objects",
            "persisted_spawn", "player_state",
        }
        for path, actual, expected in (
            (("globalReference", "required"), global_reference.required, True),
            (("globalReference", "cardinality"), global_reference.cardinality, "exactly-one"),
            (("globalReference", "persisted"), global_reference.persisted, True),
            (("globalReference", "coordinatePrecision"), global_reference.coordinate_precision, "decimal-string"),
            (("globalReference", "crsRequired"), global_reference.crs_required, True),
            (("globalReference", "crsGuessingFromNumericValuesAllowed"), global_reference.crs_guessing_allowed, False),
            (("globalReference", "trustedMetadataCrsReadAllowed"), global_reference.trusted_metadata_crs_read_allowed, True),
            (("globalReference", "supportedCoordinateDimensions"), global_reference.supported_coordinate_dimensions, (2, 3)),
            (("globalReference", "canonicalGeographicCrsId"), global_reference.canonical_geographic_crs_id, CANONICAL_GEOGRAPHIC_CRS_ID),
            (("globalReference", "canonicalGeocentricCrsId"), global_reference.canonical_geocentric_crs_id, CANONICAL_GEOCENTRIC_CRS_ID),
            (("globalReference", "allowBallparkTransformations"), global_reference.allow_ballpark_transformations, False),
            (("globalReference", "requireBestAvailableTransformation"), global_reference.require_best_available_transformation, True),
            (("globalReference", "alwaysXy"), global_reference.always_xy, True),
            (("globalReference", "mutableBeforeMaterialization"), global_reference.mutable_before_materialization, True),
            (("globalReference", "mutableAfterMaterialization"), global_reference.mutable_after_materialization, False),
            (("globalReference", "normalReanchorAllowed"), global_reference.normal_reanchor_allowed, False),
            (("globalReference", "reanchorRequiresDedicatedMigration"), global_reference.reanchor_requires_dedicated_migration, True),
        ):
            _expect(context, path, actual, expected)
        _expect_positive_decimal(context, ("globalReference", "defaultMaximumRoundtripErrorM"), global_reference.default_maximum_roundtrip_error_m)
        missing_locks = sorted(expected_lock_reasons - set(global_reference.materialization_lock_reasons))
        if missing_locks:
            context.error("required_values_missing", ("globalReference", "materializationLockReasons"), "Materialisierungssperren fehlen.", expected=sorted(expected_lock_reasons), actual=list(global_reference.materialization_lock_reasons))

    if storage_frame is not None:
        for path, actual, expected in (
            (("storageFrame", "persisted"), storage_frame.persisted, False),
            (("storageFrame", "cacheable"), storage_frame.cacheable, True),
            (("storageFrame", "reproducibleFromGlobalReference"), storage_frame.reproducible_from_global_reference, True),
            (("storageFrame", "originPolicy"), storage_frame.origin_policy, grid.storage_origin_policy if grid else STORAGE_ORIGIN_POLICY),
            (("storageFrame", "chunkAlignedAxes"), storage_frame.chunk_aligned_axes, ("x", "y", "z")),
            (("storageFrame", "referencePointMayBeSubCell"), storage_frame.reference_point_may_be_sub_cell, True),
            (("storageFrame", "referenceLocalPositionDerived"), storage_frame.reference_local_position_derived, True),
            (("storageFrame", "rotationAllowed"), storage_frame.rotation_allowed, False),
            (("storageFrame", "regionalRuntimeCrsAllowed"), storage_frame.regional_runtime_crs_allowed, False),
            (("storageFrame", "perProjectGridPhaseAllowed"), storage_frame.per_project_grid_phase_allowed, False),
        ):
            _expect(context, path, actual, expected)

    if generator is not None:
        for path, actual, expected in (
            (("generator", "type"), generator.generator_type, generator_type),
            (("generator", "version"), generator.version, generator_version),
            (("generator", "deterministic"), generator.deterministic, True),
            (("generator", "topologyAware"), generator.topology_aware, True),
            (("generator", "defaultBlockTypeId"), generator.default_block_type_id, DEFAULT_BLOCK_TYPE_ID),
            (("generator", "generationMode"), generator.generation_mode, GENERATION_MODE),
            (("generator", "terrainSurfaceGenerated"), generator.terrain_surface_generated, False),
            (("generator", "northSouthBoundaryGenerated"), generator.north_south_boundary_generated, False),
            (("generator", "periodicXBoundaryGenerated"), generator.periodic_x_boundary_generated, False),
            (("generator", "canonicalizeChunkBeforeGeneration"), generator.canonicalize_chunk_before_generation, True),
            (("generator", "snapshotCompatible"), generator.snapshot_compatible, True),
            (("generator", "eventReplayCompatible"), generator.event_replay_compatible, True),
        ):
            _expect(context, path, actual, expected)

    if spawn is not None:
        for path, actual, expected in (
            (("spawn", "persistedCoordinateSpace"), spawn.persisted_coordinate_space, "local_metric"),
            (("spawn", "defaultPolicy"), spawn.default_policy, "global-reference-point-as-local-position"),
            (("spawn", "globalCoordinateInputSupported"), spawn.global_coordinate_input_supported, True),
            (("spawn", "explicitCrsRequiredForGlobalInput"), spawn.explicit_crs_required_for_global_input, True),
            (("spawn", "moveChangesGlobalReference"), spawn.move_changes_global_reference, False),
            (("spawn", "moveReanchorsWorld"), spawn.move_reanchors_world, False),
            (("spawn", "xCanonicalizedBeforePersistence"), spawn.x_canonicalized_before_persistence, True),
            (("spawn", "northSouthBoundsValidated"), spawn.north_south_bounds_validated, True),
            (("spawn", "verticalRequiresResolvedReferenceHeight"), spawn.vertical_requires_resolved_reference_height, False),
        ):
            _expect(context, path, actual, expected)

    if persistence is not None:
        for field, actual in (
            ("blocksStoredGlobally", persistence.blocks_stored_globally),
            ("chunksStoredGlobally", persistence.chunks_stored_globally),
            ("eventsStoredGlobally", persistence.events_stored_globally),
            ("commandsStoredGlobally", persistence.commands_stored_globally),
            ("objectsStoredGlobally", persistence.objects_stored_globally),
            ("playersStoredGlobally", persistence.players_stored_globally),
            ("spawnStoredGlobally", persistence.spawn_stored_globally),
            ("derivedGlobalCoordinatesPersistedPerEntity", persistence.derived_global_coordinates_persisted_per_entity),
        ):
            _expect(context, ("persistence", field), actual, False)
        _expect(context, ("persistence", "globalReferenceRecordCount"), persistence.global_reference_record_count, 1)
        for field, actual in (
            ("canonicalizeBeforeWrite", persistence.canonicalize_before_write),
            ("canonicalizeBeforeRead", persistence.canonicalize_before_read),
            ("canonicalizeBeforeChunkKey", persistence.canonicalize_before_chunk_key),
            ("canonicalizeBeforeSnapshotLookup", persistence.canonicalize_before_snapshot_lookup),
            ("canonicalizeDirtyChunks", persistence.canonicalize_dirty_chunks),
            ("deduplicateCanonicalDirtyChunks", persistence.deduplicate_canonical_dirty_chunks),
        ):
            _expect(context, ("persistence", field), actual, True)

    if capabilities is not None:
        for field, actual, expected in (
            ("chunkGeneration", capabilities.chunk_generation, True),
            ("chunkSnapshots", capabilities.chunk_snapshots, True),
            ("chunkEvents", capabilities.chunk_events, True),
            ("blockCommands", capabilities.block_commands, True),
            ("batchCommands", capabilities.batch_commands, True),
            ("globalReference", capabilities.global_reference, True),
            ("globalToLocalConversion", capabilities.global_to_local_conversion, True),
            ("localToGlobalConversion", capabilities.local_to_global_conversion, True),
            ("globalSpawnInput", capabilities.global_spawn_input, True),
            ("periodicX", capabilities.periodic_x, True),
            ("periodicZ", capabilities.periodic_z, False),
            ("normalReanchor", capabilities.normal_reanchor, False),
            ("terrainImport", capabilities.terrain_import, False),
            ("regionalCrs", capabilities.regional_crs, False),
            ("projectGridRotation", capabilities.project_grid_rotation, False),
        ):
            _expect(context, ("capabilities", field), actual, expected)

    if compatibility is not None:
        for field, actual in (
            ("sharedChunkMath", compatibility.shared_chunk_math),
            ("sharedSnapshotFormat", compatibility.shared_snapshot_format),
            ("sharedEventFormat", compatibility.shared_event_format),
            ("sharedCommandPath", compatibility.shared_command_path),
            ("flatProviderUnchanged", compatibility.flat_provider_unchanged),
            ("flatProviderDefaultBehaviorUnchanged", compatibility.flat_provider_default_behavior_unchanged),
            ("providerSelectionRequired", compatibility.provider_selection_required),
        ):
            _expect(context, ("compatibility", field), actual, True)

    if runtime is not None:
        for path, actual, expected in (
            (("runtime", "requiresPyproj"), runtime.requires_pyproj, True),
            (("runtime", "requiresProjDatabase"), runtime.requires_proj_database, True),
            (("runtime", "projNetworkEnabledByDefault"), runtime.proj_network_enabled_by_default, False),
            (("runtime", "automaticGridDownloadAllowed"), runtime.automatic_grid_download_allowed, False),
            (("runtime", "readinessRequiresCanonicalCrs"), runtime.readiness_requires_canonical_crs, (CANONICAL_GEOGRAPHIC_CRS_ID, CANONICAL_GEOCENTRIC_CRS_ID)),
        ):
            _expect(context, path, actual, expected)
        if not _version_at_least(runtime.minimum_pyproj_version, MINIMUM_PYPROJ_VERSION):
            context.error("minimum_version_too_low", ("runtime", "minimumPyprojVersion"), "Die pyproj-Mindestversion ist zu niedrig.", expected=MINIMUM_PYPROJ_VERSION, actual=runtime.minimum_pyproj_version)

    if observability is not None:
        for field, actual, expected in (
            ("emitCanonicalizationMetrics", observability.emit_canonicalization_metrics, True),
            ("emitWrapCountMetrics", observability.emit_wrap_count_metrics, True),
            ("emitTransformerCacheMetrics", observability.emit_transformer_cache_metrics, True),
            ("emitEarthFrameCacheMetrics", observability.emit_earth_frame_cache_metrics, True),
            ("includeFullCrsDefinitionsInLogs", observability.include_full_crs_definitions_in_logs, False),
            ("includeFullTransformPipelinesInLogs", observability.include_full_transform_pipelines_in_logs, False),
        ):
            _expect(context, ("observability", field), actual, expected)


def _expect(
    context: _ValidationContext,
    path: JsonPath,
    actual: Any,
    expected: Any,
) -> None:
    if actual is None:
        return
    if actual != expected:
        context.error(
            "invariant_mismatch",
            path,
            "Das Feld verletzt den Earth-v1-Vertrag.",
            expected=expected,
            actual=actual,
        )


def _expect_positive(
    context: _ValidationContext,
    path: JsonPath,
    value: int,
) -> None:
    if value <= 0:
        context.error(
            "range_violation",
            path,
            "Der Wert muss größer als 0 sein.",
            expected="> 0",
            actual=value,
        )


def _expect_positive_decimal(
    context: _ValidationContext,
    path: JsonPath,
    value: Decimal,
) -> None:
    if value <= 0:
        context.error(
            "range_violation",
            path,
            "Der Dezimalwert muss größer als 0 sein.",
            expected="> 0",
            actual=_decimal_text(value),
        )


def _normalize_payload(
    payload: Mapping[str, Any] | str | bytes | bytearray,
) -> tuple[Mapping[str, Any], str]:
    if isinstance(payload, Mapping):
        normalized = _normalize_json_mapping(payload, depth=0)
        raw = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return normalized, sha256(raw).hexdigest()

    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise TypeError("Nicht unterstützter Manifesttyp.")

    if len(raw) <= 0 or len(raw) > _MAX_MANIFEST_SIZE_BYTES:
        raise ValueError("Manifestgröße ist ungültig.")

    decoded = raw.decode("utf-8")
    parsed = json.loads(decoded)
    if not isinstance(parsed, Mapping):
        raise ValueError("Manifestwurzel muss ein JSON-Objekt sein.")
    normalized = _normalize_json_mapping(parsed, depth=0)
    return normalized, sha256(raw).hexdigest()


def _normalize_json_mapping(
    value: Mapping[Any, Any],
    *,
    depth: int,
) -> dict[str, Any]:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("Manifest ist zu tief verschachtelt.")
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if not key:
            raise ValueError("Leerer JSON-Schlüssel ist nicht erlaubt.")
        result[key] = _normalize_json_value(raw_value, depth=depth + 1)
    return result


def _normalize_json_value(value: Any, *, depth: int) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("Manifest ist zu tief verschachtelt.")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not (value == value and value not in (float("inf"), float("-inf"))):
            raise ValueError("Nicht-endliche JSON-Zahl.")
        return value
    if isinstance(value, Mapping):
        return _normalize_json_mapping(value, depth=depth)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > _MAX_SEQUENCE_ITEMS:
            raise ValueError("JSON-Array ist zu groß.")
        return [
            _normalize_json_value(item, depth=depth + 1)
            for item in value
        ]
    raise ValueError(f"Nicht JSON-kompatibler Wert: {type(value).__name__}")


def _safe_json(value: Any, *, depth: int = 0) -> JsonValue:
    if depth >= 6:
        return {"truncated": True}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                result["truncated"] = True
                break
            result[str(key)] = _safe_json(item, depth=depth + 1)
        return result
    if isinstance(value, (tuple, list, set, frozenset)):
        return [
            _safe_json(item, depth=depth + 1)
            for item in list(value)[:100]
        ]
    return {"type": type(value).__name__}


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _require_identifier(value: Any, field_name: str) -> str:
    text = _require_text(value, field_name, maximum_length=256)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", text):
        raise GeoreferencingValidationError(
            f"'{field_name}' besitzt ein ungültiges Identifierformat."
        )
    return text


def _require_text(
    value: Any,
    field_name: str,
    *,
    maximum_length: int,
) -> str:
    if not isinstance(value, str):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Zeichenfolge sein."
        )
    normalized = value.strip()
    if not normalized or len(normalized) > maximum_length:
        raise GeoreferencingValidationError(
            f"'{field_name}' ist leer oder zu lang."
        )
    return normalized


def _path_fingerprint(path: Path) -> str:
    try:
        value = str(path.expanduser().resolve(strict=False))
    except Exception:
        value = str(path)
    return sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _version_at_least(installed: str, required: str) -> bool:
    installed_tuple = tuple(
        int(item) for item in _VERSION_COMPONENT_PATTERN.findall(installed)
    )
    required_tuple = tuple(
        int(item) for item in _VERSION_COMPONENT_PATTERN.findall(required)
    )
    if not installed_tuple:
        return False
    length = max(len(installed_tuple), len(required_tuple))
    return (
        installed_tuple + (0,) * (length - len(installed_tuple))
        >= required_tuple + (0,) * (length - len(required_tuple))
    )


__all__ = [
    "CoordinateSpacesContract",
    "EarthCapabilityContract",
    "EarthChunkContract",
    "EarthCompatibilityContract",
    "EarthGeneratorContract",
    "EarthGlobalReferenceContract",
    "EarthGridContract",
    "EarthObservabilityContract",
    "EarthPersistenceContract",
    "EarthRuntimeContract",
    "EarthSpawnContract",
    "EarthStorageFrameContract",
    "EarthTopologyContract",
    "EarthWorldDefinition",
    "EarthWorldValidationIssue",
    "EarthWorldValidationResult",
    "ValidationSeverity",
    "clear_earth_world_definition_cache",
    "earth_world_definition_cache_info",
    "earth_world_definition_status",
    "load_earth_world_definition",
    "validate_earth_world_definition",
]
