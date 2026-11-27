// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks Web-Client - Grundlagen.
 *
 * Konfiguration und globaler Zustand (``state``), Farbschema, deutsche
 * Bezeichnungen der Aufzaehlungen des Kerns, DOM-Helfer sowie Toast und
 * Fehlerbehandlung. Alle anderen Skripte bauen darauf auf.
 *
 * Klassisches Skript ohne Build: index.html laedt es in fester Reihenfolge
 * vor app.js, alle Skripte teilen sich den globalen Gueltigkeitsbereich.
 * Beim Laden ausgefuehrter Code darf nur Deklarationen aus diesem oder
 * einem frueher geladenen Skript verwenden.
 */

// --------------------------------------------------------------------------
// Konfiguration + Zustand
// --------------------------------------------------------------------------

const DEFAULT_API = "http://127.0.0.1:8000";

// Behind the reverse proxy (deploy/Caddyfile) the API is reachable same-origin
// under /api. When the page is served from a real host (not the local file://
// or the :5500 dev static server) default to that proxied path so the deployed
// app — including the Docker stack on http://localhost — works without manual
// configuration. The local dev flow serves the SPA from a static server on
// :5500 (uvicorn separately on :8000) and keeps the 127.0.0.1:8000 default.
function defaultApiBase() {
  try {
    const { protocol, port } = window.location;
    const isFile = protocol === "file:";
    const isDevStatic = port === "5500";
    if (!isFile && !isDevStatic) {
      return window.location.origin + "/api";
    }
  } catch (_e) {
    // window/location unavailable -> fall back to the dev default below.
  }
  return DEFAULT_API;
}

const state = {
  apiBase: localStorage.getItem("apiBase") || defaultApiBase(),
  token: localStorage.getItem("authToken") || "",
  // Farbschema der GUI ("dark" | "light"). Rein visuell, keine Auswirkung auf
  // die Kern-/Korrektheitslogik. Persistiert unter localStorage "theme"; wird
  // ueber das data-theme-Attribut am <html> von styles.css umgesetzt.
  theme: localStorage.getItem("theme") === "light" ? "light" : "dark",
  // Eingeklappter (schmaler) Menübereich – gibt dem Seiteninhalt mehr Platz.
  // Persistiert, damit die Wahl über Reloads erhalten bleibt.
  sidebarCollapsed: localStorage.getItem("sidebarCollapsed") === "1",
  // Vollbild des Kontrollflusses in der Modellieren-Sicht (zum Diskutieren).
  // Bewusst NICHT persistiert – ein transienter Präsentationsmodus.
  graphMaximized: false,
  principal: null,
  authMode: "open",
  passwordLogin: false,
  // OIDC-Redirect-Login (JWT-Modus, Opt-in): Endpunkte/Client-Id aus
  // /auth/config, oder null -> die SPA zeigt nur das Token-Feld.
  oidc: null,
  // Public-demo login conveniences, filled from /auth/config in demo mode only
  // (PROCWORKS_DEMO_MODE on the server). Empty/false in every real deployment.
  demo: false,
  demoPassword: "",
  demoAutologin: "",
  demoLogins: [],
  // Broker endpoint for the post-demo survey ("Demo beenden"). Empty -> no survey.
  demoFeedbackUrl: "",
  // One-shot guard so the auto-login is only attempted once per page load.
  demoAutologinTried: false,
  // Start im Ausfall: true, solange ``boot`` den Server nicht erreicht und die
  // Anmeldung daher ungeprueft ist (``showStartPending``, ``renderUser``).
  startPending: false,
  view: localStorage.getItem("view") || "model",
  schemaIds: [],
  schemaNames: {},
  // Version (revision) per schema id, captured alongside the name. Immutable
  // per id (a new revision gets a fresh id), so it is safe to cache.
  schemaVersions: {},
  // Lebenszyklus je Schema-id (ENTWURF/RELEASED/…), beim Laden der Namen
  // mitgemerkt; nur fuer die Vorauswahl (``preferredSchemaId``).
  schemaLifecycle: {},
  // Abstammung je Schema-id (``revision_of``, ``null`` fuer eine erste
  // Version), aus ``GET /schema-summaries``; nur fuer die Anzeige (welche
  // Version die juengste ihrer Familie ist).
  schemaRevisionOf: {},
  schemaId: localStorage.getItem("schemaId") || null,
  schema: null,
  validation: null,
  // Nicht-blockierende Modellhinweise (G-Gruppe, /metrics) zum gewählten
  // Schema. Rein beratend – nie Teil der Korrektheitsentscheidung.
  hints: [],
  instanceIds: [],
  instanceId: null,
  instance: null,
  worklist: null,
  selectedNode: null,
  // When a data/worker badge on a control-flow node is clicked, the target view
  // (data / resource) highlights and scrolls to that node's bindings. Mutually
  // exclusive; cleared on a manual nav click. Not persisted.
  dataFocusNode: null,
  staffFocusNode: null,
  // Rücksprungpunkt: Wird der Kontrollfluss per Klick (Badge oder "Vollansicht")
  // in die Daten-/Ressourcensicht verlassen, merkt sich dies { view, selectedNode }
  // des Ausgangspunkts, damit ein Klick auf die Rücksprung-Leiste exakt dorthin
  // zurückführt (Ansicht + re-zentrierter Knoten). Bei manueller Navigation
  // verworfen. Nicht persistiert.
  returnTo: null,
  // Modellieren-Seite: ein in der Bindungs-Palette angeklicktes Datenelement,
  // dessen Herkunft (Schreib- -> Lese-Knoten) im Kontrollfluss als gestrichelte
  // Linien hervorgehoben wird. Rein visuell, nicht persistiert.
  dataElemFocus: null,
  // Aktive Modellier-Oberflaeche: "card" (Kontrollfluss + Schritt-Karte,
  // Standard) oder "classic" (Zwei-Spalten-Sicht mit Bindungs-Palette rechts).
  // Beide bleiben gleichwertig erhalten; persistiert je Browser.
  modelUx: localStorage.getItem("modelUx") || "card",
  // Aktiver Tab der Bindungs-Palette der klassischen Sicht ("data" | "res").
  // Nicht persistiert.
  paletteTab: "data",
  // Modellieren-Sicht: aufgeklappte Abschnitte der Schritt-Karte.
  // Persistiert, damit die eigene
  // Arbeitsweise erhalten bleibt -- wer Zeit/Priorität immer braucht, klappt sie
  // einmal auf. Ein defekter/fehlender Eintrag faellt auf CARD_DEFAULT_OPEN
  // zurueck (siehe readCardOpen).
  cardOpen: null,
  // Abschnitt, der beim naechsten Rendern in den sichtbaren Bereich der Karte
  // gerollt wird ("name" = das Bezeichnungsfeld im Kartenkopf fokussieren).
  // Einmalig -- viewModel raeumt ihn nach dem Anwenden ab. Nicht persistiert.
  cardFocusSection: null,
  // Resource view: clicking an org unit (in the org chart or the Abteilungen
  // tree) highlights that unit plus the agents that belong to it -- including
  // the unit's supervisor -- in the Agenten table. orgFocusUnit is the selected
  // unit id; orgFocusAgents lists the agent ids to emphasise. Not persisted.
  orgFocusUnit: null,
  orgFocusAgents: [],
  // Last seen runtime-event revision (GET /monitoring/revision). Drives the
  // live auto-refresh of the task/monitoring/run views; not persisted.
  revision: 0,
  // Last connection-test outcome per connector id (ok/err/unknown), shown as a
  // status badge in the integration view. Purely a UI hint; not persisted.
  connectorStatus: {},
  // Personenverzeichnis über ALLE Organisationsmodelle (GET /directory/agents),
  // als { agent_id: DirectoryAgent }. Eine Arbeitsliste reicht über alle
  // Prozesse -- die Namen darin dürfen deshalb nicht am gerade gewählten Modell
  // hängen (sonst standen dort interne IDs, siehe agentNameOf). Reine
  // Anzeigedaten, nie eine Zuständigkeitsentscheidung; nicht persistiert.
  agentDirectory: {},
  // Prüfinstanz-Analyse (viewTestRun): die laufende Test-Instanz eines Entwurfs,
  // der startende Agent (Starter, oben rechts) und die beiden frei wählbaren
  // beteiligten Agenten der unteren Arbeitslisten-Quadranten. Persistiert, damit
  // ein Reload die Analyse fortsetzt (die Test-Instanz lebt im Kern weiter,
  // solange dieser läuft). Nur für Modellierer/Administratoren.
  testInstanceId: localStorage.getItem("testInstanceId") || null,
  testInstance: null,
  testStarter: localStorage.getItem("testStarter") || null,
  testAgentA: localStorage.getItem("testAgentA") || null,
  testAgentB: localStorage.getItem("testAgentB") || null,
  // Simulation der Pruefinstanz-Sicht: { key: Schema-id, values: {elemId:
  // Eingabe}, result: letztes Ergebnis | null }. Haelt beides ueber das
  // automatische Neuzeichnen hinweg; nicht persistiert.
  simulation: null,
  // Licensing (dormant unless the backend is enforced). `license` is the last
  // /license/status snapshot, `licenseAgents` maps agent_id -> AgentLicenseView.
  // Both stay null (and the whole license UI stays hidden) while the backend
  // reports enforced=false, so the default Community mode looks unchanged.
  license: null,
  licenseAgents: null,
  _licenseSig: null,
  // Set while an online auto-pull poll loop is running (after a checkout), so
  // a second "kaufen" click does not start a competing loop.
  _claimPolling: false,
};

