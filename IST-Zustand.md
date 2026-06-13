Ersetze `services/vectoplan-chunk/IST-Zustand.md` vollständig durch diese aktualisierte Fassung.

````markdown
# IST-Zustand.md – VECTOPLAN Chunk Service

<!-- services/vectoplan-chunk/IST-Zustand.md -->

## Status dieser Fassung

Diese Datei beschreibt den aktuellen **IST-Zustand** des `vectoplan-chunk`-Services nach:

- dem PostgreSQL-/Persistenz-Slice
- dem ersten erfolgreichen `SetBlock`-Command-Test
- dem bestätigten `RemoveBlock`-End-to-End-Test
- dem bestätigten Generator-Chunk-Ladepfad
- dem bestätigten Snapshot-Chunk-Ladepfad
- der Diagnose des früheren Worker-/Startup-Hängers
- der Trennung von Runtime-Startup und DB-Bootstrap
- der Einführung neuer Bootstrap-Module
- dem Umbau von `src/bootstrap/startup.py` zu einem read-only Runtime-Startup
- der Stabilisierung von `routes/chunks.py`
- der Stabilisierung von `routes/commands.py`

Diese Datei ist keine Zielarchitektur, sondern eine Bestandsaufnahme des aktuell erreichten Zustands.

Diese Fassung dokumentiert:

- die vorhandene World-/Flat-World-Schicht
- die projektgescopte World-State-API
- die PostgreSQL-Anbindung
- die SQLAlchemy-Models
- die ChunkSnapshot-/ChunkEvent-/WorldCommandLog-Struktur
- den bestätigten Generator-Read-Pfad
- den bestätigten Snapshot-Read-Pfad
- den bestätigten `SetBlock`-Schreibpfad
- den bestätigten `RemoveBlock`-Schreibpfad
- das bestätigte Reload-Verhalten nach Blockänderungen
- die frühere Worker-/Startup-Hänger-Diagnose
- die bestätigte Ursache im Startup-/Auto-Create-/Auto-Seed-Pfad
- den durchgeführten Bootstrap-Umbau
- die neu ergänzten Bootstrap-Dateien
- den aktuellen stabilen Runtime-Start mit 2 Gunicorn-Workern
- die weiterhin offenen Tests für `ReplaceBlock`, `PlaceObject`, `RemoveObject`, Batch-Mix und Editor-Anbindung
- konkrete PowerShell-Testbefehle für Block setzen, Block entfernen, Reload und DB-Prüfung

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
startup.py ist jetzt read-only.
db.create_all() und Default-Seeding laufen nicht mehr im normalen Gunicorn-Worker-Startup.
Explizite DB-Initialisierung wurde in eigene Bootstrap-Module ausgelagert.

routes/chunks.py ist auf Version 0.3.0.
routes/commands.py ist auf Version 0.2.0.
Beide Routenpfade laufen aktuell ohne Timeout und ohne Worker-Kill.
````

Der aktuelle Zustand ist:

```text
Runtime-Startup stabil
→ Statusrouten 200
→ Generator-Chunk lädt
→ Snapshot-Chunk lädt
→ SetBlock persistiert
→ Reload zeigt Änderung
→ RemoveBlock persistiert
→ Reload zeigt Entfernung
→ kein Worker-Timeout
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

4. src/bootstrap + scripts/bootstrap_db.py
   → getrennte Runtime-Startup- und DB-Bootstrap-Schicht
```

Die fachliche Struktur lautet:

```text
Project
→ Universe
→ WorldInstance
→ ChunkSnapshot
→ ChunkEvent
```

Die aktuelle Dev-Struktur ist:

```text
projectId       = dev-project
universeId      = dev-universe
worldId         = world_spawn
templateId      = flat
providerWorldId = flat
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

Docker-Compose enthält aktuell:

```text
vectoplan-editor
vectoplan-library-db
vectoplan-library
vectoplan-chunk-db
vectoplan-chunk
```

Bestätigter Runtime-Start:

```text
Gunicorn Workers = 2
Gunicorn Threads = 2
PostgreSQL socket reachable
Startup hooks laufen read-only
Auto-Create/Auto-Seed werden im Runtime-Start ignoriert
```

Bewertung:

```text
Die Containerstruktur ist richtig erweitert.
Der Chunk-Service wartet beim Start auf PostgreSQL.
PostgreSQL ist als eigener Service angebunden.
Runtime-Startup läuft stabil.
```

---

### 2.2 PostgreSQL-Zustand

Bestätigter Datenbankzustand nach erfolgreichem SetBlock/RemoveBlock-Test:

```text
projects                = 1
universes               = 1
world_instances         = 1
block_types             = 2
chunk_snapshots         = 1
chunk_events            >= 3
world_command_logs      >= 3
world_object_instances  = 0
world_object_chunk_refs = 0
```

Die exakte Anzahl von `chunk_events` und `world_command_logs` steigt mit jedem Testlauf.

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

Bestätigter Snapshot nach SetBlock + RemoveBlock:

```text
snapshotId     = chunk_snap_376cd190fbe44ee887f6649c0cdd35dc
chunkKey       = 0:0:0
chunkVersion   = chunk_rev_000003
chunkRevision  = 3
cellCount      = 4096
status         = active
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

### 2.3 Statusrouten

Bestätigte Statusrouten:

```text
GET /                         → 200
GET /projects/_status          → 200
GET /worlds/_status            → 200
GET /blocks/_status            → 200
GET /chunks/_status            → 200
GET /commands/_status          → 200
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
```

---

### 2.4 Generator-Chunk ist bestätigt

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

### 2.5 Snapshot-Chunk ist bestätigt

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
cell[273] = 1
```

Bewertung:

```text
Der Snapshot-Ladepfad funktioniert.
Der Service lädt den materialisierten Chunk aus PostgreSQL.
Events werden nicht replayt.
Der Snapshot ist die Lade-Wahrheit.
```

---

### 2.6 SetBlock ist bestätigt

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

Bestätigte Chunk-Version:

```text
chunkVersions["0:0:0"] = chunk_rev_000002
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

### 2.7 RemoveBlock ist bestätigt

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

Bestätigte Chunk-Version:

```text
chunkVersions["0:0:0"] = chunk_rev_000003
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

### 2.8 Dirty-Chunks

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
startup.py muss read-only sein.
```

Die Architekturentscheidung lautet:

```text
Runtime startup
→ read-only
→ App, Routes, Models, DB-Ping prüfen
→ keine Tabellen erzeugen
→ keine Default-Daten seeden
→ keine Snapshots/Events/ObjectRefs laden

DB bootstrap
→ explizit
→ optional db.create_all()
→ optional Default-Seeding
→ advisory-lock-geschützt
→ nicht im normalen Gunicorn-Worker-Start
```

Aktueller bestätigter Zustand:

```text
Runtime startet mit 2 Workern stabil.
Auto-Create/Auto-Seed werden im Runtime-Start ignoriert.
Startup-Hooks bleiben aktiv, aber read-only.
```

---

## 5. Bootstrap-Struktur

Bootstrap-Module:

```text
services/vectoplan-chunk/src/bootstrap/settings.py
services/vectoplan-chunk/src/bootstrap/runtime_checks.py
services/vectoplan-chunk/src/bootstrap/db_locks.py
services/vectoplan-chunk/src/bootstrap/schema_bootstrap.py
services/vectoplan-chunk/src/bootstrap/default_seed.py
services/vectoplan-chunk/src/bootstrap/db_bootstrap.py
services/vectoplan-chunk/scripts/bootstrap_db.py
```

Überarbeitet:

```text
services/vectoplan-chunk/src/bootstrap/startup.py
```

### 5.1 `src/bootstrap/settings.py`

Rolle:

```text
Zentrale, robuste Konfigurations- und ENV-Normalisierung.
```

Wichtige Regel:

```text
Legacy-Flags wie VECTOPLAN_CHUNK_AUTO_CREATE_ALL oder
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS werden erkannt,
aber im Runtime-Start nicht mutierend ausgeführt.
```

Bewertung:

```text
Settings sind zentralisiert.
Runtime-DB-Mutationen sind geschützt.
```

---

### 5.2 `src/bootstrap/runtime_checks.py`

Rolle:

```text
Read-only Runtime-Checks.
```

Grenze:

```text
Keine DB-Mutation.
Keine Chunks.
Keine Snapshots.
Keine Events.
Keine ObjectRefs.
Keine rekursive ORM-Serialisierung.
```

Bewertung:

```text
Der normale Startup kann prüfen, ohne produktive Daten zu verändern.
```

---

### 5.3 `src/bootstrap/db_locks.py`

Rolle:

```text
PostgreSQL Advisory Locks für explizite Bootstrap-Mutationen.
```

Ziel:

```text
db.create_all() und Default-Seeding dürfen keine Race-Conditions erzeugen.
```

---

### 5.4 `src/bootstrap/schema_bootstrap.py`

Rolle:

```text
Expliziter Schema-Bootstrap.
```

Grenze:

```text
Kein Seeding.
Keine Chunks.
Keine Snapshots.
Keine Events.
Keine ObjectRefs.
```

---

### 5.5 `src/bootstrap/default_seed.py`

Rolle:

```text
Explizites Default-Seeding.
```

Seed-Daten:

```text
debug-blocks / version 1
debug_grass
debug_dirt
dev-project
dev-universe
world_spawn
```

Grenze:

```text
Keine Snapshots lesen.
Keine Events lesen.
Keine CommandLogs lesen.
Keine WorldObjectInstances lesen.
Keine WorldObjectChunkRefs lesen.
Keine Chunks generieren.
```

---

### 5.6 `src/bootstrap/db_bootstrap.py`

Rolle:

```text
Orchestrator für explizite DB-Initialisierung.
```

Ablauf:

```text
optional Pre-Status
→ Schema-Bootstrap
→ Default-Seed
→ optional Post-Status
→ serialisierbares Gesamtergebnis
```

---

### 5.7 `scripts/bootstrap_db.py`

Rolle:

```text
Ausführbarer Bootstrap-Command für lokale Entwicklung und späteren Init-Container.
```

Typischer Aufruf:

```powershell
python .\services\vectoplan-chunk\scripts\bootstrap_db.py --create-all --seed --json
```

Check-only:

```powershell
python .\services\vectoplan-chunk\scripts\bootstrap_db.py --check-only --json
```

---

### 5.8 `src/bootstrap/startup.py`

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
Der normale Gunicorn-Worker-Start ist deutlich sicherer.
Der frühere Startup-/DB-Mutationsfehler ist architektonisch entschärft.
```

---

## 6. Aktuelle Architekturentscheidung

Die wichtigste Architekturentscheidung bleibt:

```text
Projekt ist nicht dauerhaft gleich Welt.
Projekt ist fachlich ein Universum-Container.
Ein Universum enthält eine oder mehrere Worlds.
Ein neues Projekt startet aktuell mit genau einer Flat-Spawn-World.
```

Technische Umsetzung:

```text
dev-project
→ dev-universe
→ world_spawn
→ templateId = flat
→ providerWorldId = flat
```

Zusätzlich gilt:

```text
Runtime-Start ist nicht mehr DB-Bootstrap.
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
→ seedet Default-Daten
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
            └── serializer.py
```

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
Zentrale Model-Registrierung.
```

Aufgaben:

```text
Project, Universe, WorldInstance, BlockRegistry, BlockType,
ChunkSnapshot, WorldCommandLog, ChunkEvent,
WorldObjectInstance und WorldObjectChunkRef importieren und registrieren.
```

Bestätigt:

```text
Models werden in /chunks/_status und /commands/_status vollständig erkannt.
missingClasses = []
failedModules = []
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
Projektgescopter Read-Pfad ist durch /routes/chunks.py bestätigt.
Snapshot- und Generator-Pfad funktionieren über projektgescopte Routen.
```

---

### 9.2 `src/world_state/bootstrap.py`

Rolle:

```text
Erzeugt Projekt-Bootstrap aus DB-backed Service.
```

Restpunkt:

```text
/projects/dev-project/bootstrap kann weiterhin separat geprüft werden.
Für den bestätigten Block-Slice ist diese Route nicht kritisch.
```

---

### 9.3 `src/world_state/serializer.py`

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
GET /projects
GET /projects/<project_id>
GET /projects/<project_id>/bootstrap
GET /projects/bootstrap
GET /projects/_status
POST /projects/_cache/reset
```

Aktuell wichtig:

```text
/projects/_status ist Healthcheck-Pfad.
Diese Route muss flach und billig bleiben.
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
GET /projects/<project_id>/worlds/<world_id>/chunks
POST /projects/<project_id>/worlds/<world_id>/chunks/batch

