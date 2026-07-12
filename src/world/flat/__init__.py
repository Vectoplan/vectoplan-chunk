# src/world/flat/__init__.py
"""
VECTOPLAN Flat World Provider Package.

Dieses Package enthält die konkrete Standard-Flat-World des Chunk-Service:

    src/world/flat/

Die Welt ist:

- lokal
- flach
- deterministisch
- chunkbasiert
- providerbasiert
- ohne direkte Flask-Abhängigkeit
- ohne direkte SQLAlchemy-Abhängigkeit
- ohne direkte Datenbankzugriffe
- ohne Geodaten
- ohne Core-Abhängigkeit
- ohne Library-Service-Abhängigkeit
- ohne Three.js-Objekte

Die kanonische Flat-World basiert ausschließlich auf zwei unveränderlichen
Systemzuständen:

    system_air
        - reservierter leerer Zellzustand
        - cellValue = 0
        - keine BlockType-Zeile
        - kein positiver Paletteintrag

    system_terrain
        - einziger positiver PaletteEntry
        - Oberfläche und Untergrund
        - persistenter, unveränderlicher Systemblock
        - Zellwert wird palettenlokal über paletteIndex + 1 bestimmt

Dateiaufteilung:

    src/world/flat/world.json
        -> deklarative Weltkonfiguration

    src/world/flat/validator.py
        -> Validierung und Normalisierung der Air-/Terrain-Invarianten

    src/world/flat/generator.py
        -> deterministische Chunk-Generierung

    src/world/flat/provider.py
        -> Provider-Vertrag für WorldLoader und WorldService

Dieses __init__.py bleibt bewusst leichtgewichtig:

- keine world.json-Lesung beim Package-Import
- keine Chunk-Generierung beim Package-Import
- keine Provider-Readiness-Prüfung beim Package-Import
- keine Datenbankzugriffe
- keine harten Imports der Provider-, Validator- oder Generator-Module
- Lazy-Imports über __getattr__
- prozesslokaler, begrenzter Symbol-Cache
- strukturelle Package-Diagnose ohne Modulimport
- optionale tiefe Provider-Diagnose nur auf explizite Anfrage
- explizite Cache-Reset-Funktionen für Development und Tests

Der bevorzugte Produktivzugriff bleibt:

    from src.world.service import get_default_world_service

Direkte Importe aus src.world.flat sind primär für Providerintegration,
Startup-Diagnose, Unit-Tests und interne Werkzeuge vorgesehen.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType
from typing import Any, Final


# ---------------------------------------------------------------------------
# Package and provider metadata
# ---------------------------------------------------------------------------

PACKAGE_NAME: Final[str] = "src.world.flat"
PACKAGE_LABEL: Final[str] = "VECTOPLAN Flat System Terrain World"
PACKAGE_VERSION: Final[str] = "0.2.0"
PACKAGE_CONTRACT_VERSION: Final[str] = "flat-package-contract.v2"

PROVIDER_ID: Final[str] = "flat"
WORLD_ID: Final[str] = "flat"
WORLD_TYPE: Final[str] = "flat"

PROVIDER_LABEL: Final[str] = "Flat System Terrain World"
PROVIDER_VERSION: Final[str] = "0.2.0"
PROVIDER_CONTRACT_VERSION: Final[str] = "flat-provider-contract.v2"

GENERATOR_TYPE: Final[str] = "flat-world"
GENERATOR_VERSION: Final[str] = "2"
GENERATOR_IMPLEMENTATION_VERSION: Final[str] = "0.2.0"
GENERATION_RULE_VERSION: Final[str] = "flat-generation-rules.v2"
GENERATION_CONTRACT_VERSION: Final[str] = (
    "flat-system-air-terrain-generation.v1"
)

VALIDATOR_VERSION: Final[str] = "0.2.0"
VALIDATION_CONTRACT_VERSION: Final[str] = "flat-validation-contract.v2"

CONFIG_FILENAME: Final[str] = "world.json"
CONFIG_FILE_IDENTITY: Final[str] = "src/world/flat/world.json"

PROJECTION_TYPE: Final[str] = "flat-local-v1"
TOPOLOGY_TYPE: Final[str] = "flat-unbounded-v1"
COORDINATE_SYSTEM: Final[str] = "vectoplan-world-y-up-v1"

SYSTEM_AIR_BLOCK_ID: Final[str] = "system_air"
SYSTEM_TERRAIN_BLOCK_TYPE_ID: Final[str] = "system_terrain"
AIR_CELL_VALUE: Final[int] = 0
TERRAIN_CELL_VALUE_RULE: Final[str] = "paletteIndex + 1"

LAZY_SYMBOL_CACHE_SIZE: Final[int] = 256


EXPECTED_FILES: Final[tuple[str, ...]] = (
    "__init__.py",
    CONFIG_FILENAME,
    "provider.py",
    "validator.py",
    "generator.py",
)

EXPECTED_MODULES: Final[tuple[str, ...]] = (
    "provider",
    "validator",
    "generator",
)

EXPECTED_MODULE_VERSIONS: Final[dict[str, str]] = {
    "provider": PROVIDER_VERSION,
    "validator": VALIDATOR_VERSION,
    "generator": GENERATOR_IMPLEMENTATION_VERSION,
}


# ---------------------------------------------------------------------------
# Lazy public symbol registry
# ---------------------------------------------------------------------------

_PUBLIC_SYMBOLS: Final[dict[str, tuple[str, str]]] = {
    # provider.py
    "FlatConfigFileSignature": (
        "src.world.flat.provider",
        "FlatConfigFileSignature",
    ),
    "FlatProviderStatus": (
        "src.world.flat.provider",
        "FlatProviderStatus",
    ),
    "get_provider_info": (
        "src.world.flat.provider",
        "get_provider_info",
    ),
    "get_default_config_path": (
        "src.world.flat.provider",
        "get_default_config_path",
    ),
    "load_world_config": (
        "src.world.flat.provider",
        "load_world_config",
    ),
    "validate_world_config": (
        "src.world.flat.provider",
        "validate_world_config",
    ),
    "create_world_definition": (
        "src.world.flat.provider",
        "create_world_definition",
    ),
    "generate_chunk": (
        "src.world.flat.provider",
        "generate_chunk",
    ),
    "load_default_world_definition": (
        "src.world.flat.provider",
        "load_default_world_definition",
    ),
    "generate_default_chunk": (
        "src.world.flat.provider",
        "generate_default_chunk",
    ),
    "get_provider_cache_info": (
        "src.world.flat.provider",
        "get_provider_cache_info",
    ),
    "get_provider_status": (
        "src.world.flat.provider",
        "get_provider_status",
    ),
    "require_provider_ready": (
        "src.world.flat.provider",
        "require_provider_ready",
    ),
    "get_provider_contract": (
        "src.world.flat.provider",
        "get_provider_contract",
    ),
    "get_cached_default_world_definition": (
        "src.world.flat.provider",
        "get_cached_default_world_definition",
    ),
    "reset_provider_caches": (
        "src.world.flat.provider",
        "reset_provider_caches",
    ),

    # validator.py
    "FlatSystemBlockContract": (
        "src.world.flat.validator",
        "FlatSystemBlockContract",
    ),
    "FlatLayerRuleContract": (
        "src.world.flat.validator",
        "FlatLayerRuleContract",
    ),
    "FlatWorldValidationResult": (
        "src.world.flat.validator",
        "FlatWorldValidationResult",
    ),
    "get_flat_system_block_contract": (
        "src.world.flat.validator",
        "get_flat_system_block_contract",
    ),
    "get_expected_flat_palette_block_type_ids": (
        "src.world.flat.validator",
        "get_expected_flat_palette_block_type_ids",
    ),
    "get_expected_flat_layer_rule_contracts": (
        "src.world.flat.validator",
        "get_expected_flat_layer_rule_contracts",
    ),
    "validate_flat_world_config": (
        "src.world.flat.validator",
        "validate_flat_world_config",
    ),
    "validate_flat_world_config_detailed": (
        "src.world.flat.validator",
        "validate_flat_world_config_detailed",
    ),
    "create_validated_flat_world_definition": (
        "src.world.flat.validator",
        "create_validated_flat_world_definition",
    ),
    "get_flat_layer_block_type_ids": (
        "src.world.flat.validator",
        "get_flat_layer_block_type_ids",
    ),
    "get_flat_validation_summary": (
        "src.world.flat.validator",
        "get_flat_validation_summary",
    ),
    "get_flat_validator_cache_info": (
        "src.world.flat.validator",
        "get_flat_validator_cache_info",
    ),
    "clear_flat_validator_caches": (
        "src.world.flat.validator",
        "clear_flat_validator_caches",
    ),

    # generator.py
    "FlatGenerationProfileKey": (
        "src.world.flat.generator",
        "FlatGenerationProfileKey",
    ),
    "FlatGenerationStats": (
        "src.world.flat.generator",
        "FlatGenerationStats",
    ),
    "FlatGenerationContext": (
        "src.world.flat.generator",
        "FlatGenerationContext",
    ),
    "FlatWorldGenerator": (
        "src.world.flat.generator",
        "FlatWorldGenerator",
    ),
    "generate_flat_chunk": (
        "src.world.flat.generator",
        "generate_flat_chunk",
    ),
    "get_flat_vertical_profile": (
        "src.world.flat.generator",
        "get_flat_vertical_profile",
    ),
    "get_default_flat_world_generator": (
        "src.world.flat.generator",
        "get_default_flat_world_generator",
    ),
    "get_flat_generation_profile_cache_info": (
        "src.world.flat.generator",
        "get_flat_generation_profile_cache_info",
    ),
    "get_default_flat_world_generator_cache_info": (
        "src.world.flat.generator",
        "get_default_flat_world_generator_cache_info",
    ),
    "clear_flat_generation_profile_cache": (
        "src.world.flat.generator",
        "clear_flat_generation_profile_cache",
    ),
    "reset_default_flat_world_generator_cache": (
        "src.world.flat.generator",
        "reset_default_flat_world_generator_cache",
    ),
}


__all__ = (
    "PACKAGE_NAME",
    "PACKAGE_LABEL",
    "PACKAGE_VERSION",
    "PACKAGE_CONTRACT_VERSION",
    "PROVIDER_ID",
    "WORLD_ID",
    "WORLD_TYPE",
    "PROVIDER_LABEL",
    "PROVIDER_VERSION",
    "PROVIDER_CONTRACT_VERSION",
    "GENERATOR_TYPE",
    "GENERATOR_VERSION",
    "GENERATOR_IMPLEMENTATION_VERSION",
    "GENERATION_RULE_VERSION",
    "GENERATION_CONTRACT_VERSION",
    "VALIDATOR_VERSION",
    "VALIDATION_CONTRACT_VERSION",
    "CONFIG_FILENAME",
    "CONFIG_FILE_IDENTITY",
    "PROJECTION_TYPE",
    "TOPOLOGY_TYPE",
    "COORDINATE_SYSTEM",
    "SYSTEM_AIR_BLOCK_ID",
    "SYSTEM_TERRAIN_BLOCK_TYPE_ID",
    "AIR_CELL_VALUE",
    "TERRAIN_CELL_VALUE_RULE",
    "EXPECTED_FILES",
    "EXPECTED_MODULES",
    "EXPECTED_MODULE_VERSIONS",
    "FlatWorldPackageStatus",
    "get_flat_package_dir",
    "get_flat_config_path",
    "get_flat_package_status",
    "get_flat_package_status_cache_info",
    "is_flat_package_ready",
    "require_flat_package_ready",
    "get_flat_package_contract",
    "get_public_symbol_map",
    "get_lazy_symbol_cache_info",
    "reset_flat_package_caches",
    *_PUBLIC_SYMBOLS.keys(),
)


# ---------------------------------------------------------------------------
# Path and module helpers
# ---------------------------------------------------------------------------

def _safe_string(value: Any, *, default: str = "") -> str:
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


def get_flat_package_dir() -> Path:
    """
    Gibt den absoluten Ordner dieses Flat-World-Packages zurück.
    """
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path.cwd() / "src" / "world" / "flat"


def get_flat_config_path() -> Path:
    """
    Gibt den erwarteten Pfad zu src/world/flat/world.json zurück.
    """
    return get_flat_package_dir() / CONFIG_FILENAME


def _module_path(module_name: str) -> str:
    """
    Baut einen vollständigen Modulpfad innerhalb von src.world.flat.
    """
    return f"{PACKAGE_NAME}.{module_name}"


def _module_exists(module_name: str) -> bool:
    """
    Prüft defensiv, ob ein Flat-World-Modul auffindbar ist.

    Das Modul wird nicht importiert.
    """
    try:
        return find_spec(_module_path(module_name)) is not None
    except Exception:
        return False


def _file_exists(filename: str) -> bool:
    """
    Prüft defensiv, ob eine erwartete Datei existiert.
    """
    try:
        return (get_flat_package_dir() / filename).is_file()
    except Exception:
        return False


def _get_file_metadata(filename: str) -> dict[str, Any]:
    """
    Ermittelt kleine, JSON-nahe Dateimetadaten für Diagnosezwecke.
    """
    path = get_flat_package_dir() / filename

    try:
        stat = path.stat()
        return {
            "filename": filename,
            "path": str(path),
            "exists": path.is_file(),
            "sizeBytes": int(stat.st_size),
            "modifiedTimeNs": int(
                getattr(
                    stat,
                    "st_mtime_ns",
                    int(stat.st_mtime * 1_000_000_000),
                )
            ),
        }
    except FileNotFoundError:
        return {
            "filename": filename,
            "path": str(path),
            "exists": False,
            "sizeBytes": None,
            "modifiedTimeNs": None,
        }
    except Exception as exc:
        return {
            "filename": filename,
            "path": str(path),
            "exists": False,
            "sizeBytes": None,
            "modifiedTimeNs": None,
            "errorType": type(exc).__name__,
            "error": str(exc),
        }


def _import_module_safe(module_path: str) -> ModuleType:
    """
    Importiert ein Flat-Modul mit klaren Fehlern.
    """
    try:
        module = import_module(module_path)
    except ModuleNotFoundError as exc:
        raise ImportError(
            f"Flat world module '{module_path}' was not found."
        ) from exc
    except Exception as exc:
        raise ImportError(
            f"Flat world module '{module_path}' could not be imported."
        ) from exc

    if not isinstance(module, ModuleType):
        raise ImportError(
            f"Flat world target '{module_path}' is not a module."
        )

    return module


def _version_from_module(
    module: ModuleType,
    *,
    attribute_names: tuple[str, ...],
) -> str | None:
    """
    Liest die erste verfügbare Versionskonstante eines Moduls.
    """
    for attribute_name in attribute_names:
        try:
            value = getattr(module, attribute_name, None)
        except Exception:
            continue

        text = _safe_string(value)

        if text:
            return text

    return None


# ---------------------------------------------------------------------------
# Package diagnostics
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlatWorldPackageStatus:
    """
    Diagnosezustand des Flat-World-Packages.

    structural_ready:
        Erwartete Dateien und Module sind vorhanden.

    runtime_ready:
        Optionale tiefe Providerprüfung war erfolgreich.

    ready:
        Bei rein struktureller Prüfung entspricht ready structural_ready.
        Bei tiefer Prüfung müssen structural_ready und runtime_ready gelten.
    """

    package_name: str
    package_label: str
    package_version: str
    package_contract_version: str

    provider_id: str
    world_id: str
    world_type: str
    provider_label: str
    provider_version: str
    provider_contract_version: str

    generator_type: str
    generator_version: str
    generator_implementation_version: str
    generation_rule_version: str
    generation_contract_version: str

    validator_version: str
    validation_contract_version: str

    air_system_block_id: str
    terrain_system_block_id: str
    air_cell_value: int
    terrain_cell_value_rule: str

    package_dir: str
    config_path: str

    expected_files: tuple[str, ...]
    available_files: tuple[str, ...]
    missing_files: tuple[str, ...]
    file_metadata: tuple[dict[str, Any], ...]

    expected_modules: tuple[str, ...]
    available_modules: tuple[str, ...]
    missing_modules: tuple[str, ...]

    module_versions: dict[str, str | None]
    version_mismatches: tuple[dict[str, Any], ...]

    deep_validation_requested: bool
    structural_ready: bool
    runtime_ready: bool | None
    ready: bool

    provider_status: dict[str, Any] = field(default_factory=dict)
    errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Gibt den Diagnosezustand als JSON-nahe camelCase-Struktur zurück.
        """
        data = asdict(self)

        rename = {
            "package_name": "packageName",
            "package_label": "packageLabel",
            "package_version": "packageVersion",
            "package_contract_version": "packageContractVersion",
            "provider_id": "providerId",
            "world_id": "worldId",
            "world_type": "worldType",
            "provider_label": "providerLabel",
            "provider_version": "providerVersion",
            "provider_contract_version": "providerContractVersion",
            "generator_type": "generatorType",
            "generator_version": "generatorVersion",
            "generator_implementation_version": (
                "generatorImplementationVersion"
            ),
            "generation_rule_version": "generationRuleVersion",
            "generation_contract_version": "generationContractVersion",
            "validator_version": "validatorVersion",
            "validation_contract_version": "validationContractVersion",
            "air_system_block_id": "airSystemBlockId",
            "terrain_system_block_id": "terrainSystemBlockId",
            "air_cell_value": "airCellValue",
            "terrain_cell_value_rule": "terrainCellValueRule",
            "package_dir": "packageDir",
            "config_path": "configPath",
            "expected_files": "expectedFiles",
            "available_files": "availableFiles",
            "missing_files": "missingFiles",
            "file_metadata": "fileMetadata",
            "expected_modules": "expectedModules",
            "available_modules": "availableModules",
            "missing_modules": "missingModules",
            "module_versions": "moduleVersions",
            "version_mismatches": "versionMismatches",
            "deep_validation_requested": "deepValidationRequested",
            "structural_ready": "structuralReady",
            "runtime_ready": "runtimeReady",
            "provider_status": "providerStatus",
        }

        for old_key, new_key in rename.items():
            data[new_key] = data.pop(old_key)

        for sequence_key in (
            "expectedFiles",
            "availableFiles",
            "missingFiles",
            "fileMetadata",
            "expectedModules",
            "availableModules",
            "missingModules",
            "versionMismatches",
            "errors",
            "warnings",
        ):
            data[sequence_key] = list(data.get(sequence_key, ()))

        return data


