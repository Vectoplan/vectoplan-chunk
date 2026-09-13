"""Safe runtime consumer for immutable ``vectoplan-editor-dataset.v1`` bundles.

The dataset is a response-time projection, not a second world database.  It
feeds its roofs through the existing semantic ``objectRefs`` path and its
streets through ``geodata-overlays.v1``.  Wall cells deliberately remain in
the canonical LoD2 import/WorldEdit command path; projecting them as writable
snapshot cells would make an edit appear successful without persisting it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from threading import RLock
from typing import Any
from uuid import uuid4

from .contracts import chunk_coordinates, content_fingerprint
from .pipeline import EDITOR_DATASET_SCHEMA_VERSION, validate_editor_dataset


SELECTOR_SCHEMA_VERSION = "vectoplan-editor-dataset-selection.v1"
ACTIVE_RESPONSE_VERSION = "vectoplan-editor-dataset-active-response.v1"
# Reuse the already persistent Chunk runtime volume.  Existing bundles such as
# ``terrain-cache/editor-datasets/test1-...`` can be activated in place.
DEFAULT_DATASET_ROOT = Path("/var/lib/vectoplan-chunk/terrain-cache/editor-datasets")
DATASET_ROOT_ENV = "VECTOPLAN_CHUNK_EDITOR_DATASET_ROOT"
MAX_SELECTOR_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024 * 1024
_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,191}$")


class EditorDatasetRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ActiveEditorDataset:
    root: Path
    bundle_path: Path
    selector_path: Path
    selector: dict[str, Any]
    manifest: dict[str, Any]
    chunk_fingerprints: dict[str, str]
    roofs_by_id: dict[str, dict[str, Any]]

    @property
    def dataset_id(self) -> str:
        return str(self.manifest["datasetId"])

    @property
    def content_fingerprint(self) -> str:
        return str(self.manifest["contentFingerprint"])

    @property
    def reference_fingerprint(self) -> str:
        return str(self.manifest["referenceFingerprint"])

    def public_summary(self) -> dict[str, Any]:
        layers = self.manifest.get("layers")
        layer_counts: dict[str, int] = {}
        if isinstance(layers, Mapping):
            for name, value in layers.items():
                if isinstance(value, Mapping) and isinstance(value.get("items"), list):
                    layer_counts[str(name)] = len(value["items"])
        return {
            "schemaVersion": ACTIVE_RESPONSE_VERSION,
            "datasetSchemaVersion": EDITOR_DATASET_SCHEMA_VERSION,
            "datasetId": self.dataset_id,
            "contentFingerprint": self.content_fingerprint,
            "referenceFingerprint": self.reference_fingerprint,
            "pipelineVersion": self.manifest.get("pipelineVersion"),
            "coordinateFrame": deepcopy(self.manifest.get("coordinateFrame")),
            "sourceBounds": deepcopy(self.manifest.get("sourceBounds")),
            "chunkSize": self.manifest.get("chunkSize"),
            "chunkCount": len(self.chunk_fingerprints),
            "layerCounts": layer_counts,
            "processes": deepcopy(self.manifest.get("processes") or []),
            "generatedAt": self.manifest.get("generatedAt"),
        }


@dataclass(frozen=True, slots=True)
class _ActiveCacheEntry:
    selector_stamp: tuple[int, int]
    manifest_stamp: tuple[int, int]
    active: ActiveEditorDataset


_active_cache: dict[tuple[str, str, str, tuple[str, ...]], _ActiveCacheEntry] = {}
_chunk_cache: dict[tuple[str, str, tuple[int, int], str], dict[str, Any]] = {}
_cache_lock = RLock()


def clear_editor_dataset_runtime_cache() -> None:
    with _cache_lock:
        _active_cache.clear()
        _chunk_cache.clear()


def editor_dataset_root(value: str | os.PathLike[str] | None = None) -> Path:
    raw = value if value is not None else os.getenv(DATASET_ROOT_ENV)
    return Path(raw or DEFAULT_DATASET_ROOT).expanduser().resolve(strict=False)


def _identity(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not _IDENTITY_PATTERN.fullmatch(text):
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_identity",
            f"{field} is not a safe project/world identifier.",
        )
    return text


def _selector_segment(value: str) -> str:
    # The digest prevents two ids that normalize to the same Windows-safe name
    # from sharing a selector directory.
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:80] or "id"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def selector_path(
    project_id: str,
    world_id: str,
    *,
    root: str | os.PathLike[str] | None = None,
) -> Path:
    project = _identity(project_id, field="projectId")
    world = _identity(world_id, field="worldId")
    base = editor_dataset_root(root)
    return base / "selectors" / _selector_segment(project) / _selector_segment(world) / "active.json"


def _stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise EditorDatasetRuntimeError(
            "editor_dataset_storage_unavailable",
            f"Cannot inspect Editor dataset file: {path.name}",
        ) from exc
    if not path.is_file():
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_storage",
            f"Editor dataset path is not a regular file: {path.name}",
        )
    return stat.st_mtime_ns, stat.st_size


def _read_json(path: Path, *, maximum_bytes: int, field: str) -> dict[str, Any]:
    stamp = _stamp(path)
    if stamp[1] > maximum_bytes:
        raise EditorDatasetRuntimeError(
            "editor_dataset_file_too_large",
            f"{field} exceeds the configured size limit.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_json",
            f"{field} is not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(value, dict):
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_contract",
            f"{field} must contain a JSON object.",
        )
    return value


def _resolve_bundle(root: Path, relative_value: Any) -> Path:
    raw = str(relative_value or "").strip().replace("\\", "/")
    relative = PurePosixPath(raw)
    if (
        not raw
        or relative.is_absolute()
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
    ):
        raise EditorDatasetRuntimeError(
            "editor_dataset_unsafe_path",
            "The active Editor dataset selector contains an unsafe datasetPath.",
        )
    try:
        candidate = root.joinpath(*relative.parts).resolve(strict=True)
    except OSError as exc:
        raise EditorDatasetRuntimeError(
            "editor_dataset_bundle_missing",
            "The selected Editor dataset bundle does not exist.",
        ) from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise EditorDatasetRuntimeError(
            "editor_dataset_unsafe_path",
            "The selected Editor dataset resolves outside the persistent dataset root.",
        ) from exc
    if not candidate.is_dir():
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_storage",
            "The selected Editor dataset is not a directory.",
        )
    return candidate


def _expected_dataset_ids(project_ids: Sequence[str], world_id: str) -> tuple[str, ...]:
    return tuple(
        sorted({f"editor:{project_id}:{world_id}" for project_id in project_ids if project_id})
    )


def _validated_active(
    *,
    root: Path,
    selector_file: Path,
    selector: Mapping[str, Any],
    project_id: str,
    world_id: str,
    accepted_dataset_ids: tuple[str, ...],
) -> ActiveEditorDataset:
    if selector.get("schemaVersion") != SELECTOR_SCHEMA_VERSION:
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_selector",
            "The active Editor dataset selector uses an unsupported schemaVersion.",
        )
    if selector.get("projectId") != project_id or selector.get("worldId") != world_id:
        raise EditorDatasetRuntimeError(
            "editor_dataset_selector_scope_mismatch",
            "The active Editor dataset selector belongs to another project or world.",
        )
    bundle = _resolve_bundle(root, selector.get("datasetPath"))
    manifest_path = bundle / "manifest.json"
    manifest = _read_json(manifest_path, maximum_bytes=MAX_MANIFEST_BYTES, field="manifest.json")
    try:
        validate_editor_dataset(manifest)
    except (TypeError, ValueError) as exc:
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_manifest",
            f"The active Editor dataset manifest failed validation: {exc}",
        ) from exc
    dataset_id = str(manifest.get("datasetId") or "")
    if dataset_id not in accepted_dataset_ids:
        raise EditorDatasetRuntimeError(
            "editor_dataset_project_mismatch",
            "The active Editor dataset manifest belongs to another project or world.",
        )
    if selector.get("datasetId") != dataset_id:
        raise EditorDatasetRuntimeError(
            "editor_dataset_selector_mismatch",
            "The active selector datasetId does not match manifest.json.",
        )
    fingerprint = str(manifest.get("contentFingerprint") or "")
    if selector.get("contentFingerprint") != fingerprint:
        raise EditorDatasetRuntimeError(
            "editor_dataset_selector_mismatch",
            "The active selector fingerprint does not match manifest.json.",
        )

    chunk_fingerprints: dict[str, str] = {}
    for index, raw_chunk in enumerate(manifest.get("chunks") or []):
        if not isinstance(raw_chunk, Mapping):
            raise EditorDatasetRuntimeError(
                "editor_dataset_invalid_manifest",
                f"manifest chunks[{index}] is not an object.",
            )
        key = str(raw_chunk.get("chunkKey") or "")
        try:
            chunk_coordinates(key, field=f"manifest.chunks[{index}].chunkKey")
        except ValueError as exc:
            raise EditorDatasetRuntimeError("editor_dataset_invalid_manifest", str(exc)) from exc
        chunk_fingerprints[key] = str(raw_chunk.get("contentFingerprint") or "")

    roofs_by_id: dict[str, dict[str, Any]] = {}
    layers = manifest.get("layers")
    editable = layers.get("editableBuildings") if isinstance(layers, Mapping) else None
    buildings = editable.get("items") if isinstance(editable, Mapping) else None
    for building in buildings if isinstance(buildings, list) else []:
        if not isinstance(building, Mapping):
            continue
        for roof in building.get("worldEditRoofs") or []:
            if not isinstance(roof, Mapping):
                continue
            object_id = str(roof.get("objectInstanceId") or "")
            if object_id:
                roofs_by_id[object_id] = deepcopy(dict(roof))

    return ActiveEditorDataset(
        root=root,
        bundle_path=bundle,
        selector_path=selector_file,
        selector=deepcopy(dict(selector)),
        manifest=manifest,
        chunk_fingerprints=chunk_fingerprints,
        roofs_by_id=roofs_by_id,
    )


def load_active_editor_dataset(
    project_id: str,
    world_id: str,
    *,
    external_project_id: str | None = None,
    root: str | os.PathLike[str] | None = None,
) -> ActiveEditorDataset | None:
    project = _identity(project_id, field="projectId")
    world = _identity(world_id, field="worldId")
    external = (
        _identity(external_project_id, field="externalProjectId")
        if external_project_id
        else None
    )
    base = editor_dataset_root(root)
    selector_file = selector_path(project, world, root=base)
    if not selector_file.exists():
        return None
    accepted = _expected_dataset_ids(tuple(item for item in (project, external) if item), world)
    cache_key = (str(base), project, world, accepted)
    selector_stamp = _stamp(selector_file)
    with _cache_lock:
        cached = _active_cache.get(cache_key)
    if cached is not None and cached.selector_stamp == selector_stamp:
        manifest_path = cached.active.bundle_path / "manifest.json"
        if _stamp(manifest_path) == cached.manifest_stamp:
            return cached.active

    selector = _read_json(selector_file, maximum_bytes=MAX_SELECTOR_BYTES, field="active.json")
    active = _validated_active(
        root=base,
        selector_file=selector_file,
        selector=selector,
        project_id=project,
        world_id=world,
        accepted_dataset_ids=accepted,
    )
    entry = _ActiveCacheEntry(
        selector_stamp=selector_stamp,
        manifest_stamp=_stamp(active.bundle_path / "manifest.json"),
        active=active,
    )
    with _cache_lock:
        _active_cache[cache_key] = entry
    return active


def load_editor_dataset_bundle(
    bundle_path: str | os.PathLike[str],
    *,
    project_id: str,
    world_id: str,
    external_project_id: str | None = None,
    root: str | os.PathLike[str] | None = None,
) -> ActiveEditorDataset:
    """Validate one bundle in its project/world scope without activating it."""
    project = _identity(project_id, field="projectId")
    world = _identity(world_id, field="worldId")
    external = (
        _identity(external_project_id, field="externalProjectId")
        if external_project_id
        else None
    )
    base = editor_dataset_root(root)
    base.mkdir(parents=True, exist_ok=True)
    try:
        bundle = Path(bundle_path).expanduser().resolve(strict=True)
        relative = bundle.relative_to(base)
    except (OSError, ValueError) as exc:
        raise EditorDatasetRuntimeError(
            "editor_dataset_unsafe_path",
            "Only bundles inside the persistent Editor dataset root can be loaded.",
        ) from exc
    manifest = _read_json(bundle / "manifest.json", maximum_bytes=MAX_MANIFEST_BYTES, field="manifest.json")
    selector = {
        "schemaVersion": SELECTOR_SCHEMA_VERSION,
        "projectId": project,
        "worldId": world,
        "datasetPath": PurePosixPath(*relative.parts).as_posix(),
        "datasetId": manifest.get("datasetId"),
        "contentFingerprint": manifest.get("contentFingerprint"),
    }
    accepted = _expected_dataset_ids(tuple(item for item in (project, external) if item), world)
    return _validated_active(
        root=base,
        selector_file=selector_path(project, world, root=base),
        selector=selector,
        project_id=project,
        world_id=world,
        accepted_dataset_ids=accepted,
    )


def activate_editor_dataset(
    bundle_path: str | os.PathLike[str],
    *,
    project_id: str,
    world_id: str,
    external_project_id: str | None = None,
    root: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically select an already-written immutable bundle for one world."""
    project = _identity(project_id, field="projectId")
    world = _identity(world_id, field="worldId")
    external = (
        _identity(external_project_id, field="externalProjectId")
        if external_project_id
        else None
    )
    base = editor_dataset_root(root)
    active = load_editor_dataset_bundle(
        bundle_path,
        project_id=project,
        external_project_id=external,
        world_id=world,
        root=base,
    )
    bundle = active.bundle_path
    relative = bundle.relative_to(base)
    manifest = active.manifest
    selector = {
        "schemaVersion": SELECTOR_SCHEMA_VERSION,
        "projectId": project,
        "worldId": world,
        "datasetPath": PurePosixPath(*relative.parts).as_posix(),
        "datasetId": manifest["datasetId"],
        "contentFingerprint": manifest["contentFingerprint"],
    }
    target = selector_path(project, world, root=base)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(selector, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    clear_editor_dataset_runtime_cache()
    return target


def editor_dataset_lod2_plan(active: ActiveEditorDataset) -> dict[str, Any]:
    """Reconstruct the canonical ``apply_import`` plan from a validated bundle."""
    layers = active.manifest.get("layers")
    editable = layers.get("editableBuildings") if isinstance(layers, Mapping) else None
    raw_buildings = editable.get("items") if isinstance(editable, Mapping) else None
    if not isinstance(raw_buildings, list):
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_manifest",
            "The active Editor dataset has no editableBuildings layer.",
        )
    if len(raw_buildings) > 200:
        raise EditorDatasetRuntimeError(
            "editor_dataset_materialization_budget_exceeded",
            "The Editor dataset exceeds the 200-building LoD2 import budget.",
        )
    buildings: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    all_cells: set[tuple[int, int, int]] = set()
    chunk_keys: set[tuple[int, int, int]] = set()
    chunk_size = int(active.manifest.get("chunkSize") or 0)
    for raw in raw_buildings:
        if not isinstance(raw, Mapping):
            raise EditorDatasetRuntimeError(
                "editor_dataset_invalid_manifest",
                "The editableBuildings layer contains a non-object item.",
            )
        walls = raw.get("wallBlocks")
        cells = walls.get("cells") if isinstance(walls, Mapping) else None
        roofs = raw.get("worldEditRoofs")
        if not isinstance(cells, list) or not isinstance(roofs, list):
            raise EditorDatasetRuntimeError(
                "editor_dataset_invalid_manifest",
                "An editable building has no canonical walls or WorldEdit roofs.",
            )
        normalized_cells = [[int(value) for value in cell] for cell in cells]
        for cell in normalized_cells:
            key = tuple(cell)
            all_cells.add(key)
            chunk_keys.add(tuple(math.floor(value / chunk_size) for value in key))
        building = {
            "buildingId": str(raw.get("buildingId") or ""),
            "sourceTile": str(raw.get("sourceTile") or ""),
            "sourceSha256": str(raw.get("sourceSha256") or ""),
            "wallCells": normalized_cells,
            "roofs": deepcopy(roofs),
            "facadeSegments": deepcopy(list(raw.get("facadeSegments") or [])),
            "groundFootprints": deepcopy(list(raw.get("groundFootprints") or [])),
            "constructionGrid": deepcopy(raw.get("constructionGrid")),
        }
        buildings.append(building)
        repairs.append({
            "buildingId": building["buildingId"],
            "facadeSegments": deepcopy(building["facadeSegments"]),
            "groundFootprints": deepcopy(building["groundFootprints"]),
            "constructionGrid": deepcopy(building["constructionGrid"]),
        })
    if len(all_cells) > 400_000 or len(chunk_keys) > 2_048:
        raise EditorDatasetRuntimeError(
            "editor_dataset_materialization_budget_exceeded",
            "The Editor dataset exceeds the canonical LoD2 wall/chunk import budget.",
        )
    return {
        "version": "vectoplan-editor-dataset-materialization.v1",
        "referenceFingerprint": active.reference_fingerprint,
        "bounds": deepcopy(list(active.manifest.get("sourceBounds") or [])),
        "heightReference": {
            "kind": "dataset-aligned-world-cells",
            "coordinateFrame": deepcopy(active.manifest.get("coordinateFrame")),
            "sourceBounds": deepcopy(list(active.manifest.get("sourceBounds") or [])),
            "datasetFingerprint": active.content_fingerprint,
        },
        "sourceRevision": active.content_fingerprint,
        "sourceRevisions": {"editor-dataset": active.content_fingerprint},
        "sourceErrors": {},
        "buildings": buildings,
        "alreadyImported": [],
        "skipped": [],
        "metadataRepairs": repairs,
        "candidateWallCells": len(all_cells),
        "candidateChunks": len(chunk_keys),
    }


