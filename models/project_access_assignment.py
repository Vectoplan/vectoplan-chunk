# services/vectoplan-chunk/models/project_access_assignment.py
"""
Persistent project-access assignments for ``vectoplan-chunk``.

``vectoplan-app`` remains the source of truth for project membership and roles.
This model stores only the synchronized access projection used by Chunk to
authorize project-scoped reads and mutations.

Security boundary:

* direct user assignments contain only canonical opaque ``auth_user_id`` values;
* local AppUser primary keys, account ids and e-mail addresses are rejected;
* group assignments use ``group_id`` and never masquerade as direct users;
* owner assignments are always direct user assignments;
* viewer assignments are data only -- mutation denial is enforced by the access
  service, not inferred from client payloads;
* public serialization never exposes raw user or group identifiers;
* the model performs no queries, commits, rollbacks or remote calls.

The field names and SQLAlchemy synonyms intentionally match the adaptive
``SQLAlchemyProjectAccessRepository`` in
``src/services/project_access_service.py``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Final, Optional
from uuid import uuid4


try:
    from extensions import db
except Exception as exc:  # pragma: no cover - explicit startup failure
    db = None  # type: ignore[assignment]
    _DB_IMPORT_ERROR = exc
else:
    _DB_IMPORT_ERROR = None


if db is None:  # pragma: no cover
    raise RuntimeError(
        "Could not import `db` from `extensions` while loading "
        "models/project_access_assignment.py. Ensure extensions.py exposes "
        "a Flask-SQLAlchemy compatible `db` instance before importing models."
    ) from _DB_IMPORT_ERROR


try:
    from sqlalchemy import event as sqlalchemy_event
    from sqlalchemy.orm import synonym
except Exception:  # pragma: no cover - import errors surface during app startup
    sqlalchemy_event = None  # type: ignore[assignment]
    synonym = None  # type: ignore[assignment]

try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - sqlite/non-postgres tooling fallback
    JSONB = None  # type: ignore[assignment]


try:
    JSON_COLUMN_TYPE = (
        JSONB()
        .with_variant(db.JSON(), "sqlite")
        .with_variant(db.JSON(), "mysql")
    ) if JSONB is not None else db.JSON
except Exception:  # pragma: no cover
    JSON_COLUMN_TYPE = db.JSON

try:
    BIGINT_PRIMARY_KEY_TYPE = db.BigInteger().with_variant(db.Integer(), "sqlite")
except Exception:  # pragma: no cover
    BIGINT_PRIMARY_KEY_TYPE = db.BigInteger


# -----------------------------------------------------------------------------
# Schema, role and assignment constants
# -----------------------------------------------------------------------------

PROJECT_ACCESS_ASSIGNMENT_SCHEMA_VERSION = "project-access-assignment.schema.v1"
PROJECT_ACCESS_PROJECTION_VERSION = "app-project-access-v1"

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"

ACCESS_ROLES: Final[frozenset[str]] = frozenset(
    {ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER}
)
ROLE_RANK: Final[dict[str, int]] = {
    ROLE_OWNER: 400,
    ROLE_ADMIN: 300,
    ROLE_EDITOR: 200,
    ROLE_VIEWER: 100,
}
ROLE_ALIASES: Final[dict[str, str]] = {
    "administrator": ROLE_ADMIN,
    "project_admin": ROLE_ADMIN,
    "write": ROLE_EDITOR,
    "writer": ROLE_EDITOR,
    "edit": ROLE_EDITOR,
    "read": ROLE_VIEWER,
    "readonly": ROLE_VIEWER,
    "read_only": ROLE_VIEWER,
    "read-only": ROLE_VIEWER,
}

ASSIGNMENT_TYPE_DIRECT = "direct"
ASSIGNMENT_TYPE_GROUP = "group"
ASSIGNMENT_TYPES: Final[frozenset[str]] = frozenset(
    {ASSIGNMENT_TYPE_DIRECT, ASSIGNMENT_TYPE_GROUP}
)
ASSIGNMENT_TYPE_ALIASES: Final[dict[str, str]] = {
    "user": ASSIGNMENT_TYPE_DIRECT,
    "auth_user": ASSIGNMENT_TYPE_DIRECT,
    "principal": ASSIGNMENT_TYPE_DIRECT,
    "member": ASSIGNMENT_TYPE_DIRECT,
    "team": ASSIGNMENT_TYPE_GROUP,
    "role_group": ASSIGNMENT_TYPE_GROUP,
}

DEFAULT_ASSIGNMENT_SOURCE_SERVICE = "vectoplan-app"
DEFAULT_GROUP_SOURCE_SERVICE = "group-directory"

ASSIGNMENT_ID_MAX_LENGTH = 120
CHUNK_PROJECT_ID_MAX_LENGTH = 255
AUTH_USER_ID_MAX_LENGTH = 255
GROUP_ID_MAX_LENGTH = 255
ROLE_MAX_LENGTH = 40
ASSIGNMENT_TYPE_MAX_LENGTH = 40
SOURCE_SERVICE_MAX_LENGTH = 120
PROJECTION_VERSION_MAX_LENGTH = 120
FINGERPRINT_MAX_LENGTH = 128
REQUEST_ID_MAX_LENGTH = 160
CORRELATION_ID_MAX_LENGTH = 160
SCHEMA_VERSION_MAX_LENGTH = 80

_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_AUTH_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])([A-Z0-9._%+-]+)@([A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])"
)
_URL_RE = re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis|amqp)://[^\s]+")
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|_)(?:authorization|bearer|token|secret|password|passwd|cookie|"
    r"session|api_key|apikey|private_key|credential)(?:$|_)"
)
_IDENTITY_KEY_RE = re.compile(
    r"(?i)^(?:auth_user_id|authuserid|subject_auth_user_id|subjectauthuserid|"
    r"user_id|userid|app_user_id|appuserid|local_user_id|localuserid|account_id|"
    r"accountid|owner_id|ownerid|owner_user_id|owneruserid|email)$"
)
_LOCAL_ID_KEY_RE = re.compile(
    r"(?i)^(?:user_id|userid|app_user_id|appuserid|local_user_id|localuserid|"
    r"account_id|accountid|owner_id|ownerid|owner_user_id|owneruserid|email)$"
)
_CANONICAL_ID_KEYS: Final[frozenset[str]] = frozenset(
    {"auth_user_id", "authuserid", "subject_auth_user_id", "subjectauthuserid"}
)


# -----------------------------------------------------------------------------
# Defensive utility functions
# -----------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def datetime_to_iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime defensively."""
    if value is None:
        return None
    try:
        candidate = value
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        return candidate.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(value)


