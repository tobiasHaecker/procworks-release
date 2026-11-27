<!-- SPDX-License-Identifier: BUSL-1.1 -->
# PITR-Leitfaden – Point-in-Time-Recovery (fortgeschritten, optional)

Dieser Leitfaden beschreibt **Stufe 2** der Datensicherung: physische
Basissicherung plus kontinuierliche **WAL-Archivierung**, mit der sich die
Datenbank auf einen **beliebigen Zeitpunkt** zurücksetzen lässt (Point-in-Time-
Recovery, RPO ≈ 0). Er ist **optional** und **nicht** Teil des mitgelieferten
Standard-Stacks – die tägliche logische Sicherung (Stufe 1, siehe
[Betriebs-Backup-Leitfaden](./Betriebs-Backup-Leitfaden.md)) läuft davon
unabhängig weiter und genügt den meisten Betrieben.

> ⚠️ **Für erfahrene Betreiber.** PITR verändert die PostgreSQL-Konfiguration,
> braucht zusätzlichen Speicher und **muss geübt werden**. Richten Sie es in
> einer Testumgebung ein und spielen Sie mindestens einmal eine Wiederherstellung
> durch, bevor Sie sich darauf verlassen. ProcWorks wird ohne jede Gewährleistung
> bereitgestellt ([DISCLAIMER.md](../DISCLAIMER.md)).

---

## 1. Wann PITR – und wann nicht

| | Stufe 1 (Standard) | Stufe 2 (PITR) |
|---|---|---|
| Verfahren | tägliches `pg_dump` | Basissicherung + WAL-Archiv |
| Max. Datenverlust (RPO) | = Sicherungsintervall (Std./Tag) | ≈ 0 (bzw. `archive_timeout`) |
| Aufwand | wartungsarm, out-of-the-box | Konfiguration, WAL-Ablage, Übung |
| Versionsbindung | portabel (auch Major-Wechsel) | an die **PostgreSQL-Major-Version** gebunden |
| Empfehlung | für die meisten Betriebe ausreichend | nur bei strengen RPO-Zielen |

- **Managed-Datenbank (RDS/Azure DB):** PITR bitte über den **nativen**
  Mechanismus des Anbieters aktivieren – nicht über diese Anleitung. Diese
  richtet sich an die **selbst betriebene**, gebündelte PostgreSQL.
- **Beides ist kein Ersatz füreinander:** WAL-Archivierung sichert die
  fortlaufenden Änderungen; die logischen `pg_dump`-Sicherungen bleiben die
  einfache, versionsunabhängige Rückfallebene. **Behalten Sie beide.**

---

## 2. Wie PITR funktioniert (in einem Absatz)

PostgreSQL schreibt jede Änderung zuerst ins **Write-Ahead-Log (WAL)**. Wenn man
(a) einmalig eine **Basissicherung** des Datenverzeichnisses zieht und danach
(b) **jedes** volle WAL-Segment fortlaufend in ein Archiv kopiert, kann man im
Ernstfall die Basissicherung zurückspielen und die WAL-Segmente bis zu einem
**gewünschten Zeitpunkt** erneut abspielen lassen. So landet man exakt auf dem
Stand „kurz vor dem Missgeschick" statt nur auf der letzten Nachtsicherung.

---

## 3. Voraussetzungen

- Ein **getrennter, dauerhafter Speicher** für das WAL-Archiv (eigenes Volume,
  idealerweise auf anderer Platte/anderem Host als die DB-Daten).
- Platz für **Basissicherungen** (jeweils etwa so groß wie die Datenbank).
- **Off-Site-Kopie** von WAL-Archiv **und** Basissicherungen – lokal allein
  nützt bei einem Totalausfall nichts.
- Ein **Neustart** der Datenbank (Aktivieren von `archive_mode` verlangt ihn).

---

## 4. WAL-Archivierung aktivieren (gebündelte PostgreSQL)

Ergänzen Sie den `postgres`-Dienst in `deploy/docker-compose.full.yml` um ein
Archiv-Volume und die Archiv-Einstellungen. Am einfachsten über `command:`
(überschreibt den Start und übergibt die Parameter):

```yaml
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: process
      POSTGRES_PASSWORD: process
      POSTGRES_DB: procworks
    command:
      - postgres
      - -c
      - wal_level=replica
      - -c
      - archive_mode=on
      # Archiviert ein WAL-Segment nur, wenn es im Archiv noch nicht existiert
      # (idempotent) und meldet Erfolg per Exit-Code 0 -- Bedingung für PITR:
      - -c
      - archive_command=test ! -f /wal-archive/%f && cp %p /wal-archive/%f
      # Erzwingt spätestens alle 5 Minuten einen WAL-Wechsel -> begrenzt den
      # maximalen Datenverlust auch bei wenig Verkehr auf ~5 min:
      - -c
      - archive_timeout=300
    volumes:
      - procworks_pgdata:/var/lib/postgresql/data
      - procworks_wal_archive:/wal-archive        # << neu
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U process -d procworks"]
      interval: 5s
      timeout: 5s
      retries: 5
```

Und das neue Volume unten ergänzen:

```yaml
volumes:
  procworks_pgdata:
  procworks_wal_archive:          # << neu: dauerhaftes WAL-Archiv
  procworks_backups:
  procworks_backup_control:
  caddy_data:
  caddy_config:
```

Neu starten und prüfen, dass die Archivierung greift:

```bash
docker compose -f deploy/docker-compose.full.yml up -d postgres
# WAL-Wechsel erzwingen und Archiv ansehen:
docker compose -f deploy/docker-compose.full.yml exec -T postgres \
  psql -U process -d procworks -c "SELECT pg_switch_wal();"
docker compose -f deploy/docker-compose.full.yml exec postgres ls -1 /wal-archive
```

