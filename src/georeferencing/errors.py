# services/vectoplan-chunk/src/georeferencing/errors.py
"""Frameworkunabhängige Domänenfehler für Georeferenzierung und CRS.

Dieses Modul erweitert die gemeinsame Koordinatenfehlerbasis um stabile
Fehlercodes für:

* globale Earth-Referenzpunkte;
* CRS-Eingaben und CRS-Verträge;
* Achsen-, Dimensions- und Einheitendefinitionen;
* Transformationsauswahl und Transformationsgenauigkeit;
* fehlende PROJ-Ressourcen und Transformationsgitter;
* Referenzkonflikte, Sperren und Revisionskonflikte.

Architekturregeln
-----------------
* CRS-Werte werden in Fehlerdetails nur sicher zusammengefasst.
* Lange WKT-/PROJ-Definitionen werden nicht vollständig ausgegeben.
* Technische Ursachen bleiben über ``cause`` intern erhalten.
* Keine Exception wird geloggt, gerollbackt oder als HTTP-Antwort ausgegeben.
  Diese Verantwortlichkeiten liegen an den äußeren Schichtgrenzen.
* Bekannte Koordinaten- und Georeferenzierungsfehler werden nicht in
  generische Fehler umgewandelt.
* Fehlende oder ungültige CRS-Angaben führen niemals zu stillem Raten.
* Ballpark-Transformationen werden als eigener fachlicher Fehler behandelt.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from hashlib import sha256
from typing import Any, Final

from ..coordinates.errors import CoordinateError
from ..coordinates.models import JsonValue


_MAX_INLINE_CRS_LENGTH: Final[int] = 128
_MAX_REASON_LENGTH: Final[int] = 512
_HASH_PREFIX_LENGTH: Final[int] = 16


class GeoreferencingErrorCode(StrEnum):
    """Stabile maschinenlesbare Fehlercodes der Georeferenzierung.

    Bestehende Werte dürfen nach Veröffentlichung weder umbenannt noch für
    eine andere Bedeutung wiederverwendet werden.
    """

    GEOREFERENCING_ERROR = "georeferencing_error"
    GEOREFERENCING_VALIDATION_FAILED = "georeferencing_validation_failed"
    GEOREFERENCING_CONFIGURATION_INVALID = (
        "georeferencing_configuration_invalid"
    )
    GEOREFERENCING_COMPUTATION_FAILED = (
        "georeferencing_computation_failed"
    )
    GEOREFERENCING_CONFLICT = "georeferencing_conflict"
    GEOREFERENCING_DEPENDENCY_UNAVAILABLE = (
        "georeferencing_dependency_unavailable"
    )

    EARTH_WORLD_REFERENCE_REQUIRED = "earth_world_reference_required"
    EARTH_REFERENCE_INVALID = "earth_reference_invalid"
    EARTH_REFERENCE_CONFLICT = "earth_reference_conflict"
    WORLD_REFERENCE_LOCKED = "world_reference_locked"
    COORDINATE_FRAME_REVISION_CONFLICT = (
        "coordinate_frame_revision_conflict"
    )

    COORDINATE_CRS_REQUIRED = "coordinate_crs_required"
    COORDINATE_CRS_INVALID = "coordinate_crs_invalid"
    COORDINATE_CRS_UNSUPPORTED = "coordinate_crs_unsupported"
    COORDINATE_CRS_DIMENSION_MISMATCH = (
        "coordinate_crs_dimension_mismatch"
    )
    COORDINATE_CRS_AXIS_ORDER_INVALID = (
        "coordinate_crs_axis_order_invalid"
    )
    COORDINATE_CRS_UNIT_INVALID = "coordinate_crs_unit_invalid"
    COORDINATE_CRS_NOT_TRANSFORMABLE = (
        "coordinate_crs_not_transformable"
    )

    COORDINATE_TRANSFORM_UNAVAILABLE = (
        "coordinate_transform_unavailable"
    )
    COORDINATE_TRANSFORM_FAILED = "coordinate_transform_failed"
    COORDINATE_TRANSFORM_NOT_EXACT = "coordinate_transform_not_exact"
    COORDINATE_TRANSFORM_BALLPARK_FORBIDDEN = (
        "coordinate_transform_ballpark_forbidden"
    )
    COORDINATE_TRANSFORM_GRID_MISSING = (
        "coordinate_transform_grid_missing"
    )
    COORDINATE_TRANSFORM_ACCURACY_UNKNOWN = (
        "coordinate_transform_accuracy_unknown"
    )
    COORDINATE_TRANSFORM_PRECISION_EXCEEDED = (
        "coordinate_transform_precision_exceeded"
    )
    COORDINATE_TRANSFORM_ROUNDTRIP_FAILED = (
        "coordinate_transform_roundtrip_failed"
    )

    PYPROJ_UNAVAILABLE = "pyproj_unavailable"
    PROJ_DATABASE_UNAVAILABLE = "proj_database_unavailable"


class GeoreferencingError(CoordinateError):
    """Basisklasse aller erwartbaren Georeferenzierungsfehler."""

    default_code = GeoreferencingErrorCode.GEOREFERENCING_ERROR
    default_http_status = 422
    default_retryable = False
    default_message = (
        "Die Georeferenzierungsoperation konnte nicht ausgeführt werden."
    )


class GeoreferencingValidationError(GeoreferencingError):
    """Ungültige fachliche Eingabe oder unvollständiger CRS-Vertrag."""

    default_code = GeoreferencingErrorCode.GEOREFERENCING_VALIDATION_FAILED
    default_http_status = 422
    default_message = "Die Georeferenzierungseingabe ist ungültig."


class GeoreferencingConfigurationError(GeoreferencingError):
    """Ungültige statische Laufzeit-, Grid- oder CRS-Konfiguration."""

    default_code = (
        GeoreferencingErrorCode.GEOREFERENCING_CONFIGURATION_INVALID
    )
    default_http_status = 500
    default_message = "Die Georeferenzierungskonfiguration ist ungültig."


class GeoreferencingComputationError(GeoreferencingError):
    """Sicherer Wrapper für unerwartete technische Berechnungsfehler."""

    default_code = GeoreferencingErrorCode.GEOREFERENCING_COMPUTATION_FAILED
    default_http_status = 500
    default_retryable = False
    default_message = (
        "Die Georeferenzierungsberechnung ist unerwartet fehlgeschlagen."
    )

    @classmethod
    def from_unexpected(
        cls,
        error: BaseException,
        *,
        operation: str,
        details: Mapping[str, Any] | None = None,
    ) -> "GeoreferencingComputationError":
        """Übersetzt einen unerwarteten Fehler an einer bewussten Grenze.

        Der konkrete technische Fehlertext wird nicht öffentlich serialisiert.
        Der Originalfehler bleibt ausschließlich über ``cause`` erhalten.
        """

        safe_details: dict[str, Any] = {
            "operation": _normalize_operation(operation),
            "causeType": type(error).__name__,
        }
        if details:
            safe_details.update(details)

        return cls(
            details=safe_details,
            cause=error,
        )


class GeoreferencingConflictError(GeoreferencingError):
    """Konflikt mit einem bereits persistierten Referenzzustand."""

    default_code = GeoreferencingErrorCode.GEOREFERENCING_CONFLICT
    default_http_status = 409
    default_message = (
        "Die Georeferenzierungsoperation steht im Konflikt "
        "mit dem aktuellen Zustand."
    )


class GeoreferencingDependencyUnavailableError(
    GeoreferencingConfigurationError
):
    """Eine benötigte lokale Georeferenzierungsabhängigkeit fehlt."""

    default_code = (
        GeoreferencingErrorCode.GEOREFERENCING_DEPENDENCY_UNAVAILABLE
    )
    default_http_status = 503
    default_retryable = False
    default_message = (
        "Eine benötigte Georeferenzierungsabhängigkeit ist nicht verfügbar."
    )

    @classmethod
    def for_dependency(
        cls,
        dependency: str,
        *,
        required_version: str | None = None,
    ) -> "GeoreferencingDependencyUnavailableError":
        return cls(
            details={
                "dependency": _normalize_identifier(
                    dependency,
                    field_name="dependency",
                ),
                "requiredVersion": _normalize_optional_identifier(
                    required_version
                ),
            }
        )


class EarthWorldReferenceRequiredError(GeoreferencingValidationError):
    """Eine Earth-World wurde ohne globalen Referenzpunkt angefordert."""

    default_code = GeoreferencingErrorCode.EARTH_WORLD_REFERENCE_REQUIRED
    default_message = (
        "Eine Earth-World benötigt genau einen globalen Referenzpunkt."
    )

    @classmethod
    def for_world(
        cls,
        *,
        world_id: str | None = None,
        provider_id: str | None = "earth",
    ) -> "EarthWorldReferenceRequiredError":
        return cls(
            details={
                "worldId": _normalize_optional_identifier(world_id),
                "providerId": _normalize_optional_identifier(provider_id),
            }
        )


class EarthReferenceInvalidError(GeoreferencingValidationError):
    """Der globale Referenzpunkt verletzt den Earth-v1-Vertrag."""

    default_code = GeoreferencingErrorCode.EARTH_REFERENCE_INVALID
    default_message = "Der globale Earth-Referenzpunkt ist ungültig."

    @classmethod
    def for_reason(
        cls,
        reason: str,
        *,
        coordinate_dimensions: int | None = None,
        crs: Any = None,
    ) -> "EarthReferenceInvalidError":
        return cls(
            details={
                "reason": _normalize_reason(reason),
                "coordinateDimensions": coordinate_dimensions,
                "crs": summarize_crs_input(crs) if crs is not None else None,
            }
        )


class EarthReferenceConflictError(GeoreferencingConflictError):
    """Idempotentes Provisioning trifft auf eine abweichende Referenz."""

    default_code = GeoreferencingErrorCode.EARTH_REFERENCE_CONFLICT
    default_message = (
        "Die vorhandene Earth-Referenz stimmt nicht mit der "
        "angeforderten Referenz überein."
    )

    @classmethod
    def for_world(
        cls,
        *,
        world_id: str,
        conflicting_fields: Sequence[str],
    ) -> "EarthReferenceConflictError":
        return cls(
            details={
                "worldId": _normalize_identifier(
                    world_id,
                    field_name="worldId",
                ),
                "conflictingFields": _normalize_field_names(
                    conflicting_fields
                ),
            }
        )


class WorldReferenceLockedError(GeoreferencingConflictError):
    """Eine materialisierte WorldInstance darf nicht normal reanchored werden."""

    default_code = GeoreferencingErrorCode.WORLD_REFERENCE_LOCKED
    default_message = (
        "Die globale Weltreferenz ist nach der Materialisierung gesperrt."
    )

    @classmethod
    def for_world(
        cls,
        *,
        world_id: str,
        lock_reasons: Sequence[str],
    ) -> "WorldReferenceLockedError":
        return cls(
            details={
                "worldId": _normalize_identifier(
                    world_id,
                    field_name="worldId",
                ),
                "lockReasons": _normalize_field_names(lock_reasons),
                "reanchorRequired": True,
            }
        )


class CoordinateFrameRevisionConflictError(GeoreferencingConflictError):
    """Die erwartete Coordinate-Frame-Revision ist veraltet."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_FRAME_REVISION_CONFLICT
    )
    default_message = (
        "Die Coordinate-Frame-Revision stimmt nicht mit dem "
        "persistierten Zustand überein."
    )

    @classmethod
    def for_revisions(
        cls,
        *,
        expected_revision: int,
        actual_revision: int,
    ) -> "CoordinateFrameRevisionConflictError":
        return cls(
            details={
                "expectedRevision": _normalize_non_negative_int(
                    expected_revision,
                    field_name="expectedRevision",
                ),
                "actualRevision": _normalize_non_negative_int(
                    actual_revision,
                    field_name="actualRevision",
                ),
            }
        )


