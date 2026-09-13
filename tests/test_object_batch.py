"""Atomic ObjectBatch command and rollback regression tests."""

from copy import deepcopy
import os
from uuid import uuid4

import pytest

from models.event import (
    EVENT_TYPE_OBJECT_CHANGE,
    VALID_COMMAND_TYPES,
    derive_event_type_from_command_type,
)
from routes import commands


def _place_payload(object_instance_id, position, block_type_id):
    return {
        "type": "PlaceObject",
        "objectInstanceId": object_instance_id,
        "objectTypeId": "object_batch_test_composite",
        "objectKind": "block_composite",
        "position": dict(position),
        "dimensions": {"x": 1, "y": 1, "z": 1},
        "occupiedCells": [dict(position)],
        "blockTypeId": block_type_id,
    }


def test_object_batch_is_a_persistable_object_command_type():
    assert "ObjectBatch" in VALID_COMMAND_TYPES
    assert derive_event_type_from_command_type("ObjectBatch") == EVENT_TYPE_OBJECT_CHANGE
    # The editor's supported 80-storey generation emits at least 162 object
    # commands (two per storey plus roof and parent).
    assert commands.DEFAULT_MAX_OBJECT_BATCH_COMMANDS >= 162


def test_object_batch_rejects_non_object_children_before_dispatch():
    payload = {
        "type": "ObjectBatch",
        "position": {"x": 0, "y": 0, "z": 0},
        "commands": [
            {
                "type": "SetBlock",
                "position": {"x": 0, "y": 0, "z": 0},
                "blockTypeId": "test",
            }
        ],
    }

    with pytest.raises(ValueError, match="Allowed child command types: PlaceObject, RemoveObject"):
        commands._normalize_object_batch_commands(payload)


def test_object_batch_enforces_configured_child_count(monkeypatch):
    monkeypatch.setattr(commands, "_get_max_object_batch_commands", lambda: 1)
    payload = {
        "type": "ObjectBatch",
        "position": {"x": 0, "y": 0, "z": 0},
        "commands": [
            {"type": "RemoveObject", "objectInstanceId": "first"},
            {"type": "RemoveObject", "objectInstanceId": "second"},
        ],
    }

    with pytest.raises(ValueError, match="contains 2 child commands, but maximum is 1"):
        commands._normalize_object_batch_commands(payload)


def test_object_batch_enforces_aggregate_placement_cell_count(monkeypatch):
    monkeypatch.setattr(commands, "_get_max_object_batch_affected_cells", lambda: 1)
    payload = {
        "type": "ObjectBatch",
        "position": {"x": 0, "y": 0, "z": 0},
        "commands": [
            {
                "type": "PlaceObject",
                "position": {"x": 0, "y": 0, "z": 0},
                "dimensions": {"x": 2, "y": 1, "z": 1},
            }
        ],
    }

    with pytest.raises(ValueError, match="affect 2 cells in total, but maximum is 1"):
        commands._normalize_object_batch_commands(payload)


def test_object_batch_rejects_place_after_remove_with_the_same_identity():
    payload = {
        "type": "ObjectBatch",
        "position": {"x": 0, "y": 0, "z": 0},
        "commands": [
            {"type": "RemoveObject", "objectInstanceId": "same-generation"},
            _place_payload(
                "same-generation",
                {"x": 0, "y": 0, "z": 0},
                "test",
            ),
        ],
    }

    with pytest.raises(ValueError, match="Use a new objectInstanceId"):
        commands._normalize_object_batch_commands(payload)


pytestmark_db = pytest.mark.skipif(
    os.getenv("VECTOPLAN_RUN_DB_INTEGRATION_TESTS") != "1",
    reason="requires the disposable local Chunk integration database",
)


def _editable_blocks(world, *, limit):
    from models import BlockType

    registry = commands._get_registry_for_world(world)
    return (
        BlockType.query.filter(
            BlockType.registry_db_id == registry.id,
            BlockType.placeable.is_(True),
            BlockType.breakable.is_(True),
            BlockType.deleted_at.is_(None),
        )
        .order_by(BlockType.block_type_id.asc())
        .limit(limit)
        .all()
    )


