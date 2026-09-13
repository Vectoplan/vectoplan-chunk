from datetime import datetime
import pytest
from routes.chunks import _make_json_safe as chunk_json_safe
from routes.commands import _make_json_safe as command_json_safe
from models.chunk import make_json_safe as snapshot_json_safe
from models.event import make_json_safe as event_json_safe


@pytest.mark.parametrize('_make_json_safe', [chunk_json_safe, command_json_safe, snapshot_json_safe, event_json_safe])
def test_large_scalar_arrays_keep_values_without_aliasing_source_containers(_make_json_safe):
    source = {'cells': [0, 1, 44, None, False, 1.25] * 4096, 'metadata': {'name': 'Bäume'}}
    result = _make_json_safe(source)
    assert result == source
    assert result is not source and result['cells'] is not source['cells']
    assert result['metadata'] is not source['metadata']


@pytest.mark.parametrize('_make_json_safe', [chunk_json_safe, command_json_safe])
def test_nested_depth_cycles_dates_and_shared_references_are_still_normalized(_make_json_safe):
    ring = [1]
    ring.append(ring)
    shared = {'valid': True}
    assert _make_json_safe({'cycle': ring, 'a': shared, 'b': shared, 'date': datetime(2026, 9, 5)}) == {
        'cycle': [1, '<recursive-reference>'], 'a': shared, 'b': shared, 'date': '2026-09-05T00:00:00'}
    assert _make_json_safe({'items': [1, None, {'deep': 'value'}]}, max_depth=1) == {
        'items': ['<max-depth-exceeded>'] * 3}
