"""Project-configurable geodata overlays for Earth chunks.

Terrain and overlays deliberately have different responsibilities:

* the DGM terrain pipeline derives voxel cells and therefore changes the world;
* this module only returns horizontal vector geometry plus rendering metadata;
* the editor drapes that geometry on the currently visible voxel surface.

Overlay definitions come from ``VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON`` and
may be replaced or patched per concrete world through
``WorldInstance.metadata_json['geodataOverlays']``.  The first supported render
mode is ``surface-lines``.  The contract is intentionally versioned so raster,
volume and object renderers can be added without coupling them to terrain.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import json
import math
import os
from threading import RLock
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from src.georeferencing.contracts import GlobalCoordinate
from src.georeferencing.crs import canonical_geographic_crs
from src.georeferencing.earth_grid import LocalEarthPosition
from src.georeferencing.frame_contract import earth_grid_frame_contract


OVERLAY_SCHEMA_VERSION = "geodata-overlays.v1"
SUPPORTED_RENDER_MODE = "surface-lines"
SUPPORTED_RENDER_MODES = frozenset((SUPPORTED_RENDER_MODE, "surface-ribbons"))
DEFAULT_OVERLAY_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "parcel-boundaries",
        "datasetId": "flurstuecke",
        "label": "Flurstuecksgrenzen",
        "enabled": True,
        "source": {
            "kind": "geoserver-wfs",
            "workspace": "public",
            "typeName": "public:flurstuecke",
            "srsName": "EPSG:4326",
            "geometryMode": "polygon-boundaries",
            "versionPolicy": "wfs-live",
        },
        "renderer": {
            "kind": SUPPORTED_RENDER_MODE,
            "style": {
                "color": "#ffd54f",
                "opacity": 0.96,
                "lineWidth": 1.5,
                "verticalOffset": 0.015,
                "sampleStep": 0.25,
            },
        },
        "semantics": {
            "role": "parcel-boundary",
            "classificationSource": False,
        },
    },
    {
        "id": "street-network",
        "datasetId": "strassendaten",
        "label": "Strassen- und Wegenetz",
        "enabled": True,
        "source": {
            "kind": "geoserver-wfs",
            "workspace": "public",
            "typeName": "public:strassendaten",
            "srsName": "EPSG:4326",
            "geometryMode": "line-centerlines",
            "versionPolicy": "wfs-live",
            "maxFeatures": 10_000,
        },
        "renderer": {
            "kind": "surface-ribbons",
            "style": {
                "color": "#fbfcfd",
                "opacity": 1.0,
                "lineWidth": 1.0,
                "surfaceWidth": 6.0,
                "verticalOffset": 0.03,
                "sampleStep": 0.5,
            },
        },
        "semantics": {
            "role": "street-network",
            "classificationSource": True,
        },
    },
)


class GeodataOverlayError(RuntimeError):
    """A recoverable overlay source or contract error."""


def _remote_service_failure(error: BaseException) -> bool:
    """Return True only for transport/service outages, not bad layer data."""
    current: Optional[BaseException] = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (URLError, TimeoutError, ConnectionError, OSError)):
            return True
        current = current.__cause__ or current.__context__
    message = _text(error).lower()
    return "nicht erreichbar" in message or "timed out" in message or "timeout" in message


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.getenv(name, "")).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _text(value: Any, fallback: str = "") -> str:
    try:
        normalized = str(value or "").strip()
    except Exception:
        return fallback
    return normalized or fallback


def _number(
    value: Any,
    fallback: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(normalized):
        return fallback
    return max(minimum, min(maximum, normalized))


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in patch.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


def _json_definitions_from_env() -> list[dict[str, Any]]:
    raw = _text(os.getenv("VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON"))
    if not raw:
        return [dict(item) for item in DEFAULT_OVERLAY_DEFINITIONS]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GeodataOverlayError(
            "VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON ist kein gueltiges JSON."
        ) from exc
    if not isinstance(parsed, list):
        raise GeodataOverlayError(
            "VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON muss ein JSON-Array sein."
        )
    return [dict(item) for item in parsed if isinstance(item, Mapping)]


def _world_overlay_value(world: Any) -> Any:
    metadata = getattr(world, "metadata_json", None)
    if not isinstance(metadata, Mapping):
        return None
    for key in ("geodataOverlays", "geodata_overlays", "visualOverlays"):
        if key in metadata:
            return metadata.get(key)
    geodata = metadata.get("geodata")
    if isinstance(geodata, Mapping):
        return geodata.get("overlays")
    return None


def _effective_raw_definitions(world: Any) -> list[dict[str, Any]]:
    defaults = _json_definitions_from_env()
    override = _world_overlay_value(world)
    if isinstance(override, Sequence) and not isinstance(
        override, (str, bytes, bytearray)
    ):
        return [dict(item) for item in override if isinstance(item, Mapping)]
    if not isinstance(override, Mapping):
        return defaults

    raw_items = override.get("items")
    items = (
        [dict(item) for item in raw_items if isinstance(item, Mapping)]
        if isinstance(raw_items, Sequence)
        and not isinstance(raw_items, (str, bytes, bytearray))
        else []
    )
    if not bool(override.get("inheritDefaults", True)):
        return items

    merged_by_id = {
        _text(item.get("id")): dict(item)
        for item in defaults
        if _text(item.get("id"))
    }
    order = list(merged_by_id)
    for item in items:
        overlay_id = _text(item.get("id"))
        if not overlay_id:
            continue
        if overlay_id not in merged_by_id:
            order.append(overlay_id)
            merged_by_id[overlay_id] = item
        else:
            merged_by_id[overlay_id] = _deep_merge(
                merged_by_id[overlay_id], item
            )
    return [merged_by_id[overlay_id] for overlay_id in order]


@dataclass(frozen=True, slots=True)
class OverlayDefinition:
    overlay_id: str
    dataset_id: str
    label: str
    workspace: str
    type_name: str
    srs_name: str
    geometry_mode: str
    version_policy: str
    render_mode: str
    semantic_role: str
    classification_source: bool
    color: str
    opacity: float
    line_width: float
    surface_width: float
    vertical_offset: float
    sample_step: float
    max_features: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OverlayDefinition":
        source = value.get("source") if isinstance(value.get("source"), Mapping) else {}
        renderer = (
            value.get("renderer")
            if isinstance(value.get("renderer"), Mapping)
            else {}
        )
        style = (
            renderer.get("style")
            if isinstance(renderer.get("style"), Mapping)
            else {}
        )
        semantics = (
            value.get("semantics")
            if isinstance(value.get("semantics"), Mapping)
            else {}
        )
        overlay_id = _text(value.get("id"))
        dataset_id = _text(value.get("datasetId") or value.get("dataset_id"))
        if not overlay_id or not dataset_id:
            raise GeodataOverlayError("Overlay id und datasetId sind erforderlich.")
        workspace = _text(source.get("workspace"), "public")
        type_name = _text(
            source.get("typeName") or source.get("type_name"),
            f"{workspace}:{dataset_id}",
        )
        render_mode = _text(renderer.get("kind"), SUPPORTED_RENDER_MODE)
        return cls(
            overlay_id=overlay_id,
            dataset_id=dataset_id,
            label=_text(value.get("label"), overlay_id),
            workspace=workspace,
            type_name=type_name,
            srs_name=_text(source.get("srsName"), "EPSG:4326"),
            geometry_mode=_text(
                source.get("geometryMode"), "polygon-boundaries"
            ),
            version_policy=_text(source.get("versionPolicy"), "wfs-live").lower(),
            render_mode=render_mode,
            semantic_role=_text(semantics.get("role"), "visual-reference"),
            classification_source=bool(
                semantics.get("classificationSource", False)
            ),
            color=_text(style.get("color"), "#ffd54f"),
            opacity=_number(
                style.get("opacity"), 0.96, minimum=0.0, maximum=1.0
            ),
            line_width=_number(
                style.get("lineWidth"), 1.5, minimum=0.1, maximum=20.0
            ),
            surface_width=_number(
                style.get("surfaceWidth"), 0.0, minimum=0.0, maximum=64.0
            ),
            vertical_offset=_number(
                style.get("verticalOffset"),
                0.015,
                minimum=0.001,
                maximum=2.0,
            ),
            sample_step=_number(
                style.get("sampleStep"), 0.25, minimum=0.05, maximum=2.0
            ),
            max_features=max(
                1,
                min(10_000, int(source.get("maxFeatures") or 2_000)),
            ),
        )

    def public_contract(self) -> dict[str, Any]:
        return {
            "id": self.overlay_id,
            "datasetId": self.dataset_id,
            "label": self.label,
            "renderMode": self.render_mode,
            "semanticRole": self.semantic_role,
            "classificationSource": self.classification_source,
            "style": {
                "color": self.color,
                "opacity": self.opacity,
                "lineWidth": self.line_width,
                "surfaceWidth": self.surface_width,
                "verticalOffset": self.vertical_offset,
                "sampleStep": self.sample_step,
            },
        }


def effective_overlay_definitions(world: Any) -> tuple[OverlayDefinition, ...]:
    definitions: list[OverlayDefinition] = []
    for raw in _effective_raw_definitions(world):
        if not bool(raw.get("enabled", True)):
            continue
        definition = OverlayDefinition.from_mapping(raw)
        if definition.render_mode not in SUPPORTED_RENDER_MODES:
            continue
        definitions.append(definition)
    return tuple(definitions)


@dataclass(frozen=True, slots=True)
class OverlayPipelineConfig:
    enabled: bool
    orchestrator_base_url: str
    geoserver_base_url: str
    service_token: str
    request_timeout_seconds: float
    version_cache_seconds: float
    tile_cache_seconds: float

    @classmethod
    def from_env(cls) -> "OverlayPipelineConfig":
        return cls(
            enabled=_env_bool("VECTOPLAN_CHUNK_GEODATA_OVERLAYS_ENABLED", True),
            orchestrator_base_url=_text(
                os.getenv("GEOSERVER_ORCHESTRATOR_INTERNAL_URL"),
                "http://geoserver-orchestrator:8010",
            ).rstrip("/"),
            geoserver_base_url=_text(
                os.getenv("GEOSERVER_INTERNAL_BASE_URL"),
                "http://geoserver:8080/geoserver",
            ).rstrip("/"),
            service_token=_text(
                os.getenv("VECTOPLAN_CHUNK_GEODATA_SERVICE_TOKEN")
                or os.getenv("PRODUCTION_PUBLICATION_SERVICE_TOKEN")
            ),
            request_timeout_seconds=_number(
                os.getenv("VECTOPLAN_CHUNK_GEODATA_OVERLAY_TIMEOUT_SECONDS"),
                1.5,
                minimum=0.25,
                maximum=120.0,
            ),
            version_cache_seconds=_number(
                os.getenv("VECTOPLAN_CHUNK_GEODATA_OVERLAY_VERSION_CACHE_SECONDS"),
                60.0,
                minimum=0.0,
                maximum=3_600.0,
            ),
            tile_cache_seconds=_number(
                os.getenv("VECTOPLAN_CHUNK_GEODATA_OVERLAY_TILE_CACHE_SECONDS"),
                30.0,
                minimum=0.0,
                maximum=86_400.0,
            ),
        )


class OrchestratorOverlayClient:
    def __init__(self, config: OverlayPipelineConfig) -> None:
        self.config = config
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = RLock()

    def _request(self, path: str) -> dict[str, Any]:
        if not self.config.service_token:
            raise GeodataOverlayError("Der interne Geodaten-Service-Token fehlt.")
        request = Request(
            f"{self.config.orchestrator_base_url}{path}",
            headers={
                "Accept": "application/json",
                "X-Vectoplan-Service-Token": self.config.service_token,
                "X-Requested-With": "fetch",
                "User-Agent": "vectoplan-chunk-overlays/1",
            },
            method="GET",
        )
        try:
            with urlopen(
                request, timeout=self.config.request_timeout_seconds
            ) as response:
                raw = response.read(8 * 1024 * 1024)
        except HTTPError as exc:
            raise GeodataOverlayError(
                f"GeoServer Orchestrator antwortete mit HTTP {exc.code}."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GeodataOverlayError(
                "GeoServer Orchestrator ist fuer Overlays nicht erreichbar."
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GeodataOverlayError(
                "GeoServer Orchestrator lieferte kein gueltiges JSON."
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            raise GeodataOverlayError(
                _text(
                    payload.get("message") if isinstance(payload, Mapping) else None,
                    "Die Overlay-Publikation ist nicht verfuegbar.",
                )
            )
        return dict(payload)

    def approved_publication(self, dataset_id: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._cache.get(dataset_id)
        if cached and now - cached[0] <= self.config.version_cache_seconds:
            return dict(cached[1])
        with self._lock:
            now = time.monotonic()
            cached = self._cache.get(dataset_id)
            if cached and now - cached[0] <= self.config.version_cache_seconds:
                return dict(cached[1])
            encoded = quote(dataset_id, safe="")
            try:
                payload = self._request(
                    f"/admin/api/production-publications/{encoded}"
                )
            except GeodataOverlayError:
                # An expired publication is still safer than making every
                # editable chunk wait for an optional catalog service.
                if cached:
                    stale = dict(cached[1])
                    stale["stale"] = True
                    return stale
                raise
            publication = payload.get("publication")
            if not isinstance(publication, Mapping):
                raise GeodataOverlayError("Publikationsvertrag fehlt.")
            approval = (
                publication.get("approval")
                if isinstance(publication.get("approval"), Mapping)
                else {}
            )
            release_key = _text(approval.get("approved_release_key"))
            if not release_key:
                raise GeodataOverlayError(
                    f"Datensatz '{dataset_id}' besitzt keinen freigegebenen Release."
                )
            if _text(publication.get("release_key")) != release_key:
                payload = self._request(
                    f"/admin/api/production-publications/{encoded}"
                    f"?{urlencode({'release_key': release_key})}"
                )
                publication = payload.get("publication")
                if not isinstance(publication, Mapping):
                    raise GeodataOverlayError("Freigegebener Publikationsvertrag fehlt.")
            result = dict(publication)
            result["release_key"] = release_key
            self._cache[dataset_id] = (now, result)
            return dict(result)


class GeoServerWfsClient:
    def __init__(self, config: OverlayPipelineConfig) -> None:
        self.config = config

    def feature_collection(
        self,
        definition: OverlayDefinition,
        bbox: tuple[float, float, float, float],
    ) -> dict[str, Any]:
        min_lon, min_lat, max_lon, max_lat = bbox
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": definition.type_name,
            "outputFormat": "application/json",
            "srsName": definition.srs_name,
            "bbox": (
                f"{min_lon:.10f},{min_lat:.10f},"
                f"{max_lon:.10f},{max_lat:.10f},EPSG:4326"
            ),
            "count": str(definition.max_features),
        }
        workspace = quote(definition.workspace, safe="")
        url = f"{self.config.geoserver_base_url}/{workspace}/ows?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/geo+json, application/json",
                "User-Agent": "vectoplan-chunk-overlays/1",
            },
            method="GET",
        )
        try:
            with urlopen(
                request, timeout=self.config.request_timeout_seconds
            ) as response:
                raw = response.read(32 * 1024 * 1024)
        except HTTPError as exc:
            raise GeodataOverlayError(
                f"GeoServer WFS antwortete mit HTTP {exc.code}."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GeodataOverlayError(
                "GeoServer WFS ist fuer Overlays nicht erreichbar."
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GeodataOverlayError("GeoServer WFS lieferte kein GeoJSON.") from exc
        if not isinstance(payload, Mapping) or payload.get("type") != "FeatureCollection":
            raise GeodataOverlayError("GeoServer WFS lieferte keine FeatureCollection.")
        return dict(payload)


def _geometry_lines(geometry: Any) -> Iterable[list[tuple[float, float]]]:
    if not isinstance(geometry, Mapping):
        return
    geometry_type = _text(geometry.get("type"))
    coordinates = geometry.get("coordinates")
    if geometry_type == "LineString":
        yield _coordinate_line(coordinates)
    elif geometry_type == "MultiLineString" and isinstance(coordinates, Sequence):
        for line in coordinates:
            yield _coordinate_line(line)
    elif geometry_type == "Polygon" and isinstance(coordinates, Sequence):
        for ring in coordinates:
            yield _coordinate_line(ring)
    elif geometry_type == "MultiPolygon" and isinstance(coordinates, Sequence):
        for polygon in coordinates:
            if isinstance(polygon, Sequence):
                for ring in polygon:
                    yield _coordinate_line(ring)
    elif geometry_type == "GeometryCollection":
        geometries = geometry.get("geometries")
        if isinstance(geometries, Sequence):
            for child in geometries:
                yield from _geometry_lines(child)


def _coordinate_line(value: Any) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return result
    for coordinate in value:
        if (
            not isinstance(coordinate, Sequence)
            or isinstance(coordinate, (str, bytes, bytearray))
            or len(coordinate) < 2
        ):
            continue
        try:
            x = float(coordinate[0])
            y = float(coordinate[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            result.append((x, y))
    return result


def _clip_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    bounds: tuple[float, float, float, float],
) -> Optional[tuple[tuple[float, float], tuple[float, float]]]:
    """Liang-Barsky clipping for one horizontal x/z segment."""
    x0, z0 = start
    x1, z1 = end
    min_x, min_z, max_x, max_z = bounds
    dx = x1 - x0
    dz = z1 - z0
    p = (-dx, dx, -dz, dz)
    q = (x0 - min_x, max_x - x0, z0 - min_z, max_z - z0)
    lower = 0.0
    upper = 1.0
    for denominator, numerator in zip(p, q, strict=True):
        if abs(denominator) <= 1e-12:
            if numerator < 0.0:
                return None
            continue
        ratio = numerator / denominator
        if denominator < 0.0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None
    clipped_start = (x0 + lower * dx, z0 + lower * dz)
    clipped_end = (x0 + upper * dx, z0 + upper * dz)
    if math.dist(clipped_start, clipped_end) <= 1e-6:
        return None
    return clipped_start, clipped_end


def _segment_key(
    start: tuple[float, float], end: tuple[float, float]
) -> tuple[tuple[int, int], tuple[int, int]]:
    first = (round(start[0] * 1_000), round(start[1] * 1_000))
    second = (round(end[0] * 1_000), round(end[1] * 1_000))
    return (first, second) if first <= second else (second, first)


def _local_to_wgs84(provider: Any, x: float, z: float) -> tuple[float, float]:
    result = provider.local_to_global(
        LocalEarthPosition(
            x=Decimal(str(x)), y=Decimal("0"), z=Decimal(str(z))
        ),
        target_crs=canonical_geographic_crs(),
    )
    coordinate = result.target_coordinate
    return float(coordinate.x), float(coordinate.y)


def _wgs84_to_local(provider: Any, lon: float, lat: float) -> tuple[float, float]:
    result = provider.global_to_local(
        GlobalCoordinate.from_values(lon, lat, 0.0),
        canonical_geographic_crs(),
    )
    position = result.local_position
    return float(position.x), float(position.z)


class GeodataOverlayService:
    def __init__(
        self,
        config: OverlayPipelineConfig,
        *,
        orchestrator: Optional[OrchestratorOverlayClient] = None,
        wfs: Optional[GeoServerWfsClient] = None,
    ) -> None:
        self.config = config
        self.orchestrator = orchestrator or OrchestratorOverlayClient(config)
        self.wfs = wfs or GeoServerWfsClient(config)
        self._tiles: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._unavailable: dict[tuple[str, str], tuple[float, str]] = {}
        self._remote_failure_until = 0.0
        self._remote_failure_message = ""
        self._lock = RLock()

    def _trip_remote_circuit(self, error: BaseException) -> None:
        if not _remote_service_failure(error):
            return
        with self._lock:
            self._remote_failure_until = max(self._remote_failure_until, time.monotonic() + 30.0)
            self._remote_failure_message = _text(error, "Optionaler Geodatendienst nicht erreichbar")[:500]

    def _tile_item(
        self,
        *,
        definition: OverlayDefinition,
        publication: Mapping[str, Any],
        provider: Any,
        chunk_x: int,
        chunk_z: int,
        chunk_size: int,
    ) -> dict[str, Any]:
        release_key = _text(publication.get("release_key"))
        reference_fingerprint = _text(
            getattr(provider, "reference_fingerprint", None), "unknown-reference"
        )
        cache_key = (
            reference_fingerprint,
            definition.overlay_id,
            definition.dataset_id,
            release_key,
            definition.geometry_mode,
            definition.render_mode,
            definition.semantic_role,
            definition.classification_source,
            definition.color,
            definition.opacity,
            definition.line_width,
            definition.surface_width,
            definition.vertical_offset,
            definition.sample_step,
            definition.max_features,
            int(chunk_x),
            int(chunk_z),
            int(chunk_size),
        )
        now = time.monotonic()
        cached = self._tiles.get(cache_key)
        if cached and now - cached[0] <= self.config.tile_cache_seconds:
            return dict(cached[1])

        min_x = int(chunk_x) * int(chunk_size)
        min_z = int(chunk_z) * int(chunk_size)
        max_x = min_x + int(chunk_size)
        max_z = min_z + int(chunk_size)
        corners = (
            _local_to_wgs84(provider, min_x, min_z),
            _local_to_wgs84(provider, max_x, min_z),
            _local_to_wgs84(provider, max_x, max_z),
            _local_to_wgs84(provider, min_x, max_z),
        )
        longitudes = [item[0] for item in corners]
        latitudes = [item[1] for item in corners]
        bbox = (
            min(longitudes), min(latitudes), max(longitudes), max(latitudes)
        )
        try:
            feature_collection = self.wfs.feature_collection(definition, bbox)
        except Exception as exc:
            self._trip_remote_circuit(exc)
            if cached:
                stale = dict(cached[1])
                stale["source"] = {**dict(stale.get("source") or {}), "stale": True}
                return stale
            raise

        segments: list[list[list[float]]] = []
        seen: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        feature_count = 0
        source_segment_count = 0
        features = feature_collection.get("features")
        if isinstance(features, Sequence):
            for feature in features:
                if not isinstance(feature, Mapping):
                    continue
                feature_count += 1
                for line in _geometry_lines(feature.get("geometry")):
                    if len(line) < 2:
                        continue
                    local_line = [
                        _wgs84_to_local(provider, lon, lat) for lon, lat in line
                    ]
                    for index in range(1, len(local_line)):
                        source_segment_count += 1
                        clipped = _clip_segment(
                            local_line[index - 1],
                            local_line[index],
                            (min_x, min_z, max_x, max_z),
                        )
                        if clipped is None:
                            continue
                        start, end = clipped
                        key = _segment_key(start, end)
                        if key in seen:
                            continue
                        seen.add(key)
                        segments.append(
                            [
                                [round(start[0], 4), round(start[1], 4)],
                                [round(end[0], 4), round(end[1], 4)],
                            ]
                        )

        item = {
            **definition.public_contract(),
            "releaseKey": release_key,
            "tileKey": f"{chunk_x}:{chunk_z}",
            "source": {
                "kind": "geoserver-wfs",
                "workspace": definition.workspace,
                "typeName": definition.type_name,
                "srsName": definition.srs_name,
                "geometryMode": definition.geometry_mode,
                "versionPolicy": definition.version_policy,
            },
            "geometry": {
                "type": "MultiLineString",
                "dimensions": "world-xz",
                "coordinates": segments,
            },
            "stats": {
                "featureCount": feature_count,
                "sourceSegmentCount": source_segment_count,
                "emittedSegmentCount": len(segments),
            },
        }
        with self._lock:
            if len(self._tiles) >= 2_048:
                oldest_key = min(self._tiles, key=lambda key: self._tiles[key][0])
                self._tiles.pop(oldest_key, None)
            self._tiles[cache_key] = (now, item)
        return dict(item)

    def chunk_contract(
        self,
        *,
        world: Any,
        provider: Any,
        chunk_x: int,
        chunk_z: int,
        chunk_size: int,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            return {
                "schemaVersion": OVERLAY_SCHEMA_VERSION,
                "status": "disabled",
                "items": [],
            }
        definitions = effective_overlay_definitions(world)
        items: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        availability: list[dict[str, str]] = []
        for definition in definitions:
            source_key = (definition.dataset_id, definition.type_name)
            try:
                if self._remote_failure_until > time.monotonic():
                    raise GeodataOverlayError(self._remote_failure_message or "Optionaler Geodatendienst nicht erreichbar")
                unavailable = self._unavailable.get(source_key)
                if unavailable and unavailable[0] > time.monotonic():
                    raise GeodataOverlayError(unavailable[1])
                publication = (
                    self.orchestrator.approved_publication(definition.dataset_id)
                    if definition.version_policy == "approved-release"
                    else {
                        "release_key": (
                            f"live:{definition.workspace}:{definition.type_name}"
                        )
                    }
                )
                item = self._tile_item(
                        definition=definition,
                        publication=publication,
                        provider=provider,
                        chunk_x=chunk_x,
                        chunk_z=chunk_z,
                        chunk_size=chunk_size,
                    )
                availability.append({"id":definition.overlay_id,"kind":"vector","status":"available" if item["geometry"]["coordinates"] else "no-data"})
                if item["geometry"]["coordinates"]:
                    items.append(item)
                self._unavailable.pop(source_key, None)
            except Exception as exc:
                self._trip_remote_circuit(exc)
                if not self._unavailable.get(source_key) or self._unavailable[source_key][0] <= time.monotonic():
                    if len(self._unavailable)>=128:
                        self._unavailable.pop(next(iter(self._unavailable)))
                    self._unavailable[source_key] = (time.monotonic()+30, _text(exc)[:500])
                availability.append({"id":definition.overlay_id,"kind":"vector","status":"unavailable"})
                errors.append(
                    {
                        "id": definition.overlay_id,
                        "datasetId": definition.dataset_id,
                        "message": f"{type(exc).__name__}: {_text(exc)}"[:500],
                    }
                )
        contract = {
            "schemaVersion": OVERLAY_SCHEMA_VERSION,
            "status": "ready" if not errors else "degraded",
            "referenceFingerprint": _text(
                getattr(provider, "reference_fingerprint", None),
                "unknown-reference",
            ),
            "items": items,
            "errors": errors,
            "availability": availability,
        }
        earth_grid = earth_grid_frame_contract(provider)
        if earth_grid is not None:
            contract["earthGrid"] = earth_grid
        return contract


_DEFAULT_SERVICE: Optional[GeodataOverlayService] = None
_DEFAULT_SERVICE_LOCK = RLock()


def get_default_geodata_overlay_service() -> GeodataOverlayService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is not None:
        return _DEFAULT_SERVICE
    with _DEFAULT_SERVICE_LOCK:
        if _DEFAULT_SERVICE is None:
            _DEFAULT_SERVICE = GeodataOverlayService(
                OverlayPipelineConfig.from_env()
            )
    return _DEFAULT_SERVICE


def attach_geodata_overlays(chunk: dict[str, Any], world: Any) -> bool:
    """Attach a non-persistent overlay contract to one serialized Earth chunk."""
    if not bool(getattr(world, "is_earth_world", False)):
        return False
    try:
        provider = world.build_earth_provider()
        contract = get_default_geodata_overlay_service().chunk_contract(
            world=world,
            provider=provider,
            chunk_x=int(chunk.get("chunkX") or 0),
            chunk_z=int(chunk.get("chunkZ") or 0),
            chunk_size=int(chunk.get("chunkSize") or getattr(world, "chunk_size", 16)),
        )
        from src.geodata.lod2_buildings import append_building_overlay

        append_building_overlay(contract, chunk=chunk, world=world, provider=provider)
    except Exception as exc:
        contract = {
            "schemaVersion": OVERLAY_SCHEMA_VERSION,
            "status": "degraded",
            "items": [],
            "errors": [
                {
                    "id": "overlay-pipeline",
                    "datasetId": "",
                    "message": f"{type(exc).__name__}: {_text(exc)}"[:500],
                }
            ],
        }
    # Selection metadata is deliberately computed after every optional source.
    # The photorealistic candidate is fail-closed and contributes no asset URL;
    # a locked or absent high-priority layer therefore cannot blank LoD2.
    from src.geodata.visual_layer_resolution import attach_visual_layer_resolution

    attach_visual_layer_resolution(contract)
    metadata = chunk.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    merged = dict(metadata)
    merged["geodataOverlays"] = contract
    chunk["metadata"] = merged
    return True


def clear_geodata_overlay_caches() -> None:
    global _DEFAULT_SERVICE
    with _DEFAULT_SERVICE_LOCK:
        _DEFAULT_SERVICE = None


__all__ = (
    "DEFAULT_OVERLAY_DEFINITIONS",
    "GeodataOverlayError",
    "GeodataOverlayService",
    "GeoServerWfsClient",
    "OVERLAY_SCHEMA_VERSION",
    "OrchestratorOverlayClient",
    "OverlayDefinition",
    "OverlayPipelineConfig",
    "SUPPORTED_RENDER_MODES",
    "attach_geodata_overlays",
    "clear_geodata_overlay_caches",
    "effective_overlay_definitions",
    "get_default_geodata_overlay_service",
)
