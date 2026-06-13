# src/world/loader.py
"""
VECTOPLAN World Loader.

Diese Datei enthält die neutrale Ladeschicht für World-Provider.

Aufgabe des Loaders:
- World-Provider über die Registry auflösen
- Provider-Module robust importieren
- world.json einer konkreten Welt laden
- rohe JSON-Konfiguration defensiv parsen
- Provider-spezifische Validierung ausführen
- rohe Config in eine WorldDefinition normalisieren
- geladene Welten optional cachen
- keine Chunks selbst generieren
- keine konkrete Flat-World-Logik enthalten
- keine Flask-Abhängigkeit erzeugen
- keine Datenbank verwenden

Architektur:

    WorldService
        → WorldLoader
            → WorldRegistry
                → src.world.flat.provider
                    → src/world/flat/world.json
                    → validator.py
                    → generator.py

Der Loader kennt also nur den Provider-Vertrag, aber nicht die konkrete
Weltlogik.

Erwarteter Provider-Vertrag für spätere Provider wie src.world.flat.provider:

    optional:
        PROVIDER_ID: str
        WORLD_TYPE: str
        CONFIG_FILENAME: str

    empfohlen:
        get_provider_info() -> WorldProviderInfo
        get_default_config_path() -> str | Path
        load_world_config(config_path: str | Path | None = None) -> Mapping[str, Any]
        validate_world_config(raw_config: Mapping[str, Any]) -> None | Mapping[str, Any]
        create_world_definition(raw_config: Mapping[str, Any]) -> WorldDefinition

    für spätere Generierung:
        generate_chunk(world: WorldDefinition, request: ChunkRequest) -> GeneratedChunk

Der Loader ist bewusst tolerant:
- Wenn ein Provider load_world_config anbietet, wird diese Funktion genutzt.
- Sonst lädt der Loader selbst die Config-Datei.
- Wenn ein Provider create_world_definition anbietet, wird diese Funktion genutzt.
- Sonst wird WorldDefinition.from_dict(raw_config) verwendet.
- Wenn ein Provider validate_world_config anbietet, wird sie vorher ausgeführt.

Dadurch können einfache Welten klein bleiben und komplexere Welten später
mehr Kontrolle übernehmen.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from threading import RLock
from types import ModuleType
from typing import Any, Final

try:
    from src.world.errors import (
        InvalidWorldConfigFileError,
        InvalidWorldDefinitionError,
        WorldConfigError,
        WorldLoaderError,
        WorldNotFoundError,
        WorldProviderContractError,
        WorldProviderImportError,
        WorldValidationError,
        coerce_world_error,
        make_json_safe,
    )
    from src.world.models import WorldDefinition, WorldProviderInfo
    from src.world.registry import (
        WorldProviderRegistration,
        WorldRegistry,
        get_default_world_registry,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.loader requires src.world.errors, src.world.models and "
        "src.world.registry to be importable before the loader can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORLD_LOADER_VERSION: Final[str] = "0.1.0"

DEFAULT_CONFIG_FILENAME: Final[str] = "world.json"

PROVIDER_FUNCTION_GET_INFO: Final[str] = "get_provider_info"
PROVIDER_FUNCTION_GET_CONFIG_PATH: Final[str] = "get_default_config_path"
PROVIDER_FUNCTION_LOAD_CONFIG: Final[str] = "load_world_config"
PROVIDER_FUNCTION_VALIDATE_CONFIG: Final[str] = "validate_world_config"
PROVIDER_FUNCTION_CREATE_DEFINITION: Final[str] = "create_world_definition"
PROVIDER_FUNCTION_GENERATE_CHUNK: Final[str] = "generate_chunk"

PROVIDER_REQUIRED_FOR_METADATA: Final[tuple[str, ...]] = ()
PROVIDER_RECOMMENDED_FUNCTIONS: Final[tuple[str, ...]] = (
    PROVIDER_FUNCTION_GET_INFO,
    PROVIDER_FUNCTION_GET_CONFIG_PATH,
    PROVIDER_FUNCTION_LOAD_CONFIG,
    PROVIDER_FUNCTION_VALIDATE_CONFIG,
    PROVIDER_FUNCTION_CREATE_DEFINITION,
)

MAX_CONFIG_FILE_BYTES: Final[int] = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt einen beliebigen Wert defensiv in einen bereinigten String um.
    """
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


