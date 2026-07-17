<!-- services/vectoplan-chunk/src/georeferencing/IST-Zustand.md -->

# IST-Zustand – `services/vectoplan-chunk/src/georeferencing`

## Status dieser Fassung

Stand: 2026-07-14  
Status: Vollständige Bestandsaufnahme der aktuell vorliegenden Georeferenzierungs- und CRS-Schicht.

Diese Datei beschreibt:

```text
services/vectoplan-chunk/src/georeferencing/
```

Ziel ist, dass Entwickler ohne vollständiges Lesen aller Python-Dateien erkennen können:

```text
welche Verträge existieren
wie CRS-Eingaben aufgelöst werden
wie pyproj und PROJ kontrolliert initialisiert werden
wie Transformer ausgewählt und gecacht werden
wie globale Earth-Koordinaten auf das lokale Raster abgebildet werden
welche Daten persistiert werden
welche Daten ausschließlich abgeleitet oder gecacht sind
welche Fehlercodes an äußeren Schichtgrenzen verfügbar sind
welche Genauigkeits-, Netzwerk- und Reproduzierbarkeitsregeln gelten
welche Integrationsgrenzen zu coordinates, world, models und routes bestehen
```

Die Dokumentation unterscheidet:

```text
implementiert
→ im vorliegenden Quellcode vorhanden

statisch geprüft
→ Python-Syntax und Struktur erfolgreich geprüft

isoliert ausgeführt
→ mit der installierten pyproj-/PROJ-Runtime und einem minimalen
  Coordinates-Vertragsstub ausgeführt

bestätigt
→ im übergeordneten Service-IST oder über eine reale Debugroute bestätigt

vorbereitet
→ Vertrag und Code vorhanden, aber noch nicht vollständig produktiv
  mit Datenbank, WorldInstance und HTTP-Pfaden verbunden
```

---

## 1. Kurzfassung

`src/georeferencing/` ist die frameworkunabhängige Georeferenzierungsbasis für Earth v1.

Das Package enthält:

```text
eine stabile Lazy-Import-Fassade
eine Domänenfehlerhierarchie
immutable Persistenz- und Transformationsverträge
eine kontrollierte pyproj-/PROJ-Integrationsschicht
eine strikte Transformer-Auswahl
ein versioniertes periodisches Earth-Raster
globale/lokale Earth-Konvertierungen
Readiness-, Cache- und Resetfunktionen
```

Es enthält nicht:

```text
keine Flask-Routen
keine SQLAlchemy-Models
keine Repositories
keine Datenbanktransaktionen
keine Snapshotpersistenz
keine Eventpersistenz
keine Commandausführung
keine WorldInstance-Auflösung
keine Benutzerautorisierung
```

Der zentrale Datenfluss lautet:

```text
explizite CRS-Eingabe
→ resolve_crs()
→ CrsDefinition mit kanonischem WKT2:2019

globale Koordinate + CRS
→ CoordinateTransformRequest
→ TransformerGroup-Auswahl
→ thread-lokaler pyproj-Transformer
→ CoordinateTransformResult

GlobalReferencePoint
→ kanonische geografische Koordinate
→ EarthGridPosition
→ chunk-ausgerichteter EarthStorageOrigin
→ EarthGridFrame
→ lokale Block-, Chunk- und Sub-Block-Koordinaten
```

Persistiert werden sollen:

```text
genau ein GlobalReferencePoint pro Earth-WorldInstance
lokale World-, Chunk-, Block-, Objekt-, Spieler- und Spawnzustände
```

Nicht persistiert werden sollen:

```text
native pyproj.CRS-Objekte
native pyproj.Transformer
EarthGridFrame
ResolvedEarthAnchor
EarthStorageOrigin als eigenständige Wahrheit
abgeleitete globale Koordinaten je Entity
Cacheinhalte
```

---

## 2. Ordner- und Dateistruktur

```text
services/
└── vectoplan-chunk/
    └── src/
        └── georeferencing/
            ├── __init__.py
            │   ├── stabile öffentliche Paketfassade
            │   ├── 101 Lazy-Exports
            │   ├── kontrolliertes Preloading
            │   ├── Runtime-Readiness
            │   ├── Cache-Diagnose
            │   └── geordneter Cache-Reset
            │
            ├── errors.py
            │   ├── GeoreferencingErrorCode
            │   ├── Validierungsfehler
            │   ├── Konfigurationsfehler
            │   ├── Referenz- und Revisionskonflikte
            │   ├── CRS-Fehler
            │   ├── Transformationsfehler
            │   └── sichere CRS-Zusammenfassungen
            │
            ├── contracts.py
            │   ├── GlobalCoordinate
            │   ├── CrsDefinition
            │   ├── EarthGridReference
            │   ├── GlobalReferencePoint
            │   ├── TransformationPolicy
            │   ├── TransformationAccuracy
            │   ├── CoordinateTransformRequest
            │   ├── CoordinateTransformResult
            │   ├── EarthGridPosition
            │   └── ResolvedEarthAnchor
            │
            ├── crs.py
            │   ├── pyproj kontrolliert laden
            │   ├── CRS-Eingaben normalisieren
            │   ├── CRS prüfen und kanonisieren
            │   ├── WKT2:2019 erzeugen
            │   ├── EPSG:4979 und EPSG:4978 bereitstellen
            │   ├── PROJ-Netzwerk steuern
            │   └── CRS-/PROJ-Readiness prüfen
            │
            ├── transformer.py
            │   ├── TransformerGroup auswählen
            │   ├── Genauigkeit und Grids prüfen
            │   ├── Ballpark verhindern
            │   ├── 2D-/3D-Transformationen ausführen
            │   ├── Roundtripfehler messen
            │   ├── thread-lokale Transformer cachen
            │   └── Transformationsdiagnose liefern
            │
            ├── earth_grid.py
            │   ├── globale Earth-Grid-Definition
            │   ├── equirektangulare Abbildung
            │   ├── periodische X-Kanonisierung
            │   ├── Polgrenzen
            │   ├── Storage-Origin ableiten
            │   ├── EarthGridFrame erzeugen
            │   ├── global → lokal
            │   └── lokal → global
            │
            └── IST-Zustand.md
                └── diese Dokumentation
```

---

## 3. Größenordnung

Die sechs vorliegenden Python-Dateien umfassen:

```text
9.766 Quellcodezeilen
299.487 Bytes
59 Top-Level-Klassen
153 Top-Level-Funktionen
101 öffentliche Lazy-Symbole
```

| Datei | Zeilen | Klassen | Top-Level-Funktionen | Hauptaufgabe |
|---|---:|---:|---:|---|
| `__init__.py` | 777 | 0 | 13 | Paketfassade und Runtime-Orchestrierung |
| `errors.py` | 1.227 | 29 | 13 | Domänenfehler |
| `contracts.py` | 1.834 | 13 | 12 | immutable Verträge |
| `crs.py` | 1.739 | 3 | 40 | CRS-Auflösung und PROJ-Runtime |
| `transformer.py` | 2.060 | 7 | 41 | Operationsauswahl und Transformation |
| `earth_grid.py` | 2.129 | 7 | 34 | globales Raster und lokale Frames |

---

## 4. Wie diese Bestandsaufnahme erstellt wurde

Die Dokumentation basiert auf:

```text
vollständigem Lesen der sechs hochgeladenen Dateien
AST-Auswertung aller Klassen, Methoden, Funktionen und Konstanten
Python-Syntaxprüfung aller Dateien
Abgleich der öffentlichen Lazy-Symboltabelle
Auflösung aller 101 öffentlichen Symbole
isolierter pyproj-/PROJ-Laufzeitprüfung
isolierten CRS- und Transformer-Tests
isolierten Earth-Grid- und Roundtrip-Tests
Prüfung einer 2D-Referenz ohne globale Höhe
Prüfung der periodischen X-Naht
Prüfung der Polgrenzen
Prüfung des thread-lokalen Transformer-Caches
```

Für den isolierten Lauf wurden nur die fehlenden Typen aus `src/coordinates`
als minimaler Schnittstellenstub bereitgestellt.

Daher gilt:

```text
CRS-, pyproj-, PROJ-, Transformer- und Earth-Grid-Code
→ tatsächlich aus den vorliegenden Dateien ausgeführt

vollständige Integration mit dem echten src/coordinates-Paket
→ in dieser Teilprüfung nicht bestätigt
```

---

## 5. Statischer und isolierter Prüfstatus

Alle sechs Dateien:

```text
→ syntaktisch gültig
```

Alle 101 öffentlichen Lazy-Symbole:

```text
→ im isolierten Paketkontext erfolgreich aufgelöst
```

Installierte Laufzeit:

```text
pyproj = 3.7.2
PROJ   = 9.5.1
```

Isolierte Runtime-Ergebnisse:

```text
pyproj verfügbar                 = ja
Mindestversion >= 3.7.0          = ja
PROJ-Datenverzeichnis verfügbar  = ja
proj.db verfügbar                = ja
EPSG:4979 verfügbar              = ja
EPSG:4978 verfügbar              = ja
PROJ-Netzwerk deaktiviert        = ja
Transformer-Auswahl bereit       = ja
Transformer-Roundtrip bereit     = ja
Earth-Grid-Definition bereit     = ja
Earth-Grid-Frame bereit          = ja
Earth-Grid-Roundtrip bereit      = ja
```

