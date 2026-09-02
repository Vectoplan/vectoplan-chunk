"""Fail-closed visual geodata layer selection for serialized Earth chunks.

The resolver publishes status and provenance only.  In particular, the locked
photorealistic candidate never contains an asset URL and cannot initiate a
download.  A future, separately approved asset pipeline can add ready items to
the existing overlay contract without changing the fallback semantics here.
"""
from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any


SCHEMA_VERSION = "geodata-visual-layer-resolution.v1"
POLICY = "photorealistic-lod3-lod2.v1"
LAYER_ORDER = ("photorealistic", "lod3", "lod2")
PRIORITIES = {"photorealistic": 300, "lod3": 200, "lod2": 100}
DATASET_IDS = {
    "photorealistic": "3d-reality-mesh",
    "lod3": "3d-gebaeudedaten",
    "lod2": "3d-gebaeudedaten",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _enabled(value: Any) -> bool:
    return _text(value).lower() in {"1", "true", "yes", "on"}


def visual_layer_kind(item: Any) -> str | None:
    """Classify an overlay item without interpreting or fetching its assets."""
    value = _mapping(item)
    explicit = _text(value.get("visualLayerKind")).lower()
    if explicit in LAYER_ORDER:
        return explicit
    render_mode = _text(value.get("renderMode")).lower()
    dataset_id = _text(value.get("datasetId")).lower()
    if dataset_id == "3d-reality-mesh" or render_mode in {
        "photorealistic-mesh", "textured-mesh", "textured-mesh-tile", "3d-tiles",
    }:
        return "photorealistic"
    source = _mapping(value.get("source"))
    try:
        lod = int(source.get("lod"))
    except (TypeError, ValueError):
        lod = None
    if lod == 3:
        return "lod3"
    if lod == 2 or (
        dataset_id == "3d-gebaeudedaten" and render_mode == "building-meshes"
    ):
        return "lod2"
    return None


def _item_id(item: Mapping[str, Any], index: int) -> str:
    return _text(item.get("id")) or f"{_text(item.get('datasetId')) or 'overlay'}:{index}"


def _item_provenance(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    source = _mapping(item.get("source"))
    result: dict[str, Any] = {
        "itemId": _item_id(item, index),
        "datasetId": _text(item.get("datasetId")),
    }
    for key in ("releaseKey", "tileKey"):
        if _text(item.get(key)):
            result[key] = _text(item.get(key))
    for key in ("sourceId", "kind", "lod", "license"):
        if source.get(key) not in (None, ""):
            result[key] = source.get(key)
    return result


def _source_availability(contract: Mapping[str, Any], kind: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    values = contract.get("availability")
    if not isinstance(values, list):
        return result
    for raw in values:
        value = _mapping(raw)
        entry_kind = _text(value.get("kind")).lower()
        if entry_kind != kind:
            continue
        result.append({
            "sourceId": _text(value.get("id")),
            "status": _text(value.get("status")) or "unavailable",
        })
    return result


def build_visual_layer_resolution(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deterministic visual-layer decision for one overlay contract."""
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = {
        kind: [] for kind in LAYER_ORDER
    }
    items = contract.get("items")
    if isinstance(items, list):
        for index, raw in enumerate(items):
            item = _mapping(raw)
            kind = visual_layer_kind(item)
            if kind is not None:
                grouped[kind].append((index, item))

    license_state = _text(os.getenv(
        "VECTOPLAN_CHUNK_PHOTOREALISTIC_LICENSE_STATE", "license_required",
    )).lower().replace("-", "_")
    if license_state not in {"license_required", "approved", "denied"}:
        license_state = "license_required"
    configured_photo_enabled = _enabled(os.getenv(
        "VECTOPLAN_CHUNK_PHOTOREALISTIC_ENABLED", "false",
    ))
    photo_enabled = configured_photo_enabled and license_state == "approved"

    layers: list[dict[str, Any]] = []
    for kind in LAYER_ORDER:
        candidates = grouped[kind]
        item_ids = [_item_id(item, index) for index, item in candidates]
        provenance: dict[str, Any] = {
            "contractOwner": "vectoplan-chunk",
            "items": [_item_provenance(item, index) for index, item in candidates],
        }
        if kind == "photorealistic":
            provenance.update({
                "provider": "Berlin Partner fuer Wirtschaft und Technologie GmbH",
                "sourceDataset": "3D-Meshmodell Berlin 2025",
                "licenseState": license_state,
                "retrieval": "blocked_until_explicit_license_approval",
            })
            enabled = photo_enabled
            status = (
                "license_required" if license_state == "license_required"
                else "denied" if license_state == "denied"
                else "disabled" if not configured_photo_enabled
                else "ready" if candidates
                else "unavailable"
            )
        else:
            enabled = True
            status = "ready" if candidates else "unavailable"
            provenance["sourceAvailability"] = _source_availability(contract, kind)
        layers.append({
            "kind": kind,
            "datasetId": DATASET_IDS[kind],
            "priority": PRIORITIES[kind],
            "enabled": enabled,
            "status": status,
            "itemIds": item_ids,
            "provenance": provenance,
        })

    selected_layer = next(
        (layer for layer in layers if layer["enabled"] and layer["status"] == "ready"),
        None,
    )
    selected = None if selected_layer is None else {
        "kind": selected_layer["kind"],
        "datasetId": selected_layer["datasetId"],
        "itemIds": list(selected_layer["itemIds"]),
        "reason": "highest_ready_layer",
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "policy": POLICY,
        "order": list(LAYER_ORDER),
        "selected": selected,
        "fallbackUsed": bool(selected and selected["kind"] != "photorealistic"),
        "layers": layers,
    }


def attach_visual_layer_resolution(contract: dict[str, Any]) -> dict[str, Any]:
    contract["visualLayerResolution"] = build_visual_layer_resolution(contract)
    return contract


__all__ = [
    "LAYER_ORDER",
    "POLICY",
    "SCHEMA_VERSION",
    "attach_visual_layer_resolution",
    "build_visual_layer_resolution",
    "visual_layer_kind",
]
