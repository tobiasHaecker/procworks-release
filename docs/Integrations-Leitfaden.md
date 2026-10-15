<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Integrations-Leitfaden: ProcWorks mit fremden Tools verbinden

> Praktischer Leitfaden für Entwickler:innen, die **fremde Systeme an ProcWorks
> anbinden**. Er liefert konkrete **Rezepte, Endpunkt-Referenzen und
> Beispiel-Aufrufe**. Zu Sicherheit und Identitäten (Bearer-Token, Rollen) siehe
> die Windows-Server-Setup-Anleitung und die `core/README.md`.
>
> Status: **Umgesetzt** (Roadmap-Phasen P0–P6). Alle hier beschriebenen Endpunkte
> existieren und sind getestet. Die OpenAPI-Spezifikation der laufenden Instanz steht
> unter `/docs` (Swagger UI) bzw. `/openapi.json`.

---

## 0. Grundbegriffe in einem Satz

| Begriff | Bedeutung |
|---|---|
| **Inbound** | Ein fremdes Tool ruft ProcWorks auf (Instanz starten, Aufgabe abschließen, Daten lesen/schreiben). |
| **Outbound – Pull** | Eine automatische Aktivität wird zur **External Task**; ein *Worker* holt sie per `fetch-and-lock` ab und meldet das Ergebnis zurück. |
| **Outbound – Push** | Bei Aktivierung **pusht** ProcWorks das Eingabe-Datenpaket an ein serverseitig konfiguriertes Tool-Endpoint (`HTTP_PUSH`). |
| **Webhook** | Abonnement auf Domänenereignisse (`instance.*`, `task.*`); ProcWorks stellt signierte Events zu. |
| **Connector** | Adapter zu einem Datenspeicher (SQL …) hinter dem Data Access Layer; liest/schreibt `EXTERNAL`-Datenelemente. |
| **Idempotency-Key** | Header `Idempotency-Key`, der wiederholte mutierende Aufrufe einmal-wirksam macht. |

Alle Integrationsendpunkte liegen unter dem versionierten Präfix **`/v1`** und teilen sich
**Maschinen-Authentifizierung mit Scopes** (Abschnitt 1). Der Ausführungskern bleibt rein:
die Integrationsschicht ruft ausschließlich die geprüften Operationen des Kerns auf.

---

## 1. Authentifizierung & Scopes

ProcWorks unterscheidet **Personen-Rollen** (`viewer`/`operator`/`modeler`/`admin`) und die
**Maschinen-Rolle** `integration`. Ein Integrations-Token trägt die Rolle `integration` und
eine Menge **Scopes**. Jeder `/v1`-Endpunkt verlangt entweder eine passende Personen-Rolle
**oder** die Rolle `integration` **mit** dem passenden Scope (Wildcard `*` deckt alles ab).

| Scope | Deckt ab |
|---|---|
| `instances:start` | Instanzen starten |
| `tasks:fetch` | External Tasks / Inzidente / Push-Endpunkte lesen, `fetch-and-lock` |
| `tasks:complete` | Aufgaben abschließen, Entscheidungen, Fehler/BPMN-Fehler, Lock verwalten, Inzident auflösen |
| `data:read` | Instanzdaten und Connector-Metadaten lesen |
| `data:write` | Instanzdaten schreiben |
| `events:subscribe` | Webhooks verwalten |
| `*` | alle Scopes |

### 1.1 Token bereitstellen (Betreiber)

**Im ausgelieferten Stack (Passwort-Login für Personen)** bleiben die Personen-Logins
erhalten; Maschinen-Tokens kommen **zusätzlich** hinzu. Dafür die Tokens als JSON in
`deploy/.env` eintragen (Variable `PROCWORKS_TOKENS_JSON`, Aufbau wie unten) und den
Stack neu starten. Neben Passwort-Logins nimmt ProcWorks nur Tokens mit genau der Rolle
`integration` an – ein Token ist ein Geheimnis ohne Passwort dahinter und soll nie eine
Person oder einen Administrator vertreten.

**Reiner Token-Betrieb** (ohne Personen-Logins, z. B. für eine reine Integrations-
Instanz): Token-Modus aktivieren und Tokens als JSON-Datei hinterlegen (nur
SHA-256-Digests werden gespeichert):

```jsonc
// tokens.json
{
  "<geheimes-token>": {
    "subject": "erp-bridge",
    "roles": ["integration"],
    "scopes": ["instances:start", "tasks:complete", "data:read", "data:write"]
  }
}
```

```bash
export PROCWORKS_AUTH=token
export PROCWORKS_TOKENS=/pfad/zu/tokens.json
```

Aufrufe tragen den Bearer-Header:

```bash
curl -H "Authorization: Bearer <geheimes-token>" https://host/v1/instances/i-123
```

Statt einer Datei (`PROCWORKS_TOKENS`) geht auch hier `PROCWORKS_TOKENS_JSON` mit dem
JSON selbst.

> Ohne `PROCWORKS_AUTH` läuft der Kern im offenen Entwicklungsmodus (`open`) – keine
> Header nötig, praktisch für lokale Tests, **nicht** für den Produktivbetrieb. Der
> mitgelieferte Stack setzt `PROCWORKS_AUTH=password`.

---

## 2. Inbound: ein Tool steuert ProcWorks

### 2.1 Instanz starten (mit Startdaten)

```bash
# 1) Instanz eines freigegebenen Schemas starten
curl -X POST https://host/v1/schemas/urlaubsantrag/instances \
     -H "Authorization: Bearer $TOKEN" \
     -H "Idempotency-Key: start-2026-0001"
# -> 201 { "id": "instance_42", "state": "RUNNING", ... }

# 2) Startdaten setzen (typgeprüft)
curl -X PUT https://host/v1/instances/instance_42/data \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"values": {"tage": 12}}'
```

* Nur **freigegebene** Schemata sind über `/v1` startbar (Entwürfe → `409`).
* `PUT …/data` prüft jeden Wert gegen den Datentyp des Elements (`422` bei Typfehler).
* Das Steuerzeichen NUL (U+0000) ist in keiner Anfrage zulässig – weder in Pfad und Query
  noch in einem JSON-Rumpf (`422`, `code` `REQ.nul-character`; es wird nichts ausgeführt).
* Ein JSON-Rumpf muss gültiges UTF-8 sein; ungültige Bytefolgen (auch die rohe Kodierung
  eines halben Ersatzzeichenpaars) lehnt die API ab (`422`, `code` `REQ.invalid-encoding`).
  **Hinweis für bestehende Integrationen:** Bis Version 1.33.2 kam hier `400`. Wer auf den
  Status prüft, wertet jetzt `422` mit diesem `code` aus.
* Jede Änderung über `PUT …/data` steht im Audit-Verlauf (Ereignis
  `INSTANCE_DATA_SET` mit altem und neuem Wert und dem Token als Absender);
  Kennzahlen und Process Mining bleiben davon unberührt. Ein **abgeschlossener**
  Vorgang nimmt keine Daten mehr an (`409`).
