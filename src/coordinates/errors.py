# services/vectoplan-chunk/src/coordinates/errors.py
"""Frameworkunabhängige Domänenfehler für Koordinaten und Welt-Topologien.

Dieses Modul bildet die stabile Fehlerbasis des gemeinsamen Koordinatenkerns.
Es kennt weder Flask noch SQLAlchemy und darf deshalb von Domain-, Application-,
Bootstrap- und HTTP-Schichten verwendet werden.

Architekturregeln
-----------------
* Fehlercodes sind stabil und maschinenlesbar.
* Domänenfehler enthalten nur serialisierbare, nicht-sensitive Details.
* Bekannte Domänenfehler werden niemals in generische Fehler umgewandelt.
* Unerwartete technische Fehler dürfen nur an einer bewusst gesetzten
  Schichtgrenze in ``CoordinateComputationError`` übersetzt werden.
* Dieses Modul führt kein Logging, keinen Rollback und keine HTTP-Antwort aus.
  Diese Verantwortlichkeiten bleiben bei den äußeren Schichten.
"""

from collections.abc import Mapping, Sequence
from enum import Enum, StrEnum
from typing import Any, Final, Self


JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


class CoordinateErrorCode(StrEnum):
    """Stabile Fehlercodes des Koordinatenkerns.

    Vorhandene Werte dürfen nach Veröffentlichung nicht umbenannt oder für eine
    andere Bedeutung wiederverwendet werden. Neue Werte werden ausschließlich
    ergänzt.
    """

    COORDINATE_ERROR = "coordinate_error"
    COORDINATE_VALIDATION_FAILED = "coordinate_validation_failed"
    COORDINATE_COMPUTATION_FAILED = "coordinate_computation_failed"
    COORDINATE_CONFIGURATION_INVALID = "coordinate_configuration_invalid"
    COORDINATE_CONFLICT = "coordinate_conflict"

    INVALID_CHUNK_SIZE = "invalid_chunk_size"
    WORLD_WIDTH_INVALID = "world_width_invalid"
    WORLD_WIDTH_NOT_CHUNK_ALIGNED = "world_width_not_chunk_aligned"
    HALF_WORLD_NOT_CHUNK_ALIGNED = "half_world_not_chunk_aligned"
    WORLD_HEIGHT_INVALID = "world_height_invalid"
    INVALID_TOPOLOGY_CONFIGURATION = "invalid_topology_configuration"
    UNSUPPORTED_WRAP_AXIS = "unsupported_wrap_axis"
    TOPOLOGY_NOT_RESOLVED = "topology_not_resolved"

    COORDINATE_OUT_OF_BOUNDS = "coordinate_out_of_bounds"
    NORTH_SOUTH_BOUNDARY_EXCEEDED = "north_south_boundary_exceeded"
    COORDINATE_OVERFLOW = "coordinate_overflow"
    COORDINATE_SPACE_MISMATCH = "coordinate_space_mismatch"
    COORDINATE_DIMENSION_MISMATCH = "coordinate_dimension_mismatch"
    COORDINATE_PRECISION_LOSS = "coordinate_precision_loss"

    CHUNK_ADDRESS_INVALID = "chunk_address_invalid"
    CHUNK_ADDRESS_NONCANONICAL = "chunk_address_noncanonical"
    CELL_ADDRESS_INVALID = "cell_address_invalid"
    AMBIGUOUS_ANTIPODAL_COORDINATE = "ambiguous_antipodal_coordinate"


