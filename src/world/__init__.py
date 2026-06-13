# src/world/__init__.py
"""
VECTOPLAN World Package.

Dieses Package ist die neutrale Einstiegsschicht für das Laden, Verwalten,
Scannen und Ausführen von Welt-Providern innerhalb des Chunk-Service.

Wichtig:
- In diesem Package liegt keine konkrete Weltlogik.
- Konkrete Welten liegen in eigenen Unterordnern, z. B.:
    src/world/flat/
    src/world/realWorld/
- Dieses Package stellt nur stabile Import-Grenzen, Metadaten,
  Lazy-Imports und Diagnose-Helfer bereit.

Die Kernmodule dieser Schicht sind:

    src/world/errors.py
    src/world/models.py
    src/world/registry.py
    src/world/loader.py
    src/world/service.py
    src/world/serializer.py
    src/world/discovery.py

Rollen:

    errors.py
        → gemeinsame Fehlerklassen

    models.py
        → framework-neutrale Datenmodelle

    registry.py
        → explizite Provider-Registry

    loader.py
        → lädt Provider und world.json

    service.py
        → zentrale WorldService-Fassade

    serializer.py
        → JSON-nahe Ausgabe für spätere Routes

    discovery.py
        → scannt vorhandene World-Provider-Ordner dynamisch

Dieses __init__.py ist bewusst robust gebaut:
- keine harten Imports auf Zielmodule beim Package-Import
- Lazy-Imports über __getattr__
- klare Fehler bei fehlenden Zielmodulen
- kleine Diagnosefunktionen für Startup-Checks und Tests
- Discovery-Symbole werden erst geladen, wenn sie wirklich verwendet werden
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from typing import Any, Final


# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------

PACKAGE_NAME: Final[str] = "src.world"
PACKAGE_LABEL: Final[str] = "VECTOPLAN World System"
PACKAGE_PURPOSE: Final[str] = (
    "Neutral loading, discovery and orchestration layer for VECTOPLAN world providers."
)

PACKAGE_VERSION: Final[str] = "0.1.0"

DEFAULT_WORLD_ID: Final[str] = "flat"

EXPECTED_CORE_MODULES: Final[tuple[str, ...]] = (
    "errors",
    "models",
    "registry",
    "loader",
    "service",
    "serializer",
    "discovery",
)

EXPECTED_WORLD_PROVIDER_PACKAGES: Final[tuple[str, ...]] = (
    "flat",
)


# ---------------------------------------------------------------------------
# Lazy public symbol map
# ---------------------------------------------------------------------------

_PUBLIC_SYMBOLS: Final[dict[str, tuple[str, str]]] = {
    # errors.py
    "WorldError": ("src.world.errors", "WorldError"),
    "WorldNotFoundError": ("src.world.errors", "WorldNotFoundError"),
    "WorldConfigError": ("src.world.errors", "WorldConfigError"),
    "WorldValidationError": ("src.world.errors", "WorldValidationError"),
    "WorldGenerationError": ("src.world.errors", "WorldGenerationError"),
    "UnsupportedWorldTypeError": ("src.world.errors", "UnsupportedWorldTypeError"),

    # models.py
    "PaletteEntry": ("src.world.models", "PaletteEntry"),
    "WorldDefinition": ("src.world.models", "WorldDefinition"),
    "WorldProviderInfo": ("src.world.models", "WorldProviderInfo"),
    "ChunkRequest": ("src.world.models", "ChunkRequest"),
    "GeneratedChunk": ("src.world.models", "GeneratedChunk"),

    # registry.py
    "WorldRegistry": ("src.world.registry", "WorldRegistry"),
    "get_default_world_registry": ("src.world.registry", "get_default_world_registry"),

    # loader.py
    "WorldLoader": ("src.world.loader", "WorldLoader"),
    "get_default_world_loader": ("src.world.loader", "get_default_world_loader"),

    # service.py
    "WorldService": ("src.world.service", "WorldService"),
    "get_default_world_service": ("src.world.service", "get_default_world_service"),

    # serializer.py
    "serialize_world_definition": (
        "src.world.serializer",
        "serialize_world_definition",
    ),
    "serialize_world_metadata_response": (
        "src.world.serializer",
        "serialize_world_metadata_response",
    ),
    "serialize_generated_chunk": (
        "src.world.serializer",
        "serialize_generated_chunk",
    ),
    "serialize_chunk_response": (
        "src.world.serializer",
        "serialize_chunk_response",
    ),
    "serialize_chunk_batch_response": (
        "src.world.serializer",
        "serialize_chunk_batch_response",
    ),

    # discovery.py
    "WorldProviderCandidate": (
        "src.world.discovery",
        "WorldProviderCandidate",
    ),
    "DiscoveredWorldProvider": (
        "src.world.discovery",
        "DiscoveredWorldProvider",
    ),
    "WorldDiscoveryResult": (
        "src.world.discovery",
        "WorldDiscoveryResult",
    ),
    "get_world_package_dir": (
        "src.world.discovery",
        "get_world_package_dir",
    ),
    "scan_world_provider_packages": (
        "src.world.discovery",
        "scan_world_provider_packages",
    ),
    "discover_world_provider": (
        "src.world.discovery",
        "discover_world_provider",
    ),
    "discover_worlds": (
        "src.world.discovery",
        "discover_worlds",
    ),
    "discover_worlds_as_dict": (
        "src.world.discovery",
        "discover_worlds_as_dict",
    ),
    "reset_world_discovery_cache": (
        "src.world.discovery",
        "reset_world_discovery_cache",
    ),
    "create_registry_from_discovery_result": (
        "src.world.discovery",
        "create_registry_from_discovery_result",
    ),
    "create_registry_from_discovered_worlds": (
        "src.world.discovery",
        "create_registry_from_discovered_worlds",
    ),
    "get_discovered_world": (
        "src.world.discovery",
        "get_discovered_world",
    ),
    "get_valid_discovered_world_ids": (
        "src.world.discovery",
        "get_valid_discovered_world_ids",
    ),
    "require_discovered_worlds_ready": (
        "src.world.discovery",
        "require_discovered_worlds_ready",
    ),
}


__all__ = (
    "PACKAGE_NAME",
    "PACKAGE_LABEL",
    "PACKAGE_PURPOSE",
    "PACKAGE_VERSION",
    "DEFAULT_WORLD_ID",
    "EXPECTED_CORE_MODULES",
    "EXPECTED_WORLD_PROVIDER_PACKAGES",
    "WorldPackageStatus",
    "get_world_package_status",
    "is_world_package_ready",
    "require_world_package_ready",
    "reset_world_package_status_cache",
    "get_public_symbol_map",
    *_PUBLIC_SYMBOLS.keys(),
)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorldPackageStatus:
    """
    Diagnosezustand des neutralen World-Packages.

    Diese Struktur ist absichtlich einfach und JSON-nah gehalten,
    damit sie später problemlos in Startup-Checks, Health-Diagnosen
    oder Tests verwendet werden kann.
    """

    package_name: str
    package_label: str
    package_version: str
    default_world_id: str
    expected_core_modules: tuple[str, ...]
    available_core_modules: tuple[str, ...]
    missing_core_modules: tuple[str, ...]
    expected_world_provider_packages: tuple[str, ...]
    available_world_provider_packages: tuple[str, ...]
    missing_world_provider_packages: tuple[str, ...]
    ready: bool

    def to_dict(self) -> dict[str, Any]:
        """
        Gibt den Diagnosezustand als normales Dictionary zurück.

        Tupel werden dabei bewusst nicht manuell in Listen konvertiert.
        Flask/jsonify kann Tupel normalerweise serialisieren. Falls später
        ein strenger JSON-Serializer verwendet wird, kann diese Methode
        zentral angepasst werden.
        """
        return asdict(self)


def _module_exists(module_path: str) -> bool:
    """
    Prüft defensiv, ob ein Modul importierbar wäre, ohne es tatsächlich
    vollständig zu importieren.

    Wichtig:
    - find_spec kann selbst Exceptions werfen, wenn Importpfade defekt sind.
    - Deshalb wird hier bewusst breit abgefangen.
    """
    try:
        return find_spec(module_path) is not None
    except Exception:
        return False


def _build_module_path(module_name: str) -> str:
    """
    Baut einen vollständigen Modulpfad innerhalb von src.world.
    """
    return f"{PACKAGE_NAME}.{module_name}"


@lru_cache(maxsize=1)
def get_world_package_status() -> WorldPackageStatus:
    """
    Ermittelt den aktuellen Strukturzustand des World-Packages.

    Diese Funktion ist gecacht, weil sie typischerweise bei Startup-Checks
    oder Tests mehrfach aufgerufen werden kann.

    Falls während der Entwicklung neue Dateien angelegt werden und der Status
    im selben Python-Prozess neu berechnet werden soll, kann der Cache mit

        get_world_package_status.cache_clear()

    oder über

        reset_world_package_status_cache()

    geleert werden.
    """
    available_core_modules: list[str] = []
    missing_core_modules: list[str] = []

    for module_name in EXPECTED_CORE_MODULES:
        module_path = _build_module_path(module_name)

        if _module_exists(module_path):
            available_core_modules.append(module_name)
        else:
            missing_core_modules.append(module_name)

    available_world_provider_packages: list[str] = []
    missing_world_provider_packages: list[str] = []

    for package_name in EXPECTED_WORLD_PROVIDER_PACKAGES:
        module_path = _build_module_path(package_name)

        if _module_exists(module_path):
            available_world_provider_packages.append(package_name)
        else:
            missing_world_provider_packages.append(package_name)

    ready = (
        len(missing_core_modules) == 0
        and len(missing_world_provider_packages) == 0
    )

    return WorldPackageStatus(
        package_name=PACKAGE_NAME,
        package_label=PACKAGE_LABEL,
        package_version=PACKAGE_VERSION,
        default_world_id=DEFAULT_WORLD_ID,
        expected_core_modules=EXPECTED_CORE_MODULES,
        available_core_modules=tuple(available_core_modules),
        missing_core_modules=tuple(missing_core_modules),
        expected_world_provider_packages=EXPECTED_WORLD_PROVIDER_PACKAGES,
        available_world_provider_packages=tuple(available_world_provider_packages),
        missing_world_provider_packages=tuple(missing_world_provider_packages),
        ready=ready,
    )


def reset_world_package_status_cache() -> None:
    """
    Leert den gecachten Package-Status.

    Nützlich, wenn während eines laufenden Entwicklungsprozesses neue Dateien
    unter src/world angelegt wurden.
    """
    get_world_package_status.cache_clear()


def is_world_package_ready() -> bool:
    """
    Gibt zurück, ob alle erwarteten World-Kernmodule und Provider-Pakete
    vorhanden sind.
    """
    try:
        return get_world_package_status().ready
    except Exception:
        return False


def require_world_package_ready() -> None:
    """
    Erzwingt, dass die erwartete World-Package-Struktur vorhanden ist.

    Diese Funktion ist für spätere Startup-Checks geeignet.

    Sie sollte nicht beim normalen Import von src.world automatisch ausgeführt
    werden, weil während der schrittweisen Entwicklung noch Dateien fehlen
    können.
    """
    status = get_world_package_status()

    if status.ready:
        return

    details = {
        "missingCoreModules": status.missing_core_modules,
        "missingWorldProviderPackages": status.missing_world_provider_packages,
    }

    raise RuntimeError(
        "VECTOPLAN world package is not ready. "
        f"Missing structure: {details}"
    )


def get_public_symbol_map() -> dict[str, tuple[str, str]]:
    """
    Gibt eine Kopie der Lazy-Import-Symboltabelle zurück.

    Nützlich für Tests, Diagnose und Dokumentation.
    """
    return dict(_PUBLIC_SYMBOLS)


# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------

@lru_cache(maxsize=128)
def _load_public_symbol(symbol_name: str) -> Any:
    """
    Lädt ein öffentliches Symbol erst bei tatsächlicher Verwendung.

    Dadurch kann dieses __init__.py bereits existieren, ohne dass alle
    Zielmodule beim Package-Import direkt importiert werden.
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
            "The target module does not exist yet or is not importable."
        ) from exc
    except Exception as exc:
        raise ImportError(
            f"Could not import module '{module_path}' while resolving "
            f"public symbol '{symbol_name}'."
        ) from exc

    try:
        return getattr(module, attribute_name)
    except AttributeError as exc:
        raise ImportError(
            f"Module '{module_path}' does not expose expected attribute "
            f"'{attribute_name}' for public symbol '{symbol_name}'."
        ) from exc


def __getattr__(name: str) -> Any:
    """
    Python-Lazy-Import-Hook für öffentliche Symbole.

    Beispiele:

        from src.world import WorldService
        from src.world import discover_worlds
        from src.world import create_registry_from_discovered_worlds

    Die jeweiligen Zielmodule werden erst importiert, wenn das Symbol wirklich
    angefragt wird.
    """
    return _load_public_symbol(name)


def __dir__() -> list[str]:
    """
    Sorgt dafür, dass dir(src.world) auch Lazy-Symbole sinnvoll anzeigt.
    """
    default_names = set(globals().keys())
    public_names = set(__all__)
    return sorted(default_names | public_names)