# Richtlinie zur Meldung von Sicherheitslücken

Die Sicherheit von ProcWorks ist uns wichtig. Danke, dass du Schwachstellen
verantwortungsvoll offenlegst.

## Unterstützte Versionen

Das Projekt befindet sich in aktiver Entwicklung. Sicherheitsfixes werden gegen
den `main`-Branch bereitgestellt.

| Version | Unterstützt |
| ------- | ----------- |
| `main`  | ✅          |
| ältere Tags | ❌      |

## Eine Schwachstelle melden

**Bitte melde Sicherheitslücken nicht über öffentliche GitHub-Issues.**

Nutze stattdessen einen der folgenden vertraulichen Kanäle:

- Bevorzugt: **GitHub Security Advisories** über den Reiter *Security* →
  *Report a vulnerability* dieses Repositorys.
- Alternativ: per E-Mail an `kontakt@procworks.de`.

Bitte gib so viele Details wie möglich an:

- betroffene Komponente/Datei und Version bzw. Commit,
- eine Beschreibung der Schwachstelle und ihrer Auswirkung,
- eine Schritt-für-Schritt-Anleitung zur Reproduktion (Proof of Concept),
- mögliche Gegenmaßnahmen, falls bekannt.

## Ablauf

1. **Eingangsbestätigung** innerhalb von 72 Stunden.
2. **Bewertung & Triage**: Wir prüfen den Bericht und melden uns mit einer
   ersten Einschätzung zurück.
3. **Behebung**: Wir entwickeln einen Fix und koordinieren mit dir einen
   Zeitpunkt für die Offenlegung.
4. **Veröffentlichung**: Nach dem Fix wird die Schwachstelle in den
   Release-Notes dokumentiert; auf Wunsch nennen wir dich als Finder.

Wir bitten um **Coordinated Disclosure**: Bitte veröffentliche Details erst,
nachdem ein Fix verfügbar ist.

## Authentifizierung & Betrieb

Die API trägt eine austauschbare Auth-Schicht am Boundary (`auth.py`,
Auth-Konzept Variante C). Hinweise für den produktiven Betrieb:

- **Code-Standardmodus ist „offen“** (`PROCWORKS_AUTH=open`): keine Identitätsprüfung,
  alle Rollen freigegeben. Dieser Modus ist ausschließlich für die lokale
  Entwicklung gedacht und darf **nicht** öffentlich exponiert werden. Der
  gebündelte Produktions-Stack ([deploy/docker-compose.full.yml](deploy/docker-compose.full.yml)
  und das Helm-Chart, `api.authMode`) setzt daher standardmäßig
  `PROCWORKS_AUTH=password`.
- **Produktiv** `PROCWORKS_AUTH=token` setzen und Tokens über `PROCWORKS_TOKENS`
  (JSON-Datei) bereitstellen. Tokens werden nur als SHA-256-Digest gehalten.
  Die Token-Datei gehört nicht ins Repository und sollte restriktive
  Dateirechte erhalten.
- **Passwort-Login** (`PROCWORKS_AUTH=password`) für Deployments ohne externen
  IdP: Zugangsdaten liegen in einem separaten `CredentialStore` (nicht im
  Modell). Passwörter werden mit `hashlib.scrypt` pro Nutzer gesalzen gehasht
  und konstant-zeitlich verglichen; Klartext wird nie gespeichert. Login-
  Sessions sind opake Bearer-Token, nur als SHA-256-Digest im Speicher
  gehalten (Neustart erzwingt erneutes Login). Neue Nutzer erhalten ein
  zufälligem Initialpasswort mit erzwungener Änderung beim ersten Login
  (min. 8 Zeichen, ungleich dem alten). Der Initial-Admin kann über
  `PROCWORKS_ADMIN_LOGIN`/`PROCWORKS_ADMIN_PASSWORD` fest provisioniert werden
  (diese Variablen als Secrets behandeln); ist nichts gesetzt und der
  Credential-Store noch leer, legt der Server beim ersten Start automatisch ein
  `admin`-Konto an und schreibt dessen zufälliges Einmal-Passwort **einmalig
  ins Server-Log** (z. B. `docker compose logs api`) – dort ablesen und sofort
  ändern. Session-Dauer über `PROCWORKS_SESSION_TTL_MINUTES`.
- **CORS** über `PROCWORKS_CORS_ORIGINS` (kommagetrennt) auf die tatsächlich
  erlaubten Ursprünge einschränken; der Default `*` ist nur für die Entwicklung.
- Die handelnde Bearbeiter-Identität wird bei `complete`/`decide` aus dem
  verifizierten `Principal` abgeleitet, niemals aus dem Request-Body
  (Impersonation-Schutz). Die feingranulare BZR-Eignungsprüfung im Kern bleibt
  als zusätzliche Schutzschicht aktiv.

## Haftungsausschluss

Diese Sicherheitsrichtlinie beschreibt Schutzmaßnahmen und Meldewege, begründet
aber **keine** Gewährleistung und **keine** Haftung. ProcWorks wird **„wie
besehen", ohne jede Gewährleistung und ohne jede Haftung** bereitgestellt;
Inbetriebnahme und Betrieb erfolgen ausschließlich auf eigenes Risiko und in
eigener Verantwortung. Im größtmöglichen gesetzlich zulässigen Umfang
übernehmen wir keine Haftung für Schäden an Servern, Hardware, Betriebssystemen,
paralleler oder Drittsoftware, Netzwerken oder Infrastruktur (Inbetriebnahme)
noch für Verlust, Beschädigung oder Offenlegung von Daten oder fehlerhafte
Geschäftsprozesse (Nutzung) – auch nicht infolge von Sicherheitslücken,
Fehlkonfigurationen oder des Betriebs im offenen Entwicklungsmodus. Die
Verantwortung für sichere Konfiguration, Backups und den Schutz der eigenen
Systeme liegt beim Betreiber. Vollständiger Text: [DISCLAIMER.md](DISCLAIMER.md).
