from types import SimpleNamespace

from routes.chunks import (
    _compact_placement_semantics,
    _event_authorship_kind,
    _event_placement_mutations,
    _is_user_authored_event,
)


def test_user_authored_event_filter_rejects_system_identities():
    assert _is_user_authored_event(None) is False
    assert _is_user_authored_event("") is False
    assert _is_user_authored_event("system") is False
    assert _is_user_authored_event("system_terrain") is False
    assert _is_user_authored_event("vectoplan-editor") is False


def test_user_authored_event_filter_accepts_editor_user():
    assert _is_user_authored_event("editor_user") is True
    assert _is_user_authored_event("usr_123") is True


def test_legacy_editor_set_block_is_a_user_placement():
    assert _is_user_authored_event(
        None,
        command_source="editor",
        command_type="SetBlock",
    ) is True
    assert _is_user_authored_event(
        None,
        command_source="system",
        command_type="SetBlock",
    ) is False


def test_lod2_import_is_available_only_for_explicit_projection_reads():
    assert _event_authorship_kind("system_lod2_import") == ""
    assert _event_authorship_kind(
        "system_lod2_import", include_system_structures=True
    ) == "system-lod2-structure"


def test_world_edit_event_expands_every_lod2_wall_cell_and_preserves_air_removals():
    row = SimpleNamespace(
        position_x=10,
        position_y=2,
        position_z=20,
        chunk_x=0,
        chunk_y=0,
        chunk_z=1,
        chunk_key="0:0:1",
        block_after_type_id=None,
        object_instance_id=None,
        object_footprint_json=None,
        affected_cells_json=[
            {
                "x": 10, "y": 2, "z": 20,
                "chunkX": 0, "chunkY": 0, "chunkZ": 1,
                "afterBlockTypeId": "lod2_exterior_wall",
            },
            {
                "x": 11, "y": 2, "z": 20,
                "chunkX": 0, "chunkY": 0, "chunkZ": 1,
                "afterBlockTypeId": None,
            },
        ],
    )

    mutations = _event_placement_mutations(row)

    assert [mutation["position"] for mutation in mutations] == [(10, 2, 20), (11, 2, 20)]
    assert [mutation["blockTypeId"] for mutation in mutations] == [
        "lod2_exterior_wall", None
    ]


def test_semantic_object_event_remains_one_logical_placement():
    row = SimpleNamespace(
        position_x=10,
        position_y=6,
        position_z=20,
        chunk_x=0,
        chunk_y=0,
        chunk_z=1,
        chunk_key="0:0:1",
        block_after_type_id="lod2_exterior_wall",
        object_instance_id="lod2_roof_1",
        object_footprint_json={"type": "Polygon"},
        affected_cells_json=[
            {"x": 10, "y": 6, "z": 20, "afterBlockTypeId": "lod2_exterior_wall"},
            {"x": 11, "y": 6, "z": 20, "afterBlockTypeId": "lod2_exterior_wall"},
        ],
    )

    mutations = _event_placement_mutations(row)

    assert len(mutations) == 1
    assert mutations[0]["position"] == (10, 6, 20)


