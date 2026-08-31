"""Bounded local-admin import. Dry-run by default; originals are never modified."""
import argparse
import faulthandler
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
from src.geodata.lod2_import import apply_import, prepare_import, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-project-id", required=True)
    parser.add_argument("--radius", type=int, choices=[32, 64, 128, 256], default=128)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if os.getenv("VECTOPLAN_LOD2_DIAGNOSTICS") == "1":
        faulthandler.dump_traceback_later(45, repeat=True)
    if args.report.exists():
        raise ValueError("Refusing to overwrite an existing import report")
    with app.app_context():
        world = (WorldInstance.query.options(noload("*")).join(Project, WorldInstance.project_db_id == Project.id)
                 .filter(Project.external_app_project_id == args.app_project_id, WorldInstance.world_id == "world_spawn").one())
        plan = prepare_import(world, radius=args.radius)
        report = {"appProjectId": args.app_project_id, "worldId": world.world_id, "applied": False, **summary(plan)}
        print(json.dumps({k: v for k, v in report.items() if k != "buildings"}), flush=True)
        if args.apply:
            report["result"] = apply_import(world, plan, progress=lambda value: print(json.dumps(value), flush=True))
            db.session.commit()
            report["applied"] = True
        else:
            db.session.rollback()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "buildings"}), flush=True)


if __name__ == "__main__":
    main()
