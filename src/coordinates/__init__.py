# services/vectoplan-chunk/src/coordinates/__init__.py
"""Stabile öffentliche Fassade des VECTOPLAN-Koordinatenpakets.

Das Paket stellt die gemeinsamen, frameworkunabhängigen Koordinatenbausteine
für ``flat`` und ``earth`` bereit:

* stabile Domänenfehler;
* unveränderliche Koordinaten-Wertobjekte;
* topologieneutrale Chunkmathematik;
* providerabhängige Welt-Topologien.

Die Fassade verwendet thread-sichere Lazy-Imports. Dadurch können leichte
Status- und Bootstrap-Pfade das Paket importieren, ohne sofort alle
Unterkomponenten zu laden. Gleichzeitig bleiben öffentliche Imports stabil:

```
from src.coordinates import LocalBlockPosition, get_periodic_x_topology
```

Architekturregeln
-----------------
* Keine Flask-, SQLAlchemy-, CRS- oder Datenbankabhängigkeit.
* Lazy-Import-Fehler werden nicht verschluckt.
* Diagnostik darf Importfehler strukturiert melden, aber nicht als Erfolg
  ausgeben.
* Cache-Reset betrifft ausschließlich ableitbare In-Process-Caches.
* Lazy-Symbolcache und Berechnungscaches sind niemals Datenwahrheit.
* Öffentliche Namen werden über eine feste Symboltabelle versioniert.
"""

from collections.abc import Mapping
from importlib import import_module
from threading import RLock
from types import MappingProxyType, ModuleType
from typing import Any, Final


MODULE_VERSION: Final[str] = "1.0.0"
PACKAGE_ID: Final[str] = "vectoplan-coordinates"
PUBLIC_API_VERSION: Final[str] = "coordinates-api.v1"


# Öffentliche Symbole werden bewusst an einer Stelle verwaltet. Bestehende
# Namen dürfen nach Veröffentlichung nicht still auf andere Bedeutungen zeigen.
_SYMBOL_TO_MODULE: Final[Mapping[str, str]] = MappingProxyType(
    {
        # errors.py
        "AmbiguousAntipodalCoordinateError": ".errors",
        "CellAddressInvalidError": ".errors",
        "ChunkAddressInvalidError": ".errors",
        "ChunkAddressNonCanonicalError": ".errors",
        "CoordinateComputationError": ".errors",
        "CoordinateConfigurationError": ".errors",
        "CoordinateConflictError": ".errors",
        "CoordinateDimensionMismatchError": ".errors",
        "CoordinateError": ".errors",
        "CoordinateErrorCode": ".errors",
        "CoordinateOutOfBoundsError": ".errors",
        "CoordinateOverflowError": ".errors",
        "CoordinatePrecisionLossError": ".errors",
        "CoordinateSpaceMismatchError": ".errors",
        "CoordinateValidationError": ".errors",
        "HalfWorldNotChunkAlignedError": ".errors",
        "InvalidChunkSizeError": ".errors",
        "InvalidTopologyConfigurationError": ".errors",
        "NorthSouthBoundaryExceededError": ".errors",
        "TopologyNotResolvedError": ".errors",
        "UnsupportedWrapAxisError": ".errors",
        "WorldHeightInvalidError": ".errors",
        "WorldWidthInvalidError": ".errors",
        "WorldWidthNotChunkAlignedError": ".errors",
        "ensure_coordinate_error": ".errors",

        # models.py
        "AxisConvention": ".models",
        "ChunkAddress": ".models",
        "ChunkPosition": ".models",
        "CoordinateAxis": ".models",
        "CoordinateSpace": ".models",
        "IntegerTriple": ".models",
        "JsonPrimitive": ".models",
        "JsonValue": ".models",
        "LocalBlockPosition": ".models",
        "LocalCellPosition": ".models",
        "LocalMetricPosition": ".models",
        "NormalizationMetadata": ".models",
        "NormalizationReason": ".models",
        "NormalizedBlockPosition": ".models",
        "NormalizedChunkAddress": ".models",
        "NumberTriple": ".models",
        "ResolvedCellAddress": ".models",
        "SIGNED_INT32_MAX": ".models",
        "SIGNED_INT32_MIN": ".models",
        "SIGNED_INT64_MAX": ".models",
        "SIGNED_INT64_MIN": ".models",

        # chunk_math.py
        "ChunkMathConfig": ".chunk_math",
        "DEFAULT_CHUNK_SIZE": ".chunk_math",
        "apply_chunk_offset": ".chunk_math",
        "block_to_chunk_position": ".chunk_math",
        "block_to_local_cell_position": ".chunk_math",
        "boundary_offsets_for_cell": ".chunk_math",
        "cell_to_linear_index": ".chunk_math",
        "checked_cell_count": ".chunk_math",
        "chunk_block_bounds": ".chunk_math",
        "chunk_cell_to_block_position": ".chunk_math",
        "chunk_contains_block": ".chunk_math",
        "chunk_math_cache_info": ".chunk_math",
        "chunk_to_block_origin": ".chunk_math",
        "clear_chunk_math_caches": ".chunk_math",
        "floor_divide": ".chunk_math",
        "floor_modulo": ".chunk_math",
        "get_chunk_math": ".chunk_math",
        "iter_chunk_blocks": ".chunk_math",
        "iter_chunk_cells": ".chunk_math",
        "join_axis": ".chunk_math",
        "linear_index_to_cell": ".chunk_math",
        "resolve_block_address": ".chunk_math",
        "same_chunk": ".chunk_math",
        "split_axis": ".chunk_math",
        "validate_chunk_size": ".chunk_math",

        # topology.py
        "CanonicalChunkBatch": ".topology",
        "NorthSouthPolicy": ".topology",
        "PeriodicXTopology": ".topology",
        "TopologyKind": ".topology",
        "UnboundedFlatTopology": ".topology",
        "WorldTopology": ".topology",
        "clear_topology_caches": ".topology",
        "get_periodic_x_topology": ".topology",
        "get_unbounded_flat_topology": ".topology",
        "topology_cache_info": ".topology",
    }
)


