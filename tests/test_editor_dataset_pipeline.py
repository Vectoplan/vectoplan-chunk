from __future__ import annotations

import json
import copy

import pytest

from src.editor_dataset import build_editor_dataset, write_editor_dataset
from src.editor_dataset.contracts import content_fingerprint


def context():
    roof = {
        "type": "PlaceObject",
        "objectTypeId": "building_roof",
        "objectInstanceId": "roof-building-1",
        "position": {"x": 2, "y": 4, "z": 2},
        "dimensions": {"x": 16, "y": 20, "z": 16},
        "footprint": {
            "type": "Polygon",
            "coordinateSpace": "world-cell-xz",
            "coordinates": [[[2, 2], [18, 2], [18, 18], [2, 18], [2, 2]]],
            "baseY": 4,
            "height": 20,
        },
        "metadata": {
            "familyRef": "world-edit.roof",
            "voxelOccupancy": "none",
        },
    }
    grid = {
        "schemaVersion": "vectoplan-lod2-construction-grid.v1",
        "buildingId": "building-1",
        "referenceMode": "lod2-existing-building",
        "origin": [0, 0],
        "axisU": [1, 0],
        "axisV": [0, 1],
        "widthM": 20,
        "depthM": 20,
        "uAnchors": [0, 20],
        "vAnchors": [0, 20],
        "facades": [],
        "partitionPolicy": {"algorithm": "anchored-axis-lines.v1"},
        "provenance": {"conversionVersion": "lod2-editable-buildings.v4"},
        "fingerprint": "grid-1",
    }
    return {
        "datasetId": "editor:project:world",
        "referenceFingerprint": "earth-frame-1",
        "coordinateFrame": {"schemaVersion": "vectoplan-earth-grid-frame.v1"},
        "chunkSize": 16,
        "lod2Plan": {
            "referenceFingerprint": "earth-frame-1",
            "bounds": [-32, -32, 32, 32],
            "buildings": [{
                "buildingId": "building-1",
                "sourceTile": "LoD2_391_5820.zip",
                "sourceSha256": "a" * 64,
                "wallCells": [[0, 0, 0], [15, 0, 0], [16, 0, 0]],
                "roofs": [roof],
                "constructionGrid": grid,
            }],
        },
        "roadFeatures": [
            {"featureId": "road-1", "centerline": [[-1, 2], [18, 2]]},
            {"featureId": "road-duplicate", "centerline": [[18, 2], [-1, 2]]},
        ],
    }


def test_pipeline_is_deterministic_and_packs_worldedit_artifacts_by_chunk():
    first = build_editor_dataset(context())
    second = build_editor_dataset(context())

    assert first == second
    assert first["schemaVersion"] == "vectoplan-editor-dataset.v1"
    assert [item["processId"] for item in first["processes"]] == [
        "lod2-editable", "parcel-grid", "road-network", "chunk-pack",
    ]
    assert len(first["layers"]["editableBuildings"]["items"]) == 1
    assert len(first["layers"]["streetNetwork"]["items"]) == 1
    assert first["layers"]["parcelGrids"]["items"][0]["alignmentMode"] == "lod2-existing-building"
    chunks = {item["chunkKey"]: item for item in first["chunks"]}
    assert {"0:0:0", "1:0:0", "-1:0:0"}.issubset(chunks)
    assert any(item["objectInstanceId"] == "roof-building-1" for item in chunks["0:0:0"]["roofObjectRefs"])
    assert any(item["objectInstanceId"] == "roof-building-1" for item in chunks["1:1:1"]["roofObjectRefs"])
    assert all(item["blockTypeId"] == "lod2_exterior_wall" and item["breakable"]
               for chunk in chunks.values() for item in chunk["wallBlocks"])
    assert all(item["contentFingerprint"] for item in chunks.values())


def test_bundle_has_one_auditable_directory_per_process_and_refuses_overwrite(tmp_path):
    dataset = build_editor_dataset(context())
    target = write_editor_dataset(dataset, tmp_path / "project-dataset")

    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["contentFingerprint"] == dataset["contentFingerprint"]
    for process_id in ("lod2-editable", "parcel-grid", "road-network", "chunk-pack"):
        process_dir = target / "processes" / process_id
        assert (process_dir / "receipt.json").is_file()
        assert (process_dir / "output.json").is_file()
    chunk_output = json.loads((target / "processes" / "chunk-pack" / "output.json").read_text(encoding="utf-8"))
    chunk_receipt = json.loads((target / "processes" / "chunk-pack" / "receipt.json").read_text(encoding="utf-8"))
    assert content_fingerprint(chunk_output) == chunk_receipt["outputFingerprint"]
    assert list((target / "chunks").glob("*.json"))
    with pytest.raises(FileExistsError):
        write_editor_dataset(dataset, target)


@pytest.mark.parametrize("patch", [
    {"datasetId": ""},
    {"referenceFingerprint": ""},
    {"chunkSize": 0},
    {"chunkSize": 257},
])
def test_contract_rejects_incomplete_or_unbounded_context(patch):
    value = {**context(), **patch}
    with pytest.raises(ValueError):
        build_editor_dataset(value)


def test_road_output_is_geometry_stable_across_source_order_and_direction():
    first_context = context()
    second_context = context()
    second_context["roadFeatures"] = list(reversed(second_context["roadFeatures"]))
    for road in second_context["roadFeatures"]:
        road["centerline"] = list(reversed(road["centerline"]))

    first = build_editor_dataset(first_context)
    second = build_editor_dataset(second_context)

    assert first["layers"]["streetNetwork"] == second["layers"]["streetNetwork"]
    first_segments = {
        item["segmentId"]
        for chunk in first["chunks"]
        for item in chunk["streetSegments"]
    }
    second_segments = {
        item["segmentId"]
        for chunk in second["chunks"]
        for item in chunk["streetSegments"]
    }
    assert first_segments == second_segments


