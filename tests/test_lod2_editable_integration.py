"""Real PostgreSQL read-back, exclusively inside a rolled-back test transaction."""
import copy
import os
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.getenv("VECTOPLAN_RUN_LOD2_DB_TESTS") != "1", reason="explicit local DB test opt-in")


def test_import_preserves_edits_and_supports_break_roof_edit_delete_and_idempotency():
    from wsgi import app
    from extensions import db
    from models.project import Project
    from models.world import WorldInstance
    from models.object import WorldObjectInstance
    from models.universe import Universe
    from sqlalchemy.orm import noload
    from routes import commands
    from src.geodata.lod2_conversion import roof_objects, WALL_BLOCK_ID
    from src.geodata.lod2_import import apply_import

    with app.app_context(), app.test_request_context("/"):
        try:
            world = (WorldInstance.query.options(noload("*")).join(Project, WorldInstance.project_db_id == Project.id)
                     .filter(Project.external_app_project_id == "prj_692f7d6144354e2eac0e92c9", WorldInstance.world_id == "world_spawn").one())
            project = db.session.get(Project, world.project_db_id, options=[noload("*")])
            universe = db.session.get(Universe, world.universe_db_id, options=[noload("*")])
            world_db_id, project_db_id, universe_db_id = world.id, project.id, universe.id
            def reload_context():
                nonlocal world, project, universe
                db.session.flush()
                db.session.expunge_all()
                world = db.session.get(WorldInstance, world_db_id, options=[noload("*")])
                project = db.session.get(Project, project_db_id, options=[noload("*")])
                universe = db.session.get(Universe, universe_db_id, options=[noload("*")])
            def execute(payload):
                return commands._execute_command(project=project, universe=universe, world=world,
                                                  payload={"userId": "lod2_integration_test", **payload})[1]
            def block(x, y, value="system_terrain"):
                return {"type": "SetBlock", "position": {"x": x, "y": y, "z": 0}, "blockTypeId": value}
            def load(x, y):
                return commands._load_chunk_for_mutation(project=project, universe=universe, world=world,
                                                         chunk_x=x//16, chunk_y=y//16, chunk_z=0)[1]
            def cell(x, y):
                content = load(x, y)
                value = commands._get_cell_value(content, local_x=x%16, local_y=y%16, local_z=0, chunk_size=16)
                return commands._block_type_id_from_cell_value(content, value)
            execute(block(0, 60))
            execute({"type": "RemoveBlock", "position": {"x": 0, "y": 60, "z": 0}})
            execute(block(1, 60))
            execute(block(0, 62))  # Roof anchor overlaps a deliberately existing user cell.
            feature = {"id": "qa-"+uuid4().hex, "sourceSha256": "a"*64, "sourceTile": "fixture.zip",
                       "polygons": [{"surface": "RoofSurface", "rings": [
                           [[0, 62, 0], [2, 62, 0], [2, 63, 2], [0, 63, 2], [0, 62, 0]]]}]}
            roofs = roof_objects(feature)
            building = {"buildingId": feature["id"], "sourceSha256": feature["sourceSha256"],
                        "sourceTile": feature["sourceTile"], "wallCells": [[0, 60, 0], [1, 60, 0], [2, 60, 0]], "roofs": roofs}
            plan = {"referenceFingerprint": world.build_earth_provider().reference_fingerprint,
                    "buildings": [building], "heightReference": {"kind": "test"}}
            result = apply_import(world, plan)
            assert result["writtenWallCells"] == 1
            assert result["protectedEditedCells"] == 2
            wall_type = commands._get_block_type(
                world=world, block_type_id=WALL_BLOCK_ID, require_breakable=True,
            )
            assert wall_type.metadata_json["color"] == "#f1f3f5"
            reload_context()
            assert cell(0, 60) is None and cell(1, 60) == "system_terrain"
            assert cell(2, 60) == WALL_BLOCK_ID and cell(0, 62) == "system_terrain"
            from src.geodata.structure_streaming import structure_streaming_hints
            hints = structure_streaming_hints(world, [{"chunkX": 0, "chunkZ": 0}])
            assert {"chunkX": 0, "chunkY": 3, "chunkZ": 0} in hints[(0, 0)]["chunkCoordinates"]
            roof = roofs[0]
            roof_id = roof["objectInstanceId"]
            existing = WorldObjectInstance.query.options(noload("*")).filter_by(world_db_id=world.id, object_instance_id=roof_id).one()
            from types import SimpleNamespace
            with pytest.raises(ValueError, match="roof cannot be converted"):
                commands._apply_existing_object_geometry_update(existing, SimpleNamespace(object_type_id="parcel_grid_body"),
                    command_id="test", user_id="test", session_id="test")
            edited = copy.deepcopy(roof)
            edited["metadata"]["roofCalculation"]["input_fingerprint"] = "changed-roof"
            edited["metadata"]["solar"] = {"schemaVersion":"vplib-roof-solar.v1", "selectedFaces":["facet-1"],
                "module":{"packageId":"test-module","powerWp":450,"widthM":1.134,"lengthM":1.722,"thicknessM":.035},
                "metricScale":{"x":.61,"y":1,"z":1},"flatAzimuthDeg":180,"flatTiltDeg":10}
            edited["metadata"]["roofCalculation"]["geometry"]["faces"][0]["polygon_3d_mm"][0][2] += 100
            assert execute(edited)["updatedExistingObject"] is True
            reload_context()
            refs = [r for r in load(0, 62)["objectRefs"] if r["objectInstanceId"] == roof_id]
            assert len(refs) == 1 and refs[0]["metadata"]["roofCalculation"]["input_fingerprint"] == "changed-roof"
            assert refs[0]["metadata"]["solar"] == edited["metadata"]["solar"]
            execute({"type": "RemoveBlock", "position": {"x": 2, "y": 60, "z": 0}})
            execute({"type": "RemoveObject", "objectInstanceId": roof_id})
            reload_context()
            assert cell(0, 62) == "system_terrain" and cell(2, 60) is None
            assert not any(r["objectInstanceId"] == roof_id for r in load(0, 62)["objectRefs"])
            assert apply_import(world, plan)["importedBuildings"] == 0
            assert cell(2, 60) is None
            obj = WorldObjectInstance.query.options(noload("*")).filter_by(world_db_id=world.id, object_instance_id=roof_id).one()
            assert obj.deleted_at is not None
        finally:
            db.session.rollback()


def test_roof_only_anchor_discovered_outside_visible_circle_and_serialized_without_world_mutation(monkeypatch):
    from wsgi import app
    from extensions import db
    from models.project import Project
    from models.world import WorldInstance
    from models.universe import Universe
    from sqlalchemy.orm import noload
    from routes import commands, chunks
    from src.geodata.lod2_conversion import roof_objects
    from src.geodata.structure_streaming import structure_streaming_hints
    from src.geodata import visual_overlays
    monkeypatch.setattr(visual_overlays, "attach_geodata_overlays", lambda *_: False)
    with app.app_context(), app.test_request_context("/"):
        try:
            world = (WorldInstance.query.options(noload("*")).join(Project, WorldInstance.project_db_id == Project.id)
                     .filter(Project.external_app_project_id == "prj_692f7d6144354e2eac0e92c9", WorldInstance.world_id == "world_spawn").one())
            project = db.session.get(Project, world.project_db_id, options=[noload("*")])
            universe = db.session.get(Universe, world.universe_db_id, options=[noload("*")])
            roof = roof_objects({"id":"qa-streaming-"+uuid4().hex,"sourceTile":"fixture.zip","sourceSha256":"a"*64,
                "polygons":[{"surface":"RoofSurface","rings":[
                [[-180,200,0],[10,200,0],[10,205,8],[-180,200,0]]]}]})[0]
            execute = lambda payload: commands._execute_command(project=project,universe=universe,world=world,payload=payload)
            execute(roof)
            snapshot, runtime = commands._load_chunk_for_mutation(project=project,universe=universe,world=world,
                chunk_x=-12,chunk_y=12,chunk_z=0)
            assert snapshot.non_air_cell_count == 0
            before = copy.deepcopy(runtime)
            hints = structure_streaming_hints(world,[{"chunkX":0,"chunkZ":0},{"chunkX":100,"chunkZ":100}])
            assert {"chunkX":-12,"chunkY":12,"chunkZ":0} in hints[(0,0)]["chunkCoordinates"]
            assert not hints.get((100,100))
            payload = chunks._serialize_chunk_load_result(project=project,universe=universe,world=world,
                result={"chunkKey":"0:0:0","chunk":{"chunkX":0,"chunkY":0,"chunkZ":0,"cells":[0]}},
                structure_hints=hints)
            assert payload["chunk"]["metadata"]["structureStreaming"] == hints[(0,0)]
            assert payload["chunk"]["cells"] == [0] and runtime == before
            execute({"type":"RemoveObject","objectInstanceId":roof["objectInstanceId"]})
            after = structure_streaming_hints(world,[{"chunkX":0,"chunkZ":0}])
            assert {"chunkX":-12,"chunkY":12,"chunkZ":0} not in after[(0,0)]["chunkCoordinates"]
        finally:
            db.session.rollback()


def test_roof_refinement_preserves_facets_and_independent_objects_without_touching_walls():
    import runpy
    from pathlib import Path
    from types import SimpleNamespace
    from wsgi import app
    from extensions import db
    from sqlalchemy.orm import noload
    from models.project import Project
    from models.world import WorldInstance
    from models.universe import Universe
    from models.object import WorldObjectInstance
    from routes import commands
    from src.geodata.lod2_conversion import roof_objects, digest
    script = Path('/qa-scripts/refine_lod2_roofs.py')
    if not script.exists():
        script = Path(__file__).parents[1]/'scripts/refine_lod2_roofs.py'
    refine = runpy.run_path(str(script))['replacement_roofs']
    parts = roof_objects({'id':'qa-levels-'+uuid4().hex,'sourceTile':'test.zip','sourceSha256':'a'*64,'polygons':[
        {'surface':'RoofSurface','rings':[[[0,80,0],[4,80,0],[4,80,4],[0,80,4],[0,80,0]]]},
        {'surface':'RoofSurface','rings':[[[4,90,0],[8,90,0],[8,90,4],[4,90,4],[4,90,0]]]},
    ]})
    parent = copy.deepcopy(parts[0])
    metadata = parent['metadata']
    source = metadata['roofParameters']['importedSource']
    source['faces'] += parts[1]['metadata']['roofParameters']['importedSource']['faces']
    metadata['roofCalculation']['geometry']['faces'] = copy.deepcopy(source['faces'])
    metadata['roofCalculation']['input_fingerprint'] = digest(source)
    proxy = SimpleNamespace(metadata_json=metadata,object_instance_id=parent['objectInstanceId'])
    replacements = refine(proxy)
    assert len(replacements) == 2
    assert sorted(r['footprint']['baseY'] for r in replacements) == [80,90]
    edited = copy.deepcopy(proxy)
    edited.metadata_json['source'] = 'vectoplan-editor.world-edit.roof'
    assert not refine(edited)
    assert not refine(SimpleNamespace(metadata_json=replacements[0]['metadata'],object_instance_id=replacements[0]['objectInstanceId']))
    with app.app_context(), app.test_request_context('/'):
        try:
            world = (WorldInstance.query.options(noload('*')).join(Project,WorldInstance.project_db_id == Project.id)
                .filter(Project.external_app_project_id == 'prj_692f7d6144354e2eac0e92c9',WorldInstance.world_id == 'world_spawn').one())
            project = db.session.get(Project,world.project_db_id,options=[noload('*')])
            universe = db.session.get(Universe,world.universe_db_id,options=[noload('*')])
            execute = lambda payload: commands._execute_command(project=project,universe=universe,world=world,payload=payload)
            execute({'type':'SetBlock','position':parent['position'],'blockTypeId':'lod2_exterior_wall'})
            execute(parent)
            execute({'type':'RemoveObject','objectInstanceId':parent['objectInstanceId']})
            for roof in replacements:
                execute(roof)
            execute({'type':'RemoveObject','objectInstanceId':replacements[0]['objectInstanceId']})
            second = WorldObjectInstance.query.options(noload('*')).filter_by(world_db_id=world.id,object_instance_id=replacements[1]['objectInstanceId']).one()
            assert second.deleted_at is None and second.footprint_json['baseY'] == 90
            content = commands._load_chunk_for_mutation(project=project,universe=universe,world=world,chunk_x=0,chunk_y=5,chunk_z=0)[1]
            value = commands._get_cell_value(content,local_x=0,local_y=0,local_z=0,chunk_size=16)
            assert commands._block_type_id_from_cell_value(content,value) == 'lod2_exterior_wall'
        finally:
            db.session.rollback()