> Erscheinen im Archiv Dateien (16-stellige Hex-Namen), läuft die Archivierung.
> Bleibt es leer, stimmt das `archive_command` oder die Schreibrechte auf
> `/wal-archive` nicht. `SELECT * FROM pg_stat_archiver;` zeigt Fehlerzähler.

---

## 5. Basissicherung ziehen

Die Basissicherung ist der Ausgangspunkt, ab dem WAL abgespielt wird. Ziehen Sie
sie **nach** dem Aktivieren der Archivierung und danach in regelmäßigem Abstand
(z. B. wöchentlich), damit die Wiederherstellung nicht das gesamte WAL seit
Tag 1 abspielen muss:

```bash
docker compose -f deploy/docker-compose.full.yml exec -T postgres \
  pg_basebackup -U process -D /wal-archive/base-$(date -u +%Y%m%dT%H%M) \
  -Ft -z -Xs -P
```

- `-Ft -z` → komprimiertes Tar. `-Xs` → nötiges WAL wird mitgestreamt, die
  Basissicherung ist für sich konsistent.
- Legen Sie Basissicherung **und** WAL-Archiv **off-site** ab (z. B. per
  `BACKUP_SYNC_CMD` bzw. einem eigenen Kopierschritt auf `/wal-archive`).

> **Aufräumen:** Alte WAL-Segmente, die **vor** der ältesten noch benötigten
> Basissicherung liegen, können gelöscht werden (sonst wächst das Archiv
> unbegrenzt). Werkzeuge wie `pg_archivecleanup` oder ein ausgereiftes
> Backup-Tool (**pgBackRest**, **Barman**) automatisieren Basissicherung,
> Archivierung und Aufräumen – für den Dauerbetrieb empfohlen.

---

## 6. Wiederherstellung auf einen Zeitpunkt

> Wie jeder Restore: **erst die API stilllegen** (`docker compose stop api web`),
> damit niemand schreibt. Üben Sie diesen Ablauf vorab.

1. **Datenbank stoppen** und das (defekte) Datenverzeichnis beiseitelegen:
   ```bash
   docker compose -f deploy/docker-compose.full.yml stop postgres
   ```
2. **Basissicherung** in ein leeres Datenverzeichnis auspacken (die jüngste, die
   **vor** dem Zielzeitpunkt liegt).
3. **Recovery konfigurieren** im ausgepackten Datenverzeichnis:
   - `restore_command` setzen (holt WAL-Segmente aus dem Archiv):
     ```
     restore_command = 'cp /wal-archive/%f %p'
     ```
   - Zielzeitpunkt wählen (weglassen = bis zum Ende des Archivs abspielen):
     ```
     recovery_target_time = '2026-07-08 09:41:00+00'
     recovery_target_action = 'promote'
     ```
     Diese Zeilen kommen in `postgresql.auto.conf`.
   - Eine **leere** Datei `recovery.signal` im Datenverzeichnis anlegen – sie
     schaltet PostgreSQL beim Start in den Wiederherstellungsmodus (ab PG 12;
     die früher genutzte `recovery.conf` gibt es nicht mehr).
4. **PostgreSQL starten.** Es spielt das WAL bis zum Ziel ab, promotet und
   entfernt `recovery.signal` selbst. Fortschritt im Log verfolgen:
   ```bash
   docker compose -f deploy/docker-compose.full.yml up -d postgres
   docker compose -f deploy/docker-compose.full.yml logs -f postgres
   ```
   Auf `recovery stopping before ... / archive recovery complete` achten.
5. **API/Web wieder starten** – der API-Start wendet nötige Migrationen an:
   ```bash
   docker compose -f deploy/docker-compose.full.yml up -d api web
   ```

> Prüfen Sie danach die Anwendung (Login, ein Prozess, eine laufende Instanz).
> Stimmt der Stand nicht, wählen Sie einen anderen `recovery_target_time` und
> beginnen bei Schritt 2 erneut aus der Basissicherung.

---

## 7. Betrieb & Überwachung

- **`pg_stat_archiver`** überwachen: `last_failed_wal`/`last_failed_time` müssen
  leer bleiben; `archived_count` sollte wachsen.
- **Speicher im Blick behalten:** WAL-Archiv und Basissicherungen wachsen; ohne
  Aufräumen läuft die Platte voll (und Archivierungsfehler können Schreibvorgänge
  in der DB blockieren, wenn `archive_mode=on` und das Archiv nicht schreibbar
  ist).
- **Major-Upgrade:** Physische Sicherungen sind an die PostgreSQL-Major-Version
  gebunden. Nach einem Major-Upgrade eine **neue** Basissicherung ziehen; alte
  WAL/Basissicherungen sind dann nicht mehr einspielbar.
- **Kubernetes:** Im Helm-Chart wird die DB extern bereitgestellt; PITR
  konfiguriert man dort am Datenbank-Cluster selbst (oder nutzt pgBackRest/Barman
  bzw. den Managed-Dienst) – nicht über das Chart.

---

## 8. Kurz-Checkliste

- [ ] `archive_mode=on` + `archive_command` gesetzt, DB neu gestartet
- [ ] Archiv füllt sich (`ls /wal-archive`, `pg_stat_archiver`)
- [ ] Regelmäßige **Basissicherung** eingerichtet
- [ ] WAL-Archiv **und** Basissicherungen **off-site**
- [ ] Aufräumen alter WAL/Basissicherungen geregelt
- [ ] Eine **PITR-Wiederherstellung** in einer Testumgebung geübt
- [ ] Tägliche logische Sicherung (Stufe 1) **zusätzlich** weiterhin aktiv
