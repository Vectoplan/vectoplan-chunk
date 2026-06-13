# VECTOPLAN Chunk Service

Der `vectoplan-chunk` ist der Python-/Flask-Service für die editierbare chunkbasierte Welt von VECTOPLAN.

Er erzeugt die initiale Welt, liefert Chunks an den Editor, verarbeitet Block- und Weltänderungen und speichert den aktuellen Zustand bearbeiteter Chunks in PostgreSQL.

Kurz gesagt:

**Der Chunk-Service ist die operative Wahrheit der editierbaren Chunk-Welt. Unberührte Chunks werden generiert. Bearbeitete Chunks werden als Snapshots gespeichert. Jede bestätigte Änderung wird zusätzlich als Event-Historie gespeichert.**

---

## Inhalt

- [Zweck dieses Services](#zweck-dieses-services)
- [Was der Chunk-Service ist](#was-der-chunk-service-ist)
- [Die wichtigste Datenidee](#die-wichtigste-datenidee)
- [Rolle in der VECTOPLAN-Architektur](#rolle-in-der-vectoplan-architektur)
- [Was der Service können soll](#was-der-service-können-soll)
- [Was der Service bewusst nicht macht](#was-der-service-bewusst-nicht-macht)
- [Erste Weltversion](#erste-weltversion)
- [Snapshots und Events](#snapshots-und-events)
- [PostgreSQL-Modelle](#postgresql-modelle)
- [API-Grundform](#api-grundform)
- [Empfohlene Service-Struktur](#empfohlene-service-struktur)
- [Datenfluss](#datenfluss)
- [Entwicklungsreihenfolge](#entwicklungsreihenfolge)
- [Wichtige Invarianten](#wichtige-invarianten)
- [Offene spätere Themen](#offene-spätere-themen)
- [Kurzfassung](#kurzfassung)

---

## Zweck dieses Services

Der `vectoplan-chunk` ist der Service für die erste editierbare Weltbasis von VECTOPLAN.

Er ist dafür zuständig, dass der Editor eine Welt laden kann, in der sich Nutzer wie in einem Minecraft-/Hytale-artigen Editor bewegen und Blöcke setzen oder entfernen können.

Der Service soll:

- eine Startwelt definieren
- Chunks aus dieser Welt generieren
- Chunk-Daten an den Editor liefern
- Blocktypen bereitstellen
- Chunk-Commands verarbeiten
- bearbeitete Chunks speichern
- jede Änderung historisch protokollieren
- Dirty-Chunks bestimmen
- schnelle Ladezeiten ermöglichen
- später Library-, Geodaten-, Planeten- und Core-Mapping-Pfade vorbereiten

Der Service ist nicht nur ein technischer Chunk-Loader.

Er ist der erste Backend-Baustein für eine dauerhaft editierbare Welt.

---

## Was der Chunk-Service ist

Der Chunk-Service ist:

- ein Python-/Flask-Microservice
- eine Backend-Schicht für chunkbasierte Weltzustände
- der Generator der ersten flachen Welt
- der Speicherort für materialisierte Chunk-Snapshots
- der Speicherort für historische Chunk-Events
- der Command-Handler für `SetBlock`, `RemoveBlock` und spätere Weltoperationen
- die Runtime-Quelle für den Editor
- später eine Brücke zu Library, Geodaten, Planetenlogik und Core-Austausch

Der wichtigste Satz:

**Unberührte Weltbereiche werden generiert. Bearbeitete Weltbereiche werden gespeichert. Jede bestätigte Änderung wird als Event-Historie aufgenommen.**

---

## Die wichtigste Datenidee

Der Chunk-Service trennt drei Dinge:

```text
1. Generator
   erzeugt unberührte Chunks deterministisch

2. ChunkSnapshot
   ist die aktuelle Lade-Wahrheit eines bearbeiteten Chunks

3. ChunkEvent
   ist die historische Wahrheit für AI-Training, Analyse und spätere Auswertung
```

Das bedeutet:

```text
Chunk laden:
  wenn Snapshot existiert
    → Snapshot aus PostgreSQL laden
  sonst
    → Chunk aus Generator erzeugen

Block ändern:
  → aktuellen Chunk laden oder generieren
  → Änderung anwenden
  → ChunkSnapshot speichern
  → ChunkEvent schreiben
  → Dirty-Chunks zurückgeben
```

Wichtig:

**Events werden nicht benutzt, um bei jedem Laden den aktuellen Chunk-Zustand neu zu berechnen.**

Events sind für Historie, AI-Training, Analyse und spätere Auswertung da.

Snapshots sind für schnelle Ladezeiten da.

---

## Rolle in der VECTOPLAN-Architektur

Im Zielbild arbeiten die VECTOPLAN-Services so zusammen:

```text
vectoplan-editor
→ zeigt, rendert, targetet und sendet ChunkCommands

vectoplan-chunk
→ erzeugt Chunks, speichert Snapshots, schreibt Events

vectoplan-library-service
→ liefert später zulässige Block-, Objekt- und Variantendefinitionen

vectoplan-core-service
→ besitzt langfristig das kanonische semantische Projektmodell

vectoplan-converter-service
→ erzeugt später Austausch- und Exportformate
```

Für den ersten Entwicklungsstand gilt:

```text
Editor
→ ChunkCommand
→ Chunk-Service
→ PostgreSQL Snapshot
→ PostgreSQL Event
→ Editor lädt Dirty-Chunk neu
```

Der Core wird dabei nicht verändert.

Chunk-Commands sind keine Core-Commands.

---

## Was der Service können soll

Der Chunk-Service soll im ersten Zielzustand:

1. starten und Health liefern
2. eine flache Startwelt definieren
3. Platzhalter-Blocktypen bereitstellen
4. Chunks anhand von Koordinaten generieren
5. mehrere Chunks per Batch liefern
6. bearbeitete Chunks aus PostgreSQL laden
7. `SetBlock` verarbeiten
8. `RemoveBlock` verarbeiten
9. bei Änderungen ChunkSnapshots speichern
10. bei Änderungen ChunkEvents schreiben
11. Dirty-Chunks berechnen
12. dem Editor editor-kompatible Chunk-Daten liefern

Später soll er zusätzlich:

- Bereichsoperationen verarbeiten
- Batch-Commands ausführen
- weitere Generatoren unterstützen
- Library-Blocktypen übernehmen
- Geodaten einarbeiten
- planetare oder geschlossene Weltmodelle vorbereiten
- Chunk-Daten in Core-kompatible Austauschdaten übersetzen

---

## Was der Service bewusst nicht macht

Der Chunk-Service macht in der ersten Phase bewusst nicht:

- keine Editor-Oberfläche
- kein Three.js
- keine Kamera- oder Pointer-Lock-Logik
- keine Hotbar-UI
- keine direkte Core-Änderung
- keine BIM- oder IFC-Logik
- keine Kostenlogik
- keine automatische Höhenanpassung bestehender Bauwerke
- keine echte Kugelwelt
- keine Geokoordinatenlogik
- keine finale Library-Verwaltung
- kein normales Laden durch Replay aller Events

Der erste Fokus ist:

**Flache Welt laden, Chunks generieren, Blöcke setzen/abbauen, Snapshots speichern, Events schreiben.**

---

## Erste Weltversion

Die erste Welt ist bewusst einfach.

Sie ist:

- flach
- lokal
- chunkbasiert
- deterministisch
- ohne Geokoordinaten
- ohne Kugelprojektion
- ohne automatische Höhenanpassung
- ohne Core-Abhängigkeit
- ohne echte Library-Abhängigkeit

Eine erste Regel kann sein:

```text
y > 0  → Air
y = 0  → debug_grass
y < 0  → debug_dirt
```

Oder kontrollierter:

```text
y > surfaceY       → Air
y = surfaceY       → debug_grass
minY <= y < 0      → debug_dirt
y < minY           → Air oder später debug_stone
```

Trotzdem werden spätere Weltideen vorbereitet:

```text
planetId = dev-earth
projectionType = flat-local-v1
topologyType = flat-unbounded-v1
generatorType = flat-world
generatorVersion = 1
```

Die Welt bleibt in Phase 1 flach.

Spätere Planeten-, Kugel- oder Wrap-Logik wird nicht jetzt eingebaut.

---

## Snapshots und Events

### `ChunkSnapshot`

Ein `ChunkSnapshot` ist die aktuelle gespeicherte Lade-Wahrheit eines bearbeiteten Chunks.

Ein Snapshot entsteht, wenn:

- ein Block gesetzt wird
- ein Block entfernt wird
- ein Chunk explizit materialisiert wird
- später eine Bereichsoperation oder ein Import einen Chunk verändert

Nicht jeder betretene Chunk wird gespeichert.

Regel:

```text
Nur Chunks, die vom Generatorzustand abweichen oder explizit materialisiert werden müssen, werden dauerhaft gespeichert.
```

### `ChunkEvent`

Ein `ChunkEvent` ist die historische Aufzeichnung einer bestätigten Änderung.

Jede bestätigte Änderung erzeugt ein Event.

Events sind wichtig für:

- AI-Training
- spätere Bauvorschläge
- Analyse
- Debugging
- Replay
- Nutzungsverhalten
- Trainingsdatensätze

Wichtig:

```text
ChunkSnapshot = Lade-Wahrheit
ChunkEvent    = historische Wahrheit
```

---

## PostgreSQL-Modelle

PostgreSQL ist der primäre Speicher.

Es werden keine JSON-Dateien, kein FileStore und kein MemoryStore als Hauptspeicher geplant.

Die Flask-/SQLAlchemy-Models liegen im Ordner:

```text
models/
```

Die Logik liegt in:

```text
src/
```

### Vorgesehene erste Models

```text
models/
  __init__.py
  planet.py
  world.py
  block.py
  chunk.py
  event.py
```

### `Planet`

Für spätere Planetenlogik wird früh ein Planetenkontext vorbereitet.

Typische Felder:

```text
id
slug
name
status
created_at
updated_at
```

Für Phase 1 reicht:

```text
planetId = dev-earth
```

### `World`

Eine Welt gehört zu einem Planetenkontext.

Typische Felder:

```text
id
planet_id
slug
name
generator_type
generator_version
projection_type
topology_type
coordinate_system
chunk_size
cell_size
surface_y
min_y
max_y
seed
status
created_at
updated_at
```

Für Phase 1:

```text
generator_type = flat-world
generator_version = 1
projection_type = flat-local-v1
topology_type = flat-unbounded-v1
```

### `BlockType`

Blocktypen sind zunächst Platzhalter, später Library-gebunden.

Typische Felder:

```text
id
registry_id
registry_version
block_type_id
label
solid
placeable
breakable
metadata_json
created_at
updated_at
```

Für Phase 1:

```text
debug_grass
debug_dirt
```

### `ChunkSnapshot`

Lade-Wahrheit für materialisierte Chunks.

Typische Felder:

```text
id
planet_id
world_id
chunk_x
chunk_y
chunk_z
chunk_key
chunk_version
schema_version
block_registry_id
block_registry_version
content_json
content_binary
content_hash
materialized_reason
created_at
updated_at
```

Wichtige Eindeutigkeit:

```text
unique(world_id, chunk_x, chunk_y, chunk_z)
```

### `ChunkEvent`

Historische Wahrheit für AI-Training und Analyse.

Typische Felder:

```text
id
event_id
planet_id
world_id
chunk_key
chunk_x
chunk_y
chunk_z
user_id
session_id
command_type
position_x
position_y
position_z
block_before_type_id
block_after_type_id
cell_before_value
cell_after_value
target_face
tool
event_schema_version
chunk_version_before
chunk_version_after
payload_json
created_at
```

`ChunkEvent` ist append-only.

---

## API-Grundform

Der Service braucht am Anfang wenige klare Routen.

### Health

```text
GET /health
```

Antwort:

```json
{
  "ok": true,
  "service": "vectoplan-chunk",
  "version": "0.1.0"
}
```

### Blocktypen

```text
GET /blocks
```

Antwort:

```json
{
  "ok": true,
  "registryId": "debug-blocks",
  "registryVersion": "1",
  "blocks": [
    {
      "blockTypeId": "debug_grass",
      "label": "Debug Grass",
      "solid": true,
      "placeable": true,
      "breakable": true
    },
    {
      "blockTypeId": "debug_dirt",
      "label": "Debug Dirt",
      "solid": true,
      "placeable": true,
      "breakable": true
    }
  ]
}
```

### Welt-Metadaten

```text
GET /worlds/default
```

Antwort:

```json
{
  "ok": true,
  "world": {
    "planetId": "dev-earth",
    "worldId": "default",
    "chunkSize": 16,
    "cellSize": 1,
    "coordinateSystem": "vectoplan-world-y-up-v1",
    "projectionType": "flat-local-v1",
    "topologyType": "flat-unbounded-v1",
    "generatorType": "flat-world",
    "generatorVersion": "1",
    "surfaceY": 0,
    "minY": -8,
    "maxY": 64
  }
}
```

### Einzelnen Chunk laden

```text
GET /chunks?worldId=default&chunkX=0&chunkY=0&chunkZ=0
```

Antwort enthält einen editor-kompatiblen Chunk.

### Mehrere Chunks laden

```text
POST /chunks/batch
```

Request:

```json
{
  "worldId": "default",
  "chunks": [
    { "chunkX": 0, "chunkY": 0, "chunkZ": 0 },
    { "chunkX": 1, "chunkY": 0, "chunkZ": 0 }
  ]
}
```

### Command ausführen

```text
POST /commands
```

Beispiel `SetBlock`:

```json
{
  "type": "SetBlock",
  "worldId": "default",
  "userId": "user_123",
  "sessionId": "session_abc",
  "position": { "x": 1, "y": 1, "z": 1 },
  "blockTypeId": "debug_grass"
}
```

Beispiel `RemoveBlock`:

```json
{
  "type": "RemoveBlock",
  "worldId": "default",
  "userId": "user_123",
  "sessionId": "session_abc",
  "position": { "x": 1, "y": 1, "z": 1 }
}
```

Antwort:

```json
{
  "ok": true,
  "commandType": "SetBlock",
  "changed": true,
  "chunkVersion": "chunk_rev_000001",
  "changedChunks": ["0:0:0"],
  "dirtyChunks": ["0:0:0"],
  "eventId": "evt_000001",
  "affectedCells": [
    { "x": 1, "y": 1, "z": 1 }
  ]
}
```

---

## Empfohlene Service-Struktur

Die aktuelle Service-Struktur basiert auf einem Flask/Python-Muster und kann daraus weiter wachsen.

Eine sinnvolle Zielstruktur:

```text
vectoplan-chunk/
  AI.md
  README.md
  requirements.txt
  Dockerfile
  entrypoint.sh
  app.py
  wsgi.py
  config.py
  extensions.py

  bootstrap/
    __init__.py
    startup.py
    health.py

  routes/
    __init__.py
    health.py
    blocks.py
    worlds.py
    chunks.py
    commands.py

  models/
    __init__.py
    planet.py
    world.py
    block.py
    chunk.py
    event.py

  src/
    bootstrap/
      __init__.py
      startup.py

    blocks/
      __init__.py
      models.py
      defaults.py
      registry.py
      serialize.py

    coordinates/
      __init__.py
      models.py
      math.py
      chunk_keys.py

    world/
      __init__.py
      models.py
      config.py
      flat_world.py
      generators.py
      service.py

    chunks/
      __init__.py
      models.py
      content.py
      palette.py
      serializer.py
      service.py

    commands/
      __init__.py
      models.py
      validate.py
      executor.py
      dirty_chunks.py
      results.py

    events/
      __init__.py
      models.py
      recorder.py
      serialize.py

    repositories/
      __init__.py
      worlds.py
      blocks.py
      chunks.py
      events.py

    api/
      __init__.py
      responses.py
      errors.py
      normalize.py

    exchange/
      __init__.py
      core_mapping.py

    utils/
      __init__.py
      ids.py
      time.py
      safe.py

  tests/
    unit/
    integration/
    e2e/
```

Die wichtigste Trennung:

```text
routes/
→ HTTP-Adapter

models/
→ SQLAlchemy-/Flask-Models

src/
→ eigentliche Chunk-, World-, Command-, Event- und Repository-Logik
```

Routen sollen dünn bleiben.

---

## Datenfluss

### Chunk laden

```text
Editor bestimmt Chunk-Koordinaten
→ POST /chunks/batch
→ Chunk-Service lädt World-Konfiguration
→ Chunk-Service prüft ChunkSnapshot
→ falls Snapshot existiert: Snapshot laden
→ falls kein Snapshot existiert: Chunk generieren
→ RuntimeChunkContent serialisieren
→ Editor rendert Chunk
```

### Block setzen

```text
Editor targetet Zelle
→ Editor sendet SetBlock
→ Chunk-Service validiert Command
→ BlockRegistry prüft Blocktyp
→ Chunk laden oder generieren
→ Zellwert ändern
→ ChunkSnapshot speichern
→ ChunkEvent schreiben
→ Dirty-Chunks berechnen
→ Antwort an Editor
→ Editor lädt Dirty-Chunks neu
```

### Block entfernen

```text
Editor targetet Zelle
→ Editor sendet RemoveBlock
→ Chunk-Service validiert Command
→ Chunk laden oder generieren
→ Zelle auf Air setzen
→ ChunkSnapshot speichern
→ ChunkEvent schreiben
→ Dirty-Chunks berechnen
→ Antwort an Editor
→ Editor lädt Dirty-Chunks neu
```

---

## Zellwerte und Palette

Der Service muss exakt mit dem Editor kompatibel sein.

Aktuelle Regel:

```text
cellValue = 0
→ Air

cellValue = paletteIndex + 1
→ Block mit PaletteIndex
```

Beispiel:

```text
paletteIndex 0 = debug_grass
cellValue 1   = debug_grass

paletteIndex 1 = debug_dirt
cellValue 2   = debug_dirt
```

Diese Regel ist kritisch.

Wenn Python und TypeScript hier unterschiedlich arbeiten, entstehen Render-, Targeting- und Place/Break-Fehler.

---

## Koordinaten

Der Chunk-Service muss dieselbe Koordinatenlogik verwenden wie der Editor.

Wichtig sind:

- Weltkoordinaten
- Chunk-Koordinaten
- lokale Zellkoordinaten
- Chunk-Key
- negative Koordinaten
- Rundungsregeln

Beispiel bei `chunkSize = 16`:

```text
worldX = -1

korrekt:
chunkX = -1
localX = 15
```

Nicht:

```text
chunkX = 0
localX = -1
```

Koordinatenfunktionen gehören zentral nach:

```text
src/coordinates/
```

Nicht verstreut in Routen.

---

## Entwicklungsreihenfolge

### Phase 1 – Service stabil startbar

Ziel:

Der Flask-Service startet sauber.

Enthalten:

- App-Start
- Health-Route
- Routenregistrierung
- JSON-Fehlerantworten
- stabile Imports

Ergebnis:

```text
GET /health
```

funktioniert.

---

### Phase 2 – Models registrieren

Ziel:

PostgreSQL-Models sind definiert und registrierbar.

Enthalten:

- `models/__init__.py`
- `Planet`
- `World`
- `BlockType`
- `ChunkSnapshot`
- `ChunkEvent`

Ergebnis:

Die Datenbank kennt die Grundstruktur des Chunk-Service.

---

### Phase 3 – Default-Daten

Ziel:

Der Service kennt eine erste Welt und zwei Debug-Blöcke.

Enthalten:

- `dev-earth`
- `default`
- `debug_grass`
- `debug_dirt`
- `debug-blocks` Registry

Ergebnis:

```text
GET /blocks
GET /worlds/default
```

liefern sinnvolle Daten.

---

### Phase 4 – Koordinaten und Chunk-Modelle

Ziel:

Chunk-Koordinaten und Zellwerte sind stabil.

Enthalten:

- negative Koordinaten
- Chunk-Key
- lokale Zellkoordinaten
- Palette-Encoding
- CellValue-Encoding

Ergebnis:

Python und TypeScript können dieselbe Welt adressieren.

---

### Phase 5 – FlatWorldGenerator

Ziel:

Der Service kann flache Chunks generieren.

Enthalten:

- `FlatWorldGenerator`
- `surfaceY`
- `minY`
- `maxY`
- `debug_grass`
- `debug_dirt`

Ergebnis:

Unberührte Chunks können ohne Speicherung erzeugt werden.

---

### Phase 6 – Chunk-Load-Routen

Ziel:

Der Editor kann Chunks remote laden.

Enthalten:

- `GET /chunks`
- `POST /chunks/batch`
- Snapshot-oder-Generator-Logik
- RuntimeChunkContent-kompatible Antwort

Ergebnis:

Der Editor kann eine flache Remote-Welt anzeigen.

---

### Phase 7 – Command-System

Ziel:

Der Editor kann Blöcke setzen und entfernen.

Enthalten:

- `SetBlock`
- `RemoveBlock`
- Validierung
- Dirty-Chunks
- CommandResult

Ergebnis:

`POST /commands` funktioniert.

---

### Phase 8 – Snapshot- und Event-Speicherung

Ziel:

Änderungen bleiben dauerhaft sichtbar und werden historisch protokolliert.

Enthalten:

- ChunkSnapshot speichern
- ChunkEvent schreiben
- User-ID speichern
- Chunk-Version erhöhen
- Event-ID erzeugen

Ergebnis:

Nach erneutem Laden bleibt die Änderung sichtbar.

---

### Phase 9 – Editor-Anbindung

Ziel:

Der Editor nutzt `vectoplan-chunk` als Remote-ChunkSource.

Enthalten:

- Chunks remote laden
- Commands remote senden
- Dirty-Chunks neu laden

Ergebnis:

```text
Editor
→ Remote-Chunk laden
→ Block setzen
→ Snapshot speichern
→ Event schreiben
→ Dirty-Chunk neu laden
→ Änderung bleibt sichtbar
```

---

## Wichtige Invarianten

Diese Regeln gelten dauerhaft:

1. Der Chunk-Service besitzt die operative Wahrheit über Chunk-Zustände.
2. Unberührte Chunks werden generiert.
3. Bearbeitete Chunks werden als `ChunkSnapshot` in PostgreSQL gespeichert.
4. `ChunkSnapshot` ist die Lade-Wahrheit.
5. `ChunkEvent` ist die historische Wahrheit.
6. Events werden immer für bestätigte Änderungen gespeichert.
7. Events sind nicht der normale Ladepfad.
8. PostgreSQL ist der primäre Speicher.
9. Nur geänderte oder explizit materialisierte Chunks werden dauerhaft gespeichert.
10. `cellValue = 0` bedeutet Air.
11. `cellValue = paletteIndex + 1` bedeutet Block.
12. Python- und TypeScript-Koordinatenlogik müssen identisch sein.
13. Der Editor rendert Chunks, aber besitzt sie nicht dauerhaft als Wahrheit.
14. Blockänderungen laufen über ChunkCommands.
15. ChunkCommands ändern niemals direkt den Core.
16. Der Chunk-Service validiert Commands selbst.
17. Der Chunk-Service kennt erlaubte Blocktypen.
18. Blocktypen müssen versionierbar sein.
19. Die Debug-Blockliste ersetzt nicht dauerhaft die Library.
20. Der Chunk-Service liefert keine Three.js-Objekte.
21. Dirty-Chunks müssen bei Änderungen zuverlässig berechnet werden.
22. Die erste Welt ist flach.
23. Die erste Welt hat keinen echten Geokoordinatenbezug.
24. `planetId`, `projectionType` und `topologyType` werden trotzdem vorbereitet.
25. Keine automatische Höhenanpassung bestehender Bauwerke in Phase 1.
26. `/src` ist der Ort für die eigentliche Chunk-Mechanik.
27. `models/` ist der Ort für SQLAlchemy-/Flask-Models.
28. Routen bleiben dünne HTTP-Adapter.
29. Chunk-Daten müssen editor-kompatibel serialisiert werden.
30. Änderungen müssen nach erneutem Laden sichtbar bleiben.

---

## Offene spätere Themen

Diese Punkte sind bewusst nicht Teil der ersten Stufe:

- automatische Höhenanpassung bestehender Bauwerke
- Migration materialisierter Chunks bei Generatoränderungen
- Übergänge zwischen generierten und materialisierten Chunks
- Gebäude oder Bauwerke über mehrere Chunks als Struktur erkennen
- Bauaktionsgruppen oder `StructureId`
- Multi-User-Konflikte
- echte Library-Anbindung
- Core-Mapping
- Geodaten-Generatoren
- echte Kugelwelt
- geschlossene Topologie
- „von der anderen Seite wieder herauskommen“
- Kompression großer ChunkSnapshots
- Ableitung von AI-Trainingsdatensätzen aus dem Event-Log

Diese Themen sollen architektonisch vorbereitet, aber nicht in Phase 1 gelöst werden.

---

## Verhältnis zu `AI.md`

Dieses `README.md` ist die entwicklerfreundliche Projektübersicht.

Es erklärt:

- warum es den Service gibt
- wie die erste Welt funktioniert
- wie Snapshots und Events gedacht sind
- welche Routen und Models zuerst entstehen
- wie der erste technische Slice aussehen soll

Die `AI.md` ist dagegen das ausführlichere Architektur- und Verantwortungsdokument.

Faustregel:

```text
README.md
→ Einstieg, Orientierung, Entwicklungsgrundlage

AI.md
→ Architekturvertrag, Zielbild, Invarianten, langfristige Richtung
```

---

## Kurzfassung

Der `vectoplan-chunk` ist der Service für die editierbare Chunk-Welt von VECTOPLAN.

Er verbindet:

- flache Startwelt
- deterministische Chunk-Generierung
- PostgreSQL-Snapshots für bearbeitete Chunks
- Event-History für AI-Training
- ChunkCommands wie `SetBlock` und `RemoveBlock`
- editor-kompatible RuntimeChunkContent-Daten
- spätere Vorbereitung für Library, Geodaten, Planeten und Core-Mapping

Der erste funktionale Zielzustand ist:

```text
Editor lädt Chunk
→ Chunk wird generiert oder aus Snapshot geladen
→ Nutzer setzt Block
→ Chunk-Service speichert Snapshot
→ Chunk-Service schreibt Event
→ Editor lädt Dirty-Chunk neu
→ Änderung bleibt sichtbar
```

Das ist die Grundlage für die weitere Weltmechanik.