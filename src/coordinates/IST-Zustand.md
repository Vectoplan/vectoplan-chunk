<!-- services/vectoplan-chunk/src/coordinates/IST-Zustand.md -->

# IST-Zustand – `services/vectoplan-chunk/src/coordinates`

## Status dieser Fassung

Stand: 2026-07-14  
Status: Vollständige Bestandsaufnahme des gemeinsamen Koordinatenkerns für `flat` und `earth`.

Diese Datei beschreibt:

```text
services/vectoplan-chunk/src/coordinates/
```

Ziel ist, dass Entwickler ohne vollständiges Lesen aller Python-Dateien erkennen können:

```text
welche Koordinatenräume und Wertobjekte existieren
wie Block-, Chunk- und Zellkoordinaten zusammenhängen
wie negative Koordinaten zerlegt werden
wie lineare Zellindizes berechnet werden
wie Flat- und Earth-Topologien voneinander getrennt sind
wie periodische X-Kanonisierung funktioniert
wie Dirty-Chunks an Chunkgrenzen und Weltgrenzen ermittelt werden
welche Caches existieren
welche Fehlercodes und Fehlergrenzen gelten
welche Teile produktiv bestätigt oder nur vorbereitet sind
wo Änderungen hingehören
```

Verwendete Statusbegriffe:

```text
implementiert
→ im vorliegenden Quellcode vorhanden

statisch geprüft
→ Python-Syntax, Klassen, Funktionen und öffentliche Symboltabellen geprüft

isoliert ausgeführt
→ die fünf Dateien als vollständiges Python-Package geladen
  und mit repräsentativen sowie Randfalltests ausgeführt

bestätigt
→ im übergeordneten Service-IST über World-, Earth-, Chunk- oder Commandpfade bestätigt

vorbereitet
→ Vertrag und Code vorhanden, aber noch nicht vollständig in jedem äußeren
  Persistence-/Routepfad verwendet
```

---

## 1. Kurzfassung

`src/coordinates/` ist der gemeinsame frameworkunabhängige Koordinatenkern des Chunk-Services.

Das Package enthält:

```text
stabile Domänenfehler
immutable Koordinaten-Wertobjekte
topologieneutrale Chunkmathematik
eine unbegrenzte Flat-Topologie
eine periodische Earth-X-Topologie
kanonische Batch-Deduplizierung
Dirty-Chunk-Berechnung
prozesslokale Berechnungs- und Strategiecaches
eine Lazy-Import- und Diagnosefassade
```

Das Package enthält nicht:

```text
keine Flask-Routen
keine SQLAlchemy-Models
keine Datenbankabfragen
keine CRS-Transformation
keine World-Providerimplementierung
keine Snapshotpersistenz
keine Events
keine Commands
keine Transaktionssteuerung
```

Zentraler Datenfluss:

```text
LocalBlockPosition
→ Topologie normalisiert providerabhängig
→ Chunkmathematik zerlegt providerneutral
→ ChunkAddress
→ LocalCellPosition
→ linearer Zellindex
```

Flat:

```text
Koordinate bleibt unverändert
→ unbegrenzte X/Y/Z-Topologie
```

Earth:

```text
X vor Key und Datenbankzugriff kanonisieren
Z gegen Nord-/Süd-Grenzen prüfen
Y lokal unbegrenzt lassen
```

---

## 2. Ordner- und Dateistruktur

```text
services/
└── vectoplan-chunk/
    └── src/
        └── coordinates/
            ├── __init__.py
            │   ├── öffentliche Lazy-Import-Fassade
            │   ├── feste Symboltabelle
            │   ├── Moduldiagnose
            │   ├── Cache-Diagnose
            │   └── kontrollierter Cache-Reset
            │
            ├── errors.py
            │   ├── CoordinateErrorCode
            │   ├── CoordinateError
            │   ├── Validierungsfehler
            │   ├── Konfigurationsfehler
            │   ├── Konfliktfehler
            │   ├── Topologiefehler
            │   ├── Adressfehler
            │   └── JSON-sichere Fehlerdetails
            │
            ├── models.py
            │   ├── Koordinatenraum-Enums
            │   ├── LocalBlockPosition
            │   ├── LocalMetricPosition
            │   ├── ChunkPosition
            │   ├── LocalCellPosition
            │   ├── ChunkAddress
            │   ├── ResolvedCellAddress
            │   ├── NormalizationMetadata
            │   ├── NormalizedBlockPosition
            │   └── NormalizedChunkAddress
            │
            ├── chunk_math.py
            │   ├── ChunkMathConfig
            │   ├── Floor-Division und Floor-Modulo
            │   ├── Achsenzerlegung
            │   ├── Block-/Chunk-/Zellumrechnung
            │   ├── lineare Zellindizes
            │   ├── Chunkgrenzen
            │   ├── Grenzoffsets
            │   └── Berechnungscaches
            │
            ├── topology.py
            │   ├── WorldTopology
            │   ├── UnboundedFlatTopology
            │   ├── PeriodicXTopology
            │   ├── NorthSouthPolicy
            │   ├── Batch-Kanonisierung
            │   ├── Nachbarchunks
            │   ├── Dirty-Chunks
            │   └── Strategiecaches
            │
            └── IST-Zustand.md
                └── diese Dokumentation
```

---

## 3. Größenordnung

Die fünf Dateien umfassen:

```text
4.505 Quellcodezeilen
139.947 Bytes
44 Top-Level-Klassen
90 Top-Level-Funktionen
81 öffentliche Lazy-Symbole
```

| Datei | Zeilen | Klassen | Top-Level-Funktionen | Hauptaufgabe |
|---|---:|---:|---:|---|
| `__init__.py` | 538 | 0 | 11 | Paketfassade und Diagnose |
| `errors.py` | 741 | 24 | 7 | Fehlerhierarchie |
| `models.py` | 1.252 | 13 | 20 | immutable Wertobjekte |
| `chunk_math.py` | 876 | 1 | 36 | topologieneutrale Mathematik |
| `topology.py` | 1.098 | 6 | 16 | Flat- und Earth-Topologien |

Alle fünf Dateien:

```text
→ syntaktisch gültig
```

Alle 81 öffentlichen Lazy-Symbole:

```text
→ isoliert erfolgreich aufgelöst
```

---

## 4. Wie diese Dokumentation erstellt wurde

Die Bestandsaufnahme wurde erzeugt durch:

```text
vollständiges Lesen der fünf Dateien
AST-Auswertung aller Klassen, Methoden, Funktionen und Konstanten
Python-Syntaxprüfung
Auswertung der öffentlichen Lazy-Symboltabelle
Import aller vier Untermodule
Auflösung aller 81 öffentlichen Symbole
Ausführung der Chunkmathematik
Ausführung negativer Koordinatenfälle
vollständiger Zellindex-Roundtrip für chunkSize 4
Flat-Topologie-Test
Earth-X-Kanonisierungstest
Earth-Antipodaltest
Batch-Deduplizierungstest
Dirty-Chunk-Test an der X-Naht
Dirty-Chunk-Test an der Nordgrenze
Cachehit-Test
Fehlerpfadtests für ungültige Zellindizes und Chunk-Keys
```

---

## 5. Isolierter Prüfstatus

Paket:

```text
Package-ID         = vectoplan-coordinates
Modulversion       = 1.0.0
öffentliche API    = coordinates-api.v1

bekannte Module    = 4
bereite Module     = 4
öffentliche Symbole = 81
auflösbare Symbole  = 81
```

