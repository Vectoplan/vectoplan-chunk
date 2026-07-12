<!-- services/vectoplan-chunk/IST-Zustand.md -->
# IST-Zustand.md – VECTOPLAN Chunk Service

## Status dieser aktualisierten Fassung

Stand: 2026-07-12  
Status: PostgreSQL-gestützter, projektgescopter Chunk-Service mit getrenntem Runtime-/DB-Bootstrap, stabiler `world_spawn`-Default-Welt, App-Provisioning, bestätigter Editor-Chunk-Anbindung und betriebsfähiger eingebauter Systemblock-Schicht.

Diese Aktualisierung **kürzt die bisherige IST-Dokumentation nicht**. Die vollständige bisherige Bestandsaufnahme bleibt weiter unten als historische Basis enthalten. Ergänzt wurden der inzwischen bestätigte Systemblock-Katalog, Air- und Railing-Invarianten, die persistente Registry-Spiegelung, die neuen Blockrouten und die verifizierte Editor-/Admin-Sichtbarkeit.

### Neu bestätigter Stand seit der historischen Basisfassung

```text
Systemblock-Katalog bereit
→ catalogReady = true
→ zwei Code-Definitionen vorhanden
→ system_air und system_railing

Registry bereit
→ registryReady = true
→ debug-blocks@1
→ source = internal

Air-Invariante bereit
→ airInvariantReady = true
→ cellValue 0
→ keine persistente BlockType-Zeile
→ nicht inventarsichtbar
→ nicht platzierbar

Railing-Mirror bereit
→ systemRailingReady = true
→ system_railing ist persistiert
→ status = active
→ category = system
→ kein Drift zur Code-Definition
→ inventarsichtbar
→ platzierbar
→ selektierbar
→ kollidierbar

Gesamte Systemblock-Schicht bereit
→ systemBlocksReady = true

Normale Welt-Blockroute
→ debug_grass  = paletteIndex 0 / cellValue 1
→ debug_dirt   = paletteIndex 1 / cellValue 2
→ system_railing = paletteIndex 2 / cellValue 3
→ Air bleibt separat bei cellValue 0
```

### Wichtige begriffliche Einordnung

Im Backend gibt es aktuell keine eigene technische Kategorie `admin`.

Die sichtbaren Blockarten sind:

```text
category = debug
→ debug_grass
→ debug_dirt

category = system
→ system_railing

reservierter Zellzustand
→ system_air
```

Wenn in der Oberfläche von „Admin-Blöcken“ gesprochen wird, ist damit im derzeit bestätigten Zustand vor allem der eingebaute, unveränderliche Systemblock `system_railing` gemeint.

### Aktuell wichtigste neue Dateien

```text
services/vectoplan-chunk/src/system_blocks/__init__.py
services/vectoplan-chunk/src/system_blocks/contracts.py
services/vectoplan-chunk/src/system_blocks/catalog.py
services/vectoplan-chunk/src/system_blocks/bootstrap.py

services/vectoplan-chunk/src/system_blocks/air/__init__.py
services/vectoplan-chunk/src/system_blocks/air/definition.py

services/vectoplan-chunk/src/system_blocks/railing/__init__.py
services/vectoplan-chunk/src/system_blocks/railing/definition.py

services/vectoplan-chunk/src/system_blocks/IST-Zustand.md
```

### Aktuell wichtigste geänderte Integrationsdateien

```text
services/vectoplan-chunk/src/bootstrap/default_seed.py
services/vectoplan-chunk/src/bootstrap/db_bootstrap.py
services/vectoplan-chunk/scripts/bootstrap_db.py
services/vectoplan-chunk/routes/blocks.py
```

### Neue zentrale Systemblock-Routen

```text
GET /projects/<project_id>/worlds/<world_id>/blocks/system
GET /blocks/system
```

Die normale Welt-Blockroute bleibt für Editor und konkrete Palette entscheidend:

```text
GET /projects/<project_id>/worlds/<world_id>/blocks
```

---

## Historische Basisfassung – VECTOPLAN Chunk Service

<!-- services/vectoplan-chunk/IST-Zustand.md -->

### Status der historischen Basisfassung

Stand: 2026-06-24
Status: PostgreSQL-gestützter, projektgescopter Chunk-Service mit getrenntem Runtime-/DB-Bootstrap, stabiler `world_spawn`-Default-Welt, App-Provisioning und bestätigter Editor-Chunk-Anbindung

> Historische Basis: Teil 1 von 2

Diese Datei beschreibt den aktuellen **IST-Zustand** des `vectoplan-chunk`-Services nach:

```text
- dem PostgreSQL-/Persistenz-Slice,
- dem ersten erfolgreichen SetBlock-Command-Test,
- dem bestätigten RemoveBlock-End-to-End-Test,
- dem bestätigten Generator-Chunk-Ladepfad,
- dem bestätigten Snapshot-Chunk-Ladepfad,
- der Diagnose des früheren Worker-/Startup-Hängers,
- der Trennung von Runtime-Startup und DB-Bootstrap,
- der Einführung expliziter Bootstrap-/Check-only-Modi,
- dem Umbau von Runtime auf read-only,
- der Reparatur der Default-Seed-Invarianten,
- der harten Trennung von world_spawn und flat,
- der Stabilisierung von routes/chunks.py,
- der Stabilisierung von routes/commands.py,
- der Erweiterung von routes/projects.py um App-Provisioning und Readiness,
- der erfolgreichen App→Chunk-Provisionierung,
- der erfolgreichen Editor→Chunk Chunk-Batch-Anbindung.
```

Diese Datei ist keine Zielarchitektur, sondern eine Bestandsaufnahme des aktuell erreichten Zustands.

Diese Fassung dokumentiert:

```text
- die vorhandene World-/Flat-World-Schicht,
- die projektgescopte World-State-API,
- die PostgreSQL-Anbindung,
- die SQLAlchemy-Models,
- die ChunkSnapshot-/ChunkEvent-/WorldCommandLog-Struktur,
- den bestätigten Generator-Read-Pfad,
- den bestätigten Snapshot-Read-Pfad,
- den bestätigten SetBlock-Schreibpfad,
- den bestätigten RemoveBlock-Schreibpfad,
- das bestätigte Reload-Verhalten nach Blockänderungen,
- die frühere Worker-/Startup-Hänger-Diagnose,
- die bestätigte Ursache im Startup-/Auto-Create-/Auto-Seed-Pfad,
- den durchgeführten Bootstrap-Umbau,
- die neu ergänzten und geänderten Bootstrap-Dateien,
- die aktuelle Runtime-/Init-Trennung im Compose-/Entrypoint-Pfad,
- den aktuellen stabilen Runtime-Start mit 2 Gunicorn-Workern,
- die App-Provisioning-Routen für vectoplan-app,
- die Readiness-Signale schemaReady/seedReady/defaultWorldReady,
- die weiterhin offenen Tests für ReplaceBlock, PlaceObject, RemoveObject, Batch-Mix und Grenzfälle,
- konkrete PowerShell-Testbefehle für Block setzen, Block entfernen, Reload und DB-Prüfung.
```

Wichtigster aktueller Befund:

```text
Der Service ist nicht mehr nur generatorbasiert.
PostgreSQL ist angebunden.
Default-Projekt, Universe, World, BlockRegistry und Debug-Blocks existieren.

Der Generator-Chunk-Pfad funktioniert.
Der Snapshot-Chunk-Pfad funktioniert.
SetBlock funktioniert End-to-End.
RemoveBlock funktioniert End-to-End.
SetBlock und RemoveBlock schreiben WorldCommandLog, ChunkEvent und ChunkSnapshot.
Reload nach SetBlock zeigt die gesetzte Zelle.
Reload nach RemoveBlock zeigt die entfernte Zelle.

Der frühere Fehler lag nicht am Port, nicht am Docker-Netzwerk und nicht grundsätzlich an PostgreSQL.
Der Fehler wurde auf Runtime-Startup mit Auto-Create/Auto-Seed beziehungsweise Startup-Hooks eingegrenzt.

Der normale Runtime-Startup wurde entschärft:
Runtime ist read-only.
db.create_all() und Default-Seeding laufen nicht mehr im normalen Gunicorn-Worker-Startup.
Explizite DB-Initialisierung läuft über Bootstrap-Modus, Init-Container oder scripts/bootstrap_db.py.

Die zentrale Default-Invariante ist stabil:
Project        = dev-project
Universe       = dev-universe
WorldInstance  = world_spawn
Template       = flat
ProviderWorld  = flat

world_spawn ist die konkrete editierbare Welt.
flat ist nur Template-/Provider-Welt.

App→Chunk funktioniert:
vectoplan-app ruft /projects/by-app/<app_project_public_id> auf.
vectoplan-chunk erzeugt oder findet Chunk-Projekt, Universe und world_spawn.
vectoplan-editor kann anschließend Chunks aus world_spawn laden.
```

Der aktuelle Zustand ist:

```text
Runtime-Startup stabil
→ Statusrouten 200
→ DB/Seed-Ready-Check erfolgreich
→ schemaReady = true
→ seedReady = true
→ defaultProjectReady = true
→ defaultUniverseReady = true
→ defaultWorldReady = true
→ Generator-Chunk lädt
→ Snapshot-Chunk lädt
→ SetBlock persistiert
→ Reload zeigt Änderung
→ RemoveBlock persistiert
→ Reload zeigt Entfernung
→ App-Provisioning funktioniert
→ Editor chunks/batch funktioniert
→ kein Worker-Timeout im getesteten Ablauf
→ kein RAM-Wachstum im getesteten Ablauf
```

---

## 1. Kurzfassung

Der `vectoplan-chunk` ist inzwischen ein PostgreSQL-gestützter Chunk-Welt-Service.

Er besitzt aktuell vier technische Ebenen:

```text
1. src/world
   → Provider-, Template-, Generator- und Debug-Welt-Schicht

2. src/world_state
   → Projekt-/Universum-/World-State-Service-Fassade
   → PostgreSQL-orientiert, aber mit Legacy-Kompatibilität

3. models/
   → SQLAlchemy-/PostgreSQL-Models für Projekte, Universen, Worlds,
     Blocktypen, Snapshots, Commands, Events und mehrblockfähige Objekte

4. src/bootstrap + scripts/bootstrap_db.py + entrypoint.sh
   → getrennte Runtime-, Check-only- und DB-Bootstrap-Schicht
```

Die fachliche Struktur lautet:

```text
Project
→ Universe
→ WorldInstance
→ ChunkSnapshot
→ ChunkEvent
```

Die aktuelle Dev-/Default-Struktur ist:

```text
projectId       = dev-project
universeId      = dev-universe
worldId         = world_spawn
templateId      = flat
providerId      = flat
providerWorldId = flat
blockRegistry   = debug-blocks / version 1
```

Bedeutung:

```text
dev-project
→ enthält dev-universe

dev-universe
→ enthält world_spawn

world_spawn
→ konkrete editierbare Projektwelt

flat
→ Provider-/Template-Welt für generierte, unberührte Chunks
```

Der erste funktionale Backend-Slice ist bestätigt:

```text
Chunk laden
→ SetBlock ausführen
→ Snapshot aktualisieren
→ Event schreiben
→ CommandLog schreiben
→ Chunk neu laden
→ Änderung sichtbar
→ RemoveBlock ausführen
→ Snapshot aktualisieren
→ Event schreiben
→ CommandLog schreiben
→ Chunk neu laden
→ Änderung entfernt
```

Der aktuelle App-/Editor-Slice ist ebenfalls bestätigt:

```text
vectoplan-app erstellt App-Projekt
→ vectoplan-app ruft Chunk-Provisioning auf
→ vectoplan-chunk erzeugt Chunk-Projektgraph
→ chunk_project_id wird an App zurückgegeben
→ chunk_world_id = world_spawn
→ vectoplan-editor lädt chunks/batch über Chunk-Service
→ HTTP 200
```

---

## 2. Aktuell bestätigter Stand

### 2.1 Docker-Start

Der Service startet als Container:

```text
vectoplan-chunk
```

Externer Port:

```text
localhost:5002
```

Interner Container-Port:

```text
5000
```

Eigener PostgreSQL-Container:

```text
vectoplan-chunk-db
```

Externer Datenbank-Port:

```text
localhost:5433
```

Interner Datenbank-Port:

```text
5432
```

Datenbank:

```text
POSTGRES_DB       = vectoplan_chunk
POSTGRES_USER     = vectoplan_chunk
POSTGRES_PASSWORD = vectoplan_chunk
```

Bestätigter Runtime-Start:

```text
Gunicorn Workers = 2
Gunicorn Threads = 2
PostgreSQL socket reachable
Runtime read-only = true
Runtime DB mutations allowed = false
Auto create all = false
Auto seed defaults = false
Seed debug blocks = false
Seed dev project = false
Repair missing columns = false
Repair seed invariants = false
Schema/Seed ready check = true
Schema/Seed ready required = true
Default project = dev-project
Default universe = dev-universe
Default world = world_spawn
Default instance world = world_spawn
Default template = flat
Provider world = flat
```

Bewertung:

```text
Die Containerstruktur ist richtig erweitert.
Der Chunk-Service wartet beim Start auf PostgreSQL.
PostgreSQL ist als eigener Service angebunden.
Runtime-Startup läuft stabil und read-only.
DB-Initialisierung ist aus Runtime herausgezogen.
```

---

### 2.2 Docker-Compose-Stand

Aktuell relevant:

```text
services/vectoplan-server/docker-compose.all.yml
```

Wichtige Entscheidung:

```text
Runtime-Service und Init-/Bootstrap-Pfad sind getrennt.
```

Runtime-Container `vectoplan-chunk`:

```text
VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED=false
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=false
VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY=true
VECTOPLAN_CHUNK_AUTO_CREATE_ALL=false
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS=false
VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS=false
VECTOPLAN_CHUNK_SEED_DEV_PROJECT=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT=false
VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS=false
```

Init-/Bootstrap-Pfad:

```text
VECTOPLAN_CHUNK_STARTUP_MODE=db-bootstrap
VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED=true
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=true
VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY=false
VECTOPLAN_CHUNK_AUTO_CREATE_ALL=true
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS=true
VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS=true
VECTOPLAN_CHUNK_SEED_DEV_PROJECT=true
VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL=true
VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS=true
VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEBUG_BLOCKS=true
VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEV_PROJECT=true
VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS=true
VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS=true
```

Init-Befehl:

```text
python ./scripts/bootstrap_db.py \
  --config "$${VECTOPLAN_CHUNK_CONFIG:-development}" \
  --create-all \
  --repair-missing-columns \
  --seed \
  --json
```

Empfohlene finale Init-Variante:

```text
python ./scripts/bootstrap_db.py \
  --config "$${VECTOPLAN_CHUNK_CONFIG:-development}" \
  --create-all \
  --repair-missing-columns \
  --seed \
  --repair-seed-invariants \
  --json
```

Bewertung:

```text
Compose trennt Runtime und DB-Bootstrap.
Das war die entscheidende Korrektur gegen Worker-Races.
Runtime mutiert die DB nicht mehr.
```

---

### 2.3 PostgreSQL-Zustand

Bestätigter Datenbankzustand nach erfolgreichem SetBlock/RemoveBlock-Test:

```text
projects                >= 1
universes               >= 1
world_instances         >= 1
block_registries        >= 1
block_types             >= 2
chunk_snapshots         >= 1
chunk_events            >= 3
world_command_logs      >= 3
world_object_instances  >= 0
world_object_chunk_refs >= 0
```

Die exakte Anzahl von `chunk_events`, `world_command_logs` und `chunk_snapshots` steigt mit jedem Testlauf und mit App-/Editor-Nutzung.

Bestätigte Tabellen:

```text
block_registries
block_types
chunk_events
chunk_snapshots
projects
universes
world_command_logs
world_instances
world_object_chunk_refs
world_object_instances
```

Bestätigter Default-Seed-/Invariant-Status:

```text
project.exists = true
project.projectId = dev-project

universe.exists = true
universe.universeId = dev-universe

world.exists = true
world.worldId = world_spawn

blockRegistry.exists = true
blockRegistry.registryId = debug-blocks
blockRegistry.registryVersion = 1

debugBlocks.complete = true
```

Bestätigter Snapshot nach SetBlock + RemoveBlock:

```text
chunkKey       = 0:0:0
cellCount      = 4096
status         = active
chunkRevision  steigt mit jeder Änderung
chunkVersion   steigt mit jeder Änderung
```

Hinweis:

```text
Die konkrete chunkVersion/chunkRevision hängt davon ab,
wie viele Änderungen bereits gegen denselben Snapshot ausgeführt wurden.

Wichtig ist:
- es bleibt ein aktiver Snapshot pro Chunk
- die Revision steigt bei jeder echten Änderung
- der Snapshot enthält den aktuellen Ladezustand
```

Bestätigtes Event-Beispiel `SetBlock`:

```text
commandType      = SetBlock
chunkKey         = 0:0:0
position         = 6 / 4 / 5
cellBeforeValue  = 0
cellAfterValue   = 1
```

Bestätigtes Event-Beispiel `RemoveBlock`:

```text
commandType      = RemoveBlock
chunkKey         = 0:0:0
position         = 6 / 4 / 5
cellBeforeValue  = 1
cellAfterValue   = 0
```

Bestätigter CommandLog `SetBlock`:

```text
commandType          = SetBlock
commandStatus        = applied
changed              = true
affectedChunkCount   = 1
eventCount           = 1
```

Bestätigter CommandLog `RemoveBlock`:

```text
commandType          = RemoveBlock
commandStatus        = applied
changed              = true
affectedChunkCount   = 1
eventCount           = 1
```

Bewertung:

```text
Der DB-Schreibpfad für SetBlock und RemoveBlock funktioniert.
PostgreSQL enthält materialisierte ChunkSnapshots, ChunkEvents und WorldCommandLogs.
Events sind append-only.
Snapshots bleiben Lade-Wahrheit.
```