def normalize_datetime(value: Any, *, field_name: str) -> Optional[datetime]:
    """Normalize a datetime or ISO-8601 string to timezone-aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            text = str(value).strip()
        except Exception as exc:
            raise ValueError(f"{field_name} must be a datetime or ISO-8601 string.") from exc
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid ISO-8601 timestamp.") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _normalize_optional_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    reject_controls: bool = True,
) -> Optional[str]:
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
    if reject_controls and _CONTROL_CHARACTER_RE.search(text):
        raise ValueError(f"{field_name} must not contain control characters.")
    return text


def _normalize_required_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    text = _normalize_optional_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )
    if text is None:
        raise ValueError(f"{field_name} is required.")
    return text


def _normalize_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if value is None:
        return default
    try:
        text = str(value).strip().lower()
    except Exception:
        return default
    if text in {"1", "true", "yes", "y", "on", "enabled", "active"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "inactive"}:
        return False
    return default


def normalize_chunk_project_id(value: Any) -> str:
    """Normalize the public Chunk project identifier."""
    text = _normalize_required_text(
        value,
        field_name="chunk_project_id",
        max_length=CHUNK_PROJECT_ID_MAX_LENGTH,
    )
    if not _PUBLIC_ID_RE.fullmatch(text):
        raise ValueError(
            "chunk_project_id may contain only letters, numbers, underscores, "
            "dashes, dots and colons, and must start with a letter or number."
        )
    return text


@lru_cache(maxsize=4096)
def _normalize_auth_user_id_cached(text: str) -> str:
    candidate = text.strip()
    if not candidate:
        raise ValueError("auth_user_id is required for a direct assignment.")
    if candidate.isdigit():
        raise ValueError("Local numeric user ids are not accepted as auth_user_id.")
    if "@" in candidate or "/" in candidate or "\\" in candidate:
        raise ValueError("auth_user_id must be a canonical opaque auth identity.")
    if not _AUTH_USER_ID_RE.fullmatch(candidate):
        raise ValueError(
            "auth_user_id contains unsupported characters or exceeds the maximum length."
        )
    return candidate


def normalize_auth_user_id(value: Any, *, required: bool = True) -> Optional[str]:
    """Normalize a canonical opaque ``auth_user_id`` and reject local ids."""
    if value is None or isinstance(value, bool):
        if required:
            raise ValueError("auth_user_id is required for a direct assignment.")
        return None
    if isinstance(value, int):
        raise ValueError("Local numeric user ids are not accepted as auth_user_id.")
    try:
        text = str(value)
    except Exception as exc:
        raise ValueError("auth_user_id must be text-like.") from exc
    if not text.strip():
        if required:
            raise ValueError("auth_user_id is required for a direct assignment.")
        return None
    return _normalize_auth_user_id_cached(text)


@lru_cache(maxsize=2048)
def _normalize_group_id_cached(text: str) -> str:
    candidate = text.strip()
    if not candidate:
        raise ValueError("group_id is required for a group assignment.")
    if not _GROUP_ID_RE.fullmatch(candidate):
        raise ValueError(
            "group_id contains unsupported characters or exceeds the maximum length."
        )
    return candidate


def normalize_group_id(value: Any, *, required: bool = True) -> Optional[str]:
    """Normalize an opaque group identifier."""
    if value is None or isinstance(value, bool):
        if required:
            raise ValueError("group_id is required for a group assignment.")
        return None
    try:
        text = str(value)
    except Exception as exc:
        raise ValueError("group_id must be text-like.") from exc
    if not text.strip():
        if required:
            raise ValueError("group_id is required for a group assignment.")
        return None
    return _normalize_group_id_cached(text)


@lru_cache(maxsize=64)
def _normalize_role_cached(text: str) -> str:
    normalized = text.strip().lower().replace("-", "_").replace(" ", "_")
    normalized = ROLE_ALIASES.get(normalized, normalized)
    if normalized not in ACCESS_ROLES:
        allowed = ", ".join(sorted(ACCESS_ROLES))
        raise ValueError(f"Invalid project role '{text}'. Allowed: {allowed}.")
    return normalized


def normalize_role(value: Any, *, allow_owner: bool = True) -> str:
    """Normalize a project role."""
    text = _normalize_required_text(value, field_name="role", max_length=ROLE_MAX_LENGTH)
    role = _normalize_role_cached(text)
    if role == ROLE_OWNER and not allow_owner:
        raise ValueError("The owner role is not allowed for this assignment operation.")
    return role


@lru_cache(maxsize=32)
def _normalize_assignment_type_cached(text: str) -> str:
    normalized = text.strip().lower().replace("-", "_").replace(" ", "_")
    normalized = ASSIGNMENT_TYPE_ALIASES.get(normalized, normalized)
    if normalized not in ASSIGNMENT_TYPES:
        allowed = ", ".join(sorted(ASSIGNMENT_TYPES))
        raise ValueError(f"Invalid assignment_type '{text}'. Allowed: {allowed}.")
    return normalized


def normalize_assignment_type(value: Any) -> str:
    """Normalize a direct-user or group assignment type."""
    if value is None:
        return ASSIGNMENT_TYPE_DIRECT
    text = _normalize_required_text(
        value,
        field_name="assignment_type",
        max_length=ASSIGNMENT_TYPE_MAX_LENGTH,
    )
    return _normalize_assignment_type_cached(text)


def normalize_source_service(value: Any, *, default: str) -> str:
    """Normalize the service responsible for the stored assignment."""
    text = _normalize_optional_text(
        value,
        field_name="source_service",
        max_length=SOURCE_SERVICE_MAX_LENGTH,
    ) or default
    if not _SERVICE_ID_RE.fullmatch(text):
        raise ValueError("source_service must be a safe service identifier.")
    return text


def normalize_optional_identifier(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    """Normalize optional request/fingerprint identifiers."""
    text = _normalize_optional_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )
    if text is None:
        return None
    if not _FINGERPRINT_RE.fullmatch(text):
        raise ValueError(f"{field_name} contains unsupported characters.")
    return text


def generate_assignment_id(prefix: str = "acc_asn") -> str:
    """Generate a stable public assignment identifier."""
    safe_prefix = _normalize_required_text(
        prefix,
        field_name="assignment_id_prefix",
        max_length=24,
    )
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", safe_prefix).strip("-")
    if not safe_prefix:
        safe_prefix = "acc_asn"
    return f"{safe_prefix}_{uuid4().hex}"


def normalize_assignment_id(value: Any) -> str:
    """Normalize a public assignment identifier."""
    text = _normalize_required_text(
        value,
        field_name="assignment_id",
        max_length=ASSIGNMENT_ID_MAX_LENGTH,
    )
    if not _PUBLIC_ID_RE.fullmatch(text):
        raise ValueError("assignment_id must be a safe public identifier.")
    return text


def _short_fingerprint(value: Any, prefix: str = "sub") -> str:
    """Build a non-reversible short SHA-256 fingerprint."""
    if value is None:
        return ""
    try:
        text = str(value).strip()
    except Exception:
        return ""
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )
    except Exception:
        return "{}"


def make_json_safe(value: Any) -> Any:
    """Convert arbitrary values to JSON-safe structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return datetime_to_iso(value)
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            try:
                safe_key = str(key)
            except Exception:
                safe_key = "<unserializable-key>"
            result[safe_key] = make_json_safe(item)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [make_json_safe(item) for item in value]
    try:
        return str(value)
    except Exception:
        return "<unserializable-value>"


