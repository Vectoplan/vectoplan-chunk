"""Pure, bounded CityGML -> editable world geometry conversion.

Input coordinates are already in the project's immutable world frame. Only
WallSurface polygons become cells: never fill a building's bounding box.
RoofSurface polygons retain their actual geometry in the WorldEdit roof contract.
LoD2 contains no reliable wall thickness, interior construction or timber data.
"""
from __future__ import annotations

import hashlib
import json
import math

from shapely import constrained_delaunay_triangles, make_valid, union_all
from shapely.geometry import LineString, Point, Polygon, box
from src.geodata.roof_topology import roof_components, unique_roof_faces

CONVERSION_VERSION = "lod2-editable-buildings.v3"
WALL_BLOCK_ID = "lod2_exterior_wall"
MAX_WALL_CELLS = 100_000
MAX_FACE_CANDIDATES = 250_000
MAX_ROOF_FACES = 8192
EPSILON = 1e-7


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def planar_polygon(rings):
    """Project a finite planar 3D polygon onto its dominant plane, preserving holes."""
    if not rings or any(len(ring) < 4 or ring[0] != ring[-1] for ring in rings):
        raise ValueError("LoD2 polygon must have closed rings")
    if any(len(p) != 3 or not all(math.isfinite(v) for v in p) for r in rings for p in r):
        raise ValueError("LoD2 polygon contains invalid XYZ coordinates")
    ring = rings[0]
    normal = [sum((a[(i + 1) % 3] - b[(i + 1) % 3]) * (a[(i + 2) % 3] + b[(i + 2) % 3])
                  for a, b in zip(ring, ring[1:])) for i in range(3)]
    dropped = max(range(3), key=lambda i: abs(normal[i]))
    length = math.sqrt(sum(n * n for n in normal))
    if length < EPSILON:
        raise ValueError("Degenerate LoD2 surface")
    axes = [i for i in range(3) if i != dropped]
    constant = sum(normal[i] * ring[0][i] for i in range(3))
    # Source decimals + reprojection can introduce sub-centimetre planarity noise.
    if max(abs(sum(normal[i] * p[i] for i in range(3)) - constant) / length
           for r in rings for p in r) > .05:
        raise ValueError("Non-planar LoD2 surface exceeds 5 cm tolerance")
    polygon = Polygon([(p[axes[0]], p[axes[1]]) for p in ring],
                      [[(p[axes[0]], p[axes[1]]) for p in r] for r in rings[1:]])
    if not polygon.is_valid or polygon.area < EPSILON:
        raise ValueError("Invalid LoD2 polygon topology")

    def lift(a, b):
        point = [0.0, 0.0, 0.0]
        point[axes[0]], point[axes[1]] = a, b
        point[dropped] = (constant - normal[axes[0]] * a - normal[axes[1]] * b) / normal[dropped]
        return point

    return polygon, dropped, axes, lift


def _legacy_wall_cells(polygons, *, max_cells=MAX_WALL_CELLS):
    """One-cell nominal shell; conservative stair-stepping at oblique facades."""
    result = set()
    for surface in polygons:
        if surface["surface"] != "WallSurface":
            continue
        polygon, dropped, axes, lift = planar_polygon(surface["rings"])
        if dropped == 1:
            raise ValueError("WallSurface is more horizontal than vertical")
        lo_a, lo_b, hi_a, hi_b = polygon.bounds
        a_range = range(math.floor(lo_a), math.ceil(hi_a - EPSILON))
        b_range = range(math.floor(lo_b), math.ceil(hi_b - EPSILON))
        if len(a_range) * len(b_range) > MAX_FACE_CANDIDATES:
            raise ValueError("LoD2 wall exceeds rasterization budget")
        for a in a_range:
            for b in b_range:
                clipped = polygon.intersection(box(a, b, a + 1, b + 1))
                if clipped.area < EPSILON:
                    continue
                # At most a narrow strip in the dropped direction, not a volume.
                ca, cb, da, db = clipped.bounds
                values = [lift(u, v)[dropped] for u, v in ((ca, cb), (ca, db), (da, cb), (da, db))]
                start = math.floor(min(values) + EPSILON)
                end = max(start, math.ceil(max(values) - EPSILON) - 1)
                for c in range(start, end + 1):
                    cell = [0, 0, 0]
                    cell[axes[0]], cell[axes[1]], cell[dropped] = a, b, c
                    result.add(tuple(cell))
                    if len(result) > max_cells:
                        raise ValueError("LoD2 building exceeds wall cell budget")
    return sorted(result)


def _linear_positive_interval(first, second):
    """Return the part of [0, 1] on which a linear value is positive."""
    if first > 0 and second > 0:
        return 0.0, 1.0
    if first <= 0 and second <= 0:
        return None
    crossing = max(0.0, min(1.0, -first / (second - first)))
    return (crossing, 1.0) if second > first else (0.0, crossing)


def _facade_active_intervals(segment, start, end, y):
    """Exact positive wall intervals inside one facade column and Y layer."""
    knots = sorted({start, end, *[
        along for profile in (segment["topProfile"], segment["bottomProfile"])
        for along, _ in profile if start < along < end
    ]})
    result = []
    for first, second in zip(knots, knots[1:]):
        first_top = _profile_height(segment["topProfile"], first)
        second_top = _profile_height(segment["topProfile"], second)
        first_bottom = _profile_height(segment["bottomProfile"], first)
        second_bottom = _profile_height(segment["bottomProfile"], second)
        conditions = (
            _linear_positive_interval(first_top - y - .005, second_top - y - .005),
            _linear_positive_interval(y + 1 - .005 - first_bottom,
                                      y + 1 - .005 - second_bottom),
            _linear_positive_interval(first_top - first_bottom - .005,
                                      second_top - second_bottom - .005),
        )
        if any(interval is None for interval in conditions):
            continue
        low = max(interval[0] for interval in conditions)
        high = min(interval[1] for interval in conditions)
        interval_start = first + (second - first) * low
        interval_end = first + (second - first) * high
        if interval_end - interval_start > .005:
            if result and interval_start - result[-1][1] <= 1e-7:
                result[-1][1] = interval_end
            else:
                result.append([interval_start, interval_end])
    return result


