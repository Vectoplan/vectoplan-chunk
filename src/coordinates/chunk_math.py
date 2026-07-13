# services/vectoplan-chunk/src/coordinates/chunk_math.py
"""Topologieneutrale Mathematik für Chunks, Blöcke und lokale Zellen.

Dieses Modul bildet die gemeinsame Rechenbasis für ``flat`` und ``earth``.
Periodische Wrap-Regeln, Nord-/Süd-Grenzen und providerabhängige
Normalisierungen gehören ausdrücklich nach ``src.coordinates.topology``.

Wesentliche Invarianten
-----------------------
* Negative Koordinaten verwenden mathematische Floor-Division.
* Eine Blockposition zerfällt eindeutig in Chunk- und lokale Zellposition.
* Lokale Zellkoordinaten liegen immer in ``0 <= value < chunk_size``.
* Die Rückrechnung aus Chunk und Zelle ergibt exakt die Ausgangsposition.
* Chunk-Keys werden ausschließlich aus validierten Chunkpositionen erzeugt.
* Alle positionsrelevanten Ganzzahlen bleiben im signierten 64-Bit-Bereich.
* Begrenzte Caches beschleunigen wiederholte reine Berechnungen, sind aber
  niemals Datenwahrheit und können jederzeit geleert werden.

Das Modul kennt weder Flask noch SQLAlchemy, CRS-Transformationen,
World-Provider, Datenbank-Sessions noch HTTP-Antworten.
"""

from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from typing import Any, Final, Iterator, Self

from .errors import (
    CellAddressInvalidError,
    CoordinateOverflowError,
    CoordinateValidationError,
    InvalidChunkSizeError,
)
from .models import (
    ChunkAddress,
    ChunkPosition,
    CoordinateAxis,
    JsonValue,
    LocalBlockPosition,
    LocalCellPosition,
    ResolvedCellAddress,
    SIGNED_INT64_MAX,
    SIGNED_INT64_MIN,
)


DEFAULT_CHUNK_SIZE: Final[int] = 16
_CHUNK_MATH_CONFIG_CACHE_SIZE: Final[int] = 64
_AXIS_SPLIT_CACHE_SIZE: Final[int] = 32_768
_AXIS_JOIN_CACHE_SIZE: Final[int] = 32_768
_RESOLVE_BLOCK_CACHE_SIZE: Final[int] = 16_384
_CHUNK_ORIGIN_CACHE_SIZE: Final[int] = 16_384