class CrsRequiredError(GeoreferencingValidationError):
    """Eine Operation wurde ohne explizite CRS-Angabe aufgerufen."""

    default_code = GeoreferencingErrorCode.COORDINATE_CRS_REQUIRED
    default_message = (
        "Für die Koordinatenoperation ist eine explizite CRS-Angabe erforderlich."
    )

    @classmethod
    def for_role(
        cls,
        role: str,
    ) -> "CrsRequiredError":
        return cls(
            details={
                "crsRole": _normalize_crs_role(role),
                "automaticGuessingAllowed": False,
            }
        )


class CrsInvalidError(GeoreferencingValidationError):
    """Eine CRS-Eingabe kann nicht als gültige Definition gelesen werden."""

    default_code = GeoreferencingErrorCode.COORDINATE_CRS_INVALID
    default_message = "Die angegebene CRS-Definition ist ungültig."

    @classmethod
    def for_value(
        cls,
        value: Any,
        *,
        role: str,
        reason: str | None = None,
    ) -> "CrsInvalidError":
        return cls(
            details={
                "crsRole": _normalize_crs_role(role),
                "crs": summarize_crs_input(value),
                "reason": (
                    _normalize_reason(reason)
                    if reason is not None
                    else None
                ),
            }
        )


class CrsUnsupportedError(GeoreferencingValidationError):
    """Ein valides CRS ist für die konkrete Operation nicht freigegeben."""

    default_code = GeoreferencingErrorCode.COORDINATE_CRS_UNSUPPORTED
    default_message = (
        "Das angegebene Koordinatenreferenzsystem wird "
        "für diese Operation nicht unterstützt."
    )

    @classmethod
    def for_crs(
        cls,
        crs: Any,
        *,
        role: str,
        operation: str,
        allowed_crs_ids: Sequence[str] | None = None,
    ) -> "CrsUnsupportedError":
        return cls(
            details={
                "crsRole": _normalize_crs_role(role),
                "operation": _normalize_operation(operation),
                "crs": summarize_crs_input(crs),
                "allowedCrsIds": (
                    _normalize_field_names(allowed_crs_ids)
                    if allowed_crs_ids is not None
                    else None
                ),
            }
        )