def load_editor_dataset_chunk(
    active: ActiveEditorDataset,
    chunk_key: str,
) -> dict[str, Any] | None:
    try:
        x, y, z = chunk_coordinates(chunk_key)
    except ValueError as exc:
        raise EditorDatasetRuntimeError("editor_dataset_invalid_chunk_key", str(exc)) from exc
    canonical_key = f"{x}:{y}:{z}"
    expected = active.chunk_fingerprints.get(canonical_key)
    if expected is None:
        return None
    chunk_path = active.bundle_path / "chunks" / f"{x}_{y}_{z}.json"
    resolved = chunk_path.resolve(strict=False)
    try:
        resolved.relative_to(active.bundle_path)
    except ValueError as exc:  # defensive: coordinates cannot produce this path
        raise EditorDatasetRuntimeError(
            "editor_dataset_unsafe_path",
            "The Editor dataset chunk path escapes its immutable bundle.",
        ) from exc
    stamp = _stamp(resolved)
    cache_key = (str(active.bundle_path), canonical_key, stamp, expected)
    with _cache_lock:
        cached = _chunk_cache.get(cache_key)
    if cached is not None:
        return deepcopy(cached)
    raw = _read_json(resolved, maximum_bytes=MAX_CHUNK_BYTES, field=f"chunks/{x}_{y}_{z}.json")
    source = dict(raw)
    fingerprint = str(source.pop("contentFingerprint", ""))
    if fingerprint != expected or content_fingerprint(source) != expected:
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_chunk_fingerprint",
            f"Editor dataset chunk {canonical_key} failed fingerprint validation.",
        )
    if str(raw.get("chunkKey") or "") != canonical_key:
        raise EditorDatasetRuntimeError(
            "editor_dataset_invalid_chunk_contract",
            f"Editor dataset chunk file does not contain {canonical_key}.",
        )
    with _cache_lock:
        if len(_chunk_cache) >= 2_048:
            _chunk_cache.pop(next(iter(_chunk_cache)))
        _chunk_cache[cache_key] = deepcopy(raw)
    return raw