Bestätigte Kernfälle:

```text
0   → Chunk 0, lokale Zelle 0
15  → Chunk 0, lokale Zelle 15
16  → Chunk 1, lokale Zelle 0

-1  → Chunk -1, lokale Zelle 15
-16 → Chunk -1, lokale Zelle 0
-17 → Chunk -2, lokale Zelle 15
```

Blockroundtrip:

```text
Block
→ Chunk + LocalCell
→ Block

für positive und negative Testwerte exakt identisch
```

Linearer Zellindex:

```text
für alle 64 Zellen eines 4³-Chunks vollständig invertierbar
```

---

# Teil I – Paketfassade

## 6. `__init__.py`

## 6.1 Paketmetadaten

```text
MODULE_VERSION     = 1.0.0
PACKAGE_ID         = vectoplan-coordinates
PUBLIC_API_VERSION = coordinates-api.v1
```

---

## 6.2 Aufgabe

Das Package stellt stabile öffentliche Imports bereit:

```python
from src.coordinates import (
    LocalBlockPosition,
    resolve_block_address,
    get_periodic_x_topology,
)
```

Ein normaler Import lädt die Untermodule erst bei Bedarf.

---

## 6.3 Öffentliche Symbolgruppen

Fehler:

```text
25 Symbole
```

Modelle und Typen:

```text
22 Symbole
```

Chunkmathematik:

```text
24 Symbole
```

Topologien:

```text
10 Symbole
```

Gesamt:

```text
81 Symbole
```

---

## 6.4 Bekannte Untermodule

```text
.errors
.models
.chunk_math
.topology
```

Nur diese relativen Module dürfen über die interne Ladefunktion geladen werden.

---

## 6.5 Lazy-Caches

```text
_SYMBOL_CACHE
→ bereits aufgelöste öffentliche Symbole

_MODULE_CACHE
→ bereits importierte Untermodule

_STATE_LOCK
→ thread-sicherer Zugriff
```

Ein unbekannter Name:

```text
→ AttributeError
```

Ein eingetragener, aber im Zielmodul fehlender Name:

```text
→ ImportError
```

Fehler werden nicht in `None` oder Platzhalter übersetzt.

---

## 6.6 Preloading

```python
preload_coordinate_modules(strict=True)
```

Ablauf:

```text
alle vier Module laden
→ Status je Modul sammeln
→ bei strict=true ersten Fehler nach vollständiger Aufnahme erneut auslösen
```

---

## 6.7 Paketdiagnose

```python
coordinate_module_status(
    preload=False,
    include_cache_info=True,
)
```

Liefert:

```text
Paketidentität
Modulversion
API-Version
Lazy-Importstatus
öffentliche Symbolanzahl
geladene Symbole
bekannte Module
geladene Module
Modulstatus
Cacheinformationen
```

Beachtung:

```text
include_cache_info=true
→ lädt chunk_math und topology bei Bedarf
```

Damit kann ein Statusaufruf mit `preload=false` trotzdem diese beiden Module
importieren.

---

## 6.8 Cache-Diagnose

```python
coordinate_cache_info()
```

enthält:

```text
Lazy-Symbolcache
Lazy-Modulcache
Chunk-Math-Caches
Topologie-Caches
```

---

## 6.9 Cache-Reset

```python
clear_coordinate_caches()
```

leert:

```text
chunk_math
topology
```

Nicht geleert werden:

```text
Lazy-Symbole
Lazy-Module
sys.modules
Klassenidentitäten
persistierte Daten
```

---

## 6.10 Paketreset

```python
reset_coordinate_package_state(
    clear_runtime_caches=True,
    clear_lazy_symbols=False,
)
```

Option:

```text
clear_lazy_symbols=true
→ geladene Symbole aus Package-Namespace entfernen
→ Module bleiben in sys.modules
```

Standardmäßig bleiben Symbolidentitäten stabil.

---

# Teil II – Fehlerbasis

## 7. `errors.py`

## 7.1 Aufgabe

Die Datei bildet die gemeinsame Domainfehlerbasis für:

```text
Chunkmathematik
Koordinatenmodelle
Topologien
Georeferenzierung
World-Provider
Application- und HTTP-Adapter
```

Sie kennt nicht:

```text
Flask
SQLAlchemy
Logging
Rollback
HTTP-Responseobjekte
```

---

## 7.2 `CoordinateErrorCode`

Allgemein:

```text
coordinate_error
coordinate_validation_failed
coordinate_computation_failed
coordinate_configuration_invalid
coordinate_conflict
```

Konfiguration und Topologie:

```text
invalid_chunk_size
world_width_invalid
world_width_not_chunk_aligned
half_world_not_chunk_aligned
world_height_invalid
invalid_topology_configuration
unsupported_wrap_axis
topology_not_resolved
```

Grenzen und Wertebereiche:

```text
coordinate_out_of_bounds
north_south_boundary_exceeded
coordinate_overflow
coordinate_space_mismatch
coordinate_dimension_mismatch
coordinate_precision_loss
```

Adressen:

```text
chunk_address_invalid
chunk_address_noncanonical
cell_address_invalid
ambiguous_antipodal_coordinate
```

Veröffentlichte Codes dürfen nicht:

```text
umbenannt
für andere Bedeutungen wiederverwendet
```

werden.

---

## 7.3 `CoordinateError`

Basisklasse aller erwartbaren Koordinatenfehler.

Felder:

```text
message
code
details
http_status
retryable
cause
```

Standard:

```text
HTTP 422
retryable = false
```

Methoden:

```text
to_dict()
to_problem_details()
with_context()
```

---

## 7.4 Fehlerausgabe

Normale Darstellung:

```json
{
  "ok": false,
  "error": {
    "code": "coordinate_validation_failed",
    "message": "...",
    "details": {},
    "retryable": false
  }
}
```

RFC-7807-nahe Darstellung:

```text
title
status
detail
code
retryable
details
instance
```

Die Domain erzeugt bewusst keine feste `type`-URL.

---

## 7.5 Fehlerbasisklassen

| Klasse | Status | Bedeutung |
|---|---:|---|
| `CoordinateValidationError` | 422 | externe oder fachliche Eingabe ungültig |
| `CoordinateConfigurationError` | 500 | Grid-/Topologiekonfiguration ungültig |
| `CoordinateComputationError` | 500 | unerwarteter technischer Fehler |
| `CoordinateConflictError` | 409 | Konflikt mit kanonischem Zustand |

---

## 7.6 Spezifische Fehlerklassen

Chunkgröße und Weltgeometrie:

```text
InvalidChunkSizeError
WorldWidthInvalidError
WorldWidthNotChunkAlignedError
HalfWorldNotChunkAlignedError
WorldHeightInvalidError
InvalidTopologyConfigurationError
```

Topologie:

```text
UnsupportedWrapAxisError
TopologyNotResolvedError
NorthSouthBoundaryExceededError
AmbiguousAntipodalCoordinateError
```

Koordinaten:

```text
CoordinateOutOfBoundsError
CoordinateOverflowError
CoordinateSpaceMismatchError
CoordinateDimensionMismatchError
CoordinatePrecisionLossError
```

Adressen:

```text
ChunkAddressInvalidError
ChunkAddressNonCanonicalError
CellAddressInvalidError
```

---

## 7.7 Fehlergrenze

```python
ensure_coordinate_error(
    error,
    operation=...,
    details=...,
)
```

Verhalten:

```text
bekannter CoordinateError
→ unverändert

unerwartete Exception
→ CoordinateComputationError
→ Ursache nur intern als cause
→ außen nur causeType
```

---

## 7.8 JSON-Sicherheit

Fehlerdetails werden defensiv konvertiert.

Unterstützt:

```text
null
String
Integer
Float
Boolean
Enum
Mapping
Sequenz
```

Unbekannte Objekte:

```json
{
  "type": "Klassenname"
}
```

Maximale Rekursionstiefe:

```text
8
```

Es wird kein beliebiges `repr()` ausgegeben.

---

## 7.9 Konkreter Fehler: `with_context()`

`CoordinateError.with_context()` versucht, denselben Fehlertyp mit einem
generischen Konstruktor neu zu erzeugen:

```text
message
code
details
http_status
retryable
cause
```

Viele spezialisierte Fehlerklassen überschreiben jedoch `__init__()` und
akzeptieren diese Schlüsselargumente nicht.

Betroffen sind unter anderem:

```text
CellAddressInvalidError
ChunkAddressInvalidError
InvalidChunkSizeError
CoordinateOverflowError
WorldWidthInvalidError
weitere spezialisierte Klassen
```

Aktuelle Aufrufstellen in `models.py`:

```text
LocalCellPosition.from_linear_index()
ChunkAddress.__post_init__()
ChunkAddress.parse()
ChunkAddress.from_mapping()
ResolvedCellAddress.__post_init__()
```

Reproduzierte Ergebnisse:

```text
LocalCellPosition.from_linear_index(64, chunk_size=4)
→ erwartet CellAddressInvalidError
→ tatsächlich TypeError:
  unexpected keyword argument 'code'

ChunkAddress.parse("1:2")
→ erwartet ChunkAddressInvalidError
→ tatsächlich TypeError

ChunkAddress(ChunkPosition(1,2,3), "9:9:9")
→ erwartet ChunkAddressInvalidError
→ tatsächlich TypeError
```

Folgen:

```text
stabiler Domänenfehlercode geht verloren
HTTP-Adapter erhält technischen TypeError
Fehlerdetails und Statussemantik gehen verloren
Tests können falschen Fehlertyp beobachten
```

Empfohlene Korrektur:

```text
Variante A
→ with_context() klont ein bestehendes Fehlerobjekt,
  ohne den spezialisierten __init__ erneut aufzurufen

Variante B
→ alle spezialisierten Konstruktoren akzeptieren einen internen
  generischen Rekonstruktionsvertrag

Variante C
→ Zusatzdetails bereits beim ursprünglichen Konstruktor übergeben
  und with_context an diesen Stellen vermeiden
```

Eine zentrale Lösung in `CoordinateError` ist vorzuziehen.

---

## 7.10 Weitere Fehlerpayload-Grenze

Mappings und Sequenzen werden bis Tiefe 8 konvertiert, aber nicht nach
Elementanzahl begrenzt.

Sehr große intern übergebene Details können dadurch große Fehlerpayloads
erzeugen.

Spätere Härtung:

```text
maximale Mappingelemente
maximale Sequenzelemente
maximale Stringlänge
nicht endliche Floatwerte ablehnen oder markieren
```

---

# Teil III – Wertobjekte

## 8. `models.py`

## 8.1 Grundregel

Alle Positions- und Adressmodelle sind:

```text
frozen
slots-basiert
frameworkunabhängig
```

Sie besitzen keine:

```text
Datenbank-ID
Session
Providerreferenz
CRS-Logik
Cachelogik
```

---

## 8.2 Typaliase und Grenzen

```text
JsonPrimitive
JsonValue
IntegerTriple
NumberTriple
```

Ganzzahlgrenzen:

```text
SIGNED_INT32_MIN = -2³¹
SIGNED_INT32_MAX =  2³¹ - 1

SIGNED_INT64_MIN = -2⁶³
SIGNED_INT64_MAX =  2⁶³ - 1
```

---

## 8.3 `CoordinateSpace`

```text
local_world
local_block
local_metric
chunk
local_cell
earth_grid
```

---

## 8.4 `CoordinateAxis`

```text
x
y
z
```

---

## 8.5 `AxisConvention`

Aktuell:

```text
x-east-y-up-z-north
```

Flat und Earth verwenden dieselbe Achsenkonvention.

---

## 8.6 `NormalizationReason`

```text
none
periodic_wrap
antipodal_canonicalization
multiple
```

---

## 8.7 `LocalBlockPosition`

Ganzzahlige Blockposition relativ zum Ursprung einer konkreten WorldInstance.

```text
x: int64
y: int64
z: int64
```

Coordinate Space:

```text
local_block
```

Methoden:

```text
origin()
from_mapping()
from_sequence()
as_tuple()
to_dict()
translated()
difference_to()
```

`translated()` und `difference_to()` prüfen int64-Überlauf.

---

## 8.8 `LocalMetricPosition`

Sub-Block-Position für:

```text
Spieler
Kamera
bewegliche Objekte
präzisen Spawn
```

Felder:

```text
x: endlicher Float
y: endlicher Float
z: endlicher Float
```

Coordinate Space:

```text
local_metric
```

NaN und Infinity werden abgelehnt.

---

## 8.9 `ChunkPosition`

Ganzzahlige lokale Chunkposition.

```text
x: int64
y: int64
z: int64
```

Coordinate Space:

```text
chunk
```

Mappingfelder:

```text
x / chunkX
y / chunkY
z / chunkZ
```

Wenn beide Varianten vorhanden sind, müssen sie identisch sein.

Die Dataclass ist sortierbar.

---

## 8.10 `LocalCellPosition`

Lokale Zellposition innerhalb eines Chunks.

```text
x: int32
y: int32
z: int32
```

Coordinate Space:

```text
local_cell
```

Die int32-Grenze allein bedeutet noch nicht, dass die Zelle in einem konkreten
Chunk liegt.

Dafür:

```python
validate_for_chunk_size(chunk_size)
```

Regel:

```text
0 <= x,y,z < chunk_size
```

---

## 8.11 Linearer Zellindex

```python
to_linear_index(chunk_size)
```

Formel:

```text
index =
    x
    + y * chunk_size
    + z * chunk_size²
```

Äquivalent:

```text
x + chunkSize * (y + chunkSize * z)
```

Reihenfolge:

```text
X am schnellsten
danach Y
danach Z
```

Inverse:

```python
LocalCellPosition.from_linear_index(
    index,
    chunk_size=...,
)
```

---

## 8.12 Zellindex-Beispiele bei Chunkgröße 16

```text
(0,0,0)   → 0
(15,0,0)  → 15
(0,1,0)   → 16
(0,0,1)   → 256
(15,15,15) → 4095
```

---

## 8.13 `ChunkAddress`

Verbindet:

```text
ChunkPosition
kanonischen Chunk-Key
```

Keyformat:

```text
x:y:z
```

Beispiele:

```text
0:0:0
-1:2:-3
1249999:0:624999
```

Methoden:

```text
from_position()
from_coordinates()
parse()
from_mapping()
format_key()
to_dict()
```

Properties:

```text
x
y
z
```

Die Dataclass ist:

```text
frozen
slots-basiert
sortierbar
hashbar
```

Sie kann daher in Sets und stabil sortierten Dirty-Chunk-Ergebnissen verwendet
werden.

---

## 8.14 Striktes Chunk-Key-Format

Erlaubt:

```text
0
-1
123
```

Nicht kanonisch:

```text
+1
01
-0
Leerzeichen innerhalb des Zahltexts
mehr oder weniger als drei Teile
```

