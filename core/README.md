# ProcWorks — Headless Process Engine Kernel

Walking Skeleton des Backend-Kerns (Roadmap-Schritte 0–11 mit Activity
Repository, Daten-Connectoren, BPMN-Import/Export und schlankem Web-Client,
Abschnitt 13 des Architektur-Konzepts). Demonstriert
**Correctness by Construction (CbC)**:
Ein Prozessschema kann ausschließlich über geprüfte High-Level-Operationen
verändert werden; jede Operation validiert das Ergebnis **vor** dem Commit
gegen die Strukturregeln K1–K3. Ein inkorrektes Modell kann nicht entstehen.

## Umfang dieses Skeletons

- **Meta-Modell** (`model.py`): block-strukturiertes Schema mit Status-Lebenszyklus.
- **Correctness Validator** (`validator.py`): K1 (balancierte Gateways), K2
  (genau ein Start/Ende, korrekte Knotengrade), K3 (Erreichbarkeit, keine
  Sackgassen); Datenfluss D1–D4: D1 (Schreiben-vor-Lesen auf allen
  Pfaden, Must-Analyse über AND-/XOR-Semantik), D2 (keine konkurrierenden
  Schreibzugriffe in parallelen AND-Zweigen), D3 (Typkonformität), D4
  (Datenzugriffe nur auf Aktivitäten, nur auf existierende Elemente); Ressourcen
  Z1–Z4: Z1 (wohlgeformte BZR + existierende Org-Referenzen), Z2 (Regel ist
  auflösbar / nicht-leer), Z3 (`NodePerformingAgent`-Rückbezüge sind
  garantiert-vorher), Z4 (Dienst-/BZR-Konsistenz: automatische Schritte ohne BZR);
  Activity Repository A1-A3: A1 (gebundene Vorlage existiert), A2 (das
  `automatic`-Flag passt zum Executor der Vorlage), A3 (typkonforme Bindung der
  Vorlagen-Schnittstelle: jede Pflicht-Parameter ist gemappt, gemappte Namen
  gehören zur Vorlage, gemappte Datenelemente existieren und sind typgleich);
  Komposition H1-H4 (Sub-Prozesse) und F1-F4 (Folgeprozesse): H1 (Ziel ist
  RELEASED, gepinnte Version), H2 (typkonformes I/O-Mapping **und**
  Datenübergabe-Soundness: ein gemappter Output muss vom Sub-Schema auf jedem
  Pfad geschrieben werden, und die Output-Zuordnung zählt im Elternprozess als
  garantierte Schreibung für D1/D2), H3 (azyklische
  Hierarchie), H4 (gepinnte Version immutable), F1 (Zielexistenz/RELEASED), F2
  (typkonformes Handover-Mapping), F3 (Entkopplung ASYNC), F4 (eine
  `CONDITIONAL`-Verkettung hat eine wohlgeformte Bedingung, die nur existierende
  Datenelemente liest). Connectoren C1-C3 (externe Daten): C1 (ein `EXTERNAL`-
  Datenelement bindet an einen registrierten Connector; ein `INSTANCE`-Element
  trägt keine Bindung), C2 (der Schlüssel verweist auf ein existierendes
  `INSTANCE`-Element und nicht auf sich selbst), C3 (die gebundene Entität ist
  nicht leer). Strukturierte SQL-Skizzen C4-C9 (typ- und kardinalitätssichere
  **Skalar**-Bindung, ohne Freitext-SQL): C4/C7 (der Projektions- bzw.
  Zielspaltentyp passt zum Datenelement), C5/C8 (jeder Filter ist wohlgeformt,
  typkonform gegen ein `INSTANCE`-Quellelement und auf jedem Pfad vor dem Lesen/
  Schreiben versorgt), C6/C9 (das `SELECT` liefert bzw. das `UPDATE` trifft
  **höchstens eine** Zeile). Die schema-
  übergreifenden Regeln nutzen einen **Resolver**.
- **Change Operations** (`operations.py`): `serial_insert`, `parallel_insert`
  (AND-Block), `conditional_insert` (XOR-Block), `add_data_element`,
  `update_data_element` (umbenennen / Typ ändern),
  `delete_data_element` (mit Aufräumen abhängiger Bindungen/Maskenfelder),
  `reset_data_element_source` (externe Bindung entfernen, wieder `INSTANCE`),
  `connect_data` (Lese-/Schreibkante), `add_role`/`add_org_unit`/`add_agent`
  (Org-Modell), `assign_service`, `assign_staff_rule` (BZR),
  `add_activity_template` (Activity Repository: wiederverwendbare Vorlage mit
  typisierter I/O-Schnittstelle + Executor),
  `register_connector`/`bind_external_data` (externe Daten: Connector
  registrieren bzw. ein Datenelement als `EXTERNAL` an eine Connector-Entität
  binden),
  `bind_sql_select`/`bind_sql_write` (typ- und kardinalitätssichere
  **Skalar**-Bindung: ein Datenelement wird per strukturiertem, deterministisch
  kompiliertem `SELECT` gefüllt bzw. per `UPDATE` zurückgeschrieben — C4–C9,
  kein Freitext-SQL),
  `insert_subprocess`/`set_subprocess_mapping` (Sub-Prozess),
  `convert_activity_to_subprocess` (eine Aktivität in einen an ein Submodell
  gebundenen `SUBPROCESS` umwandeln), `set_subprocess_binding` (Bindung eines
  bestehenden Sub-Prozess-Knotens ändern), `set_library_subprocess` (ein
  freigegebenes Schema als wiederverwendbares Submodell für die Bibliothek
  markieren),
  `link_follow_up`/`unlink_follow_up` (Folgeprozess),
  `link_org_model`/`unlink_org_model` (geteilte Organisation verknüpfen/lösen),
  `new_revision`
  (neue Schema-Revision für die Migration, behält alle Element-IDs), `release`.
- **Shared Org** (`org.py` + `store.py`): geteilte, modellübergreifende
  Organisationsmodelle als eigenständige Stammdaten-Entität mit eigenen
  Operationen (`create_org_model`, `org_add_*`, `org_set_*`, `org_update_agent`)
  und Validierung (`validate_org`); per **Hydration** an der API-Grenze in
  referenzierende Schemata eingespielt.
