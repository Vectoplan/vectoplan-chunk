"""Actual Berlin controller exports: preserve design while rebuilding vertical routing."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.planning_grounding import grounding_cell_is_compatible, protect_grounding_destination, validate_grounding_plan


def examples():
    return json.loads(gzip.decompress((Path(__file__).parent/'fixtures/berlin-grounding-plans.json.gz').read_bytes()))


@pytest.mark.parametrize('owner,block,air,allowed', [
    ('own-wall','lod2_exterior_wall',False,True), (None,None,True,True),
    (None,'system_terrain_humus',False,True), ('other-wall','lod2_exterior_wall',False,False),
    ('other-wall',None,True,False), (None,'manually_placed_marble',False,False),
])
def test_grounding_preserves_foreign_objects_and_manual_material(owner,block,air,allowed):
    assert grounding_cell_is_compatible(owner,block,{'own-wall'},air=air) is allowed


def test_destination_guard_reads_current_chunk_owner_before_any_generation(monkeypatch):
    from routes import commands
    content = {'cells': [1] + [0]*4095, 'palette': [{'blockTypeId':'system_terrain_humus'}],
               'objectRefs': [{'objectInstanceId':'foreign', 'occupiedCells':[{'x':-16,'y':0,'z':0}], 'metadata':{}}]}
    reads = []
    def load(**kwargs):
        reads.append((kwargs['chunk_x'],kwargs['chunk_y'],kwargs['chunk_z']))
        return None, content
    monkeypatch.setattr(commands,'_load_chunk_for_mutation',load)
    payload = {'commands':[{'metadata':{},'occupiedCells':[{'x':-16,'y':0,'z':0}]}]}
    kwargs = dict(project=None,universe=None,world=SimpleNamespace(chunk_size=16),payload=payload,own_ids={'own'})
    with pytest.raises(ValueError,match='another object'):
        protect_grounding_destination(**kwargs)
    assert reads == [(-1,0,0)]
    content['objectRefs'][0]['objectInstanceId']='own'
    protect_grounding_destination(**kwargs)
    content['objectRefs']=[]
    protect_grounding_destination(**kwargs)
    content['palette']=[{'blockTypeId':'user_marble'}]
    with pytest.raises(ValueError,match='user material'):
        protect_grounding_destination(**kwargs)


@pytest.mark.parametrize('index,baseline,count', [(0,1.03,6),(1,1.01,4)])
def test_actual_controller_grounding_exports_preserve_design_with_complete_cell_ownership(index,baseline,count):
    entry = examples()[index]
    payload = validate_grounding_plan(entry['record'],entry['source'])
    parent = next(child for child in payload['commands'] if child.get('objectTypeId') == 'planning_build_area')
    assert parent['metadata']['baseY'] == baseline
    assert parent['metadata']['storeyCount'] == count
    assert payload is entry['record']['payload']


@pytest.mark.parametrize('change,reason', [
    ('stale-parent','source parent changed'),('generation','previous building generation'),
    ('roof','roof shape'),('count','storey count'),('shape','ground contour'),
    ('height','individual storey'),('owner','unique integer'),('datum','terrain minimum'),
])
def test_grounding_rejects_mutated_or_stale_real_plans(change,reason):
    entry = examples()[0]
    record, source = entry['record'], entry['source']
    parent = next(child for child in record['payload']['commands'] if child.get('objectTypeId') == 'planning_build_area')
    if change == 'stale-parent':
        source['metadata']['revision'] = 999
    elif change == 'generation':
        record['payload']['planningBuildingEdit']['previousGenerationId'] = 'stale'
    elif change == 'roof':
        parent['metadata']['buildingProgram']['roof']['pitchDegrees'] = 40
    elif change == 'count':
        parent['metadata']['storeyCount'] = 5
    elif change == 'shape':
        parent['metadata']['pathBrush']['points'][0]['x'] += 1
    elif change == 'height':
        parent['metadata']['storeyProfile']['heightProfile']['boundariesByScope']['all'][1] += .1
    elif change == 'owner':
        child = next(child for child in record['payload']['commands'] if child.get('metadata',{}).get('constructionCells'))
        child['occupiedCells'].append(deepcopy(child['occupiedCells'][0]))
    elif change == 'datum':
        record['terrain']['minimumY'] -= .1
    with pytest.raises(ValueError,match=reason):
        validate_grounding_plan(record, source)