`ChunkAddress.parse()` akzeptiert ausschließlich das kanonische Format.

Aktuelle Fehlercode-Unschärfe:

```text
falsche Teilanzahl
→ soll ChunkAddressInvalidError sein
→ wird derzeit wegen with_context-Bug zu TypeError

nicht kanonische Einzelzahl wie "01"
→ CoordinateValidationError
→ nicht ChunkAddressInvalidError
```

---

## 8.15 Mappingauflösung

`ChunkAddress.from_mapping()` unterstützt:

```text
nur chunkKey
nur Komponenten
chunkKey plus Komponenten
```

Bei Key plus Komponenten:

```text
beide Darstellungen müssen identisch sein
```

---

## 8.16 `ResolvedCellAddress`

Vollständig aufgelöste Blockadresse:

```text
block: LocalBlockPosition
chunk: ChunkAddress
cell: LocalCellPosition
linear_index
chunk_size
```

Beim Erzeugen wird geprüft:

```text
cell liegt im Chunk
linear_index entspricht cell
chunk_size ist positiv
```

---

## 8.17 `NormalizationMetadata`

Felder:

```text
changed
reason
wrapped_axes
wrap_count_x
antipodal_canonicalized
```

Konsistenz:

```text
changed=false
→ reason=none
→ keine wrappedAxes
→ wrapCountX=0
→ antipodal=false

changed=true
→ konkreter reason erforderlich
```

Factorys:

```text
unchanged()
periodic_x()
```

---

## 8.18 `NormalizedBlockPosition`

```text
requested
canonical
metadata
```

Prüft:

```text
requested != canonical
genau dann, wenn metadata.changed=true
```

---

## 8.19 `NormalizedChunkAddress`

```text
requested
canonical
metadata
```

Dieselbe Konsistenzregel wie bei Blockpositionen.

Diese Struktur ist wichtig für:

```text
API-Diagnose
Aliasbehandlung
Snapshot-Key-Auswahl
Batch-Deduplizierung
Wrapmetriken
```

---

# Teil IV – Chunkmathematik

## 9. `chunk_math.py`

## 9.1 Aufgabe

Die Datei enthält ausschließlich topologieneutrale Mathematik.

Nicht enthalten:

```text
kein Earth-Wrap
keine Z-Weltgrenze
keine Providerentscheidung
kein CRS
keine Datenbank
```

---

## 9.2 Standardwerte und Cachegrößen

```text
DEFAULT_CHUNK_SIZE = 16

ChunkMathConfig-Cache = 64
Achsen-Split-Cache    = 32.768
Achsen-Join-Cache     = 32.768
Blockauflösungs-Cache = 16.384
Chunk-Origin-Cache    = 16.384
```

---

## 9.3 `ChunkMathConfig`

Immutable Konfiguration eines kubischen Chunkrasters.

Feld:

```text
chunk_size
```

Properties:

```text
cell_count = chunk_size³
maximum_linear_index = cell_count - 1
```

Methoden:

```text
split_axis()
join_axis()
block_to_chunk()
block_to_cell()
resolve()
compose()
chunk_origin()
chunk_bounds()
to_dict()
```

---

## 9.4 Chunkgrößenvalidierung

```python
validate_chunk_size(chunk_size)
```

Regeln:

```text
strikter Integer
bool verboten
> 0
<= int64
chunk_size³ <= int64
```

---

## 9.5 Zellanzahl

```python
checked_cell_count(chunk_size)
```

berechnet:

```text
chunk_size³
```

mit int64-Überlaufschutz.

Für Standardgröße 16:

```text
4096 Zellen
maximaler Index 4095
```

---

## 9.6 Floor-Division

```python
floor_divide(value, divisor)
```

verwendet mathematische Floor-Semantik.

Bei positivem Divisor:

```text
-1 // 16  = -1
-16 // 16 = -1
-17 // 16 = -2
```

Nicht verwendet wird:

```text
Truncation toward zero
```

---

## 9.7 Floor-Modulo

```python
floor_modulo(value, divisor)
```

garantiert:

```text
0 <= result < divisor
```

Beispiele:

```text
-1 % 16  = 15
-16 % 16 = 0
-17 % 16 = 15
```

---

## 9.8 Achsenzerlegung

```python
split_axis(value, chunk_size=16)
```

liefert:

```text
(chunk_coordinate, local_coordinate)
```

Beispiele:

| Weltwert | Chunk | Lokal |
|---:|---:|---:|
| 0 | 0 | 0 |
| 15 | 0 | 15 |
| 16 | 1 | 0 |
| -1 | -1 | 15 |
| -16 | -1 | 0 |
| -17 | -2 | 15 |

Algebraische Regel:

```text
value =
    chunk_coordinate * chunk_size
    + local_coordinate
```

---

## 9.9 Achsenzusammenführung

```python
join_axis(
    chunk_coordinate=...,
    local_coordinate=...,
    chunk_size=...,
)
```

Lokale Koordinate:

```text
0 <= local < chunk_size
```

Ergebnis wird auf int64 geprüft.

---

## 9.10 Block → Chunk

```python
block_to_chunk_position(position)
```

wendet `split_axis()` auf X, Y und Z an.

---

## 9.11 Block → lokale Zelle

```python
block_to_local_cell_position(position)
```

liefert immer:

```text
0 <= localX/Y/Z < chunk_size
```

auch bei negativen Blockkoordinaten.

---

## 9.12 Vollständige Blockauflösung

```python
resolve_block_address(position)
```

erzeugt:

```text
LocalBlockPosition
ChunkAddress
LocalCellPosition
linearIndex
chunkSize
```

Beispiel:

```text
Block (-17, 33, -32)
→ Chunk (-2, 2, -2)
→ Zelle (15, 1, 0)
→ Index 31
```

---

## 9.13 Rückrechnung

```python
chunk_cell_to_block_position(chunk, cell)
```

ist die exakte Inverse der Auflösung.

Isoliert bestätigt für:

```text
positive Werte
negative Werte
Chunkgrenzen
mehrere Achsen
```

---

## 9.14 Chunk-Origin

```python
chunk_to_block_origin(chunk)
```

Beispiel:

```text
Chunk (-2,2,-2), Größe 16
→ Block-Origin (-32,32,-32)
```

---

## 9.15 Chunkgrenzen

```python
chunk_block_bounds(chunk)
```

liefert inklusive:

```text
Minimumblock
Maximumblock
```

Maximum:

```text
origin + chunk_size - 1
```

---

## 9.16 Zugehörigkeitsprüfungen

```text
chunk_contains_block()
same_chunk()
```

Beide verwenden dieselbe validierte Floor-Divisionslogik.

---

## 9.17 Indexkonvertierung

```text
linear_index_to_cell()
cell_to_linear_index()
```

Isoliert vollständig für `chunk_size=4` geprüft:

```text
64 von 64 Zellen
→ Hin- und Rückrichtung identisch
```

---

## 9.18 Zelliteration

```python
iter_chunk_cells()
```

Reihenfolge:

```text
for z
    for y
        for x
```

Damit ändert sich X am schnellsten.

---

## 9.19 Blockiteration

```python
iter_chunk_blocks(chunk)
```

iteriert die Blöcke in derselben Reihenfolge wie lineare Zellindizes.

---

## 9.20 Grenzoffsets

```python
boundary_offsets_for_cell(
    cell,
    include_diagonal_combinations=True,
    include_current_chunk=True,
)
```

Die Funktion erzeugt relative Chunkoffsets.

