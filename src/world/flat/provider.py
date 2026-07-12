# services/vectoplan-chunk/src/world/flat/provider.py
"""
VECTOPLAN Flat World Provider.

Diese Datei ist die stabile Provider-Grenze zwischen der neutralen World-Schicht
und der konkreten Standard-Flat-World.

Sie verbindet:

    src/world/flat/world.json
        -> deklarative Weltkonfiguration

    src/world/flat/validator.py
        -> strikte Air-/Terrain-Validierung

    src/world/flat/generator.py
        -> deterministische Chunk-Generierung

    src/world/loader.py
        -> neutrales Laden und Provider-Auflösung

    src/world/service.py
        -> neutrale WorldService-Fassade

Die Standard-Flat-World besteht fachlich ausschließlich aus:

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

Provider-Vertrag:

    get_provider_info() -> WorldProviderInfo

    get_default_config_path() -> Path

    load_world_config(
        config_path: str | Path | None = None
    ) -> dict[str, Any]

    validate_world_config(
        raw_config: Mapping[str, Any]
    ) -> dict[str, Any]

    create_world_definition(
        raw_config: Mapping[str, Any]
    ) -> WorldDefinition

    generate_chunk(
        world: WorldDefinition,
        request: ChunkRequest
    ) -> GeneratedChunk

Robustheitsregeln:

- striktes UTF-8 und striktes JSON
- maximale Dateigröße
- Config-Dateisignatur aus Pfad, Größe und mtime_ns
- dateisignaturbasiertes LRU-Caching ohne gemeinsam mutierbare Dicts
- validierte Configs werden als kanonischer JSON-Text gecacht
- WorldDefinition-Objekte werden pro Aufruf neu rekonstruiert
- Providergrenze prüft Air-/Terrain-Invarianten erneut
- Generatorergebnis wird vollständig gegen Welt und Request geprüft
- alle externen Fehler werden in stabile WorldError-Klassen übersetzt
- ein zentraler Cache-Reset leert Provider-, Validator- und Generator-Caches

Nicht Teil dieser Datei:

- keine Flask-Abhängigkeit
- keine SQLAlchemy-Abhängigkeit
- keine Datenbankabfragen
- keine Snapshot- oder Eventpersistenz
- keine Command-Ausführung
- keine Three.js-Objekte
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

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
        DEFAULT_AIR_CELL_VALUE,
        ChunkRequest,
        GeneratedChunk,
        WorldDefinition,
        WorldProviderInfo,
    )
    from src.world.flat.generator import (
        FLAT_GENERATION_CONTRACT_VERSION,
        FLAT_GENERATION_RULE_VERSION,
        FLAT_GENERATOR_VERSION,
        FlatWorldGenerator,
        generate_flat_chunk,
        get_default_flat_world_generator,
        get_default_flat_world_generator_cache_info,
        reset_default_flat_world_generator_cache,
    )
    from src.world.flat.validator import (
        EXPECTED_CELL_ENCODING_VERSION,
        EXPECTED_COORDINATE_SYSTEM,
        EXPECTED_GENERATOR_TYPE,
        EXPECTED_GENERATOR_VERSION,
        EXPECTED_LAYERS_VERSION,
        EXPECTED_PROJECTION_TYPE,
        EXPECTED_REQUIRED_SYSTEM_BLOCKS_VERSION,
        EXPECTED_TERRAIN_CELL_VALUE,
        EXPECTED_TOPOLOGY_TYPE,
        EXPECTED_WORLD_ID,
        EXPECTED_WORLD_LABEL,
        EXPECTED_WORLD_TYPE,
        FLAT_VALIDATION_CONTRACT_VERSION,
        FLAT_VALIDATOR_VERSION,
        SYSTEM_AIR_BLOCK_ID,
        SYSTEM_TERRAIN_BLOCK_TYPE_ID,
        clear_flat_validator_caches,
        create_validated_flat_world_definition,
        get_flat_layer_block_type_ids,
        get_flat_validation_summary,
        get_flat_validator_cache_info,
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

PROVIDER_LABEL: Final[str] = EXPECTED_WORLD_LABEL
PROVIDER_VERSION: Final[str] = "0.2.1"
PROVIDER_CONTRACT_VERSION: Final[str] = "flat-provider-contract.v2"

CONFIG_FILENAME: Final[str] = "world.json"
EXPECTED_CONFIG_FILE_IDENTITY: Final[str] = "src/world/flat/world.json"

PROVIDER_MODULE: Final[str] = "src.world.flat.provider"

MAX_CONFIG_FILE_BYTES: Final[int] = 10 * 1024 * 1024
CONFIG_READ_CACHE_SIZE: Final[int] = 32
VALIDATED_CONFIG_CACHE_SIZE: Final[int] = 64

CONFIG_HASH_ALGORITHM: Final[str] = "sha256"
CANONICAL_JSON_SEPARATORS: Final[tuple[str, str]] = (",", ":")

SUPPORTED_PROVIDER_FUNCTIONS: Final[tuple[str, ...]] = (
    "get_provider_info",
    "get_default_config_path",
    "load_world_config",
    "validate_world_config",
    "create_world_definition",
    "generate_chunk",
)

OPTIONAL_PROVIDER_FUNCTIONS: Final[tuple[str, ...]] = (
    "load_default_world_definition",
    "generate_default_chunk",
    "get_provider_status",
    "require_provider_ready",
    "get_provider_contract",
    "get_provider_cache_info",
    "reset_provider_caches",
)


# ---------------------------------------------------------------------------
# Immutable provider diagnostics
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlatConfigFileSignature:
    """
    Stabile Signatur einer konkreten world.json-Datei.

    Die Signatur verhindert veraltete Config-Cache-Einträge:
    Ändern sich Dateigröße oder mtime_ns, entsteht automatisch ein neuer Key.
    """

    path: str
    size_bytes: int
    modified_time_ns: int
    content_hash: str | None = None
    hash_algorithm: str = CONFIG_HASH_ALGORITHM

    @property
    def cache_key(self) -> tuple[str, int, int]:
        return (
            self.path,
            self.size_bytes,
            self.modified_time_ns,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sizeBytes": self.size_bytes,
            "modifiedTimeNs": self.modified_time_ns,
            "contentHash": self.content_hash,
            "hashAlgorithm": self.hash_algorithm,
            "cacheKey": list(self.cache_key),
        }


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
    provider_contract_version: str
    provider_module: str

    config_path: str
    config_exists: bool
    config_signature: dict[str, Any]

    supported_functions: tuple[str, ...]
    optional_functions: tuple[str, ...]

    validator_version: str
    validation_contract_version: str

    generator_type: str
    generator_version: str
    generator_implementation_version: str
    generation_rule_version: str
    generation_contract_version: str

    system_blocks_ready: bool
    config_valid: bool
    generator_ready: bool
    ready: bool

    errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    warnings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Gibt den Status als JSON-nahe Struktur zurück.
        """
        payload = asdict(self)
        payload["providerId"] = payload.pop("provider_id")
        payload["worldId"] = payload.pop("world_id")
        payload["worldType"] = payload.pop("world_type")
        payload["providerLabel"] = payload.pop("provider_label")
        payload["providerVersion"] = payload.pop("provider_version")
        payload["providerContractVersion"] = payload.pop(
            "provider_contract_version"
        )
        payload["providerModule"] = payload.pop("provider_module")
        payload["configPath"] = payload.pop("config_path")
        payload["configExists"] = payload.pop("config_exists")
        payload["configSignature"] = payload.pop("config_signature")
        payload["supportedFunctions"] = list(
            payload.pop("supported_functions")
        )
        payload["optionalFunctions"] = list(
            payload.pop("optional_functions")
        )
        payload["validatorVersion"] = payload.pop("validator_version")
        payload["validationContractVersion"] = payload.pop(
            "validation_contract_version"
        )
        payload["generatorType"] = payload.pop("generator_type")
        payload["generatorVersion"] = payload.pop("generator_version")
        payload["generatorImplementationVersion"] = payload.pop(
            "generator_implementation_version"
        )
        payload["generationRuleVersion"] = payload.pop(
            "generation_rule_version"
        )
        payload["generationContractVersion"] = payload.pop(
            "generation_contract_version"
        )
        payload["systemBlocksReady"] = payload.pop(
            "system_blocks_ready"
        )
        payload["configValid"] = payload.pop("config_valid")
        payload["generatorReady"] = payload.pop("generator_ready")
        return make_json_safe(payload)


