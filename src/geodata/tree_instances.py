"""Approved tree points as read-only scene instances; removals are real objects.

GET never imports database objects or alters terrain. The first RemoveObject
validates its source point and creates a metadata-only object in that command's
transaction. Its soft-deleted row is a durable, release-independent tombstone.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import math
from threading import RLock
import time
from urllib.parse import urlencode

DATASET_ID = "baumkataster"
SCHEMA_VERSION = "vectoplan-tree-instances.v1"


def tree_render_profile(tree_id: str, species: str) -> dict:
    """Stable appearance only; never change surveyed position or dimensions."""
    label = species.casefold()
    form = "conifer" if any(name in label for name in ("pinus", "picea", "abies", "kiefer", "fichte", "tanne", "cedrus")) else "broadleaf"
    if any(name in label for name in ("fastigiata", "italica", "säulen", "saeulen")):
        form = "columnar"
    seed = int.from_bytes(sha256(tree_id.encode()).digest()[:4], "big") / 2**32
    return {"schemaVersion": "vectoplan-tree-appearance.v1", "crownForm": form,
            "variation": round(seed, 6), "crownRatio": .72 if form == "conifer" else .62}


def tree_object_id(tree_id: str) -> str:
    return "tree_" + sha256((DATASET_ID + ":" + tree_id).encode("utf-8")).hexdigest()[:40]


def tree_yaw(tree_id: str) -> float:
    return int.from_bytes(sha256(tree_id.encode("utf-8")).digest()[:4], "big") / 2**32 * math.tau


def terrain_height(chunk: Mapping, x: float, z: float, *, fallback: float) -> float:
    """Use the exact two terrain triangles, rather than bilinear interpolation."""
    size = int(chunk.get("chunkSize", 16))
    local_x = x - int(chunk.get("chunkX", 0)) * size
    local_z = z - int(chunk.get("chunkZ", 0)) * size
    shape = (chunk.get("metadata") or {}).get("terrainSurface") or {}
    heights = shape.get("cornerHeights")
    if not isinstance(heights, list) or len(heights) != (size + 1) ** 2:
        return fallback
    ix, iz = min(size - 1, max(0, math.floor(local_x))), min(size - 1, max(0, math.floor(local_z)))
    u, v = min(1, max(0, local_x - ix)), min(1, max(0, local_z - iz))
    a, b = float(heights[ix + (size + 1) * iz]), float(heights[ix + 1 + (size + 1) * iz])
    c, d = float(heights[ix + 1 + (size + 1) * (iz + 1)]), float(heights[ix + (size + 1) * (iz + 1)])
    return a + (b - a) * u + (c - b) * v if u >= v else a + (c - d) * u + (d - a) * v


def _bounded_dimension(item: Mapping, key: str, default: float, maximum: float) -> float:
    try:
        value = float(item.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and 0 < value <= maximum else default


class TreePointService:
    def __init__(self, client):
        self.client = client
        self._tiles = {}
        self._lock = RLock()
        self._unavailable_until = 0.0

    def points(self, bbox: tuple[float, float, float, float]) -> tuple[str, list[dict]]:
        with self._lock:
            if self._unavailable_until > time.monotonic():
                return "", []
            try:
                publication = self.client.approved_publication(DATASET_ID)
            except Exception:
                self._unavailable_until = time.monotonic() + 30
                return "", []
            release = str(publication["release_key"])
            key = (release, bbox)
            if key in self._tiles:
                return release, self._tiles[key]
            items, offset = [], 0
            while True:
                try:
                    payload = self.client._request("/admin/api/production-publications/baumkataster/tree-points?" + urlencode({
                        "bbox": ",".join(f"{value:.10f}" for value in bbox),
                        "release_key": release, "limit": 1000, "offset": offset,
                    }))
                except Exception:
                    # A preparing index or unavailable optional source must not
                    # serially stall hundreds of editable terrain requests.
                    self._unavailable_until = time.monotonic() + 30
                    raise
                if payload.get("schemaVersion") != "vectoplan-tree-points.v1" or payload.get("release_key") != release:
                    raise ValueError("Tree points do not match the approved publication")
                page = payload.get("items")
                if not isinstance(page, list):
                    raise ValueError("Tree publication has no point array")
                items.extend(page)
                next_offset = payload.get("nextOffset")
                if next_offset is None:
                    break
                if not isinstance(next_offset, int) or next_offset <= offset or len(items) >= 10000:
                    raise ValueError("Tree query exceeds the bounded chunk window")
                offset = next_offset
            if len(self._tiles) >= 1024:
                self._tiles.pop(next(iter(self._tiles)))
            self._tiles[key] = items
            return release, items


_SERVICE = None


def get_tree_service() -> TreePointService:
    global _SERVICE
    if _SERVICE is None:
        from src.geodata.visual_overlays import get_default_geodata_overlay_service
        _SERVICE = TreePointService(get_default_geodata_overlay_service().orchestrator)
    return _SERVICE


def _removed_ids(world, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    from models.object import WorldObjectInstance
    # Scalar projection avoids the production model's eager relationship graph.
    return {row[0] for row in WorldObjectInstance.query.with_entities(WorldObjectInstance.object_instance_id).filter(
        WorldObjectInstance.world_db_id == world.id,
        WorldObjectInstance.object_type_id == "source_tree",
        WorldObjectInstance.object_instance_id.in_(ids),
        WorldObjectInstance.deleted_at.is_not(None),
    ).all()}


def tree_features(*, world, provider, chunk: Mapping, points: list[Mapping], removed: set[str]) -> list[dict]:
    from src.geodata.visual_overlays import _wgs84_to_local
    size = int(chunk.get("chunkSize", 16))
    cx, cy, cz = (int(chunk.get(key, 0)) for key in ("chunkX", "chunkY", "chunkZ"))
    features = []
    for item in points:
        tree_id = str(item.get("id") or "")
        if not tree_id:
            continue
        object_id = tree_object_id(tree_id)
        if object_id in removed:
            continue
        try:
            lon, lat = float(item["longitude"]), float(item["latitude"])
            x, z = _wgs84_to_local(provider, lon, lat)
            if not all(math.isfinite(value) for value in (lon, lat, x, z)):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        # Half-open ownership: border points belong to one column only.
        if math.floor(x / size) != cx or math.floor(z / size) != cz:
            continue
        y = terrain_height(chunk, x, z, fallback=float(getattr(world, "surface_y", 0)) + 1)
        # The supporting terrain layer owns the instance. An exact upper face
        # at y=16 belongs to layer zero, which is what surface streaming loads.
        if math.floor((y - 1e-6) / size) != cy:
            continue
        features.append({
            "id": tree_id, "objectInstanceId": object_id,
            "position": [round(x, 6), round(y, 6), round(z, 6)],
            "heightM": _bounded_dimension(item, "heightM", 8, 100),
            "crownDiameterM": _bounded_dimension(item, "crownDiameterM", 5, 60),
            "trunkDiameterM": _bounded_dimension(item, "trunkDiameterM", .3, 10),
            "species": str(item.get("species") or ""), "yawRadians": tree_yaw(tree_id),
            "appearance": tree_render_profile(tree_id, str(item.get("species") or "")),
            "source": {"treeId": tree_id, "longitude": lon, "latitude": lat},
        })
    return features


def append_tree_overlay(contract: dict, *, chunk: Mapping, world, provider) -> None:
    from src.geodata.visual_overlays import _local_to_wgs84
    config = (getattr(world, "metadata_json", None) or {}).get("treeInstances")
    if contract.get("status") == "disabled" or (isinstance(config, Mapping) and config.get("enabled") is False):
        return
    size = int(chunk.get("chunkSize", 16))
    x, z = int(chunk.get("chunkX", 0)) * size, int(chunk.get("chunkZ", 0)) * size
    corners = [_local_to_wgs84(provider, a, b) for a, b in ((x, z), (x + size, z), (x, z + size), (x + size, z + size))]
    bbox = (min(p[0] for p in corners), min(p[1] for p in corners), max(p[0] for p in corners), max(p[1] for p in corners))
    try:
        release, points = get_tree_service().points(bbox)
        if not release:
            contract.setdefault("availability", []).append({"id": DATASET_ID, "kind": "vegetation", "status": "unavailable"})
            return
        removed = _removed_ids(world, [tree_object_id(str(item["id"])) for item in points if item.get("id")])
        features = tree_features(world=world, provider=provider, chunk=chunk, points=points, removed=removed)
        contract.setdefault("items", []).append({
            "id": DATASET_ID, "datasetId": DATASET_ID, "label": "Baumkataster",
            "schemaVersion": SCHEMA_VERSION, "renderMode": "tree-instances", "semanticRole": "vegetation",
            "releaseKey": release, "tileKey": f"{x // size}:{z // size}", "classificationSource": False,
            "geometry": {"type": "TreeInstances", "dimensions": "world-xyz", "features": features},
            "source": {"kind": "approved-tree-points", "heightReference": "terrain-surface", "datasetId": DATASET_ID},
        })
        contract.setdefault("availability", []).append({"id": DATASET_ID, "kind": "vegetation", "status": "available"})
    except Exception as exc:
        contract.setdefault("errors", []).append({"id": DATASET_ID, "datasetId": DATASET_ID, "message": str(exc)[:300]})


def materialize_tree_for_removal(*, world, payload: Mapping, command_log, user_id, session_id):
    """Validate a real source point before joining canonical RemoveObject logic."""
    from extensions import db
    from models.object import WorldObjectInstance, WorldObjectChunkRef
    from sqlalchemy.orm import noload
    from src.geodata.visual_overlays import _wgs84_to_local
    from src.world.earth.terrain_pipeline import generate_earth_terrain_chunk
    descriptor = payload.get("treeSource")
    if not isinstance(descriptor, Mapping):
        return None
    if not world.is_earth_world:
        raise ValueError("Source trees require an Earth world")
    tree_id = str(descriptor.get("treeId") or "")
    object_id = str(payload.get("objectInstanceId") or "")
    if not tree_id or len(tree_id) > 1000 or object_id != tree_object_id(tree_id):
        raise ValueError("Invalid source tree identity")
    existing = WorldObjectInstance.query.options(noload("*")).filter(
        WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.object_instance_id == object_id,
        WorldObjectInstance.object_type_id == "source_tree",
    ).one_or_none()
    if existing is not None:
        return existing
    lon, lat = float(descriptor.get("longitude")), float(descriptor.get("latitude"))
    if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 < lat < 90:
        raise ValueError("Invalid source tree coordinate")
    release, points = get_tree_service().points((lon - .000001, lat - .000001, lon + .000001, lat + .000001))
    point = next((item for item in points if item.get("id") == tree_id), None)
    if not release or point is None:
        raise ValueError("Source tree is not present in the approved Baumkataster")
    provider, size = world.build_earth_provider(), int(world.chunk_size)
    x, z = _wgs84_to_local(provider, float(point["longitude"]), float(point["latitude"]))
    cx, cz = math.floor(x / size), math.floor(z / size)
    chunk = generate_earth_terrain_chunk(world=world, provider=provider, chunk_x=cx, chunk_y=0, chunk_z=cz)
    y = terrain_height(chunk, x, z, fallback=float(world.surface_y or 0) + 1)
    cy = math.floor((y - 1e-6) / size)
    key = f"{cx}:{cy}:{cz}"
    instance = WorldObjectInstance.create_for_world(world, object_instance_id=object_id,
        object_type_id="source_tree", object_source="importer", object_kind="imported_object",
        anchor_x=math.floor(x), anchor_y=math.floor(y), anchor_z=math.floor(z),
        occupied_cells_json=[], touched_chunks_json=[key], primary_chunk_x=cx, primary_chunk_y=cy,
        primary_chunk_z=cz, primary_chunk_key=key, created_by_command_id=command_log.command_id,
        created_by_user_id=user_id, updated_by_user_id=user_id, last_session_id=session_id,
        metadata_json={"voxelOccupancy": "none", "treeSource": {**dict(point), "datasetId": DATASET_ID,
                       "releaseKey": release}, "schemaVersion": SCHEMA_VERSION})
    db.session.add(instance)
    db.session.flush()
    db.session.add(WorldObjectChunkRef.create_for_object(instance, chunk_x=cx, chunk_y=cy, chunk_z=cz,
                   occupied_cells_json=[], metadata_json={"voxelOccupancy": "none"}))
    db.session.flush()
    return instance