def sanitize_assignment_metadata(value: Any, *, depth: int = 0) -> Any:
    """Redact secrets, identities, URLs and large domain payloads from metadata."""
    if depth > 6:
        return "<redacted-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return datetime_to_iso(value)
    if isinstance(value, str):
        text = value
        if _EMAIL_RE.search(text):
            return "<redacted-email>"
        if _URL_RE.search(text):
            return "<redacted-url>"
        return text[:4096]
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            try:
                key = str(raw_key)
            except Exception:
                key = "<unserializable-key>"
            normalized_key = key.lower().replace("-", "_")
            if _SECRET_KEY_RE.search(normalized_key):
                result[key] = "<redacted-secret>"
                continue
            if _IDENTITY_KEY_RE.fullmatch(normalized_key):
                result[key] = "<redacted-identity>"
                continue
            if normalized_key in {
                "geometry",
                "coordinates",
                "chunks",
                "blocks",
                "world_state",
                "worldstate",
                "snapshot",
                "payload_raw",
            }:
                result[key] = "<redacted-domain-payload>"
                continue
            result[key] = sanitize_assignment_metadata(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_assignment_metadata(item, depth=depth + 1) for item in list(value)[:256]]
    return sanitize_assignment_metadata(make_json_safe(value), depth=depth + 1)


