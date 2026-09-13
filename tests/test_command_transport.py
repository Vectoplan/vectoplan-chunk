"""Strict standard-library wire validation before any command executor runs."""
import base64
import gzip
import json

import pytest

from src import command_transport as transport


def _wire(raw, *, byte_count=None):
    return {"type": "ObjectBatch", "transport": {
        "schemaVersion": transport.COMMAND_TRANSPORT_SCHEMA,
        "encoding": "gzip-base64",
        "uncompressedBytes": len(raw) if byte_count is None else byte_count,
        "payload": base64.b64encode(gzip.compress(raw)).decode("ascii"),
    }}


def _json_wire(payload):
    return _wire(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def test_utf8_payload_round_trip_preserves_exact_canonical_command():
    payload = {"type": "ObjectBatch", "commandId": "building-command", "userId": "editor_user",
               "position": {"x": 0, "y": 0, "z": 0}, "commands": [],
               "metadata": {"label": "Gebäude Süd – Straße", "transport": {"mode": "tram"}}}
    wire = _json_wire(payload)
    assert transport.decode_command_transport(wire) == payload
    assert "transport" not in transport.decode_command_transport(wire)
    assert transport.decode_command_transport(payload) is payload


@pytest.mark.parametrize("field,value", [
    ("schemaVersion", "unknown"), ("encoding", "gzip"),
    ("uncompressedBytes", True), ("uncompressedBytes", 1.0),
    ("uncompressedBytes", 0), ("uncompressedBytes", -1),
    ("uncompressedBytes", transport.MAX_EXPANDED_COMMAND_BYTES + 1),
    ("payload", ""), ("payload", "%%%%"), ("payload", "ümlaut"), ("payload", 17),
])
def test_malformed_transport_descriptors_are_rejected(field, value):
    wire = _json_wire({"type": "ObjectBatch"})
    wire["transport"][field] = value
    with pytest.raises(ValueError):
        transport.decode_command_transport(wire)


@pytest.mark.parametrize("canonical", [[], None, "ObjectBatch", 8, {"type": "SetBlock"}, {"type": "objectbatch"}])
def test_decoded_value_must_be_an_object_batch_object(canonical):
    with pytest.raises(ValueError, match="ObjectBatch JSON object"):
        transport.decode_command_transport(_json_wire(canonical))


@pytest.mark.parametrize("raw", [b"{broken", b"\xff", b'{"type":"ObjectBatch","x":NaN}'])
def test_invalid_json_utf8_and_non_finite_json_are_rejected(raw):
    with pytest.raises(ValueError):
        transport.decode_command_transport(_wire(raw))


def test_invalid_truncated_and_corrupt_gzip_are_rejected():
    canonical = b'{"type":"ObjectBatch"}'
    compressed = gzip.compress(canonical)
    for raw in (b"ordinary uncompressed text", compressed[:-4], compressed[:-8] + b"\xff" * 8):
        wire = _wire(canonical)
        wire["transport"]["payload"] = base64.b64encode(raw).decode("ascii")
        with pytest.raises(ValueError, match="invalid gzip"):
            transport.decode_command_transport(wire)


def test_size_is_an_exact_utf8_byte_count():
    raw = '{"type":"ObjectBatch","label":"Gebäude"}'.encode("utf-8")
    for count in (len(raw) - 1, len(raw) + 1, len(raw.decode("utf-8"))):
        with pytest.raises(ValueError, match="does not match"):
            transport.decode_command_transport(_wire(raw, byte_count=count))


def test_bomb_reads_only_one_byte_beyond_declared_and_expansion_limits(monkeypatch):
    original_gzip_file = gzip.GzipFile
    reads = []
    class ObservedGzipFile(original_gzip_file):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)
    monkeypatch.setattr(transport.gzip, "GzipFile", ObservedGzipFile)
    monkeypatch.setattr(transport, "MAX_EXPANDED_COMMAND_BYTES", 4096)
    wire = _wire(b"x" * 1_000_000, byte_count=32)
    with pytest.raises(ValueError, match="does not match"):
        transport.decode_command_transport(wire)
    assert reads == [33]
    reads.clear()
    wire["transport"]["uncompressedBytes"] = 4096
    with pytest.raises(ValueError, match="128 MiB limit"):
        transport.decode_command_transport(wire)
    assert reads == [4097]
    reads.clear()
    wire["transport"]["uncompressedBytes"] = 4097
    with pytest.raises(ValueError, match="128 MiB limit"):
        transport.decode_command_transport(wire)
    assert reads == []


def test_nested_envelopes_and_nested_encoded_children_are_rejected():
    nested = _json_wire({"type": "ObjectBatch"})
    for canonical in (nested, {"type": "ObjectBatch", "commands": [nested]},
                      {"type": "ObjectBatch", "commands": [{"type": "PlaceObject", "transport": {}}]}):
        with pytest.raises(ValueError, match="Nested command transport"):
            transport.decode_command_transport(_json_wire(canonical))
    with pytest.raises(ValueError, match="Nested command transport"):
        transport.decode_command_transport({"type": "ObjectBatch", "commands": [nested]})


@pytest.mark.parametrize("wire", [[], {"type": "SetBlock", "transport": {}}, {"type": "ObjectBatch", "transport": None}])
def test_invalid_outer_envelopes_are_rejected(wire):
    with pytest.raises(ValueError):
        transport.decode_command_transport(wire)
