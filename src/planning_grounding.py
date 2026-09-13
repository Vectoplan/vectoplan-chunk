"""Validate controller-generated grounding plans without transforming geometry."""
import math
from src.planning_duplicate_repair import fingerprint

KEEPERS = {'planning_build_area_mto4qfdv_gomn9e': 6, 'planning_build_area_mto6gn4a_c8e275': 4}


def grounding_cell_is_compatible(owner_id, block_type_id, own_ids, *, air):
    if owner_id:
        return owner_id in own_ids
    return air or str(block_type_id or '').startswith('system_terrain')


def protect_grounding_destination(*, project, universe, world, payload, own_ids):
    """Read the same runtime snapshot adapter as mutation; never save/materialize."""
    from routes import commands
    size = int(world.chunk_size)
    groups = {}
    for child in payload['commands']:
        if (child.get('metadata') or {}).get('voxelOccupancy') == 'none':
            continue
        for cell in child.get('occupiedCells') or []:
            x, y, z = cell['x'], cell['y'], cell['z']
            groups.setdefault((x//size,y//size,z//size), set()).add((x,y,z))
    for (cx,cy,cz), addresses in groups.items():
        _, content = commands._load_chunk_for_mutation(project=project, universe=universe, world=world,
            chunk_x=cx, chunk_y=cy, chunk_z=cz)
        # Runtime refs are in last-writer order. Build their ownership map once
        # per chunk; probing every prism against every ref repeatedly is costly.
        owners = {}
        for ref in content.get('objectRefs') or []:
            if not isinstance(ref, dict) or commands._object_ref_is_metadata_only(ref):
                continue
            for cell in ref.get('occupiedCells') or []:
                if not isinstance(cell, dict):
                    continue
                address = tuple(cell.get(axis, cell.get('world'+axis.upper(), origin*size+cell.get('local'+axis.upper(),0)))
                                for axis,origin in zip(('x','y','z'),(cx,cy,cz)))
                if address in addresses:
                    owners[address] = ref.get('objectInstanceId')
        for x,y,z in addresses:
            value = commands._get_cell_value(content,local_x=x%size,local_y=y%size,local_z=z%size,chunk_size=size)
            block = commands._block_type_id_from_cell_value(content,value)
            owner = owners.get((x,y,z))
            if not grounding_cell_is_compatible(owner,block,own_ids,air=value==commands.AIR_CELL_VALUE):
                raise ValueError(f'Grounding would replace another object or user material at ({x},{y},{z}): {owner or block}.')


def validate_grounding_plan(record, source):
    payload = record['payload']
    identity = source['objectInstanceId']
    if identity not in KEEPERS or record.get('sourceSha256') != fingerprint(source):
        raise ValueError('The source parent changed after planning; regenerate the grounding plan.')
    descriptor = payload.get('planningBuildingEdit') or {}
    if (payload.get('type') != 'ObjectBatch' or not payload.get('commandId')
            or descriptor.get('parentObjectInstanceId') != identity
            or descriptor.get('previousGenerationId') != source['metadata'].get('generationId')):
        raise ValueError('The original command identity and previous building generation are required.')
    parents = [child for child in payload['commands'] if child.get('objectTypeId') == 'planning_build_area']
    if len(parents) != 1 or parents[0].get('objectInstanceId') != identity:
        raise ValueError('Exactly the expected keeper parent must be placed.')
    parent = parents[0]
    metadata, original = parent['metadata'], source['metadata']
    terrain = record.get('terrain') or {}
    baseline = terrain.get('baselineY')
    if (terrain.get('policy') != 'min-exact-footprint-triangle-surface.v1'
            or not isinstance(baseline, (float, int)) or not math.isfinite(baseline)
            or not terrain.get('releaseKeys') or baseline > terrain['minimumY'] + 1e-7
            or terrain['minimumY'] - baseline >= .001001
            or metadata.get('baseY') != baseline or parent['footprint'].get('baseY') != baseline
            or parent['position']['y'] != math.floor(baseline)):
        raise ValueError('The plan must use the exact approved footprint terrain minimum.')
    if metadata.get('storeyCount') != original.get('storeyCount') or original['storeyCount'] != KEEPERS[identity]:
        raise ValueError('The saved keeper storey count must remain unchanged.')
    if metadata.get('wallBlockTypeId') != original.get('wallBlockTypeId'):
        raise ValueError('The saved exterior wall material must remain unchanged.')
    roof, old_roof = metadata['buildingProgram']['roof'], original['buildingProgram']['roof']
    if any(roof.get(key) != old_roof.get(key) for key in ('type', 'pitchDegrees', 'overhangMillimeters')):
        raise ValueError('The saved roof shape, pitch and overhang must remain unchanged.')
    path, old_path = metadata['pathBrush'], original['pathBrush']
    if ({key: value for key, value in path.items() if key != 'points'}
            != {key: value for key, value in old_path.items() if key != 'points'}
            or [(p['x'], p['z']) for p in path['points']] != [(p['x'], p['z']) for p in old_path['points']]
            or any(p['y'] != baseline for p in path['points'])
            or parent['footprint'].get('type') != source['footprint'].get('type')
            or parent['footprint'].get('coordinates') != source['footprint'].get('coordinates')):
        raise ValueError('The complete saved ground contour must remain unchanged.')
    old_profile = original.get('storeyProfile') or {}
    new_profile = metadata['storeyProfile']
    if (new_profile.get('segmentAdjustments') or {}) != (old_profile.get('segmentAdjustments') or {}):
        raise ValueError('The saved wing storey adjustments must remain unchanged.')
    old_scopes = (old_profile.get('heightProfile') or {}).get('boundariesByScope') or {
        'all': [i * original['storeyHeightMeters'] for i in range(original['storeyCount'] + 1)]}
    new_scopes = new_profile['heightProfile']['boundariesByScope']
    for scope, values in old_scopes.items():
        current = new_scopes.get(scope) or []
        if len(values) != len(current) or any(abs(a-b) > 1e-6 for a,b in zip(values, current)):
            raise ValueError('The saved individual storey heights must remain unchanged.')
    minimum = math.inf
    owners = set()
    for child in payload['commands']:
        if child['type'] != 'PlaceObject':
            raise ValueError('All retirement must be derived by the atomic generation executor.')
        if child is parent:
            continue
        info = child.get('metadata') or {}
        if info.get('generatedFromAreaId') != identity:
            raise ValueError('A generated child belongs to another parent.')
        if info.get('voxelOccupancy') == 'none':
            continue
        geometry = info.get('constructionCells') or []
        cells = child.get('occupiedCells') or []
        addresses = {(cell['x'], cell['y'], cell['z']) for cell in cells}
        if (len(addresses) != len(cells) or owners.intersection(addresses)
                or any(any(not isinstance(value, int) for value in address) for address in addresses)
                or addresses != {(cell['x'], cell['y'], cell['z']) for cell in geometry}):
            raise ValueError('The regenerated prisms must have complete, unique integer cell ownership.')
        owners.update(addresses)
        for cell in geometry:
            if cell['minimumY'] < baseline - 1e-6 or cell['maximumY'] <= cell['minimumY']:
                raise ValueError('Invalid regenerated construction prism height.')
            minimum = min(minimum, cell['minimumY'])
    if abs(minimum - baseline) > 1e-6:
        raise ValueError('The bottom of the regenerated building does not meet the terrain datum.')
    return payload
