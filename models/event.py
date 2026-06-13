# services/vectoplan-chunk/models/event.py
"""
SQLAlchemy models for VECTOPLAN world commands and chunk events.

This file stores two related but intentionally separate concepts:

1. WorldCommandLog
   One user/system intent. A command can affect one cell, many cells, one chunk,
   many chunks, or later one multi-block object.

2. ChunkEvent
   Append-only per-chunk historical event produced by a confirmed command.

Important design rules:
- ChunkSnapshot is the current load-truth.
- ChunkEvent is historical truth.
- Events are not the normal chunk-load path.
- WorldCommandLog groups all effects of one command.
- One command can produce multiple ChunkEvents.
- This already prepares multi-block objects such as 4x4x2 or 2x1x2 objects.
- This file does not perform commits.
- Repository/service layers are responsible for database transactions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4


try:
    from extensions import db
except Exception as exc:  # pragma: no cover - import failure should be explicit at app startup
    db = None  # type: ignore[assignment]
    _DB_IMPORT_ERROR = exc
else:
    _DB_IMPORT_ERROR = None


if db is None:  # pragma: no cover
    raise RuntimeError(
        "Could not import `db` from `extensions` while loading "
        "models/event.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
        "`db` instance before importing models."
    ) from _DB_IMPORT_ERROR


try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - fallback is useful for tests/non-postgres tooling
    JSONB = None  # type: ignore[assignment]


JSON_COLUMN_TYPE = JSONB if JSONB is not None else db.JSON


WORLD_COMMAND_LOG_SCHEMA_VERSION = "world-command-log.schema.v1"
CHUNK_EVENT_SCHEMA_VERSION = "chunk-event.schema.v1"

COMMAND_TYPE_SET_BLOCK = "SetBlock"
COMMAND_TYPE_REMOVE_BLOCK = "RemoveBlock"
COMMAND_TYPE_REPLACE_BLOCK = "ReplaceBlock"
COMMAND_TYPE_APPLY_BLOCK_BATCH = "ApplyBlockBatch"
COMMAND_TYPE_PLACE_OBJECT = "PlaceObject"
COMMAND_TYPE_REMOVE_OBJECT = "RemoveObject"
COMMAND_TYPE_REPLACE_OBJECT = "ReplaceObject"
COMMAND_TYPE_FILL_REGION = "FillRegion"
COMMAND_TYPE_CLEAR_REGION = "ClearRegion"
COMMAND_TYPE_REPLACE_REGION = "ReplaceRegion"
COMMAND_TYPE_IMPORT = "Import"
COMMAND_TYPE_SYSTEM = "System"

VALID_COMMAND_TYPES = frozenset(
    {
        COMMAND_TYPE_SET_BLOCK,
        COMMAND_TYPE_REMOVE_BLOCK,
        COMMAND_TYPE_REPLACE_BLOCK,
        COMMAND_TYPE_APPLY_BLOCK_BATCH,
        COMMAND_TYPE_PLACE_OBJECT,
        COMMAND_TYPE_REMOVE_OBJECT,
        COMMAND_TYPE_REPLACE_OBJECT,
        COMMAND_TYPE_FILL_REGION,
        COMMAND_TYPE_CLEAR_REGION,
        COMMAND_TYPE_REPLACE_REGION,
        COMMAND_TYPE_IMPORT,
        COMMAND_TYPE_SYSTEM,
    }
)

COMMAND_STATUS_RECEIVED = "received"
COMMAND_STATUS_APPLIED = "applied"
COMMAND_STATUS_NOOP = "noop"
COMMAND_STATUS_REJECTED = "rejected"
COMMAND_STATUS_FAILED = "failed"
COMMAND_STATUS_COMPENSATED = "compensated"

VALID_COMMAND_STATUSES = frozenset(
    {
        COMMAND_STATUS_RECEIVED,
        COMMAND_STATUS_APPLIED,
        COMMAND_STATUS_NOOP,
        COMMAND_STATUS_REJECTED,
        COMMAND_STATUS_FAILED,
        COMMAND_STATUS_COMPENSATED,
    }
)

COMMAND_SOURCE_EDITOR = "editor"
COMMAND_SOURCE_SYSTEM = "system"
COMMAND_SOURCE_IMPORTER = "importer"
COMMAND_SOURCE_AI = "ai"
COMMAND_SOURCE_TEST = "test"
COMMAND_SOURCE_UNKNOWN = "unknown"

VALID_COMMAND_SOURCES = frozenset(
    {
        COMMAND_SOURCE_EDITOR,
        COMMAND_SOURCE_SYSTEM,
        COMMAND_SOURCE_IMPORTER,
        COMMAND_SOURCE_AI,
        COMMAND_SOURCE_TEST,
        COMMAND_SOURCE_UNKNOWN,
    }
)

EVENT_TYPE_BLOCK_CHANGE = "block_change"
EVENT_TYPE_OBJECT_CHANGE = "object_change"
EVENT_TYPE_REGION_CHANGE = "region_change"
EVENT_TYPE_IMPORT_CHANGE = "import_change"
EVENT_TYPE_SYSTEM_CHANGE = "system_change"

VALID_EVENT_TYPES = frozenset(
    {
        EVENT_TYPE_BLOCK_CHANGE,
        EVENT_TYPE_OBJECT_CHANGE,
        EVENT_TYPE_REGION_CHANGE,
        EVENT_TYPE_IMPORT_CHANGE,
        EVENT_TYPE_SYSTEM_CHANGE,
    }
)

EVENT_STATUS_ACTIVE = "active"
EVENT_STATUS_SUPERSEDED = "superseded"
EVENT_STATUS_COMPENSATED = "compensated"

VALID_EVENT_STATUSES = frozenset(
    {
        EVENT_STATUS_ACTIVE,
        EVENT_STATUS_SUPERSEDED,
        EVENT_STATUS_COMPENSATED,
    }
)

COMMAND_ID_MAX_LENGTH = 128
EVENT_ID_MAX_LENGTH = 128
SCHEMA_VERSION_MAX_LENGTH = 64
COMMAND_TYPE_MAX_LENGTH = 96
COMMAND_STATUS_MAX_LENGTH = 64
COMMAND_SOURCE_MAX_LENGTH = 64
EVENT_TYPE_MAX_LENGTH = 96
EVENT_STATUS_MAX_LENGTH = 64

USER_ID_MAX_LENGTH = 128
SESSION_ID_MAX_LENGTH = 128
REQUEST_ID_MAX_LENGTH = 128
TRACE_ID_MAX_LENGTH = 128
CLIENT_ID_MAX_LENGTH = 128

CHUNK_KEY_MAX_LENGTH = 96
CHUNK_VERSION_MAX_LENGTH = 64

BLOCK_TYPE_ID_MAX_LENGTH = 160
TOOL_MAX_LENGTH = 96
TARGET_FACE_MAX_LENGTH = 32

OBJECT_INSTANCE_ID_MAX_LENGTH = 160
OBJECT_TYPE_ID_MAX_LENGTH = 160
OBJECT_VARIANT_ID_MAX_LENGTH = 160

CONTENT_HASH_MAX_LENGTH = 128
ERROR_CODE_MAX_LENGTH = 128

PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
CHUNK_KEY_PATTERN = re.compile(r"^-?\d+:-?\d+:-?\d+$")
HASH_PATTERN = re.compile(r"^[A-Za-z0-9_.:+/-]+$")


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def datetime_to_iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize datetime values safely for API responses."""
    if value is None:
        return None

    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(value)


