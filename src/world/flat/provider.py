# src/world/flat/provider.py
"""
VECTOPLAN Flat World Provider.

Diese Datei ist die Provider-Schnittstelle der konkreten flachen Welt.

Sie verbindet:

    src/world/flat/world.json
        → Weltkonfiguration

    src/world/flat/validator.py
        → Flat-spezifische Validierung

    src/world/flat/generator.py
        → Flat-spezifische Chunk-Generierung

    src/world/loader.py
        → neutrales Laden von Welten

    src/world/service.py
        → neutrale Service-Fassade für spätere Routes

Wichtig:
- Diese Datei ist die Grenze zwischen neutralem World-System und konkreter
  Flat-World-Implementierung.
- Der Loader importiert dieses Modul dynamisch über die Registry.
- Der Service ruft später generate_chunk(...) über den geladenen Provider auf.

Provider-Vertrag:

    get_provider_info() -> WorldProviderInfo

    get_default_config_path() -> Path

    load_world_config(config_path: str | Path | None = None) -> dict[str, Any]

    validate_world_config(raw_config: Mapping[str, Any]) -> dict[str, Any]

    create_world_definition(raw_config: Mapping[str, Any]) -> WorldDefinition

    generate_chunk(world: WorldDefinition, request: ChunkRequest) -> GeneratedChunk

Diese Datei enthält keine Flask-Abhängigkeit, keine Datenbanklogik,
keine Snapshot-Logik, keine Event-Logik und keine Three.js-Objekte.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from collections.abc import Mapping

try:
    from src.world.errors import (
        InvalidWorldConfigFileError,
        InvalidWorldDefinitionError,
        WorldGenerationError,
        WorldProviderContractError,
        WorldProviderError,
        coerce_world_error,
        make_json_safe,
    )
    from src.world.models import (
        ChunkRequest,
        GeneratedChunk,
        WorldDefinition,
        WorldProviderInfo,
    )
    from src.world.flat.generator import (
        FlatWorldGenerator,
        generate_flat_chunk,
        get_default_flat_world_generator,
    )
    from src.world.flat.validator import (
        EXPECTED_COORDINATE_SYSTEM,
        EXPECTED_GENERATOR_TYPE,
        EXPECTED_GENERATOR_VERSION,
        EXPECTED_PROJECTION_TYPE,
        EXPECTED_TOPOLOGY_TYPE,
        EXPECTED_WORLD_ID,
        EXPECTED_WORLD_TYPE,
        FLAT_VALIDATOR_VERSION,
        create_validated_flat_world_definition,
        get_flat_validation_summary,
        validate_flat_world_config,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.flat.provider requires src.world.errors, src.world.models, "
        "src.world.flat.validator and src.world.flat.generator to be importable "
        "before the provider can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Provider constants
# ---------------------------------------------------------------------------

PROVIDER_ID: Final[str] = "flat"
WORLD_ID: Final[str] = "flat"
WORLD_TYPE: Final[str] = "flat"

PROVIDER_LABEL: Final[str] = "Flat Debug World"
PROVIDER_VERSION: Final[str] = "0.1.0"

CONFIG_FILENAME: Final[str] = "world.json"

PROVIDER_MODULE: Final[str] = "src.world.flat.provider"

MAX_CONFIG_FILE_BYTES: Final[int] = 10 * 1024 * 1024

SUPPORTED_PROVIDER_FUNCTIONS: Final[tuple[str, ...]] = (
    "get_provider_info",
    "get_default_config_path",
    "load_world_config",
    "validate_world_config",
    "create_world_definition",
    "generate_chunk",
)


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


def _as_path(value: str | Path | None) -> Path | None:
    """
    Wandelt einen Pfadwert defensiv in Path um.
    """
    if value is None:
        return None

    try:
        return Path(value)
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Flat world config path is invalid.",
            details={
                "configPath": make_json_safe(value),
            },
            cause=exc,
        ) from exc


def _get_package_dir() -> Path:
    """
    Gibt den Ordner dieses Provider-Moduls zurück.
    """
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path.cwd() / "src" / "world" / "flat"


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
            "Flat world config file does not exist.",
            details={
                "configPath": str(resolved),
            },
        )

    if not resolved.is_file():
        raise InvalidWorldConfigFileError(
            "Flat world config path is not a file.",
            details={
                "configPath": str(resolved),
            },
        )

    try:
        size = resolved.stat().st_size
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not inspect flat world config file.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc

    if size > MAX_CONFIG_FILE_BYTES:
        raise InvalidWorldConfigFileError(
            "Flat world config file is too large.",
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
            "Flat world config file must be UTF-8 encoded.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not read flat world config file.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidWorldConfigFileError(
            "Flat world config file contains invalid JSON.",
            details={
                "configPath": str(resolved),
                "line": exc.lineno,
                "column": exc.colno,
                "message": exc.msg,
            },
            cause=exc,
        ) from exc
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Flat world config file could not be parsed.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc

    if not isinstance(parsed, Mapping):
        raise InvalidWorldConfigFileError(
            "Flat world config JSON root must be an object.",
            details={
                "configPath": str(resolved),
                "rootType": type(parsed).__name__,
            },
        )

    return dict(parsed)


def _normalize_raw_config(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    """
    Normalisiert rohe Config-Daten zu einem Dictionary.
    """
    if not isinstance(raw_config, Mapping):
        raise InvalidWorldDefinitionError(
            "Flat world config must be an object.",
            details={
                "configType": type(raw_config).__name__,
                "config": make_json_safe(raw_config),
            },
        )

    return dict(raw_config)


def _ensure_world_definition(value: Any) -> WorldDefinition:
    """
    Erzwingt eine WorldDefinition.
    """
    if isinstance(value, WorldDefinition):
        value.validate()
        return value

    if isinstance(value, Mapping):
        definition = create_validated_flat_world_definition(value)
        definition.validate()
        return definition

    raise WorldProviderContractError(
        "Flat provider expected WorldDefinition or mapping.",
        details={
            "valueType": type(value).__name__,
            "value": make_json_safe(value),
        },
    )


def _ensure_chunk_request(value: Any) -> ChunkRequest:
    """
    Erzwingt eine ChunkRequest.
    """
    if isinstance(value, ChunkRequest):
        value.validate()
        return value

    if isinstance(value, Mapping):
        request = ChunkRequest.from_dict(value)
        request.validate()
        return request

    raise WorldProviderContractError(
        "Flat provider expected ChunkRequest or mapping.",
        details={
            "valueType": type(value).__name__,
            "value": make_json_safe(value),
        },
    )


def _validate_world_matches_provider(world: WorldDefinition) -> None:
    """
    Prüft, ob eine WorldDefinition zu diesem Provider passt.
    """
    errors: list[dict[str, Any]] = []

    if world.world_id != WORLD_ID:
        errors.append(
            {
                "code": "world_id_mismatch",
                "expected": WORLD_ID,
                "actual": world.world_id,
            }
        )

    if world.world_type != WORLD_TYPE:
        errors.append(
            {
                "code": "world_type_mismatch",
                "expected": WORLD_TYPE,
                "actual": world.world_type,
            }
        )

    if world.generator_type != EXPECTED_GENERATOR_TYPE:
        errors.append(
            {
                "code": "generator_type_mismatch",
                "expected": EXPECTED_GENERATOR_TYPE,
                "actual": world.generator_type,
            }
        )

    if world.generator_version != EXPECTED_GENERATOR_VERSION:
        errors.append(
            {
                "code": "generator_version_mismatch",
                "expected": EXPECTED_GENERATOR_VERSION,
                "actual": world.generator_version,
            }
        )

    if world.coordinate_system != EXPECTED_COORDINATE_SYSTEM:
        errors.append(
            {
                "code": "coordinate_system_mismatch",
                "expected": EXPECTED_COORDINATE_SYSTEM,
                "actual": world.coordinate_system,
            }
        )

    if world.projection_type != EXPECTED_PROJECTION_TYPE:
        errors.append(
            {
                "code": "projection_type_mismatch",
                "expected": EXPECTED_PROJECTION_TYPE,
                "actual": world.projection_type,
            }
        )

    if world.topology_type != EXPECTED_TOPOLOGY_TYPE:
        errors.append(
            {
                "code": "topology_type_mismatch",
                "expected": EXPECTED_TOPOLOGY_TYPE,
                "actual": world.topology_type,
            }
        )

    if errors:
        raise InvalidWorldDefinitionError(
            "WorldDefinition does not match flat provider.",
            details={
                "providerId": PROVIDER_ID,
                "worldId": getattr(world, "world_id", None),
                "errors": errors,
            },
        )


# ---------------------------------------------------------------------------
# Provider status
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlatProviderStatus:
    """
    Diagnosezustand des Flat-Providers.
    """

    provider_id: str
    world_id: str
    world_type: str
    provider_label: str
    provider_version: str
    provider_module: str
    config_path: str
    config_exists: bool
    supported_functions: tuple[str, ...]
    validator_version: str
    generator_type: str
    generator_version: str
    ready: bool
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """
        Gibt den Status als Dictionary zurück.
        """
        return asdict(self)


# ---------------------------------------------------------------------------
# Provider contract functions
# ---------------------------------------------------------------------------

def get_default_config_path() -> Path:
    """
    Gibt den Standardpfad zur Flat-World-Konfiguration zurück.

    Erwarteter Pfad:

        src/world/flat/world.json
    """
    return _get_package_dir() / CONFIG_FILENAME


def get_provider_info() -> WorldProviderInfo:
    """
    Gibt öffentliche Provider-Informationen zurück.

    Diese Funktion wird vom Loader oder von Diagnose-/Listenfunktionen genutzt.
    """
    return WorldProviderInfo(
        provider_id=PROVIDER_ID,
        world_type=WORLD_TYPE,
        label=PROVIDER_LABEL,
        provider_module=PROVIDER_MODULE,
        config_path=str(get_default_config_path()),
        supports_chunk_generation=True,
        supports_world_metadata=True,
        metadata={
            "providerVersion": PROVIDER_VERSION,
            "generatorType": EXPECTED_GENERATOR_TYPE,
            "generatorVersion": EXPECTED_GENERATOR_VERSION,
            "projectionType": EXPECTED_PROJECTION_TYPE,
            "topologyType": EXPECTED_TOPOLOGY_TYPE,
            "coordinateSystem": EXPECTED_COORDINATE_SYSTEM,
            "description": "Initial deterministic flat world provider.",
        },
    )


def load_world_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    Lädt die Flat-World-Konfiguration aus world.json.

    Wenn config_path None ist, wird get_default_config_path() verwendet.
    """
    try:
        path = _as_path(config_path) or get_default_config_path()
        return _read_json_file(path)

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="Flat provider could not load world config.",
            fallback_code="flat_world_config_load_failed",
            fallback_status_code=500,
            details={
                "providerId": PROVIDER_ID,
                "configPath": str(config_path) if config_path else str(get_default_config_path()),
            },
        )
        raise world_error from exc


