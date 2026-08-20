from src.services.project_route_authorization import _core_runtime_operation_allowed


def test_core_can_execute_the_bound_world_command_translation_path():
    assert _core_runtime_operation_allowed("commands", "commands.execute") is True


def test_core_can_still_read_existing_chunk_snapshots():
    assert _core_runtime_operation_allowed("chunks", "chunks.read") is True
    assert _core_runtime_operation_allowed("chunks", "chunks.batch.read") is True


def test_core_cannot_use_other_runtime_mutation_routes():
    assert _core_runtime_operation_allowed("chunks", "chunks.materialize") is False
    assert _core_runtime_operation_allowed("worlds", "world.mutate") is False
    assert _core_runtime_operation_allowed("projects", "project.manage") is False
    assert _core_runtime_operation_allowed("commands", "chunks.read") is False
