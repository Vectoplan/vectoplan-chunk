from decimal import Decimal
from types import SimpleNamespace

import pytest

from models.event import VALID_COMMAND_TYPES, VALID_EVENT_TYPES
from models.object import normalize_object_kind
from src.world_edit.commands import WorldEditValidationError, build_world_edit_plan
from routes.commands import (
    _PLACE_OBJECT_GEOMETRY_UPDATE_ATTRIBUTES,
    _apply_existing_object_geometry_update,
    _extract_object_occupied_cells,
    _serialize_command_result,
    WORLD_EDIT_EVENT_TYPE,
)


class _Provider:
    def global_to_local(self, coordinate, source_crs):
        return SimpleNamespace(
            local_position=SimpleNamespace(
                x=(Decimal(coordinate.x) - Decimal("13")) * Decimal("100000"),
                y=Decimal("0"),
                z=(Decimal(coordinate.y) - Decimal("52")) * Decimal("100000"),
            )
        )


def test_semantic_footprint_is_a_valid_persisted_object_kind():
    assert normalize_object_kind("semantic_footprint") == "semantic_footprint"


def test_semantic_object_uses_explicit_deduplicated_occupied_cells():
    cells = _extract_object_occupied_cells(
        {
            "occupiedCells": [
                {"x": 7, "y": 2, "z": -4},
                {"x": 7, "y": 2, "z": -4},
                {"x": 8, "y": 2, "z": -4},
            ]
        },
        anchor={"x": 0, "y": 0, "z": 0},
        dimensions={"x": 1, "y": 1, "z": 1},
    )

    assert cells == [
        {"x": 7, "y": 2, "z": -4},
        {"x": 8, "y": 2, "z": -4},
    ]


def _object_stub(*, footprint, occupied_cells):
    values = {attribute: None for attribute in _PLACE_OBJECT_GEOMETRY_UPDATE_ATTRIBUTES}
    values.update({
        "footprint_json": footprint,
        "occupied_cells_json": occupied_cells,
        "occupied_cell_count": len(occupied_cells),
        "revision": 1,
    })
    value = SimpleNamespace(**values)

    def touch(*, updated_by_user_id=None, last_session_id=None):
        value.revision += 1
        value.touched_by = (updated_by_user_id, last_session_id)

    value.touch = touch
    return value


def test_existing_semantic_object_remesh_is_persisted_under_the_same_identity():
    occupied = [{"x": 7, "y": 2, "z": -4}]
    existing = _object_stub(footprint={"coordinates": [[[0, 0], [1, 0], [1, 1]]]}, occupied_cells=occupied)
    candidate = _object_stub(footprint={"coordinates": [[[0, 0], [0.8, 0], [0.7, 1]]]}, occupied_cells=occupied)

    result = _apply_existing_object_geometry_update(
        existing,
        candidate,
        command_id="cmd-remesh",
        user_id="editor_user",
        session_id="parcel_grid_geometry_migration",
    )

    assert result is existing
    assert existing.footprint_json == candidate.footprint_json
    assert existing.updated_by_command_id == "cmd-remesh"
    assert existing.revision == 2
    assert existing.touched_by == ("editor_user", "parcel_grid_geometry_migration")


def test_existing_semantic_object_remesh_cannot_move_its_occupied_voxel():
    existing = _object_stub(footprint={}, occupied_cells=[{"x": 7, "y": 2, "z": -4}])
    candidate = _object_stub(footprint={}, occupied_cells=[{"x": 8, "y": 2, "z": -4}])

    with pytest.raises(ValueError, match="same occupiedCells"):
        _apply_existing_object_geometry_update(
            existing,
            candidate,
            command_id="cmd-invalid-remesh",
            user_id="editor_user",
            session_id="parcel_grid_geometry_migration",
        )


def _mask(*polygons):
    return {
        "enabled": True,
        "coordinateSpace": "world-xz",
        "coveragePolicy": "cell-center",
        "parcels": [
            {
                "parcelId": f"parcel-{index}",
                "geometry": {"type": "Polygon", "coordinates": [polygon]},
            }
            for index, polygon in enumerate(polygons)
        ],
    }


def test_selection_set_is_clipped_to_selected_parcel_union():
    plan = build_world_edit_plan(
        {
            "tool": "selection",
            "operation": "set",
            "bounds": {"min": {"x": 0, "y": 4, "z": 0}, "max": {"x": 3, "y": 4, "z": 1}},
            "parcelMask": _mask(
                [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]],
                [[3, 0], [4, 0], [4, 2], [3, 2], [3, 0]],
            ),
        }
    )

    assert plan.requested_cell_count == 8
    assert plan.positions == (
        (0, 4, 0), (1, 4, 0), (3, 4, 0),
        (0, 4, 1), (1, 4, 1), (3, 4, 1),
    )
    assert plan.parcel_count == 2


