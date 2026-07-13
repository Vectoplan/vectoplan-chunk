# services/vectoplan-chunk/src/georeferencing/earth_grid.py
"""Versionierte Abbildung zwischen globaler Earth-Referenz und lokalem Raster.

Earth v1 speichert genau einen globalen Referenzpunkt pro WorldInstance. Aus
diesem Punkt wird deterministisch ein lokaler, chunk-ausgerichteter
Speicherframe abgeleitet. Blöcke, Chunks, Snapshots, Events und Spawnpositionen
bleiben anschließend lokal gespeichert.

Der entscheidende Unterschied zwischen exaktem Referenzpunkt und Speicherframe:

```
exakter globaler Referenzpunkt
    ↓ CRS-Transformation nach EPSG:4979
exakte, möglicherweise gebrochene Earth-Grid-Position
    ↓ floor-snap auf globales Chunkraster
abgeleiteter chunk-ausgerichteter Speicherursprung
    ↓
lokale Block-, Chunk- und Sub-Block-Koordinaten
```

Dadurch gelten gleichzeitig:

* Nur ein globaler Punkt wird persistiert.
* Die exakte Referenzkoordinate bleibt erhalten.
* Alle Earth-Projekte verwenden dieselbe Rasterphase und Chunkaufteilung.
* Ein Projekt erzeugt kein frei verschobenes oder gedrehtes Parallelraster.
* Der lokale Speicherursprung ist vollständig aus der Referenz ableitbar.
* Ein Cache-Miss kann den Frame jederzeit reproduzierbar neu berechnen.
* Die X-Weltnaht bleibt unabhängig vom Referenzpunkt exakt periodisch.

Projektionsmodell v1
--------------------
Die horizontale Earth-Grid-Abbildung ist eine normierte, periodische
equirektangulare Abbildung:

```
grid_x = canonical_longitude_delta / 360° * world_width_cells
grid_z = latitude / 180° * world_height_cells
```

X wird in ``[-world_width/2, world_width/2)`` kanonisiert. Die Pole sind in
Version 1 nicht adressierbare Grenzlinien; Referenz- und Zielpunkte müssen
streng zwischen -90° und +90° liegen.

Dieses Modell erzeugt ein einheitliches, flaches Spielraster. Es behauptet
nicht, weltweit alle geodätischen Entfernungen, Flächen oder Winkel exakt zu
erhalten. Reale Analysen verwenden weiterhin geodätische oder Quell-CRS-
Berechnungen.

Vertikales Modell v1
--------------------
Wenn der globale Punkt eine Höhe enthält, gilt:

```
grid_y = ellipsoidische Höhe / meters_per_cell
```

Ohne globale Höhe bleibt die vertikale Georeferenz ungelöst. Ein 2D-Punkt darf
nicht still eine absolute Höhe erfinden. Lokale Y-Werte können weiterhin
gespeichert werden, sind dann aber nicht in eine globale 3D-Höhe umrechenbar.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from functools import lru_cache
from hashlib import sha256
import json
from math import isfinite
from typing import Any, ClassVar, Final, Mapping, Sequence, Self

from ..coordinates.models import (
    AxisConvention,
    JsonValue,
    LocalBlockPosition,
    LocalMetricPosition,
    SIGNED_INT64_MAX,
    SIGNED_INT64_MIN,
)
from ..coordinates.topology import (
    NorthSouthPolicy,
    PeriodicXTopology,
    get_periodic_x_topology,
)
from .contracts import (
    CoordinateTransformRequest,
    CrsDefinition,
    EarthGridPosition,
    EarthGridReference,
    GlobalCoordinate,
    GlobalReferencePoint,
    ResolvedEarthAnchor,
    TransformationAccuracy,
    TransformationOperationKind,
    TransformationPolicy,
    decimal_to_canonical_string,
)
from .crs import canonical_geographic_crs, crs_equivalent
from .errors import (
    EarthReferenceInvalidError,
    GeoreferencingConfigurationError,
    GeoreferencingValidationError,
    TransformationPrecisionExceededError,
)
from .transformer import (
    TransformerSelectionOptions,
    transform_coordinate,
)


EARTH_GRID_MAPPING_ID: Final[str] = (
    "vectoplan-periodic-equirectangular"
)
EARTH_GRID_MAPPING_VERSION: Final[str] = "1"
EARTH_GRID_TOPOLOGY_TYPE: Final[str] = "periodic-x-v1"
EARTH_GRID_STORAGE_ORIGIN_POLICY: Final[str] = (
    "global-chunk-origin-floor-v1"
)
EARTH_GRID_RESOLVER_VERSION: Final[str] = "earth-grid-resolver.v1"

DEFAULT_EARTH_GRID_ID: Final[str] = "vectoplan-earth-grid"
DEFAULT_EARTH_GRID_VERSION: Final[str] = "1"
DEFAULT_EARTH_WORLD_WIDTH_CELLS: Final[int] = 40_000_000
DEFAULT_EARTH_WORLD_HEIGHT_CELLS: Final[int] = 20_000_000
DEFAULT_EARTH_CHUNK_SIZE: Final[int] = 16
DEFAULT_EARTH_METERS_PER_CELL: Final[Decimal] = Decimal("1")
DEFAULT_EARTH_CENTRAL_MERIDIAN_DEG: Final[Decimal] = Decimal("0")
DEFAULT_POLE_EXCLUSION_EPSILON_DEG: Final[Decimal] = Decimal(
    "0.000000000001"
)

_EARTH_GRID_DEFINITION_CACHE_SIZE: Final[int] = 64
_EARTH_GRID_FRAME_CACHE_SIZE: Final[int] = 512
_DECIMAL_MAX_DIGITS: Final[int] = 80
_DECIMAL_MAX_ABSOLUTE_EXPONENT: Final[int] = 1_000


@dataclass(frozen=True, slots=True)
class EarthGridDefinition:
    """Unveränderliche, global einheitliche Earth-Grid-Definition."""

    grid: EarthGridReference
    world_width_cells: int
    world_height_cells: int
    chunk_size: int
    meters_per_cell: Decimal
    central_meridian_deg: Decimal = (
        DEFAULT_EARTH_CENTRAL_MERIDIAN_DEG
    )
    pole_exclusion_epsilon_deg: Decimal = (
        DEFAULT_POLE_EXCLUSION_EPSILON_DEG
    )
    storage_origin_policy: str = (
        EARTH_GRID_STORAGE_ORIGIN_POLICY
    )

    schema_version: ClassVar[str] = "earth-grid-definition.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.grid, EarthGridReference):
            raise GeoreferencingValidationError(
                "grid muss eine EarthGridReference sein.",
                details={"actualType": type(self.grid).__name__},
            )

        width = _require_positive_even_int64(
            self.world_width_cells,
            field_name="worldWidthCells",
        )
        height = _require_positive_even_int64(
            self.world_height_cells,
            field_name="worldHeightCells",
        )
        chunk_size = _require_positive_int(
            self.chunk_size,
            field_name="chunkSize",
        )
        meters_per_cell = _require_positive_decimal(
            self.meters_per_cell,
            field_name="metersPerCell",
        )
        central_meridian = _normalize_longitude_decimal(
            _require_decimal(
                self.central_meridian_deg,
                field_name="centralMeridianDeg",
            )
        )
        pole_epsilon = _require_positive_decimal(
            self.pole_exclusion_epsilon_deg,
            field_name="poleExclusionEpsilonDeg",
        )
        storage_policy = _require_text(
            self.storage_origin_policy,
            field_name="storageOriginPolicy",
            maximum_length=128,
        )

        if width % chunk_size != 0:
            raise GeoreferencingConfigurationError(
                "worldWidthCells muss durch chunkSize teilbar sein.",
                details={
                    "worldWidthCells": width,
                    "chunkSize": chunk_size,
                    "remainder": width % chunk_size,
                },
            )
        if (width // 2) % chunk_size != 0:
            raise GeoreferencingConfigurationError(
                "Die halbe Earth-Weltbreite muss chunk-ausgerichtet sein.",
                details={
                    "halfWorldWidthCells": width // 2,
                    "chunkSize": chunk_size,
                },
            )
        if height % chunk_size != 0:
            raise GeoreferencingConfigurationError(
                "worldHeightCells muss durch chunkSize teilbar sein.",
                details={
                    "worldHeightCells": height,
                    "chunkSize": chunk_size,
                    "remainder": height % chunk_size,
                },
            )
        if (height // 2) % chunk_size != 0:
            raise GeoreferencingConfigurationError(
                "Die halbe Earth-Welthöhe muss chunk-ausgerichtet sein.",
                details={
                    "halfWorldHeightCells": height // 2,
                    "chunkSize": chunk_size,
                },
            )

        if pole_epsilon >= Decimal("90"):
            raise GeoreferencingConfigurationError(
                "poleExclusionEpsilonDeg muss kleiner als 90° sein.",
                details={
                    "poleExclusionEpsilonDeg": (
                        decimal_to_canonical_string(pole_epsilon)
                    ),
                },
            )

        if (
            self.grid.projection_id != EARTH_GRID_MAPPING_ID
            or self.grid.projection_version
            != EARTH_GRID_MAPPING_VERSION
        ):
            raise GeoreferencingConfigurationError(
                "EarthGridReference verwendet nicht das v1-Mapping.",
                details={
                    "expectedProjectionId": EARTH_GRID_MAPPING_ID,
                    "expectedProjectionVersion": (
                        EARTH_GRID_MAPPING_VERSION
                    ),
                    "actualProjectionId": self.grid.projection_id,
                    "actualProjectionVersion": (
                        self.grid.projection_version
                    ),
                },
            )
        if self.grid.topology_type != EARTH_GRID_TOPOLOGY_TYPE:
            raise GeoreferencingConfigurationError(
                "EarthGridReference verwendet nicht periodic-x-v1.",
                details={
                    "expectedTopologyType": (
                        EARTH_GRID_TOPOLOGY_TYPE
                    ),
                    "actualTopologyType": (
                        self.grid.topology_type
                    ),
                },
            )
        if (
            self.grid.axis_convention
            is not AxisConvention.X_EAST_Y_UP_Z_NORTH
        ):
            raise GeoreferencingConfigurationError(
                "Earth v1 verlangt x-east-y-up-z-north.",
                details={
                    "actualAxisConvention": (
                        self.grid.axis_convention.value
                    ),
                },
            )
        if storage_policy != EARTH_GRID_STORAGE_ORIGIN_POLICY:
            raise GeoreferencingConfigurationError(
                "Earth v1 unterstützt nur den globalen Chunk-Origin-Snap.",
                details={
                    "expectedStorageOriginPolicy": (
                        EARTH_GRID_STORAGE_ORIGIN_POLICY
                    ),
                    "actualStorageOriginPolicy": storage_policy,
                },
            )

        object.__setattr__(self, "world_width_cells", width)
        object.__setattr__(self, "world_height_cells", height)
        object.__setattr__(self, "chunk_size", chunk_size)
        object.__setattr__(
            self,
            "meters_per_cell",
            meters_per_cell,
        )
        object.__setattr__(
            self,
            "central_meridian_deg",
            central_meridian,
        )
        object.__setattr__(
            self,
            "pole_exclusion_epsilon_deg",
            pole_epsilon,
        )
        object.__setattr__(
            self,
            "storage_origin_policy",
            storage_policy,
        )

    @classmethod
    def default(cls) -> Self:
        return get_default_earth_grid_definition()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        if not isinstance(payload, Mapping):
            raise GeoreferencingValidationError(
                "EarthGridDefinition muss als Mapping übergeben werden.",
                details={"actualType": type(payload).__name__},
            )

        grid_payload = payload.get("grid")
        if not isinstance(grid_payload, Mapping):
            raise GeoreferencingValidationError(
                "EarthGridDefinition benötigt ein grid-Mapping."
            )

        required = (
            "worldWidthCells",
            "worldHeightCells",
            "chunkSize",
            "metersPerCell",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise GeoreferencingValidationError(
                "EarthGridDefinition-Pflichtfelder fehlen.",
                details={"missingFields": missing},
            )

        return cls(
            grid=EarthGridReference.from_mapping(grid_payload),
            world_width_cells=payload["worldWidthCells"],
            world_height_cells=payload["worldHeightCells"],
            chunk_size=payload["chunkSize"],
            meters_per_cell=payload["metersPerCell"],
            central_meridian_deg=payload.get(
                "centralMeridianDeg",
                DEFAULT_EARTH_CENTRAL_MERIDIAN_DEG,
            ),
            pole_exclusion_epsilon_deg=payload.get(
                "poleExclusionEpsilonDeg",
                DEFAULT_POLE_EXCLUSION_EPSILON_DEG,
            ),
            storage_origin_policy=payload.get(
                "storageOriginPolicy",
                EARTH_GRID_STORAGE_ORIGIN_POLICY,
            ),
        )

    @property
    def half_world_width_cells(self) -> int:
        return self.world_width_cells // 2

    @property
    def half_world_height_cells(self) -> int:
        return self.world_height_cells // 2

    @property
    def world_width_chunks(self) -> int:
        return self.world_width_cells // self.chunk_size

    @property
    def world_height_chunks(self) -> int:
        return self.world_height_cells // self.chunk_size

    @property
    def global_minimum_block_x(self) -> int:
        return -self.half_world_width_cells

    @property
    def global_maximum_block_x(self) -> int:
        return self.half_world_width_cells - 1

    @property
    def global_minimum_block_z(self) -> int:
        return -self.half_world_height_cells

    @property
    def global_maximum_block_z(self) -> int:
        return self.half_world_height_cells - 1

    @property
    def minimum_addressable_latitude_deg(self) -> Decimal:
        return Decimal("-90") + self.pole_exclusion_epsilon_deg

    @property
    def maximum_addressable_latitude_deg(self) -> Decimal:
        return Decimal("90") - self.pole_exclusion_epsilon_deg

    @property
    def key(self) -> str:
        return self.grid.key

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.fingerprint_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def fingerprint_payload(self) -> dict[str, JsonValue]:
        """Kanonischer Payload ohne selbstreferenzierten Fingerprint."""

        return {
            "schemaVersion": self.schema_version,
            "grid": self.grid.to_dict(),
            "worldWidthCells": self.world_width_cells,
            "worldHeightCells": self.world_height_cells,
            "chunkSize": self.chunk_size,
            "metersPerCell": decimal_to_canonical_string(
                self.meters_per_cell
            ),
            "centralMeridianDeg": decimal_to_canonical_string(
                self.central_meridian_deg
            ),
            "poleExclusionEpsilonDeg": (
                decimal_to_canonical_string(
                    self.pole_exclusion_epsilon_deg
                )
            ),
            "storageOriginPolicy": self.storage_origin_policy,
            "horizontalMapping": EARTH_GRID_MAPPING_ID,
            "verticalMapping": (
                "ellipsoid-height-divided-by-cell-size-v1"
            ),
        }

    def topology_for_storage_origin(
        self,
        storage_origin: "EarthStorageOrigin",
    ) -> PeriodicXTopology:
        if not isinstance(storage_origin, EarthStorageOrigin):
            raise GeoreferencingValidationError(
                "storage_origin muss EarthStorageOrigin sein.",
                details={
                    "actualType": type(storage_origin).__name__,
                },
            )

        minimum_local_z = (
            self.global_minimum_block_z - storage_origin.z
        )
        maximum_local_z = (
            self.global_maximum_block_z - storage_origin.z
        )

        return get_periodic_x_topology(
            world_width_blocks=self.world_width_cells,
            chunk_size=self.chunk_size,
            north_south_policy=NorthSouthPolicy.BOUNDED,
            minimum_z=minimum_local_z,
            maximum_z=maximum_local_z,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "grid": self.grid.to_dict(),
            "fingerprint": self.fingerprint,
            "worldWidthCells": self.world_width_cells,
            "worldHeightCells": self.world_height_cells,
            "worldWidthChunks": self.world_width_chunks,
            "worldHeightChunks": self.world_height_chunks,
            "chunkSize": self.chunk_size,
            "metersPerCell": decimal_to_canonical_string(
                self.meters_per_cell
            ),
            "centralMeridianDeg": decimal_to_canonical_string(
                self.central_meridian_deg
            ),
            "poleExclusionEpsilonDeg": (
                decimal_to_canonical_string(
                    self.pole_exclusion_epsilon_deg
                )
            ),
            "minimumAddressableLatitudeDeg": (
                decimal_to_canonical_string(
                    self.minimum_addressable_latitude_deg
                )
            ),
            "maximumAddressableLatitudeDeg": (
                decimal_to_canonical_string(
                    self.maximum_addressable_latitude_deg
                )
            ),
            "globalBlockXRange": {
                "minimumInclusive": self.global_minimum_block_x,
                "maximumInclusive": self.global_maximum_block_x,
            },
            "globalBlockZRange": {
                "minimumInclusive": self.global_minimum_block_z,
                "maximumInclusive": self.global_maximum_block_z,
            },
            "storageOriginPolicy": self.storage_origin_policy,
            "horizontalMapping": EARTH_GRID_MAPPING_ID,
            "verticalMapping": "ellipsoid-height-divided-by-cell-size-v1",
            "polesAddressable": False,
        }


@dataclass(frozen=True, slots=True)
class LocalEarthPosition:
    """Lokale Sub-Zell-Position relativ zum abgeleiteten Speicherursprung.

    ``y=None`` bedeutet, dass keine global auflösbare Höhe vorliegt. X und Z
    sind immer bestimmt.
    """

    x: Decimal
    y: Decimal | None
    z: Decimal

    coordinate_space: ClassVar[str] = "earth_local_grid"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "x",
            _require_decimal(self.x, field_name="localX"),
        )
        object.__setattr__(
            self,
            "y",
            (
                _require_decimal(self.y, field_name="localY")
                if self.y is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "z",
            _require_decimal(self.z, field_name="localZ"),
        )

    @classmethod
    def from_values(
        cls,
        x: Any,
        y: Any,
        z: Any,
    ) -> Self:
        return cls(x=x, y=y, z=z)

    @classmethod
    def from_block_position(
        cls,
        position: LocalBlockPosition,
    ) -> Self:
        if not isinstance(position, LocalBlockPosition):
            raise GeoreferencingValidationError(
                "position muss LocalBlockPosition sein.",
                details={"actualType": type(position).__name__},
            )
        return cls(
            x=Decimal(position.x),
            y=Decimal(position.y),
            z=Decimal(position.z),
        )

    @classmethod
    def from_metric_position(
        cls,
        position: LocalMetricPosition,
        *,
        meters_per_cell: Decimal,
    ) -> Self:
        if not isinstance(position, LocalMetricPosition):
            raise GeoreferencingValidationError(
                "position muss LocalMetricPosition sein.",
                details={"actualType": type(position).__name__},
            )
        cell_size = _require_positive_decimal(
            meters_per_cell,
            field_name="metersPerCell",
        )
        return cls(
            x=Decimal(str(position.x)) / cell_size,
            y=Decimal(str(position.y)) / cell_size,
            z=Decimal(str(position.z)) / cell_size,
        )

    def to_metric_position(
        self,
        *,
        meters_per_cell: Decimal,
    ) -> LocalMetricPosition:
        if self.y is None:
            raise EarthReferenceInvalidError.for_reason(
                "Lokale Position besitzt keine global auflösbare Y-Komponente."
            )
        cell_size = _require_positive_decimal(
            meters_per_cell,
            field_name="metersPerCell",
        )
        return LocalMetricPosition(
            x=float(self.x * cell_size),
            y=float(self.y * cell_size),
            z=float(self.z * cell_size),
        )

    @property
    def vertical_resolved(self) -> bool:
        return self.y is not None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "coordinateSpace": self.coordinate_space,
            "x": decimal_to_canonical_string(self.x),
            "y": (
                decimal_to_canonical_string(self.y)
                if self.y is not None
                else None
            ),
            "z": decimal_to_canonical_string(self.z),
            "verticalResolved": self.vertical_resolved,
        }


@dataclass(frozen=True, slots=True)
class EarthStorageOrigin:
    """Abgeleiteter, global chunk-ausgerichteter Speicherursprung."""

    x: int
    y: int
    z: int
    vertical_resolved: bool

    coordinate_space: ClassVar[str] = "earth_grid_chunk_origin"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "x",
            _require_int64(self.x, field_name="originX"),
        )
        object.__setattr__(
            self,
            "y",
            _require_int64(self.y, field_name="originY"),
        )
        object.__setattr__(
            self,
            "z",
            _require_int64(self.z, field_name="originZ"),
        )
        object.__setattr__(
            self,
            "vertical_resolved",
            bool(self.vertical_resolved),
        )

    def validate_against(
        self,
        definition: EarthGridDefinition,
    ) -> Self:
        if not isinstance(definition, EarthGridDefinition):
            raise GeoreferencingValidationError(
                "definition muss EarthGridDefinition sein."
            )

        for axis, value in (
            ("x", self.x),
            ("y", self.y),
            ("z", self.z),
        ):
            if value % definition.chunk_size != 0:
                raise GeoreferencingConfigurationError(
                    "EarthStorageOrigin ist nicht chunk-ausgerichtet.",
                    details={
                        "axis": axis,
                        "value": value,
                        "chunkSize": definition.chunk_size,
                    },
                )

        if (
            self.x < definition.global_minimum_block_x
            or self.x > definition.global_maximum_block_x
        ):
            raise GeoreferencingConfigurationError(
                "EarthStorageOrigin.x liegt außerhalb der Weltbreite.",
                details={
                    "originX": self.x,
                    "minimum": definition.global_minimum_block_x,
                    "maximum": definition.global_maximum_block_x,
                },
            )

        if (
            self.z < definition.global_minimum_block_z
            or self.z > definition.global_maximum_block_z
        ):
            raise GeoreferencingConfigurationError(
                "EarthStorageOrigin.z liegt außerhalb der Welthöhe.",
                details={
                    "originZ": self.z,
                    "minimum": definition.global_minimum_block_z,
                    "maximum": definition.global_maximum_block_z,
                },
            )

        return self

    def to_grid_position(self) -> EarthGridPosition:
        return EarthGridPosition(
            x=Decimal(self.x),
            y=Decimal(self.y),
            z=Decimal(self.z),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "coordinateSpace": self.coordinate_space,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "verticalResolved": self.vertical_resolved,
        }


@dataclass(frozen=True, slots=True)
class EarthGridMappingResult:
    """Ergebnis einer globalen Koordinate im kanonischen Earth-Grid."""

    input_coordinate: GlobalCoordinate
    input_crs: CrsDefinition
    canonical_coordinate: GlobalCoordinate
    canonical_crs: CrsDefinition
    grid_position: EarthGridPosition
    accuracy: TransformationAccuracy
    vertical_resolved: bool

    schema_version: ClassVar[str] = "earth-grid-mapping-result.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.input_coordinate, GlobalCoordinate):
            raise GeoreferencingValidationError(
                "input_coordinate muss GlobalCoordinate sein."
            )
        if not isinstance(self.input_crs, CrsDefinition):
            raise GeoreferencingValidationError(
                "input_crs muss CrsDefinition sein."
            )
        if not isinstance(
            self.canonical_coordinate,
            GlobalCoordinate,
        ):
            raise GeoreferencingValidationError(
                "canonical_coordinate muss GlobalCoordinate sein."
            )
        if not isinstance(self.canonical_crs, CrsDefinition):
            raise GeoreferencingValidationError(
                "canonical_crs muss CrsDefinition sein."
            )
        if not isinstance(self.grid_position, EarthGridPosition):
            raise GeoreferencingValidationError(
                "grid_position muss EarthGridPosition sein."
            )
        if not isinstance(self.accuracy, TransformationAccuracy):
            raise GeoreferencingValidationError(
                "accuracy muss TransformationAccuracy sein."
            )
        object.__setattr__(
            self,
            "vertical_resolved",
            bool(self.vertical_resolved),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "inputCoordinate": self.input_coordinate.to_dict(),
            "inputCrs": self.input_crs.to_dict(),
            "canonicalCoordinate": (
                self.canonical_coordinate.to_dict()
            ),
            "canonicalCrs": self.canonical_crs.to_dict(),
            "gridPosition": self.grid_position.to_dict(),
            "accuracy": self.accuracy.to_dict(),
            "verticalResolved": self.vertical_resolved,
        }


@dataclass(frozen=True, slots=True)
class EarthGridFrame:
    """Vollständig abgeleiteter lokaler Speicherframe einer Earth-World."""

    reference: GlobalReferencePoint
    definition: EarthGridDefinition
    resolved_anchor: ResolvedEarthAnchor
    storage_origin: EarthStorageOrigin
    reference_local_position: LocalEarthPosition
    topology: PeriodicXTopology

    schema_version: ClassVar[str] = "earth-grid-frame.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.reference, GlobalReferencePoint):
            raise GeoreferencingValidationError(
                "reference muss GlobalReferencePoint sein."
            )
        if not isinstance(self.definition, EarthGridDefinition):
            raise GeoreferencingValidationError(
                "definition muss EarthGridDefinition sein."
            )
        if not isinstance(self.resolved_anchor, ResolvedEarthAnchor):
            raise GeoreferencingValidationError(
                "resolved_anchor muss ResolvedEarthAnchor sein."
            )
        if not isinstance(self.storage_origin, EarthStorageOrigin):
            raise GeoreferencingValidationError(
                "storage_origin muss EarthStorageOrigin sein."
            )
        if not isinstance(
            self.reference_local_position,
            LocalEarthPosition,
        ):
            raise GeoreferencingValidationError(
                "reference_local_position muss LocalEarthPosition sein."
            )
        if not isinstance(self.topology, PeriodicXTopology):
            raise GeoreferencingValidationError(
                "topology muss PeriodicXTopology sein."
            )

        self.storage_origin.validate_against(self.definition)

        if self.reference.grid != self.definition.grid:
            raise GeoreferencingConfigurationError(
                "Referenz und EarthGridDefinition verwenden verschiedene Grids.",
                details={
                    "referenceGrid": self.reference.grid.to_dict(),
                    "definitionGrid": self.definition.grid.to_dict(),
                },
            )

        if (
            self.reference_local_position.vertical_resolved
            != self.storage_origin.vertical_resolved
        ):
            raise GeoreferencingConfigurationError(
                "Vertikaler Auflösungsstatus von Ursprung und Referenz ist inkonsistent."
            )

        expected_topology = self.definition.topology_for_storage_origin(
            self.storage_origin
        )
        if self.topology != expected_topology:
            raise GeoreferencingConfigurationError(
                "EarthGridFrame enthält eine inkonsistente Topologie."
            )

    @property
    def cache_key(self) -> str:
        payload = {
            "referenceFingerprint": self.reference.fingerprint,
            "definitionFingerprint": self.definition.fingerprint,
            "resolverVersion": EARTH_GRID_RESOLVER_VERSION,
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def normalize_local_position(
        self,
        position: LocalEarthPosition,
    ) -> LocalEarthPosition:
        if not isinstance(position, LocalEarthPosition):
            raise GeoreferencingValidationError(
                "position muss LocalEarthPosition sein."
            )

        canonical_x = _normalize_centered_decimal(
            position.x,
            width=Decimal(self.definition.world_width_cells),
        )
        _validate_local_z_decimal(
            position.z,
            topology=self.topology,
        )

        return LocalEarthPosition(
            x=canonical_x,
            y=position.y,
            z=position.z,
        )

    def local_block_to_grid(
        self,
        position: LocalBlockPosition,
    ) -> EarthGridPosition:
        normalized = self.topology.normalize_block_position(
            position
        ).canonical

        return EarthGridPosition(
            x=_normalize_centered_decimal(
                Decimal(self.storage_origin.x + normalized.x),
                width=Decimal(
                    self.definition.world_width_cells
                ),
            ),
            y=Decimal(self.storage_origin.y + normalized.y),
            z=Decimal(self.storage_origin.z + normalized.z),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "cacheKey": self.cache_key,
            "resolverVersion": EARTH_GRID_RESOLVER_VERSION,
            "reference": self.reference.to_dict(),
            "definition": self.definition.to_dict(),
            "resolvedAnchor": self.resolved_anchor.to_dict(),
            "storageOrigin": self.storage_origin.to_dict(),
            "referenceLocalPosition": (
                self.reference_local_position.to_dict()
            ),
            "topology": self.topology.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class GlobalToLocalResult:
    """Globale Zielkoordinate als lokale Position eines EarthGridFrame."""

    frame_cache_key: str
    mapping: EarthGridMappingResult
    local_position: LocalEarthPosition

    schema_version: ClassVar[str] = "earth-global-to-local.v1"

    def __post_init__(self) -> None:
        _require_hash(
            self.frame_cache_key,
            field_name="frameCacheKey",
        )
        if not isinstance(self.mapping, EarthGridMappingResult):
            raise GeoreferencingValidationError(
                "mapping muss EarthGridMappingResult sein."
            )
        if not isinstance(self.local_position, LocalEarthPosition):
            raise GeoreferencingValidationError(
                "local_position muss LocalEarthPosition sein."
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "frameCacheKey": self.frame_cache_key,
            "mapping": self.mapping.to_dict(),
            "localPosition": self.local_position.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LocalToGlobalResult:
    """Lokale Position als globale Koordinate eines gewünschten CRS."""

    frame_cache_key: str
    local_position: LocalEarthPosition
    grid_position: EarthGridPosition
    canonical_coordinate: GlobalCoordinate
    target_coordinate: GlobalCoordinate
    target_crs: CrsDefinition
    accuracy: TransformationAccuracy
    vertical_resolved: bool

    schema_version: ClassVar[str] = "earth-local-to-global.v1"

    def __post_init__(self) -> None:
        _require_hash(
            self.frame_cache_key,
            field_name="frameCacheKey",
        )
        if not isinstance(self.local_position, LocalEarthPosition):
            raise GeoreferencingValidationError(
                "local_position muss LocalEarthPosition sein."
            )
        if not isinstance(self.grid_position, EarthGridPosition):
            raise GeoreferencingValidationError(
                "grid_position muss EarthGridPosition sein."
            )
        if not isinstance(
            self.canonical_coordinate,
            GlobalCoordinate,
        ):
            raise GeoreferencingValidationError(
                "canonical_coordinate muss GlobalCoordinate sein."
            )
        if not isinstance(self.target_coordinate, GlobalCoordinate):
            raise GeoreferencingValidationError(
                "target_coordinate muss GlobalCoordinate sein."
            )
        if not isinstance(self.target_crs, CrsDefinition):
            raise GeoreferencingValidationError(
                "target_crs muss CrsDefinition sein."
            )
        if not isinstance(self.accuracy, TransformationAccuracy):
            raise GeoreferencingValidationError(
                "accuracy muss TransformationAccuracy sein."
            )
        object.__setattr__(
            self,
            "vertical_resolved",
            bool(self.vertical_resolved),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "frameCacheKey": self.frame_cache_key,
            "localPosition": self.local_position.to_dict(),
            "gridPosition": self.grid_position.to_dict(),
            "canonicalCoordinate": (
                self.canonical_coordinate.to_dict()
            ),
            "targetCoordinate": self.target_coordinate.to_dict(),
            "targetCrs": self.target_crs.to_dict(),
            "accuracy": self.accuracy.to_dict(),
            "verticalResolved": self.vertical_resolved,
        }


def get_default_earth_grid_definition() -> EarthGridDefinition:
    """Liefert die versionierte Earth-v1-Defaultdefinition."""

    return get_earth_grid_definition(
        grid_id=DEFAULT_EARTH_GRID_ID,
        grid_version=DEFAULT_EARTH_GRID_VERSION,
        world_width_cells=DEFAULT_EARTH_WORLD_WIDTH_CELLS,
        world_height_cells=DEFAULT_EARTH_WORLD_HEIGHT_CELLS,
        chunk_size=DEFAULT_EARTH_CHUNK_SIZE,
        meters_per_cell=DEFAULT_EARTH_METERS_PER_CELL,
        central_meridian_deg=(
            DEFAULT_EARTH_CENTRAL_MERIDIAN_DEG
        ),
        pole_exclusion_epsilon_deg=(
            DEFAULT_POLE_EXCLUSION_EPSILON_DEG
        ),
    )


def get_earth_grid_definition(
    *,
    grid_id: str,
    grid_version: str,
    world_width_cells: int,
    world_height_cells: int,
    chunk_size: int,
    meters_per_cell: Any,
    central_meridian_deg: Any = (
        DEFAULT_EARTH_CENTRAL_MERIDIAN_DEG
    ),
    pole_exclusion_epsilon_deg: Any = (
        DEFAULT_POLE_EXCLUSION_EPSILON_DEG
    ),
) -> EarthGridDefinition:
    """Liefert eine gecachte, unveränderliche Griddefinition."""

    normalized_meters = _require_positive_decimal(
        meters_per_cell,
        field_name="metersPerCell",
    )
    normalized_central = _normalize_longitude_decimal(
        _require_decimal(
            central_meridian_deg,
            field_name="centralMeridianDeg",
        )
    )
    normalized_epsilon = _require_positive_decimal(
        pole_exclusion_epsilon_deg,
        field_name="poleExclusionEpsilonDeg",
    )

    return _get_earth_grid_definition_cached(
        _require_text(
            grid_id,
            field_name="gridId",
            maximum_length=256,
        ),
        _require_text(
            grid_version,
            field_name="gridVersion",
            maximum_length=128,
        ),
        _require_positive_even_int64(
            world_width_cells,
            field_name="worldWidthCells",
        ),
        _require_positive_even_int64(
            world_height_cells,
            field_name="worldHeightCells",
        ),
        _require_positive_int(
            chunk_size,
            field_name="chunkSize",
        ),
        normalized_meters,
        normalized_central,
        normalized_epsilon,
    )


@lru_cache(maxsize=_EARTH_GRID_DEFINITION_CACHE_SIZE)
def _get_earth_grid_definition_cached(
    grid_id: str,
    grid_version: str,
    world_width_cells: int,
    world_height_cells: int,
    chunk_size: int,
    meters_per_cell: Decimal,
    central_meridian_deg: Decimal,
    pole_exclusion_epsilon_deg: Decimal,
) -> EarthGridDefinition:
    grid_reference = EarthGridReference(
        grid_id=grid_id,
        grid_version=grid_version,
        projection_id=EARTH_GRID_MAPPING_ID,
        projection_version=EARTH_GRID_MAPPING_VERSION,
        topology_type=EARTH_GRID_TOPOLOGY_TYPE,
        axis_convention=AxisConvention.X_EAST_Y_UP_Z_NORTH,
    )
    return EarthGridDefinition(
        grid=grid_reference,
        world_width_cells=world_width_cells,
        world_height_cells=world_height_cells,
        chunk_size=chunk_size,
        meters_per_cell=meters_per_cell,
        central_meridian_deg=central_meridian_deg,
        pole_exclusion_epsilon_deg=(
            pole_exclusion_epsilon_deg
        ),
    )


def map_global_coordinate_to_grid(
    coordinate: GlobalCoordinate,
    source_crs: CrsDefinition,
    *,
    definition: EarthGridDefinition,
    policy: TransformationPolicy | None = None,
    options: TransformerSelectionOptions | None = None,
    operation: TransformationOperationKind = (
        TransformationOperationKind.GLOBAL_TO_LOCAL
    ),
) -> EarthGridMappingResult:
    """Transformiert eine globale Koordinate in das Earth-Grid."""

    if not isinstance(coordinate, GlobalCoordinate):
        raise GeoreferencingValidationError(
            "coordinate muss GlobalCoordinate sein.",
            details={"actualType": type(coordinate).__name__},
        )
    if not isinstance(source_crs, CrsDefinition):
        raise GeoreferencingValidationError(
            "source_crs muss CrsDefinition sein.",
            details={"actualType": type(source_crs).__name__},
        )
    if not isinstance(definition, EarthGridDefinition):
        raise GeoreferencingValidationError(
            "definition muss EarthGridDefinition sein."
        )

    active_policy = policy or TransformationPolicy.strict_default()
    canonical_crs = canonical_geographic_crs()
    request = CoordinateTransformRequest(
        coordinate=coordinate,
        source_crs=source_crs,
        target_crs=canonical_crs,
        operation=operation,
        policy=active_policy,
    )
    transformed = transform_coordinate(
        request,
        options=options,
    )
    canonical = transformed.coordinate
    grid_position = _canonical_geographic_to_grid_position(
        canonical,
        definition=definition,
    )

    return EarthGridMappingResult(
        input_coordinate=coordinate,
        input_crs=source_crs,
        canonical_coordinate=canonical,
        canonical_crs=canonical_crs,
        grid_position=grid_position,
        accuracy=transformed.accuracy,
        vertical_resolved=canonical.z is not None,
    )


def resolve_earth_grid_frame(
    reference: GlobalReferencePoint,
    *,
    definition: EarthGridDefinition | None = None,
    policy: TransformationPolicy | None = None,
    options: TransformerSelectionOptions | None = None,
) -> EarthGridFrame:
    """Löst den einen globalen Referenzpunkt in einen lokalen Speicherframe auf."""

    if not isinstance(reference, GlobalReferencePoint):
        raise GeoreferencingValidationError(
            "reference muss GlobalReferencePoint sein.",
            details={"actualType": type(reference).__name__},
        )

    active_definition = (
        definition
        if definition is not None
        else get_default_earth_grid_definition()
    )
    active_policy = policy or TransformationPolicy.strict_default()
    active_options = options or TransformerSelectionOptions.default()

    if reference.grid != active_definition.grid:
        raise GeoreferencingConfigurationError(
            "GlobalReferencePoint und EarthGridDefinition verwenden verschiedene Grids.",
            details={
                "referenceGrid": reference.grid.to_dict(),
                "definitionGrid": active_definition.grid.to_dict(),
            },
        )

    return _resolve_earth_grid_frame_cached(
        reference,
        active_definition,
        active_policy,
        active_options,
    )


@lru_cache(maxsize=_EARTH_GRID_FRAME_CACHE_SIZE)
def _resolve_earth_grid_frame_cached(
    reference: GlobalReferencePoint,
    definition: EarthGridDefinition,
    policy: TransformationPolicy,
    options: TransformerSelectionOptions,
) -> EarthGridFrame:
    mapping = map_global_coordinate_to_grid(
        reference.coordinate,
        reference.crs,
        definition=definition,
        policy=policy,
        options=options,
        operation=(
            TransformationOperationKind.REFERENCE_TO_CANONICAL
        ),
    )

    storage_origin = _derive_storage_origin(
        mapping,
        definition=definition,
    )
    reference_local = _grid_to_local_position(
        mapping.grid_position,
        storage_origin=storage_origin,
        definition=definition,
        vertical_resolved=mapping.vertical_resolved,
    )
    topology = definition.topology_for_storage_origin(
        storage_origin
    )

    resolved_anchor = ResolvedEarthAnchor(
        reference=reference,
        canonical_coordinate=mapping.canonical_coordinate,
        canonical_crs=mapping.canonical_crs,
        grid_position=mapping.grid_position,
        accuracy=mapping.accuracy,
        resolver_version=EARTH_GRID_RESOLVER_VERSION,
    )

    return EarthGridFrame(
        reference=reference,
        definition=definition,
        resolved_anchor=resolved_anchor,
        storage_origin=storage_origin,
        reference_local_position=reference_local,
        topology=topology,
    )


def global_to_local(
    frame: EarthGridFrame,
    coordinate: GlobalCoordinate,
    source_crs: CrsDefinition,
    *,
    policy: TransformationPolicy | None = None,
    options: TransformerSelectionOptions | None = None,
) -> GlobalToLocalResult:
    """Berechnet die kürzeste kanonische lokale Position eines globalen Ziels."""

    if not isinstance(frame, EarthGridFrame):
        raise GeoreferencingValidationError(
            "frame muss EarthGridFrame sein."
        )

    mapping = map_global_coordinate_to_grid(
        coordinate,
        source_crs,
        definition=frame.definition,
        policy=policy,
        options=options,
        operation=TransformationOperationKind.GLOBAL_TO_LOCAL,
    )
    local_position = _grid_to_local_position(
        mapping.grid_position,
        storage_origin=frame.storage_origin,
        definition=frame.definition,
        vertical_resolved=mapping.vertical_resolved,
    )
    local_position = frame.normalize_local_position(
        local_position
    )

    return GlobalToLocalResult(
        frame_cache_key=frame.cache_key,
        mapping=mapping,
        local_position=local_position,
    )


def local_to_global(
    frame: EarthGridFrame,
    position: LocalEarthPosition | LocalBlockPosition,
    *,
    target_crs: CrsDefinition | None = None,
    policy: TransformationPolicy | None = None,
    options: TransformerSelectionOptions | None = None,
) -> LocalToGlobalResult:
    """Berechnet eine globale Koordinate aus einer lokalen Earth-Position."""

    if not isinstance(frame, EarthGridFrame):
        raise GeoreferencingValidationError(
            "frame muss EarthGridFrame sein."
        )

    local_position = (
        LocalEarthPosition.from_block_position(position)
        if isinstance(position, LocalBlockPosition)
        else position
    )
    if not isinstance(local_position, LocalEarthPosition):
        raise GeoreferencingValidationError(
            "position muss LocalEarthPosition oder LocalBlockPosition sein.",
            details={"actualType": type(position).__name__},
        )

    normalized_local = frame.normalize_local_position(
        local_position
    )

    if (
        normalized_local.y is not None
        and not frame.storage_origin.vertical_resolved
    ):
        raise EarthReferenceInvalidError.for_reason(
            "Die Earth-Referenz besitzt kein globales Höhendatum; "
            "eine lokale Y-Position kann nicht global aufgelöst werden.",
            coordinate_dimensions=2,
            crs=frame.reference.crs.crs_id,
        )

    grid_position = _local_to_grid_position(
        normalized_local,
        storage_origin=frame.storage_origin,
        definition=frame.definition,
    )
    canonical_coordinate = _grid_position_to_canonical_geographic(
        grid_position,
        definition=frame.definition,
        include_height=(
            normalized_local.y is not None
            and frame.storage_origin.vertical_resolved
        ),
    )
    canonical_crs = canonical_geographic_crs()
    resolved_target = (
        target_crs
        if target_crs is not None
        else frame.reference.crs
    )
    if not isinstance(resolved_target, CrsDefinition):
        raise GeoreferencingValidationError(
            "target_crs muss CrsDefinition sein."
        )

    active_policy = policy or TransformationPolicy.strict_default()
    request = CoordinateTransformRequest(
        coordinate=canonical_coordinate,
        source_crs=canonical_crs,
        target_crs=resolved_target,
        operation=TransformationOperationKind.LOCAL_TO_GLOBAL,
        policy=active_policy,
    )
    transformed = transform_coordinate(
        request,
        options=options,
    )

    return LocalToGlobalResult(
        frame_cache_key=frame.cache_key,
        local_position=normalized_local,
        grid_position=grid_position,
        canonical_coordinate=canonical_coordinate,
        target_coordinate=transformed.coordinate,
        target_crs=resolved_target,
        accuracy=transformed.accuracy,
        vertical_resolved=canonical_coordinate.z is not None,
    )


def reference_as_local_position(
    frame: EarthGridFrame,
) -> LocalEarthPosition:
    """Liefert die lokale Sub-Zell-Position des exakten Referenzpunkts."""

    if not isinstance(frame, EarthGridFrame):
        raise GeoreferencingValidationError(
            "frame muss EarthGridFrame sein."
        )
    return frame.reference_local_position


def clear_earth_grid_caches() -> None:
    """Leert ausschließlich reproduzierbare Grid- und Frame-Caches."""

    _get_earth_grid_definition_cached.cache_clear()
    _resolve_earth_grid_frame_cached.cache_clear()


def earth_grid_cache_info() -> dict[str, JsonValue]:
    """Liefert serialisierbare Cache-Diagnostik."""

    return {
        "definitions": _cache_info_to_dict(
            _get_earth_grid_definition_cached.cache_info()
        ),
        "frames": _cache_info_to_dict(
            _resolve_earth_grid_frame_cached.cache_info()
        ),
    }


def earth_grid_runtime_status() -> dict[str, JsonValue]:
    """Read-only Smoke-Test der Defaultdefinition und eines Referenzframes."""

    payload: dict[str, JsonValue] = {
        "ok": False,
        "definitionReady": False,
        "frameReady": False,
        "roundtripReady": False,
        "definition": None,
        "frame": None,
        "roundtripErrorCells": None,
        "cache": earth_grid_cache_info(),
        "errors": [],
    }
    errors: list[JsonValue] = payload["errors"]  # type: ignore[assignment]

    try:
        definition = get_default_earth_grid_definition()
        payload["definition"] = definition.to_dict()
        payload["definitionReady"] = True

        canonical_crs = canonical_geographic_crs()
        reference = GlobalReferencePoint(
            coordinate=GlobalCoordinate.from_values(
                "11.576",
                "48.137",
                "560.0",
            ),
            crs=canonical_crs,
            grid=definition.grid,
            reference_version=1,
            source="earth-grid-readiness",
        )
        frame = resolve_earth_grid_frame(
            reference,
            definition=definition,
        )
        payload["frame"] = frame.to_dict()
        payload["frameReady"] = True

        back = local_to_global(
            frame,
            frame.reference_local_position,
            target_crs=canonical_crs,
        )
        returned = back.target_coordinate
        error_cells = _canonical_coordinate_error_cells(
            reference.coordinate,
            returned,
            definition=definition,
        )
        payload["roundtripErrorCells"] = (
            decimal_to_canonical_string(error_cells)
        )
        payload["roundtripReady"] = (
            error_cells <= Decimal("0.000001")
        )
    except Exception as error:
        errors.append(_safe_error(error))

    payload["cache"] = earth_grid_cache_info()
    payload["ok"] = bool(
        payload["definitionReady"]
        and payload["frameReady"]
        and payload["roundtripReady"]
        and not errors
    )
    return payload


def _canonical_geographic_to_grid_position(
    coordinate: GlobalCoordinate,
    *,
    definition: EarthGridDefinition,
) -> EarthGridPosition:
    longitude = _normalize_longitude_decimal(coordinate.x)
    latitude = coordinate.y

    _validate_addressable_latitude(
        latitude,
        definition=definition,
    )

    longitude_delta = _normalize_centered_decimal(
        longitude - definition.central_meridian_deg,
        width=Decimal("360"),
    )
    grid_x = (
        longitude_delta
        / Decimal("360")
        * Decimal(definition.world_width_cells)
    )
    grid_z = (
        latitude
        / Decimal("180")
        * Decimal(definition.world_height_cells)
    )
    grid_y = (
        coordinate.z / definition.meters_per_cell
        if coordinate.z is not None
        else Decimal("0")
    )

    grid_x = _normalize_centered_decimal(
        grid_x,
        width=Decimal(definition.world_width_cells),
    )
    _validate_global_grid_z(
        grid_z,
        definition=definition,
    )

    return EarthGridPosition(
        x=grid_x,
        y=grid_y,
        z=grid_z,
    )


def _grid_position_to_canonical_geographic(
    position: EarthGridPosition,
    *,
    definition: EarthGridDefinition,
    include_height: bool,
) -> GlobalCoordinate:
    if not isinstance(position, EarthGridPosition):
        raise GeoreferencingValidationError(
            "position muss EarthGridPosition sein."
        )

    canonical_x = _normalize_centered_decimal(
        position.x,
        width=Decimal(definition.world_width_cells),
    )
    _validate_global_grid_z(
        position.z,
        definition=definition,
    )

    longitude = _normalize_longitude_decimal(
        definition.central_meridian_deg
        + (
            canonical_x
            / Decimal(definition.world_width_cells)
            * Decimal("360")
        )
    )
    latitude = (
        position.z
        / Decimal(definition.world_height_cells)
        * Decimal("180")
    )
    _validate_addressable_latitude(
        latitude,
        definition=definition,
    )

    if include_height:
        return GlobalCoordinate(
            x=longitude,
            y=latitude,
            z=position.y * definition.meters_per_cell,
        )
    return GlobalCoordinate(
        x=longitude,
        y=latitude,
        z=None,
    )


def _derive_storage_origin(
    mapping: EarthGridMappingResult,
    *,
    definition: EarthGridDefinition,
) -> EarthStorageOrigin:
    exact = mapping.grid_position
    origin_x = _floor_to_chunk_multiple(
        exact.x,
        chunk_size=definition.chunk_size,
    )
    origin_x = _normalize_centered_int(
        origin_x,
        width=definition.world_width_cells,
    )
    origin_z = _floor_to_chunk_multiple(
        exact.z,
        chunk_size=definition.chunk_size,
    )
    origin_y = (
        _floor_to_chunk_multiple(
            exact.y,
            chunk_size=definition.chunk_size,
        )
        if mapping.vertical_resolved
        else 0
    )

    origin = EarthStorageOrigin(
        x=origin_x,
        y=origin_y,
        z=origin_z,
        vertical_resolved=mapping.vertical_resolved,
    )
    return origin.validate_against(definition)


def _grid_to_local_position(
    grid_position: EarthGridPosition,
    *,
    storage_origin: EarthStorageOrigin,
    definition: EarthGridDefinition,
    vertical_resolved: bool,
) -> LocalEarthPosition:
    local_x = _normalize_centered_decimal(
        grid_position.x - Decimal(storage_origin.x),
        width=Decimal(definition.world_width_cells),
    )
    local_z = grid_position.z - Decimal(storage_origin.z)
    local_y = (
        grid_position.y - Decimal(storage_origin.y)
        if vertical_resolved
        else None
    )

    position = LocalEarthPosition(
        x=local_x,
        y=local_y,
        z=local_z,
    )
    topology = definition.topology_for_storage_origin(
        storage_origin
    )
    _validate_local_z_decimal(position.z, topology=topology)
    return position


def _local_to_grid_position(
    local_position: LocalEarthPosition,
    *,
    storage_origin: EarthStorageOrigin,
    definition: EarthGridDefinition,
) -> EarthGridPosition:
    grid_x = _normalize_centered_decimal(
        Decimal(storage_origin.x) + local_position.x,
        width=Decimal(definition.world_width_cells),
    )
    grid_z = Decimal(storage_origin.z) + local_position.z
    grid_y = (
        Decimal(storage_origin.y) + local_position.y
        if local_position.y is not None
        else Decimal(storage_origin.y)
    )

    _validate_global_grid_z(
        grid_z,
        definition=definition,
    )
    return EarthGridPosition(
        x=grid_x,
        y=grid_y,
        z=grid_z,
    )


def _validate_local_z_decimal(
    local_z: Decimal,
    *,
    topology: PeriodicXTopology,
) -> None:
    minimum = Decimal(topology.minimum_z)
    maximum_exclusive = Decimal(topology.maximum_z + 1)
    if local_z < minimum or local_z >= maximum_exclusive:
        raise EarthReferenceInvalidError.for_reason(
            "Lokale Z-Position liegt außerhalb der Earth-v1-Grenzen."
        )


def _validate_global_grid_z(
    grid_z: Decimal,
    *,
    definition: EarthGridDefinition,
) -> None:
    minimum = Decimal(definition.global_minimum_block_z)
    maximum_exclusive = Decimal(
        definition.global_maximum_block_z + 1
    )
    if grid_z <= minimum or grid_z >= maximum_exclusive:
        raise EarthReferenceInvalidError.for_reason(
            "Globale Grid-Z-Position liegt auf oder außerhalb einer Polgrenze."
        )


def _validate_addressable_latitude(
    latitude: Decimal,
    *,
    definition: EarthGridDefinition,
) -> None:
    if (
        latitude < definition.minimum_addressable_latitude_deg
        or latitude
        > definition.maximum_addressable_latitude_deg
    ):
        raise EarthReferenceInvalidError.for_reason(
            "Earth v1 adressiert die Pole nicht; die Breite muss "
            "streng zwischen den konfigurierten Polgrenzen liegen.",
            coordinate_dimensions=2,
            crs="EPSG:4979",
        )


def _canonical_coordinate_error_cells(
    expected: GlobalCoordinate,
    actual: GlobalCoordinate,
    *,
    definition: EarthGridDefinition,
) -> Decimal:
    expected_grid = _canonical_geographic_to_grid_position(
        expected,
        definition=definition,
    )
    actual_grid = _canonical_geographic_to_grid_position(
        actual,
        definition=definition,
    )
    dx = abs(
        _normalize_centered_decimal(
            actual_grid.x - expected_grid.x,
            width=Decimal(definition.world_width_cells),
        )
    )
    dz = abs(actual_grid.z - expected_grid.z)
    dy = (
        abs(actual_grid.y - expected_grid.y)
        if expected.z is not None and actual.z is not None
        else Decimal("0")
    )
    return max(dx, dy, dz)


def _floor_to_chunk_multiple(
    value: Decimal,
    *,
    chunk_size: int,
) -> int:
    quotient = (
        value / Decimal(chunk_size)
    ).to_integral_value(rounding=ROUND_FLOOR)
    result = int(quotient) * chunk_size
    return _require_int64(result, field_name="chunkAlignedOrigin")


def _normalize_centered_decimal(
    value: Decimal,
    *,
    width: Decimal,
) -> Decimal:
    """Kanonisiert Decimal-Werte mit echter Floor-Modulo-Semantik.

    ``Decimal.__mod__`` verwendet bei negativen Werten eine Restsemantik,
    die nicht dem für periodische Koordinaten benötigten Floor-Modulo
    entspricht. Deshalb wird der Quotient explizit nach ``-∞`` gerundet.
    """

    normalized_value = _require_decimal(
        value,
        field_name="periodicValue",
    )
    normalized_width = _require_positive_decimal(
        width,
        field_name="periodicWidth",
    )
    half = normalized_width / Decimal("2")
    shifted = normalized_value + half
    quotient = (
        shifted / normalized_width
    ).to_integral_value(rounding=ROUND_FLOOR)
    remainder = shifted - (quotient * normalized_width)
    canonical = remainder - half

    if canonical < -half or canonical >= half:
        raise GeoreferencingConfigurationError(
            "Decimal-Kanonisierung hat den Zielbereich verletzt.",
            details={
                "value": decimal_to_canonical_string(
                    normalized_value
                ),
                "width": decimal_to_canonical_string(
                    normalized_width
                ),
                "canonical": decimal_to_canonical_string(
                    canonical
                ),
            },
        )

    return canonical


def _normalize_centered_int(
    value: int,
    *,
    width: int,
) -> int:
    normalized_value = _require_int64(
        value,
        field_name="periodicValue",
    )
    normalized_width = _require_positive_even_int64(
        width,
        field_name="periodicWidth",
    )
    half = normalized_width // 2
    return ((normalized_value + half) % normalized_width) - half


def _normalize_longitude_decimal(
    value: Decimal,
) -> Decimal:
    return _normalize_centered_decimal(
        value,
        width=Decimal("360"),
    )


def _require_decimal(
    value: Any,
    *,
    field_name: str,
) -> Decimal:
    if isinstance(value, bool):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Dezimalzahl sein.",
            details={"actualType": "bool"},
        )
    try:
        normalized = (
            value
            if isinstance(value, Decimal)
            else Decimal(str(value).strip())
        )
    except Exception as error:
        raise GeoreferencingValidationError(
            f"'{field_name}' ist keine gültige Dezimalzahl.",
            details={"actualType": type(value).__name__},
            cause=error,
        ) from error

    if not normalized.is_finite():
        raise GeoreferencingValidationError(
            f"'{field_name}' muss endlich sein.",
            details={"value": str(normalized)},
        )

    digits = len(normalized.as_tuple().digits)
    if digits > _DECIMAL_MAX_DIGITS:
        raise GeoreferencingValidationError(
            f"'{field_name}' besitzt zu viele Stellen.",
            details={
                "digitCount": digits,
                "maximumDigits": _DECIMAL_MAX_DIGITS,
            },
        )
    if normalized != 0 and (
        abs(normalized.adjusted())
        > _DECIMAL_MAX_ABSOLUTE_EXPONENT
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' besitzt einen unzulässigen Exponenten.",
            details={
                "adjustedExponent": normalized.adjusted(),
                "maximumAbsoluteExponent": (
                    _DECIMAL_MAX_ABSOLUTE_EXPONENT
                ),
            },
        )
    return normalized


def _require_positive_decimal(
    value: Any,
    *,
    field_name: str,
) -> Decimal:
    normalized = _require_decimal(
        value,
        field_name=field_name,
    )
    if normalized <= 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss größer als 0 sein.",
            details={
                "value": decimal_to_canonical_string(normalized),
            },
        )
    return normalized


def _require_positive_even_int64(
    value: Any,
    *,
    field_name: str,
) -> int:
    normalized = _require_int64(
        value,
        field_name=field_name,
    )
    if normalized <= 0 or normalized % 2 != 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine positive gerade ganze Zahl sein.",
            details={"value": normalized},
        )
    return normalized


def _require_positive_int(
    value: Any,
    *,
    field_name: str,
) -> int:
    normalized = _require_int64(
        value,
        field_name=field_name,
    )
    if normalized <= 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss größer als 0 sein.",
            details={"value": normalized},
        )
    return normalized


def _require_int64(
    value: Any,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine ganze Zahl sein.",
            details={"actualType": type(value).__name__},
        )
    if value < SIGNED_INT64_MIN or value > SIGNED_INT64_MAX:
        raise GeoreferencingValidationError(
            f"'{field_name}' überschreitet int64.",
            details={
                "value": value,
                "minimum": SIGNED_INT64_MIN,
                "maximum": SIGNED_INT64_MAX,
            },
        )
    return value


def _require_text(
    value: Any,
    *,
    field_name: str,
    maximum_length: int,
) -> str:
    if not isinstance(value, str):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Zeichenfolge sein.",
            details={"actualType": type(value).__name__},
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


def _require_hash(
    value: Any,
    *,
    field_name: str,
) -> str:
    normalized = _require_text(
        value,
        field_name=field_name,
        maximum_length=64,
    ).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef"
        for character in normalized
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss ein SHA-256-Hash sein."
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


def _safe_error(error: BaseException) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "type": type(error).__name__,
        "message": str(error).strip() or "Earth-Grid-Operation fehlgeschlagen.",
    }
    code = getattr(error, "code", None)
    if code is not None:
        payload["code"] = str(code)
    return payload


__all__ = [
    "DEFAULT_EARTH_CHUNK_SIZE",
    "DEFAULT_EARTH_GRID_ID",
    "DEFAULT_EARTH_GRID_VERSION",
    "DEFAULT_EARTH_METERS_PER_CELL",
    "DEFAULT_EARTH_WORLD_HEIGHT_CELLS",
    "DEFAULT_EARTH_WORLD_WIDTH_CELLS",
    "EARTH_GRID_MAPPING_ID",
    "EARTH_GRID_MAPPING_VERSION",
    "EARTH_GRID_RESOLVER_VERSION",
    "EARTH_GRID_STORAGE_ORIGIN_POLICY",
    "EARTH_GRID_TOPOLOGY_TYPE",
    "EarthGridDefinition",
    "EarthGridFrame",
    "EarthGridMappingResult",
    "EarthStorageOrigin",
    "GlobalToLocalResult",
    "LocalEarthPosition",
    "LocalToGlobalResult",
    "clear_earth_grid_caches",
    "earth_grid_cache_info",
    "earth_grid_runtime_status",
    "get_default_earth_grid_definition",
    "get_earth_grid_definition",
    "global_to_local",
    "local_to_global",
    "map_global_coordinate_to_grid",
    "reference_as_local_position",
    "resolve_earth_grid_frame",
]
