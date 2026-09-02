# LoD2 als editierbarer Bestand

## Zielvertrag

`vectoplan-chunk` konvertiert ein ausdrücklich materialisiertes LoD2-Gebäude
mit `lod2-editable-buildings.v4` in zwei gemeinsame WorldEdit-Primitiven:

- die klassifizierten `WallSurface`-Flächen werden ausschließlich als
  abbrechbare Ganzzellen vom Blocktyp `lod2_exterior_wall` geschrieben;
- die klassifizierten `RoofSurface`-Flächen bleiben als unabhängige,
  editierbare `building_roof`-Objekte der Familie `world-edit.roof` erhalten.

Es wird weder die Bounding Box gefüllt noch aus einem Dachüberstand eine Wand
oder ein Gebäudegrundriss erfunden. Nutzeränderungen und bereits entfernte
Systemblöcke werden beim erneuten Import nicht überschrieben.

## Bestandsgebäudeorientiertes Bauraster

Jedes Dachobjekt trägt unter
`metadata.roofParameters.importedSource.constructionGrid` den deterministischen
Vertrag `vectoplan-lod2-construction-grid.v1`:

1. Die längengewichtete dominante Familie echter äußerer Fassaden bestimmt
   `axisU`. Als Achse wird eine gemessene Außenkante gewählt, kein Mittelwinkel.
2. Eine zweite ausreichend getrennte Fassadenfamilie bestimmt `axisV`; fehlt
   sie, wird die Senkrechte zu `axisU` verwendet.
3. Alle zu diesen Achsen parallelen Fassaden werden als feste `uAnchors` bzw.
   `vAnchors` gespeichert. Damit liegen Außenwände immer auf Rasterlinien.
4. Jede Fassade wird auf `round(lengthM)` vollständige Blockspalten verteilt.
   Die Spaltenbreite wird gleichmäßig angepasst, sodass am Fassadenende keine
   Teilwand übrig bleibt.
5. Zwischen zwei Fassadenankern gilt `anchored-axis-lines.v1`: nahe 1 m große
   vollständige Zellen werden gleichmäßig verteilt. Erst an der
   Grundstücksgrenze dürfen zugeschnittene Übergangszellen entstehen; der
   Bestandsgrundriss wird aus der bebaubaren Restfläche ausgeschlossen.

Das globale Earth-/Chunk-Raster bleibt unverändert. Der Vertrag beschreibt ein
lokales Architektur- und Darstellungsraster. Bei einem leeren Grundstück oder
bei fehlender klassifizierter `GroundSurface` entsteht kein Bestandsvertrag;
der vorhandene, an der Grundstücksgrenze ausgerichtete Rasterfall bleibt aktiv.

## Provenienz und Migration

Der Vertrag enthält Quellkachel, SHA-256, Algorithmusversion sowie getrennte
Fingerprints für Grundriss, Fassaden und den vollständigen Vertrag. Ein Import-
Receipt speichert Version und Fingerprint. Vorhandene v3-Importe werden über die
bestehende Metadaten-Reparatur auf `lod2-facade-grid.v4` ergänzt, ohne Wände,
Dächer, Dachänderungen oder die Command-Historie neu zu materialisieren.

Die tatsächliche Parzellengrenze kommt weiterhin aus dem freigegebenen
Flurstück-Datensatz. Chunk liefert Achsen, Anker, Fassadenspalten und
Partitionierungsregeln; ein Consumer verbindet diese mit der ausgewählten
Parzellengrundfläche.
