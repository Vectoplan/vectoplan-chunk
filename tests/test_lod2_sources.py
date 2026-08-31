import json
import math
from types import SimpleNamespace

import pytest
from pyproj import Transformer
from src.geodata.citygml_store import import_zip
from src.geodata.lod2_buildings import append_building_overlay, building_overlay_item, world_lod2_config
from src.geodata.lod2_sources import sources_for_world, source_availability, vertical_metres_per_unit
from src.georeferencing.frame_contract import earth_grid_frame_contract
from test_lod2_buildings import imported, source_zip, provider


def at(lon, lat):
    value = provider()
    value.frame.storage_origin.x = math.floor(lon*40_000_000/360/16)*16
    value.frame.storage_origin.z = math.floor(lat*20_000_000/180/16)*16
    return value


@pytest.mark.parametrize('lon,lat', [(8.68,50.11), (-74,40.7), (139.7,35.7), (151.2,-33.9), (179.999,0), (-179.999,0)])
def test_other_projects_never_get_berlin_fallback(tmp_path, monkeypatch, lon, lat):
    database, *_ = imported(tmp_path)
    monkeypatch.setenv('VECTOPLAN_CHUNK_LOD2_STORE',str(database))
    world = SimpleNamespace(is_earth_world=True,metadata_json={},surface_y=0)
    contract = {'status':'ready','items':[]}
    append_building_overlay(contract,world=world,provider=at(lon,lat),chunk={})
    assert contract['items'] == [] and contract['status'] == 'ready'
    assert contract['availability'][0]['status'] == 'outside-available-coverage'


def test_earth_auto_discovery_and_explicit_opt_out():
    assert world_lod2_config(SimpleNamespace(is_earth_world=True,metadata_json={}))['mode'] == 'available-sources'
    assert world_lod2_config(SimpleNamespace(is_earth_world=False,metadata_json={})) is None
    assert world_lod2_config(SimpleNamespace(is_earth_world=True,metadata_json={'lod2Buildings':{'enabled':False}})) is None


def test_missing_source_is_normal_no_data_and_never_creates_a_store(tmp_path,monkeypatch):
    path=tmp_path/'not-downloaded.sqlite3';monkeypatch.setenv('VECTOPLAN_CHUNK_LOD2_STORE',str(path))
    contract={'status':'ready','items':[{'id':'parcels'}]}
    append_building_overlay(contract,world=SimpleNamespace(is_earth_world=True,metadata_json={}),provider=provider(),chunk={})
    assert contract['items']==[{'id':'parcels'}] and contract['status']=='ready'
    assert contract['availability'][0]['status']=='not-downloaded' and not path.exists()


def test_partial_download_and_corrupt_other_source_keep_available_buildings(tmp_path,monkeypatch):
    database,*_=imported(tmp_path)
    default=sources_for_world({})[0]
    def source(id,path):return {'id':id,'path':str(path),'horizontalCrs':default.horizontal_crs,'verticalCrs':default.vertical_crs,'sourceCrs':default.source_crs}
    bad=tmp_path/'bad.sqlite3';bad.write_bytes(b'not sqlite')
    monkeypatch.setenv('VECTOPLAN_CHUNK_LOD2_SOURCES_JSON',json.dumps([source('broken',bad),source('available',database)]))
    world=SimpleNamespace(metadata_json={'lod2Buildings':{'enabled':True}},surface_y=0)
    contract={'status':'ready','items':[{'id':'parcels'}]}
    append_building_overlay(contract,world=world,provider=provider(),chunk={'terrain':{'anchorElevationM':30},'chunkSize':32})
    assert contract['status']=='degraded' and len(contract['items'])==2
    assert contract['items'][1]['geometry']['features'][0]['id']=='available:berlin-1'
    assert contract['availability'][0]['status']=='source-error'
    assert len(sources_for_world({'sourceIds':['available']}))==1


def test_generic_projected_source_in_another_country_uses_its_own_crs(tmp_path,monkeypatch):
    source,digest=source_zip(tmp_path,crs='EPSG:32654')
    database=tmp_path/'japan.sqlite3'
    import_zip(database,source,sha256=digest,source_url='fixture',storage_uri='fixture',
               source_crs='EPSG:32654',horizontal_crs='EPSG:32654',vertical_crs='EPSG:5773')
    monkeypatch.setenv('VECTOPLAN_CHUNK_LOD2_SOURCES_JSON',json.dumps([{'id':'japan-fixture','path':str(database),
        'sourceCrs':'EPSG:32654','horizontalCrs':'EPSG:32654','verticalCrs':'EPSG:5773'}]))
    lon,lat=Transformer.from_crs('EPSG:32654','EPSG:4326',always_xy=True).transform(391010,5820010)
    item=building_overlay_item(world=SimpleNamespace(metadata_json={'lod2Buildings':{'enabled':True}},surface_y=0),
         provider=at(lon,lat),chunk={'terrain':{'anchorElevationM':30,'verticalCrs':'EPSG:5773'}})
    assert item['source']['horizontalCrs']=='EPSG:32654'
    assert item['geometry']['features'][0]['polygons'][0]['rings'][0][0][1]==11
    with pytest.raises(ValueError,match='vertical'):
        building_overlay_item(world=SimpleNamespace(metadata_json={'lod2Buildings':{'enabled':True}},surface_y=0),
            provider=at(lon,lat),chunk={'terrain':{'anchorElevationM':30,'verticalCrs':'EPSG:7837'}})


def test_auto_mode_does_not_guess_ground_from_roof_only_data(tmp_path,monkeypatch):
    database,*_=imported(tmp_path);monkeypatch.setenv('VECTOPLAN_CHUNK_LOD2_STORE',str(database))
    item=building_overlay_item(world=SimpleNamespace(is_earth_world=True,metadata_json={},surface_y=0),provider=provider(),chunk={})
    assert item['geometry']['features']==[]


def test_catalog_refuses_arbitrary_relative_paths(monkeypatch):
    monkeypatch.setenv('VECTOPLAN_CHUNK_LOD2_SOURCES_JSON',json.dumps([{'id':'x','path':'relative','horizontalCrs':'EPSG:25833','verticalCrs':'EPSG:7837','sourceCrs':'x'}]))
    with pytest.raises(ValueError,match='absolute'):sources_for_world({})


def test_vertical_units_are_explicit_not_assumed_metres():
    assert vertical_metres_per_unit('EPSG:7837')==1
    assert vertical_metres_per_unit('EPSG:6360')==pytest.approx(1200/3937) # US survey feet
    with pytest.raises(ValueError,match='height axis'):vertical_metres_per_unit('EPSG:4326')
