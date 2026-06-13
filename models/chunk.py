# services/vectoplan-chunk/models/chunk.py
"""
SQLAlchemy model for materialized VECTOPLAN chunk snapshots.

A ChunkSnapshot is the current persisted load-truth for one materialized chunk
inside one concrete WorldInstance.

Current intended hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot

Important design rules:
- Unmodified chunks are generated from the provider/template world.
- Modified chunks are stored as ChunkSnapshot.
- ChunkSnapshot is the normal load-truth for materialized chunks.
- ChunkEvent / WorldCommandLog are historical truth and are not the normal
  chunk-load path.
- `world_db_id + chunk_x + chunk_y + chunk_z` is unique.
- `world_id` alone is not globally unique; use internal `world_db_id`.
- `cellValue = 0` always means Air.
- `cellValue = paletteIndex + 1` means block from this chunk's palette.
- This model stores content in JSON first, but already supports binary,
  compressed and future external content encodings.
- This model already reserves object reference metadata for later multi-block
  objects such as 4x4x2 or 2x1x2 objects.
"""

from __future__ import annotations

import hashlib
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
        "models/chunk.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
        "`db` instance before importing models."
    ) from _DB_IMPORT_ERROR


try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - fallback is useful for tests/non-postgres tooling
    JSONB = None  # type: ignore[assignment]


JSON_COLUMN_TYPE = JSONB if JSONB is not None else db.JSON


CHUNK_SNAPSHOT_SCHEMA_VERSION = "chunk-snapshot.schema.v1"
RUNTIME_CHUNK_CONTENT_VERSION = "runtime-chunk-content.v1"

CELL_ENCODING_VERSION = "cell-encoding.palette-index-plus-one.v1"
CELL_INDEX_ORDER_X_FASTEST = "x-fastest-y-then-z"
AIR_CELL_VALUE = 0
BLOCK_CELL_VALUE_RULE = "paletteIndex + 1"

DEFAULT_CHUNK_SIZE = 16
DEFAULT_CELL_SIZE = 1.0
DEFAULT_BLOCK_REGISTRY_ID = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION = "1"
DEFAULT_COORDINATE_SYSTEM = "vectoplan-world-y-up-v1"
DEFAULT_PROJECTION_TYPE = "flat-local-v1"
DEFAULT_TOPOLOGY_TYPE = "flat-unbounded-v1"

DEFAULT_TEMPLATE_ID = "flat"
DEFAULT_PROVIDER_ID = "flat"
DEFAULT_PROVIDER_WORLD_ID = "flat"
DEFAULT_GENERATOR_TYPE = "flat-world"
DEFAULT_GENERATOR_VERSION = "1"

SNAPSHOT_STATUS_ACTIVE = "active"
SNAPSHOT_STATUS_ARCHIVED = "archived"
SNAPSHOT_STATUS_DELETED = "deleted"

VALID_SNAPSHOT_STATUSES = frozenset(
    {
        SNAPSHOT_STATUS_ACTIVE,
        SNAPSHOT_STATUS_ARCHIVED,
        SNAPSHOT_STATUS_DELETED,
    }
)

SNAPSHOT_CONTENT_ENCODING_JSON = "json"
SNAPSHOT_CONTENT_ENCODING_BINARY = "binary"
SNAPSHOT_CONTENT_ENCODING_JSON_GZIP = "json_gzip"
SNAPSHOT_CONTENT_ENCODING_RLE_JSON = "rle_json"
SNAPSHOT_CONTENT_ENCODING_EXTERNAL_REF = "external_ref"

VALID_CONTENT_ENCODINGS = frozenset(
    {
        SNAPSHOT_CONTENT_ENCODING_JSON,
        SNAPSHOT_CONTENT_ENCODING_BINARY,
        SNAPSHOT_CONTENT_ENCODING_JSON_GZIP,
        SNAPSHOT_CONTENT_ENCODING_RLE_JSON,
        SNAPSHOT_CONTENT_ENCODING_EXTERNAL_REF,
    }
)

SNAPSHOT_SOURCE_COMMAND = "command"
SNAPSHOT_SOURCE_IMPORT = "import"
SNAPSHOT_SOURCE_MIGRATION = "migration"
SNAPSHOT_SOURCE_SYSTEM = "system"
SNAPSHOT_SOURCE_MATERIALIZED_GENERATED = "materialized_generated"

VALID_SNAPSHOT_SOURCES = frozenset(
    {
        SNAPSHOT_SOURCE_COMMAND,
        SNAPSHOT_SOURCE_IMPORT,
        SNAPSHOT_SOURCE_MIGRATION,
        SNAPSHOT_SOURCE_SYSTEM,
        SNAPSHOT_SOURCE_MATERIALIZED_GENERATED,
    }
)

MATERIALIZED_REASON_SET_BLOCK = "set_block"
MATERIALIZED_REASON_REMOVE_BLOCK = "remove_block"
MATERIALIZED_REASON_REPLACE_BLOCK = "replace_block"
MATERIALIZED_REASON_BATCH_COMMAND = "batch_command"
MATERIALIZED_REASON_OBJECT_PLACEMENT = "object_placement"
MATERIALIZED_REASON_OBJECT_REMOVAL = "object_removal"
MATERIALIZED_REASON_IMPORT = "import"
MATERIALIZED_REASON_MIGRATION = "migration"
MATERIALIZED_REASON_MANUAL = "manual"
MATERIALIZED_REASON_SYSTEM = "system"

VALID_MATERIALIZED_REASONS = frozenset(
    {
        MATERIALIZED_REASON_SET_BLOCK,
        MATERIALIZED_REASON_REMOVE_BLOCK,
        MATERIALIZED_REASON_REPLACE_BLOCK,
        MATERIALIZED_REASON_BATCH_COMMAND,
        MATERIALIZED_REASON_OBJECT_PLACEMENT,
        MATERIALIZED_REASON_OBJECT_REMOVAL,
        MATERIALIZED_REASON_IMPORT,
        MATERIALIZED_REASON_MIGRATION,
        MATERIALIZED_REASON_MANUAL,
        MATERIALIZED_REASON_SYSTEM,
    }
)

SNAPSHOT_ID_MAX_LENGTH = 128
CHUNK_KEY_MAX_LENGTH = 96
CHUNK_VERSION_MAX_LENGTH = 64
SCHEMA_VERSION_MAX_LENGTH = 64
RUNTIME_CONTENT_VERSION_MAX_LENGTH = 96
CELL_ENCODING_VERSION_MAX_LENGTH = 96
CELL_INDEX_ORDER_MAX_LENGTH = 64
CONTENT_ENCODING_MAX_LENGTH = 64
CONTENT_HASH_MAX_LENGTH = 128
STATUS_MAX_LENGTH = 32
SOURCE_MAX_LENGTH = 64
MATERIALIZED_REASON_MAX_LENGTH = 96