def _append_issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    **details: Any,
) -> None:
    """
    Ergänzt einen strukturierten Diagnoseeintrag.
    """
    item: dict[str, Any] = {
        "code": code,
        "message": message,
    }

    for key, value in details.items():
        item[key] = value

    issues.append(item)


def _inspect_module_versions(
    available_modules: tuple[str, ...],
) -> tuple[dict[str, str | None], tuple[dict[str, Any], ...]]:
    """
    Importiert vorhandene Module nur für eine explizite tiefe Diagnose und
    prüft deren Implementierungsversionen.
    """
    versions: dict[str, str | None] = {}
    mismatches: list[dict[str, Any]] = []

    version_attributes = {
        "provider": ("PROVIDER_VERSION",),
        "validator": ("FLAT_VALIDATOR_VERSION",),
        "generator": ("FLAT_GENERATOR_VERSION",),
    }

    for module_name in available_modules:
        module_path = _module_path(module_name)

        try:
            module = _import_module_safe(module_path)
            actual_version = _version_from_module(
                module,
                attribute_names=version_attributes.get(
                    module_name,
                    (),
                ),
            )
        except Exception as exc:
            versions[module_name] = None
            _append_issue(
                mismatches,
                code="module_import_failed",
                message=(
                    f"Flat module '{module_name}' could not be imported "
                    "during deep validation."
                ),
                moduleName=module_name,
                modulePath=module_path,
                errorType=type(exc).__name__,
                error=str(exc),
            )
            continue

        versions[module_name] = actual_version
        expected_version = EXPECTED_MODULE_VERSIONS.get(module_name)

        if expected_version and actual_version != expected_version:
            _append_issue(
                mismatches,
                code="module_version_mismatch",
                message=(
                    f"Flat module '{module_name}' version does not match "
                    "the package contract."
                ),
                moduleName=module_name,
                actual=actual_version,
                expected=expected_version,
            )

    return versions, tuple(mismatches)


