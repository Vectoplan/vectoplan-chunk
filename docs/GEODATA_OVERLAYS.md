# Dynamische Geodaten-Overlays

## Architekturgrenze

Die DGM-Pipeline bleibt getrennt, weil sie Blockzellen erzeugt und damit die
Weltform beeinflusst. Geodaten-Overlays sind ausschliesslich visuelle,
versionierte Rendervertraege:

```text
vom Orchestrator verwalteter GeoServer-Layer
  -> GeoServer-WFS-Abfrage je horizontalem Chunk
  -> globale Koordinaten in lokale Earth-Weltkoordinaten
  -> geodata-overlays.v1 im Chunk-Response (nicht im Snapshot)
  -> Editor legt Linien auf die jeweils obersten festen Blockzellen
```

Wird ein Block entfernt oder gesetzt, baut der normale Chunk-Remesh auch das
Overlay neu auf. Die x/z-Geometrie bleibt gleich; nur ihre y-Hoehe wird aus den
aktuell sichtbaren Blockzellen abgeleitet. Overlays veraendern weder Zellen,
Kollisionen noch Platzierungs- oder Abbauregeln.

## Visuelle 3D-Prioritaet und Lizenz-Gate

Jeder serialisierte Earth-Chunk erhaelt zusaetzlich
`geodataOverlays.visualLayerResolution` mit dem Schema
`geodata-visual-layer-resolution.v1`. Die feste Reihenfolge lautet
`photorealistic -> lod3 -> lod2`; ausgewaehlt wird nur eine aktivierte Ebene
mit `status: ready`, die im selben Chunk-Vertrag durch `itemIds` belegt ist.
Status und Provenienz sind Diagnosemetadaten, keine Downloadanweisung.

Das Berliner fotorealistische Mesh ist standardmaessig doppelt gesperrt:
`enabled: false` und `status: license_required`. Der Vertrag enthaelt dafuer
keine Asset- oder Anbieter-URL. Erst eine separat dokumentierte Lizenzfreigabe,
eine explizite serverseitige Aktivierung und ein spaeterer, eigener
Asset-Adapter duerfen diese Ebene auf `ready` setzen. Bis dahin bleibt LoD2
die sichtbare, unter `dl-de-zero-2.0` nutzbare Rueckfallebene. LoD2-Geometrie
bleibt weiterhin response-only; eine explizite editierbare Materialisierung
nutzt unveraendert die normale Chunk-/WorldEdit-Persistenz.

Wichtig für Flurstücke: Dieses gelbe `surface-lines`-Overlay ist nur der
allgemeine Katasterkontext. Sobald Flurstücke ausgewählt sind, erzeugt der
Editor zusätzlich eine transparente Auswahlfläche, eine blaue verbindliche
Grenze und das rote Grundstücksraster auf einer festen horizontalen Ebene.
Terrain-Blöcke dürfen diese Rasterebene nicht anheben. Der vollständige Vertrag
steht in
[`../../vectoplan-editor/docs/PARCEL_GRID_AND_WORLDEDIT.md`](../../vectoplan-editor/docs/PARCEL_GRID_AND_WORLDEDIT.md).

## Standard

Ohne weitere Konfiguration sind zwei Layer aktiv:

- Overlay-ID: `parcel-boundaries`
- Orchestrator-Datensatz: `flurstuecke`
- WFS-Typ: `public:flurstuecke`
- Renderer: `surface-lines`
- semantische Rolle: `parcel-boundary`

- Overlay-ID: `street-network`
- Orchestrator-Datensatz: `strassendaten`
- WFS-Typ: `public:strassendaten`
- Renderer: `surface-ribbons`
- nominale MVP-Breite: 6 m
- semantische Rolle: `street-network`

Standardmaessig wird der bereits in GeoServer importierte WFS-Layer direkt
gelesen (`versionPolicy: "wfs-live"`). Dadurch blockiert ein langsamer
Publikations-Status-Refresh nicht das Chunk-Laden. Mit
`versionPolicy: "approved-release"` kann eine Ebene bei Bedarf strikt an den
vom Orchestrator freigegebenen Release gebunden werden. Ist die gewaehlte
Quelle nicht erreichbar, bleibt der Chunk benutzbar und der Overlay-Vertrag
meldet `degraded`.

## Konfiguration pro WorldInstance

`metadata.geodataOverlays.items` patcht Definitionen anhand ihrer `id`.
`inheritDefaults: false` ersetzt die globale Liste vollständig.

```json
{
  "metadataMerge": {
    "geodataOverlays": {
      "inheritDefaults": true,
      "items": [
        {
          "id": "parcel-boundaries",
          "datasetId": "flurstuecke",
          "enabled": true,
          "source": {
            "kind": "geoserver-wfs",
            "workspace": "public",
            "typeName": "public:flurstuecke",
            "srsName": "EPSG:4326",
            "geometryMode": "polygon-boundaries",
            "versionPolicy": "wfs-live"
          },
          "renderer": {
            "kind": "surface-lines",
            "style": {
              "color": "#ffd54f",
              "opacity": 0.96,
              "lineWidth": 1.5,
              "verticalOffset": 0.035,
              "sampleStep": 0.25
            }
          },
          "semantics": {
            "role": "parcel-boundary",
            "classificationSource": false
          }
        }
      ]
    }
  }
}
```

Der Straßen-Layer ist eine räumlich begrenzte, helle Planungsdarstellung. Jede
Mittellinie hat eine Nennbreite von 6,0 m. Der Editor begrenzt das symmetrische
Band auf die nächste sichtbare Flurstücksgrenze; ein Editor-Dataset kann die
bereits vorberechnete effektive Breite zusätzlich über
`geometry.surfaceWidths` liefern. Damit ragt das Band auch bei einer nicht
perfekt mittigen Quelllinie nicht über die verfügbare Straßenfläche hinaus. Es
wird pro sichtbarem Chunk auf der Terrainoberfläche drapiert und verändert
weder Zellen noch Kollision. `classificationSource: true` kennzeichnet ihn
zugleich als Quelle für die spätere Straßenflurstück-Erkennung; autorisierte
Straßenänderungen laufen getrennt über Tentacle-/WorldEdit-Befehle.

Globale Defaults lassen sich als JSON-Array über
`VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON` ersetzen. Neue Renderer wie Raster,
Volumen oder 3D-Objekte erhalten eigene `renderer.kind`-Adapter und bleiben von
der Terrain-Pipeline unabhängig.