- **Execution Engine** (`execution.py`): instanziiert ein **freigegebenes**
  Schema (`instantiate`) und führt es über die ADEPT-Knoten-/Kantenmarkierung
  aus — Knotenmarkierung NS (`NOT_ACTIVATED`?`ACTIVATED`?`RUNNING`?`COMPLETED`
  bzw. `SKIPPED`), Kantenmarkierung ES (`TRUE_SIGNALED`/`FALSE_SIGNALED`).
  Gateways/Start laufen automatisch, Aktivitäten warten auf `start_activity`/
  `complete_activity`, **XOR-Splits entscheiden automatisch** anhand der
  strukturierten Partition (K7) über ihren Diskriminator-Wert; `worklist`
  liefert die bereiten Schritte. Unter jeder erreichbaren Endmarkierung ist
  jeder Knoten
  `COMPLETED` oder `SKIPPED`. Ein `SUBPROCESS`-Knoten erzeugt mit einem
  `ExecutionContext` (Resolver + Instanz-Store) eine **Kind-Instanz** seines
  gepinnten Zielschemas, übergibt die gemappten Eingabedaten, bleibt `RUNNING`
  während das Kind läuft und schreibt bei dessen Abschluss die gemappte Ausgabe
  in den Elternprozess zurück (ohne Kontext bleibt er eine Blackbox). Schließt
  eine Instanz ab, startet jeder Folgeprozess-Link, dessen Trigger feuert, eine
  neue Instanz des Zielprozesses, versorgt mit den per Handover gemappten Daten.
  Ein `ON_COMPLETE`-Trigger feuert immer, ein `CONDITIONAL`-Trigger nur, wenn
  seine Bedingung gegen die Instanzdaten wahr ist (ausgewertet durch einen
  sicheren Ausdrucks-Evaluator, `conditions.py`, ohne `eval`). Die Kopplung
  bestimmt die Verbindung: `ASYNC` startet eine vollständig entkoppelte
  Top-Level-Instanz (keine Rückverweisung, F3), `SYNC` startet eine gekoppelte
  Instanz, die die Ursprungs-Instanz-ID für die Nachverfolgung vermerkt.
- **Headless API** (`api.py`, FastAPI): einzige Eintrittstür zum Kern; identisch
  für GUI, CLI und Fremdsysteme (Abschnitt 5.4, API-first). Eine permissive
  CORS-Middleware erlaubt dem Browser-Client den Zugriff (im lokalen Betrieb
  unbedenklich, da der Client keine Korrektheitslogik trägt).
- **Web-Client** (`../web/`): ein schlanker **No-Build**-Web-Client (reines
  HTML/CSS/JavaScript, kein npm/Bundler) als dünne GUI über der API. Acht
  Sichten — Modellieren (geführte +-Operationen, live validiert; Knoten
  umbenennen/entfernen, Auswahl wird zentriert),
  Datensicht, Ressourcensicht (Organisation als Baumstruktur mit Abteilungen,
  Vorgesetzten und Umhängen-Dialog; Agenten samt Vertreter in eigener Tabelle;
  zusätzlich ein Organigramm der Abteilungs-Hierarchie, dessen Klick die
  gewählte Abteilung samt zugehöriger Agenten inkl. Vorgesetztem hervorhebt),
  Ausführung (Worklist + Live-Prozesslandkarte), Meine Aufgaben
  (Bearbeiter-Aufgabenliste), Monitoring, Integration (Connector-Registry,
  Datenanbindungs-Assistent, Automatik-Binding, Webhook-Panel, Inzident-Liste)
  und Hilfe (Kurzübersicht aller Sichten, Schnellstart je Rolle, Glossar der
  Regel-Codes).
  Jede Änderung läuft über den
  Validate-before-Commit-Pfad des Kerns; die GUI trifft **keine**
  Korrektheitsentscheidung (Abschnitt 5.4/8.3).
- **Persistenz** (`db.py`, `store.py`): austauschbarer Store für Schemata,
  Instanzen, das Event-Log **und** geteilte Organisationsmodelle. Ohne
  Konfiguration in-memory; mit
  `DATABASE_URL` PostgreSQL (SQLAlchemy 2.0, JSONB-Dokument je Schema bzw.
  Instanz, eine Zeile je Audit-Event, eine Zeile je geteilter Organisation).
  Schema-, Instanz-, Audit- und Org-Tabellen via
  Alembic (`0001`/`0002`/`0003`/`0004`). Instanzen und Audit-Verlauf sind damit durabel
  und überleben einen Neustart.
- **Ad-hoc-Änderungen** (`adhoc.py`): passen eine **einzelne** laufende Instanz
  über eine instanzeigene Schema-Variante (`ad_hoc_schema`) an, ohne das
  freigegebene Schema zu berühren. `adhoc_insert_activity`/`adhoc_delete_node`
  prüfen R1 (Zustandsverträglichkeit: nur die noch nicht ausgeführte Region
  darf sich ändern) und R2 (Korrektheitserhalt: die Variante erfüllt weiterhin
  alle Struktur-/Datenflussregeln, validate-before-commit). Die Execution Engine
  läuft anschließend nahtlos gegen die Variante weiter (ausgeführte Knoten
  behalten ihre IDs).
- **Schema-Evolution + Instanzmigration** (`migration.py`): `check_migration`/
  `migrate_instance` prüfen, ob eine laufende Instanz auf eine neue Revision
  umgezogen werden kann — M1 (Ziel ist korrekt + RELEASED), M2 (ausgeführte
  Region — Knoten und interne Kanten — unverändert), M3 (Markierungen bilden
  sauber ab: abgeschlossene Knoten behalten ihre Nachfolger, laufende bleiben
  ausführbar), M4 (Pflichtdaten der ausgeführten Region verfügbar, sonst via
  `data_mapping` ergänzen), M5 (ad-hoc geänderte Instanzen werden konservativ
  blockiert — manuelle Auflösung nötig). `build_migration_report` liefert den
  Befund je Instanz für eine ganze Release-Bestandsaufnahme.

- **Daten-Connectoren** (`dal.py`): ein **Data Access Layer** mit schmaler
  Connector-SPI (`read`/`write`/`query`) homogenisiert den Zugriff auf externe
  Systeme (MS SQL, MySQL, Dynamics 365, SAP, plus offene `CUSTOM`-SPI). Ein als
  `EXTERNAL` markiertes Datenelement wird zur Laufzeit über den gebundenen
  Connector aufgelöst; Schlüssel und Werte werden stets **parametrisiert**
  übergeben (kein String-Concat ? kein Injection-Risiko), Zugangsdaten liegen
  nur serverseitig im Connector, nie im Schema. `InMemoryConnector` ist die
  Referenz-Implementierung für Tests/Demos. Über die Record-Bindung hinaus gibt
  es eine **typ- und kardinalitätssichere Skalar-Bindung** (Correctness by
  Construction auch für die SQL-Erzeugung): der Modellierer beschreibt eine
  strukturierte Skizze (Entität, eine projizierte Spalte, strukturierte Filter,
  Kardinalitäts-Garantie), aus der ein **deterministischer, parametrisierter**
  `SELECT`/`UPDATE` kompiliert wird (`compile_select`/`compile_update`); die
  Regeln C4–C9 stellen sicher, dass das Ergebnis **typgleich** zum Datenelement
  ist und **höchstens eine** Zeile trifft. Derselbe strukturierte Zugriff wird
  vom **OData-v4-Connector** (`odata.py`, Dynamics 365 / SAP Gateway) über
  `$select`/`$filter`/`$orderby`/`$top`/`$count`/`$apply` bzw. einen keyed
  `PATCH` bedient — **dieselbe SPI**, sodass Kern, Regeln und GUI unverändert
  bleiben. Reale Verbindungen (SQLAlchemy-URL bzw. OData-Service + Bearer-Token)
  liegen serverseitig in der Connection-Registry (`connections.py`).

