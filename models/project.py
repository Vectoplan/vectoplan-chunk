# services/vectoplan-chunk/models/project.py
"""
SQLAlchemy model for VECTOPLAN Chunk projects.

A ``Project`` is the top-level persistent container owned by
``vectoplan-chunk``.  ``vectoplan-app`` remains the source of truth for the App
project, membership and project roles.  The services are linked only by stable
public identifiers; no cross-service database foreign keys are created.

Hierarchy::

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Security and lifecycle rules:

* ``id`` is the local database primary key.
* ``project_id`` is the stable Chunk public/API identifier.
* ``external_app_project_id`` is the App project public identifier.
* canonical cross-service users are stored only as ``auth_user_id`` values.
* numeric local App user ids and email addresses are never accepted as users.
* project provisioning never silently changes an existing world template.
* Earth -> Flat is represented explicitly as a controlled fallback state.
* App membership remains authoritative; Chunk stores only its access projection.
* Viewer/public access is read-only and is enforced by the access service.
* this model performs no queries, commits, rollbacks or remote calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Final, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit
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
        "models/project.py. Ensure extensions.py exposes a Flask-SQLAlchemy "
        "`db` instance before importing models."
    ) from _DB_IMPORT_ERROR


try:
    from sqlalchemy import event as sqlalchemy_event
    from sqlalchemy.orm import synonym
except Exception:  # pragma: no cover
    sqlalchemy_event = None  # type: ignore[assignment]
    synonym = None  # type: ignore[assignment]

try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - useful for sqlite/non-postgres tooling
    JSONB = None  # type: ignore[assignment]


try:
    JSON_COLUMN_TYPE = (
        JSONB()
        .with_variant(db.JSON(), "sqlite")
        .with_variant(db.JSON(), "mysql")
    ) if JSONB is not None else db.JSON
except Exception:  # pragma: no cover
    JSON_COLUMN_TYPE = db.JSON


# -----------------------------------------------------------------------------
# Schema and identity constants
# -----------------------------------------------------------------------------

PROJECT_SCHEMA_VERSION = "project.schema.v3"

# Development/bootstrap identity only.  Generic project creation does not fall
# back to this value.  It is deliberately non-numeric and cannot be confused
# with a local AppUser primary key.
DEV_PROJECT_OWNER_AUTH_USER_ID: Final[str] = "auth_dev_owner"
DEFAULT_PROJECT_OWNER_USER_ID: Final[str] = DEV_PROJECT_OWNER_AUTH_USER_ID

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

PROVISIONING_STATUS_DISABLED = "disabled"
PROVISIONING_STATUS_PENDING = "pending"
PROVISIONING_STATUS_PROVISIONING = "provisioning"
PROVISIONING_STATUS_READY = "ready"
PROVISIONING_STATUS_FALLBACK_READY = "fallback_ready"
PROVISIONING_STATUS_FAILED = "failed"
PROVISIONING_STATUS_REPAIR_REQUIRED = "repair_required"
VALID_PROVISIONING_STATUSES = frozenset(
    {
        PROVISIONING_STATUS_DISABLED,
        PROVISIONING_STATUS_PENDING,
        PROVISIONING_STATUS_PROVISIONING,
        PROVISIONING_STATUS_READY,
        PROVISIONING_STATUS_FALLBACK_READY,
        PROVISIONING_STATUS_FAILED,
        PROVISIONING_STATUS_REPAIR_REQUIRED,
    }
)

ACCESS_SYNC_STATUS_DISABLED = "disabled"
ACCESS_SYNC_STATUS_PENDING = "pending"
ACCESS_SYNC_STATUS_SYNCING = "syncing"
ACCESS_SYNC_STATUS_READY = "ready"
ACCESS_SYNC_STATUS_FAILED = "failed"
ACCESS_SYNC_STATUS_REPAIR_REQUIRED = "repair_required"
VALID_ACCESS_SYNC_STATUSES = frozenset(
    {
        ACCESS_SYNC_STATUS_DISABLED,
        ACCESS_SYNC_STATUS_PENDING,
        ACCESS_SYNC_STATUS_SYNCING,
        ACCESS_SYNC_STATUS_READY,
        ACCESS_SYNC_STATUS_FAILED,
        ACCESS_SYNC_STATUS_REPAIR_REQUIRED,
    }
)

WORLD_TEMPLATE_EARTH = "earth"
WORLD_TEMPLATE_FLAT = "flat"
VALID_WORLD_TEMPLATES = frozenset({WORLD_TEMPLATE_EARTH, WORLD_TEMPLATE_FLAT})

PROJECT_ACCESS_PROJECTION_VERSION = "app-project-access-v1"

PROJECT_ID_MAX_LENGTH = 120
PROJECT_SLUG_MAX_LENGTH = 120
PROJECT_NAME_MAX_LENGTH = 255
PROJECT_OWNER_TYPE_MAX_LENGTH = 64
PROJECT_OWNER_ID_MAX_LENGTH = 255
PROJECT_USER_ID_MAX_LENGTH = 255
PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH = 120
PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH = 120
PROJECT_DESCRIPTION_MAX_LENGTH = 4096
PROJECT_EXTERNAL_APP_PROJECT_ID_MAX_LENGTH = 160
PROJECT_SOURCE_SERVICE_MAX_LENGTH = 120
PROJECT_EXTERNAL_URL_MAX_LENGTH = 512
PROJECT_STATUS_MAX_LENGTH = 40
PROJECT_FINGERPRINT_MAX_LENGTH = 128
PROJECT_REQUEST_ID_MAX_LENGTH = 160
PROJECT_ERROR_CODE_MAX_LENGTH = 160
PROJECT_TEMPLATE_ID_MAX_LENGTH = 64
PROJECT_PROJECTION_VERSION_MAX_LENGTH = 120

PROJECT_METADATA_MAX_DEPTH = 8
PROJECT_METADATA_MAX_KEYS = 256
PROJECT_METADATA_MAX_LIST_ITEMS = 256
PROJECT_METADATA_MAX_TEXT_LENGTH = 8192
PROJECT_METADATA_MAX_BYTES = 128 * 1024

PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
OWNER_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]*$")
AUTH_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,254}$")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
WHITESPACE_PATTERN = re.compile(r"\s")
EMAIL_LIKE_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_SENSITIVE_METADATA_KEY_PARTS: Final[tuple[str, ...]] = (
    "authorization",
    "bearer",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "cookie",
    "session",
    "csrf",
    "private_key",
    "client_secret",
    "email",
    "auth_user_id",
    "authuserid",
    "owner_user_id",
    "owneruserid",
    "created_by_user_id",
    "updated_by_user_id",
    "account_id",
    "local_user_id",
    "user_id",
    "userid",
    "owner_id",
    "created_by",
    "updated_by",
    "internal_url",
    "service_url",
    "database_url",
)

_BULK_METADATA_KEY_PARTS: Final[tuple[str, ...]] = (
    "geometry",
    "geometries",
    "chunks",
    "chunk_data",
    "blocks",
    "block_data",
    "world_state",
    "snapshot_data",
    "binary",
    "blob",
)


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


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


def normalize_datetime(
    value: Any,
    *,
    field_name: str,
    required: bool = False,
) -> Optional[datetime]:
    """Normalize datetime objects or ISO-8601 strings to aware UTC values."""
    if value is None or value == "":
        if required:
            raise ValueError(f"{field_name} is required.")
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            result = datetime.fromisoformat(text)
        except Exception as exc:
            raise ValueError(f"{field_name} must be a datetime or ISO-8601 string.") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _safe_bool(value: Any, default: bool = False) -> bool:
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
    if text in {"1", "true", "yes", "y", "on", "enabled", "active", "ready"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "inactive", "failed", ""}:
        return False
    return default


def _short_fingerprint(value: Any, prefix: str = "ref") -> str:
    try:
        text = str(value or "").strip()
    except Exception:
        text = ""
    if not text:
        return ""
    return f"{prefix}_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def _stable_json(value: Any) -> str:
    return json.dumps(
        make_json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def build_project_state_fingerprint(value: Any) -> str:
    """Build a deterministic SHA-256 fingerprint for safe state objects."""
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def make_json_safe(value: Any) -> Any:
    """Convert arbitrary values to JSON-safe structures."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
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
    if CONTROL_CHARACTER_PATTERN.search(text):
        raise ValueError(f"{field_name} must not contain control characters.")
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
    """Normalize stable service/API identifiers."""
    text = normalize_required_text(
        value,
        field_name=field_name,
        max_length=max_length,
    )
    if not PUBLIC_ID_PATTERN.fullmatch(text):
        raise ValueError(
            f"{field_name} may only contain letters, numbers, underscores, "
            "dashes, dots and colons, and must start with a letter or number."
        )
    return text


def normalize_project_id(value: Any) -> str:
    """Normalize a public Chunk project id."""
    return normalize_public_id(
        value,
        field_name="project_id",
        max_length=PROJECT_ID_MAX_LENGTH,
    )


