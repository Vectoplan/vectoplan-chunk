# services/vectoplan-chunk/src/project_access/__init__.py
"""
Stabile, defensiv geladene Importfassade für die Project-Access-Schicht.

Das Paket bündelt die transaktionsneutralen Operationen aus ``service.py``
unter einem kleinen öffentlichen Vertrag. Routen, Provisioning, Bootstrap und
spätere Autorisierungsschichten sollen vorzugsweise aus diesem Paket und nicht
direkt aus dem Implementierungsmodul importieren.

Architekturregeln
-----------------
* Der Service wird lazy geladen, damit ein bloßer Paketimport keine unnötige
  Datenbank-, Flask- oder Modelinitialisierung erzwingt.
* Importfehler werden vollständig diagnostiziert und nicht still verschluckt.
* Öffentliche Symbole sind ausdrücklich aufgelistet und versionsstabil.
* ORM-Objekte und Datenbankzustände werden hier nicht gecacht.
* Caches enthalten ausschließlich Importstatus und unveränderliche
  Diagnoseinformationen.
* Das Paket führt keine Queries, Flushes, Commits, Rollbacks, Migrationen oder
  Tabelleninitialisierungen aus.
* Die eigentliche Transaktionshoheit bleibt bei Route, Provisioning oder
  Bootstrap-Caller.

Beispiele::

    from src.project_access import ensure_project_access_initialized

    result = ensure_project_access_initialized(
        project=project,
        owner_user_id="1",
        actor_user_id="1",
        session=db.session,
        flush=True,
    )

Oder über die sessiongebundene Fassade::

    from src.project_access import ProjectAccessService

    access_service = ProjectAccessService(session=db.session)
    result = access_service.initialize(
        project=project,
        owner_user_id="1",
        actor_user_id="1",
    )

Der Caller entscheidet anschließend über ``commit()`` oder ``rollback()``.
"""

from __future__ import annotations

import importlib
import threading
import traceback
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType, ModuleType
from typing import Any, Final, Mapping, Optional, Tuple


PROJECT_ACCESS_PACKAGE_VERSION: Final[str] = "1.0.0"
PROJECT_ACCESS_PACKAGE_SCHEMA_VERSION: Final[int] = 1

SERVICE_MODULE_BASENAME: Final[str] = "service"
SERVICE_MODULE_PATH: Final[str] = f"{__name__}.{SERVICE_MODULE_BASENAME}"

# Dieser Vertrag muss mit ``service.__all__`` synchron bleiben. Ein fehlendes
# Symbol macht den Package-Status bewusst nicht ready.
EXPECTED_SERVICE_SYMBOLS: Final[Tuple[str, ...]] = (
    "PROJECT_ACCESS_SERVICE_VERSION",
    "PROJECT_ACCESS_SERVICE_SCHEMA_VERSION",
    "ProjectAccessServiceError",
    "ProjectAccessValidationError",
    "ProjectAccessNotFoundError",
    "ProjectAccessConflictError",
    "ProjectAccessCrossProjectError",
    "ProjectAccessInvariantError",
    "ProjectAccessPersistenceError",
    "MutationStats",
    "EntityMutationResult",
    "ProjectAccessInitializationResult",
    "ProjectAccessDeleteResult",
    "ProjectAccessService",
    "get_default_role_templates",
    "clear_project_access_service_caches",
    "resolve_project",
    "list_project_roles",
    "find_project_role",
    "ensure_default_project_roles",
    "ensure_owner_role_assignment",
    "ensure_project_access_initialized",
    "list_project_groups",
    "find_project_group",
    "ensure_project_group",
    "list_group_memberships",
    "ensure_user_in_group",
    "remove_user_from_group",
    "list_project_role_assignments",
    "ensure_role_assignment",
    "assign_role_to_user",
    "assign_role_to_group",
    "revoke_role_assignment",
    "soft_delete_project_access",
    "build_project_access_summary",
    "get_project_access_service_contract",
)

PACKAGE_PUBLIC_SYMBOLS: Final[Tuple[str, ...]] = (
    "PROJECT_ACCESS_PACKAGE_VERSION",
    "PROJECT_ACCESS_PACKAGE_SCHEMA_VERSION",
    "SERVICE_MODULE_BASENAME",
    "SERVICE_MODULE_PATH",
    "EXPECTED_SERVICE_SYMBOLS",
    "ProjectAccessPackageImportRecord",
    "ProjectAccessPackageStatus",
    "get_project_access_service_module",
    "get_project_access_import_record",
    "get_project_access_service_symbol",
    "require_project_access_service_symbol",
    "has_project_access_service_symbol",
    "get_project_access_public_api",
    "get_project_access_package_status",
    "get_project_access_package_contract",
    "is_project_access_package_ready",
    "require_project_access_package_ready",
    "reset_project_access_package_cache",
)

