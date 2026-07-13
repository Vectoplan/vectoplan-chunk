# services/vectoplan-chunk/src/georeferencing/transformer.py
"""Strikte, thread-sichere CRS-Transformationen für Earth v1.

Dieses Modul wählt mit ``pyproj.transformer.TransformerGroup`` eine konkrete
Koordinatenoperation aus und führt validierte 2D-/3D-Transformationen aus.

Wichtige Architekturregeln
---------------------------
* ``always_xy=True`` ist für Earth v1 verpflichtend.
* Ballpark-Transformationen sind standardmäßig verboten.
* ``require_best_available=True`` blockiert degradierte Operationen.
* Fehlende Grids werden diagnostiziert, aber niemals automatisch geladen.
* ``errcheck=True`` verhindert stille ``inf``-Ergebnisse.
* Roundtrip-Fehler werden in Metern gemessen und gegen die Policy geprüft.
* Native Transformer werden niemals zwischen Threads geteilt.
* Jeder Thread besitzt einen begrenzten LRU-Cache eigener Transformer.
* Ein globaler Cache-Generationszähler invalidiert alle Thread-Caches lazy.
* Persistiert werden nur Verträge und Ergebnisse, keine nativen Transformer.
* Das Modul führt kein Logging, keinen DB-Zugriff und keinen HTTP-Response aus.

Thread-Sicherheit
-----------------
Die pyproj-Dokumentation weist darauf hin, dass ``TransformerGroup`` und die
daraus zurückgegebenen Transformer nicht thread-sicher sind. Deshalb werden
native Objekte ausschließlich im erzeugenden Thread verwendet. Globale Caches
enthalten nur Zähler und eine Invalidierungsgeneration.
"""

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from importlib import import_module
import json
from math import hypot, isfinite
from threading import RLock, get_ident, local
from typing import Any, Final, Iterable, Self
import warnings

from ..coordinates.models import JsonValue
from .contracts import (
    CoordinateDimension,
    CoordinateTransformRequest,
    CoordinateTransformResult,
    CrsDefinition,
    GlobalCoordinate,
    TransformationAccuracy,
    TransformationOperationKind,
    TransformationPolicy,
    decimal_to_canonical_string,
)
from .crs import (
    CrsResolutionPolicy,
    canonical_geocentric_crs,
    canonical_geographic_crs,
    crs_equivalent,
    ensure_crs_runtime_ready,
    resolve_native_crs,
)
from .errors import (
    BallparkTransformationForbiddenError,
    CrsDimensionMismatchError,
    GeoreferencingConfigurationError,
    GeoreferencingValidationError,
    TransformationAccuracyUnknownError,
    TransformationFailedError,
    TransformationGridMissingError,
    TransformationNotExactError,
    TransformationPrecisionExceededError,
    TransformationUnavailableError,
)


_TRANSFORMER_THREAD_CACHE_SIZE: Final[int] = 64
_MAX_AUTHORITY_LENGTH: Final[int] = 64
_MAX_WARNING_LENGTH: Final[int] = 512
_MAX_OPERATION_NAME_LENGTH: Final[int] = 2_048
_MAX_PIPELINE_LENGTH: Final[int] = 262_144
_MAX_GRID_COUNT: Final[int] = 1_024
_MAX_BATCH_SIZE: Final[int] = 100_000

_THREAD_STATE = local()
_CACHE_LOCK = RLock()
_CACHE_GENERATION = 0
_CACHE_HITS = 0
_CACHE_MISSES = 0
_CACHE_EVICTIONS = 0
_SELECTIONS_CREATED = 0
_TRANSFORMATIONS_EXECUTED = 0
_TRANSFORMATIONS_FAILED = 0


@dataclass(frozen=True, slots=True)
class AreaOfInterestBounds:
    """Geografische Area of Interest in Grad.

    Antimeridian-übergreifende AOIs werden in Earth v1 nicht durch einen
    einzelnen Bounds-Vertrag dargestellt. Sie müssen in getrennte
    Transformationsanfragen zerlegt werden.
    """

    west_longitude_deg: float
    south_latitude_deg: float
    east_longitude_deg: float
    north_latitude_deg: float

    def __post_init__(self) -> None:
        west = _require_finite_float(
            self.west_longitude_deg,
            field_name="westLongitudeDeg",
        )
        south = _require_finite_float(
            self.south_latitude_deg,
            field_name="southLatitudeDeg",
        )
        east = _require_finite_float(
            self.east_longitude_deg,
            field_name="eastLongitudeDeg",
        )
        north = _require_finite_float(
            self.north_latitude_deg,
            field_name="northLatitudeDeg",
        )

        if west < -180.0 or west > 180.0:
            raise GeoreferencingValidationError(
                "westLongitudeDeg muss zwischen -180 und 180 liegen.",
                details={"value": west},
            )
        if east < -180.0 or east > 180.0:
            raise GeoreferencingValidationError(
                "eastLongitudeDeg muss zwischen -180 und 180 liegen.",
                details={"value": east},
            )
        if south < -90.0 or south > 90.0:
            raise GeoreferencingValidationError(
                "southLatitudeDeg muss zwischen -90 und 90 liegen.",
                details={"value": south},
            )
        if north < -90.0 or north > 90.0:
            raise GeoreferencingValidationError(
                "northLatitudeDeg muss zwischen -90 und 90 liegen.",
                details={"value": north},
            )
        if west >= east:
            raise GeoreferencingValidationError(
                "Earth v1 AOI muss westLongitudeDeg < eastLongitudeDeg erfüllen.",
                details={
                    "westLongitudeDeg": west,
                    "eastLongitudeDeg": east,
                    "antimeridianCrossingSupported": False,
                },
            )
        if south >= north:
            raise GeoreferencingValidationError(
                "AOI muss southLatitudeDeg < northLatitudeDeg erfüllen.",
                details={
                    "southLatitudeDeg": south,
                    "northLatitudeDeg": north,
                },
            )

        object.__setattr__(self, "west_longitude_deg", west)
        object.__setattr__(self, "south_latitude_deg", south)
        object.__setattr__(self, "east_longitude_deg", east)
        object.__setattr__(self, "north_latitude_deg", north)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        if not isinstance(payload, Mapping):
            raise GeoreferencingValidationError(
                "AreaOfInterestBounds muss als Mapping übergeben werden.",
                details={"actualType": type(payload).__name__},
            )

        required = (
            "westLongitudeDeg",
            "southLatitudeDeg",
            "eastLongitudeDeg",
            "northLatitudeDeg",
        )
        missing = [field for field in required if field not in payload]
        if missing:
            raise GeoreferencingValidationError(
                "AreaOfInterestBounds-Pflichtfelder fehlen.",
                details={"missingFields": missing},
            )

        return cls(
            west_longitude_deg=payload["westLongitudeDeg"],
            south_latitude_deg=payload["southLatitudeDeg"],
            east_longitude_deg=payload["eastLongitudeDeg"],
            north_latitude_deg=payload["northLatitudeDeg"],
        )

    @property
    def cache_key(self) -> str:
        return (
            f"{self.west_longitude_deg:.12g},"
            f"{self.south_latitude_deg:.12g},"
            f"{self.east_longitude_deg:.12g},"
            f"{self.north_latitude_deg:.12g}"
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "westLongitudeDeg": self.west_longitude_deg,
            "southLatitudeDeg": self.south_latitude_deg,
            "eastLongitudeDeg": self.east_longitude_deg,
            "northLatitudeDeg": self.north_latitude_deg,
        }


