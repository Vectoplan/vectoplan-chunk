"""Response-only discovery of persisted structures above the Earth height field.

Snapshots remain the load truth. A roof is loaded from its primary chunk even
when only its far end intersects a visible column. No cells/refs are duplicated
or materialized by reading these hints. Queries are world-scoped and batched.

The project map deliberately uses a second, lightweight projection of the same
persisted roof objects.  Loading a several-megabyte anchor chunk just to draw a
few roof polygons made map completeness depend on the 3D streaming queue.  The
preview below contains geometry only and never becomes another write truth.
"""
from collections import defaultdict
from collections.abc import Mapping
from hashlib import sha256
import json

from sqlalchemy import or_, tuple_

from extensions import db
from models.chunk import ChunkSnapshot
from models.object import WorldObjectInstance

SCHEMA_VERSION = "structure-streaming.v1"
MAP_SCHEMA_VERSION = "vectoplan-map-structures.v1"

_MAX_MAP_ROOFS = 4096
_MAX_MAP_FACES = 16384
_MAX_MAP_POINTS = 2048


def _mapping(value):
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _same_point(first, second):
    return all(abs(float(first[index]) - float(second[index])) < 1e-7 for index in range(3))


def _open_points(points):
    if len(points) > 1 and _same_point(points[0], points[-1]):
        return points[:-1]
    return points


def _roof_map_feature(row, *, cell_size):
    """Return the bounded, draw-only roof geometry for one ORM row."""
    scale = max(0.0001, float(cell_size or 1.0))
    metadata = _mapping(row.metadata_json)
    calculation = _mapping(metadata.get("roofCalculation"))
    build_up = _mapping(calculation.get("roof_build_up"))
    geometry = _mapping(calculation.get("geometry"))
    face_values = build_up.get("top_faces")
    if not isinstance(face_values, list) or not face_values:
        face_values = geometry.get("faces")
    if not isinstance(face_values, list):
        face_values = []

    faces = []
    for index, value in enumerate(face_values[:_MAX_MAP_FACES]):
        face = _mapping(value)
        polygon = face.get("polygon_3d_mm")
        if not isinstance(polygon, list):
            continue
        points = []
        for raw_point in polygon[:_MAX_MAP_POINTS]:
            if not isinstance(raw_point, (list, tuple)) or len(raw_point) < 3:
                continue
            x, z, y = (_number(raw_point[0]), _number(raw_point[1]), _number(raw_point[2]))
            if x is None or y is None or z is None:
                continue
            points.append([
                round(x / 1000.0 * scale, 6),
                round(y / 1000.0 * scale, 6),
                round(z / 1000.0 * scale, 6),
            ])
        points = _open_points(points)
        if len(points) >= 3:
            faces.append({
                "faceRef": str(face.get("face_ref") or index + 1),
                "points": points,
            })

    footprint = _mapping(row.footprint_json)
    coordinate_values = footprint.get("coordinates")
    polygons = coordinate_values if footprint.get("type") == "MultiPolygon" else [coordinate_values]
    base_y = (_number(footprint.get("baseY")) or 0.0) * scale
    height = max(0.0, _number(footprint.get("height")) or 0.0) * scale
    outlines = []
    if isinstance(polygons, list):
        for polygon in polygons[:_MAX_MAP_ROOFS]:
            if not isinstance(polygon, list):
                continue
            for ring in polygon[:128]:
                if not isinstance(ring, list):
                    continue
                points = []
                for raw_point in ring[:_MAX_MAP_POINTS]:
                    if not isinstance(raw_point, (list, tuple)) or len(raw_point) < 2:
                        continue
                    x, z = _number(raw_point[0]), _number(raw_point[1])
                    if x is None or z is None:
                        continue
                    points.append([
                        round(x * scale, 6),
                        round((base_y + height), 6),
                        round(z * scale, 6),
                    ])
                points = _open_points(points)
                if len(points) >= 3:
                    outlines.append(points)

    if not faces and not outlines:
        return None
    return {
        "objectInstanceId": str(row.object_instance_id),
        "primaryChunkKey": str(row.primary_chunk_key or ""),
        "revision": int(row.revision or 0),
        "faces": faces,
        "outlines": outlines,
    }