Gemessener Beispiel-Roundtrip:

```text
EPSG:4979 → EPSG:4978 → EPSG:4979
Fehler ≈ 0,00000000479 m
Grenze = 0,001 m
```

Earth-Grid-Smoke-Test:

```text
Roundtripfehler = 0 Zellen
```

---

## 6. Paketarchitektur

```text
src.georeferencing
│
├── contracts
│   └── stabile immutable Domainobjekte
│
├── errors
│   └── stabile Fehlercodes und sichere Details
│
├── crs
│   └── Userinput → pyproj.CRS → CrsDefinition
│
├── transformer
│   └── CrsDefinition-Paar → Operation → Transformationsresultat
│
└── earth_grid
    └── GlobalReferencePoint → EarthGridFrame → lokale Koordinaten
```

Abhängigkeiten:

```text
src/georeferencing/errors.py
└── src/coordinates/errors.py
└── src/coordinates/models.py

src/georeferencing/contracts.py
├── src/coordinates/models.py
└── georeferencing/errors.py

src/georeferencing/crs.py
├── contracts.py
├── errors.py
└── pyproj / PROJ

src/georeferencing/transformer.py
├── contracts.py
├── crs.py
├── errors.py
└── pyproj.transformer

src/georeferencing/earth_grid.py
├── src/coordinates/models.py
├── src/coordinates/topology.py
├── contracts.py
├── crs.py
├── transformer.py
└── errors.py
```

---

## 7. Öffentliche Paketfassade – `__init__.py`

## 7.1 Paketmetadaten

```text
MODULE_VERSION               = 1.0.0
PACKAGE_ID                   = vectoplan-georeferencing
PUBLIC_API_VERSION           = georeferencing-api.v1
EARTH_WORLD_CONTRACT_VERSION = earth-world.v1
```

---

## 7.2 Aufgabe

Die Fassade stellt stabile öffentliche Namen bereit, ohne beim normalen Import
sofort die schwere Runtime zu initialisieren.

```python
import src.georeferencing
```

lädt zunächst nicht automatisch:

```text
pyproj
PROJ-Datenbank
TransformerGroup
EarthGridFrame
Runtime-Readiness
```

---

## 7.3 Lazy-Import-Gruppen

Öffentliche Symbole insgesamt:

```text
101
```

Verteilung:

```text
errors.py      = 31
contracts.py   = 14
crs.py         = 17
transformer.py = 11
earth_grid.py  = 28
```

Die Zuordnung ist statisch über:

```text
_SYMBOL_TO_MODULE
```

definiert.

Ein unbekanntes Symbol:

```text
→ AttributeError
```

Ein in der Tabelle vorhandenes, aber im Zielmodul fehlendes Symbol:

```text
→ ImportError
```

---

## 7.4 Lazy-Caches

```text
_SYMBOL_CACHE
→ bereits aufgelöste öffentliche Objekte

_MODULE_CACHE
→ bereits importierte freigegebene Untermodule

_STATE_LOCK
→ schützt beide Caches
```

Importierte Module werden nicht aus `sys.modules` entfernt.

---

## 7.5 Kontrolliertes Preloading

```python
preload_georeferencing_modules(
    include_heavy_runtime=True,
    strict=True,
)
```

Mit:

```text
include_heavy_runtime=false
→ errors und contracts

include_heavy_runtime=true
→ zusätzlich crs, transformer und earth_grid
```

`strict=true`:

```text
→ Status aller Module wird aufgebaut
→ erster Importfehler wird danach erneut ausgelöst
```

---

## 7.6 Explizite Runtimeinitialisierung

```python
initialize_georeferencing_runtime(
    network_enabled=False,
    strict=True,
)
```

Ablauf:

```text
Module preloaden
→ PROJ-Netzwerkstatus explizit setzen
→ CRS-Readiness prüfen
→ Transformer-Readiness prüfen
→ Earth-Grid-Readiness prüfen
→ aggregierten Status liefern
```

Bei `strict=true` und Unreadiness:

```text
GeoreferencingConfigurationError
```

---

## 7.7 Readiness

```python
georeferencing_runtime_status(
    require_network_disabled=True,
)
```

Teilchecks:

```text
crs
transformer
earthGrid
```

Jeder Teilcheck wird isoliert ausgeführt.

Ein Fehler in einer Komponente verhindert nicht, dass die anderen Statusobjekte
sichtbar werden.

---

## 7.8 Cacheverwaltung

```text
georeferencing_cache_info()
clear_georeferencing_caches()
reset_georeferencing_package_state()
```

Resetreihenfolge:

```text
Earth-Grid
→ Transformer
→ CRS
```

Begründung:

```text
höherliegende Frames hängen von Transformern ab
Transformer hängen von CRS ab
```

---

## 7.9 Resetgrenze

Nicht zurückgesetzt werden:

```text
persistierte Daten
GlobalReferencePoint
PROJ-Netzwerk automatisch auf Default
importierte Pythonmodule aus sys.modules
native Bibliothekszustände
```

Optional können öffentliche Lazy-Symbole aus dem Package-Namespace entfernt
werden.

---

# Teil I – Fehlerhierarchie

## 8. `errors.py`

## 8.1 Aufgabe

Das Modul erweitert die gemeinsame `CoordinateError`-Basis um stabile
Georeferenzierungsfehler.

Es führt nicht aus:

```text
kein Logging
kein Rollback
keine HTTP-Antwort
keine Datenbankoperation
```

Diese Aufgaben bleiben äußeren Schichten vorbehalten.

---

## 8.2 Fehlercode-Enum

`GeoreferencingErrorCode` definiert stabile maschinenlesbare Codes.

Allgemein:

```text
georeferencing_error
georeferencing_validation_failed
georeferencing_configuration_invalid
georeferencing_computation_failed
georeferencing_conflict
georeferencing_dependency_unavailable
```

Earth-Referenz:

```text
earth_world_reference_required
earth_reference_invalid
earth_reference_conflict
world_reference_locked
coordinate_frame_revision_conflict
```

CRS:

```text
coordinate_crs_required
coordinate_crs_invalid
coordinate_crs_unsupported
coordinate_crs_dimension_mismatch
coordinate_crs_axis_order_invalid
coordinate_crs_unit_invalid
coordinate_crs_not_transformable
```

Transformation:

```text
coordinate_transform_unavailable
coordinate_transform_failed
coordinate_transform_not_exact
coordinate_transform_ballpark_forbidden
coordinate_transform_grid_missing
coordinate_transform_accuracy_unknown
coordinate_transform_precision_exceeded
coordinate_transform_roundtrip_failed
```

Runtime:

```text
pyproj_unavailable
proj_database_unavailable
```

Veröffentlichte Werte dürfen nicht:

```text
umbenannt
für neue Bedeutungen wiederverwendet
```

werden.

---

## 8.3 Basisklassen und Statussemantik

| Klasse | Standardstatus | Bedeutung |
|---|---:|---|
| `GeoreferencingError` | 422 | allgemeiner erwartbarer Georeferenzierungsfehler |
| `GeoreferencingValidationError` | 422 | fachlich ungültige Eingabe |
| `GeoreferencingConfigurationError` | 500 | fehlerhafte Runtime-/Grid-/CRS-Konfiguration |
| `GeoreferencingComputationError` | 500 | unerwarteter technischer Berechnungsfehler |
| `GeoreferencingConflictError` | 409 | Konflikt mit persistiertem Zustand |
| `GeoreferencingDependencyUnavailableError` | 503 | lokale Abhängigkeit fehlt |

Spezifische Klassen erben diese Statuswerte, sofern sie keinen eigenen
Standardstatus setzen.

---

## 8.4 Earth-Referenzfehler

```text
EarthWorldReferenceRequiredError
→ Earth-World besitzt keinen Referenzpunkt

EarthReferenceInvalidError
→ Referenz verletzt Dimension, CRS oder Gridvertrag

EarthReferenceConflictError
→ idempotente Anlage trifft auf andere Referenz

WorldReferenceLockedError
→ Referenzänderung nach Materialisierung

CoordinateFrameRevisionConflictError
→ erwartete und tatsächliche Referenzrevision unterscheiden sich
```

---

## 8.5 CRS-Fehler

```text
CrsRequiredError
CrsInvalidError
CrsUnsupportedError
CrsDimensionMismatchError
CrsAxisOrderInvalidError
CrsUnitInvalidError
CrsNotTransformableError
```

Wichtige Regel:

```text
fehlende oder ungültige CRS-Angaben
→ niemals still raten
```

---

## 8.6 Transformationsfehler

```text
TransformationUnavailableError
TransformationFailedError
TransformationNotExactError
BallparkTransformationForbiddenError
TransformationGridMissingError
TransformationAccuracyUnknownError
TransformationPrecisionExceededError
TransformationRoundtripFailedError
```

Fehlende lokale Grids und fehlende Transformationen werden als 503-nahe
Konfigurations-/Dependency-Zustände modelliert.

---

## 8.7 Abhängigkeitsfehler

```text
PyprojUnavailableError
ProjDatabaseUnavailableError
```

Diese Fehler markieren lokale Runtimeprobleme und nicht ungültige
Benutzereingaben.

---

## 8.8 Sichere CRS-Diagnose

```python
summarize_crs_input(value)
```

Lange CRS-Texte werden nicht vollständig ausgegeben.

