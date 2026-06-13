# src/world/serializer.py
"""
VECTOPLAN World Serializer.

Diese Datei serialisiert interne World-Modelle in JSON-nahe Strukturen,
die später direkt von Flask-Routes zurückgegeben werden können.

Aufgabe:
- WorldDefinition → API-/Editor-kompatible Welt-Metadaten
- GeneratedChunk → API-/Editor-kompatible Chunk-Daten
- WorldListResult → API-kompatible Weltliste
- ChunkBatchResult-ähnliche Objekte → API-kompatible Batch-Antwort
- PaletteEntry → stabile Palettenstruktur
- keine Flask-Abhängigkeit
- keine Datenbank
- keine konkrete Flat-World-Logik
- keine Three.js-Objekte

Wichtig:
Diese Datei ist die Grenze zwischen interner Python-Logik und späterem
HTTP-/Editor-JSON.

Grundinvariante für Cells:

    cellValue = 0
        → Air

    cellValue = paletteIndex + 1
        → Block mit diesem Palette-Index

Diese Information wird explizit in den serialisierten Chunk-Daten mitgegeben,
damit Editor und Backend dieselbe Interpretation verwenden.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import is_dataclass, asdict
from typing import Any, Final

try:
    from src.world.errors import (
        WorldError,
        WorldSerializationError,
        coerce_world_error,
        make_json_safe,
    )
    from src.world.models import (
        DEFAULT_AIR_CELL_VALUE,
        DEFAULT_CELL_INDEX_ORDER,
        ChunkRequest,
        GeneratedChunk,
        PaletteEntry,
        WorldDefinition,
        WorldListResult,
        WorldProviderInfo,
        build_chunk_key,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.serializer requires src.world.errors and src.world.models "
        "to be importable before the serializer can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORLD_SERIALIZER_VERSION: Final[str] = "0.1.0"

CELL_ENCODING_VERSION: Final[str] = "cell-encoding.palette-index-plus-one.v1"

RUNTIME_CHUNK_CONTENT_VERSION: Final[str] = "runtime-chunk-content.v1"

WORLD_METADATA_RESPONSE_VERSION: Final[str] = "world-metadata-response.v1"
WORLD_LIST_RESPONSE_VERSION: Final[str] = "world-list-response.v1"
CHUNK_RESPONSE_VERSION: Final[str] = "chunk-response.v1"
CHUNK_BATCH_RESPONSE_VERSION: Final[str] = "chunk-batch-response.v1"

DEFAULT_INCLUDE_DEBUG_METADATA: Final[bool] = False
DEFAULT_INCLUDE_WORLD_PALETTE: Final[bool] = True
DEFAULT_INCLUDE_CHUNK_PALETTE: Final[bool] = True
DEFAULT_INCLUDE_CELLS: Final[bool] = True
DEFAULT_INCLUDE_CELL_ENCODING: Final[bool] = True


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt beliebige Werte defensiv in bereinigte Strings um.
    """
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


def _safe_int(value: Any, *, default: int = 0) -> int:
    """
    Wandelt beliebige Werte defensiv in int um.
    """
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    """
    Wandelt beliebige Werte defensiv in float um.
    """
    try:
        return float(value)
    except Exception:
        return default


def _without_none(data: Mapping[str, Any]) -> dict[str, Any]:
    """
    Entfernt None-Werte aus einem Dictionary.
    """
    return {key: value for key, value in data.items() if value is not None}


def _as_mapping(value: Any) -> dict[str, Any]:
    """
    Wandelt ein Objekt möglichst robust in ein Dictionary um.
    """
    if value is None:
        return {}

    if isinstance(value, Mapping):
        return dict(value)

    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            result = value.to_dict()
            if isinstance(result, Mapping):
                return dict(result)
            return {"value": make_json_safe(result)}
        except Exception:
            return {"value": make_json_safe(value)}

    if is_dataclass(value):
        try:
            return asdict(value)
        except Exception:
            return {"value": make_json_safe(value)}

    if hasattr(value, "__dict__"):
        try:
            return dict(vars(value))
        except Exception:
            return {"value": make_json_safe(value)}

    return {"value": make_json_safe(value)}


