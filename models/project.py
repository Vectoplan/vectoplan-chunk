# services/vectoplan-chunk/models/project.py
"""
SQLAlchemy model for VECTOPLAN Chunk projects.

A Project is the top-level persistent container for an editable VECTOPLAN
runtime universe inside `vectoplan-chunk`.

Important service boundary:

    vectoplan-app owns App projects.
    vectoplan-chunk owns Chunk projects.

A chunk Project may be linked to a vectoplan-app Project through:

    external_app_project_id

The chunk Project is still its own service-local entity. It stores the chunk
world references that belong to the chunk service, not foreign keys into the
app database.

Current intended hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Design rules:
- `id` is the internal database primary key.
- `project_id` is the stable chunk-service public/API identifier.
- `external_app_project_id` stores the vectoplan-app project public id.
- `owner_type='user'` and `owner_id` store the external owner user id.
- owner ids are service references and never foreign keys into auth/app databases.
- new projects temporarily default to owner user id `1` until the caller supplies
  the canonical auth user id. No local user row is created for that value.
- `slug` is optional but globally unique when present.
- deletion is soft-delete by default.
- this model does not perform queries, flushes, commits or rollbacks.
- repository/service/route layers own database transactions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Final, Optional, Tuple
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
        "models/project.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
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


PROJECT_SCHEMA_VERSION = "project.schema.v2"

# Temporary persistence default only. This value is not a local user record and
# not an authentication fallback. App/auth integration can override it per
# project as soon as canonical external user ids are available.
DEFAULT_PROJECT_OWNER_USER_ID: Final[str] = "1"
PROJECT_OWNER_TYPE_USER: Final[str] = "user"
VALID_PROJECT_OWNER_TYPES: Final[frozenset[str]] = frozenset(
    {PROJECT_OWNER_TYPE_USER}
)

PROJECT_STATUS_ACTIVE = "active"
PROJECT_STATUS_ARCHIVED = "archived"
PROJECT_STATUS_DELETED = "deleted"

VALID_PROJECT_STATUSES = frozenset(
    {
        PROJECT_STATUS_ACTIVE,
        PROJECT_STATUS_ARCHIVED,
        PROJECT_STATUS_DELETED,
    }
)

PROJECT_ID_MAX_LENGTH = 96
PROJECT_SLUG_MAX_LENGTH = 120
PROJECT_NAME_MAX_LENGTH = 255
PROJECT_OWNER_TYPE_MAX_LENGTH = 64
PROJECT_OWNER_ID_MAX_LENGTH = 128
PROJECT_USER_ID_MAX_LENGTH = 128
PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH = 96
PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH = 96
PROJECT_DESCRIPTION_MAX_LENGTH = 4096

PROJECT_EXTERNAL_APP_PROJECT_ID_MAX_LENGTH = 128
PROJECT_SOURCE_SERVICE_MAX_LENGTH = 96
PROJECT_EXTERNAL_URL_MAX_LENGTH = 512

PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
OWNER_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]*$")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")



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

    Metadata can later contain values coming from the editor, importers,
    scripts, AI tooling or integration layers.
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
    field_name: str = "project_id",
    max_length: int = PROJECT_ID_MAX_LENGTH,
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


def normalize_project_id(value: Any) -> str:
    """Normalize a public chunk project id."""
    return normalize_public_id(
        value,
        field_name="project_id",
        max_length=PROJECT_ID_MAX_LENGTH,
    )


def normalize_external_app_project_id(value: Any) -> Optional[str]:
    """Normalize optional vectoplan-app project public id."""
    text = normalize_optional_text(
        value,
        field_name="external_app_project_id",
        max_length=PROJECT_EXTERNAL_APP_PROJECT_ID_MAX_LENGTH,
    )

    if text is None:
        return None

    if not PUBLIC_ID_PATTERN.match(text):
        raise ValueError(
            "external_app_project_id may only contain letters, numbers, "
            "underscores, dashes, dots and colons, and must start with a "
            "letter or number."
        )

    return text


def normalize_slug(value: Any) -> Optional[str]:
    """Normalize optional project slugs."""
    text = normalize_optional_text(
        value,
        field_name="slug",
        max_length=PROJECT_SLUG_MAX_LENGTH,
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
    """Normalize and validate project status."""
    if value is None:
        return PROJECT_STATUS_ACTIVE

    try:
        status = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("status must be text-like.") from exc

    if status not in VALID_PROJECT_STATUSES:
        allowed = ", ".join(sorted(VALID_PROJECT_STATUSES))
        raise ValueError(f"Invalid project status '{value}'. Allowed: {allowed}.")

    return status


@lru_cache(maxsize=128)
def _normalize_owner_type_cached(text: str) -> str:
    """Validate a canonical owner type without caching arbitrary objects."""
    normalized = text.strip().lower()
    if not normalized:
        raise ValueError("owner_type is required when owner_id is present.")
    if len(normalized) > PROJECT_OWNER_TYPE_MAX_LENGTH:
        raise ValueError(
            f"owner_type must not exceed {PROJECT_OWNER_TYPE_MAX_LENGTH} characters."
        )
    if not OWNER_TYPE_PATTERN.fullmatch(normalized):
        raise ValueError(
            "owner_type must start with a lowercase letter and may only contain "
            "lowercase letters, numbers, underscores, dashes, dots and colons."
        )
    if normalized not in VALID_PROJECT_OWNER_TYPES:
        allowed = ", ".join(sorted(VALID_PROJECT_OWNER_TYPES))
        raise ValueError(
            f"Invalid owner_type '{normalized}'. Allowed: {allowed}."
        )
    return normalized


def normalize_owner_type(value: Any) -> Optional[str]:
    """Normalize an optional project owner type."""
    if value is None:
        return None
    try:
        text = str(value)
    except Exception as exc:
        raise ValueError("owner_type must be text-like.") from exc
    if not text.strip():
        return None
    return _normalize_owner_type_cached(text)


@lru_cache(maxsize=2048)
def _normalize_external_user_id_cached(text: str, field_name: str) -> str:
    """Validate a canonical external user id without caching caller objects."""
    normalized = text.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required.")
    if len(normalized) > PROJECT_USER_ID_MAX_LENGTH:
        raise ValueError(
            f"{field_name} must not exceed {PROJECT_USER_ID_MAX_LENGTH} characters."
        )
    if CONTROL_CHARACTER_PATTERN.search(normalized):
        raise ValueError(f"{field_name} must not contain control characters.")
    return normalized


def normalize_external_user_id(
    value: Any,
    *,
    field_name: str,
    required: bool = False,
) -> Optional[str]:
    """
    Normalize an external user id.

    User ids are deliberately not restricted to UUIDs. Numeric placeholders,
    UUIDs and future auth public ids remain valid as long as they are non-empty,
    bounded strings without control characters.
    """
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None
    try:
        text = str(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be text-like.") from exc
    if not text.strip():
        if required:
            raise ValueError(f"{field_name} is required.")
        return None
    return _normalize_external_user_id_cached(text, field_name)


def normalize_owner_pair(
    *,
    owner_type: Any = None,
    owner_id: Any = None,
    owner_user_id: Any = None,
    required: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Normalize the atomic owner pair.

    `ownerUserId` is the preferred external contract. The generic owner_type /
    owner_id pair remains available for stored-model compatibility, but the
    current schema intentionally accepts only user ownership.
    """
    normalized_owner_user_id = normalize_external_user_id(
        owner_user_id,
        field_name="owner_user_id",
        required=False,
    )
    normalized_owner_id = normalize_external_user_id(
        owner_id,
        field_name="owner_id",
        required=False,
    )
    normalized_owner_type = normalize_owner_type(owner_type)

    if normalized_owner_user_id is not None:
        if (
            normalized_owner_id is not None
            and normalized_owner_id != normalized_owner_user_id
        ):
            raise ValueError(
                "owner_user_id and owner_id refer to different external users."
            )
        if (
            normalized_owner_type is not None
            and normalized_owner_type != PROJECT_OWNER_TYPE_USER
        ):
            raise ValueError(
                "owner_user_id requires owner_type='user'."
            )
        normalized_owner_type = PROJECT_OWNER_TYPE_USER
        normalized_owner_id = normalized_owner_user_id

    if normalized_owner_id is not None and normalized_owner_type is None:
        normalized_owner_type = PROJECT_OWNER_TYPE_USER

    if (normalized_owner_type is None) != (normalized_owner_id is None):
        raise ValueError(
            "owner_type and owner_id must either both be set or both be empty."
        )

    if required and normalized_owner_id is None:
        raise ValueError("owner_user_id is required for a project.")

    return normalized_owner_type, normalized_owner_id