class CoordinateError(Exception):
    """Basisklasse aller erwartbaren Koordinaten-Domänenfehler.

    Parameters
    ----------
    message:
        Menschenlesbare, nicht-sensitive Fehlerbeschreibung.
    code:
        Stabiler maschinenlesbarer Fehlercode. Normalerweise wird der
        Klassenstandard verwendet.
    details:
        Strukturierte Zusatzinformationen. Werte werden defensiv in eine
        JSON-kompatible Form überführt.
    http_status:
        Empfohlener HTTP-Status für Adapter. Der Domain-Layer selbst erzeugt
        keine HTTP-Antwort.
    retryable:
        Gibt an, ob ein unveränderter erneuter Versuch sinnvoll sein kann.
    cause:
        Optionaler technischer Ursprung. Er wird intern erhalten, aber nicht
        automatisch nach außen serialisiert.
    """

    default_code: CoordinateErrorCode = CoordinateErrorCode.COORDINATE_ERROR
    default_http_status: int = 422
    default_retryable: bool = False
    default_message: str = "Die Koordinatenoperation konnte nicht ausgeführt werden."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: CoordinateErrorCode | str | None = None,
        details: Mapping[str, Any] | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
        cause: BaseException | None = None,
    ) -> None:
        resolved_message = _normalize_message(message or self.default_message)
        resolved_code = _normalize_code(code or self.default_code)
        resolved_status = _normalize_http_status(
            self.default_http_status if http_status is None else http_status
        )

        super().__init__(resolved_message)

        self.message: Final[str] = resolved_message
        self.code: Final[CoordinateErrorCode | str] = resolved_code
        self.details: Final[dict[str, JsonValue]] = _normalize_details(details)
        self.http_status: Final[int] = resolved_status
        self.retryable: Final[bool] = (
            self.default_retryable if retryable is None else bool(retryable)
        )
        self.cause: Final[BaseException | None] = cause

    def to_dict(self, *, include_http_status: bool = False) -> dict[str, JsonValue]:
        """Erzeugt eine sichere, JSON-kompatible Fehlerdarstellung."""

        payload: dict[str, JsonValue] = {
            "ok": False,
            "error": {
                "code": str(self.code),
                "message": self.message,
                "details": dict(self.details),
                "retryable": self.retryable,
            },
        }

        if include_http_status:
            error_payload = payload["error"]
            if isinstance(error_payload, dict):
                error_payload["httpStatus"] = self.http_status

        return payload

    def to_problem_details(
        self,
        *,
        instance: str | None = None,
        title: str | None = None,
    ) -> dict[str, JsonValue]:
        """Erzeugt eine RFC-7807-nahe, adapterfreundliche Darstellung.

        Es wird bewusst keine feste ``type``-URL erzeugt. Der HTTP-Adapter kann
        diese anhand seiner öffentlichen API-Dokumentation ergänzen.
        """

        payload: dict[str, JsonValue] = {
            "title": title or _title_from_code(self.code),
            "status": self.http_status,
            "detail": self.message,
            "code": str(self.code),
            "retryable": self.retryable,
            "details": dict(self.details),
        }

        if instance:
            payload["instance"] = instance

        return payload

    def with_context(self, **details: Any) -> Self:
        """Erzeugt denselben Fehlertyp mit zusätzlichen sicheren Details.

        Das ursprüngliche Fehlerobjekt bleibt unverändert. Dies ist für
        Schichtgrenzen gedacht, die beispielsweise ``projectId`` oder
        ``worldId`` ergänzen, ohne den Fehlercode zu verändern.
        """

        merged = dict(self.details)
        merged.update(details)

        return self.__class__(
            self.message,
            code=self.code,
            details=merged,
            http_status=self.http_status,
            retryable=self.retryable,
            cause=self.cause,
        )


class CoordinateValidationError(CoordinateError):
    """Basisklasse für ungültige externe oder fachliche Eingaben."""

    default_code = CoordinateErrorCode.COORDINATE_VALIDATION_FAILED
    default_http_status = 422
    default_message = "Die Koordinateneingabe ist ungültig."


class CoordinateConfigurationError(CoordinateError):
    """Basisklasse für ungültige statische Grid- oder Topologiekonfiguration."""

    default_code = CoordinateErrorCode.COORDINATE_CONFIGURATION_INVALID
    default_http_status = 500
    default_message = "Die Koordinatenkonfiguration ist ungültig."


