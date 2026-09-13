"""Read-only, evidence-only audit of terrain removed by selected planning parents.

No terrain generator is invoked and no database repair path exists in this script.
"""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--world", default="world_spawn")
    parser.add_argument("--parent", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= len(args.parent) <= 10:
        raise ValueError("Select at most ten explicit planning parents.")
    from flask import Flask
    from sqlalchemy import text
    from extensions import db
    from routes import commands
    from models import WorldObjectInstance
    from models.chunk import extract_cells_from_content

    app = Flask("planning-terrain-read-only-audit")
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI=os.environ["DATABASE_URL"],
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context(), app.test_request_context("/"):
        try:
            db.session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            db.session.execute(text("SET LOCAL statement_timeout = '45s'"))
            project, universe, world = commands._resolve_project_world_context(args.project, args.world)
            parents = commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id,
                WorldObjectInstance.object_instance_id.in_(args.parent),
                WorldObjectInstance.object_type_id == "planning_build_area")).all()
            if {parent.object_instance_id for parent in parents} != set(args.parent):
                raise ValueError("An explicit parent is missing.")
            objects = db.session.query(WorldObjectInstance.object_instance_id,
                WorldObjectInstance.metadata_json["generatedFromAreaId"].astext).filter(
                WorldObjectInstance.world_db_id == world.id,
                WorldObjectInstance.metadata_json["generatedFromAreaId"].astext.in_(args.parent)).all()
            owners = {identity: parent for identity, parent in objects}
            owners.update({parent: parent for parent in args.parent})
            # Extract historical parent footprints only, not full construction
            # geometry (which can be tens of MB in one command payload).
            footprints = {parent.object_instance_id: [parent.footprint_json] for parent in parents}
            historical = db.session.execute(text("""
                SELECT item.value->>'objectInstanceId' AS parent, item.value->'footprint' AS footprint
                FROM world_command_logs log
                CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(log.request_payload_json->'commands')='array'
                  THEN log.request_payload_json->'commands' ELSE '[]'::jsonb END) item
                WHERE log.world_db_id=:world AND log.command_status IN ('applied','noop')
                  AND item.value->>'objectInstanceId'=ANY(:parents)
                  AND item.value->>'objectTypeId'='planning_build_area'
            """), {"world": world.id, "parents": args.parent}).mappings().all()
            for item in historical:
                footprints[item["parent"]].append(item["footprint"])

            def points(value):
                if isinstance(value, list):
                    if len(value) >= 2 and all(isinstance(v, (float, int)) for v in value[:2]):
                        yield value[0], value[1]
                    else:
                        for child in value:
                            yield from points(child)

            bounds = {}
            relevant_columns = set()
            for parent, shapes in footprints.items():
                coords = [p for shape in shapes if isinstance(shape, dict) for p in points(shape.get("coordinates"))]
                if not coords:
                    raise ValueError(f"No footprint evidence for {parent}.")
                xs, zs = zip(*coords)
                bounds[parent] = [min(xs), min(zs), max(xs), max(zs)]
                for x in range(int(min(xs) // world.chunk_size), int(max(xs) // world.chunk_size) + 1):
                    for z in range(int(min(zs) // world.chunk_size), int(max(zs) // world.chunk_size) + 1):
                        relevant_columns.add((x, z))
            headers = db.session.execute(text("""
                SELECT id,command_id,command_status,command_type,affected_chunks_json,affected_cell_count,
                  jsonb_array_length(affected_cells_json) AS stored_count
                FROM world_command_logs WHERE world_db_id=:world AND command_status IN ('applied','noop') ORDER BY id
            """), {"world": world.id}).mappings().all()

            def relevant(header):
                for chunk in header["affected_chunks_json"] or []:
                    try:
                        x, _, z = (int(v) for v in chunk.split(":"))
                    except (ValueError, AttributeError):
                        continue
                    if (x, z) in relevant_columns:
                        return True
                return False

            selected_headers = [row for row in headers if relevant(row)]
            incomplete = [row["command_id"] for row in selected_headers if row["stored_count"] != row["affected_cell_count"]]
            log_ids = [row["id"] for row in selected_headers]
            # Candidate creation requires actual pre-placement terrain, an
            # explicit generated-child identity, and the historical footprint.
            before = db.session.execute(text("""
                SELECT log.id,log.command_id,entry.value AS cell
                FROM world_command_logs log
                CROSS JOIN LATERAL jsonb_path_query(log.affected_cells_json,
                    '$[*] ? (@.beforeBlockTypeId like_regex "^system_terrain(_|$)")') entry(value)
                WHERE log.world_db_id=:world AND log.id=ANY(:ids)
                  AND entry.value->>'objectInstanceId'=ANY(:owners) ORDER BY log.id
            """), {"world": world.id, "ids": log_ids, "owners": list(owners)}).mappings().all() if log_ids else []
            candidates = {}
            for row in before:
                cell = row["cell"]
                identity = cell["objectInstanceId"]
                parent = owners[identity]
                x, y, z = (int(cell[axis]) for axis in ("x", "y", "z"))
                low_x, low_z, high_x, high_z = bounds[parent]
                if x + 1 < low_x or x > high_x or z + 1 < low_z or z > high_z:
                    continue
                if not cell.get("afterBlockTypeId") or cell["afterBlockTypeId"].startswith("system_terrain"):
                    continue
                key = (x, y, z)
                candidates.setdefault(key, {"parent": parent, "terrainBlockTypeId": cell["beforeBlockTypeId"],
                    "firstPlacementLog": row["id"], "firstPlacementCommand": row["command_id"]})
            plan = []
            excluded = []
            if candidates and not incomplete:
                # Only candidate coordinates leave PostgreSQL; still verify the
                # full saved list length above, including no-op manual edits.
                cells = db.session.execute(text("""
                    SELECT log.id,log.command_id,log.command_type,entry.value AS cell,entry.ordinality AS ordinal
                    FROM world_command_logs log
                    CROSS JOIN LATERAL jsonb_array_elements(log.affected_cells_json) WITH ORDINALITY entry(value,ordinality)
                    WHERE log.world_db_id=:world AND log.id=ANY(:ids)
                      AND ((entry.value->>'x')||':'||(entry.value->>'y')||':'||(entry.value->>'z'))=ANY(:cells)
                    ORDER BY log.id,entry.ordinality
                """), {"world": world.id, "ids": log_ids,
                       "cells": [":".join(str(v) for v in key) for key in candidates]}).mappings().all()
                histories = {key: [] for key in candidates}
                for row in cells:
                    c = row["cell"]
                    histories[tuple(int(c[axis]) for axis in ("x", "y", "z"))].append(row)
                for key, evidence in candidates.items():
                    history = [row for row in histories[key] if row["id"] >= evidence["firstPlacementLog"]]
                    manual = any(row["cell"].get("objectInstanceId") not in owners for row in history)
                    last = history[-1] if history else None
                    if manual or not last or last["cell"].get("afterBlockTypeId") is not None:
                        excluded.append({"cell": key, "reason": "later-edit-or-not-final-air"})
                        continue
                    final = last["cell"]
                    if final.get("afterCellValue") != 0 or final.get("objectInstanceId") not in owners:
                        excluded.append({"cell": key, "reason": "no-proven-generated-removal"})
                        continue
                    routed = commands._world_position_to_chunk_cell(dict(zip(("x", "y", "z"), key)), world.chunk_size)
                    snapshot = commands._find_chunk_snapshot(world=world, chunk_x=routed["chunkX"],
                        chunk_y=routed["chunkY"], chunk_z=routed["chunkZ"])
                    if snapshot is None:
                        excluded.append({"cell": key, "reason": "no-persisted-snapshot"})
                        continue
                    # Inspect only durable cells. Runtime materialization can
                    # upgrade terrain and would replace the persisted evidence.
                    stored_cells = extract_cells_from_content(snapshot.content_json)
                    if len(stored_cells) != world.chunk_size ** 3:
                        excluded.append({"cell": key, "reason": "no-complete-persisted-cells"})
                        continue
                    index = commands._flatten_cell_index(routed["localX"], routed["localY"],
                        routed["localZ"], world.chunk_size)
                    current = stored_cells[index]
                    if type(current) is not int or current != 0:
                        excluded.append({"cell": key, "reason": "current-snapshot-not-air"})
                        continue
                    plan.append({"cell": key, **evidence, "lastRemovalCommand": last["command_id"],
                        "snapshotId": snapshot.id, "snapshotVersion": snapshot.chunk_version,
                        "restoration": "material-only; historical cut/full flag not inferred"})
            output = {"readOnly": True, "project": args.project, "world": args.world,
                "parentIds": args.parent, "parentFootprintBounds": bounds, "historicalFootprintCount": len(historical),
                "generatedObjectCount": len(objects), "scopedCommandCount": len(selected_headers),
                "incompleteCommandIds": incomplete, "terrainBeforeBuildingRecords": len(before),
                "candidateCellCount": len(candidates), "provenHoleCount": len(plan), "plan": plan,
                "excluded": excluded, "repairExecuted": False}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({key: value for key, value in output.items() if key not in ("plan", "excluded")}, ensure_ascii=False))
        finally:
            db.session.rollback()


if __name__ == "__main__":
    main()
