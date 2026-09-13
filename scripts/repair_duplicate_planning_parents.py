"""Explicit duplicate-parent recovery; dry-run unless --apply with backup and plan CAS."""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from src.planning_duplicate_repair import duplicate_parent_plan, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--world', default='world_spawn')
    parser.add_argument('--keep-parent', required=True)
    parser.add_argument('--retire-parent', required=True, action='append')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-plan')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    from wsgi import app
    from extensions import db
    from models import WorldInstance, WorldObjectInstance, WorldObjectChunkRef
    from routes import commands
    from sqlalchemy import select, text
    with app.app_context(), app.test_request_context('/'):
        try:
            if not args.apply:
                db.session.execute(text('SET TRANSACTION READ ONLY'))
            project, universe, world = commands._resolve_project_world_context(args.project, args.world)
            if args.apply:
                if not args.backup or not args.backup.is_file() or args.backup.stat().st_size < 100:
                    raise ValueError('An existing PostgreSQL custom backup is required.')
                with args.backup.open('rb') as backup:
                    if backup.read(5) != b'PGDMP':
                        raise ValueError('Backup must be a pg_dump custom archive.')
                db.session.execute(select(WorldInstance.id).where(WorldInstance.id == world.id).with_for_update()).one()
            ids = [args.keep_parent, *args.retire_parent]
            parents = commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.object_instance_id.in_(ids),
                WorldObjectInstance.object_type_id == 'planning_build_area', WorldObjectInstance.deleted_at.is_(None))).all()
            if {parent.object_instance_id for parent in parents} != set(ids):
                raise ValueError('A selected planning parent no longer exists.')
            # Extract only parent commands in PostgreSQL, not multi-megabyte
            # child construction geometry from every historical batch.
            logs = db.session.execute(text("""
                SELECT log.id, log.command_id, log.created_at, log.user_id, log.session_id,
                       item.value AS parent_payload
                FROM world_command_logs log
                CROSS JOIN LATERAL jsonb_array_elements(CASE
                  WHEN jsonb_typeof(log.request_payload_json->'commands') = 'array'
                  THEN log.request_payload_json->'commands' ELSE '[]'::jsonb END) item
                WHERE log.world_db_id = :world_id AND log.command_status = 'applied'
                  AND log.command_type = 'ObjectBatch'
                  AND item.value->>'objectInstanceId' = ANY(:parent_ids)
                  AND item.value->>'objectTypeId' = 'planning_build_area'
                  AND log.session_id LIKE 'world_edit_planning_generation_%'
                ORDER BY log.id
            """), {'world_id': world.id, 'parent_ids': ids}).mappings().all()
            object_lookup = {parent.object_instance_id: parent for parent in parents}
            evidence = {}
            for parent in parents:
                metadata = parent.metadata_json or {}
                generation_logs = []
                for log in logs:
                    payload = log['parent_payload']
                    if payload.get('objectInstanceId') != parent.object_instance_id:
                        continue
                    info = payload.get('metadata') or {}
                    generation_logs.append({'id': log['id'], 'commandId': log['command_id'],
                        'createdAt': log['created_at'].isoformat(), 'userId': log['user_id'],
                        'sessionId': log['session_id'], 'generationId': info.get('generationId'),
                        'pathBrush': info.get('pathBrush'), 'footprint': payload.get('footprint')})
                if not generation_logs:
                    raise ValueError('No applied generation history for selected parent.')
                children = commands._query_without_relationships(WorldObjectInstance.query.filter(
                    WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None),
                    WorldObjectInstance.metadata_json['generatedFromAreaId'].astext == parent.object_instance_id)).all()
                child_evidence = []
                for child in sorted(children, key=lambda obj: obj.object_instance_id):
                    object_lookup[child.object_instance_id] = child
                    refs = commands._query_without_relationships(WorldObjectChunkRef.query.filter(
                        WorldObjectChunkRef.object_instance_db_id == child.id,
                        WorldObjectChunkRef.deleted_at.is_(None))).all()
                    owned = sorted((int(cell.get('x', ref.chunk_x * world.chunk_size + cell.get('localX', 0))),
                                    int(cell.get('y', ref.chunk_y * world.chunk_size + cell.get('localY', 0))),
                                    int(cell.get('z', ref.chunk_z * world.chunk_size + cell.get('localZ', 0))))
                                   for ref in refs for cell in ref.occupied_cells_json or [])
                    requested = sorted((cell['x'], cell['y'], cell['z']) for cell in child.occupied_cells_json or [])
                    info = child.metadata_json or {}
                    cells = info.get('constructionCells') or []
                    minimum_y = min((cell.get('minimumY', cell.get('y')) for cell in cells), default=None)
                    child_evidence.append({'id': child.object_instance_id, 'revision': child.revision,
                        'metadataSha256': fingerprint(info), 'createdCommand': child.created_by_command_id,
                        'updatedCommand': child.updated_by_command_id, 'physical': info.get('voxelOccupancy') != 'none',
                        'groundFloor': child.object_type_id == 'planning_building_floor_slab'
                            and minimum_y == parent.anchor_y,
                        'requestedCells': len(requested), 'ownedCells': len(owned),
                        'requestedCellHash': fingerprint(requested), 'ownedCellHash': fingerprint(owned)})
                evidence[parent.object_instance_id] = {'id': parent.object_instance_id,
                    'revision': parent.revision, 'generationId': metadata.get('generationId'),
                    'updatedAt': parent.updated_at.isoformat(), 'updatedCommand': parent.updated_by_command_id,
                    'anchor': [parent.anchor_x, parent.anchor_y, parent.anchor_z],
                    'metadataSha256': fingerprint(metadata), 'pathBrush': metadata.get('pathBrush'),
                    'footprint': parent.footprint_json,
                    'manifestIds': sorted(ref['objectInstanceId'] for ref in metadata.get('generatedObjects', [])),
                    'firstGenerationLog': generation_logs[0], 'latestGenerationLog': generation_logs[-1],
                    'children': child_evidence}
            plan = duplicate_parent_plan(evidence[args.keep_parent], [evidence[key] for key in args.retire_parent])
            if args.apply:
                if args.expected_plan != plan['planSha256']:
                    raise ValueError('The plan changed after dry-run; nothing was removed.')
                removals = [{'type': 'RemoveObject', 'objectInstanceId': identity,
                    'position': {'x': object_lookup[identity].anchor_x, 'y': object_lookup[identity].anchor_y,
                                 'z': object_lookup[identity].anchor_z}} for identity in plan['removeObjectIds']]
                command_id = f'repair_planning_duplicates_{uuid4().hex}'
                _, result = commands._execute_command(project=project, universe=universe, world=world,
                    payload={'type': 'ObjectBatch', 'commandId': command_id, 'userId': 'planning_duplicate_repair',
                             'sessionId': command_id, 'position': removals[0]['position'], 'commands': removals})
                db.session.commit()
                plan.update(applied=True, commandId=command_id, dirtyChunks=result.get('dirtyChunks', []))
            if args.output:
                args.output.write_text(json.dumps(plan, sort_keys=True, ensure_ascii=False), encoding='utf-8')
                print(json.dumps({'planSha256': plan['planSha256'], 'keep': args.keep_parent,
                    'retire': args.retire_parent, 'removeCount': len(plan['removeObjectIds']),
                    'keeperState': {key: plan['keeper'][key] for key in ('generationId', 'updatedAt', 'updatedCommand')},
                    'output': str(args.output), 'applied': plan.get('applied', False)}))
            else:
                print(json.dumps(plan, sort_keys=True, ensure_ascii=False))
        finally:
            db.session.rollback()


if __name__ == '__main__':
    main()
