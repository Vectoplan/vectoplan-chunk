import copy
import pytest
from shapely.geometry import Polygon

from src.geodata.lod2_conversion import (
    _profile_height, convert_building, facade_segments, ground_footprints,
    roof_objects, wall_cells,
)


def test_roof_height_steps_are_separate_but_a_ridge_is_one_connected_roof():
    def face(points):
        return {"surface":"RoofSurface","rings":[[*points,points[0]]]}
    feature = {"id":"levels","sourceTile":"test.zip","sourceSha256":"a"*64,"polygons":[
        face([[0,5,0],[4,5,0],[4,7,2],[0,7,2]]),
        face([[0,7,2],[4,7,2],[4,5,4],[0,5,4]]),
        face([[4,12,0],[6,12,0],[6,12,4],[4,12,4]]),
    ]}
    roofs = roof_objects(feature)
    assert len(roofs) == 2
    assert sorted(r["footprint"]["baseY"] for r in roofs) == [5,12]
    assert sorted(len(r["metadata"]["roofCalculation"]["geometry"]["faces"]) for r in roofs) == [2,4]


def test_duplicate_source_facets_do_not_produce_z_fighting():
    surface = {"surface":"RoofSurface","rings":[[[0,5,0],[4,5,0],[4,5,4],[0,5,0]]]}
    roofs = roof_objects({"id":"duplicate","sourceTile":"test.zip","sourceSha256":"a"*64,"polygons":[surface,surface]})
    assert len(roofs) == 1
    assert len(roofs[0]["metadata"]["roofCalculation"]["geometry"]["faces"]) == 1


def wall(ring, holes=()):
    return {"surface": "WallSurface", "rings": [ring, *holes]}