def _json_safe_dict(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    Normalisiert ein Mapping in ein JSON-sicheres Dictionary.
    """
    if value is None:
        return {}

    safe = make_json_safe(value)

    if isinstance(safe, Mapping):
        return dict(safe)

    return {"value": safe}


def _normalize_cells(cells: Sequence[int] | tuple[int, ...] | list[int]) -> list[int]:
    """
    Normalisiert ein Zellarray für JSON-Ausgabe.

    Aktuell wird eine einfache Integer-Liste ausgegeben.
    Später kann hier optional Kompression oder Binär-/Base64-Serialisierung
    ergänzt werden, ohne Generatoren ändern zu müssen.
    """
    try:
        return [int(value) for value in cells]
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize chunk cells.",
            details={
                "cellsType": type(cells).__name__,
            },
            cause=exc,
        ) from exc


def _extract_attr_or_key(value: Any, key: str, default: Any = None) -> Any:
    """
    Liest ein Attribut oder Dictionary-Feld robust aus.
    """
    if isinstance(value, Mapping):
        return value.get(key, default)

    return getattr(value, key, default)


def _build_success_response(
    *,
    payload_key: str,
    payload: Any,
    response_version: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Baut eine einheitliche erfolgreiche API-nahe Response.
    """
    response: dict[str, Any] = {
        "ok": True,
        "responseVersion": response_version,
        payload_key: payload,
    }

    if metadata:
        response["metadata"] = _json_safe_dict(metadata)

    return response


# ---------------------------------------------------------------------------
# Shared serializations
# ---------------------------------------------------------------------------

def serialize_cell_encoding() -> dict[str, Any]:
    """
    Serialisiert die harte Cell-Encoding-Invariante.

    Diese Struktur sollte in Chunk-Antworten enthalten sein, damit der Editor
    nicht implizit raten muss, wie cellValue zu interpretieren ist.
    """
    return {
        "version": CELL_ENCODING_VERSION,
        "airCellValue": DEFAULT_AIR_CELL_VALUE,
        "blockCellValueRule": "paletteIndex + 1",
        "examples": [
            {
                "paletteIndex": 0,
                "cellValue": 1,
            },
            {
                "paletteIndex": 1,
                "cellValue": 2,
            },
        ],
    }


def serialize_palette_entry(
    entry: PaletteEntry | Mapping[str, Any],
    *,
    palette_index: int | None = None,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """
    Serialisiert einen Paletteneintrag.

    Falls palette_index angegeben ist, wird zusätzlich der zugehörige
    cellValue ausgegeben.
    """
    try:
        if isinstance(entry, PaletteEntry):
            data = entry.to_dict(
                camel_case=True,
                include_metadata=include_metadata,
            )
        elif isinstance(entry, Mapping):
            data = dict(entry)
        else:
            raise WorldSerializationError(
                "Palette entry must be PaletteEntry or mapping.",
                details={"entryType": type(entry).__name__},
            )

        if palette_index is not None:
            index = int(palette_index)
            data = {
                "paletteIndex": index,
                "cellValue": index + 1,
                **data,
            }

        if not include_metadata:
            data.pop("metadata", None)

        return make_json_safe(data)

    except WorldSerializationError:
        raise
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize palette entry.",
            details={
                "entryType": type(entry).__name__,
                "paletteIndex": palette_index,
            },
            cause=exc,
        ) from exc


def serialize_palette(
    palette: Sequence[PaletteEntry] | tuple[PaletteEntry, ...] | list[PaletteEntry],
    *,
    include_metadata: bool = True,
    include_indices: bool = True,
) -> list[dict[str, Any]]:
    """
    Serialisiert eine Palette.

    Standardmäßig werden paletteIndex und cellValue ergänzt.
    """
    try:
        result: list[dict[str, Any]] = []

        for index, entry in enumerate(palette):
            result.append(
                serialize_palette_entry(
                    entry,
                    palette_index=index if include_indices else None,
                    include_metadata=include_metadata,
                )
            )

        return result

    except WorldSerializationError:
        raise
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize palette.",
            details={"paletteType": type(palette).__name__},
            cause=exc,
        ) from exc


