# services/vectoplan-chunk/extensions.py
"""
Internal extension initialization for the `vectoplan-chunk` service.

This module owns the shared Flask extensions and the internal extension registry
for the chunk service.

Primary responsibilities:
- expose `db` for SQLAlchemy models
- expose `migrate` for Flask-Migrate/Alembic integration when available
- initialize extensions idempotently
- store extension state under `app.extensions["vectoplan_chunk"]`
- keep a legacy alias under `app.extensions["vectoplan_editor"]` during the
  transition from the copied editor service shell
- provide robust status/debug helpers

Important:
- Models import `db` from this module.
- `init_extensions(app)` must be called by the Flask app factory before routes
  rely on DB-backed repositories.
- This module does not create tables and does not run migrations.
- Bootstrap/seed logic belongs in later startup/bootstrap files.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Final, Optional

from flask import Flask


# -----------------------------------------------------------------------------
# Optional external extension imports
# -----------------------------------------------------------------------------

try:
    from flask_sqlalchemy import SQLAlchemy
except Exception as exc:  # pragma: no cover - dependency failure should be visible in status
    SQLAlchemy = None  # type: ignore[assignment]
    _SQLALCHEMY_IMPORT_ERROR: Optional[BaseException] = exc
else:
    _SQLALCHEMY_IMPORT_ERROR = None


try:
    from flask_migrate import Migrate
except Exception as exc:  # pragma: no cover - migration extension can be added later
    Migrate = None  # type: ignore[assignment]
    _MIGRATE_IMPORT_ERROR: Optional[BaseException] = exc
else:
    _MIGRATE_IMPORT_ERROR = None


# `db` must exist at module import time because models import it.
if SQLAlchemy is not None:
    db = SQLAlchemy(
        session_options={
            "expire_on_commit": False,
        }
    )
else:  # pragma: no cover
    db = None  # type: ignore[assignment]


if Migrate is not None:
    migrate = Migrate()
else:  # pragma: no cover
    migrate = None  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

CHUNK_EXTENSION_NAMESPACE: Final[str] = "vectoplan_chunk"
LEGACY_EDITOR_EXTENSION_NAMESPACE: Final[str] = "vectoplan_editor"

EXTENSION_REGISTRY_KEY: Final[str] = "extensions"
EXTENSION_REGISTRY_VERSION: Final[int] = 2

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_SERVICE_DISPLAY_NAME: Final[str] = "VECTOPLAN Chunk Service"


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExtensionSpec:
    """
    Describes an internal or external service extension/subsystem.
    """

    name: str
    category: str
    description: str
    required: bool = False


# -----------------------------------------------------------------------------
# Safe utility helpers
# -----------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """Return UTC timestamp as ISO string."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


def _is_flask_app(app: object) -> bool:
    """Check defensively whether an object can be treated like a Flask app."""
    if isinstance(app, Flask):
        return True

    required_attributes = ("extensions", "config", "logger")
    try:
        return all(hasattr(app, attribute_name) for attribute_name in required_attributes)
    except Exception:
        return False


def _safe_log_debug(app: Flask, message: str) -> None:
    try:
        app.logger.debug(message)
    except Exception:
        pass


def _safe_log_info(app: Flask, message: str) -> None:
    try:
        app.logger.info(message)
    except Exception:
        pass


def _safe_log_warning(app: Flask, message: str) -> None:
    try:
        app.logger.warning(message)
    except Exception:
        pass


def _safe_log_exception(app: Flask, message: str) -> None:
    try:
        app.logger.exception(message)
    except Exception:
        pass


def _safe_getattr(obj: Any, attribute_name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, attribute_name, default)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default

    if minimum is not None:
        result = max(minimum, result)

    return result


def _normalize_name(name: Any) -> str:
    if name is None:
        return ""

    if isinstance(name, str):
        return name.strip()

    try:
        return str(name).strip()
    except Exception:
        return ""


def _deepcopy_safe(value: Any) -> Any:
    try:
        return deepcopy(value)
    except Exception:
        return value


def _exception_to_string(exc: BaseException | None) -> str | None:
    if exc is None:
        return None

    try:
        return f"{type(exc).__name__}: {exc}"
    except Exception:
        return "Unknown extension import error"