def normalize_metadata(value: Any) -> Dict[str, Any]:
    """Normalize metadata to a redacted JSON object."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("metadata_json must be a JSON object/dict.")
    sanitized = sanitize_assignment_metadata(dict(value))
    return dict(sanitized) if isinstance(sanitized, Mapping) else {}


def _reject_local_identity_fields(
    value: Any,
    *,
    path: tuple[str, ...] = (),
    depth: int = 0,
) -> None:
    """Reject local user/account/e-mail fields in inbound assignment payloads."""
    if depth > 5 or not isinstance(value, Mapping):
        return
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip().lower().replace("-", "_")
        current_path = path + (key,)
        parent = path[-1] if path else ""
        nested_generic_id = key == "id" and parent in {
            "user",
            "member",
            "membership",
            "app_user",
            "local_user",
            "account",
            "owner",
            "actor",
        }
        if (_LOCAL_ID_KEY_RE.fullmatch(key) or nested_generic_id) and key not in _CANONICAL_ID_KEYS:
            if raw_value not in (None, "", 0, False):
                raise ValueError(
                    "Local user, account or e-mail identity fields are not accepted "
                    f"for Chunk project access assignments: {'.'.join(current_path)}."
                )
        if isinstance(raw_value, Mapping):
            _reject_local_identity_fields(
                raw_value,
                path=current_path,
                depth=depth + 1,
            )


def _payload_first(payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload:
            return payload.get(key)
    return default


def get_assignment_normalization_cache_info() -> Dict[str, Any]:
    """Return diagnostics for pure normalization caches only."""
    return {
        "authUserId": _normalize_auth_user_id_cached.cache_info()._asdict(),
        "groupId": _normalize_group_id_cached.cache_info()._asdict(),
        "role": _normalize_role_cached.cache_info()._asdict(),
        "assignmentType": _normalize_assignment_type_cached.cache_info()._asdict(),
    }


def reset_assignment_normalization_caches() -> Dict[str, Any]:
    """Clear pure normalization caches without touching ORM/database state."""
    before = get_assignment_normalization_cache_info()
    _normalize_auth_user_id_cached.cache_clear()
    _normalize_group_id_cached.cache_clear()
    _normalize_role_cached.cache_clear()
    _normalize_assignment_type_cached.cache_clear()
    return {
        "cleared": True,
        "before": before,
        "after": get_assignment_normalization_cache_info(),
    }


# -----------------------------------------------------------------------------
# SQLAlchemy model
# -----------------------------------------------------------------------------


class ProjectAccessAssignment(db.Model):
    """One persisted direct-user or group assignment for a Chunk project."""

    __allow_unmapped__ = True
    __tablename__ = "project_access_assignments"

    id = db.Column(
        BIGINT_PRIMARY_KEY_TYPE,
        primary_key=True,
        autoincrement=True,
    )

    assignment_id = db.Column(
        db.String(ASSIGNMENT_ID_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
    )

    chunk_project_id = db.Column(
        db.String(CHUNK_PROJECT_ID_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    auth_user_id = db.Column(
        db.String(AUTH_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    group_id = db.Column(
        db.String(GROUP_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    role = db.Column(
        db.String(ROLE_MAX_LENGTH),
        nullable=False,
        index=True,
    )

    assignment_type = db.Column(
        db.String(ASSIGNMENT_TYPE_MAX_LENGTH),
        nullable=False,
        default=ASSIGNMENT_TYPE_DIRECT,
        index=True,
    )

    active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        index=True,
    )

    managed = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        index=True,
    )

    source_service = db.Column(
        db.String(SOURCE_SERVICE_MAX_LENGTH),
        nullable=False,
        default=DEFAULT_ASSIGNMENT_SOURCE_SERVICE,
        index=True,
    )

    projection_version = db.Column(
        db.String(PROJECTION_VERSION_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    projection_fingerprint = db.Column(
        db.String(FINGERPRINT_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    request_id = db.Column(
        db.String(REQUEST_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    correlation_id = db.Column(
        db.String(CORRELATION_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )

    metadata_json = db.Column(
        JSON_COLUMN_TYPE,
        nullable=False,
        default=dict,
    )

    schema_version = db.Column(
        db.String(SCHEMA_VERSION_MAX_LENGTH),
        nullable=False,
        default=PROJECT_ACCESS_ASSIGNMENT_SCHEMA_VERSION,
    )

    revision = db.Column(
        db.Integer,
        nullable=False,
        default=1,
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

    deactivated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # Exactly one active owner is verified transactionally by
    # project_access_service.py.  A partial unique owner index is deliberately
    # not used: SQLAlchemy owner-transfer updates can otherwise autoflush the
    # promotion before the demotion and fail even inside one valid transaction.
    _table_args: list[Any] = [
        db.UniqueConstraint(
            "chunk_project_id",
            "assignment_type",
            "auth_user_id",
            name="uq_project_access_direct_subject",
        ),
        db.UniqueConstraint(
            "chunk_project_id",
            "assignment_type",
            "group_id",
            name="uq_project_access_group_subject",
        ),
        db.CheckConstraint(
            "assignment_id <> ''",
            name="ck_project_access_assignment_id_not_empty",
        ),
        db.CheckConstraint(
            "chunk_project_id <> ''",
            name="ck_project_access_project_id_not_empty",
        ),
        db.CheckConstraint(
            "role IN ('owner', 'admin', 'editor', 'viewer')",
            name="ck_project_access_role_valid",
        ),
        db.CheckConstraint(
            "assignment_type IN ('direct', 'group')",
            name="ck_project_access_assignment_type_valid",
        ),
        db.CheckConstraint(
            "((assignment_type = 'direct' AND auth_user_id IS NOT NULL AND group_id IS NULL) OR "
            "(assignment_type = 'group' AND auth_user_id IS NULL AND group_id IS NOT NULL))",
            name="ck_project_access_subject_complete",
        ),
        db.CheckConstraint(
            "role <> 'owner' OR assignment_type = 'direct'",
            name="ck_project_access_owner_is_direct",
        ),
        db.CheckConstraint(
            "revision >= 1",
            name="ck_project_access_revision_positive",
        ),
        db.Index(
            "ix_project_access_project_active",
            "chunk_project_id",
            "active",
            "assignment_type",
        ),
        db.Index(
            "ix_project_access_project_role",
            "chunk_project_id",
            "role",
            "active",
        ),
        db.Index(
            "ix_project_access_source_project",
            "source_service",
            "chunk_project_id",
        ),
    ]

    __table_args__ = tuple(_table_args)
    del _table_args

    if synonym is not None:
        public_id = synonym("assignment_id")
        project_id = synonym("chunk_project_id")
        project_public_id = synonym("chunk_project_id")
        subject_auth_user_id = synonym("auth_user_id")
        principal_id = synonym("auth_user_id")
        subject_id = synonym("auth_user_id")
        project_role = synonym("role")
        access_role = synonym("role")
        subject_type = synonym("assignment_type")
        principal_type = synonym("assignment_type")
        is_active = synonym("active")
        enabled = synonym("active")
        is_managed = synonym("managed")
        direct_managed = synonym("managed")
        managed_by = synonym("source_service")
        source = synonym("source_service")
        subject_group_id = synonym("group_id")
        details = synonym("metadata_json")
        payload = synonym("metadata_json")

    def __repr__(self) -> str:
        return (
            f"<ProjectAccessAssignment id={self.id!r} assignment_id={self.assignment_id!r} "
            f"chunk_project_id={self.chunk_project_id!r} role={self.role!r} "
            f"assignment_type={self.assignment_type!r} "
            f"subject_fingerprint={self.subject_fingerprint!r} active={self.active!r}>"
        )

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def create_direct(
        cls,
        *,
        chunk_project_id: str,
        auth_user_id: str,
        role: str,
        assignment_id: Optional[str] = None,
        active: bool = True,
        managed: bool = True,
        source_service: str = DEFAULT_ASSIGNMENT_SOURCE_SERVICE,
        projection_version: Optional[str] = PROJECT_ACCESS_PROJECTION_VERSION,
        projection_fingerprint: Optional[str] = None,
        request_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "ProjectAccessAssignment":
        """Construct a direct canonical-user assignment without adding it to a session."""
        now = utc_now()
        return cls(
            assignment_id=normalize_assignment_id(
                assignment_id or generate_assignment_id()
            ),
            chunk_project_id=normalize_chunk_project_id(chunk_project_id),
            auth_user_id=normalize_auth_user_id(auth_user_id, required=True),
            group_id=None,
            role=normalize_role(role),
            assignment_type=ASSIGNMENT_TYPE_DIRECT,
            active=_normalize_bool(active, default=True),
            managed=_normalize_bool(managed, default=True),
            source_service=normalize_source_service(
                source_service,
                default=DEFAULT_ASSIGNMENT_SOURCE_SERVICE,
            ),
            projection_version=_normalize_optional_text(
                projection_version,
                field_name="projection_version",
                max_length=PROJECTION_VERSION_MAX_LENGTH,
            ),
            projection_fingerprint=normalize_optional_identifier(
                projection_fingerprint,
                field_name="projection_fingerprint",
                max_length=FINGERPRINT_MAX_LENGTH,
            ),
            request_id=normalize_optional_identifier(
                request_id,
                field_name="request_id",
                max_length=REQUEST_ID_MAX_LENGTH,
            ),
            correlation_id=normalize_optional_identifier(
                correlation_id,
                field_name="correlation_id",
                max_length=CORRELATION_ID_MAX_LENGTH,
            ),
            metadata_json=normalize_metadata(metadata_json),
            schema_version=PROJECT_ACCESS_ASSIGNMENT_SCHEMA_VERSION,
            revision=1,
            created_at=now,
            updated_at=now,
            deactivated_at=None if _normalize_bool(active, default=True) else now,
        )

    @classmethod
    def create_group(
        cls,
        *,
        chunk_project_id: str,
        group_id: str,
        role: str,
        assignment_id: Optional[str] = None,
        active: bool = True,
        managed: bool = False,
        source_service: str = DEFAULT_GROUP_SOURCE_SERVICE,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> "ProjectAccessAssignment":
        """Construct a group assignment preserved by direct-user reconciliation."""
        normalized_role = normalize_role(role, allow_owner=False)
        now = utc_now()
        return cls(
            assignment_id=normalize_assignment_id(
                assignment_id or generate_assignment_id("acc_grp")
            ),
            chunk_project_id=normalize_chunk_project_id(chunk_project_id),
            auth_user_id=None,
            group_id=normalize_group_id(group_id, required=True),
            role=normalized_role,
            assignment_type=ASSIGNMENT_TYPE_GROUP,
            active=_normalize_bool(active, default=True),
            managed=_normalize_bool(managed, default=False),
            source_service=normalize_source_service(
                source_service,
                default=DEFAULT_GROUP_SOURCE_SERVICE,
            ),
            projection_version=None,
            projection_fingerprint=None,
            request_id=None,
            correlation_id=None,
            metadata_json=normalize_metadata(metadata_json),
            schema_version=PROJECT_ACCESS_ASSIGNMENT_SCHEMA_VERSION,
            revision=1,
            created_at=now,
            updated_at=now,
            deactivated_at=None if _normalize_bool(active, default=True) else now,
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        trusted_source_service: Optional[str] = None,
        allow_group: bool = False,
    ) -> "ProjectAccessAssignment":
        """Construct an assignment from compatible camelCase/snake_case keys."""
        if not isinstance(payload, Mapping):
            raise ValueError("Project access assignment payload must be a JSON object.")
        _reject_local_identity_fields(payload)

        assignment_type = normalize_assignment_type(
            _payload_first(
                payload,
                "assignment_type",
                "assignmentType",
                "subject_type",
                "subjectType",
                default=ASSIGNMENT_TYPE_DIRECT,
            )
        )
        source_service = trusted_source_service or _payload_first(
            payload,
            "source_service",
            "sourceService",
            "managed_by",
            "managedBy",
            default=(
                DEFAULT_ASSIGNMENT_SOURCE_SERVICE
                if assignment_type == ASSIGNMENT_TYPE_DIRECT
                else DEFAULT_GROUP_SOURCE_SERVICE
            ),
        )
        common = {
            "chunk_project_id": _payload_first(
                payload,
                "chunk_project_id",
                "chunkProjectId",
                "project_public_id",
                "projectPublicId",
                "project_id",
                "projectId",
            ),
            "role": _payload_first(payload, "role", "project_role", "projectRole"),
            "assignment_id": _payload_first(
                payload,
                "assignment_id",
                "assignmentId",
                "public_id",
                "publicId",
            ),
            "active": _payload_first(payload, "active", "is_active", "enabled", default=True),
            "managed": _payload_first(
                payload,
                "managed",
                "is_managed",
                "direct_managed",
                default=assignment_type == ASSIGNMENT_TYPE_DIRECT,
            ),
            "source_service": source_service,
            "metadata_json": _payload_first(
                payload,
                "metadata_json",
                "metadataJson",
                "metadata",
                "details",
                default={},
            ),
        }
        if assignment_type == ASSIGNMENT_TYPE_GROUP:
            if not allow_group:
                raise ValueError(
                    "Group assignments are not accepted in the App direct-user projection."
                )
            return cls.create_group(
                group_id=_payload_first(
                    payload,
                    "group_id",
                    "groupId",
                    "subject_group_id",
                    "subjectGroupId",
                ),
                **common,
            )
        return cls.create_direct(
            auth_user_id=_payload_first(
                payload,
                "auth_user_id",
                "authUserId",
                "subject_auth_user_id",
                "subjectAuthUserId",
            ),
            projection_version=_payload_first(
                payload,
                "projection_version",
                "projectionVersion",
                default=PROJECT_ACCESS_PROJECTION_VERSION,
            ),
            projection_fingerprint=_payload_first(
                payload,
                "projection_fingerprint",
                "projectionFingerprint",
            ),
            request_id=_payload_first(payload, "request_id", "requestId"),
            correlation_id=_payload_first(
                payload,
                "correlation_id",
                "correlationId",
            ),
            **common,
        )

    # ------------------------------------------------------------------
    # Properties and mutation helpers
    # ------------------------------------------------------------------

    @property
    def public_key(self) -> str:
        return self.assignment_id

    @property
    def is_direct(self) -> bool:
        return self.assignment_type == ASSIGNMENT_TYPE_DIRECT

    @property
    def is_group(self) -> bool:
        return self.assignment_type == ASSIGNMENT_TYPE_GROUP

    @property
    def is_owner(self) -> bool:
        return bool(self.active and self.is_direct and self.role == ROLE_OWNER)

    @property
    def is_viewer(self) -> bool:
        return bool(self.active and self.role == ROLE_VIEWER)

    @property
    def subject_fingerprint(self) -> str:
        return _short_fingerprint(
            self.auth_user_id if self.is_direct else self.group_id,
            "sub",
        )

    @property
    def assignment_fingerprint(self) -> str:
        payload = {
            "chunk_project_id": self.chunk_project_id,
            "assignment_type": self.assignment_type,
            "subject": self.auth_user_id if self.is_direct else self.group_id,
            "role": self.role,
            "active": bool(self.active),
            "managed": bool(self.managed),
            "source_service": self.source_service,
        }
        return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()

    @property
    def subject_reference(self) -> Dict[str, Any]:
        """Return a private service-level subject reference."""
        if self.is_group:
            return {
                "type": ASSIGNMENT_TYPE_GROUP,
                "groupId": self.group_id,
            }
        return {
            "type": ASSIGNMENT_TYPE_DIRECT,
            "authUserId": self.auth_user_id,
        }

    def touch(self) -> None:
        self.updated_at = utc_now()
        self.revision = max(1, int(self.revision or 1)) + 1

    def set_role(self, role: str) -> None:
        """Update a role without changing the assignment subject."""
        normalized = normalize_role(role, allow_owner=self.is_direct)
        if normalized == self.role:
            return
        self.role = normalized
        self.touch()

    def activate(self) -> None:
        """Reactivate the assignment."""
        if self.active and self.deactivated_at is None:
            return
        self.active = True
        self.deactivated_at = None
        self.touch()

    def deactivate(self) -> None:
        """Deactivate the assignment without deleting audit history."""
        if not self.active and self.deactivated_at is not None:
            return
        self.active = False
        self.deactivated_at = self.deactivated_at or utc_now()
        self.touch()

    def apply_direct_projection(
        self,
        *,
        auth_user_id: str,
        role: str,
        source_service: str = DEFAULT_ASSIGNMENT_SOURCE_SERVICE,
        projection_version: Optional[str] = PROJECT_ACCESS_PROJECTION_VERSION,
        projection_fingerprint: Optional[str] = None,
        request_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Idempotently update a direct assignment from the App projection."""
        if not self.is_direct:
            raise ValueError("A group assignment cannot be converted into a direct assignment.")
        normalized_user = normalize_auth_user_id(auth_user_id, required=True)
        if self.auth_user_id and self.auth_user_id != normalized_user:
            raise ValueError("The subject of an existing assignment is immutable.")
        normalized_role = normalize_role(role)
        normalized_source = normalize_source_service(
            source_service,
            default=DEFAULT_ASSIGNMENT_SOURCE_SERVICE,
        )
        new_metadata = normalize_metadata(metadata_json)
        changed = False
        values = {
            "auth_user_id": normalized_user,
            "role": normalized_role,
            "source_service": normalized_source,
            "projection_version": _normalize_optional_text(
                projection_version,
                field_name="projection_version",
                max_length=PROJECTION_VERSION_MAX_LENGTH,
            ),
            "projection_fingerprint": normalize_optional_identifier(
                projection_fingerprint,
                field_name="projection_fingerprint",
                max_length=FINGERPRINT_MAX_LENGTH,
            ),
            "request_id": normalize_optional_identifier(
                request_id,
                field_name="request_id",
                max_length=REQUEST_ID_MAX_LENGTH,
            ),
            "correlation_id": normalize_optional_identifier(
                correlation_id,
                field_name="correlation_id",
                max_length=CORRELATION_ID_MAX_LENGTH,
            ),
            "metadata_json": new_metadata,
            "managed": True,
            "active": True,
            "deactivated_at": None,
        }
        for field_name, value in values.items():
            if getattr(self, field_name) != value:
                setattr(self, field_name, value)
                changed = True
        if changed:
            self.touch()

    def replace_metadata(self, metadata_json: Optional[Mapping[str, Any]]) -> None:
        normalized = normalize_metadata(metadata_json)
        if normalized == normalize_metadata(self.metadata_json):
            return
        self.metadata_json = normalized
        self.touch()

    def update_metadata(
        self,
        values: Mapping[str, Any],
        *,
        remove_keys: Optional[Iterable[str]] = None,
    ) -> None:
        if not isinstance(values, Mapping):
            raise ValueError("metadata update values must be a JSON object/dict.")
        current = normalize_metadata(self.metadata_json)
        for key in remove_keys or []:
            current.pop(str(key), None)
        for key, value in values.items():
            current[str(key)] = sanitize_assignment_metadata(value)
        current = normalize_metadata(current)
        if current == normalize_metadata(self.metadata_json):
            return
        self.metadata_json = current
        self.touch()

    # ------------------------------------------------------------------
    # Validation and serialization
    # ------------------------------------------------------------------

    def get_validation_errors(self) -> Dict[str, str]:
        """Return model validation errors without raising."""
        errors: Dict[str, str] = {}
        try:
            normalize_assignment_id(self.assignment_id)
        except Exception as exc:
            errors["assignmentId"] = str(exc)
        try:
            normalize_chunk_project_id(self.chunk_project_id)
        except Exception as exc:
            errors["chunkProjectId"] = str(exc)
        try:
            assignment_type = normalize_assignment_type(self.assignment_type)
        except Exception as exc:
            errors["assignmentType"] = str(exc)
            assignment_type = None
        try:
            role = normalize_role(self.role)
        except Exception as exc:
            errors["role"] = str(exc)
            role = None
        if assignment_type == ASSIGNMENT_TYPE_DIRECT:
            try:
                normalize_auth_user_id(self.auth_user_id, required=True)
            except Exception as exc:
                errors["authUserId"] = str(exc)
            if self.group_id not in (None, ""):
                errors["groupId"] = "Direct assignments must not contain group_id."
        elif assignment_type == ASSIGNMENT_TYPE_GROUP:
            try:
                normalize_group_id(self.group_id, required=True)
            except Exception as exc:
                errors["groupId"] = str(exc)
            if self.auth_user_id not in (None, ""):
                errors["authUserId"] = "Group assignments must not contain auth_user_id."
            if role == ROLE_OWNER:
                errors["role"] = "The owner role requires a direct user assignment."
        try:
            normalize_source_service(
                self.source_service,
                default=(
                    DEFAULT_ASSIGNMENT_SOURCE_SERVICE
                    if assignment_type == ASSIGNMENT_TYPE_DIRECT
                    else DEFAULT_GROUP_SOURCE_SERVICE
                ),
            )
        except Exception as exc:
            errors["sourceService"] = str(exc)
        try:
            normalize_metadata(self.metadata_json)
        except Exception as exc:
            errors["metadata"] = str(exc)
        try:
            if self.revision is None or int(self.revision) < 1:
                errors["revision"] = "revision must be greater than or equal to 1."
        except Exception as exc:
            errors["revision"] = f"revision must be an integer: {exc}"
        try:
            normalize_datetime(self.created_at, field_name="created_at")
        except Exception as exc:
            errors["createdAt"] = str(exc)
        try:
            normalize_datetime(self.updated_at, field_name="updated_at")
        except Exception as exc:
            errors["updatedAt"] = str(exc)
        if self.active and self.deactivated_at is not None:
            errors["deactivatedAt"] = "Active assignments must not have deactivated_at set."
        if not self.active and self.deactivated_at is None:
            errors["deactivatedAt"] = "Inactive assignments must have deactivated_at set."
        return errors

    def validate(self) -> None:
        errors = self.get_validation_errors()
        if errors:
            first_key = sorted(errors)[0]
            raise ValueError(f"Invalid project access assignment: {first_key}: {errors[first_key]}")

    def to_dict(
        self,
        *,
        include_private: bool = False,
        include_internal: bool = False,
        include_metadata: bool = False,
    ) -> Dict[str, Any]:
        """Serialize the assignment; raw subject ids require ``include_private``."""
        result: Dict[str, Any] = {
            "assignmentId": self.assignment_id,
            "chunkProjectId": self.chunk_project_id,
            "projectId": self.chunk_project_id,
            "role": self.role,
            "assignmentType": self.assignment_type,
            "active": bool(self.active),
            "managed": bool(self.managed),
            "sourceService": self.source_service,
            "schemaVersion": self.schema_version,
            "revision": self.revision,
            "subjectFingerprint": self.subject_fingerprint or None,
            "assignmentFingerprint": self.assignment_fingerprint,
            "owner": self.is_owner,
            "viewer": self.is_viewer,
            "projectionVersion": self.projection_version,
            "projectionFingerprint": self.projection_fingerprint,
            "requestId": self.request_id,
            "correlationId": self.correlation_id,
            "createdAt": datetime_to_iso(self.created_at),
            "updatedAt": datetime_to_iso(self.updated_at),
            "deactivatedAt": datetime_to_iso(self.deactivated_at),
        }
        if include_private:
            result["authUserId"] = self.auth_user_id if self.is_direct else None
            result["auth_user_id"] = self.auth_user_id if self.is_direct else None
            result["groupId"] = self.group_id if self.is_group else None
            result["group_id"] = self.group_id if self.is_group else None
            result["subject"] = make_json_safe(self.subject_reference)
        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)
        if include_internal:
            result["id"] = self.id
        return result

    def to_public_dict(self) -> Dict[str, Any]:
        """Serialize without local database or raw subject identifiers."""
        return self.to_dict(
            include_private=False,
            include_internal=False,
            include_metadata=False,
        )

    def to_service_dict(self) -> Dict[str, Any]:
        """Serialize for trusted internal service/repository use."""
        return self.to_dict(
            include_private=True,
            include_internal=False,
            include_metadata=True,
        )


