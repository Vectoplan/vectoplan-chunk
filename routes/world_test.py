# routes/world_test.py
"""
VECTOPLAN World Test Routes.

Diese Datei stellt eine reine Debug-/Test-Oberfläche für die neue World-Schicht
bereit.

Ziel:
- erkannte Weltmodelle aus src/world anzeigen
- gültige und ungültige Provider sichtbar machen
- world.json-/World-Metadaten anzeigen
- Blockliste / Palette anzeigen
- generierte Chunk-JSON-Daten anzeigen
- Koordinatenbewegung mit WASD und Pfeiltasten testen
- Chunk-Wechsel bei Bewegung sichtbar machen
- negative Koordinaten korrekt prüfen

Wichtig:
Diese Route ist keine finale Editor-API.

Sie ist eine Diagnose- und Entwicklungshilfe für Phase 1:
- keine Datenbank
- keine Snapshots
- keine Events
- keine Commands
- keine Persistenz
- kein Three.js
- keine Core-Anbindung
- keine Library-Anbindung

Spätere produktive Routen bleiben separat, z. B.:

    GET /worlds/<world_id>
    GET /chunks
    POST /chunks/batch
    POST /commands

Diese Datei darf daher bewusst HTML und kleine Debug-API-Endpunkte enthalten.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

try:
    from flask import Blueprint, Response, jsonify, request
except Exception as exc:  # pragma: no cover - Flask must exist at runtime
    raise RuntimeError(
        "routes.world_test requires Flask to be installed and importable."
    ) from exc

try:
    from src.world.discovery import (
        create_registry_from_discovered_worlds,
        discover_worlds,
        discover_worlds_as_dict,
        get_discovered_world,
        reset_world_discovery_cache,
    )
    from src.world.loader import create_default_world_loader
    from src.world.serializer import (
        get_error_status_code,
        serialize_chunk_response,
        serialize_error_response,
        serialize_generated_chunk,
        serialize_world_definition,
        serialize_world_metadata_response,
    )
    from src.world.service import WorldService
except Exception as exc:  # pragma: no cover - startup/import guard
    raise RuntimeError(
        "routes.world_test requires the src.world package to be available."
    ) from exc


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

WORLD_TEST_ROUTE_VERSION: Final[str] = "0.1.0"

DEFAULT_WORLD_ID: Final[str] = "flat"
DEFAULT_STEP_SIZE: Final[int] = 1
DEFAULT_START_X: Final[int] = 0
DEFAULT_START_Y: Final[int] = 1
DEFAULT_START_Z: Final[int] = 0

MAX_ABS_CHUNK_COORDINATE: Final[int] = 1_000_000
MAX_ABS_WORLD_COORDINATE: Final[int] = 16_000_000

world_test_bp = Blueprint(
    "world_test",
    __name__,
    url_prefix="/world-test",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Wandelt einen beliebigen Wert defensiv in einen bereinigten String um.
    """
    if value is None:
        return default

    try:
        text = str(value).strip()
    except Exception:
        return default

    return text if text else default


def _to_int(
    value: Any,
    *,
    field_name: str,
    default: int = 0,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """
    Wandelt einen Query-/JSON-Wert robust in int um.
    """
    if value is None or value == "":
        converted = default
    else:
        try:
            converted = int(value)
        except Exception as exc:
            raise ValueError(f"Field '{field_name}' must be an integer.") from exc

    if minimum is not None and converted < minimum:
        raise ValueError(f"Field '{field_name}' must be >= {minimum}.")

    if maximum is not None and converted > maximum:
        raise ValueError(f"Field '{field_name}' must be <= {maximum}.")

    return converted


def _to_bool(value: Any, *, default: bool = False) -> bool:
    """
    Wandelt typische Query-Werte robust in bool um.
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int | float):
        return bool(value)

    text = _safe_str(value).lower()

    if text in {"1", "true", "yes", "y", "on"}:
        return True

    if text in {"0", "false", "no", "n", "off"}:
        return False

    return default


def _json_safe(value: Any) -> Any:
    """
    Minimaler JSON-Safe-Fallback für Routen-Hilfsdaten.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Mapping):
        return {
            _safe_str(key): _json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]

    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _json_safe(value.to_dict())
        except Exception:
            return _safe_str(value)

    if hasattr(value, "__dict__"):
        try:
            return _json_safe(vars(value))
        except Exception:
            return _safe_str(value)

    return _safe_str(value)


def _json_response(payload: Mapping[str, Any], *, status_code: int = 200) -> Any:
    """
    Gibt eine JSON-Antwort zurück.
    """
    response = jsonify(_json_safe(dict(payload)))
    response.status_code = status_code
    return response


def _ok(payload: Mapping[str, Any] | None = None, *, status_code: int = 200) -> Any:
    """
    Einheitliche erfolgreiche Debug-API-Antwort.
    """
    body: dict[str, Any] = {
        "ok": True,
        "routeVersion": WORLD_TEST_ROUTE_VERSION,
    }

    if payload:
        body.update(dict(payload))

    return _json_response(body, status_code=status_code)


