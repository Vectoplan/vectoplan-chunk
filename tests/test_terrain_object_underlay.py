from copy import deepcopy
import os

import pytest

from src.terrain_object_underlay import (
    capture_terrain_underlay, terrain_underlay, terrain_restore_value, restore_terrain_cut_flag,
)


def test_terrain_journal_survives_geometry_updates_without_becoming_a_wall_restore():
    original = capture_terrain_underlay("system_terrain_humus", full_cell=False)
    current = {"beforeBlockTypeId": "system_terrain_humus", "afterBlockTypeId": "wall", "terrainUnderlay": original}
    for _ in range(3):
        journal = capture_terrain_underlay("wall", full_cell=True, previous_cell=current)
        assert journal == original
        current = {"beforeBlockTypeId": "wall", "afterBlockTypeId": "wall", "terrainUnderlay": journal}
    assert capture_terrain_underlay("manual-block", full_cell=True, previous_cell=current) is None
    assert capture_terrain_underlay(None, full_cell=True, previous_cell=current) is None


@pytest.mark.parametrize("before", [None, "old-wall", "old-roof", "manual-brick", "system_terrainlookalike"])
def test_no_generator_fill_or_resurrection_of_displaced_objects(before):
    assert terrain_underlay({"beforeBlockTypeId": before}) is None
    assert terrain_restore_value({"palette": [{"blockTypeId": before}]},
        {"beforeBlockTypeId": before}, generated_building=True) == (0, None)


def test_legacy_material_is_resolved_by_id_without_guessing_cut_state_or_palette_index():
    content = {"palette": [{"blockTypeId": "wall"}, {"blockTypeId": "system_terrain_humus"}],
        "metadata": {"terrainSurface": {"fullCellIndices": [3, 5]}}}
    cell = {"beforeBlockTypeId": "system_terrain_humus", "beforeCellValue": 99}
    value, underlay = terrain_restore_value(content, cell, generated_building=True)
    assert value == 2
    assert underlay == {"blockTypeId": "system_terrain_humus"}
    restore_terrain_cut_flag(content, 3, underlay)
    assert content["metadata"]["terrainSurface"]["fullCellIndices"] == [3, 5]
    assert terrain_restore_value(content, cell, generated_building=False) == (0, None)
    assert terrain_restore_value({"palette": []}, cell, generated_building=True) == (0, None)


@pytest.mark.parametrize("was_full", [False, True])
def test_restoring_the_prior_cut_flag_preserves_unrelated_manual_edits(was_full):
    content = {"metadata": {"terrainSurface": {"cornerHeights": [1.1, 1.2, 1.3, 1.4], "fullCellIndices": [3, 5]}}}
    restore_terrain_cut_flag(content, 3, {"blockTypeId": "system_terrain_humus", "fullCell": was_full})
    assert content["metadata"]["terrainSurface"]["fullCellIndices"] == ([3, 5] if was_full else [5])
    assert content["metadata"]["terrainSurface"]["cornerHeights"] == [1.1, 1.2, 1.3, 1.4]


@pytest.fixture
def isolated_world():
    if os.getenv("VECTOPLAN_RUN_DB_INTEGRATION_TESTS") != "1":
        pytest.skip("requires the disposable local Chunk integration database")
    from tests.test_lod2_building_edit import imported_building_world
    yield from imported_building_world.__wrapped__()


def test_real_object_removal_restores_terrain_but_retains_manual_air_and_replacements(isolated_world, monkeypatch):
    from routes import commands
    from models import WorldObjectChunkRef
    from src.geodata.lod2_building_edit import WALL_BLOCK_ID
    from tests.test_object_batch import _place_payload

    fixture = isolated_world
    positions = [{"x": 40 + i, "y": 15, "z": 8} for i in range(6)]
    terrain_id = "system_terrain_humus"
    original_load = commands._load_chunk_for_mutation

    def seeded_terrain(**kwargs):
        snapshot, content = original_load(**kwargs)
        if snapshot is None and kwargs["chunk_x"] == 2 and kwargs["chunk_y"] == 0 and kwargs["chunk_z"] == 0:
            # Stand-in only for the external DGM generator. Real snapshots,
            # object refs, commands, manual ownership and removals stay active.
            content["palette"].append({"blockTypeId": terrain_id, "solid": True, "breakable": True})
            cells = commands._ensure_cells(content, chunk_size=16)
            full = []
            for i, point in enumerate(positions):
                index = commands._flatten_cell_index(point["x"] % 16, 15, 8, 16)
                cells[index] = 0 if i == 4 else len(content["palette"])
                if i in (4, 5):
                    full.append(index)  # Existing excavation and explicit full terrain cube.
            content["metadata"] = {**content.get("metadata", {}), "terrainSurface": {
                "cornerHeights": [15.3] * 289, "fullCellIndices": full, "sampleStepM": 1}}
        return snapshot, content

    monkeypatch.setattr(commands, "_load_chunk_for_mutation", seeded_terrain)

    def place(identity, cells):
        return fixture.execute({**_place_payload(identity, cells[0], fixture.fill),
            "occupiedCells": deepcopy(cells), "metadata": {"generatedFromAreaId": "terrain-parent"}})

    def remove(identity):
        return fixture.execute({"type": "RemoveObject", "objectInstanceId": identity})[1]

    def full(point):
        state = fixture.state(point)
        index = commands._flatten_cell_index(point["x"] % 16, 15, 8, 16)
        return index in state["content"]["metadata"]["terrainSurface"]["fullCellIndices"]

    place("terrain-wall", positions)
    place("terrain-wall", positions)  # Same-ID geometry rewrite must retain original underlay.
    ref = commands._query_without_relationships(WorldObjectChunkRef.query.filter_by(
        world_db_id=fixture.world.id, object_instance_id="terrain-wall")).one()
    assert ref.occupied_cells_json[0]["terrainUnderlay"] == {"blockTypeId": terrain_id, "fullCell": False}
    fixture.execute({"type": "RemoveBlock", "position": positions[1]})
    fixture.execute({"type": "SetBlock", "position": positions[2], "blockTypeId": WALL_BLOCK_ID})
    fixture.execute({"type": "SetBlock", "position": positions[3], "blockTypeId": fixture.fill})
    result = remove("terrain-wall")
    assert [fixture.state(p)["blockTypeId"] for p in positions] == [terrain_id, None, WALL_BLOCK_ID, fixture.fill, None, terrain_id]
    assert not full(positions[0]) and full(positions[5])
    assert any(cell["afterBlockTypeId"] == terrain_id for cell in result["affectedCells"])

    # Normal retirement before replacement keeps the underlying cut terrain
    # intact over several building generations and a later move off this cell.
    for i in range(3):
        place(f"generation-{i}", [positions[0]])
        remove(f"generation-{i}")
        assert fixture.state(positions[0])["blockTypeId"] == terrain_id
        assert not full(positions[0])

    # Historical displaced generations must never resurrect an obsolete wall.
    place("old-overlap", [positions[0]])
    place("new-overlap", [positions[0]])
    remove("old-overlap")
    assert fixture.state(positions[0])["blockTypeId"] == fixture.fill
    remove("new-overlap")
    assert fixture.state(positions[0])["blockTypeId"] is None
