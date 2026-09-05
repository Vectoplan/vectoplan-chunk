"""Derive the new terrain surface for legacy snapshots with complete edit history.

This never writes a snapshot on GET. Read and mutation adapters use the same
derived content; the next ordinary save persists it atomically with that command.
Missing or truncated provenance preserves the entire old snapshot.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
import json
from threading import RLock
from typing import Any, Mapping, Sequence

_HISTORY_CACHE: OrderedDict[tuple[Any, ...], frozenset[int] | None] = OrderedDict()
_HISTORY_LOCK = RLock()


def protected_legacy_cells(*, revision: int, last_command_id: str, chunk: tuple[int, int, int],
                           size: int, logs: Sequence[Mapping[str, Any]],
                           verified_metadata_revisions: bool = False) -> frozenset[int] | None:
    """Require one complete applied command record for every saved revision."""
    if revision < 1 or (len(logs) != revision and not verified_metadata_revisions) or last_command_id not in {row.get('id') for row in logs}:
        return None
    protected: set[int] = set()
    for row in logs:
        cells = row.get('cells')
        if not isinstance(cells, list) or len(cells) != row.get('count'):
            return None
        for cell in cells:
            if not isinstance(cell, Mapping) or not all(key in cell for key in ('x', 'y', 'z')):
                return None
            try:
                x, y, z = (int(cell[key]) for key in ('x', 'y', 'z'))
            except (ValueError, TypeError):
                return None
            if (x // size, y // size, z // size) == chunk:
                protected.add(x % size + size * (y % size + size * (z % size)))
    return frozenset(protected)


def verify_unchanged_revision_gaps(*, revision: int, content_hash: str,
                                  command_ids: set[str], events: Sequence[Mapping[str, Any]]) -> bool:
    """Prove that revisions without cell commands did not alter saved content.

    LoD2 apply_facade_metadata_repairs bumps the snapshot revision after changing
    only separate object reference metadata. It deliberately leaves content_hash
    unchanged. Compare the surrounding persisted event hashes, never merely
    tolerate one missing command or assume a particular writer ran.
    """
    if not events or not content_hash:
        return False
    seen_commands: set[str] = set()
    cursor = 0
    previous_hash: str | None = None
    ordered = sorted(events, key=lambda event: (int(event.get('after') or 0), int(event.get('before') or 0)))
    seen_transitions: set[tuple[Any, ...]] = set()
    for event in ordered:
        before, after = int(event.get('before') or 0), int(event.get('after') or 0)
        command_id = str(event.get('id') or '')
        transition = (before, after, command_id, event.get('beforeHash'), event.get('afterHash'))
        if transition in seen_transitions:
            continue
        seen_transitions.add(transition)
        if command_id not in command_ids or after != before + 1 or before < cursor or after > revision:
            return False
        if before > cursor and (not previous_hash or event.get('beforeHash') != previous_hash):
            return False
        cursor = after
        previous_hash = event.get('afterHash')
        seen_commands.add(command_id)
    return (seen_commands == command_ids and cursor <= revision
            and bool(previous_hash) and previous_hash == content_hash)


def merge_untouched_terrain(content: Mapping[str, Any], generated: Mapping[str, Any],
                            protected: frozenset[int]) -> dict[str, Any]:
    """Replace only never-edited air/terrain; retain every other cell and object."""
    shape = (generated.get('metadata') or {}).get('terrainSurface')
    cells = content.get('cells')
    generated_cells = generated.get('cells')
    if not isinstance(shape, Mapping) or not isinstance(cells, list) or not isinstance(generated_cells, list) or len(cells) != len(generated_cells):
        return dict(content)
    result = deepcopy(dict(content))
    size = int(content.get('chunkSize') or 16)
    chunk = tuple(int(content.get(key) or 0) for key in ('chunkX', 'chunkY', 'chunkZ'))
    protected = set(protected)
    protected.update(((content.get('metadata') or {}).get('terrainSurface') or {}).get('fullCellIndices') or [])
    for ref in content.get('objectRefs') or []:
        if not isinstance(ref, Mapping):
            continue
        for cell in ref.get('occupiedCells') or []:
            if not isinstance(cell, Mapping) or not all(key in cell for key in ('x', 'y', 'z')):
                continue
            x, y, z = (int(cell[key]) for key in ('x', 'y', 'z'))
            if (x // size, y // size, z // size) == chunk:
                protected.add(x % size + size * (y % size + size * (z % size)))
    palette = result.get('palette') or []
    source_palette = generated.get('palette') or []
    def block_id(entry: Any) -> str:
        return str(entry.get('blockTypeId') or '') if isinstance(entry, Mapping) else str(entry)
    palette_ids = [block_id(entry) for entry in palette]
    mapped: dict[int, int] = {0: 0}
    for value, entry in enumerate(source_palette, 1):
        identifier = block_id(entry)
        if identifier not in palette_ids:
            palette_ids.append(identifier)
            palette.append({'blockTypeId': identifier, 'solid': True, 'breakable': True, 'placeable': True})
        mapped[value] = palette_ids.index(identifier) + 1
    for index, old_value in enumerate(cells):
        if index in protected or old_value < 0:
            continue
        identifier = palette_ids[old_value - 1] if old_value > 0 and old_value <= len(palette_ids) else ''
        if old_value != 0 and not identifier.startswith('system_terrain'):
            continue
        result['cells'][index] = mapped.get(generated_cells[index], old_value)
    result['palette'] = palette
    result['metadata'] = {**result.get('metadata', {}), 'terrainSurface': {
        **deepcopy(dict(shape)), 'fullCellIndices': sorted(protected),
        'legacyUpgrade': {'policy': 'complete-command-history-only', 'protectedCellCount': len(protected),
                          'releaseKey': generated.get('terrain', {}).get('releaseKey')},
    }}
    result['terrain'] = deepcopy(generated.get('terrain') or {})
    result['chunkVersion'] = f"{content.get('chunkVersion') or content.get('chunkRevision')}:terrain-cut:{generated.get('contentHash', '')[:16]}"
    non_air = sum(value != 0 for value in result['cells'])
    result['stats'] = {**result.get('stats', {}), 'nonAirCellCount': non_air,
                       'airCellCount': len(cells) - non_air, 'cellCount': len(cells),
                       'terrain': deepcopy(generated.get('terrain') or {}),
                       'minimumSurfaceY': generated.get('stats', {}).get('minimumSurfaceY'),
                       'maximumSurfaceY': generated.get('stats', {}).get('maximumSurfaceY')}
    result['contentHash'] = sha256(json.dumps({key: result.get(key) for key in ('cells', 'palette', 'metadata', 'objectRefs')},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return result


def upgrade_legacy_terrain_snapshot(*, snapshot: Any, content: dict[str, Any], world: Any) -> dict[str, Any]:
    from .terrain_pipeline import generate_earth_terrain_chunk, get_default_terrain_config
    shape = (content.get('metadata') or {}).get('terrainSurface')
    if shape:
        terrain = content.get('terrain') or (content.get('stats') or {}).get('terrain') or {}
        resolution = shape.get('sampleStepM') or terrain.get('sampleStepM')
        # Previously upgraded 32-m snapshots still need the real 1-m surface.
        # The same complete edit history protects every user-modified cell.
        if isinstance(resolution, (int, float)) and resolution <= get_default_terrain_config().sample_step_m:
            return content
    if str(getattr(world, 'template_id', '')).lower() != 'earth' and str(getattr(world, 'provider_world_id', '')).lower() != 'earth':
        return content
    if getattr(snapshot, 'snapshot_source', None) != 'command' or not getattr(snapshot, 'last_command_id', None):
        return content
    from extensions import db
    from models.event import WorldCommandLog, ChunkEvent
    from sqlalchemy import Text, cast
    size = int(world.chunk_size)
    coordinates = (int(snapshot.chunk_x), int(snapshot.chunk_y), int(snapshot.chunk_z))
    key = (world.id, *coordinates, snapshot.chunk_revision, snapshot.last_command_id)
    try:
        with _HISTORY_LOCK:
            cached = key in _HISTORY_CACHE
            protected = _HISTORY_CACHE.get(key)
        if not cached:
            chunk_key = ':'.join(str(value) for value in coordinates)
            rows = db.session.query(WorldCommandLog.command_id, WorldCommandLog.affected_cells_json,
                                    WorldCommandLog.affected_cell_count).filter(
                WorldCommandLog.world_db_id == world.id, WorldCommandLog.changed.is_(True),
                cast(WorldCommandLog.affected_chunks_json, Text).contains(f'"{chunk_key}"'),
            ).all()
            verified_metadata_revisions = False
            if len(rows) != int(snapshot.chunk_revision):
                events = db.session.query(ChunkEvent.command_id, ChunkEvent.chunk_revision_before,
                    ChunkEvent.chunk_revision_after, ChunkEvent.content_hash_before, ChunkEvent.content_hash_after).filter(
                        ChunkEvent.world_db_id == world.id, ChunkEvent.chunk_x == coordinates[0],
                        ChunkEvent.chunk_y == coordinates[1], ChunkEvent.chunk_z == coordinates[2],
                        ChunkEvent.chunk_revision_after <= int(snapshot.chunk_revision),
                    ).all()
                verified_metadata_revisions = verify_unchanged_revision_gaps(
                    revision=int(snapshot.chunk_revision), content_hash=snapshot.content_hash,
                    command_ids={row.command_id for row in rows},
                    events=[{'id':row.command_id,'before':row.chunk_revision_before,'after':row.chunk_revision_after,
                             'beforeHash':row.content_hash_before,'afterHash':row.content_hash_after} for row in events],
                )
            protected = protected_legacy_cells(revision=int(snapshot.chunk_revision), last_command_id=snapshot.last_command_id,
                chunk=coordinates, size=size, logs=[{'id':row.command_id,'cells':row.affected_cells_json,'count':row.affected_cell_count} for row in rows],
                verified_metadata_revisions=verified_metadata_revisions)
            with _HISTORY_LOCK:
                _HISTORY_CACHE[key] = protected
                while len(_HISTORY_CACHE) > 512:
                    _HISTORY_CACHE.popitem(last=False)
        if protected is None:
            return content
        generated = generate_earth_terrain_chunk(world=world, provider=world.build_earth_provider(),
            chunk_x=coordinates[0], chunk_y=coordinates[1], chunk_z=coordinates[2])
        return merge_untouched_terrain(content, generated, protected)
    except Exception:
        # Terrain availability must never make an existing user snapshot unloadable.
        return content
