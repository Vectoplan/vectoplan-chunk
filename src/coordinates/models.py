# services/vectoplan-chunk/src/coordinates/models.py
"""Unveränderliche Wertobjekte des gemeinsamen Koordinatenkerns.

Das Modul enthält ausschließlich frameworkunabhängige Domain-Modelle. Es kennt
weder Flask noch SQLAlchemy, Datenbank-Sessions, Provider-Implementierungen oder
CRS-Bibliotheken.

Verantwortlichkeiten
---------------------
* Eindeutige Repräsentation lokaler Block-, Chunk- und Zellpositionen.
* Eindeutige, parsebare Chunk-Keys.
* Repräsentation kanonisierter Positionen und ihrer Normalisierungsmetadaten.
* Sichere Validierung externer Mapping-Payloads.
* JSON-kompatible Serialisierung ohne technische Seiteneffekte.

Nicht verantwortlich
---------------------
* Chunk- oder Floor-Divisionsmathematik.
* Periodische Normalisierung.
* CRS-Transformationen.
* Datenbankpersistenz.
* HTTP-Statuscodes oder Flask-Antworten.
* Logging, Caching oder Transaktionssteuerung.

Alle Koordinatenobjekte sind ``frozen`` und ``slots``-basiert. Nach ihrer
Erzeugung können sie nicht verändert werden.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any, ClassVar, Final, Self, TypeAlias

from .errors import (
    CellAddressInvalidError,
    ChunkAddressInvalidError,
    CoordinateDimensionMismatchError,
    CoordinateOverflowError,
    CoordinateSpaceMismatchError,
    CoordinateValidationError,
)


JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
IntegerTriple: TypeAlias = tuple[int, int, int]
NumberTriple: TypeAlias = tuple[float, float, float]


SIGNED_INT32_MIN: Final[int] = -(2**31)
SIGNED_INT32_MAX: Final[int] = 2**31 - 1
SIGNED_INT64_MIN: Final[int] = -(2**63)
SIGNED_INT64_MAX: Final[int] = 2**63 - 1


class CoordinateSpace(StrEnum):
    """Stabile Identitäten der internen Koordinatenräume."""

    LOCAL_WORLD = "local_world"
    LOCAL_BLOCK = "local_block"
    LOCAL_METRIC = "local_metric"
    CHUNK = "chunk"
    LOCAL_CELL = "local_cell"
    EARTH_GRID = "earth_grid"


class CoordinateAxis(StrEnum):
    """Kanonische Achsenbezeichner der VECTOPLAN-Welt."""

    X = "x"
    Y = "y"
    Z = "z"


class AxisConvention(StrEnum):
    """Unterstützte Achsenkonventionen.

    Earth v1 und die bestehende Voxelwelt verwenden X=Ost, Y=Oben, Z=Nord.
    Weitere Konventionen dürfen nur als neue Enum-Werte ergänzt und nicht still
    unter einer vorhandenen Identität umgedeutet werden.
    """

    X_EAST_Y_UP_Z_NORTH = "x-east-y-up-z-north"


class NormalizationReason(StrEnum):
    """Gründe, warum eine Position kanonisiert wurde."""

    NONE = "none"
    PERIODIC_WRAP = "periodic_wrap"
    ANTIPODAL_CANONICALIZATION = "antipodal_canonicalization"
    MULTIPLE = "multiple"


@dataclass(frozen=True, slots=True)
class LocalBlockPosition:
    """Ganzzahlige Blockposition relativ zum Ursprung einer WorldInstance."""

    x: int
    y: int
    z: int

    coordinate_space: ClassVar[CoordinateSpace] = CoordinateSpace.LOCAL_BLOCK

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _require_signed_int64(self.x, field_name="x"))
        object.__setattr__(self, "y", _require_signed_int64(self.y, field_name="y"))
        object.__setattr__(self, "z", _require_signed_int64(self.z, field_name="z"))

    @classmethod
    def origin(cls) -> Self:
        """Erzeugt den lokalen Ursprung."""

        return cls(0, 0, 0)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        """Liest eine Position strikt aus ``x``, ``y`` und ``z``."""

        mapping = _require_mapping(payload, object_name="LocalBlockPosition")
        return cls(
            x=_require_mapping_int(mapping, "x"),
            y=_require_mapping_int(mapping, "y"),
            z=_require_mapping_int(mapping, "z"),
        )

    @classmethod
    def from_sequence(cls, values: Sequence[Any]) -> Self:
        """Liest eine Position aus einer Sequenz mit exakt drei Ganzzahlen."""

        x, y, z = _require_int_triple(values, object_name="LocalBlockPosition")
        return cls(x=x, y=y, z=z)

    def as_tuple(self) -> IntegerTriple:
        return self.x, self.y, self.z

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "coordinateSpace": self.coordinate_space.value,
            "x": self.x,
            "y": self.y,
            "z": self.z,
        }

    def translated(self, *, dx: int = 0, dy: int = 0, dz: int = 0) -> Self:
        """Erzeugt eine verschobene Position ohne dieses Objekt zu verändern."""

        return self.__class__(
            x=_checked_add_int64(self.x, dx, axis=CoordinateAxis.X),
            y=_checked_add_int64(self.y, dy, axis=CoordinateAxis.Y),
            z=_checked_add_int64(self.z, dz, axis=CoordinateAxis.Z),
        )

    def difference_to(self, other: "LocalBlockPosition") -> IntegerTriple:
        """Liefert ``other - self`` mit geprüftem 64-Bit-Wertebereich."""

        _ensure_same_coordinate_space(self, other)

        return (
            _checked_subtract_int64(other.x, self.x, axis=CoordinateAxis.X),
            _checked_subtract_int64(other.y, self.y, axis=CoordinateAxis.Y),
            _checked_subtract_int64(other.z, self.z, axis=CoordinateAxis.Z),
        )


@dataclass(frozen=True, slots=True)
class LocalMetricPosition:
    """Sub-Block-Position im lokalen Weltkoordinatensystem.

    Die Werte sind für Spieler, Kamera oder bewegliche Objekte gedacht.
    Statische Voxelzellen verwenden weiterhin ``LocalBlockPosition``.
    """

    x: float
    y: float
    z: float

    coordinate_space: ClassVar[CoordinateSpace] = CoordinateSpace.LOCAL_METRIC

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _require_finite_float(self.x, field_name="x"))
        object.__setattr__(self, "y", _require_finite_float(self.y, field_name="y"))
        object.__setattr__(self, "z", _require_finite_float(self.z, field_name="z"))

    @classmethod
    def origin(cls) -> Self:
        return cls(0.0, 0.0, 0.0)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        mapping = _require_mapping(payload, object_name="LocalMetricPosition")
        return cls(
            x=_require_mapping_float(mapping, "x"),
            y=_require_mapping_float(mapping, "y"),
            z=_require_mapping_float(mapping, "z"),
        )

    @classmethod
    def from_sequence(cls, values: Sequence[Any]) -> Self:
        x, y, z = _require_float_triple(
            values,
            object_name="LocalMetricPosition",
        )
        return cls(x=x, y=y, z=z)

    def as_tuple(self) -> NumberTriple:
        return self.x, self.y, self.z

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "coordinateSpace": self.coordinate_space.value,
            "x": self.x,
            "y": self.y,
            "z": self.z,
        }


@dataclass(frozen=True, slots=True, order=True)
class ChunkPosition:
    """Ganzzahlige Position eines Chunks im lokalen Weltkoordinatensystem."""

    x: int
    y: int
    z: int

    coordinate_space: ClassVar[CoordinateSpace] = CoordinateSpace.CHUNK

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _require_signed_int64(self.x, field_name="x"))
        object.__setattr__(self, "y", _require_signed_int64(self.y, field_name="y"))
        object.__setattr__(self, "z", _require_signed_int64(self.z, field_name="z"))

    @classmethod
    def origin(cls) -> Self:
        return cls(0, 0, 0)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        """Akzeptiert kanonisch ``x/y/z`` sowie API-Felder ``chunkX/Y/Z``."""

        mapping = _require_mapping(payload, object_name="ChunkPosition")

        return cls(
            x=_require_one_of_mapping_int(mapping, "x", "chunkX"),
            y=_require_one_of_mapping_int(mapping, "y", "chunkY"),
            z=_require_one_of_mapping_int(mapping, "z", "chunkZ"),
        )

    @classmethod
    def from_sequence(cls, values: Sequence[Any]) -> Self:
        x, y, z = _require_int_triple(values, object_name="ChunkPosition")
        return cls(x=x, y=y, z=z)

    def as_tuple(self) -> IntegerTriple:
        return self.x, self.y, self.z

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "coordinateSpace": self.coordinate_space.value,
            "chunkX": self.x,
            "chunkY": self.y,
            "chunkZ": self.z,
        }

    def translated(self, *, dx: int = 0, dy: int = 0, dz: int = 0) -> Self:
        return self.__class__(
            x=_checked_add_int64(self.x, dx, axis=CoordinateAxis.X),
            y=_checked_add_int64(self.y, dy, axis=CoordinateAxis.Y),
            z=_checked_add_int64(self.z, dz, axis=CoordinateAxis.Z),
        )


@dataclass(frozen=True, slots=True, order=True)
class LocalCellPosition:
    """Lokale Zellposition innerhalb eines Chunks.

    Die konkrete Gültigkeit hängt von der verwendeten Chunkgröße ab und wird
    deshalb mit ``validate_for_chunk_size`` geprüft.
    """

    x: int
    y: int
    z: int

    coordinate_space: ClassVar[CoordinateSpace] = CoordinateSpace.LOCAL_CELL

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _require_signed_int32(self.x, field_name="x"))
        object.__setattr__(self, "y", _require_signed_int32(self.y, field_name="y"))
        object.__setattr__(self, "z", _require_signed_int32(self.z, field_name="z"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        """Akzeptiert ``x/y/z`` sowie ``localX/Y/Z``."""

        mapping = _require_mapping(payload, object_name="LocalCellPosition")
        return cls(
            x=_require_one_of_mapping_int(mapping, "x", "localX"),
            y=_require_one_of_mapping_int(mapping, "y", "localY"),
            z=_require_one_of_mapping_int(mapping, "z", "localZ"),
        )

    @classmethod
    def from_sequence(cls, values: Sequence[Any]) -> Self:
        x, y, z = _require_int_triple(values, object_name="LocalCellPosition")
        return cls(x=x, y=y, z=z)

    def as_tuple(self) -> IntegerTriple:
        return self.x, self.y, self.z

    def validate_for_chunk_size(self, chunk_size: int) -> Self:
        """Validiert alle Achsen gegen ``0 <= axis < chunk_size``."""

        size = _require_positive_int(chunk_size, field_name="chunkSize")

        if not (
            0 <= self.x < size
            and 0 <= self.y < size
            and 0 <= self.z < size
        ):
            raise CellAddressInvalidError(
                local_x=self.x,
                local_y=self.y,
                local_z=self.z,
                chunk_size=size,
            )

        return self

    def to_linear_index(self, chunk_size: int) -> int:
        """Berechnet den x-fastest-y-then-z-Zellindex.

        Formel:
        ``x + y * chunk_size + z * chunk_size * chunk_size``
        """

        self.validate_for_chunk_size(chunk_size)
        size = int(chunk_size)

        return self.x + (self.y * size) + (self.z * size * size)

    @classmethod
    def from_linear_index(
        cls,
        index: int,
        *,
        chunk_size: int,
    ) -> Self:
        """Erzeugt eine lokale Zellposition aus einem linearen Zellindex."""

        size = _require_positive_int(chunk_size, field_name="chunkSize")
        normalized_index = _require_non_negative_int(index, field_name="index")
        cell_count = size**3

        if normalized_index >= cell_count:
            raise CellAddressInvalidError(
                local_x=None,
                local_y=None,
                local_z=None,
                chunk_size=size,
            ).with_context(
                linearIndex=normalized_index,
                cellCount=cell_count,
            )

        z, remainder = divmod(normalized_index, size * size)
        y, x = divmod(remainder, size)

        return cls(x=x, y=y, z=z)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "coordinateSpace": self.coordinate_space.value,
            "localX": self.x,
            "localY": self.y,
            "localZ": self.z,
        }


@dataclass(frozen=True, slots=True, order=True)
class ChunkAddress:
    """Kanonische Kombination aus Chunkposition und stabilem Chunk-Key."""

    position: ChunkPosition
    key: str = field(compare=True)

    separator: ClassVar[str] = ":"

    def __post_init__(self) -> None:
        if not isinstance(self.position, ChunkPosition):
            raise ChunkAddressInvalidError(
                reason="position muss eine ChunkPosition sein.",
            )

        normalized_key = _require_non_empty_string(self.key, field_name="key")
        expected_key = self.format_key(self.position)

        if normalized_key != expected_key:
            raise ChunkAddressInvalidError(
                chunk_x=self.position.x,
                chunk_y=self.position.y,
                chunk_z=self.position.z,
                reason="Chunk-Key stimmt nicht mit der Chunkposition überein.",
            ).with_context(
                providedChunkKey=normalized_key,
                expectedChunkKey=expected_key,
            )

        object.__setattr__(self, "key", normalized_key)

    @classmethod
    def from_position(cls, position: ChunkPosition) -> Self:
        if not isinstance(position, ChunkPosition):
            raise ChunkAddressInvalidError(
                reason="position muss eine ChunkPosition sein.",
            )

        return cls(
            position=position,
            key=cls.format_key(position),
        )

    @classmethod
    def from_coordinates(
        cls,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
    ) -> Self:
        return cls.from_position(
            ChunkPosition(chunk_x, chunk_y, chunk_z),
        )

    @classmethod
    def parse(cls, key: str) -> Self:
        """Parst ausschließlich das kanonische Format ``x:y:z``."""

        normalized_key = _require_non_empty_string(key, field_name="chunkKey")
        parts = normalized_key.split(cls.separator)

        if len(parts) != 3:
            raise ChunkAddressInvalidError(
                reason="Chunk-Key muss exakt drei durch ':' getrennte Teile besitzen.",
            ).with_context(
                providedChunkKey=normalized_key,
                expectedFormat="x:y:z",
            )

        try:
            coordinates = tuple(
                _parse_strict_integer(part, field_name=f"chunkKey[{index}]")
                for index, part in enumerate(parts)
            )
        except CoordinateValidationError:
            raise
        except (TypeError, ValueError, OverflowError) as error:
            raise ChunkAddressInvalidError(
                reason="Chunk-Key enthält ungültige Ganzzahlen.",
            ).with_context(
                providedChunkKey=normalized_key,
                causeType=type(error).__name__,
            ) from error

        if len(coordinates) != 3:
            raise CoordinateDimensionMismatchError(
                expected_dimensions=3,
                actual_dimensions=len(coordinates),
            )

        return cls.from_coordinates(*coordinates)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        mapping = _require_mapping(payload, object_name="ChunkAddress")

        key = mapping.get("chunkKey")
        if key is not None:
            parsed = cls.parse(_require_non_empty_string(key, field_name="chunkKey"))

            explicit_components = {
                component
                for component in ("chunkX", "chunkY", "chunkZ", "x", "y", "z")
                if component in mapping
            }
            if not explicit_components:
                return parsed

            explicit_position = ChunkPosition.from_mapping(mapping)
            if explicit_position != parsed.position:
                raise ChunkAddressInvalidError(
                    chunk_x=explicit_position.x,
                    chunk_y=explicit_position.y,
                    chunk_z=explicit_position.z,
                    reason="Explizite Komponenten widersprechen dem Chunk-Key.",
                ).with_context(
                    providedChunkKey=parsed.key,
                    parsedPosition=parsed.position.to_dict(),
                )

            return parsed

        return cls.from_position(ChunkPosition.from_mapping(mapping))

    @classmethod
    def format_key(cls, position: ChunkPosition) -> str:
        if not isinstance(position, ChunkPosition):
            raise ChunkAddressInvalidError(
                reason="position muss eine ChunkPosition sein.",
            )

        return (
            f"{position.x}{cls.separator}"
            f"{position.y}{cls.separator}"
            f"{position.z}"
        )

    @property
    def x(self) -> int:
        return self.position.x

    @property
    def y(self) -> int:
        return self.position.y

    @property
    def z(self) -> int:
        return self.position.z

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "chunkKey": self.key,
            "chunkX": self.x,
            "chunkY": self.y,
            "chunkZ": self.z,
        }


@dataclass(frozen=True, slots=True)
class ResolvedCellAddress:
    """Vollständig aufgelöste Adresse einer lokalen Blockzelle."""

    block: LocalBlockPosition
    chunk: ChunkAddress
    cell: LocalCellPosition
    linear_index: int
    chunk_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.block, LocalBlockPosition):
            raise CoordinateValidationError(
                "block muss eine LocalBlockPosition sein.",
                details={"actualType": type(self.block).__name__},
            )
        if not isinstance(self.chunk, ChunkAddress):
            raise CoordinateValidationError(
                "chunk muss eine ChunkAddress sein.",
                details={"actualType": type(self.chunk).__name__},
            )
        if not isinstance(self.cell, LocalCellPosition):
            raise CoordinateValidationError(
                "cell muss eine LocalCellPosition sein.",
                details={"actualType": type(self.cell).__name__},
            )

        normalized_chunk_size = _require_positive_int(
            self.chunk_size,
            field_name="chunkSize",
        )
        self.cell.validate_for_chunk_size(normalized_chunk_size)

        expected_index = self.cell.to_linear_index(normalized_chunk_size)
        normalized_index = _require_non_negative_int(
            self.linear_index,
            field_name="linearIndex",
        )

        if normalized_index != expected_index:
            raise CellAddressInvalidError(
                local_x=self.cell.x,
                local_y=self.cell.y,
                local_z=self.cell.z,
                chunk_size=normalized_chunk_size,
            ).with_context(
                providedLinearIndex=normalized_index,
                expectedLinearIndex=expected_index,
            )

        object.__setattr__(self, "linear_index", normalized_index)
        object.__setattr__(self, "chunk_size", normalized_chunk_size)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "block": self.block.to_dict(),
            "chunk": self.chunk.to_dict(),
            "cell": self.cell.to_dict(),
            "linearIndex": self.linear_index,
            "chunkSize": self.chunk_size,
        }


@dataclass(frozen=True, slots=True)
class NormalizationMetadata:
    """Metadaten einer deterministischen Koordinatennormalisierung."""

    changed: bool
    reason: NormalizationReason = NormalizationReason.NONE
    wrapped_axes: tuple[CoordinateAxis, ...] = ()
    wrap_count_x: int = 0
    antipodal_canonicalized: bool = False

    def __post_init__(self) -> None:
        normalized_reason = _require_enum(
            self.reason,
            NormalizationReason,
            field_name="reason",
        )
        normalized_axes = _normalize_axis_tuple(self.wrapped_axes)
        normalized_wrap_count = _require_signed_int64(
            self.wrap_count_x,
            field_name="wrapCountX",
        )

        changed = bool(self.changed)
        antipodal = bool(self.antipodal_canonicalized)

        if not changed:
            if normalized_reason is not NormalizationReason.NONE:
                raise CoordinateValidationError(
                    "Unveränderte Normalisierung muss reason='none' verwenden.",
                    details={"reason": normalized_reason.value},
                )
            if normalized_axes:
                raise CoordinateValidationError(
                    "Unveränderte Normalisierung darf keine wrappedAxes enthalten.",
                    details={
                        "wrappedAxes": [axis.value for axis in normalized_axes],
                    },
                )
            if normalized_wrap_count != 0:
                raise CoordinateValidationError(
                    "Unveränderte Normalisierung muss wrapCountX=0 verwenden.",
                    details={"wrapCountX": normalized_wrap_count},
                )
            if antipodal:
                raise CoordinateValidationError(
                    "Unveränderte Normalisierung darf nicht antipodal markiert sein."
                )
        else:
            if normalized_reason is NormalizationReason.NONE:
                raise CoordinateValidationError(
                    "Veränderte Normalisierung benötigt einen konkreten Grund."
                )

        object.__setattr__(self, "changed", changed)
        object.__setattr__(self, "reason", normalized_reason)
        object.__setattr__(self, "wrapped_axes", normalized_axes)
        object.__setattr__(self, "wrap_count_x", normalized_wrap_count)
        object.__setattr__(self, "antipodal_canonicalized", antipodal)

    @classmethod
    def unchanged(cls) -> Self:
        return cls(changed=False)

    @classmethod
    def periodic_x(
        cls,
        *,
        wrap_count_x: int,
        antipodal_canonicalized: bool = False,
    ) -> Self:
        reason = (
            NormalizationReason.MULTIPLE
            if antipodal_canonicalized and wrap_count_x != 0
            else (
                NormalizationReason.ANTIPODAL_CANONICALIZATION
                if antipodal_canonicalized
                else NormalizationReason.PERIODIC_WRAP
            )
        )

        return cls(
            changed=True,
            reason=reason,
            wrapped_axes=(CoordinateAxis.X,),
            wrap_count_x=wrap_count_x,
            antipodal_canonicalized=antipodal_canonicalized,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "changed": self.changed,
            "reason": self.reason.value,
            "wrappedAxes": [axis.value for axis in self.wrapped_axes],
            "wrapCountX": self.wrap_count_x,
            "antipodalCanonicalized": self.antipodal_canonicalized,
        }


@dataclass(frozen=True, slots=True)
class NormalizedBlockPosition:
    """Ergebnis der Kanonisierung einer lokalen Blockposition."""

    requested: LocalBlockPosition
    canonical: LocalBlockPosition
    metadata: NormalizationMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.requested, LocalBlockPosition):
            raise CoordinateValidationError(
                "requested muss eine LocalBlockPosition sein.",
                details={"actualType": type(self.requested).__name__},
            )
        if not isinstance(self.canonical, LocalBlockPosition):
            raise CoordinateValidationError(
                "canonical muss eine LocalBlockPosition sein.",
                details={"actualType": type(self.canonical).__name__},
            )
        if not isinstance(self.metadata, NormalizationMetadata):
            raise CoordinateValidationError(
                "metadata muss NormalizationMetadata sein.",
                details={"actualType": type(self.metadata).__name__},
            )

        positions_differ = self.requested != self.canonical
        if positions_differ != self.metadata.changed:
            raise CoordinateValidationError(
                "Positionsänderung und Normalisierungsmetadaten widersprechen sich.",
                details={
                    "requested": self.requested.to_dict(),
                    "canonical": self.canonical.to_dict(),
                    "metadata": self.metadata.to_dict(),
                },
            )

    @classmethod
    def unchanged(cls, position: LocalBlockPosition) -> Self:
        return cls(
            requested=position,
            canonical=position,
            metadata=NormalizationMetadata.unchanged(),
        )

    @property
    def changed(self) -> bool:
        return self.metadata.changed

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "requested": self.requested.to_dict(),
            "canonical": self.canonical.to_dict(),
            "normalization": self.metadata.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class NormalizedChunkAddress:
    """Ergebnis der Kanonisierung einer Chunkadresse."""

    requested: ChunkAddress
    canonical: ChunkAddress
    metadata: NormalizationMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.requested, ChunkAddress):
            raise CoordinateValidationError(
                "requested muss eine ChunkAddress sein.",
                details={"actualType": type(self.requested).__name__},
            )
        if not isinstance(self.canonical, ChunkAddress):
            raise CoordinateValidationError(
                "canonical muss eine ChunkAddress sein.",
                details={"actualType": type(self.canonical).__name__},
            )
        if not isinstance(self.metadata, NormalizationMetadata):
            raise CoordinateValidationError(
                "metadata muss NormalizationMetadata sein.",
                details={"actualType": type(self.metadata).__name__},
            )

        addresses_differ = self.requested != self.canonical
        if addresses_differ != self.metadata.changed:
            raise CoordinateValidationError(
                "Adressänderung und Normalisierungsmetadaten widersprechen sich.",
                details={
                    "requested": self.requested.to_dict(),
                    "canonical": self.canonical.to_dict(),
                    "metadata": self.metadata.to_dict(),
                },
            )

    @classmethod
    def unchanged(cls, address: ChunkAddress) -> Self:
        return cls(
            requested=address,
            canonical=address,
            metadata=NormalizationMetadata.unchanged(),
        )

    @property
    def changed(self) -> bool:
        return self.metadata.changed

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "requested": self.requested.to_dict(),
            "canonical": self.canonical.to_dict(),
            "normalization": self.metadata.to_dict(),
        }


def _ensure_same_coordinate_space(
    left: LocalBlockPosition,
    right: LocalBlockPosition,
) -> None:
    left_space = getattr(left, "coordinate_space", None)
    right_space = getattr(right, "coordinate_space", None)

    if left_space != right_space:
        raise CoordinateSpaceMismatchError(
            expected_space=str(left_space),
            actual_space=str(right_space),
        )


def _require_mapping(
    payload: Mapping[str, Any],
    *,
    object_name: str,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise CoordinateValidationError(
            f"{object_name} muss als Mapping übergeben werden.",
            details={"actualType": type(payload).__name__},
        )

    return payload


def _require_mapping_int(
    mapping: Mapping[str, Any],
    key: str,
) -> int:
    if key not in mapping:
        raise CoordinateValidationError(
            f"Pflichtfeld '{key}' fehlt.",
            details={"missingField": key},
        )

    return _require_signed_int64(mapping[key], field_name=key)


def _require_one_of_mapping_int(
    mapping: Mapping[str, Any],
    primary_key: str,
    alternative_key: str,
) -> int:
    has_primary = primary_key in mapping
    has_alternative = alternative_key in mapping

    if not has_primary and not has_alternative:
        raise CoordinateValidationError(
            f"Pflichtfeld '{primary_key}' oder '{alternative_key}' fehlt.",
            details={
                "acceptedFields": [primary_key, alternative_key],
            },
        )

    if has_primary and has_alternative:
        primary_value = _require_signed_int64(
            mapping[primary_key],
            field_name=primary_key,
        )
        alternative_value = _require_signed_int64(
            mapping[alternative_key],
            field_name=alternative_key,
        )

        if primary_value != alternative_value:
            raise CoordinateValidationError(
                "Alternative Koordinatenfelder widersprechen sich.",
                details={
                    primary_key: primary_value,
                    alternative_key: alternative_value,
                },
            )

        return primary_value

    selected_key = primary_key if has_primary else alternative_key
    return _require_signed_int64(
        mapping[selected_key],
        field_name=selected_key,
    )


def _require_mapping_float(
    mapping: Mapping[str, Any],
    key: str,
) -> float:
    if key not in mapping:
        raise CoordinateValidationError(
            f"Pflichtfeld '{key}' fehlt.",
            details={"missingField": key},
        )

    return _require_finite_float(mapping[key], field_name=key)


def _require_int_triple(
    values: Sequence[Any],
    *,
    object_name: str,
) -> IntegerTriple:
    normalized_values = _require_sequence(
        values,
        object_name=object_name,
        expected_length=3,
    )

    return (
        _require_signed_int64(normalized_values[0], field_name="x"),
        _require_signed_int64(normalized_values[1], field_name="y"),
        _require_signed_int64(normalized_values[2], field_name="z"),
    )


def _require_float_triple(
    values: Sequence[Any],
    *,
    object_name: str,
) -> NumberTriple:
    normalized_values = _require_sequence(
        values,
        object_name=object_name,
        expected_length=3,
    )

    return (
        _require_finite_float(normalized_values[0], field_name="x"),
        _require_finite_float(normalized_values[1], field_name="y"),
        _require_finite_float(normalized_values[2], field_name="z"),
    )


def _require_sequence(
    values: Sequence[Any],
    *,
    object_name: str,
    expected_length: int,
) -> Sequence[Any]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Sequence,
    ):
        raise CoordinateValidationError(
            f"{object_name} muss als Sequenz übergeben werden.",
            details={"actualType": type(values).__name__},
        )

    if len(values) != expected_length:
        raise CoordinateDimensionMismatchError(
            expected_dimensions=expected_length,
            actual_dimensions=len(values),
        )

    return values


def _require_signed_int64(value: Any, *, field_name: str) -> int:
    normalized = _require_strict_int(value, field_name=field_name)

    if normalized < SIGNED_INT64_MIN or normalized > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis=field_name,
            value=normalized,
            minimum=SIGNED_INT64_MIN,
            maximum=SIGNED_INT64_MAX,
        )

    return normalized


def _require_signed_int32(value: Any, *, field_name: str) -> int:
    normalized = _require_strict_int(value, field_name=field_name)

    if normalized < SIGNED_INT32_MIN or normalized > SIGNED_INT32_MAX:
        raise CoordinateOverflowError(
            axis=field_name,
            value=normalized,
            minimum=SIGNED_INT32_MIN,
            maximum=SIGNED_INT32_MAX,
        )

    return normalized


def _require_non_negative_int(value: Any, *, field_name: str) -> int:
    normalized = _require_strict_int(value, field_name=field_name)

    if normalized < 0:
        raise CoordinateValidationError(
            f"'{field_name}' darf nicht negativ sein.",
            details={
                "field": field_name,
                "value": normalized,
                "minimum": 0,
            },
        )

    return normalized


def _require_positive_int(value: Any, *, field_name: str) -> int:
    normalized = _require_strict_int(value, field_name=field_name)

    if normalized <= 0:
        raise CoordinateValidationError(
            f"'{field_name}' muss größer als 0 sein.",
            details={
                "field": field_name,
                "value": normalized,
                "minimumExclusive": 0,
            },
        )

    return normalized


def _require_strict_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinateValidationError(
            f"'{field_name}' muss eine ganze Zahl sein.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
            },
        )

    return value


def _parse_strict_integer(value: str, *, field_name: str) -> int:
    normalized = value.strip()

    if not normalized:
        raise CoordinateValidationError(
            f"'{field_name}' darf nicht leer sein.",
            details={"field": field_name},
        )

    if normalized.startswith("+"):
        raise CoordinateValidationError(
            f"'{field_name}' darf kein führendes Pluszeichen besitzen.",
            details={
                "field": field_name,
                "value": normalized,
            },
        )

    try:
        parsed = int(normalized, 10)
    except (TypeError, ValueError, OverflowError) as error:
        raise CoordinateValidationError(
            f"'{field_name}' enthält keine gültige Ganzzahl.",
            details={
                "field": field_name,
                "value": normalized,
            },
            cause=error,
        ) from error

    if str(parsed) != normalized:
        raise CoordinateValidationError(
            f"'{field_name}' ist nicht kanonisch formatiert.",
            details={
                "field": field_name,
                "value": normalized,
                "canonicalValue": str(parsed),
            },
        )

    return _require_signed_int64(parsed, field_name=field_name)


def _require_finite_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CoordinateValidationError(
            f"'{field_name}' muss eine endliche Zahl sein.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
            },
        )

    normalized = float(value)
    if not isfinite(normalized):
        raise CoordinateValidationError(
            f"'{field_name}' muss endlich sein.",
            details={
                "field": field_name,
                "value": str(value),
            },
        )

    return normalized


def _require_non_empty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise CoordinateValidationError(
            f"'{field_name}' muss eine Zeichenfolge sein.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
            },
        )

    normalized = value.strip()
    if not normalized:
        raise CoordinateValidationError(
            f"'{field_name}' darf nicht leer sein.",
            details={"field": field_name},
        )

    return normalized


def _require_enum(
    value: Any,
    enum_type: type[StrEnum],
    *,
    field_name: str,
) -> Any:
    if isinstance(value, enum_type):
        return value

    if not isinstance(value, str):
        raise CoordinateValidationError(
            f"'{field_name}' besitzt einen ungültigen Enum-Wert.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
                "allowedValues": [item.value for item in enum_type],
            },
        )

    try:
        return enum_type(value.strip())
    except ValueError as error:
        raise CoordinateValidationError(
            f"'{field_name}' besitzt einen nicht unterstützten Wert.",
            details={
                "field": field_name,
                "value": value,
                "allowedValues": [item.value for item in enum_type],
            },
            cause=error,
        ) from error


def _normalize_axis_tuple(
    axes: Sequence[CoordinateAxis | str],
) -> tuple[CoordinateAxis, ...]:
    if isinstance(axes, (str, bytes, bytearray)) or not isinstance(
        axes,
        Sequence,
    ):
        raise CoordinateValidationError(
            "wrapped_axes muss eine Sequenz sein.",
            details={"actualType": type(axes).__name__},
        )

    normalized: list[CoordinateAxis] = []
    seen: set[CoordinateAxis] = set()

    for value in axes:
        axis = _require_enum(
            value,
            CoordinateAxis,
            field_name="wrappedAxes",
        )
        if axis in seen:
            continue
        seen.add(axis)
        normalized.append(axis)

    return tuple(normalized)


def _checked_add_int64(
    left: int,
    right: int,
    *,
    axis: CoordinateAxis,
) -> int:
    normalized_right = _require_signed_int64(right, field_name=f"d{axis.value}")
    result = left + normalized_right

    if result < SIGNED_INT64_MIN or result > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis=axis.value,
            value=result,
            minimum=SIGNED_INT64_MIN,
            maximum=SIGNED_INT64_MAX,
        )

    return result


def _checked_subtract_int64(
    left: int,
    right: int,
    *,
    axis: CoordinateAxis,
) -> int:
    result = left - right

    if result < SIGNED_INT64_MIN or result > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis=axis.value,
            value=result,
            minimum=SIGNED_INT64_MIN,
            maximum=SIGNED_INT64_MAX,
        )

    return result


__all__ = [
    "AxisConvention",
    "ChunkAddress",
    "ChunkPosition",
    "CoordinateAxis",
    "CoordinateSpace",
    "IntegerTriple",
    "JsonPrimitive",
    "JsonValue",
    "LocalBlockPosition",
    "LocalCellPosition",
    "LocalMetricPosition",
    "NormalizationMetadata",
    "NormalizationReason",
    "NormalizedBlockPosition",
    "NormalizedChunkAddress",
    "NumberTriple",
    "ResolvedCellAddress",
    "SIGNED_INT32_MAX",
    "SIGNED_INT32_MIN",
    "SIGNED_INT64_MAX",
    "SIGNED_INT64_MIN",
]