_PUBLIC_SYMBOLS: Final[tuple[str, ...]] = tuple(sorted(_SYMBOL_TO_MODULE))
_DIAGNOSTIC_MODULES: Final[tuple[str, ...]] = (
    ".errors",
    ".models",
    ".chunk_math",
    ".topology",
)

_SYMBOL_CACHE: dict[str, Any] = {}
_MODULE_CACHE: dict[str, ModuleType] = {}
_STATE_LOCK = RLock()


def __getattr__(name: str) -> Any:
    """Lädt öffentliche Symbole bei der ersten Verwendung thread-sicher.

    Import- oder Attributfehler werden bewusst nicht in ``None`` oder einen
    Platzhalter übersetzt. Eine unvollständige Installation muss sichtbar
    fehlschlagen.
    """

    module_path = _SYMBOL_TO_MODULE.get(name)
    if module_path is None:
        raise AttributeError(
            f"Modul {__name__!r} besitzt kein öffentliches Attribut {name!r}."
        )

    with _STATE_LOCK:
        if name in _SYMBOL_CACHE:
            return _SYMBOL_CACHE[name]

        module = _load_relative_module(module_path)

        try:
            value = getattr(module, name)
        except AttributeError as error:
            raise ImportError(
                f"Öffentliches Symbol {name!r} fehlt in {module.__name__!r}."
            ) from error

        _SYMBOL_CACHE[name] = value
        globals()[name] = value
        return value


def __dir__() -> list[str]:
    """Erweitert ``dir(src.coordinates)`` um alle öffentlichen Lazy-Symbole."""

    return sorted(
        set(globals())
        | set(_PUBLIC_SYMBOLS)
        | {
            "MODULE_VERSION",
            "PACKAGE_ID",
            "PUBLIC_API_VERSION",
            "clear_coordinate_caches",
            "coordinate_cache_info",
            "coordinate_module_status",
            "preload_coordinate_modules",
            "reset_coordinate_package_state",
        }
    )


def preload_coordinate_modules(
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Lädt alle Koordinaten-Unterkomponenten kontrolliert vor.

    Parameters
    ----------
    strict:
        Bei ``True`` wird der erste Importfehler erneut ausgelöst. Bei
        ``False`` werden alle Module geprüft und Fehler ausschließlich im
        Ergebnis dokumentiert.

    Returns
    -------
    dict
        Serialisierbarer Modulstatus.
    """

    statuses: list[dict[str, Any]] = []
    first_error: Exception | None = None

    for module_path in _DIAGNOSTIC_MODULES:
        try:
            module = _load_relative_module(module_path)
            statuses.append(
                {
                    "module": module.__name__,
                    "relativeModule": module_path,
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
        "moduleCount": len(statuses),
        "readyModuleCount": sum(1 for item in statuses if item["ready"]),
        "modules": statuses,
    }

    if strict and first_error is not None:
        raise first_error

    return result


def coordinate_module_status(
    *,
    preload: bool = False,
    include_cache_info: bool = True,
) -> dict[str, Any]:
    """Liefert eine robuste, serialisierbare Paketdiagnostik.

    ``preload=False`` meldet, welche Unterkomponenten bereits geladen wurden.
    ``preload=True`` prüft alle bekannten Unterkomponenten aktiv, ohne Fehler
    zu verschlucken; Importfehler werden strukturiert im Ergebnis dargestellt.
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
                "module": module.__name__ if module is not None else qualified_name,
                "relativeModule": module_path,
                "loaded": module is not None,
                "ready": module is not None,
                "error": None,
            }
        )

    with _STATE_LOCK:
        loaded_symbol_count = len(_SYMBOL_CACHE)
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
        "lazyImportsEnabled": True,
        "publicSymbolCount": len(_PUBLIC_SYMBOLS),
        "loadedSymbolCount": loaded_symbol_count,
        "loadedSymbols": list(loaded_symbols),
        "knownModuleCount": len(_DIAGNOSTIC_MODULES),
        "loadedModuleCount": loaded_module_count,
        "modules": module_statuses,
    }

    if include_cache_info:
        try:
            payload["caches"] = coordinate_cache_info()
        except Exception as error:
            payload["caches"] = {
                "ok": False,
                "error": _safe_error_summary(error),
            }
            if preload:
                payload["ok"] = False

    return payload


