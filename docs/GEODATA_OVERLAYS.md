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

Wichtig für Flurstücke: Dieses gelbe `surface-lines`-Overlay ist nur der
allgemeine Katasterkontext. Sobald Flurstücke ausgewählt sind, erzeugt der
Editor zusätzlich eine transparente Auswahlfläche, eine blaue verbindliche
Grenze und das rote Grundstücksraster auf einer festen horizontalen Ebene.
Terrain-Blöcke dürfen diese Rasterebene nicht anheben. Der vollständige Vertrag
steht in
[`../../vectoplan-editor/docs/PARCEL_GRID_AND_WORLDEDIT.md`](../../vectoplan-editor/docs/PARCEL_GRID_AND_WORLDEDIT.md).

## Standard

Ohne weitere Konfiguration ist ein Layer aktiv:

- Overlay-ID: `parcel-boundaries`
- Orchestrator-Datensatz: `flurstuecke`
- WFS-Typ: `public:flurstuecke`
- Renderer: `surface-lines`
- semantische Rolle: `parcel-boundary`

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

Dieselbe Struktur kann später einen Strassen-Layer mit
`role: "street-network"` und `classificationSource: true` deklarieren. Die
heutige Ausbaustufe bewahrt diese Semantik im Renderobjekt, wertet sie aber noch
nicht für Strassenblöcke oder die Creative Library aus.

Globale Defaults lassen sich als JSON-Array über
`VECTOPLAN_CHUNK_GEODATA_OVERLAYS_JSON` ersetzen. Neue Renderer wie Raster,
Volumen oder 3D-Objekte erhalten eigene `renderer.kind`-Adapter und bleiben von
der Terrain-Pipeline unabhängig.
