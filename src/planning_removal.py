"""Resolve a building deletion from server ownership, in the command transaction.

The client supplies an identity and a generation, never an authoritative list of
children. Existing RemoveObject cell guards, soft deletion and command history
remain responsible for material ownership and later recovery.
"""
from collections.abc import Mapping


def _record(value):
    return value if isinstance(value, Mapping) else {}


def removal_commands(parent, objects, expected_generation):
    """Pure ownership/CAS plan, also exercised with captured Berlin parents."""
    if parent is None or parent.deleted_at is not None:
        raise ValueError("Das Gebäude wurde bereits entfernt. Bitte die Ansicht aktualisieren.")
    metadata = _record(parent.metadata_json)
    if parent.object_type_id != "planning_build_area" or metadata.get("generationId") != expected_generation:
        raise ValueError("Das Gebäude wurde inzwischen geändert. Bitte erneut auswählen.")
    owned = [obj for obj in objects if obj.deleted_at is None
             and _record(obj.metadata_json).get("generatedFromAreaId") == parent.object_instance_id]
    return [{"type": "RemoveObject", "objectInstanceId": obj.object_instance_id,
             "position": {"x": obj.anchor_x, "y": obj.anchor_y, "z": obj.anchor_z}}
            for obj in sorted(owned, key=lambda obj: obj.object_instance_id) + [parent]]


def prepare_planning_removal(*, project, universe, world, payload, children, command_log):
    descriptor = payload.get("planningBuildingRemoval")
    if descriptor is None:
        return children, None, None
    if not isinstance(descriptor, Mapping) or any(key in payload for key in ("planningBuildingEdit", "lod2BuildingEdit")):
        raise ValueError("Invalid planningBuildingRemoval descriptor.")
    parent_id = descriptor.get("parentObjectInstanceId")
    if not isinstance(parent_id, str) or not parent_id:
        raise ValueError("planningBuildingRemoval requires parentObjectInstanceId.")
    # The single root placeholder makes legacy clients harmless and excludes
    # an extra unrelated RemoveObject hidden alongside the descriptor.
    if len(children) != 1 or children[0].get("type") != "RemoveObject" or children[0].get("objectInstanceId") != parent_id:
        raise ValueError("planningBuildingRemoval requires one matching root removal.")
    from models import WorldObjectInstance
    from routes import commands as route
    query = route._query_without_relationships(WorldObjectInstance.query.filter(WorldObjectInstance.world_db_id == world.id))
    parent = query.filter(WorldObjectInstance.object_instance_id == parent_id).one_or_none()
    building_id = descriptor.get("lod2BuildingId")
    if parent is not None:
        children = query.filter(WorldObjectInstance.metadata_json["generatedFromAreaId"].astext == parent_id).all()
        prepared = removal_commands(parent, children, descriptor.get("previousGenerationId"))
        if len(prepared) > route._get_max_object_batch_commands():
            raise ValueError("Die vollständige Gebäudelöschung überschreitet das Befehlslimit.")
        return prepared, None, None
    if not isinstance(building_id, str) or parent_id != f"lod2_building_{building_id}":
        raise ValueError("Das Gebäude ist nicht mehr vorhanden. Bitte erneut auswählen.")
    adopted = query.filter(WorldObjectInstance.deleted_at.is_(None), WorldObjectInstance.object_type_id == "planning_build_area",
        WorldObjectInstance.metadata_json["contourBuilding"]["source"]["buildingId"].astext == building_id).first()
    if adopted is not None:
        raise ValueError("Das Bestandsgebäude wurde inzwischen bearbeitet. Bitte erneut auswählen.")
    roofs = query.filter(WorldObjectInstance.deleted_at.is_(None), WorldObjectInstance.object_type_id.in_(("building_roof", "building_facade_source")),
        WorldObjectInstance.metadata_json["lod2BuildingId"].astext == building_id).all()
    expected_ids = descriptor.get("roofObjectIds")
    if not isinstance(expected_ids, list) or set(expected_ids) != {obj.object_instance_id for obj in roofs}:
        raise ValueError("Die Bestandsdächer wurden inzwischen geändert. Bitte erneut auswählen.")
    if not roofs:
        raise ValueError("Das Bestandsgebäude ist nicht mehr vorhanden.")
    # Reuse exactly the conversion's importer-history and manual-edit guards.
    # Its validation parent is never executed or stored.
    from src.geodata.lod2_building_edit import prepare_lod2_building_edit
    validation_parent = {"type": "PlaceObject", "objectTypeId": "planning_build_area", "objectInstanceId": parent_id,
        "position": {"x": 0, "y": 0, "z": 0}, "metadata": {"voxelOccupancy": "none",
        "contourBuilding": {"source": {"buildingId": building_id, "roofObjectIds": descriptor.get("originalRoofObjectIds", expected_ids)}}}}
    removals = [{"type": "RemoveObject", "objectInstanceId": obj.object_instance_id,
                 "position": {"x": obj.anchor_x, "y": obj.anchor_y, "z": obj.anchor_z}} for obj in roofs]
    _, cleanup, marker = prepare_lod2_building_edit(project=project, universe=universe, world=world,
        payload={"lod2BuildingEdit": {"buildingId": building_id, "parentObjectInstanceId": parent_id}},
        children=[*removals, validation_parent], command_log=command_log)
    if len(removals) > route._get_max_object_batch_commands():
        raise ValueError("Die vollständige Gebäudelöschung überschreitet das Befehlslimit.")
    return removals, cleanup, marker
