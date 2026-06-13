# services/vectoplan-chunk/src/world_state/__init__.py
"""
VECTOPLAN Chunk Service - World State Package.

This package is the project/universe/world-instance layer above `src.world`.

Important separation:

- `src.world`
  Owns provider discovery, world templates, generator definitions,
  flat-world generation and low-level generated chunk payloads.

- `src.world_state`
  Owns project-scoped runtime state and compatibility helpers:
  project -> universe -> concrete world instance -> provider/template world.

Current DB-backed runtime hierarchy:

    Project
      -> Universe
          -> WorldInstance
              -> ChunkSnapshot
              -> WorldCommandLog
              -> ChunkEvent

Current default mapping:

    dev-project
    -> dev-universe
    -> world_spawn
    -> template/provider world: flat

Important:
- This package remains safe to import during partial development.
- Public symbols are exposed lazily.
- PostgreSQL-backed models live in top-level `models/`.
- Older in-memory `src.world_state.models/defaults/resolver` modules can still
  exist as compatibility/diagnostic modules.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import os
import threading
from pathlib import Path
from types import ModuleType
from typing import Any


PACKAGE_NAME = __name__
PACKAGE_LABEL = "VECTOPLAN World State"
PACKAGE_VERSION = "0.2.0"

PACKAGE_DESCRIPTION = (
    "Project/universe/world-instance state layer for the VECTOPLAN chunk service."
)

PACKAGE_MODE = "postgres-backed-world-state"

DEFAULT_PROJECT_ID_ENV = "VECTOPLAN_CHUNK_DEFAULT_PROJECT_ID"
DEFAULT_UNIVERSE_ID_ENV = "VECTOPLAN_CHUNK_DEFAULT_UNIVERSE_ID"
DEFAULT_INSTANCE_WORLD_ID_ENV = "VECTOPLAN_CHUNK_DEFAULT_INSTANCE_WORLD_ID"
DEFAULT_WORLD_TEMPLATE_ID_ENV = "VECTOPLAN_CHUNK_DEFAULT_WORLD_TEMPLATE_ID"
DEFAULT_PROVIDER_WORLD_ID_ENV = "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_WORLD_ID"
DEFAULT_PROVIDER_ID_ENV = "VECTOPLAN_CHUNK_DEFAULT_PROVIDER_ID"
DEFAULT_BLOCK_REGISTRY_ID_ENV = "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_ID"
DEFAULT_BLOCK_REGISTRY_VERSION_ENV = "VECTOPLAN_CHUNK_DEFAULT_BLOCK_REGISTRY_VERSION"

DEFAULT_PROJECT_ID = "dev-project"
DEFAULT_UNIVERSE_ID = "dev-universe"
DEFAULT_INSTANCE_WORLD_ID = "world_spawn"
DEFAULT_WORLD_TEMPLATE_ID = "flat"
DEFAULT_PROVIDER_WORLD_ID = "flat"
DEFAULT_PROVIDER_ID = "flat"
DEFAULT_BLOCK_REGISTRY_ID = "debug-blocks"
DEFAULT_BLOCK_REGISTRY_VERSION = "1"

EXPECTED_CORE_MODULES: tuple[str, ...] = (
    "errors",
    "service",
    "bootstrap",
    "serializer",
)

LEGACY_COMPAT_MODULES: tuple[str, ...] = (
    "models",
    "defaults",
    "resolver",
)

OPTIONAL_FUTURE_MODULES: tuple[str, ...] = (
    "repositories",
    "commands",
    "snapshots",
    "events",
)

MODULE_IMPORT_PATHS: dict[str, str] = {
    module_name: f"{PACKAGE_NAME}.{module_name}"
    for module_name in EXPECTED_CORE_MODULES + LEGACY_COMPAT_MODULES + OPTIONAL_FUTURE_MODULES
}

_PUBLIC_SYMBOLS: dict[str, tuple[str, str]] = {
    # errors.py
    "WorldStateError": (f"{PACKAGE_NAME}.errors", "WorldStateError"),
    "WorldStateConfigError": (f"{PACKAGE_NAME}.errors", "WorldStateConfigError"),
    "WorldStateCatalogError": (f"{PACKAGE_NAME}.errors", "WorldStateCatalogError"),
    "WorldStateResolutionError": (f"{PACKAGE_NAME}.errors", "WorldStateResolutionError"),
    "WorldStateBootstrapError": (f"{PACKAGE_NAME}.errors", "WorldStateBootstrapError"),
    "WorldStateSerializationError": (f"{PACKAGE_NAME}.errors", "WorldStateSerializationError"),
    "WorldStateProviderError": (f"{PACKAGE_NAME}.errors", "WorldStateProviderError"),
    "InvalidWorldStatePayloadError": (f"{PACKAGE_NAME}.errors", "InvalidWorldStatePayloadError"),
    "InvalidWorldStateContextError": (f"{PACKAGE_NAME}.errors", "InvalidWorldStateContextError"),
    "ProjectNotFoundError": (f"{PACKAGE_NAME}.errors", "ProjectNotFoundError"),
    "UniverseNotFoundError": (f"{PACKAGE_NAME}.errors", "UniverseNotFoundError"),
    "WorldInstanceNotFoundError": (f"{PACKAGE_NAME}.errors", "WorldInstanceNotFoundError"),
    "InvalidProjectUniverseBindingError": (
        f"{PACKAGE_NAME}.errors",
        "InvalidProjectUniverseBindingError",
    ),
    "InvalidProjectWorldBindingError": (
        f"{PACKAGE_NAME}.errors",
        "InvalidProjectWorldBindingError",
    ),
    "InvalidUniverseWorldBindingError": (
        f"{PACKAGE_NAME}.errors",
        "InvalidUniverseWorldBindingError",
    ),
    "WorldTemplateNotFoundError": (
        f"{PACKAGE_NAME}.errors",
        "WorldTemplateNotFoundError",
    ),
    "ProviderWorldNotFoundError": (
        f"{PACKAGE_NAME}.errors",
        "ProviderWorldNotFoundError",
    ),
    "ProviderWorldResolutionError": (
        f"{PACKAGE_NAME}.errors",
        "ProviderWorldResolutionError",
    ),
    "coerce_world_state_error": (
        f"{PACKAGE_NAME}.errors",
        "coerce_world_state_error",
    ),
    "error_to_api_response_body": (
        f"{PACKAGE_NAME}.errors",
        "error_to_api_response_body",
    ),
    "error_to_response_tuple": (
        f"{PACKAGE_NAME}.errors",
        "error_to_response_tuple",
    ),
    "error_to_log_dict": (
        f"{PACKAGE_NAME}.errors",
        "error_to_log_dict",
    ),
    "make_json_safe": (
        f"{PACKAGE_NAME}.errors",
        "make_json_safe",
    ),

    # service.py
    "ChunkCoordinates": (
        f"{PACKAGE_NAME}.service",
        "ChunkCoordinates",
    ),
    "DbWorldRuntimeContext": (
        f"{PACKAGE_NAME}.service",
        "DbWorldRuntimeContext",
    ),
    "DbProjectBootstrapContext": (
        f"{PACKAGE_NAME}.service",
        "DbProjectBootstrapContext",
    ),
    "SimpleProviderWorldResolution": (
        f"{PACKAGE_NAME}.service",
        "SimpleProviderWorldResolution",
    ),
    "WorldStateChunkResult": (
        f"{PACKAGE_NAME}.service",
        "WorldStateChunkResult",
    ),
    "WorldStateChunkBatchResult": (
        f"{PACKAGE_NAME}.service",
        "WorldStateChunkBatchResult",
    ),
    "WorldStateBlocksResult": (
        f"{PACKAGE_NAME}.service",
        "WorldStateBlocksResult",
    ),
    "WorldStateWorldMetadataResult": (
        f"{PACKAGE_NAME}.service",
        "WorldStateWorldMetadataResult",
    ),
    "WorldProviderAdapter": (
        f"{PACKAGE_NAME}.service",
        "WorldProviderAdapter",
    ),
    "WorldStateService": (
        f"{PACKAGE_NAME}.service",
        "WorldStateService",
    ),
    "normalize_chunk_coordinates": (
        f"{PACKAGE_NAME}.service",
        "normalize_chunk_coordinates",
    ),
    "normalize_chunk_coordinate_items": (
        f"{PACKAGE_NAME}.service",
        "normalize_chunk_coordinate_items",
    ),
    "create_default_world_state_service": (
        f"{PACKAGE_NAME}.service",
        "create_default_world_state_service",
    ),
    "get_default_world_state_service": (
        f"{PACKAGE_NAME}.service",
        "get_default_world_state_service",
    ),
    "reset_default_world_state_service_cache": (
        f"{PACKAGE_NAME}.service",
        "reset_default_world_state_service_cache",
    ),
    "get_world_state_service_status": (
        f"{PACKAGE_NAME}.service",
        "get_world_state_service_status",
    ),
    "assert_world_state_service_ready": (
        f"{PACKAGE_NAME}.service",
        "assert_world_state_service_ready",
    ),

    # bootstrap.py
    "ProjectBootstrapOptions": (
        f"{PACKAGE_NAME}.bootstrap",
        "ProjectBootstrapOptions",
    ),
    "ProjectBootstrapBuildResult": (
        f"{PACKAGE_NAME}.bootstrap",
        "ProjectBootstrapBuildResult",
    ),
    "create_bootstrap_options_from_env": (
        f"{PACKAGE_NAME}.bootstrap",
        "create_bootstrap_options_from_env",
    ),
    "build_project_bootstrap": (
        f"{PACKAGE_NAME}.bootstrap",
        "build_project_bootstrap",
    ),
    "create_project_bootstrap": (
        f"{PACKAGE_NAME}.bootstrap",
        "create_project_bootstrap",
    ),
    "get_project_bootstrap_result": (
        f"{PACKAGE_NAME}.bootstrap",
        "get_project_bootstrap_result",
    ),
    "create_default_project_bootstrap": (
        f"{PACKAGE_NAME}.bootstrap",
        "create_default_project_bootstrap",
    ),
    "serialize_project_bootstrap_result": (
        f"{PACKAGE_NAME}.bootstrap",
        "serialize_project_bootstrap_result",
    ),
    "get_project_bootstrap_cache_status": (
        f"{PACKAGE_NAME}.bootstrap",
        "get_project_bootstrap_cache_status",
    ),
    "reset_project_bootstrap_cache": (
        f"{PACKAGE_NAME}.bootstrap",
        "reset_project_bootstrap_cache",
    ),
    "get_bootstrap_status": (
        f"{PACKAGE_NAME}.bootstrap",
        "get_bootstrap_status",
    ),
    "assert_project_bootstrap_ready": (
        f"{PACKAGE_NAME}.bootstrap",
        "assert_project_bootstrap_ready",
    ),

    # serializer.py
    "serialize_cell_encoding": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_cell_encoding",
    ),
    "serialize_air_block": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_air_block",
    ),
    "serialize_palette": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_palette",
    ),
    "serialize_project": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_project",
    ),
    "serialize_universe": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_universe",
    ),
    "serialize_world_instance": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_world_instance",
    ),
    "serialize_world_runtime_context": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_world_runtime_context",
    ),
    "serialize_world_instance_list": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_world_instance_list",
    ),
    "serialize_world_state_chunk": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_world_state_chunk",
    ),
    "serialize_world_state_chunk_response": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_world_state_chunk_response",
    ),
    "serialize_world_state_chunk_batch_response": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_world_state_chunk_batch_response",
    ),
    "serialize_world_state_blocks_response": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_world_state_blocks_response",
    ),
    "serialize_world_state_world_metadata_response": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_world_state_world_metadata_response",
    ),
    "serialize_project_bootstrap": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_project_bootstrap",
    ),
    "serialize_project_bootstrap_response": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_project_bootstrap_response",
    ),
    "serialize_error_response": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_error_response",
    ),
    "serialize_error_response_tuple": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_error_response_tuple",
    ),
    "serialize_ok_response": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_ok_response",
    ),
    "serialize_health_response": (
        f"{PACKAGE_NAME}.serializer",
        "serialize_health_response",
    ),
    "get_serializer_status": (
        f"{PACKAGE_NAME}.serializer",
        "get_serializer_status",
    ),
    "reset_serializer_status_cache": (
        f"{PACKAGE_NAME}.serializer",
        "reset_serializer_status_cache",
    ),
    "export_serialized_json": (
        f"{PACKAGE_NAME}.serializer",
        "export_serialized_json",
    ),

    # Legacy compatibility: models.py
    "ProjectRuntimeContext": (
        f"{PACKAGE_NAME}.models",
        "ProjectRuntimeContext",
    ),
    "UniverseRuntimeContext": (
        f"{PACKAGE_NAME}.models",
        "UniverseRuntimeContext",
    ),
    "WorldInstanceDefinition": (
        f"{PACKAGE_NAME}.models",
        "WorldInstanceDefinition",
    ),
    "WorldRuntimeContext": (
        f"{PACKAGE_NAME}.models",
        "WorldRuntimeContext",
    ),
    "ProjectBootstrapContext": (
        f"{PACKAGE_NAME}.models",
        "ProjectBootstrapContext",
    ),
    "WorldStateCatalog": (
        f"{PACKAGE_NAME}.models",
        "WorldStateCatalog",
    ),
    "normalize_project_id": (
        f"{PACKAGE_NAME}.models",
        "normalize_project_id",
    ),
    "normalize_universe_id": (
        f"{PACKAGE_NAME}.models",
        "normalize_universe_id",
    ),
    "normalize_world_instance_id": (
        f"{PACKAGE_NAME}.models",
        "normalize_world_instance_id",
    ),

    # Legacy compatibility: defaults.py
    "create_default_world_state_catalog": (
        f"{PACKAGE_NAME}.defaults",
        "create_default_world_state_catalog",
    ),
    "get_default_world_state_catalog": (
        f"{PACKAGE_NAME}.defaults",
        "get_default_world_state_catalog",
    ),
    "reset_default_world_state_catalog_cache": (
        f"{PACKAGE_NAME}.defaults",
        "reset_default_world_state_catalog_cache",
    ),

    # Legacy compatibility: resolver.py
    "ProviderWorldResolution": (
        f"{PACKAGE_NAME}.resolver",
        "ProviderWorldResolution",
    ),
    "WorldStateResolver": (
        f"{PACKAGE_NAME}.resolver",
        "WorldStateResolver",
    ),
    "create_default_world_state_resolver": (
        f"{PACKAGE_NAME}.resolver",
        "create_default_world_state_resolver",
    ),
    "get_default_world_state_resolver": (
        f"{PACKAGE_NAME}.resolver",
        "get_default_world_state_resolver",
    ),
    "reset_default_world_state_resolver_cache": (
        f"{PACKAGE_NAME}.resolver",
        "reset_default_world_state_resolver_cache",
    ),
}

_EXPLICIT_PUBLIC_NAMES: tuple[str, ...] = (
    "PACKAGE_NAME",
    "PACKAGE_LABEL",
    "PACKAGE_VERSION",
    "PACKAGE_DESCRIPTION",
    "PACKAGE_MODE",
    "DEFAULT_PROJECT_ID",
    "DEFAULT_UNIVERSE_ID",
    "DEFAULT_INSTANCE_WORLD_ID",
    "DEFAULT_WORLD_TEMPLATE_ID",
    "DEFAULT_PROVIDER_WORLD_ID",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_BLOCK_REGISTRY_ID",
    "DEFAULT_BLOCK_REGISTRY_VERSION",
    "DEFAULT_PROJECT_ID_ENV",
    "DEFAULT_UNIVERSE_ID_ENV",
    "DEFAULT_INSTANCE_WORLD_ID_ENV",
    "DEFAULT_WORLD_TEMPLATE_ID_ENV",
    "DEFAULT_PROVIDER_WORLD_ID_ENV",
    "DEFAULT_PROVIDER_ID_ENV",
    "DEFAULT_BLOCK_REGISTRY_ID_ENV",
    "DEFAULT_BLOCK_REGISTRY_VERSION_ENV",
    "EXPECTED_CORE_MODULES",
    "LEGACY_COMPAT_MODULES",
    "OPTIONAL_FUTURE_MODULES",
    "MODULE_IMPORT_PATHS",
    "get_world_state_package_dir",
    "get_configured_default_ids",
    "get_world_state_package_status",
    "is_world_state_package_ready",
    "require_world_state_package_ready",
    "reset_world_state_package_status_cache",
    "get_public_symbol_map",
    "get_public_symbol_status",
    "import_world_state_module",
)

__all__ = tuple(
    dict.fromkeys(
        (
            *_EXPLICIT_PUBLIC_NAMES,
            *_PUBLIC_SYMBOLS.keys(),
        )
    )
)

_status_cache_lock = threading.RLock()
_world_state_package_status_cache: dict[tuple[bool, bool], dict[str, Any]] = {}
_public_symbol_map_cache: dict[str, tuple[str, str]] | None = None


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------

def get_world_state_package_dir() -> Path:
    """
    Return the absolute filesystem path of this package directory.

    This function never imports child modules.
    """
    return Path(__file__).resolve().parent


def _get_env_value(name: str, fallback: str) -> str:
    """
    Read an environment value defensively.

    Empty strings are treated as missing values.
    """
    try:
        value = os.environ.get(name)
    except Exception:
        value = None

    if value is None:
        return fallback

    value = str(value).strip()
    return value or fallback


def get_configured_default_ids() -> dict[str, str]:
    """
    Return configured default IDs for the DB-backed world-state context.

    These values intentionally distinguish concrete runtime instance IDs from
    provider/template IDs.
    """
    return {
        "defaultProjectId": _get_env_value(
            DEFAULT_PROJECT_ID_ENV,
            DEFAULT_PROJECT_ID,
        ),
        "defaultUniverseId": _get_env_value(
            DEFAULT_UNIVERSE_ID_ENV,
            DEFAULT_UNIVERSE_ID,
        ),
        "defaultInstanceWorldId": _get_env_value(
            DEFAULT_INSTANCE_WORLD_ID_ENV,
            DEFAULT_INSTANCE_WORLD_ID,
        ),
        "defaultWorldTemplateId": _get_env_value(
            DEFAULT_WORLD_TEMPLATE_ID_ENV,
            DEFAULT_WORLD_TEMPLATE_ID,
        ),
        "defaultProviderWorldId": _get_env_value(
            DEFAULT_PROVIDER_WORLD_ID_ENV,
            DEFAULT_PROVIDER_WORLD_ID,
        ),
        "defaultProviderId": _get_env_value(
            DEFAULT_PROVIDER_ID_ENV,
            DEFAULT_PROVIDER_ID,
        ),
        "defaultBlockRegistryId": _get_env_value(
            DEFAULT_BLOCK_REGISTRY_ID_ENV,
            DEFAULT_BLOCK_REGISTRY_ID,
        ),
        "defaultBlockRegistryVersion": _get_env_value(
            DEFAULT_BLOCK_REGISTRY_VERSION_ENV,
            DEFAULT_BLOCK_REGISTRY_VERSION,
        ),
    }


def _safe_exception_message(exc: BaseException | Any) -> str:
    try:
        message = str(exc)
    except Exception:
        message = exc.__class__.__name__

    return message or exc.__class__.__name__


def _safe_find_spec(module_path: str) -> tuple[bool, str | None]:
    """
    Return whether a module can be found without importing it.
    """
    try:
        spec = importlib.util.find_spec(module_path)
    except Exception as exc:
        return False, _safe_exception_message(exc)

    if spec is None:
        return False, None

    return True, None


def _safe_import_module(module_path: str) -> tuple[ModuleType | None, str | None]:
    """
    Try importing a module and return `(module, error)`.
    """
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        return None, _safe_exception_message(exc)

    return module, None


def _module_file_path(module_name: str) -> Path:
    return get_world_state_package_dir() / f"{module_name}.py"


def _module_group(module_name: str) -> str:
    if module_name in EXPECTED_CORE_MODULES:
        return "core"
    if module_name in LEGACY_COMPAT_MODULES:
        return "legacy_compat"
    if module_name in OPTIONAL_FUTURE_MODULES:
        return "optional_future"
    return "unknown"


def _inspect_module(module_name: str, *, check_importable: bool) -> dict[str, Any]:
    module_path = MODULE_IMPORT_PATHS.get(module_name, f"{PACKAGE_NAME}.{module_name}")
    file_path = _module_file_path(module_name)

    spec_found, spec_error = _safe_find_spec(module_path)

    importable: bool | None = None
    import_error: str | None = None

    if check_importable:
        module, import_error = _safe_import_module(module_path)
        importable = module is not None

    return {
        "moduleName": module_name,
        "modulePath": module_path,
        "group": _module_group(module_name),
        "filePath": str(file_path),
        "fileExists": file_path.exists(),
        "specFound": spec_found,
        "specError": spec_error,
        "importChecked": check_importable,
        "importable": importable,
        "importError": import_error,
    }


def _inspect_postgres_model_package(*, check_importable: bool) -> dict[str, Any]:
    """
    Inspect top-level `models` package availability.

    The DB-backed world-state layer depends on top-level SQLAlchemy models.
    """
    model_package = "models"
    spec_found, spec_error = _safe_find_spec(model_package)

    imported: bool | None = None
    import_error: str | None = None
    summary: dict[str, Any] | None = None

    if check_importable:
        module, import_error = _safe_import_module(model_package)
        imported = module is not None

        if module is not None:
            get_summary = getattr(module, "get_model_debug_summary", None)
            if callable(get_summary):
                try:
                    summary = get_summary()
                except Exception as exc:
                    summary = {
                        "error": _safe_exception_message(exc),
                    }

    return {
        "package": model_package,
        "specFound": spec_found,
        "specError": spec_error,
        "importChecked": check_importable,
        "imported": imported,
        "importError": import_error,
        "summary": summary,
    }


# -----------------------------------------------------------------------------
# Package diagnostics
# -----------------------------------------------------------------------------

def get_world_state_package_status(
    *,
    refresh: bool = False,
    check_importable: bool = False,
    include_legacy: bool = True,
) -> dict[str, Any]:
    """
    Return a JSON-safe diagnostic status for the world-state package.

    The result is cached by `(check_importable, include_legacy)`.

    `ready` means:
        package directory and this __init__.py file exist.

    `strictReady` means:
        expected core module files exist and, if requested, are importable.

    Legacy modules are reported but do not block `strictReady`.
    """
    cache_key = (bool(check_importable), bool(include_legacy))

    with _status_cache_lock:
        if not refresh and cache_key in _world_state_package_status_cache:
            return copy.deepcopy(_world_state_package_status_cache[cache_key])

        package_dir = get_world_state_package_dir()
        package_init_path = package_dir / "__init__.py"

        core_modules = [
            _inspect_module(module_name, check_importable=check_importable)
            for module_name in EXPECTED_CORE_MODULES
        ]

        legacy_modules = (
            [
                _inspect_module(module_name, check_importable=check_importable)
                for module_name in LEGACY_COMPAT_MODULES
            ]
            if include_legacy
            else []
        )

        optional_modules = [
            _inspect_module(module_name, check_importable=check_importable)
            for module_name in OPTIONAL_FUTURE_MODULES
        ]

        missing_core_files = [
            module_status["moduleName"]
            for module_status in core_modules
            if not module_status["fileExists"]
        ]

        missing_core_specs = [
            module_status["moduleName"]
            for module_status in core_modules
            if not module_status["specFound"]
        ]

        core_import_errors = [
            {
                "moduleName": module_status["moduleName"],
                "modulePath": module_status["modulePath"],
                "error": module_status["importError"],
            }
            for module_status in core_modules
            if module_status["importError"]
        ]

        package_ready = package_dir.exists() and package_init_path.exists()
        all_core_files_exist = len(missing_core_files) == 0
        all_core_specs_found = len(missing_core_specs) == 0

        if check_importable:
            all_core_importable = all(
                bool(module_status["importable"])
                for module_status in core_modules
            )
        else:
            all_core_importable = None

        strict_ready = (
            package_ready
            and all_core_files_exist
            and all_core_specs_found
            and (
                all_core_importable is True
                if check_importable
                else True
            )
        )

        postgres_models = _inspect_postgres_model_package(check_importable=check_importable)

        status = {
            "ok": package_ready,
            "ready": package_ready,
            "strictReady": strict_ready,
            "packageName": PACKAGE_NAME,
            "packageLabel": PACKAGE_LABEL,
            "packageVersion": PACKAGE_VERSION,
            "packageMode": PACKAGE_MODE,
            "description": PACKAGE_DESCRIPTION,
            "packageDir": str(package_dir),
            "packageExists": package_dir.exists(),
            "packageInitPath": str(package_init_path),
            "packageInitExists": package_init_path.exists(),
            "expectedCoreModules": core_modules,
            "legacyCompatModules": legacy_modules,
            "optionalFutureModules": optional_modules,
            "postgresModels": postgres_models,
            "missingExpectedFiles": missing_core_files,
            "missingExpectedSpecs": missing_core_specs,
            "expectedImportErrors": core_import_errors,
            "allExpectedFilesExist": all_core_files_exist,
            "allExpectedSpecsFound": all_core_specs_found,
            "allExpectedImportable": all_core_importable,
            "checkImportable": check_importable,
            "includeLegacy": include_legacy,
            "configuredDefaults": get_configured_default_ids(),
            "invariants": {
                "projectContainsUniverses": True,
                "universeContainsWorlds": True,
                "worldSpawnIsConcreteWorldInstance": True,
                "flatIsProviderTemplateWorld": True,
                "chunkSnapshotIsLoadTruth": True,
                "chunkEventIsHistoricalTruth": True,
                "eventReplayIsNotNormalLoadPath": True,
            },
        }

        _world_state_package_status_cache[cache_key] = copy.deepcopy(status)
        return copy.deepcopy(status)


def is_world_state_package_ready(
    *,
    strict: bool = False,
    refresh: bool = False,
    check_importable: bool = False,
    include_legacy: bool = True,
) -> bool:
    """
    Return whether the package is ready.

    By default this only checks that the package itself exists. Use
    `strict=True` once core files are expected to be present.
    """
    status = get_world_state_package_status(
        refresh=refresh,
        check_importable=check_importable,
        include_legacy=include_legacy,
    )

    if strict:
        return bool(status.get("strictReady"))

    return bool(status.get("ready"))


def require_world_state_package_ready(
    *,
    strict: bool = False,
    check_importable: bool = False,
    include_legacy: bool = True,
) -> dict[str, Any]:
    """
    Require the world-state package to be ready and return its status.
    """
    status = get_world_state_package_status(
        refresh=True,
        check_importable=check_importable,
        include_legacy=include_legacy,
    )

    ready_key = "strictReady" if strict else "ready"

    if bool(status.get(ready_key)):
        return status

    details = {
        "packageName": status.get("packageName"),
        "packageDir": status.get("packageDir"),
        "strict": strict,
        "checkImportable": check_importable,
        "includeLegacy": include_legacy,
        "missingExpectedFiles": status.get("missingExpectedFiles") or [],
        "expectedImportErrors": status.get("expectedImportErrors") or [],
        "postgresModels": status.get("postgresModels"),
    }

    raise RuntimeError(
        f"World-state package is not ready: {details}"
    )


def reset_world_state_package_status_cache() -> None:
    """
    Reset cached package diagnostics.

    Useful in tests and during local development after adding new files.
    """
    global _public_symbol_map_cache

    with _status_cache_lock:
        _world_state_package_status_cache.clear()
        _public_symbol_map_cache = None


# -----------------------------------------------------------------------------
# Public symbol diagnostics / lazy import
# -----------------------------------------------------------------------------

def get_public_symbol_map(*, refresh: bool = False) -> dict[str, tuple[str, str]]:
    """
    Return the lazy public symbol map.

    Format:
        symbolName -> (modulePath, attributeName)
    """
    global _public_symbol_map_cache

    with _status_cache_lock:
        if _public_symbol_map_cache is not None and not refresh:
            return copy.deepcopy(_public_symbol_map_cache)

        _public_symbol_map_cache = dict(_PUBLIC_SYMBOLS)
        return copy.deepcopy(_public_symbol_map_cache)


def get_public_symbol_status(
    *,
    refresh: bool = False,
    check_importable: bool = False,
) -> dict[str, Any]:
    """
    Return diagnostics for all lazily exported public symbols.
    """
    symbol_map = get_public_symbol_map(refresh=refresh)

    by_module: dict[str, dict[str, Any]] = {}
    symbols: list[dict[str, Any]] = []

    for symbol_name, mapping in symbol_map.items():
        module_path, attribute_name = mapping

        module_status = by_module.get(module_path)
        if module_status is None:
            spec_found, spec_error = _safe_find_spec(module_path)

            imported = None
            import_error = None
            module = None

            if check_importable:
                module, import_error = _safe_import_module(module_path)
                imported = module is not None

            module_status = {
                "modulePath": module_path,
                "specFound": spec_found,
                "specError": spec_error,
                "importChecked": check_importable,
                "imported": imported,
                "importError": import_error,
                "_module": module,
            }
            by_module[module_path] = module_status

        attribute_exists: bool | None = None

        if check_importable:
            module = module_status.get("_module")
            attribute_exists = bool(
                module is not None and hasattr(module, attribute_name)
            )

        symbols.append(
            {
                "symbolName": symbol_name,
                "modulePath": module_path,
                "attributeName": attribute_name,
                "moduleSpecFound": module_status["specFound"],
                "moduleSpecError": module_status["specError"],
                "moduleImportChecked": check_importable,
                "moduleImported": module_status["imported"],
                "moduleImportError": module_status["importError"],
                "attributeExists": attribute_exists,
            }
        )

    public_modules = []
    for module_path, module_status in by_module.items():
        public_modules.append(
            {
                "modulePath": module_path,
                "specFound": module_status["specFound"],
                "specError": module_status["specError"],
                "importChecked": module_status["importChecked"],
                "imported": module_status["imported"],
                "importError": module_status["importError"],
            }
        )

    return {
        "ok": True,
        "packageName": PACKAGE_NAME,
        "packageVersion": PACKAGE_VERSION,
        "packageMode": PACKAGE_MODE,
        "symbolCount": len(symbols),
        "moduleCount": len(public_modules),
        "checkImportable": check_importable,
        "modules": public_modules,
        "symbols": symbols,
    }


def import_world_state_module(
    module_name: str,
    *,
    required: bool = True,
) -> ModuleType | None:
    """
    Import a child module from this package.

    Args:
        module_name:
            Either a short name like "service" or a full module path like
            "src.world_state.service".
        required:
            If true, import failures raise RuntimeError.
            If false, failures return None.
    """
    normalized_name = str(module_name or "").strip()

    if not normalized_name:
        if required:
            raise RuntimeError("World-state module name is empty.")
        return None

    if "." not in normalized_name:
        module_path = MODULE_IMPORT_PATHS.get(
            normalized_name,
            f"{PACKAGE_NAME}.{normalized_name}",
        )
    else:
        module_path = normalized_name

    module, error = _safe_import_module(module_path)

    if module is not None:
        return module

    if required:
        raise RuntimeError(
            f"Could not import world-state module '{module_path}': {error}"
        )

    return None


def __getattr__(name: str) -> Any:
    """
    Lazily expose public symbols from child modules.

    This keeps `import src.world_state` safe while the package is assembled or
    while optional compatibility modules are missing.
    """
    symbol_map = get_public_symbol_map()

    if name not in symbol_map:
        raise AttributeError(
            f"module '{PACKAGE_NAME}' has no attribute '{name}'"
        )

    module_path, attribute_name = symbol_map[name]

    module, error = _safe_import_module(module_path)
    if module is None:
        raise AttributeError(
            f"Could not import '{name}' from '{module_path}': {error}"
        )

    try:
        value = getattr(module, attribute_name)
    except AttributeError as exc:
        raise AttributeError(
            f"Module '{module_path}' does not expose '{attribute_name}' "
            f"for public symbol '{name}'."
        ) from exc

    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(
        set(
            list(globals().keys())
            + list(_EXPLICIT_PUBLIC_NAMES)
            + list(_PUBLIC_SYMBOLS.keys())
        )
    )