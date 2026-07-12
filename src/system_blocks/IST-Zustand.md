<!-- services/vectoplan-chunk/src/system_blocks/IST-Zustand.md -->
# IST-Zustand der eingebauten Systemblöcke

**Stand:** 12. Juli 2026  
**Service:** `vectoplan-chunk`  
**Paket:** `services/vectoplan-chunk/src/system_blocks`  
**Status:** Betriebsfähig, Bootstrap und Registry-Abgleich erfolgreich  
**Bekannte Abweichung:** Die Systemblock-Spezialroute liefert `system_railing` unter `persistentBlocks`, aber aktuell nicht zusätzlich unter den Top-Level-Feldern `blocks` und `inventoryBlocks`.

---

## 1. Zusammenfassung

Das eingebaute Systemblock-System ist aktiv und erfüllt die zentralen Invarianten:

- Der Systemblock-Katalog ist bereit.
- Die Default-Registry `debug-blocks@1` ist aktiv.
- Air ist ausschließlich der reservierte Zellwert `0`.
- Es existiert keine persistente `BlockType`-Zeile für Air.
- `system_railing` existiert als persistenter, aktiver `BlockType`.
- Der persistente Railing-Datensatz stimmt mit der unveränderlichen Code-Definition überein.
- `system_railing` wird in der normalen Welt-Blockliste und Palette ausgegeben.
- Die konkrete Zellwertzuweisung erfolgt chunk- beziehungsweise palettenlokal über `paletteIndex + 1`.
- Die Datenbank enthält keinen Drift für den Railing-Systemblock.

Der Begriff **Admin-Block** ist im Backend keine eigene technische Kategorie. Die vorhandenen Kategorien sind derzeit insbesondere:

- `system`: eingebaute Systemblöcke wie `system_railing`
- `debug`: Entwicklungsblöcke wie `debug_grass` und `debug_dirt`
- reservierter Zellzustand: `system_air`

Der im Editor sichtbare eingebaute Admin-/Systemblock ist derzeit `system_railing`.

---

## 2. Aktuell verifizierter Laufzeitstatus

### 2.1 Readiness

Die Systemblock-Route meldet:

| Prüfung | Ergebnis |
|---|---:|
| Gesamtroute | bereit |
| Systemblock-Katalog | bereit |
| Registry | bereit |
| Air-Invariante | bereit |
| Railing-Mirror | bereit |
| Gesamte Systemblock-Schicht | bereit |

Die relevanten Readiness-Werte sind:

```json
{
  "catalogReady": true,
  "registryReady": true,
  "airInvariantReady": true,
  "systemRailingReady": true,
  "systemBlocksReady": true
}
```

### 2.2 Katalogbestand

Der Code-Katalog enthält genau zwei Definitionen:

| Systemblock | Typ | Persistenz | Inventar |
|---|---|---:|---:|
| `system_air` | reservierter Zellzustand | nein | nein |
| `system_railing` | persistenter Laufzeitblock | ja | ja |

Aktuelle Katalogzählung:

```text
definitionCount            = 2
persistentDefinitionCount  = 1
reservedDefinitionCount    = 1
inventoryDefinitionCount   = 1
```

### 2.3 Registry-Bestand

Aktive Registry:

```text
registryId      = debug-blocks
registryVersion = 1
registryKey     = debug-blocks@1
status          = active
source          = internal
isDefault       = true
```

Die Registry enthält aktuell:

| Palette | Zellwert | Blocktyp | Kategorie |
|---:|---:|---|---|
| 0 | 1 | `debug_grass` | `debug` |
| 1 | 2 | `debug_dirt` | `debug` |
| 2 | 3 | `system_railing` | `system` |

Zusätzlich gilt unabhängig von der positiven Palette:

```text
Air = cellValue 0
```

Der aktuell ausgegebene Zellwert `3` für `system_railing` ist **keine globale feste ID**. Er entsteht aus dem aktuellen Paletteintrag:

```text
paletteIndex 2 + 1 = cellValue 3
```

In einem anderen Chunk oder einer anderen Palette kann derselbe Blocktyp einen anderen positiven Zellwert erhalten.

---

## 3. Paketstruktur

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

### Verantwortlichkeiten

| Datei | Aufgabe |
|---|---|
| `contracts.py` | Unveränderlicher, frameworkunabhängiger Definitionsvertrag |
| `catalog.py` | Provider laden, Definitionen validieren, indizieren und serialisieren |
| `bootstrap.py` | Persistente Systemblock-Mirrors prüfen, erstellen und reparieren |
| `__init__.py` | Lazy Package-Fassade und stabiler öffentlicher Importbereich |
| `air/definition.py` | Kanonische Air-Definition |
| `railing/definition.py` | Kanonische Railing-Definition |
| `air/__init__.py` | Lazy Air-Fassade und Diagnostik |
| `railing/__init__.py` | Lazy Railing-Fassade und Diagnostik |