def _safe_module_name(module: ModuleType | Any) -> str:
    """
    Gibt einen stabilen Modulnamen zurück.
    """
    return _safe_str(getattr(module, "__name__", None), default="<unknown-module>")


def _is_callable_attribute(module: ModuleType, attribute_name: str) -> bool:
    """
    Prüft, ob ein Modul ein aufrufbares Attribut besitzt.
    """
    try:
        value = getattr(module, attribute_name, None)
    except Exception:
        return False

    return callable(value)


def _get_callable_attribute(
    module: ModuleType,
    attribute_name: str,
    *,
    required: bool = False,
) -> Callable[..., Any] | None:
    """
    Holt defensiv eine Callable-Funktion aus einem Provider-Modul.
    """
    try:
        value = getattr(module, attribute_name, None)
    except Exception as exc:
        if required:
            raise WorldProviderContractError(
                f"Could not access provider function '{attribute_name}'.",
                details={
                    "module": _safe_module_name(module),
                    "function": attribute_name,
                },
                cause=exc,
            ) from exc

        return None

    if value is None:
        if required:
            raise WorldProviderContractError(
                f"Provider function '{attribute_name}' is missing.",
                details={
                    "module": _safe_module_name(module),
                    "function": attribute_name,
                },
            )

        return None

    if not callable(value):
        raise WorldProviderContractError(
            f"Provider attribute '{attribute_name}' must be callable.",
            details={
                "module": _safe_module_name(module),
                "function": attribute_name,
                "valueType": type(value).__name__,
            },
        )

    return value


def _normalize_config_path(value: str | Path | None) -> Path | None:
    """
    Normalisiert einen Config-Pfad.
    """
    if value is None:
        return None

    try:
        path = Path(value)
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "World config path is invalid.",
            details={"configPath": make_json_safe(value)},
            cause=exc,
        ) from exc

    return path


def _path_from_registration(registration: WorldProviderRegistration) -> Path | None:
    """
    Ermittelt den Config-Pfad aus einer Registry-Registration.

    Die Registry speichert config_path aktuell als stringnahen Wert.
    Dieser kann relativ zum Service-Root oder Arbeitsverzeichnis sein.
    Für Phase 1 wird er direkt als Path interpretiert.

    Provider können später get_default_config_path() anbieten, um diese
    Auflösung selbst genauer zu steuern.
    """
    return _normalize_config_path(registration.config_path)


def _read_json_file(path: Path) -> dict[str, Any]:
    """
    Liest eine JSON-Datei robust und gibt ein Dictionary zurück.
    """
    try:
        resolved_path = path.expanduser()
    except Exception:
        resolved_path = path

    if not resolved_path.exists():
        raise InvalidWorldConfigFileError(
            "World config file does not exist.",
            details={"configPath": str(resolved_path)},
        )

    if not resolved_path.is_file():
        raise InvalidWorldConfigFileError(
            "World config path is not a file.",
            details={"configPath": str(resolved_path)},
        )

    try:
        size = resolved_path.stat().st_size
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not inspect world config file.",
            details={"configPath": str(resolved_path)},
            cause=exc,
        ) from exc

    if size > MAX_CONFIG_FILE_BYTES:
        raise InvalidWorldConfigFileError(
            "World config file is too large.",
            details={
                "configPath": str(resolved_path),
                "sizeBytes": size,
                "maxBytes": MAX_CONFIG_FILE_BYTES,
            },
        )

    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidWorldConfigFileError(
            "World config file must be UTF-8 encoded.",
            details={"configPath": str(resolved_path)},
            cause=exc,
        ) from exc
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not read world config file.",
            details={"configPath": str(resolved_path)},
            cause=exc,
        ) from exc

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidWorldConfigFileError(
            "World config file contains invalid JSON.",
            details={
                "configPath": str(resolved_path),
                "line": exc.lineno,
                "column": exc.colno,
                "message": exc.msg,
            },
            cause=exc,
        ) from exc
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "World config file could not be parsed.",
            details={"configPath": str(resolved_path)},
            cause=exc,
        ) from exc

    if not isinstance(parsed, Mapping):
        raise InvalidWorldConfigFileError(
            "World config JSON root must be an object.",
            details={
                "configPath": str(resolved_path),
                "rootType": type(parsed).__name__,
            },
        )

    return dict(parsed)


