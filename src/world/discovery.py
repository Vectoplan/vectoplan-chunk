# src/world/discovery.py
"""
VECTOPLAN World Discovery.

Diese Datei scannt die verfügbaren Weltmodelle unterhalb von:

    src/world/

Ziel:
- alle potenziellen World-Provider-Ordner erkennen
- provider.py prüfen
- Provider-Modul defensiv importieren
- Provider-Vertrag prüfen
- world.json finden und laden
- Config validieren
- WorldDefinition erzeugen
- gültige und ungültige Weltmodelle als Diagnose zurückgeben
- aus gültigen Welten eine temporäre WorldRegistry erzeugen

Diese Datei ist für Debug-/Test-Routen gedacht, z. B.:

    GET /world-test/api/worlds

Wichtig:
- keine Flask-Abhängigkeit
- keine Datenbank
- keine Snapshots
- keine Events
- keine Commands
- keine Three.js-Objekte
- keine konkrete Flat-World-Logik

Die Discovery-Schicht ist bewusst robuster und dynamischer als die feste
Default-Registry. Die feste Registry kennt zunächst nur:

    flat → src.world.flat.provider

Discovery scannt dagegen tatsächlich vorhandene Ordner wie:

    src/world/flat/
    src/world/realWorld/
    src/world/devTerrain/

Ein Ordner gilt als potenzielles Weltmodell, wenn er unter src/world liegt
und nicht zu den reservierten Infrastrukturmodulen gehört.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import Any, Final

try:
    from src.world.errors import (
        InvalidWorldConfigFileError,
        WorldError,
        WorldProviderContractError,
        WorldProviderImportError,
        WorldRegistryError,
        coerce_world_error,
        make_json_safe,
    )
    from src.world.models import WorldDefinition, WorldProviderInfo
    from src.world.registry import WorldRegistry
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.discovery requires src.world.errors, src.world.models and "
        "src.world.registry to be importable before discovery can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORLD_DISCOVERY_VERSION: Final[str] = "0.1.0"

DEFAULT_WORLD_PACKAGE_NAME: Final[str] = "src.world"

PROVIDER_FILENAME: Final[str] = "provider.py"
DEFAULT_CONFIG_FILENAME: Final[str] = "world.json"

MAX_CONFIG_FILE_BYTES: Final[int] = 10 * 1024 * 1024

RESERVED_WORLD_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {
        "",
        ".",
        "..",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "tests",
        "test",
        "unit",
        "integration",
        "e2e",
        "bootstrap",
        "api",
        "utils",
        "repositories",
        "exchange",
        "coordinates",
        "chunks",
        "commands",
        "events",
        "blocks",
        "models",
        "routes",
        "static",
        "templates",
    }
)

RESERVED_WORLD_MODULE_FILENAMES: Final[frozenset[str]] = frozenset(
    {
        "__init__.py",
        "errors.py",
        "models.py",
        "registry.py",
        "loader.py",
        "service.py",
        "serializer.py",
        "discovery.py",
    }
)

REQUIRED_PROVIDER_FUNCTIONS: Final[tuple[str, ...]] = (
    "get_provider_info",
    "get_default_config_path",
    "load_world_config",
    "validate_world_config",
    "create_world_definition",
    "generate_chunk",
)

OPTIONAL_PROVIDER_FUNCTIONS: Final[tuple[str, ...]] = (
    "get_provider_status",
    "require_provider_ready",
    "get_provider_contract",
)

ALL_KNOWN_PROVIDER_FUNCTIONS: Final[tuple[str, ...]] = (
    *REQUIRED_PROVIDER_FUNCTIONS,
    *OPTIONAL_PROVIDER_FUNCTIONS,
)

DISCOVERY_STATUS_VALID: Final[str] = "valid"
DISCOVERY_STATUS_INVALID: Final[str] = "invalid"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt beliebige Werte defensiv in einen bereinigten String um.
    """
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


