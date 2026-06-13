# AI.md – VECTOPLAN Chunk Service

<!-- services/vectoplan-chunk/AI.md -->

## Status dieser Fassung

Diese Fassung beschreibt den **beabsichtigten Zielstand** des `vectoplan-chunk` innerhalb der VECTOPLAN-Plattform.

Wichtig:

Diese Datei ist **kein Code-Audit** und **keine reine Bestandsaufnahme**, sondern ein **Architektur-, Verantwortungs- und Produktdokument** für den Chunk-Service.

Sie beschreibt:

- was der Chunk-Service fachlich leisten soll
- welche Rolle er in der Gesamtplattform hat
- warum er mehr ist als nur ein technischer Chunk-Loader
- wie Startwelt, Chunks, Blocktypen und Weltänderungen zusammenhängen
- wie unberührte Chunks generiert werden
- wie bearbeitete Chunks als aktuelle Snapshots gespeichert werden
- warum das Event-Log dauerhaft wichtig ist, aber nicht der normale Ladepfad sein soll
- welche Commands der Service für Block- und Weltänderungen verarbeitet
- wie Änderungen in PostgreSQL gespeichert und versionierbar gemacht werden
- wie der Editor Chunks lädt und Änderungen auslöst
- wie später Library-, Core-, Geodaten-, Planeten- und Austauschpfade angebunden werden können
- welche internen Schichten und Subsysteme im `/src`-Bereich vorgesehen sind
- welche Datenbank-Models im `models/`-Bereich vorgesehen sind
- welche Invarianten dauerhaft für Chunk-Zustände gelten sollen

Diese Datei beschreibt den **Ziel- und Arbeitsstand** des `vectoplan-chunk`.

Sie ist bewusst auf den aktuellen Entwicklungsansatz angepasst:

Der Chunk-Service ist in der ersten Ausbaustufe nicht nur ein externer Generator für Runtime-Daten, sondern der Service, der die **aktuelle Chunk-Welt erzeugt, verändert, speichert und wieder ausliefert**.

Besonders wichtig:

**Unberührte Chunks werden generiert. Bearbeitete oder explizit materialisierte Chunks werden als aktuelle Snapshots in PostgreSQL gespeichert. Jede bestätigte Änderung wird zusätzlich als Event-Historie gespeichert. Snapshots sind die Lade-Wahrheit. Events sind die historische Wahrheit für Training, Analyse und spätere Auswertung.**

---

## 1. Zweck des `vectoplan-chunk`

Der `vectoplan-chunk` ist der Service für die **block- und chunkbasierte Weltrepräsentation** von VECTOPLAN.

Er erzeugt, verwaltet und liefert die Weltbereiche, in denen sich der Editor bewegt und die der Nutzer im ersten Ausbauzustand bearbeiten kann.

Sein Zweck ist:

- eine initiale Startwelt zu definieren
- Chunks aus dieser Startwelt deterministisch zu generieren
- unberührte Chunks ohne dauerhafte Speicherung auszuliefern
- bearbeitete oder explizit materialisierte Chunks als Snapshots in PostgreSQL zu speichern
- erlaubte Blocktypen bereitzustellen oder später aus der Library zu übernehmen
- Chunk-Daten für den Editor auszuliefern
- Block- und Weltänderungen entgegenzunehmen
- Änderungen an Chunks über Commands auszuführen
- jede bestätigte Änderung zusätzlich als Event-Historie zu speichern
- betroffene Nachbarbereiche und Dirty-Chunks zu bestimmen
- aktuelle Chunk-Zustände wieder auszuliefern
- später Chunk-Zustände in Core-kompatible Austauschdaten zu übersetzen
- langfristig geodatenbasierte, planetare oder prozedurale Welten zu ermöglichen

Der Chunk-Service ist damit die erste **Weltzustands- und Chunk-Wahrheit** für die editierbare Runtime-Welt.

Wichtig:

Diese Chunk-Wahrheit ist nicht automatisch die vollständige VECTOPLAN-Projektwahrheit.

Der Service besitzt in der ersten Phase die operative Wahrheit über:

- Chunks
- Blockzellen
- Startwelt-Generierung
- materialisierte Chunk-Snapshots
- Blockänderungen
- gespeicherte Chunk-Events
- Chunk-Versionen
- betroffene Dirty-Chunks

Der Core bleibt langfristig der Ort für das kanonische semantische Projektmodell.  
Für den frühen Block-/Welt-Slice bleibt der Core bewusst außerhalb des Chunk-Command-Pfads.

---

## 2. Executive Summary

Der `vectoplan-chunk` ist am treffendsten so zu verstehen:

**Ein Python-/Flask-Microservice, der eine editierbare chunkbasierte Welt erzeugt, unberührte Chunks deterministisch generiert, bearbeitete Chunks als PostgreSQL-Snapshots speichert, jede bestätigte Änderung als Event-Historie aufzeichnet und dem Editor aktuelle Chunk-Daten als Runtime-Quelle bereitstellt.**

Die wichtigsten Architekturgrundsätze lauten:

1. Der Chunk-Service ist die operative Wahrheit für die aktuelle Chunk-Welt.
2. Der Chunk-Service erzeugt die Startwelt deterministisch aus Weltkonfiguration, Generator und Seed.
3. Unberührte Chunks werden nicht dauerhaft gespeichert.
4. Bearbeitete oder explizit materialisierte Chunks werden als `ChunkSnapshot` in PostgreSQL gespeichert.
5. `ChunkSnapshot` ist die Lade-Wahrheit für materialisierte Chunks.
6. `ChunkEvent` ist die historische Wahrheit für AI-Training, Analyse, Debugging und spätere Auswertung.
7. Events werden nicht als normaler Ladepfad verwendet.
8. Der Chunk-Service verarbeitet Commands wie Block setzen, Block abbauen und später Bereichsänderungen.
9. Chunk-Commands wirken auf Chunk-Welt und Editor, aber niemals direkt auf den Core.
10. Der Editor rendert und interagiert, aber entscheidet nicht dauerhaft über Chunk-Wahrheit.
11. Der Chunk-Service kennt erlaubte Blocktypen, zunächst als Platzhalter, später aus der Library.
12. Der Chunk-Service bleibt frei von UI-, Three.js-, Pointer-Lock- und Editor-Logik.
13. Der Chunk-Service wird später zur Brücke zwischen Runtime-Welt, Library-Daten, Geodaten, Planetenlogik und Austauschformaten.

---

## 3. Der wichtigste Leitsatz

Der wichtigste Merksatz für den Chunk-Service lautet:

**Der Chunk-Service erzeugt unberührte Weltbereiche deterministisch, materialisiert bearbeitete Chunks als aktuelle PostgreSQL-Snapshots und speichert jede bestätigte Änderung zusätzlich als append-only Event-Historie.**

Noch präziser:

- Der Chunk-Service **generiert**, **liest**, **ändert**, **speichert** und **liefert** Chunks.
- Der Chunk-Service hält die **Lade-Wahrheit** über `ChunkSnapshot`.
- Der Chunk-Service hält die **historische Wahrheit** über `ChunkEvent`.
- Der Editor **zeigt**, **streamt**, **rendert**, **targetet** und **sendet Chunk-Commands**.
- Die Library **liefert langfristig zulässige Blöcke, Typen, Varianten und Objektdefinitionen**.
- Der Core **bekommt später semantische oder austauschbare Projektdaten**, wird aber durch Chunk-Commands nicht direkt verändert.
- Der Converter **kann später aus Chunk- oder Core-Zuständen Austausch- und Artefaktformate erzeugen**.
- Geodatenquellen **können später Startwelt, Terrain, Planeten und Umgebung beeinflussen**, sind aber am Anfang nicht erforderlich.

