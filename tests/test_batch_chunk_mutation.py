from types import SimpleNamespace
import pytest

from src.batch_chunk_mutation import batch_chunk_mutations, cached_chunk, remember_chunk, chunk_refs, remember_ref


def test_chunks_are_reused_only_within_their_batch_and_world():
    content = {'chunkX': 2, 'chunkY': 0, 'chunkZ': -1, 'cells': [0]}
    snapshot = SimpleNamespace(snapshot_id='snapshot', chunk_revision=5, chunk_version='5', content_hash='hash')
    with batch_chunk_mutations():
        remember_chunk('world', snapshot, content)
        loaded, runtime = cached_chunk('world', 2, 0, -1)
        assert loaded is snapshot and runtime['cells'] is content['cells']
        assert runtime['chunkRevision'] == 5 and runtime['source'] == 'snapshot'
        assert cached_chunk('another-world', 2, 0, -1) is None
        with batch_chunk_mutations():
            assert cached_chunk('world', 2, 0, -1) is None
        assert cached_chunk('world', 2, 0, -1)[0] is snapshot
    assert cached_chunk('world', 2, 0, -1) is None


def test_exception_discards_mutated_inputs_before_retry():
    with pytest.raises(ValueError):
        with batch_chunk_mutations():
            remember_chunk('world', None, {'chunkX': 0, 'chunkY': 0, 'chunkZ': 0, 'cells': [8]})
            raise ValueError('roll back generation')
    assert cached_chunk('world', 0, 0, 0) is None


def test_chunk_ref_cache_includes_children_created_by_later_storeys():
    calls = []
    first = SimpleNamespace(world_db_id=1, chunk_x=0, chunk_y=0, chunk_z=0, occupied_cells_json=[1])
    def load():
        calls.append(True)
        return [first]
    with batch_chunk_mutations():
        assert chunk_refs(1, (0, 0, 0), load) == [first]
        second = SimpleNamespace(world_db_id=1, chunk_x=0, chunk_y=0, chunk_z=0, occupied_cells_json=[2])
        remember_ref(second)
        remember_ref(second)
        assert chunk_refs(1, (0, 0, 0), load) == [first, second]
        first.occupied_cells_json = []
        assert chunk_refs(1, (0, 0, 0), load)[0].occupied_cells_json == []
        assert len(calls) == 1
    assert chunk_refs(1, (0, 0, 0), load) == [first]
    assert len(calls) == 2
