# services/vectoplan-editor/routes/editor.py
"""
Erste Benutzerroute für den Service `vectoplan-editor`.

Ziel dieser Datei in der ersten Minimalstufe:
- genau eine sichtbare Route bereitstellen: GET /editor
- eine Editor-Shell im Browser ausliefern
- die Shell robust aus Konfigurationswerten aufbauen
- bei Template-Problemen kontrolliert auf eine Inline-Fallback-Shell zurückfallen

Wichtig:
- keine Business-Logik
- keine Core-/Library-Integration
- keine 3D-Fachlogik
- nur HTTP-Adapter + Template-Auslieferung + defensive Fallbacks

Robustheitsprinzipien:
- defensive Auswertung von App-Konfiguration
- gecachte Hilfsfunktionen für stabile Defaults
- kontrolliertes Logging
- Fallback-HTML statt harter Leerseite, wenn Templates noch fehlen oder kaputt sind
"""

from __future__ import annotations

import json
from functools import lru_cache
from html import escape
from http import HTTPStatus
from typing import Any, Iterable

from flask import Blueprint, Response, current_app, make_response, render_template, url_for
from jinja2 import TemplateNotFound


# -----------------------------------------------------------------------------
# Blueprint-Konstanten
# -----------------------------------------------------------------------------

EDITOR_BLUEPRINT_NAME = "editor"
EDITOR_ROUTE_PATH = "/editor"

editor_bp = Blueprint(EDITOR_BLUEPRINT_NAME, __name__)


# -----------------------------------------------------------------------------
# Harte, robuste Defaults für die erste Minimalstufe
# -----------------------------------------------------------------------------

DEFAULT_EDITOR_TEMPLATE_NAME = "editor/index.html"

DEFAULT_PAGE_TITLE = "VECTOPLAN Editor"
DEFAULT_BRAND_NAME = "VECTOPLAN Editor"

DEFAULT_INITIAL_STATUS = "Initialisierung..."
DEFAULT_RUNTIME_READY_STATUS = "Editor Runtime gestartet"
DEFAULT_VIEWPORT_PLACEHOLDER = "3D-Viewport wird hier aufgebaut"

DEFAULT_LEFT_PANEL_TITLE = "Werkzeuge"
DEFAULT_LEFT_PANEL_TEXT = "Platzhalter für Tools"

DEFAULT_RIGHT_PANEL_TITLE = "Inspector"
DEFAULT_RIGHT_PANEL_TEXT = "Platzhalter für Eigenschaften"

DEFAULT_EDITOR_CSS_FILE = "editor/css/editor.css"
DEFAULT_EDITOR_JS_FILE = "editor/js/main.js"

DEFAULT_HOTBAR_SLOT_COUNT = 5
MIN_HOTBAR_SLOT_COUNT = 1
MAX_HOTBAR_SLOT_COUNT = 20


# -----------------------------------------------------------------------------
# Kleine defensive Hilfsfunktionen
# -----------------------------------------------------------------------------

def _safe_log_debug(message: str) -> None:
    """
    Loggt defensiv auf Debug-Level.
    """
    try:
        current_app.logger.debug(message)
    except Exception:
        pass


def _safe_log_warning(message: str) -> None:
    """
    Loggt defensiv auf Warning-Level.
    """
    try:
        current_app.logger.warning(message)
    except Exception:
        pass


def _safe_log_exception(message: str) -> None:
    """
    Loggt defensiv eine Exception aus einem aktiven Ausnahme-Kontext.
    """
    try:
        current_app.logger.exception(message)
    except Exception:
        pass


def _safe_get_config_value(key: str, default: Any) -> Any:
    """
    Liest einen Konfigurationswert defensiv aus `current_app.config`.

    Falls kein App-Kontext oder ein anderer Fehler vorliegt, wird auf den
    Default zurückgefallen.
    """
    try:
        return current_app.config.get(key, default)
    except Exception:
        return default


def _coerce_text(value: Any, default: str) -> str:
    """
    Normalisiert einen Wert robust zu einem nicht-leeren String.
    """
    if value is None:
        return default

    if isinstance(value, str):
        normalized = value.strip()
        return normalized or default

    try:
        normalized = str(value).strip()
        return normalized or default
    except Exception:
        return default


