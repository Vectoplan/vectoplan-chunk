# Muster.md – VECTOPLAN Flask/Python-Service-Muster

<!-- Muster.md -->

## Status dieser Fassung

Diese Datei beschreibt den **IST-Zustand des aktuellen Muster-Projekts** für VECTOPLAN-Services.

Wichtig:

Dieses Muster ist **kein fachlicher Service** und keine fertige Produktlogik.

Es ist ein **lauffähiges Flask-/Python-Grundgerüst**, das als Startpunkt für VECTOPLAN-Microservices dient.

Das Muster zeigt:

- wie ein Service sauber startet
- wie `app.py`, `wsgi.py`, `config.py`, `routes/`, `src/`, `extensions.py`, `Dockerfile` und `entrypoint.sh` zusammenspielen
- wie eine robuste App-Factory aufgebaut ist
- wie Blueprints registriert werden
- wie Startup-Hooks eingebunden werden
- wie ein Container über Gunicorn startet
- wie defensive Defaults und Fallbacks aussehen können
- wie alle VECTOPLAN-Services grundsätzlich gleich strukturiert werden sollen

Der Zweck dieses Musters ist nicht, bereits Fachlogik zu liefern.

Der Zweck ist:

**Ein Service soll sofort startfähig sein und eine einheitliche Struktur besitzen, damit er danach kontrolliert ausgebaut werden kann.**

---

## 1. Zweck des Muster-Projekts

Das Muster-Projekt ist die technische Grundlage für neue VECTOPLAN-Services.

Es basiert auf Flask und Python und stellt ein lauffähiges Service-Gerüst bereit.

Es soll vor allem verhindern, dass jeder neue Service anders aufgebaut wird.

Stattdessen sollen alle Services dieselbe Grundform besitzen:

```text
app.py
wsgi.py
config.py
extensions.py
routes/
src/
models/
bootstrap/
tests/
Dockerfile
entrypoint.sh
requirements.txt
```

Nicht jeder Service muss sofort alle Ordner vollständig nutzen.

Aber die Grundidee bleibt gleich:

```text
Flask-App-Factory
→ Konfiguration
→ Extensions
→ Routes
→ Startup-Hooks
→ src-Logik
→ optional Models
→ Docker-/Gunicorn-Start
```

---

## 2. Wichtigster Leitsatz

Der wichtigste Leitsatz für dieses Muster lautet:

**Das Muster ist ein lauffähiges Flask-/Python-Service-Gerüst, nicht die Fachlogik des jeweiligen Services.**

Noch präziser:

- `app.py` erzeugt die Flask-App.
- `config.py` bündelt Service-Konfiguration.
- `routes/` enthält HTTP-Adapter.
- `src/` enthält spätere Fach- und Service-Logik.
- `models/` enthält später Datenbankmodelle, falls der Service PostgreSQL nutzt.
- `extensions.py` hält interne Erweiterungs- und Initialisierungszustände.
- `src/bootstrap/` enthält Startup-Prüfungen.
- `Dockerfile` und `entrypoint.sh` machen den Service containerfähig.
- `wsgi.py` stellt die App für Gunicorn bereit.

Das Muster soll nur sicherstellen:

```text
Der Service startet.
Die Struktur ist verständlich.
Die Erweiterungspunkte sind vorbereitet.
```

---

## 3. Aktueller IST-Zustand

Der aktuelle Stand ist ein robustes Flask-/Python-Gerüst, das ursprünglich am Beispiel `vectoplan-editor` formuliert wurde.

Es enthält bereits:

- robuste Flask-App-Factory
- defensive `.env`-Ladung
- zentrale Konfiguration
- WSGI-Einstiegspunkt
- Blueprint-Registrierung
- eine Beispielroute
- Template-/Static-Unterstützung
- Fallback-HTML bei Template-Problemen
- interne Extension-Registry
- Startup-Hooks unter `src/bootstrap/`
- Dockerfile
- Entrypoint-Script
- Gunicorn-Start
- Prestart-Check
- Requirements
- erste Testfähigkeit

Wichtig:

Die aktuell gezeigten Dateien enthalten an mehreren Stellen noch editorbezogene Namen wie:

```text
vectoplan-editor
VECTOPLAN_EDITOR_*
/editor
routes/editor.py
templates/editor/
static/editor/
```

Das ist im Musterkontext akzeptabel.

Für neue Services müssen diese Namen servicebezogen angepasst werden.

Beispiel für `vectoplan-chunk`:

```text
vectoplan-editor      → vectoplan-chunk
VECTOPLAN_EDITOR_*    → VECTOPLAN_CHUNK_*
/editor               → /health, /blocks, /worlds, /chunks, /commands
routes/editor.py      → routes/chunks.py, routes/blocks.py, routes/commands.py
templates/editor/     → nur falls ein Service HTML braucht
static/editor/        → nur falls ein Service Static Assets braucht
```

---

## 4. Was das Muster bewusst ist

Das Muster ist:

1. Ein lauffähiges Flask-/Python-Grundprojekt
2. Ein technisches Startgerüst für neue Services
3. Eine einheitliche Ordner- und Dateistruktur
4. Ein Beispiel für robuste App-Factory-Struktur
5. Ein Beispiel für zentrale Konfiguration
6. Ein Beispiel für defensive Startup-Hooks
7. Ein Beispiel für saubere Blueprint-Registrierung
8. Ein Beispiel für Containerstart mit Gunicorn
9. Ein Beispiel für Prestart-Prüfungen
10. Ein Ausgangspunkt für fachliche Erweiterung in `/src`

---

## 5. Was das Muster bewusst nicht ist

Das Muster ist nicht:

1. Kein fertiger Fachservice
2. Keine Chunk-Logik
3. Keine Editor-Runtime
4. Keine Core-Logik
5. Keine Library-Logik
6. Keine Datenbanklogik, solange `models/` nicht servicebezogen ergänzt wurde
7. Kein finales Produktverhalten
8. Keine zwingende UI-Vorgabe für alle Services
9. Kein Plugin-System
10. Kein vollständiger Architekturvertrag des jeweiligen Zielservices

Das Muster soll nur die technische Startfähigkeit herstellen.

---

## 6. Aktuelle Datei- und Ordnerstruktur

Das Muster orientiert sich an dieser Grundstruktur:

```text
service-name/
  app.py
  wsgi.py
  config.py
  extensions.py
  requirements.txt
  Dockerfile
  entrypoint.sh

  bootstrap/
    __init__.py
    startup.py
    health.py

  routes/
    __init__.py
    editor.py oder service-spezifische routes

  src/
    bootstrap/
      __init__.py
      startup.py

  templates/
    editor/
      index.html

  static/
    editor/
      css/
        editor.css
      js/
        main.js

  tests/
    integration/
```

Für echte Zielservices kann diese Struktur erweitert werden.

Für `vectoplan-chunk` wäre die spätere Zielstruktur eher:

```text
vectoplan-chunk/
  app.py
  wsgi.py
  config.py
  extensions.py
  requirements.txt
  Dockerfile
  entrypoint.sh

  routes/
    __init__.py
    health.py
    blocks.py
    worlds.py
    chunks.py
    commands.py

  models/
    __init__.py
    planet.py
    world.py
    block.py
    chunk.py
    event.py

  src/
    bootstrap/
    blocks/
    coordinates/
    world/
    chunks/
    commands/
    events/
    repositories/
    api/
    exchange/
    utils/

  tests/
    unit/
    integration/
    e2e/
```

---

## 7. Dateirollen im aktuellen Muster

## 7.1 `app.py`

`app.py` enthält die Flask-App-Factory.

Aktuelle Aufgaben:

- Service-Root ermitteln
- Service-Root in `sys.path` aufnehmen
- `.env` laden
- Konfigurationsklasse auflösen
- Flask-App mit Template- und Static-Pfaden erstellen
- Konfiguration anwenden
- Logger konfigurieren
- App-Defaults setzen
- Konfiguration validieren
- Blueprints registrieren
- optionale Startup-Hooks ausführen
- Metadaten in `app.extensions` ablegen

