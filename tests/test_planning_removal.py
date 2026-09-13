from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4
import gzip
import json
import os
import pytest

from src.planning_removal import removal_commands, prepare_planning_removal


def obj(identity, *, parent=None, deleted=None, generation="current", object_type="building_roof"):
    return SimpleNamespace(object_instance_id=identity, object_type_id=object_type, deleted_at=deleted,
        metadata_json={"generationId": generation, "generatedFromAreaId": parent}, anchor_x=1, anchor_y=2, anchor_z=3)


def test_complete_removal_uses_authoritative_ownership_instead_of_incomplete_manifest():
    parent = obj("area", object_type="planning_build_area")
    parent.metadata_json["generatedObjects"] = [{"objectInstanceId": "one"}]
    objects = [obj("one", parent="area"), obj("orphan", parent="area"), obj("neighbour", parent="other"),
               obj("retired", parent="area", deleted="yesterday"), obj("manual")]
    result = removal_commands(parent, objects, "current")
    assert [item["objectInstanceId"] for item in result] == ["one", "orphan", "area"]
    assert all(item["type"] == "RemoveObject" for item in result)
    assert parent.deleted_at is None


@pytest.mark.parametrize("parent,generation", [(None, "current"), (obj("area", deleted="yesterday"), "current"),
    (obj("area", object_type="planning_build_area"), "old"), (obj("roof"), "current")])
def test_missing_stale_or_nonbuilding_parent_is_rejected(parent, generation):
    with pytest.raises(ValueError):
        removal_commands(parent, [], generation)


@pytest.mark.parametrize("children", [[], [{"type": "RemoveObject", "objectInstanceId": "foreign"}],
    [{"type": "RemoveObject", "objectInstanceId": "area"}, {"type": "RemoveObject", "objectInstanceId": "foreign"}],
    [{"type": "PlaceObject", "objectInstanceId": "area"}]])
def test_descriptor_cannot_smuggle_unrelated_or_placement_commands(children):
    with pytest.raises(ValueError, match="matching root removal"):
        prepare_planning_removal(project=None, universe=None, world=None, command_log=None,
            payload={"planningBuildingRemoval": {"parentObjectInstanceId": "area"}}, children=children)


def test_ordinary_roof_commands_preserve_existing_deletion_and_history_path():
    children = [{"type": "RemoveObject", "objectInstanceId": "roof"}]
    assert prepare_planning_removal(project=None, universe=None, world=None, payload={}, children=children,
        command_log=None) == (children, None, None)