@dataclass(frozen=True, slots=True)
class ChunkMathConfig:
    """Validierte, unveränderliche Konfiguration eines kubischen Chunkrasters."""

    chunk_size: int = DEFAULT_CHUNK_SIZE

    def __post_init__(self) -> None:
        normalized_size = validate_chunk_size(self.chunk_size)
        object.__setattr__(self, "chunk_size", normalized_size)

    @classmethod
    def create(cls, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Self:
        """Erzeugt eine Konfiguration ohne Cache-Abhängigkeit."""

        return cls(chunk_size=chunk_size)

    @property
    def cell_count(self) -> int:
        """Anzahl der Zellen eines kubischen Chunks."""

        return checked_cell_count(self.chunk_size)

    @property
    def maximum_linear_index(self) -> int:
        return self.cell_count - 1

    def split_axis(self, value: int) -> tuple[int, int]:
        return split_axis(value, chunk_size=self.chunk_size)

    def join_axis(self, chunk: int, local: int) -> int:
        return join_axis(
            chunk_coordinate=chunk,
            local_coordinate=local,
            chunk_size=self.chunk_size,
        )

    def block_to_chunk(self, position: LocalBlockPosition) -> ChunkPosition:
        return block_to_chunk_position(
            position,
            chunk_size=self.chunk_size,
        )

    def block_to_cell(self, position: LocalBlockPosition) -> LocalCellPosition:
        return block_to_local_cell_position(
            position,
            chunk_size=self.chunk_size,
        )

    def resolve(self, position: LocalBlockPosition) -> ResolvedCellAddress:
        return resolve_block_address(
            position,
            chunk_size=self.chunk_size,
        )

    def compose(
        self,
        chunk: ChunkPosition | ChunkAddress,
        cell: LocalCellPosition,
    ) -> LocalBlockPosition:
        return chunk_cell_to_block_position(
            chunk,
            cell,
            chunk_size=self.chunk_size,
        )

    def chunk_origin(
        self,
        chunk: ChunkPosition | ChunkAddress,
    ) -> LocalBlockPosition:
        return chunk_to_block_origin(
            chunk,
            chunk_size=self.chunk_size,
        )

    def chunk_bounds(
        self,
        chunk: ChunkPosition | ChunkAddress,
    ) -> tuple[LocalBlockPosition, LocalBlockPosition]:
        return chunk_block_bounds(
            chunk,
            chunk_size=self.chunk_size,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "chunkSize": self.chunk_size,
            "cellCount": self.cell_count,
            "maximumLinearIndex": self.maximum_linear_index,
            "indexOrder": "x-fastest-y-then-z",
        }


def get_chunk_math(
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ChunkMathConfig:
    """Liefert eine gecachte unveränderliche Chunk-Konfiguration.

    Der Cache ist bewusst klein, weil produktiv nur wenige Chunkgrößen
    gleichzeitig verwendet werden sollen. Ein Cache-Miss verändert weder
    Verhalten noch Datenintegrität.
    """

    normalized_size = validate_chunk_size(chunk_size)
    return _get_chunk_math_cached(normalized_size)


@lru_cache(maxsize=_CHUNK_MATH_CONFIG_CACHE_SIZE)
def _get_chunk_math_cached(chunk_size: int) -> ChunkMathConfig:
    return ChunkMathConfig.create(chunk_size)


def validate_chunk_size(chunk_size: Any) -> int:
    """Validiert eine positive ganzzahlige kubische Chunkgröße.

    ``bool`` wird trotz seiner Python-Integer-Verwandtschaft abgelehnt.
    Zusätzlich wird geprüft, dass die Zellanzahl im signierten 64-Bit-Bereich
    darstellbar bleibt.
    """

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
        raise InvalidChunkSizeError(chunk_size)

    if chunk_size <= 0:
        raise InvalidChunkSizeError(chunk_size)

    if chunk_size > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis="chunkSize",
            value=chunk_size,
            minimum=1,
            maximum=SIGNED_INT64_MAX,
        )

    checked_cell_count(chunk_size)
    return chunk_size


def checked_cell_count(chunk_size: int) -> int:
    """Berechnet ``chunk_size ** 3`` mit 64-Bit-Überlaufschutz."""

    size = _require_positive_int(
        chunk_size,
        field_name="chunkSize",
        error_factory=InvalidChunkSizeError,
    )

    cell_count = size * size * size
    if cell_count > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis="cellCount",
            value=cell_count,
            minimum=1,
            maximum=SIGNED_INT64_MAX,
        )

    return cell_count


def floor_divide(value: int, divisor: int) -> int:
    """Mathematische Floor-Division für positive Divisoren.

    Python ``//`` besitzt bereits die benötigte Floor-Semantik. Die Funktion
    kapselt diese Regel, validiert Eingaben und verhindert, dass später
    versehentlich Truncation-toward-zero verwendet wird.
    """

    normalized_value = _require_int64(value, field_name="value")
    normalized_divisor = _require_positive_int(
        divisor,
        field_name="divisor",
    )

    result = normalized_value // normalized_divisor
    return _require_int64(result, field_name="quotient")


def floor_modulo(value: int, divisor: int) -> int:
    """Nicht-negativer Rest passend zu ``floor_divide``.

    Für einen positiven Divisor gilt stets:

    ``0 <= floor_modulo(value, divisor) < divisor``
    """

    normalized_value = _require_int64(value, field_name="value")
    normalized_divisor = _require_positive_int(
        divisor,
        field_name="divisor",
    )

    result = normalized_value % normalized_divisor
    if result < 0 or result >= normalized_divisor:
        raise CoordinateValidationError(
            "Floor-Modulo hat einen ungültigen Rest erzeugt.",
            details={
                "value": normalized_value,
                "divisor": normalized_divisor,
                "remainder": result,
            },
        )

    return result