@dataclass(frozen=True, slots=True)
class TransformerSelectionOptions:
    """Zusätzliche, nicht fachliche Auswahlparameter für TransformerGroup."""

    area_of_interest: AreaOfInterestBounds | None = None
    authority: str | None = None
    allow_superseded: bool = False

    def __post_init__(self) -> None:
        if self.area_of_interest is not None and not isinstance(
            self.area_of_interest,
            AreaOfInterestBounds,
        ):
            raise GeoreferencingValidationError(
                "area_of_interest muss AreaOfInterestBounds sein.",
                details={
                    "actualType": type(self.area_of_interest).__name__,
                },
            )

        object.__setattr__(
            self,
            "authority",
            _normalize_optional_authority(self.authority),
        )
        object.__setattr__(
            self,
            "allow_superseded",
            bool(self.allow_superseded),
        )

    @classmethod
    def default(cls) -> Self:
        return cls()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "areaOfInterest": (
                self.area_of_interest.to_dict()
                if self.area_of_interest is not None
                else None
            ),
            "authority": self.authority,
            "allowSuperseded": self.allow_superseded,
        }


@dataclass(frozen=True, slots=True)
class GridRequirement:
    """Sichere Diagnose eines von PROJ referenzierten Transformationsgrids."""

    short_name: str
    available: bool
    package_name: str | None = None
    url_fingerprint: str | None = None
    direct_download: bool | None = None
    open_license: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "short_name",
            _normalize_text(
                self.short_name,
                field_name="shortName",
                maximum_length=512,
            ),
        )
        object.__setattr__(
            self,
            "available",
            bool(self.available),
        )
        object.__setattr__(
            self,
            "package_name",
            _normalize_optional_text(
                self.package_name,
                field_name="packageName",
                maximum_length=512,
            ),
        )
        object.__setattr__(
            self,
            "url_fingerprint",
            _normalize_optional_text(
                self.url_fingerprint,
                field_name="urlFingerprint",
                maximum_length=128,
            ),
        )
        object.__setattr__(
            self,
            "direct_download",
            (
                bool(self.direct_download)
                if self.direct_download is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "open_license",
            (
                bool(self.open_license)
                if self.open_license is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "shortName": self.short_name,
            "available": self.available,
            "packageName": self.package_name,
            "urlFingerprint": self.url_fingerprint,
            "directDownload": self.direct_download,
            "openLicense": self.open_license,
        }


@dataclass(frozen=True, slots=True)
class TransformerDescriptor:
    """Serialisierbare Beschreibung einer ausgewählten Operation."""

    cache_key: str
    source_crs_id: str
    target_crs_id: str
    operation_name: str
    pipeline: str | None
    reported_accuracy_m: Decimal | None
    best_available: bool
    ballpark: bool
    has_inverse: bool
    network_enabled: bool
    available_transformer_count: int
    unavailable_operation_count: int
    required_grids: tuple[GridRequirement, ...] = ()
    missing_grids: tuple[GridRequirement, ...] = ()
    selection_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cache_key",
            _normalize_hash(self.cache_key, field_name="cacheKey"),
        )
        object.__setattr__(
            self,
            "source_crs_id",
            _normalize_text(
                self.source_crs_id,
                field_name="sourceCrsId",
                maximum_length=256,
            ),
        )
        object.__setattr__(
            self,
            "target_crs_id",
            _normalize_text(
                self.target_crs_id,
                field_name="targetCrsId",
                maximum_length=256,
            ),
        )
        object.__setattr__(
            self,
            "operation_name",
            _normalize_text(
                self.operation_name,
                field_name="operationName",
                maximum_length=_MAX_OPERATION_NAME_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "pipeline",
            _normalize_optional_text(
                self.pipeline,
                field_name="pipeline",
                maximum_length=_MAX_PIPELINE_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "reported_accuracy_m",
            (
                _normalize_non_negative_decimal(
                    self.reported_accuracy_m,
                    field_name="reportedAccuracyM",
                )
                if self.reported_accuracy_m is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "best_available",
            bool(self.best_available),
        )
        object.__setattr__(self, "ballpark", bool(self.ballpark))
        object.__setattr__(
            self,
            "has_inverse",
            bool(self.has_inverse),
        )
        object.__setattr__(
            self,
            "network_enabled",
            bool(self.network_enabled),
        )
        object.__setattr__(
            self,
            "available_transformer_count",
            _normalize_non_negative_int(
                self.available_transformer_count,
                field_name="availableTransformerCount",
            ),
        )
        object.__setattr__(
            self,
            "unavailable_operation_count",
            _normalize_non_negative_int(
                self.unavailable_operation_count,
                field_name="unavailableOperationCount",
            ),
        )
        object.__setattr__(
            self,
            "required_grids",
            _normalize_grid_tuple(
                self.required_grids,
                field_name="requiredGrids",
            ),
        )
        object.__setattr__(
            self,
            "missing_grids",
            _normalize_grid_tuple(
                self.missing_grids,
                field_name="missingGrids",
            ),
        )
        object.__setattr__(
            self,
            "selection_warnings",
            _normalize_warning_tuple(self.selection_warnings),
        )

    @property
    def pipeline_fingerprint(self) -> str | None:
        if self.pipeline is None:
            return None
        return sha256(self.pipeline.encode("utf-8")).hexdigest()

    def to_dict(
        self,
        *,
        include_pipeline: bool = False,
    ) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "cacheKey": self.cache_key,
            "sourceCrsId": self.source_crs_id,
            "targetCrsId": self.target_crs_id,
            "operationName": self.operation_name,
            "pipelineLength": len(self.pipeline or ""),
            "pipelineFingerprint": self.pipeline_fingerprint,
            "reportedAccuracyM": (
                decimal_to_canonical_string(
                    self.reported_accuracy_m
                )
                if self.reported_accuracy_m is not None
                else None
            ),
            "bestAvailable": self.best_available,
            "ballpark": self.ballpark,
            "hasInverse": self.has_inverse,
            "networkEnabled": self.network_enabled,
            "availableTransformerCount": (
                self.available_transformer_count
            ),
            "unavailableOperationCount": (
                self.unavailable_operation_count
            ),
            "requiredGrids": [
                grid.to_dict() for grid in self.required_grids
            ],
            "missingGrids": [
                grid.to_dict() for grid in self.missing_grids
            ],
            "selectionWarnings": list(self.selection_warnings),
        }
        if include_pipeline:
            payload["pipeline"] = self.pipeline
        return payload


@dataclass(frozen=True, slots=True)
class TransformerSelection:
    """Öffentliches, natives Objekt-freies Ergebnis der Operationsauswahl."""

    descriptor: TransformerDescriptor
    source_crs: CrsDefinition
    target_crs: CrsDefinition
    options: TransformerSelectionOptions
    policy: TransformationPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, TransformerDescriptor):
            raise GeoreferencingValidationError(
                "descriptor muss ein TransformerDescriptor sein."
            )
        if not isinstance(self.source_crs, CrsDefinition):
            raise GeoreferencingValidationError(
                "source_crs muss eine CrsDefinition sein."
            )
        if not isinstance(self.target_crs, CrsDefinition):
            raise GeoreferencingValidationError(
                "target_crs muss eine CrsDefinition sein."
            )
        if not isinstance(self.options, TransformerSelectionOptions):
            raise GeoreferencingValidationError(
                "options muss TransformerSelectionOptions sein."
            )
        if not isinstance(self.policy, TransformationPolicy):
            raise GeoreferencingValidationError(
                "policy muss TransformationPolicy sein."
            )

    def to_dict(
        self,
        *,
        include_pipeline: bool = False,
    ) -> dict[str, JsonValue]:
        return {
            "descriptor": self.descriptor.to_dict(
                include_pipeline=include_pipeline
            ),
            "sourceCrs": self.source_crs.to_dict(),
            "targetCrs": self.target_crs.to_dict(),
            "options": self.options.to_dict(),
            "policy": self.policy.to_dict(),
        }


@dataclass(slots=True)
class _ThreadTransformerEntry:
    """Nur im erzeugenden Thread verwendbares natives Transformerobjekt."""

    thread_id: int
    native_transformer: Any
    selection: TransformerSelection


@dataclass(slots=True)
class _ThreadTransformerState:
    generation: int
    entries: OrderedDict[str, _ThreadTransformerEntry]


def select_transformer(
    source_crs: CrsDefinition,
    target_crs: CrsDefinition,
    *,
    policy: TransformationPolicy | None = None,
    options: TransformerSelectionOptions | None = None,
) -> TransformerSelection:
    """Wählt eine konkrete, policykonforme Transformation aus.

    Das zurückgegebene Objekt enthält keine native pyproj-Instanz und kann
    daher sicher zwischen Schichten serialisiert oder weitergereicht werden.
    """

    active_policy = policy or TransformationPolicy.strict_default()
    active_options = options or TransformerSelectionOptions.default()

    if not isinstance(source_crs, CrsDefinition):
        raise GeoreferencingValidationError(
            "source_crs muss eine CrsDefinition sein.",
            details={"actualType": type(source_crs).__name__},
        )
    if not isinstance(target_crs, CrsDefinition):
        raise GeoreferencingValidationError(
            "target_crs muss eine CrsDefinition sein.",
            details={"actualType": type(target_crs).__name__},
        )
    if not isinstance(active_policy, TransformationPolicy):
        raise GeoreferencingValidationError(
            "policy muss eine TransformationPolicy sein.",
            details={"actualType": type(active_policy).__name__},
        )
    if not isinstance(active_options, TransformerSelectionOptions):
        raise GeoreferencingValidationError(
            "options muss TransformerSelectionOptions sein.",
            details={"actualType": type(active_options).__name__},
        )

    entry = _get_or_create_thread_transformer(
        source_crs=source_crs,
        target_crs=target_crs,
        policy=active_policy,
        options=active_options,
    )
    return entry.selection


def transform_coordinate(
    request: CoordinateTransformRequest,
    *,
    options: TransformerSelectionOptions | None = None,
) -> CoordinateTransformResult:
    """Transformiert genau eine 2D- oder 3D-Koordinate strikt."""

    if not isinstance(request, CoordinateTransformRequest):
        raise GeoreferencingValidationError(
            "request muss ein CoordinateTransformRequest sein.",
            details={"actualType": type(request).__name__},
        )

    active_options = options or TransformerSelectionOptions.default()
    if not isinstance(active_options, TransformerSelectionOptions):
        raise GeoreferencingValidationError(
            "options muss TransformerSelectionOptions sein.",
            details={"actualType": type(active_options).__name__},
        )

    if request.is_identity_transform:
        accuracy = TransformationAccuracy(
            best_available=True,
            ballpark=False,
            reported_accuracy_m=Decimal("0"),
            measured_roundtrip_error_m=Decimal("0"),
        )
        _record_transformation_success()
        return CoordinateTransformResult(
            request=request,
            coordinate=request.coordinate,
            accuracy=accuracy,
            operation_name="identity",
            pipeline=None,
        )

    entry = _get_or_create_thread_transformer(
        source_crs=request.source_crs,
        target_crs=request.target_crs,
        policy=request.policy,
        options=active_options,
    )
    _assert_thread_ownership(entry)

    try:
        transformed = _transform_forward(
            entry.native_transformer,
            request.coordinate,
            target_crs=request.target_crs,
        )

        roundtrip_error = None
        if request.policy.validate_roundtrip:
            if not entry.selection.descriptor.has_inverse:
                raise TransformationNotExactError.for_operation(
                    source_crs=request.source_crs.crs_id,
                    target_crs=request.target_crs.crs_id,
                    reason=(
                        "Die ausgewählte Transformation besitzt keine "
                        "Inverse für die Roundtrip-Prüfung."
                    ),
                )

            returned = _transform_inverse(
                entry.native_transformer,
                transformed,
                source_crs=request.source_crs,
            )
            source_native = resolve_native_crs(
                request.source_crs,
                policy=_contract_resolution_policy(
                    request.source_crs,
                    role="source",
                ),
            )
            roundtrip_error = _measure_roundtrip_error_m(
                source_native,
                original=request.coordinate,
                returned=returned,
            )

        descriptor = _descriptor_after_transform(
            entry,
            request=request,
        )
        accuracy = TransformationAccuracy(
            best_available=descriptor.best_available,
            ballpark=descriptor.ballpark,
            reported_accuracy_m=descriptor.reported_accuracy_m,
            measured_roundtrip_error_m=roundtrip_error,
            required_grids=tuple(
                grid.short_name
                for grid in descriptor.required_grids
            ),
            missing_grids=tuple(
                grid.short_name
                for grid in descriptor.missing_grids
            ),
        )

        result = CoordinateTransformResult(
            request=request,
            coordinate=transformed,
            accuracy=accuracy,
            operation_name=descriptor.operation_name,
            pipeline=descriptor.pipeline,
        )
        _record_transformation_success()
        return result
    except (
        GeoreferencingValidationError,
        GeoreferencingConfigurationError,
    ):
        _record_transformation_failure()
        raise
    except Exception as error:
        _record_transformation_failure()
        raise TransformationFailedError.from_cause(
            error,
            source_crs=request.source_crs.crs_id,
            target_crs=request.target_crs.crs_id,
            operation=request.operation.value,
        ) from error


def transform_coordinate_batch(
    requests: Sequence[CoordinateTransformRequest],
    *,
    options: TransformerSelectionOptions | None = None,
) -> tuple[CoordinateTransformResult, ...]:
    """Transformiert eine begrenzte Folge von Einzelverträgen.

    Die Funktion bewahrt Eingabereihenfolge und Fehleratomizität auf
    Anwendungsebene: Beim ersten Fehler wird abgebrochen und kein partielles
    Ergebnis zurückgegeben. Persistenz ist nicht Bestandteil dieses Moduls.
    """

    if isinstance(requests, (str, bytes, bytearray)) or not isinstance(
        requests,
        Sequence,
    ):
        raise GeoreferencingValidationError(
            "requests muss eine Sequenz sein.",
            details={"actualType": type(requests).__name__},
        )

    if len(requests) > _MAX_BATCH_SIZE:
        raise GeoreferencingValidationError(
            "Transformationsbatch überschreitet die maximale Größe.",
            details={
                "count": len(requests),
                "maximumBatchSize": _MAX_BATCH_SIZE,
            },
        )

    results: list[CoordinateTransformResult] = []
    for index, request in enumerate(requests):
        if not isinstance(request, CoordinateTransformRequest):
            raise GeoreferencingValidationError(
                "Batch enthält einen ungültigen Transformationsauftrag.",
                details={
                    "index": index,
                    "actualType": type(request).__name__,
                },
            )
        results.append(
            transform_coordinate(request, options=options)
        )
    return tuple(results)


def transformer_runtime_status() -> dict[str, JsonValue]:
    """Prüft Auswahl und Roundtrip der kanonischen Earth-CRS read-only."""

    payload: dict[str, JsonValue] = {
        "ok": False,
        "crsRuntimeReady": False,
        "canonicalTransformReady": False,
        "roundtripReady": False,
        "selection": None,
        "result": None,
        "cache": transformer_cache_info(),
        "errors": [],
    }

    errors: list[JsonValue] = payload["errors"]  # type: ignore[assignment]

    try:
        ensure_crs_runtime_ready(require_network_disabled=True)
        payload["crsRuntimeReady"] = True
    except Exception as error:
        errors.append(_safe_error(error))
        return payload

    try:
        source = canonical_geographic_crs()
        target = canonical_geocentric_crs()
        policy = TransformationPolicy(
            allow_ballpark=False,
            require_best_available=True,
            require_known_accuracy=False,
            maximum_accuracy_m=None,
            validate_roundtrip=True,
            maximum_roundtrip_error_m=Decimal("0.001"),
            always_xy=True,
        )
        request = CoordinateTransformRequest(
            coordinate=GlobalCoordinate.from_values(
                "11.576",
                "48.137",
                "560.0",
            ),
            source_crs=source,
            target_crs=target,
            operation=(
                TransformationOperationKind.REFERENCE_TO_CANONICAL
            ),
            policy=policy,
            request_id="transformer-readiness",
        )
        selection = select_transformer(
            source,
            target,
            policy=policy,
        )
        payload["selection"] = selection.to_dict()
        payload["canonicalTransformReady"] = True

        result = transform_coordinate(request)
        payload["result"] = result.to_dict()
        payload["roundtripReady"] = True
    except Exception as error:
        errors.append(_safe_error(error))

    payload["cache"] = transformer_cache_info()
    payload["ok"] = bool(
        payload["crsRuntimeReady"]
        and payload["canonicalTransformReady"]
        and payload["roundtripReady"]
        and not errors
    )
    return payload


def clear_transformer_caches() -> dict[str, JsonValue]:
    """Invalidiert alle Thread-Caches generation-basiert.

    Andere Threads können nicht direkt manipuliert werden. Durch Erhöhung der
    Generation verwerfen sie ihren lokalen Cache beim nächsten Zugriff.
    """

    global _CACHE_GENERATION
    global _CACHE_HITS
    global _CACHE_MISSES
    global _CACHE_EVICTIONS

    with _CACHE_LOCK:
        _CACHE_GENERATION += 1
        _CACHE_HITS = 0
        _CACHE_MISSES = 0
        _CACHE_EVICTIONS = 0
        generation = _CACHE_GENERATION

    state = getattr(_THREAD_STATE, "transformers", None)
    if isinstance(state, _ThreadTransformerState):
        state.entries.clear()
        state.generation = generation

    return {
        "ok": True,
        "generation": generation,
        "currentThreadCacheCleared": True,
    }


def transformer_cache_info() -> dict[str, JsonValue]:
    """Liefert globale Zähler und Status des aktuellen Thread-Caches."""

    with _CACHE_LOCK:
        generation = _CACHE_GENERATION
        hits = _CACHE_HITS
        misses = _CACHE_MISSES
        evictions = _CACHE_EVICTIONS
        selections = _SELECTIONS_CREATED
        executed = _TRANSFORMATIONS_EXECUTED
        failed = _TRANSFORMATIONS_FAILED

    state = getattr(_THREAD_STATE, "transformers", None)
    current_size = (
        len(state.entries)
        if isinstance(state, _ThreadTransformerState)
        and state.generation == generation
        else 0
    )

    return {
        "generation": generation,
        "threadLocal": True,
        "currentThreadId": get_ident(),
        "currentThreadSize": current_size,
        "maximumThreadSize": _TRANSFORMER_THREAD_CACHE_SIZE,
        "hits": hits,
        "misses": misses,
        "evictions": evictions,
        "selectionsCreated": selections,
        "transformationsExecuted": executed,
        "transformationsFailed": failed,
    }


def _get_or_create_thread_transformer(
    *,
    source_crs: CrsDefinition,
    target_crs: CrsDefinition,
    policy: TransformationPolicy,
    options: TransformerSelectionOptions,
) -> _ThreadTransformerEntry:
    global _CACHE_HITS
    global _CACHE_MISSES
    global _CACHE_EVICTIONS
    global _SELECTIONS_CREATED

    cache_key = _selection_cache_key(
        source_crs=source_crs,
        target_crs=target_crs,
        policy=policy,
        options=options,
    )
    state = _get_thread_state()

    existing = state.entries.get(cache_key)
    if existing is not None:
        state.entries.move_to_end(cache_key)
        with _CACHE_LOCK:
            _CACHE_HITS += 1
        return existing

    with _CACHE_LOCK:
        _CACHE_MISSES += 1

    entry = _create_thread_transformer(
        cache_key=cache_key,
        source_crs=source_crs,
        target_crs=target_crs,
        policy=policy,
        options=options,
    )
    state.entries[cache_key] = entry
    state.entries.move_to_end(cache_key)

    if len(state.entries) > _TRANSFORMER_THREAD_CACHE_SIZE:
        state.entries.popitem(last=False)
        with _CACHE_LOCK:
            _CACHE_EVICTIONS += 1

    with _CACHE_LOCK:
        _SELECTIONS_CREATED += 1

    return entry


def _create_thread_transformer(
    *,
    cache_key: str,
    source_crs: CrsDefinition,
    target_crs: CrsDefinition,
    policy: TransformationPolicy,
    options: TransformerSelectionOptions,
) -> _ThreadTransformerEntry:
    source_native = resolve_native_crs(
        source_crs,
        policy=_contract_resolution_policy(
            source_crs,
            role="source",
        ),
    )
    target_native = resolve_native_crs(
        target_crs,
        policy=_contract_resolution_policy(
            target_crs,
            role="target",
        ),
    )

    pyproj_transformer = _import_pyproj_transformer_module()
    area_of_interest = _build_native_area_of_interest(
        options.area_of_interest,
        pyproj_transformer=pyproj_transformer,
    )

    desired_accuracy = (
        float(policy.maximum_accuracy_m)
        if policy.maximum_accuracy_m is not None
        else None
    )

    captured_warnings: tuple[str, ...]
    try:
        with warnings.catch_warnings(record=True) as warning_records:
            warnings.simplefilter("always")
            group = pyproj_transformer.TransformerGroup(
                source_native,
                target_native,
                always_xy=True,
                area_of_interest=area_of_interest,
                authority=options.authority,
                accuracy=desired_accuracy,
                allow_ballpark=policy.allow_ballpark,
                allow_superseded=options.allow_superseded,
            )
        captured_warnings = _summarize_warnings(warning_records)
    except Exception as error:
        raise TransformationUnavailableError.for_pair(
            source_crs=source_crs.crs_id,
            target_crs=target_crs.crs_id,
            operation="transformer-group-selection",
        ) from error

    available = tuple(group.transformers)
    unavailable = tuple(group.unavailable_operations)
    missing_grids = _collect_grids(
        unavailable,
        only_missing=True,
    )

    if not available:
        if missing_grids:
            raise TransformationGridMissingError(
                details={
                    "sourceCrs": source_crs.crs_id,
                    "targetCrs": target_crs.crs_id,
                    "missingGrids": [
                        grid.to_dict() for grid in missing_grids
                    ],
                    "automaticDownloadAllowed": False,
                }
            )
        raise TransformationUnavailableError.for_pair(
            source_crs=source_crs.crs_id,
            target_crs=target_crs.crs_id,
            operation="transformer-group-selection",
        )

    if policy.require_best_available and not bool(group.best_available):
        if missing_grids:
            raise TransformationGridMissingError(
                details={
                    "sourceCrs": source_crs.crs_id,
                    "targetCrs": target_crs.crs_id,
                    "missingGrids": [
                        grid.to_dict() for grid in missing_grids
                    ],
                    "bestAvailable": False,
                    "automaticDownloadAllowed": False,
                }
            )
        raise TransformationNotExactError.for_operation(
            source_crs=source_crs.crs_id,
            target_crs=target_crs.crs_id,
            reason=(
                "TransformerGroup meldet best_available=False."
            ),
        )

    transformer = available[0]
    ballpark = _transformer_uses_ballpark(transformer)
    if ballpark and not policy.allow_ballpark:
        raise BallparkTransformationForbiddenError.for_pair(
            source_crs=source_crs.crs_id,
            target_crs=target_crs.crs_id,
        )

    reported_accuracy = _reported_accuracy(transformer)
    if (
        policy.require_known_accuracy
        and reported_accuracy is None
    ):
        raise TransformationAccuracyUnknownError.for_operation(
            source_crs=source_crs.crs_id,
            target_crs=target_crs.crs_id,
            required_accuracy=(
                float(policy.maximum_accuracy_m)
                if policy.maximum_accuracy_m is not None
                else None
            ),
        )

    if (
        policy.maximum_accuracy_m is not None
        and reported_accuracy is not None
        and reported_accuracy > policy.maximum_accuracy_m
    ):
        raise TransformationPrecisionExceededError.for_error(
            measured_error=float(reported_accuracy),
            allowed_error=float(policy.maximum_accuracy_m),
            unit="metre",
            operation="transformer-selection",
        )

    descriptor = TransformerDescriptor(
        cache_key=cache_key,
        source_crs_id=source_crs.crs_id,
        target_crs_id=target_crs.crs_id,
        operation_name=_operation_name(transformer),
        pipeline=_operation_pipeline(transformer),
        reported_accuracy_m=reported_accuracy,
        best_available=bool(group.best_available),
        ballpark=ballpark,
        has_inverse=bool(
            getattr(transformer, "has_inverse", False)
        ),
        network_enabled=bool(
            getattr(transformer, "is_network_enabled", False)
        ),
        available_transformer_count=len(available),
        unavailable_operation_count=len(unavailable),
        required_grids=_collect_grids(
            getattr(transformer, "operations", ()) or (),
            only_missing=False,
        ),
        missing_grids=missing_grids,
        selection_warnings=captured_warnings,
    )
    selection = TransformerSelection(
        descriptor=descriptor,
        source_crs=source_crs,
        target_crs=target_crs,
        options=options,
        policy=policy,
    )
    return _ThreadTransformerEntry(
        thread_id=get_ident(),
        native_transformer=transformer,
        selection=selection,
    )


def _transform_forward(
    transformer: Any,
    coordinate: GlobalCoordinate,
    *,
    target_crs: CrsDefinition,
) -> GlobalCoordinate:
    values = coordinate.as_float_tuple()

    try:
        if len(values) == 2:
            result = transformer.transform(
                values[0],
                values[1],
                radians=False,
                errcheck=True,
            )
        else:
            result = transformer.transform(
                values[0],
                values[1],
                values[2],
                radians=False,
                errcheck=True,
            )
    except Exception as error:
        raise TransformationFailedError.from_cause(
            error,
            source_crs=getattr(
                getattr(transformer, "source_crs", None),
                "name",
                "source",
            ),
            target_crs=target_crs.crs_id,
            operation="forward",
        ) from error

    return _coordinate_from_transform_result(
        result,
        expected_max_dimension=int(
            target_crs.coordinate_dimension
        ),
        role="target",
    )


def _transform_inverse(
    transformer: Any,
    coordinate: GlobalCoordinate,
    *,
    source_crs: CrsDefinition,
) -> GlobalCoordinate:
    try:
        enums = import_module("pyproj.enums")
        inverse = enums.TransformDirection.INVERSE
    except Exception as error:
        raise GeoreferencingConfigurationError(
            "pyproj TransformDirection konnte nicht geladen werden.",
            details={"causeType": type(error).__name__},
            cause=error,
        ) from error

    values = coordinate.as_float_tuple()
    try:
        if len(values) == 2:
            result = transformer.transform(
                values[0],
                values[1],
                radians=False,
                errcheck=True,
                direction=inverse,
            )
        else:
            result = transformer.transform(
                values[0],
                values[1],
                values[2],
                radians=False,
                errcheck=True,
                direction=inverse,
            )
    except Exception as error:
        raise TransformationFailedError.from_cause(
            error,
            source_crs=getattr(
                getattr(transformer, "target_crs", None),
                "name",
                "target",
            ),
            target_crs=source_crs.crs_id,
            operation="inverse-roundtrip",
        ) from error

    return _coordinate_from_transform_result(
        result,
        expected_max_dimension=int(
            source_crs.coordinate_dimension
        ),
        role="source",
    )


def _coordinate_from_transform_result(
    result: Any,
    *,
    expected_max_dimension: int,
    role: str,
) -> GlobalCoordinate:
    if (
        not isinstance(result, Sequence)
        or isinstance(result, (str, bytes, bytearray))
    ):
        raise TransformationFailedError(
            details={
                "operation": f"{role}-result-validation",
                "reason": "transform result is not a sequence",
                "actualType": type(result).__name__,
            }
        )

    values = tuple(result)
    if len(values) not in {2, 3}:
        raise CrsDimensionMismatchError.for_dimensions(
            crs=role,
            expected_dimensions=expected_max_dimension,
            actual_dimensions=len(values),
        )

    if len(values) > expected_max_dimension:
        raise CrsDimensionMismatchError.for_dimensions(
            crs=role,
            expected_dimensions=expected_max_dimension,
            actual_dimensions=len(values),
        )

    normalized: list[float] = []
    for index, value in enumerate(values):
        number = _require_finite_float(
            value,
            field_name=f"{role}[{index}]",
        )
        normalized.append(number)

    if len(normalized) == 2:
        return GlobalCoordinate.from_values(
            normalized[0],
            normalized[1],
        )
    return GlobalCoordinate.from_values(
        normalized[0],
        normalized[1],
        normalized[2],
    )


def _measure_roundtrip_error_m(
    source_native: Any,
    *,
    original: GlobalCoordinate,
    returned: GlobalCoordinate,
) -> Decimal:
    if original.dimension != returned.dimension:
        raise CrsDimensionMismatchError.for_dimensions(
            crs=getattr(source_native, "name", "source"),
            expected_dimensions=int(original.dimension),
            actual_dimensions=int(returned.dimension),
        )

    if _is_geographic_coordinate_space(source_native):
        return _geographic_error_m(
            source_native,
            original=original,
            returned=returned,
        )

    return _linear_error_m(
        source_native,
        original=original,
        returned=returned,
    )


def _is_geographic_coordinate_space(native: Any) -> bool:
    if bool(getattr(native, "is_geographic", False)):
        return True

    if bool(getattr(native, "is_compound", False)):
        try:
            sub_crs_list = tuple(native.sub_crs_list or ())
        except Exception:
            return False
        return bool(
            sub_crs_list
            and getattr(sub_crs_list[0], "is_geographic", False)
        )

    return False


def _geographic_error_m(
    native: Any,
    *,
    original: GlobalCoordinate,
    returned: GlobalCoordinate,
) -> Decimal:
    horizontal_crs = native
    if bool(getattr(native, "is_compound", False)):
        sub_crs_list = tuple(native.sub_crs_list or ())
        if sub_crs_list:
            horizontal_crs = sub_crs_list[0]

    try:
        geod = horizontal_crs.get_geod()
        _, _, horizontal_distance = geod.inv(
            float(original.x),
            float(original.y),
            float(returned.x),
            float(returned.y),
        )
    except Exception as error:
        raise TransformationAccuracyUnknownError.for_operation(
            source_crs=getattr(native, "name", "source"),
            target_crs=getattr(native, "name", "source"),
        ) from error

    if not isfinite(horizontal_distance):
        raise TransformationAccuracyUnknownError.for_operation(
            source_crs=getattr(native, "name", "source"),
            target_crs=getattr(native, "name", "source"),
        )

    vertical_distance = 0.0
    if original.z is not None and returned.z is not None:
        factor = _vertical_unit_to_metre_factor(native)
        vertical_distance = abs(
            float(returned.z - original.z)
        ) * factor

    return Decimal(
        str(hypot(abs(horizontal_distance), vertical_distance))
    )


def _linear_error_m(
    native: Any,
    *,
    original: GlobalCoordinate,
    returned: GlobalCoordinate,
) -> Decimal:
    try:
        axis_info = tuple(native.axis_info or ())
    except Exception as error:
        raise TransformationAccuracyUnknownError.for_operation(
            source_crs=getattr(native, "name", "source"),
            target_crs=getattr(native, "name", "source"),
        ) from error

    original_values = original.as_decimal_tuple()
    returned_values = returned.as_decimal_tuple()

    if len(axis_info) < len(original_values):
        raise TransformationAccuracyUnknownError.for_operation(
            source_crs=getattr(native, "name", "source"),
            target_crs=getattr(native, "name", "source"),
        )

    squared = 0.0
    for index, (before, after) in enumerate(
        zip(original_values, returned_values)
    ):
        factor = getattr(
            axis_info[index],
            "unit_conversion_factor",
            None,
        )
        if factor is None:
            raise TransformationAccuracyUnknownError.for_operation(
                source_crs=getattr(native, "name", "source"),
                target_crs=getattr(native, "name", "source"),
            )
        factor_value = _require_finite_float(
            factor,
            field_name=f"unitConversionFactor[{index}]",
        )
        delta_m = float(after - before) * factor_value
        squared += delta_m * delta_m

    return Decimal(str(squared**0.5))


def _vertical_unit_to_metre_factor(native: Any) -> float:
    try:
        axis_info = tuple(native.axis_info or ())
    except Exception:
        return 1.0

    if len(axis_info) < 3:
        return 1.0

    factor = getattr(axis_info[2], "unit_conversion_factor", None)
    if factor is None:
        return 1.0
    return _require_finite_float(
        factor,
        field_name="verticalUnitConversionFactor",
    )


def _descriptor_after_transform(
    entry: _ThreadTransformerEntry,
    *,
    request: CoordinateTransformRequest,
) -> TransformerDescriptor:
    transformer = entry.native_transformer
    base = entry.selection.descriptor

    operation = transformer
    try:
        operation = transformer.get_last_used_operation()
    except Exception:
        operation = transformer

    operation_name = _operation_name(operation)
    pipeline = _operation_pipeline(operation)
    reported_accuracy = _reported_accuracy(operation)

    if (
        request.policy.require_known_accuracy
        and reported_accuracy is None
    ):
        raise TransformationAccuracyUnknownError.for_operation(
            source_crs=request.source_crs.crs_id,
            target_crs=request.target_crs.crs_id,
            required_accuracy=(
                float(request.policy.maximum_accuracy_m)
                if request.policy.maximum_accuracy_m is not None
                else None
            ),
        )

    if (
        request.policy.maximum_accuracy_m is not None
        and reported_accuracy is not None
        and reported_accuracy
        > request.policy.maximum_accuracy_m
    ):
        raise TransformationPrecisionExceededError.for_error(
            measured_error=float(reported_accuracy),
            allowed_error=float(
                request.policy.maximum_accuracy_m
            ),
            unit="metre",
            operation="last-used-operation",
        )

    return TransformerDescriptor(
        cache_key=base.cache_key,
        source_crs_id=base.source_crs_id,
        target_crs_id=base.target_crs_id,
        operation_name=operation_name,
        pipeline=pipeline,
        reported_accuracy_m=reported_accuracy,
        best_available=base.best_available,
        ballpark=_transformer_uses_ballpark(operation),
        has_inverse=bool(
            getattr(operation, "has_inverse", base.has_inverse)
        ),
        network_enabled=bool(
            getattr(
                operation,
                "is_network_enabled",
                base.network_enabled,
            )
        ),
        available_transformer_count=(
            base.available_transformer_count
        ),
        unavailable_operation_count=(
            base.unavailable_operation_count
        ),
        required_grids=_collect_grids(
            getattr(operation, "operations", ()) or (),
            only_missing=False,
        )
        or base.required_grids,
        missing_grids=base.missing_grids,
        selection_warnings=base.selection_warnings,
    )


def _contract_resolution_policy(
    contract: CrsDefinition,
    *,
    role: str,
) -> CrsResolutionPolicy:
    return CrsResolutionPolicy(
        role=role,
        allowed_dimensions=(contract.coordinate_dimension,),
        allowed_crs_ids=(contract.crs_id,),
        allow_geographic=contract.is_geographic,
        allow_projected=contract.is_projected,
        allow_geocentric=contract.is_geocentric,
        allow_compound=contract.is_compound,
        allow_vertical_only=contract.is_vertical,
        allow_engineering=False,
        allow_bound=True,
        allow_deprecated=False,
        require_authority_match=bool(
            contract.authority and contract.code
        ),
        minimum_authority_confidence=100,
    )


def _selection_cache_key(
    *,
    source_crs: CrsDefinition,
    target_crs: CrsDefinition,
    policy: TransformationPolicy,
    options: TransformerSelectionOptions,
) -> str:
    payload = {
        "sourceDefinitionFingerprint": (
            source_crs.definition_fingerprint
        ),
        "targetDefinitionFingerprint": (
            target_crs.definition_fingerprint
        ),
        "sourceCrsId": source_crs.crs_id,
        "targetCrsId": target_crs.crs_id,
        "policy": policy.to_dict(),
        "options": options.to_dict(),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _get_thread_state() -> _ThreadTransformerState:
    with _CACHE_LOCK:
        generation = _CACHE_GENERATION

    state = getattr(_THREAD_STATE, "transformers", None)
    if not isinstance(state, _ThreadTransformerState):
        state = _ThreadTransformerState(
            generation=generation,
            entries=OrderedDict(),
        )
        _THREAD_STATE.transformers = state
        return state

    if state.generation != generation:
        state.entries.clear()
        state.generation = generation

    return state


def _assert_thread_ownership(entry: _ThreadTransformerEntry) -> None:
    current_thread = get_ident()
    if entry.thread_id != current_thread:
        raise GeoreferencingConfigurationError(
            "Nativer Transformer darf nicht threadübergreifend verwendet werden.",
            details={
                "createdThreadId": entry.thread_id,
                "currentThreadId": current_thread,
            },
        )


def _import_pyproj_transformer_module() -> Any:
    try:
        return import_module("pyproj.transformer")
    except Exception as error:
        raise GeoreferencingConfigurationError(
            "pyproj.transformer konnte nicht geladen werden.",
            details={"causeType": type(error).__name__},
            cause=error,
        ) from error


def _build_native_area_of_interest(
    bounds: AreaOfInterestBounds | None,
    *,
    pyproj_transformer: Any,
) -> Any:
    if bounds is None:
        return None
    try:
        return pyproj_transformer.AreaOfInterest(
            west_lon_degree=bounds.west_longitude_deg,
            south_lat_degree=bounds.south_latitude_deg,
            east_lon_degree=bounds.east_longitude_deg,
            north_lat_degree=bounds.north_latitude_deg,
        )
    except Exception as error:
        raise GeoreferencingValidationError(
            "AreaOfInterest konnte nicht erzeugt werden.",
            details={
                "bounds": bounds.to_dict(),
                "causeType": type(error).__name__,
            },
            cause=error,
        ) from error


def _collect_grids(
    operations: Iterable[Any],
    *,
    only_missing: bool,
) -> tuple[GridRequirement, ...]:
    result: list[GridRequirement] = []
    seen: set[tuple[str, bool]] = set()

    for operation in operations:
        try:
            grids = tuple(getattr(operation, "grids", ()) or ())
        except Exception:
            continue

        for grid in grids:
            available = bool(getattr(grid, "available", False))
            if only_missing and available:
                continue

            short_name = (
                str(getattr(grid, "short_name", "") or "").strip()
                or str(getattr(grid, "full_name", "") or "").strip()
                or "unknown-grid"
            )
            key = (short_name, available)
            if key in seen:
                continue
            seen.add(key)

            url = str(getattr(grid, "url", "") or "").strip()
            requirement = GridRequirement(
                short_name=short_name,
                available=available,
                package_name=(
                    str(getattr(grid, "package_name", "") or "").strip()
                    or None
                ),
                url_fingerprint=(
                    sha256(url.encode("utf-8")).hexdigest()[:16]
                    if url
                    else None
                ),
                direct_download=getattr(
                    grid,
                    "direct_download",
                    None,
                ),
                open_license=getattr(
                    grid,
                    "open_license",
                    None,
                ),
            )
            result.append(requirement)

            if len(result) > _MAX_GRID_COUNT:
                raise GeoreferencingConfigurationError(
                    "Transformationsoperation referenziert zu viele Grids.",
                    details={
                        "maximumGridCount": _MAX_GRID_COUNT,
                    },
                )

    return tuple(result)


def _transformer_uses_ballpark(transformer: Any) -> bool:
    try:
        operations = tuple(
            getattr(transformer, "operations", ()) or ()
        )
    except Exception:
        operations = ()

    for operation in operations:
        if bool(
            getattr(
                operation,
                "has_ballpark_transformation",
                False,
            )
        ):
            return True

    description = str(
        getattr(transformer, "description", "") or ""
    ).lower()
    return "ballpark" in description


def _reported_accuracy(transformer: Any) -> Decimal | None:
    value = getattr(transformer, "accuracy", None)
    if value is None:
        return None
    number = _require_finite_float(
        value,
        field_name="transformerAccuracy",
    )
    if number < 0:
        return None
    return Decimal(str(number))


def _operation_name(transformer: Any) -> str:
    candidates = (
        getattr(transformer, "description", None),
        getattr(transformer, "name", None),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:_MAX_OPERATION_NAME_LENGTH]
    return "unnamed-coordinate-operation"


def _operation_pipeline(transformer: Any) -> str | None:
    value = getattr(transformer, "definition", None)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or normalized == "unavailable until proj_trans is called":
        return None
    if len(normalized) > _MAX_PIPELINE_LENGTH:
        raise GeoreferencingConfigurationError(
            "Transformationspipeline überschreitet die maximale Länge.",
            details={
                "length": len(normalized),
                "maximumLength": _MAX_PIPELINE_LENGTH,
            },
        )
    return normalized


def _summarize_warnings(
    records: Sequence[Any],
) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()

    for record in records:
        category = getattr(
            getattr(record, "category", None),
            "__name__",
            "Warning",
        )
        text = str(getattr(record, "message", "") or "").strip()
        summary = f"{category}: {text}"[:_MAX_WARNING_LENGTH]
        if not summary or summary in seen:
            continue
        seen.add(summary)
        result.append(summary)

    return tuple(result)


def _record_transformation_success() -> None:
    global _TRANSFORMATIONS_EXECUTED
    with _CACHE_LOCK:
        _TRANSFORMATIONS_EXECUTED += 1


def _record_transformation_failure() -> None:
    global _TRANSFORMATIONS_FAILED
    with _CACHE_LOCK:
        _TRANSFORMATIONS_FAILED += 1


def _normalize_optional_authority(
    value: Any,
) -> str | None:
    if value is None:
        return None
    normalized = _normalize_text(
        value,
        field_name="authority",
        maximum_length=_MAX_AUTHORITY_LENGTH,
    )
    return normalized


def _normalize_hash(
    value: Any,
    *,
    field_name: str,
) -> str:
    normalized = _normalize_text(
        value,
        field_name=field_name,
        maximum_length=128,
    )
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef"
        for character in normalized.lower()
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss ein SHA-256-Hash sein.",
            details={"length": len(normalized)},
        )
    return normalized.lower()


def _normalize_text(
    value: Any,
    *,
    field_name: str,
    maximum_length: int,
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


def _normalize_optional_text(
    value: Any,
    *,
    field_name: str,
    maximum_length: int,
) -> str | None:
    if value is None:
        return None
    return _normalize_text(
        value,
        field_name=field_name,
        maximum_length=maximum_length,
    )


def _normalize_non_negative_decimal(
    value: Any,
    *,
    field_name: str,
) -> Decimal:
    try:
        normalized = (
            value
            if isinstance(value, Decimal)
            else Decimal(str(value))
        )
    except Exception as error:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Dezimalzahl sein.",
            details={"actualType": type(value).__name__},
            cause=error,
        ) from error

    if not normalized.is_finite() or normalized < 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss endlich und nicht-negativ sein.",
            details={"value": str(normalized)},
        )
    return normalized


def _normalize_non_negative_int(
    value: Any,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine ganze Zahl sein.",
            details={"actualType": type(value).__name__},
        )
    if value < 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' darf nicht negativ sein.",
            details={"value": value},
        )
    return value


