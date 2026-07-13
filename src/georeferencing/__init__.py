# services/vectoplan-chunk/src/georeferencing/__init__.py
"""Stabile öffentliche Fassade der VECTOPLAN-Georeferenzierung.

Das Paket bündelt die frameworkunabhängigen Earth-v1-Bausteine:

* stabile CRS- und Transformationsfehler;
* unveränderliche Georeferenzierungsverträge;
* kontrollierte pyproj-/PROJ-Integration;
* strikte, thread-lokale Transformer;
* das versionierte, periodische Earth-Grid.

Die Fassade verwendet thread-sichere Lazy-Imports. Ein einfacher Import:

```
import src.georeferencing
```

lädt weder ``pyproj`` noch CRS-Datenbank, Transformer oder Earth-Grid-
Readiness automatisch. Initialisierung, Netzwerkpolicy und Readiness werden
bewusst über explizite Funktionen ausgeführt.

Architekturregeln
-----------------
* Keine Flask-, SQLAlchemy-, Repository- oder HTTP-Abhängigkeit.
* Kein PROJ-Netzwerkzugriff und keine Runtime-Mutation beim Paketimport.
* Lazy-Importfehler werden nicht verschluckt.
* Readiness meldet degradierte Teilzustände strukturiert.
* Cache-Reset betrifft nur reproduzierbare In-Process-Caches.
* Native Transformer bleiben thread-lokal und werden nie exportiert.
* Öffentliche Namen werden zentral und stabil versioniert.
* Unbekannte Symbole schlagen sichtbar mit ``AttributeError`` fehl.
"""

from collections.abc import Mapping
from importlib import import_module
from threading import RLock
from types import MappingProxyType, ModuleType
from typing import Any, Final


MODULE_VERSION: Final[str] = "1.0.0"
PACKAGE_ID: Final[str] = "vectoplan-georeferencing"
PUBLIC_API_VERSION: Final[str] = "georeferencing-api.v1"
EARTH_WORLD_CONTRACT_VERSION: Final[str] = "earth-world.v1"


