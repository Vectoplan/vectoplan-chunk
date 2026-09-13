# Baumkataster: Upload, Gelände und Abbau

`baumkataster` ist ein installationsfestes Dashboard-Projekt. Der Katalogeintrag
aktiviert den bestehenden manuellen Upload. Es werden keine Beispielpunkte in
Nutzerprojekte geschrieben. Ein später hochgeladenes GeoPackage durchläuft die
bestehende Veröffentlichung und Freigabe; erst der freigegebene Release wird
vom Chunk-Dienst verwendet.

## Datenaufbereitung

Der GeoServer-Orchestrator liest deklarierte Point-/MultiPoint-Layer und lässt
GDAL deren Quell-CRS nach EPSG:4326 umrechnen. Eine unbekannte CRS wird nicht als
WGS84 geraten. Die Originaldateien bleiben unverändert. GeoJSONSeq wird zeilenweise
in einen dauerhaften SQLite/RTree-Index pro Release überführt. Erst ein vollständig
erstellter Index wird atomar veröffentlicht. Nach Freigabe startet die Vorbereitung
im Hintergrund; nach einem Prozessneustart kann eine Abfrage sie erneut starten.
Direkte Dateien und die vom Dashboard verwendeten Object-Store-Referenzen werden
unterstützt. Referenzen werden mit dem bestehenden internen Service-Token und
ohne Redirects in temporären Speicher gestreamt (maximal 2 GiB pro Quelldatei).
Veröffentlichte Dateigröße und SHA-256 werden vor der Normalisierung geprüft;
die Originaldatei bleibt im Scraper-Objektspeicher.

`GET /admin/api/production-publications/baumkataster/tree-points` akzeptiert `bbox`
als `minLon,minLat,maxLon,maxLat`, `limit` (1–1000), `offset` und optional
`release_key`. Jede Abfrage prüft die aktuelle Freigabe. Das lokale Fenster ist auf
je 0,05 Grad begrenzt. Der vorhandene interne Service-Token gilt ausschließlich
für den lesenden Endpunkt. Antwortschema: `vectoplan-tree-points.v1`, `release_key`,
`items`, `counts` und `nextOffset`.

Ein Punkt hat `id`, `longitude`, `latitude`, `heightM`, `crownDiameterM`,
`trunkDiameterM`, `species` und Herkunft (`file`, `layer`, `featureId`). Amtliche
Baum-IDs werden unabhängig von Release und Dateinamen verwendet; ersatzweise wird
die GeoPackage-FID durch Datei und Layer abgegrenzt. MultiPoints erhalten eine
zusätzliche Teilnummer. Geometrie-Z wird nicht als Baumhöhe interpretiert.
Berliner Attribute wie `baumhoehe`, `kronedurch`, `stammumfg` und `art_dtsch` werden
normalisiert; Stammumfang in Zentimetern wird in Durchmesser in Metern umgerechnet.
Fehlende Abmessungen eines vorhandenen Punkts verwenden 8 m Höhe, 5 m Krone und
0,3 m Stamm. Es werden dabei keine zusätzlichen Standorte erzeugt.

## Chunk-Vertrag

`metadata.geodataOverlays.items` enthält einen Eintrag mit `id`/`datasetId`
`baumkataster`, `renderMode: tree-instances` und `semanticRole: vegetation`.
`geometry` hat `type: TreeInstances`, `dimensions: world-xyz` und `features`.
Ein Feature enthält die Abmessungen, Art und folgende Felder:

```json
{
  "id": "stabile Quell-ID",
  "objectInstanceId": "tree_<40 Hex-Zeichen>",
  "position": [10.5, 1.27, 8.25],
  "yawRadians": 1.4,
  "source": {"treeId": "stabile Quell-ID", "longitude": 13.405, "latitude": 52.52}
}
```

X/Z benutzen den gemeinsamen Earth-Frame. Y wird auf genau denselben beiden
Dreiecken pro Gelände-Rasterzelle interpoliert wie die Terrainoberfläche.
Die Ausrichtung entsteht deterministisch aus der Quell-ID. Der unterstützende
Gelände-Chunk besitzt den Baum; halboffene X/Z-Grenzen vermeiden doppelte Instanzen.
Bei exakt ganzzahliger Schichtoberkante gehört der Baum zur darunterliegenden
Geländeschicht. Ohne verfügbare freigegebene Punkte bleibt der Layer leer.

## Dauerhaft als Ganzes entfernen

Der Editor sendet einen bestehenden `RemoveObject`-Befehl mit `objectInstanceId`
und `treeSource: {treeId, longitude, latitude}`. Der Server prüft die stabile ID
und den Punkt im freigegebenen Baumkataster. Erst danach erzeugt er innerhalb
derselben Transaktion eine logische `WorldObjectInstance` vom Typ `source_tree`
und einen Chunk-Verweis. Beide haben keine belegten Voxel.

Der normale RemoveObject-Pfad protokolliert das Ereignis, aktualisiert den Chunk
und löscht das Objekt logisch. Diese Zeile bleibt der dauerhafte Löschvermerk.
GET-Abfragen filtern ihn auch nach Chunk-Neuladen und Releasewechsel aus. Kein
Terrainblock oder benachbartes Nutzerobjekt wird dabei entfernt. Ein Rollback
verwirft sowohl den Löschvermerk als auch die begleitende Chunkänderung.

## Prüfung

`tests/test_tree_instances.py` prüft Terrain-Dreiecke, Grenzbesitz, stabile
Ausrichtung, Quellenprüfung und Paging. Der optionale DB-Test benutzt eine eigene
UUID-Testwelt und prüft den echten RemoveObject-Pfad samt unveränderten Voxeln,
Commit, erneuter Session und dauerhafter Unterdrückung. Der Orchestrator prüft mit
`tests/test_tree_points.py` zusätzlich ein echtes, von GDAL erzeugtes UTM33-
GeoPackage, exakte räumliche Abfragen, Freigaben und Service-Token-Abgrenzung.
