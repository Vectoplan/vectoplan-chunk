from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.editor_dataset import build_editor_dataset, write_editor_dataset
from src.editor_dataset.runtime import (
    EditorDatasetRuntimeError,
    activate_editor_dataset,
    attach_active_editor_dataset,
    clear_editor_dataset_runtime_cache,
    editor_dataset_lod2_plan,
    load_active_editor_dataset,
    load_editor_dataset_bundle,
    load_editor_dataset_chunk,
    selector_path,
)


def _context():
    roof = {
        "type": "PlaceObject",
        "objectTypeId": "building_roof",
        "objectInstanceId": "roof-building-1",
        "position": {"x": 2, "y": 4, "z": 2},
        "blockTypeId": "lod2_exterior_wall",
        "objectKind": "semantic_footprint",
        "dimensions": {"x": 16, "y": 20, "z": 16},
        "occupiedCells": [{"x": 2, "y": 4, "z": 2}],
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
            "roofParameters": {
                "importedSource": {
                    "constructionGrid": {
                        "schemaVersion": "vectoplan-lod2-construction-grid.v1",
                        "buildingId": "building-1",
                    }
                }
            },
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
        "datasetId": "editor:prj_public:world_spawn",
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
        "roadFeatures": [{"centerline": [[-1, 2], [18, 2]], "nominalWidthM": 7}],
    }


def _bundle(root):
    target = write_editor_dataset(build_editor_dataset(_context()), root / "test1-berlin")
    activate_editor_dataset(
        target,
        project_id="chk_project",
        external_project_id="prj_public",
        world_id="world_spawn",
        root=root,
    )
    return target


def _world(*, materialized=False):
    metadata = {}
    if materialized:
        metadata = {"lod2Buildings": {"materializedBuildings": {"building-1": {"version": "v4"}}}}
    return SimpleNamespace(
        world_id="world_spawn",
        chunk_size=16,
        metadata_json=metadata,
        build_earth_provider=lambda: SimpleNamespace(reference_fingerprint="earth-frame-1"),
    )


def _project():
    return SimpleNamespace(project_id="chk_project", external_app_project_id="prj_public")


def test_active_bundle_projects_roofs_and_roads_through_existing_chunk_contracts(tmp_path):
    _bundle(tmp_path)
    active = load_active_editor_dataset(
        "chk_project", "world_spawn", external_project_id="prj_public", root=tmp_path
    )
    assert active is not None
    assert active.public_summary()["chunkCount"] > 0

    chunk = {
        "chunkX": 0,
        "chunkY": 0,
        "chunkZ": 0,
        "objectRefs": [{"objectInstanceId": "user-object", "metadata": {"source": "canonical"}}],
        "metadata": {"geodataOverlays": {
            "schemaVersion": "geodata-overlays.v1",
            "items": [
                {"id": "parcel-boundaries", "semanticRole": "parcel-boundaries"},
                {"id": "live-road", "semanticRole": "street-network"},
            ],
            "availability": [{"id": "street-network", "status": "available"}],
            "visualLayerResolution": {"schemaVersion": "stale-test-value"},
        }},
    }
    assert attach_active_editor_dataset(
        chunk, project=_project(), world=_world(), root=tmp_path
    ) is True

    refs = {item["objectInstanceId"]: item for item in chunk["objectRefs"]}
    assert refs["user-object"]["metadata"]["source"] == "canonical"
    roof = refs["roof-building-1"]
    assert roof["metadata"]["familyRef"] == "world-edit.roof"
    assert (
        roof["metadata"]["roofParameters"]["importedSource"]["constructionGrid"]["buildingId"]
        == "building-1"
    )
    overlays = chunk["metadata"]["geodataOverlays"]["items"]
    assert any(item.get("semanticRole") == "parcel-boundaries" for item in overlays)
    streets = [item for item in overlays if item.get("semanticRole") == "street-network"]
    assert len(streets) == 1
    assert streets[0]["source"]["kind"] == "vectoplan-editor-dataset"
    assert streets[0]["renderMode"] == "surface-ribbons"
    assert streets[0]["style"]["surfaceWidth"] == 6.0
    assert streets[0]["style"]["color"] == "#fbfcfd"
    assert streets[0]["style"]["opacity"] == 1.0
    assert streets[0]["geometry"]["surfaceWidths"] == [6.0]
    assert streets[0]["stats"]["nominalWidthM"] == 6.0
    assert (
        chunk["metadata"]["geodataOverlays"]["visualLayerResolution"]["schemaVersion"]
        == "geodata-visual-layer-resolution.v1"
    )
    projection = chunk["metadata"]["editorDataset"]
    assert projection["wallBlocksAvailable"] > 0
    assert projection["wallBlocksProjection"] == "canonical-worldedit-import-only"
    assert projection["parcelGridRefs"]


def test_materialized_building_never_reappears_from_bundle_after_canonical_delete(tmp_path):
    _bundle(tmp_path)
    chunk = {"chunkX": 0, "chunkY": 0, "chunkZ": 0, "objectRefs": [], "metadata": {}}

    attach_active_editor_dataset(
        chunk,
        project=_project(),
        world=_world(materialized=True),
        root=tmp_path,
    )

    assert not any(item.get("objectInstanceId") == "roof-building-1" for item in chunk["objectRefs"])
    assert chunk["metadata"]["editorDataset"]["roofObjectRefsAdded"] == 0