# ---------------------------------------------------------------------------
# Defensive utility helpers
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


def _resolve_path(path: Path) -> Path:
    """
    Normalisiert einen Pfad robust zu einem absoluten Pfad.
    """
    try:
        return path.expanduser().resolve()
    except Exception:
        try:
            return path.expanduser()
        except Exception:
            return path


def _get_package_dir() -> Path:
    """
    Gibt den Ordner dieses Provider-Moduls zurück.
    """
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path.cwd() / "src" / "world" / "flat"


def _append_issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    message: str,
    **details: Any,
) -> None:
    """
    Ergänzt einen JSON-sicheren Diagnoseeintrag.
    """
    item: dict[str, Any] = {
        "code": code,
        "message": message,
    }

    for key, value in details.items():
        item[key] = make_json_safe(value)

    issues.append(item)


def _canonical_json(value: Mapping[str, Any]) -> str:
    """
    Serialisiert eine Mapping-Struktur deterministisch.
    """
    if not isinstance(value, Mapping):
        raise InvalidWorldDefinitionError(
            "Flat world config must be an object.",
            details={
                "configType": type(value).__name__,
                "config": make_json_safe(value),
            },
        )

    safe = make_json_safe(dict(value))

    if not isinstance(safe, Mapping):
        raise InvalidWorldDefinitionError(
            "Flat world config could not be normalized to an object.",
            details={
                "configType": type(value).__name__,
                "config": safe,
            },
        )

    try:
        return json.dumps(
            dict(safe),
            sort_keys=True,
            separators=CANONICAL_JSON_SEPARATORS,
            ensure_ascii=False,
        )
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Flat world config could not be canonicalized.",
            details={
                "configType": type(value).__name__,
            },
            cause=exc,
        ) from exc


def _hash_text(text: str) -> str:
    """
    Erzeugt einen stabilen SHA-256-Hash für normalisierten Text.

    Diese Funktion bleibt für kanonische JSON-Zwischenstufen zuständig.
    Dateisignaturen werden dagegen aus den tatsächlich gelesenen Rohbytes
    gebildet, damit CRLF und LF nicht unbemerkt gleichgesetzt werden.
    """
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Flat world config content hash could not be calculated.",
            details={
                "algorithm": CONFIG_HASH_ALGORITHM,
            },
            cause=exc,
        ) from exc


def _hash_bytes(data: bytes) -> str:
    """
    Erzeugt einen SHA-256-Hash über die unveränderten Dateibytes.
    """
    try:
        return hashlib.sha256(data).hexdigest()
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Flat world config byte hash could not be calculated.",
            details={
                "algorithm": CONFIG_HASH_ALGORITHM,
            },
            cause=exc,
        ) from exc


