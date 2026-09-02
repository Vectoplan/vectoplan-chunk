from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from ...contracts import content_fingerprint, finite_number


def _key(x: float, y: float, z: float, chunk_size: int) -> str:
    return f"{math.floor(x / chunk_size)}:{math.floor(y / chunk_size)}:{math.floor(z / chunk_size)}"


def _position(value: Any, *, field: str) -> tuple[float, float, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an x/y/z object")
    x = finite_number(value.get("x"), field=f"{field}.x")
    y = finite_number(value.get("y"), field=f"{field}.y")
    z = finite_number(value.get("z"), field=f"{field}.z")
    return x, y, z


def _plan_point(value: Any, *, field: str) -> tuple[float, float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) < 2
    ):
        raise ValueError(f"{field} must contain world x/z")
    return (
        finite_number(value[0], field=f"{field}[0]"),
        finite_number(value[1], field=f"{field}[1]"),
    )


def _rectangle_candidates(
    min_x: float,
    min_y: float,
    min_z: float,
    max_x: float,
    max_y: float,
    max_z: float,
    chunk_size: int,
):
    for chunk_x in range(math.floor(min_x / chunk_size), math.floor(max_x / chunk_size) + 1):
        for chunk_y in range(math.floor(min_y / chunk_size), math.floor(max_y / chunk_size) + 1):
            for chunk_z in range(math.floor(min_z / chunk_size), math.floor(max_z / chunk_size) + 1):
                yield f"{chunk_x}:{chunk_y}:{chunk_z}"


def _segment_intersects_rectangle(
    start: tuple[float, float],
    end: tuple[float, float],
    bounds: tuple[float, float, float, float],
) -> bool:
    """Liang-Barsky segment/rectangle intersection, including the boundary."""
    x0, z0 = start
    x1, z1 = end
    min_x, min_z, max_x, max_z = bounds
    dx, dz = x1 - x0, z1 - z0
    lower, upper = 0.0, 1.0
    for denominator, numerator in zip(
        (-dx, dx, -dz, dz),
        (x0 - min_x, max_x - x0, z0 - min_z, max_z - z0),
        strict=True,
    ):
        if abs(denominator) <= 1e-12:
            if numerator < 0:
                return False
            continue
        ratio = numerator / denominator
        if denominator < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return False
    return True


def _roof_bounds(roof: Mapping[str, Any], *, field: str) -> tuple[float, float, float, float, float, float]:
    position = _position(roof.get("position"), field=f"{field}.position")
    dimensions = roof.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise ValueError(f"{field}.dimensions must be an object")
    extents = tuple(
        finite_number(dimensions.get(axis), field=f"{field}.dimensions.{axis}")
        for axis in ("x", "y", "z")
    )
    if any(value <= 0 for value in extents):
        raise ValueError(f"{field}.dimensions must be positive")

    footprint = roof.get("footprint")
    plan_points: list[tuple[float, float]] = []
    if isinstance(footprint, Mapping):
        rings = footprint.get("coordinates")
        if isinstance(rings, list):
            for ring_index, ring in enumerate(rings):
                if not isinstance(ring, list):
                    raise ValueError(f"{field}.footprint.coordinates[{ring_index}] must be an array")
                plan_points.extend(
                    _plan_point(point, field=f"{field}.footprint.coordinates[{ring_index}][{point_index}]")
                    for point_index, point in enumerate(ring)
                )
    if plan_points:
        min_x = min(point[0] for point in plan_points)
        max_x = max(point[0] for point in plan_points)
        min_z = min(point[1] for point in plan_points)
        max_z = max(point[1] for point in plan_points)
    else:
        min_x, min_z = position[0], position[2]
        max_x, max_z = min_x + extents[0], min_z + extents[2]
    base_y = (
        finite_number(footprint.get("baseY"), field=f"{field}.footprint.baseY")
        if isinstance(footprint, Mapping) and footprint.get("baseY") is not None
        else position[1]
    )
    height = (
        finite_number(footprint.get("height"), field=f"{field}.footprint.height")
        if isinstance(footprint, Mapping) and footprint.get("height") is not None
        else extents[1]
    )
    return min_x, base_y, min_z, max_x, base_y + max(0.1, height), max_z


