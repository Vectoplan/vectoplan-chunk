"""Explicit local-admin opt-in for ONE App project's Earth world; no cell edits.

Run in the Chunk container, with --apply only after importing verified tiles.
This changes only metadata_json.lod2Buildings, preserving all other metadata.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.getcwd())
os.environ["VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS"] = "false"

from wsgi import app
from extensions import db
from models.project import Project
from models.world import WorldInstance
from sqlalchemy.orm import noload
from src.geodata.lod2_buildings import building_overlay_item, store_path
from src.world.earth.terrain_pipeline import generate_earth_terrain_chunk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-project-id", required=True)
    parser.add_argument("--world-id", default="world_spawn")
    parser.add_argument("--allow-flat-test-height", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with app.app_context():
        world = (WorldInstance.query.options(noload("*")).join(Project, WorldInstance.project_db_id == Project.id)
                 .filter(Project.external_app_project_id == args.app_project_id, WorldInstance.world_id == args.world_id)
                 .with_for_update(of=WorldInstance).one())
        if not world.is_earth_world:
            raise ValueError("LoD2 requires an Earth world")
        provider = world.build_earth_provider()
        previous = (world.metadata_json or {}).get("lod2Buildings")
        chunk = generate_earth_terrain_chunk(world=world, provider=provider, chunk_x=0, chunk_y=0, chunk_z=0)
        config = {"enabled": True, "allowFlatTerrainAlignment": args.allow_flat_test_height,
                  "referenceFingerprint": provider.reference_fingerprint, "datasetId": "3d-gebaeudedaten", "region": "DE-BE"}
        terrain = chunk.get("terrain") or {}
        if terrain.get("anchorElevationM") is not None:
            config["anchorElevationM"] = terrain["anchorElevationM"]
        world.metadata_json = {**(world.metadata_json or {}), "lod2Buildings": config}
        overlay = building_overlay_item(world=world, provider=provider, chunk=chunk)
        report = {"appProjectId": args.app_project_id, "worldId": world.world_id, "applied": args.apply,
                  "previousLod2Config": previous, "lod2Config": config, "store": str(store_path()),
                  "terrainStatus": terrain.get("status"), "heightReference": overlay["heightReference"],
                  "referenceChunkStats": overlay["stats"], "chunkCellsChanged": False}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            if args.report.exists():
                raise ValueError("Refusing to overwrite the previous configuration report")
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        if args.apply:
            db.session.commit()
        else:
            db.session.rollback()
        print(json.dumps(report))


if __name__ == "__main__":
    main()
