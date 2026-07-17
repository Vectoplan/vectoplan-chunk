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
- routes.project_access:project_access_bp
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
- GET  /projects/dev-project/access
- PUT  /projects/dev-project/access/initialize
- GET  /projects/dev-project/roles
- GET  /projects/dev-project/groups
- GET  /projects/dev-project/assignments
- GET  /projects/dev-project/worlds
- POST /projects/dev-project/worlds
- GET  /projects/dev-project/worlds/world_spawn
- GET  /projects/dev-project/worlds/world_spawn/blocks
- GET  /projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0
- POST /projects/dev-project/worlds/world_spawn/chunks/batch
- POST /projects/dev-project/worlds/world_spawn/commands

Architecture semantics:
- Project contains Universes.
- Project owns project-scoped roles, groups, memberships and assignments.
- Project-access rows store external user ids without cross-service user foreign keys.
- Project-access routes prepare storage contracts but do not enforce authorization.
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
from typing import Any

from flask import Blueprint, Flask


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

ROUTES_REGISTRY_VERSION = "0.6.0"

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

PROJECT_ACCESS_BLUEPRINT_MODULE = "routes.project_access"
PROJECT_ACCESS_BLUEPRINT_ATTRIBUTE = "project_access_bp"
PROJECT_ACCESS_BLUEPRINT_NAME = "project_access"

CONFIG_ENABLE_PROJECT_ACCESS_ROUTES = (
    "VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES",
    "VECTOPLAN_CHUNK_PROJECT_ACCESS_ROUTES_ENABLED",
)

CONFIG_REQUIRE_PROJECT_ACCESS_ROUTES = (
    "VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_ROUTES",
    "VECTOPLAN_CHUNK_PROJECT_ACCESS_ROUTES_REQUIRED",
)

PROJECT_ACCESS_CORE_ROUTE_RULES = (
    "/project-access/_status",
    "/projects/<project_id>/access",
    "/projects/<project_id>/access/initialize",
    "/projects/<project_id>/roles",
    "/projects/<project_id>/groups",
    "/projects/<project_id>/assignments",
)

PRODUCTIVE_BLUEPRINT_MODULES = (
    "routes.projects",
    PROJECT_ACCESS_BLUEPRINT_MODULE,
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
    routing.setdefault("projectAccessApiEnabled", True)
    routing.setdefault("projectAccessRoutesRequired", True)
    routing.setdefault("projectAccessBlueprintRegistered", False)
    routing.setdefault("projectAccessRouteSurfaceReady", False)
    routing.setdefault("projectAccessAuthzEnforced", False)
    routing.setdefault("projectAccessCoreRouteRules", list(PROJECT_ACCESS_CORE_ROUTE_RULES))
    routing.setdefault("projectAccessMissingRouteRules", [])
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


def _first_config_bool(
    app: Flask,
    keys: tuple[str, ...],
    *,
    default: bool,
) -> bool:
    """Return the first explicitly configured boolean from ``keys``."""
    for key in keys:
        value = _safe_get_config_value(app, key, None)
        if value is not None:
            return _safe_bool(value, default=default)
    return default


def _should_register_project_access_routes(app: Flask) -> bool:
    """Return whether the prepared project-access route surface is enabled."""
    return _first_config_bool(
        app,
        CONFIG_ENABLE_PROJECT_ACCESS_ROUTES,
        default=True,
    )


def _should_require_project_access_routes(app: Flask) -> bool:
    """Return whether missing project-access routes must fail app startup."""
    if not _should_register_project_access_routes(app):
        return False

    return _first_config_bool(
        app,
        CONFIG_REQUIRE_PROJECT_ACCESS_ROUTES,
        default=True,
    )


def _is_project_access_spec(spec: BlueprintSpec) -> bool:
    """Return whether a specification describes the project-access Blueprint."""
    return bool(
        spec.module_name == PROJECT_ACCESS_BLUEPRINT_MODULE
        and spec.attribute_name == PROJECT_ACCESS_BLUEPRINT_ATTRIBUTE
    )


def _effective_spec_required(app: Flask, spec: BlueprintSpec) -> bool:
    """Resolve the effective required flag for a Blueprint specification."""
    if _is_project_access_spec(spec):
        return _should_require_project_access_routes(app)
    return bool(spec.required)


def _should_skip_spec(app: Flask, spec: BlueprintSpec) -> tuple[bool, str | None]:
    """
    Check whether a blueprint spec should be skipped due to config.
    """
    if _is_project_access_spec(spec) and not _should_register_project_access_routes(app):
        return True, "project_access_routes_disabled"

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
    - project access immediately after projects
    - worlds next
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
            module_name=PROJECT_ACCESS_BLUEPRINT_MODULE,
            attribute_name=PROJECT_ACCESS_BLUEPRINT_ATTRIBUTE,
            url_prefix=None,
            required=True,
            description=(
                "Prepared project-scoped roles, groups, memberships and role "
                "assignment routes without authorization enforcement."
            ),
            category="productive-access",
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


def _validate_blueprint_specs(
    specs: tuple[BlueprintSpec, ...],
) -> dict[str, Any]:
    """Validate the immutable Blueprint registry contract without importing routes."""
    errors: list[str] = []
    identities: set[tuple[str, str]] = set()
    project_access_count = 0

    for index, spec in enumerate(specs):
        module_name = _safe_str(spec.module_name)
        attribute_name = _safe_str(spec.attribute_name)

        if not module_name:
            errors.append(f"spec[{index}] has no module_name")
        if not attribute_name:
            errors.append(f"spec[{index}] has no attribute_name")

        identity = (module_name, attribute_name)
        if identity in identities:
            errors.append(
                "duplicate Blueprint specification: "
                f"{module_name}:{attribute_name}"
            )
        identities.add(identity)

        if _is_project_access_spec(spec):
            project_access_count += 1

    if project_access_count != 1:
        errors.append(
            "expected exactly one project-access Blueprint specification; "
            f"found {project_access_count}"
        )

    if errors:
        raise RuntimeError("Invalid Blueprint registry: " + " | ".join(errors))

    return {
        "ok": True,
        "specCount": len(specs),
        "uniqueIdentityCount": len(identities),
        "projectAccessSpecCount": project_access_count,
    }


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
    effective_required = _effective_spec_required(app, spec)
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
                required=effective_required,
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
                required=effective_required,
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
                required=effective_required,
                category=spec.category,
                status="error",
                skipped=False,
                reason=None,
                error_type=type(exc).__name__,
                error=_safe_exception_message(exc),
                traceback_text=traceback_text,
            ),
        )

        if effective_required:
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


