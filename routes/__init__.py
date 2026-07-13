# services/vectoplan-chunk/routes/__init__.py
"""
Central Blueprint registration for the VECTOPLAN Chunk Service.

Responsibilities:
- define known route modules
- import route modules defensively
- resolve Flask Blueprint objects
- register Blueprints exactly once per Flask app
- store routing metadata for diagnostics and health/status routes

Important boundaries:
- no business logic
- no chunk generation
- no DB writes
- no world-discovery logic
- no HTML generation
- only route wiring and defensive registration

Productive project-scoped API:
- routes.projects:projects_bp
- routes.worlds:worlds_bp
- routes.blocks:blocks_bp
- routes.chunks:chunks_bp
- routes.commands:commands_bp

Debug/development API:
- routes.world_test:world_test_bp
- routes.earth_debug:earth_debug_bp

Legacy compatibility:
- routes.editor:editor_bp, optional

Core route examples:
- GET  /projects/dev-project/bootstrap
- GET  /projects/dev-project/worlds
- POST /projects/dev-project/worlds
- GET  /projects/dev-project/worlds/world_spawn
- GET  /projects/dev-project/worlds/world_spawn/blocks
- GET  /projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0
- POST /projects/dev-project/worlds/world_spawn/chunks/batch
- POST /projects/dev-project/worlds/world_spawn/commands

Architecture semantics:
- Project contains Universes.
- Universe contains concrete WorldInstances.
- world_spawn is the concrete editable project world.
- flat is the provider/template world.
- ChunkSnapshot is load-truth for materialized chunks.
- ChunkEvent is append-only historical truth.
"""

from __future__ import annotations

import importlib
import traceback
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Dict, Iterable, Optional, Tuple

from flask import Blueprint, Flask


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

ROUTES_REGISTRY_VERSION = "0.5.0"

DEFAULT_EXTENSION_NAMESPACE = "vectoplan_chunk"
DEFAULT_ROUTING_STATE_KEY = "routing"

LEGACY_EXTENSION_NAMESPACES = (
    "vectoplan_editor",
)

CONFIG_EXTENSION_NAMESPACE_KEYS = (
    "VECTOPLAN_EXTENSION_NAMESPACE",
    "SERVICE_EXTENSION_NAMESPACE",
    "ROUTES_EXTENSION_NAMESPACE",
)

PRODUCTIVE_BLUEPRINT_MODULES = (
    "routes.projects",
    "routes.worlds",
    "routes.blocks",
    "routes.chunks",
    "routes.commands",
)

DEBUG_BLUEPRINT_MODULES = (
    "routes.world_test",
    "routes.earth_debug",
)

OPTIONAL_LEGACY_BLUEPRINT_MODULES = (
    "routes.editor",
)


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BlueprintSpec:
    """
    Blueprint loading and registration specification.
    """

    module_name: str
    attribute_name: str
    url_prefix: str | None = None
    required: bool = True
    description: str = ""
    category: str = "api"


@dataclass(frozen=True, slots=True)
class BlueprintRegistrationRecord:
    """
    Stored result of one Blueprint registration attempt.
    """

    module_name: str
    attribute_name: str
    blueprint_name: str | None
    url_prefix: str | None
    required: bool
    category: str
    status: str
    skipped: bool = False
    reason: str | None = None
    error_type: str | None = None
    error: str | None = None
    traceback_text: str | None = None

    def to_dict(self, *, include_traceback: bool = False) -> dict[str, Any]:
        return {
            "moduleName": self.module_name,
            "attributeName": self.attribute_name,
            "blueprintName": self.blueprint_name,
            "urlPrefix": self.url_prefix,
            "required": self.required,
            "category": self.category,
            "status": self.status,
            "skipped": self.skipped,
            "reason": self.reason,
            "errorType": self.error_type,
            "error": self.error,
            "traceback": self.traceback_text if include_traceback else None,
        }


# -----------------------------------------------------------------------------
# Safe primitive helpers
# -----------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """Convert value defensively to stripped string."""
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


def _safe_bool(value: Any, *, default: bool = False) -> bool:
    """Convert value defensively to bool."""
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    if value is None:
        return default

    text = _safe_str(value).lower()

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False

    return default


def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = type(exc).__name__

    return message or type(exc).__name__


def _safe_get_logger(app: Flask):
    """Return Flask logger defensively."""
    try:
        return app.logger
    except Exception:
        return None


