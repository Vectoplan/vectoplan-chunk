<!-- services/vectoplan-chunk/models/IST-Zustand.md -->

# IST-Zustand – `services/vectoplan-chunk/models`

## Status dieser Fassung

Stand: 2026-07-19  
Status: Aktualisierte Bestandsaufnahme der SQLAlchemy-Modelschicht mit 15 persistenten Modelklassen, kanonischer `ProjectAccessAssignment`-Projektion, Legacy-Rollen-/Gruppenkompatibilität, `Project`-Schema v3 sowie Flat-/Earth-Weltpersistenz.

Diese Fortschreibung gleicht die Dokumentation mit den vorliegenden Dateien `models/__init__.py`, `project.py`, `project_access_assignment.py`, `project_access.py`, `universe.py`, `world.py`, `block.py`, `chunk.py`, `event.py` und `object.py` ab. Neue Aussagen werden als **implementiert** beschrieben, wenn sie statisch im Code vorhanden sind. Laufzeit- oder HTTP-Bestätigungen werden nur übernommen, wenn sie bereits im übergeordneten Service-IST dokumentiert waren.

Diese Datei beschreibt den Ordner:

```text
services/vectoplan-chunk/models/
```

Ziel dieser Dokumentation ist, dass Aufbau, Tabellen, Beziehungen, Erzeugungspfade, Invarianten und Zuständigkeiten der Modelschicht verständlich sind, ohne jede Python-Datei einzeln lesen zu müssen.

Die Dokumentation unterscheidet zwischen:

```text
implementiert
→ im vorliegenden Python-Code vorhanden

bestätigt
→ im übergeordneten Service-IST bereits über reale Laufzeit-/DB-Tests bestätigt

vorbereitet
→ Model und Persistenzstruktur vorhanden, aber noch nicht vollständig End-to-End bestätigt
```

---

## 1. Kurzfassung

Der Ordner `models/` enthält die persistente SQLAlchemy-/PostgreSQL-Schicht des Chunk-Services.

Er modelliert:

```text
Project
→ Universe
→ WorldInstance
→ ChunkSnapshot
→ WorldCommandLog
→ ChunkEvent
→ WorldObjectInstance
→ WorldObjectChunkRef
```

Zusätzlich enthält er:

```text
ProjectAccessAssignment
→ kanonische, synchronisierte Access-Projektion
→ direct: auth_user_id
→ group: group_id

ProjectRole
→ ProjectRoleAssignment

ProjectGroup
→ ProjectGroupMember
→ ProjectRoleAssignment

BlockRegistry
→ BlockType
```

Der Access-Bereich besitzt jetzt zwei bewusst getrennte Persistenzformen:

```text
ProjectAccessAssignment
→ kanonische, durch den Project-Access-Service verwendete Projektion
→ Source of Truth bleibt vectoplan-app
→ Rollen owner/admin/editor/viewer
→ Viewer bleibt service-seitig read-only

project_access.py
→ Legacy-/Kompatibilitätsschicht für Rollen, Gruppen,
  Mitgliedschaften und Rollenzuweisungen
```

Die Models selbst treffen weiterhin keine HTTP-Zugriffsentscheidung. Sie speichern, normalisieren und validieren den dafür benötigten Zustand; Authentifizierung, effektive Berechtigungsentscheidung, Owner-Transfer und Transaktionsgrenzen liegen in der Service-/Routenebene.

Die Modelschicht besitzt aktuell **15 persistente Modelklassen in 9 fachlichen Python-Modulen**. Fünf Klassen gehören zum Access-Bereich: eine kanonische Projektion und vier Legacy-/Kompatibilitätsmodelle.

Die zentrale fachliche Trennung lautet:

```text
WorldInstance
→ Konfiguration und Identität einer konkreten editierbaren Welt

ChunkSnapshot
→ aktueller materialisierter Ladezustand eines bearbeiteten Chunks

WorldCommandLog
→ ein eingegangener und verarbeiteter Benutzer-/Systembefehl

ChunkEvent
→ append-only Historie der bestätigten Änderung pro betroffenem Chunk

WorldObjectInstance
→ logisches Mehrblockobjekt

WorldObjectChunkRef
→ Zuordnung eines Mehrblockobjekts zu den berührten Chunks
```

Wichtige Grundregel:

```text
Die Models erzeugen Python-/SQLAlchemy-Objekte,
validieren Zustände und serialisieren Ergebnisse.

Die Models:
→ führen keinen Commit aus
→ öffnen keine eigene fachliche Transaktion
→ erstellen keine Tabellen
→ führen keine Migrationen aus
→ seeden keine vollständigen Projektgraphen
```

Transaktions-, Upsert-, Lookup- und Orchestrierungsverantwortung liegt außerhalb dieses Ordners in Bootstrap-, Service-, Repository- oder Routenlogik.

---

## 2. Ordner- und Dateistruktur

```text
services/
└── vectoplan-chunk/
    └── models/
        ├── __init__.py
        │   ├── registriert alle Modelmodule
        │   ├── stellt Modelklassen zentral bereit
        │   ├── prüft Import- und Klassenvollständigkeit
        │   ├── liefert Tabellen-, Spalten- und Relationship-Diagnostik
        │   └── serialisiert Modelinstanzen defensiv
        │
        ├── project.py
        │   └── Project
        │       └── Tabelle: projects
        │
        ├── project_access_assignment.py
        │   └── ProjectAccessAssignment
        │       └── Tabelle: project_access_assignments
        │
        ├── project_access.py
        │   ├── ProjectRole
        │   │   └── Tabelle: project_roles
        │   ├── ProjectGroup
        │   │   └── Tabelle: project_groups
        │   ├── ProjectGroupMember
        │   │   └── Tabelle: project_group_members
        │   └── ProjectRoleAssignment
        │       └── Tabelle: project_role_assignments
        │
        ├── universe.py
        │   └── Universe
        │       └── Tabelle: universes
        │
        ├── world.py
        │   └── WorldInstance
        │       └── Tabelle: world_instances
        │
        ├── block.py
        │   ├── BlockRegistry
        │   │   └── Tabelle: block_registries
        │   └── BlockType
        │       └── Tabelle: block_types
        │
        ├── chunk.py
        │   └── ChunkSnapshot
        │       └── Tabelle: chunk_snapshots
        │
        ├── event.py
        │   ├── WorldCommandLog
        │   │   └── Tabelle: world_command_logs
        │   └── ChunkEvent
        │       └── Tabelle: chunk_events
        │
        ├── object.py
        │   ├── WorldObjectInstance
        │   │   └── Tabelle: world_object_instances
        │   └── WorldObjectChunkRef
        │       └── Tabelle: world_object_chunk_refs
        │
        └── IST-Zustand.md
            └── diese Dokumentation
```

Aktuelle Größenordnung der Quelldateien:

| Datei | Zeilen | Persistente Klassen | Hauptaufgabe |
|---|---:|---:|---|
| `__init__.py` | 1.521 | 0 | Registrierung, Diagnose sowie getrennte kanonische und Legacy-Access-Vertragsprüfung |
| `project.py` | 2.934 | 1 | Chunk-Projekt, kanonischer Owner, Template-/Fallback-Zustand, Provisionierung und Access-Sync |
| `project_access_assignment.py` | 1.554 | 1 | kanonische direkte User-/Gruppenprojektion für Access-Enforcement |
| `project_access.py` | 3.597 | 4 | Legacy-Rollen, Gruppen, Mitgliedschaften und Rollenzuweisungen |
| `universe.py` | 1.362 | 1 | Universum innerhalb eines Projekts |
| `world.py` | 4.685 | 1 | konkrete Welt, Providerkontext, Spawn und Earth-Referenz |
| `block.py` | 2.517 | 2 | Blockregistry und stabile Blockdefinitionen |
| `chunk.py` | 2.413 | 1 | materialisierte Chunk-Snapshots |
| `event.py` | 2.571 | 2 | Commandlog und historische Chunkevents |
| `object.py` | 2.577 | 2 | Mehrblockobjekte und Chunkzuordnungen |

---

## 3. Persistente Modelübersicht

| Python-Klasse | Tabelle | Datei | Schema-Version | Status |
|---|---|---|---|---|
| `Project` | `projects` | `project.py` | `project.schema.v3` | implementiert; kanonischer Owner-, Provisionierungs- und Access-Sync-Vertrag |
| `ProjectAccessAssignment` | `project_access_assignments` | `project_access_assignment.py` | `project-access-assignment.schema.v1` | implementiert; kanonische synchronisierte Access-Projektion |
| `ProjectRole` | `project_roles` | `project_access.py` | `1` | implementierte Legacy-/Kompatibilitätsrolle |
| `ProjectGroup` | `project_groups` | `project_access.py` | `1` | implementierte Legacy-/Kompatibilitätsgruppe |
| `ProjectGroupMember` | `project_group_members` | `project_access.py` | `1` | implementierter Legacy-Membership-Vertrag; vollständiger Lifecycle weiter zu testen |
| `ProjectRoleAssignment` | `project_role_assignments` | `project_access.py` | `1` | implementierte Legacy-User-/Gruppenrollenzuweisung |
| `Universe` | `universes` | `universe.py` | `universe.schema.v2` | implementiert und produktiv genutzt |
| `WorldInstance` | `world_instances` | `world.py` | `world-instance.schema.v3` | Flat produktiv; Earth-Vertrag und persistente Referenz implementiert |
| `BlockRegistry` | `block_registries` | `block.py` | `block-registry.schema.v1` | implementiert und produktiv genutzt |
| `BlockType` | `block_types` | `block.py` | `block-type.schema.v1` | implementiert und produktiv genutzt |
| `ChunkSnapshot` | `chunk_snapshots` | `chunk.py` | `chunk-snapshot.schema.v1` | implementiert und für Set-/RemoveBlock bestätigt |
| `WorldCommandLog` | `world_command_logs` | `event.py` | `world-command-log.schema.v1` | implementiert und für Set-/RemoveBlock bestätigt |
| `ChunkEvent` | `chunk_events` | `event.py` | `chunk-event.schema.v1` | implementiert und für Set-/RemoveBlock bestätigt |
| `WorldObjectInstance` | `world_object_instances` | `object.py` | `world-object-instance.schema.v1` | strukturell vorbereitet |
| `WorldObjectChunkRef` | `world_object_chunk_refs` | `object.py` | `world-object-chunk-ref.schema.v1` | strukturell vorbereitet |

---

## 4. Fachliche Gesamtstruktur

```text
Project
│
├── ProjectAccessAssignment
│   ├── direct
│   │   └── auth_user_id → owner/admin/editor/viewer
│   └── group
│       └── group_id → admin/editor/viewer
│
├── Legacy-/Kompatibilitätszweig
│   ├── ProjectRole
│   │   └── ProjectRoleAssignment
│   │       ├── user:<legacy-user-id>
│   │       └── group:<group-id>
│   └── ProjectGroup
│       ├── ProjectGroupMember
│       └── ProjectRoleAssignment
│
├── Universe
│   │
│   └── WorldInstance
│       │
│       ├── ChunkSnapshot
│       ├── WorldCommandLog
│       ├── ChunkEvent
│       ├── WorldObjectInstance
│       │   └── WorldObjectChunkRef
│       └── BlockRegistry-Kontext über öffentliche Registry-ID/Version
│
└── externe Serviceverknüpfungen
    ├── Project.external_app_project_id
    │   → öffentliche Projekt-ID aus vectoplan-app
    │   → keine Datenbank-Fremdschlüsselbeziehung
    ├── Project.owner_auth_user_id
    │   → kanonische Auth-Benutzeridentität
    └── ProjectAccessAssignment.chunk_project_id
        → öffentliche Chunk-Projekt-ID
        → bewusst kein Cross-Service-Foreign-Key
```

Die kanonische Access-Projektion referenziert das Chunk-Projekt über `chunk_project_id` und nicht über den lokalen Datenbankprimärschlüssel. Die vier Legacy-Modelle bleiben dagegen über `project_db_id` an `projects.id` gebunden. Dadurch können Repositories die neue serviceübergreifende Projektion verwenden, während bestehende Rollen-/Gruppenpfade kompatibel bleiben.


Blockdefinitionen stehen parallel zum Projektgraphen:

```text
BlockRegistry
└── BlockType
```

Eine `WorldInstance` referenziert die verwendete Registry über:

```text
block_registry_id
block_registry_version
```

Ein `ChunkSnapshot` speichert diese Registryidentität ebenfalls, damit ältere Snapshots auch nach späteren Registryänderungen interpretierbar bleiben.

---

## 5. Gemeinsame technische Regeln aller Models

### 5.1 SQLAlchemy-Anbindung

Alle persistenten Modelmodule importieren:

```python
from extensions import db
```

Wenn `extensions.db` nicht verfügbar ist, schlägt der Import mit einem klaren `RuntimeError` fehl. Dadurch werden unvollständige App-/Bootstrap-Initialisierungen früh sichtbar.

### 5.2 PostgreSQL und JSON

Die Modelschicht verwendet bevorzugt PostgreSQL `JSONB`.

Für Tests oder alternative SQLAlchemy-Werkzeuge existieren Fallbacks auf `db.JSON`.

JSON-Felder werden defensiv normalisiert:

```text
Mapping
→ JSON-Objekt

Sequence
→ JSON-Liste

datetime
→ ISO-UTC-Zeitstempel

unbekannter Typ
→ sichere Stringrepräsentation
```

### 5.3 Interne und öffentliche IDs

Es gibt zwei Identitätsebenen:

```text
id
→ interner BigInteger-Datenbankprimärschlüssel
→ für Foreign Keys und Joins

project_id / universe_id / world_id / snapshot_id / command_id / event_id / ...
→ stabile öffentliche oder serviceinterne API-ID
→ für Routen, Payloads, Logs und Servicekopplung
```

