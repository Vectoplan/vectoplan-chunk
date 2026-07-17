<!-- services/vectoplan-chunk/routes/IST-Zustand.md -->

# IST-Zustand – `services/vectoplan-chunk/routes`

## Status dieser Fassung

Stand: 2026-07-17  
Status: Aktualisierte vollständige Bestandsaufnahme der Flask-Route-Schicht einschließlich Project Access, atomarem Flat-/Earth-Provisioning und der zuletzt bestätigten Runtime-/DB-Integration des `vectoplan-chunk`-Services.



Diese Fassung aktualisiert den zuvor dokumentierten Stand, ohne bestehende Kapitel oder Detailbereiche zu entfernen. Neu aufgenommen beziehungsweise nachgeführt wurden insbesondere:

```text
routes/project_access.py als verpflichtender produktiver Blueprint
→ Rollen, Gruppen, Mitgliedschaften und Rollenzuweisungen

routes/__init__.py 0.6.0
→ Project-Access-Featuregate, Pflichtstatus und Route-Surface-Validierung

routes/projects.py 0.5.1
→ Provisioning-Responses v2, Access-Readiness und App-Link-Löschung

atomare Earth-Provisionierung
→ kompakte Earth-Referenz
→ kanonischer GlobalReferencePoint
→ EPSG:4979 / WKT2:2019
→ Earth-v1-Grid
→ referenzbasierter Spawn
→ idempotenter Wiederholungsrequest

bestätigter Runtimezustand
→ alle Pflicht-Blueprints registriert
→ Prestart erfolgreich
→ Project Access ready
→ Earth- und Flat-Provider discovery-/generierungsfähig
```

Diese Datei beschreibt den Ordner:

```text
services/vectoplan-chunk/routes/
```

Ziel ist, dass Entwickler ohne vollständiges Lesen aller Route-Dateien erkennen können:

```text
welche Blueprints existieren
welche HTTP-Endpunkte registriert werden
welche Endpunkte lesen oder schreiben
welche Models und Servicekomponenten verwendet werden
wie Projekt-, World-, Chunk- und Commandpfade aufgebaut sind
wo Transaktionen beginnen und enden
welche Debug- und Legacy-Routen existieren
welche Architekturgrenzen aktuell eingehalten oder überschritten werden
welche Risiken und offenen Integrationspunkte bestehen
```

Die Dokumentation unterscheidet zwischen:

```text
implementiert
→ im vorliegenden Python-Code vorhanden

statisch geprüft
→ Python-Syntax der vorliegenden Datei erfolgreich geprüft

bestätigt
→ im übergeordneten Service-IST bereits über reale HTTP-/DB-Tests bestätigt

vorbereitet
→ Route oder Ablauf ist implementiert, aber noch nicht vollständig End-to-End bestätigt
```

---

## 1. Kurzfassung

Der Ordner `routes/` enthält zehn Python-Dateien:

```text
eine zentrale Blueprint-Registry
sechs produktive API-Blueprints
zwei Debug-Blueprints
einen optionalen Legacy-Editor-Blueprint
```

Die zehn Dateien umfassen im aktuell dokumentierten Quellstand:

```text
19.526 Quellcodezeilen
566 Top-Level-Funktionen
81 deklarierte HTTP-Routen
```

Der reale App-Startup meldete zuletzt 83 URL-Regeln. Die Differenz zu den 81 expliziten Route-Dekoratoren entsteht durch Flask-/App-eigene Regeln und ist kein fehlender Blueprint.

Die expliziten Route-Dekoratoren teilen sich auf in:

```text
46 produktive projektgescopte oder Provisioning-/Access-Endpunkte
15 Default-, Entwicklungs- oder Kompatibilitätsendpunkte
8 Diagnose-/Operationsendpunkte
11 Debug-Endpunkte
1 optionalen Legacy-Editor-Endpunkt
```

Die produktive API folgt grundsätzlich dieser Struktur:

```text
/projects/<project_id>
└── /worlds/<world_id>
    ├── /blocks
    ├── /blocks/system
    ├── /chunks
    ├── /chunks/batch
    └── /commands
```

Die wichtigste Lese-/Schreibtrennung lautet:

```text
projects.py
→ Projektgraph, Provisioning und Bootstrap

project_access.py
→ Rollen, Gruppen, Mitgliedschaften und Rollenzuweisungen
→ vorbereitet für spätere Autorisierung, aktuell authzEnforced=false

worlds.py
→ konkrete WorldInstance-Metadaten und CRUD

blocks.py
→ Registry-, Palette- und Systemblock-Lesezugriff

chunks.py
→ Snapshot lesen oder Provider-Chunk generieren
→ optional generierten Chunk materialisieren

commands.py
→ Block- und Objektänderungen ausführen
→ Snapshot, Event und CommandLog schreiben
```

---

## 2. Ordner- und Dateistruktur

```text
services/
└── vectoplan-chunk/
    └── routes/
        ├── __init__.py
        │   ├── zentrale Blueprint-Spezifikationen
        │   ├── defensive Modulimporte
        │   ├── genau-einmal-Registrierung
        │   ├── Config-Gates für Debug und Legacy
        │   └── Routing-Metadaten in app.extensions
        │
        ├── projects.py
        │   ├── Projekt-CRUD
        │   ├── Project + Universe + WorldInstance erzeugen
        │   ├── App-Projekt-Provisioning
        │   ├── atomare Flat-/Earth-Provisionierung
        │   ├── Project-Access-Bootstrap einbeziehen
        │   ├── Editor-Bootstrap
        │   └── Schema-/Seed-/DB-/Access-Status
        │
        ├── project_access.py
        │   ├── Access-Zusammenfassung und Initialisierung
        │   ├── ProjectRole-CRUD
        │   ├── ProjectGroup-CRUD
        │   ├── ProjectGroupMember-CRUD
        │   ├── ProjectRoleAssignment-CRUD
        │   ├── Status- und Cache-Reset-Routen
        │   └── keine Authentifizierung/Autorisierungsdurchsetzung
        │
        ├── worlds.py
        │   ├── WorldInstance auflisten
        │   ├── WorldInstance erzeugen
        │   ├── WorldInstance lesen und ändern
        │   ├── WorldInstance soft-löschen
        │   └── World-Route-Status
        │
        ├── blocks.py
        │   ├── BlockRegistry und BlockType lesen
        │   ├── Editorpalette serialisieren
        │   ├── system_air bereitstellen
        │   ├── system_railing-Mirrorstatus lesen
        │   └── Block-/Systemblock-Status
        │
        ├── chunks.py
        │   ├── einen Chunk laden
        │   ├── mehrere Chunks laden
        │   ├── ChunkSnapshot bevorzugen
        │   ├── Provider-Fallback ausführen
        │   ├── optional generierten Chunk materialisieren
        │   └── Chunk-Route-Status
        │
        ├── commands.py
        │   ├── SetBlock
        │   ├── RemoveBlock
        │   ├── ReplaceBlock
        │   ├── PlaceObject
        │   ├── RemoveObject
        │   ├── ChunkSnapshot schreiben
        │   ├── ChunkEvent schreiben
        │   ├── WorldCommandLog schreiben
        │   └── Command-Route-Status
        │
        ├── world_test.py
        │   ├── Debug-HTML-Oberfläche
        │   ├── World-Discovery
        │   ├── Provider-/Palette-Diagnose
        │   ├── direkte Provider-Chunkgenerierung
        │   └── Koordinatentest inklusive negativer Koordinaten
        │
        ├── earth_debug.py
        │   ├── Earth-v1-Referenz erzeugen
        │   ├── Global-zu-Lokal und Lokal-zu-Global testen
        │   ├── Spawnauflösung testen
        │   └── Earth-Chunkgenerierung testen
        │
        ├── editor.py
        │   ├── optionaler Legacy-Blueprint
        │   ├── GET /editor
        │   ├── Jinja-Template ausliefern
        │   └── Inline-Fallback-Shell ausliefern
        │
        └── IST-Zustand.md
            └── diese Dokumentation
```

---

## 3. Statische Bestandsaufnahme

Alle neun vorliegenden Dateien sind syntaktisch gültiges Python.

Eine erfolgreiche Syntaxprüfung bedeutet nicht, dass:

```text
alle optionalen Imports zur Laufzeit verfügbar sind
alle Blueprints erfolgreich importiert werden
alle Datenbanktabellen aktuell sind
alle Endpunkte End-to-End getestet wurden
```

| Datei | Zeilen | Top-Level-Funktionen | Routen | Hauptrolle |
|---|---:|---:|---:|---|
| `__init__.py` | 1.247 | 43 | 0 | Blueprint-Registry, Project-Access-Surface-Validierung und Diagnose |
| `projects.py` | 3.402 | 99 | 15 | Projektgraph, Flat-/Earth-Provisioning, Access-Bootstrap und Bootstrap |
| `project_access.py` | 3.088 | 81 | 26 | Rollen, Gruppen, Mitgliedschaften, Assignments und Access-Diagnose |
| `worlds.py` | 1.663 | 60 | 11 | WorldInstance-CRUD |
| `blocks.py` | 1.994 | 63 | 6 | Registry-, Palette- und Systemblock-Lesen |
| `chunks.py` | 2.283 | 68 | 7 | Snapshot-/Provider-Chunklesen |
| `commands.py` | 3.424 | 103 | 4 | produktiver Schreibpfad |
| `world_test.py` | 1.577 | 30 | 10 | World-Debugoberfläche |
| `earth_debug.py` | 127 | 2 | 1 | Earth-v1-Debugpfad |
| `editor.py` | 721 | 17 | 1 | optionale Legacy-Editor-Shell |

Größte Einzelbereiche:

```text
commands.py
→ vollständige Command-Ausführung einschließlich Objektlogik

projects.py
→ Projektgraph, Provisioning, Access-Readiness, Bootstrap und Diagnose

project_access.py
→ vollständige vorbereitete Project-Access-HTTP-Oberfläche
→ 26 Routen in einer Datei

world_test.py
→ 735 Zeilen Inline-HTML/CSS/JavaScript in einer Funktion
```

---

## 4. Blueprint-Übersicht

| Blueprint | Datei | Kategorie | Pflicht | Config-Gate |
|---|---|---|---|---|
| `projects` | `projects.py` | produktiv | ja | immer |
| `project_access` | `project_access.py` | produktiv-access | standardmäßig ja | `VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES`; Pflichtstatus über `VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_ROUTES` |
| `worlds` | `worlds.py` | produktiv | ja | immer |
| `blocks` | `blocks.py` | produktiv | ja | immer |
| `chunks` | `chunks.py` | produktiv | ja | immer |
| `commands` | `commands.py` | produktiv | ja | immer |
| `world_test` | `world_test.py` | Debug | nein | `VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES` |
| `earth_debug` | `earth_debug.py` | Debug | nein | `VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES` |
| `editor` | `editor.py` | Legacy | nein | `VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES` |

Aktuelle Registry-Version:

```text
routes registry = 0.6.0
```

Modulversionen:

```text
projects.py       = 0.5.1
project_access.py  = 1.0.1
blocks.py         = 0.3.0
chunks.py   = 0.3.0
worlds.py   = 0.2.0
commands.py = 0.2.0
world_test.py = 0.1.0
```

`earth_debug.py` und `editor.py` besitzen keinen einheitlichen `ROUTE_MODULE_VERSION`-Vertrag.

---

## 5. Zentrale API-Struktur

```text
Project
├── Project Access
│   ├── ProjectRole
│   ├── ProjectGroup
│   │   └── ProjectGroupMember
│   └── ProjectRoleAssignment
│
└── Universe
    └── konkrete WorldInstance
        ├── BlockRegistry / BlockType lesen
        ├── ChunkSnapshot lesen
        ├── Provider-Chunk generieren
        └── Commands ausführen
```

Wichtige ID-Semantik:

```text
project_id
→ öffentliche Chunk-Projekt-ID

universe_id
→ öffentliche Universe-ID innerhalb eines Projekts

world_id
→ konkrete editierbare WorldInstance innerhalb eines Universums

template_id / provider_id / provider_world_id
→ Provider- und Templateidentität
```

Korrekt:

```text
world_id          = world_spawn
template_id       = flat
provider_id       = flat
provider_world_id = flat
```

Nicht korrekt:

```text
world_id = flat
```


Project-Access-ID-Semantik:

```text
project_id in URL
→ öffentliche Chunk-Projekt-ID
→ wird intern zu Project.id aufgelöst

user_id / subjectId
→ externe opaque String-ID
→ kein Foreign Key zu vectoplan-app oder Auth-Datenbanken

role_ref / group_ref
→ öffentliche ID oder fachlicher Key innerhalb genau eines Projekts

assignment_id
→ öffentliche Zuweisungs-ID innerhalb des Projektkontexts
```

Aktuelle Autorisierungsgrenze:

```text
authzEnforced = false
```

Die Routen persistieren und verwalten den Access-Vertrag, treffen aber noch keine Entscheidung, ob der aktuelle HTTP-Aufrufer eine Operation ausführen darf.


---

## 6. Vollständiger Endpunktkatalog

## 6.1 Produktive und Provisioning-Endpunkte

