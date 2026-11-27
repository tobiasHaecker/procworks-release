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
docker compose -f deploy/docker-compose.full.yml up -d
```

- Beim ersten Start lädt Docker die fertigen Images von `ghcr.io` (die Version
  dieses Checkouts, vom Release geprüft auf Sicherheitslücken). Gebaut wird
  nichts. Bereitgestellt wird immer nur die aktuelle Version; ältere Versionen
  sind nicht mehr verfügbar.
- `-d` startet im Hintergrund.
- Nur wer den Quelltext selbst ändert, baut mit `up --build -d` aus dem Checkout.

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

   Ob die API auch ihre Datenbank erreicht, zeigt
   `curl http://localhost/api/ready` (`{"status":"ready"}`; bei einem
   Datenbankausfall Status 503).

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
   - Das **Initialpasswort** wird **einmalig** angezeigt (mit Knopf
     „Kopieren“) – notieren und der Person sicher mitteilen. Sie vergibt beim
     ersten Login ihr eigenes Passwort.
   - Hat eine Person schon einen Login, steht in ihrer Zeile „Login: …“ statt
     des Knopfs.
4. Bestehende Logins verwalten: **Administration → Benutzer** listet alle
   Logins mit Rollen, zugeordneter Person und dem Hinweis „muss Passwort
   ändern“. Dort lässt sich ein **Passwort zurücksetzen** (das neue
   Initialpasswort erscheint einmalig, bestehende Anmeldungen enden) oder ein
   Login **löschen** (die Person im Organisationsmodell bleibt erhalten). Den
   eigenen Login und den letzten Administrator-Login kann man nicht löschen.

> **Wichtig:** Aus Sicherheitsgründen nach dem ersten erfolgreichen Admin-Login
> das Startpasswort nicht dauerhaft im Klartext belassen. Es kann nach der
> erfolgreichen Inbetriebnahme aus `PROCWORKS_ADMIN_PASSWORD` entfernt werden
> (der Bootstrap ist idempotent und legt den vorhandenen Admin nicht erneut an).

### 8.1 Optional: Beispieldaten zum Ausprobieren laden

Damit alle Funktionen sofort sichtbar werden, kann der Administrator fertige
Beispieldaten laden (eine Organisation, zwei Prozesse, drei Vorgänge – zwei
laufend, einer abgeschlossen):

1. Als **Administrator** anmelden.
2. In die Sicht **Administration** wechseln und zum Bereich **„Wartung"**
   scrollen.
3. **„Beispieldaten laden"** klicken und bestätigen.

Im selben Bereich liegt **„Order-to-Cash-Beispiel laden"**: ein umfangreicherer
Datensatz mit einem vollständigen Geschäftsprozess (Angebot → Auftrag →
Lieferung → Rechnung → Zahlungseingang → Mahnwesen), sechs Prozessen, einer
eigenen Beispielfirma und neun vorbereiteten Vorgängen. Beide Datensätze lassen
sich auch nacheinander laden; jedes Laden setzt vorher alle Daten zurück.

Anschließend stehen sechs **Testbenutzer** zum Anmelden bereit. Ihr gemeinsames
Passwort vergibt ProcWorks bei jedem Laden neu und zufällig; die Oberfläche zeigt
es nach dem Laden **einmal** an – notieren Sie es gleich:

| Login | Rolle |
| --- | --- |
| `mara.modell` | Modellierer |
| `erika.sander` | Bearbeiter (hat offene Aufgaben) |
| `tom.berger` | Bearbeiter / Leitung (genehmigt) |
| `paul.klein` | Bearbeiter / Einkauf (Beschaffungsantrag) |
| `sabine.chef` | Bearbeiter / Geschäftsleitung (genehmigt Toms Anträge) |
| `vera.viewer` | Leser (nur Monitoring) |

Über **„Auf Null zurücksetzen"** im selben Bereich werden alle Daten **und** die
Testbenutzer wieder entfernt. **Vor dem Produktivbetrieb** unbedingt
zurücksetzen, damit keine Demo-Logins bestehen bleiben – oder die Testbenutzer
unter **Administration → Benutzer** einzeln löschen bzw. ihnen neue Passwörter
geben.

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

### E-Mail und Anbindungen einrichten

Einstellungen wie der Mailserver oder Zugänge für andere Programme gehören in
eine Datei `C:\ProcWorks\deploy\.env` – Docker liest sie beim Start
automatisch, die Compose-Datei bleibt unverändert. Anlegen im Editor:

```powershell
notepad C:\ProcWorks\deploy\.env
```

Inhalt für E-Mail-Benachrichtigungen (Werte Ihres Mailservers):

