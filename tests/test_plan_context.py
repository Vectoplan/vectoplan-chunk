from types import SimpleNamespace
from src.geodata.plan_context import compact_object


def test_plan_background_does_not_issue_additional_parcel_queries():
    from unittest.mock import Mock, patch
    from src.geodata.plan_context import _context_overlays, _overlay_cache
    _overlay_cache.clear()
    service = Mock()
    service.chunk_contract.return_value = {'items': []}
    with patch('src.geodata.visual_overlays.get_default_geodata_overlay_service', return_value=service), \
         patch('src.geodata.tree_instances.append_tree_overlay'):
        _context_overlays(SimpleNamespace(id=987, metadata_json={}), SimpleNamespace(reference_fingerprint='bounded'))
    assert service.chunk_contract.call_count == 4
    assert all('parcel-boundaries' in call.kwargs['exclude_overlay_ids'] for call in service.chunk_contract.call_args_list)
    _overlay_cache.clear()


def test_context_uses_parent_geometry_without_expanding_construction_cells():
    ring = [[[0, 0], [10, 0], [10, 10]]]
    row = SimpleNamespace(object_instance_id="parent", object_type_id="planning_build_area",
                          revision=3, footprint_json={"type": "Polygon", "coordinates": ring},
                          metadata_json={"label": "Building", "storeyCount": 10,
                                         "constructionCells": [{"x": 0}] * 10000,
                                         "roofCalculation": {"large": True}})
    value = compact_object(row)
    assert value["footprint"]["coordinates"] == ring
    assert value["metadata"] == {"label": "Building", "storeyCount": 10}
    assert value["revision"] == 3


def test_context_preserves_building_grid_datum_without_roof_mesh_payload():
    grid = {"schemaVersion": "vectoplan-lod2-construction-grid.v1", "stepU": 1.8,
            "origin": [123, -45], "axisU": [.8, .6], "fingerprint": "validated"}
    row = SimpleNamespace(object_instance_id="roof", object_type_id="building_roof", revision=1,
                          footprint_json={}, metadata_json={"lod2BuildingId": "building", "roofParameters": {
                              "importedSource": {"constructionGrid": grid, "groundFootprints": [[[0, 0]]],
                                                 "roofSurfaces": [{"huge": True}]}}})
    result = compact_object(row)
    assert result["gridSource"]["constructionGrid"] == grid
    assert "roofSurfaces" not in result["gridSource"]
    assert result["metadata"]["lod2BuildingId"] == "building"