Kurz:

**Editor klickt. Chunk-Service entscheidet und speichert. PostgreSQL hält Snapshot und Event. Editor rendert den bestätigten Chunk-Zustand.**

---

## 4. Was der Chunk-Service architektonisch ist – und was nicht

## 4.1 Was der Chunk-Service klar ist

1. Ein Python-/Flask-Microservice
2. Eine Backend-Schicht für chunkbasierte Weltzustände
3. Der Generator der Startwelt
4. Der Command-Handler für Block- und Weltänderungen
5. Der Speicherort für materialisierte Chunk-Snapshots
6. Der Speicherort für historische Chunk-Events
7. Die Quelle für editor-kompatible RuntimeChunkContent-Daten
8. Die spätere Brücke zwischen generierter Welt, Library, Geodaten, Core-Austausch und Runtime-Artefakten
9. Ein Service, der die ersten Platzhalter-Blöcke selbst definieren darf, solange die Library noch nicht existiert
10. Ein System, das später mit der Library synchronisiert, welche Blöcke und Objekte zulässig sind
11. Ein System, das langfristig mehrere Welten oder Planeten über IDs adressieren können soll

## 4.2 Was der Chunk-Service klar nicht ist

1. Kein Editor
2. Kein Three.js-Service
3. Kein UI-Service
4. Kein Pointer-Lock-, Kamera- oder Input-System
5. Kein vollständiger BIM- oder Projekt-Core
6. Kein direkter Core-Command-Prozessor
7. Kein alleiniger Owner aller VECTOPLAN-Projektsemantik
8. Kein Kosten- oder Ausschreibungsservice
9. Kein finaler IFC- oder Exportservice
10. Kein Ersatz für die spätere Library
11. Kein Ort für Frontend-Hotbar-Rendering
12. Kein normales Lade-System auf Basis von Event-Replay

---

## 5. Die Rolle des Chunk-Service in der Gesamtplattform

Im Zielbild arbeiten die Kernbausteine so zusammen:

- `vectoplan-editor` stellt die interaktive 3D-Oberfläche bereit
- `vectoplan-chunk` erzeugt und verwaltet die editierbare Chunk-Welt
- `vectoplan-library-service` liefert später die zulässigen Typen, Blöcke, Varianten und Assets
- `vectoplan-core-service` verwaltet langfristig das kanonische Projektmodell
- `vectoplan-converter-service` erzeugt oder transformiert Austausch- und Exportartefakte

Der Chunk-Service sitzt zwischen:

```text
Editor Runtime
→ Chunk API
→ Generator oder ChunkSnapshot
→ aktuelle Chunk-Antworten
→ ChunkCommand
→ Snapshot-Update
→ Event-Historie
```

Für die erste Ausbaustufe gilt:

```text
Editor
→ sendet Block-/Weltcommands
→ Chunk-Service validiert und speichert
→ PostgreSQL speichert aktuellen ChunkSnapshot
→ PostgreSQL speichert ChunkEvent
→ Editor lädt geänderte Chunks neu
```

Langfristig gilt zusätzlich:

```text
Chunk-Service
→ kann Änderungen in Core-kompatible Austauschdaten übersetzen
→ kann Core-Snapshots in Runtime-Chunks übersetzen
→ kann Geodaten in Startwelt oder Terrain einarbeiten
→ kann Library-Definitionen als zulässige Block-/Objektmenge verwenden
→ kann planetare Welten oder geschlossene Topologien vorbereiten
```

---

## 6. Lade-Wahrheit, historische Wahrheit und Projekt-Wahrheit

Der Begriff Wahrheit muss im Chunk-Service sauber getrennt werden.

## 6.1 Lade-Wahrheit

Die Lade-Wahrheit ist der Zustand, den der Editor beim Chunk-Laden erhält.

Für einen Chunk gilt:

```text
Wenn ChunkSnapshot existiert:
→ ChunkSnapshot ist Lade-Wahrheit.

Wenn kein ChunkSnapshot existiert:
→ Generator erzeugt den Chunk deterministisch.
```

Das bedeutet:

- Unberührte Chunks werden generiert.
- Bearbeitete Chunks werden aus PostgreSQL geladen.
- Der Editor muss nicht wissen, ob ein Chunk generiert oder materialisiert wurde.
- Events werden für das normale Laden nicht abgespielt.

## 6.2 Historische Wahrheit

Die historische Wahrheit ist das append-only Event-Log.

Jede bestätigte Chunk-Änderung erzeugt ein `ChunkEvent`.

Das Event-Log dient für:

- AI-Training
- spätere Analyse
- Debugging
- Nutzungsverhalten
- Bauhistorie
- Replay-Funktionen
- Ableitung von Trainingsdatensätzen

Wichtig:

**Das Event-Log ist nicht der normale Ladepfad für aktuelle Chunks.**

Es ist eine Rohhistorie. Spätere Trainingsdatensätze können daraus abgeleitet werden, ohne das Original-Log zu verändern.

## 6.3 Projekt-Wahrheit

Die vollständige VECTOPLAN-Projektwahrheit umfasst später mehr als Chunks:

- Räume
- Gebäude
- Bauteile
- semantische Instanzen
- Geschosse
- Kostenbezüge
- Normdaten
- 2D-Ableitungen
- Austauschformate
- Projektrollen
- Revisionen

Diese Projektwahrheit gehört langfristig in den Core oder in klar abgegrenzte Fachservices.

## 6.4 Praktische Regel

Für den aktuellen Entwicklungsstand gilt:

**Der Chunk-Service darf die Wahrheit für die editierbare Blockwelt besitzen.**

Das bedeutet nicht:

**Der Chunk-Service ersetzt dauerhaft den Core als semantisches Gebäudemodell.**

---

## 7. Die Startwelt

Die Startwelt ist die initiale Welt, die der Editor laden kann, bevor echte Projektdaten, Geodaten oder komplexe Gebäudemodelle existieren.

Am Anfang ist sie bewusst einfach:

- flach
- deterministisch
- chunkbasiert
- mit wenigen Platzhalter-Blöcken
- ohne externe Library
- ohne Core-Abhängigkeit
- ohne Geodaten-Abhängigkeit
- ohne automatische Höhenanpassung bestehender Bauwerke
- ohne echte Kugel- oder Planetenprojektion

Typische Startwelt-Parameter:

- `planetId`
- `worldId`
- `seed`
- `generatorType`
- `generatorVersion`
- `projectionType`
- `topologyType`
- `chunkSize`
- `cellSize`
- `surfaceY`
- `minY`
- `maxY`
- `surfaceBlockTypeId`
- `subsurfaceBlockTypeId`
- `airAboveGround`
- `defaultSpawn`
- `coordinateSystem`

Eine erste flache Welt kann zum Beispiel so gedacht werden:

```text
y > 0  → Air
y = 0  → debug_grass
y < 0  → debug_dirt
```

Oder in einer etwas kontrollierteren Variante:

```text
y > surfaceY       → Air
y = surfaceY       → debug_grass
minDepth <= y < 0  → debug_dirt
y < minDepth       → Air oder später debug_stone
```

Wichtig:

Die Startwelt wird nicht vollständig gespeichert.

Unberührte Chunks werden deterministisch generiert.  
Bearbeitete Chunks werden als `ChunkSnapshot` in PostgreSQL materialisiert.

---

## 8. Flache Welt jetzt, Planeten später

Für die erste Phase gilt:

```text
Die Welt ist flach.
Koordinaten sind lokale Blockkoordinaten.
Es gibt keinen echten Geokoordinatenbezug.
Es gibt keine echte Kugelprojektion.
Es gibt keine automatische Rückführung auf Planetengeometrie.
```

