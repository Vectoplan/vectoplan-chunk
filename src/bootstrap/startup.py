# services/vectoplan-chunk/src/bootstrap/startup.py
"""
Read-only runtime startup hooks for the `vectoplan-chunk` service.

This module is the controlled runtime-startup layer for the chunk service.

Responsibilities:
- create and maintain startup state under app.extensions["vectoplan_chunk"]["startup"]
- collect compact startup metadata
- verify important service paths/files/routes through runtime_checks.py
- verify model registry availability without loading product data
- optionally perform a cheap DB connectivity check
- store compact settings/runtime-check summaries
- expose compatibility helpers for existing status routes

Important boundaries:
- no request handling here
- no chunk generation here
- no command execution here
- no editor UI logic here
- no migrations here
- no db.create_all() here
- no default seeding here
- no ChunkSnapshot loading here
- no ChunkEvent loading here
- no WorldCommandLog loading here
- no WorldObject/WorldObjectChunkRef loading here
- no recursive SQLAlchemy relationship serialization here

Design rule:

    Runtime startup must be cheap, bounded and read-only.

Database schema creation and default seed data are handled by the explicit DB
bootstrap path:

    src/bootstrap/db_bootstrap.py
    scripts/bootstrap_db.py

This prevents Gunicorn worker startup from running DB mutations in parallel.
"""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping

from flask import Flask

try:
    from extensions import (
        get_extension_summary,
        init_extensions,
        mark_extension_failed,
        mark_extension_initialized,
        mark_extension_warning,
        register_extension,
    )
except Exception:  # pragma: no cover - partial import/test environments
    get_extension_summary = None  # type: ignore[assignment]
    init_extensions = None  # type: ignore[assignment]
    mark_extension_failed = None  # type: ignore[assignment]
    mark_extension_initialized = None  # type: ignore[assignment]
    mark_extension_warning = None  # type: ignore[assignment]
    register_extension = None  # type: ignore[assignment]

try:
    from .settings import (
        build_bootstrap_settings,
        build_settings_summary,
        is_startup_strict,
        should_run_create_all_in_runtime,
        should_run_seed_in_runtime,
        should_run_startup_hooks,
    )
except Exception:  # pragma: no cover - fallback for direct imports
    build_bootstrap_settings = None  # type: ignore[assignment]
    build_settings_summary = None  # type: ignore[assignment]

    def is_startup_strict(app: Any = None) -> bool:
        return _safe_bool(_safe_get_config(app, "VECTOPLAN_CHUNK_STARTUP_STRICT", False), False)

    def should_run_create_all_in_runtime(app: Any = None) -> bool:
        return False

    def should_run_seed_in_runtime(app: Any = None) -> bool:
        return False

    def should_run_startup_hooks(app: Any = None) -> bool:
        return _safe_bool(_safe_get_config(app, "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS", True), True)

try:
    from .runtime_checks import (
        FileCheckSpec,
        PathCheckSpec,
        RouteCheckSpec,
        build_runtime_checks_summary,
        get_default_file_check_spec_data,
        get_default_file_check_specs,
        get_default_path_check_spec_data,
        get_default_path_check_specs,
        get_default_route_check_spec_data,
        get_default_route_check_specs,
        log_runtime_checks_result,
        raise_if_runtime_checks_failed,
        run_runtime_checks,
        runtime_checks_result_to_dict,
    )
except Exception:  # pragma: no cover - fallback if runtime_checks is temporarily unavailable
    FileCheckSpec = Any  # type: ignore[misc, assignment]
    PathCheckSpec = Any  # type: ignore[misc, assignment]
    RouteCheckSpec = Any  # type: ignore[misc, assignment]
    build_runtime_checks_summary = None  # type: ignore[assignment]
    get_default_file_check_spec_data = None  # type: ignore[assignment]
    get_default_file_check_specs = None  # type: ignore[assignment]
    get_default_path_check_spec_data = None  # type: ignore[assignment]
    get_default_path_check_specs = None  # type: ignore[assignment]
    get_default_route_check_spec_data = None  # type: ignore[assignment]
    get_default_route_check_specs = None  # type: ignore[assignment]
    log_runtime_checks_result = None  # type: ignore[assignment]
    raise_if_runtime_checks_failed = None  # type: ignore[assignment]
    run_runtime_checks = None  # type: ignore[assignment]
    runtime_checks_result_to_dict = None  # type: ignore[assignment]


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

CHUNK_NAMESPACE: Final[str] = "vectoplan_chunk"
LEGACY_EDITOR_NAMESPACE: Final[str] = "vectoplan_editor"
STARTUP_STATE_KEY: Final[str] = "startup"

