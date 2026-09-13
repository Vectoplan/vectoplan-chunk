"""Linear-time ownership lookups for an immutable chunk mutation input.

Only the coordinates are indexed. Cell records, last-writer precedence and
the first matching record within each object retain their existing semantics.
Indexes are discarded before object references change, never cached globally.
"""
from collections.abc import Mapping


def _integer(mapping, *keys):
    for key in keys:
        if key in mapping:
            try:
                return int(mapping[key])
            except (TypeError, ValueError):
                pass
    return None


def _coordinates(candidate, prefix):
    if prefix == 'world':
        return tuple(_integer(candidate, axis, f'world{axis.upper()}', f'world_{axis}') for axis in 'xyz')
    return tuple(_integer(candidate, f'{prefix}{axis.upper()}', f'{prefix}_{axis}') for axis in 'xyz')


def coordinate_key(candidate, *, chunk, chunk_size):
    """Normalize current world cells and legacy chunk/local-only records."""
    if not isinstance(candidate, Mapping):
        return None
    world = _coordinates(candidate, 'world')
    if all(value is not None for value in world):
        return world
    local = _coordinates(candidate, 'local')
    if any(value is None for value in local):
        return None
    candidate_chunk = _coordinates(candidate, 'chunk')
    if all(value is not None for value in candidate_chunk) and candidate_chunk != chunk:
        return None
    return tuple(origin * chunk_size + value for origin, value in zip(chunk, local))


def filter_detached_cells(occupied_cells, *, target_cells, chunk):
    targets = {(int(cell['worldX']), int(cell['worldY']), int(cell['worldZ'])) for cell in target_cells}
    local_targets = {(int(cell['localX']), int(cell['localY']), int(cell['localZ'])) for cell in target_cells}
    original = occupied_cells if isinstance(occupied_cells, list) else []
    def matches(candidate):
        if not isinstance(candidate, Mapping):
            return False
        world = _coordinates(candidate, 'world')
        if all(value is not None for value in world):
            return world in targets
        local = _coordinates(candidate, 'local')
        if any(value is None for value in local):
            return False
        candidate_chunk = _coordinates(candidate, 'chunk')
        return local in local_targets and (any(value is None for value in candidate_chunk) or candidate_chunk == chunk)
    retained = [cell for cell in original if not matches(cell)]
    return retained, len(original) - len(retained)


def runtime_owner_index(content, *, chunk, chunk_size, ownership_policy, include_metadata_object_instance_id=None):
    result = {}
    refs = content.get('objectRefs')
    for ref in reversed(refs if isinstance(refs, list) else []):
        if not isinstance(ref, Mapping):
            continue
        identity = str(ref.get('objectInstanceId') or '').strip()
        if not identity:
            continue
        metadata = ref.get('metadata') if isinstance(ref.get('metadata'), Mapping) else {}
        metadata_only = metadata.get('voxelOccupancy') == 'none' or ref.get('refRole') == 'metadata_only'
        if metadata_only and identity != include_metadata_object_instance_id:
            continue
        cells = ref.get('occupiedCells')
        for cell in cells if isinstance(cells, list) else []:
            key = coordinate_key(cell, chunk=chunk, chunk_size=chunk_size)
            if key is None or key in result:
                continue
            block_type = str(cell.get('ownedBlockTypeId') or cell.get('afterBlockTypeId')
                             or ref.get('fillBlockTypeId') or metadata.get('fillBlockTypeId') or '').strip()
            result[key] = {'objectInstanceId': identity, 'expectedBlockTypeId': block_type or None,
                           'ownershipPolicy': ownership_policy}
    return result