---

## 4. Definitionsvertrag

`SystemBlockDefinition` beschreibt einen eingebauten Block unveränderlich und ohne Flask- oder SQLAlchemy-Abhängigkeit.

Wichtige Felder:

```text
system_block_id
runtime_block_type_id
definition_version
kind
source
category
status
reserved_cell_value
persist_as_block_type
inventory_visible
solid
opaque
placeable
breakable
selectable
collidable
render_mode
shape_type
material_id
texture_id
icon_id
metadata
```

Wichtige abgeleitete Eigenschaften:

```text
is_reserved_cell_state
is_air_state
is_persisted_runtime_block
can_appear_in_inventory
definition_key
definition_fingerprint
```

Der Fingerprint dient zur Erkennung von Datenbank-Drift gegenüber der Code-Definition.

---

## 5. Air

### 5.1 Identität

```text
systemBlockId      = system_air
runtimeBlockTypeId = null
definitionVersion  = 1
kind               = air
category           = system
```

### 5.2 Speicherregel

Air ist kein normaler Blocktyp.

```text
cellValue          = 0
persistAsBlockType = false
paletteEntry       = false
inventoryVisible   = false
```

Es darf keine Datenbankzeile mit `block_type_id = system_air` geben.

Die verifizierte Registry-Prüfung meldet:

```text
illegalRowCount = 0
action          = ready
ready           = true
```

### 5.3 Verhalten

```text
solid       = false
opaque      = false
placeable   = false
breakable   = false
selectable  = false
collidable  = false
renderMode  = invisible
shapeType   = empty
replaceable = true
```

Air wird erzeugt, indem ein vorhandener Block über `RemoveBlock` entfernt wird.

Vorgesehene Semantik:

```text
SetBlock(system_air) -> nicht zulässig
RemoveBlock          -> setzt cellValue auf 0
```

Die ausdrückliche HTTP-Fehlerbehandlung für `SetBlock(system_air)` ist im bestehenden Command-Pfad noch nicht als eigene Systemblock-Regel umgesetzt. Ohne diese Ergänzung schlägt die Anfrage bereits fehl, weil kein persistenter Air-Blocktyp existiert.

---

## 6. Railing

### 6.1 Identität

```text
systemBlockId      = system_railing
runtimeBlockTypeId = system_railing
definitionVersion  = 1
kind               = railing
category           = system
```

### 6.2 Persistenz

Railing wird als normaler `BlockType` in der Registry gespiegelt:

```text
persistAsBlockType = true
status             = active
inventoryVisible   = true
immutableDefinition = true
```

Aktuell verifizierter Datenbankzustand:

```text
registryId       = debug-blocks
registryVersion  = 1
blockTypeId      = system_railing
action           = unchanged
ready            = true
driftBefore      = {}
driftAfter       = {}
```

### 6.3 Laufzeitverhalten in Version 1

```text
solid       = true
opaque      = true
placeable   = true
breakable   = true
selectable  = true
collidable  = true
renderMode  = cube
shapeType   = cube
hardness    = 1.0
stackSize   = 64
```

Version 1 verwendet absichtlich einen vollständigen Würfel:

```text
currentGeometry  = full_cube
currentCollision = full_cube
```

Die stabile Blockidentität ermöglicht später eine echte Geländer-Geometrie, ohne `runtimeBlockTypeId` zu ändern.

Noch nicht unterstützt:

```text
orientation
Nachbarverbindungen
mehrzellige Geländerobjekte
spezielle Geländerkollision
```

---

## 7. Katalog

Der Katalog führt die Definitionen aus den Providern `air` und `railing` zusammen.

Aktuelle Provider:

```text
air      -> system_air
railing  -> system_railing
```

Der Katalog validiert insbesondere:

- eindeutige `systemBlockId`
- eindeutige `runtimeBlockTypeId`
- eindeutige Aliase
- eindeutige reservierte Zellwerte
- genau eine Air-Definition für Zellwert `0`
- konsistente Persistenz- und Inventarregeln
- gültige Definition-Fingerprints

Öffentliche Suchpfade umfassen:

```text
Systemblock-ID
Runtime-Blocktyp-ID
Alias
reservierter Zellwert
```

---

## 8. Datenbank-Bootstrap