DEFAULT_SERVICE_NAME: Final[str] = "vectoplan-chunk"
DEFAULT_DISPLAY_NAME: Final[str] = "VECTOPLAN Chunk Service"

STATUS_IDLE: Final[str] = "idle"
STATUS_RUNNING: Final[str] = "running"
STATUS_COMPLETED: Final[str] = "completed"
STATUS_SKIPPED: Final[str] = "skipped"
STATUS_FAILED: Final[str] = "failed"
STATUS_WARNING: Final[str] = "warning"

_TRUE_VALUES: Final[set[str]] = {"1", "true", "t", "yes", "y", "on", "enabled"}
_FALSE_VALUES: Final[set[str]] = {"0", "false", "f", "no", "n", "off", "disabled"}


# -----------------------------------------------------------------------------
# Compatibility data structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SeedOperationResult:
    """
    Compatibility seed operation result.

    Runtime startup no longer performs seeding. This class remains exported so
    old imports do not break while the seed logic lives in default_seed.py.
    """

    name: str
    ok: bool
    created: bool = False
    updated: bool = False
    skipped: bool = False
    message: str | None = None
    data: dict[str, Any] | None = None


# -----------------------------------------------------------------------------
# Primitive helpers
# -----------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """Return UTC timestamp as ISO string."""
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return "1970-01-01T00:00:00+00:00"


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


def _safe_log_exception(app: Any, message: str, *args: Any) -> None:
    """Exception-log defensively."""
    try:
        app.logger.exception(message, *args)
    except Exception:
        pass


def _safe_get_config(app: Any, key: str, default: Any = None) -> Any:
    """Read config value defensively."""
    if app is None:
        return default

    try:
        config = getattr(app, "config", None)
    except Exception:
        return default

    if config is None:
        return default

    try:
        if hasattr(config, "get"):
            return config.get(key, default)
    except Exception:
        pass

    try:
        return config[key]
    except Exception:
        return default


def _safe_get_env(key: str, default: Any = None) -> Any:
    """Read environment variable defensively."""
    try:
        value = os.getenv(key)
    except Exception:
        return default

    if value is None:
        return default

    return value


def _safe_config_or_env(app: Any, key: str, default: Any = None) -> Any:
    """Read environment first, then Flask config."""
    value = _safe_get_env(key, None)
    if value is not None:
        return value

    return _safe_get_config(app, key, default)


def _safe_str(value: Any, default: str = "") -> str:
    """Normalize any value to string."""
    if value is None:
        return default

    try:
        normalized = str(value).strip()
    except Exception:
        return default

    return normalized or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    """Normalize bool-like values."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = _safe_str(value, "")
    if not text:
        return default

    lowered = text.lower()

    if lowered in _TRUE_VALUES:
        return True

    if lowered in _FALSE_VALUES:
        return False

    return default


def _safe_int(
    value: Any,
    default: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Normalize integer values."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    except Exception:
        result = default

    if minimum is not None:
        try:
            result = max(minimum, result)
        except Exception:
            result = minimum

    if maximum is not None:
        try:
            result = min(maximum, result)
        except Exception:
            result = maximum

    return result


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_deepcopy(value: Any) -> Any:
    """Deep-copy defensively."""
    try:
        return deepcopy(value)
    except Exception:
        return value


def _safe_dict(value: Any) -> dict[str, Any]:
    """Normalize mapping-like value to dict."""
    if isinstance(value, dict):
        return value

    if isinstance(value, Mapping):
        try:
            return dict(value)
        except Exception:
            return {}

    return {}


def _is_flask_app(app: object) -> bool:
    """Check whether object can be treated as Flask app."""
    if isinstance(app, Flask):
        return True

    required_attributes = ("extensions", "config", "logger", "url_map")
    try:
        return all(hasattr(app, attr_name) for attr_name in required_attributes)
    except Exception:
        return False


def _message_to_state_item(message: str, code: str = "startup_message", details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build serializable state warning/error item."""
    return {
        "code": _safe_str(code, "startup_message"),
        "message": _safe_str(message, ""),
        "timestamp": _utc_now_iso(),
        "details": details or {},
    }


# -----------------------------------------------------------------------------
# Namespace / startup state
# -----------------------------------------------------------------------------

