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
  Datenbank-Schemastand, Prüfsumme `sha256`) zur späteren Überprüfung,
- **dünnt alte Sicherungen aus** nach dem Großvater-Vater-Sohn-Schema:
  standardmäßig **14** tägliche, **8** wöchentliche und **6** monatliche behalten,
- läuft in einem **eigenen** Container: Schlägt eine Sicherung fehl, stört das
  **weder** die Anwendung **noch** die Datenbank.

Die Sicherungen liegen im Docker-Volume **`procworks_backups`** (getrennt vom
Datenbank-Volume, damit ein Volume-Verlust nicht beide trifft). Dateien heißen
`procworks-JJJJ-MM-TTThh-mm.dump` mit passendem `…​.manifest.json`.

---

## 2. Sicherungen ansehen

**In der Oberfläche (am einfachsten):** Als **Administrator** anmelden und die
Sicht **Administration** öffnen; dort steht der Bereich **„Sicherungen"**. Darin
stehen die vorhandenen Sicherungen (Zeitpunkt, App-Version, Größe, verschlüsselt
ja/nein), der Zeitpunkt der letzten erfolgreichen Sicherung – und eine
Schaltfläche **„Jetzt sichern"** ([Abschnitt 3](#3-sofort-eine-sicherung-auslösen)).

