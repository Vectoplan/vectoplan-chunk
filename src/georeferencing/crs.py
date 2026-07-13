# services/vectoplan-chunk/src/georeferencing/crs.py
"""Kontrollierte pyproj-Integration für CRS-Auflösung und Runtime-Readiness.

Dieses Modul ist die einzige vorgesehene Eingangsschicht für CRS-Eingaben im
Earth-v1-Slice. Es kapselt ``pyproj`` und die lokale PROJ-Runtime, damit
Application-, World- und HTTP-Schichten keine eigenen CRS-Parser oder
abweichenden Validierungsregeln implementieren.

Verantwortlichkeiten
---------------------
* kontrolliertes, spätes Laden von ``pyproj``;
* Auflösen unterstützter User-Inputs in ``CrsDefinition``;
* kanonische Speicherung als WKT2:2019;
* Analyse von Achsen, Einheiten, Dimension und CRS-Typ;
* Authority- und CRS-Allowlist-Prüfung;
* Ablehnung veralteter, vertikaler oder Engineering-CRS gemäß Policy;
* Prüfung der lokalen PROJ-Datenbank und der kanonischen Earth-CRS;
* explizite Kontrolle des PROJ-Netzwerkzugriffs;
* begrenzte, jederzeit löschbare In-Process-Caches.

Nicht verantwortlich
---------------------
* Auswahl einer konkreten Transformation zwischen zwei CRS;
* eigentliche Koordinatentransformation;
* Earth-Grid-Projektion;
* Datenbankpersistenz;
* HTTP-Antworten, Logging oder Transaktionssteuerung.

Sicherheits- und Reproduzierbarkeitsregeln
------------------------------------------
* Ein CRS wird nie aus Zahlenwerten geraten.
* Kanonische Persistenz verwendet WKT2:2019, auch wenn eine Authority-ID
  vorhanden ist.
* Netzwerkzugriff wird nicht beim Import verändert. Die Runtime initialisiert
  die Policy explizit über ``configure_proj_network``.
* Earth v1 erwartet im normalen Serverbetrieb deaktiviertes PROJ-Netzwerk.
* Fehler enthalten keine vollständigen langen WKT-/PROJJSON-Payloads.
* Caches enthalten ausschließlich aus Eingaben reproduzierbare CRS-Objekte.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import re
from threading import RLock
from types import ModuleType
from typing import Any, Final, Iterable, Self

from ..coordinates.models import JsonValue
from .contracts import (
    CoordinateDimension,
    CrsDefinition,
    CrsDefinitionFormat,
)
from .errors import (
    CrsDimensionMismatchError,
    CrsInvalidError,
    CrsRequiredError,
    CrsUnsupportedError,
    GeoreferencingConfigurationError,
    GeoreferencingValidationError,
    ProjDatabaseUnavailableError,
    PyprojUnavailableError,
    summarize_crs_input,
)


MINIMUM_PYPROJ_VERSION: Final[str] = "3.7.0"
CANONICAL_GEOGRAPHIC_CRS_ID: Final[str] = "EPSG:4979"
CANONICAL_GEOCENTRIC_CRS_ID: Final[str] = "EPSG:4978"
CANONICAL_WKT_VERSION: Final[str] = "WKT2_2019"

_CRS_CACHE_SIZE: Final[int] = 256
_MAX_INPUT_LENGTH: Final[int] = 1_048_576
_MAX_MAPPING_DEPTH: Final[int] = 24
_MAX_MAPPING_ITEMS: Final[int] = 100_000
_MAX_ALLOWLIST_ITEMS: Final[int] = 4_096
_CUSTOM_CRS_PREFIX: Final[str] = "CUSTOM"
_CRS_AUTHORITY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9_.-]+$"
)
_VERSION_COMPONENT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d+")

_RUNTIME_LOCK = RLock()
_PYPROJ_MODULE: ModuleType | None = None
_PYPROJ_IMPORT_ERROR: BaseException | None = None


class CrsInputKind(str):
    """Interne, stabile Kennzeichnung normalisierter CRS-Eingaben."""

    CONTRACT = "contract"
    AUTHORITY = "authority"
    INTEGER = "integer"
    STRING = "string"
    MAPPING = "mapping"
    NATIVE = "native"


@dataclass(frozen=True, slots=True)
class CrsResolutionPolicy:
    """Validierungsvertrag für eine aufgelöste CRS-Definition."""

    role: str = "reference"
    allowed_dimensions: tuple[CoordinateDimension, ...] = (
        CoordinateDimension.TWO_D,
        CoordinateDimension.THREE_D,
    )
    allowed_authorities: tuple[str, ...] = ()
    allowed_crs_ids: tuple[str, ...] = ()
    allow_geographic: bool = True
    allow_projected: bool = True
    allow_geocentric: bool = True
    allow_compound: bool = True
    allow_vertical_only: bool = False
    allow_engineering: bool = False
    allow_bound: bool = True
    allow_deprecated: bool = False
    require_authority_match: bool = False
    minimum_authority_confidence: int = 100

    def __post_init__(self) -> None:
        normalized_role = _normalize_role(self.role)
        normalized_dimensions = _normalize_dimensions(
            self.allowed_dimensions
        )
        normalized_authorities = _normalize_identifier_tuple(
            self.allowed_authorities,
            field_name="allowedAuthorities",
            uppercase=True,
        )
        normalized_crs_ids = _normalize_identifier_tuple(
            self.allowed_crs_ids,
            field_name="allowedCrsIds",
            uppercase=False,
        )
        confidence = _normalize_confidence(
            self.minimum_authority_confidence
        )

        object.__setattr__(self, "role", normalized_role)
        object.__setattr__(
            self,
            "allowed_dimensions",
            normalized_dimensions,
        )
        object.__setattr__(
            self,
            "allowed_authorities",
            normalized_authorities,
        )
        object.__setattr__(
            self,
            "allowed_crs_ids",
            normalized_crs_ids,
        )
        object.__setattr__(
            self,
            "allow_geographic",
            bool(self.allow_geographic),
        )
        object.__setattr__(
            self,
            "allow_projected",
            bool(self.allow_projected),
        )
        object.__setattr__(
            self,
            "allow_geocentric",
            bool(self.allow_geocentric),
        )
        object.__setattr__(
            self,
            "allow_compound",
            bool(self.allow_compound),
        )
        object.__setattr__(
            self,
            "allow_vertical_only",
            bool(self.allow_vertical_only),
        )
        object.__setattr__(
            self,
            "allow_engineering",
            bool(self.allow_engineering),
        )
        object.__setattr__(
            self,
            "allow_bound",
            bool(self.allow_bound),
        )
        object.__setattr__(
            self,
            "allow_deprecated",
            bool(self.allow_deprecated),
        )
        object.__setattr__(
            self,
            "require_authority_match",
            bool(self.require_authority_match),
        )
        object.__setattr__(
            self,
            "minimum_authority_confidence",
            confidence,
        )

    @classmethod
    def earth_reference_default(cls) -> Self:
        """Strikte Standardpolicy für den globalen Earth-Referenzpunkt."""

        return cls(
            role="reference",
            allow_geographic=True,
            allow_projected=True,
            allow_geocentric=True,
            allow_compound=True,
            allow_vertical_only=False,
            allow_engineering=False,
            allow_bound=True,
            allow_deprecated=False,
            require_authority_match=False,
            minimum_authority_confidence=100,
        )

    @classmethod
    def source_dataset_default(cls) -> Self:
        """Policy für explizit deklarierte Quell-CRS späterer Importe."""

        return cls(
            role="source",
            allow_geographic=True,
            allow_projected=True,
            allow_geocentric=True,
            allow_compound=True,
            allow_vertical_only=False,
            allow_engineering=False,
            allow_bound=True,
            allow_deprecated=False,
            require_authority_match=False,
            minimum_authority_confidence=100,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "role": self.role,
            "allowedDimensions": [
                int(value) for value in self.allowed_dimensions
            ],
            "allowedAuthorities": list(self.allowed_authorities),
            "allowedCrsIds": list(self.allowed_crs_ids),
            "allowGeographic": self.allow_geographic,
            "allowProjected": self.allow_projected,
            "allowGeocentric": self.allow_geocentric,
            "allowCompound": self.allow_compound,
            "allowVerticalOnly": self.allow_vertical_only,
            "allowEngineering": self.allow_engineering,
            "allowBound": self.allow_bound,
            "allowDeprecated": self.allow_deprecated,
            "requireAuthorityMatch": self.require_authority_match,
            "minimumAuthorityConfidence": (
                self.minimum_authority_confidence
            ),
        }


@dataclass(frozen=True, slots=True)
class CrsInspection:
    """Nicht-persistente Diagnose einer nativen pyproj-CRS-Instanz."""

    crs_id: str
    type_name: str
    authority: str | None
    code: str | None
    dimension: CoordinateDimension
    axis_names: tuple[str, ...]
    axis_directions: tuple[str, ...]
    unit_names: tuple[str, ...]
    is_geographic: bool
    is_projected: bool
    is_geocentric: bool
    is_vertical: bool
    is_compound: bool
    is_engineering: bool
    is_bound: bool
    is_deprecated: bool
    area_of_use_name: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "crsId": self.crs_id,
            "typeName": self.type_name,
            "authority": self.authority,
            "code": self.code,
            "dimension": int(self.dimension),
            "axisNames": list(self.axis_names),
            "axisDirections": list(self.axis_directions),
            "unitNames": list(self.unit_names),
            "isGeographic": self.is_geographic,
            "isProjected": self.is_projected,
            "isGeocentric": self.is_geocentric,
            "isVertical": self.is_vertical,
            "isCompound": self.is_compound,
            "isEngineering": self.is_engineering,
            "isBound": self.is_bound,
            "isDeprecated": self.is_deprecated,
            "areaOfUseName": self.area_of_use_name,
        }


def resolve_crs(
    value: Any,
    *,
    policy: CrsResolutionPolicy | None = None,
) -> CrsDefinition:
    """Löst eine CRS-Eingabe in einen kanonischen Persistenzvertrag auf.

    Unterstützt werden ausschließlich Eingaben, die ``pyproj.CRS`` eindeutig
    verarbeiten kann, darunter Authority-Codes, EPSG-Integer, WKT, PROJJSON,
    PROJ-Strings, Authority-Tupel, Mapping-Payloads, native CRS-Objekte und
    bereits kanonisierte ``CrsDefinition``-Objekte.
    """

    active_policy = (
        policy
        if policy is not None
        else CrsResolutionPolicy.earth_reference_default()
    )
    if not isinstance(active_policy, CrsResolutionPolicy):
        raise GeoreferencingValidationError(
            "policy muss eine CrsResolutionPolicy sein.",
            details={"actualType": type(active_policy).__name__},
        )

    kind, payload = _normalize_crs_input(value, role=active_policy.role)
    native = _resolve_native_crs_cached(kind, payload)
    inspection = _inspect_native_crs(
        native,
        minimum_authority_confidence=(
            active_policy.minimum_authority_confidence
        ),
    )
    _validate_native_crs_against_policy(
        native=native,
        inspection=inspection,
        policy=active_policy,
    )

    definition = _build_contract(
        native,
        inspection=inspection,
    )
    _validate_contract_against_policy(
        definition,
        inspection=inspection,
        policy=active_policy,
    )
    return definition


def resolve_native_crs(
    value: Any,
    *,
    policy: CrsResolutionPolicy | None = None,
) -> Any:
    """Liefert eine validierte native ``pyproj.CRS``-Instanz.

    Diese Funktion ist für ``transformer.py`` und interne Diagnostik gedacht.
    Aufrufende Schichten dürfen das native Objekt nicht persistieren.
    """

    active_policy = (
        policy
        if policy is not None
        else CrsResolutionPolicy.earth_reference_default()
    )
    if not isinstance(active_policy, CrsResolutionPolicy):
        raise GeoreferencingValidationError(
            "policy muss eine CrsResolutionPolicy sein.",
            details={"actualType": type(active_policy).__name__},
        )

    kind, payload = _normalize_crs_input(value, role=active_policy.role)
    native = _resolve_native_crs_cached(kind, payload)
    inspection = _inspect_native_crs(
        native,
        minimum_authority_confidence=(
            active_policy.minimum_authority_confidence
        ),
    )
    _validate_native_crs_against_policy(
        native=native,
        inspection=inspection,
        policy=active_policy,
    )
    _validate_contract_against_policy(
        _build_contract(native, inspection=inspection),
        inspection=inspection,
        policy=active_policy,
    )
    return native


def inspect_crs(
    value: Any,
    *,
    minimum_authority_confidence: int = 100,
) -> CrsInspection:
    """Liefert eine sichere native CRS-Diagnose ohne vollständige Definition."""

    confidence = _normalize_confidence(
        minimum_authority_confidence
    )
    kind, payload = _normalize_crs_input(value, role="reference")
    native = _resolve_native_crs_cached(kind, payload)
    return _inspect_native_crs(
        native,
        minimum_authority_confidence=confidence,
    )


def crs_equivalent(
    first: Any,
    second: Any,
    *,
    ignore_axis_order: bool = False,
) -> bool:
    """Prüft semantische CRS-Äquivalenz mit pyproj."""

    first_native = _resolve_native_without_policy(first)
    second_native = _resolve_native_without_policy(second)

    try:
        return bool(
            first_native.equals(
                second_native,
                ignore_axis_order=bool(ignore_axis_order),
            )
        )
    except Exception as error:
        raise CrsInvalidError.for_value(
            {
                "first": summarize_crs_input(first),
                "second": summarize_crs_input(second),
            },
            role="reference",
            reason=(
                "CRS-Äquivalenzprüfung ist fehlgeschlagen: "
                f"{type(error).__name__}."
            ),
        ) from error


def canonical_geographic_crs() -> CrsDefinition:
    """Kanonischer WGS-84-3D-Vertrag für globale geografische Positionen."""

    return _canonical_geographic_crs_cached()


@lru_cache(maxsize=1)
def _canonical_geographic_crs_cached() -> CrsDefinition:
    return resolve_crs(
        CANONICAL_GEOGRAPHIC_CRS_ID,
        policy=CrsResolutionPolicy(
            role="geographic",
            allowed_dimensions=(CoordinateDimension.THREE_D,),
            allowed_crs_ids=(CANONICAL_GEOGRAPHIC_CRS_ID,),
            allow_geographic=True,
            allow_projected=False,
            allow_geocentric=False,
            allow_compound=False,
            allow_vertical_only=False,
            allow_engineering=False,
            allow_bound=False,
            allow_deprecated=False,
            require_authority_match=True,
            minimum_authority_confidence=100,
        ),
    )


def canonical_geocentric_crs() -> CrsDefinition:
    """Kanonischer WGS-84-ECEF-Vertrag für globale kartesische Positionen."""

    return _canonical_geocentric_crs_cached()


@lru_cache(maxsize=1)
def _canonical_geocentric_crs_cached() -> CrsDefinition:
    return resolve_crs(
        CANONICAL_GEOCENTRIC_CRS_ID,
        policy=CrsResolutionPolicy(
            role="geocentric",
            allowed_dimensions=(CoordinateDimension.THREE_D,),
            allowed_crs_ids=(CANONICAL_GEOCENTRIC_CRS_ID,),
            allow_geographic=False,
            allow_projected=False,
            allow_geocentric=True,
            allow_compound=False,
            allow_vertical_only=False,
            allow_engineering=False,
            allow_bound=False,
            allow_deprecated=False,
            require_authority_match=True,
            minimum_authority_confidence=100,
        ),
    )


def configure_proj_network(
    *,
    enabled: bool = False,
) -> dict[str, JsonValue]:
    """Setzt den globalen PROJ-Netzwerkstatus explizit.

    Earth v1 soll benötigte Transformationsressourcen reproduzierbar im Image
    ausliefern. Daher ist ``enabled=False`` der vorgesehene Runtime-Standard.
    """

    pyproj = _load_pyproj()

    try:
        network = import_module("pyproj.network")
        with _RUNTIME_LOCK:
            network.set_network_enabled(active=bool(enabled))
            actual = bool(network.is_network_enabled())
    except Exception as error:
        raise GeoreferencingConfigurationError(
            "Der PROJ-Netzwerkstatus konnte nicht gesetzt werden.",
            details={
                "requestedEnabled": bool(enabled),
                "causeType": type(error).__name__,
            },
            cause=error,
        ) from error

    if actual is not bool(enabled):
        raise GeoreferencingConfigurationError(
            "Der PROJ-Netzwerkstatus entspricht nicht der Anforderung.",
            details={
                "requestedEnabled": bool(enabled),
                "actualEnabled": actual,
                "pyprojVersion": getattr(pyproj, "__version__", None),
            },
        )

    return {
        "ok": True,
        "requestedEnabled": bool(enabled),
        "networkEnabled": actual,
    }


def crs_runtime_status(
    *,
    require_network_disabled: bool = True,
    validate_required_crs: bool = True,
) -> dict[str, JsonValue]:
    """Prüft pyproj, PROJ-Datenbank und kanonische Earth-CRS read-only."""

    status: dict[str, JsonValue] = {
        "ok": False,
        "pyprojAvailable": False,
        "pyprojVersion": None,
        "minimumPyprojVersion": MINIMUM_PYPROJ_VERSION,
        "pyprojVersionReady": False,
        "projVersion": None,
        "projDataDirReady": False,
        "projDataDirFingerprint": None,
        "projDatabaseReady": False,
        "projDatabaseMetadata": {},
        "authorityCount": None,
        "networkEnabled": None,
        "networkPolicyReady": False,
        "requiredCrsReady": False,
        "requiredCrs": [],
        "errors": [],
    }

    errors: list[JsonValue] = status["errors"]  # type: ignore[assignment]

    try:
        pyproj = _load_pyproj()
    except Exception as error:
        errors.append(_safe_error(error))
        return status

    status["pyprojAvailable"] = True
    pyproj_version = str(getattr(pyproj, "__version__", "") or "")
    status["pyprojVersion"] = pyproj_version
    status["projVersion"] = str(
        getattr(pyproj, "proj_version_str", "") or ""
    )
    status["pyprojVersionReady"] = _version_at_least(
        pyproj_version,
        MINIMUM_PYPROJ_VERSION,
    )

    try:
        data_dir = _get_proj_data_dir()
        status["projDataDirReady"] = data_dir is not None
        status["projDataDirFingerprint"] = (
            _path_fingerprint(data_dir)
            if data_dir is not None
            else None
        )
        status["projDatabaseReady"] = (
            _find_proj_database(data_dir) is not None
            if data_dir is not None
            else False
        )
    except Exception as error:
        errors.append(_safe_error(error))

    try:
        database = import_module("pyproj.database")
        metadata_keys = (
            "EPSG.VERSION",
            "EPSG.DATE",
            "PROJ.VERSION",
            "PROJ_DATA.VERSION",
        )
        metadata: dict[str, JsonValue] = {}
        for key in metadata_keys:
            try:
                metadata[key] = database.get_database_metadata(key)
            except Exception as error:
                metadata[key] = {
                    "errorType": type(error).__name__,
                }

        status["projDatabaseMetadata"] = metadata
        authorities = database.get_authorities()
        status["authorityCount"] = len(authorities)
        if authorities:
            status["projDatabaseReady"] = bool(
                status["projDatabaseReady"]
            )
    except Exception as error:
        errors.append(_safe_error(error))
        status["projDatabaseReady"] = False

    try:
        network = import_module("pyproj.network")
        network_enabled = bool(network.is_network_enabled())
        status["networkEnabled"] = network_enabled
        status["networkPolicyReady"] = (
            not network_enabled
            if require_network_disabled
            else True
        )
    except Exception as error:
        errors.append(_safe_error(error))

    required_results: list[JsonValue] = []
    if validate_required_crs:
        for crs_id, expected_type in (
            (CANONICAL_GEOGRAPHIC_CRS_ID, "geographic"),
            (CANONICAL_GEOCENTRIC_CRS_ID, "geocentric"),
        ):
            try:
                inspection = inspect_crs(
                    crs_id,
                    minimum_authority_confidence=100,
                )
                type_ready = (
                    inspection.is_geographic
                    if expected_type == "geographic"
                    else inspection.is_geocentric
                )
                required_results.append(
                    {
                        "crsId": crs_id,
                        "ready": type_ready,
                        "dimension": int(inspection.dimension),
                        "typeName": inspection.type_name,
                    }
                )
            except Exception as error:
                required_results.append(
                    {
                        "crsId": crs_id,
                        "ready": False,
                        "error": _safe_error(error),
                    }
                )

        status["requiredCrsReady"] = all(
            bool(item.get("ready"))
            for item in required_results
            if isinstance(item, dict)
        )
    else:
        status["requiredCrsReady"] = True

    status["requiredCrs"] = required_results
    status["ok"] = bool(
        status["pyprojAvailable"]
        and status["pyprojVersionReady"]
        and status["projDataDirReady"]
        and status["projDatabaseReady"]
        and status["networkPolicyReady"]
        and status["requiredCrsReady"]
        and not errors
    )
    return status


def ensure_crs_runtime_ready(
    *,
    require_network_disabled: bool = True,
) -> dict[str, JsonValue]:
    """Prüft die CRS-Runtime und löst bei Unreadiness einen Domänenfehler aus."""

    status = crs_runtime_status(
        require_network_disabled=require_network_disabled,
        validate_required_crs=True,
    )

    if not status["pyprojAvailable"]:
        raise PyprojUnavailableError.create(
            required_version=MINIMUM_PYPROJ_VERSION
        )

    if not status["pyprojVersionReady"]:
        raise GeoreferencingConfigurationError(
            "Die installierte pyproj-Version ist zu alt.",
            details={
                "installedVersion": status["pyprojVersion"],
                "minimumVersion": MINIMUM_PYPROJ_VERSION,
            },
        )

    if not status["projDataDirReady"] or not status["projDatabaseReady"]:
        raise ProjDatabaseUnavailableError.create()

    if not status["networkPolicyReady"]:
        raise GeoreferencingConfigurationError(
            "Das PROJ-Netzwerk ist entgegen der Runtime-Policy aktiviert.",
            details={
                "networkEnabled": status["networkEnabled"],
                "requiredNetworkEnabled": (
                    False if require_network_disabled else None
                ),
            },
        )

    if not status["requiredCrsReady"]:
        raise GeoreferencingConfigurationError(
            "Die kanonischen Earth-CRS sind nicht vollständig verfügbar.",
            details={
                "requiredCrs": status["requiredCrs"],
            },
        )

    if status["errors"]:
        raise GeoreferencingConfigurationError(
            "Die CRS-Runtime meldet Diagnosefehler.",
            details={
                "errors": status["errors"],
            },
        )

    return status


def clear_crs_caches() -> None:
    """Leert ausschließlich aus Eingaben reproduzierbare CRS-Caches."""

    _resolve_native_crs_cached.cache_clear()
    _canonical_geographic_crs_cached.cache_clear()
    _canonical_geocentric_crs_cached.cache_clear()


def crs_cache_info() -> dict[str, JsonValue]:
    """Liefert serialisierbare Diagnose der begrenzten CRS-Caches."""

    return {
        "nativeCrs": _cache_info_to_dict(
            _resolve_native_crs_cached.cache_info()
        ),
        "canonicalGeographic": _cache_info_to_dict(
            _canonical_geographic_crs_cached.cache_info()
        ),
        "canonicalGeocentric": _cache_info_to_dict(
            _canonical_geocentric_crs_cached.cache_info()
        ),
    }


def _load_pyproj() -> ModuleType:
    global _PYPROJ_MODULE
    global _PYPROJ_IMPORT_ERROR

    with _RUNTIME_LOCK:
        if _PYPROJ_MODULE is not None:
            return _PYPROJ_MODULE

        if _PYPROJ_IMPORT_ERROR is not None:
            raise PyprojUnavailableError.create(
                required_version=MINIMUM_PYPROJ_VERSION
            ) from _PYPROJ_IMPORT_ERROR

        try:
            module = import_module("pyproj")
        except (ModuleNotFoundError, ImportError) as error:
            _PYPROJ_IMPORT_ERROR = error
            raise PyprojUnavailableError.create(
                required_version=MINIMUM_PYPROJ_VERSION
            ) from error
        except Exception as error:
            _PYPROJ_IMPORT_ERROR = error
            raise PyprojUnavailableError.create(
                required_version=MINIMUM_PYPROJ_VERSION
            ) from error

        installed_version = str(
            getattr(module, "__version__", "") or ""
        )
        if not installed_version:
            try:
                installed_version = package_version("pyproj")
            except PackageNotFoundError:
                installed_version = ""

        if not _version_at_least(
            installed_version,
            MINIMUM_PYPROJ_VERSION,
        ):
            raise GeoreferencingConfigurationError(
                "Die installierte pyproj-Version ist zu alt.",
                details={
                    "installedVersion": installed_version or None,
                    "minimumVersion": MINIMUM_PYPROJ_VERSION,
                },
            )

        _PYPROJ_MODULE = module
        return module


def _normalize_crs_input(
    value: Any,
    *,
    role: str,
) -> tuple[str, str]:
    if value is None:
        raise CrsRequiredError.for_role(role)

    if isinstance(value, CrsDefinition):
        return CrsInputKind.CONTRACT, _serialize_contract_input(value)

    if isinstance(value, bool):
        raise CrsInvalidError.for_value(
            value,
            role=role,
            reason="Bool ist keine gültige CRS-Eingabe.",
        )

    if isinstance(value, int):
        if value <= 0:
            raise CrsInvalidError.for_value(
                value,
                role=role,
                reason="Numerische EPSG-Codes müssen positiv sein.",
            )
        return CrsInputKind.INTEGER, str(value)

    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise CrsRequiredError.for_role(role)
        if len(normalized) > _MAX_INPUT_LENGTH:
            raise CrsInvalidError.for_value(
                normalized,
                role=role,
                reason="CRS-Eingabe überschreitet die maximale Länge.",
            )
        return CrsInputKind.STRING, normalized

    if isinstance(value, Mapping):
        canonical = _canonical_json_mapping(value, role=role)
        return CrsInputKind.MAPPING, canonical

    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 2
    ):
        authority = str(value[0]).strip()
        code = str(value[1]).strip()
        if not authority or not code:
            raise CrsInvalidError.for_value(
                value,
                role=role,
                reason="Authority-Tupel benötigt Authority und Code.",
            )
        token = f"{authority}:{code}"
        if not _CRS_AUTHORITY_PATTERN.fullmatch(token):
            raise CrsInvalidError.for_value(
                token,
                role=role,
                reason="Authority-Tupel besitzt ein ungültiges Format.",
            )
        return CrsInputKind.AUTHORITY, token

    pyproj = _load_pyproj()
    crs_type = getattr(pyproj, "CRS", None)
    if crs_type is not None and isinstance(value, crs_type):
        try:
            wkt = value.to_wkt(
                version=CANONICAL_WKT_VERSION,
                pretty=False,
            )
        except Exception as error:
            raise CrsInvalidError.for_value(
                value,
                role=role,
                reason=(
                    "Native CRS konnte nicht als WKT2:2019 "
                    f"serialisiert werden: {type(error).__name__}."
                ),
            ) from error
        return CrsInputKind.NATIVE, wkt

    to_wkt = getattr(value, "to_wkt", None)
    if callable(to_wkt):
        try:
            wkt = to_wkt()
        except Exception as error:
            raise CrsInvalidError.for_value(
                value,
                role=role,
                reason=(
                    "to_wkt()-Eingabe konnte nicht gelesen werden: "
                    f"{type(error).__name__}."
                ),
            ) from error
        if not isinstance(wkt, str) or not wkt.strip():
            raise CrsInvalidError.for_value(
                value,
                role=role,
                reason="to_wkt() lieferte keine gültige Zeichenfolge.",
            )
        return CrsInputKind.NATIVE, wkt.strip()

    raise CrsInvalidError.for_value(
        value,
        role=role,
        reason="Nicht unterstützter CRS-Eingabetyp.",
    )


def _serialize_contract_input(value: CrsDefinition) -> str:
    payload = {
        "definitionFormat": value.definition_format.value,
        "definition": value.definition,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


@lru_cache(maxsize=_CRS_CACHE_SIZE)
def _resolve_native_crs_cached(
    kind: str,
    payload: str,
) -> Any:
    pyproj = _load_pyproj()
    crs_type = pyproj.CRS

    try:
        if kind == CrsInputKind.CONTRACT:
            contract_payload = json.loads(payload)
            definition = contract_payload["definition"]
            native = crs_type.from_user_input(definition)
        elif kind == CrsInputKind.INTEGER:
            native = crs_type.from_epsg(int(payload))
        elif kind == CrsInputKind.AUTHORITY:
            authority, code = payload.split(":", 1)
            native = crs_type.from_authority(authority, code)
        elif kind == CrsInputKind.MAPPING:
            native = crs_type.from_user_input(json.loads(payload))
        elif kind in {CrsInputKind.STRING, CrsInputKind.NATIVE}:
            native = crs_type.from_user_input(payload)
        else:
            raise CrsInvalidError.for_value(
                {"kind": kind},
                role="reference",
                reason="Interner CRS-Eingabetyp wird nicht unterstützt.",
            )
    except CrsInvalidError:
        raise
    except Exception as error:
        raise CrsInvalidError.for_value(
            payload,
            role="reference",
            reason=(
                "pyproj konnte die CRS-Eingabe nicht auflösen: "
                f"{type(error).__name__}."
            ),
        ) from error

    return native


def _resolve_native_without_policy(value: Any) -> Any:
    kind, payload = _normalize_crs_input(value, role="reference")
    return _resolve_native_crs_cached(kind, payload)


def _inspect_native_crs(
    native: Any,
    *,
    minimum_authority_confidence: int,
) -> CrsInspection:
    try:
        axis_info = tuple(native.axis_info or ())
    except Exception as error:
        raise CrsInvalidError.for_value(
            native,
            role="reference",
            reason=(
                "CRS-Achseninformationen konnten nicht gelesen werden: "
                f"{type(error).__name__}."
            ),
        ) from error

    dimension_value = len(axis_info)
    if dimension_value not in {2, 3}:
        raise CrsDimensionMismatchError.for_dimensions(
            crs=_safe_native_name(native),
            expected_dimensions=3 if getattr(
                native,
                "is_geocentric",
                False,
            ) else 2,
            actual_dimensions=dimension_value,
        )

    dimension = CoordinateDimension(dimension_value)

    axis_names: list[str] = []
    axis_directions: list[str] = []
    unit_names: list[str] = []
    all_units_present = True

    for index, axis in enumerate(axis_info):
        name = (
            _safe_text(getattr(axis, "name", None))
            or _safe_text(getattr(axis, "abbrev", None))
            or _safe_text(getattr(axis, "direction", None))
            or f"axis-{index + 1}"
        )
        direction = (
            _safe_text(getattr(axis, "direction", None))
            or "unknown"
        )
        unit_name = _safe_text(getattr(axis, "unit_name", None))

        axis_names.append(name)
        axis_directions.append(direction)
        if unit_name is None:
            all_units_present = False
        else:
            unit_names.append(unit_name)

    authority: str | None = None
    code: str | None = None
    try:
        authority_match = native.to_authority(
            min_confidence=minimum_authority_confidence
        )
        if authority_match is not None:
            authority = str(authority_match[0]).strip().upper()
            code = str(authority_match[1]).strip()
    except Exception:
        authority = None
        code = None

    canonical_wkt = _native_to_canonical_wkt(native)
    fingerprint = sha256(canonical_wkt.encode("utf-8")).hexdigest()
    crs_id = (
        f"{authority}:{code}"
        if authority and code
        else f"{_CUSTOM_CRS_PREFIX}:{fingerprint[:24]}"
    )

    area_name: str | None = None
    try:
        area = native.area_of_use
        area_name = _safe_text(
            getattr(area, "name", None)
            if area is not None
            else None
        )
    except Exception:
        area_name = None

    return CrsInspection(
        crs_id=crs_id,
        type_name=_safe_native_type_name(native),
        authority=authority,
        code=code,
        dimension=dimension,
        axis_names=tuple(axis_names),
        axis_directions=tuple(axis_directions),
        unit_names=(
            tuple(unit_names)
            if all_units_present
            else ()
        ),
        is_geographic=bool(
            getattr(native, "is_geographic", False)
        ),
        is_projected=bool(
            getattr(native, "is_projected", False)
        ),
        is_geocentric=bool(
            getattr(native, "is_geocentric", False)
        ),
        is_vertical=bool(
            getattr(native, "is_vertical", False)
        ),
        is_compound=bool(
            getattr(native, "is_compound", False)
        ),
        is_engineering=bool(
            getattr(native, "is_engineering", False)
        ),
        is_bound=bool(getattr(native, "is_bound", False)),
        is_deprecated=bool(
            getattr(native, "is_deprecated", False)
        ),
        area_of_use_name=area_name,
    )


def _build_contract(
    native: Any,
    *,
    inspection: CrsInspection,
) -> CrsDefinition:
    wkt = _native_to_canonical_wkt(native)

    return CrsDefinition(
        crs_id=inspection.crs_id,
        definition_format=CrsDefinitionFormat.WKT,
        definition=wkt,
        coordinate_dimension=inspection.dimension,
        authority=inspection.authority,
        code=inspection.code,
        name=_safe_native_name(native),
        axis_names=inspection.axis_names,
        unit_names=inspection.unit_names,
        is_geographic=inspection.is_geographic,
        is_projected=inspection.is_projected,
        is_geocentric=inspection.is_geocentric,
        is_vertical=inspection.is_vertical,
        is_compound=inspection.is_compound,
    )


def _validate_native_crs_against_policy(
    *,
    native: Any,
    inspection: CrsInspection,
    policy: CrsResolutionPolicy,
) -> None:
    if inspection.is_deprecated and not policy.allow_deprecated:
        raise CrsUnsupportedError.for_crs(
            inspection.crs_id,
            role=policy.role,
            operation="resolve-crs",
            allowed_crs_ids=policy.allowed_crs_ids or None,
        ).with_context(reason="deprecated_crs")

    if inspection.is_engineering and not policy.allow_engineering:
        raise CrsUnsupportedError.for_crs(
            inspection.crs_id,
            role=policy.role,
            operation="resolve-crs",
            allowed_crs_ids=policy.allowed_crs_ids or None,
        ).with_context(reason="engineering_crs_forbidden")

    if inspection.is_bound and not policy.allow_bound:
        raise CrsUnsupportedError.for_crs(
            inspection.crs_id,
            role=policy.role,
            operation="resolve-crs",
            allowed_crs_ids=policy.allowed_crs_ids or None,
        ).with_context(reason="bound_crs_forbidden")

    vertical_only = (
        inspection.is_vertical
        and not inspection.is_compound
        and not inspection.is_geographic
        and not inspection.is_projected
        and not inspection.is_geocentric
    )
    if vertical_only and not policy.allow_vertical_only:
        raise CrsUnsupportedError.for_crs(
            inspection.crs_id,
            role=policy.role,
            operation="resolve-crs",
            allowed_crs_ids=policy.allowed_crs_ids or None,
        ).with_context(reason="vertical_only_crs_forbidden")

    if (
        not any(
            (
                inspection.is_geographic,
                inspection.is_projected,
                inspection.is_geocentric,
                inspection.is_compound,
                vertical_only,
                inspection.is_engineering,
            )
        )
    ):
        raise CrsUnsupportedError.for_crs(
            inspection.crs_id,
            role=policy.role,
            operation="resolve-crs",
            allowed_crs_ids=policy.allowed_crs_ids or None,
        ).with_context(
            reason="unclassified_crs_type",
            typeName=inspection.type_name,
        )

    # Expliziter Zugriff hält die native Instanz während der Validierung
    # referenziert und macht die Schichtgrenze sichtbar.
    if native is None:
        raise CrsInvalidError.for_value(
            None,
            role=policy.role,
            reason="Native CRS fehlt.",
        )


def _validate_contract_against_policy(
    definition: CrsDefinition,
    *,
    inspection: CrsInspection,
    policy: CrsResolutionPolicy,
) -> None:
    if definition.coordinate_dimension not in policy.allowed_dimensions:
        raise CrsDimensionMismatchError.for_dimensions(
            crs=definition.crs_id,
            expected_dimensions=int(policy.allowed_dimensions[0]),
            actual_dimensions=int(
                definition.coordinate_dimension
            ),
        )

    if (
        inspection.is_geographic
        and not policy.allow_geographic
    ):
        _raise_type_unsupported(
            definition,
            policy=policy,
            reason="geographic_crs_forbidden",
        )

    if inspection.is_projected and not policy.allow_projected:
        _raise_type_unsupported(
            definition,
            policy=policy,
            reason="projected_crs_forbidden",
        )

    if inspection.is_geocentric and not policy.allow_geocentric:
        _raise_type_unsupported(
            definition,
            policy=policy,
            reason="geocentric_crs_forbidden",
        )

    if inspection.is_compound and not policy.allow_compound:
        _raise_type_unsupported(
            definition,
            policy=policy,
            reason="compound_crs_forbidden",
        )

    if policy.allowed_authorities:
        if (
            definition.authority is None
            or definition.authority.upper()
            not in policy.allowed_authorities
        ):
            raise CrsUnsupportedError.for_crs(
                definition.crs_id,
                role=policy.role,
                operation="authority-allowlist",
                allowed_crs_ids=policy.allowed_crs_ids or None,
            ).with_context(
                allowedAuthorities=list(
                    policy.allowed_authorities
                ),
                detectedAuthority=definition.authority,
            )

    if (
        policy.allowed_crs_ids
        and definition.crs_id not in policy.allowed_crs_ids
    ):
        raise CrsUnsupportedError.for_crs(
            definition.crs_id,
            role=policy.role,
            operation="crs-id-allowlist",
            allowed_crs_ids=policy.allowed_crs_ids,
        )

    if (
        policy.require_authority_match
        and (
            definition.authority is None
            or definition.code is None
        )
    ):
        raise CrsUnsupportedError.for_crs(
            definition.crs_id,
            role=policy.role,
            operation="authority-match-required",
            allowed_crs_ids=policy.allowed_crs_ids or None,
        )


def _raise_type_unsupported(
    definition: CrsDefinition,
    *,
    policy: CrsResolutionPolicy,
    reason: str,
) -> None:
    raise CrsUnsupportedError.for_crs(
        definition.crs_id,
        role=policy.role,
        operation="crs-type-policy",
        allowed_crs_ids=policy.allowed_crs_ids or None,
    ).with_context(reason=reason)


def _native_to_canonical_wkt(native: Any) -> str:
    try:
        wkt = native.to_wkt(
            version=CANONICAL_WKT_VERSION,
            pretty=False,
        )
    except Exception as error:
        raise CrsInvalidError.for_value(
            native,
            role="reference",
            reason=(
                "CRS konnte nicht als WKT2:2019 serialisiert werden: "
                f"{type(error).__name__}."
            ),
        ) from error

    if not isinstance(wkt, str) or not wkt.strip():
        raise CrsInvalidError.for_value(
            native,
            role="reference",
            reason="WKT2:2019-Serialisierung war leer.",
        )

    return wkt.strip()


def _canonical_json_mapping(
    value: Mapping[Any, Any],
    *,
    role: str,
) -> str:
    state = {
        "items": 0,
    }

    try:
        normalized = _normalize_json_value(
            value,
            depth=0,
            state=state,
        )
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except CrsInvalidError:
        raise
    except Exception as error:
        raise CrsInvalidError.for_value(
            value,
            role=role,
            reason=(
                "CRS-Mapping konnte nicht kanonisch serialisiert werden: "
                f"{type(error).__name__}."
            ),
        ) from error


def _normalize_json_value(
    value: Any,
    *,
    depth: int,
    state: dict[str, int],
) -> JsonValue:
    if depth > _MAX_MAPPING_DEPTH:
        raise CrsInvalidError.for_value(
            value,
            role="reference",
            reason="CRS-Mapping ist zu tief verschachtelt.",
        )

    state["items"] += 1
    if state["items"] > _MAX_MAPPING_ITEMS:
        raise CrsInvalidError.for_value(
            value,
            role="reference",
            reason="CRS-Mapping enthält zu viele Elemente.",
        )

    if value is None or isinstance(value, (str, int, bool)):
        return value

    if isinstance(value, float):
        if not (value == value and value not in (float("inf"), float("-inf"))):
            raise CrsInvalidError.for_value(
                value,
                role="reference",
                reason="CRS-Mapping enthält keine endliche Zahl.",
            )
        return value

    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key).strip()
            if not key:
                raise CrsInvalidError.for_value(
                    value,
                    role="reference",
                    reason="CRS-Mapping enthält einen leeren Schlüssel.",
                )
            result[key] = _normalize_json_value(
                raw_item,
                depth=depth + 1,
                state=state,
            )
        return result

    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
    ):
        return [
            _normalize_json_value(
                item,
                depth=depth + 1,
                state=state,
            )
            for item in value
        ]

    raise CrsInvalidError.for_value(
        value,
        role="reference",
        reason=(
            "CRS-Mapping enthält einen nicht JSON-kompatiblen Wert."
        ),
    )


def _get_proj_data_dir() -> Path | None:
    try:
        datadir = import_module("pyproj.datadir")
        value = datadir.get_data_dir()
    except Exception as error:
        raise ProjDatabaseUnavailableError.create() from error

    if not isinstance(value, str) or not value.strip():
        return None

    # PROJ-Datenpfade können plattformabhängig mehrere Einträge enthalten.
    for raw_path in value.split(os.pathsep):
        candidate = Path(raw_path.strip())
        if candidate.is_dir():
            return candidate.resolve()

    return None


def _find_proj_database(data_dir: Path) -> Path | None:
    candidate = data_dir / "proj.db"
    if candidate.is_file():
        return candidate
    return None


def _path_fingerprint(path: Path) -> str:
    return sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _safe_native_name(native: Any) -> str:
    return _safe_text(getattr(native, "name", None)) or "unnamed-crs"


def _safe_native_type_name(native: Any) -> str:
    return _safe_text(
        getattr(native, "type_name", None)
    ) or type(native).__name__


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        normalized = str(value).strip()
    except Exception:
        return None
    return normalized or None


def _normalize_role(value: Any) -> str:
    if not isinstance(value, str):
        raise GeoreferencingValidationError(
            "role muss eine Zeichenfolge sein.",
            details={"actualType": type(value).__name__},
        )

    normalized = value.strip().lower()
    allowed = {
        "source",
        "target",
        "reference",
        "horizontal",
        "vertical",
        "geographic",
        "geocentric",
    }
    if normalized not in allowed:
        raise GeoreferencingValidationError(
            "role wird nicht unterstützt.",
            details={
                "role": normalized,
                "allowedRoles": sorted(allowed),
            },
        )
    return normalized


def _normalize_dimensions(
    values: Sequence[CoordinateDimension | int],
) -> tuple[CoordinateDimension, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Sequence,
    ):
        raise GeoreferencingValidationError(
            "allowed_dimensions muss eine Sequenz sein.",
            details={"actualType": type(values).__name__},
        )

    normalized: list[CoordinateDimension] = []
    seen: set[CoordinateDimension] = set()
    for value in values:
        try:
            dimension = (
                value
                if isinstance(value, CoordinateDimension)
                else CoordinateDimension(int(value))
            )
        except (TypeError, ValueError) as error:
            raise GeoreferencingValidationError(
                "Nicht unterstützte Koordinatendimension.",
                details={
                    "value": value
                    if isinstance(value, (str, int, float, bool))
                    else None,
                    "allowedDimensions": [2, 3],
                },
                cause=error,
            ) from error

        if dimension in seen:
            continue
        seen.add(dimension)
        normalized.append(dimension)

    if not normalized:
        raise GeoreferencingValidationError(
            "Mindestens eine Koordinatendimension muss erlaubt sein."
        )

    return tuple(normalized)


def _normalize_identifier_tuple(
    values: Sequence[str],
    *,
    field_name: str,
    uppercase: bool,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Sequence,
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Sequenz sein.",
            details={"actualType": type(values).__name__},
        )

    if len(values) > _MAX_ALLOWLIST_ITEMS:
        raise GeoreferencingValidationError(
            f"'{field_name}' enthält zu viele Einträge.",
            details={
                "count": len(values),
                "maximumItems": _MAX_ALLOWLIST_ITEMS,
            },
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise GeoreferencingValidationError(
                f"'{field_name}[{index}]' muss eine nicht-leere "
                "Zeichenfolge sein.",
                details={"actualType": type(value).__name__},
            )
        item = value.strip()
        if uppercase:
            item = item.upper()
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    return tuple(normalized)


def _normalize_confidence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeoreferencingValidationError(
            "minimumAuthorityConfidence muss eine ganze Zahl sein.",
            details={"actualType": type(value).__name__},
        )
    if value < 0 or value > 100:
        raise GeoreferencingValidationError(
            "minimumAuthorityConfidence muss zwischen 0 und 100 liegen.",
            details={"value": value},
        )
    return value


def _version_at_least(
    installed: str,
    required: str,
) -> bool:
    installed_tuple = _version_tuple(installed)
    required_tuple = _version_tuple(required)
    if not installed_tuple:
        return False

    length = max(len(installed_tuple), len(required_tuple))
    installed_padded = installed_tuple + (0,) * (
        length - len(installed_tuple)
    )
    required_padded = required_tuple + (0,) * (
        length - len(required_tuple)
    )
    return installed_padded >= required_padded


def _version_tuple(value: str) -> tuple[int, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(
        int(component)
        for component in _VERSION_COMPONENT_PATTERN.findall(value)
    )


def _cache_info_to_dict(cache_info: Any) -> dict[str, JsonValue]:
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


def _safe_error(error: BaseException) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "type": type(error).__name__,
        "message": str(error).strip() or "CRS-Diagnose fehlgeschlagen.",
    }
    code = getattr(error, "code", None)
    if code is not None:
        payload["code"] = str(code)
    return payload


__all__ = [
    "CANONICAL_GEOCENTRIC_CRS_ID",
    "CANONICAL_GEOGRAPHIC_CRS_ID",
    "CANONICAL_WKT_VERSION",
    "CrsInspection",
    "CrsResolutionPolicy",
    "MINIMUM_PYPROJ_VERSION",
    "canonical_geocentric_crs",
    "canonical_geographic_crs",
    "clear_crs_caches",
    "configure_proj_network",
    "crs_cache_info",
    "crs_equivalent",
    "crs_runtime_status",
    "ensure_crs_runtime_ready",
    "inspect_crs",
    "resolve_crs",
    "resolve_native_crs",
]