class CrsDimensionMismatchError(GeoreferencingValidationError):
    """CRS- und Koordinatendimension passen nicht zusammen."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_CRS_DIMENSION_MISMATCH
    )
    default_message = (
        "Die CRS-Dimension entspricht nicht der übergebenen Koordinate."
    )

    @classmethod
    def for_dimensions(
        cls,
        *,
        crs: Any,
        expected_dimensions: int,
        actual_dimensions: int,
    ) -> "CrsDimensionMismatchError":
        return cls(
            details={
                "crs": summarize_crs_input(crs),
                "expectedDimensions": _normalize_positive_int(
                    expected_dimensions,
                    field_name="expectedDimensions",
                ),
                "actualDimensions": _normalize_positive_int(
                    actual_dimensions,
                    field_name="actualDimensions",
                ),
            }
        )


class CrsAxisOrderInvalidError(GeoreferencingValidationError):
    """Die Achsenreihenfolge ist nicht eindeutig oder nicht erlaubt."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_CRS_AXIS_ORDER_INVALID
    )
    default_message = "Die CRS-Achsenreihenfolge ist ungültig."

    @classmethod
    def for_axes(
        cls,
        *,
        crs: Any,
        detected_axes: Sequence[str],
        required_convention: str,
    ) -> "CrsAxisOrderInvalidError":
        return cls(
            details={
                "crs": summarize_crs_input(crs),
                "detectedAxes": _normalize_field_names(detected_axes),
                "requiredConvention": _normalize_identifier(
                    required_convention,
                    field_name="requiredConvention",
                ),
            }
        )