def _safe_log_debug(app: Flask, message: str) -> None:
    logger = _safe_get_logger(app)
    if logger is None:
        return

    try:
        logger.debug(message)
    except Exception:
        pass


def _safe_log_info(app: Flask, message: str) -> None:
    logger = _safe_get_logger(app)
    if logger is None:
        return

    try:
        logger.info(message)
    except Exception:
        pass


def _safe_log_warning(app: Flask, message: str) -> None:
    logger = _safe_get_logger(app)
    if logger is None:
        return

    try:
        logger.warning(message)
    except Exception:
        pass


def _safe_log_error(app: Flask, message: str) -> None:
    logger = _safe_get_logger(app)
    if logger is None:
        return

    try:
        logger.error(message)
    except Exception:
        pass


def _safe_get_config_value(app: Flask, key: str, default: Any = None) -> Any:
    """Read Flask config value defensively."""
    try:
        return app.config.get(key, default)
    except Exception:
        return default


def _get_config_bool(app: Flask, key: str, default: bool = False) -> bool:
    """Read bool config value defensively."""
    return _safe_bool(_safe_get_config_value(app, key, default), default=default)


def _is_flask_app(app: object) -> bool:
    """
    Check defensively whether object can be used like a Flask app.
    """
    if isinstance(app, Flask):
        return True

    required_attributes = ("register_blueprint", "blueprints", "extensions")
    try:
        return all(hasattr(app, attribute_name) for attribute_name in required_attributes)
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Namespace / routing-state helpers
# -----------------------------------------------------------------------------

def _get_extension_namespace(app: Flask) -> str:
    """
    Resolve extension namespace for routing metadata.

    Priority:
    1. explicit config values
    2. service name
    3. DEFAULT_EXTENSION_NAMESPACE
    """
    for key in CONFIG_EXTENSION_NAMESPACE_KEYS:
        value = _safe_str(_safe_get_config_value(app, key, ""))
        if value:
            return value

    service_name = (
        _safe_str(_safe_get_config_value(app, "SERVICE_NAME", ""))
        or _safe_str(_safe_get_config_value(app, "serviceName", ""))
        or _safe_str(_safe_get_config_value(app, "VECTOPLAN_SERVICE_NAME", ""))
    )

    if service_name:
        return (
            service_name
            .replace("-", "_")
            .replace(".", "_")
            .replace(" ", "_")
            .lower()
        )

    return DEFAULT_EXTENSION_NAMESPACE


def _ensure_extension_namespace(app: Flask) -> dict[str, Any]:
    """
    Ensure primary extension namespace exists.

    This function does not replace an existing namespace object.
    """
    namespace_name = _get_extension_namespace(app)

    try:
        app.extensions.setdefault(namespace_name, {})
        namespace = app.extensions[namespace_name]

        if not isinstance(namespace, dict):
            raise TypeError(f"app.extensions['{namespace_name}'] is not a dictionary.")

        namespace.setdefault("namespace", namespace_name)

        for legacy_namespace in LEGACY_EXTENSION_NAMESPACES:
            try:
                app.extensions.setdefault(legacy_namespace, namespace)
            except Exception:
                pass

        return namespace

    except Exception as exc:
        raise RuntimeError(
            f"Could not initialize extension namespace '{namespace_name}'."
        ) from exc


def _ensure_routing_state(app: Flask) -> dict[str, Any]:
    """
    Ensure routing metadata state under the primary extension namespace.
    """
    namespace = _ensure_extension_namespace(app)

    routing = namespace.get(DEFAULT_ROUTING_STATE_KEY)
    if not isinstance(routing, dict):
        routing = {}
        namespace[DEFAULT_ROUTING_STATE_KEY] = routing

    routing.setdefault("routeModule", "routes")
    routing.setdefault("routesRegistryVersion", ROUTES_REGISTRY_VERSION)
    routing.setdefault("routingInitialized", False)
    routing.setdefault("projectScopedApiEnabled", True)
    routing.setdefault("worldStateApiEnabled", True)
    routing.setdefault("snapshotBackedChunksEnabled", True)
    routing.setdefault("commandWriteApiEnabled", True)
    routing.setdefault("worldTestDebugRouteEnabled", False)
    routing.setdefault("earthDebugRouteEnabled", False)
    routing.setdefault("legacyRoutesEnabled", False)
    routing.setdefault("registeredBlueprintNames", set())
    routing.setdefault("records", [])
    routing.setdefault("errors", [])
    routing.setdefault("successes", [])
    routing.setdefault("skipped", [])

    if isinstance(routing.get("registeredBlueprintNames"), list):
        routing["registeredBlueprintNames"] = {
            str(item)
            for item in routing["registeredBlueprintNames"]
        }

    if not isinstance(routing.get("registeredBlueprintNames"), set):
        routing["registeredBlueprintNames"] = set()

    for list_key in ("records", "errors", "successes", "skipped"):
        if not isinstance(routing.get(list_key), list):
            routing[list_key] = []

    return routing