Für lange Strings:

```text
Typ
Länge
SHA-256-Präfix
```

Kurze einzeilige Werte bis 128 Zeichen dürfen inline erscheinen.

WKT-, PROJJSON-, PROJ- und Pfadinformationen werden dadurch nicht
unkontrolliert in Logs oder API-Antworten kopiert.

---

## 8.9 Fehlergrenze

```python
ensure_georeferencing_error(
    error,
    operation=...,
    details=...,
)
```

Verhalten:

```text
bekannter CoordinateError
→ unverändert zurückgeben

unerwartete Exception
→ GeoreferencingComputationError
→ Original nur als cause
→ öffentlicher Payload enthält nur sicheren Ursachetyp
```

---

# Teil II – Verträge

## 9. `contracts.py`

## 9.1 Grundregel

Alle zentralen Dataclasses sind:

```text
frozen
slots-basiert
frameworkunabhängig
```

Sie bilden die stabile Grenze zwischen:

```text
HTTP-/Provisioning-Eingabe
CRS-Auflösung
Transformation
Earth-Grid
Persistenz
Runtimecache
```

---

## 9.2 `CoordinateDimension`

```text
TWO_D   = 2
THREE_D = 3
```

---

## 9.3 `CrsDefinitionFormat`

```text
authority-code
wkt
projjson
proj-string
```

Die aktuelle `crs.py`-Kanonisierung erzeugt für persistierbare aufgelöste CRS
normalerweise:

```text
definitionFormat = wkt
WKT-Version      = WKT2:2019
```

---

## 9.4 `TransformationOperationKind`

```text
reference-to-canonical
canonical-to-reference
local-to-global
global-to-local
roundtrip-validation
import-to-canonical
canonical-to-export
```

Der Wert beschreibt die fachliche Rolle einer Transformation.

---

## 9.5 `GlobalCoordinate`

Felder:

```text
x: Decimal
y: Decimal
z: Decimal | None
```

Semantik:

```text
2D
→ x, y

3D
→ x, y, z
```

Für geografische Koordinaten unter Earth v1:

```text
x = Longitude
y = Latitude
z = ellipsoidische Höhe
```

Begründung:

```text
Transformer verwenden verpflichtend always_xy=True.
```

Methoden:

```text
from_values()
from_mapping()
as_decimal_tuple()
as_float_tuple()
to_dict()
fingerprint_payload()
```

Normalisierung:

```text
bool verboten
NaN/Infinity verboten
maximal 80 Dezimalziffern
begrenzter Exponent
kanonische Stringserialisierung ohne unnötige Nullen
```

---

## 9.6 `CrsDefinition`

Felder:

```text
crs_id
definition_format
definition
coordinate_dimension

authority
code
name
axis_names
unit_names

is_geographic
is_projected
is_geocentric
is_vertical
is_compound
```

Die vollständige `definition` ist die reproduzierbare Persistenzbasis.

Normale API-Ausgabe:

```text
CRS-ID
Format
Definitionslänge
Definitionsfingerprint
Dimension
Authority
Code
Name
Achsen
Einheiten
Klassifikation
```

Die vollständige Definition wird nur ausgegeben mit:

```text
include_definition=true
```

oder:

```text
to_persistence_dict()
```

---

## 9.7 Native Achsen versus Runtimeargumente

`axis_names` und `axis_directions` beschreiben die native CRS-Metadatenreihenfolge.

Beispiel EPSG:4979:

```text
native Achsenmetadaten
→ Latitude, Longitude, Height
```

Transformationsaufrufe verwenden trotzdem:

```text
always_xy=True
→ x=Longitude, y=Latitude
```

Daraus folgt:

```text
Aufrufer dürfen die Argumentreihenfolge nicht aus axis_names ableiten.
```

Die externe Earth-v1-Eingabekonvention bleibt immer XY.

---

## 9.8 `EarthGridReference`

Felder:

```text
grid_id
grid_version
projection_id
projection_version
topology_type
axis_convention
```

Erlaubte Achsenkonvention:

```text
x-east-y-up-z-north
```

Property:

```text
key = grid_id@grid_version
```

---

## 9.9 `GlobalReferencePoint`

Der persistierte globale Referenzpunkt einer Earth-World.

Felder:

```text
coordinate
crs
grid
reference_version
source
source_reference_id
```

Schema:

```text
earth-global-reference.schema.v1
```

Prüfungen:

```text
coordinate ist GlobalCoordinate
crs ist CrsDefinition
grid ist EarthGridReference
reference_version > 0
Koordinatendimension <= CRS-Dimension
geozentrisches CRS verlangt 3D-Koordinate
```

Fingerprint umfasst:

```text
Schema
Revision
Koordinate
CRS-ID
CRS-Definitionsfingerprint
Grid
Quelle
Quellreferenz-ID
```

Persistenz:

```python
to_persistence_dict()
```

enthält:

```text
Decimal-Koordinaten als Strings
vollständige kanonische CRS-Definition
Gridvertrag
Revision
Quelle
Fingerprint
```

---

## 9.10 Kardinalität

Der Vertrag beschreibt einen Referenzpunkt.

Die Regel:

```text
genau ein GlobalReferencePoint pro Earth-WorldInstance
```

wird nicht durch dieses Package allein als Datenbank-Unique-Constraint
durchgesetzt.

Erforderliche äußere Sicherung:

```text
WorldInstance-Modell
Provisioning-Service
Repository
Datenbankconstraint oder atomarer Upsert
```

---

## 9.11 `TransformationPolicy`

Standard:

```text
allow_ballpark                  = false
require_best_available          = true
require_known_accuracy          = false
maximum_accuracy_m              = null
validate_roundtrip              = true
maximum_roundtrip_error_m       = 0.001
always_xy                       = true
```

Harte Regel:

```text
always_xy=false
→ ungültig
```

Wenn Roundtripvalidierung aktiv ist:

```text
maximum_roundtrip_error_m muss vorhanden sein
```

---

## 9.12 `TransformationAccuracy`

Felder:

```text
best_available
ballpark
reported_accuracy_m
measured_roundtrip_error_m
required_grids
missing_grids
```

Validiert sich gegen `TransformationPolicy`.

Mögliche Ablehnungsgründe:

```text
Ballpark nicht erlaubt
best_available=false
Genauigkeit unbekannt, aber erforderlich
gemeldete Genauigkeit zu schlecht
Roundtripfehler fehlt
Roundtripfehler überschreitet Grenze
```

---

## 9.13 `CoordinateTransformRequest`

Felder:

```text
coordinate
source_crs
target_crs
operation
policy
request_id
```

Schema:

```text
coordinate-transform-request.v1
```

Prüfungen:

```text
Typen korrekt
Operation gültig
Koordinatendimension <= Source-CRS-Dimension
```

Property:

```text
is_identity_transform
```

verwendet semantischen CRS-Vergleich über:

```text
CRS-ID
Definitionsfingerprint
Dimension
```

---

## 9.14 `CoordinateTransformResult`

Felder:

```text
request
coordinate
accuracy
operation_name
pipeline
```

Schema:

```text
coordinate-transform-result.v1
```

Beim Erzeugen:

```text
Zieldimension prüfen
Operationsname normalisieren
Pipeline begrenzen
Accuracy gegen Requestpolicy validieren
```

Normale Ausgabe enthält nicht die Pipeline selbst, sondern:

```text
Länge
SHA-256-Fingerprint
```

---

## 9.15 `EarthGridPosition`

Sub-Zell-Position im globalen kanonischen Earth-Raster.

```text
x: Decimal
y: Decimal
z: Decimal
```

Coordinate Space:

```text
earth_grid
```

---

## 9.16 `ResolvedEarthAnchor`

Abgeleitetes Runtimeobjekt aus einem `GlobalReferencePoint`.

Felder:

```text
reference
canonical_coordinate
canonical_crs
grid_position
accuracy
resolver_version
```

Schema:

```text
resolved-earth-anchor.v1
```

Es darf gecacht werden.

Es ist keine Persistenzwahrheit.

---

# Teil III – CRS-Auflösung

## 10. `crs.py`

## 10.1 Aufgabe

`crs.py` ist die einzige vorgesehene Eingangsschicht für CRS-Eingaben im
Earth-v1-Slice.

Andere Schichten sollen nicht:

```text
eigene pyproj.CRS-Parser bauen
abweichende CRS-Allowlisten verwenden
Netzwerkpolicy selbst ändern
WKT eigenständig kanonisieren
```

---

## 10.2 Laufzeitkonstanten

```text
Mindest-pyproj-Version       = 3.7.0
kanonisches geografisches CRS = EPSG:4979
kanonisches geozentrisches CRS = EPSG:4978
kanonisches WKT               = WKT2_2019
```

---

## 10.3 Unterstützte CRS-Eingaben

```text
CrsDefinition
positive EPSG-Integer
String
Authority-Tupel
Mapping / PROJJSON-nahe Struktur
native pyproj.CRS
Objekt mit to_wkt()
```

Beispiele:

```python
resolve_crs("EPSG:4979")
resolve_crs(4979)
resolve_crs(("EPSG", "4979"))
resolve_crs(existing_crs_definition)
```

Isoliert bestätigt:

```text
alle vier Formen lösen zu EPSG:4979 auf
```

---

## 10.4 Kein numerisches CRS-Raten

