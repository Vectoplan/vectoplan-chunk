from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from urllib.error import URLError

from src.geodata.visual_overlays import (
    GeodataOverlayService,
    OverlayPipelineConfig,
    effective_overlay_definitions,
)


class _World:
    is_earth_world = True
    chunk_size = 16

    def __init__(self, metadata=None) -> None:
        self.metadata_json = metadata or {}


class _Provider:
    reference_fingerprint = "test-reference"

    def __init__(self) -> None:
        self.grid_definition = SimpleNamespace(
            world_width_cells=40_000_000,
            world_height_cells=20_000_000,
            meters_per_cell=Decimal("1"),
            central_meridian_deg=Decimal("0"),
        )
        self.frame = SimpleNamespace(
            storage_origin=SimpleNamespace(x=1_440_000, y=0, z=5_776_000)
        )

    def local_to_global(self, position, *, target_crs):
        return SimpleNamespace(
            target_coordinate=SimpleNamespace(
                x=Decimal("13") + (Decimal(position.x) / Decimal("100000")),
                y=Decimal("52") + (Decimal(position.z) / Decimal("100000")),
                z=Decimal("0"),
            )
        )

    def global_to_local(self, coordinate, source_crs):
        return SimpleNamespace(
            local_position=SimpleNamespace(
                x=(Decimal(coordinate.x) - Decimal("13")) * Decimal("100000"),
                y=Decimal("0"),
                z=(Decimal(coordinate.y) - Decimal("52")) * Decimal("100000"),
            )
        )


class _Orchestrator:
    def approved_publication(self, dataset_id: str):
        return {
            "dataset_id": dataset_id,
            "release_key": "release-1",
        }


class _Wfs:
    def feature_collection(self, definition, bbox):
        # Two identical polygons exercise shared/duplicate segment removal.
        ring = [
            [13.0, 52.0],
            [13.00016, 52.0],
            [13.00016, 52.00016],
            [13.0, 52.00016],
            [13.0, 52.0],
        ]
        return {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}},
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}},
            ],
        }


def _service() -> GeodataOverlayService:
    config = OverlayPipelineConfig(
        enabled=True,
        orchestrator_base_url="http://orchestrator",
        geoserver_base_url="http://geoserver/geoserver",
        service_token="test-token",
        request_timeout_seconds=1.0,
        version_cache_seconds=60.0,
        tile_cache_seconds=60.0,
    )
    return GeodataOverlayService(
        config,
        orchestrator=_Orchestrator(),
        wfs=_Wfs(),
    )


def test_cad_parcels_follow_geographic_viewport_instead_of_fixed_world_origin(monkeypatch):
    monkeypatch.delenv('VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON', raising=False)
    service = _service()
    calls = []
    class RegionWfs:
        def feature_collection(self, definition, bbox):
            calls.append((definition.overlay_id, bbox))
            return {'type': 'FeatureCollection', 'features': [{'geometry': {'type': 'Polygon', 'coordinates': [[
                [13.009, 52.009], [13.011, 52.009], [13.011, 52.011], [13.009, 52.011], [13.009, 52.009]] ]}}]}
    service.wfs = RegionWfs()
    bounds = (13.01, 52.01, 13.012, 52.012)
    result = service.parcel_region_contract(world=_World(), provider=_Provider(), bounds=bounds)
    assert len(calls) == 1 and calls[0][0] == 'parcel-boundaries'
    import pytest
    assert calls[0][1] == pytest.approx(bounds)
    lines = result['items'][0]['geometry']['coordinates']
    assert len(lines) == 2  # original boundary parts only, no invented tile-edge rectangle
    assert all(1000 <= v <= 1200 for line in lines for p in line for v in p)
    service.parcel_region_contract(world=_World(), provider=_Provider(), bounds=bounds)
    assert len(calls) == 1, 'regional WFS result reused'
    service.parcel_region_contract(world=_World(), provider=_Provider(), bounds=(13.02, 52.02, 13.022, 52.022))
    assert len(calls) == 2, 'pan changes the cache key and requested area'


