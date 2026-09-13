"""Apply two reviewed controller plans atomically; default is read-only validation."""
import argparse
import hashlib
import json
from pathlib import Path
from src.planning_grounding import KEEPERS, protect_grounding_destination, validate_grounding_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, action='append', required=True)
    parser.add_argument('--expected-sha', action='append')
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    encoded = [path.read_bytes() for path in args.plan]
    digests = [hashlib.sha256(value).hexdigest() for value in encoded]
    if any(len(value) > 32*1024*1024 for value in encoded):
        raise ValueError('Grounding plan exceeds the bounded payload allowance.')
    records = [json.loads(value) for value in encoded]
    identities = [record['payload']['planningBuildingEdit']['parentObjectInstanceId'] for record in records]
    if len(records) != 2 or set(identities) != set(KEEPERS):
        raise ValueError('Supply exactly the two explicit keeper plans.')
    if args.apply:
        if args.expected_sha != digests:
            raise ValueError('Provide the reviewed SHA256 values in the same order as --plan.')
        if not args.backup or not args.backup.is_file() or args.backup.stat().st_size < 100:
            raise ValueError('An existing PostgreSQL custom backup is required.')
        with args.backup.open('rb') as backup:
            if backup.read(5) != b'PGDMP':
                raise ValueError('Backup must be a pg_dump custom archive.')
    from wsgi import app
    from extensions import db
    from models import WorldInstance, WorldObjectInstance, WorldCommandLog
    from routes import commands
    from sqlalchemy import select, text
    with app.app_context(), app.test_request_context('/'):
        try:
            if not args.apply:
                db.session.execute(text('SET TRANSACTION READ ONLY'))
            project, universe, world = commands._resolve_project_world_context(
                'chk_prj_prj_da09805bc6e54b29816c8cd6_6931567e1657', 'world_spawn')
            if args.apply:
                db.session.execute(select(WorldInstance.id).where(WorldInstance.id == world.id).with_for_update()).one()
            prepared, replays = [], {}
            for record, identity in zip(records, identities):
                payload = record['payload']
                receipt = commands._query_without_relationships(WorldCommandLog.query.filter(
                    WorldCommandLog.command_id == payload['commandId'])).one_or_none()
                if receipt is not None:
                    from src.planning_generation import replay_result
                    if receipt.world_db_id != world.id:
                        raise ValueError('The command identity belongs to another world.')
                    replays[identity] = replay_result(receipt, payload)
                    prepared.append(payload)
                    continue
                parent = commands._query_without_relationships(WorldObjectInstance.query.filter(
                    WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.object_instance_id == identity,
                    WorldObjectInstance.object_type_id == 'planning_build_area', WorldObjectInstance.deleted_at.is_(None))).one()
                source = {'objectInstanceId': identity, 'anchor': {'x':parent.anchor_x,'y':parent.anchor_y,'z':parent.anchor_z},
                          'footprint': parent.footprint_json, 'metadata': parent.metadata_json}
                checked = validate_grounding_plan(record, source)
                own_ids = {ref['objectInstanceId'] for ref in parent.metadata_json.get('generatedObjects', [])}
                children = commands._query_without_relationships(WorldObjectInstance.query.filter(
                    WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.object_instance_id.in_(own_ids),
                    WorldObjectInstance.deleted_at.is_(None))).all()
                if {child.object_instance_id for child in children} != own_ids:
                    raise ValueError('The current generation is incomplete; grounding will not restore missing user objects.')
                if any(child.updated_by_command_id and child.updated_by_command_id != child.created_by_command_id for child in children):
                    raise ValueError('A current roof or building child was edited independently after generation.')
                # Use the production limits/parser even on dry-run, without
                # creating a log, mutating a snapshot or materializing terrain.
                commands._normalize_object_batch_commands(checked)
                protect_grounding_destination(project=project,universe=universe,world=world,payload=checked,own_ids=own_ids)
                prepared.append(checked)
            results = []
            if args.apply:
                for identity, payload in zip(identities, prepared):
                    result = replays.get(identity)
                    if result is None:
                        _, result = commands._execute_command(project=project, universe=universe, world=world, payload=payload)
                    results.append({'parent': identity, 'commandId': payload['commandId'],
                                    'replayed': bool(result.get('replayed')), 'dirtyChunks': result.get('dirtyChunks', [])})
                # The two buildings share one commit; a failure in the second
                # also rolls back the first. Reuse these immutable plan files
                # after an uncertain response, never generate new command IDs.
                db.session.commit()
            print(json.dumps({'applied': args.apply, 'plans': [{'parent': identity, 'sha256': digest,
                'commandId': record['payload']['commandId'], 'baseY': record['terrain']['baselineY']}
                for identity,digest,record in zip(identities,digests,records)], 'results': results}))
        finally:
            db.session.rollback()


if __name__ == '__main__':
    main()
