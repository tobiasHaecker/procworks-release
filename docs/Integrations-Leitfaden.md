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

Token-Modus aktivieren und Tokens als JSON-Datei hinterlegen (nur SHA-256-Digests werden
gespeichert):

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

> Im Standard (`PROCWORKS_AUTH=open`) sind keine Header nötig — praktisch für lokale Tests,
> **nicht** für den Produktivbetrieb.

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
* Der **`Idempotency-Key`** macht einen wiederholten Start einmal-wirksam (gleiche Antwort).

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

# 2c) Fachlicher Fehler -> BPMN-Error-Pfad
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

**Garantien:** Lock + Worker-Bindung ⇒ **Exactly-once**-Anwendung; abgelaufene Locks werden
neu vergeben; Wiederholungen mit exponentiellem **Backoff**; nach Erschöpfung → **Inzident**.

### 3.3 Inzidente

```bash
curl "https://host/v1/incidents?unresolved_only=true" -H "Authorization: Bearer $TOKEN"
# Aufgabe nach Behebung erneut einreihen (operator/admin):
curl -X POST https://host/v1/incidents/inc_9/resolve -H "Authorization: Bearer $TOKEN"
```

---

## 4. Outbound – Push (`HTTP_PUSH`)

Statt dass ein Worker zieht, **pusht** ProcWorks das Eingabe-Datenpaket aktiv an ein
serverseitig konfiguriertes Tool-Endpoint. Das ist die *asynchrone* Variante aus
Konzept §6.3: Der Push trägt ein **Callback-Token**; das Tool quittiert und meldet das
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
* Schlägt eine Zustellung fehl, bleibt die Aufgabe `CREATED` und wird beim nächsten
  Fortschritt erneut gepusht. Ein Push-Fehler **blockiert oder verändert den Prozess nie**.
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
```

Verfügbare Ereignisse: `instance.started`, `instance.completed`, `task.ready`,
`task.completed`, `task.incident`. Jede Zustellung ist HMAC-signiert (`secret_ref` ist ein
ENV-Variablenname) und trägt eine eindeutige `delivery_id` zur Entdoppelung. Ziele werden
gegen die **SSRF-Allowlist** `PROCWORKS_WEBHOOK_ALLOWLIST` geprüft.

---

## 6. Connectoren (Direktzugriff auf Datenspeicher)

`EXTERNAL`-Datenelemente werden zur Laufzeit über einen registrierten Connector aufgelöst
(Pre-Fetch der READ-Felder vor dem Lock, Post-Flush der WRITE-Felder vor `complete`).

```bash
curl https://host/v1/connectors -H "Authorization: Bearer $TOKEN"          # nur Metadaten
curl -X POST https://host/v1/connectors/erp/test -H "Authorization: Bearer $TOKEN"
curl -X POST https://host/v1/connectors/erp/sample-read \
     -H "Authorization: Bearer $TOKEN" -d '{"entity": "Kunde", "limit": 5}'
```

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
| `POST /v1/external-tasks/{id}/bpmn-error` | `tasks:complete` | Fachlicher Fehler (BPMN-Error-Pfad) |
| `POST /v1/external-tasks/{id}/extend-lock` | `tasks:complete` | Lock verlängern |
| `POST /v1/external-tasks/{id}/unlock` | `tasks:complete` | Lock freigeben |
| `GET /v1/external-tasks/{id}` | `tasks:fetch` | Aufgabenzustand lesen |
| `POST /v1/external-tasks/drive-push` | `tasks:fetch` (operator/admin) | Aktivierte `HTTP_PUSH`-Schritte jetzt pushen |
| `GET /v1/push-endpoints` | `tasks:fetch` | Konfigurierte `HTTP_PUSH`-Referenzen (ohne URL/Secret) |
| `GET /v1/incidents` | `tasks:fetch` | Inzidente auflisten |
| `POST /v1/incidents/{id}/resolve` | `tasks:complete` (operator/admin) | Inzident lösen + Aufgabe neu einreihen |
| `GET /v1/connectors` | `data:read` | Connector-Metadaten |
| `POST /v1/connectors/{id}/test` | `data:read` | Verbindungstest |
| `POST /v1/connectors/{id}/sample-read` | `data:read` | Beispieldatensätze |
| `GET · POST /v1/webhooks` | `events:subscribe` | Abonnements auflisten/anlegen |
| `DELETE /v1/webhooks/{id}` | `events:subscribe` | Abonnement löschen |
| `POST /v1/webhooks/{id}/test` | `events:subscribe` | Testzustellung |
| `GET /v1/webhooks/{id}/deliveries` | `events:subscribe` | Zustellprotokoll |

Die maschinenlesbare Spezifikation (Parameter, Schemata, Beispiele) liefert `/openapi.json`
bzw. die Swagger-UI unter `/docs`.

---

## 8. Robustheit & Sicherheit auf einen Blick

- **Idempotenz:** mutierende `/v1`-Aufrufe akzeptieren `Idempotency-Key` (Erfolg wird gecacht).
- **Exactly-once:** External-Task-Lock + Worker-Bindung verhindern Doppelanwendung.
- **At-least-once-Zustellung:** transaktionale Outbox + Backoff-Retry + Circuit-Breaker.
- **Keine Secrets im Modell:** Connector-/Push-/Webhook-Secrets nur serverseitig (`${ENV}`/`secret_ref`).
- **SSRF-Schutz:** Webhook-Ziele gegen Allowlist; Push-Ziele nur vom Betreiber konfigurierbar.
- **Kein Injection:** ausschließlich parametrisierte DAL-Zugriffe; whitelisted Bezeichner.
- **Kern bleibt rein:** die Integrationsschicht treibt den Kern über bestehende Operationen;
  ein Integrationsfehler kann ein gültiges Schema oder eine laufende Instanz **nie** beschädigen.

---

© Tobias Häcker. Lizenz: BUSL-1.1 (siehe [LICENSE](../LICENSE)).
