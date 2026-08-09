import pytest

from routes.commands import _library_block_registration_context


RUNTIME_BLOCK_TYPE_ID = "vp.hochbau.decken.massivdecken.decke_stahlbeton"


def _library_payload(runtime_block_type_id: str = RUNTIME_BLOCK_TYPE_ID):
    return {
        "type": "SetBlock",
        "blockTypeId": runtime_block_type_id,
        "runtimeBlockTypeId": runtime_block_type_id,
        "metadata": {
            "label": "Decke Stahlbeton - 25 cm",
        },
        "libraryContext": {
            "libraryItemId": "10",
            "familyId": RUNTIME_BLOCK_TYPE_ID,
            "packageId": f"vplib.{RUNTIME_BLOCK_TYPE_ID}",
            "vplibUid": "cafedb8f-da7a-4ab5-af01-01f488ebee0a",
            "variantId": "dicke_250_mm",
            "objectKind": "cell_block",
            "placementCommand": {
                "kind": "SetBlock",
                "runtimeBlockTypeId": runtime_block_type_id,
                "blockTypeId": runtime_block_type_id,
            },
        },
    }


def test_library_context_allows_consistent_vplib_runtime_registration():
    context = _library_block_registration_context(
        _library_payload(),
        block_type_id=RUNTIME_BLOCK_TYPE_ID,
    )

    assert context is not None
    assert context["runtimeBlockTypeId"] == RUNTIME_BLOCK_TYPE_ID
    assert context["libraryItemId"] == "10"
    assert context["familyId"] == RUNTIME_BLOCK_TYPE_ID
    assert context["variantId"] == "dicke_250_mm"
    assert context["category"] == "structure"
    assert context["label"] == "Decke Stahlbeton - 25 cm"


def test_library_context_rejects_runtime_id_mismatch():
    payload = _library_payload("vp.hochbau.waende.wand_stahl")

    with pytest.raises(ValueError, match="does not match"):
        _library_block_registration_context(
            payload,
            block_type_id=RUNTIME_BLOCK_TYPE_ID,
        )


def test_library_context_is_absent_without_explicit_library_context():
    assert (
        _library_block_registration_context(
            {
                "type": "SetBlock",
                "blockTypeId": RUNTIME_BLOCK_TYPE_ID,
                "runtimeBlockTypeId": RUNTIME_BLOCK_TYPE_ID,
            },
            block_type_id=RUNTIME_BLOCK_TYPE_ID,
        )
        is None
    )
