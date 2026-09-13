"""Read-only DGM/parent input for the real-controller grounding preview."""
import json
import math
from pathlib import Path
from shapely.geometry import Polygon, shape
from src.planning_duplicate_repair import fingerprint


def main():
    from wsgi import app
    from extensions import db
    from models import WorldObjectInstance
    from routes import commands
    from sqlalchemy import text
    from src.world.earth.terrain_pipeline import generate_earth_terrain_chunk
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with app.app_context():
        try:
            db.session.execute(text('SET TRANSACTION READ ONLY'))
            _, _, world = commands._resolve_project_world_context(
                'chk_prj_prj_da09805bc6e54b29816c8cd6_6931567e1657', 'world_spawn')
            ids = ['planning_build_area_mto4qfdv_gomn9e', 'planning_build_area_mto6gn4a_c8e275']
            parents = commands._query_without_relationships(WorldObjectInstance.query.filter(
                WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.object_instance_id.in_(ids),
                WorldObjectInstance.deleted_at.is_(None))).all()
            if len(parents) != 2:
                raise ValueError('Both explicit keeper parents must exist.')
            cache, output = {}, []
            provider = world.build_earth_provider()
            size = int(world.chunk_size)
            for parent in parents:
                footprint = shape(parent.footprint_json)
                min_x, min_z, max_x, max_z = footprint.bounds
                heights, releases = [], set()
                for z in range(math.floor(min_z), math.ceil(max_z)):
                    for x in range(math.floor(min_x), math.ceil(max_x)):
                        for upper, corners_xz in [(True, [(x,z),(x+1,z),(x+1,z+1)]),
                                                  (False, [(x,z),(x+1,z+1),(x,z+1)])]:
                            piece = footprint.intersection(Polygon(corners_xz))
                            if piece.is_empty or piece.area < 1e-10:
                                continue
                            cx, cz = x // size, z // size
                            if (cx, cz) not in cache:
                                cache[cx, cz] = generate_earth_terrain_chunk(world=world, provider=provider,
                                    chunk_x=cx, chunk_y=0, chunk_z=cz)
                            chunk = cache[cx, cz]
                            terrain = chunk.get('terrain') or {}
                            data = chunk.get('metadata', {}).get('terrainSurface', {})
                            h = data.get('cornerHeights', [])
                            if terrain.get('fallback') or data.get('sampleStepM') != 1 or len(h) != (size + 1) ** 2:
                                raise ValueError('Complete approved 1m DGM is required; no flat fallback.')
                            releases.add(terrain.get('releaseKey'))
                            ix, iz = x % size, z % size
                            a,b,c,d = [h[px + (size+1)*pz] for px,pz in
                                       [(ix,iz),(ix+1,iz),(ix+1,iz+1),(ix,iz+1)]]
                            polygons = [piece] if piece.geom_type == 'Polygon' else [p for p in piece.geoms if p.geom_type == 'Polygon']
                            for polygon in polygons:
                                for px, pz in polygon.exterior.coords:
                                    u, v = px-x, pz-z
                                    heights.append(a + (b-a)*u + (c-b)*v if upper else a + (c-d)*u + (d-a)*v)
                if not heights:
                    raise ValueError('No complete terrain coverage for keeper footprint.')
                # Floor to the millimetre so numeric rounding cannot introduce
                # a sliver of air beneath the low corner of a terrain triangle.
                baseline = math.floor(min(heights)*1000 + 1e-6)/1000
                record = {'objectInstanceId': parent.object_instance_id,
                    'anchor': {'x': parent.anchor_x, 'y': parent.anchor_y, 'z': parent.anchor_z},
                    'footprint': parent.footprint_json, 'metadata': parent.metadata_json}
                output.append({'parent': record, 'parentSha256': fingerprint(record),
                    'sourceRevision': parent.revision, 'sourceUpdatedAt': parent.updated_at.isoformat(),
                    'sourceUpdatedCommand': parent.updated_by_command_id,
                    'terrain': {'policy': 'min-exact-footprint-triangle-surface.v1', 'minimumY': min(heights),
                        'maximumY': max(heights), 'baselineY': baseline, 'releaseKeys': sorted(releases)}})
            args.output.write_text(json.dumps(output, ensure_ascii=False), encoding='utf-8')
            print(json.dumps([{'parent': r['parent']['objectInstanceId'], 'generation': r['parent']['metadata']['generationId'],
                'count': r['parent']['metadata']['storeyCount'], 'terrain': r['terrain'],
                'updatedAt': r['sourceUpdatedAt'], 'updatedCommand': r['sourceUpdatedCommand']} for r in output]))
        finally:
            db.session.rollback()


if __name__ == '__main__':
    main()
