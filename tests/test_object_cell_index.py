from copy import deepcopy
from random import Random

from src.object_cell_index import filter_detached_cells, runtime_owner_index


def _legacy_match(candidate, target, chunk):
    def integer(*keys):
        for key in keys:
            if key in candidate:
                try:
                    return int(candidate[key])
                except (TypeError, ValueError):
                    pass
        return None
    if not isinstance(candidate, dict):
        return False
    world = tuple(integer(a, 'world' + a.upper(), 'world_' + a) for a in 'xyz')
    if all(v is not None for v in world):
        return world == tuple(target['world' + a.upper()] for a in 'xyz')
    local = tuple(integer('local' + a.upper(), 'local_' + a) for a in 'xyz')
    if any(v is None for v in local):
        return False
    candidate_chunk = tuple(integer('chunk' + a.upper(), 'chunk_' + a) for a in 'xyz')
    return local == tuple(target['local' + a.upper()] for a in 'xyz') and (
        any(v is None for v in candidate_chunk) or candidate_chunk == chunk)


def test_detachment_matches_legacy_coordinate_contract_and_preserves_records():
    random = Random(20260906)
    for chunk_size in (8, 16, 32):
        for chunk in ((-2, 0, 3), (0, -1, -4)):
            targets = []
            records = [None, 'legacy', {}, {'x': 'invalid'}, {'local_x': 1}]
            for _ in range(100):
                local = tuple(random.randrange(chunk_size) for _ in 'xyz')
                world = tuple(c * chunk_size + v for c, v in zip(chunk, local))
                target = {**dict(zip(('worldX', 'worldY', 'worldZ'), world)),
                          **dict(zip(('localX', 'localY', 'localZ'), local))}
                if random.randrange(2):
                    targets.append(target)
                records.extend([
                    dict(zip('xyz', world)), dict(zip(('worldX', 'worldY', 'worldZ'), world)),
                    dict(zip(('localX', 'localY', 'localZ'), local)),
                    {**dict(zip(('local_x', 'local_y', 'local_z'), local)),
                     **dict(zip(('chunk_x', 'chunk_y', 'chunk_z'), chunk))},
                    {**dict(zip(('localX', 'localY', 'localZ'), local)), 'chunkX': 999, 'chunkY': 0, 'chunkZ': 0},
                    {**dict(zip('xyz', world)), 'localX': 999, 'localY': 999, 'localZ': 999},
                ])
            before = deepcopy(records)
            expected = [cell for cell in records if not any(_legacy_match(cell, target, chunk) for target in targets)]
            actual, count = filter_detached_cells(records, target_cells=targets, chunk=chunk)
            assert actual == expected
            assert count == len(records) - len(expected)
            assert records == before
            assert all(any(cell is original for original in records) for cell in actual)


def test_owner_index_preserves_last_writer_first_cell_and_metadata_exception():
    position = {'x': -31, 'y': 2, 'z': 51}
    local = {'localX': 1, 'localY': 2, 'localZ': 3}
    refs = [
        {'objectInstanceId': 'old', 'fillBlockTypeId': 'old-block', 'occupiedCells': [position]},
        {'objectInstanceId': 'new', 'occupiedCells': [
            {**local, 'ownedBlockTypeId': 'first-block'}, {**position, 'ownedBlockTypeId': 'second-block'}]},
        {'objectInstanceId': 'metadata', 'metadata': {'voxelOccupancy': 'none'},
         'fillBlockTypeId': 'metadata-block', 'occupiedCells': [position]},
        {'objectInstanceId': 'wrong-chunk', 'occupiedCells': [{**local, 'chunkX': 0, 'chunkY': 0, 'chunkZ': 0}]},
    ]
    content = {'objectRefs': refs}
    original = deepcopy(content)
    args = {'chunk': (-2, 0, 3), 'chunk_size': 16, 'ownership_policy': 'last-writer-wins-v1'}
    assert runtime_owner_index(content, **args) == {(-31, 2, 51): {
        'objectInstanceId': 'new', 'expectedBlockTypeId': 'first-block', 'ownershipPolicy': 'last-writer-wins-v1'}}
    assert runtime_owner_index(content, **args, include_metadata_object_instance_id='metadata')[(-31, 2, 51)]['objectInstanceId'] == 'metadata'
    assert content == original


def test_coordinate_access_is_linear_for_nonoverlapping_storeys():
    class CountedCell(dict):
        reads = 0
        def __contains__(self, key):
            type(self).reads += 1
            return super().__contains__(key)
    existing = [CountedCell(x=x, y=1, z=0) for x in range(1024)]
    targets = [{'worldX': x, 'worldY': 4, 'worldZ': 0, 'localX': x, 'localY': 4, 'localZ': 0} for x in range(1024)]
    retained, count = filter_detached_cells(existing, target_cells=targets, chunk=(0, 0, 0))
    assert retained == existing and count == 0
    assert CountedCell.reads <= len(existing) * 3