BLOCK_REGISTRY_ID_MAX_LENGTH = 128
BLOCK_REGISTRY_VERSION_MAX_LENGTH = 64
TEMPLATE_ID_MAX_LENGTH = 96
PROVIDER_ID_MAX_LENGTH = 96
PROVIDER_WORLD_ID_MAX_LENGTH = 96
GENERATOR_TYPE_MAX_LENGTH = 96
GENERATOR_VERSION_MAX_LENGTH = 64
COORDINATE_SYSTEM_MAX_LENGTH = 128
PROJECTION_TYPE_MAX_LENGTH = 96
TOPOLOGY_TYPE_MAX_LENGTH = 96
COMMAND_ID_MAX_LENGTH = 128
EVENT_ID_MAX_LENGTH = 128
USER_ID_MAX_LENGTH = 128
SESSION_ID_MAX_LENGTH = 128

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

    Snapshot payloads can later be produced by editor, importers, object tools,
    migration tools or AI tooling. This helper keeps metadata/content safe.
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
            "sha256": hashlib.sha256(value).hexdigest(),
        }

    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


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

    This is intentionally broad enough for command ids, library ids and future
    namespaced ids.
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


def generate_snapshot_id(prefix: str = "chunk_snap") -> str:
    """Generate a stable public snapshot id."""
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="snapshot_id_prefix",
        max_length=32,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def normalize_snapshot_id(value: Any) -> str:
    """Normalize snapshot_id."""
    return normalize_public_id(
        value,
        field_name="snapshot_id",
        max_length=SNAPSHOT_ID_MAX_LENGTH,
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


def normalize_chunk_coord(value: Any, *, field_name: str) -> int:
    """Normalize chunk coordinates."""
    if value is None:
        raise ValueError(f"{field_name} is required.")

    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def build_chunk_key(chunk_x: int, chunk_y: int, chunk_z: int) -> str:
    """Build canonical chunk key."""
    return f"{int(chunk_x)}:{int(chunk_y)}:{int(chunk_z)}"


def parse_chunk_key(chunk_key: str) -> Tuple[int, int, int]:
    """Parse a canonical chunk key into coordinates."""
    normalized_key = normalize_chunk_key(chunk_key)

    try:
        raw_x, raw_y, raw_z = normalized_key.split(":")
        return int(raw_x), int(raw_y), int(raw_z)
    except Exception as exc:
        raise ValueError(f"Invalid chunk_key '{chunk_key}'.") from exc


def normalize_chunk_key(value: Any) -> str:
    """Normalize chunk key values."""
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


def assert_chunk_key_matches(
    *,
    chunk_key: str,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
) -> None:
    """Validate that a chunk key matches coordinates."""
    expected = build_chunk_key(chunk_x, chunk_y, chunk_z)
    if normalize_chunk_key(chunk_key) != expected:
        raise ValueError(
            f"chunk_key '{chunk_key}' does not match coordinates. "
            f"Expected '{expected}'."
        )


def normalize_positive_int(
    value: Any,
    *,
    field_name: str,
    default: int,
) -> int:
    """Normalize positive integer values."""
    if value is None:
        value = default

    try:
        result = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

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


def normalize_positive_float(
    value: Any,
    *,
    field_name: str,
    default: float,
) -> float:
    """Normalize positive float values."""
    if value is None:
        value = default

    try:
        result = float(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number.") from exc

    if result <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    return result


def normalize_status(value: Any) -> str:
    """Normalize and validate snapshot status."""
    if value is None:
        return SNAPSHOT_STATUS_ACTIVE

    status = str(value).strip().lower()

    if status not in VALID_SNAPSHOT_STATUSES:
        allowed = ", ".join(sorted(VALID_SNAPSHOT_STATUSES))
        raise ValueError(f"Invalid snapshot status '{value}'. Allowed: {allowed}.")

    return status


def normalize_content_encoding(value: Any) -> str:
    """Normalize and validate content encoding."""
    if value is None:
        return SNAPSHOT_CONTENT_ENCODING_JSON

    encoding = str(value).strip().lower()

    if encoding not in VALID_CONTENT_ENCODINGS:
        allowed = ", ".join(sorted(VALID_CONTENT_ENCODINGS))
        raise ValueError(f"Invalid content encoding '{value}'. Allowed: {allowed}.")

    return encoding


def normalize_snapshot_source(value: Any) -> str:
    """Normalize and validate snapshot source."""
    if value is None:
        return SNAPSHOT_SOURCE_COMMAND

    source = str(value).strip().lower()

    if source not in VALID_SNAPSHOT_SOURCES:
        allowed = ", ".join(sorted(VALID_SNAPSHOT_SOURCES))
        raise ValueError(f"Invalid snapshot source '{value}'. Allowed: {allowed}.")

    return source


def normalize_materialized_reason(value: Any) -> str:
    """Normalize and validate materialized reason."""
    if value is None:
        return MATERIALIZED_REASON_SET_BLOCK

    reason = str(value).strip().lower()

    if reason not in VALID_MATERIALIZED_REASONS:
        allowed = ", ".join(sorted(VALID_MATERIALIZED_REASONS))
        raise ValueError(
            f"Invalid materialized reason '{value}'. Allowed: {allowed}."
        )

    return reason


def normalize_version_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    default: str,
) -> str:
    """Normalize version strings."""
    if value is None:
        value = default

    return normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )


def format_chunk_version(chunk_revision: int) -> str:
    """Create public chunk version string from integer revision."""
    revision = normalize_positive_int(
        chunk_revision,
        field_name="chunk_revision",
        default=1,
    )
    return f"chunk_rev_{revision:06d}"


def normalize_chunk_version(
    value: Any,
    *,
    chunk_revision: int,
) -> str:
    """Normalize public chunk_version or derive it from revision."""
    if value is None:
        return format_chunk_version(chunk_revision)

    return normalize_required_text(
        value,
        field_name="chunk_version",
        max_length=CHUNK_VERSION_MAX_LENGTH,
    )