GET /projects/<project_id>/chunks
POST /projects/<project_id>/chunks/batch

GET /chunks
POST /chunks/batch

GET /chunks/_status
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

---

### 10.5 `routes/commands.py`

Wichtige Routen:

```text
POST /projects/<project_id>/worlds/<world_id>/commands
POST /projects/<project_id>/commands
POST /commands
GET /commands/_status
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
Legacy VECTOPLAN_EDITOR_* Kompatibilität bleibt vorhanden.
```

Empfohlene Runtime-Defaults:

```text
VECTOPLAN_CHUNK_AUTO_CREATE_ALL=false
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS=false
VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS=false
VECTOPLAN_CHUNK_SEED_DEV_PROJECT=false
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=false
```

Hinweis:

```text
Aktuell werden Auto-Create/Auto-Seed im Runtime-Start bereits ignoriert.
Trotzdem sollten Compose/Config langfristig keine mutierenden Runtime-Flags setzen.
```

Expliziter DB-Bootstrap läuft stattdessen über:

```text
scripts/bootstrap_db.py
src/bootstrap/db_bootstrap.py
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
startup.py ist read-only.
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
Startet Gunicorn.
```

Optionaler späterer Ausbau:

```text
runtime-Modus und db-bootstrap-Modus trennen.
```

Ziel:

```text
runtime
→ Gunicorn starten

db-bootstrap
→ scripts/bootstrap_db.py ausführen
→ Prozess beendet sich
```

---

### 11.6 `docker-compose.yml`

Aktueller Stand:

```text
vectoplan-chunk-db ist ergänzt.
vectoplan-chunk hängt von vectoplan-chunk-db health ab.
PostgreSQL-Volume vectoplan-chunk-postgres-data ist ergänzt.
Chunk-Service bekommt DB-ENV-Werte.
```

Optionaler späterer Ausbau:

```text
vectoplan-chunk-init
→ nutzt dasselbe Image
→ hängt von vectoplan-chunk-db healthy ab
→ führt scripts/bootstrap_db.py aus
→ beendet sich

vectoplan-chunk
→ normaler Runtime-Service
→ kein Auto-Create
→ kein Auto-Seed
```

---

## 12. Aktuelle API-JSON-Struktur

### 12.1 Bootstrap

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
  "spawnWorld": {
    "worldId": "world_spawn",
    "templateId": "flat",
    "providerWorldId": "flat"
  },
  "routeHints": {
    "blocks": "/projects/dev-project/worlds/world_spawn/blocks",
    "chunk": "/projects/dev-project/worlds/world_spawn/chunks",
    "chunksBatch": "/projects/dev-project/worlds/world_spawn/chunks/batch",
    "commands": "/projects/dev-project/worlds/world_spawn/commands"
  }
}
```

---

### 12.2 Blocks

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

---

### 12.3 Chunk ohne Snapshot

Route:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=5&chunkY=0&chunkZ=0
```

Bestätigt:

```text
source = generated
chunkKey = 5:0:0
cellCount = 4096
```

---

### 12.4 Chunk mit Snapshot

Route:

```text
GET /projects/dev-project/worlds/world_spawn/chunks?chunkX=0&chunkY=0&chunkZ=0
```

Bestätigt:

```text
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

### 12.5 Command-Antwort

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

1. `/src/world` ist die neutrale Provider-/Generator-Schicht.
2. `/src/world/flat` ist die erste konkrete Provider-/Template-Welt.
3. `world.json` beschreibt die Flat-Provider-Welt.
4. `flat` ist Provider-/Template-Welt.
5. `world_spawn` ist konkrete Projekt-World.
6. `dev-project` ist aktuelles Dev-Projekt.
7. `dev-universe` ist aktuelles Dev-Universum.
8. Ein Projekt ist ein Universum-Container.
9. Ein Universum enthält eine oder mehrere Worlds.
10. Produktive Routen verwenden `world_spawn`.
11. Provider-/Debug-Routen verwenden weiterhin `flat`.
12. `cellValue = 0` bedeutet Air.
13. `cellValue = paletteIndex + 1` bedeutet Block.
14. `debug_grass` hat `cellValue = 1`.
15. `debug_dirt` hat `cellValue = 2`.
16. `chunkSize = 16`.
17. Ein Chunk hat `4096` Zellen.
18. Zellindex-Reihenfolge ist `x-fastest-y-then-z`.
19. Negative Koordinaten müssen Floor-Division nutzen.
20. Unberührte Chunks werden generiert.
21. Bearbeitete Chunks werden als `ChunkSnapshot` gespeichert.
22. `ChunkSnapshot` ist Lade-Wahrheit.
23. `ChunkEvent` ist historische Wahrheit.
24. Events sind nicht der normale Ladepfad.
25. SetBlock schreibt Snapshot und Event.
26. RemoveBlock schreibt Snapshot und Event.
27. Dirty-Chunks werden für SetBlock und RemoveBlock zurückgegeben.
28. ChunkCommands ändern nicht direkt den Core.
29. Der Editor rendert und sendet Commands, besitzt aber nicht die Wahrheit.
30. PostgreSQL ist der primäre Speicher für Persistenz.
31. Mehrblockobjekte sind modellseitig vorbereitet.
32. Runtime-Startup darf keine DB-Mutation ausführen.
33. `db.create_all()` gehört in den expliziten DB-Bootstrap.
34. Default-Seeding gehört in den expliziten DB-Bootstrap.
35. Statusrouten müssen billig und flach bleiben.
36. Startup darf keine Snapshots, Events, Commands oder ObjectRefs laden.
37. `routes/chunks.py` vermeidet tiefe Relationship-Ladung im Read-Pfad.
38. `routes/commands.py` vermeidet tiefe Relationship-Ladung im Command-Pfad.
39. Ein aktiver Snapshot pro Chunk wird aktualisiert, nicht pro Änderung neu angelegt.
40. ChunkEvents bleiben append-only.

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
```

---

## 15. Bewusst noch nicht stabil oder nicht bestätigt

Noch nicht abschließend bestätigt:

```text
ReplaceBlock End-to-End
PlaceObject End-to-End
RemoveObject End-to-End
Chunk-Batch mit Snapshot-/Generator-Mix
Projekt erstellen/ändern/löschen über API
Editor-RemoteChunkSource
Dirty-Chunks an Chunk-Grenzen
Negative Koordinaten End-to-End
Mehrere gleichzeitige Commands gegen denselben Chunk
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
dedizierter vectoplan-chunk-init Compose-Service
entrypoint-Modus db-bootstrap
```

---

## 16. Bekannte technische Restpunkte

### 16.1 Runtime-Defaults in Config/Compose finalisieren

Aktuell wichtig:

```text
startup.py ist read-only umgebaut.
settings.py schützt Runtime-DB-Mutationen zusätzlich.
```

Noch sinnvoll:

```text
config.py und docker-compose.yml so anpassen,
dass Runtime-Services standardmäßig keine Auto-Create/Auto-Seed-Flags aktivieren.
```

Empfohlen:

```text
VECTOPLAN_CHUNK_AUTO_CREATE_ALL=false
VECTOPLAN_CHUNK_AUTO_SEED_DEFAULTS=false
VECTOPLAN_CHUNK_SEED_DEBUG_BLOCKS=false
VECTOPLAN_CHUNK_SEED_DEV_PROJECT=false
VECTOPLAN_CHUNK_ALLOW_RUNTIME_DB_MUTATIONS=false
```

---

### 16.2 Init-Pfad in Docker ergänzen

Noch sinnvoll:

```text
vectoplan-chunk-init
```

Ziel:

```text
DB startet
→ Init-Service führt scripts/bootstrap_db.py aus
→ Runtime-Service startet read-only
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
```

---

