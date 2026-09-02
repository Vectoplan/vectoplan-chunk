"""Activate one immutable Editor dataset for an existing App project world.

Examples::

    python scripts/activate_editor_dataset.py \
      --app-project-id prj_da09805bc6e54b29816c8cd6 \
      --dataset test1-berlin-20260901-v2-r64

``--dataset`` may be a bundle name relative to the persistent dataset root or
an absolute path below that root.  Paths outside the root are rejected.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.getcwd())
os.environ["VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS"] = "false"

from sqlalchemy.orm import noload

from extensions import db
from models.project import Project
from models.world import WorldInstance
from src.editor_dataset.runtime import (
    activate_editor_dataset,
    editor_dataset_lod2_plan,
    editor_dataset_root,
    load_editor_dataset_bundle,
)
from src.geodata.lod2_import import apply_import, summary
from wsgi import app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-project-id", required=True)
    parser.add_argument("--world-id", default="world_spawn")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Materialize validated walls/roofs through canonical WorldEdit commands, "
            "commit, then atomically activate the response projection. Without this "
            "flag the command is a read-only dry run."
        ),
    )
    args = parser.parse_args()

    root = editor_dataset_root(args.dataset_root)
    bundle = args.dataset if args.dataset.is_absolute() else root / args.dataset
    with app.app_context():
        project = (
            Project.query.options(noload("*"))
            .filter(Project.external_app_project_id == args.app_project_id)
            .one()
        )
        world = (
            WorldInstance.query.options(noload("*"))
            .filter(
                WorldInstance.project_db_id == project.id,
                WorldInstance.world_id == args.world_id,
                WorldInstance.deleted_at.is_(None),
            )
            .one()
        )
        active = load_editor_dataset_bundle(
            bundle,
            project_id=project.project_id,
            external_project_id=project.external_app_project_id,
            world_id=world.world_id,
            root=root,
        )
        if int(active.manifest.get("chunkSize") or 0) != int(world.chunk_size or 0):
            raise RuntimeError("Dataset chunkSize does not match the resolved project world")
        reference = str(world.build_earth_provider().reference_fingerprint or "")
        if active.reference_fingerprint != reference:
            raise RuntimeError("Dataset Earth reference does not match the resolved project world")
        plan = editor_dataset_lod2_plan(active)
        selector = None
        apply_result = None
        if args.apply:
            try:
                apply_result = apply_import(world, plan)
                db.session.commit()
            except BaseException:
                db.session.rollback()
                raise
            # Never publish a selector before the canonical transaction has
            # committed successfully. A failed import therefore cannot become
            # visible as an apparently active dataset.
            selector = activate_editor_dataset(
                bundle,
                project_id=project.project_id,
                external_project_id=project.external_app_project_id,
                world_id=world.world_id,
                root=root,
            )
        print(json.dumps({
            "ok": True,
            "mode": "apply-and-activate" if args.apply else "dry-run",
            "projectId": project.project_id,
            "externalAppProjectId": project.external_app_project_id,
            "worldId": world.world_id,
            "selector": str(selector) if selector is not None else None,
            "datasetRoot": str(root),
            "activeDataset": active.public_summary(),
            "lod2Plan": summary(plan),
            "applyResult": apply_result,
        }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