def _normalize_raw_config(
    raw_config: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Normalisiert rohe Config-Daten zu einem neuen Dictionary.
    """
    if not isinstance(raw_config, Mapping):
        raise InvalidWorldDefinitionError(
            "Flat world config must be an object.",
            details={
                "configType": type(raw_config).__name__,
                "config": make_json_safe(raw_config),
            },
        )

    try:
        # JSON-Roundtrip verhindert gemeinsam mutierbare verschachtelte
        # Strukturen aus Cache- oder Aufruferkontexten.
        return json.loads(_canonical_json(raw_config))
    except InvalidWorldDefinitionError:
        raise
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Flat world config could not be copied safely.",
            details={
                "configType": type(raw_config).__name__,
            },
            cause=exc,
        ) from exc


def _get_config_file_signature(
    path: Path,
    *,
    include_content_hash: bool = False,
) -> FlatConfigFileSignature:
    """
    Prüft eine Config-Datei und baut ihre Signatur.
    """
    resolved = _resolve_path(path)

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
        stat = resolved.stat()
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not inspect flat world config file.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc

    size = int(stat.st_size)
    modified_time_ns = int(
        getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    )

    if size <= 0:
        raise InvalidWorldConfigFileError(
            "Flat world config file is empty.",
            details={
                "configPath": str(resolved),
                "sizeBytes": size,
            },
        )

    if size > MAX_CONFIG_FILE_BYTES:
        raise InvalidWorldConfigFileError(
            "Flat world config file is too large.",
            details={
                "configPath": str(resolved),
                "sizeBytes": size,
                "maxBytes": MAX_CONFIG_FILE_BYTES,
            },
        )

    signature = FlatConfigFileSignature(
        path=str(resolved),
        size_bytes=size,
        modified_time_ns=modified_time_ns,
    )

    if not include_content_hash:
        return signature

    try:
        raw_bytes = _read_config_bytes_uncached(signature)
        content_hash = _hash_bytes(raw_bytes)
    except InvalidWorldConfigFileError:
        raise
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not hash flat world config file.",
            details={
                "configPath": str(resolved),
            },
            cause=exc,
        ) from exc

    return FlatConfigFileSignature(
        path=signature.path,
        size_bytes=signature.size_bytes,
        modified_time_ns=signature.modified_time_ns,
        content_hash=content_hash,
    )


def _parse_config_text(
    text: str,
    *,
    config_path: str,
) -> dict[str, Any]:
    """
    Parst striktes JSON und erzwingt ein Objekt als Root.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidWorldConfigFileError(
            "Flat world config file contains invalid JSON.",
            details={
                "configPath": config_path,
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
                "configPath": config_path,
            },
            cause=exc,
        ) from exc

    if not isinstance(parsed, Mapping):
        raise InvalidWorldConfigFileError(
            "Flat world config JSON root must be an object.",
            details={
                "configPath": config_path,
                "rootType": type(parsed).__name__,
            },
        )

    return dict(parsed)


def _stat_modified_time_ns(stat_result: Any) -> int:
    """
    Liefert mtime_ns portabel und ohne Gleitkommaabhängigkeit im Normalfall.
    """
    return int(
        getattr(
            stat_result,
            "st_mtime_ns",
            int(stat_result.st_mtime * 1_000_000_000),
        )
    )


def _raise_config_changed_during_read(
    signature: FlatConfigFileSignature,
    *,
    reason: str,
    actual_size_bytes: int | None = None,
    actual_modified_time_ns: int | None = None,
    before_size_bytes: int | None = None,
    before_modified_time_ns: int | None = None,
) -> None:
    """
    Erzeugt einen einheitlichen Fehler für echte Signaturänderungen.
    """
    raise InvalidWorldConfigFileError(
        "Flat world config file changed while it was being read.",
        details={
            "configPath": signature.path,
            "expectedSizeBytes": signature.size_bytes,
            "actualSizeBytes": actual_size_bytes,
            "expectedModifiedTimeNs": signature.modified_time_ns,
            "actualModifiedTimeNs": actual_modified_time_ns,
            "beforeSizeBytes": before_size_bytes,
            "beforeModifiedTimeNs": before_modified_time_ns,
            "reason": reason,
        },
    )


def _read_config_bytes_uncached(
    signature: FlatConfigFileSignature,
) -> bytes:
    """
    Liest world.json als Rohbytes und erkennt echte Änderungen atomar genug.

    Wichtig: Textmodus darf hier nicht verwendet werden. Python normalisiert bei
    ``read_text()`` plattformunabhängig CRLF zu LF. Ein anschließender Vergleich
    der erneut kodierten Textlänge mit ``stat().st_size`` erzeugt deshalb einen
    falschen Änderungsalarm für gültige Windows-Dateien.
    """
    path = Path(signature.path)

    try:
        stat_before = path.stat()
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not inspect flat world config file before reading.",
            details={
                "configPath": signature.path,
            },
            cause=exc,
        ) from exc

    before_size = int(stat_before.st_size)
    before_modified_time_ns = _stat_modified_time_ns(stat_before)

    if (
        before_size != signature.size_bytes
        or before_modified_time_ns != signature.modified_time_ns
    ):
        _raise_config_changed_during_read(
            signature,
            reason="config_signature_changed_before_read",
            actual_size_bytes=before_size,
            actual_modified_time_ns=before_modified_time_ns,
            before_size_bytes=before_size,
            before_modified_time_ns=before_modified_time_ns,
        )

    try:
        raw_bytes = path.read_bytes()
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not read flat world config file.",
            details={
                "configPath": signature.path,
            },
            cause=exc,
        ) from exc

    try:
        stat_after = path.stat()
    except Exception as exc:
        raise InvalidWorldConfigFileError(
            "Could not inspect flat world config file after reading.",
            details={
                "configPath": signature.path,
            },
            cause=exc,
        ) from exc

    actual_size = len(raw_bytes)
    after_size = int(stat_after.st_size)
    after_modified_time_ns = _stat_modified_time_ns(stat_after)

    if (
        actual_size != before_size
        or after_size != before_size
        or after_modified_time_ns != before_modified_time_ns
    ):
        _raise_config_changed_during_read(
            signature,
            reason="config_changed_during_read",
            actual_size_bytes=actual_size,
            actual_modified_time_ns=after_modified_time_ns,
            before_size_bytes=before_size,
            before_modified_time_ns=before_modified_time_ns,
        )

    if actual_size <= 0:
        raise InvalidWorldConfigFileError(
            "Flat world config file is empty.",
            details={
                "configPath": signature.path,
                "sizeBytes": actual_size,
            },
        )

    if actual_size > MAX_CONFIG_FILE_BYTES:
        raise InvalidWorldConfigFileError(
            "Flat world config file is too large.",
            details={
                "configPath": signature.path,
                "sizeBytes": actual_size,
                "maxBytes": MAX_CONFIG_FILE_BYTES,
            },
        )

    return raw_bytes


def _read_config_text_uncached(
    signature: FlatConfigFileSignature,
) -> str:
    """
    Liest eine bereits geprüfte Config-Datei ohne Newline-Normalisierung.
    """
    raw_bytes = _read_config_bytes_uncached(signature)

    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidWorldConfigFileError(
            "Flat world config file must be UTF-8 encoded.",
            details={
                "configPath": signature.path,
            },
            cause=exc,
        ) from exc


@lru_cache(maxsize=CONFIG_READ_CACHE_SIZE)
def _read_config_text_cached(
    path: str,
    size_bytes: int,
    modified_time_ns: int,
) -> str:
    """
    Dateisignaturbasierter LRU-Cache für unveränderlichen JSON-Text.
    """
    signature = FlatConfigFileSignature(
        path=path,
        size_bytes=size_bytes,
        modified_time_ns=modified_time_ns,
    )
    return _read_config_text_uncached(signature)


def _validate_config_file_identity(
    raw_config: Mapping[str, Any],
    *,
    config_path: str,
) -> None:
    """
    Prüft das dokumentarische _file-Feld, ohne echte Deploymentpfade zu erzwingen.
    """
    declared_file = _safe_str(raw_config.get("_file"))

    if not declared_file:
        raise InvalidWorldConfigFileError(
            "Flat world config requires the '_file' identity field.",
            details={
                "configPath": config_path,
                "expectedFileIdentity": EXPECTED_CONFIG_FILE_IDENTITY,
            },
        )

    normalized = declared_file.replace("\\", "/").lstrip("./")

    if normalized != EXPECTED_CONFIG_FILE_IDENTITY:
        raise InvalidWorldConfigFileError(
            "Flat world config '_file' identity is invalid.",
            details={
                "configPath": config_path,
                "actual": declared_file,
                "expected": EXPECTED_CONFIG_FILE_IDENTITY,
            },
        )


# ---------------------------------------------------------------------------
# Safe config and definition caches
# ---------------------------------------------------------------------------

@lru_cache(maxsize=VALIDATED_CONFIG_CACHE_SIZE)
def _validate_config_canonical_cached(
    canonical_raw_config: str,
) -> str:
    """
    Validiert kanonisches JSON und cached ausschließlich JSON-Text.
    """
    try:
        raw = json.loads(canonical_raw_config)
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Canonical flat config could not be parsed.",
            details={
                "configHash": _hash_text(canonical_raw_config),
            },
            cause=exc,
        ) from exc

    if not isinstance(raw, Mapping):
        raise InvalidWorldDefinitionError(
            "Canonical flat config root must be an object.",
            details={
                "configHash": _hash_text(canonical_raw_config),
                "rootType": type(raw).__name__,
            },
        )

    normalized = validate_flat_world_config(raw)

    try:
        return _canonical_json(normalized)
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Validated flat config could not be cached.",
            details={
                "configHash": _hash_text(canonical_raw_config),
            },
            cause=exc,
        ) from exc


@lru_cache(maxsize=VALIDATED_CONFIG_CACHE_SIZE)
def _definition_config_canonical_cached(
    canonical_validated_config: str,
) -> str:
    """
    Prüft, ob aus validierter Config eine gültige WorldDefinition entsteht.

    Gecacht wird weiterhin nur JSON-Text. Das konkrete WorldDefinition-Objekt
    wird pro Aufruf neu erzeugt, damit raw_config und Metadaten nicht gemeinsam
    mutiert werden können.
    """
    try:
        raw = json.loads(canonical_validated_config)
    except Exception as exc:
        raise InvalidWorldDefinitionError(
            "Validated flat config cache could not be parsed.",
            details={
                "configHash": _hash_text(canonical_validated_config),
            },
            cause=exc,
        ) from exc

    definition = create_validated_flat_world_definition(raw)
    definition.validate()
    _validate_world_matches_provider(definition)

    return canonical_validated_config