def _error(error: BaseException, *, status_code: int | None = None) -> Any:
    """
    Einheitliche Fehlerantwort für Debug-API-Endpunkte.
    """
    try:
        body = serialize_error_response(
            error,
            include_status_code=False,
            include_debug=True,
        )
        body["routeVersion"] = WORLD_TEST_ROUTE_VERSION
        resolved_status_code = status_code or get_error_status_code(error)
        return _json_response(body, status_code=resolved_status_code)
    except Exception:
        return _json_response(
            {
                "ok": False,
                "routeVersion": WORLD_TEST_ROUTE_VERSION,
                "error": {
                    "code": "world_test_error_response_failed",
                    "message": "Could not serialize error response.",
                    "details": {
                        "errorType": type(error).__name__,
                        "error": _safe_str(error),
                    },
                },
            },
            status_code=status_code or 500,
        )


def _get_world_id(default: str = DEFAULT_WORLD_ID) -> str:
    """
    Liest worldId aus Query-Parametern.
    """
    return _safe_str(
        request.args.get("worldId")
        or request.args.get("world")
        or request.args.get("id"),
        default=default,
    )


def _get_force_refresh() -> bool:
    """
    Liest forceRefresh/refresh aus Query-Parametern.
    """
    return _to_bool(
        request.args.get("forceRefresh")
        or request.args.get("refresh"),
        default=False,
    )


def _get_include_invalid(default: bool = True) -> bool:
    """
    Liest includeInvalid aus Query-Parametern.
    """
    return _to_bool(
        request.args.get("includeInvalid"),
        default=default,
    )


def _get_include_raw_config(default: bool = False) -> bool:
    """
    Liest includeRawConfig aus Query-Parametern.
    """
    return _to_bool(
        request.args.get("includeRawConfig"),
        default=default,
    )


def _get_chunk_coords_from_query() -> tuple[int, int, int]:
    """
    Liest chunkX/chunkY/chunkZ aus Query-Parametern.
    """
    chunk_x = _to_int(
        request.args.get("chunkX"),
        field_name="chunkX",
        default=0,
        minimum=-MAX_ABS_CHUNK_COORDINATE,
        maximum=MAX_ABS_CHUNK_COORDINATE,
    )
    chunk_y = _to_int(
        request.args.get("chunkY"),
        field_name="chunkY",
        default=0,
        minimum=-MAX_ABS_CHUNK_COORDINATE,
        maximum=MAX_ABS_CHUNK_COORDINATE,
    )
    chunk_z = _to_int(
        request.args.get("chunkZ"),
        field_name="chunkZ",
        default=0,
        minimum=-MAX_ABS_CHUNK_COORDINATE,
        maximum=MAX_ABS_CHUNK_COORDINATE,
    )

    return chunk_x, chunk_y, chunk_z


def _get_world_coords_from_query() -> tuple[int, int, int]:
    """
    Liest worldX/worldY/worldZ aus Query-Parametern.
    """
    world_x = _to_int(
        request.args.get("worldX") or request.args.get("x"),
        field_name="worldX",
        default=DEFAULT_START_X,
        minimum=-MAX_ABS_WORLD_COORDINATE,
        maximum=MAX_ABS_WORLD_COORDINATE,
    )
    world_y = _to_int(
        request.args.get("worldY") or request.args.get("y"),
        field_name="worldY",
        default=DEFAULT_START_Y,
        minimum=-MAX_ABS_WORLD_COORDINATE,
        maximum=MAX_ABS_WORLD_COORDINATE,
    )
    world_z = _to_int(
        request.args.get("worldZ") or request.args.get("z"),
        field_name="worldZ",
        default=DEFAULT_START_Z,
        minimum=-MAX_ABS_WORLD_COORDINATE,
        maximum=MAX_ABS_WORLD_COORDINATE,
    )

    return world_x, world_y, world_z


def _positive_mod(value: int, modulo: int) -> int:
    """
    Positiver Modulo für lokale Koordinaten.

    Wichtig für negative Weltkoordinaten:
        worldX = -1, chunkSize = 16
        → localX = 15
    """
    return ((value % modulo) + modulo) % modulo


def _calculate_debug_coordinates(
    *,
    world_x: int,
    world_y: int,
    world_z: int,
    chunk_size: int,
) -> dict[str, Any]:
    """
    Berechnet Chunk- und lokale Zellkoordinaten.

    Diese Funktion ist für die Test-Route gedacht.
    Die endgültige produktive Koordinatenlogik sollte später in
    src/coordinates liegen.
    """
    if chunk_size <= 0:
        raise ValueError("chunkSize must be > 0.")

    chunk_x = world_x // chunk_size
    chunk_y = world_y // chunk_size
    chunk_z = world_z // chunk_size

    local_x = _positive_mod(world_x, chunk_size)
    local_y = _positive_mod(world_y, chunk_size)
    local_z = _positive_mod(world_z, chunk_size)

    return {
        "worldX": world_x,
        "worldY": world_y,
        "worldZ": world_z,
        "chunkX": chunk_x,
        "chunkY": chunk_y,
        "chunkZ": chunk_z,
        "localX": local_x,
        "localY": local_y,
        "localZ": local_z,
        "chunkKey": f"{chunk_x}:{chunk_y}:{chunk_z}",
        "chunkSize": chunk_size,
        "rule": {
            "chunkCoord": "Math.floor(worldCoord / chunkSize)",
            "localCoord": "((worldCoord % chunkSize) + chunkSize) % chunkSize",
            "negativeCoordinateExample": {
                "worldX": -1,
                "chunkSize": chunk_size,
                "chunkX": -1 // chunk_size,
                "localX": _positive_mod(-1, chunk_size),
            },
        },
    }


