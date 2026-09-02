import os
from uuid import uuid4

import pytest

from extensions import db
from models import BlockType, WorldObjectChunkRef, WorldObjectInstance
from routes.commands import (
    _block_type_id_from_cell_value,
    _create_command_log,
    _execute_place_object,
    _execute_set_or_remove_block,
    _get_cell_value,
    _get_registry_for_world,
    _load_chunk_for_mutation,
    _query_without_relationships,
    _resolve_project_world_context,
    _world_position_to_chunk_cell,
)
from wsgi import app


pytestmark = pytest.mark.skipif(
    os.getenv("VECTOPLAN_RUN_DB_INTEGRATION_TESTS") != "1",
    reason="requires the disposable local Chunk integration database",
)


def test_place_object_remesh_updates_one_persisted_object_and_chunk_ref():
    object_instance_id = f"obj_grid_test_{uuid4().hex}"
    # Reuse the already materialized spawn chunk. A remote/earth provider may
    # legitimately make a brand-new far-away chunk slow, which is unrelated to
    # the object-ref persistence contract under test.
    position = {"x": 0, "y": 15, "z": 0}

    with app.app_context(), app.test_request_context("/"):
        try:
            project, universe, world = _resolve_project_world_context("dev-project", "world_spawn")
            registry = _get_registry_for_world(world)
            block = BlockType.query.filter(
                BlockType.registry_db_id == registry.id,
                BlockType.placeable.is_(True),
                BlockType.breakable.is_(True),
                BlockType.deleted_at.is_(None),
            ).first()
            if block is None:
                pytest.skip("dev registry has no placeable and breakable block")

            first_footprint = {
                "type": "Polygon",
                "coordinateSpace": "world-cell-xz",
                "coordinates": [[
                    [position["x"], position["z"]],
                    [position["x"] + 1, position["z"]],
                    [position["x"] + 1, position["z"] + 1],
                    [position["x"], position["z"] + 1],
                    [position["x"], position["z"]],
                ]],
                "baseY": position["y"],
                "height": 1,
            }
            second_footprint = {
                **first_footprint,
                "coordinates": [[
                    [position["x"], position["z"]],
                    [position["x"] + 0.8, position["z"] + 0.1],
                    [position["x"] + 0.7, position["z"] + 1],
                    [position["x"], position["z"] + 0.9],
                    [position["x"], position["z"]],
                ]],
            }

            def place_payload(footprint):
                return {
                    "type": "PlaceObject",
                    "objectInstanceId": object_instance_id,
                    "position": position,
                    "blockTypeId": block.block_type_id,
                    "objectTypeId": "parcel_grid_body",
                    "objectKind": "semantic_footprint",
                    "dimensions": {"x": 1, "y": 1, "z": 1},
                    "occupiedCells": [position],
                    "footprint": footprint,
                    "metadata": {"mergeKey": "integration-grid-row"},
                }

            first_payload = place_payload(first_footprint)
            first_log = _create_command_log(
                project=project,
                universe=universe,
                world=world,
                payload=first_payload,
                command_type="PlaceObject",
                user_id="editor_user",
                session_id="parcel_grid_integration_test",
            )
            first_result = _execute_place_object(
                project=project,
                universe=universe,
                world=world,
                payload=first_payload,
                command_log=first_log,
                user_id="editor_user",
                session_id="parcel_grid_integration_test",
            )
            assert first_result["updatedExistingObject"] is False

            second_payload = place_payload(second_footprint)
            second_log = _create_command_log(
                project=project,
                universe=universe,
                world=world,
                payload=second_payload,
                command_type="PlaceObject",
                user_id="editor_user",
                session_id="parcel_grid_integration_test",
            )
            second_result = _execute_place_object(
                project=project,
                universe=universe,
                world=world,
                payload=second_payload,
                command_log=second_log,
                user_id="editor_user",
                session_id="parcel_grid_integration_test",
            )
            assert second_result["updatedExistingObject"] is True

            stored_objects = _query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id,
                WorldObjectInstance.object_instance_id == object_instance_id,
                WorldObjectInstance.deleted_at.is_(None),
            )).all()
            assert len(stored_objects) == 1
            assert stored_objects[0].footprint_json == second_footprint

            stored_refs = _query_without_relationships(WorldObjectChunkRef.query.filter(
                WorldObjectChunkRef.object_instance_db_id == stored_objects[0].id,
                WorldObjectChunkRef.deleted_at.is_(None),
            )).all()
            assert len(stored_refs) == 1

            _snapshot, content = _load_chunk_for_mutation(
                project=project,
                universe=universe,
                world=world,
                chunk_x=stored_refs[0].chunk_x,
                chunk_y=stored_refs[0].chunk_y,
                chunk_z=stored_refs[0].chunk_z,
            )
            matching_refs = [
                ref for ref in content.get("objectRefs", [])
                if ref.get("objectInstanceId") == object_instance_id
            ]
            assert len(matching_refs) == 1
            assert matching_refs[0]["footprint"] == second_footprint
        finally:
            # The test exercises real SQLAlchemy persistence and snapshot/ref
            # replacement, but must leave the shared local dev database clean.
            db.session.rollback()