Öffentliche IDs sind keine Datenbank-Fremdschlüssel zwischen Microservices.

Für Project Access gilt zusätzlich:

```text
Kanonischer Vertrag
→ Project.owner_auth_user_id
→ Project.created_by_auth_user_id
→ Project.updated_by_auth_user_id
→ ProjectAccessAssignment.auth_user_id
→ ausschließlich kanonische auth_user_id
→ numerische lokale IDs und E-Mail-Adressen werden abgelehnt

Gruppenvertrag
→ ProjectAccessAssignment.group_id
→ genau ein Subject: auth_user_id XOR group_id

Legacy-/Kompatibilitätsvertrag
→ ProjectGroupMember.user_id
→ ProjectRoleAssignment.user_id
→ historische externe Stringfelder ohne Cross-Service-Foreign-Key
→ neue Servicepfade dürfen diese Felder nicht als alternative Identitätswahrheit verwenden
```

Beispiel:

```text
Project.external_app_project_id
→ speichert die öffentliche App-Projekt-ID
→ besitzt keinen Foreign Key zur Datenbank von vectoplan-app
```

### 5.4 Factory-Muster

Fast jedes Model besitzt mindestens:

```text
create(...)
→ validiert und normalisiert Eingaben
→ erzeugt eine unpersistierte SQLAlchemy-Instanz

create_for_<parent>(...)
→ übernimmt interne IDs aus einem bereits persistierten Parent-Model

from_<payload>(...)
→ liest kompatible camelCase-/snake_case-API-Felder
→ delegiert an create(...)
```

Wichtig:

```text
create(...)
→ kein db.session.add(...)
→ kein flush()
→ kein commit()
→ kein rollback()
```

### 5.5 Mutationsmuster

Aktualisierbare Models verwenden typischerweise:

```text
touch(...)
→ updated_at aktualisieren
→ bei revisionierten Models revision erhöhen
→ optional updated_by_user_id/session aktualisieren

ensure_not_deleted(...)
→ Mutation an soft-gelöschten Datensätzen verhindern

archive()
restore()
soft_delete()
→ Status und Zeitstempel konsistent pflegen
```

### 5.6 Validierung

Jedes zentrale Model besitzt:

```text
get_validation_errors()
→ gibt ein Dictionary mit Feldfehlern zurück
→ wirft im normalen Prüfpfad nicht selbst hart
```

Die Factory- und Mutationsmethoden verwenden zusätzlich direkte `ValueError`-Validierung.

### 5.7 Serialisierung

Die meisten Models stellen bereit:

```text
to_dict(...)
to_public_dict(...)
```

`to_public_dict()` lässt interne Datenbank-IDs weg.

Große Inhalte und Beziehungen werden nur über explizite Parameter eingeschlossen. Dadurch sollen Status- und API-Routen keine tiefen ORM-Graphen versehentlich serialisieren.

### 5.8 Transaktionsgrenze

Die Modelschicht kennt ihre eigene fachliche Konsistenz, besitzt aber nicht die äußere Transaktion.

Beispiel einer Blockänderung:

```text
Service/Command-Executor beginnt Transaktion
→ WorldCommandLog erzeugen
→ ChunkSnapshot laden oder erzeugen
→ Snapshotinhalt ändern
→ ChunkEvent erzeugen
→ CommandLog auf applied setzen
→ gemeinsamer Commit

bei Fehler
→ gemeinsamer Rollback
```

Diese atomare Orchestrierung gehört nicht in einzelne Modelmethoden.

---

## 6. `models/__init__.py`

### 6.1 Aufgabe

`models/__init__.py` ist keine einfache leere Paketdatei.

Sie ist die zentrale Registrierungs- und Diagnoseschicht für alle SQLAlchemy-Models.

Sie sorgt dafür, dass:

```text
alle Modelmodule importiert werden
→ alle Tabellen in SQLAlchemy-Metadata registriert sind
→ Bootstrap und spätere Migrationen das vollständige Schema sehen
→ Statusrouten fehlende Module/Klassen/Spalten melden können
```

### 6.2 Feste Importreihenfolge

```text
project
→ project_access_assignment
→ project_access
→ universe
→ world
→ block
→ chunk
→ event
→ object
```

Die Reihenfolge ist bewusst stabil.

### 6.3 Erwartete Klassen

```text
Project
ProjectAccessAssignment
ProjectRole
ProjectGroup
ProjectGroupMember
ProjectRoleAssignment
Universe
WorldInstance
BlockRegistry
BlockType
ChunkSnapshot
WorldCommandLog
ChunkEvent
WorldObjectInstance
WorldObjectChunkRef
```

### 6.4 Klassen- und Tabellenmapping

Die Datei hält zentrale Mappings:

```text
Modelklasse → Python-Modul
Modelklasse → Tabellenname
Modelklasse → erwartete Spalten
```

Dies wird für Diagnose und Readiness verwendet.

### 6.5 Diagnoseobjekte

`ModelImportRecord`

```text
beschreibt den Import eines einzelnen Modelmoduls
→ Modulname
→ Importpfad
→ erfolgreich/fehlgeschlagen
→ Fehlertext
→ optional Traceback
→ exportierte Symbole
```

`ModelClassRecord`

```text
beschreibt eine erwartete Modelklasse
→ Klasse verfügbar
→ Tabelle
→ Spalten
→ fehlende erwartete Spalten
→ Relationships
```

`ModelPackageStatus`

```text
beschreibt den Gesamtzustand des models-Pakets
→ ready
→ importierte Module
→ fehlgeschlagene Module
→ fehlende Klassen
→ verfügbare Klassen
→ Spaltenabweichungen
```

### 6.6 Zentrale öffentliche Funktionen

```text
get_model_package_status()
is_models_package_ready()
require_models_ready()
require_expected_model_columns()

get_model_class_map()
get_model_registry()
get_model_table_map()
get_model_column_map()
get_model_relationship_map()

get_model_class()
require_model_class()
iter_model_classes()
get_model_table_names()

get_model_debug_summary()
is_model_column_available()

is_app_integration_model_shape_ready()
get_project_access_projection_contract()
get_legacy_project_access_model_contract()
get_project_access_model_contract()
is_project_access_projection_model_shape_ready()
is_legacy_project_access_model_shape_ready()
is_project_access_model_shape_ready()
is_core_world_model_shape_ready()

validate_model_instances()
serialize_model_instance()
serialize_model_instances()
build_model_identity()
build_model_schema_report()

reset_model_import_cache()
```

### 6.7 Readiness-Bedeutung

`ready = true` bedeutet aktuell:

```text
alle neun erwarteten Module wurden importiert
und
alle fünfzehn erwarteten Modelklassen sind vorhanden
```

Fehlende erwartete Spalten werden separat gemeldet. Sie machen den Paketimport nicht automatisch hart ungültig, damit ältere lokale Datenbanken vor einem expliziten Bootstrap-/Migrationslauf noch diagnostiziert werden können.

Die Access-Diagnostik ist jetzt dreigeteilt:

```text
get_project_access_projection_contract()
→ kanonisches ProjectAccessAssignment-Modell
→ canonicalUserIdField = auth_user_id
→ projectIdField = chunk_project_id
→ Rollen owner/admin/editor/viewer
→ direct/group
→ viewerReadOnly = true

get_legacy_project_access_model_contract()
→ vier bestehende Rollen-/Gruppenmodelle
→ deren Modelvertrag meldet weiterhin authzEnforced = false

get_project_access_model_contract()
→ kombinierter Gesamtvertrag
→ canonicalProjection + legacyRoleGroups
→ sourceOfTruth = vectoplan-app
→ viewerReadOnly = true
```

`is_project_access_model_shape_ready()` ist nur dann wahr, wenn sowohl die kanonische Projektion als auch die Legacy-Rollen-/Gruppenstruktur vollständig registriert sind.

### 6.8 Was diese Datei bewusst nicht macht

```text
keine Tabellen erstellen
keine Migration ausführen
keine Default-Daten seeden
keinen Projektgraphen provisionieren
keine Chunks laden
keine Commands ausführen
```

---

## 7. `project.py` – `Project`

### 7.1 Aufgabe

`Project` ist der oberste persistente Container des Chunk-Services.

Es ist nicht dasselbe wie ein Projektobjekt der `vectoplan-app`.

```text
vectoplan-app
→ besitzt App-Projekte

vectoplan-chunk
→ besitzt eigene Chunk-Projekte
```

Die Verbindung erfolgt über:

```text
Project.external_app_project_id
```

### 7.2 Tabelle

```text
projects
```

### 7.3 Feldgruppen

Identität:

```text
id
project_id
slug
name
description
```

Status und Versionierung:

```text
status
schema_version
revision
```

Default- und Spawnreferenzen:

```text
default_universe_id
default_world_id
spawn_world_id
```

App-/Serviceintegration:

```text
external_app_project_id
source_service
external_url
```

Kanonischer Eigentümer- und Auditkontext:

```text
owner_auth_user_id
owner_type
owner_id
created_by_auth_user_id
updated_by_auth_user_id
created_by_user_id
updated_by_user_id
```

Die Legacy-Spalten `owner_id`, `created_by_user_id` und `updated_by_user_id` führen im aktuellen Vertrag denselben kanonischen Auth-Wert. Sie dürfen nicht mit lokalen numerischen App-IDs befüllt werden.

World-Template- und Fallbackzustand:

```text
world_template_requested
world_template_effective
world_fallback_used
world_fallback_code
earth_reference_fingerprint
world_metadata_json
```

Provisionierungszustand:

```text
provisioning_status
provisioning_fingerprint
provisioning_request_id
provisioning_correlation_id
provisioning_error_code
provisioning_retryable
provisioning_repair_required
provisioning_attempts
provisioned_at
provisioning_updated_at
```

Access-Sync-Zustand:

```text
access_sync_status
access_projection_version
access_projection_fingerprint
access_sync_request_id
access_sync_correlation_id
access_sync_error_code
access_sync_retryable
access_sync_repair_required
access_sync_attempts
access_synced_at
access_sync_updated_at
```

Metadaten und Zeitstempel:

```text
metadata_json
created_at
updated_at
archived_at
deleted_at
```

### 7.4 Eindeutigkeiten

```text
project_id
→ global eindeutig

slug
→ global eindeutig, wenn gesetzt

external_app_project_id
→ global eindeutig, wenn gesetzt
```

### 7.5 Erzeugung

Allgemein:

```text
Project.create(...)
```

Dev-Default:

```text
Project.create_dev_project(
    project_id="dev-project",
    default_universe_id="dev-universe",
    default_world_id="world_spawn",
    owner_user_id="auth_dev_owner"
)
```

Der Dev-Graph fordert bewusst `flat` an, setzt den Provisionierungsstatus auf `ready` und lässt die Access-Projektion zunächst `pending`, bis Bootstrap/Access-Service die Owner-Zuweisungen synchronisiert hat.

App-Provisioning:

```text
Project.create_for_app_project(
    app_project_public_id="prj_...",
    ...
)
```

Dabei entsteht eine deterministische serviceeigene `project_id`, während die App-ID in `external_app_project_id` gespeichert wird. Der Owner ist verpflichtend und muss eine kanonische `auth_user_id` sein. Neue App-Projekte fordern standardmäßig das Template `earth` an; der effektive Templatezustand wird erst durch den Provisionierungsservice gesetzt.

API-Payload:

```text
Project.from_create_payload(...)
```

Unterstützt mehrere kompatible Feldnamen wie:

```text
projectId
project_id
chunkProjectId
chunk_project_id
externalAppProjectId
appProjectPublicId
```

### 7.6 Mutationen

```text
rename()
update_description()

set_default_universe_id()
set_default_world_id()
set_spawn_world_id()
set_world_refs()

ensure_external_app_link()
set_external_app_link()
set_owner()
set_owner_user()
clear_owner()

set_world_template_state()
apply_provisioning_state()
apply_access_sync_state()

set_status()
archive()
restore()
soft_delete()

replace_metadata()
update_metadata()
merge_provisioning_metadata()
apply_patch_payload()
normalize_for_persistence()
```

`apply_patch_payload()` schützt Owner-, App-Link-, World-Referenz-, Template-, Provisionierungs- und Access-Sync-Felder standardmäßig. Diese Felder gehören in dedizierte interne Serviceoperationen; ein generischer Projektpatch darf sie nicht still verändern.

### 7.7 Project-Access-Bezug

`Project` ist die fachliche Parent-Identität für beide Access-Persistenzformen:

```text
Kanonische Projektion
ProjectAccessAssignment.chunk_project_id
→ referenziert Project.project_id als stabile öffentliche ID
→ bewusst ohne lokalen Foreign Key

Legacy-/Kompatibilitätsmodelle
ProjectRole.project_db_id
ProjectGroup.project_db_id
ProjectGroupMember.project_db_id
ProjectRoleAssignment.project_db_id
→ Foreign Key auf projects.id
→ ondelete = CASCADE
```

`Project.owner_auth_user_id` ist die kanonische Owneridentität. Das Modell lehnt numerische lokale IDs, E-Mail-Adressen, anonyme Identitäten sowie widersprüchliche Owner-Aliase ab. Die verbindliche Owner-Projektion und ein Owner-Transfer werden außerhalb des Models atomar durch den Project-Access-Service synchronisiert.

### 7.8 Wichtige Invarianten

