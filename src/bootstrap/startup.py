# services/vectoplan-chunk/src/bootstrap/startup.py
"""
Read-only runtime startup hooks for the `vectoplan-chunk` service.

This module is the controlled runtime-startup layer for the chunk service.

Responsibilities:
- create and maintain versioned startup state under
  app.extensions["vectoplan_chunk"]["startup"],
- collect compact app, routing and extension metadata,
- verify important service paths/files/routes through runtime_checks.py,
- verify model registry availability without loading product data,
- optionally perform a cheap DB connectivity check,
- store read-only schema, project-owner and project-access readiness,
- verify the centrally registered project-access route surface,
- store compact settings/runtime-check/readiness summaries,
- expose compatibility helpers for existing status routes.

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
- no authorization enforcement here
- no caching of ORM rows, query results or database state here

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
STARTUP_STATE_VERSION: Final[str] = "startup-state.v2"
STARTUP_CONTRACT_VERSION: Final[str] = "runtime-startup.v2"

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

PROJECT_ACCESS_BLUEPRINT_NAME: Final[str] = "project_access"
PROJECT_ACCESS_CORE_ROUTE_RULES: Final[tuple[str, ...]] = (
    "/project-access/_status",
    "/projects/<project_id>/access",
    "/projects/<project_id>/access/initialize",
    "/projects/<project_id>/roles",
    "/projects/<project_id>/groups",
    "/projects/<project_id>/assignments",
)


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


def _safe_list(value: Any) -> list[Any]:
    """Normalize sequence-like values to a new list without consuming mappings."""
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


def _safe_optional_bool(value: Any) -> bool | None:
    """Return bool for explicit values and None for absent/unknown values."""
    if value is None:
        return None
    return _safe_bool(value, False)


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


def _build_empty_readiness_state() -> dict[str, Any]:
    """Return stable readiness keys used by startup and status consumers."""
    return {
        "startupReady": None,
        "runtimeChecksReady": None,
        "databaseReady": None,
        "modelsReady": None,
        "schemaChecked": False,
        "schemaReady": None,
        "projectOwnerColumnsReady": None,
        "projectAccessSchemaReady": None,
        "projectAccessChecked": False,
        "defaultProjectOwnerReady": None,
        "defaultProjectRolesReady": None,
        "defaultProjectOwnerAssignmentReady": None,
        "defaultProjectAccessReady": None,
        "projectAccessRouteSurfaceReady": None,
        "projectAccessApiEnabled": True,
        "projectAccessRoutesRequired": True,
        "projectAccessAuthzEnforced": False,
    }


def _build_empty_routing_state() -> dict[str, Any]:
    """Return a compact routing-state placeholder."""
    return {
        "routingInitialized": False,
        "registeredBlueprintNames": [],
        "routeCount": 0,
        "rules": [],
        "projectAccess": {
            "enabled": True,
            "required": True,
            "blueprintRegistered": False,
            "routeSurfaceReady": False,
            "coreRouteRules": list(PROJECT_ACCESS_CORE_ROUTE_RULES),
            "missingRouteRules": list(PROJECT_ACCESS_CORE_ROUTE_RULES),
            "authzEnforced": False,
        },
    }


def _get_route_rules(app: Flask) -> list[str]:
    """Collect current Flask rules without invoking any route handler."""
    try:
        return sorted({str(rule.rule) for rule in app.url_map.iter_rules()})
    except Exception:
        return []


def _get_blueprint_names(app: Flask) -> list[str]:
    """Collect registered Blueprint names from Flask's authoritative registry."""
    try:
        blueprints = getattr(app, "blueprints", {})
        if isinstance(blueprints, Mapping):
            return sorted(_safe_str(name, "") for name in blueprints if _safe_str(name, ""))
    except Exception:
        pass
    return []


def _read_central_routing_state(app: Flask) -> dict[str, Any]:
    """Read the central route registry metadata without importing route modules."""
    try:
        namespace = _ensure_chunk_namespace(app)
        routing = namespace.get("routing")
        if isinstance(routing, Mapping):
            return dict(routing)
    except Exception:
        pass
    return {}


def _project_access_routes_enabled(app: Flask, routing: Mapping[str, Any]) -> bool:
    value = routing.get("projectAccessApiEnabled")
    if value is not None:
        return _safe_bool(value, True)
    return _safe_bool(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES", True),
        True,
    )