def _ensure_blueprint_tracking(app: Flask) -> set[str]:
    """Return registered blueprint tracking set."""
    routing = _ensure_routing_state(app)
    tracked = routing.get("registeredBlueprintNames")

    if isinstance(tracked, set):
        return tracked

    if isinstance(tracked, (list, tuple)):
        restored = {str(item) for item in tracked}
        routing["registeredBlueprintNames"] = restored
        return restored

    routing["registeredBlueprintNames"] = set()
    return routing["registeredBlueprintNames"]


def _spec_to_dict(spec: BlueprintSpec) -> dict[str, Any]:
    """Serialize BlueprintSpec."""
    return {
        "moduleName": spec.module_name,
        "attributeName": spec.attribute_name,
        "urlPrefix": spec.url_prefix,
        "required": spec.required,
        "description": spec.description,
        "category": spec.category,
    }


def _record_registration(
    app: Flask,
    record: BlueprintRegistrationRecord,
) -> None:
    """Store one registration record in routing state."""
    try:
        routing = _ensure_routing_state(app)
        record_dict = record.to_dict(include_traceback=False)
        routing["records"].append(record_dict)

        if record.status == "registered" or record.status == "already_registered":
            routing["successes"].append(record_dict)
        elif record.status == "skipped":
            routing["skipped"].append(record_dict)
        elif record.status == "error":
            routing["errors"].append(record_dict)

    except Exception:
        pass


# -----------------------------------------------------------------------------
# Config gate helpers
# -----------------------------------------------------------------------------

def _should_register_dev_routes(app: Flask) -> bool:
    """
    Determine whether development/debug routes should be registered.

    Default: True so /world-test remains available during the current slice.
    """
    return _get_config_bool(
        app,
        "VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES",
        default=True,
    )


def _should_register_legacy_routes(app: Flask) -> bool:
    """
    Determine whether optional legacy routes should be registered.

    Default: True, but legacy routes remain optional.
    """
    return _get_config_bool(
        app,
        "VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES",
        default=True,
    )


def _should_skip_spec(app: Flask, spec: BlueprintSpec) -> tuple[bool, str | None]:
    """
    Check whether a blueprint spec should be skipped due to config.
    """
    if spec.category == "debug" and not _should_register_dev_routes(app):
        return True, "dev_routes_disabled"

    if spec.category == "legacy" and not _should_register_legacy_routes(app):
        return True, "legacy_routes_disabled"

    return False, None


# -----------------------------------------------------------------------------
# Blueprint specs
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_blueprint_specs() -> tuple[BlueprintSpec, ...]:
    """
    Return Blueprint registration specs.

    Productive route order is intentional:
    - projects first
    - worlds second
    - blocks/chunks read APIs
    - commands write API
    - debug/legacy last
    """
    return (
        BlueprintSpec(
            module_name="routes.projects",
            attribute_name="projects_bp",
            url_prefix=None,
            required=True,
            description="Project creation, listing, patching, deletion and bootstrap routes.",
            category="productive",
        ),
        BlueprintSpec(
            module_name="routes.worlds",
            attribute_name="worlds_bp",
            url_prefix=None,
            required=True,
            description="Project-scoped world instance metadata and CRUD routes.",
            category="productive",
        ),
        BlueprintSpec(
            module_name="routes.blocks",
            attribute_name="blocks_bp",
            url_prefix=None,
            required=True,
            description="Project-scoped block registry and palette routes.",
            category="productive",
        ),
        BlueprintSpec(
            module_name="routes.chunks",
            attribute_name="chunks_bp",
            url_prefix=None,
            required=True,
            description="Project-scoped chunk load routes using ChunkSnapshot or generated fallback.",
            category="productive",
        ),
        BlueprintSpec(
            module_name="routes.commands",
            attribute_name="commands_bp",
            url_prefix=None,
            required=True,
            description="Project-scoped chunk command write routes for SetBlock, RemoveBlock and objects.",
            category="productive",
        ),
        BlueprintSpec(
            module_name="routes.world_test",
            attribute_name="world_test_bp",
            url_prefix=None,
            required=False,
            description="World discovery and provider chunk-generation debug route.",
            category="debug",
        ),
        BlueprintSpec(
            module_name="routes.earth_debug",
            attribute_name="earth_debug_bp",
            url_prefix=None,
            required=False,
            description=(
                "Temporary Earth-v1 provider, coordinate conversion, spawn "
                "and chunk-generation debug route."
            ),
            category="debug",
        ),
        BlueprintSpec(
            module_name="routes.editor",
            attribute_name="editor_bp",
            url_prefix=None,
            required=False,
            description="Optional legacy editor blueprint from copied Flask service template.",
            category="legacy",
        ),
    )


