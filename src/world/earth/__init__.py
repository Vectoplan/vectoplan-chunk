# services/vectoplan-chunk/src/world/earth/__init__.py
"""Stabile öffentliche Fassade des VECTOPLAN-Earth-Providers.

Der Provider ``earth`` ergänzt den bestehenden Provider ``flat``. Beide
verwenden dieselbe Chunk-, Snapshot-, Event- und Command-Grundarchitektur.
``earth`` erweitert sie um:

* genau einen globalen Referenzpunkt pro konkreter WorldInstance;
* ein versioniertes, global einheitliches Earth-Raster;
* eine periodische X-Topologie;
* lokale Persistenz relativ zu einem abgeleiteten Speicherframe;
* globale/lokale Koordinatenumrechnung;
* einen lokal gespeicherten, global adressierbaren Spawnpunkt.

Diese Paketfassade lädt Validator, Generator und Provider thread-sicher und
erst bei der ersten Verwendung. Ein normaler Import:

```
import src.world.earth
```

führt ausdrücklich nicht aus:

* keine CRS- oder PROJ-Initialisierung;
* keine Generatorerzeugung;
* keine Datenbankoperation;
* kein Seed;
* keine Schemaänderung;
* keinen Zugriff auf bestehende Flat-Welten.

Architekturregeln
-----------------
* ``provider_id``, ``template_id`` und ``provider_world_id`` sind ``earth``.
* ``world_spawn`` bleibt die konkrete WorldInstance und ist keine Provider-ID.
* ``world.json`` ist die versionierte statische Providerdefinition.
* Fehlende oder ungültige Providerdateien werden sichtbar gemeldet.
* Lazy-Importfehler werden nicht verschluckt.
* Cache-Inhalte sind jederzeit reproduzierbar und niemals Datenwahrheit.
* Cache-Reset wird von oben nach unten ausgeführt:
  Provider → Generator → Validator/Definition → Manifest.
* Öffentliche Namen werden zentral und stabil versioniert.
"""

from collections.abc import Mapping
from functools import lru_cache
from hashlib import sha256
from importlib import import_module
import json
from pathlib import Path
from threading import RLock
from types import MappingProxyType, ModuleType
from typing import Any, Final


MODULE_VERSION: Final[str] = "1.0.0"
PACKAGE_ID: Final[str] = "vectoplan-earth-provider"
PUBLIC_API_VERSION: Final[str] = "earth-provider-api.v1"
PROVIDER_CONTRACT_VERSION: Final[str] = "earth-provider.v1"

PROVIDER_ID: Final[str] = "earth"
TEMPLATE_ID: Final[str] = "earth"
PROVIDER_WORLD_ID: Final[str] = "earth"
WORLD_TYPE: Final[str] = "earth"

MANIFEST_FILENAME: Final[str] = "world.json"
EXPECTED_MANIFEST_SCHEMA_VERSION: Final[str] = (
    "earth-world-definition.v1"
)
_MAX_MANIFEST_SIZE_BYTES: Final[int] = 1_048_576
_MAX_STATUS_ERROR_LENGTH: Final[int] = 1_024


# Die zukünftigen Dateien validator.py, generator.py und provider.py müssen
# diese stabilen Exporte implementieren. Vorhandene Namen dürfen später nicht
# still auf eine andere Bedeutung zeigen.
_SYMBOL_TO_MODULE: Final[Mapping[str, str]] = MappingProxyType(
    {
        # validator.py
        "EarthWorldDefinition": ".validator",
        "EarthWorldValidationResult": ".validator",
        "clear_earth_world_definition_cache": ".validator",
        "earth_world_definition_status": ".validator",
        "load_earth_world_definition": ".validator",
        "validate_earth_world_definition": ".validator",

        # generator.py
        "EarthFlatPeriodicGenerator": ".generator",
        "EarthGeneratedChunk": ".generator",
        "EarthGeneratorConfig": ".generator",
        "clear_earth_generator_caches": ".generator",
        "earth_generator_runtime_status": ".generator",

        # provider.py
        "EarthProviderCapabilities": ".provider",
        "EarthWorldProvider": ".provider",
        "clear_earth_provider_component_caches": ".provider",
        "earth_provider_component_status": ".provider",
        "get_earth_world_provider": ".provider",
    }
)