# Öffentliche Symbole sind bewusst statisch zugeordnet. Vorhandene Namen
# dürfen nach Veröffentlichung nicht still auf andere Bedeutungen zeigen.
_SYMBOL_TO_MODULE: Final[Mapping[str, str]] = MappingProxyType(
    {
        # errors.py
        "BallparkTransformationForbiddenError": ".errors",
        "CoordinateFrameRevisionConflictError": ".errors",
        "CrsAxisOrderInvalidError": ".errors",
        "CrsDimensionMismatchError": ".errors",
        "CrsInvalidError": ".errors",
        "CrsNotTransformableError": ".errors",
        "CrsRequiredError": ".errors",
        "CrsUnitInvalidError": ".errors",
        "CrsUnsupportedError": ".errors",
        "EarthReferenceConflictError": ".errors",
        "EarthReferenceInvalidError": ".errors",
        "EarthWorldReferenceRequiredError": ".errors",
        "GeoreferencingComputationError": ".errors",
        "GeoreferencingConfigurationError": ".errors",
        "GeoreferencingConflictError": ".errors",
        "GeoreferencingDependencyUnavailableError": ".errors",
        "GeoreferencingError": ".errors",
        "GeoreferencingErrorCode": ".errors",
        "GeoreferencingValidationError": ".errors",
        "ProjDatabaseUnavailableError": ".errors",
        "PyprojUnavailableError": ".errors",
        "TransformationAccuracyUnknownError": ".errors",
        "TransformationFailedError": ".errors",
        "TransformationGridMissingError": ".errors",
        "TransformationNotExactError": ".errors",
        "TransformationPrecisionExceededError": ".errors",
        "TransformationRoundtripFailedError": ".errors",
        "TransformationUnavailableError": ".errors",
        "WorldReferenceLockedError": ".errors",
        "ensure_georeferencing_error": ".errors",
        "summarize_crs_input": ".errors",

        # contracts.py
        "CoordinateDimension": ".contracts",
        "CoordinateTransformRequest": ".contracts",
        "CoordinateTransformResult": ".contracts",
        "CrsDefinition": ".contracts",
        "CrsDefinitionFormat": ".contracts",
        "EarthGridPosition": ".contracts",
        "EarthGridReference": ".contracts",
        "GlobalCoordinate": ".contracts",
        "GlobalReferencePoint": ".contracts",
        "ResolvedEarthAnchor": ".contracts",
        "TransformationAccuracy": ".contracts",
        "TransformationOperationKind": ".contracts",
        "TransformationPolicy": ".contracts",
        "decimal_to_canonical_string": ".contracts",

        # crs.py
        "CANONICAL_GEOCENTRIC_CRS_ID": ".crs",
        "CANONICAL_GEOGRAPHIC_CRS_ID": ".crs",
        "CANONICAL_WKT_VERSION": ".crs",
        "CrsInspection": ".crs",
        "CrsResolutionPolicy": ".crs",
        "MINIMUM_PYPROJ_VERSION": ".crs",
        "canonical_geocentric_crs": ".crs",
        "canonical_geographic_crs": ".crs",
        "clear_crs_caches": ".crs",
        "configure_proj_network": ".crs",
        "crs_cache_info": ".crs",
        "crs_equivalent": ".crs",
        "crs_runtime_status": ".crs",
        "ensure_crs_runtime_ready": ".crs",
        "inspect_crs": ".crs",
        "resolve_crs": ".crs",
        "resolve_native_crs": ".crs",

        # transformer.py
        "AreaOfInterestBounds": ".transformer",
        "GridRequirement": ".transformer",
        "TransformerDescriptor": ".transformer",
        "TransformerSelection": ".transformer",
        "TransformerSelectionOptions": ".transformer",
        "clear_transformer_caches": ".transformer",
        "select_transformer": ".transformer",
        "transform_coordinate": ".transformer",
        "transform_coordinate_batch": ".transformer",
        "transformer_cache_info": ".transformer",
        "transformer_runtime_status": ".transformer",

        # earth_grid.py
        "DEFAULT_EARTH_CHUNK_SIZE": ".earth_grid",
        "DEFAULT_EARTH_GRID_ID": ".earth_grid",
        "DEFAULT_EARTH_GRID_VERSION": ".earth_grid",
        "DEFAULT_EARTH_METERS_PER_CELL": ".earth_grid",
        "DEFAULT_EARTH_WORLD_HEIGHT_CELLS": ".earth_grid",
        "DEFAULT_EARTH_WORLD_WIDTH_CELLS": ".earth_grid",
        "EARTH_GRID_MAPPING_ID": ".earth_grid",
        "EARTH_GRID_MAPPING_VERSION": ".earth_grid",
        "EARTH_GRID_RESOLVER_VERSION": ".earth_grid",
        "EARTH_GRID_STORAGE_ORIGIN_POLICY": ".earth_grid",
        "EARTH_GRID_TOPOLOGY_TYPE": ".earth_grid",
        "EarthGridDefinition": ".earth_grid",
        "EarthGridFrame": ".earth_grid",
        "EarthGridMappingResult": ".earth_grid",
        "EarthStorageOrigin": ".earth_grid",
        "GlobalToLocalResult": ".earth_grid",
        "LocalEarthPosition": ".earth_grid",
        "LocalToGlobalResult": ".earth_grid",
        "clear_earth_grid_caches": ".earth_grid",
        "earth_grid_cache_info": ".earth_grid",
        "earth_grid_runtime_status": ".earth_grid",
        "get_default_earth_grid_definition": ".earth_grid",
        "get_earth_grid_definition": ".earth_grid",
        "global_to_local": ".earth_grid",
        "local_to_global": ".earth_grid",
        "map_global_coordinate_to_grid": ".earth_grid",
        "reference_as_local_position": ".earth_grid",
        "resolve_earth_grid_frame": ".earth_grid",
    }
)