def normalize_external_app_project_id(value: Any) -> Optional[str]:
    """Normalize an optional vectoplan-app project public id."""
    text = normalize_optional_text(
        value,
        field_name="external_app_project_id",
        max_length=PROJECT_EXTERNAL_APP_PROJECT_ID_MAX_LENGTH,
    )
    if text is None:
        return None
    if not PUBLIC_ID_PATTERN.fullmatch(text):
        raise ValueError(
            "external_app_project_id may only contain letters, numbers, "
            "underscores, dashes, dots and colons, and must start with an "
            "alphanumeric character."
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
    if not SLUG_PATTERN.fullmatch(text):
        raise ValueError(
            "slug may only contain letters, numbers, underscores and dashes, "
            "and must start with an alphanumeric character."
        )
    return text


def normalize_status(value: Any) -> str:
    """Normalize and validate project lifecycle status."""
    text = str(value or PROJECT_STATUS_ACTIVE).strip().lower()
    if text not in VALID_PROJECT_STATUSES:
        allowed = ", ".join(sorted(VALID_PROJECT_STATUSES))
        raise ValueError(f"Invalid project status '{value}'. Allowed: {allowed}.")
    return text


def normalize_provisioning_status(value: Any) -> str:
    """Normalize project provisioning status."""
    text = str(value or PROVISIONING_STATUS_PENDING).strip().lower().replace("-", "_")
    aliases = {
        "ok": PROVISIONING_STATUS_READY,
        "complete": PROVISIONING_STATUS_READY,
        "completed": PROVISIONING_STATUS_READY,
        "fallback": PROVISIONING_STATUS_FALLBACK_READY,
        "error": PROVISIONING_STATUS_FAILED,
        "repair": PROVISIONING_STATUS_REPAIR_REQUIRED,
    }
    text = aliases.get(text, text)
    if text not in VALID_PROVISIONING_STATUSES:
        allowed = ", ".join(sorted(VALID_PROVISIONING_STATUSES))
        raise ValueError(f"Invalid provisioning status '{value}'. Allowed: {allowed}.")
    return text


def normalize_access_sync_status(value: Any) -> str:
    """Normalize project access-projection status."""
    text = str(value or ACCESS_SYNC_STATUS_PENDING).strip().lower().replace("-", "_")
    aliases = {
        "ok": ACCESS_SYNC_STATUS_READY,
        "complete": ACCESS_SYNC_STATUS_READY,
        "completed": ACCESS_SYNC_STATUS_READY,
        "processing": ACCESS_SYNC_STATUS_SYNCING,
        "error": ACCESS_SYNC_STATUS_FAILED,
        "repair": ACCESS_SYNC_STATUS_REPAIR_REQUIRED,
    }
    text = aliases.get(text, text)
    if text not in VALID_ACCESS_SYNC_STATUSES:
        allowed = ", ".join(sorted(VALID_ACCESS_SYNC_STATUSES))
        raise ValueError(f"Invalid access sync status '{value}'. Allowed: {allowed}.")
    return text


def normalize_world_template(value: Any, *, default: str = WORLD_TEMPLATE_EARTH) -> str:
    """Normalize a supported world-template id."""
    text = str(value or default).strip().lower().replace("-", "_")
    aliases = {
        "earth_world": WORLD_TEMPLATE_EARTH,
        "globe": WORLD_TEMPLATE_EARTH,
        "georeferenced": WORLD_TEMPLATE_EARTH,
        "flat_world": WORLD_TEMPLATE_FLAT,
        "local": WORLD_TEMPLATE_FLAT,
    }
    text = aliases.get(text, text)
    if text not in VALID_WORLD_TEMPLATES:
        raise ValueError(
            f"Invalid world template '{value}'. Allowed: {', '.join(sorted(VALID_WORLD_TEMPLATES))}."
        )
    return text


def normalize_request_identifier(value: Any, *, field_name: str) -> Optional[str]:
    """Normalize request/correlation identifiers without accepting control data."""
    text = normalize_optional_text(
        value,
        field_name=field_name,
        max_length=PROJECT_REQUEST_ID_MAX_LENGTH,
    )
    if text is None:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text).strip("_.:-")
    return cleaned[:PROJECT_REQUEST_ID_MAX_LENGTH] or None


def normalize_error_code(value: Any) -> Optional[str]:
    text = normalize_optional_text(
        value,
        field_name="error_code",
        max_length=PROJECT_ERROR_CODE_MAX_LENGTH,
    )
    if text is None:
        return None
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", text).strip("_.:-").lower()
    return cleaned[:PROJECT_ERROR_CODE_MAX_LENGTH] or None


def normalize_external_url(value: Any) -> Optional[str]:
    """Normalize an optional HTTP(S) service reference URL.

    The value is private/internal model data and is never included in public
    serialization.  Credentials and URL fragments are rejected.
    """
    text = normalize_optional_text(
        value,
        field_name="external_url",
        max_length=PROJECT_EXTERNAL_URL_MAX_LENGTH,
    )
    if text is None:
        return None
    try:
        parts = urlsplit(text)
    except Exception as exc:
        raise ValueError("external_url must be a valid URL.") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("external_url must be an absolute http(s) URL.")
    if parts.username or parts.password:
        raise ValueError("external_url must not contain credentials.")
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "", parts.query or "", ""))


@lru_cache(maxsize=256)
def _normalize_owner_type_cached(text: str) -> str:
    normalized = text.strip().lower()
    if not normalized:
        raise ValueError("owner_type is required when owner identity is present.")
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
        raise ValueError(f"Invalid owner_type '{normalized}'. Allowed: {allowed}.")
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


@lru_cache(maxsize=4096)
def _normalize_auth_user_id_cached(text: str, field_name: str) -> str:
    normalized = text.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required.")
    if len(normalized) > PROJECT_USER_ID_MAX_LENGTH:
        raise ValueError(
            f"{field_name} must not exceed {PROJECT_USER_ID_MAX_LENGTH} characters."
        )
    if CONTROL_CHARACTER_PATTERN.search(normalized) or WHITESPACE_PATTERN.search(normalized):
        raise ValueError(f"{field_name} must not contain whitespace or control characters.")
    if EMAIL_LIKE_PATTERN.fullmatch(normalized) or "@" in normalized:
        raise ValueError(f"{field_name} must be a canonical auth id, not an email address.")
    if normalized.isdigit():
        raise ValueError(
            f"{field_name} must be a canonical auth id, not a local numeric user id."
        )
    if normalized.lower() in {"none", "null", "undefined", "anonymous", "guest"}:
        raise ValueError(f"{field_name} is not a canonical authenticated user id.")
    if not AUTH_USER_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} may only contain letters, numbers, underscores, "
            "dashes, dots and colons."
        )
    return normalized


def normalize_auth_user_id(
    value: Any,
    *,
    field_name: str = "auth_user_id",
    required: bool = False,
) -> Optional[str]:
    """Normalize a canonical cross-service auth user id.

    Local AppUser primary keys, emails and anonymous identities are rejected.
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
    return _normalize_auth_user_id_cached(text, field_name)


def normalize_external_user_id(
    value: Any,
    *,
    field_name: str,
    required: bool = False,
) -> Optional[str]:
    """Backward-compatible alias for canonical ``auth_user_id`` validation."""
    return normalize_auth_user_id(
        value,
        field_name=field_name,
        required=required,
    )


def normalize_owner_pair(
    *,
    owner_type: Any = None,
    owner_id: Any = None,
    owner_user_id: Any = None,
    owner_auth_user_id: Any = None,
    required: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """Normalize the atomic owner pair.

    ``owner_auth_user_id`` is canonical.  ``owner_user_id`` and ``owner_id`` are
    retained as compatibility aliases and must resolve to the same canonical id.
    """
    candidates = []
    for field_name, candidate in (
        ("owner_auth_user_id", owner_auth_user_id),
        ("owner_user_id", owner_user_id),
        ("owner_id", owner_id),
    ):
        normalized = normalize_auth_user_id(
            candidate,
            field_name=field_name,
            required=False,
        )
        if normalized is not None:
            candidates.append((field_name, normalized))

    unique_values = {value for _field, value in candidates}
    if len(unique_values) > 1:
        raise ValueError(
            "owner_auth_user_id, owner_user_id and owner_id refer to different users."
        )

    normalized_owner_id = next(iter(unique_values), None)
    normalized_owner_type = normalize_owner_type(owner_type)

    if normalized_owner_id is not None:
        if normalized_owner_type is not None and normalized_owner_type != PROJECT_OWNER_TYPE_USER:
            raise ValueError("Canonical user ownership requires owner_type='user'.")
        normalized_owner_type = PROJECT_OWNER_TYPE_USER

    if (normalized_owner_type is None) != (normalized_owner_id is None):
        raise ValueError(
            "owner_type and owner_auth_user_id must either both be set or both be empty."
        )
    if required and normalized_owner_id is None:
        raise ValueError("owner_auth_user_id is required for a project.")
    return normalized_owner_type, normalized_owner_id


def get_project_normalization_cache_info() -> Dict[str, Any]:
    """Return diagnostics for pure normalization caches only."""
    return {
        "ownerType": _normalize_owner_type_cached.cache_info()._asdict(),
        "externalUserId": _normalize_auth_user_id_cached.cache_info()._asdict(),
        "authUserId": _normalize_auth_user_id_cached.cache_info()._asdict(),
    }


def reset_project_normalization_caches() -> Dict[str, Any]:
    """Clear pure normalization caches; no ORM/database state is cached here."""
    before = get_project_normalization_cache_info()
    _normalize_owner_type_cached.cache_clear()
    _normalize_auth_user_id_cached.cache_clear()
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
    for key in keys:
        if key in payload:
            return payload.get(key)
    return default


def _payload_present_key(payload: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        if key in payload:
            return key
    return None


def _normalize_metadata_key(key: Any) -> str:
    try:
        return str(key).strip()[:160]
    except Exception:
        return ""


def _metadata_key_is_sensitive(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in _SENSITIVE_METADATA_KEY_PARTS)


def _metadata_key_is_bulk(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in _BULK_METADATA_KEY_PARTS)


def sanitize_project_metadata(
    value: Any,
    *,
    _depth: int = 0,
) -> Any:
    """Recursively sanitize project metadata before persistence.

    Identity, credential and bulk world/chunk data belong in dedicated models or
    fields and are removed from the generic metadata object.
    """
    if _depth > PROJECT_METADATA_MAX_DEPTH:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, datetime):
        return datetime_to_iso(value)
    if isinstance(value, str):
        text = value[:PROJECT_METADATA_MAX_TEXT_LENGTH]
        if EMAIL_LIKE_PATTERN.fullmatch(text.strip()):
            return "<redacted-email>"
        return text
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= PROJECT_METADATA_MAX_KEYS:
                result["_truncated"] = True
                break
            clean_key = _normalize_metadata_key(key)
            if not clean_key:
                continue
            if _metadata_key_is_sensitive(clean_key):
                result[clean_key] = "<redacted>"
                continue
            if _metadata_key_is_bulk(clean_key):
                result[clean_key] = "<omitted-bulk-data>"
                continue
            result[clean_key] = sanitize_project_metadata(item, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        result = [
            sanitize_project_metadata(item, _depth=_depth + 1)
            for item in items[:PROJECT_METADATA_MAX_LIST_ITEMS]
        ]
        if len(items) > PROJECT_METADATA_MAX_LIST_ITEMS:
            result.append("<truncated>")
        return result
    try:
        return sanitize_project_metadata(str(value), _depth=_depth + 1)
    except Exception:
        return "<unserializable-value>"


def normalize_metadata(value: Any) -> Dict[str, Any]:
    """Normalize metadata into a bounded, redacted JSON object."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("metadata_json must be a JSON object/dict.")
    result = sanitize_project_metadata(dict(value))
    if not isinstance(result, dict):
        result = {}
    encoded = _stable_json(result).encode("utf-8")
    if len(encoded) > PROJECT_METADATA_MAX_BYTES:
        raise ValueError(
            f"metadata_json must not exceed {PROJECT_METADATA_MAX_BYTES} bytes after sanitization."
        )
    return result


