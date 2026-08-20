"""Serializable contracts for the immutable Earth world coordinate frame."""

from __future__ import annotations

from typing import Any, Optional


EARTH_GRID_FRAME_SCHEMA_VERSION = "vectoplan-earth-grid-frame.v1"


def earth_grid_frame_contract(provider: Any) -> Optional[dict[str, Any]]:
    """Return the exact frame shared by Chunk, Core, CAD and the editor.

    The project marker is mutable and therefore cannot be used as an origin for
    already persisted chunk cells.  The provider frame is chunk-aligned and is
    the only authoritative conversion between WGS84 and local x/z cells.
    """

    frame = getattr(provider, "frame", None)
    definition = getattr(provider, "grid_definition", None)
    storage_origin = getattr(frame, "storage_origin", None)
    if frame is None or definition is None or storage_origin is None:
        return None
    try:
        return {
            "schemaVersion": EARTH_GRID_FRAME_SCHEMA_VERSION,
            "horizontalMapping": "vectoplan-periodic-equirectangular",
            "mappingVersion": "1",
            "axisConvention": "x-east-y-up-z-north",
            "worldWidthCells": int(definition.world_width_cells),
            "worldHeightCells": int(definition.world_height_cells),
            "metersPerCell": float(definition.meters_per_cell),
            "centralMeridianDegrees": float(definition.central_meridian_deg),
            "storageOrigin": {
                "x": int(storage_origin.x),
                "y": int(storage_origin.y),
                "z": int(storage_origin.z),
            },
        }
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