| Methode | Pfad | Datei | Aufgabe |
|---|---|---|---|
| GET | `/projects/preview/by-app/<app_project_public_id>` | `projects.py` | deterministische Chunk-IDs ohne DB-Schreibzugriff vorschauen |
| GET | `/projects/by-app/<app_project_public_id>` | `projects.py` | vorhandenes Chunk-Projekt über App-Projekt-ID lesen |
| PUT | `/projects/by-app/<app_project_public_id>` | `projects.py` | Chunk-Projekt idempotent für App-Projekt sicherstellen |
| POST | `/projects/by-app/<app_project_public_id>` | `projects.py` | Alias für idempotentes App-Projekt-Provisioning |
| POST | `/projects/ensure` | `projects.py` | Projektgraph idempotent aus Requestbody sicherstellen |
| DELETE | `/projects/by-app/<app_project_public_id>` | `projects.py` | verknüpften Chunk-Projektgraph über die App-Projekt-ID soft-löschen |
| GET | `/projects/<project_id>/bootstrap` | `projects.py` | Editor-Bootstrap für Projekt, Access, Universe und Spawnwelt liefern |
| GET | `/projects` | `projects.py` | Chunk-Projekte auflisten |
| POST | `/projects` | `projects.py` | Project + Default-Universe + world_spawn direkt erzeugen |
| GET | `/projects/<project_id>` | `projects.py` | Projektdetail lesen |
| PATCH | `/projects/<project_id>` | `projects.py` | Project-Felder ändern |
| DELETE | `/projects/<project_id>` | `projects.py` | Project, Universes und Worlds soft-löschen |
| GET | `/projects/<project_id>/worlds` | `worlds.py` | konkrete WorldInstances auflisten |
| POST | `/projects/<project_id>/worlds` | `worlds.py` | konkrete WorldInstance erzeugen |
| GET | `/projects/<project_id>/worlds/<world_id>` | `worlds.py` | konkrete WorldInstance lesen |
| PATCH | `/projects/<project_id>/worlds/<world_id>` | `worlds.py` | World-Metadaten und Provider-/Generatorzuordnung ändern |
| DELETE | `/projects/<project_id>/worlds/<world_id>` | `worlds.py` | WorldInstance soft-löschen |
| GET | `/projects/<project_id>/worlds/<world_id>/blocks` | `blocks.py` | Registry und Editorpalette lesen |
| GET | `/projects/<project_id>/worlds/<world_id>/blocks/system` | `blocks.py` | Systemblockkatalog und Persistenz-Mirrorstatus lesen |
| GET | `/projects/<project_id>/worlds/<world_id>/chunks` | `chunks.py` | einzelnen Chunk aus Snapshot oder Provider laden |
| POST | `/projects/<project_id>/worlds/<world_id>/chunks/batch` | `chunks.py` | mehrere Chunks aus Snapshot oder Provider laden |
| POST | `/projects/<project_id>/worlds/<world_id>/commands` | `commands.py` | Block- oder Objektcommand ausführen |


---

## 6.2 Project-Access-Endpunkte

Alle Project-Access-Routen sind projektgescopt. Sie verwenden die lokale `Project.id` intern, geben aber die öffentliche `project_id` als HTTP-Identität aus.

### Zusammenfassung und Initialisierung

| Methode | Pfad | Aufgabe |
|---|---|---|
| GET | `/projects/<project_id>/access` | Rollen, Gruppen, Mitgliedschaften, Assignments und Owner-Zustand zusammenfassen |
| PUT | `/projects/<project_id>/access/initialize` | Defaultrollen und Owner-Zuweisung idempotent initialisieren oder reparieren |
| POST | `/projects/<project_id>/access/initialize` | Alias für dieselbe idempotente Initialisierung |

### Rollen

| Methode | Pfad | Aufgabe |
|---|---|---|
| GET | `/projects/<project_id>/roles` | Projektrollen auflisten |
| POST | `/projects/<project_id>/roles` | benutzerdefinierte Projektrolle erzeugen |
| GET | `/projects/<project_id>/roles/<role_ref>` | Rolle über ID oder Key lesen |
| PUT | `/projects/<project_id>/roles/<role_ref>` | Rolle idempotent sicherstellen |
| PATCH | `/projects/<project_id>/roles/<role_ref>` | veränderbare benutzerdefinierte Rolle patchen |
| DELETE | `/projects/<project_id>/roles/<role_ref>` | benutzerdefinierte Rolle soft-löschen |

Die vier Defaultrollen sind geschützt:

```text
owner
admin
editor
viewer
```

System-/Defaultrollen dürfen über die generischen CRUD-Routen nicht in ihren kanonischen Feldern verändert oder gelöscht werden.

### Gruppen und Mitgliedschaften

| Methode | Pfad | Aufgabe |
|---|---|---|
| GET | `/projects/<project_id>/groups` | Gruppen auflisten |
| POST | `/projects/<project_id>/groups` | Gruppe erzeugen |
| GET | `/projects/<project_id>/groups/<group_ref>` | Gruppe lesen |
| PUT | `/projects/<project_id>/groups/<group_ref>` | Gruppe idempotent sicherstellen |
| PATCH | `/projects/<project_id>/groups/<group_ref>` | Gruppe patchen |
| DELETE | `/projects/<project_id>/groups/<group_ref>` | Gruppe soft-löschen |
| GET | `/projects/<project_id>/groups/<group_ref>/members` | Gruppenmitgliedschaften auflisten |
| POST | `/projects/<project_id>/groups/<group_ref>/members` | externen User zur Gruppe hinzufügen |
| PUT | `/projects/<project_id>/groups/<group_ref>/members/<user_id>` | Mitgliedschaft idempotent sicherstellen |
| DELETE | `/projects/<project_id>/groups/<group_ref>/members/<user_id>` | User aus Gruppe entfernen beziehungsweise Mitgliedschaft soft-löschen |

### Rollenzuweisungen

| Methode | Pfad | Aufgabe |
|---|---|---|
| GET | `/projects/<project_id>/assignments` | User- und Gruppenrollenzuweisungen auflisten |
| POST | `/projects/<project_id>/assignments` | Rolle an User oder Gruppe zuweisen |
| GET | `/projects/<project_id>/assignments/<assignment_id>` | Zuweisung lesen |
| PATCH | `/projects/<project_id>/assignments/<assignment_id>` | Status, Zeitraum oder Overrides ändern |
| DELETE | `/projects/<project_id>/assignments/<assignment_id>` | Zuweisung widerrufen oder soft-löschen |

Owner-Invariante:

```text
Rolle owner
→ darf nur dem aktuellen Project.owner_id als User-Subject zugewiesen werden
→ keine generische Owner-Übertragung über Assignment-CRUD
```

Project-Access-Responses enthalten ausdrücklich:

```text
authzEnforced = false
externalUserForeignKeys = false
```


---

## 6.3 Default-, Entwicklungs- und Kompatibilitätsendpunkte

| Methode | Pfad | Datei | Delegation |
|---|---|---|---|
| GET | `/projects/bootstrap` | `projects.py` | Bootstrap des konfigurierten Default-Projekts |
| GET | `/worlds` | `worlds.py` | Projekt-Weltliste mit `project_id=default` |
| POST | `/worlds` | `worlds.py` | Welt im Default-Projekt erzeugen |
| GET | `/worlds/<world_id>` | `worlds.py` | Welt aus Default-Projekt lesen |
| PATCH | `/worlds/<world_id>` | `worlds.py` | Welt aus Default-Projekt ändern |
| DELETE | `/worlds/<world_id>` | `worlds.py` | Welt aus Default-Projekt soft-löschen |
| GET | `/projects/<project_id>/blocks` | `blocks.py` | Palette der Spawn-/Defaultwelt des Projekts |
| GET | `/blocks` | `blocks.py` | Palette des Default-Projekts |
| GET | `/blocks/system` | `blocks.py` | Systemblöcke des Default-Projekts |
| GET | `/projects/<project_id>/chunks` | `chunks.py` | Einzelchunk der Spawnwelt des Projekts |
| POST | `/projects/<project_id>/chunks/batch` | `chunks.py` | Chunkbatch der Spawnwelt des Projekts |
| GET | `/chunks` | `chunks.py` | Einzelchunk über Default-/Query-Projekt und -Welt |
| POST | `/chunks/batch` | `chunks.py` | Chunkbatch über Default-/Query-Projekt und -Welt |
| POST | `/projects/<project_id>/commands` | `commands.py` | Command gegen Spawnwelt des Projekts |
| POST | `/commands` | `commands.py` | Command gegen Default-Projekt und Spawnwelt |

Diese Routen sind für Entwicklung und Kompatibilität vorgesehen.

Produktiver Editorcode sollte bevorzugt die vollständigen projektgescopten Pfade verwenden.

---

## 6.4 Diagnose- und Operationsendpunkte

| Methode | Pfad | Datei | Aufgabe |
|---|---|---|---|
| GET | `/projects/_status` | `projects.py` | DB-, Schema-, Seed-, Defaultgraph-, Project-Access-, Model- und Provisioningstatus |
| POST | `/projects/_cache/reset` | `projects.py` | optionale World-State-/Provisioning-Caches zurücksetzen |
| GET | `/project-access/_status` | `project_access.py` | Model-, Service-, Schema-, Defaultrollen-, Owner- und Route-Surface-Readiness prüfen |
| POST | `/project-access/_cache/reset` | `project_access.py` | ausschließlich Project-Access-Service-/Route-Caches zurücksetzen |
| GET | `/worlds/_status` | `worlds.py` | World-Routen, Modelle, Counts und Config prüfen |
| GET | `/blocks/_status` | `blocks.py` | Registry-, Systemblock- und Air-Invarianten prüfen |
| GET | `/chunks/_status` | `chunks.py` | Snapshot-/Chunkroute, Counts und Config prüfen |
| GET | `/commands/_status` | `commands.py` | Command-, Event-, Objekt- und Snapshotstatus prüfen |

---

## 6.5 World-Test-Debugendpunkte

Blueprint-Prefix:

```text
/world-test
```

| Methode | Pfad | Aufgabe |
|---|---|---|
| GET | `/world-test/` | interaktive HTML-Debugseite |
| GET | `/world-test` | dieselbe Seite ohne abschließenden Slash |
| GET | `/world-test/api/health` | Debugroute und World-Discovery prüfen |
| GET | `/world-test/api/worlds` | Worldprovider entdecken |
| GET | `/world-test/api/worlds/<world_id>` | WorldDefinition und Metadaten lesen |
| GET | `/world-test/api/worlds/<world_id>/blocks` | Providerpalette lesen |
| GET | `/world-test/api/worlds/<world_id>/chunks` | Providerchunk direkt generieren |
| GET | `/world-test/api/chunks` | Chunk mit `worldId` als Queryparameter generieren |
| GET | `/world-test/api/coords` | Welt-, Chunk- und lokale Koordinaten berechnen |
| GET | `/world-test/api/worlds/<world_id>/raw` | rohes Discovery-Ergebnis lesen |

---

## 6.6 Earth-Debugendpunkt

Blueprint-Prefix:

```text
/debug/earth
```

| Methode | Pfad | Aufgabe |
|---|---|---|
| GET | `/debug/earth` | Earth-Referenz, Koordinatenkonvertierung, Spawn und Chunkgenerierung testen |

---

## 6.7 Optionaler Legacy-Editor-Endpunkt

| Methode | Pfad | Aufgabe |
|---|---|---|
| GET | `/editor` | Editor-Template oder Inline-Fallback-Shell ausliefern |

---

## 7. `routes/__init__.py` – zentrale Blueprint-Registry

## 7.1 Aufgabe

`routes/__init__.py` enthält ausschließlich Blueprint-Wiring und Diagnostik.

Es soll ausdrücklich nicht:

```text
Chunks generieren
Datenbankzustand ändern
Worlds auflösen
HTML erzeugen
fachliche Commands ausführen
```

### Registrierungsreihenfolge

```text
projects
→ project_access
→ worlds
→ blocks
→ chunks
→ commands
→ world_test
→ earth_debug
→ editor
```

Die Reihenfolge ist bewusst stabil.

### Pflicht- und optionale Module

```text
produktive Blueprints
→ required=True
→ Import-/Registrierungsfehler blockieren den App-Start

Project Access
→ standardmäßig enabled=true und required=true
→ kann über zwei kompatible Config-Key-Paare gesteuert werden
→ bei Pflichtstatus wird zusätzlich die vollständige Core-Route-Surface geprüft

Debug- und Legacy-Blueprints
→ required=False
→ Fehler werden diagnostiziert und übersprungen
```

### Config-Gates

```text
VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES
VECTOPLAN_CHUNK_PROJECT_ACCESS_ROUTES_ENABLED
→ steuern Registrierung der Project-Access-Routen
→ Default im Code: true

VECTOPLAN_CHUNK_REQUIRE_PROJECT_ACCESS_ROUTES
VECTOPLAN_CHUNK_PROJECT_ACCESS_ROUTES_REQUIRED
→ steuern, ob fehlende Project-Access-Routen den Startup blockieren
→ Default im Code: true

VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES
→ steuert world_test und earth_debug
→ Default im Code: true

VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES
→ steuert editor
→ Default im Code: true
```

### Genau-einmal-Registrierung

Die Registry prüft:

```text
internes Tracking-Set
→ Blueprint bereits durch diese Registry erfasst?

app.blueprints
→ Blueprint bereits direkt auf Flask-App registriert?

erst danach
→ app.register_blueprint(...)
```


### Project-Access-Route-Surface-Validierung

Die Registry prüft für den `project_access`-Blueprint nicht nur den Blueprintnamen, sondern mindestens diese Core-Regeln:

```text
/project-access/_status
/projects/<project_id>/access
/projects/<project_id>/access/initialize
/projects/<project_id>/roles
/projects/<project_id>/groups
/projects/<project_id>/assignments
```

Metadaten:

```text
projectAccessApiEnabled
projectAccessRoutesRequired
projectAccessBlueprintRegistered
projectAccessRouteSurfaceReady
projectAccessCoreRouteRules
projectAccessMissingRouteRules
projectAccessAuthzEnforced = false
```

Bestätigter Fehlerfall:

```text
Pflichtmodul nicht importierbar
→ Blueprint nicht registrierbar
→ Prestart schlägt fehl
→ Gunicorn startet nicht
```

Bestätigter Erfolgsfall nach Wiederherstellung:

```text
project_access Blueprint registriert
→ Core-Route-Surface vollständig
→ Prestart erfolgreich
→ Service healthy
```


### Routing-Metadaten

Die Registry speichert Zustand unter:

```text
app.extensions[<namespace>]["routing"]
```

Standardnamespace:

```text
vectoplan_chunk
```

Zusätzlich existiert ein Legacy-Alias:

```text
vectoplan_editor
```

Gespeicherte Informationen:

```text
Registry-Version
Blueprint-Spezifikationen
registrierte Blueprintnamen
Erfolge
Fehler
übersprungene Module
Featureflags
produktive/Debug-/Legacy-Modullisten
```

### Öffentliche Funktionen

```text
register_blueprints(app)

get_blueprint_specs()
iter_blueprint_specs()

get_registered_blueprint_names(app)
get_blueprint_registration_records(app)
get_blueprint_registration_errors(app)
get_blueprint_registration_successes(app)
get_blueprint_registration_skipped(app)
get_routing_metadata(app)
get_project_access_route_status(app)

reset_route_import_cache()
```

### Diagnosedatentypen

```text
BlueprintSpec
→ gewünschtes Modul, Attribut, Prefix, Pflichtstatus und Kategorie

BlueprintRegistrationRecord
→ tatsächliches Ergebnis des Registrierungsversuchs
```

---

## 8. Gemeinsame Route-Konventionen

## 8.1 Standardantwort produktiver JSON-Routen

Die produktiven Module verwenden überwiegend:

```json
{
  "ok": true,
  "responseVersion": "...",
  "...": "...",
  "metadata": {
    "routeSource": "routes.<modul>",
    "routeModuleVersion": "..."
  }
}
```

Standardfehler:

```json
{
  "ok": false,
  "responseVersion": "error-response.v1",
  "error": {
    "code": "route_error",
    "message": "..."
  },
  "metadata": {
    "routeSource": "routes.<modul>",
    "routeModuleVersion": "..."
  }
}
```

Debugdetails können aktiviert werden durch:

```text
?debug=true
?includeDebug=true
VECTOPLAN_CHUNK_ROUTE_DEBUG_ERRORS=true
current_app.debug = true
```

---

## 8.2 CamelCase und snake_case

Viele Query- und Bodyfelder akzeptieren beide Formen:

```text
includeDeleted / include_deleted
universeId / universe_id
worldId / world_id
blockTypeId / block_type_id
materializeGenerated / materialize_generated
```

Das verbessert Kompatibilität, vergrößert aber die Anzahl möglicher Eingabeformen.

---

## 8.3 Default-Aliase

Projektalias:

```text
""
default
_default
current
_current
dev
_dev
```

Universealias:

```text
""
default
_default
current
_current
dev
_dev
```

Worldalias:

```text
""
default
_default
spawn
_spawn
current
_current
```

Konfigurierte Defaults:

```text
Project  = dev-project
Universe = dev-universe
World    = world_spawn
Provider = flat
```

---

## 8.4 JSON-Sicherheit

Mehrere Module besitzen eigene defensive JSON-Konvertierung.

`chunks.py` und `commands.py` schützen zusätzlich gegen:

```text
zyklische Referenzen
zu große Rekursionstiefe
Bytesobjekte
unbekannte ORM-/Providerobjekte
```

Maximale Standardtiefe:

```text
80
```

---

## 8.5 ORM-Relationship-Schutz

Kritische Chunk- und Commandpfade verwenden:

```python
query.options(noload("*"))
```

Ziel:

```text
Project
→ Universes
→ Worlds
→ Snapshots
→ Events
→ Commands
→ Objects
```

darf nicht versehentlich als vollständiger ORM-Graph geladen werden.

Dieser Schutz ist in `chunks.py` und `commands.py` explizit implementiert.

Andere Route-Dateien verwenden nicht durchgängig denselben Schutz.


## 8.6 Autorisierungsmetadaten

Die neu vorbereitete Project-Access-Oberfläche und die Project-Provisioning-Antworten geben ausdrücklich aus:

```text
authzEnforced = false
```

Bedeutung:

```text
Rollen-/Gruppen-/Assignment-Daten werden bereits persistent gepflegt.

Die Route entscheidet aber noch nicht anhand des aktuellen HTTP-Aufrufers,
ob eine Operation erlaubt ist.
```

Dieser Wert darf nicht als Berechtigungserfolg interpretiert werden.

## 8.7 JSON-Request-Vertrag

Project-Access-Mutationsrouten lesen den Body über Flask `request.get_json(...)` und erwarten ein JSON-Objekt. Der korrigierte Stand verhindert, dass ein nicht importiertes oder falsch aufgelöstes Requestobjekt erst beim ersten Mutationsrequest einen Laufzeitfehler erzeugt.

```text
leerer Body bei Route ohne Pflichtfelder
→ {}

JSON-Array oder Scalar
→ kontrollierter 400-Fehler

ungültiges JSON
→ kontrollierter Requestfehler
```


---

## 9. `projects.py`

## 9.1 Aufgabe

`projects.py` ist der HTTP-Einstieg für:

```text
Projekt-CRUD
Projektgraph-Erzeugung
App-Projekt-Verknüpfung
idempotentes Flat-/Earth-Provisioning
Project-Access-Initialisierung im Provisioning
App-Link-basierte Soft-Löschung
Editor-Bootstrap
Service-, Schema-, Seed- und Access-Readiness
```

Das Modul schreibt keine Chunks, Events oder Commands.

Es schreibt aber:

```text
Project
Universe
WorldInstance
```

---

## 9.2 Antwortversionen

```text
project-response.v2
project-list-response.v2
project-create-response.v2
project-patch-response.v2
project-delete-response.v2
project-bootstrap-response.v2
projects-route-status-response.v4
projects-route-cache-reset-response.v1
project-provision-response.v2
project-provision-preview-response.v2
project-by-app-response.v2
```

---

## 9.3 Direkte Projektanlage

Endpunkt:

```text
POST /projects
```

Ablauf:

```text
Requestbody lesen
→ Project.from_create_payload() oder Fallback erzeugen
→ Project zur Session hinzufügen
→ flush für Project.id

→ Default-Universe erzeugen
→ flush für Universe.id

→ world_spawn erzeugen
→ Universe- und Project-Defaultreferenzen setzen

→ gemeinsamer Commit
```

Die direkte Anlage ist nicht idempotent.

Duplicate-/Unique-Fehler werden über Textvergleich erkannt und als HTTP 409 ausgegeben.

Für App-Projekte wird stattdessen das Provisioning empfohlen.

---

## 9.4 App-Projekt-Provisioning

Primärer Integrationsendpunkt:

```text
PUT /projects/by-app/<app_project_public_id>
```

Alias:

```text
POST /projects/by-app/<app_project_public_id>
```

Ablauf:

```text
Requestbody lesen
→ ensure_chunk_project_for_app_project(...)
→ session=db.session
→ commit=True
→ ProvisioningResult in HTTP-Antwort umwandeln
```

Der Pfad ist idempotent implementiert und durch reale PostgreSQL-/HTTP-Tests bestätigt.


Der aktuelle Provisioning-Service erzeugt oder repariert in einer Transaktion:

```text
Project
→ vier Defaultrollen
→ Owner-Zuweisung
→ Universe
→ konkrete WorldInstance world_spawn
```

Unterstützte Templates:

```text
flat
→ Standard

earth
→ explizite earthReference erforderlich
```

Earth-Requestvertrag, kompakte Form:

```json
{
  "worldTemplate": "earth",
  "earthReference": {
    "crs": "EPSG:4979",
    "longitude": 13.405,
    "latitude": 52.52,
    "height": 45,
    "coordinateOrder": "longitude-latitude-height",
    "alwaysXY": true
  }
}
```

Interne Normalisierung:

```text
kompakter Request
→ GlobalCoordinate mit Decimalwerten
→ resolve_crs(...)
→ kanonische WKT2:2019-Definition
→ EarthGridReference vectoplan-earth-grid@1
→ GlobalReferencePoint
→ to_persistence_dict()
→ Domain-Fingerprint
```

Bestätigte persistierte Earth-Werte:

```text
world_id          = world_spawn
template_id       = earth
provider_id       = earth
provider_world_id = earth
min_y             = -1024
max_y             = 8192
spawn_coordinate_space = local_metric
```

Der Integer-Spawn wird aus den präzisen lokalen Spawnwerten mit mathematischem Floor abgeleitet. Generische Flat-Defaults überschreiben den Earth-Spawn nicht mehr.

PostgreSQL-Locking:

```text
Lock-Lookups deaktivieren eager Joins
→ with_for_update(of=<Basismodell>)
→ FOR UPDATE OF projects/universes/world_instances
```

Damit wird der PostgreSQL-Fehler vermieden:

```text
FOR UPDATE cannot be applied to the nullable side of an outer join
```

Bestätigte Idempotenz:

```text
erster Request
→ chunk_project_provisioned
→ created=true

Reparatur eines älteren inkonsistenten Earth-Zustands
→ chunk_project_updated
→ updated=true

unmittelbar identischer Folgeaufruf
→ chunk_project_exists
→ created=false
→ updated=false
```


Body-basierte Alternative:

```text
POST /projects/ensure
```

Nur lesen:

```text
GET /projects/by-app/<app_project_public_id>
```

Dry-Run:

```text
GET /projects/preview/by-app/<app_project_public_id>
```

---

## 9.5 Editor-Bootstrap

Endpunkt:

```text
GET /projects/<project_id>/bootstrap
```

Antwort enthält typischerweise:

```text
projectId / chunkProjectId
universeId / chunkUniverseId
defaultWorldId
spawnWorldId
chunkWorldId

project
universe
spawnWorld
world
worlds
routeHints
context

optional access
→ Rollen
→ Gruppen
→ Owner-Zuweisung
→ accessInitialized
→ authzEnforced=false
```

Route-Hints zeigen auf:

```text
Project
Worlds
World
Blocks
Chunk
Chunkbatch
Commands
```

---

## 9.6 Projektliste

Endpunkt:

```text
GET /projects
```

Wichtige Queryparameter:

```text
includeDeleted=false
includeArchived=true
includeUniverses=true
includeWorlds=true
includeMetadata=true
includeInternal=false
q / search
limit=100, maximal 1000
offset=0
```

`includeUniverses=true` und `includeWorlds=true` können bei vielen Projekten umfangreiche Folgeabfragen und große Antworten erzeugen.

---

## 9.7 Patch

Endpunkt:

```text
PATCH /projects/<project_id>
```

Ablauf:

```text
Project laden
→ Project.apply_patch_payload(...)
→ db.session.add(project)
→ commit
```

Die Route ändert nur Project-Felder.

Universe- und World-Änderungen gehören in eigene Routen.

---

## 9.8 Soft-Delete

Endpunkt:

```text
DELETE /projects/<project_id>
```

Ablauf:

```text
Project soft-löschen
→ alle Universes des Projekts soft-löschen
→ alle WorldInstances des Projekts soft-löschen
→ gemeinsamer Commit
```


### App-Link-basierte Löschung

Endpunkt:

```text
DELETE /projects/by-app/<app_project_public_id>
```

Die Route löst zuerst das lokale Chunk-Projekt über `Project.external_app_project_id` auf und delegiert anschließend an denselben projektgescopten Soft-Delete-Vertrag.

```text
keine physische Löschung
→ keine Cross-Service-FK
→ App-Projekt-ID bleibt ein externer Stringbezug
```


Nicht soft-gelöscht werden:

```text
ChunkSnapshots
WorldCommandLogs
ChunkEvents
```

Diese bleiben für Historie, Audit und spätere Trainings-/Analysepfade erhalten.

---

## 9.9 Projektstatus

Endpunkt:

```text
GET /projects/_status
```

Prüft:

```text
Datenbankkonfiguration und Verbindung
Bootstrapstatus
Schema-Readiness
Seed-Readiness
Default-Project
Default-Universe
Default-World
Modelregistrierung
Tabellenzählungen
Settings
Provisioning-Verfügbarkeit und Provisioning-Vertragsversion
Project-Access-Models/Service/Schema
Defaultrollen und Default-Owner-Zuweisung
projectAccessSchemaReady
defaultProjectAccessReady
authzEnforced=false
optional sicheren Environment-Snapshot
```

Anders als die übrigen Statusrouten prüft diese Route die DB-Verbindung standardmäßig.

Queryparameter:

```text
checkDatabase=true
includeModels=true
includeCounts=true
includeConfig=true
includeSettings=true
includeEnv=false
```

Die Route antwortet HTTP 200 auch dann, wenn:

```text
ok=false
serviceReady=false
```