Die Regel:

```text
Ein CRS wird nie aus Koordinatenwerten geraten.
```

bedeutet nicht, dass ein expliziter Integer als EPSG-Code verboten ist.

```python
resolve_crs(4979)
```

ist eine explizite CRS-Eingabe.

Nicht erlaubt wäre:

```text
aus x/y-Werten automatisch ein CRS vermuten
```

---

## 10.5 Inputgrenzen

```text
maximale String-/WKT-/PROJ-Länge = 1 MiB
maximale Mappingtiefe            = 24
maximale Mappingelemente         = 100.000
maximale Allowlistgröße          = 4.096
CRS-Dimensionen                  = 2 oder 3
Authorityconfidence             = 0 bis 100
```

NaN und Infinity sind in Mappings verboten.

---

## 10.6 `CrsResolutionPolicy`

Felder:

```text
role
allowed_dimensions
allowed_authorities
allowed_crs_ids

allow_geographic
allow_projected
allow_geocentric
allow_compound
allow_vertical_only
allow_engineering
allow_bound
allow_deprecated

require_authority_match
minimum_authority_confidence
```

Vordefinierte Policies:

```text
earth_reference_default()
source_dataset_default()
```

---

## 10.7 Auflösungsfluss

```text
resolve_crs(value, policy)
→ Eingabetyp normalisieren
→ kanonischen Cache-Key bilden
→ pyproj.CRS erzeugen
→ native CRS-Metadaten untersuchen
→ native CRS gegen Policy prüfen
→ WKT2:2019 erzeugen
→ CrsDefinition erzeugen
→ Vertrag erneut gegen Policy prüfen
```

---

## 10.8 `CrsInspection`

Enthält:

```text
crs_id
type_name
authority
code
dimension

axis_names
axis_directions
unit_names

is_geographic
is_projected
is_geocentric
is_vertical
is_compound
is_engineering
is_bound
is_deprecated

area_of_use_name
```

Keine vollständige WKT-Ausgabe.

---

## 10.9 CRS-ID

Wenn Authority und Code sicher erkannt werden:

```text
EPSG:4979
```

Ohne Authority:

```text
CUSTOM:<24 Zeichen SHA-256-Präfix>
```

Die kanonische WKT bleibt unabhängig davon die Persistenzdefinition.

---

## 10.10 Policyprüfungen

Abgelehnt werden abhängig von der Policy:

```text
veraltete CRS
Engineering-CRS
Bound-CRS
rein vertikale CRS
nicht klassifizierbare CRS
unerlaubte Dimension
unerlaubter CRS-Typ
nicht erlaubte Authority
nicht erlaubte CRS-ID
fehlender Authoritymatch
```

---

## 10.11 Kanonische Earth-CRS

```python
canonical_geographic_crs()
→ EPSG:4979
→ geografisch
→ 3D

canonical_geocentric_crs()
→ EPSG:4978
→ geozentrisch
→ 3D
```

Beide werden jeweils in einem LRU-Eintrag gecacht.

---

## 10.12 Native CRS

```python
resolve_native_crs()
```

ist ausschließlich für interne Runtimekomponenten vorgesehen.

Native Objekte dürfen nicht:

```text
persistiert
über HTTP serialisiert
als langfristiger fachlicher Vertrag verwendet
```

werden.

---

## 10.13 Semantische Äquivalenz

```python
crs_equivalent(
    first,
    second,
    ignore_axis_order=False,
)
```

delegiert an `pyproj.CRS.equals()`.

---

## 10.14 PROJ-Netzwerk

```python
configure_proj_network(enabled=False)
```

ändert den globalen PROJ-Netzwerkzustand explizit.

Vorgesehener Serverstandard:

```text
disabled
```

Grund:

```text
reproduzierbare Images
keine stillen Grid-Downloads
keine unerwarteten externen Abhängigkeiten
```

Die Netzwerkpolicy wird beim Modulimport nicht verändert.

---

## 10.15 Globaler Prozesszustand

Der PROJ-Netzwerkstatus ist ein globaler Runtimezustand.

Daraus folgt:

```text
einmal kontrolliert beim Startup initialisieren
nicht pro Request umschalten
nicht konkurrierend mit laufenden Transformationen ändern
```

`RLock` schützt den Setzvorgang, aber die fachliche Startup-Reihenfolge bleibt
Aufgabe der Anwendung.

---

## 10.16 CRS-Readiness

```python
crs_runtime_status(
    require_network_disabled=True,
    validate_required_crs=True,
)
```

Prüft:

```text
pyproj importierbar
pyproj-Version
PROJ-Version
PROJ-Datenverzeichnis
proj.db
PROJ-Datenbankmetadaten
Authorityliste
Netzwerkstatus
EPSG:4979
EPSG:4978
```

---

## 10.17 `ensure_crs_runtime_ready()`

Wirft bei Problemen:

```text
PyprojUnavailableError
GeoreferencingConfigurationError
ProjDatabaseUnavailableError
```

---

## 10.18 CRS-Caches

```text
native CRS-Cache          = 256
kanonisches EPSG:4979     = 1
kanonisches EPSG:4978     = 1
```

Reset:

```python
clear_crs_caches()
```

---

## 10.19 Cache-Reset-Grenze

`clear_crs_caches()` leert nicht:

```text
_PYPROJ_MODULE
_PYPROJ_IMPORT_ERROR
PROJ-Netzwerkstatus
importierte pyproj-Module
```

Folge:

```text
ein einmal fehlgeschlagener pyproj-Import bleibt im Prozess als Fehler
gespeichert, bis der Prozess neu startet
```

Für normale Serverprozesse ist das deterministisch.

Für dynamische Entwicklungsumgebungen muss ein Prozessneustart eingeplant werden.

---

# Teil IV – Transformer

## 11. `transformer.py`

## 11.1 Aufgabe

Das Modul:

```text
wählt eine konkrete pyproj-Operation
prüft Bestverfügbarkeit
prüft Ballparkstatus
prüft Grids
führt 2D-/3D-Transformationen aus
misst Roundtripfehler
hält native Transformer threadlokal
```

---

## 11.2 Harte Regeln

```text
always_xy = true
Ballpark standardmäßig verboten
best_available standardmäßig erforderlich
fehlende Grids werden nicht heruntergeladen
errcheck = true
native Transformer nicht threadübergreifend verwenden
```

---

## 11.3 Grenzen

```text
thread-lokaler Cache        = 64 Transformer
maximale Batchgröße         = 100.000 Requests
maximale Pipeline           = 262.144 Zeichen
maximale Gridanzahl         = 1.024
maximale Operationsbezeichnung = 2.048 Zeichen
```

---

## 11.4 `AreaOfInterestBounds`

Felder:

```text
west_longitude_deg
south_latitude_deg
east_longitude_deg
north_latitude_deg
```

Regeln:

```text
Longitude in [-180, 180]
Latitude in [-90, 90]
west < east
south < north
```

Antimeridianüberschreitende AOI:

```text
nicht in einem Boundsobjekt unterstützt
→ Anfrage in mehrere AOIs teilen
```

---

## 11.5 `TransformerSelectionOptions`

```text
area_of_interest
authority
allow_superseded
```

Diese Parameter beeinflussen die technische Auswahl, nicht den fachlichen
Genauigkeitsvertrag.

---

## 11.6 `GridRequirement`

Sichere Diagnose eines PROJ-Grids:

```text
short_name
available
package_name
url_fingerprint
direct_download
open_license
```

Die vollständige Download-URL wird nicht ausgegeben.

---

## 11.7 `TransformerDescriptor`

Serialisierbare Operationsbeschreibung:

```text
cache_key
source_crs_id
target_crs_id
operation_name

pipeline
reported_accuracy_m
best_available
ballpark
has_inverse
network_enabled

available_transformer_count
unavailable_operation_count

required_grids
missing_grids
selection_warnings
```

Normale Ausgabe:

```text
Pipeline nicht enthalten
Pipeline-Länge
Pipeline-Fingerprint
```

---

## 11.8 `TransformerSelection`

Enthält nur serialisierbare Verträge:

```text
Descriptor
Source-CrsDefinition
Target-CrsDefinition
Options
Policy
```

Es enthält keinen nativen Transformer.

---

## 11.9 Threadinterne Klassen

```text
_ThreadTransformerEntry
→ thread_id
→ native_transformer
→ öffentliche Selection

_ThreadTransformerState
→ Cachegeneration
→ OrderedDict mit LRU-Reihenfolge
```

Diese Typen sind nicht öffentlich.

---

## 11.10 Auswahlfluss

```text
select_transformer(source, target, policy, options)
→ Cache-Key aus CRS-Fingerprints, Policy und Optionen
→ Threadcache prüfen
→ Source- und Target-CRS nativ auflösen
→ TransformerGroup erzeugen
→ verfügbare und nicht verfügbare Operationen lesen
→ fehlende Grids sammeln
→ best_available prüfen
→ erste zulässige Operation auswählen
→ Ballpark prüfen
→ gemeldete Genauigkeit prüfen
→ Descriptor erzeugen
→ nativen Transformer nur im aktuellen Thread cachen
→ serialisierbare Selection zurückgeben
```

---

## 11.11 TransformerGroup-Parameter

```text
always_xy=True
area_of_interest
authority
accuracy
allow_ballpark
allow_superseded
```

---