---

### 2.4 Statusrouten

Bestätigte Statusrouten:

```text
GET /                          → 200
GET /projects/_status           → 200
GET /worlds/_status             → 200
GET /blocks/_status             → 200
GET /chunks/_status             → 200
GET /commands/_status           → 200
```

Aktueller `projects`-Status ist erweitert und enthält:

```text
serviceReady
schemaReady
seedReady
defaultProjectReady
defaultUniverseReady
defaultWorldReady
defaultIds
requirements
database
bootstrapStatus
models
counts
config
settings
```

Wichtigste Readiness-Signale:

```text
schemaReady = true
seedReady = true
defaultProjectReady = true
defaultUniverseReady = true
defaultWorldReady = true
```

Aktueller bestätigter `chunks`-Status:

```text
route.moduleVersion = 0.3.0
dbBacked = true
snapshotBacked = true
generatedFallback = true
eventReplayLoadPath = false
relationshipLoadingDisabledInReadPath = true
```

Aktueller bestätigter `commands`-Status:

```text
route.moduleVersion = 0.2.0
dbBacked = true
snapshotWrites = true
eventWrites = true
objectStoragePrepared = true
relationshipLoadingDisabledInCommandPath = true
```

Bewertung:

```text
Statusrouten sind erreichbar.
Models werden sauber registriert.
Counts werden geliefert.
DB-Verbindung ist konfiguriert.
Read-/Command-Pfade vermeiden tiefe Relationship-Serialisierung.
projects/_status ist jetzt der wichtigste Healthcheck-/Readiness-Pfad.
```

---

### 2.5 Generator-Chunk ist bestätigt

Getestete Route:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=5&chunkY=0&chunkZ=0
```

Bestätigte Antwort:

```text
HTTP 200
source = generated
chunkKey = 5:0:0
cellCount = 4096
```

Zusätzlicher Negativtest:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=5&chunkY=0&chunkZ=0&preferSnapshot=false&allowGenerated=false
```

Bestätigte Antwort:

```text
HTTP 404
code = chunk_not_found
message = Chunk '5:0:0' is not materialized and generated fallback is disabled.
```

Bewertung:

```text
Der Generator-Pfad funktioniert.
Der Service kann unmaterialisierte Chunks erzeugen.
allowGenerated=false verhindert korrekt den Generator-Fallback.
```

---

### 2.6 Snapshot-Chunk ist bestätigt

Getestete Route:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0
```

Bestätigte Antwort:

```text
HTTP 200
source = snapshot
chunkKey = 0:0:0
cellCount = 4096
```

Bewertung:

```text
Der Snapshot-Ladepfad funktioniert.
Der Service lädt den materialisierten Chunk aus PostgreSQL.
Events werden nicht replayt.
Der Snapshot ist die Lade-Wahrheit.
```

---

### 2.7 SetBlock ist bestätigt

Getesteter Command:

```json
{
  "type": "SetBlock",
  "userId": "user_test",
  "sessionId": "session_test_20260514231514",
  "position": {
    "x": 6,
    "y": 4,
    "z": 5
  },
  "blockTypeId": "debug_grass"
}
```

Route:

```text
POST /projects/dev-project/worlds/world_spawn/commands
```

Bestätigte Antwort:

```text
ok = true
commandStatus = applied
changed = true
commandType = SetBlock
```

Bestätigte Änderung:

```text
chunkKey = 0:0:0
localX = 6
localY = 4
localZ = 5
beforeCellValue = 0
afterCellValue = 1
beforeBlockTypeId = null
afterBlockTypeId = debug_grass
```

Bestätigte Flags:

```text
dbBacked = true
projectScoped = true
snapshotWritten = true
eventsWritten = true
objectCommand = false
```

Bestätigter Reload:

```text
cell[1350] = 1
```

Index-Berechnung:

```text
x = 6
y = 4
z = 5
chunkSize = 16

index = x + y * 16 + z * 256
index = 6 + 4 * 16 + 5 * 256
index = 1350
```

Bewertung:

```text
SetBlock funktioniert End-to-End.
Der Snapshot wird aktualisiert.
Ein ChunkEvent wird geschrieben.
Ein WorldCommandLog wird geschrieben.
Reload zeigt den gesetzten Block.
```

---

### 2.8 RemoveBlock ist bestätigt

Getesteter Command:

```json
{
  "type": "RemoveBlock",
  "userId": "user_test",
  "sessionId": "session_test_20260514231619",
  "position": {
    "x": 6,
    "y": 4,
    "z": 5
  }
}
```

Route:

```text
POST /projects/dev-project/worlds/world_spawn/commands
```

Bestätigte Antwort:

```text
ok = true
commandStatus = applied
changed = true
commandType = RemoveBlock
```

Bestätigte Änderung:

```text
chunkKey = 0:0:0
localX = 6
localY = 4
localZ = 5
beforeCellValue = 1
afterCellValue = 0
beforeBlockTypeId = debug_grass
afterBlockTypeId = null
```

Bestätigte Flags:

```text
dbBacked = true
projectScoped = true
snapshotWritten = true
eventsWritten = true
objectCommand = false
```

Bestätigter Reload:

```text
cell[1350] = 0
```

Bewertung:

```text
RemoveBlock funktioniert End-to-End.
Der Snapshot wird aktualisiert.
Ein ChunkEvent wird geschrieben.
Ein WorldCommandLog wird geschrieben.
Reload zeigt die entfernte Zelle.
```

---

### 2.9 Dirty-Chunks

Für die bestätigte Testposition:

```text
x = 6
y = 4
z = 5
localX = 6
localY = 4
localZ = 5
```

liegt die Zelle nicht an einer Chunk-Grenze.

Bestätigt:

```text
dirtyChunks = ["0:0:0"]
changedChunks = ["0:0:0"]
```

Bewertung:

```text
Dirty-Chunk-Berechnung ist für normale Innenzellen korrekt.
Grenzzellen sollten zusätzlich separat getestet werden.
```

Noch sinnvoller Zusatztest:

```text
x = 15, y = 4, z = 5
→ rechte X-Grenze
→ dirtyChunks sollte 0:0:0 und 1:0:0 enthalten
```

---

### 2.10 App-Provisioning ist bestätigt

Neue wichtige Routen in `routes/projects.py`:

```text
GET  /projects/preview/by-app/<app_project_public_id>
GET  /projects/by-app/<app_project_public_id>
PUT  /projects/by-app/<app_project_public_id>
POST /projects/by-app/<app_project_public_id>
POST /projects/ensure
```

Bestätigter realer Aufruf aus `vectoplan-app`:

```text
PUT /projects/by-app/prj_979eb0a4d8894086a5b2a74b
→ HTTP 201
```

Bedeutung:

```text
vectoplan-app kann für ein App-Projekt idempotent einen Chunk-Projektgraphen erzeugen.
```

Zielgraph:

```text
Chunk Project
  → Universe
      → WorldInstance world_spawn
```

Wichtig:

```text
chunk_world_id = world_spawn
templateId = flat
providerWorldId = flat
```

Bewertung:

```text
App→Chunk Provisioning funktioniert.
Das ist eine grundentscheidende Erweiterung gegenüber dem alten Stand.
```

---

### 2.11 Editor-Chunk-Batch ist bestätigt

Bestätigter Aufruf aus `vectoplan-editor`:

```text
POST /projects/<chunk_project_id>/worlds/world_spawn/chunks/batch
→ HTTP 200
```

Beispielstruktur:

```text
POST /projects/chk_prj_prj_979eb0a4d8894086a5b2a74b_2653d3872366/worlds/world_spawn/chunks/batch
→ 200
```

Bewertung:

```text
Editor kann über den von App erzeugten Chunk-Projektgraphen Chunks laden.
Der App↔Chunk↔Editor-Pfad ist bestätigt.
```

---

## 3. Frühere kritische Fehlerdiagnose

Nach dem ersten erfolgreichen `SetBlock` trat ursprünglich ein Hänger auf.

Von außen:

```text
Test-NetConnection localhost -Port 5002
→ TcpTestSucceeded = True
```

Aber HTTP-Requests liefen in Timeouts:

```text
curl.exe -i --max-time 10 http://localhost:5002/
→ Operation timed out after 10000 ms with 0 bytes received

curl.exe -i --max-time 10 http://localhost:5002/projects/_status
→ Operation timed out after 10000 ms with 0 bytes received

curl.exe -i --max-time 10 http://localhost:5002/chunks/_status
→ Operation timed out after 10000 ms with 0 bytes received

curl.exe -i --max-time 10 http://localhost:5002/commands/_status
→ Operation timed out after 10000 ms with 0 bytes received
```

Gunicorn-Log:

```text
[CRITICAL] WORKER TIMEOUT
[ERROR] Worker (...) was sent SIGKILL! Perhaps out of memory?
```

Docker-Stats zeigten:

```text
vectoplan-chunk
CPU    ≈ 116 %
RAM    ≈ 14.55 GiB / 15.59 GiB
Status = unhealthy
```

PostgreSQL zeigte wiederholt:

```text
unexpected EOF on client connection with an open transaction
```

Ein früheres DB-Log zeigte zusätzlich:

```text
duplicate key value violates unique constraint "pg_type_typname_nsp_index"
CREATE TABLE projects ...
```

Bewertung:

```text
Der Fehler lag nicht primär am Snapshot-Inhalt.
Der Fehler lag im Startup-/DB-Bootstrap-Pfad.
db.create_all() beziehungsweise Auto-Seed konnten im Runtime-Start beziehungsweise parallel in Gunicorn-Workern laufen.
Dadurch entstanden CREATE TABLE-Races, offene Transaktionen und Worker-Kills.
```

---

## 4. Bestätigte Ursache und Fix-Richtung

Die Diagnose ergab:

```text
Wenn Runtime-Startup-Hooks und Auto-Create/Auto-Seed deaktiviert wurden:
→ App startete stabil.
→ Statusrouten antworteten sofort.
→ RAM blieb kontrollierbar.
```

Daraus folgt:

```text
Der normale Runtime-Start darf keine DB-Mutation ausführen.
db.create_all() und Default-Seeding müssen in einen expliziten DB-Bootstrap-Pfad.
Startup muss read-only sein.
```

Die Architekturentscheidung lautet:

```text
Runtime startup
→ read-only
→ App, Routes, Models, DB-Ping prüfen
→ Bootstrap-Status checken
→ keine Tabellen erzeugen
→ keine Default-Daten seeden
→ keine Reparatur
→ keine Snapshots/Events/ObjectRefs laden

DB bootstrap
→ explizit
→ optional db.create_all()
→ optional fehlende Spalten reparieren
→ optional Default-Seeding
→ optional Seed-Invarianten reparieren
→ advisory-lock-geschützt
→ nicht im normalen Gunicorn-Worker-Start
```

Aktueller bestätigter Zustand:

```text
Runtime startet mit 2 Workern stabil.
Auto-Create/Auto-Seed sind im Runtime-Start aus.
Runtime-Readiness prüft read-only, ob Schema/Seed/world_spawn vorhanden sind.
DB-Bootstrap ist explizit.
```

---

## 5. Aktuelle Bootstrap- und Runtime-Struktur

Aktuelle Bootstrap-/Runtime-Dateien:

```text
services/vectoplan-chunk/entrypoint.sh
services/vectoplan-chunk/config.py
services/vectoplan-chunk/scripts/bootstrap_db.py

services/vectoplan-chunk/src/bootstrap/settings.py
services/vectoplan-chunk/src/bootstrap/runtime_checks.py
services/vectoplan-chunk/src/bootstrap/db_locks.py
services/vectoplan-chunk/src/bootstrap/schema_bootstrap.py
services/vectoplan-chunk/src/bootstrap/default_seed.py
services/vectoplan-chunk/src/bootstrap/db_bootstrap.py
services/vectoplan-chunk/src/bootstrap/startup.py
```

Neu bzw. grundlegend geändert im aktuellen Umbau:

```text
config.py
entrypoint.sh
scripts/bootstrap_db.py
src/bootstrap/settings.py
src/bootstrap/default_seed.py
src/bootstrap/db_bootstrap.py
routes/projects.py
models/world.py
docker-compose.all.yml
```

Bereits zuvor wichtig:

```text
src/world_state/provisioning.py
models/project.py
models/universe.py
models/__init__.py
```

---

### 5.1 `entrypoint.sh`

Rolle:

```text
Container-Entrypoint für Runtime, Python-Dev, DB-Bootstrap, Check-only und Shell.
```

Unterstützte Modi:

```text
gunicorn / runtime
python / wsgi
db-bootstrap / bootstrap / init
check-only / db-check
shell
```

Runtime-Regeln:

```text
VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY=true
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED=false
VECTOPLAN_CHUNK_AUTO_CREATE_ALL=false
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS=false
VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS=false
VECTOPLAN_CHUNK_SEED_DEV_PROJECT=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_CREATE_ALL=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_SEED_DEFAULTS=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_REPAIR_SEED_INVARIANTS=false
VECTOPLAN_CHUNK_BOOTSTRAP_REPAIR_MISSING_COLUMNS=false
```

Runtime-Preflight:

```text
Warte auf PostgreSQL-Socket
→ Python-Prestart-Check
→ DB/Seed-Ready-Check via scripts/bootstrap_db.py --check-only
→ Gunicorn starten
```

Bootstrap-Modus:

```text
VECTOPLAN_CHUNK_STARTUP_MODE=db-bootstrap
VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED=true
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=true
VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY=false
```

Bootstrap-Aufruf:

```text
python ./scripts/bootstrap_db.py \
  --config <config> \
  --create-all \
  --repair-missing-columns \
  --seed \
  --repair-seed-invariants \
  --json
```

Check-only-Modus:

```text
python ./scripts/bootstrap_db.py \
  --check-only \
  --no-create-all \
  --no-seed \
  --no-repair-missing-columns \
  --no-repair-seed-invariants \
  --json
```

Bewertung:

```text
entrypoint.sh ist jetzt ein zentraler Schutz gegen Runtime-Mutationen.
Der normale Service-Start ist read-only.
DB-Bootstrap ist explizit.
```

---

### 5.2 `config.py`

Rolle:

```text
Zentrale Service-Konfiguration.
```

Wichtige aktuelle Regeln:

```text
Konfiguration ist pure Config.
Keine DB-Operationen in config.py.
Keine create_all-Aufrufe.
Kein Seeding.
```

Wichtige Defaults:

```text
DEFAULT_PROJECT_ID = dev-project
DEFAULT_UNIVERSE_ID = dev-universe
DEFAULT_INSTANCE_WORLD_ID = world_spawn
DEFAULT_TEMPLATE_ID = flat
DEFAULT_PROVIDER_ID = flat
DEFAULT_PROVIDER_WORLD_ID = flat
DEFAULT_BLOCK_REGISTRY_ID = debug-blocks
DEFAULT_BLOCK_REGISTRY_VERSION = 1
```

Wichtige Schutzlogik:

```text
Wenn legacy VECTOPLAN_CHUNK_DEFAULT_WORLD_ID=flat gesetzt ist,
darf daraus nicht die konkrete Welt flat werden.
Stattdessen wird world_spawn als konkrete Default-Welt verwendet.
```

Runtime-Defaults:

```text
Runtime ist read-only.
Runtime DB mutations sind deaktiviert.
Auto-Create ist deaktiviert.
Auto-Seed ist deaktiviert.
```

Bootstrap-Defaults:

```text
Bootstrap darf DB mutieren.
Bootstrap darf create_all ausführen.
Bootstrap darf seed_defaults ausführen.
Bootstrap darf missing columns reparieren.
Bootstrap darf seed invariants reparieren.
```

Readiness-Flags:

```text
VECTOPLAN_CHUNK_HEALTHCHECK_PATH
VECTOPLAN_CHUNK_HEALTHCHECK_REQUIRE_OK
VECTOPLAN_CHUNK_SCHEMA_READY_REQUIRED
VECTOPLAN_CHUNK_SEED_READY_REQUIRED
VECTOPLAN_CHUNK_DEFAULT_WORLD_READY_REQUIRED
```

Bewertung:

```text
config.py verhindert, dass flat versehentlich konkrete Runtime-Welt wird.
config.py macht Runtime und Bootstrap eindeutig unterscheidbar.
```

---

### 5.3 `src/bootstrap/settings.py`

Rolle:

```text
Read-only Normalisierung von Config und ENV.
```

Wichtige Modi:

```text
runtime
db-bootstrap
check-only
```

Erkannte Aliase:

```text
runtime / server / gunicorn / flask / web / wsgi / python
db-bootstrap / bootstrap-db / bootstrap / schema-bootstrap / init-db / db-init / init
check-only / db-check / check / database-check / schema-check / readiness-check
```

Wichtige Settings-Klassen:

```text
ServiceIdentitySettings
RuntimeStartupSettings
DatabaseSettings
SchemaBootstrapSettings
SeedBootstrapSettings
WorldDefaultsSettings
BlockDefaultsSettings
ApiSettings
BootstrapSettings
```

Neue wichtige Felder:

```text
runtime.runtime_is_read_only
schema.repair_missing_columns
seed.repair_seed_invariants
world_defaults.default_world_id
world_defaults.instance_world_id
api.healthcheck_require_ok
api.schema_ready_required
api.seed_ready_required
api.default_world_ready_required
```

Wichtige Regel:

```text
VECTOPLAN_CHUNK_DEFAULT_WORLD_ID=flat wird defensiv zu world_spawn normalisiert.
flat bleibt Template/Provider.
```

Bewertung:

```text
settings.py ist zentrale Stelle für Runtime-/Bootstrap-/Check-only-Policy.
```

---

### 5.4 `src/bootstrap/default_seed.py`

Rolle:

```text
Explizites Default-Seeding.
```

Seed-Ziel:

```text
Project(project_id="dev-project")
  → Universe(universe_id="dev-universe")
      → WorldInstance(world_id="world_spawn", provider_world_id="flat")

