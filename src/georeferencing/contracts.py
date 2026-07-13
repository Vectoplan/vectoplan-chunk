# services/vectoplan-chunk/src/georeferencing/contracts.py
"""Unveränderliche Verträge für CRS, Earth-Referenzen und Transformationen.

Dieses Modul enthält ausschließlich frameworkunabhängige Domain-Verträge.
Es importiert weder ``pyproj`` noch Flask, SQLAlchemy oder Datenbankcode.

Die Verträge bilden die stabile Grenze zwischen:

* HTTP-/Provisioning-Eingaben;
* CRS-Auflösung und CRS-Validierung;
* Transformationsauswahl;
* Earth-Grid-Abbildung;
* Persistenz des genau einen globalen Referenzpunkts;
* Runtime-Caching eines daraus abgeleiteten Earth-Ankers.

Wichtige Prinzipien
-------------------
* Eine Earth-World speichert genau einen ``GlobalReferencePoint``.
* Koordinatenwerte werden intern als ``Decimal`` normalisiert.
* Rohdefinitionen eines CRS werden in normalen API-Darstellungen nicht
  vollständig ausgegeben.
* Grid-Identität und Projektion sind versioniert.
* Transformationsrichtlinien verbieten standardmäßig Ballpark-Fallbacks.
* Ergebnisverträge validieren die zugesicherte Genauigkeit.
* Alle Dataclasses sind ``frozen`` und ``slots``-basiert.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import IntEnum, StrEnum
from hashlib import sha256
import json
from math import isfinite
import re
from typing import Any, ClassVar, Final, Self

from ..coordinates.models import AxisConvention, JsonValue
from .errors import (
    BallparkTransformationForbiddenError,
    CrsAxisOrderInvalidError,
    CrsDimensionMismatchError,
    CrsInvalidError,
    EarthReferenceInvalidError,
    GeoreferencingValidationError,
    TransformationAccuracyUnknownError,
    TransformationNotExactError,
    TransformationPrecisionExceededError,
    TransformationRoundtripFailedError,
    summarize_crs_input,
)


_MAX_CRS_DEFINITION_LENGTH: Final[int] = 1_048_576
_MAX_IDENTIFIER_LENGTH: Final[int] = 256
_MAX_NAME_LENGTH: Final[int] = 512
_MAX_DECIMAL_TEXT_LENGTH: Final[int] = 128
_MAX_DECIMAL_DIGITS: Final[int] = 80
_MAX_DECIMAL_ADJUSTED_EXPONENT: Final[int] = 1_000
_MAX_PIPELINE_LENGTH: Final[int] = 262_144
_MAX_GRID_NAMES: Final[int] = 128
_MAX_AXIS_COUNT: Final[int] = 8

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$"
)


class CoordinateDimension(IntEnum):
    """Unterstützte Dimensionen globaler Referenzkoordinaten."""

    TWO_D = 2
    THREE_D = 3


class CrsDefinitionFormat(StrEnum):
    """Kanonisches Speicherformat einer validierten CRS-Definition."""

    AUTHORITY_CODE = "authority-code"
    WKT = "wkt"
    PROJJSON = "projjson"
    PROJ_STRING = "proj-string"


class TransformationOperationKind(StrEnum):
    """Fachliche Rolle einer Koordinatentransformation."""

    REFERENCE_TO_CANONICAL = "reference-to-canonical"
    CANONICAL_TO_REFERENCE = "canonical-to-reference"
    LOCAL_TO_GLOBAL = "local-to-global"
    GLOBAL_TO_LOCAL = "global-to-local"
    ROUNDTRIP_VALIDATION = "roundtrip-validation"
    IMPORT_TO_CANONICAL = "import-to-canonical"
    CANONICAL_TO_EXPORT = "canonical-to-export"


@dataclass(frozen=True, slots=True)
class GlobalCoordinate:
    """Globales 2D- oder 3D-Koordinatentupel mit dezimaler Präzision."""

    x: Decimal
    y: Decimal
    z: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "x",
            _normalize_decimal(self.x, field_name="x"),
        )
        object.__setattr__(
            self,
            "y",
            _normalize_decimal(self.y, field_name="y"),
        )
        object.__setattr__(
            self,
            "z",
            (
                _normalize_decimal(self.z, field_name="z")
                if self.z is not None
                else None
            ),
        )

    @classmethod
    def from_values(
        cls,
        x: Any,
        y: Any,
        z: Any = None,
    ) -> Self:
        return cls(x=x, y=y, z=z)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        mapping = _require_mapping(
            payload,
            object_name="GlobalCoordinate",
        )

        if "x" not in mapping or "y" not in mapping:
            raise EarthReferenceInvalidError.for_reason(
                "GlobalCoordinate benötigt mindestens x und y.",
                coordinate_dimensions=(
                    1 if "x" in mapping or "y" in mapping else 0
                ),
            )

        return cls(
            x=mapping["x"],
            y=mapping["y"],
            z=mapping.get("z"),
        )

    @property
    def dimension(self) -> CoordinateDimension:
        return (
            CoordinateDimension.THREE_D
            if self.z is not None
            else CoordinateDimension.TWO_D
        )

    def as_decimal_tuple(
        self,
    ) -> tuple[Decimal, Decimal] | tuple[Decimal, Decimal, Decimal]:
        if self.z is None:
            return self.x, self.y
        return self.x, self.y, self.z

    def as_float_tuple(
        self,
    ) -> tuple[float, float] | tuple[float, float, float]:
        values = tuple(float(value) for value in self.as_decimal_tuple())
        for index, value in enumerate(values):
            if not isfinite(value):
                raise EarthReferenceInvalidError.for_reason(
                    f"Koordinatenwert {index} kann nicht endlich "
                    "als float dargestellt werden.",
                    coordinate_dimensions=int(self.dimension),
                )
        return values

    def to_dict(
        self,
        *,
        numeric: bool = False,
    ) -> dict[str, JsonValue]:
        if numeric:
            payload: dict[str, JsonValue] = {
                "x": float(self.x),
                "y": float(self.y),
            }
            if self.z is not None:
                payload["z"] = float(self.z)
        else:
            payload = {
                "x": decimal_to_canonical_string(self.x),
                "y": decimal_to_canonical_string(self.y),
            }
            if self.z is not None:
                payload["z"] = decimal_to_canonical_string(self.z)

        payload["dimension"] = int(self.dimension)
        return payload

    def fingerprint_payload(self) -> dict[str, JsonValue]:
        return self.to_dict(numeric=False)


@dataclass(frozen=True, slots=True)
class CrsDefinition:
    """Kanonisch aufgelöste und validierte CRS-Identität.

    ``definition`` enthält die vollständige kanonische Definition für
    Persistenz und reproduzierbare Rekonstruktion. Normale ``to_dict``-
    Aufrufe geben diese Definition nicht vollständig aus.
    """

    crs_id: str
    definition_format: CrsDefinitionFormat
    definition: str
    coordinate_dimension: CoordinateDimension
    authority: str | None = None
    code: str | None = None
    name: str | None = None
    axis_names: tuple[str, ...] = ()
    unit_names: tuple[str, ...] = ()
    is_geographic: bool = False
    is_projected: bool = False
    is_geocentric: bool = False
    is_vertical: bool = False
    is_compound: bool = False

    def __post_init__(self) -> None:
        normalized_id = _normalize_identifier(
            self.crs_id,
            field_name="crsId",
        )
        normalized_format = _normalize_enum(
            self.definition_format,
            CrsDefinitionFormat,
            field_name="definitionFormat",
        )
        normalized_definition = _normalize_crs_definition(
            self.definition
        )
        normalized_dimension = _normalize_enum(
            self.coordinate_dimension,
            CoordinateDimension,
            field_name="coordinateDimension",
        )

        normalized_authority = _normalize_optional_identifier(
            self.authority,
            field_name="authority",
        )
        normalized_code = _normalize_optional_identifier(
            self.code,
            field_name="code",
        )
        normalized_name = _normalize_optional_text(
            self.name,
            field_name="name",
            maximum_length=_MAX_NAME_LENGTH,
        )
        normalized_axes = _normalize_text_tuple(
            self.axis_names,
            field_name="axisNames",
            maximum_items=_MAX_AXIS_COUNT,
            deduplicate=False,
        )
        normalized_units = _normalize_text_tuple(
            self.unit_names,
            field_name="unitNames",
            maximum_items=_MAX_AXIS_COUNT,
            deduplicate=False,
        )

        if normalized_axes and len(normalized_axes) != int(
            normalized_dimension
        ):
            raise CrsAxisOrderInvalidError.for_axes(
                crs=normalized_id,
                detected_axes=normalized_axes,
                required_convention=(
                    f"{int(normalized_dimension)} explicitly ordered axes"
                ),
            )

        if normalized_units and len(normalized_units) not in {
            1,
            int(normalized_dimension),
        }:
            raise CrsInvalidError.for_value(
                normalized_id,
                role="reference",
                reason=(
                    "unitNames muss genau eine gemeinsame Einheit oder "
                    "eine Einheit je Koordinatenachse enthalten."
                ),
            )

        classification_count = sum(
            bool(value)
            for value in (
                self.is_geographic,
                self.is_projected,
                self.is_geocentric,
            )
        )
        if classification_count > 1 and not self.is_compound:
            raise CrsInvalidError.for_value(
                normalized_id,
                role="reference",
                reason=(
                    "Ein nicht zusammengesetztes CRS darf nicht gleichzeitig "
                    "geografisch, projiziert und geozentrisch sein."
                ),
            )

        if self.is_geocentric and (
            normalized_dimension is not CoordinateDimension.THREE_D
        ):
            raise CrsDimensionMismatchError.for_dimensions(
                crs=normalized_id,
                expected_dimensions=3,
                actual_dimensions=int(normalized_dimension),
            )

        if normalized_format is CrsDefinitionFormat.AUTHORITY_CODE:
            if normalized_authority is None or normalized_code is None:
                raise CrsInvalidError.for_value(
                    normalized_definition,
                    role="reference",
                    reason=(
                        "Authority-Code-CRS benötigt authority und code."
                    ),
                )

        object.__setattr__(self, "crs_id", normalized_id)
        object.__setattr__(self, "definition_format", normalized_format)
        object.__setattr__(self, "definition", normalized_definition)
        object.__setattr__(
            self,
            "coordinate_dimension",
            normalized_dimension,
        )
        object.__setattr__(self, "authority", normalized_authority)
        object.__setattr__(self, "code", normalized_code)
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "axis_names", normalized_axes)
        object.__setattr__(self, "unit_names", normalized_units)
        object.__setattr__(
            self,
            "is_geographic",
            bool(self.is_geographic),
        )
        object.__setattr__(
            self,
            "is_projected",
            bool(self.is_projected),
        )
        object.__setattr__(
            self,
            "is_geocentric",
            bool(self.is_geocentric),
        )
        object.__setattr__(
            self,
            "is_vertical",
            bool(self.is_vertical),
        )
        object.__setattr__(
            self,
            "is_compound",
            bool(self.is_compound),
        )

    @classmethod
    def from_authority(
        cls,
        *,
        authority: str,
        code: str | int,
        coordinate_dimension: CoordinateDimension | int,
        name: str | None = None,
        axis_names: Sequence[str] = (),
        unit_names: Sequence[str] = (),
        is_geographic: bool = False,
        is_projected: bool = False,
        is_geocentric: bool = False,
        is_vertical: bool = False,
        is_compound: bool = False,
    ) -> Self:
        normalized_authority = _normalize_identifier(
            authority,
            field_name="authority",
        )
        normalized_code = _normalize_identifier(
            str(code),
            field_name="code",
        )
        crs_id = f"{normalized_authority}:{normalized_code}"

        return cls(
            crs_id=crs_id,
            definition_format=CrsDefinitionFormat.AUTHORITY_CODE,
            definition=crs_id,
            coordinate_dimension=coordinate_dimension,
            authority=normalized_authority,
            code=normalized_code,
            name=name,
            axis_names=tuple(axis_names),
            unit_names=tuple(unit_names),
            is_geographic=is_geographic,
            is_projected=is_projected,
            is_geocentric=is_geocentric,
            is_vertical=is_vertical,
            is_compound=is_compound,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        mapping = _require_mapping(
            payload,
            object_name="CrsDefinition",
        )

        required = (
            "crsId",
            "definitionFormat",
            "definition",
            "coordinateDimension",
        )
        missing = [field for field in required if field not in mapping]
        if missing:
            raise CrsInvalidError.for_value(
                mapping,
                role="reference",
                reason=f"Pflichtfelder fehlen: {', '.join(missing)}",
            )

        return cls(
            crs_id=mapping["crsId"],
            definition_format=mapping["definitionFormat"],
            definition=mapping["definition"],
            coordinate_dimension=mapping["coordinateDimension"],
            authority=mapping.get("authority"),
            code=mapping.get("code"),
            name=mapping.get("name"),
            axis_names=tuple(mapping.get("axisNames") or ()),
            unit_names=tuple(mapping.get("unitNames") or ()),
            is_geographic=bool(mapping.get("isGeographic", False)),
            is_projected=bool(mapping.get("isProjected", False)),
            is_geocentric=bool(mapping.get("isGeocentric", False)),
            is_vertical=bool(mapping.get("isVertical", False)),
            is_compound=bool(mapping.get("isCompound", False)),
        )

    @property
    def definition_fingerprint(self) -> str:
        return sha256(self.definition.encode("utf-8")).hexdigest()

    def to_dict(
        self,
        *,
        include_definition: bool = False,
    ) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "crsId": self.crs_id,
            "definitionFormat": self.definition_format.value,
            "definitionLength": len(self.definition),
            "definitionFingerprint": self.definition_fingerprint,
            "coordinateDimension": int(self.coordinate_dimension),
            "authority": self.authority,
            "code": self.code,
            "name": self.name,
            "axisNames": list(self.axis_names),
            "unitNames": list(self.unit_names),
            "isGeographic": self.is_geographic,
            "isProjected": self.is_projected,
            "isGeocentric": self.is_geocentric,
            "isVertical": self.is_vertical,
            "isCompound": self.is_compound,
        }

        if include_definition:
            payload["definition"] = self.definition

        return payload

    def to_persistence_dict(self) -> dict[str, JsonValue]:
        return self.to_dict(include_definition=True)

    def semantically_matches(self, other: "CrsDefinition") -> bool:
        if not isinstance(other, CrsDefinition):
            return False

        return (
            self.crs_id == other.crs_id
            and self.definition_fingerprint
            == other.definition_fingerprint
            and self.coordinate_dimension
            == other.coordinate_dimension
        )


@dataclass(frozen=True, slots=True)
class EarthGridReference:
    """Versionierte Identität des global einheitlichen Earth-Rasters."""

    grid_id: str
    grid_version: str
    projection_id: str
    projection_version: str
    topology_type: str
    axis_convention: AxisConvention = (
        AxisConvention.X_EAST_Y_UP_Z_NORTH
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "grid_id",
            _normalize_identifier(
                self.grid_id,
                field_name="gridId",
            ),
        )
        object.__setattr__(
            self,
            "grid_version",
            _normalize_identifier(
                self.grid_version,
                field_name="gridVersion",
            ),
        )
        object.__setattr__(
            self,
            "projection_id",
            _normalize_identifier(
                self.projection_id,
                field_name="projectionId",
            ),
        )
        object.__setattr__(
            self,
            "projection_version",
            _normalize_identifier(
                self.projection_version,
                field_name="projectionVersion",
            ),
        )
        object.__setattr__(
            self,
            "topology_type",
            _normalize_identifier(
                self.topology_type,
                field_name="topologyType",
            ),
        )

        normalized_axis = _normalize_enum(
            self.axis_convention,
            AxisConvention,
            field_name="axisConvention",
        )
        if (
            normalized_axis
            is not AxisConvention.X_EAST_Y_UP_Z_NORTH
        ):
            raise EarthReferenceInvalidError.for_reason(
                "Earth v1 unterstützt ausschließlich "
                "x-east-y-up-z-north."
            )

        object.__setattr__(
            self,
            "axis_convention",
            normalized_axis,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        mapping = _require_mapping(
            payload,
            object_name="EarthGridReference",
        )
        required = (
            "gridId",
            "gridVersion",
            "projectionId",
            "projectionVersion",
            "topologyType",
        )
        missing = [field for field in required if field not in mapping]
        if missing:
            raise EarthReferenceInvalidError.for_reason(
                f"EarthGridReference-Pflichtfelder fehlen: "
                f"{', '.join(missing)}."
            )

        return cls(
            grid_id=mapping["gridId"],
            grid_version=mapping["gridVersion"],
            projection_id=mapping["projectionId"],
            projection_version=mapping["projectionVersion"],
            topology_type=mapping["topologyType"],
            axis_convention=mapping.get(
                "axisConvention",
                AxisConvention.X_EAST_Y_UP_Z_NORTH,
            ),
        )

    @property
    def key(self) -> str:
        return f"{self.grid_id}@{self.grid_version}"

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "gridId": self.grid_id,
            "gridVersion": self.grid_version,
            "gridKey": self.key,
            "projectionId": self.projection_id,
            "projectionVersion": self.projection_version,
            "topologyType": self.topology_type,
            "axisConvention": self.axis_convention.value,
        }


@dataclass(frozen=True, slots=True)
class GlobalReferencePoint:
    """Der eine persistierte globale Referenzpunkt einer Earth-World."""

    coordinate: GlobalCoordinate
    crs: CrsDefinition
    grid: EarthGridReference
    reference_version: int = 1
    source: str | None = None
    source_reference_id: str | None = None

    schema_version: ClassVar[str] = "earth-global-reference.schema.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate, GlobalCoordinate):
            raise EarthReferenceInvalidError.for_reason(
                "coordinate muss ein GlobalCoordinate sein."
            )
        if not isinstance(self.crs, CrsDefinition):
            raise EarthReferenceInvalidError.for_reason(
                "crs muss eine CrsDefinition sein."
            )
        if not isinstance(self.grid, EarthGridReference):
            raise EarthReferenceInvalidError.for_reason(
                "grid muss eine EarthGridReference sein."
            )

        normalized_version = _normalize_positive_int(
            self.reference_version,
            field_name="referenceVersion",
        )

        if int(self.coordinate.dimension) > int(
            self.crs.coordinate_dimension
        ):
            raise CrsDimensionMismatchError.for_dimensions(
                crs=self.crs.crs_id,
                expected_dimensions=int(
                    self.crs.coordinate_dimension
                ),
                actual_dimensions=int(self.coordinate.dimension),
            )

        if (
            self.crs.is_geocentric
            and self.coordinate.dimension
            is not CoordinateDimension.THREE_D
        ):
            raise CrsDimensionMismatchError.for_dimensions(
                crs=self.crs.crs_id,
                expected_dimensions=3,
                actual_dimensions=int(self.coordinate.dimension),
            )

        object.__setattr__(
            self,
            "reference_version",
            normalized_version,
        )
        object.__setattr__(
            self,
            "source",
            _normalize_optional_text(
                self.source,
                field_name="source",
                maximum_length=_MAX_NAME_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "source_reference_id",
            _normalize_optional_identifier(
                self.source_reference_id,
                field_name="sourceReferenceId",
            ),
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        mapping = _require_mapping(
            payload,
            object_name="GlobalReferencePoint",
        )

        for field_name in ("coordinate", "crs", "grid"):
            if field_name not in mapping:
                raise EarthReferenceInvalidError.for_reason(
                    f"GlobalReferencePoint-Pflichtfeld "
                    f"'{field_name}' fehlt."
                )

        coordinate_payload = _require_mapping(
            mapping["coordinate"],
            object_name="coordinate",
        )
        crs_payload = _require_mapping(
            mapping["crs"],
            object_name="crs",
        )
        grid_payload = _require_mapping(
            mapping["grid"],
            object_name="grid",
        )

        return cls(
            coordinate=GlobalCoordinate.from_mapping(
                coordinate_payload
            ),
            crs=CrsDefinition.from_mapping(crs_payload),
            grid=EarthGridReference.from_mapping(grid_payload),
            reference_version=mapping.get("referenceVersion", 1),
            source=mapping.get("source"),
            source_reference_id=mapping.get("sourceReferenceId"),
        )

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.fingerprint_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def fingerprint_payload(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "referenceVersion": self.reference_version,
            "coordinate": self.coordinate.fingerprint_payload(),
            "crsId": self.crs.crs_id,
            "crsDefinitionFingerprint": (
                self.crs.definition_fingerprint
            ),
            "grid": self.grid.to_dict(),
            "source": self.source,
            "sourceReferenceId": self.source_reference_id,
        }

    def to_dict(
        self,
        *,
        include_crs_definition: bool = False,
        numeric_coordinates: bool = False,
    ) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "referenceVersion": self.reference_version,
            "fingerprint": self.fingerprint,
            "coordinate": self.coordinate.to_dict(
                numeric=numeric_coordinates
            ),
            "crs": self.crs.to_dict(
                include_definition=include_crs_definition
            ),
            "grid": self.grid.to_dict(),
            "source": self.source,
            "sourceReferenceId": self.source_reference_id,
        }

    def to_persistence_dict(self) -> dict[str, JsonValue]:
        return self.to_dict(
            include_crs_definition=True,
            numeric_coordinates=False,
        )


@dataclass(frozen=True, slots=True)
class TransformationPolicy:
    """Genauigkeits- und Sicherheitsvertrag einer Transformation."""

    allow_ballpark: bool = False
    require_best_available: bool = True
    require_known_accuracy: bool = False
    maximum_accuracy_m: Decimal | None = None
    validate_roundtrip: bool = True
    maximum_roundtrip_error_m: Decimal | None = Decimal("0.001")
    always_xy: bool = True

    schema_version: ClassVar[str] = "transform-policy.schema.v1"

    def __post_init__(self) -> None:
        maximum_accuracy = (
            _normalize_non_negative_decimal(
                self.maximum_accuracy_m,
                field_name="maximumAccuracyM",
            )
            if self.maximum_accuracy_m is not None
            else None
        )
        maximum_roundtrip = (
            _normalize_non_negative_decimal(
                self.maximum_roundtrip_error_m,
                field_name="maximumRoundtripErrorM",
            )
            if self.maximum_roundtrip_error_m is not None
            else None
        )

        if self.validate_roundtrip and maximum_roundtrip is None:
            raise GeoreferencingValidationError(
                "Aktivierte Roundtrip-Prüfung benötigt "
                "maximumRoundtripErrorM."
            )

        if not self.always_xy:
            raise GeoreferencingValidationError(
                "Earth v1 verlangt always_xy=True."
            )

        object.__setattr__(
            self,
            "allow_ballpark",
            bool(self.allow_ballpark),
        )
        object.__setattr__(
            self,
            "require_best_available",
            bool(self.require_best_available),
        )
        object.__setattr__(
            self,
            "require_known_accuracy",
            bool(self.require_known_accuracy),
        )
        object.__setattr__(
            self,
            "maximum_accuracy_m",
            maximum_accuracy,
        )
        object.__setattr__(
            self,
            "validate_roundtrip",
            bool(self.validate_roundtrip),
        )
        object.__setattr__(
            self,
            "maximum_roundtrip_error_m",
            maximum_roundtrip,
        )
        object.__setattr__(self, "always_xy", True)

    @classmethod
    def strict_default(cls) -> Self:
        return cls()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        mapping = _require_mapping(
            payload,
            object_name="TransformationPolicy",
        )
        return cls(
            allow_ballpark=bool(
                mapping.get("allowBallpark", False)
            ),
            require_best_available=bool(
                mapping.get("requireBestAvailable", True)
            ),
            require_known_accuracy=bool(
                mapping.get("requireKnownAccuracy", False)
            ),
            maximum_accuracy_m=mapping.get("maximumAccuracyM"),
            validate_roundtrip=bool(
                mapping.get("validateRoundtrip", True)
            ),
            maximum_roundtrip_error_m=mapping.get(
                "maximumRoundtripErrorM",
                Decimal("0.001"),
            ),
            always_xy=bool(mapping.get("alwaysXy", True)),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "allowBallpark": self.allow_ballpark,
            "requireBestAvailable": self.require_best_available,
            "requireKnownAccuracy": self.require_known_accuracy,
            "maximumAccuracyM": (
                decimal_to_canonical_string(
                    self.maximum_accuracy_m
                )
                if self.maximum_accuracy_m is not None
                else None
            ),
            "validateRoundtrip": self.validate_roundtrip,
            "maximumRoundtripErrorM": (
                decimal_to_canonical_string(
                    self.maximum_roundtrip_error_m
                )
                if self.maximum_roundtrip_error_m is not None
                else None
            ),
            "alwaysXy": self.always_xy,
        }


@dataclass(frozen=True, slots=True)
class TransformationAccuracy:
    """Beobachtete Eigenschaften der ausgewählten Transformation."""

    best_available: bool
    ballpark: bool
    reported_accuracy_m: Decimal | None = None
    measured_roundtrip_error_m: Decimal | None = None
    required_grids: tuple[str, ...] = ()
    missing_grids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "best_available",
            bool(self.best_available),
        )
        object.__setattr__(
            self,
            "ballpark",
            bool(self.ballpark),
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
            "measured_roundtrip_error_m",
            (
                _normalize_non_negative_decimal(
                    self.measured_roundtrip_error_m,
                    field_name="measuredRoundtripErrorM",
                )
                if self.measured_roundtrip_error_m is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "required_grids",
            _normalize_text_tuple(
                self.required_grids,
                field_name="requiredGrids",
                maximum_items=_MAX_GRID_NAMES,
            ),
        )
        object.__setattr__(
            self,
            "missing_grids",
            _normalize_text_tuple(
                self.missing_grids,
                field_name="missingGrids",
                maximum_items=_MAX_GRID_NAMES,
            ),
        )

    @property
    def grids_available(self) -> bool:
        return not self.missing_grids

    def validate_against(
        self,
        policy: TransformationPolicy,
        *,
        source_crs: CrsDefinition,
        target_crs: CrsDefinition,
    ) -> None:
        """Prüft die beobachtete Transformation gegen ihre Policy."""

        if not isinstance(policy, TransformationPolicy):
            raise GeoreferencingValidationError(
                "policy muss eine TransformationPolicy sein."
            )

        if self.ballpark and not policy.allow_ballpark:
            raise BallparkTransformationForbiddenError.for_pair(
                source_crs=source_crs.crs_id,
                target_crs=target_crs.crs_id,
            )

        if policy.require_best_available and not self.best_available:
            raise TransformationNotExactError.for_operation(
                source_crs=source_crs.crs_id,
                target_crs=target_crs.crs_id,
                reason=(
                    "Die ausgewählte Transformation ist nicht als "
                    "bestmögliche lokale Operation verfügbar."
                ),
            )

        if policy.require_known_accuracy and (
            self.reported_accuracy_m is None
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
            and self.reported_accuracy_m is not None
            and self.reported_accuracy_m
            > policy.maximum_accuracy_m
        ):
            raise TransformationPrecisionExceededError.for_error(
                measured_error=float(self.reported_accuracy_m),
                allowed_error=float(policy.maximum_accuracy_m),
                unit="metre",
                operation="reported-transform-accuracy",
            )

        if policy.validate_roundtrip:
            if self.measured_roundtrip_error_m is None:
                raise TransformationAccuracyUnknownError.for_operation(
                    source_crs=source_crs.crs_id,
                    target_crs=target_crs.crs_id,
                    required_accuracy=(
                        float(policy.maximum_roundtrip_error_m)
                        if policy.maximum_roundtrip_error_m
                        is not None
                        else None
                    ),
                )

            assert policy.maximum_roundtrip_error_m is not None
            if (
                self.measured_roundtrip_error_m
                > policy.maximum_roundtrip_error_m
            ):
                raise TransformationRoundtripFailedError.for_roundtrip(
                    measured_error=float(
                        self.measured_roundtrip_error_m
                    ),
                    allowed_error=float(
                        policy.maximum_roundtrip_error_m
                    ),
                    unit="metre",
                    source_crs=source_crs.crs_id,
                    target_crs=target_crs.crs_id,
                )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "bestAvailable": self.best_available,
            "ballpark": self.ballpark,
            "reportedAccuracyM": (
                decimal_to_canonical_string(
                    self.reported_accuracy_m
                )
                if self.reported_accuracy_m is not None
                else None
            ),
            "measuredRoundtripErrorM": (
                decimal_to_canonical_string(
                    self.measured_roundtrip_error_m
                )
                if self.measured_roundtrip_error_m is not None
                else None
            ),
            "requiredGrids": list(self.required_grids),
            "missingGrids": list(self.missing_grids),
            "gridsAvailable": self.grids_available,
        }


@dataclass(frozen=True, slots=True)
class CoordinateTransformRequest:
    """Vollständiger, validierter Auftrag einer CRS-Transformation."""

    coordinate: GlobalCoordinate
    source_crs: CrsDefinition
    target_crs: CrsDefinition
    operation: TransformationOperationKind
    policy: TransformationPolicy = field(
        default_factory=TransformationPolicy
    )
    request_id: str | None = None

    schema_version: ClassVar[str] = "coordinate-transform-request.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate, GlobalCoordinate):
            raise GeoreferencingValidationError(
                "coordinate muss ein GlobalCoordinate sein."
            )
        if not isinstance(self.source_crs, CrsDefinition):
            raise GeoreferencingValidationError(
                "source_crs muss eine CrsDefinition sein."
            )
        if not isinstance(self.target_crs, CrsDefinition):
            raise GeoreferencingValidationError(
                "target_crs muss eine CrsDefinition sein."
            )
        if not isinstance(self.policy, TransformationPolicy):
            raise GeoreferencingValidationError(
                "policy muss eine TransformationPolicy sein."
            )

        normalized_operation = _normalize_enum(
            self.operation,
            TransformationOperationKind,
            field_name="operation",
        )

        if int(self.coordinate.dimension) > int(
            self.source_crs.coordinate_dimension
        ):
            raise CrsDimensionMismatchError.for_dimensions(
                crs=self.source_crs.crs_id,
                expected_dimensions=int(
                    self.source_crs.coordinate_dimension
                ),
                actual_dimensions=int(self.coordinate.dimension),
            )

        object.__setattr__(
            self,
            "operation",
            normalized_operation,
        )
        object.__setattr__(
            self,
            "request_id",
            _normalize_optional_identifier(
                self.request_id,
                field_name="requestId",
            ),
        )

    @property
    def is_identity_transform(self) -> bool:
        return self.source_crs.semantically_matches(self.target_crs)

    def to_dict(
        self,
        *,
        include_crs_definitions: bool = False,
    ) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "requestId": self.request_id,
            "operation": self.operation.value,
            "identityTransform": self.is_identity_transform,
            "coordinate": self.coordinate.to_dict(),
            "sourceCrs": self.source_crs.to_dict(
                include_definition=include_crs_definitions
            ),
            "targetCrs": self.target_crs.to_dict(
                include_definition=include_crs_definitions
            ),
            "policy": self.policy.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CoordinateTransformResult:
    """Validiertes Ergebnis einer CRS-Transformation."""

    request: CoordinateTransformRequest
    coordinate: GlobalCoordinate
    accuracy: TransformationAccuracy
    operation_name: str
    pipeline: str | None = None

    schema_version: ClassVar[str] = "coordinate-transform-result.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.request, CoordinateTransformRequest):
            raise GeoreferencingValidationError(
                "request muss ein CoordinateTransformRequest sein."
            )
        if not isinstance(self.coordinate, GlobalCoordinate):
            raise GeoreferencingValidationError(
                "coordinate muss ein GlobalCoordinate sein."
            )
        if not isinstance(self.accuracy, TransformationAccuracy):
            raise GeoreferencingValidationError(
                "accuracy muss TransformationAccuracy sein."
            )

        if int(self.coordinate.dimension) > int(
            self.request.target_crs.coordinate_dimension
        ):
            raise CrsDimensionMismatchError.for_dimensions(
                crs=self.request.target_crs.crs_id,
                expected_dimensions=int(
                    self.request.target_crs.coordinate_dimension
                ),
                actual_dimensions=int(self.coordinate.dimension),
            )

        normalized_operation_name = _normalize_text(
            self.operation_name,
            field_name="operationName",
            maximum_length=_MAX_NAME_LENGTH,
        )
        normalized_pipeline = _normalize_optional_text(
            self.pipeline,
            field_name="pipeline",
            maximum_length=_MAX_PIPELINE_LENGTH,
        )

        self.accuracy.validate_against(
            self.request.policy,
            source_crs=self.request.source_crs,
            target_crs=self.request.target_crs,
        )

        object.__setattr__(
            self,
            "operation_name",
            normalized_operation_name,
        )
        object.__setattr__(self, "pipeline", normalized_pipeline)

    def to_dict(
        self,
        *,
        include_pipeline: bool = False,
        include_crs_definitions: bool = False,
    ) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "schemaVersion": self.schema_version,
            "request": self.request.to_dict(
                include_crs_definitions=include_crs_definitions
            ),
            "coordinate": self.coordinate.to_dict(),
            "accuracy": self.accuracy.to_dict(),
            "operationName": self.operation_name,
            "pipelineLength": (
                len(self.pipeline)
                if self.pipeline is not None
                else 0
            ),
            "pipelineFingerprint": (
                sha256(self.pipeline.encode("utf-8")).hexdigest()
                if self.pipeline is not None
                else None
            ),
        }

        if include_pipeline:
            payload["pipeline"] = self.pipeline

        return payload


@dataclass(frozen=True, slots=True)
class EarthGridPosition:
    """Sub-Zell-Position im kanonischen Earth-Grid."""

    x: Decimal
    y: Decimal
    z: Decimal

    coordinate_space: ClassVar[str] = "earth_grid"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "x",
            _normalize_decimal(self.x, field_name="gridX"),
        )
        object.__setattr__(
            self,
            "y",
            _normalize_decimal(self.y, field_name="gridY"),
        )
        object.__setattr__(
            self,
            "z",
            _normalize_decimal(self.z, field_name="gridZ"),
        )

    @classmethod
    def from_values(
        cls,
        x: Any,
        y: Any,
        z: Any,
    ) -> Self:
        return cls(x=x, y=y, z=z)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "coordinateSpace": self.coordinate_space,
            "x": decimal_to_canonical_string(self.x),
            "y": decimal_to_canonical_string(self.y),
            "z": decimal_to_canonical_string(self.z),
        }


@dataclass(frozen=True, slots=True)
class ResolvedEarthAnchor:
    """Runtime-Ergebnis der einmaligen Earth-Referenzauflösung.

    Dieses Objekt darf gecacht werden, ist aber keine Persistenzwahrheit.
    Persistiert bleibt ausschließlich ``GlobalReferencePoint``.
    """

    reference: GlobalReferencePoint
    canonical_coordinate: GlobalCoordinate
    canonical_crs: CrsDefinition
    grid_position: EarthGridPosition
    accuracy: TransformationAccuracy
    resolver_version: str

    schema_version: ClassVar[str] = "resolved-earth-anchor.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.reference, GlobalReferencePoint):
            raise GeoreferencingValidationError(
                "reference muss ein GlobalReferencePoint sein."
            )
        if not isinstance(
            self.canonical_coordinate,
            GlobalCoordinate,
        ):
            raise GeoreferencingValidationError(
                "canonical_coordinate muss ein GlobalCoordinate sein."
            )
        if not isinstance(self.canonical_crs, CrsDefinition):
            raise GeoreferencingValidationError(
                "canonical_crs muss eine CrsDefinition sein."
            )
        if not isinstance(self.grid_position, EarthGridPosition):
            raise GeoreferencingValidationError(
                "grid_position muss eine EarthGridPosition sein."
            )
        if not isinstance(self.accuracy, TransformationAccuracy):
            raise GeoreferencingValidationError(
                "accuracy muss TransformationAccuracy sein."
            )

        if int(self.canonical_coordinate.dimension) > int(
            self.canonical_crs.coordinate_dimension
        ):
            raise CrsDimensionMismatchError.for_dimensions(
                crs=self.canonical_crs.crs_id,
                expected_dimensions=int(
                    self.canonical_crs.coordinate_dimension
                ),
                actual_dimensions=int(
                    self.canonical_coordinate.dimension
                ),
            )

        object.__setattr__(
            self,
            "resolver_version",
            _normalize_identifier(
                self.resolver_version,
                field_name="resolverVersion",
            ),
        )

    @property
    def cache_key(self) -> str:
        payload = {
            "referenceFingerprint": self.reference.fingerprint,
            "canonicalCrsFingerprint": (
                self.canonical_crs.definition_fingerprint
            ),
            "resolverVersion": self.resolver_version,
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(
        self,
        *,
        include_crs_definitions: bool = False,
    ) -> dict[str, JsonValue]:
        return {
            "schemaVersion": self.schema_version,
            "cacheKey": self.cache_key,
            "resolverVersion": self.resolver_version,
            "reference": self.reference.to_dict(
                include_crs_definition=include_crs_definitions
            ),
            "canonicalCoordinate": (
                self.canonical_coordinate.to_dict()
            ),
            "canonicalCrs": self.canonical_crs.to_dict(
                include_definition=include_crs_definitions
            ),
            "gridPosition": self.grid_position.to_dict(),
            "accuracy": self.accuracy.to_dict(),
        }


def decimal_to_canonical_string(value: Decimal) -> str:
    """Serialisiert ``Decimal`` ohne Exponent und ohne unnötige Nullen."""

    normalized = _normalize_decimal(
        value,
        field_name="decimal",
    )

    if normalized == 0:
        return "0"

    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")

    return text


def _normalize_decimal(
    value: Any,
    *,
    field_name: str,
) -> Decimal:
    if isinstance(value, bool):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine endliche Dezimalzahl sein.",
            details={
                "field": field_name,
                "actualType": "bool",
            },
        )

    if isinstance(value, Decimal):
        normalized = value
    elif isinstance(value, int):
        normalized = Decimal(value)
    elif isinstance(value, float):
        if not isfinite(value):
            raise GeoreferencingValidationError(
                f"'{field_name}' muss endlich sein.",
                details={
                    "field": field_name,
                    "value": str(value),
                },
            )
        normalized = Decimal(str(value))
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise GeoreferencingValidationError(
                f"'{field_name}' darf nicht leer sein.",
                details={"field": field_name},
            )
        if len(raw) > _MAX_DECIMAL_TEXT_LENGTH:
            raise GeoreferencingValidationError(
                f"'{field_name}' überschreitet die maximale Länge.",
                details={
                    "field": field_name,
                    "length": len(raw),
                    "maximumLength": _MAX_DECIMAL_TEXT_LENGTH,
                },
            )
        try:
            normalized = Decimal(raw)
        except InvalidOperation as error:
            raise GeoreferencingValidationError(
                f"'{field_name}' ist keine gültige Dezimalzahl.",
                details={
                    "field": field_name,
                    "valueLength": len(raw),
                },
                cause=error,
            ) from error
    else:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Zahl oder Dezimalzeichenfolge sein.",
            details={
                "field": field_name,
                "actualType": type(value).__name__,
            },
        )

    if not normalized.is_finite():
        raise GeoreferencingValidationError(
            f"'{field_name}' muss endlich sein.",
            details={
                "field": field_name,
                "value": str(normalized),
            },
        )

    decimal_tuple = normalized.as_tuple()
    digit_count = len(decimal_tuple.digits)
    if digit_count > _MAX_DECIMAL_DIGITS:
        raise GeoreferencingValidationError(
            f"'{field_name}' besitzt zu viele Dezimalstellen.",
            details={
                "field": field_name,
                "digitCount": digit_count,
                "maximumDigits": _MAX_DECIMAL_DIGITS,
            },
        )

    if normalized != 0 and (
        abs(normalized.adjusted())
        > _MAX_DECIMAL_ADJUSTED_EXPONENT
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' besitzt einen unzulässigen Exponenten.",
            details={
                "field": field_name,
                "adjustedExponent": normalized.adjusted(),
                "maximumAbsoluteExponent": (
                    _MAX_DECIMAL_ADJUSTED_EXPONENT
                ),
            },
        )

    return normalized


def _normalize_non_negative_decimal(
    value: Any,
    *,
    field_name: str,
) -> Decimal:
    normalized = _normalize_decimal(
        value,
        field_name=field_name,
    )
    if normalized < 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' darf nicht negativ sein.",
            details={
                "field": field_name,
                "value": decimal_to_canonical_string(normalized),
            },
        )
    return normalized


def _normalize_crs_definition(value: Any) -> str:
    if not isinstance(value, str):
        raise CrsInvalidError.for_value(
            value,
            role="reference",
            reason="CRS-Definition muss eine Zeichenfolge sein.",
        )

    normalized = value.strip()
    if not normalized:
        raise CrsInvalidError.for_value(
            value,
            role="reference",
            reason="CRS-Definition darf nicht leer sein.",
        )

    if len(normalized) > _MAX_CRS_DEFINITION_LENGTH:
        raise CrsInvalidError.for_value(
            value,
            role="reference",
            reason=(
                "CRS-Definition überschreitet die maximale "
                "zulässige Länge."
            ),
        )

    return normalized


def _normalize_identifier(
    value: Any,
    *,
    field_name: str,
) -> str:
    normalized = _normalize_text(
        value,
        field_name=field_name,
        maximum_length=_MAX_IDENTIFIER_LENGTH,
    )

    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise GeoreferencingValidationError(
            f"'{field_name}' enthält unzulässige Zeichen.",
            details={
                "field": field_name,
                "valueLength": len(normalized),
            },
        )

    return normalized


def _normalize_optional_identifier(
    value: Any,
    *,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    return _normalize_identifier(value, field_name=field_name)


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


def _normalize_text_tuple(
    values: Sequence[str],
    *,
    field_name: str,
    maximum_items: int,
    deduplicate: bool = True,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Sequence,
    ):
        raise GeoreferencingValidationError(
            f"'{field_name}' muss eine Sequenz sein.",
            details={
                "field": field_name,
                "actualType": type(values).__name__,
            },
        )

    if len(values) > maximum_items:
        raise GeoreferencingValidationError(
            f"'{field_name}' enthält zu viele Elemente.",
            details={
                "field": field_name,
                "count": len(values),
                "maximumItems": maximum_items,
            },
        )

    normalized: list[str] = []
    seen: set[str] = set()

    for index, value in enumerate(values):
        item = _normalize_text(
            value,
            field_name=f"{field_name}[{index}]",
            maximum_length=_MAX_NAME_LENGTH,
        )
        if deduplicate:
            if item in seen:
                continue
            seen.add(item)
        normalized.append(item)

    return tuple(normalized)


def _normalize_enum(
    value: Any,
    enum_type: type[IntEnum] | type[StrEnum],
    *,
    field_name: str,
) -> Any:
    if isinstance(value, enum_type):
        return value

    try:
        if issubclass(enum_type, IntEnum):
            if isinstance(value, bool):
                raise ValueError
            normalized = enum_type(int(value))
        else:
            if not isinstance(value, str):
                raise ValueError
            normalized = enum_type(value.strip())
    except (TypeError, ValueError) as error:
        raise GeoreferencingValidationError(
            f"'{field_name}' besitzt einen nicht unterstützten Wert.",
            details={
                "field": field_name,
                "value": (
                    value
                    if isinstance(value, (str, int, float, bool))
                    else None
                ),
                "allowedValues": [
                    item.value for item in enum_type
                ],
            },
            cause=error,
        ) from error

    return normalized


def _normalize_positive_int(
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

    if value <= 0:
        raise GeoreferencingValidationError(
            f"'{field_name}' muss größer als 0 sein.",
            details={
                "field": field_name,
                "value": value,
            },
        )

    return value


def _require_mapping(
    value: Any,
    *,
    object_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GeoreferencingValidationError(
            f"{object_name} muss als Mapping übergeben werden.",
            details={
                "actualType": type(value).__name__,
            },
        )
    return value


__all__ = [
    "CoordinateDimension",
    "CoordinateTransformRequest",
    "CoordinateTransformResult",
    "CrsDefinition",
    "CrsDefinitionFormat",
    "EarthGridPosition",
    "EarthGridReference",
    "GlobalCoordinate",
    "GlobalReferencePoint",
    "ResolvedEarthAnchor",
    "TransformationAccuracy",
    "TransformationOperationKind",
    "TransformationPolicy",
    "decimal_to_canonical_string",
]
