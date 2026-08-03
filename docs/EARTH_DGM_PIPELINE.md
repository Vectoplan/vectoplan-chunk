# Earth-DGM-Pipeline: von der Quelldatei bis zur begehbaren 3D-Welt

Stand: 2026-08-03

Dieses Dokument beschreibt den produktiven Datenweg für Welten vom Typ `earth`. Es ist der gemeinsame technische Vertrag für Webscraper, `vectoplan-geoserver-orchestrator`, `vectoplan-chunk` und `vectoplan-editor`.

Der derzeit vollständig angeschlossene Datensatz ist:

```text
Anzeigename: Digitales Geländemodell-5m
Dataset-ID:  digitales-gelaendemodell-5m
Kategorie:   terrain
Fähigkeiten: height-point, height-grid, earth-chunk-terrain
```

Die anderen festen Projekte sind bereits als stabile Zugänge registriert, besitzen aber noch keine vollständige Chunk-Transformation:

| Anzeigename | Dataset-ID | Vorbereitete Verwendung |
| --- | --- | --- |
| 3D-Gebäudedaten | `3d-gebaeudedaten` | CityGML, LoD2, 3D-Objektimport |
| Tatsächliche Nutzung | `tatsaechliche-nutzung` | Landnutzung und Oberflächenklassifikation |
| Hausumringe | `hausumringe` | Vektor-Footprints für Gebäude |
| Flurstücke | `flurstuecke` | Vektorgrenzen und Parzellen |

Die kanonische Deklaration liegt in [`src/geodata/fixed_projects.py`](../src/geodata/fixed_projects.py). Anzeigenamen, Slugs und Dienstpfade dürfen nicht an anderer Stelle erneut als unabhängige Konstanten gepflegt werden.

## 1. Ziel und wichtigste Regeln

Eine Earth-Welt soll aus der Georeferenz eines Benutzerprojekts ein reales Geländemodell erzeugen, ohne bei jeder Bewegung erneut Rohdateien zu öffnen oder dieselben Chunks zu übertragen.

Verbindliche Regeln:

1. Der Webscraper bleibt die Byte- und Release-Quelle der heruntergeladenen Rohdaten.
2. Der GeoServer-Orchestrator veröffentlicht nur explizit freigegebene Releases und erzeugt daraus einen abgeleiteten, versionierten Höhenindex.
3. Der Chunk-Service speichert keine zweite Rohdatenkopie. Er speichert nur abgeleitete Regions-, Spalten- und Chunk-Caches.
4. Der Editor besitzt keine dauerhafte Geländekopie. Er hält nur die für das Projekt benötigten Runtime-Chunks im Browsercache und rendert die Sichtweite.
5. Ein neuer Release ersetzt den stabilen Release erst, wenn seine Vorbereitung abgeschlossen ist. Währenddessen bleibt die vorherige Version nutzbar.
6. Derselbe Release, dieselbe Earth-Referenz und dieselbe Chunk-Koordinate ergeben denselben Cache-Key.
7. Ein Ausfall der Geodatenquelle darf keine leere 3D-Welt erzeugen. Es gilt die Reihenfolge: exakter Cache, letzter stabiler Cache, flacher Fallback.
8. Vertikale Bewegung darf keinen neuen vollständigen horizontalen Ladekreis pro Höhenstufe auslösen.
9. Direktdownload, Dateityperkennung und DGM-Vorbereitung benötigen kein LLM.

## 2. Gesamtarchitektur

```text
Webscraper
  Rohdateien, Zeitpläne, Prüfsummen, Release
          |
          | Projektfreigabe
          v
GeoServer-Orchestrator
  freigegebener Snapshot + abgeleiteter DGM-Höhenindex (.vpt.gz)
          |
          | Release-Schema + koordinatenbasierte Höhenraster
          v
vectoplan-chunk
  Earth-Referenz + Regionscache + Spaltencache + exakter Chunkcache
          |
          | projektbezogene Chunk-Batches, optional RLE-komprimiert
          v
vectoplan-editor
  ChunkRegistry + Meshing + Kollision + Spawn + sichtbarer 3D-Kreis
```

### Verantwortlichkeiten

