Der folgende Fahrplan hält `flat` vollständig stabil und führt `earth` als parallelen, versionierten Welt-Provider ein. Grundlage ist: **eine globale Referenz pro konkreter Earth-WorldInstance; sämtliche Chunks, Blöcke, Spielerpositionen und Events bleiben lokal**.

# Fahrplan: Welt-Typ `earth`

## 1. Zielzustand

Der bestehende Welt-Typ bleibt unverändert:

```text
world_spawn
→ konkrete WorldInstance

provider/template = flat
→ lokale, flache, nicht georeferenzierte Welt
```

Der neue Welt-Typ verwendet dieselbe konkrete World-ID-Struktur, aber einen anderen Provider:

```text
world_spawn
→ konkrete WorldInstance

provider/template = earth
→ flache, global referenzierte, in X-Richtung umlaufende Welt
```

`world_spawn` darf künftig also nicht mehr automatisch „Flat-Provider“ bedeuten. Die bestehende Architektur trennt konkrete Welt und Provider bereits grundsätzlich voneinander. 

Für eine Earth-World wird gespeichert:

```text
1 globaler Referenzpunkt
+ CRS dieses Referenzpunkts
+ Version des globalen Earth-Rasters
+ lokaler Spawnpunkt
```

Nicht gespeichert werden:

```text
keine globale Koordinate pro Block
keine globale Koordinate pro Chunk
keine globale Koordinate pro Event
kein regionales CRS pro Chunk
kein zusätzlicher Erdanker pro Region
```

Alle Inhalte bleiben relativ zur einen Referenz lokal gespeichert. Dieses Prinzip entspricht einem einmal definierten Projektkoordinatensystem, das bei Chunk- oder Regionswechseln nicht verändert wird. 

---

# 2. Vor der Implementierung verbindlich festzulegende Invarianten

Diese Entscheidungen müssen als Architekturvertrag feststehen, bevor Modelle oder Routen geändert werden.

## 2.1 Earth-Topologie Version 1

Für Version 1:

```text
X = Ost/West, periodisch
Y = Höhe
Z = Nord/Süd, begrenzt oder zunächst technisch unbeschränkt
```

Das entspricht einer flachen Zylinderwelt. Der letzte X-Block und der erste X-Block sind direkte Nachbarn. Es gibt an der Naht keine Transformation und keine Lücke. 

Für Version 1 sollte **nur X umlaufen**. X- und Z-Umlauf gemeinsam würden einen Torus erzeugen und ein anderes Weltmodell darstellen.

## 2.2 Ein einziges Earth-Raster

Alle Earth-Projekte verwenden dieselbe Rasterdefinition:

```text
gridId
gridVersion
axisConvention
worldWidthBlocks
chunkSize
metersPerCell
wrapAxes
projectionDefinition
```

Diese Definition gehört in den Earth-Provider und nicht individuell in jedes Projekt.

Ein Projekt speichert nur:

```text
welche Version dieses Rasters verwendet wird
+
an welchem globalen Punkt sein lokaler Ursprung liegt
```

## 2.3 Keine freie Projektraster-Rotation

Das Earth-Chunkraster muss überall gleich ausgerichtet sein.

Nicht zulässig:

```text
Projekt A dreht seine Chunks um 17°
Projekt B dreht seine Chunks um 35°
```

Eine solche Rotation würde das weltweit einheitliche Raster zerstören.

Spätere CAD- oder Bauraster dürfen lokal gedreht sein. Das eigentliche Earth-Chunkraster nicht.

## 2.4 Kanonischer lokaler X-Bereich

Mehrere lokale Werte können nach dem Umlauf denselben Ort bezeichnen:

```text
x = 0
x = WORLD_WIDTH
x = 2 × WORLD_WIDTH
```

Deshalb muss genau ein kanonischer Bereich definiert werden:

```text
-WORLD_WIDTH / 2 <= localX < WORLD_WIDTH / 2
```

Jede lokale Position wird vor der Chunk-Berechnung in diesen Bereich normalisiert.

## 2.5 Weltbreite muss zur Chunkgröße passen

Es muss gelten:

```text
WORLD_WIDTH_BLOCKS % CHUNK_SIZE == 0
```

Andernfalls würde die Weltnaht mitten durch einen Chunk verlaufen.

Auch die halbe Weltbreite sollte durch die Chunkgröße teilbar sein, wenn ein symmetrischer lokaler Bereich verwendet wird.

## 2.6 Globaler Referenzpunkt ist nach Materialisierung unveränderlich

Solange eine Earth-World noch keine Snapshots, Events oder Objekte besitzt, darf ihr globaler Referenzpunkt korrigiert werden.

Sobald persistente Inhalte existieren:

```text
normales PATCH des Referenzpunkts
→ HTTP 409
```

Eine spätere Änderung muss als explizite Migration beziehungsweise `ReanchorWorld` behandelt werden.

## 2.7 Spawn und Weltreferenz sind getrennt

