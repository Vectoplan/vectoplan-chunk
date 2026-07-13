[ADR-earth-world-v1.md herunterladen](sandbox:/mnt/data/ADR-earth-world-v1.md)

<!-- services/vectoplan-chunk/docs/adr/ADR-earth-world-v1.md -->

# ADR: Earth World v1 – eine global referenzierte, lokal persistierte und periodische Flat-Welt

## Status

**Entscheidung:** angenommen zur Implementierung
**Version:** 1.0.0
**Stand:** 2026-07-12
**Betroffener Service:** `vectoplan-chunk`
**Gültig ab:** Einführung des Providers `earth`
**Ersetzt:** keine bestehende Architekturentscheidung
**Ergänzt:** bestehende `flat`-/`world_spawn`-Architektur

---

## 1. Zweck dieser Entscheidung

Diese Architecture Decision Record definiert den verbindlichen Architekturvertrag für den zweiten Welt-Typ `earth`.

Der bestehende Welt-Typ `flat` bleibt erhalten und unverändert nutzbar. `earth` wird als zusätzlicher Provider eingeführt und verwendet weiterhin eine geometrisch flache, chunkbasierte Voxelwelt. Der entscheidende Unterschied besteht darin, dass eine konkrete Earth-`WorldInstance` genau einen globalen Referenzpunkt mit eindeutigem Koordinatensystem besitzt.

Alle veränderlichen Inhalte der Welt werden weiterhin lokal relativ zu diesem Referenzpunkt gespeichert:

```text
eine globale Referenz pro Earth-WorldInstance
→ lokale Projekt-/Weltkoordinaten
→ lokale Chunkkoordinaten
→ lokale Zellkoordinaten
→ ChunkSnapshot, ChunkEvent und WorldCommandLog
```

Globale Koordinaten einzelner Blöcke, Chunks, Events, Objekte oder Spielerpositionen werden nicht redundant persistiert. Sie werden bei Bedarf deterministisch aus der globalen Referenz, der unveränderlichen Earth-Grid-Definition und den lokalen Koordinaten berechnet.

Diese Entscheidung soll insbesondere verhindern:

* doppelte physische Positionen an der Weltnaht;
* unterschiedliche Raster zwischen Regionen oder Projekten;
* Rundungsdrift durch wiederholte Koordinatentransformationen;
* regionale Wechsel des Runtime-Koordinatensystems;
* unbeabsichtigte Verschiebung einer bereits materialisierten Welt;
* Vermischung von globaler Weltreferenz und lokalem Spawnpunkt;
* Änderungen am bestehenden `flat`-Verhalten.

---

## 2. Bestehender Kontext

Der aktuelle Chunk-Service besitzt eine konkrete, editierbare `WorldInstance` mit der ID `world_spawn`. Der bestehende Provider beziehungsweise das bestehende Template ist `flat`.

```text
Project
→ Universe
→ WorldInstance world_spawn
   → provider/template flat
```

`world_spawn` ist die konkrete Projektwelt. `flat` ist die Provider-/Template-Definition.

Der vorhandene Laufzeitpfad bleibt grundsätzlich bestehen:

```text
Chunk anfordern
→ Snapshot suchen
→ falls vorhanden: Snapshot laden
→ andernfalls: Provider/Generator verwenden

Command ausführen
→ Chunk laden oder generieren
→ lokale Änderung anwenden
→ ChunkSnapshot schreiben
→ ChunkEvent schreiben
→ WorldCommandLog schreiben
→ Dirty-Chunks zurückgeben
```

Der neue Provider `earth` darf diese bestehenden Grundmechanismen nicht duplizieren oder ersetzen. Er ergänzt die vorhandene Architektur um:

* eine globale Referenz;
* eine versionierte Earth-Grid-Definition;
* eine periodische X-Topologie;
* globale/lokale Koordinatenumrechnung;
* einen lokal gespeicherten, global adressierbaren Spawnpunkt.

---

## 3. Entscheidung in Kurzform

Für `earth` gilt:

```text
Geometrie:
→ weiterhin flach
→ weiterhin regelmäßige, unverformte Voxel
→ weiterhin chunkbasiert

Globale Verortung:
→ genau ein globaler Referenzpunkt pro konkreter Earth-WorldInstance
→ Referenzpunkt besitzt ein explizites CRS
→ keine globalen Koordinaten pro Block oder Chunk

Lokale Persistenz:
→ lokale Weltkoordinaten
→ lokale Chunkkoordinaten
→ lokale Zellkoordinaten
→ lokale Snapshots, Events und Commands

Topologie:
→ X ist periodisch
→ Ost und West sind verbunden
→ Z ist in Version 1 nicht periodisch
→ Nord und Süd bilden keine einfache Wrap-Naht

Raster:
→ ein einziges kanonisches Earth-Raster
→ überall gleiche Achsen
→ keine regionale Rotation
→ keine projektspezifische Chunkraster-Rotation

Spawn:
→ lokal gespeichert
→ kann über eine globale Zielkoordinate gesetzt werden
→ globale Weltreferenz bleibt dabei unverändert
```

---

## 4. Ziele

### 4.1 Funktionale Ziele

`earth` muss ermöglichen:

1. eine Earth-World mit genau einem globalen Referenzpunkt anzulegen;
2. das CRS des Referenzpunkts explizit zu speichern;
3. lokale Positionen in globale Positionen umzurechnen;
4. globale Positionen in kanonische lokale Positionen umzurechnen;
5. einen Spawnpunkt anhand globaler Koordinaten zu setzen;
6. in Ost-/West-Richtung die Welt vollständig zu umrunden;
7. an der Weltnaht dieselben Chunk-, Block- und Nachbarschaftsregeln wie innerhalb der Welt zu verwenden;
8. alle Block-, Snapshot-, Event- und Commanddaten weiterhin lokal zu speichern;
9. `flat` und `earth` parallel im selben Service zu betreiben;
10. spätere globale Big-Data- und Geodatenimporte vorzubereiten.

### 4.2 Qualitätsziele

Die Umsetzung muss:

* deterministisch sein;
* keine kumulative Transformationsabweichung erzeugen;
* reproduzierbar über Neustarts hinweg sein;
* thread- und worker-sicher sein;
* mit mehreren Gunicorn-Workern funktionieren;
* Runtime und DB-Bootstrap weiterhin strikt trennen;
* Fehler explizit und maschinenlesbar melden;
* fehlende CRS-Angaben nicht erraten;
* Cache-Inhalte niemals als Datenwahrheit verwenden;
* vorhandene Flat-Welten vollständig kompatibel lassen.

---

## 5. Nicht-Ziele von Earth v1

Earth v1 implementiert ausdrücklich noch nicht:

* eine geometrisch gekrümmte Kugelwelt;
* Cube-Sphere-, S2- oder H3-Voxeladressierung;
* einen Nord-/Süd-Wrap über die Pole;
* automatisch importierte Höhen- oder Geländedaten;
* regionale Big-Data-Datenbanken;
* UTM-Zonenwechsel in der Runtime;
* ein frei drehbares Earth-Chunkraster;
* eine globale Koordinate pro Block;
* eine globale Koordinate pro ChunkSnapshot;
* eine globale Koordinate pro ChunkEvent;
* eine globale Koordinate pro WorldCommandLog;
* automatische CRS-Erkennung aus bloßen Zahlenwerten;
* nachträgliches transparentes Verschieben einer materialisierten Earth-Welt;
* ein Reanchoring bestehender Snapshots;
* eine vollständige CAD-/GIS-Geometrieschicht;
* eine geodätisch exakte Repräsentation aller Erdflächen, Winkel und Distanzen.

Diese Themen dürfen später ergänzt werden, müssen aber neue versionierte Entscheidungen oder neue Provider-/Grid-Versionen erhalten.

---

## 6. Begriffe

### 6.1 `flat`

Der bestehende lokale Welt-Provider ohne globale Referenz und ohne periodische Weltbreite.

### 6.2 `earth`

Der neue Provider für eine flache, global referenzierte und in X-Richtung periodische Welt.

### 6.3 `WorldInstance`

Die konkrete, persistierte Projektwelt. Sowohl eine Flat- als auch eine Earth-Welt kann beispielsweise die konkrete World-ID `world_spawn` besitzen. Der Provider bestimmt den Welt-Typ.

### 6.4 Globale Referenz

Genau ein persistierter Punkt, der festlegt, wo der lokale Ursprung einer Earth-World global liegt.

Die Referenz besteht mindestens aus:

```text
CRS-Identität
X-Koordinate
Y-Koordinate
optionale Z-/Höhenkoordinate
```

Die globale Referenz ist ein einzelnes Koordinatentupel. Sie ist keine Sammlung regionaler Anker.

### 6.5 Earth Grid

Das kanonische, versionierte, flache Welt-Raster, das den globalen Earth-Raum auf ganzzahlige Rasterpositionen abbildet.

### 6.6 Lokale Weltkoordinate

Eine Position relativ zur globalen Referenz der konkreten Earth-World.

### 6.7 Kanonische Position

Die einzige zulässige gespeicherte Darstellung einer physischen Position innerhalb der periodischen Earth-Topologie.

### 6.8 Weltnaht

Der Übergang zwischen dem letzten und dem ersten X-Rasterwert.

### 6.9 Reanchoring

Das Ändern der globalen Referenz einer bereits existierenden Earth-World. Dies verändert die globale Bedeutung sämtlicher lokaler Inhalte und ist kein normaler Spawn- oder Metadaten-Update.

---

## 7. Provider- und World-Identität

### 7.1 Provider-IDs

Verbindliche Provider-IDs:

```text
flat
earth
```

### 7.2 Konkrete World-ID

Die konkrete editierbare Welt kann weiterhin heißen:

```text
world_spawn
```

Es gilt:

```text
worldId = world_spawn
providerId = flat
```

oder:

```text
worldId = world_spawn
providerId = earth
```

Die World-ID darf nicht verwendet werden, um den Provider abzuleiten.

### 7.3 Template- und Provider-Regel

Für Earth v1 gilt:

```text
templateId = earth
providerId = earth
providerWorldId = earth
```

Eine spätere Trennung zwischen Earth-Template und Earth-Provider ist zulässig, muss aber explizit versioniert werden.

### 7.4 Default-Verhalten

`flat` bleibt der Default-Provider für bestehende und nicht ausdrücklich als Earth angelegte Projekte.

Ein Projekt wird nur dann als Earth-Projekt provisioniert, wenn der Client explizit:

```text
worldType = earth
```

und eine gültige globale Referenz übergibt.

Es darf keine implizite oder automatisch erfundene Default-Earth-Referenz geben.

---

## 8. Kanonische Earth-Grid-Definition

### 8.1 Versionierung

Die Earth-Grid-Definition besitzt mindestens:

```text
gridId
gridVersion
projectionId
projectionVersion
axisConvention
worldWidthBlocks
chunkSize
metersPerCell
wrapAxes
northSouthPolicy
```

Die konkrete Definition wird im Earth-Provider versioniert gespeichert und nicht über frei änderbare Runtime-ENV-Werte erzeugt.

### 8.2 Achsen

Earth v1 verwendet:

```text
X = Ost/West
Y = Höhe
Z = Nord/Süd
```

Verbindliche Achsenbezeichnung:

```text
x-east-y-up-z-north
```

### 8.3 Periodische Achse

Earth v1 verwendet:

```text
wrapAxes = ["x"]
```

X ist periodisch. Z ist nicht periodisch.

### 8.4 Z-Verhalten

Earth v1 führt keinen automatischen Polübergang aus.

Der Earth-Provider muss eine explizite `northSouthPolicy` deklarieren. Für Version 1 ist zulässig:

```text
bounded
```

Die genaue nördliche und südliche Grenze wird durch die Earth-Grid-Definition festgelegt.

Bei Überschreitung wird kein stilles Wrap angewendet. Der Zugriff wird mit einem eindeutigen Domänenfehler abgelehnt oder durch eine höherliegende Bewegungslogik begrenzt.

### 8.5 Weltbreite

