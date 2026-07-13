# services/vectoplan-chunk/src/coordinates/topology.py
"""Topologiestrategien für unbegrenzte Flat- und periodische Earth-Welten.

Das Modul trennt providerabhängige Weltregeln von der gemeinsamen
Chunkmathematik. ``UnboundedFlatTopology`` verändert Koordinaten nicht.
``PeriodicXTopology`` kanonisiert die X-Achse in einen symmetrischen,
halb-offenen Bereich und bildet die Ost-/West-Weltnaht exakt ab.

Architekturregeln
-----------------
* Normalisierung erfolgt vor Chunk-Key-Erzeugung und vor jedem DB-Zugriff.
* Earth v1 wrappt ausschließlich X.
* Z wird entweder explizit begrenzt oder explizit unbegrenzt behandelt.
* Weltbreite und halbe Weltbreite müssen chunkkompatibel sein.
* Der antipodale Punkt wird kanonisch als ``-half_world`` gespeichert.
* Dirty-Chunk-Ergebnisse sind kanonisch, dedupliziert und stabil sortiert.
* Caches enthalten ausschließlich unveränderliche Strategiekonfigurationen.
* Positionsabhängige Ergebnisse werden bewusst nicht global gecacht.
* Das Modul kennt weder Flask, SQLAlchemy, CRS noch Datenbank-Sessions.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import Any, Final, Iterable, Sequence, Self

from .chunk_math import (
    DEFAULT_CHUNK_SIZE,
    apply_chunk_offset,
    boundary_offsets_for_cell,
    resolve_block_address,
    validate_chunk_size,
)
from .errors import (
    CoordinateError,
    CoordinateOverflowError,
    CoordinateValidationError,
    HalfWorldNotChunkAlignedError,
    InvalidTopologyConfigurationError,
    NorthSouthBoundaryExceededError,
    UnsupportedWrapAxisError,
    WorldHeightInvalidError,
    WorldWidthInvalidError,
    WorldWidthNotChunkAlignedError,
)
from .models import (
    ChunkAddress,
    ChunkPosition,
    CoordinateAxis,
    JsonValue,
    LocalBlockPosition,
    NormalizationMetadata,
    NormalizedBlockPosition,
    NormalizedChunkAddress,
    ResolvedCellAddress,
    SIGNED_INT64_MAX,
    SIGNED_INT64_MIN,
)


_TOPOLOGY_CACHE_SIZE: Final[int] = 128


class TopologyKind(StrEnum):
    """Stabile Identität einer Welt-Topologiestrategie."""

    UNBOUNDED_FLAT = "unbounded-flat-v1"
    PERIODIC_X = "periodic-x-v1"


class NorthSouthPolicy(StrEnum):
    """Verhalten der nicht periodischen Z-Achse."""

    UNBOUNDED = "unbounded"
    BOUNDED = "bounded"


@dataclass(frozen=True, slots=True)
class CanonicalChunkBatch:
    """Kanonisierte und deduplizierte Batch-Chunkadressen.

    ``items`` bewahrt die Reihenfolge der ursprünglichen Anfragen.
    ``unique_canonical`` enthält jeden physischen Chunk genau einmal in der
    Reihenfolge seines ersten Auftretens.
    """

    items: tuple[NormalizedChunkAddress, ...]
    unique_canonical: tuple[ChunkAddress, ...]
    deduplicated_count: int = field(init=False)

    def __post_init__(self) -> None:
        normalized_items = tuple(self.items)
        normalized_unique = tuple(self.unique_canonical)

        for item in normalized_items:
            if not isinstance(item, NormalizedChunkAddress):
                raise CoordinateValidationError(
                    "Batch-Element muss eine NormalizedChunkAddress sein.",
                    details={"actualType": type(item).__name__},
                )

        for address in normalized_unique:
            if not isinstance(address, ChunkAddress):
                raise CoordinateValidationError(
                    "Kanonische Batchadresse muss eine ChunkAddress sein.",
                    details={"actualType": type(address).__name__},
                )

        expected_unique: list[ChunkAddress] = []
        seen: set[ChunkAddress] = set()
        for item in normalized_items:
            if item.canonical in seen:
                continue
            seen.add(item.canonical)
            expected_unique.append(item.canonical)

        if tuple(expected_unique) != normalized_unique:
            raise CoordinateValidationError(
                "unique_canonical entspricht nicht den kanonischen Batch-Elementen.",
                details={
                    "expected": [item.to_dict() for item in expected_unique],
                    "provided": [item.to_dict() for item in normalized_unique],
                },
            )

        deduplicated_count = len(normalized_items) - len(normalized_unique)
        if deduplicated_count < 0:
            raise CoordinateValidationError(
                "Die Batch-Deduplizierungszählung ist ungültig."
            )

        object.__setattr__(self, "items", normalized_items)
        object.__setattr__(self, "unique_canonical", normalized_unique)
        object.__setattr__(self, "deduplicated_count", deduplicated_count)

    @property
    def requested_count(self) -> int:
        return len(self.items)

    @property
    def unique_count(self) -> int:
        return len(self.unique_canonical)

    @property
    def changed_count(self) -> int:
        return sum(1 for item in self.items if item.changed)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "requestedCount": self.requested_count,
            "uniqueCount": self.unique_count,
            "deduplicatedCount": self.deduplicated_count,
            "changedCount": self.changed_count,
            "items": [item.to_dict() for item in self.items],
            "uniqueCanonical": [
                address.to_dict() for address in self.unique_canonical
            ],
        }


class WorldTopology(ABC):
    """Frameworkunabhängiger Vertrag einer Welt-Topologie."""

    @property
    @abstractmethod
    def kind(self) -> TopologyKind:
        raise NotImplementedError

    @property
    @abstractmethod
    def chunk_size(self) -> int:
        raise NotImplementedError

    @property
    @abstractmethod
    def wrap_axes(self) -> tuple[CoordinateAxis, ...]:
        raise NotImplementedError

    @abstractmethod
    def normalize_block_position(
        self,
        position: LocalBlockPosition,
    ) -> NormalizedBlockPosition:
        raise NotImplementedError

    @abstractmethod
    def normalize_chunk_address(
        self,
        address: ChunkAddress | ChunkPosition,
    ) -> NormalizedChunkAddress:
        raise NotImplementedError

    @abstractmethod
    def validate_block_position(
        self,
        position: LocalBlockPosition,
    ) -> LocalBlockPosition:
        raise NotImplementedError

    @abstractmethod
    def validate_chunk_position(
        self,
        position: ChunkPosition,
    ) -> ChunkPosition:
        raise NotImplementedError

    @abstractmethod
    def to_dict(self) -> dict[str, JsonValue]:
        raise NotImplementedError

    def supports_wrap_axis(self, axis: CoordinateAxis | str) -> bool:
        normalized_axis = _require_axis(axis)
        return normalized_axis in self.wrap_axes

    def require_wrap_axis(self, axis: CoordinateAxis | str) -> CoordinateAxis:
        normalized_axis = _require_axis(axis)
        if normalized_axis not in self.wrap_axes:
            raise UnsupportedWrapAxisError(
                normalized_axis.value,
                supported_axes=tuple(item.value for item in self.wrap_axes),
            )
        return normalized_axis

    def resolve_block_address(
        self,
        position: LocalBlockPosition,
    ) -> ResolvedCellAddress:
        """Normalisiert zuerst und löst erst danach Chunk und Zelle auf."""

        normalized = self.normalize_block_position(position)
        return resolve_block_address(
            normalized.canonical,
            chunk_size=self.chunk_size,
        )

    def canonicalize_chunk_batch(
        self,
        addresses: Iterable[ChunkAddress | ChunkPosition],
    ) -> CanonicalChunkBatch:
        """Normalisiert und dedupliziert eine Batch-Anfrage deterministisch."""

        if isinstance(addresses, (str, bytes, bytearray)):
            raise CoordinateValidationError(
                "Chunk-Batch muss ein Iterable aus Chunkadressen sein.",
                details={"actualType": type(addresses).__name__},
            )

        items: list[NormalizedChunkAddress] = []
        unique: list[ChunkAddress] = []
        seen: set[ChunkAddress] = set()

        try:
            iterator = iter(addresses)
        except TypeError as error:
            raise CoordinateValidationError(
                "Chunk-Batch ist nicht iterierbar.",
                details={"actualType": type(addresses).__name__},
                cause=error,
            ) from error

        for address in iterator:
            normalized = self.normalize_chunk_address(address)
            items.append(normalized)

            if normalized.canonical in seen:
                continue

            seen.add(normalized.canonical)
            unique.append(normalized.canonical)

        return CanonicalChunkBatch(
            items=tuple(items),
            unique_canonical=tuple(unique),
        )

    def neighbor_chunk(
        self,
        address: ChunkAddress | ChunkPosition,
        *,
        dx: int = 0,
        dy: int = 0,
        dz: int = 0,
    ) -> ChunkAddress:
        """Berechnet einen providerkorrekten Nachbarchunk."""

        requested = _as_chunk_address(address)
        offset = ChunkPosition(
            _require_int64(dx, field_name="dx"),
            _require_int64(dy, field_name="dy"),
            _require_int64(dz, field_name="dz"),
        )
        candidate = apply_chunk_offset(requested, offset)
        return self.normalize_chunk_address(candidate).canonical

    def dirty_chunks_for_block(
        self,
        position: LocalBlockPosition,
        *,
        include_diagonal_combinations: bool = True,
        include_current_chunk: bool = True,
    ) -> tuple[ChunkAddress, ...]:
        """Berechnet kanonische Dirty-Chunks einer möglicherweise randnahen Zelle.

        Relative Grenzoffsets stammen aus der topologieneutralen Chunkmathematik.
        Erst anschließend wird jeder Kandidat durch diese Topologiestrategie
        normalisiert. Nicht vorhandene Z-Nachbarn an einer begrenzten Weltkante
        werden übersprungen.
        """

        resolved = self.resolve_block_address(position)
        offsets = boundary_offsets_for_cell(
            resolved.cell,
            chunk_size=self.chunk_size,
            include_diagonal_combinations=include_diagonal_combinations,
            include_current_chunk=include_current_chunk,
        )

        dirty: set[ChunkAddress] = set()

        for offset in offsets:
            candidate_position = apply_chunk_offset(
                resolved.chunk,
                offset,
            )

            try:
                candidate = self.normalize_chunk_address(
                    candidate_position
                ).canonical
            except NorthSouthBoundaryExceededError:
                # An einer echten Z-Weltgrenze existiert kein Nachbarchunk.
                continue

            dirty.add(candidate)

        return tuple(sorted(dirty))

    def same_physical_block(
        self,
        first: LocalBlockPosition,
        second: LocalBlockPosition,
    ) -> bool:
        return (
            self.normalize_block_position(first).canonical
            == self.normalize_block_position(second).canonical
        )

    def same_physical_chunk(
        self,
        first: ChunkAddress | ChunkPosition,
        second: ChunkAddress | ChunkPosition,
    ) -> bool:
        return (
            self.normalize_chunk_address(first).canonical
            == self.normalize_chunk_address(second).canonical
        )


@dataclass(frozen=True, slots=True)
class UnboundedFlatTopology(WorldTopology):
    """Unbegrenzte, nicht periodische Topologie des bestehenden Flat-Providers."""

    _chunk_size: int = DEFAULT_CHUNK_SIZE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_chunk_size",
            validate_chunk_size(self._chunk_size),
        )

    @property
    def kind(self) -> TopologyKind:
        return TopologyKind.UNBOUNDED_FLAT

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @property
    def wrap_axes(self) -> tuple[CoordinateAxis, ...]:
        return ()

    def normalize_block_position(
        self,
        position: LocalBlockPosition,
    ) -> NormalizedBlockPosition:
        validated = self.validate_block_position(position)
        return NormalizedBlockPosition.unchanged(validated)

    def normalize_chunk_address(
        self,
        address: ChunkAddress | ChunkPosition,
    ) -> NormalizedChunkAddress:
        requested = _as_chunk_address(address)
        self.validate_chunk_position(requested.position)
        return NormalizedChunkAddress.unchanged(requested)

    def validate_block_position(
        self,
        position: LocalBlockPosition,
    ) -> LocalBlockPosition:
        return _require_block_position(position)

    def validate_chunk_position(
        self,
        position: ChunkPosition,
    ) -> ChunkPosition:
        return _require_chunk_position(position)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "topologyType": self.kind.value,
            "chunkSize": self.chunk_size,
            "wrapAxes": [],
            "northSouthPolicy": NorthSouthPolicy.UNBOUNDED.value,
            "bounded": False,
        }


@dataclass(frozen=True, slots=True)
class PeriodicXTopology(WorldTopology):
    """Flache Earth-v1-Topologie mit periodischer X-Achse.

    Der kanonische Blockbereich lautet:

    ``-half_world_blocks <= x < half_world_blocks``

    Der kanonische Chunkbereich lautet:

    ``-half_world_chunks <= chunk_x < half_world_chunks``

    Der exakt gegenüberliegende Punkt wird als negativer Halbweltwert
    repräsentiert.
    """

    world_width_blocks: int
    _chunk_size: int = DEFAULT_CHUNK_SIZE
    north_south_policy: NorthSouthPolicy = NorthSouthPolicy.BOUNDED
    minimum_z: int | None = None
    maximum_z: int | None = None

    def __post_init__(self) -> None:
        normalized_chunk_size = validate_chunk_size(self._chunk_size)
        normalized_width = _validate_world_width(
            self.world_width_blocks,
            chunk_size=normalized_chunk_size,
        )
        normalized_policy = _require_north_south_policy(
            self.north_south_policy
        )

        minimum_z, maximum_z = _validate_north_south_bounds(
            policy=normalized_policy,
            minimum_z=self.minimum_z,
            maximum_z=self.maximum_z,
            chunk_size=normalized_chunk_size,
        )

        object.__setattr__(self, "world_width_blocks", normalized_width)
        object.__setattr__(self, "_chunk_size", normalized_chunk_size)
        object.__setattr__(self, "north_south_policy", normalized_policy)
        object.__setattr__(self, "minimum_z", minimum_z)
        object.__setattr__(self, "maximum_z", maximum_z)

    @property
    def kind(self) -> TopologyKind:
        return TopologyKind.PERIODIC_X

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @property
    def wrap_axes(self) -> tuple[CoordinateAxis, ...]:
        return (CoordinateAxis.X,)

    @property
    def half_world_blocks(self) -> int:
        return self.world_width_blocks // 2

    @property
    def world_width_chunks(self) -> int:
        return self.world_width_blocks // self.chunk_size

    @property
    def half_world_chunks(self) -> int:
        return self.world_width_chunks // 2

    @property
    def minimum_chunk_z(self) -> int | None:
        if self.north_south_policy is NorthSouthPolicy.UNBOUNDED:
            return None

        assert self.minimum_z is not None
        return self.minimum_z // self.chunk_size

    @property
    def maximum_chunk_z(self) -> int | None:
        if self.north_south_policy is NorthSouthPolicy.UNBOUNDED:
            return None

        assert self.maximum_z is not None
        return ((self.maximum_z + 1) // self.chunk_size) - 1

    def normalize_block_x(self, x: int) -> tuple[int, int, bool]:
        """Kanonisiert X und liefert ``(canonical, wrap_count, antipodal)``."""

        normalized_x = _require_int64(x, field_name="x")
        canonical, wrap_count = _canonicalize_centered(
            normalized_x,
            width=self.world_width_blocks,
            half_width=self.half_world_blocks,
        )
        antipodal = (
            canonical == -self.half_world_blocks
            and normalized_x != canonical
        )
        return canonical, wrap_count, antipodal

    def normalize_chunk_x(self, chunk_x: int) -> tuple[int, int, bool]:
        normalized_x = _require_int64(chunk_x, field_name="chunkX")
        canonical, wrap_count = _canonicalize_centered(
            normalized_x,
            width=self.world_width_chunks,
            half_width=self.half_world_chunks,
        )
        antipodal = (
            canonical == -self.half_world_chunks
            and normalized_x != canonical
        )
        return canonical, wrap_count, antipodal

    def normalize_block_position(
        self,
        position: LocalBlockPosition,
    ) -> NormalizedBlockPosition:
        requested = self.validate_block_position(position)
        canonical_x, wrap_count, antipodal = self.normalize_block_x(
            requested.x
        )

        canonical = LocalBlockPosition(
            canonical_x,
            requested.y,
            requested.z,
        )

        if canonical == requested:
            return NormalizedBlockPosition.unchanged(requested)

        return NormalizedBlockPosition(
            requested=requested,
            canonical=canonical,
            metadata=NormalizationMetadata.periodic_x(
                wrap_count_x=wrap_count,
                antipodal_canonicalized=antipodal,
            ),
        )

    def normalize_chunk_address(
        self,
        address: ChunkAddress | ChunkPosition,
    ) -> NormalizedChunkAddress:
        requested = _as_chunk_address(address)
        self.validate_chunk_position(requested.position)

        canonical_x, wrap_count, antipodal = self.normalize_chunk_x(
            requested.x
        )
        canonical = ChunkAddress.from_coordinates(
            canonical_x,
            requested.y,
            requested.z,
        )

        if canonical == requested:
            return NormalizedChunkAddress.unchanged(requested)

        return NormalizedChunkAddress(
            requested=requested,
            canonical=canonical,
            metadata=NormalizationMetadata.periodic_x(
                wrap_count_x=wrap_count,
                antipodal_canonicalized=antipodal,
            ),
        )

    def validate_block_position(
        self,
        position: LocalBlockPosition,
    ) -> LocalBlockPosition:
        normalized = _require_block_position(position)
        self._validate_z(normalized.z, is_chunk=False)
        return normalized

    def validate_chunk_position(
        self,
        position: ChunkPosition,
    ) -> ChunkPosition:
        normalized = _require_chunk_position(position)
        self._validate_z(normalized.z, is_chunk=True)
        return normalized

    def shortest_block_delta_x(
        self,
        *,
        from_x: int,
        to_x: int,
    ) -> int:
        """Liefert den kürzesten signierten Blockabstand in X.

        Beim exakt antipodalen Abstand gilt die ADR-Regel:
        ``+half_world`` wird als ``-half_world`` repräsentiert.
        """

        normalized_from = _require_int64(from_x, field_name="fromX")
        normalized_to = _require_int64(to_x, field_name="toX")
        raw_delta = normalized_to - normalized_from

        canonical, _ = _canonicalize_centered(
            raw_delta,
            width=self.world_width_blocks,
            half_width=self.half_world_blocks,
            require_input_int64=False,
        )
        return canonical

    def shortest_chunk_delta_x(
        self,
        *,
        from_chunk_x: int,
        to_chunk_x: int,
    ) -> int:
        normalized_from = _require_int64(
            from_chunk_x,
            field_name="fromChunkX",
        )
        normalized_to = _require_int64(
            to_chunk_x,
            field_name="toChunkX",
        )
        raw_delta = normalized_to - normalized_from

        canonical, _ = _canonicalize_centered(
            raw_delta,
            width=self.world_width_chunks,
            half_width=self.half_world_chunks,
            require_input_int64=False,
        )
        return canonical

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "topologyType": self.kind.value,
            "chunkSize": self.chunk_size,
            "wrapAxes": [axis.value for axis in self.wrap_axes],
            "worldWidthBlocks": self.world_width_blocks,
            "halfWorldBlocks": self.half_world_blocks,
            "worldWidthChunks": self.world_width_chunks,
            "halfWorldChunks": self.half_world_chunks,
            "canonicalBlockXRange": {
                "minimumInclusive": -self.half_world_blocks,
                "maximumExclusive": self.half_world_blocks,
            },
            "canonicalChunkXRange": {
                "minimumInclusive": -self.half_world_chunks,
                "maximumExclusive": self.half_world_chunks,
            },
            "antipodalCanonicalValueBlocks": -self.half_world_blocks,
            "antipodalCanonicalValueChunks": -self.half_world_chunks,
            "northSouthPolicy": self.north_south_policy.value,
            "minimumZ": self.minimum_z,
            "maximumZ": self.maximum_z,
            "minimumChunkZ": self.minimum_chunk_z,
            "maximumChunkZ": self.maximum_chunk_z,
            "bounded": (
                self.north_south_policy is NorthSouthPolicy.BOUNDED
            ),
        }

    def _validate_z(self, value: int, *, is_chunk: bool) -> None:
        if self.north_south_policy is NorthSouthPolicy.UNBOUNDED:
            return

        if is_chunk:
            minimum = self.minimum_chunk_z
            maximum = self.maximum_chunk_z
            field_name = "chunkZ"
        else:
            minimum = self.minimum_z
            maximum = self.maximum_z
            field_name = "z"

        assert minimum is not None
        assert maximum is not None

        if value < minimum or value > maximum:
            # Spezifischen Domänenfehler direkt auslösen. Zusätzlicher
            # HTTP-/Projektkontext wird erst an der Application-Grenze ergänzt.
            raise NorthSouthBoundaryExceededError(
                z=value,
                minimum_z=minimum,
                maximum_z=maximum,
            )


def get_unbounded_flat_topology(
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> UnboundedFlatTopology:
    """Liefert eine gecachte Flat-Topologiestrategie."""

    normalized_size = validate_chunk_size(chunk_size)
    return _get_unbounded_flat_topology_cached(normalized_size)


@lru_cache(maxsize=_TOPOLOGY_CACHE_SIZE)
def _get_unbounded_flat_topology_cached(
    chunk_size: int,
) -> UnboundedFlatTopology:
    return UnboundedFlatTopology(_chunk_size=chunk_size)


def get_periodic_x_topology(
    *,
    world_width_blocks: int,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    north_south_policy: NorthSouthPolicy | str = NorthSouthPolicy.BOUNDED,
    minimum_z: int | None = None,
    maximum_z: int | None = None,
) -> PeriodicXTopology:
    """Liefert eine gecachte Earth-v1-Topologiestrategie."""

    normalized_size = validate_chunk_size(chunk_size)
    normalized_policy = _require_north_south_policy(north_south_policy)

    return _get_periodic_x_topology_cached(
        world_width_blocks,
        normalized_size,
        normalized_policy,
        minimum_z,
        maximum_z,
    )


@lru_cache(maxsize=_TOPOLOGY_CACHE_SIZE)
def _get_periodic_x_topology_cached(
    world_width_blocks: int,
    chunk_size: int,
    north_south_policy: NorthSouthPolicy,
    minimum_z: int | None,
    maximum_z: int | None,
) -> PeriodicXTopology:
    return PeriodicXTopology(
        world_width_blocks=world_width_blocks,
        _chunk_size=chunk_size,
        north_south_policy=north_south_policy,
        minimum_z=minimum_z,
        maximum_z=maximum_z,
    )


def clear_topology_caches() -> None:
    """Leert ausschließlich ableitbare In-Process-Strategiecaches."""

    _get_unbounded_flat_topology_cached.cache_clear()
    _get_periodic_x_topology_cached.cache_clear()


def topology_cache_info() -> dict[str, JsonValue]:
    """Liefert serialisierbare Cache-Diagnostik."""

    return {
        "unboundedFlat": _cache_info_to_dict(
            _get_unbounded_flat_topology_cached.cache_info()
        ),
        "periodicX": _cache_info_to_dict(
            _get_periodic_x_topology_cached.cache_info()
        ),
    }


def _validate_world_width(
    value: Any,
    *,
    chunk_size: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorldWidthInvalidError(value)

    if value <= 0 or value % 2 != 0:
        raise WorldWidthInvalidError(value)

    if value > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis="worldWidthBlocks",
            value=value,
            minimum=2,
            maximum=SIGNED_INT64_MAX,
        )

    if value % chunk_size != 0:
        raise WorldWidthNotChunkAlignedError(
            world_width=value,
            chunk_size=chunk_size,
        )

    half_world = value // 2
    if half_world % chunk_size != 0:
        raise HalfWorldNotChunkAlignedError(
            world_width=value,
            chunk_size=chunk_size,
        )

    world_width_chunks = value // chunk_size
    if world_width_chunks <= 0 or world_width_chunks % 2 != 0:
        raise InvalidTopologyConfigurationError(
            "Die Earth-Welt benötigt eine positive, gerade Chunkbreite.",
            details={
                "worldWidthBlocks": value,
                "chunkSize": chunk_size,
                "worldWidthChunks": world_width_chunks,
            },
        )

    return value


def _validate_north_south_bounds(
    *,
    policy: NorthSouthPolicy,
    minimum_z: Any,
    maximum_z: Any,
    chunk_size: int,
) -> tuple[int | None, int | None]:
    if policy is NorthSouthPolicy.UNBOUNDED:
        if minimum_z is not None or maximum_z is not None:
            raise InvalidTopologyConfigurationError(
                "Unbegrenzte Z-Topologie darf keine minimumZ/maximumZ-Werte besitzen.",
                details={
                    "northSouthPolicy": policy.value,
                    "minimumZ": minimum_z,
                    "maximumZ": maximum_z,
                },
            )
        return None, None

    if minimum_z is None or maximum_z is None:
        raise WorldHeightInvalidError(
            minimum_z=minimum_z,
            maximum_z=maximum_z,
        )

    normalized_minimum = _require_int64(
        minimum_z,
        field_name="minimumZ",
    )
    normalized_maximum = _require_int64(
        maximum_z,
        field_name="maximumZ",
    )

    if normalized_minimum > normalized_maximum:
        raise WorldHeightInvalidError(
            minimum_z=normalized_minimum,
            maximum_z=normalized_maximum,
        )

    if normalized_maximum == SIGNED_INT64_MAX:
        raise InvalidTopologyConfigurationError(
            "maximumZ muss für die inklusive Chunkgrenze kleiner als int64_max sein.",
            details={
                "maximumZ": normalized_maximum,
                "maximumAllowed": SIGNED_INT64_MAX - 1,
            },
        )

    if normalized_minimum % chunk_size != 0:
        raise InvalidTopologyConfigurationError(
            "minimumZ muss auf einer Chunkgrenze liegen.",
            details={
                "minimumZ": normalized_minimum,
                "chunkSize": chunk_size,
                "remainder": normalized_minimum % chunk_size,
            },
        )

    maximum_exclusive = normalized_maximum + 1
    if maximum_exclusive % chunk_size != 0:
        raise InvalidTopologyConfigurationError(
            "maximumZ muss das inklusive Ende eines vollständigen Chunks bilden.",
            details={
                "maximumZ": normalized_maximum,
                "maximumExclusive": maximum_exclusive,
                "chunkSize": chunk_size,
                "remainder": maximum_exclusive % chunk_size,
            },
        )

    return normalized_minimum, normalized_maximum


def _canonicalize_centered(
    value: int,
    *,
    width: int,
    half_width: int,
    require_input_int64: bool = True,
) -> tuple[int, int]:
    """Kanonisiert in ``[-half_width, half_width)``.

    ``wrap_count`` erfüllt exakt:

    ``value = canonical + wrap_count * width``
    """

    if require_input_int64:
        normalized_value = _require_int64(value, field_name="value")
    else:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CoordinateValidationError(
                "value muss eine ganze Zahl sein.",
                details={"actualType": type(value).__name__},
            )
        normalized_value = value

    if width <= 0 or half_width <= 0 or width != half_width * 2:
        raise InvalidTopologyConfigurationError(
            "Ungültige zentrierte Periodizitätsparameter.",
            details={
                "width": width,
                "halfWidth": half_width,
            },
        )

    canonical = ((normalized_value + half_width) % width) - half_width
    wrap_count = (normalized_value - canonical) // width

    if canonical < -half_width or canonical >= half_width:
        raise InvalidTopologyConfigurationError(
            "Kanonisierung hat den Zielbereich verletzt.",
            details={
                "value": normalized_value,
                "canonical": canonical,
                "minimumInclusive": -half_width,
                "maximumExclusive": half_width,
            },
        )

    if normalized_value != canonical + (wrap_count * width):
        raise InvalidTopologyConfigurationError(
            "Kanonisierung ist algebraisch inkonsistent.",
            details={
                "value": normalized_value,
                "canonical": canonical,
                "wrapCount": wrap_count,
                "width": width,
            },
        )

    return canonical, wrap_count


def _as_chunk_address(
    address: ChunkAddress | ChunkPosition | Any,
) -> ChunkAddress:
    if isinstance(address, ChunkAddress):
        return address

    if isinstance(address, ChunkPosition):
        return ChunkAddress.from_position(address)

    raise CoordinateValidationError(
        "address muss eine ChunkAddress oder ChunkPosition sein.",
        details={"actualType": type(address).__name__},
    )


def _require_block_position(position: Any) -> LocalBlockPosition:
    if not isinstance(position, LocalBlockPosition):
        raise CoordinateValidationError(
            "position muss eine LocalBlockPosition sein.",
            details={"actualType": type(position).__name__},
        )
    return position


def _require_chunk_position(position: Any) -> ChunkPosition:
    if not isinstance(position, ChunkPosition):
        raise CoordinateValidationError(
            "position muss eine ChunkPosition sein.",
            details={"actualType": type(position).__name__},
        )
    return position


def _require_axis(axis: CoordinateAxis | str) -> CoordinateAxis:
    if isinstance(axis, CoordinateAxis):
        return axis

    if not isinstance(axis, str):
        raise CoordinateValidationError(
            "axis muss eine Koordinatenachse sein.",
            details={
                "actualType": type(axis).__name__,
                "allowedValues": [item.value for item in CoordinateAxis],
            },
        )

    try:
        return CoordinateAxis(axis.strip().lower())
    except ValueError as error:
        raise CoordinateValidationError(
            "axis wird nicht unterstützt.",
            details={
                "axis": axis,
                "allowedValues": [item.value for item in CoordinateAxis],
            },
            cause=error,
        ) from error


def _require_north_south_policy(
    policy: NorthSouthPolicy | str,
) -> NorthSouthPolicy:
    if isinstance(policy, NorthSouthPolicy):
        return policy

    if not isinstance(policy, str):
        raise InvalidTopologyConfigurationError(
            "northSouthPolicy besitzt einen ungültigen Typ.",
            details={
                "actualType": type(policy).__name__,
                "allowedValues": [item.value for item in NorthSouthPolicy],
            },
        )

    try:
        return NorthSouthPolicy(policy.strip().lower())
    except ValueError as error:
        raise InvalidTopologyConfigurationError(
            "northSouthPolicy wird nicht unterstützt.",
            details={
                "value": policy,
                "allowedValues": [item.value for item in NorthSouthPolicy],
                "causeType": type(error).__name__,
            },
        ) from error


def _require_int64(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinateValidationError(
            f"'{field_name}' muss eine ganze Zahl sein.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
            },
        )

    if value < SIGNED_INT64_MIN or value > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis=field_name,
            value=value,
            minimum=SIGNED_INT64_MIN,
            maximum=SIGNED_INT64_MAX,
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


__all__ = [
    "CanonicalChunkBatch",
    "NorthSouthPolicy",
    "PeriodicXTopology",
    "TopologyKind",
    "UnboundedFlatTopology",
    "WorldTopology",
    "clear_topology_caches",
    "get_periodic_x_topology",
    "get_unbounded_flat_topology",
    "topology_cache_info",
]