```text
globaler Referenzpunkt
→ definiert die globale Bedeutung des lokalen Weltursprungs

lokaler Spawnpunkt
→ definiert nur, wo ein Nutzer die Welt betritt
```

Eine Spawnänderung darf niemals den globalen Referenzpunkt verändern.

---

# 3. Neue Dateien

## 3.1 Architekturentscheidung

### `services/vectoplan-chunk/docs/adr/ADR-earth-world-v1.md`

**Zweck**

Verbindlicher Architekturvertrag für:

* eine globale Referenz pro Earth-WorldInstance;
* lokale Persistenz;
* periodische X-Achse;
* kanonische lokale Koordinaten;
* unveränderliches Raster;
* Spawn-/Referenz-Trennung;
* Verbot regionaler Runtime-CRS;
* Regeln für Reanchoring.

Diese Datei muss zuerst entstehen. Spätere Implementierung und Tests beziehen sich auf sie.

---

## 3.2 Gemeinsamer Koordinatenkern

### `services/vectoplan-chunk/src/coordinates/__init__.py`

Öffentliche Fassade des neuen Koordinatenpakets.

### `services/vectoplan-chunk/src/coordinates/models.py`

Kleine, frameworkunabhängige Wertobjekte:

```text
LocalBlockPosition
LocalChunkPosition
LocalCellPosition
NormalizedPosition
ChunkAddress
```

Diese Objekte enthalten keine Datenbank- oder Flask-Abhängigkeit.

### `services/vectoplan-chunk/src/coordinates/chunk_math.py`

Zentrale Logik für:

* Weltposition zu Chunkkoordinate;
* Chunkkoordinate zu lokaler Zellkoordinate;
* Floor-Division bei negativen Koordinaten;
* Chunk-Key-Erzeugung;
* Rückrechnung;
* Prüfung gültiger Zellbereiche.

Diese Logik wird später von `flat` und `earth` gemeinsam verwendet.

### `services/vectoplan-chunk/src/coordinates/topology.py`

Topologiestrategien:

```text
UnboundedFlatTopology
→ keine Normalisierung

PeriodicXTopology
→ X-Wrap
→ kanonischer lokaler Bereich
→ Nachbarn an der Weltnaht
```

Hierhin gehören auch:

* Chunk-Nachbarberechnung;
* Dirty-Chunk-Normalisierung;
* Abstand über die Weltnaht;
* kanonische Darstellung des gegenüberliegenden Punkts.

### `services/vectoplan-chunk/src/coordinates/errors.py`

Eindeutige Domänenfehler, beispielsweise:

```text
invalid_world_width
world_width_not_chunk_aligned
coordinate_out_of_bounds
ambiguous_antipodal_coordinate
unsupported_wrap_axis
```

---

## 3.3 Georeferenzierung

### `services/vectoplan-chunk/src/georeferencing/__init__.py`

Öffentliche Fassade der Georeferenzierung.

### `services/vectoplan-chunk/src/georeferencing/contracts.py`

Frameworkunabhängige Verträge für:

```text
GlobalReference
CrsDefinition
EarthGridDefinition
ResolvedEarthAnchor
CoordinateTransformResult
```

### `services/vectoplan-chunk/src/georeferencing/crs.py`

Verantwortlich für:

* Lesen und Validieren einer CRS-Angabe;
* EPSG-, WKT- oder PROJ-Definitionen;
* einheitliche Achsenreihenfolge;
* Validierung von Einheit und Dimension;
* Ermittlung, ob eine Koordinate 2D oder 3D ist;
* Ablehnung fehlender oder mehrdeutiger CRS-Angaben.

Wichtig:

```text
CRS aus Metadaten übernehmen
→ erlaubt

CRS aus Zahlenwerten erraten
→ nicht erlaubt
```

### `services/vectoplan-chunk/src/georeferencing/transformer.py`

Transformation des einmaligen globalen Referenzpunkts:

```text
Quell-CRS des Referenzpunkts
→ kanonische Earth-Rasterposition
```

Dieser Baustein wird später auch für Big-Data-Importe wiederverwendet.

### `services/vectoplan-chunk/src/georeferencing/earth_grid.py`

Abbildung zwischen:

```text
globaler Referenzkoordinate
↔ globalem Earth-Raster

globalem Earth-Raster
↔ lokaler Projektposition
```

Diese Datei darf keine Chunks laden oder Datenbankzugriffe durchführen.

---

## 3.4 Neuer World-Provider

### `services/vectoplan-chunk/src/world/earth/__init__.py`

Öffentliche Provider-Fassade.

### `services/vectoplan-chunk/src/world/earth/world.json`

Versionierte Definition des globalen Earth-Rasters:

```text
providerId = earth
templateId = earth
generatorType = earth-flat-periodic
generatorVersion
coordinateSystemId
gridId
gridVersion
axisConvention
worldWidthBlocks
chunkSize
metersPerCell
wrapX
northSouthPolicy
projectionDefinition
```