def _facade_wall_cells(segments, *, max_cells=MAX_WALL_CELLS):
    """Persist one support cell for every exact building-grid wall body.

    The editor divides a facade into ``round(length)`` equal columns and owns a
    sloped/gabled layer at the midpoint of its longest positive interval.  The
    importer must use that identical ownership rule.  Quarter-metre plan
    sampling could miss this cell near a hip, leaving a mathematically valid
    facade body without a persisted source block and therefore an open wall.
    """
    result = set()
    for segment in segments:
        start, end = segment["start"], segment["end"]
        length = math.dist(start, end)
        if length < .05:
            continue
        divisions = max(1, round(length))
        column_width = length / divisions
        height_layers = max(1, math.ceil(segment["maximumY"]) - math.floor(segment["minimumY"]))
        if divisions * height_layers > MAX_FACE_CANDIDATES:
            raise ValueError("LoD2 wall exceeds rasterization budget")
        tangent = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
        for column in range(divisions):
            column_start, column_end = column * column_width, (column + 1) * column_width
            for y in range(math.floor(segment["minimumY"]), math.ceil(segment["maximumY"])):
                active = _facade_active_intervals(segment, column_start, column_end, y)
                if not active:
                    continue
                owner = max(active, key=lambda interval: (interval[1] - interval[0], -interval[0]))
                along = (owner[0] + owner[1]) / 2
                x = math.floor(start[0] + tangent[0] * along + EPSILON)
                z = math.floor(start[1] + tangent[1] * along + EPSILON)
                result.add((x, y, z))
                if len(result) > max_cells:
                    raise ValueError("LoD2 building exceeds wall cell budget")
    return sorted(result)


def wall_cells(polygons, *, max_cells=MAX_WALL_CELLS, segments=None):
    """Return a vertical editable shell whenever GroundSurface is available.

    Old tiles without a classified ground surface retain the conservative
    source-plane rasterizer.  Current LoD2 imports use the same repaired facade
    axes for both their cells and their exact editor mesh, which prevents a
    straight ground line from receiving a visibly leaning or protruding wall.
    """
    if any(surface.get("surface") == "GroundSurface" for surface in polygons):
        segments = facade_segments(polygons) if segments is None else segments
        if segments:
            return _facade_wall_cells(segments, max_cells=max_cells)
    return _legacy_wall_cells(polygons, max_cells=max_cells)


def _simplify_profile(profile):
    result = []
    for value in profile:
        while len(result) >= 2:
            first, middle = result[-2], result[-1]
            span = value[0]-first[0]
            expected = first[1] if abs(span) <= 1e-9 else first[1] + (value[1]-first[1]) * (middle[0]-first[0]) / span
            if abs(expected-middle[1]) > 1e-5:
                break
            result.pop()
        result.append([round(value[0], 6), round(value[1], 6)])
    return result


def _section_envelope(section, *, upper):
    edges = list(zip(section, [*section[1:], section[0]]))
    result = []
    breakpoints = sorted({round(value[0], 9) for value in section})
    samples = set(breakpoints)
    for index, along in enumerate(breakpoints):
        if index:
            samples.add(along-min(.001, (along-breakpoints[index-1])/4))
        if index+1 < len(breakpoints):
            samples.add(along+min(.001, (breakpoints[index+1]-along)/4))
    for along in sorted(samples):
        heights = []
        for (first_along, first_height), (second_along, second_height) in edges:
            low, high = sorted((first_along, second_along))
            if along < low-1e-7 or along > high+1e-7:
                continue
            span = second_along-first_along
            if abs(span) <= 1e-9:
                if abs(along-first_along) <= 1e-7:
                    heights.extend((first_height, second_height))
            else:
                ratio = (along-first_along)/span
                heights.append(first_height+(second_height-first_height)*ratio)
        if heights:
            result.append([along, (max if upper else min)(heights)])
    return _simplify_profile(result)


def _profile_height(profile, along):
    if along <= profile[0][0]:
        return profile[0][1]
    for first, second in zip(profile, profile[1:]):
        if along <= second[0]+1e-8:
            span = second[0]-first[0]
            return max(first[1], second[1]) if abs(span) <= 1e-9 else first[1] + (second[1]-first[1])*(along-first[0])/span
    return profile[-1][1]


def _combine_profiles(profiles, *, upper):
    profiles = [profile for profile in profiles if profile]
    points = []
    for along in sorted({value[0] for profile in profiles for value in profile}):
        heights = [_profile_height(profile, along) for profile in profiles
                   if profile[0][0]-1e-7 <= along <= profile[-1][0]+1e-7]
        if heights:
            points.append([along, (max if upper else min)(heights)])
    return _simplify_profile(points)