def validate_world_config(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    """
    Validiert und normalisiert eine Flat-World-Konfiguration.

    Rückgabe:
        normalisierte Config als dict

    Diese Funktion erfüllt den Provider-Vertrag des neutralen Loaders.
    """
    try:
        normalized = validate_flat_world_config(raw_config)

        # Provider-spezifische Sicherheitsprüfung:
        # Der Validator prüft bereits, aber diese explizite Wiederholung hält
        # die Provider-Grenze stabil.
        world_id = _safe_str(normalized.get("worldId"))
        world_type = _safe_str(normalized.get("worldType"))
        generator_type = _safe_str(normalized.get("generatorType"))

        if world_id != WORLD_ID:
            raise InvalidWorldDefinitionError(
                "Flat config worldId does not match provider.",
                details={
                    "expected": WORLD_ID,
                    "actual": world_id,
                },
            )

        if world_type != WORLD_TYPE:
            raise InvalidWorldDefinitionError(
                "Flat config worldType does not match provider.",
                details={
                    "expected": WORLD_TYPE,
                    "actual": world_type,
                },
            )

        if generator_type != EXPECTED_GENERATOR_TYPE:
            raise InvalidWorldDefinitionError(
                "Flat config generatorType does not match provider.",
                details={
                    "expected": EXPECTED_GENERATOR_TYPE,
                    "actual": generator_type,
                },
            )

        return normalized

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="Flat provider config validation failed.",
            fallback_code="flat_world_config_validation_failed",
            fallback_status_code=400,
            details={
                "providerId": PROVIDER_ID,
                "config": make_json_safe(raw_config),
            },
        )
        raise world_error from exc