class CrsUnitInvalidError(GeoreferencingValidationError):
    """Die Einheit eines CRS ist für die Operation nicht zulässig."""

    default_code = GeoreferencingErrorCode.COORDINATE_CRS_UNIT_INVALID
    default_message = "Die CRS-Einheit ist für diese Operation ungültig."

    @classmethod
    def for_unit(
        cls,
        *,
        crs: Any,
        detected_unit: str | None,
        allowed_units: Sequence[str],
    ) -> "CrsUnitInvalidError":
        return cls(
            details={
                "crs": summarize_crs_input(crs),
                "detectedUnit": _normalize_optional_identifier(
                    detected_unit
                ),
                "allowedUnits": _normalize_field_names(allowed_units),
            }
        )


class CrsNotTransformableError(GeoreferencingValidationError):
    """Zwischen Quell- und Ziel-CRS existiert kein zulässiger Transformationspfad."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_CRS_NOT_TRANSFORMABLE
    )
    default_message = (
        "Zwischen Quell- und Ziel-CRS ist keine zulässige "
        "Transformation verfügbar."
    )

    @classmethod
    def for_pair(
        cls,
        *,
        source_crs: Any,
        target_crs: Any,
        operation: str,
    ) -> "CrsNotTransformableError":
        return cls(
            details={
                "operation": _normalize_operation(operation),
                "sourceCrs": summarize_crs_input(source_crs),
                "targetCrs": summarize_crs_input(target_crs),
            }
        )


class TransformationUnavailableError(GeoreferencingConfigurationError):
    """Eine benötigte Transformationsoperation ist lokal nicht verfügbar."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_TRANSFORM_UNAVAILABLE
    )
    default_http_status = 503
    default_message = (
        "Die benötigte Koordinatentransformation ist nicht verfügbar."
    )

    @classmethod
    def for_pair(
        cls,
        *,
        source_crs: Any,
        target_crs: Any,
        operation: str,
    ) -> "TransformationUnavailableError":
        return cls(
            details={
                "operation": _normalize_operation(operation),
                "sourceCrs": summarize_crs_input(source_crs),
                "targetCrs": summarize_crs_input(target_crs),
            }
        )