def _wall_descriptor(surface):
    points = surface["rings"][0][:-1]
    if len(points) < 3:
        return None
    plan = list(dict.fromkeys((round(point[0], 6), round(point[2], 6)) for point in points))
    if len(plan) < 2:
        return None

    # A true facade normally has a bottom or top edge whose endpoints share a
    # height.  Prefer that edge over the old globally-farthest pair: on a
    # leaning/stepped polygon the latter is a diagonal across the surface and
    # makes the plan axis lean even though the ground edge is straight.
    level_pairs = [
        ((first[0], first[2]), (second[0], second[2]))
        for index, first in enumerate(points)
        for second in points[index + 1:]
        if abs(first[1] - second[1]) <= .05
        and math.dist((first[0], first[2]), (second[0], second[2])) >= .05
    ]
    pairs = level_pairs or [
        (first, second) for index, first in enumerate(plan) for second in plan[index + 1:]
        if math.dist(first, second) >= .05
    ]
    if not pairs:
        return None
    start, end = max(pairs, key=lambda pair: math.dist(pair[0], pair[1]))
    length = math.dist(start, end)
    direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    # A gable/step facade can have its longest same-height edge on only one
    # half of the wall.  Keep that edge as the stable plan line (important for
    # leaning source data), but extend it over every wall vertex projected onto
    # the same axis.  Without this extension the remaining half was clamped to
    # the short segment and produced diagonal cuts through the facade.
    translated_peer = any(
        abs(math.dist(first, second) - length) <= max(.05, length * .02)
        and math.dist(((first[0] + second[0]) / 2, (first[1] + second[1]) / 2),
                      ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)) > .05
        for first, second in level_pairs
    )
    projected = [(point[0] - start[0]) * direction[0]
                 + (point[1] - start[1]) * direction[1] for point in plan]
    low, high = ((0.0, length) if translated_peer else (min(projected), max(projected)))
    start = (start[0] + direction[0] * low, start[1] + direction[1] * low)
    end = (start[0] + direction[0] * (high - low), start[1] + direction[1] * (high - low))
    length = high - low

    # Newell normal: angle 0 is a vertical plane, 90 a horizontal plane which
    # was merely classified as WallSurface by the source data.
    ring = surface["rings"][0]
    normal = [
        sum((first[(axis + 1) % 3] - second[(axis + 1) % 3])
            * (first[(axis + 2) % 3] + second[(axis + 2) % 3])
            for first, second in zip(ring, ring[1:]))
        for axis in range(3)
    ]
    normal_length = math.sqrt(sum(value * value for value in normal))
    tilt = 90.0 if normal_length < EPSILON else math.degrees(math.asin(min(1.0, abs(normal[1]) / normal_length)))
    return {
        "surface": surface,
        "points": points,
        "start": start,
        "end": end,
        "direction": direction,
        "length": length,
        "minimumY": min(point[1] for ring in surface["rings"] for point in ring),
        "maximumY": max(point[1] for ring in surface["rings"] for point in ring),
        "planeTiltDeg": tilt,
    }


def _ground_height_segments(polygons):
    result = []
    for surface in polygons:
        if surface.get("surface") != "GroundSurface":
            continue
        for ring in surface.get("rings", []):
            for first, second in zip(ring, ring[1:]):
                plan_start, plan_end = (first[0], first[2]), (second[0], second[2])
                if math.dist(plan_start, plan_end) >= EPSILON:
                    result.append((plan_start, plan_end, float(first[1]), float(second[1])))
    return result


def _ground_height_at(point, ground_segments, fallback):
    target = LineString([point, point])
    best = None
    for start, end, start_y, end_y in ground_segments:
        line = LineString([start, end])
        distance = line.distance(target)
        if best is not None and distance >= best[0]:
            continue
        length = math.dist(start, end)
        along = max(0.0, min(length,
            (point[0] - start[0]) * (end[0] - start[0]) / length
            + (point[1] - start[1]) * (end[1] - start[1]) / length))
        height = start_y + (end_y - start_y) * along / length
        best = (distance, height)
    return fallback if best is None else best[1]


def _half_turn_angle_deg(first, second):
    dot = abs(first[0] * second[0] + first[1] * second[1])
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _descriptor_ground_match(descriptor, start, end, ground_y):
    length = math.dist(start, end)
    direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    angle = _half_turn_angle_deg(descriptor["direction"], direction)
    if angle > 18:
        return None
    wall_line = LineString([descriptor["start"], descriptor["end"]])
    ground_line = LineString([start, end])
    distance = wall_line.distance(ground_line)
    touches_ground = descriptor["minimumY"] <= ground_y + .25
    if distance > (1.25 if touches_ground else .18):
        return None
    projected = [
        (point[0] - start[0]) * direction[0] + (point[2] - start[1]) * direction[1]
        for point in descriptor["points"]
    ]
    overlap = max(0.0, min(length, max(projected)) - max(0.0, min(projected)))
    if overlap < min(.05, length * .1):
        return None
    height = descriptor["maximumY"] - descriptor["minimumY"]
    # Thin vertical bands can be the real closure between a full-height wall
    # and an eave (Berlin samples contain valid 15 cm bands).  Discard only
    # numerical slivers here; the roof seam projection below decides whether
    # an aligned band contributes to the final exterior profile.
    if height < .02:
        return None
    # A heavily tilted upper fragment is a roof/eaves seam, not a facade.  A
    # tilted surface which actually reaches the ground is repairable and is
    # intentionally projected vertically onto the footprint edge.
    if descriptor["planeTiltDeg"] > 18 and not touches_ground:
        return None
    return distance + angle / 180 + (0 if touches_ground else .02)


def _orient_segment(start, end, top_profile, bottom_profile):
    length = math.dist(start, end)
    if tuple(start) <= tuple(end):
        return start, end, top_profile, bottom_profile
    reverse = lambda profile: sorted([[round(length - along, 6), height] for along, height in profile])
    return end, start, reverse(top_profile), reverse(bottom_profile)


def _ensure_profile_span(profile, length, fallback):
    if not profile:
        return [[0.0, fallback], [round(length, 6), fallback]]
    return _simplify_profile([
        [0.0, _profile_height(profile, 0.0)],
        *[[max(0.0, min(length, along)), height] for along, height in profile if 0 < along < length],
        [length, _profile_height(profile, length)],
    ])


