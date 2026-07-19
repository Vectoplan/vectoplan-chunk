# services/vectoplan-chunk/src/bootstrap/runtime_checks.py
"""
Read-only runtime checks for the `vectoplan-chunk` service.

This module contains cheap startup/runtime checks only.

Responsibilities:
- verify important directories and files,
- verify registered Flask routes,
- collect compact app metadata,
- verify the complete model and project-access import contracts,
- optionally perform a cheap DB connectivity check,
- inspect required tables and critical columns without changing schema,
- inspect the configured default project's owner/default-role readiness with
  strictly bounded read-only queries,
- return serializable check results for startup state and status routes.

Important boundaries:
- no db.create_all(),
- no schema migration or ALTER TABLE,
- no default seeding or invariant repair,
- no inserts, updates, deletes, flushes or commits,
- no chunk generation,
- no snapshot loading,
- no command execution,
- no ORM relationship traversal,
- no deep serialization of Project/Universe/World/Chunk/Event/Object graphs,
- no caching of ORM rows, query results or database state.

Design rule:

    Runtime checks must be cheap, bounded and read-only.

The checks in this module are safe to run from Gunicorn workers during normal
runtime startup, because they do not mutate database state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import importlib
import re
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from flask import Flask

try:
    from sqlalchemy import text
except Exception:  # pragma: no cover - fallback for partial environments
    text = None  # type: ignore[assignment]

try:
    from extensions import db, get_database_status
except Exception:  # pragma: no cover - fallback for isolated import tests
    db = None  # type: ignore[assignment]
    get_database_status = None  # type: ignore[assignment]

try:
    from .settings import (
        BootstrapSettings,
        build_bootstrap_settings,
        get_bool_setting,
        get_str_setting,
        should_check_database_on_startup,
        should_require_database_on_startup,
    )
except Exception:  # pragma: no cover - fallback for direct script-style imports
    BootstrapSettings = Any  # type: ignore[misc, assignment]

    def build_bootstrap_settings(app: Any = None) -> Any:  # type: ignore[override]
        return None

    def get_bool_setting(
        app: Any,
        key: str,
        default: bool = False,
        aliases: Sequence[str] | None = None,
        prefer_env: bool = True,
    ) -> bool:
        value = None
        try:
            value = getattr(app, "config", {}).get(key, None)
        except Exception:
            value = None
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def get_str_setting(
        app: Any,
        key: str,
        default: str = "",
        aliases: Sequence[str] | None = None,
        prefer_env: bool = True,
    ) -> str:
        try:
            value = getattr(app, "config", {}).get(key, default)
        except Exception:
            value = default
        try:
            return str(value).strip() or default
        except Exception:
            return default

    def should_check_database_on_startup(app: Any = None) -> bool:
        return True

    def should_require_database_on_startup(app: Any = None) -> bool:
        return True


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

RUNTIME_CHECKS_RESULT_VERSION: Final[str] = "runtime-checks-result.v3"

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_DISPLAY_NAME: Final[str] = "VECTOPLAN Chunk Service"
DEFAULT_PROJECT_ID: Final[str] = "dev-project"
DEFAULT_PROJECT_OWNER_AUTH_USER_ID: Final[str] = "auth_dev_owner"
# Compatibility export. The value is now a canonical cross-service auth identity.
DEFAULT_PROJECT_OWNER_USER_ID: Final[str] = DEFAULT_PROJECT_OWNER_AUTH_USER_ID
DEFAULT_CANONICAL_USER_ID_FIELD: Final[str] = "auth_user_id"
DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE: Final[str] = "vectoplan-app"
DEFAULT_PROJECT_ACCESS_PROJECTION_VERSION: Final[str] = "project-access-projection.v1"
DEFAULT_PROJECT_ROLE_KEYS: Final[tuple[str, ...]] = (
    "owner",
    "admin",
    "editor",
    "viewer",
)
PROJECT_ACCESS_QUERY_ROW_LIMIT: Final[int] = 64

CHECK_STATUS_OK: Final[str] = "ok"
CHECK_STATUS_MISSING: Final[str] = "missing"
CHECK_STATUS_INVALID_TYPE: Final[str] = "invalid-type"
CHECK_STATUS_SKIPPED: Final[str] = "skipped"
CHECK_STATUS_FAILED: Final[str] = "failed"
CHECK_STATUS_WARNING: Final[str] = "warning"

MODEL_CHECK_IMPORT_ERROR_CODE: Final[str] = "model_import_failed"
MODEL_CHECK_REGISTRY_ERROR_CODE: Final[str] = "model_registry_not_ready"
DATABASE_CHECK_UNAVAILABLE_CODE: Final[str] = "database_unavailable"
DATABASE_CHECK_FAILED_CODE: Final[str] = "database_check_failed"
SCHEMA_CHECK_UNAVAILABLE_CODE: Final[str] = "schema_check_unavailable"
SCHEMA_CHECK_FAILED_CODE: Final[str] = "schema_not_ready"
PROJECT_ACCESS_CHECK_UNAVAILABLE_CODE: Final[str] = (
    "project_access_check_unavailable"
)
PROJECT_ACCESS_CHECK_FAILED_CODE: Final[str] = "project_access_not_ready"

# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PathCheckSpec:
    """Specification for one startup path check."""

    name: str
    config_key: str
    fallback_relative_path: str
    required: bool
    description: str


@dataclass(frozen=True, slots=True)
class FileCheckSpec:
    """Specification for one startup file check."""

    name: str
    fallback_relative_path: str
    required: bool
    description: str


@dataclass(frozen=True, slots=True)
class RouteCheckSpec:
    """Specification for one registered route check."""

    name: str
    rule: str
    required: bool
    description: str
    alternatives: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CheckMessage:
    """Small serializable warning/error message."""

    code: str
    message: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeCheckItem:
    """Generic serializable check item."""

    name: str
    status: str
    ok: bool
    required: bool
    description: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeChecksResult:
    """Aggregate result for all read-only runtime checks."""

    ok: bool
    status: str
    started_at: str
    completed_at: str
    duration_ms: int
    result_version: str

    metadata: dict[str, Any]
    paths: list[dict[str, Any]]
    files: list[dict[str, Any]]
    routes: list[dict[str, Any]]
    route_summary: dict[str, Any]
    models: dict[str, Any]
    database: dict[str, Any]
    schema: dict[str, Any]
    project_access: dict[str, Any]

    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]


# -----------------------------------------------------------------------------
# Primitive safe helpers
# -----------------------------------------------------------------------------

def _utc_now() -> datetime:
    """Return current UTC datetime robustly."""
    try:
        return datetime.now(timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _utc_now_iso() -> str:
    """Return current UTC timestamp as ISO string."""
    try:
        return _utc_now().isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def _duration_ms(started_at: datetime, completed_at: datetime) -> int:
    """Return duration in milliseconds."""
    try:
        return max(0, int((completed_at - started_at).total_seconds() * 1000))
    except Exception:
        return 0


def _safe_str(value: Any, default: str = "") -> str:
    """Convert a value to a stripped string."""
    if value is None:
        return default

    try:
        result = str(value).strip()
    except Exception:
        return default

    return result or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Convert a value to bool robustly."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text_value = _safe_str(value, "").lower()

    if text_value in {"1", "true", "t", "yes", "y", "on", "enabled"}:
        return True

    if text_value in {"0", "false", "f", "no", "n", "off", "disabled"}:
        return False

    return default


def _safe_int(value: Any, default: int = 0, minimum: int | None = None) -> int:
    """Convert a value to int robustly."""
    try:
        result = int(value)
    except Exception:
        result = default

    if minimum is not None:
        try:
            result = max(minimum, result)
        except Exception:
            result = minimum

    return result


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_dict(value: Any) -> dict[str, Any]:
    """Return value as dict if possible."""
    if isinstance(value, dict):
        return value

    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}

    return {}


def _safe_list(value: Any) -> list[Any]:
    """Return a bounded sequence as a new list."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        try:
            return sorted(value, key=lambda item: _safe_str(item, ""))
        except Exception:
            return list(value)
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return []
    try:
        return list(value)
    except Exception:
        return []


def _identity_fingerprint(value: Any) -> str | None:
    """Return a stable non-reversible identity fingerprint."""
    text_value = _safe_str(value, "")
    if not text_value:
        return None
    try:
        return hashlib.sha256(text_value.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return None


def _is_canonical_auth_user_id(value: Any) -> bool:
    """Reject local ids, email addresses and client-controlled identity forms."""
    text_value = _safe_str(value, "")
    if not text_value or len(text_value) > 255:
        return False
    lowered = text_value.lower()
    if text_value.isdecimal() or "@" in text_value or "://" in text_value:
        return False
    if any(character.isspace() or ord(character) < 32 for character in text_value):
        return False
    if lowered in {
        "0",
        "1",
        "anonymous",
        "guest",
        "bootstrap",
        "system",
        "none",
        "null",
        "undefined",
    }:
        return False
    if lowered.startswith(("local_", "appuser_", "dbuser_", "email:")):
        return False
    return True


def _normalize_owner_auth_user_id(value: Any) -> str:
    """Normalize unsafe legacy bootstrap values to the canonical dev owner."""
    candidate = _safe_str(value, "")
    if _is_canonical_auth_user_id(candidate):
        return candidate
    return DEFAULT_PROJECT_OWNER_AUTH_USER_ID


def _private_identifiers_enabled(app: Any) -> bool:
    """Return whether diagnostics may include raw auth identities."""
    try:
        return get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_RUNTIME_CHECKS_INCLUDE_PRIVATE_IDENTIFIERS",
            False,
            aliases=(
                "VECTOPLAN_CHUNK_STARTUP_INCLUDE_PRIVATE_IDENTIFIERS",
                "CHUNK_RUNTIME_CHECKS_INCLUDE_PRIVATE_IDENTIFIERS",
            ),
            prefer_env=True,
        )
    except Exception:
        return False


def _public_identity_fields(app: Any, value: Any) -> dict[str, Any]:
    """Build identity diagnostics without exposing the raw value by default."""
    normalized = _safe_str(value, "")
    expose = bool(normalized and _private_identifiers_enabled(app))
    return {
        "ownerAuthUserId": normalized if expose else None,
        "ownerUserId": normalized if expose else None,
        "ownerIdFingerprint": _identity_fingerprint(normalized),
        "ownerIdentityCanonical": (
            _is_canonical_auth_user_id(normalized) if normalized else False
        ),
        "identityExposed": expose,
        "canonicalUserIdField": DEFAULT_CANONICAL_USER_ID_FIELD,
    }


def _first_row_value(row: Any, names: Sequence[str], default: Any = None) -> Any:
    """Read the first non-empty compatible mapped value from a row."""
    if row is None:
        return default
    for name in names:
        try:
            value = getattr(row, name, None)
        except Exception:
            value = None
        if value not in (None, ""):
            return value
    return default


def _first_model_attribute(model: Any, names: Sequence[str]) -> Any | None:
    """Return the first queryable SQLAlchemy model attribute."""
    if model is None:
        return None
    for name in names:
        try:
            value = getattr(model, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _row_is_active(row: Any, now: datetime | None = None) -> bool:
    """Evaluate common active/deleted/effective-window fields read-only."""
    if row is None:
        return False
    now = now or _utc_now()
    for name in ("active", "is_active", "enabled"):
        try:
            value = getattr(row, name, None)
        except Exception:
            value = None
        if value is not None and not _safe_bool(value, False):
            return False
    try:
        if getattr(row, "deleted_at", None) is not None:
            return False
    except Exception:
        return False
    status_value = _safe_str(_first_row_value(row, ("status",), ""), "").lower()
    if status_value and status_value not in {"active", "ready", "enabled"}:
        return False
    return _effective_window_ready(row, now)


def _expected_access_source_service(app: Any) -> str:
    """Return the App-owned access-projection source service."""
    try:
        value = get_str_setting(
            app,
            "VECTOPLAN_CHUNK_PROJECT_ACCESS_SOURCE_SERVICE",
            DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE,
            aliases=("CHUNK_PROJECT_ACCESS_SOURCE_SERVICE",),
            prefer_env=True,
        )
    except Exception:
        value = DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE
    return _safe_str(value, DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE).lower()


def _project_access_authz_enforced(app: Any) -> bool:
    """Return the explicitly configured route-authorization rollout state."""
    try:
        return get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_PROJECT_ACCESS_AUTHZ_ENFORCED",
            False,
            aliases=("CHUNK_PROJECT_ACCESS_AUTHZ_ENFORCED",),
            prefer_env=True,
        )
    except Exception:
        return False


def _viewer_read_only_expected(app: Any) -> bool:
    """Return whether Viewer must remain strictly read-only."""
    try:
        return get_bool_setting(
            app,
            "VECTOPLAN_CHUNK_VIEWER_READ_ONLY",
            True,
            aliases=("CHUNK_VIEWER_READ_ONLY",),
            prefer_env=True,
        )
    except Exception:
        return True


def _safe_path_to_string(path: Path | None) -> str:
    """Convert Path to string robustly."""
    if path is None:
        return ""

    try:
        return str(path)
    except Exception:
        return ""


def _safe_path_exists(path: Path) -> bool:
    """Return whether path exists robustly."""
    try:
        return path.exists()
    except Exception:
        return False


def _safe_is_dir(path: Path) -> bool:
    """Return whether path is directory robustly."""
    try:
        return path.is_dir()
    except Exception:
        return False


def _safe_is_file(path: Path) -> bool:
    """Return whether path is file robustly."""
    try:
        return path.is_file()
    except Exception:
        return False