```text
Project.project_id ist die öffentliche Chunk-Projekt-ID.

Project.external_app_project_id ist nur eine Serviceverknüpfung.

Project.owner_auth_user_id ist die kanonische Owneridentität.

Lokale numerische User-IDs und E-Mail-Adressen sind keine zulässigen Owner-/Actor-Identitäten.

Normale Provisionierungs-Retries dürfen weder Owner noch bestehendes requested/effective Template still ändern.

Earth→Flat wird nur als expliziter Fallbackzustand mit Code gespeichert.

Öffentliche Serialisierung gibt keine rohen Auth-IDs oder internen URLs aus.

Generische Metadaten werden begrenzt und um Credentials, Identitäten sowie Chunk-/World-Rohdaten bereinigt.

Project erzeugt Universe und WorldInstance nicht selbst.

Project führt keine Abfragen, Remote Calls, Transaktionen oder Commits aus.

Soft-Delete behält historische Chunks, Commands und Events grundsätzlich bei.
```

---

## 8. `project_access_assignment.py` – `ProjectAccessAssignment`

### 8.1 Aufgabe und Sicherheitsgrenze

`ProjectAccessAssignment` ist die kanonische persistente Zugriffsprojektion des Chunk-Services.

```text
vectoplan-app
→ bleibt Source of Truth für Mitgliedschaft und Projektrolle

ProjectAccessAssignment
→ speichert die synchronisierte, lokal durchsetzbare Projektion

project_access_service.py
→ berechnet Entscheidungen, synchronisiert Assignments und erzwingt Owner-/Viewer-Regeln
```

Das Model selbst authentifiziert keinen Request und entscheidet nicht, ob eine konkrete Route ausgeführt werden darf. Es stellt jedoch den gehärteten Datenvertrag bereit, auf dem diese Entscheidung basiert.

### 8.2 Tabelle und Schema

```text
Tabelle        = project_access_assignments
Schema-Version = project-access-assignment.schema.v1
Projektion     = app-project-access-v1
```

### 8.3 Feldgruppen

Identität und Projektbezug:

```text
id
assignment_id
chunk_project_id
```

Subject und Rolle:

```text
auth_user_id
group_id
role
assignment_type
```

Synchronisierungszustand:

```text
active
managed
source_service
projection_version
projection_fingerprint
request_id
correlation_id
```

Metadaten, Version und Zeit:

```text
metadata_json
schema_version
revision
created_at
updated_at
deactivated_at
```

### 8.4 Subject-Vertrag

Es existieren genau zwei Assignmenttypen:

```text
direct
→ auth_user_id gesetzt
→ group_id leer

group
→ group_id gesetzt
→ auth_user_id leer
```

Die Datenbank sichert dieses XOR über `ck_project_access_subject_complete` ab.

Direkte User-Assignments akzeptieren ausschließlich kanonische, opaque `auth_user_id`-Werte. Abgelehnt werden insbesondere:

```text
rein numerische lokale User-IDs
E-Mail-Adressen
Account-/AppUser-Felder
verschachtelte user.id-/owner.id-Payloads
Pfade oder Backslashes in der Identität
leere oder ungültige Subjects
```

`from_payload()` prüft eingehende Payloads rekursiv auf lokale Identitätsfelder. Gruppen sind im App-Direct-Projection-Pfad standardmäßig nicht erlaubt und benötigen ein ausdrückliches `allow_group=True`.

### 8.5 Rollenvertrag

Erlaubt sind exakt:

```text
owner
admin
editor
viewer
```

Aliase wie `administrator`, `write`, `read` oder `readonly` werden kanonisch normalisiert.

Owner-Invariante:

```text
owner
→ immer assignment_type = direct
→ immer kanonische auth_user_id
→ niemals Gruppe
```

Die Datenbank enthält bewusst keine partielle Unique-Constraint für den aktiven Owner. Genau ein aktiver Owner wird transaktional im `project_access_service.py` erzwungen, damit ein atomarer Owner-Transfer nicht durch SQLAlchemy-Autoflush blockiert wird.

### 8.6 Eindeutigkeiten und Indizes

```text
assignment_id
→ global eindeutig

unique(chunk_project_id, assignment_type, auth_user_id)
→ höchstens ein Direct Assignment pro Projekt/User

unique(chunk_project_id, assignment_type, group_id)
→ höchstens ein Group Assignment pro Projekt/Gruppe
```

Zusätzliche Indizes unterstützen:

```text
Projekt + active + assignment_type
Projekt + role + active
source_service + Projekt
```

### 8.7 Erzeugung

Direkt:

```text
ProjectAccessAssignment.create_direct(
    chunk_project_id=...,
    auth_user_id=...,
    role=...,
)
```

Gruppe:

```text
ProjectAccessAssignment.create_group(
    chunk_project_id=...,
    group_id=...,
    role=...,
)
```

Wichtige Defaults:

```text
Direct Assignment
→ managed = true
→ source_service = vectoplan-app
→ projection_version = app-project-access-v1

Group Assignment
→ managed = false
→ source_service = group-directory
→ Owner-Rolle verboten
```

Keine Factory fügt das Objekt einer Session hinzu oder committed.

### 8.8 Mutationen und Idempotenz

```text
set_role()
activate()
deactivate()
apply_direct_projection()
replace_metadata()
update_metadata()
touch()
```

`apply_direct_projection()` ist idempotent und schützt die Subject-Identität:

```text
bestehendes Direct Assignment
→ auth_user_id darf nicht auf einen anderen User umgebogen werden
→ Rolle/Projektionsdaten dürfen aktualisiert werden
→ managed und active werden für die App-Projektion wiederhergestellt
```

Deaktivierung erhält Audit- und Projektionshistorie; es findet kein hartes Löschen statt.

### 8.9 Serialisierung und Redaction

```text
to_public_dict()
→ keine lokale DB-ID
→ keine rohe auth_user_id
→ keine rohe group_id
→ Subject nur als nicht umkehrbarer Fingerprint

to_service_dict()
→ rohe Subject-ID für vertrauenswürdige interne Services
→ sanitisierte Metadaten

to_dict(include_private=..., include_internal=...)
→ explizit steuerbarer Vertrag
```

Metadaten entfernen beziehungsweise redigieren Secrets, E-Mail-Adressen, URLs, lokale Identitätsfelder und große Domainpayloads wie Chunks, Blocks, Geometrien oder Snapshots.

### 8.10 SQLAlchemy-Hooks

Idempotent installierte `before_insert`-/`before_update`-Listener normalisieren und validieren:

```text
IDs
Assignmenttyp
Rolle
Subject-XOR
Source Service
Projektionsfelder
Metadaten
Revision
Zeitstempel
active/deactivated_at-Konsistenz
```

Die Listener führen keine Queries und keine Commits aus.

### 8.11 Verhältnis zu `project_access.py`

```text
ProjectAccessAssignment
→ kanonische serviceübergreifende Projektion
→ auth_user_id
→ direkt für Access-Reconciliation und Enforcement vorgesehen

project_access.py
→ Legacy-/Kompatibilitätsstruktur
→ detaillierte Rollen-, Gruppen- und Membershiptabellen
→ weiterhin für Bootstrap, Gruppenstruktur und Übergangspfad vorhanden
```

Beide Strukturen sind im aktuellen Modelpaket registriert und werden getrennt diagnostiziert. `is_project_access_model_shape_ready()` verlangt aktuell beide Verträge.

### 8.12 Aktueller Status

Implementiert und statisch im vorliegenden Code bestätigt:

```text
kanonische User-ID-Validierung
Direct-/Group-XOR
Owner nur direct
Viewer-Rolle
Public-Redaction
Projection-Fingerprints
idempotente Direct-Reconciliation
Gruppenerhalt als eigener Assignmenttyp
SQLAlchemy-Adapter-Aliase
Insert-/Update-Listener
```

Die vollständige HTTP-Guard-Abdeckung und der dedizierte Owner-Transfer über reale Routen bleiben außerhalb dieser Modeldatei als End-to-End-Integrationspunkte zu bestätigen.

---

## 9. `universe.py` – `Universe`

### 9.1 Aufgabe

Ein `Universe` gruppiert eine oder mehrere konkrete `WorldInstance`-Zeilen innerhalb eines Chunk-Projekts.

```text
Project
└── Universe
    └── WorldInstance
```

### 9.2 Tabelle

```text
universes
```

### 9.3 Feldgruppen

Elternbezug und Identität:

```text
id
project_db_id
universe_id
slug
name
description
```

Status und Klassifikation:

```text
status
schema_version
revision
universe_role
universe_scope
```

Worldreferenzen:

```text
default_world_id
spawn_world_id
```

Audit und Metadaten:

```text
created_by_user_id
updated_by_user_id
metadata_json
created_at
updated_at
archived_at
deleted_at
```

### 9.4 Eindeutigkeit

```text
unique(project_db_id, universe_id)
unique(project_db_id, slug)
```

Eine `universe_id` muss damit nur innerhalb eines Projekts eindeutig sein.

### 9.5 Rollen

```text
default
workspace
sandbox
simulation
```

Aktueller Scope:

```text
project
```

### 9.6 Erzeugung

```text
Universe.create(...)
Universe.create_for_project(project, ...)
Universe.from_create_payload(...)
```

`create_for_project()` benötigt ein bereits persistiertes `Project.id`.

### 9.7 Mutationen

```text
rename()
update_description()
set_role()

set_default_world_id()
set_spawn_world_id()
set_world_defaults()

set_status()
archive()
restore()
soft_delete()

replace_metadata()
update_metadata()
merge_provisioning_metadata()
apply_patch_payload()
```

### 9.8 Fallbackreferenzen

```text
effective_default_world_id
→ default_world_id oder spawn_world_id

effective_spawn_world_id
→ spawn_world_id oder default_world_id
```

### 9.9 Wichtige Invarianten

```text
Universe bleibt vollständig intern im Chunk-Service.

project_db_id referenziert projects.id.

default_world_id und spawn_world_id sind öffentliche World-IDs,
keine internen Datenbank-IDs.

Worlds werden nicht innerhalb des Models erstellt.
```

---

## 10. `world.py` – `WorldInstance`

### 10.1 Aufgabe

`WorldInstance` ist die konkrete persistente editierbare Welt.

Sie ist nicht identisch mit einem Provider oder Template.

Richtig:

```text
world_id         = world_spawn
provider_id      = flat
template_id      = flat
provider_world_id = flat
```

oder für Earth:

```text
world_id          = world_spawn oder chk_wld_...
provider_id       = earth
template_id       = earth
provider_world_id = earth
```

Falsch:

```text
world_id = flat
world_id = earth
```

### 10.2 Tabelle

```text
world_instances
```

### 10.3 Feldgruppen

Hierarchie und Identität:

```text
id
project_db_id
universe_db_id
world_id
slug
name
description
```

Status und fachliche Rolle:

```text
status
schema_version
revision
world_type
world_role
world_scope
```

Provider- und Generatorvertrag:

```text
template_id
provider_id
provider_world_id
generator_type
generator_version
projection_type
topology_type
coordinate_system
seed
```

Chunk- und Weltgeometrie:

```text
chunk_size
cell_size
surface_y
min_y
max_y
```

Blockregistry:

```text
block_registry_id
block_registry_version
```

Legacy-/Blockspawn:

```text
spawn_x
spawn_y
spawn_z
spawn_yaw
spawn_pitch
```

Präziser lokaler Spawn:

```text
spawn_coordinate_space
spawn_x_precise
spawn_y_precise
spawn_z_precise
```

Servicekontext:

```text
source_service
external_ref
created_by_user_id
updated_by_user_id
metadata_json
```

Earth-Referenzvertrag:

```text
coordinate_frame_revision
global_reference_json
global_reference_fingerprint
global_reference_locked_at
global_reference_lock_reasons_json
global_reference_updated_at
global_reference_updated_by_user_id
```

Zeitstempel:

```text
created_at
updated_at
archived_at
deleted_at
```

### 10.4 Eindeutigkeit

```text
unique(universe_db_id, world_id)
unique(universe_db_id, slug)
```

Eine `world_id` ist nur innerhalb eines Universums eindeutig.

### 10.5 Flat-Defaults

```text
world_id          = world_spawn
template_id       = flat
provider_id       = flat
provider_world_id = flat
generator_type    = flat-world
generator_version = 1
projection_type   = flat-local-v1
topology_type     = flat-unbounded-v1
coordinate_system = vectoplan-world-y-up-v1
chunk_size        = 16
cell_size         = 1
surface_y         = 0
min_y             = -8
max_y             = 64
```

Factory:

```text
WorldInstance.create_flat_spawn(...)
```

### 10.6 Earth-v1-Vertrag

Earth ist als zusätzlicher Provider im Model implementiert.

Feste Earth-Identität:

```text
template_id       = earth
provider_id       = earth
provider_world_id = earth
generator_type    = earth-flat-periodic
generator_version = 1
projection_type   = vectoplan-periodic-equirectangular
topology_type     = periodic-x-v1
coordinate_system = vectoplan-earth-grid-v1
chunk_size        = 16
cell_size         = 1
min_y             = -1024
max_y             = 8192
```

Factory:

```text
WorldInstance.create_earth_spawn(global_reference=...)
```

Diese Factory:

```text
validiert den GlobalReferencePoint
→ lädt Earth-Definition und Earth-Provider
→ ermittelt den lokalen Default-Spawn
→ persistiert den Spawn als local_metric
→ persistiert genau einen globalen Referenzvertrag
```

Der inzwischen bestätigte Provisioning-Pfad erzeugt aus einer kompakten API-Eingabe zuerst den vollständigen Domainvertrag:

```text
explizites CRS, Longitude, Latitude, optionale Höhe
→ resolve_crs(...)
→ GlobalCoordinate mit Decimalwerten
→ EarthGridReference des Earth-v1-Grids
→ GlobalReferencePoint
→ to_persistence_dict()
→ WorldInstance.create_earth_spawn(...)
```

Bestätigt persistiert wurden unter anderem:

```text
CRS                 = EPSG:4979
CRS-Definition      = WKT2:2019
Grid                = vectoplan-earth-grid@1
Topologie           = periodic-x-v1
coordinate dimension = 3
reference version   = 1
```

Der Integer-Spawn wird aus dem präzisen lokalen Spawn mit mathematischem Floor abgeleitet. Generische Flat-Defaults dürfen Earth-Spawn oder Earth-Vertikalgrenzen nicht überschreiben.

### 10.7 Earth-Feldinvarianten

Flat-Welt:

```text
global_reference_json = null
global_reference_fingerprint = null
coordinate_frame_revision = 0
```

Earth-Welt:

```text
global_reference_json != null
global_reference_fingerprint != null
coordinate_frame_revision >= 1
```

Zusätzlich:

```text
provider_id = earth
→ global_reference_json ist Pflicht
```

Präzise Spawnfelder:

```text
entweder alle drei null
oder alle drei gesetzt
```

### 10.8 Globale Referenz und Reanchoring

Zentrale Methoden:

```text
set_global_reference()
replace_global_reference_before_materialization()
clear_global_reference_before_materialization()

ensure_global_reference_mutable()
lock_global_reference()
```

Bedeutung:

```text
Vor Materialisierung
→ Referenz kann kontrolliert gesetzt oder ersetzt werden

Nach Materialisierung
→ Referenz wird gesperrt
→ normales Reanchoring ist nicht mehr erlaubt
→ spätere Änderung benötigt einen eigenen Migrationspfad
```

Spawnverschiebung ist davon getrennt:

```text
set_spawn_position()
set_spawn_metric_position()
→ ändert den lokalen Spawn
→ ändert nicht den GlobalReferencePoint
→ reanchort die Welt nicht
```

### 10.9 Provider- und Konfigurationsmethoden

```text
set_provider_mapping()
set_world_geometry()
set_chunk_grid()
set_vertical_bounds()
set_block_registry()
set_seed()
set_source_context()
ensure_bootstrap_defaults()
```

### 10.10 Runtime-/API-Kontexte

Properties und Hilfen:

```text
chunk_config
provider_mapping
registry_context

spawn_position
spawn_precise_position
spawn_metric_position
spawn_rotation
spawn_context

global_reference_context()
coordinate_frame_context

build_earth_provider()
build_world_context_key()
build_route_hints()
```

### 10.11 Erzeugung

```text
WorldInstance.create(...)
WorldInstance.create_flat_spawn(...)
WorldInstance.create_earth_spawn(...)
WorldInstance.create_for_universe(...)
WorldInstance.from_create_payload(...)
```

`from_create_payload()` erkennt anhand von `providerId`, ob Flat- oder Earth-Defaults verwendet werden müssen.

### 10.12 Wichtige Invarianten

```text
WorldInstance speichert Weltkonfiguration, keine Chunkzellen.

Chunkzellen liegen in ChunkSnapshot.

flat und earth sind Provideridentitäten, keine konkrete world_id.

Earth besitzt genau einen globalen Referenzvertrag.

Earth verwendet `min_y=-1024` und `max_y=8192`, sofern keine expliziten Earth-spezifischen Konfigurationswerte gesetzt sind.

Der präzise Earth-Spawn ist die fachliche Quelle; `spawn_x/y/z` sind die gefloorten Integerrepräsentationen derselben lokalen Position.

Blocks, Chunks, Commands, Events, Objekte und Spawn bleiben lokal adressiert.

WorldInstance führt keinen Commit aus.
```

---

## 11. `project_access.py` – Legacy-Rollen, Gruppen und Zuweisungen

### 11.1 Aufgabe und Sicherheitsgrenze

`project_access.py` ist die weiterhin registrierte Legacy-/Kompatibilitätsschicht für detaillierte projektbezogene Rollen, Gruppen, Mitgliedschaften und Rollenzuweisungen.

Der kanonische serviceübergreifende Direct-User-Vertrag liegt inzwischen in `project_access_assignment.py`. Das Legacy-Modul bleibt wichtig für bestehende Daten, Gruppenstrukturen, Bootstrapkompatibilität und den Übergangspfad, ist aber nicht die neue Identitätswahrheit.

Das Modul speichert:

```text
ProjectRole
ProjectGroup
ProjectGroupMember
ProjectRoleAssignment
```

Es führt ausdrücklich keine Authentifizierung oder Autorisierung aus.

```text
Persistenzvertrag
→ implementiert

Berechnung effektiver Rechte
→ Serviceverantwortung

HTTP-Request erlauben/verbieten
→ nicht Aufgabe dieses Legacy-Moduls

Legacy-Modelvertrag
→ authzEnforced = false
```

Die zentrale Servicegrenze lautet:

```text
vectoplan-app / vectoplan-auth
→ besitzt Benutzeridentitäten

ProjectAccessAssignment
→ kanonischer neuer Vertrag mit auth_user_id

project_access.py
→ Legacy-Userfelder und Gruppenstruktur
→ keine Foreign Keys in fremde Datenbanken
→ darf nicht als konkurrierende Source of Truth verwendet werden
```

### 11.2 Gemeinsame Basisklasse `ProjectAccessRecord`

Die abstrakte Basisklasse enthält:

```text
id
schema_version
revision
created_by_user_id
updated_by_user_id
metadata_json
created_at
updated_at
deleted_at
```

Gemeinsame Methoden:

```text
touch()
replace_metadata()
update_metadata()
soft_delete()
restore()
ensure_not_deleted()
```

Wichtige Regeln:

```text
schema_version = 1
revision beginnt bei 1
Audit-User-IDs sind externe Strings
Soft-Delete erhält Historie
Modelmethoden führen keine DB-Abfragen, Commits oder Rollbacks aus
```

### 11.3 Standardrollen und Permissions

Feste Standardrollen:

```text
owner
admin
editor
viewer
```

Bekannte Permission-Keys:

```text
view
edit
manage
delete
transfer
embed
view_settings
manage_settings
view_team
manage_team
view_admin
```

Default-Zuordnung:

| Rolle | Allow | Deny |
|---|---|---|
| `owner` | alle bekannten Permission-Keys | keine |
| `admin` | Verwaltung einschließlich Team/Settings, aber ohne Eigentumsübertragung | `transfer` |
| `editor` | `view`, `edit`, `embed` | keine |
| `viewer` | `view` | keine |

Permissions werden kanonisch als JSON gespeichert:

```json
{
  "version": 1,
  "allow": ["view"],
  "deny": []
}
```

Doppelte Einträge werden normalisiert. Ein expliziter Deny-Eintrag bleibt Teil des gespeicherten Vertrags. Die tatsächliche Auswertung gehört in die Service-/Autorisierungsschicht.

### 11.4 `ProjectRole`

Tabelle:

```text
project_roles
```

Felder:

```text
id
role_id
project_db_id
role_key
name
description
permissions_json
is_system
status

+ gemeinsame Audit-/Metadata-/Soft-Delete-Felder
```

Eindeutigkeiten:

```text
unique(project_db_id, role_id)
unique(project_db_id, role_key)
```

Status:

```text
active
inactive
archived
deleted
```

Erzeugung und Mutation:

```text
ProjectRole.create(...)
ProjectRole.from_create_payload(...)
set_permissions(...)
set_status(...)
archive(...)
apply_patch_payload(...)
validate_or_raise()
to_dict(...)
```

Die Beziehung `assignments` verwendet `lazy="raise"`. Dadurch werden Rollenzuweisungen nicht versehentlich durch eine normale Rollenserialisierung nachgeladen.

### 11.5 `ProjectGroup`

Tabelle:

```text
project_groups
```

Felder:

```text
id
group_id
project_db_id
group_key
name
description
is_system
status

+ gemeinsame Audit-/Metadata-/Soft-Delete-Felder
```

Eindeutigkeiten:

```text
unique(project_db_id, group_id)
unique(project_db_id, group_key)
```

Beziehungen:

```text
members
→ ProjectGroupMember
→ cascade = all, delete-orphan

role_assignments
→ ProjectRoleAssignment
→ cascade = save-update, merge
```

Beide Collections verwenden `lazy="raise"` und `passive_deletes=true`.

Erzeugung und Mutation:

```text
ProjectGroup.create(...)
ProjectGroup.from_create_payload(...)
set_status(...)
archive(...)
apply_patch_payload(...)
validate_or_raise()
to_dict(...)
```

### 11.6 `ProjectGroupMember`

Tabelle:

```text
project_group_members
```

Felder:

```text
id
membership_id
project_db_id
group_db_id
group_id
user_id
status
added_by_user_id
removed_by_user_id
starts_at
expires_at
removed_at
removal_reason

+ gemeinsame Audit-/Metadata-/Soft-Delete-Felder
```

Eindeutigkeit:

```text
membership_id
→ global eindeutig

unique(project_db_id, group_db_id, user_id)
→ ein aktiver/logischer Membership-Datensatz pro User und Gruppe
```

`user_id` ist eine externe String-ID. Sie besitzt keinen Foreign Key zu einem Auth-Service.

Status:

```text
active
inactive
removed
deleted
```

Zeitvertrag:

```text
starts_at = null oder Startzeit
expires_at = null oder Endzeit
starts_at < expires_at, wenn beide gesetzt
```

`is_effective()` berücksichtigt Status, Soft-Delete, Start, Ablauf und Entfernen.

Mutationen:

```text
remove(...)
reactivate(...)
apply_patch_payload(...)
validate_or_raise()
to_dict(...)
```

### 11.7 `ProjectRoleAssignment`

Tabelle:

```text
project_role_assignments
```

Felder:

```text
id
assignment_id
project_db_id
role_db_id
role_id
subject_type
user_id
group_db_id
group_id
subject_key
permission_overrides_json
status
assigned_by_user_id
revoked_by_user_id
starts_at
expires_at
revoked_at
revocation_reason

+ gemeinsame Audit-/Metadata-/Soft-Delete-Felder
```

Unterstützte Subjekte:

```text
subject_type = user
→ user_id gesetzt
→ group_db_id und group_id null
→ subject_key = user:<user-id>

subject_type = group
→ group_db_id und group_id gesetzt
→ user_id null
→ subject_key = group:<group-id>
```

Eindeutigkeit:

```text
assignment_id
→ global eindeutig

unique(project_db_id, role_db_id, subject_key)
→ dieselbe Rolle wird demselben Subjekt innerhalb eines Projekts nicht doppelt zugewiesen
```

Status:

```text
active
inactive
revoked
deleted
```

Factories:

```text
ProjectRoleAssignment.create(...)
create_for_user(...)
create_for_group(...)
from_create_payload(...)
```

Mutationen:

```text
set_permission_overrides(...)
revoke(...)
reactivate(...)
apply_patch_payload(...)
validate_or_raise()
to_dict(...)
```

`is_effective()` berücksichtigt Status, Soft-Delete, Gültigkeitsfenster und Widerruf.

### 11.8 Foreign Keys, Cascades und Scope

Interne Beziehungen:

```text
ProjectRole.project_db_id
ProjectGroup.project_db_id
ProjectGroupMember.project_db_id
ProjectRoleAssignment.project_db_id
→ projects.id
→ ondelete = CASCADE

ProjectGroupMember.group_db_id
→ project_groups.id
→ ondelete = CASCADE

ProjectRoleAssignment.role_db_id
→ project_roles.id
→ ondelete = CASCADE

ProjectRoleAssignment.group_db_id
→ project_groups.id
→ ondelete = CASCADE
```

Keine Foreign Keys existieren für:

```text
user_id
created_by_user_id
updated_by_user_id
added_by_user_id
removed_by_user_id
assigned_by_user_id
revoked_by_user_id
```

Alle Lookups und Unique-Verträge bleiben über `project_db_id` projektgescopt.

### 11.9 Öffentlicher Modelvertrag und Diagnose

`get_project_access_model_contract()` liefert ohne Datenbankzugriff:

```text
Schema-Version
Modelklassen und Tabellen
vollständige erwartete Spalten
Default-Rollen
bekannte Permissions
Subject-Typen user/group
Normalisierungs-Cacheinformationen
authzEnforced = false
externalUserForeignKeys = false
```

`models/__init__.py` integriert diesen Vertrag über:

```text
get_project_access_model_contract()
is_project_access_model_shape_ready()
build_model_schema_report()
```

### 11.10 Bestätigter Laufzeitstand

Bestätigt wurden:

```text
vier Standardrollen pro Projekt
→ owner
→ admin
→ editor
→ viewer

Legacy-Owner-Zuweisung
→ subjectType = user
→ aktuelle Bootstrapidentität = auth_dev_owner
→ Rolle owner

kanonische Owner-Projektion
→ ProjectAccessAssignment.assignmentType = direct
→ auth_user_id = auth_dev_owner oder realer App-Owner
→ Rolle owner

wiederholte Initialisierung
→ Rollen wiederverwendet
→ Owner-Zuweisung wiederverwendet
→ keine Duplikate

Gruppen-Create-/Read-Pfad
→ projektgescopt

ProjectGroupMember
→ Model-, Constraint- und Serialisierungsvertrag vorhanden
→ vollständiger Membership-Lifecycle noch nicht vollständig End-to-End bestätigt

Provisioning-Antwort
→ accessInitialized = true
→ projectDbId gesetzt
→ projectId gesetzt
```

Das Legacy-Modul selbst wertet weiterhin keine effektiven Berechtigungen aus. Sein eigener DB-freier Vertrag meldet deshalb weiterhin:

```text
authzEnforced = false
externalUserForeignKeys = false
```

Das widerspricht nicht dem neuen Servicevertrag: Die eigentliche Berechtigungsentscheidung liegt im `project_access_service.py` und nutzt primär die kanonische `ProjectAccessAssignment`-Projektion. Die vollständige Guard-Anbindung aller HTTP-Routen bleibt separat zu bestätigen.