Diese Datei ist die kanonische Rasterdefinition. Werte wie Weltbreite dürfen nicht pro Projekt oder Deployment unterschiedlich gesetzt werden.

### `services/vectoplan-chunk/src/world/earth/validator.py`

Validiert:

* Weltbreite;
* Chunk-Ausrichtung;
* Wrap-Regeln;
* Achsenkonvention;
* Grid-Version;
* Projektionsparameter;
* zulässige Referenz-CRS;
* notwendige Earth-Metadaten.

### `services/vectoplan-chunk/src/world/earth/generator.py`

Erste Earth-Version:

```text
flacher Generator
→ zunächst gleiche oder ähnliche Terrainregel wie flat
→ aber mit PeriodicXTopology
```

Der Generator selbst muss keine globale Koordinate pro Block berechnen. Er erhält bereits normalisierte lokale Chunkadressen.

### `services/vectoplan-chunk/src/world/earth/provider.py`

Implementiert die vorhandene Provider-Schnittstelle und liefert:

* Earth-World-Metadaten;
* Topologie;
* Generator;
* Rasterdefinition;
* Georeferenzierungsfähigkeiten;
* Provider-Readiness.

---

## 3.5 Neue Persistenzmodelle

### `services/vectoplan-chunk/models/world_georeference.py`

Neue 1:1-Tabelle zur `WorldInstance`.

Empfohlene Felder:

```text
id
world_instance_db_id             unique FK

reference_crs_id
reference_crs_wkt
reference_x
reference_y
reference_z                      optional

grid_id
grid_version
axis_convention

status
revision
created_at
updated_at
```

Wichtig:

* `reference_x/y/z` sind genau der eine globale Referenzpunkt;
* keine berechnete globale Blockposition wird gespeichert;
* keine zusätzlichen regionalen Anker;
* die transformierte Rasterposition wird bei Bedarf berechnet und höchstens im Runtime-Cache gehalten.

### `services/vectoplan-chunk/models/world_spawn_position.py`

Separate 1:1-Tabelle für den beweglichen Spawnpunkt:

```text
id
world_instance_db_id             unique FK
local_x
local_y
local_z
heading
revision
created_at
updated_at
```

Die Werte bleiben lokal relativ zur Weltreferenz.

Diese Trennung verhindert, dass eine Spawnänderung versehentlich die globale Verankerung der Welt verändert.

---

## 3.6 World-State-Anwendungslogik

### `services/vectoplan-chunk/src/world_state/coordinate_service.py`

Zentrale Anwendungsfassade für:

```text
WorldInstance laden
→ Provider bestimmen
→ Georeferenz laden
→ Topologie bestimmen
→ lokal normalisieren
→ lokal/global umrechnen
```

Funktionen beziehungsweise Verantwortlichkeiten:

* lokale Position kanonisieren;
* globale Position aus Referenz plus lokalem Offset berechnen;
* globale Position in lokalen Offset umrechnen;
* globale Spawnkoordinate in lokalen Spawn übersetzen;
* Chunkkoordinate normalisieren;
* Nahtnachbarn bestimmen;
* Georeferenz-Cache verwalten und invalidieren.

Diese Datei verhindert, dass dieselbe Koordinatenlogik in `chunks.py`, `commands.py` und `worlds.py` mehrfach implementiert wird.

---

## 3.7 Neue API-Route

### `services/vectoplan-chunk/routes/world_coordinates.py`

Eigener HTTP-Adapter für Earth-Koordinaten.

Vorgesehene Routen:

```text
GET /projects/<project_id>/worlds/<world_id>/coordinate-frame

PUT /projects/<project_id>/worlds/<world_id>/coordinate-frame

POST /projects/<project_id>/worlds/<world_id>/coordinates/local-to-global

POST /projects/<project_id>/worlds/<world_id>/coordinates/global-to-local

PATCH /projects/<project_id>/worlds/<world_id>/spawn
```

Regeln:

* `PUT coordinate-frame` nur einmal beziehungsweise vor Materialisierung;
* `PATCH spawn` akzeptiert entweder lokale oder globale Zielkoordinate;
* globale Spawnkoordinate wird berechnet und anschließend als lokaler Spawn gespeichert;
* Diagnoseumrechnungen schreiben keine Daten;
* Flat-Welten beantworten Earth-spezifische Operationen mit einem eindeutigen Fehler.

---

## 3.8 Migrationen

Da aktuell noch kein finales Alembic-Konzept existiert, sollte diese Erweiterung nicht über weitere `repair_missing_columns`-Sonderlogik eingeführt werden. Das Fehlen versionierter Migrationen ist bereits als technischer Restpunkt dokumentiert. 

Neu:

```text
services/vectoplan-chunk/alembic.ini
services/vectoplan-chunk/migrations/env.py
services/vectoplan-chunk/migrations/script.py.mako
services/vectoplan-chunk/migrations/versions/<version>_earth_world_reference.py
```

Die Earth-Migration erzeugt:

```text
world_georeferences
world_spawn_positions
Foreign Keys
Unique Constraints
Status-/Lookup-Indizes
```