@pytest.mark.skipif(os.getenv("VECTOPLAN_RUN_PLANNING_DB_TESTS") != "1", reason="explicit isolated rollback replay")
def test_real_berlin_building_removal_is_atomic_complete_idempotent_and_keeps_foreign_objects():
    from wsgi import app
    from extensions import db
    from models import Project, Universe, WorldInstance, WorldObjectInstance, WorldCommandLog, ChunkSnapshot
    from routes import commands
    from sqlalchemy.orm import noload
    source = sorted(json.loads(gzip.decompress((Path(__file__).parent / "fixtures/berlin-planning-batches.json.gz").read_bytes())), key=lambda item: item["id"])[0]["request"]
    with app.app_context(), app.test_request_context("/"):
        try:
            base = WorldInstance.query.options(noload("*")).filter(WorldInstance.deleted_at.is_(None)).first()
            project = db.session.get(Project, base.project_db_id, options=[noload("*")])
            universe = db.session.get(Universe, base.universe_db_id, options=[noload("*")])
            nonce = uuid4().hex
            world = WorldInstance.create(project_db_id=project.id, universe_db_id=universe.id,
                world_id=f"world_remove_{nonce}", slug=f"remove-{nonce}", name="Uncommitted Berlin deletion regression",
                world_role="sandbox", template_id="flat", provider_id="flat", provider_world_id="flat",
                block_registry_id=base.block_registry_id, block_registry_version=base.block_registry_version)
            db.session.add(world); db.session.flush()
            def run(payload):
                return commands._execute_command(project=project, universe=universe, world=world, payload=payload)
            creation = deepcopy(source); parent = creation["commands"][-1]
            identity = parent["objectInstanceId"]
            creation["commandId"] = f"remove_create_{nonce}"
            creation["planningBuildingEdit"] = {"parentObjectInstanceId": identity, "previousGenerationId": None}
            run(creation)
            # An independent object overlapping a generated block must survive
            # both deletion and its repeated receipt request.
            neighbour = deepcopy(creation["commands"][0]); neighbour["objectInstanceId"] = f"foreign_{nonce}"
            neighbour["metadata"] = {**neighbour["metadata"], "generatedFromAreaId": "other-parent"}
            neighbour["commandId"] = f"neighbour_{nonce}"; run(neighbour)
            payload = {"type": "ObjectBatch", "commandId": f"remove_{nonce}", "position": parent["position"],
                "planningBuildingRemoval": {"parentObjectInstanceId": identity, "previousGenerationId": parent["metadata"]["generationId"]},
                "commands": [{"type": "RemoveObject", "objectInstanceId": identity, "position": parent["position"]}]}
            with pytest.raises(ValueError, match="inzwischen geändert"):
                with db.session.begin_nested():
                    bad = deepcopy(payload); bad["commandId"] += "_stale"; bad["planningBuildingRemoval"]["previousGenerationId"] = "stale"
                    run(bad)
            log, result = run(payload)
            live = commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None))).all()
            assert [item.object_instance_id for item in live] == [neighbour["objectInstanceId"]]
            assert len(result["objectBatch"]["results"]) == len(creation["commands"])
            assert log.affected_cells_json, "command history retains actual before/after cells"
            again, replay = run(deepcopy(payload))
            assert again.id == log.id and replay["replayed"]
            for snapshot in ChunkSnapshot.query.options(noload("*")).filter_by(world_db_id=world.id):
                assert all(ref["objectInstanceId"] == neighbour["objectInstanceId"] for ref in snapshot.object_refs_json or [])
        finally:
            db.session.rollback()