def _is_flask_app(app: object) -> bool:
    """Return whether object can be treated like a Flask app."""
    if isinstance(app, Flask):
        return True

    required_attrs = ("extensions", "config", "logger", "url_map")
    try:
        return all(hasattr(app, attr_name) for attr_name in required_attrs)
    except Exception:
        return False


def _safe_log_debug(app: Any, message: str, *args: Any) -> None:
    """Debug-log defensively."""
    try:
        app.logger.debug(message, *args)
    except Exception:
        pass


def _safe_log_info(app: Any, message: str, *args: Any) -> None:
    """Info-log defensively."""
    try:
        app.logger.info(message, *args)
    except Exception:
        pass


def _safe_log_warning(app: Any, message: str, *args: Any) -> None:
    """Warning-log defensively."""
    try:
        app.logger.warning(message, *args)
    except Exception:
        pass


def _make_message(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create serializable warning/error message dict."""
    item = CheckMessage(
        code=_safe_str(code, "runtime_check_message"),
        message=_safe_str(message, ""),
        timestamp=_utc_now_iso(),
        details=details or {},
    )
    return asdict(item)


def _make_check_item(
    name: str,
    status: str,
    ok: bool,
    required: bool,
    description: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a serializable check item."""
    item = RuntimeCheckItem(
        name=_safe_str(name, "unknown"),
        status=_safe_str(status, CHECK_STATUS_FAILED),
        ok=bool(ok),
        required=bool(required),
        description=_safe_str(description, ""),
        details=details or {},
    )
    return asdict(item)


# -----------------------------------------------------------------------------
# Runtime setting/default resolution
# -----------------------------------------------------------------------------


def _config_value(app: Any, key: str, default: Any = None) -> Any:
    """Read one Flask config value defensively."""
    try:
        return getattr(app, "config", {}).get(key, default)
    except Exception:
        return default


def _runtime_setting_bool(
    app: Any,
    settings: Any,
    *,
    attribute_name: str,
    config_key: str,
    default: bool,
    aliases: Sequence[str] | None = None,
) -> bool:
    """Resolve an optional runtime setting, then config/env fallback."""
    if settings is not None:
        try:
            runtime_settings = getattr(settings, "runtime", None)
            value = getattr(runtime_settings, attribute_name, None)
            if value is not None:
                return _safe_bool(value, default)
        except Exception:
            pass

    try:
        return get_bool_setting(
            app,
            config_key,
            default,
            aliases=aliases,
            prefer_env=True,
        )
    except Exception:
        return _safe_bool(_config_value(app, config_key, default), default)


def _resolve_default_project_id(app: Any, settings: Any = None) -> str:
    """Resolve the concrete default chunk-project id."""
    if settings is not None:
        try:
            value = getattr(settings.world_defaults, "project_id", None)
            normalized = _safe_str(value, "")
            if normalized:
                return normalized
        except Exception:
            pass

    try:
        return get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID",
            DEFAULT_PROJECT_ID,
            aliases=("DEFAULT_PROJECT_ID",),
            prefer_env=True,
        )
    except Exception:
        return _safe_str(
            _config_value(
                app,
                "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID",
                DEFAULT_PROJECT_ID,
            ),
            DEFAULT_PROJECT_ID,
        )


def _resolve_default_project_owner_auth_user_id(
    app: Any,
    settings: Any = None,
) -> str:
    """Resolve the canonical opaque owner auth identity used by dev bootstrap."""
    if settings is not None:
        try:
            world_defaults = getattr(settings, "world_defaults", None)
            for name in (
                "project_owner_auth_user_id",
                "default_project_owner_auth_user_id",
                "owner_auth_user_id",
                "project_owner_user_id",
                "default_project_owner_user_id",
                "owner_user_id",
            ):
                normalized = _safe_str(getattr(world_defaults, name, None), "")
                if normalized:
                    return _normalize_owner_auth_user_id(normalized)
        except Exception:
            pass

    try:
        owner_auth_user_id = get_str_setting(
            app,
            "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_AUTH_USER_ID",
            DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
            aliases=(
                "VECTOPLAN_CHUNK_PROJECT_OWNER_AUTH_USER_ID",
                "VECTOPLAN_CHUNK_DEFAULT_OWNER_AUTH_USER_ID",
                "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_USER_ID",
                "VECTOPLAN_CHUNK_DEFAULT_OWNER_USER_ID",
                "DEFAULT_PROJECT_OWNER_AUTH_USER_ID",
                "DEFAULT_PROJECT_OWNER_USER_ID",
            ),
            prefer_env=True,
        )
    except Exception:
        owner_auth_user_id = _safe_str(
            _config_value(
                app,
                "VECTOPLAN_CHUNK_DEFAULT_PROJECT_OWNER_AUTH_USER_ID",
                DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
            ),
            DEFAULT_PROJECT_OWNER_AUTH_USER_ID,
        )

    return _normalize_owner_auth_user_id(owner_auth_user_id)


def _resolve_default_project_owner_user_id(
    app: Any,
    settings: Any = None,
) -> str:
    """Compatibility alias returning the canonical auth identity."""
    return _resolve_default_project_owner_auth_user_id(app, settings)


# -----------------------------------------------------------------------------
# Service root/path resolution
# -----------------------------------------------------------------------------

def resolve_service_root_from_file() -> Path:
    """
    Resolve service root relative to this file.

    Expected:
        services/vectoplan-chunk/src/bootstrap/runtime_checks.py

    parents[0] -> bootstrap
    parents[1] -> src
    parents[2] -> vectoplan-chunk
    """
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        try:
            return Path(".").resolve()
        except Exception:
            return Path(".")


def resolve_configured_path(
    app: Any,
    config_key: str,
    fallback_relative_path: str,
) -> Path:
    """Resolve path from app config/env with service-root fallback."""
    configured_value = None

    try:
        configured_value = get_str_setting(app, config_key, "", prefer_env=True)
    except Exception:
        configured_value = ""

    if configured_value:
        try:
            configured_path = Path(configured_value)
            if configured_path.is_absolute():
                return configured_path
            return resolve_service_root(app).joinpath(configured_path)
        except Exception:
            pass

    try:
        return resolve_service_root(app).joinpath(fallback_relative_path)
    except Exception:
        return resolve_service_root_from_file().joinpath(fallback_relative_path)


def resolve_service_root(app: Any) -> Path:
    """Resolve service root robustly."""
    candidate = ""

    for key in (
        "SERVICE_ROOT",
        "VECTOPLAN_CHUNK_SERVICE_ROOT",
        "APP_HOME",
        "VECTOPLAN_CHUNK_APP_HOME",
    ):
        try:
            candidate = get_str_setting(app, key, "", prefer_env=True)
        except Exception:
            candidate = ""

        if candidate:
            break

    if candidate:
        try:
            return Path(candidate).resolve()
        except Exception:
            try:
                return Path(candidate)
            except Exception:
                pass

    return resolve_service_root_from_file()


# -----------------------------------------------------------------------------
# Default check specs
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_default_path_check_specs() -> tuple[PathCheckSpec, ...]:
    """Return default directory checks."""
    return (
        PathCheckSpec(
            name="service_root",
            config_key="SERVICE_ROOT",
            fallback_relative_path=".",
            required=True,
            description="Service root directory.",
        ),
        PathCheckSpec(
            name="routes_root",
            config_key="ROUTES_ROOT",
            fallback_relative_path="routes",
            required=True,
            description="HTTP route directory.",
        ),
        PathCheckSpec(
            name="src_root",
            config_key="SRC_ROOT",
            fallback_relative_path="src",
            required=True,
            description="Source logic directory.",
        ),
        PathCheckSpec(
            name="models_root",
            config_key="MODELS_ROOT",
            fallback_relative_path="models",
            required=True,
            description="SQLAlchemy model directory.",
        ),
        PathCheckSpec(
            name="world_src_root",
            config_key="WORLD_SRC_ROOT",
            fallback_relative_path="src/world",
            required=True,
            description="Provider/template world source directory.",
        ),
        PathCheckSpec(
            name="world_state_src_root",
            config_key="WORLD_STATE_SRC_ROOT",
            fallback_relative_path="src/world_state",
            required=True,
            description="Project/universe/world-state source directory.",
        ),
        PathCheckSpec(
            name="flat_world_root",
            config_key="FLAT_WORLD_ROOT",
            fallback_relative_path="src/world/flat",
            required=True,
            description="Flat provider world directory.",
        ),
        PathCheckSpec(
            name="bootstrap_src_root",
            config_key="BOOTSTRAP_SRC_ROOT",
            fallback_relative_path="src/bootstrap",
            required=True,
            description="Startup/bootstrap source directory.",
        ),
        PathCheckSpec(
            name="services_src_root",
            config_key="SERVICES_SRC_ROOT",
            fallback_relative_path="src/services",
            required=True,
            description="Canonical Chunk service modules.",
        ),
        PathCheckSpec(
            name="project_access_src_root",
            config_key="PROJECT_ACCESS_SRC_ROOT",
            fallback_relative_path="src/project_access",
            required=False,
            description="Optional legacy project-access facade package.",
        ),
        PathCheckSpec(
            name="migrations_root",
            config_key="MIGRATIONS_ROOT",
            fallback_relative_path="migrations",
            required=False,
            description="Optional Alembic migration directory.",
        ),
        PathCheckSpec(
            name="templates_root",
            config_key="TEMPLATES_ROOT",
            fallback_relative_path="templates",
            required=False,
            description="Optional legacy template directory.",
        ),
        PathCheckSpec(
            name="static_root",
            config_key="STATIC_ROOT",
            fallback_relative_path="static",
            required=False,
            description="Optional legacy static asset directory.",
        ),
        PathCheckSpec(
            name="tests_root",
            config_key="TESTS_ROOT",
            fallback_relative_path="tests",
            required=False,
            description="Optional test directory.",
        ),
        PathCheckSpec(
            name="scripts_root",
            config_key="SCRIPTS_ROOT",
            fallback_relative_path="scripts",
            required=False,
            description="Optional service scripts directory.",
        ),
    )


@lru_cache(maxsize=1)
def get_default_file_check_specs() -> tuple[FileCheckSpec, ...]:
    """Return default file checks."""
    return (
        FileCheckSpec(
            name="app_factory",
            fallback_relative_path="app.py",
            required=True,
            description="Flask app factory.",
        ),
        FileCheckSpec(
            name="wsgi_entrypoint",
            fallback_relative_path="wsgi.py",
            required=True,
            description="WSGI entrypoint.",
        ),
        FileCheckSpec(
            name="service_config",
            fallback_relative_path="config.py",
            required=True,
            description="Central service configuration.",
        ),
        FileCheckSpec(
            name="extensions",
            fallback_relative_path="extensions.py",
            required=True,
            description="Shared Flask extension setup.",
        ),
        FileCheckSpec(
            name="requirements",
            fallback_relative_path="requirements.txt",
            required=True,
            description="Python dependency specification.",
        ),
        FileCheckSpec(
            name="models_init",
            fallback_relative_path="models/__init__.py",
            required=True,
            description="Model registration package.",
        ),
        FileCheckSpec(
            name="project_model",
            fallback_relative_path="models/project.py",
            required=True,
            description="Project model.",
        ),
        FileCheckSpec(
            name="project_access_assignment_model",
            fallback_relative_path="models/project_access_assignment.py",
            required=True,
            description="Canonical App-to-Chunk access projection model.",
        ),
        FileCheckSpec(
            name="project_access_model",
            fallback_relative_path="models/project_access.py",
            required=True,
            description="Legacy-compatible project roles, groups and assignments models.",
        ),
        FileCheckSpec(
            name="universe_model",
            fallback_relative_path="models/universe.py",
            required=True,
            description="Universe model.",
        ),
        FileCheckSpec(
            name="world_model",
            fallback_relative_path="models/world.py",
            required=True,
            description="WorldInstance model.",
        ),
        FileCheckSpec(
            name="block_model",
            fallback_relative_path="models/block.py",
            required=True,
            description="BlockRegistry and BlockType models.",
        ),
        FileCheckSpec(
            name="chunk_model",
            fallback_relative_path="models/chunk.py",
            required=True,
            description="ChunkSnapshot model.",
        ),
        FileCheckSpec(
            name="event_model",
            fallback_relative_path="models/event.py",
            required=True,
            description="WorldCommandLog and ChunkEvent models.",
        ),
        FileCheckSpec(
            name="object_model",
            fallback_relative_path="models/object.py",
            required=True,
            description="WorldObjectInstance and WorldObjectChunkRef models.",
        ),
        FileCheckSpec(
            name="settings_module",
            fallback_relative_path="src/bootstrap/settings.py",
            required=True,
            description="Central startup/bootstrap settings module.",
        ),
        FileCheckSpec(
            name="runtime_checks_module",
            fallback_relative_path="src/bootstrap/runtime_checks.py",
            required=True,
            description="Read-only runtime checks module.",
        ),
        FileCheckSpec(
            name="schema_bootstrap_module",
            fallback_relative_path="src/bootstrap/schema_bootstrap.py",
            required=True,
            description="Explicit schema bootstrap and read-only schema status.",
        ),
        FileCheckSpec(
            name="default_seed_module",
            fallback_relative_path="src/bootstrap/default_seed.py",
            required=True,
            description="Explicit default graph and project-access seed module.",
        ),
        FileCheckSpec(
            name="db_bootstrap_module",
            fallback_relative_path="src/bootstrap/db_bootstrap.py",
            required=True,
            description="Explicit database bootstrap orchestrator.",
        ),
        FileCheckSpec(
            name="service_auth_service",
            fallback_relative_path="src/services/service_auth_service.py",
            required=True,
            description="Trusted internal service-authentication service.",
        ),
        FileCheckSpec(
            name="project_access_service",
            fallback_relative_path="src/services/project_access_service.py",
            required=True,
            description="Canonical App-owned project-access projection service.",
        ),
        FileCheckSpec(
            name="project_provisioning_service",
            fallback_relative_path="src/services/project_provisioning_service.py",
            required=True,
            description="Idempotent project/universe/world provisioning service.",
        ),
        FileCheckSpec(
            name="project_access_package_legacy",
            fallback_relative_path="src/project_access/__init__.py",
            required=False,
            description="Optional legacy project-access import facade.",
        ),
        FileCheckSpec(
            name="project_access_service_legacy",
            fallback_relative_path="src/project_access/service.py",
            required=False,
            description="Optional legacy project-access service facade.",
        ),
        FileCheckSpec(
            name="flat_world_config",
            fallback_relative_path="src/world/flat/world.json",
            required=True,
            description="Flat provider world configuration.",
        ),
        FileCheckSpec(
            name="routes_projects",
            fallback_relative_path="routes/projects.py",
            required=True,
            description="Project/bootstrap routes.",
        ),
        FileCheckSpec(
            name="routes_worlds",
            fallback_relative_path="routes/worlds.py",
            required=True,
            description="World routes.",
        ),
        FileCheckSpec(
            name="routes_blocks",
            fallback_relative_path="routes/blocks.py",
            required=True,
            description="Block routes.",
        ),
        FileCheckSpec(
            name="routes_chunks",
            fallback_relative_path="routes/chunks.py",
            required=True,
            description="Chunk load routes.",
        ),
        FileCheckSpec(
            name="routes_commands",
            fallback_relative_path="routes/commands.py",
            required=True,
            description="Command routes.",
        ),
        FileCheckSpec(
            name="routes_editor_legacy",
            fallback_relative_path="routes/editor.py",
            required=False,
            description="Optional legacy editor route module.",
        ),
    )


