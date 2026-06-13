# src/world/errors.py
"""
VECTOPLAN World Errors.

Diese Datei enthält alle zentralen Fehlerklassen der neutralen World-Schicht.

Ziele:
- keine rohen ValueError-/KeyError-/ImportError-Antworten nach außen reichen
- stabile Fehlercodes für spätere API-Responses bereitstellen
- JSON-fähige Fehlerdetails liefern
- World-, Loader-, Registry-, Provider-, Config-, Validation- und Generation-
  Fehler klar voneinander trennen
- keine Flask-Abhängigkeit in der World-Schicht erzeugen

Diese Datei ist bewusst framework-neutral.

Routes können später diese Fehler abfangen und daraus JSON-Antworten bauen:

    {
      "ok": false,
      "error": {
        "code": "world_not_found",
        "message": "World 'flat' was not found.",
        "details": {
          "worldId": "flat"
        }
      }
    }
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ERROR_STATUS_CODE: Final[int] = 500
DEFAULT_CLIENT_ERROR_STATUS_CODE: Final[int] = 400
DEFAULT_NOT_FOUND_STATUS_CODE: Final[int] = 404

MAX_SAFE_STRING_LENGTH: Final[int] = 2_000
MAX_SAFE_SEQUENCE_ITEMS: Final[int] = 100
MAX_SAFE_MAPPING_ITEMS: Final[int] = 100
MAX_SAFE_DEPTH: Final[int] = 8


# ---------------------------------------------------------------------------
# Safe detail normalization
# ---------------------------------------------------------------------------

def _safe_string(value: Any, *, max_length: int = MAX_SAFE_STRING_LENGTH) -> str:
    """
    Wandelt einen beliebigen Wert defensiv in einen begrenzten String um.

    Fehlerdetails sollen niemals selbst neue Fehler auslösen.
    """
    try:
        text = str(value)
    except Exception:
        text = f"<unprintable {type(value).__name__}>"

    if len(text) <= max_length:
        return text

    return f"{text[:max_length]}…"


def _is_json_scalar(value: Any) -> bool:
    """
    Prüft, ob ein Wert direkt JSON-kompatibel ist.
    """
    return value is None or isinstance(value, (str, int, float, bool))


def make_json_safe(value: Any, *, depth: int = 0) -> Any:
    """
    Normalisiert einen beliebigen Wert in eine JSON-nahe Struktur.

    Diese Funktion ist absichtlich defensiv:
    - unbekannte Objekte werden zu Strings
    - Mappings werden rekursiv normalisiert
    - Sequenzen werden begrenzt
    - zu tiefe Strukturen werden abgeschnitten
    - Exceptions während der Normalisierung werden abgefangen
    """
    if depth > MAX_SAFE_DEPTH:
        return "<max-depth-reached>"

    try:
        if _is_json_scalar(value):
            return value

        if isinstance(value, Mapping):
            normalized: dict[str, Any] = {}

            for index, (key, item) in enumerate(value.items()):
                if index >= MAX_SAFE_MAPPING_ITEMS:
                    normalized["<truncated>"] = True
                    break

                safe_key = _safe_string(key, max_length=200)
                normalized[safe_key] = make_json_safe(item, depth=depth + 1)

            return normalized

        if isinstance(value, tuple):
            return [
                make_json_safe(item, depth=depth + 1)
                for item in value[:MAX_SAFE_SEQUENCE_ITEMS]
            ]

        if isinstance(value, list):
            return [
                make_json_safe(item, depth=depth + 1)
                for item in value[:MAX_SAFE_SEQUENCE_ITEMS]
            ]

        if isinstance(value, set | frozenset):
            return [
                make_json_safe(item, depth=depth + 1)
                for item in list(value)[:MAX_SAFE_SEQUENCE_ITEMS]
            ]

        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [
                make_json_safe(item, depth=depth + 1)
                for item in list(value)[:MAX_SAFE_SEQUENCE_ITEMS]
            ]

        if isinstance(value, bytes | bytearray):
            return {
                "type": type(value).__name__,
                "length": len(value),
            }

        if hasattr(value, "to_dict") and callable(value.to_dict):
            try:
                return make_json_safe(value.to_dict(), depth=depth + 1)
            except Exception:
                return _safe_string(value)

        if hasattr(value, "__dict__"):
            try:
                return make_json_safe(vars(value), depth=depth + 1)
            except Exception:
                return _safe_string(value)

        return _safe_string(value)

    except Exception:
        return f"<failed-to-normalize {type(value).__name__}>"


def normalize_error_details(details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    Normalisiert Fehlerdetails in ein JSON-fähiges Dictionary.
    """
    if details is None:
        return {}

    try:
        safe_details = make_json_safe(details)
    except Exception:
        return {
            "normalizationError": "Failed to normalize error details.",
        }

    if isinstance(safe_details, dict):
        return safe_details

    return {
        "details": safe_details,
    }


