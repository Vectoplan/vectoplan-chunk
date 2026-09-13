"""Dry-run by default; retire only proven noncurrent children of one parent.

Run from the service root. --apply requires the generation printed by dry-run
and an existing --backup made with pg_dump -Fc on the same database host.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--world", default="world_spawn")
    parser.add_argument("--parent", required=True)
    parser.add_argument("--expected-generation")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    from wsgi import app
    from extensions import db
    from models import WorldInstance, WorldObjectInstance
    from routes import commands
    from sqlalchemy import select
    with app.app_context(), app.test_request_context("/"):
        try:
            project, universe, world = commands._resolve_project_world_context(args.project, args.world)
            if args.apply:
                if not args.backup or not args.backup.is_file() or args.backup.stat().st_size < 100:
                    raise ValueError("An existing PostgreSQL custom backup is required.")
                if args.backup.open("rb").read(5) != b"PGDMP":
                    raise ValueError("Backup must be a pg_dump custom archive.")
                db.session.execute(select(WorldInstance.id).where(WorldInstance.id == world.id).with_for_update()).one()
            parent = commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.object_instance_id == args.parent,
                WorldObjectInstance.object_type_id == "planning_build_area", WorldObjectInstance.deleted_at.is_(None))).one()
            metadata = deepcopy(parent.metadata_json)
            generation = metadata.get("generationId")
            if args.apply and (not args.expected_generation or args.expected_generation != generation):
                raise ValueError("The parent generation changed after dry-run; no objects were removed.")
            current = {ref["objectInstanceId"] for ref in metadata.get("generatedObjects", [])}
            if not current or not generation:
                raise ValueError("Current generation has no authoritative inventory.")
            children = commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None),
                WorldObjectInstance.metadata_json["generatedFromAreaId"].astext == parent.object_instance_id)).all()
            if not current <= {obj.object_instance_id for obj in children}:
                raise ValueError("Current generation is incomplete; it must be restored before retiring old children.")
            old = sorted((obj for obj in children if obj.object_instance_id not in current), key=lambda obj:obj.object_instance_id)
            plan = {"parent": args.parent, "generation": generation, "preservedCurrentObjectIds": sorted(current),
                "retiredObjectIds": [obj.object_instance_id for obj in old], "retiredObjectCount": len(old),
                "oldRoutingCellCount": sum(len(obj.occupied_cells_json or []) for obj in old), "applied": False}
            if args.apply and old:
                child_commands = [{"type":"RemoveObject", "objectInstanceId":obj.object_instance_id,
                    "position":{"x":obj.anchor_x,"y":obj.anchor_y,"z":obj.anchor_z}} for obj in old]
                metadata["retiredGeneratedObjects"] = []
                child_commands.append({"type":"PlaceObject", "objectInstanceId":args.parent,
                    "objectTypeId":"planning_build_area", "objectKind":"semantic_footprint",
                    "blockTypeId":metadata.get("wallBlockTypeId", "lod2_exterior_wall"),
                    "position":{"x":parent.anchor_x,"y":parent.anchor_y,"z":parent.anchor_z},
                    "dimensions":{"x":parent.size_x,"y":parent.size_y,"z":parent.size_z},
                    "footprint":parent.footprint_json,"occupiedCells":parent.occupied_cells_json,"metadata":metadata})
                command_id=f"repair_planning_generation_{uuid4().hex}"
                log,result=commands._execute_command(project=project,universe=universe,world=world,
                    payload={"type":"ObjectBatch","commandId":command_id,"userId":"planning_generation_repair",
                        "sessionId":command_id,"position":child_commands[-1]["position"],"commands":child_commands})
                db.session.commit()
                plan.update(applied=True,commandId=command_id,dirtyChunks=result.get("dirtyChunks",[]),
                    backupSha256=hashlib.sha256(args.backup.read_bytes()).hexdigest())
            print(json.dumps(plan,ensure_ascii=False,sort_keys=True))
        finally:
            db.session.rollback()


if __name__ == "__main__":
    main()
