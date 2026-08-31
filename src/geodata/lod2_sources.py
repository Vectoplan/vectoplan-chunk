"""Server-owned, spatial LoD2 source catalog. Never downloads on viewer reads.

Any region can register a derived CityGML store with explicit horizontal/vertical
CRS. A missing local store or coverage is normal no-data, not a Berlin fallback.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import sqlite3

from pyproj import CRS, Transformer

from src.geodata.citygml_store import SOURCE_CRS, SCHEMA


@dataclass(frozen=True)
class Lod2Source:
    id: str
    path: Path
    label: str
    horizontal_crs: str
    vertical_crs: str
    source_crs: str
    license: str


def sources_for_world(config) -> list[Lod2Source]:
    raw = os.getenv("VECTOPLAN_CHUNK_LOD2_SOURCES_JSON")
    definitions = json.loads(raw) if raw else [{
        "id": "berlin-lod2-buildings", "label": "Berlin LoD2-Gebäude",
        "path": os.getenv("VECTOPLAN_CHUNK_LOD2_STORE", "/var/lib/vectoplan-chunk/terrain-cache/lod2/berlin.sqlite3"),
        "horizontalCrs": "EPSG:25833", "verticalCrs": "EPSG:7837", "sourceCrs": SOURCE_CRS,
        "license": "dl-de-zero-2.0",
    }]
    if not isinstance(definitions, list):
        raise ValueError("LoD2 source catalog must be an array")
    selected = config.get("sourceIds")
    if selected is not None and not isinstance(selected, list):
        raise ValueError("LoD2 sourceIds must be an array")
    result, seen = [], set()
    for value in definitions[:128]:
        if not isinstance(value, dict) or value.get("enabled") is False:
            continue
        source_id = str(value.get("id", "")).strip()
        if not source_id or source_id in seen:
            raise ValueError("LoD2 source IDs must be unique and nonempty")
        seen.add(source_id)
        if selected is not None and source_id not in selected:
            continue
        if not all(value.get(key) for key in ("path", "horizontalCrs", "verticalCrs", "sourceCrs")):
            raise ValueError(f"LoD2 source {source_id} needs a path and explicit CRS")
        path = Path(value["path"])
        if not path.is_absolute():
            raise ValueError("LoD2 source paths must be absolute server-owned paths")
        result.append(Lod2Source(source_id, path, str(value.get("label", source_id)),
                                 str(value["horizontalCrs"]), str(value["verticalCrs"]),
                                 str(value["sourceCrs"]), str(value.get("license", "not-specified"))))
    return result


def generation(path: Path) -> tuple:
    stat = path.stat()
    wal = Path(str(path) + "-wal")
    wal_stat = wal.stat() if wal.exists() else None
    return (stat.st_mtime_ns, stat.st_size, wal_stat.st_mtime_ns if wal_stat else 0,
            wal_stat.st_size if wal_stat else 0)


@lru_cache(maxsize=128)
def store_info(path: str, version: tuple) -> dict:
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("schema") != SCHEMA:
            raise ValueError("Unsupported LoD2 store schema")
        bounds = db.execute("SELECT MIN(min_e),MIN(min_n),MAX(max_e),MAX(max_n) FROM building_bounds").fetchone()
        crs = [row[0] for row in db.execute("SELECT DISTINCT crs FROM tiles")]
        return {"bounds": bounds if bounds and bounds[0] is not None else None, "crs": crs, "metadata": metadata}


@lru_cache(maxsize=128)
def transformers(horizontal_crs: str):
    return (Transformer.from_crs("EPSG:4326", horizontal_crs, always_xy=True),
            Transformer.from_crs(horizontal_crs, "EPSG:4326", always_xy=True))


@lru_cache(maxsize=128)
def vertical_metres_per_unit(vertical_crs: str) -> float:
    axes = CRS.from_user_input(vertical_crs).axis_info
    if not axes or axes[-1].direction != "up":
        raise ValueError("LoD2 needs an explicit upward height axis")
    factor = float(axes[-1].unit_conversion_factor)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("Unsupported vertical units")
    return factor


def source_bbox(frame, cx, cz, size, source: Lod2Source):
    forward, _ = transformers(source.horizontal_crs)
    points = []
    for t in (0, .25, .5, .75, 1):
        for x, z in ((cx*size+t*size, cz*size), (cx*size+t*size, (cz+1)*size),
                     (cx*size, cz*size+t*size), ((cx+1)*size, cz*size+t*size)):
            lon = ((x + frame["storageOrigin"]["x"])*360/frame["worldWidthCells"] + frame["centralMeridianDegrees"] + 180) % 360 - 180
            lat = (z + frame["storageOrigin"]["z"])*180/frame["worldHeightCells"]
            if not -90 <= lat <= 90:
                return None
            point = forward.transform(lon, lat)
            if not all(math.isfinite(v) for v in point):
                return None
            points.append(point)
    return min(p[0] for p in points)-.05, min(p[1] for p in points)-.05, max(p[0] for p in points)+.05, max(p[1] for p in points)+.05


def source_availability(source, frame, cx, cz, size):
    if not source.path.is_file():
        return "not-downloaded", None, None
    version = generation(source.path)
    info = store_info(str(source.path), version)
    if any(crs != source.source_crs for crs in info["crs"]):
        raise ValueError(f"Source CRS does not match the stored data: {source.id}")
    for key, value in (("horizontalCrs", source.horizontal_crs), ("verticalCrs", source.vertical_crs)):
        if info["metadata"].get(key, value) != value:
            raise ValueError(f"Source catalog {key} does not match the stored data")
    native = source_bbox(frame, cx, cz, size, source)
    bounds = info["bounds"]
    if not native or not bounds or native[2]<bounds[0] or native[0]>bounds[2] or native[3]<bounds[1] or native[1]>bounds[3]:
        return "outside-available-coverage", native, version
    return "available", native, version