def _collect_route_rules(app: Flask) -> set[str]:
    """Collect registered Flask route rules without invoking any view functions."""
    try:
        return {str(rule.rule) for rule in app.url_map.iter_rules()}
    except Exception:
        return set()


def _build_project_access_route_surface_status(app: Flask) -> dict[str, Any]:
    """Build a read-only contract status for the project-access route surface."""
    enabled = _should_register_project_access_routes(app)
    required = _should_require_project_access_routes(app)

    try:
        blueprint_registered = PROJECT_ACCESS_BLUEPRINT_NAME in app.blueprints
    except Exception:
        blueprint_registered = False

    rules = _collect_route_rules(app)
    present = [rule for rule in PROJECT_ACCESS_CORE_ROUTE_RULES if rule in rules]
    missing = [rule for rule in PROJECT_ACCESS_CORE_ROUTE_RULES if rule not in rules]

    ready = bool(enabled and blueprint_registered and not missing)

    return {
        "enabled": enabled,
        "required": required,
        "authzEnforced": False,
        "blueprintName": PROJECT_ACCESS_BLUEPRINT_NAME,
        "blueprintRegistered": blueprint_registered,
        "coreRouteRules": list(PROJECT_ACCESS_CORE_ROUTE_RULES),
        "presentRouteRules": present,
        "missingRouteRules": missing,
        "routeSurfaceReady": ready,
        "status": (
            "disabled"
            if not enabled
            else "ready"
            if ready
            else "missing"
        ),
    }