def test_wall_is_hollow_one_cell_shell_and_crosses_negative_chunk_boundary():
    cells = wall_cells([wall([[-17, 1, 2], [-15, 1, 2], [-15, 4, 2], [-17, 4, 2], [-17, 1, 2]])])
    assert set(cells) == {(x, y, 2) for x in (-17, -16) for y in (1, 2, 3)}
    assert {c[0] // 16 for c in cells} == {-2, -1}


def test_wall_openings_and_angled_facades_are_not_filled_as_building_volumes():
    cells = wall_cells([wall([[0, 0, 0], [0, 0, 5], [0, 5, 5], [0, 5, 0], [0, 0, 0]],
                            [[[0, 1, 1], [0, 4, 1], [0, 4, 4], [0, 1, 4], [0, 1, 1]]])])
    assert len(cells) == 16
    assert (0, 2, 2) not in cells
    angled = wall_cells([wall([[0, 0, 0], [5, 0, 5], [5, 3, 5], [0, 3, 0], [0, 0, 0]])])
    assert set(angled) == {(x, y, x) for x in range(5) for y in range(3)}


def test_exact_facade_reference_keeps_rotated_edge_and_height_for_editor_grid():
    surface = wall([[0.2, 1, 0.3], [5.7, 1, 4.6], [5.7, 8, 4.6], [0.2, 8, 0.3], [0.2, 1, 0.3]])
    assert facade_segments([surface]) == [{
        "start": [0.2, 0.3], "end": [5.7, 4.6], "minimumY": 1, "maximumY": 8,
        "topProfile": [[0.0, 8.0], [6.981404, 8.0]],
        "bottomProfile": [[0.0, 1.0], [6.981404, 1.0]],
        "facadeRole": "source",
    }]


def test_facade_profile_preserves_a_vertical_annex_step_instead_of_inventing_a_diagonal_wall():
    surface = wall([
        [0, 20, 0], [9.99, 20, 0], [9.99, 1, 0], [10, 1, 0],
        [10, 22, 0], [0, 22, 0], [0, 20, 0],
    ])
    profile = facade_segments([surface])[0]["bottomProfile"]
    assert _profile_height(profile, 5) == pytest.approx(20)
    assert _profile_height(profile, 9.995) == pytest.approx(1, abs=.01)
    assert any(9.98 < along < 9.99 and height == pytest.approx(20)
               for along, height in profile)


def test_ground_surface_is_the_persisted_wall_footprint_and_keeps_courtyard_holes():
    surfaces = [{"surface": "GroundSurface", "rings": [
        [[0, 1, 0], [8, 1, 0], [8, 1, 6], [0, 1, 6], [0, 1, 0]],
        [[2, 1, 2], [2, 1, 4], [6, 1, 4], [6, 1, 2], [2, 1, 2]],
    ]}]
    assert ground_footprints(surfaces) == [[
        [[0, 0], [8, 0], [8, 6], [0, 6], [0, 0]],
        [[2, 2], [2, 4], [6, 4], [6, 2], [2, 2]],
    ]]


def test_roof_source_carries_exact_facades_for_existing_building_grid():
    feature = source_roof()
    feature["polygons"].append(wall([
        [0, 0, 0], [8, 0, 0], [8, 6, 0], [0, 6, 0], [0, 0, 0],
    ]))
    feature["polygons"].append({"surface": "GroundSurface", "rings": [[
        [0, 0, 0], [8, 0, 0], [8, 0, 8], [0, 0, 8], [0, 0, 0],
    ]]})
    roofs = roof_objects(feature)
    assert len(roofs) == 1
    source = roofs[0]["metadata"]["roofParameters"]["importedSource"]
    assert source["facadeSegments"] == [{
        "start": [0, 0], "end": [8, 0], "minimumY": 0, "maximumY": 6,
        "topProfile": [[0.0, 6.0], [8.0, 6.0]],
        "bottomProfile": [[0.0, 0.0], [8.0, 0.0]],
        "facadeRole": "exterior",
    }]
    assert source["groundFootprints"] == [[[
        [0, 0], [8, 0], [8, 8], [0, 8], [0, 0],
    ]]]


def test_ground_footprint_repairs_a_leaning_wall_to_one_vertical_axis():
    surfaces = [
        {"surface": "GroundSurface", "rings": [[
            [0, 1, 0], [8, 1, 0], [8, 1, 6], [0, 1, 6], [0, 1, 0],
        ]]},
        wall([[0, 1, 0], [8, 1, 0], [8.7, 7, .5], [.7, 7, .5], [0, 1, 0]]),
    ]
    segment = next(item for item in facade_segments(surfaces)
                   if item["start"] == [0, 0] and item["end"] == [8, 0])
    assert segment["bottomProfile"] == [[0.0, 1.0], [8.0, 1.0]]
    assert segment["topProfile"] == [[0.0, 7.0], [8.0, 7.0]]
    cells = wall_cells(surfaces)
    assert cells
    assert {cell[2] for cell in cells} == {0}


def test_roof_level_sliver_and_horizontal_misclassification_do_not_create_block_walls():
    surfaces = [
        {"surface": "GroundSurface", "rings": [[
            [0, 1, 0], [8, 1, 0], [8, 1, 6], [0, 1, 6], [0, 1, 0],
        ]]},
        wall([[0, 1, 0], [8, 1, 0], [8, 7, 0], [0, 7, 0], [0, 1, 0]]),
        wall([[0, 7, .08], [8, 7, .08], [8, 7.2, .16], [0, 7.2, .16], [0, 7, .08]]),
        wall([[0, 8, 2], [8, 8, 2], [8, 8.1, 4], [0, 8.1, 4], [0, 8, 2]]),
    ]
    segments = facade_segments(surfaces)
    assert len(segments) == 1
    assert segments[0]["start"] == [0, 0]
    assert segments[0]["end"] == [8, 0]
    assert segments[0]["maximumY"] == 7


def test_ground_edge_splits_a_complex_wall_and_projects_its_lower_profile_down():
    surfaces = [
        {"surface": "GroundSurface", "rings": [[
            [0, 1, 0], [5, 1, 0], [5.02, 1, -.01], [5, 1, 5], [0, 1, 5], [0, 1, 0],
        ]]},
        wall([[0, 12, 0], [0, 10, 0], [5, 10, 0], [5, 1, 0],
              [5.02, 1, -.01], [5.02, 13, -.01], [0, 12, 0]]),
    ]
    segment = next(item for item in facade_segments(surfaces)
                   if item["start"] == [0, 0] and item["end"] == [5, 0])
    assert _profile_height(segment["bottomProfile"], 2.5) == pytest.approx(1)
    assert _profile_height(segment["topProfile"], 2.5) >= 10


def source_roof():
    return {"id": "fixture", "sourceTile": "tile.zip", "sourceSha256": "a"*64,
            "polygons": [{"surface": "RoofSurface", "rings": [
                [[0, 6, 0], [8, 6, 0], [8, 10, 8], [0, 10, 8], [0, 6, 0]],
                [[2, 7, 2], [2, 9, 6], [6, 9, 6], [6, 7, 2], [2, 7, 2]]]}]}


def test_roof_is_canonical_editable_object_preserving_courtyard_and_xyz_units():
    feature = source_roof()
    before = copy.deepcopy(feature)
    roofs = roof_objects(feature)
    assert feature == before
    assert len(roofs) == 1
    roof = roofs[0]
    assert roof["objectTypeId"] == "building_roof"
    assert roof["metadata"]["voxelOccupancy"] == "none"
    assert len(roof["footprint"]["coordinates"]) == 2
    faces = roof["metadata"]["roofCalculation"]["geometry"]["faces"]
    area = sum(Polygon([(p[0]/1000, p[1]/1000) for p in f["polygon_3d_mm"]]).area for f in faces)
    assert area == pytest.approx(48)
    for face in faces:
        for x, z, y in face["polygon_3d_mm"]:
            assert y == pytest.approx(6000 + z*.5)
    assert roof_objects(feature) == roofs


def test_budget_and_invalid_geometry_fail_before_any_partial_conversion():
    with pytest.raises(ValueError, match="budget"):
        wall_cells([wall([[0, 0, 0], [0, 0, 10], [0, 10, 10], [0, 10, 0], [0, 0, 0]])], max_cells=5)
    with pytest.raises(ValueError, match="classified walls"):
        convert_building(source_roof())
    bad = source_roof()
    bad["polygons"][0]["rings"][0][1][1] = float("nan")
    with pytest.raises(ValueError, match="XYZ"):
        roof_objects(bad)


def test_large_roof_is_partitioned_without_losing_area_or_changing_its_plane():
    feature = {"id": "large", "sourceTile": "tile.zip", "sourceSha256": "b"*64,
               "polygons": [{"surface": "RoofSurface", "rings": [[
                   [-30, 10, 0], [300, 10, 0], [300, 20, 20], [-30, 20, 20], [-30, 10, 0]]]}]}
    roofs = roof_objects(feature)
    assert len(roofs) == 4
    area = 0
    for roof in roofs:
        assert max(roof["dimensions"].values()) <= 256
        for face in roof["metadata"]["roofCalculation"]["geometry"]["faces"]:
            area += Polygon([(p[0]/1000, p[1]/1000) for p in face["polygon_3d_mm"]]).area
            for x, z, y in face["polygon_3d_mm"]:
                assert y == pytest.approx(10000+z/2, abs=.001)
    assert area == pytest.approx(330*20)


def test_steep_roof_plane_uses_a_wide_triangle_and_does_not_amplify_centimetre_noise():
    def height(x, z):
        return 10 + 100*x + .5*z
    ring = [
        [0, height(0, 0), 0],
        [.1, height(.1, .1), .1],
        [.2, height(.2, .200002) + .01, .200002],
        [4, height(4, 0), 0],
        [4, height(4, 4), 4],
        [0, height(0, 4), 4],
        [0, height(0, 0), 0],
    ]
    feature = {"id": "stable-plane", "sourceTile": "tile.zip", "sourceSha256": "c"*64,
               "polygons": [{"surface": "RoofSurface", "rings": [ring]}]}
    roofs = roof_objects(feature)
    assert roofs
    heights = []
    for roof in roofs:
        for face in roof["metadata"]["roofCalculation"]["geometry"]["faces"]:
            for x_mm, z_mm, y_mm in face["polygon_3d_mm"]:
                x, z, y = x_mm/1000, z_mm/1000, y_mm/1000
                heights.append(y)
                assert y == pytest.approx(height(x, z), abs=.002)
    assert max(heights) < 500


def test_materialized_receipt_suppresses_source_overlay_even_after_roof_deletion(tmp_path, monkeypatch):
    from test_lod2_buildings import imported, provider
    from types import SimpleNamespace
    from src.geodata.lod2_buildings import building_overlay_item
    database, *_ = imported(tmp_path)
    monkeypatch.setenv("VECTOPLAN_CHUNK_LOD2_STORE", str(database))
    world = SimpleNamespace(surface_y=0, metadata_json={"lod2Buildings": {
        "enabled": True, "allowFlatTerrainAlignment": True, "materializedBuildings": {"berlin-1": {"roofIds": []}}}})
    kwargs = {"world": world, "provider": provider(), "chunk": {"chunkSize": 16}}
    assert building_overlay_item(**kwargs)["geometry"]["features"] == []
    assert len(building_overlay_item(**kwargs, include_materialized=True)["geometry"]["features"]) == 1


def test_bulk_commands_reuse_validated_cell_buffer_without_changing_json_encoding():
    import json
    from routes.commands import _ensure_cells, _set_cell_value
    content = {"cells": ["1", "invalid", 0]}
    cells = _ensure_cells(content, chunk_size=2)
    assert cells == [1, 0, 0, 0, 0, 0, 0, 0]
    _set_cell_value(content, local_x=1, local_y=1, local_z=1, chunk_size=2, cell_value=5)
    assert _ensure_cells(content, chunk_size=2) is cells
    assert json.loads(json.dumps(content))["cells"] == [1, 0, 0, 0, 0, 0, 0, 5]