def create_world_definition(raw_config: Mapping[str, Any]) -> WorldDefinition:
    """
    Erstellt eine validierte WorldDefinition für die flache Welt.

    Diese Funktion erfüllt den Provider-Vertrag des neutralen Loaders.
    """
    try:
        normalized = validate_world_config(raw_config)
        definition = create_validated_flat_world_definition(normalized)
        definition.validate()

        _validate_world_matches_provider(definition)

        return definition

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="Flat provider could not create world definition.",
            fallback_code="flat_world_definition_create_failed",
            fallback_status_code=400,
            details={
                "providerId": PROVIDER_ID,
                "config": make_json_safe(raw_config),
            },
        )
        raise world_error from exc


def generate_chunk(
    world: WorldDefinition,
    request: ChunkRequest,
) -> GeneratedChunk:
    """
    Generiert einen Chunk für die flache Welt.

    Erwartete Signatur für WorldService:

        generate_chunk(world: WorldDefinition, request: ChunkRequest) -> GeneratedChunk

    Diese Funktion delegiert an src/world/flat/generator.py.
    """
    try:
        normalized_world = _ensure_world_definition(world)
        normalized_request = _ensure_chunk_request(request)

        _validate_world_matches_provider(normalized_world)

        if normalized_request.world_id != normalized_world.world_id:
            raise WorldProviderContractError(
                "ChunkRequest worldId does not match Flat WorldDefinition.",
                details={
                    "requestWorldId": normalized_request.world_id,
                    "worldId": normalized_world.world_id,
                    "chunkKey": normalized_request.chunk_key,
                },
            )

        chunk = generate_flat_chunk(
            normalized_world,
            normalized_request,
        )

        if not isinstance(chunk, GeneratedChunk):
            raise WorldProviderContractError(
                "Flat generator returned invalid result.",
                details={
                    "returnType": type(chunk).__name__,
                    "returnValue": make_json_safe(chunk),
                },
            )

        chunk.validate()
        return chunk

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="Flat provider chunk generation failed.",
            fallback_code="flat_provider_chunk_generation_failed",
            fallback_status_code=500,
            details={
                "providerId": PROVIDER_ID,
                "worldId": getattr(world, "world_id", None),
                "request": request.to_dict(camel_case=True)
                if isinstance(request, ChunkRequest)
                else make_json_safe(request),
            },
        )
        raise world_error from exc


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def load_default_world_definition(
    *,
    config_path: str | Path | None = None,
) -> WorldDefinition:
    """
    Lädt die Standard-Flat-World und gibt eine WorldDefinition zurück.

    Diese Funktion ist für Tests und interne Diagnose hilfreich.
    Der normale Produktionspfad läuft über WorldLoader/WorldService.
    """
    raw_config = load_world_config(config_path)
    return create_world_definition(raw_config)


