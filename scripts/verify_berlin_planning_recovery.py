"""Read-only PostgreSQL verification. No Flask/WSGI import or startup hooks."""
import json
import math
import os
import psycopg
from psycopg.rows import dict_row

EXPECTED = {
    'planning_build_area_mto4qfdv_gomn9e': (6, 1.03, 'gable'),
    'planning_build_area_mto6gn4a_c8e275': (4, 1.01, 'hipped'),
}
RETIRED = {
    'planning_build_area_mto3xfkl_jf48j1',
    'planning_build_area_mto41zqa_ff92np',
    'planning_build_area_mto6h8ig_fnkcrm',
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def cells(values, chunk=None, size=16):
    result = set()
    for cell in values or []:
        coordinates = []
        for axis, origin in zip(('x','y','z'), chunk or (0,0,0)):
            value = cell.get(axis, cell.get('world'+axis.upper()))
            if value is None and chunk is not None:
                value = origin*size + cell.get('local'+axis.upper(),cell.get('local_'+axis))
            require(isinstance(value,(int,float)) and math.isfinite(value) and int(value)==value,
                    'Invalid occupied-cell address')
            coordinates.append(int(value))
        result.add(tuple(coordinates))
    return result


def main():
    uri = next((os.environ[key] for key in ('VECTOPLAN_CHUNK_DATABASE_URL',
        'VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI','DATABASE_URL','SQLALCHEMY_DATABASE_URI') if os.environ.get(key)), None)
    require(uri, 'Database URL is not configured')
    uri = uri.replace('postgresql+psycopg://','postgresql://').replace('postgresql+psycopg2://','postgresql://')
    connection = psycopg.connect(uri, row_factory=dict_row)
    try:
        cursor = connection.cursor()
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cursor.execute("""SELECT w.id,w.chunk_size FROM world_instances w JOIN projects p ON p.id=w.project_db_id
            WHERE p.project_id=%s AND w.world_id='world_spawn' AND w.deleted_at IS NULL""",
            ('chk_prj_prj_da09805bc6e54b29816c8cd6_6931567e1657',))
        worlds = cursor.fetchall()
        require(len(worlds)==1,'Expected exactly one target world')
        world,size = worlds[0]['id'],worlds[0]['chunk_size']
        cursor.execute("""SELECT id,object_instance_id,metadata_json,anchor_y,deleted_at FROM world_object_instances
            WHERE world_db_id=%s AND object_type_id='planning_build_area'""",(world,))
        parents = cursor.fetchall()
        all_live = {row['object_instance_id']:row for row in parents if row['deleted_at'] is None}
        # A new draft can legitimately be created while recovery is running.
        # Only the five explicitly reviewed historical identities are targets.
        targets=set(EXPECTED)|RETIRED
        live = {identity:row for identity,row in all_live.items() if identity in targets}
        require(set(live)==set(EXPECTED),'Expected exactly the two target keeper parents to remain live')
        deleted = {row['object_instance_id'] for row in parents if row['deleted_at'] is not None}
        require(RETIRED<=deleted,'An old duplicate parent is not deleted')
        cursor.execute("""SELECT id,object_instance_id,object_type_id,metadata_json,occupied_cells_json,
            primary_chunk_x,primary_chunk_y,primary_chunk_z
            FROM world_object_instances WHERE world_db_id=%s AND deleted_at IS NULL
            AND metadata_json->>'generatedFromAreaId'=ANY(%s)""",(world,list(set(EXPECTED)|RETIRED)))
        children = cursor.fetchall()
        require(all(row['metadata_json']['generatedFromAreaId'] in EXPECTED for row in children),
                'Live children of a deleted duplicate parent remain')
        child_by_id = {row['object_instance_id']:row for row in children}
        physical = {}
        summary = []
        for identity,(count,base,roof_type) in EXPECTED.items():
            metadata = live[identity]['metadata_json']
            require(metadata['storeyCount']==count,f'{identity}: wrong storey count')
            require(abs(metadata.get('baseY',math.inf)-base)<1e-7,f'{identity}: wrong baseY')
            require(live[identity]['anchor_y']==math.floor(base),f'{identity}: wrong parent routing height')
            roof = metadata['buildingProgram']['roof']
            require(roof['type']==roof_type and roof['pitchDegrees']==35 and roof['overhangMillimeters']==0,
                    f'{identity}: wrong parent roof parameters')
            require(metadata['wallBlockTypeId']=='lod2_exterior_wall',f'{identity}: wrong wall material')
            manifest = [ref['objectInstanceId'] for ref in metadata['generatedObjects']]
            active = {key for key,child in child_by_id.items() if child['metadata_json']['generatedFromAreaId']==identity}
            require(len(manifest)==len(set(manifest)) and set(manifest)==active,
                    f'{identity}: incomplete manifest or obsolete children')
            require(metadata.get('retiredGeneratedObjects',[])==[],f'{identity}: stale retirement manifest')
            roof_count=0
            minimum=math.inf
            for child_id in manifest:
                child=child_by_id[child_id]
                info=child['metadata_json']
                if child['object_type_id']=='building_roof':
                    roof_count+=1
                    parameters=info['roofParameters']
                    require(parameters['roofType']==roof_type and parameters['pitchDeg']==35
                            and parameters['overhangMm']==0,f'{child_id}: wrong stored roof geometry parameters')
                    require(info['roofCalculation'].get('ok') is True,f'{child_id}: missing roof calculation')
                if info.get('voxelOccupancy')=='none':
                    continue
                require(info.get('renderProfile')=='construction-grid',f'{child_id}: wrong render profile')
                requested=cells(child['occupied_cells_json'])
                require(requested==cells(info.get('constructionCells')),f'{child_id}: missing construction prisms')
                physical[child_id]=requested
                minimum=min(minimum,*(cell['minimumY'] for cell in info['constructionCells']))
            require(roof_count>0,f'{identity}: roofs missing')
            require(abs(minimum-base)<1e-7,f'{identity}: physical base still floats')
            summary.append({'parent':identity,'generation':metadata['generationId'],'storeys':count,
                            'baseY':base,'roofType':roof_type,'roofObjects':roof_count,'childObjects':len(manifest)})
        cursor.execute("""SELECT r.object_instance_id,r.chunk_x,r.chunk_y,r.chunk_z,r.occupied_cells_json
            FROM world_object_chunk_refs r WHERE r.world_db_id=%s AND r.deleted_at IS NULL
            AND r.object_instance_id=ANY(%s)""",(world,list(child_by_id)))
        refs=cursor.fetchall()
        durable={identity:set() for identity in physical}
        coordinates=set()
        for ref in refs:
            chunk=(ref['chunk_x'],ref['chunk_y'],ref['chunk_z'])
            coordinates.add(chunk)
            if ref['object_instance_id'] in durable:
                durable[ref['object_instance_id']].update(cells(ref['occupied_cells_json'],chunk,size))
        require(durable==physical,'Durable cell ownership is incomplete or displaced')
        # Check actual snapshot ownership too, where rendering and mining read.
        cursor.execute("""SELECT chunk_x,chunk_y,chunk_z,object_refs_json,palette_json,content_json->'cells' AS cells
            FROM chunk_snapshots WHERE world_db_id=%s AND deleted_at IS NULL AND status='active'""",(world,))
        snapshots=cursor.fetchall()
        runtime={identity:set() for identity in physical}
        owners={}
        semantic_seen=set()
        seen_chunks=set()
        for snapshot in snapshots:
            chunk=(snapshot['chunk_x'],snapshot['chunk_y'],snapshot['chunk_z'])
            if chunk in coordinates:
                seen_chunks.add(chunk)
            for ref in snapshot['object_refs_json'] or []:
                identity=ref.get('objectInstanceId')
                info=ref.get('metadata') or {}
                require(identity not in RETIRED and info.get('generatedFromAreaId') not in RETIRED,
                        'A removed duplicate remains in an active chunk snapshot')
                if info.get('generatedFromAreaId') in EXPECTED:
                    require(identity in child_by_id,'An obsolete keeper generation remains in an active chunk snapshot')
                if identity in child_by_id:
                    semantic_seen.add((identity,chunk))
                if info.get('voxelOccupancy')=='none' or ref.get('refRole')=='metadata_only':
                    continue
                owned={cell for cell in cells(ref.get('occupiedCells'),chunk,size)
                       if tuple(value//size for value in cell)==chunk}
                for address in owned:
                    owners[address]=identity
                if identity in physical:
                    runtime[identity].update(owned)
                    for x,y,z in owned:
                        value=(snapshot['cells'] or [])[x%size + size*(y%size+size*(z%size))]
                        require(value>0,f'{identity}: a rendered owned cell is actually air')
                        palette=snapshot['palette_json'][value-1]
                        block=palette.get('blockTypeId') if isinstance(palette,dict) else palette
                        expected=ref.get('fillBlockTypeId') or info.get('fillBlockTypeId')
                        require(not expected or block==expected,f'{identity}: owned cell has a different material')
        require(seen_chunks==coordinates,'A required vertical or horizontal structure snapshot is missing')
        require(runtime==physical,'Runtime snapshot cell ownership is incomplete')
        for child in children:
            if child['object_type_id']=='building_roof':
                anchor=(child['primary_chunk_x'],child['primary_chunk_y'],child['primary_chunk_z'])
                require((child['object_instance_id'],anchor) in semantic_seen,
                        'A current roof is missing from its rendering anchor chunk')
        require(all(owners.get(cell)==identity for identity,owned in physical.items() for cell in owned),
                'A newer foreign object owns a current building cell')
        print(json.dumps({'ok':True,'readOnly':True,'liveParents':2,'deletedDuplicateParents':len(RETIRED),
                          'otherLiveParentsOutsideRecovery':sorted(set(all_live)-targets),
                          'verifiedStructureChunks':len(coordinates),'verifiedPhysicalCells':sum(map(len,physical.values())),
                          'buildings':summary},ensure_ascii=False))
    finally:
        connection.rollback()
        connection.close()


if __name__=='__main__':
    main()