def make_json_safe(value: Any) -> Any:
    """Convert arbitrary values to JSON-safe structures."""
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, datetime):
        return datetime_to_iso(value)

    if isinstance(value, Mapping):
        safe_dict: Dict[str, Any] = {}
        for key, item in value.items():
            try:
                safe_key = str(key)
            except Exception:
                safe_key = "<unserializable-key>"
            safe_dict[safe_key] = make_json_safe(item)
        return safe_dict

    if isinstance(value, (list, tuple, set, frozenset)):
        return [make_json_safe(item) for item in value]

    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "size": len(value),
        }

    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


def stable_json_dumps(value: Any) -> str:
    """Serialize JSON values in a stable way."""
    return json.dumps(
        make_json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def normalize_optional_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    """Normalize optional text values."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if len(text) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters.")

    return text


def normalize_required_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """Normalize required text values."""
    text = normalize_optional_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if text is None:
        raise ValueError(f"{field_name} is required.")

    return text


def normalize_public_id(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """
    Normalize public/system identifiers.

    Allowed:
    - letters
    - numbers
    - underscore
    - dash
    - dot
    - colon
    """
    text = normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if not PUBLIC_ID_PATTERN.match(text):
        raise ValueError(
            f"{field_name} may only contain letters, numbers, underscores, "
            "dashes, dots and colons, and must start with a letter or number."
        )

    return text


def normalize_optional_public_id(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    """Normalize optional public/system identifiers."""
    text = normalize_optional_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if text is None:
        return None

    return normalize_public_id(
        text,
        field_name=field_name,
        max_length=max_length,
    )


def generate_command_id(prefix: str = "cmd") -> str:
    """Generate a stable public command id."""
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="command_id_prefix",
        max_length=32,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def generate_event_id(prefix: str = "evt") -> str:
    """Generate a stable public event id."""
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="event_id_prefix",
        max_length=32,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def normalize_command_id(value: Any) -> str:
    """Normalize command_id."""
    return normalize_public_id(
        value,
        field_name="command_id",
        max_length=COMMAND_ID_MAX_LENGTH,
    )


def normalize_event_id(value: Any) -> str:
    """Normalize event_id."""
    return normalize_public_id(
        value,
        field_name="event_id",
        max_length=EVENT_ID_MAX_LENGTH,
    )


def normalize_db_id(value: Any, *, field_name: str) -> int:
    """Normalize internal database ids."""
    if value is None:
        raise ValueError(f"{field_name} is required.")

    try:
        db_id = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if db_id <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    return db_id


def normalize_optional_db_id(value: Any, *, field_name: str) -> Optional[int]:
    """Normalize optional internal database ids."""
    if value is None:
        return None

    return normalize_db_id(value, field_name=field_name)


def normalize_int(value: Any, *, field_name: str) -> int:
    """Normalize required integer values."""
    if value is None:
        raise ValueError(f"{field_name} is required.")

    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def normalize_optional_int(value: Any, *, field_name: str) -> Optional[int]:
    """Normalize optional integer values."""
    if value is None:
        return None

    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def normalize_non_negative_int(
    value: Any,
    *,
    field_name: str,
    default: int = 0,
) -> int:
    """Normalize non-negative integer values."""
    if value is None:
        value = default

    try:
        result = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if result < 0:
        raise ValueError(f"{field_name} must be greater than or equal to zero.")

    return result


def normalize_optional_float(value: Any, *, field_name: str) -> Optional[float]:
    """Normalize optional float values."""
    if value is None:
        return None

    try:
        return float(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number.") from exc


def normalize_bool(value: Any, *, field_name: str, default: bool = False) -> bool:
    """Normalize booleans from API/config payloads."""
    if value is None:
        return bool(default)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False

    raise ValueError(f"{field_name} must be a boolean.")


def normalize_json_object(value: Any, *, field_name: str) -> Dict[str, Any]:
    """Normalize a JSON object field."""
    if value is None:
        return {}

    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object/dict.")

    return make_json_safe(dict(value))


def normalize_json_list(value: Any, *, field_name: str) -> List[Any]:
    """Normalize a JSON list field."""
    if value is None:
        return []

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field_name} must be a JSON list/array.")

    return make_json_safe(list(value))


def normalize_command_type(value: Any) -> str:
    """Normalize and validate command type."""
    command_type = normalize_required_text(
        value,
        field_name="command_type",
        max_length=COMMAND_TYPE_MAX_LENGTH,
    )

    if command_type not in VALID_COMMAND_TYPES:
        allowed = ", ".join(sorted(VALID_COMMAND_TYPES))
        raise ValueError(f"Invalid command type '{value}'. Allowed: {allowed}.")

    return command_type


def normalize_command_status(value: Any) -> str:
    """Normalize and validate command status."""
    if value is None:
        return COMMAND_STATUS_RECEIVED

    status = str(value).strip().lower()

    if status not in VALID_COMMAND_STATUSES:
        allowed = ", ".join(sorted(VALID_COMMAND_STATUSES))
        raise ValueError(f"Invalid command status '{value}'. Allowed: {allowed}.")

    return status


def normalize_command_source(value: Any) -> str:
    """Normalize and validate command source."""
    if value is None:
        return COMMAND_SOURCE_EDITOR

    source = str(value).strip().lower()

    if source not in VALID_COMMAND_SOURCES:
        allowed = ", ".join(sorted(VALID_COMMAND_SOURCES))
        raise ValueError(f"Invalid command source '{value}'. Allowed: {allowed}.")

    return source


def normalize_event_type(value: Any) -> str:
    """Normalize and validate event type."""
    if value is None:
        return EVENT_TYPE_BLOCK_CHANGE

    event_type = str(value).strip().lower()

    if event_type not in VALID_EVENT_TYPES:
        allowed = ", ".join(sorted(VALID_EVENT_TYPES))
        raise ValueError(f"Invalid event type '{value}'. Allowed: {allowed}.")

    return event_type


def normalize_event_status(value: Any) -> str:
    """Normalize and validate event status."""
    if value is None:
        return EVENT_STATUS_ACTIVE

    status = str(value).strip().lower()

    if status not in VALID_EVENT_STATUSES:
        allowed = ", ".join(sorted(VALID_EVENT_STATUSES))
        raise ValueError(f"Invalid event status '{value}'. Allowed: {allowed}.")

    return status


def normalize_chunk_coord(value: Any, *, field_name: str) -> int:
    """Normalize chunk coordinates."""
    return normalize_int(value, field_name=field_name)


def build_chunk_key(chunk_x: int, chunk_y: int, chunk_z: int) -> str:
    """Build canonical chunk key."""
    return f"{int(chunk_x)}:{int(chunk_y)}:{int(chunk_z)}"


def normalize_chunk_key(value: Any) -> str:
    """Normalize chunk_key."""
    text = normalize_required_text(
        value,
        field_name="chunk_key",
        max_length=CHUNK_KEY_MAX_LENGTH,
    )

    if not CHUNK_KEY_PATTERN.match(text):
        raise ValueError(
            "chunk_key must use the canonical format '<chunkX>:<chunkY>:<chunkZ>'."
        )

    return text


def parse_chunk_key(chunk_key: str) -> Tuple[int, int, int]:
    """Parse a canonical chunk key into coordinates."""
    normalized_key = normalize_chunk_key(chunk_key)

    try:
        raw_x, raw_y, raw_z = normalized_key.split(":")
        return int(raw_x), int(raw_y), int(raw_z)
    except Exception as exc:
        raise ValueError(f"Invalid chunk_key '{chunk_key}'.") from exc


def assert_chunk_key_matches(
    *,
    chunk_key: str,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
) -> None:
    """Validate that chunk key matches coordinates."""
    expected = build_chunk_key(chunk_x, chunk_y, chunk_z)
    if normalize_chunk_key(chunk_key) != expected:
        raise ValueError(
            f"chunk_key '{chunk_key}' does not match coordinates. "
            f"Expected '{expected}'."
        )


def normalize_optional_block_type_id(value: Any, *, field_name: str) -> Optional[str]:
    """Normalize optional block type ids."""
    return normalize_optional_public_id(
        value,
        field_name=field_name,
        max_length=BLOCK_TYPE_ID_MAX_LENGTH,
    )


def normalize_optional_hash(value: Any, *, field_name: str) -> Optional[str]:
    """Normalize optional content hashes."""
    text = normalize_optional_text(
        value,
        field_name=field_name,
        max_length=CONTENT_HASH_MAX_LENGTH,
    )

    if text is None:
        return None

    if not HASH_PATTERN.match(text):
        raise ValueError(f"{field_name} contains unsupported characters.")

    return text


def derive_event_type_from_command_type(command_type: str) -> str:
    """Infer event_type from command_type."""
    normalized = normalize_command_type(command_type)

    if normalized in {
        COMMAND_TYPE_SET_BLOCK,
        COMMAND_TYPE_REMOVE_BLOCK,
        COMMAND_TYPE_REPLACE_BLOCK,
        COMMAND_TYPE_APPLY_BLOCK_BATCH,
    }:
        return EVENT_TYPE_BLOCK_CHANGE

    if normalized in {
        COMMAND_TYPE_PLACE_OBJECT,
        COMMAND_TYPE_REMOVE_OBJECT,
        COMMAND_TYPE_REPLACE_OBJECT,
    }:
        return EVENT_TYPE_OBJECT_CHANGE

    if normalized in {
        COMMAND_TYPE_FILL_REGION,
        COMMAND_TYPE_CLEAR_REGION,
        COMMAND_TYPE_REPLACE_REGION,
    }:
        return EVENT_TYPE_REGION_CHANGE

    if normalized == COMMAND_TYPE_IMPORT:
        return EVENT_TYPE_IMPORT_CHANGE

    return EVENT_TYPE_SYSTEM_CHANGE


def count_json_list(value: Any) -> int:
    """Best-effort length for JSON list-like values."""
    try:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return len(value)
    except Exception:
        return 0
    return 0


def normalize_bounds_json(value: Any, *, field_name: str) -> Dict[str, Any]:
    """
    Normalize bounds JSON.

    Expected shape is flexible, but preferred:
        {
          "min": {"x": ..., "y": ..., "z": ...},
          "max": {"x": ..., "y": ..., "z": ...}
        }
    """
    return normalize_json_object(value, field_name=field_name)


class WorldCommandLog(db.Model):
    """
    Persistent command log entry.

    One row represents one intent/request. It can result in:
    - no changes
    - one ChunkEvent
    - many ChunkEvents
    - later object instance changes across many chunks
    """

    __tablename__ = "world_command_logs"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    command_id = db.Column(
        db.String(COMMAND_ID_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
    )

    project_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    universe_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("universes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    world_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("world_instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    command_type = db.Column(
        db.String(COMMAND_TYPE_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    command_status = db.Column(
        db.String(COMMAND_STATUS_MAX_LENGTH),
        nullable=False,
        default=COMMAND_STATUS_RECEIVED,
        index=True,
    )

    command_source = db.Column(
        db.String(COMMAND_SOURCE_MAX_LENGTH),
        nullable=False,
        default=COMMAND_SOURCE_EDITOR,
        index=True,
    )

    schema_version = db.Column(
        db.String(SCHEMA_VERSION_MAX_LENGTH),
        nullable=False,
        default=WORLD_COMMAND_LOG_SCHEMA_VERSION,
    )

    user_id = db.Column(
        db.String(USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    session_id = db.Column(
        db.String(SESSION_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    request_id = db.Column(
        db.String(REQUEST_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    trace_id = db.Column(
        db.String(TRACE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    client_id = db.Column(
        db.String(CLIENT_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    anchor_x = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    anchor_y = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    anchor_z = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    object_instance_id = db.Column(
        db.String(OBJECT_INSTANCE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    object_type_id = db.Column(
        db.String(OBJECT_TYPE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    object_variant_id = db.Column(
        db.String(OBJECT_VARIANT_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    object_size_x = db.Column(
        db.Integer,
        nullable=True,
    )

    object_size_y = db.Column(
        db.Integer,
        nullable=True,
    )

    object_size_z = db.Column(
        db.Integer,
        nullable=True,
    )

    object_rotation_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    affected_bounds_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    affected_chunks_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    affected_cells_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    changed = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        index=True,
    )

    affected_chunk_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    affected_cell_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    event_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    request_payload_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    result_payload_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    validation_errors_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    error_code = db.Column(
        db.String(ERROR_CODE_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    error_message = db.Column(
        db.Text,
        nullable=True,
    )

    metadata_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    applied_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    failed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    project = db.relationship(
        "Project",
        backref=db.backref(
            "world_command_logs",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    universe = db.relationship(
        "Universe",
        backref=db.backref(
            "world_command_logs",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    world = db.relationship(
        "WorldInstance",
        backref=db.backref(
            "world_command_logs",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.CheckConstraint(
            "project_db_id > 0",
            name="ck_world_command_logs_project_db_id_positive",
        ),
        db.CheckConstraint(
            "universe_db_id > 0",
            name="ck_world_command_logs_universe_db_id_positive",
        ),
        db.CheckConstraint(
            "world_db_id > 0",
            name="ck_world_command_logs_world_db_id_positive",
        ),
        db.CheckConstraint(
            "command_id <> ''",
            name="ck_world_command_logs_command_id_not_empty",
        ),
        db.CheckConstraint(
            "command_type IN ('SetBlock', 'RemoveBlock', 'ReplaceBlock', 'ApplyBlockBatch', 'PlaceObject', 'RemoveObject', 'ReplaceObject', 'FillRegion', 'ClearRegion', 'ReplaceRegion', 'Import', 'System')",
            name="ck_world_command_logs_command_type_valid",
        ),
        db.CheckConstraint(
            "command_status IN ('received', 'applied', 'noop', 'rejected', 'failed', 'compensated')",
            name="ck_world_command_logs_command_status_valid",
        ),
        db.CheckConstraint(
            "command_source IN ('editor', 'system', 'importer', 'ai', 'test', 'unknown')",
            name="ck_world_command_logs_command_source_valid",
        ),
        db.CheckConstraint(
            "affected_chunk_count >= 0",
            name="ck_world_command_logs_affected_chunk_count_non_negative",
        ),
        db.CheckConstraint(
            "affected_cell_count >= 0",
            name="ck_world_command_logs_affected_cell_count_non_negative",
        ),
        db.CheckConstraint(
            "event_count >= 0",
            name="ck_world_command_logs_event_count_non_negative",
        ),
        db.CheckConstraint(
            "object_size_x IS NULL OR object_size_x > 0",
            name="ck_world_command_logs_object_size_x_positive",
        ),
        db.CheckConstraint(
            "object_size_y IS NULL OR object_size_y > 0",
            name="ck_world_command_logs_object_size_y_positive",
        ),
        db.CheckConstraint(
            "object_size_z IS NULL OR object_size_z > 0",
            name="ck_world_command_logs_object_size_z_positive",
        ),
        db.Index(
            "ix_world_command_logs_world_created",
            "world_db_id",
            "created_at",
        ),
        db.Index(
            "ix_world_command_logs_world_type_status",
            "world_db_id",
            "command_type",
            "command_status",
        ),
        db.Index(
            "ix_world_command_logs_user_session",
            "user_id",
            "session_id",
        ),
        db.Index(
            "ix_world_command_logs_object",
            "world_db_id",
            "object_instance_id",
        ),
        db.Index(
            "ix_world_command_logs_request_trace",
            "request_id",
            "trace_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<WorldCommandLog id={self.id!r} command_id={self.command_id!r} "
            f"command_type={self.command_type!r} status={self.command_status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: int,
        universe_db_id: int,
        world_db_id: int,
        command_type: str,
        command_id: Optional[str] = None,
        command_status: str = COMMAND_STATUS_RECEIVED,
        command_source: str = COMMAND_SOURCE_EDITOR,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        client_id: Optional[str] = None,
        anchor_x: Optional[int] = None,
        anchor_y: Optional[int] = None,
        anchor_z: Optional[int] = None,
        object_instance_id: Optional[str] = None,
        object_type_id: Optional[str] = None,
        object_variant_id: Optional[str] = None,
        object_size_x: Optional[int] = None,
        object_size_y: Optional[int] = None,
        object_size_z: Optional[int] = None,
        object_rotation_json: Optional[Mapping[str, Any]] = None,
        affected_bounds_json: Optional[Mapping[str, Any]] = None,
        affected_chunks_json: Optional[Sequence[Any]] = None,
        affected_cells_json: Optional[Sequence[Any]] = None,
        changed: bool = False,
        affected_chunk_count: Optional[int] = None,
        affected_cell_count: Optional[int] = None,
        event_count: int = 0,
        request_payload_json: Optional[Mapping[str, Any]] = None,
        result_payload_json: Optional[Mapping[str, Any]] = None,
        validation_errors_json: Optional[Sequence[Any]] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "WorldCommandLog":
        """Create a WorldCommandLog instance without adding it to a session."""
        normalized_affected_chunks = normalize_json_list(
            affected_chunks_json,
            field_name="affected_chunks_json",
        )
        normalized_affected_cells = normalize_json_list(
            affected_cells_json,
            field_name="affected_cells_json",
        )

        resolved_affected_chunk_count = normalize_non_negative_int(
            affected_chunk_count
            if affected_chunk_count is not None
            else count_json_list(normalized_affected_chunks),
            field_name="affected_chunk_count",
            default=0,
        )

        resolved_affected_cell_count = normalize_non_negative_int(
            affected_cell_count
            if affected_cell_count is not None
            else count_json_list(normalized_affected_cells),
            field_name="affected_cell_count",
            default=0,
        )

        normalized_status = normalize_command_status(command_status)
        now = utc_now()

        return cls(
            command_id=normalize_command_id(command_id or generate_command_id()),
            project_db_id=normalize_db_id(project_db_id, field_name="project_db_id"),
            universe_db_id=normalize_db_id(universe_db_id, field_name="universe_db_id"),
            world_db_id=normalize_db_id(world_db_id, field_name="world_db_id"),
            command_type=normalize_command_type(command_type),
            command_status=normalized_status,
            command_source=normalize_command_source(command_source),
            schema_version=WORLD_COMMAND_LOG_SCHEMA_VERSION,
            user_id=normalize_optional_text(
                user_id,
                field_name="user_id",
                max_length=USER_ID_MAX_LENGTH,
            ),
            session_id=normalize_optional_text(
                session_id,
                field_name="session_id",
                max_length=SESSION_ID_MAX_LENGTH,
            ),
            request_id=normalize_optional_public_id(
                request_id,
                field_name="request_id",
                max_length=REQUEST_ID_MAX_LENGTH,
            ),
            trace_id=normalize_optional_public_id(
                trace_id,
                field_name="trace_id",
                max_length=TRACE_ID_MAX_LENGTH,
            ),
            client_id=normalize_optional_text(
                client_id,
                field_name="client_id",
                max_length=CLIENT_ID_MAX_LENGTH,
            ),
            anchor_x=normalize_optional_int(anchor_x, field_name="anchor_x"),
            anchor_y=normalize_optional_int(anchor_y, field_name="anchor_y"),
            anchor_z=normalize_optional_int(anchor_z, field_name="anchor_z"),
            object_instance_id=normalize_optional_public_id(
                object_instance_id,
                field_name="object_instance_id",
                max_length=OBJECT_INSTANCE_ID_MAX_LENGTH,
            ),
            object_type_id=normalize_optional_public_id(
                object_type_id,
                field_name="object_type_id",
                max_length=OBJECT_TYPE_ID_MAX_LENGTH,
            ),
            object_variant_id=normalize_optional_public_id(
                object_variant_id,
                field_name="object_variant_id",
                max_length=OBJECT_VARIANT_ID_MAX_LENGTH,
            ),
            object_size_x=normalize_optional_int(object_size_x, field_name="object_size_x"),
            object_size_y=normalize_optional_int(object_size_y, field_name="object_size_y"),
            object_size_z=normalize_optional_int(object_size_z, field_name="object_size_z"),
            object_rotation_json=normalize_json_object(
                object_rotation_json,
                field_name="object_rotation_json",
            ),
            affected_bounds_json=normalize_bounds_json(
                affected_bounds_json,
                field_name="affected_bounds_json",
            ),
            affected_chunks_json=normalized_affected_chunks,
            affected_cells_json=normalized_affected_cells,
            changed=normalize_bool(changed, field_name="changed", default=False),
            affected_chunk_count=resolved_affected_chunk_count,
            affected_cell_count=resolved_affected_cell_count,
            event_count=normalize_non_negative_int(
                event_count,
                field_name="event_count",
                default=0,
            ),
            request_payload_json=normalize_json_object(
                request_payload_json,
                field_name="request_payload_json",
            ),
            result_payload_json=normalize_json_object(
                result_payload_json,
                field_name="result_payload_json",
            ),
            validation_errors_json=normalize_json_list(
                validation_errors_json,
                field_name="validation_errors_json",
            ),
            error_code=normalize_optional_text(
                error_code,
                field_name="error_code",
                max_length=ERROR_CODE_MAX_LENGTH,
            ),
            error_message=normalize_optional_text(
                error_message,
                field_name="error_message",
                max_length=4096,
            ),
            metadata_json=normalize_json_object(metadata_json, field_name="metadata_json"),
            created_at=now,
            applied_at=now if normalized_status in {COMMAND_STATUS_APPLIED, COMMAND_STATUS_NOOP} else None,
            failed_at=now if normalized_status in {COMMAND_STATUS_REJECTED, COMMAND_STATUS_FAILED} else None,
        )

    @classmethod
    def create_for_world(
        cls,
        world: Any,
        *,
        command_type: str,
        **kwargs: Any,
    ) -> "WorldCommandLog":
        """Create command log for a persisted WorldInstance."""
        project_db_id = getattr(world, "project_db_id", None)
        universe_db_id = getattr(world, "universe_db_id", None)
        world_db_id = getattr(world, "id", None)

        if project_db_id is None:
            raise ValueError("Cannot create command log without world.project_db_id.")
        if universe_db_id is None:
            raise ValueError("Cannot create command log without world.universe_db_id.")
        if world_db_id is None:
            raise ValueError("Cannot create command log without persisted world.id.")

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_db_id=world_db_id,
            command_type=command_type,
            **kwargs,
        )

    @classmethod
    def from_command_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        project_db_id: int,
        universe_db_id: int,
        world_db_id: int,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        command_source: str = COMMAND_SOURCE_EDITOR,
    ) -> "WorldCommandLog":
        """Create command log from an API-style command payload."""
        if not isinstance(payload, Mapping):
            raise ValueError("Command payload must be a JSON object.")

        position = payload.get("position") if isinstance(payload.get("position"), Mapping) else {}
        anchor = payload.get("anchor") if isinstance(payload.get("anchor"), Mapping) else position
        object_info = payload.get("object") if isinstance(payload.get("object"), Mapping) else {}
        dimensions = object_info.get("dimensions") if isinstance(object_info.get("dimensions"), Mapping) else {}

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_db_id=world_db_id,
            command_type=payload.get("type") or payload.get("commandType") or payload.get("command_type"),
            command_id=payload.get("commandId") or payload.get("command_id"),
            command_status=COMMAND_STATUS_RECEIVED,
            command_source=command_source,
            user_id=payload.get("userId") or payload.get("user_id") or user_id,
            session_id=payload.get("sessionId") or payload.get("session_id") or session_id,
            request_id=payload.get("requestId") or payload.get("request_id"),
            trace_id=payload.get("traceId") or payload.get("trace_id"),
            client_id=payload.get("clientId") or payload.get("client_id"),
            anchor_x=anchor.get("x"),
            anchor_y=anchor.get("y"),
            anchor_z=anchor.get("z"),
            object_instance_id=(
                payload.get("objectInstanceId")
                or payload.get("object_instance_id")
                or object_info.get("objectInstanceId")
                or object_info.get("object_instance_id")
            ),
            object_type_id=(
                payload.get("objectTypeId")
                or payload.get("object_type_id")
                or object_info.get("objectTypeId")
                or object_info.get("object_type_id")
            ),
            object_variant_id=(
                payload.get("objectVariantId")
                or payload.get("object_variant_id")
                or object_info.get("variantId")
                or object_info.get("variant_id")
            ),
            object_size_x=dimensions.get("x") or dimensions.get("width") or payload.get("objectSizeX"),
            object_size_y=dimensions.get("y") or dimensions.get("height") or payload.get("objectSizeY"),
            object_size_z=dimensions.get("z") or dimensions.get("depth") or payload.get("objectSizeZ"),
            object_rotation_json=object_info.get("rotation") if isinstance(object_info.get("rotation"), Mapping) else None,
            request_payload_json=payload,
        )

    @property
    def is_applied(self) -> bool:
        return self.command_status == COMMAND_STATUS_APPLIED

    @property
    def is_noop(self) -> bool:
        return self.command_status == COMMAND_STATUS_NOOP

    @property
    def is_failed(self) -> bool:
        return self.command_status in {COMMAND_STATUS_REJECTED, COMMAND_STATUS_FAILED}

    @property
    def project_public_id(self) -> Optional[str]:
        try:
            return getattr(getattr(self, "project", None), "project_id", None)
        except Exception:
            return None

    @property
    def universe_public_id(self) -> Optional[str]:
        try:
            return getattr(getattr(self, "universe", None), "universe_id", None)
        except Exception:
            return None

    @property
    def world_public_id(self) -> Optional[str]:
        try:
            return getattr(getattr(self, "world", None), "world_id", None)
        except Exception:
            return None

    @property
    def anchor_position(self) -> Optional[Dict[str, int]]:
        if self.anchor_x is None or self.anchor_y is None or self.anchor_z is None:
            return None
        return {
            "x": int(self.anchor_x),
            "y": int(self.anchor_y),
            "z": int(self.anchor_z),
        }

    @property
    def object_dimensions(self) -> Optional[Dict[str, int]]:
        if self.object_size_x is None or self.object_size_y is None or self.object_size_z is None:
            return None
        return {
            "x": int(self.object_size_x),
            "y": int(self.object_size_y),
            "z": int(self.object_size_z),
        }

    def mark_applied(
        self,
        *,
        changed: bool,
        affected_chunks_json: Optional[Sequence[Any]] = None,
        affected_cells_json: Optional[Sequence[Any]] = None,
        affected_bounds_json: Optional[Mapping[str, Any]] = None,
        event_count: Optional[int] = None,
        result_payload_json: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Mark command as applied or noop."""
        affected_chunks = normalize_json_list(
            affected_chunks_json if affected_chunks_json is not None else self.affected_chunks_json,
            field_name="affected_chunks_json",
        )
        affected_cells = normalize_json_list(
            affected_cells_json if affected_cells_json is not None else self.affected_cells_json,
            field_name="affected_cells_json",
        )

        self.command_status = COMMAND_STATUS_APPLIED if changed else COMMAND_STATUS_NOOP
        self.changed = normalize_bool(changed, field_name="changed", default=False)
        self.affected_chunks_json = affected_chunks
        self.affected_cells_json = affected_cells
        self.affected_chunk_count = count_json_list(affected_chunks)
        self.affected_cell_count = count_json_list(affected_cells)

        if affected_bounds_json is not None:
            self.affected_bounds_json = normalize_bounds_json(
                affected_bounds_json,
                field_name="affected_bounds_json",
            )

        if event_count is not None:
            self.event_count = normalize_non_negative_int(
                event_count,
                field_name="event_count",
                default=0,
            )

        if result_payload_json is not None:
            self.result_payload_json = normalize_json_object(
                result_payload_json,
                field_name="result_payload_json",
            )

        self.applied_at = utc_now()
        self.failed_at = None
        self.error_code = None
        self.error_message = None
        self.validation_errors_json = []

    def mark_rejected(
        self,
        *,
        validation_errors_json: Optional[Sequence[Any]] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        result_payload_json: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Mark command as rejected during validation."""
        self.command_status = COMMAND_STATUS_REJECTED
        self.changed = False
        self.validation_errors_json = normalize_json_list(
            validation_errors_json,
            field_name="validation_errors_json",
        )
        self.error_code = normalize_optional_text(
            error_code,
            field_name="error_code",
            max_length=ERROR_CODE_MAX_LENGTH,
        )
        self.error_message = normalize_optional_text(
            error_message,
            field_name="error_message",
            max_length=4096,
        )
        if result_payload_json is not None:
            self.result_payload_json = normalize_json_object(
                result_payload_json,
                field_name="result_payload_json",
            )
        self.failed_at = utc_now()

    def mark_failed(
        self,
        *,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        result_payload_json: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Mark command as failed during execution."""
        self.command_status = COMMAND_STATUS_FAILED
        self.changed = False
        self.error_code = normalize_optional_text(
            error_code,
            field_name="error_code",
            max_length=ERROR_CODE_MAX_LENGTH,
        )
        self.error_message = normalize_optional_text(
            error_message,
            field_name="error_message",
            max_length=4096,
        )
        if result_payload_json is not None:
            self.result_payload_json = normalize_json_object(
                result_payload_json,
                field_name="result_payload_json",
            )
        self.failed_at = utc_now()

    def increment_event_count(self, amount: int = 1) -> None:
        """Increment event_count defensively."""
        self.event_count = normalize_non_negative_int(
            int(self.event_count or 0) + int(amount),
            field_name="event_count",
            default=0,
        )

    def get_validation_errors(self) -> Dict[str, str]:
        """Return validation errors without raising."""
        errors: Dict[str, str] = {}

        for attr, field_name in (
            ("project_db_id", "projectDbId"),
            ("universe_db_id", "universeDbId"),
            ("world_db_id", "worldDbId"),
        ):
            try:
                normalize_db_id(getattr(self, attr), field_name=attr)
            except Exception as exc:
                errors[field_name] = str(exc)

        try:
            normalize_command_id(self.command_id)
        except Exception as exc:
            errors["commandId"] = str(exc)

        try:
            normalize_command_type(self.command_type)
        except Exception as exc:
            errors["commandType"] = str(exc)

        try:
            normalize_command_status(self.command_status)
        except Exception as exc:
            errors["commandStatus"] = str(exc)

        try:
            normalize_command_source(self.command_source)
        except Exception as exc:
            errors["commandSource"] = str(exc)

        try:
            normalize_non_negative_int(
                self.affected_chunk_count,
                field_name="affected_chunk_count",
                default=0,
            )
        except Exception as exc:
            errors["affectedChunkCount"] = str(exc)

        try:
            normalize_non_negative_int(
                self.affected_cell_count,
                field_name="affected_cell_count",
                default=0,
            )
        except Exception as exc:
            errors["affectedCellCount"] = str(exc)

        try:
            normalize_non_negative_int(
                self.event_count,
                field_name="event_count",
                default=0,
            )
        except Exception as exc:
            errors["eventCount"] = str(exc)

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_payloads: bool = True,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
        world_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serialize command log for API/service responses."""
        result: Dict[str, Any] = {
            "commandId": self.command_id,
            "projectId": project_id if project_id is not None else self.project_public_id,
            "universeId": universe_id if universe_id is not None else self.universe_public_id,
            "worldId": world_id if world_id is not None else self.world_public_id,
            "commandType": self.command_type,
            "commandStatus": self.command_status,
            "commandSource": self.command_source,
            "schemaVersion": self.schema_version,
            "userId": self.user_id,
            "sessionId": self.session_id,
            "requestId": self.request_id,
            "traceId": self.trace_id,
            "clientId": self.client_id,
            "anchor": self.anchor_position,
            "object": {
                "objectInstanceId": self.object_instance_id,
                "objectTypeId": self.object_type_id,
                "objectVariantId": self.object_variant_id,
                "dimensions": self.object_dimensions,
                "rotation": normalize_json_object(
                    self.object_rotation_json,
                    field_name="object_rotation_json",
                ),
            },
            "changed": self.changed,
            "affectedChunkCount": self.affected_chunk_count,
            "affectedCellCount": self.affected_cell_count,
            "eventCount": self.event_count,
            "affectedBounds": normalize_json_object(
                self.affected_bounds_json,
                field_name="affected_bounds_json",
            ),
            "affectedChunks": normalize_json_list(
                self.affected_chunks_json,
                field_name="affected_chunks_json",
            ),
            "affectedCells": normalize_json_list(
                self.affected_cells_json,
                field_name="affected_cells_json",
            ),
            "validationErrors": normalize_json_list(
                self.validation_errors_json,
                field_name="validation_errors_json",
            ),
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "metadata": normalize_json_object(self.metadata_json, field_name="metadata_json"),
            "createdAt": datetime_to_iso(self.created_at),
            "appliedAt": datetime_to_iso(self.applied_at),
            "failedAt": datetime_to_iso(self.failed_at),
            "flags": {
                "applied": self.is_applied,
                "noop": self.is_noop,
                "failed": self.is_failed,
            },
        }

        if include_payloads:
            result["requestPayload"] = normalize_json_object(
                self.request_payload_json,
                field_name="request_payload_json",
            )
            result["resultPayload"] = normalize_json_object(
                self.result_payload_json,
                field_name="result_payload_json",
            )

        if include_internal:
            result["id"] = self.id
            result["projectDbId"] = self.project_db_id
            result["universeDbId"] = self.universe_db_id
            result["worldDbId"] = self.world_db_id

        return result


class ChunkEvent(db.Model):
    """
    Append-only chunk event.

    A ChunkEvent describes how one chunk changed because of one command.
    One command can produce many ChunkEvents.

    The first block slice will usually create one event per SetBlock/RemoveBlock.
    Later multi-block objects can create one command with events across multiple
    chunks.
    """

    __tablename__ = "chunk_events"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    event_id = db.Column(
        db.String(EVENT_ID_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
    )

    command_log_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("world_command_logs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    command_id = db.Column(
        db.String(COMMAND_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    project_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    universe_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("universes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    world_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("world_instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    chunk_snapshot_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("chunk_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    event_type = db.Column(
        db.String(EVENT_TYPE_MAX_LENGTH),
        nullable=False,
        default=EVENT_TYPE_BLOCK_CHANGE,
        index=True,
    )

    event_status = db.Column(
        db.String(EVENT_STATUS_MAX_LENGTH),
        nullable=False,
        default=EVENT_STATUS_ACTIVE,
        index=True,
    )

    event_schema_version = db.Column(
        db.String(SCHEMA_VERSION_MAX_LENGTH),
        nullable=False,
        default=CHUNK_EVENT_SCHEMA_VERSION,
    )

    command_type = db.Column(
        db.String(COMMAND_TYPE_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    user_id = db.Column(
        db.String(USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    session_id = db.Column(
        db.String(SESSION_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    chunk_x = db.Column(
        db.BigInteger,
        nullable=False,
        index=True,
    )

    chunk_y = db.Column(
        db.BigInteger,
        nullable=False,
        index=True,
    )

    chunk_z = db.Column(
        db.BigInteger,
        nullable=False,
        index=True,
    )

    chunk_key = db.Column(
        db.String(CHUNK_KEY_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    position_x = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    position_y = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    position_z = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    local_x = db.Column(
        db.Integer,
        nullable=True,
    )

    local_y = db.Column(
        db.Integer,
        nullable=True,
    )

    local_z = db.Column(
        db.Integer,
        nullable=True,
    )

    block_before_type_id = db.Column(
        db.String(BLOCK_TYPE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    block_after_type_id = db.Column(
        db.String(BLOCK_TYPE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    cell_before_value = db.Column(
        db.Integer,
        nullable=True,
        index=True,
    )

    cell_after_value = db.Column(
        db.Integer,
        nullable=True,
        index=True,
    )

    target_face = db.Column(
        db.String(TARGET_FACE_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    tool = db.Column(
        db.String(TOOL_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    chunk_revision_before = db.Column(
        db.Integer,
        nullable=True,
    )

    chunk_revision_after = db.Column(
        db.Integer,
        nullable=True,
    )

    chunk_version_before = db.Column(
        db.String(CHUNK_VERSION_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    chunk_version_after = db.Column(
        db.String(CHUNK_VERSION_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    content_hash_before = db.Column(
        db.String(CONTENT_HASH_MAX_LENGTH),
        nullable=True,
    )

    content_hash_after = db.Column(
        db.String(CONTENT_HASH_MAX_LENGTH),
        nullable=True,
    )

    object_instance_id = db.Column(
        db.String(OBJECT_INSTANCE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    object_type_id = db.Column(
        db.String(OBJECT_TYPE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    object_variant_id = db.Column(
        db.String(OBJECT_VARIANT_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    object_footprint_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    affected_bounds_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    affected_cells_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    affected_cell_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    dirty_chunks_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    dirty_chunk_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    payload_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    metadata_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )

    command_log = db.relationship(
        "WorldCommandLog",
        backref=db.backref(
            "chunk_events",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    project = db.relationship(
        "Project",
        backref=db.backref(
            "chunk_events",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    universe = db.relationship(
        "Universe",
        backref=db.backref(
            "chunk_events",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    world = db.relationship(
        "WorldInstance",
        backref=db.backref(
            "chunk_events",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    chunk_snapshot = db.relationship(
        "ChunkSnapshot",
        backref=db.backref(
            "chunk_events",
            lazy="selectin",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.CheckConstraint(
            "project_db_id > 0",
            name="ck_chunk_events_project_db_id_positive",
        ),
        db.CheckConstraint(
            "universe_db_id > 0",
            name="ck_chunk_events_universe_db_id_positive",
        ),
        db.CheckConstraint(
            "world_db_id > 0",
            name="ck_chunk_events_world_db_id_positive",
        ),
        db.CheckConstraint(
            "event_id <> ''",
            name="ck_chunk_events_event_id_not_empty",
        ),
        db.CheckConstraint(
            "command_id <> ''",
            name="ck_chunk_events_command_id_not_empty",
        ),
        db.CheckConstraint(
            "event_type IN ('block_change', 'object_change', 'region_change', 'import_change', 'system_change')",
            name="ck_chunk_events_event_type_valid",
        ),
        db.CheckConstraint(
            "event_status IN ('active', 'superseded', 'compensated')",
            name="ck_chunk_events_event_status_valid",
        ),
        db.CheckConstraint(
            "command_type IN ('SetBlock', 'RemoveBlock', 'ReplaceBlock', 'ApplyBlockBatch', 'PlaceObject', 'RemoveObject', 'ReplaceObject', 'FillRegion', 'ClearRegion', 'ReplaceRegion', 'Import', 'System')",
            name="ck_chunk_events_command_type_valid",
        ),
        db.CheckConstraint(
            "chunk_key <> ''",
            name="ck_chunk_events_chunk_key_not_empty",
        ),
        db.CheckConstraint(
            "affected_cell_count >= 0",
            name="ck_chunk_events_affected_cell_count_non_negative",
        ),
        db.CheckConstraint(
            "dirty_chunk_count >= 0",
            name="ck_chunk_events_dirty_chunk_count_non_negative",
        ),
        db.Index(
            "ix_chunk_events_world_created",
            "world_db_id",
            "created_at",
        ),
        db.Index(
            "ix_chunk_events_world_chunk_created",
            "world_db_id",
            "chunk_key",
            "created_at",
        ),
        db.Index(
            "ix_chunk_events_command",
            "command_id",
            "event_id",
        ),
        db.Index(
            "ix_chunk_events_user_session",
            "user_id",
            "session_id",
        ),
        db.Index(
            "ix_chunk_events_block_transition",
            "block_before_type_id",
            "block_after_type_id",
        ),
        db.Index(
            "ix_chunk_events_object",
            "world_db_id",
            "object_instance_id",
        ),
        db.Index(
            "ix_chunk_events_position",
            "world_db_id",
            "position_x",
            "position_y",
            "position_z",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ChunkEvent id={self.id!r} event_id={self.event_id!r} "
            f"command_id={self.command_id!r} chunk_key={self.chunk_key!r} "
            f"event_type={self.event_type!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: int,
        universe_db_id: int,
        world_db_id: int,
        command_id: str,
        command_type: str,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        event_id: Optional[str] = None,
        command_log_db_id: Optional[int] = None,
        chunk_snapshot_db_id: Optional[int] = None,
        event_type: Optional[str] = None,
        event_status: str = EVENT_STATUS_ACTIVE,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        position_x: Optional[int] = None,
        position_y: Optional[int] = None,
        position_z: Optional[int] = None,
        local_x: Optional[int] = None,
        local_y: Optional[int] = None,
        local_z: Optional[int] = None,
        block_before_type_id: Optional[str] = None,
        block_after_type_id: Optional[str] = None,
        cell_before_value: Optional[int] = None,
        cell_after_value: Optional[int] = None,
        target_face: Optional[str] = None,
        tool: Optional[str] = None,
        chunk_revision_before: Optional[int] = None,
        chunk_revision_after: Optional[int] = None,
        chunk_version_before: Optional[str] = None,
        chunk_version_after: Optional[str] = None,
        content_hash_before: Optional[str] = None,
        content_hash_after: Optional[str] = None,
        object_instance_id: Optional[str] = None,
        object_type_id: Optional[str] = None,
        object_variant_id: Optional[str] = None,
        object_footprint_json: Optional[Mapping[str, Any]] = None,
        affected_bounds_json: Optional[Mapping[str, Any]] = None,
        affected_cells_json: Optional[Sequence[Any]] = None,
        dirty_chunks_json: Optional[Sequence[Any]] = None,
        payload_json: Optional[Mapping[str, Any]] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "ChunkEvent":
        """Create a ChunkEvent instance without adding it to a session."""
        normalized_chunk_x = normalize_chunk_coord(chunk_x, field_name="chunk_x")
        normalized_chunk_y = normalize_chunk_coord(chunk_y, field_name="chunk_y")
        normalized_chunk_z = normalize_chunk_coord(chunk_z, field_name="chunk_z")
        chunk_key = build_chunk_key(
            normalized_chunk_x,
            normalized_chunk_y,
            normalized_chunk_z,
        )

        normalized_command_type = normalize_command_type(command_type)
        affected_cells = normalize_json_list(
            affected_cells_json,
            field_name="affected_cells_json",
        )
        dirty_chunks = normalize_json_list(
            dirty_chunks_json,
            field_name="dirty_chunks_json",
        )

        return cls(
            event_id=normalize_event_id(event_id or generate_event_id()),
            command_log_db_id=normalize_optional_db_id(
                command_log_db_id,
                field_name="command_log_db_id",
            ),
            command_id=normalize_command_id(command_id),
            project_db_id=normalize_db_id(project_db_id, field_name="project_db_id"),
            universe_db_id=normalize_db_id(universe_db_id, field_name="universe_db_id"),
            world_db_id=normalize_db_id(world_db_id, field_name="world_db_id"),
            chunk_snapshot_db_id=normalize_optional_db_id(
                chunk_snapshot_db_id,
                field_name="chunk_snapshot_db_id",
            ),
            event_type=normalize_event_type(
                event_type or derive_event_type_from_command_type(normalized_command_type)
            ),
            event_status=normalize_event_status(event_status),
            event_schema_version=CHUNK_EVENT_SCHEMA_VERSION,
            command_type=normalized_command_type,
            user_id=normalize_optional_text(
                user_id,
                field_name="user_id",
                max_length=USER_ID_MAX_LENGTH,
            ),
            session_id=normalize_optional_text(
                session_id,
                field_name="session_id",
                max_length=SESSION_ID_MAX_LENGTH,
            ),
            chunk_x=normalized_chunk_x,
            chunk_y=normalized_chunk_y,
            chunk_z=normalized_chunk_z,
            chunk_key=chunk_key,
            position_x=normalize_optional_int(position_x, field_name="position_x"),
            position_y=normalize_optional_int(position_y, field_name="position_y"),
            position_z=normalize_optional_int(position_z, field_name="position_z"),
            local_x=normalize_optional_int(local_x, field_name="local_x"),
            local_y=normalize_optional_int(local_y, field_name="local_y"),
            local_z=normalize_optional_int(local_z, field_name="local_z"),
            block_before_type_id=normalize_optional_block_type_id(
                block_before_type_id,
                field_name="block_before_type_id",
            ),
            block_after_type_id=normalize_optional_block_type_id(
                block_after_type_id,
                field_name="block_after_type_id",
            ),
            cell_before_value=normalize_optional_int(
                cell_before_value,
                field_name="cell_before_value",
            ),
            cell_after_value=normalize_optional_int(
                cell_after_value,
                field_name="cell_after_value",
            ),
            target_face=normalize_optional_text(
                target_face,
                field_name="target_face",
                max_length=TARGET_FACE_MAX_LENGTH,
            ),
            tool=normalize_optional_text(
                tool,
                field_name="tool",
                max_length=TOOL_MAX_LENGTH,
            ),
            chunk_revision_before=normalize_optional_int(
                chunk_revision_before,
                field_name="chunk_revision_before",
            ),
            chunk_revision_after=normalize_optional_int(
                chunk_revision_after,
                field_name="chunk_revision_after",
            ),
            chunk_version_before=normalize_optional_text(
                chunk_version_before,
                field_name="chunk_version_before",
                max_length=CHUNK_VERSION_MAX_LENGTH,
            ),
            chunk_version_after=normalize_optional_text(
                chunk_version_after,
                field_name="chunk_version_after",
                max_length=CHUNK_VERSION_MAX_LENGTH,
            ),
            content_hash_before=normalize_optional_hash(
                content_hash_before,
                field_name="content_hash_before",
            ),
            content_hash_after=normalize_optional_hash(
                content_hash_after,
                field_name="content_hash_after",
            ),
            object_instance_id=normalize_optional_public_id(
                object_instance_id,
                field_name="object_instance_id",
                max_length=OBJECT_INSTANCE_ID_MAX_LENGTH,
            ),
            object_type_id=normalize_optional_public_id(
                object_type_id,
                field_name="object_type_id",
                max_length=OBJECT_TYPE_ID_MAX_LENGTH,
            ),
            object_variant_id=normalize_optional_public_id(
                object_variant_id,
                field_name="object_variant_id",
                max_length=OBJECT_VARIANT_ID_MAX_LENGTH,
            ),
            object_footprint_json=normalize_json_object(
                object_footprint_json,
                field_name="object_footprint_json",
            ),
            affected_bounds_json=normalize_bounds_json(
                affected_bounds_json,
                field_name="affected_bounds_json",
            ),
            affected_cells_json=affected_cells,
            affected_cell_count=count_json_list(affected_cells),
            dirty_chunks_json=dirty_chunks,
            dirty_chunk_count=count_json_list(dirty_chunks),
            payload_json=normalize_json_object(payload_json, field_name="payload_json"),
            metadata_json=normalize_json_object(metadata_json, field_name="metadata_json"),
            created_at=utc_now(),
        )

    @classmethod
    def create_for_command(
        cls,
        command_log: WorldCommandLog,
        *,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        chunk_snapshot_db_id: Optional[int] = None,
        **kwargs: Any,
    ) -> "ChunkEvent":
        """Create event from a WorldCommandLog instance."""
        return cls.create(
            project_db_id=command_log.project_db_id,
            universe_db_id=command_log.universe_db_id,
            world_db_id=command_log.world_db_id,
            command_log_db_id=command_log.id,
            command_id=command_log.command_id,
            command_type=command_log.command_type,
            user_id=command_log.user_id,
            session_id=command_log.session_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            chunk_snapshot_db_id=chunk_snapshot_db_id,
            **kwargs,
        )

    @property
    def project_public_id(self) -> Optional[str]:
        try:
            return getattr(getattr(self, "project", None), "project_id", None)
        except Exception:
            return None

    @property
    def universe_public_id(self) -> Optional[str]:
        try:
            return getattr(getattr(self, "universe", None), "universe_id", None)
        except Exception:
            return None

    @property
    def world_public_id(self) -> Optional[str]:
        try:
            return getattr(getattr(self, "world", None), "world_id", None)
        except Exception:
            return None

    @property
    def world_position(self) -> Optional[Dict[str, int]]:
        if self.position_x is None or self.position_y is None or self.position_z is None:
            return None
        return {
            "x": int(self.position_x),
            "y": int(self.position_y),
            "z": int(self.position_z),
        }

    @property
    def local_position(self) -> Optional[Dict[str, int]]:
        if self.local_x is None or self.local_y is None or self.local_z is None:
            return None
        return {
            "x": int(self.local_x),
            "y": int(self.local_y),
            "z": int(self.local_z),
        }

    @property
    def chunk_coords(self) -> Dict[str, int]:
        return {
            "chunkX": int(self.chunk_x),
            "chunkY": int(self.chunk_y),
            "chunkZ": int(self.chunk_z),
        }

    @property
    def block_transition(self) -> Dict[str, Any]:
        return {
            "before": {
                "blockTypeId": self.block_before_type_id,
                "cellValue": self.cell_before_value,
            },
            "after": {
                "blockTypeId": self.block_after_type_id,
                "cellValue": self.cell_after_value,
            },
        }

    @property
    def chunk_version_transition(self) -> Dict[str, Any]:
        return {
            "before": {
                "chunkRevision": self.chunk_revision_before,
                "chunkVersion": self.chunk_version_before,
                "contentHash": self.content_hash_before,
            },
            "after": {
                "chunkRevision": self.chunk_revision_after,
                "chunkVersion": self.chunk_version_after,
                "contentHash": self.content_hash_after,
            },
        }

    def mark_superseded(self) -> None:
        """Mark event as superseded without deleting it."""
        self.event_status = EVENT_STATUS_SUPERSEDED

    def mark_compensated(self) -> None:
        """Mark event as compensated without deleting it."""
        self.event_status = EVENT_STATUS_COMPENSATED

    def get_validation_errors(self) -> Dict[str, str]:
        """Return validation errors without raising."""
        errors: Dict[str, str] = {}

        for attr, field_name in (
            ("project_db_id", "projectDbId"),
            ("universe_db_id", "universeDbId"),
            ("world_db_id", "worldDbId"),
        ):
            try:
                normalize_db_id(getattr(self, attr), field_name=attr)
            except Exception as exc:
                errors[field_name] = str(exc)

        try:
            normalize_event_id(self.event_id)
        except Exception as exc:
            errors["eventId"] = str(exc)

        try:
            normalize_command_id(self.command_id)
        except Exception as exc:
            errors["commandId"] = str(exc)

        try:
            normalize_event_type(self.event_type)
        except Exception as exc:
            errors["eventType"] = str(exc)

        try:
            normalize_event_status(self.event_status)
        except Exception as exc:
            errors["eventStatus"] = str(exc)

        try:
            normalize_command_type(self.command_type)
        except Exception as exc:
            errors["commandType"] = str(exc)

        try:
            chunk_x = normalize_chunk_coord(self.chunk_x, field_name="chunk_x")
            chunk_y = normalize_chunk_coord(self.chunk_y, field_name="chunk_y")
            chunk_z = normalize_chunk_coord(self.chunk_z, field_name="chunk_z")
            assert_chunk_key_matches(
                chunk_key=self.chunk_key,
                chunk_x=chunk_x,
                chunk_y=chunk_y,
                chunk_z=chunk_z,
            )
        except Exception as exc:
            errors["chunkKey"] = str(exc)

        try:
            normalize_non_negative_int(
                self.affected_cell_count,
                field_name="affected_cell_count",
                default=0,
            )
        except Exception as exc:
            errors["affectedCellCount"] = str(exc)

        try:
            normalize_non_negative_int(
                self.dirty_chunk_count,
                field_name="dirty_chunk_count",
                default=0,
            )
        except Exception as exc:
            errors["dirtyChunkCount"] = str(exc)

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_payload: bool = True,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
        world_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serialize event for API/service responses."""
        result: Dict[str, Any] = {
            "eventId": self.event_id,
            "commandId": self.command_id,
            "projectId": project_id if project_id is not None else self.project_public_id,
            "universeId": universe_id if universe_id is not None else self.universe_public_id,
            "worldId": world_id if world_id is not None else self.world_public_id,
            "eventType": self.event_type,
            "eventStatus": self.event_status,
            "eventSchemaVersion": self.event_schema_version,
            "commandType": self.command_type,
            "userId": self.user_id,
            "sessionId": self.session_id,
            "chunk": {
                **self.chunk_coords,
                "chunkKey": self.chunk_key,
            },
            "position": self.world_position,
            "localPosition": self.local_position,
            "blockTransition": self.block_transition,
            "targetFace": self.target_face,
            "tool": self.tool,
            "chunkVersionTransition": self.chunk_version_transition,
            "object": {
                "objectInstanceId": self.object_instance_id,
                "objectTypeId": self.object_type_id,
                "objectVariantId": self.object_variant_id,
                "footprint": normalize_json_object(
                    self.object_footprint_json,
                    field_name="object_footprint_json",
                ),
            },
            "affectedBounds": normalize_json_object(
                self.affected_bounds_json,
                field_name="affected_bounds_json",
            ),
            "affectedCells": normalize_json_list(
                self.affected_cells_json,
                field_name="affected_cells_json",
            ),
            "affectedCellCount": self.affected_cell_count,
            "dirtyChunks": normalize_json_list(
                self.dirty_chunks_json,
                field_name="dirty_chunks_json",
            ),
            "dirtyChunkCount": self.dirty_chunk_count,
            "metadata": normalize_json_object(self.metadata_json, field_name="metadata_json"),
            "createdAt": datetime_to_iso(self.created_at),
        }

        if include_payload:
            result["payload"] = normalize_json_object(
                self.payload_json,
                field_name="payload_json",
            )

        if include_internal:
            result["id"] = self.id
            result["commandLogDbId"] = self.command_log_db_id
            result["projectDbId"] = self.project_db_id
            result["universeDbId"] = self.universe_db_id
            result["worldDbId"] = self.world_db_id
            result["chunkSnapshotDbId"] = self.chunk_snapshot_db_id

        return result