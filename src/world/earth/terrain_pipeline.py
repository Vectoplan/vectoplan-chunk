'''Earth-only terrain generation from approved Vectoplan geodata releases.

The Webscraper remains the byte source of truth.  GeoServer Orchestrator owns
the approved, versioned data access.  Chunk stores only its derived voxel
chunks, keyed by Earth reference, dataset release and chunk coordinates.
Manual ChunkSnapshots are resolved before this module is called and therefore
always remain authoritative over generated terrain.
'''

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from bisect import bisect_left
import gzip
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from threading import RLock, Thread
import time
from typing import Any, Final, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from ...geodata.fixed_projects import DIGITAL_ELEVATION_MODEL
from ...georeferencing.earth_grid import LocalEarthPosition


TERRAIN_PIPELINE_VERSION: Final[str] = 'earth-dgm5-terrain.v5'
CACHE_SCHEMA_VERSION: Final[str] = 'earth-terrain-chunk-cache.v6'
COLUMN_CACHE_SCHEMA_VERSION: Final[str] = 'earth-terrain-column-cache.v4'
REGION_CACHE_SCHEMA_VERSION: Final[str] = 'earth-terrain-region-cache.v1'
PALETTE: Final[tuple[str, ...]] = (
    'system_terrain_humus',
    'system_terrain_soil',
    'system_terrain_rock',
)
HUMUS_VALUE: Final[int] = 1
SOIL_VALUE: Final[int] = 2
ROCK_VALUE: Final[int] = 3
AIR_VALUE: Final[int] = 0
_SEGMENT_PATTERN: Final[re.Pattern[str]] = re.compile(r'[^a-zA-Z0-9._-]+')
_REGION_LOCKS_GUARD = RLock()
_REGION_LOCKS: dict[str, RLock] = {}
_REGION_JOBS: set[str] = set()
_REGION_JOB_LOCK_MAX_AGE_SECONDS: Final[float] = 30.0 * 60.0


class TerrainSourceError(RuntimeError):
    pass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {'0', 'false', 'no', 'off', 'disabled'}


def _safe_segment(value: Any, fallback: str) -> str:
    text = _SEGMENT_PATTERN.sub('-', str(value or '').strip()).strip('-._')
    return text[:160] or fallback


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return f'{type(exc).__name__}: {text}'[:500]


@dataclass(frozen=True, slots=True)
class TerrainPipelineConfig:
    enabled: bool
    orchestrator_base_url: str
    service_token: str
    request_timeout_seconds: float
    version_cache_seconds: float
    cache_root: Path
    radius_m: float
    sample_step_m: float = 1.0
    region_enabled: bool = True
    region_radius_chunks: int = 64
    region_sample_step_chunks: int = 2
    region_batch_points: int = 480

    @classmethod
    def from_env(cls) -> 'TerrainPipelineConfig':
        return cls(
            enabled=_env_bool('VECTOPLAN_CHUNK_TERRAIN_ENABLED', True),
            orchestrator_base_url=str(
                os.getenv(
                    'GEOSERVER_ORCHESTRATOR_INTERNAL_URL',
                    'http://geoserver-orchestrator:8010',
                )
            ).rstrip('/'),
            service_token=str(
                os.getenv('VECTOPLAN_CHUNK_GEODATA_SERVICE_TOKEN')
                or os.getenv('PRODUCTION_PUBLICATION_SERVICE_TOKEN')
                or ''
            ).strip(),
            request_timeout_seconds=max(
                1.0,
                float(os.getenv('VECTOPLAN_CHUNK_TERRAIN_REQUEST_TIMEOUT_SECONDS', '20')),
            ),
            version_cache_seconds=max(
                0.0,
                float(os.getenv('VECTOPLAN_CHUNK_TERRAIN_VERSION_CACHE_SECONDS', '60')),
            ),
            cache_root=Path(
                os.getenv(
                    'VECTOPLAN_CHUNK_TERRAIN_CACHE_DIR',
                    '/var/lib/vectoplan-chunk/terrain-cache',
                )
            ),
            radius_m=max(
                1.0,
                min(100.0, float(os.getenv('VECTOPLAN_CHUNK_TERRAIN_RADIUS_M', '10'))),
            ),
            sample_step_m=max(
                1.0,
                min(
                    10.0,
                    float(os.getenv('VECTOPLAN_CHUNK_TERRAIN_SAMPLE_STEP_M', '1')),
                ),
            ),
            region_enabled=_env_bool(
                'VECTOPLAN_CHUNK_TERRAIN_REGION_ENABLED',
                True,
            ),
            region_radius_chunks=max(
                8,
                min(
                    256,
                    int(os.getenv('VECTOPLAN_CHUNK_TERRAIN_REGION_RADIUS_CHUNKS', '64')),
                ),
            ),
            region_sample_step_chunks=max(
                1,
                min(
                    8,
                    int(os.getenv('VECTOPLAN_CHUNK_TERRAIN_REGION_SAMPLE_STEP_CHUNKS', '2')),
                ),
            ),
            region_batch_points=max(
                32,
                min(
                    500,
                    int(os.getenv('VECTOPLAN_CHUNK_TERRAIN_REGION_BATCH_POINTS', '480')),
                ),
            ),
        )