def test_wfs_count_is_a_total_budget_even_when_more_parcels_match():
    from src.geodata.visual_overlays import GeoServerWfsClient
    from dataclasses import replace
    service = _service()
    client = GeoServerWfsClient(service.config)
    definition = replace(effective_overlay_definitions(_World())[0], max_features=2)
    requests = []
    def page(workspace, params):
        requests.append(dict(params))
        return {'type': 'FeatureCollection', 'numberMatched': 5,
                'features': [{'id': str(i)} for i in range(2)]}
    client._page = page
    result = client.feature_collection(definition, (13, 52, 13.1, 52.1))
    assert len(requests) == 1
    assert requests[0]['count'] == '2' and 'startIndex' not in requests[0]
    assert len(result['features']) == result['numberReturned'] == 2
    assert result['limited']


def test_parcel_limit_cannot_be_raised_by_metadata_or_noncompliant_wfs():
    from src.geodata.visual_overlays import GeoServerWfsClient, OverlayDefinition
    client = GeoServerWfsClient(_service().config)
    client._page = lambda *args: {'type': 'FeatureCollection', 'numberMatched': 4000,
                                 'features': [{'id': str(i)} for i in range(1500)]}
    definition = OverlayDefinition.from_mapping({'id': 'parcel-boundaries', 'datasetId': 'flurstuecke',
                                                  'source': {'maxFeatures': 100000}})
    result = client.feature_collection(definition, (13, 52, 13.1, 52.1))
    assert definition.max_features == 1000
    assert len(result['features']) == 1000 and result['limited']


def test_large_region_failure_does_not_block_smaller_viewports(monkeypatch):
    from src.geodata.visual_overlays import GeodataOverlayExtentError
    monkeypatch.delenv('VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON', raising=False)
    service = _service()
    calls = []
    def read(definition, bbox):
        calls.append(bbox)
        if len(calls) == 1:
            raise GeodataOverlayExtentError('region too large')
        return _Wfs().feature_collection(definition, bbox)
    service.wfs.feature_collection = read
    first = service.parcel_region_contract(world=_World(), provider=_Provider(), bounds=(13, 52, 13.1, 52.1))
    second = service.parcel_region_contract(world=_World(), provider=_Provider(), bounds=(13, 52, 13.00016, 52.00016))
    assert first['errors'] and not second['errors']
    assert len(calls) == 2 and second['items']


