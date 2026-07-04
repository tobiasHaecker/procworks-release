<!-- SPDX-License-Identifier: BUSL-1.1 -->
# ProcWorks · Correctness by Construction

> Self-hosted Werkzeug zur **stabilen Prozessmodellierung**,
> **Instanzerstellung/-ausführung** und **intuitiven, modernen Bedienung** –
> auf Basis der Forschungsidee *Correctness by Construction* (ADEPT2,
> Universität Ulm).

[![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)

Dies ist das **Auslieferungs-Repository** von ProcWorks: die Codebasis
(Backend-Kern + Web-Client), Lizenz und die Anleitung zur Inbetriebnahme.
Entwicklung, Konzeptdokumente und Historie liegen in einem separaten,
internen Repository.

---

## Schnellstart: In 15 Minuten einsatzbereit

> ⚠️ **Haftungsausschluss – bitte vor der Inbetriebnahme lesen.** ProcWorks wird
> **„wie besehen", ohne jede Gewährleistung und ohne jede Haftung**
> bereitgestellt. Inbetriebnahme und Nutzung erfolgen **ausschließlich auf
> eigenes Risiko**. Prüfen Sie das Werkzeug zuerst in einer **isolierten
> Testumgebung** und legen Sie **Backups** an. Vollständiger Text:
> [DISCLAIMER.md](DISCLAIMER.md).

> Für mittelständische Unternehmen **ohne eigene IT-Abteilung**. Sie brauchen
> kein Vorwissen – nur einen Rechner mit Internet. ProcWorks startet als ein
> einziger, in sich geschlossener Container-Verbund (Datenbank + Server +
> Oberfläche); Sie installieren **eine** Voraussetzung und führen **einen**
> Befehl aus.

### Standardfall: Windows Server (nichts vorinstalliert)

Auf einem frischen Windows Server sind genau **drei kostenlose Programme** nötig.
Jedes wird per Mausklick installiert – die ausführliche, bebilderte
Schritt-für-Schritt-Anleitung steht in
[docs/Windows-Server-Setup.md](docs/Windows-Server-Setup.md).

1. **WSL2 aktivieren** – einmalig in der PowerShell (als Administrator):
   `wsl --install`, danach den Server neu starten.
2. **Docker Desktop** installieren – von <https://www.docker.com/products/docker-desktop/>,
   bei der Installation „Use WSL 2" aktiviert lassen und einmal starten, bis
   „Engine running" erscheint.
3. **Git** installieren – von <https://git-scm.com/download/win> (Standardoptionen
   genügen).

Danach in der PowerShell **diese vier Zeilen** ausführen (holt ProcWorks und
startet alles):

```powershell
cd C:\
git clone https://github.com/tobiasHaecker/procworks-release.git ProcWorks
cd C:\ProcWorks
docker compose -f deploy/docker-compose.full.yml up --build -d
```

Fertig. Im Browser `http://localhost` öffnen – es erscheint das Login-Fenster.

#### Erste Anmeldung als Administrator

- **Login:** `admin`
- **Passwort:** ein **einmaliges Start-Passwort**, das beim allerersten Start
  automatisch erzeugt und **ins Server-Log geschrieben** wird. Beim ersten
  Anmelden verlangt das System sofort ein eigenes, neues Passwort.

So lesen Sie das Start-Passwort aus dem Server-Log – in der **PowerShell**, aus
dem Ordner `C:\ProcWorks`:

```powershell
docker compose -f deploy/docker-compose.full.yml logs api | Select-String "Initial admin"
```

Die gesuchte Zeile sieht so aus (das Passwort steht hinter `temporary password=`):

```text
Initial admin account created (login='admin', temporary password='…').
```

> Tipp: Wer das Passwort lieber vorab selbst festlegt, setzt es in der
> Compose-Datei über `PROCWORKS_ADMIN_PASSWORD` (siehe
> [Windows-Anleitung, Abschnitt 5](docs/Windows-Server-Setup.md)). Dann entfällt
> der Blick ins Log.