// --------------------------------------------------------------------------
// Farbschema (Theme)
// --------------------------------------------------------------------------

/**
 * Setzt das aktive Farbschema am Dokument um, ohne es zu persistieren.
 *
 * Stempelt das Attribut `data-theme` auf das <html>-Element; styles.css waehlt
 * darueber die passende Variablengruppe (Default = dunkel). Zusaetzlich wird der
 * aktive Umschalt-Knopf in der Sidebar markiert, falls er bereits im DOM ist
 * (beim ersten, sehr fruehen Aufruf existiert er ggf. noch nicht -- das ist
 * unkritisch, wireTheme() gleicht die Markierung nach dem Verdrahten ab).
 *
 * @param {"dark"|"light"} theme Zielschema.
 */
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  document.querySelectorAll(".theme-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.themeChoice === theme);
  });
}

/**
 * Wechselt das Farbschema dauerhaft: aktualisiert Zustand + localStorage und
 * wendet es sofort an. Aufgerufen von den Sidebar-Knoepfen (wireTheme).
 *
 * @param {"dark"|"light"} theme Zielschema.
 */
function setTheme(theme) {
  state.theme = theme === "light" ? "light" : "dark";
  localStorage.setItem("theme", state.theme);
  applyTheme(state.theme);
}

// So frueh wie moeglich anwenden, damit kein Aufblitzen des Default-Schemas
// entsteht, wenn der Nutzer die helle Variante gewaehlt hat.
applyTheme(state.theme);

/**
 * Spiegelt den eingeklappten/ausgeklappten Menü-Zustand ins DOM: setzt das
 * Attribut `data-sidebar` aufs <html> (die CSS-Regeln reagieren darauf) und
 * aktualisiert Beschriftung/Tooltip des Umschalt-Knopfes. Reine Darstellung –
 * kein API-Aufruf. Guard: der Knopf existiert erst, wenn das DOM geladen ist.
 */
function applySidebar() {
  const collapsed = !!state.sidebarCollapsed;
  document.documentElement.setAttribute("data-sidebar", collapsed ? "collapsed" : "expanded");
  const btn = byId("sidebar-toggle");
  if (btn) {
    btn.textContent = collapsed ? "»" : "«"; // » ausklappen / « einklappen
    btn.title = collapsed ? "Menü ausklappen" : "Menü einklappen";
  }
}

/**
 * Schaltet den schmalen Menü-Modus um, merkt die Wahl in localStorage und wendet
 * sie sofort an. Kein Voll-Render nötig – das Layout regelt reines CSS.
 */
function toggleSidebar() {
  state.sidebarCollapsed = !state.sidebarCollapsed;
  localStorage.setItem("sidebarCollapsed", state.sidebarCollapsed ? "1" : "0");
  applySidebar();
}

// Fruehzeitig anwenden, damit die Sidebar nicht kurz breit aufblitzt.
applySidebar();

/**
 * Spiegelt den offen/geschlossen-Zustand der mobilen Menue-Schublade ins DOM:
 * setzt `data-mobile-nav` aufs <html> (styles.css schiebt die Sidebar dann als
 * Overlay ein/aus) und pflegt `aria-expanded` am Hamburger-Knopf. Reine
 * Darstellung – nur in der mobilen Ansicht sichtbar wirksam.
 *
 * @param {boolean} open true = Schublade einfahren, false = schliessen.
 */
function applyMobileNav(open) {
  document.documentElement.setAttribute("data-mobile-nav", open ? "open" : "closed");
  const burger = byId("nav-burger");
  if (burger) burger.setAttribute("aria-expanded", open ? "true" : "false");
}

/** Schaltet die mobile Menue-Schublade um (Hamburger-Knopf). */
function toggleMobileNav() {
  const open = document.documentElement.getAttribute("data-mobile-nav") !== "open";
  applyMobileNav(open);
}

/** Schliesst die mobile Menue-Schublade (Tipp auf Hintergrund / Menuewahl). */
function closeMobileNav() { applyMobileNav(false); }