def _materialized_building_ids(world: Any) -> set[str]:
    metadata = getattr(world, "metadata_json", None)
    lod2 = metadata.get("lod2Buildings") if isinstance(metadata, Mapping) else None
    ledger = lod2.get("materializedBuildings") if isinstance(lod2, Mapping) else None
    return {str(key) for key in ledger} if isinstance(ledger, Mapping) else set()


def _roof_object_ref(
    roof: Mapping[str, Any],
    packed_ref: Mapping[str, Any],
    active: ActiveEditorDataset,
) -> dict[str, Any]:
    position = deepcopy(dict(roof.get("position") or {}))
    metadata = deepcopy(dict(roof.get("metadata") or {}))
    metadata["editorDataset"] = {
        "schemaVersion": EDITOR_DATASET_SCHEMA_VERSION,
        "datasetId": active.dataset_id,
        "contentFingerprint": active.content_fingerprint,
        "responseProjection": True,
    }
    return {
        "objectInstanceId": str(roof.get("objectInstanceId") or ""),
        "objectTypeId": str(roof.get("objectTypeId") or "building_roof"),
        "objectVariantId": roof.get("objectVariantId"),
        "anchor": position,
        "dimensions": deepcopy(dict(roof.get("dimensions") or {})),
        "fillBlockTypeId": str(roof.get("blockTypeId") or "lod2_exterior_wall"),
        "primaryChunkKey": str(packed_ref.get("primaryChunkKey") or ""),
        "objectKind": str(roof.get("objectKind") or "semantic_footprint"),
        "footprint": deepcopy(dict(roof.get("footprint") or {})),
        "occupiedCells": deepcopy(list(roof.get("occupiedCells") or [position])),
        "metadata": metadata,
    }


