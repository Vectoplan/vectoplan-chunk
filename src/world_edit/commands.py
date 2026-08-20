"""Pure WorldEdit command planning and parcel-mask evaluation.

The module deliberately knows nothing about SQLAlchemy or Flask.  It turns a
validated command payload into an immutable list of world cells.  Persistence,
block registration and event creation remain owned by ``routes.commands``.

Parcel geometry is treated as an editing mask, not as a replacement grid.  A
cell is editable when its horizontal centre lies inside the union of the
selected parcel polygons.  This keeps chunk addressing stable while allowing
adjacent selected parcels to form one continuous build area.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import math
from typing import Any, Iterable

from src.georeferencing.contracts import GlobalCoordinate
from src.georeferencing.crs import canonical_geographic_crs


class WorldEditValidationError(ValueError):
    """Raised when a WorldEdit payload cannot be planned safely."""


PointXZ = tuple[float, float]
RingXZ = tuple[PointXZ, ...]
PolygonXZ = tuple[RingXZ, ...]


@dataclass(frozen=True, slots=True)
class WorldEditPlan:
    tool: str
    operation: str
    positions: tuple[tuple[int, int, int], ...]
    requested_cell_count: int
    parcel_mask_enabled: bool
    parcel_count: int
    coverage_policy: str


def _record(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _text(value: Any, fallback: str = "") -> str:
    try:
        result = str(value or "").strip()
    except Exception:
        return fallback
    return result or fallback


def _integer(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorldEditValidationError(f"{name} muss eine Ganzzahl sein.") from exc
    if converted < minimum or converted > maximum:
        raise WorldEditValidationError(
            f"{name} muss zwischen {minimum} und {maximum} liegen."
        )
    return converted


def _number(
    value: Any,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorldEditValidationError(f"{name} muss eine Zahl sein.") from exc
    if not math.isfinite(converted) or converted < minimum or converted > maximum:
        raise WorldEditValidationError(
            f"{name} muss zwischen {minimum} und {maximum} liegen."
        )
    return converted


def _world_position(value: Any, *, name: str) -> tuple[int, int, int]:
    source = _record(value)
    return (
        _integer(source.get("x"), name=f"{name}.x", minimum=-2**30, maximum=2**30),
        _integer(source.get("y"), name=f"{name}.y", minimum=-2**20, maximum=2**20),
        _integer(source.get("z"), name=f"{name}.z", minimum=-2**30, maximum=2**30),
    )


def _normalize_operation(payload: Mapping[str, Any]) -> tuple[str, str]:
    tool = _text(payload.get("tool") or payload.get("worldEditTool"), "selection").lower()
    operation = _text(payload.get("operation") or payload.get("mode"), "set").lower()
    aliases = {
        "paint-brush": "paint",
        "paint_brush": "paint",
        "sculpt-brush": "sculpt",
        "sculpt_brush": "sculpt",
        "selection-tool": "selection",
        "selection_tool": "selection",
        "remove": "clear",
        "subtract": "clear",
        "erase": "clear",
    }
    tool = aliases.get(tool, tool)
    operation = aliases.get(operation, operation)
    supported_tools = {"selection", "paint", "sculpt", "clipboard"}
    supported_operations = {"set", "wall", "fill", "replace", "clear", "copy", "cut", "paste"}
    if tool not in supported_tools:
        raise WorldEditValidationError(f"WorldEdit-Werkzeug '{tool}' wird nicht unterstuetzt.")
    if operation not in supported_operations:
        raise WorldEditValidationError(f"WorldEdit-Operation '{operation}' wird nicht unterstuetzt.")
    return tool, operation


def _selection_candidates(
    payload: Mapping[str, Any],
    *,
    operation: str,
) -> Iterable[tuple[int, int, int]]:
    bounds = _record(payload.get("bounds") or payload.get("selection"))
    first = _world_position(
        bounds.get("min") or bounds.get("first") or bounds.get("from"),
        name="bounds.min",
    )
    second = _world_position(
        bounds.get("max") or bounds.get("second") or bounds.get("to"),
        name="bounds.max",
    )
    min_x, min_y, min_z = (min(first[index], second[index]) for index in range(3))
    max_x, max_y, max_z = (max(first[index], second[index]) for index in range(3))

    for y in range(min_y, max_y + 1):
        for z in range(min_z, max_z + 1):
            for x in range(min_x, max_x + 1):
                if operation == "wall" and x not in {min_x, max_x} and z not in {min_z, max_z}:
                    continue
                yield x, y, z


def _deterministic_density(x: int, y: int, z: int) -> int:
    # Stable integer hash; unlike random(), previews and server results agree.
    return abs((x * 73_856_093) ^ (y * 19_349_663) ^ (z * 83_492_791)) % 100


def _inside_brush_shape(
    shape: str,
    dx: int,
    dy: int,
    dz: int,
    rx: int,
    ry: int,
    rz: int,
) -> bool:
    if shape == "box":
        return True
    if shape == "cylinder":
        return ((dx / max(1, rx)) ** 2) + ((dz / max(1, rz)) ** 2) <= 1.0
    return (
        ((dx / max(1, rx)) ** 2)
        + ((dy / max(1, ry)) ** 2)
        + ((dz / max(1, rz)) ** 2)
        <= 1.0
    )


def _inside_inner_brush_shape(
    shape: str,
    dx: int,
    dy: int,
    dz: int,
    rx: int,
    ry: int,
    rz: int,
    thickness: int,
) -> bool:
    inner_x = rx - thickness
    inner_y = ry - thickness
    inner_z = rz - thickness
    if min(inner_x, inner_y, inner_z) <= 0:
        return False
    return _inside_brush_shape(shape, dx, dy, dz, inner_x, inner_y, inner_z)


def _brush_candidates(payload: Mapping[str, Any]) -> Iterable[tuple[int, int, int]]:
    center = _world_position(
        payload.get("position") or payload.get("target"),
        name="position",
    )
    brush = _record(payload.get("brush") or payload.get("settings"))
    radius = _integer(brush.get("radius", 2), name="brush.radius", minimum=1, maximum=64)
    rx = _integer(brush.get("radiusX", radius), name="brush.radiusX", minimum=1, maximum=64)
    ry = _integer(brush.get("radiusY", radius), name="brush.radiusY", minimum=1, maximum=64)
    rz = _integer(brush.get("radiusZ", radius), name="brush.radiusZ", minimum=1, maximum=64)
    shape = _text(brush.get("shape"), "sphere").lower()
    if shape not in {"sphere", "box", "cylinder"}:
        raise WorldEditValidationError("brush.shape muss sphere, box oder cylinder sein.")
    density = _number(
        brush.get("density", 100),
        name="brush.density",
        minimum=1,
        maximum=100,
    )
    wall_thickness = _integer(
        brush.get("wallThickness", 0),
        name="brush.wallThickness",
        minimum=0,
        maximum=32,
    )

    cx, cy, cz = center
    for dy in range(-ry, ry + 1):
        for dz in range(-rz, rz + 1):
            for dx in range(-rx, rx + 1):
                if not _inside_brush_shape(shape, dx, dy, dz, rx, ry, rz):
                    continue
                if wall_thickness and _inside_inner_brush_shape(
                    shape, dx, dy, dz, rx, ry, rz, wall_thickness
                ):
                    continue
                x, y, z = cx + dx, cy + dy, cz + dz
                if density < 100 and _deterministic_density(x, y, z) >= density:
                    continue
                yield x, y, z


def _clipboard_candidates(payload: Mapping[str, Any]) -> Iterable[tuple[int, int, int]]:
    anchor = _world_position(payload.get("position") or payload.get("target"), name="position")
    cells = _items(payload.get("clipboard") or payload.get("cells"))
    if not cells:
        raise WorldEditValidationError("Die WorldEdit-Zwischenablage ist leer.")
    for index, value in enumerate(cells):
        cell = _record(value)
        yield (
            anchor[0] + _integer(cell.get("dx", 0), name=f"clipboard[{index}].dx", minimum=-2**20, maximum=2**20),
            anchor[1] + _integer(cell.get("dy", 0), name=f"clipboard[{index}].dy", minimum=-2**20, maximum=2**20),
            anchor[2] + _integer(cell.get("dz", 0), name=f"clipboard[{index}].dz", minimum=-2**20, maximum=2**20),
        )


def _coordinate_point(value: Any) -> PointXZ:
    coordinate = _items(value)
    if len(coordinate) < 2:
        raise WorldEditValidationError("Polygonkoordinate benoetigt zwei Werte.")
    return (
        _number(coordinate[0], name="geometry.x", minimum=-1e12, maximum=1e12),
        _number(coordinate[1], name="geometry.z", minimum=-1e12, maximum=1e12),
    )


def _local_point(
    point: PointXZ,
    *,
    coordinate_space: str,
    provider: Any,
    grid_rotation_degrees: float,
) -> PointXZ:
    if coordinate_space in {"world-xz", "world_xz", "local", "voxel"}:
        return point
    if coordinate_space not in {"wgs84", "epsg:4326", "geojson"}:
        raise WorldEditValidationError(
            "parcelMask.coordinateSpace muss wgs84 oder world-xz sein."
        )
    if provider is None:
        raise WorldEditValidationError(
            "Eine WGS84-Grundstuecksmaske benoetigt einen Earth-Provider."
        )
    result = provider.global_to_local(
        GlobalCoordinate.from_values(Decimal(str(point[0])), Decimal(str(point[1])), Decimal("0")),
        canonical_geographic_crs(),
    )
    position = result.local_position
    # Earth worlds have one immutable north-oriented storage frame.  The
    # client-side preferred grid angle is presentation metadata and must never
    # rotate authoritative parcel masks away from the server's voxel cells.
    return float(position.x), float(position.z)


def _ring(
    value: Any,
    *,
    coordinate_space: str,
    provider: Any,
    grid_rotation_degrees: float,
) -> RingXZ:
    result = tuple(
        _local_point(
            _coordinate_point(item),
            coordinate_space=coordinate_space,
            provider=provider,
            grid_rotation_degrees=grid_rotation_degrees,
        )
        for item in _items(value)
    )
    if len(result) < 3:
        raise WorldEditValidationError("Ein Polygonring benoetigt mindestens drei Punkte.")
    return result


def _geometry_polygons(
    geometry: Mapping[str, Any],
    *,
    coordinate_space: str,
    provider: Any,
    grid_rotation_degrees: float,
) -> tuple[PolygonXZ, ...]:
    geometry_type = _text(geometry.get("type")).lower()
    coordinates = geometry.get("coordinates")
    if geometry_type == "polygon":
        polygon = tuple(
            _ring(
                item,
                coordinate_space=coordinate_space,
                provider=provider,
                grid_rotation_degrees=grid_rotation_degrees,
            )
            for item in _items(coordinates)
        )
        return (polygon,) if polygon else ()
    if geometry_type == "multipolygon":
        return tuple(
            tuple(
                _ring(
                    ring,
                    coordinate_space=coordinate_space,
                    provider=provider,
                    grid_rotation_degrees=grid_rotation_degrees,
                )
                for ring in _items(polygon)
            )
            for polygon in _items(coordinates)
        )
    raise WorldEditValidationError("Grundstuecksgeometrie muss Polygon oder MultiPolygon sein.")


def _parcel_polygons(
    payload: Mapping[str, Any],
    *,
    provider: Any,
) -> tuple[bool, tuple[PolygonXZ, ...], int, str]:
    mask = _record(payload.get("parcelMask") or payload.get("parcel_mask"))
    enabled = bool(mask.get("enabled", False))
    coverage_policy = _text(mask.get("coveragePolicy"), "cell-contained").lower().replace("_", "-")
    if coverage_policy not in {"cell-center", "cell-contained"}:
        raise WorldEditValidationError(
            "coveragePolicy muss cell-center oder cell-contained sein."
        )
    if not enabled:
        return False, (), 0, coverage_policy

    coordinate_space = _text(mask.get("coordinateSpace"), "wgs84").lower()
    grid_rotation_degrees = _number(
        mask.get("gridRotationDegrees", mask.get("grid_rotation_degrees", 0)),
        name="parcelMask.gridRotationDegrees",
        minimum=-360,
        maximum=360,
    )
    parcels = _items(mask.get("parcels") or mask.get("features"))
    if not parcels:
        raise WorldEditValidationError(
            "Grundstuecksmaske ist aktiv, aber es wurde kein Grundstueck ausgewaehlt."
        )
    polygons: list[PolygonXZ] = []
    for parcel in parcels:
        parcel_record = _record(parcel)
        geometry = _record(parcel_record.get("geometry") or parcel_record)
        polygons.extend(
            _geometry_polygons(
                geometry,
                coordinate_space=coordinate_space,
                provider=provider,
                grid_rotation_degrees=grid_rotation_degrees,
            )
        )
    if not polygons:
        raise WorldEditValidationError("Die Grundstuecksauswahl enthaelt keine Polygone.")
    return True, tuple(polygons), len(parcels), coverage_policy


def _point_on_segment(point: PointXZ, first: PointXZ, second: PointXZ) -> bool:
    px, pz = point
    ax, az = first
    bx, bz = second
    cross = ((px - ax) * (bz - az)) - ((pz - az) * (bx - ax))
    if abs(cross) > 1e-8:
        return False
    return (
        min(ax, bx) - 1e-8 <= px <= max(ax, bx) + 1e-8
        and min(az, bz) - 1e-8 <= pz <= max(az, bz) + 1e-8
    )


def _point_in_ring(point: PointXZ, ring: RingXZ) -> bool:
    inside = False
    previous = ring[-1]
    for current in ring:
        if _point_on_segment(point, previous, current):
            return True
        ax, az = previous
        bx, bz = current
        if (az > point[1]) != (bz > point[1]):
            crossing_x = ((bx - ax) * (point[1] - az) / (bz - az)) + ax
            if point[0] < crossing_x:
                inside = not inside
        previous = current
    return inside


def _point_in_polygon(point: PointXZ, polygon: PolygonXZ) -> bool:
    if not polygon or not _point_in_ring(point, polygon[0]):
        return False
    return not any(_point_in_ring(point, hole) for hole in polygon[1:])


def build_world_edit_plan(
    payload: Mapping[str, Any],
    *,
    provider: Any = None,
    max_cells: int = 65_536,
) -> WorldEditPlan:
    """Build a bounded, deterministic WorldEdit cell plan."""
    if not isinstance(payload, Mapping):
        raise WorldEditValidationError("WorldEdit-Payload muss ein Objekt sein.")
    safe_max = _integer(max_cells, name="max_cells", minimum=1, maximum=10_000_000)
    tool, operation = _normalize_operation(payload)
    if tool == "clipboard":
        candidates = (
            _clipboard_candidates(payload)
            if operation == "paste"
            else _selection_candidates(payload, operation=operation)
        )
    else:
        candidates = (
            _selection_candidates(payload, operation=operation)
            if tool == "selection"
            else _brush_candidates(payload)
        )
    mask_enabled, polygons, parcel_count, coverage_policy = _parcel_polygons(
        payload,
        provider=provider,
    )

    positions: list[tuple[int, int, int]] = []
    requested = 0
    seen: set[tuple[int, int, int]] = set()
    for position in candidates:
        if position in seen:
            continue
        seen.add(position)
        requested += 1
        if requested > safe_max:
            raise WorldEditValidationError(
                f"WorldEdit wuerde mehr als {safe_max} Zellen betreffen."
            )
        if mask_enabled:
            points = (
                ((position[0] + 0.5, position[2] + 0.5),)
                if coverage_policy == "cell-center"
                else (
                    (float(position[0]), float(position[2])),
                    (float(position[0] + 1), float(position[2])),
                    (float(position[0] + 1), float(position[2] + 1)),
                    (float(position[0]), float(position[2] + 1)),
                )
            )
            if not all(
                any(_point_in_polygon(point, polygon) for polygon in polygons)
                for point in points
            ):
                continue
        positions.append(position)

    if mask_enabled and not positions:
        raise WorldEditValidationError(
            "Die Auswahl liegt vollstaendig ausserhalb der ausgewaehlten Grundstuecke."
        )
    return WorldEditPlan(
        tool=tool,
        operation=operation,
        positions=tuple(positions),
        requested_cell_count=requested,
        parcel_mask_enabled=mask_enabled,
        parcel_count=parcel_count,
        coverage_policy=coverage_policy,
    )