@pytest.mark.parametrize(("center_z", "expected_width"), [
    (2.0, 4.0),
    (1.0, 2.0),
])
def test_road_width_is_nominally_six_metres_and_clamped_to_parcel_boundaries(center_z, expected_width):
    value = context()
    value["roadFeatures"] = [{"centerline": [[-1, center_z], [18, center_z]], "nominalWidthM": 14}]
    value["roadSurfaceBoundaries"] = [
        [[-32, 0], [32, 0]],
        [[-32, 4], [32, 4]],
    ]

    dataset = build_editor_dataset(value)
    road = dataset["layers"]["streetNetwork"]["items"][0]

    assert dataset["layers"]["streetNetwork"]["schemaVersion"] == "vectoplan-editor-street-network.v2"
    assert road["nominalWidthM"] == 6.0
    assert road["effectiveWidthM"] == pytest.approx(expected_width)
    assert road["segmentWidthsM"] == pytest.approx([expected_width])
    assert road["widthPolicy"] == "nominal-6m-clamped-to-road-parcel-surface.v1"
    packed = [
        segment
        for chunk in dataset["chunks"]
        for segment in chunk["streetSegments"]
        if segment["featureId"] == road["featureId"]
    ]
    assert packed
    assert all(segment["nominalWidthM"] == 6.0 for segment in packed)
    assert all(segment["effectiveWidthM"] == pytest.approx(expected_width) for segment in packed)


def test_explicit_available_road_surface_width_is_preserved_as_a_hard_upper_bound():
    value = context()
    value["roadFeatures"] = [{
        "centerline": [[-1, 2], [18, 2]],
        "availableWidthM": 3.25,
    }]

    road = build_editor_dataset(value)["layers"]["streetNetwork"]["items"][0]

    assert road["nominalWidthM"] == 6.0
    assert road["availableWidthM"] == pytest.approx(3.25)
    assert road["effectiveWidthM"] == pytest.approx(3.25)


def test_spatial_contract_rejects_roads_outside_declared_window():
    value = context()
    value["roadFeatures"] = [{"centerline": [[0, 0], [1000, 0]]}]
    with pytest.raises(ValueError, match="sourceBounds"):
        build_editor_dataset(value)


def test_spatial_contract_rejects_road_width_boundaries_outside_declared_window():
    value = context()
    value["roadSurfaceBoundaries"] = [[[0, 0], [1000, 0]]]
    with pytest.raises(ValueError, match="roadSurfaceBoundaries.*sourceBounds"):
        build_editor_dataset(value)


def test_writer_rejects_tampered_bundle_before_creating_target(tmp_path):
    dataset = build_editor_dataset(context())
    dataset["layers"]["editableBuildings"]["items"][0]["buildingId"] = "tampered"
    target = tmp_path / "tampered"

    with pytest.raises(ValueError, match="contentFingerprint"):
        write_editor_dataset(dataset, target)
    assert not target.exists()


def test_writer_rejects_path_like_chunk_key_even_with_recomputed_fingerprints(tmp_path):
    dataset = copy.deepcopy(build_editor_dataset(context()))
    chunk = dataset["chunks"][0]
    chunk["chunkKey"] = "../../escape"
    chunk_source = dict(chunk)
    chunk_source.pop("contentFingerprint")
    chunk["contentFingerprint"] = content_fingerprint(chunk_source)
    chunk_output = {
        "schemaVersion": "vectoplan-editor-chunk-artifacts.v1",
        "itemCount": len(dataset["chunks"]),
        "items": dataset["chunks"],
    }
    next(item for item in dataset["processes"] if item["processId"] == "chunk-pack")["outputFingerprint"] = content_fingerprint(chunk_output)
    dataset_source = dict(dataset)
    dataset_source.pop("contentFingerprint")
    dataset["contentFingerprint"] = content_fingerprint(dataset_source)

    with pytest.raises(ValueError, match="chunkKey"):
        write_editor_dataset(dataset, tmp_path / "invalid-key")


def test_real_conversion_payload_is_accepted_when_geometry_dependencies_exist():
    pytest.importorskip("shapely")
    from src.geodata.lod2_conversion import convert_building

    ring = [[0, 0], [8.4, 0], [8.4, 5.2], [0, 5.2], [0, 0]]
    polygons = [{"surface": "GroundSurface", "rings": [[[x, 0, z] for x, z in ring]]}]
    for start, end in zip(ring, ring[1:]):
        polygons.append({"surface": "WallSurface", "rings": [[
            [start[0], 0, start[1]], [end[0], 0, end[1]],
            [end[0], 6, end[1]], [start[0], 6, start[1]],
            [start[0], 0, start[1]],
        ]]})
    polygons.append({"surface": "RoofSurface", "rings": [[[x, 6, z] for x, z in ring]]})
    converted = convert_building({
        "id": "real-conversion",
        "sourceTile": "LoD2_fixture.zip",
        "sourceSha256": "b" * 64,
        "polygons": polygons,
    })
    value = context()
    value["lod2Plan"]["buildings"] = [converted]

    dataset = build_editor_dataset(value)

    building = dataset["layers"]["editableBuildings"]["items"][0]
    assert building["worldEditRoofs"][0]["metadata"]["familyRef"] == "world-edit.roof"
    assert building["constructionGrid"]["schemaVersion"] == "vectoplan-lod2-construction-grid.v1"
