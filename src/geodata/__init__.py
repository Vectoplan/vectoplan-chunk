'''Versioned access to the fixed Vectoplan geodata projects.'''

from .fixed_projects import (
    FIXED_GEODATA_PROJECTS,
    FixedGeodataProject,
    get_fixed_geodata_project,
)

__all__ = (
    'FIXED_GEODATA_PROJECTS',
    'FixedGeodataProject',
    'get_fixed_geodata_project',
)
