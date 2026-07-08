<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Betriebs-Backup-Leitfaden – Sichern & Wiederherstellen

Dieser Leitfaden beschreibt, wie ProcWorks im laufenden Betrieb **gesichert** und
im Ernstfall **wiederhergestellt** wird. Er richtet sich an die Person, die den
Server betreut – Vorwissen zu Datenbanken ist **nicht** nötig.

> ⚠️ **Haftungsausschluss.** ProcWorks wird „wie besehen", ohne jede
> Gewährleistung und Haftung bereitgestellt. Betrieb und Datensicherung erfolgen
> ausschließlich auf eigenes Risiko. Prüfen Sie Ihre Sicherungen regelmäßig durch
> eine echte Wiederherstellung. Vollständiger Text: [DISCLAIMER.md](../DISCLAIMER.md).

---

## Das Wichtigste in Kürze

- **Es ist schon eingerichtet.** Der mitgelieferte Stack sichert **automatisch
  täglich** – Sie müssen nichts starten.
- **Alles in einer Datenbank.** Sämtliche dauerhaften Daten (Prozesse, laufende
  Instanzen, Verlauf/Audit, Organisation, Logins) liegen in **einer** PostgreSQL-
  Datenbank. Eine Sicherung dieser einen Datenbank ist damit **immer in sich
  stimmig** – es gibt keinen „halb gesicherten" Zustand.
