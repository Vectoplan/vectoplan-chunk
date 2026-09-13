import math
import pytest
from src.geodata.parcel_context import neighbourhood_bounds, parcel_context_neighbourhood
from types import SimpleNamespace
from unittest.mock import Mock, patch


def test_circle_excludes_rectangle_corners_and_respects_holes_and_crossing_edges():
    from src.geodata.parcel_context import parcel_intersects_circle
    scale = 180 / math.pi / 6378137
    def polygon(*rings):
        return {'type': 'Polygon', 'coordinates': [[[x*scale, y*scale] for x, y in ring] for ring in rings]}
    circle = (0, 0, 400)
    assert not parcel_intersects_circle(polygon([(350,350),(390,350),(390,390),(350,350)]), circle)
    assert parcel_intersects_circle(polygon([(-500,10),(500,10),(500,20),(-500,10)]), circle)
    outer = [(-1000,-1000),(1000,-1000),(1000,1000),(-1000,1000),(-1000,-1000)]
    hole = [(-500,-500),(500,-500),(500,500),(-500,500),(-500,-500)]
    assert parcel_intersects_circle(polygon(outer), circle)
    assert not parcel_intersects_circle(polygon(outer,hole), circle)


def test_neighbourhood_is_fixed_at_project_location_and_cannot_take_zoom_or_radius():
    lon, lat = 13.4037789, 52.5200908
    west, south, east, north = neighbourhood_bounds(lon, lat)
    assert (west + east) / 2 == pytest.approx(lon)
    assert (south + north) / 2 == pytest.approx(lat)
    assert math.radians(east - lon) * 6378137 * math.cos(math.radians(lat)) == pytest.approx(400)
    assert math.radians(north - lat) * 6378137 == pytest.approx(400)
    assert neighbourhood_bounds(lon + .01, lat) != [west, south, east, north]


@pytest.mark.parametrize('lon,lat', [(None, 52), ('nan', 52), (13, float('inf')), (181, 52), (13, 90)])
def test_invalid_neighbourhood_never_reaches_wfs(lon, lat):
    with pytest.raises(ValueError):
        neighbourhood_bounds(lon, lat)


def test_one_region_query_reports_total_budget_and_keeps_authoritative_frame():
    service = Mock()
    service.parcel_region_contract.return_value = {'earthGrid': {'storageOrigin': {'x': 123}},
        'items': [{'stats': {'featureCount': 1000, 'limited': True}}]}
    world = SimpleNamespace(is_earth_world=True, cell_size=1, build_earth_provider=lambda: 'provider')
    with patch('src.geodata.visual_overlays.get_default_geodata_overlay_service', return_value=service):
        result = parcel_context_neighbourhood(world, 13.4, 52.52)
    assert service.parcel_region_contract.call_count == 1
    assert result['featureCount'] == result['featureLimit'] == 1000
    assert result['radiusMeters'] == 400 and result['limited']
    assert result['coordinateFrame']['storageOrigin']['x'] == 123