def test_cell_contained_policy_rejects_a_cell_cut_by_slanted_boundary():
    mask = _mask([[0, 0], [3, 0], [1, 2], [0, 2], [0, 0]])
    mask["coveragePolicy"] = "cell-contained"
    plan = build_world_edit_plan(
        {
            "tool": "selection",
            "operation": "set",
            "bounds": {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 0, "z": 1}},
            "parcelMask": mask,
        }
    )

    assert (2, 0, 0) not in plan.positions
    assert (1, 0, 1) not in plan.positions
    assert plan.positions == ((0, 0, 0), (1, 0, 0), (0, 0, 1))


def test_cell_contained_policy_uses_the_union_of_selected_parcels():
    mask = _mask(
        [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
        [[1, 0], [2, 0], [2, 1], [1, 1], [1, 0]],
    )
    mask["coveragePolicy"] = "cell-contained"
    plan = build_world_edit_plan(
        {
            "tool": "selection",
            "operation": "set",
            "bounds": {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 0, "z": 0}},
            "parcelMask": mask,
        }
    )

    assert plan.positions == ((0, 0, 0), (1, 0, 0))


def test_selection_wall_only_emits_horizontal_shell():
    plan = build_world_edit_plan(
        {
            "tool": "selection",
            "operation": "wall",
            "bounds": {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 2, "y": 1, "z": 2}},
            "parcelMask": {"enabled": False},
        }
    )

    assert len(plan.positions) == 16
    assert (1, 0, 1) not in plan.positions


def test_paint_sphere_is_deterministic_and_bounded():
    payload = {
        "tool": "paint",
        "operation": "set",
        "position": {"x": 4, "y": 5, "z": 6},
        "brush": {"shape": "sphere", "radius": 2, "density": 55},
        "parcelMask": {"enabled": False},
    }
    first = build_world_edit_plan(payload)
    second = build_world_edit_plan(payload)

    assert first.positions == second.positions
    assert 0 < len(first.positions) == first.requested_cell_count <= 125


def test_wgs84_mask_uses_earth_provider(monkeypatch):
    monkeypatch.setattr(
        "src.world_edit.commands.canonical_geographic_crs",
        lambda: object(),
    )
    plan = build_world_edit_plan(
        {
            "tool": "selection",
            "operation": "fill",
            "bounds": {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 0, "z": 1}},
            "parcelMask": {
                "enabled": True,
                "coordinateSpace": "wgs84",
                "parcels": [{
                    "parcelId": "wgs84-1",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [13.0, 52.0], [13.00002, 52.0], [13.00002, 52.00002],
                            [13.0, 52.00002], [13.0, 52.0],
                        ]],
                    },
                }],
            },
        },
        provider=_Provider(),
    )

    assert len(plan.positions) == 4


def test_wgs84_mask_uses_the_immutable_earth_grid_even_with_display_rotation(monkeypatch):
    monkeypatch.setattr(
        "src.world_edit.commands.canonical_geographic_crs",
        lambda: object(),
    )
    plan = build_world_edit_plan(
        {
            "tool": "selection",
            "operation": "fill",
            "bounds": {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 1, "y": 0, "z": 0}},
            "parcelMask": {
                "enabled": True,
                "coordinateSpace": "wgs84",
                "gridRotationDegrees": 90,
                "parcels": [{
                    "parcelId": "rotated-wgs84",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [13.0, 52.0], [13.00001, 52.0], [13.00001, 52.00002],
                            [13.0, 52.00002], [13.0, 52.0],
                        ]],
                    },
                }],
            },
        },
        provider=_Provider(),
    )

    # The parcel is one local metre wide.  With the immutable Earth frame only
    # the first cell centre (0.5, 0.5) is covered; presentation rotation must
    # not turn its two-metre north/south span into a second east/west cell.
    assert plan.positions == ((0, 0, 0),)


def test_enabled_empty_parcel_mask_fails_closed():
    with pytest.raises(WorldEditValidationError, match="kein Grundstueck"):
        build_world_edit_plan(
            {
                "tool": "selection",
                "operation": "set",
                "bounds": {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 0, "y": 0, "z": 0}},
                "parcelMask": {"enabled": True, "parcels": []},
            }
        )


