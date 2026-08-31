"""Response-only CityGML meshes for explicitly enabled Earth project worlds."""
from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import math
import os
from pathlib import Path

from pyproj import Transformer

from src.geodata.citygml_store import SOURCE_CRS, query_buildings
from src.georeferencing.frame_contract import earth_grid_frame_contract
from src.geodata.lod2_sources import sources_for_world, source_availability, transformers, vertical_metres_per_unit

OVERLAY_ID = "berlin-lod2-buildings"
UTM_TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
WGS84_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)


def store_path() -> Path:
    return Path(os.getenv("VECTOPLAN_CHUNK_LOD2_STORE", "/var/lib/vectoplan-chunk/terrain-cache/lod2/berlin.sqlite3"))


def world_lod2_config(world) -> Mapping | None:
    metadata = getattr(world, "metadata_json", None)
    config = metadata.get("lod2Buildings") if isinstance(metadata, Mapping) else None
    if isinstance(config, Mapping):
        return config if config.get("enabled") is not False else None
    # Project-independent read-only discovery. Never imports cells on a GET.
    return {"enabled": True, "allowFlatTerrainAlignment": True, "mode": "available-sources"} if bool(getattr(world, "is_earth_world", False)) else None


def _world_xz(frame, lon, lat):
    width = frame["worldWidthCells"]
    delta = (lon - frame["centralMeridianDegrees"] + 180) % 360 - 180
    x = (delta * width / 360 - frame["storageOrigin"]["x"] + width / 2) % width - width / 2
    z = lat * frame["worldHeightCells"] / 180 - frame["storageOrigin"]["z"]
    return x, z


def _utm_bbox(frame, cx, cz, size):
    eastings, northings = [], []
    # Densify the edges so curved CRS transformations cannot miss edge features.
    for t in (0, .25, .5, .75, 1):
        for x, z in ((cx * size + t * size, cz * size), (cx * size + t * size, (cz + 1) * size),
                     (cx * size, cz * size + t * size), ((cx + 1) * size, cz * size + t * size)):
            lon = (x + frame["storageOrigin"]["x"]) * 360 / frame["worldWidthCells"] + frame["centralMeridianDegrees"]
            lat = (z + frame["storageOrigin"]["z"]) * 180 / frame["worldHeightCells"]
            e, n = WGS84_TO_UTM.transform(lon, lat)
            eastings.append(e)
            northings.append(n)
    return min(eastings) - .05, min(northings) - .05, max(eastings) + .05, max(northings) + .05


@lru_cache(maxsize=512)
def _native_tile(path: str, generation: tuple, bbox: tuple):
    # A partially downloaded region must still expose the data that is present.
    return query_buildings(Path(path), bbox, require_coverage=False)