def _cell_state(project, universe, world, position):
    chunk_size = int(world.chunk_size or 16)
    cell = commands._world_position_to_chunk_cell(position, chunk_size)
    snapshot, content = commands._load_chunk_for_mutation(
        project=project,
        universe=universe,
        world=world,
        chunk_x=cell["chunkX"],
        chunk_y=cell["chunkY"],
        chunk_z=cell["chunkZ"],
    )
    cell_value = commands._get_cell_value(
        content,
        local_x=cell["localX"],
        local_y=cell["localY"],
        local_z=cell["localZ"],
        chunk_size=chunk_size,
    )
    return {
        "cell": cell,
        "snapshot": snapshot,
        "content": content,
        "cellValue": cell_value,
        "blockTypeId": commands._block_type_id_from_cell_value(content, cell_value),
    }


@pytestmark_db
def test_object_batch_is_allowed_by_installed_database_constraints():
    from sqlalchemy import inspect

    from extensions import db
    from wsgi import app

    with app.app_context():
        inspector = inspect(db.engine)
        expected_constraints = {
            "world_command_logs": "ck_world_command_logs_command_type_valid",
            "chunk_events": "ck_chunk_events_command_type_valid",
        }
        for table_name, constraint_name in expected_constraints.items():
            checks = {
                str(check.get("name")): str(check.get("sqltext") or "")
                for check in inspector.get_check_constraints(table_name)
            }
            assert constraint_name in checks
            assert "ObjectBatch" in checks[constraint_name]


@pytestmark_db
def test_object_batch_success_aggregates_children_under_one_command_log():
    from extensions import db
    from models import WorldObjectInstance, ChunkEvent
    from sqlalchemy import event
    from wsgi import app

    with app.app_context(), app.test_request_context("/"):
        try:
            project, universe, world = commands._resolve_project_world_context(
                "dev-project", "world_spawn"
            )
            blocks = _editable_blocks(world, limit=1)
            if not blocks:
                pytest.skip("dev registry has no placeable and breakable block")

            nonce = uuid4().hex
            first_id = f"obj_batch_success_first_{nonce}"
            second_id = f"obj_batch_success_second_{nonce}"
            parent_id = f"obj_batch_success_parent_{nonce}"
            first_position = {"x": 12, "y": 15, "z": 12}
            second_position = {"x": 13, "y": 15, "z": 12}
            payload = {
                "type": "ObjectBatch",
                "commandId": f"cmd_object_batch_success_{nonce}",
                "position": first_position,
                "userId": "object_batch_test",
                "sessionId": nonce,
                "commands": [
                    _place_payload(first_id, first_position, blocks[0].block_type_id),
                    _place_payload(second_id, second_position, blocks[0].block_type_id),
                    {
                        **_place_payload(
                            parent_id,
                            first_position,
                            blocks[0].block_type_id,
                        ),
                        "objectTypeId": "planning_build_area",
                        "objectKind": "semantic_footprint",
                        "metadata": {"voxelOccupancy": "none"},
                    },
                ],
            }

            snapshot_updates = []
            def observe_snapshot_updates(_connection, _cursor, statement, _parameters, _context, _many):
                if statement.lower().startswith('update chunk_snapshots '):
                    snapshot_updates.append(statement)
            event.listen(db.engine, 'before_cursor_execute', observe_snapshot_updates)
            try:
                command_log, result = commands._execute_command(
                    project=project, universe=universe, world=world, payload=payload)
            finally:
                event.remove(db.engine, 'before_cursor_execute', observe_snapshot_updates)
            # Intermediate revisions belong to their events; persist the large
            # final snapshot once rather than at every child query/autoflush.
            assert len(snapshot_updates) <= 1

            assert result["commandType"] == "ObjectBatch"
            assert result["changed"] is True
            assert result["objectBatch"]["commandCount"] == 3
            assert result["objectBatch"]["changedCommandCount"] == 3
            assert result["objectBatch"]["objectInstanceIds"] == [
                first_id,
                second_id,
                parent_id,
            ]
            assert [
                child["commandType"] for child in result["objectBatch"]["results"]
            ] == ["PlaceObject", "PlaceObject", "PlaceObject"]
            assert len(result["eventIds"]) == 3
            assert len(result["affectedCells"]) == 2
            assert len(result["changedChunks"]) == 1
            assert set(result["chunkVersions"]) == set(result["changedChunks"])
            # Both writes touch the same materialized snapshot. The response
            # reports that final resource once while retaining both events.
            assert len(result["snapshotIds"]) == 1
            events = commands._query_without_relationships(ChunkEvent.query.filter(
                ChunkEvent.command_id == command_log.command_id)).order_by(ChunkEvent.id).all()
            assert len(events) == 3
            for previous, following in zip(events, events[1:]):
                assert following.chunk_revision_before == previous.chunk_revision_after
                assert following.content_hash_before == previous.content_hash_after

            assert command_log.command_type == "ObjectBatch"
            assert command_log.command_status == "applied"
            assert command_log.event_count == 3
            assert command_log.affected_cell_count == 2
            assert command_log.affected_chunk_count == 1
            assert command_log.object_instance_id is None
            assert command_log.object_type_id is None
            assert command_log.object_size_x is None

            stored_ids = {
                row.object_instance_id
                for row in commands._query_without_relationships(
                    WorldObjectInstance.query.filter(
                        WorldObjectInstance.world_db_id == world.id,
                        WorldObjectInstance.object_instance_id.in_(
                            [first_id, second_id, parent_id]
                        ),
                        WorldObjectInstance.deleted_at.is_(None),
                    )
                ).all()
            }
            assert stored_ids == {first_id, second_id, parent_id}

            response = commands._serialize_command_result(
                project=project,
                universe=universe,
                world=world,
                command_log=command_log,
                result=result,
                include_command_log=False,
            )
            assert response["flags"]["objectCommand"] is True
            assert response["objectBatch"] == result["objectBatch"]
            assert response["chunkVersions"] == result["chunkVersions"]
        finally:
            db.session.rollback()