def run(context: Mapping[str, Any], artifacts: Mapping[str, Any]) -> Mapping[str, Any]:
    chunk_size = int(context.get("chunkSize") or 32)
    chunks: dict[str, dict[str, Any]] = {}

    def chunk(key: str) -> dict[str, Any]:
        return chunks.setdefault(key, {
            "chunkKey": key,
            "wallBlocks": [],
            "roofObjectRefs": [],
            "parcelGridRefs": [],
            "streetSegments": [],
        })

    buildings = artifacts.get("lod2-editable", {}).get("items", [])
    for building_index, building in enumerate(buildings if isinstance(buildings, list) else []):
        if not isinstance(building, Mapping):
            raise ValueError(f"editableBuildings[{building_index}] must be an object")
        building_id = str(building.get("buildingId") or "")
        wall_contract = building.get("wallBlocks")
        if not isinstance(wall_contract, Mapping):
            raise ValueError(f"Editable building {building_id} has no wall-block contract")
        walls = wall_contract.get("cells")
        if not isinstance(walls, list):
            raise ValueError(f"Editable building {building_id} wall cells must be an array")
        for cell_index, cell in enumerate(walls):
            if not isinstance(cell, (list, tuple)) or len(cell) != 3:
                raise ValueError(f"Editable building {building_id} wall cell {cell_index} is invalid")
            key = _key(float(cell[0]), float(cell[1]), float(cell[2]), chunk_size)
            chunk(key)["wallBlocks"].append({
                "buildingId": building_id,
                "blockTypeId": str(wall_contract.get("blockTypeId") or "lod2_exterior_wall"),
                "breakable": bool(wall_contract.get("breakable", True)),
                "cell": list(cell),
            })

        roofs = building.get("worldEditRoofs")
        if not isinstance(roofs, list):
            raise ValueError(f"Editable building {building_id} roofs must be an array")
        for roof_index, roof in enumerate(roofs):
            if not isinstance(roof, Mapping):
                raise ValueError(f"Editable building {building_id} roof {roof_index} is invalid")
            object_id = str(roof.get("objectInstanceId") or roof.get("object_instance_id") or "")
            position = _position(roof.get("position"), field=f"roof[{object_id}].position")
            primary_key = _key(position[0], position[1], position[2], chunk_size)
            bounds = _roof_bounds(roof, field=f"roof[{object_id}]")
            for key in _rectangle_candidates(*bounds, chunk_size):
                chunk(key)["roofObjectRefs"].append({
                    "buildingId": building_id,
                    "objectInstanceId": object_id,
                    "primaryChunkKey": primary_key,
                    "refRole": "primary" if key == primary_key else "footprint",
                })

    grids = artifacts.get("parcel-grid", {}).get("items", [])
    for grid_index, grid in enumerate(grids if isinstance(grids, list) else []):
        if not isinstance(grid, Mapping):
            raise ValueError(f"parcelGrids[{grid_index}] must be an object")
        origin = _plan_point(grid.get("origin"), field=f"parcelGrids[{grid_index}].origin")
        axis_u = _plan_point(grid.get("axisU"), field=f"parcelGrids[{grid_index}].axisU")
        axis_v = _plan_point(grid.get("axisV"), field=f"parcelGrids[{grid_index}].axisV")
        width = finite_number(grid.get("widthM"), field=f"parcelGrids[{grid_index}].widthM")
        depth = finite_number(grid.get("depthM"), field=f"parcelGrids[{grid_index}].depthM")
        if width <= 0 or depth <= 0:
            raise ValueError(f"parcelGrids[{grid_index}] widthM and depthM must be positive")
        corners = (
            origin,
            (origin[0] + axis_u[0] * width, origin[1] + axis_u[1] * width),
            (origin[0] + axis_v[0] * depth, origin[1] + axis_v[1] * depth),
            (
                origin[0] + axis_u[0] * width + axis_v[0] * depth,
                origin[1] + axis_u[1] * width + axis_v[1] * depth,
            ),
        )
        primary_key = _key(origin[0], 0, origin[1], chunk_size)
        for key in _rectangle_candidates(
            min(point[0] for point in corners),
            0,
            min(point[1] for point in corners),
            max(point[0] for point in corners),
            0,
            max(point[1] for point in corners),
            chunk_size,
        ):
            chunk(key)["parcelGridRefs"].append({
                "buildingId": str(grid.get("buildingId") or ""),
                "gridVersion": str(grid.get("gridVersion") or ""),
                "primaryChunkKey": primary_key,
                "refRole": "primary" if key == primary_key else "coverage",
            })

    roads = artifacts.get("road-network", {}).get("items", [])
    for road_index, road in enumerate(roads if isinstance(roads, list) else []):
        if not isinstance(road, Mapping):
            raise ValueError(f"streetNetwork[{road_index}] must be an object")
        points = road.get("centerline")
        if not isinstance(points, list):
            raise ValueError(f"streetNetwork[{road_index}].centerline must be an array")
        nominal_width = finite_number(road.get("nominalWidthM") or 6, field=f"streetNetwork[{road_index}].nominalWidthM")
        segment_widths = road.get("segmentWidthsM")
        if segment_widths is not None and (
            not isinstance(segment_widths, list) or len(segment_widths) != max(0, len(points) - 1)
        ):
            raise ValueError(f"streetNetwork[{road_index}].segmentWidthsM must align with centerline segments")
        fallback_width = finite_number(
            road.get("effectiveWidthM") or nominal_width,
            field=f"streetNetwork[{road_index}].effectiveWidthM",
        )
        for segment_index, (raw_start, raw_end) in enumerate(zip(points, points[1:])):
            start = _plan_point(raw_start, field=f"streetNetwork[{road_index}].centerline[{segment_index}]")
            end = _plan_point(raw_end, field=f"streetNetwork[{road_index}].centerline[{segment_index + 1}]")
            width = finite_number(
                segment_widths[segment_index] if isinstance(segment_widths, list) else fallback_width,
                field=f"streetNetwork[{road_index}].segmentWidthsM[{segment_index}]",
            )
            width = max(0.1, min(nominal_width, width))
            margin = width / 2
            canonical = sorted((start, end))
            segment_id = "road-segment-" + content_fingerprint({
                "start": canonical[0],
                "end": canonical[1],
                "nominalWidthM": nominal_width,
                "effectiveWidthM": width,
            })[:24]
            min_x, max_x = min(start[0], end[0]) - margin, max(start[0], end[0]) + margin
            min_z, max_z = min(start[1], end[1]) - margin, max(start[1], end[1]) + margin
            for chunk_x in range(math.floor(min_x / chunk_size), math.floor(max_x / chunk_size) + 1):
                for chunk_z in range(math.floor(min_z / chunk_size), math.floor(max_z / chunk_size) + 1):
                    bounds = (
                        chunk_x * chunk_size - margin,
                        chunk_z * chunk_size - margin,
                        (chunk_x + 1) * chunk_size + margin,
                        (chunk_z + 1) * chunk_size + margin,
                    )
                    if not _segment_intersects_rectangle(start, end, bounds):
                        continue
                    key = f"{chunk_x}:0:{chunk_z}"
                    chunk(key)["streetSegments"].append({
                        "segmentId": segment_id,
                        "featureId": str(road.get("featureId") or ""),
                        "start": list(start),
                        "end": list(end),
                        "nominalWidthM": nominal_width,
                        "availableWidthM": width,
                        "effectiveWidthM": width,
                        "widthPolicy": str(road.get("widthPolicy") or "nominal-6m.v1"),
                    })

    items = []
    for key in sorted(chunks, key=lambda value: tuple(int(item) for item in value.split(":"))):
        value = chunks[key]
        for field in ("wallBlocks", "roofObjectRefs", "parcelGridRefs", "streetSegments"):
            unique = {content_fingerprint(item): item for item in value[field]}
            value[field] = [unique[item_key] for item_key in sorted(unique)]
        value["contentFingerprint"] = content_fingerprint(value)
        items.append(value)
    return {
        "schemaVersion": "vectoplan-editor-chunk-artifacts.v1",
        "itemCount": len(items),
        "items": items,
    }