* Service-Tokens behalten diesen Weg unverändert. **Persönliche Logins**
  (Passwort, persönliches Firmenkonto) setzen über den Endpunkt nur die Werte
  ihres eigenen offenen Schritts (`403` sonst); Modellierer und Administratoren
  dürfen darüber hinaus korrigieren, dann aber nur mit Begründung im Feld
  `reason` (`422`, wenn sie fehlt).
* Der **`Idempotency-Key`** macht einen wiederholten Start einmal-wirksam: Die Wiederholung
  führt nichts erneut aus, sondern antwortet mit demselben Status und dem **aktuellen** Stand des
  damals angelegten bzw. geänderten Objekts (ProcWorks speichert zum Schlüssel nur dessen
  Kennung, keine Antwortdaten). Hat sich der Vorgang inzwischen weiterentwickelt, zeigt die
  Wiederholung das.
  Er gilt für genau **eine** Anfrage: Derselbe Schlüssel mit einem anderen Schema, anderen Daten
  oder an einem anderen Endpunkt wird mit `409` und `code: "IDEM.key-reused"` abgelehnt, ohne
  etwas auszuführen. Schlüssel gelten je Identität; eine gescheiterte Anfrage bindet ihn nicht.
  Treffen zwei Anfragen mit demselben neuen Schlüssel **gleichzeitig** ein – auch bei zwei
  verschiedenen API-Prozessen –, wartet die zweite, bis die erste gespeichert ist, und erhält
  deren Antwort.
* **Geltungsbereich des Schlüssels:** Mit Datenbank (`DATABASE_URL`, jede Installation mit
  PostgreSQL) reserviert die API den Schlüssel **in der Datenbank**, in derselben Transaktion
  wie die Aktion; die Antwort wird nur festgehalten, wenn die Aktion gespeichert wurde.
  Daraus folgt:
  - Der Schlüssel gilt **über einen Neustart** der API und **über mehrere API-Prozesse**
    hinweg (mehrere Worker oder Container hinter einem Lastverteiler).
  - **Haltedauer 24 Stunden:** Danach ist der Schlüssel wieder frei; eine Wiederholung nach
    mehr als einem Tag wird erneut ausgeführt. Abgelaufene Einträge räumt die API selbst auf.
  - Gespeichert werden nur ein Prüfwert aus Identität und Schlüssel, ein Prüfwert der Anfrage
    und die Antwort – weder der Login noch der Schlüssel selbst. Ein Schlüssel darf beliebig
    lang sein.
  - Ohne Datenbank (Probebetrieb im Arbeitsspeicher) gilt dasselbe innerhalb des einen
    API-Prozesses, bis zu dessen Neustart.

  Ein wiederholter **Abschluss** nach Ablauf der Haltedauer liefert nicht die erste Antwort,
  sondern `409` mit `code: "EX.not-ready"` (der Schritt ist schon abgeschlossen) – oder
  `409` mit `code: "EX.not-running"`, wenn es der letzte Schritt war und der Vorgang damit
  beendet ist; eine Integration kann beide als „bereits erledigt“ werten. Wer über einen Tag hinaus
  wiederholt, prüft vorher, ob der Vorgang schon existiert (z. B. über eigene Startdaten wie
  eine Antragsnummer). **Hinweis zum Update:** Schlüssel, die die Vorversion im Arbeitsspeicher
  hielt, gehen beim Update verloren – wie bisher bei jedem Neustart. Angenommen wird der
  Schlüssel an den Aufrufen **Vorgang starten**, **Schritt abschließen**
  (`POST /v1/instances/{id}/nodes/{node_id}/complete`), **Vorgangsdaten setzen**
  (`PUT /v1/instances/{id}/data`) und **externe Aufgabe melden**
  (`POST /v1/external-tasks/{id}/complete`); an anderen Aufrufen bleibt er ohne Wirkung.
* Das Start-Ereignis im Audit (`INSTANCE_CREATED`) nennt den Starter: den Login bzw. das
  Token als `detail.actor` und, wenn der Login an eine Person gebunden ist, diese Person.
  Ein Bearbeiter, der einen Vorgang startet, kann ihn danach lesen.

### 2.2 Aufgabe abschließen / Entscheidung treffen

```bash
# Interaktive Aktivität abschließen (inkl. Datenübergabe)
curl -X POST https://host/v1/instances/instance_42/nodes/act_pruefen/complete \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"agent_id": "a-erika", "data": {"geprueft": true}}'

# XOR-Verzweigungen entscheidet die Engine automatisch aus den Instanzdaten
# (vollständige, überschneidungsfreie Partition, K7) – es genügt, den
# Diskriminator-Wert beim Abschließen des vorgelagerten Schritts zu übergeben.
```

* Mitgeben lassen sich **nur Werte, die der abgeschlossene Schritt schreibt**
  (Datenbindung „Schreiben“ bzw. „Lesen und Schreiben“ am Schritt). Ein Wert,
  den der Schritt nur liest, oder ein unbekanntes Feld ergibt `422` (Befund D6,
  seit 1.27.1) – so kann etwa ein Genehmigungsschritt den Betrag nicht
  nachträglich ändern. Dieselbe Regel gilt seit jeher für externe Aufgaben.
  Andere Werte setzt eine Integration über `PUT …/data` (Scope `data:write`).
* **Pflichtwerte und Maskenregeln gelten auf jedem Weg.** Schreibt ein
  Schritt ein Datenelement als Pflicht, braucht es beim Abschluss einen Wert –
  mitgegeben oder vorher per `PUT …/data` gesetzt –, sonst `422` mit `code`
  `D1.write-missing` (`params.element`, `params.step`). **Wiederholt eine
  Schleife den Schritt** (er wurde schon einmal abgeschlossen; ein Zweig, den
  erst eine spätere Runde nimmt, läuft dort zum ersten Mal), zählt nur noch,
  was der Abschluss mitbringt oder was
  in dieser Runde per `PUT …/data` gesetzt wurde, **während der Schritt offen
  ist**; der Wert der Vorrunde bleibt gespeichert und lesbar, versorgt den
  wiederholten Schritt aber nicht – so entscheidet jede Runde über frische
  Werte. Für externe Aufgaben und Push-Antworten heißt das: in jeder Runde die
  Pflichtausgaben in `variables` melden. Auch eine leere oder nur
  aus Leerzeichen bestehende Zeichenkette für ein Textfeld ergibt
  `D1.write-missing`. `null` als Wert passt dagegen zu keinem Datentyp: Beim
  Abschluss eines Schritts kommt `422` mit `D3.wrong-type`, bei einer externen
  Aufgabe `422` mit `ET.output-type` (ebenso `""` für ein Zahlenfeld). Die Eingabeprüfungen der Maske (Minimum,
  Maximum, Länge, Muster) gelten für den Wert, mit dem der Schritt endet,
  und für jeden Wert über `PUT …/data` (`422`, `U4.*`). Das gilt ebenso für
  externe Aufgaben (`422` mit `code` im Rumpf) und für Aufsichtseingriffe.
