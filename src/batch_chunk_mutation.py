"""Reuse mutable chunk inputs between children of one atomic ObjectBatch.

Per-child events retain their revisions and hashes. Already identified snapshot
rows are detached while children execute, so their large JSON is persisted once
at the end rather than by every unrelated ORM query/autoflush. New snapshots
receive their database id first so event foreign keys remain valid.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar('object_batch_chunk_inputs', default=None)


class _Batch:
    def __init__(self, session):
        self.session = session
        self.chunks = {}
        self.snapshots = {}
        self.refs = {}
        self.projections = {}


def batch_is_active():
    return _current.get() is not None


@contextmanager
def batch_chunk_mutations(session=None):
    state = _Batch(session)
    token = _current.set(state)
    try:
        yield
        if session is not None:
            session.add_all(state.snapshots.values())
    finally:
        _current.reset(token)


def defer_snapshot(snapshot):
    state = _current.get()
    if state is None or state.session is None:
        return False
    if snapshot in state.session:
        state.session.expunge(snapshot)
    state.snapshots[snapshot.id] = snapshot
    return True


def chunk_refs(world_id, chunk, load):
    state = _current.get()
    if state is None:
        return load()
    key = (world_id, *chunk)
    if key not in state.refs:
        state.refs[key] = load()
    return state.refs[key]


def remember_ref(ref):
    state = _current.get()
    if state is None:
        return
    key = (ref.world_db_id, ref.chunk_x, ref.chunk_y, ref.chunk_z)
    refs = state.refs.get(key)
    if refs is not None and ref not in refs:
        refs.append(ref)


def cached_projection(ref, chunk, chunk_size):
    state = _current.get()
    if state is None:
        return None
    entry = state.projections.get((id(ref), *chunk, chunk_size))
    return entry[1] if entry is not None and entry[0] is ref else None


def remember_projection(ref, projected, chunk, chunk_size):
    state = _current.get()
    if state is not None:
        state.projections[(id(ref), *chunk, chunk_size)] = (ref, projected)
        state.projections[(id(projected), *chunk, chunk_size)] = (projected, projected)


def cached_chunk(world_id, x, y, z):
    state = _current.get()
    return state.chunks.get((world_id, x, y, z)) if state is not None else None


def remember_chunk(world_id, snapshot, content):
    state = _current.get()
    if state is None:
        return
    # Same routing/version fields that _runtime_content_from_snapshot supplies.
    # Other content, including the validated mutable cell buffer, remains usable.
    if snapshot is not None:
        content = {**content, 'source': 'snapshot', 'snapshotId': snapshot.snapshot_id,
                   'chunkRevision': snapshot.chunk_revision, 'chunkVersion': snapshot.chunk_version,
                   'contentHash': snapshot.content_hash}
    state.chunks[(world_id, int(content['chunkX']), int(content['chunkY']), int(content['chunkZ']))] = (snapshot, content)
