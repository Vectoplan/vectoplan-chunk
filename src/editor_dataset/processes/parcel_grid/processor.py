from __future__ import annotations

from collections.abc import Mapping
import copy
import math
from typing import Any


def _vector(value: Any, *, field: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field} must contain two finite coordinates")
    try:
        result = [float(value[0]), float(value[1])]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must contain two finite coordinates") from exc
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{field} must contain two finite coordinates")
    return result


def run(context: Mapping[str, Any], artifacts: Mapping[str, Any]) -> Mapping[str, Any]:
    supplied = context.get("parcelGrids")
    if supplied is not None and not isinstance(supplied, list):
        raise ValueError("parcelGrids must be an array when supplied")
    supplied_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(supplied or []):
        if not isinstance(item, Mapping):
            raise ValueError(f"parcelGrids[{index}] must be an object")
        building_id = str(item.get("buildingId") or "").strip()
        if not building_id or building_id in supplied_by_id:
            raise ValueError(f"parcelGrids[{index}] has a missing or duplicate buildingId")
        supplied_by_id[building_id] = copy.deepcopy(dict(item))
    buildings = artifacts.get("lod2-editable", {}).get("items", [])
    result: list[dict[str, Any]] = []
    for building in buildings if isinstance(buildings, list) else []:
        if not isinstance(building, Mapping):
            continue
        building_id = str(building.get("buildingId") or "")
        source_grid = supplied_by_id.get(building_id) or building.get("constructionGrid")
        if not isinstance(source_grid, Mapping):
            raise ValueError(f"Editable building {building_id} has no construction grid")
        grid = copy.deepcopy(dict(source_grid))
        if grid.get("schemaVersion") != "vectoplan-lod2-construction-grid.v1":
            raise ValueError(f"Editable building {building_id} has an unsupported construction grid")
        if str(grid.get("buildingId") or building_id) != building_id:
            raise ValueError(f"Construction grid for {building_id} belongs to another building")
        origin = _vector(grid.get("origin"), field=f"constructionGrid[{building_id}].origin")
        axis_u = _vector(grid.get("axisU"), field=f"constructionGrid[{building_id}].axisU")
        axis_v = _vector(grid.get("axisV"), field=f"constructionGrid[{building_id}].axisV")
        determinant = axis_u[0] * axis_v[1] - axis_u[1] * axis_v[0]
        if abs(determinant) < 0.5:
            raise ValueError(f"Construction grid for {building_id} has degenerate axes")
        try:
            width = float(grid.get("widthM"))
            depth = float(grid.get("depthM"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Construction grid for {building_id} has invalid dimensions") from exc
        if not math.isfinite(width) or not math.isfinite(depth) or width <= 0 or depth <= 0:
            raise ValueError(f"Construction grid for {building_id} has invalid dimensions")
        result.append({
            "buildingId": building_id,
            "alignmentMode": str(grid.get("alignmentMode") or grid.get("referenceMode") or "existing-building"),
            "gridVersion": str(grid.get("gridVersion") or grid.get("schemaVersion") or "building-aligned-parcel-grid.v1"),
            "origin": origin,
            "axisU": axis_u,
            "axisV": axis_v,
            "widthM": width,
            "depthM": depth,
            "lines": copy.deepcopy(list(grid.get("lines") or [])),
            "wallBoundaryCells": copy.deepcopy(list(grid.get("wallBoundaryCells") or [])),
            "metadata": {
                **copy.deepcopy(dict(grid.get("metadata") or {})),
                "fingerprint": grid.get("fingerprint"),
                "uAnchors": copy.deepcopy(list(grid.get("uAnchors") or [])),
                "vAnchors": copy.deepcopy(list(grid.get("vAnchors") or [])),
                "facades": copy.deepcopy(list(grid.get("facades") or [])),
                "partitionPolicy": copy.deepcopy(dict(grid.get("partitionPolicy") or {})),
                "provenance": copy.deepcopy(dict(grid.get("provenance") or {})),
            },
            "contract": grid,
        })
    unused = set(supplied_by_id) - {
        str(item.get("buildingId") or "") for item in result
    }
    if unused:
        raise ValueError(f"parcelGrids references unknown buildings: {sorted(unused)}")
    return {
        "schemaVersion": "vectoplan-editor-parcel-grids.v1",
        "items": sorted(result, key=lambda item: item["buildingId"]),
    }