def coordinate_cache_info() -> dict[str, Any]:
    """Liefert Cacheinformationen aller bereits verfügbaren Komponenten.

    Die Funktion lädt ``chunk_math`` und ``topology`` bei Bedarf. Dies ist
    beabsichtigt, weil Cache-Diagnostik ohne diese Module nicht aussagekräftig
    wäre.
    """

    chunk_math = _load_relative_module(".chunk_math")
    topology = _load_relative_module(".topology")

    chunk_math_info = chunk_math.chunk_math_cache_info()
    topology_info = topology.topology_cache_info()

    with _STATE_LOCK:
        lazy_symbol_count = len(_SYMBOL_CACHE)
        lazy_module_count = len(_MODULE_CACHE)

    return {
        "ok": True,
        "lazySymbolCache": {
            "currentSize": lazy_symbol_count,
            "maximumSize": len(_PUBLIC_SYMBOLS),
        },
        "lazyModuleCache": {
            "currentSize": lazy_module_count,
            "maximumSize": len(_DIAGNOSTIC_MODULES),
        },
        "chunkMath": chunk_math_info,
        "topology": topology_info,
    }


def clear_coordinate_caches() -> dict[str, Any]:
    """Leert alle ableitbaren Rechen- und Strategiecaches.

    Lazy-Module und geladene öffentliche Symbolobjekte bleiben erhalten. Damit
    werden bestehende Klassenidentitäten nicht während einer laufenden Runtime
    ausgetauscht.
    """

    chunk_math = _load_relative_module(".chunk_math")
    topology = _load_relative_module(".topology")

    chunk_math.clear_chunk_math_caches()
    topology.clear_topology_caches()

    return {
        "ok": True,
        "cleared": [
            "chunk_math",
            "topology",
        ],
        "remaining": coordinate_cache_info(),
    }


def reset_coordinate_package_state(
    *,
    clear_runtime_caches: bool = True,
    clear_lazy_symbols: bool = False,
) -> dict[str, Any]:
    """Setzt kontrolliert ableitbaren Paketstatus zurück.

    Diese Funktion ist primär für Tests, explizite Diagnose- und
    Cache-Reset-Routen gedacht.

    ``clear_lazy_symbols`` entfernt geladene Lazy-Symbole aus dem
    Modul-Namespace. Standardmäßig bleibt dies deaktiviert, um Klassenidentität
    und laufende Referenzen in einer Runtime nicht unnötig zu beeinflussen.

    Importierte Python-Module werden nicht aus ``sys.modules`` entfernt.
    """

    cleared: list[str] = []

    if clear_runtime_caches:
        clear_coordinate_caches()
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
        "status": coordinate_module_status(
            preload=False,
            include_cache_info=True,
        ),
    }


def _load_relative_module(module_path: str) -> ModuleType:
    """Lädt ein bekanntes relatives Untermodul thread-sicher."""

    if module_path not in _DIAGNOSTIC_MODULES:
        raise ImportError(
            f"Nicht freigegebenes Koordinaten-Untermodul: {module_path!r}."
        )

    with _STATE_LOCK:
        cached = _MODULE_CACHE.get(module_path)
        if cached is not None:
            return cached

        module = import_module(module_path, package=__name__)
        _MODULE_CACHE[module_path] = module
        return module


def _safe_error_summary(error: Exception) -> dict[str, Any]:
    """Erzeugt eine nicht-sensitive Diagnose ohne Stacktrace oder ``repr``."""

    summary: dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error).strip() or "Import oder Diagnose fehlgeschlagen.",
    }

    code = getattr(error, "code", None)
    if code is not None:
        summary["code"] = str(code)

    details = getattr(error, "details", None)
    if isinstance(details, Mapping):
        summary["details"] = _safe_mapping(details)

    return summary


def _safe_mapping(value: Mapping[Any, Any], *, depth: int = 0) -> dict[str, Any]:
    if depth >= 6:
        return {"truncated": True}

    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key:
            continue
        result[key] = _safe_value(raw_value, depth=depth + 1)

    return result


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return {"truncated": True}

    if value is None or isinstance(value, (str, int, float, bool)):
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
    "MODULE_VERSION",
    "PACKAGE_ID",
    "PUBLIC_API_VERSION",
    "clear_coordinate_caches",
    "coordinate_cache_info",
    "coordinate_module_status",
    "preload_coordinate_modules",
    "reset_coordinate_package_state",
]