class CoordinateComputationError(CoordinateError):
    """Sicherer Wrapper für unerwartete technische Berechnungsfehler."""

    default_code = CoordinateErrorCode.COORDINATE_COMPUTATION_FAILED
    default_http_status = 500
    default_retryable = False
    default_message = "Die Koordinatenberechnung ist unerwartet fehlgeschlagen."

    @classmethod
    def from_unexpected(
        cls,
        error: BaseException,
        *,
        operation: str,
        details: Mapping[str, Any] | None = None,
    ) -> "CoordinateComputationError":
        """Übersetzt einen unerwarteten Fehler an einer bewussten Schichtgrenze.

        Der konkrete Fehlertext wird nicht in öffentliche Details übernommen.
        Der Stacktrace bleibt durch ``cause`` für internes Logging verfügbar.
        """

        safe_details: dict[str, Any] = {
            "operation": operation,
            "causeType": type(error).__name__,
        }
        if details:
            safe_details.update(details)

        return cls(details=safe_details, cause=error)


class CoordinateConflictError(CoordinateError):
    """Basisklasse für Konflikte mit einem bestehenden kanonischen Zustand."""

    default_code = CoordinateErrorCode.COORDINATE_CONFLICT
    default_http_status = 409
    default_message = "Die Koordinatenoperation steht im Konflikt mit dem aktuellen Zustand."


class InvalidChunkSizeError(CoordinateConfigurationError):
    """Die konfigurierte Chunkgröße ist nicht positiv oder nicht ganzzahlig."""

    default_code = CoordinateErrorCode.INVALID_CHUNK_SIZE
    default_message = "Die Chunkgröße muss eine positive ganze Zahl sein."

    def __init__(self, chunk_size: Any) -> None:
        super().__init__(details={"chunkSize": chunk_size})


class WorldWidthInvalidError(CoordinateConfigurationError):
    """Die periodische Weltbreite verletzt grundlegende Invarianten."""

    default_code = CoordinateErrorCode.WORLD_WIDTH_INVALID
    default_message = (
        "Die Weltbreite muss eine positive, gerade ganze Zahl sein."
    )

    def __init__(self, world_width: Any) -> None:
        super().__init__(details={"worldWidthBlocks": world_width})


class WorldWidthNotChunkAlignedError(CoordinateConfigurationError):
    """Die Weltnaht würde mitten durch einen Chunk verlaufen."""

    default_code = CoordinateErrorCode.WORLD_WIDTH_NOT_CHUNK_ALIGNED
    default_message = "Die Weltbreite muss ohne Rest durch die Chunkgröße teilbar sein."

    def __init__(self, *, world_width: int, chunk_size: int) -> None:
        super().__init__(
            details={
                "worldWidthBlocks": world_width,
                "chunkSize": chunk_size,
                "remainder": world_width % chunk_size if chunk_size else None,
            }
        )


class HalfWorldNotChunkAlignedError(CoordinateConfigurationError):
    """Die symmetrische halbe Weltbreite ist nicht chunkkompatibel."""

    default_code = CoordinateErrorCode.HALF_WORLD_NOT_CHUNK_ALIGNED
    default_message = (
        "Die halbe Weltbreite muss ohne Rest durch die Chunkgröße teilbar sein."
    )

    def __init__(self, *, world_width: int, chunk_size: int) -> None:
        half_world = world_width // 2
        super().__init__(
            details={
                "worldWidthBlocks": world_width,
                "halfWorldBlocks": half_world,
                "chunkSize": chunk_size,
                "remainder": half_world % chunk_size if chunk_size else None,
            }
        )


class WorldHeightInvalidError(CoordinateConfigurationError):
    """Die konfigurierte Nord-/Süd-Ausdehnung ist ungültig."""

    default_code = CoordinateErrorCode.WORLD_HEIGHT_INVALID
    default_message = "Die Nord-/Süd-Ausdehnung der Welt ist ungültig."

    def __init__(
        self,
        *,
        minimum_z: Any,
        maximum_z: Any,
    ) -> None:
        super().__init__(
            details={
                "minimumZ": minimum_z,
                "maximumZ": maximum_z,
            }
        )


