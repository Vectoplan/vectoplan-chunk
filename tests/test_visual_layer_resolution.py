from __future__ import annotations

from src.geodata.visual_layer_resolution import (
    attach_visual_layer_resolution,
    build_visual_layer_resolution,
)


def _building_item(*, lod: int, item_id: str) -> dict:
    return {
        "id": item_id,
        "datasetId": "3d-gebaeudedaten",
        "renderMode": "building-meshes",
        "releaseKey": f"lod{lod}-release",
        "tileKey": "0:0",
        "source": {
            "kind": "citygml-derived-store",
            "sourceId": f"berlin-lod{lod}",
            "lod": lod,
            "license": "dl-de-zero-2.0",
        },
    }


def test_default_is_fail_closed_and_selects_lod2_without_mesh_url(monkeypatch):
    monkeypatch.delenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_ENABLED", raising=False)
    monkeypatch.delenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_LICENSE_STATE", raising=False)
    resolution = build_visual_layer_resolution({
        "items": [_building_item(lod=2, item_id="berlin-lod2")],
        "availability": [{"id": "berlin-lod2", "kind": "lod2", "status": "available"}],
    })

    assert resolution["order"] == ["photorealistic", "lod3", "lod2"]
    assert resolution["selected"] == {
        "kind": "lod2",
        "datasetId": "3d-gebaeudedaten",
        "itemIds": ["berlin-lod2"],
        "reason": "highest_ready_layer",
    }
    assert resolution["fallbackUsed"] is True
    photo, lod3, lod2 = resolution["layers"]
    assert photo["enabled"] is False
    assert photo["status"] == "license_required"
    assert photo["itemIds"] == []
    assert lod3["status"] == "unavailable"
    assert lod2["status"] == "ready"
    assert lod2["provenance"]["items"][0]["license"] == "dl-de-zero-2.0"
    assert "url" not in str(resolution).lower()


def test_priority_is_photorealistic_then_lod3_then_lod2_but_license_gate_wins(monkeypatch):
    photo = {
        "id": "photo-tile",
        "datasetId": "3d-reality-mesh",
        "renderMode": "textured-mesh-tile",
        "releaseKey": "photo-release",
        "tileKey": "tile-1",
    }
    contract = {"items": [photo, _building_item(lod=2, item_id="lod2"), _building_item(lod=3, item_id="lod3")]}

    monkeypatch.setenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_ENABLED", "true")
    monkeypatch.setenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_LICENSE_STATE", "license_required")
    locked = build_visual_layer_resolution(contract)
    assert locked["selected"]["kind"] == "lod3"
    assert locked["layers"][0]["status"] == "license_required"

    monkeypatch.setenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_LICENSE_STATE", "approved")
    approved = build_visual_layer_resolution(contract)
    assert approved["selected"]["kind"] == "photorealistic"
    assert approved["fallbackUsed"] is False


def test_approval_without_explicit_enable_remains_disabled(monkeypatch):
    monkeypatch.setenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_LICENSE_STATE", "approved")
    monkeypatch.delenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_ENABLED", raising=False)
    result = build_visual_layer_resolution({"items": []})
    assert result["layers"][0]["status"] == "disabled"
    assert result["layers"][0]["enabled"] is False
    assert result["selected"] is None


def test_resolution_is_attached_without_changing_overlay_items(monkeypatch):
    monkeypatch.delenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_ENABLED", raising=False)
    monkeypatch.delenv("VECTOPLAN_CHUNK_PHOTOREALISTIC_LICENSE_STATE", raising=False)
    item = _building_item(lod=2, item_id="lod2")
    contract = {"schemaVersion": "geodata-overlays.v1", "items": [item]}
    assert attach_visual_layer_resolution(contract) is contract
    assert contract["items"] == [item]
    assert contract["visualLayerResolution"]["selected"]["kind"] == "lod2"
