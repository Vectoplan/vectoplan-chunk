"""Ownership regression tests for PlaceObject/RemoveObject voxel composites."""

import os
from uuid import uuid4

import pytest

from routes.commands import (
    _detach_cells_from_runtime_object_refs,
    _runtime_object_cell_owner,
)


CELL = {
    "worldX": 3,
    "worldY": 7,
    "worldZ": 5,
    "chunkX": 0,
    "chunkY": 0,
    "chunkZ": 0,
    "chunkKey": "0:0:0",
    "localX": 3,
    "localY": 7,
    "localZ": 5,
}


def _legacy_ref(object_instance_id: str, block_type_id: str):
    return {
        "objectInstanceId": object_instance_id,
        "objectKind": "block_composite",
        "fillBlockTypeId": block_type_id,
        "occupiedCells": [{"x": 3, "y": 7, "z": 5}],
        "metadata": {},
    }


def test_runtime_owner_uses_last_writer_for_legacy_overlapping_refs():
    content = {
        "objectRefs": [
            _legacy_ref("older-object", "stone"),
            _legacy_ref("newer-object", "stone"),
        ]
    }

    owner = _runtime_object_cell_owner(
        content,
        world_x=3,
        world_y=7,
        world_z=5,
        chunk_x=0,
        chunk_y=0,
        chunk_z=0,
        local_x=3,
        local_y=7,
        local_z=5,
    )

    assert owner == {
        "objectInstanceId": "newer-object",
        "expectedBlockTypeId": "stone",
        "ownershipPolicy": "last-writer-wins-v1",
    }


def test_detach_removes_overwritten_cell_but_preserves_metadata_only_ref():
    metadata_only = {
        **_legacy_ref("roof", "roof-anchor"),
        "metadata": {"voxelOccupancy": "none"},
    }
    content = {
        "objectRefs": [
            _legacy_ref("wall", "wall-block"),
            metadata_only,
        ]
    }

    detached = _detach_cells_from_runtime_object_refs(
        content,
        target_cells=[CELL],
        chunk_x=0,
        chunk_y=0,
        chunk_z=0,
    )

    assert detached == {"wall"}
    refs = {ref["objectInstanceId"]: ref for ref in content["objectRefs"]}
    assert refs["wall"]["occupiedCells"] == []
    assert refs["wall"]["metadata"]["cellOwnershipPolicy"] == "last-writer-wins-v1"
    assert refs["roof"]["occupiedCells"] == [{"x": 3, "y": 7, "z": 5}]