def _normalize_grid_tuple(
    values: Sequence[GridRequirement],
    *,
    field_name: str,
) -> tuple[GridRequirement, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Sequence,
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Sequenz sein.",
            details={"actualType": type(values).__name__},
        )

    result: list[GridRequirement] = []
    seen: set[tuple[str, bool]] = set()
    for value in values:
        if not isinstance(value, GridRequirement):
            raise GeoreferencingValidationError(
                f"'{field_name}' enthält einen ungültigen Wert.",
                details={"actualType": type(value).__name__},
            )
        key = (value.short_name, value.available)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _normalize_warning_tuple(
    values: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Sequence,
    ):
        raise GeoreferencingValidationError(
            "selection_warnings muss eine Sequenz sein.",
            details={"actualType": type(values).__name__},
        )

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_text(
            value,
            field_name="selectionWarning",
            maximum_length=_MAX_WARNING_LENGTH,
        )
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _require_finite_float(
    value: Any,
    *,
    field_name: str,
) -> float:
    if isinstance(value, bool) or not isinstance(
        value,
        (int, float, Decimal),
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Zahl sein.",
            details={"actualType": type(value).__name__},
        )
    normalized = float(value)
    if not isfinite(normalized):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss endlich sein.",
            details={"value": str(value)},
        )
    return normalized


def _safe_error(error: BaseException) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "type": type(error).__name__,
        "message": str(error).strip() or "Transformation fehlgeschlagen.",
    }
    code = getattr(error, "code", None)
    if code is not None:
        payload["code"] = str(code)
    return payload


__all__ = [
    "AreaOfInterestBounds",
    "GridRequirement",
    "TransformerDescriptor",
    "TransformerSelection",
    "TransformerSelectionOptions",
    "clear_transformer_caches",
    "select_transformer",
    "transform_coordinate",
    "transform_coordinate_batch",
    "transformer_cache_info",
    "transformer_runtime_status",
]
