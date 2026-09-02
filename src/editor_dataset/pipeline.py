from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from .contracts import (
    ProcessManifest,
    ProcessReceipt,
    chunk_coordinates,
    content_fingerprint,
    finite_number,
)
from .processes.chunk_pack.processor import run as run_chunk_pack
from .processes.lod2_editable.processor import run as run_lod2_editable
from .processes.parcel_grid.processor import run as run_parcel_grid
from .processes.road_network.processor import run as run_road_network


EDITOR_DATASET_SCHEMA_VERSION = "vectoplan-editor-dataset.v1"
PIPELINE_VERSION = "vectoplan-editor-dataset-pipeline.v1"
PROCESS_ROOT = Path(__file__).with_name("processes")

ProcessRunner = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
PROCESS_ORDER = ("lod2-editable", "parcel-grid", "road-network", "chunk-pack")
LAYER_BY_PROCESS = {
    "lod2-editable": "editableBuildings",
    "parcel-grid": "parcelGrids",
    "road-network": "streetNetwork",
}


def _manifests() -> tuple[ProcessManifest, ...]:
    result = tuple(
        ProcessManifest.from_path(PROCESS_ROOT / directory / "process.json")
        for directory in ("lod2_editable", "parcel_grid", "road_network", "chunk_pack")
    )
    ids = [item.process_id for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate editor dataset process id")
    if tuple(ids) != PROCESS_ORDER:
        raise ValueError("Editor dataset processes do not match the supported process order")
    output_layers = [item.output_layer for item in result]
    if len(output_layers) != len(set(output_layers)):
        raise ValueError("Duplicate editor dataset output layer")
    available: set[str] = set()
    for item in result:
        missing = set(item.dependencies) - available
        if missing:
            raise ValueError(f"Process {item.process_id} has unresolved dependencies: {sorted(missing)}")
        available.add(item.process_id)
    return result


RUNNERS: dict[str, ProcessRunner] = {
    "lod2-editable": run_lod2_editable,
    "parcel-grid": run_parcel_grid,
    "road-network": run_road_network,
    "chunk-pack": run_chunk_pack,
}


class EditorDatasetPipeline:
    def __init__(self) -> None:
        self.manifests = _manifests()

    def run(self, context: Mapping[str, Any]) -> dict[str, Any]:
        source = dict(context)
        dataset_id = str(source.get("datasetId") or "").strip()
        reference = str(source.get("referenceFingerprint") or "").strip()
        chunk_size_value = finite_number(source.get("chunkSize"), field="chunkSize")
        if not chunk_size_value.is_integer():
            raise ValueError("chunkSize must be a whole number")
        chunk_size = int(chunk_size_value)
        if not dataset_id or not reference:
            raise ValueError("datasetId and referenceFingerprint are required")
        if chunk_size <= 0 or chunk_size > 256:
            raise ValueError("chunkSize must be between 1 and 256")

        coordinate_frame = source.get("coordinateFrame")
        if not isinstance(coordinate_frame, Mapping):
            raise ValueError("coordinateFrame must be an Earth-grid frame object")
        coordinate_frame = dict(coordinate_frame)
        if coordinate_frame.get("schemaVersion") != "vectoplan-earth-grid-frame.v1":
            raise ValueError("coordinateFrame must use vectoplan-earth-grid-frame.v1")

        lod2_plan = source.get("lod2Plan")
        if not isinstance(lod2_plan, Mapping):
            raise ValueError("lod2Plan must be a prepare_import result")
        plan_reference = str(lod2_plan.get("referenceFingerprint") or "").strip()
        if plan_reference and plan_reference != reference:
            raise ValueError("lod2Plan and dataset use different Earth reference frames")
        raw_bounds = source.get("sourceBounds") or lod2_plan.get("bounds")
        if (
            not isinstance(raw_bounds, Sequence)
            or isinstance(raw_bounds, (str, bytes, bytearray))
            or len(raw_bounds) != 4
        ):
            raise ValueError("sourceBounds must contain minX, minZ, maxX and maxZ")
        bounds = [finite_number(value, field=f"sourceBounds[{index}]") for index, value in enumerate(raw_bounds)]
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            raise ValueError("sourceBounds must have a positive extent")
        if bounds[2] - bounds[0] > 512 or bounds[3] - bounds[1] > 512:
            raise ValueError("sourceBounds exceeds the bounded 512-cell import window")

        source["chunkSize"] = chunk_size
        source["coordinateFrame"] = coordinate_frame
        source["sourceBounds"] = bounds

        context_fingerprint = content_fingerprint(source)
        artifacts: dict[str, dict[str, Any]] = {}
        receipts: list[dict[str, Any]] = []
        layers: dict[str, Any] = {}
        for manifest in self.manifests:
            runner = RUNNERS.get(manifest.process_id)
            if runner is None:
                raise ValueError(f"No runner registered for {manifest.process_id}")
            dependency_values = {
                dependency: artifacts[dependency] for dependency in manifest.dependencies
            }
            process_input = {
                "contextFingerprint": context_fingerprint,
                "dependencies": dependency_values,
                "version": manifest.version,
            }
            output = dict(runner(source, artifacts))
            if not isinstance(output.get("items"), list):
                raise ValueError(f"Process {manifest.process_id} must return an items array")
            output_fingerprint = content_fingerprint(output)
            artifacts[manifest.process_id] = output
            layers[manifest.output_layer] = output
            raw_items = output["items"]
            item_count = len(raw_items)
            receipts.append(ProcessReceipt(
                process_id=manifest.process_id,
                version=manifest.version,
                dependencies=manifest.dependencies,
                input_fingerprint=content_fingerprint(process_input),
                output_fingerprint=output_fingerprint,
                item_count=item_count,
            ).as_dict())

        chunk_output = artifacts["chunk-pack"]
        dataset = {
            "schemaVersion": EDITOR_DATASET_SCHEMA_VERSION,
            "pipelineVersion": PIPELINE_VERSION,
            "datasetId": dataset_id,
            "referenceFingerprint": reference,
            "coordinateFrame": coordinate_frame,
            "sourceBounds": bounds,
            "chunkSize": chunk_size,
            "sourceFingerprint": context_fingerprint,
            "processes": receipts,
            "layers": {
                "editableBuildings": artifacts["lod2-editable"],
                "parcelGrids": artifacts["parcel-grid"],
                "streetNetwork": artifacts["road-network"],
            },
            "chunks": list(chunk_output.get("items") or []),
        }
        dataset["contentFingerprint"] = content_fingerprint(dataset)
        return dataset


def build_editor_dataset(context: Mapping[str, Any]) -> dict[str, Any]:
    return EditorDatasetPipeline().run(context)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _chunk_pack_output(chunks: list[Any]) -> dict[str, Any]:
    return {
        "schemaVersion": "vectoplan-editor-chunk-artifacts.v1",
        "itemCount": len(chunks),
        "items": chunks,
    }


def validate_editor_dataset(dataset: Mapping[str, Any]) -> None:
    """Validate one in-memory v1 dataset, independent of its storage path.

    ``manifest.json`` adds the informational ``generatedAt`` field after the
    immutable dataset fingerprint has been calculated.  Consumers therefore
    validate the exact pipeline payload while deliberately excluding that one
    writer-owned timestamp from the fingerprint source.
    """
    if dataset.get("schemaVersion") != EDITOR_DATASET_SCHEMA_VERSION:
        raise ValueError("Unsupported editor dataset schemaVersion")
    fingerprint = str(dataset.get("contentFingerprint") or "")
    fingerprint_source = dict(dataset)
    fingerprint_source.pop("contentFingerprint", None)
    fingerprint_source.pop("generatedAt", None)
    if not fingerprint or content_fingerprint(fingerprint_source) != fingerprint:
        raise ValueError("Editor dataset contentFingerprint does not match its content")

    raw_layers = dataset.get("layers")
    if not isinstance(raw_layers, Mapping):
        raise ValueError("Editor dataset layers must be an object")
    raw_chunks = dataset.get("chunks")
    if not isinstance(raw_chunks, list):
        raise ValueError("Editor dataset chunks must be an array")
    chunks_by_key: dict[str, Mapping[str, Any]] = {}
    for index, raw_chunk in enumerate(raw_chunks):
        if not isinstance(raw_chunk, Mapping):
            raise ValueError(f"chunks[{index}] must be an object")
        key = str(raw_chunk.get("chunkKey") or "")
        chunk_coordinates(key, field=f"chunks[{index}].chunkKey")
        if key in chunks_by_key:
            raise ValueError(f"Duplicate editor dataset chunkKey: {key}")
        expected = str(raw_chunk.get("contentFingerprint") or "")
        chunk_source = dict(raw_chunk)
        chunk_source.pop("contentFingerprint", None)
        if not expected or content_fingerprint(chunk_source) != expected:
            raise ValueError(f"Chunk {key} has an invalid contentFingerprint")
        chunks_by_key[key] = raw_chunk

    raw_processes = dataset.get("processes")
    if not isinstance(raw_processes, list):
        raise ValueError("Editor dataset processes must be an array")
    receipts: dict[str, Mapping[str, Any]] = {}
    for index, receipt in enumerate(raw_processes):
        if not isinstance(receipt, Mapping):
            raise ValueError(f"processes[{index}] must be an object")
        process_id = str(receipt.get("processId") or "")
        if process_id in receipts:
            raise ValueError(f"Duplicate editor dataset process receipt: {process_id}")
        receipts[process_id] = receipt
    if tuple(receipts) != PROCESS_ORDER:
        raise ValueError("Editor dataset process receipts are incomplete or out of order")

    for process_id, receipt in receipts.items():
        if receipt.get("status") != "succeeded":
            raise ValueError(f"Process {process_id} is not succeeded")
        if process_id == "chunk-pack":
            output = _chunk_pack_output(raw_chunks)
        else:
            layer_name = LAYER_BY_PROCESS[process_id]
            output = raw_layers.get(layer_name)
            if not isinstance(output, Mapping):
                raise ValueError(f"Layer {layer_name} is missing")
        if content_fingerprint(output) != str(receipt.get("outputFingerprint") or ""):
            raise ValueError(f"Process {process_id} outputFingerprint does not match")


def write_editor_dataset(dataset: Mapping[str, Any], output: Path) -> Path:
    """Write one immutable bundle with a separate directory per process."""
    validate_editor_dataset(dataset)
    target = output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite editor dataset bundle: {target}")
    lock_path = target.parent / f".{target.name}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise FileExistsError(f"Editor dataset bundle is already being written: {target}") from exc
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        temporary.mkdir(exist_ok=False)
        processes_by_id = {
            str(item.get("processId")): item
            for item in dataset.get("processes", [])
            if isinstance(item, Mapping)
        }
        for process_id, receipt in processes_by_id.items():
            process_dir = temporary / "processes" / process_id
            _write_json(process_dir / "receipt.json", receipt)
            if process_id in LAYER_BY_PROCESS:
                _write_json(
                    process_dir / "output.json",
                    dict(dataset.get("layers") or {}).get(LAYER_BY_PROCESS[process_id], {}),
                )
            elif process_id == "chunk-pack":
                _write_json(
                    process_dir / "output.json",
                    _chunk_pack_output(list(dataset.get("chunks") or [])),
                )
        for chunk in dataset.get("chunks", []):
            key = str(chunk.get("chunkKey"))
            chunk_coordinates(key)
            _write_json(temporary / "chunks" / f"{key.replace(':', '_')}.json", chunk)
        manifest = dict(dataset)
        manifest["generatedAt"] = datetime.now(timezone.utc).isoformat()
        _write_json(temporary / "manifest.json", manifest)
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite editor dataset bundle: {target}")
        os.rename(temporary, target)
        return target
    except BaseException:
        if temporary.exists() and temporary.parent == target.parent:
            shutil.rmtree(temporary)
        raise
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