* Die mitgegebenen Werte müssen zum Datentyp ihres Elements passen, sonst
  antwortet der Abschluss mit `422` (Befund D3) und der Schritt bleibt offen.
  Der Typ **`DECIMAL`** (Betrag) ist eine JSON-Zahl mit höchstens zwei
  Nachkommastellen: `499.99` ist gültig, `499.999` nicht. Ein **`DATE`** ist
  ein echter Kalendertag in genau der Form `JJJJ-MM-TT` (`2024-02-29` ist
  gültig, `2026-02-30`, `30.02.2026` oder ein Zeitstempel nicht), eine
  **`URI`** ein Link mit `http://`, `https://` oder `mailto:`. Diese Regeln
  gelten für alle eingehenden Werte (`PUT …/data`, Abschluss, externe
  Aufgaben, Migration); bereits gespeicherte Werte bleiben lesbar.
* Lehnt die Engine eine Aktion ab (`409`), enthält die Antwort neben
  `message` jetzt `code` (z. B. `EX.claimed-by-other`) und `params` (z. B.
  `step`, `agent`); ebenso ein abgelehnter BPMN-Import (`422`, `BPMN.*`),
  der Start eines nicht freigegebenen Schemas über `/v1` (`409`,
  `EX.not-released`) und der von der Engine abgelehnte Abschluss einer
  externen Aufgabe (`409` mit dem Code der Engine). Auch die Worker-Endpunkte
  nennen ihren Grund: `409` `ET.not-locked` (`params.state`), `ET.locked-by-other`,
  `ET.lock-expired`, `ET.instance-gone`; `422` bei ungültigen Ausgaben
  `ET.output-not-writable`, `ET.output-unknown`, `ET.output-type`,
  `ET.output-not-record` (`params.name` = gemeldete Variable) und
  `ET.no-connector`; eine unbekannte Aufgabe, ein unbekannter Inzident oder
  Vorgang unter `/v1` ergibt `404` mit `code` `OP.not-found` und
  `params.kind` (`external_task`, `incident`, `instance`).
  Ein persönlicher Bearbeiter-Login, der am Vorgang nicht beteiligt ist,
  bekommt bei jeder Aktion `404`.
* **Unerwarteter Datenbankfehler (`500` `DB.unexpected`).** Lehnt die Datenbank
  eine Änderung aus einem unerwarteten Grund ab, antwortet ProcWorks mit `500` und
  `code: "DB.unexpected"`; gespeichert wurde dann nichts. Eine Wiederholung ist
  unbedenklich, hilft aber meist nicht – Einzelheiten stehen im Protokoll der API.
  Ist die Datenbank dagegen nicht erreichbar, kommt `503` `DB.unavailable` mit
  `Retry-After`: Ausgeführt wurde nichts, dann später wiederholen.
* **Ergebnis unbekannt (`503` `DB.outcome-unknown`, ohne `Retry-After`).** Bricht
  die Verbindung zur Datenbank genau beim Speichern ab (etwa weil der
  Datenbankserver hängt), kann die Änderung trotzdem gespeichert sein. Dann
  **nicht blind wiederholen**, sondern erst nachsehen (Vorgang bzw. Modell neu
  lesen) – oder die Anfrage mit **demselben `Idempotency-Key`** wiederholen:
  War sie gespeichert, kommt die erste Antwort zurück, sonst wird sie jetzt
  ausgeführt. Für Starts und Abschlüsse aus Integrationen empfiehlt sich der
  Schlüssel deshalb immer.
* **Gleichzeitige Änderungen (`409` `DB.conflict`).** Ein Vorgang, Modell oder
  eine geteilte Organisation wird nur gespeichert, wenn es seit dem Lesen
  unverändert ist. Ändern zwei
  Anfragen denselben Vorgang zur selben Zeit, wiederholt ProcWorks die spätere
  selbst auf dem neuen Stand: Sie gelingt (etwa zwei Schritte paralleler
  Zweige) oder erhält die fachliche Antwort des neuen Stands (etwa `409`
  `EX.claimed-by-other` oder `EX.not-ready`). Nur wenn der Konflikt anhält,
  kommt `409` mit `code: "DB.conflict"` und `params` `kind` (`instance`,
  `schema`, `external_task` oder `org`) und `id` – **gespeichert wurde dann nichts**. Empfehlung: den
  Vorgang neu lesen, prüfen, ob die Aktion noch nötig ist, und sie nach einer
  kurzen, leicht zufälligen Pause wiederholen (z. B. 0,2–1 s, höchstens einige
  Male). Beim Abschluss einer externen Aufgabe mit Rückschreiben in ein
  angebundenes System wiederholt ProcWorks nicht selbst – das Rückschreiben ist
  dann schon geschehen; der Worker entscheidet über die Wiederholung wie bei
  jeder anderen Ablehnung. Jede Anfrage wird mit einer Datenbank ganz oder gar
  nicht gespeichert; Webhooks und Pushes einer Anfrage gehen erst hinaus, wenn
  ihre Änderung gespeichert ist.
* **Ad-hoc-Änderungen** eines laufenden Vorgangs
  (`POST /instances/{id}/adhoc/insert|rename|delete`) stehen nur den Rollen
  Modellierer und Administrator offen und verlangen für echte Vorgänge einen
  Anlass im Feld `reason` (sonst `403` bzw. `422`). Der Verlauf nennt Anlass und
  handelnde Person.

**Wer schließt ab?** Ein Token, das an einen Bearbeiter gebunden ist, schließt
als dieser Bearbeiter ab; ein ungebundenes Token nennt ihn über `agent_id`, und
die Bearbeiterregel des Schritts wird geprüft (`409`, wenn er nicht zuständig
ist). Das Audit hält dann beides fest: den genannten Bearbeiter und den
Absender des Tokens (`detail.actor`). Im Namen einer Person handeln dürfen nur
Maschinen-Identitäten: statische Token (`PROCWORKS_AUTH=token`) und Identitäten
mit der Rolle `integration`, etwa ein Dienstkonto beim Firmen-Login (JWT).
Persönliche Logins (Passwort, Firmenkonto ohne diese Rolle) erhalten `403`,
wenn sie eine andere Person nennen. Ein ungebundenes Token **ohne** `agent_id` an einem Schritt mit
Bearbeiterregel ist ein **Aufsichtseingriff**: Er braucht eine Begründung im
Feld `supervision_reason` (sonst `422` mit `code` `REQ.reason-required` und
`params.what` `supervision`) und wird mit ihr im Audit festgehalten. Eine
Begründung aus Leerzeichen oder unsichtbaren Zeichen zählt nicht. Bestimmt ein
späterer Schritt seinen Bearbeiter aus der Person, die diesen Schritt erledigt
(„Bearbeiter von Schritt X“ oder dessen Vorgesetzte:r), ist der Eingriff nicht
möglich: `422` mit `code` `SUP.performer-needed` – nach ihm gäbe es diese
Person nicht. Darf ein späterer Schritt gerade **nicht** von dieser Person
erledigt werden (Vier-Augen-Prinzip, „… außer Bearbeiter von Schritt X“), ist
der Eingriff ebenfalls nicht möglich: `422` mit `code` `SUP.four-eyes` (gleiche
`params` `step`/`dependants`) – die Funktionstrennung ließe sich ohne
Bearbeiter nicht nachweisen.
Bei aktiver Lizenzierung ist er nicht zulässig (`403`) — dann immer mit einem
lizenzierten Bearbeiter abschließen.

### 2.3 Instanzdaten lesen