def _coerce_int(
    value: Any,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """
    Wandelt einen Wert robust in einen Integer um und begrenzt ihn optional.
    """
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default

    if minimum is not None:
        result = max(minimum, result)

    if maximum is not None:
        result = min(maximum, result)

    return result


def _normalize_iterable_labels(value: Any) -> tuple[str, ...]:
    """
    Versucht, explizite Slot-Labels aus einer Iterable robust zu normalisieren.

    Nur nicht-leere String-Repräsentationen werden übernommen.
    """
    if value is None:
        return ()

    if isinstance(value, (str, bytes)):
        return ()

    if not isinstance(value, Iterable):
        return ()

    labels: list[str] = []

    try:
        for item in value:
            text = _coerce_text(item, "").strip()
            if text:
                labels.append(text)
    except Exception:
        return ()

    if not labels:
        return ()

    return tuple(labels[:MAX_HOTBAR_SLOT_COUNT])


@lru_cache(maxsize=32)
def _build_default_hotbar_labels(slot_count: int) -> tuple[str, ...]:
    """
    Baut gecacht Standardlabels für die Hotbar.

    Warum Cache?
    - diese Werte sind pro Slot-Zahl stabil
    - Tests oder wiederholte Requests müssen die Liste nicht jedes Mal neu bauen
    - klein, deterministisch und robust
    """
    safe_slot_count = _coerce_int(
        slot_count,
        default=DEFAULT_HOTBAR_SLOT_COUNT,
        minimum=MIN_HOTBAR_SLOT_COUNT,
        maximum=MAX_HOTBAR_SLOT_COUNT,
    )

    return tuple(str(index) for index in range(1, safe_slot_count + 1))


def _resolve_hotbar_labels() -> tuple[str, ...]:
    """
    Ermittelt die Hotbar-Labels aus der Konfiguration.

    Unterstützte Formen:
    - explizite Liste/Tuple unter `EDITOR_HOTBAR_SLOT_LABELS`
    - Anzahl unter `EDITOR_HOTBAR_SLOTS`
    - robuster Fallback auf 1..5
    """
    explicit_labels = _safe_get_config_value("EDITOR_HOTBAR_SLOT_LABELS", None)
    normalized_explicit_labels = _normalize_iterable_labels(explicit_labels)
    if normalized_explicit_labels:
        return normalized_explicit_labels

    slot_count = _safe_get_config_value("EDITOR_HOTBAR_SLOTS", DEFAULT_HOTBAR_SLOT_COUNT)
    safe_slot_count = _coerce_int(
        slot_count,
        default=DEFAULT_HOTBAR_SLOT_COUNT,
        minimum=MIN_HOTBAR_SLOT_COUNT,
        maximum=MAX_HOTBAR_SLOT_COUNT,
    )

    return _build_default_hotbar_labels(safe_slot_count)


def _safe_static_url(filename: str) -> str:
    """
    Baut robust eine Static-URL.

    Falls `url_for()` fehlschlägt, wird auf einen einfachen Pfad-Fallback
    zurückgegriffen.
    """
    clean_filename = _coerce_text(filename, "").lstrip("/")
    if not clean_filename:
        return "/static/"

    try:
        return url_for("static", filename=clean_filename)
    except Exception:
        return f"/static/{clean_filename}"


def _build_editor_context() -> dict[str, Any]:
    """
    Baut den vollständigen Template-Kontext für die Editor-Shell.

    Diese Funktion bündelt alle Konfigurationszugriffe an einer Stelle.
    """
    page_title = _coerce_text(
        _safe_get_config_value("EDITOR_PAGE_TITLE", DEFAULT_PAGE_TITLE),
        DEFAULT_PAGE_TITLE,
    )
    brand_name = _coerce_text(
        _safe_get_config_value("EDITOR_BRAND_NAME", DEFAULT_BRAND_NAME),
        DEFAULT_BRAND_NAME,
    )
    initial_status = _coerce_text(
        _safe_get_config_value("EDITOR_STATUS_INITIAL", DEFAULT_INITIAL_STATUS),
        DEFAULT_INITIAL_STATUS,
    )
    runtime_ready_status = _coerce_text(
        _safe_get_config_value("EDITOR_STATUS_READY", DEFAULT_RUNTIME_READY_STATUS),
        DEFAULT_RUNTIME_READY_STATUS,
    )
    viewport_placeholder = _coerce_text(
        _safe_get_config_value("EDITOR_VIEWPORT_PLACEHOLDER", DEFAULT_VIEWPORT_PLACEHOLDER),
        DEFAULT_VIEWPORT_PLACEHOLDER,
    )
    left_panel_title = _coerce_text(
        _safe_get_config_value("EDITOR_LEFT_PANEL_TITLE", DEFAULT_LEFT_PANEL_TITLE),
        DEFAULT_LEFT_PANEL_TITLE,
    )
    left_panel_text = _coerce_text(
        _safe_get_config_value("EDITOR_LEFT_PANEL_TEXT", DEFAULT_LEFT_PANEL_TEXT),
        DEFAULT_LEFT_PANEL_TEXT,
    )
    right_panel_title = _coerce_text(
        _safe_get_config_value("EDITOR_RIGHT_PANEL_TITLE", DEFAULT_RIGHT_PANEL_TITLE),
        DEFAULT_RIGHT_PANEL_TITLE,
    )
    right_panel_text = _coerce_text(
        _safe_get_config_value("EDITOR_RIGHT_PANEL_TEXT", DEFAULT_RIGHT_PANEL_TEXT),
        DEFAULT_RIGHT_PANEL_TEXT,
    )

    editor_css_file = _coerce_text(
        _safe_get_config_value("EDITOR_MAIN_CSS_FILE", DEFAULT_EDITOR_CSS_FILE),
        DEFAULT_EDITOR_CSS_FILE,
    )
    editor_js_file = _coerce_text(
        _safe_get_config_value("EDITOR_MAIN_JS_FILE", DEFAULT_EDITOR_JS_FILE),
        DEFAULT_EDITOR_JS_FILE,
    )

    hotbar_slots = _resolve_hotbar_labels()

    context = {
        "page_title": page_title,
        "brand_name": brand_name,
        "initial_status": initial_status,
        "runtime_ready_status": runtime_ready_status,
        "viewport_placeholder": viewport_placeholder,
        "left_panel_title": left_panel_title,
        "left_panel_text": left_panel_text,
        "right_panel_title": right_panel_title,
        "right_panel_text": right_panel_text,
        "hotbar_slots": hotbar_slots,
        "editor_css_file": editor_css_file,
        "editor_js_file": editor_js_file,
        "editor_css_url": _safe_static_url(editor_css_file),
        "editor_js_url": _safe_static_url(editor_js_file),
        "editor_route_path": EDITOR_ROUTE_PATH,
    }

    return context


def _resolve_template_name() -> str:
    """
    Ermittelt den Template-Namen robust aus der Konfiguration.
    """
    return _coerce_text(
        _safe_get_config_value("EDITOR_TEMPLATE_NAME", DEFAULT_EDITOR_TEMPLATE_NAME),
        DEFAULT_EDITOR_TEMPLATE_NAME,
    )


def _build_html_response(
    html: str,
    status_code: int = HTTPStatus.OK,
    fallback_reason: str | None = None,
) -> Response:
    """
    Baut eine robuste HTML-Response mit konservativen Headern.

    Für die erste Editor-Shell ist bewusst `no-store` gesetzt, damit Browser
    Änderungen an Template/CSS/JS in der frühen Entwicklungsphase nicht zu
    aggressiv zwischenspeichern.
    """
    response = make_response(html, int(status_code))
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-VECTOPLAN-Editor-Route"] = EDITOR_ROUTE_PATH

    if fallback_reason:
        response.headers["X-VECTOPLAN-Editor-Fallback"] = fallback_reason

    return response


@lru_cache(maxsize=1)
def _fallback_shell_css() -> str:
    """
    Liefert gecacht ein minimales, aber voll funktionsfähiges CSS für die
    Fallback-Shell.

    Warum Cache?
    - die CSS-Zeichenkette ist unveränderlich
    - wiederholte Fallback-Responses vermeiden unnötigen Neuaufbau
    """
    return """
html, body {
  margin: 0;
  padding: 0;
  height: 100%;
  font-family: Arial, sans-serif;
  background: #111;
  color: #eee;
}

* {
  box-sizing: border-box;
}

#editor-app {
  display: grid;
  grid-template-columns: 260px 1fr 320px;
  grid-template-rows: 56px 1fr 88px;
  grid-template-areas:
    "topbar topbar topbar"
    "left viewport right"
    "hotbar hotbar hotbar";
  height: 100vh;
  background: #111;
  color: #eee;
}

.topbar {
  grid-area: topbar;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 16px;
  background: #1b1b1b;
  border-bottom: 1px solid #2b2b2b;
}

.brand {
  font-weight: 700;
  letter-spacing: 0.02em;
}

.status {
  color: #bdbdbd;
  font-size: 14px;
}

.left-panel,
.right-panel {
  padding: 16px;
  background: #161616;
  overflow: auto;
}

.left-panel {
  grid-area: left;
  border-right: 1px solid #2b2b2b;
}

.right-panel {
  grid-area: right;
  border-left: 1px solid #2b2b2b;
}

.panel-title {
  margin: 0 0 12px 0;
  font-size: 18px;
}

.panel-text {
  margin: 0;
  color: #b8b8b8;
  line-height: 1.5;
}

.viewport-container {
  grid-area: viewport;
  position: relative;
  overflow: hidden;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.02), rgba(0,0,0,0.05)),
    #0b0b0b;
}

#editor-viewport {
  width: 100%;
  height: 100%;
  position: relative;
}

.viewport-info {
  position: absolute;
  top: 16px;
  left: 16px;
  color: #aaa;
  background: rgba(0,0,0,0.25);
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.06);
}

.fallback-badge {
  position: absolute;
  right: 16px;
  bottom: 16px;
  font-size: 12px;
  color: #cfcfcf;
  background: rgba(255,255,255,0.05);
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid rgba(255,255,255,0.07);
}

.hotbar {
  grid-area: hotbar;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 0 16px;
  background: #1b1b1b;
  border-top: 1px solid #2b2b2b;
}

.slot {
  width: 56px;
  height: 56px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #262626;
  border: 1px solid #3a3a3a;
  border-radius: 8px;
  color: #f1f1f1;
  user-select: none;
}

.meta-note {
  margin-top: 16px;
  font-size: 12px;
  color: #9b9b9b;
  line-height: 1.45;
}
""".strip()


def _build_fallback_html(context: dict[str, Any], reason: str) -> str:
    """
    Baut eine vollständige Fallback-HTML-Seite, falls das normale Template
    nicht gerendert werden kann.

    Die Seite bleibt bewusst nah an der geplanten Editor-Shell:
    - Topbar
    - linker Bereich
    - Viewport
    - rechter Bereich
    - Hotbar
    """
    page_title = escape(_coerce_text(context.get("page_title"), DEFAULT_PAGE_TITLE))
    brand_name = escape(_coerce_text(context.get("brand_name"), DEFAULT_BRAND_NAME))
    initial_status = escape(_coerce_text(context.get("initial_status"), DEFAULT_INITIAL_STATUS))
    runtime_ready_status = _coerce_text(
        context.get("runtime_ready_status"),
        DEFAULT_RUNTIME_READY_STATUS,
    )
    viewport_placeholder = _coerce_text(
        context.get("viewport_placeholder"),
        DEFAULT_VIEWPORT_PLACEHOLDER,
    )

    left_panel_title = escape(_coerce_text(context.get("left_panel_title"), DEFAULT_LEFT_PANEL_TITLE))
    left_panel_text = escape(_coerce_text(context.get("left_panel_text"), DEFAULT_LEFT_PANEL_TEXT))
    right_panel_title = escape(_coerce_text(context.get("right_panel_title"), DEFAULT_RIGHT_PANEL_TITLE))
    right_panel_text = escape(_coerce_text(context.get("right_panel_text"), DEFAULT_RIGHT_PANEL_TEXT))

    editor_css_url = escape(_coerce_text(context.get("editor_css_url"), "/static/editor/css/editor.css"))
    editor_js_url = escape(_coerce_text(context.get("editor_js_url"), "/static/editor/js/main.js"))

    hotbar_slots_raw = context.get("hotbar_slots", _build_default_hotbar_labels(DEFAULT_HOTBAR_SLOT_COUNT))
    hotbar_slots = _normalize_iterable_labels(hotbar_slots_raw) or _build_default_hotbar_labels(
        DEFAULT_HOTBAR_SLOT_COUNT
    )

    hotbar_markup = "\n".join(
        f'        <div class="slot">{escape(slot)}</div>' for slot in hotbar_slots
    )

    runtime_ready_status_json = json.dumps(runtime_ready_status, ensure_ascii=False)
    viewport_placeholder_json = json.dumps(viewport_placeholder, ensure_ascii=False)
    fallback_reason_json = json.dumps(reason, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="de">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{page_title}</title>
    <link rel="stylesheet" href="{editor_css_url}" />
    <style>
{_fallback_shell_css()}
    </style>
  </head>
  <body>
    <div id="editor-app">
      <header class="topbar">
        <div class="brand">{brand_name}</div>
        <div class="status">{initial_status}</div>
      </header>

      <aside class="left-panel">
        <h2 class="panel-title">{left_panel_title}</h2>
        <p class="panel-text">{left_panel_text}</p>
        <p class="meta-note">
          Fallback-Shell aktiv. Diese Ansicht wird direkt aus der Route erzeugt,
          weil das reguläre Template nicht gerendert werden konnte.
        </p>
      </aside>

      <main class="viewport-container">
        <div id="editor-viewport">
          <div class="viewport-info"></div>
          <div class="fallback-badge">Fallback-Modus</div>
        </div>
      </main>

      <aside class="right-panel">
        <h2 class="panel-title">{right_panel_title}</h2>
        <p class="panel-text">{right_panel_text}</p>
        <p class="meta-note">Grund: {escape(reason)}</p>
      </aside>

      <footer class="hotbar">
{hotbar_markup}
      </footer>
    </div>

    <script>
      (function () {{
        try {{
          var statusElement = document.querySelector(".status");
          if (statusElement) {{
            statusElement.textContent = {runtime_ready_status_json};
          }}

          var viewport = document.getElementById("editor-viewport");
          if (viewport) {{
            var info = viewport.querySelector(".viewport-info");
            if (!info) {{
              info = document.createElement("div");
              info.className = "viewport-info";
              viewport.appendChild(info);
            }}
            info.textContent = {viewport_placeholder_json};
          }}

          window.__VECTOPLAN_EDITOR_FALLBACK__ = {{
            active: true,
            reason: {fallback_reason_json}
          }};
        }} catch (error) {{
          try {{
            console.error("VECTOPLAN Editor fallback runtime failed.", error);
          }} catch (consoleError) {{
            // bewusst still
          }}
        }}
      }})();
    </script>
    <script src="{editor_js_url}"></script>
  </body>
</html>
"""


def _render_editor_shell() -> str:
    """
    Rendert die reguläre Editor-Shell über das konfigurierte Template.

    Bei Template-Problemen wird eine Exception nach oben gegeben, damit der
    Aufrufer entscheiden kann, ob ein Fallback ausgeliefert wird.
    """
    template_name = _resolve_template_name()
    context = _build_editor_context()

    _safe_log_debug(
        f"Versuche Editor-Template zu rendern: template={template_name!r}, route={EDITOR_ROUTE_PATH!r}"
    )

    return render_template(template_name, **context)


# -----------------------------------------------------------------------------
# Öffentliche Route
# -----------------------------------------------------------------------------

@editor_bp.get(EDITOR_ROUTE_PATH)
def editor_index() -> Response:
    """
    Liefert die erste sichtbare Editor-Seite aus.

    Verhalten:
    1. reguläres Template rendern
    2. bei Template-Fehlern kontrolliert auf Fallback-HTML zurückfallen
    3. immer eine saubere HTML-Response mit konservativen Headern zurückgeben
    """
    try:
        rendered_html = _render_editor_shell()
        return _build_html_response(rendered_html, status_code=HTTPStatus.OK)

    except TemplateNotFound as exc:
        _safe_log_exception(
            f"Editor-Template nicht gefunden für Route {EDITOR_ROUTE_PATH!r}: {exc!r}"
        )

        fallback_context = _build_editor_context()
        fallback_html = _build_fallback_html(
            context=fallback_context,
            reason="template-not-found",
        )
        return _build_html_response(
            fallback_html,
            status_code=HTTPStatus.OK,
            fallback_reason="template-not-found",
        )

    except Exception as exc:
        _safe_log_exception(
            f"Unerwarteter Fehler beim Rendern der Editor-Route {EDITOR_ROUTE_PATH!r}: {exc!r}"
        )

        fallback_context = _build_editor_context()
        fallback_html = _build_fallback_html(
            context=fallback_context,
            reason="render-error",
        )
        return _build_html_response(
            fallback_html,
            status_code=HTTPStatus.OK,
            fallback_reason="render-error",
        )


__all__ = [
    "editor_bp",
    "editor_index",
]