- **Offene Integrationsschicht** (`/v1`-Router in `api.py`, `integration_runtime.py`,
  `outbox.py`, `connections.py`): eine versionierte, maschinen-authentifizierte
  API zur Anbindung fremder Tools. Eine **Maschinen-Rolle `integration` mit
  Scopes** (`instances:start`, `tasks:fetch`, `tasks:complete`, `data:read`,
  `data:write`, `events:subscribe`, Wildcard `*`) sichert jeden `/v1`-Endpunkt
  zusätzlich zu den Personen-Rollen; mutierende Aufrufe akzeptieren einen
  `Idempotency-Key`-Header (Erfolg wird gecacht). Vier Bausteine:
  - **Inbound**: `POST /v1/schemas/{id}/instances` (nur freigegeben),
    `…/nodes/{nodeId}/complete`, `GET`/`PUT /v1/instances/{id}/data`
    (typgeprüft), `GET /v1/instances/{id}` / `…/tasks`.
  - **External-Task-Pull** (`integration_runtime.py`): aktivierte automatische
    `EXTERNAL_TASK`-Aktivitäten werden zu Aufgaben; ein Worker holt sie per
    `POST /v1/external-tasks/fetch-and-lock` ab und meldet über
    `…/{id}/complete|failure|bpmn-error|extend-lock|unlock` zurück
    (Lock/Backoff/Inzident, Exactly-once). `GET /v1/incidents` +
    `POST /v1/incidents/{id}/resolve`.
  - **HTTP-Push** (`HTTP_PUSH`): bei Aktivierung pusht der Outbox-Dispatcher das
    Eingabe-Datenpaket an ein serverseitig konfiguriertes Tool-Endpoint
    (`PROCWORKS_PUSH_ENDPOINTS` = Dateipfad oder Inline-JSON
    `{ref: {url, secret_ref?}}`). Der Push trägt ein **Callback-Token**; das Tool
    meldet das Ergebnis über den regulären `…/complete`-Endpunkt zurück.
    `GET /v1/push-endpoints` listet die Referenzen (ohne URL/Secret),
    `POST /v1/external-tasks/drive-push` stößt einen Drive manuell an. Push-Fehler
    blockieren oder beschädigen den Prozess nie.
  - **Webhooks** (`outbox.py`): `GET`/`POST /v1/webhooks`, `DELETE …/{id}`,
    `POST …/{id}/test`, `GET …/{id}/deliveries`. Signierte (HMAC) Zustellung
    über eine transaktionale Outbox mit Backoff-Retry, Circuit-Breaker und
    SSRF-Allowlist (`PROCWORKS_WEBHOOK_ALLOWLIST`).

  Connectoren werden über `PROCWORKS_CONNECTIONS` (Dateipfad oder
  Inline-JSON-Array) konfiguriert; `GET /v1/connectors`,
  `POST /v1/connectors/{id}/test` und `…/sample-read` decken Metadaten,
  Verbindungstest und Beispiellese ab. Secrets stehen überall nur als
  `${ENV}`/`secret_ref` serverseitig, nie im Schema. Vollständige Rezepte und
  Endpunkt-Referenz: [../docs/Integrations-Leitfaden.md](../docs/Integrations-Leitfaden.md).

- **BPMN-Import/Export** (`bpmn.py`): exportiert ein Schema als semantisches
  **BPMN 2.0**-Dokument (Start/Ende ? Events, Aktivität ? `task`,
  Sub-Prozess ? `callActivity`, AND ? `parallelGateway`, XOR ?
  `exclusiveGateway`, Bedingungen als `conditionExpression`) und liest BPMN
  zurück auf die geprüfte Block-Teilsprache. Der Import folgt dem
  **No-Bypass-Prinzip**: das gemappte Modell wird vor der Rückgabe gegen die
  Korrektheitsregeln validiert, ein nicht block-strukturierter BPMN-Graph kann
  also nie zu einem gespeicherten, inkorrekten Modell werden. Konstrukte
  außerhalb der Teilsprache (z. B. `inclusiveGateway`) oder Gateways, die weder
  reiner Split noch reiner Join sind, werden mit `BpmnError` abgelehnt; der
  Split-/Join-Typ wird über den Knotengrad erschlossen. Es wird die Semantik
  übertragen, keine Diagramm-/Layout-Information (DI).

- **Monitoring/Audit + Process Mining** (`audit.py`): ein append-only
  **Event-Log** (`AuditLog`; `InMemoryAuditLog` bzw. durabel `SqlAlchemyAuditLog`)
  hält die Laufzeithistorie (Instanz erstellt/abgeschlossen, Aktivität
  gestartet/abgeschlossen, Zweig entschieden, Ad-hoc, Migration). Aufgezeichnet
  wird **an der API-Grenze**, der Ausführungskern bleibt rein. Aus der Historie
  werden **KPIs** (`compute_kpis`: laufend/abgeschlossen, Ø Durchlaufzeit, je
  Aktivität Abschlüsse + Ø Dauer als Engpass-Signal), die **Audit-Timeline**
  einer Instanz (`instance_timeline`) und eine entdeckte **Prozesskarte**
  (`discover_process_map`, ein Directly-follows-Graph als leichtes Process
  Mining) abgeleitet. Mit `DATABASE_URL` landet jedes Event durabel in der
  Tabelle `audit_event` und überlebt einen Neustart.

- **Deployment** (`Dockerfile`, [`../deploy/`](../deploy/)): der API-Server läuft
  als schlankes, **zustandsloses** Container-Image (Migrationen beim Start, dann
  Uvicorn); der Web-Client wird zusammen mit **Caddy** als Reverse Proxy
  ausgeliefert (TLS via Let's Encrypt, `/api/*` wird an die API
  weitergereicht). Ein **docker-compose**-Full-Stack
  ([`../deploy/docker-compose.full.yml`](../deploy/docker-compose.full.yml))
  und ein **Helm-Chart** ([`../deploy/helm/`](../deploy/helm/)) bringen
  PostgreSQL + API + Web zusammen; GitHub Actions baut und scannt die Images
  (Trivy) und veröffentlicht sie bei einem Versions-Tag nach ghcr.io.

## Setup & Tests

```powershell
# Abhängigkeiten (in das vorhandene .venv des Repos)
..\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# Tests
..\.venv\Scripts\python.exe -m pytest -q
```

## API starten

```powershell
$env:PYTHONPATH="src"
..\.venv\Scripts\python.exe -m uvicorn procworks.api:app --reload
```

Interaktive Dokumentation (OpenAPI/Swagger) danach unter
<http://127.0.0.1:8000/docs>.

### Authentifizierung & Rollen (optional)