def test_planning_build_area_with_no_voxel_occupancy_preserves_existing_cell():
    object_instance_id = f"obj_planning_area_test_{uuid4().hex}"
    position = {"x": 0, "y": 15, "z": 0}

    with app.app_context(), app.test_request_context("/"):
        try:
            project, universe, world = _resolve_project_world_context("dev-project", "world_spawn")
            registry = _get_registry_for_world(world)
            blocks = (
                BlockType.query.filter(
                    BlockType.registry_db_id == registry.id,
                    BlockType.placeable.is_(True),
                    BlockType.breakable.is_(True),
                    BlockType.deleted_at.is_(None),
                )
                .order_by(BlockType.block_type_id.asc())
                .limit(2)
                .all()
            )
            if len(blocks) < 2:
                pytest.skip("dev registry needs two distinct placeable and breakable blocks")
            seed_block, planning_fill_block = blocks
            assert seed_block.block_type_id != planning_fill_block.block_type_id

            seed_payload = {
                "type": "SetBlock",
                "position": position,
                "blockTypeId": seed_block.block_type_id,
            }
            seed_log = _create_command_log(
                project=project,
                universe=universe,
                world=world,
                payload=seed_payload,
                command_type="SetBlock",
                user_id="editor_user",
                session_id="planning_area_integration_test",
            )
            _execute_set_or_remove_block(
                project=project,
                universe=universe,
                world=world,
                payload=seed_payload,
                command_log=seed_log,
                command_type="SetBlock",
                user_id="editor_user",
                session_id="planning_area_integration_test",
            )

            chunk_size = int(world.chunk_size or 16)
            cell = _world_position_to_chunk_cell(position, chunk_size)
            _seed_snapshot, seeded_content = _load_chunk_for_mutation(
                project=project,
                universe=universe,
                world=world,
                chunk_x=cell["chunkX"],
                chunk_y=cell["chunkY"],
                chunk_z=cell["chunkZ"],
            )
            seeded_cell_value = _get_cell_value(
                seeded_content,
                local_x=cell["localX"],
                local_y=cell["localY"],
                local_z=cell["localZ"],
                chunk_size=chunk_size,
            )
            assert _block_type_id_from_cell_value(seeded_content, seeded_cell_value) == seed_block.block_type_id

            footprint = {
                "type": "Polygon",
                "coordinateSpace": "world-cell-xz",
                "coordinates": [[
                    [position["x"], position["z"]],
                    [position["x"] + 4, position["z"]],
                    [position["x"] + 4, position["z"] + 3],
                    [position["x"], position["z"] + 3],
                    [position["x"], position["z"]],
                ]],
                "baseY": position["y"],
                "height": 1,
            }
            planning_payload = {
                "type": "PlaceObject",
                "objectInstanceId": object_instance_id,
                "position": position,
                "blockTypeId": planning_fill_block.block_type_id,
                "objectTypeId": "planning_build_area",
                "objectKind": "semantic_footprint",
                "dimensions": {"x": 1, "y": 1, "z": 1},
                "occupiedCells": [position],
                "footprint": footprint,
                "metadata": {
                    "mergeKey": "integration-planning-build-area",
                    "voxelOccupancy": "none",
                },
            }
            planning_log = _create_command_log(
                project=project,
                universe=universe,
                world=world,
                payload=planning_payload,
                command_type="PlaceObject",
                user_id="editor_user",
                session_id="planning_area_integration_test",
            )
            result = _execute_place_object(
                project=project,
                universe=universe,
                world=world,
                payload=planning_payload,
                command_log=planning_log,
                user_id="editor_user",
                session_id="planning_area_integration_test",
            )

            assert result["affectedCells"] == []

            stored_object = _query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id,
                WorldObjectInstance.object_instance_id == object_instance_id,
                WorldObjectInstance.deleted_at.is_(None),
            )).one()
            assert stored_object.object_type_id == "planning_build_area"
            assert stored_object.metadata_json["voxelOccupancy"] == "none"
            assert stored_object.occupied_cells_json == [position]

            stored_refs = _query_without_relationships(WorldObjectChunkRef.query.filter(
                WorldObjectChunkRef.object_instance_db_id == stored_object.id,
                WorldObjectChunkRef.deleted_at.is_(None),
            )).all()
            assert len(stored_refs) == 1
            assert stored_refs[0].occupied_cells_json == []

            _planning_snapshot, planning_content = _load_chunk_for_mutation(
                project=project,
                universe=universe,
                world=world,
                chunk_x=cell["chunkX"],
                chunk_y=cell["chunkY"],
                chunk_z=cell["chunkZ"],
            )
            persisted_cell_value = _get_cell_value(
                planning_content,
                local_x=cell["localX"],
                local_y=cell["localY"],
                local_z=cell["localZ"],
                chunk_size=chunk_size,
            )
            assert persisted_cell_value == seeded_cell_value
            assert (
                _block_type_id_from_cell_value(planning_content, persisted_cell_value)
                == seed_block.block_type_id
            )

            matching_refs = [
                ref for ref in planning_content.get("objectRefs", [])
                if ref.get("objectInstanceId") == object_instance_id
            ]
            assert len(matching_refs) == 1
            assert matching_refs[0]["metadata"]["voxelOccupancy"] == "none"
            assert matching_refs[0]["footprint"] == footprint
        finally:
            db.session.rollback()