def _ensure_chunk_namespace(app: Flask) -> dict[str, Any]:
    """Ensure app.extensions['vectoplan_chunk'] exists."""
    if not _is_flask_app(app):
        raise TypeError("Startup hooks expect a Flask app or compatible object.")

    try:
        if not isinstance(app.extensions, dict):
            raise TypeError("app.extensions is not a dictionary.")
    except Exception as exc:
        raise RuntimeError("The Flask app has no usable extensions container.") from exc

    try:
        namespace = app.extensions.setdefault(CHUNK_NAMESPACE, {})
    except Exception as exc:
        raise RuntimeError("Could not create vectoplan_chunk namespace.") from exc

    if not isinstance(namespace, dict):
        raise RuntimeError(f"app.extensions['{CHUNK_NAMESPACE}'] is not a dictionary.")

    try:
        app.extensions.setdefault(LEGACY_EDITOR_NAMESPACE, namespace)
    except Exception:
        pass

    namespace.setdefault("namespace", CHUNK_NAMESPACE)
    namespace.setdefault("legacy_namespace", LEGACY_EDITOR_NAMESPACE)
    namespace.setdefault("service_name", _safe_get_config(app, "SERVICE_NAME", DEFAULT_SERVICE_NAME))

    return namespace


def _build_initial_startup_state() -> dict[str, Any]:
    """Build initial startup state."""
    return {
        "status": STATUS_IDLE,
        "started_at": None,
        "completed_at": None,
        "run_count": 0,
        "strict_mode": False,
        "warnings": [],
        "errors": [],
        "checks": {
            "paths": [],
            "files": [],
            "routes": [],
            "database": {},
            "models": {},
            "runtime": {},
        },
        "metadata": {},
        "settings": {},
        "runtimeChecks": {},
        "runtimeChecksSummary": {},
        "seed": {
            "attempted": False,
            "completed": False,
            "operations": [],
            "runtimeDisabled": True,
        },
        "database": {
            "checked": False,
            "ok": None,
            "create_all_attempted": False,
            "create_all_ok": None,
            "runtimeDisabled": True,
        },
        "route_summary": {
            "count": 0,
            "required_missing": [],
            "optional_missing": [],
        },
    }


def _ensure_startup_state(app: Flask) -> dict[str, Any]:
    """Ensure startup state container."""
    namespace = _ensure_chunk_namespace(app)

    startup_state = namespace.get(STARTUP_STATE_KEY)
    if not isinstance(startup_state, dict):
        startup_state = _build_initial_startup_state()
        namespace[STARTUP_STATE_KEY] = startup_state

    startup_state.setdefault("status", STATUS_IDLE)
    startup_state.setdefault("started_at", None)
    startup_state.setdefault("completed_at", None)
    startup_state.setdefault("run_count", 0)
    startup_state.setdefault("strict_mode", False)
    startup_state.setdefault("warnings", [])
    startup_state.setdefault("errors", [])
    startup_state.setdefault("checks", {})
    startup_state.setdefault("metadata", {})
    startup_state.setdefault("settings", {})
    startup_state.setdefault("runtimeChecks", {})
    startup_state.setdefault("runtimeChecksSummary", {})
    startup_state.setdefault("seed", {})
    startup_state.setdefault("database", {})
    startup_state.setdefault("route_summary", {})

    if not isinstance(startup_state["warnings"], list):
        startup_state["warnings"] = []

    if not isinstance(startup_state["errors"], list):
        startup_state["errors"] = []

    if not isinstance(startup_state["checks"], dict):
        startup_state["checks"] = {}

    for key, default in (
        ("paths", []),
        ("files", []),
        ("routes", []),
        ("database", {}),
        ("models", {}),
        ("runtime", {}),
    ):
        startup_state["checks"].setdefault(key, default)

    if not isinstance(startup_state["checks"]["paths"], list):
        startup_state["checks"]["paths"] = []
    if not isinstance(startup_state["checks"]["files"], list):
        startup_state["checks"]["files"] = []
    if not isinstance(startup_state["checks"]["routes"], list):
        startup_state["checks"]["routes"] = []
    if not isinstance(startup_state["checks"]["database"], dict):
        startup_state["checks"]["database"] = {}
    if not isinstance(startup_state["checks"]["models"], dict):
        startup_state["checks"]["models"] = {}
    if not isinstance(startup_state["checks"]["runtime"], dict):
        startup_state["checks"]["runtime"] = {}

    if not isinstance(startup_state["metadata"], dict):
        startup_state["metadata"] = {}

    if not isinstance(startup_state["settings"], dict):
        startup_state["settings"] = {}

    if not isinstance(startup_state["runtimeChecks"], dict):
        startup_state["runtimeChecks"] = {}

    if not isinstance(startup_state["runtimeChecksSummary"], dict):
        startup_state["runtimeChecksSummary"] = {}

    if not isinstance(startup_state["seed"], dict):
        startup_state["seed"] = {}

    startup_state["seed"].setdefault("attempted", False)
    startup_state["seed"].setdefault("completed", False)
    startup_state["seed"].setdefault("operations", [])
    startup_state["seed"].setdefault("runtimeDisabled", True)

    if not isinstance(startup_state["seed"]["operations"], list):
        startup_state["seed"]["operations"] = []

    if not isinstance(startup_state["database"], dict):
        startup_state["database"] = {}

    startup_state["database"].setdefault("checked", False)
    startup_state["database"].setdefault("ok", None)
    startup_state["database"].setdefault("create_all_attempted", False)
    startup_state["database"].setdefault("create_all_ok", None)
    startup_state["database"].setdefault("runtimeDisabled", True)

    if not isinstance(startup_state["route_summary"], dict):
        startup_state["route_summary"] = {}

    startup_state["route_summary"].setdefault("count", 0)
    startup_state["route_summary"].setdefault("required_missing", [])
    startup_state["route_summary"].setdefault("optional_missing", [])

    return startup_state