def _build_flat_package_status(
    *,
    deep_validation: bool,
) -> FlatWorldPackageStatus:
    """
    Erstellt den Package-Status ohne öffentlichen Cache.
    """
    available_files: list[str] = []
    missing_files: list[str] = []
    file_metadata: list[dict[str, Any]] = []

    for filename in EXPECTED_FILES:
        metadata = _get_file_metadata(filename)
        file_metadata.append(metadata)

        if bool(metadata.get("exists")):
            available_files.append(filename)
        else:
            missing_files.append(filename)

    available_modules: list[str] = []
    missing_modules: list[str] = []

    for module_name in EXPECTED_MODULES:
        if _module_exists(module_name):
            available_modules.append(module_name)
        else:
            missing_modules.append(module_name)

    structural_ready = (
        not missing_files
        and not missing_modules
    )

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if missing_files:
        _append_issue(
            errors,
            code="missing_flat_package_files",
            message="One or more required Flat package files are missing.",
            missingFiles=missing_files,
        )

    if missing_modules:
        _append_issue(
            errors,
            code="missing_flat_package_modules",
            message="One or more required Flat modules are unavailable.",
            missingModules=missing_modules,
        )

    module_versions: dict[str, str | None] = {}
    version_mismatches: tuple[dict[str, Any], ...] = tuple()
    runtime_ready: bool | None = None
    provider_status: dict[str, Any] = {}

    if deep_validation and structural_ready:
        module_versions, version_mismatches = _inspect_module_versions(
            tuple(available_modules)
        )

        if version_mismatches:
            errors.extend(version_mismatches)

        try:
            provider_module = _import_module_safe(
                "src.world.flat.provider"
            )
            get_status = getattr(
                provider_module,
                "get_provider_status",
                None,
            )

            if not callable(get_status):
                raise AttributeError(
                    "src.world.flat.provider.get_provider_status is missing "
                    "or not callable."
                )

            status_object = get_status(
                include_config_validation=True,
                include_content_hash=False,
                force_refresh=False,
            )

            if hasattr(status_object, "to_dict") and callable(
                status_object.to_dict
            ):
                provider_status = status_object.to_dict()
            elif isinstance(status_object, dict):
                provider_status = dict(status_object)
            else:
                provider_status = {
                    "value": str(status_object),
                }

            runtime_ready = bool(
                provider_status.get("ready")
            )

            if not runtime_ready:
                _append_issue(
                    errors,
                    code="flat_provider_not_ready",
                    message=(
                        "The Flat provider failed deep readiness validation."
                    ),
                    providerStatus=provider_status,
                )

        except Exception as exc:
            runtime_ready = False
            _append_issue(
                errors,
                code="flat_provider_status_failed",
                message=(
                    "The Flat provider could not be inspected during deep "
                    "package validation."
                ),
                errorType=type(exc).__name__,
                error=str(exc),
            )

    elif deep_validation:
        runtime_ready = False
        _append_issue(
            warnings,
            code="deep_validation_skipped",
            message=(
                "Deep Flat provider validation was skipped because the "
                "package structure is incomplete."
            ),
        )

    ready = structural_ready

    if deep_validation:
        ready = bool(
            structural_ready
            and runtime_ready
            and not version_mismatches
            and not errors
        )

    package_dir = get_flat_package_dir()
    config_path = get_flat_config_path()

    return FlatWorldPackageStatus(
        package_name=PACKAGE_NAME,
        package_label=PACKAGE_LABEL,
        package_version=PACKAGE_VERSION,
        package_contract_version=PACKAGE_CONTRACT_VERSION,
        provider_id=PROVIDER_ID,
        world_id=WORLD_ID,
        world_type=WORLD_TYPE,
        provider_label=PROVIDER_LABEL,
        provider_version=PROVIDER_VERSION,
        provider_contract_version=PROVIDER_CONTRACT_VERSION,
        generator_type=GENERATOR_TYPE,
        generator_version=GENERATOR_VERSION,
        generator_implementation_version=(
            GENERATOR_IMPLEMENTATION_VERSION
        ),
        generation_rule_version=GENERATION_RULE_VERSION,
        generation_contract_version=(
            GENERATION_CONTRACT_VERSION
        ),
        validator_version=VALIDATOR_VERSION,
        validation_contract_version=VALIDATION_CONTRACT_VERSION,
        air_system_block_id=SYSTEM_AIR_BLOCK_ID,
        terrain_system_block_id=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
        air_cell_value=AIR_CELL_VALUE,
        terrain_cell_value_rule=TERRAIN_CELL_VALUE_RULE,
        package_dir=str(package_dir),
        config_path=str(config_path),
        expected_files=EXPECTED_FILES,
        available_files=tuple(available_files),
        missing_files=tuple(missing_files),
        file_metadata=tuple(file_metadata),
        expected_modules=EXPECTED_MODULES,
        available_modules=tuple(available_modules),
        missing_modules=tuple(missing_modules),
        module_versions=module_versions,
        version_mismatches=version_mismatches,
        deep_validation_requested=deep_validation,
        structural_ready=structural_ready,
        runtime_ready=runtime_ready,
        ready=ready,
        provider_status=provider_status,
        errors=tuple(errors),
        warnings=tuple(warnings),
        metadata={
            "lazyImports": True,
            "importsProviderAtPackageImport": False,
            "readsConfigAtPackageImport": False,
            "generatesChunksAtPackageImport": False,
            "systemBlocks": {
                "air": {
                    "systemBlockId": SYSTEM_AIR_BLOCK_ID,
                    "cellValue": AIR_CELL_VALUE,
                    "storedInPositivePalette": False,
                },
                "terrain": {
                    "systemBlockId": (
                        SYSTEM_TERRAIN_BLOCK_TYPE_ID
                    ),
                    "cellValueRule": TERRAIN_CELL_VALUE_RULE,
                    "storedInPositivePalette": True,
                },
            },
        },
    )