_IMPORT_LOCK = threading.RLock()
_SERVICE_MODULE: Optional[ModuleType] = None
_SERVICE_IMPORT_RECORD: Optional["ProjectAccessPackageImportRecord"] = None


@dataclass(frozen=True)
class ProjectAccessPackageImportRecord:
    """Diagnoseergebnis des lazy Imports von ``service.py``."""

    module_path: str
    imported: bool
    declared_symbols: Tuple[str, ...] = field(default_factory=tuple)
    available_symbols: Tuple[str, ...] = field(default_factory=tuple)
    missing_symbols: Tuple[str, ...] = field(default_factory=tuple)
    unexpected_declared_symbols: Tuple[str, ...] = field(default_factory=tuple)
    error: Optional[str] = None
    traceback_text: Optional[str] = None

    @property
    def contract_complete(self) -> bool:
        return self.imported and not self.missing_symbols

    def to_dict(self, *, include_traceback: bool = False) -> dict[str, Any]:
        return {
            "modulePath": self.module_path,
            "imported": self.imported,
            "contractComplete": self.contract_complete,
            "declaredSymbols": list(self.declared_symbols),
            "availableSymbols": list(self.available_symbols),
            "missingSymbols": list(self.missing_symbols),
            "unexpectedDeclaredSymbols": list(
                self.unexpected_declared_symbols
            ),
            "error": self.error,
            "traceback": self.traceback_text if include_traceback else None,
        }


@dataclass(frozen=True)
class ProjectAccessPackageStatus:
    """Gesamtstatus der Project-Access-Importfassade."""

    package_name: str
    package_version: str
    schema_version: int
    ready: bool
    import_record: ProjectAccessPackageImportRecord
    service_contract_ready: Optional[bool] = None
    service_contract: Mapping[str, Any] = field(default_factory=dict)
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self, *, include_traceback: bool = False) -> dict[str, Any]:
        return {
            "packageName": self.package_name,
            "packageVersion": self.package_version,
            "schemaVersion": self.schema_version,
            "ready": self.ready,
            "import": self.import_record.to_dict(
                include_traceback=include_traceback
            ),
            "serviceContractReady": self.service_contract_ready,
            "serviceContract": _make_json_safe(self.service_contract),
            "warnings": list(self.warnings),
            "lazyImport": True,
            "databaseQueries": False,
            "commitsInternally": False,
            "rollbacksInternally": False,
            "ormStateCached": False,
        }


def _make_json_safe(value: Any, *, _depth: int = 0) -> Any:
    """Konvertiert Diagnosewerte defensiv in JSON-kompatible Strukturen."""

    if _depth > 12:
        return "<max-depth>"

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            try:
                normalized_key = str(key)
            except Exception:
                normalized_key = "<unprintable-key>"
            result[normalized_key] = _make_json_safe(
                item,
                _depth=_depth + 1,
            )
        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _make_json_safe(item, _depth=_depth + 1)
            for item in value
        ]

    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        try:
            return _make_json_safe(
                serializer(),
                _depth=_depth + 1,
            )
        except Exception:
            pass

    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _normalize_symbol_name(symbol_name: Any) -> Optional[str]:
    """Normalisiert einen angefragten öffentlichen Symbolnamen."""

    if symbol_name is None:
        return None

    try:
        normalized = str(symbol_name).strip()
    except Exception:
        return None

    if not normalized or normalized.startswith("_"):
        return None

    return normalized


def _declared_public_symbols(module: ModuleType) -> Tuple[str, ...]:
    """Liest ``module.__all__`` defensiv und deterministisch aus."""

    try:
        declared = getattr(module, "__all__", tuple())
    except Exception:
        return tuple()

    if not isinstance(declared, (list, tuple, set, frozenset)):
        return tuple()

    result: list[str] = []
    seen: set[str] = set()

    for value in declared:
        normalized = _normalize_symbol_name(value)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)

    return tuple(result)