Der Readinesszustand muss daher aus dem Body ausgewertet werden.

---

## 9.10 Cache-Reset

Endpunkt:

```text
POST /projects/_cache/reset
```

Versucht optional folgende Caches zurückzusetzen:

```text
src.world_state.bootstrap
src.world_state.service
src.world_state.resolver
src.world_state.defaults
```

PostgreSQL-Zustand wird nicht verändert.


---

## 10. `project_access.py`

## 10.1 Aufgabe

`project_access.py` ist der vorbereitete produktive HTTP-Adapter für projektbezogene Rollen, Gruppen, Mitgliedschaften und Rollenzuweisungen.

```text
Project
├── ProjectRole
├── ProjectGroup
│   └── ProjectGroupMember
└── ProjectRoleAssignment
```

Das Modul:

```text
liest und validiert HTTP-Eingaben
→ löst das lokale Project auf
→ ruft src.project_access.service auf
→ besitzt pro Mutation genau eine Commit-/Rollback-Grenze
→ serialisiert flache JSON-Antworten
```

Das Modul macht bewusst nicht:

```text
keine Authentifizierung
keine Autorisierungsdurchsetzung
keine Tabellenanlage
keine Migration
keinen globalen Seed
keine Chunk-/Snapshot-/Command-/Event-/Objektabfragen
```

## 10.2 Versionen und Blueprint

```text
Blueprint                 = project_access
ROUTE_MODULE_VERSION      = 1.0.1
Response                  = project-access-route-response.v1
Error                     = project-access-route-error.v1
Status                    = project-access-route-status.v1
```

Der Blueprint ist in der zentralen Registry produktiv und standardmäßig verpflichtend.

## 10.3 Defaultrollen

```text
owner
admin
editor
viewer
```

Die Initialisierungsroute synchronisiert diese Rollen aus kanonischen Templates. Sie kann gelöschte Defaultrollen reaktivieren und stellt die Owner-Zuweisung her.

Berechtigungskern:

```text
owner
→ vollständige Projektverwaltung einschließlich transfer

admin
→ Projektverwaltung ohne automatische Eigentumsübertragung
→ deny: transfer

editor
→ view, edit, embed

viewer
→ view
```

## 10.4 Geschützte Systemrollen

Generische Rolle-CRUD-Routen dürfen kanonische Defaultrollen nicht driften lassen.

Geschützte Felder umfassen unter anderem:

```text
name
description
permissions
isSystem
status
metadata
```

```text
Default-/Systemrolle
→ kein generisches Patchen dieser Felder
→ kein generisches Löschen
→ Synchronisierung ausschließlich über Access-Initializer/Service
```

## 10.5 Gruppen und Mitgliedschaften

Gruppen sind immer über die lokale `project_db_id` projektspezifisch.

Mitgliedschaften verwenden:

```text
user_id
→ externe opaque String-ID
→ keine FK zu Auth oder vectoplan-app
```

Wichtige Invarianten:

```text
Gruppe und Mitgliedschaft müssen dasselbe Projekt besitzen.

Eine aktive Usermitgliedschaft pro Gruppe/User-Identität wird idempotent wiederverwendet.

Löschen erfolgt fachlich als Status-/Soft-Delete-Operation.
```

## 10.6 Rollenzuweisungen

Subjecttypen:

```text
user
group
```

Eine Zuweisung verknüpft innerhalb desselben Projekts:

```text
ProjectRole
→ User-ID oder ProjectGroup
```

Unterstützte Zustände:

```text
active
inactive
revoked
deleted
```

Zusätzlich vorbereitet:

```text
startsAt
expiresAt
permissionOverrides
assignedByUserId
revokedByUserId
revocationReason
```

Owner-Sonderregel:

```text
owner-Rolle
→ nur subjectType=user
→ subjectId muss Project.owner_id entsprechen
```

Eine Eigentumsübertragung ist kein generischer Assignment-Patch.

## 10.7 Transaktionsgrenze

Servicefunktionen dürfen flushen, aber nicht committen oder rollbacken.

```text
HTTP-Mutationsroute
→ Serviceoperation(en)
→ db.session.commit()

bei ProjectAccessServiceError / IntegrityError / SQLAlchemyError
→ db.session.rollback()
→ stabiler JSON-Fehler
```

Read-Routen führen keinen Commit aus.

## 10.8 Idempotenz

Idempotente stabile Identitäten:

```text
Defaultrolle über roleKey
Gruppe über groupId/groupKey
Mitgliedschaft über project + group + user
Assignment über role + subject
Access-Initialisierung über Project
```

Mutationsergebnisse enthalten Statistiken:

```text
created
updated
reactivated
reused
revoked
removed
deleted
skipped
changed
```

## 10.9 Statusroute

```text
GET /project-access/_status
```

Prüft beziehungsweise beschreibt:

```text
Blueprint- und Modulversion
Modelvertrag
Servicevertrag
Project-Access-Schema-Readiness
Defaultrollen
Default-Owner-Zuweisung
Routekatalog
Cachezustand
externe User-FK = false
authzEnforced = false
```

Bestätigter Laufzeittest:

```text
status = ready
routeModuleVersion = 1.0.1
Access-Schema ready
Defaultrollen und Owner-Zuweisung vorhanden
```

## 10.10 Cache-Reset

```text
POST /project-access/_cache/reset
```

Zurückgesetzt werden ausschließlich reproduzierbare In-Process-Caches der Route und des Access-Service.

Nicht verändert werden:

```text
ProjectRole-Zeilen
ProjectGroup-Zeilen
Mitgliedschaften
Assignments
Project-Owner
Schema
```

## 10.11 Autorisierungsgrenze

Jede Antwort macht den aktuellen Stand sichtbar:

```text
authzEnforced = false
```

Die Datenbasis für spätere Berechtigungsentscheidungen ist vorhanden. Eine Route darf daraus noch nicht ableiten, dass der aktuelle Caller autorisiert ist.


---

## 11. `worlds.py`

## 11.1 Aufgabe

`worlds.py` verwaltet konkrete `WorldInstance`-Zeilen.

Es soll nicht:

```text
Chunks generieren
Commands ausführen
Snapshots schreiben
Events schreiben
```

---

## 11.2 Antwortversionen

```text
world-response.v1
world-list-response.v1
world-create-response.v1
world-patch-response.v1
world-delete-response.v1
worlds-route-status-response.v1
```

---

## 11.3 Worldliste

```text
GET /projects/<project_id>/worlds
```

Queryparameter:

```text
universeId
includeDeleted=false
includeArchived=true
includeMetadata=true
includeInternal=false
includeRouteHints=true
q / search
limit=100, maximal 1000
offset=0
apiPrefix
allowDefaultProject
```

Die Liste kann projektweit oder auf ein Universe begrenzt werden.

---

## 11.4 Worldanlage

```text
POST /projects/<project_id>/worlds
```

Ablauf:

```text
Project laden
→ Universe laden
→ WorldInstance.create(...)
→ flush
→ Universe.default_world_id bei Bedarf setzen
→ Universe.spawn_world_id bei Bedarf setzen
→ Commit
```

Body kann unter anderem enthalten:

```text
worldId
universeId
name
slug
description
worldType
worldRole
templateId
providerId
providerWorldId
generatorType
generatorVersion
projectionType
topologyType
coordinateSystem
chunkSize
cellSize
surfaceY
minY
maxY
seed
blockRegistryId
blockRegistryVersion
spawnX
spawnY
spawnZ
setAsDefaultWorld
setAsSpawnWorld
metadata
```

Wichtiger aktueller Stand:

```text
_create_world_for_universe()
→ übergibt keinen globalReference-Vertrag
→ übergibt keine präzisen Earth-Spawnfelder
```

Damit ist eine vollständige produktive Earth-Welterzeugung über die generische `worlds.py`-Create-Route weiterhin nicht sauber verkabelt.

Die produktive Earth-Erzeugung ist inzwischen jedoch über den bevorzugten idempotenten App-Provisioning-Pfad in `projects.py` plus `src/world_state/provisioning.py` bestätigt. Diese beiden Aussagen sind zu trennen:

```text
PUT /projects/by-app/<app_project_public_id>
→ Earth vollständig unterstützt und bestätigt

POST /projects/<project_id>/worlds
→ generischer Create-Pfad noch ohne vollständigen Earth-Referenzadapter
```

---

## 11.5 World lesen

```text
GET /projects/<project_id>/worlds/<world_id>
```

Antwort enthält:

```text
WorldInstance
Provider-/Templatekontext
Blockregistrykontext
Chunk-/Zellkonfiguration
Route-Hints
```

---

## 11.6 World patchen

```text
PATCH /projects/<project_id>/worlds/<world_id>
```

Ablauf:

```text
WorldInstance.apply_patch_payload(...)
→ optional als Defaultworld setzen
→ optional als Spawnworld setzen
→ Commit
```

Wichtige Grenze:

```text
Änderungen an Provider-, Generator-, Topologie- oder Koordinatenkonfiguration
migrieren vorhandene ChunkSnapshots nicht.
```

Eine solche Änderung kann bestehende materialisierte Chunks semantisch von der neuen Weltkonfiguration entkoppeln.

---

## 11.7 World soft-löschen

```text
DELETE /projects/<project_id>/worlds/<world_id>
```

Ablauf:

```text
WorldInstance.soft_delete()
→ Universe.default_world_id löschen, wenn betroffen
→ Universe.spawn_world_id löschen, wenn betroffen
→ Commit
```

Bestehende Snapshots, Commands und Events bleiben erhalten.

Aktuelle Konsistenzlücke:

```text
Project.default_world_id
Project.spawn_world_id
```

werden in dieser Route nicht aktualisiert.

Dadurch können Project-Level-Referenzen nach dem Löschen einer Welt veraltet bleiben.

---

## 11.8 Default-/Kompatibilitätsrouten

```text
GET    /worlds
POST   /worlds
GET    /worlds/<world_id>
PATCH  /worlds/<world_id>
DELETE /worlds/<world_id>
```

Diese delegieren an die projektgescopten Endpunkte mit dem Default-Projekt.

---

## 11.9 Worldstatus

```text
GET /worlds/_status
```

Liefert:

```text
Datenbankstatus
Modelstatus
Counts
Default-IDs
Provider-/Template-Defaults
World-Route-Metadaten
```

---

## 12. `blocks.py`

## 12.1 Aufgabe

`blocks.py` ist ein read-only HTTP-Adapter für:

```text
BlockRegistry
BlockType
Editorpalette
system_air
system_railing
Systemblockkatalog
Systemblock-Mirrorstatus
```

Es erzeugt oder repariert keine Systemblockzeilen.

---

## 12.2 Antwortversionen

```text
world-blocks-response.v2
system-blocks-response.v1
blocks-route-status-response.v2
```

---

## 12.3 Normale Palette

```text
GET /projects/<project_id>/worlds/<world_id>/blocks
```

Ablauf:

```text
Project / Universe / WorldInstance auflösen
→ Registry über world.block_registry_id + version laden
→ BlockTypes filtern
→ deterministisch sortieren
→ Palette neu durchnummerieren
→ cellValue = computedPaletteIndex + 1
→ Air separat als cellValue 0 ausgeben
```

Queryparameter:

```text
universeId
includeDeleted=false
includeInactive=false
includeContext=false
includeMetadata=true
includeRaw=true
includeRouteHints=true
q / search
apiPrefix
```

Wichtig:

```text
default_palette_index
→ Sortierempfehlung

computedPaletteIndex
→ tatsächliche Position der Antwortpalette

cellValue
→ computedPaletteIndex + 1
```

---

## 12.4 Air

`system_air` wird codegeführt serialisiert.

Harte Regeln:

```text
cellValue = 0
blockTypeId = null
persistAsBlockType = false
placeable = false
collidable = false
renderMode = invisible
shapeType = empty
```

Ein persistenter `BlockType` mit `block_type_id=system_air` ist illegal.

---

## 12.5 Systemblöcke

```text
GET /projects/<project_id>/worlds/<world_id>/blocks/system
```

Antwort kombiniert:

```text
codegeführten Systemblockkatalog
Air-Definition
Codeblockdefinitionen
Inventory-Blöcke
persistente BlockType-Mirrors
Katalogstatus
Registry-Mirrorstatus
Readinessflags
```

Readiness:

```text
catalogReady
registryReady
airInvariantReady
systemRailingReady
systemBlocksReady
```

Die Route ist ausdrücklich read-only.

Fehlt die Systemblock-API vollständig:

```text
HTTP 503
code = system_blocks_unavailable
```

---

## 12.6 Dynamischer Systemblockimport

Gesuchte Importwurzeln:

```text
src.system_blocks
system_blocks
```

Benötigte Exporte:

```text
serialize_air_for_world_blocks_route
get_system_block_definition
serialize_system_block_catalog
serialize_system_block_definition_from_catalog
get_system_block_catalog_status
build_system_block_bootstrap_status_for_registry
```

Erfolgreiche Funktionsauflösung wird gecacht.

Datenbankzeilen werden nicht gecacht.

---

## 12.7 Bekannte Antwortunschärfe

Die Spezialroute übernimmt:

```text
blocks
inventoryBlocks
definitions
```

direkt aus dem Codekatalog.

Der persistente `system_railing`-Mirror wird separat unter:

```text
persistentBlocks
```

ausgegeben.

Wenn der Codekatalog `blocks` beziehungsweise `inventoryBlocks` leer liefert, können die Top-Level-Felder leer bleiben, obwohl der persistente Mirror korrekt vorhanden ist.