- **Zwei Dinge müssen Sie selbst tun:**
  1. Die Sicherungen **außer Haus kopieren** (eine Kopie auf demselben Server
     überlebt keinen Totalausfall).
  2. Die **Zugangsdaten/Konfiguration** getrennt sichern (sie liegen bewusst
     **nicht** in der Datenbank – siehe [Abschnitt 8](#8-was-nicht-in-der-sicherung-steht)).
- **Restore prüfen, nicht hoffen.** Spielen Sie die Sicherung mindestens einmal
  testweise zurück ([Abschnitt 6](#6-selbsttest-einer-sicherung)).
- **Brauchen Sie RPO ≈ 0** (Wiederherstellung auf die Minute genau statt nur auf
  die letzte Nachtsicherung)? Das ist ein fortgeschrittener Zusatz – siehe den
  optionalen [PITR-Leitfaden](./PITR-Leitfaden.md).

---

## 1. Was automatisch passiert

Der Container-Verbund enthält einen Dienst namens **`backup`**. Er:

- erstellt **täglich um 02:00 Uhr (UTC)** eine **konsistente** Sicherung der
  gesamten Datenbank,
- legt zu jeder Sicherung ein **Manifest** ab (Zeitpunkt, Version,
  Prüfsumme `sha256`) zur späteren Überprüfung,
- **dünnt alte Sicherungen aus** nach dem Großvater-Vater-Sohn-Schema:
  standardmäßig **14** tägliche, **8** wöchentliche und **6** monatliche behalten,
- läuft in einem **eigenen** Container: Schlägt eine Sicherung fehl, stört das
  **weder** die Anwendung **noch** die Datenbank.

Die Sicherungen liegen im Docker-Volume **`procworks_backups`** (getrennt vom
Datenbank-Volume, damit ein Volume-Verlust nicht beide trifft). Dateien heißen
`procworks-JJJJ-MM-TTThh-mm.dump` mit passendem `…​.manifest.json`.

---

## 2. Sicherungen ansehen

**In der Oberfläche (am einfachsten):** Als **Administrator** anmelden, in die
Sicht **Monitoring** wechseln und zum Bereich **„Sicherungen"** scrollen. Dort
stehen die vorhandenen Sicherungen (Zeitpunkt, Version, Größe, verschlüsselt
ja/nein), der Zeitpunkt der letzten erfolgreichen Sicherung – und eine
Schaltfläche **„Jetzt sichern"** ([Abschnitt 3](#3-sofort-eine-sicherung-auslösen)).

**Auf der Kommandozeile:**

```bash
docker compose -f deploy/docker-compose.full.yml exec backup ls -lh /backups
```

Zeitpunkt der letzten **erfolgreichen** Sicherung:

```bash
docker compose -f deploy/docker-compose.full.yml exec backup cat /backups/.last-success
```

> Faustregel für die Überwachung: Ist `.last-success` **älter als ~26 Stunden**,
> lohnt ein Blick ins Log (`docker compose … logs backup`).

---

## 3. Sofort eine Sicherung auslösen

Ohne auf 02:00 Uhr zu warten – der Zeitplan-Dienst führt den Lauf binnen ~30 s aus.
**In der Oberfläche:** unter **Monitoring → „Sicherungen"** die Schaltfläche
**„Jetzt sichern"**. **Auf der Kommandozeile:**

```bash
docker compose -f deploy/docker-compose.full.yml exec backup touch /backups/.run-now
```

---

## 4. Einstellungen anpassen (optional)

Alle Stellschrauben sind Umgebungsvariablen im `backup`-Dienst der Datei
`deploy/docker-compose.full.yml`:

| Variable | Bedeutung | Standard |
|----------|-----------|----------|
| `BACKUP_CRON` | Zeitplan (`Minute Stunde * * *`) | `0 2 * * *` |
| `BACKUP_KEEP_DAILY` | Anzahl täglicher Sicherungen | `14` |
| `BACKUP_KEEP_WEEKLY` | Anzahl wöchentlicher Sicherungen | `8` |
| `BACKUP_KEEP_MONTHLY` | Anzahl monatlicher Sicherungen | `6` |
| `BACKUP_PASSPHRASE` | gesetzt ⇒ Sicherungen **verschlüsseln** (siehe [7.2](#72-verschlüsselung)) | leer |
| `BACKUP_SYNC_CMD` | Befehl für die **Off-Site-Kopie** (siehe [7.1](#71-off-site-kopie)) | leer |
| `BACKUP_ALERT_WEBHOOK` | URL, an die Erfolg/Fehlschlag gemeldet wird | leer |

Nach einer Änderung den Dienst neu starten:

```bash
docker compose -f deploy/docker-compose.full.yml up -d backup
```

> `BACKUP_CRON` unterstützt bewusst nur „Minute Stunde" (täglich/stündlich) – die
> übrigen Felder müssen `*` sein. Beispiel „alle zwei Stunden zur vollen Stunde":
> das lässt sich **nicht** ausdrücken; wählen Sie stattdessen eine feste Uhrzeit
> oder stündlich (`0 * * * *`).

---

## 5. Wiederherstellen (Restore)

Ein Restore **ersetzt die gesamte Datenbank** durch den Stand einer Sicherung.
Er läuft in **einer** Transaktion (alles-oder-nichts): Klappt etwas nicht, bleibt
die vorhandene Datenbank **unverändert**. Damit dabei niemand gleichzeitig
schreibt, wird die API vorher gestoppt – der Restore **verweigert** sich sogar,
solange die API noch verbunden ist.

```bash
# 1. Anwendung stilllegen (keine Schreiber):
docker compose -f deploy/docker-compose.full.yml stop api

# 2. Neueste Sicherung zurückspielen (mit ausdrücklicher Bestätigung --yes):
docker compose -f deploy/docker-compose.full.yml run --rm \
  --entrypoint /opt/backup/restore.sh backup --latest --yes

# 3. Anwendung wieder starten (wendet nötige Datenbank-Updates automatisch an):
docker compose -f deploy/docker-compose.full.yml up -d api
```

Eine **bestimmte** Sicherung statt der neuesten:

```bash
docker compose -f deploy/docker-compose.full.yml run --rm \
  --entrypoint /opt/backup/restore.sh backup \
  --file procworks-2026-07-01T02-00.dump --yes
```

**Schutzmechanismen** (der Restore bricht in diesen Fällen ab):

- Die Prüfsumme der Sicherung passt nicht zum Manifest (beschädigt/manipuliert).
- Die Sicherung stammt aus einer **neueren** ProcWorks-Version als die laufende
  (nur „vorwärts" ist auflösbar) → zuerst die passende Version bereitstellen.
- Die Zieldatenbank ist **nicht leer** → zusätzlich `--force` nötig.
- Es sind noch andere Verbindungen offen (API nicht gestoppt) → API stoppen.

> **Abgrenzung:** Der Menüpunkt **„Auf Null zurücksetzen"** in der Oberfläche ist
> ein *fachliches* Leeren (Demodaten) – **kein** Restore. Backup/Restore
> arbeiten eine Ebene tiefer auf der ganzen Datenbank.

---

## 6. Selbsttest einer Sicherung

„Eine Sicherung, die man nicht zurückspielen kann, ist wertlos." Der Selbsttest
spielt eine Sicherung in eine **Wegwerf-Datenbank** und prüft sie, **ohne** die
laufende Datenbank anzufassen:

```bash
docker compose -f deploy/docker-compose.full.yml run --rm \
  --entrypoint /opt/backup/verify.sh backup --latest
```

Er prüft u. a., dass sich die Sicherung überhaupt einspielen lässt, dass jede
laufende Instanz auf ein vorhandenes Prozessmodell verweist und der
Ereignis-Verlauf schlüssig ist. **Empfehlung:** einmal pro Woche ausführen.

---

## 7. Dringend empfohlene Ergänzungen

### 7.1 Off-Site-Kopie

Eine Sicherung, die nur auf demselben Server liegt, überlebt keinen Diebstahl,
Brand oder Totalausfall. Hinterlegen Sie im `backup`-Dienst einen
`BACKUP_SYNC_CMD`, der nach jeder Sicherung eine Kopie an einen zweiten Ort legt
(z. B. Netzlaufwerk, S3, ein anderer Server). Der Befehl läuft im Container; das
Verzeichnis `/backups` ist die Quelle. Beispielhaft (Werkzeug muss im Image
vorhanden bzw. ergänzt sein):

```yaml
    environment:
      BACKUP_SYNC_CMD: "rclone sync /backups remote:procworks-backups"
```

### 7.2 Verschlüsselung

Sicherungen enthalten **sensible Geschäftsdaten** und die Login-Hashes. Für die
Ablage außer Haus sollten sie verschlüsselt sein. Setzen Sie dazu im
`backup`-Dienst eine `BACKUP_PASSPHRASE`; die Sicherungen werden dann symmetrisch
mit GnuPG (AES-256) verschlüsselt (Dateiendung `.gpg`). Der gebündelte Stack
bringt `gpg` bereits mit – nach dem Setzen genügt:

```yaml
    environment:
      BACKUP_PASSPHRASE: "<eine-lange-zufällige-passphrase>"
```

```bash
docker compose -f deploy/docker-compose.full.yml up -d --build backup
```

> **Bewahren Sie die Passphrase getrennt und sicher auf** (Passwort-Manager/
> Tresor) – **ohne sie ist keine Wiederherstellung möglich**. Ist eine Passphrase
> gesetzt, aber `gpg` im Image nicht vorhanden (z. B. eigenes Minimal-Image),
> **bricht** die Sicherung bewusst ab, statt unverschlüsselt zu schreiben.
> Restore/Selbsttest entschlüsseln automatisch (dieselbe `BACKUP_PASSPHRASE`
> muss gesetzt sein).

---

## 8. Was **nicht** in der Sicherung steht

Bewusst **nicht** enthalten (weil regenerierbar oder außerhalb der Datenbank):

- **Zugangsdaten & Konfiguration** – Datenbank-Passwort (`DATABASE_URL`),
  Admin-/Token-Werte, Connector- und Webhook-Geheimnisse. Diese stehen in der
  Deployment-Konfiguration (Compose-Datei / Kubernetes-Secret) und gehören in
  eine **getrennte, gesicherte Ablage** (Passwort-Manager/Tresor). Ohne sie
  gelingt eine vollständige Wiederherstellung nicht.
- **Angemeldete Sitzungen** – nach einem Neustart ist eine erneute Anmeldung
  nötig (kein Datenverlust).
- **TLS-Zertifikate** (Caddy) – werden bei Bedarf automatisch neu bezogen.

---

## 9. Kubernetes (Helm)

Wird ProcWorks über das Helm-Chart betrieben, ist die Sicherung ebenfalls
standardmäßig aktiv: ein **`CronJob`** sichert täglich in einen eigenen
persistenten Speicher (PVC). Einstellungen stehen im `backup:`-Block der
`values.yaml` (Zeitplan, Aufbewahrung, Speichergröße, optionale Passphrase).

Sofort-Sicherung:

```bash
kubectl create job --from=cronjob/<release>-procworks-backup backup-now
```

Wiederherstellen (API zuerst herunterskalieren – der Restore-Job verweigert sich
sonst):

```bash
kubectl scale deploy/<release>-procworks-api --replicas=0
helm upgrade <release> ./deploy/helm \
  --set backup.restore.enabled=true \
  --set backup.restore.file=procworks-2026-07-01T02-00.dump
kubectl scale deploy/<release>-procworks-api --replicas=2
```

Danach den fertigen Restore-Job wieder abschalten (`backup.restore.enabled=false`).

---

## 10. Kurz-Checkliste

- [ ] Läuft der `backup`-Dienst? (`docker compose … ps` / `kubectl get cronjob`)
- [ ] Ist `.last-success` jünger als 26 h?
- [ ] Werden die Sicherungen **außer Haus** kopiert? (`BACKUP_SYNC_CMD`)
- [ ] Sind Sicherungen für die Ablage außer Haus **verschlüsselt**? (`BACKUP_PASSPHRASE`)
- [ ] Sind **Zugangsdaten/Konfiguration** getrennt gesichert?
- [ ] Wurde ein **Restore** schon einmal echt geübt? (Abschnitt 6)
