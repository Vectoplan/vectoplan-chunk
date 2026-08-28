"""Read-only integration check/export using the real project's overlay code.

The exported public geodata supports a separate renderer QA page, not a signed
project session. No access ticket, session or service secret is exported.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.getcwd())
os.environ["VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS"] = "false"
from wsgi import app
from models.project import Project
from models.world import WorldInstance
from sqlalchemy.orm import noload
from src.geodata.visual_overlays import attach_geodata_overlays
from src.world.earth.terrain_pipeline import generate_earth_terrain_chunk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-project-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    start = time.monotonic()
    with app.app_context():
        world = (WorldInstance.query.options(noload("*")).join(Project, WorldInstance.project_db_id == Project.id)
                 .filter(Project.external_app_project_id == args.app_project_id, WorldInstance.world_id == "world_spawn").one())
        provider = world.build_earth_provider()
        reference = generate_earth_terrain_chunk(world=world, provider=provider, chunk_x=0, chunk_y=0, chunk_z=0)
        chunks, buildings, errors = [], {}, []
        parcel_count = 0
        for cx in (-1, 0):
            for cz in (-1, 0):
                chunk = {"chunkX": cx, "chunkY": 0, "chunkZ": cz, "chunkSize": 128, "terrain": reference["terrain"]}
                assert attach_geodata_overlays(chunk, world)
                contract = chunk["metadata"]["geodataOverlays"]
                errors.extend(contract.get("errors", []))
                for item in contract["items"]:
                    if item["renderMode"] == "building-meshes":
                        buildings.update({feature["id"]: feature for feature in item["geometry"]["features"]})
                    elif item["renderMode"] == "surface-lines":
                        parcel_count += item["stats"]["emittedSegmentCount"]
                chunks.append(chunk)
        assert buildings, "No real Berlin buildings in the test region"
        assert parcel_count, "Existing parcel overlay was not retained"
        assert not errors, errors
        feature = next(iter(buildings.values()))
        points = [p for poly in feature["polygons"] for ring in poly["rings"] for p in ring]
        cx = math.floor((min(p[0] for p in points) + max(p[0] for p in points)) / 32)
        cz = math.floor((min(p[2] for p in points) + max(p[2] for p in points)) / 32)
        probe = generate_earth_terrain_chunk(world=world, provider=provider, chunk_x=cx, chunk_y=0, chunk_z=cz)
        assert attach_geodata_overlays(probe, world)
        items = probe["metadata"]["geodataOverlays"]["items"]
        lod2 = next(item for item in items if item["renderMode"] == "building-meshes")
        assert any(f["id"] == feature["id"] for f in lod2["geometry"]["features"])
        report = {"appProjectId": args.app_project_id, "referenceFingerprint": provider.reference_fingerprint,
                  "uniqueBuildings": len(buildings), "polygonCount": sum(len(f["polygons"]) for f in buildings.values()),
                  "parcelSegmentCount": parcel_count, "errors": errors,
                  "probe16CellChunk": {"x": cx, "z": cz, "buildingCount": lod2["stats"]["buildingCount"],
                                       "sampleBuildingId": feature["id"], "payloadBytes": len(json.dumps(probe))},
                  "heightReference": lod2["heightReference"], "elapsedSeconds": round(time.monotonic() - start, 2),
                  "signedProjectBrowserSessionTested": False}
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "data.json").write_text(json.dumps({"chunks": chunks, "report": report}, separators=(",", ":")))
        (args.output / "integration-report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