def _store_registration_metadata(app: Flask) -> None:
    """
    Store final routing metadata and verify the project-access route contract.
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
        project_access_status = _build_project_access_route_surface_status(app)

        routing["productiveBlueprintModules"] = list(PRODUCTIVE_BLUEPRINT_MODULES)
        routing["debugBlueprintModules"] = list(DEBUG_BLUEPRINT_MODULES)
        routing["optionalLegacyBlueprintModules"] = list(OPTIONAL_LEGACY_BLUEPRINT_MODULES)
        routing["routingInitialized"] = False
        routing["routingError"] = None
        routing["projectScopedApiEnabled"] = True
        routing["projectAccessApiEnabled"] = project_access_status["enabled"]
        routing["projectAccessRoutesRequired"] = project_access_status["required"]
        routing["projectAccessBlueprintRegistered"] = project_access_status["blueprintRegistered"]
        routing["projectAccessRouteSurfaceReady"] = project_access_status["routeSurfaceReady"]
        routing["projectAccessAuthzEnforced"] = False
        routing["projectAccessCoreRouteRules"] = project_access_status["coreRouteRules"]
        routing["projectAccessMissingRouteRules"] = project_access_status["missingRouteRules"]
        routing["projectAccess"] = project_access_status
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
        namespace["routing_initialized"] = False
        namespace["registered_blueprint_names"] = set(tracked_names)
        namespace["registered_blueprint_names_list"] = tracked_names
        namespace["registered_blueprint_count"] = len(tracked_names)
        namespace["project_scoped_api_enabled"] = True
        namespace["project_access_api_enabled"] = project_access_status["enabled"]
        namespace["project_access_routes_required"] = project_access_status["required"]
        namespace["project_access_blueprint_registered"] = project_access_status["blueprintRegistered"]
        namespace["project_access_route_surface_ready"] = project_access_status["routeSurfaceReady"]
        namespace["project_access_authz_enforced"] = False
        namespace["project_access_route_status"] = dict(project_access_status)
        namespace["world_state_api_enabled"] = True
        namespace["snapshot_backed_chunks_enabled"] = True
        namespace["command_write_api_enabled"] = routing["commandWriteApiEnabled"]
        namespace["earth_debug_route_enabled"] = routing["earthDebugRouteEnabled"]

        if (
            project_access_status["enabled"]
            and project_access_status["required"]
            and not project_access_status["routeSurfaceReady"]
        ):
            missing = ", ".join(project_access_status["missingRouteRules"]) or "<blueprint>"
            raise RuntimeError(
                "Required project-access Blueprint route surface is incomplete: "
                f"{missing}."
            )

        routing["routingInitialized"] = True
        routing["routingError"] = None
        namespace["routing_initialized"] = True

    except Exception as exc:
        try:
            routing["routingInitialized"] = False
            routing["routingError"] = {
                "type": type(exc).__name__,
                "message": _safe_exception_message(exc),
            }
        except Exception:
            pass
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
    spec_validation = _validate_blueprint_specs(specs)

    try:
        routing = _ensure_routing_state(app)
        routing["specValidation"] = spec_validation
    except Exception:
        pass

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
            "routingError": routing.get("routingError"),
            "projectScopedApiEnabled": routing.get("projectScopedApiEnabled"),
            "projectAccessApiEnabled": routing.get("projectAccessApiEnabled"),
            "projectAccessRoutesRequired": routing.get("projectAccessRoutesRequired"),
            "projectAccessBlueprintRegistered": routing.get("projectAccessBlueprintRegistered"),
            "projectAccessRouteSurfaceReady": routing.get("projectAccessRouteSurfaceReady"),
            "projectAccessAuthzEnforced": routing.get("projectAccessAuthzEnforced"),
            "projectAccessCoreRouteRules": routing.get("projectAccessCoreRouteRules", []),
            "projectAccessMissingRouteRules": routing.get("projectAccessMissingRouteRules", []),
            "projectAccess": routing.get("projectAccess", {}),
            "worldStateApiEnabled": routing.get("worldStateApiEnabled"),
            "snapshotBackedChunksEnabled": routing.get("snapshotBackedChunksEnabled"),
            "commandWriteApiEnabled": routing.get("commandWriteApiEnabled"),
            "worldTestDebugRouteEnabled": routing.get("worldTestDebugRouteEnabled"),
            "earthDebugRouteEnabled": routing.get("earthDebugRouteEnabled"),
            "legacyRoutesEnabled": routing.get("legacyRoutesEnabled"),
            "registeredBlueprintNames": get_registered_blueprint_names(app),
            "registeredBlueprintCount": routing.get("registeredBlueprintCount"),
            "blueprintSpecs": routing.get("blueprintSpecs", []),
            "specValidation": routing.get("specValidation", {}),
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
    "PROJECT_ACCESS_BLUEPRINT_MODULE",
    "PROJECT_ACCESS_BLUEPRINT_ATTRIBUTE",
    "PROJECT_ACCESS_BLUEPRINT_NAME",
    "PROJECT_ACCESS_CORE_ROUTE_RULES",
    "CONFIG_ENABLE_PROJECT_ACCESS_ROUTES",
    "CONFIG_REQUIRE_PROJECT_ACCESS_ROUTES",
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