Für vorhandene Installationen:

```text
bestehendes Schema prüfen
→ als Baseline markieren
→ Earth-Migration anwenden
```

`flat`-Welten erhalten keine künstliche Referenzzeile.

---

## 3.9 Tests

### Unit-Tests

```text
tests/unit/coordinates/test_chunk_math.py
tests/unit/coordinates/test_periodic_x_topology.py
tests/unit/coordinates/test_negative_coordinates.py
tests/unit/coordinates/test_world_seam.py

tests/unit/georeferencing/test_crs_validation.py
tests/unit/georeferencing/test_earth_grid.py
tests/unit/georeferencing/test_reference_roundtrip.py

tests/unit/world/test_earth_validator.py
tests/unit/world/test_earth_generator.py
```

### Integrationstests

```text
tests/integration/test_earth_world_creation.py
tests/integration/test_earth_world_reference.py
tests/integration/test_earth_chunk_loading.py
tests/integration/test_earth_command_seam.py
tests/integration/test_earth_spawn.py
tests/integration/test_earth_reanchor_guard.py
tests/integration/test_flat_world_regression.py
```

### End-to-End-Tests

```text
tests/e2e/test_earth_project_provisioning.py
tests/e2e/test_earth_set_remove_reload.py
tests/e2e/test_earth_world_circumnavigation.py
```

---

# 4. Bestehende Dateien, die bearbeitet werden müssen

## 4.1 `models/world.py`

Aktuell enthält dieses Modell die konkrete `WorldInstance` und schützt unter anderem gegen `flat` als konkrete World-ID. Es besitzt außerdem spezielle Flat-Erzeugungslogik. 

Anpassungen:

* `world_spawn` darf nicht mehr implizit Flat bedeuten;
* generische Factory für providerbasierte Spawn-Welten;
* `create_flat_spawn(...)` bleibt erhalten;
* neue `create_earth_spawn(...)`;
* providerabhängige Validierung;
* Earth-World benötigt eine Georeferenz;
* Flat-World darf keine Earth-Georeferenz benötigen;
* Beziehung zu `WorldGeoreference`;
* Beziehung zu `WorldSpawnPosition`;
* `to_dict()` um `worldType`, `gridId`, `coordinateFrameAvailable` erweitern;
* keine tiefe automatische Relationship-Serialisierung.

## 4.2 `models/__init__.py`

Neue Modelle importieren und registrieren:

```text
WorldGeoreference
WorldSpawnPosition
```

Model-Diagnostik und Schema-Summaries aktualisieren.

## 4.3 `models/chunk.py`

Keine globalen Koordinatenfelder hinzufügen.

Nur prüfen beziehungsweise anpassen:

* Snapshot-Identität verwendet ausschließlich kanonische lokale Chunkkoordinaten;
* ein äquivalenter Earth-Ort darf nicht mehrere Snapshot-Schlüssel erzeugen;
* Unique Constraint bleibt auf kanonischer Chunkadresse wirksam.

## 4.4 `models/event.py`

Keine globale Position pro Event speichern.

Optional ergänzen:

```text
coordinateSpace = local-world
worldProvider = earth/flat
gridVersion
```

Nur wenn diese Angaben nicht bereits eindeutig über die WorldInstance rekonstruierbar sind.

Die lokale Commandposition bleibt die operative Eventposition.

---

## 4.5 `src/world/registry.py`

Earth-Provider registrieren.

Zusätzlich Provider-Fähigkeiten abbilden:

```text
supportsGeoreference
supportsPeriodicX
requiresCoordinateFrame
topologyType
gridVersion
```

## 4.6 `src/world/discovery.py`

Earth-Paket und `world.json` entdecken.

Fehlerbehandlung ergänzen, falls:

* Earth-Provider fehlt;
* Griddefinition ungültig ist;
* Weltbreite nicht chunkkompatibel ist.

## 4.7 `src/world/loader.py`

Earth-Definition laden und validieren.

Ladeergebnis muss Provider und Topologie gemeinsam bereitstellen.

## 4.8 `src/world/service.py`

Providerunabhängige Fassade erweitern:

```text
resolve_provider(world_instance)
resolve_topology(world_instance)
resolve_grid_definition(world_instance)
generate_chunk(...)
```

Die Route darf später nicht selbst entscheiden, ob Flat- oder Earth-Logik verwendet wird.

---

## 4.9 `src/world_state/defaults.py`

Flat bleibt Default.

Ergänzen:

* Earth-Provider-ID;
* Earth-Template-ID;
* keine automatische Earth-Referenz;
* keine Änderung am bestehenden Default-Seed.

## 4.10 `src/world_state/resolver.py`

World-Auflösung erweitern:

```text
WorldInstance
→ Provider
→ optionale Georeferenz
→ Topologie
→ Generator
```

Fehlerfälle:

```text
earth world without reference
flat world with invalid earth-only metadata
unsupported grid version
inactive coordinate frame
```

