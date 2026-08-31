"""Connect roof surfaces in 3D, not merely by their horizontal projection."""
from shapely import STRtree


def roof_components(projected):
    if not projected:
        return []
    polygons = [polygon for polygon, _ in projected]
    tree = STRtree(polygons)
    parents = list(range(len(polygons)))

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def height(index, point):
        a, b, c = projected[index][1][0]
        x, z = point[0] * 1000, point[1] * 1000
        determinant = (b[0]-a[0])*(c[1]-a[1])-(c[0]-a[0])*(b[1]-a[1])
        u = ((b[2]-a[2])*(c[1]-a[1])-(c[2]-a[2])*(b[1]-a[1]))/determinant
        v = ((b[0]-a[0])*(c[2]-a[2])-(c[0]-a[0])*(b[2]-a[2]))/determinant
        return (a[2]+u*(x-a[0])+v*(z-a[1]))/1000

    for i, polygon in enumerate(polygons):
        for raw_j in tree.query(polygon):
            j = int(raw_j)
            if j <= i:
                continue
            shared = polygon.intersection(polygons[j])
            # One shared corner is not a continuous roof. A height step remains
            # two editable roofs even if both have exactly the same plan edge.
            if shared.is_empty or shared.length < .01:
                continue
            if shared.area > 1e-8:
                boundary = shared.boundary
            else:
                boundary = shared
            parts = list(boundary.geoms) if hasattr(boundary, "geoms") else [boundary]
            connected = False
            for part in parts:
                # GEOS can retain an empty member while repairing a nearly
                # coincident CityGML boundary. It is not a roof connection and
                # must not abort the conversion of the complete import plan.
                if part is None or part.is_empty:
                    continue
                if part.geom_type not in ("LineString", "LinearRing") or part.length < .01:
                    continue
                samples = [part.interpolate(t, normalized=True).coords[0] for t in (0, .5, 1)]
                if all(abs(height(i, p)-height(j, p)) <= .02 for p in samples):
                    connected = True
                    break
            if connected:
                parents[root(j)] = root(i)
    groups = {}
    for index, item in enumerate(projected):
        groups.setdefault(root(index), []).append(item)
    return list(groups.values())


def unique_roof_faces(faces):
    """Remove duplicate source facets without changing a roof's coordinates."""
    unique = {}
    for face in faces:
        key = tuple(sorted(tuple(round(v, 3) for v in p) for p in face["polygon_3d_mm"]))
        unique.setdefault(key, face)
    return list(unique.values())
