# ProcWorks auf einem Windows Server einrichten (Schritt für Schritt)

Diese Anleitung beschreibt die **Erstinstallation von Grund auf**. Es wird davon
ausgegangen, dass auf dem Server **noch nichts vorbereitet** wurde (kein Docker,
kein Git, kein Quellcode).

ProcWorks läuft als Container-Verbund (PostgreSQL + API + Web/Reverse-Proxy).
Der einfachste Weg auf Windows ist **Docker Desktop mit WSL2** zusammen mit der
mitgelieferten Compose-Datei [`deploy/docker-compose.full.yml`](../deploy/docker-compose.full.yml).

> ⚠️ **Haftungsausschluss – vor der Installation lesen.** ProcWorks wird **„wie
> besehen", ohne jede Gewährleistung und ohne jede Haftung** bereitgestellt.
> Installation, Inbetriebnahme und Betrieb erfolgen **ausschließlich auf eigenes
> Risiko und in eigener Verantwortung**. Im größtmöglichen gesetzlich zulässigen
> Umfang wird **keine Haftung** übernommen – weder für Schäden am Server, an
> Betriebssystem, paralleler oder anderer Software, Netzwerken oder Infrastruktur
> (Inbetriebnahme) noch für Verlust/Beschädigung von Daten oder fehlerhafte
> Geschäftsprozesse (Nutzung). Setzen Sie ProcWorks **nicht** ungeprüft auf einem
> produktiven Server ein: Verwenden Sie eine **isolierte Umgebung** bzw. einen
> dedizierten Server, legen Sie **Backups** an und sichern Sie den Zugang ab. Der
> vollständige Text steht in [DISCLAIMER.md](../DISCLAIMER.md).

> Empfohlen: **Windows Server 2022** (oder neuer) mit Internetzugang und
> Administratorrechten. Mindestens 2 CPU-Kerne, 4 GB RAM, 20 GB freier
> Speicher.

---

## Übersicht der Schritte

1. WSL2 aktivieren
2. Docker Desktop installieren
3. Git installieren
4. Quellcode holen
5. Konfiguration anpassen (Passwörter, Login-Modus, Admin)
6. Stack starten
7. Funktion prüfen
8. Erstmalig anmelden und weitere Logins anlegen
9. Betrieb: Autostart, Update, Stopp, Backup

---

## 1. WSL2 aktivieren

Die Linux-Container von ProcWorks benötigen WSL2 (Windows-Subsystem für Linux).