## 4.11 `src/world_state/service.py`

Der aktuelle Service lädt Projekt, Universe, WorldInstance, Blocks und Snapshot beziehungsweise Generator. 

Anpassungen:

* `CoordinateService` verwenden;
* Chunkkoordinaten vor DB-Abfrage kanonisieren;
* Earth-Topologie beim Generatorpfad berücksichtigen;
* kanonischen Chunk-Key zurückgeben;
* auf Wunsch ursprüngliche angefragte und kanonische Adresse getrennt ausgeben;
* Batch-Deduplizierung äquivalenter Earth-Chunkadressen.

Beispiel:

```text
angefragt: chunkX = 1.250.000
kanonisch: chunkX = -1.250.000
→ nur ein Chunk laden
```

## 4.12 `src/world_state/provisioning.py`

Aktuell erzeugt das App-Provisioning Project, Universe und `world_spawn` mit Flat-Provider. 

Anpassungen:

* Payloadfeld `worldType`, Default weiterhin `flat`;
* bei `worldType=earth` globale Referenz verlangen;
* Earth-WorldInstance mit `provider_id=earth` erzeugen;
* eine `WorldGeoreference` erzeugen;
* einen lokalen Spawn erzeugen;
* idempotenten Vergleich der Referenz;
* Konflikt melden, wenn dasselbe App-Projekt mit abweichender Earth-Referenz erneut provisioniert wird;
* Route-Hints um Coordinate-Frame und Spawn erweitern.

## 4.13 `src/world_state/serializer.py`

Erweitern um:

```text
worldType
providerId
topologyType
gridId
gridVersion
coordinateFrame
spawn
```

Für `flat`:

```text
coordinateFrame = null
supportsGeoreference = false
```

Für `earth`:

```text
coordinateFrame vorhanden
supportsGeoreference = true
```

Keine rekursive Model-Serialisierung.

---

## 4.14 `routes/__init__.py`

Neue `world_coordinates`-Blueprint registrieren.

## 4.15 `routes/projects.py`

Anpassungen:

* Provisioning-Payload für `worldType`;
* Earth-Referenz validieren;
* Projektstatus um Earth-Provider-Verfügbarkeit erweitern;
* Default-Readiness von Earth entkoppeln.

Wichtig:

```text
Earth-Provider nicht bereit
→ Earth-Provisioning nicht verfügbar

bestehende Flat-Defaultwelt bereit
→ bestehender Service bleibt trotzdem funktionsfähig
```

## 4.16 `routes/worlds.py`

Anpassungen:

* Earth-Worlds erzeugen und lesen;
* `worldType` aus Provider ableiten;
* Earth-World ohne Referenz nicht aktivieren;
* Referenzänderung nach Materialisierung blockieren;
* Spawninformationen ausgeben;
* keine Koordinatenmathematik direkt in der Route.

## 4.17 `routes/chunks.py`

Die bestehenden projektgescopten Einzel- und Batch-Routen bleiben erhalten. 

Anpassungen:

1. World auflösen.
2. Topologie bestimmen.
3. Chunkadresse kanonisieren.
4. Erst danach Snapshot suchen.
5. Falls kein Snapshot existiert, Provider generieren lassen.
6. Antwort mit kanonischem Chunk-Key liefern.

Besonders prüfen:

* letzter X-Chunk ↔ erster X-Chunk;
* Batch mit derselben physischen Position über unterschiedliche X-Darstellungen;
* negative Koordinaten;
* `allowGenerated=false`;
* Snapshot-/Generator-Mix an der Naht.

## 4.18 `routes/commands.py`

Die bestehenden Commandtypen bleiben bestehen. 

Anpassungen für jeden positionsbezogenen Command:

```text
lokale Eingabeposition
→ Earth-Topologie anwenden
→ kanonische lokale Position
→ Chunk und Zelle bestimmen
→ erst danach DB schreiben
```

Dies betrifft mindestens:

* `SetBlock`;
* `RemoveBlock`;
* `ReplaceBlock`;
* später `PlaceObject`;
* später `RemoveObject`.

Dirty-Chunk-Berechnung muss die Naht berücksichtigen:

```text
Zelle am rechten Rand des letzten X-Chunks
→ eigener Chunk dirty
→ erster X-Chunk dirty
```

Event und CommandLog speichern die kanonische lokale Position.

## 4.19 `routes/editor.py`

Falls diese Route Bootstrap- oder Worldinformationen an den Editor liefert:

* `worldType`;
* `topologyType`;
* `gridVersion`;
* `wrapAxes`;
* lokale Spawnposition;
* Coordinate-Frame-Route-Hints.

Der Editor darf nicht selbst eine abweichende Wrap-Formel implementieren. Der Vertrag muss versioniert sein.

---

## 4.20 `config.py`

Nur Betriebsflags ergänzen:

```text
EARTH_PROVIDER_ENABLED
EARTH_REFERENCE_CRS_ALLOWLIST
EARTH_TRANSFORM_STRICT
```