### 16.4 Cell-Payload ist weiterhin unkomprimiert

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
```

---

### 16.5 Event-Modell funktioniert, ist aber noch nicht zusätzlich gehärtet

Aktuell:

```text
models/event.py funktioniert für SetBlock und RemoveBlock.
```

Optional später:

```text
Relationship-Lazy-Strategie in event.py härten
Serializer rekursionssicherer machen
Event-/Command-Validation ausbauen
```

Für den ersten stabilen Stand ist das aktuell nicht zwingend.

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
docker logs --tail 120 vectoplan-chunk
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

### 17.3 Status JSON prüfen

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

### 17.4 DB-Bootstrap Check-only

```powershell
python .\services\vectoplan-chunk\scripts\bootstrap_db.py --check-only --json
```

---

### 17.5 DB-Bootstrap explizit

Nur ausführen, wenn Schema/Seed bewusst initialisiert werden soll:

```powershell
python .\services\vectoplan-chunk\scripts\bootstrap_db.py --create-all --seed --json
```

---

### 17.6 Generator-Chunk ohne Snapshot

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

### 17.7 Generator-Fallback deaktivieren

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

### 17.8 Snapshot-Chunk laden

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
cell[273]=1
```

Hinweis:

```text
cell[273]=1 stammt aus dem ersten historischen SetBlock-Test.
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

Wenn der Wert schon `1` ist, zuerst `RemoveBlock` ausführen oder eine andere Testposition verwenden.

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

### 19.1 Snapshots prüfen

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

### 19.2 Events prüfen

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

### 19.3 CommandLogs prüfen

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

## 20. Optionaler Grenztest für Dirty-Chunks

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

## 21. Nächster sinnvoller Schritt

Der erste Backend-Slice ist ausreichend stabil für den nächsten Entwicklungsabschnitt.

Bestätigt ist:

```text
Runtime-Startup stabil
Statusrouten 200
Generator-Chunk lädt
Snapshot-Chunk lädt
SetBlock persistiert
Reload zeigt gesetzten Block
RemoveBlock persistiert
Reload zeigt entfernten Block
CommandLog wird geschrieben
ChunkEvent wird geschrieben
Snapshot wird aktualisiert
kein Worker-Timeout im bestätigten Ablauf
```

Nächste sinnvolle Schritte:

```text
1. Editor-RemoteChunkSource anbinden.
2. Editor SetBlock/RemoveBlock gegen den Chunk-Service senden lassen.
3. Dirty-Chunks nach Commands neu laden.
4. Grenz-Dirty-Chunks testen.
5. Negative Koordinaten testen.
6. Chunk-Batch mit Generator-/Snapshot-Mix testen.
```

Optional technische Härtung:

```text
1. config.py und docker-compose.yml Runtime-Defaults finalisieren.
2. event.py später zusätzlich härten.
3. models/chunk.py später zusätzlich härten.
4. Alembic-Migrationen vorbereiten.
5. vectoplan-chunk-init als Compose-Service ergänzen.
```

Nicht mehr zwingend für den ersten Stand:

```text
Startup-Hooks komplett deaktivieren.
event.py sofort härten.
Weitere DB-Modelle vor Editor-Anbindung umbauen.
```

---

## 22. Aktueller Gesamtbefund

Der aktuelle Gesamtbefund lautet:

```text
Der Chunk-Service hat den Schritt von reiner Generator-/World-State-API
zu einem PostgreSQL-gestützten Persistenzsystem erfolgreich begonnen.
```

Bestätigt ist:

```text
Docker-Service startet.
PostgreSQL-Service existiert.
Modelle sind erstellt.
Default-Projekt-/Universe-/World-Struktur ist vorbereitet.
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
startup.py wurde zu read-only Runtime-Startup umgebaut.
DB-Create und Default-Seeding wurden in explizite Bootstrap-Module ausgelagert.
routes/chunks.py ist stabilisiert.
routes/commands.py ist stabilisiert.
```

Aktuell noch zu bestätigen:

```text
ReplaceBlock End-to-End
PlaceObject End-to-End
RemoveObject End-to-End
Chunk-Batch mit Snapshot-/Generator-Mix
Grenz-Dirty-Chunks
Negative Koordinaten
Editor-RemoteChunkSource
```

Der erreichte erste stabile Stand lautet:

```text
Editor/Client kann Chunk laden
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

Damit ist der erste Backend-Stand für die editierbare Chunk-Welt ausreichend belastbar.

```

Die Datei ist damit auf den bestätigten Stand aktualisiert. Für den ersten Backend-Slice reicht das aus; als nächstes sollte die Editor-Anbindung gegen diese stabilen Routen folgen.
```
