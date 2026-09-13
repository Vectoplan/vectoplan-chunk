"""Conservative, transactional conversion of imported LoD2 walls to editable objects.

Only server-side imported facade geometry defines the old wall footprint.  A
matching material alone is never ownership proof: command history also has to
show an importer write with no subsequent independent edit to that coordinate.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .lod2_conversion import WALL_BLOCK_ID, _facade_wall_cells


VALIDATION_VERSION = "lod2-building-edit.v1"


def _record(value):
    return value if isinstance(value, Mapping) else {}


def _key(value):
    if not isinstance(value, Mapping):
        return None
    try:
        coordinates = tuple(value[axis] for axis in ("x", "y", "z"))
        if any(isinstance(value, bool) or int(value) != value for value in coordinates):
            return None
        return tuple(int(value) for value in coordinates)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _position(key):
    return dict(zip(("x", "y", "z"), key))


def _same_edit(marker, building_id, parent_id):
    marker = _record(marker)
    return (marker.get("validationVersion") == VALIDATION_VERSION
            and marker.get("buildingId") == building_id
            and marker.get("parentObjectInstanceId") == parent_id)


def validate_imported_roof_inventory(source, original_roof_ids):
    """A partly streamed building may not silently lose its missing annex roofs."""
    requested = source.get("roofObjectIds")
    if (not isinstance(requested, list) or not all(isinstance(value, str) for value in requested)
            or set(requested) != original_roof_ids or len(requested) != len(original_roof_ids)):
        raise ValueError("Incomplete imported roof set: load every original building roof before conversion.")


def current_building_roof_inventory(roofs, parent_metadata=None):
    """A lost historical response must not revive a retired roof baseline."""
    manifest = _record(parent_metadata).get("generatedObjects")
    current = {ref.get("objectInstanceId") for ref in manifest or [] if isinstance(ref, Mapping)}
    return [roof for roof in roofs if roof.deleted_at is None
            and (manifest is None or roof.object_instance_id in current)]


def read_lod2_building_objects(*, world, building_id):
    """Load a whole building independently of the camera's streamed chunks."""
    from sqlalchemy import or_
    from models import WorldObjectInstance
    from routes import commands as route

    parents = route._query_without_relationships(WorldObjectInstance.query.filter(
        WorldObjectInstance.world_db_id == world.id,
        WorldObjectInstance.object_type_id == "planning_build_area",
        WorldObjectInstance.deleted_at.is_(None),
        WorldObjectInstance.metadata_json["contourBuilding"]["source"]["buildingId"].astext == building_id,
    )).order_by(WorldObjectInstance.id.desc()).all()
    parent_ids = [obj.object_instance_id for obj in parents]
    roof_scope = WorldObjectInstance.metadata_json["lod2BuildingId"].astext == building_id
    if parent_ids:
        roof_scope = or_(roof_scope, WorldObjectInstance.metadata_json["generatedFromAreaId"].astext.in_(parent_ids))
    roofs = route._query_without_relationships(WorldObjectInstance.query.filter(
        WorldObjectInstance.world_db_id == world.id,
        WorldObjectInstance.object_type_id.in_(("building_roof", "building_facade_source")), roof_scope,
    )).order_by(WorldObjectInstance.object_instance_id).all()

    def serialize(obj):
        return {
            "objectInstanceId": obj.object_instance_id, "objectTypeId": obj.object_type_id,
            "objectKind": obj.object_kind, "objectSource": obj.object_source,
            "anchor": {"x": obj.anchor_x, "y": obj.anchor_y, "z": obj.anchor_z},
            "dimensions": {"x": obj.size_x, "y": obj.size_y, "z": obj.size_z},
            "footprint": deepcopy(obj.footprint_json or {}),
            "occupiedCells": deepcopy(obj.occupied_cells_json or []),
            "metadata": {**deepcopy(obj.metadata_json or {}),
                         **({"lod2BuildingId": building_id} if obj.object_type_id in ("building_roof", "building_facade_source") else {})},
        }
    if not parents and not roofs:
        raise LookupError(f"LoD2 building '{building_id}' was not found in this world.")
    return {
        "buildingId": building_id,
        "objectRefs": [serialize(obj) for obj in current_building_roof_inventory(
            roofs, parents[0].metadata_json if parents else None)],
        "originalRoofObjectIds": [obj.object_instance_id for obj in roofs if obj.object_source == "importer" and obj.object_type_id == "building_roof"],
        "parentRef": serialize(parents[0]) if parents else None,
    }