Standardmäßig läuft die API im **offenen Entwicklungsmodus** ohne Login: jeder
Aufruf erhält alle Rollen (`admin`, `modeler`, `operator`, `viewer`). Die Rollen
staffeln sich grob so: `admin` darf alles; der **Modellierer** (`modeler`)
modelliert *und* ist zugleich Bearbeiter (erledigt Aufgaben über „Meine
Aufgaben", führt Instanzen aus und darf eigene **Entwürfe als Test-Instanz**
starten); `operator` startet/bearbeitet nur **freigegebene** Instanzen und liest
das Monitoring; `viewer` ist rein lesend (kein Instanzstart). Für einen
geschützten Betrieb wird ein Token-Backend aktiviert:

```powershell
$env:PROCWORKS_AUTH="token"
$env:PROCWORKS_TOKENS="C:\pfad\zu\tokens.json"
$env:PROCWORKS_CORS_ORIGINS="https://app.example.com"
```

Die Token-Datei ordnet Bearer-Tokens einer Identität samt Rollen und – für
Bearbeiter – einer gebundenen `agent_id` zu:

```json
{
  "geheimes-token-erika": {
    "subject": "erika",
    "agent_id": "a1",
    "roles": ["operator"],
    "display_name": "Erika (Bearbeiterin)"
  },
  "geheimes-token-admin": { "subject": "ada", "roles": ["admin"] }
}
```

Clients senden `Authorization: Bearer <token>`. `GET /auth/me` liefert die
verifizierte Identität, `GET /me/tasks` die eigene Arbeitsliste der angemeldeten
Person. Die handelnde Bearbeiter-Identität wird bei `complete`/`decide` aus dem
Token abgeleitet (Impersonation-Schutz); die feingranulare BZR-Eignungsprüfung
im Kern bleibt unverändert aktiv.

#### Passwort-Login ohne externen IdP

Für eigenständige Deployments gibt es ein selbstständiges Passwort-Login
(`PROCWORKS_AUTH=password`). Zugangsdaten liegen in einem separaten
`CredentialStore` (bei gesetzter `DATABASE_URL` persistent, sonst im Speicher) –
getrennt vom Agenten-/Org-Modell. Damit überhaupt jemand Nutzer anlegen kann,
wird beim Start ein Initial-Admin provisioniert. Ohne die folgenden Variablen
legt der Server bei leerem Store automatisch ein `admin`-Konto an und schreibt
dessen zufälliges Einmal-Passwort einmalig ins Server-Log (dort ablesen, beim
ersten Login ändern); alternativ fest vorgeben:

```powershell
$env:PROCWORKS_AUTH="password"
$env:PROCWORKS_ADMIN_LOGIN="admin"
$env:PROCWORKS_ADMIN_PASSWORD="bitte-aendern"
$env:PROCWORKS_SESSION_TTL_MINUTES="720"   # optional, Default 12 h
```

Der Login-Name wird aus dem Agentennamen vorgeschlagen (`vorname.nachname`).
Beim ersten Login verlangt die Login-Seite ein eigenes Passwort (min. 8 Zeichen);
danach ist man direkt angemeldet. Passwörter werden mit `hashlib.scrypt`
gesalzen gehasht (keine zusätzliche Abhängigkeit), Sessions sind opake
Bearer-Token. Admin-Verwaltung über `POST /users`,
`POST /users/{login}/reset-password` und `DELETE /users/{login}`. Im Web-Client
bietet die Ressourcensicht je Agent einen Button „Login" (nur Admin), der genau
diese Provisionierung auslöst und das Initialpasswort einmalig anzeigt.

## Web-Client starten

Der Web-Client unter [`../web/`](../web/) ist ein reiner No-Build-Client und
braucht keinen Bundler. Bei laufender API genügt ein statischer Server:

```powershell
# in einem zweiten Terminal, im Repo-Wurzelverzeichnis
.venv\Scripts\python.exe -m http.server 5500 --directory web
```

Danach <http://127.0.0.1:5500> öffnen. Die API-Basis (Standard
`http://127.0.0.1:8000`) lässt sich unten links anpassen; der Status zeigt
`verbunden`, sobald `/health` erreichbar ist, und darunter die vom Kern
gemeldete **Softwareversion**. Diese stammt aus einer einzigen Quelle – den
Paket-Metadaten (`procworks.__version__`, gepflegt in `pyproject.toml` bzw. dem
Release-Tag): `/health` liefert sie mit (`{"status":"ok","version":…}`) und die
OpenAPI-Beschreibung trägt sie ebenfalls. Alternativ lässt sich
`web/index.html` direkt öffnen (die CORS-Middleware erlaubt den Zugriff).

### Beispiel-Ablauf (headless)

```text
POST /schemas                      { "name": "Urlaubsantrag" }      -> START->END
POST /schemas/{id}/serial-insert   { "label": "Antrag prüfen", "after_node_id": "start" }
POST /schemas/{id}/conditional-insert
     { "after_node_id": "start",
       "branches": [ { "condition": "betrag > 1000", "label": "Freigabe Leitung" },
                     { "condition": "betrag <= 1000", "label": "Freigabe Team" } ] }
GET  /schemas/{id}/validation       -> { "correct": true, "findings": [] }
GET  /schemas/{id}/metrics          -> { "metrics": {...}, "hints": [...], "value_classes": {...} }  (lesend, 7PMG)
PATCH  /schemas/{id}/nodes/{nodeId} { "label": "Antrag fachlich prüfen" }  -> Aktivität umbenennen
DELETE /schemas/{id}/nodes/{nodeId}                                          -> Knoten/Block entfernen
POST /schemas/{id}/value-class      { "node_id": "<act>", "value_class": "VALUE_ADDING" }  -> Wertschöpfung (E3)
POST /schemas/{id}/priority         { "node_id": "<act>", "priority": { "impact": "HIGH", "urgency": "HIGH" } }  -> Priorität (E8)
POST /schemas/{id}/time-constraint  { "node_id": "<act>", "constraint": { "max_duration_seconds": 3600 } }  -> Dauer (E5)
POST /schemas/{id}/deadline         { "deadline_seconds": 86400 }  -> Frist; kritischer Pfad ≤ Frist (T2)
POST /schemas/{id}/release          -> lifecycle_state = RELEASED (immutable)
```

Datenfluss (D1–D4) wird identisch geprüft:

```text
POST   /schemas/{id}/data-elements    { "name": "betrag", "data_type": "FLOAT", "element_id": "betrag" }
PATCH  /schemas/{id}/data-elements/betrag  { "name": "Betrag", "data_type": "FLOAT" }   # umbenennen / Typ ändern
DELETE /schemas/{id}/data-elements/betrag                                               # löschen (räumt Bindungen auf)
POST   /schemas/{id}/data-access      { "node_id": "<writer>", "element_id": "betrag", "mode": "WRITE" }
POST   /schemas/{id}/data-access      { "node_id": "<reader>", "element_id": "betrag", "mode": "READ" }
```