---

## 12.8 Blockstatus

```text
GET /blocks/_status
```

Prüft unter anderem:

```text
BlockRegistries
aktive Registries
BlockTypes
aktive BlockTypes
System-BlockTypes
illegal persistiertes system_air
system_railing-Mirrors
Systemblockimporte
Katalogstatus
Default-Registry-Mirrorstatus
```

---

## 13. `chunks.py`

## 13.1 Aufgabe

`chunks.py` implementiert den produktiven Chunk-Lesepfad:

```text
Project / Universe / WorldInstance auflösen
→ ChunkSnapshot suchen
→ Snapshot zurückgeben, falls vorhanden
→ sonst Providerchunk generieren
→ optional explizit materialisieren
```

Events werden beim normalen Laden nicht replayt.

---

## 13.2 Antwortversionen

```text
world-state-chunk-response.v1
world-state-chunk-batch-response.v1
chunks-route-status-response.v1
```

---

## 13.3 Einzelchunk

```text
GET /projects/<project_id>/worlds/<world_id>/chunks
```

Erforderliche Queryparameter:

```text
chunkX
chunkY
chunkZ
```

Optionale Queryparameter:

```text
universeId
includeDeleted=false

preferSnapshot=true
allowGenerated=true
materializeGenerated=false

includeContext=false
includeSnapshot=true
includeRouteHints=true

userId
sessionId
apiPrefix
```

Ablauf:

```text
Snapshot bevorzugt?
├── ja
│   └── aktiven Snapshot über world_db_id + x/y/z suchen
│       └── gefunden → source=snapshot
└── nein oder nicht gefunden
    └── generierten Fallback erlaubt?
        ├── nein → HTTP 404
        └── ja
            └── WorldService.generate_chunk(provider_world_id, x, y, z)
                └── source=generated
```

---

## 13.4 Optionales Materialisieren

Mit:

```text
materializeGenerated=true
```

wird ein generierter Chunk als `ChunkSnapshot` gespeichert.

Ablauf:

```text
Providerchunk generieren
→ ChunkSnapshot.create_for_world(...)
→ db.session.add()
→ flush
→ Commit in der Route
```

Wichtige HTTP-Semantik:

```text
GET kann mit diesem Queryparameter einen Datenbank-Schreibzugriff ausführen.
```

Das widerspricht der üblichen Erwartung, dass GET sicher und schreibfrei ist.

---

## 13.5 Batch

```text
POST /projects/<project_id>/worlds/<world_id>/chunks/batch
```

Unterstützte Bodyformen:

```json
{
  "chunks": [
    {"chunkX": 0, "chunkY": 0, "chunkZ": 0}
  ]
}
```

Alternativ:

```text
items
requests
```

Einträge dürfen auch Dreierelement-Arrays sein.

Duplikate werden über `chunkKey` entfernt.

Standardmaximum:

```text
256 Chunks
```

Konfigurierbar über:

```text
VECTOPLAN_CHUNK_ROUTE_MAX_BATCH_CHUNKS
```

---

## 13.6 Batchfehler

```text
continueOnError=false
→ bei erstem Fehler abbrechen
→ Session rollback
→ HTTP 400

continueOnError=true
→ weitere Chunks bearbeiten
→ Fehler und Erfolge gemeinsam zurückgeben
→ HTTP 207
```

Aktueller Transaktionsstand:

```text
Sobald mindestens ein Fehler vorliegt,
wird die gesamte Route zurückgerollt.
```

Dadurch können im Response bereits gezählte:

```text
createdSnapshots
createdSnapshot=true
```

auf tentative, anschließend zurückgerollte Snapshotobjekte verweisen.

---

## 13.7 Provideradapter

`chunks.py` importiert dynamisch:

```text
src.world.service.get_default_world_service
```

Unterstützte Signaturen:

```python
generate_chunk(world_id, chunk_x, chunk_y, chunk_z)
generate_chunk(
    world_id=...,
    chunk_x=...,
    chunk_y=...,
    chunk_z=...
)
```

Es werden bewusst keine beliebigen weiteren Signaturen ausprobiert.

Als `world_id` wird aktuell verwendet:

```text
WorldInstance.provider_world_id
```

---

## 13.8 Snapshotnormalisierung

Snapshot und Providerergebnis werden in eine gemeinsame Runtimeform überführt:

```text
projectId
universeId
worldId
templateId
providerId
providerWorldId
providerSourceWorldId

chunkX
chunkY
chunkZ
chunkKey
source

runtimeContentVersion
cellIndexOrder
cellEncoding
palette
cells
cellCount

blockRegistryId
blockRegistryVersion
coordinateSystem
projectionType
topologyType
chunkSize
cellSize
```

Snapshotzusatz:

```text
snapshotId
chunkRevision
chunkVersion
contentHash
objectRefs
```

---

## 13.9 Earth-Grenze

Der Projektgraph kann inzwischen eine vollständig persistierte instance-spezifische Earth-Referenz enthalten. Der aktuelle Chunk-Routeadapter reicht diese Referenz bei der Providergenerierung jedoch noch nicht vollständig als konkrete Earth-Providerinstanz weiter.

Providergenerierung verwendet nur:

```text
provider_world_id
chunk_x
chunk_y
chunk_z
```

Im Provisioning bereits integriert:

```text
WorldInstance.global_reference_json
WorldInstance.global_reference_fingerprint
konkrete Earth-WorldInstance world_spawn
Earth-v1 min_y/max_y
referenzbasierter lokaler Spawn
```

Im produktiven Chunk-Lesepfad noch nicht vollständig integriert:

```text
GlobalReferencePoint an konkrete Earth-Providerinstanz übergeben
periodische X-Kanonisierung vor Chunk-Key
periodische X-Kanonisierung vor Snapshotlookup
Alias-Deduplizierung vor Snapshotlookup und Batch
```

---

## 13.10 Chunkstatus

```text
GET /chunks/_status
```

Liefert:

```text
Snapshotcounts
ObjectRef-Snapshotcounts
Batchlimit
Cellencoding
DB- und Modelstatus
noload-Verfügbarkeit
produktive und Convenience-Routen
```

---

## 14. `commands.py`

## 14.1 Aufgabe

`commands.py` ist der erste produktive Schreibpfad für die editierbare Welt.

Es implementiert nicht nur einen dünnen HTTP-Adapter, sondern aktuell auch:

```text
Commandnormalisierung
Koordinatenumrechnung
Chunkzellenmutation
Palettenmutation
Providerfallback
Snapshot-Upsertlogik
Dirty-Chunk-Berechnung
Eventerzeugung
Mehrblockobjektplatzierung
Mehrblockobjektentfernung
Transaktionsabschluss
```

---

## 14.2 Antwortversionen

```text
world-command-response.v1
commands-route-status-response.v1
```

---

## 14.3 Primärer Endpunkt

```text
POST /projects/<project_id>/worlds/<world_id>/commands
```

Queryparameter:

```text
universeId
includeDeleted=false
includeCommandLog=true
```

Bodygrundstruktur:

```json
{
  "type": "SetBlock",
  "position": {
    "x": 1,
    "y": 1,
    "z": 1
  },
  "userId": "user_123",
  "sessionId": "session_abc"
}
```

---

## 14.4 Unterstützte Commands

```text
SetBlock
RemoveBlock
ReplaceBlock
PlaceObject
RemoveObject
```

Akzeptierte Aliase umfassen unter anderem:

```text
set_block
set-block
remove_block
break_block
replace_block
place_object
remove_object
```

---

## 14.5 Gesamttransaktion

```text
Requestbody lesen
→ Project / Universe / WorldInstance ohne Relationshipgraph laden
→ WorldCommandLog erzeugen und flushen
→ Command ausführen
→ ChunkSnapshot erzeugen oder aktualisieren
→ ChunkEvent(s) erzeugen
→ CommandLog auf applied/noop setzen
→ Response serialisieren
→ gemeinsamer Commit
```

Bei Fehler:

```text
Rollback
→ HTTP-Fehlerantwort
```

---

## 14.6 Welt-zu-Chunk-Koordinaten

```text
chunkCoord = worldCoord // chunkSize
localCoord = worldCoord - chunkCoord * chunkSize
```

Python-`//` verwendet mathematische Floor-Division.

Beispiel:

```text
worldX = -1
chunkSize = 16

chunkX = -1
localX = 15
```

Zellindex:

```text
index = localX
      + chunkSize * (
          localY
          + chunkSize * localZ
        )
```

Reihenfolge:

```text
x-fastest-y-then-z
```

---

## 14.7 SetBlock

Erforderlich:

```text
type = SetBlock
position
blockTypeId
```

Ablauf:

```text
Snapshot laden oder Providerchunk generieren
→ BlockType in World-Registry laden
→ active und placeable prüfen
→ Block in Chunkpalette finden oder ergänzen
→ cellValue = paletteIndex + 1
→ Zelle schreiben
→ Stats aktualisieren
→ Snapshot speichern
→ block_change-Event schreiben
→ Dirty-Chunks berechnen
```

Wenn die Zelle bereits denselben Wert besitzt:

```text
CommandStatus = noop
kein Snapshotwrite
kein Event
```

---

## 14.8 RemoveBlock

Erforderlich:

```text
type = RemoveBlock
position
```

Ablauf:

```text
aktuelle Zelle lesen
→ BlockType über Palette auflösen
→ breakable prüfen
→ cellValue auf 0 setzen
→ Snapshot schreiben
→ Event schreiben
```

Air wird nicht als BlockType geladen.

---

## 14.9 ReplaceBlock

Erforderlich:

```text
type = ReplaceBlock
position
blockTypeId
```

Die Route verwendet denselben Pfad wie `SetBlock`.

Aktueller Unterschied zu `RemoveBlock`:

```text
der vorhandene Block wird nicht auf breakable geprüft.
```

Ein unzerstörbarer Block kann daher über `ReplaceBlock` überschrieben werden, sofern die neue Blockart `placeable` ist.

---

## 14.10 Dirty-Chunks

Für eine geänderte Zelle wird immer der Zielchunk dirty.

Liegt die lokale Zelle an einer Chunkgrenze, werden alle berührenden Nachbarchunks ergänzt.

Beispiele:

```text
localX = 0
→ Chunk x-1 zusätzlich dirty

localX = chunkSize-1
→ Chunk x+1 zusätzlich dirty

Ecke
→ bis zu 8 Chunkkombinationen
```

Earth-periodische X-Nachbarn werden aktuell nicht kanonisiert.

---

## 14.11 PlaceObject

Aktueller Objektvertrag:

```text
achsenparalleler rechteckiger Quader
ein Fill-BlockType
```

Erforderlich:

```text
type = PlaceObject
position als Anchor
blockTypeId als Fill-Block
```

Optional:

```text
objectInstanceId
objectTypeId
objectVariantId
dimensions
rotation
transform
```

Grenzen:

```text
maximale betroffene Zellen = 65.536
maximale Größe X = 256
maximale Größe Y = 256
maximale Größe Z = 256
```

Ablauf:

```text
alle belegten Weltzellen aufbauen
→ nach Chunk gruppieren

WorldObjectInstance erzeugen
→ für jeden Chunk:
   Snapshot laden/generieren
   alle Objektzellen auf Fill-Block setzen
   ObjectRef in Chunkinhalt ergänzen
   Snapshot speichern
   WorldObjectChunkRef erzeugen
   ChunkEvent erzeugen

→ CommandLog aktualisieren
→ gemeinsamer Commit
```

Pro berührtem Chunk entsteht:

```text
ein Snapshotzustand
ein WorldObjectChunkRef
ein ChunkEvent
```

---

## 14.12 Aktuelle PlaceObject-Grenzen

Die Route prüft derzeit nicht:

```text
ob belegte Zellen bereits zu einem anderen Objekt gehören
ob bestehende Blöcke breakable oder replaceable sind
ob eine Kollision fachlich erlaubt ist
ob ein Objektfootprint aus der Library stammt
ob Rotation den Footprint verändert
```

Die Platzierung überschreibt abweichende Zellwerte direkt.

---

## 14.13 RemoveObject

Erforderlich:

```text
type = RemoveObject
objectInstanceId
```

Ablauf:

```text
WorldObjectInstance laden
→ alle aktiven WorldObjectChunkRefs laden
→ pro Ref:
   Snapshot laden/generieren
   gespeicherte occupiedCells auf Air setzen
   ObjectRef aus Chunkinhalt entfernen
   Snapshot schreiben
   ChunkEvent schreiben
   Ref soft-löschen

→ Objektinstanz soft-löschen
→ CommandLog aktualisieren
→ Commit
```

Aktuelle Grenze:

```text
Die Route prüft nicht, ob eine belegte Zelle seit der Platzierung
durch einen späteren Command verändert wurde.
```

Damit kann `RemoveObject` neuere Zelländerungen auf Air zurücksetzen.

---

## 14.14 CommandLog bei Fehlern

Die Modelschicht unterstützt:

```text
received
applied
noop
rejected
failed
compensated
```

Die aktuelle Route erzeugt den `WorldCommandLog` innerhalb derselben Transaktion wie die Änderung.

Bei `ValueError`, `LookupError` oder Ausführungsfehler:

```text
gesamte Session rollback
```

Die Route ruft aktuell nicht auf:

```text
mark_rejected()
mark_failed()
```

Folge:

```text
abgelehnte oder fehlgeschlagene Commands bleiben über diesen Pfad
nicht dauerhaft als WorldCommandLog erhalten.
```

Damit wird derzeit nicht jeder eingegangene Intent auditierbar persistiert.

---

## 14.15 Command-Idempotenz

Ein übergebenes `commandId` wird in `WorldCommandLog.command_id` gespeichert.

Es existiert in der Route aber kein vorgelagerter Lookup:

```text
commandId bereits verarbeitet?
```

Ein Wiederholungsrequest mit derselben ID kann daher auf einen Unique-Constraint laufen, statt die bestehende Antwort idempotent zurückzugeben.

---

## 14.16 Nebenläufigkeit

Der Commandpfad verwendet aktuell keine sichtbare:

```text
SELECT FOR UPDATE-Sperre
Compare-and-Swap-Prüfung auf chunk_revision
per-Chunk-Commandserialisierung
```

Zwei parallele Commands auf demselben Chunk können denselben Vorzustand laden und konkurrierend schreiben.

---

## 14.17 Commandstatus

```text
GET /commands/_status
```

Liefert Counts für:

```text
Projects
Universes
Worlds
ChunkSnapshots
WorldCommandLogs
ChunkEvents
WorldObjectInstances
WorldObjectChunkRefs
```

Zusätzlich:

```text
Commandlimits
Objektgrößenlimits
Cellencoding
noload-Verfügbarkeit
unterstützte Commands
```

---

## 15. `world_test.py`

## 15.1 Aufgabe

`world_test.py` ist eine reine Entwicklungs- und Diagnoseoberfläche.

Es verwendet:

```text
World-Discovery
World-Registry
WorldLoader
WorldService
World-Serializer
```

Es verwendet nicht:

```text
PostgreSQL
ChunkSnapshots
ChunkEvents
WorldCommandLogs
Projektgraph
```

---

## 15.2 HTML-Oberfläche

Die Seite wird vollständig inline erzeugt:

```text
HTML
CSS
JavaScript
```

Es existiert keine separate Template- oder Static-Datei für diese Debugseite.

Die UI zeigt:

```text
entdeckte Provider
Providerstatus
World-Metadaten
Palette
Koordinaten
Chunkdaten
```

Steuerung:

```text
W / Pfeil hoch    → Z - 1
S / Pfeil runter  → Z + 1
A / Pfeil links   → X - 1
D / Pfeil rechts  → X + 1
Q                 → Y - 1
E                 → Y + 1
R                 → Reset
```

---

## 15.3 Koordinatentest

Die Route implementiert explizit:

```text
chunkCoord = floor(worldCoord / chunkSize)
localCoord = positiver Modulo
```

Grenzen:

```text
maximale absolute Chunkkoordinate = 1.000.000
maximale absolute Weltkoordinate = 16.000.000
```

Diese starre Debuggrenze ist für den vollständigen Earth-Periodic-X-Test zu klein:

```text
Earth worldWidthChunks = 2.500.000
Test eines vollständigen X-Wraps mit chunkX=2.500.000
→ wird bereits in der Route abgewiesen
```

Der Fehler wird derzeit außerdem als allgemeiner HTTP-500-Fehler serialisiert, obwohl ein ungültiger Queryparameter fachlich HTTP 400 sein sollte. Die Route ist als nächster Quelltext-Fix vorgesehen.

Die Datei weist selbst darauf hin, dass die endgültige produktive Koordinatenlogik unter `src/coordinates` liegen soll.

---

## 15.4 Discovery

Die Route erzeugt einen dynamischen `WorldService` aus entdeckten Providerordnern.

Neue Provider können dadurch sichtbar werden, ohne die produktive Default-Registry anzupassen.

---

## 15.5 Produktionsrisiko

Die Blueprint-Registry aktiviert Debugrouten standardmäßig, sofern die Konfiguration nicht geändert wird.

`world_test.py` selbst prüft nicht:

```text
current_app.debug
```

Damit kann `/world-test` erreichbar sein, obwohl Flask nicht im Debugmodus läuft, solange:

```text
VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES=true
```

Für Produktion sollte dieses Gate explizit deaktiviert werden.

---

## 16. `earth_debug.py`

## 16.1 Aufgabe

`earth_debug.py` testet den Earth-v1-Kern ohne Projekt- oder Datenbankintegration.

Queryparameter:

```text
refLon
refLat
refHeight

lon
lat
height
```

Ablauf:

```text
PROJ-Netzwerk deaktivieren
→ Earth-Definition laden
→ kanonisches geografisches CRS laden
→ GlobalReferencePoint erzeugen
→ EarthWorldProvider für world_spawn erzeugen
→ Chunk 0:0:0 generieren
→ Zielkoordinate global → lokal
→ lokal → global Roundtrip
→ Spawn aus globaler Koordinate auflösen
```

Antwort enthält:

```text
Provideridentität
Referenz
Storage-Origin
lokale Referenzposition
Zielkoordinate
lokale Position
Roundtrip
Spawn
Chunkmetadaten ohne Zellen
```

---

## 16.2 Debugschutz

Die Route prüft:

```python
if not current_app.debug:
    return 404
```

Sie ist damit zusätzlich zum Blueprint-Config-Gate geschützt.

---

## 16.3 Grenzen

Die Route verwendet:

```text
festes world_spawn
festen Chunk 0:0:0
keine Datenbank
keine WorldInstance
keinen Snapshot
keinen Command
```

Sie bestätigt den Earth-Kern, nicht die produktive Projektintegration.

---

## 17. `editor.py`

## 17.1 Einordnung

`editor.py` ist als optionaler Legacy-Blueprint registriert.

Der Dateikopf bezeichnet als Ursprungsort:

```text
services/vectoplan-editor/routes/editor.py
```

Im vorliegenden Chunk-Service liegt die Datei jedoch im Routeordner des Chunk-Services.

Das ist ein Hinweis auf kopierte Legacy-Struktur.

---

## 17.2 Route

```text
GET /editor
```

Ablauf:

```text
Editor-Konfiguration lesen
→ Jinja-Template rendern
→ bei TemplateNotFound Inline-Fallback erzeugen
→ bei sonstigem Renderfehler ebenfalls Fallback erzeugen
```

Standardtemplate:

```text
editor/index.html
```

Static-Pfade:

```text
editor/css/editor.css
editor/js/main.js
```

---

## 17.3 Fallback-Shell

Die Inline-Shell enthält:

```text
Topbar
linkes Werkzeugpanel
Viewportplatzhalter
rechtes Inspectorpanel
Hotbar
Statusanzeige
```

Hotbar:

```text
Standard = 5 Slots
Minimum = 1
Maximum = 20
```

---

## 17.4 Responseheader

```text
Content-Type: text/html; charset=utf-8
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
Pragma: no-cache
X-Content-Type-Options: nosniff
X-Frame-Options: SAMEORIGIN
X-VECTOPLAN-Editor-Route: /editor
```

Bei Fallback zusätzlich:

```text
X-VECTOPLAN-Editor-Fallback
```

---

## 17.5 Grenze

Die Route besitzt:

```text
keine Datenbank
keine Chunklogik
keine Worldlogik
keine 3D-Fachlogik
```

Sie ist nicht die produktive Editor-Anwendung, sondern eine optionale Shell.

---

## 18. Abhängigkeitsstruktur

```text
routes/__init__.py
└── Flask Blueprint-Registrierung

projects.py
├── extensions.db
├── models.Project
├── models.Universe
├── models.WorldInstance
├── src.bootstrap.db_bootstrap
├── src.bootstrap.settings
├── src.world_state.provisioning
└── src.project_access über den Provisioning-Service

project_access.py
├── extensions.db
├── models.Project
├── models.project_access
│   ├── ProjectRole
│   ├── ProjectGroup
│   ├── ProjectGroupMember
│   └── ProjectRoleAssignment
└── src.project_access.service

worlds.py
├── extensions.db
├── models.Project
├── models.Universe
└── models.WorldInstance

blocks.py
├── extensions.db
├── models.Project / Universe / WorldInstance
├── models.BlockRegistry / BlockType
└── src.system_blocks oder system_blocks

chunks.py
├── extensions.db
├── models.Project / Universe / WorldInstance
├── models.ChunkSnapshot
└── src.world.service

commands.py
├── extensions.db
├── Project / Universe / WorldInstance
├── BlockRegistry / BlockType
├── ChunkSnapshot
├── WorldCommandLog / ChunkEvent
├── WorldObjectInstance / WorldObjectChunkRef
└── src.world.service

world_test.py
└── src.world.discovery / loader / serializer / service

earth_debug.py
├── src.georeferencing
└── src.world.earth

editor.py
├── Flask
└── Jinja2
```

---

## 19. Lese-/Schreibmatrix

| Bereich | Normaler Zugriff | Bedingter Schreibzugriff | Commit |
|---|---|---|---|
| Blueprint-Registry | App-Metadaten | Blueprintregistrierung | kein DB-Commit |
| Projektliste/-detail | DB lesen | nein | nein |
| Projektanlage | DB schreiben | immer | ja |
| Projekt-Provisioning | DB schreiben | falls nicht vorhanden/repariert; einschließlich Access und Earth | im Provisioning |
| Project-Access-Lesen | DB lesen | nein | nein |
| Project-Access-Initialisierung/CRUD | DB schreiben | bei fachlicher Änderung | genau einmal in der Route |
| Projektpatch/-delete | DB schreiben | immer | ja |
| Worldliste/-detail | DB lesen | nein | nein |
| Worldcreate/-patch/-delete | DB schreiben | immer | ja |
| Blockrouten | DB und Codekatalog lesen | nein | nein |
| Einzelchunk | Snapshot/Provider lesen | `materializeGenerated=true` | dann ja |
| Chunkbatch | Snapshot/Provider lesen | `materializeGenerated=true` | nur fehlerfrei |
| Commands | DB lesen und schreiben | immer oder Noop | ja |
| World-Test | Provider lesen/generieren | keine DB | nein |
| Earth-Debug | Provider lesen/generieren | keine DB | nein |
| Editor | Template/Config lesen | keine DB | nein |

---

## 20. Zentrale Ablaufdarstellungen

## 20.1 Flask-Startup

```text
create_app()
→ routes.register_blueprints(app)
→ Routing-State erzeugen
→ BlueprintSpecs laden
→ produktive Blueprints importieren
→ Fehler bei Pflichtmodul: Startup abbrechen
→ Debug-/Legacy-Blueprints abhängig von Config importieren
→ optionale Fehler protokollieren und überspringen
→ Registrierungsmetadaten in app.extensions speichern
```

---

## 20.2 App-Projekt-Provisioning

```text
vectoplan-app
→ PUT /projects/by-app/<app_project_public_id>

projects.py
→ Provisioning-Service aufrufen
→ Project mit gezieltem Basistabellenlock sicherstellen
→ Defaultrollen und Owner-Zuweisung sicherstellen
→ Universe mit gezieltem Basistabellenlock sicherstellen
→ world_spawn sicherstellen
→ bei earth: GlobalReferencePoint kanonisieren und persistieren
→ Defaultreferenzen, Earth-Grenzen und Spawn reparieren
→ Commit

Response
→ chunkProjectId
→ chunkUniverseId
→ chunkWorldId
→ worldTemplate
→ earthReferenceFingerprint
→ Access-Zusammenfassung einschließlich projectDbId
→ Bootstrap-/Route-Hinweise
→ authzEnforced=false
```

---


## 20.3 Project-Access-Mutation

```text
POST/PUT/PATCH/DELETE .../roles|groups|members|assignments

Project über öffentliche project_id auflösen
→ Body als JSON-Objekt validieren
→ Serviceoperation im Project-Scope ausführen
→ Cross-Project-Referenzen abweisen
→ Owner-/Systemrollen-Invarianten prüfen
→ MutationStats serialisieren
→ Commit

bei Fehler
→ Rollback
→ project-access-route-error.v1
```

Read-Pfade:

```text
GET .../access|roles|groups|members|assignments
→ kein Commit
→ externe User-IDs bleiben Strings
```


## 20.4 Chunklesen

```text
GET .../chunks?chunkX=...&chunkY=...&chunkZ=...

Project laden
→ Universe laden
→ WorldInstance laden
→ ChunkSnapshot suchen

Snapshot gefunden
→ Snapshot normalisieren
→ source=snapshot

Snapshot fehlt
→ WorldService.generate_chunk(provider_world_id, ...)
→ Providerinhalt normalisieren
→ source=generated

optional materializeGenerated
→ ChunkSnapshot erzeugen
→ Commit
```

---

## 20.5 Blockcommand

```text
POST .../commands

WorldCommandLog erzeugen
→ Zielposition in Chunk + lokale Zelle umrechnen
→ Snapshot laden oder Providerchunk generieren
→ Block und Palette validieren
→ Zelle ändern
→ Snapshot erzeugen/aktualisieren
→ ChunkEvent erzeugen
→ Snapshot mit Event-ID verknüpfen
→ CommandLog applied/noop
→ Commit
→ dirtyChunks zurückgeben
```

---

## 20.6 Mehrblockobjekt

```text
PlaceObject
→ Objektzellen berechnen
→ nach Chunks gruppieren
→ WorldObjectInstance
→ je Chunk Snapshot + ChunkRef + Event
→ gemeinsamer Commit

RemoveObject
→ WorldObjectInstance
→ ChunkRefs laden
→ je Chunk Zellen auf Air
→ Snapshot + Event
→ Ref soft-löschen
→ Objekt soft-löschen
→ gemeinsamer Commit
```