Trotzdem soll die Architektur später nicht blockieren:

- mehrere Planeten
- Planeten über IDs
- lokale flache Arbeitsbereiche auf planetaren Oberflächen
- visuelle Krümmung aus großer Entfernung
- geschlossene oder gewrappte Welt-Topologien
- späteres „von der anderen Seite wieder herauskommen“
- geodatenbasierte Terrain- und Höhenmodelle

Deshalb sollen früh Felder vorgesehen werden wie:

```text
planetId
projectionType
topologyType
generatorVersion
coordinateSystem
```

Für Phase 1 reicht:

```text
planetId = dev-earth
projectionType = flat-local-v1
topologyType = flat-unbounded-v1
```

Später mögliche Topologien:

```text
flat-unbounded-v1
flat-wrapped-v1
planet-patch-v1
spherical-planet-v1
```

Wichtig:

**Flache Welt und spätere Kugelwelt werden nicht jetzt vermischt.**

Die erste Welt bleibt lokal flach. Die planetare Logik wird nur strukturell vorbereitet.

---

## 9. Generatoränderungen und Höhenproblem

Ein bekanntes offenes Problem ist:

```text
Generator oder Geodaten ändern später Höhenpunkte.
Bestehende Bauwerke könnten danach zu hoch, zu tief oder falsch relativ zur Oberfläche liegen.
```

Für den aktuellen Stand gilt:

```text
Keine automatische Höhenanpassung.
Keine automatische Verschiebung bestehender Bauwerke.
Keine automatische Migration materialisierter Chunks.
```

Materialisierte Chunks bleiben so, wie sie gespeichert wurden.

Später mögliche Strategien:

```text
absolute_world
surface_relative
geodetic_anchor
chunk_rebase
manual_migration
semi_automatic_migration
```

Für Phase 1 wird dieses Problem bewusst nicht gelöst.

Trotzdem werden Generator- und Weltversionen gespeichert, damit spätere Migrations- oder Vergleichslogik möglich bleibt.

---

## 10. Platzhalter-Blöcke und spätere Library

Solange `vectoplan-library-service` noch nicht vollständig existiert, darf der Chunk-Service eine kleine Platzhalter-Blockliste selbst bereitstellen.

Für den ersten Slice reichen zwei Blocktypen:

```text
debug_grass
debug_dirt
```

Diese Blocktypen sind nicht das finale VECTOPLAN-Objektmodell.

Sie dienen dazu:

- Chunks sichtbar zu machen
- Startwelt zu generieren
- Place/Break zu testen
- Editor-Hotbar zu verbinden
- Zellwerte und Paletten zu prüfen
- ChunkSnapshots zu testen
- ChunkEvents zu testen
- Dirty-Chunks zu validieren

Später soll die Library die zulässigen Blöcke und Objekte liefern.

Dann gilt:

```text
Library
→ liefert zulässige Block-/Objektdefinitionen
→ Chunk-Service übernimmt oder synchronisiert erlaubte Definitionen
→ Editor nutzt dieselben IDs in Hotbar und Place-Commands
```

Wichtig:

Die erste interne Blockliste im Chunk-Service ist eine Entwicklungsstütze, keine dauerhafte Library-Kopie.

---

## 11. Blocktypen-Versionierung

Blocktypen müssen versionierbar gedacht werden.

Auch in der Debug-Phase sollte der Chunk-Service mit einer Registry arbeiten, zum Beispiel:

```text
registryId = debug-blocks
registryVersion = 1
blockTypeId = debug_grass
```

Später kann daraus werden:

```text
registryId = vectoplan-library
registryVersion = ...
typeId = ...
variantId = ...
```

Warum das wichtig ist:

- alte Chunks müssen reproduzierbar bleiben
- alte Events müssen verständlich bleiben
- AI-Training braucht stabile Bedeutungen
- spätere Library-Änderungen dürfen alte Chunk-Daten nicht unklar machen

Ein `ChunkSnapshot` und ein `ChunkEvent` sollten deshalb wissen, mit welcher BlockRegistry-Version sie erzeugt wurden.

---

## 12. Chunk-Datenmodell

Ein Chunk ist eine begrenzte, adressierbare Lade- und Änderungseinheit der Welt.

Ein Chunk enthält typischerweise:

- `planetId`
- `worldId`
- `chunkKey`
- `chunkX`
- `chunkY`
- `chunkZ`
- `chunkSize`
- `cellSize`
- `coordinateSystem`
- `projectionType`
- `topologyType`
- `palette`
- `cells`
- `chunkVersion`
- `schemaVersion`
- `blockRegistryId`
- `blockRegistryVersion`
- `source`
- `contentHash`
- optionale Metadaten

Ein Chunk ist nicht nur ein Mesh.

Ein Chunk ist eine datengetriebene Runtime-Repräsentation, aus der der Editor meshen, targeten, sampeln und rendern kann.

Für die erste Phase ist ein zellbasiertes Format vorzuziehen:

```text
RuntimeChunkContent
```

Der Editor kann daraus:

- sichtbare Meshes erzeugen
- Raycasts und Targeting unterstützen
- Blockzellen sampeln
- Place/Break ausführen
- Nachbarflächen neu bewerten
- Dirty-Chunks neu laden oder remeshen

---

## 13. ChunkSnapshot als Lade-Wahrheit

Ein `ChunkSnapshot` ist der aktuelle gespeicherte Zustand eines materialisierten Chunks.

Ein Chunk wird materialisiert, wenn:

- ein Nutzer einen Block setzt
- ein Nutzer einen Block entfernt
- eine Block- oder Bereichsänderung ausgeführt wird
- der Service einen Chunk ausdrücklich dauerhaft speichern soll
- später ein Import, Generator-Commit oder Terrain-Patch das erfordert

Nicht jeder geladene Chunk wird materialisiert.

Regel:

```text
Nur Chunks, die vom Generatorzustand abweichen oder explizit materialisiert werden müssen, werden dauerhaft gespeichert.
```

Das bedeutet:

```text
Spieler läuft herum
→ Chunks werden generiert
→ keine dauerhafte Speicherung

Spieler setzt oder entfernt Block
→ betroffener Chunk wird materialisiert
→ ChunkSnapshot wird in PostgreSQL gespeichert
→ ChunkEvent wird in PostgreSQL gespeichert
```

Der `ChunkSnapshot` ersetzt den Generatorzustand für diesen Chunk.

---

## 14. ChunkEvent als historische Wahrheit

Ein `ChunkEvent` ist ein dauerhaft gespeicherter historischer Datensatz über eine bestätigte Änderung.

Jede bestätigte Chunk-Änderung erzeugt ein Event.

Events sind wichtig für:

- AI-Training
- spätere automatische Bauvorschläge
- Analyse von Bauverhalten
- Debugging
- Replays
- Statistik
- Ableitung von Trainingsdatensätzen
- spätere Qualitätsprüfung

Ein Event sollte mindestens enthalten:

- `eventId`
- `planetId`
- `worldId`
- `chunkKey`
- `chunkX`
- `chunkY`
- `chunkZ`
- `userId`
- `sessionId`
- `commandType`
- `positionX`
- `positionY`
- `positionZ`
- `blockBeforeTypeId`
- `blockAfterTypeId`
- `cellBeforeValue`
- `cellAfterValue`
- `tool`
- `targetFace`
- `chunkVersionBefore`
- `chunkVersionAfter`
- `eventSchemaVersion`
- `payloadJson`
- `createdAt`

Für AI-Training können später zusätzliche Kontextdaten ergänzt werden:

- Kamera-/Player-Position
- Blickrichtung
- aktive Hotbar-Auswahl
- Tool-Modus
- lokale Nachbarschaft
- Bauaktionsgruppe
- Struktur-ID
- Mehrfachplatzierung
- Region oder Auswahlbereich

Wichtig:

Das rohe Event-Log wird nicht bereinigt oder überschrieben.

Spätere Trainingsdatensätze können aus dem Event-Log abgeleitet werden.

---

## 15. Zellwerte und Palette

Der Chunk-Service muss exakt mit dem Editor kompatibel bleiben.

Für die aktuelle Block-/Chunk-Logik gilt:

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

Wenn Python und TypeScript hier unterschiedlich interpretieren, entstehen Fehler wie:

- Welt ist unsichtbar
- Air wird als Block gelesen
- Block wird als Air gelesen
- Place funktioniert scheinbar, aber rendert nicht
- Break entfernt falsche Zellen
- Targeting zeigt falsche Ergebnisse

Deshalb ist diese Regel eine harte Invariante.

---

## 16. Koordinatensystem

Der Chunk-Service muss dieselbe Koordinatenlogik verwenden wie der Editor.

Zu definieren sind:

- Weltkoordinaten
- Chunk-Koordinaten
- lokale Zellkoordinaten
- Chunk-Key
- Cell-Key
- negative Koordinaten
- Rundungsregeln
- Achsenorientierung

Für den Editor ist aktuell naheliegend:

```text
Three.js-Welt
Y-up
worldX, worldY, worldZ
chunkX, chunkY, chunkZ
localX, localY, localZ
```

Wichtig bei negativen Koordinaten:

```text
floor(-1 / 16) = -1
```

Nicht:

```text
int(-1 / 16) = 0
```

Python und TypeScript müssen hier gleich rechnen.

Der Chunk-Service braucht deshalb eigene, getestete Koordinatenfunktionen in `/src`, nicht verstreute Inline-Rechnungen in Routen.

---

## 17. Startwelt-Generierung

Die Startwelt wird aus Konfiguration erzeugt.

Eine Startwelt-Generierung muss:

- deterministisch sein
- ohne vollständige Speicherung funktionieren
- Chunks auf Anfrage erzeugen
- dieselbe Anfrage mehrfach gleich beantworten
- bearbeitete Chunks durch PostgreSQL-Snapshots ersetzen
- später durch andere Generatoren ersetzbar sein

Der Ablauf für einen nicht materialisierten Chunk:

```text
Chunk-Anfrage
→ Weltkonfiguration laden
→ Basis-Chunk aus Generator erzeugen
→ Palette und Zellwerte bauen
→ RuntimeChunkContent serialisieren
→ an Editor liefern
```

Der Ablauf für einen materialisierten Chunk:

```text
Chunk-Anfrage
→ Weltkonfiguration laden
→ ChunkSnapshot in PostgreSQL finden
→ Snapshot-Inhalt laden
→ RuntimeChunkContent serialisieren
→ an Editor liefern
```

Dadurch muss die flache Welt nicht vollständig gespeichert werden.

Gespeichert werden nur materialisierte Chunks und historische Events.

---

## 18. Änderungen und Speicherung

Wenn ein Nutzer im Editor einen Block setzt oder abbaut, soll diese Änderung im Chunk-Service gespeichert werden.

Das betrifft mindestens:

- betroffene Weltposition
- alter Zustand
- neuer Zustand
- Blocktyp
- Command-Typ
- User-ID
- Session-ID
- Zeitstempel
- Planet-ID
- Welt-ID
- Chunk-Key
- betroffene Dirty-Chunks
- neue Chunk-Version
- Event-Datensatz

Eine Änderung kann mehrere Chunks betreffen.

Beispiel:

- Block wird in der Mitte eines Chunks gesetzt
- nur dieser Chunk ist dirty

Oder:

- Block wird an einer Chunk-Grenze gesetzt
- Nachbar-Chunk kann wegen sichtbarer Face-Änderung ebenfalls dirty sein

Mit „Umgebung speichern“ ist im ersten Schritt vor allem gemeint:

- Änderung an der Zielzelle anwenden
- betroffene Dirty-Chunks berechnen
- Nachbarbereiche für Remesh/Reload markieren
- Event mit Kontext speichern
- aktuelle Chunk-Snapshots speichern

Nicht gemeint ist, dass jede Nachbarzelle redundant gespeichert werden muss.

---

## 19. Command-Modell im Chunk-Service

Der Chunk-Service verarbeitet eigene Commands für Block- und Weltänderungen.

Erste Commands:

```text
SetBlock
RemoveBlock
ReplaceBlock
ClearBlock
ApplyBlockBatch
```

Spätere Commands:

```text
FillRegion
ClearRegion
ReplaceRegion
CopyRegion
PasteRegion
RegenerateChunk
ResetChunk
SetWorldConfig
ApplyTerrainPatch
ImportGeoTile
CreateRuntimeObject
RemoveRuntimeObject
```

Der Editor sendet nicht direkt Zellarrays zurück.

Der Editor sendet eine Absicht:

```text
Setze an Position X/Y/Z Block debug_grass.
```

Der Chunk-Service entscheidet:

- ist der Blocktyp erlaubt?
- ist die Position gültig?
- gehört die Position zu einer existierenden Welt?
- gibt es einen Snapshot?
- muss der Chunk generiert werden?
- welcher Zustand liegt vor der Änderung vor?
- ist die Änderung wirklich eine Änderung?
- welcher Chunk ist betroffen?
- welche Nachbar-Chunks sind dirty?
- wie wird der Snapshot gespeichert?
- welches Event wird geschrieben?
- welche Antwort bekommt der Editor?

---

## 20. Verhältnis von Chunk-Commands zu Core-Commands

Chunk-Commands wirken nur auf die Chunk-Welt.

Für den aktuellen Slice gilt:

```text
Editor
→ ChunkCommand
→ Chunk-Service speichert ChunkSnapshot und ChunkEvent
→ Core wird nicht verändert
```

Der Core muss dafür nichts definieren.

Wichtig:

**Chunk-Commands sind keine Core-Commands.**

Der Core kann später ähnliche oder gleichnamige Operationen besitzen, aber sie gehören in einen anderen fachlichen Kontext.

Langfristige Verbindung ist nur über Mapping, Export, Import oder Austauschschichten vorgesehen:

```text
Chunk-Welt
→ Mapping / Export
→ Core-kompatible Struktur
```

Nicht:

```text
ChunkCommand
→ verändert direkt Core
```

Damit bleibt der erste Welt-Slice einfach und stabil.

---

## 21. Verhältnis zum Editor

Der Editor ist der wichtigste Konsument des Chunk-Service.

Der Editor soll:

- Chunks anfordern
- Chunks streamen
- Chunk-Daten meshen
- Chunk-Daten rendern
- Targeting aus Chunk-Daten ableiten
- Place-/Break-Absichten als ChunkCommands senden
- bestätigte Antworten verarbeiten
- betroffene Chunks neu laden oder später patchen
- User-/Session-Kontext mitsenden, soweit verfügbar

Der Editor soll nicht:

- die Chunk-Wahrheit selbst besitzen
- dauerhaft lokale Blockänderungen als Wahrheit behandeln
- eigenständig entscheiden, welche Blocktypen global erlaubt sind
- Chunk-Persistenz ersetzen
- die Weltkonfiguration hart kodieren
- Event-Historie besitzen
- ChunkSnapshots speichern

Editor-seitig ist der Chunk-Service idealerweise eine `remote ChunkSource`.

---

## 22. Verhältnis zur Library