Bewertung:

`app.py` ist als robustes Service-Factory-Muster geeignet.

Wichtig:

In servicebezogenen Kopien müssen Namespace, ENV-Prefix und Service-Metadaten angepasst werden.

---

## 7.2 `config.py`

`config.py` enthält die zentrale Service-Konfiguration.

Aktuelle Aufgaben:

- Umgebungsvariablen defensiv lesen
- Strings, Booleans und Integer normalisieren
- Service-Pfade definieren
- Default-, Development-, Testing- und Production-Konfiguration bereitstellen
- Konfigurationsklasse über Namen auflösen
- erste Template-/Route-/Static-Werte bündeln
- einfache Validierung anbieten

Bewertung:

Die Struktur ist als Muster sinnvoll.

Wichtig:

Die aktuellen Variablen sind editorbezogen.

Für andere Services müssen sie angepasst werden.

Beispiel:

```text
VECTOPLAN_EDITOR_CONFIG
```

wird bei `vectoplan-chunk` zu:

```text
VECTOPLAN_CHUNK_CONFIG
```

---

## 7.3 `wsgi.py`

`wsgi.py` ist der standardisierte WSGI-Einstiegspunkt.

Aktuelle Aufgaben:

- `create_app()` importieren
- App einmal pro Prozess gecacht erzeugen
- `app` und `application` exportieren
- optional lokalen Direktstart ermöglichen

Bewertung:

Die Datei ist als Muster stabil und serviceübergreifend geeignet.

Für neue Services müssen nur Namen und ENV-Präfixe angepasst werden.

---

## 7.4 `extensions.py`

`extensions.py` enthält eine interne Extension-Registry.

Aktuelle Aufgaben:

- Namespace unter `app.extensions` anlegen
- interne Subsysteme registrieren
- Initialisierungsstatus halten
- Warnungen und Fehler speichern
- Extension-Zusammenfassungen liefern
- spätere Erweiterungen vorbereiten

Bewertung:

Für ein Minimalprojekt ist diese Datei robuster als zwingend nötig, aber als gemeinsames Service-Muster sinnvoll.

Sie schafft einen einheitlichen Platz für spätere Erweiterungen, ohne `app.py` zu überladen.

Für neue Services muss der Namespace angepasst werden.

Beispiel:

```text
vectoplan_editor
```

sollte bei `vectoplan-chunk` zu:

```text
vectoplan_chunk
```

werden.

---

## 7.5 `routes/__init__.py`

`routes/__init__.py` ist die zentrale Blueprint-Registrierung.

Aktuelle Aufgaben:

- Blueprint-Spezifikationen definieren
- Route-Module defensiv importieren
- Blueprints auflösen
- Doppelregistrierung verhindern
- Routing-Metadaten speichern

Bewertung:

Die Datei ist als gemeinsames Muster geeignet.

Für neue Services müssen die Blueprint-Spezifikationen angepasst werden.

Beispiel `vectoplan-chunk`:

```text
routes.health:health_bp
routes.blocks:blocks_bp
routes.worlds:worlds_bp
routes.chunks:chunks_bp
routes.commands:commands_bp
```

---

## 7.6 `routes/editor.py`

`routes/editor.py` ist im aktuellen Muster eine Beispielroute.

Aktuelle Aufgaben:

- `GET /editor` bereitstellen
- HTML-Shell rendern
- Konfigurationswerte defensiv lesen
- Template-Fallback anbieten
- konservative Response-Header setzen

Bewertung:

Diese Datei zeigt, wie eine Route robust aufgebaut werden kann.

Für Services ohne UI ist diese Datei nicht zwingend nötig.

Für `vectoplan-chunk` sollte diese Datei nicht fachlich übernommen werden, sondern durch API-Routen ersetzt werden.

Beispiel:

```text
routes/blocks.py
routes/worlds.py
routes/chunks.py
routes/commands.py
```

---

## 7.7 `src/bootstrap/__init__.py`

Diese Datei stellt eine kontrollierte öffentliche API für das Bootstrap-Package bereit.

Aktuelle Aufgaben:

- Lazy-Import von `src.bootstrap.startup`
- Re-Export von Startup-Funktionen
- klare Fehlertexte bei fehlender Startup-Datei
- stabile Importgrenze

Bewertung:

Als Muster sinnvoll.

Sie erlaubt, Startup-Hooks serviceübergreifend ähnlich aufzubauen.

---

## 7.8 `src/bootstrap/startup.py`

`src/bootstrap/startup.py` enthält Startup-Hooks und Strukturprüfungen.

Aktuelle Aufgaben:

- Startup-State in `app.extensions` führen
- Pfade prüfen
- Dateien prüfen
- Routen prüfen
- Extension-Registry initialisieren
- Metadaten sammeln
- Strict-Mode unterstützen
- Startup-Zusammenfassung liefern

Bewertung:

Die Datei ist ein robustes Muster für Services, die beim Start ihren Zustand prüfen sollen.

Für neue Services müssen die geprüften Pflichtdateien und Kernrouten angepasst werden.

Für `vectoplan-chunk` wäre nicht `/editor` die Kernroute, sondern eher:

```text
/health
/blocks
/worlds/default
/chunks
/commands
```

---

## 7.9 `Dockerfile`

Das Dockerfile ist ein lauffähiges Container-Muster.

Aktuelle Aufgaben:

- Python 3.12 Slim verwenden
- Non-Root-User anlegen
- Requirements installieren
- App-Code kopieren
- Rechte setzen
- Port 5000 exponieren
- Healthcheck definieren
- Gunicorn starten
- optional `entrypoint.sh` verwenden

Bewertung:

Das Dockerfile ist als allgemeines Muster geeignet.

Für andere Services müssen angepasst werden:

- Image-Titel
- Beschreibung
- APP_HOME
- ENV-Prefix
- Healthcheck-Route
- Service-Name

Für `vectoplan-chunk` sollte der Healthcheck nicht auf `/editor`, sondern auf `/health` zeigen.

---

## 7.10 `entrypoint.sh`

`entrypoint.sh` ist der Container-Startpunkt.

Aktuelle Aufgaben:

- ENV-Defaults setzen
- Werte normalisieren
- Arbeitsverzeichnis prüfen
- zentrale Dateien prüfen
- optional Assets prüfen
- Python-Bootstrap-Check ausführen
- Startzusammenfassung loggen
- benutzerdefinierte Commands durchreichen
- Gunicorn oder Python-Direktmodus starten

Bewertung:

Das Script ist als Muster nützlich.

Für neue Services müssen angepasst werden:

- Service-Name
- ENV-Präfix
- Pflichtdateien
- Pflicht-Routen
- Prestart-Ausgabe
- Health-/Asset-Prüfungen

Für API-only-Services wie `vectoplan-chunk` sollten Template-/Static-Prüfungen nicht Pflicht sein.

---

## 7.11 `requirements.txt`

`requirements.txt` enthält die minimalen Python-Abhängigkeiten.

Aktuell enthalten:

```text
Flask
gunicorn
python-dotenv
pytest
```

Bewertung:

Für ein reines lauffähiges Flask-Muster ist das ausreichend.

Für `vectoplan-chunk` werden später zusätzlich Datenbankpakete nötig, zum Beispiel abhängig vom bestehenden Plattform-Stack:

```text
Flask-SQLAlchemy
psycopg / psycopg2
Flask-Migrate oder Alembic
```

Falls diese bereits global im Projektstandard vorhanden sind, sollte sich der Service daran halten.

---

## 8. Was aktuell bereits funktioniert

Das Muster ist darauf ausgelegt, dass der Service grundsätzlich starten kann.

Aktuell vorhanden:

- App-Factory
- WSGI-App
- Konfiguration
- Blueprint-Registrierung
- Beispielroute
- Template-/Static-Anbindung
- Startup-Hooks
- Extension-Registry
- Docker-Start
- Gunicorn-Start
- Prestart-Check
- defensive Fallbacks

Damit ist das Muster geeignet als:

```text
kopierbares Startgerüst für neue VECTOPLAN-Services
```

---