def _build_discovered_world_service(*, force_refresh: bool = False) -> WorldService:
    """
    Baut einen WorldService auf Basis der dynamisch entdeckten Weltmodelle.

    Diese Funktion verwendet bewusst nicht den Default-WorldService, damit
    neue Provider-Ordner ohne feste Registry-Anpassung in /world-test sichtbar
    werden.
    """
    registry = create_registry_from_discovered_worlds(
        default_world_id=DEFAULT_WORLD_ID,
        include_invalid=False,
        validate_config=True,
        use_cache=True,
        force_refresh=force_refresh,
        strict=False,
    )

    loader = create_default_world_loader(
        registry=registry,
        cache_enabled=True,
        verify_provider_contract=True,
    )

    return WorldService(
        loader=loader,
        max_batch_size=64,
    )


def _load_world_definition_for_route(world_id: str, *, force_refresh: bool = False) -> Any:
    """
    Lädt eine WorldDefinition über den dynamisch entdeckten WorldService.
    """
    service = _build_discovered_world_service(force_refresh=force_refresh)
    return service.get_world_definition(world_id, force_reload=force_refresh)


def _load_world_service_and_definition(world_id: str, *, force_refresh: bool = False) -> tuple[WorldService, Any]:
    """
    Lädt Service und WorldDefinition gemeinsam.
    """
    service = _build_discovered_world_service(force_refresh=force_refresh)
    definition = service.get_world_definition(world_id, force_reload=force_refresh)
    return service, definition


def _serialize_blocks_from_definition(definition: Any) -> dict[str, Any]:
    """
    Baut eine Blocklisten-/Palette-Antwort aus einer WorldDefinition.
    """
    blocks: list[dict[str, Any]] = []

    palette = getattr(definition, "palette", ()) or ()

    for index, entry in enumerate(palette):
        if hasattr(entry, "to_dict") and callable(entry.to_dict):
            item = entry.to_dict(camel_case=True, include_metadata=True)
        elif isinstance(entry, Mapping):
            item = dict(entry)
        else:
            item = {
                "value": _safe_str(entry),
            }

        item = {
            "paletteIndex": index,
            "cellValue": index + 1,
            **item,
        }
        blocks.append(item)

    return {
        "worldId": getattr(definition, "world_id", None),
        "worldType": getattr(definition, "world_type", None),
        "blockRegistryId": getattr(definition, "block_registry_id", None),
        "blockRegistryVersion": getattr(definition, "block_registry_version", None),
        "air": {
            "cellValue": 0,
            "blockTypeId": None,
            "label": "Air",
        },
        "encoding": {
            "airCellValue": 0,
            "blockCellValueRule": "paletteIndex + 1",
        },
        "blocks": blocks,
        "count": len(blocks),
    }


# ---------------------------------------------------------------------------
# HTML test page
# ---------------------------------------------------------------------------