Ein Lesezugriff, der nicht auf allen Pfaden zuvor geschrieben wurde (D1), ein
konkurrierender Schreibzugriff in parallelen AND-Zweigen (D2) oder ein
Typkonflikt (D3) wird mit **HTTP 422** abgelehnt.

Ressourcen-/Bearbeiterzuordnung (Z1–Z4) folgt demselben Muster:

```text
POST /schemas/{id}/roles            { "name": "Sachbearbeiter", "role_id": "sb" }
POST /schemas/{id}/agents           { "name": "Erika", "role_ids": ["sb"], "agent_id": "a1" }
POST /schemas/{id}/staff-rule       { "node_id": "<act>", "rule": { "kind": "ROLE", "ref": "sb" } }
POST /schemas/{id}/activity-templates { "name": "Pruefen", "executor": "SERVICE", "inputs": [{ "name": "wert", "data_type": "FLOAT" }], "template_id": "t1" }
POST /schemas/{id}/service          { "node_id": "<act>", "name": "Pruefen", "template_id": "t1", "parameter_mapping": { "wert": "betrag" } }
```

Eine Bearbeiterregel (BZR) ist ein strukturierter Ausdrucksbaum
(`ROLE`/`ORG_UNIT`/`NODE_PERFORMING_AGENT` als Blätter, `AND`/`OR`/`EXCEPT` als
Verknüpfungen). Verweise auf unbekannte Org-Elemente (Z1), nicht auflösbare
Regeln (Z2) oder Rückbezüge auf nicht garantiert vorher ausgeführte Schritte
(Z3) werden mit **HTTP 422** abgelehnt. Eine `ORG_UNIT`-Regel kann über
`recursive: true` die Abteilung samt aller untergeordneten Bereiche adressieren.

Das Organisationsmodell trägt zusätzliche Stammdaten: jede Abteilung kann einen
Vorgesetzten (`manager_id`) haben, jede Person eine selbst gepflegte
Vertreterregelung (`deputy_id`, transitiv verfolgt). Beides sind Stammdaten und
dürfen auch an freigegebenen Schemata gesetzt werden (sie wirken sofort auf
laufende Instanzen):

```text
POST /schemas/{id}/org-units                       { "name": "Einkauf", "manager_id": "a1" }
POST /schemas/{id}/org-units/{org_unit_id}/manager { "manager_id": "a1" }
POST /schemas/{id}/org-units/{org_unit_id}/parent  { "parent_id": "abt" }
POST /schemas/{id}/agents/{agent_id}/deputy        { "deputy_id": "a2" }
```

Ein unbekannter Vorgesetzter, ein unbekannter Vertreter oder ein Selbstverweis
(Person ist ihr eigener Vertreter) wird über Regel Z1 mit **HTTP 422**
abgelehnt.

Die Abteilungshierarchie lässt sich nachträglich umbauen: `parent` hängt eine
Abteilung unter eine andere (oder mit `parent_id: null` auf die oberste Ebene).
Auch dies ist eine Stammdatenoperation und an freigegebenen Schemata erlaubt.
Würde ein Umhängen einen Zyklus erzeugen oder eine Abteilung zu ihrem eigenen
Vorgesetzten machen, lehnt der Kern den Aufruf mit **HTTP 422** (Regel `OP`) ab.
In der Weboberfläche pflegt die Ressourcensicht die Organisation als
**Baumstruktur**: Abteilungen samt Vorgesetzten-Markierung erscheinen
eingerückt, neue Untereinheiten entstehen per „+ Unter“, das Verschieben läuft
über den „Umhängen“-Dialog (keine Drag-and-drop-Geste). Die Agenten bleiben in
einer eigenen Tabelle daneben.

Eine Organisation kann **modellübergreifend geteilt** werden: Statt sie in jedem
Schema einzeln zu pflegen, wird sie einmal als eigenständige Stammdaten-Entität
angelegt und von mehreren Schemata referenziert (`ProcessSchema.org_model_id`).
Änderungen an der geteilten Organisation wirken **live** in allen verknüpften
Modellen und deren laufenden Instanzen. Die geteilte Organisation ist die
alleinige Quelle der Wahrheit; ein verknüpftes Schema speichert nur die
`org_model_id` und wird beim Laden an der API-Grenze mit dem aktuellen Stand
*hydriert* (so bleiben Validator, Bearbeiterauflösung und Ausführung
unverändert). Jede Org-Änderung wird gegen **alle** referenzierenden Schemata
revalidiert – würde sie eine dort aktive BZR leerlaufen lassen (Z2), wird sie
atomar mit **HTTP 422** abgelehnt (Correctness by Construction über die
Modellgrenze):

```text
POST   /org-models                                 { "name": "Stadtverwaltung", "org_model_id": "org1" }
POST   /org-models/{org_id}/roles                  { "name": "Sachbearbeiter", "role_id": "sb" }
POST   /org-models/{org_id}/agents                 { "name": "Erika", "role_ids": ["sb"], "agent_id": "a1" }
POST   /schemas/{id}/org-model                     { "org_model_id": "org1" }   # verknüpfen (nur ENTWURF)
DELETE /schemas/{id}/org-model                                                   # lösen (lokale Kopie bleibt)
```

Die Org-Pflege-Endpunkte unter `/org-models/{org_id}/...` spiegeln die
schemabezogenen Org-Endpunkte (Rollen, Abteilungen, Manager, Parent, Agenten,
Vertreter). Bei einem verknüpften Schema sind die schemabezogenen Org-Operationen
gesperrt (Regel `OP`, **HTTP 422**) – die Organisation wird ausschließlich zentral
gepflegt. Im Web-Client bietet die Ressourcensicht dafür „Geteilte Organisation…“
zum Anlegen, Auswählen, Verknüpfen und Lösen.

Eine Aktivitätenvorlage (Activity Repository) bündelt eine typisierte
I/O-Schnittstelle und einen Executor (`MANUAL`/`SCRIPT`/`SERVICE`/`WEB_SERVICE`).
Wird sie an einen Schritt gebunden, prüft der Validator A1 (Vorlage existiert),
A2 (`automatic`-Flag passt zum Executor — `MANUAL` ist interaktiv) und A3
(typkonforme Bindung: Pflicht-Parameter gemappt, Namen gehören zur Vorlage,
Datenelemente existieren und sind typgleich). Verletzungen ? **HTTP 422**.

Externe Daten (Connectoren) werden über denselben CbC-Pfad modelliert: ein
Connector wird registriert, dann ein Datenelement als `EXTERNAL` an eine
Connector-Entität gebunden (C1-C3):

```text
POST /schemas/{id}/connectors                 { "name": "ERP", "kind": "MS_SQL", "connector_id": "erp" }
POST /schemas/{id}/data-elements              { "name": "kunden_nr", "data_type": "STRING", "element_id": "key" }
POST /schemas/{id}/data-elements              { "name": "kunde", "data_type": "STRING", "element_id": "kunde" }
POST /schemas/{id}/data-elements/kunde/external { "connector_id": "erp", "entity": "Kunde", "key_element_id": "key" }
```