## 11.12 Fehlende Grids

Wenn keine Operation verfügbar ist und Grids fehlen:

```text
TransformationGridMissingError
```

Wenn `require_best_available=true` und bessere Operation wegen Grid fehlt:

```text
ebenfalls TransformationGridMissingError
```

Automatischer Download:

```text
false
```

---

## 11.13 Transformationsfluss

```text
CoordinateTransformRequest
→ Identity prüfen

Identity
→ Koordinate unverändert
→ Accuracy = 0
→ operationName = identity

nicht Identity
→ Threadtransformer auflösen
→ Threadownership prüfen
→ forward mit errcheck=True
→ optional inverse Roundtrip
→ Fehler in Metern messen
→ tatsächlich zuletzt verwendete Operation lesen
→ Accuracyobjekt aufbauen
→ CoordinateTransformResult erzeugen
→ Policyvalidierung erfolgt erneut
```

---

## 11.14 2D und 3D

Für 2D:

```python
transformer.transform(x, y, ...)
```

Für 3D:

```python
transformer.transform(x, y, z, ...)
```

Transformationsresultate müssen genau zwei oder drei Werte besitzen.

Die Resultatdimension darf die Ziel-CRS-Dimension nicht überschreiten.

---

## 11.15 Roundtripmessung

Geografischer Source-Raum:

```text
horizontale Distanz über native Geod.inv()
vertikale Differenz in Meter
kombiniert über Hypotenuse
```

Linearer Source-Raum:

```text
Differenz je Achse
× unit_conversion_factor
euklidische Distanz in Meter
```

---

## 11.16 Tatsächlich verwendete Operation

Nach der Transformation wird versucht:

```python
transformer.get_last_used_operation()
```

Dadurch können:

```text
Name
Pipeline
Accuracy
Ballparkstatus
Grids
```

der tatsächlich genutzten Operation statt nur der Vorauswahl dokumentiert
werden.

---

## 11.17 Batch

```python
transform_coordinate_batch(requests)
```

Eigenschaften:

```text
Eingabereihenfolge bleibt erhalten
beim ersten Fehler Abbruch
kein partielles Ergebnis
keine Persistenz
```

Implementierung:

```text
Schleife über Einzeltransformationen
keine pyproj-Vektorisierung
```

Bei sehr großen Batches kann dies CPU- und Python-Overhead erzeugen.

---

## 11.18 Thread-Sicherheit

Native Transformer bleiben im erzeugenden Thread.

Vor Verwendung:

```text
createdThreadId == currentThreadId
```

anderenfalls:

```text
GeoreferencingConfigurationError
```

---

## 11.19 Cacheinvalidierung

Andere Threadcaches können nicht direkt geleert werden.

Lösung:

```text
globaler Generationzähler erhöhen
→ aktueller Thread leert sofort
→ andere Threads erkennen neue Generation beim nächsten Zugriff
→ eigener Cache wird dann geleert
```

---

## 11.20 Transformer-Metriken

```text
generation
hits
misses
evictions
selectionsCreated
transformationsExecuted
transformationsFailed
```

`clear_transformer_caches()` setzt zurück:

```text
hits
misses
evictions
```

Nicht zurückgesetzt werden:

```text
selectionsCreated
transformationsExecuted
transformationsFailed
```

Diese bleiben kumulative Prozessmetriken.

---

## 11.21 Isolierter Cachetest

Nach Cache-Reset:

```text
erste Transformation
→ 1 Miss
→ 1 Selection
→ Threadcachegröße 1

zweite Transformation mit gleichem Vertrag
→ 1 Hit
→ weiterhin nur 1 Selection
```

Damit wurde die threadlokale LRU-Wiederverwendung isoliert bestätigt.

---

# Teil V – Earth-Grid

## 12. `earth_grid.py`

## 12.1 Aufgabe

Das Modul bildet globale Earth-Koordinaten deterministisch auf ein global
einheitliches flaches Raster und daraus auf einen lokalen Speicherframe ab.

---

## 12.2 Kernunterscheidung

```text
exakter globaler Referenzpunkt
→ bleibt vollständig erhalten

exakte Earth-Grid-Position
→ kann Sub-Zell-Anteile besitzen

EarthStorageOrigin
→ wird per Floor auf globale Chunkgrenzen gesnappt

lokaler Frame
→ relativ zu diesem Storage-Origin
```

---

## 12.3 Persistenzmodell

Persistiert:

```text
GlobalReferencePoint
lokale Zustände
```

Ableitbar:

```text
kanonische geografische Referenz
EarthGridPosition
EarthStorageOrigin
EarthGridFrame
PeriodicXTopology
```

---

## 12.4 Rasterkonstanten

```text
Mapping-ID             = vectoplan-periodic-equirectangular
Mapping-Version        = 1
Topologie              = periodic-x-v1
Storage-Origin-Policy  = global-chunk-origin-floor-v1
Resolver-Version       = earth-grid-resolver.v1

Grid-ID                 = vectoplan-earth-grid
Grid-Version            = 1
Weltbreite              = 40.000.000 Zellen
Welthöhe                = 20.000.000 Zellen
Chunkgröße              = 16
Meter je Zelle vertikal = 1
Zentralmeridian         = 0°
Pol-Epsilon             = 0,000000000001°
```

Abgeleitet:

```text
Weltbreite in Chunks = 2.500.000
Welthöhe in Chunks   = 1.250.000

X-Bereich = [-20.000.000, 20.000.000)
Z-Bereich = [-10.000.000, 10.000.000)
```

---

## 12.5 Projektionsmodell

Horizontal:

```text
grid_x =
    canonical_longitude_delta / 360°
    × world_width_cells

grid_z =
    latitude / 180°
    × world_height_cells
```

Vertikal:

```text
mit Höhe:
grid_y = ellipsoidische Höhe / meters_per_cell

ohne Höhe:
grid_y = 0 als technischer Rasterwert
vertical_resolved = false
lokales Y = null
```

---

## 12.6 Fachliche Genauigkeitsgrenze

Die horizontale Abbildung ist:

```text
normiert
periodisch
equirektangular
```

Sie ist nicht:

```text
weltweit metrisch
flächentreu
winkeltreu
geodätisch distanztreu
```

Reale geodätische Analysen müssen weiterhin in geeigneten CRS oder über
geodätische Verfahren erfolgen.

---

## 12.7 `EarthGridDefinition`

Felder:

```text
grid
world_width_cells
world_height_cells
chunk_size
meters_per_cell
central_meridian_deg
pole_exclusion_epsilon_deg
storage_origin_policy
```

Prüfungen:

```text
Breite positiv, gerade und int64
Höhe positiv, gerade und int64
Chunkgröße positiv
Breite durch Chunkgröße teilbar
halbe Breite chunk-aligniert
Höhe durch Chunkgröße teilbar
halbe Höhe chunk-aligniert
Pol-Epsilon > 0 und < 90
Projection-ID/-Version korrekt
Topologietyp korrekt
Achsenkonvention korrekt
Storage-Origin-Policy korrekt
```

---

## 12.8 Definitionfingerprint

Fingerprint umfasst:

```text
Schema
Grididentität
Weltgröße
Chunkgröße
Zellmaß
Zentralmeridian
Pol-Epsilon
Storage-Origin-Policy
horizontales Mapping
vertikales Mapping
```

---

## 12.9 Topologie pro Storage-Origin

```python
definition.topology_for_storage_origin(origin)
```

erzeugt eine `PeriodicXTopology`.

X:

```text
periodisch über gesamte Weltbreite
```

Z:

```text
lokal begrenzt
Grenzen hängen vom abgeleiteten Storage-Origin ab
```

Y:

```text
nicht durch EarthGridDefinition begrenzt
```

---

## 12.10 `LocalEarthPosition`

Felder:

```text
x: Decimal
y: Decimal | None
z: Decimal
```

Coordinate Space:

```text
earth_local_grid
```

`y=None` bedeutet:

```text
globale Höhe nicht auflösbar
```

Konvertierungen:

```text
from_block_position()
from_metric_position()
to_metric_position()
```

`to_metric_position()` verlangt ein aufgelöstes Y.

---

## 12.11 `EarthStorageOrigin`

Felder:

```text
x: int64
y: int64
z: int64
vertical_resolved
```

Coordinate Space:

```text
earth_grid_chunk_origin
```

Regeln:

```text
alle drei Achsen chunk-aligniert
X innerhalb globaler Weltbreite
Z innerhalb globaler Welthöhe
Y int64
```

---

## 12.12 `EarthGridMappingResult`

Ergebnis einer globalen Koordinate im kanonischen Raster:

```text
Inputkoordinate
Input-CRS
kanonische Koordinate
kanonisches CRS
Gridposition
Transformationsaccuracy
vertical_resolved
```

---

## 12.13 `EarthGridFrame`

Enthält:

```text
GlobalReferencePoint
EarthGridDefinition
ResolvedEarthAnchor
EarthStorageOrigin
exakte lokale Referenzposition
PeriodicXTopology
```

Schema:

```text
earth-grid-frame.v1
```

Prüfungen:

```text
Referenzgrid == Definitionsgrid
Storage-Origin passt zur Definition
vertikaler Status konsistent
Topologie entspricht Definition + Origin
```

---

## 12.14 Framecache-Key

```text
Referenzfingerprint
Definitionsfingerprint
Resolverversion
```