def _build_world_test_html() -> str:
    """
    Gibt die vollständige HTML-Testseite zurück.

    Keine Templates, keine Static-Dateien, keine Build-Pipeline.
    """
    return r"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>VECTOPLAN World Test</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #111827;
      color: #e5e7eb;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      background: #111827;
      color: #e5e7eb;
    }

    header {
      padding: 16px 20px;
      border-bottom: 1px solid #374151;
      background: #0f172a;
      position: sticky;
      top: 0;
      z-index: 10;
    }

    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 700;
    }

    .subtitle {
      margin-top: 4px;
      color: #9ca3af;
      font-size: 13px;
    }

    main {
      display: grid;
      grid-template-columns: 320px minmax(360px, 1fr) minmax(420px, 1.3fr);
      gap: 12px;
      padding: 12px;
    }

    section {
      border: 1px solid #374151;
      background: #1f2937;
      border-radius: 10px;
      padding: 12px;
      min-width: 0;
    }

    h2 {
      margin: 0 0 10px;
      font-size: 15px;
      font-weight: 700;
    }

    h3 {
      margin: 14px 0 8px;
      font-size: 13px;
      color: #d1d5db;
    }

    .row {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }

    label {
      font-size: 12px;
      color: #9ca3af;
    }

    select,
    button,
    input {
      background: #111827;
      color: #e5e7eb;
      border: 1px solid #4b5563;
      border-radius: 8px;
      padding: 7px 9px;
      font: inherit;
      font-size: 13px;
    }

    button {
      cursor: pointer;
    }

    button:hover {
      background: #253044;
    }

    .world-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-top: 8px;
    }

    .world-card {
      border: 1px solid #374151;
      border-radius: 8px;
      padding: 8px;
      background: #111827;
      cursor: pointer;
    }

    .world-card.active {
      border-color: #93c5fd;
      background: #1e3a5f;
    }

    .world-title {
      font-size: 13px;
      font-weight: 700;
    }

    .world-meta {
      margin-top: 3px;
      color: #9ca3af;
      font-size: 12px;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 2px 7px;
      font-size: 11px;
      border: 1px solid #4b5563;
      color: #d1d5db;
    }

    .badge.ok {
      border-color: #22c55e;
      color: #86efac;
    }

    .badge.bad {
      border-color: #ef4444;
      color: #fca5a5;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
    }

    .metric {
      border: 1px solid #374151;
      border-radius: 8px;
      padding: 8px;
      background: #111827;
    }

    .metric-label {
      color: #9ca3af;
      font-size: 11px;
    }

    .metric-value {
      font-size: 18px;
      font-weight: 700;
      margin-top: 3px;
      overflow-wrap: anywhere;
    }

    .controls {
      display: grid;
      grid-template-columns: repeat(3, 44px);
      gap: 6px;
      justify-content: center;
      margin: 10px 0;
    }

    .controls button {
      min-height: 38px;
      font-weight: 700;
    }

    pre {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      max-height: 360px;
      overflow: auto;
      background: #020617;
      color: #d1d5db;
      border: 1px solid #374151;
      border-radius: 8px;
      padding: 10px;
      font-size: 12px;
      line-height: 1.45;
    }

    .tabs {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }

    .tab {
      font-size: 12px;
      padding: 6px 9px;
    }

    .tab.active {
      border-color: #93c5fd;
      color: #bfdbfe;
      background: #1e3a5f;
    }

    .hint {
      color: #9ca3af;
      font-size: 12px;
      line-height: 1.45;
    }

    .status {
      border: 1px solid #374151;
      background: #111827;
      border-radius: 8px;
      padding: 8px;
      font-size: 12px;
      color: #d1d5db;
      margin-bottom: 8px;
    }

    .status.error {
      border-color: #ef4444;
      color: #fecaca;
    }

    .status.ok {
      border-color: #22c55e;
      color: #bbf7d0;
    }

    @media (max-width: 1100px) {
      main {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
<header>
  <h1>VECTOPLAN World Test</h1>
  <div class="subtitle">
    Debug-Oberfläche für World Discovery, world.json, Palette, Koordinaten und generierte Chunk-Daten.
  </div>
</header>

<main>
  <section>
    <h2>Weltmodelle</h2>
    <div class="row">
      <button id="refreshBtn" type="button">Neu scannen</button>
      <button id="resetBtn" type="button">Position reset</button>
    </div>
    <div class="row">
      <label for="worldSelect">Aktive Welt</label>
      <select id="worldSelect"></select>
    </div>
    <div id="worldList" class="world-list"></div>
    <h3>Hinweis</h3>
    <div class="hint">
      Diese Seite lädt keine Datenbankdaten. Chunks werden direkt aus dem
      jeweiligen Provider generiert. Bewegung: WASD oder Pfeiltasten.
      Q/E bewegt Y.
    </div>
  </section>

  <section>
    <h2>Koordinaten-Test</h2>
    <div id="status" class="status">Initialisiere...</div>

    <h3>Position</h3>
    <div class="grid">
      <div class="metric"><div class="metric-label">worldX</div><div id="worldX" class="metric-value">0</div></div>
      <div class="metric"><div class="metric-label">worldY</div><div id="worldY" class="metric-value">1</div></div>
      <div class="metric"><div class="metric-label">worldZ</div><div id="worldZ" class="metric-value">0</div></div>
    </div>

    <h3>Chunk</h3>
    <div class="grid">
      <div class="metric"><div class="metric-label">chunkX</div><div id="chunkX" class="metric-value">0</div></div>
      <div class="metric"><div class="metric-label">chunkY</div><div id="chunkY" class="metric-value">0</div></div>
      <div class="metric"><div class="metric-label">chunkZ</div><div id="chunkZ" class="metric-value">0</div></div>
    </div>

    <h3>Local Cell</h3>
    <div class="grid">
      <div class="metric"><div class="metric-label">localX</div><div id="localX" class="metric-value">0</div></div>
      <div class="metric"><div class="metric-label">localY</div><div id="localY" class="metric-value">1</div></div>
      <div class="metric"><div class="metric-label">localZ</div><div id="localZ" class="metric-value">0</div></div>
    </div>

    <h3>Chunk-Key</h3>
    <div class="metric">
      <div class="metric-label">chunkKey</div>
      <div id="chunkKey" class="metric-value">0:0:0</div>
    </div>

    <h3>Steuerung</h3>
    <div class="controls">
      <span></span><button type="button" data-move="w">W</button><span></span>
      <button type="button" data-move="a">A</button><button type="button" data-move="s">S</button><button type="button" data-move="d">D</button>
      <button type="button" data-move="q">Q</button><span></span><button type="button" data-move="e">E</button>
    </div>

    <div class="hint">
      W/↑: Z - 1 · S/↓: Z + 1 · A/←: X - 1 · D/→: X + 1 · Q: Y - 1 · E: Y + 1
    </div>
  </section>

  <section>
    <h2>JSON</h2>
    <div class="tabs">
      <button class="tab active" type="button" data-tab="discovery">Discovery</button>
      <button class="tab" type="button" data-tab="world">World</button>
      <button class="tab" type="button" data-tab="blocks">Blocks</button>
      <button class="tab" type="button" data-tab="coords">Coords</button>
      <button class="tab" type="button" data-tab="chunk">Chunk</button>
    </div>
    <pre id="jsonView">{}</pre>
  </section>
</main>

<script>
(() => {
  const state = {
    worlds: [],
    selectedWorldId: "flat",
    worldMeta: null,
    blocks: null,
    chunk: null,
    coords: null,
    discovery: null,
    chunkSize: 16,
    position: {
      x: 0,
      y: 1,
      z: 0
    },
    activeTab: "discovery",
    lastChunkKey: null
  };

  const els = {
    status: document.getElementById("status"),
    worldSelect: document.getElementById("worldSelect"),
    worldList: document.getElementById("worldList"),
    refreshBtn: document.getElementById("refreshBtn"),
    resetBtn: document.getElementById("resetBtn"),
    jsonView: document.getElementById("jsonView"),
    worldX: document.getElementById("worldX"),
    worldY: document.getElementById("worldY"),
    worldZ: document.getElementById("worldZ"),
    chunkX: document.getElementById("chunkX"),
    chunkY: document.getElementById("chunkY"),
    chunkZ: document.getElementById("chunkZ"),
    localX: document.getElementById("localX"),
    localY: document.getElementById("localY"),
    localZ: document.getElementById("localZ"),
    chunkKey: document.getElementById("chunkKey")
  };

  function setStatus(message, type = "") {
    els.status.textContent = message;
    els.status.className = "status" + (type ? " " + type : "");
  }

  function pretty(value) {
    return JSON.stringify(value, null, 2);
  }

  async function fetchJson(url) {
    const response = await fetch(url, {
      headers: {
        "Accept": "application/json"
      }
    });

    const data = await response.json().catch(() => ({
      ok: false,
      error: {
        code: "invalid_json_response",
        message: "Response was not valid JSON."
      }
    }));

    if (!response.ok || data.ok === false) {
      const message = data?.error?.message || `Request failed: ${response.status}`;
      const error = new Error(message);
      error.payload = data;
      throw error;
    }

    return data;
  }

  function positiveMod(value, modulo) {
    return ((value % modulo) + modulo) % modulo;
  }

  function calculateCoords() {
    const size = state.chunkSize || 16;
    const x = state.position.x;
    const y = state.position.y;
    const z = state.position.z;

    const chunkX = Math.floor(x / size);
    const chunkY = Math.floor(y / size);
    const chunkZ = Math.floor(z / size);

    const localX = positiveMod(x, size);
    const localY = positiveMod(y, size);
    const localZ = positiveMod(z, size);

    return {
      worldX: x,
      worldY: y,
      worldZ: z,
      chunkX,
      chunkY,
      chunkZ,
      localX,
      localY,
      localZ,
      chunkKey: `${chunkX}:${chunkY}:${chunkZ}`,
      chunkSize: size,
      rule: {
        chunkCoord: "Math.floor(worldCoord / chunkSize)",
        localCoord: "((worldCoord % chunkSize) + chunkSize) % chunkSize"
      }
    };
  }

  function renderCoords() {
    const coords = calculateCoords();
    state.coords = coords;

    els.worldX.textContent = coords.worldX;
    els.worldY.textContent = coords.worldY;
    els.worldZ.textContent = coords.worldZ;

    els.chunkX.textContent = coords.chunkX;
    els.chunkY.textContent = coords.chunkY;
    els.chunkZ.textContent = coords.chunkZ;

    els.localX.textContent = coords.localX;
    els.localY.textContent = coords.localY;
    els.localZ.textContent = coords.localZ;

    els.chunkKey.textContent = coords.chunkKey;

    return coords;
  }

  function renderJson() {
    let value;

    if (state.activeTab === "discovery") value = state.discovery;
    else if (state.activeTab === "world") value = state.worldMeta;
    else if (state.activeTab === "blocks") value = state.blocks;
    else if (state.activeTab === "coords") value = state.coords;
    else if (state.activeTab === "chunk") value = state.chunk;
    else value = {};

    els.jsonView.textContent = pretty(value || {});
  }

  function renderWorldList() {
    els.worldSelect.innerHTML = "";

    for (const world of state.worlds) {
      const id = world.providerId || world.worldId || world.folderName;
      const option = document.createElement("option");
      option.value = id;
      option.textContent = `${id} ${world.valid ? "✅" : "❌"}`;
      els.worldSelect.appendChild(option);
    }

    if (state.selectedWorldId) {
      els.worldSelect.value = state.selectedWorldId;
    }

    els.worldList.innerHTML = "";

    for (const world of state.worlds) {
      const id = world.providerId || world.worldId || world.folderName;
      const card = document.createElement("div");
      card.className = "world-card" + (id === state.selectedWorldId ? " active" : "");
      card.tabIndex = 0;
      card.onclick = () => selectWorld(id);

      const title = document.createElement("div");
      title.className = "world-title";
      title.textContent = id || "(unknown)";

      const meta = document.createElement("div");
      meta.className = "world-meta";
      meta.textContent = `${world.worldType || "unknown"} · ${world.providerModule || ""}`;

      const badge = document.createElement("span");
      badge.className = "badge " + (world.valid ? "ok" : "bad");
      badge.textContent = world.valid ? "valid" : "invalid";

      card.appendChild(title);
      card.appendChild(meta);
      card.appendChild(badge);
      els.worldList.appendChild(card);
    }
  }

  function setActiveTab(tab) {
    state.activeTab = tab;

    for (const button of document.querySelectorAll(".tab")) {
      button.classList.toggle("active", button.dataset.tab === tab);
    }

    renderJson();
  }

  async function loadWorlds(forceRefresh = false) {
    setStatus("Scanne Weltmodelle...");

    const url = `/world-test/api/worlds?includeInvalid=true&includeRawConfig=false${forceRefresh ? "&forceRefresh=true" : ""}`;
    const data = await fetchJson(url);

    state.discovery = data;
    state.worlds = data?.discovery?.providers || data?.providers || [];

    const validWorld = state.worlds.find((item) => item.valid);
    const preferred = state.worlds.find((item) => item.providerId === state.selectedWorldId || item.worldId === state.selectedWorldId);

    if (preferred && preferred.valid) {
      state.selectedWorldId = preferred.providerId || preferred.worldId || preferred.folderName;
    } else if (validWorld) {
      state.selectedWorldId = validWorld.providerId || validWorld.worldId || validWorld.folderName;
    }

    renderWorldList();

    if (state.selectedWorldId) {
      await loadSelectedWorld();
    }

    setStatus("Weltmodelle geladen.", "ok");
  }

  async function loadSelectedWorld() {
    if (!state.selectedWorldId) {
      setStatus("Keine gültige Welt ausgewählt.", "error");
      return;
    }

    setStatus(`Lade Welt ${state.selectedWorldId}...`);

    const worldData = await fetchJson(`/world-test/api/worlds/${encodeURIComponent(state.selectedWorldId)}`);
    state.worldMeta = worldData;

    const worldPayload = worldData.world || {};
    state.chunkSize = Number(worldPayload.chunkSize || 16);

    const blocksData = await fetchJson(`/world-test/api/worlds/${encodeURIComponent(state.selectedWorldId)}/blocks`);
    state.blocks = blocksData;

    renderCoords();
    await loadCurrentChunk(true);

    renderWorldList();
    renderJson();

    setStatus(`Welt ${state.selectedWorldId} geladen.`, "ok");
  }

  async function loadCurrentChunk(force = false) {
    const coords = renderCoords();

    if (!force && state.lastChunkKey === coords.chunkKey) {
      renderJson();
      return;
    }

    state.lastChunkKey = coords.chunkKey;

    const url =
      `/world-test/api/worlds/${encodeURIComponent(state.selectedWorldId)}/chunks` +
      `?chunkX=${coords.chunkX}&chunkY=${coords.chunkY}&chunkZ=${coords.chunkZ}`;

    const data = await fetchJson(url);
    state.chunk = data;
    renderJson();
  }

  async function selectWorld(worldId) {
    state.selectedWorldId = worldId;
    state.lastChunkKey = null;
    els.worldSelect.value = worldId;
    await loadSelectedWorld();
  }

  async function move(dx, dy, dz) {
    state.position.x += dx;
    state.position.y += dy;
    state.position.z += dz;
    renderCoords();
    await loadCurrentChunk(false);
  }

  async function handleMoveCommand(command) {
    if (command === "w") await move(0, 0, -1);
    else if (command === "s") await move(0, 0, 1);
    else if (command === "a") await move(-1, 0, 0);
    else if (command === "d") await move(1, 0, 0);
    else if (command === "q") await move(0, -1, 0);
    else if (command === "e") await move(0, 1, 0);
  }

  function resetPosition() {
    state.position = { x: 0, y: 1, z: 0 };
    state.lastChunkKey = null;
    renderCoords();
    loadCurrentChunk(true).catch((error) => {
      setStatus(error.message, "error");
      if (error.payload) {
        state.chunk = error.payload;
        renderJson();
      }
    });
  }

  function bindEvents() {
    els.refreshBtn.addEventListener("click", () => {
      loadWorlds(true).catch((error) => {
        setStatus(error.message, "error");
        if (error.payload) {
          state.discovery = error.payload;
          renderJson();
        }
      });
    });

    els.resetBtn.addEventListener("click", resetPosition);

    els.worldSelect.addEventListener("change", () => {
      selectWorld(els.worldSelect.value).catch((error) => {
        setStatus(error.message, "error");
      });
    });

    for (const button of document.querySelectorAll("[data-move]")) {
      button.addEventListener("click", () => {
        handleMoveCommand(button.dataset.move).catch((error) => {
          setStatus(error.message, "error");
        });
      });
    }

    for (const button of document.querySelectorAll(".tab")) {
      button.addEventListener("click", () => setActiveTab(button.dataset.tab));
    }

    window.addEventListener("keydown", (event) => {
      const key = event.key.toLowerCase();

      let command = null;

      if (key === "w" || event.key === "ArrowUp") command = "w";
      else if (key === "s" || event.key === "ArrowDown") command = "s";
      else if (key === "a" || event.key === "ArrowLeft") command = "a";
      else if (key === "d" || event.key === "ArrowRight") command = "d";
      else if (key === "q") command = "q";
      else if (key === "e") command = "e";
      else if (key === "r") {
        resetPosition();
        return;
      }

      if (command) {
        event.preventDefault();
        handleMoveCommand(command).catch((error) => {
          setStatus(error.message, "error");
        });
      }
    });
  }

  async function boot() {
    bindEvents();
    renderCoords();
    setActiveTab("discovery");

    try {
      await loadWorlds(false);
    } catch (error) {
      setStatus(error.message, "error");
      if (error.payload) {
        state.discovery = error.payload;
        renderJson();
      }
    }
  }

  boot();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@world_test_bp.get("/")
def world_test_index() -> Response:
    """
    Zeigt die interaktive World-Test-Seite.

    Der Slash-Endpunkt ist wegen url_prefix also:

        GET /world-test/
    """
    html = _build_world_test_html()
    return Response(
        html,
        status=200,
        mimetype="text/html; charset=utf-8",
    )


@world_test_bp.get("")
def world_test_index_without_slash() -> Response:
    """
    Zeigt dieselbe Seite ohne trailing slash:

        GET /world-test
    """
    return world_test_index()


# ---------------------------------------------------------------------------
# Debug API routes
# ---------------------------------------------------------------------------

@world_test_bp.get("/api/health")
def world_test_health() -> Any:
    """
    Kleine Health-/Diagnose-Antwort für die Test-Route.
    """
    try:
        return _ok(
            {
                "service": "vectoplan-chunk",
                "debugRoute": "world-test",
                "routeVersion": WORLD_TEST_ROUTE_VERSION,
                "endpoints": {
                    "page": "/world-test",
                    "worlds": "/world-test/api/worlds",
                    "world": "/world-test/api/worlds/<world_id>",
                    "blocks": "/world-test/api/worlds/<world_id>/blocks",
                    "chunk": "/world-test/api/worlds/<world_id>/chunks?chunkX=0&chunkY=0&chunkZ=0",
                    "coords": "/world-test/api/coords?worldId=flat&x=-1&y=0&z=0",
                },
            }
        )
    except Exception as exc:
        return _error(exc)


@world_test_bp.get("/api/worlds")
def world_test_list_worlds() -> Any:
    """
    Scannt alle Weltmodelle unter src/world.

    Query:
        includeInvalid=true|false
        includeRawConfig=true|false
        forceRefresh=true|false
    """
    try:
        include_invalid = _get_include_invalid(default=True)
        include_raw_config = _get_include_raw_config(default=False)
        force_refresh = _get_force_refresh()

        if force_refresh:
            reset_world_discovery_cache()

        discovery = discover_worlds_as_dict(
            include_invalid=include_invalid,
            include_raw_config=include_raw_config,
            include_candidates=True,
            validate_config=True,
            use_cache=True,
            force_refresh=force_refresh,
        )

        return _ok(
            {
                "discovery": discovery,
                "providers": discovery.get("providers", []),
                "defaultWorldId": discovery.get("defaultWorldId"),
            }
        )

    except Exception as exc:
        return _error(exc)


@world_test_bp.get("/api/worlds/<world_id>")
def world_test_get_world(world_id: str) -> Any:
    """
    Lädt Welt-Metadaten für ein bestimmtes Weltmodell.

    Beispiel:
        GET /world-test/api/worlds/flat
    """
    try:
        force_refresh = _get_force_refresh()
        resolved_world_id = _safe_str(world_id, default=DEFAULT_WORLD_ID)

        definition = _load_world_definition_for_route(
            resolved_world_id,
            force_refresh=force_refresh,
        )

        response = serialize_world_metadata_response(
            definition,
            include_palette=True,
            include_metadata=True,
            include_raw_config=_get_include_raw_config(default=False),
        )
        response["routeVersion"] = WORLD_TEST_ROUTE_VERSION

        return _json_response(response)

    except Exception as exc:
        return _error(exc)


@world_test_bp.get("/api/worlds/<world_id>/blocks")
def world_test_get_blocks(world_id: str) -> Any:
    """
    Gibt die Blockliste / Palette eines Weltmodells zurück.

    Beispiel:
        GET /world-test/api/worlds/flat/blocks
    """
    try:
        force_refresh = _get_force_refresh()
        resolved_world_id = _safe_str(world_id, default=DEFAULT_WORLD_ID)

        definition = _load_world_definition_for_route(
            resolved_world_id,
            force_refresh=force_refresh,
        )

        blocks_payload = _serialize_blocks_from_definition(definition)

        return _ok(
            {
                "worldId": resolved_world_id,
                "blocks": blocks_payload,
            }
        )

    except Exception as exc:
        return _error(exc)


@world_test_bp.get("/api/worlds/<world_id>/chunks")
def world_test_get_chunk(world_id: str) -> Any:
    """
    Generiert einen einzelnen Chunk für ein Weltmodell.

    Beispiel:
        GET /world-test/api/worlds/flat/chunks?chunkX=0&chunkY=0&chunkZ=0
    """
    try:
        force_refresh = _get_force_refresh()
        resolved_world_id = _safe_str(world_id, default=DEFAULT_WORLD_ID)
        chunk_x, chunk_y, chunk_z = _get_chunk_coords_from_query()

        service, definition = _load_world_service_and_definition(
            resolved_world_id,
            force_refresh=force_refresh,
        )

        chunk = service.generate_chunk(
            getattr(definition, "world_id", resolved_world_id),
            chunk_x,
            chunk_y,
            chunk_z,
            metadata={
                "source": "world_test_route",
                "routeVersion": WORLD_TEST_ROUTE_VERSION,
            },
            force_reload_world=force_refresh,
        )

        response = serialize_chunk_response(
            chunk,
            include_palette=True,
            include_cells=True,
            include_metadata=True,
            include_cell_encoding=True,
        )
        response["routeVersion"] = WORLD_TEST_ROUTE_VERSION

        return _json_response(response)

    except Exception as exc:
        return _error(exc)


@world_test_bp.get("/api/chunks")
def world_test_get_chunk_by_query() -> Any:
    """
    Alternative Chunk-Test-Route mit worldId als Query-Parameter.

    Beispiel:
        GET /world-test/api/chunks?worldId=flat&chunkX=0&chunkY=0&chunkZ=0
    """
    try:
        world_id = _get_world_id(default=DEFAULT_WORLD_ID)
        chunk_x, chunk_y, chunk_z = _get_chunk_coords_from_query()
        force_refresh = _get_force_refresh()

        service, definition = _load_world_service_and_definition(
            world_id,
            force_refresh=force_refresh,
        )

        chunk = service.generate_chunk(
            getattr(definition, "world_id", world_id),
            chunk_x,
            chunk_y,
            chunk_z,
            metadata={
                "source": "world_test_route",
                "routeVersion": WORLD_TEST_ROUTE_VERSION,
            },
            force_reload_world=force_refresh,
        )

        response = serialize_chunk_response(
            chunk,
            include_palette=True,
            include_cells=True,
            include_metadata=True,
            include_cell_encoding=True,
        )
        response["routeVersion"] = WORLD_TEST_ROUTE_VERSION

        return _json_response(response)

    except Exception as exc:
        return _error(exc)


@world_test_bp.get("/api/coords")
def world_test_get_coords() -> Any:
    """
    Debug-Endpunkt zur Koordinatenberechnung.

    Beispiel:
        GET /world-test/api/coords?worldId=flat&x=-1&y=0&z=0

    Wichtig:
    Diese Funktion ist ein Testhelfer.
    Die produktive Koordinatenlogik sollte später nach src/coordinates.
    """
    try:
        force_refresh = _get_force_refresh()
        world_id = _get_world_id(default=DEFAULT_WORLD_ID)
        world_x, world_y, world_z = _get_world_coords_from_query()

        definition = _load_world_definition_for_route(
            world_id,
            force_refresh=force_refresh,
        )

        chunk_size = int(getattr(definition, "chunk_size", 16) or 16)

        coords = _calculate_debug_coordinates(
            world_x=world_x,
            world_y=world_y,
            world_z=world_z,
            chunk_size=chunk_size,
        )

        return _ok(
            {
                "worldId": getattr(definition, "world_id", world_id),
                "coords": coords,
            }
        )

    except Exception as exc:
        return _error(exc)


@world_test_bp.get("/api/worlds/<world_id>/raw")
def world_test_get_raw_discovered_world(world_id: str) -> Any:
    """
    Gibt das Discovery-Ergebnis eines bestimmten Providers zurück.

    Beispiel:
        GET /world-test/api/worlds/flat/raw?includeRawConfig=true
    """
    try:
        force_refresh = _get_force_refresh()
        include_raw_config = _get_include_raw_config(default=True)
        resolved_world_id = _safe_str(world_id, default=DEFAULT_WORLD_ID)

        provider = get_discovered_world(
            resolved_world_id,
            validate_config=True,
            use_cache=True,
            force_refresh=force_refresh,
        )

        return _ok(
            {
                "provider": provider.to_dict(
                    include_raw_config=include_raw_config,
                    include_metadata=True,
                )
            }
        )

    except Exception as exc:
        return _error(exc)


__all__ = (
    "WORLD_TEST_ROUTE_VERSION",
    "world_test_bp",
)