@lru_cache(maxsize=64)
def _import_module(module_name: str):
    """
    Import module with cache.
    """
    return importlib.import_module(module_name)


def _resolve_blueprint(spec: BlueprintSpec) -> Blueprint:
    """
    Resolve Blueprint object from BlueprintSpec.
    """
    try:
        module = _import_module(spec.module_name)
    except Exception as exc:
        raise RuntimeError(
            f"Route module '{spec.module_name}' could not be imported."
        ) from exc

    try:
        candidate = getattr(module, spec.attribute_name)
    except AttributeError as exc:
        raise RuntimeError(
            f"Route module '{spec.module_name}' is missing expected attribute "
            f"'{spec.attribute_name}'."
        ) from exc

    if not isinstance(candidate, Blueprint):
        raise RuntimeError(
            f"Attribute '{spec.attribute_name}' from '{spec.module_name}' "
            "is not a Flask Blueprint."
        )

    return candidate


def _register_single_blueprint(
    app: Flask,
    blueprint: Blueprint,
    url_prefix: str | None = None,
) -> tuple[bool, str]:
    """
    Register one Blueprint defensively.

    Returns:
        (success, reason)
    """
    blueprint_name = getattr(blueprint, "name", None)

    if not blueprint_name or not isinstance(blueprint_name, str):
        raise RuntimeError("Cannot register a Blueprint without a valid name.")

    tracked_names = _ensure_blueprint_tracking(app)

    if blueprint_name in tracked_names:
        _safe_log_debug(
            app,
            f"Blueprint '{blueprint_name}' already tracked; skipping registration.",
        )
        return True, "already_tracked"

    try:
        if blueprint_name in app.blueprints:
            tracked_names.add(blueprint_name)
            _safe_log_debug(
                app,
                f"Blueprint '{blueprint_name}' already registered on app; added to tracking.",
            )
            return True, "already_registered_on_app"
    except Exception:
        pass

    try:
        if url_prefix:
            app.register_blueprint(blueprint, url_prefix=url_prefix)
        else:
            app.register_blueprint(blueprint)
    except Exception as exc:
        raise RuntimeError(
            f"Blueprint '{blueprint_name}' could not be registered."
        ) from exc

    tracked_names.add(blueprint_name)
    _safe_log_info(app, f"Blueprint '{blueprint_name}' registered.")
    return True, "registered"


def _register_spec(app: Flask, spec: BlueprintSpec) -> None:
    """
    Register one BlueprintSpec.

    Required Blueprints fail app startup.
    Optional Blueprints are logged and skipped on failure.
    """
    skip, skip_reason = _should_skip_spec(app, spec)

    if skip:
        _safe_log_info(
            app,
            f"Blueprint '{spec.module_name}:{spec.attribute_name}' skipped: {skip_reason}",
        )
        _record_registration(
            app,
            BlueprintRegistrationRecord(
                module_name=spec.module_name,
                attribute_name=spec.attribute_name,
                blueprint_name=None,
                url_prefix=spec.url_prefix,
                required=spec.required,
                category=spec.category,
                status="skipped",
                skipped=True,
                reason=skip_reason,
            ),
        )
        return

    try:
        blueprint = _resolve_blueprint(spec)
        _success, reason = _register_single_blueprint(
            app=app,
            blueprint=blueprint,
            url_prefix=spec.url_prefix,
        )

        status = "registered" if reason == "registered" else "already_registered"

        _record_registration(
            app,
            BlueprintRegistrationRecord(
                module_name=spec.module_name,
                attribute_name=spec.attribute_name,
                blueprint_name=getattr(blueprint, "name", None),
                url_prefix=spec.url_prefix,
                required=spec.required,
                category=spec.category,
                status=status,
                skipped=reason != "registered",
                reason=reason,
            ),
        )

    except Exception as exc:
        traceback_text = traceback.format_exc()

        _record_registration(
            app,
            BlueprintRegistrationRecord(
                module_name=spec.module_name,
                attribute_name=spec.attribute_name,
                blueprint_name=None,
                url_prefix=spec.url_prefix,
                required=spec.required,
                category=spec.category,
                status="error",
                skipped=False,
                reason=None,
                error_type=type(exc).__name__,
                error=_safe_exception_message(exc),
                traceback_text=traceback_text,
            ),
        )

        if spec.required:
            _safe_log_error(
                app,
                f"Required Blueprint '{spec.module_name}:{spec.attribute_name}' "
                f"could not be registered: {_safe_exception_message(exc)}",
            )
            raise

        _safe_log_warning(
            app,
            f"Optional Blueprint '{spec.module_name}:{spec.attribute_name}' "
            f"could not be registered and was skipped: {_safe_exception_message(exc)}",
        )