@lru_cache(maxsize=2)
def _get_flat_package_status_cached(
    deep_validation: bool,
) -> FlatWorldPackageStatus:
    """
    Gecachte interne Statusfunktion.

    Es existieren maximal zwei Einträge:
    - rein strukturell
    - strukturell plus tiefe Providerprüfung
    """
    return _build_flat_package_status(
        deep_validation=deep_validation,
    )


def get_flat_package_status(
    *,
    deep_validation: bool = False,
    force_refresh: bool = False,
) -> FlatWorldPackageStatus:
    """
    Ermittelt den aktuellen Flat-Package-Status.

    Standardmäßig werden nur Dateien und importierbare Modulspezifikationen
    geprüft. Erst deep_validation=True importiert Provider, Validator und
    Generator und prüft die vollständige Provider-Readiness.
    """
    if force_refresh:
        _get_flat_package_status_cached.cache_clear()

    return _get_flat_package_status_cached(
        bool(deep_validation)
    )


def get_flat_package_status_cache_info() -> dict[str, Any]:
    """
    Gibt den Status des Package-Diagnosecaches zurück.
    """
    try:
        info = _get_flat_package_status_cached.cache_info()

        return {
            "hits": info.hits,
            "misses": info.misses,
            "maxSize": info.maxsize,
            "currentSize": info.currsize,
        }
    except Exception:
        return {
            "hits": 0,
            "misses": 0,
            "maxSize": 2,
            "currentSize": 0,
        }


