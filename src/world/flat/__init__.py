# src/world/flat/__init__.py
"""
VECTOPLAN Flat World Provider Package.

Dieses Package enthält die erste konkrete Welt-Implementierung für den
Chunk-Service:

    src/world/flat/

Die flache Welt ist die erste Phase-1-Welt. Sie ist:
- lokal
- flach
- deterministisch
- chunkbasiert
- ohne Datenbank
- ohne Geodaten
- ohne Core-Abhängigkeit
- ohne Library-Abhängigkeit
- ohne Three.js-Objekte

Wichtig:
Dieses Package enthält konkrete Flat-World-Logik.
Die neutrale Lade- und Service-Logik liegt eine Ebene höher:

    src/world/

Aufteilung:

    src/world/flat/world.json
        → Konfiguration der flachen Welt

    src/world/flat/validator.py
        → Prüfung der Flat-World-Konfiguration

    src/world/flat/generator.py
        → Erzeugung flacher Chunk-Zelldaten

    src/world/flat/provider.py
        → Provider-Schnittstelle für src/world/loader.py und WorldService

Dieses __init__.py bleibt bewusst leichtgewichtig:
- keine harte Generierung beim Import
- keine world.json-Lesung beim Import
- keine schweren Provider-Imports beim Import
- Lazy-Imports für öffentliche Provider-Funktionen
- Diagnosefunktionen für Startup-Checks und Tests

Der bevorzugte Zugriff läuft später über:

    from src.world.service import get_default_world_service

Nicht direkt über src.world.flat.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Final


# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------

PROVIDER_ID: Final[str] = "flat"
WORLD_ID: Final[str] = "flat"
WORLD_TYPE: Final[str] = "flat"

PROVIDER_LABEL: Final[str] = "Flat Debug World"
PROVIDER_VERSION: Final[str] = "0.1.0"

GENERATOR_TYPE: Final[str] = "flat-world"
GENERATOR_VERSION: Final[str] = "1"

CONFIG_FILENAME: Final[str] = "world.json"

PROJECTION_TYPE: Final[str] = "flat-local-v1"
TOPOLOGY_TYPE: Final[str] = "flat-unbounded-v1"
COORDINATE_SYSTEM: Final[str] = "vectoplan-world-y-up-v1"

PACKAGE_NAME: Final[str] = "src.world.flat"

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

_PUBLIC_SYMBOLS: Final[dict[str, tuple[str, str]]] = {
    # provider.py
    "get_provider_info": ("src.world.flat.provider", "get_provider_info"),
    "get_default_config_path": ("src.world.flat.provider", "get_default_config_path"),
    "load_world_config": ("src.world.flat.provider", "load_world_config"),
    "validate_world_config": ("src.world.flat.provider", "validate_world_config"),
    "create_world_definition": ("src.world.flat.provider", "create_world_definition"),
    "generate_chunk": ("src.world.flat.provider", "generate_chunk"),

    # validator.py
    "validate_flat_world_config": (
        "src.world.flat.validator",
        "validate_flat_world_config",
    ),

    # generator.py
    "FlatWorldGenerator": ("src.world.flat.generator", "FlatWorldGenerator"),
    "generate_flat_chunk": ("src.world.flat.generator", "generate_flat_chunk"),
}


__all__ = (
    "PROVIDER_ID",
    "WORLD_ID",
    "WORLD_TYPE",
    "PROVIDER_LABEL",
    "PROVIDER_VERSION",
    "GENERATOR_TYPE",
    "GENERATOR_VERSION",
    "CONFIG_FILENAME",
    "PROJECTION_TYPE",
    "TOPOLOGY_TYPE",
    "COORDINATE_SYSTEM",
    "PACKAGE_NAME",
    "EXPECTED_FILES",
    "EXPECTED_MODULES",
    "FlatWorldPackageStatus",
    "get_flat_package_dir",
    "get_flat_config_path",
    "get_flat_package_status",
    "is_flat_package_ready",
    "require_flat_package_ready",
    "get_public_symbol_map",
    *_PUBLIC_SYMBOLS.keys(),
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_flat_package_dir() -> Path:
    """
    Gibt den Ordner dieses Flat-World-Packages zurück.

    Diese Funktion ist bewusst defensiv, aber in normalem Python-Betrieb
    sollte __file__ immer vorhanden sein.
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
    """
    try:
        return find_spec(_module_path(module_name)) is not None
    except Exception:
        return False


def _file_exists(filename: str) -> bool:
    """
    Prüft defensiv, ob eine erwartete Datei im Flat-Package existiert.
    """
    try:
        return (get_flat_package_dir() / filename).is_file()
    except Exception:
        return False


def _safe_string(value: Any, *, default: str = "") -> str:
    """
    Wandelt einen beliebigen Wert defensiv in einen String um.
    """
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlatWorldPackageStatus:
    """
    Diagnosezustand des Flat-World-Packages.

    Diese Struktur kann später in Startup-Checks, Tests oder Health-Diagnosen
    verwendet werden.
    """

    package_name: str
    provider_id: str
    world_id: str
    world_type: str
    provider_label: str
    provider_version: str
    generator_type: str
    generator_version: str
    package_dir: str
    config_path: str
    expected_files: tuple[str, ...]
    available_files: tuple[str, ...]
    missing_files: tuple[str, ...]
    expected_modules: tuple[str, ...]
    available_modules: tuple[str, ...]
    missing_modules: tuple[str, ...]
    ready: bool

    def to_dict(self) -> dict[str, Any]:
        """
        Gibt den Diagnosezustand als Dictionary zurück.
        """
        return asdict(self)


@lru_cache(maxsize=1)
def get_flat_package_status() -> FlatWorldPackageStatus:
    """
    Ermittelt den aktuellen Strukturzustand von src/world/flat.

    Der Status wird gecacht, weil diese Funktion für Startup-Checks oder Tests
    mehrfach aufgerufen werden kann.

    Cache leeren:

        get_flat_package_status.cache_clear()
    """
    available_files: list[str] = []
    missing_files: list[str] = []

    for filename in EXPECTED_FILES:
        if _file_exists(filename):
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

    ready = len(missing_files) == 0 and len(missing_modules) == 0

    package_dir = get_flat_package_dir()
    config_path = get_flat_config_path()

    return FlatWorldPackageStatus(
        package_name=PACKAGE_NAME,
        provider_id=PROVIDER_ID,
        world_id=WORLD_ID,
        world_type=WORLD_TYPE,
        provider_label=PROVIDER_LABEL,
        provider_version=PROVIDER_VERSION,
        generator_type=GENERATOR_TYPE,
        generator_version=GENERATOR_VERSION,
        package_dir=str(package_dir),
        config_path=str(config_path),
        expected_files=EXPECTED_FILES,
        available_files=tuple(available_files),
        missing_files=tuple(missing_files),
        expected_modules=EXPECTED_MODULES,
        available_modules=tuple(available_modules),
        missing_modules=tuple(missing_modules),
        ready=ready,
    )


def is_flat_package_ready() -> bool:
    """
    Gibt zurück, ob alle erwarteten Flat-World-Dateien und Module vorhanden sind.
    """
    try:
        return get_flat_package_status().ready
    except Exception:
        return False


def require_flat_package_ready() -> None:
    """
    Erzwingt, dass die Flat-World-Package-Struktur vollständig ist.

    Diese Funktion soll nicht automatisch beim Import ausgeführt werden,
    weil während der schrittweisen Entwicklung erwartete Dateien zunächst
    noch fehlen können.
    """
    status = get_flat_package_status()

    if status.ready:
        return

    raise RuntimeError(
        "VECTOPLAN flat world package is not ready. "
        f"Missing files: {status.missing_files}. "
        f"Missing modules: {status.missing_modules}."
    )


def get_public_symbol_map() -> dict[str, tuple[str, str]]:
    """
    Gibt eine Kopie der Lazy-Import-Symboltabelle zurück.
    """
    return dict(_PUBLIC_SYMBOLS)


# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------

@lru_cache(maxsize=128)
def _load_public_symbol(symbol_name: str) -> Any:
    """
    Lädt ein öffentliches Symbol erst bei tatsächlicher Verwendung.

    Dadurch kann src.world.flat bereits importiert werden, obwohl provider.py,
    validator.py oder generator.py noch nicht vollständig existieren.
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
    Lazy-Import-Hook für öffentliche Flat-World-Symbole.

    Beispiel:

        from src.world.flat import generate_chunk

    importiert src.world.flat.provider erst dann, wenn generate_chunk
    tatsächlich angefragt wird.
    """
    return _load_public_symbol(name)


def __dir__() -> list[str]:
    """
    Sorgt dafür, dass dir(src.world.flat) auch Lazy-Symbole sinnvoll anzeigt.
    """
    default_names = set(globals().keys())
    public_names = set(__all__)
    return sorted(default_names | public_names)