# ---------------------------------------------------------------------------
# World serialization
# ---------------------------------------------------------------------------

def serialize_world_definition(
    world: WorldDefinition | Mapping[str, Any],
    *,
    include_palette: bool = DEFAULT_INCLUDE_WORLD_PALETTE,
    include_metadata: bool = True,
    include_raw_config: bool = False,
    include_debug_metadata: bool = DEFAULT_INCLUDE_DEBUG_METADATA,
) -> dict[str, Any]:
    """
    Serialisiert eine WorldDefinition in eine API-/Editor-nahe Struktur.

    Diese Funktion erzeugt nur das world-Objekt, nicht den äußeren
    { "ok": true, "world": ... } Wrapper.
    """
    try:
        if isinstance(world, WorldDefinition):
            payload: dict[str, Any] = {
                "worldId": world.world_id,
                "worldType": world.world_type,
                "label": world.label,
                "schemaVersion": world.schema_version,
                "generatorType": world.generator_type,
                "generatorVersion": world.generator_version,
                "chunkSize": world.chunk_size,
                "cellSize": world.cell_size,
                "coordinateSystem": world.coordinate_system,
                "projectionType": world.projection_type,
                "topologyType": world.topology_type,
                "surfaceY": world.surface_y,
                "minY": world.min_y,
                "maxY": world.max_y,
                "seed": world.seed,
                "blockRegistryId": world.block_registry_id,
                "blockRegistryVersion": world.block_registry_version,
            }

            if include_palette:
                payload["palette"] = serialize_palette(
                    world.palette,
                    include_metadata=include_metadata,
                    include_indices=True,
                )

            if include_metadata:
                payload["metadata"] = _json_safe_dict(world.metadata)

            if include_raw_config:
                payload["rawConfig"] = make_json_safe(world.raw_config)

        elif isinstance(world, Mapping):
            payload = make_json_safe(dict(world))

            if not isinstance(payload, dict):
                payload = {"value": payload}
        else:
            raise WorldSerializationError(
                "World definition must be WorldDefinition or mapping.",
                details={"worldType": type(world).__name__},
            )

        if include_debug_metadata:
            payload.setdefault("debug", {})
            payload["debug"]["serializerVersion"] = WORLD_SERIALIZER_VERSION

        return _without_none(payload)

    except WorldSerializationError:
        raise
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize world definition.",
            details={"worldType": type(world).__name__},
            cause=exc,
        ) from exc


def serialize_world_metadata_response(
    world: WorldDefinition | Mapping[str, Any],
    *,
    include_palette: bool = DEFAULT_INCLUDE_WORLD_PALETTE,
    include_metadata: bool = True,
    include_raw_config: bool = False,
) -> dict[str, Any]:
    """
    Baut die spätere GET /worlds/<worldId>-Response.
    """
    payload = serialize_world_definition(
        world,
        include_palette=include_palette,
        include_metadata=include_metadata,
        include_raw_config=include_raw_config,
    )

    return _build_success_response(
        payload_key="world",
        payload=payload,
        response_version=WORLD_METADATA_RESPONSE_VERSION,
        metadata={
            "serializerVersion": WORLD_SERIALIZER_VERSION,
        },
    )