`worldWidthBlocks` ist:

* eine positive ganze Zahl;
* gerade;
* größer als null;
* durch `chunkSize` teilbar;
* so gewählt, dass auch `worldWidthBlocks / 2` durch `chunkSize` teilbar ist.

Verbindliche Invarianten:

```text
worldWidthBlocks > 0
worldWidthBlocks % 2 == 0
worldWidthBlocks % chunkSize == 0
(worldWidthBlocks / 2) % chunkSize == 0
```

Eine Änderung der Weltbreite erfordert eine neue `gridVersion`.

### 8.6 Zellmaßstab

`metersPerCell` ist Bestandteil der versionierten Griddefinition.

Eine Änderung von `metersPerCell` ist keine Konfigurationsänderung, sondern eine neue Gridversion.

### 8.7 Keine Projektraster-Rotation

Das Earth-Chunkraster besitzt eine globale, unveränderliche Orientierung.

Eine Earth-World darf keine freie `rotation_deg` für ihre Chunk- oder Blockachsen speichern.

Zulässig sind später separate lokale CAD-, Objekt- oder Bauraster, die relativ zur Earth-Welt gedreht werden. Diese verändern das Earth-Chunkraster nicht.

---

## 9. Persistenzprinzip: eine globale Referenz, lokale Inhalte

### 9.1 Persistierte globale Daten

Pro Earth-`WorldInstance` wird genau eine globale Referenz gespeichert:

```text
referenceCrs
referenceX
referenceY
referenceZ optional
```

Zusätzlich werden Referenz- und Gridmetadaten gespeichert:

```text
gridId
gridVersion
axisConvention
status
revision
```

Diese Metadaten sind keine weiteren globalen Punkte.

### 9.2 Nicht persistierte abgeleitete Werte

Nicht als zusätzliche Datenwahrheit persistiert werden:

* globale Rasterkoordinate jedes Blocks;
* globale Rasterkoordinate jedes Chunks;
* globale Koordinate jedes Events;
* globale Koordinate jedes Commands;
* regionale Referenzpunkte;
* ein Earth-Anker je Chunk;
* ein Earth-Anker je Region;
* ein Earth-Anker je Objekt;
* ein transformierter Referenzpunkt in mehreren CRS als gleichrangige Wahrheit.

Abgeleitete Werte können kurzzeitig berechnet und gecacht werden. Der Cache ist nie die Persistenzwahrheit.

### 9.3 Lokale Persistenz

Weiterhin lokal persistiert werden:

```text
WorldCommand-Position
ChunkSnapshot-Chunkadresse
ChunkEvent-Position
WorldObject-Position
Spawnposition
Spielerposition, soweit im Chunk-Service gespeichert
```

### 9.4 Warum keine globale Position pro Block gespeichert wird

Das redundante Speichern lokaler und globaler Positionen würde zwei Wahrheiten erzeugen:

```text
lokal gespeichert
+
global gespeichert
```

Bei Änderungen an Referenz, Gridversion, Projektionslogik oder Rundungsregeln könnten diese Werte auseinanderlaufen.

Daher gilt:

```text
lokale Position = operative Persistenzwahrheit
globale Position = deterministisch abgeleitet
```

---

## 10. Globaler Referenzpunkt

### 10.1 Pflichtfelder

Eine Earth-Welt benötigt vor Aktivierung:

```text
referenceCrsId oder referenceCrsWkt
referenceX
referenceY
optional referenceZ
gridId
gridVersion
```

### 10.2 CRS-Vertrag

Das CRS muss:

* explizit angegeben werden;
* durch die verwendete CRS-Bibliothek ladbar sein;
* eine eindeutige Achsen- und Einheitendefinition besitzen;
* für die angegebene Koordinatendimension geeignet sein;
* auf eine erlaubte kanonische Earth-Referenz transformierbar sein.

Die Implementierung muss bei Transformationen eine explizite Achsenreihenfolge verwenden. Implizite Achsenwechsel sind unzulässig.

### 10.3 Kein numerisches CRS-Raten

Folgendes ist verboten:

```text
Koordinatenwerte ansehen
→ vermuten, dass es EPSG:25832, EPSG:4326 oder ein anderes CRS ist
```

Zulässig ist:

```text
CRS aus verlässlichen Dateimetadaten lesen
```

oder:

```text
CRS explizit durch den Client angeben
```

Fehlen CRS-Metadaten, muss die Operation abbrechen und eine explizite Angabe verlangen.

### 10.4 Zwei- und dreidimensionale Referenzen

Eine globale Referenz kann 2D oder 3D sein.

#### 2D-Referenz

```text
referenceX
referenceY
referenceZ = null
```

Lokale Y-Werte bleiben relativ zu einem weltlokalen Höhendatum. Eine absolute globale Höhe darf daraus nicht behauptet werden.

#### 3D-Referenz

```text
referenceX
referenceY
referenceZ
```

Das CRS oder ein zusätzlicher vertikaler Referenzvertrag muss eindeutig beschreiben, was `referenceZ` bedeutet.

### 10.5 Referenzpräzision

Der Referenzpunkt wird mit ausreichender Dezimalpräzision gespeichert. Die Datenbankrepräsentation darf keine unnötige Rundung auf das Blockraster erzwingen.

Der exakte Referenzpunkt bleibt erhalten. Die Zuordnung zum Earth-Grid wird deterministisch daraus berechnet.

---

## 11. Lokales Koordinatenmodell

### 11.1 Lokale Blockpositionen

Lokale Blockpositionen verwenden ganze Zahlen:

```text
localX: int64
localY: int32 oder int64 gemäß bestehendem Modell
localZ: int64
```

### 11.2 Lokale Sub-Block-Positionen

Spieler, Kameras oder bewegliche Objekte dürfen Sub-Block-Positionen besitzen. Diese Entscheidung ändert nicht die ganzzahlige Adressierung von Chunkzellen.

### 11.3 Kanonischer X-Bereich

Für die lokale X-Koordinate gilt:

```text
-halfWorld <= localX < halfWorld
```

mit:

```text
halfWorld = worldWidthBlocks / 2
```