def _append_warning(app: Flask, message: str, code: str = "startup_warning", details: dict[str, Any] | None = None) -> None:
    """Append startup warning."""
    state = _ensure_startup_state(app)

    try:
        state["warnings"].append(_message_to_state_item(message, code=code, details=details))
    except Exception:
        pass

    _safe_log_warning(app, message)


def _append_error(app: Flask, message: str, code: str = "startup_error", details: dict[str, Any] | None = None) -> None:
    """Append startup error."""
    state = _ensure_startup_state(app)

    try:
        state["errors"].append(_message_to_state_item(message, code=code, details=details))
    except Exception:
        pass

    _safe_log_warning(app, message)


# -----------------------------------------------------------------------------
# Extension registry helpers
# -----------------------------------------------------------------------------

def _ensure_extension_registry(app: Flask) -> None:
    """
    Ensure extension registry exists.

    This function must remain read-only with respect to application data.
    It may initialize extension bookkeeping, but must not create tables or seed.
    """
    namespace = _ensure_chunk_namespace(app)

    if not namespace.get("extensions_initialized") and init_extensions is not None:
        try:
            init_extensions(app)
        except RuntimeError as exc:
            message = _safe_exception_message(exc)
            if "already registered" not in message.lower() and "already initialized" not in message.lower():
                raise
            _append_warning(
                app,
                f"Extension initialization was already applied: {message}",
                code="extension_initialization_already_applied",
            )
        except Exception as exc:
            message = _safe_exception_message(exc)
            raise RuntimeError(f"Extension registry initialization failed: {message}") from exc

    if register_extension is not None:
        try:
            register_extension(
                app,
                "startup",
                category="internal",
                description="Read-only runtime startup hooks and diagnostics.",
                required=True,
            )
        except Exception as exc:
            _append_warning(
                app,
                f"Could not register startup extension metadata: {_safe_exception_message(exc)}",
                code="startup_extension_registration_failed",
                details={"exceptionType": exc.__class__.__name__},
            )


def _mark_startup_initialized(app: Flask, metadata: dict[str, Any]) -> None:
    """Mark startup extension initialized defensively."""
    if mark_extension_initialized is None:
        return

    try:
        mark_extension_initialized(app, "startup", metadata=metadata)
    except Exception:
        pass


def _mark_startup_failed(app: Flask, message: str, metadata: dict[str, Any]) -> None:
    """Mark startup extension failed defensively."""
    if mark_extension_failed is None:
        return

    try:
        mark_extension_failed(app, "startup", message, metadata=metadata)
    except Exception:
        pass


def _mark_startup_warning(app: Flask, message: str, metadata: dict[str, Any] | None = None) -> None:
    """Mark startup extension warning defensively."""
    if mark_extension_warning is None:
        return

    try:
        mark_extension_warning(app, "startup", message, metadata=metadata or {})
    except Exception:
        pass


def _get_extension_summary(app: Flask) -> dict[str, Any]:
    """Get extension summary defensively."""
    if get_extension_summary is None:
        return {}

    try:
        summary = get_extension_summary(app)
        return _safe_dict(summary)
    except Exception:
        return {}


# -----------------------------------------------------------------------------
# Settings/runtime-check helpers
# -----------------------------------------------------------------------------

def _build_settings_summary(app: Flask) -> dict[str, Any]:
    """Build startup settings summary defensively."""
    if build_settings_summary is not None:
        try:
            summary = build_settings_summary(app)
            return _safe_dict(summary)
        except Exception as exc:
            return {
                "ok": False,
                "error": _safe_exception_message(exc),
                "source": "build_settings_summary",
            }

    return {
        "ok": True,
        "runtime": {
            "runStartupHooks": should_run_startup_hooks(app),
            "autoCreateAllInRuntime": False,
            "autoSeedDefaultsInRuntime": False,
        },
    }