| Komponente | Besitzt | Besitzt ausdrücklich nicht |
| --- | --- | --- |
| Webscraper | Quelldateien, Downloadstatus, Prüfsummen, Zeitplan, Rohdatenversion | 3D-Runtime-Chunks |
| GeoServer-Orchestrator | Freigabestatus, Release-Snapshot, Dateizugriff, DGM-Serving-Index | eine unabhängige zweite Rohdatenwahrheit |
| Chunk-Service | abgeleitete Chunks, Snapshot-/Event-Logik, Earth-Regionscache | die originale DGM-Dateisammlung |
| Editor | temporäre Runtime-Registry, Meshes, sichtbare Szene, Eingabe | kanonische Geodaten oder dauerhafte Chunk-Wahrheit |

## 3. Freigabe und Vorbereitung im GeoServer-Orchestrator

### 3.1 Release-Vertrag

Nur der zentral freigegebene Release von `digitales-gelaendemodell-5m` darf für Terrain-Serving vorbereitet werden. Die Release-ID wird als `release_key` durch die ganze Pipeline geführt.

```text
GET  /admin/api/production-publications/digitales-gelaendemodell-5m/terrain-serving
POST /admin/api/production-publications/digitales-gelaendemodell-5m/terrain-serving/prepare
```

Der `POST`-Body kann den freigegebenen Release explizit wiederholen:

```json
{"release_key": "<freigegebener-release-key>"}
```

Der Dienst lehnt einen anderen oder nicht freigegebenen Release mit HTTP `409` ab. Fehlen erkennbare XYZ-Geländekacheln, antwortet er mit HTTP `422`.

### 3.2 Was „vorbereitet“ bedeutet

Jede geeignete 1-km-DGM-Kachel wird pro Release einmal gelesen und in ein kompaktes, gzip-komprimiertes Höhenartefakt überführt:

```text
<publication>/<release>/terrain-serving/
  manifest.json
  tiles/<artefakt-identität>.vpt.gz
```

Das Manifest verwendet `vectoplan-terrain-serving.v1`, eine Kachel `vectoplan-terrain-serving-tile.v1`. Der Status meldet zum Beispiel:

```json
{
  "storageMode": "derived-versioned-height-index",
  "rawDataDuplicated": false,
  "status": "complete",
  "totalFiles": 11326,
  "processedFiles": 11326,
  "failedFiles": 0
}
```

Die Zahlen sind releaseabhängig. Entscheidend für die Umschaltung sind Status und Fortschritt, nicht eine fest erwartete Kachelanzahl. `rawDataDuplicated: false` bedeutet: Die Terrain-Vorbereitung kopiert nicht alle XYZ-Rohdateien in einen zweiten Bestand. Die `.vpt.gz`-Dateien sind ein abgeleiteter, wiederherstellbarer Suchindex.

### 3.3 Abfragevertrag

Die Einzelabfrage akzeptiert WGS84 oder EPSG:25832:

```text
GET /admin/api/production-publications/digitales-gelaendemodell-5m/query?lat=<breite>&lon=<länge>&radius_m=10
GET /admin/api/production-publications/digitales-gelaendemodell-5m/query?x=<ostwert>&y=<nordwert>&srid=25832&radius_m=10
```

Ohne Koordinaten liefert die Route absichtlich Hilfe und Beispielaufrufe. Die Chunk-Erzeugung verwendet:

```text
POST /admin/api/production-publications/digitales-gelaendemodell-5m/terrain-grid
```

```json
{
  "radius_m": 10,
  "points": [
    {"id": "anchor", "lat": 48.137, "lon": 11.575},
    {"id": "sample:0:0", "x": 691875.0, "y": 5334750.0, "srid": 25832}
  ]
}
```

Der interne Aufruf benötigt `X-Vectoplan-Service-Token`. Browser greifen nicht direkt darauf zu, sondern über Editor und Chunk-Service.

## 4. Earth-Referenz und Höhenmodell

Eine Earth-Welt besitzt eine projektbezogene globale Referenz. Aus ihr wird ein `referenceFingerprint` gebildet. Dadurch teilen Projekte mit unterschiedlichen Ankern keinen abgeleiteten Terraincache.

Der DGM-Wert ist absolut. Für die lokale Chunk-Welt gilt:

```text
lokale Oberfläche = DGM-Höhe - anchorElevationM + world.surfaceY
```

So liegt die Oberfläche am Projektanker nahe der lokalen Spawnhöhe. `anchorElevationM` bleibt als Metadatum erhalten, um lokale und reale Höhe wieder zuordnen zu können.

Ein generierter Chunk enthält unter anderem:

```json
{
  "chunkVersion": "dgm5:<release>:earth-dgm5-terrain.v3",
  "generationMode": "approved-dgm5-release",
  "stats": {
    "nonAirCellCount": 256,
    "minimumSurfaceY": 0,
    "maximumSurfaceY": 2
  },
  "terrain": {
    "datasetId": "digitales-gelaendemodell-5m",
    "releaseKey": "<release>",
    "anchorElevationM": 296.4,
    "fallback": false
  }
}
```

`minimumSurfaceY` und `maximumSurfaceY` bestimmen die benötigten vertikalen Chunk-Schichten. Die Oberfläche darf nicht allein aus der Kamerahöhe abgeleitet werden.

## 5. Cache- und Versionierungsmodell im Chunk-Service

Der persistente Cache liegt standardmäßig im Docker-Volume `vectoplan-chunk-terrain-cache`:

```text
/var/lib/vectoplan-chunk/terrain-cache/
  <referenceFingerprint>/
    digitales-gelaendemodell-5m/
      <releaseKey>/
        regions/<centerX>_<centerZ>_r<radius>_s<step>.json.gz
        columns/<chunkX>_<chunkZ>.json.gz
        <chunkX>_<chunkY>_<chunkZ>.json.gz
```

Die Ebenen sind:

1. **Regionscache:** niedrig aufgelöstes Höhenfeld für Projektkarte und Prefetch.
2. **Spaltencache:** `chunkSize × chunkSize` Oberflächenwerte einer X/Z-Spalte.
3. **Exakter Chunkcache:** vollständiger Blockinhalt einer X/Y/Z-Chunkadresse.

Speicherungen erfolgen über temporäre Dateien und atomaren Austausch. Prozess- und Dateisystem-Locks deduplizieren parallele Regionsvorbereitungen.

### Stabile und wartende Releases

```text
neuer Release noch nicht vollständig vorbereitet
  -> stabilen Regions-/Chunkcache weiterverwenden
  -> pendingReleaseKey ausgeben
  -> neue Region im Hintergrund vorbereiten

neuer Release vollständig vorbereitet
  -> Cache unter neuer releaseKey erzeugen
  -> erst dann umschalten
```

Alte Caches werden nicht still überschrieben oder mit dem neuen Release vermischt.

### Fehler- und Fallback-Reihenfolge

```text
1. exakter Cache des aktiven Releases
2. Regions-/Spaltencache des aktiven Releases
3. letzter stabiler Release als stale-hit
4. letzter exakter Cache als stale-hit
5. flacher, nicht leerer Fallback-Chunk
```

Der Fallback enthält eine begehbare Oberfläche und markiert in `terrain.fallback` sowie `terrain.reason`, warum keine DGM-Daten verwendet wurden. Er ist eine Verfügbarkeitsgarantie, kein fachlicher Ersatz.

## 6. Chunk-API und kompakte Übertragung

Der Browser verwendet Same-Origin-Routen des Editors:

```text
GET  /editor/api/chunk/projects/<projectId>/worlds/<worldId>/chunks?chunkX=0&chunkY=0&chunkZ=0
POST /editor/api/chunk/projects/<projectId>/worlds/<worldId>/chunks/batch
GET  /editor/api/chunk/projects/<projectId>/worlds/<worldId>/terrain/region
```

Die Chunk-Service-Routen liegen ohne `/editor/api/chunk` davor. Der Editor-Proxy ergänzt die vertrauenswürdige Service-Identität und hält interne Tokens aus dem Browser heraus.

```json
{
  "preferSnapshot": true,
  "allowGenerated": true,
  "chunks": [
    {"chunkX": 0, "chunkY": 0, "chunkZ": 0},
    {"chunkX": 1, "chunkY": 0, "chunkZ": 0}
  ]
}
```

Ein Batch-Element ist ein Antwortumschlag; der Runtime-Chunk liegt in `chunk`:

```json
{
  "projectId": "prj_...",
  "worldId": "world_spawn",
  "chunkKey": "0:0:0",
  "source": "generated",
  "chunk": {
    "chunkX": 0,
    "chunkY": 0,
    "chunkZ": 0,
    "cells": [],
    "palette": []
  }
}
```

Der Editor-Normalizer muss `raw.chunk`, ersatzweise `raw.content` oder `raw.data`, entpacken. Wird der Umschlag als Chunk interpretiert, zählt die Runtime geladene Datensätze, erhält aber leere Zellen und erzeugt keine sichtbaren Meshes.

`cells` kann für die Übertragung lauflängencodiert sein:

```json
{
  "encoding": "rle-value-count.v1",
  "decodedCellCount": 4096,
  "runCount": 4,
  "runs": [0, 1024, 3, 256, 0, 2560, 1, 256]
}
```

Kanonische Snapshots und Servercaches bleiben dicht. RLE ist ausschließlich ein Wire-Format. Der Editor validiert die dekodierte Zellzahl.

## 7. Editor: Karte, Streaming, Meshing und Spawn

### 7.1 Projektkarte und Sichtweite

`terrain/region` liefert das niedrig aufgelöste Höhenfeld für die Projektkarte. Die Karte zeigt vorbereitete Daten, rendert in 3D aber nicht alle Chunks gleichzeitig:

```text
vollständig vorbereiteter Projektbereich -> Servercache und 2D-Karte
aktuelle Sichtweite                    -> Browser-Registry und Three.js-Meshes
```

Beim Start wird der Kreis um den Benutzer von innen nach außen geladen. Danach werden fehlende Randchunks in Bewegungsrichtung priorisiert. Bereits sichtbare Chunks bleiben erhalten, bis der nächste Zielkreis vollständig im Cache liegt.

### 7.2 Keine doppelten Anfragen

Die Runtime lädt nur fehlende Chunk-Keys. Bereits registrierte Chunks werden nicht erneut angefragt. Während eines Sichtweitenwechsels wird nur das jüngste Kameraziel vorgemerkt; alte Zielbereiche werden nicht als weitere vollständige Kreise abgearbeitet.

Für Earth bleibt `earthStreamingChunkY` nach Erkennen der Oberfläche stabil. Fliegen oder Fallen verändert damit nicht die horizontale Ladeidentität. Benötigte Oberflächenschichten werden separat aus `minimumSurfaceY` und `maximumSurfaceY` bestimmt.

Nach abgeschlossenem initialen Laden muss ein stillstehender Client in einem ruhigen Messfenster keine weiteren `chunks/batch`- oder `terrain/region`-Requests erzeugen.

### 7.3 Meshing

Der Editor dekodiert den Chunk, legt ihn in die `ChunkRegistry` und erzeugt InstancedMeshes je Material/Blocktyp. Nach `setMatrixAt` werden Bounding Box und Bounding Sphere neu berechnet, damit Three.js gültige Instanzen nicht durch Frustum-Culling ausblendet.

```text
Antwort erfolgreich
-> Chunk-Umschlag entpackt
-> RLE dekodiert und validiert
-> nonAirCellCount > 0
-> Registry enthält Chunk
-> Mesh und Bounding Volumes erzeugt
-> Scene rendert Mesh
```

### 7.4 Spawn und Kollision

Sobald die Spalte am Spawn geladen ist, sucht die Runtime die höchste Nicht-Luft-Zelle:

```text
playerBaseY = surfaceY + 1.05
cameraY     = playerBaseY + Augenhöhe
```

Physik und manuelle Kamerasteuerung werden auf dieselbe Position gesetzt. So startet der Benutzer auf dem DGM und fällt nicht durch eine noch nicht erkannte oder relativ verschobene Oberfläche.

## 8. Betriebsparameter

Wichtige Defaults aus `docker-compose.all.yml`:

```text
VECTOPLAN_CHUNK_TERRAIN_ENABLED=true
GEOSERVER_ORCHESTRATOR_INTERNAL_URL=http://geoserver-orchestrator:8010
VECTOPLAN_CHUNK_TERRAIN_CACHE_DIR=/var/lib/vectoplan-chunk/terrain-cache
VECTOPLAN_CHUNK_TERRAIN_REQUEST_TIMEOUT_SECONDS=20
VECTOPLAN_CHUNK_TERRAIN_VERSION_CACHE_SECONDS=60
VECTOPLAN_CHUNK_TERRAIN_RADIUS_M=10
VECTOPLAN_CHUNK_TERRAIN_REGION_ENABLED=true
VECTOPLAN_CHUNK_TERRAIN_REGION_RADIUS_CHUNKS=64
VECTOPLAN_CHUNK_TERRAIN_REGION_SAMPLE_STEP_CHUNKS=2
VECTOPLAN_CHUNK_TERRAIN_REGION_BATCH_POINTS=480
```

`REGION_SAMPLE_STEP_CHUNKS=2` bedeutet, dass die große Kartenregion nicht jeden Chunk als eigenen DGM-Punkt abfragt. Zwischenwerte werden interpoliert; exakte Oberflächen werden danach in Spalten- und Chunkcaches materialisiert. Radius und Step sind Bestandteil des Cache-Namens, sodass geänderte Einstellungen bestehende Regionsdateien nicht still überschreiben.

## 9. Diagnose im Livebetrieb

### Container und Health

```powershell
docker compose -f docker-compose.all.yml ps vectoplan-editor vectoplan-chunk geoserver-orchestrator
curl.exe -fsS http://localhost:5110/health/ready
curl.exe -fsS http://localhost:5102/projects/_status
```

Die App läuft standardmäßig auf Port `5103`, der Editor auf `5100`, der Chunk-Service auf `5102` und der GeoServer-Orchestrator auf `5110`.

### DOM-Diagnose

| HTML-Attribut | Bedeutung | Gesundes Signal |
| --- | --- | --- |
| `data-scene-runtime-rendered-chunk-count` | Chunks mit Scene-Eintrag | größer als `0` |
| `data-scene-runtime-mesh-count` | erzeugte Meshes | größer als `0` |
| `data-earth-terrain-spawn-prepared` | Spawn auf DGM ausgerichtet | `true` |
| `data-earth-terrain-surface-y` | erkannte lokale Oberfläche | Zahl |
| `data-earth-terrain-player-base-y` | Fußposition | ungefähr Oberfläche + `1.05` |
| `data-earth-terrain-streaming-chunk-y` | feste Earth-Streaming-Schicht | bei vertikaler Bewegung stabil |
| `data-earth-terrain-spawn-reason` | letzter Spawn-Entscheid | kein dauerhaftes `surface-pending` |

Referenzmessung vom 2026-08-03 nach dem Batch-/Spawn-Fix:

```text
Terrain-Vorbereitung: 11.326/11.326, 0 Fehler
gerenderte Chunks:     107
Meshes:                130
surfaceY:              1
playerBaseY:           2.05
ruhiges 15-s-Fenster:  0 neue Batch-Requests, 0 neue Regionsrequests
```

Diese Werte sind keine festen Grenzwerte. Sie belegen für eine reale Sitzung, dass Karte, Oberfläche, Spawn, Meshing und Request-Deduplizierung gemeinsam funktioniert haben.

### Tests und Rebuild

```powershell
docker exec vectoplan-server-vectoplan-chunk-1 python -m pytest tests/test_earth_terrain_pipeline.py -q
docker compose -f docker-compose.all.yml build vectoplan-editor
docker compose -f docker-compose.all.yml up -d --force-recreate --no-deps vectoplan-editor
```

Nach Frontendänderungen muss das Editor-Image neu gebaut und der Container neu erzeugt werden. Ein Neustart eines alten Images reicht nicht.

## 10. Fehlerdiagnose

