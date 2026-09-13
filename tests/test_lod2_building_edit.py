"""LoD2 conversion must preserve user edits, including intentionally empty cells."""
from copy import deepcopy
from types import SimpleNamespace
import os
from uuid import uuid4

import pytest

from src.geodata.lod2_building_edit import (
    VALIDATION_VERSION, WALL_BLOCK_ID, classify_cell_history,
    filter_placements, plan_cell_protection,
    validate_imported_roof_inventory,
)


def _cell(x, *, block=WALL_BLOCK_ID):
    return {"x": x, "y": 2, "z": 0, "afterBlockTypeId": block}


def _log(cells, *, importer=False, marker=None, request=None):
    return SimpleNamespace(
        command_type="ObjectBatch" if marker or request else "WorldEdit",
        command_source="importer" if importer else "editor",
        user_id="system_lod2_import" if importer else "editor_user",
        affected_cells_json=cells, affected_cell_count=len(cells),
        result_payload_json={"lod2BuildingEdit": marker} if marker else {},
        request_payload_json={"lod2BuildingEdit": request} if request else {},
    )


def _marker(parent="parent", building="building"):
    return {"validationVersion": VALIDATION_VERSION, "buildingId": building,
            "parentObjectInstanceId": parent, "generatedObjectIds": ["previous_walls"]}


def test_history_preserves_mined_holes_and_even_edits_replaced_with_the_imported_material():
    candidates = {(x, 2, 0) for x in range(4)}
    logs = [_log([_cell(x) for x in range(4)], importer=True),
            _log([_cell(1, block=None), _cell(2)]),
            _log([_cell(3)], request=_marker())]
    imported, edited, generated = classify_cell_history(logs, candidates, building_id="building", parent_id="parent")
    assert imported == candidates
    assert edited == {(1, 2, 0), (2, 2, 0), (3, 2, 0)}
    assert generated == set()  # A caller-supplied descriptor is not validation.


def test_only_server_validated_generations_of_this_parent_are_exempt():
    logs = [_log([_cell(0)], marker=_marker()), _log([_cell(1)], marker=_marker(parent="other")),
            _log([_cell(2)], marker=_marker(building="other"))]
    _, edited, generated = classify_cell_history(logs, {(x, 2, 0) for x in range(3)},
                                                 building_id="building", parent_id="parent")
    assert edited == {(1, 2, 0), (2, 2, 0)}
    assert generated == {"previous_walls"}


def test_incomplete_history_fails_conservatively():
    log = _log([])
    log.affected_cell_count = 20
    candidates = {(0, 2, 0), (1, 2, 0)}
    _, edited, _ = classify_cell_history([log], candidates, building_id="building", parent_id="parent")
    assert edited == candidates


def test_first_conversion_requires_the_complete_authoritative_roof_inventory():
    validate_imported_roof_inventory({"roofObjectIds": ["main", "annex"]}, {"main", "annex"})
    for ids in (["main"], ["main", "annex", "unrelated"], ["main", "annex", "annex"], None):
        with pytest.raises(ValueError, match="Incomplete imported roof set"):
            validate_imported_roof_inventory({"roofObjectIds": ids}, {"main", "annex"})


def test_cleanup_requires_original_geometry_importer_proof_and_unchanged_ownership():
    candidates = {(x, 2, 0) for x in range(9)}
    states = {key: {"blockTypeId": WALL_BLOCK_ID} for key in candidates}
    states[(2, 2, 0)] = {"blockTypeId": "purple-user-block"}
    states[(3, 2, 0)] = {"blockTypeId": WALL_BLOCK_ID, "objectInstanceId": "foreign_object",
                        "expectedBlockTypeId": WALL_BLOCK_ID}
    states[(4, 2, 0)] = {"blockTypeId": "old-generation", "objectInstanceId": "previous_walls",
                        "expectedBlockTypeId": "old-generation"}
    states[(5, 2, 0)] = {"blockTypeId": None}
    states[(6, 2, 0)] = {"blockTypeId": None}  # Mined hole, protected by history.
    states[(7, 2, 0)] = {"blockTypeId": None, "isAir": False}  # Corrupt/unknown palette.
    protected, cleanup = plan_cell_protection(candidates, candidates, states,
        imported=candidates - {(8, 2, 0)}, edited={(1, 2, 0), (6, 2, 0)}, generated_ids={"previous_walls"})
    assert cleanup == {(0, 2, 0)}
    assert protected == {(x, 2, 0) for x in (1, 2, 3, 6, 7, 8)}


def _placement(object_id, positions):
    return {"type": "PlaceObject", "objectInstanceId": object_id, "position": dict(positions[0]),
            "occupiedCells": deepcopy(positions), "metadata": {"voxelOccupancy": "blocks",
                "wallCellCount": len(positions), "constructionCells": [
                    {**position, "corners": [[0, 0], [1, 0], [1, 1]]} for position in positions]}}


