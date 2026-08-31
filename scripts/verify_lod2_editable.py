"""Read committed editable LoD2 state through the normal snapshot adapter.

Exports only imported LoD2 walls/roofs and public parcel overlays for a local,
clearly labelled renderer QA. Does not test or bypass a signed project session.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, os.getcwd())
os.environ["VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS"] = "false"
from wsgi import app
from models.project import Project
from models.world import WorldInstance
from models.universe import Universe
from models.chunk import ChunkSnapshot
from models.object import WorldObjectInstance
from extensions import db
from sqlalchemy import or_
from sqlalchemy.orm import noload
from routes.chunks import _runtime_content_from_snapshot, _serialize_chunk_load_result
from src.geodata.structure_streaming import structure_streaming_hints
from unittest.mock import patch
from src.geodata.lod2_buildings import building_overlay_item, store_path
from src.geodata.visual_overlays import attach_geodata_overlays
from src.geodata.lod2_conversion import WALL_BLOCK_ID
from src.world.earth.terrain_pipeline import generate_earth_terrain_chunk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-project-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with app.app_context():
        world = (WorldInstance.query.options(noload("*")).join(Project, WorldInstance.project_db_id == Project.id)
                 .filter(Project.external_app_project_id == args.app_project_id, WorldInstance.world_id == "world_spawn").one())
        project = db.session.get(Project, world.project_db_id, options=[noload("*")])
        universe = db.session.get(Universe, world.universe_db_id, options=[noload("*")])
        config = (world.metadata_json or {}).get("lod2Buildings", {})
        ledger = config.get("materializedBuildings", {})
        assert ledger, "No committed LoD2 import receipts"
        provider = world.build_earth_provider()
        snapshots = ChunkSnapshot.query.options(noload("*")).filter(
            ChunkSnapshot.world_db_id == world.id, ChunkSnapshot.deleted_at.is_(None),
            or_(ChunkSnapshot.palette_json.contains([{"blockTypeId": WALL_BLOCK_ID}]),
                ChunkSnapshot.object_refs_json.contains([{"objectTypeId": "building_roof"}]))).all()
        hints = structure_streaming_hints(world, [{"chunkX": s.chunk_x, "chunkZ": s.chunk_z} for s in snapshots])
        wire_chunks = []
        chunks, roof_refs, wall_count = [], {}, 0
        for snapshot in snapshots:
            chunk = _runtime_content_from_snapshot(snapshot=snapshot, project=project, universe=universe, world=world)
            palette = chunk["palette"]
            wall_values = {i+1 for i, p in enumerate(palette) if p.get("blockTypeId") == WALL_BLOCK_ID}
            count = sum(c in wall_values for c in chunk["cells"])
            wall_count += count
            refs = [ref for ref in chunk["objectRefs"] if ref.get("metadata", {}).get("lod2BuildingId") in ledger]
            for ref in refs:
                roof_refs[ref["objectInstanceId"]] = ref
                assert ref["metadata"].get("voxelOccupancy") == "none"
                assert ref["metadata"]["roofCalculation"]["ok"] is True
            if count or refs:
                # Exercise the production response serializer including streaming
                # hints and RLE. Exclude unrelated user objects from this QA file.
                qa_chunk = {**chunk, "cells": [value if value in wall_values else 0 for value in chunk["cells"]], "objectRefs": refs}
                with app.test_request_context("/", headers={"User-Agent": "vectoplan-editor-qa"}), patch(
                        "src.geodata.visual_overlays.attach_geodata_overlays", return_value=False):
                    wire_chunks.append(_serialize_chunk_load_result(project=project, universe=universe, world=world,
                        result={"chunk": qa_chunk, "chunkKey": chunk["chunkKey"], "source": "snapshot"},
                        structure_hints=hints, include_snapshot_metadata=False, include_route_hints=False))
                # Export the actual persisted cell positions, but not unrelated user objects.
                chunks.append({"chunkKey": chunk["chunkKey"], "chunkX": chunk["chunkX"], "chunkY": chunk["chunkY"],
                               "chunkZ": chunk["chunkZ"], "chunkSize": chunk["chunkSize"],
                               "wallCells": [i for i, value in enumerate(chunk["cells"]) if value in wall_values], "objectRefs": refs})
        objects = WorldObjectInstance.query.options(noload("*")).filter(
            WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None),
            WorldObjectInstance.object_instance_id.in_([r for b in ledger.values() for r in b["roofIds"]])).all()
        assert {obj.object_instance_id for obj in objects} == set(roof_refs), "Roof refs differ from active persisted objects"
        parcels, unconverted, errors = [], set(), []
        for x in (-1, 0):
            for z in (-1, 0):
                chunk = generate_earth_terrain_chunk(world=world, provider=provider, chunk_x=x, chunk_y=0, chunk_z=z)
                chunk.update(chunkX=x, chunkZ=z, chunkSize=128)
                assert attach_geodata_overlays(chunk, world)
                contract = chunk["metadata"]["geodataOverlays"]
                errors.extend(contract.get("errors", []))
                for item in contract["items"]:
                    if item["renderMode"] == "surface-lines":
                        parcels.extend(item["geometry"]["coordinates"])
                    if item["renderMode"] == "building-meshes":
                        for feature in item["geometry"]["features"]:
                            assert feature["id"] not in ledger, "Decorative mesh covers an editable building"
                            unconverted.add(feature["id"])
        with sqlite3.connect(f"file:{store_path()}?mode=ro", uri=True) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            stored_buildings = connection.execute("SELECT count(*) FROM buildings").fetchone()[0]
        report = {"appProjectId": args.app_project_id, "importedBuildings": len(ledger), "wallCells": wall_count,
                  "roofObjects": len(objects), "chunkCount": len(chunks), "sourceStoreBuildings": stored_buildings,
                  "sourceStoreIntegrity": "ok", "sourceOverlaySuppressedForImportedBuildings": True,
                  "remainingReferenceBuildings": sorted(unconverted), "parcelLines": len(parcels), "overlayErrors": errors,
                  "heightReference": next(iter(ledger.values()))["heightReference"], "signedBrowserTested": False}
        assert wall_count and objects and parcels and not errors, report
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "state-report.json").write_text(json.dumps(report, indent=2)+"\n")
        (args.output / "editable-data.json").write_text(json.dumps({"chunks": chunks, "parcels": parcels, "report": report}, separators=(",", ":")))
        (args.output / "streaming-data.json").write_text(json.dumps({"ok": True, "projectId": project.project_id,
            "worldId": world.world_id, "chunks": wire_chunks, "parcels": parcels, "report": report}, separators=(",", ":")))
        print(json.dumps(report))


if __name__ == "__main__":
    main()