Sie kennt noch keine Welt-Topologie.

Isolierte Ergebnisse bei Chunkgröße 4:

| Zelllage | Offsets mit Diagonalen |
|---|---:|
| Innenzelle | 1 |
| Fläche | 2 |
| Kante | 4 |
| Ecke | 8 |
| Ecke ohne Diagonalen | 4 |

Ecke ohne Diagonalen:

```text
aktueller Chunk
+ drei flächenadjazente Nachbarn
```

---

## 9.21 Chunkoffset

```python
apply_chunk_offset(chunk, offset)
```

addiert drei Chunkachsen mit int64-Überlaufschutz.

Die anschließende providerabhängige Kanonisierung gehört in `topology.py`.

---

## 9.22 Chunk-Math-Caches

```text
Konfiguration
Achsenzerlegung
Achsenzusammenführung
Blockauflösung
Chunk-Origin
```

Caches enthalten nur immutable beziehungsweise reine Berechnungsergebnisse.

Reset:

```python
clear_chunk_math_caches()
```

---

# Teil V – Topologien

## 10. `topology.py`

## 10.1 Aufgabe

Das Modul trennt providerabhängige Weltregeln von der gemeinsamen
Chunkmathematik.

```text
chunk_math
→ mathematische Zerlegung

topology
→ welche Koordinate physisch kanonisch ist
```

---

## 10.2 `TopologyKind`

```text
unbounded-flat-v1
periodic-x-v1
```

---

## 10.3 `NorthSouthPolicy`

```text
unbounded
bounded
```

Earth v1 verwendet:

```text
bounded
```

---

## 10.4 `CanonicalChunkBatch`

Felder:

```text
items
→ ein NormalizedChunkAddress je ursprünglicher Anfrage

unique_canonical
→ jeder physische Chunk genau einmal
→ Reihenfolge des ersten Auftretens

deduplicated_count
→ requested - unique
```

Properties:

```text
requested_count
unique_count
changed_count
```

---

## 10.5 `WorldTopology`

Abstrakter gemeinsamer Vertrag.

Abstrakte Bereiche:

```text
kind
chunk_size
wrap_axes

normalize_block_position()
normalize_chunk_address()

validate_block_position()
validate_chunk_position()

to_dict()
```

Gemeinsame Implementierungen:

```text
supports_wrap_axis()
require_wrap_axis()
resolve_block_address()
canonicalize_chunk_batch()
neighbor_chunk()
dirty_chunks_for_block()
same_physical_block()
same_physical_chunk()
```

---

## 10.6 Normalisierungsreihenfolge

```text
providerabhängige Position normalisieren
→ erst danach providerneutrale Chunk-/Zellauflösung
```

Dies ist entscheidend für Earth-Aliasadressen.

---

## 10.7 Batch-Kanonisierung

```python
canonicalize_chunk_batch(addresses)
```

Ablauf:

```text
jede Adresse normalisieren
→ Normalisierungsresultat in Eingabereihenfolge behalten
→ kanonische Adressen per Set deduplizieren
→ Reihenfolge des ersten kanonischen Auftretens behalten
```

Keine maximale Batchgröße im Koordinatenkern.

Die Grenze muss außen gesetzt werden.

---

## 10.8 Nachbarchunk

```python
neighbor_chunk(
    address,
    dx=...,
    dy=...,
    dz=...,
)
```

Ablauf:

```text
Offset anwenden
→ Topologie normalisieren
→ kanonische Adresse zurückgeben
```

Damit kann ein Earth-Ostnachbar direkt auf die Westseite kanonisiert werden.

---

## 10.9 Dirty-Chunks

```python
dirty_chunks_for_block(position)
```

Ablauf:

```text
Blockposition normalisieren
→ Chunk und lokale Zelle auflösen
→ relative Grenzoffsets berechnen
→ jeden Kandidaten topologisch normalisieren
→ nicht vorhandene Z-Nachbarn überspringen
→ kanonisch deduplizieren
→ stabil sortieren
```

Ergebnis:

```text
tuple[ChunkAddress, ...]
```

---

## 10.10 Flat-Topologie

`UnboundedFlatTopology`:

```text
keine Wrapachsen
keine X-Grenze
keine Y-Grenze
keine Z-Grenze
Positionen bleiben unverändert
```

Standard-Chunkgröße:

```text
16
```

---

## 10.11 `PeriodicXTopology`

Earth-v1-Topologie:

```text
X periodisch
Y lokal unbegrenzt
Z begrenzt oder explizit unbegrenzt
```

Felder:

```text
world_width_blocks
chunk_size
north_south_policy
minimum_z
maximum_z
```

---

## 10.12 Weltbreitenregeln

Weltbreite muss:

```text
strikter Integer
positiv
gerade
int64
durch chunk_size teilbar
halbe Breite durch chunk_size teilbar
positive gerade Chunkanzahl erzeugen
```

Dadurch liegt die Weltnaht immer auf Chunkgrenzen.

---

## 10.13 Earth-Standardbreite

Bei:

```text
world_width_blocks = 40.000.000
chunk_size = 16
```

gilt:

```text
half_world_blocks = 20.000.000
world_width_chunks = 2.500.000
half_world_chunks = 1.250.000
```

Kanonischer Blockbereich:

```text
[-20.000.000, 20.000.000)
```

Kanonischer Chunkbereich:

```text
[-1.250.000, 1.250.000)
```

---

## 10.14 Zentrierte Kanonisierung

Formel:

```text
canonical =
    ((value + half_width) % width)
    - half_width
```

Wrapanzahl:

```text
value =
    canonical
    + wrap_count * width
```

Beispiele für Block-X:

| angefordert | kanonisch | Wrapcount | antipodal |
|---:|---:|---:|---|
| -20.000.001 | 19.999.999 | -1 | nein |
| -20.000.000 | -20.000.000 | 0 | nein |
| 19.999.999 | 19.999.999 | 0 | nein |
| 20.000.000 | -20.000.000 | 1 | ja |
| 20.000.001 | -19.999.999 | 1 | nein |
| 60.000.000 | -20.000.000 | 2 | ja |

---

## 10.15 Antipodalregel

Der exakt gegenüberliegende Halbweltwert wird immer negativ gespeichert:

```text
+half_world
→ -half_world
```

Für Blöcke:

```text
20.000.000
→ -20.000.000
```

Für Chunks:

```text
1.250.000
→ -1.250.000
```

Dies verhindert zwei kanonische Darstellungen desselben physischen Punkts.

---

## 10.16 Blocknormalisierung

```python
normalize_block_position()
```

ändert nur:

```text
X
```

Y und Z bleiben erhalten.

Z wird vorher gegen die NorthSouthPolicy validiert.

---

## 10.17 Chunknormalisierung

```python
normalize_chunk_address()
```

ändert nur:

```text
chunkX
```

Der kanonische Key wird neu aus der kanonischen Position erzeugt.

---

## 10.18 Kürzeste X-Differenz

```text
shortest_block_delta_x()
shortest_chunk_delta_x()
```

geben den kürzesten signierten Abstand zurück.

Beim exakt antipodalen Abstand:

```text
+half_world
→ -half_world
```

---

## 10.19 Nord-/Süd-Grenzen

Bei `bounded` müssen vorhanden sein:

```text
minimum_z
maximum_z
```

Regeln:

```text
minimum_z <= maximum_z
minimum_z auf Chunkgrenze
maximum_z + 1 auf Chunkgrenze
maximum_z < int64_max
```