def test_filtering_keeps_shape_cells_in_sync_and_empty_ids_without_phantom_anchor_blocks():
    first, second = ({"x": x, "y": 2, "z": 0} for x in (0, 1))
    children = [_placement("partial", [first, second]), _placement("empty", [first]),
                {"type": "RemoveObject", "objectInstanceId": "old_roof"}]
    original = deepcopy(children)
    result, count = filter_placements(children, {(0, 2, 0)}, parent_id="parent")
    assert count == 2
    assert result[0]["occupiedCells"] == [second]
    assert [cell["x"] for cell in result[0]["metadata"]["constructionCells"]] == [1]
    assert result[0]["metadata"]["wallCellCount"] == 1
    assert result[1]["occupiedCells"] == [first]
    assert result[1]["metadata"]["voxelOccupancy"] == "none"
    assert result[1]["metadata"]["constructionCells"] == []
    assert result[1]["metadata"]["wallCellCount"] == 0
    assert result[2] == children[2]
    assert children == original


@pytest.fixture
def imported_building_world():
    if os.getenv("VECTOPLAN_RUN_DB_INTEGRATION_TESTS") != "1":
        pytest.skip("requires the disposable local Chunk integration database")
    from extensions import db
    from models import BlockType
    from models.world import WorldInstance
    from routes import commands
    from src.geodata.lod2_import import register_wall
    from tests.test_object_batch import _cell_state, _place_payload
    from flask import Flask

    # Integration tests need the existing schema and command executors, never
    # the production startup audit (which walks unrelated worlds/registries).
    app = Flask("lod2-building-edit-integration")
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI=os.environ["DATABASE_URL"],
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)

    with app.app_context(), app.test_request_context("/"):
        world_id = f"world_lod2_edit_test_{uuid4().hex}"
        try:
            project, universe, base = commands._resolve_project_world_context("dev-project", "world_spawn")
            project_id, universe_id = project.project_id, universe.universe_id
            world = WorldInstance.create(
                project_db_id=project.id, universe_db_id=universe.id, world_id=world_id,
                slug=world_id, name="LoD2 edit preservation integration test", world_role="sandbox",
                template_id="flat", provider_id="flat", provider_world_id="flat",
                block_registry_id=base.block_registry_id, block_registry_version=base.block_registry_version,
                metadata_json={"testScope": "lod2-building-edit", "lod2Buildings": {"enabled": True}})
            db.session.add(world)
            db.session.flush()
            register_wall(world)
            registry = commands._get_registry_for_world(world)
            blocks = commands._query_without_relationships(BlockType.query.filter(
                BlockType.registry_db_id == registry.id, BlockType.placeable.is_(True),
                BlockType.breakable.is_(True), BlockType.deleted_at.is_(None),
                BlockType.block_type_id != WALL_BLOCK_ID)).order_by(BlockType.block_type_id).limit(1).all()
            if not blocks:
                pytest.skip("dev registry needs an editable material")
            fill = blocks[0].block_type_id
            positions = [{"x": x, "y": 15, "z": 10} for x in range(10, 16)]
            def execute(payload):
                return commands._execute_command(project=project, universe=universe, world=world, payload=payload)
            execute({"type": "WorldEdit", "tool": "clipboard", "operation": "paste",
                "position": {"x": 0, "y": 0, "z": 0}, "commandSource": "importer", "userId": "system_lod2_import",
                "clipboard": [{"dx": p["x"], "dy": p["y"], "dz": p["z"], "blockTypeId": WALL_BLOCK_ID} for p in positions]})
            source = {"buildingId": "test-building", "facadeSegments": [{"start": [10, 10], "end": [16, 10],
                "minimumY": 15, "maximumY": 16, "topProfile": [[0, 16], [6, 16]], "bottomProfile": [[0, 15], [6, 15]]}]}
            execute({**_place_payload("original_roof", positions[0], WALL_BLOCK_ID),
                     "objectTypeId": "building_roof", "objectSource": "importer",
                     "commandSource": "importer", "userId": "system_lod2_import",
                     "metadata": {"voxelOccupancy": "none", "lod2BuildingId": "test-building",
                                  "roofParameters": {"importedSource": source}}})
            execute({"type": "RemoveBlock", "position": positions[1], "userId": "editor_user"})
            execute({"type": "SetBlock", "position": positions[2], "blockTypeId": fill, "userId": "editor_user"})
            execute(_place_payload("foreign_object", positions[3], WALL_BLOCK_ID))
            execute({"type": "SetBlock", "position": positions[4], "blockTypeId": fill})
            execute({"type": "SetBlock", "position": positions[4], "blockTypeId": WALL_BLOCK_ID})
            db.session.commit()
            # Fresh WorldInstances have joined Project/Universe relationships.
            # Refresh through the route's noload query after commit; otherwise
            # expiration can traverse the whole dev project's chunk graph.
            db.session.remove()
            project, universe, world = commands._resolve_project_world_context(
                project_id, world_id, universe_id=universe_id)
            def refresh():
                nonlocal project, universe, world
                db.session.remove()
                project, universe, world = commands._resolve_project_world_context(
                    project_id, world_id, universe_id=universe_id)
                return project, universe, world
            def state(position):
                return _cell_state(project, universe, world, position)
            def batch(generation="first"):
                from models import WorldObjectInstance
                stored_metadata = db.session.query(WorldObjectInstance.metadata_json).filter_by(
                    world_db_id=world.id, object_instance_id="parent").scalar() or {}
                wall = {**_place_payload(f"walls_{generation}", positions[0], fill),
                    "occupiedCells": deepcopy(positions),
                    "metadata": {"generatedFromAreaId": "parent", "renderProfile": "construction-grid",
                                 "voxelOccupancy": "blocks", "constructionCells": deepcopy(positions)}}
                empty = {**_place_payload(f"empty_{generation}", positions[1], fill),
                    "metadata": {"generatedFromAreaId": "parent", "renderProfile": "construction-grid",
                                 "voxelOccupancy": "blocks", "constructionCells": [dict(positions[1])]}}
                roof = {**_place_payload(f"roof_{generation}", positions[0], fill),
                    "objectTypeId": "building_roof", "metadata": {
                        "generatedFromAreaId": "parent", "voxelOccupancy": "none"}}
                parent = {**_place_payload("parent", positions[0], fill), "objectTypeId": "planning_build_area",
                    "metadata": {"voxelOccupancy": "none", "generationId": generation, "contourBuilding": {"source": {
                        "buildingId": "test-building", "roofObjectIds": ["original_roof"]}},
                                 "generatedObjects": [{"objectInstanceId": child["objectInstanceId"]}
                                                      for child in (wall, empty, roof)]}}
                return {"type": "ObjectBatch", "position": positions[0], "userId": "editor_user",
                        "lod2BuildingEdit": {"buildingId": "test-building", "parentObjectInstanceId": "parent"},
                        "planningBuildingEdit": {"parentObjectInstanceId": "parent",
                                                 "previousGenerationId": stored_metadata.get("generationId")},
                        "commands": [wall, empty, roof, parent]}
            yield SimpleNamespace(world=world, project=project, universe=universe, positions=positions,
                                  fill=fill, execute=execute, state=state, batch=batch, refresh=refresh)
        finally:
            db.session.rollback()
            db.session.remove()
            # The UUID identifies only this fixture's sandbox. Database FKs
            # cascade its small seed; avoid ORM backref graph loading on delete.
            WorldInstance.query.filter_by(world_id=world_id).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()