def serialize_world_provider_info(
    provider: WorldProviderInfo | Mapping[str, Any],
    *,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """
    Serialisiert WorldProviderInfo für Weltlisten.
    """
    try:
        if isinstance(provider, WorldProviderInfo):
            data = provider.to_dict(camel_case=True)
        elif isinstance(provider, Mapping):
            data = dict(provider)
        else:
            raise WorldSerializationError(
                "World provider info must be WorldProviderInfo or mapping.",
                details={"providerType": type(provider).__name__},
            )

        if not include_metadata:
            data.pop("metadata", None)

        return make_json_safe(data)

    except WorldSerializationError:
        raise
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize world provider info.",
            details={"providerType": type(provider).__name__},
            cause=exc,
        ) from exc


def serialize_world_list_result(
    result: WorldListResult | Mapping[str, Any] | Sequence[WorldProviderInfo],
    *,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """
    Serialisiert eine Weltliste.

    Gibt nur das Payload-Objekt zurück, nicht den äußeren ok-Wrapper.
    """
    try:
        if isinstance(result, WorldListResult):
            worlds = [
                serialize_world_provider_info(
                    world,
                    include_metadata=include_metadata,
                )
                for world in result.worlds
            ]

            payload: dict[str, Any] = {
                "worlds": worlds,
                "defaultWorldId": result.default_world_id,
            }

            if include_metadata:
                payload["metadata"] = _json_safe_dict(result.metadata)

        elif isinstance(result, Mapping):
            payload = make_json_safe(dict(result))

            if not isinstance(payload, dict):
                payload = {"value": payload}

        elif isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
            payload = {
                "worlds": [
                    serialize_world_provider_info(
                        item,
                        include_metadata=include_metadata,
                    )
                    for item in result
                ]
            }
        else:
            raise WorldSerializationError(
                "World list result must be WorldListResult, mapping or sequence.",
                details={"resultType": type(result).__name__},
            )

        return _without_none(payload)

    except WorldSerializationError:
        raise
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize world list result.",
            details={"resultType": type(result).__name__},
            cause=exc,
        ) from exc


def serialize_world_list_response(
    result: WorldListResult | Mapping[str, Any] | Sequence[WorldProviderInfo],
    *,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """
    Baut eine spätere GET /worlds-Response.
    """
    payload = serialize_world_list_result(
        result,
        include_metadata=include_metadata,
    )

    return _build_success_response(
        payload_key="result",
        payload=payload,
        response_version=WORLD_LIST_RESPONSE_VERSION,
        metadata={
            "serializerVersion": WORLD_SERIALIZER_VERSION,
        },
    )


# ---------------------------------------------------------------------------
# Chunk serialization
# ---------------------------------------------------------------------------

def serialize_chunk_request(
    request: ChunkRequest | Mapping[str, Any],
    *,
    include_metadata: bool = True,
) -> dict[str, Any]:
    """
    Serialisiert eine ChunkRequest.
    """
    try:
        if isinstance(request, ChunkRequest):
            payload = request.to_dict(camel_case=True)
        elif isinstance(request, Mapping):
            payload = dict(request)
        else:
            raise WorldSerializationError(
                "Chunk request must be ChunkRequest or mapping.",
                details={"requestType": type(request).__name__},
            )

        if not include_metadata:
            payload.pop("metadata", None)

        return make_json_safe(payload)

    except WorldSerializationError:
        raise
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize chunk request.",
            details={"requestType": type(request).__name__},
            cause=exc,
        ) from exc


