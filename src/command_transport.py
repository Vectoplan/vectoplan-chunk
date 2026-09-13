"""Bounded wire decoding for large, otherwise ordinary ObjectBatch commands.

The HTTP server still enforces its existing compressed request-body limit. This
module adds an independent expanded-byte bound before JSON/business validation;
it neither authenticates requests nor executes or persists commands.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import io
import json
import zlib
from collections.abc import Mapping


COMMAND_TRANSPORT_SCHEMA = "vectoplan-command-transport.v1"
COMMAND_TRANSPORT_ENCODING = "gzip-base64"
MAX_EXPANDED_COMMAND_BYTES = 128 * 1024 * 1024


def _reject_json_constant(value):
    raise ValueError(f"Compressed command contains invalid JSON constant {value}.")


def _reject_nested_transport(command):
    """Only the HTTP envelope may be encoded; no encoded child commands."""
    pending = [command]
    while pending:
        candidate = pending.pop()
        if not isinstance(candidate, Mapping):
            continue
        if "transport" in candidate:
            raise ValueError("Nested command transport is not allowed.")
        children = candidate.get("commands")
        if isinstance(children, list):
            pending.extend(children)


def decode_command_transport(payload):
    """Return the canonical ObjectBatch, or an unchanged normal JSON command."""
    if not isinstance(payload, Mapping):
        raise ValueError("Command payload must be a JSON object.")
    if "transport" not in payload:
        _reject_nested_transport(payload)
        return payload
    transport = payload.get("transport")
    if payload.get("type") != "ObjectBatch" or not isinstance(transport, Mapping):
        raise ValueError("Command transport is only supported for ObjectBatch.")
    if transport.get("schemaVersion") != COMMAND_TRANSPORT_SCHEMA:
        raise ValueError("Unsupported command transport schemaVersion.")
    if transport.get("encoding") != COMMAND_TRANSPORT_ENCODING:
        raise ValueError("Unsupported command transport encoding.")
    byte_count = transport.get("uncompressedBytes")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 1:
        raise ValueError("Command transport uncompressedBytes must be a positive integer.")
    if byte_count > MAX_EXPANDED_COMMAND_BYTES:
        raise ValueError("Expanded command exceeds the 128 MiB limit.")
    encoded = transport.get("payload")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("Command transport payload must contain base64 text.")
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Command transport payload is invalid base64.") from exc
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            # Read one byte past the DECLARED bound (already <=128MiB), so a
            # false small size cannot force an expensive full bomb expansion.
            expanded = stream.read(byte_count + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("Command transport payload is invalid gzip.") from exc
    if len(expanded) > MAX_EXPANDED_COMMAND_BYTES:
        raise ValueError("Expanded command exceeds the 128 MiB limit.")
    if len(expanded) != byte_count:
        raise ValueError("Command transport uncompressedBytes does not match the UTF-8 payload size.")
    try:
        canonical = json.loads(expanded.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Expanded command must contain valid UTF-8 JSON.") from exc
    if not isinstance(canonical, dict) or canonical.get("type") != "ObjectBatch":
        raise ValueError("Expanded command must be an ObjectBatch JSON object.")
    _reject_nested_transport(canonical)
    return canonical
