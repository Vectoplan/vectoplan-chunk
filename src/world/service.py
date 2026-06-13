# src/world/service.py
"""
VECTOPLAN World Service.

Diese Datei ist die zentrale Fassade der neutralen World-Schicht.

Aufgabe:
- WorldLoader kapseln
- Welt-Metadaten bereitstellen
- registrierte Welten auflisten
- konkrete Weltdefinitionen laden
- Chunk-Anfragen normalisieren
- Provider-Generatoren aufrufen
- GeneratedChunk-Ergebnisse validieren
- Batch-Chunk-Erzeugung vorbereiten
- keine Flask-Abhängigkeit erzeugen
- keine Datenbank verwenden
- keine konkrete Flat-World-Logik enthalten

Spätere Routes sollen möglichst nur mit dieser Datei sprechen.

Beispiel für spätere Routes:

    GET /worlds/flat
        → WorldService.get_world_metadata("flat")

    GET /chunks?worldId=flat&chunkX=0&chunkY=0&chunkZ=0
        → WorldService.generate_chunk("flat", 0, 0, 0)

    POST /chunks/batch
        → WorldService.generate_chunk_batch(...)

Wichtig:
- Diese Datei generiert selbst keine flache Welt.
- Die konkrete Generierung passiert später in src/world/flat/generator.py.
- Der Service ruft nur die Provider-Funktion generate_chunk(...) auf.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from threading import RLock
from typing import Any, Final

try:
    from src.world.errors import (
        InvalidChunkRequestError,
        WorldGenerationError,
        WorldProviderContractError,
        WorldServiceError,
        WorldValidationError,
        coerce_world_error,
        make_json_safe,
    )
except ImportError:
    from src.world.errors import (  # type: ignore[no-redef]
        InvalidChunkRequestError,
        WorldGenerationError,
        WorldProviderContractError,
        WorldValidationError,
        coerce_world_error,
        make_json_safe,
    )

    class WorldServiceError(Exception):  # type: ignore[no-redef]
        """
        Fallback, falls WorldServiceError in errors.py noch nicht existiert.
        """

        def __init__(
            self,
            message: str,
            *,
            details: Mapping[str, Any] | None = None,
            cause: BaseException | None = None,
        ) -> None:
            self.message = message
            self.details = dict(details or {})
            self.cause = cause
            super().__init__(message)

try:
    from src.world.loader import (
        PROVIDER_FUNCTION_GENERATE_CHUNK,
        LoadedWorld,
        WorldLoader,
        get_default_world_loader,
    )
    from src.world.models import (
        ChunkBatchRequest,
        ChunkRequest,
        GeneratedChunk,
        WorldDefinition,
        WorldListResult,
        WorldProviderInfo,
    )
except Exception as exc:  # pragma: no cover - defensive bootstrap guard
    raise RuntimeError(
        "src.world.service requires src.world.loader and src.world.models "
        "to be importable before the service can be used."
    ) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORLD_SERVICE_VERSION: Final[str] = "0.1.0"

DEFAULT_MAX_BATCH_SIZE: Final[int] = 256
ABSOLUTE_MAX_BATCH_SIZE: Final[int] = 4096

GENERATION_SOURCE_PROVIDER: Final[str] = "provider"
GENERATION_SOURCE_SERVICE: Final[str] = "world_service"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt einen beliebigen Wert defensiv in einen bereinigten String um.
    """
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


def _to_int(
    value: Any,
    *,
    field_name: str,
    default: int | None = None,
) -> int:
    """
    Wandelt einen Wert robust in int um.
    """
    if value is None:
        if default is not None:
            return default

        raise InvalidChunkRequestError(
            f"Required integer field '{field_name}' is missing.",
            details={"field": field_name},
        )

    try:
        return int(value)
    except Exception as exc:
        raise InvalidChunkRequestError(
            f"Field '{field_name}' must be an integer.",
            details={"field": field_name, "value": make_json_safe(value)},
        ) from exc


def _normalize_metadata(value: Any) -> dict[str, Any]:
    """
    Normalisiert beliebige Metadaten in ein JSON-sicheres Dictionary.
    """
    if value is None:
        return {}

    safe = make_json_safe(value)

    if isinstance(safe, Mapping):
        return dict(safe)

    return {"value": safe}