def _keep_positive_roof_profile(repaired, source, bottom, length):
    """Reject a wrong lower roof plane before it collapses a real wall span."""
    if not repaired:
        return source
    samples = sorted({0.0, length, *[value[0] for value in repaired],
                      *[value[0] for value in source], *[value[0] for value in bottom]})
    result = []
    for along in samples:
        roof_y = _profile_height(repaired, along)
        bottom_y = _profile_height(bottom, along)
        source_y = _profile_height(source, along)
        result.append([along, source_y if roof_y <= bottom_y + .05 else roof_y])
    return _simplify_profile(result)


def _roof_plan_surfaces(polygons):
    """Return horizontal projections with numerically stable y=f(x,z) planes."""
    result = []
    for surface in polygons:
        if surface.get("surface") != "RoofSurface":
            continue
        rings = surface.get("rings", [])
        if not rings or len(rings[0]) < 4:
            continue
        polygon = make_valid(Polygon(
            [(point[0], point[2]) for point in rings[0]],
            [[(point[0], point[2]) for point in ring] for ring in rings[1:]],
        ))
        if polygon.geom_type != "Polygon" or polygon.area < EPSILON:
            continue
        _, dropped, _, lift = planar_polygon(rings)
        if dropped == 1:
            constant = lift(0, 0)[1]
            result.append((polygon, (lift(1, 0)[1] - constant, lift(0, 1)[1] - constant, constant)))
            continue
        # Match the recovery path in roof_objects for a steep roof whose
        # dominant projection is not XZ.
        points = rings[0][:-1]
        first = points[0]
        candidates = [
            (abs((second[0] - first[0]) * (third[2] - first[2])
                 - (third[0] - first[0]) * (second[2] - first[2])), second, third)
            for second_index, second in enumerate(points[1:-1], 1)
            for third in points[second_index + 1:]
        ]
        _, second, third = max(candidates, default=(0, None, None), key=lambda item: item[0])
        if second is None or third is None:
            continue
        determinant = ((second[0] - first[0]) * (third[2] - first[2])
                       - (third[0] - first[0]) * (second[2] - first[2]))
        if abs(determinant) < EPSILON:
            continue
        u = ((second[1] - first[1]) * (third[2] - first[2])
             - (third[1] - first[1]) * (second[2] - first[2])) / determinant
        v = ((second[0] - first[0]) * (third[1] - first[1])
             - (third[0] - first[0]) * (second[1] - first[1])) / determinant
        result.append((polygon, (u, v, first[1] - u * first[0] - v * first[2])))
    return result


def _roof_height_profile(start, end, source_profile, roof_surfaces, minimum_y, probe_normal=None,
                         maximum_adjustment=1.5):
    length = math.dist(start, end)
    if length < .05 or not roof_surfaces:
        return []
    facade_line = LineString([start, end])
    roof_surfaces = [surface for surface in roof_surfaces if surface[0].distance(facade_line) <= 2.0]
    if not roof_surfaces:
        return []
    direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    # The exact source planes are linear between their own vertices.  A
    # half-metre probe finds level switches without turning a large complex
    # into thousands of persisted profile knots; collinear probes collapse.
    sample_count = max(2, math.ceil(length * 4))
    normal = probe_normal or (-direction[1], direction[0])
    sample_alongs = {length * index / sample_count for index in range(sample_count + 1)}
    # Preserve roof-level and ridge switches as near-vertical profile steps.
    # Interpolating between two unrelated planes over the ordinary half-metre
    # probe distance invented the diagonal cuts visible above some walls.
    for polygon, _ in roof_surfaces:
        intersection = facade_line.intersection(polygon.boundary)
        pieces = list(intersection.geoms) if hasattr(intersection, "geoms") else [intersection]
        for piece in pieces:
            coordinates = ([piece.coords[0]] if piece.geom_type == "Point" else
                           [piece.coords[0], piece.coords[-1]] if hasattr(piece, "coords") and len(piece.coords) else [])
            for x, z in coordinates:
                along = max(0.0, min(length,
                    (x - start[0]) * direction[0] + (z - start[1]) * direction[1]))
                sample_alongs.update((along, max(0.0, along - .001), min(length, along + .001)))
    def height_at(along):
        x, z = start[0] + direction[0] * along, start[1] + direction[1] * along
        point = Point(x, z)
        options = []
        for polygon, plane in roof_surfaces:
            # Prefer the roof covering the building side of the facade.  At an
            # exact shared edge several roof levels can touch the same point;
            # choosing that zero-distance hit first caused annex roofs to cut
            # gables belonging to the main roof.
            probes = [(x, z, 0.0),
                      (x + normal[0] * .02, z + normal[1] * .02, .01),
                      (x - normal[0] * .02, z - normal[1] * .02, .02),
                      (x + normal[0] * .08, z + normal[1] * .08, .03),
                      (x - normal[0] * .08, z - normal[1] * .08, .04)]
            # Probe coverage must mean actual roof coverage.  A 3 cm polygon
            # buffer let an upper roof influence facade points more than ten
            # centimetres past its real end (8 cm probe + 3 cm tolerance),
            # leaving a narrow but several-metres-high wedge. Millimetre input
            # rounding needs only a small numerical tolerance here.
            covered = [(probe_x, probe_z, offset) for probe_x, probe_z, offset in probes
                       if polygon.distance(Point(probe_x, probe_z)) <= .003]
            distance = polygon.distance(point)
            if not covered and distance > 2.0:
                continue
            if covered:
                for roof_x, roof_z, offset in covered:
                    height = plane[0] * roof_x + plane[1] * roof_z + plane[2]
                    if height > minimum_y + .05:
                        options.append((True, offset, height))
                continue
            else:
                boundary = polygon.boundary
                nearest = boundary.interpolate(boundary.project(point))
                roof_x, roof_z, offset = nearest.x, nearest.y, distance
            height = plane[0] * roof_x + plane[1] * roof_z + plane[2]
            if height > minimum_y + .05:
                options.append((bool(covered), offset, height))
        if not options:
            return _profile_height(source_profile, along) if source_profile else None
        direct = [option for option in options if option[0]]
        if direct:
            nearest_distance = min(option[1] for option in direct)
            pool = [option for option in direct if option[1] <= nearest_distance + .005]
        else:
            nearest_distance = min(option[1] for option in options)
            pool = [option for option in options if option[1] <= nearest_distance + .05]
        target = _profile_height(source_profile, along) if source_profile else None
        chosen = min(pool, key=lambda option: abs(option[2] - target) if target is not None else option[2])
        # The classified WallSurface stays authoritative at real roof-level
        # changes.  Roof sampling is only a centimetre/metre-scale correction
        # for the horizontal projection of a leaning source facade.  A remote
        # roof plane must never replace the wall profile merely because it is
        # the only other plane in a large LoD2 building complex.
        # A roof which really covers one of the probes is the physical seam
        # partner of this ground facade.  It must win even when a malformed or
        # over-merged WallSurface differs by several metres; keeping the source
        # height produced the large wall wedges visible above lower roofs.
        # The adjustment limit remains for a merely nearby/remote roof, where
        # replacing the source profile could cut a facade with another wing.
        if target is not None and not chosen[0] and abs(chosen[2] - target) > maximum_adjustment:
            return target
        return chosen[2]

    result = [[along, height] for along in sorted(sample_alongs)
              if (height := height_at(along)) is not None]
    # Two overlapping roof levels can switch abruptly along one ground
    # facade.  Half-metre sampling alone interpolated that level change into a
    # large diagonal wedge.  Refine only discontinuous spans: a real planar
    # slope has its midpoint on the linear interpolation and remains compact,
    # while a roof-level switch converges to a near-vertical profile step.
    for _ in range(12):
        additions = []
        for first, second in zip(result, result[1:]):
            if second[0] - first[0] <= .001:
                continue
            middle = (first[0] + second[0]) / 2
            middle_height = height_at(middle)
            if middle_height is None:
                continue
            linear_height = (first[1] + second[1]) / 2
            if abs(middle_height - linear_height) > .005:
                additions.append([middle, middle_height])
        if not additions:
            break
        result = sorted([*result, *additions])
    return _simplify_profile(result)


