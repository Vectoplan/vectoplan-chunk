from types import SimpleNamespace
import math

import pytest

from src.geodata.tree_instances import TreePointService, tree_features, tree_object_id, tree_yaw, terrain_height


def _chunk(cx=0, cy=0, cz=0):
    return {"chunkX": cx, "chunkY": cy, "chunkZ": cz, "chunkSize": 2,
            "metadata": {"terrainSurface": {"cornerHeights": [0, 1, 2, 2, 4, 5, 4, 6, 7]}}}


def test_species_appearance_is_stable_and_independent_of_grounding():
    from src.geodata.tree_instances import tree_render_profile
    for species, form in [("Tilia cordata", "broadleaf"), ("Pinus sylvestris", "conifer"),
                          ("Populus nigra italica", "columnar")]:
        profile = tree_render_profile("source-point-1", species)
        assert profile == tree_render_profile("source-point-1", species)
        assert profile["crownForm"] == form
        assert 0 <= profile["variation"] <= 1


def test_tree_uses_exact_terrain_triangles_including_negative_heights():
    chunk = _chunk()
    assert terrain_height(chunk, .75, .25, fallback=10) == 1.5
    assert terrain_height(chunk, .25, .75, fallback=10) == 2
    chunk["metadata"]["terrainSurface"]["cornerHeights"] = [-3] * 9
    assert terrain_height(chunk, 1.5, 1.5, fallback=10) == -3
    assert terrain_height({}, 0, 0, fallback=4) == 4


def test_stable_identity_orientation_and_tombstone_filtering(monkeypatch):
    from src.geodata import visual_overlays
    monkeypatch.setattr(visual_overlays, "_wgs84_to_local", lambda _, lon, lat: (lon, lat))
    point = {"id": "native-tree-id", "longitude": .5, "latitude": .5, "heightM": 12}
    world = SimpleNamespace(surface_y=0)
    kwargs = dict(world=world, provider=None, chunk=_chunk(), points=[point])
    features = tree_features(**kwargs, removed=set())
    # An exact upper face at y=2 belongs to its supporting lower layer.
    assert features[0]["position"] == [.5, 2, .5]
    assert features[0]["heightM"] == 12
    assert features[0]["objectInstanceId"] == tree_object_id(point["id"])
    assert features[0]["source"] == {"treeId": point["id"], "longitude": .5, "latitude": .5}
    assert 0 <= tree_yaw(point["id"]) < math.tau
    assert tree_yaw(point["id"]) == features[0]["yawRadians"]
    assert tree_features(**kwargs, removed={tree_object_id(point["id"])}) == []
    kwargs["chunk"] = _chunk(cy=1)
    assert tree_features(**kwargs, removed=set()) == []


def test_chunk_boundary_points_have_one_owner_and_no_phantom_defaults(monkeypatch):
    from src.geodata import visual_overlays
    monkeypatch.setattr(visual_overlays, "_wgs84_to_local", lambda _, lon, lat: (lon, lat))
    points = [{"id": "border", "longitude": 2, "latitude": 0}]
    assert tree_features(world=SimpleNamespace(surface_y=0), provider=None, chunk=_chunk(), points=points, removed=set()) == []
    assert tree_features(world=SimpleNamespace(surface_y=0), provider=None, chunk=_chunk(), points=[], removed=set()) == []
    assert tree_object_id("a") != tree_object_id("b")


def test_tree_service_pages_caches_by_release_and_never_creates_missing_points():
    calls = []
    client = SimpleNamespace(approved_publication=lambda _: {"release_key": "one"})
    def request(path):
        calls.append(path)
        return {"ok": True, "schemaVersion": "vectoplan-tree-points.v1", "release_key": "one",
                "items": [{"id": "a" if len(calls) == 1 else "b"}], "nextOffset": 1 if len(calls) == 1 else None}
    client._request = request
    service = TreePointService(client)
    assert service.points((13, 52, 13.001, 52.001)) == ("one", [{"id": "a"}, {"id": "b"}])
    assert service.points((13, 52, 13.001, 52.001))[0] == "one"
    assert len(calls) == 2
    unavailable = TreePointService(SimpleNamespace(approved_publication=lambda _: (_ for _ in ()).throw(ValueError("not uploaded"))))
    assert unavailable.points((13, 52, 13.001, 52.001)) == ("", [])


