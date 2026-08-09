from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from src.world.earth.terrain_pipeline import (
    TerrainChunkCache,
    TerrainPipelineConfig,
    _claim_region_job,
    apply_earth_surface_shell,
    generate_earth_terrain_chunk,
)


@dataclass
class _World:
    chunk_size: int = 16
    surface_y: int = 0
    min_y: int = -64
    max_y: int = 512
    global_reference_fingerprint: str = 'reference-test'


class _Provider:
    reference_fingerprint = 'reference-test'
    frame = SimpleNamespace(
        reference_local_position=SimpleNamespace(
            x=Decimal('8.5'),
            z=Decimal('8.5'),
        )
    )

    @staticmethod
    def normalize_chunk_address(address):
        return SimpleNamespace(
            canonical=SimpleNamespace(
                x=int(address[0]),
                y=int(address[1]),
                z=int(address[2]),
            )
        )

    @staticmethod
    def local_to_global(position):
        return SimpleNamespace(
            target_coordinate=SimpleNamespace(
                x=Decimal('10') + Decimal(position.x) / Decimal('100000'),
                y=Decimal('49') + Decimal(position.z) / Decimal('100000'),
            )
        )


class _Client:
    def __init__(self):
        self.grid_calls = 0

    @staticmethod
    def source_version(dataset_id):
        return {'dataset_id': dataset_id, 'release_key': 'release-1'}

    def terrain_grid(self, dataset_id, points, *, radius_m):
        self.grid_calls += 1
        items = []
        for point in points:
            point_id = str(point['id'])
            elevation = 100.0
            if point_id.startswith('sample:'):
                _, sample_x, sample_z = point_id.split(':')
                elevation += (int(sample_x) + int(sample_z)) / 2.0
            items.append({'id': point_id, 'found': True, 'value': elevation})
        return {
            'ok': True,
            'dataset_id': dataset_id,
            'release_key': 'release-1',
            'source_partial': False,
            'items': items,
            'counts': {'requested': len(items), 'found': len(items)},
        }


class _UnavailableClient:
    @staticmethod
    def source_version(dataset_id):
        raise RuntimeError(f'{dataset_id} unavailable')


class _PreparingReleaseClient(_Client):
    @staticmethod
    def source_version(dataset_id):
        return {
            'dataset_id': dataset_id,
            'release_key': 'release-2',
            'terrain_serving': {
                'status': 'running',
                'processedFiles': 750,
                'totalFiles': 40389,
                'failedFiles': 0,
            },
        }

    def terrain_grid(self, dataset_id, points, *, radius_m):
        raise AssertionError('Ein unfertiger Release darf nicht abgefragt werden.')


def _config(tmp_path: Path) -> TerrainPipelineConfig:
    return TerrainPipelineConfig(
        enabled=True,
        orchestrator_base_url='http://orchestrator.invalid',
        service_token='test',
        request_timeout_seconds=1.0,
        version_cache_seconds=0.0,
        cache_root=tmp_path,
        radius_m=10.0,
        region_enabled=False,
    )


def test_dgm_chunk_is_release_cached(tmp_path):
    client = _Client()
    cache = TerrainChunkCache(tmp_path)
    first = generate_earth_terrain_chunk(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_y=0,
        chunk_z=0,
        config=_config(tmp_path),
        client=client,
        cache=cache,
    )
    second = generate_earth_terrain_chunk(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_y=0,
        chunk_z=0,
        config=_config(tmp_path),
        client=client,
        cache=cache,
    )

    assert first['terrain']['status'] == 'dgm'
    assert first['terrain']['cache']['status'] == 'miss'
    assert first['stats']['nonAirCellCount'] > 0
    assert len(first['cells']) == 16 ** 3
    assert second['terrain']['cache']['status'] == 'hit'
    assert second['contentHash'] == first['contentHash']
    assert client.grid_calls == 1
    assert first['terrain']['samplePointCount'] == 16
    assert first['terrain']['queryCounts']['requested'] == 17


def test_vertical_chunks_reuse_release_column_cache(tmp_path):
    client = _Client()
    cache = TerrainChunkCache(tmp_path)
    upper = generate_earth_terrain_chunk(
        world=_World(),
        provider=_Provider(),
        chunk_x=2,
        chunk_y=0,
        chunk_z=3,
        config=_config(tmp_path),
        client=client,
        cache=cache,
    )
    lower = generate_earth_terrain_chunk(
        world=_World(),
        provider=_Provider(),
        chunk_x=2,
        chunk_y=-1,
        chunk_z=3,
        config=_config(tmp_path),
        client=client,
        cache=cache,
    )

    assert upper['terrain']['cache']['column'] == 'miss'
    assert lower['terrain']['cache']['column'] == 'hit'
    assert client.grid_calls == 1


