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
  view: localStorage.getItem("view") || "model",
  schemaIds: [],
  schemaNames: {},
  // Version (revision) per schema id, captured alongside the name. Immutable
  // per id (a new revision gets a fresh id), so it is safe to cache.
  schemaVersions: {},
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

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
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

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function byId(id) { return document.getElementById(id); }

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