# ---------------------------------------------------------------------------
# Provider/world contract validation
# ---------------------------------------------------------------------------

def _validate_world_matches_provider(
    world: WorldDefinition,
) -> None:
    """
    Prüft, ob eine WorldDefinition vollständig zum Flat-Provider passt.
    """
    if not isinstance(world, WorldDefinition):
        raise InvalidWorldDefinitionError(
            "Flat provider requires WorldDefinition.",
            details={
                "worldType": type(world).__name__,
            },
        )

    errors: list[dict[str, Any]] = []

    try:
        world.validate()
    except Exception as exc:
        _append_issue(
            errors,
            code="world_definition_validation_failed",
            message="WorldDefinition failed general validation.",
            errorType=type(exc).__name__,
            error=str(exc),
        )

    expected_values = {
        "worldId": (world.world_id, WORLD_ID),
        "worldType": (world.world_type, WORLD_TYPE),
        "generatorType": (
            world.generator_type,
            EXPECTED_GENERATOR_TYPE,
        ),
        "generatorVersion": (
            world.generator_version,
            EXPECTED_GENERATOR_VERSION,
        ),
        "coordinateSystem": (
            world.coordinate_system,
            EXPECTED_COORDINATE_SYSTEM,
        ),
        "projectionType": (
            world.projection_type,
            EXPECTED_PROJECTION_TYPE,
        ),
        "topologyType": (
            world.topology_type,
            EXPECTED_TOPOLOGY_TYPE,
        ),
    }

    for field_name, (actual, expected) in expected_values.items():
        if actual != expected:
            _append_issue(
                errors,
                code=f"{field_name}_mismatch",
                message=(
                    f"WorldDefinition {field_name} does not match "
                    "the Flat provider contract."
                ),
                field=field_name,
                actual=actual,
                expected=expected,
            )

    palette_ids = tuple(world.palette_block_type_ids)

    if palette_ids != (SYSTEM_TERRAIN_BLOCK_TYPE_ID,):
        _append_issue(
            errors,
            code="flat_palette_mismatch",
            message=(
                "Flat provider requires exactly one positive palette entry: "
                "system_terrain."
            ),
            actual=palette_ids,
            expected=(SYSTEM_TERRAIN_BLOCK_TYPE_ID,),
        )

    if SYSTEM_AIR_BLOCK_ID in palette_ids:
        _append_issue(
            errors,
            code="system_air_in_positive_palette",
            message="system_air must not appear in the positive palette.",
            paletteBlockTypeIds=palette_ids,
        )

    try:
        terrain_cell_value = world.get_cell_value_for_block_type(
            SYSTEM_TERRAIN_BLOCK_TYPE_ID
        )
    except Exception as exc:
        terrain_cell_value = None
        _append_issue(
            errors,
            code="terrain_cell_value_resolution_failed",
            message="Could not resolve system_terrain cellValue.",
            errorType=type(exc).__name__,
            error=str(exc),
        )

    if terrain_cell_value != EXPECTED_TERRAIN_CELL_VALUE:
        _append_issue(
            errors,
            code="terrain_cell_value_mismatch",
            message=(
                "Canonical Flat palette must resolve system_terrain "
                "to cellValue 1."
            ),
            actual=terrain_cell_value,
            expected=EXPECTED_TERRAIN_CELL_VALUE,
        )

    raw_config = (
        dict(world.raw_config)
        if isinstance(world.raw_config, Mapping)
        else {}
    )

    if not raw_config:
        _append_issue(
            errors,
            code="missing_world_raw_config",
            message=(
                "Flat WorldDefinition requires raw_config for provider "
                "contract verification."
            ),
        )
    else:
        try:
            validate_flat_world_config(raw_config)
        except Exception as exc:
            _append_issue(
                errors,
                code="world_raw_config_validation_failed",
                message=(
                    "WorldDefinition.raw_config violates the canonical "
                    "Flat Air/Terrain contract."
                ),
                errorType=type(exc).__name__,
                error=str(exc),
            )

        try:
            surface_id, subsurface_id = get_flat_layer_block_type_ids(
                raw_config
            )
        except Exception as exc:
            surface_id = ""
            subsurface_id = ""
            _append_issue(
                errors,
                code="layer_resolution_failed",
                message="Could not resolve Flat layer block types.",
                errorType=type(exc).__name__,
                error=str(exc),
            )

        for field_name, actual in (
            ("surfaceBlockTypeId", surface_id),
            ("subsurfaceBlockTypeId", subsurface_id),
        ):
            if actual != SYSTEM_TERRAIN_BLOCK_TYPE_ID:
                _append_issue(
                    errors,
                    code="flat_layer_not_system_terrain",
                    message=(
                        f"{field_name} must resolve to system_terrain."
                    ),
                    field=field_name,
                    actual=actual,
                    expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
                )

        cell_encoding = raw_config.get("cellEncoding", {})

        if isinstance(cell_encoding, Mapping):
            air_value = cell_encoding.get(
                "airCellValue",
                DEFAULT_AIR_CELL_VALUE,
            )

            try:
                normalized_air_value = int(air_value)
            except Exception:
                normalized_air_value = None

            if normalized_air_value != DEFAULT_AIR_CELL_VALUE:
                _append_issue(
                    errors,
                    code="air_cell_value_mismatch",
                    message="system_air must use cellValue 0.",
                    actual=normalized_air_value,
                    expected=DEFAULT_AIR_CELL_VALUE,
                )

    if errors:
        raise InvalidWorldDefinitionError(
            "WorldDefinition does not match Flat provider.",
            details={
                "providerId": PROVIDER_ID,
                "worldId": getattr(world, "world_id", None),
                "errors": errors,
            },
        )


def _ensure_world_definition(value: Any) -> WorldDefinition:
    """
    Erzwingt eine vollständig validierte WorldDefinition.
    """
    if isinstance(value, WorldDefinition):
        _validate_world_matches_provider(value)
        return value

    if isinstance(value, Mapping):
        return create_world_definition(value)

    raise WorldProviderContractError(
        "Flat provider expected WorldDefinition or mapping.",
        details={
            "valueType": type(value).__name__,
            "value": make_json_safe(value),
        },
    )


def _ensure_chunk_request(value: Any) -> ChunkRequest:
    """
    Erzwingt eine validierte ChunkRequest.
    """
    if isinstance(value, ChunkRequest):
        try:
            value.validate()
            return value
        except Exception as exc:
            raise WorldProviderContractError(
                "Flat provider received invalid ChunkRequest.",
                details={
                    "request": make_json_safe(value),
                },
                cause=exc,
            ) from exc

    if isinstance(value, Mapping):
        try:
            request = ChunkRequest.from_dict(value)
            request.validate()
            return request
        except Exception as exc:
            raise WorldProviderContractError(
                "Flat provider could not normalize ChunkRequest mapping.",
                details={
                    "request": make_json_safe(value),
                },
                cause=exc,
            ) from exc

    raise WorldProviderContractError(
        "Flat provider expected ChunkRequest or mapping.",
        details={
            "valueType": type(value).__name__,
            "value": make_json_safe(value),
        },
    )


