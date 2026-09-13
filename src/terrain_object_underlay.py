"""Restore proven terrain covered by generated buildings, never old buildings.

This journal records the actual pre-placement cell. It deliberately does not
sample the terrain generator: air can be a user's excavation.
"""
from collections.abc import Mapping


def _terrain_id(value):
    return isinstance(value, str) and (value == "system_terrain" or value.startswith("system_terrain_"))


def terrain_underlay(cell):
    journal = cell.get("terrainUnderlay")
    if isinstance(journal, Mapping) and _terrain_id(journal.get("blockTypeId")):
        return dict(journal)
    # Existing occupied refs already retain the original material, but did not
    # record whether it was an explicitly placed cube. Do not guess that flag.
    if _terrain_id(cell.get("beforeBlockTypeId")):
        return {"blockTypeId": cell["beforeBlockTypeId"]}
    return None


def capture_terrain_underlay(before_block_type_id, *, full_cell, previous_cell=None):
    if _terrain_id(before_block_type_id):
        return {"blockTypeId": before_block_type_id,
                **({"fullCell": full_cell} if isinstance(full_cell, bool) else {})}
    if isinstance(previous_cell, Mapping) and before_block_type_id == previous_cell.get("afterBlockTypeId"):
        # Same-object geometry updates must not replace its original underlay
        # with the wall/slab that the previous version itself wrote.
        return terrain_underlay(previous_cell)
    return None


def terrain_restore_value(content, cell, *, generated_building):
    if not generated_building:
        return 0, None
    underlay = terrain_underlay(cell)
    if not underlay:
        return 0, None
    identifier = underlay["blockTypeId"]
    for index, entry in enumerate(content.get("palette") or []):
        if isinstance(entry, Mapping) and identifier == (
                entry.get("blockTypeId") or entry.get("block_type_id") or entry.get("typeId") or entry.get("type_id")):
            return index + 1, underlay
    # No palette/provenance synthesis when a legacy record is incomplete.
    return 0, None


def restore_terrain_cut_flag(content, index, underlay):
    if not underlay or underlay.get("fullCell") is not False:
        return
    metadata = content.get("metadata")
    shape = metadata.get("terrainSurface") if isinstance(metadata, Mapping) else None
    if isinstance(shape, Mapping):
        full = set(shape.get("fullCellIndices") or [])
        full.discard(index)
        content["metadata"] = {**metadata, "terrainSurface": {**shape, "fullCellIndices": sorted(full)}}