def _get_config_bool(app: Flask, key: str, default: bool = False) -> bool:
    try:
        value = app.config.get(key, default)
    except Exception:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    try:
        text = str(value).strip().lower()
    except Exception:
        return default

    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False

    return default


# -----------------------------------------------------------------------------
# Default extension specs
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_default_extension_specs() -> tuple[ExtensionSpec, ...]:
    """
    Return the default extension/subsystem specs for the chunk service.
    """
    return (
        ExtensionSpec(
            name="registry",
            category="internal",
            description="Internal extension registry under app.extensions['vectoplan_chunk'].",
            required=True,
        ),
        ExtensionSpec(
            name="database",
            category="storage",
            description="Flask-SQLAlchemy database extension used by all SQLAlchemy models.",
            required=True,
        ),
        ExtensionSpec(
            name="migrations",
            category="storage",
            description="Flask-Migrate/Alembic integration for PostgreSQL schema migrations.",
            required=False,
        ),
        ExtensionSpec(
            name="models",
            category="storage",
            description="SQLAlchemy model registration package.",
            required=True,
        ),
        ExtensionSpec(
            name="templates",
            category="delivery",
            description="Optional template delivery inherited from the copied Flask shell.",
            required=False,
        ),
        ExtensionSpec(
            name="static_assets",
            category="delivery",
            description="Optional static asset delivery inherited from the copied Flask shell.",
            required=False,
        ),
        ExtensionSpec(
            name="world_provider",
            category="world",
            description="Provider/template world layer, including the flat provider.",
            required=True,
        ),
        ExtensionSpec(
            name="world_state",
            category="world",
            description="Project/universe/world-instance runtime state layer.",
            required=True,
        ),
        ExtensionSpec(
            name="repositories",
            category="storage",
            description="Repository layer for DB-backed project, world, chunk and event access.",
            required=False,
        ),
        ExtensionSpec(
            name="commands",
            category="runtime",
            description="Command execution layer for SetBlock, RemoveBlock and later object commands.",
            required=False,
        ),
        ExtensionSpec(
            name="future_integrations",
            category="integration",
            description="Reserved state for later service clients and integrations.",
            required=False,
        ),
    )


# -----------------------------------------------------------------------------
# Namespace and registry helpers
# -----------------------------------------------------------------------------

def _ensure_extensions_container(app: Flask) -> dict[str, Any]:
    """Ensure `app.extensions` is usable."""
    if not _is_flask_app(app):
        raise TypeError("extensions.py expects a Flask app or a compatible object.")

    try:
        container = app.extensions
    except Exception as exc:
        raise RuntimeError("The Flask app has no usable `extensions` container.") from exc

    if not isinstance(container, dict):
        raise RuntimeError("`app.extensions` is not a dictionary and cannot be used.")

    return container


def _get_service_name(app: Flask) -> str:
    try:
        return (
            app.config.get("SERVICE_NAME")
            or app.config.get("APP_NAME")
            or app.config.get("VECTOPLAN_SERVICE_NAME")
            or DEFAULT_SERVICE_NAME
        )
    except Exception:
        return DEFAULT_SERVICE_NAME


def _get_service_display_name(app: Flask) -> str:
    try:
        return (
            app.config.get("APP_DISPLAY_NAME")
            or app.config.get("SERVICE_DISPLAY_NAME")
            or DEFAULT_SERVICE_DISPLAY_NAME
        )
    except Exception:
        return DEFAULT_SERVICE_DISPLAY_NAME