class TransformationFailedError(GeoreferencingComputationError):
    """Eine ausgewählte Transformation ist während der Berechnung fehlgeschlagen."""

    default_code = GeoreferencingErrorCode.COORDINATE_TRANSFORM_FAILED
    default_http_status = 422
    default_message = "Die Koordinatentransformation ist fehlgeschlagen."

    @classmethod
    def from_cause(
        cls,
        error: BaseException,
        *,
        source_crs: Any,
        target_crs: Any,
        operation: str,
    ) -> "TransformationFailedError":
        return cls(
            details={
                "operation": _normalize_operation(operation),
                "sourceCrs": summarize_crs_input(source_crs),
                "targetCrs": summarize_crs_input(target_crs),
                "causeType": type(error).__name__,
            },
            cause=error,
        )


class TransformationNotExactError(GeoreferencingValidationError):
    """Die verfügbare Transformation erfüllt den Exactness-Vertrag nicht."""

    default_code = GeoreferencingErrorCode.COORDINATE_TRANSFORM_NOT_EXACT
    default_message = (
        "Die verfügbare Transformation erfüllt die geforderte "
        "Genauigkeitsrichtlinie nicht."
    )

    @classmethod
    def for_operation(
        cls,
        *,
        source_crs: Any,
        target_crs: Any,
        reason: str,
    ) -> "TransformationNotExactError":
        return cls(
            details={
                "sourceCrs": summarize_crs_input(source_crs),
                "targetCrs": summarize_crs_input(target_crs),
                "reason": _normalize_reason(reason),
            }
        )


class BallparkTransformationForbiddenError(TransformationNotExactError):
    """Nur eine Ballpark-Transformation wäre verfügbar, sie ist aber verboten."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_TRANSFORM_BALLPARK_FORBIDDEN
    )
    default_message = (
        "Eine ungenaue Ballpark-Transformation ist für diese "
        "Operation nicht zulässig."
    )

    @classmethod
    def for_pair(
        cls,
        *,
        source_crs: Any,
        target_crs: Any,
    ) -> "BallparkTransformationForbiddenError":
        return cls(
            details={
                "sourceCrs": summarize_crs_input(source_crs),
                "targetCrs": summarize_crs_input(target_crs),
                "allowBallpark": False,
            }
        )


class TransformationGridMissingError(GeoreferencingConfigurationError):
    """Ein erforderliches lokales PROJ-Transformationsgitter fehlt."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_TRANSFORM_GRID_MISSING
    )
    default_http_status = 503
    default_message = (
        "Ein für die genaue Koordinatentransformation benötigtes "
        "Transformationsgitter fehlt."
    )

    @classmethod
    def for_grid(
        cls,
        grid_name: str,
        *,
        source_crs: Any = None,
        target_crs: Any = None,
    ) -> "TransformationGridMissingError":
        return cls(
            details={
                "gridName": _normalize_identifier(
                    grid_name,
                    field_name="gridName",
                ),
                "sourceCrs": (
                    summarize_crs_input(source_crs)
                    if source_crs is not None
                    else None
                ),
                "targetCrs": (
                    summarize_crs_input(target_crs)
                    if target_crs is not None
                    else None
                ),
                "automaticDownloadAllowed": False,
            }
        )


