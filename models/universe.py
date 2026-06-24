# services/vectoplan-chunk/models/universe.py
"""
SQLAlchemy model for VECTOPLAN Chunk universes.

A Universe is a persistent container inside a chunk Project. It groups one or
more concrete editable WorldInstance rows.

Current intended hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Service boundary:
- Project.external_app_project_id links the chunk project to vectoplan-app.
- Universe remains internal to vectoplan-chunk.
- Universe ids are stable public/API identifiers inside one chunk project.
- Universe ids are not required to be globally unique.

Important design rules:
- `id` is the internal database primary key.
- `project_db_id` references `projects.id`.
- `universe_id` is unique per chunk project.
- `default_world_id` and `spawn_world_id` are public chunk world ids.
- the first provisioned universe normally contains one editable spawn world.
- deletion is soft-delete by default.
- this model does not perform commits.
- repository/service/route layers own database transactions.
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
        "models/universe.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
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


UNIVERSE_SCHEMA_VERSION = "universe.schema.v2"

UNIVERSE_STATUS_ACTIVE = "active"
UNIVERSE_STATUS_ARCHIVED = "archived"
UNIVERSE_STATUS_DELETED = "deleted"

VALID_UNIVERSE_STATUSES = frozenset(
    {
        UNIVERSE_STATUS_ACTIVE,
        UNIVERSE_STATUS_ARCHIVED,
        UNIVERSE_STATUS_DELETED,
    }
)

UNIVERSE_SCOPE_PROJECT = "project"

UNIVERSE_ROLE_DEFAULT = "default"
UNIVERSE_ROLE_WORKSPACE = "workspace"
UNIVERSE_ROLE_SANDBOX = "sandbox"
UNIVERSE_ROLE_SIMULATION = "simulation"

VALID_UNIVERSE_ROLES = frozenset(
    {
        UNIVERSE_ROLE_DEFAULT,
        UNIVERSE_ROLE_WORKSPACE,
        UNIVERSE_ROLE_SANDBOX,
        UNIVERSE_ROLE_SIMULATION,
    }
)

UNIVERSE_ID_MAX_LENGTH = 96
UNIVERSE_SLUG_MAX_LENGTH = 120
UNIVERSE_NAME_MAX_LENGTH = 255
UNIVERSE_DESCRIPTION_MAX_LENGTH = 4096
UNIVERSE_ROLE_MAX_LENGTH = 64
UNIVERSE_SCOPE_MAX_LENGTH = 64
UNIVERSE_WORLD_ID_MAX_LENGTH = 96
UNIVERSE_USER_ID_MAX_LENGTH = 128

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


def normalize_universe_id(value: Any) -> str:
    """Normalize a public universe id."""
    return normalize_public_id(
        value,
        field_name="universe_id",
        max_length=UNIVERSE_ID_MAX_LENGTH,
    )


def normalize_world_id(value: Any, *, field_name: str) -> Optional[str]:
    """Normalize optional world references such as default_world_id."""
    if value is None:
        return None

    return normalize_public_id(
        value,
        field_name=field_name,
        max_length=UNIVERSE_WORLD_ID_MAX_LENGTH,
    )


def normalize_slug(value: Any) -> Optional[str]:
    """Normalize optional universe slugs."""
    text = normalize_optional_text(
        value,
        field_name="slug",
        max_length=UNIVERSE_SLUG_MAX_LENGTH,
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
    """Normalize and validate universe status."""
    if value is None:
        return UNIVERSE_STATUS_ACTIVE

    try:
        status = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("status must be text-like.") from exc

    if status not in VALID_UNIVERSE_STATUSES:
        allowed = ", ".join(sorted(VALID_UNIVERSE_STATUSES))
        raise ValueError(f"Invalid universe status '{value}'. Allowed: {allowed}.")

    return status


def normalize_universe_role(value: Any) -> str:
    """Normalize and validate universe role."""
    if value is None:
        return UNIVERSE_ROLE_DEFAULT

    try:
        role = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("universe_role must be text-like.") from exc

    if role not in VALID_UNIVERSE_ROLES:
        allowed = ", ".join(sorted(VALID_UNIVERSE_ROLES))
        raise ValueError(f"Invalid universe role '{value}'. Allowed: {allowed}.")

    return role


def normalize_universe_scope(value: Any) -> str:
    """Normalize universe scope."""
    if value is None:
        return UNIVERSE_SCOPE_PROJECT

    try:
        scope = str(value).strip().lower()
    except Exception as exc:
        raise ValueError("universe_scope must be text-like.") from exc

    if scope != UNIVERSE_SCOPE_PROJECT:
        raise ValueError(
            f"Invalid universe scope '{value}'. "
            f"Only '{UNIVERSE_SCOPE_PROJECT}' is supported in this service slice."
        )

    return scope


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


def normalize_project_db_id(value: Any) -> int:
    """Normalize an internal project database id."""
    if value is None:
        raise ValueError("project_db_id is required.")

    try:
        project_db_id = int(value)
    except Exception as exc:
        raise ValueError("project_db_id must be an integer.") from exc

    if project_db_id <= 0:
        raise ValueError("project_db_id must be greater than zero.")

    return project_db_id


def generate_universe_id(prefix: str = "univ") -> str:
    """
    Generate a stable public universe identifier.

    Example:
        univ_2f2f7a1c9d3b4a44a5d9e41910b51e70
    """
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="universe_id_prefix",
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
    if "universeMetadata" in payload:
        return payload.get("universeMetadata")
    if "universe_metadata" in payload:
        return payload.get("universe_metadata")
    return None


class Universe(db.Model):
    """
    Persistent Chunk universe.

    A universe groups one or more world instances inside a chunk project.

    The default app-integrated flow creates:

        Project(external_app_project_id="prj_...")
          -> Universe(universe_id="chk_uni_...")
              -> WorldInstance(world_id="chk_wld_..." or "world_spawn")

    This model intentionally does not create worlds itself. Repository/service
    code should create project + universe + spawn world atomically.
    """

    __tablename__ = "universes"

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

    universe_id = db.Column(
        db.String(UNIVERSE_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    slug = db.Column(
        db.String(UNIVERSE_SLUG_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    name = db.Column(
        db.String(UNIVERSE_NAME_MAX_LENGTH),
        nullable=False,
    )

    description = db.Column(
        db.String(UNIVERSE_DESCRIPTION_MAX_LENGTH),
        nullable=True,
    )

    status = db.Column(
        db.String(32),
        nullable=False,
        default=UNIVERSE_STATUS_ACTIVE,
        index=True,
    )

    schema_version = db.Column(
        db.String(64),
        nullable=False,
        default=UNIVERSE_SCHEMA_VERSION,
    )

    revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    universe_role = db.Column(
        db.String(UNIVERSE_ROLE_MAX_LENGTH),
        nullable=False,
        default=UNIVERSE_ROLE_DEFAULT,
        index=True,
    )

    universe_scope = db.Column(
        db.String(UNIVERSE_SCOPE_MAX_LENGTH),
        nullable=False,
        default=UNIVERSE_SCOPE_PROJECT,
        index=True,
    )

    default_world_id = db.Column(
        db.String(UNIVERSE_WORLD_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    spawn_world_id = db.Column(
        db.String(UNIVERSE_WORLD_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    created_by_user_id = db.Column(
        db.String(UNIVERSE_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    updated_by_user_id = db.Column(
        db.String(UNIVERSE_USER_ID_MAX_LENGTH),
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
            "universes",
            lazy="selectin",
            cascade="all, delete-orphan",
            passive_deletes=True,
        ),
        lazy="joined",
    )

    __table_args__ = (
        db.UniqueConstraint(
            "project_db_id",
            "universe_id",
            name="uq_universes_project_universe_id",
        ),
        db.UniqueConstraint(
            "project_db_id",
            "slug",
            name="uq_universes_project_slug",
        ),
        db.CheckConstraint(
            "project_db_id > 0",
            name="ck_universes_project_db_id_positive",
        ),
        db.CheckConstraint(
            "universe_id <> ''",
            name="ck_universes_universe_id_not_empty",
        ),
        db.CheckConstraint(
            "name <> ''",
            name="ck_universes_name_not_empty",
        ),
        db.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_universes_status_valid",
        ),
        db.CheckConstraint(
            "universe_role IN ('default', 'workspace', 'sandbox', 'simulation')",
            name="ck_universes_role_valid",
        ),
        db.CheckConstraint(
            "universe_scope IN ('project')",
            name="ck_universes_scope_valid",
        ),
        db.CheckConstraint(
            "revision >= 1",
            name="ck_universes_revision_positive",
        ),
        db.Index(
            "ix_universes_project_status_created_at",
            "project_db_id",
            "status",
            "created_at",
        ),
        db.Index(
            "ix_universes_project_role",
            "project_db_id",
            "universe_role",
        ),
        db.Index(
            "ix_universes_project_default_world",
            "project_db_id",
            "default_world_id",
        ),
        db.Index(
            "ix_universes_project_spawn_world",
            "project_db_id",
            "spawn_world_id",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Universe id={self.id!r} project_db_id={self.project_db_id!r} "
            f"universe_id={self.universe_id!r} status={self.status!r}>"
        )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: int,
        universe_id: Optional[str] = None,
        name: Optional[str] = None,
        slug: Optional[str] = None,
        description: Optional[str] = None,
        status: str = UNIVERSE_STATUS_ACTIVE,
        universe_role: str = UNIVERSE_ROLE_DEFAULT,
        universe_scope: str = UNIVERSE_SCOPE_PROJECT,
        default_world_id: Optional[str] = None,
        spawn_world_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "Universe":
        """
        Create a Universe instance without adding it to a session.

        Repository/service code is responsible for:
        - checking project existence
        - checking uniqueness inside the project
        - adding to db.session
        - committing or rolling back
        """
        normalized_project_db_id = normalize_project_db_id(project_db_id)

        public_universe_id = normalize_universe_id(
            universe_id or generate_universe_id()
        )

        normalized_name = normalize_required_text(
            name or public_universe_id,
            field_name="name",
            max_length=UNIVERSE_NAME_MAX_LENGTH,
        )

        normalized_status = normalize_status(status)
        now = utc_now()

        return cls(
            project_db_id=normalized_project_db_id,
            universe_id=public_universe_id,
            slug=normalize_slug(slug),
            name=normalized_name,
            description=normalize_optional_text(
                description,
                field_name="description",
                max_length=UNIVERSE_DESCRIPTION_MAX_LENGTH,
            ),
            status=normalized_status,
            schema_version=UNIVERSE_SCHEMA_VERSION,
            revision=1,
            universe_role=normalize_universe_role(universe_role),
            universe_scope=normalize_universe_scope(universe_scope),
            default_world_id=normalize_world_id(
                default_world_id,
                field_name="default_world_id",
            ),
            spawn_world_id=normalize_world_id(
                spawn_world_id,
                field_name="spawn_world_id",
            ),
            created_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="created_by_user_id",
                max_length=UNIVERSE_USER_ID_MAX_LENGTH,
            ),
            updated_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="updated_by_user_id",
                max_length=UNIVERSE_USER_ID_MAX_LENGTH,
            ),
            metadata_json=normalize_metadata(metadata_json),
            created_at=now,
            updated_at=now,
            archived_at=now if normalized_status == UNIVERSE_STATUS_ARCHIVED else None,
            deleted_at=now if normalized_status == UNIVERSE_STATUS_DELETED else None,
        )

    @classmethod
    def create_for_project(
        cls,
        project: Any,
        *,
        universe_id: Optional[str] = None,
        name: Optional[str] = None,
        slug: Optional[str] = None,
        description: Optional[str] = None,
        status: str = UNIVERSE_STATUS_ACTIVE,
        universe_role: str = UNIVERSE_ROLE_DEFAULT,
        default_world_id: Optional[str] = None,
        spawn_world_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "Universe":
        """
        Create a Universe for a Project model instance.

        The project instance must already have an internal database id.
        """
        project_db_id = getattr(project, "id", None)
        if project_db_id is None:
            raise ValueError(
                "Cannot create universe for project without persisted project.id."
            )

        return cls.create(
            project_db_id=project_db_id,
            universe_id=universe_id,
            name=name,
            slug=slug,
            description=description,
            status=status,
            universe_role=universe_role,
            universe_scope=UNIVERSE_SCOPE_PROJECT,
            default_world_id=default_world_id,
            spawn_world_id=spawn_world_id,
            created_by_user_id=created_by_user_id,
            metadata_json=metadata_json,
        )

    @classmethod
    def from_create_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        project_db_id: int,
        created_by_user_id: Optional[str] = None,
    ) -> "Universe":
        """
        Create a Universe instance from an API-style payload.

        Supported keys:
        - universeId / universe_id / chunkUniverseId / chunk_universe_id
        - name / universeName
        - slug
        - description
        - universeRole / universe_role
        - defaultWorldId / default_world_id
        - spawnWorldId / spawn_world_id
        - metadata / metadataJson / metadata_json
        """
        if not isinstance(payload, Mapping):
            raise ValueError("Universe create payload must be a JSON object.")

        metadata_value = _payload_metadata_value(payload)

        return cls.create(
            project_db_id=project_db_id,
            universe_id=(
                payload.get("chunkUniverseId")
                or payload.get("chunk_universe_id")
                or payload.get("universeId")
                or payload.get("universe_id")
            ),
            name=(
                payload.get("universeName")
                or payload.get("universe_name")
                or payload.get("name")
            ),
            slug=payload.get("slug") or payload.get("universeSlug") or payload.get("universe_slug"),
            description=payload.get("description"),
            universe_role=payload.get("universeRole") or payload.get("universe_role") or UNIVERSE_ROLE_DEFAULT,
            default_world_id=(
                payload.get("defaultWorldId")
                or payload.get("default_world_id")
                or payload.get("worldId")
                or payload.get("world_id")
                or payload.get("chunkWorldId")
                or payload.get("chunk_world_id")
            ),
            spawn_world_id=(
                payload.get("spawnWorldId")
                or payload.get("spawn_world_id")
                or payload.get("worldId")
                or payload.get("world_id")
                or payload.get("chunkWorldId")
                or payload.get("chunk_world_id")
            ),
            created_by_user_id=created_by_user_id,
            metadata_json=metadata_value,
        )

    @property
    def is_active(self) -> bool:
        return self.status == UNIVERSE_STATUS_ACTIVE and self.deleted_at is None

    @property
    def is_archived(self) -> bool:
        return self.status == UNIVERSE_STATUS_ARCHIVED

    @property
    def is_deleted(self) -> bool:
        return self.status == UNIVERSE_STATUS_DELETED or self.deleted_at is not None

    @property
    def project_public_id(self) -> Optional[str]:
        """
        Return the parent project's public id if the relationship is available.

        Repository/serializer layers can also inject projectId externally when
        they already resolved the project context.
        """
        try:
            project = getattr(self, "project", None)
            return getattr(project, "project_id", None)
        except Exception:
            return None

    @property
    def chunk_project_id(self) -> Optional[str]:
        """Compatibility alias for parent Project.project_id."""
        return self.project_public_id

    @property
    def chunk_universe_id(self) -> str:
        """Compatibility alias for universe_id."""
        return self.universe_id

    @property
    def effective_default_world_id(self) -> Optional[str]:
        """Return default world id with spawn fallback."""
        return self.default_world_id or self.spawn_world_id

    @property
    def effective_spawn_world_id(self) -> Optional[str]:
        """Return spawn world id with default fallback."""
        return self.spawn_world_id or self.default_world_id

    def touch(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Mark the universe as updated and increment its optimistic revision."""
        self.updated_at = utc_now()
        self.revision = int(self.revision or 1) + 1

        normalized_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="updated_by_user_id",
            max_length=UNIVERSE_USER_ID_MAX_LENGTH,
        )

        if normalized_user_id is not None:
            self.updated_by_user_id = normalized_user_id

    def ensure_not_deleted(self) -> None:
        """Raise when a mutation is attempted on a soft-deleted universe."""
        if self.is_deleted:
            raise ValueError(
                f"Universe '{self.universe_id}' is deleted and cannot be modified."
            )

    def rename(
        self,
        *,
        name: str,
        slug: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Rename the universe and optionally update its slug."""
        self.ensure_not_deleted()
        self.name = normalize_required_text(
            name,
            field_name="name",
            max_length=UNIVERSE_NAME_MAX_LENGTH,
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
        """Update the universe description."""
        self.ensure_not_deleted()
        self.description = normalize_optional_text(
            description,
            field_name="description",
            max_length=UNIVERSE_DESCRIPTION_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_role(
        self,
        universe_role: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set the universe role."""
        self.ensure_not_deleted()
        self.universe_role = normalize_universe_role(universe_role)
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
        belongs to this universe.
        """
        self.ensure_not_deleted()
        self.default_world_id = normalize_world_id(
            default_world_id,
            field_name="default_world_id",
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_spawn_world_id(
        self,
        spawn_world_id: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set the public spawn world id.

        The repository/service layer should verify that the world exists and
        belongs to this universe.
        """
        self.ensure_not_deleted()
        self.spawn_world_id = normalize_world_id(
            spawn_world_id,
            field_name="spawn_world_id",
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_world_defaults(
        self,
        *,
        default_world_id: Optional[str],
        spawn_world_id: Optional[str],
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set default and spawn world ids together."""
        self.ensure_not_deleted()
        self.default_world_id = normalize_world_id(
            default_world_id,
            field_name="default_world_id",
        )
        self.spawn_world_id = normalize_world_id(
            spawn_world_id,
            field_name="spawn_world_id",
        )
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_status(
        self,
        status: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """
        Set universe status.

        Prefer using archive(), restore() and soft_delete() in application code
        because those methods maintain timestamp fields consistently.
        """
        normalized_status = normalize_status(status)
        now = utc_now()

        if normalized_status == UNIVERSE_STATUS_DELETED:
            self.deleted_at = self.deleted_at or now
        elif normalized_status == UNIVERSE_STATUS_ARCHIVED:
            self.archived_at = self.archived_at or now
            self.deleted_at = None
        elif normalized_status == UNIVERSE_STATUS_ACTIVE:
            self.archived_at = None
            self.deleted_at = None

        self.status = normalized_status
        self.touch(updated_by_user_id=updated_by_user_id)

    def archive(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Archive the universe without deleting historical data."""
        self.ensure_not_deleted()
        self.status = UNIVERSE_STATUS_ARCHIVED
        self.archived_at = self.archived_at or utc_now()
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Restore an archived or soft-deleted universe."""
        self.status = UNIVERSE_STATUS_ACTIVE
        self.archived_at = None
        self.deleted_at = None
        self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """
        Soft-delete the universe.

        This intentionally keeps worlds, chunks, command logs and events
        available for audit/history/AI-training purposes unless a later explicit
        purge process removes them.
        """
        now = utc_now()
        self.status = UNIVERSE_STATUS_DELETED
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
        chunk_project_id: Optional[str] = None,
        chunk_world_id: Optional[str] = None,
        external_app_project_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Merge standard provisioning metadata."""
        self.update_metadata(
            {
                "schemaVersion": UNIVERSE_SCHEMA_VERSION,
                "chunkProjectId": chunk_project_id or self.project_public_id,
                "chunkUniverseId": self.universe_id,
                "chunkWorldId": chunk_world_id or self.effective_spawn_world_id,
                "externalAppProjectId": external_app_project_id,
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
        - universeRole / universe_role
        - defaultWorldId / default_world_id
        - spawnWorldId / spawn_world_id
        - metadata / metadataJson / metadata_json
        - metadataMerge
        - metadataRemoveKeys
        - status
        """
        if not isinstance(payload, Mapping):
            raise ValueError("Universe patch payload must be a JSON object.")

        self.ensure_not_deleted()

        changed = False

        if "name" in payload:
            self.name = normalize_required_text(
                payload.get("name"),
                field_name="name",
                max_length=UNIVERSE_NAME_MAX_LENGTH,
            )
            changed = True

        if "slug" in payload:
            self.slug = normalize_slug(payload.get("slug"))
            changed = True

        if "description" in payload:
            self.description = normalize_optional_text(
                payload.get("description"),
                field_name="description",
                max_length=UNIVERSE_DESCRIPTION_MAX_LENGTH,
            )
            changed = True

        if "universeRole" in payload or "universe_role" in payload:
            self.universe_role = normalize_universe_role(
                payload.get("universeRole")
                if "universeRole" in payload
                else payload.get("universe_role")
            )
            changed = True

        if "defaultWorldId" in payload or "default_world_id" in payload:
            self.default_world_id = normalize_world_id(
                payload.get("defaultWorldId")
                if "defaultWorldId" in payload
                else payload.get("default_world_id"),
                field_name="default_world_id",
            )
            changed = True

        if "spawnWorldId" in payload or "spawn_world_id" in payload:
            self.spawn_world_id = normalize_world_id(
                payload.get("spawnWorldId")
                if "spawnWorldId" in payload
                else payload.get("spawn_world_id"),
                field_name="spawn_world_id",
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
            normalize_project_db_id(self.project_db_id)
        except Exception as exc:
            errors["projectDbId"] = str(exc)

        try:
            normalize_universe_id(self.universe_id)
        except Exception as exc:
            errors["universeId"] = str(exc)

        try:
            normalize_required_text(
                self.name,
                field_name="name",
                max_length=UNIVERSE_NAME_MAX_LENGTH,
            )
        except Exception as exc:
            errors["name"] = str(exc)

        try:
            normalize_status(self.status)
        except Exception as exc:
            errors["status"] = str(exc)

        try:
            normalize_universe_role(self.universe_role)
        except Exception as exc:
            errors["universeRole"] = str(exc)

        try:
            normalize_universe_scope(self.universe_scope)
        except Exception as exc:
            errors["universeScope"] = str(exc)

        try:
            normalize_metadata(self.metadata_json)
        except Exception as exc:
            errors["metadataJson"] = str(exc)

        if self.slug is not None:
            try:
                normalize_slug(self.slug)
            except Exception as exc:
                errors["slug"] = str(exc)

        if self.default_world_id is not None:
            try:
                normalize_world_id(
                    self.default_world_id,
                    field_name="default_world_id",
                )
            except Exception as exc:
                errors["defaultWorldId"] = str(exc)

        if self.spawn_world_id is not None:
            try:
                normalize_world_id(
                    self.spawn_world_id,
                    field_name="spawn_world_id",
                )
            except Exception as exc:
                errors["spawnWorldId"] = str(exc)

        if self.revision is None or int(self.revision) < 1:
            errors["revision"] = "revision must be greater than or equal to 1."

        return errors

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Serialize the universe for API/service responses.

        Internal database IDs are excluded by default.
        """
        resolved_project_id = project_id if project_id is not None else self.project_public_id

        result: Dict[str, Any] = {
            "projectId": resolved_project_id,
            "chunkProjectId": resolved_project_id,
            "universeId": self.universe_id,
            "chunkUniverseId": self.universe_id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "universeRole": self.universe_role,
            "universeScope": self.universe_scope,
            "defaultWorldId": self.default_world_id,
            "spawnWorldId": self.spawn_world_id,
            "effectiveDefaultWorldId": self.effective_default_world_id,
            "effectiveSpawnWorldId": self.effective_spawn_world_id,
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
                "hasDefaultWorld": bool(self.default_world_id),
                "hasSpawnWorld": bool(self.spawn_world_id),
            },
        }

        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)

        if include_internal:
            result["id"] = self.id
            result["projectDbId"] = self.project_db_id

        return result

    def to_public_dict(self, *, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Serialize without internal database identifiers."""
        return self.to_dict(
            include_internal=False,
            include_metadata=True,
            project_id=project_id,
        )