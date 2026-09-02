from __future__ import annotations

from collections.abc import Mapping
import copy
import math
import re
from typing import Any


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _cell(value: Any, *, field: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field} must contain exactly three integer coordinates")
    result = []
    for index, coordinate in enumerate(value):
        if isinstance(coordinate, bool):
            raise ValueError(f"{field}[{index}] must be an integer")
        try:
            number = float(coordinate)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field}[{index}] must be an integer") from exc
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"{field}[{index}] must be an integer")
        result.append(int(number))
    return result


def _roof(value: Any, *, building_id: str, index: int) -> dict[str, Any]:
    field = f"lod2Plan.buildings[{building_id}].roofs[{index}]"
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a WorldEdit roof object")
    roof = copy.deepcopy(dict(value))
    object_id = str(roof.get("objectInstanceId") or roof.get("object_instance_id") or "").strip()
    metadata = roof.get("metadata")
    footprint = roof.get("footprint")
    position = roof.get("position")
    dimensions = roof.get("dimensions")
    if (
        roof.get("type") != "PlaceObject"
        or roof.get("objectTypeId") != "building_roof"
        or not object_id
        or not isinstance(metadata, Mapping)
        or metadata.get("familyRef") != "world-edit.roof"
        or metadata.get("voxelOccupancy") != "none"
        or not isinstance(footprint, Mapping)
        or footprint.get("coordinateSpace") != "world-cell-xz"
        or not isinstance(footprint.get("coordinates"), list)
        or not isinstance(position, Mapping)
        or not isinstance(dimensions, Mapping)
    ):
        raise ValueError(f"{field} is not a canonical editable LoD2 roof")
    _cell(
        [position.get("x"), position.get("y"), position.get("z")],
        field=f"{field}.position",
    )
    for axis in ("x", "y", "z"):
        try:
            extent = float(dimensions.get(axis))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field}.dimensions.{axis} must be positive") from exc
        if not math.isfinite(extent) or extent <= 0:
            raise ValueError(f"{field}.dimensions.{axis} must be positive")
    return roof


def run(context: Mapping[str, Any], artifacts: Mapping[str, Any]) -> Mapping[str, Any]:
    del artifacts
    plan = context.get("lod2Plan")
    if not isinstance(plan, Mapping) or not isinstance(plan.get("buildings"), list):
        raise ValueError("lod2Plan.buildings must be an array")
    buildings = plan["buildings"]
    result = []
    building_ids: set[str] = set()
    roof_ids: set[str] = set()
    for building_index, raw in enumerate(buildings):
        if not isinstance(raw, Mapping):
            raise ValueError(f"lod2Plan.buildings[{building_index}] must be an object")
        building_id = str(raw.get("buildingId") or "").strip()
        if not building_id:
            raise ValueError(f"lod2Plan.buildings[{building_index}].buildingId is required")
        if building_id in building_ids:
            raise ValueError(f"Duplicate LoD2 buildingId: {building_id}")
        building_ids.add(building_id)
        source_tile = str(raw.get("sourceTile") or "").strip()
        source_sha256 = str(raw.get("sourceSha256") or "").strip().lower()
        if not source_tile or not SHA256_PATTERN.fullmatch(source_sha256):
            raise ValueError(f"LoD2 building {building_id} has incomplete source provenance")
        raw_walls = raw.get("wallCells")
        raw_roofs = raw.get("roofs")
        if not isinstance(raw_walls, list) or not raw_walls:
            raise ValueError(f"LoD2 building {building_id} has no whole wall cells")
        if not isinstance(raw_roofs, list) or not raw_roofs:
            raise ValueError(f"LoD2 building {building_id} has no editable roofs")
        walls = sorted({
            tuple(_cell(value, field=f"lod2Plan.buildings[{building_index}].wallCells[{index}]"))
            for index, value in enumerate(raw_walls)
        })
        roofs = [_roof(item, building_id=building_id, index=index) for index, item in enumerate(raw_roofs)]
        for roof in roofs:
            roof_id = str(roof.get("objectInstanceId") or roof.get("object_instance_id"))
            if roof_id in roof_ids:
                raise ValueError(f"Duplicate WorldEdit roof objectInstanceId: {roof_id}")
            roof_ids.add(roof_id)
        grid_value = raw.get("constructionGrid") or raw.get("parcelGrid")
        if not isinstance(grid_value, Mapping):
            raise ValueError(f"LoD2 building {building_id} has no construction-grid contract")
        grid = copy.deepcopy(dict(grid_value))
        if grid.get("schemaVersion") != "vectoplan-lod2-construction-grid.v1":
            raise ValueError(f"LoD2 building {building_id} uses an unsupported construction grid")
        if str(grid.get("buildingId") or building_id) != building_id:
            raise ValueError(f"LoD2 building {building_id} construction grid belongs to another building")
        result.append({
            "buildingId": building_id,
            "sourceTile": source_tile,
            "sourceSha256": source_sha256,
            "wallBlocks": {
                "blockTypeId": "lod2_exterior_wall",
                "cellSizeM": 1,
                "breakable": True,
                "cells": [list(item) for item in walls],
            },
            "worldEditRoofs": roofs,
            "facadeSegments": copy.deepcopy(list(raw.get("facadeSegments") or [])),
            "groundFootprints": copy.deepcopy(list(raw.get("groundFootprints") or [])),
            "constructionGrid": grid,
        })
    return {
        "schemaVersion": "vectoplan-editor-editable-buildings.v1",
        "items": sorted(result, key=lambda item: item["buildingId"]),
    }