@pytest.mark.skipif(
    os.getenv("VECTOPLAN_RUN_DB_INTEGRATION_TESTS") != "1",
    reason="requires the disposable local Chunk integration database",
)
def test_remove_object_preserves_manual_replacement_and_overlapping_new_owner():
    from extensions import db
    from models import BlockType, WorldObjectChunkRef, WorldObjectInstance
    from routes import commands
    from wsgi import app

    with app.app_context(), app.test_request_context("/"):
        try:
            project, universe, world = commands._resolve_project_world_context(
                "dev-project", "world_spawn"
            )
            registry = commands._get_registry_for_world(world)
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
                pytest.skip("dev registry needs two distinct editable blocks")
            first_block, replacement_block = blocks
            assert first_block.block_type_id != replacement_block.block_type_id

            nonce = uuid4().hex
            manual_position = {"x": 8, "y": 15, "z": 8}
            overlap_position = {"x": 9, "y": 15, "z": 8}

            def execute(payload):
                return commands._execute_command(
                    project=project,
                    universe=universe,
                    world=world,
                    payload={
                        "userId": "object_ownership_test",
                        "sessionId": nonce,
                        **payload,
                    },
                )[1]

            def place(object_id, position, block_type_id):
                return execute(
                    {
                        "type": "PlaceObject",
                        "objectInstanceId": object_id,
                        "objectTypeId": "ownership_test_composite",
                        "objectKind": "block_composite",
                        "position": position,
                        "dimensions": {"x": 1, "y": 1, "z": 1},
                        "occupiedCells": [position],
                        "blockTypeId": block_type_id,
                    }
                )

            def cell_block_type(position):
                cell = commands._world_position_to_chunk_cell(
                    position, int(world.chunk_size or 16)
                )
                _snapshot, content = commands._load_chunk_for_mutation(
                    project=project,
                    universe=universe,
                    world=world,
                    chunk_x=cell["chunkX"],
                    chunk_y=cell["chunkY"],
                    chunk_z=cell["chunkZ"],
                )
                value = commands._get_cell_value(
                    content,
                    local_x=cell["localX"],
                    local_y=cell["localY"],
                    local_z=cell["localZ"],
                    chunk_size=int(world.chunk_size or 16),
                )
                return commands._block_type_id_from_cell_value(content, value)

            manual_object_id = f"obj_manual_owner_{nonce}"
            place(manual_object_id, manual_position, first_block.block_type_id)
            replacement = execute(
                {
                    "type": "SetBlock",
                    "position": manual_position,
                    "blockTypeId": replacement_block.block_type_id,
                }
            )
            assert manual_object_id in replacement["detachedObjectInstanceIds"]

            stored_manual_object = commands._query_without_relationships(
                WorldObjectInstance.query.filter_by(
                    world_db_id=world.id,
                    object_instance_id=manual_object_id,
                )
            ).one()
            stored_manual_ref = commands._query_without_relationships(
                WorldObjectChunkRef.query.filter_by(
                    object_instance_db_id=stored_manual_object.id
                )
            ).one()
            assert stored_manual_ref.occupied_cells_json == []

            manual_removal = execute(
                {"type": "RemoveObject", "objectInstanceId": manual_object_id}
            )
            assert manual_removal["clearedCellCount"] == 0
            assert cell_block_type(manual_position) == replacement_block.block_type_id

            same_type_position = {"x": 10, "y": 15, "z": 8}
            same_type_object_id = f"obj_same_type_owner_{nonce}"
            place(
                same_type_object_id,
                same_type_position,
                first_block.block_type_id,
            )
            same_type_write = execute(
                {
                    "type": "SetBlock",
                    "position": same_type_position,
                    "blockTypeId": first_block.block_type_id,
                }
            )
            assert same_type_write["changed"] is True
            assert same_type_object_id in same_type_write["detachedObjectInstanceIds"]
            same_type_removal = execute(
                {"type": "RemoveObject", "objectInstanceId": same_type_object_id}
            )
            assert same_type_removal["clearedCellCount"] == 0
            assert cell_block_type(same_type_position) == first_block.block_type_id

            older_id = f"obj_overlap_older_{nonce}"
            newer_id = f"obj_overlap_newer_{nonce}"
            place(older_id, overlap_position, first_block.block_type_id)
            newer_placement = place(
                newer_id, overlap_position, first_block.block_type_id
            )
            assert older_id in newer_placement["displacedObjectInstanceIds"]

            older_removal = execute(
                {"type": "RemoveObject", "objectInstanceId": older_id}
            )
            assert older_removal["clearedCellCount"] == 0
            assert cell_block_type(overlap_position) == first_block.block_type_id

            newer_removal = execute(
                {"type": "RemoveObject", "objectInstanceId": newer_id}
            )
            assert newer_removal["clearedCellCount"] == 1
            assert cell_block_type(overlap_position) is None
        finally:
            db.session.rollback()


