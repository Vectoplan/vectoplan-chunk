# services/vectoplan-chunk/models/block.py
"""
SQLAlchemy models for VECTOPLAN block registries and block types.

The chunk service starts with a small debug registry:

    registry_id      = debug-blocks
    registry_version = 1

    debug_grass
    debug_dirt

Important design rules:
- Air is not stored as a BlockType.
- `cellValue = 0` always means Air.
- `cellValue = paletteIndex + 1` means a block from the current chunk palette.
- BlockType stores stable block definitions.
- ChunkSnapshot stores the actual chunk palette/cells used for a specific chunk.
- Registry/version are stored because old chunks and old events must remain
  interpretable after future library changes.
- This file prepares the future transition to `vectoplan-library-service`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
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
        "models/block.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
        "`db` instance before importing models."
    ) from _DB_IMPORT_ERROR


try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - fallback is useful for tests/non-postgres tooling
    JSONB = None  # type: ignore[assignment]


JSON_COLUMN_TYPE = JSONB if JSONB is not None else db.JSON


BLOCK_REGISTRY_SCHEMA_VERSION = "block-registry.schema.v1"
BLOCK_TYPE_SCHEMA_VERSION = "block-type.schema.v1"

DEFAULT_BLOCK_REGISTRY_ID = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION = "1"
DEFAULT_BLOCK_REGISTRY_LABEL = "Debug Blocks"

DEBUG_GRASS_BLOCK_TYPE_ID = "debug_grass"
DEBUG_DIRT_BLOCK_TYPE_ID = "debug_dirt"

AIR_CELL_VALUE = 0
BLOCK_CELL_VALUE_RULE = "paletteIndex + 1"
CELL_ENCODING_VERSION = "cell-encoding.palette-index-plus-one.v1"

REGISTRY_STATUS_ACTIVE = "active"
REGISTRY_STATUS_ARCHIVED = "archived"
REGISTRY_STATUS_DELETED = "deleted"

VALID_REGISTRY_STATUSES = frozenset(
    {
        REGISTRY_STATUS_ACTIVE,
        REGISTRY_STATUS_ARCHIVED,
        REGISTRY_STATUS_DELETED,
    }
)

BLOCK_STATUS_ACTIVE = "active"
BLOCK_STATUS_DEPRECATED = "deprecated"
BLOCK_STATUS_DISABLED = "disabled"
BLOCK_STATUS_DELETED = "deleted"

VALID_BLOCK_STATUSES = frozenset(
    {
        BLOCK_STATUS_ACTIVE,
        BLOCK_STATUS_DEPRECATED,
        BLOCK_STATUS_DISABLED,
        BLOCK_STATUS_DELETED,
    }
)

REGISTRY_SOURCE_INTERNAL = "internal"
REGISTRY_SOURCE_LIBRARY = "library"
REGISTRY_SOURCE_IMPORTED = "imported"
REGISTRY_SOURCE_TEST = "test"

VALID_REGISTRY_SOURCES = frozenset(
    {
        REGISTRY_SOURCE_INTERNAL,
        REGISTRY_SOURCE_LIBRARY,
        REGISTRY_SOURCE_IMPORTED,
        REGISTRY_SOURCE_TEST,
    }
)

BLOCK_CATEGORY_DEBUG = "debug"
BLOCK_CATEGORY_TERRAIN = "terrain"
BLOCK_CATEGORY_STRUCTURE = "structure"
BLOCK_CATEGORY_OBJECT = "object"
BLOCK_CATEGORY_SYSTEM = "system"
BLOCK_CATEGORY_UNKNOWN = "unknown"

VALID_BLOCK_CATEGORIES = frozenset(
    {
        BLOCK_CATEGORY_DEBUG,
        BLOCK_CATEGORY_TERRAIN,
        BLOCK_CATEGORY_STRUCTURE,
        BLOCK_CATEGORY_OBJECT,
        BLOCK_CATEGORY_SYSTEM,
        BLOCK_CATEGORY_UNKNOWN,
    }
)

RENDER_MODE_CUBE = "cube"
RENDER_MODE_INVISIBLE = "invisible"
RENDER_MODE_CUSTOM = "custom"
RENDER_MODE_MESH = "mesh"

VALID_RENDER_MODES = frozenset(
    {
        RENDER_MODE_CUBE,
        RENDER_MODE_INVISIBLE,
        RENDER_MODE_CUSTOM,
        RENDER_MODE_MESH,
    }
)

SHAPE_TYPE_CUBE = "cube"
SHAPE_TYPE_EMPTY = "empty"
SHAPE_TYPE_CUSTOM = "custom"

VALID_SHAPE_TYPES = frozenset(
    {
        SHAPE_TYPE_CUBE,
        SHAPE_TYPE_EMPTY,
        SHAPE_TYPE_CUSTOM,
    }
)

REGISTRY_ID_MAX_LENGTH = 128
REGISTRY_VERSION_MAX_LENGTH = 64
REGISTRY_LABEL_MAX_LENGTH = 255
REGISTRY_DESCRIPTION_MAX_LENGTH = 4096
REGISTRY_SOURCE_MAX_LENGTH = 64
REGISTRY_USER_ID_MAX_LENGTH = 128

BLOCK_TYPE_ID_MAX_LENGTH = 160
BLOCK_LABEL_MAX_LENGTH = 255
BLOCK_DESCRIPTION_MAX_LENGTH = 4096
BLOCK_CATEGORY_MAX_LENGTH = 96
BLOCK_MATERIAL_ID_MAX_LENGTH = 160
BLOCK_TEXTURE_ID_MAX_LENGTH = 160
BLOCK_ICON_ID_MAX_LENGTH = 160
BLOCK_RENDER_MODE_MAX_LENGTH = 64
BLOCK_SHAPE_TYPE_MAX_LENGTH = 64
BLOCK_LIBRARY_TYPE_ID_MAX_LENGTH = 160
BLOCK_LIBRARY_VARIANT_ID_MAX_LENGTH = 160
BLOCK_USER_ID_MAX_LENGTH = 128

TYPE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")


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

    Metadata can later come from the editor, importers, scripts, AI tooling or
    the library service, so this is intentionally defensive.
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


def normalize_type_id(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """
    Normalize registry/block/library IDs.

    Allowed:
    - letters
    - numbers
    - underscore
    - dash
    - dot
    - colon

    This is intentionally broader than project/world IDs because future
    library identifiers may use namespaced IDs.
    """
    text = normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if not TYPE_ID_PATTERN.match(text):
        raise ValueError(
            f"{field_name} may only contain letters, numbers, underscores, "
            "dashes, dots and colons, and must start with a letter or number."
        )

    return text


def normalize_registry_id(value: Any) -> str:
    """Normalize registry_id."""
    return normalize_type_id(
        value,
        field_name="registry_id",
        max_length=REGISTRY_ID_MAX_LENGTH,
    )


def normalize_block_type_id(value: Any) -> str:
    """Normalize block_type_id."""
    return normalize_type_id(
        value,
        field_name="block_type_id",
        max_length=BLOCK_TYPE_ID_MAX_LENGTH,
    )


def normalize_version(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    default: str,
) -> str:
    """Normalize version strings."""
    if value is None:
        value = default

    text = normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if not VERSION_PATTERN.match(text):
        raise ValueError(
            f"{field_name} may only contain letters, numbers, underscores, "
            "dashes, dots, colons and plus signs, and must start with a "
            "letter or number."
        )

    return text


def normalize_registry_version(value: Any) -> str:
    """Normalize registry_version."""
    return normalize_version(
        value,
        field_name="registry_version",
        max_length=REGISTRY_VERSION_MAX_LENGTH,
        default=DEFAULT_BLOCK_REGISTRY_VERSION,
    )


def normalize_registry_status(value: Any) -> str:
    """Normalize and validate registry status."""
    if value is None:
        return REGISTRY_STATUS_ACTIVE

    status = str(value).strip().lower()

    if status not in VALID_REGISTRY_STATUSES:
        allowed = ", ".join(sorted(VALID_REGISTRY_STATUSES))
        raise ValueError(f"Invalid registry status '{value}'. Allowed: {allowed}.")

    return status


def normalize_block_status(value: Any) -> str:
    """Normalize and validate block status."""
    if value is None:
        return BLOCK_STATUS_ACTIVE

    status = str(value).strip().lower()

    if status not in VALID_BLOCK_STATUSES:
        allowed = ", ".join(sorted(VALID_BLOCK_STATUSES))
        raise ValueError(f"Invalid block status '{value}'. Allowed: {allowed}.")

    return status


def normalize_registry_source(value: Any) -> str:
    """Normalize and validate registry source."""
    if value is None:
        return REGISTRY_SOURCE_INTERNAL

    source = str(value).strip().lower()

    if source not in VALID_REGISTRY_SOURCES:
        allowed = ", ".join(sorted(VALID_REGISTRY_SOURCES))
        raise ValueError(f"Invalid registry source '{value}'. Allowed: {allowed}.")

    return source


def normalize_block_category(value: Any) -> str:
    """Normalize and validate block category."""
    if value is None:
        return BLOCK_CATEGORY_UNKNOWN

    category = str(value).strip().lower()

    if category not in VALID_BLOCK_CATEGORIES:
        allowed = ", ".join(sorted(VALID_BLOCK_CATEGORIES))
        raise ValueError(f"Invalid block category '{value}'. Allowed: {allowed}.")

    return category


def normalize_render_mode(value: Any) -> str:
    """Normalize and validate render mode."""
    if value is None:
        return RENDER_MODE_CUBE

    render_mode = str(value).strip().lower()

    if render_mode not in VALID_RENDER_MODES:
        allowed = ", ".join(sorted(VALID_RENDER_MODES))
        raise ValueError(f"Invalid render mode '{value}'. Allowed: {allowed}.")

    return render_mode


def normalize_shape_type(value: Any) -> str:
    """Normalize and validate shape type."""
    if value is None:
        return SHAPE_TYPE_CUBE

    shape_type = str(value).strip().lower()

    if shape_type not in VALID_SHAPE_TYPES:
        allowed = ", ".join(sorted(VALID_SHAPE_TYPES))
        raise ValueError(f"Invalid shape type '{value}'. Allowed: {allowed}.")

    return shape_type


def normalize_bool(value: Any, *, field_name: str, default: bool) -> bool:
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


def normalize_optional_int(
    value: Any,
    *,
    field_name: str,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> Optional[int]:
    """Normalize optional integer values."""
    if value is None:
        return None

    try:
        result = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if min_value is not None and result < min_value:
        raise ValueError(f"{field_name} must be greater than or equal to {min_value}.")

    if max_value is not None and result > max_value:
        raise ValueError(f"{field_name} must be less than or equal to {max_value}.")

    return result


def normalize_required_int(
    value: Any,
    *,
    field_name: str,
    default: int,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    """Normalize required integer values."""
    if value is None:
        value = default

    result = normalize_optional_int(
        value,
        field_name=field_name,
        min_value=min_value,
        max_value=max_value,
    )

    if result is None:
        raise ValueError(f"{field_name} is required.")

    return result


def normalize_optional_float(
    value: Any,
    *,
    field_name: str,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> Optional[float]:
    """Normalize optional float values."""
    if value is None:
        return None

    try:
        result = float(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number.") from exc

    if min_value is not None and result < min_value:
        raise ValueError(f"{field_name} must be greater than or equal to {min_value}.")

    if max_value is not None and result > max_value:
        raise ValueError(f"{field_name} must be less than or equal to {max_value}.")

    return result


def normalize_required_float(
    value: Any,
    *,
    field_name: str,
    default: float,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> float:
    """Normalize required float values."""
    if value is None:
        value = default

    result = normalize_optional_float(
        value,
        field_name=field_name,
        min_value=min_value,
        max_value=max_value,
    )

    if result is None:
        raise ValueError(f"{field_name} is required.")

    return result


def normalize_metadata(value: Any) -> Dict[str, Any]:
    """
    Normalize metadata into a JSON-safe object.

    Metadata must be an object at the top level.
    """
    if value is None:
        return {}

    if not isinstance(value, Mapping):
        raise ValueError("metadata_json must be a JSON object/dict.")

    return make_json_safe(dict(value))


def normalize_registry_db_id(value: Any) -> int:
    """Normalize an internal block registry database id."""
    if value is None:
        raise ValueError("registry_db_id is required.")

    try:
        registry_db_id = int(value)
    except Exception as exc:
        raise ValueError("registry_db_id must be an integer.") from exc

    if registry_db_id <= 0:
        raise ValueError("registry_db_id must be greater than zero.")

    return registry_db_id


def generate_registry_id(prefix: str = "registry") -> str:
    """Generate a stable registry id."""
    normalized_prefix = normalize_type_id(
        prefix,
        field_name="registry_id_prefix",
        max_length=24,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def generate_block_type_id(prefix: str = "block") -> str:
    """Generate a stable block type id."""
    normalized_prefix = normalize_type_id(
        prefix,
        field_name="block_type_id_prefix",
        max_length=24,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


class BlockRegistry(db.Model):
    """
    Persistent block registry version.

    A registry version groups a stable set of BlockType definitions.

    Examples:
        registry_id      = debug-blocks
        registry_version = 1

    Later:
        registry_id      = vectoplan-library
        registry_version = 2026.05.14
    """

    __tablename__ = "block_registries"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    registry_id = db.Column(
        db.String(REGISTRY_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    registry_version = db.Column(
        db.String(REGISTRY_VERSION_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    label = db.Column(
        db.String(REGISTRY_LABEL_MAX_LENGTH),
        nullable=False,
    )

    description = db.Column(
        db.String(REGISTRY_DESCRIPTION_MAX_LENGTH),
        nullable=True,
    )

    status = db.Column(
        db.String(32),
        nullable=False,
        default=REGISTRY_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(64),
        nullable=False,
        default=BLOCK_REGISTRY_SCHEMA_VERSION,
    )

    revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    source = db.Column(
        db.String(REGISTRY_SOURCE_MAX_LENGTH),
        nullable=False,
        default=REGISTRY_SOURCE_INTERNAL,
        index=True,
    )

    is_default = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        index=True,
    )

    library_snapshot_id = db.Column(
        db.String(160),
        nullable=True,
        index=True,
    )

    created_by_user_id = db.Column(
        db.String(REGISTRY_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_by_user_id = db.Column(
        db.String(REGISTRY_USER_ID_MAX_LENGTH),
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

    __table_args__ = (
        db.UniqueConstraint(
            "registry_id",
            "registry_version",
            name="uq_block_registries_registry_version",
        ),
        db.CheckConstraint(
            "registry_id <> ''",
            name="ck_block_registries_registry_id_not_empty",
        ),
        db.CheckConstraint(
            "registry_version <> ''",
            name="ck_block_registries_registry_version_not_empty",
        ),
        db.CheckConstraint(
            "label <> ''",
            name="ck_block_registries_label_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_block_registries_status_valid",
        ),
        db.CheckConstraint(
            "source IN ('internal', 'library', 'imported', 'test')",
            name="ck_block_registries_source_valid",
        ),
        db.CheckConstraint(
            "revision >= 1",
            name="ck_block_registries_revision_positive",
        ),
        db.Index(
            "ix_block_registries_status_created_at",
            "status",
            "created_at",
        ),
        db.Index(
            "ix_block_registries_default_active",
            "is_default",
            "status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<BlockRegistry id={self.id!r} registry_id={self.registry_id!r} "
            f"registry_version={self.registry_version!r} status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        registry_id: Optional[str] = None,
        registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION,
        label: Optional[str] = None,
        description: Optional[str] = None,
        status: str = REGISTRY_STATUS_ACTIVE,
        source: str = REGISTRY_SOURCE_INTERNAL,
        is_default: bool = False,
        library_snapshot_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "BlockRegistry":
        """
        Create a BlockRegistry instance without adding it to a session.

        Repository/service code is responsible for:
        - checking uniqueness
        - adding to db.session
        - committing or rolling back
        """
        normalized_registry_id = normalize_registry_id(
            registry_id or generate_registry_id()
        )
        normalized_version = normalize_registry_version(registry_version)
        normalized_status = normalize_registry_status(status)
        now = utc_now()

        return cls(
            registry_id=normalized_registry_id,
            registry_version=normalized_version,
            label=normalize_required_text(
                label or normalized_registry_id,
                field_name="label",
                max_length=REGISTRY_LABEL_MAX_LENGTH,
            ),
            description=normalize_optional_text(
                description,
                field_name="description",
                max_length=REGISTRY_DESCRIPTION_MAX_LENGTH,
            ),
            status=normalized_status,
            schema_version=BLOCK_REGISTRY_SCHEMA_VERSION,
            revision=1,
            source=normalize_registry_source(source),
            is_default=normalize_bool(
                is_default,
                field_name="is_default",
                default=False,
            ),
            library_snapshot_id=normalize_optional_text(
                library_snapshot_id,
                field_name="library_snapshot_id",
                max_length=160,
            ),
            created_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="created_by_user_id",
                max_length=REGISTRY_USER_ID_MAX_LENGTH,
            ),
            updated_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="updated_by_user_id",
                max_length=REGISTRY_USER_ID_MAX_LENGTH,
            ),
            metadata_json=normalize_metadata(metadata_json),
            created_at=now,
            updated_at=now,
            archived_at=now if normalized_status == REGISTRY_STATUS_ARCHIVED else None,
            deleted_at=now if normalized_status == REGISTRY_STATUS_DELETED else None,
        )

    @classmethod
    def create_debug_registry(
        cls,
        *,
        is_default: bool = True,
        created_by_user_id: Optional[str] = None,
    ) -> "BlockRegistry":
        """Create the default debug block registry."""
        return cls.create(
            registry_id=DEFAULT_BLOCK_REGISTRY_ID,
            registry_version=DEFAULT_BLOCK_REGISTRY_VERSION,
            label=DEFAULT_BLOCK_REGISTRY_LABEL,
            description=(
                "Initial internal debug block registry for the first "
                "editable flat-world slice."
            ),
            status=REGISTRY_STATUS_ACTIVE,
            source=REGISTRY_SOURCE_INTERNAL,
            is_default=is_default,
            created_by_user_id=created_by_user_id,
            metadata_json={
                "purpose": "development",
                "cellEncoding": {
                    "version": CELL_ENCODING_VERSION,
                    "airCellValue": AIR_CELL_VALUE,
                    "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
                },
            },
        )

    @classmethod
    def from_create_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        created_by_user_id: Optional[str] = None,
    ) -> "BlockRegistry":
        """Create a registry from an API-style payload."""
        if not isinstance(payload, Mapping):
            raise ValueError("Block registry create payload must be a JSON object.")

        metadata_value = (
            payload.get("metadataJson")
            if "metadataJson" in payload
            else payload.get("metadata_json")
            if "metadata_json" in payload
            else payload.get("metadata")
        )

        return cls.create(
            registry_id=payload.get("registryId") or payload.get("registry_id"),
            registry_version=payload.get("registryVersion") or payload.get("registry_version") or DEFAULT_BLOCK_REGISTRY_VERSION,
            label=payload.get("label"),
            description=payload.get("description"),
            status=payload.get("status") or REGISTRY_STATUS_ACTIVE,
            source=payload.get("source") or REGISTRY_SOURCE_INTERNAL,
            is_default=payload.get("isDefault") if "isDefault" in payload else payload.get("is_default", False),
            library_snapshot_id=payload.get("librarySnapshotId") or payload.get("library_snapshot_id"),
            created_by_user_id=created_by_user_id,
            metadata_json=metadata_value,
        )

    @property
    def is_active(self) -> bool:
        return self.status == REGISTRY_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_archived(self) -> bool:
        return self.status == REGISTRY_STATUS_ARCHIVED

    @property
    def is_deleted(self) -> bool:
        return self.status == REGISTRY_STATUS_DELETED or self.deleted_at is not None

    @property
    def registry_key(self) -> str:
        return f"{self.registry_id}@{self.registry_version}"

    def touch(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Mark the registry as updated and increment its optimistic revision."""
        self.updated_at = utc_now()
        self.revision = int(self.revision or 1) + 1

        normalized_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="updated_by_user_id",
            max_length=REGISTRY_USER_ID_MAX_LENGTH,
        )

        if normalized_user_id is not None:
            self.updated_by_user_id = normalized_user_id

    def ensure_not_deleted(self) -> None:
        """Raise when a mutation is attempted on a soft-deleted registry."""
        if self.is_deleted:
            raise ValueError(
                f"Block registry '{self.registry_key}' is deleted and cannot be modified."
            )

    def set_default(
        self,
        is_default: bool,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set or unset this registry as default."""
        self.ensure_not_deleted()
        self.is_default = normalize_bool(
            is_default,
            field_name="is_default",
            default=False,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def archive(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Archive the registry without deleting historical data."""
        self.ensure_not_deleted()
        self.status = REGISTRY_STATUS_ARCHIVED
        self.archived_at = self.archived_at or utc_now()
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Restore an archived or soft-deleted registry."""
        self.status = REGISTRY_STATUS_ACTIVE
        self.archived_at = None
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Soft-delete the registry."""
        now = utc_now()
        self.status = REGISTRY_STATUS_DELETED
        self.deleted_at = self.deleted_at or now
        self.touch(updated_by_user_id=updated_by_user_id)

    def replace_metadata(
        self,
        metadata_json: Optional[Mapping[str, Any]],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Replace metadata_json entirely."""
        self.ensure_not_deleted()
        self.metadata_json = normalize_metadata(metadata_json)
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

        current = normalize_metadata(self.metadata_json)

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

        try:
            normalize_registry_id(self.registry_id)
        except Exception as exc:
            errors["registryId"] = str(exc)

        try:
            normalize_registry_version(self.registry_version)
        except Exception as exc:
            errors["registryVersion"] = str(exc)

        try:
            normalize_required_text(
                self.label,
                field_name="label",
                max_length=REGISTRY_LABEL_MAX_LENGTH,
            )
        except Exception as exc:
            errors["label"] = str(exc)

        try:
            normalize_registry_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_registry_source(self.source)
        except Exception as exc:
            errors["source"] = str(exc)

        try:
            normalize_metadata(self.metadata_json)
        except Exception as exc:
            errors["metadataJson"] = str(exc)

        if self.revision is None or int(self.revision) < 1:
            errors["revision"] = "revision must be greater than or equal to 1."

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
        include_blocks: bool = False,
    ) -> Dict[str, Any]:
        """Serialize registry for API/service responses."""
        result: Dict[str, Any] = {
            "registryId": self.registry_id,
            "registryVersion": self.registry_version,
            "registryKey": self.registry_key,
            "label": self.label,
            "description": self.description,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "source": self.source,
            "isDefault": self.is_default,
            "librarySnapshotId": self.library_snapshot_id,
            "cellEncoding": {
                "version": CELL_ENCODING_VERSION,
                "airCellValue": AIR_CELL_VALUE,
                "blockCellValueRule": BLOCK_CELL_VALUE_RULE,
            },
            "createdByUserId": self.created_by_user_id,
            "updatedByUserId": self.updated_by_user_id,
            "createdAt": datetime_to_iso(self.created_at),
            "updatedAt": datetime_to_iso(self.updated_at),
            "archivedAt": datetime_to_iso(self.archived_at),
            "deletedAt": datetime_to_iso(self.deleted_at),
            "flags": {
                "active": self.is_active,
                "archived": self.is_archived,
                "deleted": self.is_deleted,
            },
        }

        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)

        if include_blocks:
            try:
                blocks = sorted(
                    list(self.block_types or []),
                    key=lambda item: (
                        item.default_palette_index is None,
                        item.default_palette_index if item.default_palette_index is not None else 999999,
                        item.block_type_id,
                    ),
                )
                result["blocks"] = [
                    block.to_dict(include_internal=include_internal)
                    for block in blocks
                    if not block.is_deleted
                ]
            except Exception:
                result["blocks"] = []

        if include_internal:
            result["id"] = self.id

        return result

    def to_public_dict(self, *, include_blocks: bool = False) -> Dict[str, Any]:
        """Serialize without internal database identifiers."""
        return self.to_dict(
            include_internal=False,
            include_metadata=True,
            include_blocks=include_blocks,
        )


class BlockType(db.Model):
    """
    Persistent block type definition.

    Air is not a BlockType. Air is represented by cellValue 0.

    A block type can be used in a chunk palette. The chunk palette determines
    paletteIndex and therefore cellValue for a specific chunk payload.

    `default_palette_index` is only the recommended registry-level order for
    debug/default palettes. It is not the global truth for all chunks.
    """

    __tablename__ = "block_types"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    registry_db_id = db.Column(
        db.BigInteger,
        db.ForeignKey("block_registries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    registry_id = db.Column(
        db.String(REGISTRY_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    registry_version = db.Column(
        db.String(REGISTRY_VERSION_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    block_type_id = db.Column(
        db.String(BLOCK_TYPE_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    label = db.Column(
        db.String(BLOCK_LABEL_MAX_LENGTH),
        nullable=False,
    )

    description = db.Column(
        db.String(BLOCK_DESCRIPTION_MAX_LENGTH),
        nullable=True,
    )

    status = db.Column(
        db.String(32),
        nullable=False,
        default=BLOCK_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(64),
        nullable=False,
        default=BLOCK_TYPE_SCHEMA_VERSION,
    )

    revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    category = db.Column(
        db.String(BLOCK_CATEGORY_MAX_LENGTH),
        nullable=False,
        default=BLOCK_CATEGORY_UNKNOWN,
        index=True,
    )

    default_palette_index = db.Column(
        db.Integer,
        nullable=True,
        index=True,
    )

    solid = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    opaque = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    placeable = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    breakable = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    selectable = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    collidable = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    emits_light = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    light_level = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )

    hardness = db.Column(
        db.Float,
        nullable=False,
        default=1.0,
    )

    stack_size = db.Column(
        db.Integer,
        nullable=False,
        default=64,
    )

    render_mode = db.Column(
        db.String(BLOCK_RENDER_MODE_MAX_LENGTH),
        nullable=False,
        default=RENDER_MODE_CUBE,
        index=True,
    )

    shape_type = db.Column(
        db.String(BLOCK_SHAPE_TYPE_MAX_LENGTH),
        nullable=False,
        default=SHAPE_TYPE_CUBE,
        index=True,
    )

    material_id = db.Column(
        db.String(BLOCK_MATERIAL_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    texture_id = db.Column(
        db.String(BLOCK_TEXTURE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    icon_id = db.Column(
        db.String(BLOCK_ICON_ID_MAX_LENGTH),
        nullable=True,
    )

    library_type_id = db.Column(
        db.String(BLOCK_LIBRARY_TYPE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    library_variant_id = db.Column(
        db.String(BLOCK_LIBRARY_VARIANT_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_by_user_id = db.Column(
        db.String(BLOCK_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_by_user_id = db.Column(
        db.String(BLOCK_USER_ID_MAX_LENGTH),
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

    deprecated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    deleted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    registry = db.relationship(
        "BlockRegistry",
        backref=db.backref(
            "block_types",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "registry_db_id",
            "block_type_id",
            name="uq_block_types_registry_block_type",
        ),
        db.UniqueConstraint(
            "registry_id",
            "registry_version",
            "block_type_id",
            name="uq_block_types_registry_version_block_type",
        ),
        db.UniqueConstraint(
            "registry_db_id",
            "default_palette_index",
            name="uq_block_types_registry_default_palette_index",
        ),
        db.CheckConstraint(
            "registry_db_id > 0",
            name="ck_block_types_registry_db_id_positive",
        ),
        db.CheckConstraint(
            "registry_id <> ''",
            name="ck_block_types_registry_id_not_empty",
        ),
        db.CheckConstraint(
            "registry_version <> ''",
            name="ck_block_types_registry_version_not_empty",
        ),
        db.CheckConstraint(
            "block_type_id <> ''",
            name="ck_block_types_block_type_id_not_empty",
        ),
        db.CheckConstraint(
            "label <> ''",
            name="ck_block_types_label_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'deprecated', 'disabled', 'deleted')",
            name="ck_block_types_status_valid",
        ),
        db.CheckConstraint(
            "category IN ('debug', 'terrain', 'structure', 'object', 'system', 'unknown')",
            name="ck_block_types_category_valid",
        ),
        db.CheckConstraint(
            "render_mode IN ('cube', 'invisible', 'custom', 'mesh')",
            name="ck_block_types_render_mode_valid",
        ),
        db.CheckConstraint(
            "shape_type IN ('cube', 'empty', 'custom')",
            name="ck_block_types_shape_type_valid",
        ),
        db.CheckConstraint(
            "revision >= 1",
            name="ck_block_types_revision_positive",
        ),
        db.CheckConstraint(
            "default_palette_index IS NULL OR default_palette_index >= 0",
            name="ck_block_types_default_palette_index_non_negative",
        ),
        db.CheckConstraint(
            "light_level >= 0 AND light_level <= 15",
            name="ck_block_types_light_level_range",
        ),
        db.CheckConstraint(
            "hardness >= 0",
            name="ck_block_types_hardness_non_negative",
        ),
        db.CheckConstraint(
            "stack_size >= 1",
            name="ck_block_types_stack_size_positive",
        ),
        db.Index(
            "ix_block_types_registry_lookup",
            "registry_id",
            "registry_version",
            "block_type_id",
        ),
        db.Index(
            "ix_block_types_registry_status_palette",
            "registry_db_id",
            "status",
            "default_palette_index",
        ),
        db.Index(
            "ix_block_types_library_lookup",
            "library_type_id",
            "library_variant_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<BlockType id={self.id!r} registry_id={self.registry_id!r} "
            f"registry_version={self.registry_version!r} "
            f"block_type_id={self.block_type_id!r} status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        registry_db_id: int,
        registry_id: str,
        registry_version: str,
        block_type_id: Optional[str] = None,
        label: Optional[str] = None,
        description: Optional[str] = None,
        status: str = BLOCK_STATUS_ACTIVE,
        category: str = BLOCK_CATEGORY_UNKNOWN,
        default_palette_index: Optional[int] = None,
        solid: bool = True,
        opaque: bool = True,
        placeable: bool = True,
        breakable: bool = True,
        selectable: bool = True,
        collidable: bool = True,
        emits_light: bool = False,
        light_level: int = 0,
        hardness: float = 1.0,
        stack_size: int = 64,
        render_mode: str = RENDER_MODE_CUBE,
        shape_type: str = SHAPE_TYPE_CUBE,
        material_id: Optional[str] = None,
        texture_id: Optional[str] = None,
        icon_id: Optional[str] = None,
        library_type_id: Optional[str] = None,
        library_variant_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "BlockType":
        """
        Create a BlockType instance without adding it to a session.

        Repository/service code is responsible for:
        - checking registry existence
        - checking uniqueness
        - adding to db.session
        - committing or rolling back
        """
        normalized_block_type_id = normalize_block_type_id(
            block_type_id or generate_block_type_id()
        )
        normalized_status = normalize_block_status(status)
        now = utc_now()

        return cls(
            registry_db_id=normalize_registry_db_id(registry_db_id),
            registry_id=normalize_registry_id(registry_id),
            registry_version=normalize_registry_version(registry_version),
            block_type_id=normalized_block_type_id,
            label=normalize_required_text(
                label or normalized_block_type_id,
                field_name="label",
                max_length=BLOCK_LABEL_MAX_LENGTH,
            ),
            description=normalize_optional_text(
                description,
                field_name="description",
                max_length=BLOCK_DESCRIPTION_MAX_LENGTH,
            ),
            status=normalized_status,
            schema_version=BLOCK_TYPE_SCHEMA_VERSION,
            revision=1,
            category=normalize_block_category(category),
            default_palette_index=normalize_optional_int(
                default_palette_index,
                field_name="default_palette_index",
                min_value=0,
            ),
            solid=normalize_bool(solid, field_name="solid", default=True),
            opaque=normalize_bool(opaque, field_name="opaque", default=True),
            placeable=normalize_bool(placeable, field_name="placeable", default=True),
            breakable=normalize_bool(breakable, field_name="breakable", default=True),
            selectable=normalize_bool(selectable, field_name="selectable", default=True),
            collidable=normalize_bool(collidable, field_name="collidable", default=True),
            emits_light=normalize_bool(emits_light, field_name="emits_light", default=False),
            light_level=normalize_required_int(
                light_level,
                field_name="light_level",
                default=0,
                min_value=0,
                max_value=15,
            ),
            hardness=normalize_required_float(
                hardness,
                field_name="hardness",
                default=1.0,
                min_value=0,
            ),
            stack_size=normalize_required_int(
                stack_size,
                field_name="stack_size",
                default=64,
                min_value=1,
            ),
            render_mode=normalize_render_mode(render_mode),
            shape_type=normalize_shape_type(shape_type),
            material_id=normalize_optional_text(
                material_id,
                field_name="material_id",
                max_length=BLOCK_MATERIAL_ID_MAX_LENGTH,
            ),
            texture_id=normalize_optional_text(
                texture_id,
                field_name="texture_id",
                max_length=BLOCK_TEXTURE_ID_MAX_LENGTH,
            ),
            icon_id=normalize_optional_text(
                icon_id,
                field_name="icon_id",
                max_length=BLOCK_ICON_ID_MAX_LENGTH,
            ),
            library_type_id=normalize_optional_text(
                library_type_id,
                field_name="library_type_id",
                max_length=BLOCK_LIBRARY_TYPE_ID_MAX_LENGTH,
            ),
            library_variant_id=normalize_optional_text(
                library_variant_id,
                field_name="library_variant_id",
                max_length=BLOCK_LIBRARY_VARIANT_ID_MAX_LENGTH,
            ),
            created_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="created_by_user_id",
                max_length=BLOCK_USER_ID_MAX_LENGTH,
            ),
            updated_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="updated_by_user_id",
                max_length=BLOCK_USER_ID_MAX_LENGTH,
            ),
            metadata_json=normalize_metadata(metadata_json),
            created_at=now,
            updated_at=now,
            deprecated_at=now if normalized_status == BLOCK_STATUS_DEPRECATED else None,
            deleted_at=now if normalized_status == BLOCK_STATUS_DELETED else None,
        )

    @classmethod
    def create_for_registry(
        cls,
        registry: BlockRegistry,
        **kwargs: Any,
    ) -> "BlockType":
        """Create a BlockType for a persisted BlockRegistry instance."""
        registry_db_id = getattr(registry, "id", None)
        if registry_db_id is None:
            raise ValueError(
                "Cannot create block type for registry without persisted registry.id."
            )

        return cls.create(
            registry_db_id=registry_db_id,
            registry_id=registry.registry_id,
            registry_version=registry.registry_version,
            **kwargs,
        )

    @classmethod
    def create_debug_grass(
        cls,
        registry: BlockRegistry,
        *,
        created_by_user_id: Optional[str] = None,
    ) -> "BlockType":
        """Create the debug grass block type."""
        return cls.create_for_registry(
            registry,
            block_type_id=DEBUG_GRASS_BLOCK_TYPE_ID,
            label="Debug Grass",
            description="Visible debug surface block for flat-world generation.",
            status=BLOCK_STATUS_ACTIVE,
            category=BLOCK_CATEGORY_DEBUG,
            default_palette_index=0,
            solid=True,
            opaque=True,
            placeable=True,
            breakable=True,
            selectable=True,
            collidable=True,
            emits_light=False,
            light_level=0,
            hardness=0.8,
            stack_size=64,
            render_mode=RENDER_MODE_CUBE,
            shape_type=SHAPE_TYPE_CUBE,
            material_id="debug_grass",
            texture_id="debug_grass",
            icon_id="debug_grass",
            created_by_user_id=created_by_user_id,
            metadata_json={
                "debug": True,
                "generatorRole": "surface",
                "recommendedColor": "grass",
            },
        )

    @classmethod
    def create_debug_dirt(
        cls,
        registry: BlockRegistry,
        *,
        created_by_user_id: Optional[str] = None,
    ) -> "BlockType":
        """Create the debug dirt block type."""
        return cls.create_for_registry(
            registry,
            block_type_id=DEBUG_DIRT_BLOCK_TYPE_ID,
            label="Debug Dirt",
            description="Visible debug subsurface block for flat-world generation.",
            status=BLOCK_STATUS_ACTIVE,
            category=BLOCK_CATEGORY_DEBUG,
            default_palette_index=1,
            solid=True,
            opaque=True,
            placeable=True,
            breakable=True,
            selectable=True,
            collidable=True,
            emits_light=False,
            light_level=0,
            hardness=1.0,
            stack_size=64,
            render_mode=RENDER_MODE_CUBE,
            shape_type=SHAPE_TYPE_CUBE,
            material_id="debug_dirt",
            texture_id="debug_dirt",
            icon_id="debug_dirt",
            created_by_user_id=created_by_user_id,
            metadata_json={
                "debug": True,
                "generatorRole": "subsurface",
                "recommendedColor": "dirt",
            },
        )

    @classmethod
    def create_default_debug_blocks(
        cls,
        registry: BlockRegistry,
        *,
        created_by_user_id: Optional[str] = None,
    ) -> List["BlockType"]:
        """Create the default debug block list for a registry."""
        return [
            cls.create_debug_grass(
                registry,
                created_by_user_id=created_by_user_id,
            ),
            cls.create_debug_dirt(
                registry,
                created_by_user_id=created_by_user_id,
            ),
        ]

    @classmethod
    def from_create_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        registry: BlockRegistry,
        created_by_user_id: Optional[str] = None,
    ) -> "BlockType":
        """Create a BlockType from an API-style payload."""
        if not isinstance(payload, Mapping):
            raise ValueError("Block type create payload must be a JSON object.")

        metadata_value = (
            payload.get("metadataJson")
            if "metadataJson" in payload
            else payload.get("metadata_json")
            if "metadata_json" in payload
            else payload.get("metadata")
        )

        return cls.create_for_registry(
            registry,
            block_type_id=payload.get("blockTypeId") or payload.get("block_type_id"),
            label=payload.get("label"),
            description=payload.get("description"),
            status=payload.get("status") or BLOCK_STATUS_ACTIVE,
            category=payload.get("category") or BLOCK_CATEGORY_UNKNOWN,
            default_palette_index=payload.get("defaultPaletteIndex") if "defaultPaletteIndex" in payload else payload.get("default_palette_index"),
            solid=payload.get("solid", True),
            opaque=payload.get("opaque", True),
            placeable=payload.get("placeable", True),
            breakable=payload.get("breakable", True),
            selectable=payload.get("selectable", True),
            collidable=payload.get("collidable", True),
            emits_light=payload.get("emitsLight") if "emitsLight" in payload else payload.get("emits_light", False),
            light_level=payload.get("lightLevel") if "lightLevel" in payload else payload.get("light_level", 0),
            hardness=payload.get("hardness", 1.0),
            stack_size=payload.get("stackSize") if "stackSize" in payload else payload.get("stack_size", 64),
            render_mode=payload.get("renderMode") or payload.get("render_mode") or RENDER_MODE_CUBE,
            shape_type=payload.get("shapeType") or payload.get("shape_type") or SHAPE_TYPE_CUBE,
            material_id=payload.get("materialId") or payload.get("material_id"),
            texture_id=payload.get("textureId") or payload.get("texture_id"),
            icon_id=payload.get("iconId") or payload.get("icon_id"),
            library_type_id=payload.get("libraryTypeId") or payload.get("library_type_id"),
            library_variant_id=payload.get("libraryVariantId") or payload.get("library_variant_id"),
            created_by_user_id=created_by_user_id,
            metadata_json=metadata_value,
        )

    @staticmethod
    def sort_for_palette(blocks: Sequence["BlockType"]) -> List["BlockType"]:
        """Sort block types by default palette order, then id."""
        return sorted(
            list(blocks or []),
            key=lambda block: (
                block.default_palette_index is None,
                block.default_palette_index if block.default_palette_index is not None else 999999,
                block.block_type_id,
            ),
        )

    @property
    def is_active(self) -> bool:
        return self.status == BLOCK_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_deprecated(self) -> bool:
        return self.status == BLOCK_STATUS_DEPRECATED

    @property
    def is_disabled(self) -> bool:
        return self.status == BLOCK_STATUS_DISABLED

    @property
    def is_deleted(self) -> bool:
        return self.status == BLOCK_STATUS_DELETED or self.deleted_at is not None

    @property
    def registry_key(self) -> str:
        return f"{self.registry_id}@{self.registry_version}"

    @property
    def default_cell_value(self) -> Optional[int]:
        """
        Return the default cell value for registry-level debug palettes.

        Actual chunk cell values are always derived from the chunk's own palette.
        """
        if self.default_palette_index is None:
            return None
        return int(self.default_palette_index) + 1

    @property
    def can_be_placed(self) -> bool:
        return self.is_active and bool(self.placeable)

    @property
    def can_be_broken(self) -> bool:
        return self.is_active and bool(self.breakable)

    def touch(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Mark the block type as updated and increment its optimistic revision."""
        self.updated_at = utc_now()
        self.revision = int(self.revision or 1) + 1

        normalized_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="updated_by_user_id",
            max_length=BLOCK_USER_ID_MAX_LENGTH,
        )

        if normalized_user_id is not None:
            self.updated_by_user_id = normalized_user_id

    def ensure_not_deleted(self) -> None:
        """Raise when a mutation is attempted on a soft-deleted block type."""
        if self.is_deleted:
            raise ValueError(
                f"Block type '{self.block_type_id}' is deleted and cannot be modified."
            )

    def set_status(
        self,
        status: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set block type status."""
        normalized_status = normalize_block_status(status)
        now = utc_now()

        if normalized_status == BLOCK_STATUS_DELETED:
            self.deleted_at = self.deleted_at or now
        elif normalized_status == BLOCK_STATUS_DEPRECATED:
            self.deprecated_at = self.deprecated_at or now
            self.deleted_at = None
        elif normalized_status in {BLOCK_STATUS_ACTIVE, BLOCK_STATUS_DISABLED}:
            self.deleted_at = None
            if normalized_status == BLOCK_STATUS_ACTIVE:
                self.deprecated_at = None

        self.status = normalized_status
        self.touch(updated_by_user_id=updated_by_user_id)

    def deprecate(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Mark this block type as deprecated."""
        self.ensure_not_deleted()
        self.status = BLOCK_STATUS_DEPRECATED
        self.deprecated_at = self.deprecated_at or utc_now()
        self.touch(updated_by_user_id=updated_by_user_id)

    def disable(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Disable this block type for new placement while keeping history valid."""
        self.ensure_not_deleted()
        self.status = BLOCK_STATUS_DISABLED
        self.touch(updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Restore an inactive/deprecated/deleted block type."""
        self.status = BLOCK_STATUS_ACTIVE
        self.deprecated_at = None
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Soft-delete this block type."""
        now = utc_now()
        self.status = BLOCK_STATUS_DELETED
        self.deleted_at = self.deleted_at or now
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_default_palette_index(
        self,
        default_palette_index: Optional[int],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set registry-level default palette index.

        Actual ChunkSnapshot palettes remain chunk-local.
        """
        self.ensure_not_deleted()
        self.default_palette_index = normalize_optional_int(
            default_palette_index,
            field_name="default_palette_index",
            min_value=0,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_flags(
        self,
        *,
        solid: Optional[bool] = None,
        opaque: Optional[bool] = None,
        placeable: Optional[bool] = None,
        breakable: Optional[bool] = None,
        selectable: Optional[bool] = None,
        collidable: Optional[bool] = None,
        emits_light: Optional[bool] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Update block interaction/render flags."""
        self.ensure_not_deleted()

        if solid is not None:
            self.solid = normalize_bool(solid, field_name="solid", default=True)
        if opaque is not None:
            self.opaque = normalize_bool(opaque, field_name="opaque", default=True)
        if placeable is not None:
            self.placeable = normalize_bool(placeable, field_name="placeable", default=True)
        if breakable is not None:
            self.breakable = normalize_bool(breakable, field_name="breakable", default=True)
        if selectable is not None:
            self.selectable = normalize_bool(selectable, field_name="selectable", default=True)
        if collidable is not None:
            self.collidable = normalize_bool(collidable, field_name="collidable", default=True)
        if emits_light is not None:
            self.emits_light = normalize_bool(emits_light, field_name="emits_light", default=False)

        self.touch(updated_by_user_id=updated_by_user_id)

    def set_rendering(
        self,
        *,
        render_mode: Optional[str] = None,
        shape_type: Optional[str] = None,
        material_id: Optional[str] = None,
        texture_id: Optional[str] = None,
        icon_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Update render-related block metadata."""
        self.ensure_not_deleted()

        if render_mode is not None:
            self.render_mode = normalize_render_mode(render_mode)
        if shape_type is not None:
            self.shape_type = normalize_shape_type(shape_type)
        if material_id is not None:
            self.material_id = normalize_optional_text(
                material_id,
                field_name="material_id",
                max_length=BLOCK_MATERIAL_ID_MAX_LENGTH,
            )
        if texture_id is not None:
            self.texture_id = normalize_optional_text(
                texture_id,
                field_name="texture_id",
                max_length=BLOCK_TEXTURE_ID_MAX_LENGTH,
            )
        if icon_id is not None:
            self.icon_id = normalize_optional_text(
                icon_id,
                field_name="icon_id",
                max_length=BLOCK_ICON_ID_MAX_LENGTH,
            )

        self.touch(updated_by_user_id=updated_by_user_id)

    def replace_metadata(
        self,
        metadata_json: Optional[Mapping[str, Any]],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Replace metadata_json entirely."""
        self.ensure_not_deleted()
        self.metadata_json = normalize_metadata(metadata_json)
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

        current = normalize_metadata(self.metadata_json)

        for key in remove_keys or []:
            try:
                current.pop(str(key), None)
            except Exception:
                continue

        for key, value in values.items():
            current[str(key)] = make_json_safe(value)

        self.metadata_json = current
        self.touch(updated_by_user_id=updated_by_user_id)

    def apply_patch_payload(
        self,
        payload: Mapping[str, Any],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Apply a PATCH-style API payload.

        Supported mutable keys:
        - label
        - description
        - status
        - category
        - defaultPaletteIndex / default_palette_index
        - solid
        - opaque
        - placeable
        - breakable
        - selectable
        - collidable
        - emitsLight / emits_light
        - lightLevel / light_level
        - hardness
        - stackSize / stack_size
        - renderMode / render_mode
        - shapeType / shape_type
        - materialId / material_id
        - textureId / texture_id
        - iconId / icon_id
        - libraryTypeId / library_type_id
        - libraryVariantId / library_variant_id
        - metadata / metadataJson / metadata_json
        - metadataMerge
        - metadataRemoveKeys
        """
        if not isinstance(payload, Mapping):
            raise ValueError("Block type patch payload must be a JSON object.")

        self.ensure_not_deleted()

        changed = False

        if "label" in payload:
            self.label = normalize_required_text(
                payload.get("label"),
                field_name="label",
                max_length=BLOCK_LABEL_MAX_LENGTH,
            )
            changed = True

        if "description" in payload:
            self.description = normalize_optional_text(
                payload.get("description"),
                field_name="description",
                max_length=BLOCK_DESCRIPTION_MAX_LENGTH,
            )
            changed = True

        if "category" in payload:
            self.category = normalize_block_category(payload.get("category"))
            changed = True

        if "defaultPaletteIndex" in payload or "default_palette_index" in payload:
            self.default_palette_index = normalize_optional_int(
                payload.get("defaultPaletteIndex")
                if "defaultPaletteIndex" in payload
                else payload.get("default_palette_index"),
                field_name="default_palette_index",
                min_value=0,
            )
            changed = True

        for attr, field_name in (
            ("solid", "solid"),
            ("opaque", "opaque"),
            ("placeable", "placeable"),
            ("breakable", "breakable"),
            ("selectable", "selectable"),
            ("collidable", "collidable"),
        ):
            if field_name in payload:
                setattr(
                    self,
                    attr,
                    normalize_bool(payload.get(field_name), field_name=field_name, default=True),
                )
                changed = True

        if "emitsLight" in payload or "emits_light" in payload:
            self.emits_light = normalize_bool(
                payload.get("emitsLight")
                if "emitsLight" in payload
                else payload.get("emits_light"),
                field_name="emits_light",
                default=False,
            )
            changed = True

        if "lightLevel" in payload or "light_level" in payload:
            self.light_level = normalize_required_int(
                payload.get("lightLevel")
                if "lightLevel" in payload
                else payload.get("light_level"),
                field_name="light_level",
                default=0,
                min_value=0,
                max_value=15,
            )
            changed = True

        if "hardness" in payload:
            self.hardness = normalize_required_float(
                payload.get("hardness"),
                field_name="hardness",
                default=1.0,
                min_value=0,
            )
            changed = True

        if "stackSize" in payload or "stack_size" in payload:
            self.stack_size = normalize_required_int(
                payload.get("stackSize")
                if "stackSize" in payload
                else payload.get("stack_size"),
                field_name="stack_size",
                default=64,
                min_value=1,
            )
            changed = True

        if "renderMode" in payload or "render_mode" in payload:
            self.render_mode = normalize_render_mode(
                payload.get("renderMode")
                if "renderMode" in payload
                else payload.get("render_mode")
            )
            changed = True

        if "shapeType" in payload or "shape_type" in payload:
            self.shape_type = normalize_shape_type(
                payload.get("shapeType")
                if "shapeType" in payload
                else payload.get("shape_type")
            )
            changed = True

        if "materialId" in payload or "material_id" in payload:
            self.material_id = normalize_optional_text(
                payload.get("materialId")
                if "materialId" in payload
                else payload.get("material_id"),
                field_name="material_id",
                max_length=BLOCK_MATERIAL_ID_MAX_LENGTH,
            )
            changed = True

        if "textureId" in payload or "texture_id" in payload:
            self.texture_id = normalize_optional_text(
                payload.get("textureId")
                if "textureId" in payload
                else payload.get("texture_id"),
                field_name="texture_id",
                max_length=BLOCK_TEXTURE_ID_MAX_LENGTH,
            )
            changed = True

        if "iconId" in payload or "icon_id" in payload:
            self.icon_id = normalize_optional_text(
                payload.get("iconId")
                if "iconId" in payload
                else payload.get("icon_id"),
                field_name="icon_id",
                max_length=BLOCK_ICON_ID_MAX_LENGTH,
            )
            changed = True

        if "libraryTypeId" in payload or "library_type_id" in payload:
            self.library_type_id = normalize_optional_text(
                payload.get("libraryTypeId")
                if "libraryTypeId" in payload
                else payload.get("library_type_id"),
                field_name="library_type_id",
                max_length=BLOCK_LIBRARY_TYPE_ID_MAX_LENGTH,
            )
            changed = True

        if "libraryVariantId" in payload or "library_variant_id" in payload:
            self.library_variant_id = normalize_optional_text(
                payload.get("libraryVariantId")
                if "libraryVariantId" in payload
                else payload.get("library_variant_id"),
                field_name="library_variant_id",
                max_length=BLOCK_LIBRARY_VARIANT_ID_MAX_LENGTH,
            )
            changed = True

        metadata_replace_key = None
        for candidate in ("metadataJson", "metadata_json", "metadata"):
            if candidate in payload:
                metadata_replace_key = candidate
                break

        if metadata_replace_key is not None:
            self.metadata_json = normalize_metadata(payload.get(metadata_replace_key))
            changed = True

        if "metadataMerge" in payload:
            merge_value = payload.get("metadataMerge")
            if not isinstance(merge_value, Mapping):
                raise ValueError("metadataMerge must be a JSON object/dict.")
            current = normalize_metadata(self.metadata_json)
            for key, value in merge_value.items():
                current[str(key)] = make_json_safe(value)
            self.metadata_json = current
            changed = True

        if "metadataRemoveKeys" in payload:
            remove_keys = payload.get("metadataRemoveKeys") or []
            if not isinstance(remove_keys, Iterable) or isinstance(remove_keys, (str, bytes)):
                raise ValueError("metadataRemoveKeys must be a list of keys.")
            current = normalize_metadata(self.metadata_json)
            for key in remove_keys:
                current.pop(str(key), None)
            self.metadata_json = current
            changed = True

        if "status" in payload:
            self.set_status(
                str(payload.get("status")),
                updated_by_user_id=updated_by_user_id,
            )
            return

        if changed:
            self.touch(updated_by_user_id=updated_by_user_id)

    def get_validation_errors(self) -> Dict[str, str]:
        """Return validation errors without raising."""
        errors: Dict[str, str] = {}

        try:
            normalize_registry_db_id(self.registry_db_id)
        except Exception as exc:
            errors["registryDbId"] = str(exc)

        try:
            normalize_registry_id(self.registry_id)
        except Exception as exc:
            errors["registryId"] = str(exc)

        try:
            normalize_registry_version(self.registry_version)
        except Exception as exc:
            errors["registryVersion"] = str(exc)

        try:
            normalize_block_type_id(self.block_type_id)
        except Exception as exc:
            errors["blockTypeId"] = str(exc)

        try:
            normalize_required_text(
                self.label,
                field_name="label",
                max_length=BLOCK_LABEL_MAX_LENGTH,
            )
        except Exception as exc:
            errors["label"] = str(exc)

        try:
            normalize_block_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_block_category(self.category)
        except Exception as exc:
            errors["category"] = str(exc)

        try:
            normalize_optional_int(
                self.default_palette_index,
                field_name="default_palette_index",
                min_value=0,
            )
        except Exception as exc:
            errors["defaultPaletteIndex"] = str(exc)

        try:
            normalize_required_int(
                self.light_level,
                field_name="light_level",
                default=0,
                min_value=0,
                max_value=15,
            )
        except Exception as exc:
            errors["lightLevel"] = str(exc)

        try:
            normalize_required_float(
                self.hardness,
                field_name="hardness",
                default=1.0,
                min_value=0,
            )
        except Exception as exc:
            errors["hardness"] = str(exc)

        try:
            normalize_required_int(
                self.stack_size,
                field_name="stack_size",
                default=64,
                min_value=1,
            )
        except Exception as exc:
            errors["stackSize"] = str(exc)

        try:
            normalize_render_mode(self.render_mode)
        except Exception as exc:
            errors["renderMode"] = str(exc)

        try:
            normalize_shape_type(self.shape_type)
        except Exception as exc:
            errors["shapeType"] = str(exc)

        try:
            normalize_metadata(self.metadata_json)
        except Exception as exc:
            errors["metadataJson"] = str(exc)

        if self.revision is None or int(self.revision) < 1:
            errors["revision"] = "revision must be greater than or equal to 1."

        return errors

    def to_palette_entry(
        self,
        *,
        palette_index: Optional[int] = None,
        include_metadata: bool = True,
    ) -> Dict[str, Any]:
        """
        Serialize as a palette entry.

        If `palette_index` is omitted, `default_palette_index` is used.
        """
        resolved_palette_index = (
            int(palette_index)
            if palette_index is not None
            else self.default_palette_index
        )

        cell_value = (
            int(resolved_palette_index) + 1
            if resolved_palette_index is not None
            else None
        )

        result: Dict[str, Any] = {
            "paletteIndex": resolved_palette_index,
            "cellValue": cell_value,
            "blockTypeId": self.block_type_id,
            "label": self.label,
            "registryId": self.registry_id,
            "registryVersion": self.registry_version,
            "solid": self.solid,
            "opaque": self.opaque,
            "placeable": self.placeable,
            "breakable": self.breakable,
            "selectable": self.selectable,
            "collidable": self.collidable,
            "emitsLight": self.emits_light,
            "lightLevel": self.light_level,
            "renderMode": self.render_mode,
            "shapeType": self.shape_type,
            "materialId": self.material_id,
            "textureId": self.texture_id,
            "iconId": self.icon_id,
            "status": self.status,
        }

        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)

        return result

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
    ) -> Dict[str, Any]:
        """Serialize block type for API/service responses."""
        result: Dict[str, Any] = {
            "registryId": self.registry_id,
            "registryVersion": self.registry_version,
            "registryKey": self.registry_key,
            "blockTypeId": self.block_type_id,
            "label": self.label,
            "description": self.description,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "category": self.category,
            "defaultPaletteIndex": self.default_palette_index,
            "defaultCellValue": self.default_cell_value,
            "solid": self.solid,
            "opaque": self.opaque,
            "placeable": self.placeable,
            "breakable": self.breakable,
            "selectable": self.selectable,
            "collidable": self.collidable,
            "emitsLight": self.emits_light,
            "lightLevel": self.light_level,
            "hardness": self.hardness,
            "stackSize": self.stack_size,
            "renderMode": self.render_mode,
            "shapeType": self.shape_type,
            "materialId": self.material_id,
            "textureId": self.texture_id,
            "iconId": self.icon_id,
            "libraryTypeId": self.library_type_id,
            "libraryVariantId": self.library_variant_id,
            "createdByUserId": self.created_by_user_id,
            "updatedByUserId": self.updated_by_user_id,
            "createdAt": datetime_to_iso(self.created_at),
            "updatedAt": datetime_to_iso(self.updated_at),
            "deprecatedAt": datetime_to_iso(self.deprecated_at),
            "deletedAt": datetime_to_iso(self.deleted_at),
            "flags": {
                "active": self.is_active,
                "deprecated": self.is_deprecated,
                "disabled": self.is_disabled,
                "deleted": self.is_deleted,
                "canBePlaced": self.can_be_placed,
                "canBeBroken": self.can_be_broken,
            },
        }

        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)

        if include_internal:
            result["id"] = self.id
            result["registryDbId"] = self.registry_db_id

        return result

    def to_public_dict(self) -> Dict[str, Any]:
        """Serialize without internal database identifiers."""
        return self.to_dict(include_internal=False, include_metadata=True)