def _store_registration_metadata(app: Flask) -> None:
    """
    Store final routing metadata.
    """
    routing = _ensure_routing_state(app)

    try:
        tracked_names = sorted(_ensure_blueprint_tracking(app))
        specs = get_blueprint_specs()

        routing["routeModule"] = "routes"
        routing["routesRegistryVersion"] = ROUTES_REGISTRY_VERSION
        routing["blueprintSpecs"] = [_spec_to_dict(spec) for spec in specs]
        routing["registeredBlueprintNamesList"] = tracked_names
        routing["registeredBlueprintCount"] = len(tracked_names)
        routing["blueprintRegistrationErrorCount"] = len(routing.get("errors", []) or [])
        routing["blueprintRegistrationSuccessCount"] = len(routing.get("successes", []) or [])
        routing["blueprintRegistrationSkippedCount"] = len(routing.get("skipped", []) or [])
        routing["productiveBlueprintModules"] = list(PRODUCTIVE_BLUEPRINT_MODULES)
        routing["debugBlueprintModules"] = list(DEBUG_BLUEPRINT_MODULES)
        routing["optionalLegacyBlueprintModules"] = list(OPTIONAL_LEGACY_BLUEPRINT_MODULES)
        routing["routingInitialized"] = True
        routing["projectScopedApiEnabled"] = True
        routing["worldStateApiEnabled"] = True
        routing["snapshotBackedChunksEnabled"] = True
        routing["commandWriteApiEnabled"] = "commands" in tracked_names
        routing["worldTestDebugRouteEnabled"] = "world_test" in tracked_names
        routing["earthDebugRouteEnabled"] = "earth_debug" in tracked_names
        routing["legacyRoutesEnabled"] = "editor" in tracked_names

        # Also expose selected routing values at namespace top-level for older
        # startup/status code that reads app.extensions[namespace] directly.
        namespace = _ensure_extension_namespace(app)
        namespace["routes_registry_version"] = ROUTES_REGISTRY_VERSION
        namespace["routing_initialized"] = True
        namespace["registered_blueprint_names"] = set(tracked_names)
        namespace["registered_blueprint_names_list"] = tracked_names
        namespace["registered_blueprint_count"] = len(tracked_names)
        namespace["project_scoped_api_enabled"] = True
        namespace["world_state_api_enabled"] = True
        namespace["snapshot_backed_chunks_enabled"] = True
        namespace["command_write_api_enabled"] = routing["commandWriteApiEnabled"]
        namespace["earth_debug_route_enabled"] = routing["earthDebugRouteEnabled"]

    except Exception as exc:
        raise RuntimeError("Routing metadata could not be stored.") from exc


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def register_blueprints(app: Flask) -> Flask:
    """
    Register all configured Blueprints on the Flask app.

    Flow:
    1. validate app object
    2. ensure routing state
    3. load Blueprint specs
    4. import and register each Blueprint
    5. store routing metadata

    Returns the same Flask app.
    """
    if not _is_flask_app(app):
        raise TypeError(
            "register_blueprints(app) expects a Flask app or compatible object."
        )

    _ensure_routing_state(app)

    specs = get_blueprint_specs()

    if not specs:
        _safe_log_warning(app, "No Blueprint specs found; no routes registered.")
        _store_registration_metadata(app)
        return app

    for spec in specs:
        _register_spec(app, spec)

    _store_registration_metadata(app)
    return app