SHA-256.

---

## 12.15 Referenzauflösung

```text
GlobalReferencePoint
→ Referenzkoordinate nach EPSG:4979 transformieren
→ Longitude/Latitude auf Earth-Grid abbilden
→ Höhe optional auf Grid-Y abbilden
→ exakten Gridpunkt erhalten
→ X/Y/Z per floor auf Chunkmultiple snappen
→ Storage-Origin erzeugen
→ exakte lokale Referenzdifferenz berechnen
→ PeriodicXTopology erzeugen
→ EarthGridFrame erzeugen
```

---

## 12.16 Floor-Snap

Für jede aufgelöste Achse:

```text
origin =
    floor(exact_grid_coordinate / chunk_size)
    × chunk_size
```

Bei negativen Werten wird echte Floor-Semantik verwendet.

Beispiel:

```text
exact = -1
chunkSize = 16
origin = -16
```

nicht:

```text
origin = 0
```

---

## 12.17 Periodische Decimal-Kanonisierung

Python- beziehungsweise Decimal-Restsemantik wird nicht direkt verwendet.

Stattdessen:

```text
Wert um Halbbreite verschieben
→ Quotient nach -∞ runden
→ Rest berechnen
→ Halbbreite abziehen
```

Zielbereich:

```text
[-width/2, width/2)
```

Dies ist für negative Werte und Antimeridianfälle entscheidend.

---

## 12.18 `global_to_local()`

Ablauf:

```text
Zielkoordinate nach EPSG:4979 transformieren
→ globale Earth-Grid-Position
→ Storage-Origin abziehen
→ X auf kürzeste periodische Darstellung kanonisieren
→ Z-Grenzen prüfen
→ LocalEarthPosition zurückgeben
```

Ergebnis:

```text
GlobalToLocalResult
```

---

## 12.19 `local_to_global()`

Ablauf:

```text
lokale Position kanonisieren
→ Storage-Origin addieren
→ globalen Gridpunkt bilden
→ Longitude/Latitude/Höhe rekonstruieren
→ kanonisches EPSG:4979
→ optional in Ziel-CRS transformieren
→ LocalToGlobalResult
```

---

## 12.20 2D-Referenz

Wenn der Referenzpunkt keine Höhe besitzt:

```text
storage_origin.y = 0
vertical_resolved = false
reference_local_position.y = null
```

Erlaubt:

```text
globale X/Z-Auflösung
lokale X/Z-Persistenz
2D-Roundtrip
```

Nicht erlaubt:

```text
lokales Y direkt als absolute globale Höhe ausgeben
```

Der isolierte Test bestätigte:

```text
2D-Referenz bei Longitude 179,999999°
→ Frame erfolgreich
→ y=null
→ globaler 2D-Roundtrip erfolgreich
```

---

## 12.21 Periodische X-Naht

Isolierter Test:

```text
Referenzlongitude = 179,999°
Ziel             = -179,999°
```

Ergebnis:

```text
lokale X-Differenz ≈ 222,222 Zellen
```

nicht:

```text
nahezu 40.000.000 Zellen
```

Damit wurde die kürzeste periodische X-Darstellung bestätigt.

---

## 12.22 Polgrenzen

Exakte Pole:

```text
Latitude +90°
Latitude -90°
```

werden abgelehnt.

Koordinaten knapp innerhalb des konfigurierten Epsilons werden akzeptiert.

Die Pole sind Grenzlinien, keine adressierbaren Zellen.

---

## 12.23 `GlobalToLocalResult`

```text
frame_cache_key
EarthGridMappingResult
LocalEarthPosition
```

Schema:

```text
earth-global-to-local.v1
```

---

## 12.24 `LocalToGlobalResult`

```text
frame_cache_key
LocalEarthPosition
EarthGridPosition
kanonische Koordinate
Zielkoordinate
Ziel-CRS
Accuracy
vertical_resolved
```

Schema:

```text
earth-local-to-global.v1
```

---

## 12.25 Earth-Grid-Caches

```text
Definitionen = 64
Frames       = 512
```

Cache-Key der Framefunktion umfasst immutable und hashbare Verträge:

```text
GlobalReferencePoint
EarthGridDefinition
TransformationPolicy
TransformerSelectionOptions
```

---

## 12.26 Earth-Grid-Readiness

```python
earth_grid_runtime_status()
```

Prüft:

```text
Defaultdefinition
EPSG:4979
Beispielreferenz
Frameauflösung
lokal → global
Roundtripfehler in Zellen
```

Readinessgrenze:

```text
<= 0,000001 Zellen
```

---

# Teil VI – Gesamtflüsse

## 13. Runtime-Startup

```text
Anwendung startet
→ initialize_georeferencing_runtime(network_enabled=false)
→ errors/contracts/crs/transformer/earth_grid importieren
→ PROJ-Netzwerk deaktivieren
→ pyproj-Version prüfen
→ proj.db prüfen
→ EPSG:4979 und EPSG:4978 prüfen
→ Transformer auswählen und Roundtrip testen
→ Earth-Grid-Frame erzeugen und Roundtrip testen
→ Readinessstatus speichern/ausgeben
```

---

## 14. Earth-World-Provisioning

Außerhalb dieses Packages:

```text
Request enthält globale Koordinate + explizites CRS
→ resolve_crs()
→ GlobalCoordinate
→ EarthGridReference
→ GlobalReferencePoint
→ to_persistence_dict()
→ atomar an WorldInstance speichern
```

Danach:

```text
resolve_earth_grid_frame(reference)
→ abgeleiteten Frame cachen
```

---

## 15. Globaler Spawninput

```text
globale Spawnkoordinate
→ explizites Source-CRS
→ global_to_local(frame, coordinate, source_crs)
→ LocalEarthPosition
→ in lokale metrische Position umrechnen
→ lokal an WorldInstance speichern
```

Der globale Input wird nicht dauerhaft als redundante Entitykoordinate
gespeichert.

---

## 16. Lokale Position global anzeigen

```text
lokale Position
→ local_to_global(frame, position, target_crs)
→ kanonische EPSG:4979-Koordinate
→ optional Ziel-CRS
→ nur als abgeleitete Ausgabe
```

---

## 17. Referenzänderung

Dieses Package stellt Verträge und Fehler bereit.

Die eigentliche Sperrentscheidung liegt in einer höheren Schicht:

```text
Materialisierung vorhanden?
├── nein
│   └── Referenzänderung eventuell zulässig
└── ja
    └── WorldReferenceLockedError
```

Materialisierungsgründe können sein:

```text
ChunkSnapshots
ChunkEvents
BlockCommands
Objekte
Spawn
Spielerzustand
```

---

# Teil VII – Caches und Threads

## 18. Cacheübersicht

```text
Paketfassade
├── Lazy-Symbole
└── Lazy-Module

CRS
├── native CRS = 256
├── EPSG:4979 = 1
└── EPSG:4978 = 1

Transformer
└── pro Thread = 64 native Transformer

Earth-Grid
├── Definitionen = 64
└── Frames = 512
```

---

## 19. Cache-Wahrheit

```text
Cache
→ reproduzierbare Performanceoptimierung

GlobalReferencePoint
→ Persistenzwahrheit

CrsDefinition im GlobalReferencePoint
→ reproduzierbare CRS-Wahrheit

EarthGridFrame
→ abgeleitete Runtimeansicht
```

Ein Cachemiss darf jederzeit zu demselben fachlichen Ergebnis führen.

---

## 20. Multi-Worker-Betrieb

Jeder Gunicorn-/Python-Prozess besitzt eigene:

```text
Lazy-Caches
CRS-Caches
Transformer-Metriken
Earth-Grid-Caches
```

Caches und Metriken sind daher:

```text
prozesslokal
nicht clusterweit
nicht Datenwahrheit
```

---

## 21. Threadmodell

```text
CrsDefinition und Vertragsobjekte
→ immutable und threadübergreifend sicher weitergebbar

TransformerSelection
→ enthält keine native Instanz
→ threadübergreifend serialisierbar

native pyproj.Transformer
→ ausschließlich aktueller Thread

EarthGridFrame
→ immutable
→ hängt von immutable Topologie und Verträgen ab
```

---

# Teil VIII – Sicherheit und Reproduzierbarkeit

## 22. Netzwerk

Standard:

```text
PROJ-Netzwerk deaktiviert
```

Keine automatischen Grid-Downloads.

Deployment muss benötigte Ressourcen im Image oder lokalen Dateisystem
bereitstellen.

---

## 23. CRS-Persistenz

Kanonisches Format:

```text
WKT2:2019
```

Authority-ID allein reicht nicht als vollständige reproduzierbare Definition.

Persistiert werden:

```text
CRS-ID
Authority/Code
kanonisches WKT
Definitionsfingerprint
Dimension
Klassifikation
```

---

## 24. Sensitive Diagnosedaten

Standardausgaben vermeiden:

```text
vollständige WKT
vollständige PROJ-Pipelines
vollständige Grid-URLs
vollständige PROJ-Datenpfade
repr() unerwarteter Exceptions
```

Stattdessen:

```text
Länge
Typ
Hash/Fingerprint
sicherer Dateipfadfingerprint
Ursachentyp
```

---

## 25. Inputgrenzen

Große Eingaben werden begrenzt:

```text
CRS-Definitionen
CRS-Mappings
Transformationspipelines
Gridlisten
AOI-Felder
Batchgrößen
Decimalstellen
Exponenten
Identifier
```

Dies schützt gegen:

```text
unkontrollierte Speicherbelegung
tiefe Rekursion
nicht endliche Zahlen
extreme Integer
unbounded Diagnosepayloads
```

---

# Teil IX – Integrationsgrenzen

## 26. Abhängigkeit zu `src/coordinates`

Benötigt werden unter anderem:

```text
CoordinateError
JsonValue
AxisConvention
LocalBlockPosition
LocalMetricPosition
int64-Grenzen
PeriodicXTopology
NorthSouthPolicy
get_periodic_x_topology()
```

`src/georeferencing` setzt voraus, dass diese Verträge stabil sind.

Änderungen an:

```text
Achsenkonvention
PeriodicXTopology-Gleichheit
Z-Grenzsemantik
LocalMetricPosition
CoordinateError-Konstruktor
with_context()
```

müssen gemeinsam getestet werden.

---

## 27. Abhängigkeit zu `src/world/earth`

`src/world/earth` verwendet:

```text
GlobalReferencePoint
TransformationPolicy
TransformerSelectionOptions
EarthGridDefinition
EarthGridFrame
global_to_local
local_to_global
resolve_earth_grid_frame
```

Der Worldprovider übernimmt:

```text
WorldInstance-Identität
Referenzsperre
Chunknormalisierung
Spawnlogik
Providercache
```

---

## 28. Abhängigkeit zu Models

Die Model-/Repositoryschicht muss speichern:

```text
GlobalReferencePoint.to_persistence_dict()
reference_version
lokale Spawnposition
lokale Objekt-/Spieler-/Chunkzustände
```

Sie muss verhindern:

```text
mehr als einen Referenzdatensatz pro Earth-WorldInstance
stille Referenzänderung nach Materialisierung
Revision-Lost-Update
unvollständige CRS-Definition
```

---

## 29. Abhängigkeit zu Routes

Routen sollen:

```text
CRS explizit verlangen
resolve_crs() verwenden
Domänenfehler in HTTP übersetzen
keine pyproj-Objekte serialisieren
keine PROJ-Netzwerkpolicy pro Request ändern
keine vollständigen Pipelines ausgeben
```

---

## 30. Abhängigkeit zu Persistenz- und Commandpfaden

Vor jedem Earth-Read/Write:

```text
WorldInstance auflösen
→ GlobalReferencePoint laden
→ EarthGridFrame/Provider auflösen
→ lokale Position oder Chunkadresse kanonisieren
→ erst danach Datenbank-Key bilden
```

Das Georeferenzierungspackage selbst schreibt keine Snapshots oder Commands.

---

# Teil X – Harte Invarianten

## 31. Paketweite Invarianten

```text
1. Das Package ist frameworkunabhängig.

2. pyproj wird spät und kontrolliert geladen.

3. PROJ-Netzwerk wird nicht beim Import verändert.

4. Earth v1 verwendet always_xy=True.

5. CRS wird nie aus Koordinatenzahlen geraten.

6. Persistierbare CRS werden als WKT2:2019 kanonisiert.

7. Lange CRS-Definitionen werden in normalen Diagnosen nicht vollständig ausgegeben.

8. Ballpark-Transformationen sind standardmäßig verboten.

9. best_available ist standardmäßig erforderlich.

10. Roundtripvalidierung ist standardmäßig aktiv.

11. Standard-Roundtripgrenze ist 0,001 m.

12. Fehlende Grids werden nicht automatisch heruntergeladen.

13. Native Transformer bleiben threadlokal.

14. Genau ein GlobalReferencePoint ist Persistenzwahrheit je Earth-WorldInstance.

15. Koordinatenwerte werden intern als Decimal normalisiert.

16. EarthGridFrame ist abgeleitet und nicht Persistenzwahrheit.

17. Storage-Origin ist global chunk-aligniert.

18. Alle Earth-Projekte verwenden dieselbe Rasterphase.

19. X ist periodisch.

20. X-Zielbereich ist halb-offen zentriert.

21. Pole sind nicht adressierbar.

22. Z ist begrenzt und nicht periodisch.

23. Y kann lokal bestehen, auch wenn globale Höhe ungelöst ist.

24. Ungelöstes lokales Y darf nicht still als globale Höhe ausgegeben werden.

25. Cache-Reset verändert keine persistierten Daten.

26. Native CRS- und Transformerobjekte werden nicht persistiert.

27. vollständige Pipelines werden standardmäßig nicht serialisiert.

28. Identity-Transformationen benötigen keinen nativen Transformer.

29. Ergebnisverträge validieren die beobachtete Genauigkeit erneut.

30. Revisions- und Referenzkonflikte werden als 409-nahe Domänenfehler modelliert.
```

---

# Teil XI – Tatsächlich geprüfte Beispiele

## 32. CRS-Auflösung

Bestätigt:

```text
4979
("EPSG", "4979")
"EPSG:4979"
bestehende CrsDefinition
```

erzeugen jeweils:

```text
crsId = EPSG:4979
Dimension = 3
geografisch = true
```

---

## 33. Geografisch → geozentrisch

Eingabe:

```text
Longitude = 11,576
Latitude  = 48,137
Höhe      = 560 m
Source    = EPSG:4979
Target    = EPSG:4978
```

Beispielresultat:

```text
X ≈ 4.178.011,037 m
Y ≈   855.798,834 m
Z ≈ 4.727.472,880 m
```

Roundtripfehler:

```text
≈ 0,00000000479 m
```

---

## 34. Referenzframe

Beispielreferenz:

```text
11,576°
48,137°
560 m
```

Abgeleiteter Storage-Origin:

```text
x = 1.286.208
y = 560
z = 5.348.544
```

Exakte lokale Referenzposition:

```text
x ≈ 14,222222 Zellen
y = 0
z ≈ 11,555556 Zellen
```

Damit bleibt der genaue Referenzpunkt erhalten, obwohl der Storage-Origin auf
Chunkgrenzen gesnappt ist.

---

## 35. 2D-Referenz

Beispiel:

```text
Longitude = 179,999999°
Latitude  = 0°
keine Höhe
```

Ergebnis:

```text
verticalResolved = false
Storage-Origin Y = 0
lokales Referenz-Y = null
2D-Rücktransformation erfolgreich
```

---

## 36. X-Naht

```text
179,999° → -179,999°
```

wird über die kurze periodische Distanz abgebildet.

---

## 37. Pole

```text
+90° → abgelehnt
-90° → abgelehnt
```

Werte innerhalb der Epsilon-Grenze werden akzeptiert.

---

# Teil XII – Technische Restpunkte und Risiken

## 38. Vollständige Integration mit `src/coordinates` separat prüfen

Der isolierte Lauf verwendete einen minimalen Vertragsstub.

Noch separat zu bestätigen:

```text
echter CoordinateError-Konstruktor
with_context()-Semantik
echte PeriodicXTopology-Gleichheit
echte NormalizedBlockPosition
int64-Verträge
LocalMetricPosition-Validierung
Cacheverhalten des Coordinates-Pakets
```

---

## 39. Globale PROJ-Netzwerkmutation

`configure_proj_network()` verändert globalen Prozesszustand.

Risiko bei falscher Verwendung:

```text
Request A aktiviert Netzwerk
Request B erwartet deaktiviertes Netzwerk
```

Regel:

```text
nur kontrolliert beim Startup konfigurieren
```

---

## 40. Persistenzkardinalität liegt außerhalb

`GlobalReferencePoint` ist immutable, verhindert aber nicht mehrere
Datenbankzeilen.

Benötigt:

```text
Unique-Constraint oder genau ein JSON-Feld auf WorldInstance
atomarer Upsert
Konfliktvergleich über Fingerprint
```

---

## 41. Referenzsperre liegt außerhalb

Fehlerklassen sind vorhanden.

Die Materialisierungsprüfung muss aber in:

```text
World-Service
Repository
Provisioning
Earth-Provider
```

konsistent erfolgen.

---

## 42. CRS-Importfehler wird pro Prozess gemerkt

Nach einem fehlgeschlagenen pyproj-Import:

```text
_PYPROJ_IMPORT_ERROR
```

wird der Fehler für weitere Aufrufe wiederverwendet.

Ein späteres dynamisches Installieren von pyproj im selben Prozess wird nicht
erkannt.

Lösung in Entwicklung:

```text
Prozess neu starten
```

---

## 43. Cache-Reset ist nicht vollständiger Runtime-Reset

`clear_georeferencing_caches()` setzt nicht zurück:

```text
PROJ-Netzwerk
pyproj-Modul
pyproj-Importfehler
sys.modules
alle kumulativen Transformationsmetriken
```

Der Funktionsname bedeutet:

```text
reproduzierbare fachliche In-Process-Caches leeren
```

nicht:

```text
native Runtime vollständig deinitialisieren
```

---

## 44. Native Achsenmetadaten können verwirren

`CrsInspection` meldet native Achsen.

Transformationen verwenden `always_xy`.

API-Dokumentation muss deutlich machen:

```text
x/y-Eingabekonvention
≠ zwingend native axis_names-Reihenfolge
```

---

## 45. Transformationsbatch ist nicht vektorisiert

Bis zu 100.000 Einzelrequests werden nacheinander bearbeitet.