def building_overlay_item(*, world, provider, chunk: Mapping, include_materialized: bool = False, source=None) -> dict | None:
    config = world_lod2_config(world)
    if config is None:
        return None
    frame = earth_grid_frame_contract(provider)
    if frame is None or frame["metersPerCell"] != 1:
        raise ValueError("LoD2 requires the current one-metre Earth/DGM frame")
    if source is None:
        sources = sources_for_world(config)
        if not sources:
            return None
        source = sources[0]
    terrain = chunk.get("terrain") or (chunk.get("stats") or {}).get("terrain") or {}
    # Existing snapshots can omit generator metadata. Only accept an explicit
    # project anchor with the matching immutable reference as their fallback.
    anchor = terrain.get("anchorElevationM")
    if anchor is None and config.get("referenceFingerprint") == provider.reference_fingerprint:
        anchor = config.get("anchorElevationM")
    ground_relative = (terrain.get("fallback") is True or anchor is None) and config.get("allowFlatTerrainAlignment") is True
    if not ground_relative and (anchor is None or not math.isfinite(float(anchor))):
        raise ValueError("LoD2 is waiting for the project's DGM height anchor")
    if terrain.get("fallback") is True and not ground_relative:
        raise ValueError("LoD2 hidden while terrain uses a flat fallback")
    cx, cz, size = int(chunk.get("chunkX", 0)), int(chunk.get("chunkZ", 0)), int(chunk.get("chunkSize", 16))
    status, bbox, version = source_availability(source, frame, cx, cz, size)
    if status != "available":
        return None
    if not ground_relative and source.vertical_crs != str(terrain.get("verticalCrs", "EPSG:7837")):
        raise ValueError("LoD2 vertical reference does not match the terrain; no height is guessed")
    revision, native = _native_tile(str(source.path), version, bbox)
    _, to_wgs84 = transformers(source.horizontal_crs)
    vertical_scale = vertical_metres_per_unit(source.vertical_crs)
    features = []
    for feature in native:
        if source.id != OVERLAY_ID:
            feature = {**feature, "id": f"{source.id}:{feature['id']}", "sourceObjectId": feature["id"]}
        # Persisted import receipts are tombstones too: deleting a wall/roof must
        # not reveal its old decorative source mesh or re-import the building.
        if not include_materialized and feature["id"] in config.get("materializedBuildings", {}):
            continue
        ground = [point[2] for polygon in feature["polygons"] if polygon["surface"] == "GroundSurface"
                  for ring in polygon["rings"] for point in ring]
        if ground_relative and not ground and config.get("mode") == "available-sources":
            # A roof-only dataset provides no defensible ground alignment.
            continue
        base = min(ground or [point[2] for polygon in feature["polygons"] for ring in polygon["rings"] for point in ring]) * vertical_scale
        height_anchor = base if ground_relative else float(anchor)
        polygons = []
        for polygon in feature["polygons"]:
            rings = []
            for ring in polygon["rings"]:
                lons, lats = to_wgs84.transform([p[0] for p in ring], [p[1] for p in ring])
                converted = []
                for point, lon, lat in zip(ring, lons, lats, strict=True):
                    if not math.isfinite(lon) or not math.isfinite(lat) or not -90 <= lat <= 90:
                        raise ValueError("Source coordinate cannot be transformed into the Earth frame")
                    x, z = _world_xz(frame, lon, lat)
                    # DGM stores the top solid cell at round(h-anchor+surface_y).
                    # Its visible upper face is one cell higher. Do not interpret
                    # DHHN2016 normal heights as WGS84 ellipsoidal heights.
                    y = point[2]*vertical_scale - height_anchor + float(getattr(world, "surface_y", 0)) + 1
                    converted.append([round(x, 4), round(y, 4), round(z, 4)])
                rings.append(converted)
            polygons.append({"surface": polygon["surface"], "rings": rings})
        features.append({**feature, "baseElevationM": base, "polygons": polygons})
    return {
        "id": source.id, "datasetId": "3d-gebaeudedaten", "label": source.label,
        "renderMode": "building-meshes", "semanticRole": "building-reference",
        "classificationSource": False, "releaseKey": revision, "tileKey": f"{cx}:{cz}",
        "source": {"kind": "citygml-derived-store", "crs": source.source_crs, "horizontalCrs": source.horizontal_crs,
                   "verticalCrs": source.vertical_crs, "lod": 2, "license": source.license,
                   "coveragePolicy": "available-features-only", "sourceId": source.id},
        "heightReference": {"kind": "source-ground-relative-to-flat-terrain" if ground_relative else "project-reference-dgm-height",
                            "anchorElevationM": None if ground_relative else float(anchor),
                            "absoluteHeightPreserved": not ground_relative,
                            "surfaceY": float(getattr(world, "surface_y", 0)),
                            "note": "No released DGM; each complete building is aligned to the flat test terrain." if ground_relative else None,
                            "surfaceFaceOffset": 1, "terrainReleaseKey": terrain.get("releaseKey")},
        "geometry": {"type": "BuildingMultiSurface", "dimensions": "world-xyz", "features": features},
        "stats": {"buildingCount": len(features), "polygonCount": sum(len(f["polygons"]) for f in features)},
    }


def append_building_overlay(contract: dict, *, chunk: Mapping, world, provider) -> None:
    if world_lod2_config(world) is None or contract.get("status") == "disabled":
        return
    try:
        sources = sources_for_world(world_lod2_config(world))
    except Exception as exc:
        contract["status"] = "degraded"
        contract.setdefault("errors", []).append({"id": OVERLAY_ID, "datasetId": "3d-gebaeudedaten",
                                                 "message": f"{type(exc).__name__}: {exc}"[:500]})
        return
    for source in sources:
        availability = {"id": source.id, "kind": "lod2", "status": "unavailable"}
        contract.setdefault("availability", []).append(availability)
        try:
            frame = earth_grid_frame_contract(provider)
            if frame is None:
                availability["status"] = "missing-project-reference"
                continue
            status, _, _ = source_availability(source, frame, int(chunk.get("chunkX", 0)), int(chunk.get("chunkZ", 0)), int(chunk.get("chunkSize", 16)))
            availability["status"] = status
            if status != "available":
                continue
            item = building_overlay_item(world=world, provider=provider, chunk=chunk, source=source)
            if item is not None:
                if item["geometry"]["features"]:
                    contract.setdefault("items", []).append(item)
                else:
                    availability["status"] = "no-visible-features"
        except Exception as exc:
            availability["status"] = "source-error"
            contract["status"] = "degraded"
            contract.setdefault("errors", []).append({"id": source.id, "datasetId": "3d-gebaeudedaten",
                "message": f"{type(exc).__name__}: {exc}"[:500]})