# ---------------------------------------------------------------------------
# Error payload
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorldErrorPayload:
    """
    JSON-nahe Repräsentation eines World-Fehlers.

    Diese Struktur ist framework-neutral und kann später in Flask-Routes
    direkt für jsonify verwendet werden.
    """

    code: str
    message: str
    details: dict[str, Any]
    status_code: int

    def to_dict(self, *, include_status_code: bool = False) -> dict[str, Any]:
        """
        Gibt den Fehler als API-nahe Struktur zurück.

        Standardmäßig wird status_code nicht innerhalb des error-Objekts
        ausgegeben, weil HTTP-Status und Body getrennte Schichten sind.
        Für Logs oder Tests kann include_status_code=True genutzt werden.
        """
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }

        if include_status_code:
            payload["statusCode"] = self.status_code

        return payload

    def to_api_response_body(self) -> dict[str, Any]:
        """
        Gibt eine vollständige spätere API-Response-Struktur zurück.
        """
        return {
            "ok": False,
            "error": self.to_dict(include_status_code=False),
        }


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------

class WorldError(Exception):
    """
    Basisklasse für alle Fehler aus src.world.

    Jede konkrete Fehlerklasse soll mindestens definieren:

        error_code
        default_message
        status_code

    Fehlerdetails müssen JSON-nah bleiben. Falls beliebige Objekte übergeben
    werden, werden sie defensiv normalisiert.
    """

    error_code: ClassVar[str] = "world_error"
    default_message: ClassVar[str] = "World operation failed."
    status_code: ClassVar[int] = DEFAULT_ERROR_STATUS_CODE

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = normalize_error_details(details)
        self.cause = cause
        self.code = code or self.error_code
        self.http_status_code = status_code or self.status_code

        super().__init__(self.message)

    def to_payload(self) -> WorldErrorPayload:
        """
        Gibt den Fehler als WorldErrorPayload zurück.
        """
        return WorldErrorPayload(
            code=self.code,
            message=self.message,
            details=self.details,
            status_code=self.http_status_code,
        )

    def to_dict(self, *, include_status_code: bool = False) -> dict[str, Any]:
        """
        Gibt das error-Objekt als Dictionary zurück.
        """
        return self.to_payload().to_dict(include_status_code=include_status_code)

    def to_api_response_body(self) -> dict[str, Any]:
        """
        Gibt eine vollständige spätere API-Response-Struktur zurück.
        """
        return self.to_payload().to_api_response_body()

    def to_log_dict(self) -> dict[str, Any]:
        """
        Gibt eine ausführlichere Log-Struktur zurück.

        Der verursachende Fehler wird bewusst nur typisiert und als String
        ausgegeben, damit keine unkontrollierten Objekte in Logs landen.
        """
        payload = self.to_dict(include_status_code=True)
        payload["exceptionType"] = type(self).__name__

        if self.cause is not None:
            payload["cause"] = {
                "type": type(self.cause).__name__,
                "message": _safe_string(self.cause),
            }

        return payload

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# General world-system errors
# ---------------------------------------------------------------------------

class WorldSystemError(WorldError):
    """
    Unerwarteter Fehler in der neutralen World-Schicht.
    """

    error_code: ClassVar[str] = "world_system_error"
    default_message: ClassVar[str] = "World system failed."
    status_code: ClassVar[int] = 500


class WorldRegistryError(WorldError):
    """
    Fehler beim Registrieren oder Auflösen von World-Providern.
    """

    error_code: ClassVar[str] = "world_registry_error"
    default_message: ClassVar[str] = "World registry operation failed."
    status_code: ClassVar[int] = 500


class WorldLoaderError(WorldError):
    """
    Fehler beim Laden einer Weltdefinition oder eines Providers.
    """

    error_code: ClassVar[str] = "world_loader_error"
    default_message: ClassVar[str] = "World loader operation failed."
    status_code: ClassVar[int] = 500