def _normalize_chunk_request_item(
    item: ChunkRequest | Mapping[str, Any],
    *,
    default_world_id: str,
) -> ChunkRequest:
    """
    Normalisiert einen einzelnen ChunkRequest-Eintrag.

    Erlaubt:
    - ChunkRequest
    - Mapping mit worldId/chunkX/chunkY/chunkZ
    - Mapping ohne worldId, wenn default_world_id vorhanden ist
    """
    if isinstance(item, ChunkRequest):
        return item

    if not isinstance(item, Mapping):
        raise InvalidChunkRequestError(
            "Chunk request item must be ChunkRequest or object.",
            details={"value": make_json_safe(item)},
        )

    raw = dict(item)
    raw.setdefault("worldId", default_world_id)

    return ChunkRequest.from_dict(raw)


def _ensure_generated_chunk(value: Any, *, world_id: str, chunk_key: str) -> GeneratedChunk:
    """
    Normalisiert ein Provider-Ergebnis zu GeneratedChunk.

    Provider sollen idealerweise direkt GeneratedChunk zurückgeben.
    Für spätere Flexibilität wird zusätzlich ein Mapping akzeptiert, falls
    ein Provider zuerst JSON-nahe Daten erzeugt.

    Mapping-Fallback ist bewusst streng:
    - aktuell wird kein beliebiges Chunk-Dict rekonstruiert
    - falls Provider kein GeneratedChunk zurückgeben, wird ein klarer
      Contract-Fehler erzeugt

    Dadurch bleibt Phase 1 stabil und vermeidet verdeckte Formatdrift.
    """
    if isinstance(value, GeneratedChunk):
        value.validate()
        return value

    raise WorldProviderContractError(
        "World provider generate_chunk must return GeneratedChunk.",
        details={
            "worldId": world_id,
            "chunkKey": chunk_key,
            "returnType": type(value).__name__,
            "returnValue": make_json_safe(value),
        },
    )