@pytest.mark.parametrize("local_source", [True, False])
def test_single_deleted_roof_is_not_resurrected_by_immutable_bundle(tmp_path, local_source):
    _bundle(tmp_path)
    world = _world()
    chunk = {"chunkX": 0, "chunkY": 0, "chunkZ": 0, "objectRefs": [], "metadata": {}}
    if local_source:
        chunk["objectRefs"] = [{"objectInstanceId": "preserved-facade", "objectTypeId": "building_facade_source",
            "metadata": {"lod2FacadeSource": {"deletedRoofObjectInstanceId": "roof-building-1"}}}]
    else:
        world.metadata_json = {"lod2Buildings": {"removedRoofObjectIds": {"roof-building-1": {"buildingId": "building-1"}}}}
    attach_active_editor_dataset(chunk, project=_project(), world=world, root=tmp_path)
    assert not any(item.get("objectInstanceId") == "roof-building-1" for item in chunk["objectRefs"])
    assert chunk["metadata"]["editorDataset"]["roofObjectRefsAdded"] == 0


def test_street_surface_ribbon_has_only_the_y_zero_owner(tmp_path):
    _bundle(tmp_path)
    chunk = {"chunkX": 0, "chunkY": 1, "chunkZ": 0, "objectRefs": [], "metadata": {}}

    assert attach_active_editor_dataset(
        chunk, project=_project(), world=_world(), root=tmp_path
    ) is True

    assert "geodataOverlays" not in chunk["metadata"]
    assert chunk["metadata"]["editorDataset"]["streetSegmentCount"] == 0


def test_inactive_dataset_is_a_noop_so_live_fallback_stays_untouched(tmp_path):
    chunk = {"chunkX": 0, "chunkY": 0, "chunkZ": 0, "metadata": {"live": True}}
    original = json.loads(json.dumps(chunk))

    assert attach_active_editor_dataset(
        chunk, project=_project(), world=_world(), root=tmp_path
    ) is False
    assert chunk == original


def test_activation_rejects_bundle_outside_persistent_root(tmp_path):
    root = tmp_path / "root"
    outside = write_editor_dataset(build_editor_dataset(_context()), tmp_path / "outside")

    with pytest.raises(EditorDatasetRuntimeError, match="inside the persistent"):
        activate_editor_dataset(
            outside,
            project_id="chk_project",
            external_project_id="prj_public",
            world_id="world_spawn",
            root=root,
        )


def test_selector_cannot_escape_root_and_chunk_tampering_is_detected(tmp_path):
    target = _bundle(tmp_path)
    active_selector = selector_path("chk_project", "world_spawn", root=tmp_path)
    selector = json.loads(active_selector.read_text(encoding="utf-8"))
    selector["datasetPath"] = "../../outside"
    active_selector.write_text(json.dumps(selector), encoding="utf-8")
    clear_editor_dataset_runtime_cache()
    with pytest.raises(EditorDatasetRuntimeError) as traversal:
        load_active_editor_dataset(
            "chk_project", "world_spawn", external_project_id="prj_public", root=tmp_path
        )
    assert traversal.value.code == "editor_dataset_unsafe_path"

    activate_editor_dataset(
        target,
        project_id="chk_project",
        external_project_id="prj_public",
        world_id="world_spawn",
        root=tmp_path,
    )
    active = load_active_editor_dataset(
        "chk_project", "world_spawn", external_project_id="prj_public", root=tmp_path
    )
    chunk_path = target / "chunks" / "0_0_0.json"
    packed = json.loads(chunk_path.read_text(encoding="utf-8"))
    packed["wallBlocks"] = []
    chunk_path.write_text(json.dumps(packed), encoding="utf-8")
    clear_editor_dataset_runtime_cache()
    with pytest.raises(EditorDatasetRuntimeError) as tampered:
        load_editor_dataset_chunk(active, "0:0:0")
    assert tampered.value.code == "editor_dataset_invalid_chunk_fingerprint"


def test_selector_rejects_dataset_from_another_project(tmp_path):
    target = write_editor_dataset(build_editor_dataset(_context()), tmp_path / "bundle")
    with pytest.raises(EditorDatasetRuntimeError) as error:
        activate_editor_dataset(
            target,
            project_id="other_project",
            world_id="world_spawn",
            root=tmp_path,
        )
    assert error.value.code == "editor_dataset_project_mismatch"


def test_validated_bundle_reconstructs_self_contained_canonical_lod2_plan(tmp_path):
    target = write_editor_dataset(build_editor_dataset(_context()), tmp_path / "bundle")
    active = load_editor_dataset_bundle(
        target,
        project_id="chk_project",
        external_project_id="prj_public",
        world_id="world_spawn",
        root=tmp_path,
    )

    plan = editor_dataset_lod2_plan(active)

    assert plan["referenceFingerprint"] == "earth-frame-1"
    assert plan["heightReference"]["kind"] == "dataset-aligned-world-cells"
    assert plan["heightReference"]["datasetFingerprint"] == active.content_fingerprint
    assert plan["candidateWallCells"] == 3
    building = plan["buildings"][0]
    assert building["wallCells"] == [[0, 0, 0], [15, 0, 0], [16, 0, 0]]
    assert building["roofs"][0]["metadata"]["familyRef"] == "world-edit.roof"
    assert building["constructionGrid"]["schemaVersion"] == "vectoplan-lod2-construction-grid.v1"
    assert plan["metadataRepairs"][0]["buildingId"] == "building-1"