Nicht per Environment konfigurierbar machen:

```text
Weltbreite
Grid-Version
metersPerCell
Wrap-Regeln
```

Diese Werte müssen aus der versionierten `earth/world.json` kommen. Sonst könnten zwei Instanzen desselben Services verschiedene Earth-Raster verwenden.

## 4.21 `src/bootstrap/settings.py`

Neue Betriebsflags lesen und normalisieren.

Keine globale Referenz oder Griddefinition aus ENV erzeugen.

## 4.22 `src/bootstrap/default_seed.py`

Flat bleibt der Default-Seed.

Anpassungen:

* Earth-Provider nur diagnostizieren;
* keine Earth-World automatisch erzeugen;
* keine künstliche Earth-Referenz seeden;
* optional Earth-Testfixture ausschließlich in explizitem Dev-Modus.

## 4.23 `src/bootstrap/db_bootstrap.py`

Erweitern um:

```text
earthSchemaReady
earthProviderReady
earthGridReady
crsTransformerReady
```

Earth-Readiness darf die Flat-Defaultwelt nicht unnötig blockieren.

## 4.24 `scripts/bootstrap_db.py`

Ausgabe um Earth- und Migrationsstatus erweitern.

Langfristig:

```text
create_all
→ nur Entwicklung

Migrationen
→ normaler Schema-Upgrade-Pfad
```

## 4.25 `entrypoint.sh`

Nur falls Migration-Readiness ergänzt wird:

* Schema-Version prüfen;
* keine Migration im normalen Gunicorn-Worker ausführen;
* Runtime bleibt read-only.

Die vorhandene Trennung zwischen Runtime und Bootstrap muss erhalten bleiben. 

## 4.26 `requirements.txt`

Ergänzen und fest versionieren:

```text
pyproj
```

Für spätere Datensätze eventuell:

```text
rasterio
```

`rasterio` ist für den ersten Earth-World-Slice noch nicht erforderlich.

## 4.27 `Dockerfile`

Sicherstellen:

* PROJ-Datenbank verfügbar;
* Versionen reproduzierbar;
* CRS-Transformation startet ohne Netzwerkzugriff;
* keine Transformationsgitter während der Runtime automatisch herunterladen.

## 4.28 `docker-compose.all.yml`

Ergänzen:

* Earth-Provider-Featureflag;
* PROJ-Datenpfad, falls erforderlich;
* keine Griddefinition als frei änderbare ENV-Werte;
* Init-Service führt Migrationen aus;
* Runtime prüft nur Readiness.

---

# 5. Empfohlene Implementierungsreihenfolge

## Phase 0 – Architekturvertrag festschreiben

**Datei**

```text
docs/adr/ADR-earth-world-v1.md
```

**Festlegen**

* X ist periodisch;
* Z-Verhalten;
* Weltbreite;
* Chunkgröße;
* Zellmaßstab;
* kanonischer lokaler Bereich;
* CRS-Vertrag;
* Referenzimmutabilität;
* Spawnsemantik;
* Reanchor-Regel;
* Grid-Versionierung.

**Abschlusskriterium**

Alle späteren Tests können aus dem ADR abgeleitet werden.

---

## Phase 1 – Reinen Koordinatenkern bauen

**Dateien**

```text
src/coordinates/*
tests/unit/coordinates/*
```

**Noch keine DB und keine Routen ändern.**

Zuerst beweisen:

```text
normalize(x) == normalize(x + n × WORLD_WIDTH)
```

und:

```text
derselbe physische Ort
→ derselbe Chunk-Key
→ dieselbe lokale Zelle
```

Außerdem:

* negative Koordinaten;
* Weltnaht;
* Chunknachbarn;
* Dirty-Nachbarn;
* gegenüberliegender Punkt;
* Weltbreite/Chunkgröße.

**Abschlusskriterium**

Der mathematische Kern ist vollständig deterministisch und unit-getestet.

---

## Phase 2 – Georeferenzierung isoliert implementieren

**Dateien**

```text
src/georeferencing/*
tests/unit/georeferencing/*
requirements.txt
Dockerfile
```

Umsetzen:

```text
ein globaler Punkt + CRS
→ Earth-Grid-Anker

Earth-Grid-Position
→ globale Koordinate

globale Zielkoordinate
→ lokaler Offset zum Anker
```

Kein Persistieren berechneter globaler Positionen.

**Abschlusskriterium**

Referenz und lokale Position lassen sich innerhalb definierter Toleranz vorwärts und rückwärts umrechnen.

---

## Phase 3 – Earth-Provider einführen

**Dateien**

```text
src/world/earth/*
src/world/registry.py
src/world/discovery.py
src/world/loader.py
src/world/service.py
```

Der Provider muss laden und seinen Status melden können, ohne dass bereits eine Earth-World in der DB existiert.

**Abschlusskriterium**

```text
earthProviderReady = true
earthGridReady = true
```

Flat-Verhalten bleibt unverändert.

---

## Phase 4 – Migration und Modelle

**Dateien**