def get_registered_blueprint_names(app: Flask) -> list[str]:
    """
    Return tracked Blueprint names.
    """
    tracked_names = _ensure_blueprint_tracking(app)

    try:
        return sorted(tracked_names)
    except Exception:
        return list(tracked_names)


def get_blueprint_registration_records(
    app: Flask,
    *,
    include_tracebacks: bool = False,
) -> list[dict[str, Any]]:
    """
    Return all registration records.
    """
    routing = _ensure_routing_state(app)
    records = routing.get("records", [])

    if not isinstance(records, list):
        return []

    if include_tracebacks:
        return list(records)

    result: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        item = dict(record)
        item.pop("traceback", None)
        item.pop("tracebackText", None)
        result.append(item)

    return result


def get_blueprint_registration_errors(app: Flask) -> list[dict[str, Any]]:
    """
    Return stored Blueprint registration errors.
    """
    routing = _ensure_routing_state(app)
    errors = routing.get("errors", [])

    if isinstance(errors, list):
        return list(errors)

    return []


def get_blueprint_registration_successes(app: Flask) -> list[dict[str, Any]]:
    """
    Return stored Blueprint registration successes.
    """
    routing = _ensure_routing_state(app)
    successes = routing.get("successes", [])

    if isinstance(successes, list):
        return list(successes)

    return []


def get_blueprint_registration_skipped(app: Flask) -> list[dict[str, Any]]:
    """
    Return stored skipped Blueprint records.
    """
    routing = _ensure_routing_state(app)
    skipped = routing.get("skipped", [])

    if isinstance(skipped, list):
        return list(skipped)

    return []


def get_routing_metadata(app: Flask) -> dict[str, Any]:
    """
    Return routing metadata from app extension state.
    """
    try:
        routing = _ensure_routing_state(app)

        return {
            "namespace": _get_extension_namespace(app),
            "routesRegistryVersion": routing.get("routesRegistryVersion"),
            "routeModule": routing.get("routeModule"),
            "routingInitialized": routing.get("routingInitialized"),
            "projectScopedApiEnabled": routing.get("projectScopedApiEnabled"),
            "worldStateApiEnabled": routing.get("worldStateApiEnabled"),
            "snapshotBackedChunksEnabled": routing.get("snapshotBackedChunksEnabled"),
            "commandWriteApiEnabled": routing.get("commandWriteApiEnabled"),
            "worldTestDebugRouteEnabled": routing.get("worldTestDebugRouteEnabled"),
            "earthDebugRouteEnabled": routing.get("earthDebugRouteEnabled"),
            "legacyRoutesEnabled": routing.get("legacyRoutesEnabled"),
            "registeredBlueprintNames": get_registered_blueprint_names(app),
            "registeredBlueprintCount": routing.get("registeredBlueprintCount"),
            "blueprintSpecs": routing.get("blueprintSpecs", []),
            "records": get_blueprint_registration_records(app),
            "errors": get_blueprint_registration_errors(app),
            "successes": get_blueprint_registration_successes(app),
            "skipped": get_blueprint_registration_skipped(app),
            "productiveBlueprintModules": routing.get("productiveBlueprintModules", []),
            "debugBlueprintModules": routing.get("debugBlueprintModules", []),
            "optionalLegacyBlueprintModules": routing.get("optionalLegacyBlueprintModules", []),
        }

    except Exception as exc:
        return {
            "routingInitialized": False,
            "error": "routing_metadata_unavailable",
            "message": _safe_exception_message(exc),
        }


def iter_blueprint_specs() -> tuple[BlueprintSpec, ...]:
    """
    Return Blueprint specs.
    """
    return get_blueprint_specs()


def reset_route_import_cache() -> None:
    """
    Clear route import and spec caches.

    Intended for tests/development tooling.
    """
    get_blueprint_specs.cache_clear()
    _import_module.cache_clear()


__all__ = [
    "BlueprintSpec",
    "BlueprintRegistrationRecord",
    "ROUTES_REGISTRY_VERSION",
    "PRODUCTIVE_BLUEPRINT_MODULES",
    "DEBUG_BLUEPRINT_MODULES",
    "OPTIONAL_LEGACY_BLUEPRINT_MODULES",
    "register_blueprints",
    "get_registered_blueprint_names",
    "get_blueprint_registration_records",
    "get_blueprint_registration_errors",
    "get_blueprint_registration_successes",
    "get_blueprint_registration_skipped",
    "get_routing_metadata",
    "iter_blueprint_specs",
    "reset_route_import_cache",
]