class WorldProviderError(WorldError):
    """
    Fehler innerhalb eines konkreten World-Providers.
    """

    error_code: ClassVar[str] = "world_provider_error"
    default_message: ClassVar[str] = "World provider operation failed."
    status_code: ClassVar[int] = 500


class WorldSerializationError(WorldError):
    """
    Fehler bei der Serialisierung von World- oder Chunk-Daten.
    """

    error_code: ClassVar[str] = "world_serialization_error"
    default_message: ClassVar[str] = "World serialization failed."
    status_code: ClassVar[int] = 500


# ---------------------------------------------------------------------------
# Specific expected errors
# ---------------------------------------------------------------------------

class WorldNotFoundError(WorldError):
    """
    Eine angefragte Welt ist nicht registriert oder nicht ladbar.
    """

    error_code: ClassVar[str] = "world_not_found"
    default_message: ClassVar[str] = "World was not found."
    status_code: ClassVar[int] = DEFAULT_NOT_FOUND_STATUS_CODE

    def __init__(
        self,
        world_id: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        merged_details: dict[str, Any] = {}

        if details:
            merged_details.update(dict(details))

        if world_id is not None:
            merged_details["worldId"] = world_id

        message = (
            f"World '{world_id}' was not found."
            if world_id
            else self.default_message
        )

        super().__init__(
            message,
            details=merged_details,
            cause=cause,
        )


class WorldConfigError(WorldError):
    """
    Die Konfiguration einer Welt fehlt, ist unlesbar oder strukturell defekt.
    """

    error_code: ClassVar[str] = "world_config_error"
    default_message: ClassVar[str] = "World configuration is invalid."
    status_code: ClassVar[int] = 500


class WorldValidationError(WorldError):
    """
    Eine Weltdefinition oder Chunk-Anfrage verletzt fachliche Regeln.
    """

    error_code: ClassVar[str] = "world_validation_error"
    default_message: ClassVar[str] = "World validation failed."
    status_code: ClassVar[int] = DEFAULT_CLIENT_ERROR_STATUS_CODE


class WorldGenerationError(WorldError):
    """
    Ein Chunk oder eine andere Weltstruktur konnte nicht generiert werden.
    """

    error_code: ClassVar[str] = "world_generation_error"
    default_message: ClassVar[str] = "World generation failed."
    status_code: ClassVar[int] = 500


class UnsupportedWorldTypeError(WorldError):
    """
    Der angeforderte oder konfigurierte Welt-Typ wird nicht unterstützt.
    """

    error_code: ClassVar[str] = "unsupported_world_type"
    default_message: ClassVar[str] = "World type is not supported."
    status_code: ClassVar[int] = DEFAULT_CLIENT_ERROR_STATUS_CODE

    def __init__(
        self,
        world_type: str | None = None,
        *,
        supported_world_types: Sequence[str] | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        merged_details: dict[str, Any] = {}

        if details:
            merged_details.update(dict(details))

        if world_type is not None:
            merged_details["worldType"] = world_type

        if supported_world_types is not None:
            merged_details["supportedWorldTypes"] = list(supported_world_types)

        message = (
            f"World type '{world_type}' is not supported."
            if world_type
            else self.default_message
        )

        super().__init__(
            message,
            details=merged_details,
            cause=cause,
        )


class UnknownGeneratorTypeError(WorldError):
    """
    Der konfigurierte Generator-Typ ist unbekannt.
    """

    error_code: ClassVar[str] = "unknown_generator_type"
    default_message: ClassVar[str] = "Generator type is not registered."
    status_code: ClassVar[int] = DEFAULT_CLIENT_ERROR_STATUS_CODE

    def __init__(
        self,
        generator_type: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        merged_details: dict[str, Any] = {}

        if details:
            merged_details.update(dict(details))

        if generator_type is not None:
            merged_details["generatorType"] = generator_type

        message = (
            f"Generator type '{generator_type}' is not registered."
            if generator_type
            else self.default_message
        )

        super().__init__(
            message,
            details=merged_details,
            cause=cause,
        )


class InvalidChunkRequestError(WorldValidationError):
    """
    Eine Chunk-Anfrage enthält ungültige oder fehlende Parameter.
    """

    error_code: ClassVar[str] = "invalid_chunk_request"
    default_message: ClassVar[str] = "Chunk request is invalid."
    status_code: ClassVar[int] = DEFAULT_CLIENT_ERROR_STATUS_CODE


class InvalidWorldDefinitionError(WorldValidationError):
    """
    Eine geladene Weltdefinition ist fachlich ungültig.
    """

    error_code: ClassVar[str] = "invalid_world_definition"
    default_message: ClassVar[str] = "World definition is invalid."
    status_code: ClassVar[int] = DEFAULT_CLIENT_ERROR_STATUS_CODE


class InvalidWorldConfigFileError(WorldConfigError):
    """
    Die world.json fehlt, ist nicht lesbar oder kein gültiges JSON.
    """

    error_code: ClassVar[str] = "invalid_world_config_file"
    default_message: ClassVar[str] = "World config file is missing or invalid."
    status_code: ClassVar[int] = 500


class WorldProviderImportError(WorldLoaderError):
    """
    Ein Provider-Modul konnte nicht importiert werden.
    """

    error_code: ClassVar[str] = "world_provider_import_error"
    default_message: ClassVar[str] = "World provider could not be imported."
    status_code: ClassVar[int] = 500


class WorldProviderContractError(WorldProviderError):
    """
    Ein Provider erfüllt die erwartete Schnittstelle nicht.
    """

    error_code: ClassVar[str] = "world_provider_contract_error"
    default_message: ClassVar[str] = "World provider contract is invalid."
    status_code: ClassVar[int] = 500


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def is_world_error(error: BaseException) -> bool:
    """
    Prüft, ob eine Exception aus der World-Schicht stammt.
    """
    return isinstance(error, WorldError)


def coerce_world_error(
    error: BaseException,
    *,
    fallback_message: str = "Unexpected world error.",
    fallback_code: str = "unexpected_world_error",
    fallback_status_code: int = 500,
    details: Mapping[str, Any] | None = None,
) -> WorldError:
    """
    Wandelt eine beliebige Exception defensiv in einen WorldError um.

    Bereits vorhandene WorldError-Instanzen werden unverändert zurückgegeben.
    """
    if isinstance(error, WorldError):
        return error

    merged_details: dict[str, Any] = {}

    if details:
        try:
            merged_details.update(dict(details))
        except Exception:
            merged_details["details"] = make_json_safe(details)

    merged_details["originalErrorType"] = type(error).__name__
    merged_details["originalErrorMessage"] = _safe_string(error)

    return WorldSystemError(
        fallback_message,
        details=merged_details,
        cause=error,
        code=fallback_code,
        status_code=fallback_status_code,
    )


def error_to_payload(error: BaseException) -> WorldErrorPayload:
    """
    Gibt für eine beliebige Exception ein WorldErrorPayload zurück.
    """
    world_error = coerce_world_error(error)
    return world_error.to_payload()


def error_to_api_response_body(error: BaseException) -> dict[str, Any]:
    """
    Gibt für eine beliebige Exception eine spätere API-Response-Struktur zurück.
    """
    return error_to_payload(error).to_api_response_body()


def error_to_log_dict(error: BaseException) -> dict[str, Any]:
    """
    Gibt für eine beliebige Exception eine robuste Log-Struktur zurück.
    """
    world_error = coerce_world_error(error)
    return world_error.to_log_dict()


__all__ = (
    "DEFAULT_ERROR_STATUS_CODE",
    "DEFAULT_CLIENT_ERROR_STATUS_CODE",
    "DEFAULT_NOT_FOUND_STATUS_CODE",
    "WorldErrorPayload",
    "WorldError",
    "WorldSystemError",
    "WorldRegistryError",
    "WorldLoaderError",
    "WorldProviderError",
    "WorldSerializationError",
    "WorldNotFoundError",
    "WorldConfigError",
    "WorldValidationError",
    "WorldGenerationError",
    "UnsupportedWorldTypeError",
    "UnknownGeneratorTypeError",
    "InvalidChunkRequestError",
    "InvalidWorldDefinitionError",
    "InvalidWorldConfigFileError",
    "WorldProviderImportError",
    "WorldProviderContractError",
    "make_json_safe",
    "normalize_error_details",
    "is_world_error",
    "coerce_world_error",
    "error_to_payload",
    "error_to_api_response_body",
    "error_to_log_dict",
)