class InvalidTopologyConfigurationError(CoordinateConfigurationError):
    """Die Kombination aus Grenzen, Wrap-Achsen oder Policies ist ungültig."""

    default_code = CoordinateErrorCode.INVALID_TOPOLOGY_CONFIGURATION
    default_message = "Die Topologiekonfiguration ist ungültig."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)


class UnsupportedWrapAxisError(CoordinateConfigurationError):
    """Eine angeforderte Wrap-Achse wird von der Topologie nicht unterstützt."""

    default_code = CoordinateErrorCode.UNSUPPORTED_WRAP_AXIS
    default_message = "Die angeforderte periodische Achse wird nicht unterstützt."

    def __init__(
        self,
        axis: str,
        *,
        supported_axes: Sequence[str] = ("x",),
    ) -> None:
        super().__init__(
            details={
                "axis": axis,
                "supportedAxes": list(supported_axes),
            }
        )


class TopologyNotResolvedError(CoordinateConfigurationError):
    """Für eine WorldInstance konnte keine Topologiestrategie bestimmt werden."""

    default_code = CoordinateErrorCode.TOPOLOGY_NOT_RESOLVED
    default_message = "Für die Welt konnte keine Koordinatentopologie aufgelöst werden."

    def __init__(
        self,
        *,
        world_id: str | None = None,
        provider_id: str | None = None,
    ) -> None:
        super().__init__(
            details={
                "worldId": world_id,
                "providerId": provider_id,
            }
        )


class CoordinateOutOfBoundsError(CoordinateValidationError):
    """Eine Position liegt außerhalb eines nicht periodischen Bereichs."""

    default_code = CoordinateErrorCode.COORDINATE_OUT_OF_BOUNDS
    default_message = "Die Koordinate liegt außerhalb des zulässigen Weltbereichs."

    def __init__(
        self,
        *,
        axis: str,
        value: int | float,
        minimum: int | float | None = None,
        maximum: int | float | None = None,
    ) -> None:
        super().__init__(
            details={
                "axis": axis,
                "value": value,
                "minimum": minimum,
                "maximum": maximum,
            }
        )


class NorthSouthBoundaryExceededError(CoordinateOutOfBoundsError):
    """Die nicht periodische Z-Achse überschreitet ihre Earth-v1-Grenze."""

    default_code = CoordinateErrorCode.NORTH_SOUTH_BOUNDARY_EXCEEDED
    default_message = "Die Nord-/Süd-Grenze der Earth-Welt wurde überschritten."

    def __init__(
        self,
        *,
        z: int | float,
        minimum_z: int | float,
        maximum_z: int | float,
    ) -> None:
        CoordinateValidationError.__init__(
            self,
            details={
                "axis": "z",
                "value": z,
                "minimumZ": minimum_z,
                "maximumZ": maximum_z,
                "wrapApplied": False,
            },
        )