def test_prepared_project_region_avoids_per_chunk_geodata_query(tmp_path):
    client = _Client()
    cache = TerrainChunkCache(tmp_path)
    config = replace(_config(tmp_path), region_enabled=True)
    cache.store_region(
        reference_fingerprint='reference-test',
        dataset_id='digitales-gelaendemodell-5m',
        release_key='release-1',
        center_chunk_x=0,
        center_chunk_z=0,
        radius_chunks=64,
        sample_step_chunks=2,
        axis_world_x=[-1024.0, 0.0, 1024.0],
        axis_world_z=[-1024.0, 0.0, 1024.0],
        values=[
            98.0, 99.0, 100.0,
            99.0, 100.0, 101.0,
            100.0, 101.0, 102.0,
        ],
        anchor_elevation_m=100.0,
        source_partial=False,
        missing_samples=0,
    )

    result = generate_earth_terrain_chunk(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_y=0,
        chunk_z=0,
        config=config,
        client=client,
        cache=cache,
    )

    assert result['terrain']['status'] == 'dgm'
    assert result['terrain']['cache']['region'] == 'hit'
    assert result['terrain']['sampleStepM'] == 32
    assert client.grid_calls == 0


def test_preparing_release_keeps_previous_project_region_active(tmp_path):
    client = _PreparingReleaseClient()
    cache = TerrainChunkCache(tmp_path)
    config = replace(_config(tmp_path), region_enabled=True)
    cache.store_region(
        reference_fingerprint='reference-test',
        dataset_id='digitales-gelaendemodell-5m',
        release_key='release-1',
        center_chunk_x=0,
        center_chunk_z=0,
        radius_chunks=64,
        sample_step_chunks=2,
        axis_world_x=[-1024.0, 0.0, 1024.0],
        axis_world_z=[-1024.0, 0.0, 1024.0],
        values=[
            98.0, 99.0, 100.0,
            99.0, 100.0, 101.0,
            100.0, 101.0, 102.0,
        ],
        anchor_elevation_m=100.0,
        source_partial=False,
        missing_samples=0,
    )

    result = generate_earth_terrain_chunk(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_y=0,
        chunk_z=0,
        config=config,
        client=client,
        cache=cache,
    )

    assert result['terrain']['releaseKey'] == 'release-1'
    assert result['terrain']['pendingReleaseKey'] == 'release-2'
    assert result['terrain']['releaseSwitchPending'] is True
    assert result['terrain']['cache']['region'] == 'hit'
    assert client.grid_calls == 0


def test_region_preparation_lock_is_shared_via_filesystem(tmp_path):
    region_path = tmp_path / 'reference' / 'dataset' / 'release' / 'regions' / 'region.json.gz'
    first = _claim_region_job(region_path)
    assert first is not None
    assert _claim_region_job(region_path) is None
    first.unlink()
    second = _claim_region_job(region_path)
    assert second is not None
    second.unlink()


def test_unavailable_dgm_returns_non_empty_flat_fallback(tmp_path):
    result = generate_earth_terrain_chunk(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_y=0,
        chunk_z=0,
        config=_config(tmp_path),
        client=_UnavailableClient(),
        cache=TerrainChunkCache(tmp_path),
    )

    assert result['terrain']['status'] == 'fallback-flat'
    assert result['terrain']['fallback'] is True
    assert result['stats']['nonAirCellCount'] == 16 * 16
    assert len(result['cells']) == 16 ** 3


def test_surface_shell_keeps_five_blocks_and_marks_deeper_cells_implicit():
    chunk_size = 8
    surfaces = [6] * (chunk_size ** 2)
    cells = [0] * (chunk_size ** 3)
    for local_z in range(chunk_size):
        for local_x in range(chunk_size):
            for local_y in range(7):
                index = local_x + chunk_size * (
                    local_y + chunk_size * local_z
                )
                cells[index] = 3
    chunk = {
        'chunkSize': chunk_size,
        'chunkY': 0,
        'cells': cells,
        'surfaceYByColumn': surfaces,
        'stats': {},
    }

    assert apply_earth_surface_shell(chunk) is True
    assert chunk['cells'].count(3) == 5 * chunk_size ** 2
    assert chunk['cells'].count(-1) == 2 * chunk_size ** 2
    assert chunk['surfaceDepth'] == 5
    assert 'surfaceYByColumn' not in chunk

    deep_chunk = {
        'chunkSize': chunk_size,
        'chunkY': 0,
        'cells': [3] * (chunk_size ** 3),
        'surfaceYByColumn': [20] * (chunk_size ** 2),
        'stats': {},
    }
    assert apply_earth_surface_shell(deep_chunk) is True
    assert deep_chunk['cells'].count(-1) == chunk_size ** 3