def _validate_generated_chunk_contract(
    chunk: GeneratedChunk,
    *,
    world: WorldDefinition,
    request: ChunkRequest,
) -> None:
    """
    Prüft das Generatorergebnis an der Providergrenze.
    """
    if not isinstance(chunk, GeneratedChunk):
        raise WorldProviderContractError(
            "Flat generator returned invalid result.",
            details={
                "returnType": type(chunk).__name__,
                "returnValue": make_json_safe(chunk),
            },
        )

    errors: list[dict[str, Any]] = []

    try:
        chunk.validate()
    except Exception as exc:
        _append_issue(
            errors,
            code="generated_chunk_validation_failed",
            message="GeneratedChunk failed structural validation.",
            errorType=type(exc).__name__,
            error=str(exc),
        )

    comparisons = {
        "worldId": (chunk.world_id, world.world_id),
        "chunkX": (chunk.chunk_x, request.chunk_x),
        "chunkY": (chunk.chunk_y, request.chunk_y),
        "chunkZ": (chunk.chunk_z, request.chunk_z),
        "chunkSize": (chunk.chunk_size, world.chunk_size),
        "cellSize": (chunk.cell_size, world.cell_size),
        "generatorType": (
            chunk.generator_type,
            world.generator_type,
        ),
        "generatorVersion": (
            chunk.generator_version,
            world.generator_version,
        ),
        "blockRegistryId": (
            chunk.block_registry_id,
            world.block_registry_id,
        ),
        "blockRegistryVersion": (
            chunk.block_registry_version,
            world.block_registry_version,
        ),
    }

    for field_name, (actual, expected) in comparisons.items():
        if actual != expected:
            _append_issue(
                errors,
                code=f"generated_chunk_{field_name}_mismatch",
                message=(
                    f"GeneratedChunk {field_name} does not match "
                    "world or request."
                ),
                field=field_name,
                actual=actual,
                expected=expected,
            )

    if tuple(entry.block_type_id for entry in chunk.palette) != (
        SYSTEM_TERRAIN_BLOCK_TYPE_ID,
    ):
        _append_issue(
            errors,
            code="generated_chunk_palette_mismatch",
            message=(
                "GeneratedChunk palette must contain only system_terrain."
            ),
            actual=tuple(
                entry.block_type_id for entry in chunk.palette
            ),
            expected=(SYSTEM_TERRAIN_BLOCK_TYPE_ID,),
        )

    allowed_values = {
        DEFAULT_AIR_CELL_VALUE,
        EXPECTED_TERRAIN_CELL_VALUE,
    }

    unexpected_values = sorted(
        set(chunk.cells) - allowed_values
    )

    if unexpected_values:
        _append_issue(
            errors,
            code="generated_chunk_unexpected_cell_values",
            message=(
                "Generated Flat chunk contains values outside Air/Terrain."
            ),
            unexpectedCellValues=unexpected_values,
            allowedCellValues=sorted(allowed_values),
        )

    if chunk.air_cell_value != DEFAULT_AIR_CELL_VALUE:
        _append_issue(
            errors,
            code="generated_chunk_air_value_mismatch",
            message="GeneratedChunk Air value must be 0.",
            actual=chunk.air_cell_value,
            expected=DEFAULT_AIR_CELL_VALUE,
        )

    if errors:
        raise WorldGenerationError(
            "Generated chunk violates Flat provider contract.",
            details={
                "providerId": PROVIDER_ID,
                "worldId": world.world_id,
                "chunkKey": request.chunk_key,
                "errors": errors,
            },
        )


# ---------------------------------------------------------------------------
# Provider contract functions
# ---------------------------------------------------------------------------

def get_default_config_path() -> Path:
    """
    Gibt den Standardpfad zu src/world/flat/world.json zurück.
    """
    return _get_package_dir() / CONFIG_FILENAME


