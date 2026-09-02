"""Explicit local-admin materialization, using canonical commands and snapshots.

No GET-side writes, automatic source updates, or city-wide import. A successful
receipt survives roof deletion and block removal. Re-running skips that building.
The caller owns commit/rollback; geometry preparation runs before the write lock.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
import sqlite3

from sqlalchemy import select
from sqlalchemy.orm import noload

from extensions import db
from models.block import BLOCK_STATUS_ACTIVE, BlockType
from models.chunk import ChunkSnapshot
from models.event import WorldCommandLog
from models.object import WorldObjectInstance
from models.project import Project
from models.universe import Universe
from models.world import WorldInstance
from routes import commands
from src.geodata.lod2_buildings import building_overlay_item, world_lod2_config
from src.geodata.lod2_sources import sources_for_world
from src.geodata.lod2_conversion import (
    CONVERSION_VERSION,
    CONSTRUCTION_GRID_VERSION,
    WALL_BLOCK_ID,
    construction_grid_contract,
    convert_building,
    facade_segments,
    ground_footprints,
)
from src.world.earth.terrain_pipeline import generate_earth_terrain_chunk

FACADE_GRID_VERSION = "lod2-facade-grid.v4"
LOD2_EXISTING_WALL_COLOR = "#f1f3f5"
LOD2_WALL_MATERIAL_ID = "lod2_exterior_wall"
LOD2_WALL_METADATA = {
    "semanticRole": "wall",
    "color": LOD2_EXISTING_WALL_COLOR,
    "source": "lod2",
    "thicknessAssumed": True,
    "constructionGridVersion": CONSTRUCTION_GRID_VERSION,
    "cellPolicy": "whole-breakable-voxel",
}
LOD2_WALL_CANONICAL_PROPERTIES = {
    "status": BLOCK_STATUS_ACTIVE,
    "deleted_at": None,
    "deprecated_at": None,
    "solid": True,
    "opaque": True,
    "placeable": True,
    "breakable": True,
    "selectable": True,
    "collidable": True,
    "emits_light": False,
    "light_level": 0,
    "render_mode": "cube",
    "shape_type": "cube",
    "material_id": LOD2_WALL_MATERIAL_ID,
    # Old textured prototypes must not override the neutral stock-building
    # material after the registry row has been upgraded.
    "texture_id": None,
}


def prepare_import(world, *, radius=128, center_x=0, center_z=0):
    if radius not in (32, 64, 128, 256):
        raise ValueError("Choose a bounded radius: 32, 64, 128 or 256 cells")
    config = world_lod2_config(world)
    if config is None or not world.is_earth_world:
        raise ValueError("Explicit LoD2 Earth-world opt-in required")
    provider = world.build_earth_provider()
    if config.get("referenceFingerprint") not in (None, provider.reference_fingerprint):
        raise ValueError("LoD2 project reference changed; explicit re-alignment required")
    chunk = generate_earth_terrain_chunk(world=world, provider=provider, chunk_x=0, chunk_y=0, chunk_z=0)
    features, source_revisions, height_references, source_errors = {}, {}, {}, {}
    sources = sources_for_world(config)
    # Small source windows and registered stores only; no viewer-side downloads.
    for x in range(center_x-radius, center_x+radius, 32):
        for z in range(center_z-radius, center_z+radius, 32):
            for source in sources:
                if source.id in source_errors:
                    continue
                try:
                    item = building_overlay_item(world=world, provider=provider,
                        chunk={**chunk, "chunkX": math.floor(x / 32), "chunkZ": math.floor(z / 32), "chunkSize": 32},
                        include_materialized=True, source=source)
                    if item is None:
                        continue
                    source_revisions[source.id] = item["releaseKey"]
                    height_references[source.id] = item["heightReference"]
                    features.update({f["id"]: f for f in item["geometry"]["features"]})
                except (ValueError, OSError, sqlite3.Error) as exc:
                    source_errors[source.id] = str(exc)
    if len(features) > 200:
        raise ValueError("More than 200 buildings; reduce import radius")
    converted, skipped, existing, metadata_repairs = [], [], [], []
    for feature in sorted(features.values(), key=lambda f: f["id"]):
        receipt = config.get("materializedBuildings", {}).get(feature["id"])
        if receipt is not None:
            existing.append(feature["id"])
            if receipt.get("facadeGridVersion") != FACADE_GRID_VERSION:
                segments = facade_segments(feature["polygons"])
                if segments:
                    footprints = ground_footprints(feature["polygons"])
                    metadata_repairs.append({
                        "buildingId": feature["id"],
                        "facadeSegments": segments,
                        "groundFootprints": footprints,
                        "constructionGrid": construction_grid_contract(
                            feature,
                            building_facades=segments,
                            building_ground_footprints=footprints,
                        ),
                    })
            continue
        try:
            building = convert_building(feature)
            if any(max(roof["dimensions"].values()) > 256 for roof in building["roofs"]):
                raise ValueError("Roof exceeds WorldEdit's 256-cell object limit")
            converted.append(building)
        except ValueError as exc:
            # Retain its original overlay. Never suppress a partially converted building.
            skipped.append({"buildingId": feature["id"], "reason": str(exc)})
    cells = {tuple(c) for b in converted for c in b["wallCells"]}
    if len(cells) > 400_000:
        raise ValueError("Import exceeds 400,000 wall cells; reduce radius")
    chunk_keys = {tuple(math.floor(c[i]/world.chunk_size) for i in range(3)) for c in cells}
    if len(chunk_keys) > 2048:
        raise ValueError("Import exceeds 2048 chunks; reduce radius")
    return {"version": CONVERSION_VERSION, "referenceFingerprint": provider.reference_fingerprint,
            "bounds": [center_x-radius, center_z-radius, center_x+radius, center_z+radius],
            "heightReference": next(iter(height_references.values()), None) if len(height_references)<=1 else {"kind":"per-source", "sources":height_references},
            "sourceRevision": next(iter(source_revisions.values()), None), "sourceRevisions":source_revisions,
            "sourceErrors":source_errors,
            "buildings": converted, "alreadyImported": existing, "skipped": skipped,
            "metadataRepairs": metadata_repairs,
            "candidateWallCells": len(cells), "candidateChunks": len(chunk_keys)}


def summary(plan):
    return {k: v for k, v in plan.items() if k not in {"buildings", "metadataRepairs"}} | {
        "buildingCount": len(plan["buildings"]),
        "facadeMetadataRepairCount": len(plan.get("metadataRepairs", [])),
        "roofCount": sum(len(b["roofs"]) for b in plan["buildings"]),
        "buildings": [{"buildingId": b["buildingId"], "sourceTile": b["sourceTile"],
                       "sourceSha256": b["sourceSha256"], "wallCellCount": len(b["wallCells"]),
                       "roofIds": [r["objectInstanceId"] for r in b["roofs"]],
                       "constructionGridVersion": ((b.get("constructionGrid") or {}).get("schemaVersion")),
                       "constructionGridFingerprint": ((b.get("constructionGrid") or {}).get("fingerprint"))}
                      for b in plan["buildings"]]}


def apply_facade_metadata_repairs(world, repairs):
    """Attach exact WallSurface axes to old roof refs without rematerializing geometry.

    Existing roof edits, removed wall cells, command history and object
    footprints remain untouched; only the missing source reference is added.
    """
    by_building = {
        str(item.get("buildingId")): {
            "facadeSegments": copy.deepcopy(item.get("facadeSegments") or []),
            "groundFootprints": copy.deepcopy(item.get("groundFootprints") or []),
            "constructionGrid": copy.deepcopy(item.get("constructionGrid")),
        }
        for item in repairs
        if item.get("buildingId") and item.get("facadeSegments")
    }
    if not by_building:
        return {"repairedBuildings": 0, "repairedRoofObjects": 0, "repairedSnapshots": 0}

    metadata_by_object_id = {}
    repaired_buildings = set()
    objects = WorldObjectInstance.query.options(noload("*")).filter(
        WorldObjectInstance.world_db_id == world.id,
        WorldObjectInstance.deleted_at.is_(None),
        WorldObjectInstance.status == "active",
        WorldObjectInstance.object_type_id == "building_roof",
    ).all()
    for object_instance in objects:
        metadata = copy.deepcopy(object_instance.metadata_json or {})
        building_id = str(metadata.get("lod2BuildingId") or "")
        repair = by_building.get(building_id)
        if not repair:
            continue
        parameters = copy.deepcopy(metadata.get("roofParameters") or {})
        imported_source = copy.deepcopy(parameters.get("importedSource") or {})
        imported_source["facadeSegments"] = copy.deepcopy(repair["facadeSegments"])
        imported_source["groundFootprints"] = copy.deepcopy(repair["groundFootprints"])
        imported_source["facadeGridVersion"] = FACADE_GRID_VERSION
        if repair["constructionGrid"] is not None:
            imported_source["constructionGrid"] = copy.deepcopy(repair["constructionGrid"])
        parameters["importedSource"] = imported_source
        metadata["roofParameters"] = parameters
        metadata["facadeGridVersion"] = FACADE_GRID_VERSION
        if repair["constructionGrid"] is not None:
            metadata["constructionGridVersion"] = CONSTRUCTION_GRID_VERSION
        object_instance.replace_metadata(metadata, updated_by_user_id="system_lod2_import")
        metadata_by_object_id[str(object_instance.object_instance_id)] = metadata
        repaired_buildings.add(building_id)

    repaired_snapshots = 0
    snapshots = ChunkSnapshot.query.options(noload("*")).filter(
        ChunkSnapshot.world_db_id == world.id,
        ChunkSnapshot.deleted_at.is_(None),
        ChunkSnapshot.status == "active",
        ChunkSnapshot.has_object_refs.is_(True),
    ).all()
    for snapshot in snapshots:
        changed = False
        refs = copy.deepcopy(snapshot.object_refs_json or [])
        for ref in refs:
            metadata = metadata_by_object_id.get(str(ref.get("objectInstanceId"))) if isinstance(ref, dict) else None
            if metadata is None:
                continue
            ref["metadata"] = copy.deepcopy(metadata)
            changed = True
        if changed:
            snapshot.set_object_refs(refs, updated_by_user_id="system_lod2_import")
            snapshot.bump_revision()
            repaired_snapshots += 1
    db.session.flush()
    return {"repairedBuildings": len(repaired_buildings),
            "repairedRoofObjects": len(metadata_by_object_id), "repairedSnapshots": repaired_snapshots}


def apply_only_facade_metadata_repairs(world, plan):
    """Upgrade already materialized buildings without importing new neighbours.

    `prepare_import` deliberately reports both new buildings and old metadata.
    Maintenance callers must not pass that mixed plan to `apply_import` when the
    requested scope is only a renderer/schema repair.  This locked operation
    filters strictly to receipt-backed buildings and leaves cells, roof edits,
    command history and unmaterialized LoD2 features untouched.
    """
    db.session.execute(select(WorldInstance.id).where(WorldInstance.id == world.id).with_for_update()).one()
    db.session.refresh(world, attribute_names=["metadata_json"])
    config = dict(world_lod2_config(world) or {})
    if not config or world.build_earth_provider().reference_fingerprint != plan["referenceFingerprint"]:
        raise ValueError("Project configuration changed while preparing facade repair")
    # The repair command is also the maintenance/bootstrap path for worlds that
    # were materialized before LoD2 walls became individually editable.
    register_wall(world)
    ledger = dict(config.get("materializedBuildings", {}))
    repairs = [item for item in plan.get("metadataRepairs", []) if item.get("buildingId") in ledger]
    result = apply_facade_metadata_repairs(world, repairs)
    repaired_at = datetime.now(timezone.utc).isoformat()
    for item in repairs:
        building_id = item.get("buildingId")
        grid = item.get("constructionGrid") or {}
        ledger[building_id] = {
            **ledger[building_id],
            "facadeGridVersion": FACADE_GRID_VERSION,
            "constructionGridVersion": grid.get("schemaVersion"),
            "constructionGridFingerprint": grid.get("fingerprint"),
            "facadeGridRepairedAt": repaired_at,
        }
    world.metadata_json = {**world.metadata_json, "lod2Buildings": {
        **config, "materializedBuildings": ledger,
    }}
    db.session.flush()
    return {**result, "ignoredNewBuildings": len(plan.get("buildings", []))}


def _reconcile_lod2_wall_type(wall):
    """Upgrade only an already-resolved reserved LoD2 wall row in place."""
    changed = False
    for attribute, canonical_value in LOD2_WALL_CANONICAL_PROPERTIES.items():
        if getattr(wall, attribute) != canonical_value:
            setattr(wall, attribute, canonical_value)
            changed = True
    current_metadata = dict(wall.metadata_json or {})
    metadata = {**current_metadata, **LOD2_WALL_METADATA}
    if metadata != current_metadata:
        wall.metadata_json = metadata
        changed = True
    if changed:
        wall.touch(updated_by_user_id="system_lod2_import")
    return changed


def register_wall(world):
    registry = commands._get_registry_for_world(world)
    existing = BlockType.query.options(noload("*")).filter_by(registry_db_id=registry.id, block_type_id=WALL_BLOCK_ID).one_or_none()
    if existing:
        # Reconcile only the reserved LoD2 wall id in this world's registry.
        # Historic snapshots keep referencing the same row, so replacing it or
        # creating an alias would leave those cells permanently unbreakable.
        if _reconcile_lod2_wall_type(existing):
            db.session.flush()
        return existing
    wall = BlockType.create_for_registry(
        registry, block_type_id=WALL_BLOCK_ID, label="LoD2 Außenwand", category="structure",
        description="Abbaubare LoD2-Außenhülle im 1-m-Raster; keine gemessene Wandstärke.",
        solid=True, opaque=True, placeable=True, breakable=True, selectable=True, collidable=True,
        material_id=LOD2_WALL_MATERIAL_ID, metadata_json=LOD2_WALL_METADATA)
    db.session.add(wall)
    db.session.flush()
    return wall


def apply_import(world, plan, *, progress=None):
    db.session.execute(select(WorldInstance.id).where(WorldInstance.id == world.id).with_for_update()).one()
    db.session.refresh(world, attribute_names=["metadata_json"])
    config = dict(world_lod2_config(world) or {})
    if not config or world.build_earth_provider().reference_fingerprint != plan["referenceFingerprint"]:
        raise ValueError("Project configuration changed while preparing import")
    ledger = dict(config.get("materializedBuildings", {}))
    buildings = [b for b in plan["buildings"] if b["buildingId"] not in ledger]
    repairs = [item for item in plan.get("metadataRepairs", []) if item.get("buildingId") in ledger]
    # Explicit re-imports and editor-dataset bootstrap runs must heal legacy
    # registries even when every building already has a materialization receipt.
    register_wall(world)
    if not buildings and not repairs:
        return {"importedBuildings": 0, "writtenWallCells": 0, "roofCount": 0, "commandIds": [],
                "repairedBuildings": 0, "repairedRoofObjects": 0, "repairedSnapshots": 0}
    project = db.session.get(Project, world.project_db_id, options=[noload("*")])
    universe = db.session.get(Universe, world.universe_db_id, options=[noload("*")])
    repair_result = apply_facade_metadata_repairs(world, repairs)
    for item in repairs:
        building_id = item.get("buildingId")
        if building_id not in ledger:
            continue
        grid = item.get("constructionGrid") or {}
        ledger[building_id] = {
            **ledger[building_id],
            "facadeGridVersion": FACADE_GRID_VERSION,
            "constructionGridVersion": grid.get("schemaVersion"),
            "constructionGridFingerprint": grid.get("fingerprint"),
            "facadeGridRepairedAt": datetime.now(timezone.utc).isoformat(),
        }
    # Protect all explicitly edited cells, including air after removal, not just
    # currently occupied cells. Only scalar JSON columns, never eager relationships.
    protected = set()
    for (affected,) in db.session.query(WorldCommandLog.affected_cells_json).filter_by(world_db_id=world.id):
        for cell in affected or []:
            if isinstance(cell, dict) and all(axis in cell for axis in ("x", "y", "z")):
                protected.add(tuple(int(cell[axis]) for axis in ("x", "y", "z")))
    requested = {tuple(c) for b in buildings for c in b["wallCells"]}
    grouped = {}
    for cell in sorted(requested-protected):
        key = tuple(c // world.chunk_size for c in cell)
        grouped.setdefault(key, []).append(cell)
    written, occupied_skips, command_ids = 0, 0, []
    for index, (key, candidates) in enumerate(sorted(grouped.items())):
        if progress and index % 20 == 0:
            progress({"stage": "wall-chunks", "completed": index, "total": len(grouped), "writtenWallCells": written})
        snapshot, content = commands._load_chunk_for_mutation(project=project, universe=universe, world=world,
                                                             chunk_x=key[0], chunk_y=key[1], chunk_z=key[2])
        # Snapshots without command history may originate from older/manual
        # imports. Conservatively retain them entirely rather than refill air.
        unknown_snapshot = snapshot is not None and not snapshot.last_command_id
        safe = []
        for cell in candidates:
            value = commands._get_cell_value(content, local_x=cell[0] % world.chunk_size,
                                            local_y=cell[1] % world.chunk_size, local_z=cell[2] % world.chunk_size,
                                            chunk_size=world.chunk_size)
            if value == 0 and not unknown_snapshot:
                safe.append(cell)
            else:
                occupied_skips += 1
        if not safe:
            continue
        payload = {"type": "WorldEdit", "tool": "clipboard", "operation": "paste",
                   "position": {"x": 0, "y": 0, "z": 0}, "commandSource": "importer",
                   "userId": "system_lod2_import", "metadata": {
                       "source": CONVERSION_VERSION,
                       "constructionGridVersion": CONSTRUCTION_GRID_VERSION,
                       "wallCellPolicy": "whole-breakable-voxel",
                   },
                   "clipboard": [{"dx": x, "dy": y, "dz": z, "blockTypeId": WALL_BLOCK_ID} for x, y, z in safe]}
        log, result = commands._execute_command(project=project, universe=universe, world=world, payload=payload)
        written += len(result["affectedCells"])
        command_ids.append(log.command_id)
    for building in buildings:
        for roof in building["roofs"]:
            log, _ = commands._execute_command(project=project, universe=universe, world=world,
                                               payload={**roof, "commandSource": "importer", "userId": "system_lod2_import"})
            command_ids.append(log.command_id)
        ledger[building["buildingId"]] = {"version": CONVERSION_VERSION, "sourceSha256": building["sourceSha256"],
                                         "sourceTile": building["sourceTile"],
                                         "roofIds": [r["objectInstanceId"] for r in building["roofs"]],
                                         "facadeGridVersion": FACADE_GRID_VERSION,
                                         "constructionGridVersion": ((building.get("constructionGrid") or {}).get("schemaVersion")),
                                         "constructionGridFingerprint": ((building.get("constructionGrid") or {}).get("fingerprint")),
                                         "heightReference": plan["heightReference"],
                                         "importedAt": datetime.now(timezone.utc).isoformat()}
    world.metadata_json = {**world.metadata_json, "lod2Buildings": {**config, "referenceFingerprint":plan["referenceFingerprint"], "materializedBuildings": ledger}}
    db.session.flush()
    return {"importedBuildings": len(buildings), "writtenWallCells": written,
            "protectedEditedCells": len(requested & protected), "occupiedOrUntrackedCellsSkipped": occupied_skips,
            "roofCount": sum(len(b["roofs"]) for b in buildings), "commandIds": command_ids, **repair_result}