const NODE_TYPE = {
  START: "START", END: "END", ACTIVITY: "ACTIVITY",
  AND_SPLIT: "AND_SPLIT", AND_JOIN: "AND_JOIN",
  XOR_SPLIT: "XOR_SPLIT", XOR_JOIN: "XOR_JOIN", SUBPROCESS: "SUBPROCESS",
  // Schleifenblock (K6): Begrenzer einer REPEAT-UNTIL-Schleife. Für das
  // Layout gewöhnliche serielle Knoten (je ein Ein-/Ausgang; die
  // Rücksprungkante ist nie gespeichert, der Graph bleibt azyklisch).
  LOOP_START: "LOOP_START", LOOP_END: "LOOP_END",
};
const LOOP_TYPES = new Set([NODE_TYPE.LOOP_START, NODE_TYPE.LOOP_END]);
const GATEWAYS = new Set([
  NODE_TYPE.AND_SPLIT, NODE_TYPE.AND_JOIN, NODE_TYPE.XOR_SPLIT, NODE_TYPE.XOR_JOIN,
]);
const DATA_TYPES = ["INTEGER", "FLOAT", "DECIMAL", "STRING", "DATE", "BOOLEAN", "URI"];
/**
 * Zahlentypen: Ganzzahl, Kommazahl und Betrag (DECIMAL). Eine
 * Stelle statt verstreuter ``=== "INTEGER" || === "FLOAT"``-Pruefungen, die
 * einen neuen Zahlentyp sonst stillschweigend wie Text behandelten.
 * @param {string} t Datentyp
 * @returns {boolean}
 */
function isNumericType(t) { return t === "INTEGER" || t === "FLOAT" || t === "DECIMAL"; }

/**
 * Zahl in deutscher Schreibweise mit fester Stellenzahl („7,6“, „1.234,5“).
 * Statt ``toFixed`` (Dezimalpunkt, kein Tausendertrennzeichen).
 * @param {number} x Zahl
 * @param {number} [digits=0] Nachkommastellen
 * @returns {string}
 */
function fmtNumber(x, digits) {
  const d = digits || 0;
  return Number(x).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
}

/**
 * Anzahl mit passendem Wort: „1 Eintrag“, „2 Einträge“, „0 Einträge“.
 * @param {number} n Anzahl
 * @param {string} one Einzahl
 * @param {string} many Mehrzahl
 * @returns {string}
 */
function countLabel(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * Datum (ohne Uhrzeit) deutsch: „30.09.2026“. Ungueltiges bleibt, wie es ist.
 * @param {string} iso ISO-Zeitpunkt oder -Datum
 * @returns {string}
 */
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" });
}

/**
 * Anzeigewert eines Datenelements: Betraege immer mit zwei Nachkommastellen
 * und deutschem Trennzeichen („1.234,50“), Kommazahlen deutsch („8.880,5“),
 * Ganzzahlen ohne Tausenderpunkt (oft Kennnummern wie Kunden- oder
 * Lieferantennummer), Ja/Nein ausgeschrieben, alles andere unveraendert. Nur
 * Anzeige -- Eingabefelder erhalten den Rohwert.
 * @param {object|undefined} elem Datenelement (nur ``data_type`` wird gelesen)
 * @param {*} v Wert
 * @returns {string}
 */