def _build_success_import_record(
    module: ModuleType,
) -> ProjectAccessPackageImportRecord:
    declared = _declared_public_symbols(module)
    expected_set = set(EXPECTED_SERVICE_SYMBOLS)
    declared_set = set(declared)

    available: list[str] = []
    missing: list[str] = []

    for symbol_name in EXPECTED_SERVICE_SYMBOLS:
        try:
            getattr(module, symbol_name)
        except Exception:
            missing.append(symbol_name)
        else:
            available.append(symbol_name)

    unexpected = tuple(
        symbol_name
        for symbol_name in declared
        if symbol_name not in expected_set
    )

    # Ein Symbol kann vorhanden, aber versehentlich nicht in service.__all__ sein.
    # Das gilt als Vertragsfehler, weil Star-Imports und Dokumentationswerkzeuge
    # sonst ein anderes API-Bild sehen würden.
    for symbol_name in EXPECTED_SERVICE_SYMBOLS:
        if symbol_name not in declared_set and symbol_name not in missing:
            missing.append(symbol_name)

    return ProjectAccessPackageImportRecord(
        module_path=SERVICE_MODULE_PATH,
        imported=True,
        declared_symbols=declared,
        available_symbols=tuple(available),
        missing_symbols=tuple(missing),
        unexpected_declared_symbols=unexpected,
        error=None,
        traceback_text=None,
    )


def _build_failed_import_record(
    exc: BaseException,
) -> ProjectAccessPackageImportRecord:
    try:
        message = f"{type(exc).__name__}: {exc}"
    except Exception:
        message = type(exc).__name__

    try:
        traceback_text = traceback.format_exc()
    except Exception:
        traceback_text = None

    return ProjectAccessPackageImportRecord(
        module_path=SERVICE_MODULE_PATH,
        imported=False,
        declared_symbols=tuple(),
        available_symbols=tuple(),
        missing_symbols=EXPECTED_SERVICE_SYMBOLS,
        unexpected_declared_symbols=tuple(),
        error=message,
        traceback_text=traceback_text,
    )


def _load_service_module(
    *,
    retry_failed: bool = False,
) -> Optional[ModuleType]:
    """
    Lädt ``service.py`` thread-sicher und speichert Importdiagnostik.

    Ein erfolgreicher Modulimport wird dauerhaft wiederverwendet. Ein
    fehlgeschlagener Import wird ebenfalls gemerkt, kann aber über
    ``retry_failed=True`` oder ``reset_project_access_package_cache()`` erneut
    versucht werden.
    """

    global _SERVICE_MODULE
    global _SERVICE_IMPORT_RECORD

    with _IMPORT_LOCK:
        if _SERVICE_MODULE is not None:
            return _SERVICE_MODULE

        if (
            _SERVICE_IMPORT_RECORD is not None
            and not _SERVICE_IMPORT_RECORD.imported
            and not retry_failed
        ):
            return None

        if retry_failed:
            _SERVICE_IMPORT_RECORD = None
            importlib.invalidate_caches()

        try:
            module = importlib.import_module(SERVICE_MODULE_PATH)
        except Exception as exc:
            _SERVICE_MODULE = None
            _SERVICE_IMPORT_RECORD = _build_failed_import_record(exc)
            _clear_status_caches()
            return None

        _SERVICE_MODULE = module
        _SERVICE_IMPORT_RECORD = _build_success_import_record(module)
        _clear_status_caches()
        return module


def get_project_access_service_module(
    *,
    required: bool = False,
    retry_failed: bool = False,
) -> Optional[ModuleType]:
    """Gibt das geladene Service-Modul zurück oder löst kontrolliert aus."""

    module = _load_service_module(retry_failed=retry_failed)

    if module is not None:
        return module

    if not required:
        return None

    record = get_project_access_import_record(
        load=False,
        retry_failed=False,
    )
    details = (
        record.to_dict(include_traceback=True)
        if record is not None
        else {"modulePath": SERVICE_MODULE_PATH}
    )
    raise RuntimeError(
        "vectoplan-chunk project_access service could not be imported. "
        f"Details: {details}"
    )


def get_project_access_import_record(
    *,
    load: bool = True,
    retry_failed: bool = False,
) -> Optional[ProjectAccessPackageImportRecord]:
    """Liefert eine unveränderliche Kopie des aktuellen Importstatus."""

    if load:
        _load_service_module(retry_failed=retry_failed)

    with _IMPORT_LOCK:
        return _SERVICE_IMPORT_RECORD