## 9. Was bewusst noch nicht enthalten ist

Das Muster enthält bewusst keine fachliche Service-Logik.

Nicht enthalten:

- keine Chunk-Models
- keine PostgreSQL-Models
- keine SQLAlchemy-Initialisierung, sofern nicht später ergänzt
- keine BlockRegistry
- keine World-Generatoren
- keine Command-Executoren
- keine Core-Clients
- keine Library-Clients
- keine echten Business-Services
- keine Domain-Repositories
- keine produktive Authentifizierung
- keine Realtime-Logik
- keine Exportlogik

Diese Dinge gehören später servicebezogen in:

```text
models/
src/
routes/
```

---

## 10. Bedeutung für alle VECTOPLAN-Projekte

Alle VECTOPLAN-Services sollen nach demselben Grundmuster aufgebaut sein.

Das bedeutet nicht, dass jede Datei identisch bleibt.

Es bedeutet:

```text
gleiche Startlogik
gleiche App-Factory-Idee
gleiche Config-Struktur
gleiche Blueprint-Registrierung
gleiche Trennung von routes/src/models
gleiche Docker-/Entrypoint-Grundform
gleiche Startup- und Health-Denkweise
```

Der Vorteil:

- neue Entwickler finden sich schneller zurecht
- Services sehen gleich aus
- Startprobleme sind leichter zu finden
- Docker-Verhalten ist vergleichbar
- Tests können ähnlicher aufgebaut werden
- spätere Automatisierung wird einfacher

---

## 11. Anwendung auf `vectoplan-chunk`

Für `vectoplan-chunk` sollte das Muster als Startbasis genutzt werden.

Dabei sollte nicht die Editor-Fachlogik übernommen werden.

Stattdessen:

```text
Editor-spezifische Route entfernen oder ersetzen
Chunk-spezifische API-Routen ergänzen
Models für PostgreSQL anlegen
Chunk-Logik in /src aufbauen
Startup-Checks auf Chunk-Service anpassen
ENV-Präfix auf VECTOPLAN_CHUNK_* ändern
Extension-Namespace auf vectoplan_chunk ändern
Docker-Healthcheck auf /health ändern
```

Erste sinnvolle Zielstruktur für `vectoplan-chunk`:

```text
vectoplan-chunk/
  app.py
  wsgi.py
  config.py
  extensions.py
  requirements.txt
  Dockerfile
  entrypoint.sh

  routes/
    __init__.py
    health.py
    blocks.py
    worlds.py
    chunks.py
    commands.py

  models/
    __init__.py
    planet.py
    world.py
    block.py
    chunk.py
    event.py

  src/
    bootstrap/
    blocks/
    coordinates/
    world/
    chunks/
    commands/
    events/
    repositories/
    api/
    utils/

  tests/
```

---

## 12. Anpassungen, die bei Service-Kopie zwingend nötig sind

Wenn dieses Muster kopiert wird, müssen mindestens diese Punkte angepasst werden:

## 12.1 Service-Name

Von:

```text
vectoplan-editor
```

zu:

```text
vectoplan-chunk
```

oder dem jeweiligen Service-Namen.

## 12.2 ENV-Präfix

Von:

```text
VECTOPLAN_EDITOR_*
```

zu:

```text
VECTOPLAN_CHUNK_*
```

oder entsprechendem Service-Präfix.

## 12.3 Extension-Namespace

Von:

```text
vectoplan_editor
```

zu:

```text
vectoplan_chunk
```

## 12.4 Routen

Von:

```text
/editor
```

zu servicebezogenen API-Routen.

Für Chunk:

```text
/health
/blocks
/worlds/default
/chunks
/chunks/batch
/commands
```

## 12.5 Docker-Healthcheck

Von:

```text
/editor
```

zu:

```text
/health
```

## 12.6 Startup-Checks

Die Pflichtdateien und Kernrouten müssen zum Zielservice passen.

Bei `vectoplan-chunk` sollten Template-/Static-Dateien nicht als Kernanforderung gelten.

---

## 13. Wichtige Invarianten des Musters

Diese Regeln sollen für alle VECTOPLAN-Services gelten:

1. Jeder Service hat eine `create_app()`-Factory.
2. Jeder Service hat einen `wsgi.py`-Einstieg.
3. Jeder Service hat eine zentrale `config.py`.
4. Jeder Service registriert Blueprints über `routes/__init__.py`.
5. Routen enthalten keine tiefe Business-Logik.
6. Fachlogik lebt in `/src`.
7. Datenbankmodels leben in `/models`.
8. Startup-Hooks leben in `/src/bootstrap`.
9. Containerstart läuft über Gunicorn.
10. `entrypoint.sh` prüft den Service vor dem Start.
11. Service-Namen und ENV-Präfixe müssen servicebezogen sein.
12. Die Struktur darf wachsen, aber nicht chaotisch auseinanderlaufen.
13. Das Muster ist startfähig, aber fachlich leer.
14. Neue Services sollen mit derselben Struktur beginnen.

---

## 14. Bekannte Grenzen des aktuellen Musters

Das Muster ist aktuell noch editornah benannt.

Bekannte Grenzen:

- viele Namen enthalten noch `editor`
- ENV-Variablen sind editorbezogen
- Route `/editor` ist UI-orientiert
- Template-/Static-Prüfungen passen nicht zu API-only-Services
- Docker-Healthcheck prüft `/editor`
- `extensions.py` nutzt Namespace `vectoplan_editor`
- Startup-Prüfungen erwarten Editor-Dateien
- keine PostgreSQL-Modelle enthalten
- keine Service-Domain enthalten

Diese Punkte sind keine Fehler des Musters.

Sie zeigen nur, was bei der Übertragung auf einen neuen Service angepasst werden muss.

---

## 15. Akzeptanzkriterien für das Muster

Das Muster gilt als nutzbar, wenn:

```text
create_app() funktioniert
wsgi:app exportiert wird
Konfiguration geladen wird
Blueprints registriert werden
Startup-Hooks laufen oder sauber übersprungen werden
Container mit Gunicorn startet
Health- oder Beispielroute erreichbar ist
Prestart-Check klare Fehler liefert
Ordnerstruktur verständlich ist
```

Für das aktuell editorbasierte Muster bedeutet das:

```text
GET /editor
```

liefert eine sichtbare Seite oder Fallback-Shell.

Für `vectoplan-chunk` sollte das Ziel stattdessen sein:

```text
GET /health
```

liefert eine JSON-Antwort.

---

## 16. Empfohlene nächste Schritte für `vectoplan-chunk`

Ausgehend von diesem Muster:

1. Service-Namen von Editor auf Chunk umstellen
2. ENV-Präfixe auf `VECTOPLAN_CHUNK_*` umstellen
3. `routes/editor.py` nicht weiter fachlich nutzen
4. `routes/health.py` ergänzen
5. `routes/blocks.py`, `routes/worlds.py`, `routes/chunks.py`, `routes/commands.py` vorbereiten
6. `models/` anlegen
7. PostgreSQL-Models registrieren
8. `/src` für Chunk-Mechanik aufbauen
9. Docker-Healthcheck auf `/health` ändern
10. Startup-Checks auf Chunk-Service anpassen

Danach kann die eigentliche Chunk-Logik beginnen.

---

## 17. Kurzfassung

Das aktuelle Muster-Projekt ist ein **lauffähiges Flask-/Python-Service-Gerüst**.

Es dient als Beispiel für:

- App-Factory
- Konfiguration
- WSGI
- Blueprint-Registrierung
- Startup-Hooks
- Extension-Registry
- Dockerfile
- Entrypoint
- Gunicorn-Start
- einheitliche Service-Struktur

Es ist nicht die fertige Fachlogik.

Für neue VECTOPLAN-Services soll dieses Muster kopiert, servicebezogen umbenannt und dann in `/src`, `routes/` und `models/` fachlich erweitert werden.

Der wichtigste Punkt:

**Alle VECTOPLAN-Services sollen dieselbe Grundstruktur haben, damit sie einheitlich startbar, testbar, wartbar und ausbaubar bleiben.**
```