from routes.chunks import _compact_placement_semantics, _is_user_authored_event


def test_user_authored_event_filter_rejects_system_identities():
    assert _is_user_authored_event(None) is False
    assert _is_user_authored_event("") is False
    assert _is_user_authored_event("system") is False
    assert _is_user_authored_event("system_terrain") is False
    assert _is_user_authored_event("vectoplan-editor") is False


def test_user_authored_event_filter_accepts_editor_user():
    assert _is_user_authored_event("editor_user") is True
    assert _is_user_authored_event("usr_123") is True


def test_legacy_editor_set_block_is_a_user_placement():
    assert _is_user_authored_event(
        None,
        command_source="editor",
        command_type="SetBlock",
    ) is True
    assert _is_user_authored_event(
        None,
        command_source="system",
        command_type="SetBlock",
    ) is False


def test_compact_placement_semantics_keeps_library_identity_and_real_dimensions():
    semantics = _compact_placement_semantics(
        {
            "blockTypeId": "wall_runtime",
            "metadata": {
                "libraryPlacementContext": {
                    "source": "library",
                    "libraryItemId": "7",
                    "familyId": "vp.hochbau.waende.wand_mauerwerk",
                    "variantId": "dicke_365_mm",
                    "libraryRef": {
                        "category": "waende",
                        "objectKind": "block",
                    },
                    "semanticProfile": {
                        "role": "wall",
                        "definitionValues": {
                            "dimensions.thickness_mm": 365,
                            "dimensions.height_mm": 1000,
                            "nested": {"must": "not leak"},
                        },
                    },
                }
            },
        }
    )

    assert semantics["runtimeBlockTypeId"] == "wall_runtime"
    assert semantics["library"]["libraryItemId"] == "7"
    assert semantics["library"]["variantId"] == "dicke_365_mm"
    assert semantics["classification"]["role"] == "wall"
    assert semantics["variables"]["dimensions.thickness_mm"] == 365
    assert "nested" not in semantics["variables"]
    assert _is_user_authored_event(
        None,
        command_source="editor",
        command_type="RemoveBlock",
    ) is False