function formatValue(elem, v) {
  if (v === null || v === undefined) return "\u2013";
  const t = elem && elem.data_type;
  if (t === "DECIMAL" && typeof v === "number") {
    return v.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  if (t === "FLOAT" && typeof v === "number") return v.toLocaleString("de-DE");
  if (t === "INTEGER" && typeof v === "number") return String(v);
  if (v === true) return "Ja";
  if (v === false) return "Nein";
  return String(v);
}
// Presentation widgets available per data type for the input-mask designer
// (mirrors model._WIDGETS_FOR_TYPE; the server has the final say via rule U2).
const WIDGETS_FOR_TYPE = {
  STRING: ["TEXT", "TEXTAREA", "DROPDOWN"],
  URI: ["TEXT"],
  INTEGER: ["NUMBER"],
  FLOAT: ["NUMBER"],
  DECIMAL: ["NUMBER"],
  BOOLEAN: ["CHECKBOX"],
  DATE: ["DATE"],
};
const WIDGET_LABELS = {
  TEXT: "Textfeld", TEXTAREA: "Textbereich", NUMBER: "Zahlenfeld",
  DROPDOWN: "Auswahlliste", CHECKBOX: "Kontrollk\u00E4stchen", DATE: "Datumsfeld",
};
const CONNECTOR_KINDS = ["MS_SQL", "MYSQL", "DYNAMICS_365", "SAP", "CUSTOM"];
// Structured scalar SQL-select (C4-C6): closed operator/aggregate/cardinality sets.
const SQL_OPERATORS = [["EQ", "="], ["NE", "\u2260"], ["LT", "<"], ["LE", "\u2264"], ["GT", ">"], ["GE", "\u2265"], ["LIKE", "LIKE"], ["IN", "IN"]];
const SQL_AGGREGATES = ["NONE", "COUNT", "SUM", "MIN", "MAX", "AVG"];
const SQL_CARDINALITIES = [["KEY_UNIQUE", "Eindeutiger Schl\u00FCssel"], ["AGGREGATE", "Aggregat (1 Zeile)"], ["FIRST_ORDERED", "Erste nach Sortierung"]];
// Domain events a webhook may subscribe to (mirrors outbox.WEBHOOK_EVENTS). The
// server validates the selection; this list only drives the checkbox picker.
const WEBHOOK_EVENT_TYPES = [
  "instance.started", "instance.completed", "task.ready", "task.completed", "task.incident",
  "task.cancelled",
];

// --------------------------------------------------------------------------
// Deutsche Bezeichnungen der Aufzaehlungen des Kerns
// --------------------------------------------------------------------------
// Der Kern liefert Aufzaehlungswerte roh (COMPLETED, READ_WRITE, RELEASED …);
// angezeigt wird nur, was hier steht. Ein Waechter
// (test_every_core_enum_value_has_a_german_label) verlangt fuer jeden Wert des
// Kerns einen Eintrag -- eine neue Auspraegung faellt also im Test auf, nicht
// als englischer Rohtext beim Kunden. Unbekanntes faellt auf den Rohwert zurueck.

/** Knotentypen (``NodeType``). */
const NODE_TYPE_LABELS = {
  START: "Start", END: "Ende", ACTIVITY: "Schritt",
  AND_SPLIT: "UND-Verzweigung", AND_JOIN: "UND-Zusammenf\u00FChrung",
  XOR_SPLIT: "Entscheidung", XOR_JOIN: "Entscheidung zusammengef\u00FChrt",
  SUBPROCESS: "Teilprozess", LOOP_START: "Schleifenbeginn", LOOP_END: "Schleifenende",
};
/** Zugriffsart einer Datenbindung (``AccessMode``). */
const ACCESS_MODE_LABELS = { READ: "liest", WRITE: "schreibt", READ_WRITE: "liest und schreibt" };
/** Herkunft eines Datenelements (``DataSourceKind``). */
const DATA_SOURCE_LABELS = { INSTANCE: "im Vorgang", EXTERNAL: "extern (Connector)" };
/** Lebenszyklus eines Schemas (``LifecycleState``). */
const LIFECYCLE_LABELS = {
  ENTWURF: "Entwurf", REVIEW: "in Pr\u00FCfung", RELEASED: "freigegeben",
  DEPRECATED: "abgel\u00F6st", ARCHIVED: "archiviert",
};
/** Webhook-Ereignisse (``outbox.WEBHOOK_EVENTS``); der technische Name bleibt daneben sichtbar. */
const WEBHOOK_EVENT_LABELS = {
  "instance.started": "Vorgang gestartet",
  "instance.completed": "Vorgang abgeschlossen",
  "task.ready": "Aufgabe bereit",
  "task.completed": "Aufgabe erledigt",
  "task.incident": "St\u00F6rung einer externen Aufgabe",
  "task.cancelled": "Externe Aufgabe verworfen",
};

/** @param {string} t Knotentyp @returns {string} deutsche Bezeichnung */
function nodeTypeLabel(t) { return NODE_TYPE_LABELS[t] || t; }
/** @param {string} m Zugriffsart @returns {string} deutsche Bezeichnung */
function accessModeLabel(m) { return ACCESS_MODE_LABELS[m] || m; }
/** @param {string} s Lebenszyklus @returns {string} deutsche Bezeichnung */
function lifecycleLabel(s) { return LIFECYCLE_LABELS[s] || s; }
/**
 * @param {string} ev technischer Ereignisname (z. B. „task.ready“)
 * @returns {string} „Aufgabe bereit (task.ready)“ -- der technische Name
 *   bleibt sichtbar, weil Empfaenger ihn im Webhook-Inhalt wiederfinden.
 */
function webhookEventLabel(ev) { return WEBHOOK_EVENT_LABELS[ev] ? `${WEBHOOK_EVENT_LABELS[ev]} (${ev})` : ev; }

// --------------------------------------------------------------------------
// DOM-Helfer
// --------------------------------------------------------------------------

/**
 * Sperrt einen Klick-Ausloeser, solange seine Aktion laeuft.
 *
 * Die eine Stelle gegen Doppelklicks: ``el`` legt jeden ``onClick`` hierueber
 * an, ``openModal`` damit auch jeden Bestaetigen-Knopf. Liefert der Handler
 * ein Promise (eine Aktion mit API-Aufruf), ist der Ausloeser bis zu dessen
 * Erledigung gesperrt -- weitere Klicks und ein zweites Enter im Dialog
 * (``confirmBtn.click()``) laufen ins Leere. Vorher legte ein Dreifachklick
 * auf „Instanz starten“ drei Vorgaenge an, ein Doppelklick auf „Anlegen“
 * zwei Schemata.
 *
 * Besonderheiten:
 * - Ein synchroner Handler (Filter, Umschalter) wird nie gesperrt.
 * - Knoepfe und Formularfelder sind waehrend der Aktion ``disabled`` und
 *   ``aria-busy``; andere Elemente (Tabellenzeilen …) tragen nur die Sperre.
 *   Ein ignorierter Klick ruft ``preventDefault`` (ein Kontrollkaestchen
 *   schaltet dann nicht um).
 * - Freigegeben wird bei Erfolg wie bei Fehler (``finally``) -- der Knopf ist
 *   danach wieder bedienbar, Eingaben eines offenen Dialogs bleiben stehen
 *   (der Dialog schliesst nur bei Erfolg, siehe ``openModal``). Ein Knopf, der
 *   beim Klick schon gesperrt war, loest im Browser gar nicht erst aus.
 * - Das Ergebnis (auch eine Ablehnung) reicht unveraendert durch.
 * - Fokus: Ein fokussierter Knopf, der ``disabled`` wird, gibt im Browser den
 *   Fokus an ``<body>`` ab. Waehrend der Sperre merkt ``lockedFocusOwner`` ihn
 *   deshalb als eigentlichen Fokusinhaber (ein Dialog, den die Aktion oeffnet,
 *   kennt so seinen Ausloeser, siehe ``focusOrigin``). Nach der Aktion bekommt
 *   der Knopf den Fokus zurueck -- aber nur, wenn er noch im Dokument steht und
 *   der Fokus noch auf ``<body>`` liegt; hat ein Dialog oder die Person ihn
 *   inzwischen woanders hingesetzt, bleibt er dort.
 *
 * @param {Element} node der Ausloeser.
 * @param {Function} handler der Klick-Handler.
 * @returns {Function} der Listener fuer ``addEventListener("click", …)``.
 */
function guardAsyncClick(node, handler) {
  let busy = false;
  return function guardedClick(e) {
    if (busy) {
      if (e && typeof e.preventDefault === "function") e.preventDefault();
      return undefined;
    }
    const result = handler.call(this, e);
    if (!result || typeof result.then !== "function") return result;
    busy = true;
    const lockable = /^(BUTTON|INPUT|SELECT|TEXTAREA)$/.test(node.tagName);
    const hadFocus = lockable && document.activeElement === node;
    // Lag der Knopf im Inhalt? Dann bekommt, falls er nach der Aktion weg ist,
    // der Inhalt den Fokus (unten) -- nie <body>.
    const contentRoot = hadFocus ? byId("content") : null;
    const inContent = !!(contentRoot && contentRoot.contains(node));
    if (hadFocus) lockedFocusOwner = node;
    if (lockable) {
      node.setAttribute("disabled", "");
      node.setAttribute("aria-busy", "true");
    }
    return Promise.resolve(result).finally(() => {
      busy = false;
      if (lockable) {
        node.removeAttribute("disabled");
        node.removeAttribute("aria-busy");
      }
      if (lockedFocusOwner === node) lockedFocusOwner = null;
      const a = document.activeElement;
      const free = !a || a === document.body || !a.isConnected;
      if (hadFocus && node.isConnected && free) {
        try { node.focus(); } catch (_e) { /* nur Komfort */ }
      } else if (hadFocus && inContent && free && !keepsViewState()
          && !byId("modal-root").children.length) {
        // Die Aktion hat neu gezeichnet und den Knopf ersetzt (Modellier- und
        // Verwaltungssichten; Laufzeit-Sichten stellen ihn selbst ueber die
        // Kennung wieder her, ein offener Dialog haelt den Fokus selbst).
        focusContentFallback();
      }
    });
  };
}

/**
 * Der Knopf, dem die Doppelklick-Sperre gerade den Fokus entzogen hat
 * (``guardAsyncClick``), solange seine Aktion laeuft; sonst ``null``.
 * @type {Element|null}
 */
let lockedFocusOwner = null;

/**
 * Wer fachlich gerade den Fokus haelt -- fuer die Fokus-Rueckgabe eines
 * Dialogs (``openModal``).
 *
 * Normalerweise ``document.activeElement``. Liegt der Fokus aber auf
 * ``<body>``, weil die Doppelklick-Sperre ihn dem ausloesenden Knopf entzogen
 * hat (der Dialog oeffnet erst, nachdem „Erledigen“ Vorgang und Schema geladen
 * hat), zaehlt dieser Knopf. So gab ein Dialog den Fokus frueher an ``<body>``
 * zurueck.
 *
 * @returns {Element|null} das Element; ``null`` ohne Fokus und ohne
 *   gesperrten Ausloeser. Ein gesperrter Ausloeser, der inzwischen nicht mehr
 *   im Dokument steht, zaehlt nicht.
 */
function focusOrigin() {
  const a = document.activeElement;
  if (a && a !== document.body) return a;
  if (lockedFocusOwner && lockedFocusOwner.isConnected) return lockedFocusOwner;
  return a || null;
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") {
        // Klicks laufen ueber die Doppelklick-Sperre (``guardAsyncClick``).
        const type = k.slice(2).toLowerCase();
        node.addEventListener(type, type === "click" ? guardAsyncClick(node, v) : v);
      } else node.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" || typeof c === "number"
      ? document.createTextNode(String(c)) : c);
  }
  return node;
}