def split_axis(
    value: int,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> tuple[int, int]:
    """Zerlegt eine Weltachse in Chunk- und lokale Zellkoordinate.

    Beispiele bei ``chunk_size=16``:

    ``0 -> (0, 0)``
    ``15 -> (0, 15)``
    ``16 -> (1, 0)``
    ``-1 -> (-1, 15)``
    ``-16 -> (-1, 0)``
    ``-17 -> (-2, 15)``
    """

    normalized_value = _require_int64(value, field_name="value")
    normalized_size = validate_chunk_size(chunk_size)
    return _split_axis_cached(normalized_value, normalized_size)


@lru_cache(maxsize=_AXIS_SPLIT_CACHE_SIZE)
def _split_axis_cached(
    value: int,
    chunk_size: int,
) -> tuple[int, int]:
    chunk_coordinate, local_coordinate = divmod(value, chunk_size)

    _require_int64(chunk_coordinate, field_name="chunkCoordinate")

    if local_coordinate < 0 or local_coordinate >= chunk_size:
        raise CoordinateValidationError(
            "Achsenzerlegung hat eine ungültige lokale Koordinate erzeugt.",
            details={
                "value": value,
                "chunkSize": chunk_size,
                "chunkCoordinate": chunk_coordinate,
                "localCoordinate": local_coordinate,
            },
        )

    return chunk_coordinate, local_coordinate


def join_axis(
    *,
    chunk_coordinate: int,
    local_coordinate: int,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Rekonstruiert eine Weltachse aus Chunk und lokaler Zelle."""

    normalized_chunk = _require_int64(
        chunk_coordinate,
        field_name="chunkCoordinate",
    )
    normalized_size = validate_chunk_size(chunk_size)
    normalized_local = _validate_local_coordinate(
        local_coordinate,
        chunk_size=normalized_size,
        axis="axis",
    )

    return _join_axis_cached(
        normalized_chunk,
        normalized_local,
        normalized_size,
    )


@lru_cache(maxsize=_AXIS_JOIN_CACHE_SIZE)
def _join_axis_cached(
    chunk_coordinate: int,
    local_coordinate: int,
    chunk_size: int,
) -> int:
    result = (chunk_coordinate * chunk_size) + local_coordinate
    return _require_int64(result, field_name="worldCoordinate")


def block_to_chunk_position(
    position: LocalBlockPosition,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ChunkPosition:
    """Bestimmt den Chunk einer lokalen Blockposition."""

    normalized_position = _require_block_position(position)
    normalized_size = validate_chunk_size(chunk_size)

    chunk_x, _ = _split_axis_cached(normalized_position.x, normalized_size)
    chunk_y, _ = _split_axis_cached(normalized_position.y, normalized_size)
    chunk_z, _ = _split_axis_cached(normalized_position.z, normalized_size)

    return ChunkPosition(chunk_x, chunk_y, chunk_z)


def block_to_local_cell_position(
    position: LocalBlockPosition,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> LocalCellPosition:
    """Bestimmt die lokale Zellposition innerhalb des zugehörigen Chunks."""

    normalized_position = _require_block_position(position)
    normalized_size = validate_chunk_size(chunk_size)

    _, local_x = _split_axis_cached(normalized_position.x, normalized_size)
    _, local_y = _split_axis_cached(normalized_position.y, normalized_size)
    _, local_z = _split_axis_cached(normalized_position.z, normalized_size)

    return LocalCellPosition(local_x, local_y, local_z).validate_for_chunk_size(
        normalized_size
    )


def resolve_block_address(
    position: LocalBlockPosition,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ResolvedCellAddress:
    """Löst eine lokale Blockposition vollständig in Chunk und Zelle auf."""

    normalized_position = _require_block_position(position)
    normalized_size = validate_chunk_size(chunk_size)

    return _resolve_block_address_cached(
        normalized_position.x,
        normalized_position.y,
        normalized_position.z,
        normalized_size,
    )


@lru_cache(maxsize=_RESOLVE_BLOCK_CACHE_SIZE)
def _resolve_block_address_cached(
    x: int,
    y: int,
    z: int,
    chunk_size: int,
) -> ResolvedCellAddress:
    chunk_x, local_x = _split_axis_cached(x, chunk_size)
    chunk_y, local_y = _split_axis_cached(y, chunk_size)
    chunk_z, local_z = _split_axis_cached(z, chunk_size)

    block = LocalBlockPosition(x, y, z)
    chunk = ChunkAddress.from_coordinates(chunk_x, chunk_y, chunk_z)
    cell = LocalCellPosition(local_x, local_y, local_z)
    linear_index = cell.to_linear_index(chunk_size)

    return ResolvedCellAddress(
        block=block,
        chunk=chunk,
        cell=cell,
        linear_index=linear_index,
        chunk_size=chunk_size,
    )


def chunk_cell_to_block_position(
    chunk: ChunkPosition | ChunkAddress,
    cell: LocalCellPosition,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> LocalBlockPosition:
    """Rekonstruiert eine lokale Blockposition aus Chunk und Zellposition."""

    normalized_chunk = _require_chunk_position(chunk)
    normalized_cell = _require_cell_position(cell)
    normalized_size = validate_chunk_size(chunk_size)

    normalized_cell.validate_for_chunk_size(normalized_size)

    return LocalBlockPosition(
        x=_join_axis_cached(
            normalized_chunk.x,
            normalized_cell.x,
            normalized_size,
        ),
        y=_join_axis_cached(
            normalized_chunk.y,
            normalized_cell.y,
            normalized_size,
        ),
        z=_join_axis_cached(
            normalized_chunk.z,
            normalized_cell.z,
            normalized_size,
        ),
    )


def chunk_to_block_origin(
    chunk: ChunkPosition | ChunkAddress,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> LocalBlockPosition:
    """Liefert die minimale Blockposition eines Chunks."""

    normalized_chunk = _require_chunk_position(chunk)
    normalized_size = validate_chunk_size(chunk_size)

    return _chunk_to_block_origin_cached(
        normalized_chunk.x,
        normalized_chunk.y,
        normalized_chunk.z,
        normalized_size,
    )


@lru_cache(maxsize=_CHUNK_ORIGIN_CACHE_SIZE)
def _chunk_to_block_origin_cached(
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    chunk_size: int,
) -> LocalBlockPosition:
    return LocalBlockPosition(
        x=_join_axis_cached(chunk_x, 0, chunk_size),
        y=_join_axis_cached(chunk_y, 0, chunk_size),
        z=_join_axis_cached(chunk_z, 0, chunk_size),
    )


def chunk_block_bounds(
    chunk: ChunkPosition | ChunkAddress,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> tuple[LocalBlockPosition, LocalBlockPosition]:
    """Liefert inklusive minimale und maximale Blockposition eines Chunks."""

    normalized_size = validate_chunk_size(chunk_size)
    minimum = chunk_to_block_origin(
        chunk,
        chunk_size=normalized_size,
    )
    offset = normalized_size - 1

    maximum = LocalBlockPosition(
        x=_checked_add(minimum.x, offset, axis=CoordinateAxis.X),
        y=_checked_add(minimum.y, offset, axis=CoordinateAxis.Y),
        z=_checked_add(minimum.z, offset, axis=CoordinateAxis.Z),
    )

    return minimum, maximum


def chunk_contains_block(
    chunk: ChunkPosition | ChunkAddress,
    block: LocalBlockPosition,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> bool:
    """Prüft, ob eine Blockposition im angegebenen Chunk liegt."""

    normalized_chunk = _require_chunk_position(chunk)
    resolved_chunk = block_to_chunk_position(
        _require_block_position(block),
        chunk_size=chunk_size,
    )
    return normalized_chunk == resolved_chunk


def same_chunk(
    first: LocalBlockPosition,
    second: LocalBlockPosition,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> bool:
    """Prüft, ob zwei Blockpositionen demselben Chunk angehören."""

    normalized_size = validate_chunk_size(chunk_size)
    return (
        block_to_chunk_position(first, chunk_size=normalized_size)
        == block_to_chunk_position(second, chunk_size=normalized_size)
    )


def linear_index_to_cell(
    index: int,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> LocalCellPosition:
    """Konvertiert einen linearen x-fastest-y-then-z-Index in eine Zelle."""

    normalized_size = validate_chunk_size(chunk_size)
    return LocalCellPosition.from_linear_index(
        index,
        chunk_size=normalized_size,
    )


def cell_to_linear_index(
    cell: LocalCellPosition,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Konvertiert eine lokale Zelle in den linearen Chunkindex."""

    normalized_cell = _require_cell_position(cell)
    normalized_size = validate_chunk_size(chunk_size)
    return normalized_cell.to_linear_index(normalized_size)


def iter_chunk_cells(
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[LocalCellPosition]:
    """Iteriert Zellen in derselben Reihenfolge wie der lineare Index.

    Reihenfolge: X ist die schnellste, danach Y, danach Z.
    """

    size = validate_chunk_size(chunk_size)

    for z in range(size):
        for y in range(size):
            for x in range(size):
                yield LocalCellPosition(x, y, z)


def iter_chunk_blocks(
    chunk: ChunkPosition | ChunkAddress,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[LocalBlockPosition]:
    """Iteriert alle Blockpositionen eines Chunks in Zellindexreihenfolge."""

    normalized_chunk = _require_chunk_position(chunk)
    size = validate_chunk_size(chunk_size)

    for cell in iter_chunk_cells(chunk_size=size):
        yield chunk_cell_to_block_position(
            normalized_chunk,
            cell,
            chunk_size=size,
        )


def boundary_offsets_for_cell(
    cell: LocalCellPosition,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    include_diagonal_combinations: bool = True,
    include_current_chunk: bool = True,
) -> tuple[ChunkPosition, ...]:
    """Liefert relative Chunkoffsets, die eine Grenzzelle berührt.

    Die Funktion ist topologieneutral. Sie erzeugt nur relative Offsets.
    Eine spätere Topologiestrategie normalisiert daraus konkrete
    Chunkpositionen, beispielsweise über die Earth-Weltnaht.

    Bei einer inneren Zelle wird ausschließlich ``(0, 0, 0)`` geliefert,
    sofern ``include_current_chunk`` aktiv ist.

    An einer Ecke können bei aktivierten Diagonalkombinationen bis zu acht
    Offsets entstehen. Ohne Diagonalkombinationen werden nur flächenadjazente
    Nachbarn plus optional der aktuelle Chunk geliefert.
    """

    normalized_cell = _require_cell_position(cell)
    size = validate_chunk_size(chunk_size)
    normalized_cell.validate_for_chunk_size(size)

    axis_options: list[tuple[int, ...]] = []
    for value in normalized_cell.as_tuple():
        options = [0]
        if value == 0:
            options.append(-1)
        if value == size - 1:
            options.append(1)
        axis_options.append(tuple(options))

    offsets: set[ChunkPosition] = set()

    if include_diagonal_combinations:
        for dx, dy, dz in product(*axis_options):
            if not include_current_chunk and dx == dy == dz == 0:
                continue
            offsets.add(ChunkPosition(dx, dy, dz))
    else:
        if include_current_chunk:
            offsets.add(ChunkPosition.origin())

        x_options, y_options, z_options = axis_options
        for dx in x_options:
            if dx:
                offsets.add(ChunkPosition(dx, 0, 0))
        for dy in y_options:
            if dy:
                offsets.add(ChunkPosition(0, dy, 0))
        for dz in z_options:
            if dz:
                offsets.add(ChunkPosition(0, 0, dz))

    return tuple(sorted(offsets))


def apply_chunk_offset(
    chunk: ChunkPosition | ChunkAddress,
    offset: ChunkPosition,
) -> ChunkPosition:
    """Addiert einen relativen Chunkoffset mit 64-Bit-Überlaufschutz."""

    normalized_chunk = _require_chunk_position(chunk)
    normalized_offset = _require_chunk_position(offset)

    return ChunkPosition(
        x=_checked_add(
            normalized_chunk.x,
            normalized_offset.x,
            axis=CoordinateAxis.X,
        ),
        y=_checked_add(
            normalized_chunk.y,
            normalized_offset.y,
            axis=CoordinateAxis.Y,
        ),
        z=_checked_add(
            normalized_chunk.z,
            normalized_offset.z,
            axis=CoordinateAxis.Z,
        ),
    )


def clear_chunk_math_caches() -> None:
    """Leert ausschließlich ableitbare In-Process-Caches."""

    _get_chunk_math_cached.cache_clear()
    _split_axis_cached.cache_clear()
    _join_axis_cached.cache_clear()
    _resolve_block_address_cached.cache_clear()
    _chunk_to_block_origin_cached.cache_clear()


def chunk_math_cache_info() -> dict[str, JsonValue]:
    """Liefert eine serialisierbare Diagnose der begrenzten Caches."""

    return {
        "config": _cache_info_to_dict(_get_chunk_math_cached.cache_info()),
        "axisSplit": _cache_info_to_dict(_split_axis_cached.cache_info()),
        "axisJoin": _cache_info_to_dict(_join_axis_cached.cache_info()),
        "resolveBlock": _cache_info_to_dict(
            _resolve_block_address_cached.cache_info()
        ),
        "chunkOrigin": _cache_info_to_dict(
            _chunk_to_block_origin_cached.cache_info()
        ),
    }


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


def _require_block_position(position: Any) -> LocalBlockPosition:
    if not isinstance(position, LocalBlockPosition):
        raise CoordinateValidationError(
            "position muss eine LocalBlockPosition sein.",
            details={"actualType": type(position).__name__},
        )

    return position


def _require_chunk_position(
    chunk: ChunkPosition | ChunkAddress | Any,
) -> ChunkPosition:
    if isinstance(chunk, ChunkAddress):
        return chunk.position

    if isinstance(chunk, ChunkPosition):
        return chunk

    raise CoordinateValidationError(
        "chunk muss eine ChunkPosition oder ChunkAddress sein.",
        details={"actualType": type(chunk).__name__},
    )


def _require_cell_position(cell: Any) -> LocalCellPosition:
    if not isinstance(cell, LocalCellPosition):
        raise CoordinateValidationError(
            "cell muss eine LocalCellPosition sein.",
            details={"actualType": type(cell).__name__},
        )

    return cell


def _validate_local_coordinate(
    value: Any,
    *,
    chunk_size: int,
    axis: str,
) -> int:
    normalized_value = _require_int64(value, field_name=f"local{axis}")
    if normalized_value < 0 or normalized_value >= chunk_size:
        raise CellAddressInvalidError(
            local_x=normalized_value if axis in {"axis", "x"} else None,
            local_y=normalized_value if axis == "y" else None,
            local_z=normalized_value if axis == "z" else None,
            chunk_size=chunk_size,
        )

    return normalized_value


def _require_positive_int(
    value: Any,
    *,
    field_name: str,
    error_factory: Any | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if error_factory is not None:
            raise error_factory(value)

        raise CoordinateValidationError(
            f"'{field_name}' muss eine positive ganze Zahl sein.",
            details={
                "field": field_name,
                "value": value if isinstance(value, (int, float, str)) else None,
                "actualType": type(value).__name__,
            },
        )

    if value > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis=field_name,
            value=value,
            minimum=1,
            maximum=SIGNED_INT64_MAX,
        )

    return value


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


def _checked_add(
    left: int,
    right: int,
    *,
    axis: CoordinateAxis,
) -> int:
    normalized_left = _require_int64(left, field_name=f"{axis.value}Left")
    normalized_right = _require_int64(right, field_name=f"{axis.value}Right")
    result = normalized_left + normalized_right

    if result < SIGNED_INT64_MIN or result > SIGNED_INT64_MAX:
        raise CoordinateOverflowError(
            axis=axis.value,
            value=result,
            minimum=SIGNED_INT64_MIN,
            maximum=SIGNED_INT64_MAX,
        )

    return result


__all__ = [
    "ChunkMathConfig",
    "DEFAULT_CHUNK_SIZE",
    "apply_chunk_offset",
    "block_to_chunk_position",
    "block_to_local_cell_position",
    "boundary_offsets_for_cell",
    "cell_to_linear_index",
    "checked_cell_count",
    "chunk_block_bounds",
    "chunk_cell_to_block_position",
    "chunk_contains_block",
    "chunk_math_cache_info",
    "chunk_to_block_origin",
    "clear_chunk_math_caches",
    "floor_divide",
    "floor_modulo",
    "get_chunk_math",
    "iter_chunk_blocks",
    "iter_chunk_cells",
    "join_axis",
    "linear_index_to_cell",
    "resolve_block_address",
    "same_chunk",
    "split_axis",
    "validate_chunk_size",
]