def serialize_generated_chunk(
    chunk: GeneratedChunk | Mapping[str, Any],
    *,
    include_palette: bool = DEFAULT_INCLUDE_CHUNK_PALETTE,
    include_cells: bool = DEFAULT_INCLUDE_CELLS,
    include_metadata: bool = True,
    include_cell_encoding: bool = DEFAULT_INCLUDE_CELL_ENCODING,
    include_debug_metadata: bool = DEFAULT_INCLUDE_DEBUG_METADATA,
) -> dict[str, Any]:
    """
    Serialisiert einen GeneratedChunk in editor-kompatible Chunk-Daten.

    Diese Funktion erzeugt nur das chunk-Objekt, nicht den äußeren
    { "ok": true, "chunk": ... } Wrapper.

    Zentrale Felder für den Editor:
    - worldId
    - chunkX/chunkY/chunkZ
    - chunkKey
    - chunkSize
    - cellSize
    - palette
    - cells
    - cellIndexOrder
    - cellEncoding
    """
    try:
        if isinstance(chunk, GeneratedChunk):
            chunk.validate()

            payload: dict[str, Any] = {
                "worldId": chunk.world_id,
                "chunkX": chunk.chunk_x,
                "chunkY": chunk.chunk_y,
                "chunkZ": chunk.chunk_z,
                "chunkKey": chunk.chunk_key,
                "chunkSize": chunk.chunk_size,
                "cellSize": chunk.cell_size,
                "source": chunk.source,
                "schemaVersion": chunk.schema_version,
                "runtimeContentVersion": RUNTIME_CHUNK_CONTENT_VERSION,
                "coordinateSystem": chunk.coordinate_system,
                "projectionType": chunk.projection_type,
                "topologyType": chunk.topology_type,
                "generatorType": chunk.generator_type,
                "generatorVersion": chunk.generator_version,
                "blockRegistryId": chunk.block_registry_id,
                "blockRegistryVersion": chunk.block_registry_version,
                "cellIndexOrder": chunk.cell_index_order,
                "airCellValue": chunk.air_cell_value,
                "chunkVersion": chunk.chunk_version,
                "contentHash": chunk.content_hash,
            }

            if include_palette:
                payload["palette"] = serialize_palette(
                    chunk.palette,
                    include_metadata=include_metadata,
                    include_indices=True,
                )

            if include_cells:
                payload["cells"] = _normalize_cells(chunk.cells)
                payload["cellCount"] = len(chunk.cells)

            if include_cell_encoding:
                payload["cellEncoding"] = serialize_cell_encoding()

            if include_metadata:
                payload["metadata"] = _json_safe_dict(chunk.metadata)

        elif isinstance(chunk, Mapping):
            payload = make_json_safe(dict(chunk))

            if not isinstance(payload, dict):
                payload = {"value": payload}

            if include_cell_encoding:
                payload.setdefault("cellEncoding", serialize_cell_encoding())

            if "chunkKey" not in payload:
                chunk_x = payload.get("chunkX", payload.get("chunk_x"))
                chunk_y = payload.get("chunkY", payload.get("chunk_y"))
                chunk_z = payload.get("chunkZ", payload.get("chunk_z"))

                if chunk_x is not None and chunk_y is not None and chunk_z is not None:
                    payload["chunkKey"] = build_chunk_key(
                        _safe_int(chunk_x),
                        _safe_int(chunk_y),
                        _safe_int(chunk_z),
                    )

        else:
            raise WorldSerializationError(
                "Generated chunk must be GeneratedChunk or mapping.",
                details={"chunkType": type(chunk).__name__},
            )

        if include_debug_metadata:
            payload.setdefault("debug", {})
            payload["debug"]["serializerVersion"] = WORLD_SERIALIZER_VERSION

        return _without_none(payload)

    except WorldSerializationError:
        raise
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize generated chunk.",
            details={"chunkType": type(chunk).__name__},
            cause=exc,
        ) from exc


def serialize_chunk_response(
    chunk: GeneratedChunk | Mapping[str, Any],
    *,
    include_palette: bool = DEFAULT_INCLUDE_CHUNK_PALETTE,
    include_cells: bool = DEFAULT_INCLUDE_CELLS,
    include_metadata: bool = True,
    include_cell_encoding: bool = DEFAULT_INCLUDE_CELL_ENCODING,
) -> dict[str, Any]:
    """
    Baut die spätere GET /chunks-Response.
    """
    payload = serialize_generated_chunk(
        chunk,
        include_palette=include_palette,
        include_cells=include_cells,
        include_metadata=include_metadata,
        include_cell_encoding=include_cell_encoding,
    )

    return _build_success_response(
        payload_key="chunk",
        payload=payload,
        response_version=CHUNK_RESPONSE_VERSION,
        metadata={
            "serializerVersion": WORLD_SERIALIZER_VERSION,
        },
    )


