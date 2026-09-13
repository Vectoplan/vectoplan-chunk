"""Evidence checks for explicitly selected historical duplicate building parents."""
from hashlib import sha256
import json


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def duplicate_parent_plan(keeper, retired):
    """Never discover/deduplicate by shape alone or combine storey variants.

    The operator explicitly chooses identities. The plan requires identical
    complete drafts, applied editor generation provenance and displaced ground
    floors; the sole intact, most recently saved version survives unchanged.
    """
    all_parents = [keeper, *retired]
    ids = [parent['id'] for parent in all_parents]
    if not retired or len(set(ids)) != len(ids):
        raise ValueError('Explicit distinct keeper and retired parent identities are required.')
    first = keeper['firstGenerationLog']
    shape = {'pathBrush': first['pathBrush'], 'footprint': first['footprint']}
    if not shape['pathBrush'] or not shape['footprint']:
        raise ValueError('A complete persisted draft and footprint are required.')
    keep_latest = keeper['latestGenerationLog']['id']
    user = keeper['latestGenerationLog']['userId']
    for parent in all_parents:
        origin = parent['firstGenerationLog']
        if {'pathBrush': origin['pathBrush'], 'footprint': origin['footprint']} != shape:
            raise ValueError('Complete original pathBrush and footprint must match exactly.')
        latest = parent['latestGenerationLog']
        current_shape = {'pathBrush': parent['pathBrush'], 'footprint': parent['footprint']}
        if {'pathBrush': latest['pathBrush'], 'footprint': latest['footprint']} != current_shape:
            raise ValueError('Current geometry does not match its recorded generation.')
        if parent is not keeper and current_shape != shape:
            raise ValueError('A displaced parent was subsequently reshaped; review it separately.')
        for log in (parent['firstGenerationLog'], parent['latestGenerationLog']):
            if (not user or log['userId'] != user
                    or not str(log['sessionId']).startswith('world_edit_planning_generation_')):
                raise ValueError('Applied editor generation provenance does not match this draft.')
        if parent['latestGenerationLog']['generationId'] != parent['generationId']:
            raise ValueError('The current parent generation has no authoritative source command.')
        if set(parent['manifestIds']) != {child['id'] for child in parent['children']}:
            raise ValueError('Retire same-parent obsolete generations before duplicate-parent recovery.')
        for child in parent['children']:
            if child['updatedCommand'] and child['updatedCommand'] != child['createdCommand']:
                raise ValueError('A generated object was edited independently; preserve it for separate review.')
    if any(child['requestedCellHash'] != child['ownedCellHash']
           for child in keeper['children'] if child['physical']):
        raise ValueError('The keeper must retain its complete physical generation.')
    for parent in retired:
        if parent['latestGenerationLog']['id'] >= keep_latest:
            raise ValueError('The keeper must be the latest saved generation of the selected draft.')
        ground = [child for child in parent['children'] if child['groundFloor']]
        if not ground or any(child['ownedCells'] != 0 for child in ground):
            raise ValueError('A retired parent must have a completely displaced ground floor.')
    plan = {'policy': 'explicit-identities-identical-draft-displaced-ground.v1',
            'shapeSha256': fingerprint(shape), 'keeper': keeper, 'retired': retired,
            'removeObjectIds': [identity for parent in retired
                                for identity in [*(child['id'] for child in parent['children']), parent['id']]]}
    return {**plan, 'planSha256': fingerprint(plan)}