const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVGNS, tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else node.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.appendChild(c);
  }
  return node;
}

/** Laufende Nummer fuer die Kennungen der Sperrgruende (``blockedButton``). */
let blockedReasonSeq = 0;

/**
 * Ein gesperrter Knopf, dessen Grund Tastatur und Screenreader erreichen.
 *
 * Ein Knopf mit ``disabled`` ist nicht fokussierbar; eine Erklaerung nur in
 * seinem ``title`` erreichen Tastatur, Screenreader und Touch nie. Deshalb
 * ist der Knopf hier nur ``aria-disabled`` (bleibt in der Tab-Reihenfolge und
 * wird als „nicht verfuegbar“ angesagt), ``aria-describedby`` zeigt auf den
 * Grund in einem unsichtbaren Element daneben, und ein Klick (auch per
 * Tastatur) loest nichts aus, sondern zeigt den Grund als Meldung. Der
 * ``title`` bleibt fuer die Maus.
 *
 * Nur Anzeige: Ob die Aktion zulaessig ist, entscheidet weiter der Kern.
 *
 * @param {string} cls CSS-Klassen des Knopfs (wie beim freien Knopf).
 * @param {string} label Beschriftung des Knopfs.
 * @param {string} reason Warum die Aktion hier nicht moeglich ist (ganzer Satz).
 * @returns {HTMLElement} ``span.blocked-action`` mit dem Knopf und dem Grund;
 *   jede Kennung ist eindeutig, auch wenn derselbe Knopf mehrfach auf der
 *   Seite steht (Kopfzeile und Inspektor).
 */
function blockedButton(cls, label, reason) {
  blockedReasonSeq += 1;
  const id = `blocked-reason-${blockedReasonSeq}`;
  return el("span", { class: "blocked-action" },
    el("button", { class: cls, type: "button", "aria-disabled": "true",
      "aria-describedby": id, title: reason,
      onClick: () => toast("info", reason) }, label),
    el("span", { id, class: "sr-only" }, reason));
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function byId(id) { return document.getElementById(id); }

/** Zaehler fuer ``nextDomId`` (eindeutig je Seitenaufruf). */
let domIdCounter = 0;

/**
 * Liefert eine neue, im Dokument eindeutige Kennung -- fuer Verweise wie
 * ``aria-labelledby``/``aria-describedby`` auf erzeugte Elemente.
 * @param {string} prefix lesbarer Anfang, z. B. ``"modal-title"``.
 * @returns {string} ``prefix-<n>``.
 */
function nextDomId(prefix) {
  domIdCounter += 1;
  return `${prefix}-${domIdCounter}`;
}

/**
 * Markiert ein Feld fuer Hilfsmittel als fehlerhaft (``aria-invalid``) oder
 * nimmt die Markierung zurueck.
 * @param {Element|null} control das Feld; ``null`` tut nichts.
 * @param {boolean} invalid true = fehlerhaft.
 */
function markInvalid(control, invalid) {
  if (!control) return;
  if (invalid) control.setAttribute("aria-invalid", "true");
  else control.removeAttribute("aria-invalid");
}

/**
 * Fuegt einer Liste von Kennungen (``aria-describedby``) eine hinzu oder
 * nimmt sie heraus, ohne andere Eintraege (etwa einen Hilfetext) anzutasten.
 * @param {Element} control das Feld.
 * @param {string} id die Kennung.
 * @param {boolean} on true = hinzufuegen, false = entfernen.
 */
function toggleDescribedBy(control, id, on) {
  const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/)
    .filter((x) => x && x !== id);
  if (on) ids.push(id);
  if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
  else control.removeAttribute("aria-describedby");
}

// --------------------------------------------------------------------------
// Ansichtszustand ueber ein Neuzeichnen (Fokus, aufgeklappte Bereiche)
// --------------------------------------------------------------------------
//
// Die Sichten bauen ihren Inhalt bei jedem Neuzeichnen komplett neu auf. Ohne
// Gegenmassnahme verschwindet dabei der fokussierte Knopf: Der Tastaturfokus
// faellt auf <body>, ein Enter danach geht ins Leere, und aufgeklappte
// Bereiche klappen zu -- auf den Laufzeit-Sichten alle paar Sekunden. Deshalb
// merkt ``render`` den Zustand vorher (``captureViewState``) und stellt ihn
// danach im neuen Inhalt wieder her (``restoreViewState``). Wiedergefunden wird
// ein Element ueber eine Kennung, nicht ueber den (verschwundenen) Knoten:
// Zeile (``data-row-key``), Beschriftung und Stelle; als Rueckfall die Liste
// (``data-focus-region``) und zuletzt der Inhaltsbereich selbst.

/** Was als Fokusziel zaehlt (Knoepfe, Links, Felder, Zusammenfassungen). */
const FOCUSABLE_SELECTOR = "button, a, input, select, textarea, summary, [tabindex]";

/**
 * Kennung des Elements, das zuletzt im Inhalt den Fokus hatte (gesetzt von
 * ``trackContentFocus``). Gebraucht, wenn der Fokus beim Neuzeichnen schon weg
 * ist: Ein Knopf, der waehrend seiner Aktion gesperrt wird (Doppelklick-Sperre),
 * gibt den Fokus im Browser an <body> ab, bevor neu gezeichnet wird.
 * @type {{node: Element, key: object}|null}
 */