Ein unbekannter Connector (C1), ein fehlender/externer Schlüssel (C2) oder eine
leere Entität (C3) wird mit **HTTP 422** abgelehnt.

Alternativ füllt eine **typ- und kardinalitätssichere Skalar-Bindung** ein
Datenelement aus **einem** Wert einer externen Quelle bzw. schreibt es zurück —
ohne Freitext-SQL, mit Correctness by Construction für die SQL-Erzeugung
(C4–C9):

```text
POST /schemas/{id}/data-elements/kundenname/sql-select
  { "connector_id": "erp", "entity": "Kunde", "column": "name",
    "column_type": "STRING", "cardinality": "KEY_UNIQUE", "unique_column": "kd_id",
    "filters": [ { "column": "kd_id", "column_type": "INTEGER",
                   "operator": "EQ", "key_element_id": "key" } ] }
POST /schemas/{id}/data-elements/status/sql-write
  { "connector_id": "erp", "entity": "Kunde", "column": "status",
    "column_type": "STRING", "unique_column": "kd_id",
    "filters": [ { "column": "kd_id", "column_type": "INTEGER",
                   "operator": "EQ", "key_element_id": "key" } ] }
```

Ein nicht zum Datenelement passender Ergebnistyp (C4/C7), ein fehlerhafter
Filter (C5/C8) oder eine nicht auf genau eine Zeile eingegrenzte Abfrage (C6/C9)
wird mit **HTTP 422** abgelehnt. `GET /v1/connectors/{id}/columns?entity=…`
liefert per Introspektion die Spalten samt gemapptem Datentyp für den geführten
Assistenten.

BPMN 2.0 dient als Austauschformat auf der geprüften Block-Teilsprache:

```text
GET  /schemas/{id}/bpmn             -> application/xml (BPMN 2.0)
POST /bpmn-import                   { "xml": "<bpmn:definitions ...>", "name": "Importiert" }
```

Der Export liefert semantisches BPMN (ohne Diagramm-Layout). Der Import wird
**vor** dem Speichern validiert: fehlerhaftes XML oder ein Konstrukt außerhalb
der Teilsprache (z. B. `inclusiveGateway`) liefert **HTTP 422**, und ein nicht
block-strukturierter Graph wird über denselben Korrektheitspfad (K1-K3)
abgelehnt — der Importweg lässt sich nicht umgehen.

Eine ungültige Operation (z. B. Einfügen nach `end`) wird mit **HTTP 422** und
lokalisierten `findings` abgelehnt — der Validierungspfad lässt sich nicht
umgehen.

### Ausführung (Execution Engine)

Ein freigegebenes Schema wird instanziiert und über die Worklist abgearbeitet:

```text
POST /schemas/{id}/instances        -> ProcessInstance (state RUNNING, Markierungen gesetzt)
GET  /instances/{iid}/worklist      -> { "ready_activities": [...] }
POST /instances/{iid}/complete      { "node_id": "<act>", "data": { "betrag": 1500 } }
```

Parallele AND-Zweige werden gleichzeitig bereit; ein XOR-Split **entscheidet
automatisch** anhand der strukturierten Partition (K7) über seinen
Diskriminator-Wert aus den Instanzdaten – der nicht gewählte Zweig wird
`SKIPPED`, ein manueller Entscheidungsschritt entfällt. Eine
Laufzeitoperation im falschen Zustand (z. B. Instanziieren eines Entwurfs)
wird mit **HTTP 409** abgelehnt.

### Bearbeiter-Aufgabenliste (Z-Laufzeitauflösung)

Während die BZR zur Entwurfszeit nur eine Über-Approximation prüft, löst die
Laufzeit die Bearbeiterregel eines aktiven Schritts konkret auf: `ROLE` und
`ORG_UNIT` (optional rekursiv über alle Unterbereiche) ergeben die zugehörigen
Personen, `NODE_PERFORMING_AGENT` bindet an die Person, die einen früheren
Schritt tatsächlich ausgeführt hat (`instance.performed_by`), und `AND`/`OR`/
`EXCEPT` verknüpfen Teilmengen. Die Berechtigten werden anschließend um die
transitive Vertreterkette erweitert.

```text
GET  /instances/{iid}/tasks         -> [ { schema_id, schema_version, node_id, label, eligible_agents: [...] } ]
GET  /agents/{agent_id}/tasks       -> offene Aufgaben dieser Person (inkl. Vertretung)
POST /instances/{iid}/complete      { "node_id": "<act>", "agent_id": "a1", "data": {...} }
```

Eine an eine Abteilung oder Rolle gerichtete Aufgabe erscheint bei allen
zugehörigen Personen, kann aber von genau einer bearbeitet werden. Wird beim
Abschluss eine `agent_id` mitgegeben, lehnt der Kern eine nicht berechtigte
Person mit **HTTP 409** ab und vermerkt sonst den Bearbeiter in
`performed_by`. Im Web-Client bündelt die Sicht **Meine Aufgaben** diese Liste
pro angemeldeter Person.

### Komposition (Sub-/Folgeprozesse)

Ein Schema kann ein anderes als **Sub-Prozess** einbetten oder als
**Folgeprozess** verketten. Beides ist nur gegen ein **freigegebenes** Ziel
zulässig (typkonformes Mapping, azyklische Hierarchie):

```text
POST /schemas/{id}/subprocess
     { "after_node_id": "start", "target_schema_id": "<sub>", "target_version": 1,
       "input_mapping": { "<sub_input>": "<parent_de>" } }
POST /schemas/{id}/convert-to-subprocess
     { "node_id": "<activity>", "target_schema_id": "<sub>", "target_version": 1,
       "input_mapping": { "<sub_input>": "<parent_de>" },
       "output_mapping": { "<sub_output>": "<parent_de>" } }
POST /schemas/{id}/subprocess-binding
     { "node_id": "<subprocess>", "target_schema_id": "<sub>", "target_version": 1,
       "output_mapping": { "<sub_output>": "<parent_de>" } }
POST /schemas/{id}/library-flag     { "is_library": true }   -> als Submodell markieren
GET  /subprocess-library            -> [ { "id", "name", "version", "data_elements": [...] } ]
POST /schemas/{id}/follow-up
     { "target_schema_id": "<typ>", "mode": "ASYNC",
       "handover_mapping": { "<ziel_start_de>": "<quell_de>" } }
```

Eine **Aktivität** lässt sich per `convert-to-subprocess` in einen an ein
freigegebenes **Submodell** gebundenen `SUBPROCESS` umwandeln; die Bindung eines
bestehenden Sub-Prozess-Knotens ändert `subprocess-binding`. Freigegebene
Modelle werden per `library-flag` in die über `GET /subprocess-library`
abrufbare **Bibliothek** aufgenommen. Verweist ein Sub-Prozess auf ein nicht
freigegebenes Ziel (H1), ist ein Mapping nicht typkonform oder erzeugt das
Submodell einen gemappten Output nicht auf jedem Pfad (H2/F2) oder würde die
Hierarchie zyklisch (H3), wird die Operation mit **HTTP 422** abgelehnt.