Beispiele:

```text
localX = 0
localX = worldWidthBlocks
localX = -worldWidthBlocks
```

bezeichnen denselben physischen Ort, dürfen aber nicht als drei unterschiedliche persistierte Adressen bestehen.

Alle Werte werden vor Chunkauflösung und Persistenz kanonisiert.

### 11.4 Antipodaler Grenzfall

Der Punkt exakt eine halbe Weltbreite vom Referenzpunkt entfernt kann als:

```text
+halfWorld
```

oder:

```text
-halfWorld
```

dargestellt werden.

Earth v1 verwendet verbindlich:

```text
-halfWorld
```

als kanonische Darstellung.

`+halfWorld` wird zu `-halfWorld` normalisiert.

### 11.5 Normalisierungsreihenfolge

Verbindlich:

```text
rohe lokale Position
→ Provider/Topologie auflösen
→ X kanonisieren
→ Z-Grenze prüfen
→ Chunkkoordinate berechnen
→ lokale Zellkoordinate berechnen
→ Chunk-Key erzeugen
→ DB lesen oder schreiben
```

Verboten:

```text
rohe lokale Position
→ Chunk-Key erzeugen
→ DB lesen oder schreiben
→ erst danach X normalisieren
```

Diese Reihenfolge ist eine harte Datenintegritätsinvariante.

---

## 12. Umrechnung lokal zu global

Die globale Position wird aus folgenden Eingaben berechnet:

```text
persistierter Referenzpunkt
+ Earth-Grid-Definition
+ lokale Position
```

Konzeptioneller Ablauf:

```text
Referenzpunkt im Quell-CRS
→ kanonische Earth-Rasterposition der Referenz bestimmen
→ lokalen Offset addieren
→ X periodisch normalisieren
→ auf Wunsch in ein Ziel-CRS transformieren
```

Die Umrechnung darf die lokale Position nicht verändern oder überschreiben.

Die globale Ergebnisposition ist abgeleitet und wird standardmäßig nicht persistiert.

---

## 13. Umrechnung global zu lokal

Eingaben:

```text
globale Zielkoordinate
+ CRS der Zielkoordinate
+ persistierter Referenzpunkt
+ Earth-Grid-Definition
```

Ablauf:

```text
globale Zielkoordinate in kanonisches Earth-Grid transformieren
→ Differenz zum Referenzanker bilden
→ kürzesten signierten X-Abstand bestimmen
→ antipodalen Grenzfall kanonisieren
→ Z-Grenze prüfen
→ lokale Position zurückgeben
```

### 13.1 Kürzester periodischer Abstand

Für X wird der kürzeste signierte Abstand zur Referenz verwendet.

Dadurch wird ein Ziel direkt östlich über der Weltnaht nicht als nahezu eine gesamte Weltbreite westlich interpretiert.

### 13.2 Mehrdeutigkeit bei Weltumrundungen

Eine physische Position enthält keine Information darüber, wie oft sie umrundet wurde.

Daher werden getrennt:

```text
kanonische Position
→ wo befindet sich der Nutzer?

Bewegungshistorie
→ welchen Weg hat der Nutzer zurückgelegt?
```

Eine mögliche spätere Rundenzahl oder unwrapped Bewegungskoordinate gehört nicht in die statische Block- oder Chunkadresse.

---

## 14. Chunkadressierung und Weltnaht

### 14.1 Chunkbreite der Welt

Die Anzahl der Earth-Chunks in X ist:

```text
worldWidthChunks = worldWidthBlocks / chunkSize
```

### 14.2 Kanonischer Chunkbereich

Analog zur Blockposition besitzt die Earth-Welt einen kanonischen X-Chunkbereich.

Mehrere angefragte Chunkadressen, die sich um ein ganzzahliges Vielfaches von `worldWidthChunks` unterscheiden, bezeichnen denselben physischen Chunk.

### 14.3 Eindeutige Snapshot-Identität

Vor jedem Snapshot-Lookup oder Snapshot-Write wird die Chunkadresse kanonisiert.

Es darf nicht möglich sein, gleichzeitig Snapshots für physisch äquivalente Adressen anzulegen.

Beispiel:

```text
chunkX = 0
chunkX = worldWidthChunks
chunkX = -worldWidthChunks
```

müssen auf genau dieselbe persistente Chunkidentität zeigen.

### 14.4 Batch-Deduplizierung

Eine Batch-Anfrage kann mehrere äquivalente Earth-Adressen enthalten.

Die Anwendung muss:

1. jede Adresse kanonisieren;
2. äquivalente Adressen deduplizieren;
3. den physischen Chunk nur einmal laden oder generieren;
4. im Response eindeutig dokumentieren, welche Anfrage auf welche kanonische Adresse abgebildet wurde.

### 14.5 Nachbarschaft an der Naht

Der östliche Nachbar des letzten X-Chunks ist der erste X-Chunk.

Der westliche Nachbar des ersten X-Chunks ist der letzte X-Chunk.

Diese Regel gilt für:

* Chunkloading;
* Meshing;
* Kollision;
* Navigation;
* Dirty-Chunk-Berechnung;
* Nachbarblockabfragen;
* Objektüberschneidungen;
* spätere Bereichsoperationen.

---

## 15. Dirty-Chunks

Dirty-Chunks werden nach der kanonischen Position berechnet.

Eine Änderung an einer Zelle am östlichen Rand des letzten X-Chunks markiert:

```text
aktuellen letzten X-Chunk
+
ersten X-Chunk
```

Eine Änderung am westlichen Rand des ersten X-Chunks markiert:

```text
aktuellen ersten X-Chunk
+
letzten X-Chunk
```

Dirty-Chunk-Listen müssen:

* kanonische Chunk-Keys enthalten;
* dedupliziert sein;
* stabil sortiert oder anderweitig deterministisch serialisiert werden;
* niemals zwei verschiedene Keys für denselben physischen Chunk enthalten.

---

## 16. Spawnmodell

### 16.1 Persistenz

Der Spawn wird lokal relativ zur Earth-Referenz gespeichert:

```text
spawnLocalX
spawnLocalY
spawnLocalZ
optional heading/orientation
revision
```

### 16.2 Spawn über lokale Koordinate

Ein Client kann einen lokalen Spawn setzen. Die Position wird vor Persistenz kanonisiert und validiert.

### 16.3 Spawn über globale Koordinate

Ein Client kann eine globale Zielkoordinate einschließlich CRS übergeben.

Die Anwendung:

1. validiert das CRS;
2. transformiert die Zielkoordinate in das Earth-Grid;
3. berechnet den kanonischen lokalen Offset zur World-Referenz;
4. speichert ausschließlich den lokalen Spawn;
5. gibt optional die berechnete globale Position zur Bestätigung zurück.

### 16.4 Spawnänderung ist kein Reanchoring

Eine Spawnänderung darf niemals:

* den globalen Referenzpunkt ändern;
* Chunkadressen umschreiben;
* Snapshots verschieben;
* Events neu interpretieren;
* Objekte global verschieben.

---

## 17. Referenzimmutabilität und Reanchoring

### 17.1 Vor Materialisierung

Solange eine Earth-Welt keine fachlichen Inhalte besitzt, darf die Referenz korrigiert werden.

Als nicht materialisiert gilt nur eine Welt ohne:

* aktive ChunkSnapshots;
* ChunkEvents;
* WorldCommandLogs mit angewandten Änderungen;
* WorldObjectInstances;
* andere persistierte positionsabhängige Inhalte.

### 17.2 Nach Materialisierung

Sobald positionsabhängige Inhalte existieren, ist die Referenz gesperrt.

Ein normaler Updateversuch liefert:

```text
HTTP 409
code = world_reference_locked
```

### 17.3 Reanchoring

Ein späteres Reanchoring ist eine explizite Migrationsoperation mit:

* eigener Berechtigung;
* eigenem Command-/Jobtyp;
* Vorabprüfung;
* exklusiver Sperre;
* vollständigem Audit-Log;
* Dry-Run;
* Rollbackstrategie;
* definierter Behandlung aller positionsabhängigen Modelle.

Reanchoring ist nicht Bestandteil von Earth v1.

---

## 18. Transaktionen und Nebenläufigkeit

### 18.1 Earth-World-Erstellung

Folgende Schritte müssen in einer äußeren Transaktion erfolgen:

```text
Project/Universe auflösen oder erzeugen
→ WorldInstance erzeugen
→ WorldGeoreference erzeugen
→ WorldSpawnPosition erzeugen
→ validieren
→ flushen
→ äußerer Commit
```

Fehlschlägt ein Schritt:

```text
äußerer Rollback
```

Es darf keine aktive Earth-World ohne Referenz oder ohne Spawn zurückbleiben.

### 18.2 Commit-Verantwortung

Domänen- und Hilfsdienste dürfen:

* validieren;
* lesen;
* schreiben;
* flushen;
* verschachtelte Transaktionen verwenden, wenn begründet.

Sie führen keinen unkoordinierten äußeren Commit aus.

Die äußere Application-/Bootstrap-Schicht besitzt Commit und Rollback.

### 18.3 Optimistische Nebenläufigkeit

Georeferenz und Spawn besitzen eine Revision.

Updates müssen optional oder verpflichtend eine erwartete Revision unterstützen.

Bei Konflikt:

```text
HTTP 409
code = coordinate_frame_revision_conflict
```

beziehungsweise:

```text
HTTP 409
code = spawn_revision_conflict
```

### 18.4 Idempotenz

Wiederholtes Earth-Provisioning mit identischen kanonischen Daten ist idempotent.

Wiederholtes Provisioning mit abweichender Referenz für dieselbe bestehende WorldInstance darf nicht still überschreiben.

Ergebnis:

```text
HTTP 409
code = earth_reference_conflict
```

---

## 19. Fehlerbehandlung und Try/Except-Vertrag

### 19.1 Grundregel

Fehler werden nicht durch breite, ungezielte `except Exception`-Blöcke verschluckt.

Jede Schicht fängt nur Fehler ab, die sie sinnvoll:

* in einen Domänenfehler übersetzen;
* mit Kontext anreichern;
* transaktional zurückrollen;
* als HTTP-Fehler serialisieren;
* oder kontrolliert als nicht-fatal behandeln kann.

### 19.2 Domänenfehler

Mindestens folgende stabile Fehlercodes werden vorgesehen:

```text
earth_provider_disabled
earth_provider_not_ready
earth_grid_not_ready
earth_grid_version_unsupported
earth_world_reference_required
earth_reference_invalid
earth_reference_conflict
world_reference_locked
coordinate_crs_required
coordinate_crs_invalid
coordinate_crs_unsupported
coordinate_transform_failed
coordinate_transform_not_exact
coordinate_out_of_bounds
north_south_boundary_exceeded
world_width_invalid
world_width_not_chunk_aligned
chunk_address_invalid
chunk_address_noncanonical
coordinate_frame_revision_conflict
spawn_revision_conflict
flat_world_has_no_global_reference
```

### 19.3 Datenbankfehler

Bei Datenbankfehlern:

1. spezifischen Fehler erfassen;
2. Session beziehungsweise äußere Transaktion zurückrollen;
3. strukturiert loggen;
4. keine halbfertigen Modelle weiterverwenden;
5. generischen internen Fehler nach außen geben, sofern keine sichere fachliche Übersetzung möglich ist.

Interne SQL- oder Constraint-Details werden nicht ungefiltert an Clients ausgegeben.

### 19.4 Transformationsfehler

Fehlschlägt eine CRS-Transformation:

* kein Fallback auf geratenes CRS;
* kein stilles Verwenden der Eingabekoordinate;
* kein Ballpark-Fallback ohne explizite Policy;
* keine teilweise Persistenz;
* eindeutiger Fehler mit Quell-CRS, Zieldefinition und Korrelations-ID im Log.

### 19.5 Unerwartete Fehler

Unerwartete Fehler werden an der äußeren HTTP-, CLI- oder Workergrenze abgefangen, geloggt und in eine sichere generische Antwort übersetzt.