# -----------------------------------------------------------------------------
# SQLAlchemy normalization hooks
# -----------------------------------------------------------------------------


def _normalize_model_state(target: ProjectAccessAssignment) -> None:
    """Normalize and validate one ORM instance before insert/update."""
    target.assignment_id = normalize_assignment_id(
        target.assignment_id or generate_assignment_id()
    )
    target.chunk_project_id = normalize_chunk_project_id(target.chunk_project_id)
    target.assignment_type = normalize_assignment_type(target.assignment_type)
    target.role = normalize_role(
        target.role,
        allow_owner=target.assignment_type == ASSIGNMENT_TYPE_DIRECT,
    )
    target.active = _normalize_bool(target.active, default=True)
    target.managed = _normalize_bool(
        target.managed,
        default=target.assignment_type == ASSIGNMENT_TYPE_DIRECT,
    )

    if target.assignment_type == ASSIGNMENT_TYPE_DIRECT:
        target.auth_user_id = normalize_auth_user_id(target.auth_user_id, required=True)
        target.group_id = None
        target.source_service = normalize_source_service(
            target.source_service,
            default=DEFAULT_ASSIGNMENT_SOURCE_SERVICE,
        )
    else:
        target.group_id = normalize_group_id(target.group_id, required=True)
        target.auth_user_id = None
        if target.role == ROLE_OWNER:
            raise ValueError("The owner role requires a direct user assignment.")
        target.source_service = normalize_source_service(
            target.source_service,
            default=DEFAULT_GROUP_SOURCE_SERVICE,
        )

    target.projection_version = _normalize_optional_text(
        target.projection_version,
        field_name="projection_version",
        max_length=PROJECTION_VERSION_MAX_LENGTH,
    )
    target.projection_fingerprint = normalize_optional_identifier(
        target.projection_fingerprint,
        field_name="projection_fingerprint",
        max_length=FINGERPRINT_MAX_LENGTH,
    )
    target.request_id = normalize_optional_identifier(
        target.request_id,
        field_name="request_id",
        max_length=REQUEST_ID_MAX_LENGTH,
    )
    target.correlation_id = normalize_optional_identifier(
        target.correlation_id,
        field_name="correlation_id",
        max_length=CORRELATION_ID_MAX_LENGTH,
    )
    target.metadata_json = normalize_metadata(target.metadata_json)
    target.schema_version = PROJECT_ACCESS_ASSIGNMENT_SCHEMA_VERSION
    try:
        target.revision = max(1, int(target.revision or 1))
    except Exception as exc:
        raise ValueError("revision must be an integer greater than or equal to 1.") from exc

    target.created_at = normalize_datetime(
        target.created_at or utc_now(),
        field_name="created_at",
    )
    target.updated_at = normalize_datetime(
        target.updated_at or utc_now(),
        field_name="updated_at",
    )
    if target.active:
        target.deactivated_at = None
    else:
        target.deactivated_at = normalize_datetime(
            target.deactivated_at or utc_now(),
            field_name="deactivated_at",
        )
    target.validate()