---

## 12. `block.py` – `BlockRegistry` und `BlockType`

## 12.1 `BlockRegistry`

### Aufgabe

Eine Registryversion gruppiert eine stabile Menge von Blockdefinitionen.

Aktueller Default:

```text
registry_id      = debug-blocks
registry_version = 1
label            = Debug Blocks
source           = internal
```

### Tabelle

```text
block_registries
```

### Feldgruppen

```text
id
registry_id
registry_version
label
description

status
schema_version
revision
source
is_default

library_snapshot_id
created_by_user_id
updated_by_user_id
metadata_json

created_at
updated_at
archived_at
deleted_at
```

### Eindeutigkeit

```text
unique(registry_id, registry_version)
```

### Erzeugung

```text
BlockRegistry.create(...)
BlockRegistry.create_debug_registry(...)
BlockRegistry.from_create_payload(...)
```

### Mutationen

```text
set_default()
archive()
restore()
soft_delete()
replace_metadata()
update_metadata()
```

---

## 12.2 `BlockType`

### Aufgabe

`BlockType` speichert eine stabile Blockdefinition innerhalb einer Registryversion.

Es speichert nicht den konkreten Zellwert eines beliebigen Chunks.

Der konkrete Zellwert entsteht aus der jeweiligen Palette:

```text
cellValue = paletteIndex + 1
```

### Tabelle

```text
block_types
```

### Feldgruppen

Registryidentität:

```text
id
registry_db_id
registry_id
registry_version
block_type_id
```

Darstellung und Status:

```text
label
description
status
schema_version
revision
category
default_palette_index
```

Interaktion und Physik:

```text
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
```

Rendering:

```text
render_mode
shape_type
material_id
texture_id
icon_id
```

Spätere Library-Anbindung:

```text
library_type_id
library_variant_id
```

Audit und Metadaten:

```text
created_by_user_id
updated_by_user_id
metadata_json
created_at
updated_at
deprecated_at
deleted_at
```

### Eindeutigkeiten

```text
unique(registry_db_id, block_type_id)
unique(registry_id, registry_version, block_type_id)
unique(registry_db_id, default_palette_index)
```

### Kategorien

```text
debug
terrain
structure
object
system
unknown
```

### Status

```text
active
deprecated
disabled
deleted
```

### Erzeugung

```text
BlockType.create(...)
BlockType.create_for_registry(...)
BlockType.from_create_payload(...)
```

Debug-Factories:

```text
BlockType.create_debug_grass(...)
BlockType.create_debug_dirt(...)
BlockType.create_default_debug_blocks(...)
```

### Mutationen

```text
set_status()
deprecate()
disable()
restore()
soft_delete()

set_default_palette_index()
set_flags()
set_rendering()

replace_metadata()
update_metadata()
apply_patch_payload()
```

### Palettenausgabe

```text
sort_for_palette()
to_palette_entry()
default_cell_value
```

`default_palette_index` ist nur die empfohlene Registryreihenfolge. Er ist keine globale feste Zellwertzuweisung.

---

## 12.3 Air-Invariante

Air ist kein `BlockType`.

```text
cellValue = 0
→ Air

positive cellValue
→ Paletteintrag
```

Daraus folgt:

```text
keine BlockType-Zeile für system_air
keine positive Paletteposition für Air
SetBlock setzt einen positiven Block
RemoveBlock erzeugt Air
```

---

## 12.4 Systemblöcke

Die eigentlichen Code-Definitionen von Systemblöcken liegen nicht in `models/`.

Sie liegen unter:

```text
src/system_blocks/
```

Persistente Systemblöcke wie `system_railing` werden jedoch als `BlockType` in die vorhandene Registry gespiegelt.

Damit bleibt:

```text
Code-Definition
→ kanonische Systemblockwahrheit

BlockType-Mirror
→ Kompatibilität mit bestehender Registry-, Paletten- und Commandlogik
```

---

## 13. `chunk.py` – `ChunkSnapshot`

### 13.1 Aufgabe

`ChunkSnapshot` ist die aktuelle persistente Lade-Wahrheit eines materialisierten Chunks.

Eine Zeile existiert nur, wenn:

```text
ein Chunk verändert wurde
oder
ein Chunk explizit materialisiert wurde
```

Wenn keine Zeile existiert:

```text
Chunk-Service
→ Provider/Generator verwenden
```

### 13.2 Tabelle

```text
chunk_snapshots
```

### 13.3 Identität und Hierarchie

```text
id
snapshot_id

project_db_id
universe_db_id
world_db_id

chunk_x
chunk_y
chunk_z
chunk_key
```

### 13.4 Eindeutigkeit

```text
unique(world_db_id, chunk_x, chunk_y, chunk_z)
```

Damit existiert pro konkreter Welt und Chunkadresse ein materialisierter Snapshotdatensatz, der bei Änderungen aktualisiert wird.

### 13.5 Version und Status

```text
status
schema_version
runtime_content_version
chunk_revision
chunk_version
```

Öffentliche Version:

```text
chunk_revision = 1
→ chunk_version = chunk_rev_000001
```

Bei echter Änderung:

```text
bump_revision()
→ chunk_revision + 1
→ chunk_version neu formatieren
```

### 13.6 Inhalt

```text
content_encoding
content_json
content_binary
content_hash
content_size_bytes
```

Unterstützte Encodings:

```text
json
binary
json_gzip
rle_json
external_ref
```

Aktuell wird primär JSON verwendet. Binäre, komprimierte und externe Formate sind bereits im Schema vorbereitet.

### 13.7 Abgeleitete Runtimeinformationen

```text
palette_json
object_refs_json
object_ref_count
has_object_refs

stats_json
metadata_json

cell_count
non_air_cell_count
```

### 13.8 Zell- und Geometrievertrag

```text
chunk_size
cell_size
cell_index_order
cell_encoding_version
air_cell_value
block_cell_value_rule
```

Aktuell:

```text
chunk_size = 16
cell_count = 4096
cell_index_order = x-fastest-y-then-z
air_cell_value = 0
block_cell_value_rule = paletteIndex + 1
```

### 13.9 Registry-, Provider- und Generatorhistorie

Der Snapshot speichert zusätzlich den Kontext, in dem sein Inhalt interpretierbar ist:

```text
block_registry_id
block_registry_version

coordinate_system
projection_type
topology_type

template_id
provider_id
provider_world_id
generator_type
generator_version
```

### 13.10 Änderungsherkunft

```text
snapshot_source
materialized_reason

last_command_id
last_event_id
created_by_user_id
updated_by_user_id
last_session_id
```

Mögliche Quellen:

```text
command
import
migration
system
materialized_generated
```

Mögliche Materialisierungsgründe:

```text
set_block
remove_block
replace_block
batch_command
object_placement
object_removal
import
migration
manual
system
```

### 13.11 Erzeugung

```text
ChunkSnapshot.create(...)
ChunkSnapshot.create_for_world(...)
ChunkSnapshot.from_runtime_content(...)
```

### 13.12 Inhaltsänderung

```text
replace_content(...)
```

Diese Methode:

```text
normalisiert JSON/Binary-Inhalt
→ extrahiert oder übernimmt Palette
→ extrahiert oder übernimmt Objektverweise
→ berechnet Statistiken
→ berechnet SHA-256-Inhaltshash
→ berechnet Payloadgröße
→ aktualisiert Command-/Eventkontext
→ erhöht optional Revision
```

Sie soll innerhalb derselben äußeren Transaktion ausgeführt werden, in der auch `WorldCommandLog` und `ChunkEvent` geschrieben werden.

Weitere Methoden:

```text
update_command_context()
set_object_refs()
replace_metadata()
update_metadata()

build_runtime_content()
to_dict()
to_public_dict()
```

### 13.13 Wichtige Invarianten

```text
Snapshot ist Lade-Wahrheit.

Event-Replay ist nicht der normale Ladepfad.

Chunk-Key muss exakt zu chunk_x/y/z passen.

Mindestens content_json oder content_binary muss vorhanden sein.

air_cell_value muss 0 sein.

Ein bestehender aktiver Snapshot wird bei Änderungen aktualisiert.
```

---

## 14. `event.py` – `WorldCommandLog` und `ChunkEvent`

## 14.1 Trennung

```text
WorldCommandLog
→ ein Benutzer-/Systemintent
→ beschreibt den gesamten Command
→ kann keine, eine oder viele Änderungen erzeugen

ChunkEvent
→ historisches Ereignis für genau einen betroffenen Chunk
→ ein Command kann mehrere ChunkEvents erzeugen
```

---

## 14.2 `WorldCommandLog`

### Tabelle

```text
world_command_logs
```

### Identität und Hierarchie

```text
id
command_id

project_db_id
universe_db_id
world_db_id
```

`command_id` ist eindeutig.

### Commandklassifikation

```text
command_type
command_status
command_source
schema_version
```

Unterstützte Commandtypen im Modelvertrag:

```text
SetBlock
RemoveBlock
ReplaceBlock
ApplyBlockBatch
PlaceObject
RemoveObject
ReplaceObject
FillRegion
ClearRegion
ReplaceRegion
Import
System
```

Nicht jeder modellseitig erlaubte Typ ist bereits End-to-End bestätigt.

Status:

```text
received
applied
noop
rejected
failed
compensated
```

Quellen:

```text
editor
system
importer
ai
test
unknown
```

### Request- und Nutzerkontext

```text
user_id
session_id
request_id
trace_id
client_id
```

### Räumlicher und Objektkontext

```text
anchor_x
anchor_y
anchor_z

object_instance_id
object_type_id
object_variant_id

object_size_x
object_size_y
object_size_z
object_rotation_json
```

### Ergebniszusammenfassung

```text
affected_bounds_json
affected_chunks_json
affected_cells_json

changed
affected_chunk_count
affected_cell_count
event_count
```

### Payloads und Fehler

```text
request_payload_json
result_payload_json
validation_errors_json

error_code
error_message
metadata_json
```

### Zeitstempel

```text
created_at
applied_at
failed_at
```

### Erzeugung und Statusübergänge

```text
WorldCommandLog.create(...)
WorldCommandLog.create_for_world(...)
WorldCommandLog.from_command_payload(...)
```

Danach:

```text
mark_applied(changed=True|False)
→ applied oder noop

mark_rejected(...)
→ Validierungsablehnung

mark_failed(...)
→ Ausführungsfehler

increment_event_count(...)
→ Anzahl erzeugter ChunkEvents nachführen
```

---

## 14.3 `ChunkEvent`

### Tabelle

```text
chunk_events
```

### Aufgabe

Ein `ChunkEvent` ist die append-only historische Beschreibung einer bestätigten Änderung in einem Chunk.

### Identität und Beziehungen

```text
id
event_id
command_log_db_id
command_id

project_db_id
universe_db_id
world_db_id
chunk_snapshot_db_id
```

`event_id` ist eindeutig.

`chunk_snapshot_db_id` darf null werden, wenn der referenzierte Snapshot später entfernt wird:

```text
Foreign Key on delete = SET NULL
```

### Eventklassifikation

```text
event_type
event_status
event_schema_version
command_type
```

Eventtypen:

```text
block_change
object_change
region_change
import_change
system_change
```

Status:

```text
active
superseded
compensated
```

### Benutzer- und Chunkkontext

```text
user_id
session_id

chunk_x
chunk_y
chunk_z
chunk_key
```

### Welt- und Lokalposition

```text
position_x
position_y
position_z

local_x
local_y
local_z
```

### Blocktransition

```text
block_before_type_id
block_after_type_id

cell_before_value
cell_after_value

target_face
tool
```

### Snapshot-/Versionsübergang

```text
chunk_revision_before
chunk_revision_after

chunk_version_before
chunk_version_after

content_hash_before
content_hash_after
```

### Objekt- und Bereichskontext

```text
object_instance_id
object_type_id
object_variant_id
object_footprint_json

affected_bounds_json
affected_cells_json
affected_cell_count

dirty_chunks_json
dirty_chunk_count
```

### Payload und Metadaten

```text
payload_json
metadata_json
created_at
```

### Erzeugung

```text
ChunkEvent.create(...)
ChunkEvent.create_for_command(...)
```

### Historische Statusänderung

```text
mark_superseded()
mark_compensated()
```

Das Event wird nicht gelöscht oder überschrieben, um eine neue Änderung darzustellen. Seine historische Identität bleibt bestehen.

### Wichtige Invarianten

```text
ChunkEvent ist historische Wahrheit.

Events werden nicht zum normalen Chunkladen replayt.

Ein Command kann mehrere Events besitzen.

Eventposition, Chunk-Key und lokale Position müssen zueinander passen.

Events sollen nach Bestätigung append-only behandelt werden.
```

---

## 15. `object.py` – `WorldObjectInstance` und `WorldObjectChunkRef`

## 15.1 Zweck

Die Objektmodelle bereiten persistente Mehrblockobjekte vor.

Beispiele:

```text
4 × 4 × 2
2 × 1 × 2
1 × 1 × 3
beliebige spätere Library-Footprints
```

Die Objektmodelle ersetzen den ChunkSnapshot nicht.

```text
WorldObjectInstance
→ semantische/logische Objektidentität

WorldObjectChunkRef
→ räumliche Zuordnung zu Chunks

ChunkSnapshot
→ tatsächlicher sichtbarer Zellzustand
```

---

## 15.2 `WorldObjectInstance`

### Tabelle

```text
world_object_instances
```

### Identität und Hierarchie

```text
id
object_instance_id

project_db_id
universe_db_id
world_db_id
```

Eindeutigkeit:

```text
unique(world_db_id, object_instance_id)
```

### Status und Klassifikation

```text
status
schema_version
revision

object_source
object_kind
object_type_id
object_variant_id
```

Status:

```text
active
archived
deleted
detached
```

Quellen:

```text
editor
library
importer
system
ai
test
unknown
```

Arten:

```text
block_composite
library_object
imported_object
runtime_object
structure
unknown
```

### Beschreibung und Librarykontext

```text
label
description

library_id
library_version
library_snapshot_id
```

### Platzierung

```text
anchor_mode
anchor_x
anchor_y
anchor_z

size_x
size_y
size_z

rotation_json
transform_json
bounds_json
footprint_json
```

Anchor-Modi:

```text
world_cell
surface_relative
geo_anchored
free
```

### Belegte Zellen und Chunks

```text
occupied_cells_json
occupied_cell_count

touched_chunks_json
touched_chunk_count

primary_chunk_x
primary_chunk_y
primary_chunk_z
primary_chunk_key
```

### Command-/Eventhistorie

```text
created_by_command_id
updated_by_command_id
removed_by_command_id

created_event_id
updated_event_id
removed_event_id
```

### Audit

```text
content_hash
created_by_user_id
updated_by_user_id
last_session_id
metadata_json

created_at
updated_at
archived_at
deleted_at
```

### Erzeugung

```text
WorldObjectInstance.create(...)
WorldObjectInstance.create_for_world(...)
WorldObjectInstance.from_place_object_payload(...)
```

### Mutationen

```text
set_status()
archive()
restore()
soft_delete()

set_placement()
set_chunk_refs_summary()

replace_metadata()
update_metadata()
```

---

## 15.3 `WorldObjectChunkRef`

### Tabelle

```text
world_object_chunk_refs
```

### Aufgabe

Diese Tabelle bildet die Many-to-Many-ähnliche räumliche Zuordnung ab:

```text
ein Objekt
→ kann viele Chunks berühren

ein Chunk
→ kann Teile vieler Objekte enthalten
```

### Felder

```text
id
object_instance_db_id

project_db_id
universe_db_id
world_db_id

object_instance_id

status
schema_version
ref_role

chunk_x
chunk_y
chunk_z
chunk_key

local_bounds_json
world_bounds_json
occupied_cells_json
occupied_cell_count

object_content_hash
metadata_json

created_at
updated_at
deleted_at
```

### Eindeutigkeit

```text
unique(object_instance_db_id, chunk_x, chunk_y, chunk_z)
```

### Rollen

```text
primary
occupied
boundary
dirty_neighbor
metadata_only
```

### Status

```text
active
stale
deleted
```

### Erzeugung und Mutationen

```text
WorldObjectChunkRef.create(...)
WorldObjectChunkRef.create_for_object(...)

mark_stale()
restore()
soft_delete()
replace_occupied_cells()
```

### Aktueller Stand

Die Tabellen- und Modelstruktur ist vorhanden.

Noch nicht vollständig bestätigt sind insbesondere:

```text
PlaceObject End-to-End
RemoveObject End-to-End
mehrere Chunks pro reales Objekt
Objektänderung plus Snapshot/Event/CommandLog in einer vollständigen Transaktion
Objekte über Earth-Weltnaht
```

---

## 16. Relationship- und Cascade-Struktur

Die kanonische Access-Projektion ist bewusst nicht als ORM-Relationship an `Project.id` gebunden:

```text
ProjectAccessAssignment.chunk_project_id
→ entspricht fachlich Project.project_id
→ kein lokaler Foreign Key
→ Repository-/Service-Lookup über stabile öffentliche ID
```

Die Legacy-Access-Tabellen verwenden dagegen weiterhin `project_db_id` mit `ondelete=CASCADE`.

### 16.1 Projektgraph

```text
Project
└── universes
    └── Universe
        └── worlds
            └── WorldInstance
```

### 16.2 Weltbezogene Daten

```text
Project / Universe / WorldInstance
├── chunk_snapshots
├── world_command_logs
├── chunk_events
└── world_object_instances
```

### 16.3 Blockregistry

```text
BlockRegistry
└── block_types
```

### 16.4 Command und Event

```text
WorldCommandLog
└── chunk_events
```

### 16.5 Snapshot und Event

```text
ChunkSnapshot
└── chunk_events
```

Der Event-Foreign-Key auf einen Snapshot verwendet `SET NULL`, damit ein historisches Event nicht zusammen mit einem entfernten Snapshot verloren geht.

### 16.6 Objekt und Chunkreferenzen

```text
WorldObjectInstance
└── chunk_refs
    └── WorldObjectChunkRef
```

### 16.7 Ladeverhalten

Die Relationships verwenden überwiegend:

```text
Parentbezug
→ lazy="joined"

Collections/Backrefs
→ lazy="selectin"
```

Status- und Read-Pfade sollen trotzdem keine vollständigen tiefen Relationship-Graphen serialisieren.

### 16.8 Löschverhalten

Viele Parent-Foreign-Keys verwenden:

```text
ondelete="CASCADE"
```

Die Anwendungslogik nutzt jedoch grundsätzlich Soft-Delete für fachliche Objekte. Ein echter Datenbank-Cascade wird erst bei physischem Löschen relevant.

---

## 17. Wie die wichtigsten Daten entstehen

## 17.1 Default-Entwicklungsgraph

```text
Project.create_dev_project()
→ Project(dev-project)

Universe.create_for_project(...)
→ Universe(dev-universe)

WorldInstance.create_flat_spawn(...)
→ WorldInstance(world_spawn)
→ provider/template = flat
```

Ergänzend:

```text
BlockRegistry.create_debug_registry()
→ debug-blocks@1

BlockType.create_default_debug_blocks()
→ debug_grass
→ debug_dirt

Systemblock-Bootstrap
→ spiegelt system_railing als BlockType
→ persistiert system_air ausdrücklich nicht
```

Äußere Bootstraplogik:

```text
Objekte erzeugen
→ db.session.add(...)
→ flush für interne IDs
→ Beziehungen/Defaults setzen
→ gemeinsamer Commit
```

---

## 17.2 App-Provisioning

Der aktuelle Provisioning-Service erzeugt oder repariert den vollständigen projektgescopten Graphen in einer Transaktion:

```text
öffentliche App-Projekt-ID
→ Project.create_for_app_project()
→ Project persistieren/flushen

Project-Access-Service
→ kanonische ProjectAccessAssignment-Projektion synchronisieren
→ direkte Assignments verwenden ausschließlich auth_user_id
→ genau einen aktiven Direct Owner sicherstellen
→ stale direkte managed Assignments entfernen/deaktivieren
→ bestehende Gruppenassignments erhalten

Legacy-Kompatibilitätsinitialisierung
→ owner/admin/editor/viewer in project_roles sicherstellen
→ passende Legacy-Owner-Zuweisung erhalten oder ergänzen

Universe.create_for_project()
→ Universe persistieren/flushen

worldTemplate = flat
→ WorldInstance.create_flat_spawn()

worldTemplate = earth
→ kompakte Earth-Referenz kanonisieren
→ WorldInstance.create_earth_spawn(global_reference=...)

Project.default_universe_id setzen
Project.default_world_id setzen
Project.spawn_world_id setzen

Universe.default_world_id setzen
Universe.spawn_world_id setzen

gemeinsamer Commit
```

Lookups mit pessimistischem Locking deaktivieren implizite eager Joins und verwenden zielgerichtet `FOR UPDATE OF <Basistabelle>`. Dadurch wird der PostgreSQL-Fehler `FOR UPDATE cannot be applied to the nullable side of an outer join` vermieden.

Das Ergebnis ist:

```text
Chunk Project
├── ProjectAccessAssignment direct:<auth_user_id> → owner/admin/editor/viewer
├── optionale ProjectAccessAssignment group:<group_id> → admin/editor/viewer
├── Legacy ProjectRole owner/admin/editor/viewer
├── Legacy ProjectRoleAssignment user:<auth_user_id> → owner
├── optionale ProjectGroup-Strukturen
└── Universe
    └── konkrete WorldInstance world_spawn
        ├── provider/template = flat
        └── oder provider/template = earth + GlobalReferencePoint
```

Bestätigte Idempotenz:

```text
erster Request
→ created = true
→ vollständiger Graph committed

identischer Folge-Request
→ created = false
→ updated = false
→ code = chunk_project_exists
```

---

## 17.3 Chunk laden

```text
world_db_id + chunk_x/y/z
→ ChunkSnapshot suchen

Snapshot vorhanden
→ Snapshot ist Lade-Wahrheit
→ build_runtime_content()

Snapshot nicht vorhanden
→ Provider/Generator verwenden
→ kein automatischer Snapshot nur durch Lesen
```

---

## 17.4 Block setzen oder entfernen

```text
Command empfangen
→ WorldCommandLog.from_command_payload()

ChunkSnapshot laden oder aus Generatorzustand materialisieren
→ Zelle ändern
→ ChunkSnapshot.replace_content()
→ Revision erhöhen
→ Hash und Statistiken aktualisieren

ChunkEvent.create_for_command()
→ Block-/Zelltransition speichern
→ Dirty-Chunks speichern
→ Vorher-/Nachher-Version speichern

WorldCommandLog.mark_applied()
→ affectedChunks/Cells/EventCount setzen

gemeinsamer Commit
```

---

## 17.5 Earth-Welt erzeugen

```text
GlobalReferencePoint mit explizitem CRS
→ WorldInstance.create_earth_spawn()

Earth-Manifest laden
→ EarthWorldProvider erzeugen
→ Referenz validieren
→ lokalen Storage-/Spawnkontext ableiten

WorldInstance erzeugen
→ provider/template = earth
→ global_reference_json speichern
→ Fingerprint speichern
→ coordinate_frame_revision setzen
→ präzisen lokalen Spawn speichern

äußere Schicht persistiert und committed
```

Der reale Provisioning-Test bestätigte zusätzlich:

```text
Earth-Projektgraph committed
→ Project, vier Standardrollen, Owner-Zuweisung, Universe und WorldInstance erzeugt
→ global_reference_fingerprint in Response, Project-Metadaten, World-Metadaten und World-Spalten identisch
→ min_y = -1024
→ max_y = 8192
→ präziser Spawn und Integer-Spawn konsistent
→ Access-Antwort enthält projectDbId und öffentliche projectId
→ identischer zweiter Request ist unverändert idempotent
```

Nach erster Materialisierung muss die äußere Service-/Commandlogik:

```text
WorldInstance.lock_global_reference(...)
```

aufrufen, damit periodische Chunkadressen und persistierte lokale Koordinaten nicht durch normales Reanchoring ungültig werden.

---

## 17.6 Mehrblockobjekt erzeugen

Vorgesehener Ablauf:

```text
PlaceObject-Command
→ WorldCommandLog erzeugen

WorldObjectInstance erzeugen
→ logische Objektidentität und Bounds

für jeden berührten Chunk
→ WorldObjectChunkRef erzeugen

betroffene ChunkSnapshots aktualisieren
→ reale Zellen/ObjectRefs schreiben

pro betroffenem Chunk
→ ChunkEvent erzeugen

CommandLog auf applied setzen
→ gemeinsamer Commit
```

Dieser Ablauf ist modellseitig vorbereitet, aber noch nicht vollständig End-to-End bestätigt.

---

## 18. Harte Invarianten des Models-Ordners

```text
1. Interne DB-IDs und öffentliche API-IDs sind getrennt.

2. Project.project_id ist die öffentliche Chunk-Projekt-ID.

3. Project.external_app_project_id ist keine Datenbank-FK zu vectoplan-app.

4. Serviceübergreifende Benutzeridentität ist im kanonischen Vertrag ausschließlich auth_user_id.

5. Lokale numerische AppUser-IDs und E-Mail-Adressen sind keine zulässigen kanonischen Benutzeridentitäten.

6. Project.owner_auth_user_id ist die kanonische Owneridentität.

7. ProjectAccessAssignment direct besitzt auth_user_id und keine group_id.

8. ProjectAccessAssignment group besitzt group_id und keine auth_user_id.

9. Owner-Assignments sind immer direct.

10. Genau ein aktiver Owner wird transaktional im Service erzwungen, nicht durch eine partielle Owner-Unique-Constraint.

11. Viewer ist im Servicevertrag strikt read-only; das Model speichert nur die Rolle.

12. vectoplan-app bleibt Source of Truth für App-Projektmitgliedschaften und Rollen.

13. ProjectAccessAssignment speichert nur die synchronisierte Access-Projektion.

14. Gruppenassignments bleiben bei Direct-User-Reconciliation erhalten.

15. ProjectAccessAssignment.chunk_project_id verwendet die stabile öffentliche Projekt-ID und keinen lokalen FK.

16. Die Legacy-Access-Tabellen bleiben über project_db_id an projects.id gebunden.

17. Legacy-Userfelder dürfen keine konkurrierende serviceübergreifende Identitätswahrheit bilden.

18. Standardrollen sind owner, admin, editor und viewer.

19. Normale Provisionierung darf einen bestehenden Owner nicht still ändern.

20. Normale Provisionierung darf requested/effective World-Templates nicht still wechseln.

21. Earth→Flat muss als expliziter Fallbackzustand mit Fehlercode gespeichert werden.

22. Universe.universe_id ist nur innerhalb eines Projekts eindeutig.

23. WorldInstance.world_id ist nur innerhalb eines Universums eindeutig.

24. flat und earth sind Provider-/Template-IDs, keine konkrete world_id.

25. world_spawn oder chk_wld_... ist eine konkrete editierbare WorldInstance.

26. WorldInstance speichert Konfiguration, nicht Chunkzellen.

27. Unveränderte Chunks werden generiert.

28. Bearbeitete Chunks werden als ChunkSnapshot gespeichert.

29. ChunkSnapshot ist die aktuelle Lade-Wahrheit.

30. ChunkEvent ist historische Wahrheit.

31. Events sind nicht der normale Ladepfad.

32. Pro Welt und Chunkkoordinate existiert maximal ein Snapshotdatensatz.

33. chunk_key muss zu chunk_x/chunk_y/chunk_z passen.

34. cellValue 0 bedeutet Air.

35. Air ist kein BlockType.

36. Positive Zellwerte folgen paletteIndex + 1.

37. BlockRegistry und Registryversion bleiben für historische Interpretation erhalten.

38. WorldCommandLog beschreibt einen Command als Ganzes.

39. Ein Command kann mehrere ChunkEvents erzeugen.

40. ChunkEvents werden append-only behandelt.

41. Mehrblockobjekte ersetzen ChunkSnapshots nicht.

42. WorldObjectChunkRef bildet Objekt-zu-Chunk-Zuordnung ab.

43. Earth-Welten benötigen genau einen globalen Referenzvertrag.

44. Flat-Welten besitzen keinen globalen Referenzvertrag.

45. Earth-Spawn wird lokal und präzise gespeichert.

46. Spawnverschiebung ist kein Reanchoring.

47. Die globale Earth-Referenz muss nach Materialisierung gesperrt werden.

48. Modelmethoden führen keine Queries, Remote Calls, Commits oder Rollbacks aus, sofern nicht ausdrücklich anders dokumentiert; die vorliegenden Kernmodels tun dies nicht.

49. Projektgraphen und Access-Synchronisierungen werden außerhalb der Models atomar orchestriert.

50. Öffentliche Serialisierung darf keine rohen Auth-/Gruppen-IDs, Secrets oder internen Datenbank-IDs offenlegen.

51. Status- und Serializerpfade dürfen keine tiefen ORM-Graphen unkontrolliert laden.

52. Schemaänderungen gehören in einen expliziten Bootstrap-/Migrationspfad, nicht in den normalen Runtime-Startup.

53. Earth-spezifische Konfigurationswerte dürfen nicht aus generischen Flat-Defaults überschrieben werden.

54. Integer- und Präzisionsspawn einer Earth-Welt müssen dieselbe lokale Position beschreiben.
```