class TransformationAccuracyUnknownError(GeoreferencingValidationError):
    """Die Genauigkeit einer Transformation ist unbekannt, aber erforderlich."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_TRANSFORM_ACCURACY_UNKNOWN
    )
    default_message = (
        "Die Genauigkeit der ausgewählten Transformation ist unbekannt."
    )

    @classmethod
    def for_operation(
        cls,
        *,
        source_crs: Any,
        target_crs: Any,
        required_accuracy: float | None = None,
        unit: str = "metre",
    ) -> "TransformationAccuracyUnknownError":
        return cls(
            details={
                "sourceCrs": summarize_crs_input(source_crs),
                "targetCrs": summarize_crs_input(target_crs),
                "requiredAccuracy": (
                    _normalize_non_negative_float(
                        required_accuracy,
                        field_name="requiredAccuracy",
                    )
                    if required_accuracy is not None
                    else None
                ),
                "unit": _normalize_identifier(
                    unit,
                    field_name="unit",
                ),
            }
        )


class TransformationPrecisionExceededError(GeoreferencingValidationError):
    """Gemessene Transformationsabweichung überschreitet die Toleranz."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_TRANSFORM_PRECISION_EXCEEDED
    )
    default_message = (
        "Die Transformationsabweichung überschreitet die zulässige Toleranz."
    )

    @classmethod
    def for_error(
        cls,
        *,
        measured_error: float,
        allowed_error: float,
        unit: str,
        operation: str,
    ) -> "TransformationPrecisionExceededError":
        return cls(
            details={
                "operation": _normalize_operation(operation),
                "measuredError": _normalize_non_negative_float(
                    measured_error,
                    field_name="measuredError",
                ),
                "allowedError": _normalize_non_negative_float(
                    allowed_error,
                    field_name="allowedError",
                ),
                "unit": _normalize_identifier(unit, field_name="unit"),
            }
        )


class TransformationRoundtripFailedError(GeoreferencingValidationError):
    """Vorwärts- und Rücktransformation sind nicht ausreichend stabil."""

    default_code = (
        GeoreferencingErrorCode.COORDINATE_TRANSFORM_ROUNDTRIP_FAILED
    )
    default_message = (
        "Die Koordinaten-Rücktransformation überschreitet die "
        "zulässige Roundtrip-Toleranz."
    )

    @classmethod
    def for_roundtrip(
        cls,
        *,
        measured_error: float,
        allowed_error: float,
        unit: str,
        source_crs: Any,
        target_crs: Any,
    ) -> "TransformationRoundtripFailedError":
        return cls(
            details={
                "measuredError": _normalize_non_negative_float(
                    measured_error,
                    field_name="measuredError",
                ),
                "allowedError": _normalize_non_negative_float(
                    allowed_error,
                    field_name="allowedError",
                ),
                "unit": _normalize_identifier(unit, field_name="unit"),
                "sourceCrs": summarize_crs_input(source_crs),
                "targetCrs": summarize_crs_input(target_crs),
            }
        )


class PyprojUnavailableError(GeoreferencingDependencyUnavailableError):
    """Die verpflichtende Python-Bibliothek ``pyproj`` ist nicht verfügbar."""

    default_code = GeoreferencingErrorCode.PYPROJ_UNAVAILABLE
    default_message = (
        "Die für CRS-Transformationen benötigte Bibliothek pyproj "
        "ist nicht verfügbar."
    )

    @classmethod
    def create(
        cls,
        *,
        required_version: str | None = None,
    ) -> "PyprojUnavailableError":
        return cls(
            details={
                "dependency": "pyproj",
                "requiredVersion": _normalize_optional_identifier(
                    required_version
                ),
            }
        )