def generate_project_id(prefix: str = "proj") -> str:
    """Generate a stable public Chunk project identifier."""
    normalized_prefix = normalize_public_id(
        prefix,
        field_name="project_id_prefix",
        max_length=24,
    )
    return f"{normalized_prefix}_{uuid4().hex}"


def _merge_metadata(
    base: Optional[Mapping[str, Any]],
    update: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    result = normalize_metadata(base)
    if update is None:
        return result
    if not isinstance(update, Mapping):
        raise ValueError("metadata update must be a JSON object/dict.")
    merged: Dict[str, Any] = dict(result)
    for key, value in update.items():
        merged[str(key)] = value
    return normalize_metadata(merged)


def _payload_metadata_value(payload: Mapping[str, Any]) -> Any:
    for key in (
        "metadataJson",
        "metadata_json",
        "metadata",
        "projectMetadata",
        "project_metadata",
    ):
        if key in payload:
            return payload.get(key)
    return None


# -----------------------------------------------------------------------------
# Project model
# -----------------------------------------------------------------------------


class Project(db.Model):
    """Persistent Chunk project with explicit provisioning/access state."""

    __tablename__ = "projects"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)

    project_id = db.Column(
        db.String(PROJECT_ID_MAX_LENGTH),
        nullable=False,
        unique=True,
        index=True,
    )
    slug = db.Column(db.String(PROJECT_SLUG_MAX_LENGTH), nullable=True, index=True)
    name = db.Column(db.String(PROJECT_NAME_MAX_LENGTH), nullable=False)
    description = db.Column(db.String(PROJECT_DESCRIPTION_MAX_LENGTH), nullable=True)

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
    revision = db.Column(db.Integer, nullable=False, default=1)

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
    external_url = db.Column(db.String(PROJECT_EXTERNAL_URL_MAX_LENGTH), nullable=True)

    # Canonical owner identity.  Legacy pair columns remain synchronized for
    # transition compatibility but never hold local AppUser primary keys.
    owner_auth_user_id = db.Column(
        db.String(PROJECT_OWNER_ID_MAX_LENGTH),
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

    created_by_auth_user_id = db.Column(
        db.String(PROJECT_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    updated_by_auth_user_id = db.Column(
        db.String(PROJECT_USER_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    # Compatibility columns: values are canonical auth ids, never local ids.
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

    world_template_requested = db.Column(
        db.String(PROJECT_TEMPLATE_ID_MAX_LENGTH),
        nullable=False,
        default=WORLD_TEMPLATE_EARTH,
        index=True,
    )
    world_template_effective = db.Column(
        db.String(PROJECT_TEMPLATE_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    world_fallback_used = db.Column(db.Boolean, nullable=False, default=False)
    world_fallback_code = db.Column(
        db.String(PROJECT_ERROR_CODE_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    earth_reference_fingerprint = db.Column(
        db.String(PROJECT_FINGERPRINT_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    world_metadata_json = db.Column(JSON_COLUMN_TYPE, nullable=False, default=dict)

    provisioning_status = db.Column(
        db.String(PROJECT_STATUS_MAX_LENGTH),
        nullable=False,
        default=PROVISIONING_STATUS_PENDING,
        index=True,
    )
    provisioning_fingerprint = db.Column(
        db.String(PROJECT_FINGERPRINT_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    provisioning_request_id = db.Column(
        db.String(PROJECT_REQUEST_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    provisioning_correlation_id = db.Column(
        db.String(PROJECT_REQUEST_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    provisioning_error_code = db.Column(
        db.String(PROJECT_ERROR_CODE_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    provisioning_retryable = db.Column(db.Boolean, nullable=False, default=False)
    provisioning_repair_required = db.Column(db.Boolean, nullable=False, default=False)
    provisioning_attempts = db.Column(db.Integer, nullable=False, default=0)
    provisioned_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    provisioning_updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    access_sync_status = db.Column(
        db.String(PROJECT_STATUS_MAX_LENGTH),
        nullable=False,
        default=ACCESS_SYNC_STATUS_PENDING,
        index=True,
    )
    access_projection_version = db.Column(
        db.String(PROJECT_PROJECTION_VERSION_MAX_LENGTH),
        nullable=False,
        default=PROJECT_ACCESS_PROJECTION_VERSION,
    )
    access_projection_fingerprint = db.Column(
        db.String(PROJECT_FINGERPRINT_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    access_sync_request_id = db.Column(
        db.String(PROJECT_REQUEST_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    access_sync_correlation_id = db.Column(
        db.String(PROJECT_REQUEST_ID_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    access_sync_error_code = db.Column(
        db.String(PROJECT_ERROR_CODE_MAX_LENGTH),
        nullable=True,
        index=True,
    )
    access_sync_retryable = db.Column(db.Boolean, nullable=False, default=False)
    access_sync_repair_required = db.Column(db.Boolean, nullable=False, default=False)
    access_sync_attempts = db.Column(db.Integer, nullable=False, default=0)
    access_synced_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    access_sync_updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    metadata_json = db.Column(JSON_COLUMN_TYPE, nullable=False, default=dict)

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
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        db.UniqueConstraint("slug", name="uq_projects_slug"),
        db.UniqueConstraint(
            "external_app_project_id",
            name="uq_projects_external_app_project_id",
        ),
        db.CheckConstraint("project_id <> ''", name="ck_projects_project_id_not_empty"),
        db.CheckConstraint("name <> ''", name="ck_projects_name_not_empty"),
        db.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="ck_projects_status_valid",
        ),
        db.CheckConstraint("revision >= 1", name="ck_projects_revision_positive"),
        db.CheckConstraint(
            "owner_type IS NULL OR owner_type = 'user'",
            name="ck_projects_owner_type_valid",
        ),
        db.CheckConstraint(
            "(owner_type IS NULL AND owner_id IS NULL AND owner_auth_user_id IS NULL) OR "
            "(owner_type IS NOT NULL AND owner_id IS NOT NULL AND owner_auth_user_id IS NOT NULL)",
            name="ck_projects_owner_identity_complete",
        ),
        db.CheckConstraint(
            "provisioning_attempts >= 0",
            name="ck_projects_provisioning_attempts_nonnegative",
        ),
        db.CheckConstraint(
            "access_sync_attempts >= 0",
            name="ck_projects_access_sync_attempts_nonnegative",
        ),
        db.Index("ix_projects_status_created_at", "status", "created_at"),
        db.Index("ix_projects_owner_lookup", "owner_type", "owner_auth_user_id"),
        db.Index("ix_projects_default_universe", "project_id", "default_universe_id"),
        db.Index("ix_projects_default_world", "project_id", "default_world_id"),
        db.Index("ix_projects_spawn_world", "project_id", "spawn_world_id"),
        db.Index("ix_projects_active_lookup", "project_id", "status", "deleted_at"),
        db.Index(
            "ix_projects_app_link_lookup",
            "external_app_project_id",
            "status",
            "deleted_at",
        ),
        db.Index(
            "ix_projects_provisioning_lookup",
            "provisioning_status",
            "provisioning_repair_required",
            "provisioning_updated_at",
        ),
        db.Index(
            "ix_projects_access_sync_lookup",
            "access_sync_status",
            "access_sync_repair_required",
            "access_sync_updated_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Project id={self.id!r} project_id={self.project_id!r} "
            f"external_app_project_id={self.external_app_project_id!r} "
            f"owner_fingerprint={self.owner_fingerprint!r} "
            f"provisioning_status={self.provisioning_status!r} "
            f"access_sync_status={self.access_sync_status!r}>"
        )

    # ------------------------------------------------------------------
    # Queryable compatibility aliases used by adaptive repositories
    # ------------------------------------------------------------------

    if synonym is not None:
        public_id = synonym("project_id")
        project_public_id = synonym("project_id")
        chunk_project_id = synonym("project_id")
        app_project_public_id = synonym("external_app_project_id")
        universe_id = synonym("default_universe_id")
        world_id = synonym("default_world_id")
        requested_template_id = synonym("world_template_requested")
        effective_template_id = synonym("world_template_effective")
        fallback_used = synonym("world_fallback_used")
        fallback_code = synonym("world_fallback_code")
        access_status = synonym("access_sync_status")
        request_fingerprint = synonym("provisioning_fingerprint")
        world_metadata = synonym("world_metadata_json")
        auth_owner_user_id = synonym("owner_auth_user_id")

    @property
    def owner_user_id(self) -> Optional[str]:
        """Compatibility alias for the canonical owner auth id."""
        return self.owner_auth_user_id or self.owner_id

    @property
    def owner_fingerprint(self) -> str:
        return _short_fingerprint(self.owner_user_id, "usr")

    @property
    def created_by_fingerprint(self) -> str:
        return _short_fingerprint(self.created_by_auth_user_id, "usr")

    @property
    def updated_by_fingerprint(self) -> str:
        return _short_fingerprint(self.updated_by_auth_user_id, "usr")

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

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
        owner_auth_user_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        created_by_auth_user_id: Optional[str] = None,
        updated_by_auth_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        world_template_requested: str = WORLD_TEMPLATE_EARTH,
        world_template_effective: Optional[str] = None,
        provisioning_status: str = PROVISIONING_STATUS_PENDING,
        access_sync_status: str = ACCESS_SYNC_STATUS_PENDING,
    ) -> "Project":
        """Create a Project instance without adding it to a session.

        Generic creation never invents an owner.  The sole compatibility
        exception is the explicit ``dev-project`` bootstrap project.
        """
        public_project_id = normalize_project_id(project_id or generate_project_id())
        normalized_name = normalize_required_text(
            name or public_project_id,
            field_name="name",
            max_length=PROJECT_NAME_MAX_LENGTH,
        )
        normalized_external_id = normalize_external_app_project_id(
            external_app_project_id
        )
        normalized_source = normalize_optional_text(
            source_service,
            field_name="source_service",
            max_length=PROJECT_SOURCE_SERVICE_MAX_LENGTH,
        )
        if normalized_external_id is not None and normalized_source is None:
            normalized_source = "vectoplan-app"

        no_owner_supplied = all(
            value is None
            for value in (owner_type, owner_id, owner_user_id, owner_auth_user_id)
        )
        if no_owner_supplied:
            if (
                public_project_id == "dev-project"
                and normalized_external_id is None
                and normalized_source in {None, "vectoplan-chunk", "vectoplan-chunk-init"}
            ):
                owner_auth_user_id = DEV_PROJECT_OWNER_AUTH_USER_ID
                normalized_source = normalized_source or "vectoplan-chunk"
            else:
                raise ValueError(
                    "owner_auth_user_id is required. The model does not create a "
                    "numeric or anonymous owner placeholder."
                )

        normalized_owner_type, normalized_owner_id = normalize_owner_pair(
            owner_type=owner_type,
            owner_id=owner_id,
            owner_user_id=owner_user_id,
            owner_auth_user_id=owner_auth_user_id,
            required=True,
        )

        created_actor = normalize_auth_user_id(
            created_by_auth_user_id or created_by_user_id,
            field_name="created_by_auth_user_id",
            required=False,
        ) or normalized_owner_id
        updated_actor = normalize_auth_user_id(
            updated_by_auth_user_id or updated_by_user_id,
            field_name="updated_by_auth_user_id",
            required=False,
        ) or created_actor

        normalized_lifecycle_status = normalize_status(status)
        requested_template = normalize_world_template(world_template_requested)
        effective_template = (
            normalize_world_template(world_template_effective)
            if world_template_effective not in (None, "")
            else None
        )
        now = utc_now()
        instance = cls(
            project_id=public_project_id,
            slug=normalize_slug(slug),
            name=normalized_name,
            description=normalize_optional_text(
                description,
                field_name="description",
                max_length=PROJECT_DESCRIPTION_MAX_LENGTH,
            ),
            status=normalized_lifecycle_status,
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
                spawn_world_id or default_world_id,
                field_name="spawn_world_id",
                max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
            ),
            external_app_project_id=normalized_external_id,
            source_service=normalized_source,
            external_url=normalize_external_url(external_url),
            owner_auth_user_id=normalized_owner_id,
            owner_type=normalized_owner_type,
            owner_id=normalized_owner_id,
            created_by_auth_user_id=created_actor,
            updated_by_auth_user_id=updated_actor,
            created_by_user_id=created_actor,
            updated_by_user_id=updated_actor,
            world_template_requested=requested_template,
            world_template_effective=effective_template,
            world_fallback_used=False,
            provisioning_status=normalize_provisioning_status(provisioning_status),
            access_sync_status=normalize_access_sync_status(access_sync_status),
            access_projection_version=PROJECT_ACCESS_PROJECTION_VERSION,
            metadata_json=normalize_metadata(metadata_json),
            world_metadata_json={},
            created_at=now,
            updated_at=now,
            archived_at=now if normalized_lifecycle_status == PROJECT_STATUS_ARCHIVED else None,
            deleted_at=now if normalized_lifecycle_status == PROJECT_STATUS_DELETED else None,
        )
        instance.normalize_for_persistence()
        return instance

    @classmethod
    def create_dev_project(
        cls,
        *,
        project_id: str = "dev-project",
        default_universe_id: str = "dev-universe",
        default_world_id: str = "world_spawn",
        owner_user_id: str = DEV_PROJECT_OWNER_AUTH_USER_ID,
        created_by_user_id: Optional[str] = None,
    ) -> "Project":
        """Create the explicit development project for DB bootstrap."""
        owner = normalize_auth_user_id(
            owner_user_id,
            field_name="owner_user_id",
            required=True,
        )
        return cls.create(
            project_id=project_id,
            slug=project_id,
            name="Dev Project",
            description="Default development project for the Chunk world slice.",
            default_universe_id=default_universe_id,
            default_world_id=default_world_id,
            spawn_world_id=default_world_id,
            source_service="vectoplan-chunk",
            owner_auth_user_id=owner,
            created_by_auth_user_id=created_by_user_id or owner,
            world_template_requested=WORLD_TEMPLATE_FLAT,
            world_template_effective=WORLD_TEMPLATE_FLAT,
            provisioning_status=PROVISIONING_STATUS_READY,
            access_sync_status=ACCESS_SYNC_STATUS_PENDING,
            metadata_json={
                "seed": True,
                "seedType": "development",
                "createdBy": "vectoplan-chunk",
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
        owner_user_id: Optional[str] = None,
        owner_auth_user_id: Optional[str] = None,
        created_by_user_id: Optional[str] = None,
        created_by_auth_user_id: Optional[str] = None,
        metadata_json: Optional[Mapping[str, Any]] = None,
        requested_template_id: str = WORLD_TEMPLATE_EARTH,
    ) -> "Project":
        """Create a Chunk project linked to one App project.

        Owner identity is mandatory and must be the canonical Auth identity.
        Universe/World creation remains in the provisioning transaction.
        """
        app_id = normalize_external_app_project_id(app_project_public_id)
        if app_id is None:
            raise ValueError("app_project_public_id is required.")
        owner = normalize_auth_user_id(
            owner_auth_user_id or owner_user_id,
            field_name="owner_auth_user_id",
            required=True,
        )
        if chunk_project_id is None:
            digest = hashlib.sha256(f"project:{app_id}".encode("utf-8")).hexdigest()[:24]
            hint = re.sub(r"[^A-Za-z0-9_-]+", "-", app_id)[:28].strip("-") or "project"
            chunk_project_id = f"chk_prj_{hint}_{digest}"

        metadata = _merge_metadata(
            {
                "schemaVersion": PROJECT_SCHEMA_VERSION,
                "sourceService": source_service,
                "externalAppProjectId": app_id,
                "createdBy": "vectoplan-chunk.project-provisioning",
            },
            metadata_json,
        )
        return cls.create(
            project_id=chunk_project_id,
            slug=chunk_project_id,
            name=name or f"Chunk Project for {app_id}",
            description=description,
            default_universe_id=default_universe_id,
            default_world_id=default_world_id,
            spawn_world_id=spawn_world_id or default_world_id,
            external_app_project_id=app_id,
            source_service=source_service,
            external_url=external_url,
            owner_auth_user_id=owner,
            created_by_auth_user_id=created_by_auth_user_id or created_by_user_id or owner,
            metadata_json=metadata,
            world_template_requested=requested_template_id,
            provisioning_status=PROVISIONING_STATUS_PENDING,
            access_sync_status=ACCESS_SYNC_STATUS_PENDING,
        )

    @classmethod
    def from_create_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        created_by_user_id: Optional[str] = None,
        created_by_auth_user_id: Optional[str] = None,
    ) -> "Project":
        """Create a Project from compatible API keys.

        Actor identity is a trusted method argument.  Body-supplied actor/local
        user fields are intentionally ignored or rejected.
        """
        if not isinstance(payload, Mapping):
            raise ValueError("Project create payload must be a JSON object.")

        forbidden = {
            "user_id",
            "userId",
            "local_user_id",
            "localUserId",
            "createdByUserId",
            "created_by_user_id",
            "updatedByUserId",
            "updated_by_user_id",
            "email",
            "ownerEmail",
        }.intersection(payload)
        if forbidden:
            raise ValueError(
                "Project create payload contains forbidden local identity fields: "
                + ", ".join(sorted(forbidden))
            )

        owner = _payload_first(
            payload,
            "ownerAuthUserId",
            "owner_auth_user_id",
            "ownerUserId",
            "owner_user_id",
            "authUserId",
            "auth_user_id",
        )
        return cls.create(
            project_id=_payload_first(
                payload,
                "chunkProjectId",
                "chunk_project_id",
                "projectId",
                "project_id",
            ),
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
            ),
            external_app_project_id=_payload_first(
                payload,
                "externalAppProjectId",
                "external_app_project_id",
                "appProjectPublicId",
                "app_project_public_id",
            ),
            source_service=_payload_first(payload, "sourceService", "source_service"),
            external_url=_payload_first(payload, "externalUrl", "external_url"),
            owner_type=_payload_first(payload, "ownerType", "owner_type"),
            owner_id=_payload_first(payload, "ownerId", "owner_id"),
            owner_auth_user_id=owner,
            created_by_auth_user_id=created_by_auth_user_id or created_by_user_id,
            updated_by_auth_user_id=created_by_auth_user_id or created_by_user_id,
            metadata_json=_payload_metadata_value(payload),
            world_template_requested=_payload_first(
                payload,
                "requestedTemplateId",
                "requested_template_id",
                "worldTemplate",
                "world_template",
                default=WORLD_TEMPLATE_EARTH,
            ),
        )

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------

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
    def has_owner(self) -> bool:
        return bool(self.owner_type and self.owner_user_id)

    @property
    def is_user_owned(self) -> bool:
        return self.owner_type == PROJECT_OWNER_TYPE_USER and bool(self.owner_user_id)

    @property
    def owner_reference(self) -> Optional[Dict[str, str]]:
        if not self.has_owner:
            return None
        return {
            "type": PROJECT_OWNER_TYPE_USER,
            "fingerprint": self.owner_fingerprint,
        }

    @property
    def provisioning_ready(self) -> bool:
        return self.provisioning_status in {
            PROVISIONING_STATUS_READY,
            PROVISIONING_STATUS_FALLBACK_READY,
        }

    @property
    def access_ready(self) -> bool:
        return self.access_sync_status in {
            ACCESS_SYNC_STATUS_READY,
            ACCESS_SYNC_STATUS_DISABLED,
        }

    @property
    def repair_required(self) -> bool:
        return bool(
            self.provisioning_repair_required
            or self.access_sync_repair_required
            or self.provisioning_status == PROVISIONING_STATUS_REPAIR_REQUIRED
            or self.access_sync_status == ACCESS_SYNC_STATUS_REPAIR_REQUIRED
        )

    @property
    def world_template_state(self) -> Dict[str, Any]:
        return {
            "requested": self.world_template_requested,
            "effective": self.world_template_effective,
            "fallbackUsed": bool(self.world_fallback_used),
            "fallbackCode": self.world_fallback_code,
            "earthReferenceFingerprint": self.earth_reference_fingerprint,
        }

    @property
    def provisioning_state(self) -> Dict[str, Any]:
        return {
            "status": self.provisioning_status,
            "ready": self.provisioning_ready,
            "retryable": bool(self.provisioning_retryable),
            "repairRequired": bool(self.provisioning_repair_required),
            "errorCode": self.provisioning_error_code,
            "attempts": int(self.provisioning_attempts or 0),
            "requestId": self.provisioning_request_id,
            "correlationId": self.provisioning_correlation_id,
            "requestFingerprint": self.provisioning_fingerprint,
            "provisionedAt": datetime_to_iso(self.provisioned_at),
            "updatedAt": datetime_to_iso(self.provisioning_updated_at),
        }

    @property
    def access_projection_state(self) -> Dict[str, Any]:
        return {
            "status": self.access_sync_status,
            "ready": self.access_ready,
            "retryable": bool(self.access_sync_retryable),
            "repairRequired": bool(self.access_sync_repair_required),
            "errorCode": self.access_sync_error_code,
            "attempts": int(self.access_sync_attempts or 0),
            "projectionVersion": self.access_projection_version,
            "projectionFingerprint": self.access_projection_fingerprint,
            "requestId": self.access_sync_request_id,
            "correlationId": self.access_sync_correlation_id,
            "syncedAt": datetime_to_iso(self.access_synced_at),
            "updatedAt": datetime_to_iso(self.access_sync_updated_at),
        }

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def touch(
        self,
        *,
        updated_by_user_id: Optional[str] = None,
        updated_by_auth_user_id: Optional[str] = None,
    ) -> None:
        """Mark the project updated and increment its optimistic revision."""
        self.updated_at = utc_now()
        self.revision = max(1, int(self.revision or 1)) + 1
        actor = normalize_auth_user_id(
            updated_by_auth_user_id or updated_by_user_id,
            field_name="updated_by_auth_user_id",
            required=False,
        )
        if actor is not None:
            self.updated_by_auth_user_id = actor
            self.updated_by_user_id = actor

    def ensure_not_deleted(self) -> None:
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
        updated_by_auth_user_id: Optional[str] = None,
    ) -> None:
        self.ensure_not_deleted()
        self.name = normalize_required_text(
            name,
            field_name="name",
            max_length=PROJECT_NAME_MAX_LENGTH,
        )
        if slug is not None:
            self.slug = normalize_slug(slug)
        self.touch(
            updated_by_user_id=updated_by_user_id,
            updated_by_auth_user_id=updated_by_auth_user_id,
        )

    def update_description(
        self,
        description: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
        updated_by_auth_user_id: Optional[str] = None,
    ) -> None:
        self.ensure_not_deleted()
        self.description = normalize_optional_text(
            description,
            field_name="description",
            max_length=PROJECT_DESCRIPTION_MAX_LENGTH,
        )
        self.touch(
            updated_by_user_id=updated_by_user_id,
            updated_by_auth_user_id=updated_by_auth_user_id,
        )

    def set_world_refs(
        self,
        *,
        default_universe_id: Optional[str] = None,
        default_world_id: Optional[str] = None,
        spawn_world_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        updated_by_auth_user_id: Optional[str] = None,
    ) -> None:
        """Update service-owned Universe/World references in one revision."""
        self.ensure_not_deleted()
        if default_universe_id is not None:
            self.universe_id = default_universe_id
        if default_world_id is not None:
            self.world_id = default_world_id
        if spawn_world_id is not None:
            self.spawn_world_id = normalize_optional_text(
                spawn_world_id,
                field_name="spawn_world_id",
                max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
            )
        self.touch(
            updated_by_user_id=updated_by_user_id,
            updated_by_auth_user_id=updated_by_auth_user_id,
        )

    def set_default_universe_id(
        self,
        default_universe_id: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        self.set_world_refs(
            default_universe_id=default_universe_id,
            updated_by_user_id=updated_by_user_id,
        )

    def set_default_world_id(
        self,
        default_world_id: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        self.set_world_refs(
            default_world_id=default_world_id,
            updated_by_user_id=updated_by_user_id,
        )

    def set_spawn_world_id(
        self,
        spawn_world_id: Optional[str],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        self.set_world_refs(
            spawn_world_id=spawn_world_id,
            updated_by_user_id=updated_by_user_id,
        )

    def ensure_external_app_link(
        self,
        *,
        external_app_project_id: str,
        source_service: str = "vectoplan-app",
        external_url: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        """Idempotently establish one immutable App project link."""
        self.set_external_app_link(
            external_app_project_id=external_app_project_id,
            source_service=source_service,
            external_url=external_url,
            updated_by_user_id=updated_by_user_id,
            allow_replace=False,
        )

    def set_external_app_link(
        self,
        *,
        external_app_project_id: Optional[str],
        source_service: Optional[str] = "vectoplan-app",
        external_url: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        allow_replace: bool = False,
    ) -> None:
        self.ensure_not_deleted()
        external_id = normalize_external_app_project_id(external_app_project_id)
        if (
            self.external_app_project_id
            and external_id
            and self.external_app_project_id != external_id
            and not allow_replace
        ):
            raise ValueError(
                "Project is already linked to a different App project. "
                "Use an explicit maintenance migration to replace the link."
            )
        source = normalize_optional_text(
            source_service,
            field_name="source_service",
            max_length=PROJECT_SOURCE_SERVICE_MAX_LENGTH,
        )
        if external_id and not source:
            raise ValueError("source_service is required for an App project link.")
        url = normalize_external_url(external_url)
        if (
            self.external_app_project_id == external_id
            and self.source_service == source
            and self.external_url == url
        ):
            return
        self.external_app_project_id = external_id
        self.source_service = source
        self.external_url = url
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_owner(
        self,
        *,
        owner_type: Optional[str] = None,
        owner_id: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        owner_auth_user_id: Optional[str] = None,
        updated_by_user_id: Optional[str] = None,
        allow_clear: bool = False,
    ) -> None:
        """Set owner identity atomically.

        Application routes should use the dedicated access owner-transfer
        service; this method is the model-level primitive for that service.
        """
        self.ensure_not_deleted()
        wants_clear = all(
            value is None
            for value in (owner_type, owner_id, owner_user_id, owner_auth_user_id)
        )
        if wants_clear:
            if not allow_clear:
                raise ValueError(
                    "Project ownership cannot be cleared unless allow_clear=True."
                )
            normalized_type = None
            normalized_id = None
        else:
            normalized_type, normalized_id = normalize_owner_pair(
                owner_type=owner_type,
                owner_id=owner_id,
                owner_user_id=owner_user_id,
                owner_auth_user_id=owner_auth_user_id,
                required=True,
            )
        if (
            self.owner_type == normalized_type
            and self.owner_auth_user_id == normalized_id
            and self.owner_id == normalized_id
        ):
            return
        self.owner_type = normalized_type
        self.owner_auth_user_id = normalized_id
        self.owner_id = normalized_id
        self.touch(updated_by_user_id=updated_by_user_id)

    def set_owner_user(
        self,
        owner_user_id: Any,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        self.set_owner(
            owner_auth_user_id=owner_user_id,
            updated_by_user_id=updated_by_user_id,
        )

    def clear_owner(self, *, updated_by_user_id: Optional[str] = None) -> None:
        """Maintenance-only helper; normal projects must always retain an owner."""
        self.set_owner(
            updated_by_user_id=updated_by_user_id,
            allow_clear=True,
        )

    def is_owned_by_user(self, user_id: Any) -> bool:
        try:
            normalized = normalize_auth_user_id(
                user_id,
                field_name="auth_user_id",
                required=True,
            )
        except Exception:
            return False
        return self.owner_user_id == normalized

    def set_world_template_state(
        self,
        *,
        requested_template_id: Any,
        effective_template_id: Any,
        fallback_used: Any = False,
        fallback_code: Any = None,
        earth_reference_fingerprint: Any = None,
        allow_template_change: bool = False,
        updated_by_auth_user_id: Optional[str] = None,
    ) -> None:
        """Persist requested/effective template state without silent migration."""
        self.ensure_not_deleted()
        requested = normalize_world_template(requested_template_id)
        effective = normalize_world_template(effective_template_id)
        fallback = _safe_bool(fallback_used, False)
        code = normalize_error_code(fallback_code)

        if self.world_template_requested and self.world_template_requested != requested:
            if not allow_template_change:
                raise ValueError(
                    "Existing world template cannot be changed by provisioning retry; "
                    "use a dedicated world-migration operation."
                )
        if effective != requested:
            if not (
                requested == WORLD_TEMPLATE_EARTH
                and effective == WORLD_TEMPLATE_FLAT
                and fallback
                and code
            ):
                raise ValueError(
                    "Different requested/effective templates require an explicit "
                    "Earth-to-Flat fallback code."
                )
        elif fallback or code:
            raise ValueError(
                "fallback_used/fallback_code must be empty when templates are equal."
            )

        self.world_template_requested = requested
        self.world_template_effective = effective
        self.world_fallback_used = fallback
        self.world_fallback_code = code
        self.earth_reference_fingerprint = normalize_optional_text(
            earth_reference_fingerprint,
            field_name="earth_reference_fingerprint",
            max_length=PROJECT_FINGERPRINT_MAX_LENGTH,
        )
        self.touch(updated_by_auth_user_id=updated_by_auth_user_id)

    def apply_provisioning_state(
        self,
        *,
        status: Any,
        request_fingerprint: Any = None,
        request_id: Any = None,
        correlation_id: Any = None,
        error_code: Any = None,
        retryable: Any = False,
        repair_required: Any = False,
        increment_attempt: bool = False,
        world_metadata: Optional[Mapping[str, Any]] = None,
        updated_by_auth_user_id: Optional[str] = None,
    ) -> None:
        """Apply a provisioning lifecycle update."""
        self.ensure_not_deleted()
        normalized_status = normalize_provisioning_status(status)
        now = utc_now()
        self.provisioning_status = normalized_status
        self.provisioning_fingerprint = normalize_optional_text(
            request_fingerprint,
            field_name="provisioning_fingerprint",
            max_length=PROJECT_FINGERPRINT_MAX_LENGTH,
        ) or self.provisioning_fingerprint
        self.provisioning_request_id = normalize_request_identifier(
            request_id,
            field_name="provisioning_request_id",
        ) or self.provisioning_request_id
        self.provisioning_correlation_id = normalize_request_identifier(
            correlation_id,
            field_name="provisioning_correlation_id",
        ) or self.provisioning_correlation_id
        self.provisioning_error_code = normalize_error_code(error_code)
        self.provisioning_retryable = _safe_bool(retryable, False)
        self.provisioning_repair_required = bool(
            _safe_bool(repair_required, False)
            or normalized_status == PROVISIONING_STATUS_REPAIR_REQUIRED
        )
        if increment_attempt:
            self.provisioning_attempts = max(0, int(self.provisioning_attempts or 0)) + 1
        self.provisioning_updated_at = now
        if normalized_status in {
            PROVISIONING_STATUS_READY,
            PROVISIONING_STATUS_FALLBACK_READY,
        }:
            self.provisioned_at = self.provisioned_at or now
            self.provisioning_error_code = None
            self.provisioning_retryable = False
            self.provisioning_repair_required = False
        if world_metadata is not None:
            self.world_metadata_json = normalize_metadata(world_metadata)
        self.touch(updated_by_auth_user_id=updated_by_auth_user_id)

    def apply_access_sync_state(
        self,
        *,
        status: Any,
        projection_version: Any = None,
        projection_fingerprint: Any = None,
        request_id: Any = None,
        correlation_id: Any = None,
        error_code: Any = None,
        retryable: Any = False,
        repair_required: Any = False,
        increment_attempt: bool = False,
        updated_by_auth_user_id: Optional[str] = None,
    ) -> None:
        """Apply a Chunk access-projection lifecycle update."""
        self.ensure_not_deleted()
        normalized_status = normalize_access_sync_status(status)
        now = utc_now()
        self.access_sync_status = normalized_status
        self.access_projection_version = normalize_optional_text(
            projection_version,
            field_name="access_projection_version",
            max_length=PROJECT_PROJECTION_VERSION_MAX_LENGTH,
        ) or self.access_projection_version or PROJECT_ACCESS_PROJECTION_VERSION
        self.access_projection_fingerprint = normalize_optional_text(
            projection_fingerprint,
            field_name="access_projection_fingerprint",
            max_length=PROJECT_FINGERPRINT_MAX_LENGTH,
        ) or self.access_projection_fingerprint
        self.access_sync_request_id = normalize_request_identifier(
            request_id,
            field_name="access_sync_request_id",
        ) or self.access_sync_request_id
        self.access_sync_correlation_id = normalize_request_identifier(
            correlation_id,
            field_name="access_sync_correlation_id",
        ) or self.access_sync_correlation_id
        self.access_sync_error_code = normalize_error_code(error_code)
        self.access_sync_retryable = _safe_bool(retryable, False)
        self.access_sync_repair_required = bool(
            _safe_bool(repair_required, False)
            or normalized_status == ACCESS_SYNC_STATUS_REPAIR_REQUIRED
        )
        if increment_attempt:
            self.access_sync_attempts = max(0, int(self.access_sync_attempts or 0)) + 1
        self.access_sync_updated_at = now
        if normalized_status == ACCESS_SYNC_STATUS_READY:
            self.access_synced_at = now
            self.access_sync_error_code = None
            self.access_sync_retryable = False
            self.access_sync_repair_required = False
        self.touch(updated_by_auth_user_id=updated_by_auth_user_id)

    def set_status(
        self,
        status: str,
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
        normalized = normalize_status(status)
        now = utc_now()
        if normalized == PROJECT_STATUS_DELETED:
            self.deleted_at = self.deleted_at or now
        elif normalized == PROJECT_STATUS_ARCHIVED:
            self.archived_at = self.archived_at or now
            self.deleted_at = None
        else:
            self.archived_at = None
            self.deleted_at = None
        self.status = normalized
        self.touch(updated_by_user_id=updated_by_user_id)

    def archive(self, *, updated_by_user_id: Optional[str] = None) -> None:
        self.ensure_not_deleted()
        self.set_status(PROJECT_STATUS_ARCHIVED, updated_by_user_id=updated_by_user_id)

    def restore(self, *, updated_by_user_id: Optional[str] = None) -> None:
        self.set_status(PROJECT_STATUS_ACTIVE, updated_by_user_id=updated_by_user_id)

    def soft_delete(self, *, updated_by_user_id: Optional[str] = None) -> None:
        self.set_status(PROJECT_STATUS_DELETED, updated_by_user_id=updated_by_user_id)

    def replace_metadata(
        self,
        metadata_json: Optional[Mapping[str, Any]],
        *,
        updated_by_user_id: Optional[str] = None,
    ) -> None:
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
            current[str(key)] = value
        self.metadata_json = normalize_metadata(current)
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
        """Merge bounded provisioning metadata without persisting identities."""
        metadata_update: Dict[str, Any] = {
            "schemaVersion": PROJECT_SCHEMA_VERSION,
            "chunkProjectId": self.project_id,
            "externalAppProjectId": external_app_project_id or self.external_app_project_id,
            "chunkUniverseId": chunk_universe_id or self.default_universe_id,
            "chunkWorldId": chunk_world_id or self.spawn_world_id or self.default_world_id,
            "sourceService": self.source_service,
            "routeHints": sanitize_project_metadata(route_hints or {}),
            "appPayload": sanitize_project_metadata(app_payload or {}),
            "linkedAt": datetime_to_iso(utc_now()),
        }
        self.update_metadata(metadata_update, updated_by_user_id=updated_by_user_id)

    def get_metadata_value(self, key: str, default: Any = None) -> Any:
        try:
            return normalize_metadata(self.metadata_json).get(key, default)
        except Exception:
            return default

    def apply_patch_payload(
        self,
        payload: Mapping[str, Any],
        *,
        updated_by_user_id: Optional[str] = None,
        allow_system_fields: bool = False,
        allow_owner_transfer: bool = False,
    ) -> None:
        """Apply a PATCH-style payload.

        Owner, App-link, world-reference and provisioning/access fields are
        protected by default and belong to dedicated service operations.
        """
        if not isinstance(payload, Mapping):
            raise ValueError("Project patch payload must be a JSON object.")
        self.ensure_not_deleted()

        owner_keys = {
            "ownerAuthUserId",
            "owner_auth_user_id",
            "ownerUserId",
            "owner_user_id",
            "ownerType",
            "owner_type",
            "ownerId",
            "owner_id",
        }
        if owner_keys.intersection(payload) and not allow_owner_transfer:
            raise ValueError(
                "Owner changes require the dedicated access owner-transfer operation."
            )

        system_keys = {
            "defaultUniverseId",
            "default_universe_id",
            "defaultWorldId",
            "default_world_id",
            "spawnWorldId",
            "spawn_world_id",
            "externalAppProjectId",
            "external_app_project_id",
            "appProjectPublicId",
            "app_project_public_id",
            "sourceService",
            "source_service",
            "externalUrl",
            "external_url",
            "requestedTemplateId",
            "requested_template_id",
            "effectiveTemplateId",
            "effective_template_id",
            "provisioningStatus",
            "provisioning_status",
            "accessSyncStatus",
            "access_sync_status",
        }
        if system_keys.intersection(payload) and not allow_system_fields:
            raise ValueError(
                "Service-owned project fields require a dedicated provisioning or "
                "access operation."
            )

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

        metadata_replace_key = _payload_present_key(
            payload,
            "metadataJson",
            "metadata_json",
            "metadata",
        )
        if metadata_replace_key is not None:
            self.metadata_json = normalize_metadata(payload.get(metadata_replace_key))
            changed = True
        if "metadataMerge" in payload:
            merge_value = payload.get("metadataMerge")
            if not isinstance(merge_value, Mapping):
                raise ValueError("metadataMerge must be a JSON object/dict.")
            current = normalize_metadata(self.metadata_json)
            current.update(dict(merge_value))
            self.metadata_json = normalize_metadata(current)
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

        if allow_system_fields:
            if "defaultUniverseId" in payload or "default_universe_id" in payload:
                self.universe_id = _payload_first(
                    payload,
                    "defaultUniverseId",
                    "default_universe_id",
                )
                changed = True
            if "defaultWorldId" in payload or "default_world_id" in payload:
                self.world_id = _payload_first(
                    payload,
                    "defaultWorldId",
                    "default_world_id",
                )
                changed = True
            if "spawnWorldId" in payload or "spawn_world_id" in payload:
                self.spawn_world_id = normalize_optional_text(
                    _payload_first(payload, "spawnWorldId", "spawn_world_id"),
                    field_name="spawn_world_id",
                    max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
                )
                changed = True
            if system_keys.intersection(payload):
                external_key = _payload_present_key(
                    payload,
                    "externalAppProjectId",
                    "external_app_project_id",
                    "appProjectPublicId",
                    "app_project_public_id",
                )
                if external_key:
                    self.external_app_project_id = normalize_external_app_project_id(
                        payload.get(external_key)
                    )
                    changed = True
                source_key = _payload_present_key(payload, "sourceService", "source_service")
                if source_key:
                    self.source_service = normalize_optional_text(
                        payload.get(source_key),
                        field_name="source_service",
                        max_length=PROJECT_SOURCE_SERVICE_MAX_LENGTH,
                    )
                    changed = True

        if allow_owner_transfer and owner_keys.intersection(payload):
            self.set_owner(
                owner_type=_payload_first(payload, "ownerType", "owner_type"),
                owner_id=_payload_first(payload, "ownerId", "owner_id"),
                owner_auth_user_id=_payload_first(
                    payload,
                    "ownerAuthUserId",
                    "owner_auth_user_id",
                    "ownerUserId",
                    "owner_user_id",
                ),
                updated_by_user_id=updated_by_user_id,
            )
            changed = False  # set_owner already touched

        if "status" in payload:
            self.set_status(
                str(payload.get("status")),
                updated_by_user_id=updated_by_user_id,
            )
            changed = False
        if changed:
            self.touch(updated_by_user_id=updated_by_user_id)

    # ------------------------------------------------------------------
    # Persistence normalization and validation
    # ------------------------------------------------------------------

    def normalize_for_persistence(self) -> "Project":
        """Normalize all persisted fields before insert/update."""
        self.project_id = normalize_project_id(self.project_id)
        self.slug = normalize_slug(self.slug)
        self.name = normalize_required_text(
            self.name,
            field_name="name",
            max_length=PROJECT_NAME_MAX_LENGTH,
        )
        self.description = normalize_optional_text(
            self.description,
            field_name="description",
            max_length=PROJECT_DESCRIPTION_MAX_LENGTH,
        )
        self.status = normalize_status(self.status)
        self.schema_version = PROJECT_SCHEMA_VERSION
        self.revision = max(1, int(self.revision or 1))

        self.default_universe_id = normalize_optional_text(
            self.default_universe_id,
            field_name="default_universe_id",
            max_length=PROJECT_DEFAULT_UNIVERSE_ID_MAX_LENGTH,
        )
        self.default_world_id = normalize_optional_text(
            self.default_world_id,
            field_name="default_world_id",
            max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
        )
        self.spawn_world_id = normalize_optional_text(
            self.spawn_world_id or self.default_world_id,
            field_name="spawn_world_id",
            max_length=PROJECT_DEFAULT_WORLD_ID_MAX_LENGTH,
        )
        self.external_app_project_id = normalize_external_app_project_id(
            self.external_app_project_id
        )
        self.source_service = normalize_optional_text(
            self.source_service,
            field_name="source_service",
            max_length=PROJECT_SOURCE_SERVICE_MAX_LENGTH,
        )
        if self.external_app_project_id and not self.source_service:
            self.source_service = "vectoplan-app"
        self.external_url = normalize_external_url(self.external_url)

        owner_type, owner_id = normalize_owner_pair(
            owner_type=self.owner_type,
            owner_id=self.owner_id,
            owner_auth_user_id=self.owner_auth_user_id,
            required=True,
        )
        self.owner_type = owner_type
        self.owner_auth_user_id = owner_id
        self.owner_id = owner_id

        created_actor = normalize_auth_user_id(
            self.created_by_auth_user_id or self.created_by_user_id or owner_id,
            field_name="created_by_auth_user_id",
            required=True,
        )
        updated_actor = normalize_auth_user_id(
            self.updated_by_auth_user_id or self.updated_by_user_id or created_actor,
            field_name="updated_by_auth_user_id",
            required=True,
        )
        self.created_by_auth_user_id = created_actor
        self.created_by_user_id = created_actor
        self.updated_by_auth_user_id = updated_actor
        self.updated_by_user_id = updated_actor

        self.world_template_requested = normalize_world_template(
            self.world_template_requested or WORLD_TEMPLATE_EARTH
        )
        if self.world_template_effective:
            self.world_template_effective = normalize_world_template(
                self.world_template_effective
            )
        self.world_fallback_used = _safe_bool(self.world_fallback_used, False)
        self.world_fallback_code = normalize_error_code(self.world_fallback_code)
        self.earth_reference_fingerprint = normalize_optional_text(
            self.earth_reference_fingerprint,
            field_name="earth_reference_fingerprint",
            max_length=PROJECT_FINGERPRINT_MAX_LENGTH,
        )

        self.provisioning_status = normalize_provisioning_status(
            self.provisioning_status
        )
        self.provisioning_fingerprint = normalize_optional_text(
            self.provisioning_fingerprint,
            field_name="provisioning_fingerprint",
            max_length=PROJECT_FINGERPRINT_MAX_LENGTH,
        )
        self.provisioning_request_id = normalize_request_identifier(
            self.provisioning_request_id,
            field_name="provisioning_request_id",
        )
        self.provisioning_correlation_id = normalize_request_identifier(
            self.provisioning_correlation_id,
            field_name="provisioning_correlation_id",
        )
        self.provisioning_error_code = normalize_error_code(
            self.provisioning_error_code
        )
        self.provisioning_retryable = _safe_bool(self.provisioning_retryable, False)
        self.provisioning_repair_required = bool(
            _safe_bool(self.provisioning_repair_required, False)
            or self.provisioning_status == PROVISIONING_STATUS_REPAIR_REQUIRED
        )
        self.provisioning_attempts = max(0, int(self.provisioning_attempts or 0))
        self.provisioned_at = normalize_datetime(
            self.provisioned_at,
            field_name="provisioned_at",
        )
        self.provisioning_updated_at = normalize_datetime(
            self.provisioning_updated_at,
            field_name="provisioning_updated_at",
        )
        if self.provisioning_status in {
            PROVISIONING_STATUS_READY,
            PROVISIONING_STATUS_FALLBACK_READY,
        }:
            self.provisioning_error_code = None
            self.provisioning_retryable = False
            self.provisioning_repair_required = False
            self.provisioned_at = self.provisioned_at or utc_now()

        self.access_sync_status = normalize_access_sync_status(
            self.access_sync_status
        )
        self.access_projection_version = normalize_optional_text(
            self.access_projection_version or PROJECT_ACCESS_PROJECTION_VERSION,
            field_name="access_projection_version",
            max_length=PROJECT_PROJECTION_VERSION_MAX_LENGTH,
        ) or PROJECT_ACCESS_PROJECTION_VERSION
        self.access_projection_fingerprint = normalize_optional_text(
            self.access_projection_fingerprint,
            field_name="access_projection_fingerprint",
            max_length=PROJECT_FINGERPRINT_MAX_LENGTH,
        )
        self.access_sync_request_id = normalize_request_identifier(
            self.access_sync_request_id,
            field_name="access_sync_request_id",
        )
        self.access_sync_correlation_id = normalize_request_identifier(
            self.access_sync_correlation_id,
            field_name="access_sync_correlation_id",
        )
        self.access_sync_error_code = normalize_error_code(self.access_sync_error_code)
        self.access_sync_retryable = _safe_bool(self.access_sync_retryable, False)
        self.access_sync_repair_required = bool(
            _safe_bool(self.access_sync_repair_required, False)
            or self.access_sync_status == ACCESS_SYNC_STATUS_REPAIR_REQUIRED
        )
        self.access_sync_attempts = max(0, int(self.access_sync_attempts or 0))
        self.access_synced_at = normalize_datetime(
            self.access_synced_at,
            field_name="access_synced_at",
        )
        self.access_sync_updated_at = normalize_datetime(
            self.access_sync_updated_at,
            field_name="access_sync_updated_at",
        )
        if self.access_sync_status == ACCESS_SYNC_STATUS_READY:
            self.access_sync_error_code = None
            self.access_sync_retryable = False
            self.access_sync_repair_required = False
            self.access_synced_at = self.access_synced_at or utc_now()

        self.metadata_json = normalize_metadata(self.metadata_json)
        self.world_metadata_json = normalize_metadata(self.world_metadata_json)

        now = utc_now()
        self.created_at = normalize_datetime(
            self.created_at,
            field_name="created_at",
        ) or now
        self.updated_at = normalize_datetime(
            self.updated_at,
            field_name="updated_at",
        ) or now
        self.archived_at = normalize_datetime(
            self.archived_at,
            field_name="archived_at",
        )
        self.deleted_at = normalize_datetime(
            self.deleted_at,
            field_name="deleted_at",
        )
        if self.status == PROJECT_STATUS_ACTIVE:
            self.archived_at = None
            self.deleted_at = None
        elif self.status == PROJECT_STATUS_ARCHIVED:
            self.archived_at = self.archived_at or now
            self.deleted_at = None
        elif self.status == PROJECT_STATUS_DELETED:
            self.deleted_at = self.deleted_at or now

        errors = self.get_validation_errors()
        if errors:
            first_key = sorted(errors)[0]
            raise ValueError(f"Project validation failed for {first_key}: {errors[first_key]}")
        return self

    def get_validation_errors(self) -> Dict[str, str]:
        """Return validation errors without raising."""
        errors: Dict[str, str] = {}

        checks = (
            ("projectId", lambda: normalize_project_id(self.project_id)),
            (
                "name",
                lambda: normalize_required_text(
                    self.name,
                    field_name="name",
                    max_length=PROJECT_NAME_MAX_LENGTH,
                ),
            ),
            ("status", lambda: normalize_status(self.status)),
            ("metadataJson", lambda: normalize_metadata(self.metadata_json)),
            ("worldMetadata", lambda: normalize_metadata(self.world_metadata_json)),
            (
                "ownerAuthUserId",
                lambda: normalize_owner_pair(
                    owner_type=self.owner_type,
                    owner_id=self.owner_id,
                    owner_auth_user_id=self.owner_auth_user_id,
                    required=True,
                ),
            ),
            (
                "createdByAuthUserId",
                lambda: normalize_auth_user_id(
                    self.created_by_auth_user_id or self.created_by_user_id,
                    field_name="created_by_auth_user_id",
                    required=True,
                ),
            ),
            (
                "updatedByAuthUserId",
                lambda: normalize_auth_user_id(
                    self.updated_by_auth_user_id or self.updated_by_user_id,
                    field_name="updated_by_auth_user_id",
                    required=True,
                ),
            ),
            (
                "worldTemplateRequested",
                lambda: normalize_world_template(self.world_template_requested),
            ),
            (
                "provisioningStatus",
                lambda: normalize_provisioning_status(self.provisioning_status),
            ),
            (
                "accessSyncStatus",
                lambda: normalize_access_sync_status(self.access_sync_status),
            ),
        )
        for key, check in checks:
            try:
                check()
            except Exception as exc:
                errors[key] = str(exc)

        if self.slug is not None:
            try:
                normalize_slug(self.slug)
            except Exception as exc:
                errors["slug"] = str(exc)
        if self.external_app_project_id is not None:
            try:
                normalize_external_app_project_id(self.external_app_project_id)
            except Exception as exc:
                errors["externalAppProjectId"] = str(exc)
            if not self.source_service:
                errors["sourceService"] = (
                    "source_service is required when external_app_project_id is set."
                )

        if self.owner_auth_user_id != self.owner_id:
            errors["ownerIdentity"] = (
                "owner_auth_user_id and compatibility owner_id must be identical."
            )
        if self.created_by_auth_user_id != self.created_by_user_id:
            errors["createdByIdentity"] = (
                "created_by_auth_user_id and compatibility created_by_user_id must match."
            )
        if self.updated_by_auth_user_id != self.updated_by_user_id:
            errors["updatedByIdentity"] = (
                "updated_by_auth_user_id and compatibility updated_by_user_id must match."
            )

        requested = self.world_template_requested
        effective = self.world_template_effective
        fallback = bool(self.world_fallback_used)
        if effective and effective != requested:
            if not (
                requested == WORLD_TEMPLATE_EARTH
                and effective == WORLD_TEMPLATE_FLAT
                and fallback
                and self.world_fallback_code
            ):
                errors["worldTemplate"] = (
                    "Different requested/effective templates require an explicit "
                    "Earth-to-Flat fallback state."
                )
        if effective == requested and (fallback or self.world_fallback_code):
            errors["worldFallback"] = (
                "fallback state must be empty when requested/effective templates match."
            )
        if fallback and not self.world_fallback_code:
            errors["worldFallbackCode"] = "fallback_used requires world_fallback_code."

        if self.provisioning_status in {
            PROVISIONING_STATUS_READY,
            PROVISIONING_STATUS_FALLBACK_READY,
        }:
            if not self.default_universe_id:
                errors["defaultUniverseId"] = "ready provisioning requires a Universe id."
            if not self.default_world_id:
                errors["defaultWorldId"] = "ready provisioning requires a World id."
            if not self.world_template_effective:
                errors["worldTemplateEffective"] = (
                    "ready provisioning requires an effective world template."
                )
        if self.provisioning_status == PROVISIONING_STATUS_FALLBACK_READY:
            if not self.world_fallback_used:
                errors["provisioningFallback"] = (
                    "fallback_ready requires world_fallback_used=true."
                )
        if self.provisioning_repair_required and self.provisioning_status not in {
            PROVISIONING_STATUS_REPAIR_REQUIRED,
            PROVISIONING_STATUS_FAILED,
        }:
            errors["provisioningRepairRequired"] = (
                "provisioning_repair_required requires failed or repair_required status."
            )

        if self.access_sync_status == ACCESS_SYNC_STATUS_READY:
            if not self.access_projection_version:
                errors["accessProjectionVersion"] = (
                    "ready access sync requires a projection version."
                )
            if not self.access_projection_fingerprint:
                errors["accessProjectionFingerprint"] = (
                    "ready access sync requires a projection fingerprint."
                )
        if self.access_sync_repair_required and self.access_sync_status not in {
            ACCESS_SYNC_STATUS_REPAIR_REQUIRED,
            ACCESS_SYNC_STATUS_FAILED,
        }:
            errors["accessSyncRepairRequired"] = (
                "access_sync_repair_required requires failed or repair_required status."
            )

        try:
            if int(self.revision or 0) < 1:
                errors["revision"] = "revision must be greater than or equal to 1."
        except Exception as exc:
            errors["revision"] = f"revision must be an integer: {exc}"
        try:
            if int(self.provisioning_attempts or 0) < 0:
                errors["provisioningAttempts"] = "must be non-negative."
        except Exception:
            errors["provisioningAttempts"] = "must be an integer."
        try:
            if int(self.access_sync_attempts or 0) < 0:
                errors["accessSyncAttempts"] = "must be non-negative."
        except Exception:
            errors["accessSyncAttempts"] = "must be an integer."

        if self.status == PROJECT_STATUS_ACTIVE and self.deleted_at is not None:
            errors["deletedAt"] = "active projects must not have deleted_at set."
        if self.status == PROJECT_STATUS_DELETED and self.deleted_at is None:
            errors["deletedAt"] = "deleted projects must have deleted_at set."
        return errors

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
        include_private: bool = False,
    ) -> Dict[str, Any]:
        """Serialize the project.

        Raw canonical user ids and private service URLs are omitted unless
        ``include_private=True``.  Public consumers receive only fingerprints.
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
            "owner": self.owner_reference,
            "ownerFingerprint": self.owner_fingerprint,
            "worldTemplate": self.world_template_state,
            "provisioning": {
                "status": self.provisioning_status,
                "ready": self.provisioning_ready,
                "retryable": bool(self.provisioning_retryable),
                "repairRequired": bool(self.provisioning_repair_required),
                "errorCode": self.provisioning_error_code,
                "attempts": int(self.provisioning_attempts or 0),
                "requestFingerprint": self.provisioning_fingerprint,
                "provisionedAt": datetime_to_iso(self.provisioned_at),
                "updatedAt": datetime_to_iso(self.provisioning_updated_at),
            },
            "accessProjection": {
                "status": self.access_sync_status,
                "ready": self.access_ready,
                "retryable": bool(self.access_sync_retryable),
                "repairRequired": bool(self.access_sync_repair_required),
                "errorCode": self.access_sync_error_code,
                "attempts": int(self.access_sync_attempts or 0),
                "projectionVersion": self.access_projection_version,
                "projectionFingerprint": self.access_projection_fingerprint,
                "syncedAt": datetime_to_iso(self.access_synced_at),
                "updatedAt": datetime_to_iso(self.access_sync_updated_at),
            },
            "createdByFingerprint": self.created_by_fingerprint,
            "updatedByFingerprint": self.updated_by_fingerprint,
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
                "provisioningReady": self.provisioning_ready,
                "accessReady": self.access_ready,
                "repairRequired": self.repair_required,
                "viewerReadOnly": True,
            },
        }

        if include_metadata:
            result["metadata"] = normalize_metadata(self.metadata_json)
            result["worldMetadata"] = normalize_metadata(self.world_metadata_json)
        if include_private:
            result.update(
                {
                    "externalUrl": normalize_external_url(self.external_url),
                    "ownerType": self.owner_type,
                    "ownerId": self.owner_id,
                    "ownerAuthUserId": self.owner_auth_user_id,
                    "ownerUserId": self.owner_user_id,
                    "createdByAuthUserId": self.created_by_auth_user_id,
                    "updatedByAuthUserId": self.updated_by_auth_user_id,
                    "provisioningRequestId": self.provisioning_request_id,
                    "provisioningCorrelationId": self.provisioning_correlation_id,
                    "accessSyncRequestId": self.access_sync_request_id,
                    "accessSyncCorrelationId": self.access_sync_correlation_id,
                }
            )
        if include_internal:
            result["id"] = self.id
        return result

    def to_public_dict(self) -> Dict[str, Any]:
        """Serialize without database ids, raw users or internal URLs."""
        return self.to_dict(
            include_internal=False,
            include_metadata=True,
            include_private=False,
        )

    def to_private_dict(self, *, include_internal: bool = False) -> Dict[str, Any]:
        """Serialize for authenticated internal service responses."""
        return self.to_dict(
            include_internal=include_internal,
            include_metadata=True,
            include_private=True,
        )


# -----------------------------------------------------------------------------
# SQLAlchemy lifecycle listeners
# -----------------------------------------------------------------------------


def _normalize_project_before_write(_mapper: Any, _connection: Any, target: Project) -> None:
    target.normalize_for_persistence()


def _install_project_model_listeners() -> bool:
    if sqlalchemy_event is None:
        return False
    try:
        for event_name in ("before_insert", "before_update"):
            if not sqlalchemy_event.contains(Project, event_name, _normalize_project_before_write):
                sqlalchemy_event.listen(
                    Project,
                    event_name,
                    _normalize_project_before_write,
                    propagate=True,
                )
        return True
    except Exception:
        return False


PROJECT_MODEL_LISTENERS_INSTALLED = _install_project_model_listeners()


__all__ = [
    "PROJECT_SCHEMA_VERSION",
    "DEV_PROJECT_OWNER_AUTH_USER_ID",
    "DEFAULT_PROJECT_OWNER_USER_ID",
    "PROJECT_OWNER_TYPE_USER",
    "VALID_PROJECT_OWNER_TYPES",
    "PROJECT_STATUS_ACTIVE",
    "PROJECT_STATUS_ARCHIVED",
    "PROJECT_STATUS_DELETED",
    "VALID_PROJECT_STATUSES",
    "PROVISIONING_STATUS_DISABLED",
    "PROVISIONING_STATUS_PENDING",
    "PROVISIONING_STATUS_PROVISIONING",
    "PROVISIONING_STATUS_READY",
    "PROVISIONING_STATUS_FALLBACK_READY",
    "PROVISIONING_STATUS_FAILED",
    "PROVISIONING_STATUS_REPAIR_REQUIRED",
    "VALID_PROVISIONING_STATUSES",
    "ACCESS_SYNC_STATUS_DISABLED",
    "ACCESS_SYNC_STATUS_PENDING",
    "ACCESS_SYNC_STATUS_SYNCING",
    "ACCESS_SYNC_STATUS_READY",
    "ACCESS_SYNC_STATUS_FAILED",
    "ACCESS_SYNC_STATUS_REPAIR_REQUIRED",
    "VALID_ACCESS_SYNC_STATUSES",
    "WORLD_TEMPLATE_EARTH",
    "WORLD_TEMPLATE_FLAT",
    "VALID_WORLD_TEMPLATES",
    "PROJECT_ACCESS_PROJECTION_VERSION",
    "Project",
    "utc_now",
    "datetime_to_iso",
    "normalize_datetime",
    "make_json_safe",
    "normalize_optional_text",
    "normalize_required_text",
    "normalize_public_id",
    "normalize_project_id",
    "normalize_external_app_project_id",
    "normalize_slug",
    "normalize_status",
    "normalize_provisioning_status",
    "normalize_access_sync_status",
    "normalize_world_template",
    "normalize_request_identifier",
    "normalize_error_code",
    "normalize_external_url",
    "normalize_auth_user_id",
    "normalize_external_user_id",
    "normalize_owner_type",
    "normalize_owner_pair",
    "normalize_metadata",
    "sanitize_project_metadata",
    "build_project_state_fingerprint",
    "generate_project_id",
    "get_project_normalization_cache_info",
    "reset_project_normalization_caches",
    "PROJECT_MODEL_LISTENERS_INSTALLED",
]