@lru_cache(maxsize=1)
def get_default_route_check_specs() -> tuple[RouteCheckSpec, ...]:
    """Return default route checks."""
    return (
        RouteCheckSpec(
            name="root",
            rule="/",
            required=False,
            description="Root probe route.",
        ),
        RouteCheckSpec(
            name="projects_status",
            rule="/projects/_status",
            required=True,
            description="Project service status route.",
        ),
        RouteCheckSpec(
            name="worlds_status",
            rule="/worlds/_status",
            required=True,
            description="World service status route.",
        ),
        RouteCheckSpec(
            name="blocks_status",
            rule="/blocks/_status",
            required=True,
            description="Block service status route.",
        ),
        RouteCheckSpec(
            name="chunks_status",
            rule="/chunks/_status",
            required=True,
            description="Chunk service status route.",
        ),
        RouteCheckSpec(
            name="commands_status",
            rule="/commands/_status",
            required=True,
            description="Command service status route.",
        ),
        RouteCheckSpec(
            name="project_bootstrap",
            rule="/projects/<project_id>/bootstrap",
            required=True,
            description="Project bootstrap route.",
        ),
        RouteCheckSpec(
            name="project_worlds",
            rule="/projects/<project_id>/worlds",
            required=True,
            description="Project-scoped world list route.",
        ),
        RouteCheckSpec(
            name="project_access_summary",
            rule="/projects/<project_id>/access",
            required=False,
            description=(
                "Prepared project-access summary route; optional until the "
                "access blueprint is registered."
            ),
        ),
        RouteCheckSpec(
            name="project_roles",
            rule="/projects/<project_id>/roles",
            required=False,
            description=(
                "Prepared project-role route; optional while authorization "
                "enforcement remains disabled."
            ),
        ),
        RouteCheckSpec(
            name="project_groups",
            rule="/projects/<project_id>/groups",
            required=False,
            description=(
                "Prepared project-group route; optional while authorization "
                "enforcement remains disabled."
            ),
        ),
        RouteCheckSpec(
            name="project_access_initialize",
            rule="/projects/<project_id>/access/initialize",
            required=False,
            description="Trusted App/init access-projection initialization route.",
        ),
        RouteCheckSpec(
            name="project_access_assignments",
            rule="/projects/<project_id>/access/assignments",
            required=False,
            description="Canonical access-assignment management route.",
            alternatives=("/projects/<project_id>/assignments",),
        ),
        RouteCheckSpec(
            name="project_owner_transfer",
            rule="/projects/<project_id>/access/transfer-owner",
            required=False,
            description="Dedicated owner-transfer route.",
        ),
        RouteCheckSpec(
            name="project_world_metadata",
            rule="/projects/<project_id>/worlds/<world_id>",
            required=True,
            description="Project-scoped world metadata route.",
        ),
        RouteCheckSpec(
            name="project_world_blocks",
            rule="/projects/<project_id>/worlds/<world_id>/blocks",
            required=True,
            description="Project-scoped block route.",
        ),
        RouteCheckSpec(
            name="project_world_chunk",
            rule="/projects/<project_id>/worlds/<world_id>/chunks",
            required=True,
            description="Project-scoped single chunk route.",
        ),
        RouteCheckSpec(
            name="project_world_chunk_batch",
            rule="/projects/<project_id>/worlds/<world_id>/chunks/batch",
            required=True,
            description="Project-scoped chunk batch route.",
        ),
        RouteCheckSpec(
            name="project_world_commands",
            rule="/projects/<project_id>/worlds/<world_id>/commands",
            required=True,
            description="Project-scoped command route.",
        ),
        RouteCheckSpec(
            name="world_test",
            rule="/world-test",
            required=False,
            description="Debug world-test UI route.",
        ),
        RouteCheckSpec(
            name="world_test_health",
            rule="/world-test/api/health",
            required=False,
            description="Debug world-test health route.",
        ),
    )


def get_default_path_check_spec_data() -> list[dict[str, Any]]:
    """Return default path specs as dicts."""
    return [asdict(spec) for spec in get_default_path_check_specs()]


def get_default_file_check_spec_data() -> list[dict[str, Any]]:
    """Return default file specs as dicts."""
    return [asdict(spec) for spec in get_default_file_check_specs()]


def get_default_route_check_spec_data() -> list[dict[str, Any]]:
    """Return default route specs as dicts."""
    return [asdict(spec) for spec in get_default_route_check_specs()]


# -----------------------------------------------------------------------------
# Metadata collection
# -----------------------------------------------------------------------------

def collect_app_metadata(app: Flask, settings: BootstrapSettings | None = None) -> dict[str, Any]:
    """
    Collect compact app metadata.

    This function must not query product data.
    """
    metadata: dict[str, Any] = {}

    try:
        if settings is None:
            settings = build_bootstrap_settings(app)
    except Exception:
        settings = None

    try:
        template_folder = app.template_folder
    except Exception:
        template_folder = None

    try:
        static_folder = app.static_folder
    except Exception:
        static_folder = None

    try:
        static_url_path = app.static_url_path
    except Exception:
        static_url_path = None

    try:
        instance_path = app.instance_path
    except Exception:
        instance_path = None

    try:
        app_name = app.name
    except Exception:
        app_name = ""

    metadata.update(
        {
            "serviceName": DEFAULT_SERVICE_NAME,
            "displayName": DEFAULT_DISPLAY_NAME,
            "flaskAppName": _safe_str(app_name, ""),
            "templateFolder": _safe_str(template_folder, ""),
            "staticFolder": _safe_str(static_folder, ""),
            "staticUrlPath": _safe_str(static_url_path, ""),
            "instancePath": _safe_str(instance_path, ""),
            "serviceRoot": _safe_path_to_string(resolve_service_root(app)),
            "collectedAt": _utc_now_iso(),
        }
    )

    if settings is not None:
        try:
            metadata.update(
                {
                    "serviceName": settings.identity.service_name,
                    "displayName": settings.identity.display_name,
                    "configName": settings.identity.config_name,
                    "mode": settings.identity.mode,
                    "isRuntimeMode": settings.identity.is_runtime_mode,
                    "isDbBootstrapMode": settings.identity.is_db_bootstrap_mode,
                    "runStartupHooks": settings.runtime.run_startup_hooks,
                    "startupStrict": settings.runtime.startup_strict,
                    "checkDatabase": settings.runtime.check_database,
                    "requireDatabase": settings.runtime.require_database,
                    "allowRuntimeDbMutations": settings.runtime.allow_runtime_db_mutations,
                    "autoCreateAllInRuntime": settings.runtime.auto_create_all_in_runtime,
                    "autoSeedDefaultsInRuntime": settings.runtime.auto_seed_defaults_in_runtime,
                    "projectId": settings.world_defaults.project_id,
                    "universeId": settings.world_defaults.universe_id,
                    "worldId": settings.world_defaults.world_id,
                    "templateId": settings.world_defaults.template_id,
                    "providerWorldId": settings.world_defaults.provider_world_id,
                    "blockRegistryId": settings.world_defaults.block_registry_id,
                    "blockRegistryVersion": settings.world_defaults.block_registry_version,
                    **_public_identity_fields(
                        app,
                        _resolve_default_project_owner_auth_user_id(app, settings),
                    ),
                    "projectAccessSourceOfTruth": _expected_access_source_service(app),
                    "projectAccessViewerReadOnly": _viewer_read_only_expected(app),
                    "projectAccessAuthzEnforced": _project_access_authz_enforced(app),
                }
            )
        except Exception:
            pass
    else:
        metadata.update(
            {
                "serviceName": get_str_setting(app, "SERVICE_NAME", DEFAULT_SERVICE_NAME),
                "displayName": get_str_setting(app, "APP_DISPLAY_NAME", DEFAULT_DISPLAY_NAME),
                "configName": get_str_setting(app, "VECTOPLAN_CHUNK_CONFIG", "development"),
                "projectId": _resolve_default_project_id(app, settings),
                **_public_identity_fields(
                    app,
                    _resolve_default_project_owner_auth_user_id(app, settings),
                ),
                "projectAccessSourceOfTruth": _expected_access_source_service(app),
                "projectAccessViewerReadOnly": _viewer_read_only_expected(app),
                "projectAccessAuthzEnforced": _project_access_authz_enforced(app),
            }
        )

    return metadata


# -----------------------------------------------------------------------------
# Path/file checks
# -----------------------------------------------------------------------------