def serialize_chunk_batch_result(
    batch_result: Any,
    *,
    include_chunks: bool = True,
    include_palette: bool = DEFAULT_INCLUDE_CHUNK_PALETTE,
    include_cells: bool = DEFAULT_INCLUDE_CELLS,
    include_metadata: bool = True,
    include_cell_encoding: bool = DEFAULT_INCLUDE_CELL_ENCODING,
) -> dict[str, Any]:
    """
    Serialisiert ein ChunkBatchResult-ähnliches Objekt.

    Diese Funktion importiert ChunkBatchResult absichtlich nicht direkt aus
    service.py, damit serializer.py keine harte Abhängigkeit auf service.py
    bekommt.

    Erwartete Attribute oder Keys:
    - world_id / worldId
    - chunks
    - requested_count / requestedCount
    - generated_count / generatedCount
    - metadata
    """
    try:
        if isinstance(batch_result, Mapping):
            source = dict(batch_result)
            world_id = source.get("worldId", source.get("world_id"))
            chunks = source.get("chunks", ())
            requested_count = source.get("requestedCount", source.get("requested_count", len(chunks or ())))
            generated_count = source.get("generatedCount", source.get("generated_count", len(chunks or ())))
            metadata = source.get("metadata", {})
        else:
            world_id = _extract_attr_or_key(batch_result, "world_id")
            chunks = _extract_attr_or_key(batch_result, "chunks", ())
            requested_count = _extract_attr_or_key(
                batch_result,
                "requested_count",
                len(chunks or ()),
            )
            generated_count = _extract_attr_or_key(
                batch_result,
                "generated_count",
                len(chunks or ()),
            )
            metadata = _extract_attr_or_key(batch_result, "metadata", {})

        if chunks is None:
            chunks = ()

        chunk_list = list(chunks)

        payload: dict[str, Any] = {
            "worldId": _safe_str(world_id),
            "requestedCount": _safe_int(requested_count, default=len(chunk_list)),
            "generatedCount": _safe_int(generated_count, default=len(chunk_list)),
            "chunkKeys": [
                _safe_str(getattr(chunk, "chunk_key", None), default="")
                or _safe_str(
                    _extract_attr_or_key(chunk, "chunkKey"),
                    default="",
                )
                for chunk in chunk_list
            ],
        }

        if include_chunks:
            payload["chunks"] = [
                serialize_generated_chunk(
                    chunk,
                    include_palette=include_palette,
                    include_cells=include_cells,
                    include_metadata=include_metadata,
                    include_cell_encoding=include_cell_encoding,
                )
                for chunk in chunk_list
            ]

        if include_metadata:
            payload["metadata"] = _json_safe_dict(metadata)

        return _without_none(payload)

    except WorldSerializationError:
        raise
    except Exception as exc:
        raise WorldSerializationError(
            "Could not serialize chunk batch result.",
            details={"batchResultType": type(batch_result).__name__},
            cause=exc,
        ) from exc


def serialize_chunk_batch_response(
    batch_result: Any,
    *,
    include_chunks: bool = True,
    include_palette: bool = DEFAULT_INCLUDE_CHUNK_PALETTE,
    include_cells: bool = DEFAULT_INCLUDE_CELLS,
    include_metadata: bool = True,
    include_cell_encoding: bool = DEFAULT_INCLUDE_CELL_ENCODING,
) -> dict[str, Any]:
    """
    Baut die spätere POST /chunks/batch-Response.
    """
    payload = serialize_chunk_batch_result(
        batch_result,
        include_chunks=include_chunks,
        include_palette=include_palette,
        include_cells=include_cells,
        include_metadata=include_metadata,
        include_cell_encoding=include_cell_encoding,
    )

    return _build_success_response(
        payload_key="result",
        payload=payload,
        response_version=CHUNK_BATCH_RESPONSE_VERSION,
        metadata={
            "serializerVersion": WORLD_SERIALIZER_VERSION,
        },
    )