BlockRegistry(registry_id="debug-blocks", registry_version="1")
  → BlockType(block_type_id="debug_grass")
  → BlockType(block_type_id="debug_dirt")
```

Wichtige aktuelle Regeln:

```text
world_spawn ist konkrete WorldInstance.
flat ist nur template_id/provider_id/provider_world_id.
Seed ist idempotent.
Seed kann partial state reparieren.
Seed nutzt advisory locks, wenn verfügbar.
Seed lädt keine Chunks.
Seed lädt keine Snapshots.
Seed lädt keine Events.
Seed lädt keine Commands.
Seed lädt keine ObjectRefs.
```

Wichtige neue/gehärtete Funktionen:

```text
resolve_world_defaults(...)
resolve_block_defaults(...)
resolve_seed_settings(...)
find_default_project(...)
find_default_universe(...)
find_default_world(...)
is_default_seed_complete(...)
seed_debug_blocks(...)
seed_dev_project_universe_world(...)
build_default_seed_status(...)
run_default_seed(...)
```

Bewertung:

```text
default_seed.py erzeugt nun direkt den richtigen Default-Graphen.
world_spawn entsteht im normalen Seed-Pfad, nicht nur über Fallbacks.
```

---

### 5.5 `src/bootstrap/db_bootstrap.py`

Rolle:

```text
Orchestrator für explizite DB-Initialisierung und Readiness-Status.
```

Ablauf:

```text
optional Pre-Status
→ Schema-Bootstrap
→ Default-Seed
→ Default-World-Invariant prüfen
→ ggf. Default-World-Invariant reparieren
→ optional Post-Status
→ serialisierbares Gesamtergebnis
```

Wichtige neue Konzepte:

```text
schemaReady
seedReady
defaultProjectReady
defaultUniverseReady
defaultWorldReady
seedInvariantRepairExecuted
seedInvariantRepairOk
```

Wichtige Funktionen:

```text
build_default_world_invariant_status(...)
repair_default_world_invariant(...)
run_db_bootstrap(...)
build_db_bootstrap_status(...)
```

Zentrale Invariante:

```text
Project.project_id = dev-project
Universe.universe_id = dev-universe
WorldInstance.world_id = world_spawn
BlockRegistry = debug-blocks / 1
BlockTypes = debug_grass, debug_dirt
```

Bewertung:

```text
db_bootstrap.py macht den Unterschied zwischen Schema-Readiness und Seed-/World-Readiness sichtbar.
Das war entscheidend, um die fehlende world_spawn sauber zu diagnostizieren und zu reparieren.
```

---

### 5.6 `scripts/bootstrap_db.py`

Rolle:

```text
Ausführbarer CLI-/Container-Bootstrap.
```

Result-Version:

```text
bootstrap-db-script-result.v4
```

Wichtige Modi:

```text
--check-only
--create-all
--no-create-all
--repair-missing-columns
--no-repair-missing-columns
--seed
--no-seed
--repair-seed-invariants
--no-repair-seed-invariants
--json
```

Check-only-Regel:

```text
Check-only mutiert niemals die DB.
Check-only setzt Runtime read-only.
Check-only setzt create/seed/repair aus.
```

Bootstrap-Regel:

```text
Bootstrap darf DB mutieren.
Bootstrap kann create_all ausführen.
Bootstrap kann seed ausführen.
Bootstrap kann missing columns reparieren.
Bootstrap kann seed invariants reparieren.
```

Wichtige Ergebnisfelder:

```text
schemaReady
seedReady
defaultProjectReady
defaultUniverseReady
defaultWorldReady
schemaBootstrapExecuted
seedBootstrapExecuted
seedInvariantRepairExecuted
seedInvariantRepairOk
```

Bekannte kleine Ausgabe-Unschärfe:

```text
Einige ältere summary-Felder können im Top-Level noch null sein,
obwohl die echten verschachtelten readiness-Werte korrekt true sind.
Das ist Ausgabe-Normalisierung, kein Runtime-Problem.
```

Bewertung:

```text
bootstrap_db.py ist der zentrale CLI- und Init-Pfad.
Runtime nutzt es nur im check-only Modus.
```

---

### 5.7 `src/bootstrap/startup.py`

Rolle:

```text
Read-only Runtime-Startup-Orchestrator.
```

Macht:

```text
Startup-State initialisieren
Settings-Summary erfassen
Runtime-Checks ausführen
Extension-Status setzen
Startup-Summary bereitstellen
```

Macht nicht:

```text
kein db.create_all()
kein Default-Seeding
keine Chunk-/Snapshot-Ladung
keine Event-/Command-/ObjectRef-Ladung
keine rekursive ORM-Serialisierung
```

Bewertung:

```text
Der normale Gunicorn-Worker-Start ist sicherer.
Der frühere Startup-/DB-Mutationsfehler ist architektonisch entschärft.
```

---

## 6. World- und Default-Graph-Architektur

Die wichtigste Architekturentscheidung bleibt:

```text
Projekt ist nicht dauerhaft gleich Welt.
Projekt ist fachlich ein Universum-Container.
Ein Universum enthält eine oder mehrere Worlds.
Ein neues Projekt startet aktuell mit genau einer Flat-Spawn-World.
```

Technische Umsetzung:

```text
Project
→ Universe
→ WorldInstance
```

Dev-Default:

```text
dev-project
→ dev-universe
→ world_spawn
```

Provider-/Template-Bezug:

```text
world_spawn.template_id = flat
world_spawn.provider_id = flat
world_spawn.provider_world_id = flat
```

Zusätzlich gilt:

```text
Runtime-Start ist nicht DB-Bootstrap.
DB-Bootstrap ist ein eigener expliziter Pfad.
```

Das bedeutet:

```text
Gunicorn-Worker
→ starten Flask-App
→ prüfen read-only
→ dienen HTTP-Routen

DB-Bootstrap
→ läuft separat
→ erzeugt Tabellen
→ repariert fehlende Spalten, falls erlaubt
→ seedet Default-Daten
→ repariert world_spawn-Invariante, falls erlaubt
```

---

## 7. Aktuelle Ordnerstruktur

Aktueller relevanter Zustand:

```text
services/
└── vectoplan-chunk/
    ├── app.py
    ├── wsgi.py
    ├── config.py
    ├── extensions.py
    ├── Dockerfile
    ├── entrypoint.sh
    ├── requirements.txt
    │
    ├── scripts/
    │   └── bootstrap_db.py
    │
    ├── routes/
    │   ├── __init__.py
    │   ├── projects.py
    │   ├── worlds.py
    │   ├── blocks.py
    │   ├── chunks.py
    │   ├── commands.py
    │   ├── world_test.py
    │   └── editor.py
    │
    ├── models/
    │   ├── __init__.py
    │   ├── project.py
    │   ├── universe.py
    │   ├── world.py
    │   ├── block.py
    │   ├── chunk.py
    │   ├── event.py
    │   └── object.py
    │
    └── src/
        ├── bootstrap/
        │   ├── __init__.py
        │   ├── startup.py
        │   ├── settings.py
        │   ├── runtime_checks.py
        │   ├── db_locks.py
        │   ├── schema_bootstrap.py
        │   ├── default_seed.py
        │   └── db_bootstrap.py
        │
        ├── world/
        │   ├── __init__.py
        │   ├── errors.py
        │   ├── models.py
        │   ├── registry.py
        │   ├── loader.py
        │   ├── service.py
        │   ├── serializer.py
        │   ├── discovery.py
        │   │
        │   └── flat/
        │       ├── __init__.py
        │       ├── world.json
        │       ├── validator.py
        │       ├── generator.py
        │       └── provider.py
        │
        └── world_state/
            ├── __init__.py
            ├── errors.py
            ├── models.py
            ├── defaults.py
            ├── resolver.py
            ├── service.py
            ├── bootstrap.py
            ├── provisioning.py
            └── serializer.py