def _normalize_raw_config(raw_config: Any, *, provider_id: str) -> dict[str, Any]:
    """
    Normalisiert Provider-Config auf ein Dictionary.
    """
    if isinstance(raw_config, WorldDefinition):
        return raw_config.raw_config or raw_config.to_dict(camel_case=True)

    if not isinstance(raw_config, Mapping):
        raise WorldConfigError(
            "World provider returned invalid config object.",
            details={
                "providerId": provider_id,
                "configType": type(raw_config).__name__,
                "config": make_json_safe(raw_config),
            },
        )

    return dict(raw_config)


def _normalize_world_definition(
    value: Any,
    *,
    provider_id: str,
    raw_config: Mapping[str, Any] | None = None,
) -> WorldDefinition:
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

    raise InvalidWorldDefinitionError(
        "World provider returned invalid world definition.",
        details={
            "providerId": provider_id,
            "definitionType": type(value).__name__,
            "definition": make_json_safe(value),
        },
    )


def _call_provider_function(
    function: Callable[..., Any],
    *args: Any,
    provider_id: str,
    function_name: str,
    **kwargs: Any,
) -> Any:
    """
    Ruft eine Provider-Funktion robust auf und übersetzt Fehler in
    WorldProviderContractError/WorldValidationError-nahe Fehler.

    Bereits vorhandene World-Fehler bleiben über coerce_world_error erhalten.
    """
    try:
        return function(*args, **kwargs)
    except TypeError as exc:
        raise WorldProviderContractError(
            f"Provider function '{function_name}' could not be called with expected arguments.",
            details={
                "providerId": provider_id,
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
                "providerId": provider_id,
                "function": function_name,
            },
        )
        raise world_error from exc


# ---------------------------------------------------------------------------
# Loaded world structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LoadedWorld:
    """
    Ergebnis einer erfolgreichen World-Ladung.

    Diese Struktur verbindet:
    - Registry-Registration
    - importiertes Provider-Modul
    - rohe Config
    - normalisierte WorldDefinition
    """

    registration: WorldProviderRegistration
    provider_module: ModuleType
    raw_config: dict[str, Any]
    definition: WorldDefinition
    config_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def world_id(self) -> str:
        return self.definition.world_id

    @property
    def provider_id(self) -> str:
        return self.registration.provider_id

    @property
    def world_type(self) -> str:
        return self.definition.world_type

    @property
    def provider_module_name(self) -> str:
        return _safe_module_name(self.provider_module)

    def get_provider_function(
        self,
        name: str,
        *,
        required: bool = False,
    ) -> Callable[..., Any] | None:
        """
        Holt eine Callable-Funktion aus dem geladenen Provider-Modul.
        """
        return _get_callable_attribute(
            self.provider_module,
            name,
            required=required,
        )

    def has_provider_function(self, name: str) -> bool:
        """
        Prüft, ob der geladene Provider eine Callable-Funktion besitzt.
        """
        return _is_callable_attribute(self.provider_module, name)

    def to_dict(
        self,
        *,
        include_raw_config: bool = False,
        include_provider_details: bool = True,
    ) -> dict[str, Any]:
        """
        Serialisiert den Ladezustand für Diagnose/Tests.
        """
        data: dict[str, Any] = {
            "worldId": self.world_id,
            "worldType": self.world_type,
            "providerId": self.provider_id,
            "providerModule": self.provider_module_name,
            "configPath": self.config_path,
            "definition": self.definition.to_metadata_dict(camel_case=True),
            "metadata": self.metadata,
        }

        if include_provider_details:
            data["provider"] = self.registration.to_dict(camel_case=True)

        if include_raw_config:
            data["rawConfig"] = make_json_safe(self.raw_config)

        return data