let lastContentFocus = null;

/**
 * Kennung des Ausloesers des offenen Dialogs (``openModal``), falls er im
 * Inhalt lag. Zeichnet die Sicht hinter dem Dialog neu, gibt es den Ausloeser
 * danach nicht mehr; der Dialog gibt den Fokus dann an sein Gegenstueck im
 * neuen Inhalt zurueck (``restoreFocusKey``).
 * @type {object|null}
 */
let dialogOpenerKey = null;

/**
 * Sucht vom Element aufwaerts (bis ausschliesslich ``stop``) den ersten
 * Vorfahren mit dem Attribut ``attr``.
 *
 * @param {Element} node Startelement (zaehlt selbst mit).
 * @param {string} attr Attributname, z. B. ``"data-row-key"``.
 * @param {Element} stop Obergrenze der Suche (der Inhaltsbereich).
 * @returns {{node: Element, value: string}|null} Fundstelle oder ``null``.
 */
function ancestorWithAttr(node, attr, stop) {
  for (let n = node; n && n !== stop; n = n.parentNode) {
    if (n.hasAttribute && n.hasAttribute(attr)) return { node: n, value: n.getAttribute(attr) };
  }
  return null;
}

/**
 * Beschriftung eines Fokusziels, an der es nach dem Neuzeichnen
 * wiedererkannt wird.
 *
 * Reihenfolge: ``data-focus-key`` (eine feste Kennung fuer Ziele, deren Text
 * sich mit den Daten aendert -- etwa ein Filterknopf „Laufend (44)“, der nach
 * einem neuen Vorgang „Laufend (45)“ heisst); ``aria-label``; bei Feldern Art plus ``name``/Platzhalter (ihr
 * Text waere der Inhalt aller Optionen); sonst der sichtbare Text, gekuerzt.
 * Gleich beschriftete Ziele unterscheidet ``focusKeyOf`` ueber ihre Reihenfolge.
 *
 * @param {Element} node das Fokusziel.
 * @returns {string} Beschriftung (leer, wenn es keine gibt).
 */
function focusLabelOf(node) {
  const fixed = node.getAttribute && node.getAttribute("data-focus-key");
  if (fixed) return "key:" + fixed;
  const aria = node.getAttribute && node.getAttribute("aria-label");
  if (aria) return aria;
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(node.tagName)) {
    return node.tagName + ":" + (node.getAttribute("name") || node.getAttribute("placeholder")
      || node.getAttribute("type") || "");
  }
  return String(node.textContent || "").trim().slice(0, 80);
}

/**
 * Die Fokusziele innerhalb von ``scope`` in Dokumentreihenfolge.
 *
 * Rueckfallziele (``data-focus-region``) zaehlen nicht mit -- sie tragen nach
 * ihrem ersten Einsatz ein ``tabindex`` und verschoeben sonst die Stellen.
 *
 * @param {Element} scope Zeile oder Inhaltsbereich.
 * @returns {Element[]} die Ziele.
 */
function focusablesIn(scope) {
  return [...scope.querySelectorAll(FOCUSABLE_SELECTOR)]
    .filter((x) => !x.hasAttribute("data-focus-region"));
}

/**
 * Kennung eines Fokusziels im Inhaltsbereich, die ein Neuzeichnen uebersteht.
 *
 * @param {Element} node das fokussierte Element.
 * @param {Element} root der Inhaltsbereich (``#content``).
 * @returns {{row: (string|null), region: (string|null), label: string,
 *   nth: number, pos: number}|null} Zeile (``data-row-key``), Liste
 *   (``data-focus-region``), Beschriftung, Nummer unter gleich beschrifteten
 *   Zielen derselben Zeile bzw. desselben Bereichs und Stelle in der Zeile;
 *   ``null``, wenn ``node`` nicht im Inhalt liegt oder der Inhalt selbst ist.
 */
function focusKeyOf(node, root) {
  if (!node || !root || node === root || !root.contains(node)) return null;
  const row = ancestorWithAttr(node, "data-row-key", root);
  const region = ancestorWithAttr(node, "data-focus-region", root);
  const label = focusLabelOf(node);
  const scope = row ? row.node : root;
  const all = focusablesIn(scope);
  const same = all.filter((x) => focusLabelOf(x) === label);
  return {
    row: row ? row.value : null,
    region: region ? region.value : null,
    label,
    nth: Math.max(0, same.indexOf(node)),
    pos: Math.max(0, all.indexOf(node)),
  };
}

/**
 * Merkt das zuletzt im Inhalt fokussierte Element (``focusin`` am Dokument,
 * verdrahtet in ``wireNav``).
 *
 * @param {Event} e das Ereignis; nur Ziele in ``#content`` zaehlen.
 */
function trackContentFocus(e) {
  const content = byId("content");
  const key = focusKeyOf(e && e.target, content);
  if (key) lastContentFocus = { node: e.target, key };
}

/**
 * Setzt den Fokus auf ``node``, falls es fokussierbar ist.
 * @param {Element|null} node Ziel.
 * @returns {Element|null} ``node`` bei Erfolg, sonst ``null``.
 */
function focusNode(node) {
  if (!node || typeof node.focus !== "function") return null;
  try { node.focus(); } catch (_e) { return null; }
  return node;
}

/**
 * Setzt den Fokus im neu gezeichneten Inhalt auf das Gegenstueck einer
 * gemerkten Kennung (``focusKeyOf``).
 *
 * Reihenfolge der Ziele:
 * 1. dasselbe Ziel (Zeile, Beschriftung, Nummer) -- der Normalfall nach einer
 *    Live-Aktualisierung;
 * 2. gibt es die Zeile noch, aber das Ziel nicht (aus „Übernehmen“ wurde
 *    „Zurücklegen“), das Ziel an derselben Stelle der Zeile;
 * 3. ist die Zeile verschwunden (jemand anderes hat die Aufgabe erledigt), die
 *    Liste (``data-focus-region``) -- bewusst keine fremde Zeile, deren Knopf
 *    zufaellig genauso heisst;
 * 4. der Inhaltsbereich selbst. Nie ``<body>``.
 * Gesperrte Ziele werden uebersprungen. Rueckfallziele ohne eigenes
 * ``tabindex`` bekommen ``tabindex="-1"`` (per Programm fokussierbar, ohne
 * die Tab-Reihenfolge zu aendern).
 *
 * @param {object|null} key Kennung von ``focusKeyOf``; ``null`` tut nichts.
 * @param {Element} root der Inhaltsbereich.
 * @returns {Element|null} das fokussierte Element oder ``null``.
 */
