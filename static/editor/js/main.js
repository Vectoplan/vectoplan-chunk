// services/vectoplan-editor/static/editor/js/main.js
(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // VECTOPLAN Editor - Minimal Client Runtime Bootstrap
  // ---------------------------------------------------------------------------
  // Ziele dieser Datei:
  // - die erste Browser-Runtime für /editor stabil starten
  // - Status, Viewport und Shell konsistent initialisieren
  // - robuste Bootstrap-Daten aus dem DOM lesen
  // - spätere Runtime-Erweiterungen vorbereiten, ohne jetzt schon Three.js,
  //   Chunking oder Microservice-Integration einzubauen
  //
  // Robustheitsprinzipien:
  // - defensive try/catch-Blöcke an allen I/O-Grenzen
  // - kleine interne Caches für DOM-Zugriffe und Bootstrap-Daten
  // - idempotenter Start: mehrfaches Starten verursacht keinen Strukturbruch
  // - kontrollierte globale Exporte für Debugging und spätere Erweiterung
  // ---------------------------------------------------------------------------

  var RUNTIME_NAME = "vectoplan-editor";
  var RUNTIME_VERSION = "0.1.0-shell";

  var DEFAULT_BOOTSTRAP = Object.freeze({
    appName: RUNTIME_NAME,
    pageTitle: "VECTOPLAN Editor",
    brandName: "VECTOPLAN Editor",
    routePath: "/editor",
    initialStatus: "Initialisierung...",
    runtimeReadyStatus: "Editor Runtime gestartet",
    runtimeLoadingStatus: "Frontend wird geladen...",
    runtimeErrorStatus: "Editor Runtime Fehler",
    viewportPlaceholder: "3D-Viewport wird hier aufgebaut",
    assets: Object.freeze({
      cssUrl: "/static/editor/css/editor.css",
      jsUrl: "/static/editor/js/main.js"
    }),
    hotbarSlots: Object.freeze(["1", "2", "3", "4", "5"])
  });

  var STATE = {
    started: false,
    startInProgress: false,
    failed: false,
    bootAttemptCount: 0,
    bootStartedAt: null,
    bootCompletedAt: null,
    lastResizeAt: null,
    lastVisibilityState: null,
    currentRuntimeState: "idle",
    lastErrorMessage: null,
    viewportSize: {
      width: 0,
      height: 0
    },
    bootstrap: null
  };

  var CACHE = {
    bootstrap: null,
    publicApi: null,
    dom: {
      root: null,
      status: null,
      viewport: null,
      viewportPlaceholder: null,
      hotbarSlots: null,
      runtimeRoot: null,
      bootstrapScript: null
    },
    listeners: {
      registered: false,
      resizeHandler: null,
      visibilityHandler: null,
      errorHandler: null,
      rejectionHandler: null
    },
    flags: {
      resizeScheduled: false
    }
  };

  // ---------------------------------------------------------------------------
  // Primitive Hilfsfunktionen
  // ---------------------------------------------------------------------------

  function safeWindow() {
    try {
      return window;
    } catch (error) {
      return null;
    }
  }

  function safeDocument() {
    try {
      return document;
    } catch (error) {
      return null;
    }
  }

  function safeNow() {
    try {
      return Date.now();
    } catch (error) {
      return 0;
    }
  }

  function safeConsole(method) {
    var win = safeWindow();
    var consoleRef = null;
    var args = [];
    var index = 1;

    try {
      consoleRef = win && win.console ? win.console : null;
    } catch (error) {
      consoleRef = null;
    }

    for (; index < arguments.length; index += 1) {
      args.push(arguments[index]);
    }

    if (!consoleRef) {
      return;
    }

    try {
      if (typeof consoleRef[method] === "function") {
        consoleRef[method].apply(consoleRef, args);
        return;
      }

      if (typeof consoleRef.log === "function") {
        consoleRef.log.apply(consoleRef, args);
      }
    } catch (error) {
      // bewusst still
    }
  }

  function safeString(value, fallback) {
    if (typeof value === "string") {
      var trimmed = value.trim();
      return trimmed || fallback;
    }

    if (value === null || value === undefined) {
      return fallback;
    }

    try {
      var converted = String(value).trim();
      return converted || fallback;
    } catch (error) {
      return fallback;
    }
  }

  function safeNumber(value, fallback, minimum, maximum) {
    var result;

    try {
      result = Number(value);
    } catch (error) {
      result = Number(fallback);
    }

    if (!isFinite(result)) {
      result = Number(fallback);
    }

    if (typeof minimum === "number" && isFinite(minimum)) {
      result = Math.max(minimum, result);
    }

    if (typeof maximum === "number" && isFinite(maximum)) {
      result = Math.min(maximum, result);
    }

    return result;
  }

  function safeArrayOfStrings(value, fallback) {
    var source = value;
    var result = [];
    var index;

    if (!Array.isArray(source)) {
      return fallback.slice();
    }

    for (index = 0; index < source.length; index += 1) {
      var item = safeString(source[index], "");
      if (item) {
        result.push(item);
      }
    }

    return result.length > 0 ? result : fallback.slice();
  }

  function safeJsonParse(value, fallback) {
    if (typeof value !== "string" || !value.trim()) {
      return fallback;
    }

    try {
      return JSON.parse(value);
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Bootstrap-JSON konnte nicht geparst werden.", error);
      return fallback;
    }
  }

  function freezeBootstrapObject(value) {
    try {
      if (value && typeof value === "object" && !Object.isFrozen(value)) {
        Object.freeze(value);
      }
    } catch (error) {
      // bewusst still
    }

    return value;
  }

  function shallowMerge(base, override) {
    var merged = {};
    var key;

    for (key in base) {
      if (Object.prototype.hasOwnProperty.call(base, key)) {
        merged[key] = base[key];
      }
    }

    if (override && typeof override === "object") {
      for (key in override) {
        if (Object.prototype.hasOwnProperty.call(override, key)) {
          merged[key] = override[key];
        }
      }
    }

    return merged;
  }

  function safeRequestAnimationFrame(callback) {
    var win = safeWindow();

    try {
      if (win && typeof win.requestAnimationFrame === "function") {
        return win.requestAnimationFrame(callback);
      }
    } catch (error) {
      // bewusst still
    }

    try {
      if (win && typeof win.setTimeout === "function") {
        return win.setTimeout(callback, 16);
      }
    } catch (error) {
      // bewusst still
    }

    try {
      callback();
    } catch (callbackError) {
      safeConsole("error", "[VECTOPLAN Editor] Fallback-Animation-Callback fehlgeschlagen.", callbackError);
    }

    return null;
  }

  // ---------------------------------------------------------------------------
  // DOM-Caching
  // ---------------------------------------------------------------------------

  function getCachedElement(cacheKey, resolver) {
    var cached = CACHE.dom[cacheKey];

    try {
      if (cached && typeof cached.isConnected === "boolean" && cached.isConnected) {
        return cached;
      }

      if (cached && typeof cached.isConnected === "undefined") {
        return cached;
      }
    } catch (error) {
      // bewusst still
    }

    try {
      var resolved = resolver();
      CACHE.dom[cacheKey] = resolved || null;
      return resolved || null;
    } catch (resolverError) {
      CACHE.dom[cacheKey] = null;
      safeConsole("warn", "[VECTOPLAN Editor] DOM-Resolver fehlgeschlagen:", cacheKey, resolverError);
      return null;
    }
  }

  function getRootElement() {
    return getCachedElement("root", function () {
      var doc = safeDocument();
      if (!doc || typeof doc.getElementById !== "function") {
        return null;
      }

      return doc.getElementById("editor-app");
    });
  }

  function getStatusElement() {
    return getCachedElement("status", function () {
      var doc = safeDocument();
      if (!doc || typeof doc.querySelector !== "function") {
        return null;
      }

      return doc.querySelector("[data-editor-status]");
    });
  }

  function getViewportElement() {
    return getCachedElement("viewport", function () {
      var doc = safeDocument();
      if (!doc || typeof doc.querySelector !== "function") {
        return null;
      }

      return doc.querySelector("[data-editor-viewport]") || doc.getElementById("editor-viewport");
    });
  }

  function getViewportPlaceholderElement() {
    return getCachedElement("viewportPlaceholder", function () {
      var doc = safeDocument();
      if (!doc || typeof doc.querySelector !== "function") {
        return null;
      }

      return doc.querySelector("[data-viewport-placeholder]");
    });
  }

  function getBootstrapScriptElement() {
    return getCachedElement("bootstrapScript", function () {
      var doc = safeDocument();
      if (!doc || typeof doc.getElementById !== "function") {
        return null;
      }

      return doc.getElementById("vectoplan-editor-bootstrap");
    });
  }

  function getHotbarSlotElements() {
    var cached = CACHE.dom.hotbarSlots;
    var doc;
    var elements;
    var result = [];

    if (Array.isArray(cached) && cached.length > 0) {
      return cached;
    }

    doc = safeDocument();
    if (!doc || typeof doc.querySelectorAll !== "function") {
      CACHE.dom.hotbarSlots = [];
      return [];
    }

    try {
      elements = doc.querySelectorAll("[data-hotbar-slot]");
      result = Array.prototype.slice.call(elements || []);
    } catch (error) {
      result = [];
      safeConsole("warn", "[VECTOPLAN Editor] Hotbar-Slots konnten nicht gelesen werden.", error);
    }

    CACHE.dom.hotbarSlots = result;
    return result;
  }

  // ---------------------------------------------------------------------------
  // Bootstrap-Auflösung
  // ---------------------------------------------------------------------------

  function readBootstrapFromScriptTag() {
    var element = getBootstrapScriptElement();
    var parsed = null;

    if (!element) {
      return {};
    }

    try {
      parsed = safeJsonParse(element.textContent || "", {});
    } catch (error) {
      parsed = {};
    }

    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }

    return parsed;
  }

  function readBootstrapFromGlobal() {
    var win = safeWindow();
    var value = null;

    if (!win) {
      return {};
    }

    try {
      value = win.__VECTOPLAN_EDITOR_BOOTSTRAP__;
    } catch (error) {
      value = null;
    }

    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return {};
    }

    return value;
  }

  function readBootstrapFromDataset() {
    var root = getRootElement();
    var result = {};

    if (!root || !root.dataset) {
      return result;
    }

    try {
      if (root.dataset.pageTitle) {
        result.pageTitle = root.dataset.pageTitle;
      }

      if (root.dataset.route) {
        result.routePath = root.dataset.route;
      }

      if (root.dataset.runtimeReadyStatus) {
        result.runtimeReadyStatus = root.dataset.runtimeReadyStatus;
      }
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Dataset-Bootstrap konnte nicht gelesen werden.", error);
    }

    return result;
  }

  function normalizeBootstrap(rawValue) {
    var raw = rawValue && typeof rawValue === "object" ? rawValue : {};
    var merged = shallowMerge(DEFAULT_BOOTSTRAP, raw);

    merged.assets = shallowMerge(DEFAULT_BOOTSTRAP.assets, raw.assets);
    merged.hotbarSlots = safeArrayOfStrings(raw.hotbarSlots, DEFAULT_BOOTSTRAP.hotbarSlots);

    merged.appName = safeString(merged.appName, DEFAULT_BOOTSTRAP.appName);
    merged.pageTitle = safeString(merged.pageTitle, DEFAULT_BOOTSTRAP.pageTitle);
    merged.brandName = safeString(merged.brandName, DEFAULT_BOOTSTRAP.brandName);
    merged.routePath = safeString(merged.routePath, DEFAULT_BOOTSTRAP.routePath);
    merged.initialStatus = safeString(merged.initialStatus, DEFAULT_BOOTSTRAP.initialStatus);
    merged.runtimeReadyStatus = safeString(
      merged.runtimeReadyStatus,
      DEFAULT_BOOTSTRAP.runtimeReadyStatus
    );
    merged.runtimeLoadingStatus = safeString(
      merged.runtimeLoadingStatus,
      DEFAULT_BOOTSTRAP.runtimeLoadingStatus
    );
    merged.runtimeErrorStatus = safeString(
      merged.runtimeErrorStatus,
      DEFAULT_BOOTSTRAP.runtimeErrorStatus
    );
    merged.viewportPlaceholder = safeString(
      merged.viewportPlaceholder,
      DEFAULT_BOOTSTRAP.viewportPlaceholder
    );
    merged.assets.cssUrl = safeString(merged.assets.cssUrl, DEFAULT_BOOTSTRAP.assets.cssUrl);
    merged.assets.jsUrl = safeString(merged.assets.jsUrl, DEFAULT_BOOTSTRAP.assets.jsUrl);

    freezeBootstrapObject(merged.assets);
    freezeBootstrapObject(merged.hotbarSlots);
    freezeBootstrapObject(merged);

    return merged;
  }

  function resolveBootstrap() {
    var merged;

    if (CACHE.bootstrap) {
      return CACHE.bootstrap;
    }

    try {
      merged = shallowMerge(
        DEFAULT_BOOTSTRAP,
        readBootstrapFromScriptTag()
      );
      merged = shallowMerge(
        merged,
        readBootstrapFromGlobal()
      );
      merged = shallowMerge(
        merged,
        readBootstrapFromDataset()
      );
      merged = normalizeBootstrap(merged);
    } catch (error) {
      safeConsole("error", "[VECTOPLAN Editor] Bootstrap-Auflösung fehlgeschlagen. Fallback wird verwendet.", error);
      merged = normalizeBootstrap(DEFAULT_BOOTSTRAP);
    }

    CACHE.bootstrap = merged;
    return CACHE.bootstrap;
  }

  // ---------------------------------------------------------------------------
  // DOM-Schreibzugriffe
  // ---------------------------------------------------------------------------

  function safeSetText(element, value) {
    if (!element) {
      return;
    }

    try {
      element.textContent = safeString(value, "");
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] textContent konnte nicht gesetzt werden.", error);
    }
  }

  function safeSetAttribute(element, name, value) {
    if (!element || !name) {
      return;
    }

    try {
      element.setAttribute(name, safeString(value, ""));
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Attribut konnte nicht gesetzt werden.", name, error);
    }
  }

  function safeSetDataset(element, name, value) {
    if (!element || !element.dataset || !name) {
      return;
    }

    try {
      element.dataset[name] = safeString(value, "");
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] data-* Wert konnte nicht gesetzt werden.", name, error);
    }
  }

  function safeSetStyle(element, propertyName, value) {
    if (!element || !element.style || !propertyName) {
      return;
    }

    try {
      element.style[propertyName] = safeString(value, "");
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Style konnte nicht gesetzt werden.", propertyName, error);
    }
  }

  // ---------------------------------------------------------------------------
  // Runtime Root / Viewport
  // ---------------------------------------------------------------------------

  function ensureRuntimeRoot() {
    var existing = CACHE.dom.runtimeRoot;
    var viewport = getViewportElement();
    var runtimeRoot;
    var gridHint;

    try {
      if (existing && typeof existing.isConnected === "boolean" && existing.isConnected) {
        return existing;
      }
    } catch (error) {
      // bewusst still
    }

    if (!viewport) {
      return null;
    }

    try {
      runtimeRoot = viewport.querySelector("[data-editor-runtime-root]");
      if (runtimeRoot) {
        CACHE.dom.runtimeRoot = runtimeRoot;
        return runtimeRoot;
      }
    } catch (error) {
      // bewusst still
    }

    try {
      runtimeRoot = safeDocument().createElement("div");
      runtimeRoot.setAttribute("data-editor-runtime-root", "true");
      runtimeRoot.setAttribute("aria-hidden", "true");

      safeSetStyle(runtimeRoot, "position", "absolute");
      safeSetStyle(runtimeRoot, "inset", "0");
      safeSetStyle(runtimeRoot, "zIndex", "1");
      safeSetStyle(runtimeRoot, "pointerEvents", "none");
      safeSetStyle(runtimeRoot, "overflow", "hidden");

      gridHint = viewport.querySelector(".viewport-grid-hint");

      if (gridHint && gridHint.parentNode === viewport && gridHint.nextSibling) {
        viewport.insertBefore(runtimeRoot, gridHint.nextSibling);
      } else if (gridHint && gridHint.parentNode === viewport) {
        viewport.appendChild(runtimeRoot);
      } else if (viewport.firstChild) {
        viewport.insertBefore(runtimeRoot, viewport.firstChild);
      } else {
        viewport.appendChild(runtimeRoot);
      }

      CACHE.dom.runtimeRoot = runtimeRoot;
      return runtimeRoot;
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Runtime Root konnte nicht erzeugt werden.", error);
      return null;
    }
  }

  function updateViewportPlaceholder(message) {
    var element = getViewportPlaceholderElement();
    var fallbackText = resolveBootstrap().viewportPlaceholder;
    safeSetText(element, safeString(message, fallbackText));
  }

  function updateViewportMetrics() {
    var viewport = getViewportElement();
    var root = getRootElement();
    var width = 0;
    var height = 0;

    if (!viewport) {
      return;
    }

    try {
      width = safeNumber(viewport.clientWidth, 0, 0);
      height = safeNumber(viewport.clientHeight, 0, 0);

      STATE.viewportSize.width = width;
      STATE.viewportSize.height = height;
      STATE.lastResizeAt = safeNow();

      safeSetDataset(viewport, "viewportWidth", String(width));
      safeSetDataset(viewport, "viewportHeight", String(height));

      if (root) {
        safeSetDataset(root, "viewportWidth", String(width));
        safeSetDataset(root, "viewportHeight", String(height));
      }
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Viewport-Metriken konnten nicht aktualisiert werden.", error);
    }
  }

  // ---------------------------------------------------------------------------
  // Status / Runtime State
  // ---------------------------------------------------------------------------

  function setStatusMessage(message) {
    var bootstrap = resolveBootstrap();
    var statusElement = getStatusElement();
    var finalMessage = safeString(message, bootstrap.initialStatus);

    safeSetText(statusElement, finalMessage);
  }

  function setRuntimeState(runtimeState) {
    var normalized = safeString(runtimeState, "idle");
    var root = getRootElement();
    var viewport = getViewportElement();
    var runtimeRoot = ensureRuntimeRoot();

    STATE.currentRuntimeState = normalized;

    if (root) {
      safeSetAttribute(root, "data-runtime-state", normalized);
      safeSetDataset(root, "runtimeState", normalized);
    }

    if (viewport) {
      safeSetAttribute(viewport, "data-runtime-state", normalized);
      safeSetDataset(viewport, "runtimeState", normalized);
    }

    if (runtimeRoot) {
      safeSetAttribute(runtimeRoot, "data-runtime-state", normalized);
    }
  }

  function applyDocumentTitle(title) {
    var doc = safeDocument();
    var finalTitle = safeString(title, DEFAULT_BOOTSTRAP.pageTitle);

    if (!doc) {
      return;
    }

    try {
      doc.title = finalTitle;
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Dokumenttitel konnte nicht gesetzt werden.", error);
    }
  }

  function decorateHotbarSlots() {
    var slots = getHotbarSlotElements();
    var bootstrap = resolveBootstrap();
    var labels = bootstrap.hotbarSlots;
    var index;

    if (!slots || slots.length === 0) {
      return;
    }

    for (index = 0; index < slots.length; index += 1) {
      var slotElement = slots[index];
      var label = labels[index] || String(index + 1);

      safeSetText(slotElement, label);
      safeSetAttribute(slotElement, "title", "Hotbar-Slot " + label);
      safeSetAttribute(slotElement, "aria-label", "Hotbar-Slot " + label);
    }
  }

  function syncRootMetadata() {
    var root = getRootElement();
    var bootstrap = resolveBootstrap();

    if (!root) {
      return;
    }

    safeSetAttribute(root, "data-app-name", bootstrap.appName);
    safeSetAttribute(root, "data-route", bootstrap.routePath);
    safeSetAttribute(root, "data-runtime-version", RUNTIME_VERSION);
    safeSetAttribute(root, "data-runtime-started", STATE.started ? "true" : "false");
    safeSetAttribute(root, "data-runtime-failed", STATE.failed ? "true" : "false");
  }

  function updateGlobalExports() {
    var win = safeWindow();

    if (!win) {
      return;
    }

    try {
      win.__VECTOPLAN_EDITOR_BOOTSTRAP__ = STATE.bootstrap || resolveBootstrap();
    } catch (error) {
      // bewusst still
    }

    try {
      win.__VECTOPLAN_EDITOR_RUNTIME_STARTED__ = !!STATE.started;
    } catch (error) {
      // bewusst still
    }

    try {
      win.__VECTOPLAN_EDITOR_RUNTIME_STATE__ = buildStateSnapshot();
    } catch (error) {
      // bewusst still
    }
  }

  // ---------------------------------------------------------------------------
  // Fehlerbehandlung
  // ---------------------------------------------------------------------------

  function markRuntimeError(message, error) {
    var bootstrap = resolveBootstrap();
    var finalMessage = safeString(message, bootstrap.runtimeErrorStatus);

    STATE.failed = true;
    STATE.startInProgress = false;
    STATE.started = false;
    STATE.lastErrorMessage = finalMessage;

    setRuntimeState("error");
    setStatusMessage(finalMessage);
    syncRootMetadata();
    updateGlobalExports();

    safeConsole("error", "[VECTOPLAN Editor] " + finalMessage, error || null);
  }

  // ---------------------------------------------------------------------------
  // Öffentliche State-Sicht
  // ---------------------------------------------------------------------------

  function buildStateSnapshot() {
    return {
      runtimeName: RUNTIME_NAME,
      runtimeVersion: RUNTIME_VERSION,
      started: !!STATE.started,
      startInProgress: !!STATE.startInProgress,
      failed: !!STATE.failed,
      bootAttemptCount: safeNumber(STATE.bootAttemptCount, 0, 0),
      bootStartedAt: STATE.bootStartedAt,
      bootCompletedAt: STATE.bootCompletedAt,
      lastResizeAt: STATE.lastResizeAt,
      lastVisibilityState: STATE.lastVisibilityState,
      currentRuntimeState: safeString(STATE.currentRuntimeState, "idle"),
      lastErrorMessage: STATE.lastErrorMessage,
      viewportSize: {
        width: safeNumber(STATE.viewportSize.width, 0, 0),
        height: safeNumber(STATE.viewportSize.height, 0, 0)
      },
      bootstrap: STATE.bootstrap || resolveBootstrap()
    };
  }

  // ---------------------------------------------------------------------------
  // Event Listener
  // ---------------------------------------------------------------------------

  function scheduleResizeUpdate() {
    if (CACHE.flags.resizeScheduled) {
      return;
    }

    CACHE.flags.resizeScheduled = true;

    safeRequestAnimationFrame(function () {
      CACHE.flags.resizeScheduled = false;
      updateViewportMetrics();
      updateGlobalExports();
    });
  }

  function handleWindowResize() {
    try {
      scheduleResizeUpdate();
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Resize-Verarbeitung fehlgeschlagen.", error);
    }
  }

  function handleVisibilityChange() {
    var doc = safeDocument();

    try {
      STATE.lastVisibilityState = doc && typeof doc.visibilityState === "string"
        ? doc.visibilityState
        : "unknown";
      updateGlobalExports();
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] visibilitychange konnte nicht verarbeitet werden.", error);
    }
  }

  function handleWindowError(event) {
    try {
      safeConsole("error", "[VECTOPLAN Editor] Unbehandelter Window-Fehler.", event || null);
    } catch (error) {
      // bewusst still
    }
  }

  function handleUnhandledRejection(event) {
    try {
      safeConsole("error", "[VECTOPLAN Editor] Unbehandelte Promise-Rejection.", event || null);
    } catch (error) {
      // bewusst still
    }
  }

  function registerGlobalListeners() {
    var win = safeWindow();
    var doc = safeDocument();

    if (CACHE.listeners.registered) {
      return;
    }

    CACHE.listeners.resizeHandler = handleWindowResize;
    CACHE.listeners.visibilityHandler = handleVisibilityChange;
    CACHE.listeners.errorHandler = handleWindowError;
    CACHE.listeners.rejectionHandler = handleUnhandledRejection;

    try {
      if (win && typeof win.addEventListener === "function") {
        win.addEventListener("resize", CACHE.listeners.resizeHandler, { passive: true });
        win.addEventListener("error", CACHE.listeners.errorHandler);
        win.addEventListener("unhandledrejection", CACHE.listeners.rejectionHandler);
      }
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Window-Listener konnten nicht vollständig registriert werden.", error);
    }

    try {
      if (doc && typeof doc.addEventListener === "function") {
        doc.addEventListener("visibilitychange", CACHE.listeners.visibilityHandler);
      }
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Document-Listener konnten nicht vollständig registriert werden.", error);
    }

    CACHE.listeners.registered = true;
  }

  // ---------------------------------------------------------------------------
  // Runtime API
  // ---------------------------------------------------------------------------

  function startEditorRuntime() {
    var bootstrap;
    var runtimeRoot;

    if (STATE.started) {
      updateGlobalExports();
      return buildStateSnapshot();
    }

    if (STATE.startInProgress) {
      return buildStateSnapshot();
    }

    STATE.startInProgress = true;
    STATE.failed = false;
    STATE.bootAttemptCount += 1;
    STATE.bootStartedAt = safeNow();
    STATE.bootCompletedAt = null;
    STATE.lastErrorMessage = null;

    try {
      bootstrap = resolveBootstrap();
      STATE.bootstrap = bootstrap;

      setRuntimeState("bootstrapping");
      setStatusMessage(bootstrap.runtimeLoadingStatus || bootstrap.initialStatus);
      updateViewportPlaceholder(bootstrap.viewportPlaceholder);
      applyDocumentTitle(bootstrap.pageTitle);
      decorateHotbarSlots();
      syncRootMetadata();
      updateGlobalExports();

      runtimeRoot = ensureRuntimeRoot();
      if (runtimeRoot) {
        safeSetAttribute(runtimeRoot, "data-runtime-name", RUNTIME_NAME);
        safeSetAttribute(runtimeRoot, "data-runtime-version", RUNTIME_VERSION);
      }

      registerGlobalListeners();
      updateViewportMetrics();

      safeRequestAnimationFrame(function () {
        try {
          STATE.started = true;
          STATE.startInProgress = false;
          STATE.bootCompletedAt = safeNow();

          setRuntimeState("running");
          setStatusMessage(bootstrap.runtimeReadyStatus);
          syncRootMetadata();
          updateGlobalExports();

          safeConsole(
            "info",
            "[VECTOPLAN Editor] Runtime erfolgreich gestartet.",
            buildStateSnapshot()
          );
        } catch (error) {
          markRuntimeError("Editor Runtime konnte nicht finalisiert werden.", error);
        }
      });

      return buildStateSnapshot();
    } catch (error) {
      markRuntimeError("Editor Runtime Start fehlgeschlagen.", error);
      return buildStateSnapshot();
    }
  }

  function setRuntimeStatus(message) {
    try {
      setStatusMessage(message);
      updateGlobalExports();
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] setRuntimeStatus fehlgeschlagen.", error);
    }

    return buildStateSnapshot();
  }

  function setRuntimeViewportMessage(message) {
    try {
      updateViewportPlaceholder(message);
      updateGlobalExports();
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] setRuntimeViewportMessage fehlgeschlagen.", error);
    }

    return buildStateSnapshot();
  }

  function getRuntimeState() {
    return buildStateSnapshot();
  }

  function getRuntimeBootstrap() {
    return STATE.bootstrap || resolveBootstrap();
  }

  function exposePublicApi() {
    var win = safeWindow();

    if (!win) {
      return null;
    }

    if (CACHE.publicApi) {
      return CACHE.publicApi;
    }

    CACHE.publicApi = Object.freeze({
      start: startEditorRuntime,
      getState: getRuntimeState,
      getBootstrap: getRuntimeBootstrap,
      setStatus: setRuntimeStatus,
      setViewportMessage: setRuntimeViewportMessage,
      markError: function (message, error) {
        markRuntimeError(message, error);
        return buildStateSnapshot();
      }
    });

    try {
      win.VECTOPLAN_EDITOR_RUNTIME = CACHE.publicApi;
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] Öffentliche Runtime-API konnte nicht exportiert werden.", error);
    }

    updateGlobalExports();
    return CACHE.publicApi;
  }

  // ---------------------------------------------------------------------------
  // DOM Ready
  // ---------------------------------------------------------------------------

  function onDomReady(callback) {
    var doc = safeDocument();

    if (!doc) {
      try {
        callback();
      } catch (error) {
        safeConsole("error", "[VECTOPLAN Editor] DOM-Ready Callback ohne Dokument fehlgeschlagen.", error);
      }
      return;
    }

    try {
      if (doc.readyState === "interactive" || doc.readyState === "complete") {
        callback();
        return;
      }

      doc.addEventListener(
        "DOMContentLoaded",
        function handleReady() {
          try {
            callback();
          } catch (error) {
            safeConsole("error", "[VECTOPLAN Editor] DOMContentLoaded Callback fehlgeschlagen.", error);
          }
        },
        { once: true }
      );
    } catch (error) {
      safeConsole("warn", "[VECTOPLAN Editor] DOM-Ready Registrierung fehlgeschlagen. Direkter Start wird versucht.", error);

      try {
        callback();
      } catch (callbackError) {
        safeConsole("error", "[VECTOPLAN Editor] Direktstart nach DOM-Ready Fehler fehlgeschlagen.", callbackError);
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Bootstrap Entry
  // ---------------------------------------------------------------------------

  exposePublicApi();

  onDomReady(function () {
    try {
      startEditorRuntime();
    } catch (error) {
      markRuntimeError("Editor Runtime Initialisierung auf DOM-Ebene fehlgeschlagen.", error);
    }
  });
})();