def _source_segment(descriptor):
    start, end = descriptor["start"], descriptor["end"]
    length = descriptor["length"]
    direction = descriptor["direction"]
    section = []
    for point in descriptor["points"]:
        along = (point[0] - start[0]) * direction[0] + (point[2] - start[1]) * direction[1]
        perpendicular = abs((point[0] - start[0]) * direction[1] - (point[2] - start[1]) * direction[0])
        if perpendicular <= .08:
            section.append((max(0.0, min(length, along)), float(point[1])))
    top = _ensure_profile_span(_section_envelope(section, upper=True), length, descriptor["maximumY"])
    bottom = _ensure_profile_span(_section_envelope(section, upper=False), length, descriptor["minimumY"])
    start, end, top, bottom = _orient_segment(start, end, top, bottom)
    return {
        "start": [round(start[0], 6), round(start[1], 6)],
        "end": [round(end[0], 6), round(end[1], 6)],
        "minimumY": descriptor["minimumY"],
        "maximumY": descriptor["maximumY"],
        "topProfile": top,
        "bottomProfile": bottom,
        "facadeRole": "source",
    }


def _descriptor_top_on_edge(descriptor, start, end):
    """Clip a source top profile to one GroundSurface edge without clamping.

    A source wall can span several collinear ground edges.  Clamping all of its
    out-of-range vertices onto every sub-edge copied a remote ridge/step onto
    the sub-edge endpoint.  Projecting the already reconstructed source
    profile and interpolating at the two real clip boundaries preserves only
    the portion geometrically belonging to this edge.
    """
    source_start, source_end = descriptor["start"], descriptor["end"]
    source_length = math.dist(source_start, source_end)
    target_length = math.dist(start, end)
    if source_length < EPSILON or target_length < EPSILON:
        return []
    source_direction = ((source_end[0] - source_start[0]) / source_length,
                        (source_end[1] - source_start[1]) / source_length)
    target_direction = ((end[0] - start[0]) / target_length,
                        (end[1] - start[1]) / target_length)
    raw_alongs = [((point[0] - source_start[0]) * source_direction[0]
                   + (point[2] - source_start[1]) * source_direction[1])
                  for point in descriptor["points"]]
    level_groups = {}
    for point, along in zip(descriptor["points"], raw_alongs):
        level_groups.setdefault(round(point[1] / .05), []).append(along)
    level_shifts = {}
    for level, values in level_groups.items():
        span = max(values) - min(values)
        level_shifts[level] = ((max(values) + min(values)) / 2 - source_length / 2
                               if span >= source_length * .8 else 0.0)
    source_section = [
        (max(0.0, min(source_length, along - level_shifts[round(point[1] / .05)])), float(point[1]))
        for point, along in zip(descriptor["points"], raw_alongs)
    ]
    source_profile = _ensure_profile_span(
        _section_envelope(source_section, upper=True), source_length, descriptor["maximumY"],
    )
    result = []
    for target_along in (0.0, target_length):
        x = start[0] + target_direction[0] * target_along
        z = start[1] + target_direction[1] * target_along
        source_along = ((x - source_start[0]) * source_direction[0]
                        + (z - source_start[1]) * source_direction[1])
        if -.05 <= source_along <= source_length + .05:
            result.append([target_along, _profile_height(
                source_profile, max(0.0, min(source_length, source_along)),
            )])
    for source_along, height in source_profile:
        x = source_start[0] + source_direction[0] * source_along
        z = source_start[1] + source_direction[1] * source_along
        target_along = ((x - start[0]) * target_direction[0]
                        + (z - start[1]) * target_direction[1])
        if 1e-7 < target_along < target_length - 1e-7:
            result.append([target_along, height])
    return _simplify_profile(sorted(result))