Die Library soll langfristig der Owner von Block-, Objekt-, Typ- und Variantendefinitionen sein.

Der Chunk-Service soll aus der Library später lesen oder synchronisieren:

- zulässige Blocktypen
- technische IDs
- Namen
- Icons
- Materialhinweise
- einfache Platzierungsregeln
- Geometrie- oder Renderhinweise
- Kategorien
- Varianten

Für den Anfang darf der Chunk-Service eine eigene Debug-Route anbieten:

```text
GET /blocks
```

Diese Route liefert zunächst:

```text
debug_grass
debug_dirt
```

Wichtig:

Diese Route ist eine Platzhalter-Library im Chunk-Service, nicht das finale Library-System.

---

## 23. Verhältnis zum Core

Der Core bleibt langfristig der Owner des kanonischen VECTOPLAN-Projektmodells.

Der Chunk-Service kann später mit dem Core verbunden werden, um:

- Chunk-Weltzustände in Core-kompatible Austauschdaten zu überführen
- Core-Snapshots in Runtime-Chunks zu übersetzen
- semantische Projektzustände aus Chunk-Weltzuständen abzuleiten
- Gebäude- oder Bauteilinformationen zu synchronisieren
- exportierbare Zwischenformate zu erzeugen

Für die erste Phase gilt aber:

```text
Kein Core-Zwang für SetBlock/RemoveBlock.
Keine direkte Core-Änderung durch ChunkCommands.
```

Der Chunk-Service darf Blockwelt-Zustände selbst speichern.

Später sollte es eine eigene Schicht geben:

```text
src/exchange/
src/core_mapping/
```

Diese Schicht übersetzt dann zwischen:

```text
Chunk-Zustand
↔ Core-kompatibles Authoring-/Austauschformat
```

---

## 24. Verhältnis zu Geodaten

Langfristig soll die Welt nicht nur flach sein.

Der Chunk-Service ist der richtige Ort, um später geodatenbasierte oder prozedurale Weltgenerierung anzubinden.

Mögliche spätere Quellen:

- Höhenmodelle
- Terrain-Tiles
- GIS-Daten
- Luftbilder als Referenz
- Grundstücksgrenzen
- Straßen und Wege
- Gebäudebestände
- Katasterdaten
- Projektumgebungen
- generierte Landschaften
- planetare Koordinatensysteme

Das Zielbild:

```text
Geodaten / Terrain / Projektkontext / Planet
→ World Generator
→ Chunk-Daten
→ Editor kann nahtlos in jede Richtung laufen und editieren
```

Für Phase 1 gilt:

```text
FlatWorldGenerator reicht.
```

Aber die Architektur soll so gebaut werden, dass später andere Generatoren ergänzt werden können.

---

## 25. Verhältnis zum Converter

Der Converter ist langfristig für Austausch- und Exportformate zuständig.

Der Chunk-Service kann vorbereitende Runtime-Daten liefern, aber er soll nicht zum allgemeinen Exportservice werden.

Mögliche spätere Übergänge:

```text
Chunk-Welt
→ Core Mapping
→ Converter
→ IFC / GLB / DXF / SVG / PDF / .vecto
```

Oder:

```text
Chunk-Welt
→ Runtime-Artefakt
→ Converter / Object Storage
→ Editor lädt Artefakt
```

Wichtig:

Der Chunk-Service darf interne Daten serialisieren, aber er soll nicht alle Exportformate fachlich besitzen.

---

## 26. Serverseitige Service-Schicht

Der sichtbare Flask-Service ist die Hülle.

Er ist zuständig für:

- App-Start
- Konfiguration
- Health-Checks
- Routenregistrierung
- JSON-Antworten
- Fehlerantworten
- CORS oder Gateway-Kompatibilität
- Dev-/Fallback-Verhalten
- SQLAlchemy-/Model-Registrierung

Die tiefe Chunk-Logik soll nicht in den Routen liegen.

Routen sollen HTTP-Adapter sein.

Die eigentliche Mechanik gehört in:

```text
src/
```

Die Datenbankmodelle gehören in:

```text
models/
```

---

## 27. PostgreSQL als primärer Speicher

Der Chunk-Service verwendet PostgreSQL als primären Speicher.

Es ist nicht geplant, die erste Persistenz über Dateien, JSON-Dateien oder MemoryStore zu bauen.

Wichtig:

```text
PostgreSQL ist Speicher für:
- Planeten
- Welten
- Blocktypen / Registry-Snapshots
- materialisierte ChunkSnapshots
- ChunkEvents
```

Die Anwendungslogik soll trotzdem nicht direkt überall SQLAlchemy-Models manipulieren.

Dafür ist eine Repository-/Service-Schicht sinnvoll:

```text
models/
→ SQLAlchemy Models

src/repositories/
→ Datenzugriff

src/chunks/
→ Chunk-Logik

src/commands/
→ Command-Ausführung

routes/
→ HTTP-Adapter
```

Für die aktuelle Entwicklungsphase müssen Crash-Recovery, Locking und komplexe Konfliktbehandlung noch nicht vollständig gelöst werden.

Trotzdem sollte der normale Command-Ablauf transaktional gedacht werden:

```text
Command empfangen
→ Snapshot laden oder Chunk generieren
→ Änderung anwenden
→ Snapshot speichern
→ Event speichern
→ Commit
```

---

## 28. Empfohlene Zielstruktur des Services

Die aktuelle Service-Struktur darf aus dem kopierten Flask-Muster wachsen.

Eine sinnvolle Zielstruktur ist:

```text
services/
└── vectoplan-chunk/
    ├── AI.md
    ├── README.md
    ├── Dockerfile
    ├── entrypoint.sh
    ├── requirements.txt
    ├── wsgi.py
    ├── app.py
    ├── config.py
    ├── extensions.py
    │
    ├── bootstrap/
    │   ├── __init__.py
    │   ├── startup.py
    │   └── health.py
    │
    ├── routes/
    │   ├── __init__.py
    │   ├── health.py
    │   ├── chunks.py
    │   ├── blocks.py
    │   ├── worlds.py
    │   └── commands.py
    │
    ├── models/
    │   ├── __init__.py
    │   ├── planet.py
    │   ├── world.py
    │   ├── block.py
    │   ├── chunk.py
    │   └── event.py
    │
    ├── src/
    │   ├── bootstrap/
    │   │   ├── __init__.py
    │   │   └── startup.py
    │   │
    │   ├── blocks/
    │   │   ├── __init__.py
    │   │   ├── models.py
    │   │   ├── registry.py
    │   │   ├── defaults.py
    │   │   └── serialize.py
    │   │
    │   ├── coordinates/
    │   │   ├── __init__.py
    │   │   ├── models.py
    │   │   ├── chunk_keys.py
    │   │   └── math.py
    │   │
    │   ├── world/
    │   │   ├── __init__.py
    │   │   ├── models.py
    │   │   ├── config.py
    │   │   ├── flat_world.py
    │   │   ├── generators.py
    │   │   └── service.py
    │   │
    │   ├── chunks/
    │   │   ├── __init__.py
    │   │   ├── models.py
    │   │   ├── content.py
    │   │   ├── palette.py
    │   │   ├── serializer.py
    │   │   └── service.py
    │   │
    │   ├── commands/
    │   │   ├── __init__.py
    │   │   ├── models.py
    │   │   ├── validate.py
    │   │   ├── executor.py
    │   │   ├── results.py
    │   │   └── dirty_chunks.py
    │   │
    │   ├── events/
    │   │   ├── __init__.py
    │   │   ├── models.py
    │   │   ├── recorder.py
    │   │   └── serialize.py
    │   │
    │   ├── repositories/
    │   │   ├── __init__.py
    │   │   ├── worlds.py
    │   │   ├── blocks.py
    │   │   ├── chunks.py
    │   │   └── events.py
    │   │
    │   ├── api/
    │   │   ├── __init__.py
    │   │   ├── responses.py
    │   │   ├── errors.py
    │   │   └── normalize.py
    │   │
    │   ├── exchange/
    │   │   ├── __init__.py
    │   │   └── core_mapping.py
    │   │
    │   └── utils/
    │       ├── __init__.py
    │       ├── ids.py
    │       ├── time.py
    │       └── safe.py
    │
    └── tests/
        ├── unit/
        ├── integration/
        └── e2e/
```