def test_conversion_preserves_live_cells_roof_and_mined_hole_across_generations(imported_building_world):
    from extensions import db
    from models import WorldObjectInstance
    from routes import commands
    fixture = imported_building_world
    log, result = fixture.execute(fixture.batch())
    assert result["lod2BuildingEdit"]["clearedOriginalWallCellCount"] == 2
    assert result["lod2BuildingEdit"]["filteredCellCount"] == 5
    assert [fixture.state(position)["blockTypeId"] for position in fixture.positions] == [
        fixture.fill, None, fixture.fill, WALL_BLOCK_ID, WALL_BLOCK_ID, fixture.fill]
    objects = {obj.object_instance_id: obj for obj in commands._query_without_relationships(
        WorldObjectInstance.query.filter_by(world_db_id=fixture.world.id)).all()}
    assert objects["original_roof"].deleted_at is not None
    assert objects["roof_first"].deleted_at is None
    assert objects["foreign_object"].deleted_at is None
    assert objects["empty_first"].metadata_json["voxelOccupancy"] == "none"
    assert objects["empty_first"].metadata_json["constructionCells"] == []
    assert objects["parent"].metadata_json["lod2BuildingEdit"] == result["lod2BuildingEdit"]
    response = commands._serialize_command_result(project=fixture.project, universe=fixture.universe,
        world=fixture.world, command_log=log, result=result, include_command_log=False)
    assert response["lod2BuildingEdit"] == result["lod2BuildingEdit"]
    db.session.flush()
    _, next_result = fixture.execute(fixture.batch("second"))
    assert next_result["lod2BuildingEdit"]["clearedOriginalWallCellCount"] == 0
    assert next_result["lod2BuildingEdit"]["filteredCellCount"] == 5
    assert fixture.state(fixture.positions[1])["blockTypeId"] is None
    content = fixture.state(fixture.positions[0])["content"]
    assert any(ref.get("objectInstanceId") == "walls_second" for ref in content["objectRefs"])
    # The read API returns this parent's current roof even outside camera
    # chunks; an orphan left by a legacy client must not revive old geometry.
    roof = fixture.batch("read") ["commands"][0]
    roof.update(objectInstanceId="generated_roof", objectTypeId="building_roof",
                occupiedCells=[fixture.positions[0]],
                metadata={"voxelOccupancy": "none", "generatedFromAreaId": "parent"})
    fixture.execute(roof)
    from src.geodata.lod2_building_edit import read_lod2_building_objects
    whole = read_lod2_building_objects(world=fixture.world, building_id="test-building")
    assert {ref["objectInstanceId"] for ref in whole["objectRefs"]} == {"roof_second"}
    assert whole["parentRef"]["objectInstanceId"] == "parent"
    assert whole["originalRoofObjectIds"] == ["original_roof"]
    assert all(ref["metadata"]["lod2BuildingId"] == "test-building" for ref in whole["objectRefs"])
    with pytest.raises(LookupError):
        read_lod2_building_objects(world=fixture.world, building_id="other-building")