@dataclass(frozen=True, slots=True)
class WorldLoaderStatus:
    """
    Diagnosezustand eines WorldLoaders.
    """

    loader_version: str
    cache_enabled: bool
    cached_world_ids: tuple[str, ...]
    registry: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert den Loaderstatus.
        """
        return {
            "loaderVersion": self.loader_version,
            "cacheEnabled": self.cache_enabled,
            "cachedWorldIds": list(self.cached_world_ids),
            "registry": self.registry,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# WorldLoader
# ---------------------------------------------------------------------------

class WorldLoader:
    """
    Neutrale Ladeschicht für VECTOPLAN-Welten.

    Der Loader lädt und normalisiert konkrete Welten.
    Er generiert keine Chunks selbst. Chunk-Generierung wird später über
    den geladenen Provider und WorldService ausgeführt.
    """

    def __init__(
        self,
        *,
        registry: WorldRegistry | None = None,
        cache_enabled: bool = True,
        verify_provider_contract: bool = True,
    ) -> None:
        self._registry = registry or get_default_world_registry()
        self._cache_enabled = bool(cache_enabled)
        self._verify_provider_contract = bool(verify_provider_contract)
        self._cache: dict[str, LoadedWorld] = {}
        self._lock = RLock()

    @property
    def registry(self) -> WorldRegistry:
        """
        Gibt die verwendete Registry zurück.
        """
        return self._registry

    @property
    def cache_enabled(self) -> bool:
        """
        Gibt zurück, ob geladene Welten gecacht werden.
        """
        return self._cache_enabled

    @property
    def verify_provider_contract(self) -> bool:
        """
        Gibt zurück, ob Provider beim Laden strukturell geprüft werden.
        """
        return self._verify_provider_contract

    def clear_cache(self) -> None:
        """
        Leert den internen LoadedWorld-Cache.
        """
        with self._lock:
            self._cache.clear()

    def cached_world_ids(self) -> tuple[str, ...]:
        """
        Gibt aktuell gecachte Welt-IDs zurück.
        """
        with self._lock:
            return tuple(sorted(self._cache.keys()))

    def get_status(self) -> WorldLoaderStatus:
        """
        Gibt einen Diagnosezustand zurück.
        """
        return WorldLoaderStatus(
            loader_version=WORLD_LOADER_VERSION,
            cache_enabled=self.cache_enabled,
            cached_world_ids=self.cached_world_ids(),
            registry=self.registry.to_dict(
                include_disabled=True,
                include_registrations=True,
            ),
            metadata={
                "verifyProviderContract": self.verify_provider_contract,
            },
        )

    def import_provider_module(
        self,
        registration: WorldProviderRegistration,
    ) -> ModuleType:
        """
        Importiert ein Provider-Modul robust.

        Importfehler werden in WorldProviderImportError übersetzt.
        """
        module_path = registration.provider_module

        if not module_path:
            raise WorldProviderImportError(
                "World provider module path is empty.",
                details={"providerId": registration.provider_id},
            )

        try:
            module = import_module(module_path)
        except ModuleNotFoundError as exc:
            raise WorldProviderImportError(
                "World provider module could not be found.",
                details={
                    "providerId": registration.provider_id,
                    "providerModule": module_path,
                },
                cause=exc,
            ) from exc
        except Exception as exc:
            raise WorldProviderImportError(
                "World provider module could not be imported.",
                details={
                    "providerId": registration.provider_id,
                    "providerModule": module_path,
                },
                cause=exc,
            ) from exc

        if not isinstance(module, ModuleType):
            raise WorldProviderImportError(
                "Imported provider is not a module.",
                details={
                    "providerId": registration.provider_id,
                    "providerModule": module_path,
                    "importedType": type(module).__name__,
                },
            )

        return module

    def verify_provider_module_contract(
        self,
        module: ModuleType,
        registration: WorldProviderRegistration,
    ) -> dict[str, Any]:
        """
        Prüft den Provider-Vertrag defensiv.

        Aktuell gibt es keine harten Pflichtfunktionen, weil einfache Provider
        durch Loader-Fallbacks funktionieren können.

        Trotzdem wird geprüft:
        - vorhandene empfohlene Provider-Funktionen müssen callable sein
        - PROVIDER_ID, falls vorhanden, sollte zur Registry passen
        - WORLD_TYPE, falls vorhanden, sollte nicht leer sein
        """
        provider_id = registration.provider_id
        module_name = _safe_module_name(module)
        warnings: list[dict[str, Any]] = []
        functions: dict[str, bool] = {}

        for function_name in PROVIDER_RECOMMENDED_FUNCTIONS:
            try:
                value = getattr(module, function_name, None)
            except Exception as exc:
                raise WorldProviderContractError(
                    f"Could not inspect provider function '{function_name}'.",
                    details={
                        "providerId": provider_id,
                        "providerModule": module_name,
                        "function": function_name,
                    },
                    cause=exc,
                ) from exc

            exists = value is not None
            functions[function_name] = exists

            if exists and not callable(value):
                raise WorldProviderContractError(
                    f"Provider attribute '{function_name}' must be callable.",
                    details={
                        "providerId": provider_id,
                        "providerModule": module_name,
                        "function": function_name,
                        "valueType": type(value).__name__,
                    },
                )

        provider_id_constant = getattr(module, "PROVIDER_ID", None)

        if provider_id_constant is not None:
            provider_id_text = _safe_str(provider_id_constant)

            if provider_id_text and provider_id_text != provider_id:
                warnings.append(
                    {
                        "code": "provider_id_mismatch",
                        "registryProviderId": provider_id,
                        "moduleProviderId": provider_id_text,
                    }
                )

        world_type_constant = getattr(module, "WORLD_TYPE", None)

        if world_type_constant is not None and not _safe_str(world_type_constant):
            warnings.append(
                {
                    "code": "empty_world_type_constant",
                    "providerId": provider_id,
                }
            )

        if not functions.get(PROVIDER_FUNCTION_LOAD_CONFIG) and not registration.config_path:
            warnings.append(
                {
                    "code": "no_provider_config_loader_or_config_path",
                    "providerId": provider_id,
                    "message": (
                        "Provider has no load_world_config function and registry "
                        "registration has no configPath."
                    ),
                }
            )

        return {
            "providerId": provider_id,
            "providerModule": module_name,
            "functions": functions,
            "warnings": warnings,
        }

    def resolve_config_path(
        self,
        module: ModuleType,
        registration: WorldProviderRegistration,
    ) -> Path | None:
        """
        Ermittelt den Config-Pfad.

        Priorität:
        1. Provider-Funktion get_default_config_path()
        2. Registry-Registration config_path
        3. Provider-Konstante CONFIG_FILENAME relativ zum Provider-Modulordner
        4. DEFAULT_CONFIG_FILENAME relativ zum Provider-Modulordner
        """
        provider_id = registration.provider_id

        get_path = _get_callable_attribute(
            module,
            PROVIDER_FUNCTION_GET_CONFIG_PATH,
            required=False,
        )

        if get_path is not None:
            result = _call_provider_function(
                get_path,
                provider_id=provider_id,
                function_name=PROVIDER_FUNCTION_GET_CONFIG_PATH,
            )
            path = _normalize_config_path(result)

            if path is not None:
                return path

        registration_path = _path_from_registration(registration)

        if registration_path is not None and registration_path.exists():
            return registration_path

        module_file = getattr(module, "__file__", None)

        if module_file:
            try:
                module_dir = Path(module_file).resolve().parent
                config_filename = _safe_str(
                    getattr(module, "CONFIG_FILENAME", None),
                    default=DEFAULT_CONFIG_FILENAME,
                )
                provider_local_path = module_dir / config_filename

                if provider_local_path.exists():
                    return provider_local_path
            except Exception:
                pass

        if registration_path is not None:
            return registration_path

        return None

    def load_raw_config(
        self,
        module: ModuleType,
        registration: WorldProviderRegistration,
        *,
        config_path: str | Path | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """
        Lädt die rohe world.json-Konfiguration.

        Priorität:
        1. expliziter config_path
        2. Provider-Funktion load_world_config(config_path)
        3. Loader liest JSON-Datei selbst
        """
        provider_id = registration.provider_id

        explicit_path = _normalize_config_path(config_path)
        resolved_path = explicit_path or self.resolve_config_path(module, registration)

        load_config = _get_callable_attribute(
            module,
            PROVIDER_FUNCTION_LOAD_CONFIG,
            required=False,
        )

        if load_config is not None:
            try:
                raw = _call_provider_function(
                    load_config,
                    resolved_path,
                    provider_id=provider_id,
                    function_name=PROVIDER_FUNCTION_LOAD_CONFIG,
                )
            except WorldProviderContractError:
                raise
            except Exception as exc:
                raise coerce_world_error(
                    exc,
                    fallback_message="World provider failed to load config.",
                    fallback_code="world_provider_config_load_failed",
                    fallback_status_code=500,
                    details={
                        "providerId": provider_id,
                        "configPath": str(resolved_path) if resolved_path else None,
                    },
                ) from exc

            return (
                _normalize_raw_config(raw, provider_id=provider_id),
                str(resolved_path) if resolved_path else None,
            )

        if resolved_path is None:
            raise InvalidWorldConfigFileError(
                "World config path could not be resolved.",
                details={
                    "providerId": provider_id,
                    "providerModule": registration.provider_module,
                    "configPath": registration.config_path,
                },
            )

        raw_config = _read_json_file(resolved_path)

        return raw_config, str(resolved_path)

    def validate_raw_config(
        self,
        module: ModuleType,
        registration: WorldProviderRegistration,
        raw_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """
        Führt optionale Provider-spezifische Config-Validierung aus.

        Provider-Funktion:
            validate_world_config(raw_config)

        Erlaubte Rückgaben:
            None
                → raw_config wird unverändert weiterverwendet

            Mapping
                → Rückgabe ersetzt/normalisiert raw_config

            WorldDefinition
                → wird in raw_config-nahe Form zurückgeführt
        """
        provider_id = registration.provider_id

        validate_config = _get_callable_attribute(
            module,
            PROVIDER_FUNCTION_VALIDATE_CONFIG,
            required=False,
        )

        if validate_config is None:
            return dict(raw_config)

        result = _call_provider_function(
            validate_config,
            raw_config,
            provider_id=provider_id,
            function_name=PROVIDER_FUNCTION_VALIDATE_CONFIG,
        )

        if result is None:
            return dict(raw_config)

        if isinstance(result, WorldDefinition):
            return result.raw_config or result.to_dict(camel_case=True)

        if isinstance(result, Mapping):
            return dict(result)

        raise WorldProviderContractError(
            "Provider validate_world_config must return None, Mapping or WorldDefinition.",
            details={
                "providerId": provider_id,
                "returnType": type(result).__name__,
                "returnValue": make_json_safe(result),
            },
        )

    def create_world_definition(
        self,
        module: ModuleType,
        registration: WorldProviderRegistration,
        raw_config: Mapping[str, Any],
    ) -> WorldDefinition:
        """
        Erstellt eine WorldDefinition aus validierter Config.

        Priorität:
        1. Provider-Funktion create_world_definition(raw_config)
        2. WorldDefinition.from_dict(raw_config)
        """
        provider_id = registration.provider_id

        create_definition = _get_callable_attribute(
            module,
            PROVIDER_FUNCTION_CREATE_DEFINITION,
            required=False,
        )

        if create_definition is not None:
            result = _call_provider_function(
                create_definition,
                raw_config,
                provider_id=provider_id,
                function_name=PROVIDER_FUNCTION_CREATE_DEFINITION,
            )
            definition = _normalize_world_definition(
                result,
                provider_id=provider_id,
                raw_config=raw_config,
            )
        else:
            definition = WorldDefinition.from_dict(raw_config)

        self.validate_definition_against_registration(
            definition,
            registration,
        )

        return definition

    def validate_definition_against_registration(
        self,
        definition: WorldDefinition,
        registration: WorldProviderRegistration,
    ) -> None:
        """
        Prüft, ob geladene Weltdefinition und Registry-Eintrag zusammenpassen.
        """
        errors: list[dict[str, Any]] = []

        if definition.world_id != registration.provider_id:
            allowed_keys = registration.all_keys

            if definition.world_id not in allowed_keys:
                errors.append(
                    {
                        "code": "world_id_not_registered_key",
                        "definitionWorldId": definition.world_id,
                        "providerId": registration.provider_id,
                        "allowedKeys": allowed_keys,
                    }
                )

        if definition.world_type != registration.world_type:
            errors.append(
                {
                    "code": "world_type_mismatch",
                    "definitionWorldType": definition.world_type,
                    "registryWorldType": registration.world_type,
                    "providerId": registration.provider_id,
                }
            )

        if errors:
            raise InvalidWorldDefinitionError(
                "Loaded world definition does not match registry registration.",
                details={
                    "providerId": registration.provider_id,
                    "worldId": definition.world_id,
                    "errors": errors,
                },
            )

    def load_world(
        self,
        world_id: str | None = None,
        *,
        config_path: str | Path | None = None,
        force_reload: bool = False,
    ) -> LoadedWorld:
        """
        Lädt eine Welt über worldId oder Alias.

        Wenn world_id leer ist, wird die Default-Welt der Registry geladen.
        """
        try:
            registration = self.registry.resolve(world_id)
        except WorldNotFoundError:
            raise
        except Exception as exc:
            raise WorldLoaderError(
                "Could not resolve world provider.",
                details={"worldId": world_id},
                cause=exc,
            ) from exc

        cache_key = registration.provider_id

        with self._lock:
            if (
                self.cache_enabled
                and not force_reload
                and config_path is None
                and cache_key in self._cache
            ):
                return self._cache[cache_key]

        module = self.import_provider_module(registration)

        contract_report: dict[str, Any] = {}

        if self.verify_provider_contract:
            contract_report = self.verify_provider_module_contract(
                module,
                registration,
            )

        raw_config, loaded_config_path = self.load_raw_config(
            module,
            registration,
            config_path=config_path,
        )

        validated_config = self.validate_raw_config(
            module,
            registration,
            raw_config,
        )

        definition = self.create_world_definition(
            module,
            registration,
            validated_config,
        )

        loaded_world = LoadedWorld(
            registration=registration,
            provider_module=module,
            raw_config=dict(validated_config),
            definition=definition,
            config_path=loaded_config_path,
            metadata={
                "loaderVersion": WORLD_LOADER_VERSION,
                "contractReport": contract_report,
            },
        )

        with self._lock:
            if self.cache_enabled and config_path is None:
                self._cache[cache_key] = loaded_world

        return loaded_world

    def get_world_definition(
        self,
        world_id: str | None = None,
        *,
        force_reload: bool = False,
    ) -> WorldDefinition:
        """
        Lädt eine Welt und gibt nur die WorldDefinition zurück.
        """
        return self.load_world(
            world_id,
            force_reload=force_reload,
        ).definition

    def get_provider_function(
        self,
        world_id: str | None,
        function_name: str,
        *,
        required: bool = False,
        force_reload: bool = False,
    ) -> Callable[..., Any] | None:
        """
        Lädt eine Welt und gibt eine Provider-Funktion zurück.
        """
        loaded_world = self.load_world(
            world_id,
            force_reload=force_reload,
        )

        return loaded_world.get_provider_function(
            function_name,
            required=required,
        )

    def list_worlds(self) -> tuple[WorldProviderInfo, ...]:
        """
        Listet registrierte World-Provider.
        """
        return self.registry.list_provider_info(include_disabled=False)

    def has_world(self, world_id: str) -> bool:
        """
        Prüft, ob eine Welt registriert und aktiv ist.
        """
        return self.registry.has(world_id, include_disabled=False)

    def reload_world(self, world_id: str | None = None) -> LoadedWorld:
        """
        Lädt eine Welt neu und ersetzt den Cache-Eintrag.
        """
        return self.load_world(
            world_id,
            force_reload=True,
        )


# ---------------------------------------------------------------------------
# Default loader factory
# ---------------------------------------------------------------------------

def create_default_world_loader(
    *,
    cache_enabled: bool = True,
    verify_provider_contract: bool = True,
    registry: WorldRegistry | None = None,
) -> WorldLoader:
    """
    Erstellt eine neue WorldLoader-Instanz.
    """
    return WorldLoader(
        registry=registry or get_default_world_registry(),
        cache_enabled=cache_enabled,
        verify_provider_contract=verify_provider_contract,
    )


@lru_cache(maxsize=1)
def get_default_world_loader() -> WorldLoader:
    """
    Gibt den pro Prozess gecachten Default-WorldLoader zurück.

    Der Loader selbst cached geladene Welten ebenfalls, sofern cache_enabled=True.

    Cache leeren:

        get_default_world_loader.cache_clear()

    oder:

        reset_default_world_loader_cache()
    """
    return create_default_world_loader(
        cache_enabled=True,
        verify_provider_contract=True,
    )


def reset_default_world_loader_cache() -> None:
    """
    Leert den Cache des Default-Loader-Singletons.

    Falls bereits eine Loader-Instanz erzeugt wurde, wird zusätzlich deren
    interner LoadedWorld-Cache geleert.
    """
    try:
        loader = get_default_world_loader()
        loader.clear_cache()
    except Exception:
        pass

    get_default_world_loader.cache_clear()


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def load_world(
    world_id: str | None = None,
    *,
    loader: WorldLoader | None = None,
    force_reload: bool = False,
) -> LoadedWorld:
    """
    Komfortfunktion zum Laden einer Welt.
    """
    active_loader = loader or get_default_world_loader()

    return active_loader.load_world(
        world_id,
        force_reload=force_reload,
    )


def get_world_definition(
    world_id: str | None = None,
    *,
    loader: WorldLoader | None = None,
    force_reload: bool = False,
) -> WorldDefinition:
    """
    Komfortfunktion zum Laden einer WorldDefinition.
    """
    active_loader = loader or get_default_world_loader()

    return active_loader.get_world_definition(
        world_id,
        force_reload=force_reload,
    )


def get_world_loader_status(
    *,
    loader: WorldLoader | None = None,
) -> WorldLoaderStatus:
    """
    Komfortfunktion für Loader-Diagnose.
    """
    active_loader = loader or get_default_world_loader()

    return active_loader.get_status()


__all__ = (
    "WORLD_LOADER_VERSION",
    "DEFAULT_CONFIG_FILENAME",
    "PROVIDER_FUNCTION_GET_INFO",
    "PROVIDER_FUNCTION_GET_CONFIG_PATH",
    "PROVIDER_FUNCTION_LOAD_CONFIG",
    "PROVIDER_FUNCTION_VALIDATE_CONFIG",
    "PROVIDER_FUNCTION_CREATE_DEFINITION",
    "PROVIDER_FUNCTION_GENERATE_CHUNK",
    "LoadedWorld",
    "WorldLoaderStatus",
    "WorldLoader",
    "create_default_world_loader",
    "get_default_world_loader",
    "reset_default_world_loader_cache",
    "load_world",
    "get_world_definition",
    "get_world_loader_status",
)