_PUBLIC_SYMBOLS: Final[tuple[str, ...]] = tuple(sorted(_SYMBOL_TO_MODULE))
_DIAGNOSTIC_MODULES: Final[tuple[str, ...]] = (
    ".validator",
    ".generator",
    ".provider",
)

_SYMBOL_CACHE: dict[str, Any] = {}
_MODULE_CACHE: dict[str, ModuleType] = {}
_STATE_LOCK = RLock()


def __getattr__(name: str) -> Any:
    """Lädt ein öffentliches Provider-Symbol thread-sicher bei Bedarf."""

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
                f"Öffentliches Symbol {name!r} fehlt "
                f"in {module.__name__!r}."
            ) from error

        _SYMBOL_CACHE[name] = value
        globals()[name] = value
        return value


def __dir__() -> list[str]:
    """Erweitert ``dir(src.world.earth)`` um alle Lazy-Exporte."""

    return sorted(
        set(globals())
        | set(_PUBLIC_SYMBOLS)
        | {
            "EXPECTED_MANIFEST_SCHEMA_VERSION",
            "MANIFEST_FILENAME",
            "MODULE_VERSION",
            "PACKAGE_ID",
            "PROVIDER_CONTRACT_VERSION",
            "PROVIDER_ID",
            "PROVIDER_WORLD_ID",
            "PUBLIC_API_VERSION",
            "TEMPLATE_ID",
            "WORLD_TYPE",
            "clear_earth_provider_caches",
            "earth_provider_cache_info",
            "earth_provider_manifest_path",
            "earth_provider_manifest_status",
            "earth_provider_module_status",
            "earth_provider_runtime_status",
            "preload_earth_provider_modules",
            "reset_earth_provider_package_state",
        }
    )


def earth_provider_manifest_path() -> Path:
    """Liefert den kanonischen Pfad der statischen Earth-Definition."""

    return Path(__file__).resolve().with_name(MANIFEST_FILENAME)


