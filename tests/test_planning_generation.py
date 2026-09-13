"""Receipts and actual Berlin generation replay in an uncommitted test world."""
from copy import deepcopy
import gzip
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.planning_generation import prepare_planning_generation, replay_result


def _berlin_batches():
    return sorted(json.loads(gzip.decompress(
        (Path(__file__).parent / "fixtures/berlin-planning-batches.json.gz").read_bytes())),
        key=lambda entry: entry["id"])


@pytest.mark.parametrize("command_id", [951, 953, 955])
def test_old_browser_cannot_commit_real_berlin_generations_without_atomic_descriptor(command_id):
    payload = next(entry["request"] for entry in _berlin_batches() if entry["id"] == command_id)
    assert "planningBuildingEdit" not in payload
    with pytest.raises(ValueError, match="Seite neu laden"):
        prepare_planning_generation(None, payload, payload["commands"])


@pytest.mark.parametrize("ownership", ["child", "manifest"])
def test_legacy_generation_guard_recognizes_both_ownership_contracts(ownership):
    parent = {"type": "PlaceObject", "objectTypeId": "planning_build_area",
              "objectInstanceId": "area", "metadata": {"voxelOccupancy": "none"}}
    child = {"type": "PlaceObject", "objectInstanceId": "roof", "metadata": {}}
    if ownership == "child":
        child["metadata"]["generatedFromAreaId"] = "area"
    else:
        parent["metadata"]["generatedObjects"] = [{"objectInstanceId": "roof"}]
    with pytest.raises(ValueError, match="planningBuildingEdit fehlt"):
        prepare_planning_generation(None, {}, [child, parent])


def test_generation_guard_preserves_metadata_repairs_and_independent_roof_edits():
    payload = _berlin_batches()[0]["request"]
    parent = payload["commands"][-1]
    parent["metadata"]["retiredGeneratedObjects"] = []
    for children in ([parent], [{"type": "RemoveObject", "objectInstanceId": "old-roof"}, parent],
                     [payload["commands"][0]], [parent, {"type": "PlaceObject", "objectInstanceId": "unrelated"}]):
        assert prepare_planning_generation(None, {}, children) is children


def test_converted_lod2_read_excludes_live_orphan_roof_generations():
    from src.geodata.lod2_building_edit import current_building_roof_inventory
    roofs = [SimpleNamespace(object_instance_id=value, deleted_at=None) for value in ("old-roof", "current-roof")]
    assert current_building_roof_inventory(roofs) == roofs
    assert current_building_roof_inventory(roofs, {"generatedObjects": [{"objectInstanceId": "current-roof"}]}) == roofs[1:]
    assert current_building_roof_inventory(roofs, {"generatedObjects": []}) == []


def test_receipt_replays_only_the_identical_request():
    payload = {"commandId": "same", "type": "ObjectBatch", "commands": [{"objectInstanceId": "roof"}]}
    command = SimpleNamespace(request_payload_json=deepcopy(payload), command_status="applied", command_type="ObjectBatch",
        result_payload_json={"dirtyChunks": ["1:0:2"], "changed": True})
    assert replay_result(command, payload)["dirtyChunks"] == ["1:0:2"]
    changed = deepcopy(payload); changed["commands"][0]["objectInstanceId"] = "different"
    with pytest.raises(ValueError, match="different command"):
        replay_result(command, changed)