def _street_overlay_item(
    active: ActiveEditorDataset,
    chunk_key: str,
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    effective_widths = [
        max(0.1, min(6.0, float(item.get("effectiveWidthM") or item.get("availableWidthM") or 6.0)))
        for item in segments
        if math.isfinite(float(item.get("effectiveWidthM") or item.get("availableWidthM") or 6.0))
    ]
    coordinates = [
        [deepcopy(list(item.get("start") or [])), deepcopy(list(item.get("end") or []))]
        for item in segments
    ]
    return {
        "id": "street-network",
        "datasetId": "strassendaten",
        "label": "Straßen- und Wegenetz",
        "renderMode": "surface-ribbons",
        "semanticRole": "street-network",
        "classificationSource": True,
        "style": {
            "color": "#fbfcfd",
            "opacity": 1.0,
            "lineWidth": 2.0,
            "surfaceWidth": 6.0,
            "verticalOffset": 0.03,
            "sampleStep": 0.25,
        },
        "releaseKey": active.content_fingerprint,
        "tileKey": chunk_key,
        "source": {
            "kind": "vectoplan-editor-dataset",
            "schemaVersion": EDITOR_DATASET_SCHEMA_VERSION,
            "datasetId": active.dataset_id,
            "immutable": True,
        },
        "geometry": {
            "type": "MultiLineString",
            "dimensions": "world-xz",
            "coordinates": coordinates,
            "surfaceWidths": effective_widths,
        },
        "stats": {
            "featureCount": len({str(item.get("featureId") or "") for item in segments}),
            "sourceSegmentCount": len(segments),
            "emittedSegmentCount": len(segments),
            "nominalWidthM": 6.0,
            "minimumEffectiveWidthM": min(effective_widths, default=6.0),
            "widthPolicy": "nominal-6m-clamped-to-road-parcel-surface.v1",
        },
    }


def attach_active_editor_dataset(
    chunk: dict[str, Any],
    *,
    project: Any,
    world: Any,
    root: str | os.PathLike[str] | None = None,
) -> bool:
    """Project validated bundle artifacts into an already serialized chunk."""
    project_id = str(getattr(project, "project_id", "") or "")
    external_id = str(getattr(project, "external_app_project_id", "") or "") or None
    world_id = str(getattr(world, "world_id", "") or "")
    active = load_active_editor_dataset(
        project_id,
        world_id,
        external_project_id=external_id,
        root=root,
    )
    if active is None:
        return False
    if int(active.manifest.get("chunkSize") or 0) != int(getattr(world, "chunk_size", 0) or 0):
        raise EditorDatasetRuntimeError(
            "editor_dataset_chunk_size_mismatch",
            "The active Editor dataset chunk size does not match the project world.",
        )
    try:
        provider = world.build_earth_provider()
        expected_reference = str(getattr(provider, "reference_fingerprint", "") or "")
    except Exception:
        expected_reference = ""
    if expected_reference and active.reference_fingerprint != expected_reference:
        raise EditorDatasetRuntimeError(
            "editor_dataset_reference_mismatch",
            "The active Editor dataset uses another immutable Earth reference frame.",
        )

    x = int(chunk.get("chunkX") or 0)
    y = int(chunk.get("chunkY") or 0)
    z = int(chunk.get("chunkZ") or 0)
    key = f"{x}:{y}:{z}"
    exact = load_editor_dataset_chunk(active, key)
    # Surface ribbons have one owner in the y=0 projection chunk.  Copying the
    # same street overlay into every vertical chunk would multiply draw calls
    # as the camera streams building-height chunks.
    surface = exact if y == 0 else None
    if exact is None and surface is None:
        return False

    object_refs = deepcopy(list(chunk.get("objectRefs") or []))
    existing_ids = {
        str(item.get("objectInstanceId") or "")
        for item in object_refs
        if isinstance(item, Mapping)
    }
    # A preserved facade source is not a visible roof. Both its explicit
    # tombstone and the world ledger prevent immutable bundle projection from
    # reviving that deleted roof in another streamed chunk.
    lod2_config = (getattr(world, "metadata_json", None) or {}).get("lod2Buildings") or {}
    removed_roofs = lod2_config.get("removedRoofObjectIds") or {}
    existing_ids.update(str(key) for key in removed_roofs)
    for item in object_refs:
        source = (item.get("metadata") or {}).get("lod2FacadeSource") if isinstance(item, Mapping) else None
        if isinstance(source, Mapping):
            existing_ids.update(str(source[key]) for key in ("deletedRoofObjectInstanceId", "originalRoofObjectInstanceId") if source.get(key))
    materialized = _materialized_building_ids(world)
    added_roofs = 0
    if exact is not None:
        for packed in exact.get("roofObjectRefs") or []:
            if not isinstance(packed, Mapping):
                continue
            object_id = str(packed.get("objectInstanceId") or "")
            building_id = str(packed.get("buildingId") or "")
            roof = active.roofs_by_id.get(object_id)
            if not object_id or roof is None or object_id in existing_ids or building_id in materialized:
                continue
            object_refs.append(_roof_object_ref(roof, packed, active))
            existing_ids.add(object_id)
            added_roofs += 1

    metadata = deepcopy(dict(chunk.get("metadata") or {}))
    street_segments = [
        item for item in (surface or {}).get("streetSegments") or [] if isinstance(item, Mapping)
    ]
    if street_segments:
        contract = deepcopy(dict(metadata.get("geodataOverlays") or {}))
        items = [
            deepcopy(dict(item))
            for item in contract.get("items") or []
            if isinstance(item, Mapping) and item.get("semanticRole") != "street-network"
        ]
        items.append(_street_overlay_item(active, f"{x}:0:{z}", street_segments))
        contract.update({
            "schemaVersion": "geodata-overlays.v1",
            "status": contract.get("status") or "ready",
            "referenceFingerprint": active.reference_fingerprint,
            "items": items,
        })
        availability = [
            deepcopy(dict(item))
            for item in contract.get("availability") or []
            if isinstance(item, Mapping) and item.get("id") != "street-network"
        ]
        availability.append({"id": "street-network", "kind": "vector", "status": "available"})
        contract["availability"] = availability
        # The live overlay pipeline may already have resolved its visual layer
        # priority before the immutable street item replaced the live one.
        # Recompute instead of leaking that stale decision to the Editor.
        from src.geodata.visual_layer_resolution import attach_visual_layer_resolution

        attach_visual_layer_resolution(contract)
        metadata["geodataOverlays"] = contract

    exact_fingerprint = str((exact or {}).get("contentFingerprint") or "") or None
    metadata["editorDataset"] = {
        "schemaVersion": EDITOR_DATASET_SCHEMA_VERSION,
        "datasetId": active.dataset_id,
        "contentFingerprint": active.content_fingerprint,
        "chunkFingerprint": exact_fingerprint,
        "referenceFingerprint": active.reference_fingerprint,
        "roofObjectRefsAdded": added_roofs,
        "streetSegmentCount": len(street_segments),
        "wallBlocksAvailable": len((exact or {}).get("wallBlocks") or []),
        "wallBlocksProjection": "canonical-worldedit-import-only",
        "parcelGridRefs": deepcopy(list((exact or {}).get("parcelGridRefs") or [])),
    }
    chunk["objectRefs"] = object_refs
    chunk["metadata"] = metadata
    return True


__all__ = (
    "ACTIVE_RESPONSE_VERSION",
    "DATASET_ROOT_ENV",
    "DEFAULT_DATASET_ROOT",
    "EDITOR_DATASET_SCHEMA_VERSION",
    "SELECTOR_SCHEMA_VERSION",
    "ActiveEditorDataset",
    "EditorDatasetRuntimeError",
    "activate_editor_dataset",
    "attach_active_editor_dataset",
    "clear_editor_dataset_runtime_cache",
    "editor_dataset_root",
    "editor_dataset_lod2_plan",
    "load_active_editor_dataset",
    "load_editor_dataset_bundle",
    "load_editor_dataset_chunk",
    "selector_path",
)