def test_failed_conversion_rolls_back_original_cleanup_and_all_child_writes(imported_building_world, monkeypatch):
    from extensions import db
    from models import WorldObjectInstance
    from routes import commands
    fixture = imported_building_world
    before = deepcopy(fixture.state(fixture.positions[0])["content"])
    original_place = commands._execute_place_object
    observed = []
    def failing_place(**kwargs):
        # The initial cleanup already ran when the first child starts.
        if kwargs["payload"]["objectInstanceId"] == "walls_first":
            observed.append(fixture.state(fixture.positions[5])["blockTypeId"])
        if kwargs["payload"]["objectInstanceId"] == "empty_first":
            assert fixture.state(fixture.positions[0])["blockTypeId"] == fixture.fill
            raise ValueError("forced child failure after cleanup and placement")
        return original_place(**kwargs)
    monkeypatch.setattr(commands, "_execute_place_object", failing_place)
    with pytest.raises(ValueError, match="forced child failure"):
        fixture.execute(fixture.batch())
    assert observed == [None]
    db.session.rollback()
    fixture.project, fixture.universe, fixture.world = fixture.refresh()
    assert fixture.state(fixture.positions[0])["content"] == before
    assert commands._query_without_relationships(WorldObjectInstance.query.filter_by(
        world_db_id=fixture.world.id, object_instance_id="walls_first")).count() == 0


def test_conversion_rejects_mismatched_parent_before_any_cleanup(imported_building_world):
    fixture = imported_building_world
    payload = fixture.batch()
    payload["commands"][-1]["metadata"]["contourBuilding"]["source"]["buildingId"] = "unrelated-building"
    with pytest.raises(ValueError, match="parent contourBuilding source"):
        fixture.execute(payload)
    assert fixture.state(fixture.positions[0])["blockTypeId"] == WALL_BLOCK_ID


def test_http_compressed_object_batch_executes_and_logs_only_the_canonical_payload(imported_building_world):
    from flask import current_app, request
    from extensions import db
    from models import WorldCommandLog
    from routes import commands
    from tests.test_command_transport import _json_wire
    fixture = imported_building_world
    app = current_app._get_current_object()
    app.register_blueprint(commands.commands_bp)
    @app.before_request
    def authorize_test_request():
        # As in the production application, auth runs before the route decodes
        # any bytes. Even malformed encodings cannot bypass an access denial.
        if request.headers.get("X-Test-Command-Access") != "granted":
            return {"ok": False, "error": {"code": "command_forbidden"}}, 403
    payload = fixture.batch("http")
    payload["commandId"] = f"cmd_encoded_http_{uuid4().hex}"
    world_db_id = fixture.world.id
    path = f"/projects/{fixture.project.project_id}/worlds/{fixture.world.world_id}/commands?includeCommandLog=false"
    wrapped = _json_wire(payload)
    rejected = {**wrapped, "transport": {**wrapped["transport"], "payload": "malformed"}}
    client = app.test_client()
    assert client.post(path, json=rejected).status_code == 403
    assert db.session.query(WorldCommandLog.id).filter_by(command_id=payload["commandId"]).count() == 0
    response = client.post(path, json=wrapped, headers={"X-Test-Command-Access": "granted"})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["ok"] and result["changed"] and result["commandType"] == "ObjectBatch"
    assert result["lod2BuildingEdit"]["filteredCellCount"] == 5
    fixture.project, fixture.universe, fixture.world = fixture.refresh()
    log = commands._query_without_relationships(WorldCommandLog.query.filter_by(
        world_db_id=world_db_id, command_id=payload["commandId"])).one()
    assert log.request_payload_json == payload
    assert "transport" not in log.request_payload_json
    assert log.command_type == "ObjectBatch" and log.changed
    assert fixture.state(fixture.positions[1])["blockTypeId"] is None