class ProjDatabaseUnavailableError(
    GeoreferencingDependencyUnavailableError
):
    """Die lokale PROJ-Datenbank kann nicht geladen werden."""

    default_code = GeoreferencingErrorCode.PROJ_DATABASE_UNAVAILABLE
    default_message = (
        "Die lokale PROJ-Datenbank ist nicht verfügbar oder nicht lesbar."
    )

    @classmethod
    def create(
        cls,
        *,
        configured_data_path: str | None = None,
    ) -> "ProjDatabaseUnavailableError":
        return cls(
            details={
                "dependency": "PROJ database",
                "configuredDataPath": _safe_path_summary(
                    configured_data_path
                ),
            }
        )


def summarize_crs_input(value: Any) -> dict[str, JsonValue]:
    """Erzeugt eine sichere, reproduzierbare Beschreibung einer CRS-Eingabe.

    Lange Definitionen werden ausschließlich über Länge und SHA-256-Präfix
    beschrieben. Dadurch gelangen weder große WKT-Blöcke noch möglicherweise
    sensitive Pfadinformationen unkontrolliert in API-Antworten oder Logs.
    """

    if value is None:
        return {
            "provided": False,
            "type": "null",
        }

    if isinstance(value, bool):
        return {
            "provided": True,
            "type": "bool",
            "value": value,
        }

    if isinstance(value, int):
        return {
            "provided": True,
            "type": "integer",
            "value": value,
        }

    if isinstance(value, str):
        normalized = value.strip()
        encoded = normalized.encode("utf-8", errors="replace")
        digest = sha256(encoded).hexdigest()[:_HASH_PREFIX_LENGTH]

        summary: dict[str, JsonValue] = {
            "provided": bool(normalized),
            "type": "string",
            "length": len(normalized),
            "sha256Prefix": digest,
        }

        if (
            normalized
            and len(normalized) <= _MAX_INLINE_CRS_LENGTH
            and "\n" not in normalized
            and "\r" not in normalized
        ):
            summary["value"] = normalized

        return summary

    authority = _safe_object_attribute(value, "to_authority")
    if authority is not None:
        authority_name: str | None = None
        authority_code: str | None = None

        if (
            isinstance(authority, Sequence)
            and not isinstance(authority, (str, bytes, bytearray))
            and len(authority) == 2
        ):
            authority_name = str(authority[0])
            authority_code = str(authority[1])

        if authority_name and authority_code:
            return {
                "provided": True,
                "type": type(value).__name__,
                "authority": authority_name,
                "code": authority_code,
            }

    return {
        "provided": True,
        "type": type(value).__name__,
    }


def ensure_georeferencing_error(
    error: BaseException,
    *,
    operation: str,
    details: Mapping[str, Any] | None = None,
) -> CoordinateError:
    """Erhält bekannte Domänenfehler und übersetzt nur Unerwartetes.

    Auch bekannte ``CoordinateError``-Instanzen aus dem gemeinsamen
    Koordinatenkern bleiben unverändert erhalten.
    """

    if isinstance(error, CoordinateError):
        return error

    return GeoreferencingComputationError.from_unexpected(
        error,
        operation=operation,
        details=details,
    )


def _normalize_crs_role(role: str) -> str:
    normalized = _normalize_identifier(role, field_name="crsRole").lower()
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
            "Die CRS-Rolle wird nicht unterstützt.",
            details={
                "crsRole": normalized,
                "allowedRoles": sorted(allowed),
            },
        )

    return normalized


def _normalize_operation(operation: str) -> str:
    return _normalize_identifier(
        operation,
        field_name="operation",
        maximum_length=128,
    )


def _normalize_identifier(
    value: Any,
    *,
    field_name: str,
    maximum_length: int = 256,
) -> str:
    if not isinstance(value, str):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Zeichenfolge sein.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
            },
        )

    normalized = value.strip()
    if not normalized:
        raise GeoreferencingValidationError(
            f"'{field_name}' darf nicht leer sein.",
            details={"field": field_name},
        )

    if len(normalized) > maximum_length:
        raise GeoreferencingValidationError(
            f"'{field_name}' überschreitet die maximale Länge.",
            details={
                "field": field_name,
                "length": len(normalized),
                "maximumLength": maximum_length,
            },
        )

    return normalized