_PUBLIC_SYMBOLS: Final[tuple[str, ...]] = tuple(sorted(_SYMBOL_TO_MODULE))
_DIAGNOSTIC_MODULES: Final[tuple[str, ...]] = (
    ".errors",
    ".contracts",
    ".crs",
    ".transformer",
    ".earth_grid",
)
_HEAVY_RUNTIME_MODULES: Final[tuple[str, ...]] = (
    ".crs",
    ".transformer",
    ".earth_grid",
)

_SYMBOL_CACHE: dict[str, Any] = {}
_MODULE_CACHE: dict[str, ModuleType] = {}
_STATE_LOCK = RLock()


def __getattr__(name: str) -> Any:
    """Lädt ein öffentliches Symbol bei der ersten Verwendung thread-sicher."""

    module_path = _SYMBOL_TO_MODULE.get(name)
    if module_path is None:
        raise AttributeError(
            f"Modul {__name__!r} besitzt kein öffentliches Attribut {name!r}."
        )

    with _STATE_LOCK:
        cached = _SYMBOL_CACHE.get(name)
        if cached is not None:
            return cached

        module = _load_relative_module(module_path)

        try:
            value = getattr(module, name)
        except AttributeError as error:
            raise ImportError(
                f"Öffentliches Symbol {name!r} fehlt "
                f"in {module.__name__!r}."
            ) from error

        _SYMBOL_CACHE[name] = value
        globals()[name] = value
        return value


def __dir__() -> list[str]:
    """Erweitert ``dir()`` um alle öffentlichen Lazy-Symbole."""

    return sorted(
        set(globals())
        | set(_PUBLIC_SYMBOLS)
        | {
            "EARTH_WORLD_CONTRACT_VERSION",
            "MODULE_VERSION",
            "PACKAGE_ID",
            "PUBLIC_API_VERSION",
            "clear_georeferencing_caches",
            "georeferencing_cache_info",
            "georeferencing_module_status",
            "georeferencing_runtime_status",
            "initialize_georeferencing_runtime",
            "preload_georeferencing_modules",
            "reset_georeferencing_package_state",
        }
    )


def preload_georeferencing_modules(
    *,
    include_heavy_runtime: bool = True,
    strict: bool = True,
) -> dict[str, Any]:
    """Lädt bekannte Unterkomponenten kontrolliert vor.

    Parameters
    ----------
    include_heavy_runtime:
        Bei ``False`` werden nur ``errors`` und ``contracts`` geladen.
        Bei ``True`` zusätzlich CRS, Transformer und Earth-Grid.
    strict:
        Bei ``True`` wird der erste Importfehler nach vollständiger
        Statusaufnahme erneut ausgelöst.
    """

    selected_modules = (
        _DIAGNOSTIC_MODULES
        if include_heavy_runtime
        else (".errors", ".contracts")
    )

    statuses: list[dict[str, Any]] = []
    first_error: Exception | None = None

    for module_path in selected_modules:
        try:
            module = _load_relative_module(module_path)
            statuses.append(
                {
                    "module": module.__name__,
                    "relativeModule": module_path,
                    "heavyRuntime": (
                        module_path in _HEAVY_RUNTIME_MODULES
                    ),
                    "imported": True,
                    "ready": True,
                    "error": None,
                }
            )
        except Exception as error:
            if first_error is None:
                first_error = error

            statuses.append(
                {
                    "module": f"{__name__}{module_path}",
                    "relativeModule": module_path,
                    "heavyRuntime": (
                        module_path in _HEAVY_RUNTIME_MODULES
                    ),
                    "imported": False,
                    "ready": False,
                    "error": _safe_error_summary(error),
                }
            )

    result = {
        "ok": all(item["ready"] for item in statuses),
        "packageId": PACKAGE_ID,
        "moduleVersion": MODULE_VERSION,
        "publicApiVersion": PUBLIC_API_VERSION,
        "earthWorldContractVersion": (
            EARTH_WORLD_CONTRACT_VERSION
        ),
        "includeHeavyRuntime": bool(include_heavy_runtime),
        "moduleCount": len(statuses),
        "readyModuleCount": sum(
            1 for item in statuses if item["ready"]
        ),
        "modules": statuses,
    }

    if strict and first_error is not None:
        raise first_error

    return result


