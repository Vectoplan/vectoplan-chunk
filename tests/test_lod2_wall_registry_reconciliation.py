"""Regression coverage for legacy LoD2 wall registries and no-op bootstrap."""
from __future__ import annotations

import copy
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _LegacyWall(SimpleNamespace):
    def touch(self, *, updated_by_user_id=None):
        self.revision += 1
        self.updated_by_user_id = updated_by_user_id


def _legacy_wall():
    return _LegacyWall(
        status="deleted",
        deleted_at=object(),
        deprecated_at=object(),
        solid=False,
        opaque=False,
        placeable=False,
        breakable=False,
        selectable=False,
        collidable=False,
        emits_light=True,
        light_level=12,
        render_mode="invisible",
        shape_type="empty",
        material_id="legacy-lod2-wall",
        texture_id="legacy-brick-texture",
        metadata_json={
            "legacyReceipt": "preserve-me",
            "semanticRole": "legacy-wall",
            "color": "#123456",
        },
        revision=4,
        updated_by_user_id="legacy_importer",
    )


def test_reconcile_legacy_lod2_wall_is_complete_preserving_and_idempotent():
    from src.geodata.lod2_conversion import CONSTRUCTION_GRID_VERSION
    from src.geodata.lod2_import import _reconcile_lod2_wall_type

    wall = _legacy_wall()
    assert _reconcile_lod2_wall_type(wall) is True

    assert wall.status == "active"
    assert wall.deleted_at is None and wall.deprecated_at is None
    assert wall.solid and wall.opaque and wall.placeable
    assert wall.breakable and wall.selectable and wall.collidable
    assert not wall.emits_light and wall.light_level == 0
    assert wall.render_mode == "cube" and wall.shape_type == "cube"
    assert wall.material_id == "lod2_exterior_wall"
    assert wall.texture_id is None
    assert wall.metadata_json == {
        "legacyReceipt": "preserve-me",
        "semanticRole": "wall",
        "color": "#f1f3f5",
        "source": "lod2",
        "thicknessAssumed": True,
        "constructionGridVersion": CONSTRUCTION_GRID_VERSION,
        "cellPolicy": "whole-breakable-voxel",
    }
    assert wall.revision == 5
    assert wall.updated_by_user_id == "system_lod2_import"

    assert _reconcile_lod2_wall_type(wall) is False
    assert wall.revision == 5


def test_noop_reimport_and_metadata_maintenance_still_reconcile_registry(monkeypatch):
    import src.geodata.lod2_import as lod2_import

    class _Session:
        def execute(self, _statement):
            return SimpleNamespace(one=lambda: (7,))

        def refresh(self, _value, attribute_names=None):
            del attribute_names

        def flush(self):
            return None

    config = {"materializedBuildings": {"already-imported": {"version": "legacy"}}}
    world = SimpleNamespace(
        id=7,
        metadata_json={"lod2Buildings": copy.deepcopy(config)},
        build_earth_provider=lambda: SimpleNamespace(reference_fingerprint="same-reference"),
    )
    plan = {
        "referenceFingerprint": "same-reference",
        "buildings": [],
        "metadataRepairs": [],
        "heightReference": {"kind": "test"},
    }
    reconciled = []
    monkeypatch.setattr(lod2_import, "db", SimpleNamespace(session=_Session()))
    monkeypatch.setattr(lod2_import, "world_lod2_config", lambda _world: copy.deepcopy(config))
    monkeypatch.setattr(lod2_import, "register_wall", lambda value: reconciled.append(value))
    monkeypatch.setattr(
        lod2_import,
        "apply_facade_metadata_repairs",
        lambda _world, _repairs: {
            "repairedBuildings": 0,
            "repairedRoofObjects": 0,
            "repairedSnapshots": 0,
        },
    )

    assert lod2_import.apply_import(world, plan)["importedBuildings"] == 0
    assert reconciled == [world]

    reconciled.clear()
    result = lod2_import.apply_only_facade_metadata_repairs(world, plan)
    assert result["repairedBuildings"] == 0
    assert reconciled == [world]


@pytest.mark.skipif(
    os.getenv("VECTOPLAN_RUN_LOD2_DB_TESTS") != "1",
    reason="requires the disposable local Chunk integration database",
)
def test_register_wall_repairs_reserved_row_without_touching_foreign_block():
    from wsgi import app
    from extensions import db
    from models.block import BlockRegistry, BlockType
    from routes import commands
    from src.geodata.lod2_conversion import CONSTRUCTION_GRID_VERSION, WALL_BLOCK_ID
    from src.geodata.lod2_import import register_wall

    with app.app_context(), app.test_request_context("/"):
        try:
            nonce = uuid4().hex
            registry = BlockRegistry.create(
                registry_id=f"qa-lod2-reconcile-{nonce}",
                registry_version="1",
                label="LoD2 reconciliation QA",
            )
            db.session.add(registry)
            db.session.flush()
            world = SimpleNamespace(
                block_registry_id=registry.registry_id,
                block_registry_version=registry.registry_version,
            )
            wall = BlockType.create_for_registry(
                registry,
                block_type_id=WALL_BLOCK_ID,
                label="Legacy LoD2 wall",
                status="deleted",
                solid=False,
                opaque=False,
                placeable=False,
                breakable=False,
                selectable=False,
                collidable=False,
                emits_light=True,
                light_level=12,
                render_mode="invisible",
                shape_type="empty",
                material_id="legacy-lod2-wall",
                texture_id="legacy-brick-texture",
                metadata_json={"legacyReceipt": "preserve-me"},
            )
            foreign = BlockType.create_for_registry(
                registry,
                block_type_id=f"qa_foreign_{nonce}",
                label="Unrelated QA block",
                breakable=False,
                placeable=False,
                material_id="foreign-material",
                metadata_json={"sentinel": "unchanged"},
            )
            db.session.add_all([wall, foreign])
            db.session.flush()
            wall_id = wall.id
            stale_revision = wall.revision
            foreign_before = {
                "revision": foreign.revision,
                "breakable": foreign.breakable,
                "placeable": foreign.placeable,
                "material_id": foreign.material_id,
                "metadata": copy.deepcopy(foreign.metadata_json),
            }

            assert register_wall(world).id == wall_id
            db.session.refresh(wall)
            assert wall.is_active and wall.breakable and wall.placeable
            assert wall.deleted_at is None and wall.deprecated_at is None
            assert wall.texture_id is None
            assert wall.metadata_json["legacyReceipt"] == "preserve-me"
            assert wall.metadata_json["constructionGridVersion"] == CONSTRUCTION_GRID_VERSION
            assert wall.revision == stale_revision + 1
            assert wall.updated_by_user_id == "system_lod2_import"
            commands._get_block_type(
                world=world,
                block_type_id=WALL_BLOCK_ID,
                require_breakable=True,
            )

            reconciled_revision = wall.revision
            register_wall(world)
            db.session.refresh(wall)
            assert wall.revision == reconciled_revision

            db.session.refresh(foreign)
            assert {
                "revision": foreign.revision,
                "breakable": foreign.breakable,
                "placeable": foreign.placeable,
                "material_id": foreign.material_id,
                "metadata": foreign.metadata_json,
            } == foreign_before
        finally:
            db.session.rollback()