def generate_default_chunk(
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    *,
    config_path: str | Path | None = None,
    generator: FlatWorldGenerator | None = None,
) -> GeneratedChunk:
    """
    Komfortfunktion für Tests:

        generate_default_chunk(0, 0, 0)

    Lädt world.json und generiert direkt einen Chunk.
    """
    try:
        world = load_default_world_definition(config_path=config_path)
        request = ChunkRequest.create(
            world_id=world.world_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            metadata={
                "source": "generate_default_chunk",
            },
        )

        active_generator = generator or get_default_flat_world_generator()
        return active_generator.generate_chunk(world, request)

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="Flat provider could not generate default chunk.",
            fallback_code="flat_default_chunk_generation_failed",
            fallback_status_code=500,
            details={
                "providerId": PROVIDER_ID,
                "chunkX": chunk_x,
                "chunkY": chunk_y,
                "chunkZ": chunk_z,
            },
        )
        raise world_error from exc


def get_provider_status(
    *,
    include_config_validation: bool = True,
) -> FlatProviderStatus:
    """
    Gibt einen Diagnosezustand des Providers zurück.

    Diese Funktion ist für Tests, Startup-Checks und spätere Health-Diagnosen
    nützlich.
    """
    config_path = get_default_config_path()
    config_exists = config_path.is_file()

    ready = config_exists
    metadata: dict[str, Any] = {
        "packageDir": str(_get_package_dir()),
    }

    if include_config_validation and config_exists:
        try:
            raw_config = load_world_config(config_path)
            summary = get_flat_validation_summary(raw_config)
            metadata["configValidation"] = summary
            ready = ready and bool(summary.get("ok"))
        except Exception as exc:
            metadata["configValidation"] = {
                "ok": False,
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
            ready = False

    return FlatProviderStatus(
        provider_id=PROVIDER_ID,
        world_id=WORLD_ID,
        world_type=WORLD_TYPE,
        provider_label=PROVIDER_LABEL,
        provider_version=PROVIDER_VERSION,
        provider_module=PROVIDER_MODULE,
        config_path=str(config_path),
        config_exists=config_exists,
        supported_functions=SUPPORTED_PROVIDER_FUNCTIONS,
        validator_version=FLAT_VALIDATOR_VERSION,
        generator_type=EXPECTED_GENERATOR_TYPE,
        generator_version=EXPECTED_GENERATOR_VERSION,
        ready=ready,
        metadata=metadata,
    )


def require_provider_ready() -> None:
    """
    Erzwingt, dass der Flat-Provider startbereit ist.

    Diese Funktion sollte nicht automatisch beim Import ausgeführt werden.
    Sie ist für explizite Startup-Checks gedacht.
    """
    status = get_provider_status(include_config_validation=True)

    if status.ready:
        return

    raise WorldProviderError(
        "Flat provider is not ready.",
        details=status.to_dict(),
    )


def get_provider_contract() -> dict[str, Any]:
    """
    Gibt den Provider-Vertrag als JSON-nahe Struktur zurück.

    Diese Funktion ist für Tests und Dokumentation hilfreich.
    """
    return {
        "providerId": PROVIDER_ID,
        "worldId": WORLD_ID,
        "worldType": WORLD_TYPE,
        "providerModule": PROVIDER_MODULE,
        "providerVersion": PROVIDER_VERSION,
        "configFilename": CONFIG_FILENAME,
        "requiredFunctions": list(SUPPORTED_PROVIDER_FUNCTIONS),
        "generator": {
            "type": EXPECTED_GENERATOR_TYPE,
            "version": EXPECTED_GENERATOR_VERSION,
        },
        "world": {
            "coordinateSystem": EXPECTED_COORDINATE_SYSTEM,
            "projectionType": EXPECTED_PROJECTION_TYPE,
            "topologyType": EXPECTED_TOPOLOGY_TYPE,
        },
    }


# ---------------------------------------------------------------------------
# Cached helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_cached_default_world_definition() -> WorldDefinition:
    """
    Gibt eine gecachte WorldDefinition aus der Standard-world.json zurück.

    Diese Funktion ist für Tests und lokale Diagnose gedacht.
    Der normale Service-Pfad cached über WorldLoader.
    """
    return load_default_world_definition()


def reset_provider_caches() -> None:
    """
    Leert Provider-eigene Caches.
    """
    get_cached_default_world_definition.cache_clear()


__all__ = (
    "PROVIDER_ID",
    "WORLD_ID",
    "WORLD_TYPE",
    "PROVIDER_LABEL",
    "PROVIDER_VERSION",
    "CONFIG_FILENAME",
    "PROVIDER_MODULE",
    "SUPPORTED_PROVIDER_FUNCTIONS",
    "FlatProviderStatus",
    "get_default_config_path",
    "get_provider_info",
    "load_world_config",
    "validate_world_config",
    "create_world_definition",
    "generate_chunk",
    "load_default_world_definition",
    "generate_default_chunk",
    "get_provider_status",
    "require_provider_ready",
    "get_provider_contract",
    "get_cached_default_world_definition",
    "reset_provider_caches",
)