@pytestmark_db
def test_object_batch_failure_rolls_back_overwrite_and_object_refs(monkeypatch):
    from extensions import db
    from models import (
        ChunkEvent,
        WorldCommandLog,
        WorldObjectChunkRef,
        WorldObjectInstance,
    )
    from models.world import WorldInstance
    from wsgi import app

    with app.app_context(), app.test_request_context("/"):
        temporary_world_id = None
        try:
            project, universe, base_world = commands._resolve_project_world_context(
                "dev-project", "world_spawn"
            )
            project_public_id = project.project_id
            universe_public_id = universe.universe_id
            nonce = uuid4().hex
            temporary_world_id = f"world_object_batch_{nonce}"
            temporary_world = WorldInstance.create(
                project_db_id=project.id,
                universe_db_id=universe.id,
                world_id=temporary_world_id,
                slug=f"object-batch-{nonce}",
                name="ObjectBatch rollback integration world",
                world_role="sandbox",
                template_id="flat",
                provider_id="flat",
                provider_world_id="flat",
                block_registry_id=base_world.block_registry_id,
                block_registry_version=base_world.block_registry_version,
                metadata_json={"testScope": "object-batch-rollback"},
            )
            db.session.add(temporary_world)
            db.session.commit()
            db.session.remove()

            project, universe, world = commands._resolve_project_world_context(
                project_public_id,
                temporary_world_id,
                universe_id=universe_public_id,
            )
            blocks = _editable_blocks(world, limit=2)
            if len(blocks) < 2:
                pytest.skip("dev registry needs two distinct editable blocks")
            block_type_ids = [block.block_type_id for block in blocks]

            position = {"x": 10, "y": 15, "z": 10}
            old_object_id = f"obj_batch_old_generation_{nonce}"
            new_object_id = f"obj_batch_new_generation_{nonce}"
            missing_object_id = f"obj_batch_missing_{nonce}"
            batch_command_id = f"cmd_object_batch_rollback_{nonce}"

            commands._execute_command(
                project=project,
                universe=universe,
                world=world,
                payload={
                    "commandId": f"cmd_object_batch_seed_{nonce}",
                    "userId": "object_batch_test",
                    "sessionId": nonce,
                    **_place_payload(
                        old_object_id,
                        position,
                        block_type_ids[0],
                    ),
                },
            )
            db.session.commit()
            db.session.remove()

            # Baseline is committed so the rollback can be verified from a
            # genuinely fresh ORM session instead of trusting expired identity
            # map state from the writer.
            project, universe, world = commands._resolve_project_world_context(
                project_public_id,
                temporary_world_id,
                universe_id=universe_public_id,
            )

            old_object = commands._query_without_relationships(
                WorldObjectInstance.query.filter_by(
                    world_db_id=world.id,
                    object_instance_id=old_object_id,
                )
            ).one()
            old_ref = commands._query_without_relationships(
                WorldObjectChunkRef.query.filter_by(
                    object_instance_db_id=old_object.id,
                )
            ).one()
            old_object_db_id = old_object.id
            before_ref = {
                "occupiedCells": deepcopy(old_ref.occupied_cells_json),
                "metadata": deepcopy(old_ref.metadata_json),
                "deletedAt": old_ref.deleted_at,
            }
            before_state = _cell_state(project, universe, world, position)
            before_content = deepcopy(before_state["content"])
            before_snapshot = {
                "id": before_state["snapshot"].id,
                "revision": before_state["snapshot"].chunk_revision,
                "version": before_state["snapshot"].chunk_version,
                "contentHash": before_state["snapshot"].content_hash,
            }

            observed_mid_batch = {}
            original_remove_object = commands._execute_remove_object

            def observe_then_remove(**kwargs):
                state = _cell_state(project, universe, world, position)
                current_old_ref = commands._query_without_relationships(
                    WorldObjectChunkRef.query.filter_by(
                        object_instance_db_id=old_object_db_id,
                    )
                ).one()
                runtime_ids = [
                    ref.get("objectInstanceId")
                    for ref in state["content"].get("objectRefs", [])
                    if isinstance(ref, dict)
                ]
                observed_mid_batch.update(
                    {
                        "blockTypeId": state["blockTypeId"],
                        "oldOccupiedCells": deepcopy(current_old_ref.occupied_cells_json),
                        "runtimeObjectIds": runtime_ids,
                    }
                )
                return original_remove_object(**kwargs)

            monkeypatch.setattr(commands, "_execute_remove_object", observe_then_remove)

            with pytest.raises(LookupError, match=r"commands\[1\].*was not found"):
                commands._execute_command(
                    project=project,
                    universe=universe,
                    world=world,
                    payload={
                        "type": "ObjectBatch",
                        "commandId": batch_command_id,
                        "position": position,
                        "userId": "object_batch_test",
                        "sessionId": nonce,
                        "commands": [
                            _place_payload(
                                new_object_id,
                                position,
                                block_type_ids[1],
                            ),
                            {
                                "type": "RemoveObject",
                                "objectInstanceId": missing_object_id,
                            },
                        ],
                    },
                )

            # Prove the first child really displaced the old generation before
            # the second child failed. This prevents a vacuous rollback test.
            assert observed_mid_batch["blockTypeId"] == block_type_ids[1]
            assert observed_mid_batch["oldOccupiedCells"] == []
            assert new_object_id in observed_mid_batch["runtimeObjectIds"]

            # This mirrors the request error handler. Discard the complete
            # failed transaction, then open a fresh scoped session for every
            # persistence assertion below.
            db.session.rollback()
            db.session.remove()
            project, universe, world = commands._resolve_project_world_context(
                project_public_id,
                temporary_world_id,
                universe_id=universe_public_id,
            )
            after_state = _cell_state(project, universe, world, position)
            assert after_state["blockTypeId"] == block_type_ids[0]
            assert after_state["content"] == before_content
            assert after_state["snapshot"].id == before_snapshot["id"]
            assert after_state["snapshot"].chunk_revision == before_snapshot["revision"]
            assert after_state["snapshot"].chunk_version == before_snapshot["version"]
            assert after_state["snapshot"].content_hash == before_snapshot["contentHash"]

            restored_old_object = commands._query_without_relationships(
                WorldObjectInstance.query.filter_by(
                    world_db_id=world.id,
                    object_instance_id=old_object_id,
                )
            ).one()
            restored_old_ref = commands._query_without_relationships(
                WorldObjectChunkRef.query.filter_by(
                    object_instance_db_id=restored_old_object.id,
                )
            ).one()
            assert restored_old_object.deleted_at is None
            assert restored_old_ref.occupied_cells_json == before_ref["occupiedCells"]
            assert restored_old_ref.metadata_json == before_ref["metadata"]
            assert restored_old_ref.deleted_at == before_ref["deletedAt"]

            assert commands._query_without_relationships(
                WorldObjectInstance.query.filter_by(
                    world_db_id=world.id,
                    object_instance_id=new_object_id,
                )
            ).count() == 0
            assert commands._query_without_relationships(
                WorldCommandLog.query.filter_by(command_id=batch_command_id)
            ).count() == 0
            assert commands._query_without_relationships(
                ChunkEvent.query.filter_by(command_id=batch_command_id)
            ).count() == 0
        finally:
            db.session.rollback()
            db.session.remove()
            if temporary_world_id:
                cleanup_world = commands._query_without_relationships(
                    WorldInstance.query.filter_by(world_id=temporary_world_id)
                ).one_or_none()
                if cleanup_world is not None:
                    db.session.delete(cleanup_world)
                    db.session.commit()
                db.session.remove()