def test_invalid_or_mismatched_tree_publication_cannot_enter_scene():
    client = SimpleNamespace(approved_publication=lambda _: {"release_key": "approved"},
                             _request=lambda _: {"schemaVersion": "vectoplan-tree-points.v1", "release_key": "different", "items": []})
    with pytest.raises(ValueError, match="approved publication"):
        TreePointService(client).points((13, 52, 13.001, 52.001))


def test_tree_query_failure_has_a_circuit_breaker_for_chunk_streaming():
    calls = []
    def fail(_):
        calls.append(True)
        raise TimeoutError("optional tree source offline")
    service = TreePointService(SimpleNamespace(approved_publication=lambda _: {"release_key": "approved"}, _request=fail))
    with pytest.raises(TimeoutError):
        service.points((13, 52, 13.001, 52.001))
    assert service.points((13.001, 52, 13.002, 52.001)) == ("", [])
    assert len(calls) == 1


# Reuse only the isolated database fixture; importing it does not start wsgi.
@pytest.fixture
def imported_building_world():
    import os
    if os.getenv("VECTOPLAN_RUN_DB_INTEGRATION_TESTS") != "1":
        pytest.skip("requires the disposable local Chunk integration database")
    from tests.test_lod2_building_edit import imported_building_world as fixture
    yield from fixture.__wrapped__()


def test_source_tree_remove_object_persists_tombstone_without_touching_voxels(imported_building_world, monkeypatch):
    from extensions import db
    from models.object import WorldObjectInstance
    from src.geodata import tree_instances, visual_overlays
    from src.world.earth import terrain_pipeline
    from sqlalchemy.orm import noload
    fixture = imported_building_world
    # Keep the production flat fixture's chunk generator. Only the source
    # resolver sees an Earth provider here; point/terrain transforms have their
    # own tests and no network request belongs inside a database regression.
    real_materialize = tree_instances.materialize_tree_for_removal
    def materialize(**kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(type(fixture.world), "is_earth_world", property(lambda _: True))
            patch.setattr(type(fixture.world), "build_earth_provider", lambda _: None)
            patch.setattr(visual_overlays, "_wgs84_to_local", lambda *_: (10.5, 10.5))
            patch.setattr(terrain_pipeline, "generate_earth_terrain_chunk", lambda **_: {"chunkSize": 16, "chunkX": 0, "chunkZ": 0,
                "metadata": {"terrainSurface": {"cornerHeights": [15.0] * 289}}})
            return real_materialize(**kwargs)
    monkeypatch.setattr(tree_instances, "materialize_tree_for_removal", materialize)
    source = {"id": "fixture-native-tree", "longitude": 13.405, "latitude": 52.52}
    monkeypatch.setattr(tree_instances, "get_tree_service", lambda: SimpleNamespace(points=lambda _: ("approved-test", [source])))
    before = [fixture.state(position)["blockTypeId"] for position in fixture.positions]
    object_id = tree_object_id(source["id"])
    command = {"type": "RemoveObject", "objectInstanceId": object_id,
               "treeSource": {"treeId": source["id"], "longitude": source["longitude"], "latitude": source["latitude"]}}
    _, result = fixture.execute(command)
    assert result["changed"] is True and result["clearedCellCount"] == 0
    assert [fixture.state(position)["blockTypeId"] for position in fixture.positions] == before
    db.session.commit()
    fixture.project, fixture.universe, fixture.world = fixture.refresh()
    row = WorldObjectInstance.query.options(noload("*")).filter_by(world_db_id=fixture.world.id, object_instance_id=object_id).one()
    assert row.deleted_at is not None and row.metadata_json["voxelOccupancy"] == "none"
    assert tree_instances._removed_ids(fixture.world, [object_id]) == {object_id}
    # A repeated removal cannot reimport the source or collide with its stable ID.
    fixture.execute(command)
    assert WorldObjectInstance.query.filter_by(world_db_id=fixture.world.id, object_instance_id=object_id).count() == 1