def earth_provider_manifest_status(
    *,
    include_manifest: bool = False,
) -> dict[str, Any]:
    """Prüft Existenz, JSON-Syntax und Identität von ``world.json``.

    Die Funktion führt nur Dateisystem- und JSON-Lesezugriffe aus. Die tiefe
    fachliche Validierung erfolgt später in ``validator.py``.
    """

    path = earth_provider_manifest_path()

    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return {
            "ok": False,
            "ready": False,
            "pathFingerprint": _path_fingerprint(path),
            "filename": path.name,
            "exists": False,
            "isFile": False,
            "sizeBytes": None,
            "fingerprint": None,
            "schemaVersion": None,
            "providerId": None,
            "templateId": None,
            "providerWorldId": None,
            "worldType": None,
            "errors": [
                {
                    "code": "earth_manifest_missing",
                    "message": (
                        "Die statische Earth-Providerdefinition fehlt."
                    ),
                }
            ],
        }
    except OSError as error:
        return {
            "ok": False,
            "ready": False,
            "pathFingerprint": _path_fingerprint(path),
            "filename": path.name,
            "exists": path.exists(),
            "isFile": path.is_file(),
            "sizeBytes": None,
            "fingerprint": None,
            "schemaVersion": None,
            "providerId": None,
            "templateId": None,
            "providerWorldId": None,
            "worldType": None,
            "errors": [
                {
                    "code": "earth_manifest_stat_failed",
                    "message": (
                        "Die Earth-Providerdefinition konnte nicht "
                        "statistisch geprüft werden."
                    ),
                    "causeType": type(error).__name__,
                }
            ],
        }

    if not path.is_file():
        return {
            "ok": False,
            "ready": False,
            "pathFingerprint": _path_fingerprint(path),
            "filename": path.name,
            "exists": True,
            "isFile": False,
            "sizeBytes": int(stat_result.st_size),
            "fingerprint": None,
            "schemaVersion": None,
            "providerId": None,
            "templateId": None,
            "providerWorldId": None,
            "worldType": None,
            "errors": [
                {
                    "code": "earth_manifest_not_file",
                    "message": (
                        "Der Earth-Manifestpfad ist keine reguläre Datei."
                    ),
                }
            ],
        }

    size = int(stat_result.st_size)
    if size <= 0 or size > _MAX_MANIFEST_SIZE_BYTES:
        return {
            "ok": False,
            "ready": False,
            "pathFingerprint": _path_fingerprint(path),
            "filename": path.name,
            "exists": True,
            "isFile": True,
            "sizeBytes": size,
            "fingerprint": None,
            "schemaVersion": None,
            "providerId": None,
            "templateId": None,
            "providerWorldId": None,
            "worldType": None,
            "errors": [
                {
                    "code": "earth_manifest_size_invalid",
                    "message": (
                        "Die Earth-Providerdefinition ist leer oder "
                        "überschreitet die maximale Größe."
                    ),
                    "maximumSizeBytes": _MAX_MANIFEST_SIZE_BYTES,
                }
            ],
        }

    try:
        manifest, fingerprint = _read_manifest_cached(
            str(path),
            int(stat_result.st_mtime_ns),
            size,
        )
    except Exception as error:
        return {
            "ok": False,
            "ready": False,
            "pathFingerprint": _path_fingerprint(path),
            "filename": path.name,
            "exists": True,
            "isFile": True,
            "sizeBytes": size,
            "fingerprint": None,
            "schemaVersion": None,
            "providerId": None,
            "templateId": None,
            "providerWorldId": None,
            "worldType": None,
            "errors": [
                {
                    "code": "earth_manifest_parse_failed",
                    "message": (
                        "Die Earth-Providerdefinition konnte nicht "
                        "als JSON-Objekt geladen werden."
                    ),
                    "causeType": type(error).__name__,
                }
            ],
        }

    errors = _validate_manifest_identity(manifest)
    payload: dict[str, Any] = {
        "ok": not errors,
        "ready": not errors,
        "pathFingerprint": _path_fingerprint(path),
        "filename": path.name,
        "exists": True,
        "isFile": True,
        "sizeBytes": size,
        "fingerprint": fingerprint,
        "schemaVersion": manifest.get("schemaVersion"),
        "providerId": manifest.get("providerId"),
        "templateId": manifest.get("templateId"),
        "providerWorldId": manifest.get("providerWorldId"),
        "worldType": manifest.get("worldType"),
        "generatorType": manifest.get("generatorType"),
        "topologyType": manifest.get("topologyType"),
        "coordinateSystemId": manifest.get("coordinateSystemId"),
        "gridId": manifest.get("gridId"),
        "gridVersion": manifest.get("gridVersion"),
        "errors": errors,
    }

    if include_manifest:
        payload["manifest"] = manifest

    return payload


@lru_cache(maxsize=8)
def _read_manifest_cached(
    path_text: str,
    mtime_ns: int,
    size_bytes: int,
) -> tuple[dict[str, Any], str]:
    """Lädt JSON anhand Dateipfad und Stat-Signatur reproduzierbar."""

    # Die Parameter mtime_ns und size_bytes sind absichtlich Teil des
    # Cache-Keys. Sie werden im Funktionskörper nur zur Konsistenz geprüft.
    path = Path(path_text)

    stat_result = path.stat()
    if (
        int(stat_result.st_mtime_ns) != mtime_ns
        or int(stat_result.st_size) != size_bytes
    ):
        raise RuntimeError(
            "Earth-Manifest wurde während des Lesevorgangs verändert."
        )

    raw = path.read_bytes()
    if len(raw) != size_bytes:
        raise RuntimeError(
            "Earth-Manifestgröße änderte sich während des Lesens."
        )

    fingerprint = sha256(raw).hexdigest()

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            "Earth-Manifest muss UTF-8-kodiert sein."
        ) from error

    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as error:
        raise ValueError(
            "Earth-Manifest enthält ungültiges JSON."
        ) from error

    if not isinstance(parsed, dict):
        raise ValueError(
            "Earth-Manifest muss ein JSON-Objekt sein."
        )

    return parsed, fingerprint