---

## 21. Status- und Diagnosekonzept

Produktive Statusrouten:

```text
/projects/_status
/project-access/_status
/worlds/_status
/blocks/_status
/chunks/_status
/commands/_status
```

Gemeinsame optionale Bereiche:

```text
database
models
counts
config
```

Zusätzliche Projektbereiche:

```text
bootstrapStatus
schemaReady
seedReady
defaultProjectReady
defaultUniverseReady
defaultWorldReady
settings
env
provisioning
projectAccessSchemaReady
defaultProjectAccessReady
projectAccessRouteSurfaceReady
authzEnforced=false
```

Beachtung:

```text
Counts verwenden COUNT-Abfragen auf produktiven Tabellen.

Bei großen Datenmengen können Statusrouten dadurch teuer werden.

includeEnv=true kann zusätzliche Konfigurationsinformationen sichtbar machen.
```

---

## 22. Bestätigter Funktionsstand

Über den bisherigen Service-IST bestätigt:

```text
Blueprintregistrierung aller produktiven Pflicht-Blueprints einschließlich project_access
Prestart mit vollständiger Project-Access-Route-Surface

GET Projektbootstrap
GET Worldliste
GET Worlddetail

GET Blockpalette

GET generierter Chunk
GET Snapshot-Chunk
POST Chunkbatch

POST SetBlock
POST RemoveBlock

ChunkSnapshot wird aktualisiert
ChunkEvent wird geschrieben
WorldCommandLog wird geschrieben
Reload zeigt Änderung

App-Projekt-Provisioning Flat und Earth
→ Project
→ vier Defaultrollen
→ Owner-Zuweisung
→ Universe
→ world_spawn
→ atomarer Commit
→ idempotenter Folgeaufruf

Project-Access-Routen
→ Status ready
→ Access-Zusammenfassung
→ Defaultrollen
→ Owner-Assignment
→ externe User-ID "1"
→ authzEnforced=false

Earth-Provisioning über PUT /projects/by-app/...
→ kompakte EPSG:4979-Referenz
→ vollständiger GlobalReferencePoint
→ WKT2:2019-Persistenz
→ Earth-v1-Grid
→ min_y=-1024 / max_y=8192
→ referenzbasierter Integer-/Precise-Spawn
→ Reparatur bestehender inkonsistenter WorldInstance
→ danach unveränderte Idempotenz

Earth-Debugroute
→ Referenz
→ Konvertierung
→ Spawn
→ Chunkgenerierung
```

---

## 23. Implementiert, aber noch nicht vollständig bestätigt

```text
ReplaceBlock End-to-End

PlaceObject End-to-End über mehrere Chunks
RemoveObject End-to-End
Rollback bei Objektfehlern
Objektkonflikte
Objekte an Chunkgrenzen

Chunkbatch mit gemischtem Snapshot-/Providerinhalt unter Last
Batchfehler plus materializeGenerated

negative Koordinaten im produktiven Commandpfad
gleichzeitige Commands auf demselben Chunk

generische Earth-WorldInstance-Erzeugung über POST /projects/<project_id>/worlds
projektgebundener Earth-Chunk über den aktuellen chunks.py-Provideradapter
Earth-Snapshotlookup mit kanonischem X
Earth-Commands
periodische X-Aliase
Dirty-Chunks über die Earth-Naht
vollständiger Periodic-X-Debugtest nach Aufhebung des 1.000.000-Limits
```

---

## 24. Harte Invarianten des Routeordners

```text
1. Produktive Pfade sind projektgescopt.

2. Project enthält Universe.

3. Universe enthält konkrete WorldInstances.

4. world_spawn ist eine konkrete editierbare Welt.

5. flat und earth sind Provider-/Templateidentitäten.

6. Blockrouten verändern keine Registrydaten.

7. system_air wird nicht als BlockType persistiert.

8. cellValue 0 bedeutet Air.

9. Positive Zellwerte folgen paletteIndex + 1.

10. ChunkSnapshot ist Lade-Wahrheit.

11. Events werden beim normalen Chunkladen nicht replayt.

12. Providerchunks werden ohne explizite Materialisierung nicht gespeichert.

13. Commands materialisieren nur tatsächlich geänderte Chunks.

14. WorldCommandLog fasst einen Command zusammen.

15. Ein Command kann mehrere ChunkEvents erzeugen.

16. Chunk- und Command-Lookups sollen keine tiefen ORM-Graphen laden.

17. Routeantworten sollen JSON-sicher und flach bleiben.

18. Projekt-, World- und Command-Schreibpfade müssen bei Fehlern rollbacken.

19. Debug- und Legacy-Blueprints dürfen bei Importfehlern den produktiven App-Start nicht blockieren.

20. Pflicht-Blueprints müssen bei Import-/Registrierungsfehlern den Startup fehlschlagen lassen.

21. Earth-Debug ist nicht gleich produktive Earth-Integration.

22. Project-Access-Zeilen sind immer über die lokale Project.id projektspezifisch.

23. Externe User-IDs besitzen keine Foreign Keys zu anderen Services.

24. Defaultrollen owner/admin/editor/viewer werden kanonisch synchronisiert.

25. Die Owner-Rolle darf nur dem aktuellen Project.owner_id zugewiesen werden.

26. Project-Access-Routen erzwingen aktuell keine Autorisierung und müssen authzEnforced=false ausgeben.

27. Ein Project-Access-Mutationsrequest besitzt genau eine Route-Commit-/Rollback-Grenze.

28. Earth-Provisioning verlangt ein explizites CRS und alwaysXY=true.

29. Der kanonische Earth-Fingerprint stammt aus GlobalReferencePoint.fingerprint.

30. Provider-/Generatoränderungen migrieren bestehende Snapshots nicht automatisch.
```

---

## 25. Bekannte technische Risiken und Restpunkte

## 25.1 Route-Dateien enthalten umfangreiche Businesslogik

Kommentare beschreiben mehrere Dateien als dünne HTTP-Adapter.

Der tatsächliche Stand ist teilweise anders:

```text
commands.py
→ kompletter Commandexecutor

projects.py
→ Projektgraph- und Soft-Delete-Orchestrierung

chunks.py
→ Provideradapter und Snapshotmaterialisierung

worlds.py
→ Defaultreferenz-Orchestrierung
```

Eine spätere Trennung sollte Service-/Repositorylogik aus den Routen herauslösen.

---

## 25.2 Massive Helper-Duplikation

Mehrfach vorhanden:

```text
_safe_exception_message
_coerce_string
_coerce_int
_coerce_bool
_make_json_safe
_get_env_*
_get_config_*
_get_query_*
_get_json_body
_error_response
_ok_response

Project-/Universe-/World-Auflösung
Provider-Chunkgenerierung
Snapshotnormalisierung
Chunk-Key-Bildung
```

Besonders `chunks.py` und `commands.py` besitzen große, nahezu parallele Provider- und Snapshotadapter.

Risiko:

```text
Verhaltensdrift
unterschiedliche Fehlercodes
unterschiedliche Earth-Unterstützung
doppelte Fehlerbehebung
```

---

## 25.3 Kein zentraler Request-Schemavertrag

Die Routen validieren Payloads über manuelle `payload.get(...)`-Aufrufe.

Es gibt keinen sichtbaren zentralen:

```text
JSON-Schema-Vertrag
Pydantic-/Marshmallow-Schema
OpenAPI-generierten Validator
```

Folgen:

```text
viele Feldaliase
uneinheitliche Fehlermeldungen
schwer erkennbare Pflichtfelder
keine automatisch generierte API-Dokumentation
```

---

## 25.4 Schreibender GET-Endpunkt

```text
GET .../chunks?materializeGenerated=true
```

kann einen Snapshot erzeugen und committen.

Empfohlene spätere Trennung:

```text
GET
→ ausschließlich lesen/generieren

POST /materialize
oder
PUT /chunks/<key>
→ explizit persistieren
```

---

## 25.5 Commandfehler werden nicht dauerhaft geloggt

Der aktuelle Rollback entfernt auch den zuvor erzeugten `WorldCommandLog`.

Damit fehlen persistente:

```text
rejected
failed
```

Commands im normalen Fehlerpfad.

---

## 25.6 Fehlende Command-Idempotenz

`commandId` ist eindeutig, wird aber nicht vor Ausführung auf vorhandene Ergebnisse geprüft.

Empfehlung:

```text
commandId lookup
→ vorhanden und abgeschlossen
   → vorhandenes Ergebnis zurückgeben
→ vorhanden und in Bearbeitung
   → Konflikt/Retry
→ nicht vorhanden
   → ausführen
```

---

## 25.7 Nebenläufigkeit

Snapshotmutation besitzt keine sichtbare Sperr- oder Compare-and-Swap-Strategie.

Benötigt:

```text
SELECT FOR UPDATE
oder
UPDATE ... WHERE chunk_revision=<expected>
oder
per-Chunk-Queue/Lock
```

---

## 25.8 Earth ist nur teilweise durchgängig integriert

Inzwischen produktiv bestätigt:

```text
Earth-Projektgraph über App-Provisioning
GlobalReferencePoint aus kompaktem Request
kanonische CRS-/Grid-Persistenz
Earth-WorldInstance world_spawn
referenzbasierter Spawn
idempotente Reparatur
```

Weiterhin fehlende produktive Verkabelung:

```text
generische Worldanlage über worlds.py mit GlobalReferencePoint
instance-spezifischer EarthProvider im Chunk-/Commandadapter
X-Kanonisierung vor Chunk-Key
X-Kanonisierung vor Snapshotlookup
X-Kanonisierung vor Snapshotwrite
X-Kanonisierung vor Dirty-Chunk-Ausgabe
Objekte über die periodische Naht
```

---

## 25.9 Projekt-/World-Referenzen können auseinanderlaufen

Worldanlage und -löschung aktualisieren primär Universe-Referenzen.

Project-Level-Referenzen werden nicht überall synchronisiert.

Mögliche Folge:

```text
Project.spawn_world_id zeigt auf eine gelöschte WorldInstance
Universe.spawn_world_id ist bereits null oder zeigt auf eine andere Welt
```

Eine zentrale Graph-Invariantenfunktion sollte beide Ebenen atomar pflegen.

---

## 25.10 Objektplatzierung überschreibt Inhalte

`PlaceObject` prüft keine fachliche Belegung oder Replaceability.

`RemoveObject` setzt gespeicherte Objektzellen später pauschal auf Air.

Benötigt werden:

```text
Belegungs-/Kollisionsprüfung
Ownership pro Zelle oder Objektref
Vorherhash/Revision
Konflikterkennung bei späteren Änderungen
```

---

## 25.11 Debugrouten standardmäßig aktiviert

Registry-Defaults:

```text
Dev-Routen = true
Legacy-Routen = true
```

`world_test.py` besitzt keinen zusätzlichen Flask-Debug-Guard.

Produktionskonfiguration muss daher explizit setzen:

```text
VECTOPLAN_CHUNK_ENABLE_DEV_ROUTES=false
VECTOPLAN_CHUNK_ENABLE_LEGACY_ROUTES=false
```

---

## 25.12 Access-Daten vorhanden, aber keine Autorisierungsdurchsetzung

Mit `project_access.py` existieren jetzt persistente und HTTP-erreichbare:

```text
Rollen
Gruppen
Mitgliedschaften
Rollenzuweisungen
Owner-Invarianten
```

In den Route-Modulen ist weiterhin keine Durchsetzung anhand des aktuellen Callers sichtbar:

```text
Authentifizierung des Request-Callers
Berechtigungsauflösung
Rollenprüfung vor Mutation
Projektzugriffsprüfung vor Lesen
```

Der aktuelle Vertrag macht dies ausdrücklich sichtbar:

```text
authzEnforced = false
```

Eine mögliche globale Authentifizierung außerhalb dieses Ordners wurde in dieser Bestandsaufnahme nicht als Schutz der einzelnen Operationen bestätigt.

Besonders schützenswert:

```text
POST /commands
POST /projects
PUT /projects/by-app/...
PATCH-/DELETE-Routen
POST /projects/_cache/reset
Statusrouten mit includeInternal/includeEnv
Debugrouten
```

---

## 25.13 Interne IDs und Umgebungsdaten

Mehrere Routen unterstützen:

```text
includeInternal=true
```

Die Projektstatusroute unterstützt:

```text
includeEnv=true
```

Diese Optionen sollten in produktiven Umgebungen nur für autorisierte interne Diagnosezugriffe verfügbar sein.

---

## 25.14 Statuszählungen können teuer werden

Statusrouten führen mehrere Tabellenzählungen aus.

Mit wachsenden Tabellen:

```text
ChunkSnapshots
ChunkEvents
WorldCommandLogs
```

können vollständige `COUNT(*)`-Abfragen zu einem relevanten Betriebsaufwand werden.

---

## 25.15 Fehlerklassifikation über Text

Bei Projekt- und Worldanlage werden Unique-Konflikte teilweise über folgende Prüfung erkannt:

```text
"unique" in Fehlermeldung
oder
"duplicate" in Fehlermeldung
```

Das ist datenbank- und treiberabhängig.

Robuster wäre die Auswertung konkreter IntegrityError-/Constraintinformationen.

---

## 25.16 Legacy-Editor im Chunk-Service