> **Welche „App-Version"?** Die Angabe stammt aus der Einstellung
> `PROCWORKS_VERSION` des **`backup`-Dienstes** (unter Kubernetes aus der
> App-Version des Charts, `appVersion` in `Chart.yaml` – nicht der Chart-Version
> `version`), nicht aus der laufenden Anwendung. Im mitgelieferten Stack ist
> das dieselbe Einstellung, die auch die Version der Anwendung wählt – beide
> stimmen also überein. Mit selbst gebauten oder lokal getaggten Images, oder
> wenn die Einstellung nur für einzelne Dienste geändert wurde, kann sie von der
> tatsächlich laufenden Version abweichen; diese meldet die Anwendung selbst
> (`/health`). Für den Restore zählt ohnehin nicht diese Zahl, sondern der
> **Datenbank-Schemastand** der Sicherung ([Abschnitt 5](#5-wiederherstellen-restore)).

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
**In der Oberfläche:** unter **Administration → „Sicherungen"** die Schaltfläche
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
schreibt, wird die API vorher gestoppt.

Auf einer **bestehenden Installation** ist die Datenbank nie leer – der Restore
verlangt deshalb dort immer `--force` (ohne bricht er mit „target database is
not empty … pass --force to overwrite it" ab, ohne etwas zu ändern). Nur in eine
frisch angelegte, leere Datenbank genügt `--yes` allein.

```bash
# 1. Anwendung stilllegen (keine Schreiber) und prüfen, dass sie steht:
docker compose -f deploy/docker-compose.full.yml stop api
docker compose -f deploy/docker-compose.full.yml ps api      # darf nicht "running" zeigen

# 2. Optional: Probelauf – prüft Sicherung und Versionsstand, ändert nichts:
docker compose -f deploy/docker-compose.full.yml run --rm \
  --entrypoint /opt/backup/restore.sh backup --latest --yes --force --pre-check-only

# 3. Neueste Sicherung zurückspielen (--yes bestätigt, --force überschreibt
#    die vorhandene Datenbank):
docker compose -f deploy/docker-compose.full.yml run --rm \
  --entrypoint /opt/backup/restore.sh backup --latest --yes --force

# 4. Anwendung wieder starten (wendet nötige Datenbank-Updates automatisch an):
docker compose -f deploy/docker-compose.full.yml up -d api
```

Eine **bestimmte** Sicherung statt der neuesten:

```bash
docker compose -f deploy/docker-compose.full.yml run --rm \
  --entrypoint /opt/backup/restore.sh backup \
  --file procworks-2026-07-01T02-00.dump --yes --force
```

**Schutzmechanismen** – in diesen Fällen bricht der Restore ab, ohne etwas zu
ändern:

- Die Prüfsumme der Sicherung passt nicht zum Manifest (beschädigt/manipuliert).
- Der **Datenbank-Schemastand** der Sicherung ist neuer als der der laufenden
  Installation (die Sicherung stammt aus einer neueren ProcWorks-Version; nur
  „vorwärts" ist auflösbar) → zuerst die passende Version bereitstellen.
- `--yes` fehlt.

Die folgenden Prüfungen hebt **`--force` auf** – sie werden dann nur als
Warnung ins Log geschrieben:

- Die Zieldatenbank ist **nicht leer** (auf einer bestehenden Installation
  immer der Fall – dafür ist `--force` gedacht).
- Es sind noch **andere Verbindungen** zur Datenbank offen (API nicht gestoppt).
  Ohne `--force` verweigert sich der Restore dann; **mit** `--force` schreibt er
  trotzdem („other DB connection(s) present (proceeding due to --force)").
  Deshalb die API in Schritt 1 wirklich stoppen und das prüfen.
- Die Sicherung stammt von einer **neueren PostgreSQL-Hauptversion** als die
  Zieldatenbank.

> **Anmeldungen nach dem Restore:** Ein Restore **beendet alle Sitzungen** –
> nach dem Start der API melden sich alle neu an (siehe
> [Abschnitt 8](#8-was-nicht-in-der-sicherung-steht)).

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

Er prüft genau diese Punkte:

- die Prüfsumme der Sicherung gegen das Manifest,
- dass sich die Sicherung vollständig in eine leere Datenbank einspielen lässt,
- dass jede gespeicherte Instanz auf ein vorhandenes Prozessmodell verweist,
- dass die laufenden Nummern des Ereignis-Verlaufs (Audit) eindeutig sind,
- dass genau ein Datenbank-Schemastand eingetragen ist.

**Nicht** geprüft wird die **Hash-Verkettung** des Ereignis-Verlaufs (ob jedes
Ereignis auf seinen Vorgänger verweist). „OK" heißt also: einspielbar und in den
genannten Punkten stimmig – nicht, dass der Verlauf lückenlos verkettet ist.
Die Verkettung prüft die laufende Anwendung selbst (Abschnitt 6.1).
**Empfehlung:** einmal pro Woche ausführen.

### 6.1 Prüfung der Hash-Kette des Ereignis-Verlaufs

Jedes Ereignis im Verlauf (Audit) nennt den Prüfwert seines Vorgängers. Wird
ein Ereignis nachträglich verändert oder entfernt, bricht die Kette an genau
dieser Stelle. Die Anwendung prüft die ganze Kette auf Anfrage eines Admins:

```bash
TOKEN=$(curl -s -X POST https://<host>/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"login": "admin", "password": "<passwort>"}' | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" https://<host>/api/admin/audit-chain
```

Das Feld `status` sagt:

| `status` | Bedeutung | Was tun |
|----------|-----------|---------|
| `intact` | Die Kette ist lückenlos (bzw. alle historischen Verzweigungen sind abgeschlossen). | Nichts. |
| `open` | Die Kette enthält **historische Verzweigungen** (Liste `branches`, je Ereignisnummer `seq` und Zeitpunkt `timestamp`). | Prüfen und mit einem Anker abschließen (unten). |
| `defective` | Ein Ereignis wurde verändert oder entfernt, oder ein Prozess hat ohne Sperre angehängt (Liste `defects`, je Art `kind`, Nummer und Zeitpunkt). | Unten „Bei einem Defekt“. |

**Was ist eine historische Verzweigung?** Versionen vor der Einführung dieser
Prüfung konnten bei gleichzeitigen Aktionen zwei Ereignisse an dasselbe
Kettenende hängen. Jedes dieser Ereignisse ist unverändert und für sich
prüfbar; nur die Kette ist an dieser Stelle keine Linie. Der Verlauf wird
grundsätzlich nie umgeschrieben – solche Stellen bleiben deshalb bestehen und
werden mit Zeitpunkt gemeldet. Seit dieser Version hängt jede Aktion unter
einer Sperre an; neue Verzweigungen entstehen nicht mehr. Verzweigungen nach
dem Update gelten deshalb **nicht** als historisch, sondern als Defekt.

**Anker setzen** (einmalig nach dem Update, wenn `open` gemeldet wird): Die
gelisteten Zeitpunkte mit Zeiten gleichzeitiger Nutzung vergleichen, dann mit
Begründung abschließen:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reason": "Verzweigungen geprüft: gleichzeitige Aktionen vor dem Update"}' \
  https://<host>/api/admin/audit-chain/anchor
```

Der Anker ist selbst ein Ereignis im Verlauf (mit Admin, Begründung und den
abgeschlossenen Nummern); er ändert nichts Bestehendes. Danach meldet die
Prüfung `intact`, die Verzweigungen bleiben mit `anchored_by` sichtbar. Ist
die Kette `defective`, verweigert die Anwendung den Anker (`409`) – ein Anker
deckt nie eine Veränderung.

Der Anker hält den geprüften Stand fest: jede abgeschlossene Verzweigung und
jedes **Ende einer Verzweigung** (Liste `dead_ends`: ein Ereignis, auf das kein
späteres verweist) mit seinem Hash. Gerade ein solches Ende ließe sich sonst
entfernen oder samt Hash neu schreiben, ohne dass eine Verkettung bricht. Ab
dem Anker meldet die Prüfung deshalb `defective`, wenn ein vom Anker genanntes
Ereignis fehlt oder eine genannte Verzweigung keine mehr ist
(`anchored-branch-missing`), wenn es einen anderen Hash trägt
(`anchored-branch-altered`) oder wenn vor dem Anker ein Ende auftaucht, das er
nicht nennt (`branch-not-anchored`).

**Der Stand vor dem Update ist festgeschrieben.** Das Update hält Anzahl und
Prüfwert aller Ereignisse bis `serialized_since` fest (Feld `historic_events`
der Prüfung). Wird dort später ein Ereignis entfernt, samt Prüfwert neu
geschrieben oder eingeschoben, meldet die Prüfung `defective` mit der Art
`historic-range-changed` – **auch vor dem ersten Anker**, und der Anker wird
verweigert. Die Meldung nennt nur, *dass* sich dieser Bereich geändert hat;
welches Ereignis, zeigt der Vergleich mit einer Sicherung von vor dem Update.
Festgehalten wird der Stand, den das Update vorfindet: Eine Änderung, die
schon **vor** dem Update geschah, gehört dazu. Wer sichergehen will, vergleicht
vor dem ersten Anker einmal mit einer Sicherung von vor dem Update.

**Prüfwert außerhalb der Datenbank festhalten.** Anzahl und Prüfwert stehen
in derselben Datenbank wie die Ereignisse. Wer dort Ereignisse samt Prüfwert
umschreiben kann, kann auch diesen Eintrag neu berechnen – bis zum ersten Anker
bemerkt die Prüfung das nicht. Die Prüfung zeigt deshalb beide Werte
(`historic_events`, `historic_digest`): Notieren Sie sie gleich nach dem Update
außerhalb des Servers (etwa im Betriebshandbuch) und vergleichen Sie sie vor
dem ersten Anker. Jeder Anker hält sie in seinem eigenen Eintrag fest; ändert
sich der Eintrag danach, meldet die Prüfung `defective` mit der Art
`historic-record-changed`. Wer auch den Anker samt allen späteren Ereignissen
umschreibt, bleibt unentdeckt – die Grenze jeder Prüfkette in der eigenen
Datenbank; dagegen hilft nur ein außerhalb aufbewahrter Anker-Eintrag oder eine
Sicherung.

Zwei gleichzeitig gesendete Anker werden nacheinander geschrieben: Der erste
schließt ab, der zweite erhält `409 AUD.chain-nothing-open`.

**Bei einem Defekt:** Die gemeldete Nummer und den Zeitpunkt notieren, den
Verlauf mit einer Sicherung von vorher vergleichen (Selbsttest, Abschnitt 6)
und klären, wer zu dieser Zeit Zugriff auf die Datenbank hatte. Liegt der
Defekt direkt nach einem Update mit mehreren API-Prozessen
(`unserialized-branch`, `end-mismatch`), lief vermutlich kurz ein Prozess der
Vorversion mit – künftig vor einem Update alle API-Prozesse beenden.

**Empfehlung:** die Prüfung zusammen mit dem Selbsttest einmal pro Woche
ausführen.

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
- **TLS-Zertifikate** (Caddy) – werden bei Bedarf automatisch neu bezogen.

**Die Anmeldungen (Sitzungen)** liegen in der Datenbank (nur als Prüfwert des
Tokens, nie das Token selbst) und stehen damit auch in der Sicherung. Daraus
folgt:

- Nach einem **Neustart** oder Update der API bleiben Angemeldete angemeldet
  (bis die Sitzung abläuft, Standard 12 Stunden nach der Anmeldung,
  einstellbar über `PROCWORKS_SESSION_TTL_MINUTES`).
- Ein **Restore beendet alle Sitzungen**: Er spielt alles zurück außer den
  Sitzungen – die Sitzungstabelle ist danach leer, in derselben Transaktion
  wie der übrige Restore. Damit wird auch keine Sitzung wieder gültig, die
  nach der Sicherung abgemeldet oder deren Login danach gelöscht wurde. Nach
  dem Start der API melden sich alle neu an. (Die Prüfung einer Sicherung in
  einer Wegwerf-Datenbank, Abschnitt 6, spielt sie dagegen vollständig ein.)
- Ein Passwort-Reset oder das Löschen eines Logins beendet dessen Sitzungen.

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

**Updates:** Das Chart ersetzt die API-Pods standardmäßig auf einen Schlag
(`api.strategy.type: Recreate`): Alle alten Pods enden, bevor die neue Version
startet. Ein Prozess der Vorversion, der kurz neben dem neuen auf derselben
Datenbank liefe, kennte deren Schutzmechanismen nicht und könnte gleichzeitige
Änderungen überschreiben. Das Update hat dadurch eine kurze Ausfallzeit (bis die
neuen Pods bereit sind). `RollingUpdate` nur bewusst einstellen.

Wiederherstellen (API zuerst herunterskalieren – der Restore-Job verweigert sich
sonst):

```bash
kubectl scale deploy/<release>-procworks-api --replicas=0
helm upgrade <release> ./deploy/helm \
  --set backup.restore.enabled=true \
  --set backup.restore.force=true \
  --set backup.restore.file=procworks-2026-07-01T02-00.dump
kubectl scale deploy/<release>-procworks-api --replicas=1
```

Zum Schluss die API wieder auf die gewohnte Anzahl Prozesse setzen (Standard
des Charts: 1, sonst Ihr Wert für `api.replicaCount`). Liegt die Installation in
einem eigenen Namespace, an jeden Befehl `-n <namespace>` anhängen; mit
`fullnameOverride` heißt das Deployment `<fullnameOverride>-api`.

Ohne `backup.restore.file` spielt der Job die neueste Sicherung zurück. Wie
unter Compose ([Abschnitt 5](#5-wiederherstellen-restore)) ist die Datenbank
einer bestehenden Installation nie leer: Der Job braucht dort zusätzlich
`--set backup.restore.force=true`, sonst bricht er mit „target database is not
empty" ab, ohne etwas zu ändern. Ohne `force` bricht er auch ab, solange noch
API-Pods mit der Datenbank verbunden sind; **mit** `force` prüft er das nicht
mehr, sondern warnt nur – deshalb die API vorher wirklich auf 0 skalieren
(`kubectl get pods` zeigt keinen API-Pod mehr). Die API wendet nach dem
Hochskalieren nötige Datenbank-Updates selbst an. Danach den fertigen
Restore-Job wieder abschalten (`backup.restore.enabled=false`,
`backup.restore.force=false`).

---

## 10. Kurz-Checkliste

- [ ] Läuft der `backup`-Dienst? (`docker compose … ps` / `kubectl get cronjob`)
- [ ] Ist `.last-success` jünger als 26 h?
- [ ] Werden die Sicherungen **außer Haus** kopiert? (`BACKUP_SYNC_CMD`)
- [ ] Sind Sicherungen für die Ablage außer Haus **verschlüsselt**? (`BACKUP_PASSPHRASE`)
- [ ] Sind **Zugangsdaten/Konfiguration** getrennt gesichert?
- [ ] Wurde ein **Restore** schon einmal echt geübt? (Abschnitt 6)
- [ ] Meldet die Prüfung der Hash-Kette `intact`? (Abschnitt 6.1)