def get_provider_info() -> WorldProviderInfo:
    """
    Gibt öffentliche Provider-Informationen zurück.
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
            "providerContractVersion": PROVIDER_CONTRACT_VERSION,
            "generatorType": EXPECTED_GENERATOR_TYPE,
            "generatorVersion": EXPECTED_GENERATOR_VERSION,
            "generatorImplementationVersion": FLAT_GENERATOR_VERSION,
            "generationRuleVersion": FLAT_GENERATION_RULE_VERSION,
            "generationContractVersion": (
                FLAT_GENERATION_CONTRACT_VERSION
            ),
            "validatorVersion": FLAT_VALIDATOR_VERSION,
            "validationContractVersion": (
                FLAT_VALIDATION_CONTRACT_VERSION
            ),
            "projectionType": EXPECTED_PROJECTION_TYPE,
            "topologyType": EXPECTED_TOPOLOGY_TYPE,
            "coordinateSystem": EXPECTED_COORDINATE_SYSTEM,
            "systemBlocks": {
                "air": {
                    "systemBlockId": SYSTEM_AIR_BLOCK_ID,
                    "runtimeBlockTypeId": None,
                    "cellValue": DEFAULT_AIR_CELL_VALUE,
                    "storedInPositivePalette": False,
                },
                "terrain": {
                    "systemBlockId": (
                        SYSTEM_TERRAIN_BLOCK_TYPE_ID
                    ),
                    "runtimeBlockTypeId": (
                        SYSTEM_TERRAIN_BLOCK_TYPE_ID
                    ),
                    "cellValueRule": "paletteIndex + 1",
                    "storedInPositivePalette": True,
                },
            },
            "description": (
                "Canonical deterministic Flat provider generated from "
                "immutable system_air and system_terrain definitions."
            ),
        },
    )


def load_world_config(
    config_path: str | Path | None = None,
    *,
    use_cache: bool = True,
    validate_file_identity: bool = True,
) -> dict[str, Any]:
    """
    Lädt world.json robust und gibt immer ein neu rekonstruiertes Dictionary zurück.

    Cache:
    - basiert auf absolutem Pfad, Dateigröße und mtime_ns
    - gecacht wird nur unveränderlicher Text
    - zurückgegebene Dictionaries werden nie gemeinsam verwendet
    """
    try:
        path = _as_path(config_path) or get_default_config_path()
        signature = _get_config_file_signature(path)

        if use_cache:
            text = _read_config_text_cached(
                signature.path,
                signature.size_bytes,
                signature.modified_time_ns,
            )
        else:
            text = _read_config_text_uncached(signature)

        raw_config = _parse_config_text(
            text,
            config_path=signature.path,
        )

        if validate_file_identity:
            _validate_config_file_identity(
                raw_config,
                config_path=signature.path,
            )

        return _normalize_raw_config(raw_config)

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="Flat provider could not load world config.",
            fallback_code="flat_world_config_load_failed",
            fallback_status_code=500,
            details={
                "providerId": PROVIDER_ID,
                "configPath": (
                    str(config_path)
                    if config_path is not None
                    else str(get_default_config_path())
                ),
                "useCache": bool(use_cache),
            },
        )
        raise world_error from exc


def validate_world_config(
    raw_config: Mapping[str, Any],
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """
    Validiert und normalisiert eine Flat-World-Konfiguration.

    Providerseitig wird zusätzlich geprüft:
    - Provideridentität
    - Generatorversion
    - Air-/Terrain-Palette
    - Surface-/Subsurface-Zuordnung
    """
    try:
        canonical_raw = _canonical_json(raw_config)

        if use_cache:
            canonical_normalized = (
                _validate_config_canonical_cached(canonical_raw)
            )
            normalized = json.loads(canonical_normalized)
        else:
            normalized = validate_flat_world_config(
                json.loads(canonical_raw),
                use_cache=False,
            )

        if not isinstance(normalized, Mapping):
            raise InvalidWorldDefinitionError(
                "Flat validator returned invalid config root.",
                details={
                    "returnType": type(normalized).__name__,
                },
            )

        normalized_dict = dict(normalized)

        identity_values = {
            "worldId": (
                _safe_str(normalized_dict.get("worldId")),
                WORLD_ID,
            ),
            "worldType": (
                _safe_str(normalized_dict.get("worldType")),
                WORLD_TYPE,
            ),
            "generatorType": (
                _safe_str(normalized_dict.get("generatorType")),
                EXPECTED_GENERATOR_TYPE,
            ),
            "generatorVersion": (
                _safe_str(normalized_dict.get("generatorVersion")),
                EXPECTED_GENERATOR_VERSION,
            ),
        }

        errors: list[dict[str, Any]] = []

        for field_name, (actual, expected) in identity_values.items():
            if actual != expected:
                _append_issue(
                    errors,
                    code=f"provider_{field_name}_mismatch",
                    message=(
                        f"Flat config {field_name} does not match "
                        "provider contract."
                    ),
                    field=field_name,
                    actual=actual,
                    expected=expected,
                )

        palette = normalized_dict.get("palette", [])

        if not isinstance(palette, list):
            _append_issue(
                errors,
                code="provider_palette_not_array",
                message="Flat config palette must be an array.",
                actualType=type(palette).__name__,
            )
        else:
            palette_ids = tuple(
                _safe_str(
                    entry.get("blockTypeId")
                    if isinstance(entry, Mapping)
                    else None
                )
                for entry in palette
            )

            if palette_ids != (SYSTEM_TERRAIN_BLOCK_TYPE_ID,):
                _append_issue(
                    errors,
                    code="provider_palette_contract_mismatch",
                    message=(
                        "Flat config palette must contain exactly "
                        "system_terrain."
                    ),
                    actual=palette_ids,
                    expected=(SYSTEM_TERRAIN_BLOCK_TYPE_ID,),
                )

        try:
            surface_id, subsurface_id = get_flat_layer_block_type_ids(
                normalized_dict
            )
        except Exception as exc:
            surface_id = ""
            subsurface_id = ""
            _append_issue(
                errors,
                code="provider_layer_resolution_failed",
                message="Could not resolve Flat layer IDs.",
                errorType=type(exc).__name__,
                error=str(exc),
            )

        if surface_id != SYSTEM_TERRAIN_BLOCK_TYPE_ID:
            _append_issue(
                errors,
                code="provider_surface_not_system_terrain",
                message="Flat surface must use system_terrain.",
                actual=surface_id,
                expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            )

        if subsurface_id != SYSTEM_TERRAIN_BLOCK_TYPE_ID:
            _append_issue(
                errors,
                code="provider_subsurface_not_system_terrain",
                message="Flat subsurface must use system_terrain.",
                actual=subsurface_id,
                expected=SYSTEM_TERRAIN_BLOCK_TYPE_ID,
            )

        if errors:
            raise InvalidWorldDefinitionError(
                "Flat provider config validation failed.",
                details={
                    "providerId": PROVIDER_ID,
                    "errors": errors,
                },
            )

        return _normalize_raw_config(normalized_dict)

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message="Flat provider config validation failed.",
            fallback_code="flat_world_config_validation_failed",
            fallback_status_code=400,
            details={
                "providerId": PROVIDER_ID,
                "config": make_json_safe(raw_config),
                "useCache": bool(use_cache),
            },
        )
        raise world_error from exc


def create_world_definition(
    raw_config: Mapping[str, Any],
    *,
    use_cache: bool = True,
) -> WorldDefinition:
    """
    Erstellt eine vollständig validierte, neue WorldDefinition.
    """
    try:
        normalized = validate_world_config(
            raw_config,
            use_cache=use_cache,
        )
        canonical = _canonical_json(normalized)

        if use_cache:
            canonical_definition_config = (
                _definition_config_canonical_cached(canonical)
            )
            definition_raw = json.loads(
                canonical_definition_config
            )
        else:
            definition_raw = json.loads(canonical)

        definition = create_validated_flat_world_definition(
            definition_raw,
            use_cache=use_cache,
        )
        definition.validate()
        _validate_world_matches_provider(definition)

        return definition

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message=(
                "Flat provider could not create world definition."
            ),
            fallback_code="flat_world_definition_create_failed",
            fallback_status_code=400,
            details={
                "providerId": PROVIDER_ID,
                "config": make_json_safe(raw_config),
                "useCache": bool(use_cache),
            },
        )
        raise world_error from exc


def generate_chunk(
    world: WorldDefinition,
    request: ChunkRequest,
) -> GeneratedChunk:
    """
    Generiert einen Flat-Chunk über den kanonischen FlatWorldGenerator.
    """
    try:
        normalized_world = _ensure_world_definition(world)
        normalized_request = _ensure_chunk_request(request)

        if (
            normalized_request.world_id
            != normalized_world.world_id
        ):
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

        _validate_generated_chunk_contract(
            chunk,
            world=normalized_world,
            request=normalized_request,
        )

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
                "request": (
                    request.to_dict(camel_case=True)
                    if isinstance(request, ChunkRequest)
                    else make_json_safe(request)
                ),
            },
        )
        raise world_error from exc


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def load_default_world_definition(
    *,
    config_path: str | Path | None = None,
    use_cache: bool = True,
) -> WorldDefinition:
    """
    Lädt die Standard-Flat-World und erzeugt eine neue WorldDefinition.
    """
    raw_config = load_world_config(
        config_path,
        use_cache=use_cache,
    )
    return create_world_definition(
        raw_config,
        use_cache=use_cache,
    )


def generate_default_chunk(
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    *,
    config_path: str | Path | None = None,
    generator: FlatWorldGenerator | None = None,
    use_cache: bool = True,
) -> GeneratedChunk:
    """
    Lädt die Standardwelt und generiert direkt einen Chunk.
    """
    try:
        world = load_default_world_definition(
            config_path=config_path,
            use_cache=use_cache,
        )
        request = ChunkRequest.create(
            world_id=world.world_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            metadata={
                "source": "generate_default_chunk",
                "providerId": PROVIDER_ID,
                "providerVersion": PROVIDER_VERSION,
            },
        )

        active_generator = (
            generator or get_default_flat_world_generator()
        )

        if not isinstance(active_generator, FlatWorldGenerator):
            raise WorldProviderContractError(
                "generator must be FlatWorldGenerator.",
                details={
                    "generatorType": type(active_generator).__name__,
                },
            )

        chunk = active_generator.generate_chunk(world, request)

        _validate_generated_chunk_contract(
            chunk,
            world=world,
            request=request,
        )

        return chunk

    except Exception as exc:
        world_error = coerce_world_error(
            exc,
            fallback_message=(
                "Flat provider could not generate default chunk."
            ),
            fallback_code="flat_default_chunk_generation_failed",
            fallback_status_code=500,
            details={
                "providerId": PROVIDER_ID,
                "chunkX": chunk_x,
                "chunkY": chunk_y,
                "chunkZ": chunk_z,
                "useCache": bool(use_cache),
            },
        )
        raise world_error from exc


# ---------------------------------------------------------------------------
# Provider status and contract
# ---------------------------------------------------------------------------

def get_provider_cache_info() -> dict[str, Any]:
    """
    Gibt den Status aller Provider-nahen Caches zurück.
    """
    def cache_info_dict(function: Any) -> dict[str, Any]:
        try:
            info = function.cache_info()
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
                "maxSize": None,
                "currentSize": 0,
            }

    return {
        "configRead": cache_info_dict(_read_config_text_cached),
        "validatedConfig": cache_info_dict(
            _validate_config_canonical_cached
        ),
        "definitionConfig": cache_info_dict(
            _definition_config_canonical_cached
        ),
        "validator": get_flat_validator_cache_info(),
        "generator": get_default_flat_world_generator_cache_info(),
    }


def get_provider_status(
    *,
    include_config_validation: bool = True,
    include_content_hash: bool = False,
    force_refresh: bool = False,
) -> FlatProviderStatus:
    """
    Gibt einen vollständigen Diagnosezustand des Providers zurück.
    """
    if force_refresh:
        reset_provider_caches()

    config_path = get_default_config_path()
    resolved_path = _resolve_path(config_path)

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    config_exists = resolved_path.is_file()
    config_signature: dict[str, Any] = {}

    config_valid = False
    system_blocks_ready = False
    generator_ready = False

    metadata: dict[str, Any] = {
        "packageDir": str(_get_package_dir()),
        "expectedConfigFileIdentity": EXPECTED_CONFIG_FILE_IDENTITY,
        "cache": get_provider_cache_info(),
    }

    if config_exists:
        try:
            signature = _get_config_file_signature(
                resolved_path,
                include_content_hash=include_content_hash,
            )
            config_signature = signature.to_dict()
        except Exception as exc:
            _append_issue(
                errors,
                code="config_signature_failed",
                message="Could not inspect Flat config signature.",
                errorType=type(exc).__name__,
                error=str(exc),
            )
    else:
        _append_issue(
            errors,
            code="config_file_missing",
            message="Flat world config file does not exist.",
            configPath=str(resolved_path),
        )

    if include_config_validation and config_exists:
        try:
            raw_config = load_world_config(
                resolved_path,
                use_cache=not force_refresh,
            )
            summary = get_flat_validation_summary(
                raw_config,
                use_cache=not force_refresh,
            )
            metadata["configValidation"] = summary
            config_valid = bool(summary.get("ok"))

            system_blocks_ready = bool(
                summary.get("airSystemBlockId")
                == SYSTEM_AIR_BLOCK_ID
                and summary.get("airCellValue")
                == DEFAULT_AIR_CELL_VALUE
                and summary.get("terrainSystemBlockId")
                == SYSTEM_TERRAIN_BLOCK_TYPE_ID
                and summary.get("surfaceBlockTypeId")
                == SYSTEM_TERRAIN_BLOCK_TYPE_ID
                and summary.get("subsurfaceBlockTypeId")
                == SYSTEM_TERRAIN_BLOCK_TYPE_ID
                and summary.get("paletteBlockTypeIds")
                == [SYSTEM_TERRAIN_BLOCK_TYPE_ID]
            )

            if not system_blocks_ready:
                _append_issue(
                    errors,
                    code="system_block_contract_not_ready",
                    message=(
                        "Flat config does not expose the canonical "
                        "system_air/system_terrain contract."
                    ),
                    summary=summary,
                )

            definition = create_world_definition(
                raw_config,
                use_cache=not force_refresh,
            )
            metadata["world"] = {
                "worldId": definition.world_id,
                "worldType": definition.world_type,
                "generatorType": definition.generator_type,
                "generatorVersion": definition.generator_version,
                "paletteBlockTypeIds": list(
                    definition.palette_block_type_ids
                ),
            }

        except Exception as exc:
            _append_issue(
                errors,
                code="config_validation_failed",
                message="Flat config validation failed.",
                errorType=type(exc).__name__,
                error=str(exc),
            )
            metadata["configValidation"] = {
                "ok": False,
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
    elif config_exists:
        config_valid = True
        system_blocks_ready = True
        _append_issue(
            warnings,
            code="config_validation_skipped",
            message=(
                "Provider status skipped deep config validation."
            ),
        )

    try:
        generator = get_default_flat_world_generator()
        generator_status = generator.get_status()
        metadata["generator"] = generator_status

        generator_ready = bool(
            generator_status.get("generatorVersion")
            == FLAT_GENERATOR_VERSION
            and generator_status.get("generationRuleVersion")
            == FLAT_GENERATION_RULE_VERSION
            and generator_status.get("generationContractVersion")
            == FLAT_GENERATION_CONTRACT_VERSION
            and generator_status.get("systemBlocks", {})
            .get("air", {})
            .get("systemBlockId")
            == SYSTEM_AIR_BLOCK_ID
            and generator_status.get("systemBlocks", {})
            .get("terrain", {})
            .get("systemBlockId")
            == SYSTEM_TERRAIN_BLOCK_TYPE_ID
        )

        if not generator_ready:
            _append_issue(
                errors,
                code="generator_not_ready",
                message=(
                    "Flat generator status does not match provider "
                    "Air/Terrain contract."
                ),
                generatorStatus=generator_status,
            )

    except Exception as exc:
        _append_issue(
            errors,
            code="generator_status_failed",
            message="Could not inspect Flat generator.",
            errorType=type(exc).__name__,
            error=str(exc),
        )

    metadata["cache"] = get_provider_cache_info()

    ready = bool(
        config_exists
        and config_valid
        and system_blocks_ready
        and generator_ready
        and not errors
    )

    return FlatProviderStatus(
        provider_id=PROVIDER_ID,
        world_id=WORLD_ID,
        world_type=WORLD_TYPE,
        provider_label=PROVIDER_LABEL,
        provider_version=PROVIDER_VERSION,
        provider_contract_version=PROVIDER_CONTRACT_VERSION,
        provider_module=PROVIDER_MODULE,
        config_path=str(resolved_path),
        config_exists=config_exists,
        config_signature=config_signature,
        supported_functions=SUPPORTED_PROVIDER_FUNCTIONS,
        optional_functions=OPTIONAL_PROVIDER_FUNCTIONS,
        validator_version=FLAT_VALIDATOR_VERSION,
        validation_contract_version=(
            FLAT_VALIDATION_CONTRACT_VERSION
        ),
        generator_type=EXPECTED_GENERATOR_TYPE,
        generator_version=EXPECTED_GENERATOR_VERSION,
        generator_implementation_version=FLAT_GENERATOR_VERSION,
        generation_rule_version=FLAT_GENERATION_RULE_VERSION,
        generation_contract_version=(
            FLAT_GENERATION_CONTRACT_VERSION
        ),
        system_blocks_ready=system_blocks_ready,
        config_valid=config_valid,
        generator_ready=generator_ready,
        ready=ready,
        errors=tuple(errors),
        warnings=tuple(warnings),
        metadata=metadata,
    )


def require_provider_ready() -> None:
    """
    Erzwingt, dass Config, Validator, Systemblöcke und Generator bereit sind.
    """
    status = get_provider_status(
        include_config_validation=True,
        include_content_hash=False,
        force_refresh=False,
    )

    if status.ready:
        return

    raise WorldProviderError(
        "Flat Air/Terrain provider is not ready.",
        details=status.to_dict(),
    )


def get_provider_contract() -> dict[str, Any]:
    """
    Gibt den vollständigen Providervertrag als JSON-nahe Struktur zurück.
    """
    return {
        "providerId": PROVIDER_ID,
        "worldId": WORLD_ID,
        "worldType": WORLD_TYPE,
        "providerLabel": PROVIDER_LABEL,
        "providerModule": PROVIDER_MODULE,
        "providerVersion": PROVIDER_VERSION,
        "providerContractVersion": PROVIDER_CONTRACT_VERSION,
        "config": {
            "filename": CONFIG_FILENAME,
            "fileIdentity": EXPECTED_CONFIG_FILE_IDENTITY,
            "maxBytes": MAX_CONFIG_FILE_BYTES,
            "encoding": "UTF-8",
            "format": "strict-json-object",
        },
        "requiredFunctions": list(SUPPORTED_PROVIDER_FUNCTIONS),
        "optionalFunctions": list(OPTIONAL_PROVIDER_FUNCTIONS),
        "validator": {
            "version": FLAT_VALIDATOR_VERSION,
            "contractVersion": FLAT_VALIDATION_CONTRACT_VERSION,
            "layersVersion": EXPECTED_LAYERS_VERSION,
            "requiredSystemBlocksVersion": (
                EXPECTED_REQUIRED_SYSTEM_BLOCKS_VERSION
            ),
        },
        "generator": {
            "type": EXPECTED_GENERATOR_TYPE,
            "version": EXPECTED_GENERATOR_VERSION,
            "implementationVersion": FLAT_GENERATOR_VERSION,
            "ruleVersion": FLAT_GENERATION_RULE_VERSION,
            "contractVersion": FLAT_GENERATION_CONTRACT_VERSION,
        },
        "world": {
            "coordinateSystem": EXPECTED_COORDINATE_SYSTEM,
            "projectionType": EXPECTED_PROJECTION_TYPE,
            "topologyType": EXPECTED_TOPOLOGY_TYPE,
        },
        "cellEncoding": {
            "version": EXPECTED_CELL_ENCODING_VERSION,
            "airCellValue": DEFAULT_AIR_CELL_VALUE,
            "blockCellValueRule": "paletteIndex + 1",
        },
        "systemBlocks": {
            "air": {
                "systemBlockId": SYSTEM_AIR_BLOCK_ID,
                "runtimeBlockTypeId": None,
                "cellValue": DEFAULT_AIR_CELL_VALUE,
                "persistAsBlockType": False,
                "storedInPositivePalette": False,
            },
            "terrain": {
                "systemBlockId": SYSTEM_TERRAIN_BLOCK_TYPE_ID,
                "runtimeBlockTypeId": (
                    SYSTEM_TERRAIN_BLOCK_TYPE_ID
                ),
                "canonicalPaletteIndex": 0,
                "canonicalCellValue": EXPECTED_TERRAIN_CELL_VALUE,
                "cellValueRule": "paletteIndex + 1",
                "persistAsBlockType": True,
                "storedInPositivePalette": True,
            },
        },
        "cache": {
            "configReadMaxSize": CONFIG_READ_CACHE_SIZE,
            "validatedConfigMaxSize": (
                VALIDATED_CONFIG_CACHE_SIZE
            ),
            "mutableObjectsShared": False,
            "fileSignatureFields": [
                "absolutePath",
                "sizeBytes",
                "modifiedTimeNs",
            ],
        },
    }


# ---------------------------------------------------------------------------
# Cached helpers and cache reset
# ---------------------------------------------------------------------------

def get_cached_default_world_definition() -> WorldDefinition:
    """
    Gibt eine neu rekonstruierte WorldDefinition aus gecachten JSON-Stufen zurück.

    Der Name bleibt aus Kompatibilitätsgründen erhalten. Es wird bewusst nicht
    dieselbe WorldDefinition-Instanz geteilt.
    """
    raw_config = load_world_config(
        get_default_config_path(),
        use_cache=True,
    )
    return create_world_definition(
        raw_config,
        use_cache=True,
    )


def reset_provider_caches() -> None:
    """
    Leert alle Provider-, Validator- und Generator-Caches.

    Reihenfolge:
    1. Config-Text
    2. validierte Config
    3. Definition-Config
    4. Validator
    5. Generator und Generierungsprofile
    """
    errors: list[Exception] = []

    for reset_action in (
        _read_config_text_cached.cache_clear,
        _validate_config_canonical_cached.cache_clear,
        _definition_config_canonical_cached.cache_clear,
        clear_flat_validator_caches,
        reset_default_flat_world_generator_cache,
    ):
        try:
            reset_action()
        except Exception as exc:  # pragma: no cover - defensive reset guard
            errors.append(exc)

    if errors:
        raise WorldProviderError(
            "One or more Flat provider caches could not be reset.",
            details={
                "errors": [
                    {
                        "errorType": type(error).__name__,
                        "error": str(error),
                    }
                    for error in errors
                ],
            },
        )


__all__ = (
    "PROVIDER_ID",
    "WORLD_ID",
    "WORLD_TYPE",
    "PROVIDER_LABEL",
    "PROVIDER_VERSION",
    "PROVIDER_CONTRACT_VERSION",
    "CONFIG_FILENAME",
    "EXPECTED_CONFIG_FILE_IDENTITY",
    "PROVIDER_MODULE",
    "MAX_CONFIG_FILE_BYTES",
    "CONFIG_READ_CACHE_SIZE",
    "VALIDATED_CONFIG_CACHE_SIZE",
    "SUPPORTED_PROVIDER_FUNCTIONS",
    "OPTIONAL_PROVIDER_FUNCTIONS",
    "FlatConfigFileSignature",
    "FlatProviderStatus",
    "get_default_config_path",
    "get_provider_info",
    "load_world_config",
    "validate_world_config",
    "create_world_definition",
    "generate_chunk",
    "load_default_world_definition",
    "generate_default_chunk",
    "get_provider_cache_info",
    "get_provider_status",
    "require_provider_ready",
    "get_provider_contract",
    "get_cached_default_world_definition",
    "reset_provider_caches",
)