def is_flat_package_ready(
    *,
    deep_validation: bool = False,
    force_refresh: bool = False,
) -> bool:
    """
    Gibt zurück, ob das Flat-Package bereit ist.
    """
    try:
        return get_flat_package_status(
            deep_validation=deep_validation,
            force_refresh=force_refresh,
        ).ready
    except Exception:
        return False


def require_flat_package_ready(
    *,
    deep_validation: bool = False,
    force_refresh: bool = False,
) -> None:
    """
    Erzwingt einen strukturell oder vollständig bereiten Packagezustand.
    """
    status = get_flat_package_status(
        deep_validation=deep_validation,
        force_refresh=force_refresh,
    )

    if status.ready:
        return

    raise RuntimeError(
        "VECTOPLAN Flat System Terrain World package is not ready. "
        f"Status: {status.to_dict()}"
    )


def get_flat_package_contract() -> dict[str, Any]:
    """
    Gibt den statischen Packagevertrag ohne Modulimport zurück.
    """
    return {
        "packageName": PACKAGE_NAME,
        "packageLabel": PACKAGE_LABEL,
        "packageVersion": PACKAGE_VERSION,
        "packageContractVersion": PACKAGE_CONTRACT_VERSION,
        "provider": {
            "providerId": PROVIDER_ID,
            "worldId": WORLD_ID,
            "worldType": WORLD_TYPE,
            "label": PROVIDER_LABEL,
            "version": PROVIDER_VERSION,
            "contractVersion": PROVIDER_CONTRACT_VERSION,
        },
        "generator": {
            "type": GENERATOR_TYPE,
            "version": GENERATOR_VERSION,
            "implementationVersion": (
                GENERATOR_IMPLEMENTATION_VERSION
            ),
            "ruleVersion": GENERATION_RULE_VERSION,
            "contractVersion": GENERATION_CONTRACT_VERSION,
        },
        "validator": {
            "version": VALIDATOR_VERSION,
            "contractVersion": VALIDATION_CONTRACT_VERSION,
        },
        "world": {
            "configFilename": CONFIG_FILENAME,
            "configFileIdentity": CONFIG_FILE_IDENTITY,
            "coordinateSystem": COORDINATE_SYSTEM,
            "projectionType": PROJECTION_TYPE,
            "topologyType": TOPOLOGY_TYPE,
        },
        "systemBlocks": {
            "air": {
                "systemBlockId": SYSTEM_AIR_BLOCK_ID,
                "runtimeBlockTypeId": None,
                "cellValue": AIR_CELL_VALUE,
                "persistAsBlockType": False,
                "storedInPositivePalette": False,
            },
            "terrain": {
                "systemBlockId": SYSTEM_TERRAIN_BLOCK_TYPE_ID,
                "runtimeBlockTypeId": (
                    SYSTEM_TERRAIN_BLOCK_TYPE_ID
                ),
                "cellValueRule": TERRAIN_CELL_VALUE_RULE,
                "persistAsBlockType": True,
                "storedInPositivePalette": True,
            },
        },
        "expectedFiles": list(EXPECTED_FILES),
        "expectedModules": list(EXPECTED_MODULES),
        "expectedModuleVersions": dict(
            EXPECTED_MODULE_VERSIONS
        ),
        "importPolicy": {
            "lazySymbols": True,
            "readConfigAtImport": False,
            "generateChunksAtImport": False,
            "deepValidateAtImport": False,
        },
    }