def stable_json_dumps(value: Any) -> str:
    """Serialize JSON content in a stable way for hashing."""
    safe_value = make_json_safe(value)
    return json.dumps(
        safe_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def normalize_content_json(value: Any) -> Optional[Dict[str, Any]]:
    """Normalize snapshot content_json."""
    if value is None:
        return None

    if not isinstance(value, Mapping):
        raise ValueError("content_json must be a JSON object/dict.")

    return make_json_safe(dict(value))


def normalize_content_binary(value: Any) -> Optional[bytes]:
    """Normalize content_binary."""
    if value is None:
        return None

    if isinstance(value, bytes):
        return value

    if isinstance(value, bytearray):
        return bytes(value)

    raise ValueError("content_binary must be bytes or bytearray.")


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


def compute_content_hash(
    *,
    content_json: Optional[Mapping[str, Any]] = None,
    content_binary: Optional[bytes] = None,
) -> str:
    """
    Compute a stable SHA-256 hash for snapshot content.

    If JSON and binary content are both present, both are included. This keeps
    the hash useful for future hybrid formats.
    """
    if content_json is None and content_binary is None:
        raise ValueError("Cannot compute content hash without content.")

    digest = hashlib.sha256()

    if content_json is not None:
        digest.update(b"json:")
        digest.update(stable_json_dumps(content_json).encode("utf-8"))

    if content_binary is not None:
        digest.update(b"|binary:")
        digest.update(content_binary)

    return digest.hexdigest()


def estimate_content_size_bytes(
    *,
    content_json: Optional[Mapping[str, Any]] = None,
    content_binary: Optional[bytes] = None,
) -> int:
    """Estimate serialized content size in bytes."""
    total = 0

    if content_json is not None:
        total += len(stable_json_dumps(content_json).encode("utf-8"))

    if content_binary is not None:
        total += len(content_binary)

    return total


def normalize_content_hash(value: Any) -> str:
    """Normalize content_hash."""
    text = normalize_required_text(
        value,
        field_name="content_hash",
        max_length=CONTENT_HASH_MAX_LENGTH,
    )

    if not HASH_PATTERN.match(text):
        raise ValueError("content_hash contains unsupported characters.")

    return text


def extract_cells_from_content(content_json: Optional[Mapping[str, Any]]) -> List[Any]:
    """Best-effort extraction of cells from runtime chunk content."""
    if not isinstance(content_json, Mapping):
        return []

    candidates = [
        content_json.get("cells"),
        content_json.get("cellValues"),
    ]

    chunk_value = content_json.get("chunk")
    if isinstance(chunk_value, Mapping):
        candidates.extend(
            [
                chunk_value.get("cells"),
                chunk_value.get("cellValues"),
            ]
        )

    runtime_value = content_json.get("runtimeContent")
    if isinstance(runtime_value, Mapping):
        candidates.extend(
            [
                runtime_value.get("cells"),
                runtime_value.get("cellValues"),
            ]
        )

    for candidate in candidates:
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
            return list(candidate)

    return []


def extract_palette_from_content(content_json: Optional[Mapping[str, Any]]) -> List[Any]:
    """Best-effort extraction of palette from runtime chunk content."""
    if not isinstance(content_json, Mapping):
        return []

    candidates = [
        content_json.get("palette"),
    ]

    chunk_value = content_json.get("chunk")
    if isinstance(chunk_value, Mapping):
        candidates.append(chunk_value.get("palette"))

    runtime_value = content_json.get("runtimeContent")
    if isinstance(runtime_value, Mapping):
        candidates.append(runtime_value.get("palette"))

    for candidate in candidates:
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
            return make_json_safe(list(candidate))

    return []


def extract_object_refs_from_content(content_json: Optional[Mapping[str, Any]]) -> List[Any]:
    """
    Best-effort extraction of object references from runtime chunk content.

    This prepares later multi-block objects. For the first SetBlock/RemoveBlock
    slice this will usually be empty.
    """
    if not isinstance(content_json, Mapping):
        return []

    candidates = [
        content_json.get("objectRefs"),
        content_json.get("objects"),
        content_json.get("objectReferences"),
    ]

    chunk_value = content_json.get("chunk")
    if isinstance(chunk_value, Mapping):
        candidates.extend(
            [
                chunk_value.get("objectRefs"),
                chunk_value.get("objects"),
                chunk_value.get("objectReferences"),
            ]
        )

    for candidate in candidates:
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
            return make_json_safe(list(candidate))

    return []


def build_stats_from_content(
    content_json: Optional[Mapping[str, Any]],
    *,
    fallback_cell_count: int,
) -> Dict[str, Any]:
    """Build basic stats from runtime chunk content."""
    cells = extract_cells_from_content(content_json)
    cell_count = len(cells) if cells else int(fallback_cell_count)

    non_air_cell_count = 0
    air_cell_count = None

    if cells:
        try:
            non_air_cell_count = sum(1 for value in cells if int(value) != AIR_CELL_VALUE)
            air_cell_count = cell_count - non_air_cell_count
        except Exception:
            non_air_cell_count = 0
            air_cell_count = None

    stats: Dict[str, Any] = {
        "cellCount": cell_count,
        "nonAirCellCount": non_air_cell_count,
    }

    if air_cell_count is not None:
        stats["airCellCount"] = air_cell_count

    if isinstance(content_json, Mapping):
        existing_stats = content_json.get("stats") or content_json.get("generationStats")
        if isinstance(existing_stats, Mapping):
            stats.update(make_json_safe(dict(existing_stats)))

    return stats


class ChunkSnapshot(db.Model):
    """
    Current materialized state of one chunk.

    A row exists only when a chunk has been changed or explicitly materialized.
    If no row exists for a requested chunk, the chunk service should generate
    the chunk from the world/provider configuration.
    """

    __tablename__ = "chunk_snapshots"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    snapshot_id = db.Column(
        db.String(SNAPSHOT_ID_MAX_LENGTH),
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

    status = db.Column(
        db.String(STATUS_MAX_LENGTH),
        nullable=False,
        default=SNAPSHOT_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(SCHEMA_VERSION_MAX_LENGTH),
        nullable=False,
        default=CHUNK_SNAPSHOT_SCHEMA_VERSION,
    )

    runtime_content_version = db.Column(
        db.String(RUNTIME_CONTENT_VERSION_MAX_LENGTH),
        nullable=False,
        default=RUNTIME_CHUNK_CONTENT_VERSION,
    )

    chunk_revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        index=True,
    )

    chunk_version = db.Column(
        db.String(CHUNK_VERSION_MAX_LENGTH),
        nullable=False,
        default="chunk_rev_000001",
        index=True,
    )

    content_encoding = db.Column(
        db.String(CONTENT_ENCODING_MAX_LENGTH),
        nullable=False,
        default=SNAPSHOT_CONTENT_ENCODING_JSON,
        index=True,
    )

    content_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=True,
    )

    content_binary = db.Column(
        db.LargeBinary,
        nullable=True,
    )

    content_hash = db.Column(
        db.String(CONTENT_HASH_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    content_size_bytes = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
    )

    palette_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    object_refs_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=list,
    )

    object_ref_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    has_object_refs = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        index=True,
    )

    stats_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    metadata_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    cell_count = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_CHUNK_SIZE * DEFAULT_CHUNK_SIZE * DEFAULT_CHUNK_SIZE,
    )

    non_air_cell_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        index=True,
    )

    chunk_size = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_CHUNK_SIZE,
    )

    cell_size = db.Column(
        db.Float,
        nullable=False,
        default=DEFAULT_CELL_SIZE,
    )

    cell_index_order = db.Column(
        db.String(CELL_INDEX_ORDER_MAX_LENGTH),
        nullable=False,
        default=CELL_INDEX_ORDER_X_FASTEST,
    )

    cell_encoding_version = db.Column(
        db.String(CELL_ENCODING_VERSION_MAX_LENGTH),
        nullable=False,
        default=CELL_ENCODING_VERSION,
    )

    air_cell_value = db.Column(
        db.Integer,
        nullable=False,
        default=AIR_CELL_VALUE,
    )

    block_cell_value_rule = db.Column(
        db.String(64),
        nullable=False,
        default=BLOCK_CELL_VALUE_RULE,
    )

    block_registry_id = db.Column(
        db.String(BLOCK_REGISTRY_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_BLOCK_REGISTRY_ID,
        index=True,
    )

    block_registry_version = db.Column(
        db.String(BLOCK_REGISTRY_VERSION_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_BLOCK_REGISTRY_VERSION,
        index=True,
    )

    coordinate_system = db.Column(
        db.String(COORDINATE_SYSTEM_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_COORDINATE_SYSTEM,
        index=True,
    )

    projection_type = db.Column(
        db.String(PROJECTION_TYPE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_PROJECTION_TYPE,
        index=True,
    )

    topology_type = db.Column(
        db.String(TOPOLOGY_TYPE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_TOPOLOGY_TYPE,
        index=True,
    )

    template_id = db.Column(
        db.String(TEMPLATE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    provider_id = db.Column(
        db.String(PROVIDER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    provider_world_id = db.Column(
        db.String(PROVIDER_WORLD_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    generator_type = db.Column(
        db.String(GENERATOR_TYPE_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    generator_version = db.Column(
        db.String(GENERATOR_VERSION_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    snapshot_source = db.Column(
        db.String(SOURCE_MAX_LENGTH),
        nullable=False,
        default=SNAPSHOT_SOURCE_COMMAND,
        index=True,
    )

    materialized_reason = db.Column(
        db.String(MATERIALIZED_REASON_MAX_LENGTH),
        nullable=False,
        default=MATERIALIZED_REASON_SET_BLOCK,
        index=True,
    )

    last_command_id = db.Column(
        db.String(COMMAND_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    last_event_id = db.Column(
        db.String(EVENT_ID_MAX_LENGTH),
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
            "chunk_snapshots",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    universe = db.relationship(
        "Universe",
        backref=db.backref(
            "chunk_snapshots",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    world = db.relationship(
        "WorldInstance",
        backref=db.backref(
            "chunk_snapshots",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "world_db_id",
            "chunk_x",
            "chunk_y",
            "chunk_z",
            name="uq_chunk_snapshots_world_chunk_coords",
        ),
        db.CheckConstraint(
            "project_db_id > 0",
            name="ck_chunk_snapshots_project_db_id_positive",
        ),
        db.CheckConstraint(
            "universe_db_id > 0",
            name="ck_chunk_snapshots_universe_db_id_positive",
        ),
        db.CheckConstraint(
            "world_db_id > 0",
            name="ck_chunk_snapshots_world_db_id_positive",
        ),
        db.CheckConstraint(
            "chunk_key <> ''",
            name="ck_chunk_snapshots_chunk_key_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_chunk_snapshots_status_valid",
        ),
        db.CheckConstraint(
            "chunk_revision >= 1",
            name="ck_chunk_snapshots_chunk_revision_positive",
        ),
        db.CheckConstraint(
            "content_encoding IN ('json', 'binary', 'json_gzip', 'rle_json', 'external_ref')",
            name="ck_chunk_snapshots_content_encoding_valid",
        ),
        db.CheckConstraint(
            "content_json IS NOT NULL OR content_binary IS NOT NULL",
            name="ck_chunk_snapshots_has_content",
        ),
        db.CheckConstraint(
            "content_size_bytes >= 0",
            name="ck_chunk_snapshots_content_size_non_negative",
        ),
        db.CheckConstraint(
            "object_ref_count >= 0",
            name="ck_chunk_snapshots_object_ref_count_non_negative",
        ),
        db.CheckConstraint(
            "cell_count >= 0",
            name="ck_chunk_snapshots_cell_count_non_negative",
        ),
        db.CheckConstraint(
            "non_air_cell_count >= 0",
            name="ck_chunk_snapshots_non_air_cell_count_non_negative",
        ),
        db.CheckConstraint(
            "chunk_size > 0",
            name="ck_chunk_snapshots_chunk_size_positive",
        ),
        db.CheckConstraint(
            "cell_size > 0",
            name="ck_chunk_snapshots_cell_size_positive",
        ),
        db.CheckConstraint(
            "air_cell_value = 0",
            name="ck_chunk_snapshots_air_cell_value_zero",
        ),
        db.CheckConstraint(
            "snapshot_source IN ('command', 'import', 'migration', 'system', 'materialized_generated')",
            name="ck_chunk_snapshots_snapshot_source_valid",
        ),
        db.CheckConstraint(
            "materialized_reason IN ('set_block', 'remove_block', 'replace_block', 'batch_command', 'object_placement', 'object_removal', 'import', 'migration', 'manual', 'system')",
            name="ck_chunk_snapshots_materialized_reason_valid",
        ),
        db.Index(
            "ix_chunk_snapshots_project_universe_world",
            "project_db_id",
            "universe_db_id",
            "world_db_id",
        ),
        db.Index(
            "ix_chunk_snapshots_world_chunk_key",
            "world_db_id",
            "chunk_key",
        ),
        db.Index(
            "ix_chunk_snapshots_world_status_updated",
            "world_db_id",
            "status",
            "updated_at",
        ),
        db.Index(
            "ix_chunk_snapshots_world_revision",
            "world_db_id",
            "chunk_revision",
        ),
        db.Index(
            "ix_chunk_snapshots_registry",
            "block_registry_id",
            "block_registry_version",
        ),
        db.Index(
            "ix_chunk_snapshots_provider_generator",
            "provider_id",
            "provider_world_id",
            "generator_type",
            "generator_version",
        ),
        db.Index(
            "ix_chunk_snapshots_last_command",
            "last_command_id",
            "last_event_id",
        ),
        db.Index(
            "ix_chunk_snapshots_object_refs",
            "has_object_refs",
            "object_ref_count",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ChunkSnapshot id={self.id!r} world_db_id={self.world_db_id!r} "
            f"chunk_key={self.chunk_key!r} chunk_version={self.chunk_version!r} "
            f"status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: int,
        universe_db_id: int,
        world_db_id: int,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        snapshot_id: Optional[str] = None,
        chunk_key: Optional[str] = None,
        status: str = SNAPSHOT_STATUS_ACTIVE,
        content_json: Optional[Mapping[str, Any]] = None,
        content_binary: Optional[bytes] = None,
        content_encoding: str = SNAPSHOT_CONTENT_ENCODING_JSON,
        palette_json: Optional[Sequence[Any]] = None,
        object_refs_json: Optional[Sequence[Any]] = None,
        stats_json: Optional[Mapping[str, Any]] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        chunk_revision: int = 1,
        chunk_version: Optional[str] = None,
        runtime_content_version: str = RUNTIME_CHUNK_CONTENT_VERSION,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        cell_size: float = DEFAULT_CELL_SIZE,
        cell_count: Optional[int] = None,
        non_air_cell_count: Optional[int] = None,
        cell_index_order: str = CELL_INDEX_ORDER_X_FASTEST,
        cell_encoding_version: str = CELL_ENCODING_VERSION,
        block_registry_id: str = DEFAULT_BLOCK_REGISTRY_ID,
        block_registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION,
        coordinate_system: str = DEFAULT_COORDINATE_SYSTEM,
        projection_type: str = DEFAULT_PROJECTION_TYPE,
        topology_type: str = DEFAULT_TOPOLOGY_TYPE,
        template_id: Optional[str] = DEFAULT_TEMPLATE_ID,
        provider_id: Optional[str] = DEFAULT_PROVIDER_ID,
        provider_world_id: Optional[str] = DEFAULT_PROVIDER_WORLD_ID,
        generator_type: Optional[str] = DEFAULT_GENERATOR_TYPE,
        generator_version: Optional[str] = DEFAULT_GENERATOR_VERSION,
        snapshot_source: str = SNAPSHOT_SOURCE_COMMAND,
        materialized_reason: str = MATERIALIZED_REASON_SET_BLOCK,
        last_command_id: Optional[str] = None,
        last_event_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        last_session_id: Optional[str] = None,
    ) -> "ChunkSnapshot":
        """
        Create a ChunkSnapshot instance without adding it to a session.

        Repository/service code is responsible for:
        - checking project/universe/world existence
        - checking that universe belongs to project
        - checking that world belongs to universe
        - upserting against unique(world_db_id, chunk_x, chunk_y, chunk_z)
        - adding to db.session
        - committing or rolling back
        """
        normalized_project_db_id = normalize_db_id(
            project_db_id,
            field_name="project_db_id",
        )
        normalized_universe_db_id = normalize_db_id(
            universe_db_id,
            field_name="universe_db_id",
        )
        normalized_world_db_id = normalize_db_id(
            world_db_id,
            field_name="world_db_id",
        )

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

        normalized_content_json = normalize_content_json(content_json)
        normalized_content_binary = normalize_content_binary(content_binary)

        if normalized_content_json is None and normalized_content_binary is None:
            raise ValueError("ChunkSnapshot requires content_json or content_binary.")

        normalized_content_encoding = normalize_content_encoding(content_encoding)

        normalized_chunk_revision = normalize_positive_int(
            chunk_revision,
            field_name="chunk_revision",
            default=1,
        )

        normalized_chunk_size = normalize_positive_int(
            chunk_size,
            field_name="chunk_size",
            default=DEFAULT_CHUNK_SIZE,
        )

        fallback_cell_count = normalized_chunk_size * normalized_chunk_size * normalized_chunk_size

        resolved_palette = (
            normalize_json_list(palette_json, field_name="palette_json")
            if palette_json is not None
            else extract_palette_from_content(normalized_content_json)
        )

        resolved_object_refs = (
            normalize_json_list(object_refs_json, field_name="object_refs_json")
            if object_refs_json is not None
            else extract_object_refs_from_content(normalized_content_json)
        )

        resolved_stats = (
            normalize_json_object(stats_json, field_name="stats_json")
            if stats_json is not None
            else build_stats_from_content(
                normalized_content_json,
                fallback_cell_count=fallback_cell_count,
            )
        )

        resolved_cell_count = normalize_non_negative_int(
            cell_count
            if cell_count is not None
            else resolved_stats.get("cellCount", fallback_cell_count),
            field_name="cell_count",
            default=fallback_cell_count,
        )

        resolved_non_air_cell_count = normalize_non_negative_int(
            non_air_cell_count
            if non_air_cell_count is not None
            else resolved_stats.get("nonAirCellCount", 0),
            field_name="non_air_cell_count",
            default=0,
        )

        content_hash = compute_content_hash(
            content_json=normalized_content_json,
            content_binary=normalized_content_binary,
        )
        content_size_bytes = estimate_content_size_bytes(
            content_json=normalized_content_json,
            content_binary=normalized_content_binary,
        )

        now = utc_now()
        normalized_status = normalize_status(status)

        return cls(
            snapshot_id=normalize_snapshot_id(snapshot_id or generate_snapshot_id()),
            project_db_id=normalized_project_db_id,
            universe_db_id=normalized_universe_db_id,
            world_db_id=normalized_world_db_id,
            chunk_x=normalized_chunk_x,
            chunk_y=normalized_chunk_y,
            chunk_z=normalized_chunk_z,
            chunk_key=resolved_chunk_key,
            status=normalized_status,
            schema_version=CHUNK_SNAPSHOT_SCHEMA_VERSION,
            runtime_content_version=normalize_required_text(
                runtime_content_version,
                field_name="runtime_content_version",
                max_length=RUNTIME_CONTENT_VERSION_MAX_LENGTH,
            ),
            chunk_revision=normalized_chunk_revision,
            chunk_version=normalize_chunk_version(
                chunk_version,
                chunk_revision=normalized_chunk_revision,
            ),
            content_encoding=normalized_content_encoding,
            content_json=normalized_content_json,
            content_binary=normalized_content_binary,
            content_hash=content_hash,
            content_size_bytes=content_size_bytes,
            palette_json=resolved_palette,
            object_refs_json=resolved_object_refs,
            object_ref_count=len(resolved_object_refs),
            has_object_refs=len(resolved_object_refs) > 0,
            stats_json=resolved_stats,
            metadata_json=normalize_json_object(metadata_json, field_name="metadata_json"),
            cell_count=resolved_cell_count,
            non_air_cell_count=resolved_non_air_cell_count,
            chunk_size=normalized_chunk_size,
            cell_size=normalize_positive_float(
                cell_size,
                field_name="cell_size",
                default=DEFAULT_CELL_SIZE,
            ),
            cell_index_order=normalize_required_text(
                cell_index_order,
                field_name="cell_index_order",
                max_length=CELL_INDEX_ORDER_MAX_LENGTH,
            ),
            cell_encoding_version=normalize_required_text(
                cell_encoding_version,
                field_name="cell_encoding_version",
                max_length=CELL_ENCODING_VERSION_MAX_LENGTH,
            ),
            air_cell_value=AIR_CELL_VALUE,
            block_cell_value_rule=BLOCK_CELL_VALUE_RULE,
            block_registry_id=normalize_required_text(
                block_registry_id,
                field_name="block_registry_id",
                max_length=BLOCK_REGISTRY_ID_MAX_LENGTH,
            ),
            block_registry_version=normalize_version_text(
                block_registry_version,
                field_name="block_registry_version",
                max_length=BLOCK_REGISTRY_VERSION_MAX_LENGTH,
                default=DEFAULT_BLOCK_REGISTRY_VERSION,
            ),
            coordinate_system=normalize_required_text(
                coordinate_system,
                field_name="coordinate_system",
                max_length=COORDINATE_SYSTEM_MAX_LENGTH,
            ),
            projection_type=normalize_required_text(
                projection_type,
                field_name="projection_type",
                max_length=PROJECTION_TYPE_MAX_LENGTH,
            ),
            topology_type=normalize_required_text(
                topology_type,
                field_name="topology_type",
                max_length=TOPOLOGY_TYPE_MAX_LENGTH,
            ),
            template_id=normalize_optional_public_id(
                template_id,
                field_name="template_id",
                max_length=TEMPLATE_ID_MAX_LENGTH,
            ),
            provider_id=normalize_optional_public_id(
                provider_id,
                field_name="provider_id",
                max_length=PROVIDER_ID_MAX_LENGTH,
            ),
            provider_world_id=normalize_optional_public_id(
                provider_world_id,
                field_name="provider_world_id",
                max_length=PROVIDER_WORLD_ID_MAX_LENGTH,
            ),
            generator_type=normalize_optional_text(
                generator_type,
                field_name="generator_type",
                max_length=GENERATOR_TYPE_MAX_LENGTH,
            ),
            generator_version=normalize_optional_text(
                generator_version,
                field_name="generator_version",
                max_length=GENERATOR_VERSION_MAX_LENGTH,
            ),
            snapshot_source=normalize_snapshot_source(snapshot_source),
            materialized_reason=normalize_materialized_reason(materialized_reason),
            last_command_id=normalize_optional_public_id(
                last_command_id,
                field_name="last_command_id",
                max_length=COMMAND_ID_MAX_LENGTH,
            ),
            last_event_id=normalize_optional_public_id(
                last_event_id,
                field_name="last_event_id",
                max_length=EVENT_ID_MAX_LENGTH,
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
            created_at=now,
            updated_at=now,
            archived_at=now if normalized_status == SNAPSHOT_STATUS_ARCHIVED else None,
            deleted_at=now if normalized_status == SNAPSHOT_STATUS_DELETED else None,
        )

    @classmethod
    def create_for_world(
        cls,
        world: Any,
        *,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        content_json: Optional[Mapping[str, Any]] = None,
        content_binary: Optional[bytes] = None,
        materialized_reason: str = MATERIALIZED_REASON_SET_BLOCK,
        snapshot_source: str = SNAPSHOT_SOURCE_COMMAND,
        last_command_id: Optional[str] = None,
        last_event_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        last_session_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        **overrides: Any,
    ) -> "ChunkSnapshot":
        """
        Create a snapshot for a persisted WorldInstance.

        The world instance must already have internal database ids.
        """
        project_db_id = getattr(world, "project_db_id", None)
        universe_db_id = getattr(world, "universe_db_id", None)
        world_db_id = getattr(world, "id", None)

        if project_db_id is None:
            raise ValueError("Cannot create chunk snapshot without world.project_db_id.")

        if universe_db_id is None:
            raise ValueError("Cannot create chunk snapshot without world.universe_db_id.")

        if world_db_id is None:
            raise ValueError("Cannot create chunk snapshot without persisted world.id.")

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_db_id=world_db_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            content_json=content_json,
            content_binary=content_binary,
            chunk_size=overrides.pop("chunk_size", getattr(world, "chunk_size", DEFAULT_CHUNK_SIZE)),
            cell_size=overrides.pop("cell_size", getattr(world, "cell_size", DEFAULT_CELL_SIZE)),
            block_registry_id=overrides.pop("block_registry_id", getattr(world, "block_registry_id", DEFAULT_BLOCK_REGISTRY_ID)),
            block_registry_version=overrides.pop("block_registry_version", getattr(world, "block_registry_version", DEFAULT_BLOCK_REGISTRY_VERSION)),
            coordinate_system=overrides.pop("coordinate_system", getattr(world, "coordinate_system", DEFAULT_COORDINATE_SYSTEM)),
            projection_type=overrides.pop("projection_type", getattr(world, "projection_type", DEFAULT_PROJECTION_TYPE)),
            topology_type=overrides.pop("topology_type", getattr(world, "topology_type", DEFAULT_TOPOLOGY_TYPE)),
            template_id=overrides.pop("template_id", getattr(world, "template_id", DEFAULT_TEMPLATE_ID)),
            provider_id=overrides.pop("provider_id", getattr(world, "provider_id", DEFAULT_PROVIDER_ID)),
            provider_world_id=overrides.pop("provider_world_id", getattr(world, "provider_world_id", DEFAULT_PROVIDER_WORLD_ID)),
            generator_type=overrides.pop("generator_type", getattr(world, "generator_type", DEFAULT_GENERATOR_TYPE)),
            generator_version=overrides.pop("generator_version", getattr(world, "generator_version", DEFAULT_GENERATOR_VERSION)),
            materialized_reason=materialized_reason,
            snapshot_source=snapshot_source,
            last_command_id=last_command_id,
            last_event_id=last_event_id,
            created_by_user_id=created_by_user_id,
            updated_by_user_id=updated_by_user_id,
            last_session_id=last_session_id,
            metadata_json=metadata_json,
            **overrides,
        )

    @classmethod
    def from_runtime_content(
        cls,
        *,
        project_db_id: int,
        universe_db_id: int,
        world_db_id: int,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        runtime_content: Mapping[str, Any],
        **kwargs: Any,
    ) -> "ChunkSnapshot":
        """Create a snapshot from RuntimeChunkContent-like JSON."""
        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_db_id=world_db_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            content_json=runtime_content,
            **kwargs,
        )

    @property
    def is_active(self) -> bool:
        return self.status == SNAPSHOT_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_archived(self) -> bool:
        return self.status == SNAPSHOT_STATUS_ARCHIVED

    @property
    def is_deleted(self) -> bool:
        return self.status == SNAPSHOT_STATUS_DELETED or self.deleted_at is not None

    @property
    def project_public_id(self) -> Optional[str]:
        """Return parent project public id if the relationship is available."""
        try:
            project = getattr(self, "project", None)
            return getattr(project, "project_id", None)
        except Exception:
            return None

    @property
    def universe_public_id(self) -> Optional[str]:
        """Return parent universe public id if the relationship is available."""
        try:
            universe = getattr(self, "universe", None)
            return getattr(universe, "universe_id", None)
        except Exception:
            return None

    @property
    def world_public_id(self) -> Optional[str]:
        """Return parent world public id if the relationship is available."""
        try:
            world = getattr(self, "world", None)
            return getattr(world, "world_id", None)
        except Exception:
            return None

    @property
    def chunk_coords(self) -> Dict[str, int]:
        """Return chunk coordinates in API-compatible shape."""
        return {
            "chunkX": int(self.chunk_x),
            "chunkY": int(self.chunk_y),
            "chunkZ": int(self.chunk_z),
        }

    @property
    def cell_encoding(self) -> Dict[str, Any]:
        """Return cell encoding metadata."""
        return {
            "version": self.cell_encoding_version,
            "airCellValue": self.air_cell_value,
            "blockCellValueRule": self.block_cell_value_rule,
        }

    @property
    def provider_context(self) -> Dict[str, Any]:
        """Return provider/generator context."""
        return {
            "templateId": self.template_id,
            "providerId": self.provider_id,
            "providerWorldId": self.provider_world_id,
            "generatorType": self.generator_type,
            "generatorVersion": self.generator_version,
        }

    @property
    def registry_context(self) -> Dict[str, Any]:
        """Return block registry context."""
        return {
            "blockRegistryId": self.block_registry_id,
            "blockRegistryVersion": self.block_registry_version,
        }

    @property
    def world_geometry_context(self) -> Dict[str, Any]:
        """Return world/chunk geometry context."""
        return {
            "chunkSize": self.chunk_size,
            "cellSize": self.cell_size,
            "coordinateSystem": self.coordinate_system,
            "projectionType": self.projection_type,
            "topologyType": self.topology_type,
        }

    def touch(
        self,
        *,
        updated_by_user_id: Optional[str] = None,
        last_session_id: Optional[str] = None,
    ) -> None:
        """Mark snapshot as updated."""
        self.updated_at = utc_now()

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
        """Raise when a mutation is attempted on a soft-deleted snapshot."""
        if self.is_deleted:
            raise ValueError(
                f"ChunkSnapshot '{self.snapshot_id}' is deleted and cannot be modified."
            )

    def bump_revision(self) -> None:
        """Increase chunk revision and update public chunk_version."""
        self.chunk_revision = int(self.chunk_revision or 1) + 1
        self.chunk_version = format_chunk_version(self.chunk_revision)

    def archive(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Archive the snapshot without deleting historical data."""
        self.ensure_not_deleted()
        self.status = SNAPSHOT_STATUS_ARCHIVED
        self.archived_at = self.archived_at or utc_now()
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Restore an archived or soft-deleted snapshot."""
        self.status = SNAPSHOT_STATUS_ACTIVE
        self.archived_at = None
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Soft-delete this snapshot."""
        now = utc_now()
        self.status = SNAPSHOT_STATUS_DELETED
        self.deleted_at = self.deleted_at or now
        self.touch(updated_by_user_id=updated_by_user_id)

    def replace_content(
        self,
        *,
        content_json: Optional[Mapping[str, Any]] = None,
        content_binary: Optional[bytes] = None,
        content_encoding: str = SNAPSHOT_CONTENT_ENCODING_JSON,
        palette_json: Optional[Sequence[Any]] = None,
        object_refs_json: Optional[Sequence[Any]] = None,
        stats_json: Optional[Mapping[str, Any]] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        materialized_reason: str = MATERIALIZED_REASON_SET_BLOCK,
        snapshot_source: str = SNAPSHOT_SOURCE_COMMAND,
        last_command_id: Optional[str] = None,
        last_event_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        last_session_id: Optional[str] = None,
        bump_revision: bool = True,
    ) -> None:
        """
        Replace snapshot content.

        Repository/service code should call this inside the same transaction
        that writes CommandLog and ChunkEvent.
        """
        self.ensure_not_deleted()

        normalized_content_json = normalize_content_json(content_json)
        normalized_content_binary = normalize_content_binary(content_binary)

        if normalized_content_json is None and normalized_content_binary is None:
            raise ValueError("ChunkSnapshot content replacement requires content.")

        resolved_palette = (
            normalize_json_list(palette_json, field_name="palette_json")
            if palette_json is not None
            else extract_palette_from_content(normalized_content_json)
        )

        resolved_object_refs = (
            normalize_json_list(object_refs_json, field_name="object_refs_json")
            if object_refs_json is not None
            else extract_object_refs_from_content(normalized_content_json)
        )

        fallback_cell_count = int(self.chunk_size or DEFAULT_CHUNK_SIZE) ** 3

        resolved_stats = (
            normalize_json_object(stats_json, field_name="stats_json")
            if stats_json is not None
            else build_stats_from_content(
                normalized_content_json,
                fallback_cell_count=fallback_cell_count,
            )
        )

        self.content_json = normalized_content_json
        self.content_binary = normalized_content_binary
        self.content_encoding = normalize_content_encoding(content_encoding)
        self.content_hash = compute_content_hash(
            content_json=normalized_content_json,
            content_binary=normalized_content_binary,
        )
        self.content_size_bytes = estimate_content_size_bytes(
            content_json=normalized_content_json,
            content_binary=normalized_content_binary,
        )
        self.palette_json = resolved_palette
        self.object_refs_json = resolved_object_refs
        self.object_ref_count = len(resolved_object_refs)
        self.has_object_refs = len(resolved_object_refs) > 0
        self.stats_json = resolved_stats
        self.cell_count = normalize_non_negative_int(
            resolved_stats.get("cellCount", fallback_cell_count),
            field_name="cell_count",
            default=fallback_cell_count,
        )
        self.non_air_cell_count = normalize_non_negative_int(
            resolved_stats.get("nonAirCellCount", 0),
            field_name="non_air_cell_count",
            default=0,
        )
        self.materialized_reason = normalize_materialized_reason(materialized_reason)
        self.snapshot_source = normalize_snapshot_source(snapshot_source)

        if metadata_json is not None:
            self.metadata_json = normalize_json_object(
                metadata_json,
                field_name="metadata_json",
            )

        self.last_command_id = normalize_optional_public_id(
            last_command_id,
            field_name="last_command_id",
            max_length=COMMAND_ID_MAX_LENGTH,
        )
        self.last_event_id = normalize_optional_public_id(
            last_event_id,
            field_name="last_event_id",
            max_length=EVENT_ID_MAX_LENGTH,
        )

        if bump_revision:
            self.bump_revision()

        self.touch(
            updated_by_user_id=updated_by_user_id,
            last_session_id=last_session_id,
        )

    def update_command_context(
        self,
        *,
        last_command_id: Optional[str] = None,
        last_event_id: Optional[str] = None,
        materialized_reason: Optional[str] = None,
        snapshot_source: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        last_session_id: Optional[str] = None,
    ) -> None:
        """Update command/event context without replacing content."""
        self.ensure_not_deleted()

        if last_command_id is not None:
            self.last_command_id = normalize_optional_public_id(
                last_command_id,
                field_name="last_command_id",
                max_length=COMMAND_ID_MAX_LENGTH,
            )

        if last_event_id is not None:
            self.last_event_id = normalize_optional_public_id(
                last_event_id,
                field_name="last_event_id",
                max_length=EVENT_ID_MAX_LENGTH,
            )

        if materialized_reason is not None:
            self.materialized_reason = normalize_materialized_reason(materialized_reason)

        if snapshot_source is not None:
            self.snapshot_source = normalize_snapshot_source(snapshot_source)

        self.touch(
            updated_by_user_id=updated_by_user_id,
            last_session_id=last_session_id,
        )

    def replace_metadata(
        self,
        metadata_json: Optional[Mapping[str, Any]],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Replace metadata_json entirely."""
        self.ensure_not_deleted()
        self.metadata_json = normalize_json_object(
            metadata_json,
            field_name="metadata_json",
        )
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

    def set_object_refs(
        self,
        object_refs_json: Optional[Sequence[Any]],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set object references for this chunk.

        This prepares multi-block object tracking without forcing the first
        block-editing slice to use object instances immediately.
        """
        self.ensure_not_deleted()
        refs = normalize_json_list(object_refs_json, field_name="object_refs_json")
        self.object_refs_json = refs
        self.object_ref_count = len(refs)
        self.has_object_refs = len(refs) > 0
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
            normalize_snapshot_id(self.snapshot_id)
        except Exception as exc:
            errors["snapshotId"] = str(exc)

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
            normalize_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_positive_int(
                self.chunk_revision,
                field_name="chunk_revision",
                default=1,
            )
        except Exception as exc:
            errors["chunkRevision"] = str(exc)

        try:
            normalize_content_encoding(self.content_encoding)
        except Exception as exc:
            errors["contentEncoding"] = str(exc)

        try:
            if self.content_json is None and self.content_binary is None:
                raise ValueError("content_json or content_binary is required.")
        except Exception as exc:
            errors["content"] = str(exc)

        try:
            normalize_content_hash(self.content_hash)
        except Exception as exc:
            errors["contentHash"] = str(exc)

        try:
            normalize_non_negative_int(
                self.content_size_bytes,
                field_name="content_size_bytes",
                default=0,
            )
        except Exception as exc:
            errors["contentSizeBytes"] = str(exc)

        try:
            normalize_non_negative_int(
                self.cell_count,
                field_name="cell_count",
                default=0,
            )
        except Exception as exc:
            errors["cellCount"] = str(exc)

        try:
            normalize_non_negative_int(
                self.non_air_cell_count,
                field_name="non_air_cell_count",
                default=0,
            )
        except Exception as exc:
            errors["nonAirCellCount"] = str(exc)

        try:
            normalize_positive_int(
                self.chunk_size,
                field_name="chunk_size",
                default=DEFAULT_CHUNK_SIZE,
            )
        except Exception as exc:
            errors["chunkSize"] = str(exc)

        try:
            normalize_positive_float(
                self.cell_size,
                field_name="cell_size",
                default=DEFAULT_CELL_SIZE,
            )
        except Exception as exc:
            errors["cellSize"] = str(exc)

        try:
            if int(self.air_cell_value) != AIR_CELL_VALUE:
                raise ValueError("air_cell_value must be 0.")
        except Exception as exc:
            errors["airCellValue"] = str(exc)

        try:
            normalize_snapshot_source(self.snapshot_source)
        except Exception as exc:
            errors["snapshotSource"] = str(exc)

        try:
            normalize_materialized_reason(self.materialized_reason)
        except Exception as exc:
            errors["materializedReason"] = str(exc)

        try:
            normalize_json_object(self.metadata_json, field_name="metadata_json")
        except Exception as exc:
            errors["metadataJson"] = str(exc)

        return errors

    def build_runtime_content(
        self,
        *,
        include_object_refs: bool = True,
    ) -> Dict[str, Any]:
        """
        Build editor-compatible RuntimeChunkContent-like payload.

        For JSON snapshots this returns the stored content plus normalized
        context fields. For binary/external formats it returns metadata and
        leaves binary loading to a later API path.
        """
        base: Dict[str, Any]

        if isinstance(self.content_json, Mapping):
            base = make_json_safe(dict(self.content_json))
        else:
            base = {}

        base.update(
            {
                "chunkX": int(self.chunk_x),
                "chunkY": int(self.chunk_y),
                "chunkZ": int(self.chunk_z),
                "chunkKey": self.chunk_key,
                "source": "snapshot",
                "snapshotId": self.snapshot_id,
                "chunkVersion": self.chunk_version,
                "chunkRevision": self.chunk_revision,
                "runtimeContentVersion": self.runtime_content_version,
                "cellIndexOrder": self.cell_index_order,
                "airCellValue": self.air_cell_value,
                "cellEncoding": self.cell_encoding,
                "palette": normalize_json_list(self.palette_json, field_name="palette_json"),
                "cellCount": self.cell_count,
                "contentHash": self.content_hash,
                "blockRegistryId": self.block_registry_id,
                "blockRegistryVersion": self.block_registry_version,
                "coordinateSystem": self.coordinate_system,
                "projectionType": self.projection_type,
                "topologyType": self.topology_type,
            }
        )

        if include_object_refs:
            base["objectRefs"] = normalize_json_list(
                self.object_refs_json,
                field_name="object_refs_json",
            )

        return base

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_content: bool = False,
        include_binary_info: bool = True,
        include_metadata: bool = True,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
        world_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serialize snapshot for API/service responses."""
        resolved_project_id = project_id if project_id is not None else self.project_public_id
        resolved_universe_id = universe_id if universe_id is not None else self.universe_public_id
        resolved_world_id = world_id if world_id is not None else self.world_public_id

        result: Dict[str, Any] = {
            "snapshotId": self.snapshot_id,
            "projectId": resolved_project_id,
            "universeId": resolved_universe_id,
            "worldId": resolved_world_id,
            "chunkX": int(self.chunk_x),
            "chunkY": int(self.chunk_y),
            "chunkZ": int(self.chunk_z),
            "chunkKey": self.chunk_key,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "runtimeContentVersion": self.runtime_content_version,
            "chunkRevision": self.chunk_revision,
            "chunkVersion": self.chunk_version,
            "contentEncoding": self.content_encoding,
            "contentHash": self.content_hash,
            "contentSizeBytes": int(self.content_size_bytes or 0),
            "cellCount": int(self.cell_count or 0),
            "nonAirCellCount": int(self.non_air_cell_count or 0),
            "chunkSize": self.chunk_size,
            "cellSize": self.cell_size,
            "cellIndexOrder": self.cell_index_order,
            "cellEncoding": self.cell_encoding,
            "blockRegistryId": self.block_registry_id,
            "blockRegistryVersion": self.block_registry_version,
            "coordinateSystem": self.coordinate_system,
            "projectionType": self.projection_type,
            "topologyType": self.topology_type,
            "templateId": self.template_id,
            "providerId": self.provider_id,
            "providerWorldId": self.provider_world_id,
            "generatorType": self.generator_type,
            "generatorVersion": self.generator_version,
            "snapshotSource": self.snapshot_source,
            "materializedReason": self.materialized_reason,
            "lastCommandId": self.last_command_id,
            "lastEventId": self.last_event_id,
            "createdByUserId": self.created_by_user_id,
            "updatedByUserId": self.updated_by_user_id,
            "lastSessionId": self.last_session_id,
            "objectRefCount": self.object_ref_count,
            "hasObjectRefs": self.has_object_refs,
            "palette": normalize_json_list(self.palette_json, field_name="palette_json"),
            "objectRefs": normalize_json_list(self.object_refs_json, field_name="object_refs_json"),
            "stats": normalize_json_object(self.stats_json, field_name="stats_json"),
            "createdAt": datetime_to_iso(self.created_at),
            "updatedAt": datetime_to_iso(self.updated_at),
            "archivedAt": datetime_to_iso(self.archived_at),
            "deletedAt": datetime_to_iso(self.deleted_at),
            "flags": {
                "active": self.is_active,
                "archived": self.is_archived,
                "deleted": self.is_deleted,
                "materialized": True,
            },
        }

        if include_metadata:
            result["metadata"] = normalize_json_object(
                self.metadata_json,
                field_name="metadata_json",
            )

        if include_content:
            result["content"] = self.build_runtime_content()

        if include_binary_info:
            result["binary"] = {
                "hasBinary": self.content_binary is not None,
                "binarySizeBytes": len(self.content_binary) if self.content_binary is not None else 0,
                "binaryContentOmitted": True,
            }

        if include_internal:
            result["id"] = self.id
            result["projectDbId"] = self.project_db_id
            result["universeDbId"] = self.universe_db_id
            result["worldDbId"] = self.world_db_id

        return result

    def to_public_dict(
        self,
        *,
        include_content: bool = False,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
        world_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serialize without internal database identifiers."""
        return self.to_dict(
            include_internal=False,
            include_content=include_content,
            include_binary_info=True,
            include_metadata=True,
            project_id=project_id,
            universe_id=universe_id,
            world_id=world_id,
        )