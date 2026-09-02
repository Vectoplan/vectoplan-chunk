"""Versioned geodata-to-editor dataset pipeline.

The package prepares deterministic, content-addressed artifacts.  Runtime
mutation remains in the canonical Chunk/WorldEdit command path; this dataset is
the auditable hand-off between source transformation and those commands.
"""

from .pipeline import (
    EDITOR_DATASET_SCHEMA_VERSION,
    EditorDatasetPipeline,
    build_editor_dataset,
    validate_editor_dataset,
    write_editor_dataset,
)
from .runtime import (
    SELECTOR_SCHEMA_VERSION,
    activate_editor_dataset,
    attach_active_editor_dataset,
    editor_dataset_lod2_plan,
    load_active_editor_dataset,
    load_editor_dataset_bundle,
    load_editor_dataset_chunk,
)

__all__ = (
    "EDITOR_DATASET_SCHEMA_VERSION",
    "EditorDatasetPipeline",
    "SELECTOR_SCHEMA_VERSION",
    "activate_editor_dataset",
    "attach_active_editor_dataset",
    "build_editor_dataset",
    "editor_dataset_lod2_plan",
    "load_active_editor_dataset",
    "load_editor_dataset_bundle",
    "load_editor_dataset_chunk",
    "validate_editor_dataset",
    "write_editor_dataset",
)