```bash
curl https://host/v1/instances/instance_42/data -H "Authorization: Bearer $TOKEN"
# -> { "values": { "tage": 12, "geprueft": true } }

curl https://host/v1/instances/instance_42/tasks -H "Authorization: Bearer $TOKEN"
# -> offene Arbeitslisten-Einträge mit Priorität und Schema-Version
```

---

## 3. Outbound – Pull (External Task / Worker)

Eine automatische Aktivität wird über ihr **Service-Binding** auf `EXTERNAL_TASK` gestellt
und erhält ein **Topic**. Sobald sie aktiviert wird, materialisiert ProcWorks eine External
Task. Ein Worker arbeitet die Schleife **fetch → bearbeiten → complete/failure** ab.

### 3.1 Schema vorbereiten (Modellierer)

```bash
# Dienst zuweisen (automatisch) + Automatik auf External Task stellen
curl -X POST https://host/schemas/$SID/service \
     -d '{"node_id": "act_pruefen", "name": "Bonität", "automatic": true}'
curl -X POST https://host/schemas/$SID/automation \
     -d '{"node_id": "act_pruefen", "automation": "EXTERNAL_TASK", "topic": "bonitaet"}'
```

### 3.2 Worker-Schleife

```bash
# 1) Aufgabe(n) abholen und sperren
curl -X POST https://host/v1/external-tasks/fetch-and-lock \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"worker_id": "w-7", "topics": ["bonitaet"], "lock_ms": 300000}'
# -> [ { "id": "et_ab12", "topic": "bonitaet", "input_variables": {"betrag": 1200}, "state": "LOCKED" } ]

# 2a) Erfolgreich abschließen (Rückgabedaten)
curl -X POST https://host/v1/external-tasks/et_ab12/complete \
     -H "Authorization: Bearer $TOKEN" \
     -H "Idempotency-Key: et_ab12-done" \
     -d '{"worker_id": "w-7", "variables": {"approved": true}}'

# 2b) Transienter Fehler -> Wiederholung mit Backoff
curl -X POST https://host/v1/external-tasks/et_ab12/failure \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"worker_id": "w-7", "error_message": "Upstream-Timeout"}'

# 2c) Fachlicher Fehler -> Inzident (siehe 3.3)
curl -X POST https://host/v1/external-tasks/et_ab12/bpmn-error \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"worker_id": "w-7", "error_code": "BONITAET_ABGELEHNT"}'
```

Weitere Worker-Endpunkte:

| Methode · Pfad | Zweck |
|---|---|
| `POST /v1/external-tasks/{id}/extend-lock` | Lock einer langlaufenden Aufgabe verlängern |
| `POST /v1/external-tasks/{id}/unlock` | Lock sofort freigeben (zurück in die Queue) |
| `GET /v1/external-tasks/{id}` | Aktuellen Aufgabenzustand lesen |

**Garantien:** Lock + Worker-Bindung ⇒ ein Abschluss wird **genau einmal je Sperrfrist**
angewendet: Nur der Worker, der die Aufgabe hält, kann sie abschließen, und ein zweiter Abschluss
derselben Aufgabe wird abgelehnt. Läuft die Sperre ab, bevor der Worker abschließt, kann ein
anderer Worker die Aufgabe erhalten – die eigentliche Arbeit kann dann zweimal geschehen (der
erste Abschluss wird abgelehnt, siehe unten). Wer Nebenwirkungen in Fremdsystemen hat, verlängert
die Sperre rechtzeitig oder macht seine Arbeit wiederholbar. Abgelaufene Locks werden neu
vergeben; Wiederholungen mit exponentiellem **Backoff**; nach Erschöpfung → **Inzident**.

**Genau einmal ausgegeben – je Sperrfrist:** Jeder Schritt eines Vorgangs hat höchstens **eine**
offene Aufgabe (die Datenbank erzwingt das), und eine Aufgabe geht innerhalb ihrer Sperrfrist
(`lock_ms`) an genau **einen** Worker – auch wenn viele Worker-Threads, -Instanzen oder mehrere
API-Prozesse gleichzeitig `fetch-and-lock` aufrufen. Wer verliert, bekommt die nächste freie
Aufgabe oder eine leere Liste, nie dieselbe. Erst wenn die Sperre abläuft, ohne dass der Worker
abschließt, verlängert (`extend-lock`) oder freigibt, kann ein anderer Worker die Aufgabe erhalten;
der erste kann sie dann nicht mehr abschließen: `409` `ET.locked-by-other`, sobald ein anderer
Worker sie abgeholt hat, sonst `409` `ET.lock-expired`. Wer länger arbeitet,
verlängert die Sperre rechtzeitig. Eine neue Schleifenrunde erhält ihre eigene Aufgabe, sobald die
vorige abgeschlossen ist.

**Aufgaben beendeter Vorgänge:** Ist der Vorgang einer Aufgabe beendet oder verschwunden oder der
Schritt nicht mehr aktiv, ohne dass die Aufgabe dabei geschlossen wurde (etwa ein Vorgang, der
anders endete), gibt `fetch-and-lock` sie nicht mehr aus, sondern schließt sie mit dem Zustand
`CANCELLED` (`cancel_reason` `step-ended`) und meldet das als Ereignis `task.cancelled` (mit
`task_id`, `instance_id`, `node_id`, `topic` und `reason`), genau einmal je Aufgabe. Schließt dagegen ein Modellierer oder Administrator
den Schritt **von Hand** ab, endet die Aufgabe sofort als `COMPLETED` mit dem Ereignis
`task.completed` und `closed_by: "supervision"` (Abschnitt 3.3) – der Schritt wurde erledigt,
nur nicht vom Worker. Eine Meldung
dazu antwortet `409` `ET.not-locked` mit `params.state` `CANCELLED`. `CANCELLED` mit
`cancel_reason` `duplicate` tragen Aufgaben, die eine frühere Version doppelt angelegt hatte und
die die Datenbank-Migration beim Update geschlossen hat; für sie kommt kein `task.cancelled`
nachträglich.

**Vorab-Lesen scheitert:** Liest die Aufgabe beim Abholen Daten aus einem angebundenen System
und schlägt das fehl, wird **nur diese Aufgabe** übersprungen: Sie zählt als Fehlversuch (Backoff,
nach Erschöpfung Inzident), und `fetch-and-lock` liefert stattdessen die nächsten Aufgaben aller
genannten Topics bis `max_tasks`. Nur wenn gar keine Aufgabe ausgegeben werden kann, antwortet der
Aufruf mit `502` und dem Grund des ersten Fehlschlags (z. B. `code` `DAL.many-rows`).

**Eingaben beim Abholen:** `input_variables` gibt den Stand des Vorgangs **beim Abholen** wieder,
wie beim Push bei jeder Zustellung. Wird ein Wert nach einem Fehlversuch korrigiert (etwa der
Schlüssel, zu dem das Vorab-Lesen mehrere Zeilen fand), erhält das nächste `fetch-and-lock` den
korrigierten Wert und die dazu gelesenen Daten. Eine bereits gesperrte Aufgabe behält ihr Paket.