Wichtig ist nicht jeder einzelne Ordnername.

Wichtig ist die Invariante:

**Routes bleiben dünn. Chunk-, World-, Block-, Command-, Event- und Repository-Logik lebt in `/src`. SQLAlchemy-Models leben in `models/`.**

---

## 29. Vorgesehene Datenbank-Models

Für den ersten belastbaren Slice sind diese Models sinnvoll.

## 29.1 `Planet`

Auch wenn die erste Welt flach ist, soll ein Planetenkontext früh existieren.

Typische Felder:

```text
id
slug
name
status
created_at
updated_at
```

Für Phase 1:

```text
planetId = dev-earth
```

Noch keine echte Kugellogik.

## 29.2 `World`

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

## 29.3 `BlockType`

Debug-Blocktypen, später Library-gebunden.

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

## 29.4 `ChunkSnapshot`

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

## 29.5 `ChunkEvent`

Historische Wahrheit für AI-Training, Analyse und spätere Auswertung.

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

Wichtig:

`ChunkEvent` ist append-only.

---

## 30. Aktuelle kopierte Flask-Struktur

Der aktuelle Service kann noch editorähnliche Namen enthalten, zum Beispiel:

```text
routes/editor.py
templates/editor/
static/editor/
```

Das ist für den Start nicht entscheidend.

Entscheidend ist:

- der Service startet
- Health funktioniert
- Routen lassen sich registrieren
- Models werden sauber registriert
- `/src` wird sauber aufgebaut
- Chunk-Logik wird nicht in Template- oder Editor-Namen fest verdrahtet

Die Umbenennung von `editor.py` zu `chunks.py` oder von `templates/editor` zu service-neutralen Namen kann später erfolgen.

Der erste technische Fokus liegt auf:

```text
models/
src/
routes/chunks.py
routes/blocks.py
routes/commands.py
```

---

## 31. Zentrale API-Idee

Der Chunk-Service braucht wenige klare Routen.

## 31.1 Health

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

## 31.2 Blocktypen

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

## 31.3 Welt-Metadaten

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

## 31.4 Einzelnen Chunk laden

```text
GET /chunks?worldId=default&chunkX=0&chunkY=0&chunkZ=0
```

Antwort enthält einen editor-kompatiblen Chunk.

## 31.5 Mehrere Chunks laden

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

## 31.6 Command ausführen

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

## 32. Chunk-Load-Ablauf

Ein typischer Load-Ablauf sieht so aus:

1. Editor bestimmt benötigte Chunk-Koordinaten.
2. Editor sendet Chunk-Anfrage an `vectoplan-chunk`.
3. Chunk-Service normalisiert Welt- und Chunk-Parameter.
4. Chunk-Service lädt Weltkonfiguration.
5. Chunk-Service prüft, ob ein `ChunkSnapshot` existiert.
6. Falls ja: Snapshot wird geladen.
7. Falls nein: Basis-Chunk wird aus Generator erzeugt.
8. Chunk-Service baut Palette und Zellwerte.
9. Chunk-Service serialisiert RuntimeChunkContent.
10. Editor erhält Chunk.
11. Editor registriert Chunk.
12. Editor mesht und rendert Chunk.

Wichtig:

Der Editor bekommt den aktuellen Zustand.  
Er muss nicht wissen, ob der Chunk generiert oder materialisiert wurde.

---

## 33. Command-Ablauf

Ein typischer SetBlock-Ablauf sieht so aus:

1. Nutzer zielt im Editor auf eine Position.
2. Editor bestimmt Zielzelle und aktiven Blocktyp.
3. Editor sendet `SetBlock` an `vectoplan-chunk`.
4. Chunk-Service validiert Command.
5. Chunk-Service prüft Blocktyp gegen Block-Registry.
6. Chunk-Service berechnet Chunk-Key und lokale Zellposition.
7. Chunk-Service lädt `ChunkSnapshot` oder erzeugt Basis-Chunk aus Generator.
8. Chunk-Service liest aktuellen Zellzustand.
9. Chunk-Service entscheidet, ob sich etwas ändert.
10. Chunk-Service erzeugt oder aktualisiert den `ChunkSnapshot`.
11. Chunk-Service berechnet Dirty-Chunks.
12. Chunk-Service schreibt ein `ChunkEvent`.
13. Chunk-Service erhöht Chunk-Version.
14. Chunk-Service committet die Datenbanktransaktion.
15. Chunk-Service antwortet mit Ergebnis.
16. Editor lädt dirty Chunks neu oder wendet später Patch an.
17. Editor rendert bestätigten Zustand.

Ein RemoveBlock-Ablauf ist analog:

```text
Zielzelle
→ aktueller Block
→ Air setzen
→ Snapshot aktualisieren
→ Event schreiben
→ Dirty-Chunks berechnen
→ Antwort
```

---

## 34. Dirty-Chunks

Jede Änderung muss bestimmen, welche Chunks neu geladen oder neu gemesht werden müssen.

Ein einzelner Block kann mehr als einen Chunk betreffen, wenn er an einer Chunk-Grenze liegt.

Beispiel:

```text
Block liegt bei localX = 0
→ linker Nachbar-Chunk kann betroffen sein

Block liegt bei localX = chunkSize - 1
→ rechter Nachbar-Chunk kann betroffen sein
```

Gleiches gilt für:

- Y-Grenzen
- Z-Grenzen
- Kanten
- Ecken

Der Chunk-Service soll deshalb eine eigene Dirty-Chunk-Schicht besitzen:

```text
src/commands/dirty_chunks.py
```

Diese bestimmt:

- `changedChunks`
- `dirtyChunks`
- `neighborChunks`
- `reloadHints`

---

## 35. RuntimeChunkContent-Kompatibilität

Der erste Chunk-Service muss sich an den existierenden Editor-Modellen orientieren.

Relevant sind editorseitig vor allem:

```text
frontend/src/runtime/world/chunk_content_models.ts
frontend/src/runtime/world/chunk_coordinates.ts
frontend/src/runtime/world/chunk_source.ts
```

Der Chunk-Service soll nicht zuerst ein komplett neues Format erfinden.

Er soll zuerst das liefern, was der Editor bereits verstehen und meshen kann.

Später können zusätzliche Formate entstehen:

- binäre Chunk-Daten
- komprimierte Zellarrays
- Mesh-Artefakte
- Instancing-Daten
- Picking-Indizes
- Semantic Maps
- Anchor-/Socket-Maps

Aber Phase 1 bleibt zellbasiert.

---

## 36. Performance-Grundsätze

Der Chunk-Service muss langfristig für große Welten geeignet sein.

Deshalb gelten:

## 36.1 Generieren statt alles speichern

Unberührte Chunks werden generiert.

## 36.2 Materialisieren nur bei Bedarf

Nur geänderte oder explizit materialisierte Chunks werden dauerhaft gespeichert.

## 36.3 Snapshot statt Event-Replay

Der normale Ladepfad nutzt `ChunkSnapshot` oder Generator, nicht das Event-Log.

