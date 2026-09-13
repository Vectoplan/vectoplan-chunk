"""Atomic retirement and optimistic concurrency for an editable building."""
from collections.abc import Mapping


def _reject_legacy_generation(children):
    """Old clients must not save a new generation before retiring the old one.

    Identify actual generation placements, not a parent metadata repair or a
    separate roof edit.  Both parent manifests and child ownership were used
    by the old editor; neither requires reading or materializing a chunk.
    """
    placements = [child for child in children if child.get("type") == "PlaceObject"]
    for parent in placements:
        if parent.get("objectTypeId") != "planning_build_area":
            continue
        parent_id = parent.get("objectInstanceId")
        if not parent_id:
            continue
        metadata = parent.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        manifest = metadata.get("generatedObjects")
        manifest_ids = {ref.get("objectInstanceId") for ref in manifest
                        if isinstance(ref, Mapping) and isinstance(ref.get("objectInstanceId"), str)} \
            if isinstance(manifest, list) else set()
        for child in placements:
            if child is parent:
                continue
            child_metadata = child.get("metadata")
            child_metadata = child_metadata if isinstance(child_metadata, Mapping) else {}
            if (child_metadata.get("generatedFromAreaId") == parent_id
                    or child.get("objectInstanceId") in manifest_ids):
                raise ValueError(
                    "Diese Editor-Version kann Gebäudegenerationen nicht sicher ersetzen. "
                    "Bitte die Seite neu laden und das Gebäude erneut auswählen, bevor es gespeichert wird. "
                    "(planningBuildingEdit fehlt.)")


def prepare_planning_generation(world, payload, children):
    descriptor = payload.get("planningBuildingEdit")
    if descriptor is None:
        _reject_legacy_generation(children)
        return children
    from models import WorldObjectInstance
    from routes import commands as route
    if not isinstance(descriptor, Mapping):
        raise ValueError("planningBuildingEdit must be an object.")
    parent_id = descriptor.get("parentObjectInstanceId")
    parents = [child for child in children if child.get("objectInstanceId") == parent_id
               and child.get("type") == "PlaceObject" and child.get("objectTypeId") == "planning_build_area"]
    if not parent_id or len(parents) != 1:
        raise ValueError("planningBuildingEdit requires exactly one matching building parent.")
    parent = parents[0]
    if parent.get("metadata", {}).get("voxelOccupancy") != "none":
        raise ValueError("The building parent must be metadata only.")
    stored = route._query_without_relationships(WorldObjectInstance.query.filter(
        WorldObjectInstance.world_db_id == world.id,
        WorldObjectInstance.object_instance_id == parent_id)).one_or_none()
    if stored is not None and (stored.deleted_at is not None or stored.object_type_id != "planning_build_area"):
        raise ValueError("The building parent no longer exists.")
    current_generation = (stored.metadata_json or {}).get("generationId") if stored else None
    if current_generation != descriptor.get("previousGenerationId"):
        raise ValueError("Das Gebäude wurde inzwischen geändert. Bitte erneut auswählen, bevor es gespeichert wird.")
    placement_ids = set()
    for child in children:
        if child.get("type") != "PlaceObject":
            raise ValueError("planningBuildingEdit retirement is derived by the server.")
        identity = child.get("objectInstanceId")
        if not identity or identity in placement_ids:
            raise ValueError("Building generation identities must be unique.")
        placement_ids.add(identity)
        if identity != parent_id and child.get("metadata", {}).get("generatedFromAreaId") != parent_id:
            raise ValueError("Every generated child must belong to the declared building.")
    manifest = parent.get("metadata", {}).get("generatedObjects")
    manifest_ids = [ref.get("objectInstanceId") for ref in manifest or [] if isinstance(ref, Mapping)]
    if len(manifest_ids) != len(set(manifest_ids)) or set(manifest_ids) != placement_ids - {parent_id}:
        raise ValueError("The building parent must reference exactly its complete new generation.")
    old = route.db.session.query(WorldObjectInstance.object_instance_id,
        WorldObjectInstance.anchor_x, WorldObjectInstance.anchor_y, WorldObjectInstance.anchor_z).filter(
        WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None),
        WorldObjectInstance.metadata_json["generatedFromAreaId"].astext == parent_id).all()
    # A first LoD2 conversion also retires its imported roofs, whose identity is
    # subsequently validated against immutable importer provenance.
    lod2 = payload.get("lod2BuildingEdit", {})
    if isinstance(lod2, Mapping) and lod2.get("buildingId"):
        old += route.db.session.query(WorldObjectInstance.object_instance_id,
            WorldObjectInstance.anchor_x, WorldObjectInstance.anchor_y, WorldObjectInstance.anchor_z).filter(
            WorldObjectInstance.world_db_id == world.id, WorldObjectInstance.deleted_at.is_(None),
            WorldObjectInstance.object_source == "importer", WorldObjectInstance.object_type_id.in_(("building_roof", "building_facade_source")),
            WorldObjectInstance.metadata_json["lod2BuildingId"].astext == lod2["buildingId"]).all()
    removals = []
    seen = set()
    for obj in old:
        identity = obj.object_instance_id
        if identity in placement_ids:
            raise ValueError("A replacement must use new child identities.")
        if identity in seen:
            continue
        seen.add(identity)
        removals.append({"type": "RemoveObject", "objectInstanceId": identity,
            "position": {"x": obj.anchor_x, "y": obj.anchor_y, "z": obj.anchor_z}})
    # Retirement is part of the same database transaction as every new child.
    # No successful commit can leave an old roof waiting for another HTTP call.
    if len(removals) + len(children) > route._get_max_object_batch_commands():
        raise ValueError("Building replacement exceeds the atomic command limit including retirement.")
    return removals + children


def replay_result(command, payload):
    """Reject identity reuse for a different edit; never infer from geometry."""
    if command.request_payload_json != payload:
        raise ValueError("commandId already belongs to a different command payload.")
    if command.command_status != "applied":
        raise ValueError("The previous command has not completed successfully.")
    return {**(command.result_payload_json or {}), "commandType": command.command_type,
            "affectedCells": [], "replayed": True}