Bei Earth-v1:

```text
minimum_z = -10.000.000
maximum_z = 9.999.999
```

Chunkbereich:

```text
minimumChunkZ = -625.000
maximumChunkZ = 624.999
```

---

## 10.20 Z-Grenzfehler

Außerhalb:

```text
NorthSouthBoundaryExceededError
HTTP-Empfehlung 422
wrapApplied=false
```

Isoliert bestätigt:

```text
z = 10.000.000
→ abgelehnt
```

---

## 10.21 Unbegrenzte Z-Policy

Bei:

```text
northSouthPolicy = unbounded
```

müssen:

```text
minimum_z = null
maximum_z = null
```

sein.

Explizite Grenzen in diesem Modus sind ein Konfigurationsfehler.

---

## 10.22 Batch-Deduplizierung an der Weltnaht

Testadressen:

```text
-1.250.000:0:0
 1.250.000:0:0
 3.750.000:0:0
 0:0:0
```

Kanonische physische Chunks:

```text
-1.250.000:0:0
0:0:0
```

Ergebnis:

```text
requestedCount = 4
uniqueCount = 2
deduplicatedCount = 2
changedCount = 2
```

---

## 10.23 Physische Gleichheit

```text
same_physical_block()
same_physical_chunk()
```

Isoliert bestätigt:

```text
Block -20.000.000 und +20.000.000
→ derselbe physische Block

Chunk -1.250.000 und +1.250.000
→ derselbe physische Chunk
```

---

## 10.24 Dirty-Chunks an der X-Naht

Beispiel:

```text
Block-X = 19.999.999
```

liegt in der letzten lokalen X-Zelle des östlichsten kanonischen Chunks.

Der X-Nachbar wird kanonisiert zu:

```text
Chunk-X = -1.250.000
```

Damit enthält das Ergebnis nicht den Alias:

```text
+1.250.000
```

sondern nur kanonische Keys.

---

## 10.25 Dirty-Chunks an der Nordgrenze

Beispiel:

```text
X = 19.999.999
Y = 1
Z = 9.999.999
```

Die Zelle liegt:

```text
an der X-Weltnaht
an der maximalen Z-Grenze
```

Ergebnis:

```text
östlicher aktueller Chunk
westlich kanonisierter X-Nachbar
```

Nicht enthalten:

```text
Z+1-Nachbar
```

weil dort kein physischer Chunk existiert.

---

## 10.26 Dirty-Chunk-Sortierung

Ergebnisse werden:

```text
kanonisch
dedupliziert
als ChunkAddress sortiert
```

zurückgegeben.

Die Sortierbarkeit stammt aus der `order=True`-Dataclass von `ChunkAddress`.

---

## 10.27 Topologie-Caches

```text
UnboundedFlatTopology = 128
PeriodicXTopology     = 128
```

Cache-Key der Earth-Topologie umfasst:

```text
Weltbreite
Chunkgröße
NorthSouthPolicy
minimum_z
maximum_z
```

Positionsabhängige Resultate werden nicht global gecacht.

---

# Teil VI – Gesamtflüsse

## 11. Flat-Blockauflösung

```text
LocalBlockPosition
→ UnboundedFlatTopology.normalize_block_position()
→ unverändert
→ resolve_block_address()
→ Floor-Division je Achse
→ ChunkAddress
→ LocalCellPosition
→ linearIndex
```

---

## 12. Earth-Blockauflösung

```text
LocalBlockPosition
→ PeriodicXTopology.validate_block_position()
→ Z-Grenze prüfen
→ X kanonisieren
→ NormalizedBlockPosition
→ resolve_block_address(canonical)
→ kanonischer Chunk-Key
→ LocalCellPosition
→ linearIndex
```

---

## 13. Earth-Chunk-Lesen

Außerhalb dieses Packages:

```text
angeforderte Chunkadresse
→ PeriodicXTopology.normalize_chunk_address()
→ canonical ChunkAddress
→ erst jetzt Snapshotlookup
```

Falsch:

```text
Snapshotlookup mit Aliasadresse
→ danach normalisieren
```

---

## 14. Earth-Chunk-Schreiben

```text
Commandposition
→ Blockposition kanonisieren
→ Chunkadresse auflösen
→ kanonischen Snapshot-Key verwenden
→ Event mit kanonischem Chunk-Key schreiben
```

---

## 15. Chunkbatch

```text
Requestadressen
→ canonicalize_chunk_batch()
→ items für per-Request-Diagnose
→ unique_canonical für physische Arbeit
→ Provider-/Snapshotzugriff nur je unique_canonical
```

---

## 16. Dirty-Chunk-Reload

```text
geänderte Blockposition
→ resolve_block_address()
→ boundary_offsets_for_cell()
→ Kandidaten bilden
→ topologisch normalisieren
→ Z-Grenznachbarn überspringen
→ deduplizieren
→ sortieren
→ Editor lädt nur kanonische Dirty-Chunks neu
```

---

# Teil VII – Caches

## 17. Cacheübersicht

```text
Paketfassade
├── Lazy-Symbole: maximal 81
└── Lazy-Module: maximal 4

Chunk-Math
├── Config: 64
├── Axis-Split: 32.768
├── Axis-Join: 32.768
├── Resolve-Block: 16.384
└── Chunk-Origin: 16.384

Topologie
├── Flat: 128
└── Periodic-X: 128
```

---

## 18. Isolierter Cachetest

Nach Reset:

```text
get_chunk_math(16)
→ 1 Miss

zweiter identischer Aufruf
→ 1 Hit
→ derselbe immutable Konfigurationswert
```

Periodic-X:

```text
erster identischer Vertrag
→ 1 Miss

zweiter identischer Vertrag
→ 1 Hit
```

---

## 19. Cache-Wahrheit

```text
Caches
→ nur Performance

Koordinateninput
kanonische Topologieregel
Persistenzkey
→ fachliche Wahrheit
```

Ein Cachemiss darf das Ergebnis nicht verändern.

---

## 20. Multi-Worker-Betrieb

Jeder Python-/Gunicorn-Prozess besitzt eigene:

```text
Lazy-Caches
Chunk-Math-Caches
Topologie-Caches
Cachezähler
```

Die Zähler sind:

```text
prozesslokal
nicht clusterweit
```

---

# Teil VIII – Harte Invarianten

## 21. Paketweite Invarianten

```text
1. Das Package ist frameworkunabhängig.

2. Koordinatenobjekte sind immutable.

3. Block- und Chunkkoordinaten verwenden int64.

4. lokale Zellkoordinaten verwenden int32.

5. LocalMetricPosition enthält nur endliche Werte.

6. Chunkgröße ist ein positiver strikter Integer.

7. bool ist keine gültige Integerkoordinate.

8. Chunkzellanzahl ist chunk_size³.

9. Negative Koordinaten verwenden Floor-Division.

10. Lokale Zellwerte liegen immer in [0, chunk_size).

11. Block → Chunk+Zelle → Block ist exakt invertierbar.

12. X ist die schnellste Zellindexachse.

13. Chunk-Key-Format ist exakt x:y:z.

14. Chunk-Key und ChunkPosition müssen übereinstimmen.

15. Providerabhängige Normalisierung erfolgt vor Key-Erzeugung.

16. Flat verändert keine Koordinaten.

17. Earth wrappt ausschließlich X.

18. Earth-Z ist nicht periodisch.

19. Earth-Y bleibt lokal unbegrenzt.

20. Earth-X wird in einen halb-offenen zentrierten Bereich kanonisiert.

21. Der positive antipodale Halbweltwert wird negativ kanonisiert.

22. Weltbreite und halbe Weltbreite sind chunk-aligniert.

23. Batcharbeit verwendet kanonische deduplizierte Adressen.

24. Dirty-Chunks sind kanonisch, dedupliziert und stabil sortiert.

25. Nicht vorhandene Z-Nachbarn werden nicht als Dirty-Chunks ausgegeben.

26. Positionsresultate sind keine globalen Cachewahrheiten.

27. Cache-Reset verändert keine persistierten Daten.

28. Bekannte Domänenfehler sollen ihre stabilen Codes behalten.

29. Unerwartete technische Fehler werden nur an bewussten Schichtgrenzen übersetzt.

30. Kein Modul dieses Ordners führt Commit, Rollback oder HTTP-Ausgabe aus.
```