@pytest.mark.skipif(os.getenv("VECTOPLAN_RUN_PLANNING_DB_TESTS") != "1", reason="explicit rollback DB replay")
def test_real_berlin_four_to_five_floors_retires_all_generations_atomically(monkeypatch):
    from wsgi import app
    from extensions import db
    from models import Project, Universe, WorldInstance, WorldObjectInstance, WorldCommandLog, ChunkSnapshot
    from routes import commands
    from sqlalchemy.orm import noload
    source = _berlin_batches()
    assert [entry["id"] for entry in source] == [951, 953, 955]
    assert source[0]["request"]["commands"][-1]["metadata"]["storeyCount"] == 4
    with app.app_context(), app.test_request_context("/"):
        try:
            base = WorldInstance.query.options(noload("*")).filter(WorldInstance.deleted_at.is_(None)).first()
            assert base is not None
            project = db.session.get(Project, base.project_db_id, options=[noload("*")])
            universe = db.session.get(Universe, base.universe_db_id, options=[noload("*")])
            nonce = uuid4().hex
            world = WorldInstance.create(project_db_id=project.id, universe_db_id=universe.id,
                world_id=f"world_receipt_{nonce}", slug=f"receipt-{nonce}", name="Uncommitted real Berlin replay",
                world_role="sandbox", template_id="flat", provider_id="flat", provider_world_id="flat",
                block_registry_id=base.block_registry_id, block_registry_version=base.block_registry_version,
                metadata_json={"testScope": "planning-generation-rollback"})
            db.session.add(world); db.session.flush()
            def run(payload):
                return commands._execute_command(project=project, universe=universe, world=world, payload=payload)
            first = deepcopy(source[0]["request"])
            first["commandId"] = f"receipt_first_{nonce}"
            first_parent = first["commands"][-1]
            parent_id = first_parent["objectInstanceId"]
            first["planningBuildingEdit"] = {"parentObjectInstanceId": parent_id, "previousGenerationId": None}
            log1, _ = run(first)
            # Reproduce the real defect: a successful old client batch left an
            # orphan roof/body generation whose parent retirement list was stale.
            orphan = deepcopy(source[1]["request"])
            orphan["commandId"] = f"receipt_orphan_{nonce}"
            # Only this historical setup uses the pre-deployment server path.
            # A live old-browser request must now fail before changing objects.
            with pytest.raises(ValueError, match="Seite neu laden"):
                with db.session.begin_nested():
                    run(orphan)
            assert {obj.object_instance_id for obj in commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None))).all()} == {
                    child["objectInstanceId"] for child in first["commands"]}
            with monkeypatch.context() as historical_server:
                historical_server.setattr("src.planning_generation.prepare_planning_generation",
                                          lambda world, payload, children: children)
                run(orphan)
            current_generation = orphan["commands"][-1]["metadata"]["generationId"]
            replacement = deepcopy(source[2]["request"])
            replacement["commandId"] = f"receipt_replacement_{nonce}"
            replacement["planningBuildingEdit"] = {"parentObjectInstanceId": parent_id,
                "previousGenerationId": current_generation}
            # Deliberately keep the historical stale list: server ownership
            # must find both old generations independently of those ten refs.
            assert len(replacement["commands"][-1]["metadata"]["retiredGeneratedObjects"]) == 10
            replacement["commands"][-1]["metadata"]["retiredGeneratedObjects"] = []
            log2, result = run(replacement)
            new_ids = {c["objectInstanceId"] for c in replacement["commands"]}
            live = commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None))).all()
            assert {obj.object_instance_id for obj in live} == new_ids
            parent = next(obj for obj in live if obj.object_instance_id == parent_id)
            assert parent.metadata_json["storeyCount"] == 5
            assert len([obj for obj in live if obj.object_type_id == "building_roof"]) == 2
            assert len([child for child in result["objectBatch"]["results"] if child["commandType"] == "RemoveObject"]) == 22
            logs_before = WorldCommandLog.query.filter_by(world_db_id=world.id).count()
            replay_log, replay = run(deepcopy(replacement))
            assert replay_log.id == log2.id and replay["replayed"]
            assert WorldCommandLog.query.filter_by(world_db_id=world.id).count() == logs_before
            with pytest.raises(ValueError, match="different command"):
                different = deepcopy(replacement); different["commands"][-1]["metadata"]["storeyCount"] = 4
                run(different)
            # Current chunk refs may be compact but must carry no old identities.
            for snapshot in ChunkSnapshot.query.options(noload("*")).filter_by(world_db_id=world.id):
                assert {r["objectInstanceId"] for r in snapshot.object_refs_json or []} <= new_ids
            # A receipt read never executes/materializes another generation.
            response, status = commands.get_command_status(project.project_id, world.world_id, log2.command_id)
            assert status == 200 and response.get_json()["commandStatus"] == "applied"
            assert "commands" not in response.get_json()
            unknown, _ = commands.get_command_status(project.project_id, world.world_id, "not-yet-confirmed")
            assert unknown.get_json()["commandStatus"] == "unconfirmed"
            broken = deepcopy(replacement)
            broken["commandId"] = f"receipt_broken_{nonce}"
            broken["planningBuildingEdit"]["previousGenerationId"] = parent.metadata_json["generationId"]
            for child in broken["commands"]:
                if child["objectInstanceId"] != parent_id:
                    child["objectInstanceId"] += "_broken"
            for ref in broken["commands"][-1]["metadata"]["generatedObjects"]:
                ref["objectInstanceId"] += "_broken"
            broken["commands"][1]["blockTypeId"] = "missing_generation_test_material"
            with pytest.raises((ValueError, LookupError)):
                with db.session.begin_nested():
                    run(broken)
            assert {obj.object_instance_id for obj in commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None))).all()} == new_ids
        finally:
            db.session.rollback()