Der Originalfehler bleibt mit Stacktrace in internen Logs erhalten.

---

## 20. Cache-Vertrag

### 20.1 Cache-Zweck

Berechnet und gecacht werden dürfen:

* aufgelöste CRS-Objekte;
* wiederverwendbare Transformer;
* validierte Earth-Grid-Definitionen;
* aus Referenz und Gridversion berechnete Runtime-Frames;
* Provider- und Topologieobjekte.

### 20.2 Cache-Key

Ein Runtime-Frame-Cache muss mindestens enthalten:

```text
worldInstanceId
georeferenceRevision
gridId
gridVersion
```

### 20.3 Invalidation

Der Cache wird invalidiert bei:

* Änderung der Georeferenz;
* Änderung der Georeferenzrevision;
* Providerwechsel;
* Gridversionswechsel;
* explizitem Cache-Reset;
* Prozessneustart.

### 20.4 Cache ist keine Wahrheit

Ein Cache-Miss muss durch erneute Berechnung aus persistierten Daten auflösbar sein.

Ein Cache darf keine nicht persistierten Änderungen dauerhaft verstecken.

### 20.5 Cache-Fehler

Fehler beim Lesen oder Schreiben eines optionalen Caches dürfen die Datenintegrität nicht verändern.

Wenn der Cache nicht verfügbar ist:

```text
aus Persistenz lesen
→ neu berechnen
→ normal fortfahren
```

Cache-Fehler werden beobachtbar geloggt, aber nicht mit fachlichen Transformationsfehlern verwechselt.

---

## 21. API-Vertrag

### 21.1 Coordinate Frame lesen

```text
GET /projects/<project_id>/worlds/<world_id>/coordinate-frame
```

Flat-Welt:

```text
HTTP 409 oder 422
code = flat_world_has_no_global_reference
```

Earth-Welt:

```text
HTTP 200
```

Antwort enthält:

* World- und Provideridentität;
* Referenz-CRS;
* Referenzkoordinate;
* Grid-ID und Gridversion;
* Achsenkonvention;
* Topologie;
* Revision;
* Lockstatus;
* Gründe für einen Lock;
* lokale Spawnposition;
* optionale berechnete globale Spawnposition.

### 21.2 Coordinate Frame setzen

```text
PUT /projects/<project_id>/worlds/<world_id>/coordinate-frame
```

Zulässig:

* während atomarer Earth-Erstellung;
* bei noch nicht materialisierter Earth-Welt;
* mit korrekter erwarteter Revision.

Nicht zulässig:

* für Flat-Welten;
* ohne CRS;
* nach Materialisierung;
* bei Gridversionskonflikt.

### 21.3 Lokal zu global

```text
POST /projects/<project_id>/worlds/<world_id>/coordinates/local-to-global
```

Diese Route ist read-only.

### 21.4 Global zu lokal

```text
POST /projects/<project_id>/worlds/<world_id>/coordinates/global-to-local
```

Diese Route ist read-only.

### 21.5 Spawn ändern

```text
PATCH /projects/<project_id>/worlds/<world_id>/spawn
```

Zulässige Eingabevarianten:

```text
lokale Position
```

oder:

```text
globale Position + CRS
```

Genau eine Variante muss vorhanden sein.

### 21.6 Antwortversionierung

Neue Earth-bezogene Responses besitzen explizite Versionen, beispielsweise:

```text
earth-coordinate-frame-response.v1
earth-coordinate-transform-response.v1
earth-spawn-response.v1
```

---

## 22. Runtime- und Bootstrap-Regeln

### 22.1 Runtime

Die Runtime bleibt read-only bezüglich Schema und Seed.

Runtime darf:

* Earth-Provider laden;
* Earth-Grid validieren;
* Georeferenz lesen;
* Koordinaten transformieren;
* Commands und fachliche Änderungen verarbeiten;
* Spawn fachlich ändern, sofern autorisiert.

Runtime darf nicht:

* Tabellen erzeugen;
* Migrationen ausführen;
* fehlende Referenzzeilen automatisch erfinden;
* Earth-Welten ohne explizite Referenz reparieren;
* Gridversionen still ändern.

### 22.2 Bootstrap

DB-Bootstrap beziehungsweise Migration darf:

* Earth-Tabellen erzeugen;
* Constraints und Indizes anlegen;
* Provider-Readiness prüfen;
* bestehende Flat-Daten unverändert lassen.

Bootstrap darf keine globale Referenz für vorhandene Flat-Worlds erfinden.

### 22.3 Readiness

Getrennte Readiness-Signale:

```text
earthProviderReady
earthGridReady
earthCrsRuntimeReady
```

Eine nicht verfügbare Earth-Funktion darf die bestehende Flat-Defaultwelt nicht unnötig als unready markieren.

Empfohlen:

```text
flatServiceReady = true
earthFeatureReady = false
```

ist ein zulässiger degradierter Zustand, sofern kein aktives Earth-Projekt bedient werden muss.

---

## 23. Migration und Kompatibilität

### 23.1 Neue Tabellen

Vorgesehen:

```text
world_georeferences
world_spawn_positions
```

### 23.2 Bestehende Flat-Welten

Bestehende Flat-Welten:

* erhalten keine künstliche Georeferenz;
* erhalten nur dann eine neue Spawnzeile, wenn dies migrationsseitig erforderlich und semantisch eindeutig ist;
* behalten bestehende Chunk-Keys;
* behalten bestehende Snapshots;
* behalten bestehende Events;
* behalten bestehendes Providerverhalten.

### 23.3 Keine Änderung bestehender Zellwerte

Earth verändert nicht:

```text
cellValue = 0 → Air
cellValue = paletteIndex + 1 → Block
```

### 23.4 Kein automatischer Providerwechsel

Eine bestehende Flat-Welt wird nicht automatisch in Earth umgewandelt.

Eine spätere Konvertierung `flat → earth` ist eine explizite Migrationsfunktion und nicht Bestandteil von Earth v1.

---

## 24. Sicherheit und Berechtigungen

Mindestens folgende Operationen benötigen getrennte Berechtigungen:

```text
Earth-Welt erstellen
Coordinate Frame setzen oder ändern
globalen Spawn setzen
lokalen Spawn setzen
zukünftiges Reanchoring
```

Die globale Referenz kann fachlich sensible Standortdaten enthalten. API-Antworten und Logs dürfen sie nur entsprechend der Projektberechtigung ausgeben.

Logs sollen Koordinaten nicht unnötig in hoher Präzision ausgeben, wenn dies für die Diagnose nicht erforderlich ist.

---

## 25. Observability

### 25.1 Strukturierte Logs

Relevante Felder:

```text
projectId
worldId
providerId
gridId
gridVersion
coordinateFrameRevision
requestedChunkKey
canonicalChunkKey
requestedLocalPosition
canonicalLocalPosition
sourceCrs
targetCrs
operation
durationMs
cacheHit
errorCode
correlationId
```

### 25.2 Metriken

Empfohlene Metriken:

```text
earth_coordinate_transform_total
earth_coordinate_transform_failed_total
earth_coordinate_transform_duration
earth_coordinate_frame_cache_hit_total
earth_coordinate_frame_cache_miss_total
earth_chunk_address_normalized_total
earth_batch_address_deduplicated_total
earth_world_seam_access_total
earth_spawn_update_total
earth_reference_update_rejected_total
```

### 25.3 Keine Kardinalitätsexplosion

Exakte Koordinaten, Projekt-IDs oder Chunk-Keys dürfen nicht unkontrolliert als Metriklabels verwendet werden.

---

## 26. Testvertrag

### 26.1 Koordinateninvarianten

Für beliebiges ganzzahliges `n`:

```text
normalizeX(x) == normalizeX(x + n * worldWidthBlocks)
```

Für Chunkadressen:

```text
normalizeChunkX(cx) == normalizeChunkX(cx + n * worldWidthChunks)
```

### 26.2 Roundtrip

Innerhalb definierter Toleranz:

```text
global → local → global
```

und:

```text
local → global → local
```

müssen stabil sein.

Bei Rasterquantisierung muss die Toleranz explizit dokumentiert werden.

### 26.3 Weltnaht

Zu testen:

* letzter Block → östlicher Nachbar ist erster Block;
* erster Block → westlicher Nachbar ist letzter Block;
* letzter Chunk → östlicher Nachbar ist erster Chunk;
* SetBlock an der Naht;
* Reload von der äquivalenten Gegenseite;
* RemoveBlock von der Gegenseite;
* Dirty-Chunks über der Naht;
* Objekt oder Struktur über der Naht.

### 26.4 Doppelte Adressen

Zu beweisen:

```text
x
x + worldWidthBlocks
x - worldWidthBlocks
```

führen zur selben physischen Zelle und niemals zu mehreren Snapshots.

### 26.5 Negative Koordinaten

Negative lokale Koordinaten müssen mit Floor-Division und kanonischer Normalisierung korrekt auf Chunk und Zelle abgebildet werden.

### 26.6 Spawn

Zu testen:

* lokaler Spawn;
* globaler Spawn;
* Spawn direkt über der Weltnaht;
* Spawn am antipodalen Grenzfall;
* Spawnupdate mit Revision;
* Spawnupdate verändert keine Referenz;
* Spawnupdate verändert keine Snapshots.

### 26.7 Referenzschutz

Zu testen:

* Referenzänderung ohne Inhalte erlaubt;
* Referenzänderung mit Snapshot blockiert;
* Referenzänderung mit Event blockiert;
* Referenzänderung mit Objekt blockiert;
* abweichendes idempotentes Provisioning liefert Konflikt.

### 26.8 Flat-Regression

Alle bestehenden Flat-Tests müssen unverändert erfolgreich bleiben.

Flat darf:

* keine X-Normalisierung erhalten;
* keine Earth-Referenz verlangen;
* keine Earth-Grenzen anwenden;
* keine Earth-spezifischen Responsefelder als Pflicht bekommen.

---

## 27. Datenintegritätsinvarianten

Folgende Regeln sind verbindlich:

1. Eine aktive Earth-World besitzt genau eine aktive Georeferenz.
2. Eine Flat-World benötigt keine Georeferenz.
3. Eine Earth-Georeferenz gehört genau einer WorldInstance.
4. Eine WorldInstance besitzt höchstens eine aktive Spawnposition.
5. Earth-Chunkadressen werden vor jedem DB-Zugriff kanonisiert.
6. Persistierte Earth-Snapshots verwenden ausschließlich kanonische Chunkadressen.
7. Persistierte Earth-Events verwenden ausschließlich kanonische lokale Positionen.
8. Persistierte Earth-Commands verwenden ausschließlich kanonische lokale Positionen.
9. Der Earth-Provider verwendet ein global einheitliches Raster.
10. Region, Land, Datenquelle oder Chunk verändern das Raster nicht.
11. Das CRS des Referenzpunkts wird nie aus Zahlenwerten geraten.
12. Der Referenzpunkt wird nach Materialisierung nicht normal geändert.
13. Der Spawn wird lokal gespeichert.
14. Eine Spawnänderung verändert die Referenz nicht.
15. Cache-Daten sind nie Persistenzwahrheit.
16. Griddefinitionen werden versioniert.
17. Eine Gridänderung erfolgt nie still unter derselben Gridversion.
18. `flat` bleibt die bestehende Defaultwelt.
19. Earth v1 wrappt ausschließlich X.
20. Z-Grenzen werden explizit behandelt und nicht still gewrappt.

---

## 28. Abgelehnte Alternativen

### 28.1 Globale Koordinate für jeden Block speichern

Abgelehnt, weil:

* redundante Datenwahrheit;
* hoher Speicherbedarf;
* Driftgefahr;
* unnötige Migrationen bei Gridänderungen;
* lokale Chunkmechanik bereits ausreicht.

### 28.2 Einen globalen Anker pro Chunk speichern

Abgelehnt, weil:

* regionale Übergänge entstehen;
* Anker auseinanderlaufen können;
* unnötige Komplexität;
* Weltnaht und Gebäude über Chunkgrenzen schwieriger werden.