def _build_bootstrap_settings(app: Flask) -> Any | None:
    """Build aggregate settings defensively."""
    if build_bootstrap_settings is None:
        return None

    try:
        return build_bootstrap_settings(app)
    except Exception:
        return None


def _should_run_startup_hooks(app: Flask) -> bool:
    """Return whether startup hooks should run."""
    try:
        return should_run_startup_hooks(app)
    except Exception:
        return _safe_bool(
            _safe_config_or_env(app, "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS", True),
            True,
        )


def _is_strict_startup_enabled(app: Flask) -> bool:
    """Return whether strict startup is enabled."""
    try:
        return is_startup_strict(app)
    except Exception:
        return _safe_bool(
            _safe_config_or_env(app, "VECTOPLAN_CHUNK_STARTUP_STRICT", False),
            False,
        )


def _runtime_db_mutations_requested(app: Flask) -> dict[str, bool]:
    """Return whether runtime DB mutation flags are requested/effective."""
    legacy_create_all_requested = _safe_bool(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_AUTO_CREATE_ALL", False),
        False,
    )
    legacy_seed_requested = _safe_bool(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS", False),
        False,
    )

    try:
        create_all_effective = should_run_create_all_in_runtime(app)
    except Exception:
        create_all_effective = False

    try:
        seed_effective = should_run_seed_in_runtime(app)
    except Exception:
        seed_effective = False

    return {
        "legacyCreateAllRequested": legacy_create_all_requested,
        "legacySeedRequested": legacy_seed_requested,
        "createAllEffective": bool(create_all_effective),
        "seedEffective": bool(seed_effective),
    }


def _run_read_only_runtime_checks(app: Flask, settings: Any | None) -> Any:
    """Run read-only runtime checks."""
    if run_runtime_checks is None:
        raise RuntimeError("runtime_checks.run_runtime_checks is unavailable.")

    return run_runtime_checks(
        app,
        settings=settings,
    )


def _store_runtime_checks_result(app: Flask, result: Any) -> None:
    """Store runtime checks result into startup state."""
    state = _ensure_startup_state(app)

    if runtime_checks_result_to_dict is not None:
        try:
            runtime_data = runtime_checks_result_to_dict(result)
        except Exception:
            runtime_data = {}
    else:
        runtime_data = {}

    if not runtime_data:
        try:
            if hasattr(result, "to_dict") and callable(result.to_dict):
                runtime_data = result.to_dict()
        except Exception:
            runtime_data = {}

    if not runtime_data and isinstance(result, Mapping):
        runtime_data = _safe_dict(result)

    state["runtimeChecks"] = runtime_data

    if build_runtime_checks_summary is not None:
        try:
            summary = build_runtime_checks_summary(result)
        except Exception:
            summary = {}
    else:
        summary = {}

    state["runtimeChecksSummary"] = _safe_dict(summary)

    if runtime_data:
        state["checks"]["paths"] = list(runtime_data.get("paths") or [])
        state["checks"]["files"] = list(runtime_data.get("files") or [])
        state["checks"]["routes"] = list(runtime_data.get("routes") or [])
        state["checks"]["database"] = _safe_dict(runtime_data.get("database"))
        state["checks"]["models"] = _safe_dict(runtime_data.get("models"))

        route_summary = _safe_dict(runtime_data.get("route_summary"))
        required_missing = list(route_summary.get("requiredMissing") or route_summary.get("required_missing") or [])
        optional_missing = list(route_summary.get("optionalMissing") or route_summary.get("optional_missing") or [])

        state["route_summary"] = {
            "count": _safe_int(route_summary.get("count", 0), 0, minimum=0),
            "required_missing": required_missing,
            "optional_missing": optional_missing,
            "rules": list(route_summary.get("rules") or []),
        }

        database = _safe_dict(runtime_data.get("database"))
        state["database"]["checked"] = bool(database.get("connectionChecked", database.get("checked", False)))
        state["database"]["ok"] = database.get("connectionOk", database.get("ok"))

    warnings = list(runtime_data.get("warnings") or [])
    for warning in warnings:
        if isinstance(warning, Mapping):
            state["warnings"].append(dict(warning))

    errors = list(runtime_data.get("errors") or [])
    for error in errors:
        if isinstance(error, Mapping):
            state["errors"].append(dict(error))


# -----------------------------------------------------------------------------
# State transitions
# -----------------------------------------------------------------------------