# ---------------------------------------------------------------------------
# Lazy symbol loading
# ---------------------------------------------------------------------------

def get_public_symbol_map() -> dict[str, tuple[str, str]]:
    """
    Gibt eine Kopie der Lazy-Import-Symboltabelle zurück.
    """
    return dict(_PUBLIC_SYMBOLS)


@lru_cache(maxsize=LAZY_SYMBOL_CACHE_SIZE)
def _load_public_symbol(symbol_name: str) -> Any:
    """
    Lädt ein öffentliches Symbol erst bei tatsächlicher Verwendung.
    """
    if symbol_name not in _PUBLIC_SYMBOLS:
        raise AttributeError(
            f"module '{PACKAGE_NAME}' has no attribute '{symbol_name}'"
        )

    module_path, attribute_name = _PUBLIC_SYMBOLS[symbol_name]

    try:
        module = import_module(module_path)
    except ModuleNotFoundError as exc:
        raise ImportError(
            f"Could not import '{symbol_name}' from '{module_path}'. "
            "The target module does not exist or is not importable."
        ) from exc
    except Exception as exc:
        raise ImportError(
            f"Could not import module '{module_path}' while resolving "
            f"public symbol '{symbol_name}'."
        ) from exc

    try:
        value = getattr(module, attribute_name)
    except AttributeError as exc:
        raise ImportError(
            f"Module '{module_path}' does not expose expected attribute "
            f"'{attribute_name}' for public symbol '{symbol_name}'."
        ) from exc
    except Exception as exc:
        raise ImportError(
            f"Could not access attribute '{attribute_name}' from "
            f"module '{module_path}'."
        ) from exc

    return value


