# Editor-Datensatzpipeline

Die Pipeline trennt externe Geodaten von der schreibbaren Voxelwelt. Sie baut
einen unveränderlichen, inhaltsadressierten Datensatz; die eigentliche
Materialisierung läuft anschließend weiterhin über die kanonischen
Chunk-/WorldEdit-Befehle. Dadurch gibt es keine zweite Schreibwahrheit.

## Format

`vectoplan-editor-dataset.v1` enthält:

- den unveränderlichen Earth-Frame und die Chunkgröße,
- editierbare LoD2-Wandblöcke und WorldEdit-Dachobjekte,
- bestandsgebäudeorientierte Grundstücksraster,
- normalisierte Straßenmittellinien mit 6,0 m Nennbreite und einer separat
  auditierten, auf die verfügbare Straßen-/Flurstücksfläche begrenzten Breite,
- pro Chunk aufgeteilte Artefakte,
- für jeden Prozess Eingangs-/Ausgangsfingerprint und Version.

`sourceBounds` ist verpflichtend und auf das explizite LoD2-Importfenster von
maximal 512 × 512 Weltzellen begrenzt. Der Earth-Frame des LoD2-Plans muss mit
dem Referenzfingerprint des Datensatzes übereinstimmen. Damit kann weder ein
unbegrenzter WFS-Abruf noch Geometrie aus einem anderen Projektframe unbemerkt
in ein Bundle gelangen.

Jeder Verarbeitungsschritt besitzt einen eigenen Quellordner unter
`src/editor_dataset/processes/` und im erzeugten Bundle einen eigenen Ordner
unter `processes/<process-id>/`. Ein vorhandenes Ziel wird nie überschrieben.

## Prozessfolge

1. `lod2-editable`: LoD2-Hüllen werden zu abbrechbaren 1-m-Wandblöcken und
   WorldEdit-Dächern normalisiert.
2. `parcel-grid`: übernimmt die am Bestand verankerten Achsen, Fassadenanker
   und Teilungsregeln. Leere Grundstücke werden absichtlich nicht erfunden;
   dort bleibt das bestehende grenzorientierte Raster der autoritative Fall.
3. `road-network`: normalisiert und dedupliziert Straßenmittellinien
   richtungsunabhängig, vergibt geometriebasierte stabile IDs und versieht sie
   mit 6,0 m Nennbreite. `effectiveWidthM`/`segmentWidthsM` begrenzen das
   symmetrische Band auf eine explizit gelieferte Breite und auf die nächsten
   Flurstücksgrenzen; die Quelllinien werden nicht verändert.
4. `chunk-pack`: ordnet Wandblöcke, die gesamte räumliche Dach-Footprint-
   Abdeckung, Rasterabdeckung und tatsächlich geschnittene Straßenbänder
   deterministisch den Editor-Chunks zu. Ein WorldEdit-Dach ist dadurch nicht
   nur in seinem Anker-Chunk auffindbar.

Die Ausgabe wird zuerst vollständig in einem gleichgeordneten temporären
Verzeichnis aufgebaut und erst danach unter einem exklusiven Bundle-Lock
atomar veröffentlicht. Ein vorhandenes Ziel, ein ungültiger Chunk-Schlüssel,
ein veränderter Fingerprint oder ein nicht erfolgreicher Prozess führt zum
Abbruch; das Ziel wird nicht teilweise geschrieben. `generatedAt` ist reine
Publikationsmetadaten und daher nicht Teil des deterministischen
`contentFingerprint`.

## Projektbundle bauen

Im Chunk-Container bzw. in dessen Python-Umgebung:

```powershell
python scripts/build_editor_dataset.py `
  --app-project-id prj_da09805bc6e54b29816c8cd6 `
  --radius 128 `
  --output /var/lib/vectoplan-chunk/terrain-cache/editor-datasets/test1-berlin
```

Der persistente Root ist standardmäßig
`/var/lib/vectoplan-chunk/terrain-cache/editor-datasets` und kann über
`VECTOPLAN_CHUNK_EDITOR_DATASET_ROOT` geändert werden. Ein vorhandenes Bundle
wird ohne Umbenennen projektgebunden aktiviert:

```powershell
python scripts/activate_editor_dataset.py `
  --app-project-id prj_da09805bc6e54b29816c8cd6 `
  --dataset test1-berlin `
  --apply
```

Der Aktivierungsbefehl löst Projekt und Welt gegen die Chunk-Datenbank auf,
akzeptiert ausschließlich Bundles innerhalb dieses Roots und schreibt den
kleinen Selektor erst nach erfolgreichem Commit der kanonischen LoD2-
Materialisierung atomar. Ohne `--apply` ist derselbe Aufruf ein reiner Dry-run
und zeigt den rekonstruierten Importplan an; er schreibt weder Welt noch
Selektor. Manifest-, Prozess-, Layer- und Chunk-Fingerprints werden vor jeder
Verwendung geprüft. Diagnoseendpunkte sind:

- `GET /projects/<project>/worlds/<world>/editor-datasets/active`
- `GET /projects/<project>/worlds/<world>/editor-datasets/active/chunks?chunkX=…&chunkY=…&chunkZ=…`

Normale Chunk-Antworten konsumieren das aktive Bundle automatisch über die
bereits vorhandenen Editor-Verträge: Dächer erscheinen als vollständige
semantische `objectRefs`, das Bestandsraster bleibt im Dach-Metadatensatz und
Straßen werden als `geodata-overlays.v1`/`surface-ribbons` geliefert. Es gibt
keinen zweiten Frontend-Loader. Live-WFS bleibt unverändert aktiv, wenn kein
Bundle selektiert ist oder der angeforderte Bundle-Chunk keine Straßen enthält.
Straßenbänder besitzen ausschließlich im Projektions-Chunk `chunkY=0` einen
Render-Owner.

Wandzellen werden bewusst **nicht** als scheinbar schreibbare Response-Zellen
eingeblendet. Sie werden mit dem folgenden Importbefehl über die kanonische
WorldEdit-Befehlskette materialisiert. Damit bleiben Abbau, Undo-/Command-Log
und Schutz vorhandener Benutzeränderungen korrekt.

Alternativ bleibt die Materialisierung mit einem neuen Source-Store-Plan
weiterhin separat möglich:

```powershell
python scripts/import_lod2_editable.py `
  --app-project-id prj_da09805bc6e54b29816c8cd6 `
  --radius 128 --apply --report /tmp/lod2-import-report.json
```

Straßendaten werden räumlich pro Chunk über `public:strassendaten` abgefragt;
niemals werden die rund 3,9 Millionen deutschlandweiten Features unbeschränkt
in den Editor geladen. Ist die Straßenquelle für einen angeforderten Chunk
nicht erreichbar oder deaktiviert, bricht der Builder fehlersicher ab, statt
ein scheinbar vollständiges Bundle ohne Straßen zu veröffentlichen.