def _start_run(app: Flask) -> dict[str, Any]:
    """Mark startup run as running."""
    state = _ensure_startup_state(app)

    state["status"] = STATUS_RUNNING
    state["started_at"] = _utc_now_iso()
    state["completed_at"] = None
    state["run_count"] = _safe_int(state.get("run_count"), default=0, minimum=0) + 1
    state["strict_mode"] = _is_strict_startup_enabled(app)

    # Reset run-local data while keeping run_count.
    state["warnings"] = []
    state["errors"] = []
    state["runtimeChecks"] = {}
    state["runtimeChecksSummary"] = {}
    state["checks"] = {
        "paths": [],
        "files": [],
        "routes": [],
        "database": {},
        "models": {},
        "runtime": {},
    }
    state["route_summary"] = {
        "count": 0,
        "required_missing": [],
        "optional_missing": [],
    }
    state["seed"] = {
        "attempted": False,
        "completed": False,
        "operations": [],
        "runtimeDisabled": True,
    }
    state["database"] = {
        "checked": False,
        "ok": None,
        "create_all_attempted": False,
        "create_all_ok": None,
        "runtimeDisabled": True,
    }

    return state


def _complete_run(app: Flask, status: str = STATUS_COMPLETED) -> dict[str, Any]:
    """Mark startup run as completed."""
    state = _ensure_startup_state(app)

    state["completed_at"] = _utc_now_iso()

    if state.get("errors"):
        state["status"] = STATUS_FAILED
    elif state.get("warnings") and status == STATUS_COMPLETED:
        state["status"] = STATUS_WARNING
    else:
        state["status"] = status

    return state


def _skip_run(app: Flask, reason: str) -> dict[str, Any]:
    """Mark startup run as skipped."""
    state = _ensure_startup_state(app)

    now = _utc_now_iso()
    state["status"] = STATUS_SKIPPED
    state["started_at"] = now
    state["completed_at"] = now
    state["run_count"] = _safe_int(state.get("run_count"), default=0, minimum=0) + 1
    state["strict_mode"] = _is_strict_startup_enabled(app)

    state["metadata"]["skipReason"] = reason
    state["metadata"]["settingsSummary"] = _build_settings_summary(app)

    state["database"]["checked"] = False
    state["database"]["ok"] = None
    state["database"]["create_all_attempted"] = False
    state["database"]["create_all_ok"] = None
    state["database"]["runtimeDisabled"] = True

    state["seed"]["attempted"] = False
    state["seed"]["completed"] = False
    state["seed"]["runtimeDisabled"] = True

    _safe_log_info(app, "Startup hooks for `vectoplan-chunk` skipped: %s", reason)

    _mark_startup_initialized(
        app,
        {
            "status": STATUS_SKIPPED,
            "runCount": state["run_count"],
            "skipped": True,
            "reason": reason,
            "completedAt": state["completed_at"],
        },
    )

    return state


def _fail_run(app: Flask, exc: BaseException) -> dict[str, Any]:
    """Mark startup run as failed."""
    state = _ensure_startup_state(app)
    state["status"] = STATUS_FAILED
    state["completed_at"] = _utc_now_iso()

    error_message = f"Startup of `vectoplan-chunk` failed: {_safe_exception_message(exc)}"
    _append_error(
        app,
        error_message,
        code="startup_failed",
        details={"exceptionType": exc.__class__.__name__},
    )
    _safe_log_exception(app, error_message)

    _mark_startup_failed(
        app,
        error_message,
        metadata={
            "status": state["status"],
            "runCount": state["run_count"],
            "strictMode": state["strict_mode"],
            "completedAt": state["completed_at"],
        },
    )

    return state


# -----------------------------------------------------------------------------
# Public startup functions
# -----------------------------------------------------------------------------