@pytest.mark.skipif(os.getenv("VECTOPLAN_RUN_PLANNING_DB_TESTS") != "1", reason="explicit isolated rollback replay")
@pytest.mark.parametrize("delete_roof_first", [False, True])
def test_real_unconverted_lod2_removal_cleans_only_proven_original_walls_and_is_atomic(monkeypatch, delete_roof_first):
    from wsgi import app
    from extensions import db
    from models import Project, Universe, WorldInstance, WorldObjectInstance
    from routes import commands
    from sqlalchemy.orm import noload
    from src.geodata.lod2_conversion import _facade_wall_cells, WALL_BLOCK_ID
    roof = json.loads(gzip.decompress((Path(__file__).parent / "fixtures/berlin-lod2-removal.json.gz").read_bytes()))
    building_id = roof["metadata"]["lod2BuildingId"]
    source = roof["metadata"]["roofParameters"]["importedSource"]
    keys = sorted(_facade_wall_cells(source["facadeSegments"]))
    assert len(keys) > 50
    with app.app_context(), app.test_request_context("/"):
        try:
            base = WorldInstance.query.options(noload("*")).filter(WorldInstance.deleted_at.is_(None)).first()
            project = db.session.get(Project, base.project_db_id, options=[noload("*")])
            universe = db.session.get(Universe, base.universe_db_id, options=[noload("*")])
            nonce = uuid4().hex
            world = WorldInstance.create(project_db_id=project.id, universe_db_id=universe.id,
                world_id=f"world_lod2remove_{nonce}", slug=f"lod2remove-{nonce}", name="Uncommitted real LoD2 removal",
                world_role="sandbox", template_id="flat", provider_id="flat", provider_world_id="flat",
                block_registry_id=base.block_registry_id, block_registry_version=base.block_registry_version)
            db.session.add(world); db.session.flush()
            def run(payload):
                return commands._execute_command(project=project, universe=universe, world=world, payload=payload)
            position = lambda key: dict(zip(("x", "y", "z"), key))
            run({"type":"WorldEdit", "tool":"clipboard", "operation":"paste", "position":{"x":0,"y":0,"z":0},
                "commandSource":"importer", "userId":"system_lod2_import",
                "clipboard":[{"dx":x,"dy":y,"dz":z,"blockTypeId":WALL_BLOCK_ID} for x,y,z in keys]})
            run({**roof, "type":"PlaceObject", "position":roof["anchor"], "blockTypeId":WALL_BLOCK_ID,
                 "objectSource":"importer", "commandSource":"importer", "userId":"system_lod2_import"})
            run({"type":"RemoveBlock", "position":position(keys[0]), "userId":"editor_user"})
            # Same material can also be a deliberate independent user edit.
            run({"type":"RemoveBlock", "position":position(keys[1]), "userId":"editor_user"})
            run({"type":"SetBlock", "position":position(keys[1]), "blockTypeId":WALL_BLOCK_ID, "userId":"editor_user"})
            run({"type":"PlaceObject", "position":position(keys[2]), "objectInstanceId":f"foreign_{nonce}",
                 "blockTypeId":WALL_BLOCK_ID, "objectTypeId":"user_marker", "dimensions":{"x":1,"y":1,"z":1}})
            active_roof_id = roof["objectInstanceId"]
            if delete_roof_first:
                _, deleted = run({"type":"RemoveObject", "commandId":f"single_roof_{nonce}",
                    "objectInstanceId":active_roof_id, "position":roof["anchor"], "preserveLod2Facade":True})
                assert not deleted["affectedCells"], "single roof deletion must not alter original walls"
                from src.geodata.lod2_building_edit import read_lod2_building_objects
                inventory=read_lod2_building_objects(world=world,building_id=building_id)
                assert len(inventory["objectRefs"])==1
                facade=inventory["objectRefs"][0]
                assert facade["objectTypeId"]=="building_facade_source"
                assert facade["metadata"]["roofParameters"]["importedSource"]["facadeSegments"]==source["facadeSegments"]
                assert inventory["originalRoofObjectIds"]==[roof["objectInstanceId"]]
                assert roof["objectInstanceId"] in world.metadata_json["lod2Buildings"]["removedRoofObjectIds"]
                active_roof_id=facade["objectInstanceId"]
            identity = f"lod2_building_{building_id}"
            payload = {"type":"ObjectBatch", "commandId":f"lod2_removal_{nonce}", "position":roof["anchor"],
                "planningBuildingRemoval":{"parentObjectInstanceId":identity,"previousGenerationId":None,
                    "lod2BuildingId":building_id,"roofObjectIds":[active_roof_id],"originalRoofObjectIds":[roof["objectInstanceId"]]},
                "commands":[{"type":"RemoveObject","objectInstanceId":identity,"position":roof["anchor"]}]}
            original_remove=commands._execute_remove_object
            with monkeypatch.context() as patch:
                def fail(**kwargs):
                    raise ValueError("forced failure after original wall cleanup")
                patch.setattr(commands,"_execute_remove_object",fail)
                with pytest.raises(ValueError,match="forced failure"):
                    with db.session.begin_nested(): run(payload)
            assert commands._query_without_relationships(WorldObjectInstance.query.filter_by(world_db_id=world.id,
                object_instance_id=active_roof_id)).one().deleted_at is None
            _, result=run(payload)
            assert result["lod2BuildingEdit"]["clearedOriginalWallCellCount"] == len(keys)-3
            objects=commands._query_without_relationships(WorldObjectInstance.query.filter_by(world_db_id=world.id)).all()
            assert next(obj for obj in objects if obj.object_instance_id==roof["objectInstanceId"]).deleted_at is not None
            assert next(obj for obj in objects if obj.object_instance_id==active_roof_id).deleted_at is not None
            assert next(obj for obj in objects if obj.object_instance_id==f"foreign_{nonce}").deleted_at is None
            assert all(obj.object_instance_id!=identity for obj in objects), "validation parent must never be stored"
            assert {tuple(cell[axis] for axis in ("x","y","z")) for cell in result["lod2BuildingEdit"]["preservedCells"]} >= set(keys[:3])
        finally:
            db.session.rollback()