def georeferencing_module_status(
    *,
    preload: bool = False,
    include_cache_info: bool = False,
) -> dict[str, Any]:
    """Liefert eine serialisierbare Diagnose des Paket-Ladezustands.

    Ohne ``preload`` wird keine zusätzliche Unterkomponente geladen.
    """

    module_statuses: list[dict[str, Any]] = []

    for module_path in _DIAGNOSTIC_MODULES:
        qualified_name = f"{__name__}{module_path}"

        if preload:
            try:
                module = _load_relative_module(module_path)
                module_statuses.append(
                    {
                        "module": module.__name__,
                        "relativeModule": module_path,
                        "heavyRuntime": (
                            module_path in _HEAVY_RUNTIME_MODULES
                        ),
                        "loaded": True,
                        "ready": True,
                        "error": None,
                    }
                )
            except Exception as error:
                module_statuses.append(
                    {
                        "module": qualified_name,
                        "relativeModule": module_path,
                        "heavyRuntime": (
                            module_path in _HEAVY_RUNTIME_MODULES
                        ),
                        "loaded": False,
                        "ready": False,
                        "error": _safe_error_summary(error),
                    }
                )
            continue

        with _STATE_LOCK:
            module = _MODULE_CACHE.get(module_path)

        module_statuses.append(
            {
                "module": (
                    module.__name__
                    if module is not None
                    else qualified_name
                ),
                "relativeModule": module_path,
                "heavyRuntime": (
                    module_path in _HEAVY_RUNTIME_MODULES
                ),
                "loaded": module is not None,
                "ready": module is not None,
                "error": None,
            }
        )

    with _STATE_LOCK:
        loaded_symbols = tuple(sorted(_SYMBOL_CACHE))
        loaded_module_count = len(_MODULE_CACHE)

    payload: dict[str, Any] = {
        "ok": (
            all(item["ready"] for item in module_statuses)
            if preload
            else True
        ),
        "packageId": PACKAGE_ID,
        "moduleVersion": MODULE_VERSION,
        "publicApiVersion": PUBLIC_API_VERSION,
        "earthWorldContractVersion": (
            EARTH_WORLD_CONTRACT_VERSION
        ),
        "lazyImportsEnabled": True,
        "publicSymbolCount": len(_PUBLIC_SYMBOLS),
        "loadedSymbolCount": len(loaded_symbols),
        "loadedSymbols": list(loaded_symbols),
        "knownModuleCount": len(_DIAGNOSTIC_MODULES),
        "loadedModuleCount": loaded_module_count,
        "modules": module_statuses,
    }

    if include_cache_info:
        try:
            payload["caches"] = georeferencing_cache_info()
        except Exception as error:
            payload["caches"] = {
                "ok": False,
                "error": _safe_error_summary(error),
            }
            if preload:
                payload["ok"] = False

    return payload


def initialize_georeferencing_runtime(
    *,
    network_enabled: bool = False,
    strict: bool = True,
) -> dict[str, Any]:
    """Initialisiert die lokale CRS-Runtime explizit und reproduzierbar.

    Die Funktion:

    1. lädt alle Georeferenzierungsmodule;
    2. setzt den PROJ-Netzwerkstatus explizit;
    3. führt die vollständige Readiness-Prüfung aus.

    Bei ``strict=True`` wird ein nicht-bereiter Zustand als
    ``GeoreferencingConfigurationError`` ausgelöst.
    """

    preload = preload_georeferencing_modules(
        include_heavy_runtime=True,
        strict=strict,
    )

    crs_module = _load_relative_module(".crs")
    network = crs_module.configure_proj_network(
        enabled=bool(network_enabled)
    )

    readiness = georeferencing_runtime_status(
        require_network_disabled=not bool(network_enabled)
    )

    result = {
        "ok": bool(
            preload["ok"]
            and network["ok"]
            and readiness["ok"]
        ),
        "packageId": PACKAGE_ID,
        "moduleVersion": MODULE_VERSION,
        "network": network,
        "preload": preload,
        "readiness": readiness,
    }

    if strict and not result["ok"]:
        error_type = __getattr__(
            "GeoreferencingConfigurationError"
        )
        raise error_type(
            "Die Georeferenzierungs-Runtime ist nicht bereit.",
            details={
                "network": network,
                "readiness": readiness,
            },
        )

    return result