```ini
PROCWORKS_SMTP_HOST=smtp.ihre-firma.de
PROCWORKS_SMTP_PORT=587
PROCWORKS_SMTP_USER=procworks@ihre-firma.de
PROCWORKS_SMTP_PASSWORD=geheim
PROCWORKS_MAIL_FROM=procworks@ihre-firma.de
```

Speichern (Dateityp „Alle Dateien“, damit keine Endung `.txt` angehängt wird)
und den Stack neu starten:

```powershell
docker compose -f deploy/docker-compose.full.yml up -d
```

In der Verwaltung zeigt der Bereich **„E-Mail-Ausgang"**, ob Mails versendet
werden; ohne Mailserver erscheinen sie als „verworfen – kein Mailserver“ (und
werden auch nach späterer Einrichtung nicht nachgeschickt). Zugänge für Worker und andere Programme (Maschinen-Tokens) werden in
derselben Datei eingetragen, siehe README, Abschnitt „Einstellungen“.

Verlangt Ihr Mailserver in der Begrüßung einen Rechnernamen oder prüft er
die Begrüßung (HELO-Prüfung), ergänzen Sie
`PROCWORKS_SMTP_EHLO_NAME=procworks.ihre-firma.de` (vollständiger DNS-Name
des Servers) oder ein Adressliteral in eckigen Klammern wie `[192.0.2.10]`.
Ohne diese Zeile meldet sich ProcWorks mit der IP-Adresse seiner Verbindung –
unter Docker ist das die Adresse des Containers im Docker-Netz (etwa
`[172.18.0.3]`), nicht die Adresse Ihres Servers. Ein ungültiger Wert
(z. B. eine IP-Adresse ohne Klammern) verhindert den Start mit einer Meldung,
die die Variable und den Grund nennt.

Jeder Schritt der Verbindung zum Mailserver darf höchstens 10 Sekunden
dauern, eine ganze Sitzung höchstens 2 Minuten. Für die Bestätigung nach dem
eigentlichen Mailinhalt wartet ProcWorks länger, bis zu 60 Sekunden – viele
Mailserver prüfen den Inhalt (Viren, Spam), bevor sie bestätigen. Kommt die
Bestätigung noch später oder dauert die ganze Sitzung länger als 2 Minuten,
gilt der Versuch als fehlgeschlagen, und ProcWorks schickt die Mail nach einer
Pause noch einmal – mit derselben `Message-ID`, sodass der Empfänger sie als
Doppel erkennen kann.
**Anzeige während einer Zustellung:** Bevor ProcWorks eine Mail sendet,
reserviert es den Eintrag für 5 Minuten (damit kein zweiter Vorgang dieselbe
Mail gleichzeitig sendet). In dieser Zeit zeigt der E-Mail-Ausgang ihn als
„ausstehend“ mit einem Versuch mehr, und „Fällige jetzt senden“ lässt ihn
unberührt – er ist ja nicht fällig. Ist die Mail versendet, steht er auf
„versendet“. Bricht der Server mitten im Versand ab (Neustart), bleibt der
Eintrag bis zum Ende der 5 Minuten so stehen und wird dann erneut gesendet
(mit derselben `Message-ID`). Ein Eintrag, der einige Minuten „ausstehend“
zeigt, ist also kein Fehler; erst „fehlgeschlagen“ nach allen Versuchen
verlangt eine Prüfung der Mailserver-Einstellungen.

### Datenbankverbindungen

Alle Teile der API teilen sich einen Verbindungsvorrat zur Datenbank. Ein
API-Prozess öffnet höchstens `PROCWORKS_DB_POOL_SIZE` +
`PROCWORKS_DB_MAX_OVERFLOW` Verbindungen (Standard 10 + 10 = 20); sind alle
belegt, wartet eine Anfrage bis zu `PROCWORKS_DB_POOL_TIMEOUT` Sekunden
(Standard 30) und meldet dann „vorübergehend nicht verfügbar“ (503). Für den
mitgelieferten Stack mit einem API-Prozess passt der Standard.

Ein einzelner Datenbankbefehl darf höchstens `PROCWORKS_DB_STATEMENT_TIMEOUT`
Sekunden dauern (Standard 30, `0` = ohne Grenze, höchstens 3600); danach bricht
die Datenbank ihn ab und die Anfrage meldet ebenfalls 503, ohne etwas zu
ändern. So hängt keine Anfrage, wenn etwa eine Wartungssitzung eine Tabelle
gesperrt hält. Die Sicherung und die Datenbank-Updates beim Start sind davon
nicht betroffen. Reagiert der Datenbankserver gar nicht mehr (eingefroren,
Netzwerk unterbrochen), meldet `/api/ready` das nach spätestens 5 Sekunden mit
503; andere Anfragen gibt die API nach `PROCWORKS_DB_STATEMENT_TIMEOUT` plus
höchstens 10 Sekunden mit 503 auf (bei `0` warten sie, bis die Datenbank wieder
antwortet).