Der Systemblock-Bootstrap gleicht Code-Definitionen mit der aktiven Registry ab.

### Aufgaben

- Registry prüfen
- Air-Invariante prüfen
- illegale Air-Zeilen erkennen
- fehlende persistente Systemblöcke erstellen
- abweichende persistente Systemblöcke aktualisieren
- inaktive Mirrors wiederherstellen
- Metadaten und Definition-Fingerprint prüfen
- Modelvalidierung ausführen
- Ergebnis ohne eigenen äußeren Commit zurückgeben

### Transaktionsgrenze

`src/system_blocks/bootstrap.py` übernimmt keinen äußeren Commit.

Die Commit-/Rollback-Verantwortung bleibt bei:

```text
src/bootstrap/default_seed.py
src/bootstrap/db_bootstrap.py
scripts/bootstrap_db.py
```

Dadurch werden Registry, Debug-Blöcke, Systemblöcke, Projekt, Universe und Welt gemeinsam atomar aufgebaut.

### Registry-Quelle

Die Default-Registry verwendet:

```text
source = internal
```

`bootstrap` wird nur als Benutzer- oder Metadatenkennzeichnung verwendet, nicht als `BlockRegistry.source`.

---

## 9. HTTP-Routen

### 9.1 Normale Welt-Blockliste

```http
GET /projects/dev-project/worlds/world_spawn/blocks
```

Diese Route liefert derzeit:

- Air separat
- `debug_grass`
- `debug_dirt`
- `system_railing`
- konkrete Paletteinträge
- konkrete Zellwerte
- Registry-Kontext
- Systemblock-Metadaten

Dies ist die entscheidende Route für die aktuell sichtbare Blockauswahl im Editor.

Verifizierter Railing-Eintrag:

```json
{
  "paletteIndex": 2,
  "cellValue": 3,
  "blockTypeId": "system_railing",
  "category": "system",
  "systemBlockId": "system_railing",
  "runtimeBlockTypeId": "system_railing",
  "inventoryVisible": true,
  "placeable": true,
  "selectable": true,
  "collidable": true
}
```

### 9.2 Systemblock-Spezialroute

```http
GET /projects/dev-project/worlds/world_spawn/blocks/system
```

Diese Route liefert:

- Code-Definitionen
- Air-Definition
- persistenten Railing-Mirror
- Katalogstatus
- Registry-Bootstrapstatus
- Readiness
- Encoding-Regeln

Aktuell bestätigt:

```text
ready                     = true
readiness.systemBlocksReady = true
persistentBlocks          = [system_railing]
```

### 9.3 Bekannte Routeninkonsistenz

In der Systemblock-Spezialroute stehen derzeit gleichzeitig:

```text
persistentBlocks = [system_railing]
blocks           = []
inventoryBlocks  = []
```

Der Railing-Systemblock ist trotzdem korrekt vorhanden und wird über die normale Welt-Blockroute ausgegeben.

Die leeren Felder sind jedoch semantisch missverständlich. Abhängig von der beabsichtigten API sollten sie später entweder:

1. ebenfalls den persistenten Railing-Eintrag enthalten, oder
2. eindeutiger benannt beziehungsweise dokumentiert werden, zum Beispiel als `codeOnlyBlocks`.

**Auswirkung heute:**

- Nutzt der Editor `/projects/.../blocks`, wird Railing korrekt angezeigt.
- Nutzt eine Admin-Oberfläche ausschließlich `/projects/.../blocks/system` und liest nur `inventoryBlocks`, erscheint Railing dort nicht.
- `persistentBlocks` enthält den korrekten Eintrag.

---

## 10. Aktuelle Editor-/Admin-Sichtbarkeit

### Sichtbar

```text
debug_grass
debug_dirt
system_railing
```

### Nicht als auswählbarer Block sichtbar

```text
system_air
```

Das ist beabsichtigt. Air ist kein Inventarblock.

### Bewertung

Der aktuelle sichtbare Zustand ist korrekt, sofern mit „Admin-Blöcke“ die Systemblöcke in der normalen Welt-Blockliste gemeint sind.

`system_railing` ist:

- aktiv
- persistiert
- inventarsichtbar
- platzierbar
- selektierbar
- kollidierbar
- in der Palette enthalten
- als Systemblock gekennzeichnet

---

## 11. Zellwert- und Palettenregeln

Globale Regel:

```text
Air:
    cellValue = 0

Normale Blöcke:
    cellValue = paletteIndex + 1
```

Beispiele aus dem aktuellen Zustand:

```text
debug_grass:
    paletteIndex = 0
    cellValue    = 1

debug_dirt:
    paletteIndex = 1
    cellValue    = 2

system_railing:
    paletteIndex = 2
    cellValue    = 3
```

Die Datenbank speichert für Railing absichtlich keinen globalen festen Zellwert:

```text
defaultPaletteIndex = null
defaultCellValue    = null
```

Erst die konkrete Palette bestimmt den Laufzeitwert.

---

## 12. Invarianten

Die folgenden Regeln müssen dauerhaft gelten:

1. `cellValue 0` bedeutet ausschließlich Air.
2. Air besitzt keine `BlockType`-Zeile.
3. Air besitzt keinen positiven Paletteintrag.
4. Ein persistenter Systemblock besitzt eine stabile `runtimeBlockTypeId`.
5. Positive Zellwerte sind palettenlokal.
6. `system_railing` wird in die Registry jeder relevanten Welt gespiegelt.
7. Der Railing-Mirror muss aktiv und nicht gelöscht sein.
8. Der Railing-Mirror muss dem Code-Fingerprint entsprechen.
9. Systemblock-Definitionen werden nicht über Admin-Datenbankänderungen umdefiniert.
10. Bootstrap-Unterfunktionen committen nicht eigenständig.
11. Der äußere Seed-/DB-Bootstrap entscheidet über Commit oder Rollback.
12. Unbekannte Metadaten können bei Reparaturen erhalten bleiben.

---

## 13. Verifizierte Erfolgskriterien

| Kriterium | Status |
|---|---:|
| Bootstrap erfolgreich | erfüllt |
| Schema vollständig | erfüllt |
| Default-Registry vorhanden | erfüllt |
| Registry-Quelle gültig | erfüllt |
| Air ohne DB-Zeile | erfüllt |
| Air-Zellwert 0 | erfüllt |
| Railing-Codeprovider bereit | erfüllt |
| Railing-DB-Mirror vorhanden | erfüllt |
| Railing aktiv | erfüllt |
| Railing ohne Drift | erfüllt |
| Railing in Weltpalette | erfüllt |
| Railing inventarsichtbar | erfüllt |
| Railing platzierbar | erfüllt |
| Systemroute vollständig bereit | erfüllt |
| `inventoryBlocks` der Systemroute befüllt | derzeit nicht erfüllt / semantisch offen |

---

## 14. Empfohlene nächste Änderungen

### Priorität 1: `routes/commands.py`

Empfohlene Ergänzungen:

- `SetBlock(system_air)` ausdrücklich mit HTTP 400 ablehnen
- Fehlercode `air_requires_remove_block` verwenden
- Systemblock-Metadaten beim Erzeugen neuer Chunk-Paletten erhalten
- Systemblockregeln im Command-Status dokumentieren

### Priorität 2: `routes/blocks.py`

Systemblock-Spezialroute konsistent machen:

- `inventoryBlocks` mit inventarsichtbaren persistenten Systemblöcken befüllen
- alternativ Feldnamen auf `codeOnlyBlocks` präzisieren
- `counts.inventoryBlocks` an die tatsächliche persistente Inventarsichtbarkeit angleichen

### Priorität 3: Editor

- `source = system` und `systemBlockId` visuell kennzeichnen
- Air nicht als platzierbares Inventarelement anbieten
- Railing als eingebauten, unveränderlichen Blocktyp markieren
- Kollision und Rendering später für echte Geländergeometrie erweitern

---

## 15. Manuelle Prüf-URLs

Systemblockstatus:

```text
http://localhost:5000/projects/dev-project/worlds/world_spawn/blocks/system
```

Normale Welt-Blockliste:

```text
http://localhost:5000/projects/dev-project/worlds/world_spawn/blocks
```

Je nach Deployment können Host, Port oder ein `/api`-Prefix abweichen.

---

## 16. Schlussbewertung

Das Systemblock-System ist aktuell funktionsfähig.

`system_railing` wird korrekt als eingebauter Systemblock in der Weltpalette ausgegeben. Air ist korrekt reserviert, unsichtbar und nicht persistent. Katalog, Registry und Datenbank-Mirror sind bereit und driftfrei.

Der einzige aktuell dokumentierte API-Punkt ist die leere `inventoryBlocks`-Liste der Systemblock-Spezialroute trotz vorhandenem inventarsichtbarem persistentem Railing-Mirror. Dies verhindert die Anzeige über die normale Welt-Blockroute nicht, sollte aber vor einer ausschließlichen Nutzung der Spezialroute durch eine Admin-Oberfläche bereinigt oder eindeutig dokumentiert werden.