def _normalize_assignment_before_write(
    _mapper: Any,
    _connection: Any,
    target: ProjectAccessAssignment,
) -> None:
    _normalize_model_state(target)


def _install_project_access_assignment_listeners() -> bool:
    """Install normalization listeners idempotently."""
    if sqlalchemy_event is None:
        return False
    try:
        for event_name in ("before_insert", "before_update"):
            if not sqlalchemy_event.contains(
                ProjectAccessAssignment,
                event_name,
                _normalize_assignment_before_write,
            ):
                sqlalchemy_event.listen(
                    ProjectAccessAssignment,
                    event_name,
                    _normalize_assignment_before_write,
                    propagate=True,
                )
        return True
    except Exception:  # pragma: no cover - mapper/bootstrap integration issue
        return False


PROJECT_ACCESS_ASSIGNMENT_MODEL_LISTENERS_INSTALLED = (
    _install_project_access_assignment_listeners()
)

AccessAssignmentModel = ProjectAccessAssignment


__all__ = [
    "PROJECT_ACCESS_ASSIGNMENT_SCHEMA_VERSION",
    "PROJECT_ACCESS_PROJECTION_VERSION",
    "ROLE_OWNER",
    "ROLE_ADMIN",
    "ROLE_EDITOR",
    "ROLE_VIEWER",
    "ACCESS_ROLES",
    "ROLE_RANK",
    "ASSIGNMENT_TYPE_DIRECT",
    "ASSIGNMENT_TYPE_GROUP",
    "ASSIGNMENT_TYPES",
    "DEFAULT_ASSIGNMENT_SOURCE_SERVICE",
    "DEFAULT_GROUP_SOURCE_SERVICE",
    "ProjectAccessAssignment",
    "AccessAssignmentModel",
    "utc_now",
    "datetime_to_iso",
    "normalize_datetime",
    "normalize_chunk_project_id",
    "normalize_auth_user_id",
    "normalize_group_id",
    "normalize_role",
    "normalize_assignment_type",
    "normalize_source_service",
    "normalize_assignment_id",
    "generate_assignment_id",
    "make_json_safe",
    "sanitize_assignment_metadata",
    "normalize_metadata",
    "get_assignment_normalization_cache_info",
    "reset_assignment_normalization_caches",
    "PROJECT_ACCESS_ASSIGNMENT_MODEL_LISTENERS_INSTALLED",
]