### Ad-hoc-Änderungen einer Instanz (R1/R2)

Eine **einzelne** laufende Instanz lässt sich an die Realität anpassen, ohne
das freigegebene Schema zu ändern. Die Instanz erhält eine eigene Variante
(`ad_hoc_schema`); die Engine läuft nahtlos dagegen weiter:

```text
POST /instances/{id}/adhoc/insert  { "after_node_id": "<knoten>", "label": "Zusatzschritt" }
POST /instances/{id}/adhoc/delete  { "node_id": "<knoten>" }
```

Erlaubt ist nur die noch nicht ausgeführte Region (R1: ein bereits
laufender/abgeschlossener/übersprungener Knoten ist eingefroren); die
resultierende Variante wird vor dem Commit voll validiert (R2). Verletzungen
werden mit **HTTP 422** abgelehnt.

### Schema-Evolution + Instanzmigration (M1–M5)

Eine neue Revision (`POST /schemas/{id}/revision`) behält alle Element-IDs,
sodass laufende Instanzen umziehen können — sofern die ausgeführte Region
erhalten bleibt:

```text
POST /schemas/{id}/revision                { }
POST /instances/{id}/migration-check       { "target_schema_id": "<rev>" }
POST /instances/{id}/migrate               { "target_schema_id": "<rev>",
                                             "data_mapping": { "<de>": "<wert>" } }
```

`migration-check` liefert `{ migratable, findings }` (M1 Ziel korrekt+RELEASED,
M2 ausgeführte Region unverändert, M3 saubere Markierungsabbildung, M4
Pflichtdaten verfügbar, M5 keine ad-hoc Änderungen). `migrate` zieht die
Instanz atomar um (Markierungen werden umgesetzt, neue Knoten starten
unmarkiert) oder lehnt mit **HTTP 422 + Befund** ab.

### Monitoring/Audit + Process Mining

Jede Laufzeitoperation wird **an der API-Grenze** in ein append-only Event-Log
geschrieben (der Ausführungskern bleibt rein und kennt das Log nicht). Aus der
Historie liest die API drei Auswertungen:

```text
GET  /instances/{iid}/audit              -> Audit-Timeline einer Instanz (chronologisch)
GET  /audit?schema_id=&instance_id=      -> Roh-Events (optional gefiltert)
GET  /monitoring/kpis?schema_id=         -> { total_instances, running, completed,
                                             avg_cycle_seconds, activity_stats: [...] }
GET  /monitoring/process-map?schema_id=  -> { nodes: [...], edges: [...] }
GET  /monitoring/revision                -> { revision }   # monotoner Zähler für Live-Refresh
```

Erfasste Ereignisse sind `INSTANCE_CREATED`, `ACTIVITY_STARTED`,
`ACTIVITY_COMPLETED`, `BRANCH_DECIDED`, `ADHOC_INSERTED`, `ADHOC_DELETED`,
`INSTANCE_MIGRATED`, `INSTANCE_COMPLETED` (je mit Zeitstempel, Knoten/Label und
ggf. Bearbeiter). `kpis` liefert Durchlaufzeit (Ø) und je Aktivität
Abschlüsse + Ø Bearbeitungszeit als Engpass-Signal; `process-map` ist ein aus
den realen Abläufen entdeckter **Directly-follows-Graph** (leichtes Process
Mining). Der Web-Client zeigt all das in der Sicht **Monitoring** (KPI-Kacheln,
Engpass-Tabelle, Prozesskarte) und blendet pro Instanz einen **Audit-Verlauf**
ein. Ohne `DATABASE_URL` liegt das Log in-memory; mit `DATABASE_URL` wird jedes
Event durabel in die Tabelle `audit_event` geschrieben (`SqlAlchemyAuditLog`,
monotone `seq` per Datenbank) und überlebt einen Neustart.

Aus derselben `seq` leitet sich `GET /monitoring/revision` ab – ein **monoton
steigender Revisionszähler** (`AuditLog.revision()`). Der Web-Client pollt ihn
im Hintergrund und aktualisiert die aktive Laufzeit-Sicht (Aufgabenlisten,
Ausführen, Monitoring) **automatisch**, sobald sich der Fortschritt einer
Aktivität/Instanz irgendwo ändert – ohne manuelles Neuladen. Die zuletzt
gewählte Sicht wird zudem im Browser gemerkt und bei einem Reload
wiederhergestellt.

### Beispieldaten & Reset (Administrator)

`demo.py` enthält einen reproduzierbaren Demo-Datensatz, der alle Funktionen
greifbar macht: eine geteilte Organisation `org-acme` (Rollen, Abteilungen,
Agenten mit Vertreter), den **freigegebenen** Prozess `urlaubsantrag`
(START → erfassen → prüfen → XOR Genehmigung/Ablehnung → benachrichtigen) und
den **Entwurf** `beschaffung` (AND-Split), dazu **drei Instanzen** an
unterschiedlichen Punkten:

```text
urlaub-2026-001  RUNNING    frisch gestartet (erste Aktivität offen)
urlaub-2026-002  RUNNING    erfasst + geprüft, wartet auf Genehmigung der Leitung
urlaub-2026-003  COMPLETED  abgelehnt + benachrichtigt (Ende erreicht)
```

Beide Prozesse zeigen **Datenobjekte, die zwischen Aufgaben wandern und befüllt
werden** (Schreiben-vor-Lesen, D1):

- `urlaubsantrag`: `tage` wird in *Antrag erfassen* geschrieben und in *Antrag
  prüfen* sowie der XOR-Bedingung gelesen. Zusätzlich wandert ein **angereichertes**
  Objekt `entscheidung` durch den Fluss: es wird vom jeweils laufenden XOR-Zweig
  (*Genehmigung* **oder** *Ablehnung*) befüllt und danach von *Mitarbeiter
  benachrichtigen* gelesen – garantiert vorhanden auf jedem Pfad (XOR-Join-Schnitt).
- `beschaffung`: zwei Objekte werden auf **parallelen** Zweigen befüllt und am
  AND-Join zusammengeführt – `betrag` (*Angebote einholen*) und `budget_ok`
  (*Budget prüfen*) werden beide von *Bestellung freigeben* gelesen (keine
  konkurrierenden Schreibzugriffe, D2).

Die abgeschlossene Instanz `urlaub-2026-003` trägt entsprechend reale Werte
(`tage=20`, `entscheidung="Abgelehnt: …"`), die über die Aktivitäten
weitergereicht wurden.

Damit **jede Sicht** ab dem ersten Start etwas anzeigt, demonstriert der
Datensatz nahezu den gesamten Funktionsumfang:

- **Eingabemasken** (Formular-Designer): *Antrag erfassen* (Zahlenfeld +
  optionales Textfeld), *Angebote einholen* (Bestellwert + Lieferantennummer) und
  *Budget prüfen* (Checkbox).
