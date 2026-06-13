# services/vectoplan-chunk/src/world_state/resolver.py
"""
Resolver for the VECTOPLAN world-state layer.

This module resolves the productive project-scoped runtime context:

    projectId
    -> universeId
    -> concrete worldId
    -> templateId / providerWorldId
    -> existing src.world provider/template layer

Important distinction:

    worldId
        Concrete runtime world instance inside a project/universe.
        Example: world_spawn

    templateId / providerWorldId
        Generator/template id used by src.world.
        Example: flat

The resolver intentionally does not contain Flask, SQLAlchemy or PostgreSQL
code. In phase 1 it uses the in-memory catalog from defaults.py. Later the same
resolver API can be backed by repositories.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .defaults import (
    get_default_world_state_catalog,
    get_default_world_state_ids,
    get_default_world_state_status,
    reset_default_world_state_catalog_cache,
)
from .errors import (
    InvalidProjectUniverseBindingError,
    InvalidProjectWorldBindingError,
    InvalidUniverseWorldBindingError,
    InvalidWorldStateContextError,
    ProjectNotFoundError,
    ProviderWorldNotFoundError,
    ProviderWorldResolutionError,
    UniverseNotFoundError,
    WorldInstanceNotFoundError,
    WorldStateCatalogError,
    WorldStateProviderError,
    WorldStateResolutionError,
    WorldTemplateNotFoundError,
    coerce_world_state_error,
    make_json_safe,
    raise_for_missing_project_id,
    raise_for_missing_provider_world_id,
    raise_for_missing_template_id,
    raise_for_missing_universe_id,
    raise_for_missing_world_id,
)
from .models import (
    DEFAULT_STATUS_ACTIVE,
    ProjectRuntimeContext,
    UniverseRuntimeContext,
    WorldInstanceDefinition,
    WorldRuntimeContext,
    WorldStateCatalog,
    assert_same_project,
    assert_same_universe,
    build_world_context_key,
    create_world_runtime_context,
    deep_copy_json,
    normalize_project_id,
    normalize_provider_world_id,
    normalize_template_id,
    normalize_universe_id,
    normalize_world_instance_id,
    stable_hash,
)


RESOLVER_MODULE_VERSION = "0.1.0"
RESOLVER_SOURCE = "world_state.resolver"

_PROVIDER_MODULE_CANDIDATES: tuple[str, ...] = (
    "src.world.service",
    "src.world.loader",
)

_PROVIDER_WORLD_METHOD_CANDIDATES: tuple[str, ...] = (
    "get_world_metadata",
    "get_world_definition",
    "load_world",
)


_default_resolver_lock = threading.RLock()
_default_world_state_resolver_cache: "WorldStateResolver | None" = None


@dataclass(frozen=True, slots=True)
class ProviderWorldResolution:
    """
    Result of resolving a concrete world instance to a provider/template world.
    """

    provider_world_id: str
    template_id: str
    provider_id: str
    world: WorldInstanceDefinition
    provider_definition: Any | None = None
    available: bool = False
    source: str | None = None
    error: str | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_world_id",
            normalize_provider_world_id(self.provider_world_id),
        )
        object.__setattr__(
            self,
            "template_id",
            normalize_template_id(self.template_id),
        )
        object.__setattr__(
            self,
            "provider_id",
            str(self.provider_id or self.provider_world_id).strip(),
        )
        object.__setattr__(
            self,
            "diagnostics",
            deep_copy_json(self.diagnostics or {}),
        )

    @property
    def providerWorldId(self) -> str:
        return self.provider_world_id

    @property
    def templateId(self) -> str:
        return self.template_id

    @property
    def providerId(self) -> str:
        return self.provider_id

    def to_dict(self, *, include_definition: bool = False) -> dict[str, Any]:
        payload = {
            "providerWorldId": self.provider_world_id,
            "templateId": self.template_id,
            "providerId": self.provider_id,
            "worldId": self.world.world_id,
            "projectId": self.world.project_id,
            "universeId": self.world.universe_id,
            "available": self.available,
            "source": self.source,
            "error": self.error,
            "diagnostics": deep_copy_json(self.diagnostics),
        }

        if include_definition:
            payload["providerDefinition"] = _safe_provider_definition_to_dict(
                self.provider_definition,
            )

        return payload


@dataclass(frozen=True, slots=True)
class ProjectResolution:
    """
    Resolved project context plus related universes and worlds.
    """

    project: ProjectRuntimeContext
    universes: tuple[UniverseRuntimeContext, ...] = field(default_factory=tuple)
    worlds: tuple[WorldInstanceDefinition, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "universes", tuple(self.universes or ()))
        object.__setattr__(self, "worlds", tuple(self.worlds or ()))

        for universe in self.universes:
            assert_same_project(project=self.project, universe=universe)

        for world in self.worlds:
            assert_same_project(project=self.project, world=world)

    @property
    def project_id(self) -> str:
        return self.project.project_id

    @property
    def projectId(self) -> str:
        return self.project_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project.to_dict(),
            "universes": [universe.to_dict() for universe in self.universes],
            "worlds": [world.to_dict() for world in self.worlds],
            "counts": {
                "universes": len(self.universes),
                "worlds": len(self.worlds),
            },
        }


@dataclass(frozen=True, slots=True)
class ResolverStatus:
    """
    JSON-safe resolver status.
    """

    ok: bool
    source: str
    module_version: str
    catalog_hash: str | None
    default_ids: Mapping[str, Any]
    counts: Mapping[str, int]
    provider_check_enabled: bool
    error: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "moduleVersion": self.module_version,
            "catalogHash": self.catalog_hash,
            "defaultIds": deep_copy_json(self.default_ids),
            "counts": deep_copy_json(self.counts),
            "providerCheckEnabled": self.provider_check_enabled,
            "error": deep_copy_json(self.error),
            "metadata": deep_copy_json(self.metadata),
        }


def _safe_exception_message(exc: BaseException | Any) -> str:
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_import_module(module_path: str) -> tuple[Any | None, str | None]:
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        return None, _safe_exception_message(exc)

    return module, None


def _safe_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any | None, str | None]:
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:
        return None, _safe_exception_message(exc)


def _safe_provider_definition_to_dict(definition: Any) -> dict[str, Any]:
    if definition is None:
        return {}

    if isinstance(definition, Mapping):
        return dict(make_json_safe(definition))

    if dataclasses.is_dataclass(definition):
        try:
            return dict(make_json_safe(dataclasses.asdict(definition)))
        except Exception:
            return {"value": make_json_safe(definition)}

    if hasattr(definition, "to_dict") and callable(definition.to_dict):
        try:
            result = definition.to_dict()
            if isinstance(result, Mapping):
                return dict(make_json_safe(result))
            return {"value": make_json_safe(result)}
        except Exception as exc:
            return {
                "serializationError": _safe_exception_message(exc),
                "type": definition.__class__.__name__,
            }

    result: dict[str, Any] = {
        "type": definition.__class__.__name__,
    }

    for attr in (
        "world_id",
        "worldId",
        "world_type",
        "worldType",
        "label",
        "name",
        "generator_type",
        "generatorType",
        "generator_version",
        "generatorVersion",
        "projection_type",
        "projectionType",
        "topology_type",
        "topologyType",
        "coordinate_system",
        "coordinateSystem",
        "chunk_size",
        "chunkSize",
        "cell_size",
        "cellSize",
        "surface_y",
        "surfaceY",
        "min_y",
        "minY",
        "max_y",
        "maxY",
        "block_registry_id",
        "blockRegistryId",
        "block_registry_version",
        "blockRegistryVersion",
        "metadata",
    ):
        try:
            if hasattr(definition, attr):
                result[attr] = make_json_safe(getattr(definition, attr))
        except Exception:
            continue

    return result


def _extract_provider_world_id_from_definition(definition: Any) -> str | None:
    if definition is None:
        return None

    for key in ("world_id", "worldId", "id"):
        try:
            if isinstance(definition, Mapping) and key in definition:
                value = definition[key]
                if value:
                    return str(value).strip()
        except Exception:
            pass

        try:
            if hasattr(definition, key):
                value = getattr(definition, key)
                if value:
                    return str(value).strip()
        except Exception:
            pass

    return None


class WorldStateResolver:
    """
    Resolve project/universe/world-instance context for productive APIs.

    The resolver uses a `WorldStateCatalog` as its state source. In phase 1 the
    catalog is in-memory. Later the catalog can be created from repositories.

    Main responsibilities:
    - resolve projectId
    - resolve default universe for project
    - resolve concrete worldId inside project/universe
    - map concrete worldId to providerWorldId/templateId
    - optionally validate provider/template availability through src.world
    """

    def __init__(
        self,
        catalog: WorldStateCatalog | None = None,
        *,
        provider_check_enabled: bool = True,
        strict_provider_resolution: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._catalog = copy.deepcopy(catalog) if catalog is not None else get_default_world_state_catalog()
        self._provider_check_enabled = bool(provider_check_enabled)
        self._strict_provider_resolution = bool(strict_provider_resolution)
        self._metadata = deep_copy_json(metadata or {})
        self._provider_resolution_cache: dict[str, ProviderWorldResolution] = {}
        self._status_cache: dict[str, Any] | None = None

    @property
    def catalog(self) -> WorldStateCatalog:
        with self._lock:
            return copy.deepcopy(self._catalog)

    @property
    def provider_check_enabled(self) -> bool:
        return self._provider_check_enabled

    @property
    def strict_provider_resolution(self) -> bool:
        return self._strict_provider_resolution

    def reset_caches(self) -> None:
        """
        Reset resolver-local caches.
        """
        with self._lock:
            self._provider_resolution_cache.clear()
            self._status_cache = None

    def replace_catalog(self, catalog: WorldStateCatalog) -> None:
        """
        Replace the backing catalog.

        This is useful for tests and for a later repository-backed refresh.
        """
        with self._lock:
            self._catalog = copy.deepcopy(catalog)
            self.reset_caches()

    def refresh_default_catalog(self) -> WorldStateCatalog:
        """
        Refresh the default in-memory catalog from defaults.py and install it.
        """
        catalog = get_default_world_state_catalog(refresh=True)

        with self._lock:
            self._catalog = copy.deepcopy(catalog)
            self.reset_caches()

        return copy.deepcopy(catalog)

    def get_catalog_hash(self) -> str:
        with self._lock:
            return self._catalog.catalog_hash()

    def get_default_project_id(self) -> str:
        with self._lock:
            return self._catalog.default_project_id

    def resolve_project(
        self,
        project_id: str | None = None,
        *,
        allow_default: bool = False,
    ) -> ProjectRuntimeContext:
        """
        Resolve a project by id.

        If `allow_default=True`, missing project_id resolves to the catalog's
        default project.
        """
        if project_id is None or str(project_id).strip() == "":
            if not allow_default:
                raise_for_missing_project_id(project_id)
            project_id = self.get_default_project_id()

        normalized_project_id = normalize_project_id(project_id)

        with self._lock:
            project = self._catalog.get_project(normalized_project_id)

        if project is None:
            raise ProjectNotFoundError(
                normalized_project_id,
                details={
                    "availableProjectIds": self.list_project_ids(),
                    "defaultProjectId": self.get_default_project_id(),
                },
            )

        return copy.deepcopy(project)

    def list_projects(self, *, include_inactive: bool = False) -> tuple[ProjectRuntimeContext, ...]:
        """
        List projects in the catalog.
        """
        with self._lock:
            projects = tuple(self._catalog.projects)

        if include_inactive:
            return copy.deepcopy(projects)

        return copy.deepcopy(
            tuple(
                project
                for project in projects
                if project.status == DEFAULT_STATUS_ACTIVE
            )
        )

    def list_project_ids(self, *, include_inactive: bool = True) -> list[str]:
        """
        List known project ids.
        """
        return [
            project.project_id
            for project in self.list_projects(include_inactive=include_inactive)
        ]

    def resolve_project_details(
        self,
        project_id: str | None = None,
        *,
        allow_default: bool = False,
        include_inactive_worlds: bool = False,
    ) -> ProjectResolution:
        """
        Resolve a project plus its universes and worlds.
        """
        project = self.resolve_project(project_id, allow_default=allow_default)
        universes = self.list_project_universes(project.project_id)
        worlds = self.list_project_worlds(
            project.project_id,
            include_inactive=include_inactive_worlds,
        )

        return ProjectResolution(
            project=project,
            universes=universes,
            worlds=worlds,
        )

    def resolve_universe(
        self,
        universe_id: str,
        *,
        project_id: str | None = None,
    ) -> UniverseRuntimeContext:
        """
        Resolve a universe and optionally validate its project binding.
        """
        if universe_id is None or str(universe_id).strip() == "":
            raise_for_missing_universe_id(universe_id)

        normalized_universe_id = normalize_universe_id(universe_id)

        with self._lock:
            universe = self._catalog.get_universe(normalized_universe_id)

        if universe is None:
            raise UniverseNotFoundError(
                normalized_universe_id,
                project_id=project_id,
                details={
                    "availableUniverseIds": self.list_universe_ids(),
                },
            )

        if project_id is not None:
            project = self.resolve_project(project_id)
            if universe.project_id != project.project_id:
                raise InvalidProjectUniverseBindingError(
                    project_id=project.project_id,
                    universe_id=universe.universe_id,
                    universe_project_id=universe.project_id,
                )

        return copy.deepcopy(universe)

    def resolve_default_universe(
        self,
        project_id: str | None = None,
        *,
        allow_default_project: bool = False,
    ) -> UniverseRuntimeContext:
        """
        Resolve the default universe for a project.
        """
        project = self.resolve_project(
            project_id,
            allow_default=allow_default_project,
        )

        universe = self.resolve_universe(
            project.default_universe_id,
            project_id=project.project_id,
        )

        return universe

    def list_universes(self, *, include_inactive: bool = False) -> tuple[UniverseRuntimeContext, ...]:
        """
        List all universes in the catalog.
        """
        with self._lock:
            universes = tuple(self._catalog.universes)

        if include_inactive:
            return copy.deepcopy(universes)

        return copy.deepcopy(
            tuple(
                universe
                for universe in universes
                if universe.status == DEFAULT_STATUS_ACTIVE
            )
        )

    def list_universe_ids(self, *, include_inactive: bool = True) -> list[str]:
        """
        List known universe ids.
        """
        return [
            universe.universe_id
            for universe in self.list_universes(include_inactive=include_inactive)
        ]

    def list_project_universes(
        self,
        project_id: str,
        *,
        include_inactive: bool = False,
    ) -> tuple[UniverseRuntimeContext, ...]:
        """
        List universes belonging to one project.
        """
        project = self.resolve_project(project_id)

        with self._lock:
            universes = self._catalog.get_project_universes(project.project_id)

        if include_inactive:
            return copy.deepcopy(universes)

        return copy.deepcopy(
            tuple(
                universe
                for universe in universes
                if universe.status == DEFAULT_STATUS_ACTIVE
            )
        )

    def resolve_world(
        self,
        project_id: str,
        world_id: str,
        *,
        universe_id: str | None = None,
        include_inactive: bool = False,
    ) -> WorldInstanceDefinition:
        """
        Resolve a concrete world instance inside a project.

        This resolves productive API `worldId`, not provider/template id.
        """
        if project_id is None or str(project_id).strip() == "":
            raise_for_missing_project_id(project_id)

        if world_id is None or str(world_id).strip() == "":
            raise_for_missing_world_id(world_id)

        project = self.resolve_project(project_id)
        normalized_world_id = normalize_world_instance_id(world_id)

        normalized_universe_id = (
            normalize_universe_id(universe_id)
            if universe_id is not None
            else None
        )

        if normalized_universe_id is not None:
            self.resolve_universe(normalized_universe_id, project_id=project.project_id)

        with self._lock:
            world = self._catalog.get_world(
                project_id=project.project_id,
                universe_id=normalized_universe_id,
                world_id=normalized_world_id,
            )

        if world is None:
            raise WorldInstanceNotFoundError(
                normalized_world_id,
                project_id=project.project_id,
                universe_id=normalized_universe_id,
                details={
                    "availableWorldIds": [
                        item.world_id
                        for item in self.list_project_worlds(
                            project.project_id,
                            universe_id=normalized_universe_id,
                            include_inactive=True,
                        )
                    ],
                },
            )

        if not include_inactive and world.status != DEFAULT_STATUS_ACTIVE:
            raise WorldInstanceNotFoundError(
                normalized_world_id,
                project_id=project.project_id,
                universe_id=world.universe_id,
                details={
                    "reason": "world_exists_but_is_not_active",
                    "status": world.status,
                },
            )

        if world.project_id != project.project_id:
            raise InvalidProjectWorldBindingError(
                project_id=project.project_id,
                world_id=world.world_id,
                world_project_id=world.project_id,
            )

        if normalized_universe_id is not None and world.universe_id != normalized_universe_id:
            raise InvalidUniverseWorldBindingError(
                universe_id=normalized_universe_id,
                world_id=world.world_id,
                world_universe_id=world.universe_id,
                project_id=project.project_id,
            )

        return copy.deepcopy(world)

    def resolve_default_world(
        self,
        project_id: str | None = None,
        *,
        allow_default_project: bool = False,
    ) -> WorldInstanceDefinition:
        """
        Resolve the default world for a project's default universe.
        """
        universe = self.resolve_default_universe(
            project_id,
            allow_default_project=allow_default_project,
        )

        return self.resolve_world(
            universe.project_id,
            universe.default_world_id,
            universe_id=universe.universe_id,
        )

    def resolve_spawn_world(
        self,
        project_id: str | None = None,
        *,
        allow_default_project: bool = False,
    ) -> WorldInstanceDefinition:
        """
        Resolve the spawn world for a project's default universe.
        """
        universe = self.resolve_default_universe(
            project_id,
            allow_default_project=allow_default_project,
        )

        return self.resolve_world(
            universe.project_id,
            universe.spawn_world_id,
            universe_id=universe.universe_id,
        )

    def list_project_worlds(
        self,
        project_id: str,
        *,
        universe_id: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[WorldInstanceDefinition, ...]:
        """
        List concrete world instances of a project.

        Phase 1 returns exactly one world:
            world_spawn -> flat
        """
        project = self.resolve_project(project_id)

        normalized_universe_id = (
            normalize_universe_id(universe_id)
            if universe_id is not None
            else None
        )

        if normalized_universe_id is not None:
            self.resolve_universe(
                normalized_universe_id,
                project_id=project.project_id,
            )

        with self._lock:
            worlds = self._catalog.get_project_worlds(
                project.project_id,
                universe_id=normalized_universe_id,
                include_inactive=include_inactive,
            )

        return copy.deepcopy(worlds)

    def list_world_ids(
        self,
        project_id: str,
        *,
        universe_id: str | None = None,
        include_inactive: bool = True,
    ) -> list[str]:
        """
        List concrete world ids for one project.
        """
        return [
            world.world_id
            for world in self.list_project_worlds(
                project_id,
                universe_id=universe_id,
                include_inactive=include_inactive,
            )
        ]

    def resolve_world_runtime_context(
        self,
        project_id: str,
        world_id: str,
        *,
        universe_id: str | None = None,
    ) -> WorldRuntimeContext:
        """
        Resolve project, universe and concrete world as one context object.
        """
        project = self.resolve_project(project_id)
        world = self.resolve_world(
            project.project_id,
            world_id,
            universe_id=universe_id,
        )
        universe = self.resolve_universe(
            world.universe_id,
            project_id=project.project_id,
        )

        assert_same_project(project=project, universe=universe, world=world)
        assert_same_universe(universe=universe, world=world)

        return create_world_runtime_context(
            project=project,
            universe=universe,
            world=world,
        )

    def resolve_default_world_runtime_context(
        self,
        project_id: str | None = None,
        *,
        allow_default_project: bool = False,
    ) -> WorldRuntimeContext:
        """
        Resolve the default world as a full runtime context.
        """
        project = self.resolve_project(
            project_id,
            allow_default=allow_default_project,
        )
        world = self.resolve_default_world(project.project_id)

        return self.resolve_world_runtime_context(
            project.project_id,
            world.world_id,
            universe_id=world.universe_id,
        )

    def resolve_spawn_world_runtime_context(
        self,
        project_id: str | None = None,
        *,
        allow_default_project: bool = False,
    ) -> WorldRuntimeContext:
        """
        Resolve the spawn world as a full runtime context.
        """
        project = self.resolve_project(
            project_id,
            allow_default=allow_default_project,
        )
        world = self.resolve_spawn_world(project.project_id)

        return self.resolve_world_runtime_context(
            project.project_id,
            world.world_id,
            universe_id=world.universe_id,
        )

    def validate_world_binding(
        self,
        *,
        project_id: str,
        world_id: str,
        universe_id: str | None = None,
    ) -> bool:
        """
        Validate that a world belongs to the requested project/universe.
        """
        self.resolve_world_runtime_context(
            project_id,
            world_id,
            universe_id=universe_id,
        )
        return True

    def get_world_context_key(
        self,
        *,
        project_id: str,
        world_id: str,
        universe_id: str | None = None,
    ) -> str:
        """
        Return the stable project/universe/world context key.
        """
        context = self.resolve_world_runtime_context(
            project_id,
            world_id,
            universe_id=universe_id,
        )

        return build_world_context_key(
            project_id=context.project_id,
            universe_id=context.universe_id,
            world_id=context.world_id,
        )

    def resolve_template_id(self, world: WorldInstanceDefinition | WorldRuntimeContext) -> str:
        """
        Resolve the template id for a concrete world.
        """
        if isinstance(world, WorldRuntimeContext):
            world_instance = world.world
        else:
            world_instance = world

        if world_instance.template_id is None or str(world_instance.template_id).strip() == "":
            raise_for_missing_template_id(world_instance.template_id)

        return normalize_template_id(world_instance.template_id)

    def resolve_provider_world_id(self, world: WorldInstanceDefinition | WorldRuntimeContext) -> str:
        """
        Resolve the provider world id for a concrete world.
        """
        if isinstance(world, WorldRuntimeContext):
            world_instance = world.world
        else:
            world_instance = world

        if world_instance.provider_world_id is None or str(world_instance.provider_world_id).strip() == "":
            raise_for_missing_provider_world_id(world_instance.provider_world_id)

        return normalize_provider_world_id(world_instance.provider_world_id)

    def resolve_provider_world(
        self,
        world: WorldInstanceDefinition | WorldRuntimeContext,
        *,
        require_available: bool | None = None,
        refresh: bool = False,
    ) -> ProviderWorldResolution:
        """
        Resolve provider/template world backing a concrete world instance.

        If provider checks are disabled, this returns a successful logical
        mapping without loading src.world.

        If checks are enabled, it attempts to load the provider definition from
        the existing src.world layer.
        """
        if isinstance(world, WorldRuntimeContext):
            world_instance = world.world
        else:
            world_instance = world

        template_id = self.resolve_template_id(world_instance)
        provider_world_id = self.resolve_provider_world_id(world_instance)
        provider_id = str(world_instance.provider_id or provider_world_id).strip()

        strict = self._strict_provider_resolution if require_available is None else bool(require_available)

        cache_key = stable_hash(
            {
                "projectId": world_instance.project_id,
                "universeId": world_instance.universe_id,
                "worldId": world_instance.world_id,
                "templateId": template_id,
                "providerWorldId": provider_world_id,
                "providerId": provider_id,
                "providerCheckEnabled": self._provider_check_enabled,
                "strict": strict,
            }
        )

        with self._lock:
            if not refresh and cache_key in self._provider_resolution_cache:
                resolution = copy.deepcopy(self._provider_resolution_cache[cache_key])
                if strict and not resolution.available:
                    raise ProviderWorldResolutionError(
                        provider_world_id,
                        template_id=template_id,
                        project_id=world_instance.project_id,
                        universe_id=world_instance.universe_id,
                        world_id=world_instance.world_id,
                        details=resolution.to_dict(include_definition=False),
                    )
                return resolution

        if not self._provider_check_enabled:
            resolution = ProviderWorldResolution(
                provider_world_id=provider_world_id,
                template_id=template_id,
                provider_id=provider_id,
                world=copy.deepcopy(world_instance),
                provider_definition=None,
                available=True,
                source="logical-mapping-no-provider-check",
                error=None,
                diagnostics={
                    "providerCheckEnabled": False,
                    "note": (
                        "Provider availability was not checked. The concrete "
                        "world still maps to the providerWorldId."
                    ),
                },
            )

            with self._lock:
                self._provider_resolution_cache[cache_key] = copy.deepcopy(resolution)

            return copy.deepcopy(resolution)

        resolution = self._load_provider_world_definition(
            provider_world_id=provider_world_id,
            template_id=template_id,
            provider_id=provider_id,
            world=world_instance,
        )

        with self._lock:
            self._provider_resolution_cache[cache_key] = copy.deepcopy(resolution)

        if strict and not resolution.available:
            raise ProviderWorldResolutionError(
                provider_world_id,
                template_id=template_id,
                project_id=world_instance.project_id,
                universe_id=world_instance.universe_id,
                world_id=world_instance.world_id,
                details=resolution.to_dict(include_definition=False),
            )

        return copy.deepcopy(resolution)

    def require_provider_world(
        self,
        world: WorldInstanceDefinition | WorldRuntimeContext,
        *,
        refresh: bool = False,
    ) -> ProviderWorldResolution:
        """
        Resolve and require provider world availability.
        """
        return self.resolve_provider_world(
            world,
            require_available=True,
            refresh=refresh,
        )

    def _load_provider_world_definition(
        self,
        *,
        provider_world_id: str,
        template_id: str,
        provider_id: str,
        world: WorldInstanceDefinition,
    ) -> ProviderWorldResolution:
        diagnostics: dict[str, Any] = {
            "providerWorldId": provider_world_id,
            "templateId": template_id,
            "providerId": provider_id,
            "worldId": world.world_id,
            "projectId": world.project_id,
            "universeId": world.universe_id,
            "attempts": [],
        }

        # Attempt 1: src.world.service.get_default_world_service()
        service_module, service_import_error = _safe_import_module("src.world.service")
        diagnostics["attempts"].append(
            {
                "source": "import src.world.service",
                "ok": service_module is not None,
                "error": service_import_error,
            }
        )

        if service_module is not None:
            get_service = getattr(service_module, "get_default_world_service", None)
            if callable(get_service):
                world_service, service_error = _safe_call(get_service)
                diagnostics["attempts"].append(
                    {
                        "source": "src.world.service.get_default_world_service",
                        "ok": world_service is not None,
                        "error": service_error,
                    }
                )

                if world_service is not None:
                    for method_name in _PROVIDER_WORLD_METHOD_CANDIDATES:
                        method = getattr(world_service, method_name, None)

                        if not callable(method):
                            diagnostics["attempts"].append(
                                {
                                    "source": f"world_service.{method_name}",
                                    "ok": False,
                                    "error": "method_not_available",
                                }
                            )
                            continue

                        definition, method_error = _safe_call(method, provider_world_id)
                        definition_world_id = _extract_provider_world_id_from_definition(definition)

                        ok = definition is not None and (
                            definition_world_id is None
                            or definition_world_id == provider_world_id
                        )

                        diagnostics["attempts"].append(
                            {
                                "source": f"world_service.{method_name}",
                                "ok": ok,
                                "error": method_error,
                                "definitionWorldId": definition_world_id,
                            }
                        )

                        if ok:
                            return ProviderWorldResolution(
                                provider_world_id=provider_world_id,
                                template_id=template_id,
                                provider_id=provider_id,
                                world=copy.deepcopy(world),
                                provider_definition=definition,
                                available=True,
                                source=f"src.world.service.WorldService.{method_name}",
                                error=None,
                                diagnostics=diagnostics,
                            )

        # Attempt 2: module-level src.world.loader.get_world_definition()
        loader_module, loader_import_error = _safe_import_module("src.world.loader")
        diagnostics["attempts"].append(
            {
                "source": "import src.world.loader",
                "ok": loader_module is not None,
                "error": loader_import_error,
            }
        )

        if loader_module is not None:
            for method_name in ("get_world_definition", "load_world"):
                method = getattr(loader_module, method_name, None)

                if not callable(method):
                    diagnostics["attempts"].append(
                        {
                            "source": f"src.world.loader.{method_name}",
                            "ok": False,
                            "error": "method_not_available",
                        }
                    )
                    continue

                definition, method_error = _safe_call(method, provider_world_id)
                definition_world_id = _extract_provider_world_id_from_definition(definition)

                ok = definition is not None and (
                    definition_world_id is None
                    or definition_world_id == provider_world_id
                )

                diagnostics["attempts"].append(
                    {
                        "source": f"src.world.loader.{method_name}",
                        "ok": ok,
                        "error": method_error,
                        "definitionWorldId": definition_world_id,
                    }
                )

                if ok:
                    return ProviderWorldResolution(
                        provider_world_id=provider_world_id,
                        template_id=template_id,
                        provider_id=provider_id,
                        world=copy.deepcopy(world),
                        provider_definition=definition,
                        available=True,
                        source=f"src.world.loader.{method_name}",
                        error=None,
                        diagnostics=diagnostics,
                    )

        # Attempt 3: discovery fallback. This validates that the provider exists
        # even if the high-level service shape changes.
        discovery_module, discovery_import_error = _safe_import_module("src.world.discovery")
        diagnostics["attempts"].append(
            {
                "source": "import src.world.discovery",
                "ok": discovery_module is not None,
                "error": discovery_import_error,
            }
        )

        if discovery_module is not None:
            get_discovered_world = getattr(discovery_module, "get_discovered_world", None)
            if callable(get_discovered_world):
                discovered, discovery_error = _safe_call(get_discovered_world, provider_world_id)
                ok = discovered is not None

                diagnostics["attempts"].append(
                    {
                        "source": "src.world.discovery.get_discovered_world",
                        "ok": ok,
                        "error": discovery_error,
                    }
                )

                if ok:
                    return ProviderWorldResolution(
                        provider_world_id=provider_world_id,
                        template_id=template_id,
                        provider_id=provider_id,
                        world=copy.deepcopy(world),
                        provider_definition=discovered,
                        available=True,
                        source="src.world.discovery.get_discovered_world",
                        error=None,
                        diagnostics=diagnostics,
                    )

        return ProviderWorldResolution(
            provider_world_id=provider_world_id,
            template_id=template_id,
            provider_id=provider_id,
            world=copy.deepcopy(world),
            provider_definition=None,
            available=False,
            source=None,
            error="provider_world_not_available",
            diagnostics=diagnostics,
        )

    def assert_provider_world_exists(
        self,
        world: WorldInstanceDefinition | WorldRuntimeContext,
    ) -> bool:
        """
        Require that the provider/template world is resolvable.
        """
        resolution = self.require_provider_world(world)

        if not resolution.available:
            raise ProviderWorldNotFoundError(
                resolution.provider_world_id,
                template_id=resolution.template_id,
                project_id=resolution.world.project_id,
                universe_id=resolution.world.universe_id,
                world_id=resolution.world.world_id,
                details=resolution.to_dict(),
            )

        return True

    def resolve_project_bootstrap_parts(
        self,
        project_id: str | None = None,
        *,
        allow_default_project: bool = False,
    ) -> dict[str, Any]:
        """
        Resolve the raw parts needed to create a project bootstrap context.

        bootstrap.py will use this to build the final bootstrap object.
        """
        project = self.resolve_project(
            project_id,
            allow_default=allow_default_project,
        )
        universe = self.resolve_default_universe(project.project_id)
        default_world = self.resolve_world(
            project.project_id,
            universe.default_world_id,
            universe_id=universe.universe_id,
        )
        spawn_world = self.resolve_world(
            project.project_id,
            universe.spawn_world_id,
            universe_id=universe.universe_id,
        )
        worlds = self.list_project_worlds(
            project.project_id,
            universe_id=universe.universe_id,
        )

        return {
            "project": project,
            "universe": universe,
            "defaultWorld": default_world,
            "spawnWorld": spawn_world,
            "worlds": worlds,
        }

    def get_status(
        self,
        *,
        refresh: bool = False,
        include_catalog: bool = False,
        include_provider_checks: bool = False,
    ) -> dict[str, Any]:
        """
        Return JSON-safe resolver diagnostics.
        """
        with self._lock:
            if self._status_cache is not None and not refresh and not include_catalog and not include_provider_checks:
                return copy.deepcopy(self._status_cache)

        status_payload: dict[str, Any] = {
            "ok": False,
            "source": RESOLVER_SOURCE,
            "moduleVersion": RESOLVER_MODULE_VERSION,
            "catalogHash": None,
            "defaultIds": None,
            "counts": {
                "projects": 0,
                "universes": 0,
                "worlds": 0,
            },
            "providerCheckEnabled": self._provider_check_enabled,
            "strictProviderResolution": self._strict_provider_resolution,
            "providerResolutionCacheSize": 0,
            "error": None,
            "metadata": deep_copy_json(self._metadata),
            "catalog": None,
            "providerChecks": None,
            "defaultsStatus": None,
        }

        try:
            with self._lock:
                catalog = copy.deepcopy(self._catalog)
                provider_cache_size = len(self._provider_resolution_cache)

            status_payload["ok"] = True
            status_payload["catalogHash"] = catalog.catalog_hash()
            status_payload["defaultIds"] = {
                "defaultProjectId": catalog.default_project_id,
                **get_default_world_state_ids(refresh=False),
            }
            status_payload["counts"] = {
                "projects": len(catalog.projects),
                "universes": len(catalog.universes),
                "worlds": len(catalog.worlds),
            }
            status_payload["providerResolutionCacheSize"] = provider_cache_size
            status_payload["defaultsStatus"] = get_default_world_state_status(
                refresh=False,
                include_catalog=False,
            )

            if include_catalog:
                status_payload["catalog"] = catalog.to_dict()

            if include_provider_checks:
                provider_checks = []

                for world in catalog.worlds:
                    try:
                        resolution = self.resolve_provider_world(
                            world,
                            require_available=False,
                            refresh=refresh,
                        )
                        provider_checks.append(
                            resolution.to_dict(include_definition=False)
                        )
                    except Exception as exc:
                        provider_checks.append(
                            {
                                "worldId": world.world_id,
                                "projectId": world.project_id,
                                "universeId": world.universe_id,
                                "providerWorldId": world.provider_world_id,
                                "ok": False,
                                "error": _safe_exception_message(exc),
                            }
                        )

                status_payload["providerChecks"] = provider_checks

        except Exception as exc:
            coerced = coerce_world_state_error(exc)
            status_payload["ok"] = False
            status_payload["error"] = coerced.to_dict(include_debug=True)

        safe_status = make_json_safe(status_payload)

        with self._lock:
            if not include_catalog and not include_provider_checks:
                self._status_cache = copy.deepcopy(safe_status)

        return copy.deepcopy(safe_status)

    def to_dict(self) -> dict[str, Any]:
        """
        Return a compact resolver dictionary.
        """
        return {
            "source": RESOLVER_SOURCE,
            "moduleVersion": RESOLVER_MODULE_VERSION,
            "providerCheckEnabled": self._provider_check_enabled,
            "strictProviderResolution": self._strict_provider_resolution,
            "catalogHash": self.get_catalog_hash(),
            "defaultProjectId": self.get_default_project_id(),
            "metadata": deep_copy_json(self._metadata),
        }


def create_default_world_state_resolver(
    *,
    refresh_catalog: bool = False,
    provider_check_enabled: bool = True,
    strict_provider_resolution: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> WorldStateResolver:
    """
    Create a resolver backed by the default in-memory catalog.
    """
    catalog = get_default_world_state_catalog(refresh=refresh_catalog)

    return WorldStateResolver(
        catalog=catalog,
        provider_check_enabled=provider_check_enabled,
        strict_provider_resolution=strict_provider_resolution,
        metadata={
            "source": RESOLVER_SOURCE,
            "createdFrom": "create_default_world_state_resolver",
            **deep_copy_json(metadata or {}),
        },
    )


def get_default_world_state_resolver(
    *,
    refresh: bool = False,
    refresh_catalog: bool = False,
    provider_check_enabled: bool = True,
    strict_provider_resolution: bool = False,
) -> WorldStateResolver:
    """
    Return the cached default world-state resolver.

    Use `refresh=True` after changing environment variables or defaults.
    """
    global _default_world_state_resolver_cache

    with _default_resolver_lock:
        if _default_world_state_resolver_cache is not None and not refresh and not refresh_catalog:
            cached = _default_world_state_resolver_cache

            # If callers request stricter settings than the cached resolver,
            # recreate it. This avoids silently ignoring provider checks.
            if (
                cached.provider_check_enabled == bool(provider_check_enabled)
                and cached.strict_provider_resolution == bool(strict_provider_resolution)
            ):
                return cached

        _default_world_state_resolver_cache = create_default_world_state_resolver(
            refresh_catalog=refresh or refresh_catalog,
            provider_check_enabled=provider_check_enabled,
            strict_provider_resolution=strict_provider_resolution,
        )

        return _default_world_state_resolver_cache


def reset_default_world_state_resolver_cache(
    *,
    reset_catalog_cache: bool = False,
) -> None:
    """
    Reset the cached default resolver.

    Args:
        reset_catalog_cache:
            Also reset the defaults.py catalog/settings cache.
    """
    global _default_world_state_resolver_cache

    with _default_resolver_lock:
        if _default_world_state_resolver_cache is not None:
            try:
                _default_world_state_resolver_cache.reset_caches()
            except Exception:
                pass

        _default_world_state_resolver_cache = None

        if reset_catalog_cache:
            reset_default_world_state_catalog_cache()


def resolve_project(
    project_id: str | None = None,
    *,
    allow_default: bool = False,
) -> ProjectRuntimeContext:
    """
    Convenience function using the default resolver.
    """
    resolver = get_default_world_state_resolver()

    return resolver.resolve_project(
        project_id,
        allow_default=allow_default,
    )


def resolve_default_universe(
    project_id: str | None = None,
    *,
    allow_default_project: bool = False,
) -> UniverseRuntimeContext:
    """
    Convenience function using the default resolver.
    """
    resolver = get_default_world_state_resolver()

    return resolver.resolve_default_universe(
        project_id,
        allow_default_project=allow_default_project,
    )


def resolve_world(
    project_id: str,
    world_id: str,
    *,
    universe_id: str | None = None,
) -> WorldInstanceDefinition:
    """
    Convenience function using the default resolver.
    """
    resolver = get_default_world_state_resolver()

    return resolver.resolve_world(
        project_id,
        world_id,
        universe_id=universe_id,
    )


def resolve_world_runtime_context(
    project_id: str,
    world_id: str,
    *,
    universe_id: str | None = None,
) -> WorldRuntimeContext:
    """
    Convenience function using the default resolver.
    """
    resolver = get_default_world_state_resolver()

    return resolver.resolve_world_runtime_context(
        project_id,
        world_id,
        universe_id=universe_id,
    )


def resolve_spawn_world_runtime_context(
    project_id: str | None = None,
    *,
    allow_default_project: bool = False,
) -> WorldRuntimeContext:
    """
    Convenience function using the default resolver.
    """
    resolver = get_default_world_state_resolver()

    return resolver.resolve_spawn_world_runtime_context(
        project_id,
        allow_default_project=allow_default_project,
    )


def get_world_state_resolver_status(
    *,
    refresh: bool = False,
    include_catalog: bool = False,
    include_provider_checks: bool = False,
) -> dict[str, Any]:
    """
    Convenience status function using the default resolver.
    """
    resolver = get_default_world_state_resolver(refresh=refresh)

    return resolver.get_status(
        refresh=refresh,
        include_catalog=include_catalog,
        include_provider_checks=include_provider_checks,
    )


def assert_world_state_resolver_ready(
    *,
    refresh: bool = False,
    require_provider_worlds: bool = False,
) -> WorldStateResolver:
    """
    Validate that the default resolver can resolve the default project and world.

    Raises RuntimeError if not ready.
    """
    resolver = get_default_world_state_resolver(refresh=refresh)

    try:
        context = resolver.resolve_spawn_world_runtime_context(
            allow_default_project=True,
        )

        if require_provider_worlds:
            resolver.require_provider_world(context)

    except Exception as exc:
        raise RuntimeError(
            f"World-state resolver is not ready: {_safe_exception_message(exc)}"
        ) from exc

    return resolver


__all__ = (
    "RESOLVER_MODULE_VERSION",
    "RESOLVER_SOURCE",
    "ProviderWorldResolution",
    "ProjectResolution",
    "ResolverStatus",
    "WorldStateResolver",
    "create_default_world_state_resolver",
    "get_default_world_state_resolver",
    "reset_default_world_state_resolver_cache",
    "resolve_project",
    "resolve_default_universe",
    "resolve_world",
    "resolve_world_runtime_context",
    "resolve_spawn_world_runtime_context",
    "get_world_state_resolver_status",
    "assert_world_state_resolver_ready",
)