---

## 19. Aktuell bestätigter Nutzungsstand

Bestätigt beziehungsweise im übergeordneten Service-IST als genutzt dokumentiert:

```text
Project
ProjectAccessAssignment
ProjectRole
ProjectGroup
ProjectRoleAssignment
Universe
WorldInstance mit flat-Provider
WorldInstance mit earth-Provider
BlockRegistry
BlockType
ChunkSnapshot
WorldCommandLog
ChunkEvent
```

Bestätigte reale Abläufe:

```text
Default-Projektgraph vorhanden
App-Projekt-Provisioning vorhanden
Project-Access-Initialisierung mit vier Standardrollen vorhanden
kanonische Owner-Projektion mit auth_user_id vorhanden
Legacy-Owner-Zuweisung als Kompatibilitätsprojektion vorhanden
Gruppen-Create-/Read-Pfad vorhanden
Earth-Projekt-Provisioning mit kanonischer EPSG:4979-Referenz vorhanden
Earth-Provisioning-Reparatur und Idempotenz bestätigt
Generator-Chunk laden
Snapshot-Chunk laden
SetBlock
RemoveBlock
Snapshot aktualisieren
CommandLog schreiben
ChunkEvent schreiben
Reload zeigt Änderung
Systemblock system_railing als BlockType-Mirror
Earth-Kern und WorldInstance-Earth-Vertrag über Debugpfad ausführbar
projektgebundene Earth-WorldInstance persistent erzeugt
Earth-Vertikalgrenzen und referenzbasierter Spawn persistent bestätigt
```

Strukturell vorhanden, aber noch nicht vollständig bestätigt:

```text
WorldObjectInstance
WorldObjectChunkRef
ProjectGroupMember-Lifecycle einschließlich Remove/Reaktivierung/Ablauf
vollständige Gruppenauflösung aus Legacy- und kanonischer Projektion in allen HTTP-Guards

ReplaceBlock End-to-End
ApplyBlockBatch End-to-End
PlaceObject End-to-End
RemoveObject End-to-End
Region-Commands
projektgebundene Earth-Chunk-, Snapshot- und Commandmutation über die vollständige World-State-API
Earth-Commands und Snapshots über die periodische X-Naht
```

---

## 20. Bekannte technische Restpunkte

### 20.1 Modeldateien sind sehr groß

Insbesondere:

```text
world.py                     → 4.685 Zeilen
project_access.py            → 3.597 Zeilen
project.py                   → 2.934 Zeilen
object.py                    → 2.577 Zeilen
event.py                     → 2.571 Zeilen
block.py                     → 2.517 Zeilen
chunk.py                     → 2.413 Zeilen
project_access_assignment.py → 1.554 Zeilen
```

Aktuell bündeln die Dateien jeweils:

```text
SQLAlchemy-Schema
Konstanten
Normalisierung
Factorymethoden
Mutationslogik
Validierung
Serialisierung
API-Payload-Kompatibilität
```

Das ist funktional, erhöht aber die Einstiegshürde und die Gefahr, dass fachfremde Änderungen dieselbe Datei betreffen.

Eine spätere Trennung könnte lauten:

```text
models/
→ reine SQLAlchemy-Tabellen und kleine Invarianten

src/.../contracts.py
→ Payload-/Serialisierungsverträge

src/.../validation.py
→ Normalisierung und Validierung

src/.../factories.py
→ Erzeugungslogik

src/.../services.py
→ fachliche Orchestrierung
```

Eine solche Aufteilung ist noch nicht umgesetzt und darf nicht ohne Tests erfolgen.

### 20.2 Hilfsfunktionen sind mehrfach vorhanden

Mehrere Dateien enthalten eigene Varianten von:

```text
utc_now
datetime_to_iso
make_json_safe
normalize_optional_text
normalize_required_text
normalize_public_id
normalize_json_object
normalize_json_list
build_chunk_key
normalize_chunk_key
```

Das hält Module unabhängig, kann aber zu Drift führen.

Besonders kritisch:

```text
Chunk-Key-Regeln
ID-Zeichensätze
JSON-Normalisierung
Zeitstempelserialisierung
```

Eine spätere gemeinsame Utility-Schicht wäre möglich, muss aber zyklische Imports vermeiden.

### 20.3 `models/__init__.py` prüft Spalten unterschiedlich tief

Für `Project`, `Universe` und `WorldInstance` existieren umfangreiche erwartete Spaltenlisten.

Für `ProjectAccessAssignment` und die vier Legacy-Access-Klassen ist die Prüfung inzwischen vollständig auf die kritischen Spalten erweitert. Für mehrere ältere Block-, Snapshot-, Event- und Objektklassen ist die Mindestprüfung dagegen weiterhin deutlich flacher und erwartet teilweise nur:

```text
id
```

Dadurch kann:

```text
models ready = true
```

sein, obwohl eine fachlich benötigte neue Spalte eines Snapshot-, Event- oder Objektmodels in einer älteren Datenbank noch fehlt.

Sinnvolle Härtung:

```text
EXPECTED_MODEL_COLUMNS
→ für die sieben derzeit nur flach geprüften Block-/Snapshot-/Event-/Object-Klassen auf die tatsächlich kritischen Spalten erweitern
```

### 20.4 Kanonische und Legacy-Access-Struktur existieren parallel

Aktuell registriert das Modelpaket gleichzeitig:

```text
project_access_assignments
→ kanonische synchronisierte Projektion

project_roles / project_groups / project_group_members / project_role_assignments
→ Legacy-/Kompatibilitätsstruktur
```

Das ist für Bootstrap- und Übergangskompatibilität beabsichtigt, erhöht aber die Konsolidierungsanforderung:

```text
eine Source of Truth für direkte Userzuweisungen
klare Reconciliation-Richtung
keine divergierenden Owner
keine divergierenden Viewer-/Editor-Rollen
saubere Migrations- und Backfillstrategie
```

Langfristig muss entschieden werden, welche Legacy-Tabellen dauerhaft fachliche Gruppen-/Rollenmodelle bleiben und welche nur noch als Kompatibilitätsprojektion geführt werden.

### 20.5 Produktionsmigrationen

Die Models definieren das Zielschema, ersetzen aber kein Migrationssystem.

Noch erforderlich beziehungsweise weiter zu härten:

```text
Alembic-Migrationen
reproduzierbare Upgrade-/Downgradepfade
produktiver Schema-Upgrade-Prozess
Trennung zwischen Dev-Repair und Production-Migration
```

### 20.6 Nebenläufigkeit

Die revision-Felder sind vorbereitet, aber eine vollständige Optimistic-Concurrency-Strategie ist noch nicht dokumentiert oder End-to-End bestätigt.

Risiko:

```text
zwei gleichzeitige Commands
→ laden dieselbe Snapshotrevision
→ schreiben konkurrierend
```

Erforderliche spätere Entscheidung:

```text
SELECT FOR UPDATE
oder
optimistic compare-and-swap auf chunk_revision
oder
serialisierter Commandpfad pro Chunk
```

### 20.7 Earth-Kanonisierung liegt nicht allein im Model

`WorldInstance` speichert den Earth-Vertrag.

Die vollständige Sicherheit erfordert zusätzlich in Read-/Write-Pfaden:

```text
X vor Chunk-Key kanonisieren
X vor Snapshot-Lookup kanonisieren
X vor Snapshot-Write kanonisieren
periodische Aliase deduplizieren
Dirty-Chunks über die Weltnaht berechnen
```

Diese Verantwortung gehört in Koordinaten-, Provider-, Service- und Commandlogik, nicht ausschließlich in `world.py`.

### 20.8 Objektpersistenz

Die Objektmodelle sind umfangreich, aber noch nicht durch den vollständigen produktiven Ablauf bestätigt.

Vor einer Einstufung als produktiv fehlen mindestens:

```text
PlaceObject-Test
RemoveObject-Test
mehrere ChunkRefs
Snapshot/ObjectRef-Konsistenz
Command-/Eventkonsistenz
Rollbacktest
Konflikttest
Grenz-/Earth-Nahttest
```

---

## 21. Wo eine Änderung hingehört

| Änderung | Zuständige Datei/Schicht |
|---|---|
| neues persistentes Top-Level-Projektfeld | `models/project.py` |
| Universe-Rolle oder Universe-Referenz | `models/universe.py` |
| kanonische direkte User-/Gruppenprojektion | `models/project_access_assignment.py` |
| Legacy-Rolle, Gruppe, Mitgliedschaft oder Rollenzuweisung | `models/project_access.py` |
| Standardrollen-/Owner-Synchronisation und effektive Access-Logik | `src/services/project_access_service.py` |
| Weltkonfiguration, Provider, Spawn, Earth-Referenz | `models/world.py` |
| Registry- oder Blockdefinition | `models/block.py` |
| persistierter Chunkinhalt oder Snapshotmetadaten | `models/chunk.py` |
| Commandstatus, Commandpayload, historische Events | `models/event.py` |
| Mehrblockobjekt oder Objekt-Chunk-Zuordnung | `models/object.py` |
| neue Modelklasse zentral registrieren | `models/__init__.py` |
| Tabellen tatsächlich erstellen/ändern | Bootstrap/Migration außerhalb `models/` |
| Projektgraph und Access-Projektion atomar erzeugen/synchronisieren | `src/services/project_provisioning_service.py`, `src/services/project_access_service.py` oder Bootstrap |
| HTTP-Payload lesen und Response senden | Route/Serializer außerhalb oder Model-Payloadfactory |
| Chunkkoordinaten kanonisieren | `src/coordinates` beziehungsweise Provider-/Servicelogik |
| Systemblock-Codewahrheit | `src/system_blocks` |
| BlockType-Mirror eines Systemblocks | Systemblock-Bootstrap + `models/block.py` |

---

## 22. Checkliste beim Hinzufügen oder Ändern eines Models

```text
1. SQLAlchemy-Klasse und __tablename__ definieren.

2. Interne DB-ID und öffentliche ID klar trennen.

3. Foreign Keys und ondelete-Verhalten festlegen.

4. UniqueConstraints definieren.

5. CheckConstraints für harte Datenbankinvarianten ergänzen.

6. Suchrelevante Indizes definieren.

7. schema_version setzen oder erhöhen.

8. create(...) ohne Sessionzugriff implementieren.

9. Parent-Factory oder Payload-Factory nur bei echtem Bedarf ergänzen.

10. get_validation_errors() ergänzen.

11. to_dict() und gegebenenfalls to_public_dict() ergänzen.

12. Keine Commits oder Rollbacks im Model ausführen.

13. Model in models/__init__.py registrieren.

14. MODEL_CLASS_TO_MODULE und MODEL_CLASS_TO_TABLE erweitern.

15. EXPECTED_MODEL_CLASSES erweitern.

16. EXPECTED_MODEL_COLUMNS mit kritischen Spalten erweitern.

17. Bootstrap-/Migrationpfad ergänzen.

18. Readiness und Statusrouten prüfen.

19. Unit-Test für Factory, Validierung und Serialisierung ergänzen.

20. Integrationstest für Persistenz und Constraints ergänzen.

21. Bei mehreren betroffenen Models Transaktions-/Rollbacktest ergänzen.

22. Bei kanonischen Direct-Assignments ausschließlich auth_user_id akzeptieren; lokale IDs, E-Mail und verschachtelte user.id-Felder ablehnen.

23. Bei ProjectAccessAssignment Subject-XOR, Owner-nur-direct, Public-Redaction und Gruppen-Erhalt prüfen.

24. Bei Legacy-Access-Models project_db_id-Scope und klare Abgrenzung zur kanonischen Projektion prüfen.

25. Bei Rollen-/Gruppenbeziehungen versehentliche eager Serialisierung vermeiden.

26. Bei Earth-Änderungen kanonischen GlobalReferencePoint, Fingerprint, Vertikalgrenzen und Spawnkonsistenz prüfen.

27. Diese IST-Zustand.md aktualisieren.
```