```text
alembic.ini
migrations/*
models/world_georeference.py
models/world_spawn_position.py
models/world.py
models/__init__.py
```

Zuerst Migration in leerer DB testen, dann gegen eine vorhandene Flat-DB.

**Abschlusskriterium**

* bestehende Flat-Daten bleiben unverändert;
* neue Tabellen existieren;
* pro Earth-World maximal eine Referenz und ein Spawn;
* Flat-World benötigt keine Referenzzeile.

---

## Phase 5 – World-State-Koordinatenservice

**Dateien**

```text
src/world_state/coordinate_service.py
src/world_state/resolver.py
src/world_state/service.py
src/world_state/serializer.py
```

Hier wird die zentrale providerabhängige Auflösung hergestellt.

**Abschlusskriterium**

Routen müssen keine eigene Wrap- oder CRS-Logik mehr enthalten.

---

## Phase 6 – Bootstrap und Readiness

**Dateien**

```text
config.py
src/bootstrap/settings.py
src/bootstrap/default_seed.py
src/bootstrap/db_bootstrap.py
scripts/bootstrap_db.py
entrypoint.sh
```

Earth wird als verfügbare Fähigkeit geprüft, aber nicht als Defaultwelt geseedet.

**Abschlusskriterium**

Eine Installation ohne Earth-Projekte startet weiter mit der bestehenden Flat-Defaultwelt.

---

## Phase 7 – Earth-World erstellen und provisionieren

**Dateien**

```text
src/world_state/provisioning.py
routes/projects.py
routes/worlds.py
routes/world_coordinates.py
routes/__init__.py
```

Implementieren:

```text
Earth-Projekt erstellen
→ world_spawn anlegen
→ provider = earth
→ genau eine globale Referenz speichern
→ lokalen Spawn anlegen
```

**Abschlusskriterium**

Wiederholtes Provisioning mit identischen Daten ist idempotent. Abweichende Referenzdaten erzeugen einen Konflikt und überschreiben nicht still.

---

## Phase 8 – Chunk-Lesepfad

**Dateien**

```text
routes/chunks.py
src/world_state/service.py
models/chunk.py
```

Zuerst Einzelchunk, dann Batch.

Besonders testen:

```text
chunkX
chunkX + earthWidthChunks
chunkX - earthWidthChunks
```

Alle müssen denselben physischen Chunk ergeben.

**Abschlusskriterium**

Es kann keine doppelten Snapshots für äquivalente Earth-Chunkadressen geben.

---

## Phase 9 – Command-Schreibpfad

**Dateien**

```text
routes/commands.py
models/event.py
```

Reihenfolge im Command:

```text
Input validieren
→ World und Provider auflösen
→ Position kanonisieren
→ Chunk bestimmen
→ Snapshot laden/generieren
→ Änderung ausführen
→ Event und CommandLog schreiben
```

**Abschlusskriterium**

Ein Block an der Weltnaht kann gesetzt, von der äquivalenten Gegenseite geladen und wieder entfernt werden.

---

## Phase 10 – Spawn über globale Koordinaten

**Dateien**

```text
routes/world_coordinates.py
src/world_state/coordinate_service.py
models/world_spawn_position.py
```

Ablauf:

```text
globale Zielkoordinate + CRS
→ Earth-Rasterposition
→ lokaler kanonischer Offset zur Weltreferenz
→ lokalen Spawn speichern
```

**Abschlusskriterium**

Der erneut global berechnete Spawn entspricht dem angefragten Ziel innerhalb der festgelegten Toleranz.

---

## Phase 11 – Reanchor-Schutz

**Dateien**

```text
routes/world_coordinates.py
src/world_state/coordinate_service.py
models/world_georeference.py
```

Prüfen:

```text
ChunkSnapshots vorhanden?
ChunkEvents vorhanden?
Objekte vorhanden?
```

Dann:

```text
Referenzänderung
→ HTTP 409 world_reference_locked
```

**Abschlusskriterium**

Keine bestehende Earth-Welt kann durch ein gewöhnliches Update unbemerkt global verschoben werden.

---

## Phase 12 – Editor- und App-Vertrag

**Dateien**

```text
routes/editor.py
src/world_state/serializer.py
README.md
```

Editor erhält:

```text
worldType
topologyType
wrapAxes
gridId
gridVersion
worldWidthBlocks
localSpawn
```

Er muss nicht den globalen Referenzpunkt für jeden Block verwenden. Dieser wird nur für globale Navigation, Anzeige und Spawnverschiebung benötigt.

**Abschlusskriterium**

Flat-Editorbetrieb unverändert; Earth-Editorbetrieb kennt die X-Naht.

---

## Phase 13 – End-to-End- und Belastungstests

Testfälle:

1. Flat-Regression.
2. Earth-Provisioning.
3. Ein globaler Referenzpunkt vorhanden.
4. Keine globalen Blockkoordinaten persistiert.
5. SetBlock normal.
6. RemoveBlock normal.
7. SetBlock direkt an der Weltnaht.
8. Reload von der Gegenseite.
9. Dirty-Chunk über die Naht.
10. Negative lokale Koordinaten.
11. Mehrere Weltumrundungen ergeben denselben Chunk.
12. Batch-Deduplizierung.
13. Globaler Spawn nach Osten.
14. Globaler Spawn über der Weltnaht.
15. Referenzänderung vor Materialisierung.
16. Referenzänderung nach Materialisierung wird blockiert.
17. Neustart liefert identische Umrechnung.
18. Mehrere Earth-Projekte mit unterschiedlichen Referenzpunkten.
19. Gleiches globales Ziel aus zwei Projekten ergibt unterschiedliche lokale Offsets, aber dieselbe globale Position.
20. Gleichzeitige Commands an derselben Nahtzelle.

---

# 6. Dateien, die bewusst nicht grundlegend umgebaut werden

Der erste Earth-Slice soll nicht gleichzeitig den gesamten Service neu strukturieren.

Unverändert beziehungsweise nur minimal angepasst bleiben:

```text
BlockRegistry
BlockType
Systemblock-Katalog
Air-Invariante
Railing-Mirror
ChunkSnapshot-Inhalt
Zellwertkodierung
SetBlock-/RemoveBlock-Grundmechanik
WorldCommandLog-Grundstruktur
ChunkEvent-Grundstruktur
```

Weiterhin gilt:

```text
cellValue = 0
→ Air

cellValue = paletteIndex + 1
→ Block
```

Der Earth-Typ verändert die Adressierung und Georeferenzierung, nicht das Block- oder Palettenmodell.

---

# 7. Wichtigste Fehler, die der Fahrplan verhindern muss

## Doppelte Snapshots

Ursache:

```text
x
x + WORLD_WIDTH
```

werden vor dem DB-Zugriff nicht normalisiert.

Vermeidung:

```text
immer zuerst kanonisieren
→ danach Chunk-Key
→ danach DB
```

## Unterschiedliche Raster zwischen Deployments

Ursache:

```text
Weltbreite aus frei änderbarer ENV
```

Vermeidung:

```text
versionierte earth/world.json
```

## Unbemerkte globale Verschiebung

Ursache:

```text
Referenzpunkt nachträglich ändern
```

Vermeidung:

```text
Referenz nach Materialisierung sperren
```

## Spawn verschiebt Welt

Ursache:

```text
Spawn und Referenz in demselben Updatepfad
```

Vermeidung:

```text
separate Modelle
separate Routen
separate Berechtigungen
```

## CRS wird geraten

Ursache:

```text
Koordinaten ohne CRS akzeptieren
```

Vermeidung:

```text
CRS zwingend angeben oder aus verlässlichen Metadaten übernehmen
```

## Flat wird unbeabsichtigt verändert

Ursache:

```text
Wrap-Logik direkt in gemeinsame Routen schreiben
```

Vermeidung:

```text
providerabhängige Topologiestrategie
Flat = unbounded
Earth = periodic-x
```

---

# 8. Definition of Done

Der erste Earth-Slice ist fertig, wenn:

```text
1. flat unverändert funktioniert.

2. earth als zweiter Provider verfügbar ist.

3. eine Earth-World genau einen globalen Referenzpunkt besitzt.

4. CRS und Earth-Grid-Version gespeichert sind.

5. Blöcke, Chunks, Events und Snapshots nur lokal gespeichert werden.

6. globale Positionen jederzeit deterministisch berechnet werden können.

7. die X-Achse exakt umläuft.

8. äquivalente Positionen immer denselben Chunk-Key erzeugen.

9. an der Weltnaht keine doppelten Snapshots entstehen.

10. Dirty-Chunks über die Naht korrekt berechnet werden.

11. ein globales Spawnziel in einen lokalen Spawn umgerechnet wird.

12. eine Spawnänderung keine Weltinhalte verschiebt.

13. der globale Referenzpunkt nach Materialisierung gesperrt ist.

14. mehrere Earth-Projekte unterschiedliche Referenzpunkte besitzen können.

15. alle Transformations- und Gridverträge versioniert sind.

16. Runtime weiterhin read-only startet.

17. Schemaänderungen über Migrationen erfolgen.

18. alle Flat-Regressionstests erfolgreich sind.
```

# 9. Kompakte Reihenfolge

```text
ADR festlegen
→ Koordinatenkern
→ Georeferenzierung
→ Earth-Provider
→ Migrationen und Modelle
→ CoordinateService
→ Bootstrap/Readiness
→ World-Erstellung/Provisioning
→ Chunk-Lesepfad
→ Command-Schreibpfad
→ global steuerbarer Spawn
→ Reanchor-Schutz
→ Editor-/App-Vertrag
→ End-to-End-Tests
→ Dokumentation aktualisieren
```

Als nächster Arbeitsschritt sollte Phase 0 in konkrete, unveränderliche Earth-v1-Invarianten mit festen Feldnamen und API-Payloads überführt werden.
