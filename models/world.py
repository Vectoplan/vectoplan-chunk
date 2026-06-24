# services/vectoplan-chunk/models/world.py
"""
SQLAlchemy model for concrete VECTOPLAN Chunk world instances.

A WorldInstance is the persistent editable world inside a Universe. It is not
the same thing as a provider/template world.

Current intended hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Important design rules:
- `id` is the internal database primary key.
- `project_db_id` references `projects.id`.
- `universe_db_id` references `universes.id`.
- `world_id` is the stable public/API identifier inside one universe.
- `world_id` is only unique per universe, not globally.
- `world_spawn` or generated `chk_wld_...` is a concrete editable world.
- `flat` is a provider/template world and must be stored as
  `template_id` / `provider_world_id`, not as the concrete `world_id`.
- This model stores world configuration, not chunk cells.
- Chunk cells are stored in ChunkSnapshot.
- This model does not perform commits.
- Repository/service/route/bootstrap layers own database transactions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Dict, Optional
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
        "models/world.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
        "`db` instance before importing models."
    ) from _DB_IMPORT_ERROR


try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - fallback is useful for tests/non-postgres tooling
    JSONB = None  # type: ignore[assignment]


try:
    JSON_COLUMN_TYPE = (
        JSONB()
        .with_variant(db.JSON(), "sqlite")
        .with_variant(db.JSON(), "mysql")
    ) if JSONB is not None else db.JSON
except Exception:  # pragma: no cover
    JSON_COLUMN_TYPE = db.JSON


WORLD_INSTANCE_SCHEMA_VERSION = "world-instance.schema.v2"

WORLD_STATUS_ACTIVE = "active"
WORLD_STATUS_ARCHIVED = "archived"
WORLD_STATUS_DELETED = "deleted"

VALID_WORLD_STATUSES = frozenset(
    {
        WORLD_STATUS_ACTIVE,
        WORLD_STATUS_ARCHIVED,
        WORLD_STATUS_DELETED,
    }
)

WORLD_TYPE_RUNTIME = "runtime-world"
WORLD_TYPE_TEMPLATE_INSTANCE = "template-instance"
WORLD_TYPE_IMPORTED = "imported-world"
WORLD_TYPE_SIMULATION = "simulation-world"

VALID_WORLD_TYPES = frozenset(
    {
        WORLD_TYPE_RUNTIME,
        WORLD_TYPE_TEMPLATE_INSTANCE,
        WORLD_TYPE_IMPORTED,
        WORLD_TYPE_SIMULATION,
    }
)

WORLD_SCOPE_PROJECT = "project"

VALID_WORLD_SCOPES = frozenset(
    {
        WORLD_SCOPE_PROJECT,
    }
)

WORLD_ROLE_DEFAULT_SPAWN = "default_spawn"
WORLD_ROLE_DESIGN = "design"
WORLD_ROLE_SITE = "site"
WORLD_ROLE_INTERIOR = "interior"
WORLD_ROLE_IMPORTED = "imported"
WORLD_ROLE_SIMULATION = "simulation"
WORLD_ROLE_SANDBOX = "sandbox"

VALID_WORLD_ROLES = frozenset(
    {
        WORLD_ROLE_DEFAULT_SPAWN,
        WORLD_ROLE_DESIGN,
        WORLD_ROLE_SITE,
        WORLD_ROLE_INTERIOR,
        WORLD_ROLE_IMPORTED,
        WORLD_ROLE_SIMULATION,
        WORLD_ROLE_SANDBOX,
    }
)

DEFAULT_WORLD_ID = "world_spawn"
DEFAULT_WORLD_SLUG = "spawn"
DEFAULT_WORLD_NAME = "Flat Spawn World"

DEFAULT_TEMPLATE_ID = "flat"
DEFAULT_PROVIDER_ID = "flat"
DEFAULT_PROVIDER_WORLD_ID = "flat"

DEFAULT_GENERATOR_TYPE = "flat-world"
DEFAULT_GENERATOR_VERSION = "1"
DEFAULT_PROJECTION_TYPE = "flat-local-v1"
DEFAULT_TOPOLOGY_TYPE = "flat-unbounded-v1"
DEFAULT_COORDINATE_SYSTEM = "vectoplan-world-y-up-v1"

DEFAULT_CHUNK_SIZE = 16
DEFAULT_CELL_SIZE = 1.0
DEFAULT_SURFACE_Y = 0
DEFAULT_MIN_Y = -8
DEFAULT_MAX_Y = 64

DEFAULT_BLOCK_REGISTRY_ID = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION = "1"

DEFAULT_SPAWN_X = 0
DEFAULT_SPAWN_Y = 2
DEFAULT_SPAWN_Z = 0
DEFAULT_SPAWN_YAW = 0.0
DEFAULT_SPAWN_PITCH = 0.0

WORLD_ID_MAX_LENGTH = 96
WORLD_SLUG_MAX_LENGTH = 120
WORLD_NAME_MAX_LENGTH = 255
WORLD_DESCRIPTION_MAX_LENGTH = 4096
WORLD_TYPE_MAX_LENGTH = 64
WORLD_ROLE_MAX_LENGTH = 64
WORLD_SCOPE_MAX_LENGTH = 64
WORLD_TEMPLATE_ID_MAX_LENGTH = 96
WORLD_PROVIDER_ID_MAX_LENGTH = 96
WORLD_PROVIDER_WORLD_ID_MAX_LENGTH = 96
WORLD_GENERATOR_TYPE_MAX_LENGTH = 96
WORLD_GENERATOR_VERSION_MAX_LENGTH = 64
WORLD_PROJECTION_TYPE_MAX_LENGTH = 96
WORLD_TOPOLOGY_TYPE_MAX_LENGTH = 96
WORLD_COORDINATE_SYSTEM_MAX_LENGTH = 128
WORLD_SEED_MAX_LENGTH = 128
WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH = 128
WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH = 64
WORLD_USER_ID_MAX_LENGTH = 128
WORLD_SOURCE_SERVICE_MAX_LENGTH = 96
WORLD_EXTERNAL_REF_MAX_LENGTH = 128

PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


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

    This is intentionally defensive because metadata can later contain values
    coming from editor, importers, scripts or integration layers.
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

    try:
        text = str(value).strip()
    except Exception as exc:
        raise ValueError(f"{field_name} must be text-like.") from exc

    if not text:
        return None

    if len(text) > max_length:
        raise ValueError(
            f"{field_name} must not exceed {max_length} characters."
        )

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
    Normalize public API identifiers.

    Allowed:
    - letters
    - numbers
    - underscore
    - dash
    - dot
    - colon

    The first character must be alphanumeric.
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


def is_provider_like_world_id(value: Any) -> bool:
    """
    Return true if a value looks like a provider/template id rather than a
    concrete editable world id.
    """
    try:
        text = str(value).strip().lower()
    except Exception:
        return False

    if not text:
        return False

    return text in {
        DEFAULT_TEMPLATE_ID,
        DEFAULT_PROVIDER_ID,
        DEFAULT_PROVIDER_WORLD_ID,
    }


def normalize_world_id(value: Any) -> str:
    """Normalize a public concrete world id."""
    return normalize_public_id(
        value,
        field_name="world_id",
        max_length=WORLD_ID_MAX_LENGTH,
    )


def normalize_concrete_world_id(
    value: Any,
    *,
    default: str = DEFAULT_WORLD_ID,
    field_name: str = "world_id",
    allow_provider_like: bool = False,
) -> str:
    """
    Normalize a concrete editable world id.

    By default this rejects provider/template ids such as `flat`. This protects
    the bootstrap path from accidentally using provider ids as concrete world ids.
    """
    fallback = normalize_world_id(default)
    candidate = normalize_world_id(value or fallback)

    if not allow_provider_like and is_provider_like_world_id(candidate):
        return fallback

    return candidate


def normalize_template_id(value: Any) -> str:
    """Normalize a provider/template id such as `flat`."""
    return normalize_public_id(
        value,
        field_name="template_id",
        max_length=WORLD_TEMPLATE_ID_MAX_LENGTH,
    )


def normalize_provider_id(value: Any) -> str:
    """Normalize a provider id such as `flat`."""
    return normalize_public_id(
        value,
        field_name="provider_id",
        max_length=WORLD_PROVIDER_ID_MAX_LENGTH,
    )


def normalize_provider_world_id(value: Any) -> str:
    """Normalize a provider world id such as `flat`."""
    return normalize_public_id(
        value,
        field_name="provider_world_id",
        max_length=WORLD_PROVIDER_WORLD_ID_MAX_LENGTH,
    )


def normalize_slug(value: Any) -> Optional[str]:
    """Normalize optional world slugs."""
    text = normalize_optional_text(
        value,
        field_name="slug",
        max_length=WORLD_SLUG_MAX_LENGTH,
    )

    if text is None:
        return None

    if not SLUG_PATTERN.match(text):
        raise ValueError(
            "slug may only contain letters, numbers, underscores and dashes, "
            "and must start with a letter or number."
        )

    return text


def normalize_status(value: Any) -> str:
    """Normalize and validate world status."""
    if value is None:
        return WORLD_STATUS_ACTIVE

    try:
        status = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("status must be text-like.") from exc

    if status not in VALID_WORLD_STATUSES:
        allowed = ", ".join(sorted(VALID_WORLD_STATUSES))
        raise ValueError(f"Invalid world status '{value}'. Allowed: {allowed}.")

    return status


def normalize_world_type(value: Any) -> str:
    """Normalize and validate world type."""
    if value is None:
        return WORLD_TYPE_RUNTIME

    try:
        world_type = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("world_type must be text-like.") from exc

    if world_type not in VALID_WORLD_TYPES:
        allowed = ", ".join(sorted(VALID_WORLD_TYPES))
        raise ValueError(f"Invalid world type '{value}'. Allowed: {allowed}.")

    return world_type


def normalize_world_role(value: Any) -> str:
    """Normalize and validate world role."""
    if value is None:
        return WORLD_ROLE_DEFAULT_SPAWN

    try:
        role = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("world_role must be text-like.") from exc

    if role not in VALID_WORLD_ROLES:
        allowed = ", ".join(sorted(VALID_WORLD_ROLES))
        raise ValueError(f"Invalid world role '{value}'. Allowed: {allowed}.")

    return role


def normalize_world_scope(value: Any) -> str:
    """Normalize and validate world scope."""
    if value is None:
        return WORLD_SCOPE_PROJECT

    try:
        scope = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("world_scope must be text-like.") from exc

    if scope not in VALID_WORLD_SCOPES:
        allowed = ", ".join(sorted(VALID_WORLD_SCOPES))
        raise ValueError(f"Invalid world scope '{value}'. Allowed: {allowed}.")

    return scope


def normalize_version_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    default: str,
) -> str:
    """
    Normalize version strings.

    Version strings are deliberately less strict than public ids because later
    systems may use semver-like values such as `1.0.0`.
    """
    if value is None:
        value = default

    return normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )


def normalize_metadata(value: Any) -> Dict[str, Any]:
    """
    Normalize metadata into a JSON-safe object.

    Metadata must be an object at the top level. This keeps database content
    predictable and avoids storing lists/scalars where later services expect
    key/value data.
    """
    if value is None:
        return {}

    if not isinstance(value, Mapping):
        raise ValueError("metadata_json must be a JSON object/dict.")

    return make_json_safe(dict(value))


def normalize_db_id(value: Any, *, field_name: str) -> int:
    """Normalize an internal database id."""
    if value is None:
        raise ValueError(f"{field_name} is required.")

    try:
        db_id = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if db_id <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    return db_id


def normalize_positive_int(
    value: Any,
    *,
    field_name: str,
    default: int,
) -> int:
    """Normalize positive integer configuration values."""
    if value is None:
        value = default

    try:
        integer_value = int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc

    if integer_value <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    return integer_value


def normalize_int(
    value: Any,
    *,
    field_name: str,
    default: int,
) -> int:
    """Normalize integer configuration values."""
    if value is None:
        value = default

    try:
        return int(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def normalize_float(
    value: Any,
    *,
    field_name: str,
    default: float,
) -> float:
    """Normalize float configuration values."""
    if value is None:
        value = default

    try:
        return float(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a number.") from exc


def normalize_positive_float(
    value: Any,
    *,
    field_name: str,
    default: float,
) -> float:
    """Normalize positive float configuration values."""
    float_value = normalize_float(
        value,
        field_name=field_name,
        default=default,
    )

    if float_value <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")

    return float_value


def validate_vertical_bounds(*, min_y: int, surface_y: int, max_y: int) -> None:
    """Validate vertical world bounds."""
    if min_y > surface_y:
        raise ValueError("min_y must be less than or equal to surface_y.")

    if surface_y > max_y:
        raise ValueError("surface_y must be less than or equal to max_y.")


def generate_world_id(prefix: str = "world") -> str:
    """
    Generate a stable public world identifier.

    Example:
        world_2f2f7a1c9d3b4a44a5d9e41910b51e70
    """
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="world_id_prefix",
        max_length=24,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def _payload_metadata_value(payload: Mapping[str, Any]) -> Any:
    """Read metadata payload from several compatible keys."""
    if "metadataJson" in payload:
        return payload.get("metadataJson")
    if "metadata_json" in payload:
        return payload.get("metadata_json")
    if "metadata" in payload:
        return payload.get("metadata")
    if "worldMetadata" in payload:
        return payload.get("worldMetadata")
    if "world_metadata" in payload:
        return payload.get("world_metadata")
    return None


def _payload_get(payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """Read first available payload value."""
    for key in keys:
        if key in payload:
            return payload.get(key)
    return default


def _resolve_project_universe_db_ids(
    *,
    project: Any = None,
    universe: Any = None,
    project_db_id: Any = None,
    universe_db_id: Any = None,
) -> tuple[int, int]:
    """Resolve internal project/universe ids from either objects or explicit ids."""
    resolved_project_db_id = project_db_id
    resolved_universe_db_id = universe_db_id

    if resolved_project_db_id is None and project is not None:
        resolved_project_db_id = getattr(project, "id", None)

    if resolved_universe_db_id is None and universe is not None:
        resolved_universe_db_id = getattr(universe, "id", None)

    if resolved_project_db_id is None and universe is not None:
        resolved_project_db_id = getattr(universe, "project_db_id", None)

    return (
        normalize_db_id(resolved_project_db_id, field_name="project_db_id"),
        normalize_db_id(resolved_universe_db_id, field_name="universe_db_id"),
    )


class WorldInstance(db.Model):
    """
    Persistent concrete VECTOPLAN Chunk world instance.

    The default app-integrated flow creates:

        Project(external_app_project_id="prj_...")
          -> Universe(universe_id="chk_uni_...")
              -> WorldInstance(
                     world_id="chk_wld_..." or "world_spawn",
                     template_id="flat",
                     provider_world_id="flat",
                     generator_type="flat-world"
                 )

    Important:
    - this model stores world configuration and provider mapping,
    - this model does not store generated chunks,
    - unchanged chunks are generated from provider/template config,
    - changed chunks are stored as ChunkSnapshot.
    """

    __tablename__ = "world_instances"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
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

    world_id = db.Column(
        db.String(WORLD_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    slug = db.Column(
        db.String(WORLD_SLUG_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    name = db.Column(
        db.String(WORLD_NAME_MAX_LENGTH),
        nullable=False,
    )

    description = db.Column(
        db.String(WORLD_DESCRIPTION_MAX_LENGTH),
        nullable=True,
    )

    status = db.Column(
        db.String(32),
        nullable=False,
        default=WORLD_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(64),
        nullable=False,
        default=WORLD_INSTANCE_SCHEMA_VERSION,
    )

    revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    world_type = db.Column(
        db.String(WORLD_TYPE_MAX_LENGTH),
        nullable=False,
        default=WORLD_TYPE_RUNTIME,
        index=True,
    )

    world_role = db.Column(
        db.String(WORLD_ROLE_MAX_LENGTH),
        nullable=False,
        default=WORLD_ROLE_DEFAULT_SPAWN,
        index=True,
    )

    world_scope = db.Column(
        db.String(WORLD_SCOPE_MAX_LENGTH),
        nullable=False,
        default=WORLD_SCOPE_PROJECT,
        index=True,
    )

    template_id = db.Column(
        db.String(WORLD_TEMPLATE_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_TEMPLATE_ID,
        index=True,
    )

    provider_id = db.Column(
        db.String(WORLD_PROVIDER_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_PROVIDER_ID,
        index=True,
    )

    provider_world_id = db.Column(
        db.String(WORLD_PROVIDER_WORLD_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_PROVIDER_WORLD_ID,
        index=True,
    )

    generator_type = db.Column(
        db.String(WORLD_GENERATOR_TYPE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_GENERATOR_TYPE,
        index=True,
    )

    generator_version = db.Column(
        db.String(WORLD_GENERATOR_VERSION_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_GENERATOR_VERSION,
        index=True,
    )

    projection_type = db.Column(
        db.String(WORLD_PROJECTION_TYPE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_PROJECTION_TYPE,
        index=True,
    )

    topology_type = db.Column(
        db.String(WORLD_TOPOLOGY_TYPE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_TOPOLOGY_TYPE,
        index=True,
    )

    coordinate_system = db.Column(
        db.String(WORLD_COORDINATE_SYSTEM_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_COORDINATE_SYSTEM,
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

    surface_y = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_SURFACE_Y,
    )

    min_y = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_MIN_Y,
    )

    max_y = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_MAX_Y,
    )

    seed = db.Column(
        db.String(WORLD_SEED_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    block_registry_id = db.Column(
        db.String(WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_BLOCK_REGISTRY_ID,
        index=True,
    )

    block_registry_version = db.Column(
        db.String(WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_BLOCK_REGISTRY_VERSION,
        index=True,
    )

    spawn_x = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_SPAWN_X,
    )

    spawn_y = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_SPAWN_Y,
    )

    spawn_z = db.Column(
        db.Integer,
        nullable=False,
        default=DEFAULT_SPAWN_Z,
    )

    spawn_yaw = db.Column(
        db.Float,
        nullable=False,
        default=DEFAULT_SPAWN_YAW,
    )

    spawn_pitch = db.Column(
        db.Float,
        nullable=False,
        default=DEFAULT_SPAWN_PITCH,
    )

    source_service = db.Column(
        db.String(WORLD_SOURCE_SERVICE_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    external_ref = db.Column(
        db.String(WORLD_EXTERNAL_REF_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_by_user_id = db.Column(
        db.String(WORLD_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_by_user_id = db.Column(
        db.String(WORLD_USER_ID_MAX_LENGTH),
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
            "world_instances",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    universe = db.relationship(
        "Universe",
        backref=db.backref(
            "worlds",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "universe_db_id",
            "world_id",
            name="uq_world_instances_universe_world_id",
        ),
        db.UniqueConstraint(
            "universe_db_id",
            "slug",
            name="uq_world_instances_universe_slug",
        ),
        db.CheckConstraint(
            "project_db_id > 0",
            name="ck_world_instances_project_db_id_positive",
        ),
        db.CheckConstraint(
            "universe_db_id > 0",
            name="ck_world_instances_universe_db_id_positive",
        ),
        db.CheckConstraint(
            "world_id <> ''",
            name="ck_world_instances_world_id_not_empty",
        ),
        db.CheckConstraint(
            "name <> ''",
            name="ck_world_instances_name_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_world_instances_status_valid",
        ),
        db.CheckConstraint(
            "world_type IN ('runtime-world', 'template-instance', 'imported-world', 'simulation-world')",
            name="ck_world_instances_world_type_valid",
        ),
        db.CheckConstraint(
            "world_role IN ('default_spawn', 'design', 'site', 'interior', 'imported', 'simulation', 'sandbox')",
            name="ck_world_instances_world_role_valid",
        ),
        db.CheckConstraint(
            "world_scope IN ('project')",
            name="ck_world_instances_world_scope_valid",
        ),
        db.CheckConstraint(
            "revision >= 1",
            name="ck_world_instances_revision_positive",
        ),
        db.CheckConstraint(
            "chunk_size > 0",
            name="ck_world_instances_chunk_size_positive",
        ),
        db.CheckConstraint(
            "cell_size > 0",
            name="ck_world_instances_cell_size_positive",
        ),
        db.CheckConstraint(
            "min_y <= surface_y",
            name="ck_world_instances_min_y_lte_surface_y",
        ),
        db.CheckConstraint(
            "surface_y <= max_y",
            name="ck_world_instances_surface_y_lte_max_y",
        ),
        db.Index(
            "ix_world_instances_project_universe_status",
            "project_db_id",
            "universe_db_id",
            "status",
        ),
        db.Index(
            "ix_world_instances_project_role",
            "project_db_id",
            "world_role",
        ),
        db.Index(
            "ix_world_instances_provider_mapping",
            "provider_id",
            "provider_world_id",
            "template_id",
        ),
        db.Index(
            "ix_world_instances_generator",
            "generator_type",
            "generator_version",
        ),
        db.Index(
            "ix_world_instances_registry",
            "block_registry_id",
            "block_registry_version",
        ),
        db.Index(
            "ix_world_instances_source_external",
            "source_service",
            "external_ref",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<WorldInstance id={self.id!r} project_db_id={self.project_db_id!r} "
            f"universe_db_id={self.universe_db_id!r} world_id={self.world_id!r} "
            f"provider_world_id={self.provider_world_id!r} status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: int,
        universe_db_id: int,
        world_id: Optional[str] = None,
        slug: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: str = WORLD_STATUS_ACTIVE,
        world_type: str = WORLD_TYPE_RUNTIME,
        world_role: str = WORLD_ROLE_DEFAULT_SPAWN,
        world_scope: str = WORLD_SCOPE_PROJECT,
        template_id: str = DEFAULT_TEMPLATE_ID,
        provider_id: str = DEFAULT_PROVIDER_ID,
        provider_world_id: str = DEFAULT_PROVIDER_WORLD_ID,
        generator_type: str = DEFAULT_GENERATOR_TYPE,
        generator_version: str = DEFAULT_GENERATOR_VERSION,
        projection_type: str = DEFAULT_PROJECTION_TYPE,
        topology_type: str = DEFAULT_TOPOLOGY_TYPE,
        coordinate_system: str = DEFAULT_COORDINATE_SYSTEM,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        cell_size: float = DEFAULT_CELL_SIZE,
        surface_y: int = DEFAULT_SURFACE_Y,
        min_y: int = DEFAULT_MIN_Y,
        max_y: int = DEFAULT_MAX_Y,
        seed: Optional[str] = None,
        block_registry_id: str = DEFAULT_BLOCK_REGISTRY_ID,
        block_registry_version: str = DEFAULT_BLOCK_REGISTRY_VERSION,
        spawn_x: int = DEFAULT_SPAWN_X,
        spawn_y: int = DEFAULT_SPAWN_Y,
        spawn_z: int = DEFAULT_SPAWN_Z,
        spawn_yaw: float = DEFAULT_SPAWN_YAW,
        spawn_pitch: float = DEFAULT_SPAWN_PITCH,
        source_service: Optional[str] = None,
        external_ref: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        allow_provider_like_world_id: bool = False,
    ) -> "WorldInstance":
        """
        Create a WorldInstance without adding it to a session.

        Repository/service code is responsible for:
        - checking project/universe existence,
        - checking that universe belongs to project,
        - checking uniqueness inside the universe,
        - adding to db.session,
        - committing or rolling back.
        """
        normalized_project_db_id = normalize_db_id(
            project_db_id,
            field_name="project_db_id",
        )
        normalized_universe_db_id = normalize_db_id(
            universe_db_id,
            field_name="universe_db_id",
        )

        public_world_id = normalize_concrete_world_id(
            world_id or generate_world_id(),
            allow_provider_like=allow_provider_like_world_id,
        )

        normalized_name = normalize_required_text(
            name or public_world_id,
            field_name="name",
            max_length=WORLD_NAME_MAX_LENGTH,
        )

        normalized_status = normalize_status(status)

        normalized_chunk_size = normalize_positive_int(
            chunk_size,
            field_name="chunk_size",
            default=DEFAULT_CHUNK_SIZE,
        )
        normalized_cell_size = normalize_positive_float(
            cell_size,
            field_name="cell_size",
            default=DEFAULT_CELL_SIZE,
        )
        normalized_surface_y = normalize_int(
            surface_y,
            field_name="surface_y",
            default=DEFAULT_SURFACE_Y,
        )
        normalized_min_y = normalize_int(
            min_y,
            field_name="min_y",
            default=DEFAULT_MIN_Y,
        )
        normalized_max_y = normalize_int(
            max_y,
            field_name="max_y",
            default=DEFAULT_MAX_Y,
        )
        validate_vertical_bounds(
            min_y=normalized_min_y,
            surface_y=normalized_surface_y,
            max_y=normalized_max_y,
        )

        now = utc_now()

        return cls(
            project_db_id=normalized_project_db_id,
            universe_db_id=normalized_universe_db_id,
            world_id=public_world_id,
            slug=normalize_slug(slug),
            name=normalized_name,
            description=normalize_optional_text(
                description,
                field_name="description",
                max_length=WORLD_DESCRIPTION_MAX_LENGTH,
            ),
            status=normalized_status,
            schema_version=WORLD_INSTANCE_SCHEMA_VERSION,
            revision=1,
            world_type=normalize_world_type(world_type),
            world_role=normalize_world_role(world_role),
            world_scope=normalize_world_scope(world_scope),
            template_id=normalize_template_id(template_id),
            provider_id=normalize_provider_id(provider_id),
            provider_world_id=normalize_provider_world_id(provider_world_id),
            generator_type=normalize_required_text(
                generator_type,
                field_name="generator_type",
                max_length=WORLD_GENERATOR_TYPE_MAX_LENGTH,
            ),
            generator_version=normalize_version_text(
                generator_version,
                field_name="generator_version",
                max_length=WORLD_GENERATOR_VERSION_MAX_LENGTH,
                default=DEFAULT_GENERATOR_VERSION,
            ),
            projection_type=normalize_required_text(
                projection_type,
                field_name="projection_type",
                max_length=WORLD_PROJECTION_TYPE_MAX_LENGTH,
            ),
            topology_type=normalize_required_text(
                topology_type,
                field_name="topology_type",
                max_length=WORLD_TOPOLOGY_TYPE_MAX_LENGTH,
            ),
            coordinate_system=normalize_required_text(
                coordinate_system,
                field_name="coordinate_system",
                max_length=WORLD_COORDINATE_SYSTEM_MAX_LENGTH,
            ),
            chunk_size=normalized_chunk_size,
            cell_size=normalized_cell_size,
            surface_y=normalized_surface_y,
            min_y=normalized_min_y,
            max_y=normalized_max_y,
            seed=normalize_optional_text(
                seed,
                field_name="seed",
                max_length=WORLD_SEED_MAX_LENGTH,
            ),
            block_registry_id=normalize_required_text(
                block_registry_id,
                field_name="block_registry_id",
                max_length=WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH,
            ),
            block_registry_version=normalize_version_text(
                block_registry_version,
                field_name="block_registry_version",
                max_length=WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH,
                default=DEFAULT_BLOCK_REGISTRY_VERSION,
            ),
            spawn_x=normalize_int(
                spawn_x,
                field_name="spawn_x",
                default=DEFAULT_SPAWN_X,
            ),
            spawn_y=normalize_int(
                spawn_y,
                field_name="spawn_y",
                default=DEFAULT_SPAWN_Y,
            ),
            spawn_z=normalize_int(
                spawn_z,
                field_name="spawn_z",
                default=DEFAULT_SPAWN_Z,
            ),
            spawn_yaw=normalize_float(
                spawn_yaw,
                field_name="spawn_yaw",
                default=DEFAULT_SPAWN_YAW,
            ),
            spawn_pitch=normalize_float(
                spawn_pitch,
                field_name="spawn_pitch",
                default=DEFAULT_SPAWN_PITCH,
            ),
            source_service=normalize_optional_text(
                source_service,
                field_name="source_service",
                max_length=WORLD_SOURCE_SERVICE_MAX_LENGTH,
            ),
            external_ref=normalize_optional_text(
                external_ref,
                field_name="external_ref",
                max_length=WORLD_EXTERNAL_REF_MAX_LENGTH,
            ),
            created_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="created_by_user_id",
                max_length=WORLD_USER_ID_MAX_LENGTH,
            ),
            updated_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="updated_by_user_id",
                max_length=WORLD_USER_ID_MAX_LENGTH,
            ),
            metadata_json=normalize_metadata(metadata_json),
            created_at=now,
            updated_at=now,
            archived_at=now if normalized_status == WORLD_STATUS_ARCHIVED else None,
            deleted_at=now if normalized_status == WORLD_STATUS_DELETED else None,
        )

    @classmethod
    def create_flat_spawn(
        cls,
        *,
        project: Any = None,
        universe: Any = None,
        project_db_id: Optional[int] = None,
        universe_db_id: Optional[int] = None,
        world_id: str = DEFAULT_WORLD_ID,
        slug: str = DEFAULT_WORLD_SLUG,
        name: str = DEFAULT_WORLD_NAME,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        source_service: Optional[str] = "vectoplan-chunk-bootstrap",
        external_ref: Optional[str] = None,
    ) -> "WorldInstance":
        """
        Create the default project spawn world backed by the `flat` provider.

        This factory accepts either:
        - explicit `project_db_id` and `universe_db_id`, or
        - persisted `project` and `universe` model instances.

        This flexibility is intentional because bootstrap paths and provisioning
        paths do not always call factories with the same shape.
        """
        resolved_project_db_id, resolved_universe_db_id = _resolve_project_universe_db_ids(
            project=project,
            universe=universe,
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
        )

        concrete_world_id = normalize_concrete_world_id(
            world_id or DEFAULT_WORLD_ID,
            default=DEFAULT_WORLD_ID,
        )

        merged_metadata = normalize_metadata(metadata_json)
        merged_metadata.setdefault("schemaVersion", WORLD_INSTANCE_SCHEMA_VERSION)
        merged_metadata.setdefault("seededBy", "WorldInstance.create_flat_spawn")
        merged_metadata.setdefault("chunkWorldId", concrete_world_id)
        merged_metadata.setdefault("templateId", DEFAULT_TEMPLATE_ID)
        merged_metadata.setdefault("providerId", DEFAULT_PROVIDER_ID)
        merged_metadata.setdefault("providerWorldId", DEFAULT_PROVIDER_WORLD_ID)

        return cls.create(
            project_db_id=resolved_project_db_id,
            universe_db_id=resolved_universe_db_id,
            world_id=concrete_world_id,
            slug=slug,
            name=name,
            status=WORLD_STATUS_ACTIVE,
            world_type=WORLD_TYPE_RUNTIME,
            world_role=WORLD_ROLE_DEFAULT_SPAWN,
            world_scope=WORLD_SCOPE_PROJECT,
            template_id=DEFAULT_TEMPLATE_ID,
            provider_id=DEFAULT_PROVIDER_ID,
            provider_world_id=DEFAULT_PROVIDER_WORLD_ID,
            generator_type=DEFAULT_GENERATOR_TYPE,
            generator_version=DEFAULT_GENERATOR_VERSION,
            projection_type=DEFAULT_PROJECTION_TYPE,
            topology_type=DEFAULT_TOPOLOGY_TYPE,
            coordinate_system=DEFAULT_COORDINATE_SYSTEM,
            chunk_size=DEFAULT_CHUNK_SIZE,
            cell_size=DEFAULT_CELL_SIZE,
            surface_y=DEFAULT_SURFACE_Y,
            min_y=DEFAULT_MIN_Y,
            max_y=DEFAULT_MAX_Y,
            block_registry_id=DEFAULT_BLOCK_REGISTRY_ID,
            block_registry_version=DEFAULT_BLOCK_REGISTRY_VERSION,
            spawn_x=DEFAULT_SPAWN_X,
            spawn_y=DEFAULT_SPAWN_Y,
            spawn_z=DEFAULT_SPAWN_Z,
            spawn_yaw=DEFAULT_SPAWN_YAW,
            spawn_pitch=DEFAULT_SPAWN_PITCH,
            source_service=source_service,
            external_ref=external_ref or concrete_world_id,
            created_by_user_id=created_by_user_id,
            metadata_json=merged_metadata,
        )

    @classmethod
    def create_for_universe(
        cls,
        universe: Any,
        *,
        world_id: Optional[str] = None,
        slug: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        world_role: str = WORLD_ROLE_DEFAULT_SPAWN,
        template_id: str = DEFAULT_TEMPLATE_ID,
        provider_id: str = DEFAULT_PROVIDER_ID,
        provider_world_id: str = DEFAULT_PROVIDER_WORLD_ID,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        **world_config: Any,
    ) -> "WorldInstance":
        """
        Create a world for a Universe model instance.

        The universe instance must already have internal database ids.
        """
        project_db_id = getattr(universe, "project_db_id", None)
        universe_db_id = getattr(universe, "id", None)

        if project_db_id is None:
            raise ValueError(
                "Cannot create world for universe without universe.project_db_id."
            )

        if universe_db_id is None:
            raise ValueError(
                "Cannot create world for universe without persisted universe.id."
            )

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_id=world_id,
            slug=slug,
            name=name,
            description=description,
            world_role=world_role,
            template_id=template_id,
            provider_id=provider_id,
            provider_world_id=provider_world_id,
            created_by_user_id=created_by_user_id,
            metadata_json=metadata_json,
            **world_config,
        )

    @classmethod
    def from_create_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        project_db_id: int,
        universe_db_id: int,
        created_by_user_id: Optional[str] = None,
    ) -> "WorldInstance":
        """
        Create a WorldInstance from an API-style payload.

        Supported keys:
        - worldId / world_id / chunkWorldId / chunk_world_id
        - name / worldName
        - slug
        - description
        - worldType / world_type
        - worldRole / world_role
        - templateId / template_id
        - providerId / provider_id
        - providerWorldId / provider_world_id
        - generatorType / generator_type
        - generatorVersion / generator_version
        - projectionType / projection_type
        - topologyType / topology_type
        - coordinateSystem / coordinate_system
        - chunkSize / chunk_size
        - cellSize / cell_size
        - surfaceY / surface_y
        - minY / min_y
        - maxY / max_y
        - seed
        - blockRegistryId / block_registry_id
        - blockRegistryVersion / block_registry_version
        - spawn / spawnX/spawnY/spawnZ/spawnYaw/spawnPitch
        - metadata / metadataJson / metadata_json
        """
        if not isinstance(payload, Mapping):
            raise ValueError("World create payload must be a JSON object.")

        metadata_value = _payload_metadata_value(payload)
        spawn = payload.get("spawn") if isinstance(payload.get("spawn"), Mapping) else {}

        raw_world_id = (
            payload.get("chunkWorldId")
            or payload.get("chunk_world_id")
            or payload.get("worldId")
            or payload.get("world_id")
        )

        return cls.create(
            project_db_id=project_db_id,
            universe_db_id=universe_db_id,
            world_id=raw_world_id,
            slug=payload.get("slug") or payload.get("worldSlug") or payload.get("world_slug"),
            name=payload.get("worldName") or payload.get("world_name") or payload.get("name"),
            description=payload.get("description"),
            world_type=payload.get("worldType") or payload.get("world_type") or WORLD_TYPE_RUNTIME,
            world_role=payload.get("worldRole") or payload.get("world_role") or WORLD_ROLE_DEFAULT_SPAWN,
            world_scope=payload.get("worldScope") or payload.get("world_scope") or WORLD_SCOPE_PROJECT,
            template_id=payload.get("templateId") or payload.get("template_id") or DEFAULT_TEMPLATE_ID,
            provider_id=payload.get("providerId") or payload.get("provider_id") or DEFAULT_PROVIDER_ID,
            provider_world_id=payload.get("providerWorldId") or payload.get("provider_world_id") or DEFAULT_PROVIDER_WORLD_ID,
            generator_type=payload.get("generatorType") or payload.get("generator_type") or DEFAULT_GENERATOR_TYPE,
            generator_version=payload.get("generatorVersion") or payload.get("generator_version") or DEFAULT_GENERATOR_VERSION,
            projection_type=payload.get("projectionType") or payload.get("projection_type") or DEFAULT_PROJECTION_TYPE,
            topology_type=payload.get("topologyType") or payload.get("topology_type") or DEFAULT_TOPOLOGY_TYPE,
            coordinate_system=payload.get("coordinateSystem") or payload.get("coordinate_system") or DEFAULT_COORDINATE_SYSTEM,
            chunk_size=payload.get("chunkSize") or payload.get("chunk_size") or DEFAULT_CHUNK_SIZE,
            cell_size=payload.get("cellSize") or payload.get("cell_size") or DEFAULT_CELL_SIZE,
            surface_y=payload.get("surfaceY") if "surfaceY" in payload else payload.get("surface_y", DEFAULT_SURFACE_Y),
            min_y=payload.get("minY") if "minY" in payload else payload.get("min_y", DEFAULT_MIN_Y),
            max_y=payload.get("maxY") if "maxY" in payload else payload.get("max_y", DEFAULT_MAX_Y),
            seed=payload.get("seed"),
            block_registry_id=payload.get("blockRegistryId") or payload.get("block_registry_id") or DEFAULT_BLOCK_REGISTRY_ID,
            block_registry_version=payload.get("blockRegistryVersion") or payload.get("block_registry_version") or DEFAULT_BLOCK_REGISTRY_VERSION,
            spawn_x=payload.get("spawnX") if "spawnX" in payload else payload.get("spawn_x", spawn.get("x", DEFAULT_SPAWN_X)),
            spawn_y=payload.get("spawnY") if "spawnY" in payload else payload.get("spawn_y", spawn.get("y", DEFAULT_SPAWN_Y)),
            spawn_z=payload.get("spawnZ") if "spawnZ" in payload else payload.get("spawn_z", spawn.get("z", DEFAULT_SPAWN_Z)),
            spawn_yaw=payload.get("spawnYaw") if "spawnYaw" in payload else payload.get("spawn_yaw", spawn.get("yaw", DEFAULT_SPAWN_YAW)),
            spawn_pitch=payload.get("spawnPitch") if "spawnPitch" in payload else payload.get("spawn_pitch", spawn.get("pitch", DEFAULT_SPAWN_PITCH)),
            source_service=payload.get("sourceService") or payload.get("source_service"),
            external_ref=payload.get("externalRef") or payload.get("external_ref"),
            created_by_user_id=created_by_user_id,
            metadata_json=metadata_value,
        )

    @property
    def is_active(self) -> bool:
        return self.status == WORLD_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_archived(self) -> bool:
        return self.status == WORLD_STATUS_ARCHIVED

    @property
    def is_deleted(self) -> bool:
        return self.status == WORLD_STATUS_DELETED or self.deleted_at is not None

    @property
    def project_public_id(self) -> Optional[str]:
        """Return the parent project's public id if the relationship is available."""
        try:
            project = getattr(self, "project", None)
            return getattr(project, "project_id", None)
        except Exception:
            return None

    @property
    def universe_public_id(self) -> Optional[str]:
        """Return the parent universe's public id if the relationship is available."""
        try:
            universe = getattr(self, "universe", None)
            return getattr(universe, "universe_id", None)
        except Exception:
            return None

    @property
    def chunk_project_id(self) -> Optional[str]:
        """Compatibility alias for parent Project.project_id."""
        return self.project_public_id

    @property
    def chunk_universe_id(self) -> Optional[str]:
        """Compatibility alias for parent Universe.universe_id."""
        return self.universe_public_id

    @property
    def chunk_world_id(self) -> str:
        """Compatibility alias for world_id."""
        return self.world_id

    @property
    def chunk_config(self) -> Dict[str, Any]:
        """Return chunk/grid configuration in API-compatible shape."""
        return {
            "chunkSize": self.chunk_size,
            "cellSize": self.cell_size,
            "coordinateSystem": self.coordinate_system,
            "projectionType": self.projection_type,
            "topologyType": self.topology_type,
            "surfaceY": self.surface_y,
            "minY": self.min_y,
            "maxY": self.max_y,
        }

    @property
    def provider_mapping(self) -> Dict[str, Any]:
        """Return provider/template mapping in API-compatible shape."""
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
    def spawn_position(self) -> Dict[str, int]:
        """Return spawn position."""
        return {
            "x": int(self.spawn_x),
            "y": int(self.spawn_y),
            "z": int(self.spawn_z),
        }

    @property
    def spawn_rotation(self) -> Dict[str, float]:
        """Return spawn camera/player rotation."""
        return {
            "yaw": float(self.spawn_yaw),
            "pitch": float(self.spawn_pitch),
        }

    @property
    def spawn_context(self) -> Dict[str, Any]:
        """Return combined spawn context."""
        return {
            "position": self.spawn_position,
            "rotation": self.spawn_rotation,
        }

    def build_world_context_key(self) -> str:
        """Build a stable debug/context key for logs and traces."""
        return f"{self.project_db_id}:{self.universe_db_id}:{self.world_id}"

    def build_route_hints(self, *, api_prefix: str = "") -> Dict[str, str]:
        """Build project-scoped route hints for this world."""
        prefix = str(api_prefix or "").rstrip("/")
        project_id = self.project_public_id or str(self.project_db_id)
        world_id = self.world_id

        return {
            "projectBootstrap": f"{prefix}/projects/{project_id}/bootstrap",
            "project": f"{prefix}/projects/{project_id}",
            "worlds": f"{prefix}/projects/{project_id}/worlds",
            "world": f"{prefix}/projects/{project_id}/worlds/{world_id}",
            "blocks": f"{prefix}/projects/{project_id}/worlds/{world_id}/blocks",
            "chunk": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks",
            "chunks": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks",
            "chunksBatch": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks/batch",
            "commands": f"{prefix}/projects/{project_id}/worlds/{world_id}/commands",
        }

    def touch(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Mark the world as updated and increment its optimistic revision."""
        self.updated_at = utc_now()
        self.revision = int(self.revision or 1) + 1

        normalized_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="updated_by_user_id",
            max_length=WORLD_USER_ID_MAX_LENGTH,
        )

        if normalized_user_id is not None:
            self.updated_by_user_id = normalized_user_id

    def ensure_not_deleted(self) -> None:
        """Raise when a mutation is attempted on a soft-deleted world."""
        if self.is_deleted:
            raise ValueError(
                f"World '{self.world_id}' is deleted and cannot be modified."
            )

    def rename(
        self,
        *,
        name: str,
        slug: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Rename the world and optionally update its slug."""
        self.ensure_not_deleted()
        self.name = normalize_required_text(
            name,
            field_name="name",
            max_length=WORLD_NAME_MAX_LENGTH,
        )

        if slug is not None:
            self.slug = normalize_slug(slug)

        self.touch(updated_by_user_id=updated_by_user_id)

    def update_description(
        self,
        description: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Update the world description."""
        self.ensure_not_deleted()
        self.description = normalize_optional_text(
            description,
            field_name="description",
            max_length=WORLD_DESCRIPTION_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_role(
        self,
        world_role: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set the world role."""
        self.ensure_not_deleted()
        self.world_role = normalize_world_role(world_role)
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_status(
        self,
        status: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set world status.

        Prefer using archive(), restore() and soft_delete() in application code
        because those methods maintain timestamp fields consistently.
        """
        normalized_status = normalize_status(status)
        now = utc_now()

        if normalized_status == WORLD_STATUS_DELETED:
            self.deleted_at = self.deleted_at or now
        elif normalized_status == WORLD_STATUS_ARCHIVED:
            self.archived_at = self.archived_at or now
            self.deleted_at = None
        elif normalized_status == WORLD_STATUS_ACTIVE:
            self.archived_at = None
            self.deleted_at = None

        self.status = normalized_status
        self.touch(updated_by_user_id=updated_by_user_id)

    def archive(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Archive the world without deleting historical data."""
        self.ensure_not_deleted()
        self.status = WORLD_STATUS_ARCHIVED
        self.archived_at = self.archived_at or utc_now()
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Restore an archived or soft-deleted world."""
        self.status = WORLD_STATUS_ACTIVE
        self.archived_at = None
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """
        Soft-delete the world.

        This intentionally keeps snapshots, command logs and events available
        for audit/history/AI-training purposes unless a later explicit purge
        process removes them.
        """
        now = utc_now()
        self.status = WORLD_STATUS_DELETED
        self.deleted_at = self.deleted_at or now
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_provider_mapping(
        self,
        *,
        template_id: str,
        provider_id: str,
        provider_world_id: str,
        generator_type: str,
        generator_version: str,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update provider/template mapping.

        This should be used carefully. Existing materialized snapshots keep
        their saved content. Unmaterialized chunks may change if the provider
        mapping or generator changes.
        """
        self.ensure_not_deleted()
        self.template_id = normalize_template_id(template_id)
        self.provider_id = normalize_provider_id(provider_id)
        self.provider_world_id = normalize_provider_world_id(provider_world_id)
        self.generator_type = normalize_required_text(
            generator_type,
            field_name="generator_type",
            max_length=WORLD_GENERATOR_TYPE_MAX_LENGTH,
        )
        self.generator_version = normalize_version_text(
            generator_version,
            field_name="generator_version",
            max_length=WORLD_GENERATOR_VERSION_MAX_LENGTH,
            default=DEFAULT_GENERATOR_VERSION,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_world_geometry(
        self,
        *,
        projection_type: str,
        topology_type: str,
        coordinate_system: str,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update world geometry metadata.

        This does not migrate existing ChunkSnapshots. Migration/rebase logic
        must be implemented separately.
        """
        self.ensure_not_deleted()
        self.projection_type = normalize_required_text(
            projection_type,
            field_name="projection_type",
            max_length=WORLD_PROJECTION_TYPE_MAX_LENGTH,
        )
        self.topology_type = normalize_required_text(
            topology_type,
            field_name="topology_type",
            max_length=WORLD_TOPOLOGY_TYPE_MAX_LENGTH,
        )
        self.coordinate_system = normalize_required_text(
            coordinate_system,
            field_name="coordinate_system",
            max_length=WORLD_COORDINATE_SYSTEM_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_chunk_grid(
        self,
        *,
        chunk_size: int,
        cell_size: float,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update chunk grid configuration.

        This should generally only be used before chunks are materialized.
        Existing ChunkSnapshots are not automatically migrated.
        """
        self.ensure_not_deleted()
        self.chunk_size = normalize_positive_int(
            chunk_size,
            field_name="chunk_size",
            default=DEFAULT_CHUNK_SIZE,
        )
        self.cell_size = normalize_positive_float(
            cell_size,
            field_name="cell_size",
            default=DEFAULT_CELL_SIZE,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_vertical_bounds(
        self,
        *,
        min_y: int,
        surface_y: int,
        max_y: int,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update vertical generation bounds.

        Existing ChunkSnapshots are not automatically migrated.
        """
        self.ensure_not_deleted()

        normalized_min_y = normalize_int(
            min_y,
            field_name="min_y",
            default=DEFAULT_MIN_Y,
        )
        normalized_surface_y = normalize_int(
            surface_y,
            field_name="surface_y",
            default=DEFAULT_SURFACE_Y,
        )
        normalized_max_y = normalize_int(
            max_y,
            field_name="max_y",
            default=DEFAULT_MAX_Y,
        )

        validate_vertical_bounds(
            min_y=normalized_min_y,
            surface_y=normalized_surface_y,
            max_y=normalized_max_y,
        )

        self.min_y = normalized_min_y
        self.surface_y = normalized_surface_y
        self.max_y = normalized_max_y
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_block_registry(
        self,
        *,
        block_registry_id: str,
        block_registry_version: str,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Update block registry context.

        Existing ChunkSnapshots keep their saved registry metadata.
        """
        self.ensure_not_deleted()
        self.block_registry_id = normalize_required_text(
            block_registry_id,
            field_name="block_registry_id",
            max_length=WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH,
        )
        self.block_registry_version = normalize_version_text(
            block_registry_version,
            field_name="block_registry_version",
            max_length=WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH,
            default=DEFAULT_BLOCK_REGISTRY_VERSION,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_seed(
        self,
        seed: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set generator seed.

        Existing materialized chunks are not automatically regenerated.
        """
        self.ensure_not_deleted()
        self.seed = normalize_optional_text(
            seed,
            field_name="seed",
            max_length=WORLD_SEED_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_spawn_position(
        self,
        *,
        x: int,
        y: int,
        z: int,
        yaw: Optional[float] = None,
        pitch: Optional[float] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set world spawn position and optional rotation."""
        self.ensure_not_deleted()
        self.spawn_x = normalize_int(x, field_name="spawn_x", default=DEFAULT_SPAWN_X)
        self.spawn_y = normalize_int(y, field_name="spawn_y", default=DEFAULT_SPAWN_Y)
        self.spawn_z = normalize_int(z, field_name="spawn_z", default=DEFAULT_SPAWN_Z)

        if yaw is not None:
            self.spawn_yaw = normalize_float(
                yaw,
                field_name="spawn_yaw",
                default=DEFAULT_SPAWN_YAW,
            )

        if pitch is not None:
            self.spawn_pitch = normalize_float(
                pitch,
                field_name="spawn_pitch",
                default=DEFAULT_SPAWN_PITCH,
            )

        self.touch(updated_by_user_id=updated_by_user_id)

    def set_source_context(
        self,
        *,
        source_service: Optional[str],
        external_ref: Optional[str],
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set source service/external reference metadata."""
        self.ensure_not_deleted()
        self.source_service = normalize_optional_text(
            source_service,
            field_name="source_service",
            max_length=WORLD_SOURCE_SERVICE_MAX_LENGTH,
        )
        self.external_ref = normalize_optional_text(
            external_ref,
            field_name="external_ref",
            max_length=WORLD_EXTERNAL_REF_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def ensure_bootstrap_defaults(
        self,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Ensure required bootstrap/default-world fields are populated.

        This is intentionally light-touch and does not change world_id.
        """
        if not self.template_id:
            self.template_id = DEFAULT_TEMPLATE_ID
        if not self.provider_id:
            self.provider_id = DEFAULT_PROVIDER_ID
        if not self.provider_world_id:
            self.provider_world_id = DEFAULT_PROVIDER_WORLD_ID
        if not self.generator_type:
            self.generator_type = DEFAULT_GENERATOR_TYPE
        if not self.generator_version:
            self.generator_version = DEFAULT_GENERATOR_VERSION
        if not self.projection_type:
            self.projection_type = DEFAULT_PROJECTION_TYPE
        if not self.topology_type:
            self.topology_type = DEFAULT_TOPOLOGY_TYPE
        if not self.coordinate_system:
            self.coordinate_system = DEFAULT_COORDINATE_SYSTEM
        if not self.block_registry_id:
            self.block_registry_id = DEFAULT_BLOCK_REGISTRY_ID
        if not self.block_registry_version:
            self.block_registry_version = DEFAULT_BLOCK_REGISTRY_VERSION
        if self.chunk_size is None or int(self.chunk_size) <= 0:
            self.chunk_size = DEFAULT_CHUNK_SIZE
        if self.cell_size is None or float(self.cell_size) <= 0:
            self.cell_size = DEFAULT_CELL_SIZE
        if self.spawn_y is None:
            self.spawn_y = DEFAULT_SPAWN_Y
        if self.spawn_yaw is None:
            self.spawn_yaw = DEFAULT_SPAWN_YAW
        if self.spawn_pitch is None:
            self.spawn_pitch = DEFAULT_SPAWN_PITCH

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
        """
        Merge metadata values and optionally remove keys.

        This method does not commit.
        """
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

    def merge_provisioning_metadata(
        self,
        *,
        chunk_project_id: Optional[str] = None,
        chunk_universe_id: Optional[str] = None,
        external_app_project_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Merge standard provisioning metadata."""
        self.update_metadata(
            {
                "schemaVersion": WORLD_INSTANCE_SCHEMA_VERSION,
                "chunkProjectId": chunk_project_id or self.project_public_id,
                "chunkUniverseId": chunk_universe_id or self.universe_public_id,
                "chunkWorldId": self.world_id,
                "externalAppProjectId": external_app_project_id,
                "templateId": self.template_id,
                "providerWorldId": self.provider_world_id,
                "blockRegistryId": self.block_registry_id,
                "blockRegistryVersion": self.block_registry_version,
                "spawn": self.spawn_context,
                "provisionedAt": datetime_to_iso(utc_now()),
            },
            updated_by_user_id=updated_by_user_id,
        )

    def get_metadata_value(self, key: str, default: Any = None) -> Any:
        """Read one metadata value safely."""
        try:
            metadata = normalize_metadata(self.metadata_json)
            return metadata.get(key, default)
        except Exception:
            return default

    def apply_patch_payload(
        self,
        payload: Mapping[str, Any],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Apply a PATCH-style API payload.

        Supported mutable keys:
        - name
        - slug
        - description
        - worldRole / world_role
        - templateId / template_id
        - providerId / provider_id
        - providerWorldId / provider_world_id
        - generatorType / generator_type
        - generatorVersion / generator_version
        - projectionType / projection_type
        - topologyType / topology_type
        - coordinateSystem / coordinate_system
        - chunkSize / chunk_size
        - cellSize / cell_size
        - surfaceY / surface_y
        - minY / min_y
        - maxY / max_y
        - seed
        - blockRegistryId / block_registry_id
        - blockRegistryVersion / block_registry_version
        - sourceService / source_service
        - externalRef / external_ref
        - spawn / spawnX/spawnY/spawnZ/spawnYaw/spawnPitch
        - metadata / metadataJson / metadata_json
        - metadataMerge
        - metadataRemoveKeys
        - status
        """
        if not isinstance(payload, Mapping):
            raise ValueError("World patch payload must be a JSON object.")

        self.ensure_not_deleted()

        changed = False

        if "name" in payload:
            self.name = normalize_required_text(
                payload.get("name"),
                field_name="name",
                max_length=WORLD_NAME_MAX_LENGTH,
            )
            changed = True

        if "slug" in payload:
            self.slug = normalize_slug(payload.get("slug"))
            changed = True

        if "description" in payload:
            self.description = normalize_optional_text(
                payload.get("description"),
                field_name="description",
                max_length=WORLD_DESCRIPTION_MAX_LENGTH,
            )
            changed = True

        if "worldRole" in payload or "world_role" in payload:
            self.world_role = normalize_world_role(
                payload.get("worldRole")
                if "worldRole" in payload
                else payload.get("world_role")
            )
            changed = True

        provider_fields = {
            "templateId",
            "template_id",
            "providerId",
            "provider_id",
            "providerWorldId",
            "provider_world_id",
            "generatorType",
            "generator_type",
            "generatorVersion",
            "generator_version",
        }

        if any(key in payload for key in provider_fields):
            self.template_id = normalize_template_id(
                payload.get("templateId")
                if "templateId" in payload
                else payload.get("template_id", self.template_id)
            )
            self.provider_id = normalize_provider_id(
                payload.get("providerId")
                if "providerId" in payload
                else payload.get("provider_id", self.provider_id)
            )
            self.provider_world_id = normalize_provider_world_id(
                payload.get("providerWorldId")
                if "providerWorldId" in payload
                else payload.get("provider_world_id", self.provider_world_id)
            )
            self.generator_type = normalize_required_text(
                payload.get("generatorType")
                if "generatorType" in payload
                else payload.get("generator_type", self.generator_type),
                field_name="generator_type",
                max_length=WORLD_GENERATOR_TYPE_MAX_LENGTH,
            )
            self.generator_version = normalize_version_text(
                payload.get("generatorVersion")
                if "generatorVersion" in payload
                else payload.get("generator_version", self.generator_version),
                field_name="generator_version",
                max_length=WORLD_GENERATOR_VERSION_MAX_LENGTH,
                default=DEFAULT_GENERATOR_VERSION,
            )
            changed = True

        geometry_fields = {
            "projectionType",
            "projection_type",
            "topologyType",
            "topology_type",
            "coordinateSystem",
            "coordinate_system",
        }

        if any(key in payload for key in geometry_fields):
            self.projection_type = normalize_required_text(
                payload.get("projectionType")
                if "projectionType" in payload
                else payload.get("projection_type", self.projection_type),
                field_name="projection_type",
                max_length=WORLD_PROJECTION_TYPE_MAX_LENGTH,
            )
            self.topology_type = normalize_required_text(
                payload.get("topologyType")
                if "topologyType" in payload
                else payload.get("topology_type", self.topology_type),
                field_name="topology_type",
                max_length=WORLD_TOPOLOGY_TYPE_MAX_LENGTH,
            )
            self.coordinate_system = normalize_required_text(
                payload.get("coordinateSystem")
                if "coordinateSystem" in payload
                else payload.get("coordinate_system", self.coordinate_system),
                field_name="coordinate_system",
                max_length=WORLD_COORDINATE_SYSTEM_MAX_LENGTH,
            )
            changed = True

        if "chunkSize" in payload or "chunk_size" in payload:
            self.chunk_size = normalize_positive_int(
                payload.get("chunkSize")
                if "chunkSize" in payload
                else payload.get("chunk_size"),
                field_name="chunk_size",
                default=DEFAULT_CHUNK_SIZE,
            )
            changed = True

        if "cellSize" in payload or "cell_size" in payload:
            self.cell_size = normalize_positive_float(
                payload.get("cellSize")
                if "cellSize" in payload
                else payload.get("cell_size"),
                field_name="cell_size",
                default=DEFAULT_CELL_SIZE,
            )
            changed = True

        bounds_changed = False
        new_min_y = self.min_y
        new_surface_y = self.surface_y
        new_max_y = self.max_y

        if "minY" in payload or "min_y" in payload:
            new_min_y = normalize_int(
                payload.get("minY") if "minY" in payload else payload.get("min_y"),
                field_name="min_y",
                default=DEFAULT_MIN_Y,
            )
            bounds_changed = True

        if "surfaceY" in payload or "surface_y" in payload:
            new_surface_y = normalize_int(
                payload.get("surfaceY")
                if "surfaceY" in payload
                else payload.get("surface_y"),
                field_name="surface_y",
                default=DEFAULT_SURFACE_Y,
            )
            bounds_changed = True

        if "maxY" in payload or "max_y" in payload:
            new_max_y = normalize_int(
                payload.get("maxY") if "maxY" in payload else payload.get("max_y"),
                field_name="max_y",
                default=DEFAULT_MAX_Y,
            )
            bounds_changed = True

        if bounds_changed:
            validate_vertical_bounds(
                min_y=new_min_y,
                surface_y=new_surface_y,
                max_y=new_max_y,
            )
            self.min_y = new_min_y
            self.surface_y = new_surface_y
            self.max_y = new_max_y
            changed = True

        if "seed" in payload:
            self.seed = normalize_optional_text(
                payload.get("seed"),
                field_name="seed",
                max_length=WORLD_SEED_MAX_LENGTH,
            )
            changed = True

        if "blockRegistryId" in payload or "block_registry_id" in payload:
            self.block_registry_id = normalize_required_text(
                payload.get("blockRegistryId")
                if "blockRegistryId" in payload
                else payload.get("block_registry_id"),
                field_name="block_registry_id",
                max_length=WORLD_BLOCK_REGISTRY_ID_MAX_LENGTH,
            )
            changed = True

        if "blockRegistryVersion" in payload or "block_registry_version" in payload:
            self.block_registry_version = normalize_version_text(
                payload.get("blockRegistryVersion")
                if "blockRegistryVersion" in payload
                else payload.get("block_registry_version"),
                field_name="block_registry_version",
                max_length=WORLD_BLOCK_REGISTRY_VERSION_MAX_LENGTH,
                default=DEFAULT_BLOCK_REGISTRY_VERSION,
            )
            changed = True

        if "sourceService" in payload or "source_service" in payload:
            self.source_service = normalize_optional_text(
                payload.get("sourceService")
                if "sourceService" in payload
                else payload.get("source_service"),
                field_name="source_service",
                max_length=WORLD_SOURCE_SERVICE_MAX_LENGTH,
            )
            changed = True

        if "externalRef" in payload or "external_ref" in payload:
            self.external_ref = normalize_optional_text(
                payload.get("externalRef")
                if "externalRef" in payload
                else payload.get("external_ref"),
                field_name="external_ref",
                max_length=WORLD_EXTERNAL_REF_MAX_LENGTH,
            )
            changed = True

        spawn = payload.get("spawn") if isinstance(payload.get("spawn"), Mapping) else None
        spawn_changed = spawn is not None

        if spawn_changed or "spawnX" in payload or "spawn_x" in payload:
            self.spawn_x = normalize_int(
                payload.get("spawnX")
                if "spawnX" in payload
                else payload.get("spawn_x", spawn.get("x", self.spawn_x) if spawn else self.spawn_x),
                field_name="spawn_x",
                default=DEFAULT_SPAWN_X,
            )
            changed = True

        if spawn_changed or "spawnY" in payload or "spawn_y" in payload:
            self.spawn_y = normalize_int(
                payload.get("spawnY")
                if "spawnY" in payload
                else payload.get("spawn_y", spawn.get("y", self.spawn_y) if spawn else self.spawn_y),
                field_name="spawn_y",
                default=DEFAULT_SPAWN_Y,
            )
            changed = True

        if spawn_changed or "spawnZ" in payload or "spawn_z" in payload:
            self.spawn_z = normalize_int(
                payload.get("spawnZ")
                if "spawnZ" in payload
                else payload.get("spawn_z", spawn.get("z", self.spawn_z) if spawn else self.spawn_z),
                field_name="spawn_z",
                default=DEFAULT_SPAWN_Z,
            )
            changed = True

        if spawn_changed or "spawnYaw" in payload or "spawn_yaw" in payload:
            self.spawn_yaw = normalize_float(
                payload.get("spawnYaw")
                if "spawnYaw" in payload
                else payload.get("spawn_yaw", spawn.get("yaw", self.spawn_yaw) if spawn else self.spawn_yaw),
                field_name="spawn_yaw",
                default=DEFAULT_SPAWN_YAW,
            )
            changed = True

        if spawn_changed or "spawnPitch" in payload or "spawn_pitch" in payload:
            self.spawn_pitch = normalize_float(
                payload.get("spawnPitch")
                if "spawnPitch" in payload
                else payload.get("spawn_pitch", spawn.get("pitch", self.spawn_pitch) if spawn else self.spawn_pitch),
                field_name="spawn_pitch",
                default=DEFAULT_SPAWN_PITCH,
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
        """
        Return validation errors without raising.

        Useful for debug/status endpoints and repository preflight checks.
        """
        errors: Dict[str, str] = {}

        try:
            normalize_db_id(self.project_db_id, field_name="project_db_id")
        except Exception as exc:
            errors["projectDbId"] = str(exc)

        try:
            normalize_db_id(self.universe_db_id, field_name="universe_db_id")
        except Exception as exc:
            errors["universeDbId"] = str(exc)

        try:
            normalize_world_id(self.world_id)
        except Exception as exc:
            errors["worldId"] = str(exc)

        if is_provider_like_world_id(self.world_id):
            errors["worldId"] = (
                "world_id must be a concrete editable world id. "
                "`flat` belongs in template_id/provider_world_id."
            )

        try:
            normalize_required_text(
                self.name,
                field_name="name",
                max_length=WORLD_NAME_MAX_LENGTH,
            )
        except Exception as exc:
            errors["name"] = str(exc)

        try:
            normalize_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_world_type(self.world_type)
        except Exception as exc:
            errors["worldType"] = str(exc)

        try:
            normalize_world_role(self.world_role)
        except Exception as exc:
            errors["worldRole"] = str(exc)

        try:
            normalize_world_scope(self.world_scope)
        except Exception as exc:
            errors["worldScope"] = str(exc)

        try:
            normalize_template_id(self.template_id)
        except Exception as exc:
            errors["templateId"] = str(exc)

        try:
            normalize_provider_id(self.provider_id)
        except Exception as exc:
            errors["providerId"] = str(exc)

        try:
            normalize_provider_world_id(self.provider_world_id)
        except Exception as exc:
            errors["providerWorldId"] = str(exc)

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
            validate_vertical_bounds(
                min_y=int(self.min_y),
                surface_y=int(self.surface_y),
                max_y=int(self.max_y),
            )
        except Exception as exc:
            errors["verticalBounds"] = str(exc)

        try:
            normalize_float(
                self.spawn_yaw,
                field_name="spawn_yaw",
                default=DEFAULT_SPAWN_YAW,
            )
        except Exception as exc:
            errors["spawnYaw"] = str(exc)

        try:
            normalize_float(
                self.spawn_pitch,
                field_name="spawn_pitch",
                default=DEFAULT_SPAWN_PITCH,
            )
        except Exception as exc:
            errors["spawnPitch"] = str(exc)

        try:
            normalize_metadata(self.metadata_json)
        except Exception as exc:
            errors["metadataJson"] = str(exc)

        if self.slug is not None:
            try:
                normalize_slug(self.slug)
            except Exception as exc:
                errors["slug"] = str(exc)

        if self.source_service is not None:
            try:
                normalize_optional_text(
                    self.source_service,
                    field_name="source_service",
                    max_length=WORLD_SOURCE_SERVICE_MAX_LENGTH,
                )
            except Exception as exc:
                errors["sourceService"] = str(exc)

        if self.external_ref is not None:
            try:
                normalize_optional_text(
                    self.external_ref,
                    field_name="external_ref",
                    max_length=WORLD_EXTERNAL_REF_MAX_LENGTH,
                )
            except Exception as exc:
                errors["externalRef"] = str(exc)

        if self.revision is None or int(self.revision) < 1:
            errors["revision"] = "revision must be greater than or equal to 1."

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Serialize the world instance for API/service responses.

        Internal database IDs are excluded by default.
        """
        resolved_project_id = project_id if project_id is not None else self.project_public_id
        resolved_universe_id = universe_id if universe_id is not None else self.universe_public_id

        result: Dict[str, Any] = {
            "projectId": resolved_project_id,
            "chunkProjectId": resolved_project_id,
            "universeId": resolved_universe_id,
            "chunkUniverseId": resolved_universe_id,
            "worldId": self.world_id,
            "chunkWorldId": self.world_id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "worldType": self.world_type,
            "worldRole": self.world_role,
            "worldScope": self.world_scope,
            "templateId": self.template_id,
            "providerId": self.provider_id,
            "providerWorldId": self.provider_world_id,
            "generatorType": self.generator_type,
            "generatorVersion": self.generator_version,
            "projectionType": self.projection_type,
            "topologyType": self.topology_type,
            "coordinateSystem": self.coordinate_system,
            "chunkSize": self.chunk_size,
            "cellSize": self.cell_size,
            "surfaceY": self.surface_y,
            "minY": self.min_y,
            "maxY": self.max_y,
            "seed": self.seed,
            "blockRegistryId": self.block_registry_id,
            "blockRegistryVersion": self.block_registry_version,
            "spawn": self.spawn_context,
            "sourceService": self.source_service,
            "externalRef": self.external_ref,
            "createdByUserId": self.created_by_user_id,
            "updatedByUserId": self.updated_by_user_id,
            "createdAt": datetime_to_iso(self.created_at),
            "updatedAt": datetime_to_iso(self.updated_at),
            "archivedAt": datetime_to_iso(self.archived_at),
            "deletedAt": datetime_to_iso(self.deleted_at),
            "chunkConfig": self.chunk_config,
            "providerMapping": self.provider_mapping,
            "registryContext": self.registry_context,
            "routeHints": self.build_route_hints(),
            "flags": {
                "active": self.is_active,
                "archived": self.is_archived,
                "deleted": self.is_deleted,
                "runtimeWorld": self.world_type == WORLD_TYPE_RUNTIME,
                "defaultSpawn": self.world_role == WORLD_ROLE_DEFAULT_SPAWN,
                "providerLikeWorldId": is_provider_like_world_id(self.world_id),
            },
        }

        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)

        if include_internal:
            result["id"] = self.id
            result["projectDbId"] = self.project_db_id
            result["universeDbId"] = self.universe_db_id
            result["worldContextKey"] = self.build_world_context_key()

        return result

    def to_public_dict(
        self,
        *,
        project_id: Optional[str] = None,
        universe_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serialize without internal database identifiers."""
        return self.to_dict(
            include_internal=False,
            include_metadata=True,
            project_id=project_id,
            universe_id=universe_id,
        )


__all__ = [
    "DEFAULT_BLOCK_REGISTRY_ID",
    "DEFAULT_BLOCK_REGISTRY_VERSION",
    "DEFAULT_CELL_SIZE",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_COORDINATE_SYSTEM",
    "DEFAULT_GENERATOR_TYPE",
    "DEFAULT_GENERATOR_VERSION",
    "DEFAULT_MAX_Y",
    "DEFAULT_MIN_Y",
    "DEFAULT_PROJECTION_TYPE",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_PROVIDER_WORLD_ID",
    "DEFAULT_SPAWN_PITCH",
    "DEFAULT_SPAWN_X",
    "DEFAULT_SPAWN_Y",
    "DEFAULT_SPAWN_YAW",
    "DEFAULT_SPAWN_Z",
    "DEFAULT_SURFACE_Y",
    "DEFAULT_TEMPLATE_ID",
    "DEFAULT_TOPOLOGY_TYPE",
    "DEFAULT_WORLD_ID",
    "DEFAULT_WORLD_NAME",
    "DEFAULT_WORLD_SLUG",
    "JSON_COLUMN_TYPE",
    "VALID_WORLD_ROLES",
    "VALID_WORLD_SCOPES",
    "VALID_WORLD_STATUSES",
    "VALID_WORLD_TYPES",
    "WORLD_INSTANCE_SCHEMA_VERSION",
    "WORLD_ROLE_DEFAULT_SPAWN",
    "WORLD_SCOPE_PROJECT",
    "WORLD_STATUS_ACTIVE",
    "WORLD_STATUS_ARCHIVED",
    "WORLD_STATUS_DELETED",
    "WORLD_TYPE_RUNTIME",
    "WorldInstance",
    "datetime_to_iso",
    "generate_world_id",
    "is_provider_like_world_id",
    "make_json_safe",
    "normalize_concrete_world_id",
    "normalize_world_id",
    "utc_now",
]