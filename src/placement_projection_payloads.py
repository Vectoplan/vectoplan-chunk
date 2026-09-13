"""Read semantic placement inputs without transferring full command histories."""
from extensions import db
from models.event import ChunkEvent, WorldCommandLog
from sqlalchemy import and_, func

# All top-level inputs consumed by routes.chunks._compact_placement_semantics.
# Nested metadata is retained intact (including exact CAD/roof definitions).
SEMANTIC_PAYLOAD_FIELDS = (
    "metadata", "libraryPlacementContext", "semanticProfile",
    "runtimeBlockTypeId", "blockTypeId", "source",
)


def _columns(column):
    return [and_(func.jsonb_typeof(column) == "object", column != {}).label("payload_present"), *[
        column[field].label("projection_" + field) for field in SEMANTIC_PAYLOAD_FIELDS
    ]]


def _payload(row):
    values = {field: getattr(row, "projection_" + field)
              for field in SEMANTIC_PAYLOAD_FIELDS
              if getattr(row, "projection_" + field) is not None}
    # Preserve _first_mapping(event, command) precedence when an event contains
    # only audit fields; an actually empty event still falls back to its command.
    if row.payload_present:
        values["_projectionPayloadPresent"] = True
    return values


def page_projection_payloads(event_ids):
    result, commands = {}, {}
    ids = sorted(set(int(value) for value in event_ids))
    for offset in range(0, len(ids), 500):
        rows = db.session.query(
            ChunkEvent.id, ChunkEvent.command_log_db_id,
            ChunkEvent.object_footprint_json, *_columns(ChunkEvent.payload_json),
        ).filter(ChunkEvent.id.in_(ids[offset:offset + 500])).all()
        missing_command_ids = sorted({row.command_log_db_id for row in rows
                                      if row.command_log_db_id and row.command_log_db_id not in commands})
        if missing_command_ids:
            command_rows = db.session.query(
                WorldCommandLog.id, *_columns(WorldCommandLog.request_payload_json),
            ).filter(WorldCommandLog.id.in_(missing_command_ids)).all()
            commands.update({row.id: _payload(row) for row in command_rows})
        result.update({row.id: (_payload(row), commands.get(row.command_log_db_id, {}),
                               row.object_footprint_json) for row in rows})
    return result
