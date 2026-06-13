# services/vectoplan-chunk/models/object.py
"""
SQLAlchemy models for multi-block world objects in VECTOPLAN.

This file prepares storage for objects that are larger than one block, for
example:

    4x4x2
    2x1x2
    1x1x3
    arbitrary future Library-backed object footprints

Important design rules:
- A WorldObjectInstance is the logical object in a concrete WorldInstance.
- A WorldObjectChunkRef maps that object to every chunk it touches.
- One object can occupy many chunks.
- One chunk can contain parts of many objects.
- ChunkSnapshot remains the load-truth for actual chunk cell state.
- ChunkEvent / WorldCommandLog remain the historical truth.
- This model does not replace ChunkSnapshot.
- This model makes later PlaceObject / RemoveObject possible without redesigning
  the persistence layer.
- This model does not perform commits.
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
        "models/object.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
        "`db` instance before importing models."
    ) from _DB_IMPORT_ERROR


try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - fallback is useful for tests/non-postgres tooling
    JSONB = None  # type: ignore[assignment]


JSON_COLUMN_TYPE = JSONB if JSONB is not None else db.JSON


WORLD_OBJECT_INSTANCE_SCHEMA_VERSION = "world-object-instance.schema.v1"
WORLD_OBJECT_CHUNK_REF_SCHEMA_VERSION = "world-object-chunk-ref.schema.v1"

OBJECT_STATUS_ACTIVE = "active"
OBJECT_STATUS_ARCHIVED = "archived"
OBJECT_STATUS_DELETED = "deleted"
OBJECT_STATUS_DETACHED = "detached"

VALID_OBJECT_STATUSES = frozenset(
    {
        OBJECT_STATUS_ACTIVE,
        OBJECT_STATUS_ARCHIVED,
        OBJECT_STATUS_DELETED,
        OBJECT_STATUS_DETACHED,
    }
)

OBJECT_SOURCE_EDITOR = "editor"
OBJECT_SOURCE_LIBRARY = "library"
OBJECT_SOURCE_IMPORTER = "importer"
OBJECT_SOURCE_SYSTEM = "system"
OBJECT_SOURCE_AI = "ai"
OBJECT_SOURCE_TEST = "test"
OBJECT_SOURCE_UNKNOWN = "unknown"

VALID_OBJECT_SOURCES = frozenset(
    {
        OBJECT_SOURCE_EDITOR,
        OBJECT_SOURCE_LIBRARY,
        OBJECT_SOURCE_IMPORTER,
        OBJECT_SOURCE_SYSTEM,
        OBJECT_SOURCE_AI,
        OBJECT_SOURCE_TEST,
        OBJECT_SOURCE_UNKNOWN,
    }
)

OBJECT_KIND_BLOCK_COMPOSITE = "block_composite"
OBJECT_KIND_LIBRARY_OBJECT = "library_object"
OBJECT_KIND_IMPORTED_OBJECT = "imported_object"
OBJECT_KIND_RUNTIME_OBJECT = "runtime_object"
OBJECT_KIND_STRUCTURE = "structure"
OBJECT_KIND_UNKNOWN = "unknown"

VALID_OBJECT_KINDS = frozenset(
    {
        OBJECT_KIND_BLOCK_COMPOSITE,
        OBJECT_KIND_LIBRARY_OBJECT,
        OBJECT_KIND_IMPORTED_OBJECT,
        OBJECT_KIND_RUNTIME_OBJECT,
        OBJECT_KIND_STRUCTURE,
        OBJECT_KIND_UNKNOWN,
    }
)

OBJECT_ANCHOR_MODE_WORLD_CELL = "world_cell"
OBJECT_ANCHOR_MODE_SURFACE_RELATIVE = "surface_relative"
OBJECT_ANCHOR_MODE_GEO_ANCHORED = "geo_anchored"
OBJECT_ANCHOR_MODE_FREE = "free"

VALID_OBJECT_ANCHOR_MODES = frozenset(
    {
        OBJECT_ANCHOR_MODE_WORLD_CELL,
        OBJECT_ANCHOR_MODE_SURFACE_RELATIVE,
        OBJECT_ANCHOR_MODE_GEO_ANCHORED,
        OBJECT_ANCHOR_MODE_FREE,
    }
)

CHUNK_REF_STATUS_ACTIVE = "active"
CHUNK_REF_STATUS_STALE = "stale"
CHUNK_REF_STATUS_DELETED = "deleted"

VALID_CHUNK_REF_STATUSES = frozenset(
    {
        CHUNK_REF_STATUS_ACTIVE,
        CHUNK_REF_STATUS_STALE,
        CHUNK_REF_STATUS_DELETED,
    }
)

CHUNK_REF_ROLE_PRIMARY = "primary"
CHUNK_REF_ROLE_OCCUPIED = "occupied"
CHUNK_REF_ROLE_BOUNDARY = "boundary"
CHUNK_REF_ROLE_DIRTY_NEIGHBOR = "dirty_neighbor"
CHUNK_REF_ROLE_METADATA_ONLY = "metadata_only"

VALID_CHUNK_REF_ROLES = frozenset(
    {
        CHUNK_REF_ROLE_PRIMARY,
        CHUNK_REF_ROLE_OCCUPIED,
        CHUNK_REF_ROLE_BOUNDARY,
        CHUNK_REF_ROLE_DIRTY_NEIGHBOR,
        CHUNK_REF_ROLE_METADATA_ONLY,
    }
)

OBJECT_INSTANCE_ID_MAX_LENGTH = 160
OBJECT_TYPE_ID_MAX_LENGTH = 160
OBJECT_VARIANT_ID_MAX_LENGTH = 160
OBJECT_LABEL_MAX_LENGTH = 255
OBJECT_DESCRIPTION_MAX_LENGTH = 4096
OBJECT_STATUS_MAX_LENGTH = 64
OBJECT_SOURCE_MAX_LENGTH = 64
OBJECT_KIND_MAX_LENGTH = 96
OBJECT_ANCHOR_MODE_MAX_LENGTH = 64
OBJECT_REF_ROLE_MAX_LENGTH = 64
SCHEMA_VERSION_MAX_LENGTH = 64
USER_ID_MAX_LENGTH = 128
SESSION_ID_MAX_LENGTH = 128
COMMAND_ID_MAX_LENGTH = 128
EVENT_ID_MAX_LENGTH = 128
CHUNK_KEY_MAX_LENGTH = 96
CONTENT_HASH_MAX_LENGTH = 128
LIBRARY_ID_MAX_LENGTH = 160

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
    """
    Convert arbitrary values to JSON-safe structures.

    Object metadata can later come from editor tools, library data, importers,
    AI suggestions or migration scripts.
    """
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


def generate_object_instance_id(prefix: str = "obj") -> str:
    """Generate a stable object instance id."""
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="object_instance_id_prefix",
        max_length=32,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def normalize_object_instance_id(value: Any) -> str:
    """Normalize object_instance_id."""
    return normalize_public_id(
        value,
        field_name="object_instance_id",
        max_length=OBJECT_INSTANCE_ID_MAX_LENGTH,
    )


def normalize_optional_object_instance_id(value: Any) -> Optional[str]:
    """Normalize optional object_instance_id."""
    return normalize_optional_public_id(
        value,
        field_name="object_instance_id",
        max_length=OBJECT_INSTANCE_ID_MAX_LENGTH,
    )


def normalize_object_type_id(value: Any) -> str:
    """Normalize object_type_id."""
    return normalize_public_id(
        value,
        field_name="object_type_id",
        max_length=OBJECT_TYPE_ID_MAX_LENGTH,
    )


def normalize_optional_object_type_id(value: Any) -> Optional[str]:
    """Normalize optional object_type_id."""
    return normalize_optional_public_id(
        value,
        field_name="object_type_id",
        max_length=OBJECT_TYPE_ID_MAX_LENGTH,
    )


def normalize_optional_object_variant_id(value: Any) -> Optional[str]:
    """Normalize optional object_variant_id."""
    return normalize_optional_public_id(
        value,
        field_name="object_variant_id",
        max_length=OBJECT_VARIANT_ID_MAX_LENGTH,
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


def normalize_positive_int(value: Any, *, field_name: str) -> int:
    """Normalize required positive integer values."""
    result = normalize_int(value, field_name=field_name)
    if result <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return result


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


def normalize_object_status(value: Any) -> str:
    """Normalize and validate object status."""
    if value is None:
        return OBJECT_STATUS_ACTIVE

    status = str(value).strip().lower()

    if status not in VALID_OBJECT_STATUSES:
        allowed = ", ".join(sorted(VALID_OBJECT_STATUSES))
        raise ValueError(f"Invalid object status '{value}'. Allowed: {allowed}.")

    return status


def normalize_object_source(value: Any) -> str:
    """Normalize and validate object source."""
    if value is None:
        return OBJECT_SOURCE_EDITOR

    source = str(value).strip().lower()

    if source not in VALID_OBJECT_SOURCES:
        allowed = ", ".join(sorted(VALID_OBJECT_SOURCES))
        raise ValueError(f"Invalid object source '{value}'. Allowed: {allowed}.")

    return source


def normalize_object_kind(value: Any) -> str:
    """Normalize and validate object kind."""
    if value is None:
        return OBJECT_KIND_BLOCK_COMPOSITE

    kind = str(value).strip().lower()

    if kind not in VALID_OBJECT_KINDS:
        allowed = ", ".join(sorted(VALID_OBJECT_KINDS))
        raise ValueError(f"Invalid object kind '{value}'. Allowed: {allowed}.")

    return kind


def normalize_anchor_mode(value: Any) -> str:
    """Normalize and validate anchor mode."""
    if value is None:
        return OBJECT_ANCHOR_MODE_WORLD_CELL

    mode = str(value).strip().lower()

    if mode not in VALID_OBJECT_ANCHOR_MODES:
        allowed = ", ".join(sorted(VALID_OBJECT_ANCHOR_MODES))
        raise ValueError(f"Invalid object anchor mode '{value}'. Allowed: {allowed}.")

    return mode


def normalize_chunk_ref_status(value: Any) -> str:
    """Normalize and validate chunk ref status."""
    if value is None:
        return CHUNK_REF_STATUS_ACTIVE

    status = str(value).strip().lower()

    if status not in VALID_CHUNK_REF_STATUSES:
        allowed = ", ".join(sorted(VALID_CHUNK_REF_STATUSES))
        raise ValueError(f"Invalid chunk ref status '{value}'. Allowed: {allowed}.")

    return status


def normalize_chunk_ref_role(value: Any) -> str:
    """Normalize and validate chunk ref role."""
    if value is None:
        return CHUNK_REF_ROLE_OCCUPIED

    role = str(value).strip().lower()

    if role not in VALID_CHUNK_REF_ROLES:
        allowed = ", ".join(sorted(VALID_CHUNK_REF_ROLES))
        raise ValueError(f"Invalid chunk ref role '{value}'. Allowed: {allowed}.")

    return role


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
    """Parse canonical chunk key into coordinates."""
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


def count_json_list(value: Any) -> int:
    """Best-effort length for JSON list-like values."""
    try:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return len(value)
    except Exception:
        return 0
    return 0


def build_bounds_from_anchor_and_size(
    *,
    anchor_x: int,
    anchor_y: int,
    anchor_z: int,
    size_x: int,
    size_y: int,
    size_z: int,
) -> Dict[str, Any]:
    """
    Build inclusive/exclusive world-cell bounds for an axis-aligned object.

    `min` is inclusive.
    `maxExclusive` is exclusive.
    """
    return {
        "min": {
            "x": int(anchor_x),
            "y": int(anchor_y),
            "z": int(anchor_z),
        },
        "maxExclusive": {
            "x": int(anchor_x) + int(size_x),
            "y": int(anchor_y) + int(size_y),
            "z": int(anchor_z) + int(size_z),
        },
        "size": {
            "x": int(size_x),
            "y": int(size_y),
            "z": int(size_z),
        },
    }


def normalize_bounds_json(value: Any, *, field_name: str) -> Dict[str, Any]:
    """Normalize bounds JSON."""
    return normalize_json_object(value, field_name=field_name)


def normalize_rotation_json(value: Any) -> Dict[str, Any]:
    """
    Normalize object rotation.

    Preferred future shape:
        {
          "yaw": 0,
          "pitch": 0,
          "roll": 0,
          "unit": "degrees"
        }
    """
    if value is None:
        return {
            "yaw": 0,
            "pitch": 0,
            "roll": 0,
            "unit": "degrees",
        }

    return normalize_json_object(value, field_name="rotation_json")


class WorldObjectInstance(db.Model):
    """
    Persistent logical object instance in one concrete WorldInstance.

    This represents a larger semantic/runtime object, not merely one cell.

    Examples:
    - A 4x4x2 placed block-composite object.
    - A future Library object.
    - A grouped runtime structure.
    - A later imported object mapped into chunk cells.
    """

    __tablename__ = "world_object_instances"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    object_instance_id = db.Column(
        db.String(OBJECT_INSTANCE_ID_MAX_LENGTH),
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

    status = db.Column(
        db.String(OBJECT_STATUS_MAX_LENGTH),
        nullable=False,
        default=OBJECT_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(SCHEMA_VERSION_MAX_LENGTH),
        nullable=False,
        default=WORLD_OBJECT_INSTANCE_SCHEMA_VERSION,
    )

    revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    object_source = db.Column(
        db.String(OBJECT_SOURCE_MAX_LENGTH),
        nullable=False,
        default=OBJECT_SOURCE_EDITOR,
        index=True,
    )

    object_kind = db.Column(
        db.String(OBJECT_KIND_MAX_LENGTH),
        nullable=False,
        default=OBJECT_KIND_BLOCK_COMPOSITE,
        index=True,
    )

    object_type_id = db.Column(
        db.String(OBJECT_TYPE_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    object_variant_id = db.Column(
        db.String(OBJECT_VARIANT_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    label = db.Column(
        db.String(OBJECT_LABEL_MAX_LENGTH),
        nullable=True,
    )

    description = db.Column(
        db.String(OBJECT_DESCRIPTION_MAX_LENGTH),
        nullable=True,
    )

    library_id = db.Column(
        db.String(LIBRARY_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    library_version = db.Column(
        db.String(64),
        nullable=True,
        index=True,
    )

    library_snapshot_id = db.Column(
        db.String(160),
        nullable=True,
        index=True,
    )

    anchor_mode = db.Column(
        db.String(OBJECT_ANCHOR_MODE_MAX_LENGTH),
        nullable=False,
        default=OBJECT_ANCHOR_MODE_WORLD_CELL,
        index=True,
    )

    anchor_x = db.Column(
        db.BigInteger,
        nullable=False,
        index=True,
    )

    anchor_y = db.Column(
        db.BigInteger,
        nullable=False,
        index=True,
    )

    anchor_z = db.Column(
        db.BigInteger,
        nullable=False,
        index=True,
    )

    size_x = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    size_y = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    size_z = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    rotation_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    transform_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    bounds_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    footprint_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    occupied_cells_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    occupied_cell_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    touched_chunks_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    touched_chunk_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        index=True,
    )

    primary_chunk_x = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    primary_chunk_y = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    primary_chunk_z = db.Column(
        db.BigInteger,
        nullable=True,
        index=True,
    )

    primary_chunk_key = db.Column(
        db.String(CHUNK_KEY_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_by_command_id = db.Column(
        db.String(COMMAND_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_by_command_id = db.Column(
        db.String(COMMAND_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    removed_by_command_id = db.Column(
        db.String(COMMAND_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_event_id = db.Column(
        db.String(EVENT_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_event_id = db.Column(
        db.String(EVENT_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    removed_event_id = db.Column(
        db.String(EVENT_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    content_hash = db.Column(
        db.String(CONTENT_HASH_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_by_user_id = db.Column(
        db.String(USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_by_user_id = db.Column(
        db.String(USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    last_session_id = db.Column(
        db.String(SESSION_ID_MAX_LENGTH),
        nullable=True,
        index=True,
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

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        index=True,
    )

    archived_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    deleted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    project = db.relationship(
        "Project",
        backref=db.backref(
            "world_object_instances",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    universe = db.relationship(
        "Universe",
        backref=db.backref(
            "world_object_instances",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    world = db.relationship(
        "WorldInstance",
        backref=db.backref(
            "world_object_instances",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "world_db_id",
            "object_instance_id",
            name="uq_world_object_instances_world_object_instance_id",
        ),
        db.CheckConstraint(
            "project_db_id > 0",
            name="ck_world_object_instances_project_db_id_positive",
        ),
        db.CheckConstraint(
            "universe_db_id > 0",
            name="ck_world_object_instances_universe_db_id_positive",
        ),
        db.CheckConstraint(
            "world_db_id > 0",
            name="ck_world_object_instances_world_db_id_positive",
        ),
        db.CheckConstraint(
            "object_instance_id <> ''",
            name="ck_world_object_instances_object_instance_id_not_empty",
        ),
        db.CheckConstraint(
            "object_type_id <> ''",
            name="ck_world_object_instances_object_type_id_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'archived', 'deleted', 'detached')",
            name="ck_world_object_instances_status_valid",
        ),
        db.CheckConstraint(
            "object_source IN ('editor', 'library', 'importer', 'system', 'ai', 'test', 'unknown')",
            name="ck_world_object_instances_object_source_valid",
        ),
        db.CheckConstraint(
            "object_kind IN ('block_composite', 'library_object', 'imported_object', 'runtime_object', 'structure', 'unknown')",
            name="ck_world_object_instances_object_kind_valid",
        ),
        db.CheckConstraint(
            "anchor_mode IN ('world_cell', 'surface_relative', 'geo_anchored', 'free')",
            name="ck_world_object_instances_anchor_mode_valid",
        ),
        db.CheckConstraint(
            "revision >= 1",
            name="ck_world_object_instances_revision_positive",
        ),
        db.CheckConstraint(
            "size_x > 0",
            name="ck_world_object_instances_size_x_positive",
        ),
        db.CheckConstraint(
            "size_y > 0",
            name="ck_world_object_instances_size_y_positive",
        ),
        db.CheckConstraint(
            "size_z > 0",
            name="ck_world_object_instances_size_z_positive",
        ),
        db.CheckConstraint(
            "occupied_cell_count >= 0",
            name="ck_world_object_instances_occupied_cell_count_non_negative",
        ),
        db.CheckConstraint(
            "touched_chunk_count >= 0",
            name="ck_world_object_instances_touched_chunk_count_non_negative",
        ),
        db.Index(
            "ix_world_object_instances_world_status_updated",
            "world_db_id",
            "status",
            "updated_at",
        ),
        db.Index(
            "ix_world_object_instances_world_type_variant",
            "world_db_id",
            "object_type_id",
            "object_variant_id",
        ),
        db.Index(
            "ix_world_object_instances_world_anchor",
            "world_db_id",
            "anchor_x",
            "anchor_y",
            "anchor_z",
        ),
        db.Index(
            "ix_world_object_instances_primary_chunk",
            "world_db_id",
            "primary_chunk_key",
        ),
        db.Index(
            "ix_world_object_instances_library",
            "library_id",
            "library_version",
            "library_snapshot_id",
        ),
        db.Index(
            "ix_world_object_instances_commands",
            "created_by_command_id",
            "updated_by_command_id",
            "removed_by_command_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<WorldObjectInstance id={self.id!r} "
            f"object_instance_id={self.object_instance_id!r} "
            f"world_db_id={self.world_db_id!r} status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: int,
        universe_db_id: int,
        world_db_id: int,
        object_type_id: str,
        anchor_x: int,
        anchor_y: int,
        anchor_z: int,
        size_x: int = 1,
        size_y: int = 1,
        size_z: int = 1,
        object_instance_id: Optional[str] = None,
        object_variant_id: Optional[str] = None,
        label: Optional[str] = None,
        description: Optional[str] = None,
        status: str = OBJECT_STATUS_ACTIVE,
        object_source: str = OBJECT_SOURCE_EDITOR,
        object_kind: str = OBJECT_KIND_BLOCK_COMPOSITE,
        anchor_mode: str = OBJECT_ANCHOR_MODE_WORLD_CELL,
        library_id: Optional[str] = None,
        library_version: Optional[str] = None,
        library_snapshot_id: Optional[str] = None,
        rotation_json: Optional[Mapping[str, Any]] = None,
        transform_json: Optional[Mapping[str, Any]] = None,
        bounds_json: Optional[Mapping[str, Any]] = None,
        footprint_json: Optional[Mapping[str, Any]] = None,
        occupied_cells_json: Optional[Sequence[Any]] = None,
        touched_chunks_json: Optional[Sequence[Any]] = None,
        primary_chunk_x: Optional[int] = None,
        primary_chunk_y: Optional[int] = None,
        primary_chunk_z: Optional[int] = None,
        primary_chunk_key: Optional[str] = None,
        created_by_command_id: Optional[str] = None,
        updated_by_command_id: Optional[str] = None,
        removed_by_command_id: Optional[str] = None,
        created_event_id: Optional[str] = None,
        updated_event_id: Optional[str] = None,
        removed_event_id: Optional[str] = None,
        content_hash: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        last_session_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "WorldObjectInstance":
        """Create a WorldObjectInstance without adding it to a session."""
        normalized_anchor_x = normalize_int(anchor_x, field_name="anchor_x")
        normalized_anchor_y = normalize_int(anchor_y, field_name="anchor_y")
        normalized_anchor_z = normalize_int(anchor_z, field_name="anchor_z")
        normalized_size_x = normalize_positive_int(size_x, field_name="size_x")
        normalized_size_y = normalize_positive_int(size_y, field_name="size_y")
        normalized_size_z = normalize_positive_int(size_z, field_name="size_z")

        resolved_bounds = (
            normalize_bounds_json(bounds_json, field_name="bounds_json")
            if bounds_json is not None
            else build_bounds_from_anchor_and_size(
                anchor_x=normalized_anchor_x,
                anchor_y=normalized_anchor_y,
                anchor_z=normalized_anchor_z,
                size_x=normalized_size_x,
                size_y=normalized_size_y,
                size_z=normalized_size_z,
            )
        )

        resolved_occupied_cells = normalize_json_list(
            occupied_cells_json,
            field_name="occupied_cells_json",
        )
        resolved_touched_chunks = normalize_json_list(
            touched_chunks_json,
            field_name="touched_chunks_json",
        )

        if primary_chunk_key is not None:
            resolved_primary_chunk_key = normalize_chunk_key(primary_chunk_key)
            parsed_x, parsed_y, parsed_z = parse_chunk_key(resolved_primary_chunk_key)

            if primary_chunk_x is None:
                primary_chunk_x = parsed_x
            if primary_chunk_y is None:
                primary_chunk_y = parsed_y
            if primary_chunk_z is None:
                primary_chunk_z = parsed_z

            assert_chunk_key_matches(
                chunk_key=resolved_primary_chunk_key,
                chunk_x=primary_chunk_x,
                chunk_y=primary_chunk_y,
                chunk_z=primary_chunk_z,
            )
        elif (
            primary_chunk_x is not None
            and primary_chunk_y is not None
            and primary_chunk_z is not None
        ):
            resolved_primary_chunk_key = build_chunk_key(
                primary_chunk_x,
                primary_chunk_y,
                primary_chunk_z,
            )
        else:
            resolved_primary_chunk_key = None

        normalized_status = normalize_object_status(status)
        now = utc_now()

        return cls(
            object_instance_id=normalize_object_instance_id(
                object_instance_id or generate_object_instance_id()
            ),
            project_db_id=normalize_db_id(project_db_id, field_name="project_db_id"),
            universe_db_id=normalize_db_id(universe_db_id, field_name="universe_db_id"),
            world_db_id=normalize_db_id(world_db_id, field_name="world_db_id"),
            status=normalized_status,
            schema_version=WORLD_OBJECT_INSTANCE_SCHEMA_VERSION,
            revision=1,
            object_source=normalize_object_source(object_source),
            object_kind=normalize_object_kind(object_kind),
            object_type_id=normalize_object_type_id(object_type_id),
            object_variant_id=normalize_optional_object_variant_id(object_variant_id),
            label=normalize_optional_text(
                label,
                field_name="label",
                max_length=OBJECT_LABEL_MAX_LENGTH,
            ),
            description=normalize_optional_text(
                description,
                field_name="description",
                max_length=OBJECT_DESCRIPTION_MAX_LENGTH,
            ),
            library_id=normalize_optional_public_id(
                library_id,
                field_name="library_id",
                max_length=LIBRARY_ID_MAX_LENGTH,
            ),
            library_version=normalize_optional_text(
                library_version,
                field_name="library_version",
                max_length=64,
            ),
            library_snapshot_id=normalize_optional_public_id(
                library_snapshot_id,
                field_name="library_snapshot_id",
                max_length=160,
            ),
            anchor_mode=normalize_anchor_mode(anchor_mode),
            anchor_x=normalized_anchor_x,
            anchor_y=normalized_anchor_y,
            anchor_z=normalized_anchor_z,
            size_x=normalized_size_x,
            size_y=normalized_size_y,
            size_z=normalized_size_z,
            rotation_json=normalize_rotation_json(rotation_json),
            transform_json=normalize_json_object(
                transform_json,
                field_name="transform_json",
            ),
            bounds_json=resolved_bounds,
            footprint_json=normalize_json_object(
                footprint_json,
                field_name="footprint_json",
            ),
            occupied_cells_json=resolved_occupied_cells,
            occupied_cell_count=count_json_list(resolved_occupied_cells),
            touched_chunks_json=resolved_touched_chunks,
            touched_chunk_count=count_json_list(resolved_touched_chunks),
            primary_chunk_x=normalize_optional_int(
                primary_chunk_x,
                field_name="primary_chunk_x",
            ),
            primary_chunk_y=normalize_optional_int(
                primary_chunk_y,
                field_name="primary_chunk_y",
            ),
            primary_chunk_z=normalize_optional_int(
                primary_chunk_z,
                field_name="primary_chunk_z",
            ),
            primary_chunk_key=resolved_primary_chunk_key,
            created_by_command_id=normalize_optional_public_id(
                created_by_command_id,
                field_name="created_by_command_id",
                max_length=COMMAND_ID_MAX_LENGTH,
            ),
            updated_by_command_id=normalize_optional_public_id(
                updated_by_command_id,
                field_name="updated_by_command_id",
                max_length=COMMAND_ID_MAX_LENGTH,
            ),
            removed_by_command_id=normalize_optional_public_id(
                removed_by_command_id,
                field_name="removed_by_command_id",
                max_length=COMMAND_ID_MAX_LENGTH,
            ),
            created_event_id=normalize_optional_public_id(
                created_event_id,
                field_name="created_event_id",
                max_length=EVENT_ID_MAX_LENGTH,
            ),
            updated_event_id=normalize_optional_public_id(
                updated_event_id,
                field_name="updated_event_id",
                max_length=EVENT_ID_MAX_LENGTH,
            ),
            removed_event_id=normalize_optional_public_id(
                removed_event_id,
                field_name="removed_event_id",
                max_length=EVENT_ID_MAX_LENGTH,
            ),
            content_hash=normalize_optional_hash(
                content_hash,
                field_name="content_hash",
            ),
            created_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="created_by_user_id",
                max_length=USER_ID_MAX_LENGTH,
            ),
            updated_by_user_id=normalize_optional_text(
                updated_by_user_id or created_by_user_id,
                field_name="updated_by_user_id",
                max_length=USER_ID_MAX_LENGTH,
            ),
            last_session_id=normalize_optional_text(
                last_session_id,
                field_name="last_session_id",
                max_length=SESSION_ID_MAX_LENGTH,
            ),
            metadata_json=normalize_json_object(metadata_json, field_name="metadata_json"),
            created_at=now,
            updated_at=now,
            archived_at=now if normalized_status == OBJECT_STATUS_ARCHIVED else None,
            deleted_at=now if normalized_status == OBJECT_STATUS_DELETED else None,
        )

    @classmethod
    def create_for_world(
        cls,
        world: Any,
        *,
        object_type_id: str,
        anchor_x: int,
        anchor_y: int,
        anchor_z: int,
        size_x: int = 1,
        size_y: int = 1,
        size_z: int = 1,
        **kwargs: Any,
    ) -> "WorldObjectInstance":
        """Create an object for a persisted WorldInstance."""
        project_db_id = getattr(world, "project_db_id", None)
        universe_db_id = getattr(world, "universe_db_id", None)
        world_db_id = getattr(world, "id", None)

        if project_db_id is None:
            raise ValueError("Cannot create object without world.project_db_id.")
        if universe_db_id is None:
            raise ValueError("Cannot create object without world.universe_db_id.")
        if world_db_id is None:
            raise ValueError("Cannot create object without persisted world.id.")

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_db_id=world_db_id,
            object_type_id=object_type_id,
            anchor_x=anchor_x,
            anchor_y=anchor_y,
            anchor_z=anchor_z,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            **kwargs,
        )

    @classmethod
    def from_place_object_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        project_db_id: int,
        universe_db_id: int,
        world_db_id: int,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> "WorldObjectInstance":
        """Create object instance from a future PlaceObject-style payload."""
        if not isinstance(payload, Mapping):
            raise ValueError("PlaceObject payload must be a JSON object.")

        position = payload.get("position") if isinstance(payload.get("position"), Mapping) else {}
        anchor = payload.get("anchor") if isinstance(payload.get("anchor"), Mapping) else position
        object_data = payload.get("object") if isinstance(payload.get("object"), Mapping) else {}
        dimensions = object_data.get("dimensions") if isinstance(object_data.get("dimensions"), Mapping) else payload.get("dimensions")
        if not isinstance(dimensions, Mapping):
            dimensions = {}

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_db_id=world_db_id,
            object_instance_id=(
                payload.get("objectInstanceId")
                or payload.get("object_instance_id")
                or object_data.get("objectInstanceId")
                or object_data.get("object_instance_id")
            ),
            object_type_id=(
                payload.get("objectTypeId")
                or payload.get("object_type_id")
                or object_data.get("objectTypeId")
                or object_data.get("object_type_id")
            ),
            object_variant_id=(
                payload.get("objectVariantId")
                or payload.get("object_variant_id")
                or object_data.get("variantId")
                or object_data.get("variant_id")
            ),
            label=payload.get("label") or object_data.get("label"),
            object_source=payload.get("objectSource") or payload.get("object_source") or OBJECT_SOURCE_EDITOR,
            object_kind=payload.get("objectKind") or payload.get("object_kind") or OBJECT_KIND_BLOCK_COMPOSITE,
            anchor_x=anchor.get("x"),
            anchor_y=anchor.get("y"),
            anchor_z=anchor.get("z"),
            size_x=dimensions.get("x") or dimensions.get("width") or payload.get("sizeX") or 1,
            size_y=dimensions.get("y") or dimensions.get("height") or payload.get("sizeY") or 1,
            size_z=dimensions.get("z") or dimensions.get("depth") or payload.get("sizeZ") or 1,
            rotation_json=object_data.get("rotation") if isinstance(object_data.get("rotation"), Mapping) else payload.get("rotation"),
            transform_json=object_data.get("transform") if isinstance(object_data.get("transform"), Mapping) else payload.get("transform"),
            footprint_json=payload.get("footprint") if isinstance(payload.get("footprint"), Mapping) else None,
            occupied_cells_json=payload.get("occupiedCells") or payload.get("occupied_cells"),
            touched_chunks_json=payload.get("touchedChunks") or payload.get("touched_chunks"),
            created_by_command_id=payload.get("commandId") or payload.get("command_id"),
            created_by_user_id=payload.get("userId") or payload.get("user_id") or user_id,
            updated_by_user_id=payload.get("userId") or payload.get("user_id") or user_id,
            last_session_id=payload.get("sessionId") or payload.get("session_id") or session_id,
            metadata_json=payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else None,
        )

    @property
    def is_active(self) -> bool:
        return self.status == OBJECT_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_archived(self) -> bool:
        return self.status == OBJECT_STATUS_ARCHIVED

    @property
    def is_deleted(self) -> bool:
        return self.status == OBJECT_STATUS_DELETED or self.deleted_at is not None

    @property
    def is_detached(self) -> bool:
        return self.status == OBJECT_STATUS_DETACHED

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
    def anchor_position(self) -> Dict[str, int]:
        return {
            "x": int(self.anchor_x),
            "y": int(self.anchor_y),
            "z": int(self.anchor_z),
        }

    @property
    def size(self) -> Dict[str, int]:
        return {
            "x": int(self.size_x),
            "y": int(self.size_y),
            "z": int(self.size_z),
        }

    @property
    def primary_chunk(self) -> Optional[Dict[str, Any]]:
        if self.primary_chunk_key is None:
            return None

        return {
            "chunkX": self.primary_chunk_x,
            "chunkY": self.primary_chunk_y,
            "chunkZ": self.primary_chunk_z,
            "chunkKey": self.primary_chunk_key,
        }

    def touch(
        self,
        *,
        updated_by_user_id: Optional[str] = None,
        last_session_id: Optional[str] = None,
    ) -> None:
        """Mark object as updated and increment revision."""
        self.updated_at = utc_now()
        self.revision = int(self.revision or 1) + 1

        normalized_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="updated_by_user_id",
            max_length=USER_ID_MAX_LENGTH,
        )
        if normalized_user_id is not None:
            self.updated_by_user_id = normalized_user_id

        normalized_session_id = normalize_optional_text(
            last_session_id,
            field_name="last_session_id",
            max_length=SESSION_ID_MAX_LENGTH,
        )
        if normalized_session_id is not None:
            self.last_session_id = normalized_session_id

    def ensure_not_deleted(self) -> None:
        """Raise when a mutation is attempted on a soft-deleted object."""
        if self.is_deleted:
            raise ValueError(
                f"WorldObjectInstance '{self.object_instance_id}' is deleted and cannot be modified."
            )

    def set_status(
        self,
        status: str,
        *,
        updated_by_user_id: Optional[str] = None,
        command_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> None:
        """Set object status."""
        normalized_status = normalize_object_status(status)
        now = utc_now()

        if normalized_status == OBJECT_STATUS_DELETED:
            self.deleted_at = self.deleted_at or now
            self.removed_by_command_id = normalize_optional_public_id(
                command_id,
                field_name="removed_by_command_id",
                max_length=COMMAND_ID_MAX_LENGTH,
            )
            self.removed_event_id = normalize_optional_public_id(
                event_id,
                field_name="removed_event_id",
                max_length=EVENT_ID_MAX_LENGTH,
            )
        elif normalized_status == OBJECT_STATUS_ARCHIVED:
            self.archived_at = self.archived_at or now
            self.deleted_at = None
        elif normalized_status in {OBJECT_STATUS_ACTIVE, OBJECT_STATUS_DETACHED}:
            self.archived_at = None
            self.deleted_at = None

        self.status = normalized_status
        self.touch(updated_by_user_id=updated_by_user_id)

    def archive(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Archive object without deleting historical data."""
        self.ensure_not_deleted()
        self.status = OBJECT_STATUS_ARCHIVED
        self.archived_at = self.archived_at or utc_now()
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Restore archived/deleted object."""
        self.status = OBJECT_STATUS_ACTIVE
        self.archived_at = None
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(
        self,
        *,
        updated_by_user_id: Optional[str] = None,
        command_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> None:
        """Soft-delete object."""
        self.status = OBJECT_STATUS_DELETED
        self.deleted_at = self.deleted_at or utc_now()
        self.removed_by_command_id = normalize_optional_public_id(
            command_id,
            field_name="removed_by_command_id",
            max_length=COMMAND_ID_MAX_LENGTH,
        )
        self.removed_event_id = normalize_optional_public_id(
            event_id,
            field_name="removed_event_id",
            max_length=EVENT_ID_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_placement(
        self,
        *,
        anchor_x: int,
        anchor_y: int,
        anchor_z: int,
        size_x: Optional[int] = None,
        size_y: Optional[int] = None,
        size_z: Optional[int] = None,
        rotation_json: Optional[Mapping[str, Any]] = None,
        transform_json: Optional[Mapping[str, Any]] = None,
        updated_by_user_id: Optional[str] = None,
        command_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> None:
        """Update object placement and recompute default bounds."""
        self.ensure_not_deleted()

        self.anchor_x = normalize_int(anchor_x, field_name="anchor_x")
        self.anchor_y = normalize_int(anchor_y, field_name="anchor_y")
        self.anchor_z = normalize_int(anchor_z, field_name="anchor_z")

        if size_x is not None:
            self.size_x = normalize_positive_int(size_x, field_name="size_x")
        if size_y is not None:
            self.size_y = normalize_positive_int(size_y, field_name="size_y")
        if size_z is not None:
            self.size_z = normalize_positive_int(size_z, field_name="size_z")

        if rotation_json is not None:
            self.rotation_json = normalize_rotation_json(rotation_json)

        if transform_json is not None:
            self.transform_json = normalize_json_object(
                transform_json,
                field_name="transform_json",
            )

        self.bounds_json = build_bounds_from_anchor_and_size(
            anchor_x=self.anchor_x,
            anchor_y=self.anchor_y,
            anchor_z=self.anchor_z,
            size_x=self.size_x,
            size_y=self.size_y,
            size_z=self.size_z,
        )

        self.updated_by_command_id = normalize_optional_public_id(
            command_id,
            field_name="updated_by_command_id",
            max_length=COMMAND_ID_MAX_LENGTH,
        )
        self.updated_event_id = normalize_optional_public_id(
            event_id,
            field_name="updated_event_id",
            max_length=EVENT_ID_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_chunk_refs_summary(
        self,
        *,
        touched_chunks_json: Optional[Sequence[Any]],
        primary_chunk_x: Optional[int] = None,
        primary_chunk_y: Optional[int] = None,
        primary_chunk_z: Optional[int] = None,
        primary_chunk_key: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Update summarized chunk reference metadata."""
        self.ensure_not_deleted()

        touched_chunks = normalize_json_list(
            touched_chunks_json,
            field_name="touched_chunks_json",
        )

        self.touched_chunks_json = touched_chunks
        self.touched_chunk_count = count_json_list(touched_chunks)

        if primary_chunk_key is not None:
            resolved_primary_chunk_key = normalize_chunk_key(primary_chunk_key)
            parsed_x, parsed_y, parsed_z = parse_chunk_key(resolved_primary_chunk_key)

            if primary_chunk_x is None:
                primary_chunk_x = parsed_x
            if primary_chunk_y is None:
                primary_chunk_y = parsed_y
            if primary_chunk_z is None:
                primary_chunk_z = parsed_z

            assert_chunk_key_matches(
                chunk_key=resolved_primary_chunk_key,
                chunk_x=primary_chunk_x,
                chunk_y=primary_chunk_y,
                chunk_z=primary_chunk_z,
            )

            self.primary_chunk_key = resolved_primary_chunk_key
            self.primary_chunk_x = primary_chunk_x
            self.primary_chunk_y = primary_chunk_y
            self.primary_chunk_z = primary_chunk_z

        self.touch(updated_by_user_id=updated_by_user_id)

    def replace_metadata(
        self,
        metadata_json: Optional[Mapping[str, Any]],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Replace metadata_json entirely."""
        self.ensure_not_deleted()
        self.metadata_json = normalize_json_object(metadata_json, field_name="metadata_json")
        self.touch(updated_by_user_id=updated_by_user_id)

    def update_metadata(
        self,
        values: Mapping[str, Any],
        *,
        remove_keys: Optional[Iterable[str]] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Merge metadata values and optionally remove keys."""
        self.ensure_not_deleted()

        if not isinstance(values, Mapping):
            raise ValueError("metadata update values must be a JSON object/dict.")

        current = normalize_json_object(self.metadata_json, field_name="metadata_json")

        for key in remove_keys or []:
            try:
                current.pop(str(key), None)
            except Exception:
                continue

        for key, value in values.items():
            current[str(key)] = make_json_safe(value)

        self.metadata_json = current
        self.touch(updated_by_user_id=updated_by_user_id)

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
            normalize_object_instance_id(self.object_instance_id)
        except Exception as exc:
            errors["objectInstanceId"] = str(exc)

        try:
            normalize_object_type_id(self.object_type_id)
        except Exception as exc:
            errors["objectTypeId"] = str(exc)

        try:
            normalize_object_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_object_source(self.object_source)
        except Exception as exc:
            errors["objectSource"] = str(exc)

        try:
            normalize_object_kind(self.object_kind)
        except Exception as exc:
            errors["objectKind"] = str(exc)

        try:
            normalize_anchor_mode(self.anchor_mode)
        except Exception as exc:
            errors["anchorMode"] = str(exc)

        for attr, field_name in (
            ("size_x", "sizeX"),
            ("size_y", "sizeY"),
            ("size_z", "sizeZ"),
        ):
            try:
                normalize_positive_int(getattr(self, attr), field_name=attr)
            except Exception as exc:
                errors[field_name] = str(exc)

        try:
            normalize_non_negative_int(
                self.occupied_cell_count,
                field_name="occupied_cell_count",
                default=0,
            )
        except Exception as exc:
            errors["occupiedCellCount"] = str(exc)

        try:
            normalize_non_negative_int(
                self.touched_chunk_count,
                field_name="touched_chunk_count",
                default=0,
            )
        except Exception as exc:
            errors["touchedChunkCount"] = str(exc)

        if self.primary_chunk_key is not None:
            try:
                chunk_x, chunk_y, chunk_z = parse_chunk_key(self.primary_chunk_key)
                if self.primary_chunk_x is not None:
                    chunk_x = self.primary_chunk_x
                if self.primary_chunk_y is not None:
                    chunk_y = self.primary_chunk_y
                if self.primary_chunk_z is not None:
                    chunk_z = self.primary_chunk_z
                assert_chunk_key_matches(
                    chunk_key=self.primary_chunk_key,
                    chunk_x=chunk_x,
                    chunk_y=chunk_y,
                    chunk_z=chunk_z,
                )
            except Exception as exc:
                errors["primaryChunkKey"] = str(exc)

        if self.revision is None or int(self.revision) < 1:
            errors["revision"] = "revision must be greater than or equal to 1."

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_cells: bool = False,
        include_metadata: bool = True,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
        world_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serialize object instance for API/service responses."""
        result: Dict[str, Any] = {
            "objectInstanceId": self.object_instance_id,
            "projectId": project_id if project_id is not None else self.project_public_id,
            "universeId": universe_id if universe_id is not None else self.universe_public_id,
            "worldId": world_id if world_id is not None else self.world_public_id,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "objectSource": self.object_source,
            "objectKind": self.object_kind,
            "objectTypeId": self.object_type_id,
            "objectVariantId": self.object_variant_id,
            "label": self.label,
            "description": self.description,
            "library": {
                "libraryId": self.library_id,
                "libraryVersion": self.library_version,
                "librarySnapshotId": self.library_snapshot_id,
            },
            "anchorMode": self.anchor_mode,
            "anchor": self.anchor_position,
            "size": self.size,
            "rotation": normalize_json_object(self.rotation_json, field_name="rotation_json"),
            "transform": normalize_json_object(self.transform_json, field_name="transform_json"),
            "bounds": normalize_json_object(self.bounds_json, field_name="bounds_json"),
            "footprint": normalize_json_object(self.footprint_json, field_name="footprint_json"),
            "occupiedCellCount": self.occupied_cell_count,
            "touchedChunks": normalize_json_list(
                self.touched_chunks_json,
                field_name="touched_chunks_json",
            ),
            "touchedChunkCount": self.touched_chunk_count,
            "primaryChunk": self.primary_chunk,
            "createdByCommandId": self.created_by_command_id,
            "updatedByCommandId": self.updated_by_command_id,
            "removedByCommandId": self.removed_by_command_id,
            "createdEventId": self.created_event_id,
            "updatedEventId": self.updated_event_id,
            "removedEventId": self.removed_event_id,
            "contentHash": self.content_hash,
            "createdByUserId": self.created_by_user_id,
            "updatedByUserId": self.updated_by_user_id,
            "lastSessionId": self.last_session_id,
            "createdAt": datetime_to_iso(self.created_at),
            "updatedAt": datetime_to_iso(self.updated_at),
            "archivedAt": datetime_to_iso(self.archived_at),
            "deletedAt": datetime_to_iso(self.deleted_at),
            "flags": {
                "active": self.is_active,
                "archived": self.is_archived,
                "deleted": self.is_deleted,
                "detached": self.is_detached,
            },
        }

        if include_cells:
            result["occupiedCells"] = normalize_json_list(
                self.occupied_cells_json,
                field_name="occupied_cells_json",
            )

        if include_metadata:
            result["metadata"] = normalize_json_object(
                self.metadata_json,
                field_name="metadata_json",
            )

        if include_internal:
            result["id"] = self.id
            result["projectDbId"] = self.project_db_id
            result["universeDbId"] = self.universe_db_id
            result["worldDbId"] = self.world_db_id

        return result


class WorldObjectChunkRef(db.Model):
    """
    Mapping between a WorldObjectInstance and one touched chunk.

    This allows efficient:
    - RemoveObject
    - object lookup by chunk
    - dirty-chunk calculation for large objects
    - object-footprint inspection
    """

    __tablename__ = "world_object_chunk_refs"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    object_instance_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("world_object_instances.id", ondelete="CASCADE"),
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

    object_instance_id = db.Column(
        db.String(OBJECT_INSTANCE_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    status = db.Column(
        db.String(OBJECT_STATUS_MAX_LENGTH),
        nullable=False,
        default=CHUNK_REF_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(SCHEMA_VERSION_MAX_LENGTH),
        nullable=False,
        default=WORLD_OBJECT_CHUNK_REF_SCHEMA_VERSION,
    )

    ref_role = db.Column(
        db.String(OBJECT_REF_ROLE_MAX_LENGTH),
        nullable=False,
        default=CHUNK_REF_ROLE_OCCUPIED,
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

    local_bounds_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    world_bounds_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    occupied_cells_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    occupied_cell_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    object_content_hash = db.Column(
        db.String(CONTENT_HASH_MAX_LENGTH),
        nullable=True,
        index=True,
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

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        index=True,
    )

    deleted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    object_instance = db.relationship(
        "WorldObjectInstance",
        backref=db.backref(
            "chunk_refs",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    project = db.relationship(
        "Project",
        backref=db.backref(
            "world_object_chunk_refs",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    universe = db.relationship(
        "Universe",
        backref=db.backref(
            "world_object_chunk_refs",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    world = db.relationship(
        "WorldInstance",
        backref=db.backref(
            "world_object_chunk_refs",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "object_instance_db_id",
            "chunk_x",
            "chunk_y",
            "chunk_z",
            name="uq_world_object_chunk_refs_object_chunk",
        ),
        db.CheckConstraint(
            "object_instance_db_id > 0",
            name="ck_world_object_chunk_refs_object_instance_db_id_positive",
        ),
        db.CheckConstraint(
            "project_db_id > 0",
            name="ck_world_object_chunk_refs_project_db_id_positive",
        ),
        db.CheckConstraint(
            "universe_db_id > 0",
            name="ck_world_object_chunk_refs_universe_db_id_positive",
        ),
        db.CheckConstraint(
            "world_db_id > 0",
            name="ck_world_object_chunk_refs_world_db_id_positive",
        ),
        db.CheckConstraint(
            "object_instance_id <> ''",
            name="ck_world_object_chunk_refs_object_instance_id_not_empty",
        ),
        db.CheckConstraint(
            "chunk_key <> ''",
            name="ck_world_object_chunk_refs_chunk_key_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'stale', 'deleted')",
            name="ck_world_object_chunk_refs_status_valid",
        ),
        db.CheckConstraint(
            "ref_role IN ('primary', 'occupied', 'boundary', 'dirty_neighbor', 'metadata_only')",
            name="ck_world_object_chunk_refs_ref_role_valid",
        ),
        db.CheckConstraint(
            "occupied_cell_count >= 0",
            name="ck_world_object_chunk_refs_occupied_cell_count_non_negative",
        ),
        db.Index(
            "ix_world_object_chunk_refs_world_chunk",
            "world_db_id",
            "chunk_key",
        ),
        db.Index(
            "ix_world_object_chunk_refs_world_object",
            "world_db_id",
            "object_instance_id",
        ),
        db.Index(
            "ix_world_object_chunk_refs_status_role",
            "status",
            "ref_role",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<WorldObjectChunkRef id={self.id!r} "
            f"object_instance_id={self.object_instance_id!r} "
            f"chunk_key={self.chunk_key!r} status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        object_instance_db_id: int,
        project_db_id: int,
        universe_db_id: int,
        world_db_id: int,
        object_instance_id: str,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        chunk_key: Optional[str] = None,
        status: str = CHUNK_REF_STATUS_ACTIVE,
        ref_role: str = CHUNK_REF_ROLE_OCCUPIED,
        local_bounds_json: Optional[Mapping[str, Any]] = None,
        world_bounds_json: Optional[Mapping[str, Any]] = None,
        occupied_cells_json: Optional[Sequence[Any]] = None,
        object_content_hash: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "WorldObjectChunkRef":
        """Create a WorldObjectChunkRef without adding it to a session."""
        normalized_chunk_x = normalize_chunk_coord(chunk_x, field_name="chunk_x")
        normalized_chunk_y = normalize_chunk_coord(chunk_y, field_name="chunk_y")
        normalized_chunk_z = normalize_chunk_coord(chunk_z, field_name="chunk_z")

        resolved_chunk_key = (
            normalize_chunk_key(chunk_key)
            if chunk_key is not None
            else build_chunk_key(
                normalized_chunk_x,
                normalized_chunk_y,
                normalized_chunk_z,
            )
        )

        assert_chunk_key_matches(
            chunk_key=resolved_chunk_key,
            chunk_x=normalized_chunk_x,
            chunk_y=normalized_chunk_y,
            chunk_z=normalized_chunk_z,
        )

        occupied_cells = normalize_json_list(
            occupied_cells_json,
            field_name="occupied_cells_json",
        )

        return cls(
            object_instance_db_id=normalize_db_id(
                object_instance_db_id,
                field_name="object_instance_db_id",
            ),
            project_db_id=normalize_db_id(project_db_id, field_name="project_db_id"),
            universe_db_id=normalize_db_id(universe_db_id, field_name="universe_db_id"),
            world_db_id=normalize_db_id(world_db_id, field_name="world_db_id"),
            object_instance_id=normalize_object_instance_id(object_instance_id),
            status=normalize_chunk_ref_status(status),
            schema_version=WORLD_OBJECT_CHUNK_REF_SCHEMA_VERSION,
            ref_role=normalize_chunk_ref_role(ref_role),
            chunk_x=normalized_chunk_x,
            chunk_y=normalized_chunk_y,
            chunk_z=normalized_chunk_z,
            chunk_key=resolved_chunk_key,
            local_bounds_json=normalize_bounds_json(
                local_bounds_json,
                field_name="local_bounds_json",
            ),
            world_bounds_json=normalize_bounds_json(
                world_bounds_json,
                field_name="world_bounds_json",
            ),
            occupied_cells_json=occupied_cells,
            occupied_cell_count=count_json_list(occupied_cells),
            object_content_hash=normalize_optional_hash(
                object_content_hash,
                field_name="object_content_hash",
            ),
            metadata_json=normalize_json_object(metadata_json, field_name="metadata_json"),
            created_at=utc_now(),
            updated_at=utc_now(),
        )

    @classmethod
    def create_for_object(
        cls,
        object_instance: WorldObjectInstance,
        *,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        **kwargs: Any,
    ) -> "WorldObjectChunkRef":
        """Create chunk reference for a persisted object instance."""
        object_instance_db_id = getattr(object_instance, "id", None)
        if object_instance_db_id is None:
            raise ValueError(
                "Cannot create chunk ref without persisted object_instance.id."
            )

        return cls.create(
            object_instance_db_id=object_instance_db_id,
            project_db_id=object_instance.project_db_id,
            universe_db_id=object_instance.universe_db_id,
            world_db_id=object_instance.world_db_id,
            object_instance_id=object_instance.object_instance_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            object_content_hash=kwargs.pop("object_content_hash", object_instance.content_hash),
            **kwargs,
        )

    @property
    def is_active(self) -> bool:
        return self.status == CHUNK_REF_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_stale(self) -> bool:
        return self.status == CHUNK_REF_STATUS_STALE

    @property
    def is_deleted(self) -> bool:
        return self.status == CHUNK_REF_STATUS_DELETED or self.deleted_at is not None

    @property
    def chunk_coords(self) -> Dict[str, int]:
        return {
            "chunkX": int(self.chunk_x),
            "chunkY": int(self.chunk_y),
            "chunkZ": int(self.chunk_z),
        }

    def mark_stale(self) -> None:
        """Mark ref as stale."""
        self.status = CHUNK_REF_STATUS_STALE
        self.updated_at = utc_now()

    def restore(self) -> None:
        """Restore stale/deleted ref."""
        self.status = CHUNK_REF_STATUS_ACTIVE
        self.deleted_at = None
        self.updated_at = utc_now()

    def soft_delete(self) -> None:
        """Soft-delete ref."""
        self.status = CHUNK_REF_STATUS_DELETED
        self.deleted_at = self.deleted_at or utc_now()
        self.updated_at = utc_now()

    def replace_occupied_cells(self, occupied_cells_json: Optional[Sequence[Any]]) -> None:
        """Replace occupied cell list."""
        cells = normalize_json_list(
            occupied_cells_json,
            field_name="occupied_cells_json",
        )
        self.occupied_cells_json = cells
        self.occupied_cell_count = count_json_list(cells)
        self.updated_at = utc_now()

    def get_validation_errors(self) -> Dict[str, str]:
        """Return validation errors without raising."""
        errors: Dict[str, str] = {}

        for attr, field_name in (
            ("object_instance_db_id", "objectInstanceDbId"),
            ("project_db_id", "projectDbId"),
            ("universe_db_id", "universeDbId"),
            ("world_db_id", "worldDbId"),
        ):
            try:
                normalize_db_id(getattr(self, attr), field_name=attr)
            except Exception as exc:
                errors[field_name] = str(exc)

        try:
            normalize_object_instance_id(self.object_instance_id)
        except Exception as exc:
            errors["objectInstanceId"] = str(exc)

        try:
            normalize_chunk_ref_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_chunk_ref_role(self.ref_role)
        except Exception as exc:
            errors["refRole"] = str(exc)

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
                self.occupied_cell_count,
                field_name="occupied_cell_count",
                default=0,
            )
        except Exception as exc:
            errors["occupiedCellCount"] = str(exc)

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_cells: bool = False,
    ) -> Dict[str, Any]:
        """Serialize object chunk ref."""
        result: Dict[str, Any] = {
            "objectInstanceId": self.object_instance_id,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "refRole": self.ref_role,
            "chunk": {
                **self.chunk_coords,
                "chunkKey": self.chunk_key,
            },
            "localBounds": normalize_json_object(
                self.local_bounds_json,
                field_name="local_bounds_json",
            ),
            "worldBounds": normalize_json_object(
                self.world_bounds_json,
                field_name="world_bounds_json",
            ),
            "occupiedCellCount": self.occupied_cell_count,
            "objectContentHash": self.object_content_hash,
            "metadata": normalize_json_object(self.metadata_json, field_name="metadata_json"),
            "createdAt": datetime_to_iso(self.created_at),
            "updatedAt": datetime_to_iso(self.updated_at),
            "deletedAt": datetime_to_iso(self.deleted_at),
            "flags": {
                "active": self.is_active,
                "stale": self.is_stale,
                "deleted": self.is_deleted,
            },
        }

        if include_cells:
            result["occupiedCells"] = normalize_json_list(
                self.occupied_cells_json,
                field_name="occupied_cells_json",
            )

        if include_internal:
            result["id"] = self.id
            result["objectInstanceDbId"] = self.object_instance_db_id
            result["projectDbId"] = self.project_db_id
            result["universeDbId"] = self.universe_db_id
            result["worldDbId"] = self.world_db_id

        return result