**Wiederholungen einstellen:** Die Aktivität trägt `retry_max` (Zahl der Versuche),
`retry_backoff_ms` (Wartezeit vor der ersten Wiederholung) und `request_timeout_ms` (Zeitlimit
eines Push-Aufrufs, Abschnitt 4), zu setzen beim `POST /schemas/$SID/automation` bzw. in der
Oberfläche unter „Erweitert (Robustheit)“. Nach dem ersten Fehlversuch ist die Aufgabe nach
`retry_backoff_ms` wieder abholbar, jeder weitere Fehlversuch verdoppelt die Wartezeit bis
höchstens 60 Sekunden – oder bis zum eingestellten Wert, wenn er größer ist. `0` bedeutet:
sofort wieder abholbar. Nennt der Worker in `failure` selbst ein `retry_timeout_ms`, gilt für
diese Meldung sein Wert. Negative Werte werden mit `422` abgewiesen (auch beim BPMN-Import,
`code` `I1.negative`), ebenso eine Wartezeit
über einen Tag (`retry_backoff_ms` > 86 400 000, `code` `I1.backoff-too-large`) und ein
Zeitlimit über 30 Sekunden (`request_timeout_ms` > 30 000, `code` `I1.timeout-too-large`) und
mehr als 100 Wiederholungen (`retry_max` > 100, `code` `I1.retries-too-large`) – auch beim
BPMN-Import. Ein größerer Wert aus einem älteren Modell wirkt wie die Grenze.

**Grenzen für Werte des Workers:** `lock_ms` (bei `fetch-and-lock` und `extend-lock`) liegt
zwischen 1 und 86 400 000 (ein Tag), `retry_timeout_ms` bei `failure` zwischen 0 und
86 400 000 und `retries` bei `failure` zwischen 0 und 100. Ein Wert außerhalb wird mit `422`
abgewiesen, bevor sich etwas ändert; der Eintrag in `detail` nennt das Feld (`loc`), den
Grund (`type` `less_than_equal` mit `ctx.le` bzw. `greater_than_equal`) und den gesendeten
Wert. Wer länger als einen Tag arbeitet, verlängert die Sperre mit `extend-lock`.

**Verlauf und Ereignisse:** Ein erfolgreicher Abschluss steht im Audit wie der Abschluss durch
eine Person (`ACTIVITY_COMPLETED` mit dem Token als `detail.actor`, der `worker_id` und der
Aufgabe als `detail.external_task`); endet der Vorgang damit, folgen `INSTANCE_COMPLETED` und
das Ereignis `instance.completed`. Ein abgelehnter Abschluss schreibt nichts.

**Prüfinstanzen** (Test-Instanzen eines Entwurfs) erzeugen keine External Tasks, keine Pushes
und keine `task.*`-Ereignisse – auch wenn sie dasselbe Topic oder Push-Ziel nutzen wie der
freigegebene Prozess. Ein Worker bekommt eine solche Aufgabe nie zu sehen; ihre automatischen
Schritte schließt der Modellierer in der Prüfinstanz von Hand ab.

### 3.3 Inzidente

Ein Inzident entsteht, wenn die Wiederholungen einer Aufgabe erschöpft sind – und sofort, wenn
der Worker einen **fachlichen Fehler** meldet (`bpmn-error`). Einen modellierbaren Fehlerpfad,
der den Vorgang auf einen anderen Weg lenkt, gibt es (noch) nicht: Der Schritt bleibt aktiv, die
Aufgabe steht auf `BPMN_ERROR` mit dem gemeldeten `error_code` und wird nicht erneut ausgegeben.
Der Inzident nennt den Code (`worker reported BPMN error '<code>'`), erscheint im Monitoring
und wird als `task.incident` (mit `error_code`) gemeldet. Weiter geht es auf einem von zwei
Wegen: `resolve` reiht die Aufgabe mit neuem Budget wieder ein (der Worker versucht es erneut),
oder ein Modellierer/Admin schließt den Schritt von Hand ab (siehe unten).

**Automatischen Schritt von Hand abschließen:** Ein Schritt mit `EXTERNAL_TASK` oder `HTTP_PUSH`
lässt sich über `POST /instances/{id}/complete` bzw. `/v1/instances/{id}/nodes/{node_id}/complete`
nur als **Aufsichtseingriff** abschließen: nur mit der Rolle Modellierer oder Administrator und
mit einer Begründung in `supervision_reason` (sonst `422` mit `code` `REQ.reason-required`,
`params.what` `automatic`); jede andere Identität erhält `409`
mit `code` `EX.automatic`. Steht hinter dem Login keine Person und bestimmt ein späterer
Schritt seinen Bearbeiter aus der Person, die diesen Schritt erledigt (oder schließt sie aus),
ist der Eingriff wie beim interaktiven Schritt nicht möglich (`422` `SUP.performer-needed` bzw.
`SUP.four-eyes`). Der Verlauf hält Begründung und Person fest (`ACTIVITY_SUPERVISED`).
Die offene Aufgabe des Schritts wird dabei beendet (`COMPLETED`), ihr Inzident aufgelöst und
`task.completed` mit `closed_by: "supervision"` gemeldet; ein Worker, der sie noch zu halten
glaubt, erhält beim Abschluss `409` `ET.not-locked`, und `fetch-and-lock` gibt sie nicht mehr
aus. Ein automatischer Schritt **ohne** Automatik (`MANUAL_NONE`, Dienst ohne externe Aufgabe)
wird unverändert über den regulären Abschluss erledigt. Prüfinstanzen sind ausgenommen.

```bash
curl "https://host/v1/incidents?unresolved_only=true" -H "Authorization: Bearer $TOKEN"
# Aufgabe nach Behebung erneut einreihen (operator/admin):
curl -X POST https://host/v1/incidents/inc_9/resolve -H "Authorization: Bearer $TOKEN"
```

---

## 4. Outbound – Push (`HTTP_PUSH`)

Statt dass ein Worker zieht, **pusht** ProcWorks das Eingabe-Datenpaket aktiv an ein
serverseitig konfiguriertes Tool-Endpoint. Das ist die *asynchrone* Variante:
Der Push trägt ein **Callback-Token**; das Tool quittiert und meldet das
Ergebnis später über den **regulären** Completion-Endpunkt zurück.

### 4.1 Push-Ziele konfigurieren (Betreiber)

Konkrete URLs und Signatur-Secrets bleiben **serverseitig** — im Schema steht nur die
logische `endpoint_ref`. Quelle ist die Umgebungsvariable `PROCWORKS_PUSH_ENDPOINTS`
(Dateipfad **oder** Inline-JSON):

```jsonc
// push-endpoints.json  ->  PROCWORKS_PUSH_ENDPOINTS=/pfad/push-endpoints.json
{
  "erp": { "url": "https://erp.intern/procworks/inbox", "secret_ref": "ERP_PUSH_SECRET" },
  "crm": { "url": "https://crm.intern/hook" }
}
```

* `secret_ref` benennt eine **Umgebungsvariable** mit dem HMAC-Signatur-Secret (optional).
* Push-Ziele dürfen **interne** Hosts sein (vertrauenswürdig, vom Betreiber konfiguriert);
  geprüft werden nur Schema (`http`/`https`) und Host.
* Verfügbare Referenzen (ohne URLs/Secrets) listet `GET /v1/push-endpoints`.