def map_structure_preview(world):
    """Return all active roof shapes without coupling the map to 3D chunk loads."""
    rows = (
        db.session.query(
            WorldObjectInstance.object_instance_id,
            WorldObjectInstance.primary_chunk_key,
            WorldObjectInstance.revision,
            WorldObjectInstance.footprint_json,
            WorldObjectInstance.metadata_json,
            WorldObjectInstance.updated_at,
        )
        .filter(
            WorldObjectInstance.world_db_id == world.id,
            WorldObjectInstance.deleted_at.is_(None),
            WorldObjectInstance.status == "active",
            WorldObjectInstance.object_type_id == "building_roof",
        )
        .order_by(WorldObjectInstance.object_instance_id.asc())
        .limit(_MAX_MAP_ROOFS)
        .all()
    )
    roofs = []
    revision_values = []
    for row in rows:
        feature = _roof_map_feature(row, cell_size=world.cell_size)
        if feature is not None:
            roofs.append(feature)
        revision_values.append([
            str(row.object_instance_id),
            int(row.revision or 0),
            row.updated_at.isoformat() if row.updated_at else "",
        ])
    revision = sha256(
        json.dumps(revision_values, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "schemaVersion": MAP_SCHEMA_VERSION,
        "revision": revision,
        "roofCount": len(roofs),
        "roofs": roofs,
    }


def structure_streaming_hints(world, chunk_items):
    config = (getattr(world, "metadata_json", None) or {}).get("lod2Buildings", {})
    if not config.get("enabled") or not config.get("materializedBuildings"):
        return {}
    columns = {(int(item["chunkX"]), int(item["chunkZ"])) for item in chunk_items}
    if not columns:
        return {}
    size = int(world.chunk_size)
    hints = defaultdict(set)
    rows = db.session.query(ChunkSnapshot.chunk_x, ChunkSnapshot.chunk_y, ChunkSnapshot.chunk_z).filter(
        ChunkSnapshot.world_db_id == world.id,
        ChunkSnapshot.deleted_at.is_(None), ChunkSnapshot.status == "active",
        tuple_(ChunkSnapshot.chunk_x, ChunkSnapshot.chunk_z).in_(sorted(columns)),
        or_(ChunkSnapshot.non_air_cell_count > 0, ChunkSnapshot.object_refs_json != []),
    ).all()
    for x, y, z in rows:
        hints[(x, z)].add((x, y, z))

    min_x = min(x for x, _ in columns) * size
    max_x = (max(x for x, _ in columns) + 1) * size
    min_z = min(z for _, z in columns) * size
    max_z = (max(z for _, z in columns) + 1) * size
    # Scalar columns only: loading ORM relationships here would eagerly fetch
    # the project's snapshots/events for every streaming request.
    roofs = db.session.query(
        WorldObjectInstance.anchor_x, WorldObjectInstance.anchor_z,
        WorldObjectInstance.size_x, WorldObjectInstance.size_z,
        WorldObjectInstance.primary_chunk_x, WorldObjectInstance.primary_chunk_y,
        WorldObjectInstance.primary_chunk_z,
    ).filter(
        WorldObjectInstance.world_db_id == world.id,
        WorldObjectInstance.deleted_at.is_(None), WorldObjectInstance.status == "active",
        WorldObjectInstance.object_type_id == "building_roof",
        WorldObjectInstance.anchor_x < max_x,
        WorldObjectInstance.anchor_x + WorldObjectInstance.size_x > min_x,
        WorldObjectInstance.anchor_z < max_z,
        WorldObjectInstance.anchor_z + WorldObjectInstance.size_z > min_z,
    ).all()
    for x, z, width, depth, cx, cy, cz in roofs:
        if cx is None or cy is None or cz is None:
            continue
        for column_x, column_z in columns:
            if (x < (column_x + 1) * size and x + width > column_x * size
                    and z < (column_z + 1) * size and z + depth > column_z * size):
                hints[(column_x, column_z)].add((cx, cy, cz))
    return {
        column: {"schemaVersion": SCHEMA_VERSION, "chunkCoordinates": [
            {"chunkX": x, "chunkY": y, "chunkZ": z} for x, y, z in sorted(coordinates)
        ]}
        for column, coordinates in hints.items()
    }
