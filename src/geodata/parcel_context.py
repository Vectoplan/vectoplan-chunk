"""One bounded parcel neighbourhood per project location, independent of zoom."""
import math

PARCEL_RADIUS_METERS = 400
PARCEL_FEATURE_LIMIT = 1000


def neighbourhood_bounds(longitude, latitude):
    try:
        lon, lat = float(longitude), float(latitude)
    except (TypeError, ValueError):
        raise ValueError("A finite project longitude and latitude are required") from None
    if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -85 <= lat <= 85:
        raise ValueError("Project coordinate is outside the supported map area")
    delta = math.degrees(PARCEL_RADIUS_METERS / 6378137)
    dx = delta / math.cos(math.radians(lat))
    return [max(-180, lon-dx), lat-delta, min(180, lon+dx), lat+delta]


def parcel_intersects_circle(geometry, circle):
    """Match OpenLayer's ground-distance neighbourhood, including polygon holes."""
    lon, lat, radius = circle
    scale = math.pi / 180 * 6378137
    scale_x = scale * math.cos(math.radians(lat))
    if not isinstance(geometry, dict):
        return False
    polygons = geometry.get("coordinates") or []
    if geometry.get("type") == "Polygon":
        polygons = [polygons]
    elif geometry.get("type") != "MultiPolygon":
        return False
    def local_ring(ring):
        points = []
        for point in ring:
            try:
                x, y = (float(point[0])-lon)*scale_x, (float(point[1])-lat)*scale
                if math.isfinite(x) and math.isfinite(y):
                    points.append((x, y))
            except (TypeError, ValueError, IndexError):
                continue
        return points
    def contains_origin(ring):
        inside = False
        for a, b in zip(ring, ring[1:] + ring[:1]):
            if (a[1] > 0) != (b[1] > 0) and a[0] + (b[0]-a[0]) * -a[1] / (b[1]-a[1]) > 0:
                inside = not inside
        return inside
    def touches(ring):
        if any(x*x+y*y <= radius*radius for x, y in ring):
            return True
        for a, b in zip(ring, ring[1:] + ring[:1]):
            dx, dy = b[0]-a[0], b[1]-a[1]
            length_sq = dx*dx + dy*dy
            t = max(0, min(1, -(a[0]*dx+a[1]*dy)/length_sq)) if length_sq else 0
            if (a[0]+t*dx)**2 + (a[1]+t*dy)**2 <= radius*radius:
                return True
        return False
    for polygon in polygons:
        rings = [local_ring(ring) for ring in polygon]
        if rings and (any(touches(ring) for ring in rings) or
                      (contains_origin(rings[0]) and not any(contains_origin(hole) for hole in rings[1:]))):
            return True
    return False


def parcel_context_neighbourhood(world, longitude, latitude):
    from src.geodata.visual_overlays import get_default_geodata_overlay_service
    bounds = neighbourhood_bounds(longitude, latitude)
    if not getattr(world, "is_earth_world", False):
        raise ValueError("Parcel context requires an Earth world")
    provider = world.build_earth_provider()
    contract = get_default_geodata_overlay_service().parcel_region_contract(
        world=world, provider=provider, bounds=bounds,
        geographic_circle=(float(longitude), float(latitude), PARCEL_RADIUS_METERS))
    overlays = contract.get("items") or []
    return {"bounds": bounds, "coordinateFrame": contract.get("earthGrid"),
            "radiusMeters": PARCEL_RADIUS_METERS, "featureLimit": PARCEL_FEATURE_LIMIT,
            "featureCount": sum(item.get("stats", {}).get("featureCount", 0) for item in overlays),
            "limited": any(item.get("stats", {}).get("limited", False) for item in overlays),
            "cellSizeMeters": float(world.cell_size or 1), "overlays": overlays,
            "errors": contract.get("errors") or [], "availability": contract.get("availability") or []}