### 4.2 Aktivität auf Push stellen (Modellierer)

```bash
curl -X POST https://host/schemas/$SID/service \
     -d '{"node_id": "act_melden", "name": "ERP-Meldung", "automatic": true}'
curl -X POST https://host/schemas/$SID/automation \
     -d '{"node_id": "act_melden", "automation": "HTTP_PUSH", "endpoint_ref": "erp"}'
```

### 4.3 Was ProcWorks an das Tool sendet

Sobald die Aktivität aktiviert ist (beim Start oder einem späteren Fortschritt), stellt der
Outbox-Dispatcher eine signierte `POST`-Zustellung an die konfigurierte URL zu. Der Body
folgt dem Outbox-Umschlag; das Nutzdatenpaket (`data`) trägt das **Callback-Token**:

```jsonc
{
  "delivery_id": "…",
  "event": "task.push",
  "timestamp": 1700000000.0,
  "data": {
    "task_id": "et_77",
    "instance_id": "instance_42",
    "node_id": "act_melden",
    "callback_token": "push_abc123",
    "variables": { "betrag": 1200 }
  }
}
```

* Header `X-ProcWorks-Event: task.push`, `X-ProcWorks-Delivery: <delivery_id>` und — falls
  `secret_ref` gesetzt — `X-ProcWorks-Signature: sha256=…` (HMAC über den Rohbody).
* Die Zustellung nutzt die volle Outbox-Maschinerie: **Backoff-Retry**, **Circuit-Breaker**
  je Host, **Zustellprotokoll**.

### 4.4 Ergebnis zurückmelden (Tool)

Das Tool quittiert den Push und ruft später den **Standard**-Completion-Endpunkt mit dem
`callback_token` als `worker_id` auf:

```bash
curl -X POST https://host/v1/external-tasks/et_77/complete \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"worker_id": "push_abc123", "variables": {"ok": true}}'
```

* Die gepushte Aufgabe ist intern eine `LOCKED` External Task **ohne** Lock-Ablauf und
  **ohne** Topic — sie wird also nie versehentlich per `fetch-and-lock` gezogen.
* Jeder Schritt wird **einmal** gepusht, auch wenn mehrere Vorgänge oder API-Prozesse
  gleichzeitig Pushes auslösen: Die Aufgabe wird vor der Zustellung mit dem Callback-Token
  gesperrt, nur der Durchlauf, dem das gelingt, stellt zu. Auch die Zustellung selbst ist für
  diesen einen Durchlauf reserviert: Anfragen, die währenddessen abgeschlossen werden, stellen
  denselben Push nicht noch einmal zu. Das Tool darf deshalb schon antworten, während der
  Push-Aufruf noch läuft – es erhält den Push trotzdem nur einmal. Bricht ein API-Prozess
  zwischen Sperre und Zustellung ab, wird nach Ablauf von `request_timeout_ms` plus 60 Sekunden
  erneut gepusht (mit neuem Callback-Token und neuer `delivery_id`).
* Der Abschluss wird wie bei einer abgeholten Aufgabe protokolliert (Abschnitt 3.2); statt der
  `worker_id` steht `detail.channel = "push"` im Audit — das Callback-Token erscheint dort nie.
* Schlägt eine Zustellung fehl (Fehlerstatus oder Tool nicht erreichbar), bleibt die Aufgabe
  `CREATED` und wird nach einem Backoff beim nächsten Fortschritt (oder per `drive-push`) erneut
  gepusht – mit einem **neuen** Callback-Token; das Tool erkennt Wiederholungen an der `task_id`.
  Die Wartezeit folgt `retry_backoff_ms` wie beim Pull (Abschnitt 3.2). Jeder Push-Aufruf ist
  auf `request_timeout_ms` der Aktivität begrenzt (`0`: Standard von 5 Sekunden); antwortet das
  Tool nicht rechtzeitig, zählt das als Fehlversuch. Weil der Push beim Fortschritt eines
  Vorgangs läuft, verzögert ein langsames Tool diesen Schritt höchstens um dieses Zeitlimit.
  Jeder Fehlversuch verbraucht einen der `retry_max` Versuche der Aktivität; danach wird die
  Aufgabe zum **Inzident** (`GET /v1/incidents`, Monitoring). `resolve` reiht sie mit neuem
  Budget wieder ein. Ein Push-Fehler **blockiert oder verändert den Prozess nie**.
* Liest der Schritt vor dem Push Daten aus einem angebundenen System und schlägt das fehl,
  wird nichts zugestellt; das zählt wie beim Abholen als **Fehlversuch** (Backoff, nach
  Erschöpfung von `retry_max` Inzident).
* Ist das Push-Ziel gar nicht konfiguriert, bleibt die Aufgabe `CREATED` und wird beim nächsten
  Fortschritt erneut versucht, ohne Versuche zu verbrauchen.
* Ist die konfigurierte URL nie zustellbar (etwa Port `0`, ein Port über 65535 oder ein anderes
  Schema als `http`/`https` – dieselbe Prüfung, die beim Start eine Warnung ins Log schreibt),
  wird die Aufgabe sofort zum **Inzident** mit Verweis und Grund (z. B. `push endpoint 'erp'
  can never be delivered (WH.port)`, nie die URL). Nach Korrektur von
  `PROCWORKS_PUSH_ENDPOINTS` und Neustart reiht `resolve` sie wieder ein.
* Manuell nachstoßen (z. B. nach einem Backoff): `POST /v1/external-tasks/drive-push`
  (operator/admin) — idempotent und nebenwirkungsfrei.

> **Synchrone Push-Variante (nicht implementiert):** Eine Variante, bei der die HTTP-Antwort
> sofort `complete_activity` auslöst, würde die Zustellung zurück in den Kern koppeln und die
> Leitplanke „Kern bleibt rein“ verletzen. Sie ist als optionale Erweiterung dokumentiert.

---

## 5. Webhooks (Ereignis-Abonnements)

```bash
# Abonnement anlegen (events:subscribe / modeler / admin)
curl -X POST https://host/v1/webhooks \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"url": "https://hooks.example.com/pw",
          "events": ["instance.completed", "task.incident"],
          "secret_ref": "WH_SECRET"}'

curl https://host/v1/webhooks/$WID/deliveries -H "Authorization: Bearer $TOKEN"  # Protokoll
curl -X POST https://host/v1/webhooks/$WID/test -H "Authorization: Bearer $TOKEN" # Testping
curl -X DELETE https://host/v1/webhooks/$WID -H "Authorization: Bearer $TOKEN"

# Probelauf: zeigt Urteil, Kopfzeilen und signierten Rumpf -- sendet NICHTS
curl -X POST https://host/v1/webhooks/preview \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"url": "https://hooks.example.com/pw", "event": "task.completed",
          "secret_ref": "WH_SECRET"}'
```

Verfügbare Ereignisse: `instance.started`, `instance.completed`, `task.ready`,
`task.completed`, `task.incident`, `task.cancelled`. `instance.started` und `instance.completed` gelten für
jeden echten Vorgang, auch für Teilprozess-Kinder und Folgevorgänge, die ein anderer Vorgang
gestartet hat (endet ein solcher schon beim Start, folgen beide direkt nacheinander);
Prüfinstanzen melden nichts. Jede Zustellung ist HMAC-signiert (`secret_ref` ist ein
ENV-Variablenname) und trägt eine eindeutige `delivery_id` zur Entdoppelung.