def classify_cell_history(logs, candidates, *, building_id, parent_id):
    """Return imported cells, independently edited cells and verified child IDs.

    Historical exemptions come from a server result marker, never merely the
    caller's lod2BuildingEdit request. Removed blocks remain protected even
    when their current material is air or a later importer restored that type.
    """
    imported, protected, generated_ids = set(), set(), set()
    for log in logs:
        result = _record(getattr(log, "result_payload_json", None))
        marker = _record(getattr(log, "lod2_edit_marker", result.get("lod2BuildingEdit")))
        same_edit = (getattr(log, "command_type", None) == "ObjectBatch"
                     and _same_edit(marker, building_id, parent_id))
        if same_edit:
            generated_ids.update(str(value) for value in marker.get("generatedObjectIds", [])
                                 if isinstance(value, str))
        importer = (getattr(log, "command_source", None) == "importer"
                    and getattr(log, "user_id", None) == "system_lod2_import")
        affected = getattr(log, "affected_cells_json", None) or []
        # Missing provenance cannot establish that a cell was untouched.
        if int(getattr(log, "affected_cell_count", 0) or 0) > len(affected):
            protected.update(candidates)
        for cell in affected:
            key = _key(cell)
            if key not in candidates:
                continue
            if importer:
                if cell.get("afterBlockTypeId") == WALL_BLOCK_ID:
                    imported.add(key)
            elif not same_edit:
                protected.add(key)
    return imported, protected, generated_ids


def plan_cell_protection(original, requested, states, *, imported, edited, generated_ids):
    """Protect foreign/current materials and choose proven original wall cleanup."""
    protected = set(edited)
    cleanup = set()
    for key in original | requested:
        state = states[key]
        block_id = state.get("blockTypeId")
        owner_id = state.get("objectInstanceId")
        owned_generation = (owner_id in generated_ids
                            and state.get("expectedBlockTypeId") == block_id)
        original_import = (key in original and key in imported
                           and block_id == WALL_BLOCK_ID and not owner_id)
        if owner_id and not owned_generation:
            protected.add(key)
        elif not state.get("isAir", not block_id) and not owned_generation and not original_import:
            protected.add(key)
        if original_import and key not in protected:
            cleanup.add(key)
    return protected, cleanup


def filter_placements(children, protected, *, parent_id):
    """Keep visual shape cells and persisted voxel ownership in agreement.

    Empty generated children remain metadata-only routing objects. Their stable
    IDs let the editor finish its normal generation readiness/retirement flow
    without placing a phantom block at their anchor.
    """
    result, filtered_count = deepcopy(children), 0
    for child in result:
        if child.get("type") != "PlaceObject":
            continue
        metadata = _record(child.get("metadata"))
        if metadata.get("voxelOccupancy") == "none" or child.get("objectInstanceId") == parent_id:
            continue
        original = child["occupiedCells"]
        retained = [cell for cell in original if _key(cell) not in protected]
        removed = len(original) - len(retained)
        filtered_count += removed
        if not removed:
            continue
        child["metadata"] = metadata = dict(metadata)
        if isinstance(metadata.get("constructionCells"), list):
            metadata["constructionCells"] = [cell for cell in metadata["constructionCells"]
                                              if _key(cell) not in protected]
        child["occupiedCells"] = retained or [dict(child["position"])]
        for field in ("wallCellCount", "slabCellCount"):
            if metadata.get(field):
                metadata[field] = len(retained)
        if not retained:
            metadata.update(voxelOccupancy="none", constructionCells=[], lod2BuildingEditEmpty=True)
    return result, filtered_count