def _project_access_routes_required(
    app: Flask,
    routing: Mapping[str, Any],
    *,
    enabled: bool,
) -> bool:
    if not enabled:
        return False
    value = routing.get("projectAccessRoutesRequired")
    if value is not None:
        return _safe_bool(value, True)
    return _safe_bool(
        _safe_config_or_env(app, "VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_ROUTES", True),
        True,
    )


def _build_routing_snapshot(app: Flask) -> dict[str, Any]:
    """Build bounded, JSON-safe routing and project-access route diagnostics."""
    central = _read_central_routing_state(app)
    rules = _get_route_rules(app)
    rule_set = set(rules)
    blueprint_names = _get_blueprint_names(app)

    enabled = _project_access_routes_enabled(app, central)
    required = _project_access_routes_required(app, central, enabled=enabled)
    blueprint_registered = PROJECT_ACCESS_BLUEPRINT_NAME in blueprint_names
    missing_rules = [
        rule for rule in PROJECT_ACCESS_CORE_ROUTE_RULES if rule not in rule_set
    ]
    route_surface_ready = bool(
        not enabled or (blueprint_registered and not missing_rules)
    )

    central_project_access = _safe_dict(central.get("projectAccess"))
    routing_initialized = _safe_bool(
        central.get("routingInitialized"),
        bool(blueprint_names),
    )

    return {
        "routingInitialized": routing_initialized,
        "routesRegistryVersion": central.get("routesRegistryVersion"),
        "registeredBlueprintNames": blueprint_names,
        "routeCount": len(rules),
        "rules": rules,
        "registrationErrorCount": _safe_int(
            central.get("blueprintRegistrationErrorCount"),
            len(_safe_list(central.get("errors"))),
            minimum=0,
        ),
        "registrationSuccessCount": _safe_int(
            central.get("blueprintRegistrationSuccessCount"),
            len(_safe_list(central.get("successes"))),
            minimum=0,
        ),
        "registrationSkippedCount": _safe_int(
            central.get("blueprintRegistrationSkippedCount"),
            len(_safe_list(central.get("skipped"))),
            minimum=0,
        ),
        "projectAccess": {
            "enabled": enabled,
            "required": required,
            "blueprintRegistered": blueprint_registered,
            "routeSurfaceReady": route_surface_ready,
            "coreRouteRules": list(PROJECT_ACCESS_CORE_ROUTE_RULES),
            "missingRouteRules": missing_rules,
            "authzEnforced": False,
            "registryStatus": central_project_access,
        },
    }