`editor.py` stammt laut Dateikopf aus dem Editor-Service, wird aber optional im Chunk-Service registriert.

Dies erhöht:

```text
Servicekopplung
Templateabhängigkeit
Verwechslungsgefahr zwischen produktivem Editor und Legacy-Shell
```


## 25.17 `project_access.py` bündelt umfangreiche CRUD-Logik

Mit 3.088 Zeilen und 81 Top-Level-Funktionen enthält die Datei:

```text
HTTP-Parsing
Projektauflösung
Rollen-CRUD
Gruppen-CRUD
Mitgliedschafts-CRUD
Assignment-CRUD
Fehlerabbildung
Statusdiagnostik
Cache-Reset
```

Die fachliche Kernlogik liegt zwar im Access-Service, trotzdem bleibt der Routeadapter groß. Später sinnvoll:

```text
routes/project_access.py
→ dünne Endpunktdefinitionen

src/http/project_access_contracts.py
→ Request-/Responsevalidierung

src/project_access/service.py
→ fachliche Invarianten

repository
→ Query- und Lockstrategie
```

## 25.18 Project-Access-Featuregate kann die Oberfläche vollständig abschalten

Die Registry unterstützt:

```text
VECTOPLAN_CHUNK_ENABLE_PROJECT_ACCESS_ROUTES=false
```

Wenn diese Option verwendet wird, ist die vorbereitete Rollen-/Gruppenoberfläche nicht registriert. Andere Komponenten dürfen dann nicht stillschweigend annehmen, dass `/projects/<project_id>/access` verfügbar ist.

Der Status muss deshalb über die Registry-Metadaten geprüft werden:

```text
projectAccessApiEnabled
projectAccessBlueprintRegistered
projectAccessRouteSurfaceReady
```


---

## 26. Empfohlene spätere Zielstruktur

```text
routes/
├── __init__.py
├── projects.py
├── project_access.py
├── worlds.py
├── blocks.py
├── chunks.py
├── commands.py
├── debug/
│   ├── world_test.py
│   └── earth_debug.py
└── legacy/
    └── editor.py

src/
├── http/
│   ├── responses.py
│   ├── parsing.py
│   ├── errors.py
│   └── auth.py
│
├── world_state/
│   ├── project_service.py
│   ├── project_access_service.py
│   ├── world_service.py
│   ├── chunk_read_service.py
│   ├── command_service.py
│   └── object_service.py
│
├── repositories/
│   ├── project_repository.py
│   ├── project_access_repository.py
│   ├── world_repository.py
│   ├── chunk_repository.py
│   ├── command_repository.py
│   └── object_repository.py
│
└── contracts/
    ├── project_api.py
    ├── project_access_api.py
    ├── world_api.py
    ├── chunk_api.py
    └── command_api.py
```

Ziel:

```text
Route
→ HTTP lesen
→ Contract validieren
→ Service aufrufen
→ standardisierte Response zurückgeben

Service
→ fachliche Orchestrierung

Repository
→ Datenbankzugriff und Sperrstrategie
```

---

## 27. Wo eine Änderung hingehört

| Änderung | Aktuell zuständig | Spätere Zielschicht |
|---|---|---|
| Blueprint hinzufügen | `routes/__init__.py` | gleich |
| Projekt-HTTP-Endpunkt | `routes/projects.py` | Route + ProjectService |
| App-Provisioning | `routes/projects.py` + `src/world_state/provisioning` | ProvisioningService |
| Project-Access-HTTP | `routes/project_access.py` | Route + ProjectAccessService |
| Rollen-/Gruppen-/Assignment-Invarianten | `src/project_access/service.py` | ProjectAccessService |
| Autorisierungsdurchsetzung | noch nicht implementiert; `authzEnforced=false` | zentraler Authz-Layer vor Mutationen/Reads |
| World-CRUD | `routes/worlds.py` | WorldService |
| Blockpalette | `routes/blocks.py` | BlockQueryService |
| Systemblockkatalog | `routes/blocks.py` + `src/system_blocks` | SystemBlockService |
| Chunk lesen | `routes/chunks.py` | ChunkReadService |
| Providerfallback | `routes/chunks.py` und `commands.py` | gemeinsamer ProviderAdapter |
| Snapshot materialisieren | `routes/chunks.py` | ChunkMaterializationService |
| Command ausführen | `routes/commands.py` | CommandService |
| Objekt platzieren/entfernen | `routes/commands.py` | ObjectCommandService |
| Standardantwort | pro Datei dupliziert | `src/http/responses.py` |
| Query-/Bodyparsing | pro Datei dupliziert | `src/http/parsing.py` |
| Authentifizierung | außerhalb/unbestätigt | zentraler Auth-Layer |
| Earth-Referenz-Provisionierung | `routes/projects.py` + `src/world_state/provisioning.py` | Provisioning-/Earth-Reference-Service |
| Earth-X-Kanonisierung in Chunk/Command | noch nicht integriert | Coordinates-/Earth-Service |
| Debugroute | `world_test.py`, `earth_debug.py` | `routes/debug/` |
| Legacy-Editor | `editor.py` | aus Chunk-Service entfernen oder `routes/legacy/` |

---

## 28. Checkliste für neue produktive Routen

```text
1. Ist die Route projektgescopt?

2. Wird eine konkrete world_id verwendet und keine Provider-ID?

3. Sind Pflichtfelder über einen klaren Contract validiert?

4. Sind CamelCase-/snake_case-Aliase wirklich erforderlich?

5. Wird eine standardisierte responseVersion verwendet?

6. Sind Fehlercodes stabil und fachlich?

7. Werden Debugdetails nur kontrolliert ausgegeben?

8. Wird ORM-Relationship-Laden begrenzt?

9. Werden keine tiefen ORM-Objekte direkt serialisiert?

10. Ist klar, ob die Route lesen oder schreiben darf?

11. Führt ein GET garantiert keinen Schreibzugriff aus?

12. Liegt der Commit an genau einer klaren Transaktionsgrenze?

13. Wird bei jedem Fehler rollbackt?

14. Müssen fehlgeschlagene Requests auditierbar persistiert werden?

15. Ist Idempotenz erforderlich?

16. Gibt es Race Conditions oder notwendige Row Locks?

17. Werden Projekt-, Universe- und Worldreferenzen gemeinsam konsistent gehalten?

18. Wird Earth-X vor jeder Persistenzadresse kanonisiert?

19. Sind Batchgrenzen definiert?

20. Sind Status-/Count-Abfragen billig genug?

21. Ist die Route authentifiziert und autorisiert?

22. Sind Debug-/Legacy-Routen in Produktion deaktiviert?

23. Ist der Blueprint in routes/__init__.py registriert?

24. Ist der Modulstatusendpunkt aktualisiert?

25. Ist bei Project-Access-Routen `authzEnforced` korrekt und unmissverständlich?

26. Bleiben externe User-IDs ohne Cross-Service-Foreign-Key?

27. Sind Role-/Group-/Assignment-Referenzen garantiert projektspezifisch?

28. Sind Default-/Systemrollen vor generischer Mutation geschützt?

29. Wird eine Owner-Zuweisung nur für Project.owner_id akzeptiert?

30. Ist bei Earth-Provisioning das CRS explizit und `alwaysXY=true`?

31. Wird der echte GlobalReferencePoint-Fingerprint verwendet?

32. Ist diese IST-Zustand.md aktualisiert?
```

---

## 29. Empfohlene Navigationsreihenfolge

Für den Einstieg:

```text
1. routes/IST-Zustand.md
2. routes/__init__.py
3. routes/projects.py
4. routes/project_access.py
5. routes/worlds.py
6. routes/blocks.py
7. routes/chunks.py
8. routes/commands.py
9. routes/world_test.py
10. routes/earth_debug.py
11. routes/editor.py
```

Für Projekt-Provisioning:

```text
projects.py
→ src/world_state/provisioning
→ src/project_access/service.py
→ models/project.py
→ models/project_access.py
→ models/universe.py
→ models/world.py
```


Für Project Access:

```text
project_access.py
→ src/project_access/service.py
→ models/project_access.py
→ models/project.py
```


Für Chunklesen:

```text
chunks.py
→ models/chunk.py
→ src/world/service.py
→ konkreter Provider
```

Für Blockänderungen:

```text
commands.py
→ models/block.py
→ models/chunk.py
→ models/event.py
```

Für Objekte:

```text
commands.py
→ models/object.py
→ models/chunk.py
→ models/event.py
```

Für Earth:

```text
projects.py + src/world_state/provisioning.py
→ produktive Earth-Referenz und WorldInstance

earth_debug.py
→ isolierter Earth-Kern

src/georeferencing
→ src/world/earth
→ models/world.py
→ später chunks.py und commands.py für kanonische Projektintegration
```

---

## 30. Gesamtbefund

Die Route-Schicht besitzt einen funktionsfähigen vertikalen Slice:

```text
Projekt provisionieren
→ Defaultrollen und Owner-Assignment sicherstellen
→ konkrete Flat- oder Earth-Welt auflösen
→ Blockpalette lesen
→ Chunk aus Snapshot oder Provider laden
→ Blockcommand ausführen
→ Snapshot und Event persistieren
→ Editor erhält dirtyChunks
```

Besonders belastbar sind:

```text
Blueprintregistrierung einschließlich Pflicht-Project-Access
Projektgraph und idempotentes Flat-/Earth-App-Provisioning
Project-Access-Datenhaltung und CRUD-Oberfläche
Flat- und Earth-World-Metadaten
Blockpalette
Snapshot-/Provider-Lesepfad
SetBlock
RemoveBlock
Status- und Diagnoserouten
```

Noch zu härten sind:

```text
Trennung von HTTP und Businesslogik
zentrale Request-/Responsecontracts
Authentifizierung und tatsächliche Autorisierungsdurchsetzung
Project-Access-Contractvalidierung weiter zentralisieren
Command-Idempotenz
Persistenz fehlgeschlagener Commands
Nebenläufigkeit
Objektkonflikte
Earth-Kanonisierung in produktiven Chunk-/Commandpfaden
world_test-Periodic-X-Limit und HTTP-400-Klassifizierung
Debugroute-Absicherung
Projekt-/World-Referenzkonsistenz
```

Die wichtigste Architekturentscheidung für weitere Arbeit lautet:

```text
routes/
→ HTTP-Adapter, Auth, Contractvalidierung und Response

Services
→ fachliche Abläufe und Transaktionen

Repositories
→ Datenzugriff, Upserts, Locks und Nebenläufigkeit

Models
→ persistente Datenverträge und lokale Invarianten
```

Damit kann der Routeordner künftig verstanden und geändert werden, ohne jede der aktuell 19.526 dokumentierten Quellcodezeilen vollständig zu lesen.

---

## 31. Aktualisierungs- und Verifikationsnachweis dieser Fassung

Diese Fassung übernimmt die vollständige vorherige Route-Bestandsaufnahme und ergänzt den seitdem implementierten und getesteten Stand.

### 31.1 Statisch ausgewertete aktuelle Dateien

```text
routes/__init__.py
→ 1.247 Zeilen
→ 43 Top-Level-Funktionen
→ Registry-Version 0.6.0

routes/projects.py
→ 3.402 Zeilen
→ 99 Top-Level-Funktionen
→ 15 Routen
→ Modulversion 0.5.1

routes/project_access.py
→ 3.088 Zeilen
→ 81 Top-Level-Funktionen
→ 26 Routen
→ Modulversion 1.0.1
```

Die unveränderten Größenangaben der übrigen sieben Route-Dateien wurden aus der vorherigen Bestandsaufnahme übernommen.

### 31.2 Bestätigte Runtime-/HTTP-/DB-Ergebnisse

```text
Docker-Build erfolgreich
Container healthy
alle Pflicht-Blueprints registriert
Prestart erfolgreich

Project Access
→ Status ready
→ Defaultrollen vorhanden
→ Owner-Assignment vorhanden
→ authzEnforced=false

Earth-Provisioning
→ erster Request created=true
→ vollständiger Project-/Access-/Universe-/World-Graph
→ EPSG:4979 als WKT2:2019 persistiert
→ Earth-v1-Grid persistiert
→ Domain-Fingerprint konsistent

Reparaturlauf
→ min_y/max_y auf -1024/8192 korrigiert
→ Integer-Spawn aus Precise-Spawn korrigiert
→ access.projectDbId ergänzt

identischer Folgeaufruf
→ chunk_project_exists
→ created=false
→ updated=false
```

### 31.3 Noch nicht als erfolgreich abgeschlossen eingestuft

```text
world_test Periodic-X-Wrap mit chunkX=2.500.000
→ aktuell durch starres 1.000.000-Limit blockiert

produktiver Earth-Snapshot-/Commandpfad
→ X-Kanonisierung und konkrete Providerinstanz noch ausstehend

generische Earth-Erzeugung über worlds.py
→ vollständiger Referenzadapter noch ausstehend

tatsächliche Autorisierungsdurchsetzung
→ noch nicht aktiv
```

### 31.4 Dokumentationsregel

Bei jeder weiteren Routeänderung müssen mindestens geprüft werden:

```text
Datei- und Funktionsanzahl
Route-Dekoratoren
Blueprint-Spezifikation
Pflicht-/Featuregate
responseVersion
Read-/Write-/Commit-Semantik
Project-Scope
Authn/Authz-Status
Earth-/Periodic-X-Auswirkung
Statusroute
reale HTTP-/DB-Bestätigung
```

