from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any


PROCESS_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHUNK_KEY_PATTERN = re.compile(r"^-?\d+:-?\d+:-?\d+$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_fingerprint(value: Any) -> str:
    return sha256(canonical_json(value)).hexdigest()


def json_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    return dict(value)


def finite_number(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def chunk_coordinates(value: Any, *, field: str = "chunkKey") -> tuple[int, int, int]:
    key = str(value or "").strip()
    if not CHUNK_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"{field} must be x:y:z integer coordinates")
    x, y, z = (int(item) for item in key.split(":"))
    coordinates = (x, y, z)
    if any(abs(item) > 100_000_000 for item in coordinates):
        raise ValueError(f"{field} exceeds the supported coordinate range")
    return coordinates


@dataclass(frozen=True, slots=True)
class ProcessManifest:
    process_id: str
    version: str
    dependencies: tuple[str, ...]
    output_layer: str

    @classmethod
    def from_path(cls, path: Path) -> "ProcessManifest":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid editor dataset process manifest: {path}") from exc
        value = json_mapping(raw, field=str(path))
        process_id = str(value.get("id") or "").strip()
        version = str(value.get("version") or "").strip()
        output_layer = str(value.get("outputLayer") or "").strip()
        dependencies_value = value.get("dependencies") or []
        if not isinstance(dependencies_value, Sequence) or isinstance(
            dependencies_value, (str, bytes, bytearray)
        ):
            raise ValueError(f"dependencies must be an array in {path}")
        dependencies = tuple(str(item).strip() for item in dependencies_value)
        if (
            not process_id
            or not PROCESS_ID_PATTERN.fullmatch(process_id)
            or not version
            or not output_layer
            or any(not item or not PROCESS_ID_PATTERN.fullmatch(item) for item in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            raise ValueError(f"Incomplete editor dataset process manifest: {path}")
        return cls(process_id, version, dependencies, output_layer)


@dataclass(frozen=True, slots=True)
class ProcessReceipt:
    process_id: str
    version: str
    dependencies: tuple[str, ...]
    input_fingerprint: str
    output_fingerprint: str
    item_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "processId": self.process_id,
            "version": self.version,
            "dependencies": list(self.dependencies),
            "inputFingerprint": self.input_fingerprint,
            "outputFingerprint": self.output_fingerprint,
            "itemCount": self.item_count,
            "status": "succeeded",
        }