**Zustellung: einmal je Reservierung, mindestens einmal bei Abbruch.** Vor jedem
Zustellversuch reserviert ein API-Prozess den Eintrag für die Dauer des Zeitlimits plus
60 Sekunden; nur er stellt zu, gleichzeitige Anfragen und weitere API-Prozesse überspringen
ihn. Das Zeitlimit gilt für die **ganze** Zustellung – Verbindung, Senden und das Lesen der
vollständigen Antwort zusammen: Wer die Antwort nicht innerhalb des Zeitlimits vollständig
liefert (auch wer sie byteweise tröpfeln lässt), erhält den Versuch als fehlgeschlagen
gewertet und die Zustellung später erneut. Antworten Sie deshalb zügig und verarbeiten Sie
aufwendige Dinge nach der Antwort. Ein Ereignis kommt deshalb im Normalfall genau einmal an, ein fehlgeschlagener Versuch
wird nach dem Backoff wiederholt. Bricht ein API-Prozess **während** einer Zustellung ab
(Absturz, Neustart), ist offen, ob der Empfänger sie erhalten hat: Nach Ablauf der
Reservierung wird sie erneut zugestellt, mit **derselben** `delivery_id`. Empfänger, die
keine Doppelwirkung vertragen, entdoppeln daher über die `delivery_id`. Bei mehreren
API-Prozessen setzt die Reservierung synchron laufende Uhren voraus (NTP); geht eine Uhr um
mehr als eine Minute vor, kann eine laufende Zustellung ein zweites Mal kommen – ebenfalls mit
derselben `delivery_id`. War der
abgebrochene Versuch der letzte erlaubte, wird nicht erneut zugestellt; der Eintrag endet als
fehlgeschlagen mit dem Vermerk `delivery abandoned (lease expired)`. Mit dem
**Probelauf** lässt sich der eigene Empfänger testen, bevor ein Abonnement Ereignisse
zustellt: Er liefert genau die Kopfzeilen und den Rumpf, die eine echte Zustellung
hätte, samt Signatur zum Nachprüfen, dazu Adresse und Port, an die zugestellt würde
(`resolved_address`, `resolved_port`; ohne Portangabe der Standardport, `0` bei Ablehnung).

**Schutz vor Server-Side Request Forgery (SSRF).** Ein Webhook-Ziel wird geprüft, wenn
das Abonnement angelegt wird, **und erneut bei jeder Zustellung**:

- Ist `PROCWORKS_WEBHOOK_ALLOWLIST` gesetzt (kommagetrennte Hostnamen), sind nur diese
  Hosts erlaubt. **Empfehlung für den Betrieb.**
- Ohne Allowlist sind nur Ziele erlaubt, deren Name ausschließlich auf **öffentliche**
  Adressen auflöst. Abgelehnt werden private, Loopback-, Link-Local- (etwa
  Cloud-Metadaten `169.254.169.254`), CGNAT- und reservierte Adressen – auch in
  Sonderschreibweisen (`2130706433`, `0x7f000001`, `::ffff:127.0.0.1`).
- Die Verbindung geht an **genau die geprüfte Adresse** (kein zweites Auflösen, damit
  kein DNS-Rebinding). TLS prüft das Zertifikat trotzdem gegen den Hostnamen.
- **Weiterleitungen werden nicht befolgt.** Ein 3xx gilt als fehlgeschlagene Zustellung.
- URLs mit Zugangsdaten (`https://user:pw@…`) werden abgelehnt.
- `PROCWORKS_EGRESS_DENY=1` sperrt jede ausgehende Zustellung, auch bereits
  eingereihte.

Push-Ziele (`PROCWORKS_PUSH_ENDPOINTS`) konfiguriert nur der Betreiber; sie dürfen im
internen Netz liegen, werden aber ebenfalls an die geprüfte Adresse gebunden.

---

## 6. Connectoren (Direktzugriff auf Datenspeicher)

`EXTERNAL`-Datenelemente werden zur Laufzeit über einen registrierten Connector aufgelöst
(Pre-Fetch der READ-Felder vor dem Lock, Post-Flush der WRITE-Felder vor `complete`).

Ein **Einzelwert-Rückschreiben** (SQL-Write) trifft genau einen Datensatz – sonst scheitert der
Abschluss mit `502` und einem `code`, die Aufgabe bleibt gesperrt und der Vorgang unverändert:
`DAL.no-row` (kein Datensatz zum Schlüssel), `DAL.many-rows` (mehrere, die Schlüsselspalte ist
nicht eindeutig; es wird nichts geschrieben), `DAL.row-count-unknown` (ein Connector meldet nicht,
wie viele Datensätze er geändert hat). Der Worker meldet dann `failure`; nach Erschöpfung der
Wiederholungen entsteht ein Inzident.

Dasselbe gilt beim **Lesen** vor dem Abholen (Vorab-Lesen der Eingaben): Findet der Schlüssel
mehrere Datensätze, wird die Aufgabe nicht ausgegeben und zählt als Fehlversuch; liefert
`fetch-and-lock` dadurch gar keine Aufgabe, antwortet es mit `502` und `code` `DAL.many-rows`
(Abschnitt 3.2). Nach der Korrektur des Schlüssels erhält das nächste Abholen die richtigen Daten.

```bash
curl https://host/v1/connectors -H "Authorization: Bearer $TOKEN"          # nur Metadaten
curl -X POST https://host/v1/connectors/erp/test -H "Authorization: Bearer $TOKEN"
curl https://host/v1/connectors/erp/entities -H "Authorization: Bearer $TOKEN"  # Tabellenkatalog
curl -X POST https://host/v1/connectors/erp/sample-read \
     -H "Authorization: Bearer $TOKEN" -d '{"entity": "Kunde", "limit": 5}'
```

`entities` und `columns` lesen ausschließlich den **Katalog** des Systems (keine Zeile) und
speisen die Auswahllisten der Oberfläche. `sample-read` liest nur Tabellen, die der Katalog
anbietet (oder schemaqualifizierte Namen außerhalb der Systemschemata), nie Systemtabellen.
**Systemtabellen** (`sqlite_master`, `pg_catalog.*`, `information_schema.*`, `sys.*`,
`mysql.*` …) sind überall ausgeschlossen – auch schemaqualifiziert (`main.sqlite_master`) und
in jeder Schreibweise (`PG_CATALOG.pg_authid`): `sample-read` und `columns` antworten mit `422`
und `code` `DAL.system-entity`, `entities` bietet sie nicht an, eine Datenbindung darauf wird
schon beim Modellieren und beim BPMN-Import abgelehnt (Regel `C10`, `code` `C10.system-entity`
bzw. `C10.unsafe-name`), und auch zur Laufzeit wird keine gelesen oder geschrieben.
Ein unzulässiger Tabellen- oder Spaltenname (Anführungszeichen, `;`, Kommentarzeichen,
Leerzeichen) ergibt ebenso `422` mit `code` `DAL.unsafe-name`, ein Fehler des angebundenen
Systems `502` – auch beim Verbindungstest und beim Rückschreiben, wenn das System nicht antwortet
(die Meldung enthält keine Verbindungsangaben). Als
Modellierhilfe steht es Modellierern, Administratoren und Service-Tokens mit `data:read`
offen, nicht der Bearbeiter-Rolle. Dasselbe gilt für den Verbindungstest (`test`), der eine
ausgehende Verbindung zum angebundenen System aufbaut. Ein Treiber ohne Katalog-Auskunft liefert eine leere
Liste — die Eingabe von Hand bleibt immer möglich.