def georeferencing_runtime_status(
    *,
    require_network_disabled: bool = True,
) -> dict[str, Any]:
    """Aggregiert CRS-, Transformer- und Earth-Grid-Readiness.

    Jede Unterprüfung wird isoliert ausgeführt. Ein Fehler in einer
    Komponente verhindert nicht, dass die anderen Teilzustände sichtbar
    werden.
    """

    checks: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []

    crs_module = None
    transformer_module = None
    earth_grid_module = None

    try:
        crs_module = _load_relative_module(".crs")
        checks["crs"] = crs_module.crs_runtime_status(
            require_network_disabled=require_network_disabled,
            validate_required_crs=True,
        )
    except Exception as error:
        checks["crs"] = {
            "ok": False,
            "error": _safe_error_summary(error),
        }
        errors.append(
            {
                "component": "crs",
                **_safe_error_summary(error),
            }
        )

    try:
        transformer_module = _load_relative_module(
            ".transformer"
        )
        checks["transformer"] = (
            transformer_module.transformer_runtime_status()
        )
    except Exception as error:
        checks["transformer"] = {
            "ok": False,
            "error": _safe_error_summary(error),
        }
        errors.append(
            {
                "component": "transformer",
                **_safe_error_summary(error),
            }
        )

    try:
        earth_grid_module = _load_relative_module(
            ".earth_grid"
        )
        checks["earthGrid"] = (
            earth_grid_module.earth_grid_runtime_status()
        )
    except Exception as error:
        checks["earthGrid"] = {
            "ok": False,
            "error": _safe_error_summary(error),
        }
        errors.append(
            {
                "component": "earthGrid",
                **_safe_error_summary(error),
            }
        )

    component_ok = all(
        bool(checks.get(name, {}).get("ok"))
        for name in ("crs", "transformer", "earthGrid")
    )

    return {
        "ok": bool(component_ok and not errors),
        "packageId": PACKAGE_ID,
        "moduleVersion": MODULE_VERSION,
        "publicApiVersion": PUBLIC_API_VERSION,
        "earthWorldContractVersion": (
            EARTH_WORLD_CONTRACT_VERSION
        ),
        "requireNetworkDisabled": bool(
            require_network_disabled
        ),
        "checks": checks,
        "caches": georeferencing_cache_info(),
        "errors": errors,
    }


def georeferencing_cache_info() -> dict[str, Any]:
    """Liefert Cacheinformationen aller Georeferenzierungskomponenten."""

    crs_module = _load_relative_module(".crs")
    transformer_module = _load_relative_module(".transformer")
    earth_grid_module = _load_relative_module(".earth_grid")

    with _STATE_LOCK:
        loaded_symbols = len(_SYMBOL_CACHE)
        loaded_modules = len(_MODULE_CACHE)

    return {
        "ok": True,
        "lazySymbolCache": {
            "currentSize": loaded_symbols,
            "maximumSize": len(_PUBLIC_SYMBOLS),
        },
        "lazyModuleCache": {
            "currentSize": loaded_modules,
            "maximumSize": len(_DIAGNOSTIC_MODULES),
        },
        "crs": crs_module.crs_cache_info(),
        "transformer": (
            transformer_module.transformer_cache_info()
        ),
        "earthGrid": earth_grid_module.earth_grid_cache_info(),
    }


