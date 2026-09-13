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


def test_whole_lod2_building_read_requires_view_access_and_does_not_relax_commands():
    from flask import Blueprint, Flask
    from src.services.project_route_authorization import _operation
    app = Flask(__name__)
    blueprint = Blueprint("commands", __name__)
    blueprint.add_url_rule("/building", endpoint="get_lod2_building", view_func=lambda: "", methods=["GET"])
    blueprint.add_url_rule("/command", endpoint="post_project_world_command", view_func=lambda: "", methods=["POST"])
    app.register_blueprint(blueprint)
    with app.test_request_context("/building"):
        assert _operation() == "world.read"
    with app.test_request_context("/command", method="POST"):
        assert _operation() == "commands.execute"