def get_project_access_service_symbol(
    symbol_name: Any,
    *,
    required: bool = False,
    retry_failed: bool = False,
) -> Any:
    """Liest ein explizit freigegebenes Service-Symbol defensiv."""

    normalized = _normalize_symbol_name(symbol_name)

    if normalized is None or normalized not in EXPECTED_SERVICE_SYMBOLS:
        if required:
            raise LookupError(
                f"Unknown project_access service symbol: {symbol_name!r}"
            )
        return None

    module = get_project_access_service_module(
        required=required,
        retry_failed=retry_failed,
    )
    if module is None:
        return None

    try:
        value = getattr(module, normalized)
    except Exception as exc:
        if required:
            raise RuntimeError(
                f"Project-access symbol '{normalized}' is unavailable: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return None

    return value


def require_project_access_service_symbol(symbol_name: Any) -> Any:
    """Gibt ein Service-Symbol zurück oder löst mit klarer Diagnose aus."""

    return get_project_access_service_symbol(
        symbol_name,
        required=True,
    )


def has_project_access_service_symbol(symbol_name: Any) -> bool:
    """Prüft, ob ein freigegebenes Service-Symbol verfügbar ist."""

    try:
        return get_project_access_service_symbol(symbol_name) is not None
    except Exception:
        return False


@lru_cache(maxsize=1)
def get_project_access_public_api() -> Mapping[str, Any]:
    """Liefert eine unveränderlich zu behandelnde Map der Service-API."""

    module = get_project_access_service_module(required=True)
    result: dict[str, Any] = {}

    for symbol_name in EXPECTED_SERVICE_SYMBOLS:
        try:
            result[symbol_name] = getattr(module, symbol_name)
        except Exception as exc:
            raise RuntimeError(
                "Project-access public API is incomplete. "
                f"Missing or broken symbol: {symbol_name}. "
                f"Cause: {type(exc).__name__}: {exc}"
            ) from exc

    return MappingProxyType(result)


def _read_service_contract(module: ModuleType) -> tuple[dict[str, Any], bool]:
    """Liest den DB-freien Servicevertrag ohne Fehler nach außen zu verlieren."""

    try:
        contract_builder = getattr(
            module,
            "get_project_access_service_contract",
        )
    except Exception as exc:
        return (
            {
                "ready": False,
                "error": f"{type(exc).__name__}: {exc}",
            },
            False,
        )

    if not callable(contract_builder):
        return (
            {
                "ready": False,
                "error": "get_project_access_service_contract is not callable",
            },
            False,
        )

    try:
        raw_contract = contract_builder()
    except Exception as exc:
        return (
            {
                "ready": False,
                "error": f"{type(exc).__name__}: {exc}",
            },
            False,
        )

    if not isinstance(raw_contract, Mapping):
        return (
            {
                "ready": False,
                "error": "service contract is not a mapping",
                "receivedType": type(raw_contract).__name__,
            },
            False,
        )

    contract = dict(raw_contract)
    return contract, bool(contract.get("ready", False))


@lru_cache(maxsize=2)
def get_project_access_package_status(
    *,
    include_service_contract: bool = True,
) -> ProjectAccessPackageStatus:
    """Ermittelt den vollständigen DB-freien Paket- und Importstatus."""

    module = _load_service_module()
    record = get_project_access_import_record(load=False)

    if record is None:
        record = ProjectAccessPackageImportRecord(
            module_path=SERVICE_MODULE_PATH,
            imported=False,
            missing_symbols=EXPECTED_SERVICE_SYMBOLS,
            error="service import was not attempted",
        )

    warnings: list[str] = []
    service_contract: dict[str, Any] = {}
    service_contract_ready: Optional[bool] = None

    if record.unexpected_declared_symbols:
        warnings.append(
            "service.py exports additional symbols not covered by the "
            "package facade"
        )

    if include_service_contract and module is not None:
        service_contract, service_contract_ready = _read_service_contract(
            module
        )

    ready = record.contract_complete
    if include_service_contract:
        ready = ready and service_contract_ready is True

    return ProjectAccessPackageStatus(
        package_name=__name__,
        package_version=PROJECT_ACCESS_PACKAGE_VERSION,
        schema_version=PROJECT_ACCESS_PACKAGE_SCHEMA_VERSION,
        ready=ready,
        import_record=record,
        service_contract_ready=service_contract_ready,
        service_contract=service_contract,
        warnings=tuple(warnings),
    )


def get_project_access_package_contract() -> dict[str, Any]:
    """Liefert einen kompakten Vertrag für Readiness- und Statusrouten."""

    status = get_project_access_package_status(
        include_service_contract=True
    )

    return {
        "packageVersion": PROJECT_ACCESS_PACKAGE_VERSION,
        "schemaVersion": PROJECT_ACCESS_PACKAGE_SCHEMA_VERSION,
        "ready": status.ready,
        "serviceModule": SERVICE_MODULE_PATH,
        "serviceImported": status.import_record.imported,
        "serviceContractComplete": status.import_record.contract_complete,
        "serviceContractReady": status.service_contract_ready,
        "missingSymbols": list(status.import_record.missing_symbols),
        "unexpectedDeclaredSymbols": list(
            status.import_record.unexpected_declared_symbols
        ),
        "lazyImport": True,
        "transactionNeutral": True,
        "databaseQueries": False,
        "commitsInternally": False,
        "rollbacksInternally": False,
        "ormStateCached": False,
        "publicServiceSymbols": list(EXPECTED_SERVICE_SYMBOLS),
        "warnings": list(status.warnings),
        "importError": status.import_record.error,
    }


def is_project_access_package_ready() -> bool:
    """Gibt ``True`` zurück, wenn Import- und Servicevertrag vollständig sind."""

    try:
        return get_project_access_package_status(
            include_service_contract=True
        ).ready
    except Exception:
        return False


def require_project_access_package_ready() -> None:
    """Löst aus, wenn die öffentliche Access-Service-API unvollständig ist."""

    status = get_project_access_package_status(
        include_service_contract=True
    )
    if status.ready:
        return

    raise RuntimeError(
        "vectoplan-chunk project_access package is not ready. "
        f"Status: {status.to_dict(include_traceback=True)}"
    )


def _clear_status_caches() -> None:
    """Leert ausschließlich lokale Diagnose- und API-Mapping-Caches."""

    try:
        get_project_access_package_status.cache_clear()
    except Exception:
        pass

    try:
        get_project_access_public_api.cache_clear()
    except Exception:
        pass


def reset_project_access_package_cache(
    *,
    retry_failed_import: bool = False,
    clear_service_pure_caches: bool = True,
) -> dict[str, Any]:
    """
    Leert Paketdiagnose und optional reine Service-Caches.

    Das bereits erfolgreich importierte Python-Modul wird absichtlich nicht
    aus ``sys.modules`` entfernt oder neu geladen. Ein Reload könnte
    SQLAlchemy-Klassen doppelt registrieren. Fehlgeschlagene Imports können
    hingegen kontrolliert erneut versucht werden.
    """

    global _SERVICE_IMPORT_RECORD

    service_cache_result: Any = None
    service_cache_error: Optional[str] = None

    with _IMPORT_LOCK:
        module = _SERVICE_MODULE
        if retry_failed_import and module is None:
            _SERVICE_IMPORT_RECORD = None
            importlib.invalidate_caches()

        _clear_status_caches()

    if clear_service_pure_caches and module is not None:
        try:
            clear_service_caches = getattr(
                module,
                "clear_project_access_service_caches",
                None,
            )
            if callable(clear_service_caches):
                service_cache_result = clear_service_caches()
        except Exception as exc:
            service_cache_error = f"{type(exc).__name__}: {exc}"

    retried = False
    if retry_failed_import and module is None:
        retried = True
        _load_service_module(retry_failed=True)

    record = get_project_access_import_record(load=False)

    return {
        "ok": service_cache_error is None,
        "packageCachesCleared": True,
        "servicePureCachesRequested": clear_service_pure_caches,
        "servicePureCacheResult": _make_json_safe(service_cache_result),
        "servicePureCacheError": service_cache_error,
        "failedImportRetried": retried,
        "serviceImported": bool(record and record.imported),
        "importError": record.error if record else None,
    }


def __getattr__(name: str) -> Any:
    """PEP-562-Lazy-Export für die ausdrücklich freigegebene Service-API."""

    if name in EXPECTED_SERVICE_SYMBOLS:
        try:
            return require_project_access_service_symbol(name)
        except Exception as exc:
            raise AttributeError(
                f"module {__name__!r} cannot provide {name!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Liefert deterministische Namen für IDEs und Introspektionswerkzeuge."""

    names = set(globals())
    names.update(PACKAGE_PUBLIC_SYMBOLS)
    names.update(EXPECTED_SERVICE_SYMBOLS)
    return sorted(name for name in names if not name.startswith("_"))


__all__ = [
    *PACKAGE_PUBLIC_SYMBOLS,
    *EXPECTED_SERVICE_SYMBOLS,
]