Konfiguration über `PROCWORKS_CONNECTIONS` (Dateipfad oder Inline-JSON-Array). Secrets stehen
als `${ENV}`-Platzhalter darin und werden serverseitig aufgelöst — **nie** im Schema.
Zugriffe sind stets **parametrisiert**; Bezeichner werden gegen ein striktes Whitelist-Regex
geprüft (kein Injection-Risiko).

---

## 7. Endpunkt-Referenz (`/v1`)

| Methode · Pfad | Scope | Zweck |
|---|---|---|
| `POST /v1/schemas/{id}/instances` | `instances:start` | Instanz eines freigegebenen Schemas starten |
| `GET /v1/instances/{id}` | `data:read` | Instanz lesen |
| `GET /v1/instances/{id}/tasks` | `data:read` | Offene Arbeitslisten-Einträge |
| `GET /v1/instances/{id}/data` | `data:read` | Instanzdaten lesen |
| `PUT /v1/instances/{id}/data` | `data:write` | Instanzdaten schreiben (typgeprüft) |
| `POST /v1/instances/{id}/nodes/{nodeId}/complete` | `tasks:complete` | Aktivität abschließen (XOR-Zweige entscheidet die Engine automatisch, K7) |
| `POST /v1/external-tasks/fetch-and-lock` | `tasks:fetch` | External Tasks abholen + sperren |
| `POST /v1/external-tasks/{id}/complete` | `tasks:complete` | Aufgabe abschließen (auch Push-Callback) |
| `POST /v1/external-tasks/{id}/failure` | `tasks:complete` | Transienter Fehler (Backoff-Retry) |
| `POST /v1/external-tasks/{id}/bpmn-error` | `tasks:complete` | Fachlicher Fehler (wird ein Inzident) |
| `POST /v1/external-tasks/{id}/extend-lock` | `tasks:complete` | Lock verlängern |
| `POST /v1/external-tasks/{id}/unlock` | `tasks:complete` | Lock freigeben |
| `GET /v1/external-tasks/{id}` | `tasks:fetch` | Aufgabenzustand lesen |
| `POST /v1/external-tasks/drive-push` | `tasks:fetch` (operator/admin) | Aktivierte `HTTP_PUSH`-Schritte jetzt pushen |
| `GET /v1/push-endpoints` | `tasks:fetch` | Konfigurierte `HTTP_PUSH`-Referenzen (ohne URL/Secret) |
| `GET /v1/incidents` | `tasks:fetch` | Inzidente auflisten |
| `POST /v1/incidents/{id}/resolve` | `tasks:complete` (operator/admin) | Inzident lösen + Aufgabe neu einreihen |
| `GET /v1/connectors` | `data:read` | Connector-Metadaten |
| `POST /v1/connectors/{id}/test` | `data:read` (modeler/admin) | Verbindungstest |
| `POST /v1/connectors/{id}/sample-read` | `data:read` (modeler/admin) | Beispieldatensätze aus einer angebotenen Tabelle |
| `GET /v1/connectors/{id}/entities` | `data:read` | Lesbare Tabellen/Sichten (Katalog, keine Zeilen) |
| `GET /v1/connectors/{id}/columns?entity=…` | `data:read` | Spalten einer Tabelle mit Datentyp |
| `GET · POST /v1/webhooks` | `events:subscribe` | Abonnements auflisten/anlegen |
| `DELETE /v1/webhooks/{id}` | `events:subscribe` | Abonnement löschen; noch nicht zugestellte Ereignisse des Abos werden verworfen (eine gerade laufende Zustellung endet noch, danach geht nichts mehr an das Ziel) |
| `POST /v1/webhooks/{id}/test` | `events:subscribe` | Testzustellung |
| `POST /v1/webhooks/preview` | `events:subscribe` | Probelauf: Urteil + Anfrage, ohne Senden |
| `GET /v1/webhooks/{id}/deliveries` | `events:subscribe` | Zustellprotokoll |

Die maschinenlesbare Spezifikation (Parameter, Schemata, Beispiele) liefert `/openapi.json`
bzw. die Swagger-UI unter `/docs`.

---

## 8. Robustheit & Sicherheit auf einen Blick

- **Idempotenz:** Start, Abschluss, Datensetzen und Melden externer Aufgaben akzeptieren
  `Idempotency-Key` (eine Wiederholung führt nichts erneut aus und zeigt den aktuellen Stand
  des Objekts mit demselben Status, je Identität, gebunden an Endpunkt und Inhalt; Wiederverwendung für eine andere Anfrage → `409 IDEM.key-reused`). Mit
  Datenbank ist der Schlüssel dort reserviert: Er gilt **über Neustarts und mehrere
  API-Prozesse** hinweg, 24 Stunden lang (Einzelheiten in Abschnitt 2.1).
- **Genau einmal je Sperrfrist:** External-Task-Lock + Worker-Bindung verhindern, dass ein
  Abschluss doppelt angewendet wird; je Schritt höchstens eine offene Aufgabe, jede innerhalb
  ihrer Sperrfrist an genau einen Worker ausgegeben – auch bei parallelen Abrufen über mehrere
  API-Prozesse (Abschnitt 3.2). Nach Ablauf einer Sperre kann ein anderer Worker die Aufgabe
  erhalten; die Arbeit im Worker ist dann nicht mehr „genau einmal“.
- **Zustellung einmal je Reservierung, mindestens einmal bei Abbruch:** transaktionale
  Outbox + Reservierung vor jedem Versuch + Backoff-Retry + Circuit-Breaker; nur nach einem
  Abbruch während der Zustellung kommt ein Ereignis erneut an, mit derselben `delivery_id`
  (Abschnitt 5). Modellierte E-Mails folgen derselben Regel (Wiederholung mit derselben
  `Message-ID`).
- **Keine Secrets im Modell:** Connector-/Push-/Webhook-Secrets nur serverseitig (`${ENV}`/`secret_ref`).
- **SSRF-Schutz:** Webhook-Ziele gegen Allowlist bzw. nur öffentliche Adressen, geprüft bei
  jeder Zustellung, Verbindung an die geprüfte Adresse, keine Weiterleitungen (siehe Abschnitt 5 dieses Leitfadens); Push-Ziele
  nur vom Betreiber konfigurierbar.
- **Kein Injection:** ausschließlich parametrisierte DAL-Zugriffe; whitelisted Bezeichner.
- **Kern bleibt rein:** die Integrationsschicht treibt den Kern über bestehende Operationen;
  ein Integrationsfehler kann ein gültiges Schema oder eine laufende Instanz **nie** beschädigen.

---

© Tobias Häcker. Lizenz: BUSL-1.1 (siehe [LICENSE](../LICENSE)).