Wer mehrere API-Prozesse betreibt, muss deren **Uhren synchron halten** (NTP
bzw. Windows-Zeitdienst auf allen Rechnern; Container übernehmen die Uhr ihres
Rechners). Zustellungen werden für eine feste Zeit reserviert, und jeder
Prozess vergleicht das Ende der Reservierung mit seiner eigenen Uhr: Geht
eine Uhr um mehr als eine Minute vor, kann ein Prozess eine noch laufende
Zustellung ein zweites Mal ausführen.

Wer mehrere API-Prozesse gegen dieselbe Datenbank betreibt, rechnet:
**Anzahl Prozesse × (Pool + Überlauf) < `max_connections` von PostgreSQL**
(Standard 100), mit Reserve für Sicherung und Verwaltungswerkzeuge – zwei
Prozesse mit dem Standard brauchen also bis zu 40 Verbindungen. Die Werte
gehören wie oben in `deploy/.env`; erlaubt sind einfache Zahlen (Pool 1–100,
Überlauf 0–100, Wartezeit 1–300 Sekunden). Ein ungültiger Wert (etwa `0`,
eine negative Zahl, `1_0` oder `1e-9`) verhindert den Start der API, die
Meldung steht im Log.

### Eskalationen

Ob eine modellierte Eskalationsstufe fällig ist, prüft die API im Hintergrund
in einem festen Takt, standardmäßig **einmal je Minute**; eine Stufe feuert
also bis zu einer Minute nach ihrem Fälligkeitszeitpunkt (Arbeitslisten und
Monitoring zeigen sie danach). Der Takt ist in `deploy/.env` einstellbar:

```ini
PROCWORKS_ESCALATION_SWEEP_SECONDS=60
```

`0` schaltet die Prüfung in diesem API-Prozess ab (Eskalationen feuern dann
nur noch über den Administrator-Aufruf `POST /admin/escalations/sweep`);
erlaubt sind sonst 1 bis 86 400 Sekunden (einfache Zahl, etwa `30` oder
`7.5`). Ein ungültiger Wert – auch ein Takt unter einer Sekunde – verhindert
den Start der API,
die Meldung steht im Log. Mehrere API-Prozesse dürfen die Prüfung alle
ausführen – jede Stufe feuert trotzdem genau einmal.

### Update auf eine neue Version

Vorher eine Sicherung anlegen (Abschnitt „Datensicherung“). Dann:

```powershell
cd C:\ProcWorks
git fetch origin
git reset --hard origin/main
docker compose -f deploy/docker-compose.full.yml pull
docker compose -f deploy/docker-compose.full.yml up -d
```

`git fetch` und `git reset --hard` holen den Stand der neuen Version, `pull`
lädt deren Images, `up -d` tauscht die Container aus. Migrationen werden beim
Start automatisch angewendet. Ihre Einstellungen in `deploy/.env` bleiben
erhalten, weil diese Datei nicht zum Repository gehört. **Steht dort oder in
der Umgebung ein `PROCWORKS_VERSION`, bitte entfernen** – sonst bleibt die
Installation auf der alten Version, deren Images nicht mehr bereitgestellt
werden. Eigene Änderungen an
Dateien des Repositories (etwa an der Compose-Datei) überschreibt der Befehl
dagegen – solche Anpassungen gehören in `deploy/.env`.

Nach dem Update lohnt ein Blick ins Log der API
(`docker compose -f deploy/docker-compose.full.yml logs api`): Meldet es
„person(s) have more than one login“, hat eine Person aus der Zeit vor dieser
Regel mehrere Logins. Sie bleiben erhalten; löschen Sie die überzähligen in
der Benutzerverwaltung – für diese Person lässt sich kein weiterer Login
anlegen.

> **Warum nicht `git pull`?** Das Repository enthält immer nur die aktuelle
> Version, ohne die Historie älterer Versionen. `git pull` meldet deshalb
> einen Fehler („refusing to merge unrelated histories“ oder „divergent
> branches“); der Weg oben funktioniert von jeder älteren Version aus.
> Ältere Versionen enthalten Fehler, die in der aktuellen Version behoben
> sind – welche, steht in der
> [Versionshistorie](Versionshistorie.md).

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

**Wiederherstellen** (ersetzt die Datenbank – API vorher stilllegen). Auf einer
bestehenden Installation ist die Datenbank nie leer, deshalb gehört `--force`
dazu; es hebt aber auch die Prüfung auf offene Verbindungen auf – die API muss
also wirklich gestoppt sein (`ps api` zeigt sie nicht als laufend):