1. **PowerShell als Administrator** öffnen (Startmenü → „PowerShell" → Rechtsklick
   → „Als Administrator ausführen").
2. WSL installieren:

   ```powershell
   wsl --install
   ```

3. **Server neu starten**, wenn dazu aufgefordert wird.
4. Nach dem Neustart prüfen, dass WSL2 Standard ist:

   ```powershell
   wsl --set-default-version 2
   wsl --status
   ```

> Falls `wsl --install` meldet, dass die Virtualisierung deaktiviert ist:
> Virtualisierung (Intel VT-x / AMD-V) im BIOS/UEFI bzw. – bei einer VM – in den
> VM-Einstellungen aktivieren („nested virtualization").

---

## 2. Docker Desktop installieren

1. Installer herunterladen: <https://www.docker.com/products/docker-desktop/>
   (Datei `Docker Desktop Installer.exe`).
2. Installer ausführen, dabei die Option **„Use WSL 2 instead of Hyper-V"**
   aktiviert lassen.
3. Nach der Installation **Docker Desktop starten** und einmal abwarten, bis es
   den Status **„Engine running"** anzeigt.
4. Installation in einer neuen PowerShell prüfen:

   ```powershell
   docker version
   docker compose version
   ```

   Beide Befehle müssen eine Versionsnummer ausgeben.

> Hinweis zur Lizenz: Docker Desktop ist für größere Unternehmen
> kostenpflichtig. Alternativ kann Docker Engine direkt in einer WSL2-Ubuntu-
> Distribution installiert werden; die Compose-Befehle bleiben identisch.

---

## 3. Git installieren

1. Git für Windows herunterladen und installieren:
   <https://git-scm.com/download/win> (Standardoptionen genügen).
2. Prüfen:

   ```powershell
   git --version
   ```

---

## 4. Quellcode holen

1. Einen Zielordner wählen, z. B. `C:\ProcWorks`:

   ```powershell
   cd C:\
   git clone https://github.com/tobiasHaecker/procworks-release.git ProcWorks
   cd C:\ProcWorks
   ```

Alle folgenden Befehle werden **aus diesem Ordner** (`C:\ProcWorks`) ausgeführt.

---

## 5. Konfiguration anpassen

Vor dem ersten Start zwei Dinge anpassen: **sichere Passwörter** und der
**Login-Modus** (Passwort-Login mit initialem Admin).

Öffne die Datei `deploy\docker-compose.full.yml` in einem Editor, z. B.:

```powershell
notepad deploy\docker-compose.full.yml
```

### 5.1 Datenbank-Passwort setzen

Im Abschnitt `postgres:` das Standardpasswort `process` durch ein sicheres
Passwort ersetzen (an **beiden** Stellen – siehe 5.2 die `DATABASE_URL`):

```yaml
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: process
      POSTGRES_PASSWORD: <SICHERES-DB-PASSWORT>
      POSTGRES_DB: procworks
```

### 5.2 API: Login-Modus + Admin-Konto

Im Abschnitt `api:` den `environment:`-Block so ergänzen, dass das
**Passwort-Login** aktiv ist und beim Start ein **Initial-Admin** angelegt wird:

```yaml
  api:
    build:
      context: ../core
    restart: unless-stopped
    environment:
      # Dauerhafte Speicherung in PostgreSQL (DB-Passwort wie in 5.1):
      DATABASE_URL: postgresql+psycopg://process:<SICHERES-DB-PASSWORT>@postgres:5432/procworks
      # Passwort-Login aktivieren:
      PROCWORKS_AUTH: "password"
      # Initialer Administrator (einmaliger Bootstrap, muss beim ersten Login
      # ein eigenes Passwort vergeben):
      PROCWORKS_ADMIN_LOGIN: "admin"
      PROCWORKS_ADMIN_PASSWORD: "<STARTPASSWORT-ADMIN>"
      PROCWORKS_ADMIN_NAME: "Administrator"
      # Optional: Login-Sitzungsdauer in Minuten (Standard 720 = 12 h):
      # PROCWORKS_SESSION_TTL_MINUTES: "720"
    depends_on:
      postgres:
        condition: service_healthy
    expose:
      - "8000"
```

> Hinweis: `PROCWORKS_AUTH: "password"` ist im mitgelieferten Compose-Stack
> bereits Standard. Lässt man `PROCWORKS_ADMIN_LOGIN`/`PROCWORKS_ADMIN_PASSWORD`
> weg, legt der Server beim ersten Start automatisch ein `admin`-Konto an und
> schreibt dessen zufälliges Einmal-Passwort ins API-Log
> (`docker compose -f deploy/docker-compose.full.yml logs api`). Das oben
> gezeigte feste Startpasswort ist die Alternative, wenn man es nicht aus dem Log
> ablesen möchte.

### 5.3 Optional: eigene Domain mit HTTPS

Für den Betrieb unter einer öffentlichen Domain im Abschnitt `web:` die beiden
Werte setzen (Caddy holt das Zertifikat automatisch von Let's Encrypt):

```yaml
  web:
    environment:
      SITE_ADDRESS: "prozesse.meine-firma.de"   # statt ":80"
      API_UPSTREAM: "api:8000"
      ACME_EMAIL: "it@meine-firma.de"
```

Voraussetzung: Der DNS-Eintrag der Domain zeigt auf den Server und die Ports
**80** und **443** sind aus dem Internet erreichbar. Ohne eigene Domain bleibt
`SITE_ADDRESS: ":80"` (reines HTTP, Zugriff über die Server-IP).

Datei speichern und schließen.

---

## 6. Stack starten

Aus `C:\ProcWorks`:

```powershell
docker compose -f deploy/docker-compose.full.yml up --build -d
```

- `--build` baut die Images beim ersten Mal (dauert ein paar Minuten).
- `-d` startet im Hintergrund.

Der API-Container wendet beim Start automatisch die Datenbank-Migrationen an und
legt den Initial-Admin an.

Status der Container ansehen:

```powershell
docker compose -f deploy/docker-compose.full.yml ps
```

Alle Container sollten `running` (bzw. `healthy`) sein.

---

## 7. Funktion prüfen

1. API-Gesundheit prüfen:

   ```powershell
   curl http://localhost/api/health
   ```

   Erwartete Antwort: `{"status":"ok"}`.

2. Im Browser die Oberfläche öffnen:
   - lokal: `http://localhost`
   - bei eigener Domain: `https://prozesse.meine-firma.de`

Es erscheint direkt das **Login-Fenster**.

---

## 8. Erstmalig anmelden und Logins anlegen

1. Mit den Bootstrap-Daten anmelden:
   - **Login:** `admin`
   - **Passwort:** das in `PROCWORKS_ADMIN_PASSWORD` gesetzte Startpasswort
2. Das System verlangt sofort die Vergabe eines **eigenen Passworts**
   (mind. 8 Zeichen). Danach ist man direkt angemeldet.
3. Weitere Benutzer anlegen:
   - In die **Ressourcensicht** wechseln, einen **Agenten** anlegen
     (z. B. „Erika Musterfrau").
   - In der Agentenzeile auf **„Login"** klicken. Der Login-Name wird aus dem
     Namen vorgeschlagen (`erika.musterfrau`), Rollen auswählen, anlegen.
   - Das **Initialpasswort** wird **einmalig** angezeigt – notieren und der
     Person sicher mitteilen. Sie vergibt beim ersten Login ihr eigenes
     Passwort.

> **Wichtig:** Aus Sicherheitsgründen nach dem ersten erfolgreichen Admin-Login
> das Startpasswort nicht dauerhaft im Klartext belassen. Es kann nach der
> erfolgreichen Inbetriebnahme aus `PROCWORKS_ADMIN_PASSWORD` entfernt werden
> (der Bootstrap ist idempotent und legt den vorhandenen Admin nicht erneut an).

### 8.1 Optional: Beispieldaten zum Ausprobieren laden

Damit alle Funktionen sofort sichtbar werden, kann der Administrator fertige
Beispieldaten laden (eine Organisation, zwei Prozesse, drei laufende Instanzen):

1. Als **Administrator** anmelden.
2. In die Sicht **Monitoring** wechseln und ganz nach unten zum Bereich
   **„Wartung (Administrator)"** scrollen.
3. **„Beispieldaten laden"** klicken und bestätigen.

Anschließend stehen vier **Testbenutzer** zum Anmelden bereit (Passwort für alle:
`demo-procworks`):

| Login | Rolle |
| --- | --- |
| `mara.modell` | Modellierer |
| `erika.sander` | Bearbeiter (hat offene Aufgaben) |
| `tom.berger` | Bearbeiter / Leitung (genehmigt) |
| `vera.viewer` | Leser (nur Monitoring) |

Über **„Auf Null zurücksetzen"** im selben Bereich werden alle Daten **und** die
Testbenutzer wieder entfernt. **Vor dem Produktivbetrieb** unbedingt
zurücksetzen, damit keine Demo-Logins bestehen bleiben.

---

## 9. Betrieb

### Automatischer Start nach Server-Neustart

- Die Container sind mit `restart: unless-stopped` konfiguriert und starten
  automatisch wieder, **sobald Docker läuft**.
- Damit Docker selbst automatisch startet: In **Docker Desktop → Settings →
  General** die Option **„Start Docker Desktop when you log in"** aktivieren.
  Für unbeaufsichtigte Server empfiehlt sich zusätzlich ein automatischer Login
  bzw. der Betrieb von Docker Engine als Dienst in WSL2.

### Logs ansehen

```powershell
docker compose -f deploy/docker-compose.full.yml logs -f api
```

(`Strg + C` beendet die Anzeige, nicht die Container.)

### Stoppen / Starten

```powershell
# stoppen (Daten bleiben erhalten)
docker compose -f deploy/docker-compose.full.yml down

# wieder starten
docker compose -f deploy/docker-compose.full.yml up -d
```

### Update auf eine neue Version

```powershell
cd C:\ProcWorks
git pull
docker compose -f deploy/docker-compose.full.yml up --build -d
```

Migrationen werden beim Start automatisch angewendet.

### Datensicherung (automatisch)

Der mitgelieferte Stack enthält einen **`backup`-Dienst**, der **ohne weitere
Einrichtung** täglich (02:00 UTC) eine konsistente Sicherung der Datenbank
erstellt, sie mit Prüfsumme und Versionsangaben versieht und alte Sicherungen
nach dem Großvater-Vater-Sohn-Schema ausdünnt (14 tägliche, 8 wöchentliche,
6 monatliche). Die Sicherungen liegen im Docker-Volume `procworks_backups`.

Vorhandene Sicherungen ansehen:

```powershell
docker compose -f deploy/docker-compose.full.yml exec backup ls -lh /backups
```

Sofort eine Sicherung auslösen (der Zeitplan-Dienst führt sie binnen ~30 s aus):

```powershell
docker compose -f deploy/docker-compose.full.yml exec backup touch /backups/.run-now
```

**Wiederherstellen** (ersetzt die Datenbank – API vorher stilllegen):

```powershell
docker compose -f deploy/docker-compose.full.yml stop api
docker compose -f deploy/docker-compose.full.yml run --rm `
  --entrypoint /opt/backup/restore.sh backup --latest --yes
docker compose -f deploy/docker-compose.full.yml up -d api
```

> **Dringend empfohlen:** Das Volume `procworks_backups` zusätzlich **außer Haus**
> kopieren (eine Sicherung auf demselben Server überlebt keinen Totalausfall).
> Alle Details – Aufbewahrung feinjustieren, Verschlüsselung, Off-Site-Kopie,
> Selbsttest, Kubernetes – stehen im
> [Betriebs-Backup-Leitfaden](./Betriebs-Backup-Leitfaden.md).

---

## 10. Netzwerk / Firewall

- Lokaler Zugriff funktioniert ohne Anpassung.
- Für Zugriff aus dem Netzwerk in der **Windows Defender Firewall** eingehende
  Regeln für die benötigten Ports freigeben:
  - **80/TCP** (HTTP) und – bei HTTPS – **443/TCP**.

```powershell
New-NetFirewallRule -DisplayName "ProcWorks HTTP"  -Direction Inbound -Protocol TCP -LocalPort 80  -Action Allow
New-NetFirewallRule -DisplayName "ProcWorks HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

---

## 11. Fehlerbehebung

| Symptom | Ursache / Lösung |
| --- | --- |
| `docker` wird nicht gefunden | Neue PowerShell öffnen; Docker Desktop gestartet und „Engine running"? |
| Container startet nicht, Logs zeigen DB-Fehler | DB-Passwort in `POSTGRES_PASSWORD` und `DATABASE_URL` müssen identisch sein. |
| `wsl --install` schlägt fehl | Virtualisierung im BIOS/UEFI bzw. in der VM aktivieren; danach neu starten. |
| Login-Fenster erscheint nicht | `PROCWORKS_AUTH: "password"` im `api`-Block gesetzt? Container nach Änderung neu bauen (`up --build -d`). |
| Anmeldung als `admin` schlägt fehl | `PROCWORKS_ADMIN_LOGIN`/`PROCWORKS_ADMIN_PASSWORD` waren beim **ersten** Start nicht gesetzt. Setzen und Stack neu starten – der Admin wird dann angelegt. |
| HTTPS-Zertifikat wird nicht ausgestellt | Domain muss per DNS auf den Server zeigen, Ports 80+443 aus dem Internet erreichbar; gültige `ACME_EMAIL` setzen. |

---

## Weiterführend

- Passwort-Login, Rollen und Betriebshinweise im Detail:
  [`core/README.md`](../core/README.md) (Abschnitt „Passwort-Login ohne externen
  IdP"), [`SECURITY.md`](../SECURITY.md).
</content>