def preload_earth_provider_modules(
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Lädt Validator, Generator und Provider kontrolliert vor."""

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
        "providerContractVersion": (
            PROVIDER_CONTRACT_VERSION
        ),
        "providerId": PROVIDER_ID,
        "moduleCount": len(statuses),
        "readyModuleCount": sum(
            1 for item in statuses if item["ready"]
        ),
        "modules": statuses,
    }

    if strict and first_error is not None:
        raise first_error

    return result


def earth_provider_module_status(
    *,
    preload: bool = False,
    include_manifest_status: bool = True,
    include_cache_info: bool = False,
) -> dict[str, Any]:
    """Liefert Paket-, Modul-, Manifest- und optional Cache-Status."""

    modules: list[dict[str, Any]] = []

    for module_path in _DIAGNOSTIC_MODULES:
        qualified_name = f"{__name__}{module_path}"

        if preload:
            try:
                module = _load_relative_module(module_path)
                modules.append(
                    {
                        "module": module.__name__,
                        "relativeModule": module_path,
                        "loaded": True,
                        "ready": True,
                        "error": None,
                    }
                )
            except Exception as error:
                modules.append(
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

        modules.append(
            {
                "module": (
                    module.__name__
                    if module is not None
                    else qualified_name
                ),
                "relativeModule": module_path,
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
            all(item["ready"] for item in modules)
            if preload
            else True
        ),
        "packageId": PACKAGE_ID,
        "moduleVersion": MODULE_VERSION,
        "publicApiVersion": PUBLIC_API_VERSION,
        "providerContractVersion": (
            PROVIDER_CONTRACT_VERSION
        ),
        "providerId": PROVIDER_ID,
        "templateId": TEMPLATE_ID,
        "providerWorldId": PROVIDER_WORLD_ID,
        "worldType": WORLD_TYPE,
        "lazyImportsEnabled": True,
        "publicSymbolCount": len(_PUBLIC_SYMBOLS),
        "loadedSymbolCount": len(loaded_symbols),
        "loadedSymbols": list(loaded_symbols),
        "knownModuleCount": len(_DIAGNOSTIC_MODULES),
        "loadedModuleCount": loaded_module_count,
        "modules": modules,
    }

    if include_manifest_status:
        manifest = earth_provider_manifest_status()
        payload["manifest"] = manifest
        if preload and not manifest["ok"]:
            payload["ok"] = False

    if include_cache_info:
        try:
            payload["caches"] = earth_provider_cache_info()
        except Exception as error:
            payload["caches"] = {
                "ok": False,
                "error": _safe_error_summary(error),
            }
            if preload:
                payload["ok"] = False

    return payload


def earth_provider_runtime_status() -> dict[str, Any]:
    """Aggregiert Manifest, Validator, Generator und Provider read-only."""

    checks: dict[str, Any] = {
        "manifest": earth_provider_manifest_status(),
    }
    errors: list[dict[str, Any]] = []

    component_calls = (
        (
            "definition",
            ".validator",
            "earth_world_definition_status",
        ),
        (
            "generator",
            ".generator",
            "earth_generator_runtime_status",
        ),
        (
            "provider",
            ".provider",
            "earth_provider_component_status",
        ),
    )

    for component, module_path, function_name in component_calls:
        try:
            module = _load_relative_module(module_path)
            function = getattr(module, function_name)
            result = function()
            checks[component] = result

            if not isinstance(result, Mapping):
                raise TypeError(
                    f"{function_name} muss ein Mapping liefern."
                )

            if not bool(result.get("ok")):
                errors.append(
                    {
                        "component": component,
                        "code": f"earth_{component}_not_ready",
                        "message": (
                            f"Earth-Komponente {component} ist nicht bereit."
                        ),
                    }
                )
        except Exception as error:
            checks[component] = {
                "ok": False,
                "error": _safe_error_summary(error),
            }
            errors.append(
                {
                    "component": component,
                    **_safe_error_summary(error),
                }
            )

    if not checks["manifest"]["ok"]:
        errors.append(
            {
                "component": "manifest",
                "code": "earth_manifest_not_ready",
                "message": (
                    "Die statische Earth-Providerdefinition "
                    "ist nicht bereit."
                ),
            }
        )

    component_ok = all(
        bool(checks.get(name, {}).get("ok"))
        for name in (
            "manifest",
            "definition",
            "generator",
            "provider",
        )
    )

    return {
        "ok": bool(component_ok and not errors),
        "packageId": PACKAGE_ID,
        "moduleVersion": MODULE_VERSION,
        "publicApiVersion": PUBLIC_API_VERSION,
        "providerContractVersion": (
            PROVIDER_CONTRACT_VERSION
        ),
        "providerId": PROVIDER_ID,
        "checks": checks,
        "caches": earth_provider_cache_info(),
        "errors": errors,
    }


def earth_provider_cache_info() -> dict[str, Any]:
    """Liefert Cacheinformationen der Earth-Provider-Schichten."""

    with _STATE_LOCK:
        loaded_symbol_count = len(_SYMBOL_CACHE)
        loaded_module_count = len(_MODULE_CACHE)

    payload: dict[str, Any] = {
        "ok": True,
        "lazySymbolCache": {
            "currentSize": loaded_symbol_count,
            "maximumSize": len(_PUBLIC_SYMBOLS),
        },
        "lazyModuleCache": {
            "currentSize": loaded_module_count,
            "maximumSize": len(_DIAGNOSTIC_MODULES),
        },
        "manifest": _cache_info_to_dict(
            _read_manifest_cached.cache_info()
        ),
        "definition": None,
        "generator": None,
        "provider": None,
        "errors": [],
    }

    component_cache_functions = (
        (
            "definition",
            ".validator",
            "earth_world_definition_cache_info",
        ),
        (
            "generator",
            ".generator",
            "earth_generator_cache_info",
        ),
        (
            "provider",
            ".provider",
            "earth_provider_component_cache_info",
        ),
    )

    errors: list[dict[str, Any]] = payload["errors"]

    for component, module_path, function_name in (
        component_cache_functions
    ):
        try:
            module = _load_relative_module(module_path)
            function = getattr(module, function_name, None)

            if function is None:
                payload[component] = {
                    "available": False,
                    "reason": "cache_info_not_implemented",
                }
                continue

            payload[component] = function()
        except Exception as error:
            payload[component] = {
                "available": False,
                "error": _safe_error_summary(error),
            }
            errors.append(
                {
                    "component": component,
                    **_safe_error_summary(error),
                }
            )

    payload["ok"] = not errors
    return payload


def clear_earth_provider_caches() -> dict[str, Any]:
    """Leert Provider-, Generator-, Definition- und Manifest-Caches.

    Die Reihenfolge ist absichtlich von der höchsten zur niedrigsten Schicht.
    Fehlschläge werden gesammelt; alle übrigen Cache-Resets werden trotzdem
    versucht.
    """

    operations = (
        (
            "provider",
            ".provider",
            "clear_earth_provider_component_caches",
        ),
        (
            "generator",
            ".generator",
            "clear_earth_generator_caches",
        ),
        (
            "definition",
            ".validator",
            "clear_earth_world_definition_cache",
        ),
    )

    cleared: list[str] = []
    errors: list[dict[str, Any]] = []
    results: dict[str, Any] = {}

    for component, module_path, function_name in operations:
        try:
            module = _load_relative_module(module_path)
            function = getattr(module, function_name)
            result = function()
            results[component] = result
            cleared.append(component)
        except Exception as error:
            results[component] = {
                "ok": False,
                "error": _safe_error_summary(error),
            }
            errors.append(
                {
                    "component": component,
                    **_safe_error_summary(error),
                }
            )

    _read_manifest_cached.cache_clear()
    cleared.append("manifest")

    return {
        "ok": not errors,
        "cleared": cleared,
        "results": results,
        "errors": errors,
        "remaining": earth_provider_cache_info(),
    }


def reset_earth_provider_package_state(
    *,
    clear_runtime_caches: bool = True,
    clear_lazy_symbols: bool = False,
) -> dict[str, Any]:
    """Setzt kontrolliert ableitbaren Paketstatus zurück."""

    cleared: list[str] = []

    if clear_runtime_caches:
        clear_earth_provider_caches()
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
        "status": earth_provider_module_status(
            preload=False,
            include_manifest_status=True,
            include_cache_info=True,
        ),
    }


def _load_relative_module(module_path: str) -> ModuleType:
    """Lädt ein freigegebenes Earth-Untermodul thread-sicher."""

    if module_path not in _DIAGNOSTIC_MODULES:
        raise ImportError(
            f"Nicht freigegebenes Earth-Untermodul: {module_path!r}."
        )

    with _STATE_LOCK:
        cached = _MODULE_CACHE.get(module_path)
        if cached is not None:
            return cached

        module = import_module(module_path, package=__name__)
        _MODULE_CACHE[module_path] = module
        return module


def _validate_manifest_identity(
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []

    expected = {
        "schemaVersion": EXPECTED_MANIFEST_SCHEMA_VERSION,
        "providerId": PROVIDER_ID,
        "templateId": TEMPLATE_ID,
        "providerWorldId": PROVIDER_WORLD_ID,
        "worldType": WORLD_TYPE,
    }

    for field_name, expected_value in expected.items():
        actual = manifest.get(field_name)
        if actual != expected_value:
            errors.append(
                {
                    "code": "earth_manifest_identity_mismatch",
                    "field": field_name,
                    "expected": expected_value,
                    "actual": (
                        actual
                        if isinstance(
                            actual,
                            (str, int, float, bool),
                        )
                        or actual is None
                        else {"type": type(actual).__name__}
                    ),
                }
            )

    return errors


def _path_fingerprint(path: Path) -> str:
    return sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _cache_info_to_dict(cache_info: Any) -> dict[str, Any]:
    return {
        "hits": int(cache_info.hits),
        "misses": int(cache_info.misses),
        "maxSize": (
            int(cache_info.maxsize)
            if cache_info.maxsize is not None
            else None
        ),
        "currentSize": int(cache_info.currsize),
    }


def _safe_error_summary(
    error: BaseException,
) -> dict[str, Any]:
    message = (
        str(error).strip()
        or "Earth-Provideroperation fehlgeschlagen."
    )
    if len(message) > _MAX_STATUS_ERROR_LENGTH:
        message = (
            message[: _MAX_STATUS_ERROR_LENGTH - 3]
            + "..."
        )

    payload: dict[str, Any] = {
        "type": type(error).__name__,
        "message": message,
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
    "EXPECTED_MANIFEST_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "MODULE_VERSION",
    "PACKAGE_ID",
    "PROVIDER_CONTRACT_VERSION",
    "PROVIDER_ID",
    "PROVIDER_WORLD_ID",
    "PUBLIC_API_VERSION",
    "TEMPLATE_ID",
    "WORLD_TYPE",
    "clear_earth_provider_caches",
    "earth_provider_cache_info",
    "earth_provider_manifest_path",
    "earth_provider_manifest_status",
    "earth_provider_module_status",
    "earth_provider_runtime_status",
    "preload_earth_provider_modules",
    "reset_earth_provider_package_state",
]
