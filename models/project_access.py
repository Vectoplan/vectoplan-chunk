# services/vectoplan-chunk/models/project_access.py
"""
Persistente Projektzugriffs-Grundlage für ``vectoplan-chunk``.

Dieses Modul speichert ausschließlich vorbereitende Projektrollen, Gruppen,
Gruppenmitgliedschaften und Rollenzuweisungen. Es führt bewusst noch keine
Authentifizierung oder Autorisierung aus.

Architekturregeln
-----------------
* Benutzer-IDs sind externe, nicht verknüpfte String-IDs.
* Es existiert keine Foreign-Key-Beziehung zu ``vectoplan-auth`` oder
  ``vectoplan-app``.
* Alle Datensätze sind strikt über ``project_db_id`` projektgescopt.
* Models führen keine Commits oder Rollbacks aus.
* Models führen keine Datenbankabfragen aus.
* Berechtigungsdaten werden gespeichert, aber in diesem Modul nicht ausgewertet.
* Soft-Delete und fachliche Statuswechsel erhalten Historie.
* Öffentliche IDs und interne Datenbank-IDs bleiben getrennt.
* Reine Normalisierungen dürfen gecacht werden; ORM-Objekte und DB-Zustände
  werden niemals prozesslokal gecacht.

Die spätere Service-Schicht ist verantwortlich für:
* das atomare Erzeugen der Standardrollen,
* die Owner-Zuweisung,
* projektübergreifende Konsistenzprüfungen,
* Lookups,
* Transaktionsgrenzen,
* Berechnung effektiver Rechte,
* Synchronisation mit anderen Microservices.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, ClassVar, Final, Optional

from sqlalchemy import CheckConstraint, Index, UniqueConstraint, func
from sqlalchemy.orm import validates

from extensions import db

try:
    from sqlalchemy.dialects.postgresql import JSONB
except Exception:  # pragma: no cover - SQLAlchemy ohne PostgreSQL-Dialekt
    JSONB = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Modulvertrag und Konstanten
# ---------------------------------------------------------------------------

PROJECT_ACCESS_SCHEMA_VERSION: Final[int] = 1

ROLE_STATUS_ACTIVE: Final[str] = "active"
ROLE_STATUS_INACTIVE: Final[str] = "inactive"
ROLE_STATUS_ARCHIVED: Final[str] = "archived"
ROLE_STATUS_DELETED: Final[str] = "deleted"
ROLE_STATUSES: Final[frozenset[str]] = frozenset(
    {
        ROLE_STATUS_ACTIVE,
        ROLE_STATUS_INACTIVE,
        ROLE_STATUS_ARCHIVED,
        ROLE_STATUS_DELETED,
    }
)

GROUP_STATUS_ACTIVE: Final[str] = "active"
GROUP_STATUS_INACTIVE: Final[str] = "inactive"
GROUP_STATUS_ARCHIVED: Final[str] = "archived"
GROUP_STATUS_DELETED: Final[str] = "deleted"
GROUP_STATUSES: Final[frozenset[str]] = frozenset(
    {
        GROUP_STATUS_ACTIVE,
        GROUP_STATUS_INACTIVE,
        GROUP_STATUS_ARCHIVED,
        GROUP_STATUS_DELETED,
    }
)

MEMBERSHIP_STATUS_ACTIVE: Final[str] = "active"
MEMBERSHIP_STATUS_INACTIVE: Final[str] = "inactive"
MEMBERSHIP_STATUS_REMOVED: Final[str] = "removed"
MEMBERSHIP_STATUS_DELETED: Final[str] = "deleted"
MEMBERSHIP_STATUSES: Final[frozenset[str]] = frozenset(
    {
        MEMBERSHIP_STATUS_ACTIVE,
        MEMBERSHIP_STATUS_INACTIVE,
        MEMBERSHIP_STATUS_REMOVED,
        MEMBERSHIP_STATUS_DELETED,
    }
)

ASSIGNMENT_STATUS_ACTIVE: Final[str] = "active"
ASSIGNMENT_STATUS_INACTIVE: Final[str] = "inactive"
ASSIGNMENT_STATUS_REVOKED: Final[str] = "revoked"
ASSIGNMENT_STATUS_DELETED: Final[str] = "deleted"
ASSIGNMENT_STATUSES: Final[frozenset[str]] = frozenset(
    {
        ASSIGNMENT_STATUS_ACTIVE,
        ASSIGNMENT_STATUS_INACTIVE,
        ASSIGNMENT_STATUS_REVOKED,
        ASSIGNMENT_STATUS_DELETED,
    }
)

SUBJECT_TYPE_USER: Final[str] = "user"
SUBJECT_TYPE_GROUP: Final[str] = "group"
ASSIGNMENT_SUBJECT_TYPES: Final[frozenset[str]] = frozenset(
    {SUBJECT_TYPE_USER, SUBJECT_TYPE_GROUP}
)

DEFAULT_ROLE_OWNER: Final[str] = "owner"
DEFAULT_ROLE_ADMIN: Final[str] = "admin"
DEFAULT_ROLE_EDITOR: Final[str] = "editor"
DEFAULT_ROLE_VIEWER: Final[str] = "viewer"
DEFAULT_PROJECT_ROLE_KEYS: Final[tuple[str, ...]] = (
    DEFAULT_ROLE_OWNER,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_EDITOR,
    DEFAULT_ROLE_VIEWER,
)

KNOWN_PERMISSION_KEYS: Final[tuple[str, ...]] = (
    "view",
    "edit",
    "manage",
    "delete",
    "transfer",
    "embed",
    "view_settings",
    "manage_settings",
    "view_team",
    "manage_team",
    "view_admin",
)

MAX_PUBLIC_ID_LENGTH: Final[int] = 96
MAX_KEY_LENGTH: Final[int] = 80
MAX_NAME_LENGTH: Final[int] = 160
MAX_DESCRIPTION_LENGTH: Final[int] = 4000
MAX_EXTERNAL_USER_ID_LENGTH: Final[int] = 191
MAX_SOURCE_LENGTH: Final[int] = 80
MAX_REASON_LENGTH: Final[int] = 1000

_PUBLIC_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
)
_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_PERMISSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_CONTROL_CHARACTER_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")

_MISSING: Final[object] = object()


_DEFAULT_PROJECT_ROLE_DEFINITIONS: Final[tuple[dict[str, Any], ...]] = (
    {
        "roleKey": DEFAULT_ROLE_OWNER,
        "name": "Owner",
        "description": "Projektbesitzer mit vollständiger Projektverwaltung.",
        "isSystem": True,
        "permissions": {
            "version": 1,
            "allow": list(KNOWN_PERMISSION_KEYS),
            "deny": [],
        },
    },
    {
        "roleKey": DEFAULT_ROLE_ADMIN,
        "name": "Admin",
        "description": "Projektadministration ohne automatische Eigentumsübertragung.",
        "isSystem": True,
        "permissions": {
            "version": 1,
            "allow": [
                "view",
                "edit",
                "manage",
                "delete",
                "embed",
                "view_settings",
                "manage_settings",
                "view_team",
                "manage_team",
                "view_admin",
            ],
            "deny": ["transfer"],
        },
    },
    {
        "roleKey": DEFAULT_ROLE_EDITOR,
        "name": "Editor",
        "description": "Projektmitarbeiter mit Lese- und Bearbeitungszugriff.",
        "isSystem": True,
        "permissions": {
            "version": 1,
            "allow": ["view", "edit", "embed"],
            "deny": [],
        },
    },
    {
        "roleKey": DEFAULT_ROLE_VIEWER,
        "name": "Viewer",
        "description": "Nur-Lese-Rolle für ein Projekt.",
        "isSystem": True,
        "permissions": {
            "version": 1,
            "allow": ["view"],
            "deny": [],
        },
    },
)


# ---------------------------------------------------------------------------
# Robuste, frameworkarme Hilfsfunktionen
# ---------------------------------------------------------------------------


def _json_column_type() -> Any:
    """Liefert JSONB für PostgreSQL und einen portablen JSON-Fallback."""

    if JSONB is not None:
        try:
            return JSONB().with_variant(db.JSON(), "sqlite")
        except Exception:
            pass
    return db.JSON()


def _bigint_column_type() -> Any:
    """Verwendet BigInteger produktiv und SQLite-Integer für lokale Werkzeuge."""

    try:
        return db.BigInteger().with_variant(db.Integer(), "sqlite")
    except Exception:
        return db.BigInteger()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_isoformat(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            current = value
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return current.astimezone(timezone.utc).isoformat()
        isoformat = getattr(value, "isoformat", None)
        if callable(isoformat):
            return str(isoformat())
    except Exception:
        return None
    return None


def _make_public_id(prefix: str) -> str:
    normalized_prefix = _normalize_required_text(
        prefix,
        field_name="prefix",
        max_length=24,
    )
    return f"{normalized_prefix}{uuid.uuid4().hex}"


def _normalize_required_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    if value is None:
        raise ValueError(f"{field_name} is required")
    try:
        normalized = str(value).strip()
    except Exception as exc:
        raise ValueError(f"{field_name} must be text-compatible") from exc
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if _CONTROL_CHARACTER_PATTERN.search(normalized):
        raise ValueError(f"{field_name} contains control characters")
    if len(normalized) > max_length:
        raise ValueError(
            f"{field_name} exceeds maximum length {max_length}"
        )
    return normalized


def _normalize_optional_text(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> Optional[str]:
    if value is None:
        return None
    try:
        normalized = str(value).strip()
    except Exception as exc:
        raise ValueError(f"{field_name} must be text-compatible") from exc
    if not normalized:
        return None
    if _CONTROL_CHARACTER_PATTERN.search(normalized):
        raise ValueError(f"{field_name} contains control characters")
    if len(normalized) > max_length:
        raise ValueError(
            f"{field_name} exceeds maximum length {max_length}"
        )
    return normalized


@lru_cache(maxsize=512)
def _normalize_key_cached(raw_value: str) -> str:
    normalized = raw_value.strip().lower().replace(" ", "_")
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        raise ValueError("key must not be empty")
    if len(normalized) > MAX_KEY_LENGTH:
        raise ValueError(f"key exceeds maximum length {MAX_KEY_LENGTH}")
    if not _KEY_PATTERN.fullmatch(normalized):
        raise ValueError(
            "key may only contain lowercase letters, digits, '.', '_', ':' or '-'"
        )
    return normalized


def _normalize_key(value: Any, *, field_name: str) -> str:
    raw_value = _normalize_required_text(
        value,
        field_name=field_name,
        max_length=MAX_KEY_LENGTH,
    )
    try:
        return _normalize_key_cached(raw_value)
    except ValueError as exc:
        raise ValueError(f"{field_name}: {exc}") from exc


@lru_cache(maxsize=1024)
def _normalize_permission_cached(raw_value: str) -> str:
    normalized = raw_value.strip().lower().replace(" ", "_")
    normalized = re.sub(r"_+", "_", normalized)
    if not normalized:
        raise ValueError("permission name must not be empty")
    if len(normalized) > MAX_KEY_LENGTH:
        raise ValueError(
            f"permission name exceeds maximum length {MAX_KEY_LENGTH}"
        )
    if not _PERMISSION_PATTERN.fullmatch(normalized):
        raise ValueError(
            "permission name may only contain lowercase letters, digits, "
            "'.', '_', ':' or '-'"
        )
    return normalized


def _normalize_permission_name(value: Any) -> str:
    raw_value = _normalize_required_text(
        value,
        field_name="permission",
        max_length=MAX_KEY_LENGTH,
    )
    return _normalize_permission_cached(raw_value)


@lru_cache(maxsize=256)
def _normalize_status_cached(raw_value: str, allowed_key: str) -> str:
    normalized = raw_value.strip().lower().replace(" ", "_")
    allowed_map = {
        "role": ROLE_STATUSES,
        "group": GROUP_STATUSES,
        "membership": MEMBERSHIP_STATUSES,
        "assignment": ASSIGNMENT_STATUSES,
    }
    allowed = allowed_map[allowed_key]
    if normalized not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(
            f"unsupported status {normalized!r}; expected one of: {allowed_values}"
        )
    return normalized


def _normalize_status(
    value: Any,
    *,
    field_name: str,
    allowed_key: str,
    default: str,
) -> str:
    raw_value = default if value is None else str(value)
    try:
        return _normalize_status_cached(raw_value, allowed_key)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{field_name}: {exc}") from exc


def _normalize_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    raise ValueError("value must be boolean-compatible")


def _normalize_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if normalized <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return normalized


def _normalize_external_user_id(
    value: Any,
    *,
    field_name: str = "user_id",
    required: bool = True,
) -> Optional[str]:
    normalizer = _normalize_required_text if required else _normalize_optional_text
    normalized = normalizer(
        value,
        field_name=field_name,
        max_length=MAX_EXTERNAL_USER_ID_LENGTH,
    )
    return normalized


def _normalize_public_id(
    value: Any,
    *,
    field_name: str,
    prefix: str,
) -> str:
    """Normalisiert eine stabile serviceeigene öffentliche ID."""

    normalized = (
        _make_public_id(prefix)
        if value is None
        else _normalize_required_text(
            value,
            field_name=field_name,
            max_length=MAX_PUBLIC_ID_LENGTH,
        )
    )
    if not _PUBLIC_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} may only contain letters, digits, '.', '_', ':' or '-', "
            "and must start with a letter or digit"
        )
    return normalized


def _normalize_datetime(
    value: Any,
    *,
    field_name: str,
    required: bool = False,
) -> Optional[datetime]:
    if value is None or value == "":
        if required:
            raise ValueError(f"{field_name} is required")
        return None

    if isinstance(value, datetime):
        normalized = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            if required:
                raise ValueError(f"{field_name} is required")
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            normalized = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be an ISO-8601 datetime"
            ) from exc
    else:
        raise ValueError(
            f"{field_name} must be a datetime or ISO-8601 string"
        )

    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc)


def _as_utc_datetime(value: Any, *, field_name: str) -> Optional[datetime]:
    """Liefert einen vergleichbaren UTC-Zeitwert oder ``None``."""

    normalized = _normalize_datetime(
        value,
        field_name=field_name,
        required=False,
    )
    if normalized is None:
        return None
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc)


def _make_json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 32:
        raise ValueError("JSON value exceeds maximum nesting depth")

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, datetime):
        return _safe_isoformat(value)

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            try:
                normalized_key = str(key)
            except Exception as exc:
                raise ValueError("JSON object contains an invalid key") from exc
            result[normalized_key] = _make_json_safe(item, depth=depth + 1)
        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        return [_make_json_safe(item, depth=depth + 1) for item in value]

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _make_json_safe(to_dict(), depth=depth + 1)
        except Exception as exc:
            raise ValueError("object.to_dict() did not return JSON-safe data") from exc

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"value of type {type(value).__name__} is not JSON serializable"
        ) from exc


def _normalize_json_object(
    value: Any,
    *,
    field_name: str,
    default: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    if value is None:
        return copy.deepcopy(dict(default or {}))
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    try:
        normalized = _make_json_safe(value)
    except ValueError as exc:
        raise ValueError(f"{field_name}: {exc}") from exc
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must normalize to a JSON object")
    return normalized


def _normalize_permission_collection(
    value: Any,
    *,
    field_name: str,
) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        source: Iterable[Any] = [value]
    elif isinstance(value, Iterable) and not isinstance(value, Mapping):
        source = value
    else:
        raise ValueError(f"{field_name} must be a list of permission names")

    normalized: list[str] = []
    seen: set[str] = set()

    for item in source:
        permission = _normalize_permission_name(item)
        if permission not in seen:
            seen.add(permission)
            normalized.append(permission)

    normalized.sort()
    return normalized


def _normalize_permissions_json(
    value: Any,
    *,
    field_name: str = "permissions_json",
) -> dict[str, Any]:
    if value is None:
        value = {}

    if isinstance(value, str):
        value = {"allow": [value]}
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        value = {"allow": list(value)}

    normalized = _normalize_json_object(
        value,
        field_name=field_name,
        default={"version": 1, "allow": [], "deny": []},
    )

    allow_source = normalized.get(
        "allow",
        normalized.get("permissions", normalized.get("grants", [])),
    )
    deny_source = normalized.get("deny", normalized.get("revokes", []))

    allow = _normalize_permission_collection(
        allow_source,
        field_name=f"{field_name}.allow",
    )
    deny = _normalize_permission_collection(
        deny_source,
        field_name=f"{field_name}.deny",
    )

    deny_set = set(deny)
    allow = [permission for permission in allow if permission not in deny_set]

    version_raw = normalized.get("version", 1)
    try:
        version = int(version_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}.version must be an integer") from exc
    if version < 1:
        raise ValueError(f"{field_name}.version must be at least 1")

    result = dict(normalized)
    result.pop("permissions", None)
    result.pop("grants", None)
    result.pop("revokes", None)
    result["version"] = version
    result["allow"] = allow
    result["deny"] = deny
    return result


def _payload_mapping(payload: Any) -> Mapping[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return payload
    raise ValueError("payload must be a JSON object")


def _payload_value(
    payload: Mapping[str, Any],
    *names: str,
    default: Any = _MISSING,
) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    if default is _MISSING:
        return None
    return default


def _normalize_subject_type(value: Any) -> str:
    normalized = _normalize_required_text(
        value,
        field_name="subject_type",
        max_length=16,
    ).lower()
    if normalized not in ASSIGNMENT_SUBJECT_TYPES:
        allowed = ", ".join(sorted(ASSIGNMENT_SUBJECT_TYPES))
        raise ValueError(f"subject_type must be one of: {allowed}")
    return normalized


def _build_subject_key(
    *,
    subject_type: str,
    user_id: Optional[str] = None,
    group_id: Optional[str] = None,
) -> str:
    normalized_type = _normalize_subject_type(subject_type)
    if normalized_type == SUBJECT_TYPE_USER:
        normalized_user_id = _normalize_external_user_id(
            user_id,
            field_name="user_id",
            required=True,
        )
        return f"user:{normalized_user_id}"

    normalized_group_id = _normalize_required_text(
        group_id,
        field_name="group_id",
        max_length=MAX_PUBLIC_ID_LENGTH,
    )
    return f"group:{normalized_group_id}"


def _validate_effective_window(
    *,
    starts_at: Optional[datetime],
    expires_at: Optional[datetime],
    errors: MutableMapping[str, str],
) -> None:
    """Validiert ein Zeitfenster robust über naive/aware DB-Zeitwerte hinweg."""

    try:
        normalized_start = _as_utc_datetime(
            starts_at,
            field_name="starts_at",
        )
    except ValueError as exc:
        errors["starts_at"] = str(exc)
        normalized_start = None

    try:
        normalized_expiry = _as_utc_datetime(
            expires_at,
            field_name="expires_at",
        )
    except ValueError as exc:
        errors["expires_at"] = str(exc)
        normalized_expiry = None

    if normalized_start is not None and normalized_expiry is not None:
        if normalized_expiry <= normalized_start:
            errors["expires_at"] = "must be later than starts_at"


@lru_cache(maxsize=1)
def _normalized_default_project_role_definitions() -> tuple[dict[str, Any], ...]:
    """Erzeugt den kanonischen unveränderlichen Standardrollenvertrag."""

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_definition in _DEFAULT_PROJECT_ROLE_DEFINITIONS:
        definition = copy.deepcopy(raw_definition)
        role_key = _normalize_key(
            definition.get("roleKey"),
            field_name="roleKey",
        )
        if role_key in seen:
            raise RuntimeError(f"duplicate default role key: {role_key}")
        seen.add(role_key)
        normalized.append(
            {
                "roleKey": role_key,
                "name": _normalize_required_text(
                    definition.get("name") or role_key.replace("_", " ").title(),
                    field_name="name",
                    max_length=MAX_NAME_LENGTH,
                ),
                "description": _normalize_optional_text(
                    definition.get("description"),
                    field_name="description",
                    max_length=MAX_DESCRIPTION_LENGTH,
                ),
                "isSystem": _normalize_bool(
                    definition.get("isSystem"),
                    default=True,
                ),
                "permissions": _normalize_permissions_json(
                    definition.get("permissions"),
                    field_name=f"default_role[{role_key}].permissions",
                ),
            }
        )

    missing = set(DEFAULT_PROJECT_ROLE_KEYS) - seen
    if missing:
        raise RuntimeError(
            f"default role definitions are incomplete: {sorted(missing)}"
        )
    return tuple(normalized)


def normalize_project_permissions(value: Any) -> dict[str, Any]:
    """Öffentliche reine Normalisierung für Rollen- und Overrideverträge."""

    return copy.deepcopy(
        _normalize_permissions_json(
            value,
            field_name="permissions",
        )
    )


def get_default_project_role_definitions() -> list[dict[str, Any]]:
    """Liefert tiefe Kopien bereits kanonisch normalisierter Standardrollen."""

    return copy.deepcopy(list(_normalized_default_project_role_definitions()))


def clear_project_access_normalization_caches() -> dict[str, Any]:
    """Leert ausschließlich reine Normalisierungs-Caches, niemals ORM-Daten."""

    before = {
        "key": _normalize_key_cached.cache_info()._asdict(),
        "permission": _normalize_permission_cached.cache_info()._asdict(),
        "status": _normalize_status_cached.cache_info()._asdict(),
        "defaultRoles": _normalized_default_project_role_definitions.cache_info()._asdict(),
    }
    _normalize_key_cached.cache_clear()
    _normalize_permission_cached.cache_clear()
    _normalize_status_cached.cache_clear()
    _normalized_default_project_role_definitions.cache_clear()
    return {
        "cleared": True,
        "before": before,
        "after": {
            "key": _normalize_key_cached.cache_info()._asdict(),
            "permission": _normalize_permission_cached.cache_info()._asdict(),
            "status": _normalize_status_cached.cache_info()._asdict(),
            "defaultRoles": _normalized_default_project_role_definitions.cache_info()._asdict(),
        },
    }


# ---------------------------------------------------------------------------
# Abstrakte gemeinsame Modelbasis
# ---------------------------------------------------------------------------


class ProjectAccessRecord(db.Model):
    """Gemeinsame persistente Basis ohne eigene Tabelle."""

    __abstract__ = True
    __allow_unmapped__ = True

    id = db.Column(
        _bigint_column_type(),
        primary_key=True,
        autoincrement=True,
    )

    schema_version = db.Column(
        db.Integer,
        nullable=False,
        default=PROJECT_ACCESS_SCHEMA_VERSION,
        server_default=str(PROJECT_ACCESS_SCHEMA_VERSION),
    )
    revision = db.Column(
        db.BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )

    created_by_user_id = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH),
        nullable=True,
        index=True,
    )
    updated_by_user_id = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH),
        nullable=True,
        index=True,
    )

    metadata_json = db.Column(
        _json_column_type(),
        nullable=False,
        default=dict,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
        onupdate=_utc_now,
    )
    deleted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    def __init__(self, **kwargs: Any) -> None:
        """Setzt ORM-Defaults bereits vor einer Factory-Validierung explizit."""

        now = _utc_now()
        if kwargs.get("schema_version") is None:
            kwargs["schema_version"] = PROJECT_ACCESS_SCHEMA_VERSION
        if kwargs.get("revision") is None:
            kwargs["revision"] = 1
        if kwargs.get("metadata_json") is None:
            kwargs["metadata_json"] = {}
        if kwargs.get("created_at") is None:
            kwargs["created_at"] = now
        if kwargs.get("updated_at") is None:
            kwargs["updated_at"] = now
        super().__init__(**kwargs)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None or getattr(self, "status", None) == "deleted"

    def ensure_not_deleted(self) -> None:
        if self.is_deleted:
            raise ValueError(f"{type(self).__name__} is deleted")

    def touch(
        self,
        *,
        updated_by_user_id: Any = None,
        increment_revision: bool = True,
    ) -> "ProjectAccessRecord":
        if increment_revision:
            try:
                current_revision = int(self.revision or 0)
            except (TypeError, ValueError):
                current_revision = 0
            self.revision = max(1, current_revision + 1)

        if updated_by_user_id is not None:
            self.updated_by_user_id = _normalize_external_user_id(
                updated_by_user_id,
                field_name="updated_by_user_id",
                required=True,
            )

        self.updated_at = _utc_now()
        return self

    def replace_metadata(
        self,
        value: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectAccessRecord":
        self.ensure_not_deleted()
        self.metadata_json = _normalize_json_object(
            value,
            field_name="metadata_json",
        )
        return self.touch(updated_by_user_id=updated_by_user_id)

    def update_metadata(
        self,
        patch: Any,
        *,
        updated_by_user_id: Any = None,
        remove_null_values: bool = False,
    ) -> "ProjectAccessRecord":
        self.ensure_not_deleted()
        normalized_patch = _normalize_json_object(
            patch,
            field_name="metadata_patch",
        )
        current = _normalize_json_object(
            self.metadata_json,
            field_name="metadata_json",
        )
        for key, value in normalized_patch.items():
            if remove_null_values and value is None:
                current.pop(key, None)
            else:
                current[key] = value
        self.metadata_json = current
        return self.touch(updated_by_user_id=updated_by_user_id)

    def soft_delete(
        self,
        *,
        deleted_by_user_id: Any = None,
    ) -> "ProjectAccessRecord":
        """Soft-Delete ist idempotent und erhöht die Revision nur bei Änderung."""

        changed = False
        if self.deleted_at is None:
            self.deleted_at = _utc_now()
            changed = True
        if hasattr(self, "status") and getattr(self, "status", None) != "deleted":
            setattr(self, "status", "deleted")
            changed = True
        if changed:
            self.touch(updated_by_user_id=deleted_by_user_id)
        return self

    def restore(
        self,
        *,
        restored_by_user_id: Any = None,
    ) -> "ProjectAccessRecord":
        """Stellt einen gelöschten Datensatz idempotent als aktiv wieder her."""

        changed = False
        if self.deleted_at is not None:
            self.deleted_at = None
            changed = True
        if hasattr(self, "status") and getattr(self, "status", None) != "active":
            setattr(self, "status", "active")
            changed = True
        if changed:
            self.touch(updated_by_user_id=restored_by_user_id)
        return self

    def _base_validation_errors(self) -> dict[str, str]:
        errors: dict[str, str] = {}

        try:
            if int(self.schema_version or 0) < 1:
                errors["schema_version"] = "must be at least 1"
        except (TypeError, ValueError):
            errors["schema_version"] = "must be an integer"

        try:
            if int(self.revision or 0) < 1:
                errors["revision"] = "must be at least 1"
        except (TypeError, ValueError):
            errors["revision"] = "must be an integer"

        try:
            _normalize_json_object(
                self.metadata_json,
                field_name="metadata_json",
            )
        except ValueError as exc:
            errors["metadata_json"] = str(exc)

        for field_name in ("created_by_user_id", "updated_by_user_id"):
            value = getattr(self, field_name, None)
            if value is None:
                continue
            try:
                _normalize_external_user_id(
                    value,
                    field_name=field_name,
                    required=False,
                )
            except ValueError as exc:
                errors[field_name] = str(exc)

        return errors

    def _base_dict(
        self,
        *,
        include_internal: bool,
        include_metadata: bool,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schemaVersion": int(self.schema_version or PROJECT_ACCESS_SCHEMA_VERSION),
            "revision": int(self.revision or 1),
            "createdByUserId": self.created_by_user_id,
            "updatedByUserId": self.updated_by_user_id,
            "createdAt": _safe_isoformat(self.created_at),
            "updatedAt": _safe_isoformat(self.updated_at),
            "deletedAt": _safe_isoformat(self.deleted_at),
            "isDeleted": self.is_deleted,
        }
        if include_metadata:
            result["metadata"] = _make_json_safe(self.metadata_json or {})
        if include_internal:
            result["id"] = self.id
        return result


# ---------------------------------------------------------------------------
# ProjectRole
# ---------------------------------------------------------------------------


class ProjectRole(ProjectAccessRecord):
    """Projektbezogene Rolle mit speicherbarem Berechtigungsvertrag."""

    __tablename__ = "project_roles"
    __allow_unmapped__ = True

    ID_PREFIX: ClassVar[str] = "chk_role_"

    role_id = db.Column(
        db.String(MAX_PUBLIC_ID_LENGTH),
        nullable=False,
    )
    project_db_id = db.Column(
        _bigint_column_type(),
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role_key = db.Column(
        db.String(MAX_KEY_LENGTH),
        nullable=False,
    )
    name = db.Column(
        db.String(MAX_NAME_LENGTH),
        nullable=False,
    )
    description = db.Column(
        db.Text,
        nullable=True,
    )

    permissions_json = db.Column(
        _json_column_type(),
        nullable=False,
        default=lambda: {"version": 1, "allow": [], "deny": []},
    )

    is_system = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    status = db.Column(
        db.String(32),
        nullable=False,
        default=ROLE_STATUS_ACTIVE,
        server_default=ROLE_STATUS_ACTIVE,
        index=True,
    )

    assignments = db.relationship(
        "ProjectRoleAssignment",
        back_populates="role",
        cascade="save-update, merge",
        passive_deletes=True,
        lazy="raise",
        foreign_keys="ProjectRoleAssignment.role_db_id",
    )

    __table_args__ = (
        UniqueConstraint(
            "project_db_id",
            "role_id",
            name="uq_prj_roles_project_role_id",
        ),
        UniqueConstraint(
            "project_db_id",
            "role_key",
            name="uq_prj_roles_project_role_key",
        ),
        CheckConstraint("project_db_id > 0", name="ck_prj_roles_project_positive"),
        CheckConstraint("role_id <> ''", name="ck_prj_roles_role_id_not_empty"),
        CheckConstraint("role_key <> ''", name="ck_prj_roles_role_key_not_empty"),
        CheckConstraint("name <> ''", name="ck_prj_roles_name_not_empty"),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_prj_roles_schema_version",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_prj_roles_revision",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'archived', 'deleted')",
            name="ck_prj_roles_status",
        ),
        Index(
            "ix_prj_roles_project_status",
            "project_db_id",
            "status",
        ),
        Index(
            "ix_prj_roles_project_deleted",
            "project_db_id",
            "deleted_at",
        ),
    )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: Any,
        role_key: Any,
        name: Any = None,
        role_id: Any = None,
        description: Any = None,
        permissions: Any = None,
        is_system: Any = False,
        status: Any = ROLE_STATUS_ACTIVE,
        created_by_user_id: Any = None,
        metadata: Any = None,
    ) -> "ProjectRole":
        normalized_role_key = _normalize_key(role_key, field_name="role_key")
        normalized_status = _normalize_status(
            status,
            field_name="status",
            allowed_key="role",
            default=ROLE_STATUS_ACTIVE,
        )
        normalized_actor = _normalize_external_user_id(
            created_by_user_id,
            field_name="created_by_user_id",
            required=False,
        )
        now = _utc_now()
        instance = cls(
            project_db_id=_normalize_positive_int(project_db_id, field_name="project_db_id"),
            role_id=_normalize_public_id(role_id, field_name="role_id", prefix=cls.ID_PREFIX),
            role_key=normalized_role_key,
            name=_normalize_required_text(
                name if name is not None else normalized_role_key.replace("_", " ").title(),
                field_name="name",
                max_length=MAX_NAME_LENGTH,
            ),
            description=_normalize_optional_text(
                description,
                field_name="description",
                max_length=MAX_DESCRIPTION_LENGTH,
            ),
            permissions_json=_normalize_permissions_json(permissions, field_name="permissions"),
            is_system=_normalize_bool(is_system, default=False),
            status=normalized_status,
            schema_version=PROJECT_ACCESS_SCHEMA_VERSION,
            revision=1,
            created_by_user_id=normalized_actor,
            updated_by_user_id=normalized_actor,
            metadata_json=_normalize_json_object(metadata, field_name="metadata"),
            created_at=now,
            updated_at=now,
            deleted_at=now if normalized_status == ROLE_STATUS_DELETED else None,
        )
        instance.validate_or_raise()
        return instance

    @classmethod
    def from_create_payload(
        cls,
        *,
        project_db_id: Any,
        payload: Any,
        created_by_user_id: Any = None,
    ) -> "ProjectRole":
        body = _payload_mapping(payload)
        return cls.create(
            project_db_id=project_db_id,
            role_id=_payload_value(body, "roleId", "role_id"),
            role_key=_payload_value(body, "roleKey", "role_key", "key"),
            name=_payload_value(body, "name", "title"),
            description=_payload_value(body, "description"),
            permissions=_payload_value(
                body,
                "permissions",
                "permissionsJson",
                "permissions_json",
            ),
            is_system=_payload_value(
                body,
                "isSystem",
                "is_system",
                default=False,
            ),
            status=_payload_value(
                body,
                "status",
                default=ROLE_STATUS_ACTIVE,
            ),
            created_by_user_id=created_by_user_id
            if created_by_user_id is not None
            else _payload_value(
                body,
                "createdByUserId",
                "created_by_user_id",
            ),
            metadata=_payload_value(
                body,
                "metadata",
                "metadataJson",
                "metadata_json",
            ),
        )

    @validates("role_key")
    def _validate_role_key_column(self, _: str, value: Any) -> str:
        return _normalize_key(value, field_name="role_key")

    @validates("status")
    def _validate_status_column(self, _: str, value: Any) -> str:
        return _normalize_status(
            value,
            field_name="status",
            allowed_key="role",
            default=ROLE_STATUS_ACTIVE,
        )

    def set_permissions(
        self,
        value: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectRole":
        self.ensure_not_deleted()
        self.permissions_json = _normalize_permissions_json(
            value,
            field_name="permissions",
        )
        return self.touch(updated_by_user_id=updated_by_user_id)  # type: ignore[return-value]

    def _apply_status_without_touch(self, value: Any) -> bool:
        normalized = _normalize_status(
            value,
            field_name="status",
            allowed_key="role",
            default=ROLE_STATUS_ACTIVE,
        )
        changed = self.status != normalized
        self.status = normalized
        if normalized == ROLE_STATUS_DELETED:
            if self.deleted_at is None:
                self.deleted_at = _utc_now()
                changed = True
        elif self.deleted_at is not None:
            self.deleted_at = None
            changed = True
        return changed

    def set_status(
        self,
        value: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectRole":
        normalized = _normalize_status(
            value,
            field_name="status",
            allowed_key="role",
            default=ROLE_STATUS_ACTIVE,
        )
        if normalized != ROLE_STATUS_DELETED:
            self.ensure_not_deleted()
        if self._apply_status_without_touch(normalized):
            self.touch(updated_by_user_id=updated_by_user_id)
        return self

    def archive(self, *, updated_by_user_id: Any = None) -> "ProjectRole":
        return self.set_status(
            ROLE_STATUS_ARCHIVED,
            updated_by_user_id=updated_by_user_id,
        )

    def apply_patch_payload(
        self,
        payload: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectRole":
        self.ensure_not_deleted()
        body = _payload_mapping(payload)
        changed = False

        if "name" in body or "title" in body:
            normalized = _normalize_required_text(
                _payload_value(body, "name", "title"),
                field_name="name",
                max_length=MAX_NAME_LENGTH,
            )
            if self.name != normalized:
                self.name = normalized
                changed = True
        if "description" in body:
            normalized = _normalize_optional_text(
                body.get("description"),
                field_name="description",
                max_length=MAX_DESCRIPTION_LENGTH,
            )
            if self.description != normalized:
                self.description = normalized
                changed = True
        if any(key in body for key in ("permissions", "permissionsJson", "permissions_json")):
            normalized = _normalize_permissions_json(
                _payload_value(body, "permissions", "permissionsJson", "permissions_json"),
                field_name="permissions",
            )
            if self.permissions_json != normalized:
                self.permissions_json = normalized
                changed = True
        if "isSystem" in body or "is_system" in body:
            normalized = _normalize_bool(_payload_value(body, "isSystem", "is_system"))
            if bool(self.is_system) != normalized:
                self.is_system = normalized
                changed = True
        if "status" in body:
            changed = self._apply_status_without_touch(body["status"]) or changed
        if any(key in body for key in ("metadata", "metadataJson", "metadata_json")):
            normalized = _normalize_json_object(
                _payload_value(body, "metadata", "metadataJson", "metadata_json"),
                field_name="metadata",
            )
            if self.metadata_json != normalized:
                self.metadata_json = normalized
                changed = True

        if changed:
            self.touch(updated_by_user_id=updated_by_user_id)
        self.validate_or_raise()
        return self

    def get_validation_errors(self) -> dict[str, str]:
        errors = self._base_validation_errors()

        try:
            _normalize_positive_int(self.project_db_id, field_name="project_db_id")
        except ValueError as exc:
            errors["project_db_id"] = str(exc)
        try:
            _normalize_public_id(self.role_id, field_name="role_id", prefix=self.ID_PREFIX)
        except ValueError as exc:
            errors["role_id"] = str(exc)
        try:
            _normalize_key(self.role_key, field_name="role_key")
        except ValueError as exc:
            errors["role_key"] = str(exc)
        try:
            _normalize_required_text(self.name, field_name="name", max_length=MAX_NAME_LENGTH)
        except ValueError as exc:
            errors["name"] = str(exc)
        try:
            _normalize_optional_text(
                self.description,
                field_name="description",
                max_length=MAX_DESCRIPTION_LENGTH,
            )
        except ValueError as exc:
            errors["description"] = str(exc)
        try:
            _normalize_permissions_json(self.permissions_json, field_name="permissions_json")
        except ValueError as exc:
            errors["permissions_json"] = str(exc)
        try:
            _normalize_bool(self.is_system, default=False)
        except ValueError as exc:
            errors["is_system"] = str(exc)
        try:
            normalized_status = _normalize_status(
                self.status,
                field_name="status",
                allowed_key="role",
                default=ROLE_STATUS_ACTIVE,
            )
        except ValueError as exc:
            errors["status"] = str(exc)
            normalized_status = None

        if normalized_status == ROLE_STATUS_DELETED and self.deleted_at is None:
            errors["deleted_at"] = "is required when status is deleted"
        if normalized_status not in {None, ROLE_STATUS_DELETED} and self.deleted_at is not None:
            errors["deleted_at"] = "must be empty unless status is deleted"
        return errors

    def validate_or_raise(self) -> "ProjectRole":
        errors = self.get_validation_errors()
        if errors:
            raise ValueError(f"invalid ProjectRole: {errors}")
        return self

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
        include_assignments: bool = False,
    ) -> dict[str, Any]:
        result = self._base_dict(
            include_internal=include_internal,
            include_metadata=include_metadata,
        )
        result.update(
            {
                "roleId": self.role_id,
                "roleKey": self.role_key,
                "name": self.name,
                "description": self.description,
                "permissions": _make_json_safe(self.permissions_json or {}),
                "isSystem": bool(self.is_system),
                "status": self.status,
            }
        )
        if include_internal:
            result["projectDbId"] = self.project_db_id
        if include_assignments:
            try:
                result["assignments"] = [
                    assignment.to_dict(
                        include_internal=include_internal,
                        include_metadata=include_metadata,
                    )
                    for assignment in list(self.assignments or [])
                ]
            except Exception:
                result["assignments"] = []
                result["assignmentsUnavailable"] = True
        return result

    def __repr__(self) -> str:
        return (
            f"<ProjectRole role_id={self.role_id!r} "
            f"project_db_id={self.project_db_id!r} "
            f"role_key={self.role_key!r} status={self.status!r}>"
        )


# ---------------------------------------------------------------------------
# ProjectGroup
# ---------------------------------------------------------------------------


class ProjectGroup(ProjectAccessRecord):
    """Projektbezogene Benutzergruppe ohne eigene Authentitätswahrheit."""

    __tablename__ = "project_groups"
    __allow_unmapped__ = True

    ID_PREFIX: ClassVar[str] = "chk_group_"

    group_id = db.Column(
        db.String(MAX_PUBLIC_ID_LENGTH),
        nullable=False,
    )
    project_db_id = db.Column(
        _bigint_column_type(),
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    group_key = db.Column(
        db.String(MAX_KEY_LENGTH),
        nullable=False,
    )
    name = db.Column(
        db.String(MAX_NAME_LENGTH),
        nullable=False,
    )
    description = db.Column(
        db.Text,
        nullable=True,
    )

    is_system = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    status = db.Column(
        db.String(32),
        nullable=False,
        default=GROUP_STATUS_ACTIVE,
        server_default=GROUP_STATUS_ACTIVE,
        index=True,
    )

    members = db.relationship(
        "ProjectGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
        foreign_keys="ProjectGroupMember.group_db_id",
    )
    role_assignments = db.relationship(
        "ProjectRoleAssignment",
        back_populates="group",
        cascade="save-update, merge",
        passive_deletes=True,
        lazy="raise",
        foreign_keys="ProjectRoleAssignment.group_db_id",
    )

    __table_args__ = (
        UniqueConstraint(
            "project_db_id",
            "group_id",
            name="uq_prj_groups_project_group_id",
        ),
        UniqueConstraint(
            "project_db_id",
            "group_key",
            name="uq_prj_groups_project_group_key",
        ),
        CheckConstraint("project_db_id > 0", name="ck_prj_groups_project_positive"),
        CheckConstraint("group_id <> ''", name="ck_prj_groups_group_id_not_empty"),
        CheckConstraint("group_key <> ''", name="ck_prj_groups_group_key_not_empty"),
        CheckConstraint("name <> ''", name="ck_prj_groups_name_not_empty"),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_prj_groups_schema_version",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_prj_groups_revision",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'archived', 'deleted')",
            name="ck_prj_groups_status",
        ),
        Index(
            "ix_prj_groups_project_status",
            "project_db_id",
            "status",
        ),
        Index(
            "ix_prj_groups_project_deleted",
            "project_db_id",
            "deleted_at",
        ),
    )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: Any,
        group_key: Any,
        name: Any = None,
        group_id: Any = None,
        description: Any = None,
        is_system: Any = False,
        status: Any = GROUP_STATUS_ACTIVE,
        created_by_user_id: Any = None,
        metadata: Any = None,
    ) -> "ProjectGroup":
        normalized_group_key = _normalize_key(group_key, field_name="group_key")
        normalized_status = _normalize_status(
            status,
            field_name="status",
            allowed_key="group",
            default=GROUP_STATUS_ACTIVE,
        )
        normalized_actor = _normalize_external_user_id(
            created_by_user_id,
            field_name="created_by_user_id",
            required=False,
        )
        now = _utc_now()
        instance = cls(
            project_db_id=_normalize_positive_int(project_db_id, field_name="project_db_id"),
            group_id=_normalize_public_id(group_id, field_name="group_id", prefix=cls.ID_PREFIX),
            group_key=normalized_group_key,
            name=_normalize_required_text(
                name if name is not None else normalized_group_key.replace("_", " ").title(),
                field_name="name",
                max_length=MAX_NAME_LENGTH,
            ),
            description=_normalize_optional_text(
                description,
                field_name="description",
                max_length=MAX_DESCRIPTION_LENGTH,
            ),
            is_system=_normalize_bool(is_system, default=False),
            status=normalized_status,
            schema_version=PROJECT_ACCESS_SCHEMA_VERSION,
            revision=1,
            created_by_user_id=normalized_actor,
            updated_by_user_id=normalized_actor,
            metadata_json=_normalize_json_object(metadata, field_name="metadata"),
            created_at=now,
            updated_at=now,
            deleted_at=now if normalized_status == GROUP_STATUS_DELETED else None,
        )
        instance.validate_or_raise()
        return instance

    @classmethod
    def from_create_payload(
        cls,
        *,
        project_db_id: Any,
        payload: Any,
        created_by_user_id: Any = None,
    ) -> "ProjectGroup":
        body = _payload_mapping(payload)
        return cls.create(
            project_db_id=project_db_id,
            group_id=_payload_value(body, "groupId", "group_id"),
            group_key=_payload_value(body, "groupKey", "group_key", "key"),
            name=_payload_value(body, "name", "title"),
            description=_payload_value(body, "description"),
            is_system=_payload_value(
                body,
                "isSystem",
                "is_system",
                default=False,
            ),
            status=_payload_value(
                body,
                "status",
                default=GROUP_STATUS_ACTIVE,
            ),
            created_by_user_id=created_by_user_id
            if created_by_user_id is not None
            else _payload_value(
                body,
                "createdByUserId",
                "created_by_user_id",
            ),
            metadata=_payload_value(
                body,
                "metadata",
                "metadataJson",
                "metadata_json",
            ),
        )

    @validates("group_key")
    def _validate_group_key_column(self, _: str, value: Any) -> str:
        return _normalize_key(value, field_name="group_key")

    @validates("status")
    def _validate_status_column(self, _: str, value: Any) -> str:
        return _normalize_status(
            value,
            field_name="status",
            allowed_key="group",
            default=GROUP_STATUS_ACTIVE,
        )

    def _apply_status_without_touch(self, value: Any) -> bool:
        normalized = _normalize_status(
            value,
            field_name="status",
            allowed_key="group",
            default=GROUP_STATUS_ACTIVE,
        )
        changed = self.status != normalized
        self.status = normalized
        if normalized == GROUP_STATUS_DELETED:
            if self.deleted_at is None:
                self.deleted_at = _utc_now()
                changed = True
        elif self.deleted_at is not None:
            self.deleted_at = None
            changed = True
        return changed

    def set_status(
        self,
        value: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectGroup":
        normalized = _normalize_status(
            value,
            field_name="status",
            allowed_key="group",
            default=GROUP_STATUS_ACTIVE,
        )
        if normalized != GROUP_STATUS_DELETED:
            self.ensure_not_deleted()
        if self._apply_status_without_touch(normalized):
            self.touch(updated_by_user_id=updated_by_user_id)
        return self

    def archive(self, *, updated_by_user_id: Any = None) -> "ProjectGroup":
        return self.set_status(
            GROUP_STATUS_ARCHIVED,
            updated_by_user_id=updated_by_user_id,
        )

    def apply_patch_payload(
        self,
        payload: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectGroup":
        self.ensure_not_deleted()
        body = _payload_mapping(payload)
        changed = False

        if "name" in body or "title" in body:
            normalized = _normalize_required_text(
                _payload_value(body, "name", "title"),
                field_name="name",
                max_length=MAX_NAME_LENGTH,
            )
            if self.name != normalized:
                self.name = normalized
                changed = True
        if "description" in body:
            normalized = _normalize_optional_text(
                body.get("description"),
                field_name="description",
                max_length=MAX_DESCRIPTION_LENGTH,
            )
            if self.description != normalized:
                self.description = normalized
                changed = True
        if "isSystem" in body or "is_system" in body:
            normalized = _normalize_bool(_payload_value(body, "isSystem", "is_system"))
            if bool(self.is_system) != normalized:
                self.is_system = normalized
                changed = True
        if "status" in body:
            changed = self._apply_status_without_touch(body["status"]) or changed
        if any(key in body for key in ("metadata", "metadataJson", "metadata_json")):
            normalized = _normalize_json_object(
                _payload_value(body, "metadata", "metadataJson", "metadata_json"),
                field_name="metadata",
            )
            if self.metadata_json != normalized:
                self.metadata_json = normalized
                changed = True

        if changed:
            self.touch(updated_by_user_id=updated_by_user_id)
        self.validate_or_raise()
        return self

    def get_validation_errors(self) -> dict[str, str]:
        errors = self._base_validation_errors()

        try:
            _normalize_positive_int(self.project_db_id, field_name="project_db_id")
        except ValueError as exc:
            errors["project_db_id"] = str(exc)
        try:
            _normalize_public_id(self.group_id, field_name="group_id", prefix=self.ID_PREFIX)
        except ValueError as exc:
            errors["group_id"] = str(exc)
        try:
            _normalize_key(self.group_key, field_name="group_key")
        except ValueError as exc:
            errors["group_key"] = str(exc)
        try:
            _normalize_required_text(self.name, field_name="name", max_length=MAX_NAME_LENGTH)
        except ValueError as exc:
            errors["name"] = str(exc)
        try:
            _normalize_optional_text(
                self.description,
                field_name="description",
                max_length=MAX_DESCRIPTION_LENGTH,
            )
        except ValueError as exc:
            errors["description"] = str(exc)
        try:
            _normalize_bool(self.is_system, default=False)
        except ValueError as exc:
            errors["is_system"] = str(exc)
        try:
            normalized_status = _normalize_status(
                self.status,
                field_name="status",
                allowed_key="group",
                default=GROUP_STATUS_ACTIVE,
            )
        except ValueError as exc:
            errors["status"] = str(exc)
            normalized_status = None

        if normalized_status == GROUP_STATUS_DELETED and self.deleted_at is None:
            errors["deleted_at"] = "is required when status is deleted"
        if normalized_status not in {None, GROUP_STATUS_DELETED} and self.deleted_at is not None:
            errors["deleted_at"] = "must be empty unless status is deleted"
        return errors

    def validate_or_raise(self) -> "ProjectGroup":
        errors = self.get_validation_errors()
        if errors:
            raise ValueError(f"invalid ProjectGroup: {errors}")
        return self

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
        include_members: bool = False,
        include_role_assignments: bool = False,
    ) -> dict[str, Any]:
        result = self._base_dict(
            include_internal=include_internal,
            include_metadata=include_metadata,
        )
        result.update(
            {
                "groupId": self.group_id,
                "groupKey": self.group_key,
                "name": self.name,
                "description": self.description,
                "isSystem": bool(self.is_system),
                "status": self.status,
            }
        )
        if include_internal:
            result["projectDbId"] = self.project_db_id

        if include_members:
            try:
                result["members"] = [
                    member.to_dict(
                        include_internal=include_internal,
                        include_metadata=include_metadata,
                    )
                    for member in list(self.members or [])
                ]
            except Exception:
                result["members"] = []
                result["membersUnavailable"] = True

        if include_role_assignments:
            try:
                result["roleAssignments"] = [
                    assignment.to_dict(
                        include_internal=include_internal,
                        include_metadata=include_metadata,
                    )
                    for assignment in list(self.role_assignments or [])
                ]
            except Exception:
                result["roleAssignments"] = []
                result["roleAssignmentsUnavailable"] = True

        return result

    def __repr__(self) -> str:
        return (
            f"<ProjectGroup group_id={self.group_id!r} "
            f"project_db_id={self.project_db_id!r} "
            f"group_key={self.group_key!r} status={self.status!r}>"
        )


# ---------------------------------------------------------------------------
# ProjectGroupMember
# ---------------------------------------------------------------------------


class ProjectGroupMember(ProjectAccessRecord):
    """Zuordnung einer externen User-ID zu einer projektbezogenen Gruppe."""

    __tablename__ = "project_group_members"
    __allow_unmapped__ = True

    ID_PREFIX: ClassVar[str] = "chk_gm_"

    membership_id = db.Column(
        db.String(MAX_PUBLIC_ID_LENGTH),
        nullable=False,
        unique=True,
    )
    project_db_id = db.Column(
        _bigint_column_type(),
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_db_id = db.Column(
        _bigint_column_type(),
        db.ForeignKey("project_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Denormalisierte, stabile öffentliche Gruppen-ID für API, Audit und
    # konfliktfreie Serialisierung ohne erzwungenen Relationship-Load.
    group_id = db.Column(
        db.String(MAX_PUBLIC_ID_LENGTH),
        nullable=False,
    )
    user_id = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH),
        nullable=False,
        index=True,
    )

    status = db.Column(
        db.String(32),
        nullable=False,
        default=MEMBERSHIP_STATUS_ACTIVE,
        server_default=MEMBERSHIP_STATUS_ACTIVE,
        index=True,
    )

    added_by_user_id = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH),
        nullable=True,
        index=True,
    )
    removed_by_user_id = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH),
        nullable=True,
        index=True,
    )
    starts_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    removed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    removal_reason = db.Column(
        db.String(MAX_REASON_LENGTH),
        nullable=True,
    )

    group = db.relationship(
        "ProjectGroup",
        back_populates="members",
        lazy="raise",
        foreign_keys=[group_db_id],
    )

    __table_args__ = (
        UniqueConstraint(
            "project_db_id",
            "group_db_id",
            "user_id",
            name="uq_prj_group_members_project_group_user",
        ),
        CheckConstraint("project_db_id > 0", name="ck_prj_group_members_project_positive"),
        CheckConstraint("group_db_id > 0", name="ck_prj_group_members_group_positive"),
        CheckConstraint("membership_id <> ''", name="ck_prj_group_members_id_not_empty"),
        CheckConstraint("group_id <> ''", name="ck_prj_group_members_group_id_not_empty"),
        CheckConstraint("user_id <> ''", name="ck_prj_group_members_user_id_not_empty"),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_prj_group_members_schema_version",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_prj_group_members_revision",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'removed', 'deleted')",
            name="ck_prj_group_members_status",
        ),
        Index(
            "ix_prj_group_members_project_user",
            "project_db_id",
            "user_id",
        ),
        Index(
            "ix_prj_group_members_group_status",
            "group_db_id",
            "status",
        ),
        Index(
            "ix_prj_group_members_project_deleted",
            "project_db_id",
            "deleted_at",
        ),
    )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: Any,
        group_db_id: Any,
        group_id: Any,
        user_id: Any,
        membership_id: Any = None,
        status: Any = MEMBERSHIP_STATUS_ACTIVE,
        added_by_user_id: Any = None,
        starts_at: Any = None,
        expires_at: Any = None,
        removed_by_user_id: Any = None,
        removed_at: Any = None,
        removal_reason: Any = None,
        metadata: Any = None,
    ) -> "ProjectGroupMember":
        normalized_status = _normalize_status(
            status,
            field_name="status",
            allowed_key="membership",
            default=MEMBERSHIP_STATUS_ACTIVE,
        )
        normalized_added_by = _normalize_external_user_id(
            added_by_user_id,
            field_name="added_by_user_id",
            required=False,
        )
        normalized_removed_by = _normalize_external_user_id(
            removed_by_user_id,
            field_name="removed_by_user_id",
            required=False,
        )
        normalized_start = _normalize_datetime(starts_at, field_name="starts_at")
        normalized_expiry = _normalize_datetime(expires_at, field_name="expires_at")
        window_errors: dict[str, str] = {}
        _validate_effective_window(
            starts_at=normalized_start,
            expires_at=normalized_expiry,
            errors=window_errors,
        )
        if window_errors:
            raise ValueError(f"invalid membership window: {window_errors}")
        now = _utc_now()
        normalized_removed_at = _normalize_datetime(
            removed_at,
            field_name="removed_at",
        )
        if normalized_status == MEMBERSHIP_STATUS_REMOVED and normalized_removed_at is None:
            normalized_removed_at = now
        instance = cls(
            project_db_id=_normalize_positive_int(project_db_id, field_name="project_db_id"),
            group_db_id=_normalize_positive_int(group_db_id, field_name="group_db_id"),
            group_id=_normalize_public_id(group_id, field_name="group_id", prefix="chk_group_"),
            membership_id=_normalize_public_id(
                membership_id,
                field_name="membership_id",
                prefix=cls.ID_PREFIX,
            ),
            user_id=_normalize_external_user_id(user_id, field_name="user_id", required=True),
            status=normalized_status,
            added_by_user_id=normalized_added_by,
            removed_by_user_id=normalized_removed_by,
            starts_at=normalized_start,
            expires_at=normalized_expiry,
            removed_at=normalized_removed_at,
            removal_reason=_normalize_optional_text(
                removal_reason,
                field_name="removal_reason",
                max_length=MAX_REASON_LENGTH,
            ),
            schema_version=PROJECT_ACCESS_SCHEMA_VERSION,
            revision=1,
            created_by_user_id=normalized_added_by,
            updated_by_user_id=normalized_removed_by or normalized_added_by,
            metadata_json=_normalize_json_object(metadata, field_name="metadata"),
            created_at=now,
            updated_at=now,
            deleted_at=now if normalized_status == MEMBERSHIP_STATUS_DELETED else None,
        )
        instance.validate_or_raise()
        return instance

    @classmethod
    def from_create_payload(
        cls,
        *,
        project_db_id: Any,
        group_db_id: Any,
        group_id: Any,
        payload: Any,
        added_by_user_id: Any = None,
    ) -> "ProjectGroupMember":
        body = _payload_mapping(payload)
        return cls.create(
            project_db_id=project_db_id,
            group_db_id=group_db_id,
            group_id=group_id,
            membership_id=_payload_value(body, "membershipId", "membership_id"),
            user_id=_payload_value(
                body,
                "userId",
                "user_id",
                "memberUserId",
                "member_user_id",
            ),
            status=_payload_value(body, "status", default=MEMBERSHIP_STATUS_ACTIVE),
            added_by_user_id=(
                added_by_user_id
                if added_by_user_id is not None
                else _payload_value(body, "addedByUserId", "added_by_user_id")
            ),
            starts_at=_payload_value(body, "startsAt", "starts_at"),
            expires_at=_payload_value(body, "expiresAt", "expires_at"),
            removed_by_user_id=_payload_value(
                body,
                "removedByUserId",
                "removed_by_user_id",
            ),
            removed_at=_payload_value(body, "removedAt", "removed_at"),
            removal_reason=_payload_value(body, "removalReason", "removal_reason"),
            metadata=_payload_value(body, "metadata", "metadataJson", "metadata_json"),
        )

    @validates("status")
    def _validate_status_column(self, _: str, value: Any) -> str:
        return _normalize_status(
            value,
            field_name="status",
            allowed_key="membership",
            default=MEMBERSHIP_STATUS_ACTIVE,
        )

    @validates("user_id")
    def _validate_user_id_column(self, _: str, value: Any) -> str:
        normalized = _normalize_external_user_id(
            value,
            field_name="user_id",
            required=True,
        )
        assert normalized is not None
        return normalized

    def is_effective(self, *, at: Optional[datetime] = None) -> bool:
        if self.is_deleted or self.status != MEMBERSHIP_STATUS_ACTIVE:
            return False
        current = _as_utc_datetime(at or _utc_now(), field_name="at")
        starts_at = _as_utc_datetime(self.starts_at, field_name="starts_at")
        expires_at = _as_utc_datetime(self.expires_at, field_name="expires_at")
        assert current is not None
        if starts_at is not None and current < starts_at:
            return False
        if expires_at is not None and current >= expires_at:
            return False
        return True

    def remove(
        self,
        *,
        removed_by_user_id: Any = None,
        reason: Any = None,
    ) -> "ProjectGroupMember":
        self.ensure_not_deleted()
        normalized_actor = _normalize_external_user_id(
            removed_by_user_id,
            field_name="removed_by_user_id",
            required=False,
        )
        normalized_reason = _normalize_optional_text(
            reason,
            field_name="removal_reason",
            max_length=MAX_REASON_LENGTH,
        )
        changed = self.status != MEMBERSHIP_STATUS_REMOVED
        self.status = MEMBERSHIP_STATUS_REMOVED
        if self.removed_at is None:
            self.removed_at = _utc_now()
            changed = True
        if normalized_actor is not None and self.removed_by_user_id != normalized_actor:
            self.removed_by_user_id = normalized_actor
            changed = True
        if self.removal_reason != normalized_reason:
            self.removal_reason = normalized_reason
            changed = True
        if changed:
            self.touch(updated_by_user_id=normalized_actor)
        return self

    def reactivate(
        self,
        *,
        reactivated_by_user_id: Any = None,
        starts_at: Any = None,
        expires_at: Any = None,
    ) -> "ProjectGroupMember":
        if self.is_deleted:
            raise ValueError("deleted membership must be restored before reactivation")
        normalized_actor = _normalize_external_user_id(
            reactivated_by_user_id,
            field_name="reactivated_by_user_id",
            required=False,
        )
        normalized_start = _normalize_datetime(starts_at, field_name="starts_at")
        normalized_expiry = _normalize_datetime(expires_at, field_name="expires_at")
        errors: dict[str, str] = {}
        _validate_effective_window(
            starts_at=normalized_start,
            expires_at=normalized_expiry,
            errors=errors,
        )
        if errors:
            raise ValueError(f"invalid membership window: {errors}")

        changed = self.status != MEMBERSHIP_STATUS_ACTIVE
        self.status = MEMBERSHIP_STATUS_ACTIVE
        for field_name, new_value in (
            ("starts_at", normalized_start),
            ("expires_at", normalized_expiry),
            ("removed_at", None),
            ("removed_by_user_id", None),
            ("removal_reason", None),
        ):
            if getattr(self, field_name) != new_value:
                setattr(self, field_name, new_value)
                changed = True
        if changed:
            self.touch(updated_by_user_id=normalized_actor)
        self.validate_or_raise()
        return self

    def apply_patch_payload(
        self,
        payload: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectGroupMember":
        self.ensure_not_deleted()
        body = _payload_mapping(payload)
        actor = _normalize_external_user_id(
            updated_by_user_id,
            field_name="updated_by_user_id",
            required=False,
        )
        changed = False

        next_start = self.starts_at
        next_expiry = self.expires_at
        if "startsAt" in body or "starts_at" in body:
            next_start = _normalize_datetime(
                _payload_value(body, "startsAt", "starts_at"),
                field_name="starts_at",
            )
        if "expiresAt" in body or "expires_at" in body:
            next_expiry = _normalize_datetime(
                _payload_value(body, "expiresAt", "expires_at"),
                field_name="expires_at",
            )
        errors: dict[str, str] = {}
        _validate_effective_window(
            starts_at=next_start,
            expires_at=next_expiry,
            errors=errors,
        )
        if errors:
            raise ValueError(f"invalid membership window: {errors}")
        if self.starts_at != next_start:
            self.starts_at = next_start
            changed = True
        if self.expires_at != next_expiry:
            self.expires_at = next_expiry
            changed = True

        if "status" in body:
            normalized_status = _normalize_status(
                body["status"],
                field_name="status",
                allowed_key="membership",
                default=MEMBERSHIP_STATUS_ACTIVE,
            )
            if normalized_status == MEMBERSHIP_STATUS_REMOVED:
                before = (self.status, self.removed_at, self.removed_by_user_id, self.removal_reason)
                self.status = normalized_status
                self.removed_at = self.removed_at or _utc_now()
                removed_actor = _payload_value(
                    body,
                    "removedByUserId",
                    "removed_by_user_id",
                    default=actor,
                )
                normalized_removed_actor = _normalize_external_user_id(
                    removed_actor,
                    field_name="removed_by_user_id",
                    required=False,
                )
                if normalized_removed_actor is not None:
                    self.removed_by_user_id = normalized_removed_actor
                if "removalReason" in body or "removal_reason" in body:
                    self.removal_reason = _normalize_optional_text(
                        _payload_value(body, "removalReason", "removal_reason"),
                        field_name="removal_reason",
                        max_length=MAX_REASON_LENGTH,
                    )
                changed = before != (
                    self.status,
                    self.removed_at,
                    self.removed_by_user_id,
                    self.removal_reason,
                ) or changed
            elif normalized_status == MEMBERSHIP_STATUS_DELETED:
                if self.status != normalized_status:
                    self.status = normalized_status
                    changed = True
                if self.deleted_at is None:
                    self.deleted_at = _utc_now()
                    changed = True
            else:
                if self.status != normalized_status:
                    self.status = normalized_status
                    changed = True
                if self.deleted_at is not None:
                    self.deleted_at = None
                    changed = True
                if normalized_status in {MEMBERSHIP_STATUS_ACTIVE, MEMBERSHIP_STATUS_INACTIVE}:
                    for field_name in ("removed_at", "removed_by_user_id", "removal_reason"):
                        if getattr(self, field_name) is not None:
                            setattr(self, field_name, None)
                            changed = True

        if any(key in body for key in ("metadata", "metadataJson", "metadata_json")):
            normalized = _normalize_json_object(
                _payload_value(body, "metadata", "metadataJson", "metadata_json"),
                field_name="metadata",
            )
            if self.metadata_json != normalized:
                self.metadata_json = normalized
                changed = True

        if changed:
            self.touch(updated_by_user_id=actor)
        self.validate_or_raise()
        return self

    def get_validation_errors(self) -> dict[str, str]:
        errors = self._base_validation_errors()

        for field_name, value in (
            ("project_db_id", self.project_db_id),
            ("group_db_id", self.group_db_id),
        ):
            try:
                _normalize_positive_int(value, field_name=field_name)
            except ValueError as exc:
                errors[field_name] = str(exc)
        for field_name, value, prefix in (
            ("membership_id", self.membership_id, self.ID_PREFIX),
            ("group_id", self.group_id, ProjectGroup.ID_PREFIX),
        ):
            try:
                _normalize_public_id(value, field_name=field_name, prefix=prefix)
            except ValueError as exc:
                errors[field_name] = str(exc)
        try:
            _normalize_external_user_id(self.user_id, field_name="user_id", required=True)
        except ValueError as exc:
            errors["user_id"] = str(exc)
        for field_name in (
            "added_by_user_id",
            "removed_by_user_id",
            "created_by_user_id",
            "updated_by_user_id",
        ):
            try:
                _normalize_external_user_id(
                    getattr(self, field_name),
                    field_name=field_name,
                    required=False,
                )
            except ValueError as exc:
                errors[field_name] = str(exc)
        try:
            _normalize_optional_text(
                self.removal_reason,
                field_name="removal_reason",
                max_length=MAX_REASON_LENGTH,
            )
        except ValueError as exc:
            errors["removal_reason"] = str(exc)
        try:
            _as_utc_datetime(self.removed_at, field_name="removed_at")
        except ValueError as exc:
            errors["removed_at"] = str(exc)
        try:
            normalized_status = _normalize_status(
                self.status,
                field_name="status",
                allowed_key="membership",
                default=MEMBERSHIP_STATUS_ACTIVE,
            )
        except ValueError as exc:
            errors["status"] = str(exc)
            normalized_status = None

        _validate_effective_window(
            starts_at=self.starts_at,
            expires_at=self.expires_at,
            errors=errors,
        )
        if normalized_status == MEMBERSHIP_STATUS_REMOVED and self.removed_at is None:
            errors["removed_at"] = "is required when status is removed"
        if normalized_status in {MEMBERSHIP_STATUS_ACTIVE, MEMBERSHIP_STATUS_INACTIVE}:
            if any(
                value is not None
                for value in (self.removed_at, self.removed_by_user_id, self.removal_reason)
            ):
                errors["removal_state"] = "must be empty for active/inactive membership"
        if normalized_status == MEMBERSHIP_STATUS_DELETED and self.deleted_at is None:
            errors["deleted_at"] = "is required when status is deleted"
        if normalized_status not in {None, MEMBERSHIP_STATUS_DELETED} and self.deleted_at is not None:
            errors["deleted_at"] = "must be empty unless status is deleted"
        return errors

    def validate_or_raise(self) -> "ProjectGroupMember":
        errors = self.get_validation_errors()
        if errors:
            raise ValueError(f"invalid ProjectGroupMember: {errors}")
        return self

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        result = self._base_dict(
            include_internal=include_internal,
            include_metadata=include_metadata,
        )
        result.update(
            {
                "membershipId": self.membership_id,
                "groupId": self.group_id,
                "userId": self.user_id,
                "status": self.status,
                "effective": self.is_effective(),
                "addedByUserId": self.added_by_user_id,
                "removedByUserId": self.removed_by_user_id,
                "startsAt": _safe_isoformat(self.starts_at),
                "expiresAt": _safe_isoformat(self.expires_at),
                "removedAt": _safe_isoformat(self.removed_at),
                "removalReason": self.removal_reason,
            }
        )
        if include_internal:
            result["projectDbId"] = self.project_db_id
            result["groupDbId"] = self.group_db_id
        return result

    def __repr__(self) -> str:
        return (
            f"<ProjectGroupMember membership_id={self.membership_id!r} "
            f"project_db_id={self.project_db_id!r} "
            f"group_id={self.group_id!r} user_id={self.user_id!r} "
            f"status={self.status!r}>"
        )


# ---------------------------------------------------------------------------
# ProjectRoleAssignment
# ---------------------------------------------------------------------------


class ProjectRoleAssignment(ProjectAccessRecord):
    """Rollenvergabe an eine externe User-ID oder eine Projektgruppe."""

    __tablename__ = "project_role_assignments"
    __allow_unmapped__ = True

    ID_PREFIX: ClassVar[str] = "chk_ra_"

    assignment_id = db.Column(
        db.String(MAX_PUBLIC_ID_LENGTH),
        nullable=False,
        unique=True,
    )
    project_db_id = db.Column(
        _bigint_column_type(),
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_db_id = db.Column(
        _bigint_column_type(),
        db.ForeignKey("project_roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Denormalisierte öffentliche Rollen-ID für stabile API-/Auditantworten.
    role_id = db.Column(
        db.String(MAX_PUBLIC_ID_LENGTH),
        nullable=False,
    )

    subject_type = db.Column(
        db.String(16),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH),
        nullable=True,
        index=True,
    )
    group_db_id = db.Column(
        _bigint_column_type(),
        db.ForeignKey("project_groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    group_id = db.Column(
        db.String(MAX_PUBLIC_ID_LENGTH),
        nullable=True,
        index=True,
    )
    subject_key = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH + 16),
        nullable=False,
    )

    permission_overrides_json = db.Column(
        _json_column_type(),
        nullable=False,
        default=lambda: {"version": 1, "allow": [], "deny": []},
    )

    status = db.Column(
        db.String(32),
        nullable=False,
        default=ASSIGNMENT_STATUS_ACTIVE,
        server_default=ASSIGNMENT_STATUS_ACTIVE,
        index=True,
    )

    assigned_by_user_id = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH),
        nullable=True,
        index=True,
    )
    revoked_by_user_id = db.Column(
        db.String(MAX_EXTERNAL_USER_ID_LENGTH),
        nullable=True,
        index=True,
    )
    starts_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    revoked_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )
    revocation_reason = db.Column(
        db.String(MAX_REASON_LENGTH),
        nullable=True,
    )

    role = db.relationship(
        "ProjectRole",
        back_populates="assignments",
        lazy="raise",
        foreign_keys=[role_db_id],
    )
    group = db.relationship(
        "ProjectGroup",
        back_populates="role_assignments",
        lazy="raise",
        foreign_keys=[group_db_id],
    )

    __table_args__ = (
        UniqueConstraint(
            "project_db_id",
            "role_db_id",
            "subject_key",
            name="uq_prj_role_assign_project_role_subject",
        ),
        CheckConstraint("project_db_id > 0", name="ck_prj_role_assign_project_positive"),
        CheckConstraint("role_db_id > 0", name="ck_prj_role_assign_role_positive"),
        CheckConstraint("assignment_id <> ''", name="ck_prj_role_assign_id_not_empty"),
        CheckConstraint("role_id <> ''", name="ck_prj_role_assign_role_id_not_empty"),
        CheckConstraint("subject_key <> ''", name="ck_prj_role_assign_subject_key_not_empty"),
        CheckConstraint(
            "schema_version >= 1",
            name="ck_prj_role_assign_schema_version",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_prj_role_assign_revision",
        ),
        CheckConstraint(
            "subject_type IN ('user', 'group')",
            name="ck_prj_role_assign_subject_type",
        ),
        CheckConstraint(
            "("
            "subject_type = 'user' "
            "AND user_id IS NOT NULL "
            "AND group_db_id IS NULL "
            "AND group_id IS NULL"
            ") OR ("
            "subject_type = 'group' "
            "AND user_id IS NULL "
            "AND group_db_id IS NOT NULL "
            "AND group_id IS NOT NULL"
            ")",
            name="ck_prj_role_assign_subject_fields",
        ),
        CheckConstraint(
            "("
            "subject_type = 'user' AND subject_key LIKE 'user:%'"
            ") OR ("
            "subject_type = 'group' AND subject_key LIKE 'group:%'"
            ")",
            name="ck_prj_role_assign_subject_key",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'revoked', 'deleted')",
            name="ck_prj_role_assign_status",
        ),
        Index(
            "ix_prj_role_assign_project_subject",
            "project_db_id",
            "subject_type",
            "subject_key",
        ),
        Index(
            "ix_prj_role_assign_role_status",
            "role_db_id",
            "status",
        ),
        Index(
            "ix_prj_role_assign_project_deleted",
            "project_db_id",
            "deleted_at",
        ),
    )

    @classmethod
    def create(
        cls,
        *,
        project_db_id: Any,
        role_db_id: Any,
        role_id: Any,
        subject_type: Any,
        user_id: Any = None,
        group_db_id: Any = None,
        group_id: Any = None,
        assignment_id: Any = None,
        permission_overrides: Any = None,
        status: Any = ASSIGNMENT_STATUS_ACTIVE,
        assigned_by_user_id: Any = None,
        starts_at: Any = None,
        expires_at: Any = None,
        revoked_by_user_id: Any = None,
        revoked_at: Any = None,
        revocation_reason: Any = None,
        metadata: Any = None,
    ) -> "ProjectRoleAssignment":
        normalized_subject_type = _normalize_subject_type(subject_type)
        normalized_status = _normalize_status(
            status,
            field_name="status",
            allowed_key="assignment",
            default=ASSIGNMENT_STATUS_ACTIVE,
        )
        normalized_user_id: Optional[str] = None
        normalized_group_db_id: Optional[int] = None
        normalized_group_id: Optional[str] = None
        if normalized_subject_type == SUBJECT_TYPE_USER:
            normalized_user_id = _normalize_external_user_id(
                user_id,
                field_name="user_id",
                required=True,
            )
            if group_db_id is not None or group_id is not None:
                raise ValueError("group_db_id and group_id must be empty for user assignment")
        else:
            if user_id is not None:
                raise ValueError("user_id must be empty for group assignment")
            normalized_group_db_id = _normalize_positive_int(
                group_db_id,
                field_name="group_db_id",
            )
            normalized_group_id = _normalize_public_id(
                group_id,
                field_name="group_id",
                prefix="chk_group_",
            )

        normalized_actor = _normalize_external_user_id(
            assigned_by_user_id,
            field_name="assigned_by_user_id",
            required=False,
        )
        normalized_revoked_by = _normalize_external_user_id(
            revoked_by_user_id,
            field_name="revoked_by_user_id",
            required=False,
        )
        normalized_start = _normalize_datetime(starts_at, field_name="starts_at")
        normalized_expiry = _normalize_datetime(expires_at, field_name="expires_at")
        window_errors: dict[str, str] = {}
        _validate_effective_window(
            starts_at=normalized_start,
            expires_at=normalized_expiry,
            errors=window_errors,
        )
        if window_errors:
            raise ValueError(f"invalid assignment window: {window_errors}")
        now = _utc_now()
        normalized_revoked_at = _normalize_datetime(revoked_at, field_name="revoked_at")
        if normalized_status == ASSIGNMENT_STATUS_REVOKED and normalized_revoked_at is None:
            normalized_revoked_at = now

        instance = cls(
            project_db_id=_normalize_positive_int(project_db_id, field_name="project_db_id"),
            role_db_id=_normalize_positive_int(role_db_id, field_name="role_db_id"),
            role_id=_normalize_public_id(role_id, field_name="role_id", prefix="chk_role_"),
            assignment_id=_normalize_public_id(
                assignment_id,
                field_name="assignment_id",
                prefix=cls.ID_PREFIX,
            ),
            subject_type=normalized_subject_type,
            user_id=normalized_user_id,
            group_db_id=normalized_group_db_id,
            group_id=normalized_group_id,
            subject_key=_build_subject_key(
                subject_type=normalized_subject_type,
                user_id=normalized_user_id,
                group_id=normalized_group_id,
            ),
            permission_overrides_json=_normalize_permissions_json(
                permission_overrides,
                field_name="permission_overrides",
            ),
            status=normalized_status,
            assigned_by_user_id=normalized_actor,
            revoked_by_user_id=normalized_revoked_by,
            starts_at=normalized_start,
            expires_at=normalized_expiry,
            revoked_at=normalized_revoked_at,
            revocation_reason=_normalize_optional_text(
                revocation_reason,
                field_name="revocation_reason",
                max_length=MAX_REASON_LENGTH,
            ),
            schema_version=PROJECT_ACCESS_SCHEMA_VERSION,
            revision=1,
            created_by_user_id=normalized_actor,
            updated_by_user_id=normalized_revoked_by or normalized_actor,
            metadata_json=_normalize_json_object(metadata, field_name="metadata"),
            created_at=now,
            updated_at=now,
            deleted_at=now if normalized_status == ASSIGNMENT_STATUS_DELETED else None,
        )
        instance.validate_or_raise()
        return instance

    @classmethod
    def create_for_user(
        cls,
        *,
        project_db_id: Any,
        role_db_id: Any,
        role_id: Any,
        user_id: Any,
        **kwargs: Any,
    ) -> "ProjectRoleAssignment":
        return cls.create(
            project_db_id=project_db_id,
            role_db_id=role_db_id,
            role_id=role_id,
            subject_type=SUBJECT_TYPE_USER,
            user_id=user_id,
            **kwargs,
        )

    @classmethod
    def create_for_group(
        cls,
        *,
        project_db_id: Any,
        role_db_id: Any,
        role_id: Any,
        group_db_id: Any,
        group_id: Any,
        **kwargs: Any,
    ) -> "ProjectRoleAssignment":
        return cls.create(
            project_db_id=project_db_id,
            role_db_id=role_db_id,
            role_id=role_id,
            subject_type=SUBJECT_TYPE_GROUP,
            group_db_id=group_db_id,
            group_id=group_id,
            **kwargs,
        )

    @classmethod
    def from_create_payload(
        cls,
        *,
        project_db_id: Any,
        role_db_id: Any,
        role_id: Any,
        payload: Any,
        group_db_id: Any = None,
        assigned_by_user_id: Any = None,
    ) -> "ProjectRoleAssignment":
        body = _payload_mapping(payload)
        subject = _payload_value(body, "subject", default={})
        subject_mapping = subject if isinstance(subject, Mapping) else {}
        subject_type = _payload_value(
            body,
            "subjectType",
            "subject_type",
            default=_payload_value(subject_mapping, "type", "subjectType", "subject_type"),
        )
        user_id = _payload_value(
            body,
            "userId",
            "user_id",
            default=_payload_value(subject_mapping, "userId", "user_id", "id"),
        )
        group_id = _payload_value(
            body,
            "groupId",
            "group_id",
            default=_payload_value(subject_mapping, "groupId", "group_id", "id"),
        )
        return cls.create(
            project_db_id=project_db_id,
            role_db_id=role_db_id,
            role_id=role_id,
            assignment_id=_payload_value(body, "assignmentId", "assignment_id"),
            subject_type=subject_type,
            user_id=user_id,
            group_db_id=group_db_id,
            group_id=group_id,
            permission_overrides=_payload_value(
                body,
                "permissionOverrides",
                "permission_overrides",
                "permissionOverridesJson",
                "permission_overrides_json",
            ),
            status=_payload_value(body, "status", default=ASSIGNMENT_STATUS_ACTIVE),
            assigned_by_user_id=(
                assigned_by_user_id
                if assigned_by_user_id is not None
                else _payload_value(body, "assignedByUserId", "assigned_by_user_id")
            ),
            starts_at=_payload_value(body, "startsAt", "starts_at"),
            expires_at=_payload_value(body, "expiresAt", "expires_at"),
            revoked_by_user_id=_payload_value(
                body,
                "revokedByUserId",
                "revoked_by_user_id",
            ),
            revoked_at=_payload_value(body, "revokedAt", "revoked_at"),
            revocation_reason=_payload_value(
                body,
                "revocationReason",
                "revocation_reason",
            ),
            metadata=_payload_value(body, "metadata", "metadataJson", "metadata_json"),
        )

    @validates("subject_type")
    def _validate_subject_type_column(self, _: str, value: Any) -> str:
        return _normalize_subject_type(value)

    @validates("status")
    def _validate_status_column(self, _: str, value: Any) -> str:
        return _normalize_status(
            value,
            field_name="status",
            allowed_key="assignment",
            default=ASSIGNMENT_STATUS_ACTIVE,
        )

    def is_effective(self, *, at: Optional[datetime] = None) -> bool:
        if self.is_deleted or self.status != ASSIGNMENT_STATUS_ACTIVE:
            return False
        current = _as_utc_datetime(at or _utc_now(), field_name="at")
        starts_at = _as_utc_datetime(self.starts_at, field_name="starts_at")
        expires_at = _as_utc_datetime(self.expires_at, field_name="expires_at")
        assert current is not None
        if starts_at is not None and current < starts_at:
            return False
        if expires_at is not None and current >= expires_at:
            return False
        return True

    def set_permission_overrides(
        self,
        value: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectRoleAssignment":
        self.ensure_not_deleted()
        self.permission_overrides_json = _normalize_permissions_json(
            value,
            field_name="permission_overrides",
        )
        return self.touch(updated_by_user_id=updated_by_user_id)  # type: ignore[return-value]

    def revoke(
        self,
        *,
        revoked_by_user_id: Any = None,
        reason: Any = None,
    ) -> "ProjectRoleAssignment":
        self.ensure_not_deleted()
        normalized_actor = _normalize_external_user_id(
            revoked_by_user_id,
            field_name="revoked_by_user_id",
            required=False,
        )
        normalized_reason = _normalize_optional_text(
            reason,
            field_name="revocation_reason",
            max_length=MAX_REASON_LENGTH,
        )
        changed = self.status != ASSIGNMENT_STATUS_REVOKED
        self.status = ASSIGNMENT_STATUS_REVOKED
        if self.revoked_at is None:
            self.revoked_at = _utc_now()
            changed = True
        if normalized_actor is not None and self.revoked_by_user_id != normalized_actor:
            self.revoked_by_user_id = normalized_actor
            changed = True
        if self.revocation_reason != normalized_reason:
            self.revocation_reason = normalized_reason
            changed = True
        if changed:
            self.touch(updated_by_user_id=normalized_actor)
        return self

    def reactivate(
        self,
        *,
        reactivated_by_user_id: Any = None,
        starts_at: Any = None,
        expires_at: Any = None,
    ) -> "ProjectRoleAssignment":
        if self.is_deleted:
            raise ValueError("deleted assignment must be restored before reactivation")
        normalized_actor = _normalize_external_user_id(
            reactivated_by_user_id,
            field_name="reactivated_by_user_id",
            required=False,
        )
        normalized_start = _normalize_datetime(starts_at, field_name="starts_at")
        normalized_expiry = _normalize_datetime(expires_at, field_name="expires_at")
        errors: dict[str, str] = {}
        _validate_effective_window(
            starts_at=normalized_start,
            expires_at=normalized_expiry,
            errors=errors,
        )
        if errors:
            raise ValueError(f"invalid assignment window: {errors}")

        changed = self.status != ASSIGNMENT_STATUS_ACTIVE
        self.status = ASSIGNMENT_STATUS_ACTIVE
        for field_name, new_value in (
            ("starts_at", normalized_start),
            ("expires_at", normalized_expiry),
            ("revoked_at", None),
            ("revoked_by_user_id", None),
            ("revocation_reason", None),
        ):
            if getattr(self, field_name) != new_value:
                setattr(self, field_name, new_value)
                changed = True
        if changed:
            self.touch(updated_by_user_id=normalized_actor)
        self.validate_or_raise()
        return self

    def apply_patch_payload(
        self,
        payload: Any,
        *,
        updated_by_user_id: Any = None,
    ) -> "ProjectRoleAssignment":
        self.ensure_not_deleted()
        body = _payload_mapping(payload)
        actor = _normalize_external_user_id(
            updated_by_user_id,
            field_name="updated_by_user_id",
            required=False,
        )
        changed = False

        if any(
            key in body
            for key in (
                "permissionOverrides",
                "permission_overrides",
                "permissionOverridesJson",
                "permission_overrides_json",
            )
        ):
            normalized = _normalize_permissions_json(
                _payload_value(
                    body,
                    "permissionOverrides",
                    "permission_overrides",
                    "permissionOverridesJson",
                    "permission_overrides_json",
                ),
                field_name="permission_overrides",
            )
            if self.permission_overrides_json != normalized:
                self.permission_overrides_json = normalized
                changed = True

        next_start = self.starts_at
        next_expiry = self.expires_at
        if "startsAt" in body or "starts_at" in body:
            next_start = _normalize_datetime(
                _payload_value(body, "startsAt", "starts_at"),
                field_name="starts_at",
            )
        if "expiresAt" in body or "expires_at" in body:
            next_expiry = _normalize_datetime(
                _payload_value(body, "expiresAt", "expires_at"),
                field_name="expires_at",
            )
        errors: dict[str, str] = {}
        _validate_effective_window(
            starts_at=next_start,
            expires_at=next_expiry,
            errors=errors,
        )
        if errors:
            raise ValueError(f"invalid assignment window: {errors}")
        if self.starts_at != next_start:
            self.starts_at = next_start
            changed = True
        if self.expires_at != next_expiry:
            self.expires_at = next_expiry
            changed = True

        if "status" in body:
            normalized_status = _normalize_status(
                body["status"],
                field_name="status",
                allowed_key="assignment",
                default=ASSIGNMENT_STATUS_ACTIVE,
            )
            if normalized_status == ASSIGNMENT_STATUS_REVOKED:
                before = (self.status, self.revoked_at, self.revoked_by_user_id, self.revocation_reason)
                self.status = normalized_status
                self.revoked_at = self.revoked_at or _utc_now()
                revoked_actor = _payload_value(
                    body,
                    "revokedByUserId",
                    "revoked_by_user_id",
                    default=actor,
                )
                normalized_revoked_actor = _normalize_external_user_id(
                    revoked_actor,
                    field_name="revoked_by_user_id",
                    required=False,
                )
                if normalized_revoked_actor is not None:
                    self.revoked_by_user_id = normalized_revoked_actor
                if "revocationReason" in body or "revocation_reason" in body:
                    self.revocation_reason = _normalize_optional_text(
                        _payload_value(body, "revocationReason", "revocation_reason"),
                        field_name="revocation_reason",
                        max_length=MAX_REASON_LENGTH,
                    )
                changed = before != (
                    self.status,
                    self.revoked_at,
                    self.revoked_by_user_id,
                    self.revocation_reason,
                ) or changed
            elif normalized_status == ASSIGNMENT_STATUS_DELETED:
                if self.status != normalized_status:
                    self.status = normalized_status
                    changed = True
                if self.deleted_at is None:
                    self.deleted_at = _utc_now()
                    changed = True
            else:
                if self.status != normalized_status:
                    self.status = normalized_status
                    changed = True
                if self.deleted_at is not None:
                    self.deleted_at = None
                    changed = True
                if normalized_status in {ASSIGNMENT_STATUS_ACTIVE, ASSIGNMENT_STATUS_INACTIVE}:
                    for field_name in ("revoked_at", "revoked_by_user_id", "revocation_reason"):
                        if getattr(self, field_name) is not None:
                            setattr(self, field_name, None)
                            changed = True

        if any(key in body for key in ("metadata", "metadataJson", "metadata_json")):
            normalized = _normalize_json_object(
                _payload_value(body, "metadata", "metadataJson", "metadata_json"),
                field_name="metadata",
            )
            if self.metadata_json != normalized:
                self.metadata_json = normalized
                changed = True

        if changed:
            self.touch(updated_by_user_id=actor)
        self.validate_or_raise()
        return self

    def get_validation_errors(self) -> dict[str, str]:
        errors = self._base_validation_errors()

        for field_name, value in (
            ("project_db_id", self.project_db_id),
            ("role_db_id", self.role_db_id),
        ):
            try:
                _normalize_positive_int(value, field_name=field_name)
            except ValueError as exc:
                errors[field_name] = str(exc)
        for field_name, value, prefix in (
            ("assignment_id", self.assignment_id, self.ID_PREFIX),
            ("role_id", self.role_id, ProjectRole.ID_PREFIX),
        ):
            try:
                _normalize_public_id(value, field_name=field_name, prefix=prefix)
            except ValueError as exc:
                errors[field_name] = str(exc)
        try:
            _normalize_required_text(
                self.subject_key,
                field_name="subject_key",
                max_length=MAX_EXTERNAL_USER_ID_LENGTH + 16,
            )
        except ValueError as exc:
            errors["subject_key"] = str(exc)

        try:
            normalized_subject_type = _normalize_subject_type(self.subject_type)
        except ValueError as exc:
            errors["subject_type"] = str(exc)
            normalized_subject_type = None

        if normalized_subject_type == SUBJECT_TYPE_USER:
            try:
                _normalize_external_user_id(self.user_id, field_name="user_id", required=True)
            except ValueError as exc:
                errors["user_id"] = str(exc)
            if self.group_db_id is not None:
                errors["group_db_id"] = "must be empty for user assignment"
            if self.group_id is not None:
                errors["group_id"] = "must be empty for user assignment"
        elif normalized_subject_type == SUBJECT_TYPE_GROUP:
            if self.user_id is not None:
                errors["user_id"] = "must be empty for group assignment"
            try:
                _normalize_positive_int(self.group_db_id, field_name="group_db_id")
            except ValueError as exc:
                errors["group_db_id"] = str(exc)
            try:
                _normalize_public_id(
                    self.group_id,
                    field_name="group_id",
                    prefix=ProjectGroup.ID_PREFIX,
                )
            except ValueError as exc:
                errors["group_id"] = str(exc)

        if normalized_subject_type is not None:
            try:
                expected_subject_key = _build_subject_key(
                    subject_type=normalized_subject_type,
                    user_id=self.user_id,
                    group_id=self.group_id,
                )
                if self.subject_key != expected_subject_key:
                    errors["subject_key"] = f"must equal canonical key {expected_subject_key!r}"
            except ValueError as exc:
                errors["subject_key"] = str(exc)
        try:
            _normalize_permissions_json(
                self.permission_overrides_json,
                field_name="permission_overrides_json",
            )
        except ValueError as exc:
            errors["permission_overrides_json"] = str(exc)
        for field_name in (
            "assigned_by_user_id",
            "revoked_by_user_id",
            "created_by_user_id",
            "updated_by_user_id",
        ):
            try:
                _normalize_external_user_id(
                    getattr(self, field_name),
                    field_name=field_name,
                    required=False,
                )
            except ValueError as exc:
                errors[field_name] = str(exc)
        try:
            _normalize_optional_text(
                self.revocation_reason,
                field_name="revocation_reason",
                max_length=MAX_REASON_LENGTH,
            )
        except ValueError as exc:
            errors["revocation_reason"] = str(exc)
        try:
            _as_utc_datetime(self.revoked_at, field_name="revoked_at")
        except ValueError as exc:
            errors["revoked_at"] = str(exc)
        try:
            normalized_status = _normalize_status(
                self.status,
                field_name="status",
                allowed_key="assignment",
                default=ASSIGNMENT_STATUS_ACTIVE,
            )
        except ValueError as exc:
            errors["status"] = str(exc)
            normalized_status = None

        _validate_effective_window(
            starts_at=self.starts_at,
            expires_at=self.expires_at,
            errors=errors,
        )
        if normalized_status == ASSIGNMENT_STATUS_REVOKED and self.revoked_at is None:
            errors["revoked_at"] = "is required when status is revoked"
        if normalized_status in {ASSIGNMENT_STATUS_ACTIVE, ASSIGNMENT_STATUS_INACTIVE}:
            if any(
                value is not None
                for value in (self.revoked_at, self.revoked_by_user_id, self.revocation_reason)
            ):
                errors["revocation_state"] = "must be empty for active/inactive assignment"
        if normalized_status == ASSIGNMENT_STATUS_DELETED and self.deleted_at is None:
            errors["deleted_at"] = "is required when status is deleted"
        if normalized_status not in {None, ASSIGNMENT_STATUS_DELETED} and self.deleted_at is not None:
            errors["deleted_at"] = "must be empty unless status is deleted"
        return errors

    def validate_or_raise(self) -> "ProjectRoleAssignment":
        errors = self.get_validation_errors()
        if errors:
            raise ValueError(f"invalid ProjectRoleAssignment: {errors}")
        return self

    def to_dict(
        self,
        *,
        include_internal: bool = False,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        subject_id = self.user_id if self.subject_type == SUBJECT_TYPE_USER else self.group_id
        result = self._base_dict(
            include_internal=include_internal,
            include_metadata=include_metadata,
        )
        result.update(
            {
                "assignmentId": self.assignment_id,
                "roleId": self.role_id,
                "subjectType": self.subject_type,
                "subjectId": subject_id,
                "userId": self.user_id,
                "groupId": self.group_id,
                "subjectKey": self.subject_key,
                "permissionOverrides": _make_json_safe(
                    self.permission_overrides_json or {}
                ),
                "status": self.status,
                "effective": self.is_effective(),
                "assignedByUserId": self.assigned_by_user_id,
                "revokedByUserId": self.revoked_by_user_id,
                "startsAt": _safe_isoformat(self.starts_at),
                "expiresAt": _safe_isoformat(self.expires_at),
                "revokedAt": _safe_isoformat(self.revoked_at),
                "revocationReason": self.revocation_reason,
            }
        )
        if include_internal:
            result["projectDbId"] = self.project_db_id
            result["roleDbId"] = self.role_db_id
            result["groupDbId"] = self.group_db_id
        return result

    def __repr__(self) -> str:
        return (
            f"<ProjectRoleAssignment assignment_id={self.assignment_id!r} "
            f"project_db_id={self.project_db_id!r} role_id={self.role_id!r} "
            f"subject_key={self.subject_key!r} status={self.status!r}>"
        )


# ---------------------------------------------------------------------------
# Diagnose- und Registrierungsvertrag für models/__init__.py
# ---------------------------------------------------------------------------


PROJECT_ACCESS_MODEL_CLASSES: Final[tuple[type[db.Model], ...]] = (
    ProjectRole,
    ProjectGroup,
    ProjectGroupMember,
    ProjectRoleAssignment,
)

PROJECT_ACCESS_MODEL_CLASS_NAMES: Final[tuple[str, ...]] = tuple(
    model.__name__ for model in PROJECT_ACCESS_MODEL_CLASSES
)

PROJECT_ACCESS_TABLE_NAMES: Final[tuple[str, ...]] = tuple(
    model.__tablename__ for model in PROJECT_ACCESS_MODEL_CLASSES
)

PROJECT_ACCESS_EXPECTED_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "ProjectRole": (
        "id",
        "role_id",
        "project_db_id",
        "role_key",
        "name",
        "description",
        "permissions_json",
        "is_system",
        "status",
        "schema_version",
        "revision",
        "metadata_json",
        "created_by_user_id",
        "updated_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "ProjectGroup": (
        "id",
        "group_id",
        "project_db_id",
        "group_key",
        "name",
        "description",
        "is_system",
        "status",
        "schema_version",
        "revision",
        "metadata_json",
        "created_by_user_id",
        "updated_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "ProjectGroupMember": (
        "id",
        "membership_id",
        "project_db_id",
        "group_db_id",
        "group_id",
        "user_id",
        "status",
        "added_by_user_id",
        "removed_by_user_id",
        "starts_at",
        "expires_at",
        "removed_at",
        "removal_reason",
        "schema_version",
        "revision",
        "metadata_json",
        "created_by_user_id",
        "updated_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
    "ProjectRoleAssignment": (
        "id",
        "assignment_id",
        "project_db_id",
        "role_db_id",
        "role_id",
        "subject_type",
        "user_id",
        "group_db_id",
        "group_id",
        "subject_key",
        "permission_overrides_json",
        "status",
        "assigned_by_user_id",
        "revoked_by_user_id",
        "starts_at",
        "expires_at",
        "revoked_at",
        "revocation_reason",
        "schema_version",
        "revision",
        "metadata_json",
        "created_by_user_id",
        "updated_by_user_id",
        "created_at",
        "updated_at",
        "deleted_at",
    ),
}


def get_project_access_model_contract() -> dict[str, Any]:
    """Liefert eine JSON-sichere, DB-freie Modelvertragsübersicht."""

    model_entries: list[dict[str, Any]] = []
    for model in PROJECT_ACCESS_MODEL_CLASSES:
        try:
            table = model.__table__
            columns = tuple(column.name for column in table.columns)
        except Exception:
            columns = PROJECT_ACCESS_EXPECTED_COLUMNS.get(model.__name__, ())
        model_entries.append(
            {
                "className": model.__name__,
                "tableName": model.__tablename__,
                "expectedColumns": list(
                    PROJECT_ACCESS_EXPECTED_COLUMNS.get(model.__name__, ())
                ),
                "declaredColumns": list(columns),
            }
        )

    return {
        "schemaVersion": PROJECT_ACCESS_SCHEMA_VERSION,
        "models": model_entries,
        "modelClassNames": list(PROJECT_ACCESS_MODEL_CLASS_NAMES),
        "tableNames": list(PROJECT_ACCESS_TABLE_NAMES),
        "defaultRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
        "knownPermissionKeys": list(KNOWN_PERMISSION_KEYS),
        "assignmentSubjectTypes": sorted(ASSIGNMENT_SUBJECT_TYPES),
        "authzEnforced": False,
        "externalUserForeignKeys": False,
        "normalizationCaches": {
            "key": _normalize_key_cached.cache_info()._asdict(),
            "permission": _normalize_permission_cached.cache_info()._asdict(),
            "status": _normalize_status_cached.cache_info()._asdict(),
            "defaultRoles": _normalized_default_project_role_definitions.cache_info()._asdict(),
        },
    }


__all__ = [
    "PROJECT_ACCESS_SCHEMA_VERSION",
    "ROLE_STATUS_ACTIVE",
    "ROLE_STATUS_INACTIVE",
    "ROLE_STATUS_ARCHIVED",
    "ROLE_STATUS_DELETED",
    "ROLE_STATUSES",
    "GROUP_STATUS_ACTIVE",
    "GROUP_STATUS_INACTIVE",
    "GROUP_STATUS_ARCHIVED",
    "GROUP_STATUS_DELETED",
    "GROUP_STATUSES",
    "MEMBERSHIP_STATUS_ACTIVE",
    "MEMBERSHIP_STATUS_INACTIVE",
    "MEMBERSHIP_STATUS_REMOVED",
    "MEMBERSHIP_STATUS_DELETED",
    "MEMBERSHIP_STATUSES",
    "ASSIGNMENT_STATUS_ACTIVE",
    "ASSIGNMENT_STATUS_INACTIVE",
    "ASSIGNMENT_STATUS_REVOKED",
    "ASSIGNMENT_STATUS_DELETED",
    "ASSIGNMENT_STATUSES",
    "SUBJECT_TYPE_USER",
    "SUBJECT_TYPE_GROUP",
    "ASSIGNMENT_SUBJECT_TYPES",
    "DEFAULT_ROLE_OWNER",
    "DEFAULT_ROLE_ADMIN",
    "DEFAULT_ROLE_EDITOR",
    "DEFAULT_ROLE_VIEWER",
    "DEFAULT_PROJECT_ROLE_KEYS",
    "KNOWN_PERMISSION_KEYS",
    "ProjectAccessRecord",
    "ProjectRole",
    "ProjectGroup",
    "ProjectGroupMember",
    "ProjectRoleAssignment",
    "PROJECT_ACCESS_MODEL_CLASSES",
    "PROJECT_ACCESS_MODEL_CLASS_NAMES",
    "PROJECT_ACCESS_TABLE_NAMES",
    "PROJECT_ACCESS_EXPECTED_COLUMNS",
    "get_default_project_role_definitions",
    "normalize_project_permissions",
    "get_project_access_model_contract",
    "clear_project_access_normalization_caches",
]