def _call_generate_chunk_provider(
    loaded_world: LoadedWorld,
    request: ChunkRequest,
) -> GeneratedChunk:
    """
    Ruft die generate_chunk-Funktion eines Providers robust auf.

    Erwarteter Provider-Vertrag:

        generate_chunk(world: WorldDefinition, request: ChunkRequest) -> GeneratedChunk

    Der Service unterstützt zusätzlich einen defensiven Fallback für Provider,
    die versehentlich nur eine der folgenden Signaturen anbieten:

        generate_chunk(loaded_world, request)
        generate_chunk(world, chunk_x, chunk_y, chunk_z)

    Der bevorzugte und dokumentierte Vertrag bleibt aber:
        generate_chunk(world, request)
    """
    generate_chunk = loaded_world.get_provider_function(
        PROVIDER_FUNCTION_GENERATE_CHUNK,
        required=True,
    )

    if generate_chunk is None:
        raise WorldProviderContractError(
            "World provider does not expose generate_chunk.",
            details={
                "worldId": loaded_world.world_id,
                "providerId": loaded_world.provider_id,
                "providerModule": loaded_world.provider_module_name,
            },
        )

    attempts: list[dict[str, Any]] = []

    call_patterns: tuple[tuple[str, tuple[Any, ...]], ...] = (
        ("world_definition_and_request", (loaded_world.definition, request)),
        ("loaded_world_and_request", (loaded_world, request)),
        (
            "world_definition_and_coordinates",
            (
                loaded_world.definition,
                request.chunk_x,
                request.chunk_y,
                request.chunk_z,
            ),
        ),
    )

    for pattern_name, args in call_patterns:
        try:
            result = generate_chunk(*args)
            return _ensure_generated_chunk(
                result,
                world_id=request.world_id,
                chunk_key=request.chunk_key,
            )
        except TypeError as exc:
            attempts.append(
                {
                    "pattern": pattern_name,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                }
            )
            continue
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="World provider failed to generate chunk.",
                fallback_code="world_provider_generate_chunk_failed",
                fallback_status_code=500,
                details={
                    "worldId": request.world_id,
                    "chunkKey": request.chunk_key,
                    "providerId": loaded_world.provider_id,
                    "providerModule": loaded_world.provider_module_name,
                    "pattern": pattern_name,
                },
            )
            raise world_error from exc

    raise WorldProviderContractError(
        "World provider generate_chunk could not be called with supported signatures.",
        details={
            "worldId": request.world_id,
            "chunkKey": request.chunk_key,
            "providerId": loaded_world.provider_id,
            "providerModule": loaded_world.provider_module_name,
            "supportedSignatures": [
                "generate_chunk(world: WorldDefinition, request: ChunkRequest)",
                "generate_chunk(loaded_world: LoadedWorld, request: ChunkRequest)",
                "generate_chunk(world: WorldDefinition, chunk_x: int, chunk_y: int, chunk_z: int)",
            ],
            "attempts": attempts,
        },
    )


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ChunkBatchResult:
    """
    Ergebnis einer Batch-Chunk-Erzeugung.

    Diese Struktur ist noch keine API-Response. Sie ist eine interne,
    serializer-freundliche Ergebnisstruktur.
    """

    world_id: str
    chunks: tuple[GeneratedChunk, ...]
    requested_count: int
    generated_count: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chunk_keys(self) -> tuple[str, ...]:
        """
        Gibt alle generierten Chunk-Keys zurück.
        """
        return tuple(chunk.chunk_key for chunk in self.chunks)

    def to_dict(
        self,
        *,
        include_chunks: bool = True,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        """
        Serialisiert das Batch-Ergebnis als JSON-nahe Struktur.
        """
        data: dict[str, Any] = {
            "worldId": self.world_id,
            "requestedCount": self.requested_count,
            "generatedCount": self.generated_count,
            "chunkKeys": list(self.chunk_keys),
        }

        if include_chunks:
            data["chunks"] = [
                chunk.to_dict(camel_case=True)
                for chunk in self.chunks
            ]

        if include_metadata:
            data["metadata"] = self.metadata

        return data


@dataclass(frozen=True, slots=True)
class WorldServiceStatus:
    """
    Diagnosezustand des WorldService.
    """

    service_version: str
    max_batch_size: int
    loader_status: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialisiert den Service-Status.
        """
        return {
            "serviceVersion": self.service_version,
            "maxBatchSize": self.max_batch_size,
            "loaderStatus": self.loader_status,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# WorldService
# ---------------------------------------------------------------------------

class WorldService:
    """
    Zentrale Fassade der World-Schicht.

    Diese Klasse ist der bevorzugte Einstiegspunkt für spätere Routes,
    Tests und andere interne Services.
    """

    def __init__(
        self,
        *,
        loader: WorldLoader | None = None,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    ) -> None:
        self._loader = loader or get_default_world_loader()
        self._max_batch_size = self._normalize_max_batch_size(max_batch_size)
        self._lock = RLock()

    @property
    def loader(self) -> WorldLoader:
        """
        Gibt den verwendeten WorldLoader zurück.
        """
        return self._loader

    @property
    def max_batch_size(self) -> int:
        """
        Maximale Anzahl an Chunks pro Batch.
        """
        return self._max_batch_size

    @staticmethod
    def _normalize_max_batch_size(value: Any) -> int:
        """
        Normalisiert und begrenzt max_batch_size.
        """
        try:
            converted = int(value)
        except Exception as exc:
            raise WorldServiceError(
                "max_batch_size must be an integer.",
                details={"maxBatchSize": make_json_safe(value)},
                cause=exc,
            ) from exc

        if converted < 1:
            raise WorldServiceError(
                "max_batch_size must be >= 1.",
                details={"maxBatchSize": converted},
            )

        if converted > ABSOLUTE_MAX_BATCH_SIZE:
            raise WorldServiceError(
                "max_batch_size exceeds absolute limit.",
                details={
                    "maxBatchSize": converted,
                    "absoluteMaxBatchSize": ABSOLUTE_MAX_BATCH_SIZE,
                },
            )

        return converted

    def clear_cache(self) -> None:
        """
        Leert den Cache des darunterliegenden Loaders.
        """
        self.loader.clear_cache()

    def get_status(self) -> WorldServiceStatus:
        """
        Gibt einen Diagnosezustand zurück.
        """
        return WorldServiceStatus(
            service_version=WORLD_SERVICE_VERSION,
            max_batch_size=self.max_batch_size,
            loader_status=self.loader.get_status().to_dict(),
            metadata={
                "generationProviderFunction": PROVIDER_FUNCTION_GENERATE_CHUNK,
            },
        )

    # ---------------------------------------------------------------------
    # World metadata
    # ---------------------------------------------------------------------

    def list_worlds(self) -> WorldListResult:
        """
        Listet verfügbare Welten.

        Diese Methode lädt keine world.json-Dateien und generiert keine Chunks.
        Sie liest nur die Registry-Informationen.
        """
        try:
            return self.loader.registry.list_worlds(include_disabled=False)
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Could not list worlds.",
                fallback_code="world_list_failed",
                fallback_status_code=500,
            )
            raise world_error from exc

    def list_provider_info(self) -> tuple[WorldProviderInfo, ...]:
        """
        Gibt alle öffentlichen Provider-Infos zurück.
        """
        return self.list_worlds().worlds

    def has_world(self, world_id: str) -> bool:
        """
        Prüft, ob eine Welt registriert ist.
        """
        try:
            return self.loader.has_world(world_id)
        except Exception:
            return False

    def load_world(
        self,
        world_id: str | None = None,
        *,
        force_reload: bool = False,
    ) -> LoadedWorld:
        """
        Lädt eine konkrete Welt.

        Das Ergebnis enthält Provider-Modul, raw_config und WorldDefinition.
        """
        try:
            return self.loader.load_world(
                world_id,
                force_reload=force_reload,
            )
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Could not load world.",
                fallback_code="world_load_failed",
                fallback_status_code=500,
                details={"worldId": world_id},
            )
            raise world_error from exc

    def get_world_definition(
        self,
        world_id: str | None = None,
        *,
        force_reload: bool = False,
    ) -> WorldDefinition:
        """
        Gibt die normalisierte WorldDefinition zurück.
        """
        return self.load_world(
            world_id,
            force_reload=force_reload,
        ).definition

    def get_world_metadata(
        self,
        world_id: str | None = None,
        *,
        force_reload: bool = False,
        include_palette: bool = True,
    ) -> dict[str, Any]:
        """
        Gibt JSON-nahe Welt-Metadaten zurück.

        Diese Methode ist für die spätere /worlds/<worldId>-Route vorbereitet.
        Für die finale API-Form kann später serializer.py verwendet werden.
        """
        definition = self.get_world_definition(
            world_id,
            force_reload=force_reload,
        )

        return definition.to_dict(
            camel_case=True,
            include_palette=include_palette,
            include_raw_config=False,
        )

    # ---------------------------------------------------------------------
    # Chunk generation
    # ---------------------------------------------------------------------

    def normalize_chunk_request(
        self,
        world_id: str,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        *,
        request_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ChunkRequest:
        """
        Normalisiert einzelne Chunk-Parameter zu einer ChunkRequest.
        """
        return ChunkRequest.create(
            world_id=world_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            request_id=request_id,
            metadata=metadata,
        )

    def generate_chunk(
        self,
        world_id: str | None,
        chunk_x: int,
        chunk_y: int,
        chunk_z: int,
        *,
        request_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        force_reload_world: bool = False,
    ) -> GeneratedChunk:
        """
        Generiert einen einzelnen Chunk über den passenden World-Provider.

        Diese Methode:
        - normalisiert die Anfrage
        - lädt die Welt
        - ruft Provider generate_chunk(...) auf
        - validiert das Ergebnis
        """
        if not world_id:
            loaded_world = self.load_world(
                None,
                force_reload=force_reload_world,
            )
            resolved_world_id = loaded_world.definition.world_id
        else:
            loaded_world = self.load_world(
                world_id,
                force_reload=force_reload_world,
            )
            resolved_world_id = loaded_world.definition.world_id

        request = ChunkRequest.create(
            world_id=resolved_world_id,
            chunk_x=chunk_x,
            chunk_y=chunk_y,
            chunk_z=chunk_z,
            request_id=request_id,
            metadata=metadata,
        )

        return self.generate_chunk_from_request(
            request,
            loaded_world=loaded_world,
        )

    def generate_chunk_from_dict(
        self,
        raw_request: Mapping[str, Any],
        *,
        force_reload_world: bool = False,
    ) -> GeneratedChunk:
        """
        Generiert einen Chunk aus einem JSON-/Query-nahen Request-Dict.
        """
        try:
            request = ChunkRequest.from_dict(raw_request)
        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Invalid chunk request.",
                fallback_code="invalid_chunk_request",
                fallback_status_code=400,
                details={"request": make_json_safe(raw_request)},
            )
            raise world_error from exc

        loaded_world = self.load_world(
            request.world_id,
            force_reload=force_reload_world,
        )

        return self.generate_chunk_from_request(
            request,
            loaded_world=loaded_world,
        )

    def generate_chunk_from_request(
        self,
        request: ChunkRequest,
        *,
        loaded_world: LoadedWorld | None = None,
    ) -> GeneratedChunk:
        """
        Generiert einen Chunk aus einer bereits normalisierten ChunkRequest.
        """
        if not isinstance(request, ChunkRequest):
            raise InvalidChunkRequestError(
                "generate_chunk_from_request requires ChunkRequest.",
                details={"requestType": type(request).__name__},
            )

        try:
            active_loaded_world = loaded_world or self.load_world(request.world_id)

            if request.world_id != active_loaded_world.definition.world_id:
                # Alias-Fall erlauben:
                # request.world_id kann "default" sein, Definition aber "flat".
                # Wenn request.world_id kein registrierter Alias der geladenen Welt ist,
                # wirft die Registry/Loader-Schicht vorher.
                request = ChunkRequest.create(
                    world_id=active_loaded_world.definition.world_id,
                    chunk_x=request.chunk_x,
                    chunk_y=request.chunk_y,
                    chunk_z=request.chunk_z,
                    request_id=request.request_id,
                    include_metadata=request.include_metadata,
                    metadata=request.metadata,
                )

            chunk = _call_generate_chunk_provider(
                active_loaded_world,
                request,
            )

            self.validate_generated_chunk_against_request(
                chunk,
                request,
                active_loaded_world.definition,
            )

            return chunk

        except Exception as exc:
            world_error = coerce_world_error(
                exc,
                fallback_message="Chunk generation failed.",
                fallback_code="chunk_generation_failed",
                fallback_status_code=500,
                details={
                    "worldId": request.world_id,
                    "chunkKey": request.chunk_key,
                    "chunkX": request.chunk_x,
                    "chunkY": request.chunk_y,
                    "chunkZ": request.chunk_z,
                },
            )
            raise world_error from exc

    def validate_generated_chunk_against_request(
        self,
        chunk: GeneratedChunk,
        request: ChunkRequest,
        world: WorldDefinition,
    ) -> None:
        """
        Prüft, ob ein GeneratedChunk zur Anfrage und Weltdefinition passt.
        """
        errors: list[dict[str, Any]] = []

        if chunk.world_id != world.world_id:
            errors.append(
                {
                    "code": "chunk_world_mismatch",
                    "chunkWorldId": chunk.world_id,
                    "worldId": world.world_id,
                }
            )

        if chunk.world_id != request.world_id:
            errors.append(
                {
                    "code": "chunk_request_world_mismatch",
                    "chunkWorldId": chunk.world_id,
                    "requestWorldId": request.world_id,
                }
            )

        if chunk.chunk_x != request.chunk_x:
            errors.append(
                {
                    "code": "chunk_x_mismatch",
                    "chunkX": chunk.chunk_x,
                    "requestChunkX": request.chunk_x,
                }
            )

        if chunk.chunk_y != request.chunk_y:
            errors.append(
                {
                    "code": "chunk_y_mismatch",
                    "chunkY": chunk.chunk_y,
                    "requestChunkY": request.chunk_y,
                }
            )

        if chunk.chunk_z != request.chunk_z:
            errors.append(
                {
                    "code": "chunk_z_mismatch",
                    "chunkZ": chunk.chunk_z,
                    "requestChunkZ": request.chunk_z,
                }
            )

        if chunk.chunk_size != world.chunk_size:
            errors.append(
                {
                    "code": "chunk_size_mismatch",
                    "chunkSize": chunk.chunk_size,
                    "worldChunkSize": world.chunk_size,
                }
            )

        if chunk.cell_size != world.cell_size:
            errors.append(
                {
                    "code": "cell_size_mismatch",
                    "cellSize": chunk.cell_size,
                    "worldCellSize": world.cell_size,
                }
            )

        if chunk.generator_type != world.generator_type:
            errors.append(
                {
                    "code": "generator_type_mismatch",
                    "chunkGeneratorType": chunk.generator_type,
                    "worldGeneratorType": world.generator_type,
                }
            )

        if errors:
            raise WorldGenerationError(
                "Generated chunk does not match request or world definition.",
                details={
                    "worldId": world.world_id,
                    "chunkKey": request.chunk_key,
                    "errors": errors,
                },
            )

        try:
            chunk.validate()
        except Exception as exc:
            raise WorldGenerationError(
                "Generated chunk failed structural validation.",
                details={
                    "worldId": world.world_id,
                    "chunkKey": request.chunk_key,
                },
                cause=exc,
            ) from exc

    # ---------------------------------------------------------------------
    # Batch generation
    # ---------------------------------------------------------------------

    def normalize_batch_request(
        self,
        raw: Mapping[str, Any] | ChunkBatchRequest,
    ) -> ChunkBatchRequest:
        """
        Normalisiert eine Batch-Anfrage.
        """
        if isinstance(raw, ChunkBatchRequest):
            raw.validate()
            self.validate_batch_size(len(raw.chunks))
            return raw

        if not isinstance(raw, Mapping):
            raise InvalidChunkRequestError(
                "Batch request must be ChunkBatchRequest or object.",
                details={"request": make_json_safe(raw)},
            )

        batch = ChunkBatchRequest.from_dict(raw)
        self.validate_batch_size(len(batch.chunks))
        return batch

    def validate_batch_size(self, size: int) -> None:
        """
        Prüft die Batch-Größe.
        """
        converted = _to_int(size, field_name="batchSize")

        if converted < 1:
            raise InvalidChunkRequestError(
                "Chunk batch must contain at least one chunk.",
                details={"batchSize": converted},
            )

        if converted > self.max_batch_size:
            raise InvalidChunkRequestError(
                "Chunk batch exceeds service limit.",
                details={
                    "batchSize": converted,
                    "maxBatchSize": self.max_batch_size,
                },
            )

    def generate_chunk_batch(
        self,
        batch: ChunkBatchRequest | Mapping[str, Any],
        *,
        force_reload_world: bool = False,
    ) -> ChunkBatchResult:
        """
        Generiert mehrere Chunks für dieselbe Welt.

        Die Welt wird einmal geladen und für alle Chunk-Anfragen verwendet.
        """
        normalized_batch = self.normalize_batch_request(batch)

        loaded_world = self.load_world(
            normalized_batch.world_id,
            force_reload=force_reload_world,
        )

        chunks: list[GeneratedChunk] = []

        for request in normalized_batch.chunks:
            if request.world_id != loaded_world.definition.world_id:
                request = ChunkRequest.create(
                    world_id=loaded_world.definition.world_id,
                    chunk_x=request.chunk_x,
                    chunk_y=request.chunk_y,
                    chunk_z=request.chunk_z,
                    request_id=request.request_id,
                    include_metadata=request.include_metadata,
                    metadata=request.metadata,
                )

            chunk = self.generate_chunk_from_request(
                request,
                loaded_world=loaded_world,
            )
            chunks.append(chunk)

        return ChunkBatchResult(
            world_id=loaded_world.definition.world_id,
            chunks=tuple(chunks),
            requested_count=len(normalized_batch.chunks),
            generated_count=len(chunks),
            metadata={
                "serviceVersion": WORLD_SERVICE_VERSION,
                "providerId": loaded_world.provider_id,
                "providerModule": loaded_world.provider_module_name,
                "source": GENERATION_SOURCE_PROVIDER,
            },
        )

    def generate_chunks(
        self,
        world_id: str,
        chunks: Iterable[ChunkRequest | Mapping[str, Any]],
        *,
        request_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        force_reload_world: bool = False,
    ) -> ChunkBatchResult:
        """
        Komfortmethode für Batch-Erzeugung mit separaten Parametern.

        Beispiel:
            service.generate_chunks(
                "flat",
                [
                    {"chunkX": 0, "chunkY": 0, "chunkZ": 0},
                    {"chunkX": 1, "chunkY": 0, "chunkZ": 0},
                ],
            )
        """
        chunk_requests = [
            _normalize_chunk_request_item(item, default_world_id=world_id)
            for item in chunks
        ]

        batch = ChunkBatchRequest(
            world_id=world_id,
            chunks=tuple(chunk_requests),
            request_id=request_id,
            metadata=_normalize_metadata(metadata),
        )

        batch.validate()
        self.validate_batch_size(len(batch.chunks))

        return self.generate_chunk_batch(
            batch,
            force_reload_world=force_reload_world,
        )

    # ---------------------------------------------------------------------
    # Convenience serialization-neutral helpers
    # ---------------------------------------------------------------------

    def get_world_runtime_info(
        self,
        world_id: str | None = None,
        *,
        force_reload: bool = False,
    ) -> dict[str, Any]:
        """
        Gibt eine kompakte runtime-nahe Weltbeschreibung zurück.

        Diese Methode ist bewusst nicht die finale API-Serialisierung.
        serializer.py kann daraus später eine exakte Response bauen.
        """
        loaded_world = self.load_world(
            world_id,
            force_reload=force_reload,
        )

        definition = loaded_world.definition

        return {
            "worldId": definition.world_id,
            "worldType": definition.world_type,
            "label": definition.label,
            "chunkSize": definition.chunk_size,
            "cellSize": definition.cell_size,
            "coordinateSystem": definition.coordinate_system,
            "projectionType": definition.projection_type,
            "topologyType": definition.topology_type,
            "generatorType": definition.generator_type,
            "generatorVersion": definition.generator_version,
            "surfaceY": definition.surface_y,
            "minY": definition.min_y,
            "maxY": definition.max_y,
            "blockRegistryId": definition.block_registry_id,
            "blockRegistryVersion": definition.block_registry_version,
            "palette": [
                entry.to_dict(camel_case=True)
                for entry in definition.palette
            ],
            "provider": {
                "providerId": loaded_world.provider_id,
                "providerModule": loaded_world.provider_module_name,
                "configPath": loaded_world.config_path,
            },
        }


# ---------------------------------------------------------------------------
# Default service factory
# ---------------------------------------------------------------------------

def create_default_world_service(
    *,
    loader: WorldLoader | None = None,
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
) -> WorldService:
    """
    Erstellt eine neue WorldService-Instanz.
    """
    return WorldService(
        loader=loader or get_default_world_loader(),
        max_batch_size=max_batch_size,
    )


@lru_cache(maxsize=1)
def get_default_world_service() -> WorldService:
    """
    Gibt den pro Prozess gecachten Default-WorldService zurück.

    Cache leeren:

        get_default_world_service.cache_clear()

    oder:

        reset_default_world_service_cache()
    """
    return create_default_world_service()


def reset_default_world_service_cache() -> None:
    """
    Leert den Default-Service-Cache und den darunterliegenden Loader-Cache.
    """
    try:
        service = get_default_world_service()
        service.clear_cache()
    except Exception:
        pass

    get_default_world_service.cache_clear()


# ---------------------------------------------------------------------------
# Module-level convenience helpers
# ---------------------------------------------------------------------------

def list_worlds(
    *,
    service: WorldService | None = None,
) -> WorldListResult:
    """
    Komfortfunktion zum Auflisten verfügbarer Welten.
    """
    active_service = service or get_default_world_service()
    return active_service.list_worlds()


def get_world_metadata(
    world_id: str | None = None,
    *,
    service: WorldService | None = None,
    force_reload: bool = False,
    include_palette: bool = True,
) -> dict[str, Any]:
    """
    Komfortfunktion für Welt-Metadaten.
    """
    active_service = service or get_default_world_service()

    return active_service.get_world_metadata(
        world_id,
        force_reload=force_reload,
        include_palette=include_palette,
    )


def generate_chunk(
    world_id: str | None,
    chunk_x: int,
    chunk_y: int,
    chunk_z: int,
    *,
    service: WorldService | None = None,
    request_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    force_reload_world: bool = False,
) -> GeneratedChunk:
    """
    Komfortfunktion zur Erzeugung eines einzelnen Chunks.
    """
    active_service = service or get_default_world_service()

    return active_service.generate_chunk(
        world_id,
        chunk_x,
        chunk_y,
        chunk_z,
        request_id=request_id,
        metadata=metadata,
        force_reload_world=force_reload_world,
    )


def generate_chunk_batch(
    batch: ChunkBatchRequest | Mapping[str, Any],
    *,
    service: WorldService | None = None,
    force_reload_world: bool = False,
) -> ChunkBatchResult:
    """
    Komfortfunktion zur Batch-Chunk-Erzeugung.
    """
    active_service = service or get_default_world_service()

    return active_service.generate_chunk_batch(
        batch,
        force_reload_world=force_reload_world,
    )


__all__ = (
    "WORLD_SERVICE_VERSION",
    "DEFAULT_MAX_BATCH_SIZE",
    "ABSOLUTE_MAX_BATCH_SIZE",
    "ChunkBatchResult",
    "WorldServiceStatus",
    "WorldService",
    "create_default_world_service",
    "get_default_world_service",
    "reset_default_world_service_cache",
    "list_worlds",
    "get_world_metadata",
    "generate_chunk",
    "generate_chunk_batch",
)