---

# Teil IX – Bestätigte und vorbereitete Bereiche

## 22. Isoliert bestätigt

```text
Paketimport
alle vier Untermodule
alle 81 öffentlichen Lazy-Symbole
Moduldiagnose
Cache-Diagnose
Cache-Reset

positive Achsenzerlegung
negative Achsenzerlegung
Achsenroundtrip
Blockroundtrip
lineare Zellindizes
Grenzoffsets

Flat-Identitätstopologie
Earth-X-Wrap
Antipodalregel
kürzeste physische Gleichheit
Chunkbatch-Deduplizierung
Dirty-Chunks an X-Naht
Dirty-Chunks an Z-Grenze
Z-Grenzfehler
Topologiecache
```

---

## 23. Über den Service bestätigt

```text
negative Chunkkoordinaten im Flat-Lesepfad
x-fastest-y-then-z in Chunk- und Commandpfaden
Earth-Provider verwendet PeriodicXTopology
Earth-Generator verwendet kanonische Chunkadressen
Georeferenzierung verwendet dieselbe Achsenkonvention
```

---

## 24. Noch vollständig produktiv durchzusetzen

```text
Earth-Kanonisierung vor jedem Snapshotlookup
Earth-Kanonisierung vor jedem Snapshotwrite
Earth-Kanonisierung im Commandpfad
Earth-Kanonisierung für Objekt-Chunks
Earth-Deduplizierung in produktiven Batchrouten
Dirty-Chunks über Topologiestrategie statt lokale Routenduplikate
Referenz-/Topologieauflösung je WorldInstance
durchgängige Domänenfehlerabbildung nach Behebung des with_context-Bugs
```

---

# Teil X – Technische Restpunkte und Risiken

## 25. `with_context()` ist aktuell nicht polymorph sicher

Dies ist der wichtigste konkrete Defekt des Ordners.

Siehe Abschnitt 7.9.

Priorität:

```text
hoch
```

Grund:

```text
mehrere öffentlich erreichbare Validierungsfehler können zu TypeError werden
```

---

## 26. Fehlerdetails sind elementmäßig unbegrenzt

Tiefe ist begrenzt, Anzahl nicht.

Risiko:

```text
große Diagnosepayloads
hohe Serialisierungskosten
ungewollt große Logs
```

---

## 27. Nicht endliche Floats in Fehlerdetails

`_to_json_value()` übernimmt Floatwerte direkt.

Damit können theoretisch:

```text
NaN
Infinity
-Infinity
```

in Fehlerdetails gelangen.

Einige JSON-Serializer lehnen dies ab, andere erzeugen nicht standardkonformes
JSON.

---

## 28. Gemischte Fehlercodes im Chunk-Key-Parser

Nicht kanonische Zahlkomponenten werden als allgemeiner
`CoordinateValidationError` ausgegeben.

Strukturell ungültige Keys sollen dagegen `ChunkAddressInvalidError` sein.

Für API-Stabilität sollte entschieden werden:

```text
alle Chunk-Key-Parserfehler
→ chunk_address_invalid

oder

allgemeine Feldvalidierungsfehler bewusst beibehalten
```

---

## 29. Keine Batchgrößengrenze im Topologiekern

`canonicalize_chunk_batch()` konsumiert ein beliebiges Iterable vollständig.

Das ist im Domainkern vertretbar, verlangt aber außen zwingend:

```text
maximale Batchgröße
Requestgrößenlimit
Timeout-/CPU-Schutz
```

---

## 30. Große Iterationen sind bewusst lazy

```text
iter_chunk_cells()
iter_chunk_blocks()
```

sind Generatoren.

Trotzdem kann ein sehr großer `chunk_size` zu extrem langer Laufzeit führen,
obwohl `chunk_size³` noch in int64 passt.

Produktive Provider sollten deutlich kleinere praktische Grenzen setzen.

---

## 31. Cachegrößen sind pro Prozess fix

Die Werte sind fest im Code definiert.

Es existiert keine Konfiguration über:

```text
Environment
Flask-Config
Runtimeparameter
```

Eine spätere Anpassung sollte reproduzierbar und begrenzt bleiben.

---

## 32. Topologieauflösung liegt außerhalb

`TopologyNotResolvedError` existiert.

Ein zentraler Resolver:

```text
WorldInstance.provider_id
→ UnboundedFlatTopology oder PeriodicXTopology
```

ist in diesem Ordner nicht enthalten.

Der Earth-Provider erzeugt seine Topologie über das Earth-Grid.

Andere Aufrufer müssen dieselbe Quelle verwenden.

---

## 33. Gleichheit der Topologiestrategien ist strukturell

Dataclasses vergleichen alle Felder.

Dies ist für Caches und Framekonsistenz geeignet.

Änderungen an zusätzlichen Feldern verändern aber automatisch die
Gleichheitssemantik.

---

## 34. Z-Grenzen verlangen ganze Chunks

`minimum_z` muss Chunkbeginn sein.

`maximum_z` muss inklusives Ende eines Chunks sein.

Feingranulare halbe Randchunks sind im Vertrag nicht vorgesehen.

---

## 35. Nur X ist periodisch

Weitere Wrapachsen dürfen nicht still in `PeriodicXTopology` eingebaut werden.

Neue Topologie:

```text
neuer TopologyKind
neue Strategieklasse
neuer Vertrag
```

---

## 36. `AmbiguousAntipodalCoordinateError` wird aktuell nicht aktiv verwendet

Die implementierte ADR-Regel ist bereits eindeutig:

```text
+half_world
→ -half_world
```

Die Fehlerklasse ist für Konfigurationen oder spätere Strategien vorbereitet,
in denen keine Antipodalregel festgelegt wurde.

---

# Teil XI – Änderungsnavigation

## 37. Wo eine Änderung hingehört

| Änderung | Datei |
|---|---|
| neuer öffentlicher Export | `__init__.py` |
| Paketdiagnose | `__init__.py` |
| Cache-Orchestrierung | `__init__.py` |
| neuer Fehlercode | `errors.py` |
| neuer Koordinatenfehler | `errors.py` |
| Fehlerkopie/-kontext | `errors.py` |
| neuer Koordinatenraum | `models.py` |
| neues immutable Wertobjekt | `models.py` |
| Chunk-Key-Vertrag | `models.py` |
| Zellindex-Vertrag | `models.py` und `chunk_math.py` |
| Floor-Division | `chunk_math.py` |
| Block-/Chunkumrechnung | `chunk_math.py` |
| Grenzoffsets | `chunk_math.py` |
| neue Topologieart | `topology.py` |
| Earth-X-Wrap | `topology.py` |
| Z-Grenzen | `topology.py` |
| Dirty-Chunks | `topology.py` |
| Provider→Topologie-Auflösung | höhere World-/Provider-Schicht |
| CRS/Globalreferenz | `src/georeferencing/` |
| Snapshot-/Event-/Commandkey | Persistence-/World-State-Schicht |
| HTTP-Fehlerabbildung | Routes-/HTTP-Schicht |

