import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from src.geodata.citygml_store import SOURCE_CRS, import_zip, query_buildings
from src.geodata.lod2_buildings import (
    UTM_TO_WGS84, _world_xz, append_building_overlay, building_overlay_item,
)
from src.georeferencing.frame_contract import earth_grid_frame_contract


def source_zip(tmp_path, *, crs=SOURCE_CRS, height=40, member="lod2.xml"):
    xml = f'''<core:CityModel xmlns:core="http://www.opengis.net/citygml/1.0"
        xmlns:b="http://www.opengis.net/citygml/building/1.0" xmlns:gml="http://www.opengis.net/gml"
        xmlns:xlink="http://www.w3.org/1999/xlink">
        <gml:boundedBy><gml:Envelope srsName="{crs}"/></gml:boundedBy>
        <core:cityObjectMember><b:Building gml:id="berlin-1">
          <b:lod2Solid><gml:Solid><gml:surfaceMember xlink:href="#roof"/></gml:Solid></b:lod2Solid>
          <b:consistsOfBuildingPart><b:BuildingPart gml:id="part-1">
            <b:boundedBy><b:RoofSurface><b:lod2MultiSurface><gml:MultiSurface>
              <gml:surfaceMember><gml:Polygon gml:id="roof"><gml:exterior><gml:LinearRing>
                <gml:posList>391005 5820005 {height} 391015 5820005 {height} 391015 5820015 {height} 391005 5820015 {height} 391005 5820005 {height}</gml:posList>
              </gml:LinearRing></gml:exterior><gml:interior><gml:LinearRing>
                <gml:posList>391008 5820008 {height} 391012 5820008 {height} 391012 5820012 {height} 391008 5820012 {height} 391008 5820008 {height}</gml:posList>
              </gml:LinearRing></gml:interior></gml:Polygon></gml:surfaceMember>
            </gml:MultiSurface></b:lod2MultiSurface></b:RoofSurface></b:boundedBy>
          </b:BuildingPart></b:consistsOfBuildingPart>
        </b:Building></core:cityObjectMember></core:CityModel>'''
    path = tmp_path / "LoD2_391_5820.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, xml)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def imported(tmp_path):
    source, digest = source_zip(tmp_path)
    database = tmp_path / "lod2.sqlite3"
    report = import_zip(database, source, sha256=digest, source_url="https://gdi.berlin.de/fixture", storage_uri="s3://raw/fixture")
    return database, source, digest, report


def test_import_preserves_holes_parts_provenance_and_deduplicates_solid(tmp_path):
    database, source, digest, report = imported(tmp_path)
    assert report == {"tile": source.name, "sha256": digest, "buildingCount": 1,
                      "buildingPartCount": 1, "polygonCount": 1, "emptyBuildingCount": 0}
    revision, buildings = query_buildings(database, (391000, 5820000, 391020, 5820020))
    assert len(revision) == 64
    assert len(buildings) == 1
    assert buildings[0]["sourceSha256"] == digest
    assert len(buildings[0]["polygons"][0]["rings"]) == 2
    assert query_buildings(database, (390000, 5800000, 390100, 5800100))[1] == []
    assert import_zip(database, source, sha256=digest, source_url="", storage_uri="")["cache"] == "already-imported"


def test_checksum_and_unsupported_crs_do_not_replace_good_tile(tmp_path):
    database, source, digest, _ = imported(tmp_path)
    before = query_buildings(database, (391000, 5820000, 391020, 5820020))
    with pytest.raises(ValueError, match="SHA-256"):
        import_zip(database, source, sha256="0" * 64, source_url="", storage_uri="")
    source, wrong_digest = source_zip(tmp_path, crs="EPSG:4326")
    with pytest.raises(ValueError, match="Unsupported source CRS"):
        import_zip(database, source, sha256=wrong_digest, source_url="", storage_uri="")
    assert query_buildings(database, (391000, 5820000, 391020, 5820020)) == before


def test_archive_paths_are_not_extracted_or_accepted(tmp_path):
    source, digest = source_zip(tmp_path, member="../escape.xml")
    with pytest.raises(ValueError, match="Unsafe"):
        import_zip(tmp_path / "lod2.sqlite3", source, sha256=digest, source_url="", storage_uri="")
    assert not (tmp_path.parent / "escape.xml").exists()


