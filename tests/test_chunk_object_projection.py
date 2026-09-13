from copy import deepcopy
from src.chunk_object_projection import project_chunk_construction_refs


def test_chunk_projection_keeps_exact_geometry_and_deleted_cell_ownership():
    cells = [{"x": x, "y": y, "z": -1, "minimumY": y + .1, "maximumY": y + .7,
              "footprintPolygons": [[[x, -1], [x + .2, -1], [x, -.8]]]}
             for x, y in [(-17, 2), (-16, 2), (-1, 2), (0, 2), (-1, 16)]]
    ref = {"objectInstanceId": "wall", "occupiedCells": [dict(c) for c in cells if c["x"] != -1],
           "metadata": {"renderProfile": "construction-grid", "constructionCells": cells,
                        "generatedFromAreaId": "parent"}}
    roof = {"objectInstanceId": "roof", "metadata": {"roofCalculation": {"complete": True}}}
    chunk = {"chunkX": -1, "chunkY": 0, "chunkZ": -1, "chunkSize": 16,
             "cells": [1, 0], "objectRefs": [ref, roof]}
    original = deepcopy(chunk)
    projected = project_chunk_construction_refs(chunk)
    assert [c["x"] for c in projected["objectRefs"][0]["metadata"]["constructionCells"]] == [-16, -1]
    assert [c["x"] for c in projected["objectRefs"][0]["occupiedCells"]] == [-16]
    assert projected["objectRefs"][0]["metadata"]["constructionCells"][0] == cells[1]
    assert projected["objectRefs"][1] == roof
    assert projected["cells"] == original["cells"]
    assert chunk == original
    assert project_chunk_construction_refs(projected) == projected


def test_projections_partition_all_construction_prisms_without_loss_or_duplicates():
    cells = [{"x": x, "y": y, "z": z} for x in range(-18, 18) for y in range(18) for z in [-1, 0]]
    refs = [{"occupiedCells": cells, "metadata": {"renderProfile": "construction-grid", "constructionCells": cells}}]
    result = []
    for x in [-2, -1, 0, 1]:
        for y in [0, 1]:
            for z in [-1, 0]:
                projected = project_chunk_construction_refs({"chunkX": x, "chunkY": y, "chunkZ": z,
                                                            "chunkSize": 16, "objectRefs": refs})
                result.extend(projected["objectRefs"][0]["metadata"]["constructionCells"])
    assert sorted((c["x"], c["y"], c["z"]) for c in result) == sorted((c["x"], c["y"], c["z"]) for c in cells)


def test_single_pass_snapshot_fingerprint_preserves_existing_hash_and_byte_size():
    from datetime import datetime
    from models.chunk import (normalize_content_json, compute_content_hash, estimate_content_size_bytes,
                              _normalized_content_fingerprint)
    for raw in [None, {"cells": [0, -1, 2], "metadata": {2: "Bäume", "stamp": datetime(2026, 9, 5)}}]:
        content = normalize_content_json(raw)
        for binary in [None, b"binary-test"]:
            if content is None and binary is None:
                continue
            assert _normalized_content_fingerprint(content, binary) == (
                compute_content_hash(content_json=content, content_binary=binary),
                estimate_content_size_bytes(content_json=content, content_binary=binary),
            )


def test_batch_projection_cache_keeps_revised_object_refs_independent():
    from src.batch_chunk_mutation import batch_chunk_mutations
    from src.frozen_json import freeze_json
    cells = [{'x': 0, 'y': 0, 'z': 0}, {'x': 16, 'y': 0, 'z': 0}]
    ref = freeze_json({'occupiedCells': cells, 'metadata': {'renderProfile': 'construction-grid', 'constructionCells': cells}})
    content = {'chunkX': 0, 'chunkY': 0, 'chunkZ': 0, 'objectRefs': [ref]}
    with batch_chunk_mutations():
        first = project_chunk_construction_refs(content)['objectRefs'][0]
        assert project_chunk_construction_refs(content)['objectRefs'][0] is first
        changed = freeze_json({**ref, 'occupiedCells': []})
        second = project_chunk_construction_refs({**content, 'objectRefs': [changed]})['objectRefs'][0]
        assert second['occupiedCells'] == [] and first['occupiedCells'] == [cells[0]]
        assert project_chunk_construction_refs({**content, 'chunkX': 1})['objectRefs'][0]['occupiedCells'] == [cells[1]]
    assert project_chunk_construction_refs(content)['objectRefs'][0] is not first