def get_project_normalization_cache_info() -> Dict[str, Any]:
    """Return diagnostics for pure normalization caches only."""
    return {
        "ownerType": _normalize_owner_type_cached.cache_info()._asdict(),
        "externalUserId": _normalize_external_user_id_cached.cache_info()._asdict(),
    }


def reset_project_normalization_caches() -> Dict[str, Any]:
    """Clear pure normalization caches; no ORM/database state is cached here."""
    before = get_project_normalization_cache_info()
    _normalize_owner_type_cached.cache_clear()
    _normalize_external_user_id_cached.cache_clear()
    return {
        "cleared": True,
        "before": before,
        "after": get_project_normalization_cache_info(),
    }


def _payload_first(
    payload: Mapping[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:
    """Return the first explicitly present payload value, preserving falsey values."""
    for key in keys:
        if key in payload:
            return payload.get(key)
    return default


def _payload_present_key(payload: Mapping[str, Any], *keys: str) -> Optional[str]:
    """Return the first present key without reading through truthiness."""
    for key in keys:
        if key in payload:
            return key
    return None


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


def generate_project_id(prefix: str = "proj") -> str:
    """
    Generate a stable public chunk project identifier.

    Example:
        proj_2f2f7a1c9d3b4a44a5d9e41910b51e70
    """
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="project_id_prefix",
        max_length=24,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def _coalesce_first_text(*values: Any) -> Optional[str]:
    """Return first normalized non-empty text value."""
    for value in values:
        text = normalize_optional_text(
            value,
            field_name="value",
            max_length=4096,
        )
        if text is not None:
            return text

    return None


def _payload_metadata_value(payload: Mapping[str, Any]) -> Any:
    """Read metadata payload from several compatible keys."""
    if "metadataJson" in payload:
        return payload.get("metadataJson")
    if "metadata_json" in payload:
        return payload.get("metadata_json")
    if "metadata" in payload:
        return payload.get("metadata")
    if "projectMetadata" in payload:
        return payload.get("projectMetadata")
    if "project_metadata" in payload:
        return payload.get("project_metadata")
    return None


def _merge_metadata(
    base: Optional[Mapping[str, Any]],
    update: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Merge two metadata mappings safely."""
    result = normalize_metadata(base)

    if update is None:
        return result

    if not isinstance(update, Mapping):
        raise ValueError("metadata update must be a JSON object/dict.")

    for key, value in update.items():
        result[str(key)] = make_json_safe(value)

    return result


class Project(db.Model):
    """
    Persistent Chunk project.

    A chunk Project is the editable top-level container used by the editor.

    In the app-integrated flow:

        vectoplan-app Project.public_id
            -> Project.external_app_project_id

        Project.project_id
            -> chunk-service public/API project id

    This model intentionally does not know how to create universes or worlds.
    That belongs in repositories/services/routes so transactions can create:

        Project + Universe + WorldInstance

    atomically.
    """

    __tablename__ = "projects"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    project_id = db.Column(
        db.String(PROJECT_ID_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
    )

    slug = db.Column(
        db.String(PROJECT_SLUG_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    name = db.Column(
        db.String(PROJECT_NAME_MAX_LENGTH),
        nullable=False,
    )

    description = db.Column(
        db.String(PROJECT_DESCRIPTION_MAX_LENGTH),
        nullable=True,
    )

    status = db.Column(
        db.String(32),
        nullable=False,
        default=PROJECT_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(64),
        nullable=False,
        default=PROJECT_SCHEMA_VERSION,
    )

    revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    # Default universe/world references are public chunk-service IDs.
    default_universe_id = db.Column(
        db.String(PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    default_world_id = db.Column(
        db.String(PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    spawn_world_id = db.Column(
        db.String(PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    # External link to vectoplan-app. This is not a DB FK.
    external_app_project_id = db.Column(
        db.String(PROJECT_EXTERNAL_APP_PROJECT_ID_MAX_LENGTH),
        nullable=True,
        unique=True,
        index=True,
    )

    source_service = db.Column(
        db.String(PROJECT_SOURCE_SERVICE_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    external_url = db.Column(
        db.String(PROJECT_EXTERNAL_URL_MAX_LENGTH),
        nullable=True,
    )

    owner_type = db.Column(
        db.String(PROJECT_OWNER_TYPE_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    owner_id = db.Column(
        db.String(PROJECT_OWNER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_by_user_id = db.Column(
        db.String(PROJECT_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_by_user_id = db.Column(
        db.String(PROJECT_USER_ID_MAX_LENGTH),
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
            "slug",
            name="uq_projects_slug",
        ),
        db.UniqueConstraint(
            "external_app_project_id",
            name="uq_projects_external_app_project_id",
        ),
        db.CheckConstraint(
            "project_id <> ''",
            name="ck_projects_project_id_not_empty",
        ),
        db.CheckConstraint(
            "name <> ''",
            name="ck_projects_name_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_projects_status_valid",
        ),
        db.CheckConstraint(
            "revision >= 1",
            name="ck_projects_revision_positive",
        ),
        db.CheckConstraint(
            "owner_type IS NULL OR owner_type = 'user'",
            name="ck_projects_owner_type_valid",
        ),
        db.CheckConstraint(
            "(owner_type IS NULL AND owner_id IS NULL) OR "
            "(owner_type IS NOT NULL AND owner_id IS NOT NULL)",
            name="ck_projects_owner_pair_complete",
        ),
        db.Index(
            "ix_projects_status_created_at",
            "status",
            "created_at",
        ),
        db.Index(
            "ix_projects_owner_lookup",
            "owner_type",
            "owner_id",
        ),
        db.Index(
            "ix_projects_default_universe",
            "project_id",
            "default_universe_id",
        ),
        db.Index(
            "ix_projects_default_world",
            "project_id",
            "default_world_id",
        ),
        db.Index(
            "ix_projects_spawn_world",
            "project_id",
            "spawn_world_id",
        ),
        db.Index(
            "ix_projects_active_lookup",
            "project_id",
            "status",
            "deleted_at",
        ),
        db.Index(
            "ix_projects_app_link_lookup",
            "external_app_project_id",
            "status",
            "deleted_at",
        ),
        db.Index(
            "ix_projects_source_service_lookup",
            "source_service",
            "external_app_project_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Project id={self.id!r} project_id={self.project_id!r} "
            f"external_app_project_id={self.external_app_project_id!r} "
            f"owner_user_id={self.owner_user_id!r} status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        project_id: Optional[str] = None,
        name: Optional[str] = None,
        slug: Optional[str] = None,
        description: Optional[str] = None,
        status: str = PROJECT_STATUS_ACTIVE,
        default_universe_id: Optional[str] = None,
        default_world_id: Optional[str] = None,
        spawn_world_id: Optional[str] = None,
        external_app_project_id: Optional[str] = None,
        source_service: Optional[str] = None,
        external_url: Optional[str] = None,
        owner_type: Optional[str] = None,
        owner_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "Project":
        """
        Create a Project instance without adding it to a session.

        Until the app/auth owner id is supplied, new projects use the external
        placeholder user id ``"1"``. This stores only an opaque reference and
        does not create or authenticate a user.

        Repository/service code is responsible for uniqueness checks, adding
        the instance to a session and owning the transaction boundary.
        """
        public_project_id = normalize_project_id(project_id or generate_project_id())
        normalized_name = normalize_required_text(
            name or public_project_id,
            field_name="name",
            max_length=PROJECT_NAME_MAX_LENGTH,
        )
        normalized_status = normalize_status(status)
        normalized_external_app_project_id = normalize_external_app_project_id(
            external_app_project_id
        )
        normalized_source_service = normalize_optional_text(
            source_service,
            field_name="source_service",
            max_length=PROJECT_SOURCE_SERVICE_MAX_LENGTH,
        )
        if (
            normalized_external_app_project_id is not None
            and normalized_source_service is None
        ):
            normalized_source_service = "vectoplan-app"

        if owner_type is None and owner_id is None and owner_user_id is None:
            owner_user_id = DEFAULT_PROJECT_OWNER_USER_ID
        normalized_owner_type, normalized_owner_id = normalize_owner_pair(
            owner_type=owner_type,
            owner_id=owner_id,
            owner_user_id=owner_user_id,
            required=True,
        )
        normalized_created_by = normalize_external_user_id(
            created_by_user_id,
            field_name="created_by_user_id",
            required=False,
        )
        if normalized_created_by is None:
            normalized_created_by = normalized_owner_id
        normalized_updated_by = normalize_external_user_id(
            updated_by_user_id,
            field_name="updated_by_user_id",
            required=False,
        )
        if normalized_updated_by is None:
            normalized_updated_by = normalized_created_by

        now = utc_now()
        return cls(
            project_id=public_project_id,
            slug=normalize_slug(slug),
            name=normalized_name,
            description=normalize_optional_text(
                description,
                field_name="description",
                max_length=PROJECT_DESCRIPTION_MAX_LENGTH,
            ),
            status=normalized_status,
            schema_version=PROJECT_SCHEMA_VERSION,
            revision=1,
            default_universe_id=normalize_optional_text(
                default_universe_id,
                field_name="default_universe_id",
                max_length=PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH,
            ),
            default_world_id=normalize_optional_text(
                default_world_id,
                field_name="default_world_id",
                max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
            ),
            spawn_world_id=normalize_optional_text(
                spawn_world_id,
                field_name="spawn_world_id",
                max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
            ),
            external_app_project_id=normalized_external_app_project_id,
            source_service=normalized_source_service,
            external_url=normalize_optional_text(
                external_url,
                field_name="external_url",
                max_length=PROJECT_EXTERNAL_URL_MAX_LENGTH,
            ),
            owner_type=normalized_owner_type,
            owner_id=normalized_owner_id,
            created_by_user_id=normalized_created_by,
            updated_by_user_id=normalized_updated_by,
            metadata_json=normalize_metadata(metadata_json),
            created_at=now,
            updated_at=now,
            archived_at=now if normalized_status == PROJECT_STATUS_ARCHIVED else None,
            deleted_at=now if normalized_status == PROJECT_STATUS_DELETED else None,
        )

    @classmethod
    def create_dev_project(
        cls,
        *,
        project_id: str = "dev-project",
        default_universe_id: str = "dev-universe",
        default_world_id: str = "world_spawn",
        owner_user_id: str = DEFAULT_PROJECT_OWNER_USER_ID,
        created_by_user_id: Optional[str] = None,
    ) -> "Project":
        """Create the default development project for idempotent DB bootstrap."""
        return cls.create(
            project_id=project_id,
            slug=project_id,
            name="Dev Project",
            description="Default development project for the chunk-service world slice.",
            default_universe_id=default_universe_id,
            default_world_id=default_world_id,
            spawn_world_id=default_world_id,
            source_service="vectoplan-chunk",
            owner_user_id=owner_user_id,
            created_by_user_id=created_by_user_id or owner_user_id,
            metadata_json={
                "seed": True,
                "seedType": "development",
                "createdBy": "vectoplan-chunk",
                "ownerUserId": str(owner_user_id),
            },
        )

    @classmethod
    def create_for_app_project(
        cls,
        *,
        app_project_public_id: str,
        chunk_project_id: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        default_universe_id: Optional[str] = None,
        default_world_id: Optional[str] = None,
        spawn_world_id: Optional[str] = None,
        source_service: str = "vectoplan-app",
        external_url: Optional[str] = None,
        owner_user_id: str = DEFAULT_PROJECT_OWNER_USER_ID,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "Project":
        """
        Create a chunk Project linked to a vectoplan-app project.

        The App Project id and owner user id are opaque external references;
        neither creates a database foreign key across service boundaries.
        Universe/WorldInstance creation remains in the provisioning transaction.
        """
        normalized_app_project_id = normalize_external_app_project_id(
            app_project_public_id
        )
        if normalized_app_project_id is None:
            raise ValueError("app_project_public_id is required.")

        normalized_owner_user_id = normalize_external_user_id(
            owner_user_id,
            field_name="owner_user_id",
            required=True,
        )
        if chunk_project_id is None:
            chunk_project_id = f"chk_prj_{normalized_app_project_id}_{uuid4().hex[:12]}"

        metadata = _merge_metadata(
            {
                "sourceService": source_service,
                "externalAppProjectId": normalized_app_project_id,
                "ownerUserId": normalized_owner_user_id,
                "createdBy": "vectoplan-chunk.project-provisioning",
            },
            metadata_json,
        )
        return cls.create(
            project_id=chunk_project_id,
            slug=chunk_project_id,
            name=name or f"Chunk Project for {normalized_app_project_id}",
            description=description,
            default_universe_id=default_universe_id,
            default_world_id=default_world_id,
            spawn_world_id=spawn_world_id or default_world_id,
            external_app_project_id=normalized_app_project_id,
            source_service=source_service,
            external_url=external_url,
            owner_user_id=normalized_owner_user_id,
            created_by_user_id=created_by_user_id or normalized_owner_user_id,
            metadata_json=metadata,
        )

    @classmethod
    def from_create_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        created_by_user_id: Optional[str] = None,
    ) -> "Project":
        """Create a Project from compatible camelCase/snake_case API keys."""
        if not isinstance(payload, Mapping):
            raise ValueError("Project create payload must be a JSON object.")

        metadata_value = _payload_metadata_value(payload)
        app_project_public_id = _payload_first(
            payload,
            "externalAppProjectId",
            "external_app_project_id",
            "appProjectPublicId",
            "app_project_public_id",
            "appProjectId",
            "app_project_id",
        )
        project_id = _payload_first(
            payload,
            "chunkProjectId",
            "chunk_project_id",
            "projectId",
            "project_id",
        )
        owner_user_id = _payload_first(
            payload,
            "ownerUserId",
            "owner_user_id",
            default=None,
        )

        # Actor fields are trusted method arguments, not user-controlled body
        # fields. If omitted, create() safely falls back to the owner user id.
        trusted_created_by = created_by_user_id

        return cls.create(
            project_id=project_id,
            name=_payload_first(payload, "name", "projectName", "project_name"),
            slug=_payload_first(payload, "slug"),
            description=_payload_first(payload, "description"),
            status=_payload_first(payload, "status", default=PROJECT_STATUS_ACTIVE),
            default_universe_id=_payload_first(
                payload,
                "defaultUniverseId",
                "default_universe_id",
                "universeId",
                "universe_id",
            ),
            default_world_id=_payload_first(
                payload,
                "defaultWorldId",
                "default_world_id",
                "worldId",
                "world_id",
            ),
            spawn_world_id=_payload_first(
                payload,
                "spawnWorldId",
                "spawn_world_id",
                "worldId",
                "world_id",
            ),
            external_app_project_id=app_project_public_id,
            source_service=_payload_first(payload, "sourceService", "source_service"),
            external_url=_payload_first(payload, "externalUrl", "external_url"),
            owner_type=_payload_first(payload, "ownerType", "owner_type"),
            owner_id=_payload_first(payload, "ownerId", "owner_id"),
            owner_user_id=owner_user_id,
            created_by_user_id=trusted_created_by,
            updated_by_user_id=trusted_created_by,
            metadata_json=metadata_value,
        )

    @property
    def is_active(self) -> bool:
        return self.status == PROJECT_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_archived(self) -> bool:
        return self.status == PROJECT_STATUS_ARCHIVED

    @property
    def is_deleted(self) -> bool:
        return self.status == PROJECT_STATUS_DELETED or self.deleted_at is not None

    @property
    def public_key(self) -> str:
        return self.project_id

    @property
    def has_external_app_link(self) -> bool:
        return bool(self.external_app_project_id)

    @property
    def app_project_public_id(self) -> Optional[str]:
        """Compatibility alias for external_app_project_id."""
        return self.external_app_project_id

    @property
    def has_owner(self) -> bool:
        """Return whether the complete owner pair is present."""
        return bool(self.owner_type and self.owner_id)

    @property
    def is_user_owned(self) -> bool:
        """Return whether this project has a canonical external user owner."""
        return self.owner_type == PROJECT_OWNER_TYPE_USER and bool(self.owner_id)

    @property
    def owner_user_id(self) -> Optional[str]:
        """Preferred compatibility alias for a user-owned project's owner id."""
        if not self.is_user_owned:
            return None
        return self.owner_id

    @property
    def owner_reference(self) -> Optional[Dict[str, str]]:
        """Return a small service-reference object without resolving a user."""
        if not self.has_owner:
            return None
        return {
            "type": str(self.owner_type),
            "id": str(self.owner_id),
            "userId": self.owner_user_id,
        }

    def touch(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Mark the project as updated and increment its optimistic revision."""
        self.updated_at = utc_now()
        self.revision = int(self.revision or 1) + 1

        normalized_user_id = normalize_external_user_id(
            updated_by_user_id,
            field_name="updated_by_user_id",
            required=False,
        )

        if normalized_user_id is not None:
            self.updated_by_user_id = normalized_user_id

    def ensure_not_deleted(self) -> None:
        """Raise when a mutation is attempted on a soft-deleted project."""
        if self.is_deleted:
            raise ValueError(
                f"Project '{self.project_id}' is deleted and cannot be modified."
            )

    def rename(
        self,
        *,
        name: str,
        slug: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Rename the project and optionally update its slug."""
        self.ensure_not_deleted()
        self.name = normalize_required_text(
            name,
            field_name="name",
            max_length=PROJECT_NAME_MAX_LENGTH,
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
        """Update the project description."""
        self.ensure_not_deleted()
        self.description = normalize_optional_text(
            description,
            field_name="description",
            max_length=PROJECT_DESCRIPTION_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_default_universe_id(
        self,
        default_universe_id: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set the public default universe id.

        The repository/service layer should verify that the universe exists and
        belongs to this project.
        """
        self.ensure_not_deleted()
        self.default_universe_id = normalize_optional_text(
            default_universe_id,
            field_name="default_universe_id",
            max_length=PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_default_world_id(
        self,
        default_world_id: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set the public default world id.

        The repository/service layer should verify that the world exists and
        belongs to this project/universe.
        """
        self.ensure_not_deleted()
        self.default_world_id = normalize_optional_text(
            default_world_id,
            field_name="default_world_id",
            max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_spawn_world_id(
        self,
        spawn_world_id: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set the public spawn world id."""
        self.ensure_not_deleted()
        self.spawn_world_id = normalize_optional_text(
            spawn_world_id,
            field_name="spawn_world_id",
            max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_world_refs(
        self,
        *,
        default_universe_id: Optional[str] = None,
        default_world_id: Optional[str] = None,
        spawn_world_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Update universe/world references in one revision bump."""
        self.ensure_not_deleted()

        if default_universe_id is not None:
            self.default_universe_id = normalize_optional_text(
                default_universe_id,
                field_name="default_universe_id",
                max_length=PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH,
            )

        if default_world_id is not None:
            self.default_world_id = normalize_optional_text(
                default_world_id,
                field_name="default_world_id",
                max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
            )

        if spawn_world_id is not None:
            self.spawn_world_id = normalize_optional_text(
                spawn_world_id,
                field_name="spawn_world_id",
                max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
            )

        self.touch(updated_by_user_id=updated_by_user_id)

    def set_external_app_link(
        self,
        *,
        external_app_project_id: Optional[str],
        source_service: Optional[str] = "vectoplan-app",
        external_url: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        allow_replace: bool = True,
    ) -> None:
        """Link or clear the external App Project service reference."""
        self.ensure_not_deleted()
        normalized_external_id = normalize_external_app_project_id(
            external_app_project_id
        )
        if (
            not allow_replace
            and self.external_app_project_id is not None
            and normalized_external_id is not None
            and self.external_app_project_id != normalized_external_id
        ):
            raise ValueError(
                "Project is already linked to a different external app project."
            )

        normalized_source = normalize_optional_text(
            source_service,
            field_name="source_service",
            max_length=PROJECT_SOURCE_SERVICE_MAX_LENGTH,
        )
        normalized_url = normalize_optional_text(
            external_url,
            field_name="external_url",
            max_length=PROJECT_EXTERNAL_URL_MAX_LENGTH,
        )
        if (
            self.external_app_project_id == normalized_external_id
            and self.source_service == normalized_source
            and self.external_url == normalized_url
        ):
            return

        self.external_app_project_id = normalized_external_id
        self.source_service = normalized_source
        self.external_url = normalized_url
        self.touch(updated_by_user_id=updated_by_user_id)

    def ensure_external_app_link(
        self,
        *,
        external_app_project_id: str,
        source_service: str = "vectoplan-app",
        external_url: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Idempotently establish one immutable App Project link."""
        self.set_external_app_link(
            external_app_project_id=external_app_project_id,
            source_service=source_service,
            external_url=external_url,
            updated_by_user_id=updated_by_user_id,
            allow_replace=False,
        )

    def set_owner(
        self,
        *,
        owner_type: Optional[str] = None,
        owner_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        allow_clear: bool = False,
    ) -> None:
        """Set the complete project owner pair atomically."""
        self.ensure_not_deleted()
        wants_clear = (
            owner_type is None and owner_id is None and owner_user_id is None
        )
        if wants_clear:
            if not allow_clear:
                raise ValueError(
                    "Project ownership cannot be cleared unless allow_clear=True."
                )
            normalized_owner_type = None
            normalized_owner_id = None
        else:
            normalized_owner_type, normalized_owner_id = normalize_owner_pair(
                owner_type=owner_type,
                owner_id=owner_id,
                owner_user_id=owner_user_id,
                required=True,
            )

        if (
            self.owner_type == normalized_owner_type
            and self.owner_id == normalized_owner_id
        ):
            return
        self.owner_type = normalized_owner_type
        self.owner_id = normalized_owner_id
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_owner_user(
        self,
        owner_user_id: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set or transfer ownership to one external user id."""
        self.set_owner(
            owner_user_id=owner_user_id,
            updated_by_user_id=updated_by_user_id,
        )

    def clear_owner(
        self,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Explicit maintenance helper; normal project flows should keep an owner."""
        self.set_owner(
            updated_by_user_id=updated_by_user_id,
            allow_clear=True,
        )

    def is_owned_by_user(self, user_id: Any) -> bool:
        """Compare against one external user id without database lookup."""
        try:
            normalized = normalize_external_user_id(
                user_id,
                field_name="user_id",
                required=True,
            )
        except Exception:
            return False
        return self.owner_user_id == normalized

    def set_status(
        self,
        status: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set project status.

        Prefer using archive(), restore() and soft_delete() in application code
        because those methods maintain timestamp fields consistently.
        """
        normalized_status = normalize_status(status)
        now = utc_now()

        if normalized_status == PROJECT_STATUS_DELETED:
            self.deleted_at = self.deleted_at or now
        elif normalized_status == PROJECT_STATUS_ARCHIVED:
            self.archived_at = self.archived_at or now
            self.deleted_at = None
        elif normalized_status == PROJECT_STATUS_ACTIVE:
            self.archived_at = None
            self.deleted_at = None

        self.status = normalized_status
        self.touch(updated_by_user_id=updated_by_user_id)

    def archive(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Archive the project without deleting historical data."""
        self.ensure_not_deleted()
        self.status = PROJECT_STATUS_ARCHIVED
        self.archived_at = self.archived_at or utc_now()
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Restore an archived or soft-deleted project."""
        self.status = PROJECT_STATUS_ACTIVE
        self.archived_at = None
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """
        Soft-delete the project.

        This intentionally keeps chunks, command logs and events available for
        audit/history/AI-training purposes unless a later explicit purge process
        removes them.
        """
        now = utc_now()
        self.status = PROJECT_STATUS_DELETED
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
        external_app_project_id: Optional[str] = None,
        chunk_universe_id: Optional[str] = None,
        chunk_world_id: Optional[str] = None,
        route_hints: Optional[Mapping[str, Any]] = None,
        app_payload: Optional[Mapping[str, Any]] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Merge standard project-provisioning metadata."""
        metadata_update: Dict[str, Any] = {
            "schemaVersion": PROJECT_SCHEMA_VERSION,
            "chunkProjectId": self.project_id,
            "externalAppProjectId": external_app_project_id or self.external_app_project_id,
            "chunkUniverseId": chunk_universe_id or self.default_universe_id,
            "chunkWorldId": chunk_world_id or self.spawn_world_id or self.default_world_id,
            "sourceService": self.source_service,
            "ownerType": self.owner_type,
            "ownerUserId": self.owner_user_id,
            "routeHints": make_json_safe(route_hints or {}),
            "appPayload": make_json_safe(app_payload or {}),
            "linkedAt": datetime_to_iso(utc_now()),
        }
        self.update_metadata(metadata_update, updated_by_user_id=updated_by_user_id)

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
        - defaultUniverseId / default_universe_id
        - defaultWorldId / default_world_id
        - spawnWorldId / spawn_world_id
        - externalAppProjectId / external_app_project_id
        - sourceService / source_service
        - externalUrl / external_url
        - ownerUserId / owner_user_id (preferred)
        - ownerType / owner_type + ownerId / owner_id (atomic compatibility pair)
        - metadata / metadataJson / metadata_json
        - metadataMerge
        - metadataRemoveKeys
        - status
        """
        if not isinstance(payload, Mapping):
            raise ValueError("Project patch payload must be a JSON object.")

        self.ensure_not_deleted()

        changed = False

        if "name" in payload:
            self.name = normalize_required_text(
                payload.get("name"),
                field_name="name",
                max_length=PROJECT_NAME_MAX_LENGTH,
            )
            changed = True

        if "slug" in payload:
            self.slug = normalize_slug(payload.get("slug"))
            changed = True

        if "description" in payload:
            self.description = normalize_optional_text(
                payload.get("description"),
                field_name="description",
                max_length=PROJECT_DESCRIPTION_MAX_LENGTH,
            )
            changed = True

        if "defaultUniverseId" in payload or "default_universe_id" in payload:
            self.default_universe_id = normalize_optional_text(
                payload.get("defaultUniverseId")
                if "defaultUniverseId" in payload
                else payload.get("default_universe_id"),
                field_name="default_universe_id",
                max_length=PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH,
            )
            changed = True

        if "defaultWorldId" in payload or "default_world_id" in payload:
            self.default_world_id = normalize_optional_text(
                payload.get("defaultWorldId")
                if "defaultWorldId" in payload
                else payload.get("default_world_id"),
                field_name="default_world_id",
                max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
            )
            changed = True

        if "spawnWorldId" in payload or "spawn_world_id" in payload:
            self.spawn_world_id = normalize_optional_text(
                payload.get("spawnWorldId")
                if "spawnWorldId" in payload
                else payload.get("spawn_world_id"),
                field_name="spawn_world_id",
                max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
            )
            changed = True

        if (
            "externalAppProjectId" in payload
            or "external_app_project_id" in payload
            or "appProjectPublicId" in payload
            or "app_project_public_id" in payload
        ):
            self.external_app_project_id = normalize_external_app_project_id(
                payload.get("externalAppProjectId")
                if "externalAppProjectId" in payload
                else payload.get("external_app_project_id")
                if "external_app_project_id" in payload
                else payload.get("appProjectPublicId")
                if "appProjectPublicId" in payload
                else payload.get("app_project_public_id")
            )
            changed = True

        if "sourceService" in payload or "source_service" in payload:
            self.source_service = normalize_optional_text(
                payload.get("sourceService")
                if "sourceService" in payload
                else payload.get("source_service"),
                field_name="source_service",
                max_length=PROJECT_SOURCE_SERVICE_MAX_LENGTH,
            )
            changed = True

        if "externalUrl" in payload or "external_url" in payload:
            self.external_url = normalize_optional_text(
                payload.get("externalUrl")
                if "externalUrl" in payload
                else payload.get("external_url"),
                field_name="external_url",
                max_length=PROJECT_EXTERNAL_URL_MAX_LENGTH,
            )
            changed = True

        owner_user_key = _payload_present_key(
            payload,
            "ownerUserId",
            "owner_user_id",
        )
        owner_type_key = _payload_present_key(payload, "ownerType", "owner_type")
        owner_id_key = _payload_present_key(payload, "ownerId", "owner_id")

        if owner_user_key is not None:
            owner_user_value = payload.get(owner_user_key)
            generic_owner_type = (
                payload.get(owner_type_key) if owner_type_key is not None else None
            )
            generic_owner_id = (
                payload.get(owner_id_key) if owner_id_key is not None else None
            )
            normalized_owner_type, normalized_owner_id = normalize_owner_pair(
                owner_type=generic_owner_type,
                owner_id=generic_owner_id,
                owner_user_id=owner_user_value,
                required=True,
            )
            if (
                self.owner_type != normalized_owner_type
                or self.owner_id != normalized_owner_id
            ):
                self.owner_type = normalized_owner_type
                self.owner_id = normalized_owner_id
                changed = True
        elif owner_type_key is not None or owner_id_key is not None:
            if (
                owner_type_key is not None
                and normalize_owner_type(payload.get(owner_type_key)) is None
            ):
                raise ValueError(
                    "ownerType cannot be cleared independently; use a complete "
                    "owner transfer instead."
                )
            proposed_owner_type = (
                payload.get(owner_type_key)
                if owner_type_key is not None
                else self.owner_type
            )
            proposed_owner_id = (
                payload.get(owner_id_key)
                if owner_id_key is not None
                else self.owner_id
            )
            normalized_owner_type, normalized_owner_id = normalize_owner_pair(
                owner_type=proposed_owner_type,
                owner_id=proposed_owner_id,
                required=True,
            )
            if (
                self.owner_type != normalized_owner_type
                or self.owner_id != normalized_owner_id
            ):
                self.owner_type = normalized_owner_type
                self.owner_id = normalized_owner_id
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
            normalize_project_id(self.project_id)
        except Exception as exc:
            errors["projectId"] = str(exc)

        try:
            normalize_required_text(
                self.name,
                field_name="name",
                max_length=PROJECT_NAME_MAX_LENGTH,
            )
        except Exception as exc:
            errors["name"] = str(exc)

        try:
            normalize_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_metadata(self.metadata_json)
        except Exception as exc:
            errors["metadataJson"] = str(exc)

        if self.slug is not None:
            try:
                normalize_slug(self.slug)
            except Exception as exc:
                errors["slug"] = str(exc)

        if self.default_universe_id is not None:
            try:
                normalize_optional_text(
                    self.default_universe_id,
                    field_name="default_universe_id",
                    max_length=PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH,
                )
            except Exception as exc:
                errors["defaultUniverseId"] = str(exc)

        if self.default_world_id is not None:
            try:
                normalize_optional_text(
                    self.default_world_id,
                    field_name="default_world_id",
                    max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
                )
            except Exception as exc:
                errors["defaultWorldId"] = str(exc)

        if self.spawn_world_id is not None:
            try:
                normalize_optional_text(
                    self.spawn_world_id,
                    field_name="spawn_world_id",
                    max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
                )
            except Exception as exc:
                errors["spawnWorldId"] = str(exc)

        if self.external_app_project_id is not None:
            try:
                normalize_external_app_project_id(self.external_app_project_id)
            except Exception as exc:
                errors["externalAppProjectId"] = str(exc)

        if self.source_service is not None:
            try:
                normalize_optional_text(
                    self.source_service,
                    field_name="source_service",
                    max_length=PROJECT_SOURCE_SERVICE_MAX_LENGTH,
                )
            except Exception as exc:
                errors["sourceService"] = str(exc)
        elif self.external_app_project_id is not None:
            errors["sourceService"] = (
                "source_service is required when external_app_project_id is set."
            )

        if self.external_url is not None:
            try:
                normalize_optional_text(
                    self.external_url,
                    field_name="external_url",
                    max_length=PROJECT_EXTERNAL_URL_MAX_LENGTH,
                )
            except Exception as exc:
                errors["externalUrl"] = str(exc)

        try:
            normalize_owner_pair(
                owner_type=self.owner_type,
                owner_id=self.owner_id,
                required=True,
            )
        except Exception as exc:
            errors["ownerUserId"] = str(exc)

        for field_name in ("created_by_user_id", "updated_by_user_id"):
            value = getattr(self, field_name, None)
            try:
                normalize_external_user_id(
                    value,
                    field_name=field_name,
                    required=field_name == "created_by_user_id",
                )
            except Exception as exc:
                response_key = (
                    "createdByUserId"
                    if field_name == "created_by_user_id"
                    else "updatedByUserId"
                )
                errors[response_key] = str(exc)

        try:
            if self.revision is None or int(self.revision) < 1:
                errors["revision"] = (
                    "revision must be greater than or equal to 1."
                )
        except Exception as exc:
            errors["revision"] = f"revision must be an integer: {exc}"

        if self.status == PROJECT_STATUS_ACTIVE and self.deleted_at is not None:
            errors["deletedAt"] = "active projects must not have deleted_at set."
        if self.status == PROJECT_STATUS_DELETED and self.deleted_at is None:
            errors["deletedAt"] = "deleted projects must have deleted_at set."

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
    ) -> Dict[str, Any]:
        """
        Serialize the project for API/service responses.

        Internal database IDs are excluded by default.
        """
        result: Dict[str, Any] = {
            "projectId": self.project_id,
            "chunkProjectId": self.project_id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "defaultUniverseId": self.default_universe_id,
            "defaultWorldId": self.default_world_id,
            "spawnWorldId": self.spawn_world_id,
            "externalAppProjectId": self.external_app_project_id,
            "appProjectPublicId": self.external_app_project_id,
            "sourceService": self.source_service,
            "externalUrl": self.external_url,
            "ownerType": self.owner_type,
            "ownerId": self.owner_id,
            "ownerUserId": self.owner_user_id,
            "owner": make_json_safe(self.owner_reference),
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
                "linkedToAppProject": self.has_external_app_link,
                "hasOwner": self.has_owner,
                "userOwned": self.is_user_owned,
            },
        }

        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)

        if include_internal:
            result["id"] = self.id

        return result

    def to_public_dict(self) -> Dict[str, Any]:
        """Serialize without internal database identifiers."""
        return self.to_dict(include_internal=False, include_metadata=True)


__all__ = [
    "PROJECT_SCHEMA_VERSION",
    "DEFAULT_PROJECT_OWNER_USER_ID",
    "PROJECT_OWNER_TYPE_USER",
    "VALID_PROJECT_OWNER_TYPES",
    "PROJECT_STATUS_ACTIVE",
    "PROJECT_STATUS_ARCHIVED",
    "PROJECT_STATUS_DELETED",
    "VALID_PROJECT_STATUSES",
    "Project",
    "utc_now",
    "datetime_to_iso",
    "make_json_safe",
    "normalize_optional_text",
    "normalize_required_text",
    "normalize_public_id",
    "normalize_project_id",
    "normalize_external_app_project_id",
    "normalize_slug",
    "normalize_status",
    "normalize_external_user_id",
    "normalize_owner_type",
    "normalize_owner_pair",
    "normalize_metadata",
    "generate_project_id",
    "get_project_normalization_cache_info",
    "reset_project_normalization_caches",
]