def run_path_checks(
    app: Flask,
    strict: bool = False,
    specs: Sequence[PathCheckSpec] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Run configured path checks.

    Returns:
        (results, warnings, errors)
    """
    results: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for spec in specs or get_default_path_check_specs():
        resolved_path = resolve_configured_path(
            app,
            spec.config_key,
            spec.fallback_relative_path,
        )

        exists = _safe_path_exists(resolved_path)
        is_dir = _safe_is_dir(resolved_path)
        ok = exists and is_dir

        if ok:
            status = CHECK_STATUS_OK
        elif not exists:
            status = CHECK_STATUS_MISSING
        else:
            status = CHECK_STATUS_INVALID_TYPE

        result = _make_check_item(
            name=spec.name,
            status=status,
            ok=ok or not spec.required,
            required=spec.required,
            description=spec.description,
            details={
                "configKey": spec.config_key,
                "fallbackRelativePath": spec.fallback_relative_path,
                "path": _safe_path_to_string(resolved_path),
                "exists": exists,
                "isDir": is_dir,
            },
        )
        results.append(result)

        if spec.required and not ok:
            message = (
                f"Required startup directory not available: "
                f"{spec.name} ({_safe_path_to_string(resolved_path)})"
            )
            error = _make_message(
                code="required_path_missing",
                message=message,
                details={
                    "name": spec.name,
                    "path": _safe_path_to_string(resolved_path),
                    "status": status,
                },
            )
            errors.append(error)
            if strict:
                break

        elif not spec.required and not ok:
            warnings.append(
                _make_message(
                    code="optional_path_missing",
                    message=(
                        f"Optional startup directory not fully available: "
                        f"{spec.name} ({_safe_path_to_string(resolved_path)})"
                    ),
                    details={
                        "name": spec.name,
                        "path": _safe_path_to_string(resolved_path),
                        "status": status,
                    },
                )
            )

    return results, warnings, errors


def run_file_checks(
    app: Flask,
    strict: bool = False,
    specs: Sequence[FileCheckSpec] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Run configured file checks.

    Returns:
        (results, warnings, errors)
    """
    results: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    service_root = resolve_service_root(app)

    for spec in specs or get_default_file_check_specs():
        try:
            file_path = service_root.joinpath(spec.fallback_relative_path)
        except Exception:
            file_path = resolve_service_root_from_file().joinpath(spec.fallback_relative_path)

        exists = _safe_path_exists(file_path)
        is_file = _safe_is_file(file_path)
        ok = exists and is_file

        if ok:
            status = CHECK_STATUS_OK
        elif not exists:
            status = CHECK_STATUS_MISSING
        else:
            status = CHECK_STATUS_INVALID_TYPE

        result = _make_check_item(
            name=spec.name,
            status=status,
            ok=ok or not spec.required,
            required=spec.required,
            description=spec.description,
            details={
                "fallbackRelativePath": spec.fallback_relative_path,
                "path": _safe_path_to_string(file_path),
                "exists": exists,
                "isFile": is_file,
            },
        )
        results.append(result)

        if spec.required and not ok:
            message = (
                f"Required startup file not available: "
                f"{spec.name} ({_safe_path_to_string(file_path)})"
            )
            error = _make_message(
                code="required_file_missing",
                message=message,
                details={
                    "name": spec.name,
                    "path": _safe_path_to_string(file_path),
                    "status": status,
                },
            )
            errors.append(error)
            if strict:
                break

        elif not spec.required and not ok:
            warnings.append(
                _make_message(
                    code="optional_file_missing",
                    message=(
                        f"Optional startup file not fully available: "
                        f"{spec.name} ({_safe_path_to_string(file_path)})"
                    ),
                    details={
                        "name": spec.name,
                        "path": _safe_path_to_string(file_path),
                        "status": status,
                    },
                )
            )

    return results, warnings, errors


# -----------------------------------------------------------------------------
# Route checks
# -----------------------------------------------------------------------------

def _normalize_route_rule(rule: Any) -> str:
    """Normalize Flask converter syntax and trailing slashes."""
    text_value = _safe_str(rule, "")
    if not text_value:
        return ""
    if not text_value.startswith("/"):
        text_value = "/" + text_value
    text_value = re.sub(r"<[^:<>]+:([^<>]+)>", r"<\1>", text_value)
    text_value = re.sub(r"/{2,}", "/", text_value)
    if len(text_value) > 1:
        text_value = text_value.rstrip("/")
    return text_value


def _configured_api_prefix(app: Any) -> str:
    """Return the normalized optional API prefix."""
    for key in (
        "VECTOPLAN_CHUNK_API_PREFIX",
        "APPLICATION_ROOT",
        "SCRIPT_NAME",
    ):
        try:
            value = get_str_setting(app, key, "", prefer_env=True)
        except Exception:
            value = _safe_str(_config_value(app, key, ""), "")
        normalized = _normalize_route_rule(value)
        if normalized and normalized != "/":
            return normalized
    return ""


def _accepted_route_variants(app: Any, rule: str) -> tuple[str, ...]:
    """Return exact normalized variants including the configured API prefix."""
    normalized = _normalize_route_rule(rule)
    prefix = _configured_api_prefix(app)
    variants = [normalized]
    if prefix and normalized:
        variants.append(prefix + normalized)
    return tuple(dict.fromkeys(value for value in variants if value))


def collect_route_rules(app: Flask) -> list[str]:
    """Collect all raw Flask route rules."""
    try:
        return sorted({str(rule.rule) for rule in app.url_map.iter_rules()})
    except Exception:
        return []


def _find_matching_route(
    app: Any,
    raw_rules: Sequence[str],
    expected_rules: Sequence[str],
) -> tuple[str | None, str | None]:
    """Find an exact normalized route, allowing configured path prefixes."""
    normalized_actual = {
        _normalize_route_rule(raw): raw
        for raw in raw_rules
        if _normalize_route_rule(raw)
    }
    for expected in expected_rules:
        for candidate in _accepted_route_variants(app, expected):
            raw = normalized_actual.get(candidate)
            if raw is not None:
                return raw, candidate
        normalized_expected = _normalize_route_rule(expected)
        if normalized_expected:
            for normalized, raw in normalized_actual.items():
                if normalized.endswith(normalized_expected):
                    prefix = normalized[: -len(normalized_expected)]
                    if prefix and prefix.startswith("/"):
                        return raw, normalized
    return None, None


def run_route_checks(
    app: Flask,
    strict: bool = False,
    specs: Sequence[RouteCheckSpec] | None = None,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Run converter- and prefix-aware registered-route checks."""
    route_rules = collect_route_rules(app)
    normalized_rules = sorted(
        {_normalize_route_rule(rule) for rule in route_rules if rule}
    )

    results: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    required_missing: list[str] = []
    optional_missing: list[str] = []

    for spec in specs or get_default_route_check_specs():
        accepted = (spec.rule, *tuple(spec.alternatives or ()))
        matched_raw, matched_normalized = _find_matching_route(
            app,
            route_rules,
            accepted,
        )
        exists = matched_raw is not None
        ok = exists or not spec.required
        status = CHECK_STATUS_OK if exists else CHECK_STATUS_MISSING

        result = _make_check_item(
            name=spec.name,
            status=status,
            ok=ok,
            required=spec.required,
            description=spec.description,
            details={
                "rule": spec.rule,
                "alternatives": list(spec.alternatives or ()),
                "acceptedVariants": [
                    variant
                    for item in accepted
                    for variant in _accepted_route_variants(app, item)
                ],
                "matchedRule": matched_raw,
                "matchedNormalizedRule": matched_normalized,
                "exists": exists,
            },
        )
        results.append(result)

        if spec.required and not exists:
            required_missing.append(spec.rule)
            error = _make_message(
                code="required_route_missing",
                message=f"Required route is missing: {spec.rule}",
                details={
                    "name": spec.name,
                    "rule": spec.rule,
                    "alternatives": list(spec.alternatives or ()),
                },
            )
            errors.append(error)
            if strict:
                break
        elif not spec.required and not exists:
            optional_missing.append(spec.rule)
            warnings.append(
                _make_message(
                    code="optional_route_missing",
                    message=f"Optional route is missing: {spec.rule}",
                    details={
                        "name": spec.name,
                        "rule": spec.rule,
                        "alternatives": list(spec.alternatives or ()),
                    },
                )
            )

    route_summary = {
        "count": len(route_rules),
        "requiredMissing": required_missing,
        "optionalMissing": optional_missing,
        "rules": route_rules,
        "normalizedRules": normalized_rules,
        "apiPrefix": _configured_api_prefix(app),
    }

    return results, route_summary, warnings, errors


# -----------------------------------------------------------------------------
# Model checks
# -----------------------------------------------------------------------------

def _load_project_access_service_status(app: Any = None) -> dict[str, Any]:
    """Load canonical service contract diagnostics without querying the DB."""
    import_errors: list[str] = []
    candidates = (
        "src.services.project_access_service",
        "services.project_access_service",
        "src.project_access",
        "project_access",
    )

    for import_path in candidates:
        try:
            module = importlib.import_module(import_path)
        except Exception as exc:
            import_errors.append(
                f"{import_path}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )
            continue

        status_factory = getattr(module, "get_project_access_service_status", None)
        legacy_factory = getattr(module, "get_project_access_package_status", None)
        factory = status_factory if callable(status_factory) else legacy_factory
        if not callable(factory):
            import_errors.append(f"{import_path}: status factory unavailable")
            continue

        try:
            try:
                status = factory(config=getattr(app, "config", None))
            except TypeError:
                status = factory()
            serializer = getattr(status, "to_dict", None)
            if callable(serializer):
                try:
                    status = serializer(include_traceback=False)
                except TypeError:
                    status = serializer()
            result = _safe_dict(status)
            result.setdefault("module", import_path)
            result.setdefault("ready", result.get("ok"))
            return result
        except Exception as exc:
            import_errors.append(
                f"{import_path}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )

    return {
        "ok": False,
        "ready": False,
        "error": " | ".join(import_errors),
        "importCandidates": list(candidates),
    }


def _project_access_service_contract_ready(
    app: Any,
    status: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Validate immutable access-service safety properties."""
    data = _safe_dict(status)
    allowed_roles = {
        _safe_str(role, "").lower()
        for role in _safe_list(data.get("allowed_roles") or data.get("allowedRoles"))
        if _safe_str(role, "")
    }
    expected_roles = set(DEFAULT_PROJECT_ROLE_KEYS)
    source = _safe_str(
        data.get("source_service", data.get("sourceService")),
        "",
    ).lower()
    canonical_field = _safe_str(
        data.get("canonical_user_id_field", data.get("canonicalUserIdField")),
        "",
    ).lower()
    viewer_read_only = _safe_bool(
        data.get("viewer_read_only", data.get("viewerReadOnly")),
        False,
    )
    checks = {
        "serviceReady": _safe_bool(data.get("ok", data.get("ready")), False),
        "enabled": _safe_bool(data.get("enabled"), False),
        "defaultDeny": _safe_bool(data.get("default_deny", data.get("defaultDeny")), False),
        "canonicalUserIdFieldReady": canonical_field == DEFAULT_CANONICAL_USER_ID_FIELD,
        "rolesReady": allowed_roles == expected_roles,
        "viewerReadOnly": viewer_read_only,
        "publicMutationsDisabled": not _safe_bool(
            data.get("allow_public_mutations", data.get("allowPublicMutations")),
            False,
        ),
        "identityOverrideDisabled": not _safe_bool(
            data.get("allow_identity_override", data.get("allowIdentityOverride")),
            False,
        ),
        "sourceOfTruthReady": source == _expected_access_source_service(app),
    }
    return all(checks.values()), {
        **checks,
        "sourceOfTruth": source,
        "expectedSourceOfTruth": _expected_access_source_service(app),
        "canonicalUserIdField": canonical_field,
        "allowedRoles": sorted(allowed_roles),
    }


def run_model_registry_check(
    app: Flask,
    strict: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Check model registry plus canonical and legacy access contracts."""
    del strict
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        models_module = importlib.import_module("models")
        require_models_ready = getattr(models_module, "require_models_ready")
        require_expected_model_columns = getattr(
            models_module,
            "require_expected_model_columns",
        )
    except Exception as exc:
        message = (
            "Could not import model registry helpers: "
            f"{_safe_exception_message(exc)}"
        )
        errors.append(
            _make_message(
                code=MODEL_CHECK_IMPORT_ERROR_CODE,
                message=message,
                details={"exceptionType": exc.__class__.__name__},
            )
        )
        return {
            "ok": False,
            "status": CHECK_STATUS_FAILED,
            "errorCode": MODEL_CHECK_IMPORT_ERROR_CODE,
            "error": message,
            "summary": {},
            "projectAccess": {},
        }, warnings, errors

    try:
        require_models_ready()
    except Exception as exc:
        message = f"Model registry is not ready: {_safe_exception_message(exc)}"
        errors.append(
            _make_message(
                code=MODEL_CHECK_REGISTRY_ERROR_CODE,
                message=message,
                details={"exceptionType": exc.__class__.__name__},
            )
        )
        return {
            "ok": False,
            "status": CHECK_STATUS_FAILED,
            "errorCode": MODEL_CHECK_REGISTRY_ERROR_CODE,
            "error": message,
            "summary": {},
            "projectAccess": {},
        }, warnings, errors

    critical_classes = (
        "Project",
        "ProjectAccessAssignment",
        "ProjectRole",
        "ProjectGroup",
        "ProjectGroupMember",
        "ProjectRoleAssignment",
    )
    column_contract_ready = True
    column_contract_error: str | None = None
    try:
        require_expected_model_columns(class_names=critical_classes)
    except Exception as exc:
        column_contract_ready = False
        column_contract_error = _safe_exception_message(exc)
        errors.append(
            _make_message(
                code="project_access_model_columns_not_ready",
                message=(
                    "Project owner/access model columns are incomplete: "
                    f"{column_contract_error}"
                ),
                details={
                    "exceptionType": exc.__class__.__name__,
                    "classNames": list(critical_classes),
                },
            )
        )

    def optional_call(name: str, default: Any) -> Any:
        function = getattr(models_module, name, None)
        if not callable(function):
            return default
        try:
            return function()
        except Exception:
            return default

    summary = _safe_dict(optional_call("get_model_debug_summary", {}))
    model_contract = _safe_dict(
        optional_call("get_project_access_model_contract", {})
    )
    canonical_shape_ready = bool(
        optional_call(
            "is_project_access_projection_model_shape_ready",
            optional_call("is_project_access_model_shape_ready", False),
        )
    )
    legacy_shape_ready = bool(
        optional_call(
            "is_legacy_project_access_model_shape_ready",
            optional_call("is_project_access_model_shape_ready", False),
        )
    )
    combined_shape_ready = bool(
        optional_call(
            "is_project_access_model_shape_ready",
            canonical_shape_ready and legacy_shape_ready,
        )
    )
    app_integration_shape_ready = bool(
        optional_call("is_app_integration_model_shape_ready", False)
    )

    service_status = _load_project_access_service_status(app)
    service_contract_ready, service_contract = (
        _project_access_service_contract_ready(app, service_status)
    )

    for ready, code, message, details in (
        (
            canonical_shape_ready,
            "project_access_projection_model_shape_not_ready",
            "Canonical project-access projection model shape is not ready.",
            {"contract": model_contract},
        ),
        (
            legacy_shape_ready,
            "legacy_project_access_model_shape_not_ready",
            "Legacy project-role/group compatibility model shape is not ready.",
            {"contract": model_contract},
        ),
        (
            combined_shape_ready,
            "project_access_model_shape_not_ready",
            "Combined project-access model shape is not ready.",
            {"contract": model_contract},
        ),
        (
            app_integration_shape_ready,
            "project_app_integration_shape_not_ready",
            "Project model does not expose the required provisioning/access columns.",
            {"summary": summary},
        ),
        (
            service_contract_ready,
            "project_access_service_contract_not_ready",
            "Project-access service safety contract is not ready.",
            {"serviceContract": service_contract},
        ),
    ):
        if not ready:
            errors.append(_make_message(code=code, message=message, details=details))

    ok = bool(
        column_contract_ready
        and canonical_shape_ready
        and legacy_shape_ready
        and combined_shape_ready
        and app_integration_shape_ready
        and service_contract_ready
        and not errors
    )

    return {
        "ok": ok,
        "status": CHECK_STATUS_OK if ok else CHECK_STATUS_FAILED,
        "summary": summary,
        "appIntegrationShapeReady": app_integration_shape_ready,
        "projectAccessShapeReady": combined_shape_ready,
        "canonicalProjectAccessShapeReady": canonical_shape_ready,
        "legacyProjectAccessShapeReady": legacy_shape_ready,
        "criticalColumnContractReady": column_contract_ready,
        "criticalColumnContractError": column_contract_error,
        "projectAccess": {
            "serviceReady": service_contract_ready,
            "serviceStatus": service_status,
            "serviceContract": service_contract,
            "modelContract": model_contract,
            "sourceOfTruth": _expected_access_source_service(app),
            "viewerReadOnly": service_contract.get("viewerReadOnly"),
            "authzEnforced": _project_access_authz_enforced(app),
        },
    }, warnings, errors


# -----------------------------------------------------------------------------
# Database checks
# -----------------------------------------------------------------------------

def _run_extensions_database_status_check(
    app: Flask,
    check_connection: bool,
) -> dict[str, Any] | None:
    """Try extensions.get_database_status() if available."""
    if get_database_status is None:
        return None

    try:
        status = get_database_status(app, check_connection=check_connection)
    except Exception as exc:
        return {
            "available": False,
            "configured": False,
            "connectionChecked": check_connection,
            "connectionOk": False,
            "connectionError": _safe_exception_message(exc),
            "source": "extensions.get_database_status",
        }

    if isinstance(status, Mapping):
        result = dict(status)
    else:
        result = {
            "available": bool(status),
            "configured": bool(status),
            "connectionChecked": check_connection,
            "connectionOk": bool(status),
        }

    result.setdefault("source", "extensions.get_database_status")
    return result


def _run_direct_database_ping(app: Flask, check_connection: bool) -> dict[str, Any]:
    """
    Run a minimal direct DB ping.

    This is read-only and does not use ORM queries.
    """
    configured = False
    uri = ""

    try:
        uri = get_str_setting(
            app,
            "SQLALCHEMY_DATABASE_URI",
            "",
            aliases=(
                "VECTOPLAN_CHUNK_SQLALCHEMY_DATABASE_URI",
                "VECTOPLAN_CHUNK_DATABASE_URI",
                "DATABASE_URL",
            ),
        )
        configured = bool(uri)
    except Exception:
        configured = False

    result: dict[str, Any] = {
        "available": db is not None,
        "configured": configured,
        "connectionChecked": bool(check_connection),
        "connectionOk": None,
        "connectionError": None,
        "source": "runtime_checks.direct_ping",
    }

    if not check_connection:
        return result

    if db is None:
        result["connectionOk"] = False
        result["connectionError"] = "SQLAlchemy db extension is unavailable."
        return result

    if text is None:
        result["connectionOk"] = False
        result["connectionError"] = "sqlalchemy.text is unavailable."
        return result

    connection = None

    try:
        connection = db.engine.connect()
        connection.execute(text("SELECT 1"))
        result["connectionOk"] = True
    except Exception as exc:
        result["connectionOk"] = False
        result["connectionError"] = _safe_exception_message(exc)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

        try:
            db.session.remove()
        except Exception:
            pass

    return result


def run_database_check(
    app: Flask,
    check_connection: bool | None = None,
    require_ok: bool | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Run cheap DB availability check.

    This function does not create tables and does not query product data.
    """
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if check_connection is None:
        try:
            check_connection = should_check_database_on_startup(app)
        except Exception:
            check_connection = True

    if require_ok is None:
        try:
            require_ok = should_require_database_on_startup(app)
        except Exception:
            require_ok = True

    try:
        status = _run_extensions_database_status_check(
            app,
            check_connection=bool(check_connection),
        )
        if status is None:
            status = _run_direct_database_ping(
                app,
                check_connection=bool(check_connection),
            )
    except Exception as exc:
        status = {
            "available": False,
            "configured": False,
            "connectionChecked": bool(check_connection),
            "connectionOk": False,
            "connectionError": _safe_exception_message(exc),
            "source": "runtime_checks.exception",
        }

    connection_checked = bool(status.get("connectionChecked"))
    connection_ok = status.get("connectionOk")

    if connection_checked and connection_ok is False:
        message = _safe_str(
            status.get("connectionError"),
            "Database connection check failed.",
        )

        item = _make_message(
            code=DATABASE_CHECK_FAILED_CODE if status.get("configured") else DATABASE_CHECK_UNAVAILABLE_CODE,
            message=message,
            details={
                "source": status.get("source"),
                "requireOk": bool(require_ok),
            },
        )

        if require_ok:
            errors.append(item)
        else:
            warnings.append(item)

    return status, warnings, errors


# -----------------------------------------------------------------------------
# Schema readiness checks
# -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_schema_readiness_api() -> Mapping[str, Any]:
    """Load immutable schema inspection callables; never cache DB state."""
    import_errors: list[str] = []

    for import_path in (
        "src.bootstrap.schema_bootstrap",
        "bootstrap.schema_bootstrap",
    ):
        try:
            module = importlib.import_module(import_path)
            exports = {
                "get_required_table_names": getattr(
                    module,
                    "get_required_table_names",
                ),
                "get_required_column_names": getattr(
                    module,
                    "get_required_column_names",
                ),
                "inspect_schema_contract": getattr(
                    module,
                    "inspect_schema_contract",
                ),
            }
            if all(callable(value) for value in exports.values()):
                return exports
            import_errors.append(f"{import_path}: required exports unavailable")
        except Exception as exc:
            import_errors.append(
                f"{import_path}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )

    raise RuntimeError(
        "Could not import read-only schema inspection API. "
        + " | ".join(import_errors)
    )


def run_schema_readiness_check(
    app: Flask,
    *,
    require_ok: bool = True,
    db_extension: Any = None,
    schema_name: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Inspect required tables/columns without creating or altering anything."""
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    db_obj = db_extension if db_extension is not None else db

    unavailable = {
        "ok": False,
        "status": CHECK_STATUS_FAILED,
        "checked": False,
        "required": bool(require_ok),
        "projectOwnerColumnsReady": False,
        "projectAccessTablesReady": False,
        "projectAccessColumnsReady": False,
        "projectAccessSchemaReady": False,
        "missingTables": [],
        "missingColumns": {},
        "mutated": False,
    }

    if db_obj is None:
        message = "SQLAlchemy db extension is unavailable for schema checks."
        item = _make_message(
            code=SCHEMA_CHECK_UNAVAILABLE_CODE,
            message=message,
            details={"requireOk": bool(require_ok)},
        )
        (errors if require_ok else warnings).append(item)
        unavailable["error"] = message
        return unavailable, warnings, errors

    try:
        api = _load_schema_readiness_api()
        required_tables = api["get_required_table_names"](
            db_extension=db_obj
        )
        required_columns = api["get_required_column_names"](
            required_tables=required_tables
        )
        snapshot = api["inspect_schema_contract"](
            app,
            db_extension=db_obj,
            required_tables=required_tables,
            required_columns=required_columns,
            schema=schema_name,
        )
        snapshot = _safe_dict(snapshot)
    except Exception as exc:
        message = (
            "Read-only schema inspection failed: "
            f"{_safe_exception_message(exc)}"
        )
        item = _make_message(
            code=SCHEMA_CHECK_FAILED_CODE,
            message=message,
            details={
                "exceptionType": exc.__class__.__name__,
                "requireOk": bool(require_ok),
            },
        )
        (errors if require_ok else warnings).append(item)
        unavailable.update(
            {
                "checked": True,
                "error": message,
                "exceptionType": exc.__class__.__name__,
            }
        )
        return unavailable, warnings, errors

    missing_tables = list(snapshot.get("missingTables") or [])
    missing_columns = _safe_dict(snapshot.get("missingColumns"))
    missing_column_count = 0
    for values in missing_columns.values():
        try:
            missing_column_count += len(values or [])
        except Exception:
            missing_column_count += 1

    ok = not missing_tables and not missing_columns
    result = {
        "ok": ok,
        "status": CHECK_STATUS_OK if ok else CHECK_STATUS_FAILED,
        "checked": True,
        "required": bool(require_ok),
        "schema": schema_name,
        "requiredTables": list(required_tables),
        "requiredColumns": dict(required_columns),
        "existingTables": list(snapshot.get("existingTables") or []),
        "existingColumns": _safe_dict(snapshot.get("existingColumns")),
        "missingTables": missing_tables,
        "missingColumns": missing_columns,
        "missingTableCount": len(missing_tables),
        "missingColumnCount": missing_column_count,
        "projectOwnerColumnsReady": _safe_bool(
            snapshot.get("projectOwnerColumnsReady"),
            False,
        ),
        "projectAccessTablesReady": _safe_bool(
            snapshot.get("projectAccessTablesReady"),
            False,
        ),
        "projectAccessColumnsReady": _safe_bool(
            snapshot.get("projectAccessColumnsReady"),
            False,
        ),
        "projectAccessSchemaReady": _safe_bool(
            snapshot.get("projectAccessSchemaReady"),
            False,
        ),
        "canonicalProjectAccessSchemaReady": _safe_bool(
            snapshot.get(
                "canonicalProjectAccessSchemaReady",
                snapshot.get("projectAccessSchemaReady"),
            ),
            False,
        ),
        "legacyProjectAccessSchemaReady": _safe_bool(
            snapshot.get(
                "legacyProjectAccessSchemaReady",
                snapshot.get("projectAccessSchemaReady"),
            ),
            False,
        ),
        "mutated": False,
    }

    if not ok:
        item = _make_message(
            code=SCHEMA_CHECK_FAILED_CODE,
            message="Required database tables or critical columns are missing.",
            details={
                "missingTables": missing_tables,
                "missingColumns": missing_columns,
                "requireOk": bool(require_ok),
                "repairHint": (
                    "Run the explicit bootstrap/migration path; runtime checks "
                    "never repair schema."
                ),
            },
        )
        (errors if require_ok else warnings).append(item)

    return result, warnings, errors


# -----------------------------------------------------------------------------
# Bounded project-owner/access readiness checks
# -----------------------------------------------------------------------------


def _cleanup_read_only_session(db_obj: Any) -> None:
    """Release a scoped read-only session and its implicit transaction."""
    if db_obj is None:
        return
    try:
        db_obj.session.remove()
    except Exception:
        try:
            db_obj.session.close()
        except Exception:
            pass


def _effective_window_ready(row: Any, now: datetime) -> bool:
    """Evaluate optional starts/expires fields without loading relationships."""
    try:
        starts_at = getattr(row, "starts_at", None)
        expires_at = getattr(row, "expires_at", None)

        if isinstance(starts_at, datetime):
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=timezone.utc)
            if now < starts_at.astimezone(timezone.utc):
                return False

        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now >= expires_at.astimezone(timezone.utc):
                return False

        return True
    except Exception:
        return False


def _empty_project_access_runtime_status(
    *,
    app: Any,
    project_id: str,
    owner_auth_user_id: str,
    required: bool,
    error: str | None = None,
    checked: bool = False,
) -> dict[str, Any]:
    identity = _public_identity_fields(app, owner_auth_user_id)
    return {
        "ok": False,
        "status": CHECK_STATUS_FAILED,
        "checked": checked,
        "required": required,
        "projectId": project_id,
        "projectDbId": None,
        "projectExists": False,
        **identity,
        "projectOwnerReady": False,
        "ownerReady": False,
        "canonicalProjectAccessReady": False,
        "canonicalReady": False,
        "legacyProjectAccessReady": False,
        "legacyReady": False,
        "rolesReady": False,
        "ownerAssignmentReady": False,
        "accessReady": False,
        "requiredRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
        "activeRoleKeys": [],
        "missingRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
        "canonicalOwnerAssignmentCount": 0,
        "legacyOwnerAssignmentCount": 0,
        "matchingOwnerAssignmentCount": 0,
        "projectAccessAssignmentCount": 0,
        "projectAccessRoleCount": 0,
        "assignmentScanTruncated": False,
        "sourceOfTruth": _expected_access_source_service(app),
        "sourceOfTruthReady": False,
        "viewerReadOnly": _viewer_read_only_expected(app),
        "authzEnforced": _project_access_authz_enforced(app),
        "effectivePermissionsCalculated": False,
        "mutated": False,
        "error": error,
    }


def _project_scoped_query(
    session: Any,
    model: Any,
    *,
    project_public_id: str,
    project_db_id: Any,
) -> Any | None:
    """Build a project-scoped query using public-id aliases before DB ids."""
    if session is None or model is None:
        return None
    public_field = _first_model_attribute(
        model,
        ("chunk_project_id", "project_public_id", "project_id"),
    )
    db_field = _first_model_attribute(model, ("project_db_id",))
    field = public_field if public_field is not None else db_field
    value = project_public_id if public_field is not None else project_db_id
    if field is None or value is None:
        return None
    return session.query(model).filter(field == value)


def _query_project_scoped_rows(
    session: Any,
    model: Any,
    *,
    project_public_id: str,
    project_db_id: Any,
    limit: int,
) -> tuple[list[Any], int, bool]:
    """Run one bounded project-scoped query using compatible field aliases."""
    query = _project_scoped_query(
        session,
        model,
        project_public_id=project_public_id,
        project_db_id=project_db_id,
    )
    if query is None:
        return [], 0, False
    try:
        count = int(query.count())
    except Exception:
        count = 0
    rows = query.limit(limit + 1).all()
    truncated = len(rows) > limit or count > limit
    return list(rows[:limit]), count, truncated


def _query_canonical_owner_rows(
    session: Any,
    model: Any,
    *,
    project_public_id: str,
    project_db_id: Any,
    limit: int,
) -> tuple[list[Any], int, bool, int]:
    """Query only canonical direct-owner rows while separately counting all assignments."""
    base_query = _project_scoped_query(
        session,
        model,
        project_public_id=project_public_id,
        project_db_id=project_db_id,
    )
    if base_query is None:
        return [], 0, False, 0
    try:
        total_assignment_count = int(base_query.count())
    except Exception:
        total_assignment_count = 0

    query = base_query
    role_field = _first_model_attribute(model, ("role", "project_role", "access_role"))
    assignment_type_field = _first_model_attribute(
        model,
        ("assignment_type", "subject_type", "principal_type"),
    )
    active_field = _first_model_attribute(model, ("active", "is_active", "enabled"))
    deleted_field = _first_model_attribute(model, ("deleted_at",))
    if role_field is not None:
        query = query.filter(role_field == "owner")
    if assignment_type_field is not None:
        query = query.filter(assignment_type_field.in_(("direct", "user")))
    if active_field is not None:
        query = query.filter(active_field.is_(True))
    if deleted_field is not None:
        query = query.filter(deleted_field.is_(None))

    try:
        owner_count = int(query.count())
    except Exception:
        owner_count = 0
    rows = query.limit(limit + 1).all()
    truncated = len(rows) > limit or owner_count > limit
    return list(rows[:limit]), owner_count, truncated, total_assignment_count


def _canonical_assignment_status(
    app: Any,
    rows: Sequence[Any],
    *,
    owner_auth_user_id: str,
    truncated: bool,
) -> dict[str, Any]:
    """Evaluate canonical direct-owner projection from bounded rows."""
    now = _utc_now()
    active_rows = [row for row in rows if _row_is_active(row, now)]
    direct_rows = [
        row
        for row in active_rows
        if _safe_str(
            _first_row_value(row, ("assignment_type", "subject_type", "principal_type"), "direct"),
            "direct",
        ).lower() in {"direct", "user"}
    ]
    owner_rows = [
        row
        for row in direct_rows
        if _safe_str(
            _first_row_value(row, ("role", "project_role", "access_role"), ""),
            "",
        ).lower() == "owner"
    ]
    matching_rows = [
        row
        for row in owner_rows
        if _safe_str(
            _first_row_value(
                row,
                ("auth_user_id", "subject_auth_user_id", "principal_id", "subject_id"),
                "",
            ),
            "",
        ) == owner_auth_user_id
    ]
    expected_source = _expected_access_source_service(app)
    owner_sources = {
        _safe_str(
            _first_row_value(row, ("source_service", "managed_by", "source"), ""),
            "",
        ).lower()
        for row in matching_rows
    }
    source_ready = bool(matching_rows and owner_sources == {expected_source})
    ready = bool(
        not truncated
        and len(owner_rows) == 1
        and len(matching_rows) == 1
        and source_ready
        and _is_canonical_auth_user_id(owner_auth_user_id)
    )
    return {
        "ready": ready,
        "activeAssignmentCount": len(active_rows),
        "directAssignmentCount": len(direct_rows),
        "ownerAssignmentCount": len(owner_rows),
        "matchingOwnerAssignmentCount": len(matching_rows),
        "sourceOfTruth": next(iter(owner_sources), expected_source),
        "sourceOfTruthReady": source_ready,
        "scanTruncated": truncated,
    }


def _legacy_access_status(
    session: Any,
    ProjectRole: Any,
    ProjectRoleAssignment: Any,
    *,
    project_db_id: Any,
    owner_auth_user_id: str,
) -> dict[str, Any]:
    """Evaluate the retained role/assignment compatibility projection."""
    role_field = _first_model_attribute(ProjectRole, ("project_db_id",))
    role_value = project_db_id
    if role_field is None:
        role_field = _first_model_attribute(
            ProjectRole,
            ("chunk_project_id", "project_public_id", "project_id"),
        )
    if role_field is None or role_value is None:
        role_rows, stored_role_count, role_truncated = [], 0, False
    else:
        role_query = session.query(ProjectRole).filter(role_field == role_value)
        role_key_field = _first_model_attribute(
            ProjectRole,
            ("role_key", "role", "project_role"),
        )
        if role_key_field is not None:
            role_query = role_query.filter(role_key_field.in_(DEFAULT_PROJECT_ROLE_KEYS))
        try:
            stored_role_count = int(role_query.count())
        except Exception:
            stored_role_count = 0
        raw_role_rows = role_query.limit(
            max(PROJECT_ACCESS_QUERY_ROW_LIMIT, len(DEFAULT_PROJECT_ROLE_KEYS) * 4) + 1
        ).all()
        role_truncated = bool(
            len(raw_role_rows) > max(PROJECT_ACCESS_QUERY_ROW_LIMIT, len(DEFAULT_PROJECT_ROLE_KEYS) * 4)
            or stored_role_count > max(PROJECT_ACCESS_QUERY_ROW_LIMIT, len(DEFAULT_PROJECT_ROLE_KEYS) * 4)
        )
        role_rows = list(
            raw_role_rows[: max(PROJECT_ACCESS_QUERY_ROW_LIMIT, len(DEFAULT_PROJECT_ROLE_KEYS) * 4)]
        )
    now = _utc_now()
    active_roles: dict[str, Any] = {}
    duplicates: set[str] = set()
    for role in role_rows:
        role_key = _safe_str(
            _first_row_value(role, ("role_key", "role", "project_role"), ""),
            "",
        ).lower()
        if role_key not in DEFAULT_PROJECT_ROLE_KEYS or not _row_is_active(role, now):
            continue
        if role_key in active_roles:
            duplicates.add(role_key)
        else:
            active_roles[role_key] = role

    missing = sorted(set(DEFAULT_PROJECT_ROLE_KEYS) - set(active_roles))
    roles_ready = bool(not role_truncated and not missing and not duplicates)
    owner_role = active_roles.get("owner")
    owner_role_db_id = _first_row_value(owner_role, ("id",), None)
    owner_role_id = _safe_str(
        _first_row_value(owner_role, ("role_id",), ""),
        "",
    ) or None

    assignment_rows: list[Any] = []
    assignment_count = 0
    assignment_truncated = False
    if owner_role_db_id is not None and ProjectRoleAssignment is not None:
        project_field = _first_model_attribute(ProjectRoleAssignment, ("project_db_id",))
        role_field = _first_model_attribute(ProjectRoleAssignment, ("role_db_id",))
        if project_field is not None and role_field is not None:
            query = session.query(ProjectRoleAssignment).filter(
                project_field == project_db_id,
                role_field == owner_role_db_id,
            )
            try:
                assignment_count = int(query.count())
            except Exception:
                assignment_count = 0
            rows = query.limit(PROJECT_ACCESS_QUERY_ROW_LIMIT + 1).all()
            assignment_truncated = (
                len(rows) > PROJECT_ACCESS_QUERY_ROW_LIMIT
                or assignment_count > PROJECT_ACCESS_QUERY_ROW_LIMIT
            )
            assignment_rows = list(rows[:PROJECT_ACCESS_QUERY_ROW_LIMIT])

    effective_rows = [row for row in assignment_rows if _row_is_active(row, now)]
    matching_rows = [
        row
        for row in effective_rows
        if _safe_str(
            _first_row_value(row, ("subject_type", "assignment_type"), "user"),
            "user",
        ).lower() in {"user", "direct"}
        and _safe_str(
            _first_row_value(row, ("user_id", "auth_user_id", "subject_auth_user_id"), ""),
            "",
        ) == owner_auth_user_id
    ]
    owner_assignment_ready = bool(
        not assignment_truncated
        and len(effective_rows) == 1
        and len(matching_rows) == 1
    )
    ready = bool(roles_ready and owner_assignment_ready)
    return {
        "ready": ready,
        "rolesReady": roles_ready,
        "ownerAssignmentReady": owner_assignment_ready,
        "activeRoleKeys": sorted(active_roles),
        "missingRoleKeys": missing,
        "duplicateActiveRoleKeys": sorted(duplicates),
        "ownerRoleId": owner_role_id,
        "ownerRoleDbId": owner_role_db_id,
        "roleCount": stored_role_count,
        "activeOwnerAssignmentCount": len(effective_rows),
        "matchingOwnerAssignmentCount": len(matching_rows),
        "storedOwnerAssignmentCount": assignment_count,
        "scanTruncated": bool(role_truncated or assignment_truncated),
    }


def run_project_access_readiness_check(
    app: Flask,
    *,
    settings: Any = None,
    require_ok: bool = False,
    db_extension: Any = None,
    project_id: str | None = None,
    owner_user_id: str | None = None,
    owner_auth_user_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Check canonical and legacy access projections using bounded read-only queries."""
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    db_obj = db_extension if db_extension is not None else db

    resolved_project_id = _safe_str(
        project_id,
        _resolve_default_project_id(app, settings),
    )
    resolved_owner = _normalize_owner_auth_user_id(
        owner_auth_user_id
        or owner_user_id
        or _resolve_default_project_owner_auth_user_id(app, settings)
    )
    empty = _empty_project_access_runtime_status(
        app=app,
        project_id=resolved_project_id,
        owner_auth_user_id=resolved_owner,
        required=bool(require_ok),
    )

    if db_obj is None:
        message = "SQLAlchemy db extension is unavailable for access checks."
        item = _make_message(
            code=PROJECT_ACCESS_CHECK_UNAVAILABLE_CODE,
            message=message,
            details={"requireOk": bool(require_ok)},
        )
        (errors if require_ok else warnings).append(item)
        empty["error"] = message
        return empty, warnings, errors

    try:
        models_module = importlib.import_module("models")
        require_model_class = getattr(models_module, "require_model_class")
        Project = require_model_class("Project")
        ProjectAccessAssignment = require_model_class("ProjectAccessAssignment")
        ProjectRole = require_model_class("ProjectRole")
        ProjectRoleAssignment = require_model_class("ProjectRoleAssignment")

        session = db_obj.session
        project_field = _first_model_attribute(
            Project,
            ("project_id", "chunk_project_id", "project_public_id"),
        )
        if project_field is None:
            raise RuntimeError("Project model has no queryable public project id.")
        project = (
            session.query(Project)
            .filter(project_field == resolved_project_id)
            .limit(1)
            .one_or_none()
        )

        if project is None:
            message = f"Default project '{resolved_project_id}' does not exist."
            item = _make_message(
                code=PROJECT_ACCESS_CHECK_FAILED_CODE,
                message=message,
                details={
                    "projectId": resolved_project_id,
                    "requireOk": bool(require_ok),
                },
            )
            (errors if require_ok else warnings).append(item)
            empty.update({"checked": True, "error": message})
            return empty, warnings, errors

        project_db_id = _first_row_value(project, ("id",), None)
        project_status = _safe_str(
            _first_row_value(project, ("status",), "active"),
            "active",
        ).lower()
        project_deleted = bool(
            _first_row_value(project, ("deleted_at",), None) is not None
            or project_status == "deleted"
        )
        actual_owner = _safe_str(
            _first_row_value(
                project,
                (
                    "owner_auth_user_id",
                    "auth_owner_user_id",
                    "owner_user_id",
                    "owner_id",
                ),
                "",
            ),
            "",
        )
        owner_type = _safe_str(
            _first_row_value(project, ("owner_type",), "user"),
            "user",
        ).lower()
        owner_identity_canonical = _is_canonical_auth_user_id(actual_owner)
        owner_ready = bool(
            not project_deleted
            and owner_type == "user"
            and owner_identity_canonical
            and actual_owner == resolved_owner
        )

        (
            canonical_rows,
            canonical_owner_row_count,
            canonical_truncated,
            canonical_count,
        ) = _query_canonical_owner_rows(
            session,
            ProjectAccessAssignment,
            project_public_id=resolved_project_id,
            project_db_id=project_db_id,
            limit=PROJECT_ACCESS_QUERY_ROW_LIMIT,
        )
        canonical = _canonical_assignment_status(
            app,
            canonical_rows,
            owner_auth_user_id=actual_owner or resolved_owner,
            truncated=canonical_truncated,
        )
        legacy = _legacy_access_status(
            session,
            ProjectRole,
            ProjectRoleAssignment,
            project_db_id=project_db_id,
            owner_auth_user_id=actual_owner or resolved_owner,
        )

        service_status = _load_project_access_service_status(app)
        service_ready, service_contract = _project_access_service_contract_ready(
            app,
            service_status,
        )
        viewer_read_only = bool(
            service_contract.get("viewerReadOnly")
            and _viewer_read_only_expected(app)
        )
        expected_source = _expected_access_source_service(app)
        actual_source = _safe_str(
            canonical.get("sourceOfTruth"),
            service_contract.get("sourceOfTruth", expected_source),
        ).lower()
        source_ready = bool(
            canonical.get("sourceOfTruthReady")
            and service_contract.get("sourceOfTruthReady")
            and actual_source == expected_source
        )

        projection_status = _safe_str(
            _first_row_value(
                project,
                ("access_sync_status", "access_status"),
                "",
            ),
            "",
        ).lower()
        projection_state_ready = (
            projection_status == "ready" if projection_status else None
        )
        canonical_ready = bool(
            owner_ready
            and canonical.get("ready")
            and service_ready
            and source_ready
            and viewer_read_only
            and projection_state_ready is not False
        )
        legacy_ready = bool(legacy.get("ready"))
        access_ready = bool(canonical_ready and legacy_ready)

        identity = _public_identity_fields(app, actual_owner or resolved_owner)
        result = {
            "ok": access_ready,
            "status": CHECK_STATUS_OK if access_ready else CHECK_STATUS_WARNING,
            "checked": True,
            "required": bool(require_ok),
            "projectId": resolved_project_id,
            "projectDbId": project_db_id,
            "projectExists": True,
            "projectStatus": project_status,
            "projectDeleted": project_deleted,
            **identity,
            "expectedOwnerIdFingerprint": _identity_fingerprint(resolved_owner),
            "projectOwnerReady": owner_ready,
            "ownerReady": owner_ready,
            "canonicalProjectAccessReady": canonical_ready,
            "canonicalReady": canonical_ready,
            "legacyProjectAccessReady": legacy_ready,
            "legacyReady": legacy_ready,
            "rolesReady": legacy.get("rolesReady"),
            "ownerAssignmentReady": bool(
                canonical.get("ready") and legacy.get("ownerAssignmentReady")
            ),
            "accessReady": access_ready,
            "projectionStatus": projection_status or None,
            "projectionStateReady": projection_state_ready,
            "requiredRoleKeys": list(DEFAULT_PROJECT_ROLE_KEYS),
            "activeRoleKeys": list(legacy.get("activeRoleKeys") or []),
            "missingRoleKeys": list(legacy.get("missingRoleKeys") or []),
            "duplicateActiveRoleKeys": list(
                legacy.get("duplicateActiveRoleKeys") or []
            ),
            "ownerRoleId": legacy.get("ownerRoleId"),
            "canonicalOwnerAssignmentCount": canonical_owner_row_count,
            "legacyOwnerAssignmentCount": legacy.get("activeOwnerAssignmentCount", 0),
            "matchingOwnerAssignmentCount": canonical.get("matchingOwnerAssignmentCount", 0),
            "projectAccessAssignmentCount": canonical_count,
            "projectAccessRoleCount": legacy.get("roleCount", 0),
            "assignmentScanLimit": PROJECT_ACCESS_QUERY_ROW_LIMIT,
            "assignmentScanTruncated": bool(
                canonical.get("scanTruncated") or legacy.get("scanTruncated")
            ),
            "sourceOfTruth": actual_source,
            "expectedSourceOfTruth": expected_source,
            "sourceOfTruthReady": source_ready,
            "viewerReadOnly": viewer_read_only,
            "authzEnforced": _project_access_authz_enforced(app),
            "effectivePermissionsCalculated": False,
            "serviceReady": service_ready,
            "serviceContract": service_contract,
            "counts": {
                "canonicalAssignments": canonical_count,
                "canonicalOwnerAssignments": canonical_owner_row_count,
                "legacyRoles": legacy.get("roleCount", 0),
                "legacyOwnerAssignments": legacy.get("activeOwnerAssignmentCount", 0),
            },
            "mutated": False,
            "error": None,
        }

        if not access_ready:
            failure_keys: list[str] = []
            for ready, key in (
                (owner_ready, "projectOwner"),
                (canonical_ready, "canonicalProjectAccess"),
                (legacy_ready, "legacyProjectAccess"),
                (service_ready, "projectAccessService"),
                (source_ready, "sourceOfTruth"),
                (viewer_read_only, "viewerReadOnly"),
            ):
                if not ready:
                    failure_keys.append(key)
            if projection_state_ready is False:
                failure_keys.append("projectionState")
            item = _make_message(
                code=PROJECT_ACCESS_CHECK_FAILED_CODE,
                message=(
                    "Default project owner and canonical/legacy access projections "
                    "are not ready."
                ),
                details={
                    "projectId": resolved_project_id,
                    "ownerIdFingerprint": identity.get("ownerIdFingerprint"),
                    "failureKeys": failure_keys,
                    "canonicalOwnerAssignmentCount": canonical_owner_row_count,
                    "legacyOwnerAssignmentCount": legacy.get("activeOwnerAssignmentCount", 0),
                    "missingRoleKeys": legacy.get("missingRoleKeys", []),
                    "assignmentScanTruncated": result["assignmentScanTruncated"],
                    "requireOk": bool(require_ok),
                    "repairHint": (
                        "Run the explicit default-seed or DB-bootstrap repair; "
                        "runtime checks never mutate access data."
                    ),
                },
            )
            (errors if require_ok else warnings).append(item)

        return result, warnings, errors

    except Exception as exc:
        message = (
            "Read-only project-access check failed: "
            f"{_safe_exception_message(exc)}"
        )
        item = _make_message(
            code=PROJECT_ACCESS_CHECK_FAILED_CODE,
            message=message,
            details={
                "exceptionType": exc.__class__.__name__,
                "projectId": resolved_project_id,
                "ownerIdFingerprint": _identity_fingerprint(resolved_owner),
                "requireOk": bool(require_ok),
            },
        )
        (errors if require_ok else warnings).append(item)
        empty.update(
            {
                "checked": True,
                "error": message,
                "exceptionType": exc.__class__.__name__,
            }
        )
        return empty, warnings, errors
    finally:
        _cleanup_read_only_session(db_obj)


def clear_runtime_check_caches() -> dict[str, Any]:
    """Clear only immutable spec/import caches; never cache or clear DB rows."""

    def cache_info(function: Any) -> dict[str, Any]:
        try:
            info = function.cache_info()
            serializer = getattr(info, "_asdict", None)
            return serializer() if callable(serializer) else {}
        except Exception:
            return {}

    def cache_clear(function: Any) -> bool:
        try:
            clear = getattr(function, "cache_clear", None)
            if callable(clear):
                clear()
                return True
        except Exception:
            pass
        return False

    before = {
        "pathSpecs": cache_info(get_default_path_check_specs),
        "fileSpecs": cache_info(get_default_file_check_specs),
        "routeSpecs": cache_info(get_default_route_check_specs),
        "schemaApi": cache_info(_load_schema_readiness_api),
    }
    cleared = {
        "pathSpecs": cache_clear(get_default_path_check_specs),
        "fileSpecs": cache_clear(get_default_file_check_specs),
        "routeSpecs": cache_clear(get_default_route_check_specs),
        "schemaApi": cache_clear(_load_schema_readiness_api),
    }
    return {
        "cleared": any(cleared.values()),
        "clearedCaches": cleared,
        "ormRowsCached": False,
        "databaseStateCached": False,
        "before": before,
    }


# -----------------------------------------------------------------------------
# Aggregate runner
# -----------------------------------------------------------------------------

def run_runtime_checks(
    app: Flask,
    settings: BootstrapSettings | None = None,
    *,
    check_paths: bool | None = None,
    check_files: bool | None = None,
    check_routes: bool | None = None,
    check_models: bool | None = None,
    check_database: bool | None = None,
    check_schema: bool | None = None,
    check_project_access: bool | None = None,
    require_schema: bool | None = None,
    require_project_access: bool | None = None,
    strict: bool | None = None,
) -> RuntimeChecksResult:
    """
    Run all read-only runtime checks.

    This is safe to run during normal app startup.
    """
    started = _utc_now()
    started_iso = started.isoformat()

    if not _is_flask_app(app):
        completed = _utc_now()
        error = _make_message(
            code="invalid_flask_app",
            message="run_runtime_checks(app) expects a Flask app or compatible object.",
            details={},
        )
        return RuntimeChecksResult(
            ok=False,
            status=CHECK_STATUS_FAILED,
            started_at=started_iso,
            completed_at=completed.isoformat(),
            duration_ms=_duration_ms(started, completed),
            result_version=RUNTIME_CHECKS_RESULT_VERSION,
            metadata={},
            paths=[],
            files=[],
            routes=[],
            route_summary={
                "count": 0,
                "requiredMissing": [],
                "optionalMissing": [],
                "rules": [],
            },
            models={
                "ok": False,
                "status": CHECK_STATUS_FAILED,
            },
            database={
                "available": False,
                "configured": False,
                "connectionChecked": False,
                "connectionOk": None,
            },
            schema={
                "ok": False,
                "status": CHECK_STATUS_SKIPPED,
                "checked": False,
            },
            project_access={
                "ok": False,
                "status": CHECK_STATUS_SKIPPED,
                "checked": False,
                "authzEnforced": False,
            },
            warnings=[],
            errors=[error],
        )

    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    route_summary: dict[str, Any] = {
        "count": 0,
        "requiredMissing": [],
        "optionalMissing": [],
        "rules": [],
    }
    models: dict[str, Any] = {
        "ok": None,
        "status": CHECK_STATUS_SKIPPED,
        "summary": {},
    }
    database: dict[str, Any] = {
        "available": None,
        "configured": None,
        "connectionChecked": False,
        "connectionOk": None,
        "status": CHECK_STATUS_SKIPPED,
    }
    schema: dict[str, Any] = {
        "ok": None,
        "status": CHECK_STATUS_SKIPPED,
        "checked": False,
        "mutated": False,
    }
    project_access: dict[str, Any] = {
        "ok": None,
        "status": CHECK_STATUS_SKIPPED,
        "checked": False,
        "authzEnforced": False,
        "mutated": False,
    }

    try:
        if settings is None:
            settings = build_bootstrap_settings(app)
    except Exception as exc:
        settings = None
        warnings.append(
            _make_message(
                code="settings_unavailable",
                message=f"Could not build bootstrap settings: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    if strict is None:
        if settings is not None:
            try:
                strict = bool(settings.runtime.startup_strict)
            except Exception:
                strict = False
        else:
            strict = get_bool_setting(
                app,
                "VECTOPLAN_CHUNK_STARTUP_STRICT",
                False,
                aliases=("CHUNK_STARTUP_STRICT",),
            )

    if check_paths is None:
        check_paths = bool(settings.runtime.check_paths) if settings is not None else True
    if check_files is None:
        check_files = bool(settings.runtime.check_files) if settings is not None else True
    if check_routes is None:
        check_routes = bool(settings.runtime.check_routes) if settings is not None else True
    if check_models is None:
        check_models = bool(settings.runtime.check_models) if settings is not None else True
    if check_database is None:
        check_database = bool(settings.runtime.check_database) if settings is not None else True

    require_database = None
    if settings is not None:
        try:
            require_database = bool(settings.runtime.require_database)
        except Exception:
            require_database = None
    if require_database is None:
        try:
            require_database = should_require_database_on_startup(app)
        except Exception:
            require_database = True

    if check_schema is None:
        check_schema = _runtime_setting_bool(
            app,
            settings,
            attribute_name="check_schema",
            config_key="VECTOPLAN_CHUNK_RUNTIME_CHECK_SCHEMA",
            default=bool(check_database),
            aliases=("CHUNK_RUNTIME_CHECK_SCHEMA",),
        )
    if require_schema is None:
        require_schema = _runtime_setting_bool(
            app,
            settings,
            attribute_name="require_schema",
            config_key="VECTOPLAN_CHUNK_RUNTIME_REQUIRE_SCHEMA",
            default=bool(require_database),
            aliases=("CHUNK_RUNTIME_REQUIRE_SCHEMA",),
        )
    if check_project_access is None:
        check_project_access = _runtime_setting_bool(
            app,
            settings,
            attribute_name="check_project_access",
            config_key="VECTOPLAN_CHUNK_RUNTIME_CHECK_PROJECT_ACCESS",
            default=bool(check_database),
            aliases=("CHUNK_RUNTIME_CHECK_PROJECT_ACCESS",),
        )
    if require_project_access is None:
        require_project_access = _runtime_setting_bool(
            app,
            settings,
            attribute_name="require_project_access",
            config_key="VECTOPLAN_CHUNK_RUNTIME_REQUIRE_PROJECT_ACCESS",
            default=bool(require_database),
            aliases=("CHUNK_RUNTIME_REQUIRE_PROJECT_ACCESS",),
        )

    if require_project_access and not check_project_access:
        check_project_access = True
        warnings.append(
            _make_message(
                code="project_access_check_forced",
                message=(
                    "Project-access readiness is required, so the read-only "
                    "access check was enabled."
                ),
            )
        )

    metadata = collect_app_metadata(app, settings=settings)
    metadata["runtimeCheckPolicy"] = {
        "strict": bool(strict),
        "checkPaths": bool(check_paths),
        "checkFiles": bool(check_files),
        "checkRoutes": bool(check_routes),
        "checkModels": bool(check_models),
        "checkDatabase": bool(check_database),
        "checkSchema": bool(check_schema),
        "requireSchema": bool(require_schema),
        "checkProjectAccess": bool(check_project_access),
        "requireProjectAccess": bool(require_project_access),
        "canonicalUserIdField": DEFAULT_CANONICAL_USER_ID_FIELD,
        "projectAccessSourceOfTruth": _expected_access_source_service(app),
        "viewerReadOnly": _viewer_read_only_expected(app),
        "projectAccessAuthzEnforced": _project_access_authz_enforced(app),
    }

    try:
        if check_paths:
            path_results, path_warnings, path_errors = run_path_checks(
                app,
                strict=bool(strict),
            )
            paths = path_results
            warnings.extend(path_warnings)
            errors.extend(path_errors)
        else:
            paths.append(
                _make_check_item(
                    name="path_checks",
                    status=CHECK_STATUS_SKIPPED,
                    ok=True,
                    required=False,
                    description="Path checks skipped by settings.",
                )
            )
    except Exception as exc:
        errors.append(
            _make_message(
                code="path_checks_failed",
                message=f"Path checks failed: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_files:
            file_results, file_warnings, file_errors = run_file_checks(
                app,
                strict=bool(strict),
            )
            files = file_results
            warnings.extend(file_warnings)
            errors.extend(file_errors)
        else:
            files.append(
                _make_check_item(
                    name="file_checks",
                    status=CHECK_STATUS_SKIPPED,
                    ok=True,
                    required=False,
                    description="File checks skipped by settings.",
                )
            )
    except Exception as exc:
        errors.append(
            _make_message(
                code="file_checks_failed",
                message=f"File checks failed: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_routes:
            route_results, summary, route_warnings, route_errors = run_route_checks(
                app,
                strict=bool(strict),
            )
            routes = route_results
            route_summary = summary
            warnings.extend(route_warnings)
            errors.extend(route_errors)
        else:
            routes.append(
                _make_check_item(
                    name="route_checks",
                    status=CHECK_STATUS_SKIPPED,
                    ok=True,
                    required=False,
                    description="Route checks skipped by settings.",
                )
            )
    except Exception as exc:
        errors.append(
            _make_message(
                code="route_checks_failed",
                message=f"Route checks failed: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_models:
            model_result, model_warnings, model_errors = run_model_registry_check(
                app,
                strict=bool(strict),
            )
            models = model_result
            warnings.extend(model_warnings)
            errors.extend(model_errors)
        else:
            models = {
                "ok": True,
                "status": CHECK_STATUS_SKIPPED,
                "summary": {},
                "message": "Model checks skipped by settings.",
            }
    except Exception as exc:
        errors.append(
            _make_message(
                code="model_checks_failed",
                message=f"Model checks failed: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_database:
            database_result, database_warnings, database_errors = run_database_check(
                app,
                check_connection=True,
                require_ok=bool(require_database),
            )
            database = database_result
            database["status"] = (
                CHECK_STATUS_OK
                if database.get("connectionOk") is True
                else CHECK_STATUS_FAILED
                if database.get("connectionOk") is False
                else CHECK_STATUS_WARNING
            )
            warnings.extend(database_warnings)
            errors.extend(database_errors)
        else:
            database = {
                "available": None,
                "configured": None,
                "connectionChecked": False,
                "connectionOk": None,
                "status": CHECK_STATUS_SKIPPED,
                "message": "Database check skipped by settings.",
            }
    except Exception as exc:
        errors.append(
            _make_message(
                code="database_check_exception",
                message=f"Database check failed unexpectedly: {_safe_exception_message(exc)}",
                details={
                    "exceptionType": exc.__class__.__name__,
                },
            )
        )

    try:
        if check_schema:
            schema_result, schema_warnings, schema_errors = (
                run_schema_readiness_check(
                    app,
                    require_ok=bool(require_schema),
                    db_extension=db,
                    schema_name=_safe_str(
                        _config_value(
                            app,
                            "VECTOPLAN_CHUNK_DATABASE_SCHEMA",
                            "",
                        ),
                        "",
                    )
                    or None,
                )
            )
            schema = schema_result
            warnings.extend(schema_warnings)
            errors.extend(schema_errors)
        else:
            schema = {
                "ok": True,
                "status": CHECK_STATUS_SKIPPED,
                "checked": False,
                "required": bool(require_schema),
                "message": "Schema readiness check skipped by settings.",
                "mutated": False,
            }
    except Exception as exc:
        item = _make_message(
            code="schema_check_exception",
            message=(
                "Schema readiness check failed unexpectedly: "
                f"{_safe_exception_message(exc)}"
            ),
            details={"exceptionType": exc.__class__.__name__},
        )
        (errors if require_schema else warnings).append(item)
        schema = {
            "ok": False,
            "status": CHECK_STATUS_FAILED,
            "checked": True,
            "required": bool(require_schema),
            "error": item["message"],
            "mutated": False,
        }

    try:
        if check_project_access:
            access_schema_ready = schema.get("projectAccessSchemaReady")
            if check_schema and access_schema_ready is False:
                message = (
                    "Project-access row readiness was not queried because its "
                    "required tables or columns are missing."
                )
                item = _make_message(
                    code=PROJECT_ACCESS_CHECK_UNAVAILABLE_CODE,
                    message=message,
                    details={
                        "requireOk": bool(require_project_access),
                        "schema": schema,
                    },
                )
                (errors if require_project_access else warnings).append(item)
                project_access = _empty_project_access_runtime_status(
                    app=app,
                    project_id=_resolve_default_project_id(app, settings),
                    owner_auth_user_id=(
                        _resolve_default_project_owner_auth_user_id(app, settings)
                    ),
                    required=bool(require_project_access),
                    error=message,
                    checked=False,
                )
                project_access["status"] = CHECK_STATUS_SKIPPED
                project_access["schemaReady"] = False
            else:
                access_result, access_warnings, access_errors = (
                    run_project_access_readiness_check(
                        app,
                        settings=settings,
                        require_ok=bool(require_project_access),
                        db_extension=db,
                    )
                )
                project_access = access_result
                project_access["schemaReady"] = access_schema_ready
                warnings.extend(access_warnings)
                errors.extend(access_errors)
        else:
            project_access = {
                "ok": True,
                "status": CHECK_STATUS_SKIPPED,
                "checked": False,
                "required": bool(require_project_access),
                "message": "Project-access readiness check skipped by settings.",
                "authzEnforced": _project_access_authz_enforced(app),
                "mutated": False,
            }
    except Exception as exc:
        item = _make_message(
            code="project_access_check_exception",
            message=(
                "Project-access readiness check failed unexpectedly: "
                f"{_safe_exception_message(exc)}"
            ),
            details={"exceptionType": exc.__class__.__name__},
        )
        (errors if require_project_access else warnings).append(item)
        project_access = {
            "ok": False,
            "status": CHECK_STATUS_FAILED,
            "checked": True,
            "required": bool(require_project_access),
            "error": item["message"],
            "authzEnforced": _project_access_authz_enforced(app),
            "mutated": False,
        }

    completed = _utc_now()
    ok = len(errors) == 0
    status = CHECK_STATUS_OK if ok else CHECK_STATUS_FAILED

    if warnings and ok:
        status = CHECK_STATUS_WARNING

    return RuntimeChecksResult(
        ok=ok,
        status=status,
        started_at=started_iso,
        completed_at=completed.isoformat(),
        duration_ms=_duration_ms(started, completed),
        result_version=RUNTIME_CHECKS_RESULT_VERSION,
        metadata=metadata,
        paths=paths,
        files=files,
        routes=routes,
        route_summary=route_summary,
        models=models,
        database=database,
        schema=schema,
        project_access=project_access,
        warnings=warnings,
        errors=errors,
    )


# -----------------------------------------------------------------------------
# Serialization and summaries
# -----------------------------------------------------------------------------

def runtime_checks_result_to_dict(result: RuntimeChecksResult | Mapping[str, Any] | Any) -> dict[str, Any]:
    """Serialize runtime check result to dict."""
    if isinstance(result, RuntimeChecksResult):
        return asdict(result)

    if isinstance(result, Mapping):
        try:
            return dict(result)
        except Exception:
            return {}

    return {}


def build_runtime_checks_summary(result: RuntimeChecksResult | Mapping[str, Any] | Any) -> dict[str, Any]:
    """Build compact runtime-check summary without raw user identities."""
    data = runtime_checks_result_to_dict(result)

    paths = data.get("paths") or []
    files = data.get("files") or []
    routes = data.get("routes") or []
    warnings = data.get("warnings") or []
    errors = data.get("errors") or []
    schema = _safe_dict(data.get("schema"))
    access = _safe_dict(data.get("project_access"))
    models = _safe_dict(data.get("models"))

    def count_failed(items: Any) -> int:
        try:
            return sum(1 for item in items if not bool(item.get("ok")))
        except Exception:
            return 0

    return {
        "ok": bool(data.get("ok")),
        "status": _safe_str(data.get("status"), "unknown"),
        "startedAt": data.get("started_at"),
        "completedAt": data.get("completed_at"),
        "durationMs": _safe_int(data.get("duration_ms"), 0, minimum=0),
        "pathCount": len(paths),
        "fileCount": len(files),
        "routeCount": _safe_int(
            _safe_dict(data.get("route_summary")).get("count", len(routes)),
            len(routes),
            minimum=0,
        ),
        "failedPathChecks": count_failed(paths),
        "failedFileChecks": count_failed(files),
        "failedRouteChecks": count_failed(routes),
        "resultVersion": data.get("result_version"),
        "modelsOk": bool(models.get("ok")),
        "canonicalProjectAccessShapeReady": models.get(
            "canonicalProjectAccessShapeReady"
        ),
        "legacyProjectAccessShapeReady": models.get(
            "legacyProjectAccessShapeReady"
        ),
        "databaseChecked": bool(_safe_dict(data.get("database")).get("connectionChecked")),
        "databaseOk": _safe_dict(data.get("database")).get("connectionOk"),
        "schemaChecked": bool(schema.get("checked")),
        "schemaOk": schema.get("ok"),
        "projectOwnerColumnsReady": schema.get("projectOwnerColumnsReady"),
        "projectAccessSchemaReady": schema.get("projectAccessSchemaReady"),
        "canonicalProjectAccessSchemaReady": schema.get(
            "canonicalProjectAccessSchemaReady"
        ),
        "legacyProjectAccessSchemaReady": schema.get(
            "legacyProjectAccessSchemaReady"
        ),
        "projectAccessChecked": bool(access.get("checked")),
        "projectAccessReady": access.get("accessReady", access.get("ok")),
        "defaultProjectOwnerReady": access.get(
            "projectOwnerReady",
            access.get("ownerReady"),
        ),
        "canonicalProjectAccessReady": access.get(
            "canonicalProjectAccessReady",
            access.get("canonicalReady"),
        ),
        "legacyProjectAccessReady": access.get(
            "legacyProjectAccessReady",
            access.get("legacyReady"),
        ),
        "defaultProjectRolesReady": access.get("rolesReady"),
        "defaultProjectOwnerAssignmentReady": access.get(
            "ownerAssignmentReady"
        ),
        "ownerIdFingerprint": access.get("ownerIdFingerprint"),
        "projectAccessSourceOfTruth": access.get("sourceOfTruth"),
        "projectAccessSourceOfTruthReady": access.get("sourceOfTruthReady"),
        "viewerReadOnly": access.get("viewerReadOnly"),
        "projectAccessAuthzEnforced": access.get("authzEnforced"),
        "warningCount": len(warnings),
        "errorCount": len(errors),
        "requiredMissingRoutes": list(
            _safe_dict(data.get("route_summary")).get("requiredMissing", []) or []
        ),
        "optionalMissingRoutes": list(
            _safe_dict(data.get("route_summary")).get("optionalMissing", []) or []
        ),
    }


def raise_if_runtime_checks_failed(result: RuntimeChecksResult) -> None:
    """Raise RuntimeError if runtime checks contain errors."""
    if result.ok:
        return

    summary = build_runtime_checks_summary(result)
    errors = result.errors or []

    if errors:
        first_error = errors[0]
        message = _safe_str(first_error.get("message"), "Runtime checks failed.")
    else:
        message = "Runtime checks failed."

    raise RuntimeError(
        f"{message} "
        f"(errorCount={summary.get('errorCount')}, "
        f"warningCount={summary.get('warningCount')})"
    )


def log_runtime_checks_result(app: Flask, result: RuntimeChecksResult) -> None:
    """Write a compact runtime-check result to app logs."""
    summary = build_runtime_checks_summary(result)

    if result.ok:
        if summary.get("warningCount"):
            _safe_log_warning(
                app,
                "Runtime checks completed with warnings: %s",
                summary,
            )
        else:
            _safe_log_info(
                app,
                "Runtime checks completed successfully: %s",
                summary,
            )
    else:
        _safe_log_warning(
            app,
            "Runtime checks failed: %s",
            summary,
        )


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "CHECK_STATUS_FAILED",
    "CHECK_STATUS_INVALID_TYPE",
    "CHECK_STATUS_MISSING",
    "CHECK_STATUS_OK",
    "CHECK_STATUS_SKIPPED",
    "CHECK_STATUS_WARNING",
    "RUNTIME_CHECKS_RESULT_VERSION",
    "DEFAULT_PROJECT_ID",
    "DEFAULT_PROJECT_OWNER_AUTH_USER_ID",
    "DEFAULT_PROJECT_OWNER_USER_ID",
    "DEFAULT_CANONICAL_USER_ID_FIELD",
    "DEFAULT_PROJECT_ACCESS_SOURCE_SERVICE",
    "DEFAULT_PROJECT_ROLE_KEYS",
    "CheckMessage",
    "FileCheckSpec",
    "PathCheckSpec",
    "RouteCheckSpec",
    "RuntimeCheckItem",
    "RuntimeChecksResult",
    "build_runtime_checks_summary",
    "collect_app_metadata",
    "collect_route_rules",
    "clear_runtime_check_caches",
    "get_default_file_check_spec_data",
    "get_default_file_check_specs",
    "get_default_path_check_spec_data",
    "get_default_path_check_specs",
    "get_default_route_check_spec_data",
    "get_default_route_check_specs",
    "log_runtime_checks_result",
    "raise_if_runtime_checks_failed",
    "resolve_configured_path",
    "resolve_service_root",
    "resolve_service_root_from_file",
    "run_database_check",
    "run_file_checks",
    "run_model_registry_check",
    "_load_project_access_service_status",
    "run_path_checks",
    "run_route_checks",
    "run_schema_readiness_check",
    "run_project_access_readiness_check",
    "run_runtime_checks",
    "runtime_checks_result_to_dict",
]