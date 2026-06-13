# services/vectoplan-editor/src/bootstrap/__init__.py
"""
Package-Initialisierung für `src.bootstrap`.

Ziel dieser Datei:
- ein stabiler, kleiner Einstiegspunkt für das Bootstrap-Package
- lazy Zugriff auf `src.bootstrap.startup`, damit Importe möglichst leichtgewichtig bleiben
- saubere Re-Exports der öffentlichen Startup-API
- klare Fehlertexte, falls `startup.py` fehlt oder unvollständig ist

Wichtig:
- Diese Datei enthält selbst keine Startup-Logik.
- Die eigentliche Implementierung liegt in `src/bootstrap/startup.py`.
- Das Package dient hier als sauberer Namensraum und als kontrollierte API-Grenze.

Warum diese Datei sinnvoll ist:
- spätere Importe wie `from src.bootstrap import run_startup` bleiben stabil
- neue Entwickler sehen sofort, welche öffentliche Bootstrap-API existiert
- das Package bleibt leicht testbar und sauber strukturiert

Robustheitsprinzipien:
- keine harten Top-Level-Importe auf `startup.py`
- Lazy-Import mit Cache
- klare Validierung erwarteter Attribute/Funktionen
- defensive Wrapper statt direkter, ungeschützter Durchreichung
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final


if TYPE_CHECKING:
    from flask import Flask
    from .startup import FileCheckSpec, PathCheckSpec


# -----------------------------------------------------------------------------
# Konstanten
# -----------------------------------------------------------------------------

_STARTUP_MODULE_NAME: Final[str] = "src.bootstrap.startup"

_PUBLIC_EXPORTS: Final[tuple[str, ...]] = (
    "PathCheckSpec",
    "FileCheckSpec",
    "get_default_path_check_specs",
    "get_default_file_check_specs",
    "get_default_path_check_spec_data",
    "get_default_file_check_spec_data",
    "run_startup",
    "bootstrap_app",
    "initialize_app",
    "get_startup_state",
    "get_startup_summary",
)


# -----------------------------------------------------------------------------
# Interne Hilfsfunktionen
# -----------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _import_startup_module():
    """
    Importiert das Startup-Modul genau einmal pro Prozess.

    Warum Cache?
    - verhindert unnötige Mehrfachimporte
    - stabilisiert wiederholte Zugriffe über Wrapper-Funktionen
    - hält `__init__.py` leichtgewichtig

    Fehler:
    - werden mit klarem Kontext neu geworfen
    """
    try:
        return importlib.import_module(_STARTUP_MODULE_NAME)
    except ModuleNotFoundError as exc:
        if getattr(exc, "name", None) == _STARTUP_MODULE_NAME:
            raise RuntimeError(
                "Das Bootstrap-Modul `src.bootstrap.startup` wurde nicht gefunden. "
                "Prüfe, ob die Datei "
                "`services/vectoplan-editor/src/bootstrap/startup.py` existiert."
            ) from exc

        raise RuntimeError(
            "Das Bootstrap-Modul `src.bootstrap.startup` konnte nicht geladen werden, "
            f"weil eine innere Abhängigkeit fehlt: {getattr(exc, 'name', None)!r}."
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            "Das Bootstrap-Modul `src.bootstrap.startup` konnte nicht importiert werden."
        ) from exc


def _resolve_public_attribute(name: str) -> Any:
    """
    Löst ein öffentliches Attribut aus `startup.py` auf.

    Diese Funktion validiert zusätzlich, dass nur freigegebene Exporte
    aufgelöst werden.
    """
    if name not in _PUBLIC_EXPORTS:
        raise AttributeError(f"`src.bootstrap` exportiert kein Attribut namens `{name}`.")

    module = _import_startup_module()

    try:
        return getattr(module, name)
    except AttributeError as exc:
        raise RuntimeError(
            f"Das Attribut `{name}` fehlt in `{_STARTUP_MODULE_NAME}`. "
            "Prüfe, ob die öffentliche Startup-API vollständig implementiert ist."
        ) from exc


def _resolve_callable(name: str):
    """
    Löst ein aufrufbares öffentliches Attribut aus `startup.py` auf.
    """
    candidate = _resolve_public_attribute(name)
    if not callable(candidate):
        raise RuntimeError(
            f"Das Attribut `{name}` in `{_STARTUP_MODULE_NAME}` ist nicht aufrufbar."
        )
    return candidate


# -----------------------------------------------------------------------------
# Öffentliche Wrapper-Funktionen
# -----------------------------------------------------------------------------

def get_default_path_check_specs() -> tuple["PathCheckSpec", ...]:
    """
    Liefert die Standard-Pfadprüfungen aus `startup.py`.
    """
    function = _resolve_callable("get_default_path_check_specs")
    return function()


def get_default_file_check_specs() -> tuple["FileCheckSpec", ...]:
    """
    Liefert die Standard-Dateiprüfungen aus `startup.py`.
    """
    function = _resolve_callable("get_default_file_check_specs")
    return function()


def get_default_path_check_spec_data() -> list[dict[str, Any]]:
    """
    Liefert eine serialisierbare Darstellung der Standard-Pfadprüfungen.
    """
    function = _resolve_callable("get_default_path_check_spec_data")
    return function()


def get_default_file_check_spec_data() -> list[dict[str, Any]]:
    """
    Liefert eine serialisierbare Darstellung der Standard-Dateiprüfungen.
    """
    function = _resolve_callable("get_default_file_check_spec_data")
    return function()


def run_startup(app: "Flask"):
    """
    Führt die Startup-Hooks des Editors aus.
    """
    function = _resolve_callable("run_startup")
    return function(app)


def bootstrap_app(app: "Flask"):
    """
    Alias für den Bootstrap-Aufruf des Editors.
    """
    function = _resolve_callable("bootstrap_app")
    return function(app)


def initialize_app(app: "Flask"):
    """
    Alias für den Initialisierungsaufruf des Editors.
    """
    function = _resolve_callable("initialize_app")
    return function(app)


def get_startup_state(app: "Flask") -> dict[str, Any]:
    """
    Liefert den aktuellen Startup-Zustand des Editors.
    """
    function = _resolve_callable("get_startup_state")
    return function(app)


def get_startup_summary(app: "Flask") -> dict[str, Any]:
    """
    Liefert eine kompakte Zusammenfassung des Startup-Zustands.
    """
    function = _resolve_callable("get_startup_summary")
    return function(app)


# -----------------------------------------------------------------------------
# Lazy-Attributzugriff für Klassen / Re-Exports
# -----------------------------------------------------------------------------

def __getattr__(name: str) -> Any:
    """
    Lazy-Attributauflösung für öffentliche Re-Exports.

    So bleiben auch Klassen wie `PathCheckSpec` oder `FileCheckSpec`
    verfügbar, ohne dass `startup.py` beim Package-Import sofort geladen
    werden muss.
    """
    return _resolve_public_attribute(name)


def __dir__() -> list[str]:
    """
    Liefert eine stabile, sortierte Darstellung der öffentlichen Package-API.
    """
    default_dir = set(globals().keys())
    default_dir.update(_PUBLIC_EXPORTS)
    return sorted(default_dir)


__all__ = list(_PUBLIC_EXPORTS)