class CoordinateOverflowError(CoordinateValidationError):
    """Eine Eingabe überschreitet den unterstützten ganzzahligen Wertebereich."""

    default_code = CoordinateErrorCode.COORDINATE_OVERFLOW
    default_message = "Die Koordinate überschreitet den unterstützten Wertebereich."

    def __init__(
        self,
        *,
        axis: str,
        value: Any,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> None:
        super().__init__(
            details={
                "axis": axis,
                "value": value,
                "minimum": minimum,
                "maximum": maximum,
            }
        )


class CoordinateSpaceMismatchError(CoordinateValidationError):
    """Koordinaten aus unterschiedlichen Räumen wurden unzulässig kombiniert."""

    default_code = CoordinateErrorCode.COORDINATE_SPACE_MISMATCH
    default_message = "Die Koordinaten gehören zu unterschiedlichen Koordinatenräumen."

    def __init__(
        self,
        *,
        expected_space: str,
        actual_space: str,
    ) -> None:
        super().__init__(
            details={
                "expectedSpace": expected_space,
                "actualSpace": actual_space,
            }
        )


class CoordinateDimensionMismatchError(CoordinateValidationError):
    """Die Anzahl der Koordinatenachsen passt nicht zum erwarteten Vertrag."""

    default_code = CoordinateErrorCode.COORDINATE_DIMENSION_MISMATCH
    default_message = "Die Dimension der Koordinate entspricht nicht dem erwarteten Vertrag."

    def __init__(
        self,
        *,
        expected_dimensions: int,
        actual_dimensions: int,
    ) -> None:
        super().__init__(
            details={
                "expectedDimensions": expected_dimensions,
                "actualDimensions": actual_dimensions,
            }
        )


class CoordinatePrecisionLossError(CoordinateValidationError):
    """Eine Umrechnung würde die konfigurierte Präzision unzulässig verletzen."""

    default_code = CoordinateErrorCode.COORDINATE_PRECISION_LOSS
    default_message = "Die Koordinatenumrechnung überschreitet die zulässige Präzisionstoleranz."

    def __init__(
        self,
        *,
        measured_error: float,
        allowed_error: float,
        unit: str,
    ) -> None:
        super().__init__(
            details={
                "measuredError": measured_error,
                "allowedError": allowed_error,
                "unit": unit,
            }
        )


class ChunkAddressInvalidError(CoordinateValidationError):
    """Eine Chunkadresse besitzt ungültige oder unvollständige Komponenten."""

    default_code = CoordinateErrorCode.CHUNK_ADDRESS_INVALID
    default_message = "Die Chunkadresse ist ungültig."

    def __init__(
        self,
        *,
        chunk_x: Any = None,
        chunk_y: Any = None,
        chunk_z: Any = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(
            details={
                "chunkX": chunk_x,
                "chunkY": chunk_y,
                "chunkZ": chunk_z,
                "reason": reason,
            }
        )


class ChunkAddressNonCanonicalError(CoordinateConflictError):
    """Eine persistierte oder verlangte Earth-Adresse ist nicht kanonisch."""

    default_code = CoordinateErrorCode.CHUNK_ADDRESS_NONCANONICAL
    default_message = "Die Chunkadresse ist für die aktuelle Topologie nicht kanonisch."

    def __init__(
        self,
        *,
        requested_chunk_key: str,
        canonical_chunk_key: str,
    ) -> None:
        super().__init__(
            details={
                "requestedChunkKey": requested_chunk_key,
                "canonicalChunkKey": canonical_chunk_key,
            }
        )


class CellAddressInvalidError(CoordinateValidationError):
    """Eine lokale Zelladresse liegt außerhalb eines Chunks."""

    default_code = CoordinateErrorCode.CELL_ADDRESS_INVALID
    default_message = "Die lokale Zelladresse ist ungültig."

    def __init__(
        self,
        *,
        local_x: Any,
        local_y: Any,
        local_z: Any,
        chunk_size: int,
    ) -> None:
        super().__init__(
            details={
                "localX": local_x,
                "localY": local_y,
                "localZ": local_z,
                "chunkSize": chunk_size,
            }
        )


class AmbiguousAntipodalCoordinateError(CoordinateConfigurationError):
    """Die Topologie besitzt keine Regel für den exakt gegenüberliegenden Punkt."""

    default_code = CoordinateErrorCode.AMBIGUOUS_ANTIPODAL_COORDINATE
    default_message = (
        "Für die antipodale Koordinate ist keine kanonische Darstellung definiert."
    )

    def __init__(
        self,
        *,
        value: int,
        half_world: int,
    ) -> None:
        super().__init__(
            details={
                "value": value,
                "halfWorldBlocks": half_world,
                "requiredCanonicalValue": -half_world,
            }
        )


def ensure_coordinate_error(
    error: BaseException,
    *,
    operation: str,
    details: Mapping[str, Any] | None = None,
) -> CoordinateError:
    """Erhält bekannte Domänenfehler und übersetzt nur unerwartete Fehler.

    Diese Funktion ist für klar definierte äußere Schichtgrenzen gedacht,
    beispielsweise Application-Service, CLI oder HTTP-Adapter.

    Sie führt kein Logging und keinen Rollback aus.
    """

    if isinstance(error, CoordinateError):
        return error

    return CoordinateComputationError.from_unexpected(
        error,
        operation=operation,
        details=details,
    )


def _normalize_message(message: str) -> str:
    value = str(message).strip()
    return value or CoordinateError.default_message


def _normalize_code(
    code: CoordinateErrorCode | str,
) -> CoordinateErrorCode | str:
    if isinstance(code, CoordinateErrorCode):
        return code

    value = str(code).strip()
    if not value:
        return CoordinateErrorCode.COORDINATE_ERROR

    try:
        return CoordinateErrorCode(value)
    except ValueError:
        # Erweiterungscodes anderer Subdomänen bleiben erlaubt, ohne die
        # zentrale Enum bei jeder Integration sofort ändern zu müssen.
        return value


def _normalize_http_status(status: int) -> int:
    try:
        value = int(status)
    except (TypeError, ValueError) as error:
        raise ValueError("http_status muss eine ganze Zahl sein.") from error

    if value < 400 or value > 599:
        raise ValueError("http_status muss zwischen 400 und 599 liegen.")

    return value


def _normalize_details(
    details: Mapping[str, Any] | None,
) -> dict[str, JsonValue]:
    if not details:
        return {}

    normalized: dict[str, JsonValue] = {}
    for raw_key, raw_value in details.items():
        key = str(raw_key).strip()
        if not key:
            continue
        normalized[key] = _to_json_value(raw_value)

    return normalized


def _to_json_value(value: Any, *, _depth: int = 0) -> JsonValue:
    """Konvertiert Fehlerdetails defensiv in JSON-kompatible Werte."""

    if _depth >= 8:
        return {"type": type(value).__name__, "truncated": True}

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Enum):
        return _to_json_value(value.value, _depth=_depth + 1)

    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key).strip()
            if key:
                result[key] = _to_json_value(raw_value, _depth=_depth + 1)
        return result

    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [
            _to_json_value(item, _depth=_depth + 1)
            for item in value
        ]

    # Keine beliebigen repr()-Werte serialisieren: Diese können Secrets,
    # Speicheradressen oder interne Implementierungsdetails enthalten.
    return {"type": type(value).__name__}


def _title_from_code(code: CoordinateErrorCode | str) -> str:
    value = str(code).replace("_", " ").strip()
    return value.capitalize() if value else "Coordinate error"


__all__ = [
    "AmbiguousAntipodalCoordinateError",
    "CellAddressInvalidError",
    "ChunkAddressInvalidError",
    "ChunkAddressNonCanonicalError",
    "CoordinateComputationError",
    "CoordinateConfigurationError",
    "CoordinateConflictError",
    "CoordinateDimensionMismatchError",
    "CoordinateError",
    "CoordinateErrorCode",
    "CoordinateOutOfBoundsError",
    "CoordinateOverflowError",
    "CoordinatePrecisionLossError",
    "CoordinateSpaceMismatchError",
    "CoordinateValidationError",
    "HalfWorldNotChunkAlignedError",
    "InvalidChunkSizeError",
    "InvalidTopologyConfigurationError",
    "NorthSouthBoundaryExceededError",
    "TopologyNotResolvedError",
    "UnsupportedWrapAxisError",
    "WorldHeightInvalidError",
    "WorldWidthInvalidError",
    "WorldWidthNotChunkAlignedError",
    "ensure_coordinate_error",
]