```powershell
docker compose -f deploy/docker-compose.full.yml stop api
docker compose -f deploy/docker-compose.full.yml ps api
docker compose -f deploy/docker-compose.full.yml run --rm `
  --entrypoint /opt/backup/restore.sh backup --latest --yes --force
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

### Anmeldesperre und Docker Desktop

Nach vielen fehlgeschlagenen Anmeldungen sperrt ProcWorks kurz – je Login und
je Absenderadresse (zuerst 30 Sekunden, bei Wiederholung länger, höchstens
15 Minuten). **Unter Docker Desktop sieht ProcWorks alle Zugriffe unter
derselben Adresse** (der Weiterleitung von Docker Desktop): Die
Absenderadresse eines Rechners kommt dort gar nicht erst im Container an. Dann
treffen rund 20 Fehlversuche – auch mit verschiedenen, erfundenen Logins –
kurzzeitig alle Nutzer. Die Sperre je Login bleibt davon unberührt. Abhilfe:

- **Empfohlen für den Produktivbetrieb:** ProcWorks auf einem Linux-Server mit
  Docker Engine betreiben; dort bleibt die Absenderadresse in der Regel
  erhalten.
- Oder einen eigenen, vorgeschalteten Proxy einsetzen (Lastverteiler oder
  Reverse-Proxy auf dem Host), der `X-Forwarded-For` setzt, und dem
  mitgelieferten Webserver (Caddy) sagen, dass er diesem Proxy glauben darf –
  in `deploy/.env`:

  ```
  CADDY_TRUSTED_PROXIES=10.0.0.5
  ```

  Mehrere Adressen oder Netze durch Leerzeichen trennen
  (`10.0.0.5 10.1.0.0/16`), `private_ranges` steht für alle internen Netze.
  Leer (Standard) glaubt Caddy niemandem und gibt die Adresse weiter, die es
  selbst sieht – wie bisher. Nur Adressen eintragen, über die ausschließlich
  Ihr Proxy kommt: Wer aus einem eingetragenen Netz direkt zugreift, könnte
  sonst eine beliebige Absenderadresse angeben. Unter Docker Desktop ohne
  eigenen Proxy hilft die Einstellung nicht – die Adresse des Rechners kommt
  dort gar nicht erst an.

**`PROCWORKS_TRUSTED_PROXIES`** (im `api`-Block oder in `deploy/.env`) legt
fest, wessen Angabe `X-Forwarded-For` ProcWorks glaubt: einzelne Adressen oder
Netze (`172.16.0.0/12`), `private` (Standard: interne Netze – im
mitgelieferten Stack erreicht nur der Webserver-Container die API) oder
`none`. Ist der API-Port (8000) in Ihrem Netz direkt erreichbar, tragen Sie
hier nur die Adresse Ihres Proxys ein. Diese Einstellung entscheidet allein:
Der Anwendungsserver im Container wertet `X-Forwarded-For` nicht selbst aus
(`UVICORN_PROXY_HEADERS=false` im Container voreingestellt). Ein ungültiger Wert verhindert den
Start der API (Meldung im Log).

---

## 11. Fehlerbehebung

| Symptom | Ursache / Lösung |
| --- | --- |
| `docker` wird nicht gefunden | Neue PowerShell öffnen; Docker Desktop gestartet und „Engine running"? |
| Container startet nicht, Logs zeigen DB-Fehler | DB-Passwort in `POSTGRES_PASSWORD` und `DATABASE_URL` müssen identisch sein. |
| `wsl --install` schlägt fehl | Virtualisierung im BIOS/UEFI bzw. in der VM aktivieren; danach neu starten. |
| Login-Fenster erscheint nicht | `PROCWORKS_AUTH: "password"` im `api`-Block gesetzt? Container nach der Änderung neu erzeugen (`up -d`). |
| Anmeldung als `admin` schlägt fehl | `PROCWORKS_ADMIN_LOGIN`/`PROCWORKS_ADMIN_PASSWORD` waren beim **ersten** Start nicht gesetzt. Setzen und Stack neu starten – der Admin wird dann angelegt. |
| HTTPS-Zertifikat wird nicht ausgestellt | Domain muss per DNS auf den Server zeigen, Ports 80+443 aus dem Internet erreichbar; gültige `ACME_EMAIL` setzen. |

---

## Weiterführend

- Passwort-Login, Rollen und Betriebshinweise im Detail:
  [`core/README.md`](../core/README.md) (Abschnitt „Passwort-Login ohne externen
  IdP"), [`SECURITY.md`](../SECURITY.md).
</content>