def provider():
    lon, lat = UTM_TO_WGS84.transform(391010, 5820010)
    return SimpleNamespace(reference_fingerprint="fixture-reference",
        grid_definition=SimpleNamespace(world_width_cells=40_000_000, world_height_cells=20_000_000,
                                        meters_per_cell=1, central_meridian_deg=0),
        frame=SimpleNamespace(storage_origin=SimpleNamespace(
            x=math.floor(lon * 40_000_000 / 360 / 16) * 16, y=0,
            z=math.floor(lat * 20_000_000 / 180 / 16) * 16)))


def test_world_transform_uses_same_terrain_height_and_retains_full_roof(tmp_path, monkeypatch):
    database, *_ = imported(tmp_path)
    monkeypatch.setenv("VECTOPLAN_CHUNK_LOD2_STORE", str(database))
    world = SimpleNamespace(metadata_json={"lod2Buildings": {"enabled": True}}, surface_y=0)
    chunk = {"chunkX": 0, "chunkZ": 0, "chunkSize": 16, "terrain": {"anchorElevationM": 30, "releaseKey": "dgm"}}
    item = building_overlay_item(world=world, provider=provider(), chunk=chunk)
    feature = item["geometry"]["features"][0]
    ring = feature["polygons"][0]["rings"][0]
    assert all(p[1] == 11 for p in ring)  # 40m normal height - 30m DGM anchor + solid face
    assert len(feature["polygons"][0]["rings"]) == 2
    lon, lat = UTM_TO_WGS84.transform(391005, 5820005)
    x, z = _world_xz(earth_grid_frame_contract(provider()), lon, lat)
    assert ring[0][0] == pytest.approx(x, abs=.0001)
    assert ring[0][2] == pytest.approx(z, abs=.0001)
    other_y = building_overlay_item(world=world, provider=provider(), chunk={**chunk, "chunkY": 3})
    assert other_y == item  # frontend deduplicates complete buildings across Y levels


def test_disabled_is_opt_in_and_missing_height_degrades_without_losing_parcels(tmp_path, monkeypatch):
    database, *_ = imported(tmp_path)
    monkeypatch.setenv("VECTOPLAN_CHUNK_LOD2_STORE", str(database))
    world = SimpleNamespace(metadata_json={}, surface_y=0)
    assert building_overlay_item(world=world, provider=None, chunk={}) is None
    world.metadata_json = {"lod2Buildings": {"enabled": True}}
    contract = {"status": "ready", "items": [{"id": "parcels"}], "errors": []}
    append_building_overlay(contract, chunk={}, world=world, provider=provider())
    assert contract["status"] == "degraded"
    assert contract["items"] == [{"id": "parcels"}]
    assert "DGM" in contract["errors"][0]["message"]


def test_flat_terrain_fallback_never_displays_misregistered_buildings():
    world = SimpleNamespace(metadata_json={"lod2Buildings": {"enabled": True}}, surface_y=0)
    with pytest.raises(ValueError, match="flat fallback"):
        building_overlay_item(world=world, provider=provider(), chunk={"terrain": {"fallback": True, "anchorElevationM": 30}})


def test_explicit_flat_test_mode_preserves_original_elevation_as_metadata(tmp_path, monkeypatch):
    database, *_ = imported(tmp_path)
    monkeypatch.setenv("VECTOPLAN_CHUNK_LOD2_STORE", str(database))
    world = SimpleNamespace(metadata_json={"lod2Buildings": {"enabled": True, "allowFlatTerrainAlignment": True}}, surface_y=0)
    item = building_overlay_item(world=world, provider=provider(), chunk={"terrain": {"fallback": True}})
    assert item["heightReference"]["absoluteHeightPreserved"] is False
    assert item["heightReference"]["kind"] == "source-ground-relative-to-flat-terrain"
    feature = item["geometry"]["features"][0]
    assert feature["baseElevationM"] == 40
    assert feature["polygons"][0]["rings"][0][0][1] == 1
    dgm = building_overlay_item(world=world, provider=provider(), chunk={"terrain": {"anchorElevationM": 30}})
    assert dgm["heightReference"]["absoluteHeightPreserved"] is True


def test_unimported_coverage_is_not_reported_as_no_buildings(tmp_path):
    database, *_ = imported(tmp_path)
    with pytest.raises(ValueError, match="source tile not imported"):
        query_buildings(database, (390000, 5800000, 390100, 5800100), require_coverage=True)
