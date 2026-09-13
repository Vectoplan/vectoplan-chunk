"""Compact current-world geometry for Core's 3D -> 2D conversion.

No voxel history is expanded and no model data is written. Conversion to CAD
units, projection, floor filtering and drawing styles belong to vectoplan-core.
"""
from collections.abc import Mapping
from hashlib import sha256
import json
import time
from threading import RLock

from extensions import db
from models.object import WorldObjectInstance
from src.georeferencing.frame_contract import earth_grid_frame_contract

SCHEMA_VERSION = "vectoplan-plan-context.v1"
_overlay_cache = {}
_overlay_lock = RLock()


def compact_object(row):
    metadata = row.metadata_json if isinstance(row.metadata_json, Mapping) else {}
    parameters = metadata.get("roofParameters") or {}
    imported = parameters.get("importedSource") or {}
    return {
        "objectInstanceId": str(row.object_instance_id),
        "objectTypeId": str(row.object_type_id),
        "revision": int(row.revision or 0),
        "footprint": row.footprint_json or {},
        "metadata": {key: metadata[key] for key in (
            "label", "lod2BuildingId", "planningBuildAreaId", "baseY",
            "storeyCount", "storeyHeightMeters", "storeyProfile", "buildingLayout",
            "treeSource",
        ) if key in metadata},
        "groundFootprints": imported.get("groundFootprints") or [],
        # Preserve the already validated building datum for CAD's shared
        # parcel-grid kernel; no re-derivation from SVG or roof extents.
        "gridSource": {key: imported[key] for key in (
            "groundFootprints", "facadeSegments", "constructionGrid",
        ) if key in imported},
    }


def _context_overlays(world, provider):
    # Four regional source queries cover the same initial surroundings as the
    # 3D viewer without hundreds of per-16m-tile WFS requests or terrain work.
    reference = str(getattr(provider, "reference_fingerprint", ""))
    key = (int(world.id), reference, json.dumps(world.metadata_json or {}, sort_keys=True))
    with _overlay_lock:
        cached = _overlay_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    from src.geodata.visual_overlays import get_default_geodata_overlay_service
    from src.geodata.tree_instances import append_tree_overlay
    service = get_default_geodata_overlay_service()
    items, errors = [], []
    for x, z in ((-1, -1), (-1, 0), (0, -1), (0, 0)):
        chunk = {"chunkX": x, "chunkY": 0, "chunkZ": z, "chunkSize": 512}
        try:
            contract = service.chunk_contract(world=world, provider=provider,
                                              chunk_x=x, chunk_z=z, chunk_size=512,
                                              exclude_overlay_ids={"parcel-boundaries"})
            append_tree_overlay(contract, chunk=chunk, world=world, provider=provider)
            items.extend(contract.get("items") or [])
            errors.extend(contract.get("errors") or [])
        except Exception as exc:
            errors.append({"id": "geodata", "message": str(exc)[:200]})
    result = {"items": items, "errors": errors}
    with _overlay_lock:
        if len(_overlay_cache) >= 16:
            _overlay_cache.pop(next(iter(_overlay_cache)))
        _overlay_cache[key] = (time.monotonic() + 30, result)
    return result


def plan_context(world):
    # Avoid loading per-storey constructionCells (many MB) or eager ORM
    # relationships. Parents and roofs retain exact contour geometry.
    rows = db.session.query(
        WorldObjectInstance.object_instance_id, WorldObjectInstance.object_type_id,
        WorldObjectInstance.revision, WorldObjectInstance.footprint_json,
        WorldObjectInstance.metadata_json,
    ).filter(
        WorldObjectInstance.world_db_id == world.id,
        WorldObjectInstance.deleted_at.is_(None), WorldObjectInstance.status == "active",
        WorldObjectInstance.object_type_id.in_(("planning_build_area", "building_roof")),
    ).order_by(WorldObjectInstance.object_instance_id.asc()).all()
    objects = [compact_object(row) for row in rows]
    frame, overlays = None, {"items": [], "errors": []}
    if getattr(world, "is_earth_world", False):
        try:
            provider = world.build_earth_provider()
            frame = earth_grid_frame_contract(provider)
            overlays = _context_overlays(world, provider)
        except Exception as exc:
            overlays["errors"].append({"id": "geodata", "message": str(exc)[:200]})
    payload = {"schemaVersion": SCHEMA_VERSION, "coordinateFrame": frame,
               "cellSizeMeters": float(world.cell_size or 1), "objects": objects,
               "overlays": overlays["items"], "errors": overlays["errors"]}
    payload["revision"] = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                          default=str).encode()).hexdigest()
    return payload