def _ensure_namespace(app: Flask, namespace_name: str) -> dict[str, Any]:
    """
    Ensure a namespace under `app.extensions`.

    The primary namespace is `vectoplan_chunk`. A legacy alias
    `vectoplan_editor` is kept for compatibility during the migration.
    """
    extensions_container = _ensure_extensions_container(app)

    try:
        namespace = extensions_container.setdefault(namespace_name, {})
    except Exception as exc:
        raise RuntimeError(
            f"The extension namespace '{namespace_name}' could not be initialized."
        ) from exc

    if not isinstance(namespace, dict):
        raise RuntimeError(f"`app.extensions['{namespace_name}']` is not a dictionary.")

    namespace.setdefault("namespace", namespace_name)
    namespace.setdefault("primary_namespace", CHUNK_EXTENSION_NAMESPACE)
    namespace.setdefault("legacy_namespaces", [LEGACY_EDITOR_EXTENSION_NAMESPACE])
    namespace.setdefault("extension_registry_version", EXTENSION_REGISTRY_VERSION)
    namespace.setdefault("extensions_initialized", False)
    namespace.setdefault("extensions_initialized_at", None)
    namespace.setdefault("extensions_init_count", 0)
    namespace.setdefault("service_name", _get_service_name(app))
    namespace.setdefault("service_display_name", _get_service_display_name(app))
    namespace.setdefault("extension_errors", [])
    namespace.setdefault("extension_warnings", [])
    namespace.setdefault("dependencies", {})
    namespace.setdefault(EXTENSION_REGISTRY_KEY, {})

    if not isinstance(namespace[EXTENSION_REGISTRY_KEY], dict):
        namespace[EXTENSION_REGISTRY_KEY] = {}

    if not isinstance(namespace["extension_errors"], list):
        namespace["extension_errors"] = []

    if not isinstance(namespace["extension_warnings"], list):
        namespace["extension_warnings"] = []

    if not isinstance(namespace["dependencies"], dict):
        namespace["dependencies"] = {}

    return namespace


def _ensure_chunk_namespace(app: Flask) -> dict[str, Any]:
    """Ensure the primary chunk namespace exists."""
    return _ensure_namespace(app, CHUNK_EXTENSION_NAMESPACE)


def _ensure_legacy_namespace_alias(app: Flask) -> dict[str, Any]:
    """
    Ensure the legacy editor namespace exists and points to the same object.

    This prevents old copied startup/status code from failing while the service
    is being renamed to vectoplan-chunk.
    """
    extensions_container = _ensure_extensions_container(app)
    primary_namespace = _ensure_chunk_namespace(app)

    legacy_namespace = extensions_container.get(LEGACY_EDITOR_EXTENSION_NAMESPACE)

    if legacy_namespace is primary_namespace:
        return primary_namespace

    if isinstance(legacy_namespace, dict) and legacy_namespace:
        # Preserve legacy diagnostics before replacing the alias.
        warnings = legacy_namespace.get("extension_warnings")
        errors = legacy_namespace.get("extension_errors")

        if isinstance(warnings, list):
            primary_namespace.setdefault("legacy_extension_warnings", []).extend(
                _deepcopy_safe(warnings)
            )

        if isinstance(errors, list):
            primary_namespace.setdefault("legacy_extension_errors", []).extend(
                _deepcopy_safe(errors)
            )

    extensions_container[LEGACY_EDITOR_EXTENSION_NAMESPACE] = primary_namespace
    return primary_namespace


def _ensure_extension_registry(app: Flask) -> dict[str, dict[str, Any]]:
    """Ensure and return the extension registry."""
    namespace = _ensure_chunk_namespace(app)
    registry = namespace.get(EXTENSION_REGISTRY_KEY)

    if not isinstance(registry, dict):
        registry = {}
        namespace[EXTENSION_REGISTRY_KEY] = registry

    return registry


def _new_extension_state(spec: ExtensionSpec) -> dict[str, Any]:
    """Create initial state for one extension/subsystem."""
    timestamp = _utc_now_iso()

    return {
        "name": spec.name,
        "category": spec.category,
        "description": spec.description,
        "required": bool(spec.required),
        "registered": True,
        "initialized": False,
        "available": None,
        "status": "registered",
        "created_at": timestamp,
        "last_initialized_at": None,
        "last_updated_at": timestamp,
        "init_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "metadata": {},
        "last_error": None,
        "last_warning": None,
    }


