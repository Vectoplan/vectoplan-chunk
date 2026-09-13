from types import SimpleNamespace
import pytest
from routes.chunks import _compact_placement_semantics
from src.placement_projection_payloads import SEMANTIC_PAYLOAD_FIELDS, _payload


def projected_payload(value):
    row = {"payload_present": bool(value) if isinstance(value, dict) else False}
    row.update({"projection_" + key: value.get(key) if isinstance(value, dict) else None
                for key in SEMANTIC_PAYLOAD_FIELDS})
    return _payload(SimpleNamespace(**row))


@pytest.mark.parametrize("event", [
    {}, {"irrelevantAuditRecord": {"affectedCells": [1, 2, 3]}},
    {"metadata": {"irrelevant": True}},
    {"metadata": {"familyId": "wall", "storeyId": "floor-2", "storeyHeightMm": 3000}},
    {"libraryPlacementContext": {"familyId": "beam", "variantId": "300"}, "runtimeBlockTypeId": "beam"},
    {"metadata": {"roofParameters": {"roof_type": "gable", "points_mm": [[0, 0], [1, 1]]},
                  "roofCalculation": {"roof_type": "gable", "faces": [{"points": [[0, 1, 2]]}]}}},
    None, [], "bad-legacy-payload",
])
def test_compact_database_fields_preserve_full_payload_semantics(event):
    command = {"metadata": {"familyId": "fallback", "source": "vectoplan-cad",
                             "semanticProfile": {"variables": {"height_mm": 3000}}},
               "runtimeBlockTypeId": "fallback-block", "source": "editor",
               "affectedCells": [{"large": "audit-data"}] * 1000}
    kw = {"block_type_id": "stone", "object_type_id": "building_wall", "object_variant_id": "default"}
    expected = _compact_placement_semantics(event, command, **kw)
    actual = _compact_placement_semantics(projected_payload(event), projected_payload(command), **kw)
    assert actual == expected
    assert "affectedCells" not in projected_payload(command)