def _normalize_optional_identifier(value: Any) -> str | None:
    if value is None:
        return None

    return _normalize_identifier(
        value,
        field_name="value",
    )


def _normalize_reason(reason: str) -> str:
    normalized = _normalize_identifier(
        reason,
        field_name="reason",
        maximum_length=_MAX_REASON_LENGTH,
    )
    return normalized


def _normalize_field_names(values: Sequence[str] | None) -> list[str]:
    if values is None:
        return []

    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Sequence,
    ):
        raise GeoreferencingValidationError(
            "Die Feldliste muss eine Sequenz sein.",
            details={"actualType": type(values).__name__},
        )

    normalized: list[str] = []
    seen: set[str] = set()

    for index, value in enumerate(values):
        item = _normalize_identifier(
            value,
            field_name=f"fields[{index}]",
        )
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)

    return normalized


def _normalize_non_negative_int(
    value: Any,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine ganze Zahl sein.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
            },
        )

    if value < 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' darf nicht negativ sein.",
            details={
                "field": field_name,
                "value": value,
                "minimum": 0,
            },
        )

    return value


def _normalize_positive_int(
    value: Any,
    *,
    field_name: str,
) -> int:
    normalized = _normalize_non_negative_int(
        value,
        field_name=field_name,
    )
    if normalized == 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss größer als 0 sein.",
            details={
                "field": field_name,
                "value": normalized,
                "minimumExclusive": 0,
            },
        )

    return normalized


def _normalize_non_negative_float(
    value: Any,
    *,
    field_name: str,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Zahl sein.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
            },
        )

    normalized = float(value)
    if normalized != normalized or normalized in (float("inf"), float("-inf")):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss endlich sein.",
            details={
                "field": field_name,
                "value": str(value),
            },
        )

    if normalized < 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' darf nicht negativ sein.",
            details={
                "field": field_name,
                "value": normalized,
                "minimum": 0.0,
            },
        )

    return normalized


def _safe_object_attribute(value: Any, attribute_name: str) -> Any:
    try:
        attribute = getattr(value, attribute_name, None)
    except Exception:
        return None

    if attribute is None:
        return None

    if callable(attribute):
        try:
            return attribute()
        except Exception:
            return None

    return attribute


def _safe_path_summary(path: str | None) -> dict[str, JsonValue] | None:
    if path is None:
        return None

    if not isinstance(path, str):
        return {
            "type": type(path).__name__,
        }

    normalized = path.strip()
    encoded = normalized.encode("utf-8", errors="replace")
    return {
        "provided": bool(normalized),
        "length": len(normalized),
        "sha256Prefix": sha256(encoded).hexdigest()[:_HASH_PREFIX_LENGTH],
    }


__all__ = [
    "BallparkTransformationForbiddenError",
    "CoordinateFrameRevisionConflictError",
    "CrsAxisOrderInvalidError",
    "CrsDimensionMismatchError",
    "CrsInvalidError",
    "CrsNotTransformableError",
    "CrsRequiredError",
    "CrsUnitInvalidError",
    "CrsUnsupportedError",
    "EarthReferenceConflictError",
    "EarthReferenceInvalidError",
    "EarthWorldReferenceRequiredError",
    "GeoreferencingComputationError",
    "GeoreferencingConfigurationError",
    "GeoreferencingConflictError",
    "GeoreferencingDependencyUnavailableError",
    "GeoreferencingError",
    "GeoreferencingErrorCode",
    "GeoreferencingValidationError",
    "ProjDatabaseUnavailableError",
    "PyprojUnavailableError",
    "TransformationAccuracyUnknownError",
    "TransformationFailedError",
    "TransformationGridMissingError",
    "TransformationNotExactError",
    "TransformationPrecisionExceededError",
    "TransformationRoundtripFailedError",
    "TransformationUnavailableError",
    "WorldReferenceLockedError",
    "ensure_georeferencing_error",
    "summarize_crs_input",
]
