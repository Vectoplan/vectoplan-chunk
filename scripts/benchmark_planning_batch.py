"""Replay two captured planning generations in an automatically rolled-back world.

Input: gzip JSON array of {"id": <capture row id>, "request": <ObjectBatch>}.
The existing world is read only to obtain its registry/project configuration.
Every placement, event and snapshot is restricted to a newly created sandbox;
the database transaction is always rolled back and never committed.
"""
import argparse
from copy import deepcopy
import gzip
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captures', type=Path)
    parser.add_argument('--before-id', type=int, required=True)
    parser.add_argument('--after-id', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    captured = {entry['id']: entry['request'] for entry in json.loads(gzip.decompress(args.captures.read_bytes()))}
    initial, replacement = deepcopy(captured[args.before_id]), deepcopy(captured[args.after_id])
    parent = next(child for child in initial['commands'] if child.get('objectTypeId') == 'planning_build_area')
    initial['planningBuildingEdit'] = {'parentObjectInstanceId': parent['objectInstanceId'], 'previousGenerationId': None}

    # Import command executors without the production startup audit, which can
    # inspect unrelated worlds before the isolated benchmark transaction.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from flask import Flask
    from extensions import db
    from models import Project, Universe, WorldInstance, WorldObjectInstance, ChunkSnapshot
    from routes import commands
    from sqlalchemy.orm import noload

    app = Flask('planning-performance-rollback')
    app.config.update(SQLALCHEMY_DATABASE_URI=os.environ['DATABASE_URL'],
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)

    with app.app_context(), app.test_request_context('/'):
        try:
            base = WorldInstance.query.options(noload('*')).filter(WorldInstance.deleted_at.is_(None)).first()
            if base is None:
                raise RuntimeError('A configured registry and world are required for the rollback benchmark.')
            project = db.session.get(Project, base.project_db_id, options=[noload('*')])
            universe = db.session.get(Universe, base.universe_db_id, options=[noload('*')])
            nonce = uuid4().hex
            world = WorldInstance.create(project_db_id=project.id, universe_db_id=universe.id,
                world_id=f'world_perf_{nonce}', slug=f'perf-{nonce}', name='Uncommitted planning benchmark',
                world_role='sandbox', template_id='flat', provider_id='flat', provider_world_id='flat',
                block_registry_id=base.block_registry_id, block_registry_version=base.block_registry_version,
                metadata_json={'testScope': 'planning-performance-rollback'})
            db.session.add(world)
            db.session.flush()
            timings = []
            result = None
            for index, payload in enumerate((initial, replacement)):
                payload['commandId'] = f'perf_{nonce}_{index}'
                started = time.perf_counter()
                _, result = commands._execute_command(project=project, universe=universe, world=world, payload=payload)
                db.session.flush()
                timings.append(time.perf_counter() - started)
            live = WorldObjectInstance.query.options(noload('*')).filter_by(world_db_id=world.id, deleted_at=None).all()
            assert {obj.object_instance_id for obj in live} == {child['objectInstanceId'] for child in replacement['commands']}
            snapshots = ChunkSnapshot.query.options(noload('*')).filter_by(world_db_id=world.id).all()
            geometry = [{'key': snapshot.chunk_key, 'cells': snapshot.content_json['cells'],
                         'palette': snapshot.palette_json, 'refs': snapshot.object_refs_json}
                        for snapshot in sorted(snapshots, key=lambda snapshot: snapshot.chunk_key)]
            summary = {'initialSeconds': timings[0], 'replacementSeconds': timings[1], 'rollbackOnly': True,
                'liveObjects': len(live), 'chunks': len(snapshots), 'affectedCells': len(result['affectedCells']),
                'eventCount': len(result['eventIds'])}
            args.output.write_text(json.dumps(summary, indent=2), encoding='utf-8')
            args.output.with_suffix('.geometry.json.gz').write_bytes(gzip.compress(json.dumps(geometry, sort_keys=True).encode()))
            print(json.dumps(summary))
        finally:
            db.session.rollback()


if __name__ == '__main__':
    main()