function restoreFocusKey(key, root) {
  if (!key || !root) return null;
  const usable = (x) => !x.hasAttribute("disabled");
  const pick = (scope) => {
    const all = focusablesIn(scope).filter(usable);
    const same = all.filter((x) => focusLabelOf(x) === key.label);
    return same[Math.min(key.nth, same.length - 1)] || null;
  };
  if (key.row != null) {
    const row = [...root.querySelectorAll("[data-row-key]")]
      .find((x) => x.getAttribute("data-row-key") === key.row);
    if (row) {
      const all = focusablesIn(row).filter(usable);
      const hit = pick(row) || all[Math.min(key.pos, all.length - 1)];
      if (hit) return focusNode(hit);
    }
  } else {
    const hit = pick(root);
    if (hit) return focusNode(hit);
  }
  const region = key.region == null ? null
    : [...root.querySelectorAll("[data-focus-region]")]
      .find((x) => x.getAttribute("data-focus-region") === key.region);
  const target = region || root;
  if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
  return focusNode(target);
}

/**
 * Rueckfallziel, wenn das Fokusziel im Inhalt verschwunden ist: der
 * Inhaltsbereich selbst (``#content``, ``tabindex="-1"``). Nie ``<body>`` --
 * sonst begann Tab nach „Freigeben“ (der Knopf wird durch „Neue Revision“
 * ersetzt) wieder am Seitenanfang, und ein Screenreader verlor den Ort.
 *
 * Verwendet in Sichten ohne Wiedererkennung ueber Kennungen (Modellier- und
 * Verwaltungssichten, ``keepsViewState`` false): Dort gibt es keine Zeilen-
 * kennungen, ein gleich beschrifteter Knopf koennte zu einem anderen Schritt
 * gehoeren -- also bewusst nur der Inhaltsbereich. Ohne Bildlauf
 * (``preventScroll``), damit Canvas und Liste stehen bleiben.
 *
 * @returns {Element|null} der Inhaltsbereich, oder ``null`` ohne ihn.
 */
function focusContentFallback() {
  const content = byId("content");
  if (!content || typeof content.focus !== "function") return null;
  if (!content.hasAttribute("tabindex")) content.setAttribute("tabindex", "-1");
  try { content.focus({ preventScroll: true }); } catch (_e) { return null; }
  return content;
}

/**
 * Ob die gerade gezeigte Sicht ihren Ansichtszustand ueber ein Neuzeichnen
 * behaelt: die Laufzeit-Sichten, die sich von selbst aktualisieren
 * (``LIVE_VIEWS``). Modellier- und Verwaltungssichten bleiben unveraendert --
 * dort steuern die Sichten den Fokus selbst (Schritt-Karte, Tastennavigation).
 * @returns {boolean} true fuer eine Laufzeit-Sicht.
 */
function keepsViewState() {
  return LIVE_VIEWS.has(state.view);
}

/**
 * Merkt vor einem Neuzeichnen, was danach wiederhergestellt werden soll.
 *
 * Fokus: das fokussierte Ziel im Inhalt; ist ein Dialog offen, dessen
 * Ausloeser (der Dialog gibt den Fokus beim Schliessen dorthin zurueck); ist
 * der Fokus schon auf <body> gefallen, weil sein Ziel gesperrt oder entfernt
 * wurde, das zuletzt fokussierte Ziel. Liegt der Fokus anderswo (Seitenleiste),
 * wird er nicht gemerkt -- das Neuzeichnen holt ihn dann nicht zurueck.
 *
 * @param {Element} root der Inhaltsbereich (``#content``).
 * @returns {{focus: (object|null), open: string[]}} Fokus-Kennung und die
 *   Kennungen der aufgeklappten ``<details>`` (``detailsKeyOf``).
 */
function captureViewState(root) {
  if (!root) return { focus: null, open: [] };
  const a = document.activeElement;
  let focus = null;
  if (a && a !== root && root.contains(a)) focus = focusKeyOf(a, root);
  else if (byId("modal-root").children.length) focus = dialogOpenerKey;
  else if ((!a || a === document.body || a === root) && lastContentFocus
      && (!lastContentFocus.node.isConnected || lastContentFocus.node.hasAttribute("disabled"))) {
    focus = lastContentFocus.key;
  }
  const details = [...root.querySelectorAll("details")];
  const open = details.filter((d) => d.hasAttribute("open")).map((d) => detailsKeyOf(d, details));
  return { focus, open };
}

/**
 * Kennung eines aufklappbaren Bereichs: Text seiner Zusammenfassung plus
 * Nummer unter gleich benannten.
 * @param {Element} d das ``<details>``.
 * @param {Element[]} all alle ``<details>`` des Inhalts.
 * @returns {string} Kennung.
 */
function detailsKeyOf(d, all) {
  const name = (x) => {
    const s = x.querySelector("summary");
    return s ? String(s.textContent || "").trim() : "";
  };
  const own = name(d);
  return own + "#" + all.filter((x) => name(x) === own).indexOf(d);
}

/**
 * Stellt nach einem Neuzeichnen den mit ``captureViewState`` gemerkten
 * Zustand wieder her.
 *
 * Aufgeklappte Bereiche werden wieder geoeffnet. Den Fokus setzt sie nur, wenn
 * niemand sonst ihn haelt: Er liegt auf <body>, auf dem Inhaltsbereich oder auf
 * einem entfernten Element. Ein offener Dialog, eine Sicht, die selbst
 * fokussiert hat, oder ein Klick der Person in die Seitenleiste behalten ihn.
 *
 * @param {{focus: (object|null), open: string[]}|null} snap gemerkter Zustand.
 * @param {Element} root der Inhaltsbereich.
 */
function restoreViewState(snap, root) {
  if (!snap || !root) return;
  if (snap.open.length) {
    const details = [...root.querySelectorAll("details")];
    details.forEach((d) => {
      if (snap.open.includes(detailsKeyOf(d, details))) d.setAttribute("open", "");
    });
  }
  if (!snap.focus || byId("modal-root").children.length) return;
  const a = document.activeElement;
  const free = !a || a === document.body || a === root || !a.isConnected;
  if (free) restoreFocusKey(snap.focus, root);
}

// --------------------------------------------------------------------------
// Neuzeichnen erst nach dem Laden (Laufzeit-Sichten)
// --------------------------------------------------------------------------
//
// Die Laufzeit-Sichten leerten ``#content`` als Erstes und luden dann ihre
// Daten -- im Monitoring je Vorgang Detail und Aufgaben, bei vielen Vorgaengen
// mehrere Sekunden. So lange war die Sicht bei jeder Live-Aktualisierung und
// jedem Zeit-Tick leer, der Fokus lag auf ``<body>``, und ein Tastendruck ging
// ins Leere. Jetzt zeichnet eine Sicht beim Neuzeichnen derselben Sicht in
// einen Zwischenbehaelter (``beginViewContent``); der bisherige Inhalt bleibt
// stehen (``aria-busy``), bis ``render`` den fertigen Stand in einem Zug
// austauscht (``commitViewStage``).