def _safe_bool(value: Any, *, default: bool = False) -> bool:
    """
    Wandelt typische Werte robust in bool um.
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int | float):
        return bool(value)

    text = _safe_str(value).lower()

    if text in {"1", "true", "yes", "y", "on"}:
        return True

    if text in {"0", "false", "no", "n", "off"}:
        return False

    return default


def _json_safe_dict(value: Any) -> dict[str, Any]:
    """
    Normalisiert beliebige Werte in ein JSON-sicheres Dictionary.
    """
    if value is None:
        return {}

    safe = make_json_safe(value)

    if isinstance(safe, Mapping):
        return dict(safe)

    return {"value": safe}


def _append_issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    **extra: Any,
) -> None:
    """
    Ergänzt eine strukturierte Warning/Error-Meldung.
    """
    item: dict[str, Any] = {
        "code": code,
        "message": message,
    }

    for key, value in extra.items():
        item[key] = make_json_safe(value)

    issues.append(item)


def _dedupe_preserve_order(values: Iterable[str]) -> tuple[str, ...]:
    """
    Entfernt Duplikate und erhält die Reihenfolge.
    """
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        text = _safe_str(value)

        if not text:
            continue

        if text in seen:
            continue

        seen.add(text)
        result.append(text)

    return tuple(result)


def _module_exists(module_path: str) -> bool:
    """
    Prüft defensiv, ob ein Modul grundsätzlich auffindbar ist.

    Das Modul wird nicht importiert.
    """
    try:
        return find_spec(module_path) is not None
    except Exception:
        return False


def _import_module_safe(module_path: str) -> ModuleType:
    """
    Importiert ein Modul robust und übersetzt Fehler in WorldProviderImportError.
    """
    try:
        module = import_module(module_path)
    except ModuleNotFoundError as exc:
        raise WorldProviderImportError(
            "World provider module could not be found.",
            details={
                "providerModule": module_path,
            },
            cause=exc,
        ) from exc
    except Exception as exc:
        raise WorldProviderImportError(
            "World provider module could not be imported.",
            details={
                "providerModule": module_path,
            },
            cause=exc,
        ) from exc

    if not isinstance(module, ModuleType):
        raise WorldProviderImportError(
            "Imported world provider is not a module.",
            details={
                "providerModule": module_path,
                "importedType": type(module).__name__,
            },
        )

    return module


def _get_callable(module: ModuleType, name: str) -> Callable[..., Any] | None:
    """
    Holt ein Callable-Attribut aus einem Modul.
    """
    try:
        value = getattr(module, name, None)
    except Exception:
        return None

    return value if callable(value) else None


def _inspect_provider_functions(module: ModuleType) -> dict[str, bool]:
    """
    Prüft alle bekannten Provider-Funktionen auf Callable-Status.
    """
    result: dict[str, bool] = {}

    for function_name in ALL_KNOWN_PROVIDER_FUNCTIONS:
        result[function_name] = _get_callable(module, function_name) is not None

    return result


def _missing_required_functions(functions: Mapping[str, bool]) -> tuple[str, ...]:
    """
    Gibt fehlende Pflichtfunktionen zurück.
    """
    return tuple(
        function_name
        for function_name in REQUIRED_PROVIDER_FUNCTIONS
        if not bool(functions.get(function_name))
    )


def _call_provider_function(
    function: Callable[..., Any],
    *args: Any,
    function_name: str,
    provider_module: str,
    **kwargs: Any,
) -> Any:
    """
    Ruft eine Provider-Funktion robust auf.
    """
    try:
        return function(*args, **kwargs)
    except TypeError as exc:
        raise WorldProviderContractError(
            f"Provider function '{function_name}' could not be called with expected arguments.",
            details={
                "providerModule": provider_module,
                "function": function_name,
                "argsCount": len(args),
                "kwargs": sorted(kwargs.keys()),
            },
            cause=exc,
        ) from exc
    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message=f"Provider function '{function_name}' failed.",
            fallback_code="world_provider_function_failed",
            fallback_status_code=500,
            details={
                "providerModule": provider_module,
                "function": function_name,
            },
        )
        raise world_error from exc


def _call_provider_function_flexible(
    function: Callable[..., Any],
    *,
    function_name: str,
    provider_module: str,
    call_patterns: tuple[tuple[str, tuple[Any, ...]], ...],
) -> Any:
    """
    Ruft eine Provider-Funktion mit mehreren Signaturvarianten auf.

    Nützlich für Provider-Funktionen, die optional einen config_path annehmen.
    """
    attempts: list[dict[str, Any]] = []

    for pattern_name, args in call_patterns:
        try:
            return function(*args)
        except TypeError as exc:
            attempts.append(
                {
                    "pattern": pattern_name,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                }
            )
            continue
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message=f"Provider function '{function_name}' failed.",
                fallback_code="world_provider_function_failed",
                fallback_status_code=500,
                details={
                    "providerModule": provider_module,
                    "function": function_name,
                    "pattern": pattern_name,
                },
            )
            raise world_error from exc

    raise WorldProviderContractError(
        f"Provider function '{function_name}' could not be called with supported signatures.",
        details={
            "providerModule": provider_module,
            "function": function_name,
            "attempts": attempts,
        },
    )


def _path_from_value(value: Any) -> Path | None:
    """
    Wandelt einen beliebigen Pfadwert robust in Path um.
    """
    if value is None:
        return None

    try:
        return Path(value)
    except Exception:
        return None


def _read_json_file(path: Path) -> dict[str, Any]:
    """
    Liest eine JSON-Datei robust und gibt ein Dictionary zurück.
    """
    try:
        resolved = path.expanduser()
    except Exception:
        resolved = path

    if not resolved.exists():
        raise InvalidWorldConfigFileError(
            "World config file does not exist.",
            details={
                "configPath": str(resolved),
            },
        )

    if not resolved.is_file():
        raise InvalidWorldConfigFileError(
            "World config path is not a file.",
            details={
                "configPath": str(resolved),
            },
        )

    try:
        size = resolved.stat().st_size
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not inspect world config file.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc

    if size > MAX_CONFIG_FILE_BYTES:
        raise InvalidWorldConfigFileError(
            "World config file is too large.",
            details={
                "configPath": str(resolved),
                "sizeBytes": size,
                "maxBytes": MAX_CONFIG_FILE_BYTES,
            },
        )

    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidWorldConfigFileError(
            "World config file must be UTF-8 encoded.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not read world config file.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidWorldConfigFileError(
            "World config file contains invalid JSON.",
            details={
                "configPath": str(resolved),
                "line": exc.lineno,
                "column": exc.colno,
                "message": exc.msg,
            },
            cause=exc,
        ) from exc

    if not isinstance(parsed, Mapping):
        raise InvalidWorldConfigFileError(
            "World config JSON root must be an object.",
            details={
                "configPath": str(resolved),
                "rootType": type(parsed).__name__,
            },
        )

    return dict(parsed)


def _normalize_raw_config(value: Any) -> dict[str, Any]:
    """
    Normalisiert rohe Config-Daten.
    """
    if isinstance(value, WorldDefinition):
        return value.raw_config or value.to_dict(camel_case=True)

    if isinstance(value, Mapping):
        return dict(value)

    raise InvalidWorldConfigFileError(
        "World provider returned invalid config object.",
        details={
            "configType": type(value).__name__,
            "config": make_json_safe(value),
        },
    )


def _normalize_world_definition(value: Any, *, raw_config: Mapping[str, Any] | None = None) -> WorldDefinition:
    """
    Normalisiert ein Provider-Ergebnis zu WorldDefinition.
    """
    if isinstance(value, WorldDefinition):
        value.validate()
        return value

    if isinstance(value, Mapping):
        definition = WorldDefinition.from_dict(value)
        definition.validate()
        return definition

    if value is None and raw_config is not None:
        definition = WorldDefinition.from_dict(raw_config)
        definition.validate()
        return definition

    raise WorldProviderContractError(
        "Provider returned invalid WorldDefinition.",
        details={
            "definitionType": type(value).__name__,
            "definition": make_json_safe(value),
        },
    )


def _provider_info_to_dict(value: Any) -> dict[str, Any]:
    """
    Normalisiert ProviderInfo in ein Dictionary.
    """
    if isinstance(value, WorldProviderInfo):
        return value.to_dict(camel_case=True)

    if isinstance(value, Mapping):
        return make_json_safe(dict(value))

    if hasattr(value, "to_dict") and callable(value.to_dict):
        result = value.to_dict()
        if isinstance(result, Mapping):
            return make_json_safe(dict(result))

    raise WorldProviderContractError(
        "get_provider_info must return WorldProviderInfo or mapping.",
        details={
            "returnType": type(value).__name__,
            "returnValue": make_json_safe(value),
        },
    )


def get_world_package_dir() -> Path:
    """
    Gibt den src/world-Ordner zurück.

    Da discovery.py selbst in src/world liegt, ist der Parent von __file__
    der erwartete Scan-Ordner.
    """
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path.cwd() / "src" / "world"


def _is_reserved_candidate_dir(path: Path) -> bool:
    """
    Prüft, ob ein Ordner für World-Discovery ignoriert werden soll.
    """
    name = path.name

    if not name:
        return True

    if name in RESERVED_WORLD_DIR_NAMES:
        return True

    if name.startswith("."):
        return True

    if name.startswith("__") and name.endswith("__"):
        return True

    return False


def _looks_like_python_package(path: Path) -> bool:
    """
    Prüft, ob ein Ordner grundsätzlich ein Python-Package sein kann.
    """
    try:
        return (path / "__init__.py").is_file()
    except Exception:
        return False


def _candidate_module_path(package_name: str, folder_name: str) -> str:
    """
    Baut den Provider-Modulpfad für einen World-Ordner.
    """
    return f"{package_name}.{folder_name}.provider"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorldProviderCandidate:
    """
    Ein möglicher World-Provider-Ordner unter src/world.
    """

    folder_name: str
    package_name: str
    package_path: str
    provider_module: str
    provider_path: str
    config_path: str
    has_package_init: bool
    has_provider_file: bool
    has_config_file: bool
    ignored: bool = False
    ignore_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert den Kandidaten.
        """
        return {
            "folderName": self.folder_name,
            "packageName": self.package_name,
            "packagePath": self.package_path,
            "providerModule": self.provider_module,
            "providerPath": self.provider_path,
            "configPath": self.config_path,
            "hasPackageInit": self.has_package_init,
            "hasProviderFile": self.has_provider_file,
            "hasConfigFile": self.has_config_file,
            "ignored": self.ignored,
            "ignoreReason": self.ignore_reason,
            "metadata": make_json_safe(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DiscoveredWorldProvider:
    """
    Ergebnis der Prüfung eines einzelnen World-Provider-Kandidaten.
    """

    folder_name: str
    provider_module: str
    package_path: str
    provider_path: str
    config_path: str | None

    provider_id: str | None = None
    world_id: str | None = None
    world_type: str | None = None
    label: str | None = None

    valid: bool = False
    status: str = DISCOVERY_STATUS_INVALID

    has_package_init: bool = False
    has_provider_file: bool = False
    importable: bool = False
    config_exists: bool = False
    config_loaded: bool = False
    config_valid: bool = False
    definition_created: bool = False
    supports_chunk_generation: bool = False

    callable_functions: dict[str, bool] = field(default_factory=dict)
    missing_required_functions: tuple[str, ...] = field(default_factory=tuple)

    errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    provider_info: dict[str, Any] = field(default_factory=dict)
    world_metadata: dict[str, Any] = field(default_factory=dict)
    raw_config: dict[str, Any] = field(default_factory=dict, repr=False)

    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def registry_provider_id(self) -> str:
        """
        Provider-ID für eine temporäre Registry.
        """
        return _safe_str(self.provider_id, default=self.folder_name)

    @property
    def registry_world_type(self) -> str:
        """
        World-Type für eine temporäre Registry.
        """
        return _safe_str(self.world_type, default=self.folder_name)

    @property
    def registry_label(self) -> str:
        """
        Label für eine temporäre Registry.
        """
        return _safe_str(self.label, default=self.registry_provider_id)

    @property
    def is_usable_for_generation(self) -> bool:
        """
        Gibt zurück, ob dieser Provider für Chunk-Generierung nutzbar ist.
        """
        return bool(self.valid and self.supports_chunk_generation)

    def to_registry_definition(self) -> dict[str, Any]:
        """
        Baut eine Registry-Definition aus dem Discovery-Ergebnis.
        """
        if not self.valid:
            raise WorldRegistryError(
                "Cannot create registry definition from invalid discovered world provider.",
                details={
                    "folderName": self.folder_name,
                    "providerModule": self.provider_module,
                    "errors": self.errors,
                },
            )

        aliases = _dedupe_preserve_order(
            value
            for value in (
                self.folder_name,
                self.world_id,
                self.provider_id,
            )
            if value
        )

        return {
            "providerId": self.registry_provider_id,
            "worldType": self.registry_world_type,
            "label": self.registry_label,
            "providerModule": self.provider_module,
            "configPath": self.config_path,
            "supportsChunkGeneration": self.supports_chunk_generation,
            "supportsWorldMetadata": bool(self.world_metadata),
            "aliases": aliases,
            "metadata": {
                "discovered": True,
                "discoveryVersion": WORLD_DISCOVERY_VERSION,
                "folderName": self.folder_name,
                "packagePath": self.package_path,
                "providerPath": self.provider_path,
                "worldId": self.world_id,
                "warnings": list(self.warnings),
            },
        }

    def to_dict(
        self,
        *,
        include_raw_config: bool = False,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        """
        Serialisiert das Discovery-Ergebnis.
        """
        payload: dict[str, Any] = {
            "folderName": self.folder_name,
            "providerId": self.provider_id,
            "worldId": self.world_id,
            "worldType": self.world_type,
            "label": self.label,
            "valid": self.valid,
            "status": self.status,
            "providerModule": self.provider_module,
            "packagePath": self.package_path,
            "providerPath": self.provider_path,
            "configPath": self.config_path,
            "hasPackageInit": self.has_package_init,
            "hasProviderFile": self.has_provider_file,
            "importable": self.importable,
            "configExists": self.config_exists,
            "configLoaded": self.config_loaded,
            "configValid": self.config_valid,
            "definitionCreated": self.definition_created,
            "supportsChunkGeneration": self.supports_chunk_generation,
            "callableFunctions": dict(self.callable_functions),
            "missingRequiredFunctions": list(self.missing_required_functions),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "providerInfo": make_json_safe(self.provider_info),
            "worldMetadata": make_json_safe(self.world_metadata),
            "isUsableForGeneration": self.is_usable_for_generation,
        }

        if include_raw_config:
            payload["rawConfig"] = make_json_safe(self.raw_config)

        if include_metadata:
            payload["metadata"] = make_json_safe(self.metadata)

        return payload


@dataclass(frozen=True, slots=True)
class WorldDiscoveryResult:
    """
    Ergebnis eines vollständigen Discovery-Scans.
    """

    package_name: str
    base_dir: str
    providers: tuple[DiscoveredWorldProvider, ...]
    candidates: tuple[WorldProviderCandidate, ...] = field(default_factory=tuple)
    errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def scanned_count(self) -> int:
        return len(self.candidates)

    @property
    def provider_count(self) -> int:
        return len(self.providers)

    @property
    def valid_providers(self) -> tuple[DiscoveredWorldProvider, ...]:
        return tuple(provider for provider in self.providers if provider.valid)

    @property
    def invalid_providers(self) -> tuple[DiscoveredWorldProvider, ...]:
        return tuple(provider for provider in self.providers if not provider.valid)

    @property
    def valid_count(self) -> int:
        return len(self.valid_providers)

    @property
    def invalid_count(self) -> int:
        return len(self.invalid_providers)

    @property
    def default_world_id(self) -> str | None:
        """
        Gibt eine sinnvolle Default-Welt aus Discovery zurück.
        """
        for provider in self.valid_providers:
            if provider.world_id == "flat" or provider.provider_id == "flat":
                return provider.registry_provider_id

        if self.valid_providers:
            return self.valid_providers[0].registry_provider_id

        return None

    def get_provider(self, world_id_or_provider_id: str) -> DiscoveredWorldProvider | None:
        """
        Findet einen Provider über folderName, providerId oder worldId.
        """
        key = _safe_str(world_id_or_provider_id)

        if not key:
            return None

        for provider in self.providers:
            if key in {
                _safe_str(provider.folder_name),
                _safe_str(provider.provider_id),
                _safe_str(provider.world_id),
            }:
                return provider

        return None

    def require_provider(self, world_id_or_provider_id: str) -> DiscoveredWorldProvider:
        """
        Findet einen Provider oder wirft einen WorldRegistryError.
        """
        provider = self.get_provider(world_id_or_provider_id)

        if provider is not None:
            return provider

        raise WorldRegistryError(
            "Discovered world provider was not found.",
            details={
                "worldId": world_id_or_provider_id,
                "availableWorlds": [
                    item.to_dict(include_metadata=False)
                    for item in self.providers
                ],
            },
        )

    def to_registry(
        self,
        *,
        default_world_id: str | None = None,
        include_invalid: bool = False,
        strict: bool = False,
    ) -> WorldRegistry:
        """
        Baut eine temporäre WorldRegistry aus dem Discovery-Ergebnis.
        """
        return create_registry_from_discovery_result(
            self,
            default_world_id=default_world_id,
            include_invalid=include_invalid,
            strict=strict,
        )

    def to_dict(
        self,
        *,
        include_raw_config: bool = False,
        include_invalid: bool = True,
        include_candidates: bool = True,
    ) -> dict[str, Any]:
        """
        Serialisiert das vollständige Discovery-Ergebnis.
        """
        providers = (
            self.providers
            if include_invalid
            else self.valid_providers
        )

        payload: dict[str, Any] = {
            "packageName": self.package_name,
            "baseDir": self.base_dir,
            "scannedCount": self.scanned_count,
            "providerCount": self.provider_count,
            "validCount": self.valid_count,
            "invalidCount": self.invalid_count,
            "defaultWorldId": self.default_world_id,
            "providers": [
                provider.to_dict(
                    include_raw_config=include_raw_config,
                    include_metadata=True,
                )
                for provider in providers
            ],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metadata": make_json_safe(self.metadata),
        }

        if include_candidates:
            payload["candidates"] = [
                candidate.to_dict()
                for candidate in self.candidates
            ]

        return payload


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan_world_provider_packages(
    *,
    base_dir: str | Path | None = None,
    package_name: str = DEFAULT_WORLD_PACKAGE_NAME,
    include_missing_provider: bool = True,
) -> tuple[WorldProviderCandidate, ...]:
    """
    Scannt src/world nach potenziellen World-Provider-Ordnern.

    Ein Kandidat ist jeder nicht reservierte Unterordner.

    Wenn include_missing_provider=True ist, werden auch Ordner ohne provider.py
    als ungültige Kandidaten zurückgegeben. Das ist für Diagnose hilfreich.
    """
    scan_dir = Path(base_dir) if base_dir is not None else get_world_package_dir()

    try:
        resolved_base_dir = scan_dir.resolve()
    except Exception:
        resolved_base_dir = scan_dir

    if not resolved_base_dir.exists():
        raise WorldRegistryError(
            "World discovery base directory does not exist.",
            details={
                "baseDir": str(resolved_base_dir),
            },
        )

    if not resolved_base_dir.is_dir():
        raise WorldRegistryError(
            "World discovery base path is not a directory.",
            details={
                "baseDir": str(resolved_base_dir),
            },
        )

    candidates: list[WorldProviderCandidate] = []

    try:
        children = sorted(
            resolved_base_dir.iterdir(),
            key=lambda path: path.name.lower(),
        )
    except Exception as exc:
        raise WorldRegistryError(
            "Could not scan world discovery directory.",
            details={
                "baseDir": str(resolved_base_dir),
            },
            cause=exc,
        ) from exc

    for child in children:
        if not child.is_dir():
            continue

        if _is_reserved_candidate_dir(child):
            continue

        folder_name = child.name
        provider_path = child / PROVIDER_FILENAME
        config_path = child / DEFAULT_CONFIG_FILENAME
        has_provider_file = provider_path.is_file()

        if not has_provider_file and not include_missing_provider:
            continue

        candidate = WorldProviderCandidate(
            folder_name=folder_name,
            package_name=package_name,
            package_path=str(child),
            provider_module=_candidate_module_path(package_name, folder_name),
            provider_path=str(provider_path),
            config_path=str(config_path),
            has_package_init=_looks_like_python_package(child),
            has_provider_file=has_provider_file,
            has_config_file=config_path.is_file(),
            ignored=False,
            ignore_reason=None,
            metadata={
                "discoveryVersion": WORLD_DISCOVERY_VERSION,
            },
        )
        candidates.append(candidate)

    return tuple(candidates)


# ---------------------------------------------------------------------------
# Single-provider discovery
# ---------------------------------------------------------------------------

def discover_world_provider(
    candidate: WorldProviderCandidate,
    *,
    validate_config: bool = True,
) -> DiscoveredWorldProvider:
    """
    Prüft einen einzelnen WorldProviderCandidate vollständig.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    provider_id: str | None = None
    world_id: str | None = None
    world_type: str | None = None
    label: str | None = None

    provider_info: dict[str, Any] = {}
    world_metadata: dict[str, Any] = {}
    raw_config: dict[str, Any] = {}

    callable_functions: dict[str, bool] = {}
    missing_required_functions: tuple[str, ...] = REQUIRED_PROVIDER_FUNCTIONS

    importable = False
    config_exists = False
    config_loaded = False
    config_valid = False
    definition_created = False
    supports_chunk_generation = False

    config_path: str | None = candidate.config_path

    if not candidate.has_package_init:
        _append_issue(
            warnings,
            code="missing_package_init",
            message="World provider folder has no __init__.py.",
            folderName=candidate.folder_name,
            packagePath=candidate.package_path,
        )

    if not candidate.has_provider_file:
        _append_issue(
            errors,
            code="missing_provider_file",
            message="World provider folder does not contain provider.py.",
            folderName=candidate.folder_name,
            providerPath=candidate.provider_path,
        )

        return DiscoveredWorldProvider(
            folder_name=candidate.folder_name,
            provider_module=candidate.provider_module,
            package_path=candidate.package_path,
            provider_path=candidate.provider_path,
            config_path=config_path,
            provider_id=provider_id or candidate.folder_name,
            world_id=world_id,
            world_type=world_type,
            label=label,
            valid=False,
            status=DISCOVERY_STATUS_INVALID,
            has_package_init=candidate.has_package_init,
            has_provider_file=False,
            importable=False,
            config_exists=candidate.has_config_file,
            config_loaded=False,
            config_valid=False,
            definition_created=False,
            supports_chunk_generation=False,
            callable_functions={},
            missing_required_functions=REQUIRED_PROVIDER_FUNCTIONS,
            errors=tuple(errors),
            warnings=tuple(warnings),
            provider_info={},
            world_metadata={},
            raw_config={},
            metadata={
                "discoveryVersion": WORLD_DISCOVERY_VERSION,
            },
        )

    module: ModuleType | None = None

    try:
        module = _import_module_safe(candidate.provider_module)
        importable = True
    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="World provider import failed during discovery.",
            fallback_code="world_provider_discovery_import_failed",
            fallback_status_code=500,
            details={
                "folderName": candidate.folder_name,
                "providerModule": candidate.provider_module,
                "providerPath": candidate.provider_path,
            },
        )

        _append_issue(
            errors,
            code=world_error.code,
            message=world_error.message,
            details=world_error.details,
        )

        return DiscoveredWorldProvider(
            folder_name=candidate.folder_name,
            provider_module=candidate.provider_module,
            package_path=candidate.package_path,
            provider_path=candidate.provider_path,
            config_path=config_path,
            provider_id=provider_id or candidate.folder_name,
            world_id=world_id,
            world_type=world_type,
            label=label,
            valid=False,
            status=DISCOVERY_STATUS_INVALID,
            has_package_init=candidate.has_package_init,
            has_provider_file=True,
            importable=False,
            config_exists=candidate.has_config_file,
            config_loaded=False,
            config_valid=False,
            definition_created=False,
            supports_chunk_generation=False,
            callable_functions={},
            missing_required_functions=REQUIRED_PROVIDER_FUNCTIONS,
            errors=tuple(errors),
            warnings=tuple(warnings),
            provider_info={},
            world_metadata={},
            raw_config={},
            metadata={
                "discoveryVersion": WORLD_DISCOVERY_VERSION,
            },
        )

    callable_functions = _inspect_provider_functions(module)
    missing_required_functions = _missing_required_functions(callable_functions)

    for function_name in missing_required_functions:
        _append_issue(
            errors,
            code="missing_required_provider_function",
            message=f"Provider is missing required function '{function_name}'.",
            providerModule=candidate.provider_module,
            function=function_name,
        )

    provider_id = _safe_str(getattr(module, "PROVIDER_ID", None), default=candidate.folder_name)
    world_id = _safe_str(getattr(module, "WORLD_ID", None), default=provider_id)
    world_type = _safe_str(getattr(module, "WORLD_TYPE", None), default=provider_id)
    label = _safe_str(getattr(module, "PROVIDER_LABEL", None), default=provider_id)

    get_provider_info = _get_callable(module, "get_provider_info")

    if get_provider_info is not None:
        try:
            info_result = _call_provider_function(
                get_provider_info,
                function_name="get_provider_info",
                provider_module=candidate.provider_module,
            )
            provider_info = _provider_info_to_dict(info_result)

            provider_id = _safe_str(
                provider_info.get("providerId") or provider_info.get("provider_id"),
                default=provider_id,
            )
            world_type = _safe_str(
                provider_info.get("worldType") or provider_info.get("world_type"),
                default=world_type,
            )
            label = _safe_str(
                provider_info.get("label"),
                default=label,
            )

            config_path_from_info = _safe_str(
                provider_info.get("configPath") or provider_info.get("config_path")
            )

            if config_path_from_info:
                config_path = config_path_from_info

            supports_chunk_generation = _safe_bool(
                provider_info.get("supportsChunkGeneration")
                or provider_info.get("supports_chunk_generation"),
                default=callable_functions.get("generate_chunk", False),
            )

        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Provider get_provider_info failed during discovery.",
                fallback_code="provider_info_discovery_failed",
                fallback_status_code=500,
                details={
                    "providerModule": candidate.provider_module,
                },
            )
            _append_issue(
                errors,
                code=world_error.code,
                message=world_error.message,
                details=world_error.details,
            )

    get_default_config_path = _get_callable(module, "get_default_config_path")

    if get_default_config_path is not None:
        try:
            path_result = _call_provider_function(
                get_default_config_path,
                function_name="get_default_config_path",
                provider_module=candidate.provider_module,
            )
            path = _path_from_value(path_result)

            if path is not None:
                config_path = str(path)
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Provider get_default_config_path failed during discovery.",
                fallback_code="provider_config_path_discovery_failed",
                fallback_status_code=500,
                details={
                    "providerModule": candidate.provider_module,
                },
            )
            _append_issue(
                errors,
                code=world_error.code,
                message=world_error.message,
                details=world_error.details,
            )

    config_path_obj = _path_from_value(config_path)
    config_exists = bool(config_path_obj and config_path_obj.is_file())

    if not config_exists:
        _append_issue(
            errors,
            code="missing_world_config",
            message="World provider config file does not exist.",
            providerModule=candidate.provider_module,
            configPath=config_path,
        )

    if validate_config and config_exists:
        try:
            load_world_config = _get_callable(module, "load_world_config")

            if load_world_config is not None:
                raw_result = _call_provider_function_flexible(
                    load_world_config,
                    function_name="load_world_config",
                    provider_module=candidate.provider_module,
                    call_patterns=(
                        ("config_path", (config_path_obj,)),
                        ("no_args", ()),
                    ),
                )
                raw_config = _normalize_raw_config(raw_result)
            elif config_path_obj is not None:
                raw_config = _read_json_file(config_path_obj)
            else:
                raw_config = {}

            config_loaded = True

        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="World config could not be loaded during discovery.",
                fallback_code="world_config_discovery_load_failed",
                fallback_status_code=500,
                details={
                    "providerModule": candidate.provider_module,
                    "configPath": config_path,
                },
            )
            _append_issue(
                errors,
                code=world_error.code,
                message=world_error.message,
                details=world_error.details,
            )

        if config_loaded:
            try:
                validate_world_config = _get_callable(module, "validate_world_config")

                if validate_world_config is not None:
                    validated_result = _call_provider_function(
                        validate_world_config,
                        raw_config,
                        function_name="validate_world_config",
                        provider_module=candidate.provider_module,
                    )

                    if validated_result is None:
                        config_valid = True
                    elif isinstance(validated_result, WorldDefinition):
                        raw_config = validated_result.raw_config or validated_result.to_dict(camel_case=True)
                        config_valid = True
                    elif isinstance(validated_result, Mapping):
                        raw_config = dict(validated_result)
                        config_valid = True
                    else:
                        raise WorldProviderContractError(
                            "validate_world_config must return None, Mapping or WorldDefinition.",
                            details={
                                "providerModule": candidate.provider_module,
                                "returnType": type(validated_result).__name__,
                                "returnValue": make_json_safe(validated_result),
                            },
                        )
                else:
                    config_valid = True

            except Exception as exc:
                world_error = coerce_world_error(
                    exc,
                    fallback_message="World config validation failed during discovery.",
                    fallback_code="world_config_discovery_validation_failed",
                    fallback_status_code=400,
                    details={
                        "providerModule": candidate.provider_module,
                        "configPath": config_path,
                    },
                )
                _append_issue(
                    errors,
                    code=world_error.code,
                    message=world_error.message,
                    details=world_error.details,
                )

        if config_valid:
            try:
                create_world_definition = _get_callable(module, "create_world_definition")

                if create_world_definition is not None:
                    definition_result = _call_provider_function(
                        create_world_definition,
                        raw_config,
                        function_name="create_world_definition",
                        provider_module=candidate.provider_module,
                    )
                    definition = _normalize_world_definition(
                        definition_result,
                        raw_config=raw_config,
                    )
                else:
                    definition = WorldDefinition.from_dict(raw_config)

                definition.validate()
                definition_created = True

                world_id = definition.world_id
                world_type = definition.world_type
                label = definition.label
                provider_id = provider_id or definition.world_id

                world_metadata = definition.to_dict(
                    camel_case=True,
                    include_palette=True,
                    include_raw_config=False,
                )

            except Exception as exc:
                world_error = coerce_world_error(
                    exc,
                    fallback_message="WorldDefinition could not be created during discovery.",
                    fallback_code="world_definition_discovery_failed",
                    fallback_status_code=400,
                    details={
                        "providerModule": candidate.provider_module,
                        "configPath": config_path,
                    },
                )
                _append_issue(
                    errors,
                    code=world_error.code,
                    message=world_error.message,
                    details=world_error.details,
                )

    supports_chunk_generation = bool(
        supports_chunk_generation
        or callable_functions.get("generate_chunk", False)
    )

    if not supports_chunk_generation:
        _append_issue(
            errors,
            code="chunk_generation_not_supported",
            message="Provider does not support chunk generation.",
            providerModule=candidate.provider_module,
        )

    if provider_id and provider_id != candidate.folder_name:
        _append_issue(
            warnings,
            code="provider_id_differs_from_folder",
            message="Provider ID differs from folder name.",
            folderName=candidate.folder_name,
            providerId=provider_id,
        )

    valid = (
        importable
        and candidate.has_provider_file
        and config_exists
        and config_loaded
        and config_valid
        and definition_created
        and supports_chunk_generation
        and len(missing_required_functions) == 0
        and len(errors) == 0
    )

    return DiscoveredWorldProvider(
        folder_name=candidate.folder_name,
        provider_module=candidate.provider_module,
        package_path=candidate.package_path,
        provider_path=candidate.provider_path,
        config_path=config_path,
        provider_id=provider_id,
        world_id=world_id,
        world_type=world_type,
        label=label,
        valid=valid,
        status=DISCOVERY_STATUS_VALID if valid else DISCOVERY_STATUS_INVALID,
        has_package_init=candidate.has_package_init,
        has_provider_file=candidate.has_provider_file,
        importable=importable,
        config_exists=config_exists,
        config_loaded=config_loaded,
        config_valid=config_valid,
        definition_created=definition_created,
        supports_chunk_generation=supports_chunk_generation,
        callable_functions=dict(callable_functions),
        missing_required_functions=missing_required_functions,
        errors=tuple(errors),
        warnings=tuple(warnings),
        provider_info=provider_info,
        world_metadata=world_metadata,
        raw_config=raw_config,
        metadata={
            "discoveryVersion": WORLD_DISCOVERY_VERSION,
            "moduleExists": _module_exists(candidate.provider_module),
        },
    )


# ---------------------------------------------------------------------------
# Full discovery
# ---------------------------------------------------------------------------

def _discover_worlds_uncached(
    *,
    base_dir: str | Path | None = None,
    package_name: str = DEFAULT_WORLD_PACKAGE_NAME,
    include_invalid: bool = True,
    validate_config: bool = True,
) -> WorldDiscoveryResult:
    """
    Führt einen vollständigen Discovery-Scan ohne Cache aus.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    scan_dir = Path(base_dir) if base_dir is not None else get_world_package_dir()

    try:
        candidates = scan_world_provider_packages(
            base_dir=scan_dir,
            package_name=package_name,
            include_missing_provider=include_invalid,
        )
    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="World provider package scan failed.",
            fallback_code="world_provider_scan_failed",
            fallback_status_code=500,
            details={
                "baseDir": str(scan_dir),
                "packageName": package_name,
            },
        )

        _append_issue(
            errors,
            code=world_error.code,
            message=world_error.message,
            details=world_error.details,
        )

        return WorldDiscoveryResult(
            package_name=package_name,
            base_dir=str(scan_dir),
            providers=tuple(),
            candidates=tuple(),
            errors=tuple(errors),
            warnings=tuple(warnings),
            metadata={
                "discoveryVersion": WORLD_DISCOVERY_VERSION,
                "validateConfig": validate_config,
            },
        )

    providers: list[DiscoveredWorldProvider] = []

    for candidate in candidates:
        try:
            discovered = discover_world_provider(
                candidate,
                validate_config=validate_config,
            )

            if include_invalid or discovered.valid:
                providers.append(discovered)

        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="World provider discovery failed.",
                fallback_code="world_provider_discovery_failed",
                fallback_status_code=500,
                details={
                    "folderName": candidate.folder_name,
                    "providerModule": candidate.provider_module,
                },
            )

            failed_provider = DiscoveredWorldProvider(
                folder_name=candidate.folder_name,
                provider_module=candidate.provider_module,
                package_path=candidate.package_path,
                provider_path=candidate.provider_path,
                config_path=candidate.config_path,
                provider_id=candidate.folder_name,
                world_id=None,
                world_type=None,
                label=None,
                valid=False,
                status=DISCOVERY_STATUS_INVALID,
                has_package_init=candidate.has_package_init,
                has_provider_file=candidate.has_provider_file,
                importable=False,
                config_exists=candidate.has_config_file,
                config_loaded=False,
                config_valid=False,
                definition_created=False,
                supports_chunk_generation=False,
                callable_functions={},
                missing_required_functions=REQUIRED_PROVIDER_FUNCTIONS,
                errors=(
                    {
                        "code": world_error.code,
                        "message": world_error.message,
                        "details": world_error.details,
                    },
                ),
                warnings=tuple(),
                provider_info={},
                world_metadata={},
                raw_config={},
                metadata={
                    "discoveryVersion": WORLD_DISCOVERY_VERSION,
                    "failedDuringDiscovery": True,
                },
            )

            if include_invalid:
                providers.append(failed_provider)

    if not providers:
        _append_issue(
            warnings,
            code="no_world_providers_discovered",
            message="No world providers were discovered.",
            baseDir=str(scan_dir),
            packageName=package_name,
        )

    result = WorldDiscoveryResult(
        package_name=package_name,
        base_dir=str(scan_dir),
        providers=tuple(providers),
        candidates=candidates,
        errors=tuple(errors),
        warnings=tuple(warnings),
        metadata={
            "discoveryVersion": WORLD_DISCOVERY_VERSION,
            "validateConfig": validate_config,
            "includeInvalid": include_invalid,
            "requiredProviderFunctions": REQUIRED_PROVIDER_FUNCTIONS,
        },
    )

    return result


@lru_cache(maxsize=32)
def _discover_worlds_cached(
    base_dir: str,
    package_name: str,
    include_invalid: bool,
    validate_config: bool,
) -> WorldDiscoveryResult:
    """
    Cache-Schicht für Discovery.

    Der Cache ist nützlich, weil Discovery Provider importiert und Configs
    validiert. Bei Entwicklung neuer World-Ordner kann der Cache mit
    reset_world_discovery_cache() geleert werden.
    """
    return _discover_worlds_uncached(
        base_dir=base_dir,
        package_name=package_name,
        include_invalid=include_invalid,
        validate_config=validate_config,
    )


def discover_worlds(
    *,
    base_dir: str | Path | None = None,
    package_name: str = DEFAULT_WORLD_PACKAGE_NAME,
    include_invalid: bool = True,
    validate_config: bool = True,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> WorldDiscoveryResult:
    """
    Öffentliche Discovery-Funktion.

    Parameter:
        include_invalid:
            Wenn True, erscheinen auch ungültige Ordner/Provider in der
            Diagnose. Für /world-test ist das sinnvoll.

        validate_config:
            Wenn True, wird world.json geladen, validiert und eine
            WorldDefinition erzeugt.

        use_cache:
            Wenn True, wird der Discovery-Cache verwendet.

        force_refresh:
            Wenn True, wird der Discovery-Cache vorher geleert.
    """
    scan_dir = Path(base_dir) if base_dir is not None else get_world_package_dir()

    try:
        scan_dir_text = str(scan_dir.resolve())
    except Exception:
        scan_dir_text = str(scan_dir)

    if force_refresh:
        reset_world_discovery_cache()

    if not use_cache:
        return _discover_worlds_uncached(
            base_dir=scan_dir_text,
            package_name=package_name,
            include_invalid=include_invalid,
            validate_config=validate_config,
        )

    return _discover_worlds_cached(
        scan_dir_text,
        package_name,
        bool(include_invalid),
        bool(validate_config),
    )


def reset_world_discovery_cache() -> None:
    """
    Leert den Discovery-Cache.
    """
    _discover_worlds_cached.cache_clear()


# ---------------------------------------------------------------------------
# Registry creation
# ---------------------------------------------------------------------------

def create_registry_from_discovery_result(
    result: WorldDiscoveryResult,
    *,
    default_world_id: str | None = None,
    include_invalid: bool = False,
    strict: bool = False,
) -> WorldRegistry:
    """
    Baut eine temporäre WorldRegistry aus einem Discovery-Ergebnis.

    Diese Registry ist für Test-/Debug-Routen geeignet, weil sie neu
    hinzugefügte World-Ordner automatisch kennt.
    """
    if not isinstance(result, WorldDiscoveryResult):
        raise WorldRegistryError(
            "create_registry_from_discovery_result requires WorldDiscoveryResult.",
            details={
                "resultType": type(result).__name__,
                "result": make_json_safe(result),
            },
        )

    selected_providers = (
        result.providers
        if include_invalid
        else result.valid_providers
    )

    resolved_default_world_id = (
        _safe_str(default_world_id)
        or result.default_world_id
        or "flat"
    )

    registry = WorldRegistry(
        default_world_id=resolved_default_world_id,
        strict=strict,
    )

    registration_errors: list[dict[str, Any]] = []

    for provider in selected_providers:
        if not provider.valid and not include_invalid:
            continue

        if not provider.valid:
            continue

        try:
            registry.register(
                provider.to_registry_definition(),
                verify_importable=False,
                replace_existing=True,
            )
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Could not register discovered world provider.",
                fallback_code="discovered_world_registration_failed",
                fallback_status_code=500,
                details={
                    "provider": provider.to_dict(include_raw_config=False),
                },
            )
            registration_errors.append(
                {
                    "code": world_error.code,
                    "message": world_error.message,
                    "details": world_error.details,
                }
            )

            if strict:
                raise world_error from exc

    if registration_errors and strict:
        raise WorldRegistryError(
            "One or more discovered world providers could not be registered.",
            details={
                "errors": registration_errors,
            },
        )

    if registry.provider_ids(include_disabled=True):
        if not registry.has(resolved_default_world_id, include_disabled=True):
            first_provider_id = registry.provider_ids(include_disabled=True)[0]
            registry.set_default_world_id(
                first_provider_id,
                require_registered=True,
            )

    return registry


def create_registry_from_discovered_worlds(
    *,
    base_dir: str | Path | None = None,
    package_name: str = DEFAULT_WORLD_PACKAGE_NAME,
    default_world_id: str | None = None,
    include_invalid: bool = False,
    validate_config: bool = True,
    use_cache: bool = True,
    force_refresh: bool = False,
    strict: bool = False,
) -> WorldRegistry:
    """
    Scannt Weltmodelle und baut daraus direkt eine temporäre Registry.
    """
    result = discover_worlds(
        base_dir=base_dir,
        package_name=package_name,
        include_invalid=True,
        validate_config=validate_config,
        use_cache=use_cache,
        force_refresh=force_refresh,
    )

    return create_registry_from_discovery_result(
        result,
        default_world_id=default_world_id,
        include_invalid=include_invalid,
        strict=strict,
    )


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def get_discovered_world(
    world_id_or_provider_id: str,
    *,
    base_dir: str | Path | None = None,
    package_name: str = DEFAULT_WORLD_PACKAGE_NAME,
    validate_config: bool = True,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> DiscoveredWorldProvider:
    """
    Findet ein Discovery-Ergebnis über worldId/providerId/folderName.
    """
    result = discover_worlds(
        base_dir=base_dir,
        package_name=package_name,
        include_invalid=True,
        validate_config=validate_config,
        use_cache=use_cache,
        force_refresh=force_refresh,
    )

    return result.require_provider(world_id_or_provider_id)


def discover_worlds_as_dict(
    *,
    base_dir: str | Path | None = None,
    package_name: str = DEFAULT_WORLD_PACKAGE_NAME,
    include_invalid: bool = True,
    validate_config: bool = True,
    include_raw_config: bool = False,
    include_candidates: bool = True,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Gibt Discovery direkt als JSON-nahes Dictionary zurück.
    """
    result = discover_worlds(
        base_dir=base_dir,
        package_name=package_name,
        include_invalid=include_invalid,
        validate_config=validate_config,
        use_cache=use_cache,
        force_refresh=force_refresh,
    )

    return result.to_dict(
        include_raw_config=include_raw_config,
        include_invalid=include_invalid,
        include_candidates=include_candidates,
    )


def get_valid_discovered_world_ids(
    *,
    base_dir: str | Path | None = None,
    package_name: str = DEFAULT_WORLD_PACKAGE_NAME,
    validate_config: bool = True,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> tuple[str, ...]:
    """
    Gibt gültige entdeckte providerIds zurück.
    """
    result = discover_worlds(
        base_dir=base_dir,
        package_name=package_name,
        include_invalid=False,
        validate_config=validate_config,
        use_cache=use_cache,
        force_refresh=force_refresh,
    )

    return tuple(
        provider.registry_provider_id
        for provider in result.valid_providers
    )


def require_discovered_worlds_ready(
    *,
    minimum_valid_worlds: int = 1,
    base_dir: str | Path | None = None,
    package_name: str = DEFAULT_WORLD_PACKAGE_NAME,
    validate_config: bool = True,
    force_refresh: bool = False,
) -> WorldDiscoveryResult:
    """
    Erzwingt, dass mindestens eine gültige Welt gefunden wird.

    Diese Funktion ist für Startup-Checks oder Tests geeignet.
    """
    result = discover_worlds(
        base_dir=base_dir,
        package_name=package_name,
        include_invalid=True,
        validate_config=validate_config,
        use_cache=True,
        force_refresh=force_refresh,
    )

    if result.valid_count < minimum_valid_worlds:
        raise WorldRegistryError(
            "Not enough valid discovered world providers.",
            details={
                "minimumValidWorlds": minimum_valid_worlds,
                "validCount": result.valid_count,
                "invalidCount": result.invalid_count,
                "result": result.to_dict(
                    include_raw_config=False,
                    include_invalid=True,
                    include_candidates=True,
                ),
            },
        )

    return result


__all__ = (
    "WORLD_DISCOVERY_VERSION",
    "DEFAULT_WORLD_PACKAGE_NAME",
    "PROVIDER_FILENAME",
    "DEFAULT_CONFIG_FILENAME",
    "REQUIRED_PROVIDER_FUNCTIONS",
    "OPTIONAL_PROVIDER_FUNCTIONS",
    "ALL_KNOWN_PROVIDER_FUNCTIONS",
    "DISCOVERY_STATUS_VALID",
    "DISCOVERY_STATUS_INVALID",
    "WorldProviderCandidate",
    "DiscoveredWorldProvider",
    "WorldDiscoveryResult",
    "get_world_package_dir",
    "scan_world_provider_packages",
    "discover_world_provider",
    "discover_worlds",
    "reset_world_discovery_cache",
    "create_registry_from_discovery_result",
    "create_registry_from_discovered_worlds",
    "get_discovered_world",
    "discover_worlds_as_dict",
    "get_valid_discovered_world_ids",
    "require_discovered_worlds_ready",
)