def test_compact_placement_semantics_keeps_library_identity_and_real_dimensions():
    semantics = _compact_placement_semantics(
        {
            "blockTypeId": "wall_runtime",
            "metadata": {
                "source": "vectoplan-cad",
                "clientCommandId": "cad-wall-1",
                "storeyId": "upper_floor_2",
                "storeyName": "2. Obergeschoss",
                "storeyBaseY": 5,
                "storeyHeightMm": 2645,
                "placementPolicy": "above-supporting-surface",
                "libraryPlacementContext": {
                    "source": "library",
                    "libraryItemId": "7",
                    "familyId": "vp.hochbau.waende.wand_mauerwerk",
                    "variantId": "dicke_365_mm",
                    "libraryRef": {
                        "category": "waende",
                        "objectKind": "block",
                    },
                    "semanticProfile": {
                        "role": "wall",
                        "definitionValues": {
                            "dimensions.thickness_mm": 365,
                            "dimensions.height_mm": 1000,
                            "nested": {"must": "not leak"},
                        },
                    },
                }
            },
        }
    )

    assert semantics["runtimeBlockTypeId"] == "wall_runtime"
    assert semantics["library"]["libraryItemId"] == "7"
    assert semantics["library"]["variantId"] == "dicke_365_mm"
    assert semantics["classification"]["role"] == "wall"
    assert semantics["model"] == {
        "source": "vectoplan-cad",
        "clientCommandId": "cad-wall-1",
        "storeyId": "upper_floor_2",
        "storeyName": "2. Obergeschoss",
        "storeyBaseY": 5,
        "storeyHeightMm": 2645,
        "placementPolicy": "above-supporting-surface",
    }
    assert semantics["variables"]["dimensions.thickness_mm"] == 365
    assert "nested" not in semantics["variables"]
    assert _is_user_authored_event(
        None,
        command_source="editor",
        command_type="RemoveBlock",
    ) is False


def test_compact_placement_semantics_keeps_only_versioned_nested_roof_geometry():
    roof_request = {
        "contract_version": "cad-roof-calculation-request/0.1",
        "roof_type": "gable",
        "footprint": {"outer_ring_mm": [[0, 0], [8000, 0], [8000, 6000], [0, 6000]]},
    }
    roof_calculation = {
        "contract_version": "cad-roof-calculation-result/0.1",
        "ok": True,
        "roof_type": "gable",
        "geometry": {"faces": [{"vertices_mm": [[0, 0, 3500], [8000, 0, 3500], [4000, 3000, 5900]]}]},
        "structure": {"rafters": [{"start_mm": [0, 0, 3500], "end_mm": [4000, 3000, 5900]}]},
    }
    semantics = _compact_placement_semantics({
        "blockTypeId": "system_terrain",
        "metadata": {
            "libraryPlacementContext": {
                "familyId": "world-edit.roof",
                "variantId": "gable",
                "semanticProfile": {
                    "role": "roof",
                    "variables": {
                        "semantic.role": "roof",
                        "roof.request": roof_request,
                        "roof.calculation": roof_calculation,
                        "unrelated.nested": {"must": "not leak"},
                    },
                },
            },
        },
    })

    assert semantics["classification"]["role"] == "roof"
    assert semantics["variables"]["roof.request"] == roof_request
    assert semantics["variables"]["roof.calculation"] == roof_calculation
    assert "unrelated.nested" not in semantics["variables"]


def test_compact_placement_semantics_keeps_imported_lod2_roof_faces():
    calculation = {
        "contract_version": "cad-roof-calculation-result/0.1",
        "ok": True,
        "roof_type": "imported",
        "geometry": {
            "faces": [{
                "face_ref": "lod2-1",
                "polygon_3d_mm": [[0, 0, 6000], [8000, 0, 6000], [4000, 3000, 8200]],
            }],
        },
        "source": "lod2-original-surfaces",
    }
    parameters = {
        "roofType": "imported",
        "pitchDeg": 35,
        "importedSource": {"schemaVersion": "lod2-roof-source.v1"},
    }
    semantics = _compact_placement_semantics({
        "blockTypeId": "lod2_exterior_wall",
        "metadata": {
            "source": "vectoplan-chunk.lod2-import",
            "familyRef": "world-edit.roof",
            "variantRef": "imported",
            "roofType": "imported",
            "roofParameters": parameters,
            "roofCalculation": calculation,
        },
    }, object_type_id="building_roof", object_variant_id="imported")

    assert semantics["variables"]["roof.type"] == "imported"
    assert semantics["variables"]["roof.request"] == parameters
    assert semantics["variables"]["roof.calculation"] == calculation
