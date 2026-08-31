"""Upgrade existing LoD2 facade profiles without materializing new buildings."""
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
from src.geodata.lod2_import import apply_only_facade_metadata_repairs, prepare_import


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-project-id", required=True)
    parser.add_argument("--radius", type=int, choices=[32, 64, 128, 256], default=128)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise ValueError("Refusing to overwrite an existing facade repair report")
    with app.app_context():
        world = (WorldInstance.query.options(noload("*")).join(Project, WorldInstance.project_db_id == Project.id)
                 .filter(Project.external_app_project_id == args.app_project_id,
                         WorldInstance.world_id == "world_spawn").one())
        plan = prepare_import(world, radius=args.radius)
        repairs = plan.get("metadataRepairs", [])
        report = {
            "appProjectId": args.app_project_id,
            "worldId": world.world_id,
            "applied": False,
            "repairBuildingIds": [item["buildingId"] for item in repairs],
            "repairCount": len(repairs),
            "ignoredNewBuildings": len(plan.get("buildings", [])),
            "sourceErrors": plan.get("sourceErrors", {}),
        }
        if args.apply:
            report["result"] = apply_only_facade_metadata_repairs(world, plan)
            db.session.commit()
            report["applied"] = True
        else:
            db.session.rollback()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
