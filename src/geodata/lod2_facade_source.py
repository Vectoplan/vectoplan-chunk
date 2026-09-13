"""Keep wall provenance when a user deletes an imported roof on its own."""
from copy import deepcopy
from hashlib import sha256
from math import floor, isfinite

FACADE_SOURCE_TYPE = "building_facade_source"


def facade_source_placement(roof, chunk_size):
    metadata = deepcopy(roof.metadata_json or {})
    source = metadata.get("roofParameters", {}).get("importedSource", {})
    if roof.object_type_id != "building_roof" or not metadata.get("lod2BuildingId") or not source.get("buildingId"):
        return None
    if source.get("buildingId") != metadata["lod2BuildingId"]:
        raise ValueError("LoD2 roof and facade identity disagree.")
    points = []
    for segment in source.get("facadeSegments", []):
        for point in (segment.get("start"), segment.get("end")):
            for height in (segment.get("minimumY"), segment.get("maximumY")):
                if isinstance(point, list) and len(point) == 2 and all(isinstance(v, (int,float)) and isfinite(v) for v in (*point,height)):
                    points.append((point[0],height,point[1]))
    if not points:
        # Converted buildings already own their walls. Their current contour
        # source deliberately has no old facade axes, but still needs a height
        # reference after its roof is removed.
        for ring in (roof.footprint_json or {}).get("coordinates", []):
            for point in ring:
                if isinstance(point,list) and len(point)==2 and all(isinstance(v,(int,float)) and isfinite(v) for v in point):
                    points.append((point[0],roof.anchor_y,point[1]))
    if not points:
        raise ValueError("No complete LoD2 facade source remains for this roof.")
    minimum = [floor(min(point[axis] for point in points) / chunk_size) for axis in range(3)]
    maximum = [floor(max(point[axis] for point in points) / chunk_size) for axis in range(3)]
    count = (maximum[0]-minimum[0]+1)*(maximum[1]-minimum[1]+1)*(maximum[2]-minimum[2]+1)
    if count > 4096:
        raise ValueError("LoD2 facade source exceeds its routing limit.")
    identity = f"lod2_facade_{sha256(roof.object_instance_id.encode()).hexdigest()[:28]}"
    metadata.update(voxelOccupancy="none", semanticRole="building.facade-source",
        lod2FacadeSource={"schemaVersion":"vectoplan-lod2-facade-source.v1",
            "deletedRoofObjectInstanceId":roof.object_instance_id,
            "originalRoofObjectInstanceId":metadata.get("lod2FacadeSource",{}).get("originalRoofObjectInstanceId",roof.object_instance_id)},
        mergeKey=identity)
    return {"type":"PlaceObject", "objectInstanceId":identity, "objectTypeId":FACADE_SOURCE_TYPE,
        "objectKind":"semantic_footprint", "objectSource":roof.object_source,
        "position":{"x":roof.anchor_x,"y":roof.anchor_y,"z":roof.anchor_z},
        "dimensions":{"x":roof.size_x,"y":roof.size_y,"z":roof.size_z},
        "blockTypeId":metadata.get("fillBlockTypeId") or "lod2_exterior_wall",
        "footprint":deepcopy(roof.footprint_json), "metadata":metadata,
        # Metadata routing, not occupied wall voxels. Sources remain available
        # beside the facade even when the former roof's chunk is not streamed.
        "occupiedCells":[{"x":x*chunk_size,"y":y*chunk_size,"z":z*chunk_size}
            for x in range(minimum[0],maximum[0]+1) for y in range(minimum[1],maximum[1]+1)
            for z in range(minimum[2],maximum[2]+1)]}


def parent_facade_replacement(parent, removed_roof_id, facade_id):
    """A generated roof's parent manifest must point to its non-rendered source."""
    metadata = deepcopy(parent.metadata_json or {})
    manifest = metadata.get("generatedObjects", [])
    if not any(item.get("objectInstanceId") == removed_roof_id for item in manifest):
        return None
    metadata["generatedObjects"] = [{**item,"objectInstanceId":facade_id} if item.get("objectInstanceId")==removed_roof_id else item for item in manifest]
    return {"type":"PlaceObject", "objectInstanceId":parent.object_instance_id,
        "objectTypeId":parent.object_type_id,"objectKind":parent.object_kind,"objectSource":parent.object_source,
        "position":{"x":parent.anchor_x,"y":parent.anchor_y,"z":parent.anchor_z},
        "dimensions":{"x":parent.size_x,"y":parent.size_y,"z":parent.size_z},
        "blockTypeId":metadata.get("wallBlockTypeId") or "lod2_exterior_wall", "footprint":deepcopy(parent.footprint_json),
        "occupiedCells":deepcopy(parent.occupied_cells_json),"metadata":metadata}