def test_adjacent_tiles_reuse_exact_vertices_but_never_another_reference_frame(monkeypatch):
    monkeypatch.delenv('VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON', raising=False)
    service = _service()
    provider = _Provider()
    calls = []
    original = provider.global_to_local
    def project(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    provider.global_to_local = project
    first = service.chunk_contract(world=_World(), provider=provider, chunk_x=0, chunk_z=0, chunk_size=16)
    assert len(calls) == 4  # two identical closed rings, two overlay definitions
    service.chunk_contract(world=_World(), provider=provider, chunk_x=1, chunk_z=0, chunk_size=16)
    assert len(calls) == 4
    provider.reference_fingerprint = 'another-earth-frame'
    second = service.chunk_contract(world=_World(), provider=provider, chunk_x=0, chunk_z=0, chunk_size=16)
    assert len(calls) == 8
    assert [i['geometry'] for i in first['items']] == [i['geometry'] for i in second['items']]


def test_unknown_reference_is_not_cached():
    service = _service()
    first = _Provider()
    second = _Provider()
    second.global_to_local = lambda *_: SimpleNamespace(local_position=SimpleNamespace(x=1, z=2))
    assert service._project_vertex(first, 'unknown-reference', 13, 52) == (0, 0)
    assert service._project_vertex(second, 'unknown-reference', 13, 52) == (1, 2)


def test_unavailable_publication_is_negative_cached_but_other_layers_survive(monkeypatch):
    monkeypatch.delenv('VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON', raising=False)
    service = _service()
    calls = []
    class Missing:
        def approved_publication(self, dataset_id):
            calls.append(dataset_id)
            raise RuntimeError('not released')
    service.orchestrator = Missing()
    world = _World({'geodataOverlays': [{'id':'unavailable','datasetId':'missing','workspace':'vectoplan','typeName':'missing',
        'enabled':True,'renderMode':'surface-lines','versionPolicy':'approved-release'}]})
    # Use the actual definition schema through the dedicated environment catalog.
    import json
    monkeypatch.setenv('VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON', json.dumps([
        {'id':'live','datasetId':'flurstuecke','source':{'workspace':'vectoplan','typeName':'parcels','versionPolicy':'wfs-live'},'renderer':{'kind':'surface-lines'}},
        {'id':'missing','datasetId':'missing','source':{'workspace':'vectoplan','typeName':'missing','versionPolicy':'approved-release'},'renderer':{'kind':'surface-lines'}}]))
    world.metadata_json = {}
    first = service.chunk_contract(world=world,provider=_Provider(),chunk_x=0,chunk_z=0,chunk_size=16)
    second = service.chunk_contract(world=world,provider=_Provider(),chunk_x=1,chunk_z=0,chunk_size=16)
    assert len(calls)==1
    assert first['status']=='degraded' and second['status']=='degraded'
    assert first['items'] and first['items'][0]['datasetId']=='flurstuecke'


def test_default_parcel_overlay_is_clipped_live_and_deduplicated(monkeypatch):
    monkeypatch.delenv("VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON", raising=False)

    contract = _service().chunk_contract(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_z=0,
        chunk_size=16,
    )

    assert contract["schemaVersion"] == "geodata-overlays.v1"
    assert contract["status"] == "ready"
    assert contract["referenceFingerprint"] == "test-reference"
    assert contract["earthGrid"] == {
        "schemaVersion": "vectoplan-earth-grid-frame.v1",
        "horizontalMapping": "vectoplan-periodic-equirectangular",
        "mappingVersion": "1",
        "axisConvention": "x-east-y-up-z-north",
        "worldWidthCells": 40_000_000,
        "worldHeightCells": 20_000_000,
        "metersPerCell": 1.0,
        "centralMeridianDegrees": 0.0,
        "storageOrigin": {"x": 1_440_000, "y": 0, "z": 5_776_000},
    }
    assert len(contract["items"]) == 2

    parcel = next(item for item in contract["items"] if item["id"] == "parcel-boundaries")
    assert parcel["id"] == "parcel-boundaries"
    assert parcel["datasetId"] == "flurstuecke"
    assert parcel["releaseKey"] == "live:public:public:flurstuecke"
    assert parcel["renderMode"] == "surface-lines"
    assert parcel["semanticRole"] == "parcel-boundary"
    assert parcel["geometry"]["dimensions"] == "world-xz"
    assert len(parcel["geometry"]["coordinates"]) == 4
    assert parcel["stats"]["featureCount"] == 2
    assert parcel["stats"]["sourceSegmentCount"] == 8
    assert parcel["stats"]["emittedSegmentCount"] == 4

    street = next(item for item in contract["items"] if item["id"] == "street-network")
    assert street["datasetId"] == "strassendaten"
    assert street["releaseKey"] == "live:public:public:strassendaten"
    assert street["renderMode"] == "surface-ribbons"
    assert street["semanticRole"] == "street-network"
    assert street["classificationSource"] is True
    assert street["style"]["surfaceWidth"] == 6.0
    assert street["style"]["color"] == "#fbfcfd"


def test_approved_release_policy_uses_orchestrator_version(monkeypatch):
    monkeypatch.setenv(
        "VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON",
        '[{"id":"approved-parcels","datasetId":"flurstuecke",'
        '"source":{"workspace":"public","typeName":"public:flurstuecke",'
        '"versionPolicy":"approved-release"},'
        '"renderer":{"kind":"surface-lines"}}]',
    )

    contract = _service().chunk_contract(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_z=0,
        chunk_size=16,
    )

    assert contract["status"] == "ready"
    assert contract["items"][0]["releaseKey"] == "release-1"
    assert contract["items"][0]["source"]["versionPolicy"] == "approved-release"


def test_style_change_invalidates_live_tile_contract_cache(monkeypatch):
    monkeypatch.delenv("VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON", raising=False)
    service = _service()

    first = service.chunk_contract(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_z=0,
        chunk_size=16,
    )
    changed_world = _World(
        {
            "geodataOverlays": {
                "items": [
                    {
                        "id": "parcel-boundaries",
                        "renderer": {"style": {"color": "#00aaff"}},
                    }
                ]
            }
        }
    )
    second = service.chunk_contract(
        world=changed_world,
        provider=_Provider(),
        chunk_x=0,
        chunk_z=0,
        chunk_size=16,
    )

    assert first["items"][0]["style"]["color"] == "#ffd54f"
    assert second["items"][0]["style"]["color"] == "#00aaff"


def test_world_metadata_can_replace_overlay_project_style_and_semantics(monkeypatch):
    monkeypatch.delenv("VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON", raising=False)
    world = _World(
        {
            "geodataOverlays": {
                "inheritDefaults": False,
                "items": [
                    {
                        "id": "street-foundation",
                        "datasetId": "strassendaten",
                        "source": {
                            "workspace": "public",
                            "typeName": "public:strassendaten",
                        },
                        "renderer": {
                            "kind": "surface-lines",
                            "style": {"color": "#ff00aa", "lineWidth": 2.5},
                        },
                        "semantics": {
                            "role": "street-network",
                            "classificationSource": True,
                        },
                    }
                ],
            }
        }
    )

    definitions = effective_overlay_definitions(world)

    assert len(definitions) == 1
    assert definitions[0].dataset_id == "strassendaten"
    assert definitions[0].type_name == "public:strassendaten"
    assert definitions[0].color == "#ff00aa"
    assert definitions[0].line_width == 2.5
    assert definitions[0].semantic_role == "street-network"
    assert definitions[0].classification_source is True


def test_overlay_source_failure_does_not_fail_chunk_contract(monkeypatch):
    monkeypatch.delenv("VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON", raising=False)

    class _FailingWfs:
        def feature_collection(self, definition, bbox):
            raise RuntimeError("wfs unavailable")

    service = _service()
    service.wfs = _FailingWfs()
    contract = service.chunk_contract(
        world=_World(),
        provider=_Provider(),
        chunk_x=0,
        chunk_z=0,
        chunk_size=16,
    )

    assert contract["status"] == "degraded"
    assert contract["items"] == []
    assert contract["errors"][0]["id"] == "parcel-boundaries"
    assert "wfs unavailable" in contract["errors"][0]["message"]


def test_remote_outage_opens_one_global_circuit_for_all_optional_layers(monkeypatch):
    monkeypatch.setenv(
        "VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON",
        '[{"id":"one","datasetId":"one","source":{"workspace":"public","typeName":"public:one"},"renderer":{"kind":"surface-lines"}},'
        '{"id":"two","datasetId":"two","source":{"workspace":"public","typeName":"public:two"},"renderer":{"kind":"surface-lines"}}]',
    )
    calls = []

    class _OfflineWfs:
        def feature_collection(self, definition, bbox):
            calls.append(definition.dataset_id)
            raise URLError("connection refused")

    service = _service()
    service.wfs = _OfflineWfs()
    first = service.chunk_contract(world=_World(), provider=_Provider(), chunk_x=0, chunk_z=0, chunk_size=16)
    second = service.chunk_contract(world=_World(), provider=_Provider(), chunk_x=1, chunk_z=0, chunk_size=16)

    assert calls == ["one"]
    assert first["status"] == second["status"] == "degraded"
    assert first["items"] == second["items"] == []
    assert {item["id"] for item in first["availability"]} == {"one", "two"}
