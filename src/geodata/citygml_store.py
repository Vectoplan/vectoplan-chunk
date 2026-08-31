"""Derived, spatially indexed LoD2 surfaces. Originals remain in the raw store.

Import is explicit/offline, checksum-verified and transactional per source ZIP.
The runtime never downloads/parses a city model on a chunk request. Coordinates
remain ETRS89/UTM33 + DHHN2016 here; world-specific conversion is a separate step.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sqlite3
import xml.etree.ElementTree as ET
import zipfile

GML = "{http://www.opengis.net/gml}"
SOURCE_CRS = "urn:adv:crs:ETRS89_UTM33*DE_DHHN2016_NH"
SCHEMA = "citygml-lod2-store.v1"
SURFACES = {"WallSurface", "RoofSurface", "GroundSurface", "ClosureSurface",
            "OuterCeilingSurface", "OuterFloorSurface"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ring(element: ET.Element) -> list[list[float]]:
    positions = element.findall(f".//{GML}posList")
    points = []
    if positions:
        for position in positions:
            if position.get("srsDimension", "3") != "3":
                raise ValueError("LoD2 requires three-dimensional positions")
            values = [float(value) for value in (position.text or "").split()]
            if len(values) % 3:
                raise ValueError("Invalid XYZ position list")
            points.extend([values[i:i + 3] for i in range(0, len(values), 3)])
    else:
        points = [[float(value) for value in (p.text or "").split()]
                  for p in element.findall(f".//{GML}pos")]
    if any(len(p) != 3 or not all(math.isfinite(v) for v in p) for p in points):
        raise ValueError("Invalid/non-finite XYZ coordinate")
    if len(points) < 4 or points[0] != points[-1]:
        raise ValueError("CityGML LinearRing must be closed with at least 3 vertices")
    return points


def building_feature(building: ET.Element) -> dict:
    object_id = building.get(f"{GML}id")
    if not object_id:
        raise ValueError("Building has no stable gml:id")
    polygons, seen = [], set()

    def append(polygon: ET.Element, kind: str) -> None:
        exterior = polygon.find(f"{GML}exterior")
        if exterior is None:
            raise ValueError("Polygon has no exterior")
        rings = [_ring(exterior)] + [_ring(r) for r in polygon.findall(f"{GML}interior")]
        identity = polygon.get(f"{GML}id") or json.dumps(rings, separators=(",", ":"))
        if identity in seen:
            return
        seen.add(identity)
        polygons.append({"surface": kind, "rings": rings})

    # Collect boundary surfaces once, including nested BuildingParts. Solid
    # xlinks reference these same polygons and must not create duplicate faces.
    for surface in building.iter():
        kind = _local(surface.tag)
        if kind not in SURFACES:
            continue
        for geometry in surface:
            if _local(geometry.tag) == "lod2MultiSurface":
                for polygon in geometry.iter(f"{GML}Polygon"):
                    append(polygon, kind)
    for geometry in building.iter():
        if _local(geometry.tag) in {"lod2Solid", "lod2MultiSurface"}:
            for polygon in geometry.iter(f"{GML}Polygon"):
                append(polygon, "Surface")
    return {"id": object_id, "polygons": polygons,
            "buildingPartCount": sum(_local(e.tag) == "BuildingPart" for e in building.iter())}


def connect_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tiles(
            name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, source_url TEXT NOT NULL,
            storage_uri TEXT NOT NULL, crs TEXT NOT NULL, report TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS buildings(
            pk INTEGER PRIMARY KEY, tile TEXT NOT NULL REFERENCES tiles(name),
            object_id TEXT NOT NULL, feature TEXT NOT NULL, UNIQUE(tile, object_id));
        CREATE VIRTUAL TABLE IF NOT EXISTS building_bounds USING rtree(
            pk, min_e, max_e, min_n, max_n);
        CREATE TRIGGER IF NOT EXISTS delete_building_bounds AFTER DELETE ON buildings
            BEGIN DELETE FROM building_bounds WHERE pk=old.pk; END;
    """)
    previous = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
    if previous and previous[0] != SCHEMA:
        db.close()
        raise ValueError("Unsupported CityGML store schema")
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema', ?)", (SCHEMA,))
    db.commit()
    return db


