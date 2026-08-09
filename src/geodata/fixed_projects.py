'''Canonical links between Chunk and the fixed data-pipeline projects.

The dataset ids are shared by the Webscraper and GeoServer Orchestrator.  Code
outside this module must not duplicate display names, slugs or service paths.
The declarations are capabilities, not promises that a release is currently
approved.  Runtime clients still verify the approved release before use.
'''

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class FixedGeodataProject:
    dataset_id: str
    display_name: str
    category: str
    capabilities: tuple[str, ...]

    @property
    def publication_path(self) -> str:
        return f'/admin/api/production-publications/{self.dataset_id}'

    @property
    def query_path(self) -> str:
        return f'{self.publication_path}/query'

    @property
    def query_schema_path(self) -> str:
        return f'{self.query_path}/schema'

    @property
    def terrain_grid_path(self) -> str:
        return f'{self.publication_path}/terrain-grid'


DIGITAL_ELEVATION_MODEL: Final[FixedGeodataProject] = FixedGeodataProject(
    dataset_id='digitales-gelaendemodell-5m',
    display_name='Digitales Geländemodell-5m',
    category='terrain',
    capabilities=('height-point', 'height-grid', 'earth-chunk-terrain'),
)

THREE_D_BUILDINGS: Final[FixedGeodataProject] = FixedGeodataProject(
    dataset_id='3d-gebaeudedaten',
    display_name='3D-Gebäudedaten',
    category='buildings-3d',
    capabilities=('citygml', 'lod2', 'earth-object-import'),
)

ACTUAL_USE: Final[FixedGeodataProject] = FixedGeodataProject(
    dataset_id='tatsaechliche-nutzung',
    display_name='Tatsächliche Nutzung',
    category='land-use',
    capabilities=('land-use', 'earth-surface-classification'),
)

BUILDING_FOOTPRINTS: Final[FixedGeodataProject] = FixedGeodataProject(
    dataset_id='hausumringe',
    display_name='Hausumringe',
    category='building-footprints',
    capabilities=('vector', 'earth-object-footprints'),
)

PARCELS: Final[FixedGeodataProject] = FixedGeodataProject(
    dataset_id='flurstuecke',
    display_name='Flurstücke',
    category='parcels',
    capabilities=('vector', 'earth-parcel-boundaries'),
)

FIXED_GEODATA_PROJECTS: Final[dict[str, FixedGeodataProject]] = {
    item.dataset_id: item
    for item in (
        DIGITAL_ELEVATION_MODEL,
        THREE_D_BUILDINGS,
        ACTUAL_USE,
        BUILDING_FOOTPRINTS,
        PARCELS,
    )
}


def get_fixed_geodata_project(dataset_id: str) -> FixedGeodataProject:
    normalized = str(dataset_id or '').strip().lower()
    try:
        return FIXED_GEODATA_PROJECTS[normalized]
    except KeyError as exc:
        raise KeyError(f'Unknown fixed geodata project: {dataset_id!r}') from exc


__all__ = (
    'ACTUAL_USE',
    'BUILDING_FOOTPRINTS',
    'DIGITAL_ELEVATION_MODEL',
    'FIXED_GEODATA_PROJECTS',
    'FixedGeodataProject',
    'PARCELS',
    'THREE_D_BUILDINGS',
    'get_fixed_geodata_project',
)