- **Wertschöpfungsklassen** (alle drei), **Arbeitslisten-Priorität** und die
  **Zeitperspektive** (Soll-Dauern je Schritt + Prozessfrist) im Urlaubsprozess.
- **Integration** im Beschaffungs-Entwurf: ein **Daten-Connector** (`erp`) mit
  **CbC-sicherer skalarer SQL-Anbindung** (Kreditlimit aus dem ERP per
  Lieferantennummer). Alle Schritte sind interaktiv, sodass sich der Prozess
  vollständig in der GUI durchspielen lässt; die External-Task-Automatik zeigt der
  [Integrations-Leitfaden](../docs/Integrations-Leitfaden.md) (sie benötigt einen
  Worker zum Abschließen).
- **Strukturierte Bearbeiterregeln** (BZR): eine Organisationseinheit-Regel und
  ein ODER-Kombinator statt reiner Rollen-Blätter.
- Eine **zweistufige Organisationshierarchie** (Geschäftsleitung über Vertrieb und
  Einkauf) für ein echtes Organigramm.

`load_demo(*, schema_store, instance_store, org_store, audit_log, password_backend=None)`
befüllt die Stores idempotent und erzeugt dabei echte Audit-Events (die Instanzen
tauchen also in KPIs und Prozesskarte auf). Mit `password_backend` werden
zusätzlich die Test-Logins angelegt (`mara.modell`, `erika.sander`, `tom.berger`,
`vera.viewer`; Passwort `demo-procworks`, ohne erzwungene Änderung).

Der Reset ist **administrator-exklusiv** und läuft über die API:

```text
POST /admin/reset   {"load_demo": false}  -> alles auf Null
POST /admin/reset   {"load_demo": true}   -> alles auf Null, danach Beispieldaten
   -> { demo_loaded, schemas, instances, org_models, users }
```

`require_role("admin")` schützt den Endpunkt; ein Nicht-Admin erhält HTTP 403.
Geleert werden Schemata, Instanzen, Organisationsmodelle und das Audit-Log (alle
Stores haben dafür ein additives `clear()`). Im Passwort-Login werden zusätzlich
alle Nutzerkonten entfernt – **außer** dem Bootstrap-`admin` und der gerade
handelnden Administrator-Identität, damit sich niemand aussperrt. Der Web-Client
bietet das als Bereich **„Wartung (Administrator)"** in der Monitoring-Sicht
(zwei Aktionen mit Bestätigungsdialog), nur für die Rolle `admin` sichtbar.


## Persistenz (PostgreSQL)

Ohne `DATABASE_URL` arbeitet der Kern mit einem flüchtigen In-Memory-Store
(Daten gehen beim Neustart verloren). Für dauerhafte Speicherung PostgreSQL
verwenden:

```powershell
# 1. Lokale Datenbank starten (Docker)
docker compose -f deploy/docker-compose.yml up -d

# 2. Treiber + Migrationswerkzeug installieren
..\.venv\Scripts\python.exe -m pip install -e ".[postgres]"

# 3. Verbindung setzen (siehe .env.example)
$env:DATABASE_URL = "postgresql+psycopg://process:process@localhost:5432/procworks"

# 4. Schema- und Instanz-Migration anwenden
..\.venv\Scripts\python.exe -m alembic upgrade head

# 5. API starten (nutzt jetzt PostgreSQL)
..\.venv\Scripts\python.exe -m uvicorn procworks.api:app --reload
```

Der Store-Wechsel ist transparent: Dieselbe API und derselbe Validierungspfad
gelten für In-Memory wie PostgreSQL. Jedes Schema wird als ein JSONB-Dokument
je Zeile (`process_schema`) abgelegt, jede laufende Instanz analog
(`process_instance`), jedes Audit-Event als eigene Zeile (`audit_event`);
Persistenz fügt der CbC-Garantie nichts hinzu und nimmt ihr nichts — gespeichert
werden ausschließlich zuvor validierte Modelle bzw. durch die Engine erzeugte
Instanzzustände.

## Deployment (Container, Reverse Proxy, Helm, CI/CD)

Der gesamte Stack ist quelloffen und containerisiert (Abschnitt 11 des
Architektur-Konzepts). Der API-Server ist **zustandslos** und horizontal
skalierbar; der Web-Client wird statisch über **Caddy** ausgeliefert, das
zugleich als Reverse Proxy mit automatischem TLS dient und `/api/*` an die API
weiterreicht.

```powershell
# Voller lokaler Stack (PostgreSQL + API + Web/Caddy), aus dem Repo-Wurzelverzeichnis:
docker compose -f deploy/docker-compose.full.yml up --build
# danach http://localhost oeffnen (Web liefert die SPA, /api wird geroutet)
```

- **Windows Server (Erstinstallation von Grund auf):** Schritt-für-Schritt-
  Anleitung unter [`../docs/Windows-Server-Setup.md`](../docs/Windows-Server-Setup.md).
- **Mitarbeiter-Anleitung (Anmelden + Aufgaben bearbeiten):** zum Weitergeben
  unter [`../docs/Mitarbeiter-Anleitung.md`](../docs/Mitarbeiter-Anleitung.md).
- **Container:** [`Dockerfile`](Dockerfile) (API; Migrationen beim Start via
  `docker-entrypoint.sh`, dann Uvicorn, non-root, Healthcheck) und
  [`../web/Dockerfile`](../web/Dockerfile) (Web + Caddy).
- **Reverse Proxy / TLS:** [`../deploy/Caddyfile`](../deploy/Caddyfile) — für
  öffentliches HTTPS `SITE_ADDRESS` auf die Domain und `ACME_EMAIL` setzen.
- **Kubernetes:** Helm-Chart unter [`../deploy/helm/`](../deploy/helm/)
  (API-/Web-Deployment + Service, optionales Ingress, `DATABASE_URL`-Secret;
  PostgreSQL wird extern bereitgestellt).
- **CI/CD:** [`../.github/workflows/release.yml`](../.github/workflows/release.yml)
  baut beide Images, scannt sie mit **Trivy** und pusht sie bei einem
  Versions-Tag (`v*`) nach **ghcr.io**.

## Lizenz

Business Source License 1.1 (BUSL-1.1) — siehe [../LICENSE](../LICENSE).

## Haftungsausschluss

ProcWorks wird **„wie besehen", ohne jede Gewährleistung und ohne jede Haftung**
bereitgestellt. Inbetriebnahme (z. B. an Servern, Betriebssystemen, paralleler
oder Drittsoftware, Netzwerken/Infrastruktur) und Nutzung (z. B. Daten und
Geschäftsprozesse) erfolgen ausschließlich auf eigenes Risiko und in eigener
Verantwortung. Im größtmöglichen gesetzlich zulässigen Umfang ist jede Haftung
für Schäden ausgeschlossen; zwingende gesetzliche Haftung bleibt unberührt.
Vollständiger Text: [../DISCLAIMER.md](../DISCLAIMER.md).
