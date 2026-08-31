"""Explicit, reversible roof-zone migration; never rewrites wall cells.

Only untouched imported roofs are eligible. Removed or edited roofs are skipped.
The report keeps the complete old object payloads; all commands commit together.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.getcwd())
os.environ["VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS"] = "false"
from wsgi import app
from extensions import db
from sqlalchemy import select
from sqlalchemy.orm import noload
from models.project import Project
from models.world import WorldInstance
from models.universe import Universe
from models.object import WorldObjectInstance
from routes import commands
from src.geodata.lod2_conversion import roof_objects, digest, WALL_BLOCK_ID

VERSION = "lod2-roof-zones.v2"


def replacement_roofs(obj):
    metadata = obj.metadata_json or {}
    parameters = metadata.get("roofParameters", {})
    source = parameters.get("importedSource", {})
    calculation = metadata.get("roofCalculation", {})
    if (metadata.get("geometryVersion") == VERSION
            or metadata.get("source") != "vectoplan-chunk.lod2-import"
            or parameters.get("roofType") != "imported"
            or calculation.get("geometry", {}).get("faces") != source.get("faces")
            or calculation.get("input_fingerprint") != digest(source)):
        return []
    feature = {"id": source["buildingId"], "sourceTile": source["sourceTile"], "sourceSha256": source["sourceSha256"],
        "polygons": [{"surface": "RoofSurface", "rings": [[
            *[[x/1000, y/1000, z/1000] for x, z, y in f["polygon_3d_mm"]],
            [f["polygon_3d_mm"][0][0]/1000, f["polygon_3d_mm"][0][2]/1000, f["polygon_3d_mm"][0][1]/1000],
        ]]} for f in source["faces"]]}
    replacements = roof_objects(feature)
    def facets(faces):
        return {tuple(sorted(tuple(round(v,3) for v in p) for p in face["polygon_3d_mm"])) for face in faces}
    if facets(source["faces"]) != facets([f for r in replacements for f in r["metadata"]["roofCalculation"]["geometry"]["faces"]]):
        raise ValueError("Roof partition changed source geometry; abort refinement")
    if len(replacements) == 1 and len(replacements[0]["metadata"]["roofCalculation"]["geometry"]["faces"]) == len(source["faces"]):
        return []
    for roof in replacements:
        new_id = "lod2_roof_"+digest([VERSION, obj.object_instance_id, roof["objectInstanceId"]])[:28]
        roof["objectInstanceId"] = new_id
        roof["metadata"].update(geometryVersion=VERSION, refinedFrom=obj.object_instance_id, mergeKey=new_id)
        roof["metadata"]["roofCalculation"]["calculation_id"] = new_id
    return replacements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-project-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.report.exists():
        raise ValueError("Report exists; choose a new path")
    with app.app_context(), app.test_request_context("/"):
        world = (WorldInstance.query.options(noload("*")).join(Project, WorldInstance.project_db_id == Project.id)
            .filter(Project.external_app_project_id == args.app_project_id, WorldInstance.world_id == "world_spawn").one())
        ledger = (world.metadata_json or {}).get("lod2Buildings", {}).get("materializedBuildings", {})
        expected_ids = [rid for receipt in ledger.values() for rid in receipt["roofIds"]]
        objects = WorldObjectInstance.query.options(noload("*")).filter(WorldObjectInstance.world_db_id == world.id,
            WorldObjectInstance.deleted_at.is_(None), WorldObjectInstance.object_instance_id.in_(expected_ids)).all()
        plan = []
        for obj in objects:
            roofs = replacement_roofs(obj)
            if not roofs:
                continue
            backup = {"type":"PlaceObject","objectInstanceId":obj.object_instance_id,"objectTypeId":"building_roof",
                "objectKind":obj.object_kind,"objectSource":obj.object_source,"blockTypeId":WALL_BLOCK_ID,
                "position":{"x":obj.anchor_x,"y":obj.anchor_y,"z":obj.anchor_z},
                "dimensions":{"x":obj.size_x,"y":obj.size_y,"z":obj.size_z},
                "occupiedCells":copy.deepcopy(obj.occupied_cells_json),"footprint":copy.deepcopy(obj.footprint_json),
                "metadata":copy.deepcopy(obj.metadata_json)}
            plan.append({"old":backup,"replacements":roofs})
        report = {"version":VERSION,"apply":args.apply,"replacedRoofs":len(plan),
            "newRoofs":sum(len(p["replacements"]) for p in plan),"plan":plan,"committed":False}
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+"\n")
        if args.apply and plan:
            db.session.execute(select(WorldInstance.id).where(WorldInstance.id == world.id).with_for_update()).one()
            db.session.refresh(world,attribute_names=["metadata_json"])
            config = copy.deepcopy(world.metadata_json["lod2Buildings"])
            project = db.session.get(Project,world.project_db_id,options=[noload("*")])
            universe = db.session.get(Universe,world.universe_db_id,options=[noload("*")])
            ids = []
            for entry in plan:
                old = entry["old"]
                obj = next(o for o in objects if o.object_instance_id == old["objectInstanceId"])
                db.session.refresh(obj,attribute_names=["metadata_json","deleted_at","occupied_cells_json"])
                if obj.deleted_at is not None or obj.metadata_json != old["metadata"] or obj.occupied_cells_json != old["occupiedCells"]:
                    raise ValueError("Roof was edited while preparing migration; nothing committed")
                for payload in [{"type":"RemoveObject","objectInstanceId":obj.object_instance_id},*entry["replacements"]]:
                    result = commands._execute_command(project=project,universe=universe,world=world,
                        payload={**payload,"userId":"system_lod2_roof_refinement","sessionId":VERSION,"commandSource":"importer"})
                    ids.append(result[0].command_id)
                receipt = config["materializedBuildings"][old["metadata"]["lod2BuildingId"]]
                receipt["roofIds"] = [r for r in receipt["roofIds"] if r != obj.object_instance_id]+[r["objectInstanceId"] for r in entry["replacements"]]
                receipt.setdefault("retiredRoofIds",[]).append(obj.object_instance_id)
                receipt["roofGeometryVersion"] = VERSION
            world.metadata_json = {**world.metadata_json,"lod2Buildings":config}
            db.session.commit()
            report.update(committed=True,commandIds=ids)
            args.report.write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps({k:v for k,v in report.items() if k != "plan"}))


if __name__ == "__main__":
    main()
