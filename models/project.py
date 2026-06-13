# services/vectoplan-chunk/models/project.py
"""
SQLAlchemy model for VECTOPLAN projects.

A Project is the top-level persistent container for an editable VECTOPLAN
runtime universe. It does not directly store chunks. Chunks belong to concrete
world instances inside universes that belong to this project.

Current intended hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Important design rules:
- `id` is the internal database primary key.
- `project_id` is the stable public/API identifier.
- `slug` is optional but should be globally unique when present.
- Deletion is soft-delete by default.
- This model does not perform commits.
- Repository/service layers are responsible for database transactions.
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


PROJECT_SCHEMA_VERSION = "project.schema.v1"

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
PROJECT_DESCRIPTION_MAX_LENGTH = 4096

PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
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

    The first character must be alphanumeric.
    """
    text = normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if not PUBLIC_ID_PATTERN.match(text):
        raise ValueError(
            f"{field_name} may only contain letters, numbers, underscores "
            "and dashes, and must start with a letter or number."
        )

    return text


def normalize_project_id(value: Any) -> str:
    """Normalize a public project id."""
    return normalize_public_id(
        value,
        field_name="project_id",
        max_length=PROJECT_ID_MAX_LENGTH,
    )


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
    Generate a stable public project identifier.

    Example:
        proj_2f2f7a1c9d3b4a44a5d9e41910b51e70
    """
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="project_id_prefix",
        max_length=24,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


class Project(db.Model):
    """
    Persistent VECTOPLAN project.

    A project is the editable top-level container visible to the editor.
    It usually owns one default universe and one default spawn world in the
    first implementation phase.

    This model intentionally does not know how to create universes or worlds.
    That belongs in repositories/services so transactions can create:

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

    default_universe_id = db.Column(
        db.String(PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
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
            "ix_projects_active_lookup",
            "project_id",
            "status",
            "deleted_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Project id={self.id!r} project_id={self.project_id!r} "
            f"status={self.status!r}>"
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
        owner_type: Optional[str] = None,
        owner_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "Project":
        """
        Create a Project instance without adding it to a session.

        Repository/service code is responsible for:
        - checking uniqueness
        - adding to db.session
        - committing or rolling back
        """
        public_project_id = normalize_project_id(project_id or generate_project_id())

        normalized_name = normalize_required_text(
            name or public_project_id,
            field_name="name",
            max_length=PROJECT_NAME_MAX_LENGTH,
        )

        normalized_status = normalize_status(status)
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
            owner_type=normalize_optional_text(
                owner_type,
                field_name="owner_type",
                max_length=PROJECT_OWNER_TYPE_MAX_LENGTH,
            ),
            owner_id=normalize_optional_text(
                owner_id,
                field_name="owner_id",
                max_length=PROJECT_OWNER_ID_MAX_LENGTH,
            ),
            created_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="created_by_user_id",
                max_length=PROJECT_USER_ID_MAX_LENGTH,
            ),
            updated_by_user_id=normalize_optional_text(
                created_by_user_id,
                field_name="updated_by_user_id",
                max_length=PROJECT_USER_ID_MAX_LENGTH,
            ),
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
        created_by_user_id: Optional[str] = None,
    ) -> "Project":
        """
        Create the default development project instance.

        This is useful for idempotent local startup seeding.
        """
        return cls.create(
            project_id=project_id,
            slug=project_id,
            name="Dev Project",
            description="Default development project for the chunk-service world slice.",
            default_universe_id=default_universe_id,
            created_by_user_id=created_by_user_id,
            metadata_json={
                "seed": True,
                "seedType": "development",
                "createdBy": "vectoplan-chunk",
            },
        )

    @classmethod
    def from_create_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        created_by_user_id: Optional[str] = None,
    ) -> "Project":
        """
        Create a Project instance from an API-style payload.

        Supported keys:
        - projectId / project_id
        - name
        - slug
        - description
        - defaultUniverseId / default_universe_id
        - ownerType / owner_type
        - ownerId / owner_id
        - metadata / metadataJson / metadata_json
        """
        if not isinstance(payload, Mapping):
            raise ValueError("Project create payload must be a JSON object.")

        metadata_value = (
            payload.get("metadataJson")
            if "metadataJson" in payload
            else payload.get("metadata_json")
            if "metadata_json" in payload
            else payload.get("metadata")
        )

        return cls.create(
            project_id=payload.get("projectId") or payload.get("project_id"),
            name=payload.get("name"),
            slug=payload.get("slug"),
            description=payload.get("description"),
            default_universe_id=(
                payload.get("defaultUniverseId")
                or payload.get("default_universe_id")
            ),
            owner_type=payload.get("ownerType") or payload.get("owner_type"),
            owner_id=payload.get("ownerId") or payload.get("owner_id"),
            created_by_user_id=created_by_user_id,
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

    def touch(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Mark the project as updated and increment its optimistic revision."""
        self.updated_at = utc_now()
        self.revision = int(self.revision or 1) + 1

        normalized_user_id = normalize_optional_text(
            updated_by_user_id,
            field_name="updated_by_user_id",
            max_length=PROJECT_USER_ID_MAX_LENGTH,
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

    def set_owner(
        self,
        *,
        owner_type: Optional[str],
        owner_id: Optional[str],
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Set or clear project ownership metadata."""
        self.ensure_not_deleted()
        self.owner_type = normalize_optional_text(
            owner_type,
            field_name="owner_type",
            max_length=PROJECT_OWNER_TYPE_MAX_LENGTH,
        )
        self.owner_id = normalize_optional_text(
            owner_id,
            field_name="owner_id",
            max_length=PROJECT_OWNER_ID_MAX_LENGTH,
        )
        self.touch(updated_by_user_id=updated_by_user_id)

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
        - ownerType / owner_type
        - ownerId / owner_id
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

        if "ownerType" in payload or "owner_type" in payload:
            self.owner_type = normalize_optional_text(
                payload.get("ownerType")
                if "ownerType" in payload
                else payload.get("owner_type"),
                field_name="owner_type",
                max_length=PROJECT_OWNER_TYPE_MAX_LENGTH,
            )
            changed = True

        if "ownerId" in payload or "owner_id" in payload:
            self.owner_id = normalize_optional_text(
                payload.get("ownerId")
                if "ownerId" in payload
                else payload.get("owner_id"),
                field_name="owner_id",
                max_length=PROJECT_OWNER_ID_MAX_LENGTH,
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

        if self.revision is None or int(self.revision) < 1:
            errors["revision"] = "revision must be greater than or equal to 1."

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
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "defaultUniverseId": self.default_universe_id,
            "ownerType": self.owner_type,
            "ownerId": self.owner_id,
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

        if include_internal:
            result["id"] = self.id

        return result

    def to_public_dict(self) -> Dict[str, Any]:
        """Serialize without internal database identifiers."""
        return self.to_dict(include_internal=False, include_metadata=True)