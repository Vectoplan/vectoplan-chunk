"""Code-owned natural world blocks used by the Earth provider.

All terrain variants deliberately retain kind='terrain'. Consumers can render
or analyse a more specific material through terrain.subtype without losing the
common terrain semantics.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ..contracts import SystemBlockDefinition


def _natural_metadata(
    *,
    semantic_role: str,
    subtype: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "builtIn": True,
        "adminBlock": True,
        "semanticRole": semantic_role,
        "runtimeRole": "natural_world_system_block",
        "storageMode": "persistent_block_type",
        "commands": {
            "placementCommand": "SetBlock",
            "removalCommand": "RemoveBlock",
        },
    }
    if subtype is not None:
        metadata["terrain"] = {
            "baseSystemBlockId": "system_terrain",
            "family": "terrain",
            "subtype": subtype,
        }
    return metadata


def _terrain_definition(
    *,
    system_block_id: str,
    label: str,
    subtype: str,
    description: str,
    material_id: str,
    texture_id: str,
    inventory_visible: bool = False,
) -> SystemBlockDefinition:
    return SystemBlockDefinition(
        system_block_id=system_block_id,
        runtime_block_type_id=system_block_id,
        label=label,
        description=description,
        kind="terrain",
        definition_version="1.0.0",
        persist_as_block_type=True,
        inventory_visible=inventory_visible,
        solid=True,
        opaque=True,
        placeable=True,
        breakable=True,
        selectable=True,
        collidable=True,
        hardness=1.0 if subtype != "rock" else 2.5,
        stack_size=64,
        render_mode="cube",
        shape_type="cube",
        material_id=material_id,
        texture_id=texture_id,
        icon_id=system_block_id,
        aliases=(subtype,),
        metadata=_natural_metadata(
            semantic_role="terrain",
            subtype=subtype,
        ),
    )


@lru_cache(maxsize=1)
def get_water_definition() -> SystemBlockDefinition:
    return SystemBlockDefinition(
        system_block_id="system_water",
        runtime_block_type_id="system_water",
        label="Wasser",
        description="Systemverwalteter Wasserblock für natürliche Weltdaten.",
        kind="water",
        definition_version="1.0.0",
        persist_as_block_type=True,
        inventory_visible=False,
        solid=False,
        opaque=False,
        placeable=False,
        breakable=True,
        selectable=True,
        collidable=False,
        hardness=0.1,
        stack_size=64,
        render_mode="cube",
        shape_type="cube",
        material_id="natural_water",
        texture_id="natural_water",
        icon_id="system_water",
        aliases=("water",),
        metadata=_natural_metadata(semantic_role="water"),
    )


@lru_cache(maxsize=1)
def get_terrain_definition() -> SystemBlockDefinition:
    return _terrain_definition(
        system_block_id="system_terrain",
        label="Terrain",
        subtype="generic",
        description="Generischer Terrain-Adminblock für Editor- und Importtests.",
        material_id="natural_terrain",
        texture_id="natural_terrain",
        inventory_visible=True,
    )


@lru_cache(maxsize=1)
def get_terrain_humus_definition() -> SystemBlockDefinition:
    return _terrain_definition(
        system_block_id="system_terrain_humus",
        label="Terrain · Humus",
        subtype="humus",
        description="Organische oberste Schicht der Terrain-Familie.",
        material_id="natural_humus",
        texture_id="natural_humus",
    )


@lru_cache(maxsize=1)
def get_terrain_soil_definition() -> SystemBlockDefinition:
    return _terrain_definition(
        system_block_id="system_terrain_soil",
        label="Terrain · Erdreich",
        subtype="soil",
        description="Tragende Erdschicht unterhalb der Humusoberfläche.",
        material_id="natural_soil",
        texture_id="natural_soil",
    )


@lru_cache(maxsize=1)
def get_terrain_rock_definition() -> SystemBlockDefinition:
    return _terrain_definition(
        system_block_id="system_terrain_rock",
        label="Terrain · Fels",
        subtype="rock",
        description="Feste Felsschicht der Terrain-Familie.",
        material_id="natural_rock",
        texture_id="natural_rock",
    )


__all__ = [
    "get_terrain_definition",
    "get_terrain_humus_definition",
    "get_terrain_rock_definition",
    "get_terrain_soil_definition",
    "get_water_definition",
]