def _derive_readiness_state(
    runtime_data: Mapping[str, Any] | None,
    routing_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize runtime-check and route-registry results into one readiness view."""
    runtime = _safe_dict(runtime_data)
    routing = _safe_dict(routing_snapshot)
    database = _safe_dict(runtime.get("database"))
    models = _safe_dict(runtime.get("models"))
    schema = _safe_dict(runtime.get("schema"))
    access = _safe_dict(runtime.get("project_access"))
    route_access = _safe_dict(routing.get("projectAccess"))

    schema_checked = _safe_bool(schema.get("checked"), False)
    access_checked = _safe_bool(access.get("checked"), False)
    access_ready = access.get("accessReady", access.get("ok"))
    route_ready = route_access.get("routeSurfaceReady")
    routes_enabled = _safe_bool(route_access.get("enabled"), True)
    routes_required = _safe_bool(route_access.get("required"), routes_enabled)

    runtime_ready = runtime.get("ok")
    if runtime_ready is not None:
        runtime_ready = _safe_bool(runtime_ready, False)

    routing_requirement_ready = bool(
        not routes_enabled
        or not routes_required
        or _safe_bool(route_ready, False)
    )
    startup_ready = (
        bool(runtime_ready and routing_requirement_ready)
        if runtime_ready is not None
        else None
    )

    connection_checked = _safe_bool(database.get("connectionChecked"), False)
    database_ready = (
        _safe_optional_bool(database.get("connectionOk"))
        if connection_checked
        else None
    )

    return {
        "startupReady": startup_ready,
        "runtimeChecksReady": runtime_ready,
        "databaseReady": database_ready,
        "modelsReady": _safe_optional_bool(models.get("ok")),
        "schemaChecked": schema_checked,
        "schemaReady": _safe_optional_bool(schema.get("ok")) if schema_checked else None,
        "projectOwnerColumnsReady": _safe_optional_bool(
            schema.get("projectOwnerColumnsReady")
        ),
        "projectAccessSchemaReady": _safe_optional_bool(
            schema.get("projectAccessSchemaReady")
        ),
        "projectAccessChecked": access_checked,
        "defaultProjectOwnerReady": _safe_optional_bool(access.get("ownerReady")),
        "defaultProjectRolesReady": _safe_optional_bool(access.get("rolesReady")),
        "defaultProjectOwnerAssignmentReady": _safe_optional_bool(
            access.get("ownerAssignmentReady")
        ),
        "defaultProjectAccessReady": _safe_optional_bool(access_ready),
        "projectAccessRouteSurfaceReady": _safe_optional_bool(route_ready),
        "projectAccessApiEnabled": routes_enabled,
        "projectAccessRoutesRequired": routes_required,
        "projectAccessAuthzEnforced": False,
    }


def _store_namespace_startup_projection(app: Flask, state: Mapping[str, Any]) -> None:
    """Expose compact readiness fields at namespace top-level for old consumers."""
    try:
        namespace = _ensure_chunk_namespace(app)
        readiness = _safe_dict(state.get("readiness"))
        routing = _safe_dict(state.get("routing"))
        route_access = _safe_dict(routing.get("projectAccess"))

        namespace["startup_state_version"] = STARTUP_STATE_VERSION
        namespace["startup_contract_version"] = STARTUP_CONTRACT_VERSION
        namespace["startup_status"] = state.get("status")
        namespace["startup_ready"] = readiness.get("startupReady")
        namespace["runtime_checks_ready"] = readiness.get("runtimeChecksReady")
        namespace["schema_ready"] = readiness.get("schemaReady")
        namespace["project_owner_columns_ready"] = readiness.get(
            "projectOwnerColumnsReady"
        )
        namespace["project_access_schema_ready"] = readiness.get(
            "projectAccessSchemaReady"
        )
        namespace["default_project_owner_ready"] = readiness.get(
            "defaultProjectOwnerReady"
        )
        namespace["default_project_roles_ready"] = readiness.get(
            "defaultProjectRolesReady"
        )
        namespace["default_project_owner_assignment_ready"] = readiness.get(
            "defaultProjectOwnerAssignmentReady"
        )
        namespace["default_project_access_ready"] = readiness.get(
            "defaultProjectAccessReady"
        )
        namespace["project_access_api_enabled"] = readiness.get(
            "projectAccessApiEnabled"
        )
        namespace["project_access_routes_required"] = readiness.get(
            "projectAccessRoutesRequired"
        )
        namespace["project_access_route_surface_ready"] = readiness.get(
            "projectAccessRouteSurfaceReady"
        )
        namespace["project_access_blueprint_registered"] = route_access.get(
            "blueprintRegistered"
        )
        namespace["project_access_authz_enforced"] = False
        namespace["startup_readiness"] = dict(readiness)
        namespace["startup_routing"] = dict(routing)
        namespace["runtime_checks_summary"] = _safe_dict(
            state.get("runtimeChecksSummary")
        )
    except Exception:
        pass


def _build_initial_startup_state() -> dict[str, Any]:
    """Build a fresh, serializable startup state contract."""
    return {
        "state_version": STARTUP_STATE_VERSION,
        "contract_version": STARTUP_CONTRACT_VERSION,
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
            "schema": {},
            "project_access": {},
            "routing": {},
            "runtime": {},
        },
        "metadata": {
            "authzEnforced": False,
            "runtimeReadOnly": True,
        },
        "settings": {},
        "runtimeChecks": {},
        "runtimeChecksSummary": {},
        "readiness": _build_empty_readiness_state(),
        "routing": _build_empty_routing_state(),
        "projectAccess": {
            "checked": False,
            "ready": None,
            "required": False,
            "authzEnforced": False,
        },
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
            "rules": [],
        },
    }


def _ensure_startup_state(app: Flask) -> dict[str, Any]:
    """Ensure startup state container."""
    namespace = _ensure_chunk_namespace(app)

    startup_state = namespace.get(STARTUP_STATE_KEY)
    if not isinstance(startup_state, dict):
        startup_state = _build_initial_startup_state()
        namespace[STARTUP_STATE_KEY] = startup_state

    startup_state.setdefault("state_version", STARTUP_STATE_VERSION)
    startup_state.setdefault("contract_version", STARTUP_CONTRACT_VERSION)
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
    startup_state.setdefault("readiness", _build_empty_readiness_state())
    startup_state.setdefault("routing", _build_empty_routing_state())
    startup_state.setdefault("projectAccess", {})
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
        ("schema", {}),
        ("project_access", {}),
        ("routing", {}),
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
    if not isinstance(startup_state["checks"]["schema"], dict):
        startup_state["checks"]["schema"] = {}
    if not isinstance(startup_state["checks"]["project_access"], dict):
        startup_state["checks"]["project_access"] = {}
    if not isinstance(startup_state["checks"]["routing"], dict):
        startup_state["checks"]["routing"] = {}
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

    if not isinstance(startup_state["readiness"], dict):
        startup_state["readiness"] = _build_empty_readiness_state()
    else:
        for key, value in _build_empty_readiness_state().items():
            startup_state["readiness"].setdefault(key, value)

    if not isinstance(startup_state["routing"], dict):
        startup_state["routing"] = _build_empty_routing_state()

    if not isinstance(startup_state["projectAccess"], dict):
        startup_state["projectAccess"] = {}
    startup_state["projectAccess"].setdefault("checked", False)
    startup_state["projectAccess"].setdefault("ready", None)
    startup_state["projectAccess"].setdefault("required", False)
    startup_state["projectAccess"].setdefault("authzEnforced", False)

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
    startup_state["route_summary"].setdefault("rules", [])

    startup_state["state_version"] = STARTUP_STATE_VERSION
    startup_state["contract_version"] = STARTUP_CONTRACT_VERSION
    startup_state["metadata"]["authzEnforced"] = False
    startup_state["metadata"]["runtimeReadOnly"] = True

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
    """Store the complete read-only runtime result and derived readiness state."""
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

    runtime_data = _safe_dict(runtime_data)
    state["runtimeChecks"] = runtime_data

    if build_runtime_checks_summary is not None:
        try:
            summary = build_runtime_checks_summary(result)
        except Exception:
            summary = {}
    else:
        summary = {}

    state["runtimeChecksSummary"] = _safe_dict(summary)

    routing_snapshot = _build_routing_snapshot(app)
    state["routing"] = routing_snapshot
    state["checks"]["routing"] = dict(routing_snapshot)

    if runtime_data:
        state["checks"]["paths"] = list(runtime_data.get("paths") or [])
        state["checks"]["files"] = list(runtime_data.get("files") or [])
        state["checks"]["routes"] = list(runtime_data.get("routes") or [])
        state["checks"]["database"] = _safe_dict(runtime_data.get("database"))
        state["checks"]["models"] = _safe_dict(runtime_data.get("models"))
        state["checks"]["schema"] = _safe_dict(runtime_data.get("schema"))
        state["checks"]["project_access"] = _safe_dict(
            runtime_data.get("project_access")
        )
        state["checks"]["runtime"] = _safe_dict(summary)

        route_summary = _safe_dict(runtime_data.get("route_summary"))
        required_missing = list(
            route_summary.get("requiredMissing")
            or route_summary.get("required_missing")
            or []
        )
        optional_missing = list(
            route_summary.get("optionalMissing")
            or route_summary.get("optional_missing")
            or []
        )

        state["route_summary"] = {
            "count": _safe_int(route_summary.get("count", 0), 0, minimum=0),
            "required_missing": required_missing,
            "optional_missing": optional_missing,
            "rules": list(route_summary.get("rules") or []),
        }

        database = _safe_dict(runtime_data.get("database"))
        state["database"]["checked"] = bool(
            database.get("connectionChecked", database.get("checked", False))
        )
        state["database"]["ok"] = database.get(
            "connectionOk",
            database.get("ok"),
        )

    access = _safe_dict(runtime_data.get("project_access"))
    state["projectAccess"] = {
        "checked": _safe_bool(access.get("checked"), False),
        "ready": _safe_optional_bool(
            access.get("accessReady", access.get("ok"))
        ),
        "required": _safe_bool(access.get("required"), False),
        "projectId": access.get("projectId"),
        "ownerUserId": access.get("ownerUserId"),
        "ownerReady": _safe_optional_bool(access.get("ownerReady")),
        "rolesReady": _safe_optional_bool(access.get("rolesReady")),
        "ownerAssignmentReady": _safe_optional_bool(
            access.get("ownerAssignmentReady")
        ),
        "authzEnforced": False,
        "status": access.get("status"),
    }

    state["readiness"] = _derive_readiness_state(runtime_data, routing_snapshot)

    warnings = list(runtime_data.get("warnings") or [])
    for warning in warnings:
        if isinstance(warning, Mapping):
            state["warnings"].append(dict(warning))

    errors = list(runtime_data.get("errors") or [])
    for error in errors:
        if isinstance(error, Mapping):
            state["errors"].append(dict(error))

    _store_namespace_startup_projection(app, state)


def _validate_project_access_route_surface(app: Flask) -> None:
    """Fail startup when an enabled, required Access route surface is incomplete."""
    state = _ensure_startup_state(app)
    routing = _safe_dict(state.get("routing"))
    access = _safe_dict(routing.get("projectAccess"))

    enabled = _safe_bool(access.get("enabled"), True)
    required = _safe_bool(access.get("required"), enabled)
    ready = _safe_bool(access.get("routeSurfaceReady"), False)

    if not enabled or not required or ready:
        return

    missing = list(access.get("missingRouteRules") or [])
    message = (
        "Required project-access route surface is incomplete. Missing routes: "
        + (", ".join(missing) if missing else "project_access Blueprint")
    )
    _append_error(
        app,
        message,
        code="project_access_route_surface_not_ready",
        details={
            "missingRouteRules": missing,
            "blueprintRegistered": access.get("blueprintRegistered"),
            "authzEnforced": False,
        },
    )
    state["readiness"]["startupReady"] = False
    _store_namespace_startup_projection(app, state)
    raise RuntimeError(message)


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
    state["readiness"] = _build_empty_readiness_state()
    state["routing"] = _build_routing_snapshot(app)
    state["projectAccess"] = {
        "checked": False,
        "ready": None,
        "required": False,
        "authzEnforced": False,
    }
    state["checks"] = {
        "paths": [],
        "files": [],
        "routes": [],
        "database": {},
        "models": {},
        "schema": {},
        "project_access": {},
        "routing": dict(state["routing"]),
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
    state["state_version"] = STARTUP_STATE_VERSION
    state["contract_version"] = STARTUP_CONTRACT_VERSION
    state["metadata"] = {
        "authzEnforced": False,
        "runtimeReadOnly": True,
        "routingSnapshot": dict(state["routing"]),
    }
    _store_namespace_startup_projection(app, state)

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

    readiness = _safe_dict(state.get("readiness"))
    if state["status"] == STATUS_FAILED:
        readiness["startupReady"] = False
    elif readiness.get("startupReady") is None:
        readiness["startupReady"] = state["status"] in {
            STATUS_COMPLETED,
            STATUS_WARNING,
        }
    state["readiness"] = readiness
    _store_namespace_startup_projection(app, state)

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
    state["routing"] = _build_routing_snapshot(app)
    state["checks"]["routing"] = dict(state["routing"])
    state["readiness"] = _derive_readiness_state({}, state["routing"])
    state["readiness"]["startupReady"] = None

    state["database"]["checked"] = False
    state["database"]["ok"] = None
    state["database"]["create_all_attempted"] = False
    state["database"]["create_all_ok"] = None
    state["database"]["runtimeDisabled"] = True

    state["seed"]["attempted"] = False
    state["seed"]["completed"] = False
    state["seed"]["runtimeDisabled"] = True

    _safe_log_info(app, "Startup hooks for `vectoplan-chunk` skipped: %s", reason)

    _store_namespace_startup_projection(app, state)

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
    state["readiness"] = _safe_dict(state.get("readiness"))
    state["readiness"]["startupReady"] = False

    error_message = f"Startup of `vectoplan-chunk` failed: {_safe_exception_message(exc)}"
    _append_error(
        app,
        error_message,
        code="startup_failed",
        details={"exceptionType": exc.__class__.__name__},
    )
    _safe_log_exception(app, error_message)

    _store_namespace_startup_projection(app, state)

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
        _validate_project_access_route_surface(app)

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
                "routingInitialized": completed_state.get("routing", {}).get("routingInitialized"),
                "projectAccessRouteSurfaceReady": completed_state.get("readiness", {}).get("projectAccessRouteSurfaceReady"),
                "schemaReady": completed_state.get("readiness", {}).get("schemaReady"),
                "projectOwnerColumnsReady": completed_state.get("readiness", {}).get("projectOwnerColumnsReady"),
                "projectAccessSchemaReady": completed_state.get("readiness", {}).get("projectAccessSchemaReady"),
                "defaultProjectOwnerReady": completed_state.get("readiness", {}).get("defaultProjectOwnerReady"),
                "defaultProjectRolesReady": completed_state.get("readiness", {}).get("defaultProjectRolesReady"),
                "defaultProjectOwnerAssignmentReady": completed_state.get("readiness", {}).get("defaultProjectOwnerAssignmentReady"),
                "defaultProjectAccessReady": completed_state.get("readiness", {}).get("defaultProjectAccessReady"),
                "projectAccessAuthzEnforced": False,
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
    """Return compact startup, routing and access-readiness summary."""
    state = _ensure_startup_state(app)

    runtime_checks_summary = _safe_dict(state.get("runtimeChecksSummary"))
    settings_summary = _safe_dict(state.get("settings"))
    readiness = _safe_dict(state.get("readiness"))
    routing = _safe_dict(state.get("routing"))
    route_access = _safe_dict(routing.get("projectAccess"))
    project_access = _safe_dict(state.get("projectAccess"))

    return {
        "stateVersion": state.get("state_version", STARTUP_STATE_VERSION),
        "contractVersion": state.get(
            "contract_version",
            STARTUP_CONTRACT_VERSION,
        ),
        "status": _safe_str(state.get("status"), "unknown"),
        "startedAt": state.get("started_at"),
        "completedAt": state.get("completed_at"),
        "runCount": _safe_int(state.get("run_count"), default=0, minimum=0),
        "strictMode": _safe_bool(state.get("strict_mode"), False),
        "startupReady": readiness.get("startupReady"),
        "warningCount": len(state.get("warnings", []) or []),
        "errorCount": len(state.get("errors", []) or []),
        "routeCount": _safe_int(
            state.get("route_summary", {}).get("count", 0),
            default=0,
            minimum=0,
        ),
        "requiredMissingRoutes": list(
            state.get("route_summary", {}).get("required_missing", []) or []
        ),
        "optionalMissingRoutes": list(
            state.get("route_summary", {}).get("optional_missing", []) or []
        ),
        "routing": {
            "initialized": routing.get("routingInitialized"),
            "registeredBlueprintNames": list(
                routing.get("registeredBlueprintNames") or []
            ),
            "projectAccessApiEnabled": route_access.get("enabled"),
            "projectAccessRoutesRequired": route_access.get("required"),
            "projectAccessBlueprintRegistered": route_access.get(
                "blueprintRegistered"
            ),
            "projectAccessRouteSurfaceReady": route_access.get(
                "routeSurfaceReady"
            ),
            "projectAccessMissingRouteRules": list(
                route_access.get("missingRouteRules") or []
            ),
            "projectAccessAuthzEnforced": False,
        },
        "readiness": dict(readiness),
        "projectAccess": dict(project_access),
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
        "authzEnforced": False,
        "runtimeReadOnly": True,
    }


def get_startup_readiness(app: Flask) -> dict[str, Any]:
    """Return a defensive copy of the normalized startup readiness projection."""
    state = _ensure_startup_state(app)
    return _safe_deepcopy(_safe_dict(state.get("readiness")))


def get_startup_routing_summary(app: Flask) -> dict[str, Any]:
    """Return bounded routing and project-access route-surface diagnostics."""
    state = _ensure_startup_state(app)
    return _safe_deepcopy(_safe_dict(state.get("routing")))


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
    "STARTUP_STATE_VERSION",
    "STARTUP_CONTRACT_VERSION",
    "PROJECT_ACCESS_BLUEPRINT_NAME",
    "PROJECT_ACCESS_CORE_ROUTE_RULES",
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
    "get_startup_readiness",
    "get_startup_routing_summary",
    "get_runtime_checks_summary",
    "get_settings_summary",
]