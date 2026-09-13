from copy import deepcopy

from src.world.earth.terrain_snapshot_upgrade import protected_legacy_cells, merge_untouched_terrain, verify_unchanged_revision_gaps


def test_complete_command_history_protects_removal_and_placement_cells():
    logs = [{'id':'a','count':1,'cells':[{'x':0,'y':0,'z':0}]},
            {'id':'b','count':1,'cells':[{'x':1,'y':0,'z':0}]}]
    assert protected_legacy_cells(revision=2,last_command_id='b',chunk=(0,0,0),size=2,logs=logs) == frozenset({0,1})
    assert protected_legacy_cells(revision=3,last_command_id='b',chunk=(0,0,0),size=2,logs=logs) is None
    assert protected_legacy_cells(revision=2,last_command_id='missing',chunk=(0,0,0),size=2,logs=logs) is None
    assert protected_legacy_cells(revision=1,last_command_id='a',chunk=(0,0,0),size=2,
                                  logs=[{'id':'a','count':2,'cells':[{'x':0,'y':0,'z':0}]}]) is None


def test_legacy_upgrade_preserves_edits_objects_and_original_snapshot():
    original = {'chunkSize':2,'chunkX':0,'chunkY':0,'chunkZ':0,'cells':[0,1,2,0,1,1,0,0],
                'palette':[{'blockTypeId':'system_terrain_humus'},{'blockTypeId':'user-brick'}],
                'objectRefs':[{'objectInstanceId':'kept','occupiedCells':[{'x':1,'y':1,'z':0}]}]}
    before = deepcopy(original)
    generated = {'cells':[1]*8,'palette':['system_terrain_humus'],'stats':{},'contentHash':'new-terrain',
                 'terrain':{'releaseKey':'approved-berlin'},'metadata':{'terrainSurface':{
                     'schemaVersion':'terrain-cut-cells.v1','cornerHeights':[.5]*9}}}
    result = merge_untouched_terrain(original,generated,frozenset({0,1}))
    assert result['cells'][0] == 0  # explicitly removed air
    assert result['cells'][1] == 1  # edited terrain remains a full block
    assert result['cells'][2] == 2  # library material remains unchanged
    assert result['cells'][3] == 0  # object-owned air remains unchanged
    assert result['cells'][6] == 1  # untouched old baseline follows the DGM
    assert result['objectRefs'] == original['objectRefs']
    assert result['metadata']['terrainSurface']['fullCellIndices'] == [0,1,3]
    assert original == before


def test_detail_refresh_preserves_previously_placed_full_cells_and_updates_terrain_stats():
    old={'chunkSize':2,'cells':[1]*8,'palette':['system_terrain_humus'],
         'metadata':{'terrainSurface':{'schemaVersion':'terrain-cut-cells.v1','sampleStepM':32,
                     'cornerHeights':[1]*9,'fullCellIndices':[4]}},
         'stats':{'terrain':{'status':'fallback-flat'}}}
    detail={'cells':[0]*8,'palette':['system_terrain_humus'],'terrain':{'sampleStepM':1,'status':'dgm'},
            'metadata':{'terrainSurface':{'schemaVersion':'terrain-cut-cells.v1','sampleStepM':1,'cornerHeights':[-2]*9}}}
    updated=merge_untouched_terrain(old,detail,frozenset())
    assert updated['cells'][4]==1
    assert sum(updated['cells'])==1
    assert updated['metadata']['terrainSurface']['sampleStepM']==1
    assert updated['stats']['terrain']==detail['terrain']


def test_metadata_only_revision_gaps_require_matching_persisted_content_hashes():
    events=[{'id':'a','before':None,'after':1,'beforeHash':None,'afterHash':'content-a'},
            {'id':'b','before':1,'after':2,'beforeHash':'content-a','afterHash':'content-b'}]
    assert verify_unchanged_revision_gaps(revision=3,content_hash='content-b',command_ids={'a','b'},events=events)
    assert not verify_unchanged_revision_gaps(revision=3,content_hash='unlogged-edit',command_ids={'a','b'},events=events)
    later={'id':'c','before':3,'after':4,'beforeHash':'content-b','afterHash':'content-c'}
    assert verify_unchanged_revision_gaps(revision=4,content_hash='content-c',command_ids={'a','b','c'},events=events+[later])
    assert not verify_unchanged_revision_gaps(revision=4,content_hash='content-c',command_ids={'a','b','c'},events=events+[{**later,'beforeHash':'unlogged-edit'}])
    assert not verify_unchanged_revision_gaps(revision=4,content_hash='content-c',command_ids={'b','c'},events=events[1:]+[later])