def run_startup(app: Flask) -> Flask:
    """
    Run read-only runtime startup for `vectoplan-chunk`.

    Idempotent:
    - repeated calls do not destroy the app
    - run_count is incremented
    - startup state is refreshed

    Safe:
    - does not create tables
    - does not seed defaults
    - does not load chunks/snapshots/events/object refs

    Critical failures:
    - incompatible app object
    - required path/file/route/model/database checks fail
    """
    if not _is_flask_app(app):
        raise TypeError("run_startup(app) expects a Flask app or compatible object.")

    if not _should_run_startup_hooks(app):
        _ensure_chunk_namespace(app)
        _skip_run(app, "VECTOPLAN_CHUNK_RUN_STARTUP_HOOKS=false")
        return app

    state = _start_run(app)

    _safe_log_info(app, "Startup hooks for `vectoplan-chunk` are running.")

    try:
        _ensure_extension_registry(app)

        settings_summary = _build_settings_summary(app)
        state["settings"] = settings_summary
        state["metadata"]["settingsSummary"] = settings_summary

        mutation_flags = _runtime_db_mutations_requested(app)
        state["metadata"]["runtimeDbMutationFlags"] = mutation_flags

        if mutation_flags.get("legacyCreateAllRequested") and not mutation_flags.get("createAllEffective"):
            _append_warning(
                app,
                (
                    "VECTOPLAN_CHUNK_AUTO_CREATE_ALL was requested but ignored during runtime startup. "
                    "Use scripts/bootstrap_db.py or db_bootstrap.py for schema bootstrap."
                ),
                code="runtime_create_all_ignored",
                details=mutation_flags,
            )

        if mutation_flags.get("legacySeedRequested") and not mutation_flags.get("seedEffective"):
            _append_warning(
                app,
                (
                    "VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS was requested but ignored during runtime startup. "
                    "Use scripts/bootstrap_db.py or db_bootstrap.py for default seeding."
                ),
                code="runtime_seed_ignored",
                details=mutation_flags,
            )

        state["database"]["create_all_attempted"] = False
        state["database"]["create_all_ok"] = None
        state["database"]["runtimeDisabled"] = True
        state["seed"]["attempted"] = False
        state["seed"]["completed"] = False
        state["seed"]["runtimeDisabled"] = True

        settings = _build_bootstrap_settings(app)
        runtime_result = _run_read_only_runtime_checks(app, settings)
        _store_runtime_checks_result(app, runtime_result)

        if log_runtime_checks_result is not None:
            try:
                log_runtime_checks_result(app, runtime_result)
            except Exception:
                pass

        # raise_if_runtime_checks_failed encodes the intended policy:
        # errors fail startup, warnings do not.
        if raise_if_runtime_checks_failed is not None:
            raise_if_runtime_checks_failed(runtime_result)
        else:
            if state.get("errors"):
                first_error = state["errors"][0]
                message = first_error.get("message", "Runtime startup checks failed.")
                raise RuntimeError(message)

        extension_summary = _get_extension_summary(app)
        state["metadata"]["extensionSummary"] = extension_summary

        completed_state = _complete_run(app, STATUS_COMPLETED)

        _mark_startup_initialized(
            app,
            metadata={
                "status": completed_state["status"],
                "runCount": completed_state["run_count"],
                "strictMode": completed_state["strict_mode"],
                "routeCount": completed_state["route_summary"].get("count", 0),
                "requiredMissingRoutes": completed_state["route_summary"].get("required_missing", []),
                "warningCount": len(completed_state.get("warnings", []) or []),
                "errorCount": len(completed_state.get("errors", []) or []),
                "seedAttempted": False,
                "seedCompleted": False,
                "seedRuntimeDisabled": True,
                "createAllAttempted": False,
                "createAllOk": None,
                "createAllRuntimeDisabled": True,
                "completedAt": completed_state["completed_at"],
            },
        )

        if completed_state["status"] == STATUS_WARNING:
            _mark_startup_warning(
                app,
                "Startup hooks for `vectoplan-chunk` completed with warnings.",
                metadata={
                    "warningCount": len(completed_state.get("warnings", []) or []),
                },
            )
            _safe_log_warning(app, "Startup hooks for `vectoplan-chunk` completed with warnings.")
        else:
            _safe_log_info(app, "Startup hooks for `vectoplan-chunk` completed successfully.")

        return app

    except Exception as exc:
        _fail_run(app, exc)
        raise


def bootstrap_app(app: Flask) -> Flask:
    """Compatibility alias."""
    return run_startup(app)


def initialize_app(app: Flask) -> Flask:
    """Compatibility alias."""
    return run_startup(app)


# -----------------------------------------------------------------------------
# Read/debug helpers
# -----------------------------------------------------------------------------

def get_startup_state(app: Flask) -> dict[str, Any]:
    """Return startup state as defensive copy."""
    state = _ensure_startup_state(app)
    return _safe_deepcopy(state)


def get_startup_summary(app: Flask) -> dict[str, Any]:
    """Return compact startup summary."""
    state = _ensure_startup_state(app)

    runtime_checks_summary = _safe_dict(state.get("runtimeChecksSummary"))
    settings_summary = _safe_dict(state.get("settings"))

    return {
        "status": _safe_str(state.get("status"), "unknown"),
        "startedAt": state.get("started_at"),
        "completedAt": state.get("completed_at"),
        "runCount": _safe_int(state.get("run_count"), default=0, minimum=0),
        "strictMode": _safe_bool(state.get("strict_mode"), False),
        "warningCount": len(state.get("warnings", []) or []),
        "errorCount": len(state.get("errors", []) or []),
        "routeCount": _safe_int(
            state.get("route_summary", {}).get("count", 0),
            default=0,
            minimum=0,
        ),
        "requiredMissingRoutes": list(state.get("route_summary", {}).get("required_missing", []) or []),
        "optionalMissingRoutes": list(state.get("route_summary", {}).get("optional_missing", []) or []),
        "database": {
            "checked": state.get("database", {}).get("checked", False),
            "ok": state.get("database", {}).get("ok"),
            "createAllAttempted": False,
            "createAllOk": None,
            "runtimeDisabled": True,
        },
        "seed": {
            "attempted": False,
            "completed": False,
            "operationCount": 0,
            "runtimeDisabled": True,
        },
        "runtimeChecks": runtime_checks_summary,
        "settings": settings_summary,
    }