Für große Importe kann eine spätere vektorisierte API nötig sein:

```text
ein Source-/Target-/Policyvertrag
→ Arrays von X/Y/Z
→ ein nativer Transformeraufruf
```

Dabei müssen Fehleratomizität und Indexzuordnung erhalten bleiben.

---

## 46. Sehr große Diagnosepayloads möglich

Statusobjekte können enthalten:

```text
vollständige Griddefinition
Frame
Reference
Transformerdescriptor
CRS-Metadaten
Caches
```

Sie vermeiden sensible Rohdefinitionen, können aber dennoch umfangreich sein.

Produktive Statusrouten sollten:

```text
authentifiziert
intern
optional detailliert
```

sein.

---

## 47. Pipeline kann optional vollständig ausgegeben werden

Mehrere `to_dict()`-Methoden unterstützen:

```text
include_pipeline=true
```

Dies sollte nicht in öffentlichen Standardrouten aktiviert werden.

---

## 48. Equirektangulares Raster ist nicht metrisch horizontal

Ein Zellschritt in X repräsentiert global keine überall gleiche reale Distanz.

Dies muss bei:

```text
Entfernungsanzeigen
Flächen
Physik
Importauflösung
Kollaborationswerkzeugen
```

berücksichtigt werden.

---

## 49. 2D-Referenz und lokales Y

Eine 2D-Referenz erlaubt lokale Y-Werte als Weltzustand.

Sie erlaubt nicht automatisch:

```text
absolute globale Höhe
```

API-Ausgaben müssen `verticalResolved` beachten.

---

## 50. Roundtripgrenzen

Die Standardgrenze von 0,001 m ist streng.

Bei regionalen CRS mit fehlenden lokalen Grids kann dies zu kontrollierter
Ablehnung führen.

Das ist Teil des v1-Vertrags und sollte nicht still gelockert werden.

---

## 51. Authorityconfidence

Authorityauflösung verwendet standardmäßig:

```text
100
```

Custom-WKT kann dadurch eine `CUSTOM:<fingerprint>`-ID erhalten, obwohl eine
niedriger bewertete Authorityähnlichkeit existiert.

Das ist reproduzierbar und beabsichtigt.

---

# Teil XIII – Änderungsnavigation

## 52. Wo eine Änderung hingehört

| Änderung | Datei |
|---|---|
| neuer öffentlicher Export | `__init__.py` |
| neue Runtime-Diagnose | `__init__.py` oder zuständiges Modul |
| neuer Fehlercode | `errors.py` |
| neue Fehlerklasse | `errors.py` |
| neue persistierbare Vertragsstruktur | `contracts.py` |
| neue Transformationpolicy | `contracts.py` |
| neuer CRS-Eingabetyp | `crs.py` |
| neue CRS-Allowlistregel | `crs.py` |
| pyproj-/PROJ-Readiness | `crs.py` |
| Transformationsauswahl | `transformer.py` |
| Grid-/Ballpark-/Accuracyprüfung | `transformer.py` |
| Transformer-Threadcache | `transformer.py` |
| Earth-Rastergröße | `earth_grid.py` und Earth-Manifest |
| Earth-Projektionsformel | `earth_grid.py` |
| Storage-Origin-Policy | `earth_grid.py` |
| Global-/Lokalkonvertierung | `earth_grid.py` |
| periodische Integer-Chunklogik | `src/coordinates/topology.py` |
| WorldInstance-Referenzpersistenz | Model-/Repositoryschicht |
| Referenzlock | Earth-Provider/World-State-Service |
| HTTP-Fehlerabbildung | Route-/HTTP-Schicht |

---

## 53. Checkliste für neue CRS-Funktionen

```text
1. Keine direkte pyproj-Nutzung außerhalb crs.py/transformer.py einführen.

2. CRS-Eingabe muss explizit sein.

3. Keine Koordinatenwert-Heuristik verwenden.

4. CrsResolutionPolicy definieren.

5. Dimensionen begrenzen.

6. CRS-Typen explizit erlauben.

7. Authority-/ID-Allowlist bewusst setzen.

8. WKT2:2019 als Persistenzdefinition verwenden.

9. Lange Definitionen nicht standardmäßig ausgeben.

10. Cache-Key deterministisch bilden.

11. PROJ-Netzwerkpolicy respektieren.

12. lokale Grids nicht automatisch herunterladen.

13. Domänenfehler verwenden.

14. Readiness ergänzen.

15. Cache-Reset ergänzen.

16. öffentliche Lazy-Exports ergänzen.

17. diese IST-Datei aktualisieren.
```

---

## 54. Checkliste für neue Transformationen

```text
1. CoordinateTransformRequest verwenden.

2. TransformationOperationKind setzen.

3. TransformationPolicy verwenden.

4. always_xy muss true bleiben.

5. Ballparkstatus prüfen.

6. best_available prüfen.

7. fehlende Grids diagnostizieren.

8. Accuracy prüfen.

9. Roundtrip prüfen.

10. native Transformer threadlokal halten.

11. Pipeline standardmäßig nicht ausgeben.

12. Batchgrenze definieren.

13. keine Persistenz im Transformer durchführen.

14. Fehler in stabile Klassen übersetzen.

15. Cachemetriken aktualisieren.

16. Readiness-Smoke-Test ergänzen.
```

---

## 55. Checkliste für Earth-Referenzpersistenz

```text
1. Explizites CRS verlangen.

2. CRS über resolve_crs() auflösen.

3. GlobalCoordinate mit Decimalwerten erzeugen.

4. richtige EarthGridReference verwenden.

5. GlobalReferencePoint erzeugen.

6. vollständige CRS-Definition persistieren.

7. genau eine Referenz je WorldInstance erzwingen.

8. reference_version persistieren.

9. Fingerprint für idempotenten Vergleich verwenden.

10. abweichende bestehende Referenz als Konflikt behandeln.

11. Materialisierungsgründe prüfen.

12. nach Materialisierung normalen Wechsel sperren.

13. EarthGridFrame nur ableiten/cachen.

14. Storage-Origin nicht als eigenständige Wahrheit behandeln.

15. lokale Spawnposition separat speichern.

16. diese IST-Datei aktualisieren.
```

---

## 56. Empfohlene Navigationsreihenfolge

Für Gesamtverständnis:

```text
1. src/georeferencing/IST-Zustand.md
2. src/georeferencing/__init__.py
3. src/georeferencing/contracts.py
4. src/georeferencing/errors.py
5. src/georeferencing/crs.py
6. src/georeferencing/transformer.py
7. src/georeferencing/earth_grid.py
```

Für Persistenz:

```text
contracts.py
→ GlobalReferencePoint
→ CrsDefinition
→ EarthGridReference
→ Models/Repository
```

Für CRS-Probleme:

```text
errors.py
→ crs.py
→ transformer.py
```

Für globale/lokale Abbildung:

```text
earth_grid.py
→ contracts.py
→ transformer.py
→ crs.py
→ src/coordinates/topology.py
```

Für Earth-Worlds:

```text
src/world/earth/provider.py
→ src/georeferencing/earth_grid.py
→ src/georeferencing/transformer.py
→ src/georeferencing/crs.py
```

---

## 57. Gesamtbefund

Die Georeferenzierungsschicht ist kein dünner Hilfsordner, sondern ein
weitgehend geschlossener Earth-v1-Kern.

Besonders belastbar implementiert sind:

```text
immutable Domainverträge
Decimal-Normalisierung
kanonische WKT2:2019-Persistenz
explizite CRS-Policies
PROJ-Netzwerksteuerung
Readinessdiagnose
Ballpark-/Grid-/Accuracyprüfung
thread-lokale Transformer
Roundtripmessung
periodische Decimal-Kanonisierung
chunk-ausgerichteter Storage-Origin
2D-/3D-Referenzsemantik
globale/lokale Earth-Konvertierung
Cache- und Resetgrenzen
```

Isoliert erfolgreich geprüft wurden:

```text
alle öffentlichen Exporte
pyproj- und PROJ-Readiness
EPSG:4979 und EPSG:4978
CRS-Auflösung aus mehreren Eingabeformen
geografisch-geozentrischer Roundtrip
Earth-Grid-Frame
2D-Referenz
X-Naht
Polgrenzen
Transformer-Cachehit
lokal-globaler Earth-Roundtrip
```

Noch außerhalb dieses Ordners zu härten sind:

```text
genau-eine-Referenz-Datenbankconstraint
atomare Referenzprovisionierung
Referenzrevision und Lost-Update-Schutz
Materialisierungslock
Integration mit echtem Coordinates-Paket
produktive Earth-Providerauflösung
Kanonisierung vor jedem Snapshot-/Commandzugriff
authentifizierte Diagnoseendpunkte
große vektorisierte Importtransformationen
```

Die zentrale Architekturregel lautet:

```text
GlobalReferencePoint
→ einzige persistierte globale Earth-Wahrheit

CrsDefinition
→ reproduzierbarer CRS-Vertrag

EarthGridFrame
→ deterministisch abgeleitete Runtimeansicht

lokale Koordinaten
→ persistierter Weltzustand

native pyproj-Objekte und Caches
→ ausschließlich Prozessruntime
```

Damit kann der Ordner verstanden, betrieben und erweitert werden, ohne jede der
fast 10.000 Quellcodezeilen einzeln lesen zu müssen.
