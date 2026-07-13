# services/vectoplan-chunk/src/world/earth/provider.py
"""WorldInstance-spezifischer Provider des Earth-v1-Welttyps.

Der Provider verbindet die bereits getrennten Verantwortlichkeiten:

* ``world.json`` und ``EarthWorldDefinition`` beschreiben den statischen
  Providervertrag;
* ``GlobalReferencePoint`` ist der eine persistierte globale Bezugspunkt
  einer konkreten Earth-WorldInstance;
* ``EarthGridFrame`` leitet daraus den lokalen Speicherframe ab;
* ``PeriodicXTopology`` kanonisiert Block- und Chunkkoordinaten;
* ``EarthFlatPeriodicGenerator`` liefert den Basiszustand unmaterialisierter
  Chunks;
* die Snapshot-, Event- und Command-Schichten können weiterhin ausschließlich
  mit lokalen, kanonischen Chunkadressen arbeiten.

Identitätsregel
---------------
Eine konkrete Instanz kann beispielsweise ``world_spawn`` heißen. Dagegen
bleiben:

```
providerId      = earth
templateId      = earth
providerWorldId = earth
```

``earth`` darf deshalb nicht als konkrete ``world_id`` dieses
WorldInstance-Providers verwendet werden.

Persistenzregel
---------------
Der Provider persistiert selbst nichts. Er stellt nur sicher, dass aufrufende
Schichten vor Lese- oder Schreiboperationen kanonische lokale Adressen
erhalten. Globale Koordinaten werden aus Referenz und lokalem Zustand
berechnet, jedoch nicht je Block, Chunk, Event, Objekt, Spieler oder Spawn
redundant gespeichert.

Cachemodell
-----------
Providerinstanzen sind unveränderlich und werden begrenzt nach konkreter
WorldInstance, Referenzfingerprint, Manifest, Transformationspolicy und
Transformationsoptionen gecacht. Ein Cache-Reset verändert keine persistierte
Wahrheit. Generator-, Transformer- und Earth-Grid-Caches besitzen eigene
Lebenszyklen und werden durch die übergeordnete Paketfassade geordnet geleert.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json
import re
from threading import RLock
from typing import Any, ClassVar, Final, Self

from ...coordinates.errors import CoordinateError
from ...coordinates.models import (
    ChunkAddress,
    ChunkPosition,
    JsonValue,
    LocalBlockPosition,
    LocalMetricPosition,
    NormalizedBlockPosition,
    NormalizedChunkAddress,
    ResolvedCellAddress,
)
from ...coordinates.topology import PeriodicXTopology
from ...georeferencing.contracts import (
    CrsDefinition,
    GlobalCoordinate,
    GlobalReferencePoint,
    TransformationPolicy,
    decimal_to_canonical_string,
)
from ...georeferencing.earth_grid import (
    EarthGridDefinition,
    EarthGridFrame,
    GlobalToLocalResult,
    LocalEarthPosition,
    LocalToGlobalResult,
    global_to_local as convert_global_to_local,
    local_to_global as convert_local_to_global,
    resolve_earth_grid_frame,
)
from ...georeferencing.errors import (
    EarthReferenceConflictError,
    EarthReferenceInvalidError,
    GeoreferencingConfigurationError,
    GeoreferencingValidationError,
    WorldReferenceLockedError,
)
from ...georeferencing.transformer import (
    TransformerSelectionOptions,
)
from .generator import (
    EarthFlatPeriodicGenerator,
    EarthGeneratedChunk,
    get_earth_flat_periodic_generator,
    get_earth_generator_config,
)
from .validator import (
    PROVIDER_ID,
    PROVIDER_WORLD_ID,
    TEMPLATE_ID,
    WORLD_TYPE,
    EarthWorldDefinition,
    load_earth_world_definition,
)


DEFAULT_INSTANCE_WORLD_ID: Final[str] = "world_spawn"
PROVIDER_SCHEMA_VERSION: Final[str] = "earth-world-provider.v1"
CAPABILITIES_SCHEMA_VERSION: Final[str] = (
    "earth-provider-capabilities.v1"
)
_MAX_WORLD_ID_LENGTH: Final[int] = 256
_PROVIDER_CACHE_SIZE: Final[int] = 512
_MAX_BATCH_SIZE: Final[int] = 4_096
_WORLD_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
)

_METRICS_LOCK = RLock()
_FACTORY_CALLS = 0
_PROVIDER_CREATIONS = 0
_PROVIDER_CREATION_FAILURES = 0
_CHUNK_GENERATION_CALLS = 0
_BATCH_GENERATION_CALLS = 0
_GLOBAL_TO_LOCAL_CALLS = 0
_LOCAL_TO_GLOBAL_CALLS = 0
_SPAWN_RESOLUTION_CALLS = 0
_REFERENCE_REBUILDS = 0
_OPERATION_FAILURES = 0


@dataclass(frozen=True, slots=True)
class EarthProviderCapabilities:
    """Unveränderliche Laufzeitfähigkeiten des Earth-v1-Providers."""

    provider_id: str
    world_type: str
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

    schema_version: ClassVar[str] = CAPABILITIES_SCHEMA_VERSION

    def __post_init__(self) -> None:
        provider_id = _require_exact_text(
            self.provider_id,
            expected=PROVIDER_ID,
            field_name="providerId",
        )
        world_type = _require_exact_text(
            self.world_type,
            expected=WORLD_TYPE,
            field_name="worldType",
        )

        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "world_type", world_type)

        for field_name in (
            "chunk_generation",
            "chunk_snapshots",
            "chunk_events",
            "block_commands",
            "batch_commands",
            "global_reference",
            "global_to_local_conversion",
            "local_to_global_conversion",
            "global_spawn_input",
            "periodic_x",
            "periodic_z",
            "normal_reanchor",
            "terrain_import",
            "regional_crs",
            "project_grid_rotation",
        ):
            object.__setattr__(
                self,
                field_name,
                bool(getattr(self, field_name)),
            )

        required_true = {
            "chunk_generation": self.chunk_generation,
            "chunk_snapshots": self.chunk_snapshots,
            "chunk_events": self.chunk_events,
            "block_commands": self.block_commands,
            "batch_commands": self.batch_commands,
            "global_reference": self.global_reference,
            "global_to_local_conversion": (
                self.global_to_local_conversion
            ),
            "local_to_global_conversion": (
                self.local_to_global_conversion
            ),
            "global_spawn_input": self.global_spawn_input,
            "periodic_x": self.periodic_x,
        }
        required_false = {
            "periodic_z": self.periodic_z,
            "normal_reanchor": self.normal_reanchor,
            "terrain_import": self.terrain_import,
            "regional_crs": self.regional_crs,
            "project_grid_rotation": self.project_grid_rotation,
        }

        failures = [
            name
            for name, enabled in required_true.items()
            if not enabled
        ]
        failures.extend(
            name
            for name, enabled in required_false.items()
            if enabled
        )

        if failures:
            raise GeoreferencingConfigurationError(
                "Die Earth-Providerfähigkeiten verletzen den v1-Vertrag.",
                details={
                    "failureCount": len(failures),
                    "failures": sorted(failures),
                },
            )

    @classmethod
    def from_definition(
        cls,
        definition: EarthWorldDefinition,
    ) -> Self:
        if not isinstance(definition, EarthWorldDefinition):
            raise GeoreferencingValidationError(
                "definition muss EarthWorldDefinition sein.",
                details={
                    "actualType": type(definition).__name__,
                },
            )

        capabilities = definition.capabilities
        return cls(
            provider_id=definition.provider_id,
            world_type=definition.world_type,
            chunk_generation=capabilities.chunk_generation,
            chunk_snapshots=capabilities.chunk_snapshots,
            chunk_events=capabilities.chunk_events,
            block_commands=capabilities.block_commands,
            batch_commands=capabilities.batch_commands,
            global_reference=capabilities.global_reference,
            global_to_local_conversion=(
                capabilities.global_to_local_conversion
            ),
            local_to_global_conversion=(
                capabilities.local_to_global_conversion
            ),
            global_spawn_input=capabilities.global_spawn_input,
            periodic_x=capabilities.periodic_x,
            periodic_z=capabilities.periodic_z,
            normal_reanchor=capabilities.normal_reanchor,
            terrain_import=capabilities.terrain_import,
            regional_crs=capabilities.regional_crs,
            project_grid_rotation=(
                capabilities.project_grid_rotation
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "providerId": self.provider_id,
            "worldType": self.world_type,
            "chunkGeneration": self.chunk_generation,
            "chunkSnapshots": self.chunk_snapshots,
            "chunkEvents": self.chunk_events,
            "blockCommands": self.block_commands,
            "batchCommands": self.batch_commands,
            "globalReference": self.global_reference,
            "globalToLocalConversion": (
                self.global_to_local_conversion
            ),
            "localToGlobalConversion": (
                self.local_to_global_conversion
            ),
            "globalSpawnInput": self.global_spawn_input,
            "periodicX": self.periodic_x,
            "periodicZ": self.periodic_z,
            "normalReanchor": self.normal_reanchor,
            "terrainImport": self.terrain_import,
            "regionalCrs": self.regional_crs,
            "projectGridRotation": self.project_grid_rotation,
        }


@dataclass(frozen=True, slots=True)
class EarthWorldProvider:
    """Konkreter, unveränderlicher Provider einer Earth-WorldInstance."""

    world_id: str
    reference: GlobalReferencePoint
    definition: EarthWorldDefinition
    grid_definition: EarthGridDefinition
    frame: EarthGridFrame
    generator: EarthFlatPeriodicGenerator
    capabilities: EarthProviderCapabilities
    transformation_policy: TransformationPolicy
    transformation_options: TransformerSelectionOptions

    schema_version: ClassVar[str] = PROVIDER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        world_id = _normalize_concrete_world_id(self.world_id)
        object.__setattr__(self, "world_id", world_id)

        expected_types = (
            ("reference", self.reference, GlobalReferencePoint),
            ("definition", self.definition, EarthWorldDefinition),
            (
                "grid_definition",
                self.grid_definition,
                EarthGridDefinition,
            ),
            ("frame", self.frame, EarthGridFrame),
            (
                "generator",
                self.generator,
                EarthFlatPeriodicGenerator,
            ),
            (
                "capabilities",
                self.capabilities,
                EarthProviderCapabilities,
            ),
            (
                "transformation_policy",
                self.transformation_policy,
                TransformationPolicy,
            ),
            (
                "transformation_options",
                self.transformation_options,
                TransformerSelectionOptions,
            ),
        )
        for field_name, value, expected_type in expected_types:
            if not isinstance(value, expected_type):
                raise GeoreferencingValidationError(
                    f"'{field_name}' besitzt einen ungültigen Typ.",
                    details={
                        "actualType": type(value).__name__,
                        "expectedType": expected_type.__name__,
                    },
                )

        _validate_provider_definition(self.definition)
        _validate_provider_relationships(self)

    @property
    def provider_id(self) -> str:
        return self.definition.provider_id

    @property
    def template_id(self) -> str:
        return self.definition.template_id

    @property
    def provider_world_id(self) -> str:
        return self.definition.provider_world_id

    @property
    def world_type(self) -> str:
        return self.definition.world_type

    @property
    def topology(self) -> PeriodicXTopology:
        return self.frame.topology

    @property
    def chunk_size(self) -> int:
        return self.definition.chunk.size

    @property
    def reference_fingerprint(self) -> str:
        return self.reference.fingerprint

    @property
    def coordinate_frame_revision(self) -> int:
        return self.reference.reference_version

    @property
    def provider_cache_key(self) -> str:
        payload = {
            "schemaVersion": self.schema_version,
            "worldId": self.world_id,
            "providerId": self.provider_id,
            "definitionSemanticFingerprint": (
                self.definition.semantic_fingerprint
            ),
            "referenceFingerprint": self.reference.fingerprint,
            "frameCacheKey": self.frame.cache_key,
            "generatorConfigFingerprint": (
                self.generator.config.config_fingerprint
            ),
            "transformationPolicy": (
                self.transformation_policy.to_dict()
            ),
            "transformationOptions": (
                self.transformation_options.to_dict()
            ),
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def normalize_block_position(
        self,
        position: LocalBlockPosition,
    ) -> NormalizedBlockPosition:
        return self.topology.normalize_block_position(position)

    def normalize_chunk_address(
        self,
        address: (
            ChunkAddress
            | ChunkPosition
            | Mapping[str, Any]
            | Sequence[int]
        ),
    ) -> NormalizedChunkAddress:
        requested = _coerce_chunk_address(address)
        return self.topology.normalize_chunk_address(requested)

    def resolve_block_address(
        self,
        position: LocalBlockPosition,
    ) -> ResolvedCellAddress:
        return self.topology.resolve_block_address(position)

    def neighbor_chunk(
        self,
        address: (
            ChunkAddress
            | ChunkPosition
            | Mapping[str, Any]
            | Sequence[int]
        ),
        *,
        dx: int = 0,
        dy: int = 0,
        dz: int = 0,
    ) -> ChunkAddress:
        return self.topology.neighbor_chunk(
            _coerce_chunk_address(address),
            dx=dx,
            dy=dy,
            dz=dz,
        )

    def dirty_chunks_for_block(
        self,
        position: LocalBlockPosition,
        *,
        include_diagonal_combinations: bool = True,
        include_current_chunk: bool = True,
    ) -> tuple[ChunkAddress, ...]:
        return self.topology.dirty_chunks_for_block(
            position,
            include_diagonal_combinations=(
                include_diagonal_combinations
            ),
            include_current_chunk=include_current_chunk,
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
        _record_chunk_generation()
        try:
            return self.generator.generate_chunk(address)
        except CoordinateError:
            _record_operation_failure()
            raise
        except Exception as error:
            _record_operation_failure()
            raise GeoreferencingConfigurationError(
                "Earth-Provider konnte den Chunk nicht generieren.",
                details={
                    "worldId": self.world_id,
                    "causeType": type(error).__name__,
                },
                cause=error,
            ) from error

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
        _record_batch_generation()
        try:
            return self.generator.generate_batch(
                addresses,
                deduplicate_canonical=deduplicate_canonical,
                maximum_batch_size=maximum_batch_size,
            )
        except CoordinateError:
            _record_operation_failure()
            raise
        except Exception as error:
            _record_operation_failure()
            raise GeoreferencingConfigurationError(
                "Earth-Provider konnte den Chunkbatch nicht generieren.",
                details={
                    "worldId": self.world_id,
                    "causeType": type(error).__name__,
                },
                cause=error,
            ) from error

    def global_to_local(
        self,
        coordinate: GlobalCoordinate,
        source_crs: CrsDefinition,
        *,
        policy: TransformationPolicy | None = None,
        options: TransformerSelectionOptions | None = None,
    ) -> GlobalToLocalResult:
        """Berechnet eine kanonische lokale Position aus globalen Daten."""

        _record_global_to_local()
        active_policy = _require_policy_not_weaker(
            policy or self.transformation_policy,
            baseline=self.transformation_policy,
        )
        active_options = _normalize_transform_options(
            options or self.transformation_options
        )

        try:
            return convert_global_to_local(
                self.frame,
                coordinate,
                source_crs,
                policy=active_policy,
                options=active_options,
            )
        except CoordinateError:
            _record_operation_failure()
            raise
        except Exception as error:
            _record_operation_failure()
            raise GeoreferencingConfigurationError(
                "Globale Earth-Koordinate konnte nicht lokal aufgelöst werden.",
                details={
                    "worldId": self.world_id,
                    "causeType": type(error).__name__,
                },
                cause=error,
            ) from error

    def local_to_global(
        self,
        position: LocalEarthPosition | LocalBlockPosition,
        *,
        target_crs: CrsDefinition | None = None,
        policy: TransformationPolicy | None = None,
        options: TransformerSelectionOptions | None = None,
    ) -> LocalToGlobalResult:
        """Berechnet eine globale Position aus lokalem Earth-Zustand."""

        _record_local_to_global()
        active_policy = _require_policy_not_weaker(
            policy or self.transformation_policy,
            baseline=self.transformation_policy,
        )
        active_options = _normalize_transform_options(
            options or self.transformation_options
        )

        try:
            return convert_local_to_global(
                self.frame,
                position,
                target_crs=target_crs,
                policy=active_policy,
                options=active_options,
            )
        except CoordinateError:
            _record_operation_failure()
            raise
        except Exception as error:
            _record_operation_failure()
            raise GeoreferencingConfigurationError(
                "Lokale Earth-Koordinate konnte nicht global aufgelöst werden.",
                details={
                    "worldId": self.world_id,
                    "causeType": type(error).__name__,
                },
                cause=error,
            ) from error

    def normalize_local_metric_position(
        self,
        position: LocalMetricPosition,
    ) -> LocalMetricPosition:
        """Kanonisiert eine persistierbare lokale Sub-Block-Position."""

        if not isinstance(position, LocalMetricPosition):
            raise GeoreferencingValidationError(
                "position muss LocalMetricPosition sein.",
                details={
                    "actualType": type(position).__name__,
                },
            )

        local = LocalEarthPosition.from_metric_position(
            position,
            meters_per_cell=(
                self.grid_definition.meters_per_cell
            ),
        )
        normalized = self.frame.normalize_local_position(local)
        return normalized.to_metric_position(
            meters_per_cell=(
                self.grid_definition.meters_per_cell
            ),
        )

    def default_spawn_position(self) -> LocalMetricPosition:
        """Liefert den lokal persistierbaren Default-Spawn.

        Bei einer 2D-Referenz wird lokal Y=0 verwendet. Dadurch bleibt ein
        lokaler Spawn speicherbar, ohne eine globale absolute Höhe zu erfinden.
        """

        reference_local = self.frame.reference_local_position
        y_cells = (
            reference_local.y
            if reference_local.y is not None
            else Decimal("0")
        )
        scale = self.grid_definition.meters_per_cell

        return LocalMetricPosition(
            x=float(reference_local.x * scale),
            y=float(y_cells * scale),
            z=float(reference_local.z * scale),
        )

    def resolve_spawn_from_global(
        self,
        coordinate: GlobalCoordinate,
        source_crs: CrsDefinition,
        *,
        local_y_when_unresolved: float | None = None,
        policy: TransformationPolicy | None = None,
        options: TransformerSelectionOptions | None = None,
    ) -> LocalMetricPosition:
        """Konvertiert einen global adressierten Spawn in lokale Persistenz.

        Eine globale 2D-Koordinate verändert weder Referenz noch Weltframe.
        Wenn keine globale Höhe auflösbar ist, bleibt Y lokal. Ohne expliziten
        Fallback wird der aktuelle Default-Spawn-Y-Wert verwendet.
        """

        _record_spawn_resolution()
        result = self.global_to_local(
            coordinate,
            source_crs,
            policy=policy,
            options=options,
        )

        local = result.local_position
        scale = self.grid_definition.meters_per_cell

        if local.y is not None:
            local_y_metric = float(local.y * scale)
        elif local_y_when_unresolved is not None:
            local_y_metric = _require_finite_float(
                local_y_when_unresolved,
                field_name="localYWhenUnresolved",
            )
        else:
            local_y_metric = self.default_spawn_position().y

        position = LocalMetricPosition(
            x=float(local.x * scale),
            y=local_y_metric,
            z=float(local.z * scale),
        )
        return self.normalize_local_metric_position(position)

    def spawn_to_global(
        self,
        position: LocalMetricPosition,
        *,
        target_crs: CrsDefinition | None = None,
        require_vertical: bool = False,
        policy: TransformationPolicy | None = None,
        options: TransformerSelectionOptions | None = None,
    ) -> LocalToGlobalResult:
        """Berechnet die globale Spawnkoordinate ohne Reanchoring.

        Bei einer 2D-Weltreferenz kann X/Z global aufgelöst werden. Lokales Y
        besitzt dann keine absolute globale Bedeutung und wird weggelassen,
        sofern ``require_vertical`` nicht aktiv ist.
        """

        _record_spawn_resolution()
        normalized_metric = self.normalize_local_metric_position(
            position
        )
        local = LocalEarthPosition.from_metric_position(
            normalized_metric,
            meters_per_cell=(
                self.grid_definition.meters_per_cell
            ),
        )

        vertical_required = bool(
            require_vertical
            or self.definition.spawn
            .vertical_requires_resolved_reference_height
        )

        if not self.frame.storage_origin.vertical_resolved:
            if vertical_required:
                raise EarthReferenceInvalidError.for_reason(
                    "Die Earth-Referenz besitzt keine global auflösbare "
                    "Höhe für den Spawn.",
                    coordinate_dimensions=2,
                    crs=self.reference.crs.crs_id,
                )
            local = LocalEarthPosition(
                x=local.x,
                y=None,
                z=local.z,
            )

        return self.local_to_global(
            local,
            target_crs=target_crs,
            policy=policy,
            options=options,
        )

    def with_reference_before_materialization(
        self,
        proposed_reference: GlobalReferencePoint,
        *,
        materialization_lock_reasons: Sequence[str] = (),
    ) -> "EarthWorldProvider":
        """Erzeugt vor Materialisierung einen Provider mit neuer Referenz.

        Die Methode mutiert diese Instanz nicht. Nach dem ersten materialisierten
        Zustand wird ein normaler Referenzwechsel abgelehnt.
        """

        if not isinstance(
            proposed_reference,
            GlobalReferencePoint,
        ):
            raise GeoreferencingValidationError(
                "proposed_reference muss GlobalReferencePoint sein.",
                details={
                    "actualType": type(
                        proposed_reference
                    ).__name__,
                },
            )

        if (
            proposed_reference.fingerprint
            == self.reference.fingerprint
        ):
            return self

        reasons = _normalize_lock_reasons(
            materialization_lock_reasons
        )
        if reasons:
            raise WorldReferenceLockedError.for_world(
                world_id=self.world_id,
                lock_reasons=reasons,
            )

        contract = self.definition.global_reference
        if not contract.mutable_before_materialization:
            raise EarthReferenceConflictError.for_world(
                world_id=self.world_id,
                conflicting_fields=(
                    "globalReference",
                    "referenceVersion",
                ),
            )

        if proposed_reference.grid != self.grid_definition.grid:
            raise EarthReferenceConflictError.for_world(
                world_id=self.world_id,
                conflicting_fields=(
                    "gridId",
                    "gridVersion",
                    "projectionId",
                    "topologyType",
                ),
            )

        _record_reference_rebuild()
        return get_earth_world_provider(
            self.world_id,
            proposed_reference,
            definition=self.definition,
            transformation_policy=(
                self.transformation_policy
            ),
            transformation_options=(
                self.transformation_options
            ),
        )

    def to_dict(
        self,
        *,
        include_reference: bool = False,
        include_definition: bool = False,
    ) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "schemaVersion": self.schema_version,
            "providerCacheKey": self.provider_cache_key,
            "worldId": self.world_id,
            "providerId": self.provider_id,
            "templateId": self.template_id,
            "providerWorldId": self.provider_world_id,
            "worldType": self.world_type,
            "coordinateFrameRevision": (
                self.coordinate_frame_revision
            ),
            "referenceFingerprint": (
                self.reference_fingerprint
            ),
            "definitionSemanticFingerprint": (
                self.definition.semantic_fingerprint
            ),
            "gridDefinitionFingerprint": (
                self.grid_definition.fingerprint
            ),
            "frameCacheKey": self.frame.cache_key,
            "chunkSize": self.chunk_size,
            "storageOrigin": (
                self.frame.storage_origin.to_dict()
            ),
            "referenceLocalPosition": (
                self.frame.reference_local_position.to_dict()
            ),
            "topology": self.topology.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "generator": self.generator.to_dict(),
            "transformationPolicy": (
                self.transformation_policy.to_dict()
            ),
            "transformationOptions": (
                self.transformation_options.to_dict()
            ),
            "defaultSpawn": (
                self.default_spawn_position().to_dict()
            ),
        }

        if include_reference:
            payload["reference"] = self.reference.to_dict()
        if include_definition:
            payload["definition"] = self.definition.to_dict()

        return payload


def get_earth_world_provider(
    world_id: str,
    reference: GlobalReferencePoint,
    *,
    definition: EarthWorldDefinition | None = None,
    transformation_policy: TransformationPolicy | None = None,
    transformation_options: TransformerSelectionOptions | None = None,
) -> EarthWorldProvider:
    """Liefert einen gecachten Provider für eine konkrete Earth-WorldInstance."""

    _record_factory_call()

    try:
        normalized_world_id = _normalize_concrete_world_id(
            world_id
        )
        if not isinstance(reference, GlobalReferencePoint):
            raise GeoreferencingValidationError(
                "reference muss GlobalReferencePoint sein.",
                details={
                    "actualType": type(reference).__name__,
                },
            )

        active_definition = (
            definition
            if definition is not None
            else load_earth_world_definition()
        )
        if not isinstance(
            active_definition,
            EarthWorldDefinition,
        ):
            raise GeoreferencingValidationError(
                "definition muss EarthWorldDefinition sein.",
                details={
                    "actualType": type(
                        active_definition
                    ).__name__,
                },
            )
        _validate_provider_definition(active_definition)

        grid_definition = (
            active_definition.to_earth_grid_definition()
        )
        if reference.grid != grid_definition.grid:
            raise EarthReferenceConflictError.for_world(
                world_id=normalized_world_id,
                conflicting_fields=(
                    "gridId",
                    "gridVersion",
                    "projectionId",
                    "projectionVersion",
                    "topologyType",
                ),
            )

        supported_dimensions = (
            active_definition.global_reference
            .supported_coordinate_dimensions
        )
        if (
            int(reference.coordinate.dimension)
            not in supported_dimensions
        ):
            raise EarthReferenceInvalidError.for_reason(
                "Die Dimension des globalen Referenzpunkts wird "
                "vom Earth-Provider nicht unterstützt.",
                coordinate_dimensions=int(
                    reference.coordinate.dimension
                ),
                crs=reference.crs.crs_id,
            )

        baseline_policy = _policy_from_definition(
            active_definition
        )
        active_policy = _require_policy_not_weaker(
            transformation_policy or baseline_policy,
            baseline=baseline_policy,
        )
        active_options = _normalize_transform_options(
            transformation_options
            or TransformerSelectionOptions.default()
        )

        provider = _get_earth_world_provider_cached(
            normalized_world_id,
            reference,
            active_definition,
            grid_definition,
            active_policy,
            active_options,
        )
        return provider
    except CoordinateError:
        _record_provider_creation_failure()
        raise
    except Exception as error:
        _record_provider_creation_failure()
        raise GeoreferencingConfigurationError(
            "Earth-WorldProvider konnte nicht erzeugt werden.",
            details={
                "worldId": (
                    world_id
                    if isinstance(world_id, str)
                    else None
                ),
                "causeType": type(error).__name__,
            },
            cause=error,
        ) from error


@lru_cache(maxsize=_PROVIDER_CACHE_SIZE)
def _get_earth_world_provider_cached(
    world_id: str,
    reference: GlobalReferencePoint,
    definition: EarthWorldDefinition,
    grid_definition: EarthGridDefinition,
    transformation_policy: TransformationPolicy,
    transformation_options: TransformerSelectionOptions,
) -> EarthWorldProvider:
    frame = resolve_earth_grid_frame(
        reference,
        definition=grid_definition,
        policy=transformation_policy,
        options=transformation_options,
    )
    generator_config = get_earth_generator_config(
        definition,
        topology=frame.topology,
    )
    generator = get_earth_flat_periodic_generator(
        generator_config
    )
    capabilities = EarthProviderCapabilities.from_definition(
        definition
    )

    provider = EarthWorldProvider(
        world_id=world_id,
        reference=reference,
        definition=definition,
        grid_definition=grid_definition,
        frame=frame,
        generator=generator,
        capabilities=capabilities,
        transformation_policy=transformation_policy,
        transformation_options=transformation_options,
    )
    _record_provider_creation()
    return provider


def earth_provider_component_status() -> dict[str, JsonValue]:
    """Read-only Smoke-Test des konkreten Earth-Providers."""

    payload: dict[str, JsonValue] = {
        "ok": False,
        "ready": False,
        "definitionReady": False,
        "referenceReady": False,
        "providerReady": False,
        "identityReady": False,
        "chunkGenerationReady": False,
        "periodicAliasReady": False,
        "globalLocalRoundtripReady": False,
        "spawnReady": False,
        "capabilitiesReady": False,
        "provider": None,
        "cache": earth_provider_component_cache_info(),
        "errors": [],
    }
    errors: list[JsonValue] = payload["errors"]  # type: ignore[assignment]

    try:
        from ...georeferencing.crs import (
            canonical_geographic_crs,
        )

        definition = load_earth_world_definition()
        payload["definitionReady"] = True

        grid_definition = (
            definition.to_earth_grid_definition()
        )
        crs = canonical_geographic_crs()
        reference = GlobalReferencePoint(
            coordinate=GlobalCoordinate.from_values(
                "11.576",
                "48.137",
                "560",
            ),
            crs=crs,
            grid=grid_definition.grid,
            reference_version=1,
            source="earth-provider-readiness",
        )
        payload["referenceReady"] = True

        provider = get_earth_world_provider(
            DEFAULT_INSTANCE_WORLD_ID,
            reference,
            definition=definition,
        )
        payload["providerReady"] = True
        payload["identityReady"] = bool(
            provider.world_id == DEFAULT_INSTANCE_WORLD_ID
            and provider.provider_id == PROVIDER_ID
            and provider.template_id == TEMPLATE_ID
            and provider.provider_world_id
            == PROVIDER_WORLD_ID
            and provider.world_type == WORLD_TYPE
        )

        zero = provider.generate_chunk((0, 0, 0))
        payload["chunkGenerationReady"] = bool(
            zero.chunk_key == "0:0:0"
            and zero.cell_count == 4_096
            and zero.non_air_cell_count == 0
        )

        alias = provider.generate_chunk(
            (
                provider.generator.config
                .world_width_chunks,
                0,
                0,
            )
        )
        payload["periodicAliasReady"] = bool(
            alias.address == zero.address
            and alias.canonicalized
            and alias.content_fingerprint
            == zero.content_fingerprint
        )

        reference_local = provider.global_to_local(
            reference.coordinate,
            reference.crs,
        )
        reference_global = provider.local_to_global(
            reference_local.local_position,
            target_crs=reference.crs,
        )
        payload["globalLocalRoundtripReady"] = bool(
            abs(
                reference_global.target_coordinate.x
                - reference.coordinate.x
            )
            < Decimal("0.000000000001")
            and abs(
                reference_global.target_coordinate.y
                - reference.coordinate.y
            )
            < Decimal("0.000000000001")
            and reference_global.target_coordinate.z
            is not None
            and reference.coordinate.z is not None
            and abs(
                reference_global.target_coordinate.z
                - reference.coordinate.z
            )
            < Decimal("0.000001")
        )

        default_spawn = provider.default_spawn_position()
        resolved_spawn = provider.resolve_spawn_from_global(
            reference.coordinate,
            reference.crs,
        )
        spawn_global = provider.spawn_to_global(
            resolved_spawn,
            target_crs=reference.crs,
            require_vertical=True,
        )
        payload["spawnReady"] = bool(
            abs(default_spawn.x - resolved_spawn.x)
            < 0.000001
            and abs(default_spawn.y - resolved_spawn.y)
            < 0.000001
            and abs(default_spawn.z - resolved_spawn.z)
            < 0.000001
            and spawn_global.target_coordinate.z is not None
        )

        capabilities = provider.capabilities
        payload["capabilitiesReady"] = bool(
            capabilities.chunk_generation
            and capabilities.global_reference
            and capabilities.global_to_local_conversion
            and capabilities.local_to_global_conversion
            and capabilities.global_spawn_input
            and capabilities.periodic_x
            and not capabilities.periodic_z
            and not capabilities.normal_reanchor
        )

        payload["provider"] = provider.to_dict(
            include_reference=False,
            include_definition=False,
        )
    except Exception as error:
        errors.append(_safe_error(error))

    payload["cache"] = earth_provider_component_cache_info()
    payload["ready"] = bool(
        payload["definitionReady"]
        and payload["referenceReady"]
        and payload["providerReady"]
        and payload["identityReady"]
        and payload["chunkGenerationReady"]
        and payload["periodicAliasReady"]
        and payload["globalLocalRoundtripReady"]
        and payload["spawnReady"]
        and payload["capabilitiesReady"]
    )
    payload["ok"] = bool(
        payload["ready"] and not errors
    )
    return payload


def earth_provider_component_cache_info() -> dict[str, JsonValue]:
    """Liefert Providercache- und Operationsmetriken."""

    with _METRICS_LOCK:
        metrics = {
            "factoryCalls": _FACTORY_CALLS,
            "providerCreations": _PROVIDER_CREATIONS,
            "providerCreationFailures": (
                _PROVIDER_CREATION_FAILURES
            ),
            "chunkGenerationCalls": (
                _CHUNK_GENERATION_CALLS
            ),
            "batchGenerationCalls": (
                _BATCH_GENERATION_CALLS
            ),
            "globalToLocalCalls": (
                _GLOBAL_TO_LOCAL_CALLS
            ),
            "localToGlobalCalls": (
                _LOCAL_TO_GLOBAL_CALLS
            ),
            "spawnResolutionCalls": (
                _SPAWN_RESOLUTION_CALLS
            ),
            "referenceRebuilds": _REFERENCE_REBUILDS,
            "operationFailures": _OPERATION_FAILURES,
        }

    return {
        "providers": _cache_info_to_dict(
            _get_earth_world_provider_cached.cache_info()
        ),
        "metrics": metrics,
    }


def clear_earth_provider_component_caches() -> dict[str, JsonValue]:
    """Leert ausschließlich Providercache und Provider-Metriken."""

    global _FACTORY_CALLS
    global _PROVIDER_CREATIONS
    global _PROVIDER_CREATION_FAILURES
    global _CHUNK_GENERATION_CALLS
    global _BATCH_GENERATION_CALLS
    global _GLOBAL_TO_LOCAL_CALLS
    global _LOCAL_TO_GLOBAL_CALLS
    global _SPAWN_RESOLUTION_CALLS
    global _REFERENCE_REBUILDS
    global _OPERATION_FAILURES

    _get_earth_world_provider_cached.cache_clear()

    with _METRICS_LOCK:
        _FACTORY_CALLS = 0
        _PROVIDER_CREATIONS = 0
        _PROVIDER_CREATION_FAILURES = 0
        _CHUNK_GENERATION_CALLS = 0
        _BATCH_GENERATION_CALLS = 0
        _GLOBAL_TO_LOCAL_CALLS = 0
        _LOCAL_TO_GLOBAL_CALLS = 0
        _SPAWN_RESOLUTION_CALLS = 0
        _REFERENCE_REBUILDS = 0
        _OPERATION_FAILURES = 0

    return {
        "ok": True,
        "cleared": [
            "providers",
            "metrics",
        ],
        "remaining": earth_provider_component_cache_info(),
    }


def _validate_provider_definition(
    definition: EarthWorldDefinition,
) -> None:
    failures: list[str] = []

    checks = (
        (definition.enabled, "provider_disabled"),
        (
            definition.provider_id == PROVIDER_ID,
            "provider_id_mismatch",
        ),
        (
            definition.template_id == TEMPLATE_ID,
            "template_id_mismatch",
        ),
        (
            definition.provider_world_id
            == PROVIDER_WORLD_ID,
            "provider_world_id_mismatch",
        ),
        (
            definition.world_type == WORLD_TYPE,
            "world_type_mismatch",
        ),
        (
            definition.global_reference.required,
            "global_reference_required",
        ),
        (
            definition.global_reference.cardinality
            == "exactly-one",
            "global_reference_cardinality",
        ),
        (
            definition.global_reference.crs_required,
            "crs_required",
        ),
        (
            not definition.global_reference
            .crs_guessing_allowed,
            "crs_guessing_must_be_disabled",
        ),
        (
            not definition.global_reference
            .allow_ballpark_transformations,
            "ballpark_must_be_disabled",
        ),
        (
            definition.global_reference
            .require_best_available_transformation,
            "best_available_required",
        ),
        (
            definition.global_reference.always_xy,
            "always_xy_required",
        ),
        (
            not definition.storage_frame.persisted,
            "derived_storage_frame_must_not_persist",
        ),
        (
            definition.storage_frame
            .reproducible_from_global_reference,
            "storage_frame_must_be_reproducible",
        ),
        (
            not definition.storage_frame.rotation_allowed,
            "rotation_must_be_disabled",
        ),
        (
            not definition.storage_frame
            .regional_runtime_crs_allowed,
            "regional_crs_must_be_disabled",
        ),
        (
            not definition.storage_frame
            .per_project_grid_phase_allowed,
            "project_grid_phase_must_be_global",
        ),
        (
            not definition.persistence
            .derived_global_coordinates_persisted_per_entity,
            "derived_global_entity_coordinates_forbidden",
        ),
        (
            definition.persistence
            .global_reference_record_count
            == 1,
            "exactly_one_reference_record",
        ),
        (
            definition.persistence
            .canonicalize_before_write,
            "canonicalize_before_write",
        ),
        (
            definition.persistence
            .canonicalize_before_read,
            "canonicalize_before_read",
        ),
        (
            definition.persistence
            .canonicalize_before_chunk_key,
            "canonicalize_before_chunk_key",
        ),
        (
            definition.persistence
            .canonicalize_before_snapshot_lookup,
            "canonicalize_before_snapshot_lookup",
        ),
        (
            definition.spawn.persisted_coordinate_space
            == "local_metric",
            "spawn_must_be_local_metric",
        ),
        (
            definition.spawn.global_coordinate_input_supported,
            "global_spawn_input_required",
        ),
        (
            definition.spawn
            .explicit_crs_required_for_global_input,
            "global_spawn_crs_required",
        ),
        (
            not definition.spawn
            .move_changes_global_reference,
            "spawn_move_must_not_change_reference",
        ),
        (
            not definition.spawn.move_reanchors_world,
            "spawn_move_must_not_reanchor",
        ),
        (
            definition.compatibility.flat_provider_unchanged,
            "flat_provider_must_remain_unchanged",
        ),
    )

    for passed, code in checks:
        if not passed:
            failures.append(code)

    if failures:
        raise GeoreferencingConfigurationError(
            "Die Earth-Definition ist nicht providerfähig.",
            details={
                "failureCount": len(failures),
                "failures": failures,
                "semanticFingerprint": (
                    definition.semantic_fingerprint
                ),
            },
        )


def _validate_provider_relationships(
    provider: EarthWorldProvider,
) -> None:
    if provider.reference.grid != provider.grid_definition.grid:
        raise GeoreferencingConfigurationError(
            "Referenz und Griddefinition widersprechen sich."
        )
    if provider.frame.reference != provider.reference:
        raise GeoreferencingConfigurationError(
            "EarthGridFrame verwendet eine andere Referenz."
        )
    if (
        provider.frame.definition
        != provider.grid_definition
    ):
        raise GeoreferencingConfigurationError(
            "EarthGridFrame verwendet eine andere Griddefinition."
        )
    if provider.frame.topology != provider.generator.config.topology:
        raise GeoreferencingConfigurationError(
            "Frame- und Generatortopologie widersprechen sich."
        )
    if (
        provider.generator.config
        .definition_semantic_fingerprint
        != provider.definition.semantic_fingerprint
    ):
        raise GeoreferencingConfigurationError(
            "Generator und Manifest verwenden verschiedene Definitionen."
        )

    expected_capabilities = (
        EarthProviderCapabilities.from_definition(
            provider.definition
        )
    )
    if provider.capabilities != expected_capabilities:
        raise GeoreferencingConfigurationError(
            "Providerfähigkeiten entsprechen nicht dem Manifest."
        )

    baseline_policy = _policy_from_definition(
        provider.definition
    )
    _require_policy_not_weaker(
        provider.transformation_policy,
        baseline=baseline_policy,
    )


def _policy_from_definition(
    definition: EarthWorldDefinition,
) -> TransformationPolicy:
    contract = definition.global_reference
    return TransformationPolicy(
        allow_ballpark=(
            contract.allow_ballpark_transformations
        ),
        require_best_available=(
            contract.require_best_available_transformation
        ),
        require_known_accuracy=False,
        maximum_accuracy_m=None,
        validate_roundtrip=True,
        maximum_roundtrip_error_m=(
            contract.default_maximum_roundtrip_error_m
        ),
        always_xy=contract.always_xy,
    )


def _require_policy_not_weaker(
    policy: TransformationPolicy,
    *,
    baseline: TransformationPolicy,
) -> TransformationPolicy:
    if not isinstance(policy, TransformationPolicy):
        raise GeoreferencingValidationError(
            "policy muss TransformationPolicy sein.",
            details={
                "actualType": type(policy).__name__,
            },
        )
    if not isinstance(baseline, TransformationPolicy):
        raise GeoreferencingValidationError(
            "baseline muss TransformationPolicy sein."
        )

    weakened: list[str] = []

    if not baseline.allow_ballpark and policy.allow_ballpark:
        weakened.append("allowBallpark")
    if (
        baseline.require_best_available
        and not policy.require_best_available
    ):
        weakened.append("requireBestAvailable")
    if (
        baseline.require_known_accuracy
        and not policy.require_known_accuracy
    ):
        weakened.append("requireKnownAccuracy")
    if baseline.validate_roundtrip and not policy.validate_roundtrip:
        weakened.append("validateRoundtrip")
    if baseline.always_xy and not policy.always_xy:
        weakened.append("alwaysXy")

    if baseline.maximum_accuracy_m is not None:
        if (
            policy.maximum_accuracy_m is None
            or policy.maximum_accuracy_m
            > baseline.maximum_accuracy_m
        ):
            weakened.append("maximumAccuracyM")

    if baseline.maximum_roundtrip_error_m is not None:
        if (
            policy.maximum_roundtrip_error_m is None
            or policy.maximum_roundtrip_error_m
            > baseline.maximum_roundtrip_error_m
        ):
            weakened.append("maximumRoundtripErrorM")

    if weakened:
        raise GeoreferencingValidationError(
            "Die angeforderte Transformationspolicy ist schwächer "
            "als der Earth-Providervertrag.",
            details={
                "weakenedFields": weakened,
                "baseline": baseline.to_dict(),
                "requested": policy.to_dict(),
            },
        )

    return policy


def _normalize_transform_options(
    options: TransformerSelectionOptions,
) -> TransformerSelectionOptions:
    if not isinstance(options, TransformerSelectionOptions):
        raise GeoreferencingValidationError(
            "options muss TransformerSelectionOptions sein.",
            details={
                "actualType": type(options).__name__,
            },
        )
    return options


def _normalize_concrete_world_id(value: Any) -> str:
    if not isinstance(value, str):
        raise GeoreferencingValidationError(
            "worldId muss eine Zeichenfolge sein.",
            details={
                "actualType": type(value).__name__,
            },
        )

    normalized = value.strip()
    if not normalized:
        raise GeoreferencingValidationError(
            "worldId darf nicht leer sein."
        )
    if len(normalized) > _MAX_WORLD_ID_LENGTH:
        raise GeoreferencingValidationError(
            "worldId überschreitet die maximale Länge.",
            details={
                "length": len(normalized),
                "maximumLength": _MAX_WORLD_ID_LENGTH,
            },
        )
    if not _WORLD_ID_PATTERN.fullmatch(normalized):
        raise GeoreferencingValidationError(
            "worldId enthält unzulässige Zeichen.",
            details={"worldId": normalized},
        )
    if normalized in {
        PROVIDER_ID,
        TEMPLATE_ID,
        PROVIDER_WORLD_ID,
    }:
        raise GeoreferencingValidationError(
            "Eine konkrete Earth-WorldInstance darf nicht "
            "die Provideridentität 'earth' verwenden.",
            details={
                "worldId": normalized,
                "providerId": PROVIDER_ID,
                "recommendedWorldId": (
                    DEFAULT_INSTANCE_WORLD_ID
                ),
            },
        )
    return normalized


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
        },
    )


def _normalize_lock_reasons(
    reasons: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(reasons, (str, bytes, bytearray)) or not isinstance(
        reasons,
        Sequence,
    ):
        raise GeoreferencingValidationError(
            "materialization_lock_reasons muss eine Sequenz sein.",
            details={
                "actualType": type(reasons).__name__,
            },
        )

    normalized: list[str] = []
    seen: set[str] = set()

    for index, value in enumerate(reasons):
        item = _require_non_empty_text(
            value,
            field_name=f"lockReasons[{index}]",
            maximum_length=256,
        )
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    return tuple(normalized)


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


def _require_finite_float(
    value: Any,
    *,
    field_name: str,
) -> float:
    if isinstance(value, bool) or not isinstance(
        value,
        (int, float, Decimal),
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Zahl sein.",
            details={
                "actualType": type(value).__name__,
            },
        )

    normalized = float(value)
    if not (
        normalized == normalized
        and normalized
        not in (float("inf"), float("-inf"))
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss endlich sein.",
            details={"value": str(value)},
        )
    return normalized


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


def _record_factory_call() -> None:
    global _FACTORY_CALLS
    with _METRICS_LOCK:
        _FACTORY_CALLS += 1


def _record_provider_creation() -> None:
    global _PROVIDER_CREATIONS
    with _METRICS_LOCK:
        _PROVIDER_CREATIONS += 1


def _record_provider_creation_failure() -> None:
    global _PROVIDER_CREATION_FAILURES
    with _METRICS_LOCK:
        _PROVIDER_CREATION_FAILURES += 1


def _record_chunk_generation() -> None:
    global _CHUNK_GENERATION_CALLS
    with _METRICS_LOCK:
        _CHUNK_GENERATION_CALLS += 1


def _record_batch_generation() -> None:
    global _BATCH_GENERATION_CALLS
    with _METRICS_LOCK:
        _BATCH_GENERATION_CALLS += 1


def _record_global_to_local() -> None:
    global _GLOBAL_TO_LOCAL_CALLS
    with _METRICS_LOCK:
        _GLOBAL_TO_LOCAL_CALLS += 1


def _record_local_to_global() -> None:
    global _LOCAL_TO_GLOBAL_CALLS
    with _METRICS_LOCK:
        _LOCAL_TO_GLOBAL_CALLS += 1


def _record_spawn_resolution() -> None:
    global _SPAWN_RESOLUTION_CALLS
    with _METRICS_LOCK:
        _SPAWN_RESOLUTION_CALLS += 1


def _record_reference_rebuild() -> None:
    global _REFERENCE_REBUILDS
    with _METRICS_LOCK:
        _REFERENCE_REBUILDS += 1


def _record_operation_failure() -> None:
    global _OPERATION_FAILURES
    with _METRICS_LOCK:
        _OPERATION_FAILURES += 1


def _safe_error(error: BaseException) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "type": type(error).__name__,
        "message": (
            str(error).strip()
            or "Earth-Provideroperation fehlgeschlagen."
        ),
    }
    code = getattr(error, "code", None)
    if code is not None:
        payload["code"] = str(code)
    return payload


__all__ = [
    "DEFAULT_INSTANCE_WORLD_ID",
    "EarthProviderCapabilities",
    "EarthWorldProvider",
    "clear_earth_provider_component_caches",
    "earth_provider_component_cache_info",
    "earth_provider_component_status",
    "get_earth_world_provider",
]