def test_cell_limit_is_enforced_before_command_execution():
    with pytest.raises(WorldEditValidationError, match="mehr als 10"):
        build_world_edit_plan(
            {
                "tool": "selection",
                "operation": "set",
                "bounds": {"min": {"x": 0, "y": 0, "z": 0}, "max": {"x": 10, "y": 0, "z": 0}},
                "parcelMask": {"enabled": False},
            },
            max_cells=10,
        )


def test_clipboard_copy_and_cut_use_the_existing_selection_bounds():
    base = {
        "tool": "clipboard",
        "bounds": {"min": {"x": 2, "y": 3, "z": 4}, "max": {"x": 3, "y": 4, "z": 5}},
        "parcelMask": {"enabled": False},
    }
    copy_plan = build_world_edit_plan({**base, "operation": "copy"})
    cut_plan = build_world_edit_plan({**base, "operation": "cut"})

    assert copy_plan.positions == cut_plan.positions
    assert len(copy_plan.positions) == 8


def test_clipboard_paste_offsets_cells_from_target():
    plan = build_world_edit_plan({
        "tool": "clipboard",
        "operation": "paste",
        "position": {"x": 10, "y": 20, "z": 30},
        "clipboard": [
            {"dx": 0, "dy": 0, "dz": 0, "blockTypeId": "stone"},
            {"dx": 2, "dy": 1, "dz": -1, "blockTypeId": None},
        ],
        "parcelMask": {"enabled": False},
    })

    assert plan.positions == ((10, 20, 30), (12, 21, 29))


def test_command_response_preserves_clipboard_capture():
    project = SimpleNamespace(project_id="project")
    universe = SimpleNamespace(universe_id="universe")
    world = SimpleNamespace(
        world_id="world",
        template_id="earth",
        provider_id="earth",
        provider_world_id="earth",
    )
    command_log = SimpleNamespace(
        command_id="command",
        command_type="WorldEdit",
        command_status="noop",
    )
    clipboard = [{"dx": 0, "dy": 0, "dz": 0, "blockTypeId": "stone"}]

    result = _serialize_command_result(
        project=project,
        universe=universe,
        world=world,
        command_log=command_log,
        result={
            "changed": False,
            "commandType": "WorldEdit",
            "clipboard": clipboard,
            "worldEdit": {"operation": "copy", "clipboardCellCount": 1},
        },
        include_command_log=False,
    )

    assert result["clipboard"] == clipboard
    assert result["worldEdit"]["clipboardCellCount"] == 1


def test_sculpt_accepts_a_single_horizontal_box_layer():
    plan = build_world_edit_plan({
        "tool": "sculpt",
        "operation": "clear",
        "position": {"x": 10, "y": 7, "z": 20},
        "brush": {
            "shape": "box",
            "radius": 5,
            "radiusX": 5,
            "radiusY": 0,
            "radiusZ": 5,
        },
        "parcelMask": {"enabled": False},
    })

    assert len(plan.positions) == 121
    assert {position[1] for position in plan.positions} == {7}
    assert (5, 7, 15) in plan.positions
    assert (15, 7, 25) in plan.positions


def test_tentacle_expands_a_deduplicated_brush_along_the_path():
    plan = build_world_edit_plan({
        "tool": "tentacle",
        "operation": "clear",
        "position": {"x": 0, "y": 0, "z": 0},
        "path": [
            {"x": 0, "y": 4, "z": 0},
            {"x": 1, "y": 4, "z": 0},
            {"x": 2, "y": 4, "z": 1},
        ],
        "brush": {"shape": "box", "radius": 1},
        "parcelMask": {"enabled": False},
    })

    assert plan.tool == "tentacle"
    assert len(plan.positions) == len(set(plan.positions))
    assert (-1, 3, -1) in plan.positions
    assert (3, 5, 2) in plan.positions


def test_tentacle_rejects_an_incomplete_path():
    with pytest.raises(WorldEditValidationError, match="mindestens zwei"):
        build_world_edit_plan({
            "tool": "tentacle",
            "operation": "clear",
            "path": [{"x": 0, "y": 0, "z": 0}],
            "brush": {"shape": "sphere", "radius": 1},
            "parcelMask": {"enabled": False},
        })


def test_world_edit_is_a_persistable_command_type():
    assert "WorldEdit" in VALID_COMMAND_TYPES


def test_world_edit_uses_a_persistable_region_event_type():
    assert WORLD_EDIT_EVENT_TYPE == "region_change"
    assert WORLD_EDIT_EVENT_TYPE in VALID_EVENT_TYPES