# ---------------------------------------------------------------------------
# Generic response helpers
# ---------------------------------------------------------------------------

def serialize_success_response(
    payload: Mapping[str, Any] | None = None,
    *,
    response_version: str = "generic-success-response.v1",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Baut eine generische erfolgreiche Response.

    Diese Funktion ist für Diagnose-/Hilfsrouten gedacht, nicht zwingend
    für die finalen Chunk-Routen.
    """
    response: dict[str, Any] = {
        "ok": True,
        "responseVersion": response_version,
    }

    if payload:
        response.update(make_json_safe(dict(payload)))

    if metadata:
        response["metadata"] = _json_safe_dict(metadata)

    return response


def serialize_error_response(
    error: BaseException,
    *,
    include_status_code: bool = False,
    include_debug: bool = False,
) -> dict[str, Any]:
    """
    Serialisiert eine Exception in eine API-nahe Fehlerantwort.

    Diese Funktion ist framework-neutral.
    Die Route kann daraus später Body + HTTP-Status ableiten.
    """
    try:
        world_error = coerce_world_error(error)

        response: dict[str, Any] = {
            "ok": False,
            "error": world_error.to_dict(
                include_status_code=include_status_code,
            ),
        }

        if include_debug:
            response["debug"] = {
                "serializerVersion": WORLD_SERIALIZER_VERSION,
                "exceptionType": type(error).__name__,
                "log": world_error.to_log_dict(),
            }

        return make_json_safe(response)

    except Exception:
        return {
            "ok": False,
            "error": {
                "code": "error_serialization_failed",
                "message": "Could not serialize error response.",
                "details": {
                    "originalErrorType": type(error).__name__,
                    "originalErrorMessage": _safe_str(error),
                },
            },
        }


def get_error_status_code(error: BaseException, *, default: int = 500) -> int:
    """
    Ermittelt den HTTP-Statuscode aus einem WorldError.

    Diese Funktion setzt selbst keinen HTTP-Status, sondern gibt nur den
    passenden Wert für spätere Routes zurück.
    """
    try:
        if isinstance(error, WorldError):
            return int(error.http_status_code)

        coerced = coerce_world_error(error)
        return int(coerced.http_status_code)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# API aliases for readability
# ---------------------------------------------------------------------------

def to_world_response(
    world: WorldDefinition | Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Alias für serialize_world_metadata_response.
    """
    return serialize_world_metadata_response(world, **kwargs)


def to_chunk_response(
    chunk: GeneratedChunk | Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Alias für serialize_chunk_response.
    """
    return serialize_chunk_response(chunk, **kwargs)


def to_chunk_batch_response(
    batch_result: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Alias für serialize_chunk_batch_response.
    """
    return serialize_chunk_batch_response(batch_result, **kwargs)


__all__ = (
    "WORLD_SERIALIZER_VERSION",
    "CELL_ENCODING_VERSION",
    "RUNTIME_CHUNK_CONTENT_VERSION",
    "WORLD_METADATA_RESPONSE_VERSION",
    "WORLD_LIST_RESPONSE_VERSION",
    "CHUNK_RESPONSE_VERSION",
    "CHUNK_BATCH_RESPONSE_VERSION",
    "serialize_cell_encoding",
    "serialize_palette_entry",
    "serialize_palette",
    "serialize_world_definition",
    "serialize_world_metadata_response",
    "serialize_world_provider_info",
    "serialize_world_list_result",
    "serialize_world_list_response",
    "serialize_chunk_request",
    "serialize_generated_chunk",
    "serialize_chunk_response",
    "serialize_chunk_batch_result",
    "serialize_chunk_batch_response",
    "serialize_success_response",
    "serialize_error_response",
    "get_error_status_code",
    "to_world_response",
    "to_chunk_response",
    "to_chunk_batch_response",
)