def _register_spec_if_missing(app: Flask, spec: ExtensionSpec) -> dict[str, Any]:
    """Register an ExtensionSpec idempotently."""
    registry = _ensure_extension_registry(app)
    key = _normalize_name(spec.name)

    if not key:
        raise ValueError("An ExtensionSpec without a valid name cannot be registered.")

    entry = registry.get(key)

    if isinstance(entry, dict):
        entry.setdefault("name", spec.name)
        entry.setdefault("category", spec.category)
        entry.setdefault("description", spec.description)
        entry.setdefault("required", bool(spec.required))
        entry.setdefault("registered", True)
        entry.setdefault("initialized", False)
        entry.setdefault("available", None)
        entry.setdefault("status", "registered")
        entry.setdefault("created_at", _utc_now_iso())
        entry.setdefault("last_initialized_at", None)
        entry.setdefault("last_updated_at", _utc_now_iso())
        entry.setdefault("init_count", 0)
        entry.setdefault("error_count", 0)
        entry.setdefault("warning_count", 0)
        entry.setdefault("metadata", {})
        entry.setdefault("last_error", None)
        entry.setdefault("last_warning", None)

        if not isinstance(entry.get("metadata"), dict):
            entry["metadata"] = {}

        return entry

    entry = _new_extension_state(spec)
    registry[key] = entry
    return entry


def _append_warning(app: Flask, message: str) -> None:
    """Append warning to the primary namespace."""
    namespace = _ensure_chunk_namespace(app)

    try:
        namespace["extension_warnings"].append(
            {
                "message": message,
                "timestamp": _utc_now_iso(),
            }
        )
    except Exception:
        pass

    _safe_log_warning(app, message)


def _append_error(app: Flask, message: str) -> None:
    """Append error to the primary namespace."""
    namespace = _ensure_chunk_namespace(app)

    try:
        namespace["extension_errors"].append(
            {
                "message": message,
                "timestamp": _utc_now_iso(),
            }
        )
    except Exception:
        pass

    _safe_log_warning(app, message)


# -----------------------------------------------------------------------------
# Extension state updates
# -----------------------------------------------------------------------------

def register_extension(
    app: Flask,
    name: str,
    *,
    category: str = "custom",
    description: str = "",
    required: bool = False,
) -> dict[str, Any]:
    """Register an additional extension/subsystem."""
    spec = ExtensionSpec(
        name=_normalize_name(name),
        category=category.strip() if isinstance(category, str) else "custom",
        description=description.strip() if isinstance(description, str) else "",
        required=bool(required),
    )

    if not spec.name:
        raise ValueError("`register_extension()` requires a valid extension name.")

    entry = _register_spec_if_missing(app, spec)
    entry["last_updated_at"] = _utc_now_iso()
    return entry