class OrchestratorGeodataClient:
    def __init__(self, config: TerrainPipelineConfig) -> None:
        self.config = config
        self._versions: dict[str, tuple[float, dict[str, Any]]] = {}
        self._version_lock = RLock()

    def _request(
        self,
        path: str,
        *,
        method: str = 'GET',
        payload: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.config.service_token:
            raise TerrainSourceError('Der interne Geodaten-Service-Token fehlt.')
        body = None
        headers = {
            'Accept': 'application/json',
            'X-Vectoplan-Service-Token': self.config.service_token,
            'X-Requested-With': 'fetch',
            'User-Agent': 'vectoplan-chunk-terrain/1',
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(
            f'{self.config.orchestrator_base_url}{path}',
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                raw = response.read(32 * 1024 * 1024)
        except HTTPError as exc:
            try:
                detail = exc.read(4096).decode('utf-8', 'replace')
            except Exception:
                detail = ''
            raise TerrainSourceError(
                f'GeoServer Orchestrator antwortete mit HTTP {exc.code}: {detail[:500]}'
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TerrainSourceError(
                f'GeoServer Orchestrator ist nicht erreichbar: {_short_error(exc)}'
            ) from exc
        try:
            result = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TerrainSourceError('GeoServer Orchestrator lieferte kein gültiges JSON.') from exc
        if not isinstance(result, dict) or result.get('ok') is not True:
            message = result.get('message') if isinstance(result, dict) else None
            raise TerrainSourceError(str(message or 'Die Geodatenabfrage war nicht erfolgreich.'))
        return result

    def source_version(self, dataset_id: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._versions.get(dataset_id)
        if cached and now - cached[0] <= self.config.version_cache_seconds:
            return dict(cached[1])
        with self._version_lock:
            now = time.monotonic()
            cached = self._versions.get(dataset_id)
            if cached and now - cached[0] <= self.config.version_cache_seconds:
                return dict(cached[1])
            dataset = quote(dataset_id, safe='')
            payload = self._request(
                f'/admin/api/production-publications/{dataset}/query/schema'
            )
            release_key = str(payload.get('release_key') or '').strip().lower()
            if not release_key:
                raise TerrainSourceError('Die freigegebene Geodatenversion fehlt.')
            version = {
                'dataset_id': dataset_id,
                'release_key': release_key,
                'source_partial': bool(payload.get('source_partial')),
                'terrain_serving': (
                    dict(payload.get('terrain_serving'))
                    if isinstance(payload.get('terrain_serving'), dict)
                    else None
                ),
            }
            self._versions[dataset_id] = (now, version)
            return dict(version)

    def terrain_grid(
        self,
        dataset_id: str,
        points: list[dict[str, Any]],
        *,
        radius_m: float,
    ) -> dict[str, Any]:
        dataset = quote(dataset_id, safe='')
        return self._request(
            f'/admin/api/production-publications/{dataset}/terrain-grid',
            method='POST',
            payload={'points': points, 'radius_m': radius_m},
        )


class TerrainChunkCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._region_memory_lock = RLock()
        self._region_memory: dict[str, tuple[int, dict[str, Any]]] = {}

    @staticmethod
    def _coordinate_name(chunk_x: int, chunk_y: int, chunk_z: int) -> str:
        return f'{int(chunk_x)}_{int(chunk_y)}_{int(chunk_z)}.json.gz'

    def _path(
        self,
        *,
        reference_fingerprint: str,
        dataset_id: str,
        release_key: str,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
    ) -> Path:
        return (
            self.root
            / _safe_segment(reference_fingerprint, 'reference')
            / _safe_segment(dataset_id, 'dataset')
            / _safe_segment(release_key, 'release')
            / self._coordinate_name(chunk_x, chunk_y, chunk_z)
        )

    def _column_path(
        self,
        *,
        reference_fingerprint: str,
        dataset_id: str,
        release_key: str,
        chunk_x: int,
        chunk_z: int,
    ) -> Path:
        return (
            self.root
            / _safe_segment(reference_fingerprint, 'reference')
            / _safe_segment(dataset_id, 'dataset')
            / _safe_segment(release_key, 'release')
            / 'columns'
            / f'{int(chunk_x)}_{int(chunk_z)}.json.gz'
        )

    def _region_path(
        self,
        *,
        reference_fingerprint: str,
        dataset_id: str,
        release_key: str,
        center_chunk_x: int,
        center_chunk_z: int,
        radius_chunks: int,
        sample_step_chunks: int,
    ) -> Path:
        return (
            self.root
            / _safe_segment(reference_fingerprint, 'reference')
            / _safe_segment(dataset_id, 'dataset')
            / _safe_segment(release_key, 'release')
            / 'regions'
            / (
                f'{int(center_chunk_x)}_{int(center_chunk_z)}_'
                f'r{int(radius_chunks)}_s{int(sample_step_chunks)}.json.gz'
            )
        )

    def load_region(self, **key: Any) -> Optional[dict[str, Any]]:
        path = self._region_path(**key)
        return self._load_region_path(path)

    def _load_region_path(self, path: Path) -> Optional[dict[str, Any]]:
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            return None
        memory_key = path.resolve(strict=False).as_posix()
        with self._region_memory_lock:
            cached = self._region_memory.get(memory_key)
            if cached and cached[0] == modified:
                return cached[1]
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as stream:
                payload = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get('schemaVersion') != REGION_CACHE_SCHEMA_VERSION
            or not isinstance(payload.get('axisWorldX'), list)
            or not isinstance(payload.get('axisWorldZ'), list)
            or not isinstance(payload.get('values'), list)
        ):
            return None
        with self._region_memory_lock:
            self._region_memory[memory_key] = (modified, payload)
        return payload

    def load_latest_region(
        self,
        *,
        reference_fingerprint: str,
        dataset_id: str,
        center_chunk_x: int,
        center_chunk_z: int,
        radius_chunks: int,
        sample_step_chunks: int,
        exclude_release_key: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        base = (
            self.root
            / _safe_segment(reference_fingerprint, 'reference')
            / _safe_segment(dataset_id, 'dataset')
        )
        name = (
            f'{int(center_chunk_x)}_{int(center_chunk_z)}_'
            f'r{int(radius_chunks)}_s{int(sample_step_chunks)}.json.gz'
        )
        excluded = _safe_segment(exclude_release_key, 'release') if exclude_release_key else None
        try:
            candidates = list(base.glob(f'*/regions/{name}'))
        except OSError:
            return None
        valid: list[tuple[float, dict[str, Any]]] = []
        for path in candidates:
            if excluded and path.parent.parent.name == excluded:
                continue
            payload = self._load_region_path(path)
            if payload is None:
                continue
            try:
                created = float(payload.get('createdAtUnix') or path.stat().st_mtime)
            except (TypeError, ValueError, OSError):
                created = 0.0
            valid.append((created, payload))
        if not valid:
            return None
        valid.sort(key=lambda item: item[0], reverse=True)
        return valid[0][1]

    def store_region(
        self,
        *,
        reference_fingerprint: str,
        dataset_id: str,
        release_key: str,
        center_chunk_x: int,
        center_chunk_z: int,
        radius_chunks: int,
        sample_step_chunks: int,
        axis_world_x: list[float],
        axis_world_z: list[float],
        values: list[float],
        anchor_elevation_m: float,
        source_partial: bool,
        missing_samples: int,
    ) -> Path:
        path = self._region_path(
            reference_fingerprint=reference_fingerprint,
            dataset_id=dataset_id,
            release_key=release_key,
            center_chunk_x=center_chunk_x,
            center_chunk_z=center_chunk_z,
            radius_chunks=radius_chunks,
            sample_step_chunks=sample_step_chunks,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'schemaVersion': REGION_CACHE_SCHEMA_VERSION,
            'pipelineVersion': TERRAIN_PIPELINE_VERSION,
            'createdAtUnix': time.time(),
            'referenceFingerprint': reference_fingerprint,
            'datasetId': dataset_id,
            'releaseKey': release_key,
            'centerChunk': [int(center_chunk_x), int(center_chunk_z)],
            'radiusChunks': int(radius_chunks),
            'sampleStepChunks': int(sample_step_chunks),
            'axisWorldX': [float(value) for value in axis_world_x],
            'axisWorldZ': [float(value) for value in axis_world_z],
            'values': [float(value) for value in values],
            'anchorElevationM': float(anchor_elevation_m),
            'sourcePartial': bool(source_partial),
            'missingSamples': int(missing_samples),
        }
        temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
        try:
            with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=6) as stream:
                json.dump(payload, stream, ensure_ascii=True, separators=(',', ':'))
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        modified = path.stat().st_mtime_ns
        with self._region_memory_lock:
            self._region_memory[path.resolve(strict=False).as_posix()] = (
                modified,
                payload,
            )
        return path

    @staticmethod
    def _load_path(path: Path) -> Optional[dict[str, Any]]:
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as stream:
                payload = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get('schemaVersion') != CACHE_SCHEMA_VERSION:
            return None
        content = payload.get('content')
        if not isinstance(content, dict) or not isinstance(content.get('cells'), list):
            return None
        return payload

    def load_exact(self, **key: Any) -> Optional[dict[str, Any]]:
        return self._load_path(self._path(**key))

    def load_column(self, **key: Any) -> Optional[dict[str, Any]]:
        path = self._column_path(**key)
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as stream:
                payload = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get('schemaVersion') != COLUMN_CACHE_SCHEMA_VERSION
            or not isinstance(payload.get('surfaces'), list)
            or not isinstance(payload.get('terrain'), dict)
        ):
            return None
        return payload

    def store_column(
        self,
        *,
        reference_fingerprint: str,
        dataset_id: str,
        release_key: str,
        chunk_x: int,
        chunk_z: int,
        surfaces: list[int],
        terrain: Mapping[str, Any],
    ) -> Path:
        path = self._column_path(
            reference_fingerprint=reference_fingerprint,
            dataset_id=dataset_id,
            release_key=release_key,
            chunk_x=chunk_x,
            chunk_z=chunk_z,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'schemaVersion': COLUMN_CACHE_SCHEMA_VERSION,
            'pipelineVersion': TERRAIN_PIPELINE_VERSION,
            'createdAtUnix': time.time(),
            'referenceFingerprint': reference_fingerprint,
            'datasetId': dataset_id,
            'releaseKey': release_key,
            'chunk': [int(chunk_x), int(chunk_z)],
            'surfaces': [int(value) for value in surfaces],
            'terrain': dict(terrain),
        }
        temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
        try:
            with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=6) as stream:
                json.dump(payload, stream, ensure_ascii=True, separators=(',', ':'))
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return path

    def load_latest(
        self,
        *,
        reference_fingerprint: str,
        dataset_id: str,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
    ) -> Optional[dict[str, Any]]:
        base = (
            self.root
            / _safe_segment(reference_fingerprint, 'reference')
            / _safe_segment(dataset_id, 'dataset')
        )
        name = self._coordinate_name(chunk_x, chunk_y, chunk_z)
        try:
            candidates = sorted(
                base.glob(f'*/{name}'),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        for path in candidates:
            payload = self._load_path(path)
            if payload is not None:
                return payload
        return None

    def store(
        self,
        *,
        reference_fingerprint: str,
        dataset_id: str,
        release_key: str,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        content: Mapping[str, Any],
    ) -> Path:
        path = self._path(
            reference_fingerprint=reference_fingerprint,
            dataset_id=dataset_id,
            release_key=release_key,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'schemaVersion': CACHE_SCHEMA_VERSION,
            'pipelineVersion': TERRAIN_PIPELINE_VERSION,
            'createdAtUnix': time.time(),
            'referenceFingerprint': reference_fingerprint,
            'datasetId': dataset_id,
            'releaseKey': release_key,
            'chunk': [int(chunk_x), int(chunk_y), int(chunk_z)],
            'content': dict(content),
        }
        temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
        try:
            with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=6) as stream:
                json.dump(payload, stream, ensure_ascii=True, separators=(',', ':'))
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return path


def _chunk_cells(
    *,
    chunk_size: int,
    chunk_y: int,
    min_y: int,
    max_y: int,
    surfaces: list[int],
) -> tuple[list[int], int]:
    cells = [AIR_VALUE] * (chunk_size ** 3)
    origin_y = chunk_y * chunk_size
    non_air = 0
    for local_z in range(chunk_size):
        for local_x in range(chunk_size):
            surface = int(surfaces[local_x + chunk_size * local_z])
            for local_y in range(chunk_size):
                world_y = origin_y + local_y
                if world_y < min_y or world_y > max_y or world_y > surface:
                    continue
                depth = surface - world_y
                value = HUMUS_VALUE if depth == 0 else SOIL_VALUE if depth <= 3 else ROCK_VALUE
                index = local_x + chunk_size * (local_y + chunk_size * local_z)
                cells[index] = value
                non_air += 1
    return cells, non_air



def apply_earth_surface_shell(
    chunk: dict[str, Any],
    *,
    depth: int = 5,
    implicit_solid_value: int = -1,
) -> bool:
    """Convert dense generated terrain to a shallow response representation."""
    cells = chunk.get('cells')
    surfaces = chunk.get('surfaceYByColumn')
    try:
        chunk_size = int(chunk.get('chunkSize') or 16)
        chunk_y = int(chunk.get('chunkY') or 0)
        shell_depth = max(1, int(depth))
        implicit_value = int(implicit_solid_value)
    except (TypeError, ValueError):
        return False

    column_count = chunk_size * chunk_size
    expected_cell_count = column_count * chunk_size
    if (
        not isinstance(cells, list)
        or len(cells) != expected_cell_count
        or not isinstance(surfaces, list)
        or len(surfaces) != column_count
    ):
        return False

    origin_y = chunk_y * chunk_size
    implicit_count = 0
    visible_solid_count = 0
    for local_z in range(chunk_size):
        for local_x in range(chunk_size):
            column_index = local_x + chunk_size * local_z
            try:
                surface_y = int(surfaces[column_index])
            except (TypeError, ValueError):
                return False
            for local_y in range(chunk_size):
                cell_index = local_x + chunk_size * (
                    local_y + chunk_size * local_z
                )
                try:
                    value = int(cells[cell_index])
                except (TypeError, ValueError):
                    return False
                if value <= 0:
                    continue
                if surface_y - (origin_y + local_y) >= shell_depth:
                    cells[cell_index] = implicit_value
                    implicit_count += 1
                else:
                    visible_solid_count += 1

    chunk.pop('surfaceYByColumn', None)
    chunk['contentProfile'] = 'surface-shell.v1'
    chunk['surfaceDepth'] = shell_depth
    chunk['implicitSolidCellValue'] = implicit_value
    stats = chunk.get('stats')
    if not isinstance(stats, dict):
        stats = {}
        chunk['stats'] = stats
    stats['visibleSolidCellCount'] = visible_solid_count
    stats['implicitSolidCellCount'] = implicit_count
    stats['surfaceDepth'] = shell_depth
    return True

def _content_hash(
    cells: list[int],
    *,
    reference_fingerprint: str,
    release_key: str,
    chunk: tuple[int, int, int],
    surface_shape: Optional[Mapping[str, Any]] = None,
) -> str:
    payload = {
        'pipelineVersion': TERRAIN_PIPELINE_VERSION,
        'referenceFingerprint': reference_fingerprint,
        'releaseKey': release_key,
        'chunk': list(chunk),
        'palette': list(PALETTE),
        'cellsSha256': sha256(bytes(cells)).hexdigest(),
        'terrainSurface': surface_shape,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def _generated_content(
    *,
    chunk_size: int,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    min_y: int,
    max_y: int,
    surfaces: list[int],
    reference_fingerprint: str,
    release_key: str,
    terrain: Mapping[str, Any],
) -> dict[str, Any]:
    surface_shape = terrain.get('surfaceShape')
    if isinstance(surface_shape, Mapping):
        corners = surface_shape.get('cornerHeights')
        if isinstance(corners, list) and len(corners) == (chunk_size + 1) ** 2:
            # Occupancy encloses the true surface. Only cells intersected by
            # the heightfield are cut; everything underneath stays a cube.
            stride = chunk_size + 1
            surfaces = [
                math.ceil(max(corners[x + stride * z], corners[x + 1 + stride * z],
                              corners[x + stride * (z + 1)], corners[x + 1 + stride * (z + 1)])) - 1
                for z in range(chunk_size) for x in range(chunk_size)
            ]
    cells, non_air = _chunk_cells(
        chunk_size=chunk_size,
        chunk_y=chunk_y,
        min_y=min_y,
        max_y=max_y,
        surfaces=surfaces,
    )
    chunk = (chunk_x, chunk_y, chunk_z)
    content_hash = _content_hash(
        cells,
        reference_fingerprint=reference_fingerprint,
        release_key=release_key,
        chunk=chunk,
        surface_shape=surface_shape if isinstance(surface_shape, Mapping) else None,
    )
    public_terrain = {key: value for key, value in terrain.items() if key != 'surfaceShape'}
    return {
        'schemaVersion': 'earth-generated-chunk.v2',
        'source': 'generated',
        'generatorType': 'earth-dgm5-terrain',
        'generatorVersion': TERRAIN_PIPELINE_VERSION,
        'chunkVersion': f'dgm5:{release_key[:16]}:{TERRAIN_PIPELINE_VERSION}',
        'generationMode': 'approved-dgm5-release',
        'chunkX': chunk_x,
        'chunkY': chunk_y,
        'chunkZ': chunk_z,
        'chunkSize': chunk_size,
        'cellCount': len(cells),
        'palette': list(PALETTE),
        'cells': cells,
        # Kept in the derived cache so the HTTP adapter can create a shallow
        # surface representation without guessing where a column's actual DGM
        # surface is. Canonical cells remain dense for later materialization.
        'surfaceYByColumn': [int(value) for value in surfaces],
        'nonAirCellCount': non_air,
        'contentHash': content_hash,
        'stats': {
            'nonAirCellCount': non_air,
            'minimumSurfaceY': min(surfaces),
            'maximumSurfaceY': max(surfaces),
            'terrain': public_terrain,
        },
        'terrain': public_terrain,
        'metadata': {'terrainSurface': dict(surface_shape)} if isinstance(surface_shape, Mapping) else {},
    }


def _fallback_content(
    world: Any,
    *,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    reference_fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    chunk_size = int(getattr(world, 'chunk_size', 16) or 16)
    surface_y = int(getattr(world, 'surface_y', 0) or 0)
    terrain = {
        'status': 'fallback-flat',
        'fallback': True,
        'reason': reason[:500],
        'datasetId': DIGITAL_ELEVATION_MODEL.dataset_id,
        'releaseKey': None,
        'cache': {'status': 'not-used'},
    }
    return _generated_content(
        chunk_size=chunk_size,
        chunk_x=chunk_x,
        chunk_y=chunk_y,
        chunk_z=chunk_z,
        min_y=int(getattr(world, 'min_y', -1024) or -1024),
        max_y=int(getattr(world, 'max_y', 8192) or 8192),
        surfaces=[surface_y] * (chunk_size * chunk_size),
        reference_fingerprint=reference_fingerprint,
        release_key='flat-fallback-v1',
        terrain=terrain,
    )


def _cached_content(
    payload: Mapping[str, Any],
    *,
    cache_status: str,
    stale_reason: Optional[str] = None,
) -> dict[str, Any]:
    content = deepcopy(dict(payload.get('content') or {}))
    terrain = dict(content.get('terrain') or {})
    terrain['cache'] = {
        'status': cache_status,
        'schemaVersion': CACHE_SCHEMA_VERSION,
    }
    if stale_reason:
        terrain['stale'] = True
        terrain['staleReason'] = stale_reason[:500]
    content['terrain'] = terrain
    stats = dict(content.get('stats') or {})
    stats['terrain'] = terrain
    content['stats'] = stats
    return content


def _local_to_wgs84(provider: Any, x: Decimal, z: Decimal) -> tuple[float, float]:
    converted = provider.local_to_global(
        LocalEarthPosition(x=x, y=Decimal('0'), z=z)
    )
    coordinate = converted.target_coordinate
    return float(coordinate.x), float(coordinate.y)


def _sample_axis(chunk_size: int, step_m: float) -> list[float]:
    last = float(chunk_size)
    positions = [0.0]
    while positions[-1] < last:
        candidate = min(last, positions[-1] + max(1.0, float(step_m)))
        if candidate <= positions[-1]:
            break
        positions.append(candidate)
    return positions


def _axis_bounds(axis: list[float], value: float) -> tuple[int, int, float]:
    upper = min(len(axis) - 1, bisect_left(axis, value))
    lower = max(0, upper - 1)
    if upper == lower or axis[upper] <= axis[lower]:
        return lower, upper, 0.0
    factor = (value - axis[lower]) / (axis[upper] - axis[lower])
    return lower, upper, max(0.0, min(1.0, factor))


def _interpolate_surface_values(
    *,
    chunk_size: int,
    axis: list[float],
    samples: Mapping[tuple[int, int], float],
    anchor_height: float,
    surface_y: int,
    corners: Optional[list[float]] = None,
) -> tuple[list[int], int]:
    surfaces: list[int] = []
    missing = 0
    for local_z in range(chunk_size):
        z0, z1, tz = _axis_bounds(axis, float(local_z) + 0.5)
        for local_x in range(chunk_size):
            x0, x1, tx = _axis_bounds(axis, float(local_x) + 0.5)
            values: list[float] = []
            for key in ((x0, z0), (x1, z0), (x0, z1), (x1, z1)):
                value = samples.get(key)
                if value is None:
                    missing += 1
                    value = anchor_height
                values.append(float(value))
            top = values[0] * (1.0 - tx) + values[1] * tx
            bottom = values[2] * (1.0 - tx) + values[3] * tx
            height = top * (1.0 - tz) + bottom * tz
            surfaces.append(int(round(height - anchor_height + surface_y)))
    if corners is not None:
        for local_z in range(chunk_size + 1):
            z0, z1, tz = _axis_bounds(axis, float(local_z))
            for local_x in range(chunk_size + 1):
                x0, x1, tx = _axis_bounds(axis, float(local_x))
                values = [float(samples.get(key, anchor_height))
                          for key in ((x0, z0), (x1, z0), (x0, z1), (x1, z1))]
                height = ((values[0] * (1-tx) + values[1] * tx) * (1-tz)
                          + (values[2] * (1-tx) + values[3] * tx) * tz)
                corners.append(round(height - anchor_height + surface_y + 1, 6))
    return surfaces, missing


def _region_axis_chunks(center: int, radius: int, step: int) -> list[int]:
    start = int(center) - int(radius)
    end = int(center) + int(radius)
    values = list(range(start, end + 1, max(1, int(step))))
    values.extend((start, int(center), end))
    return sorted(set(values))


def _region_cache_key(
    *,
    reference_fingerprint: str,
    dataset_id: str,
    release_key: str,
    config: TerrainPipelineConfig,
) -> dict[str, Any]:
    return {
        'reference_fingerprint': reference_fingerprint,
        'dataset_id': dataset_id,
        'release_key': release_key,
        'center_chunk_x': 0,
        'center_chunk_z': 0,
        'radius_chunks': config.region_radius_chunks,
        'sample_step_chunks': config.region_sample_step_chunks,
    }


def _terrain_serving_is_ready(version: Mapping[str, Any]) -> bool:
    serving = version.get('terrain_serving')
    # Older orchestrator versions and test clients did not expose readiness.
    # Preserve their established behavior while enforcing the gate whenever
    # the current API provides a terrain-serving state.
    if not isinstance(serving, Mapping):
        return True
    status = str(serving.get('status') or '').strip().lower()
    failed_raw = serving.get('failedFiles', serving.get('failed_files', 0))
    try:
        failed = int(failed_raw or 0)
    except (TypeError, ValueError):
        failed = 0
    return status in {'complete', 'completed', 'ready'} and failed == 0


def _resolve_project_terrain_release(
    *,
    cache: TerrainChunkCache,
    reference_fingerprint: str,
    dataset_id: str,
    target_release_key: str,
    config: TerrainPipelineConfig,
    version: Mapping[str, Any],
) -> tuple[str, Optional[dict[str, Any]], Optional[str], bool]:
    target_key = _region_cache_key(
        reference_fingerprint=reference_fingerprint,
        dataset_id=dataset_id,
        release_key=target_release_key,
        config=config,
    )
    target_region = cache.load_region(**target_key)
    serving_ready = _terrain_serving_is_ready(version)
    if target_region is not None:
        return target_release_key, target_region, None, serving_ready
    previous = cache.load_latest_region(
        reference_fingerprint=reference_fingerprint,
        dataset_id=dataset_id,
        center_chunk_x=0,
        center_chunk_z=0,
        radius_chunks=config.region_radius_chunks,
        sample_step_chunks=config.region_sample_step_chunks,
        exclude_release_key=target_release_key,
    )
    if previous is not None:
        previous_release = str(previous.get('releaseKey') or '').strip().lower()
        if previous_release:
            return previous_release, previous, target_release_key, serving_ready
    return target_release_key, None, None, serving_ready


def _with_pending_release(
    content: Mapping[str, Any],
    *,
    pending_release_key: Optional[str],
    terrain_serving: Any,
) -> dict[str, Any]:
    result = deepcopy(dict(content))
    if not pending_release_key:
        return result
    terrain = dict(result.get('terrain') or {})
    terrain['releaseSwitchPending'] = True
    terrain['pendingReleaseKey'] = pending_release_key
    if isinstance(terrain_serving, Mapping):
        terrain['pendingTerrainServing'] = dict(terrain_serving)
    result['terrain'] = terrain
    stats = dict(result.get('stats') or {})
    stats['terrain'] = terrain
    result['stats'] = stats
    return result


def _prepare_terrain_region(
    *,
    world: Any,
    provider: Any,
    reference_fingerprint: str,
    release_key: str,
    config: TerrainPipelineConfig,
    client: OrchestratorGeodataClient,
    cache: TerrainChunkCache,
) -> None:
    dataset_id = DIGITAL_ELEVATION_MODEL.dataset_id
    key = _region_cache_key(
        reference_fingerprint=reference_fingerprint,
        dataset_id=dataset_id,
        release_key=release_key,
        config=config,
    )
    if cache.load_region(**key) is not None:
        return
    chunk_size = int(getattr(world, 'chunk_size', 16) or 16)
    chunks_x = _region_axis_chunks(
        0,
        config.region_radius_chunks,
        config.region_sample_step_chunks,
    )
    chunks_z = _region_axis_chunks(
        0,
        config.region_radius_chunks,
        config.region_sample_step_chunks,
    )
    axis_world_x = [
        float(chunk_x * chunk_size) + chunk_size * 0.5
        for chunk_x in chunks_x
    ]
    axis_world_z = [
        float(chunk_z * chunk_size) + chunk_size * 0.5
        for chunk_z in chunks_z
    ]
    reference_local = provider.frame.reference_local_position
    anchor_lon, anchor_lat = _local_to_wgs84(
        provider,
        Decimal(reference_local.x),
        Decimal(reference_local.z),
    )
    requests: list[dict[str, Any]] = [{
        'id': 'anchor',
        'lon': anchor_lon,
        'lat': anchor_lat,
    }]
    for index_z, world_z in enumerate(axis_world_z):
        for index_x, world_x in enumerate(axis_world_x):
            longitude, latitude = _local_to_wgs84(
                provider,
                Decimal(str(world_x)),
                Decimal(str(world_z)),
            )
            requests.append({
                'id': f'region:{index_x}:{index_z}',
                'lon': longitude,
                'lat': latitude,
            })

    items: dict[str, dict[str, Any]] = {}
    source_partial = False
    batch_size = config.region_batch_points
    for offset in range(0, len(requests), batch_size):
        response = client.terrain_grid(
            dataset_id,
            requests[offset:offset + batch_size],
            radius_m=config.radius_m,
        )
        if str(response.get('release_key') or '').strip().lower() != release_key:
            raise TerrainSourceError(
                'Die DGM-Version aenderte sich waehrend der Regionsvorbereitung.'
            )
        source_partial = source_partial or bool(response.get('source_partial'))
        for item in response.get('items') or []:
            if isinstance(item, dict):
                items[str(item.get('id') or '')] = item

    found_values = [
        float(item['value'])
        for item in items.values()
        if item.get('found') and item.get('value') is not None
    ]
    if not found_values:
        raise TerrainSourceError(
            'Die freigegebene DGM-Version enthaelt in der Projektregion keine Hoehenwerte.'
        )
    anchor_item = items.get('anchor') or {}
    anchor_height = (
        float(anchor_item['value'])
        if anchor_item.get('found') and anchor_item.get('value') is not None
        else sorted(found_values)[len(found_values) // 2]
    )
    values: list[float] = []
    missing = 0
    for index_z in range(len(axis_world_z)):
        for index_x in range(len(axis_world_x)):
            item = items.get(f'region:{index_x}:{index_z}') or {}
            if item.get('found') and item.get('value') is not None:
                values.append(float(item['value']))
            else:
                missing += 1
                values.append(anchor_height)
    cache.store_region(
        axis_world_x=axis_world_x,
        axis_world_z=axis_world_z,
        values=values,
        anchor_elevation_m=anchor_height,
        source_partial=source_partial,
        missing_samples=missing,
        **key,
    )


def _region_job_lock_path(region_path: Path) -> Path:
    return region_path.with_name(f'{region_path.name}.preparing.lock')


def _claim_region_job(region_path: Path) -> Optional[Path]:
    lock_path = _region_job_lock_path(region_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            if attempt > 0:
                return None
            try:
                age = max(0.0, time.time() - lock_path.stat().st_mtime)
            except OSError:
                continue
            if age <= _REGION_JOB_LOCK_MAX_AGE_SECONDS:
                return None
            try:
                lock_path.unlink()
            except OSError:
                return None
            continue
        try:
            os.write(
                descriptor,
                json.dumps(
                    {'pid': os.getpid(), 'createdAtUnix': time.time()},
                    separators=(',', ':'),
                ).encode('ascii'),
            )
        finally:
            os.close(descriptor)
        return lock_path
    return None


def _schedule_terrain_region(
    *,
    world: Any,
    provider: Any,
    reference_fingerprint: str,
    release_key: str,
    config: TerrainPipelineConfig,
    client: OrchestratorGeodataClient,
    cache: TerrainChunkCache,
) -> None:
    key = _region_cache_key(
        reference_fingerprint=reference_fingerprint,
        dataset_id=DIGITAL_ELEVATION_MODEL.dataset_id,
        release_key=release_key,
        config=config,
    )
    if cache.load_region(**key) is not None:
        return
    region_path = cache._region_path(**key).resolve(strict=False)
    path = region_path.as_posix()
    with _REGION_LOCKS_GUARD:
        if path in _REGION_JOBS:
            return
    job_lock_path = _claim_region_job(region_path)
    if job_lock_path is None:
        return
    with _REGION_LOCKS_GUARD:
        _REGION_JOBS.add(path)

    def run() -> None:
        try:
            _prepare_terrain_region(
                world=world,
                provider=provider,
                reference_fingerprint=reference_fingerprint,
                release_key=release_key,
                config=config,
                client=client,
                cache=cache,
            )
        finally:
            try:
                job_lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            with _REGION_LOCKS_GUARD:
                _REGION_JOBS.discard(path)

    Thread(
        target=run,
        name=f'earth-terrain-region-{release_key[:8]}',
        daemon=True,
    ).start()


def _surfaces_from_region(
    region: Mapping[str, Any],
    *,
    chunk_size: int,
    chunk_x: int,
    chunk_z: int,
    surface_y: int,
    corners: Optional[list[float]] = None,
) -> Optional[list[int]]:
    axis_x = [float(value) for value in region.get('axisWorldX') or []]
    axis_z = [float(value) for value in region.get('axisWorldZ') or []]
    values = [float(value) for value in region.get('values') or []]
    if (
        not axis_x
        or not axis_z
        or len(values) != len(axis_x) * len(axis_z)
    ):
        return None
    minimum_x = float(chunk_x * chunk_size) + 0.5
    maximum_x = float((chunk_x + 1) * chunk_size) - 0.5
    minimum_z = float(chunk_z * chunk_size) + 0.5
    maximum_z = float((chunk_z + 1) * chunk_size) - 0.5
    if (
        maximum_x < axis_x[0]
        or minimum_x > axis_x[-1]
        or maximum_z < axis_z[0]
        or minimum_z > axis_z[-1]
    ):
        return None
    anchor_height = float(region.get('anchorElevationM') or 0.0)

    def value(index_x: int, index_z: int) -> float:
        return values[index_x + len(axis_x) * index_z]

    surfaces: list[int] = []
    for local_z in range(chunk_size):
        world_z = float(chunk_z * chunk_size + local_z) + 0.5
        z0, z1, factor_z = _axis_bounds(axis_z, world_z)
        for local_x in range(chunk_size):
            world_x = float(chunk_x * chunk_size + local_x) + 0.5
            x0, x1, factor_x = _axis_bounds(axis_x, world_x)
            top = value(x0, z0) * (1.0 - factor_x) + value(x1, z0) * factor_x
            bottom = value(x0, z1) * (1.0 - factor_x) + value(x1, z1) * factor_x
            height = top * (1.0 - factor_z) + bottom * factor_z
            surfaces.append(int(round(height - anchor_height + surface_y)))
    if corners is not None:
        for local_z in range(chunk_size + 1):
            z0, z1, tz = _axis_bounds(axis_z, float(chunk_z * chunk_size + local_z))
            for local_x in range(chunk_size + 1):
                x0, x1, tx = _axis_bounds(axis_x, float(chunk_x * chunk_size + local_x))
                height = ((value(x0, z0) * (1-tx) + value(x1, z0) * tx) * (1-tz)
                          + (value(x0, z1) * (1-tx) + value(x1, z1) * tx) * tz)
                corners.append(round(height - anchor_height + surface_y + 1, 6))
    return surfaces


def generate_earth_terrain_chunk(
    *,
    world: Any,
    provider: Any,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    config: Optional[TerrainPipelineConfig] = None,
    client: Optional[OrchestratorGeodataClient] = None,
    cache: Optional[TerrainChunkCache] = None,
) -> dict[str, Any]:
    active_config = config or get_default_terrain_config()
    reference_fingerprint = str(
        getattr(provider, 'reference_fingerprint', '')
        or getattr(world, 'global_reference_fingerprint', '')
        or 'unknown-reference'
    )
    normalized = provider.normalize_chunk_address((chunk_x, chunk_y, chunk_z))
    canonical = normalized.canonical
    chunk_x = int(canonical.x)
    chunk_y = int(canonical.y)
    chunk_z = int(canonical.z)

    if not active_config.enabled:
        return _fallback_content(
            world,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            reference_fingerprint=reference_fingerprint,
            reason='Die DGM-Terrain-Pipeline ist deaktiviert.',
        )

    active_client = client or get_default_geodata_client()
    active_cache = cache or get_default_terrain_cache()
    dataset_id = DIGITAL_ELEVATION_MODEL.dataset_id
    try:
        version = active_client.source_version(dataset_id)
        target_release_key = str(version['release_key']).strip().lower()
    except Exception as exc:
        stable_region = (
            active_cache.load_latest_region(
                reference_fingerprint=reference_fingerprint,
                dataset_id=dataset_id,
                center_chunk_x=0,
                center_chunk_z=0,
                radius_chunks=active_config.region_radius_chunks,
                sample_step_chunks=active_config.region_sample_step_chunks,
            )
            if active_config.region_enabled
            else None
        )
        stable_release_key = str(
            (stable_region or {}).get('releaseKey') or ''
        ).strip().lower()
        if stable_region is not None and stable_release_key:
            version = {
                'dataset_id': dataset_id,
                'release_key': stable_release_key,
                'terrain_serving': {
                    'status': 'source-unavailable',
                    'error': _short_error(exc),
                },
            }
            target_release_key = stable_release_key
        else:
            stale = active_cache.load_latest(
                reference_fingerprint=reference_fingerprint,
                dataset_id=dataset_id,
                chunk_x=chunk_x,
                chunk_y=chunk_y,
                chunk_z=chunk_z,
            )
            if stale is not None:
                return _cached_content(
                    stale,
                    cache_status='stale-hit',
                    stale_reason=_short_error(exc),
                )
            return _fallback_content(
                world,
                chunk_x=chunk_x,
                chunk_y=chunk_y,
                chunk_z=chunk_z,
                reference_fingerprint=reference_fingerprint,
                reason=_short_error(exc),
            )

    release_key = target_release_key
    selected_region: Optional[dict[str, Any]] = None
    pending_release_key: Optional[str] = None
    serving_ready = _terrain_serving_is_ready(version)
    if active_config.region_enabled:
        (
            release_key,
            selected_region,
            pending_release_key,
            serving_ready,
        ) = _resolve_project_terrain_release(
            cache=active_cache,
            reference_fingerprint=reference_fingerprint,
            dataset_id=dataset_id,
            target_release_key=target_release_key,
            config=active_config,
            version=version,
        )
        if serving_ready and (
            selected_region is None or release_key != target_release_key
        ):
            _schedule_terrain_region(
                world=world,
                provider=provider,
                reference_fingerprint=reference_fingerprint,
                release_key=target_release_key,
                config=active_config,
                client=active_client,
                cache=active_cache,
            )

    cache_key = {
        'reference_fingerprint': reference_fingerprint,
        'dataset_id': dataset_id,
        'release_key': release_key,
        'chunk_x': chunk_x,
        'chunk_y': chunk_y,
        'chunk_z': chunk_z,
    }
    exact = active_cache.load_exact(**cache_key)
    # The region is an overview and an offline fallback. It must not permanently
    # replace the available DGM's local relief with interpolated 32-m planes.
    can_query_detail = serving_ready and release_key == target_release_key
    if exact is not None and (
        not can_query_detail
        or float((exact.get('content', {}).get('terrain') or {}).get('sampleStepM') or math.inf) <= active_config.sample_step_m
    ):
        return _with_pending_release(
            _cached_content(exact, cache_status='hit'),
            pending_release_key=pending_release_key,
            terrain_serving=version.get('terrain_serving'),
        )

    if not serving_ready and selected_region is None:
        stale = active_cache.load_latest(
            reference_fingerprint=reference_fingerprint,
            dataset_id=dataset_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
        )
        if stale is not None:
            return _with_pending_release(
                _cached_content(
                    stale,
                    cache_status='stale-hit',
                    stale_reason='Der neue DGM-Release wird noch vorbereitet.',
                ),
                pending_release_key=target_release_key,
                terrain_serving=version.get('terrain_serving'),
            )
        return _fallback_content(
            world,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            reference_fingerprint=reference_fingerprint,
            reason='Der erste DGM-Release wird noch vorbereitet.',
        )

    chunk_size = int(getattr(world, 'chunk_size', 16) or 16)
    try:
        column_key = {
            'reference_fingerprint': reference_fingerprint,
            'dataset_id': dataset_id,
            'release_key': release_key,
            'chunk_x': chunk_x,
            'chunk_z': chunk_z,
        }
        column = active_cache.load_column(**column_key)
        if column is not None and len(column.get('surfaces') or []) == chunk_size ** 2 and (
            not can_query_detail or float((column.get('terrain') or {}).get('sampleStepM') or math.inf) <= active_config.sample_step_m
        ):
            surfaces = [int(value) for value in column['surfaces']]
            terrain = dict(column['terrain'])
            terrain['cache'] = {'status': 'miss', 'column': 'hit'}
        else:
            region = selected_region or (
                active_cache.load_region(
                    **_region_cache_key(
                        reference_fingerprint=reference_fingerprint,
                        dataset_id=dataset_id,
                        release_key=release_key,
                        config=active_config,
                    )
                )
                if active_config.region_enabled
                else None
            )
            surface_corners: list[float] = []
            region_surfaces = (
                _surfaces_from_region(
                    region,
                    chunk_size=chunk_size,
                    chunk_x=chunk_x,
                    chunk_z=chunk_z,
                    surface_y=int(getattr(world, 'surface_y', 0) or 0),
                    corners=surface_corners,
                )
                if region is not None
                else None
            )
            if region_surfaces is not None and not can_query_detail:
                terrain = {
                    'surfaceShape': {'schemaVersion': 'terrain-cut-cells.v1', 'cornerHeights': surface_corners,
                                     'sampleStepM': active_config.region_sample_step_chunks * chunk_size},
                    'status': 'dgm',
                    'fallback': False,
                    'datasetId': dataset_id,
                    'datasetName': DIGITAL_ELEVATION_MODEL.display_name,
                    'releaseKey': release_key,
                    'sourcePartial': bool(region.get('sourcePartial')),
                    'anchorElevationM': float(region.get('anchorElevationM') or 0.0),
                    'anchorPolicy': 'project-reference-dgm-height',
                    'sampleStepM': (
                        active_config.region_sample_step_chunks * chunk_size
                    ),
                    'samplePointCount': len(region.get('values') or []),
                    'missingSampleUses': int(region.get('missingSamples') or 0),
                    'queryCounts': {'source': 'project-region-cache'},
                    'cache': {
                        'status': 'miss',
                        'column': 'miss',
                        'region': 'hit',
                    },
                }
                active_cache.store_column(
                    surfaces=region_surfaces,
                    terrain={
                        key: value
                        for key, value in terrain.items()
                        if key != 'cache'
                    },
                    **column_key,
                )
                content = _generated_content(
                    chunk_size=chunk_size,
                    chunk_x=chunk_x,
                    chunk_y=chunk_y,
                    chunk_z=chunk_z,
                    min_y=int(getattr(world, 'min_y', -1024) or -1024),
                    max_y=int(getattr(world, 'max_y', 8192) or 8192),
                    surfaces=region_surfaces,
                    reference_fingerprint=reference_fingerprint,
                    release_key=release_key,
                    terrain=terrain,
                )
                active_cache.store(content=content, **cache_key)
                return _with_pending_release(
                    content,
                    pending_release_key=pending_release_key,
                    terrain_serving=version.get('terrain_serving'),
                )
            points: list[dict[str, Any]] = []
            reference_local = provider.frame.reference_local_position
            anchor_lon, anchor_lat = _local_to_wgs84(
                provider,
                Decimal(reference_local.x),
                Decimal(reference_local.z),
            )
            points.append({'id': 'anchor', 'lon': anchor_lon, 'lat': anchor_lat})
            axis = _sample_axis(chunk_size, active_config.sample_step_m)
            for sample_z, local_z in enumerate(axis):
                for sample_x, local_x in enumerate(axis):
                    world_x = Decimal(chunk_x * chunk_size) + Decimal(str(local_x))
                    world_z = Decimal(chunk_z * chunk_size) + Decimal(str(local_z))
                    longitude, latitude = _local_to_wgs84(provider, world_x, world_z)
                    points.append({
                        'id': f'sample:{sample_x}:{sample_z}',
                        'lon': longitude,
                        'lat': latitude,
                    })
            response = active_client.terrain_grid(
                dataset_id,
                points,
                radius_m=active_config.radius_m,
            )
            response_release = str(response.get('release_key') or '').strip().lower()
            if response_release != release_key:
                raise TerrainSourceError(
                    'Die DGM-Version änderte sich während der Chunk-Erzeugung.'
                )
            items = {
                str(item.get('id')): item
                for item in response.get('items') or []
                if isinstance(item, dict)
            }
            found_values = [
                float(item['value'])
                for item in items.values()
                if item.get('found') and item.get('value') is not None
            ]
            if not found_values:
                raise TerrainSourceError(
                    'Für diesen Chunk enthält der freigegebene DGM-Release keine Höhenwerte.'
                )
            anchor_item = items.get('anchor') or {}
            anchor_height = (
                float(anchor_item['value'])
                if anchor_item.get('found') and anchor_item.get('value') is not None
                else sorted(found_values)[len(found_values) // 2]
            )
            samples: dict[tuple[int, int], float] = {}
            for sample_z in range(len(axis)):
                for sample_x in range(len(axis)):
                    item = items.get(f'sample:{sample_x}:{sample_z}') or {}
                    if item.get('found') and item.get('value') is not None:
                        samples[(sample_x, sample_z)] = float(item['value'])
            surface_corners = []
            surfaces, missing = _interpolate_surface_values(
                chunk_size=chunk_size,
                axis=axis,
                samples=samples,
                anchor_height=anchor_height,
                surface_y=int(getattr(world, 'surface_y', 0) or 0),
                corners=surface_corners,
            )
            terrain = {
                'surfaceShape': {'schemaVersion': 'terrain-cut-cells.v1', 'cornerHeights': surface_corners,
                                 'sampleStepM': active_config.sample_step_m},
                'status': 'dgm',
                'fallback': False,
                'datasetId': dataset_id,
                'datasetName': DIGITAL_ELEVATION_MODEL.display_name,
                'releaseKey': release_key,
                'sourcePartial': bool(response.get('source_partial')),
                'anchorElevationM': anchor_height,
                'anchorPolicy': 'project-reference-dgm-height',
                'sampleStepM': active_config.sample_step_m,
                'samplePointCount': len(points) - 1,
                'missingSampleUses': missing,
                'queryCounts': dict(response.get('counts') or {}),
                'cache': {'status': 'miss', 'column': 'miss'},
            }
            active_cache.store_column(
                surfaces=surfaces,
                terrain={key: value for key, value in terrain.items() if key != 'cache'},
                **column_key,
            )
        content = _generated_content(
            chunk_size=chunk_size,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            min_y=int(getattr(world, 'min_y', -1024) or -1024),
            max_y=int(getattr(world, 'max_y', 8192) or 8192),
            surfaces=surfaces,
            reference_fingerprint=reference_fingerprint,
            release_key=release_key,
            terrain=terrain,
        )
        active_cache.store(content=content, **cache_key)
        return _with_pending_release(
            content,
            pending_release_key=pending_release_key,
            terrain_serving=version.get('terrain_serving'),
        )
    except Exception as exc:
        stale = active_cache.load_latest(
            reference_fingerprint=reference_fingerprint,
            dataset_id=dataset_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
        )
        if stale is not None:
            return _with_pending_release(
                _cached_content(
                    stale,
                    cache_status='stale-hit',
                    stale_reason=_short_error(exc),
                ),
                pending_release_key=pending_release_key,
                terrain_serving=version.get('terrain_serving'),
            )
        # A temporary detail query failure must not flatten an already known
        # landscape. Keep the explicitly labelled overview until detail recovers;
        # do not persist this response as a permanent replacement for 1-m data.
        if selected_region is not None:
            fallback_corners: list[float] = []
            fallback_surfaces = _surfaces_from_region(
                selected_region, chunk_size=chunk_size, chunk_x=chunk_x, chunk_z=chunk_z,
                surface_y=int(getattr(world, 'surface_y', 0) or 0), corners=fallback_corners,
            )
            if fallback_surfaces is not None:
                return _generated_content(
                    chunk_size=chunk_size, chunk_x=chunk_x, chunk_y=chunk_y, chunk_z=chunk_z,
                    min_y=int(getattr(world, 'min_y', -1024) or -1024),
                    max_y=int(getattr(world, 'max_y', 8192) or 8192), surfaces=fallback_surfaces,
                    reference_fingerprint=reference_fingerprint, release_key=release_key,
                    terrain={'status': 'dgm-region-fallback', 'fallback': True, 'releaseKey': release_key,
                             'sampleStepM': active_config.region_sample_step_chunks * chunk_size,
                             'anchorElevationM': selected_region.get('anchorElevationM'),
                             'detailError': _short_error(exc), 'surfaceShape': {
                                 'schemaVersion': 'terrain-cut-cells.v1', 'cornerHeights': fallback_corners,
                                 'sampleStepM': active_config.region_sample_step_chunks * chunk_size}},
                )
        return _fallback_content(
            world,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            reference_fingerprint=reference_fingerprint,
            reason=_short_error(exc),
        )


def get_earth_terrain_region_preview(
    *,
    world: Any,
    provider: Any,
    config: Optional[TerrainPipelineConfig] = None,
    client: Optional[OrchestratorGeodataClient] = None,
    cache: Optional[TerrainChunkCache] = None,
) -> dict[str, Any]:
    active_config = config or get_default_terrain_config()
    active_client = client or get_default_geodata_client()
    active_cache = cache or get_default_terrain_cache()
    reference_fingerprint = str(
        getattr(provider, 'reference_fingerprint', '')
        or getattr(world, 'global_reference_fingerprint', '')
        or 'unknown-reference'
    )
    dataset_id = DIGITAL_ELEVATION_MODEL.dataset_id
    if not active_config.enabled or not active_config.region_enabled:
        return {
            'schemaVersion': REGION_CACHE_SCHEMA_VERSION,
            'status': 'disabled',
            'ready': False,
            'datasetId': dataset_id,
        }
    try:
        version = active_client.source_version(dataset_id)
        target_release_key = str(version['release_key']).strip().lower()
    except Exception as exc:
        return {
            'schemaVersion': REGION_CACHE_SCHEMA_VERSION,
            'status': 'source-unavailable',
            'ready': False,
            'datasetId': dataset_id,
            'error': _short_error(exc),
        }
    (
        release_key,
        region,
        pending_release_key,
        serving_ready,
    ) = _resolve_project_terrain_release(
        cache=active_cache,
        reference_fingerprint=reference_fingerprint,
        dataset_id=dataset_id,
        target_release_key=target_release_key,
        config=active_config,
        version=version,
    )
    target_key = _region_cache_key(
        reference_fingerprint=reference_fingerprint,
        dataset_id=dataset_id,
        release_key=target_release_key,
        config=active_config,
    )
    if serving_ready and (region is None or release_key != target_release_key):
        _schedule_terrain_region(
            world=world,
            provider=provider,
            reference_fingerprint=reference_fingerprint,
            release_key=target_release_key,
            config=active_config,
            client=active_client,
            cache=active_cache,
        )
    if region is None:
        path = active_cache._region_path(**target_key).resolve(strict=False).as_posix()
        with _REGION_LOCKS_GUARD:
            active = path in _REGION_JOBS
        active = active or _region_job_lock_path(Path(path)).exists()
        return {
            'schemaVersion': REGION_CACHE_SCHEMA_VERSION,
            'status': (
                'preparing'
                if active
                else 'source-preparing'
                if not serving_ready
                else 'pending'
            ),
            'ready': False,
            'datasetId': dataset_id,
            'releaseKey': target_release_key,
            'radiusChunks': active_config.region_radius_chunks,
            'sampleStepChunks': active_config.region_sample_step_chunks,
            'terrainServing': version.get('terrain_serving'),
        }
    anchor = float(region.get('anchorElevationM') or 0.0)
    return {
        'schemaVersion': REGION_CACHE_SCHEMA_VERSION,
        'status': 'ready',
        'ready': True,
        'datasetId': dataset_id,
        'releaseKey': release_key,
        'referenceFingerprint': reference_fingerprint,
        'radiusChunks': int(region.get('radiusChunks') or 0),
        'sampleStepChunks': int(region.get('sampleStepChunks') or 1),
        'axisWorldX': [float(value) for value in region.get('axisWorldX') or []],
        'axisWorldZ': [float(value) for value in region.get('axisWorldZ') or []],
        'heights': [
            round(float(value) - anchor, 2)
            for value in region.get('values') or []
        ],
        'anchorElevationM': anchor,
        'sourcePartial': bool(region.get('sourcePartial')),
        'missingSamples': int(region.get('missingSamples') or 0),
        'terrainServing': version.get('terrain_serving'),
        'releaseSwitchPending': bool(pending_release_key),
        'pendingReleaseKey': pending_release_key,
    }


@lru_cache(maxsize=1)
def get_default_terrain_config() -> TerrainPipelineConfig:
    return TerrainPipelineConfig.from_env()


@lru_cache(maxsize=1)
def get_default_geodata_client() -> OrchestratorGeodataClient:
    return OrchestratorGeodataClient(get_default_terrain_config())


@lru_cache(maxsize=1)
def get_default_terrain_cache() -> TerrainChunkCache:
    return TerrainChunkCache(get_default_terrain_config().cache_root)


def clear_terrain_pipeline_caches() -> None:
    get_default_geodata_client.cache_clear()
    get_default_terrain_cache.cache_clear()
    get_default_terrain_config.cache_clear()


__all__ = (
    'CACHE_SCHEMA_VERSION',
    'OrchestratorGeodataClient',
    'TERRAIN_PIPELINE_VERSION',
    'TerrainChunkCache',
    'TerrainPipelineConfig',
    'TerrainSourceError',
    'apply_earth_surface_shell',
    'clear_terrain_pipeline_caches',
    'generate_earth_terrain_chunk',
    'get_earth_terrain_region_preview',
)