| Symptom | Wahrscheinliche Ursache | Prüfung / Maßnahme |
| --- | --- | --- |
| 2D-Karte sichtbar, 3D nur Himmel | Batch-Umschlag nicht entpackt oder Meshzahl `0` | Mesh-Dataset und `chunk_api_normalize.ts` prüfen |
| Chunks gezählt, aber alle Zellen Luft | `raw.chunk` wurde nicht normalisiert | Batch-Payload und `nonAirCellCount` prüfen |
| Mesh existiert, ist unsichtbar | veraltete Bounding Box/Sphere | Bounds nach Instanzmatrizen neu berechnen |
| Benutzer fällt oder startet falsch | Oberfläche am Spawn fehlt oder absolute Höhe wurde lokal verwendet | Spawn-Datasets und Ankerhöhe prüfen |
| Beim Fliegen viele vollständige Kreise | `chunkY` folgt Kamera oder alte Ziele laufen weiter | `earthStreamingChunkY`, Queue und Netzwerklog prüfen |
| Wiederholte Requests im Stillstand | Registry-/In-flight-Deduplizierung greift nicht | Netzwerklog nach Settle-Fenster prüfen |
| Neuer Release zeigt alte Daten | neue Vorbereitung läuft | `pendingReleaseKey` und Serving-Status prüfen |
| Erster Release zeigt flache Welt | Serving/Token/Quelle nicht bereit | Orchestrator-Health und `terrain.reason` prüfen |
| Direkter Browseraufruf erhält 401/403 | interne Route ist geschützt | Editor-Proxy oder Admin-Sitzung verwenden |
| Karte leer, 3D teilweise vorhanden | Regionscache fehlt | `terrain/region`, Job-Lock und Orchestrator-Batch prüfen |

## 11. Abnahmekriterien

- Direktdownload und DGM-Vorbereitung laufen ohne LLM.
- Die Dataset-ID lautet in allen Diensten `digitales-gelaendemodell-5m`.
- Der gewünschte Release ist zentral freigegeben.
- Terrain-Serving ist vollständig; Fehler und räumliche Lücken sind bewertet.
- `rawDataDuplicated` ist `false`; nur abgeleitete Serving-Artefakte werden ergänzt.
- Region und exakte Chunks lassen sich für die Projektgeoreferenz erzeugen.
- Wiederholte Abrufe derselben Adresse treffen den Cache.
- Projektkarte und 3D-Szene zeigen Nicht-Luft-Daten.
- Spawn und Kollision liegen auf der erkannten Oberfläche.
- Bewegung lädt nur neu eintretende Randchunks.
- Stillstand erzeugt nach dem Settle-Fenster keinen dauerhaften Datenverkehr.
- Bei Quellenausfall bleiben stabiler Cache oder begehbarer Fallback verfügbar.

## 12. Maßgebliche Implementierungsstellen

| Thema | Datei |
| --- | --- |
| feste Projektverknüpfungen | [`src/geodata/fixed_projects.py`](../src/geodata/fixed_projects.py) |
| DGM, Releasewechsel und Cache | [`src/world/earth/terrain_pipeline.py`](../src/world/earth/terrain_pipeline.py) |
| Chunk-, Batch- und Regionsrouten | [`routes/chunks.py`](../routes/chunks.py) |
| Orchestrator-Freigabe und Serving | [`../../vectoplan-geoserver-orchestrator/src/publications/service.py`](../../vectoplan-geoserver-orchestrator/src/publications/service.py) |
| Orchestrator-Adminrouten | [`../../vectoplan-geoserver-orchestrator/routes/admin.py`](../../vectoplan-geoserver-orchestrator/routes/admin.py) |
| Editor-Proxy | [`../../vectoplan-editor/routes/chunk.py`](../../vectoplan-editor/routes/chunk.py) |
| RLE- und Batch-Normalisierung | [`../../vectoplan-editor/src/frontend/api/chunk_api_normalize.ts`](../../vectoplan-editor/src/frontend/api/chunk_api_normalize.ts) |
| Sichtweite, Spawn und Meshing | [`../../vectoplan-editor/src/frontend/scene/scene_runtime.ts`](../../vectoplan-editor/src/frontend/scene/scene_runtime.ts) |
| 2D-Projektkarte | [`../../vectoplan-editor/src/frontend/scene/chunk_map_overlay.ts`](../../vectoplan-editor/src/frontend/scene/chunk_map_overlay.ts) |
| Container, Ports, Volumes und ENV | [`../../../docker-compose.all.yml`](../../../docker-compose.all.yml) |

Die globale Earth-Koordinatenentscheidung steht in [`adr/ADR-earth-world-v1.md`](adr/ADR-earth-world-v1.md). Dieses Dokument ergänzt sie um die implementierte DGM-, Cache- und Editor-Pipeline.
