from types import SimpleNamespace

from src.geodata.structure_streaming import _roof_map_feature


def test_roof_map_feature_is_small_metric_geometry_and_prefers_top_faces():
    row = SimpleNamespace(
        object_instance_id="roof-map-1",
        primary_chunk_key="2:0:3",
        revision=7,
        footprint_json={
            "type": "Polygon",
            "baseY": 4,
            "height": 3,
            "coordinates": [[
                [32, 48], [38, 48], [35, 54], [32, 48],
            ]],
        },
        metadata_json={
            "roofCalculation": {
                "geometry": {
                    "faces": [{
                        "face_ref": "structural",
                        "polygon_3d_mm": [[0, 0, 4000], [4000, 0, 4000], [0, 4000, 7000]],
                    }],
                },
                "roof_build_up": {
                    "top_faces": [{
                        "face_ref": "tiles",
                        "polygon_3d_mm": [
                            [1000, 0, 4200], [5000, 0, 4200], [1000, 4000, 7200],
                        ],
                    }],
                },
            },
        },
    )

    feature = _roof_map_feature(row, cell_size=2)

    assert feature["objectInstanceId"] == "roof-map-1"
    assert feature["revision"] == 7
    assert feature["faces"] == [{
        "faceRef": "tiles",
        "points": [[2.0, 8.4, 0.0], [10.0, 8.4, 0.0], [2.0, 14.4, 8.0]],
    }]
    assert feature["outlines"] == [[
        [64.0, 14.0, 96.0], [76.0, 14.0, 96.0], [70.0, 14.0, 108.0],
    ]]