def clear_georeferencing_caches() -> dict[str, Any]:
    """Leert alle reproduzierbaren Georeferenzierungs-Caches.

    Reihenfolge:
    1. Earth-Grid-Frames;
    2. thread-lokale Transformer;
    3. CRS-Objekte.

    Dadurch bleiben keine höherliegenden Cacheeinträge mit bereits
    invalidierten Abhängigkeiten bestehen.
    """

    earth_grid_module = _load_relative_module(".earth_grid")
    transformer_module = _load_relative_module(".transformer")
    crs_module = _load_relative_module(".crs")

    earth_grid_module.clear_earth_grid_caches()
    transformer_result = (
        transformer_module.clear_transformer_caches()
    )
    crs_module.clear_crs_caches()

    return {
        "ok": True,
        "cleared": [
            "earth_grid",
            "transformer",
            "crs",
        ],
        "transformerInvalidation": transformer_result,
        "remaining": georeferencing_cache_info(),
    }


def reset_georeferencing_package_state(
    *,
    clear_runtime_caches: bool = True,
    clear_lazy_symbols: bool = False,
) -> dict[str, Any]:
    """Setzt kontrolliert ableitbaren Paketstatus zurück.

    Importierte Python-Module werden bewusst nicht aus ``sys.modules``
    entfernt. Laufende Klassenidentitäten und native Bibliothekszustände
    bleiben stabil.
    """

    cleared: list[str] = []

    if clear_runtime_caches:
        clear_georeferencing_caches()
        cleared.append("runtime_caches")

    if clear_lazy_symbols:
        with _STATE_LOCK:
            symbol_names = tuple(_SYMBOL_CACHE)
            _SYMBOL_CACHE.clear()

            for symbol_name in symbol_names:
                globals().pop(symbol_name, None)

        cleared.append("lazy_symbols")

    return {
        "ok": True,
        "cleared": cleared,
        "status": georeferencing_module_status(
            preload=False,
            include_cache_info=True,
        ),
    }


def _load_relative_module(module_path: str) -> ModuleType:
    """Lädt ein freigegebenes Untermodul thread-sicher."""

    if module_path not in _DIAGNOSTIC_MODULES:
        raise ImportError(
            "Nicht freigegebenes Georeferenzierungs-Untermodul: "
            f"{module_path!r}."
        )

    with _STATE_LOCK:
        cached = _MODULE_CACHE.get(module_path)
        if cached is not None:
            return cached

        module = import_module(module_path, package=__name__)
        _MODULE_CACHE[module_path] = module
        return module


def _safe_error_summary(
    error: BaseException,
) -> dict[str, Any]:
    """Erzeugt eine nicht-sensitive Diagnose ohne Stacktrace oder repr()."""

    payload: dict[str, Any] = {
        "type": type(error).__name__,
        "message": (
            str(error).strip()
            or "Georeferenzierungsoperation fehlgeschlagen."
        ),
    }

    code = getattr(error, "code", None)
    if code is not None:
        payload["code"] = str(code)

    details = getattr(error, "details", None)
    if isinstance(details, Mapping):
        payload["details"] = _safe_mapping(details)

    return payload


def _safe_mapping(
    value: Mapping[Any, Any],
    *,
    depth: int = 0,
) -> dict[str, Any]:
    if depth >= 6:
        return {"truncated": True}

    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key:
            continue
        result[key] = _safe_value(
            raw_value,
            depth=depth + 1,
        )
    return result


def _safe_value(
    value: Any,
    *,
    depth: int = 0,
) -> Any:
    if depth >= 6:
        return {"truncated": True}

    if value is None or isinstance(
        value,
        (str, int, float, bool),
    ):
        return value

    if isinstance(value, Mapping):
        return _safe_mapping(value, depth=depth + 1)

    if isinstance(value, (list, tuple)):
        return [
            _safe_value(item, depth=depth + 1)
            for item in value[:100]
        ]

    return {"type": type(value).__name__}


__all__ = [
    *_PUBLIC_SYMBOLS,
    "EARTH_WORLD_CONTRACT_VERSION",
    "MODULE_VERSION",
    "PACKAGE_ID",
    "PUBLIC_API_VERSION",
    "clear_georeferencing_caches",
    "georeferencing_cache_info",
    "georeferencing_module_status",
    "georeferencing_runtime_status",
    "initialize_georeferencing_runtime",
    "preload_georeferencing_modules",
    "reset_georeferencing_package_state",
]