def facade_segments(polygons):
    """Exact plan edges and vertical spans of classified LoD2 walls.

    Voxel cells remain the editable persistence primitive, but they cannot
    encode a rotated/oblique facade without a stair step. Keeping this compact
    source reference lets the editor render and extend the same wall-aligned
    one-metre grid without inventing geometry from the roof overhang.
    """
    descriptors = [descriptor for surface in polygons if surface.get("surface") == "WallSurface"
                   if (descriptor := _wall_descriptor(surface)) is not None]
    footprints = ground_footprints(polygons)
    if not footprints:
        result = {}
        for descriptor in descriptors:
            segment = _source_segment(descriptor)
            key = (tuple(segment["start"]), tuple(segment["end"]))
            previous = result.get(key)
            if previous:
                segment["topProfile"] = _combine_profiles([previous["topProfile"], segment["topProfile"]], upper=True)
                segment["bottomProfile"] = _combine_profiles([previous["bottomProfile"], segment["bottomProfile"]], upper=False)
                segment["minimumY"] = min(previous["minimumY"], segment["minimumY"])
                segment["maximumY"] = max(previous["maximumY"], segment["maximumY"])
            result[key] = segment
        return list(result.values())

    raw_ground_y = [point[1] for surface in polygons if surface.get("surface") == "GroundSurface"
                    for ring in surface.get("rings", []) for point in ring]
    ground_y = sorted(raw_ground_y)[len(raw_ground_y) // 2] if raw_ground_y else 0.0
    ground_segments = _ground_height_segments(polygons)
    roof_surfaces = _roof_plan_surfaces(polygons)
    used = set()
    result = {}
    for footprint in footprints:
        footprint_polygon = Polygon(footprint[0], footprint[1:])
        for ring in footprint:
            for raw_start, raw_end in zip(ring, ring[1:]):
                start, end = tuple(raw_start), tuple(raw_end)
                length = math.dist(start, end)
                if length < .05:
                    continue
                start_y = _ground_height_at(start, ground_segments, ground_y)
                end_y = _ground_height_at(end, ground_segments, ground_y)
                edge_ground_y = min(start_y, end_y)
                matches = []
                for index, descriptor in enumerate(descriptors):
                    score = _descriptor_ground_match(descriptor, start, end, edge_ground_y)
                    if score is not None:
                        matches.append((score, index, descriptor))
                matches.sort(key=lambda item: item[0])
                if not matches:
                    continue
                # Merge coincident source pieces (e.g. an eaves split), while
                # rejecting a separate parallel facade which merely happens to
                # be nearby on a narrow annex.
                selected = [match for match in matches if match[0] <= matches[0][0] + .12]
                direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
                midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
                probe_normal = (-direction[1], direction[0])
                inside_probe = Point(midpoint[0] + probe_normal[0] * .05,
                                     midpoint[1] + probe_normal[1] * .05)
                if not footprint_polygon.buffer(1e-7).covers(inside_probe):
                    probe_normal = (-probe_normal[0], -probe_normal[1])
                top_profiles = []
                maximum_y = edge_ground_y
                for _, index, descriptor in selected:
                    used.add(index)
                    maximum_y = max(maximum_y, descriptor["maximumY"])
                    top_profiles.append(_descriptor_top_on_edge(descriptor, start, end))
                source_top = _ensure_profile_span(
                    _combine_profiles(top_profiles, upper=True), length, maximum_y,
                )
                if not source_top:
                    continue
                bottom = [[0.0, start_y], [round(length, 6), end_y]]
                roof_top = _roof_height_profile(
                    start, end, source_top, roof_surfaces,
                    edge_ground_y, probe_normal,
                )
                corrected_top = _keep_positive_roof_profile(
                    roof_top, source_top, bottom, length,
                )
                top = _ensure_profile_span(corrected_top, length, maximum_y)
                maximum_y = max(height for _, height in top)
                start, end, top, bottom = _orient_segment(start, end, top, bottom)
                key = (tuple(start), tuple(end))
                result[key] = {
                    "start": [round(start[0], 6), round(start[1], 6)],
                    "end": [round(end[0], 6), round(end[1], 6)],
                    "minimumY": min(start_y, end_y),
                    "maximumY": maximum_y,
                    "topProfile": top,
                    "bottomProfile": bottom,
                    "facadeRole": "exterior",
                }

    # Keep real elevated/set-back facades.  Tiny eaves slivers and almost
    # horizontal source polygons are classified noise and must not become
    # independent full-height block walls.
    for index, descriptor in enumerate(descriptors):
        if index in used or descriptor["maximumY"] - descriptor["minimumY"] < .35 or descriptor["planeTiltDeg"] > 18:
            continue
        segment = _source_segment(descriptor)
        segment["facadeRole"] = "connector"
        segment_length = math.dist(segment["start"], segment["end"])
        if segment_length < .15:
            continue
        profile_samples = sorted({0.0, segment_length,
                                  *[along for along, _ in segment["topProfile"]],
                                  *[along for along, _ in segment["bottomProfile"]]})
        if max((_profile_height(segment["topProfile"], along)
                - _profile_height(segment["bottomProfile"], along) for along in profile_samples), default=0) <= .005:
            continue
        key = (tuple(segment["start"]), tuple(segment["end"]))
        previous = result.get(key)
        if previous:
            segment["topProfile"] = _combine_profiles([previous["topProfile"], segment["topProfile"]], upper=True)
            segment["bottomProfile"] = _combine_profiles([previous["bottomProfile"], segment["bottomProfile"]], upper=False)
            segment["minimumY"] = min(previous["minimumY"], segment["minimumY"])
            segment["maximumY"] = max(previous["maximumY"], segment["maximumY"])
            if previous.get("facadeRole") == "exterior":
                segment["facadeRole"] = "exterior"
        result[key] = segment
    return list(result.values())


def ground_footprints(polygons):
    """Dissolve classified GroundSurface rings into the real wall footprint.

    Roof projections can include overhangs and therefore must never be used as
    the exclusion or snap contour of the parcel grid.  Coordinates stay in the
    immutable project/world frame.  A CityGML building can split one ground
    plate into several touching surfaces; persisting those parts separately
    would incorrectly expose their shared seams as exterior facades.
    """
    ground_parts = []
    for surface in polygons:
        if surface["surface"] != "GroundSurface":
            continue
        rings = []
        for ring in surface["rings"]:
            plan = [(round(point[0], 6), round(point[2], 6)) for point in ring]
            if len(plan) >= 4 and plan[0] == plan[-1]:
                rings.append(plan)
        if rings:
            geometry = make_valid(Polygon(rings[0], rings[1:]))
            parts = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
            ground_parts.extend(part for part in parts if part.geom_type == "Polygon" and part.area > EPSILON)
    if not ground_parts:
        return []
    dissolved = make_valid(union_all(ground_parts, grid_size=.001))
    parts = list(dissolved.geoms) if dissolved.geom_type == "MultiPolygon" else [dissolved]
    result = []
    for part in parts:
        if part.geom_type != "Polygon" or part.area <= EPSILON:
            continue
        result.append([
            [[round(x, 6), round(z, 6)] for x, z in part.exterior.coords],
            *[[[round(x, 6), round(z, 6)] for x, z in ring.coords] for ring in part.interiors],
        ])
    return result


def roof_objects(feature, *, building_facades=None, building_ground_footprints=None):
    """One editable zone per 3D-connected roof, preserving separate height levels.

    Triangulation is constrained, so concave rings and holes are not bridged.
    CAD uses [x, plan-y, height] millimetres; Chunk uses [x, height, z] metres.
    """
    roof_surfaces = [p for p in feature["polygons"] if p["surface"] == "RoofSurface"]
    building_facades = facade_segments(feature["polygons"]) if building_facades is None else building_facades
    building_ground_footprints = (ground_footprints(feature["polygons"])
                                  if building_ground_footprints is None else building_ground_footprints)
    projected = []
    for surface in roof_surfaces:
        polygon, dropped, _, lift = planar_polygon(surface["rings"])
        if dropped != 1:
            # A steep roof can have a dominant horizontal normal: use XZ explicitly.
            ring = surface["rings"][0]
            polygon = Polygon([(p[0], p[2]) for p in ring],
                              [[(p[0], p[2]) for p in r] for r in surface["rings"][1:]])
            if not polygon.is_valid or polygon.area < EPSILON:
                raise ValueError("RoofSurface has no valid horizontal projection")
            # Fit plane through any three non-collinear projected vertices.
            a = ring[0]
            # CityGML rings often start with several almost-collinear eaves
            # points. Using the first determinant above epsilon is numerically
            # catastrophic (centimetre input noise becomes hundreds of metres
            # of roof height). Fit through the widest projected triangle.
            candidates = [
                ((b[0]-a[0])*(c[2]-a[2]) - (c[0]-a[0])*(b[2]-a[2]), b, c)
                for index, b in enumerate(ring[1:-1]) for c in ring[index+2:-1]
            ]
            determinant, b, c = max(candidates, key=lambda item: abs(item[0]), default=(0, None, None))
            if abs(determinant) <= EPSILON:
                raise ValueError("Cannot recover roof plane")
            u = ((b[1]-a[1])*(c[2]-a[2]) - (c[1]-a[1])*(b[2]-a[2])) / determinant
            v = ((b[0]-a[0])*(c[1]-a[1]) - (c[0]-a[0])*(b[1]-a[1])) / determinant
            plane = (u, v, a[1]-u*a[0]-v*a[2])
            def lift(x, z, plane=plane):
                return [x, plane[0]*x + plane[1]*z + plane[2], z]
        triangles = []
        for triangle in constrained_delaunay_triangles(polygon).geoms:
            points = [lift(x, z) for x, z in list(triangle.exterior.coords)[:3]]
            triangles.append([[round(p[0]*1000, 3), round(p[2]*1000, 3), round(p[1]*1000, 3)] for p in points])
        projected.append((polygon, triangles))
    if not projected:
        return []
    zones = []
    for component in roof_components(projected):
        union = union_all([p for p, _ in component], grid_size=.001)
        parts = list(union.geoms) if union.geom_type == "MultiPolygon" else [union]
        if any(zone.geom_type != "Polygon" for zone in parts):
            raise ValueError("Unsupported roof footprint")
        zones.extend((zone, component) for zone in parts)
    bounded_zones = []
    for zone, component in zones:
        x0, z0, x1, z1 = zone.bounds
        if math.ceil(x1)-math.floor(x0) <= 256 and math.ceil(z1)-math.floor(z0) <= 256:
            bounded_zones.append((zone, None, component))
            continue
        # Large complexes become adjacent editable roof zones. Clip the actual
        # facets, never replace them with a bounding-box roof or truncate them.
        for x in range(math.floor(x0/128)*128, math.ceil(x1), 128):
            for z in range(math.floor(z0/128)*128, math.ceil(z1), 128):
                clip = box(x, z, x+128, z+128)
                cut = zone.intersection(clip)
                parts = list(cut.geoms) if hasattr(cut, "geoms") else [cut]
                bounded_zones.extend((part, clip, component) for part in parts if part.geom_type == "Polygon" and part.area > EPSILON)
    if len(bounded_zones) > 64:
        raise ValueError("Building exceeds 64 roof zones")
    result = []
    for zone_index, (zone, clip, component) in enumerate(sorted(bounded_zones, key=lambda entry: entry[0].bounds)):
        faces = []
        for polygon, triangles in component:
            if clip is None:
                if zone.buffer(.002).covers(polygon.representative_point()):
                    faces.extend({"face_ref": f"lod2-{len(faces)+i}", "polygon_3d_mm": points}
                                 for i, points in enumerate(triangles))
                continue
            for points in triangles:
                tri = Polygon([(p[0]/1000, p[1]/1000) for p in points])
                cut = tri.intersection(clip)
                if cut.area < EPSILON or not zone.buffer(.002).covers(cut.representative_point()):
                    continue
                a, b, c = points
                determinant = (b[0]-a[0])*(c[1]-a[1]) - (c[0]-a[0])*(b[1]-a[1])
                u = ((b[2]-a[2])*(c[1]-a[1]) - (c[2]-a[2])*(b[1]-a[1])) / determinant
                v = ((b[0]-a[0])*(c[2]-a[2]) - (c[0]-a[0])*(b[2]-a[2])) / determinant
                for piece in constrained_delaunay_triangles(cut).geoms:
                    vertices = [[round(x*1000, 3), round(z*1000, 3), round(a[2]+u*(x*1000-a[0])+v*(z*1000-a[1]), 3)]
                                for x, z in list(piece.exterior.coords)[:3]]
                    faces.append({"face_ref": f"lod2-{len(faces)}", "polygon_3d_mm": vertices})
        faces = unique_roof_faces(faces)
        if not faces or len(faces) > MAX_ROOF_FACES:
            raise ValueError("Roof face budget exceeded or empty zone")
        heights = [p[2] / 1000 for face in faces for p in face["polygon_3d_mm"]]
        base, top = min(heights), max(heights)
        coordinates = [list(map(list, zone.exterior.coords)), *[list(map(list, r.coords)) for r in zone.interiors]]
        source = {"schemaVersion": "lod2-roof-source.v1", "buildingId": feature["id"],
                  "sourceTile": feature["sourceTile"], "sourceSha256": feature["sourceSha256"],
                  "faces": faces, "footprint": coordinates, "baseY": base,
                  "referencePitchDeg": 35 if top-base > .001 else 0,
                  "groundFootprints": building_ground_footprints,
                  "facadeGeometryMode": "ground-normalized-v1"}
        source["facadeSegments"] = [segment for segment in building_facades
            if zone.boundary.distance(LineString([segment["start"], segment["end"]])) <= 2.0
            and segment["maximumY"] >= base - 2.0]
        # 'pitch' is a relative shape-scaling control for a multi-slope source roof,
        # not a claim that every source facet has this measured inclination.
        source["referencePitchDeg"] = 35 if top-base > .001 else 0
        roof_id = "lod2_roof_" + digest([feature["id"], zone_index, coordinates])[:28]
        parameters = {"roofType": "imported", "pitchDeg": source["referencePitchDeg"],
                      "eavesHeightMm": base*1000, "overhangMm": 0, "overhangNorthMm": 0,
                      "overhangEastMm": 0, "overhangSouthMm": 0, "overhangWestMm": 0,
                      "edgeOverhangsMm": [], "importedSource": source}
        calculation = {"ok": True, "contract_version": "cad-roof-calculation-result/0.1",
                       "roof_type": "imported", "calculation_id": roof_id,
                       "input_fingerprint": digest(source), "geometry": {"faces": faces},
                       "structure": {"rafters": [], "purlins": [], "source": "not-provided-by-lod2"},
                       "summary": {"face_count": len(faces), "maximum_height_mm": top*1000},
                       "source": "lod2-original-surfaces"}
        x0, z0, x1, z1 = zone.bounds
        # This is a metadata-only semantic object: no fake anchor block may
        # overwrite an existing wall or user cell. PlaceObject handles this flag.
        anchor = {"x": math.floor(x0), "y": math.floor(base), "z": math.floor(z0)}
        result.append({"type": "PlaceObject", "position": anchor, "blockTypeId": WALL_BLOCK_ID,
                       "objectTypeId": "building_roof", "objectKind": "semantic_footprint",
                       "objectSource": "importer", "objectInstanceId": roof_id,
                       "dimensions": {"x": max(1, math.ceil(x1)-anchor["x"]),
                                      "y": max(1, math.ceil(top)-anchor["y"]),
                                      "z": max(1, math.ceil(z1)-anchor["z"])},
                       "occupiedCells": [anchor],
                       "footprint": {"type": "Polygon", "coordinateSpace": "world-cell-xz",
                                     "coordinates": coordinates, "baseY": base, "height": max(.1, top-base),
                                     "schemaVersion": "vectoplan-building-roof-footprint.v1"},
                       "metadata": {"schemaVersion": "vectoplan-building-roof.v1", "voxelOccupancy": "none",
                                    "source": "vectoplan-chunk.lod2-import", "familyRef": "world-edit.roof",
                                    "variantRef": "imported", "roofType": "imported", "mergeKey": roof_id,
                                    "lod2BuildingId": feature["id"], "roofParameters": parameters,
                                    "roofCalculation": calculation}})
    return result


def convert_building(feature):
    facades = facade_segments(feature["polygons"])
    footprints = ground_footprints(feature["polygons"])
    cells = wall_cells(feature["polygons"], segments=facades)
    roofs = roof_objects(feature, building_facades=facades, building_ground_footprints=footprints)
    if not cells or not roofs:
        raise ValueError("Building needs classified walls and roofs for complete conversion")
    return {"buildingId": feature["id"], "sourceTile": feature["sourceTile"],
            "sourceSha256": feature["sourceSha256"], "wallCells": cells, "roofs": roofs}