def mark_extension_initialized(
    app: Flask,
    name: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark an extension/subsystem as initialized."""
    normalized_name = _normalize_name(name)
    if not normalized_name:
        raise ValueError("`mark_extension_initialized()` requires a valid extension name.")

    entry = register_extension(app, normalized_name)

    entry["initialized"] = True
    entry["available"] = True
    entry["status"] = "initialized"
    entry["init_count"] = _safe_int(entry.get("init_count"), default=0, minimum=0) + 1
    entry["last_initialized_at"] = _utc_now_iso()
    entry["last_updated_at"] = entry["last_initialized_at"]
    entry["last_error"] = None

    if isinstance(metadata, dict) and metadata:
        current_metadata = entry.get("metadata")
        if not isinstance(current_metadata, dict):
            current_metadata = {}
            entry["metadata"] = current_metadata

        try:
            current_metadata.update(metadata)
        except Exception:
            entry["metadata"] = _deepcopy_safe(metadata)

    return entry


def mark_extension_unavailable(
    app: Flask,
    name: str,
    message: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark an extension/subsystem as unavailable."""
    normalized_name = _normalize_name(name)
    if not normalized_name:
        raise ValueError("`mark_extension_unavailable()` requires a valid extension name.")

    entry = register_extension(app, normalized_name)
    timestamp = _utc_now_iso()

    entry["initialized"] = False
    entry["available"] = False
    entry["status"] = "unavailable"
    entry["last_error"] = {
        "message": message,
        "timestamp": timestamp,
    }
    entry["last_updated_at"] = timestamp

    if isinstance(metadata, dict) and metadata:
        current_metadata = entry.get("metadata")
        if not isinstance(current_metadata, dict):
            current_metadata = {}
            entry["metadata"] = current_metadata

        try:
            current_metadata.update(metadata)
        except Exception:
            entry["metadata"] = _deepcopy_safe(metadata)

    _append_error(app, f"Extension unavailable [{normalized_name}]: {message}")
    return entry


def mark_extension_warning(
    app: Flask,
    name: str,
    warning_message: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark an extension/subsystem with warning status."""
    normalized_name = _normalize_name(name)
    if not normalized_name:
        raise ValueError("`mark_extension_warning()` requires a valid extension name.")

    entry = register_extension(app, normalized_name)
    timestamp = _utc_now_iso()

    entry["status"] = "warning"
    entry["warning_count"] = _safe_int(entry.get("warning_count"), default=0, minimum=0) + 1
    entry["last_warning"] = {
        "message": warning_message,
        "timestamp": timestamp,
    }
    entry["last_updated_at"] = timestamp

    if isinstance(metadata, dict) and metadata:
        current_metadata = entry.get("metadata")
        if not isinstance(current_metadata, dict):
            current_metadata = {}
            entry["metadata"] = current_metadata

        try:
            current_metadata.update(metadata)
        except Exception:
            entry["metadata"] = _deepcopy_safe(metadata)

    _append_warning(app, f"Extension warning [{normalized_name}]: {warning_message}")
    return entry


def mark_extension_failed(
    app: Flask,
    name: str,
    error_message: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark an extension/subsystem as failed."""
    normalized_name = _normalize_name(name)
    if not normalized_name:
        raise ValueError("`mark_extension_failed()` requires a valid extension name.")

    entry = register_extension(app, normalized_name)
    timestamp = _utc_now_iso()

    entry["initialized"] = False
    entry["available"] = False
    entry["status"] = "failed"
    entry["error_count"] = _safe_int(entry.get("error_count"), default=0, minimum=0) + 1
    entry["last_error"] = {
        "message": error_message,
        "timestamp": timestamp,
    }
    entry["last_updated_at"] = timestamp

    if isinstance(metadata, dict) and metadata:
        current_metadata = entry.get("metadata")
        if not isinstance(current_metadata, dict):
            current_metadata = {}
            entry["metadata"] = current_metadata

        try:
            current_metadata.update(metadata)
        except Exception:
            entry["metadata"] = _deepcopy_safe(metadata)

    _append_error(app, f"Extension error [{normalized_name}]: {error_message}")
    return entry


# -----------------------------------------------------------------------------
# Dependency / extension initialization
# -----------------------------------------------------------------------------

def _seed_default_specs(app: Flask) -> None:
    """Register default extension specs."""
    for spec in get_default_extension_specs():
        _register_spec_if_missing(app, spec)


def _record_dependency_status(app: Flask) -> None:
    """Store import/dependency status in the extension namespace."""
    namespace = _ensure_chunk_namespace(app)
    dependencies = namespace.setdefault("dependencies", {})

    dependencies["flask_sqlalchemy"] = {
        "available": SQLAlchemy is not None,
        "error": _exception_to_string(_SQLALCHEMY_IMPORT_ERROR),
    }
    dependencies["flask_migrate"] = {
        "available": Migrate is not None,
        "error": _exception_to_string(_MIGRATE_IMPORT_ERROR),
    }


def _initialize_database_extension(app: Flask) -> None:
    """Initialize Flask-SQLAlchemy extension."""
    if db is None:
        mark_extension_unavailable(
            app,
            "database",
            "Flask-SQLAlchemy is not installed or could not be imported.",
            metadata={
                "import_error": _exception_to_string(_SQLALCHEMY_IMPORT_ERROR),
            },
        )
        raise RuntimeError(
            "Flask-SQLAlchemy is required by vectoplan-chunk models. "
            "Install Flask-SQLAlchemy and configure SQLALCHEMY_DATABASE_URI."
        ) from _SQLALCHEMY_IMPORT_ERROR

    database_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    database_url = app.config.get("DATABASE_URL")

    if not database_uri and database_url:
        app.config["SQLALCHEMY_DATABASE_URI"] = database_url
        database_uri = database_url

    if not database_uri:
        mark_extension_failed(
            app,
            "database",
            "Missing SQLALCHEMY_DATABASE_URI / DATABASE_URL configuration.",
        )
        raise RuntimeError(
            "Missing database configuration. Set SQLALCHEMY_DATABASE_URI or DATABASE_URL."
        )

    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)

    try:
        db.init_app(app)
    except Exception as exc:
        mark_extension_failed(
            app,
            "database",
            f"db.init_app(app) failed: {type(exc).__name__}: {exc}",
        )
        raise

    safe_uri = _mask_database_uri(str(database_uri))

    mark_extension_initialized(
        app,
        "database",
        metadata={
            "sqlalchemy_available": True,
            "database_uri_configured": True,
            "database_uri_masked": safe_uri,
            "track_modifications": bool(app.config.get("SQLALCHEMY_TRACK_MODIFICATIONS")),
        },
    )


def _initialize_migration_extension(app: Flask) -> None:
    """Initialize Flask-Migrate if available."""
    if migrate is None:
        mark_extension_warning(
            app,
            "migrations",
            "Flask-Migrate is not installed or could not be imported.",
            metadata={
                "import_error": _exception_to_string(_MIGRATE_IMPORT_ERROR),
                "required": False,
            },
        )
        return

    if db is None:
        mark_extension_warning(
            app,
            "migrations",
            "Cannot initialize migrations because db is unavailable.",
        )
        return

    try:
        migrate.init_app(app, db)
    except Exception as exc:
        if _get_config_bool(app, "VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS", default=False):
            mark_extension_failed(
                app,
                "migrations",
                f"migrate.init_app(app, db) failed: {type(exc).__name__}: {exc}",
            )
            raise

        mark_extension_warning(
            app,
            "migrations",
            f"migrate.init_app(app, db) failed: {type(exc).__name__}: {exc}",
        )
        return

    mark_extension_initialized(
        app,
        "migrations",
        metadata={
            "flask_migrate_available": True,
            "required": _get_config_bool(app, "VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS", default=False),
        },
    )


def _import_models_for_registration(app: Flask) -> None:
    """
    Import models package so SQLAlchemy metadata is populated.

    This does not create tables and does not run migrations.
    """
    try:
        from models import get_model_debug_summary, require_models_ready

        require_models_ready()
        model_summary = get_model_debug_summary()

        mark_extension_initialized(
            app,
            "models",
            metadata=model_summary,
        )
    except Exception as exc:
        mark_extension_failed(
            app,
            "models",
            f"Model registration failed: {type(exc).__name__}: {exc}",
        )
        raise


def _initialize_builtin_states(app: Flask) -> None:
    """Initialize all built-in extension states."""
    mark_extension_initialized(
        app,
        "registry",
        metadata={
            "namespace": CHUNK_EXTENSION_NAMESPACE,
            "legacy_namespace": LEGACY_EDITOR_EXTENSION_NAMESPACE,
            "registry_version": EXTENSION_REGISTRY_VERSION,
        },
    )

    _record_dependency_status(app)
    _initialize_database_extension(app)
    _initialize_migration_extension(app)
    _import_models_for_registration(app)

    template_folder = _safe_getattr(app, "template_folder", None)
    if template_folder:
        mark_extension_initialized(
            app,
            "templates",
            metadata={
                "template_folder": template_folder,
            },
        )
    else:
        mark_extension_warning(
            app,
            "templates",
            "No template folder detected on Flask app.",
        )

    static_folder = _safe_getattr(app, "static_folder", None)
    static_url_path = _safe_getattr(app, "static_url_path", None)
    if static_folder:
        mark_extension_initialized(
            app,
            "static_assets",
            metadata={
                "static_folder": static_folder,
                "static_url_path": static_url_path,
            },
        )
    else:
        mark_extension_warning(
            app,
            "static_assets",
            "No static folder detected on Flask app.",
        )

    register_extension(
        app,
        "world_provider",
        category="world",
        description="Provider/template world layer, including the flat provider.",
        required=True,
    )

    register_extension(
        app,
        "world_state",
        category="world",
        description="Project/universe/world-instance runtime state layer.",
        required=True,
    )

    register_extension(
        app,
        "repositories",
        category="storage",
        description="Repository layer for DB-backed project, world, chunk and event access.",
        required=False,
    )

    register_extension(
        app,
        "commands",
        category="runtime",
        description="Command execution layer for SetBlock, RemoveBlock and later object commands.",
        required=False,
    )

    register_extension(
        app,
        "future_integrations",
        category="integration",
        description="Reserved state for later service clients and integrations.",
        required=False,
    )


def _mask_database_uri(uri: str) -> str:
    """Mask password part of a database URI for status output."""
    if not uri:
        return ""

    try:
        if "://" not in uri or "@" not in uri:
            return uri

        scheme, rest = uri.split("://", 1)
        credentials, host_part = rest.split("@", 1)

        if ":" not in credentials:
            return f"{scheme}://{credentials}@{host_part}"

        username, _password = credentials.split(":", 1)
        return f"{scheme}://{username}:***@{host_part}"
    except Exception:
        return "<masked>"


def init_extensions(app: Flask) -> Flask:
    """
    Initialize internal extension structure and external Flask extensions.

    This function is idempotent for registry setup, but Flask extension
    initialization should still normally be called once per app factory run.
    """
    namespace = _ensure_chunk_namespace(app)
    _ensure_legacy_namespace_alias(app)
    _seed_default_specs(app)

    try:
        _initialize_builtin_states(app)
    except Exception as exc:
        _safe_log_exception(app, "Failed to initialize vectoplan-chunk extensions.")
        _append_error(app, f"vectoplan-chunk extension initialization failed: {exc!r}")
        raise

    namespace["extensions_initialized"] = True
    namespace["extensions_initialized_at"] = _utc_now_iso()
    namespace["extensions_init_count"] = _safe_int(
        namespace.get("extensions_init_count"),
        default=0,
        minimum=0,
    ) + 1
    namespace["service_name"] = _get_service_name(app)
    namespace["service_display_name"] = _get_service_display_name(app)

    _safe_log_info(
        app,
        "Internal extension structure for `vectoplan-chunk` was initialized.",
    )
    return app


# -----------------------------------------------------------------------------
# Database status helpers
# -----------------------------------------------------------------------------

def get_database_status(app: Flask, *, check_connection: bool = False) -> dict[str, Any]:
    """
    Return database extension status.

    If `check_connection=True`, a lightweight `SELECT 1` is attempted inside the
    app context. Keep this false for cheap status calls.
    """
    status: dict[str, Any] = {
        "available": db is not None,
        "sqlalchemyImportError": _exception_to_string(_SQLALCHEMY_IMPORT_ERROR),
        "configured": False,
        "uriMasked": None,
        "connectionChecked": False,
        "connectionOk": None,
        "connectionError": None,
    }

    try:
        database_uri = app.config.get("SQLALCHEMY_DATABASE_URI") or app.config.get("DATABASE_URL")
        status["configured"] = bool(database_uri)
        status["uriMasked"] = _mask_database_uri(str(database_uri)) if database_uri else None
    except Exception as exc:
        status["connectionError"] = f"config read failed: {type(exc).__name__}: {exc}"

    if not check_connection or db is None:
        return status

    status["connectionChecked"] = True

    try:
        from sqlalchemy import text

        with app.app_context():
            db.session.execute(text("SELECT 1"))
        status["connectionOk"] = True
    except Exception as exc:
        status["connectionOk"] = False
        status["connectionError"] = f"{type(exc).__name__}: {exc}"

    return status


def get_migration_status(app: Flask) -> dict[str, Any]:
    """Return migration extension status."""
    return {
        "available": migrate is not None,
        "flaskMigrateImportError": _exception_to_string(_MIGRATE_IMPORT_ERROR),
        "required": _get_config_bool(app, "VECTOPLAN_CHUNK_REQUIRE_MIGRATIONS", default=False),
    }


# -----------------------------------------------------------------------------
# Read/debug helpers
# -----------------------------------------------------------------------------

def get_extension_registry(app: Flask) -> dict[str, dict[str, Any]]:
    """Return complete extension registry as safe copy."""
    registry = _ensure_extension_registry(app)
    return _deepcopy_safe(registry)


def get_extension_state(app: Flask, name: str) -> dict[str, Any] | None:
    """Return state of one extension as safe copy."""
    normalized_name = _normalize_name(name)
    if not normalized_name:
        return None

    registry = _ensure_extension_registry(app)
    entry = registry.get(normalized_name)

    if not isinstance(entry, dict):
        return None

    return _deepcopy_safe(entry)


def list_extension_states(app: Flask) -> list[dict[str, Any]]:
    """Return all extension states sorted as safe copies."""
    registry = _ensure_extension_registry(app)
    result: list[dict[str, Any]] = []

    for name in sorted(registry.keys()):
        entry = registry.get(name)
        if isinstance(entry, dict):
            result.append(_deepcopy_safe(entry))

    return result


def get_extension_summary(app: Flask) -> dict[str, Any]:
    """Return compact extension status summary."""
    namespace = _ensure_chunk_namespace(app)
    registry = _ensure_extension_registry(app)

    total_count = 0
    initialized_count = 0
    warning_count = 0
    failed_count = 0
    unavailable_count = 0
    required_failed_count = 0

    for entry in registry.values():
        if not isinstance(entry, dict):
            continue

        total_count += 1

        if bool(entry.get("initialized")):
            initialized_count += 1

        status = entry.get("status")
        if status == "warning":
            warning_count += 1
        elif status == "failed":
            failed_count += 1
            if bool(entry.get("required")):
                required_failed_count += 1
        elif status == "unavailable":
            unavailable_count += 1
            if bool(entry.get("required")):
                required_failed_count += 1

    return {
        "namespace": namespace.get("namespace", CHUNK_EXTENSION_NAMESPACE),
        "legacyNamespace": LEGACY_EDITOR_EXTENSION_NAMESPACE,
        "registryVersion": namespace.get(
            "extension_registry_version",
            EXTENSION_REGISTRY_VERSION,
        ),
        "extensionsInitialized": bool(namespace.get("extensions_initialized")),
        "extensionsInitializedAt": namespace.get("extensions_initialized_at"),
        "extensionsInitCount": _safe_int(namespace.get("extensions_init_count"), default=0, minimum=0),
        "totalExtensions": total_count,
        "initializedExtensions": initialized_count,
        "warningExtensions": warning_count,
        "failedExtensions": failed_count,
        "unavailableExtensions": unavailable_count,
        "requiredFailedExtensions": required_failed_count,
        "warningLogCount": len(namespace.get("extension_warnings", []) or []),
        "errorLogCount": len(namespace.get("extension_errors", []) or []),
        "database": get_database_status(app, check_connection=False),
        "migrations": get_migration_status(app),
    }


def get_full_extension_status(
    app: Flask,
    *,
    check_database_connection: bool = False,
    include_registry: bool = True,
) -> dict[str, Any]:
    """Return detailed extension status."""
    result = {
        "summary": get_extension_summary(app),
        "database": get_database_status(app, check_connection=check_database_connection),
        "migrations": get_migration_status(app),
        "defaultSpecs": get_default_extension_spec_data(),
    }

    if include_registry:
        result["registry"] = get_extension_registry(app)

    return result


def get_default_extension_spec_data() -> list[dict[str, Any]]:
    """Return default specs as serializable dictionaries."""
    return [asdict(spec) for spec in get_default_extension_specs()]


__all__ = [
    "db",
    "migrate",
    "CHUNK_EXTENSION_NAMESPACE",
    "LEGACY_EDITOR_EXTENSION_NAMESPACE",
    "EXTENSION_REGISTRY_KEY",
    "EXTENSION_REGISTRY_VERSION",
    "ExtensionSpec",
    "get_default_extension_specs",
    "get_default_extension_spec_data",
    "register_extension",
    "mark_extension_initialized",
    "mark_extension_unavailable",
    "mark_extension_warning",
    "mark_extension_failed",
    "init_extensions",
    "get_database_status",
    "get_migration_status",
    "get_extension_registry",
    "get_extension_state",
    "list_extension_states",
    "get_extension_summary",
    "get_full_extension_status",
]