/**
 * Der Zwischenbehaelter des laufenden Neuzeichnens, oder ``null``.
 * @type {{node: Element, content: Element, used: boolean, discarded: boolean}|null}
 */
let viewStage = null;

/**
 * Oeffnet fuer das folgende Neuzeichnen einen Zwischenbehaelter (aufgerufen
 * von ``render`` nur, wenn dieselbe Laufzeit-Sicht schon gezeichnet steht).
 *
 * Markiert den Inhalt als ``aria-busy`` -- Hilfsmittel erfahren, dass er
 * gleich ersetzt wird; ``#content[aria-busy]`` zeigt den Wartezeiger.
 * Ein noch offener Behaelter (sollte nicht vorkommen) wird ersetzt.
 *
 * @param {Element} content der Inhaltsbereich (``#content``).
 */
function openViewStage(content) {
  viewStage = { node: document.createElement("div"), content, used: false, discarded: false };
  content.setAttribute("aria-busy", "true");
}

/**
 * Wohin eine Sicht zeichnet: Erste Zeile jeder Laufzeit-Sicht (statt
 * ``clear(byId("content"))``).
 *
 * @returns {Element} der Zwischenbehaelter, wenn ``render`` einen geoeffnet hat
 *   (dann bleibt der bisherige Inhalt bis zum Austausch stehen); sonst der
 *   geleerte Inhaltsbereich -- etwa beim Wechsel in die Sicht, damit unter dem
 *   neuen Titel nie der Inhalt der alten Sicht steht. Ein Behaelter gilt fuer
 *   genau einen Aufruf.
 */
function beginViewContent() {
  if (viewStage && !viewStage.used) {
    viewStage.used = true;
    return viewStage.node;
  }
  const content = byId("content");
  clear(content);
  return content;
}

/**
 * Ob ein fertig gezeichneter Zwischenbehaelter auf den Austausch wartet --
 * dann steht im Inhalt noch der alte Stand, und ``render`` merkt den
 * Ansichtszustand erst jetzt (die Person kann sich waehrend des Ladens
 * bewegt haben). Zeichnete die Sicht direkt in den Inhalt, gilt der vor dem
 * Zeichnen gemerkte Zustand.
 * @returns {boolean} true bei angefordertem, nicht verworfenem Behaelter.
 */
function viewStagePending() {
  return !!(viewStage && viewStage.used && !viewStage.discarded);
}

/**
 * Tauscht den Inhalt gegen den fertig gezeichneten Zwischenbehaelter
 * (``render``, nach dem Zeichnen der Sicht, auch nach einem Fehler) und
 * nimmt ``aria-busy`` weg.
 *
 * Hat die Sicht den Behaelter nie angefordert, bleibt der Inhalt, wie die
 * Sicht ihn hinterliess. Wurde er verworfen (``discardViewStage``: Sitzung
 * beendet), kommt nichts davon auf den Schirm. Ohne offenen Behaelter ein
 * No-op.
 */
function commitViewStage() {
  const s = viewStage;
  viewStage = null;
  if (!s) return;
  s.content.removeAttribute("aria-busy");
  if (!s.used || s.discarded) return;
  clear(s.content);
  while (s.node.firstChild) s.content.appendChild(s.node.firstChild);
}

/**
 * Verwirft ein laufendes Neuzeichnen (``resetSessionState``): Was die Sicht
 * noch mit den Rechten der alten Sitzung laedt, darf danach nicht mehr
 * erscheinen. Ohne offenen Behaelter ein No-op.
 */
function discardViewStage() {
  if (viewStage) viewStage.discarded = true;
}

// --------------------------------------------------------------------------
// Toast + Fehlerbehandlung
// --------------------------------------------------------------------------

/**
 * Kurzmeldung unten rechts.
 *
 * Erfolg und Info verschwinden nach 3,5 s. Fehler bleiben stehen, bis sie
 * geschlossen werden (×-Knopf): Eine Ablehnung des Kerns traegt oft eine
 * Begruendung mit mehreren Zeilen, die nach sieben Sekunden noch nicht gelesen
 * ist. Damit sich Fehler nicht endlos stapeln,
 * bleiben hoechstens drei offen; die aeltesten werden verdraengt.
 *
 * @param {"ok"|"err"|"info"} kind Art der Meldung (Farbe, Verweildauer)
 * @param {string} title Ueberschrift
 * @param {string[]} [lines] optionale Detailzeilen
 */
function toast(kind, title, lines) {
  const root = byId("toast-root");
  const list = (lines && lines.length)
    ? el("ul", { class: "t-list" }, ...lines.map((l) => el("li", null, l)))
    : null;
  const sticky = kind === "err";
  const t = el("div", { class: "toast " + kind, role: sticky ? "alert" : "status" },
    sticky
      ? el("button", { class: "t-close", "aria-label": "Meldung schließen", title: "Schließen",
          onClick: () => t.remove() }, "\u00D7")
      : null,
    el("div", { class: "t-title" }, title), list);
  // Eine erfolgreiche Folgeaktion erledigt die offenen Fehler: Sie standen
  // sonst neben der Erfolgsmeldung, als waere noch etwas falsch.
  if (kind === "ok") clearToasts("err");
  // Dieselbe Meldung ein zweites Mal ersetzt die erste, statt sich darunter zu
  // stapeln (zweimal „Abschliessen“ = zweimal derselbe Pflichtfeld-Hinweis).
  [...root.querySelectorAll(".toast." + kind)]
    .filter((o) => o.textContent.replace(/^\u00D7/, "") === t.textContent.replace(/^\u00D7/, ""))
    .forEach((o) => o.remove());
  root.appendChild(t);
  if (sticky) {
    const open = [...root.querySelectorAll(".toast.err")];
    open.slice(0, Math.max(0, open.length - 3)).forEach((o) => o.remove());
    // Fehler bleiben lesbar stehen, aber nicht ewig.
    setTimeout(() => t.remove(), TOAST_ERROR_MS);
  } else {
    setTimeout(() => t.remove(), 3500);
  }
}

/** Wie lange eine Fehlermeldung ohne Zutun stehen bleibt. */
const TOAST_ERROR_MS = 20000;

/**
 * Schliesst offene Meldungen -- alle oder nur einer Art (``"err"``).
 * Aufgerufen nach einer erfolgreichen Aktion (nur Fehler) und beim Wechsel des
 * Logins (alle): Maras Z3-Fehler stand sonst bei Erika und Tom noch da.
 * @param {string} [kind] nur Meldungen dieser Art schliessen
 */
function clearToasts(kind) {
  const root = byId("toast-root");
  if (!root) return;
  root.querySelectorAll(kind ? `.toast.${kind}` : ".toast").forEach((t) => t.remove());
}