@pytest.mark.skipif(
    os.getenv("VECTOPLAN_RUN_DB_INTEGRATION_TESTS") != "1",
    reason="requires the disposable local Chunk integration database",
)
def test_metadata_only_object_can_reroute_across_chunks_and_restore_old_ref():
    from extensions import db
    from models import BlockType, WorldObjectChunkRef, WorldObjectInstance
    from routes import commands
    from wsgi import app

    with app.app_context(), app.test_request_context("/"):
        try:
            project, universe, world = commands._resolve_project_world_context(
                "dev-project", "world_spawn"
            )
            registry = commands._get_registry_for_world(world)
            block = BlockType.query.filter(
                BlockType.registry_db_id == registry.id,
                BlockType.placeable.is_(True),
                BlockType.breakable.is_(True),
                BlockType.deleted_at.is_(None),
            ).first()
            if block is None:
                pytest.skip("dev registry has no placeable and breakable block")

            nonce = uuid4().hex
            object_id = f"obj_metadata_reroute_{nonce}"
            first_position = {"x": 15, "y": 15, "z": 0}
            second_position = {"x": 16, "y": 15, "z": 0}

            def execute(position, *, voxel_occupancy="none", include_log=False):
                payload = {
                    "type": "PlaceObject",
                    "userId": "object_ownership_test",
                    "sessionId": nonce,
                    "objectInstanceId": object_id,
                    "objectTypeId": "planning_build_area",
                    "objectKind": "semantic_footprint",
                    "position": position,
                    "dimensions": {"x": 1, "y": 1, "z": 1},
                    "occupiedCells": [position],
                    "blockTypeId": block.block_type_id,
                    "metadata": {"voxelOccupancy": voxel_occupancy},
                }
                command_log, result = commands._execute_command(
                    project=project,
                    universe=universe,
                    world=world,
                    payload=payload,
                )
                return (command_log, result) if include_log else result

            def runtime_refs(position):
                cell = commands._world_position_to_chunk_cell(
                    position, int(world.chunk_size or 16)
                )
                _snapshot, content = commands._load_chunk_for_mutation(
                    project=project,
                    universe=universe,
                    world=world,
                    chunk_x=cell["chunkX"],
                    chunk_y=cell["chunkY"],
                    chunk_z=cell["chunkZ"],
                )
                return [
                    ref
                    for ref in content.get("objectRefs", [])
                    if ref.get("objectInstanceId") == object_id
                ]

            # Simulate a pre-fix planning-area row: it was physically written,
            # but its persisted/runtime metadata already declared no occupancy.
            assert execute(first_position, voxel_occupancy="legacy")["updatedExistingObject"] is False
            assert len(runtime_refs(first_position)) == 1

            stored_object = commands._query_without_relationships(
                WorldObjectInstance.query.filter_by(
                    world_db_id=world.id,
                    object_instance_id=object_id,
                )
            ).one()
            stored_object.metadata_json = {
                **dict(stored_object.metadata_json or {}),
                "voxelOccupancy": "none",
            }
            first_cell = commands._world_position_to_chunk_cell(
                first_position, int(world.chunk_size or 16)
            )
            legacy_snapshot, legacy_content = commands._load_chunk_for_mutation(
                project=project,
                universe=universe,
                world=world,
                chunk_x=first_cell["chunkX"],
                chunk_y=first_cell["chunkY"],
                chunk_z=first_cell["chunkZ"],
            )
            for ref in legacy_content.get("objectRefs", []):
                if ref.get("objectInstanceId") == object_id:
                    ref["metadata"] = {
                        **dict(ref.get("metadata") or {}),
                        "voxelOccupancy": "none",
                    }
            legacy_snapshot.replace_content(
                content_json=legacy_content,
                materialized_reason="migration",
                updated_by_user_id="object_ownership_test",
                last_session_id=nonce,
            )
            db.session.add(legacy_snapshot)

            moved_log, moved = execute(second_position, include_log=True)
            assert moved["updatedExistingObject"] is True
            assert len(runtime_refs(first_position)) == 0
            assert len(runtime_refs(second_position)) == 1
            old_chunk_key = commands._build_chunk_key(0, 0, 0)
            new_chunk_key = commands._build_chunk_key(1, 0, 0)
            assert set(moved_log.affected_chunks_json) == {old_chunk_key, new_chunk_key}
            _old_snapshot, old_content = commands._load_chunk_for_mutation(
                project=project,
                universe=universe,
                world=world,
                chunk_x=0,
                chunk_y=0,
                chunk_z=0,
            )
            old_value = commands._get_cell_value(
                old_content,
                local_x=first_cell["localX"],
                local_y=first_cell["localY"],
                local_z=first_cell["localZ"],
                chunk_size=int(world.chunk_size or 16),
            )
            assert old_value == commands.AIR_CELL_VALUE
            assert any(
                cell.get("legacyMetadataOnlyCleanup") is True
                for cell in moved["affectedCells"]
            )

            assert stored_object.occupied_cells_json == [second_position]
            refs_after_move = commands._query_without_relationships(
                WorldObjectChunkRef.query.filter_by(
                    object_instance_db_id=stored_object.id
                )
            ).all()
            assert len(refs_after_move) == 2
            assert sum(ref.is_active for ref in refs_after_move) == 1
            assert next(ref for ref in refs_after_move if ref.is_active).chunk_x == 1

            moved_back = execute(first_position)
            assert moved_back["updatedExistingObject"] is True
            assert len(runtime_refs(first_position)) == 1
            assert len(runtime_refs(second_position)) == 0
            refs_after_return = commands._query_without_relationships(
                WorldObjectChunkRef.query.filter_by(
                    object_instance_db_id=stored_object.id
                )
            ).all()
            assert len(refs_after_return) == 2
            assert sum(ref.is_active for ref in refs_after_return) == 1
            assert next(ref for ref in refs_after_return if ref.is_active).chunk_x == 0
        finally:
            db.session.rollback()