### macOS / Linux (zum Ausprobieren)

Voraussetzung ist nur **Docker** (Docker Desktop auf macOS, Docker Engine unter
Linux). Dann:

```bash
git clone https://github.com/tobiasHaecker/procworks-release.git procworks
cd procworks
docker compose -f deploy/docker-compose.full.yml up --build -d
# Oberfläche: http://localhost   ·   Login: admin
# Das einmalige Start-Passwort steht im Server-Log (hinter "temporary password="):
docker compose -f deploy/docker-compose.full.yml logs api | grep "Initial admin"
```

### Sofort ausprobieren: Beispieldaten laden

Damit alle Funktionen **sofort greifbar** sind, bringt ProcWorks fertige
Beispieldaten mit (eine Organisation „Acme", zwei Prozesse und drei laufende
Instanzen). So laden Sie sie:

1. Als **Administrator** anmelden.
2. In die Sicht **Monitoring** wechseln, ganz unten zum Bereich
   **„Wartung (Administrator)"** scrollen.
3. **„Beispieldaten laden"** klicken und bestätigen.

Derselbe Bereich enthält **„Auf Null zurücksetzen"**, um jederzeit wieder mit
einem leeren System zu starten. Beides ist **nur für Administratoren** sichtbar.

Nach dem Laden können Sie sich mit den **Testbenutzern** anmelden (Passwort für
alle: `demo-procworks`):

| Login | Person | Rolle | Sieht / kann |
| --- | --- | --- | --- |
| `mara.modell` | Mara Modell | Modellierer | Prozesse modellieren, Daten/Organisation pflegen |
| `erika.sander` | Erika Sander | Bearbeiter | Aufgaben erledigen (hat offene Urlaubsanträge) |
| `tom.berger` | Tom Berger | Bearbeiter (Leitung) | Genehmigungen erteilen |
| `vera.viewer` | Vera Viewer | Leser | Monitoring nur ansehen |

> Die Testbenutzer existieren erst **nach** dem Laden der Beispieldaten und nur
> im Login-Betrieb (Standard im mitgelieferten Stack). Bitte vor dem
> Produktivbetrieb über **„Auf Null zurücksetzen"** entfernen.

## Inhalt dieses Repositories

```text
.
- core/                        # Headless Backend-Kern (Python/FastAPI)
- web/                         # Schlanker No-Build-Web-Client (HTML/CSS/JS)
- deploy/                      # Caddyfile, docker-compose.full.yml, Helm-Chart
- docs/Windows-Server-Setup.md # Ausführliche Installations- und Betriebsanleitung
- LICENSE                      # Business Source License 1.1
- DISCLAIMER.md                # Vollständiger Haftungsausschluss
```

Details zum Backend-Kern (API-Endpunkte, lokale Entwicklung, Tests):
[core/README.md](core/README.md).

## Lizenz

Veröffentlicht unter der Business Source License 1.1 (BUSL-1.1) –
quelloffen und kostenlos zum Testen, Entwickeln und für
nicht-konkurrierende Produktivnutzung, ohne jegliche Gewährleistung
oder Haftung. Eine konkurrierende kommerzielle Nutzung (insbesondere
das Anbieten als gehosteter/eingebetteter Dienst im Wettbewerb zum
Lizenzgeber oder der Weiterverkauf als kommerzielles
Prozessmanagement-Produkt) erfordert eine kommerzielle Lizenz des
Lizenzgebers. Siehe [LICENSE](LICENSE). Für kommerzielle Lizenzen wende dich
an den Lizenzgeber.

### Haftungsausschluss

ProcWorks wird **„wie besehen" (as is)**, **ohne jede Gewährleistung** und
**ohne jede Haftung** bereitgestellt. Bezug, Installation, Inbetriebnahme und
Nutzung erfolgen **ausschließlich auf eigenes Risiko und in eigener
Verantwortung**. Vollständiger Text: [DISCLAIMER.md](DISCLAIMER.md).

© 2026 Tobias Häcker – alle Rechte vorbehalten.