def prepare_lod2_building_edit(*, project, universe, world, payload, children, command_log):
    """Read authoritative state and prepare bounded actions; never commit or write."""
    if "lod2BuildingEdit" not in payload:
        return children, None, None
    descriptor = _record(payload.get("lod2BuildingEdit"))
    building_id, parent_id = descriptor.get("buildingId"), descriptor.get("parentObjectInstanceId")
    if not isinstance(building_id, str) or not building_id.strip() or not isinstance(parent_id, str) or not parent_id.strip():
        raise ValueError("lod2BuildingEdit requires buildingId and parentObjectInstanceId.")

    # Delayed imports keep pure geometry/protection tests independent of Flask.
    from models import WorldObjectInstance, WorldCommandLog
    from routes import commands as route

    parent_children = [child for child in children if child.get("type") == "PlaceObject"
                       and route._extract_object_payload(child).get("objectInstanceId") == parent_id]
    if len(parent_children) != 1:
        raise ValueError("lod2BuildingEdit requires exactly one parent PlaceObject.")
    parent_child = parent_children[0]
    metadata = _record(parent_child.get("metadata"))
    source = _record(_record(metadata.get("contourBuilding")).get("source"))
    if (source.get("buildingId") != building_id or parent_child.get("objectTypeId") != "planning_build_area"
            or metadata.get("voxelOccupancy") != "none"):
        raise ValueError("lod2BuildingEdit does not match the parent contourBuilding source.")
    stored_parent = route.db.session.query(
        WorldObjectInstance.deleted_at,
        WorldObjectInstance.metadata_json["contourBuilding"]["source"]["buildingId"].astext.label("source_building_id"),
    ).filter(WorldObjectInstance.world_db_id == world.id,
             WorldObjectInstance.object_instance_id == parent_id).one_or_none()
    if stored_parent is not None:
        if stored_parent.deleted_at is not None or stored_parent.source_building_id != building_id:
            raise ValueError("lod2BuildingEdit does not match the stored parent.")

    # Deleted source roofs still retain the immutable importer geometry after
    # explicit roof replacement; later storey edits need that same provenance.
    roofs = route.db.session.query(
        WorldObjectInstance.object_instance_id,
        WorldObjectInstance.metadata_json["roofParameters"]["importedSource"].label("imported_source"),
    ).filter(WorldObjectInstance.world_db_id == world.id,
             WorldObjectInstance.object_type_id == "building_roof", WorldObjectInstance.object_source == "importer",
             WorldObjectInstance.metadata_json["lod2BuildingId"].astext == building_id).all()
    original, original_roof_ids = set(), set()
    for roof in roofs:
        roof_source = _record(roof.imported_source)
        if roof_source.get("buildingId") != building_id:
            continue
        original_roof_ids.add(roof.object_instance_id)
        original.update(_facade_wall_cells(roof_source.get("facadeSegments") or []))
    if not original_roof_ids or not original:
        raise ValueError("lod2BuildingEdit has no authoritative imported facade geometry.")

    facade_ids = {obj.object_instance_id for obj in route.db.session.query(WorldObjectInstance.object_instance_id).filter(
        WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.object_type_id == "building_facade_source",
        WorldObjectInstance.metadata_json["lod2BuildingId"].astext == building_id).all()}

    prepared = deepcopy(children)
    requested = set()
    placement_ids = set()
    for child in prepared:
        if child.get("type") != "PlaceObject":
            continue
        object_payload = route._extract_object_payload(child)
        child_id = object_payload.get("objectInstanceId")
        child["objectInstanceId"] = child_id
        placement_ids.add(child_id)
        child_metadata = _record(child.get("metadata"))
        if not child_id or (child_id != parent_id and child_metadata.get("generatedFromAreaId") != parent_id):
            raise ValueError("lod2BuildingEdit children must belong to the declared parent.")
        if child.get("objectTypeId") in ("building_roof", "building_facade_source"):
            child["metadata"] = {**child_metadata, "lod2BuildingId": building_id}
        if child_id == parent_id or child_metadata.get("voxelOccupancy") == "none":
            continue
        occupied = route._extract_object_occupied_cells(child, anchor=route._get_payload_position(child, required=True),
                                                      dimensions=route._extract_object_dimensions(child))
        child["occupiedCells"] = occupied
        requested.update(_key(cell) for cell in occupied)

    candidates = original | requested
    # Project only provenance; ordinary PlaceObject requests/results can contain
    # megabytes of facade meshes which have no bearing on cell protection.
    logs = route.db.session.query(
        WorldCommandLog.command_type, WorldCommandLog.command_source, WorldCommandLog.user_id,
        WorldCommandLog.affected_cells_json, WorldCommandLog.affected_cell_count,
        WorldCommandLog.result_payload_json["lod2BuildingEdit"].label("lod2_edit_marker"),
    ).filter(
        WorldCommandLog.world_db_id == world.id, WorldCommandLog.changed.is_(True),
        WorldCommandLog.id != command_log.id).all()
    imported, edited, verified_ids = classify_cell_history(logs, candidates, building_id=building_id, parent_id=parent_id)
    if not verified_ids:
        # A saved virtual parent without an applied conversion is still a first
        # conversion, so it cannot bypass the complete-source check.
        validate_imported_roof_inventory(source, original_roof_ids)
    generated_ids = set()
    if verified_ids:
        generated = route.db.session.query(WorldObjectInstance.object_instance_id).filter(
            WorldObjectInstance.world_db_id == world.id,
            WorldObjectInstance.object_instance_id.in_(verified_ids),
            WorldObjectInstance.metadata_json["generatedFromAreaId"].astext == parent_id).all()
        generated_ids = {obj.object_instance_id for obj in generated}
    for obj in route.db.session.query(WorldObjectInstance.object_instance_id).filter(
            WorldObjectInstance.world_db_id == world.id,
            WorldObjectInstance.object_instance_id.in_(placement_ids)).all():
        if obj.object_instance_id != parent_id and obj.object_instance_id not in generated_ids:
            raise ValueError("lod2BuildingEdit may not replace an unrelated object identity.")
    for child in prepared:
        if child.get("type") == "RemoveObject":
            child_id = route._extract_object_payload(child).get("objectInstanceId")
            if child_id not in original_roof_ids | generated_ids | facade_ids:
                raise ValueError("lod2BuildingEdit may not retire an unrelated object.")

    chunk_size = int(world.chunk_size or 16)
    chunk_cache, states = {}, {}
    for key in candidates:
        cell = route._world_position_to_chunk_cell(_position(key), chunk_size)
        coordinates = (cell["chunkX"], cell["chunkY"], cell["chunkZ"])
        if coordinates not in chunk_cache:
            _, chunk_cache[coordinates] = route._load_chunk_for_mutation(
                project=project, universe=universe, world=world,
                chunk_x=coordinates[0], chunk_y=coordinates[1], chunk_z=coordinates[2])
        content = chunk_cache[coordinates]
        value = route._get_cell_value(content, local_x=cell["localX"], local_y=cell["localY"],
                                     local_z=cell["localZ"], chunk_size=chunk_size)
        owner = route._runtime_object_cell_owner(content, world_x=key[0], world_y=key[1], world_z=key[2],
            chunk_x=coordinates[0], chunk_y=coordinates[1], chunk_z=coordinates[2],
            local_x=cell["localX"], local_y=cell["localY"], local_z=cell["localZ"])
        states[key] = {"blockTypeId": route._block_type_id_from_cell_value(content, value),
                       "isAir": int(value) == route.AIR_CELL_VALUE, **(owner or {})}
    protected, cleanup = plan_cell_protection(original, requested, states, imported=imported,
                                             edited=edited, generated_ids=generated_ids)
    prepared, filtered_count = filter_placements(prepared, protected, parent_id=parent_id)
    marker = {"validationVersion": VALIDATION_VERSION, "buildingId": building_id,
              "parentObjectInstanceId": parent_id, "generatedObjectIds": sorted(placement_ids - {parent_id}),
              "preservedCells": [_position(key) for key in sorted(protected)],
              "clearedOriginalWallCellCount": len(cleanup), "filteredCellCount": filtered_count}
    for child in prepared:
        if child.get("type") == "PlaceObject" and child.get("objectInstanceId") == parent_id:
            child["metadata"] = {**child["metadata"], "lod2BuildingEdit": marker}
    cleanup_payload = None
    if cleanup:
        cleanup_payload = {"type": "WorldEdit", "tool": "clipboard", "operation": "paste",
                           "position": {"x": 0, "y": 0, "z": 0},
                           "clipboard": [{"dx": key[0], "dy": key[1], "dz": key[2], "blockTypeId": None}
                                         for key in sorted(cleanup)]}
    return prepared, cleanup_payload, marker