---

## 23. Empfohlene Navigationsreihenfolge für Entwickler

Für einen schnellen Einstieg:

```text
1. models/IST-Zustand.md
   → Gesamtverständnis

2. models/__init__.py
   → Registrierung und getrennte Access-Diagnostik

3. models/project.py
   → Projekt, kanonischer Owner, Provisionierungs- und Access-Sync-Zustand

4. models/project_access_assignment.py
   → kanonische Direct-/Group-Projektion

5. models/project_access.py
   → Legacy-Rollen, Gruppen, Memberships und Zuweisungen

6. models/universe.py
7. models/world.py
   → Projekt-/Weltgraph

8. models/block.py
   → Blockdefinitionen und Registry

9. models/chunk.py
   → aktuelle Chunkzustände

10. models/event.py
    → Commands und Historie

11. models/object.py
    → vorbereitete Mehrblockobjekte
```

Für einen Blockänderungspfad:

```text
world.py
→ chunk.py
→ event.py
→ block.py
```

Für App-Provisioning:

```text
project.py
→ project_access_assignment.py
→ src/services/project_access_service.py
→ project_access.py (Legacy-Kompatibilität)
→ universe.py
→ world.py
→ src/services/project_provisioning_service.py
```

Für Earth:

```text
world.py
→ src/georeferencing/
→ src/coordinates/
→ src/world/earth/
```

Für Mehrblockobjekte:

```text
object.py
→ chunk.py
→ event.py
```

---

## 24. Gesamtbefund

Der Ordner `models/` bildet inzwischen eine umfangreiche und funktional belastbare Persistenzbasis.

Bestätigt tragfähig sind:

```text
Projektgraph
kanonische ProjectAccessAssignment-Projektion mit owner/admin/editor/viewer
Legacy-Project-Access-Persistenz mit Rollen, Gruppen und Owner-Zuweisung
Flat-World-Konfiguration
persistent provisionierte Earth-World mit kanonischem GlobalReferencePoint
BlockRegistry und BlockType
ChunkSnapshot als Lade-Wahrheit
WorldCommandLog als Commandzusammenfassung
ChunkEvent als historische Wahrheit
App-Projektverknüpfung
Systemblock-Mirror
Earth-Referenzvertrag im World-Modell
Earth-Vertikalgrenzen, referenzbasierter Spawn und Provisioning-Idempotenz
```

Vorbereitet, aber weiter zu integrieren, sind:

```text
vollständige Mehrblockobjektpfade
optimistische Nebenläufigkeit
produktionsreife Migrationen
vollständige HTTP-Guard-Abdeckung und Owner-Transfer-End-to-End auf Basis der kanonischen Access-Projektion
produktive Earth-Snapshot-/Commandpfade einschließlich periodischer Weltnaht
```

Die wichtigste dauerhafte Architekturregel lautet:

```text
models/
→ definiert persistente Datenverträge,
  lokale Invarianten,
  Factories,
  Mutationen,
  Validierung und Serialisierung

Service/Repository/Bootstrap/Routes
→ besitzen Lookups,
  Orchestrierung,
  Transaktionen,
  Upserts,
  Commit/Rollback und HTTP-Verhalten
```

Damit ist der Ordner fachlich nachvollziehbar, ohne für die normale Orientierung jede einzelne `.py`-Datei vollständig lesen zu müssen.

---

## 25. Historischer Aktualisierungs- und Verifikationsnachweis vom 2026-07-17

Dieser Abschnitt dokumentiert den Stand vor Einführung der kanonischen `ProjectAccessAssignment`-Projektion und von `project.schema.v3`. Angaben wie „acht Module“, „vierzehn Modelklassen“, `subjectId = "1"` oder ein ausschließlich Legacy-basierter Access-Vertrag sind daher als historischer Entwicklungsstand zu lesen und werden durch Abschnitt 26 ersetzt.

Die damalige Fassung ergänzte die zuvor dokumentierte Modelschicht, ohne ältere fachliche Beschreibungen zu entfernen.

Neu dokumentiert beziehungsweise hochgestuft wurden:

```text
models/project_access.py
→ vier persistente Project-Access-Models
→ Standardrollen und Permission-Vertrag
→ externe User-ID-Strings ohne Cross-Service-FK
→ authzEnforced bleibt false

models/__init__.py
→ acht Modelmodule
→ vierzehn persistente Modelklassen
→ vollständige Project-Access-Spaltenprüfung
→ DB-freier Project-Access-Vertrag

models/project.py
→ erweiterter App-/Owner-Vertrag
→ Parent-Scope der Access-Zeilen

models/world.py
→ produktiv provisionierte Earth-World bestätigt
→ kanonischer GlobalReferencePoint bestätigt
→ EPSG:4979/WKT2:2019 bestätigt
→ Earth-Grid und Fingerprint bestätigt
→ Earth-Vertikalgrenzen und Spawnkonsistenz bestätigt
```

Bestätigte reale Integrationswerte des Testprojekts:

```text
project.id            = 6
project.project_id    = chk_prj_earth_20260717173207
universe.id           = 4
world.id              = 2
world.world_id        = world_spawn
world.template_id     = earth
world.provider_id     = earth
world.provider_world_id = earth
world.min_y           = -1024
world.max_y           = 8192
world.spawn_x/y/z     = 4 / 13 / 3
world.spawn precise   = 4.444... / 13.0 / 3.555...
coordinate frame revision = 1
```

Der gespeicherte Earth-Referenzfingerprint war in folgenden Stellen identisch:

```text
Provisioning-Response
Project.metadata_json
WorldInstance.metadata_json
WorldInstance.global_reference_json.fingerprint
WorldInstance.global_reference_fingerprint
```

Bestätigte Transaktions- und Idempotenzfolge:

```text
erster Provisioning-Request
→ vollständiger Projektgraph erzeugt
→ Transaktion committed

Reparaturrequest nach Vertragskorrektur
→ bestehende Earth-Werte synchronisiert
→ created = false
→ updated = true

unmittelbar identischer Folge-Request
→ code = chunk_project_exists
→ created = false
→ updated = false
```

Die noch offene nächste Integrationshärtung außerhalb des Models-Ordners betrifft insbesondere:

```text
routes/world_test.py
→ providerabhängige Chunkkoordinatengrenzen
→ Periodic-X-Test über vollständige Weltbreite
→ Queryvalidierungsfehler als HTTP 400 statt HTTP 500
```

---

## 26. Aktualisierung 2026-07-19 – kanonische Access-Projektion und `Project`-Schema v3

### 26.1 Geprüfte Dateien

Diese Fortschreibung wurde gegen folgende vorliegende Quelldateien abgeglichen:

```text
models/__init__.py
models/project.py
models/project_access_assignment.py
models/project_access.py
models/universe.py
models/world.py
models/block.py
models/chunk.py
models/event.py
models/object.py
```

Alle Python-Dateien wurden statisch geparst; die dokumentierten Klassen-, Spalten-, Methoden- und Schemaangaben stammen aus dem vorliegenden Code. Ein statischer Abgleich bestätigt Implementierung, ersetzt aber keinen vollständigen Datenbank-/HTTP-End-to-End-Test.

### 26.2 Neuer Modelgraph

```text
9 Modelmodule
15 persistente Modelklassen
15 Tabellen
```

Neue Importreihenfolge:

```text
project
→ project_access_assignment
→ project_access
→ universe
→ world
→ block
→ chunk
→ event
→ object
```

Neu registriert:

```text
ProjectAccessAssignment
→ Tabelle project_access_assignments
→ Schema project-access-assignment.schema.v1
```

### 26.3 Neue Access-Aufteilung

```text
Kanonisch
→ ProjectAccessAssignment
→ auth_user_id
→ chunk_project_id
→ direct/group
→ owner/admin/editor/viewer
→ Public-Redaction
→ Source of Truth = vectoplan-app

Legacy/Kompatibilität
→ ProjectRole
→ ProjectGroup
→ ProjectGroupMember
→ ProjectRoleAssignment
→ project_db_id-Foreign-Keys
→ eigener Vertrag authzEnforced=false
```

`models/__init__.py` liefert getrennte Verträge und Readiness-Funktionen für beide Ebenen. Der kombinierte Modelvertrag ist nur bereit, wenn kanonische Projektion und Legacy-Struktur vollständig verfügbar sind.

### 26.4 `Project`-Schema v3

Aktuell bestätigt implementiert:

```text
PROJECT_SCHEMA_VERSION = project.schema.v3
DEV_PROJECT_OWNER_AUTH_USER_ID = auth_dev_owner
```

Neue beziehungsweise gehärtete Feldgruppen:

```text
owner_auth_user_id
created_by_auth_user_id
updated_by_auth_user_id

world_template_requested
world_template_effective
world_fallback_used
world_fallback_code
earth_reference_fingerprint
world_metadata_json

provisioning_*
access_sync_*
```

Der Project-Vertrag lehnt lokale numerische IDs, E-Mail-Adressen, anonyme Identitäten und widersprüchliche Owner-Aliase ab. Der generische Patch schützt interne Owner-, App-Link-, Template-, Provisionierungs- und Access-Sync-Felder.

### 26.5 Identitäts- und Ownervertrag

```text
kanonische serviceübergreifende Benutzeridentität
→ auth_user_id

Dev-Owner
→ auth_dev_owner

Owner-Assignment
→ direct
→ genau ein aktiver Owner im Service
→ Gruppen können kein Owner sein

Owner-Transfer
→ dedizierte Serviceoperation
→ kein generischer Projekt-/Assignment-Patch
```

Die fehlende partielle Owner-Unique-Constraint ist bewusst: Der Service erzwingt die Invariante transaktional, damit Promotion und Demotion innerhalb eines atomaren Transfers nicht durch Autoflush in falscher Reihenfolge scheitern.

### 26.6 Projektions- und Fallbackzustand

`Project` speichert getrennt:

```text
angefordertes Template
effektives Template
Fallback verwendet
Fallbackcode
Earth-Referenzfingerprint
Provisionierungsstatus
Access-Sync-Status
Request-/Correlation-IDs
Retry-/Repair-Zustand
Versuchszähler
Erfolgszeitpunkte
```

Normale Retries dürfen weder Owner noch bestehende Templates still wechseln. Ein unterschiedliches requested/effective Template ist nur als expliziter Earth→Flat-Fallbackzustand zulässig.

### 26.7 Public-/Private-Serialisierung

```text
Project.to_public_dict()
→ keine rohe Owner-/Actor-auth_user_id
→ keine interne URL
→ Fingerprints und nicht-sensitive Zustände

ProjectAccessAssignment.to_public_dict()
→ keine rohe auth_user_id/group_id
→ Subject-Fingerprint

Private/Service-Ausgabe
→ nur explizit
→ sanitisierte Metadaten
```

Metadaten-Sanitizer begrenzen Größe und Tiefe und entfernen Credentials, Identitätsfelder, interne URLs sowie Chunk-/Block-/World-/Snapshot-Rohdaten.

### 26.8 Unveränderte Kernaussagen

Folgende bereits dokumentierte Modelverträge bleiben unverändert gültig:

```text
Universe = universe.schema.v2
WorldInstance = world-instance.schema.v3
BlockRegistry = block-registry.schema.v1
BlockType = block-type.schema.v1
ChunkSnapshot = chunk-snapshot.schema.v1
WorldCommandLog = world-command-log.schema.v1
ChunkEvent = chunk-event.schema.v1
WorldObjectInstance = world-object-instance.schema.v1
WorldObjectChunkRef = world-object-chunk-ref.schema.v1
```

Weiterhin gilt:

```text
Snapshot = Lade-Wahrheit
Event = historische Wahrheit
Air = cellValue 0
Block = paletteIndex + 1
flat/earth = Provider-/Template-IDs
konkrete Welt = world_spawn oder chk_wld_...
Models committen nicht
```

### 26.9 Noch separat Ende-zu-Ende zu bestätigen

Aus den Modeldateien allein nicht vollständig nachgewiesen:

```text
Service-Auth auf allen Access-Mutationsrouten
Access-Decision-Guard auf allen Projekt-/World-/Block-/Chunk-/Command-Routen
Viewer-read-only über reale HTTP-Sitzung
dedizierter Owner-Transfer über HTTP
Reconciliation realer App-Mitgliedschaftsänderungen
Migration bestehender produktiver Daten auf project.schema.v3
Backfill project_access_assignments
Konsolidierung der Legacy-Access-Tabellen
Earth-Chunk-/Snapshot-/Commandpfad über die periodische X-Naht
```

### 26.10 Aktualisierter Gesamtbefund

```text
Die Modelschicht besitzt jetzt einen klaren kanonischen Access-Vertrag:

vectoplan-app Membership/Rollen
→ ProjectAccessAssignment-Projektion
→ project_access_service Enforcement

Parallel bleibt die Legacy-Rollen-/Gruppenstruktur für Kompatibilität,
Bootstrap und Übergangspfad registriert.

Project trägt mit schema.v3 den kanonischen Owner-, Template-,
Provisionierungs- und Access-Sync-Zustand.

Die World-/Chunk-/Event-/Object-Verträge bleiben fachlich kompatibel.
```

Damit ist die Model-IST-Dokumentation auf den vorliegenden Code-Stand vom 2026-07-19 aktualisiert.

