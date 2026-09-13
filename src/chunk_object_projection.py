"""Chunk-local projections of construction objects; full objects stay in their table.

Older snapshots repeated every wall/slab prism of a building in each touched
chunk. Rendering and ownership have always been chunk-local. Filter before
recursive serialization, without modifying source metadata or restoring cells
which a user already removed.
"""
from collections.abc import Mapping
from math import isfinite
from src.frozen_json import FrozenJsonDict, freeze_json


def construction_refs_for_chunk(refs, *, chunk_x, chunk_y, chunk_z, chunk_size):
    coordinates = (chunk_x, chunk_y, chunk_z)
    from src.batch_chunk_mutation import cached_projection, remember_projection

    def local(cell):
        if not isinstance(cell, Mapping):
            return False
        values = [cell.get(axis, cell.get("world" + axis.upper())) for axis in ("x", "y", "z")]
        if not all(isinstance(value, (int, float)) and isfinite(value) and value == int(value) for value in values):
            return False
        return tuple(int(value) // chunk_size for value in values) == coordinates

    result = []
    for ref in refs if isinstance(refs, list) else []:
        metadata = ref.get("metadata") if isinstance(ref, Mapping) else None
        if not isinstance(metadata, Mapping) or metadata.get("renderProfile") != "construction-grid":
            result.append(ref)
            continue
        cached = cached_projection(ref, coordinates, chunk_size)
        if cached is not None:
            result.append(cached)
            continue
        cells = metadata.get("constructionCells")
        occupied = ref.get("occupiedCells")
        projected = {
            **ref,
            "occupiedCells": [cell for cell in occupied if local(cell)] if isinstance(occupied, list) else [],
            "metadata": {**metadata, "constructionCells": [cell for cell in cells if local(cell)]
                         if isinstance(cells, list) else []},
        }
        if isinstance(ref, FrozenJsonDict):
            projected = freeze_json(projected)
        remember_projection(ref, projected, coordinates, chunk_size)
        result.append(projected)
    return result


def project_chunk_construction_refs(content):
    result = dict(content)
    result["objectRefs"] = construction_refs_for_chunk(
        content.get("objectRefs"), chunk_x=int(content.get("chunkX") or 0),
        chunk_y=int(content.get("chunkY") or 0), chunk_z=int(content.get("chunkZ") or 0),
        chunk_size=max(1, int(content.get("chunkSize") or 16)),
    )
    return result
