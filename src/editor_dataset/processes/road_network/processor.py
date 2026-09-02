from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from ...contracts import content_fingerprint, finite_number


NOMINAL_ROAD_WIDTH_M = 6.0
MINIMUM_ROAD_WIDTH_M = 0.1
BOUNDARY_BUCKET_SIZE_M = 8.0
RAY_EPSILON_M = 0.02


def _point(value: Any, *, field: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError(f"{field} must contain x and z")
    try:
        point = [float(value[0]), float(value[1])]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must contain finite x and z") from exc
    if not all(math.isfinite(item) for item in point):
        raise ValueError(f"{field} must contain finite x and z")
    return [round(item, 4) for item in point]


def _width(value: Any, *, field: str) -> float:
    return max(MINIMUM_ROAD_WIDTH_M, min(64.0, finite_number(value, field=field)))


def _optional_width(value: Any, *, field: str) -> float | None:
    if value is None or value == "":
        return None
    return _width(value, field=field)


def _boundary_segments(
    value: Any,
    bounds: list[float],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("roadSurfaceBoundaries must be an array when supplied")
    result: dict[tuple[tuple[int, int], tuple[int, int]], tuple[tuple[float, float], tuple[float, float]]] = {}
    for line_index, raw_line in enumerate(value):
        if not isinstance(raw_line, list):
            raise ValueError(f"roadSurfaceBoundaries[{line_index}] must be an array")
        points = [
            _point(raw, field=f"roadSurfaceBoundaries[{line_index}][{point_index}]")
            for point_index, raw in enumerate(raw_line)
        ]
        for point in points:
            if (
                point[0] < bounds[0] - 0.001
                or point[0] > bounds[2] + 0.001
                or point[1] < bounds[1] - 0.001
                or point[1] > bounds[3] + 0.001
            ):
                raise ValueError(f"roadSurfaceBoundaries[{line_index}] leaves the declared sourceBounds")
        for start, end in zip(points, points[1:]):
            if start == end:
                continue
            first = (round(start[0] * 1000), round(start[1] * 1000))
            second = (round(end[0] * 1000), round(end[1] * 1000))
            key = tuple(sorted((first, second)))
            result[key] = ((start[0], start[1]), (end[0], end[1]))
    return [result[key] for key in sorted(result)]


def _boundary_index(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> dict[tuple[int, int], list[tuple[tuple[float, float], tuple[float, float]]]]:
    result: dict[tuple[int, int], list[tuple[tuple[float, float], tuple[float, float]]]] = {}
    for segment in segments:
        start, end = segment
        min_x, max_x = sorted((start[0], end[0]))
        min_z, max_z = sorted((start[1], end[1]))
        for bucket_x in range(math.floor(min_x / BOUNDARY_BUCKET_SIZE_M), math.floor(max_x / BOUNDARY_BUCKET_SIZE_M) + 1):
            for bucket_z in range(math.floor(min_z / BOUNDARY_BUCKET_SIZE_M), math.floor(max_z / BOUNDARY_BUCKET_SIZE_M) + 1):
                result.setdefault((bucket_x, bucket_z), []).append(segment)
    return result


def _nearby_boundaries(
    index: Mapping[tuple[int, int], list[tuple[tuple[float, float], tuple[float, float]]]],
    point: tuple[float, float],
    radius: float,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    found: dict[tuple[tuple[float, float], tuple[float, float]], None] = {}
    for bucket_x in range(
        math.floor((point[0] - radius) / BOUNDARY_BUCKET_SIZE_M),
        math.floor((point[0] + radius) / BOUNDARY_BUCKET_SIZE_M) + 1,
    ):
        for bucket_z in range(
            math.floor((point[1] - radius) / BOUNDARY_BUCKET_SIZE_M),
            math.floor((point[1] + radius) / BOUNDARY_BUCKET_SIZE_M) + 1,
        ):
            for segment in index.get((bucket_x, bucket_z), []):
                found[segment] = None
    return list(found)


def _ray_segment_distance(
    origin: tuple[float, float],
    direction: tuple[float, float],
    segment: tuple[tuple[float, float], tuple[float, float]],
    maximum: float,
) -> float | None:
    start, end = segment
    segment_x = end[0] - start[0]
    segment_z = end[1] - start[1]
    denominator = direction[0] * segment_z - direction[1] * segment_x
    if abs(denominator) <= 1e-9:
        return None
    offset_x = start[0] - origin[0]
    offset_z = start[1] - origin[1]
    distance = (offset_x * segment_z - offset_z * segment_x) / denominator
    factor = (offset_x * direction[1] - offset_z * direction[0]) / denominator
    if distance <= RAY_EPSILON_M or distance > maximum + 1e-6 or factor < -1e-6 or factor > 1.0 + 1e-6:
        return None
    return distance


def _effective_segment_width(
    start: list[float],
    end: list[float],
    boundaries: Mapping[tuple[int, int], list[tuple[tuple[float, float], tuple[float, float]]]],
    maximum_width: float,
) -> float:
    dx = end[0] - start[0]
    dz = end[1] - start[1]
    length = math.hypot(dx, dz)
    if length <= 1e-9 or not boundaries:
        return maximum_width
    normal = (-dz / length, dx / length)
    maximum_half_width = maximum_width / 2.0
    constrained_half_width = maximum_half_width
    for factor in (0.2, 0.5, 0.8):
        point = (start[0] + dx * factor, start[1] + dz * factor)
        candidates = _nearby_boundaries(boundaries, point, maximum_half_width)
        for direction in (normal, (-normal[0], -normal[1])):
            distances = [
                distance
                for segment in candidates
                if (distance := _ray_segment_distance(point, direction, segment, maximum_half_width)) is not None
            ]
            if distances:
                constrained_half_width = min(constrained_half_width, min(distances))
    return round(max(MINIMUM_ROAD_WIDTH_M, min(maximum_width, constrained_half_width * 2.0)), 4)


def run(context: Mapping[str, Any], artifacts: Mapping[str, Any]) -> Mapping[str, Any]:
    del artifacts
    if context.get("defaultRoadWidthM") is not None:
        _width(context.get("defaultRoadWidthM"), field="defaultRoadWidthM")
    nominal_width = NOMINAL_ROAD_WIDTH_M
    raw_bounds = context.get("sourceBounds")
    if not isinstance(raw_bounds, list) or len(raw_bounds) != 4:
        raise ValueError("sourceBounds is required for the road-network process")
    bounds = [finite_number(value, field=f"sourceBounds[{index}]") for index, value in enumerate(raw_bounds)]
    boundary_index = _boundary_index(_boundary_segments(context.get("roadSurfaceBoundaries"), bounds))
    raw_features = context.get("roadFeatures")
    if not isinstance(raw_features, list):
        raise ValueError("roadFeatures must be a spatially bounded array")
    normalized: dict[tuple[str, tuple[tuple[int, int], ...]], dict[str, Any]] = {}
    for index, raw in enumerate(raw_features):
        if not isinstance(raw, Mapping):
            raise ValueError(f"roadFeatures[{index}] must be an object")
        raw_centerline = raw.get("centerline")
        if not isinstance(raw_centerline, list):
            raise ValueError(f"roadFeatures[{index}].centerline must be an array")
        centerline = [
            _point(value, field=f"roadFeatures[{index}].centerline[{point_index}]")
            for point_index, value in enumerate(raw_centerline)
        ]
        deduplicated = []
        for point in centerline:
            if not deduplicated or point != deduplicated[-1]:
                deduplicated.append(point)
        centerline = deduplicated
        if len(centerline) < 2:
            raise ValueError(f"roadFeatures[{index}] has fewer than two distinct points")
        for point in centerline:
            if (
                point[0] < float(bounds[0]) - 0.001
                or point[0] > float(bounds[2]) + 0.001
                or point[1] < float(bounds[1]) - 0.001
                or point[1] > float(bounds[3]) + 0.001
            ):
                raise ValueError(f"roadFeatures[{index}] leaves the declared sourceBounds")
        key = tuple((round(point[0] * 1000), round(point[1] * 1000)) for point in centerline)
        reverse = tuple(reversed(key))
        identity = min(key, reverse)
        if identity == reverse:
            centerline = list(reversed(centerline))
        dataset_id = str(raw.get("sourceDataset") or "strassendaten").strip() or "strassendaten"
        normalized_key = (dataset_id, identity)
        if raw.get("nominalWidthM") is not None:
            _width(raw.get("nominalWidthM"), field=f"roadFeatures[{index}].nominalWidthM")
        supplied_available_width = next(
            (
                _optional_width(raw.get(field), field=f"roadFeatures[{index}].{field}")
                for field in ("availableWidthM", "roadSurfaceWidthM", "parcelWidthM")
                if raw.get(field) is not None
            ),
            None,
        )
        maximum_width = min(nominal_width, supplied_available_width or nominal_width)
        segment_widths = [
            _effective_segment_width(start, end, boundary_index, maximum_width)
            for start, end in zip(centerline, centerline[1:])
        ]
        effective_width = min(segment_widths, default=maximum_width)
        available_width = min(
            [value for value in (supplied_available_width, effective_width) if value is not None],
            default=nominal_width,
        )
        existing = normalized.get(normalized_key)
        if existing is not None:
            existing["segmentWidthsM"] = [
                min(first, second)
                for first, second in zip(existing["segmentWidthsM"], segment_widths)
            ]
            existing["effectiveWidthM"] = min(existing["segmentWidthsM"])
            existing["availableWidthM"] = min(existing["availableWidthM"], available_width)
            continue
        stable_id = "road-" + content_fingerprint({
            "sourceDataset": dataset_id,
            "centerlineMillimetres": identity,
        })[:24]
        normalized[normalized_key] = {
            "featureId": stable_id,
            "sourceDataset": dataset_id,
            "centerline": centerline,
            "nominalWidthM": nominal_width,
            "availableWidthM": available_width,
            "effectiveWidthM": effective_width,
            "segmentWidthsM": segment_widths,
            "widthPolicy": "nominal-6m-clamped-to-road-parcel-surface.v1",
            "surface": "road-placeholder",
            "classification": "street-network",
        }
    result = list(normalized.values())
    return {
        "schemaVersion": "vectoplan-editor-street-network.v2",
        "items": sorted(result, key=lambda item: item["featureId"]),
    }
