import copy
import math

import pytest

from src.geodata.lod2_conversion import (
    CONSTRUCTION_GRID_VERSION,
    construction_grid_contract,
    convert_building,
)


def transform(point, *, angle=0, offset=(0, 0)):
    radians = math.radians(angle)
    x, z = point
    return [
        offset[0] + x * math.cos(radians) - z * math.sin(radians),
        offset[1] + x * math.sin(radians) + z * math.cos(radians),
    ]


def fixture(*, width=8.4, depth=5.2, angle=0, offset=(0, 0), building_id="existing-1"):
    plan = [(0, 0), (width, 0), (width, depth), (0, depth)]
    ring = [transform(point, angle=angle, offset=offset) for point in [*plan, plan[0]]]
    facades = []
    polygons = [{"surface": "GroundSurface", "rings": [[
        [x, 0, z] for x, z in ring
    ]]}]
    for start, end in zip(ring, ring[1:]):
        length = math.dist(start, end)
        facades.append({
            "start": start,
            "end": end,
            "minimumY": 0,
            "maximumY": 6,
            "topProfile": [[0, 6], [length, 6]],
            "bottomProfile": [[0, 0], [length, 0]],
            "facadeRole": "exterior",
        })
        polygons.append({"surface": "WallSurface", "rings": [[
            [start[0], 0, start[1]], [end[0], 0, end[1]],
            [end[0], 6, end[1]], [start[0], 6, start[1]],
            [start[0], 0, start[1]],
        ]]})
    polygons.append({"surface": "RoofSurface", "rings": [[
        [x, 6, z] for x, z in ring
    ]]})
    return {
        "feature": {
            "id": building_id,
            "sourceTile": "LoD2_fixture.zip",
            "sourceSha256": "a" * 64,
            "polygons": polygons,
        },
        "footprints": [[ring]],
        "facades": facades,
    }


def contract(data):
    return construction_grid_contract(
        data["feature"],
        building_facades=data["facades"],
        building_ground_footprints=data["footprints"],
    )


def angle_distance(first, second):
    difference = abs(first - second) % 180
    return min(difference, 180 - difference)


def test_existing_building_owns_rotated_basis_complete_wall_columns_and_anchored_remainder_policy():
    data = fixture(angle=27, offset=(17.25, -4.5))
    result = contract(data)

    assert result["schemaVersion"] == CONSTRUCTION_GRID_VERSION
    assert result["referenceMode"] == "lod2-existing-building"
    assert angle_distance(result["rotationDegrees"], 27) < 1e-7
    assert result["columns"] == 8
    assert result["rows"] == 5
    assert result["partitionPolicy"] == {
        "algorithm": "anchored-axis-lines.v1",
        "targetCellSizeM": 1,
        "buildingFacadeCells": "complete-block-columns",
        "betweenFacadeAnchors": "equal-near-metre-whole-cells",
        "parcelBoundaryCells": "adapted-clipped-cells",
        "buildingFootprint": "excluded-from-buildable-remainder",
        "emptyParcelFallback": "existing-parcel-boundary-oriented-grid",
    }
    assert len(result["facades"]) == 4
    for facade in result["facades"]:
        assert facade["wallCellOwnership"] == "one-complete-block-per-column-and-height-layer"
        assert facade["columnWidthM"] * facade["columnCount"] == pytest.approx(facade["lengthM"])
        assert facade["columnCount"] in (5, 8)
    assert len(result["uAnchors"]) == 2
    assert len(result["vAnchors"]) == 2


def test_annex_support_lines_become_stable_grid_anchors_without_rotating_each_notch():
    data = fixture(width=9, depth=6, angle=13)
    # A measured two-metre annex facade parallel to axis V contributes an
    # interior U support.  It cannot become a separate local raster.
    start = transform((4, 6), angle=13)
    end = transform((4, 8), angle=13)
    data["facades"].append({
        "start": start,
        "end": end,
        "minimumY": 0,
        "maximumY": 4,
        "topProfile": [[0, 4], [2, 4]],
        "bottomProfile": [[0, 0], [2, 0]],
        "facadeRole": "exterior",
    })
    result = contract(data)

    assert angle_distance(result["rotationDegrees"], 13) < 1e-7
    assert len(result["uAnchors"]) == 3
    assert result["uAnchors"][1] - result["uAnchors"][0] == pytest.approx(4)
    assert result["facades"][0]["id"] != result["facades"][1]["id"]


def test_contract_is_deterministic_for_source_order_ring_winding_and_segment_direction():
    first = fixture(angle=31, offset=(-25, 70))
    second = copy.deepcopy(first)
    second["facades"] = [
        {**segment, "start": segment["end"], "end": segment["start"]}
        for segment in reversed(second["facades"])
    ]
    reversed_ring = list(reversed(second["footprints"][0][0]))
    second["footprints"] = [[reversed_ring]]

    assert contract(first) == contract(second)


def test_empty_parcel_falls_through_to_existing_boundary_oriented_grid():
    feature = {"id": "empty", "sourceTile": "none", "sourceSha256": "b" * 64, "polygons": []}
    assert construction_grid_contract(
        feature,
        building_facades=[],
        building_ground_footprints=[],
    ) is None


def test_conversion_embeds_one_versioned_grid_in_worldedit_roof_and_whole_breakable_wall_cells():
    data = fixture(width=4, depth=3, angle=0)
    converted = convert_building(data["feature"])

    grid = converted["constructionGrid"]
    assert grid["schemaVersion"] == CONSTRUCTION_GRID_VERSION
    assert converted["groundFootprints"]
    assert converted["facadeSegments"]
    assert converted["wallCells"]
    assert all(len(cell) == 3 and all(isinstance(value, int) for value in cell)
               for cell in converted["wallCells"])
    for roof in converted["roofs"]:
        assert roof["objectTypeId"] == "building_roof"
        assert roof["metadata"]["familyRef"] == "world-edit.roof"
        assert roof["metadata"]["constructionGridVersion"] == CONSTRUCTION_GRID_VERSION
        source = roof["metadata"]["roofParameters"]["importedSource"]
        assert source["constructionGrid"] == grid
        assert source["groundFootprints"]
        assert source["facadeSegments"]