def get_lazy_symbol_cache_info() -> dict[str, Any]:
    """
    Gibt den Status des Lazy-Symbol-Caches zurück.
    """
    try:
        info = _load_public_symbol.cache_info()

        return {
            "hits": info.hits,
            "misses": info.misses,
            "maxSize": info.maxsize,
            "currentSize": info.currsize,
        }
    except Exception:
        return {
            "hits": 0,
            "misses": 0,
            "maxSize": LAZY_SYMBOL_CACHE_SIZE,
            "currentSize": 0,
        }


def reset_flat_package_caches(
    *,
    include_provider_caches: bool = True,
    strict: bool = False,
) -> dict[str, Any]:
    """
    Leert Package- und optional Provider-, Validator- und Generator-Caches.

    Das Package importiert Providercode nur, wenn include_provider_caches=True
    explizit angefordert wird.

    strict=False:
        Fehler werden im Ergebnis dokumentiert.

    strict=True:
        Nach allen Resetversuchen wird bei Fehlern RuntimeError ausgelöst.
    """
    errors: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    local_actions = (
        (
            "packageStatus",
            _get_flat_package_status_cached.cache_clear,
        ),
        (
            "lazySymbols",
            _load_public_symbol.cache_clear,
        ),
    )

    for action_name, action in local_actions:
        try:
            action()
            actions.append(
                {
                    "name": action_name,
                    "ok": True,
                }
            )
        except Exception as exc:
            issue = {
                "name": action_name,
                "ok": False,
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
            actions.append(issue)
            errors.append(issue)

    if include_provider_caches:
        try:
            provider_module = import_module(
                "src.world.flat.provider"
            )
            reset_action = getattr(
                provider_module,
                "reset_provider_caches",
                None,
            )

            if not callable(reset_action):
                raise AttributeError(
                    "src.world.flat.provider.reset_provider_caches "
                    "is missing or not callable."
                )

            reset_action()
            actions.append(
                {
                    "name": "providerAndDependencies",
                    "ok": True,
                }
            )
        except Exception as exc:
            issue = {
                "name": "providerAndDependencies",
                "ok": False,
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
            actions.append(issue)
            errors.append(issue)

    result = {
        "ok": not errors,
        "includeProviderCaches": include_provider_caches,
        "actions": actions,
        "errors": errors,
        "packageStatusCache": (
            get_flat_package_status_cache_info()
        ),
        "lazySymbolCache": get_lazy_symbol_cache_info(),
    }

    if errors and strict:
        raise RuntimeError(
            "One or more Flat package caches could not be reset. "
            f"Result: {result}"
        )

    return result


def __getattr__(name: str) -> Any:
    """
    Lazy-Import-Hook für öffentliche Flat-World-Symbole.
    """
    return _load_public_symbol(name)


def __dir__() -> list[str]:
    """
    Sorgt dafür, dass dir(src.world.flat) auch Lazy-Symbole anzeigt.
    """
    default_names = set(globals().keys())
    public_names = set(__all__)
    return sorted(default_names | public_names)
