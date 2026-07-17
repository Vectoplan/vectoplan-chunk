# services/vectoplan-chunk/app.py
"""
Flask app factory for the `vectoplan-chunk` service.

Responsibilities:
- load .env defensively
- resolve and apply config class
- create Flask app with stable template/static paths
- initialize shared extensions such as SQLAlchemy and migrations
- import/register SQLAlchemy models
- optionally check database connectivity
- register the central route registry
- verify/register the productive project-access blueprint
- run optional read-only startup hooks
- store service, routing and project-access metadata under app.extensions

Important boundaries:
- no chunk generation here
- no command execution here
- no repository logic here
- no direct snapshot/event writes here
- no direct project/world/access bootstrap writes here
- no direct database migrations here
- no authentication or authorization enforcement here
- no runtime schema creation or default seeding here

Core runtime semantics:

    Project
      -> Universe
          -> WorldInstance(world_spawn)
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Provider/template semantics:

    world_spawn = concrete editable project world
    flat        = provider/template world
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from dotenv import load_dotenv
from flask import Blueprint, Flask


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

_TRUE_VALUES = {"1", "true", "t", "yes", "y", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "f", "no", "n", "off", "disabled"}

_DEFAULT_SERVICE_NAME = "vectoplan-chunk"
_DEFAULT_APP_NAME = "vectoplan-chunk"
_DEFAULT_APP_DISPLAY_NAME = "VECTOPLAN Chunk Service"
_DEFAULT_EXTENSION_NAMESPACE = "vectoplan_chunk"

_LEGACY_EXTENSION_NAMESPACE = "vectoplan_editor"

_DEFAULT_STARTUP_MODULE_CANDIDATES = (
    "src.bootstrap.startup",
    "bootstrap.startup",
)

_ROUTE_MODULE_NAME = "routes"

APP_FACTORY_VERSION = "app-factory.v2"

_PROJECT_ACCESS_BLUEPRINT_NAME = "project_access"
_PROJECT_ACCESS_BLUEPRINT_ATTRIBUTE = "project_access_bp"
_PROJECT_ACCESS_ROUTE_MODULE_CANDIDATES = (
    "routes.project_access",
)
_PROJECT_ACCESS_REQUIRED_RULES = (
    "/project-access/_status",
    "/projects/<project_id>/access",
    "/projects/<project_id>/access/initialize",
    "/projects/<project_id>/roles",
    "/projects/<project_id>/groups",
    "/projects/<project_id>/assignments",
)


# -----------------------------------------------------------------------------
# Defensive helpers
# -----------------------------------------------------------------------------

def _normalize_text(value: Any, default: str | None = None) -> str | None:
    """
    Normalize text-like values.

    Behavior:
    - None -> default
    - str -> stripped
    - other -> str(value).strip()
    - empty -> default
    """
    if value is None:
        return default

    if isinstance(value, str):
        normalized = value.strip()
        return normalized or default

    try:
        normalized = str(value).strip()
        return normalized or default
    except Exception:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    """Read boolean environment variable defensively."""
    try:
        raw_value = os.getenv(name)
    except Exception:
        return default

    normalized = _normalize_text(raw_value)
    if normalized is None:
        return default

    lowered = normalized.lower()

    if lowered in _TRUE_VALUES:
        return True

    if lowered in _FALSE_VALUES:
        return False

    return default


def _env_text(name: str, default: str | None = None) -> str | None:
    """Read string environment variable defensively."""
    try:
        return _normalize_text(os.getenv(name), default)
    except Exception:
        return default


def _safe_log_debug(app: Flask, message: str, *args: Any) -> None:
    """Log debug defensively."""
    try:
        app.logger.debug(message, *args)
    except Exception:
        pass


def _safe_log_info(app: Flask, message: str, *args: Any) -> None:
    """Log info defensively."""
    try:
        app.logger.info(message, *args)
    except Exception:
        pass


def _safe_log_warning(app: Flask, message: str, *args: Any) -> None:
    """Log warning defensively."""
    try:
        app.logger.warning(message, *args)
    except Exception:
        pass


def _safe_log_error(app: Flask, message: str, *args: Any) -> None:
    """Log error defensively."""
    try:
        app.logger.error(message, *args)
    except Exception:
        pass


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_bool_config(app: Flask, key: str, default: bool = False) -> bool:
    """Read boolean config value defensively."""
    try:
        value = app.config.get(key, default)
    except Exception:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    try:
        normalized = str(value).strip().lower()
    except Exception:
        return default

    if normalized in _TRUE_VALUES:
        return True

    if normalized in _FALSE_VALUES:
        return False

    return default


# -----------------------------------------------------------------------------
# Path and import helpers
# -----------------------------------------------------------------------------

def _resolve_service_root() -> Path:
    """
    Resolve service root directory.

    Normal case:
    app.py is located directly in service root.

    Fallback:
    current working directory.
    """
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path(".").resolve()


SERVICE_ROOT = _resolve_service_root()
SRC_ROOT = SERVICE_ROOT / "src"


@lru_cache(maxsize=1)
def _ensure_service_root_on_sys_path() -> bool:
    """
    Ensure service root is available on sys.path.

    This keeps imports stable for:
    - config
    - routes
    - models
    - extensions
    - src.bootstrap.startup
    """
    try:
        service_root_str = str(SERVICE_ROOT)
    except Exception:
        return False

    if not service_root_str:
        return False

    try:
        if service_root_str not in sys.path:
            sys.path.insert(0, service_root_str)
        return True
    except Exception:
        return False


def _safe_path_from_config(value: Any, fallback_name: str) -> str:
    """Convert configured path into string with service-root fallback."""
    try:
        if isinstance(value, Path):
            return str(value)

        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass

    try:
        return str(SERVICE_ROOT / fallback_name)
    except Exception:
        return fallback_name


@lru_cache(maxsize=1)
def _load_environment_file() -> bool:
    """
    Load .env defensively once per process.

    Search order:
    1. .env in service root
    2. .env in current working directory
    3. generic load_dotenv fallback
    """
    _ensure_service_root_on_sys_path()

    candidate_paths: list[Path] = []

    try:
        candidate_paths.append(SERVICE_ROOT / ".env")
    except Exception:
        pass

    try:
        candidate_paths.append(Path.cwd() / ".env")
    except Exception:
        pass

    for candidate in candidate_paths:
        try:
            if candidate.is_file():
                load_dotenv(dotenv_path=candidate, override=False)
                return True
        except Exception:
            continue

    try:
        load_dotenv(override=False)
        return True
    except Exception:
        return False


@lru_cache(maxsize=64)
def _import_module(module_name: str) -> ModuleType:
    """
    Import module with process-local cache.

    Successful imports are cached. Exceptions are not cached by lru_cache.
    """
    return importlib.import_module(module_name)


@lru_cache(maxsize=16)
def _candidate_missing_names(module_name: str) -> tuple[str, ...]:
    """
    Return valid ModuleNotFoundError.name values for a module path.

    Example:
    src.bootstrap.startup -> ("src", "src.bootstrap", "src.bootstrap.startup")
    """
    parts = module_name.split(".")
    return tuple(".".join(parts[:index]) for index in range(1, len(parts) + 1))


def _is_missing_candidate_module(exc: ModuleNotFoundError, module_name: str) -> bool:
    """
    Check whether ModuleNotFoundError means the candidate module itself is
    missing, not one of its inner dependencies.
    """
    missing_name = _normalize_text(getattr(exc, "name", None))
    if missing_name is None:
        return False

    return missing_name in _candidate_missing_names(module_name)


@lru_cache(maxsize=1)
def _get_startup_module_candidates() -> tuple[str, ...]:
    """
    Return startup module candidates in priority order.

    Priority:
    1. VECTOPLAN_CHUNK_STARTUP_MODULE
    2. VECTOPLAN_EDITOR_STARTUP_MODULE
    3. src.bootstrap.startup
    4. bootstrap.startup
    """
    candidates: list[str] = []

    for env_name in (
        "VECTOPLAN_CHUNK_STARTUP_MODULE",
        "VECTOPLAN_EDITOR_STARTUP_MODULE",
    ):
        env_candidate = _env_text(env_name)
        if env_candidate and env_candidate not in candidates:
            candidates.append(env_candidate)

    for default_candidate in _DEFAULT_STARTUP_MODULE_CANDIDATES:
        if default_candidate not in candidates:
            candidates.append(default_candidate)

    return tuple(candidates)


# -----------------------------------------------------------------------------
# Config resolution and validation
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_config_module() -> ModuleType:
    """Load local config module after .env load."""
    _ensure_service_root_on_sys_path()
    _load_environment_file()
    return _import_module("config")


def _get_base_config_class() -> type:
    """Return BaseConfig from local config module."""
    try:
        config_module = _load_config_module()
        base_config = getattr(config_module, "BaseConfig", None)
        if isinstance(base_config, type):
            return base_config
    except Exception:
        pass

    return object


def _get_default_config_class() -> type:
    """Return Config from local config module."""
    try:
        config_module = _load_config_module()
        config_class = getattr(config_module, "Config", None)
        if isinstance(config_class, type):
            return config_class
    except Exception:
        pass

    return object


def _resolve_config_class(config_object: type | str | None) -> type:
    """
    Resolve config class.

    Supported:
    - None -> get_config_class()
    - str -> get_config_class(name)
    - class -> direct usage

    Fallback:
    - Config
    """
    config_module = _load_config_module()

    fallback_config = getattr(config_module, "Config", None)
    if not isinstance(fallback_config, type):
        fallback_config = object

    get_config_class = getattr(config_module, "get_config_class", None)

    if config_object is None:
        if callable(get_config_class):
            try:
                return get_config_class()
            except Exception:
                return fallback_config
        return fallback_config

    if isinstance(config_object, str):
        if callable(get_config_class):
            try:
                return get_config_class(config_object)
            except Exception:
                return fallback_config
        return fallback_config

    if isinstance(config_object, type):
        return config_object

    return fallback_config


def _validate_config(config_class: type, logger: logging.Logger) -> None:
    """
    Run optional config validation.

    Behavior:
    - calls config_class.validate() when available
    - logs validation errors by default
    - fails fast only when VECTOPLAN_CHUNK_FAIL_FAST_CONFIG=true
    """
    validator = getattr(config_class, "validate", None)
    if not callable(validator):
        return

    try:
        errors = validator()
    except Exception as exc:
        errors = [f"Configuration validation crashed: {exc!r}"]

    if not errors:
        return

    message = " | ".join(str(error) for error in errors if error)

    fail_fast = (
        _env_flag("VECTOPLAN_CHUNK_FAIL_FAST_CONFIG", default=False)
        or _env_flag("VECTOPLAN_EDITOR_FAIL_FAST_CONFIG", default=False)
    )

    if fail_fast:
        raise RuntimeError(f"Invalid vectoplan-chunk configuration: {message}")

    try:
        logger.warning("vectoplan-chunk configuration warning: %s", message)
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Flask creation and metadata
# -----------------------------------------------------------------------------

def _create_flask_app(config_class: type) -> Flask:
    """Create Flask app with robust template/static folder resolution."""
    template_folder = _safe_path_from_config(
        getattr(config_class, "TEMPLATES_ROOT", None),
        "templates",
    )
    static_folder = _safe_path_from_config(
        getattr(config_class, "STATIC_ROOT", None),
        "static",
    )

    try:
        app = Flask(
            __name__,
            template_folder=template_folder,
            static_folder=static_folder,
            static_url_path="/static",
        )
    except Exception as exc:
        raise RuntimeError(
            "Could not create Flask app. "
            f"template_folder={template_folder!r}, static_folder={static_folder!r}"
        ) from exc

    return app


def _get_extension_namespace_from_config(config_class: type) -> str:
    """Resolve preferred extension namespace."""
    for attribute_name in (
        "VECTOPLAN_EXTENSION_NAMESPACE",
        "SERVICE_EXTENSION_NAMESPACE",
        "ROUTES_EXTENSION_NAMESPACE",
    ):
        value = _normalize_text(getattr(config_class, attribute_name, None))
        if value:
            return value

    service_name = _normalize_text(
        getattr(config_class, "SERVICE_NAME", None),
        _DEFAULT_SERVICE_NAME,
    )

    if service_name:
        return (
            service_name
            .replace("-", "_")
            .replace(".", "_")
            .replace(" ", "_")
            .lower()
        )

    return _DEFAULT_EXTENSION_NAMESPACE


def _ensure_app_metadata_registry(app: Flask) -> dict[str, Any]:
    """
    Ensure primary metadata registry in app.extensions.

    Also keeps `vectoplan_editor` as legacy alias during transition.
    """
    namespace = _normalize_text(
        app.config.get("VECTOPLAN_EXTENSION_NAMESPACE"),
        _DEFAULT_EXTENSION_NAMESPACE,
    )

    try:
        app.extensions.setdefault(namespace, {})
        metadata = app.extensions[namespace]

        if not isinstance(metadata, dict):
            metadata = {}
            app.extensions[namespace] = metadata

        app.extensions.setdefault(_LEGACY_EXTENSION_NAMESPACE, metadata)
        app.extensions.setdefault("vectoplan_chunk", metadata)

        metadata.setdefault("namespace", namespace)
        metadata.setdefault("legacy_namespace", _LEGACY_EXTENSION_NAMESPACE)

        return metadata

    except Exception:
        app.extensions.setdefault("vectoplan_chunk", {})
        metadata = app.extensions["vectoplan_chunk"]
        app.extensions.setdefault(_LEGACY_EXTENSION_NAMESPACE, metadata)
        return metadata


def _apply_config(app: Flask, config_class: type) -> None:
    """Apply config object to Flask app and store service metadata."""
    try:
        app.config.from_object(config_class)
    except Exception as exc:
        raise RuntimeError(
            f"Could not load config class {config_class.__name__}."
        ) from exc

    app.config.setdefault(
        "VECTOPLAN_EXTENSION_NAMESPACE",
        _get_extension_namespace_from_config(config_class),
    )
    app.config.setdefault("SERVICE_NAME", _DEFAULT_SERVICE_NAME)
    app.config.setdefault("APP_NAME", _DEFAULT_APP_NAME)
    app.config.setdefault("APP_DISPLAY_NAME", _DEFAULT_APP_DISPLAY_NAME)
    app.config.setdefault("VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES", True)
    app.config.setdefault("VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_ROUTES", True)

    metadata = _ensure_app_metadata_registry(app)

    metadata["service_name"] = app.config.get("SERVICE_NAME", _DEFAULT_SERVICE_NAME)
    metadata["app_name"] = app.config.get("APP_NAME", _DEFAULT_APP_NAME)
    metadata["service_display_name"] = app.config.get(
        "APP_DISPLAY_NAME",
        _DEFAULT_APP_DISPLAY_NAME,
    )
    metadata["config_class_name"] = config_class.__name__
    metadata["service_root"] = str(SERVICE_ROOT)
    metadata["src_root"] = str(SRC_ROOT)
    metadata["service_root_on_sys_path"] = _ensure_service_root_on_sys_path()
    metadata["dotenv_loaded"] = _load_environment_file()
    metadata["extensions_initialized"] = False
    metadata["startup_completed"] = False
    metadata["startup_attempted"] = False
    metadata["startup_module_name"] = None
    metadata["startup_hook_name"] = None
    metadata["startup_skipped"] = False
    metadata["blueprints_registered"] = False
    metadata["app_factory_version"] = APP_FACTORY_VERSION
    metadata["project_scoped_api_enabled"] = True
    metadata["world_state_api_enabled"] = True
    metadata["project_access_api_enabled"] = _safe_bool_config(
        app,
        "VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES",
        True,
    )
    metadata["project_access_routes_required"] = _safe_bool_config(
        app,
        "VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_ROUTES",
        True,
    )
    metadata["project_access_authz_enforced"] = False
    metadata["project_access_blueprint_registered"] = False
    metadata["project_access_blueprint_registration"] = None
    metadata["legacy_editor_compatibility_enabled"] = True
    metadata["database_startup_check"] = None
    metadata["routing_snapshot"] = None
    metadata["startup_routing_snapshot"] = None

    try:
        build_database_config = getattr(config_class, "build_database_config", None)
        if callable(build_database_config):
            metadata["database_config"] = build_database_config()
    except Exception as exc:
        metadata["database_config_error"] = _safe_exception_message(exc)

    try:
        build_world_state_defaults = getattr(config_class, "build_world_state_defaults", None)
        if callable(build_world_state_defaults):
            metadata["world_state_defaults"] = build_world_state_defaults()
    except Exception as exc:
        metadata["world_state_defaults_error"] = _safe_exception_message(exc)

    try:
        build_service_status_context = getattr(config_class, "build_service_status_context", None)
        if callable(build_service_status_context):
            metadata["service_status_context"] = build_service_status_context()
    except Exception as exc:
        metadata["service_status_context_error"] = _safe_exception_message(exc)


def _configure_app_defaults(app: Flask) -> None:
    """Set small Flask defaults."""
    try:
        app.json.sort_keys = False
    except Exception:
        pass

    try:
        app.url_map.strict_slashes = False
    except Exception:
        pass


def _configure_logger(app: Flask) -> None:
    """Ensure logger has a useful level."""
    try:
        if app.debug:
            app.logger.setLevel(logging.DEBUG)
        else:
            app.logger.setLevel(logging.INFO)
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Extension initialization
# -----------------------------------------------------------------------------

def _initialize_extensions(app: Flask) -> None:
    """
    Initialize extensions from extensions.py.

    This initializes:
    - db
    - migrate if available
    - model registration
    - internal extension registry
    """
    _ensure_service_root_on_sys_path()

    try:
        extensions_module = _import_module("extensions")
    except Exception as exc:
        raise RuntimeError("Could not import local `extensions` module.") from exc

    init_extensions = getattr(extensions_module, "init_extensions", None)
    if not callable(init_extensions):
        raise RuntimeError("Module `extensions` has no callable init_extensions(app).")

    try:
        init_extensions(app)
    except Exception as exc:
        raise RuntimeError("Extension initialization failed.") from exc

    try:
        metadata = _ensure_app_metadata_registry(app)
        metadata["extensions_initialized"] = True

        get_extension_summary = getattr(extensions_module, "get_extension_summary", None)
        if callable(get_extension_summary):
            metadata["extension_summary"] = get_extension_summary(app)
    except Exception:
        pass


def _run_database_startup_check(app: Flask) -> None:
    """
    Optionally check database connectivity.

    Controlled by:
    - VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP
    - VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP
    """
    should_check = _safe_bool_config(
        app,
        "VECTOPLAN_CHUNK_DB_CHECK_ON_STARTUP",
        default=False,
    )

    require_ok = _safe_bool_config(
        app,
        "VECTOPLAN_CHUNK_DB_REQUIRE_ON_STARTUP",
        default=True,
    )

    if not should_check:
        return

    try:
        extensions_module = _import_module("extensions")
        get_database_status = getattr(extensions_module, "get_database_status", None)
    except Exception as exc:
        if require_ok:
            raise RuntimeError("Could not import database status helper.") from exc

        _safe_log_warning(
            app,
            "Database startup check skipped because extensions module could not be imported: %s",
            _safe_exception_message(exc),
        )
        return

    if not callable(get_database_status):
        if require_ok:
            raise RuntimeError("extensions.get_database_status(app) is unavailable.")

        _safe_log_warning(app, "Database startup check skipped: helper unavailable.")
        return

    try:
        status = get_database_status(app, check_connection=True)
    except Exception as exc:
        if require_ok:
            raise RuntimeError("Database startup check failed.") from exc

        status = {
            "connectionChecked": True,
            "connectionOk": False,
            "connectionError": _safe_exception_message(exc),
        }

    try:
        metadata = _ensure_app_metadata_registry(app)
        metadata["database_startup_check"] = status
    except Exception:
        pass

    connection_ok = bool(status.get("connectionOk"))

    if not connection_ok:
        message = status.get("connectionError") or "database connection check failed"

        if require_ok:
            raise RuntimeError(f"Database startup check failed: {message}")

        _safe_log_warning(app, "Database startup check failed but is not required: %s", message)


# -----------------------------------------------------------------------------
# Blueprint registration
# -----------------------------------------------------------------------------

def _collect_registered_blueprint_names(app: Flask) -> list[str]:
    """Return actual Flask blueprint names, independent of registry metadata."""
    try:
        blueprints = getattr(app, "blueprints", {})
        return sorted(str(name) for name in blueprints.keys())
    except Exception:
        return []


def _collect_route_rules(app: Flask) -> list[str]:
    """Return the current URL rules without invoking route handlers."""
    try:
        return sorted({str(rule.rule) for rule in app.url_map.iter_rules()})
    except Exception:
        return []


def _build_app_routing_snapshot(app: Flask) -> dict[str, Any]:
    """Build compact, read-only routing diagnostics from the Flask app itself."""
    blueprint_names = _collect_registered_blueprint_names(app)
    rules = _collect_route_rules(app)
    rule_set = set(rules)
    missing_project_access_rules = [
        rule for rule in _PROJECT_ACCESS_REQUIRED_RULES if rule not in rule_set
    ]

    return {
        "blueprintCount": len(blueprint_names),
        "blueprints": blueprint_names,
        "routeCount": len(rules),
        "projectAccess": {
            "blueprintName": _PROJECT_ACCESS_BLUEPRINT_NAME,
            "registered": _PROJECT_ACCESS_BLUEPRINT_NAME in blueprint_names,
            "requiredRules": list(_PROJECT_ACCESS_REQUIRED_RULES),
            "missingRequiredRules": missing_project_access_rules,
            "ready": bool(
                _PROJECT_ACCESS_BLUEPRINT_NAME in blueprint_names
                and not missing_project_access_rules
            ),
            "authzEnforced": False,
        },
    }


def _project_access_routes_enabled(app: Flask) -> bool:
    """Return whether the prepared project-access HTTP surface is enabled."""
    return _safe_bool_config(
        app,
        "VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES",
        True,
    )


def _project_access_routes_required(app: Flask) -> bool:
    """Return whether missing project-access routes must block app creation."""
    return _safe_bool_config(
        app,
        "VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_ROUTES",
        True,
    )


def _is_blueprint_like(value: Any) -> bool:
    """Return whether a value can safely be passed to register_blueprint()."""
    if isinstance(value, Blueprint):
        return True

    try:
        return bool(
            _normalize_text(getattr(value, "name", None))
            and callable(getattr(value, "register", None))
        )
    except Exception:
        return False


def _load_project_access_blueprint(
    routes_module: ModuleType | None = None,
) -> tuple[Any, str]:
    """
    Resolve ``project_access_bp`` from the central registry or route module.

    This fallback keeps one-file rollout safe while ``routes/__init__.py`` may
    still be on an older revision. Once the central registry contains the
    blueprint, this function is not used during normal startup.
    """
    candidates: list[tuple[ModuleType, str]] = []

    if routes_module is not None:
        candidates.append((routes_module, _ROUTE_MODULE_NAME))

    import_errors: list[str] = []

    for module_name in _PROJECT_ACCESS_ROUTE_MODULE_CANDIDATES:
        try:
            candidates.append((_import_module(module_name), module_name))
        except Exception as exc:
            import_errors.append(
                f"{module_name}: {exc.__class__.__name__}: "
                f"{_safe_exception_message(exc)}"
            )

    for module, source in candidates:
        try:
            blueprint = getattr(module, _PROJECT_ACCESS_BLUEPRINT_ATTRIBUTE, None)
        except Exception:
            blueprint = None

        if not _is_blueprint_like(blueprint):
            continue

        blueprint_name = _normalize_text(
            getattr(blueprint, "name", None),
            "",
        )
        if blueprint_name != _PROJECT_ACCESS_BLUEPRINT_NAME:
            raise RuntimeError(
                "Project-access blueprint has an unexpected name: "
                f"{blueprint_name!r}; expected "
                f"{_PROJECT_ACCESS_BLUEPRINT_NAME!r}."
            )

        return blueprint, source

    details = " | ".join(import_errors) if import_errors else "no candidate exported it"
    raise RuntimeError(
        "Could not resolve routes.project_access:project_access_bp; " + details
    )


def _ensure_project_access_blueprint_registered(
    app: Flask,
    routes_module: ModuleType,
) -> dict[str, Any]:
    """
    Verify or supplement registration of the productive access blueprint.

    Exactly-once behavior is based on Flask's authoritative ``app.blueprints``
    mapping. No route or database mutation is performed beyond normal blueprint
    registration during app construction.
    """
    enabled = _project_access_routes_enabled(app)
    required = _project_access_routes_required(app)
    before_names = _collect_registered_blueprint_names(app)

    diagnostics: dict[str, Any] = {
        "enabled": enabled,
        "required": required,
        "authzEnforced": False,
        "blueprintName": _PROJECT_ACCESS_BLUEPRINT_NAME,
        "blueprintAttribute": _PROJECT_ACCESS_BLUEPRINT_ATTRIBUTE,
        "moduleCandidates": list(_PROJECT_ACCESS_ROUTE_MODULE_CANDIDATES),
        "requiredRules": list(_PROJECT_ACCESS_REQUIRED_RULES),
        "registeredBeforeSupplement": (
            _PROJECT_ACCESS_BLUEPRINT_NAME in before_names
        ),
        "registered": False,
        "registrationSource": None,
        "supplementalRegistration": False,
        "missingRequiredRules": [],
        "error": None,
    }

    if not enabled:
        diagnostics["registered"] = (
            _PROJECT_ACCESS_BLUEPRINT_NAME in before_names
        )
        diagnostics["registrationSource"] = (
            "central_registry_despite_disabled_gate"
            if diagnostics["registered"]
            else "disabled"
        )
        return diagnostics

    if _PROJECT_ACCESS_BLUEPRINT_NAME in before_names:
        diagnostics["registered"] = True
        diagnostics["registrationSource"] = "routes.register_blueprints"
    else:
        try:
            blueprint, source = _load_project_access_blueprint(routes_module)
            app.register_blueprint(blueprint)
            diagnostics["registered"] = True
            diagnostics["registrationSource"] = source
            diagnostics["supplementalRegistration"] = True
            _safe_log_info(
                app,
                "Project-access blueprint registered by app-factory fallback "
                "(source=%s).",
                source,
            )
        except Exception as exc:
            diagnostics["error"] = _safe_exception_message(exc)
            diagnostics["errorType"] = exc.__class__.__name__
            if required:
                raise RuntimeError(
                    "Required project-access blueprint registration failed."
                ) from exc
            _safe_log_warning(
                app,
                "Optional project-access blueprint registration failed: %s",
                diagnostics["error"],
            )

    after_names = _collect_registered_blueprint_names(app)
    diagnostics["registered"] = (
        _PROJECT_ACCESS_BLUEPRINT_NAME in after_names
    )

    rule_set = set(_collect_route_rules(app))
    diagnostics["missingRequiredRules"] = [
        rule for rule in _PROJECT_ACCESS_REQUIRED_RULES if rule not in rule_set
    ]
    diagnostics["ready"] = bool(
        diagnostics["registered"]
        and not diagnostics["missingRequiredRules"]
    )

    if required and not diagnostics["ready"]:
        raise RuntimeError(
            "Project-access blueprint is not ready after registration; "
            f"registered={diagnostics['registered']}, "
            f"missingRules={diagnostics['missingRequiredRules']}."
        )

    return diagnostics


def _refresh_route_registry_metadata(
    app: Flask,
    routes_module: ModuleType,
    project_access_diagnostics: Mapping[str, Any],
) -> None:
    """Store central-registry and authoritative Flask routing diagnostics."""
    metadata = _ensure_app_metadata_registry(app)
    metadata["blueprints_registered"] = True
    metadata["registered_blueprint_names"] = _collect_registered_blueprint_names(app)
    metadata["project_access_blueprint_registered"] = bool(
        project_access_diagnostics.get("registered")
    )
    metadata["project_access_blueprint_registration"] = dict(
        project_access_diagnostics
    )
    metadata["routing_snapshot"] = _build_app_routing_snapshot(app)

    get_registered_blueprint_names = getattr(
        routes_module,
        "get_registered_blueprint_names",
        None,
    )
    if callable(get_registered_blueprint_names):
        try:
            metadata["route_registry_blueprint_names"] = list(
                get_registered_blueprint_names(app)
            )
        except Exception as exc:
            metadata["route_registry_blueprint_names_error"] = (
                _safe_exception_message(exc)
            )

    get_routing_metadata = getattr(routes_module, "get_routing_metadata", None)
    if callable(get_routing_metadata):
        try:
            route_metadata = get_routing_metadata(app)
            if isinstance(route_metadata, dict):
                route_metadata = dict(route_metadata)
            else:
                route_metadata = {"registryMetadata": route_metadata}
            route_metadata["appFactorySupplement"] = {
                "version": APP_FACTORY_VERSION,
                "projectAccess": dict(project_access_diagnostics),
                "authoritativeSnapshot": metadata["routing_snapshot"],
            }
            metadata["routing_metadata"] = route_metadata
        except Exception as exc:
            metadata["routing_metadata_error"] = _safe_exception_message(exc)


def _register_blueprints(app: Flask) -> None:
    """
    Register central route blueprints and verify the project-access surface.

    The central ``routes`` registry remains authoritative. The direct
    ``routes.project_access`` import is only a rollout fallback for a registry
    revision that does not yet list the new productive blueprint.
    """
    _ensure_service_root_on_sys_path()

    try:
        routes_module = _import_module(_ROUTE_MODULE_NAME)
    except Exception as exc:
        raise RuntimeError(
            "Module `routes` could not be imported. "
            "Check services/vectoplan-chunk/routes/__init__.py."
        ) from exc

    register_function = getattr(routes_module, "register_blueprints", None)
    if not callable(register_function):
        raise RuntimeError(
            "Module `routes` has no callable register_blueprints(app)."
        )

    try:
        register_function(app)
    except Exception as exc:
        raise RuntimeError("Blueprint registration failed.") from exc

    project_access_diagnostics = _ensure_project_access_blueprint_registered(
        app,
        routes_module,
    )
    _refresh_route_registry_metadata(
        app,
        routes_module,
        project_access_diagnostics,
    )


# -----------------------------------------------------------------------------
# Optional startup hooks
# -----------------------------------------------------------------------------

def _resolve_startup_module(app: Flask) -> tuple[ModuleType | None, str | None]:
    """
    Resolve preferred startup module.

    Behavior:
    - if candidate module itself is missing, next candidate is tried
    - if candidate exists but inner dependency is missing, fail hard
    """
    _ensure_service_root_on_sys_path()

    candidates = _get_startup_module_candidates()

    try:
        metadata = _ensure_app_metadata_registry(app)
        metadata["startup_module_candidates"] = list(candidates)
    except Exception:
        pass

    for module_name in candidates:
        try:
            module = _import_module(module_name)
            return module, module_name
        except ModuleNotFoundError as exc:
            if _is_missing_candidate_module(exc, module_name):
                _safe_log_debug(
                    app,
                    "Startup module `%s` not found; trying next candidate.",
                    module_name,
                )
                continue

            raise RuntimeError(
                f"Startup module `{module_name}` could not be loaded because "
                f"an inner dependency is missing: {exc.name!r}."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Startup module `{module_name}` could not be loaded."
            ) from exc

    return None, None


def _should_run_startup_hooks(app: Flask) -> bool:
    """Return whether optional startup hooks should run."""
    try:
        value = app.config.get("VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS", None)
        if value is not None:
            return bool(value)
    except Exception:
        pass

    disabled = (
        _env_flag("VECTOPLAN_CHUNK_DISABLE_STARTUP_HOOKS", default=False)
        or _env_flag("VECTOPLAN_EDITOR_DISABLE_STARTUP_HOOKS", default=False)
    )

    return not disabled


def _run_optional_startup_hooks(app: Flask) -> None:
    """
    Run optional startup hooks.

    Supported function names:
    - run_startup(app)
    - bootstrap_app(app)
    - initialize_app(app)

    Runtime startup hooks must remain read-only. They may perform:
    - bounded path/file/route/model checks
    - optional database connectivity checks
    - cheap schema/access readiness inspection
    - health preflight checks

    Schema creation, migrations, default seeding and repair belong exclusively
    to the explicit DB-bootstrap command/container.
    """
    metadata = _ensure_app_metadata_registry(app)
    metadata["startup_attempted"] = True

    if not _should_run_startup_hooks(app):
        metadata["startup_skipped"] = True
        metadata["startup_skip_reason"] = "startup_hooks_disabled"
        metadata["startup_routing_snapshot"] = _build_app_routing_snapshot(app)
        _safe_log_debug(app, "Startup hooks disabled by configuration.")
        return

    startup_module, module_name = _resolve_startup_module(app)

    if startup_module is None or module_name is None:
        metadata["startup_skipped"] = True
        metadata["startup_skip_reason"] = "startup_module_not_found"
        metadata["startup_routing_snapshot"] = _build_app_routing_snapshot(app)
        _safe_log_debug(
            app,
            "No startup module found; checked candidates: %s",
            ", ".join(_get_startup_module_candidates()),
        )
        return

    metadata["startup_module_name"] = module_name

    startup_function = None
    startup_function_name = None

    for function_name in ("run_startup", "bootstrap_app", "initialize_app"):
        candidate = getattr(startup_module, function_name, None)
        if callable(candidate):
            startup_function = candidate
            startup_function_name = function_name
            break

    if startup_function is None:
        metadata["startup_skipped"] = True
        metadata["startup_skip_reason"] = "startup_hook_not_found"
        metadata["startup_routing_snapshot"] = _build_app_routing_snapshot(app)
        _safe_log_debug(
            app,
            "Startup module `%s` found, but no known startup function is defined.",
            module_name,
        )
        return

    metadata["startup_hook_name"] = startup_function_name

    try:
        startup_function(app)
    except Exception as exc:
        raise RuntimeError(
            "Chunk-service startup hooks failed "
            f"(module={module_name}, hook={startup_function_name})."
        ) from exc

    try:
        metadata["startup_completed"] = True
        metadata["startup_skipped"] = False
        metadata["startup_skip_reason"] = None
        metadata["startup_routing_snapshot"] = _build_app_routing_snapshot(app)
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Root probe
# -----------------------------------------------------------------------------

def _register_root_probe(app: Flask) -> None:
    """
    Register a small root route if none exists.

    Helpful for manual browser checks on http://localhost:5002/.
    """
    try:
        existing_rules = {str(rule.rule) for rule in app.url_map.iter_rules()}
    except Exception:
        existing_rules = set()

    if "/" in existing_rules:
        return

    @app.get("/")
    def _vectoplan_chunk_root_probe():  # pragma: no cover - defensive convenience route
        metadata = _ensure_app_metadata_registry(app)

        return {
            "ok": True,
            "service": app.config.get("SERVICE_NAME", _DEFAULT_SERVICE_NAME),
            "app": app.config.get("APP_NAME", _DEFAULT_APP_NAME),
            "displayName": app.config.get("APP_DISPLAY_NAME", _DEFAULT_APP_DISPLAY_NAME),
            "version": app.config.get("SERVICE_VERSION", "0.1.0"),
            "projectScopedApi": {
                "bootstrap": "/projects/dev-project/bootstrap",
                "worlds": "/projects/dev-project/worlds",
                "blocks": "/projects/dev-project/worlds/world_spawn/blocks",
                "chunk": "/projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0",
                "batch": "/projects/dev-project/worlds/world_spawn/chunks/batch",
                "commands": "/projects/dev-project/worlds/world_spawn/commands",
            },
            "projectAccessApi": {
                "enabled": bool(metadata.get("project_access_api_enabled", True)),
                "registered": bool(
                    metadata.get("project_access_blueprint_registered", False)
                ),
                "authzEnforced": False,
                "status": "/project-access/_status",
                "summary": "/projects/dev-project/access",
                "initialize": "/projects/dev-project/access/initialize",
                "roles": "/projects/dev-project/roles",
                "groups": "/projects/dev-project/groups",
                "assignments": "/projects/dev-project/assignments",
            },
            "debug": {
                "worldTest": "/world-test",
                "worldTestHealth": "/world-test/api/health",
                "projectsStatus": "/projects/_status",
                "worldsStatus": "/worlds/_status",
                "blocksStatus": "/blocks/_status",
                "chunksStatus": "/chunks/_status",
                "projectAccessStatus": "/project-access/_status",
            },
            "metadata": {
                "namespace": metadata.get("namespace"),
                "blueprintsRegistered": metadata.get("blueprints_registered"),
                "extensionsInitialized": metadata.get("extensions_initialized"),
                "startupCompleted": metadata.get("startup_completed"),
                "startupSkipped": metadata.get("startup_skipped"),
                "databaseStartupCheck": metadata.get("database_startup_check"),
                "appFactoryVersion": metadata.get("app_factory_version"),
                "registeredBlueprintNames": metadata.get(
                    "registered_blueprint_names"
                ),
                "projectAccessRegistration": metadata.get(
                    "project_access_blueprint_registration"
                ),
                "routingSnapshot": metadata.get("routing_snapshot"),
                "startupRoutingSnapshot": metadata.get(
                    "startup_routing_snapshot"
                ),
            },
        }


# -----------------------------------------------------------------------------
# Public app factory
# -----------------------------------------------------------------------------

def create_app(config_object: type | str | None = None) -> Flask:
    """
    Public Flask app factory.

    Examples:
        app = create_app()
        app = create_app("testing")
        app = create_app(TestingConfig)

    Order:
    1. ensure service root on sys.path
    2. load .env
    3. resolve config class
    4. create Flask app
    5. apply config
    6. configure app defaults/logger
    7. validate config
    8. initialize extensions/db/migrations/models
    9. optionally check database connectivity
    10. register central blueprints
    11. verify/supplement project-access blueprint registration
    12. register root probe
    13. run optional read-only startup hooks
    """
    _ensure_service_root_on_sys_path()
    _load_environment_file()

    config_class = _resolve_config_class(config_object)
    app = _create_flask_app(config_class)

    _apply_config(app, config_class)
    _configure_app_defaults(app)
    _configure_logger(app)

    _validate_config(config_class, app.logger)
    _initialize_extensions(app)

    with app.app_context():
        _run_database_startup_check(app)

    _register_blueprints(app)
    _register_root_probe(app)

    with app.app_context():
        _run_optional_startup_hooks(app)

    metadata = _ensure_app_metadata_registry(app)
    metadata["routing_snapshot"] = _build_app_routing_snapshot(app)
    metadata["app_factory_ready"] = bool(
        metadata["routing_snapshot"].get("projectAccess", {}).get("ready")
        if _project_access_routes_enabled(app)
        and _project_access_routes_required(app)
        else True
    )

    _safe_log_info(
        app,
        "Flask app `%s` initialized successfully "
        "(config=%s, startup_module=%s, blueprints=%s, "
        "project_access_ready=%s).",
        app.config.get("APP_NAME", _DEFAULT_APP_NAME),
        config_class.__name__,
        metadata.get("startup_module_name"),
        metadata.get("registered_blueprint_names"),
        metadata.get("routing_snapshot", {}).get("projectAccess", {}).get("ready"),
    )

    return app


__all__ = ["APP_FACTORY_VERSION", "create_app"]