def get_runtime_checks_summary(app: Flask) -> dict[str, Any]:
    """Return compact runtime checks summary."""
    state = _ensure_startup_state(app)
    return _safe_deepcopy(_safe_dict(state.get("runtimeChecksSummary")))


def get_settings_summary(app: Flask) -> dict[str, Any]:
    """Return compact bootstrap settings summary."""
    state = _ensure_startup_state(app)
    summary = _safe_dict(state.get("settings"))

    if summary:
        return _safe_deepcopy(summary)

    return _build_settings_summary(app)


# -----------------------------------------------------------------------------
# Compatibility helpers for old imports
# -----------------------------------------------------------------------------

def _seed_operation_to_dict(result: SeedOperationResult) -> dict[str, Any]:
    """Serialize compatibility seed operation result."""
    try:
        return asdict(result)
    except Exception:
        return {
            "name": getattr(result, "name", "unknown"),
            "ok": bool(getattr(result, "ok", False)),
            "created": bool(getattr(result, "created", False)),
            "updated": bool(getattr(result, "updated", False)),
            "skipped": bool(getattr(result, "skipped", False)),
            "message": getattr(result, "message", None),
            "data": getattr(result, "data", None) or {},
        }


def _run_create_all_if_enabled(app: Flask) -> None:
    """
    Compatibility no-op.

    Runtime startup no longer creates tables. Use:

        scripts/bootstrap_db.py
        src.bootstrap.db_bootstrap.run_db_bootstrap()
    """
    state = _ensure_startup_state(app)
    state["database"]["create_all_attempted"] = False
    state["database"]["create_all_ok"] = None
    state["database"]["runtimeDisabled"] = True

    _append_warning(
        app,
        "Runtime db.create_all() is disabled. Use explicit DB bootstrap instead.",
        code="runtime_create_all_disabled",
    )


def _run_default_seeding_if_enabled(app: Flask) -> None:
    """
    Compatibility no-op.

    Runtime startup no longer seeds defaults. Use:

        scripts/bootstrap_db.py
        src.bootstrap.db_bootstrap.run_db_bootstrap()
    """
    state = _ensure_startup_state(app)
    state["seed"]["attempted"] = False
    state["seed"]["completed"] = False
    state["seed"]["runtimeDisabled"] = True

    _append_warning(
        app,
        "Runtime default seeding is disabled. Use explicit DB bootstrap instead.",
        code="runtime_seed_disabled",
    )


# -----------------------------------------------------------------------------
# Fallback spec helpers if runtime_checks import failed
# -----------------------------------------------------------------------------

if get_default_path_check_specs is None:
    def get_default_path_check_specs() -> tuple[Any, ...]:  # type: ignore[no-redef]
        return tuple()

if get_default_file_check_specs is None:
    def get_default_file_check_specs() -> tuple[Any, ...]:  # type: ignore[no-redef]
        return tuple()

if get_default_route_check_specs is None:
    def get_default_route_check_specs() -> tuple[Any, ...]:  # type: ignore[no-redef]
        return tuple()

if get_default_path_check_spec_data is None:
    def get_default_path_check_spec_data() -> list[dict[str, Any]]:  # type: ignore[no-redef]
        return []

if get_default_file_check_spec_data is None:
    def get_default_file_check_spec_data() -> list[dict[str, Any]]:  # type: ignore[no-redef]
        return []

if get_default_route_check_spec_data is None:
    def get_default_route_check_spec_data() -> list[dict[str, Any]]:  # type: ignore[no-redef]
        return []


# -----------------------------------------------------------------------------
# Public exports
# -----------------------------------------------------------------------------

__all__ = [
    "PathCheckSpec",
    "FileCheckSpec",
    "RouteCheckSpec",
    "SeedOperationResult",
    "get_default_path_check_specs",
    "get_default_file_check_specs",
    "get_default_route_check_specs",
    "get_default_path_check_spec_data",
    "get_default_file_check_spec_data",
    "get_default_route_check_spec_data",
    "run_startup",
    "bootstrap_app",
    "initialize_app",
    "get_startup_state",
    "get_startup_summary",
    "get_runtime_checks_summary",
    "get_settings_summary",
]