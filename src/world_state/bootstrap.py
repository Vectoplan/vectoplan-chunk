# services/vectoplan-chunk/src/world_state/bootstrap.py
"""
Project bootstrap helpers for the VECTOPLAN world-state layer.

The bootstrap layer is the editor entrypoint:

    GET /projects/<project_id>/bootstrap

It resolves:

    projectId
    -> universeId
    -> defaultWorldId / spawnWorldId
    -> concrete world instance
    -> templateId / providerWorldId
    -> route hints for chunks, blocks and commands

Current DB-backed mapping:

    projectId       = dev-project
    universeId      = dev-universe
    worldId         = world_spawn
    templateId      = flat
    providerWorldId = flat

Important:
- This module is framework-neutral.
- It does not import Flask.
- It does not generate chunks.
- It does not write chunks.
- It does not execute commands.
- It delegates persisted Project / Universe / WorldInstance resolution to
  src.world_state.service.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional


from .errors import (
    WorldStateBootstrapError,
    WorldStateError,
    coerce_world_state_error,
    make_json_safe,
    raise_for_missing_project_id,
)

from .service import (
    SERVICE_MODULE_VERSION as WORLD_STATE_SERVICE_MODULE_VERSION,
    DbProjectBootstrapContext,
    WorldStateService,
    get_default_world_state_service,
    reset_default_world_state_service_cache,
    utc_now_iso,
)


BOOTSTRAP_MODULE_VERSION = "0.2.0"
BOOTSTRAP_SOURCE = "world_state.bootstrap"
PROJECT_BOOTSTRAP_RESPONSE_VERSION = "project-bootstrap-response.v1"

ENV_BOOTSTRAP_API_PREFIX = "VECTOPLAN_CHUNK_API_PREFIX"
ENV_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS = "VECTOPLAN_CHUNK_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS"
ENV_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS = "VECTOPLAN_CHUNK_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS"
ENV_BOOTSTRAP_ALLOW_DEFAULT_PROJECT = "VECTOPLAN_CHUNK_BOOTSTRAP_ALLOW_DEFAULT_PROJECT"

DEFAULT_BOOTSTRAP_API_PREFIX = ""
DEFAULT_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS = False
DEFAULT_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS = False
DEFAULT_BOOTSTRAP_ALLOW_DEFAULT_PROJECT = False

_bootstrap_cache_lock = threading.RLock()
_project_bootstrap_cache: dict[str, "ProjectBootstrapBuildResult"] = {}
_bootstrap_status_cache: dict[str, Any] | None = None


# -----------------------------------------------------------------------------
# Safe helpers
# -----------------------------------------------------------------------------

def _safe_exception_message(exc: BaseException | Any) -> str:
    """Return robust exception message."""
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _coerce_string(value: Any, *, fallback: str = "") -> str:
    """Coerce arbitrary value to stripped string."""
    if value is None:
        return str(fallback)

    try:
        text = str(value)
    except Exception:
        text = repr(value)

    text = text.strip()
    return text if text else str(fallback)


def _coerce_bool(value: Any, *, fallback: bool = False) -> bool:
    """Coerce bool-like values."""
    if value is None:
        return bool(fallback)

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return bool(value)

    text = _coerce_string(value).lower()

    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False

    return bool(fallback)


def _get_env(name: str, fallback: Any = None) -> Any:
    """Read env var defensively."""
    try:
        value = os.environ.get(name)
    except Exception:
        return fallback

    if value is None:
        return fallback

    if isinstance(value, str) and value.strip() == "":
        return fallback

    return value


def _get_env_string(name: str, fallback: str = "") -> str:
    """Read env var as string."""
    return _coerce_string(_get_env(name, fallback), fallback=fallback)


def _get_env_bool(name: str, fallback: bool = False) -> bool:
    """Read env var as bool."""
    return _coerce_bool(_get_env(name, fallback), fallback=fallback)


def deep_copy_json(value: Any) -> Any:
    """Return JSON-safe deep copy."""
    try:
        return copy.deepcopy(make_json_safe(value))
    except Exception:
        return make_json_safe(value)


def stable_hash(value: Any) -> str:
    """Return stable sha256 hash for JSON-like values."""
    try:
        payload = json.dumps(
            make_json_safe(value),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        payload = repr(value)

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _call_to_dict(obj: Any, *, include_internal: bool = False, include_metadata: bool = True) -> dict[str, Any]:
    """Serialize object defensively."""
    if obj is None:
        return {}

    if isinstance(obj, Mapping):
        return dict(make_json_safe(obj))

    to_dict = getattr(obj, "to_dict", None)

    if callable(to_dict):
        attempts = (
            {
                "include_internal": include_internal,
                "include_metadata": include_metadata,
            },
            {
                "include_internal": include_internal,
            },
            {},
        )

        for kwargs in attempts:
            try:
                result = to_dict(**kwargs)
                if isinstance(result, Mapping):
                    return dict(make_json_safe(result))
                return {"value": make_json_safe(result)}
            except TypeError:
                continue
            except Exception as exc:
                return {
                    "type": obj.__class__.__name__,
                    "serializationError": _safe_exception_message(exc),
                }

    try:
        return {
            key: make_json_safe(value)
            for key, value in vars(obj).items()
            if not key.startswith("_")
        }
    except Exception:
        return {
            "type": obj.__class__.__name__,
            "repr": repr(obj),
        }


def _world_id(world: Any) -> str:
    return _coerce_string(getattr(world, "world_id", None) or getattr(world, "worldId", None))


def _project_id(project: Any) -> str:
    return _coerce_string(getattr(project, "project_id", None) or getattr(project, "projectId", None))


def _universe_id(universe: Any) -> str:
    return _coerce_string(getattr(universe, "universe_id", None) or getattr(universe, "universeId", None))


def _provider_world_id(world: Any) -> str:
    return _coerce_string(getattr(world, "provider_world_id", None) or getattr(world, "providerWorldId", None))


def _template_id(world: Any) -> str:
    return _coerce_string(getattr(world, "template_id", None) or getattr(world, "templateId", None))


def _provider_id(world: Any) -> str:
    return _coerce_string(getattr(world, "provider_id", None) or getattr(world, "providerId", None))


def _build_route_hints(*, project_id: str, world_id: str, api_prefix: str = "") -> dict[str, str]:
    """Build project-scoped route hints."""
    prefix = _coerce_string(api_prefix).rstrip("/")

    return {
        "projectBootstrap": f"{prefix}/projects/{project_id}/bootstrap",
        "project": f"{prefix}/projects/{project_id}",
        "worlds": f"{prefix}/projects/{project_id}/worlds",
        "world": f"{prefix}/projects/{project_id}/worlds/{world_id}",
        "blocks": f"{prefix}/projects/{project_id}/worlds/{world_id}/blocks",
        "chunk": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks",
        "chunksBatch": f"{prefix}/projects/{project_id}/worlds/{world_id}/chunks/batch",
        "commands": f"{prefix}/projects/{project_id}/worlds/{world_id}/commands",
    }


# -----------------------------------------------------------------------------
# Options/result models
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProjectBootstrapOptions:
    """
    Options for building a project bootstrap context.
    """

    project_id: str | None = None
    allow_default_project: bool = DEFAULT_BOOTSTRAP_ALLOW_DEFAULT_PROJECT
    api_prefix: str = DEFAULT_BOOTSTRAP_API_PREFIX
    include_provider_checks: bool = DEFAULT_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS
    require_provider_worlds: bool = DEFAULT_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS
    include_route_hints: bool = True
    include_worlds: bool = True
    include_metadata: bool = True
    cache_enabled: bool = True

    def __post_init__(self) -> None:
        normalized_project_id = None

        if self.project_id is not None and str(self.project_id).strip() != "":
            normalized_project_id = _coerce_string(self.project_id)

        object.__setattr__(self, "project_id", normalized_project_id)
        object.__setattr__(self, "allow_default_project", bool(self.allow_default_project))
        object.__setattr__(self, "api_prefix", _coerce_string(self.api_prefix, fallback="").rstrip("/"))
        object.__setattr__(self, "include_provider_checks", bool(self.include_provider_checks))
        object.__setattr__(self, "require_provider_worlds", bool(self.require_provider_worlds))
        object.__setattr__(self, "include_route_hints", bool(self.include_route_hints))
        object.__setattr__(self, "include_worlds", bool(self.include_worlds))
        object.__setattr__(self, "include_metadata", bool(self.include_metadata))
        object.__setattr__(self, "cache_enabled", bool(self.cache_enabled))

    @property
    def projectId(self) -> str | None:
        return self.project_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "allowDefaultProject": self.allow_default_project,
            "apiPrefix": self.api_prefix,
            "includeProviderChecks": self.include_provider_checks,
            "requireProviderWorlds": self.require_provider_worlds,
            "includeRouteHints": self.include_route_hints,
            "includeWorlds": self.include_worlds,
            "includeMetadata": self.include_metadata,
            "cacheEnabled": self.cache_enabled,
        }

    def cache_key(self) -> str:
        return stable_hash(self.to_dict())

    def copy_with(self, **changes: Any) -> "ProjectBootstrapOptions":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ProjectBootstrapBuildResult:
    """
    Build result for a project bootstrap.

    `context` is usually DbProjectBootstrapContext from src.world_state.service.
    """

    context: Any
    options: ProjectBootstrapOptions
    provider_checks: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    route_hints: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_checks",
            tuple(deep_copy_json(item) for item in (self.provider_checks or ())),
        )
        object.__setattr__(self, "route_hints", deep_copy_json(self.route_hints or {}))
        object.__setattr__(self, "metadata", deep_copy_json(self.metadata or {}))

    @property
    def project_id(self) -> str:
        return _coerce_string(
            getattr(self.context, "project_id", None)
            or getattr(self.context, "projectId", None)
        )

    @property
    def universe_id(self) -> str:
        return _coerce_string(
            getattr(self.context, "universe_id", None)
            or getattr(self.context, "universeId", None)
        )

    @property
    def default_world_id(self) -> str:
        return _coerce_string(
            getattr(self.context, "default_world_id", None)
            or getattr(self.context, "defaultWorldId", None)
        )

    @property
    def spawn_world_id(self) -> str:
        return _coerce_string(
            getattr(self.context, "spawn_world_id", None)
            or getattr(self.context, "spawnWorldId", None)
        )

    @property
    def projectId(self) -> str:
        return self.project_id

    @property
    def universeId(self) -> str:
        return self.universe_id

    @property
    def defaultWorldId(self) -> str:
        return self.default_world_id

    @property
    def spawnWorldId(self) -> str:
        return self.spawn_world_id

    def to_dict(self, *, include_context: bool = True) -> dict[str, Any]:
        payload = {
            "ok": True,
            "responseVersion": PROJECT_BOOTSTRAP_RESPONSE_VERSION,
            "source": BOOTSTRAP_SOURCE,
            "moduleVersion": BOOTSTRAP_MODULE_VERSION,
            "serviceModuleVersion": WORLD_STATE_SERVICE_MODULE_VERSION,
            "projectId": self.project_id,
            "universeId": self.universe_id,
            "defaultWorldId": self.default_world_id,
            "spawnWorldId": self.spawn_world_id,
            "options": self.options.to_dict(),
            "routeHints": deep_copy_json(self.route_hints),
            "providerChecks": [deep_copy_json(item) for item in self.provider_checks],
            "metadata": deep_copy_json(self.metadata),
        }

        if include_context:
            context_to_dict = getattr(self.context, "to_dict", None)
            if callable(context_to_dict):
                try:
                    payload["bootstrap"] = context_to_dict()
                except Exception as exc:
                    payload["bootstrap"] = {
                        "serializationError": _safe_exception_message(exc),
                    }
            else:
                payload["bootstrap"] = _call_to_dict(self.context)

        return payload


# -----------------------------------------------------------------------------
# Options factory / provider checks
# -----------------------------------------------------------------------------

def create_bootstrap_options_from_env(
    *,
    project_id: str | None = None,
    allow_default_project: bool | None = None,
    api_prefix: str | None = None,
    include_provider_checks: bool | None = None,
    require_provider_worlds: bool | None = None,
    cache_enabled: bool = True,
) -> ProjectBootstrapOptions:
    """
    Create bootstrap options from explicit values plus environment defaults.
    """
    resolved_allow_default_project = (
        bool(allow_default_project)
        if allow_default_project is not None
        else _get_env_bool(
            ENV_BOOTSTRAP_ALLOW_DEFAULT_PROJECT,
            DEFAULT_BOOTSTRAP_ALLOW_DEFAULT_PROJECT,
        )
    )

    resolved_include_provider_checks = (
        bool(include_provider_checks)
        if include_provider_checks is not None
        else _get_env_bool(
            ENV_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS,
            DEFAULT_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS,
        )
    )

    resolved_require_provider_worlds = (
        bool(require_provider_worlds)
        if require_provider_worlds is not None
        else _get_env_bool(
            ENV_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS,
            DEFAULT_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS,
        )
    )

    resolved_api_prefix = (
        _coerce_string(api_prefix)
        if api_prefix is not None
        else _get_env_string(
            ENV_BOOTSTRAP_API_PREFIX,
            DEFAULT_BOOTSTRAP_API_PREFIX,
        )
    )

    return ProjectBootstrapOptions(
        project_id=project_id,
        allow_default_project=resolved_allow_default_project,
        api_prefix=resolved_api_prefix,
        include_provider_checks=resolved_include_provider_checks,
        require_provider_worlds=resolved_require_provider_worlds,
        cache_enabled=cache_enabled,
    )


def _collect_provider_checks(
    service: WorldStateService,
    context: Any,
    *,
    require_provider_worlds: bool,
) -> tuple[Mapping[str, Any], ...]:
    """
    Collect provider availability checks for all worlds in a bootstrap context.
    """
    checks: list[Mapping[str, Any]] = []

    worlds = tuple(getattr(context, "worlds", ()) or ())
    project = getattr(context, "project", None)
    universe = getattr(context, "universe", None)

    project_id = _project_id(project) or getattr(context, "project_id", "")
    universe_id = _universe_id(universe) or getattr(context, "universe_id", "")

    for world in worlds:
        world_id = _world_id(world)
        provider_world_id = _provider_world_id(world)
        template_id = _template_id(world)
        provider_id = _provider_id(world)

        try:
            provider_metadata = service.provider_adapter.get_world_metadata(provider_world_id)

            checks.append(
                {
                    "ok": True,
                    "projectId": project_id,
                    "universeId": universe_id,
                    "worldId": world_id,
                    "templateId": template_id,
                    "providerId": provider_id,
                    "providerWorldId": provider_world_id,
                    "available": True,
                    "providerMetadata": provider_metadata,
                }
            )

        except Exception as exc:
            error_payload = coerce_world_state_error(exc).to_dict(include_debug=True)

            checks.append(
                {
                    "ok": False,
                    "projectId": project_id,
                    "universeId": universe_id,
                    "worldId": world_id,
                    "templateId": template_id,
                    "providerId": provider_id,
                    "providerWorldId": provider_world_id,
                    "available": False,
                    "error": error_payload,
                }
            )

            if require_provider_worlds:
                raise

    return tuple(checks)


# -----------------------------------------------------------------------------
# Build functions
# -----------------------------------------------------------------------------

def build_project_bootstrap(
    *,
    project_id: str | None = None,
    service: WorldStateService | None = None,
    options: ProjectBootstrapOptions | None = None,
    allow_default_project: bool | None = None,
    api_prefix: str | None = None,
    include_provider_checks: bool | None = None,
    require_provider_worlds: bool | None = None,
    refresh: bool = False,
    cache_enabled: bool | None = None,
) -> ProjectBootstrapBuildResult:
    """
    Build the project bootstrap result.

    Route code may either pass discrete arguments or a prepared
    ProjectBootstrapOptions instance.
    """
    resolved_options = options or create_bootstrap_options_from_env(
        project_id=project_id,
        allow_default_project=allow_default_project,
        api_prefix=api_prefix,
        include_provider_checks=include_provider_checks,
        require_provider_worlds=require_provider_worlds,
        cache_enabled=True if cache_enabled is None else bool(cache_enabled),
    )

    if cache_enabled is not None:
        resolved_options = resolved_options.copy_with(cache_enabled=bool(cache_enabled))

    if not resolved_options.allow_default_project:
        raise_for_missing_project_id(resolved_options.project_id)

    cache_key = resolved_options.cache_key()

    with _bootstrap_cache_lock:
        if (
            resolved_options.cache_enabled
            and not refresh
            and cache_key in _project_bootstrap_cache
        ):
            return copy.deepcopy(_project_bootstrap_cache[cache_key])

    resolved_service = service or get_default_world_state_service(
        strict_provider_resolution=resolved_options.require_provider_worlds,
    )

    try:
        context = resolved_service.create_project_bootstrap(
            resolved_options.project_id,
            allow_default_project=resolved_options.allow_default_project,
            api_prefix=resolved_options.api_prefix,
        )

        route_hints = (
            _build_route_hints(
                project_id=context.project_id,
                world_id=context.spawn_world_id,
                api_prefix=resolved_options.api_prefix,
            )
            if resolved_options.include_route_hints
            else {}
        )

        provider_checks: tuple[Mapping[str, Any], ...] = ()

        if resolved_options.include_provider_checks or resolved_options.require_provider_worlds:
            provider_checks = _collect_provider_checks(
                resolved_service,
                context,
                require_provider_worlds=resolved_options.require_provider_worlds,
            )

        spawn_world = getattr(context, "spawn_world", None)

        metadata = {
            "source": BOOTSTRAP_SOURCE,
            "moduleVersion": BOOTSTRAP_MODULE_VERSION,
            "serviceModuleVersion": WORLD_STATE_SERVICE_MODULE_VERSION,
            "createdAt": utc_now_iso(),
            "dbBacked": True,
            "projectScopedRoutes": True,
            "phase": "postgres-world-state",
            "routingInvariant": (
                "Editor opens project bootstrap, then loads concrete worldId. "
                "Provider/template id stays separate."
            ),
            "defaultMapping": {
                "projectId": context.project_id,
                "universeId": context.universe_id,
                "defaultWorldId": context.default_world_id,
                "spawnWorldId": context.spawn_world_id,
                "spawnTemplateId": _template_id(spawn_world),
                "spawnProviderWorldId": _provider_world_id(spawn_world),
            },
            "bootstrapHash": stable_hash(
                {
                    "projectId": context.project_id,
                    "universeId": context.universe_id,
                    "defaultWorldId": context.default_world_id,
                    "spawnWorldId": context.spawn_world_id,
                    "options": resolved_options.to_dict(),
                }
            ),
        }

        if not resolved_options.include_metadata:
            metadata = {
                "source": BOOTSTRAP_SOURCE,
                "moduleVersion": BOOTSTRAP_MODULE_VERSION,
                "serviceModuleVersion": WORLD_STATE_SERVICE_MODULE_VERSION,
                "dbBacked": True,
            }

        if not resolved_options.include_worlds:
            context = DbProjectBootstrapContext(
                project=context.project,
                universe=context.universe,
                default_world=context.default_world,
                spawn_world=context.spawn_world,
                worlds=(context.default_world, context.spawn_world),
                route_hints=route_hints,
                metadata={
                    **deep_copy_json(getattr(context, "metadata", {}) or {}),
                    **deep_copy_json(metadata),
                },
            )
        elif route_hints != getattr(context, "route_hints", None):
            context = DbProjectBootstrapContext(
                project=context.project,
                universe=context.universe,
                default_world=context.default_world,
                spawn_world=context.spawn_world,
                worlds=tuple(context.worlds),
                route_hints=route_hints,
                metadata={
                    **deep_copy_json(getattr(context, "metadata", {}) or {}),
                    **deep_copy_json(metadata),
                },
            )

        result = ProjectBootstrapBuildResult(
            context=context,
            options=resolved_options,
            provider_checks=provider_checks,
            route_hints=route_hints,
            metadata=metadata,
        )

        with _bootstrap_cache_lock:
            if resolved_options.cache_enabled:
                _project_bootstrap_cache[cache_key] = copy.deepcopy(result)

        return copy.deepcopy(result)

    except WorldStateError:
        raise
    except Exception as exc:
        raise WorldStateBootstrapError(
            "Could not build project bootstrap.",
            details={
                "projectId": resolved_options.project_id,
                "options": resolved_options.to_dict(),
                "error": _safe_exception_message(exc),
            },
            cause=exc if isinstance(exc, BaseException) else None,
        ) from exc


def create_project_bootstrap(
    project_id: str | None = None,
    *,
    service: WorldStateService | None = None,
    options: ProjectBootstrapOptions | None = None,
    allow_default_project: bool | None = None,
    api_prefix: str | None = None,
    include_provider_checks: bool | None = None,
    require_provider_worlds: bool | None = None,
    refresh: bool = False,
    cache_enabled: bool | None = None,
) -> Any:
    """
    Create and return only the bootstrap context.
    """
    result = build_project_bootstrap(
        project_id=project_id,
        service=service,
        options=options,
        allow_default_project=allow_default_project,
        api_prefix=api_prefix,
        include_provider_checks=include_provider_checks,
        require_provider_worlds=require_provider_worlds,
        refresh=refresh,
        cache_enabled=cache_enabled,
    )

    return copy.deepcopy(result.context)


def get_project_bootstrap_result(
    project_id: str | None = None,
    *,
    service: WorldStateService | None = None,
    options: ProjectBootstrapOptions | None = None,
    allow_default_project: bool | None = None,
    api_prefix: str | None = None,
    include_provider_checks: bool | None = None,
    require_provider_worlds: bool | None = None,
    refresh: bool = False,
    cache_enabled: bool | None = None,
) -> ProjectBootstrapBuildResult:
    """
    Create and return the full bootstrap build result.

    Accepts `options=...` because route code commonly builds
    ProjectBootstrapOptions after reading query parameters and env values.
    """
    return build_project_bootstrap(
        project_id=project_id,
        service=service,
        options=options,
        allow_default_project=allow_default_project,
        api_prefix=api_prefix,
        include_provider_checks=include_provider_checks,
        require_provider_worlds=require_provider_worlds,
        refresh=refresh,
        cache_enabled=cache_enabled,
    )


def create_default_project_bootstrap(
    *,
    refresh: bool = False,
    require_provider_worlds: bool | None = None,
) -> Any:
    """
    Create bootstrap context for the configured/default project.
    """
    return create_project_bootstrap(
        project_id=None,
        allow_default_project=True,
        require_provider_worlds=require_provider_worlds,
        refresh=refresh,
    )


def serialize_project_bootstrap_result(
    result: ProjectBootstrapBuildResult,
    *,
    include_context: bool = True,
) -> dict[str, Any]:
    """
    Lightweight local serializer for diagnostics.

    The main API serializer is still routes/projects.py or
    world_state/serializer.py, depending on the caller.
    """
    return result.to_dict(include_context=include_context)


# -----------------------------------------------------------------------------
# Cache/status helpers
# -----------------------------------------------------------------------------

def get_project_bootstrap_cache_status() -> dict[str, Any]:
    """
    Return cache diagnostics for bootstrap creation.
    """
    with _bootstrap_cache_lock:
        return {
            "ok": True,
            "source": BOOTSTRAP_SOURCE,
            "moduleVersion": BOOTSTRAP_MODULE_VERSION,
            "cacheSize": len(_project_bootstrap_cache),
            "cacheKeys": sorted(_project_bootstrap_cache.keys()),
        }


def reset_project_bootstrap_cache(
    *,
    reset_service_cache: bool = False,
    reset_resolver_cache: bool = False,
    reset_catalog_cache: bool = False,
) -> None:
    """
    Reset bootstrap caches.

    Args:
        reset_service_cache:
            Also reset the cached world-state service.
        reset_resolver_cache:
            Forwarded to service cache reset.
        reset_catalog_cache:
            Forwarded to service/resolver/defaults cache reset where available.
    """
    global _bootstrap_status_cache

    with _bootstrap_cache_lock:
        _project_bootstrap_cache.clear()
        _bootstrap_status_cache = None

    if reset_service_cache:
        reset_default_world_state_service_cache(
            reset_resolver_cache=reset_resolver_cache,
            reset_catalog_cache=reset_catalog_cache,
        )


def get_bootstrap_status(
    *,
    refresh: bool = False,
    include_default_bootstrap: bool = False,
    require_provider_worlds: bool = False,
) -> dict[str, Any]:
    """
    Return bootstrap module diagnostics.
    """
    global _bootstrap_status_cache

    with _bootstrap_cache_lock:
        if _bootstrap_status_cache is not None and not refresh and not include_default_bootstrap:
            return copy.deepcopy(_bootstrap_status_cache)

    payload: dict[str, Any] = {
        "ok": False,
        "source": BOOTSTRAP_SOURCE,
        "moduleVersion": BOOTSTRAP_MODULE_VERSION,
        "serviceModuleVersion": WORLD_STATE_SERVICE_MODULE_VERSION,
        "responseVersion": PROJECT_BOOTSTRAP_RESPONSE_VERSION,
        "dbBacked": True,
        "cache": get_project_bootstrap_cache_status(),
        "env": {
            "apiPrefix": _get_env_string(ENV_BOOTSTRAP_API_PREFIX, DEFAULT_BOOTSTRAP_API_PREFIX),
            "includeProviderChecks": _get_env_bool(
                ENV_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS,
                DEFAULT_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS,
            ),
            "requireProviderWorlds": _get_env_bool(
                ENV_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS,
                DEFAULT_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS,
            ),
            "allowDefaultProject": _get_env_bool(
                ENV_BOOTSTRAP_ALLOW_DEFAULT_PROJECT,
                DEFAULT_BOOTSTRAP_ALLOW_DEFAULT_PROJECT,
            ),
        },
        "defaultBootstrap": None,
        "error": None,
    }

    try:
        if include_default_bootstrap:
            result = get_project_bootstrap_result(
                project_id=None,
                allow_default_project=True,
                require_provider_worlds=require_provider_worlds,
                refresh=refresh,
                cache_enabled=False,
            )
            payload["defaultBootstrap"] = result.to_dict(include_context=True)

        payload["ok"] = True

    except Exception as exc:
        error = coerce_world_state_error(exc)
        payload["ok"] = False
        payload["error"] = error.to_dict(include_debug=True)

    safe_payload = make_json_safe(payload)

    with _bootstrap_cache_lock:
        if not include_default_bootstrap:
            _bootstrap_status_cache = copy.deepcopy(safe_payload)

    return copy.deepcopy(safe_payload)


def assert_project_bootstrap_ready(
    *,
    refresh: bool = False,
    require_provider_worlds: bool = False,
) -> Any:
    """
    Validate that default project bootstrap can be created.
    """
    try:
        return create_default_project_bootstrap(
            refresh=refresh,
            require_provider_worlds=require_provider_worlds,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Project bootstrap is not ready: {_safe_exception_message(exc)}"
        ) from exc


__all__ = (
    "BOOTSTRAP_MODULE_VERSION",
    "BOOTSTRAP_SOURCE",
    "PROJECT_BOOTSTRAP_RESPONSE_VERSION",
    "ENV_BOOTSTRAP_API_PREFIX",
    "ENV_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS",
    "ENV_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS",
    "ENV_BOOTSTRAP_ALLOW_DEFAULT_PROJECT",
    "DEFAULT_BOOTSTRAP_API_PREFIX",
    "DEFAULT_BOOTSTRAP_INCLUDE_PROVIDER_CHECKS",
    "DEFAULT_BOOTSTRAP_REQUIRE_PROVIDER_WORLDS",
    "DEFAULT_BOOTSTRAP_ALLOW_DEFAULT_PROJECT",
    "ProjectBootstrapOptions",
    "ProjectBootstrapBuildResult",
    "create_bootstrap_options_from_env",
    "build_project_bootstrap",
    "create_project_bootstrap",
    "get_project_bootstrap_result",
    "create_default_project_bootstrap",
    "serialize_project_bootstrap_result",
    "get_project_bootstrap_cache_status",
    "reset_project_bootstrap_cache",
    "get_bootstrap_status",
    "assert_project_bootstrap_ready",
)