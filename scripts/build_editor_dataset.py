"""Build an immutable, versioned Editor dataset for one concrete project.

This command performs no world mutation.  LoD2 materialization continues to use
``import_lod2_editable.py --apply`` so every block and roof remains represented
by canonical WorldEdit commands and receipts.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, os.getcwd())
os.environ["VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS"] = "false"

from sqlalchemy.orm import noload

from models.project import Project
from models.world import WorldInstance
from src.editor_dataset import activate_editor_dataset, build_editor_dataset, write_editor_dataset
from src.geodata.lod2_import import prepare_import
from src.geodata.visual_overlays import get_default_geodata_overlay_service
from src.georeferencing.frame_contract import earth_grid_frame_contract
from wsgi import app


def _street_features(world, provider, *, center_x: int, center_z: int, radius: int):
    service = get_default_geodata_overlay_service()
    chunk_size = int(world.chunk_size)
    result = []
    parcel_boundaries = []
    min_chunk_x = math.floor((center_x - radius) / chunk_size)
    max_chunk_x = math.floor((center_x + radius - 1) / chunk_size)
    min_chunk_z = math.floor((center_z - radius) / chunk_size)
    max_chunk_z = math.floor((center_z + radius - 1) / chunk_size)
    for chunk_x in range(min_chunk_x, max_chunk_x + 1):
        for chunk_z in range(min_chunk_z, max_chunk_z + 1):
            contract = service.chunk_contract(
                world=world,
                provider=provider,
                chunk_x=chunk_x,
                chunk_z=chunk_z,
                chunk_size=chunk_size,
            )
            availability = next(
                (
                    item
                    for item in contract.get("availability", [])
                    if isinstance(item, dict) and item.get("id") == "street-network"
                ),
                None,
            )
            if not isinstance(availability, dict):
                raise RuntimeError(
                    "The street-network overlay is disabled or missing; refusing a partial Editor dataset"
                )
            if availability.get("status") == "unavailable":
                errors = [
                    str(item.get("message") or "")
                    for item in contract.get("errors", [])
                    if isinstance(item, dict) and item.get("id") == "street-network"
                ]
                detail = errors[0] if errors else "unknown WFS error"
                raise RuntimeError(
                    f"The spatial street-network request failed for chunk {chunk_x}:0:{chunk_z}: {detail}"
                )
            parcel_availability = next(
                (
                    item
                    for item in contract.get("availability", [])
                    if isinstance(item, dict) and item.get("id") == "parcel-boundaries"
                ),
                None,
            )
            if not isinstance(parcel_availability, dict) or parcel_availability.get("status") == "unavailable":
                errors = [
                    str(item.get("message") or "")
                    for item in contract.get("errors", [])
                    if isinstance(item, dict) and item.get("id") == "parcel-boundaries"
                ]
                detail = errors[0] if errors else "parcel-boundary overlay unavailable"
                raise RuntimeError(
                    f"The road-width boundary request failed for chunk {chunk_x}:0:{chunk_z}: {detail}"
                )
            for item in contract.get("items", []):
                if item.get("semanticRole") == "parcel-boundary":
                    geometry = item.get("geometry") or {}
                    parcel_boundaries.extend(geometry.get("coordinates") or [])
                    continue
                if item.get("semanticRole") != "street-network":
                    continue
                geometry = item.get("geometry") or {}
                for index, centerline in enumerate(geometry.get("coordinates") or []):
                    result.append({
                        "featureId": f"{item.get('releaseKey')}:{item.get('tileKey')}:{index}",
                        "sourceDataset": item.get("datasetId") or "strassendaten",
                        "centerline": centerline,
                        "nominalWidthM": 6.0,
                    })
    return result, parcel_boundaries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-project-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--radius", type=int, choices=(32, 64, 128, 256), default=128)
    parser.add_argument("--center-x", type=int, default=0)
    parser.add_argument("--center-z", type=int, default=0)
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Atomically select this immutable bundle for the resolved project/world after writing it.",
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=None,
        help="Persistent dataset root used by --activate (defaults to service configuration).",
    )
    args = parser.parse_args()

    with app.app_context():
        world = (
            WorldInstance.query.options(noload("*"))
            .join(Project, WorldInstance.project_db_id == Project.id)
            .filter(
                Project.external_app_project_id == args.app_project_id,
                WorldInstance.world_id == "world_spawn",
            )
            .one()
        )
        project = Project.query.options(noload("*")).filter(Project.id == world.project_db_id).one()
        provider = world.build_earth_provider()
        lod2_plan = prepare_import(
            world,
            radius=args.radius,
            center_x=args.center_x,
            center_z=args.center_z,
        )
        streets, road_surface_boundaries = _street_features(
            world,
            provider,
            center_x=args.center_x,
            center_z=args.center_z,
            radius=args.radius,
        )
        coordinate_frame = earth_grid_frame_contract(provider)
        if coordinate_frame is None:
            raise RuntimeError("The project has no immutable Earth-grid frame")
        dataset = build_editor_dataset({
            "datasetId": f"editor:{args.app_project_id}:world_spawn",
            "referenceFingerprint": provider.reference_fingerprint,
            "coordinateFrame": coordinate_frame,
            "sourceBounds": list(lod2_plan["bounds"]),
            "chunkSize": int(world.chunk_size),
            "lod2Plan": lod2_plan,
            "roadFeatures": streets,
            "roadSurfaceBoundaries": road_surface_boundaries,
            "defaultRoadWidthM": 6.0,
        })
        target = write_editor_dataset(dataset, args.output)
        selector = None
        if args.activate:
            selector = activate_editor_dataset(
                target,
                project_id=project.project_id,
                external_project_id=project.external_app_project_id,
                world_id=world.world_id,
                root=args.dataset_root,
            )
        print(target)
        print(dataset["contentFingerprint"])
        if selector is not None:
            print(f"activeSelector={selector}")
        print(f"chunks={len(dataset['chunks'])} roads={len(dataset['layers']['streetNetwork']['items'])}")


if __name__ == "__main__":
    main()