```

Neue oder besonders wichtige Datei gegenüber dem alten Stand:

```text
src/world_state/provisioning.py
```

Diese Datei ist entscheidend für App→Chunk-Provisioning.

---

## 8. PostgreSQL- und Model-Schicht

### 8.1 `extensions.py`

Rolle:

```text
Flask-Erweiterungen und DB-Anbindung.
```

Aktueller Stand:

```text
SQLAlchemy db ist vorhanden.
Database-Initialisierung ist vorbereitet.
Database-Status-/Connection-Check ist vorhanden.
Legacy-Extension-Metadaten bleiben kompatibel.
```

Wichtige Aufgabe:

```text
extensions.db wird von models/* importiert.
```

Wichtiger Restpunkt:

```text
Sicherstellen, dass Sessions nach Requests und Bootstrap-Pfaden sauber entfernt werden.
```

---

### 8.2 `models/__init__.py`

Rolle:

```text
Zentrale Model-Registrierung und Model-Diagnostik.
```

Aufgaben:

```text
Project, Universe, WorldInstance, BlockRegistry, BlockType,
ChunkSnapshot, WorldCommandLog, ChunkEvent,
WorldObjectInstance und WorldObjectChunkRef importieren und registrieren.
```

Bestätigt:

```text
Models werden in /chunks/_status, /commands/_status und /projects/_status erkannt.
missingClasses = []
failedModules = []
```

Zusätzlich wichtig:

```text
Model-Debug-/Schema-Summaries werden für Status- und Bootstrap-Diagnose verwendet.
```

---

### 8.3 `models/project.py`

Rolle:

```text
Persistenter Top-Level-Projektcontainer.
```

Bedeutung:

```text
Project ist der dauerhafte Container für Universen und editierbare Worlds.
```

Aktuelle relevante Felder:

```text
id
project_id
slug
name
description
status
schema_version
revision
default_universe_id
default_world_id
spawn_world_id
external_app_project_id
source_service
external_url
owner_type
owner_id
created_by_user_id
updated_by_user_id
metadata_json
created_at
updated_at
archived_at
deleted_at
```

Wichtig für App-Anbindung:

```text
external_app_project_id
source_service
default_universe_id
default_world_id
spawn_world_id
```

---

### 8.4 `models/universe.py`

Rolle:

```text
Persistente Universe-Ebene innerhalb eines Projects.
```

Bedeutung:

```text
Ein Projekt kann ein oder mehrere Universen enthalten.
Ein Universe besitzt Default-/Spawn-World-Verweise.
```

Aktuelle relevante Felder:

```text
id
project_db_id
universe_id
slug
name
description
status
schema_version
revision
universe_role
universe_scope
default_world_id
spawn_world_id
created_by_user_id
updated_by_user_id
metadata_json
created_at
updated_at
archived_at
deleted_at
```

Wichtig:

```text
default_world_id = world_spawn
spawn_world_id = world_spawn
```

---

### 8.5 `models/world.py`

Rolle:

```text
Persistente konkrete World-Instanz.
```

Bedeutung:

```text
world_spawn ist eine WorldInstance.
flat bleibt providerWorldId/templateId.
```

Aktuelle wichtige Konstanten:

```text
DEFAULT_WORLD_ID = world_spawn
DEFAULT_TEMPLATE_ID = flat
DEFAULT_PROVIDER_ID = flat
DEFAULT_PROVIDER_WORLD_ID = flat
DEFAULT_BLOCK_REGISTRY_ID = debug-blocks
DEFAULT_BLOCK_REGISTRY_VERSION = 1
```

Wichtige neue/gehärtete Funktionen:

```text
is_provider_like_world_id(...)
normalize_concrete_world_id(...)
WorldInstance.create(...)
WorldInstance.create_flat_spawn(...)
WorldInstance.ensure_bootstrap_defaults(...)
WorldInstance.from_create_payload(...)
WorldInstance.get_validation_errors(...)
WorldInstance.to_dict(...)
```

Wichtige Regel:

```text
WorldInstance.create(...) normalisiert konkrete world_id.
Wenn flat als konkrete world_id hereinkommt, wird daraus world_spawn.
```

`create_flat_spawn(...)`:

```text
erstellt konkrete WorldInstance world_spawn
setzt template_id/provider_id/provider_world_id auf flat
akzeptiert project/universe oder project_db_id/universe_db_id
füllt Bootstrap-Metadaten
```

Bewertung:

```text
models/world.py schützt jetzt auf Model-Ebene gegen den alten flat/world_spawn-Fehler.
```

---

### 8.6 `models/block.py`

Rolle:

```text
Persistente BlockRegistry und BlockType-Definitionen.
```

Aktuelle Blocktypen:

```text
debug_grass
debug_dirt
```

Aktuelle Registry:

```text
registryId = debug-blocks
registryVersion = 1
```

Wichtige Zellregel:

```text
cellValue = 0
→ Air

cellValue = paletteIndex + 1
→ Block
```

---

### 8.7 `models/chunk.py`

Rolle:

```text
Persistente ChunkSnapshot-Lade-Wahrheit.
```

Bedeutung:

```text
Sobald ein Chunk verändert wird, wird er materialisiert.
Danach lädt der normale Chunk-Load diesen Snapshot.
```

Bestätigt:

```text
SetBlock erzeugt oder aktualisiert ChunkSnapshot.
RemoveBlock aktualisiert ChunkSnapshot.
Snapshot-Reload funktioniert.
chunkKey = 0:0:0
cellCount = 4096
```

Aktueller bestätigter Ablauf:

```text
SetBlock
→ Snapshot revision steigt
→ Reload zeigt cellValue = 1

RemoveBlock
→ Snapshot revision steigt
→ Reload zeigt cellValue = 0
```

---

### 8.8 `models/event.py`

Rolle:

```text
Persistente Command- und Event-Historie.
```

Enthalten:

```text
WorldCommandLog
ChunkEvent
```

Bestätigt:

```text
SetBlock schreibt WorldCommandLog.
SetBlock schreibt ChunkEvent.
RemoveBlock schreibt WorldCommandLog.
RemoveBlock schreibt ChunkEvent.
```

Aktuelle Einordnung:

```text
event.py wurde nach dem stabilen Command-Test nicht zusätzlich gehärtet.
Der bestehende Stand funktioniert für den ersten bestätigten Slice.
Eine spätere Härtung ist optional, aber für den ersten Stand nicht zwingend.
```

---

### 8.9 `models/object.py`

Rolle:

```text
Persistente Vorbereitung für Mehrblockobjekte.
```

Enthalten:

```text
WorldObjectInstance
WorldObjectChunkRef
```

Aktueller Stand:

```text
Struktur ist vorbereitet.
PlaceObject/RemoveObject ist in routes/commands.py vorbereitet.
Noch nicht stabil manuell bestätigt.
```

---

## 9. World-State-Schicht

### 9.1 `src/world_state/service.py`

Rolle:

```text
Projektgescopte Service-Fassade über PostgreSQL und src.world.
```

Aktuelle Aufgaben:

```text
Project aus PostgreSQL laden
Universe aus PostgreSQL laden
WorldInstance aus PostgreSQL laden
Blocks aus PostgreSQL laden
ChunkSnapshot prüfen
falls Snapshot existiert: Snapshot laden
falls kein Snapshot existiert: Provider/Generator nutzen
Batch-Chunks laden
Bootstrap-Kontext erzeugen
```

Aktueller Stand:

```text
Projektgescopter Read-Pfad ist durch routes/chunks.py bestätigt.
Snapshot- und Generator-Pfad funktionieren über projektgescopte Routen.
```

---

### 9.2 `src/world_state/provisioning.py`

Rolle:

```text
Idempotentes Provisioning von Chunk-Projektgraphen für vectoplan-app.
```

Diese Datei ist neu bzw. im aktuellen Stand grundentscheidend.

Aufgaben:

```text
App-Projekt-ID entgegennehmen
deterministische Chunk-IDs berechnen
Chunk Project erzeugen oder finden
Universe erzeugen oder finden
WorldInstance world_spawn erzeugen oder finden
Projekt-/Universe-/World-Referenzen zurückgeben
Route-Hints zurückgeben
serialisierbares Provisioning-Ergebnis erzeugen
```

Genutzte Routen in `routes/projects.py`:

```text
GET  /projects/preview/by-app/<app_project_public_id>
GET  /projects/by-app/<app_project_public_id>
PUT  /projects/by-app/<app_project_public_id>
POST /projects/by-app/<app_project_public_id>
POST /projects/ensure
```

Wichtige Regel:

```text
App-Projekte bekommen im Chunk-Service eine konkrete WorldInstance world_spawn.
flat bleibt Template-/Provider-Welt.
```

Bewertung:

```text
provisioning.py ist der verbindende Baustein zwischen vectoplan-app und vectoplan-chunk.
Der Pfad ist im realen Test bestätigt.
```

---

### 9.3 `src/world_state/bootstrap.py`

Rolle:

```text
Erzeugt Projekt-Bootstrap aus DB-backed Service.
```

Restpunkt:

```text
/projects/<project_id>/bootstrap kann weiterhin separat geprüft werden.
Für den bestätigten App↔Chunk↔Editor-Slice ist projects/by-app wichtiger.
```

---

### 9.4 `src/world_state/serializer.py`

Rolle:

```text
Serialisiert DB-backed und ältere World-State-Objekte.
```

Wichtige Regel:

```text
Serializer darf keine rekursiven SQLAlchemy-Objekte serialisieren.
Snapshot-ChunkResponse muss auf sichere Runtime-Daten begrenzt bleiben.
```

Nicht serialisieren:

```text
SQLAlchemy-Beziehungen
Project-Objekte tief
Universe-Objekte tief
WorldInstance-Objekte tief
CommandLogs
Events
ChunkSnapshot selbst rekursiv
WorldObjectChunkRefs unnötig
```

---

## 10. Routes

### 10.1 `routes/projects.py`

Wichtige Routen:

```text
GET  /projects
POST /projects
GET  /projects/<project_id>
PATCH /projects/<project_id>
DELETE /projects/<project_id>
GET  /projects/<project_id>/bootstrap
GET  /projects/bootstrap
GET  /projects/_status
POST /projects/_cache/reset

GET  /projects/preview/by-app/<app_project_public_id>
GET  /projects/by-app/<app_project_public_id>
PUT  /projects/by-app/<app_project_public_id>
POST /projects/by-app/<app_project_public_id>
POST /projects/ensure
```

Aktuell wichtig:

```text
/projects/_status ist Healthcheck- und Readiness-Pfad.
Diese Route muss flach und billig bleiben.
```

Neue Readiness-Ausgabe:

```text
serviceReady
schemaReady
seedReady
defaultProjectReady
defaultUniverseReady
defaultWorldReady
```

Neue Provisioning-Routen:

```text
/projects/by-app/<app_project_public_id>
```

für App-Projektintegration.

Wichtige Regel:

```text
Wenn in Routen flat als konkrete world_id auftaucht,
muss auf world_spawn normalisiert werden.
```

Bewertung:

```text
routes/projects.py ist nun nicht nur Projekt-CRUD,
sondern auch App-Provisioning- und Readiness-Adapter.
```

---

### 10.2 `routes/worlds.py`

Wichtige Routen:

```text
GET /projects/<project_id>/worlds
GET /projects/<project_id>/worlds/<world_id>
GET /worlds
GET /worlds/<world_id>
GET /worlds/_status
```

Aktuell wichtig:

```text
/worlds/_status darf keine Worlds tief serialisieren.
```

---

### 10.3 `routes/blocks.py`

Wichtige Routen:

```text
GET /projects/<project_id>/worlds/<world_id>/blocks
GET /projects/<project_id>/blocks
GET /blocks
GET /blocks/_status
```

Bestätigte Debug-Blöcke:

```text
debug_grass
debug_dirt
```

---

### 10.4 `routes/chunks.py`

Wichtige Routen:

```text
GET  /projects/<project_id>/worlds/<world_id>/chunks
POST /projects/<project_id>/worlds/<world_id>/chunks/batch

GET  /projects/<project_id>/chunks
POST /projects/<project_id>/chunks/batch

GET  /chunks
POST /chunks/batch

GET  /chunks/_status
```

Aktueller Stand:

```text
moduleVersion = 0.3.0
Generator-Pfad bestätigt
Snapshot-Pfad bestätigt
allowGenerated=false Negativtest bestätigt
relationshipLoadingDisabledInReadPath = true
```

Bestätigt:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=5&chunkY=0&chunkZ=0
→ source = generated

GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0
→ source = snapshot
```

Bestätigt aus Editor-Kontext:

```text
POST /projects/<chunk_project_id>/worlds/world_spawn/chunks/batch
→ HTTP 200
```

---

### 10.5 `routes/commands.py`

Wichtige Routen:

```text
POST /projects/<project_id>/worlds/<world_id>/commands
POST /projects/<project_id>/commands
POST /commands
GET  /commands/_status
```

Implementierte Commands:

```text
SetBlock
RemoveBlock
ReplaceBlock
PlaceObject
RemoveObject
```

Aktueller Stand:

```text
moduleVersion = 0.2.0
SetBlock bestätigt
RemoveBlock bestätigt
relationshipLoadingDisabledInCommandPath = true
```

Bestätigt:

```text
SetBlock
→ Snapshot geschrieben/aktualisiert
→ ChunkEvent geschrieben
→ WorldCommandLog geschrieben
→ Reload zeigt Block

RemoveBlock
→ Snapshot aktualisiert
→ ChunkEvent geschrieben
→ WorldCommandLog geschrieben
→ Reload zeigt Air
```

Noch nicht bestätigt:

```text
ReplaceBlock
PlaceObject
RemoveObject
```

---

## 11. App-, Config-, WSGI-, Docker-Stand

### 11.1 `config.py`

Aktueller Stand:

```text
Chunk-spezifische Config vorhanden.
PostgreSQL-/SQLAlchemy-Config vorhanden.
World-State Defaults vorhanden.
Mehrblockobjekt-Grenzen vorhanden.
Runtime-/Bootstrap-Modi vorhanden.
world_spawn/flat-Trennung vorhanden.
Readiness-Flags vorhanden.
Legacy VECTOPLAN_EDITOR_* Kompatibilität bleibt vorhanden.
```

Empfohlene Runtime-Defaults:

```text
VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY=true
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=false
VECTOPLAN_CHUNK_AUTO_CREATE_ALL=false
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS=false
VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS=false
VECTOPLAN_CHUNK_SEED_DEV_PROJECT=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED=false
```

Expliziter DB-Bootstrap läuft stattdessen über:

```text
scripts/bootstrap_db.py
src/bootstrap/db_bootstrap.py
entrypoint.sh db-bootstrap
```

---

### 11.2 `app.py`

Aktueller Stand:

```text
Flask-App-Factory ist chunk-spezifisch.
routes.register_blueprints(app) registriert alle Routen.
Root-Probe / ist vorhanden.
Startup-Hooks werden über src.bootstrap.startup geladen.
```

Bewertung:

```text
Startup ist read-only.
Damit ist app.py als Gunicorn-App-Factory stabiler.
```

---

### 11.3 `wsgi.py`

Aktueller Stand:

```text
VECTOPLAN_CHUNK_CONFIG wird priorisiert.
app und application werden exportiert.
Gecachte WSGI-App-Erzeugung vorhanden.
```

---

### 11.4 `Dockerfile`

Aktueller Stand:

```text
Service ist auf vectoplan-chunk ausgerichtet.
Python 3.12 slim wird verwendet.
requirements.txt wird installiert.
entrypoint.sh wird genutzt.
Healthcheck zeigt auf VECTOPLAN_CHUNK_HEALTHCHECK_PATH.
```

---

### 11.5 `entrypoint.sh`

Aktueller Stand:

```text
Startet vectoplan-chunk.
Wartet auf PostgreSQL-Socket.
Prüft zentrale Dateien.
Setzt Runtime read-only Guards.
Führt DB/Seed Ready Check im check-only Modus aus.
Startet Gunicorn.
Kann db-bootstrap explizit ausführen.
Kann check-only explizit ausführen.
```

Wichtig:

```text
Runtime-Start ist nicht DB-Bootstrap.
```

---

### 11.6 `docker-compose.all.yml`

Aktueller Stand:

```text
vectoplan-chunk-db ist vorhanden.
vectoplan-chunk hängt von vectoplan-chunk-db health ab.
PostgreSQL-Volume vectoplan-chunk-postgres-data ist vorhanden.
Chunk-Service bekommt DB-ENV-Werte.
Runtime-Flags sind read-only.
Init-/Bootstrap-Flags sind mutierend.
```

Bewertung:

```text
Der Compose-Stand ist jetzt auf Runtime/Init-Trennung ausgerichtet.
```

---

Fortsetzung in Teil 2:

```text
12. Aktuelle API-JSON-Struktur
13. Aktuelle Invarianten
14. Nicht mehr korrekt aus älteren Ständen
15. Bewusst noch nicht stabil oder nicht bestätigt
16. Bekannte technische Restpunkte
17. Manuelle Testliste
18. Block setzen und entfernen – vollständiger PowerShell-Test
19. DB-Prüfung nach Block-Test
20. App↔Chunk↔Editor Smoke-Test
21. Optionaler Grenztest für Dirty-Chunks
22. Nächster sinnvoller Schritt
23. Aktueller Gesamtbefund
```
Teil 2 setzt die Chunk-IST-Datei ab Abschnitt 12 fort und ergänzt die aktuellen Bootstrap-, Readiness-, App-Provisioning- und Testpfade. Grundlage bleibt deine bestehende Chunk-IST-Datei. 

## 12. Aktuelle API-JSON-Struktur

### 12.1 Projekt-/Bootstrap-Status

Wichtigster Health-/Readiness-Pfad:

```text
GET /projects/_status
```

Aktueller Zweck:

```text
Route-/Service-Diagnostik
DB-Status
Model-Status
Schema-Readiness
Seed-Readiness
Default-Projekt-/Universe-/World-Readiness
App-Provisioning-Verfügbarkeit
```

Erwartete Kernfelder:

```json
{
  "ok": true,
  "status": "ready",
  "serviceReady": true,
  "schemaReady": true,
  "seedReady": true,
  "defaultProjectReady": true,
  "defaultUniverseReady": true,
  "defaultWorldReady": true,
  "defaultIds": {
    "projectId": "dev-project",
    "universeId": "dev-universe",
    "worldId": "world_spawn",
    "templateId": "flat",
    "providerId": "flat",
    "providerWorldId": "flat"
  }
}
```

Wichtig:

```text
defaultWorldReady = true bedeutet:
WorldInstance(world_id="world_spawn") existiert.

templateId/providerWorldId = flat bedeutet:
flat ist nur Provider-/Template-Referenz.
```

Nicht verwechseln:

```text
worldId         = world_spawn
providerWorldId = flat
```

---

### 12.2 Projekt-Bootstrap

Route:

```text
GET /projects/dev-project/bootstrap
```

Soll liefern:

```json
{
  "ok": true,
  "projectId": "dev-project",
  "universeId": "dev-universe",
  "defaultWorldId": "world_spawn",
  "spawnWorldId": "world_spawn",
  "chunkWorldId": "world_spawn",
  "spawnWorld": {
    "worldId": "world_spawn",
    "templateId": "flat",
    "providerId": "flat",
    "providerWorldId": "flat"
  },
  "routeHints": {
    "projectBootstrap": "/projects/dev-project/bootstrap",
    "project": "/projects/dev-project",
    "worlds": "/projects/dev-project/worlds",
    "world": "/projects/dev-project/worlds/world_spawn",
    "blocks": "/projects/dev-project/worlds/world_spawn/blocks",
    "chunk": "/projects/dev-project/worlds/world_spawn/chunks",
    "chunks": "/projects/dev-project/worlds/world_spawn/chunks",
    "chunksBatch": "/projects/dev-project/worlds/world_spawn/chunks/batch",
    "commands": "/projects/dev-project/worlds/world_spawn/commands"
  }
}
```

Wichtig:

```text
Editor und App dürfen gegen world_spawn arbeiten.
flat darf nicht als konkrete World-Route verwendet werden.
```

---

### 12.3 App-Provisioning Preview

Route:

```text
GET /projects/preview/by-app/<app_project_public_id>
```

Zweck:

```text
deterministische Chunk-IDs berechnen,
ohne DB-Zeilen zu schreiben.
```

Beispiel:

```text
GET /projects/preview/by-app/prj_979eb0a4d8894086a5b2a74b
```

Erwartete Struktur:

```json
{
  "ok": true,
  "preview": {
    "appProjectPublicId": "prj_979eb0a4d8894086a5b2a74b",
    "chunkProjectId": "chk_prj_prj_979eb0a4d8894086a5b2a74b_...",
    "chunkUniverseId": "dev-universe",
    "chunkWorldId": "world_spawn"
  },
  "willCreateDatabaseRows": false
}
```

---

### 12.4 App-Provisioning sicherstellen

Route:

```text
PUT /projects/by-app/<app_project_public_id>
POST /projects/by-app/<app_project_public_id>
POST /projects/ensure
```

Zweck:

```text
App-Projekt idempotent in Chunk-Projektgraph übersetzen.
```

Beispiel:

```text
PUT /projects/by-app/prj_979eb0a4d8894086a5b2a74b
```

Erwartete Wirkung:

```text
Project wird erstellt oder gefunden.
Universe wird erstellt oder gefunden.
WorldInstance world_spawn wird erstellt oder gefunden.
Antwort enthält Chunk-Referenzen und Route-Hints.
```

Erwartete Kernfelder:

```json
{
  "ok": true,
  "created": true,
  "appProjectPublicId": "prj_979eb0a4d8894086a5b2a74b",
  "chunkProjectId": "chk_prj_prj_979eb0a4d8894086a5b2a74b_2653d3872366",
  "chunkUniverseId": "dev-universe",
  "chunkWorldId": "world_spawn",
  "projectId": "chk_prj_prj_979eb0a4d8894086a5b2a74b_2653d3872366",
  "universeId": "dev-universe",
  "worldId": "world_spawn",
  "routeHints": {
    "blocks": "/projects/chk_prj_prj_979eb0a4d8894086a5b2a74b_2653d3872366/worlds/world_spawn/blocks",
    "chunks": "/projects/chk_prj_prj_979eb0a4d8894086a5b2a74b_2653d3872366/worlds/world_spawn/chunks",
    "chunksBatch": "/projects/chk_prj_prj_979eb0a4d8894086a5b2a74b_2653d3872366/worlds/world_spawn/chunks/batch",
    "commands": "/projects/chk_prj_prj_979eb0a4d8894086a5b2a74b_2653d3872366/worlds/world_spawn/commands"
  }
}
```

HTTP-Status:

```text
201 = neu erstellt
200 = bereits vorhanden / idempotent zurückgegeben
```

---

### 12.5 Blocks

Route:

```text
GET /projects/dev-project/worlds/world_spawn/blocks
```

Soll liefern:

```text
Air = cellValue 0
debug_grass = cellValue 1
debug_dirt = cellValue 2
```

BlockRegistry:

```text
registryId      = debug-blocks
registryVersion = 1
```

---

### 12.6 Chunk ohne Snapshot

Route:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=5&chunkY=0&chunkZ=0
```

Bestätigt:

```text
HTTP 200
source = generated
chunkKey = 5:0:0
cellCount = 4096
```

Bedeutung:

```text
Chunk war noch nicht materialisiert.
Service nutzt Generator-Fallback.
```

---

### 12.7 Chunk ohne Snapshot und ohne Generator-Fallback

Route:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=5&chunkY=0&chunkZ=0&preferSnapshot=false&allowGenerated=false
```

Bestätigt:

```text
HTTP 404
code = chunk_not_found
```

Bedeutung:

```text
Service verhält sich korrekt:
kein Snapshot vorhanden und Generator-Fallback explizit deaktiviert.
```

---

### 12.8 Chunk mit Snapshot

Route:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0
```

Bestätigt:

```text
HTTP 200
source = snapshot
snapshotBacked = true
snapshotId vorhanden
cellCount = 4096
```

Beispiel bestätigte Zelle aus altem Test:

```text
cells[273] = 1
```

Index-Berechnung:

```text
x = 1
y = 1
z = 1
chunkSize = 16

index = x + y * 16 + z * 256
index = 1 + 1 * 16 + 1 * 256
index = 273
```

Beispiel bestätigte Set-/Remove-Zelle aus aktuellem Test:

```text
x = 6
y = 4
z = 5
chunkSize = 16

index = x + y * 16 + z * 256
index = 6 + 4 * 16 + 5 * 256
index = 1350
```

---

### 12.9 Batch-Chunks

Route:

```text
POST /projects/<project_id>/worlds/<world_id>/chunks/batch
```

Bestätigt aus Editor-Kontext:

```text
POST /projects/chk_prj_prj_979eb0a4d8894086a5b2a74b_2653d3872366/worlds/world_spawn/chunks/batch
→ HTTP 200
```

Aktuelle Bedeutung:

```text
Editor kann über App-Projekt-Provisioning erzeugte Chunk-Projekte laden.
```

Noch separat zu testen:

```text
Batch mit Mix aus:
- Snapshot-Chunk
- Generated-Chunk
- fehlendem Chunk bei allowGenerated=false
```

---

### 12.10 Command-Antwort

Route:

```text
POST /projects/dev-project/worlds/world_spawn/commands
```

Bestätigte SetBlock-Antwort enthält:

```text
ok = true
commandType = SetBlock
commandStatus = applied
changed = true
eventIds = [...]
changedChunks = ["0:0:0"]
dirtyChunks = ["0:0:0"]
snapshotIds = [...]
chunkVersions["0:0:0"] = chunk_rev_...
flags.snapshotWritten = true
flags.eventsWritten = true
```

Bestätigte RemoveBlock-Antwort enthält:

```text
ok = true
commandType = RemoveBlock
commandStatus = applied
changed = true
eventIds = [...]
changedChunks = ["0:0:0"]
dirtyChunks = ["0:0:0"]
snapshotIds = [...]
chunkVersions["0:0:0"] = chunk_rev_...
flags.snapshotWritten = true
flags.eventsWritten = true
```

---

## 13. Aktuelle Invarianten

Diese Regeln gelten aktuell:

```text
1. /src/world ist die neutrale Provider-/Generator-Schicht.
2. /src/world/flat ist die erste konkrete Provider-/Template-Welt.
3. world.json beschreibt die Flat-Provider-Welt.
4. flat ist Provider-/Template-Welt.
5. world_spawn ist konkrete Projekt-World.
6. dev-project ist aktuelles Dev-Projekt.
7. dev-universe ist aktuelles Dev-Universum.
8. Ein Projekt ist ein Universum-Container.
9. Ein Universum enthält eine oder mehrere Worlds.
10. Produktive App-/Editor-Routen verwenden world_spawn.
11. Provider-/Debug-Routen dürfen weiterhin flat als Provider verwenden.
12. flat darf nicht als konkrete WorldInstance world_id verwendet werden.
13. cellValue = 0 bedeutet Air.
14. cellValue = paletteIndex + 1 bedeutet Block.
15. debug_grass hat cellValue = 1.
16. debug_dirt hat cellValue = 2.
17. chunkSize = 16.
18. Ein Chunk hat 4096 Zellen.
19. Zellindex-Reihenfolge ist x-fastest-y-then-z.
20. Negative Koordinaten müssen Floor-Division nutzen.
21. Unberührte Chunks werden generiert.
22. Bearbeitete Chunks werden als ChunkSnapshot gespeichert.
23. ChunkSnapshot ist Lade-Wahrheit.
24. ChunkEvent ist historische Wahrheit.
25. Events sind nicht der normale Ladepfad.
26. SetBlock schreibt Snapshot und Event.
27. RemoveBlock schreibt Snapshot und Event.
28. Dirty-Chunks werden für SetBlock und RemoveBlock zurückgegeben.
29. ChunkCommands ändern nicht direkt den Editor-Core.
30. Der Editor rendert und sendet Commands, besitzt aber nicht die Wahrheit.
31. PostgreSQL ist der primäre Speicher für Persistenz.
32. Mehrblockobjekte sind modellseitig vorbereitet.
33. Runtime-Startup darf keine DB-Mutation ausführen.
34. db.create_all() gehört in den expliziten DB-Bootstrap.
35. Default-Seeding gehört in den expliziten DB-Bootstrap.
36. Missing-Column-Repair gehört in den expliziten DB-Bootstrap.
37. Seed-Invariant-Repair gehört in den expliziten DB-Bootstrap.
38. Statusrouten müssen billig und flach bleiben.
39. Startup darf keine Snapshots, Events, Commands oder ObjectRefs laden.
40. routes/chunks.py vermeidet tiefe Relationship-Ladung im Read-Pfad.
41. routes/commands.py vermeidet tiefe Relationship-Ladung im Command-Pfad.
42. routes/projects.py darf Status/Readiness flach prüfen, aber keine Runtime-Reparatur ausführen.
43. Ein aktiver Snapshot pro Chunk wird aktualisiert, nicht pro Änderung neu angelegt.
44. ChunkEvents bleiben append-only.
45. App-Provisioning ist idempotent.
46. App-Provisioning erzeugt Chunk Project + Universe + world_spawn.
47. vectoplan-app speichert nur Chunk-Referenzen, nicht Chunk-Inhalte.
48. vectoplan-editor lädt Chunks aus vectoplan-chunk, nicht aus vectoplan-app.
```

---

## 14. Nicht mehr korrekt aus älteren Ständen

Diese älteren Aussagen sind nicht mehr korrekt:

```text
PostgreSQL wird für den Chunk-Service noch nicht genutzt.
Es gibt noch keine Snapshots.
Es gibt noch keine Events.
Es gibt noch keine Commands.
Die persistente editierbare Welt existiert noch nicht.
Startup-Hooks müssen Auto-Create/Auto-Seed ausführen.
db.create_all() gehört in startup.py.
RemoveBlock ist noch nicht bestätigt.
Snapshot-Reload ist noch nicht bestätigt.
Generator-Chunk-Load hängt.
Snapshot-Chunk-Load hängt.
App-Projekte sind noch nicht an Chunk-Projekte angebunden.
Editor lädt noch keine Chunk-Batches aus App-Projektkontext.
flat ist die konkrete Runtime-Welt.
```

Korrektur:

```text
PostgreSQL ist angebunden.
Modelle sind erstellt.
ChunkSnapshot existiert.
ChunkEvent existiert.
WorldCommandLog existiert.
SetBlock funktioniert End-to-End.
RemoveBlock funktioniert End-to-End.
Generator-Chunk-Load funktioniert.
Snapshot-Chunk-Load funktioniert.
Reload nach SetBlock zeigt Änderung.
Reload nach RemoveBlock zeigt Entfernung.
Runtime-Startup ist read-only.
db.create_all() und Default-Seeding sind in explizite Bootstrap-Module ausgelagert.
App→Chunk-Provisioning funktioniert.
Editor→Chunk chunks/batch funktioniert.
world_spawn ist konkrete Runtime-Welt.
flat ist Template-/Provider-Welt.
```

---

## 15. Bewusst noch nicht stabil oder nicht bestätigt

Noch nicht abschließend bestätigt:

```text
ReplaceBlock End-to-End
PlaceObject End-to-End
RemoveObject End-to-End
Chunk-Batch mit Snapshot-/Generator-Mix
Projekt ändern/löschen über Chunk-Projekt-API
Editor-Commands SetBlock/RemoveBlock aus echter UI
Dirty-Chunks an Chunk-Grenzen
Negative Koordinaten End-to-End
Mehrere gleichzeitige Commands gegen denselben Chunk
Mehrere App-Projekte parallel mit je eigenem Chunk-Projektgraph
```

Noch nicht gebaut oder nicht final:

```text
Alembic-Migrationen
Repository-Schicht
Produktionsreifes DB-Migrationskonzept
Multi-User-Konflikte
Optimistic concurrency
Core-Mapping
Library-Anbindung
Geodaten
Kugelwelt
Kompression großer Snapshots
dauerhaft finaler vectoplan-chunk-init Compose-Service
dauerhaft finaler DB-Migration-/Repair-Prozess
```

---

## 16. Bekannte technische Restpunkte

### 16.1 Runtime-Defaults final beobachten

Aktuell wichtig:

```text
Runtime ist read-only.
entrypoint.sh setzt Schutzvariablen.
config.py schützt zusätzlich.
settings.py schützt zusätzlich.
```

Empfohlen:

```text
VECTOPLAN_CHUNK_RUNTIME_IS_READ_ONLY=true
VECTOPLAN_CHUNK_AUTO_CREATE_ALL=false
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS=false
VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS=false
VECTOPLAN_CHUNK_SEED_DEV_PROJECT=false
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=false
VECTOPLAN_CHUNK_DB_BOOTSTRAP_ENABLED=false
```

Weiterhin prüfen:

```text
Keine anderen App-Factory- oder Startup-Hooks dürfen create_all/seed ausführen.
```

---

### 16.2 Init-Pfad in Docker finalisieren

Aktuell vorhanden:

```text
Bootstrap-/Init-Logik ist in Compose/Entrypoint/Scripts vorbereitet.
```

Langfristig sinnvoll:

```text
vectoplan-chunk-init
```

Ziel:

```text
DB startet
→ Init-Service führt scripts/bootstrap_db.py aus
→ Runtime-Service startet read-only
```

Runtime darf danach nur prüfen:

```text
schemaReady
seedReady
defaultWorldReady
```

---

### 16.3 Statusrouten flach halten

Statusrouten dürfen nicht laden:

```text
Snapshots mit Inhalt
Events mit Payloads
CommandLogs mit Payloads
ObjectRefs mit tiefen Beziehungen
Chunk-Inhalte
tiefe Project-/Universe-/World-Beziehungen
```

Statusrouten sollen nur melden:

```text
Service verfügbar
Route verfügbar
optional DB-Ping
Startup-Summary
Settings-Summary
Counts
Model-Registrierung
Schema-Readiness
Seed-Readiness
Default-World-Readiness
```

---

### 16.4 Check-only-Ausgabe normalisieren

Bekannte kleine Unschärfe:

```text
scripts/bootstrap_db.py --check-only --json
```

liefert in verschachtelten Bereichen korrekte Werte:

```text
bootstrap.defaultProjectReady = true
bootstrap.defaultUniverseReady = true
bootstrap.defaultWorldReady = true
bootstrap.schemaReady = true
bootstrap.seedReady = true
```

Einige ältere Top-Level- oder Summary-Felder können aber noch `null` sein:

```text
summary.defaultProjectReady = null
summary.schemaReady = null
summary.seedReady = null
```

Einordnung:

```text
kein Runtime-Problem
kein Seed-Problem
nur Ausgabe-/Summary-Normalisierung
```

Später sinnvoll:

```text
build_db_bootstrap_summary() und script summary flattening vereinheitlichen.
```

---

### 16.5 Cell-Payload ist weiterhin unkomprimiert

Aktuell:

```text
cells: [4096 Integer]
```

Das ist für Debug und erste Editor-Anbindung gut.

Später möglich:

```text
RLE
Binary Payload
content_binary
Kompression
Streaming
Delta-Payloads
```

---

### 16.6 Event-Modell funktioniert, ist aber noch nicht zusätzlich gehärtet

Aktuell:

```text
models/event.py funktioniert für SetBlock und RemoveBlock.
```

Optional später:

```text
Relationship-Lazy-Strategie in event.py härten
Serializer rekursionssicherer machen
Event-/Command-Validation ausbauen
Command-Replay optional vorbereiten
```

Für den ersten stabilen Stand ist das aktuell nicht zwingend.

---

### 16.7 App-Provisioning weiter absichern

Aktuell bestätigt:

```text
PUT /projects/by-app/<app_project_public_id> funktioniert.
```

Noch sinnvoll:

```text
Mehrere App-Projekte nacheinander testen.
Idempotenz wiederholt testen.
GET /projects/by-app/<id> nach PUT testen.
POST /projects/ensure testen.
Preview-Route testen.
Service-Link-Rückgabe gegen App-Erwartung abgleichen.
```

---

## 17. Manuelle Testliste

Nach einem Restart zuerst:

```powershell
$BASE = "http://127.0.0.1:5002"
```

### 17.1 Service neu bauen/starten

```powershell
docker compose up -d --build vectoplan-chunk
```

Optional Logs prüfen:

```powershell
docker logs --tail 160 vectoplan-chunk
```

Erwartung:

```text
Runtime read-only: true
Runtime DB mutations allowed: false
DB/Seed-Ready-Check erfolgreich
Default world: world_spawn
Default template: flat
Provider world: flat
Gunicorn startet
```

---

### 17.2 Service erreichbar

```powershell
curl.exe -sS --max-time 10 -o NUL -w "root status=%{http_code} bytes=%{size_download} time=%{time_total}`n" "$($BASE)/"
curl.exe -sS --max-time 10 -o NUL -w "projects status=%{http_code} bytes=%{size_download} time=%{time_total}`n" "$($BASE)/projects/_status"
curl.exe -sS --max-time 10 -o NUL -w "worlds status=%{http_code} bytes=%{size_download} time=%{time_total}`n" "$($BASE)/worlds/_status"
curl.exe -sS --max-time 10 -o NUL -w "blocks status=%{http_code} bytes=%{size_download} time=%{time_total}`n" "$($BASE)/blocks/_status"
curl.exe -sS --max-time 10 -o NUL -w "chunks status=%{http_code} bytes=%{size_download} time=%{time_total}`n" "$($BASE)/chunks/_status"
curl.exe -sS --max-time 10 -o NUL -w "commands status=%{http_code} bytes=%{size_download} time=%{time_total}`n" "$($BASE)/commands/_status"
```

Erwartung:

```text
alle status=200
keine Timeouts
```

---

### 17.3 Projects-Status JSON prüfen

```powershell
curl.exe -sS --max-time 20 "$($BASE)/projects/_status" | ConvertFrom-Json | ConvertTo-Json -Depth 40
```

Erwartung:

```text
ok = true
status = ready
schemaReady = true
seedReady = true
defaultProjectReady = true
defaultUniverseReady = true
defaultWorldReady = true
defaultIds.worldId = world_spawn
defaultIds.templateId = flat
defaultIds.providerWorldId = flat
```

---

### 17.4 Chunks-Status JSON prüfen

```powershell
curl.exe -sS --max-time 10 "$($BASE)/chunks/_status" | ConvertFrom-Json | ConvertTo-Json -Depth 20
```

Erwartung:

```text
ok = true
route.moduleVersion = 0.3.0
snapshotBacked = true
generatedFallback = true
relationshipLoadingDisabledInReadPath = true
```

---

### 17.5 Commands-Status JSON prüfen

```powershell
curl.exe -sS --max-time 10 "$($BASE)/commands/_status" | ConvertFrom-Json | ConvertTo-Json -Depth 20
```

Erwartung:

```text
ok = true
route.moduleVersion = 0.2.0
snapshotWrites = true
eventWrites = true
relationshipLoadingDisabledInCommandPath = true
```

---

### 17.6 DB-Bootstrap Check-only

```powershell
python .\services\vectoplan-chunk\scripts\bootstrap_db.py --check-only --json
```

Erwartung:

```text
ok = true
checkOnly = true
createAll = false
seed = false
repairMissingColumns = false
repairSeedInvariants = false
bootstrap.schemaReady = true
bootstrap.seedReady = true
bootstrap.defaultProjectReady = true
bootstrap.defaultUniverseReady = true
bootstrap.defaultWorldReady = true
```

---

### 17.7 DB-Bootstrap explizit

Nur ausführen, wenn Schema/Seed bewusst initialisiert oder repariert werden soll:

```powershell
python .\services\vectoplan-chunk\scripts\bootstrap_db.py `
  --config development `
  --create-all `
  --repair-missing-columns `
  --seed `
  --repair-seed-invariants `
  --json
```

Wichtig:

```text
Nicht im normalen Runtime-Start ausführen.
Nicht parallel in mehreren Runtime-Workern ausführen.
```

---

### 17.8 Generator-Chunk ohne Snapshot

```powershell
curl.exe -v --max-time 15 "$($BASE)/projects/dev-project/worlds/world_spawn/chunks?chunkX=5&chunkY=0&chunkZ=0" -o chunk-generated-5-0-0.json
```

```powershell
$chunk = Get-Content .\chunk-generated-5-0-0.json -Raw | ConvertFrom-Json
$cells = $chunk.chunk.cells

"source=$($chunk.source)"
"chunkKey=$($chunk.chunkKey)"
"cellCount=$($cells.Count)"
```

Erwartung:

```text
source=generated
chunkKey=5:0:0
cellCount=4096
```

---

### 17.9 Generator-Fallback deaktivieren

```powershell
curl.exe -v --max-time 15 "$($BASE)/projects/dev-project/worlds/world_spawn/chunks?chunkX=5&chunkY=0&chunkZ=0&preferSnapshot=false&allowGenerated=false" -o chunk-no-snapshot-no-generated.json
```

```powershell
Get-Content .\chunk-no-snapshot-no-generated.json -Raw
```

Erwartung:

```text
HTTP 404
code = chunk_not_found
```

---

### 17.10 Snapshot-Chunk laden

```powershell
curl.exe -v --max-time 15 "$($BASE)/projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0" -o chunk-snapshot-0-0-0.json
```

```powershell
$chunk = Get-Content .\chunk-snapshot-0-0-0.json -Raw | ConvertFrom-Json
$cells = $chunk.chunk.cells

"source=$($chunk.source)"
"chunkKey=$($chunk.chunkKey)"
"cellCount=$($cells.Count)"
"cell[273]=$($cells[273])"
```

Erwartung:

```text
source=snapshot
chunkKey=0:0:0
cellCount=4096
```

Hinweis:

```text
cell[273]=1 stammt aus einem historischen SetBlock-Test.
Wenn die DB zurückgesetzt wurde, kann dieser Wert anders sein.
```

---

## 18. Block setzen und entfernen – vollständiger PowerShell-Test

Dieser Test setzt einen Block an einer neuen Zelle, lädt den Chunk neu, entfernt den Block wieder und prüft erneut.

### 18.1 Testposition definieren

```powershell
$BASE = "http://127.0.0.1:5002"
$chunkSize = 16

$pos = @{
  x = 6
  y = 4
  z = 5
}

$cellIndex = $pos.x + ($pos.y * $chunkSize) + ($pos.z * $chunkSize * $chunkSize)

"cellIndex=$cellIndex"
```

Erwartung:

```text
cellIndex=1350
```

---

### 18.2 Chunk vor Änderung laden

```powershell
curl.exe -sS --max-time 15 "$($BASE)/projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0" -o chunk-before-command.json

$chunk = Get-Content .\chunk-before-command.json -Raw | ConvertFrom-Json
$cells = $chunk.chunk.cells

"before cell[$cellIndex]=$($cells[$cellIndex])"
```

Erwartung bei frisch entfernter Testzelle:

```text
before cell[1350]=0
```

Wenn der Wert schon `1` ist:

```text
zuerst RemoveBlock ausführen
oder eine andere Testposition verwenden
```

---

### 18.3 SetBlock ausführen

```powershell
$setBody = @{
  type = "SetBlock"
  userId = "user_test"
  sessionId = "session_test_$(Get-Date -Format yyyyMMddHHmmss)"
  position = $pos
  blockTypeId = "debug_grass"
} | ConvertTo-Json -Depth 20

$setResult = Invoke-RestMethod `
  -Method Post `
  -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/commands" `
  -ContentType "application/json" `
  -Body $setBody `
  -TimeoutSec 15

$setResult | ConvertTo-Json -Depth 30
```

Erwartung:

```text
ok = true
commandType = SetBlock
commandStatus = applied
changed = true
eventIds enthält 1 Event
changedChunks enthält 0:0:0
dirtyChunks enthält 0:0:0
snapshotIds enthält Snapshot-ID
chunkVersions enthält 0:0:0
flags.snapshotWritten = true
flags.eventsWritten = true
```

---

### 18.4 Reload nach SetBlock

```powershell
curl.exe -sS --max-time 15 "$($BASE)/projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0" -o chunk-after-setblock.json

$chunk = Get-Content .\chunk-after-setblock.json -Raw | ConvertFrom-Json
$cells = $chunk.chunk.cells

"after SetBlock cell[$cellIndex]=$($cells[$cellIndex])"
```

Erwartung:

```text
after SetBlock cell[1350]=1
```

---

### 18.5 RemoveBlock ausführen

```powershell
$removeBody = @{
  type = "RemoveBlock"
  userId = "user_test"
  sessionId = "session_test_$(Get-Date -Format yyyyMMddHHmmss)"
  position = $pos
} | ConvertTo-Json -Depth 20

$removeResult = Invoke-RestMethod `
  -Method Post `
  -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/commands" `
  -ContentType "application/json" `
  -Body $removeBody `
  -TimeoutSec 15

$removeResult | ConvertTo-Json -Depth 30
```

Erwartung:

```text
ok = true
commandType = RemoveBlock
commandStatus = applied
changed = true
affectedCells[0].beforeCellValue = 1
affectedCells[0].afterCellValue = 0
affectedCells[0].beforeBlockTypeId = debug_grass
affectedCells[0].afterBlockTypeId = null
eventIds enthält 1 Event
snapshotIds enthält Snapshot-ID
```

---

### 18.6 Reload nach RemoveBlock

```powershell
curl.exe -sS --max-time 15 "$($BASE)/projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0" -o chunk-after-removeblock.json

$chunk = Get-Content .\chunk-after-removeblock.json -Raw | ConvertFrom-Json
$cells = $chunk.chunk.cells

"after RemoveBlock cell[$cellIndex]=$($cells[$cellIndex])"
```

Erwartung:

```text
after RemoveBlock cell[1350]=0
```

---

### 18.7 Automatische Assertion für Set/Remove

Diese Variante bricht mit Fehler ab, wenn etwas nicht stimmt.

```powershell
$BASE = "http://127.0.0.1:5002"
$chunkSize = 16
$pos = @{ x = 6; y = 4; z = 5 }
$cellIndex = $pos.x + ($pos.y * $chunkSize) + ($pos.z * $chunkSize * $chunkSize)

function Get-ChunkCells {
  param($ChunkResponse)

  if ($null -ne $ChunkResponse.cells) { return $ChunkResponse.cells }
  if ($null -ne $ChunkResponse.chunk -and $null -ne $ChunkResponse.chunk.cells) { return $ChunkResponse.chunk.cells }
  if ($null -ne $ChunkResponse.content -and $null -ne $ChunkResponse.content.cells) { return $ChunkResponse.content.cells }

  throw "Keine cells im Chunk-Response gefunden."
}

function Load-Chunk000 {
  Invoke-RestMethod `
    -Method Get `
    -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0" `
    -TimeoutSec 15
}

function Send-Command {
  param($Body)

  $json = $Body | ConvertTo-Json -Depth 20

  Invoke-RestMethod `
    -Method Post `
    -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/commands" `
    -ContentType "application/json" `
    -Body $json `
    -TimeoutSec 15
}

"cellIndex=$cellIndex"

$setBody = @{
  type = "SetBlock"
  userId = "user_test"
  sessionId = "session_test_$(Get-Date -Format yyyyMMddHHmmss)"
  position = $pos
  blockTypeId = "debug_grass"
}

$setResult = Send-Command $setBody

if (-not $setResult.ok) {
  throw "SetBlock failed: ok=false"
}

$chunkAfterSet = Load-Chunk000
$cellsAfterSet = Get-ChunkCells $chunkAfterSet

"after SetBlock cell[$cellIndex]=$($cellsAfterSet[$cellIndex])"

if ($cellsAfterSet[$cellIndex] -ne 1) {
  throw "SetBlock-Test fehlgeschlagen: Erwartet cell[$cellIndex] = 1, erhalten $($cellsAfterSet[$cellIndex])"
}

$removeBody = @{
  type = "RemoveBlock"
  userId = "user_test"
  sessionId = "session_test_$(Get-Date -Format yyyyMMddHHmmss)"
  position = $pos
}

$removeResult = Send-Command $removeBody

if (-not $removeResult.ok) {
  throw "RemoveBlock failed: ok=false"
}

$chunkAfterRemove = Load-Chunk000
$cellsAfterRemove = Get-ChunkCells $chunkAfterRemove

"after RemoveBlock cell[$cellIndex]=$($cellsAfterRemove[$cellIndex])"

if ($cellsAfterRemove[$cellIndex] -ne 0) {
  throw "RemoveBlock-Test fehlgeschlagen: Erwartet cell[$cellIndex] = 0, erhalten $($cellsAfterRemove[$cellIndex])"
}

"TEST ERFOLGREICH: SetBlock und RemoveBlock funktionieren für Position x=$($pos.x), y=$($pos.y), z=$($pos.z)."
```

---

## 19. DB-Prüfung nach Block-Test

### 19.1 Default-Invariant prüfen

```powershell
docker exec vectoplan-chunk-db psql -U vectoplan_chunk -d vectoplan_chunk -c "select id, project_id, default_universe_id, default_world_id, spawn_world_id, external_app_project_id from projects order by id;"
```

Erwartung für Dev-Seed:

```text
project_id = dev-project
default_universe_id = dev-universe
default_world_id = world_spawn
spawn_world_id = world_spawn
```

```powershell
docker exec vectoplan-chunk-db psql -U vectoplan_chunk -d vectoplan_chunk -c "select id, project_db_id, universe_id, default_world_id, spawn_world_id from universes order by id;"
```

Erwartung:

```text
universe_id = dev-universe
default_world_id = world_spawn
spawn_world_id = world_spawn
```

```powershell
docker exec vectoplan-chunk-db psql -U vectoplan_chunk -d vectoplan_chunk -c "select id, project_db_id, universe_db_id, world_id, template_id, provider_id, provider_world_id, block_registry_id, block_registry_version from world_instances order by id;"
```

Erwartung:

```text
world_id = world_spawn
template_id = flat
provider_id = flat
provider_world_id = flat
block_registry_id = debug-blocks
block_registry_version = 1
```

---

### 19.2 Snapshots prüfen

```powershell
docker exec vectoplan-chunk-db psql -U vectoplan_chunk -d vectoplan_chunk -c "select snapshot_id, chunk_key, chunk_version, chunk_revision, cell_count, status, updated_at from chunk_snapshots order by id desc limit 10;"
```

Erwartung:

```text
ein aktiver Snapshot für 0:0:0
chunk_revision steigt nach echten Änderungen
cell_count = 4096
status = active
```

---

### 19.3 Events prüfen

```powershell
docker exec vectoplan-chunk-db psql -U vectoplan_chunk -d vectoplan_chunk -c "select event_id, command_type, event_type, event_status, chunk_key, position_x, position_y, position_z, cell_before_value, cell_after_value, affected_cell_count, dirty_chunk_count, created_at from chunk_events order by id desc limit 10;"
```

Erwartung:

```text
SetBlock:
command_type = SetBlock
event_type = block_change
event_status = active
cell_before_value = 0
cell_after_value = 1

RemoveBlock:
command_type = RemoveBlock
event_type = block_change
event_status = active
cell_before_value = 1
cell_after_value = 0
```

---

### 19.4 CommandLogs prüfen

```powershell
docker exec vectoplan-chunk-db psql -U vectoplan_chunk -d vectoplan_chunk -c "select command_id, command_type, command_status, changed, affected_chunk_count, event_count, created_at from world_command_logs order by id desc limit 10;"
```

Erwartung:

```text
SetBlock:
command_status = applied
changed = true
affected_chunk_count = 1
event_count = 1

RemoveBlock:
command_status = applied
changed = true
affected_chunk_count = 1
event_count = 1
```

---

### 19.5 App-Provisioning-Projects prüfen

```powershell
docker exec vectoplan-chunk-db psql -U vectoplan_chunk -d vectoplan_chunk -c "select id, project_id, external_app_project_id, default_universe_id, default_world_id, spawn_world_id, source_service from projects order by id desc limit 20;"
```

Erwartung für App-Projekte:

```text
project_id beginnt typischerweise mit chk_prj_
external_app_project_id = prj_...
default_world_id = world_spawn
spawn_world_id = world_spawn
source_service enthält vectoplan-app oder provisioning-Hinweis
```

---

## 20. App↔Chunk↔Editor Smoke-Test

### 20.1 Chunk-Service Status

```powershell
$CHUNK = "http://127.0.0.1:5002"

curl.exe -sS "$($CHUNK)/projects/_status" | ConvertFrom-Json | Select-Object ok,status,schemaReady,seedReady,defaultProjectReady,defaultUniverseReady,defaultWorldReady
```

Erwartung:

```text
ok = true
status = ready
schemaReady = true
seedReady = true
defaultProjectReady = true
defaultUniverseReady = true
defaultWorldReady = true
```

---

### 20.2 Provisioning Preview

```powershell
$appProjectId = "prj_smoke_$(Get-Date -Format yyyyMMddHHmmss)"

curl.exe -sS "$($CHUNK)/projects/preview/by-app/$appProjectId" | ConvertFrom-Json | ConvertTo-Json -Depth 20
```

Erwartung:

```text
willCreateDatabaseRows = false
chunkWorldId = world_spawn
```

---

### 20.3 Provisioning ausführen

```powershell
$appProjectId = "prj_smoke_$(Get-Date -Format yyyyMMddHHmmss)"

$body = @{
  name = "Smoke Test Project"
  description = "App to Chunk provisioning smoke test"
} | ConvertTo-Json -Depth 20

$result = Invoke-RestMethod `
  -Method Put `
  -Uri "$($CHUNK)/projects/by-app/$appProjectId" `
  -ContentType "application/json" `
  -Body $body `
  -TimeoutSec 20

$result | ConvertTo-Json -Depth 30
```

Erwartung:

```text
ok = true
chunkProjectId vorhanden
chunkUniverseId vorhanden
chunkWorldId = world_spawn
```

---

### 20.4 Provisioned Chunk-Batch laden

```powershell
$chunkProjectId = $result.chunkProjectId
if (-not $chunkProjectId) { $chunkProjectId = $result.projectId }

$chunkWorldId = $result.chunkWorldId
if (-not $chunkWorldId) { $chunkWorldId = "world_spawn" }

$batchBody = @{
  chunks = @(
    @{ chunkX = 0; chunkY = 0; chunkZ = 0 },
    @{ chunkX = 1; chunkY = 0; chunkZ = 0 }
  )
} | ConvertTo-Json -Depth 20

$batch = Invoke-RestMethod `
  -Method Post `
  -Uri "$($CHUNK)/projects/$chunkProjectId/worlds/$chunkWorldId/chunks/batch" `
  -ContentType "application/json" `
  -Body $batchBody `
  -TimeoutSec 20

$batch | ConvertTo-Json -Depth 30
```

Erwartung:

```text
HTTP 200
Chunks vorhanden
worldId = world_spawn
```

---

## 21. Optionaler Grenztest für Dirty-Chunks

Dieser Test setzt einen Block an der rechten X-Grenze von Chunk `0:0:0`.

Position:

```text
x = 15
y = 4
z = 5
```

Da `localX = 15` bei `chunkSize = 16` die rechte Grenze ist, sollte `dirtyChunks` mindestens enthalten:

```text
0:0:0
1:0:0
```

PowerShell:

```powershell
$BASE = "http://127.0.0.1:5002"

$edgePos = @{
  x = 15
  y = 4
  z = 5
}

$edgeSetBody = @{
  type = "SetBlock"
  userId = "user_test"
  sessionId = "session_test_edge_$(Get-Date -Format yyyyMMddHHmmss)"
  position = $edgePos
  blockTypeId = "debug_grass"
} | ConvertTo-Json -Depth 20

$edgeSetResult = Invoke-RestMethod `
  -Method Post `
  -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/commands" `
  -ContentType "application/json" `
  -Body $edgeSetBody `
  -TimeoutSec 15

$edgeSetResult | ConvertTo-Json -Depth 30
"dirtyChunks=$($edgeSetResult.dirtyChunks -join ',')"
```

Erwartung:

```text
dirtyChunks enthält 0:0:0 und 1:0:0
```

Aufräumen:

```powershell
$edgeRemoveBody = @{
  type = "RemoveBlock"
  userId = "user_test"
  sessionId = "session_test_edge_$(Get-Date -Format yyyyMMddHHmmss)"
  position = $edgePos
} | ConvertTo-Json -Depth 20

$edgeRemoveResult = Invoke-RestMethod `
  -Method Post `
  -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/commands" `
  -ContentType "application/json" `
  -Body $edgeRemoveBody `
  -TimeoutSec 15

$edgeRemoveResult | ConvertTo-Json -Depth 30
```

---

## 22. Nächster sinnvoller Schritt

Der erste Backend-Slice ist stabil genug für den nächsten Entwicklungsabschnitt.

Bestätigt ist:

```text
Runtime-Startup stabil
Statusrouten 200
DB/Seed/Default-World readiness true
Generator-Chunk lädt
Snapshot-Chunk lädt
SetBlock persistiert
Reload zeigt gesetzten Block
RemoveBlock persistiert
Reload zeigt entfernten Block
CommandLog wird geschrieben
ChunkEvent wird geschrieben
Snapshot wird aktualisiert
App-Provisioning funktioniert
Editor chunks/batch funktioniert
kein Worker-Timeout im bestätigten Ablauf
```

Nächste sinnvolle Schritte:

```text
1. Editor-Commands SetBlock/RemoveBlock aus echter UI gegen Chunk-Service senden lassen.
2. Dirty-Chunks nach Commands im Editor neu laden.
3. Grenz-Dirty-Chunks testen.
4. Negative Koordinaten testen.
5. Chunk-Batch mit Generator-/Snapshot-Mix testen.
6. ReplaceBlock End-to-End testen.
7. PlaceObject/RemoveObject End-to-End testen.
8. App-Provisioning mit mehreren Projekten testen.
```

Optional technische Härtung:

```text
1. bootstrap_db.py Summary-Flattening normalisieren.
2. event.py später zusätzlich härten.
3. models/chunk.py später zusätzlich härten.
4. Alembic-Migrationen vorbereiten.
5. vectoplan-chunk-init als finalen Compose-Service sauber dokumentieren.
6. DB-Repair nur für Dev/Init zulassen, nicht für Runtime.
```

Nicht mehr zwingend für den ersten Stand:

```text
Startup-Hooks komplett deaktivieren.
event.py sofort härten.
Weitere DB-Modelle vor Editor-Anbindung umbauen.
```

---

## 23. Aktueller Gesamtbefund

Der aktuelle Gesamtbefund lautet:

```text
Der Chunk-Service hat den Schritt von reiner Generator-/World-State-API
zu einem PostgreSQL-gestützten Persistenzsystem erfolgreich begonnen
und ist nun zusätzlich als App-Projekt-Provisioning-Ziel nutzbar.
```

Bestätigt ist:

```text
Docker-Service startet.
PostgreSQL-Service existiert.
Modelle sind erstellt.
Default-Projekt-/Universe-/World-Struktur ist vorbereitet.
world_spawn existiert als konkrete editierbare Welt.
flat bleibt Template-/Provider-Welt.
BlockRegistry und Debug-Blocks sind vorbereitet.
Projektgescopte Read-Routen funktionieren.
Generator-Chunk-Pfad funktioniert.
Snapshot-Chunk-Pfad funktioniert.
SetBlock funktioniert.
RemoveBlock funktioniert.
SetBlock schreibt CommandLog.
SetBlock schreibt ChunkEvent.
SetBlock schreibt oder aktualisiert ChunkSnapshot.
RemoveBlock schreibt CommandLog.
RemoveBlock schreibt ChunkEvent.
RemoveBlock aktualisiert ChunkSnapshot.
Dirty-Chunks werden bei SetBlock zurückgegeben.
Dirty-Chunks werden bei RemoveBlock zurückgegeben.
Reload nach SetBlock zeigt den gesetzten Block.
Reload nach RemoveBlock zeigt entfernten Block.
Der frühere Worker-Hänger wurde auf Runtime-Startup mit DB-Mutation eingegrenzt.
Runtime ist read-only.
DB-Create und Default-Seeding wurden in explizite Bootstrap-Module ausgelagert.
Seed-Invarianten werden explizit geprüft und bei Bootstrap repariert.
routes/chunks.py ist stabilisiert.
routes/commands.py ist stabilisiert.
routes/projects.py ist um Readiness und App-Provisioning erweitert.
src/world_state/provisioning.py verbindet vectoplan-app mit vectoplan-chunk.
App→Chunk Provisioning funktioniert.
Editor→Chunk chunks/batch funktioniert.
```

Aktuell noch zu bestätigen:

```text
ReplaceBlock End-to-End
PlaceObject End-to-End
RemoveObject End-to-End
Chunk-Batch mit Snapshot-/Generator-Mix
Grenz-Dirty-Chunks
Negative Koordinaten
Editor SetBlock/RemoveBlock aus echter UI
Mehrere App-Projekte parallel
Idempotenz von /projects/by-app über wiederholte Calls
```

Der erreichte stabile Stand lautet:

```text
App erstellt Projekt
→ App sichert Chunk-Projektgraph
→ Chunk-Service erzeugt/holt Project + Universe + world_spawn
→ Editor lädt Chunks aus world_spawn
→ Client kann Chunk laden
→ Chunk wird generiert oder aus Snapshot geladen
→ Nutzer/Client setzt Block
→ Chunk-Service speichert Snapshot
→ Chunk-Service schreibt Event
→ Chunk-Service schreibt CommandLog
→ Chunk wird neu geladen
→ Block bleibt sichtbar
→ Nutzer/Client entfernt Block
→ Chunk-Service aktualisiert Snapshot
→ Chunk-Service schreibt Event
→ Chunk-Service schreibt CommandLog
→ Chunk wird neu geladen
→ Block ist entfernt
```

Damit ist der erste Backend-Stand für die editierbare Chunk-Welt und die App-/Editor-Anbindung ausreichend belastbar.

---

## 24. Dateien, die im aktuellen Umbau besonders wichtig waren

### 24.1 Infrastruktur

```text
services/vectoplan-server/docker-compose.all.yml
```

Wichtigste Änderung:

```text
Runtime und Init/Bootstrap wurden getrennt.
Runtime läuft read-only.
Init/Bootstrap darf mutieren.
```

---

### 24.2 Runtime/Bootstrap

```text
services/vectoplan-chunk/config.py
services/vectoplan-chunk/entrypoint.sh
services/vectoplan-chunk/scripts/bootstrap_db.py
services/vectoplan-chunk/src/bootstrap/settings.py
services/vectoplan-chunk/src/bootstrap/default_seed.py
services/vectoplan-chunk/src/bootstrap/db_bootstrap.py
```

Wichtigste Änderung:

```text
world_spawn/flat-Invariante ist durchgehend verankert.
Runtime mutiert nicht.
Check-only mutiert nicht.
Bootstrap darf explizit create/seed/repair ausführen.
```

---

### 24.3 Model-Schicht

```text
services/vectoplan-chunk/models/project.py
services/vectoplan-chunk/models/universe.py
services/vectoplan-chunk/models/world.py
services/vectoplan-chunk/models/__init__.py
```

Wichtigste Änderung:

```text
Project/Universe/WorldInstance tragen Default-/Spawn-Weltbezüge.
WorldInstance schützt gegen flat als konkrete world_id.
```

---

### 24.4 App-Provisioning

```text
services/vectoplan-chunk/src/world_state/provisioning.py
services/vectoplan-chunk/routes/projects.py
```

Wichtigste Änderung:

```text
vectoplan-app kann über by-app/ensure einen Chunk-Projektgraphen sicherstellen.
```

---

### 24.5 Read-/Write-Pfade

```text
services/vectoplan-chunk/routes/chunks.py
services/vectoplan-chunk/routes/commands.py
services/vectoplan-chunk/models/chunk.py
services/vectoplan-chunk/models/event.py
```

Wichtigste Änderung:

```text
Generator-/Snapshot-Lesen funktioniert.
SetBlock/RemoveBlock-Schreiben funktioniert.
Snapshots, Events und CommandLogs werden persistiert.
```

---

## 25. Abschlussstand

Aktueller Status:

```text
vectoplan-chunk startet.
Runtime ist read-only.
DB/Seed/world_spawn sind ready.
Generator- und Snapshot-Chunks funktionieren.
SetBlock/RemoveBlock funktionieren.
App-Provisioning funktioniert.
Editor chunks/batch funktioniert.
```

Aktuell noch nicht final:

```text
ReplaceBlock
PlaceObject
RemoveObject
Grenz-Dirty-Chunks
Negative Koordinaten
Batch-Mix-Tests
Alembic-/Migrationskonzept
Produktionsreifes DB-Upgrade-Konzept
```

Empfohlener nächster Schritt:

```text
Editor-Command-Integration gegen /projects/<chunk_project_id>/worlds/world_spawn/commands
```

Danach:

```text
Dirty-Chunk-Reload im Editor
Grenztests
Negative Koordinaten
Batch-Mix
Object-Commands
```

---

## 26. Aktualisierung 2026-07-12 – Eingebaute Systemblöcke

Dieser Abschnitt ergänzt die vollständige historische Bestandsaufnahme um den inzwischen implementierten und erfolgreich gestarteten Systemblock-Slice.

### 26.1 Aktueller Gesamtstatus

Der Systemblock-Slice ist betriebsfähig.

Verifiziert ist:

```text
responseVersion = system-blocks-response.v1
ok = true
ready = true

catalogReady = true
registryReady = true
airInvariantReady = true
systemRailingReady = true
systemBlocksReady = true
```

Der Code-Katalog, die Datenbank-Registry und die persistente Spiegelung stimmen miteinander überein.

Aktuelle Definitionen:

```text
system_air
system_railing
```

Aktuelle persistente Systemblock-Mirrors:

```text
system_railing
```

Aktuelle reservierte, nicht persistente Definition:

```text
system_air
```

Katalogzählung:

```text
definitionCount           = 2
persistentDefinitionCount = 1
reservedDefinitionCount   = 1
inventoryDefinitionCount  = 1
```

Registry-Zählung für Systemblöcke:

```text
mirrors      = 1
readyMirrors = 1
created      = 0
updated      = 0
drifted      = 0
```

Die Werte `created = 0` und `updated = 0` im Read-only-Status bedeuten, dass beim Statusaufruf keine Reparatur nötig war. Der persistente Mirror war bereits vorhanden und unverändert.

---

### 26.2 Fachliche Abgrenzung: Systemblock, Debug-Block und Air

#### Systemblock

Ein Systemblock ist im Code definiert und besitzt eine stabile Systemidentität.

Beispiel:

```text
systemBlockId      = system_railing
runtimeBlockTypeId = system_railing
source             = system
category           = system
```

Ein persistenter Systemblock wird in die BlockRegistry gespiegelt, damit bestehende Runtime-, Palette- und Command-Pfade ihn wie einen normalen `BlockType` auflösen können.

#### Debug-Block

Debug-Blöcke sind persistente Entwicklungsblöcke der Default-Registry.

Aktuell:

```text
debug_grass
debug_dirt
```

Sie besitzen:

```text
category = debug
source   = Registry-/Seed-Kontext
```

#### Air

Air ist weder Debug-Block noch normaler persistenter Systemblock.

Air ist der reservierte leere Zellzustand:

```text
systemBlockId = system_air
cellValue     = 0
BlockType     = nicht vorhanden
PaletteEntry  = nicht vorhanden
```

---

### 26.3 Architektur des Pakets `src/system_blocks`

Aktuelle Struktur:

```text
services/vectoplan-chunk/src/system_blocks/
├── __init__.py
├── contracts.py
├── catalog.py
├── bootstrap.py
├── IST-Zustand.md
├── air/
│   ├── __init__.py
│   └── definition.py
└── railing/
    ├── __init__.py
    └── definition.py
```

Verantwortlichkeiten:

```text
contracts.py
→ frameworkunabhängiger Definitionsvertrag
→ Validierung
→ Serialisierung
→ Fingerprint
→ persistente Wertabbildung
→ DB-Vergleich

catalog.py
→ Provider laden
→ Air und Railing zusammenführen
→ Definitionen validieren
→ Indizes und Lookups erzeugen
→ Katalogstatus liefern
→ persistente Definitionen filtern

bootstrap.py
→ Air-Invariante prüfen
→ persistente Mirrors prüfen
→ fehlende Mirrors erstellen
→ Drift reparieren
→ inaktive Mirrors wiederherstellen
→ Registry-Identität prüfen
→ ohne äußeren Commit arbeiten

__init__.py
→ Lazy Package-Fassade
→ stabile öffentliche Exporte
→ Moduldiagnostik
→ Cache-Steuerung

air/definition.py
→ kanonische Air-Definition

railing/definition.py
→ kanonische Railing-Definition
```

Die Paketstruktur erzeugt keine zweite SQL-Registry. Es bleibt bei der vorhandenen `BlockRegistry`-/`BlockType`-Struktur.

---

### 26.4 Definitionsvertrag

Die gemeinsame Definition wird durch `SystemBlockDefinition` beschrieben.

Wichtige Identitätsfelder:

```text
system_block_id
runtime_block_type_id
definition_version
definition_key
definition_fingerprint
kind
source
category
status
```

Wichtige Speicherfelder:

```text
reserved_cell_value
persist_as_block_type
default_palette_index
```

Wichtige Editor-/Runtime-Felder:

```text
inventory_visible
solid
opaque
placeable
breakable
selectable
collidable
emits_light
light_level
hardness
stack_size
render_mode
shape_type
material_id
texture_id
icon_id
```

Wichtige abgeleitete Eigenschaften:

```text
is_reserved_cell_state
is_air_state
is_persisted_runtime_block
can_appear_in_inventory
```

Der Definitions-Fingerprint ist entscheidend für den Drift-Abgleich:

```text
Code-Definition
→ kanonisch serialisieren
→ Fingerprint erzeugen
→ im persistenten Metadata-Bereich speichern
→ beim Status-/Bootstrap-Lauf vergleichen
```

---

### 26.5 Katalog

Der Katalog trägt aktuell die Identität:

```text
catalogId      = vectoplan-built-in-system-blocks
catalogVersion = 1
moduleVersion  = 1.0.1
```

Provider:

```text
air
railing
```

Beide Provider sind aktuell:

```text
required = true
ready    = true
imported = true
```

Importpfade:

```text
src.system_blocks.air
src.system_blocks.railing
```

Der Katalog stellt Lookups bereit nach:

```text
systemBlockId
runtimeBlockTypeId
Definition-Key
Alias
reserviertem Zellwert
```

Kataloginvarianten:

```text
systemBlockId muss eindeutig sein
runtimeBlockTypeId muss eindeutig sein
Alias muss eindeutig sein
reservierter Zellwert muss eindeutig sein
Air muss cellValue 0 reservieren
Air darf nicht persistiert werden
persistente Systemblöcke benötigen runtimeBlockTypeId
inventarsichtbare Definitionen müssen platzierbar sein
```

---

### 26.6 Air-Definition

Identität:

```text
systemBlockId      = system_air
runtimeBlockTypeId = null
definitionVersion  = 1
kind               = air
source             = system
category           = system
status             = active
```

Speichersemantik:

```text
reservedCellValue  = 0
persistAsBlockType = false
storedAsBlockType  = false
storedInPositivePalette = false
storedAsCellValue  = 0
```

Editorsemantik:

```text
inventoryVisible = false
placeable        = false
breakable        = false
selectable       = false
targetable       = false
replaceable      = true
```

Rendering:

```text
visible     = false
createsMesh = false
renderMode  = invisible
shapeType   = empty
```

Physik:

```text
solid          = false
collidable     = false
blocksMovement = false
hardness       = 0.0
```

Command-Semantik:

```text
creationCommand          = RemoveBlock
placementCommand         = null
forbiddenPlacementCommand = SetBlock
setBlockErrorCode        = air_requires_remove_block
```

Verifizierter Datenbankstatus:

```text
illegalRowCount     = 0
illegalRowDbIds     = []
illegalBlockTypeIds = []
action              = ready
ready               = true
```

Damit ist bestätigt:

```text
Keine BlockType-Zeile für system_air vorhanden.
cellValue 0 bleibt ausschließlich Air.
```

---

### 26.7 Railing-Definition

Identität:

```text
systemBlockId      = system_railing
runtimeBlockTypeId = system_railing
definitionVersion  = 1
kind               = railing
source             = system
category           = system
status             = active
```

Persistenz:

```text
persistAsBlockType  = true
immutableDefinition = true
inventoryVisible    = true
```

Laufzeitflags:

```text
solid       = true
opaque      = true
placeable   = true
breakable   = true
selectable  = true
collidable  = true
emitsLight  = false
lightLevel  = 0
hardness    = 1.0
stackSize   = 64
```

Rendering in Version 1:

```text
renderMode      = cube
shapeType       = cube
currentGeometry = full_cube
futureGeometry  = railing
```

Kollision in Version 1:

```text
currentCollision = full_cube
blocksMovement   = true
```

Noch nicht implementiert:

```text
orientationSupported          = false
neighbourConnectionSupported  = false
multiBlockObject               = false
```

Zukunftskompatibilität:

```text
runtimeBlockTypeId bleibt system_railing
Geometrie darf später geändert werden
Kollision darf später geändert werden
Orientierung darf später ergänzt werden
Nachbarverbindungen dürfen später ergänzt werden
```

---

### 26.8 Persistenter Railing-Mirror

Der persistente Mirror liegt in:

```text
registryId      = debug-blocks
registryVersion = 1
registryKey     = debug-blocks@1
```

Persistente Identität:

```text
blockTypeId      = system_railing
systemBlockId    = system_railing
runtimeBlockTypeId = system_railing
category         = system
status           = active
revision         = 1
```

Verifizierter Mirror-Status:

```text
ready                 = true
repairable            = true
action                = unchanged
driftBefore           = {}
driftAfter            = {}
modelValidationErrors = {}
wouldChange           = false
changed               = false
created               = false
updated               = false
errors                = []
```

Der Mirror enthält Systemmetadaten unter:

```text
metadata.vectoplanSystemBlock
```

Wichtige Werte:

```text
schemaVersion          = system-block-metadata.schema.v1
systemBlockId          = system_railing
runtimeBlockTypeId     = system_railing
definitionVersion      = 1
persistAsBlockType     = true
inventoryVisible       = true
immutableDefinition    = true
cellEncodingVersion    = cell-encoding.palette-index-plus-one.v1
blockCellValueRule     = paletteIndex + 1
definitionFingerprint  = kanonischer Railing-Fingerprint
```

---

### 26.9 BlockRegistry

Aktuell bestätigte Default-Registry:

```text
registryId      = debug-blocks
registryVersion = 1
registryKey     = debug-blocks@1
label           = Debug Blocks
status          = active
source          = internal
isDefault       = true
```

Wichtig:

```text
BlockRegistry.source = internal
```

Der frühere temporäre Bootstrap-Wert:

```text
source = bootstrap
```

ist für die Datenbankspalte nicht gültig und wird nicht mehr verwendet.

`bootstrap` bleibt zulässig als:

```text
createdByUserId
updatedByUserId
seededBy-Metadatum
```

Die Registry enthält aktuell drei positive Paletteeinträge:

```text
debug_grass
debug_dirt
system_railing
```

Air ist nicht Teil dieser positiven Liste.

---

### 26.10 Zellwert- und Palettenmodell

Globale Kodierungsregel:

```text
encoding.version = cell-encoding.palette-index-plus-one.v1
airCellValue     = 0
blockCellValueRule = paletteIndex + 1
```

Aktuell konkrete Default-Weltpalette:

```text
paletteIndex 0
→ blockTypeId = debug_grass
→ cellValue   = 1

paletteIndex 1
→ blockTypeId = debug_dirt
→ cellValue   = 2

paletteIndex 2
→ blockTypeId = system_railing
→ cellValue   = 3
```

Wichtig:

```text
system_railing hat keinen global fest reservierten Zellwert 3.
```

Der aktuelle Wert `3` entsteht nur aus:

```text
paletteIndex 2 + 1
```

In einer anderen Chunkpalette kann `system_railing` einen anderen positiven Zellwert erhalten.

Persistente Railing-Defaults bleiben deshalb:

```text
defaultPaletteIndex = null
defaultCellValue    = null
```

Die konkrete Palette bestimmt den Zellwert.

---

### 26.11 Bootstrap-Integration

Die Systemblock-Schicht ist in den expliziten DB-Bootstrap integriert.

Beteiligte Dateien:

```text
src/bootstrap/default_seed.py
src/bootstrap/db_bootstrap.py
scripts/bootstrap_db.py
src/system_blocks/bootstrap.py
```

Aktueller Ablauf:

```text
Schema prüfen/erzeugen
→ Default-Registry sicherstellen
→ Debug-Blöcke sicherstellen
→ Air-Invariante prüfen
→ system_railing-Mirror sicherstellen
→ Dev-Project sicherstellen
→ Dev-Universe sicherstellen
→ world_spawn sicherstellen
→ Gesamtstatus prüfen
→ äußerer Commit
```

Bei einem Fehler:

```text
äußerer Rollback
```

Transaktionsregel:

```text
src/system_blocks/bootstrap.py
→ darf flushen
→ darf verschachtelte Transaktion verwenden
→ führt keinen äußeren Commit aus

default_seed.py / db_bootstrap.py
→ besitzen Commit-/Rollback-Verantwortung
```

Aktuelle Readiness-Felder:

```text
systemBlocksReady
systemRailingReady
airInvariantReady
systemBlockCount
systemBlocksCreated
systemBlocksUpdated
systemBlocksMissing
systemBlocksDrifted
```

Der Bootstrap gilt nicht als vollständig bereit, wenn:

```text
system_air als BlockType existiert
system_railing fehlt
system_railing inaktiv ist
system_railing gelöscht ist
system_railing zur Code-Definition driftet
Registry fehlt oder inaktiv ist
```

---

### 26.12 Normale Welt-Blockroute

Route:

```text
GET /projects/dev-project/worlds/world_spawn/blocks
```

Antwortversion:

```text
world-blocks-response.v2
```

Die Route liefert:

```text
Registry-Kontext
Air separat
positive Blockliste
Palette
konkrete Paletteindizes
konkrete Zellwerte
Blockflags
Systemblock-Metadaten
Route-Hints
```

Aktuell bestätigte Blockliste:

```text
debug_grass
debug_dirt
system_railing
```

Aktuelle Zählung:

```text
blocks         = 3
paletteEntries = 3
includingAir   = 4
```

Bestätigter Railing-Eintrag:

```json
{
  "paletteIndex": 2,
  "cellValue": 3,
  "blockTypeId": "system_railing",
  "label": "Railing",
  "category": "system",
  "systemBlockId": "system_railing",
  "runtimeBlockTypeId": "system_railing",
  "definitionVersion": "1",
  "source": "system",
  "persistAsBlockType": true,
  "inventoryVisible": true,
  "immutableDefinition": true,
  "placeable": true,
  "breakable": true,
  "selectable": true,
  "collidable": true,
  "renderMode": "cube",
  "shapeType": "cube"
}
```

Diese Route bestätigt, dass der eingebaute Systemblock für die normale Editor-Blockauswahl verfügbar ist.

---

### 26.13 Systemblock-Spezialroute

Projektgescopt:

```text
GET /projects/dev-project/worlds/world_spawn/blocks/system
```

Default-Komfortpfad:

```text
GET /blocks/system
```

Antwortversion:

```text
system-blocks-response.v1
```

Die Route liefert:

```text
readiness
Air
Code-Definitionen
persistente Mirrors
Katalog
Katalogstatus
Registrystatus
Encoding
Counts
Route-Hints
Metadaten
```

Verifizierte Kernwerte:

```text
ok    = true
ready = true

readiness.catalogReady        = true
readiness.registryReady       = true
readiness.airInvariantReady   = true
readiness.systemRailingReady  = true
readiness.systemBlocksReady   = true
```

Persistente Systemblöcke:

```text
persistentBlocks = [system_railing]
```

Air:

```text
air.systemBlockId = system_air
air.cellValue     = 0
air.blockTypeId   = null
```

---

### 26.14 Bekannte Spezialrouten-Inkonsistenz

Die aktuelle Spezialroute liefert gleichzeitig:

```text
persistentBlocks = [system_railing]
blocks           = []
inventoryBlocks  = []
```

Die Readiness bleibt korrekt und der persistente Mirror ist vorhanden.

Einordnung:

```text
Die normale Welt-Blockroute zeigt system_railing korrekt an.
Die Systemblock-Spezialroute zeigt system_railing korrekt unter persistentBlocks an.
Die Top-Level-Listen blocks und inventoryBlocks der Spezialroute sind derzeit leer.
```

Auswirkung:

```text
Editor liest normale Welt-Blockroute
→ Railing sichtbar

Admin-UI liest Spezialroute und persistentBlocks
→ Railing sichtbar

Admin-UI liest Spezialroute ausschließlich über inventoryBlocks
→ Railing derzeit nicht sichtbar
```

Empfohlene spätere Bereinigung in `routes/blocks.py`:

```text
Option A:
inventoryBlocks mit inventarsichtbaren persistenten Systemblöcken befüllen

Option B:
blocks/inventoryBlocks eindeutig als code-only Listen benennen

Option C:
Payload-Vertrag dokumentieren und Counts angleichen
```

Dies ist kein Bootstrap- oder Persistenzfehler.

---

### 26.15 Editor-/Admin-Sichtbarkeit

Aktuell über die normale Welt-Blockroute sichtbar:

```text
debug_grass
debug_dirt
system_railing
```

Nicht als Blockauswahl sichtbar:

```text
system_air
```

Das ist beabsichtigt.

`system_railing` ist aktuell:

```text
aktiv
inventarsichtbar
platzierbar
zerstörbar
selektierbar
zielbar
kollidierbar
persistiert
als Systemblock markiert
in positiver Palette enthalten
```

`system_air` ist aktuell:

```text
nicht inventarsichtbar
nicht platzierbar
nicht selektierbar
nicht kollidierbar
nicht persistiert
als Zellwert 0 reserviert
```

---

### 26.16 Command-Integration

Der bestehende Command-Pfad kann `system_railing` grundsätzlich wie einen normalen Registry-Block auflösen.

Vorgesehener Railing-Pfad:

```text
SetBlock
→ blockTypeId = system_railing
→ Registry-Block auflösen
→ positive Chunkpalette verwenden/ergänzen
→ cellValue = paletteIndex + 1
→ Snapshot schreiben
→ ChunkEvent schreiben
→ WorldCommandLog schreiben
```

Railing benötigt keinen neuen Command-Typ.

Metadaten:

```text
placementCommand      = SetBlock
removalCommand        = RemoveBlock
requiresNewCommandType = false
```

Noch nicht als eigene Command-Regel umgesetzt:

```text
SetBlock(system_air)
→ sollte ausdrücklich HTTP 400 liefern
→ code = air_requires_remove_block
```

Aktueller Schutz ohne Command-Anpassung:

```text
system_air besitzt keine BlockType-Zeile
→ bestehende BlockType-Auflösung findet Air nicht
→ Air kann nicht als normaler Block gesetzt werden
```

Empfohlene spätere Änderung in `routes/commands.py`:

```text
system_air vor DB-Lookup erkennen
explizit als invalid_command ablehnen
Hinweis auf RemoveBlock ausgeben
Systemblock-Metadaten in neu erzeugten Paletten erhalten
Command-Status um Systemblockregeln ergänzen
```

---

### 26.17 Aktuelle Datenbankerwartung

Tabelle `block_registries`:

```text
debug-blocks@1
status  = active
source  = internal
default = true
```

Tabelle `block_types`:

```text
debug_grass
debug_dirt
system_railing
```

Nicht vorhanden:

```text
system_air
```

SQL-Prüfung:

```powershell
docker exec vectoplan-chunk-db psql `
  -U vectoplan_chunk `
  -d vectoplan_chunk `
  -c "select registry_id, registry_version, status, source, is_default from block_registries order by id;"
```

Erwartung:

```text
registry_id      = debug-blocks
registry_version = 1
status           = active
source           = internal
is_default       = true
```

Systemblock-Prüfung:

```powershell
docker exec vectoplan-chunk-db psql `
  -U vectoplan_chunk `
  -d vectoplan_chunk `
  -c "select block_type_id, category, status, placeable, selectable, collidable, render_mode, shape_type from block_types where block_type_id in ('system_air','system_railing') order by block_type_id;"
```

Erwartung:

```text
genau eine Zeile:
system_railing | system | active | true | true | true | cube | cube

keine Zeile:
system_air
```

---

### 26.18 Manuelle Statusprüfung

Externer lokaler Service-Port laut bestehender Service-Dokumentation:

```text
http://127.0.0.1:5002
```

Systemblockstatus:

```powershell
$BASE = "http://127.0.0.1:5002"

$system = Invoke-RestMethod `
  -Method Get `
  -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/blocks/system" `
  -TimeoutSec 20

$system | ConvertTo-Json -Depth 50
```

Assertions:

```powershell
if (-not $system.ok) {
  throw "Systemblock-Route meldet ok=false."
}

if (-not $system.ready) {
  throw "Systemblock-Route meldet ready=false."
}

if (-not $system.readiness.catalogReady) {
  throw "Systemblock-Katalog ist nicht bereit."
}

if (-not $system.readiness.registryReady) {
  throw "BlockRegistry ist nicht bereit."
}

if (-not $system.readiness.airInvariantReady) {
  throw "Air-Invariante ist nicht bereit."
}

if (-not $system.readiness.systemRailingReady) {
  throw "system_railing ist nicht bereit."
}

if (-not $system.readiness.systemBlocksReady) {
  throw "Gesamte Systemblock-Schicht ist nicht bereit."
}

$railingMirror = @(
  $system.persistentBlocks |
    Where-Object { $_.blockTypeId -eq "system_railing" }
)

if ($railingMirror.Count -ne 1) {
  throw "Erwartet genau einen persistenten system_railing-Mirror."
}

if ($system.air.cellValue -ne 0) {
  throw "Air muss cellValue 0 besitzen."
}

if ($null -ne $system.air.blockTypeId) {
  throw "Air darf keine blockTypeId besitzen."
}

"SYSTEMBLOCK-STATUS ERFOLGREICH"
```

---

### 26.19 Manuelle Weltpalettenprüfung

```powershell
$BASE = "http://127.0.0.1:5002"

$worldBlocks = Invoke-RestMethod `
  -Method Get `
  -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/blocks" `
  -TimeoutSec 20

$worldBlocks | ConvertTo-Json -Depth 50
```

Railing suchen:

```powershell
$railing = @(
  $worldBlocks.blocks.blocks |
    Where-Object { $_.blockTypeId -eq "system_railing" }
)

if ($railing.Count -ne 1) {
  throw "system_railing fehlt in der normalen Welt-Blockliste."
}

if (-not $railing[0].inventoryVisible) {
  throw "system_railing ist nicht inventarsichtbar."
}

if (-not $railing[0].placeable) {
  throw "system_railing ist nicht platzierbar."
}

if ($railing[0].category -ne "system") {
  throw "system_railing besitzt nicht category=system."
}

if ($railing[0].cellValue -ne ($railing[0].paletteIndex + 1)) {
  throw "Railing-Zellwert verletzt paletteIndex+1."
}

if ($worldBlocks.blocks.air.cellValue -ne 0) {
  throw "Air muss in der Welt-Blockroute cellValue 0 besitzen."
}

"WELTPALETTE ERFOLGREICH"
```

---

### 26.20 Railing-Platzierungstest

Der bestehende Command-Pfad ist für Railing konzeptionell vorbereitet.

Beispiel:

```powershell
$BASE = "http://127.0.0.1:5002"

$body = @{
  type = "SetBlock"
  userId = "system_block_test"
  sessionId = "system_block_test_$(Get-Date -Format yyyyMMddHHmmss)"
  position = @{
    x = 1
    y = 2
    z = 1
  }
  blockTypeId = "system_railing"
} | ConvertTo-Json -Depth 20

$result = Invoke-RestMethod `
  -Method Post `
  -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/commands" `
  -ContentType "application/json" `
  -Body $body `
  -TimeoutSec 20

$result | ConvertTo-Json -Depth 40
```

Erwartung:

```text
ok = true
commandType = SetBlock
commandStatus = applied
changed = true
snapshotIds vorhanden
eventIds vorhanden
changedChunks vorhanden
dirtyChunks vorhanden
```

Aufräumen:

```powershell
$removeBody = @{
  type = "RemoveBlock"
  userId = "system_block_test"
  sessionId = "system_block_test_$(Get-Date -Format yyyyMMddHHmmss)"
  position = @{
    x = 1
    y = 2
    z = 1
  }
} | ConvertTo-Json -Depth 20

$removeResult = Invoke-RestMethod `
  -Method Post `
  -Uri "$($BASE)/projects/dev-project/worlds/world_spawn/commands" `
  -ContentType "application/json" `
  -Body $removeBody `
  -TimeoutSec 20

$removeResult | ConvertTo-Json -Depth 40
```

---

### 26.21 Erweiterte aktuelle Invarianten

Zusätzlich zu den in Abschnitt 13 dokumentierten Regeln gelten nun:

```text
49. Systemblock-Definitionen sind codegeführt und unveränderlich.
50. Der Systemblock-Katalog ist keine zweite Datenbankregistry.
51. Persistente Systemblöcke werden in die vorhandene BlockRegistry gespiegelt.
52. system_air reserviert ausschließlich cellValue 0.
53. system_air darf niemals als BlockType persistiert werden.
54. system_air darf keinen positiven Paletteintrag besitzen.
55. system_air ist nicht inventarsichtbar.
56. system_air wird durch RemoveBlock erzeugt.
57. SetBlock(system_air) ist fachlich unzulässig.
58. system_railing besitzt die stabile runtimeBlockTypeId system_railing.
59. system_railing wird als BlockType in die World-Registry gespiegelt.
60. system_railing muss aktiv und nicht gelöscht sein.
61. system_railing muss dem Code-Fingerprint entsprechen.
62. system_railing besitzt keinen global festen Zellwert.
63. Der konkrete Railing-Zellwert ist paletteIndex + 1.
64. system_railing ist aktuell inventarsichtbar und platzierbar.
65. Railing Version 1 rendert als full_cube.
66. Railing Version 1 kollidiert als full_cube.
67. Eine spätere Geländergeometrie darf die stabile Block-ID nicht ändern.
68. Der Systemblock-Bootstrap führt keinen äußeren Commit aus.
69. Default-Seed und DB-Bootstrap besitzen die äußere Transaktion.
70. BlockRegistry.source für die interne Default-Registry ist internal.
71. Bootstrap ist nur Benutzer-/Metadatenkennzeichnung, kein Registry-source-Wert.
72. Readiness erfordert Katalog, Registry, Air und Railing gemeinsam.
73. Ein fehlender oder gedrifteter Railing-Mirror macht Seed-Readiness false.
74. Eine persistente Air-Zeile macht Air-Invariant-Readiness false.
75. Die normale Welt-Blockroute ist die konkrete Editor-Palettenquelle.
76. Die Systemblock-Spezialroute ist Diagnose- und Definitionsquelle.
77. persistentBlocks der Spezialroute enthält den Datenbankmirror.
78. Die derzeit leeren Spezialroutenfelder blocks/inventoryBlocks sind dokumentierte API-Unschärfe.
```

---

### 26.22 Aktualisierte Dateieinordnung

Neu:

```text
src/system_blocks/__init__.py
src/system_blocks/contracts.py
src/system_blocks/catalog.py
src/system_blocks/bootstrap.py
src/system_blocks/air/__init__.py
src/system_blocks/air/definition.py
src/system_blocks/railing/__init__.py
src/system_blocks/railing/definition.py
src/system_blocks/IST-Zustand.md
```

Geändert:

```text
src/bootstrap/default_seed.py
src/bootstrap/db_bootstrap.py
scripts/bootstrap_db.py
routes/blocks.py
```

Noch als nächster Integrationspunkt vorgesehen:

```text
routes/commands.py
```

Später zu prüfen:

```text
routes/projects.py
routes/worlds.py
Editor-Inventar-/Admin-UI
```

---

### 26.23 Aktualisierte Bewertung von `routes/blocks.py`

Neue wichtige Routen:

```text
GET /projects/<project_id>/worlds/<world_id>/blocks/system
GET /blocks/system
```

Erweiterte normale Route:

```text
GET /projects/<project_id>/worlds/<world_id>/blocks
```

Neue Fähigkeiten:

```text
Air als eigener reservierter Zellzustand
Systemblock-Metadaten
Railing-Erkennung
Code-Katalog-Abgleich
Registry-Mirror-Abgleich
Air-/Railing-/Gesamtreadiness
Katalogdiagnostik
Registrydiagnostik
Systemblock-Route-Hints
```

Aktuelle Route-Version:

```text
routeModuleVersion = 0.3.0
```

Antwortversionen:

```text
world-blocks-response.v2
system-blocks-response.v1
blocks-route-status-response.v2
```

---

### 26.24 Aktualisierte Bewertung von `default_seed.py`

Der Default-Seed erzeugt beziehungsweise prüft nun nicht nur:

```text
debug-blocks@1
debug_grass
debug_dirt
dev-project
dev-universe
world_spawn
```

sondern zusätzlich:

```text
Air-Invariante
system_railing-Mirror
Systemblock-Readiness
```

Der Seed bleibt idempotent.

Der Systemblock-Abgleich erfolgt nach dem Sicherstellen der Registry und innerhalb des kontrollierten Seed-Transaktionspfads.

Bei `seed-on-empty-only` darf ein vorhandener Datenbestand nicht allein deshalb als vollständig gelten, weil Registry und Debug-Blöcke existieren. Auch Air und Railing müssen bereit sein.

---

### 26.25 Aktualisierte Bewertung von `db_bootstrap.py`

Zusätzliche Readiness:

```text
systemBlocksReady
systemRailingReady
airInvariantReady
```

Zusätzliche Zähler:

```text
systemBlockCount
systemBlocksCreated
systemBlocksUpdated
systemBlocksMissing
systemBlocksDrifted
```

Zusätzliche Reparaturziele:

```text
fehlender Railing-Mirror
inaktiver Railing-Mirror
gelöschter Railing-Mirror
gedrifteter Railing-Mirror
ungültige Registry-Quelle
```

Harte Fehlerbedingungen:

```text
persistentes system_air
fehlendes system_railing
nicht bereites Systemblock-Paket
nicht bereite Registry
```

---

### 26.26 Aktualisierte Bewertung von `scripts/bootstrap_db.py`

Aktuelle Script-Ergebnisversion:

```text
bootstrap-db-script-result.v5
```

Die Ausgabe normalisiert Systemblock-Werte über:

```text
Preferred Bootstrap-Pfad
Fallback-Pfad
Check-only-Pfad
JSON-Ausgabe
Human-Ausgabe
```

Systemblock-Reconciliation benötigt keinen eigenen CLI-Schalter.

Sie folgt automatisch:

```text
--seed
```

Check-only bleibt read-only:

```text
--check-only
→ keine Erstellung
→ kein Update
→ keine Reparatur
→ nur Status und Readiness
```

---

### 26.27 Bekannte offene Punkte

Noch offen beziehungsweise nicht abschließend bestätigt:

```text
explizite HTTP-400-Regel für SetBlock(system_air)
vollständiger Railing-SetBlock-/Reload-/RemoveBlock-End-to-End-Test
Systemblock-Metadaten in jeder neu erzeugten Chunkpalette
Befüllung von inventoryBlocks der Systemblock-Spezialroute
echte Geländergeometrie
schmalere Geländerkollision
Orientierung
Nachbarverbindungen
Editor-Kennzeichnung als eingebaut/unveränderlich
Admin-UI-Vertrag für persistentBlocks vs inventoryBlocks
```

Diese Punkte ändern nicht die bestätigte Tatsache, dass Katalog, Registry, Air-Invariante und Railing-Mirror aktuell bereit sind und `system_railing` in der normalen Weltpalette ausgegeben wird.

---

### 26.28 Aktualisierter nächster sinnvoller Schritt

Fachlich nächster Integrationspunkt:

```text
services/vectoplan-chunk/routes/commands.py
```

Minimal notwendige Ergänzungen:

```text
SetBlock(system_air) explizit ablehnen
air_requires_remove_block ausgeben
Railing-Systemmetadaten in Paletten erhalten
Command-Status um Systemblockregeln ergänzen
```

Danach:

```text
Railing End-to-End setzen
Chunk neu laden
Railing-Zelle prüfen
Railing entfernen
Chunk neu laden
Air-Zelle prüfen
Editor-Auswahl prüfen
Spezialroute inventoryBlocks konsistent machen
```

---

### 26.29 Aktualisierter Gesamtbefund

Der aktuelle bestätigte Gesamtstand lautet:

```text
vectoplan-chunk startet stabil.
Runtime bleibt read-only.
DB-Bootstrap ist explizit.
Schema ist bereit.
Default-Seed ist bereit.
dev-project ist vorhanden.
dev-universe ist vorhanden.
world_spawn ist vorhanden.
debug-blocks@1 ist aktiv.
Registry-source ist internal.
debug_grass ist vorhanden.
debug_dirt ist vorhanden.
Systemblock-Katalog ist bereit.
system_air ist als cellValue 0 reserviert.
Es existiert keine persistente Air-Zeile.
system_railing ist als aktiver BlockType gespiegelt.
system_railing stimmt mit der Code-Definition überein.
system_railing ist in der normalen Weltpalette enthalten.
system_railing ist inventarsichtbar und platzierbar.
Air-, Railing- und Gesamtreadiness sind true.
```

Damit ist der eingebaute Systemblock-Slice als aktueller IST-Zustand bestätigt.

Die Formulierung „Admin-Blöcke werden angezeigt“ ist für die Oberfläche verständlich. Technisch präzise lautet der Befund:

```text
Der eingebaute Systemblock system_railing wird zusammen mit den Debug-Blöcken
in der normalen Welt-Blockpalette ausgegeben. Air bleibt korrekt separat,
unsichtbar und nicht persistent.
```