## 36.4 Event-Log bleibt vollständig

Events werden für History und AI-Training gespeichert, aber nicht für jedes Laden replayt.

## 36.5 Batch statt Einzelanfragen

Der Editor soll mehrere Chunks auf einmal anfordern können.

## 36.6 Kleine, stabile Payloads

Chunk-Antworten müssen stabil und editor-kompatibel sein.

## 36.7 Dirty-Updates statt Full Reload

Nach Änderungen sollen nur betroffene Chunks neu geladen werden.

## 36.8 Deterministische Generatoren

Gleiche Weltkonfiguration und gleicher Seed ergeben gleiche Chunks.

## 36.9 Klare Fehlerantworten

Fehler dürfen den Editor nicht in einen unklaren Zustand bringen.

---

## 37. Robustheit und Fehlerverhalten

Der Chunk-Service muss früh robust sein.

Typische Fehlerfälle:

- ungültige Chunk-Koordinaten
- ungültige Welt-ID
- ungültige Planet-ID
- unbekannter Blocktyp
- nicht erlaubter Blocktyp
- ungültige Position
- ungültiger Command-Typ
- beschädigter ChunkSnapshot
- fehlende Weltkonfiguration
- fehlende BlockRegistry
- Generatorfehler
- Serialisierungsfehler
- Datenbankfehler

Fehlerantworten sollen strukturiert sein:

```json
{
  "ok": false,
  "error": {
    "code": "unknown_block_type",
    "message": "Block type is not registered.",
    "details": {
      "blockTypeId": "unknown"
    }
  }
}
```

Routen sollen keine Python-Tracebacks als normale API-Antwort liefern.

---

## 38. Sicherheit und Validierung

Auch wenn der erste Slice lokal und ohne echte Authentifizierung laufen kann, darf der Service nicht blind dem Editor vertrauen.

Der Chunk-Service muss selbst prüfen:

- Command-Typ
- Blocktyp
- Position
- Welt-ID
- Planet-ID
- Chunk-Grenzen
- erlaubte Operation
- Placeable/Breakable
- Payload-Größe
- Batch-Größe

Später kommen hinzu:

- Nutzer
- Projekt
- Session
- Rechte
- Rollen
- Sperren
- Konflikte
- Revisionsbedingungen

Für die aktuelle Phase reicht:

- `userId` bei Änderungen speichern
- `sessionId` optional speichern
- mehrere Nutzer dürfen denselben Chunk verändern
- komplexe Konfliktauflösung ist noch nicht Teil des ersten Slices

---

## 39. Teststrategie

Der Chunk-Service braucht mehrere Testebenen.

## 39.1 Unit-Tests

Zum Beispiel:

- Weltkoordinate zu Chunk-Koordinate
- negative Koordinaten
- lokale Zellkoordinate
- Chunk-Key-Erzeugung
- Palette-Encoding
- CellValue-Encoding
- FlatWorldGenerator
- Dirty-Chunk-Berechnung
- BlockRegistry
- Command-Validierung
- Event-Serialisierung

## 39.2 Model-/Repository-Tests

Zum Beispiel:

- Planet anlegen
- World anlegen
- BlockType anlegen
- ChunkSnapshot speichern
- ChunkSnapshot laden
- ChunkEvent schreiben
- `unique(world_id, chunk_x, chunk_y, chunk_z)` beachten

## 39.3 Service-Tests

Zum Beispiel:

- Chunk laden
- Batch laden
- Blockliste laden
- Weltmetadaten laden
- SetBlock ausführen
- RemoveBlock ausführen
- unbekannter Blocktyp wird abgelehnt
- Änderung bleibt nach erneutem Chunk-Laden sichtbar
- Event wird geschrieben
- Snapshot wird geschrieben

## 39.4 Integration mit Editor

Zum Beispiel:

- Editor lädt Remote-Chunks
- Editor zeigt flache Welt
- Editor setzt Block über ChunkCommand
- Editor lädt Dirty-Chunk neu
- gesetzter Block bleibt sichtbar
- Editor entfernt Block
- Block verschwindet sichtbar
- Event enthält User-ID

## 39.5 Spätere Integration mit Library, Core und Geodaten

Zum Beispiel:

- Library liefert BlockRegistry
- Chunk-Service übernimmt erlaubte Typen
- Chunk-Zustand wird in Core-Austauschformat übersetzt
- Core-Snapshot kann zu Chunks kompiliert werden
- Geodaten-Generator erzeugt Terrain-Chunks

---

## 40. Empfohlene Entwicklungsreihenfolge

Der Chunk-Service sollte schrittweise wachsen.

## Phase 1 – Service stabil startbar

- Flask-App
- Health-Route
- saubere Routenregistrierung
- JSON-Fehlerantworten
- `/src`-Imports stabilisieren
- `models/__init__.py` für Model-Registrierung vorbereiten

## Phase 2 – PostgreSQL-Models

- `Planet`
- `World`
- `BlockType`
- `ChunkSnapshot`
- `ChunkEvent`
- Model-Registrierung
- minimale Seed-/Default-Erzeugung

## Phase 3 – BlockRegistry und Platzhalter-Blöcke

- `debug_grass`
- `debug_dirt`
- `GET /blocks`
- Registry-ID und Registry-Version
- placeable/breakable/solid-Felder
- Palette-Mapping

## Phase 4 – Koordinaten und Chunk-Modelle

- Chunk-Koordinaten
- lokale Zellkoordinaten
- negative Koordinaten
- Chunk-Key
- Cell-Encoding
- RuntimeChunkContent-kompatible Modelle

## Phase 5 – FlatWorldGenerator

- Weltkonfiguration
- flache Startwelt
- deterministische Chunk-Erzeugung
- Oberfläche und Tiefe definieren
- `planetId`, `projectionType`, `topologyType` vorbereiten

## Phase 6 – Chunk-Load-Routen

- `GET /chunks`
- `POST /chunks/batch`
- Snapshot-oder-Generator-Load
- editor-kompatible Payloads
- erste sichtbare Remote-Welt im Editor

## Phase 7 – Command-System

- `SetBlock`
- `RemoveBlock`
- Command-Validierung
- CommandResult
- Dirty-Chunks
- ChunkVersion

## Phase 8 – Snapshot- und Event-Speicherung

- ChunkSnapshot speichern
- ChunkEvent schreiben
- User-ID speichern
- Änderung nach Reload sichtbar halten
- Event-History für AI-Training aufzeichnen

## Phase 9 – Editor-Anbindung

- Editor `ChunkServiceSource`
- Remote Source per Bootstrap aktivieren
- SetBlock/RemoveBlock an Chunk-Service senden
- Dirty-Chunks neu laden

## Phase 10 – Erweiterte Weltmechanik

- Batch-Commands
- Fill/Replace/Region-Tools
- ResetChunk
- RegenerateChunk
- größere Weltbereiche
- Bauaktionsgruppen
- optional Structure-/BuildGroup-Metadaten

## Phase 11 – Library-, Core-, Geodaten- und Planeten-Anbindung

- Library-BlockRegistry
- Core-Mapping
- Geodaten-Generatoren
- planetare Projektionen
- geschlossene Topologien
- Austauschformate
- echte Projektwelt

---

## 41. Wichtigste Invarianten des Chunk-Service

Diese Regeln sollten dauerhaft gelten:

1. Der Chunk-Service besitzt die operative Wahrheit über Chunk-Zustände.
2. Unberührte Chunks werden deterministisch generiert.
3. Bearbeitete oder explizit materialisierte Chunks werden als `ChunkSnapshot` in PostgreSQL gespeichert.
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
19. Die interne Platzhalter-Blockliste ersetzt nicht dauerhaft die Library.
20. Der Chunk-Service liefert keine Three.js-Objekte.
21. Dirty-Chunks müssen bei Änderungen zuverlässig berechnet werden.
22. Die erste Welt ist flach.
23. Die erste Welt hat keinen echten Geokoordinatenbezug.
24. `planetId`, `projectionType` und `topologyType` werden trotzdem vorbereitet.
25. Keine automatische Höhenanpassung bestehender Bauwerke in Phase 1.
26. Core-Anbindung ist später wichtig, aber nicht Teil des ersten Chunk-Command-Pfads.
27. `/src` ist der Ort für die eigentliche Chunk-Mechanik.
28. `models/` ist der Ort für SQLAlchemy-/Flask-Models.
29. Routen bleiben dünne HTTP-Adapter.
30. Chunk-Daten müssen editor-kompatibel serialisiert werden.
31. Änderungen müssen nach erneutem Laden sichtbar bleiben.
32. Performance darf nicht durch vollständige Speicherung unendlicher Welten blockiert werden.

---

## 42. Offene Probleme und spätere Klärungen

Diese Punkte werden bewusst nicht im ersten Slice gelöst, müssen aber bekannt bleiben:

1. Wie werden bestehende Bauwerke behandelt, wenn spätere Terrain- oder Höhendaten anders ausfallen?
2. Wie werden materialisierte Chunks später gegen neue Generatorversionen migriert?
3. Wie werden Übergänge zwischen generierten und materialisierten Chunks geglättet?
4. Wie wird ein Gebäude über mehrere Chunks hinweg als zusammengehörige Struktur erkannt?
5. Wie werden Bauaktionen später zu `BuildGroup`, `StructureId` oder ähnlichen Einheiten gruppiert?
6. Wie werden mehrere Nutzer bei gleichzeitigen Änderungen am selben Chunk konfliktarm behandelt?
7. Welche zusätzlichen Kontextdaten braucht das Event-Log für hochwertige AI-Trainingsdaten?
8. Wie wird aus roher Event-History später ein Trainingsdatensatz abgeleitet?
9. Wie wird eine flache Welt später auf planetare Koordinaten oder geschlossene Topologien gemappt?
10. Wie wird das spätere „von der anderen Seite wieder herauskommen“ topologisch modelliert?
11. Wie werden Geodaten, Terrain und gebaute Inhalte dauerhaft sauber getrennt?
12. Wie werden sehr große ChunkSnapshots effizient gespeichert oder komprimiert?

Diese offenen Punkte blockieren Phase 1 nicht.

---

## 43. Prägnantes Gesamtbild

Der belastbare Gesamtbefund für den Chunk-Service lautet:

**Der `vectoplan-chunk` ist der Python-/Flask-Microservice für die editierbare chunkbasierte Welt von VECTOPLAN. Er erzeugt unberührte Chunks deterministisch, speichert bearbeitete Chunks als PostgreSQL-Snapshots, schreibt jede bestätigte Änderung als Event-Historie und liefert dem Editor aktuelle RuntimeChunkContent-Daten.**

Besonders wichtig ist:

- Der Chunk-Service ist mehr als ein passiver Chunk-Loader.
- Er besitzt die operative Chunk-Wahrheit.
- Snapshots sind die Lade-Wahrheit.
- Events sind die historische Wahrheit.
- Events dienen langfristig AI-Training, Analyse und Auswertung.
- Der normale Ladepfad nutzt nicht das Event-Log.
- Er startet mit einer flachen Welt.
- Er arbeitet zunächst mit Platzhalter-Blöcken.
- Er nutzt PostgreSQL als primären Speicher.
- Er berechnet Dirty-Chunks.
- Er bleibt unabhängig von Editor-UI und Three.js.
- ChunkCommands ändern niemals direkt den Core.
- Spätere Planeten-, Geodaten- und Kugelwelt-Ideen werden strukturell vorbereitet, aber nicht in Phase 1 gelöst.
- Die eigentliche Mechanik gehört in `/src`.
- Die Datenbankmodelle gehören in `models/`.

---

## 44. Kurzfassung für Reviewer

- `vectoplan-chunk` ist der Service für die editierbare Chunk-Welt.
- Er erzeugt die initiale Startwelt.
- Die Startwelt ist am Anfang flach.
- Unberührte Chunks werden generiert.
- Bearbeitete Chunks werden als `ChunkSnapshot` in PostgreSQL gespeichert.
- `ChunkSnapshot` ist die Lade-Wahrheit.
- Jede bestätigte Änderung erzeugt ein `ChunkEvent`.
- `ChunkEvent` ist die historische Wahrheit für AI-Training, Analyse und spätere Auswertung.
- Events sind nicht der normale Ladepfad.
- Der Service liefert Chunks an den Editor.
- Der Service verarbeitet ChunkCommands wie `SetBlock` und `RemoveBlock`.
- ChunkCommands verändern niemals direkt den Core.
- Der Editor ist Interaktions- und Renderoberfläche.
- Der Core muss für den ersten Block-Slice nicht beteiligt sein.
- Die Library wird später zulässige Blöcke liefern.
- Bis dahin liefert der Chunk-Service zwei Platzhalter-Blöcke.
- Die Chunk-Daten müssen mit dem Editor kompatibel bleiben.
- `0 = Air`, `paletteIndex + 1 = Block`.
- Koordinatenlogik muss zwischen Python und TypeScript exakt gleich sein.
- PostgreSQL ist der primäre Speicher.
- `models/` enthält die Datenbank-Models.
- `/src` enthält die eigentliche Chunk-Mechanik.
- Später kann der Service Geodaten, Planeten, Core-Mapping und Austauschformate vorbereiten.

---

## 45. Nächster sinnvoller Schritt

Der nächste sinnvolle Schritt nach dieser Datei ist:

1. die aktuelle Flask-Shell unangetastet startfähig lassen
2. `models/__init__.py` für Model-Registrierung vorbereiten
3. PostgreSQL-Models definieren:
   - `Planet`
   - `World`
   - `BlockType`
   - `ChunkSnapshot`
   - `ChunkEvent`
4. in `/src` die Grundmodule für Blöcke, Koordinaten, Welt, Chunks, Commands, Events und Repositories anlegen
5. zwei Platzhalter-Blöcke definieren:
   - `debug_grass`
   - `debug_dirt`
6. eine `GET /blocks`-Route bauen
7. eine flache Startwelt als `FlatWorldGenerator` bauen
8. `GET /chunks` und `POST /chunks/batch` mit editor-kompatiblen Chunk-Daten liefern
9. dabei zuerst prüfen:
   - existiert `ChunkSnapshot`?
   - wenn ja: Snapshot laden
   - wenn nein: Chunk generieren
10. danach `POST /commands` für `SetBlock` und `RemoveBlock` ergänzen
11. bei jedem bestätigten Command:
   - Snapshot speichern oder aktualisieren
   - Event schreiben
   - User-ID speichern
   - Dirty-Chunks zurückgeben
12. anschließend den Editor über eine Remote-ChunkSource anbinden

Erst wenn diese Schleife funktioniert, sollte die nächste Ebene folgen:

```text
Editor
→ Remote-Chunk laden
→ flache Welt sehen
→ Block setzen
→ Chunk-Service speichert ChunkSnapshot
→ Chunk-Service schreibt ChunkEvent
→ Dirty-Chunk neu laden
→ Block bleibt sichtbar
→ Block abbauen
→ ChunkSnapshot wird aktualisiert
→ ChunkEvent wird geschrieben
→ Änderung bleibt sichtbar
```