---

## 38. Empfohlene Korrekturreihenfolge

```text
1. with_context()-Defekt beheben.

2. Fehlerpfadtests für alle sechs aktuellen with_context-Aufrufstellen ergänzen.

3. Chunk-Key-Fehlercodes vereinheitlichen.

4. Fehlerdetails nach Größe und endlichen Floatwerten härten.

5. produktiven TopologyResolver zentralisieren.

6. Chunk- und Commandrouten auf WorldTopology umstellen.

7. Earth-Snapshotlookup vor DB-Zugriff kanonisieren.

8. Earth-Dirty-Chunks ausschließlich über WorldTopology berechnen.
```

---

## 39. Checkliste für neue Koordinatenmodelle

```text
1. frozen=True verwenden.

2. slots=True verwenden.

3. Koordinatenraum explizit definieren.

4. int32/int64/Floatgrenzen festlegen.

5. bool als Integer ablehnen.

6. NaN/Infinity ablehnen.

7. Mappingparser strikt halten.

8. Sequenzdimension prüfen.

9. JSON-nahe to_dict()-Ausgabe bereitstellen.

10. keine Providerlogik einbauen.

11. keine Datenbanklogik einbauen.

12. keine Caches in Wertobjekten einbauen.

13. Überlauf bei Translation prüfen.

14. öffentliche Exporte ergänzen.

15. diese IST-Datei aktualisieren.
```

---

## 40. Checkliste für neue Chunkmathematik

```text
1. topologieneutral bleiben.

2. negative Koordinaten explizit testen.

3. Floor- statt Truncation-Semantik verwenden.

4. int64-Überlauf prüfen.

5. Hin-/Rückrichtung testen.

6. Chunkgröße validieren.

7. lokale Zellgrenzen prüfen.

8. x-fastest-y-then-z beibehalten oder versionieren.

9. reine Ergebnisse nur begrenzt cachen.

10. Cache-Reset ergänzen.

11. öffentliche Exporte ergänzen.

12. diese IST-Datei aktualisieren.
```

---

## 41. Checkliste für neue Topologien

```text
1. neuen TopologyKind-Wert ergänzen.

2. WorldTopology vollständig implementieren.

3. Wrapachsen explizit definieren.

4. Nicht-Wrapachsen explizit begrenzen oder freigeben.

5. Normalisierung vor Key-Erzeugung erzwingen.

6. kanonischen Zielbereich dokumentieren.

7. Antipodalregel dokumentieren.

8. Weltbreite auf Chunkkompatibilität prüfen.

9. Batch-Kanonisierung testen.

10. Nachbarchunks testen.

11. Dirty-Chunks an allen Grenzen testen.

12. kanonische Deduplizierung testen.

13. stabile Sortierung testen.

14. Caches nur für Strategiekonfigurationen verwenden.

15. Providerresolver aktualisieren.

16. diese IST-Datei aktualisieren.
```

---

## 42. Testcheckliste für negative Koordinaten

```text
0
1
chunkSize - 1
chunkSize
-chunkSize
-1
-chunkSize - 1
int64-nahe Werte
```

Je Wert prüfen:

```text
split_axis
join_axis
block_to_chunk
block_to_cell
resolve
compose
Chunk-Key
linearIndex
```

---

## 43. Testcheckliste für Earth-X

```text
-half_world - 1
-half_world
-half_world + 1
half_world - 1
half_world
half_world + 1
mehrere positive Wraps
mehrere negative Wraps
exakt antipodal
Chunkentsprechungen
same_physical_block
same_physical_chunk
Batch-Deduplizierung
Nachbar über Naht
Dirty-Chunks über Naht
```

---

## 44. Testcheckliste für Z-Grenzen

```text
minimum_z - 1
minimum_z
minimum_z + 1
maximum_z - 1
maximum_z
maximum_z + 1

minimumChunkZ - 1
minimumChunkZ
maximumChunkZ
maximumChunkZ + 1
```

Dirty-Chunk-Sonderfälle:

```text
Zelle am südlichen Rand
Zelle am nördlichen Rand
X-Naht plus Z-Rand
Y-Rand plus Z-Rand
Ecke mit diagonalen Kombinationen
```

---

## 45. Empfohlene Navigationsreihenfolge

Für Gesamtverständnis:

```text
1. src/coordinates/IST-Zustand.md
2. src/coordinates/__init__.py
3. src/coordinates/models.py
4. src/coordinates/chunk_math.py
5. src/coordinates/topology.py
6. src/coordinates/errors.py
```

Für einen Blockcommand:

```text
LocalBlockPosition
→ WorldTopology
→ resolve_block_address
→ ResolvedCellAddress
→ Snapshotzelle
```

Für Earth:

```text
src/georeferencing/earth_grid.py
→ PeriodicXTopology
→ NormalizedBlockPosition
→ ChunkAddress
```

Für Fehler:

```text
errors.py
→ models.py-Aufrufstelle
→ Application-/HTTP-Adapter
```

---

## 46. Gesamtbefund

Der Koordinatenordner bildet eine klare und weitgehend geschlossene
Domainbasis.

Besonders belastbar sind:

```text
immutable Wertobjekte
int32-/int64-Grenzen
negative Floor-Division
eindeutige Block-/Chunk-/Zellzerlegung
x-fastest-y-then-z
strikte Chunk-Keys
Flat-Identitätstopologie
periodische Earth-X-Kanonisierung
Antipodalregel
begrenzte Z-Achse
Batch-Deduplizierung
kanonische Dirty-Chunks
begrenzte Caches
Lazy-Import-Fassade
```

Isoliert erfolgreich geprüft wurden:

```text
alle öffentlichen Exporte
alle Module
positive und negative Achsenwerte
Blockroundtrips
Zellindexroundtrips
Grenzoffsets
Flat-Topologie
Earth-X-Wrap
Earth-Antipodalwerte
physische Aliasgleichheit
Batch-Deduplizierung
Dirty-Chunks an Weltnaht und Nordgrenze
Z-Grenzfehler
Cachehits
```

Der wichtigste aktuelle Defekt ist:

```text
CoordinateError.with_context()
→ nicht kompatibel mit spezialisierten Fehlerkonstruktoren
→ mehrere Validierungsfälle liefern TypeError statt Domainfehler
```

Außerhalb dieses Ordners noch zu härten:

```text
zentraler WorldTopology-Resolver
produktive Earth-Kanonisierung vor jedem DB-Zugriff
gemeinsame Dirty-Chunk-Nutzung in Commands
Batchgrößenlimits
HTTP-Abbildung der stabilen Fehlercodes
```

Die zentrale Architekturregel lautet:

```text
models.py
→ beschreibt Werte

chunk_math.py
→ zerlegt und verbindet Werte providerneutral

topology.py
→ bestimmt die kanonische physische Adresse providerabhängig

errors.py
→ beschreibt erwartbare Fehler ohne äußere Seiteneffekte

__init__.py
→ hält die öffentliche Importgrenze stabil
```

Damit kann `src/coordinates/` verstanden, getestet und erweitert werden, ohne
alle 4.505 Quellcodezeilen einzeln lesen zu müssen.
