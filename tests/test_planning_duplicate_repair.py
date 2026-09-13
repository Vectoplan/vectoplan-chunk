from copy import deepcopy
import pytest
from src.planning_duplicate_repair import duplicate_parent_plan


def records():
    path = {'width': 8, 'points': [{'x': 1, 'y': 1, 'z': 2}, {'x': 3, 'y': 1, 'z': 4}]}
    footprint = {'type': 'Polygon', 'coordinates': [[[0, 0], [4, 0], [0, 4], [0, 0]]]}
    def parent(identity, log_id, owned):
        log = {'id': log_id, 'userId': 'editor_user', 'sessionId': f'world_edit_planning_generation_{log_id}',
               'pathBrush': path, 'footprint': footprint, 'generationId': identity + '-generation'}
        child = {'id': identity + '-floor', 'physical': True, 'groundFloor': True,
                 'createdCommand': str(log_id), 'updatedCommand': str(log_id),
                 'requestedCellHash': 'full', 'ownedCellHash': 'full' if owned else 'empty', 'ownedCells': owned}
        return {'id': identity, 'generationId': log['generationId'], 'pathBrush': path, 'footprint': footprint,
                'firstGenerationLog': log, 'latestGenerationLog': log, 'manifestIds': [child['id']], 'children': [child]}
    return parent('keep', 3, 10), parent('old', 1, 0)


def test_explicit_latest_complete_parent_preserved_without_merging_variants():
    keep, old = records()
    result = duplicate_parent_plan(keep, [old])
    assert result['keeper'] == keep
    assert result['removeObjectIds'] == ['old-floor', 'old']
    changed = deepcopy(old)
    changed['children'][0]['revision'] = 2
    assert duplicate_parent_plan(keep, [changed])['planSha256'] != result['planSha256']


def test_keeper_may_be_moved_after_duplicate_creation_but_is_preserved_exactly():
    keep, old = records()
    keep = deepcopy(keep)
    original = deepcopy(keep['firstGenerationLog'])
    keep['pathBrush']['points'][0]['x'] = 17
    keep['footprint']['coordinates'][0][0][0] = 17
    keep['firstGenerationLog'] = original
    result = duplicate_parent_plan(keep, [old])
    assert result['keeper']['pathBrush'] == keep['pathBrush']
    assert result['removeObjectIds'] == ['old-floor', 'old']


@pytest.mark.parametrize('change,reason', [
    ('shape', 'match exactly'), ('provenance', 'provenance'), ('owner', 'displaced ground'),
    ('manual', 'edited independently'), ('keep-incomplete', 'complete physical'),
    ('missing-child', 'obsolete generations'), ('newer', 'latest saved'),
])
def test_duplicate_parent_plan_rejects_unproven_or_changed_recovery(change, reason):
    keep, old = records()
    old = deepcopy(old)
    if change == 'shape':
        old['pathBrush']['width'] = 9
    elif change == 'provenance':
        old['latestGenerationLog']['userId'] = 'other-user'
    elif change == 'owner':
        old['children'][0]['ownedCells'] = 1
    elif change == 'manual':
        old['children'][0]['updatedCommand'] = 'manual-roof-edit'
    elif change == 'keep-incomplete':
        keep['children'][0]['ownedCellHash'] = 'partial'
    elif change == 'missing-child':
        old['manifestIds'].append('missing')
    elif change == 'newer':
        old['latestGenerationLog']['id'] = 4
    with pytest.raises(ValueError, match=reason):
        duplicate_parent_plan(keep, [old])