### 28.3 Regionale CRS in der Runtime wechseln

Abgelehnt, weil:

* Zonen- und Grenzübergänge;
* potenzielle Abweichungen;
* komplizierte Gebäude- und Chunküberschneidungen;
* schwer reproduzierbare Analysen.

### 28.4 X und Z in Version 1 wrappen

Abgelehnt, weil:

* ein Torus statt eines erdähnlichen Zylindermodells entsteht;
* Polsemantik unklar bleibt;
* zusätzliche Nachbarschafts- und Orientierungsregeln erforderlich wären.

### 28.5 Echte Kugelwelt in Version 1

Abgelehnt, weil:

* regelmäßige unverformte Würfel keine Kugel lückenlos überdecken;
* lokale Rasterrotationen oder Spezialzellen notwendig wären;
* erheblich höhere Komplexität ohne aktuellen Bedarf.

### 28.6 Weltbreite aus ENV

Abgelehnt, weil:

* verschiedene Instanzen unterschiedliche physische Welten bedienen könnten;
* Snapshots deploymentabhängig interpretiert würden;
* Datenintegrität nicht gewährleistet wäre.

### 28.7 Referenzänderung als normales PATCH

Abgelehnt, weil:

* sämtliche lokalen Inhalte global verschoben würden;
* dies keine harmlose Metadatenänderung ist;
* eine kontrollierte Migration erforderlich ist.

---

## 29. Konsequenzen

### 29.1 Positive Konsequenzen

* nur ein globaler Referenzpunkt pro Earth-Welt;
* lokale Persistenz bleibt einfach und performant;
* keine globalen Blockkoordinaten müssen gespeichert werden;
* keine regionalen Rasterwechsel;
* exakte Weltnaht im Spielraster;
* bestehende Snapshot-/Event-/Commandarchitektur bleibt nutzbar;
* globale Spawnpunkte sind möglich;
* spätere Geodatenimporte sind vorbereitet;
* `flat` bleibt stabil.

### 29.2 Negative Konsequenzen

* globale und lokale Umrechnung benötigt eine zusätzliche Domänenschicht;
* alle positionsbezogenen Routen müssen providerabhängig normalisieren;
* Weltbreite und Gridversion werden harte Datenverträge;
* die Referenz kann nach Materialisierung nicht einfach geändert werden;
* globale Earth-Abbildung bleibt projektionsbedingt von einer echten Kugelgeometrie verschieden;
* Z-Polübergänge sind in Version 1 nicht enthalten.

### 29.3 Bewusster Trade-off

Earth v1 priorisiert:

```text
einheitliches Raster
+ exakte lokale Voxelgeometrie
+ stabile Persistenz
+ einfache Umrundung in X
```

gegenüber:

```text
vollständig kugelgetreuer globaler Geometrie
```

---

## 30. Implementierungsreihenfolge

Die Umsetzung folgt verbindlich dieser Reihenfolge:

```text
1. ADR-earth-world-v1.md
2. gemeinsamer Koordinatenkern
3. Topologiestrategien
4. Georeferenzierungsverträge
5. CRS-Validierung und Transformation
6. Earth-Grid-Abbildung
7. Earth-Provider
8. Migrationen und Modelle
9. World-State CoordinateService
10. Bootstrap und Readiness
11. Earth-Provisioning
12. Chunk-Lesepfad
13. Command-Schreibpfad
14. Spawn über globale Koordinaten
15. Reanchor-Schutz
16. Editor-/App-Vertrag
17. End-to-End-Tests
18. Dokumentationsaktualisierung
```

Keine Route darf vor dem gemeinsamen Koordinaten- und Topologiekern eigene Earth-Mathematik implementieren.

---

## 31. Definition of Done für Earth v1

Earth v1 gilt als fertig, wenn:

1. `flat` unverändert funktioniert.
2. `earth` als zweiter Provider registriert ist.
3. eine Earth-World genau einen globalen Referenzpunkt besitzt.
4. das CRS der Referenz explizit gespeichert wird.
5. Grid-ID und Gridversion gespeichert werden.
6. lokale Inhalte weiterhin ausschließlich lokal persistiert werden.
7. globale Positionen deterministisch abgeleitet werden.
8. X exakt periodisch ist.
9. äquivalente lokale Positionen dieselbe kanonische Adresse ergeben.
10. keine doppelten Snapshots an der Weltnaht entstehen.
11. Dirty-Chunks über der Weltnaht korrekt funktionieren.
12. globale Spawnziele in lokale Spawnwerte übersetzt werden.
13. Spawnänderungen keine Weltinhalte verschieben.
14. die Referenz nach Materialisierung gesperrt ist.
15. alle Transformationsfehler explizit behandelt werden.
16. Cache-Ausfälle keine Datenintegrität beeinflussen.
17. Migrationen bestehende Flat-Daten erhalten.
18. Runtime weiterhin ohne Schema- oder Seedmutation startet.
19. alle Flat-Regressionstests erfolgreich sind.
20. alle Earth-Naht-, Roundtrip-, Spawn- und Konflikttests erfolgreich sind.

---

## 32. Nachfolgende Entscheidungen

Folgende Themen benötigen bei Umsetzung eine eigene ADR oder eine neue Earth-Grid-Version:

* echte Kugel- oder Cube-Sphere-Welt;
* Polübergang;
* X- und Z-Wrap;
* Reanchoring materialisierter Welten;
* Flat-zu-Earth-Migration;
* Big-Data-Terrainimport;
* vertikale Referenzsysteme und Höhengitter;
* globale Struktursegmentierung;
* lokale gedrehte CAD-/Bauraster;
* Änderung von Weltbreite oder Zellmaßstab;
* Earth Grid v2.

---

## 33. Verbindlicher Leitsatz

```text
Eine Earth-World speichert genau einen globalen Referenzpunkt
mit explizitem Koordinatensystem.

Alle veränderlichen Weltinhalte bleiben lokal relativ zu dieser Referenz.

Das Earth-Raster ist global einheitlich, versioniert und in X periodisch.

Jede Position wird vor Chunkauflösung und Persistenz kanonisiert.
```
