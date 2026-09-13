from copy import deepcopy
import json
import pytest

from src.frozen_json import freeze_json, FrozenJsonDict, FrozenJsonList
from models.chunk import make_json_safe


def test_frozen_references_are_recursively_immutable_and_keep_standard_json():
    source = {'occupiedCells': [{'x': 2, 'y': 3, 'z': 4}], 'metadata': {'role': 'wall'}, 'size': 3.0}
    frozen = freeze_json(source)
    assert json.loads(json.dumps(frozen)) == source
    assert frozen == source and frozen is not source
    with pytest.raises(TypeError):
        frozen['metadata']['role'] = 'roof'
    with pytest.raises(TypeError):
        frozen['occupiedCells'][0]['x'] = 9
    with pytest.raises(TypeError):
        frozen['occupiedCells'].append({'x': 9})
    with pytest.raises(TypeError):
        frozen['occupiedCells'] += [5]
    assert deepcopy(frozen) is frozen
    assert make_json_safe({'objectRefs': [frozen]})['objectRefs'][0] is frozen
    source['occupiedCells'][0]['x'] = 44
    assert frozen['occupiedCells'][0]['x'] == 2
    with pytest.raises(TypeError):
        frozen.__init__({'mutable': True})
    with pytest.raises(TypeError):
        frozen['occupiedCells'].__init__([])


def test_direct_construction_freezes_children_and_preserves_revision_independence():
    source = {'metadata': {'cells': [1, 2]}}
    first = FrozenJsonDict(source)
    second = FrozenJsonList([source])
    source['metadata']['cells'][0] = 99
    assert first['metadata']['cells'] == [1, 2]
    assert second[0]['metadata']['cells'] == [1, 2]
    first_snapshot = make_json_safe({'cells': [1], 'objectRefs': [first]})
    next_ref = freeze_json({**first, 'metadata': {**first['metadata'], 'cells': [3, 4]}})
    next_snapshot = make_json_safe({'cells': [2], 'objectRefs': [next_ref]})
    assert first_snapshot['objectRefs'][0]['metadata']['cells'] == [1, 2]
    assert next_snapshot['objectRefs'][0]['metadata']['cells'] == [3, 4]


@pytest.mark.parametrize('value', [object(), {'value': {1, 2}}, {1: 'integer key'}, {'value': (1, 2)}])
def test_only_already_normalized_json_can_be_shared(value):
    with pytest.raises(TypeError):
        freeze_json(value)