def import_zip(database: Path, source: Path, *, sha256: str,
               source_url: str, storage_uri: str, source_crs: str = SOURCE_CRS,
               horizontal_crs: str | None = None, vertical_crs: str | None = None) -> dict:
    if source_crs != SOURCE_CRS and (not horizontal_crs or not vertical_crs):
        raise ValueError("Non-default sources require explicit horizontal and vertical CRS")
    horizontal_crs = horizontal_crs or "EPSG:25833"
    vertical_crs = vertical_crs or "EPSG:7837"
    from pyproj import CRS
    CRS.from_user_input(horizontal_crs)
    CRS.from_user_input(vertical_crs)
    with source.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != sha256:
        raise ValueError("Source ZIP SHA-256 mismatch")
    db = connect_store(database)
    try:
        declared = dict(db.execute("SELECT key,value FROM metadata"))
        for key, value in (("horizontalCrs", horizontal_crs), ("verticalCrs", vertical_crs), ("sourceCrs", source_crs)):
            if declared.get(key, value) != value:
                raise ValueError("Source CRS must remain consistent within one store")
        if any(row[0] != source_crs for row in db.execute("SELECT DISTINCT crs FROM tiles")):
            raise ValueError("Source CRS must remain consistent within one store")
        existing = db.execute("SELECT sha256, report FROM tiles WHERE name=?", (source.name,)).fetchone()
        if existing and existing[0] == actual:
            return {**json.loads(existing[1]), "cache": "already-imported"}
        report = {"tile": source.name, "sha256": actual, "buildingCount": 0,
                  "buildingPartCount": 0, "polygonCount": 0, "emptyBuildingCount": 0}
        # On any CRC/XML/CRS/geometry error, rollback leaves the prior tile intact.
        with db, zipfile.ZipFile(source) as archive:
            db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", (
                ("horizontalCrs", horizontal_crs), ("verticalCrs", vertical_crs), ("sourceCrs", source_crs)))
            members = [m for m in archive.infolist() if not m.is_dir()]
            if not members or sum(m.file_size for m in members) > 2_147_483_648:
                raise ValueError("Empty or oversized CityGML ZIP")
            db.execute("DELETE FROM buildings WHERE tile=?", (source.name,))
            db.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?,?,?)",
                       (source.name, actual, source_url, storage_uri, source_crs, "{}"))
            for member in members:
                path = PurePosixPath(member.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts or path.suffix.lower() not in {".xml", ".gml"}:
                    raise ValueError("Unsafe/unexpected CityGML archive member")
                with archive.open(member) as stream:
                    root = None
                    crs_seen = False
                    for event, element in ET.iterparse(stream, events=("start", "end")):
                        if event == "start":
                            if root is None:
                                root = element
                                if element.tag not in {"{http://www.opengis.net/citygml/1.0}CityModel",
                                                       "{http://www.opengis.net/citygml/2.0}CityModel"}:
                                    raise ValueError("Unsupported CityModel")
                            srs = element.get("srsName")
                            if srs:
                                if srs != source_crs:
                                    raise ValueError(f"Unsupported source CRS: {srs}")
                                crs_seen = True
                            continue
                        if _local(element.tag) != "cityObjectMember":
                            continue
                        for building in element:
                            if _local(building.tag) != "Building":
                                continue
                            feature = building_feature(building)
                            report["buildingCount"] += 1
                            report["buildingPartCount"] += feature["buildingPartCount"]
                            report["polygonCount"] += len(feature["polygons"])
                            if not feature["polygons"]:
                                report["emptyBuildingCount"] += 1
                                continue
                            coords = [p for poly in feature["polygons"] for ring in poly["rings"] for p in ring]
                            cursor = db.execute("INSERT INTO buildings(tile,object_id,feature) VALUES(?,?,?)",
                                                (source.name, feature["id"], json.dumps(feature, separators=(",", ":"))))
                            db.execute("INSERT INTO building_bounds VALUES(?,?,?,?,?)",
                                       (cursor.lastrowid, min(p[0] for p in coords), max(p[0] for p in coords),
                                        min(p[1] for p in coords), max(p[1] for p in coords)))
                        element.clear()
                        root.clear()
                    if not crs_seen:
                        raise ValueError("CityModel has no declared source CRS")
            db.execute("UPDATE tiles SET report=? WHERE name=?", (json.dumps(report), source.name))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('revision', ?)",
                       (hashlib.sha256(json.dumps(db.execute("SELECT name,sha256 FROM tiles ORDER BY name").fetchall()).encode()).hexdigest(),))
        return report
    finally:
        db.close()


def query_buildings(database: Path, bbox: tuple[float, float, float, float], limit=200,
                    *, require_coverage=False) -> tuple[str, list[dict]]:
    """Read-only bbox query; an absent store is an explicit error, not empty data."""
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        # Keep coverage, revision and geometry on one snapshot during imports.
        db.execute("BEGIN")
        schema = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if not schema or schema[0] != SCHEMA:
            raise ValueError("Unsupported CityGML store schema")
        if require_coverage:
            available = {row[0] for row in db.execute("SELECT name FROM tiles")}
            expected = {f"LoD2_{east}_{north}.zip"
                        for east in range(math.floor(bbox[0] / 1000), math.floor(bbox[2] / 1000) + 1)
                        for north in range(math.floor(bbox[1] / 1000), math.floor(bbox[3] / 1000) + 1)}
            missing = expected - available
            if missing:
                raise ValueError("LoD2 source tile not imported: " + ", ".join(sorted(missing)))
        revision = db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
        rows = db.execute("""SELECT b.feature, t.name, t.sha256 FROM building_bounds r
            JOIN buildings b ON b.pk=r.pk JOIN tiles t ON t.name=b.tile
            WHERE r.max_e>=? AND r.min_e<=? AND r.max_n>=? AND r.min_n<=?
            ORDER BY b.object_id, t.name LIMIT ?""", (bbox[0], bbox[2], bbox[1], bbox[3], limit + 1)).fetchall()
        if len(rows) > limit:
            raise ValueError("Building query limit exceeded; reduce the requested tile size")
        features = [{**json.loads(row[0]), "sourceTile": row[1], "sourceSha256": row[2]} for row in rows]
        return revision[0] if revision else "empty", features


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--storage-uri", required=True)
    parser.add_argument("--source-crs", default=SOURCE_CRS)
    parser.add_argument("--horizontal-crs")
    parser.add_argument("--vertical-crs")
    args = parser.parse_args()
    print(json.dumps(import_zip(args.database, args.zip, sha256=args.sha256,
                                source_url=args.source_url, storage_uri=args.storage_uri, source_crs=args.source_crs,
                                horizontal_crs=args.horizontal_crs, vertical_crs=args.vertical_crs)))
